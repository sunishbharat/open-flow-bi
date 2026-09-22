# open-flow-bi

Self-hosted Jira flow metrics: full issue + changelog history extracted into Postgres, modeled in
Cube, and visualized in Superset — no SaaS, no per-seat licensing.

## Features

- Extracts Jira issues and their complete changelog history (Jira Cloud and Server/Data Center)
- Incremental syncs — a re-run only fetches what changed since the last one
- Loads into local Parquet files, or into Postgres with idempotent merges and data-quality checks
- Discovers which of Jira's (often 200+) fields are actually worth having, lets you select them with
  a versioned, auditable decision log, then materializes them as real Postgres columns — no
  re-reading Jira required
- Builds per-issue status-interval history from the changelog (time-in-status, cycle time), seeded
  correctly from issue creation even though Jira's changelog only records transitions
- A Cube semantic model + Superset dashboard for building charts without writing SQL

## Requirements

- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- Docker, for the Postgres/Cube/Superset stack
- A Jira Cloud or Server/Data Center instance — or just try the Quickstart below, which uses a
  public Jira instance and needs no account

## Installation

```bash
uv sync
cp .env.example .env
```

## Configuration

Edit `.env` (see `.env.example` for prefilled defaults you can try immediately):

| Variable | Required for | Notes |
|---|---|---|
| `FLOWBI_JIRA_BASE_URL` | always | e.g. `https://your-domain.atlassian.net` or `https://jira.your-company.com` |
| `FLOWBI_JIRA_EMAIL` + `FLOWBI_JIRA_API_TOKEN` | Jira Cloud | |
| `FLOWBI_JIRA_PAT` | Jira Server/DC | personal access token |
| `FLOWBI_JIRA_PROJECT` | optional | limit extraction to one project key, e.g. `KAFKA` |
| `FLOWBI_POSTGRES_DSN` | Postgres / dashboard | connection string for the local `docker-compose` Postgres |
| `CUBE_READER_PW` | dashboard | password for Cube's read-only Postgres role |
| `CUBE_SQL_USER` / `CUBE_SQL_PASSWORD` | dashboard | credentials Superset uses to connect to Cube |
| `SUPERSET_SECRET_KEY` / `SUPERSET_ADMIN_PW` | dashboard | local Superset instance |

## Quickstart

Works immediately against a public Jira instance — no account needed:

```bash
uv run flowbi doctor                          # confirms connectivity
uv run flowbi extract issues --limit 5        # -> out/jira_raw/issues/*.parquet
uv run flowbi extract changelog --limit 5     # -> out/jira_raw/issue_changelog/*.parquet
uv run flowbi quality check issues
```

To point this at your own Jira, set `FLOWBI_JIRA_BASE_URL` and your credentials in `.env` (see
Configuration above).

## CLI

```bash
uv run flowbi --help
uv run flowbi doctor                            # detect deployment type, check connectivity
uv run flowbi extract fields --sink table       # preview available fields
uv run flowbi extract issues --limit N          # extract issues
uv run flowbi extract changelog --limit N       # extract full changelog history
uv run flowbi quality check <table>             # validate extracted data
uv run flowbi fields discover [--project X]     # refresh field fill-rate stats (needs Postgres)
uv run flowbi fields list [--min-fill 0.1]      # show fields sorted by fill rate
uv run flowbi transform [--rebuild-all] [--project X]   # materialize promoted columns + status intervals
uv run flowbi fields request-rebuild [--project X]      # queue a rebuild for `flowbi transform` to drain
```

`--limit` bounds every extract command — useful while testing before a full run.

## Using Postgres

Add `--destination postgres` to load into Postgres instead of local Parquet files:

```bash
docker compose up -d postgres
uv run alembic upgrade head                                    # one-time: sets up the control-plane schema
uv run flowbi extract issues --limit 20 --destination postgres
uv run flowbi extract changelog --limit 20 --destination postgres
```

Loads are idempotent (safe to re-run) and incremental (a re-run only fetches issues updated since
the last successful run).

Inspect the data directly:

```bash
docker exec -it open-flow-bi-postgres-1 psql -U flowbi -d openflowbi
```

```sql
SELECT * FROM jira_raw.issues LIMIT 5;
```

### Extracting multiple projects

