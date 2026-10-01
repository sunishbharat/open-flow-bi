import re
from unittest.mock import patch

import dlt
import pendulum
import pyarrow.parquet as pq
import pytest

from openflowbi.jira.deployment import DeploymentProfile
from openflowbi.jira.fields import Field
from openflowbi.pipeline import run as pipeline_run

CLOUD_PROFILE = DeploymentProfile(
    is_cloud=True,
    base_url="https://example.atlassian.net",
    version="1001.0.0",
    auth=None,
    instance_id="example-atlassian-net",
)  # type: ignore[arg-type]

APACHE_JIRA = "https://issues.apache.org/jira"

FAKE_PROFILE = DeploymentProfile(
    is_cloud=False,
    base_url="https://fake.example",
    version="1",
    auth=None,
    instance_id="fake-example",
)  # type: ignore[arg-type]

# Six issues, one per day, each "updated" == "created" — oldest first, which
# is the order the (mocked) search endpoint would return them in when sorted
# `order by updated asc` (M5's incremental JQL, built by _jql).
ROWS = [
    {
        "id": str(i),
        "key": f"PROJ-{i}",
        "fields": {
            "created": f"2024-01-0{i}T00:00:00.000+0000",
            "updated": f"2024-01-0{i}T00:00:00.000+0000",
        },
    }
    for i in range(1, 7)
]
ALL_IDS = [row["id"] for row in ROWS]

_FLOOR_RE = re.compile(r'updated >= "([^"]+)"')


def _passes_floor(row, jql):
    """Apply a JQL `updated >= "YYYY-MM-DD HH:mm"` floor the way Jira does:
    minute-granular, in the account timezone (UTC in these tests). A
    date-only compare would let the 1-hour overlap (OVERLAP_SECONDS) pull in
    the previous day's issue.
    """
    match = _FLOOR_RE.search(jql)
    if not match:
        return True
    floor = pendulum.from_format(match.group(1), "YYYY-MM-DD HH:mm", tz="UTC")
    return pendulum.parse(row["fields"]["updated"]) >= floor


def _floor_filtered_search_pages(call_log):
    """Stand-in for _search_pages that filters ROWS the way a real Jira
    `updated >= "..."` JQL clause would, and records which issue ids each
    call actually fetched — the "request-count spy" the M5 plan calls for.
    """

    def fake(profile, jql, expand=None):
        ids = [row["id"] for row in ROWS if _passes_floor(row, jql)]
        call_log.append(ids)
        for row in ROWS:
            if row["id"] in ids:
                yield row

    return fake


def _kill_after(n):
    """Stand-in for _search_pages that raises after yielding n rows —
    simulating a process kill mid-extraction.
    """

    def fake(profile, jql, expand=None):
        for i, row in enumerate(ROWS):
            if i == n:
                raise RuntimeError("simulated kill")
            yield row

    return fake


def _loaded_issue_ids(out_dir):
    # issue_id is bigint on disk from M7.2 on (docs/phase2-postgres-design.md
    # §3) — cast back to str so comparisons against ALL_IDS/call_log (still
    # str, matching the raw Jira JSON shape) don't need to change throughout.
    files = list(out_dir.glob("jira_raw/issues/*.parquet"))
    ids: set[str] = set()
    for f in files:
        ids.update(str(v) for v in pq.read_table(f).column("issue_id").to_pylist())
    return sorted(ids, key=int)


# Same six issues as ROWS above, but each carries one changelog history item
# (expand tier, complete=True) — an issue with zero histories yields zero
# flatten.changelog() rows, so it could never exercise the M7.5 incremental
# cursor at all (see pipeline/source.py's issue_changelog comment).
CHANGELOG_ROWS = [
    {
        "id": str(i),
        "key": f"PROJ-{i}",
        "fields": {"updated": f"2024-01-0{i}T00:00:00.000+0000"},
        "changelog": {
            "total": 1,
            "histories": [
                {
                    "id": f"h{i}",
                    "created": f"2024-01-0{i}T00:00:00.000+0000",
                    "items": [
                        {"field": "status", "fieldId": "status", "to": "3", "toString": "Done"}
                    ],
                }
            ],
        },
    }
    for i in range(1, 7)
]
CHANGELOG_ALL_IDS = [row["id"] for row in CHANGELOG_ROWS]


