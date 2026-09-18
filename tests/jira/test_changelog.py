from unittest.mock import patch

import pytest

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
    mock_per_issue.assert_called_once_with(APACHE_JIRA, None, "1")
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

    mock_bulk.assert_called_once_with(APACHE_JIRA, None, ["1", "2"])
    mock_per_issue.assert_called_once_with(APACHE_JIRA, None, "2")
    assert ("1", "bulkfetch", True, [{"id": "hbulk"}]) in batches
    assert ("2", "per_issue", True, [{"id": "hfallback"}]) in batches


def test_fetch_orchestration_per_issue_unavailable_marks_incomplete():
    issues = [{"id": "1", "changelog": {"total": 5, "histories": []}}]
    with patch.object(changelog, "fetch_per_issue", return_value=None):
        batches = list(changelog.fetch(APACHE_JIRA, None, is_cloud=False, issues=issues))
    assert batches == [("1", "per_issue", False, [])]
