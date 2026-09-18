from openflowbi.jira import flatten
from openflowbi.jira.fields import Field


def test_fields_flattens_field_objects():
    raw = [
        Field(id="customfield_10001", name="Story Points", schema_type="number", custom=True),
        Field(id="summary", name="Summary", schema_type="string", custom=False),
    ]
    rows = list(flatten.fields(raw))
    assert rows == [
        {
            "field_id": "customfield_10001",
            "name": "Story Points",
            "schema_type": "number",
            "custom": True,
        },
        {"field_id": "summary", "name": "Summary", "schema_type": "string", "custom": False},
    ]


def test_fields_empty_yields_nothing():
    assert list(flatten.fields([])) == []


def test_issues_flattens_normal_case():
    raw = [
        {
            "id": "10001",
            "key": "PROJ-1",
            "fields": {
                "summary": "Do the thing",
                "created": "2024-01-01T00:00:00.000+0000",
                "updated": "2024-01-02T00:00:00.000+0000",
            },
        }
    ]
    rows = list(flatten.issues(raw))
    assert rows == [
        {
            "issue_id": "10001",
            "issue_key": "PROJ-1",
            "created_at": "2024-01-01T00:00:00.000+0000",
            "updated_at": "2024-01-02T00:00:00.000+0000",
            "fields": raw[0]["fields"],
        }
    ]


def test_issues_handles_empty_fields():
    raw = [{"id": "10002", "key": "PROJ-2", "fields": {}}]
    rows = list(flatten.issues(raw))
    assert rows[0]["created_at"] is None
    assert rows[0]["updated_at"] is None
    assert rows[0]["fields"] == {}


def test_issues_handles_missing_fields_key():
    raw = [{"id": "10003", "key": "PROJ-3"}]
    rows = list(flatten.issues(raw))
    assert rows[0]["fields"] == {}


def test_issues_never_uses_key_as_identity():
    raw = [{"id": "10004", "key": "PROJ-4", "fields": {}}]
    row = next(flatten.issues(raw))
    assert row["issue_id"] == "10004"
    # issue_key is carried but must never be relied on as an identifier.
    assert row["issue_key"] == "PROJ-4"


def test_issues_empty_page_yields_nothing():
    assert list(flatten.issues([])) == []


def test_changelog_item_index_is_deterministic_regardless_of_input_order():
    history = {
        "id": "h1",
        "created": "2024-01-01T00:00:00.000+0000",
        "author": {"name": "alice"},
        "items": [
            {
                "field": "status",
                "fieldId": "status",
                "from": "1",
                "fromString": "Open",
                "to": "3",
                "toString": "Done",
            },
            {
                "field": "assignee",
                "fieldId": "assignee",
                "from": None,
                "fromString": None,
                "to": "bob",
                "toString": "Bob",
            },
        ],
    }
    reordered = {**history, "items": list(reversed(history["items"]))}

    rows_a = list(flatten.changelog([("1", "expand", True, [history])]))
    rows_b = list(flatten.changelog([("1", "expand", True, [reordered])]))

    assert [r["field"] for r in rows_a] == [r["field"] for r in rows_b] == ["assignee", "status"]
    assert [r["item_index"] for r in rows_a] == [0, 1]


def test_changelog_carries_source_and_completeness():
    history = {
        "id": "h1",
        "created": "2024-01-01T00:00:00.000+0000",
        "items": [{"field": "status"}],
    }
    rows = list(flatten.changelog([("1", "bulkfetch", True, [history])]))
    assert rows[0]["source"] == "bulkfetch"
    assert rows[0]["changelog_complete"] is True


def test_changelog_handles_null_field_id_for_system_fields():
    history = {
        "id": "h1",
        "created": "2024-01-01T00:00:00.000+0000",
        "items": [{"field": "status", "fieldId": None, "to": "3", "toString": "Done"}],
    }
    rows = list(flatten.changelog([("1", "expand", True, [history])]))
    assert rows[0]["field_id"] is None
    assert rows[0]["field"] == "status"


def test_changelog_empty_batches_yield_nothing():
    assert list(flatten.changelog([])) == []
    assert list(flatten.changelog([("1", "per_issue", False, [])])) == []
