"""Phase 3a.1 (docs/phase3-field-selection-design.md §2.1/§6).

`_field_stats_row` is pure - no network, no Postgres - so it's tested
directly here, zero-network. `refresh_field_definitions`/`compute_field_stats`/
`discover` need real Postgres (`@pytest.mark.postgres`, excluded from the
default run, same pattern as test_issue_dirty.py/test_sql_checks.py).
"""

import json
from collections.abc import Iterator
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from testcontainers.community.postgres import PostgresContainer

from openflowbi.fields import discovery
from openflowbi.jira.fields import Field
from openflowbi.ops.tables import metadata as ops_metadata

# --- _field_stats_row: pure, zero-network -----------------------------------


def test_field_stats_row_computes_fill_rate_and_ignores_empty_values():
    sampled = [
        {"story_points": 5, "project": {"key": "A"}},
        {"story_points": None, "project": {"key": "A"}},
        {"project": {"key": "B"}},  # key absent entirely - also not filled
    ]
    row = discovery._field_stats_row("inst-a", "story_points", sampled)
    assert row["sampled_issues"] == 3
    assert row["non_null_count"] == 1
    assert row["fill_rate"] == 0.3333
    assert row["distinct_count"] == 1
    # Only project A actually had a non-null value for this field.
    assert row["project_count"] == 1
    assert row["sample_values"] == [5]


def test_field_stats_row_treats_empty_string_and_empty_array_as_unfilled():
    sampled = [{"labels": []}, {"labels": ""}, {"labels": ["backend"]}]
    row = discovery._field_stats_row("inst-a", "labels", sampled)
    assert row["non_null_count"] == 1
    assert row["fill_rate"] == round(1 / 3, 4)


def test_field_stats_row_caps_sample_values_but_not_distinct_count():
    sampled = [{"f": i} for i in range(15)]  # 15 distinct non-null values
    row = discovery._field_stats_row("inst-a", "f", sampled)
    assert row["distinct_count"] == 15
    assert len(row["sample_values"]) == 10


def test_field_stats_row_zero_sampled_issues_is_zero_fill_rate_not_a_crash():
    row = discovery._field_stats_row("inst-a", "f", [])
    assert row["sampled_issues"] == 0
    assert row["fill_rate"] == 0.0


# --- refresh_field_definitions / compute_field_stats / discover: Postgres ---
# Applied per-function (not module-level `pytestmark`) since the pure tests
# above must stay in the default zero-network run.


@pytest.fixture
def postgres_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        dsn = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
        engine = sa.create_engine(dsn)
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE SCHEMA IF NOT EXISTS flowbi_ops"))
        ops_metadata.create_all(engine)
        with engine.begin() as conn:
            # Minimal jira_raw shape - just the column compute_field_stats
            # reads. dlt owns the real schema (P2-D1); hand-writing DDL here
            # is only for this throwaway test fixture.
            conn.execute(sa.text("CREATE SCHEMA IF NOT EXISTS jira_raw"))
            conn.execute(
                sa.text(
                    "CREATE TABLE jira_raw.issues "
                    "(instance_id text, issue_id bigint, fields jsonb)"
                )
            )
        engine.dispose()
        yield dsn


def _insert_issues(dsn: str, instance_id: str, rows: list[dict]) -> None:
    engine = sa.create_engine(dsn)
    with engine.begin() as conn:
        for i, fields in enumerate(rows):
            conn.execute(
                sa.text(
                    "INSERT INTO jira_raw.issues (instance_id, issue_id, fields) "
                    "VALUES (:i, :n, CAST(:f AS jsonb))"
                ),
                {"i": instance_id, "n": i, "f": json.dumps(fields)},
            )
    engine.dispose()


def _field_definition_rows(dsn: str, instance_id: str) -> dict[str, dict]:
    engine = sa.create_engine(dsn)
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT field_id, name, is_custom, schema_type, disappeared_at "
                "FROM flowbi_ops.field_definition WHERE instance_id = :i"
            ),
            {"i": instance_id},
        ).fetchall()
    engine.dispose()
    return {r.field_id: dict(r._mapping) for r in rows}