def _floor_filtered_changelog_pages(call_log):
    """Same idea as _floor_filtered_search_pages, but yielding CHANGELOG_ROWS
    (embedded expand=changelog shape) instead of plain ROWS.
    """

    def fake(profile, jql, expand=None):
        ids = [row["id"] for row in CHANGELOG_ROWS if _passes_floor(row, jql)]
        call_log.append(ids)
        for row in CHANGELOG_ROWS:
            if row["id"] in ids:
                yield row

    return fake


def _loaded_changelog_issue_ids(out_dir, table="issue_changelog"):
    files = list(out_dir.glob(f"jira_raw/{table}/*.parquet"))
    ids: set[str] = set()
    for f in files:
        ids.update(str(v) for v in pq.read_table(f).column("issue_id").to_pylist())
    return sorted(ids, key=int)


@pytest.mark.vcr
def test_run_writes_parquet_with_issue_id_dc(tmp_path):
    profile = DeploymentProfile(
        is_cloud=False,
        base_url=APACHE_JIRA,
        version="8.20.10",
        auth=None,
        instance_id="issues-apache-org",
    )  # type: ignore[arg-type]

    info = pipeline_run.run(
        profile,
        project="KAFKA",
        limit=3,
        out_dir=tmp_path / "out",
        pipelines_dir=str(tmp_path / ".dlt"),
    )

    assert not info.has_failed_jobs

    parquet_files = list((tmp_path / "out").glob("jira_raw/issues/*.parquet"))
    assert parquet_files, "expected at least one issues parquet file"

    table = pq.read_table(parquet_files[0])
    columns = table.column_names
    assert "issue_id" in columns
    assert "issue_key" in columns
    # issue_id must be the identity dlt merges on — never issue_key.
    assert table.num_rows <= 3


def test_run_writes_parquet_for_fields(tmp_path):
    # Mocked, not a VCR cassette: unlike the search/changelog HTTP shape
    # (which genuinely differs between Cloud and Server/DC and is covered by
    # real/hand-authored cassettes in tests/jira/test_fields.py), fields.fetch()
    # is a single non-paginated GET with no deployment-specific branching, so
    # one mocked pipeline-wiring test covers both. Mocking (not a cassette)
    # also sidesteps a real dlt behaviour: DltResource extraction always runs
    # each resource on a worker thread (dlt/extract/pipe_iterator.py), and
    # that thread's teardown isn't synchronized tightly enough with vcrpy's
    # per-test cassette patch/unpatch for back-to-back cassette-based
    # pipeline.run() calls in the same process — it intermittently serves a
    # request made by test N's straggling worker thread through test N+1's
    # already-active cassette. Confirmed empirically (multiple reruns of a
    # cassette-based version of this test failed with cross-test cassette
    # mismatches); tests/jira/*.py's plain-function cassette tests aren't
    # affected since they never go through dlt's resource/pipe machinery.
    fake_fields = [
        Field(id="summary", name="Summary", schema_type="string", custom=False),
        Field(id="customfield_10032", name="Story Points", schema_type="number", custom=True),
    ]
    with patch("openflowbi.pipeline.source.fields_mod.fetch", return_value=fake_fields):
        info = pipeline_run.run(
            FAKE_PROFILE,
            out_dir=tmp_path / "out",
            pipelines_dir=str(tmp_path / ".dlt"),
            resources=("fields",),
        )

    assert not info.has_failed_jobs

    parquet_files = list((tmp_path / "out").glob("jira_raw/fields/*.parquet"))
    assert parquet_files, "expected at least one fields parquet file"

    table = pq.read_table(parquet_files[0])
    assert set(table.column_names) >= {"field_id", "name", "schema_type", "custom"}
    assert table.num_rows == 2


