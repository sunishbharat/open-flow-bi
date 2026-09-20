-- flowbi_ops / jira_raw roles and grants (docs/phase2-postgres-design.md §10).
--
-- Not an Alembic migration: role creation needs superuser and is a one-time
-- operator action, run by hand once per environment (local docker-compose,
-- or the target Postgres instance in whatever deployment eventually hosts
-- it — Cloud Foundry manifests are out of scope for now, per CLAUDE.md).
--
-- Run as a superuser (e.g. the docker-compose `flowbi` bootstrap user):
--   psql "$FLOWBI_POSTGRES_DSN" -v writer_pw='...' -v reader_pw='...' -f migrations/sql/roles.sql

-- flowbi_writer: dlt needs to create and alter its own schema, and Alembic
-- needs the same for flowbi_ops. dlt's docs note the loader user is simplest
-- as the schema owner — compatible with least privilege as long as it owns
-- only jira_raw (+ its per-pipeline staging schemas) and flowbi_ops, not the
-- whole database.
CREATE ROLE flowbi_writer LOGIN PASSWORD :'writer_pw';
GRANT CREATE, USAGE ON SCHEMA jira_raw TO flowbi_writer;
ALTER SCHEMA jira_raw OWNER TO flowbi_writer;
GRANT CREATE, USAGE ON SCHEMA flowbi_ops TO flowbi_writer;
-- staging schemas (jira_raw_staging_<project>, §6) are created by dlt at
-- runtime, one per pipeline — the writer needs CREATE on the database itself
-- to bring new ones into existence.
GRANT CREATE ON DATABASE openflowbi TO flowbi_writer;

-- cube_reader: Phase 3+ (Cube itself is out of scope now), created here so
-- the read/write boundary is real from day one rather than retrofitted.
CREATE ROLE cube_reader LOGIN PASSWORD :'reader_pw';
GRANT USAGE ON SCHEMA analytics TO cube_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO cube_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA analytics GRANT SELECT ON TABLES TO cube_reader;
-- Deliberately no grant on jira_raw or flowbi_ops: a bug in a future
-- row-level-security policy on `analytics` can never become a full dump of
-- the raw extracted data.
