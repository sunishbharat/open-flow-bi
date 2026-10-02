import itertools
from collections.abc import Iterable, Iterator
from typing import Any

import requests
import structlog
from dlt.sources.helpers.rest_client.auth import AuthConfigBase
from dlt.sources.helpers.rest_client.paginators import OffsetPaginator

from openflowbi.cloud.http import ClientCert, make_client

logger = structlog.get_logger(__name__)

PER_ISSUE_PAGE_SIZE = 100

# Documented maximum of issueIdsOrKeys per bulkfetch request.
BULKFETCH_MAX_ISSUES = 1000

# Issues fetch() resolves together: one search page (pipeline/source.py's
# PAGE_SIZE, which passes it explicitly).
FETCH_BATCH_SIZE = 50

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
    base_url: str,
    auth: AuthConfigBase,
    issue_ids: list[str],
    *,
    client_cert: ClientCert | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """POST /changelog/bulkfetch. Cloud-only and still experimental
    — returns {} if the endpoint is unavailable so callers fall through to
    fetch_per_issue instead of crashing. Server/DC has no /rest/api/3/ at all;
    rather than a clean 404 it routes unknown paths to its normal web UI,
    returning HTML with a 200 status — so "unavailable" is detected by a
    non-JSON response, not just a 404 status code.

    The endpoint caps issues per request and pages its histories with a
    top-level nextPageToken, so ids are sent
    in chunks and every page of a chunk is read. A chunk is only returned
    once all of its pages are in: an issue split across pages must never be
    yielded as complete with half its history. A chunk the endpoint rejects
    (4xx other than auth) is left out, so its issues fall through to the
    per-issue tier instead of crashing the extraction.
    """
    base_url = base_url.rstrip("/")
    client = make_client(base_url, auth, client_cert=client_cert)
    result: dict[str, list[dict[str, Any]]] = {}
    for start in range(0, len(issue_ids), BULKFETCH_MAX_ISSUES):
        chunk = issue_ids[start : start + BULKFETCH_MAX_ISSUES]
        try:
            chunk_histories = _fetch_bulk_chunk(client.session, base_url, auth, chunk)
        except _BulkfetchUnavailable:
            # Not deployed on this instance: every later chunk would get the
            # same answer, so stop asking.
            break
        result.update(chunk_histories)
    return result


class _BulkfetchUnavailable(Exception):
    """The bulkfetch endpoint does not exist on this instance."""


# Rejected dlt's JSONResponseCursorPaginator here (used for /search/jql): it
# cannot tell Server/DC's HTML-with-200 "unavailable" answer from a real page,
# and a rejected chunk must fall through, not raise.
def _fetch_bulk_chunk(
    session: requests.Session, base_url: str, auth: AuthConfigBase, chunk: list[str]
) -> dict[str, list[dict[str, Any]]]:
    histories: dict[str, list[dict[str, Any]]] = {}
    body: dict[str, Any] = {"issueIdsOrKeys": chunk}
    while True:
        response = session.post(f"{base_url}/rest/api/3/changelog/bulkfetch", json=body, auth=auth)
        if response.status_code == 404 or "json" not in response.headers.get("Content-Type", ""):
            raise _BulkfetchUnavailable
        if 400 <= response.status_code < 500 and response.status_code not in (401, 403):
            logger.warning(
                "bulkfetch_chunk_rejected", status=response.status_code, issues=len(chunk)
            )
            return {}
        response.raise_for_status()
        data = response.json()
        for entry in data.get("issueChangeLogs", []):
            histories.setdefault(entry["issueId"], []).extend(entry.get("changeHistories", []))
        token = data.get("nextPageToken")
        if not token:
            return histories
        body = {"issueIdsOrKeys": chunk, "nextPageToken": token}


def fetch_per_issue(
    base_url: str,
    auth: AuthConfigBase,
    issue_id: str,
    *,
    client_cert: ClientCert | None = None,
) -> list[dict[str, Any]] | None:
    """GET /issue/{id}/changelog, paginated via startAt.

    Documented as the only universal path across Cloud and Server/DC
    — but some older Server builds don't expose it at all, so
    this returns None (rather than raising) on a 404, letting the caller mark
    changelog_complete=False instead of crashing the whole extraction.
    """
    client = make_client(
        base_url,
        auth,
        client_cert=client_cert,
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
    *,
    batch_size: int = FETCH_BATCH_SIZE,
    client_cert: ClientCert | None = None,
) -> Iterator[ChangelogBatch]:
    """3-tier changelog strategy: expand (free, riding the search response) ->
    bulkfetch (Cloud-only, one request for many issues) -> per-issue (the
    universal fallback). WRITE: no library implements this — it is the core
    domain value of the project.

    A tier's result is taken whole, never stitched with another tier's partial
    page — mixing partial slices from two tiers risks silent duplicate items.

    Issues are resolved `batch_size` at a time (one search page) and yielded
    in the order they arrived, which is the incremental cursor's order. A
    --limit-bounded caller can then only stop at an issue boundary in cursor
    order. Deferring tiers 2 and 3 to the end of the whole walk let the limit
    stop first: later, tier-1-complete issues advanced the watermark past
    the deferred ones, which were never fetched again. Buffering one page
    still stops the walk within a page of the limit (every command honours
    --limit).
    """
    for batch in itertools.batched(issues, batch_size):
        yield from _fetch_batch(base_url, auth, is_cloud, batch, client_cert)


def _fetch_batch(
    base_url: str,
    auth: AuthConfigBase,
    is_cloud: bool,
    batch: tuple[dict[str, Any], ...],
    client_cert: ClientCert | None,
) -> Iterator[ChangelogBatch]:
    resolved: dict[str, ChangelogBatch] = {}
    pending: list[str] = []
    for issue in batch:
        histories, complete = from_expand(issue)
        if complete:
            resolved[issue["id"]] = (issue["id"], "expand", True, histories)
        else:
            pending.append(issue["id"])

    if is_cloud and pending:
        bulk = fetch_bulk(base_url, auth, pending, client_cert=client_cert)
        for issue_id in pending:
            if issue_id in bulk:
                resolved[issue_id] = (issue_id, "bulkfetch", True, bulk[issue_id])
        pending = [issue_id for issue_id in pending if issue_id not in bulk]

    for issue_id in pending:
        per_issue_histories = fetch_per_issue(base_url, auth, issue_id, client_cert=client_cert)
        if per_issue_histories is None:
            resolved[issue_id] = (issue_id, "per_issue", False, [])
        else:
            resolved[issue_id] = (issue_id, "per_issue", True, per_issue_histories)

    for issue in batch:
        yield resolved[issue["id"]]
