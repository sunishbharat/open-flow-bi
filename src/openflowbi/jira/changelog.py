from collections.abc import Iterable, Iterator
from typing import Any

import requests
from dlt.sources.helpers.rest_client import RESTClient
from dlt.sources.helpers.rest_client.auth import AuthConfigBase
from dlt.sources.helpers.rest_client.paginators import OffsetPaginator

PER_ISSUE_PAGE_SIZE = 100

# (issue_id, source tier, complete, raw histories)
ChangelogBatch = tuple[str, str, bool, list[dict[str, Any]]]


def from_expand(issue: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """Extract changelog histories embedded via expand=changelog on the issue/search response.

    `complete` is False when the embedded page doesn't cover the full
    changelog (total > len(histories)) — Jira's changelog records transitions,
    not state, and its embedded expand is itself paginated, so a caller must
    fall back to bulkfetch/per-issue for the remainder.
    """
    changelog = issue.get("changelog") or {}
    histories = changelog.get("histories", [])
    total = changelog.get("total", len(histories))
    return histories, len(histories) >= total


def fetch_bulk(
    base_url: str, auth: AuthConfigBase, issue_ids: list[str]
) -> dict[str, list[dict[str, Any]]]:
    """POST /changelog/bulkfetch. Cloud-only and still experimental (CLAUDE.md)
    — returns {} if the endpoint is unavailable so callers fall through to
    fetch_per_issue instead of crashing. Server/DC has no /rest/api/3/ at all;
    rather than a clean 404 it routes unknown paths to its normal web UI,
    returning HTML with a 200 status — so "unavailable" is detected by a
    non-JSON response, not just a 404 status code.
    """
    base_url = base_url.rstrip("/")
    client = RESTClient(base_url=base_url, auth=auth)
    response = client.session.post(
        f"{base_url}/rest/api/3/changelog/bulkfetch",
        json={"issueIdsOrKeys": issue_ids},
        auth=auth,
    )
    if response.status_code == 404 or "json" not in response.headers.get("Content-Type", ""):
        return {}
    response.raise_for_status()
    data = response.json()
    return {
        entry["issueId"]: entry.get("changeHistories", [])
        for entry in data.get("issueChangeLogs", [])
    }


def fetch_per_issue(
    base_url: str, auth: AuthConfigBase, issue_id: str
) -> list[dict[str, Any]] | None:
    """GET /issue/{id}/changelog, paginated via startAt.

    Documented as the only universal path across Cloud and Server/DC
    (CLAUDE.md) — but some older Server builds don't expose it at all, so
    this returns None (rather than raising) on a 404, letting the caller mark
    changelog_complete=False instead of crashing the whole extraction.
    """
    base_url = base_url.rstrip("/")
    client = RESTClient(
        base_url=base_url,
        auth=auth,
        paginator=OffsetPaginator(
            limit=PER_ISSUE_PAGE_SIZE,
            offset_param="startAt",
            limit_param="maxResults",
            total_path="total",
        ),
    )
    try:
        histories: list[dict[str, Any]] = []
        path = f"/rest/api/2/issue/{issue_id}/changelog"
        for page in client.paginate(path, data_selector="values"):
            histories.extend(page)
        return histories
    except requests.exceptions.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return None
        raise


def fetch(
    base_url: str,
    auth: AuthConfigBase,
    is_cloud: bool,
    issues: Iterable[dict[str, Any]],
) -> Iterator[ChangelogBatch]:
    """3-tier changelog strategy: expand (free, riding the search response) ->
    bulkfetch (Cloud-only, one request for many issues) -> per-issue (the
    universal fallback). WRITE: no library implements this — it is the core
    domain value of the project (CLAUDE.md / library-decision-register).

    A tier's result is taken whole, never stitched with another tier's partial
    page — mixing partial slices from two tiers risks silent duplicate items.

    Tier-1 results are yielded as they're seen (not collected into a dict
    first) so a --limit-bounded caller stops pulling search pages as soon as
    enough rows have been produced, rather than walking the whole project
    before truncating (CLAUDE.md rule 6).
    """
    pending: list[str] = []
    for issue in issues:
        histories, complete = from_expand(issue)
        if complete:
            yield issue["id"], "expand", True, histories
        else:
            pending.append(issue["id"])

    if is_cloud and pending:
        bulk = fetch_bulk(base_url, auth, pending)
        pending = [issue_id for issue_id in pending if issue_id not in bulk]
        for issue_id, histories in bulk.items():
            yield issue_id, "bulkfetch", True, histories

    for issue_id in pending:
        per_issue_histories = fetch_per_issue(base_url, auth, issue_id)
        if per_issue_histories is None:
            yield issue_id, "per_issue", False, []
        else:
            yield issue_id, "per_issue", True, per_issue_histories
