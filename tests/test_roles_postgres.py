"""The whole pipeline runs as `flowbi_writer`.

`migrations/sql/roles.sql` used to grant schema-level CREATE/USAGE only. The tables Alembic and a
first extraction had already created as the bootstrap superuser stayed out of reach: no DML on
flowbi_ops, and no ALTER TABLE on analytics.issue for promoted columns. Everything worked only
because the documented DSN was the superuser's. The script also failed on a second run
(`CREATE ROLE` is not idempotent).

This test follows an existing deployment's path: Alembic and one extraction as the superuser,
then roles.sql (twice), then extract, promote and transform as flowbi_writer, and read the
result as cube_reader. roles.sql runs through the container's own psql, since it uses psql
variables and `\\gexec`.

Needs Docker (`postgres` marker), like every other test here that touches Postgres.
"""

import io
import tarfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from testcontainers.community.postgres import PostgresContainer

from openflowbi.fields import selection
from openflowbi.jira.deployment import DeploymentProfile
from openflowbi.pipeline import run as pipeline_run
from openflowbi.transform import runner

pytestmark = pytest.mark.postgres

REPO = Path(__file__).resolve().parents[1]
INSTANCE_ID = "roles-test"
WRITER_PW = "writer-test-pw"
READER_PW = "reader-test-pw"

PROFILE = DeploymentProfile(
    is_cloud=False,
    base_url="https://fake.example",
    version="1",
    auth=None,
    instance_id=INSTANCE_ID,
)  # type: ignore[arg-type]


def _issue(issue_id: int, day: int) -> dict[str, Any]:
    stamp = f"2024-01-{day:02d}T00:00:00.000+0000"
    return {
        "id": str(issue_id),
        "key": f"PROJ-{issue_id}",
        "fields": {
            "created": stamp,
            "updated": stamp,
            "project": {"key": "PROJ"},
            "status": {"id": "1", "name": "Open"},
            "customfield_10016": issue_id,
            "labels": ["a", "b"],
        },
        "changelog": {
            "total": 1,
            "histories": [
                {
                    "id": f"h{issue_id}",
                    "created": stamp,
                    "items": [
                        {
                            "field": "status",
                            "fieldId": "status",
                            "from": "1",
                            "fromString": "Open",
                            "to": "3",
                            "toString": "Done",
                        }
                    ],
                }
            ],
        },
    }


@pytest.fixture
def container() -> Iterator[PostgresContainer]:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg


def _dsn(pg: PostgresContainer, user: str, password: str) -> str:
    host, port = pg.get_container_host_ip(), pg.get_exposed_port(5432)
    return f"postgresql://{user}:{password}@{host}:{port}/{pg.dbname}"


def _run_roles_sql(pg: PostgresContainer) -> None:
    script = (REPO / "migrations" / "sql" / "roles.sql").read_bytes()
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        info = tarfile.TarInfo("roles.sql")
        info.size = len(script)
        tar.addfile(info, io.BytesIO(script))
    pg.get_wrapped_container().put_archive("/tmp", archive.getvalue())
    result = pg.exec(
        [
            "psql",
            "-U",
            pg.username,
            "-d",
            pg.dbname,
            "-v",
            f"writer_pw={WRITER_PW}",
            "-v",
            f"reader_pw={READER_PW}",
            "-f",
            "/tmp/roles.sql",
        ]
    )
    assert result.exit_code == 0, result.output.decode()


def _extract(dsn: str, rows: list[dict[str, Any]], pipelines_dir: Path) -> None:
    for resource in ("issues", "issue_changelog"):
        with (
            patch("openflowbi.pipeline.source.deployment_mod.account_timezone", return_value="UTC"),
            patch("openflowbi.pipeline.source._search_pages", return_value=iter(rows)),
        ):
            info = pipeline_run.run(
                PROFILE,
                project="PROJ",
                destination="postgres",
                postgres_dsn=dsn,
                pipelines_dir=str(pipelines_dir),
                resources=(resource,),
            )
        assert not info.has_failed_jobs


def test_the_whole_pipeline_runs_as_flowbi_writer(container, tmp_path, monkeypatch):
    superuser = _dsn(container, container.username, container.password)
    writer = _dsn(container, "flowbi_writer", WRITER_PW)
    reader = _dsn(container, "cube_reader", READER_PW)

    # An existing deployment: migrated and loaded once as the superuser.
    monkeypatch.setenv("FLOWBI_POSTGRES_DSN", superuser)
    command.upgrade(Config(str(REPO / "alembic.ini")), "head")
    _extract(superuser, [_issue(1, 1)], tmp_path / "su")

    _run_roles_sql(container)
    _run_roles_sql(container)  # re-runnable

    # From here on, only flowbi_writer.
    _extract(writer, [_issue(1, 1), _issue(2, 2)], tmp_path / "writer")
    selection.save_selection(
        writer,
        INSTANCE_ID,
        [
            selection.SelectionChange(
                field_name="Story Points",
                schema_type="number",
                field_id="customfield_10016",
                promote=True,
                column_name="story_points",
                target="column",
            ),
            selection.SelectionChange(
                field_name="Labels",
                schema_type="array",
                field_id="labels",
                promote=True,
                column_name="label",
                target="bridge_table",
            ),
        ],
        actor="test",
    )
    runner.run_transform(writer, INSTANCE_ID)

    # A later migration run as flowbi_writer works too (it owns alembic_version).
    monkeypatch.setenv("FLOWBI_POSTGRES_DSN", writer)
    command.upgrade(Config(str(REPO / "alembic.ini")), "head")

    engine = sa.create_engine(reader)
    try:
        with engine.connect() as conn:
            points = conn.execute(
                sa.text("SELECT issue_id, story_points FROM analytics.issue ORDER BY issue_id")
            ).fetchall()
            labels = conn.execute(sa.text("SELECT count(*) FROM analytics.label")).scalar_one()
            intervals = conn.execute(
                sa.text("SELECT count(*) FROM analytics.issue_status_interval")
            ).scalar_one()
    finally:
        engine.dispose()
    assert [(row[0], int(row[1])) for row in points] == [(1, 1), (2, 2)]
    assert labels == 4
    assert intervals == 4  # seeded from created_at, then the one transition, per issue