@pytest.mark.postgres
def test_refresh_field_definitions_upserts_and_tracks_disappearance(postgres_dsn):
    instance_id = "refresh-test"
    fetch_two = [
        Field(id="story_points", name="Story Points", schema_type="number", custom=True),
        Field(id="priority", name="Priority", schema_type="priority", custom=False),
    ]
    with patch("openflowbi.fields.discovery.jira_fields.fetch", return_value=fetch_two):
        seen, disappeared = discovery.refresh_field_definitions(
            postgres_dsn, instance_id, "https://fake.example", auth=None
        )
    assert seen == 2
    assert disappeared == 0
    rows = _field_definition_rows(postgres_dsn, instance_id)
    assert set(rows) == {"story_points", "priority"}
    assert rows["story_points"]["disappeared_at"] is None

    # priority stops appearing (e.g. field deleted/renamed in Jira).
    fetch_one = [fetch_two[0]]
    with patch("openflowbi.fields.discovery.jira_fields.fetch", return_value=fetch_one):
        seen, disappeared = discovery.refresh_field_definitions(
            postgres_dsn, instance_id, "https://fake.example", auth=None
        )
    assert seen == 1
    assert disappeared == 1
    rows = _field_definition_rows(postgres_dsn, instance_id)
    assert rows["priority"]["disappeared_at"] is not None
    assert rows["story_points"]["disappeared_at"] is None

    # priority reappears - disappeared_at clears.
    with patch("openflowbi.fields.discovery.jira_fields.fetch", return_value=fetch_two):
        discovery.refresh_field_definitions(
            postgres_dsn, instance_id, "https://fake.example", auth=None
        )
    rows = _field_definition_rows(postgres_dsn, instance_id)
    assert rows["priority"]["disappeared_at"] is None


@pytest.mark.postgres
def test_compute_field_stats_only_covers_known_fields_and_scopes_by_project(postgres_dsn):
    instance_id = "stats-test"
    known_field = Field(id="story_points", name="Story Points", schema_type="number", custom=True)
    with patch(
        "openflowbi.fields.discovery.jira_fields.fetch", return_value=[known_field]
    ):
        discovery.refresh_field_definitions(
            postgres_dsn, instance_id, "https://fake.example", auth=None
        )

    _insert_issues(
        postgres_dsn,
        instance_id,
        [
            {"story_points": 3, "project": {"key": "ALPHA"}},
            {"story_points": None, "project": {"key": "ALPHA"}},
            {"story_points": 8, "project": {"key": "BETA"}},
        ],
    )

    with_stats, sampled = discovery.compute_field_stats(postgres_dsn, instance_id)
    assert with_stats == 1  # only the one known field, not every jsonb key
    assert sampled == 3

    with_stats, sampled = discovery.compute_field_stats(
        postgres_dsn, instance_id, project="ALPHA"
    )
    assert sampled == 2


@pytest.mark.postgres
def test_discover_end_to_end_matches_acceptance_criterion(postgres_dsn):
    """The literal 3a.1 acceptance: after `discover`, field_stats carries real
    fill rates a caller (`flowbi fields list`) can read back."""
    instance_id = "discover-test"
    fetched = [Field(id="story_points", name="Story Points", schema_type="number", custom=True)]
    _insert_issues(
        postgres_dsn,
        instance_id,
        [{"story_points": 5}, {"story_points": None}, {"story_points": 8}],
    )

    with patch("openflowbi.fields.discovery.jira_fields.fetch", return_value=fetched):
        result = discovery.discover(postgres_dsn, instance_id, "https://fake.example", auth=None)

    assert result.fields_seen == 1
    assert result.sampled_issues == 3
    assert result.fields_with_stats == 1

    engine = sa.create_engine(postgres_dsn)
    with engine.connect() as conn:
        fill_rate = conn.execute(
            sa.text(
                "SELECT fill_rate FROM flowbi_ops.field_stats "
                "WHERE instance_id = :i AND field_id = 'story_points'"
            ),
            {"i": instance_id},
        ).scalar_one()
    engine.dispose()
    assert float(fill_rate) == round(2 / 3, 4)
