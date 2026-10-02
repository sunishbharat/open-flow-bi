"""Pure, zero-network - no Postgres needed (contrast with test_selection.py,
which is `pytest.mark.postgres` throughout). transform/runner.py interpolates
column_name/field_id directly into DDL text; these are the guards that keep
an unsafe value from ever reaching that code (fields/selection.py's
save_selection() calls validate_identifier at promotion time; runner.py
calls validate_field_id on field_id defensively before using it).
"""

import pytest

from openflowbi.fields import selection
from openflowbi.fields.selection import validate_field_id, validate_identifier


@pytest.mark.parametrize("name", ["story_points", "a", "business_unit_2", "x" * 63])
def test_accepts_safe_identifiers(name: str) -> None:
    validate_identifier(name)  # must not raise


@pytest.mark.parametrize(
    "name",
    [
        "",
        "Story Points",  # spaces
        "1story",  # must start with a letter
        "story-points",  # hyphen
        "story;drop table analytics.issue;--",  # injection attempt
        "x" * 64,  # over Postgres' 63-byte identifier limit
        "fixVersions",  # real Jira field id - camelCase, not a valid column_name
    ],
)
def test_rejects_unsafe_identifiers(name: str) -> None:
    with pytest.raises(ValueError, match="not a valid identifier"):
        validate_identifier(name)


@pytest.mark.parametrize(
    "field_id",
    [
        "fixVersions",  # the real bug: "Fix Version/s"'s actual Jira field id
        "customfield_10016",
        "duedate",
        "lastViewed",
        "a",
        "x" * 63,
    ],
)
def test_validate_field_id_accepts_real_jira_field_ids(field_id: str) -> None:
    validate_field_id(field_id)  # must not raise, including camelCase


@pytest.mark.parametrize(
    "field_id",
    [
        "",
        "1field",  # must start with a letter
        "field-id",  # hyphen
        "field;drop table jira_raw.issues;--",  # injection attempt
        "x" * 64,
    ],
)
def test_validate_field_id_rejects_unsafe_values(field_id: str) -> None:
    with pytest.raises(ValueError, match="not a valid identifier"):
        validate_field_id(field_id)


# A syntactically fine name can still be one
# the runner's idempotent DDL silently reuses. Rejected in save_selection's
# validation, before any database access, so no Postgres is needed here.
@pytest.mark.parametrize(
    ("column_name", "target"),
    [
        ("issue_key", "column"),
        ("issue_id", "column"),
        ("created_at", "column"),
        ("ctid", "column"),
        ("issue", "bridge_table"),
        ("issue_status_interval", "bridge_table"),
    ],
)
def test_save_selection_rejects_reserved_names(column_name: str, target: str) -> None:
    change = selection.SelectionChange(
        field_name="Anything",
        schema_type="array" if target == "bridge_table" else "string",
        promote=True,
        column_name=column_name,
        target=target,
    )
    with pytest.raises(ValueError, match="existing"):
        selection.save_selection("postgresql://unused", "inst", [change], actor="test")


def test_reserved_names_are_per_target() -> None:
    # `issue` is only reserved as a table; as an extra analytics.issue
    # column it's merely odd, not dangerous.
    selection.validate_promoted_name("issue", "column")
    selection.validate_promoted_name("issue_key", "bridge_table")
