# open-flow-bi

Self hosted flow metrics for jira with full changelog, history, flow metrics modelled in Cube.

Right now only the **extractor** is built: a `flowbi` CLI that pulls Jira issues and their full
changelog (Cloud or Server/DC) into Parquet files via [dlt](https://github.com/dlt-hub/dlt). See
`CLAUDE.md` for the working rules and `docs/library-decision-register.md` for the architecture.
Current build status: `docs/session-status-2026-09-18.md`.

## Setup

```bash
uv sync
cp .env.example .env   # prefilled with Apache's public Jira, works as-is - see Quickstart below
```

`.env` fields:

| Var | Required for | Notes |
|---|---|---|
| `FLOWBI_JIRA_BASE_URL` | always | e.g. `https://your-domain.atlassian.net` or `https://jira.your-company.com` |
| `FLOWBI_JIRA_EMAIL` + `FLOWBI_JIRA_API_TOKEN` | Cloud | |
| `FLOWBI_JIRA_PAT` | Server/DC | personal access token |
| `FLOWBI_JIRA_PROJECT` | optional | scopes extraction to one project key, e.g. `KAFKA` |
| `FLOWBI_JIRA_INCREMENTAL_START` | optional | ISO datetime floor for `extract issues`' first run (default: epoch, i.e. everything) |
| `FLOWBI_JIRA_INSTANCE_ID` | optional | stable slug, part of every Postgres primary key (default: derived from the base URL's host, e.g. `issues-apache-org`) |

## Quickstart — try it in under a minute

No real Jira account needed for this: `.env.example` is prefilled with Apache's public Jira
(`https://issues.apache.org/jira`, anonymous access, `KAFKA` project), so it works straight out of
a fresh clone.

```bash
uv sync
cp .env.example .env
uv run flowbi doctor                          # confirms it can reach Jira; reports deployment + account timezone
uv run flowbi extract issues --limit 5        # 5 real KAFKA issues -> out/jira_raw/issues/*.parquet
uv run flowbi extract changelog --limit 5     # their changelogs -> out/jira_raw/issue_changelog/*.parquet
uv run flowbi quality check issues            # OK: 5 rows, 1 file(s)
uv run flowbi quality check issue_changelog
```

Look at what landed (see "Inspecting output" below for more, including a no-binary fallback):

```bash
# git bash
./duckdb.exe -c "SELECT issue_key, json_extract_string(fields, '\$.status.name') AS status FROM 'out/jira_raw/issues/*.parquet'"

# PowerShell
.\duckdb.exe -c "SELECT issue_key, json_extract_string(fields, '\$.status.name') AS status FROM 'out/jira_raw/issues/*.parquet'"
```

If `doctor`/`extract` fail with `SSLError`/`UnknownIssuer`, you're behind a TLS-intercepting proxy
or antivirus — see `docs/session-status-2026-09-18.md`'s blocker/workaround section.

To point this at your own Jira instead of Apache's, edit `.env` with your `FLOWBI_JIRA_BASE_URL`
and either Cloud (`FLOWBI_JIRA_EMAIL` + `FLOWBI_JIRA_API_TOKEN`) or Server/DC (`FLOWBI_JIRA_PAT`)
credentials, per the fields table above. Everything above writes to the local filesystem
(Parquet under `out/`) — see "Postgres (Phase 2)" below to try `--destination postgres` instead.

## CLI

```bash
uv run flowbi --help
uv run flowbi doctor                          # detect deployment, account timezone, rate-limit budget
uv run flowbi extract fields --sink table      # preview field (name, schema type) -> id map
uv run flowbi extract issues --limit 20        # issues -> out/jira_raw/issues/*.parquet
uv run flowbi extract changelog --limit 20     # changelog (3-tier) -> out/jira_raw/issue_changelog/*.parquet
uv run flowbi quality check issues             # validate out/jira_raw/issues/*.parquet against its pandera contract
uv run flowbi quality check issue_changelog    # same, for the changelog table
uv run flowbi quality check issues --destination postgres   # whole-table SQL invariants against Postgres instead
```

`--limit` bounds every extract command — a debug run never walks a whole project by accident.

## Postgres (Phase 2)

`extract issues`/`extract changelog`/`extract fields` all take `--destination postgres` as an
alternative to the default `filesystem` (Parquet). See `docs/phase2-postgres-design.md` for the
full design; `docker-compose.yml` runs a local Postgres for this:

```bash
docker compose up -d                                    # Postgres on host port 5433 (see FLOWBI_POSTGRES_DSN in .env.example)
uv run flowbi extract issues --limit 20 --destination postgres
uv run flowbi extract changelog --limit 20 --destination postgres
```

dlt owns `jira_raw` (and its staging schemas) exclusively — created, evolved and merged by dlt
itself, never by hand-written DDL or Alembic (CLAUDE.md non-negotiable rule 2). `merge` write
disposition with compound `(instance_id, ...)` primary keys means re-running the same extract
doesn't duplicate rows; both `issues` and `issue_changelog` are incremental (state persists in
Postgres, in `jira_raw._dlt_pipeline_state`) — a re-run only re-fetches issues updated since the
last successful run's watermark, and dlt's own cursor-value+primary-key deduplication drops the
inclusive floor's boundary repeat before it ever reaches the destination (see "Testing the
changelog incremental (M7.5)" below).

**One dlt pipeline per (instance, project)** (`pipeline.pipeline_name`, e.g.
`openflowbi_issues-apache-org_kafka`) — isolates incremental watermarks between projects, and, for
Postgres, gives each project its own staging schema (`jira_raw_staging_<project>` via
`staging_dataset_name_layout`) so two projects loading concurrently never share one physical
staging table (a known dlt defect, [dlt#4297](https://github.com/dlt-hub/dlt/issues/4297), that
silently duplicates or drops rows when they do). A Postgres advisory lock
(`pipeline/locks.py`) additionally serializes just the load step — not extraction — across every
process pointed at the same DSN, belt-and-braces while dlt's own fix for this is still
opt-in/unreleased. See `docs/phase2-postgres-design.md` §6 and the "Testing concurrency (M7.4)"
section below.

**`flowbi_ops`** (control-plane tables: `jira_instance`, `sync_run`, `rate_budget`,
`issue_dirty`) is Alembic-owned, never dlt. `issue_dirty` is populated as of M7.5 — every
`--destination postgres` `extract changelog`/`extract issues` run upserts the `(instance_id,
issue_id)` pairs that load actually touched (`pipeline/dirty.py`, read off the completed load's
`_dlt_load_id`s); nothing consumes it until Phase 3. `sync_run` is populated as of M7.6 — every
`flowbi quality check <table> --destination postgres` invocation writes one row recording its
verdict (`ops/sync_run.py`; see "Testing SQL quality checks against Postgres (M7.6)" below).
`jira_instance` and `rate_budget` still aren't populated by application code — out of M7.6's scope
per `docs/phase2-postgres-design.md` §14. Alembic is configured (`migrations/env.py`)
to only ever see `flowbi_ops`/`analytics`, never `jira_raw` — verified by the fact that `alembic
revision --autogenerate` against a database with real `jira_raw` data produces an **empty**
migration:

```bash
uv run alembic upgrade head                    # creates flowbi_ops + its 4 tables
uv run alembic revision --autogenerate -m probe   # must generate an empty upgrade()/downgrade() - delete the file after checking
```

`extract issues` is incremental: each successful run advances a watermark (dlt pipeline state,
keyed on `updated_at`) and the next run only re-fetches issues updated since then. A
`--limit`-truncated run only ever advances the watermark to the oldest-updated issue it actually
fetched, so a later unlimited run still picks up whatever the limit cut off — see
`docs/session-status-2026-09-18.md`'s M5 section for the verified kill/resume behaviour.

### Inspecting the Postgres database

**`psql` via the running container needs zero setup** and is the fastest way to look at
`--destination postgres` output — no local Postgres client install, no GUI:

```bash
docker exec -it open-flow-bi-postgres-1 psql -U flowbi -d openflowbi
```

Then, at the `psql` prompt:

```sql
\dn                              -- list schemas (jira_raw, jira_raw_staging_<project>, flowbi_ops)
\dt jira_raw.*                   -- list dlt-owned tables
SELECT * FROM jira_raw.issues LIMIT 5;
SELECT * FROM flowbi_ops.sync_run ORDER BY started_at DESC LIMIT 5;
```

`\q` to exit. `docker exec` only works while `docker compose up -d` is running (see "Postgres
(Phase 2)" above); the container name is fixed by `docker-compose.yml`'s service name
(`open-flow-bi-postgres-1` — confirm with `docker compose ps` if you ever rename the service).

**A GUI client (e.g. pgAdmin4) works too**, pointed at the same connection `FLOWBI_POSTGRES_DSN`
in `.env` uses: host `localhost`, port `5433` (not the default 5432 — this project's compose file
deliberately avoids colliding with another unrelated local Postgres), database `openflowbi`, user
`flowbi`, password `flowbi`. **pgAdmin4's desktop app is prone to a Windows-only
`access violation writing 0x0000` crash** (an Electron/webview runtime bug, not anything in this
project) — if you hit it, clearing `%APPDATA%\pgAdmin4` and `%LOCALAPPDATA%\pgAdmin4` before
relaunching usually fixes it; `psql` above is the reliable fallback that needs no client install at
all.

As with the Parquet output below, **never hand-edit anything under `jira_raw` or
`jira_raw_staging_*`** from a SQL client — dlt owns those schemas exclusively and reconciles them
against its own stored schema on every run, so an out-of-band edit will make dlt's schema and the
database disagree (`CLAUDE.md` non-negotiable rule 2). `flowbi_ops` is Alembic's; edit it only
through a migration.

## Inspecting output

Files land as Parquet under `out/jira_raw/<table>/*.parquet`. The `duckdb` CLI binary
(`duckdb.exe`) lives at the repo root — it's gitignored (`*.exe`), not a project dependency, so it
won't affect anyone else's checkout. Run it from the repo root:

```bash
# git bash
./duckdb.exe -c "SELECT * FROM 'out/jira_raw/issues/*.parquet' LIMIT 20"
./duckdb.exe -ui

# PowerShell
.\duckdb.exe -c "SELECT * FROM 'out/jira_raw/issues/*.parquet' LIMIT 20"
.\duckdb.exe -ui
```

(If you instead move `duckdb.exe` onto your `PATH`, e.g. `C:\Users\<you>\bin`, drop the `./`/`.\`
prefix and just run `duckdb -c ...` from anywhere. [VisiData](https://www.visidata.org/) (`vd`) is
an alternative if installed: `vd out/jira_raw/issue_changelog/*.parquet`.)

Without any CLI installed, the `duckdb` Python package (already pulled in by `dlt[duckdb]`) gives
the same table output — useful in CI or on a machine without the binary:

```bash
uv run python -c "
import duckdb
print(duckdb.sql(\"SELECT issue_id, source, field, from_value, to_value, created_at FROM 'out/jira_raw/issue_changelog/*.parquet' LIMIT 20\"))
"
```

`debug/queries.sql` holds the standing debug queries (source-tier coverage, duplicate `item_index`,
incomplete changelogs, status transition matrix, run-to-run diff via `_dlt_load_id`, common-field
extraction) — run any of them the same way (`./duckdb.exe -c "$(cat debug/queries.sql)"`, or paste
into `./duckdb.exe -ui`). Don't write one-off inspection scripts; add to that file instead.

`issues`' `fields` column is a JSON **passthrough** — there's no top-level `status`/`assignee`/etc.
column (custom field ids and shapes differ per Jira instance, so it's never exploded into columns;
see `CLAUDE.md`). Pull a value out with `json_extract_string`:

```bash
./duckdb.exe -c "SELECT issue_key, json_extract_string(fields, '\$.status.name') AS status FROM 'out/jira_raw/issues/*.parquet' LIMIT 20"
```

Swap the `$.status.name` path for any other field the same way, e.g. `$.priority.name` or
`$.assignee.displayName` — see `debug/queries.sql` for a ready-made query.

## Testing

```bash
uv run pytest -q            # replays VCR cassettes, no network
uv run ruff check src tests
uv run mypy src
```

These three commands are what `.github/workflows/ci.yml` runs on every push/PR (via `uv sync
--locked` first) — running them locally before pushing is the same check CI will do.

Tests replay cassettes under `tests/cassettes/{cloud,dc}/` — both trees must pass. Recording a new
cassette needs real network access (`--record-mode=once`); see `docs/session-status-2026-09-18.md`
if you hit TLS/proxy issues recording against a live Jira instance.

Cassette-based HTTP tests only exist at the `jira/*.py` layer (plain functions). Anything under
`pipeline/` that needs to simulate HTTP uses `unittest.mock.patch` on `_search_pages`/
`account_timezone`/`fields_mod.fetch` instead of a cassette — see `docs/session-status-2026-09-18.md`'s
M6 section for why (a real dlt+vcrpy worker-thread race, not a style choice). Follow that pattern
for any new `pipeline/`-level test rather than adding another cassette there.

### Testing concurrency (M7.4)

`tests/pipeline/test_concurrency.py::test_concurrent_projects` needs a real Postgres (via
`testcontainers`, Docker must be running) — it's excluded from the default `pytest -q` run by the
`postgres` marker (`addopts` in `pyproject.toml`), keeping the standing gate zero-network. Run it
explicitly:

```bash
uv run pytest -m postgres tests/pipeline/test_concurrency.py -v
```

It loads two projects into the same Postgres database on separate threads (mocked Jira HTTP, real
dlt + Postgres) and asserts zero rows lost and zero duplicate `(instance_id, issue_id)` pairs —
the scenario [dlt#4297](https://github.com/dlt-hub/dlt/issues/4297) breaks when two pipelines
share one staging dataset. `pipeline/run.py`'s per-project `staging_dataset_name_layout` and
`pipeline/locks.py`'s advisory lock are what make it pass.

### Testing the changelog incremental and issue_dirty (M7.5)

Automated, no network — mirrors the M5 `issues` incremental tests, one resource swapped for the
other:

```bash
uv run pytest tests/pipeline/test_run.py -q -k changelog_limit_truncated
uv run pytest tests/jira/test_flatten.py -q -k updated_at
```

`issue_changelog`'s incremental cursor is `updated_at` — the *issue's* own `fields.updated`,
stamped onto every flattened row by `pipeline/source.py`'s `issue_changelog` resource (not the
history's `created_at`, a different concept the row also carries). An issue with zero changelog
items yields zero rows and so can't advance the cursor on its own; that's safe by the same
argument M5's `--limit` truncation relies on (the watermark only ever advances to what was actually
fetched and emitted, never silently skipping something unfetched) — see the comment on
`issue_changelog` in `pipeline/source.py` for the full argument.

`issue_dirty` needs a real Postgres (same `testcontainers` requirement as M7.4's concurrency test,
excluded from the default run by the `postgres` marker):

```bash
uv run pytest -m postgres tests/pipeline/test_issue_dirty.py -v
```

It runs `extract changelog --destination postgres` twice — once against four fresh issues, once
against the same four plus one issue that genuinely changed and one brand-new issue — and asserts
each load only touches (and marks dirty) the issue ids it actually loaded: dlt's own
cursor-value+primary-key deduplication (`Incremental.primary_key`) drops an unchanged issue caught
by the inclusive `updated >= floor` JQL filter before it ever reaches the destination, so it's
correctly excluded from that run's `issue_dirty` write. `pipeline/dirty.py`'s `mark_dirty()` reads
`_dlt_load_id`s off the completed `LoadInfo` and upserts against `jira_raw.issue_changelog`/
`jira_raw.issues` directly — there's no public dlt API to read row-level primary keys off
`LoadInfo` itself.

**`issue_dirty` population is best-effort, not a gate**: if `flowbi_ops` hasn't been migrated yet
(`alembic upgrade head` — see above) the upsert fails with `UndefinedTable`, and `pipeline/run.py`
catches it, logs a `structlog` warning (`issue_dirty_mark_failed`), and returns the otherwise-
successful `LoadInfo` unchanged. Nothing consumes `issue_dirty` until Phase 3, so this must never
fail an extraction that already succeeded — the same graceful-degradation pattern as the
account-timezone fallback in `pipeline/source.py`.

### Testing SQL quality checks against Postgres (M7.6)

`src/openflowbi/quality/sql_checks.py` holds whole-table SQL invariants (uniqueness across every
row, not a bounded sample) — pandera can't answer "is this unique across the whole table" the way
the database can (`docs/phase2-postgres-design.md` §9). Needs a real Postgres (`testcontainers`,
same requirement as M7.4/M7.5's Postgres-marked tests, excluded from the default `pytest -q` run):

```bash
uv run pytest -m postgres tests/quality/test_sql_checks.py -v
uv run pytest -m postgres tests/test_cli_quality_postgres.py -v
```

`flowbi quality check <table> --destination postgres` runs the BLOCKING checks registered for that
table (`sql_checks.CHECKS_BY_TABLE`) plus any ALERTING ones, prints a `rich.Table` of both, and
writes one `flowbi_ops.sync_run` row recording the verdict (`status='succeeded'` or
`'quality_failed'`). A single non-zero BLOCKING check fails the whole command (exit 1) — that's
"the quality gate has teeth" (§9). Live-verified against the docker-compose Postgres, not just the
test suite: seeding a duplicate `(instance_id, issue_id)` row into a real `jira_raw.issues` table
made `flowbi quality check issues --destination postgres` exit 1 and write
`sync_run.status='quality_failed'`; removing the duplicate made it pass again.

**A real drift found and fixed while verifying this against live output** (`CLAUDE.md`: "trust the
response and update this file"): the design doc's `changelog_incomplete` ALERTING check queried
`jira_raw.issues.changelog_complete` — that column doesn't exist there. `changelog_complete` is
stamped by `flatten.changelog()` onto `jira_raw.issue_changelog` rows only (`flatten.py`'s own
comment already explained why: an issue whose per-issue changelog tier is entirely unavailable
yields zero `issue_changelog` rows, so there's no row to carry the flag either way — the same
reason `debug/queries.sql`'s "incomplete changelogs" query diffs `issues` against `issue_changelog`
instead of filtering a column on `issues`). Fixed in `sql_checks.py` and in
`docs/phase2-postgres-design.md` §3/§9; ALERTING checks are now scoped per table
(`ALERTING_BY_TABLE`) so `quality check issues` never touches `issue_changelog` at all, including
on a checkout where that table doesn't exist yet.

**`no_orphan_changelog` is genuinely non-zero against this checkout's accumulated data** (confirmed
live, not hypothetical) — `extract issues` and `extract changelog` have been run independently,
with different `--limit`s, across several sessions, so some changelog rows reference issues that
were never themselves extracted. This is the exact, expected gap `docs/phase2-postgres-design.md`
§9 calls out ("will be non-zero at first ... run issues first ... or scope the check"); the decision
made here (§9 open question #3) is to leave the check whole-table and document the ordering
requirement (run `extract issues` before `extract changelog` for a project) rather than weaken it.

`migrations/sql/roles.sql` (§10 — `flowbi_writer`/`cube_reader` grants) is a one-time operator
script, not an Alembic migration (role creation needs superuser); it's not run by tests. CI
(`.github/workflows/ci.yml`) now runs the `postgres`-marked suite as a second `pytest` step after
the default zero-network one — `testcontainers` needs only a working Docker daemon, which
`ubuntu-latest` runners already provide, so no service-container YAML wiring was needed.

### Testing the quality contracts (M6)

`src/openflowbi/quality/checks.py` holds pandera contracts for the `issues` and `issue_changelog`
tables (`issue_id` unique/non-null, the changelog's `(issue_id, history_id, item_index)` unique
together, `item_index >= 0`, `source` restricted to the three changelog tiers). Pure, no network:

```bash
uv run pytest tests/quality -q
```

Against real output, after an `extract` run:

```bash
uv run flowbi extract issues --limit 20
uv run flowbi quality check issues             # prints OK + row/file counts, or FAILED + the pandera error
uv run flowbi extract changelog --limit 20
uv run flowbi quality check issue_changelog
```

`quality check` exits 1 on a contract violation or if no Parquet files are found yet for that
table — safe to use as a CI/pipeline gate later, not just a manual check.

**A real gotcha, found while verifying this against live output**: `quality check issue_changelog`
validates every accumulated file under `out/jira_raw/issue_changelog/`, the same way
`debug/queries.sql`'s duplicate-`item_index` query does — it does not limit itself to the most
recent `_dlt_load_id`. `extract changelog` (unlike `extract issues`) isn't incremental, so a second
`extract changelog` run against the same `out/` re-fetches and re-appends the same issues (the
filesystem destination falls back from `merge` to `append` — see
`docs/session-status-2026-09-18.md`), which genuinely produces duplicate
`(issue_id, history_id, item_index)` rows on disk, and `quality check` correctly reports `FAILED`
for it. This isn't a bug in the check; it's real information about accumulated dev output. To see
a clean `OK` pass, either clear `out/jira_raw/issue_changelog/` first or point `--out-dir` at a
fresh directory before running `extract changelog` once.

### Testing the incremental extraction (M5)

Automated, no network:

```bash
uv run pytest tests/pipeline/test_run.py -q -k "kill_mid_run or limit_truncated"
uv run pytest tests/jira/test_deployment.py -q -k jql_updated_floor
```

- `test_kill_mid_run_commits_nothing_and_resume_recovers_every_issue` — mocks `_search_pages` to
  raise partway through and asserts nothing is written and the watermark doesn't move; then re-runs
  and asserts every issue is recovered.
- `test_limit_truncated_run_advances_cursor_without_skipping_unfetched_issues` — asserts a
  `--limit`-truncated run's watermark only advances to what it actually fetched, and that a
  follow-up run never re-requests the issues already loaded (a "request-count spy" on the mocked
  search calls).
- `jql_updated_floor` tests cover the timezone conversion the incremental cursor's JQL clause
  depends on (UTC passthrough, cross-timezone offset, and rejecting a non-datetime cursor value).

Live, against a real Jira. Either fill in `.env` (see Setup above) or pass the vars inline — no
credentials needed against Apache's public instance:

```bash
FLOWBI_JIRA_BASE_URL="https://issues.apache.org/jira" \
FLOWBI_JIRA_PAT="dummy-anonymous" \
FLOWBI_JIRA_PROJECT="KAFKA" \
uv run flowbi extract issues --limit 5   # first run: fetches the 5 oldest-updated issues

FLOWBI_JIRA_BASE_URL="https://issues.apache.org/jira" \
FLOWBI_JIRA_PAT="dummy-anonymous" \
FLOWBI_JIRA_PROJECT="KAFKA" \
uv run flowbi extract issues --limit 5   # second run: fetches the next 5 — no repeats, no gaps

./duckdb.exe -c "SELECT issue_id, issue_key, updated_at FROM 'out/jira_raw/issues/*.parquet' ORDER BY updated_at"
```

(If you're behind a TLS-intercepting proxy/antivirus and requests fail with `SSLError`/
`UnknownIssuer`, prefix both commands with `REQUESTS_CA_BUNDLE=<combined-bundle-path>` — see
`docs/session-status-2026-09-18.md`'s blocker/workaround section.)

Confirm the second run picked up strictly where the first left off (no duplicate `issue_id`s below
the first run's max `updated_at`, no missing ones above it). To inspect the raw watermark dlt
persisted between runs:

```bash
uv run python -c "
import dlt
# One pipeline per (instance, project) since M7.4 — openflowbi_<instance_id>_<project>,
# e.g. openflowbi_issues-apache-org_kafka for the KAFKA example above.
p = dlt.attach(pipeline_name='openflowbi_issues-apache-org_kafka')
print(p.state['sources']['jira']['resources']['issues']['incremental']['updated_at'])
"
```

To watch the kill/resume behaviour live rather than via the mocked test, interrupt a real
`extract issues` run (Ctrl-C, same env vars as above) mid-run before it prints a result, then check
the watermark above hasn't moved from its pre-run value and `out/jira_raw/issues/*.parquet` has no
new file — then re-run and confirm it fetches from that same unmoved watermark.
