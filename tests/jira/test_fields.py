import pytest

from openflowbi.jira import fields

APACHE_JIRA = "https://issues.apache.org/jira"

# No Cloud tenant is available yet; hand-authored cassette, see test_deployment.py.
CLOUD_STUB = "https://example.atlassian.net"


@pytest.mark.vcr
def test_fetch_dc():
    result = fields.fetch(APACHE_JIRA)
    assert result
    assert all(isinstance(f, fields.Field) for f in result)
    assert any(f.name == "Resolution" for f in result)


@pytest.mark.vcr
def test_fetch_cloud():
    result = fields.fetch(CLOUD_STUB)
    assert result
    assert any(f.custom for f in result)


def test_field_map_looks_up_by_name_and_schema_type():
    field_map = fields.FieldMap(
        [
            fields.Field(
                id="customfield_10001", name="Story Points", schema_type="number", custom=True
            ),
            fields.Field(id="summary", name="Summary", schema_type="string", custom=False),
        ]
    )
    assert field_map.id_for("Story Points", "number") == "customfield_10001"
    assert field_map.id_for("Summary", "string") == "summary"


def test_field_map_raises_on_unknown_field():
    field_map = fields.FieldMap([])
    with pytest.raises(KeyError):
        field_map.id_for("Nonexistent")
