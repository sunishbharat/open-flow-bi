-- cube_reader's read-only grant on jira_raw - the `issues` cube
-- (cube/model/cubes/issues.yml) reads jira_raw.issues directly, which
-- cube_reader cannot do under its base grant (migrations/sql/roles.sql,
-- analytics only).
--
-- Kept as its own script, not folded into roles.sql: roles.sql runs once at
-- environment bootstrap, before jira_raw exists (dlt creates that schema on
-- first extraction) - a GRANT ... ON SCHEMA jira_raw run at that point would
-- fail with "schema does not exist". Run this only after at least one
-- extraction has landed data in jira_raw (e.g. `flowbi extract issues
-- --destination postgres`), as a superuser:
--
--   psql "$FLOWBI_POSTGRES_DSN" -f migrations/sql/cube_reader_grants.sql
--
-- Idempotent - safe to re-run. Deliberately two tables, not the whole
-- schema, and no write access. Meant to be revoked once analytics-backed
-- cubes replace the ones that read jira_raw.
GRANT USAGE ON SCHEMA jira_raw TO cube_reader;

-- Each table granted independently, tolerant of the other not existing yet:
-- `extract issues` and `extract changelog` are separate CLI commands
-- (docs/cli.md), so an operator (or a CI fixture that only seeds `issues`,
-- as CI's cube-smoke job does) may legitimately have only one of the two
-- tables at grant time. A plain multi-table GRANT fails outright if either
-- is missing.
DO $$
BEGIN
    GRANT SELECT ON jira_raw.issues TO cube_reader;
EXCEPTION WHEN undefined_table THEN
    RAISE NOTICE 'jira_raw.issues does not exist yet - grant skipped, re-run after `extract issues`';
END $$;

DO $$
BEGIN
    GRANT SELECT ON jira_raw.issue_changelog TO cube_reader;
EXCEPTION WHEN undefined_table THEN
    RAISE NOTICE 'jira_raw.issue_changelog does not exist yet - grant skipped, re-run after `extract changelog`';
END $$;
