# cube/ — Cube semantic model

Mounted into the Cube container (`docker-compose.yml`'s `cube` service, `./cube:/cube/conf`).
Self-contained: it does not import from `src/`. This file is the modeling standard. It's enforced
by review and by CI: the `cube-smoke` job in `.github/workflows/ci.yml` starts Postgres and a
throwaway Cube container against `model/`, loads a fixture, and checks the `flow` view's shape.

## What's here

| File | Reads | Purpose |
|---|---|---|
| `model/cubes/issues.yml` | `jira_raw.issues` | Current issue fields, read straight from the raw JSON (`fields->...`). Declares every join |
| `model/cubes/issue_status_interval.yml` | `analytics.issue_status_interval` | One row per status period per issue: time in status, cycle time |
| `model/cubes/issue.yml` | `analytics.issue` | One row per issue with the promoted columns (`resolution_name` so far) |
| `model/cubes/fix_version.yml` | `analytics.fix_version` | Bridge table for an array field: one row per issue per fix version |
| `model/views/flow.yml` | the cubes above | The one view Superset users pick |

The `analytics` tables are built by `flowbi transform`. `issues.yml` reads `jira_raw` directly,
through `cube_reader`'s read-only grant on two raw tables (`migrations/sql/cube_reader_grants.sql`).

**No `cube.py` yet, deliberately.** `CUBEJS_DEV_MODE=true` in local dev bypasses authentication
entirely, so there's nothing for a config file to do yet. Add one, with JWKS-based auth via
`checkAuth`, before this is deployed anywhere reachable from a network. Never set
`CUBEJS_DEV_MODE=true` outside local `docker-compose`.

## Modeling rules

- Every cube has a `primary_key` dimension. Without it, a one-to-many join (for example to
  `issue_status_interval`) silently inflates `issues.count`.
- Declare joins on `issues.yml`, in the same direction as the view's `join_path`. Cube's SQL API
  can't find a join path declared the other way round when a query selects only the joined cube's
  members.
- A cube that another cube's join references needs `issue_id` and `instance_id` as real dimensions,
  not just columns folded into its primary key.
- Group by status **id**, never the display string, once a status-id source exists. The
  `issues.status_name` and `priority_name` dimensions use display strings for now, so a rename in
  Jira changes historical grouping; see their comments.
- `instance_id` stays a dimension on every cube, even with one Jira instance. It's cheap now and
  expensive to retrofit.
- Users pick a **view** (`model/views/*.yml`), never a raw cube. Views are the stable interface: a
  cube's `sql`/`sql_table` can change without breaking a saved dashboard, as long as the view's
  member names stay the same.
- Name collisions inside a view need an alias (`interval_status_name`, `fix_version_count`). Cube
  rejects a view with two members of the same name.
