from openflowbi.pipeline.source import _jql

# The "fields" dlt resource wrapper is exercised end-to-end in
# tests/pipeline/test_run.py (via pipeline_run.run(), which properly closes
# dlt's ManagedPipeIterator worker thread) rather than here via direct
# DltResource iteration, which doesn't - see that module's comments.


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
