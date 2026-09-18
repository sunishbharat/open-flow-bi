import re
from unittest.mock import patch

import dlt
import pyarrow.parquet as pq
import pytest

from openflowbi.jira.deployment import DeploymentProfile
from openflowbi.jira.fields import Field
from openflowbi.pipeline import run as pipeline_run

CLOUD_PROFILE = DeploymentProfile(
    is_cloud=True, base_url="https://example.atlassian.net", version="1001.0.0", auth=None
)  # type: ignore[arg-type]

APACHE_JIRA = "https://issues.apache.org/jira"

FAKE_PROFILE = DeploymentProfile(
    is_cloud=False, base_url="https://fake.example", version="1", auth=None
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


def _floor_filtered_search_pages(call_log):
    """Stand-in for _search_pages that filters ROWS the way a real Jira
    `updated >= "..."` JQL clause would, and records which issue ids each
    call actually fetched — the "request-count spy" the M5 plan calls for.
    """

    def fake(profile, jql, expand=None):
        match = _FLOOR_RE.search(jql)
        floor_date = match.group(1)[:10] if match else None
        ids = [
            row["id"]
            for row in ROWS
            if floor_date is None or row["fields"]["updated"][:10] >= floor_date
        ]
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
    files = list(out_dir.glob("jira_raw/issues/*.parquet"))
    ids: set[str] = set()
    for f in files:
        ids.update(pq.read_table(f).column("issue_id").to_pylist())
    return sorted(ids, key=int)


@pytest.mark.vcr
def test_run_writes_parquet_with_issue_id_dc(tmp_path):
    profile = DeploymentProfile(is_cloud=False, base_url=APACHE_JIRA, version="8.20.10", auth=None)  # type: ignore[arg-type]

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
    pipeline = dlt.attach(pipeline_name="openflowbi", pipelines_dir=str(pipelines_dir))
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
