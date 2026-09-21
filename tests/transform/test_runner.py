"""Phase 3a.3-3a.6 (docs/phase3-field-selection-design.md §4-§7). Needs real
Postgres - excluded from the default run, same pattern as
tests/fields/test_selection.py and the rest of the `postgres`-marked suite.
"""

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from testcontainers.community.postgres import PostgresContainer

from openflowbi.fields import selection
from openflowbi.fields import service as fields_service
from openflowbi.ops.analytics_tables import metadata as analytics_metadata
from openflowbi.ops.tables import issue_dirty
from openflowbi.ops.tables import metadata as ops_metadata
from openflowbi.transform import runner

pytestmark = pytest.mark.postgres

INSTANCE_ID = "transform-test"
T0 = datetime(2026, 1, 1, tzinfo=UTC)
T1 = T0 + timedelta(hours=1)
T2 = T0 + timedelta(hours=3)


@pytest.fixture
def postgres_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        dsn = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
        engine = sa.create_engine(dsn)
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE SCHEMA IF NOT EXISTS flowbi_ops"))
            conn.execute(sa.text("CREATE SCHEMA IF NOT EXISTS analytics"))
            conn.execute(sa.text("CREATE SCHEMA IF NOT EXISTS jira_raw"))
            # Minimal jira_raw shape, same rationale as
            # tests/fields/test_discovery.py's fixture: dlt owns the real
            # schema (P2-D1), this is only for this throwaway fixture.
            conn.execute(
                sa.text(
                    "CREATE TABLE jira_raw.issues (instance_id text, issue_id bigint, "
                    "issue_key text, created_at timestamptz, updated_at timestamptz, "
                    "fields jsonb)"
                )
            )
            conn.execute(
                sa.text(
                    "CREATE TABLE jira_raw.issue_changelog (instance_id text, issue_id bigint, "
                    "history_id text, item_index int, source text, changelog_complete boolean, "
                    "updated_at timestamptz, author text, created_at timestamptz, field text, "
                    "field_id text, from_id text, from_value text, to_id text, to_value text)"
                )
            )
        ops_metadata.create_all(engine)
        analytics_metadata.create_all(engine)
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "CREATE TABLE analytics.issue (instance_id text, issue_id bigint, "
                    "issue_key text, created_at timestamptz, updated_at timestamptz, "
                    "rebuilt_at timestamptz NOT NULL DEFAULT now(), "
                    "PRIMARY KEY (instance_id, issue_id))"
                )
            )
        engine.dispose()
        yield dsn


def _insert_issue(
    dsn: str, issue_id: int, created_at: datetime, fields: dict, project: str = "TEST"
) -> None:
    full_fields = {"project": {"key": project}, **fields}
    engine = sa.create_engine(dsn)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO jira_raw.issues (instance_id, issue_id, issue_key, created_at, "
                "updated_at, fields) VALUES (:i, :n, :k, :c, :c, CAST(:f AS jsonb))"
            ),
            {
                "i": INSTANCE_ID,
                "n": issue_id,
                "k": f"{project}-{issue_id}",
                "c": created_at,
                "f": json.dumps(full_fields),
            },
        )
    engine.dispose()


def _insert_status_change(
    dsn: str,
    issue_id: int,
    history_id: str,
    changed_at: datetime,
    from_id: str,
    from_value: str,
    to_id: str,
    to_value: str,
) -> None:
    engine = sa.create_engine(dsn)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO jira_raw.issue_changelog (instance_id, issue_id, history_id, "
                "item_index, source, changelog_complete, created_at, field, from_id, "
                "from_value, to_id, to_value) VALUES (:i, :n, :h, 0, 'expand', true, :c, "
                "'status', :fi, :fv, :ti, :tv)"
            ),
            {
                "i": INSTANCE_ID,
                "n": issue_id,
                "h": history_id,
                "c": changed_at,
                "fi": from_id,
                "fv": from_value,
                "ti": to_id,
                "tv": to_value,
            },
        )
    engine.dispose()


