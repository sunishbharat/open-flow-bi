"""Cloud /search/jql request shape and pagination, through dlt's real RESTClient + paginator.

Every other pipeline test mocks `_search_pages` wholesale, which is how two Cloud bugs went
unnoticed: the paginator never read `nextPageToken` (dlt's `cursor_path` defaults to
`cursors.next`), so extraction stopped after page 1, and `expand` was sent as a JSON list
rather than the comma-separated string Cloud accepts.

HTTP is stubbed at `requests.adapters.HTTPAdapter.send` - the lowest layer - so dlt's own
pagination logic runs unmodified. `_search_pages` is a plain generator, not a DltResource,
so the dlt worker-thread/vcrpy race from M6 doesn't apply here.
"""

import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import requests
from dlt.sources.helpers.rest_client.auth import HttpBasicAuth

from openflowbi.jira.deployment import DeploymentProfile
from openflowbi.pipeline.source import PAGE_SIZE, _search_pages

CLOUD = DeploymentProfile(
    is_cloud=True,
    base_url="https://example.atlassian.net",
    version="1001.0.0",
    auth=HttpBasicAuth("svc@example.com", "api-token"),
    instance_id="example-atlassian-net",
)


def _issue(issue_id: str) -> dict[str, Any]:
    return {"id": issue_id, "key": f"ABC-{issue_id}", "fields": {}}


def _serve(pages: list[dict[str, Any]]) -> tuple[Any, list[dict[str, Any]]]:
    """Fake HTTPAdapter.send: replies with `pages` in order, records each JSON request body."""
    sent: list[dict[str, Any]] = []
    remaining: Iterator[dict[str, Any]] = iter(pages)

    def send(self: Any, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
        assert request.method == "POST"
        assert request.url == "https://example.atlassian.net/rest/api/3/search/jql"
        raw = request.body.decode() if isinstance(request.body, bytes) else request.body
        sent.append(json.loads(raw or "{}"))
        response = requests.Response()
        response.status_code = 200
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps(next(remaining)).encode()
        response.url = request.url
        response.request = request
        response.encoding = "utf-8"
        return response

    return send, sent


def test_cloud_search_follows_next_page_token_across_pages():
    # Real /search/jql shape: nextPageToken at the top level, absent on the last page.
    send, sent = _serve(
        [
            {"issues": [_issue("1"), _issue("2")], "nextPageToken": "TOKEN-2", "isLast": False},
            {"issues": [_issue("3")], "isLast": True},
        ]
    )
    with patch.object(requests.adapters.HTTPAdapter, "send", send):
        issues = list(_search_pages(CLOUD, "project = ABC order by created asc"))

    assert [i["id"] for i in issues] == ["1", "2", "3"]  # was ["1", "2"]: stopped after page 1
    assert len(sent) == 2
    assert "nextPageToken" not in sent[0]
    assert sent[1]["nextPageToken"] == "TOKEN-2"


def test_cloud_search_request_body_shape():
    send, sent = _serve([{"issues": [_issue("1")], "isLast": True}])
    with patch.object(requests.adapters.HTTPAdapter, "send", send):
        list(_search_pages(CLOUD, "project = ABC order by created asc"))

    assert sent == [
        {"jql": "project = ABC order by created asc", "maxResults": PAGE_SIZE, "fields": ["*all"]}
    ]


def test_cloud_search_sends_expand_as_a_string_not_a_list():
    # /search/jql's `expand` is a comma-separated string; a live Cloud tenant rejects the
    # list form.
    send, sent = _serve([{"issues": [_issue("1")], "isLast": True}])
    with patch.object(requests.adapters.HTTPAdapter, "send", send):
        list(_search_pages(CLOUD, "project = ABC order by created asc", expand="changelog"))

    assert sent[0]["expand"] == "changelog"


def test_both_search_branches_present_the_client_cert():
    # The certificate must reach the wire from _search_pages on Cloud and on Server/DC alike.
    from dataclasses import replace

    from dlt.sources.helpers.rest_client.auth import BearerTokenAuth

    cert = ("/c/client-cert.pem", "/c/client-key.pem")
    certs: list[Any] = []

    def send(self: Any, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
        certs.append(kwargs.get("cert"))
        response = requests.Response()
        response.status_code = 200
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps(
            {"issues": [_issue("1")], "isLast": True, "total": 1, "startAt": 0, "maxResults": 50}
        ).encode()
        response.url = request.url
        response.request = request
        return response

    server = replace(CLOUD, is_cloud=False, auth=BearerTokenAuth("p"), client_cert=cert)
    with patch.object(requests.adapters.HTTPAdapter, "send", send):
        list(_search_pages(replace(CLOUD, client_cert=cert), "order by created asc"))
        list(_search_pages(server, "order by created asc"))

    assert [tuple(c) for c in certs] == [cert, cert]
