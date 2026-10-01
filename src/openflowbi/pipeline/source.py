import email.utils
import itertools
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import dlt
import pendulum
import requests
import structlog
from dlt.sources.helpers.rest_client import RESTClient
from dlt.sources.helpers.rest_client.paginators import JSONResponseCursorPaginator

from openflowbi.cloud.http import make_client
from openflowbi.jira import changelog as changelog_mod
from openflowbi.jira import deployment as deployment_mod
from openflowbi.jira import fields as fields_mod
from openflowbi.jira import flatten
from openflowbi.jira.deployment import DeploymentProfile

logger = structlog.get_logger(__name__)

PAGE_SIZE = 50

# Rows each Server/DC search page re-reads from the previous one, to detect
# rows that moved while the walk was running (see _dc_search_pages).
DC_PAGE_OVERLAP = 10

# Epoch: an unconfigured incremental start means "extract everything" on the
# first run, matching M3/M4's prior plain-full-pass behaviour.
DEFAULT_INCREMENTAL_START = "1970-01-01T00:00:00.000+0000"

# Every incremental run re-reads the last hour before its watermark
# (open-flow-bi-repo-structure_1.md §0). Cloud search is an eventually
# consistent index, so an issue can become searchable after the run that
# passed its `updated` time. Widening the JQL floor alone would not help:
# dlt also drops every row below the stored last_value, so the overlap has
# to be dlt's own `lag`, which lowers both (architecture review finding 2).
OVERLAP_SECONDS = 3600

# Per-issue changelog completeness (flatten.changelog_status) is a table of
# its own, fed by the issue_changelog resource as a dlt table variant: one
# search walk feeds both, and the status rows share the resource's cursor.
CHANGELOG_STATUS_TABLE = "issue_changelog_status"
_CHANGELOG_STATUS_HINTS = dlt.mark.make_hints(
    table_name=CHANGELOG_STATUS_TABLE,
    primary_key=("instance_id", "issue_id"),
    write_disposition="merge",
)


def parse_cursor(value: str) -> pendulum.DateTime:
    """Parse FLOWBI_JIRA_INCREMENTAL_START (or a stored cursor) into an aware datetime."""
    parsed = pendulum.parse(value)
    if not isinstance(parsed, pendulum.DateTime):
        raise ValueError(f"expected a full ISO datetime cursor value, got {value!r}")
    return parsed


def _jql(project: str | None, updated_since: str | None = None) -> str:
    """Build search JQL, optionally floored by an incremental `updated` cursor.

    Sorted by `updated asc` (not `created asc`) whenever a cursor is in play:
    a --limit-truncated run then only ever advances the incremental "last
    value" to the oldest-updated issue it actually fetched, so a later
    unlimited run resumes from there instead of silently skipping issues that
    were never walked (M5 design note in the build plan).
    """
    clauses = [f"project = {project}"] if project else []
    if updated_since:
        clauses.append(f'updated >= "{updated_since}"')
    where = " and ".join(clauses)
    order = "updated asc" if updated_since else "created asc"
    return f"{where} order by {order}" if where else f"order by {order}"


def _search_pages(
    profile: DeploymentProfile, jql: str, expand: str | None = None
) -> Iterator[dict[str, Any]]:
    """Yield raw issue dicts from /search, one search per call.

    Each resource that needs search results (issues, issue_changelog) calls
    this independently rather than sharing one HTTP generator between two dlt
    resources — a second search call is simpler and keeps both resources
    lazily limit-able on their own, at the cost of fetching issue pages twice
    when both resources run in the same pipeline (boring and readable
    over clever).
    """
    if profile.is_cloud:
        # POST /search/jql ignores startAt and returns no total — pagination
        # is strictly sequential via an opaque nextPageToken in the JSON body
        # (Jira Cloud's /search/jql contract). cursor_path is where the token is READ
        # from the response (dlt's default, "cursors.next", never matches Jira,
        # which silently stopped every Cloud walk after page 1);
        # cursor_body_path is where it is WRITTEN into the next request.
        client = make_client(
            profile.base_url,
            profile.auth,
            client_cert=profile.client_cert,
            paginator=JSONResponseCursorPaginator(
                cursor_path="nextPageToken", cursor_body_path="nextPageToken"
            ),
        )
        body: dict[str, Any] = {"jql": jql, "maxResults": PAGE_SIZE, "fields": ["*all"]}
        if expand:
            # A comma-separated string, not a JSON list — Cloud rejects the list form.
            body["expand"] = expand
        pages = client.paginate(
            "/rest/api/3/search/jql",
            method="POST",
            json=body,
            data_selector="issues",
        )
        for page in pages:
            yield from page
    else:
        client = make_client(profile.base_url, profile.auth, client_cert=profile.client_cert)
        params: dict[str, Any] = {"jql": jql, "fields": "*all"}
        if expand:
            params["expand"] = expand
        yield from _dc_search_pages(client, params)