def test_run_writes_parquet_with_issue_id_cloud(tmp_path):
    # Mocked, not a VCR cassette — see test_run_writes_parquet_for_fields'
    # comment on the dlt worker-thread/vcrpy cassette race this sidesteps.
    # The Cloud-specific HTTP shape (POST /search/jql, cursor pagination)
    # this test doesn't re-exercise is covered directly, without going
    # through dlt's threaded resource machinery, by tests/pipeline/
    # test_source.py's _jql tests and (at the raw HTTP level)
    # tests/jira/test_changelog.py's Cloud cassettes.
    with (
        patch("openflowbi.pipeline.source.deployment_mod.account_timezone", return_value="UTC"),
        patch(
            "openflowbi.pipeline.source._search_pages",
            side_effect=_floor_filtered_search_pages([]),
        ),
    ):
        info = pipeline_run.run(
            CLOUD_PROFILE,
            project="PROJ",
            limit=2,
            out_dir=tmp_path / "out",
            pipelines_dir=str(tmp_path / ".dlt"),
        )

    assert not info.has_failed_jobs

    parquet_files = list((tmp_path / "out").glob("jira_raw/issues/*.parquet"))
    assert parquet_files, "expected at least one issues parquet file"

    table = pq.read_table(parquet_files[0])
    columns = table.column_names
    assert "issue_id" in columns
    assert "issue_key" in columns
    assert table.num_rows == 2


def test_kill_mid_run_commits_nothing_and_resume_recovers_every_issue(tmp_path):
    """A run killed mid-extraction must not advance the incremental
    watermark or write partial output — otherwise a later run would believe
    issues it never actually loaded had already been seen. Verified against
    dlt's real behaviour (pipeline.run() raises before the state-bump/
    commit_packages step it would otherwise reach), not assumed.
    """
    out_dir = tmp_path / "out"
    pipelines_dir = tmp_path / ".dlt"

    with (
        patch("openflowbi.pipeline.source.deployment_mod.account_timezone", return_value="UTC"),
        patch("openflowbi.pipeline.source._search_pages", side_effect=_kill_after(3)),
    ):
        with pytest.raises(Exception, match="simulated kill"):
            pipeline_run.run(
                FAKE_PROFILE, project="PROJ", out_dir=out_dir, pipelines_dir=str(pipelines_dir)
            )

    assert not list(out_dir.glob("**/*.parquet"))
    name = pipeline_run.pipeline_name(FAKE_PROFILE.instance_id, "PROJ")
    pipeline = dlt.attach(pipeline_name=name, pipelines_dir=str(pipelines_dir))
    assert pipeline.state.get("sources", {}) == {}, "a killed run must not persist a watermark"

    call_log: list[list[str]] = []
    with (
        patch("openflowbi.pipeline.source.deployment_mod.account_timezone", return_value="UTC"),
        patch(
            "openflowbi.pipeline.source._search_pages",
            side_effect=_floor_filtered_search_pages(call_log),
        ),
    ):
        info = pipeline_run.run(
            FAKE_PROFILE, project="PROJ", out_dir=out_dir, pipelines_dir=str(pipelines_dir)
        )

    assert not info.has_failed_jobs
    # The resumed run starts from the untouched initial cursor, not from
    # wherever the kill happened — every issue is recovered.
    assert call_log[0] == ALL_IDS
    assert _loaded_issue_ids(out_dir) == ALL_IDS


def test_limit_truncated_run_advances_cursor_without_skipping_unfetched_issues(tmp_path):
    """A --limit-truncated debug run must only ever advance the incremental
    watermark to the oldest-updated issue it actually fetched, so a later
    unlimited run still picks up whatever the limit cut off — never a silent
    skip (M5 design note in the build plan).
    """
    out_dir = tmp_path / "out"
    pipelines_dir = tmp_path / ".dlt"
    call_log: list[list[str]] = []

    with (
        patch("openflowbi.pipeline.source.deployment_mod.account_timezone", return_value="UTC"),
        patch(
            "openflowbi.pipeline.source._search_pages",
            side_effect=_floor_filtered_search_pages(call_log),
        ),
    ):
        pipeline_run.run(
            FAKE_PROFILE,
            project="PROJ",
            limit=3,
            out_dir=out_dir,
            pipelines_dir=str(pipelines_dir),
        )

    assert _loaded_issue_ids(out_dir) == ["1", "2", "3"]

    with (
        patch("openflowbi.pipeline.source.deployment_mod.account_timezone", return_value="UTC"),
        patch(
            "openflowbi.pipeline.source._search_pages",
            side_effect=_floor_filtered_search_pages(call_log),
        ),
    ):
        pipeline_run.run(
            FAKE_PROFILE, project="PROJ", out_dir=out_dir, pipelines_dir=str(pipelines_dir)
        )

    # The request-count spy: issues 1 and 2 were already loaded in the first
    # (limited) run and must never be re-requested by the second.
    assert "1" not in call_log[1]
    assert "2" not in call_log[1]
    assert _loaded_issue_ids(out_dir) == ALL_IDS