`FLOWBI_JIRA_PROJECT` scopes extraction to one project — there's no `--project` flag on `extract
issues`/`extract changelog` themselves (only on `fields discover`/`transform`/`fields
request-rebuild`, which read/write instance-wide state instead). To pull several projects, override
the env var per invocation rather than editing `.env` each time:

```bash
# bash / Git Bash
FLOWBI_JIRA_PROJECT=KAFKA uv run flowbi extract issues --limit 5000 --destination postgres
FLOWBI_JIRA_PROJECT=KAFKA uv run flowbi extract changelog --limit 5000 --destination postgres
# repeat for HIVE, HADOOP, ZOOKEEPER, ...
```

```powershell
# PowerShell
$env:FLOWBI_JIRA_PROJECT = "KAFKA"
uv run flowbi extract issues --limit 5000 --destination postgres
uv run flowbi extract changelog --limit 5000 --destination postgres
# repeat for HIVE, HADOOP, ZOOKEEPER, ...
```

`--limit 5000` is just a generously large number so it isn't capped at the CLI default of 20 — raise
it if a project has more issues than that. `extract changelog` is the slow part (per-issue changelog
fetches); expect it to take a while per project. Once every project is extracted, run one **unscoped**
rebuild to cover all of them in a single pass — see "Field discovery and materialization" below.

## Field discovery and materialization (Phase 3a)

Jira's ~200+ fields (most of them custom, per-instance) live as a raw JSON blob on every issue —
`flowbi fields` tells you which ones are actually worth having, before you write a Cube dimension
for one:

```bash
uv run flowbi fields discover --project KAFKA   # samples jira_raw.issues, computes fill rates
uv run flowbi fields list --min-fill 0.5        # only fields filled on >= 50% of the sample
uv run flowbi fields list --custom-only
```

`fields discover` needs Postgres data to sample from (see "Using Postgres" above) and hits Jira
once, live, to refresh field names; `fields list` only reads back what was already discovered, so
it works even without Jira reachable.

Once you've found a field worth having, select it — this only records a decision, it doesn't touch
Jira or rebuild anything by itself:

```bash
uv run flowbi fields promote "Story Points" --column story_points   # add --schema-type if the name is ambiguous
uv run flowbi fields demote "Story Points"                          # never deletes anything, just marks it
uv run flowbi fields export > config/fields.yml                     # the current selection as YAML
uv run flowbi fields import config/fields.yml                       # re-import it (always writes a new version)
```

Every save writes a brand-new version rather than editing in place, so `field_selection` doubles as
a full audit trail of who decided what, when.

**Materialize the selection** — turns the current decision into real, queryable data. This is a
`CREATE ... AS SELECT` over data you already extracted, never a Jira re-read, so promoting and
demoting fields is cheap to change your mind about:

```bash
uv run flowbi transform --rebuild-all               # every issue for this Jira instance
uv run flowbi transform --rebuild-all --project X    # just one project
uv run flowbi transform                              # incremental — only issues touched since the last extract
```

This populates two things in `analytics`:

- **`analytics.issue`** — one row per issue, with a real column for every promoted field (a bridge
  table instead, for array-typed fields like labels or sprints)
- **`analytics.issue_status_interval`** — one row per status period per issue, reconstructed from the
  changelog and correctly seeded from `created_at` (Jira's changelog only records transitions, never
  the status an issue was created in) — this is what gives you cycle-time / time-in-status measures

An incremental `flowbi transform` (no `--rebuild-all`) only touches issues marked dirty by a more
recent `extract issues`/`extract changelog` run, so it's safe to run after every extraction, not just
once. To queue a rebuild without running it inline — e.g. from a script — use `flowbi fields
request-rebuild [--project X]`; the next `flowbi transform` call drains the queue.

## Dashboard: Cube + Superset

A browser-based dashboard for building charts against the extracted data — no SQL required.

**One-time setup**, once you have Postgres data (see above):

```bash
docker exec -it open-flow-bi-postgres-1 psql -U flowbi -d openflowbi \
  -v writer_pw=<pick a password> -v reader_pw=<pick a password> -f migrations/sql/roles.sql
docker exec -it open-flow-bi-postgres-1 psql -U flowbi -d openflowbi \
  -f migrations/sql/cube_reader_grants.sql
