from datetime import UTC, datetime

import pyarrow as pa
import pytest
from pandera.errors import SchemaError

from openflowbi.quality import checks

_TIMESTAMP = pa.timestamp("us", tz="UTC")


def _ts(*args):
    return datetime(*args, tzinfo=UTC) if args else None


def _issues_table(**overrides):
    columns = {
        "instance_id": ["inst-a", "inst-a"],
        # bigint from M7.2 (docs/phase2-postgres-design.md §3), not string.
        "issue_id": pa.array([1, 2], type=pa.int64()),
        "issue_key": ["PROJ-1", "PROJ-2"],
        # timestamp, not string: dlt's normalizer casts flatten.issues()'s
        # passthrough ISO strings to a real timestamp column at load time -
        # confirmed against a live extract (see checks.py's comment).
        "created_at": pa.array([_ts(2024, 1, 1), _ts(2024, 1, 2)], type=_TIMESTAMP),
        "updated_at": pa.array([_ts(2024, 1, 1), _ts()], type=_TIMESTAMP),
    }
    columns.update(overrides)
    return pa.table(columns)


def _changelog_table(**overrides):
    columns = {
        "instance_id": ["inst-a", "inst-a"],
        "issue_id": pa.array([1, 1], type=pa.int64()),
        "history_id": ["h1", "h1"],
        "item_index": pa.array([0, 1], type=pa.int64()),
        "source": ["expand", "expand"],
        "changelog_complete": [True, True],
    }
    columns.update(overrides)
    return pa.table(columns)


def test_validate_issues_accepts_a_well_formed_table():
    validated = checks.validate_issues(_issues_table())
    assert validated.num_rows == 2


def test_validate_issues_rejects_duplicate_issue_id():
    # issue_id is the only identity — never duplicated
    # (within the same instance_id — P2-D2).
    table = _issues_table(issue_id=pa.array([1, 1], type=pa.int64()))
    with pytest.raises(SchemaError):
        checks.validate_issues(table)


def test_validate_issues_rejects_null_issue_id():
    table = _issues_table(issue_id=pa.array([None, 2], type=pa.int64()))
    with pytest.raises(SchemaError):
        checks.validate_issues(table)


def test_validate_issues_allows_same_issue_id_across_instances():
    # P2-D2: instance_id is part of the primary key, so the same numeric
    # issue_id from two different Jira instances is not a duplicate.
    table = _issues_table(
        instance_id=["inst-a", "inst-b"], issue_id=pa.array([1, 1], type=pa.int64())
    )
    validated = checks.validate_issues(table)
    assert validated.num_rows == 2


def test_validate_issues_allows_extra_passthrough_columns():
    # `fields` (JSON passthrough) and dlt's _dlt_* columns must not trip strict=True.
    table = _issues_table(fields=["{}", "{}"], _dlt_load_id=["1", "1"])
    validated = checks.validate_issues(table)
    assert "fields" in validated.column_names


def test_validate_changelog_accepts_a_well_formed_table():
    validated = checks.validate_changelog(_changelog_table())
    assert validated.num_rows == 2


def test_validate_changelog_rejects_unknown_source_tier():
    table = _changelog_table(source=["not_a_tier", "expand"])
    with pytest.raises(SchemaError):
        checks.validate_changelog(table)


def test_validate_changelog_rejects_negative_item_index():
    # item_index comes from a deterministic sort starting at 0 (flatten.changelog).
    table = _changelog_table(item_index=pa.array([-1, 0], type=pa.int64()))
    with pytest.raises(SchemaError):
        checks.validate_changelog(table)


def test_validate_changelog_rejects_duplicate_composite_key():
    # (issue_id, history_id, item_index) is the row identity, matching the
    # dlt resource's primary_key in pipeline/source.py.
    table = _changelog_table(item_index=pa.array([0, 0], type=pa.int64()))
    with pytest.raises(SchemaError):
        checks.validate_changelog(table)


def test_validate_changelog_accepts_updated_at_when_present():
    # M7.5: updated_at (the issue's own `fields.updated`, used as the
    # incremental cursor) is optional (required=False, older/partial extracts
    # predate it) but must still type-check when a table does carry it.
    table = _changelog_table(
        updated_at=pa.array([_ts(2024, 1, 2), _ts(2024, 1, 2)], type=_TIMESTAMP)
    )
    validated = checks.validate_changelog(table)
    assert "updated_at" in validated.column_names
