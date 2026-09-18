# open-flow-bi

Self hosted flow metrics for jira with full changelog, history, flow metrics modelled in Cube.

Right now only the **extractor** is built: a `flowbi` CLI that pulls Jira issues and their full
changelog (Cloud or Server/DC) into Parquet files via [dlt](https://github.com/dlt-hub/dlt). See
`CLAUDE.md` for the working rules and `docs/library-decision-register.md` for the architecture.
Current build status: `docs/session-status-2026-09-18.md`.

## Setup

```bash
uv sync
cp .env.example .env   # fill in FLOWBI_JIRA_BASE_URL and either Cloud or Server/DC auth
```

`.env` fields:

| Var | Required for | Notes |
|---|---|---|
| `FLOWBI_JIRA_BASE_URL` | always | e.g. `https://your-domain.atlassian.net` or `https://jira.your-company.com` |
| `FLOWBI_JIRA_EMAIL` + `FLOWBI_JIRA_API_TOKEN` | Cloud | |
| `FLOWBI_JIRA_PAT` | Server/DC | personal access token |
| `FLOWBI_JIRA_PROJECT` | optional | scopes extraction to one project key, e.g. `KAFKA` |
| `FLOWBI_JIRA_INCREMENTAL_START` | optional | ISO datetime floor for `extract issues`' first run (default: epoch, i.e. everything) |

## CLI

```bash
uv run flowbi --help
uv run flowbi doctor                          # detect deployment, account timezone, rate-limit budget
uv run flowbi extract fields --sink table      # preview field (name, schema type) -> id map
uv run flowbi extract issues --limit 20        # issues -> out/jira_raw/issues/*.parquet
uv run flowbi extract changelog --limit 20     # changelog (3-tier) -> out/jira_raw/issue_changelog/*.parquet
```

`--limit` bounds every extract command — a debug run never walks a whole project by accident.

`extract issues` is incremental: each successful run advances a watermark (dlt pipeline state,
keyed on `updated_at`) and the next run only re-fetches issues updated since then. A
`--limit`-truncated run only ever advances the watermark to the oldest-updated issue it actually
fetched, so a later unlimited run still picks up whatever the limit cut off — see
`docs/session-status-2026-09-18.md`'s M5 section for the verified kill/resume behaviour.

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

Tests replay cassettes under `tests/cassettes/{cloud,dc}/` — both trees must pass. Recording a new
cassette needs real network access (`--record-mode=once`); see `docs/session-status-2026-09-18.md`
if you hit TLS/proxy issues recording against a live Jira instance.

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
p = dlt.attach(pipeline_name='openflowbi')
print(p.state['sources']['jira']['resources']['issues']['incremental']['updated_at'])
"
```

To watch the kill/resume behaviour live rather than via the mocked test, interrupt a real
`extract issues` run (Ctrl-C, same env vars as above) mid-run before it prints a result, then check
the watermark above hasn't moved from its pre-run value and `out/jira_raw/issues/*.parquet` has no
new file — then re-run and confirm it fetches from that same unmoved watermark.