docker compose up -d cube superset
```

Whatever you choose for `reader_pw` must also be set as `CUBE_READER_PW` in `.env`.

**Build a chart**, at http://localhost:8088 (first login: `admin` / your `.env`'s `SUPERSET_ADMIN_PW`):

1. **Settings → Database Connections → + Database → PostgreSQL** —
   `postgresql://<CUBE_SQL_USER>:<CUBE_SQL_PASSWORD>@cube:15432/db`
2. **Datasets → + Dataset** — table `flow`
3. **Charts → + Chart** — pick a chart type, a dimension, a metric, then save it to a dashboard

### Example: filtering issues into a chart

To chart, say, "all Open issues with priority Critical or Major":

1. **Charts → + Chart**, dataset `flow`, chart type e.g. Bar Chart
2. Add filters: `status_name` equal to `Open`, `priority_name` is in `Critical, Major`
3. Group by a dimension (e.g. `project_key`), metric `Count`
4. Save

### Example: cycle time / time-in-status

Needs `flowbi transform` to have run at least once (see "Field discovery and materialization" above)
— otherwise these measures come back empty, not wrong.

The `flow` view has two different `status_name`-shaped fields, on purpose — pick the one that answers
your actual question:

| Field | Meaning |
|---|---|
| `status_name` | the issue's **current** status right now |
| `interval_status_name` | whichever status a given **historical interval row** represents — use this one for "how long did issues spend in X" |

1. **Charts → + Chart**, dataset `flow`, chart type e.g. Bar Chart
2. Group by `interval_status_name`
3. Metric: `avg_duration_days` (aggregate **AVG**) — add `total_duration_seconds` (aggregate **SUM**)
   too if you want total time alongside the average
4. Save

### Example: a promoted field (resolution breakdown)

`resolution_name` is a real promoted column (`analytics.issue`, via `flowbi fields promote
"Resolution" --column resolution_name --schema-type resolution` + `flowbi transform --rebuild-all`) —
unlike the fields above, which all read straight out of raw Jira JSON.

1. **Charts → + Chart**, dataset `flow`, chart type e.g. Pie or Bar Chart
2. Group by `resolution_name`, metric `Count`
3. Save

If the field you want to filter or group by isn't available yet, it needs adding to the Cube model
first — see `cube/README.md` and "For developers" below. If you add a new dataset over `flow` (rather
than reusing an existing one), re-sync its columns from **Data → Datasets → Edit → Columns → Sync
columns from source** after any Cube model change.

## For developers: adding a new field/dimension to a report

There's no self-service screen for this yet (that's the Phase 3b item in Roadmap) — today it's one
small, manual YAML edit. Two paths, pick based on how the field will be used.

**Which path?**

| | Path A: promote it | Path B: quick JSON extraction |
|---|---|---|
| Use when | filtered/grouped by often, needs to be fast | occasional, one-off use |
| Storage | real, indexed Postgres column | read live out of the raw `fields` JSON blob, unindexed |
| Setup cost | a few CLI commands + a rebuild | one YAML line, nothing else |

### Path A — promote, then expose it

1. `uv run flowbi fields list --min-fill 0.1` — find the field's exact display name.
2. `uv run flowbi fields promote "Field Name" --column your_column_name` — records the decision only.
3. `uv run flowbi transform --rebuild-all` — materializes `your_column_name` as a real column in
   `analytics.issue` (or a bridge table, for array-typed fields — see "Field discovery and
   materialization" above).
4. Expose it in Cube — add a dimension to `cube/model/cubes/issue.yml` (reads `analytics.issue`, joined
   onto `issues` the same way `issue_status_interval.yml` is), then add it to
   `cube/model/views/flow.yml`'s `issues.issue` `includes` list — a one-line change in each file. The
   `resolution_name` dimension already in both files is a working reference to copy:
   ```yaml
   # cube/model/cubes/issue.yml
   - name: story_points
     sql: story_points
     type: number
   ```
5. In Superset: **Data → Datasets → `flow` → Edit → Columns → Sync columns from source**.
6. Use the field in a chart.

### Path B — skip promotion entirely

Same steps 5–6 as above, but step 4 reads the field straight out of raw Jira data instead of a
promoted column, e.g. in `cube/model/cubes/issues.yml`:

```yaml
- name: your_field_name
  sql: "{CUBE}.fields->>'customfield_10099'"   # or ->'field'->>'name' for an object-shaped field
  type: string
```

No `flowbi fields promote`/`flowbi transform` needed. Cube's dev-mode Playground (bind-mounted
`./cube:/cube/conf`) picks up the file change automatically — no restart required unless something
looks stale, in which case `docker compose restart cube` forces a clean recompile.

