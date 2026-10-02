import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from openflowbi.jira import changelog

APACHE_JIRA = "https://issues.apache.org/jira"


def test_from_expand_complete():
    issue = {"changelog": {"total": 2, "histories": [{"id": "1"}, {"id": "2"}]}}
    histories, complete = changelog.from_expand(issue)
    assert complete is True
    assert len(histories) == 2


def test_from_expand_incomplete():
    issue = {"changelog": {"total": 50, "histories": [{"id": "1"}]}}
    histories, complete = changelog.from_expand(issue)
    assert complete is False
    assert len(histories) == 1


def test_from_expand_missing_changelog_key():
    histories, complete = changelog.from_expand({})
    assert histories == []
    assert complete is True  # 0 >= 0: nothing embedded, nothing missing either


@pytest.mark.vcr
def test_fetch_per_issue_unavailable_returns_none_dc():
    # Apache's Jira 8.20.10 does not expose the standalone per-issue changelog
    # endpoint at all (404) — a real, live-recorded example of the "universal"
    # fallback tier itself being unavailable on an older Server build.
    result = changelog.fetch_per_issue(APACHE_JIRA, None, "KAFKA-1")
    assert result is None


@pytest.mark.vcr
def test_fetch_bulk_unavailable_on_dc():
    # Bulkfetch does not exist on Server/DC at all — confirmed 404 live.
    result = changelog.fetch_bulk(APACHE_JIRA, None, ["KAFKA-1"])
    assert result == {}


@pytest.mark.vcr
def test_fetch_per_issue_cloud():
    # No Cloud tenant available; hand-authored cassette. Unlike Apache's
    # Server/DC instance (test_fetch_per_issue_unavailable_returns_none_dc
    # above), Cloud does expose this endpoint — this is the success path
    # that instance can't demonstrate live.
    result = changelog.fetch_per_issue("https://example.atlassian.net", None, "10001")
    assert result is not None
    assert result[0]["id"] == "10001"


@pytest.mark.vcr
def test_fetch_bulk_cloud():
    # No Cloud tenant available yet; hand-authored cassette matching
    # Atlassian's documented bulkfetch response shape.
    result = changelog.fetch_bulk("https://example.atlassian.net", None, ["10001", "10002"])
    assert set(result) == {"10001", "10002"}
    assert result["10001"][0]["id"] == "9001"


@pytest.mark.vcr
def test_fetch_bulk_follows_next_page_token_cloud():
    # bulkfetch pages its histories. An issue
    # split across two pages must come back with both halves, in order.
    result = changelog.fetch_bulk("https://example.atlassian.net", None, ["10001", "10002"])
    assert [h["id"] for h in result["10001"]] == ["9001", "9002"]
    assert [h["id"] for h in result["10002"]] == ["9003"]


def _response(status: int, body: dict | None = None) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.headers["Content-Type"] = "application/json"
    response._content = json.dumps(body or {}).encode()
    return response


def _patched_session(post):
    client = MagicMock()
    client.session.post.side_effect = post
    return patch.object(changelog, "make_client", return_value=client), client


def test_fetch_bulk_sends_ids_in_chunks():
    ids = [str(i) for i in range(changelog.BULKFETCH_MAX_ISSUES + 1)]

    def post(url, json, auth):
        logs = [{"issueId": i, "changeHistories": []} for i in json["issueIdsOrKeys"]]
        return _response(200, {"issueChangeLogs": logs})

    patcher, client = _patched_session(post)
    with patcher:
        result = changelog.fetch_bulk(APACHE_JIRA, None, ids)

    sent = [call.kwargs["json"]["issueIdsOrKeys"] for call in client.session.post.call_args_list]
    assert [len(chunk) for chunk in sent] == [changelog.BULKFETCH_MAX_ISSUES, 1]
    assert set(result) == set(ids)


def test_fetch_bulk_rejected_chunk_falls_through_instead_of_raising():
    # A 400 (e.g. too many ids) must leave that chunk's issues to the
    # per-issue tier, not crash the whole extraction.
    def post(url, json, auth):
        if "bad" in json["issueIdsOrKeys"]:
            return _response(400, {"errorMessages": ["nope"]})
        return _response(200, {"issueChangeLogs": [{"issueId": "ok", "changeHistories": []}]})

    patcher, _ = _patched_session(post)
    with patcher, patch.object(changelog, "BULKFETCH_MAX_ISSUES", 1):
        result = changelog.fetch_bulk(APACHE_JIRA, None, ["bad", "ok"])
    assert result == {"ok": []}