def _interval_rows(dsn: str, issue_id: int) -> list[tuple]:
    engine = sa.create_engine(dsn)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT seq, status_id, status_name, entered_at, exited_at, duration_seconds "
                    "FROM analytics.issue_status_interval "
                    "WHERE instance_id = :i AND issue_id = :n ORDER BY seq"
                ),
                {"i": INSTANCE_ID, "n": issue_id},
            ).fetchall()
        return [tuple(r) for r in rows]
    finally:
        engine.dispose()


# --- 3a.3: seq=0 seeding + a hand-computed cycle time ------------------------


def test_status_interval_seeds_seq_zero_from_created_at_and_matches_hand_computed(
    postgres_dsn: str,
) -> None:
    _insert_issue(postgres_dsn, 1, T0, {"status": {"id": "3", "name": "Done"}})
    _insert_status_change(postgres_dsn, 1, "h1", T1, "1", "Open", "2", "In Progress")
    _insert_status_change(postgres_dsn, 1, "h2", T2, "2", "In Progress", "3", "Done")

    engine = sa.create_engine(postgres_dsn)
    with engine.begin() as conn:
        runner.rebuild_intervals(conn, INSTANCE_ID, None)
    engine.dispose()

    rows = _interval_rows(postgres_dsn, 1)
    assert rows == [
        (0, "1", "Open", T0, T1, 3600),
        (1, "2", "In Progress", T1, T2, 7200),
        (2, "3", "Done", T2, None, None),
    ]
    # The literal 3a.3 acceptance line: min(entered_at) = created_at.
    assert rows[0][3] == T0


def test_status_interval_seeds_from_current_status_when_no_changelog_exists(
    postgres_dsn: str,
) -> None:
    # An issue that has never transitioned (or whose changelog hasn't been
    # extracted yet) still gets exactly one open-ended interval, from its
    # current fields->status, not zero rows.
    _insert_issue(postgres_dsn, 2, T0, {"status": {"id": "1", "name": "Open"}})

    engine = sa.create_engine(postgres_dsn)
    with engine.begin() as conn:
        runner.rebuild_intervals(conn, INSTANCE_ID, None)
    engine.dispose()

    assert _interval_rows(postgres_dsn, 2) == [(0, "1", "Open", T0, None, None)]


# --- 3a.4: promoted columns + bridge tables ----------------------------------


def test_promoted_column_and_bridge_table_are_populated(postgres_dsn: str) -> None:
    _insert_issue(
        postgres_dsn,
        3,
        T0,
        {
            "status": {"id": "1", "name": "Open"},
            "customfield_10016": 5,
            "labels": ["backend", "urgent"],
        },
    )
    selection.save_selection(
        postgres_dsn,
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
                column_name="labels",
                target="bridge_table",
            ),
        ],
        actor="test",
    )

    result = runner.run_transform(postgres_dsn, INSTANCE_ID, rebuild_all=True)
    assert result.issue_rows >= 1

    engine = sa.create_engine(postgres_dsn)
    with engine.connect() as conn:
        story_points = conn.execute(
            sa.text(
                "SELECT story_points FROM analytics.issue WHERE instance_id = :i AND issue_id = 3"
            ),
            {"i": INSTANCE_ID},
        ).scalar_one()
        labels = conn.execute(
            sa.text(
                "SELECT value FROM analytics.labels WHERE instance_id = :i AND issue_id = 3 "
                "ORDER BY seq"
            ),
            {"i": INSTANCE_ID},
        ).fetchall()
    engine.dispose()

    assert story_points == 5
    assert [row[0] for row in labels] == ["backend", "urgent"]


