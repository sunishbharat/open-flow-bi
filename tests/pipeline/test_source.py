import itertools

import pytest

from openflowbi.jira.deployment import DeploymentProfile
from openflowbi.pipeline.source import _jql, jira_source

APACHE_JIRA = "https://issues.apache.org/jira"


def test_jql_without_cursor_sorts_by_created():
    # No incremental cursor in play (e.g. issue_changelog, which isn't
    # incremental in M5) - unchanged from M3/M4 behaviour.
    assert _jql("KAFKA") == "project = KAFKA order by created asc"
    assert _jql(None) == "order by created asc"


def test_jql_with_cursor_floors_and_sorts_by_updated():
    # A --limit-truncated run must only ever advance the incremental cursor
    # to the oldest-updated issue it actually fetched, which only holds if
    # results are walked oldest-updated-first (M5 design note).
    jql = _jql("KAFKA", updated_since="2024-01-15 09:30")
    assert jql == 'project = KAFKA and updated >= "2024-01-15 09:30" order by updated asc'


def test_jql_with_cursor_and_no_project():
    jql = _jql(None, updated_since="2024-01-15 09:30")
    assert jql == 'updated >= "2024-01-15 09:30" order by updated asc'


@pytest.mark.vcr
def test_fields_resource_yields_flattened_dicts_dc():
    profile = DeploymentProfile(is_cloud=False, base_url=APACHE_JIRA, version="8.20.10", auth=None)  # type: ignore[arg-type]
    source = jira_source(profile)

    rows = list(itertools.islice(source.fields, 3))

    assert len(rows) == 3
    for row in rows:
        assert set(row) == {"field_id", "name", "schema_type", "custom"}
