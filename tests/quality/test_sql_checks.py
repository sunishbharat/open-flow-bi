"""M7.6 acceptance proof (docs/phase2-postgres-design.md §9/§14): SQL
invariants that pandera-on-a-sample can't check ("is this unique across the
whole table") run against real Postgres and return non-zero for exactly the
kind of duplicate that Phase 1's pandera-on-Parquet check already caught, but
that the filesystem destination's merge->append downgrade let accumulate.

Needs Docker; excluded from the default `pytest` run (see the `postgres`
marker in pyproject.toml), same as test_concurrency.py/test_issue_dirty.py.
Run explicitly: `uv run pytest -m postgres tests/quality/test_sql_checks.py`.
"""

from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from testcontainers.community.postgres import PostgresContainer

from openflowbi.quality import sql_checks

pytestmark = pytest.mark.postgres


@pytest.fixture(scope="module")
def postgres_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        dsn = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
        engine = sa.create_engine(dsn)
        with engine.begin() as conn:
            # Minimal jira_raw shape — just the columns the SQL checks
            # touch. Real columns come from dlt (§3); hand-writing DDL here
            # only for a throwaway test fixture is not the "never hand-write
            # DDL for jira_raw" rule (§3) — that rule is about the real
            # pipeline, dlt still owns the schema there.
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
        engine.dispose()
        yield dsn


@pytest.fixture
def clean_tables(postgres_dsn: str) -> Iterator[None]:
    engine = sa.create_engine(postgres_dsn)
    with engine.begin() as conn:
        conn.execute(sa.text("TRUNCATE jira_raw.issues, jira_raw.issue_changelog"))
    engine.dispose()
    yield
    engine = sa.create_engine(postgres_dsn)
    with engine.begin() as conn:
        conn.execute(sa.text("TRUNCATE jira_raw.issues, jira_raw.issue_changelog"))
    engine.dispose()


def _insert_issues(dsn: str, rows: list[tuple[str, int]]) -> None:
    engine = sa.create_engine(dsn)
    with engine.begin() as conn:
        for instance_id, issue_id in rows:
            conn.execute(
                sa.text("INSERT INTO jira_raw.issues (instance_id, issue_id) VALUES (:i, :n)"),
                {"i": instance_id, "n": issue_id},
            )
    engine.dispose()


def _insert_changelog(dsn: str, rows: list[tuple[str, int, str, int]]) -> None:
    engine = sa.create_engine(dsn)
    with engine.begin() as conn:
        for instance_id, issue_id, history_id, item_index in rows:
            conn.execute(
                sa.text(
                    "INSERT INTO jira_raw.issue_changelog "
                    "(instance_id, issue_id, history_id, item_index) "
                    "VALUES (:i, :n, :h, :x)"
                ),
                {"i": instance_id, "n": issue_id, "h": history_id, "x": item_index},
            )
    engine.dispose()


def test_issues_pk_unique_passes_on_clean_data(postgres_dsn: str, clean_tables: None) -> None:
    _insert_issues(postgres_dsn, [("inst-a", 1), ("inst-a", 2)])
    result = sql_checks.run_checks(postgres_dsn, "issues")
    assert result.passed
    assert result.blocking["issues_pk_unique"] == 0


def test_issues_pk_unique_fails_on_seeded_duplicate(postgres_dsn: str, clean_tables: None) -> None:
    # issue_id is the only identity — never duplicated
    # within an instance. Postgres is where `merge` actually enforces this
    # (§1) — this seeds the exact failure a filesystem destination silently
    # allows to accumulate.
    _insert_issues(postgres_dsn, [("inst-a", 1), ("inst-a", 1)])
    result = sql_checks.run_checks(postgres_dsn, "issues")
    assert not result.passed
    assert result.blocking["issues_pk_unique"] == 1
    assert result.failing == ["issues_pk_unique"]


def test_changelog_pk_unique_fails_on_seeded_duplicate(
    postgres_dsn: str, clean_tables: None
) -> None:
    _insert_issues(postgres_dsn, [("inst-a", 1)])
    _insert_changelog(
        postgres_dsn, [("inst-a", 1, "h1", 0), ("inst-a", 1, "h1", 0)]
    )
    result = sql_checks.run_checks(postgres_dsn, "issue_changelog")
    assert not result.passed
    assert result.blocking["changelog_pk_unique"] == 1


def test_no_orphan_changelog_flags_a_changelog_row_with_no_matching_issue(
    postgres_dsn: str, clean_tables: None
) -> None:
    # §9's documented scoping decision: whole-table, so a changelog row with
    # no matching `issues` row (e.g. `extract changelog` run before `extract
    # issues`) is correctly flagged, not silently ignored.
    _insert_changelog(postgres_dsn, [("inst-a", 99, "h1", 0)])
    result = sql_checks.run_checks(postgres_dsn, "issue_changelog")
    assert not result.passed
    assert result.blocking["no_orphan_changelog"] == 1


def test_alerting_checks_never_fail_the_gate(postgres_dsn: str, clean_tables: None) -> None:
    # changelog_complete lives on issue_changelog rows (flatten.changelog()),
    # never on issues — see sql_checks.py's ALERTING comment for how this was
    # confirmed against a live run.
    _insert_issues(postgres_dsn, [("inst-a", 1)])
    engine = sa.create_engine(postgres_dsn)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO jira_raw.issue_changelog "
                "(instance_id, issue_id, history_id, item_index, changelog_complete) "
                "VALUES ('inst-a', 1, 'h1', 0, false)"
            )
        )
    engine.dispose()

    result = sql_checks.run_checks(postgres_dsn, "issue_changelog")
    assert result.alerting["changelog_incomplete"] == 1
    assert result.passed  # alerting-only, does not affect the blocking verdict


def test_alerting_is_empty_for_issues_table(postgres_dsn: str, clean_tables: None) -> None:
    # No ALERTING check is registered for "issues" (see ALERTING_BY_TABLE) -
    # this must not touch jira_raw.issue_changelog at all, so a checkout that
    # has only ever run `extract issues` (table doesn't exist yet) still
    # works.
    _insert_issues(postgres_dsn, [("inst-a", 1)])
    result = sql_checks.run_checks(postgres_dsn, "issues")
    assert result.alerting == {}