def test_fetch_bulk_drops_a_chunk_whose_later_page_is_rejected():
    # Never return half an issue's history as if it were all of it.
    pages = iter(
        [
            _response(
                200,
                {
                    "issueChangeLogs": [{"issueId": "1", "changeHistories": [{"id": "h1"}]}],
                    "nextPageToken": "p2",
                },
            ),
            _response(400),
        ]
    )
    patcher, _ = _patched_session(lambda url, json, auth: next(pages))
    with patcher:
        assert changelog.fetch_bulk(APACHE_JIRA, None, ["1"]) == {}


def test_fetch_bulk_auth_failure_raises():
    patcher, _ = _patched_session(lambda url, json, auth: _response(401))
    with patcher, pytest.raises(requests.HTTPError):
        changelog.fetch_bulk(APACHE_JIRA, None, ["1"])


def test_fetch_orchestration_prefers_expand_when_complete():
    issues = [{"id": "1", "changelog": {"total": 1, "histories": [{"id": "h1"}]}}]
    batches = list(changelog.fetch(APACHE_JIRA, None, is_cloud=False, issues=issues))
    assert batches == [("1", "expand", True, [{"id": "h1"}])]


def test_fetch_orchestration_dc_skips_bulk_and_uses_per_issue():
    issues = [{"id": "1", "changelog": {"total": 5, "histories": []}}]
    with (
        patch.object(changelog, "fetch_bulk") as mock_bulk,
        patch.object(changelog, "fetch_per_issue", return_value=[{"id": "h9"}]) as mock_per_issue,
    ):
        batches = list(changelog.fetch(APACHE_JIRA, None, is_cloud=False, issues=issues))

    mock_bulk.assert_not_called()
    mock_per_issue.assert_called_once_with(APACHE_JIRA, None, "1", client_cert=None)
    assert batches == [("1", "per_issue", True, [{"id": "h9"}])]


def test_fetch_orchestration_cloud_tries_bulk_before_per_issue():
    issues = [
        {"id": "1", "changelog": {"total": 5, "histories": []}},
        {"id": "2", "changelog": {"total": 5, "histories": []}},
    ]
    with (
        patch.object(changelog, "fetch_bulk", return_value={"1": [{"id": "hbulk"}]}) as mock_bulk,
        patch.object(
            changelog, "fetch_per_issue", return_value=[{"id": "hfallback"}]
        ) as mock_per_issue,
    ):
        batches = list(changelog.fetch(APACHE_JIRA, None, is_cloud=True, issues=issues))

    mock_bulk.assert_called_once_with(APACHE_JIRA, None, ["1", "2"], client_cert=None)
    mock_per_issue.assert_called_once_with(APACHE_JIRA, None, "2", client_cert=None)
    assert ("1", "bulkfetch", True, [{"id": "hbulk"}]) in batches
    assert ("2", "per_issue", True, [{"id": "hfallback"}]) in batches


def test_fetch_yields_in_search_order_with_fallback_tiers_resolved_per_page():
    # The fallback tiers used to run only after the whole walk,
    # so a limit could stop the walk after later, tier-1-complete issues had advanced the
    # watermark, and the deferred, earlier-updated issue was never fetched.
    pulled: list[str] = []

    def search():
        for issue_id, total in (("1", 5), ("2", 0), ("3", 0), ("4", 5)):
            pulled.append(issue_id)
            yield {"id": issue_id, "changelog": {"total": total, "histories": []}}

    with patch.object(changelog, "fetch_per_issue", return_value=[{"id": "h"}]):
        batches = changelog.fetch(APACHE_JIRA, None, is_cloud=False, issues=search(), batch_size=2)
        first = next(batches)
        assert pulled == ["1", "2"]  # one page read, not the whole walk
        rest = list(batches)

    assert [(b[0], b[1]) for b in [first, *rest]] == [
        ("1", "per_issue"),
        ("2", "expand"),
        ("3", "expand"),
        ("4", "per_issue"),
    ]


def test_fetch_orchestration_per_issue_unavailable_marks_incomplete():
    issues = [{"id": "1", "changelog": {"total": 5, "histories": []}}]
    with patch.object(changelog, "fetch_per_issue", return_value=None):
        batches = list(changelog.fetch(APACHE_JIRA, None, is_cloud=False, issues=issues))
    assert batches == [("1", "per_issue", False, [])]
