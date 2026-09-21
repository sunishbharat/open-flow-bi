-- Rebuilds analytics.issue_status_interval rows for :instance_id, scoped to
-- :issue_ids (a bigint[]) or every issue when :issue_ids IS NULL — "a full
-- rebuild is the same SQL with the id set replaced by all"
-- (docs/phase3-field-selection-design.md §4). The caller (runner.py) deletes
-- any existing rows for the same scope first, in the same transaction.
--
-- seq=0 is always seeded from jira_raw.issues.created_at: the changelog only
-- records transitions, never the initial state (CLAUDE.md's Jira API facts).
-- from_id/from_value on the first status-change row (if any) become seq=0's
-- status; with no status changes at all, the issue has never left its
-- current status, so that becomes seq=0 with an open-ended exited_at.

WITH status_changes AS (
    SELECT issue_id, history_id, created_at AS changed_at, from_id, from_value, to_id, to_value
    FROM jira_raw.issue_changelog
    WHERE instance_id = :instance_id
      AND field = 'status'
      AND (CAST(:issue_ids AS bigint[]) IS NULL OR issue_id = ANY(:issue_ids))
),
ordered AS (
    SELECT *,
           row_number() OVER (PARTITION BY issue_id ORDER BY changed_at, history_id) AS rn
    FROM status_changes
),
seeded AS (
    SELECT
        i.issue_id,
        0 AS seq,
        COALESCE(first_t.from_id, i.fields -> 'status' ->> 'id') AS status_id,
        COALESCE(first_t.from_value, i.fields -> 'status' ->> 'name') AS status_name,
        i.created_at AS entered_at,
        first_t.changed_at AS exited_at
    FROM jira_raw.issues i
    LEFT JOIN ordered first_t ON first_t.issue_id = i.issue_id AND first_t.rn = 1
    WHERE i.instance_id = :instance_id
      AND (CAST(:issue_ids AS bigint[]) IS NULL OR i.issue_id = ANY(:issue_ids))
),
transitions AS (
    SELECT
        o.issue_id,
        o.rn AS seq,
        o.to_id AS status_id,
        o.to_value AS status_name,
        o.changed_at AS entered_at,
        nxt.changed_at AS exited_at
    FROM ordered o
    LEFT JOIN ordered nxt ON nxt.issue_id = o.issue_id AND nxt.rn = o.rn + 1
)
INSERT INTO analytics.issue_status_interval
    (instance_id, issue_id, seq, status_id, status_name, entered_at, exited_at, duration_seconds)
SELECT
    :instance_id,
    issue_id,
    seq,
    status_id,
    status_name,
    entered_at,
    exited_at,
    CASE WHEN exited_at IS NOT NULL THEN EXTRACT(EPOCH FROM (exited_at - entered_at)) END
FROM (
    SELECT * FROM seeded
    UNION ALL
    SELECT * FROM transitions
) all_rows;
