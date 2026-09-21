"""Phase 3a.2 (docs/phase3-field-selection-design.md §2.1/§3/§5): versioned
field_selection, promote/demote, export/import YAML. Needs real Postgres -
excluded from the default run, same pattern as the rest of the
`postgres`-marked suite.
"""

from collections.abc import Iterator

import pytest
import sqlalchemy as sa
import yaml
from testcontainers.community.postgres import PostgresContainer

from openflowbi.fields import selection
from openflowbi.ops.tables import field_definition
from openflowbi.ops.tables import metadata as ops_metadata

pytestmark = pytest.mark.postgres

INSTANCE_ID = "selection-test"


@pytest.fixture
def postgres_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        dsn = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
        engine = sa.create_engine(dsn)
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE SCHEMA IF NOT EXISTS flowbi_ops"))
        ops_metadata.create_all(engine)
        with engine.begin() as conn:
            conn.execute(
                sa.insert(field_definition).values(
                    [
                        {
                            "instance_id": INSTANCE_ID,
                            "field_id": "customfield_10016",
                            "name": "Story Points",
                            "is_custom": True,
                            "schema_type": "number",
                            "disappeared_at": None,
                        },
                        {
                            "instance_id": INSTANCE_ID,
                            "field_id": "duedate",
                            "name": "Ambiguous",
                            "is_custom": False,
                            "schema_type": "date",
                            "disappeared_at": None,
                        },
                        {
                            "instance_id": INSTANCE_ID,
                            "field_id": "customfield_99999",
                            "name": "Ambiguous",
                            "is_custom": True,
                            "schema_type": "string",
                            "disappeared_at": None,
                        },
                    ]
                )
            )
        engine.dispose()
        yield dsn


def test_resolve_field_name_unambiguous(postgres_dsn):
    schema_type, field_id = selection.resolve_field_name(postgres_dsn, INSTANCE_ID, "Story Points")
    assert schema_type == "number"
    assert field_id == "customfield_10016"


def test_resolve_field_name_unknown_raises(postgres_dsn):
    with pytest.raises(ValueError, match="no field named"):
        selection.resolve_field_name(postgres_dsn, INSTANCE_ID, "Nope")


def test_resolve_field_name_ambiguous_without_schema_type_raises(postgres_dsn):
    with pytest.raises(ValueError, match="ambiguous"):
        selection.resolve_field_name(postgres_dsn, INSTANCE_ID, "Ambiguous")


def test_resolve_field_name_ambiguous_disambiguated_by_schema_type(postgres_dsn):
    schema_type, field_id = selection.resolve_field_name(
        postgres_dsn, INSTANCE_ID, "Ambiguous", schema_type="date"
    )
    assert schema_type == "date"
    assert field_id == "duedate"


def test_save_selection_promote_then_demote_is_append_only(postgres_dsn):
    v1 = selection.save_selection(
        postgres_dsn,
        INSTANCE_ID,
        [
            selection.SelectionChange(
                field_name="Story Points",
                schema_type="number",
                promote=True,
                column_name="story_points",
            )
        ],
        actor="test",
    )
    assert v1 == 1
    _, sel = selection.current_selection(postgres_dsn, INSTANCE_ID)
    assert sel[("Story Points", "number")].deprecated_at is None
    assert sel[("Story Points", "number")].column_name == "story_points"

    v2 = selection.save_selection(
        postgres_dsn,
        INSTANCE_ID,
        [selection.SelectionChange(field_name="Story Points", schema_type="number", promote=False)],
        actor="test",
    )
    assert v2 == 2
    # v1 is untouched - append-only, never updated in place.
    _, sel_v1 = selection.current_selection(postgres_dsn, INSTANCE_ID, version=1)
    assert sel_v1[("Story Points", "number")].deprecated_at is None
    _, sel_v2 = selection.current_selection(postgres_dsn, INSTANCE_ID, version=2)
    assert sel_v2[("Story Points", "number")].deprecated_at is not None
    # The column_name survives demotion (design doc §10 rule 1: never drop it).
    assert sel_v2[("Story Points", "number")].column_name == "story_points"


def test_save_selection_demote_never_selected_raises(postgres_dsn):
    change = selection.SelectionChange(
        field_name="Story Points", schema_type="number", promote=False
    )
    with pytest.raises(ValueError, match="not currently selected"):
        selection.save_selection(postgres_dsn, INSTANCE_ID, [change], actor="test")


def test_save_selection_carries_forward_untouched_fields(postgres_dsn):
    selection.save_selection(
        postgres_dsn,
        INSTANCE_ID,
        [
            selection.SelectionChange(
                field_name="Story Points",
                schema_type="number",
                promote=True,
                column_name="story_points",
            )
        ],
        actor="test",
    )
    # A second, unrelated save must not lose the first field from "current".
    selection.save_selection(
        postgres_dsn,
        INSTANCE_ID,
        [
            selection.SelectionChange(
                field_name="Ambiguous",
                schema_type="date",
                promote=True,
                column_name="due_ambiguous",
            )
        ],
        actor="test",
    )
    _, sel = selection.current_selection(postgres_dsn, INSTANCE_ID)
    assert set(sel) == {("Story Points", "number"), ("Ambiguous", "date")}


def test_save_selection_rejects_column_name_collision(postgres_dsn):
    selection.save_selection(
        postgres_dsn,
        INSTANCE_ID,
        [
            selection.SelectionChange(
                field_name="Story Points", schema_type="number", promote=True, column_name="team"
            )
        ],
        actor="test",
    )
    with pytest.raises(ValueError, match="claimed by both"):
        selection.save_selection(
            postgres_dsn,
            INSTANCE_ID,
            [
                selection.SelectionChange(
                    field_name="Ambiguous", schema_type="date", promote=True, column_name="team"
                )
            ],
            actor="test",
        )


def test_export_then_import_round_trips_content(postgres_dsn):
    """The literal 3a.2 acceptance: promote a field, export YAML, re-import —
    same version content."""
    selection.save_selection(
        postgres_dsn,
        INSTANCE_ID,
        [
            selection.SelectionChange(
                field_name="Story Points",
                schema_type="number",
                promote=True,
                column_name="story_points",
                note="for velocity charts",
            )
        ],
        actor="test",
    )
    exported = selection.export_yaml(postgres_dsn, INSTANCE_ID)
    imported_version = selection.import_yaml(postgres_dsn, INSTANCE_ID, exported, actor="test")
    assert imported_version == 2

    doc_v1 = yaml.safe_load(exported)
    reexported = selection.export_yaml(postgres_dsn, INSTANCE_ID, version=imported_version)
    doc_v2 = yaml.safe_load(reexported)
    # Content identical - only the version number differs.
    assert doc_v1["fields"] == doc_v2["fields"]


def test_import_yaml_with_no_fields_raises(postgres_dsn):
    empty_doc = "instance_id: x\nfields: []\n"
    with pytest.raises(ValueError, match="no fields"):
        selection.import_yaml(postgres_dsn, INSTANCE_ID, empty_doc, actor="test")
