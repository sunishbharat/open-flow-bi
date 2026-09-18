from collections.abc import Iterable, Iterator
from typing import Any

import dlt
import requests
import structlog
from dlt.sources.helpers.rest_client import RESTClient
from dlt.sources.helpers.rest_client.paginators import JSONResponseCursorPaginator, OffsetPaginator

from openflowbi.jira import changelog as changelog_mod
from openflowbi.jira import deployment as deployment_mod
from openflowbi.jira import fields as fields_mod
from openflowbi.jira import flatten
from openflowbi.jira.deployment import DeploymentProfile

logger = structlog.get_logger(__name__)

PAGE_SIZE = 50

# Epoch: an unconfigured incremental start means "extract everything" on the
# first run, matching M3/M4's prior plain-full-pass behaviour.
DEFAULT_INCREMENTAL_START = "1970-01-01T00:00:00.000+0000"


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
    when both resources run in the same pipeline (CLAUDE.md prefers boring
    and readable over clever).
    """
    if profile.is_cloud:
        # POST /search/jql ignores startAt and returns no total — pagination
        # is strictly sequential via an opaque nextPageToken in the JSON body
        # (CLAUDE.md "Jira API facts").
        client = RESTClient(
            base_url=profile.base_url,
            auth=profile.auth,
            paginator=JSONResponseCursorPaginator(cursor_body_path="nextPageToken"),
        )
        body: dict[str, Any] = {"jql": jql, "maxResults": PAGE_SIZE, "fields": ["*all"]}
        if expand:
            body["expand"] = [expand]
        pages = client.paginate(
            "/rest/api/3/search/jql",
            method="POST",
            json=body,
            data_selector="issues",
        )
    else:
        # GET /search uses startAt offsets and does return total.
        client = RESTClient(
            base_url=profile.base_url,
            auth=profile.auth,
            paginator=OffsetPaginator(
                limit=PAGE_SIZE,
                offset_param="startAt",
                limit_param="maxResults",
                total_path="total",
            ),
        )
        params: dict[str, Any] = {"jql": jql, "fields": "*all"}
        if expand:
            params["expand"] = expand
        pages = client.paginate("/rest/api/2/search", params=params, data_selector="issues")

    for page in pages:
        yield from page


@dlt.source(name="jira")
def jira_source(
    profile: DeploymentProfile,
    project: str | None = None,
    incremental_start: str = DEFAULT_INCREMENTAL_START,
) -> tuple[Any, ...]:
    @dlt.resource(name="fields", write_disposition="replace")
    def fields() -> Iterator[dict[str, Any]]:
        yield from flatten.fields(fields_mod.fetch(profile.base_url, profile.auth))

    # max_table_nesting=0: `fields` stays a single passthrough JSON column
    # instead of exploding into per-instance child tables (CLAUDE.md: strict
    # models/relational explosion over custom fields break on the next Jira
    # instance they meet).
    @dlt.resource(
        name="issues",
        primary_key="issue_id",
        write_disposition="merge",
        max_table_nesting=0,
    )
    def issues(
        # dlt's own API requires the incremental default constructed inline in
        # the signature (it inspects the default via reflection) — not a B008
        # footgun here, just how dlt.sources.incremental is wired up.
        updated: dlt.sources.incremental[str] = dlt.sources.incremental(  # noqa: B008
            "updated_at", initial_value=incremental_start
        ),
    ) -> Iterator[dict[str, Any]]:
        # Cursor state persists in the pipeline's durable state (dlt), not a
        # hand-rolled watermark table — resume-after-kill and --limit safety
        # both fall out of this rather than being written by hand (M5).
        try:
            timezone = deployment_mod.account_timezone(profile.base_url, profile.auth)
        except requests.exceptions.RequestException:
            # CLAUDE.md says to assert the account timezone at startup; doctor
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
        floor = deployment_mod.jql_updated_floor(updated.start_value, timezone)
        yield from flatten.issues(_search_pages(profile, _jql(project, updated_since=floor)))

    @dlt.resource(
        name="issue_changelog",
        primary_key=("issue_id", "history_id", "item_index"),
        write_disposition="merge",
    )
    def issue_changelog() -> Iterator[dict[str, Any]]:
        raw_issues: Iterable[dict[str, Any]] = _search_pages(
            profile, _jql(project), expand="changelog"
        )
        batches = changelog_mod.fetch(profile.base_url, profile.auth, profile.is_cloud, raw_issues)
        yield from flatten.changelog(batches)

    return (fields, issues, issue_changelog)