# Rejected dlt's OffsetPaginator for Server/DC search: it advances a fixed
# offset and cannot re-read or step back based on what a page returned.
def _dc_search_pages(client: RESTClient, params: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Walk GET /rest/api/2/search by startAt offset without skipping issues.

    Only for JQL sorted `updated asc`, which is all the resources use.
    Editing an issue already read moves it to the end of that ordering, so
    every later row shifts left by one and a plain offset walk skips the row
    at the next page boundary, below the new watermark for good (architecture
    review finding 3). So each page re-reads DC_PAGE_OVERLAP rows of the
    previous one. Its first row must be one already yielded, still carrying
    the `updated` it had then: the rows yielded so far are then all at or
    before it, so nothing was skipped. If it isn't, more rows than the
    overlap moved away, and the walk steps further back until it is.

    The walk stops at the first issue updated after it began (the first
    response's `Date`, the server's own clock): an issue edited mid-walk is
    left for the next run rather than yielded, so the watermark never passes
    an edit this walk stepped over.
    """
    yielded: dict[str, Any] = {}  # issue id -> its `updated` when yielded
    ceiling: datetime | None = None
    start_at = 0
    rewind = DC_PAGE_OVERLAP
    next_position = 0  # just past the last row of the last page accepted
    while True:
        response = client.get(
            "/rest/api/2/search", params={**params, "startAt": start_at, "maxResults": PAGE_SIZE}
        )
        response.raise_for_status()
        if ceiling is None:
            ceiling = _server_time(response)
        data = response.json()
        page: list[dict[str, Any]] = data.get("issues", [])

        if start_at > 0 and not (page and _unchanged(page[0], yielded)):
            rewind *= 2
            start_at = max(0, next_position - rewind)
            logger.info("dc_search_rewind", start_at=start_at)
            continue

        for issue in page:
            updated = (issue.get("fields") or {}).get("updated")
            if updated is not None and parse_cursor(updated) > ceiling:
                return
            if issue["id"] not in yielded:
                yielded[issue["id"]] = updated
                yield issue

        next_position = start_at + len(page)
        if not page or next_position >= data.get("total", 0):
            return
        rewind = DC_PAGE_OVERLAP
        start_at = max(0, next_position - rewind)


def _unchanged(issue: dict[str, Any], yielded: dict[str, Any]) -> bool:
    updated = (issue.get("fields") or {}).get("updated")
    return issue["id"] in yielded and yielded[issue["id"]] == updated


def _server_time(response: requests.Response) -> datetime:
    """The response's `Date` header, or this host's clock if it has none."""
    header = response.headers.get("Date")
    if header:
        return email.utils.parsedate_to_datetime(header)
    return pendulum.now("UTC")


class JiraCredentialsError(RuntimeError):
    """Jira Cloud rejected the configured credentials."""


def _updated_floor(profile: DeploymentProfile, cursor_start: str | datetime) -> str:
    """Resolve an incremental cursor's JQL floor, in the account's timezone.

    Shared by `issues` and `issue_changelog` (M7.5) — both track the same
    Jira `updated` field, so the account-timezone lookup (and its
    anonymous-access UTC fallback, see the resources below) only needs to
    live in one place.
    """
    try:
        timezone = deployment_mod.account_timezone(
            profile.base_url, profile.auth, client_cert=profile.client_cert
        )
    except requests.exceptions.RequestException as exc:
        if profile.is_cloud:
            # Not a fallback on Cloud: Cloud may answer a search made with bad
            # credentials with an empty result instead of a 401, so carrying on
            # would turn wrong credentials into a "successful", empty extraction.
            # Cloud has no anonymous access for the UTC fallback to serve anyway.
            raise JiraCredentialsError(
                f"Jira Cloud rejected the credentials for {profile.base_url} (GET /myself "
                "failed). Check FLOWBI_JIRA_EMAIL and FLOWBI_JIRA_API_TOKEN belong to the "
                "same account, and run `flowbi doctor`."
            ) from exc
        # The account timezone should be asserted at startup; doctor
        # (M1) does that and fails loudly. Here, an unresolvable /myself
        # (anonymous access or an invalid token - the live Apache Jira
        # test target in this repo's docs uses a dummy PAT with no real
        # account) must not crash extraction outright, so this degrades
        # to UTC with a warning instead - callers with real credentials
        # never hit this branch.
        logger.warning(
            "account_timezone_unavailable", base_url=profile.base_url, fallback="UTC"
        )
        timezone = "UTC"
    return deployment_mod.jql_updated_floor(cursor_start, timezone)


@dlt.source(name="jira")
def jira_source(
    profile: DeploymentProfile,
    project: str | None = None,
    incremental_start: str = DEFAULT_INCREMENTAL_START,
    changelog_issue_limit: int | None = None,
) -> tuple[Any, ...]:
    """`changelog_issue_limit` is --limit for issue_changelog, counted in
    issues. dlt's add_limit counts yields, and that resource yields two per
    issue (item rows, status row), so add_limit could stop between them.
    """
    start = parse_cursor(incremental_start)

    # write_disposition="merge" + a compound (instance_id, field_id) key
    # (docs/phase2-postgres-design.md §4.3) — a plain "replace" would wipe
    # every other instance's fields sharing this dataset on the next run.
    @dlt.resource(
        name="fields",
        primary_key=("instance_id", "field_id"),
        write_disposition="merge",
    )
    def fields() -> Iterator[dict[str, Any]]:
        raw_fields = fields_mod.fetch(
            profile.base_url, profile.auth, client_cert=profile.client_cert
        )
        yield from flatten.fields(raw_fields, profile.instance_id)

    # max_table_nesting=0: `fields` stays a single passthrough JSON column
    # instead of exploding into per-instance child tables (strict
    # models/relational explosion over custom fields break on the next Jira
    # instance they meet).
    @dlt.resource(
        name="issues",
        primary_key=("instance_id", "issue_id"),
        write_disposition="merge",
        max_table_nesting=0,
    )
    def issues(
        # dlt's own API requires the incremental default constructed inline in
        # the signature (it inspects the default via reflection) — not a B008
        # footgun here, just how dlt.sources.incremental is wired up.
        # A datetime cursor, never the raw string: see flatten._instant.
        updated: dlt.sources.incremental[pendulum.DateTime] = dlt.sources.incremental(  # noqa: B008
            "updated_at", initial_value=start, lag=OVERLAP_SECONDS
        ),
    ) -> Iterator[dict[str, Any]]:
        # Cursor state persists in the pipeline's durable state (dlt), not a
        # hand-rolled watermark table — resume-after-kill and --limit safety
        # both fall out of this rather than being written by hand (M5).
        floor = _updated_floor(profile, updated.start_value)
        yield from flatten.issues(
            _search_pages(profile, _jql(project, updated_since=floor)), profile.instance_id
        )

    @dlt.resource(
        name="issue_changelog",
        primary_key=("instance_id", "issue_id", "history_id", "item_index"),
        write_disposition="merge",
    )
    def issue_changelog(
        # M7.5 (docs/phase2-postgres-design.md §14): incremental off the same
        # `updated` field as `issues`, tracked via each row's `updated_at`
        # (flatten.changelog's issue_updated map below) — a separate cursor,
        # not a literal shared watermark, since the two resources run from
        # separate CLI commands (`extract issues`/`extract changelog`) and
        # dlt scopes incremental state per resource regardless. §15 open
        # question #1 (chaining issue_changelog off issues as a dlt
        # transformer, to halve the HTTP search cost) is deferred again here
        # for the same reason noted since M4/M5: issues' own search doesn't
        # request expand=changelog, so chaining wouldn't save a request
        # unless `issues` always paid for changelog data most runs don't need.
        updated: dlt.sources.incremental[pendulum.DateTime] = dlt.sources.incremental(  # noqa: B008
            "updated_at", initial_value=start, lag=OVERLAP_SECONDS
        ),
    ) -> Iterator[Any]:
        floor = _updated_floor(profile, updated.start_value)
        raw_issues = _search_pages(profile, _jql(project, updated_since=floor), expand="changelog")

        # Tap each raw issue's fields.updated as it streams through, before
        # changelog_mod.fetch() consumes the generator (it buffers a search
        # page at a time for the bulkfetch/per-issue tiers, so the tap must
        # run first). flatten.changelog() stamps this onto every row as
        # `updated_at`, which is what the incremental cursor above tracks.
        issue_updated: dict[str, str] = {}

        def _tap(issues: Iterator[dict[str, Any]]) -> Iterator[dict[str, Any]]:
            for issue in issues:
                value = (issue.get("fields") or {}).get("updated")
                if value is not None:
                    issue_updated[issue["id"]] = value
                yield issue

        batches: Iterator[changelog_mod.ChangelogBatch] = changelog_mod.fetch(
            profile.base_url,
            profile.auth,
            profile.is_cloud,
            _tap(raw_issues),
            batch_size=PAGE_SIZE,
            client_cert=profile.client_cert,
        )
        if changelog_issue_limit is not None:
            batches = itertools.islice(batches, changelog_issue_limit)
        # Every issue yields its status row, even with zero histories, so the
        # cursor and issue_dirty see every extracted issue, and the transform
        # can tell "nothing extracted yet" from "no transitions" (finding 4).
        for batch in batches:
            yield list(flatten.changelog([batch], profile.instance_id, issue_updated))
            yield dlt.mark.with_hints(
                flatten.changelog_status(batch, profile.instance_id, issue_updated),
                _CHANGELOG_STATUS_HINTS,
                create_table_variant=True,
            )

    return (fields, issues, issue_changelog)