def test_changelog_limit_truncated_run_advances_cursor_without_skipping_unfetched_issues(
    tmp_path,
):
    """M7.5 acceptance (docs/phase2-postgres-design.md §14): issue_changelog
    is now incremental too — mirrors the `issues` test directly above, one
    resource swapped for the other.
    """
    out_dir = tmp_path / "out"
    pipelines_dir = tmp_path / ".dlt"
    call_log: list[list[str]] = []

    with (
        patch("openflowbi.pipeline.source.deployment_mod.account_timezone", return_value="UTC"),
        patch(
            "openflowbi.pipeline.source._search_pages",
            side_effect=_floor_filtered_changelog_pages(call_log),
        ),
    ):
        pipeline_run.run(
            FAKE_PROFILE,
            project="PROJ",
            limit=3,
            out_dir=out_dir,
            pipelines_dir=str(pipelines_dir),
            resources=("issue_changelog",),
        )

    assert _loaded_changelog_issue_ids(out_dir) == ["1", "2", "3"]
    assert _loaded_changelog_issue_ids(out_dir, "issue_changelog_status") == ["1", "2", "3"]

    with (
        patch("openflowbi.pipeline.source.deployment_mod.account_timezone", return_value="UTC"),
        patch(
            "openflowbi.pipeline.source._search_pages",
            side_effect=_floor_filtered_changelog_pages(call_log),
        ),
    ):
        pipeline_run.run(
            FAKE_PROFILE,
            project="PROJ",
            out_dir=out_dir,
            pipelines_dir=str(pipelines_dir),
            resources=("issue_changelog",),
        )

    assert "1" not in call_log[1]
    assert "2" not in call_log[1]
    assert _loaded_changelog_issue_ids(out_dir) == CHANGELOG_ALL_IDS


def _run_changelog(tmp_path, rows, limit=None):
    out_dir = tmp_path / "out"
    with (
        patch("openflowbi.pipeline.source.deployment_mod.account_timezone", return_value="UTC"),
        patch("openflowbi.pipeline.source._search_pages", return_value=iter(rows)),
        patch("openflowbi.jira.changelog.fetch_per_issue", return_value=None),
    ):
        pipeline_run.run(
            FAKE_PROFILE,
            project="PROJ",
            limit=limit,
            out_dir=out_dir,
            pipelines_dir=str(tmp_path / ".dlt"),
            resources=("issue_changelog",),
        )
    return out_dir


def test_changelog_status_row_is_written_for_every_issue_even_without_items(tmp_path):
    """Architecture review finding 4: an issue with no item rows (never
    transitioned, or per-issue tier unavailable) still gets a status row, so
    the transform can tell it apart from an issue never extracted at all.
    """
    rows = [
        {
            "id": "1",
            "fields": {"updated": "2024-01-01T00:00:00.000+0000"},
            "changelog": {"total": 0, "histories": []},
        },
        # Embedded page truncated, per-issue tier unavailable (patched above).
        {
            "id": "2",
            "fields": {"updated": "2024-01-02T00:00:00.000+0000"},
            "changelog": {"total": 5, "histories": []},
        },
    ]
    out_dir = _run_changelog(tmp_path, rows)

    assert _loaded_changelog_issue_ids(out_dir) == []
    files = list(out_dir.glob("jira_raw/issue_changelog_status/*.parquet"))
    status = {
        row["issue_id"]: (row["source"], row["changelog_complete"])
        for f in files
        for row in pq.read_table(f).to_pylist()
    }
    assert status == {1: ("expand", True), 2: ("per_issue", False)}


