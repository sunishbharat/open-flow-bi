"""M7.4 concurrency proof (docs/phase2-postgres-design.md §6/§11).

`test_concurrent_projects` is "the one that would have caught dlt#4297" —
two projects, loaded into the same Postgres database at the same time, must
not collide on a shared staging dataset (the per-project
`staging_dataset_name_layout` in pipeline/run.py) or interleave badly under
the advisory lock (pipeline/locks.py). Real Postgres via
`testcontainers[postgres]`, not the docker-compose stack (§11: "so CI needs
no fixture wiring"); mocked Jira HTTP at the `_search_pages` boundary — the
same pattern test_run.py's mocked tests use, required by the M6
dlt-worker-thread/vcrpy finding (no cassette use anywhere near the load
layer).

Needs Docker; excluded from the default `pytest` run (see the `postgres`
marker in pyproject.toml) so the standing zero-network gate stays fast and
offline. Run explicitly: `uv run pytest -m postgres tests/pipeline/test_concurrency.py`.
"""

import re
import threading
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from testcontainers.community.postgres import PostgresContainer

from openflowbi.jira.deployment import DeploymentProfile
from openflowbi.pipeline import run as pipeline_run

pytestmark = pytest.mark.postgres

INSTANCE_ID = "concurrency-test"
PROJECTS = {"ALPHA": 1000, "BETA": 2000}
ROWS_PER_PROJECT = 5

PROFILE = DeploymentProfile(
    is_cloud=False,
    base_url="https://fake.example",
    version="1",
    auth=None,
    instance_id=INSTANCE_ID,
)  # type: ignore[arg-type]

_PROJECT_RE = re.compile(r"project = (\S+)")


@pytest.fixture(scope="module")
def postgres_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        # dlt's PostgresCredentials pins drivername="postgresql" (plain,
        # no "+psycopg2" suffix) — testcontainers' URL isn't a valid dlt DSN
        # as-is.
        yield pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")


def _rows(project: str, start_id: int, n: int) -> list[dict[str, Any]]:
    return [
        {
            "id": str(start_id + i),
            "key": f"{project}-{i}",
            "fields": {
                "created": f"2024-01-0{i + 1}T00:00:00.000+0000",
                "updated": f"2024-01-0{i + 1}T00:00:00.000+0000",
            },
        }
        for i in range(n)
    ]


def _fake_search_pages(rows_by_project: dict[str, list[dict[str, Any]]]):
    """One shared fake, keyed by the `project = ...` clause `_jql` builds —
    applied as a single patch around all threads (not one `with patch(...)`
    per thread), since two concurrent `with patch(...)` blocks on the same
    module attribute would race each other's setup/teardown.
    """

    def fake(profile: Any, jql: str, expand: str | None = None) -> Iterator[dict[str, Any]]:
        match = _PROJECT_RE.search(jql)
        assert match, f"expected a 'project = ...' clause, got: {jql}"
        yield from rows_by_project[match.group(1)]

    return fake


def test_concurrent_projects(postgres_dsn: str, tmp_path: Any) -> None:
    rows_by_project = {p: _rows(p, start, ROWS_PER_PROJECT) for p, start in PROJECTS.items()}
    results: dict[str, Any] = {}
    errors: list[BaseException] = []

    def worker(project: str) -> None:
        try:
            results[project] = pipeline_run.run(
                PROFILE,
                project=project,
                destination="postgres",
                postgres_dsn=postgres_dsn,
                resources=("issues",),
                # Without this, dlt falls back to its real host-local
                # ~/.dlt/pipelines/ directory, shared and NOT reset between
                # runs (unlike `postgres_dsn`'s fresh testcontainers
                # container). issues is incremental (M5): a second real
                # execution of this test on the same machine, with the
                # per-project pipeline_name's local watermark left over from
                # the first, then filters out every row as "already seen"
                # via dlt's own incremental dedup - mimicking exactly the
                # symptom this test exists to catch (rows missing after a
                # concurrent run) for a completely unrelated reason. Found
                # while investigating a spurious failure in an M7.5 session
                # (see docs/session-status-2026-09-18.md's M7.5 section) -
                # the staging-dataset/advisory-lock protection itself was
                # never actually broken.
                pipelines_dir=str(tmp_path / ".dlt"),
            )
        except BaseException as exc:  # noqa: BLE001 - surfaced via `errors`, not swallowed
            errors.append(exc)

    with (
        patch("openflowbi.pipeline.source.deployment_mod.account_timezone", return_value="UTC"),
        patch(
            "openflowbi.pipeline.source._search_pages",
            side_effect=_fake_search_pages(rows_by_project),
        ),
    ):
        threads = [threading.Thread(target=worker, args=(p,)) for p in PROJECTS]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)

    assert not errors, errors
    assert not any(info.has_failed_jobs for info in results.values())

    engine = sa.create_engine(postgres_dsn)
    with engine.connect() as conn:
        total = conn.execute(
            sa.text("SELECT count(*) FROM jira_raw.issues WHERE instance_id = :iid"),
            {"iid": INSTANCE_ID},
        ).scalar_one()
        # §6's proof: nothing lost (dlt#4297's TRUNCATE race silently drops
        # rows) and nothing duplicated (its INSERT race silently doubles
        # them) when two projects' staging tables would otherwise collide.
        assert total == len(PROJECTS) * ROWS_PER_PROJECT

        dupes = conn.execute(
            sa.text(
                "SELECT count(*) FROM (SELECT instance_id, issue_id FROM jira_raw.issues "
                "WHERE instance_id = :iid GROUP BY 1, 2 HAVING count(*) > 1) d"
            ),
            {"iid": INSTANCE_ID},
        ).scalar_one()
        assert dupes == 0
    engine.dispose()
