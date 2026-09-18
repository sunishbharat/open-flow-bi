-- Standing debug queries for `duckdb -c` / `duckdb -ui` against out/jira_raw/**/*.parquet.
-- See CLAUDE.md "Inspecting output" — add to this file rather than writing one-off scripts.

-- Changelog source-tier coverage: how many rows came from each of the 3 tiers.
SELECT source, count(*) AS rows, count(DISTINCT issue_id) AS issues
FROM 'out/jira_raw/issue_changelog/*.parquet'
GROUP BY source
ORDER BY rows DESC;

-- Duplicate item_index within a single (issue_id, history_id) — should be zero rows.
-- A non-empty result means flatten.changelog's deterministic sort broke, or a
-- re-ingest duplicated a reordered items[] (CLAUDE.md "Jira API facts").
SELECT issue_id, history_id, item_index, count(*) AS n
FROM 'out/jira_raw/issue_changelog/*.parquet'
GROUP BY issue_id, history_id, item_index
HAVING count(*) > 1;

-- Issues with an incomplete changelog: no changelog_complete=False row exists
-- for an issue whose per-issue tier was entirely unavailable (empty
-- histories), so diff against the issues table instead of filtering the
-- changelog table alone.
SELECT i.issue_id, i.issue_key
FROM 'out/jira_raw/issues/*.parquet' i
LEFT JOIN (
    SELECT DISTINCT issue_id FROM 'out/jira_raw/issue_changelog/*.parquet'
) c ON c.issue_id = i.issue_id
WHERE c.issue_id IS NULL;

-- Explicit changelog_complete=False rows (per-issue tier fetched but the API
-- reported it as partial).
SELECT DISTINCT issue_id
FROM 'out/jira_raw/issue_changelog/*.parquet'
WHERE changelog_complete = false;

-- Status transition matrix: from_value -> to_value counts for the "status" field.
SELECT from_value, to_value, count(*) AS transitions
FROM 'out/jira_raw/issue_changelog/*.parquet'
WHERE field = 'status'
GROUP BY from_value, to_value
ORDER BY transitions DESC;

-- Run-to-run diff: row counts per _dlt_load_id (each pipeline.run() call gets
-- its own load id — dlt's filesystem layout has no run=<load_id> directory,
-- see CLAUDE.md "Inspecting output", so diff on this column instead).
SELECT _dlt_load_id, count(*) AS rows
FROM 'out/jira_raw/issues/*.parquet'
GROUP BY _dlt_load_id
ORDER BY _dlt_load_id DESC;

-- Field discovery: every top-level `fields` key present on one issue, one per
-- row (fields is a passthrough JSON column — see M4 notes in
-- pipeline/source.py on why it isn't exploded into child tables). Custom
-- field ids (customfield_NNNNN) are per-instance — CLAUDE.md rule "map by
-- (name, schema type), never hard-code an id" — so treat these as
-- discovery-only, not stable identifiers to hard-code elsewhere.
SELECT unnest(json_keys(fields)) AS field_key
FROM (SELECT fields FROM 'out/jira_raw/issues/*.parquet' LIMIT 1);

-- Full raw value of one field, to see its shape before deciding how to
-- extract it (objects like status/assignee/priority carry more than just a
-- display name — e.g. status also has statusCategory.key, useful for
-- grouping into to-do/in-progress/done buckets).
SELECT issue_key, json_extract(fields, '$.status') AS status_raw
FROM 'out/jira_raw/issues/*.parquet'
LIMIT 5;

-- Common fields pulled out of the `fields` JSON passthrough column.
-- `fields` has no top-level `status`/`assignee`/etc. columns by design
-- (custom field ids and shapes differ per Jira instance — rule 4/CLAUDE.md's
-- "fields stays a passthrough dict[str, Any]"), so pull nested values out
-- with json_extract_string(fields, '$.<path>') instead of selecting them
-- directly. Swap the path for any other field the same way, e.g.
-- '$.priority.name' or '$.reporter.displayName'.
SELECT
    issue_id,
    issue_key,
    json_extract_string(fields, '$.status.name') AS status,
    json_extract_string(fields, '$.status.statusCategory.key') AS status_category,
    json_extract_string(fields, '$.assignee.displayName') AS assignee,
    updated_at
FROM 'out/jira_raw/issues/*.parquet'
ORDER BY updated_at;