### `analytics.issue` is in the Cube model, with one real promoted field so far

`cube/model/cubes/issue.yml` (`sql_table: analytics.issue`, joined onto `issues`) exists and is wired
into the `flow` view. `resolution_name` (promoted from Jira's `Resolution` field) is the first real
example — group `flow` by `resolution_name` in Superset and you'll see the real spread across every
extracted issue (`Fixed`, `Duplicate`, `Won't Fix`, a null bucket for genuinely unresolved issues,
...). Adding the next promoted field is exactly Path A's step 4 above: one dimension line in
`issue.yml`, one `includes` line in `flow.yml`.

Remaining gap: bridge tables (`--target bridge_table`, e.g. `analytics.fix_version`) still have no Cube
cube of their own — only the wide-table path above is wired up.

### Array-valued fields (Fix Version/s, Labels, Sprint, ...) need `--target bridge_table`

Some Jira fields can hold more than one value per issue — Fix Version/s, Labels, Sprint, Components.
Jira represents these as arrays, and a single Postgres column can't hold multiple values per row, so
`fields promote` refuses `--target column` for them:

```bash
uv run flowbi fields promote "Fix Version/s" --column fix_version
# Error: array fields must use --target bridge_table, not column
```

Use `--target bridge_table` instead — this creates a separate table with one row per issue per value
(same pattern the design uses for Labels/Sprint):

```bash
uv run flowbi fields promote "Fix Version/s" --column fix_version --target bridge_table
uv run flowbi transform --rebuild-all
```

(Also note: `--column` — the name *you* choose — is validated as a real Postgres identifier:
lowercase letters, digits and underscores only, starting with a letter. `Fix_Version` is rejected for
the capital letter; `fix_version` is fine. This does **not** apply to Jira's own field id, which the
tool resolves automatically and never asks you to type — Jira's ids are not always lowercase either,
e.g. "Fix Version/s" itself is `fixVersions` internally, and that's handled correctly.)

This has the same Cube gap as above, one level deeper: after promoting and rebuilding,
`analytics.fix_version` exists in Postgres with real data, but — same as `analytics.issue` — nothing
in `cube/model/` reads it yet. It needs its own small cube (`sql_table: analytics.fix_version`, joined
to `issues` one-to-many, same shape as `issue_status_interval.yml`) before it can show up in Superset.

### Two `status_name`-shaped fields, on purpose

The `flow` view has both `status_name` (an issue's *current* status, from the fast path) and
`interval_status_name` (whichever status a given *historical interval row* represents, from Phase 3a's
status-interval table) — don't merge these into one field if extending the model further. They answer
different questions and a status-scheme rename would silently corrupt history if they were conflated.

## Inspecting raw output

```bash
./duckdb.exe -c "SELECT * FROM 'out/jira_raw/issues/*.parquet' LIMIT 20"
```

(`duckdb.exe` is an optional CLI binary you place at the repo root — not a dependency. The `duckdb`
Python package, already installed, works the same way: `uv run python -c "import duckdb; print(duckdb.sql(...))"`.)

`fields` is a raw JSON column — pull a value out with `json_extract_string`:

```bash
./duckdb.exe -c "SELECT issue_key, json_extract_string(fields, '\$.status.name') AS status FROM 'out/jira_raw/issues/*.parquet'"
```

See `debug/queries.sql` for more ready-made queries.

## Testing

```bash
uv run pytest -q
uv run ruff check src tests
uv run mypy src
```

Some tests need a real Postgres via Docker and are excluded from the default run:

```bash
uv run pytest -m postgres
```

## Roadmap

- A visual, browser-based field-promotion screen (the `flowbi fields` CLI above already covers
  discovering, promoting, demoting and materializing fields — this would be a UI over the same thing)
- Row-level security, for multi-user access
- Cloud deployment (currently local `docker-compose` only)