def test_changelog_limit_never_cuts_an_issue_in_half(tmp_path):
    """--limit counts issues for issue_changelog, not dlt yields: each issue
    yields its item rows and then its status row, and a yield-counting limit
    could stop between the two.
    """
    items = [{"field": "status", "to": "2"}, {"field": "assignee", "to": "x"}]
    rows = [
        {
            "id": str(i),
            "fields": {"updated": f"2024-01-0{i}T00:00:00.000+0000"},
            "changelog": {"total": 1, "histories": [{"id": f"h{i}", "items": items}]},
        }
        for i in (1, 2)
    ]
    out_dir = _run_changelog(tmp_path, rows, limit=1)

    loaded = [
        row["item_index"]
        for f in out_dir.glob("jira_raw/issue_changelog/*.parquet")
        for row in pq.read_table(f).to_pylist()
    ]
    assert sorted(loaded) == [0, 1]
    assert _loaded_changelog_issue_ids(out_dir, "issue_changelog_status") == ["1"]


def test_changelog_limit_never_skips_an_issue_deferred_to_a_fallback_tier(tmp_path):
    """Architecture review finding 6: issue 1's embedded changelog is truncated,
    so it needs the per-issue tier. That tier used to run after the whole walk,
    so --limit 2 loaded issues 2 and 3 instead, advancing the watermark past
    issue 1, which no later run would fetch.
    """
    rows = [
        {
            "id": str(i),
            "fields": {"updated": f"2024-01-0{i}T00:00:00.000+0000"},
            "changelog": {"total": 5 if i == 1 else 0, "histories": []},
        }
        for i in (1, 2, 3)
    ]
    out_dir = _run_changelog(tmp_path, rows, limit=2)

    assert _loaded_changelog_issue_ids(out_dir, "issue_changelog_status") == ["1", "2"]


def test_a_package_left_by_a_failed_load_is_loaded_by_the_next_run(tmp_path):
    """run() is split into extract/normalize/load so only load() sits under
    the advisory lock (architecture review finding 7). pipeline.run() used to
    finish a package a crashed run left behind; the split version must too.
    """
    out_dir = tmp_path / "out"
    pipelines_dir = str(tmp_path / ".dlt")

    def _run(rows):
        with (
            patch(
                "openflowbi.pipeline.source.deployment_mod.account_timezone", return_value="UTC"
            ),
            patch("openflowbi.pipeline.source._search_pages", return_value=iter(rows)),
        ):
            return pipeline_run.run(
                FAKE_PROFILE, project="PROJ", out_dir=out_dir, pipelines_dir=pipelines_dir
            )

    _run(ROWS[:2])
    # Killed after extract and normalize: the package for issues 3-4 stays
    # on disk, and the watermark already sits past them. (A first-ever run
    # killed this way is different: the destination has no dataset yet, so
    # dlt's sync_destination drops local state, watermark included, and the
    # next run re-reads everything.)
    with (
        patch.object(dlt.Pipeline, "load", side_effect=RuntimeError("killed mid-load")),
        pytest.raises(RuntimeError, match="killed mid-load"),
    ):
        _run(ROWS[2:4])
    assert _loaded_issue_ids(out_dir) == ALL_IDS[:2]

    info = _run(ROWS[4:])
    assert len(info.loads_ids) == 2  # the leftover package and this run's
    assert _loaded_issue_ids(out_dir) == ALL_IDS


def _issue(issue_id, updated):
    return {"id": issue_id, "key": f"PROJ-{issue_id}", "fields": {"updated": updated}}


def _run_returning(tmp_path, rows, jqls=None, **run_kwargs):
    """One real pipeline run whose search returns exactly `rows`, ignoring the
    JQL floor, so dlt's own incremental filter is the thing under test."""

    def fake(profile, jql, expand=None):
        if jqls is not None:
            jqls.append(jql)
        yield from rows

    with (
        patch("openflowbi.pipeline.source.deployment_mod.account_timezone", return_value="UTC"),
        patch("openflowbi.pipeline.source._search_pages", side_effect=fake),
    ):
        return pipeline_run.run(
            FAKE_PROFILE,
            project="PROJ",
            out_dir=tmp_path / "out",
            pipelines_dir=str(tmp_path / ".dlt"),
            **run_kwargs,
        )