def test_camel_case_jira_field_id_does_not_crash_transform(postgres_dsn: str) -> None:
    # Regression: "Fix Version/s"'s real Jira field id is "fixVersions"
    # (camelCase) - found live crashing `flowbi transform --rebuild-all`
    # entirely with a ValueError, because validate_identifier (meant for
    # operator-chosen column_name) was also being applied to field_id, which
    # Jira does not guarantee is lowercase.
    _insert_issue(
        postgres_dsn,
        5,
        T0,
        {
            "status": {"id": "1", "name": "Open"},
            "fixVersions": [{"name": "3.6.0"}, {"name": "3.7.0"}],
        },
    )
    selection.save_selection(
        postgres_dsn,
        INSTANCE_ID,
        [
            selection.SelectionChange(
                field_name="Fix Version/s",
                schema_type="array",
                field_id="fixVersions",
                promote=True,
                column_name="fix_version",
                target="bridge_table",
            ),
        ],
        actor="test",
    )

    result = runner.run_transform(postgres_dsn, INSTANCE_ID, rebuild_all=True)
    assert result.bridge_rows >= 2

    engine = sa.create_engine(postgres_dsn)
    with engine.connect() as conn:
        values = conn.execute(
            sa.text(
                "SELECT value FROM analytics.fix_version WHERE instance_id = :i AND issue_id = 5 "
                "ORDER BY seq"
            ),
            {"i": INSTANCE_ID},
        ).fetchall()
    engine.dispose()
    assert [row[0] for row in values] == ["3.6.0", "3.7.0"]


# --- 3a.5: rebuild_request queue ----------------------------------------------


def test_rebuild_request_is_drained_and_marked_succeeded(postgres_dsn: str) -> None:
    _insert_issue(
        postgres_dsn,
        4,
        T0,
        {"status": {"id": "1", "name": "Open"}, "priority": {"name": "High"}},
    )
    selection.save_selection(
        postgres_dsn,
        INSTANCE_ID,
        [
            selection.SelectionChange(
                field_name="Priority",
                schema_type="priority",
                field_id="priority",
                promote=True,
                column_name="priority_name",
                target="column",
            )
        ],
        actor="test",
    )
    request_id = fields_service.request_rebuild(postgres_dsn, INSTANCE_ID, scope=None, actor="test")

    status_before = fields_service.rebuild_status(postgres_dsn, request_id)
    assert status_before.status == "queued"

    runner.run_transform(postgres_dsn, INSTANCE_ID, rebuild_all=False)  # drains as a side effect

    status_after = fields_service.rebuild_status(postgres_dsn, request_id)
    assert status_after.status == "succeeded"
    assert status_after.rows_written is not None and status_after.rows_written >= 1

    engine = sa.create_engine(postgres_dsn)
    with engine.connect() as conn:
        priority_name = conn.execute(
            sa.text(
                "SELECT priority_name FROM analytics.issue WHERE instance_id = :i AND issue_id = 4"
            ),
            {"i": INSTANCE_ID},
        ).scalar_one()
    engine.dispose()
    assert priority_name == "High"


# --- 3a.6: incremental proof --------------------------------------------------


def test_incremental_transform_touches_only_the_dirty_issue(postgres_dsn: str) -> None:
    _insert_issue(postgres_dsn, 10, T0, {"status": {"id": "1", "name": "Open"}})
    _insert_issue(postgres_dsn, 11, T0, {"status": {"id": "1", "name": "Open"}})
    runner.run_transform(postgres_dsn, INSTANCE_ID, rebuild_all=True)

    rows_10_before = _interval_rows(postgres_dsn, 10)
    rows_11_before = _interval_rows(postgres_dsn, 11)
    assert rows_10_before == [(0, "1", "Open", T0, None, None)]

    # Issue 10 transitions; issue 11 is untouched. Only issue 10 is marked dirty.
    _insert_status_change(postgres_dsn, 10, "h1", T1, "1", "Open", "2", "Done")
    engine = sa.create_engine(postgres_dsn)
    with engine.begin() as conn:
        conn.execute(
            sa.insert(issue_dirty).values(
                instance_id=INSTANCE_ID, issue_id=10, reason="test_changelog_extracted"
            )
        )
    engine.dispose()

    result = runner.run_transform(postgres_dsn, INSTANCE_ID)  # incremental, default
    assert result.claimed == 1

    rows_10_after = _interval_rows(postgres_dsn, 10)
    rows_11_after = _interval_rows(postgres_dsn, 11)
    assert rows_10_after == [
        (0, "1", "Open", T0, T1, 3600),
        (1, "2", "Done", T1, None, None),
    ]
    assert rows_11_after == rows_11_before  # untouched — the literal 3a.6 line
