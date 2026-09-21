"""Phase 3a.1: service.list_fields() is the join+filter the CLI's `fields list`
command (and, later, a 3b UI screen) both call (docs/phase3-field-selection-design.md
§3: "write this once, call it twice"). Needs real Postgres - excluded from
the default run, same pattern as the rest of the `postgres`-marked suite.
"""

from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from testcontainers.community.postgres import PostgresContainer

from openflowbi.fields import service
from openflowbi.ops.tables import field_definition, field_stats
from openflowbi.ops.tables import metadata as ops_metadata

pytestmark = pytest.mark.postgres

INSTANCE_ID = "service-test"


@pytest.fixture
def postgres_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        dsn = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
        engine = sa.create_engine(dsn)
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE SCHEMA IF NOT EXISTS flowbi_ops"))
        ops_metadata.create_all(engine)
        with engine.begin() as conn:
            # Every dict below must share the exact same keys: SQLAlchemy
            # Core's multi-row .values([...]) compiles one INSERT from a
            # single column set (taken from the dicts as a whole) and
            # silently drops a column that's only present on some rows,
            # rather than erroring or defaulting it per-row — confirmed the
            # hard way (disappeared_at came back NULL for every row) before
            # this comment existed.
            conn.execute(
                sa.insert(field_definition).values(
                    [
                        {
                            "instance_id": INSTANCE_ID,
                            "field_id": "story_points",
                            "name": "Story Points",
                            "is_custom": True,
                            "schema_type": "number",
                            "disappeared_at": None,
                        },
                        {
                            "instance_id": INSTANCE_ID,
                            "field_id": "priority",
                            "name": "Priority",
                            "is_custom": False,
                            "schema_type": "priority",
                            "disappeared_at": None,
                        },
                        {
                            "instance_id": INSTANCE_ID,
                            "field_id": "legacy_code",
                            "name": "Legacy Approval Code",
                            "is_custom": True,
                            "schema_type": "string",
                            "disappeared_at": sa.func.now(),
                        },
                        {
                            "instance_id": INSTANCE_ID,
                            "field_id": "unmeasured",
                            "name": "Never Discovered",
                            "is_custom": False,
                            "schema_type": "string",
                            "disappeared_at": None,
                        },
                    ]
                )
            )
            conn.execute(
                sa.insert(field_stats).values(
                    [
                        {
                            "instance_id": INSTANCE_ID,
                            "field_id": "story_points",
                            "sampled_issues": 100,
                            "non_null_count": 90,
                            "fill_rate": 0.9,
                            "distinct_count": 20,
                            "project_count": 3,
                        },
                        {
                            "instance_id": INSTANCE_ID,
                            "field_id": "priority",
                            "sampled_issues": 100,
                            "non_null_count": 100,
                            "fill_rate": 1.0,
                            "distinct_count": 3,
                            "project_count": 3,
                        },
                    ]
                )
            )
        engine.dispose()
        yield dsn


def test_list_fields_sorts_by_fill_rate_descending_and_excludes_disappeared(postgres_dsn):
    rows = service.list_fields(postgres_dsn, INSTANCE_ID)
    ids = [r.field_id for r in rows]
    assert "legacy_code" not in ids  # disappeared - excluded by default
    assert ids.index("priority") < ids.index("story_points")  # 1.0 before 0.9
    assert ids.index("story_points") < ids.index("unmeasured")  # has stats before none


def test_list_fields_custom_only(postgres_dsn):
    rows = service.list_fields(postgres_dsn, INSTANCE_ID, custom_only=True)
    assert {r.field_id for r in rows} == {"story_points"}


def test_list_fields_min_fill_excludes_fields_with_no_stats_row(postgres_dsn):
    # A field with no field_stats row (never sampled) has fill_rate NULL,
    # which a `>= min_fill` comparison must exclude, not error on.
    rows = service.list_fields(postgres_dsn, INSTANCE_ID, min_fill=0.5)
    assert {r.field_id for r in rows} == {"priority", "story_points"}


def test_list_fields_include_disappeared(postgres_dsn):
    rows = service.list_fields(postgres_dsn, INSTANCE_ID, include_disappeared=True)
    assert "legacy_code" in {r.field_id for r in rows}
