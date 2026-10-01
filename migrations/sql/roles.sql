-- flowbi_ops / jira_raw / analytics roles and ownership (docs/phase2-postgres-design.md §10).
--
-- Not an Alembic migration: role creation needs superuser and is an
-- operator action, run by hand per environment (local docker-compose, or the
-- target Postgres instance in whatever deployment eventually hosts it —
-- Cloud Foundry manifests are out of scope for now).
--
-- Run as a superuser (e.g. the docker-compose `flowbi` bootstrap user):
--   psql "$FLOWBI_POSTGRES_DSN" -v writer_pw=plaintext_pw -v reader_pw=plaintext_pw -f migrations/sql/roles.sql
--
-- Found running this live (2026-09-20): do NOT wrap the -v value in its own
-- single quotes (-v reader_pw="'...'"). This script's `PASSWORD :'reader_pw'`
-- already asks psql to quote the substituted value — quoting it again at the
-- shell level bakes literal quote characters into the password itself.
--
-- Safe to re-run, and re-run it after anything the superuser created in
-- these schemas: an `alembic upgrade` or `flowbi extract` run as the
-- superuser leaves tables flowbi_writer does not own (architecture review
-- finding 12). Better still, once this has run, point FLOWBI_POSTGRES_DSN at
-- flowbi_writer for Alembic, extraction and transform alike, so it owns
-- everything it creates.
--
-- `\gexec` runs each row a query returns as a statement: psql variables
-- don't expand inside a DO $$ ... $$ block, and object names from the
-- catalog need %I quoting.

\set ON_ERROR_STOP on

-- flowbi_writer runs the whole pipeline: dlt (jira_raw and its staging
-- schemas), Alembic (flowbi_ops, analytics) and transform (analytics). It
-- must OWN those tables, not just hold grants on them: dlt evolves its
-- tables with ALTER TABLE, and transform adds promoted columns to
-- analytics.issue with ALTER TABLE, and only an owner may do either.
-- Ownership stops at these schemas, never the whole database.
SELECT 'CREATE ROLE flowbi_writer'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'flowbi_writer') \gexec
ALTER ROLE flowbi_writer LOGIN PASSWORD :'writer_pw';

-- jira_raw is dlt's to create; this only bootstraps it (empty, owned) on a
-- fresh database, so the ownership below has something to apply to.
CREATE SCHEMA IF NOT EXISTS jira_raw AUTHORIZATION flowbi_writer;
CREATE SCHEMA IF NOT EXISTS flowbi_ops AUTHORIZATION flowbi_writer;
CREATE SCHEMA IF NOT EXISTS analytics AUTHORIZATION flowbi_writer;

-- Staging schemas (jira_raw_staging_<project>, §6) are created by dlt at
-- runtime, one per pipeline: the writer needs CREATE on the database.
SELECT format('GRANT CREATE ON DATABASE %I TO flowbi_writer', current_database()) \gexec

SELECT format('ALTER SCHEMA %I OWNER TO flowbi_writer', nspname)
FROM pg_namespace
WHERE nspname IN ('jira_raw', 'flowbi_ops', 'analytics')
   OR nspname LIKE 'jira\_raw\_staging\_%' \gexec

-- Tables, views and materialized views. Sequences owned by a table (serial
-- and identity columns) move with their table.
SELECT format(
    CASE c.relkind
        WHEN 'v' THEN 'ALTER VIEW %I.%I OWNER TO flowbi_writer'
        WHEN 'm' THEN 'ALTER MATERIALIZED VIEW %I.%I OWNER TO flowbi_writer'
        ELSE 'ALTER TABLE %I.%I OWNER TO flowbi_writer'
    END,
    n.nspname, c.relname)
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'v', 'm')
  AND (n.nspname IN ('jira_raw', 'flowbi_ops', 'analytics')
       OR n.nspname LIKE 'jira\_raw\_staging\_%')
  AND pg_get_userbyid(c.relowner) <> 'flowbi_writer' \gexec

-- cube_reader: read-only, analytics only (Phase 4 P4-D1). Deliberately no
-- grant on flowbi_ops, and jira_raw only through cube_reader_grants.sql's
-- two tables: a bug in a future row-level-security policy on `analytics`
-- can never become a full dump of the raw extracted data.
SELECT 'CREATE ROLE cube_reader'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'cube_reader') \gexec
ALTER ROLE cube_reader LOGIN PASSWORD :'reader_pw';
GRANT USAGE ON SCHEMA analytics TO cube_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO cube_reader;
-- Default privileges attach to the role that creates a table, so cover
-- both: flowbi_writer (the normal case) and whoever runs this script (a
-- migration still run as the superuser).
ALTER DEFAULT PRIVILEGES FOR ROLE flowbi_writer IN SCHEMA analytics
    GRANT SELECT ON TABLES TO cube_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA analytics GRANT SELECT ON TABLES TO cube_reader;
