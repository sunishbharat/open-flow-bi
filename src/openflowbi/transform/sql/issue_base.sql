-- Upserts analytics.issue's fixed columns for :instance_id, scoped to
-- :issue_ids (a bigint[]) or every issue when :issue_ids IS NULL. Runs
-- unconditionally on every transform pass, even with zero fields promoted
-- yet, so every issue in scope has a base row for the promoted-column
-- UPDATE (runner.py) to target.

INSERT INTO analytics.issue (instance_id, issue_id, issue_key, created_at, updated_at, rebuilt_at)
SELECT instance_id, issue_id, issue_key, created_at, updated_at, now()
FROM jira_raw.issues
WHERE instance_id = :instance_id
  AND (CAST(:issue_ids AS bigint[]) IS NULL OR issue_id = ANY(:issue_ids))
ON CONFLICT (instance_id, issue_id) DO UPDATE SET
    issue_key = EXCLUDED.issue_key,
    created_at = EXCLUDED.created_at,
    updated_at = EXCLUDED.updated_at,
    rebuilt_at = EXCLUDED.rebuilt_at;
