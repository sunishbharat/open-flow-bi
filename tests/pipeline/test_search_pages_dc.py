"""Server/DC /search offset walk while issues change under it.

The walk is sorted `updated asc`. Editing an issue already read moves it to the end, so every
later row shifts left by one and a plain offset walk skips the row at the next page boundary.
That row is below the new watermark, so no later run revisits it.

HTTP is stubbed at `requests.adapters.HTTPAdapter.send`, as in test_search_pages_cloud.py. The
fake serves a live, mutable result set, re-sorted on every request, the way Jira does.
"""

import json
from collections.abc import Callable
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pendulum
import requests
from dlt.sources.helpers.rest_client.auth import BearerTokenAuth

from openflowbi.jira.deployment import DeploymentProfile
from openflowbi.pipeline.source import PAGE_SIZE, _search_pages

SERVER = DeploymentProfile(
    is_cloud=False,
    base_url="https://jira.example.com",
    version="9.12.0",
    auth=BearerTokenAuth("pat"),
    instance_id="jira-example-com",
)
JQL = 'project = ABC and updated >= "1970-01-01 00:00" order by updated asc'
BASE = pendulum.datetime(2024, 1, 1, tz="UTC")
WALK_STARTED = "Tue, 02 Jan 2024 00:00:00 GMT"
EDITED_DURING_WALK = "2024-01-02T00:05:00.000+0000"


def _stamp(at: pendulum.DateTime) -> str:
    return at.format("YYYY-MM-DD[T]HH:mm:ss.SSSZZ")


def _issues(n: int) -> list[dict[str, Any]]:
    """n issues, one minute apart, all updated before the walk starts."""
    return [{"id": str(i), "fields": {"updated": _stamp(BASE.add(minutes=i))}} for i in range(n)]


def _serve(
    issues: list[dict[str, Any]], after_request: Callable[[int], None] | None = None
) -> tuple[Any, list[int]]:
    """Fake Jira search over `issues`; `after_request(n)` runs after the n-th response."""
    starts: list[int] = []

    def send(self: Any, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
        query = parse_qs(urlsplit(request.url).query)
        assert query["jql"] == [JQL]
        start, size = int(query["startAt"][0]), int(query["maxResults"][0])
        ordered = sorted(issues, key=lambda i: pendulum.parse(i["fields"]["updated"]))
        body = {
            "startAt": start,
            "maxResults": size,
            "total": len(ordered),
            "issues": ordered[start : start + size],
        }
        starts.append(start)
        response = requests.Response()
        response.status_code = 200
        response.headers["Content-Type"] = "application/json"
        response.headers["Date"] = WALK_STARTED
        response._content = json.dumps(body).encode()
        response.url = request.url
        response.request = request
        if after_request is not None:
            after_request(len(starts))
        return response

    return send, starts


def _walk(send: Any) -> list[str]:
    with patch.object(requests.adapters.HTTPAdapter, "send", send):
        return [issue["id"] for issue in _search_pages(SERVER, JQL)]


def _edit(issues: list[dict[str, Any]], ids: set[str]) -> None:
    for issue in issues:
        if issue["id"] in ids:
            issue["fields"]["updated"] = EDITED_DURING_WALK


def test_walk_without_changes_reads_every_issue_once():
    issues = _issues(2 * PAGE_SIZE + 5)
    send, _ = _serve(issues)
    assert _walk(send) == [issue["id"] for issue in issues]


def test_an_issue_edited_after_its_page_was_read_does_not_skip_another():
    issues = _issues(3 * PAGE_SIZE)
    all_ids = [issue["id"] for issue in issues]

    def edit_one(n: int) -> None:
        if n == 1:
            _edit(issues, {"5"})

    send, _ = _serve(issues, edit_one)
    walked = _walk(send)

    # Was one short: the row at the page-1/page-2 boundary shifted into page 1 and was skipped.
    assert walked == all_ids


def test_more_edits_than_the_overlap_step_the_walk_further_back():
    issues = _issues(3 * PAGE_SIZE)
    all_ids = [issue["id"] for issue in issues]

    def edit_many(n: int) -> None:
        if n == 1:
            _edit(issues, {str(i) for i in range(30)})

    send, starts = _serve(issues, edit_many)
    walked = _walk(send)

    assert walked == all_ids
    assert len(starts) > 3  # it had to step back at least once


def test_issues_deleted_mid_walk_do_not_skip_the_rest():
    issues = _issues(PAGE_SIZE + 10)

    def delete_some(n: int) -> None:
        if n == 1:
            issues[:] = [issue for issue in issues if int(issue["id"]) not in range(10, 30)]

    send, _ = _serve(issues, delete_some)
    walked = _walk(send)

    expected = [str(i) for i in range(PAGE_SIZE + 10)]
    assert sorted(walked, key=int) == expected  # everything read, the deleted ones on page 1
    assert len(walked) == len(set(walked))


def test_an_issue_edited_before_it_was_read_is_left_for_the_next_run():
    # Yielding it would move the watermark to after the walk began, past any edit the walk
    # stepped over; left alone, its new `updated` is above this run's watermark.
    issues = _issues(2 * PAGE_SIZE)

    def edit_unread(n: int) -> None:
        if n == 1:
            _edit(issues, {str(PAGE_SIZE + 5)})

    send, _ = _serve(issues, edit_unread)
    walked = _walk(send)

    assert walked == [issue["id"] for issue in issues if issue["id"] != str(PAGE_SIZE + 5)]
