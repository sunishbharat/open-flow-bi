-- flowbi_ops / jira_raw roles and grants (docs/phase2-postgres-design.md §10).
--
-- Not an Alembic migration: role creation needs superuser and is a one-time
-- operator action, run by hand once per environment (local docker-compose,
-- or the target Postgres instance in whatever deployment eventually hosts
-- it — Cloud Foundry manifests are out of scope for now, per CLAUDE.md).
--
-- Run as a superuser (e.g. the docker-compose `flowbi` bootstrap user):
--   psql "$FLOWBI_POSTGRES_DSN" -v writer_pw=plaintext_pw -v reader_pw=plaintext_pw -f migrations/sql/roles.sql
--
-- Found running this live (2026-09-20): do NOT wrap the -v value in its own
-- single quotes (-v reader_pw="'...'"). This script's `PASSWORD :'reader_pw'`
-- already asks psql to quote the substituted value — quoting it again at the
-- shell level bakes literal quote characters into the password itself.

-- flowbi_writer: dlt needs to create and alter its own schema, and Alembic
-- needs the same for flowbi_ops. dlt's docs note the loader user is simplest
-- as the schema owner — compatible with least privilege as long as it owns
-- only jira_raw (+ its per-pipeline staging schemas) and flowbi_ops, not the
-- whole database.
--
-- Found running this live on a genuinely fresh database (2026-09-21): this
-- script was previously assumed to run *before* any extraction, but
-- `GRANT ... ON SCHEMA jira_raw` / `ALTER SCHEMA jira_raw OWNER TO ...` both
-- fail with "schema does not exist" if no extraction has created jira_raw
-- yet (dlt owns creating it — CLAUDE.md rule 2 — this script never should,
-- and still doesn't: `CREATE SCHEMA IF NOT EXISTS` below is a no-op on a
-- database where jira_raw already exists from a prior `flowbi extract`
-- run, and only bootstraps the empty schema + ownership on a fresh one).
CREATE ROLE flowbi_writer LOGIN PASSWORD :'writer_pw';
CREATE SCHEMA IF NOT EXISTS jira_raw AUTHORIZATION flowbi_writer;
GRANT CREATE, USAGE ON SCHEMA jira_raw TO flowbi_writer;
ALTER SCHEMA jira_raw OWNER TO flowbi_writer;
GRANT CREATE, USAGE ON SCHEMA flowbi_ops TO flowbi_writer;
-- staging schemas (jira_raw_staging_<project>, §6) are created by dlt at
-- runtime, one per pipeline — the writer needs CREATE on the database itself
-- to bring new ones into existence.
GRANT CREATE ON DATABASE openflowbi TO flowbi_writer;

-- Phase 3a.3/3a.4: transform/runner.py writes analytics.issue_status_interval
-- and analytics.issue (including dynamically ALTER TABLE ADD COLUMN / CREATE
-- TABLE for promoted columns and bridge tables) using whatever role
-- FLOWBI_POSTGRES_DSN names. The documented local default is the bootstrap
-- superuser itself (.env.example), which already owns everything - this
-- grant only matters for a hardened deployment that points FLOWBI_POSTGRES_DSN
-- at flowbi_writer specifically, which otherwise has no privilege on
-- `analytics` at all.
GRANT CREATE, USAGE ON SCHEMA analytics TO flowbi_writer;

-- cube_reader: Phase 3+ (Cube itself is out of scope now), created here so
-- the read/write boundary is real from day one rather than retrofitted.
CREATE ROLE cube_reader LOGIN PASSWORD :'reader_pw';
GRANT USAGE ON SCHEMA analytics TO cube_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO cube_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA analytics GRANT SELECT ON TABLES TO cube_reader;
-- Deliberately no grant on jira_raw or flowbi_ops: a bug in a future
-- row-level-security policy on `analytics` can never become a full dump of
-- the raw extracted data.