def test_updates_straddling_a_dst_fall_back_both_load(tmp_path):
    """Architecture review finding 1. Run 2's issue is 40 minutes later in
    real time but sorts lower as a string (+0100 after the fall-back vs
    +0200 before it), so a string cursor silently dropped it."""
    _run_returning(tmp_path, [_issue("1", "2024-10-27T02:30:00.000+0200")])  # 00:30Z
    _run_returning(tmp_path, [_issue("2", "2024-10-27T02:10:00.000+0100")])  # 01:10Z

    assert _loaded_issue_ids(tmp_path / "out") == ["1", "2"]


def test_late_indexed_issue_inside_the_overlap_window_still_loads(tmp_path):
    """Architecture review finding 2. Cloud search is eventually consistent:
    issue 2 was updated before run 1's watermark but only became searchable
    afterwards. It is inside the 1-hour overlap, so run 2 keeps it; issue 3
    is older than the overlap and stays filtered out."""
    _run_returning(tmp_path, [_issue("1", "2024-01-01T10:00:00.000+0000")])

    jqls: list[str] = []
    _run_returning(
        tmp_path,
        [
            _issue("3", "2024-01-01T08:59:00.000+0000"),
            _issue("2", "2024-01-01T09:30:00.000+0000"),
        ],
        jqls,
    )

    # The JQL floor is lowered by the same hour, not only dlt's filter.
    assert 'updated >= "2024-01-01 09:00"' in jqls[0]
    assert _loaded_issue_ids(tmp_path / "out") == ["1", "2"]


def test_reset_watermark_re_walks_from_the_incremental_start(tmp_path):
    """`flowbi extract changelog --reset-watermark`: an issue older than the
    watermark (and its 1-hour lag) is filtered out by a normal run, and loads
    once the watermark is reset. The JQL floor drops back to the start too."""
    _run_returning(tmp_path, [_issue("1", "2024-01-01T10:00:00.000+0000")])
    old = [_issue("0", "2024-01-01T08:00:00.000+0000")]

    jqls: list[str] = []
    _run_returning(tmp_path, old, jqls)
    assert 'updated >= "2024-01-01 09:00"' in jqls[0]
    assert _loaded_issue_ids(tmp_path / "out") == ["1"]

    jqls.clear()
    _run_returning(tmp_path, old, jqls, reset_watermark=True)
    assert 'updated >= "2024-01-01' not in jqls[0]
    assert _loaded_issue_ids(tmp_path / "out") == ["0", "1"]


def test_reset_watermark_only_touches_the_resources_being_run(tmp_path):
    _run_returning(tmp_path, [_issue("1", "2024-01-01T10:00:00.000+0000")])
    name = pipeline_run.pipeline_name(FAKE_PROFILE.instance_id, "PROJ")
    pipeline = dlt.attach(pipeline_name=name, pipelines_dir=str(tmp_path / ".dlt"))

    pipeline_run._reset_watermarks(pipeline, ("issue_changelog",))

    resources = pipeline.state["sources"]["jira"]["resources"]
    assert "incremental" in resources["issues"]


def test_a_cursor_stored_as_a_string_before_the_fix_is_migrated(tmp_path):
    """Pipelines that ran before finding 1's fix hold a string last_value.
    Without the migration dlt raises TypeError comparing it with the datetime
    rows it gets now."""
    _run_returning(tmp_path, [_issue("1", "2024-01-01T10:00:00.000+0000")])
    name = pipeline_run.pipeline_name(FAKE_PROFILE.instance_id, "PROJ")
    pipeline = dlt.attach(pipeline_name=name, pipelines_dir=str(tmp_path / ".dlt"))
    with pipeline.managed_state() as state:
        cursor = state["sources"]["jira"]["resources"]["issues"]["incremental"]["updated_at"]
        cursor["last_value"] = "2024-01-01T10:00:00.000+0000"
        cursor["start_value"] = "1970-01-01T00:00:00.000+0000"
        cursor["initial_value"] = "1970-01-01T00:00:00.000+0000"

    info = _run_returning(tmp_path, [_issue("2", "2024-01-01T11:00:00.000+0000")])

    assert not info.has_failed_jobs
    assert _loaded_issue_ids(tmp_path / "out") == ["1", "2"]
