"""M7.6 acceptance proof, literally (docs/phase2-postgres-design.md §14):
"A seeded duplicate makes `flowbi quality check --destination postgres` exit
1 and write `status='quality_failed'`." Ties together quality/sql_checks.py,
ops/sync_run.py and cli.py's `quality check --destination postgres` path.

Needs Docker; excluded from the default `pytest` run (see the `postgres`
marker in pyproject.toml), same as the other Postgres-backed test files. Run
explicitly: `uv run pytest -m postgres tests/test_cli_quality_postgres.py`.
"""

from collections.abc import Iterator
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from testcontainers.community.postgres import PostgresContainer
from typer.testing import CliRunner

from openflowbi.cli import app
from openflowbi.jira.deployment import DeploymentProfile
from openflowbi.ops.tables import metadata as ops_metadata

pytestmark = pytest.mark.postgres

runner = CliRunner()
INSTANCE_ID = "cli-quality-test"

PROFILE = DeploymentProfile(
    is_cloud=False,
    base_url="https://fake.example",
    version="1",
    auth=None,
    instance_id=INSTANCE_ID,
)  # type: ignore[arg-type]


@pytest.fixture
def postgres_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        dsn = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
        engine = sa.create_engine(dsn)
        with engine.begin() as conn:
            # flowbi_ops for sync_run — bypasses Alembic like the other
            # Postgres-backed tests (M7.3's own tests cover migration history).
            conn.execute(sa.text("CREATE SCHEMA IF NOT EXISTS flowbi_ops"))
        ops_metadata.create_all(engine)
        with engine.begin() as conn:
            # Minimal jira_raw shape, hand-written only for this throwaway
            # fixture — dlt still owns the real schema (§3).
            conn.execute(sa.text("CREATE SCHEMA IF NOT EXISTS jira_raw"))
            conn.execute(
                sa.text("CREATE TABLE jira_raw.issues (instance_id text, issue_id bigint)")
            )
            conn.execute(
                sa.text(
                    "CREATE TABLE jira_raw.issue_changelog ("
                    "instance_id text, issue_id bigint, history_id text, item_index bigint, "
                    "changelog_complete boolean)"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO jira_raw.issue_changelog "
                    "(instance_id, issue_id, history_id, item_index) VALUES "
                    "(:i, 1, 'h1', 0), (:i, 1, 'h1', 0)"
                ),
                {"i": INSTANCE_ID},
            )
        engine.dispose()
        yield dsn


def _sync_run_rows(dsn: str) -> list[sa.Row]:
    engine = sa.create_engine(dsn)
    try:
        with engine.connect() as conn:
            return list(
                conn.execute(
                    sa.text(
                        "SELECT status, mode, error FROM flowbi_ops.sync_run "
                        "WHERE instance_id = :i"
                    ),
                    {"i": INSTANCE_ID},
                )
            )
    finally:
        engine.dispose()


def test_quality_check_postgres_fails_on_seeded_duplicate_and_records_sync_run(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLOWBI_JIRA_BASE_URL", "https://fake.example")
    monkeypatch.setenv("FLOWBI_JIRA_PAT", "unused")
    monkeypatch.setenv("FLOWBI_POSTGRES_DSN", postgres_dsn)

    with patch("openflowbi.cli.deployment.detect", return_value=PROFILE):
        result = runner.invoke(
            app, ["quality", "check", "issue_changelog", "--destination", "postgres"]
        )

    assert result.exit_code == 1
    assert "FAILED" in result.stdout
    assert "changelog_pk_unique" in result.stdout

    rows = _sync_run_rows(postgres_dsn)
    assert len(rows) == 1
    assert rows[0].status == "quality_failed"
    assert rows[0].mode == "changelog"
    assert "changelog_pk_unique" in rows[0].error
