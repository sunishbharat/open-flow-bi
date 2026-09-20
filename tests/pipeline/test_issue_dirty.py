"""M7.5 acceptance proof (docs/phase2-postgres-design.md §8/§14):
`flowbi_ops.issue_dirty` is populated from a completed Postgres load, and
holds exactly the issue ids that load actually touched — not every issue
that has ever loaded, and not issues an earlier run already covered.

Needs Docker; excluded from the default `pytest` run (see the `postgres`
marker in pyproject.toml), same as test_concurrency.py. Run explicitly:
`uv run pytest -m postgres tests/pipeline/test_issue_dirty.py`.
"""

import re
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from testcontainers.community.postgres import PostgresContainer

from openflowbi.jira.deployment import DeploymentProfile
from openflowbi.ops.tables import metadata as ops_metadata
from openflowbi.pipeline import run as pipeline_run

pytestmark = pytest.mark.postgres

INSTANCE_ID = "issue-dirty-test"

PROFILE = DeploymentProfile(
    is_cloud=False,
    base_url="https://fake.example",
    version="1",
    auth=None,
    instance_id=INSTANCE_ID,
)  # type: ignore[arg-type]

# Four issues, one per day, each carrying one changelog history item — an
# issue with zero histories yields zero flatten.changelog() rows, so it can
# never end up in issue_dirty from that load (see pipeline/source.py's
# issue_changelog comment).
def _row(issue_id: int, day: int, history_suffix: str = "") -> dict[str, Any]:
    history_id = f"h{issue_id}{history_suffix}"
    return {
        "id": str(issue_id),
        "key": f"PROJ-{issue_id}",
        "fields": {"updated": f"2024-01-{day:02d}T00:00:00.000+0000"},
        "changelog": {
            "total": 1,
            "histories": [
                {
                    "id": history_id,
                    "created": f"2024-01-{day:02d}T00:00:00.000+0000",
                    "items": [{"field": "status", "to": "3", "toString": "Done"}],
                }
            ],
        },
    }


ROWS_RUN_1 = [_row(1, 1), _row(2, 2), _row(3, 3), _row(4, 4)]
# Between the two runs: issue 3 transitions again (a new history_id, a later
# `updated`) and issue 5 is newly created. Issue 4 is untouched — included
# again by the JQL floor (inclusive `updated >=`), but with the exact same
# primary key and cursor value as run 1, so dlt's own incremental
# deduplication (Incremental.primary_key) must filter it out before it ever
# reaches the destination.
ROWS_RUN_2 = [_row(1, 1), _row(2, 2), _row(3, 5, history_suffix="b"), _row(4, 4), _row(5, 6)]

_FLOOR_RE = re.compile(r'updated >= "([^"]+)"')


@pytest.fixture
def postgres_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        # dlt's PostgresCredentials pins drivername="postgresql" (no
        # "+psycopg2" suffix) — same fixup test_concurrency.py needs.
        dsn = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
        # Deliberately bypasses Alembic: this test only needs the four
        # flowbi_ops tables to exist, not migration history/version
        # tracking, which M7.3's own tests already cover separately.
        # migrations/env.py hits the same "schema must exist before
        # create_all" ordering gap on a genuinely fresh database (see its
        # comment); mirrored here rather than reusing Alembic wholesale.
        engine = sa.create_engine(dsn)
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE SCHEMA IF NOT EXISTS flowbi_ops"))
        ops_metadata.create_all(engine)
        engine.dispose()
        yield dsn


def _fake_search_pages(rows: list[dict[str, Any]]):
    def fake(profile: Any, jql: str, expand: str | None = None) -> Iterator[dict[str, Any]]:
        match = _FLOOR_RE.search(jql)
        floor_date = match.group(1)[:10] if match else None
        for row in rows:
            if floor_date is None or row["fields"]["updated"][:10] >= floor_date:
                yield row

    return fake


def _dirty_ids(dsn: str) -> set[int]:
    engine = sa.create_engine(dsn)
    try:
        with engine.connect() as conn:
            result = conn.execute(
                sa.text("SELECT issue_id FROM flowbi_ops.issue_dirty WHERE instance_id = :iid"),
                {"iid": INSTANCE_ID},
            )
            return {row.issue_id for row in result}
    finally:
        engine.dispose()


def _load_touched_ids(dsn: str, load_ids: list[str]) -> set[int]:
    """What a specific load actually wrote to jira_raw.issue_changelog — the
    same query dirty.mark_dirty() itself runs, used here independently to
    prove that call's input/output without depending on its internals.
    """
    engine = sa.create_engine(dsn)
    try:
        with engine.connect() as conn:
            result = conn.execute(
                sa.text(
                    "SELECT DISTINCT issue_id FROM jira_raw.issue_changelog "
                    "WHERE instance_id = :iid AND _dlt_load_id = ANY(:load_ids)"
                ),
                {"iid": INSTANCE_ID, "load_ids": load_ids},
            )
            return {row.issue_id for row in result}
    finally:
        engine.dispose()


def test_issue_dirty_has_exactly_the_touched_ids_per_load(tmp_path, postgres_dsn):
    pipelines_dir = tmp_path / ".dlt"

    def _run(rows: list[dict[str, Any]]) -> Any:
        with (
            patch(
                "openflowbi.pipeline.source.deployment_mod.account_timezone", return_value="UTC"
            ),
            patch(
                "openflowbi.pipeline.source._search_pages", side_effect=_fake_search_pages(rows)
            ),
        ):
            return pipeline_run.run(
                PROFILE,
                project="PROJ",
                destination="postgres",
                postgres_dsn=postgres_dsn,
                pipelines_dir=str(pipelines_dir),
                resources=("issue_changelog",),
            )

    info = _run(ROWS_RUN_1)
    assert not info.has_failed_jobs
    # Run 1: every issue is new, so this load touches all four.
    assert _load_touched_ids(postgres_dsn, list(info.loads_ids)) == {1, 2, 3, 4}
    assert _dirty_ids(postgres_dsn) == {1, 2, 3, 4}

    # Run 2 (M7.5 acceptance, docs/phase2-postgres-design.md §14): only issue
    # 3 (transitioned again) and issue 5 (new) actually changed. Issue 4 is
    # re-included by the inclusive `updated >= floor` JQL filter but is
    # byte-for-byte the same primary key + cursor value as run 1, so dlt's
    # incremental dedup must drop it before it reaches the destination —
    # this load must touch exactly {3, 5}, and issue_dirty (cumulative) must
    # gain issue 5 while leaving 1, 2 and 4 alone.
    info = _run(ROWS_RUN_2)
    assert not info.has_failed_jobs
    assert _load_touched_ids(postgres_dsn, list(info.loads_ids)) == {3, 5}
    assert _dirty_ids(postgres_dsn) == {1, 2, 3, 4, 5}
