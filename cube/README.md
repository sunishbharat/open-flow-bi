# cube/ — Cube semantic model

Volume-mounted into the Cube container (`docker-compose.yml`'s `cube` service, `./cube:/cube/conf`).
Self-contained — does not import from `src/`. See `docs/phase4-cube-dashboard-design.md` for the
full design; this file is the modeling standard, enforced both by review and by CI
(`.github/workflows/ci.yml`'s `cube-smoke` job, M9a.3 — starts Postgres + a throwaway Cube container
against `model/`, loads a fixture, and asserts the `flow` view's shape).

## Status

**Fast path only** (`docs/phase4-cube-dashboard-design.md` §2, milestone M9a.1). `model/cubes/issues.yml`
reads `jira_raw.issues` directly through `cube_reader`'s read-only grant on that table — no Phase 3
dependency. The full path (`analytics.issue_status_interval`-backed cycle-time/lead-time measures)
is commented out in `model/views/flow.yml`, added once Phase 3a exists.

**No `cube.py` yet, deliberately.** `CUBEJS_DEV_MODE=true` in local dev bypasses auth entirely (P4-D4
of the design doc), so there is nothing for a config file to do yet. Add it — JWKS-based auth via
`checkAuth`, per the design doc's P4-D4/D6 — before this is ever deployed anywhere with a route.
Do not set `CUBEJS_DEV_MODE=true` outside local `docker-compose`.

## Modeling rules (enforced by review, and by the `cube-smoke` CI job)

- Every cube has a `primary_key` dimension — without it, a future one-to-many join (e.g. to
  `issue_changelog`) silently inflates `issues.count`.
- Group by status **id**, never the display string, once a status-id source exists (the full-path
  cube does this correctly; the fast-path `issues.status_name` deliberately does not yet — see its
  own comment).
- `instance_id` stays a dimension on every cube, even with a single instance today — cheap now,
  expensive to retrofit if a second Jira instance is ever added.
- Users pick a **view** (`model/views/*.yml`), never a raw cube, in Superset. Views are the stable
  interface — a cube's internal `sql`/`sql_table` can change (e.g. the Phase 3a swap) without
  touching a saved dashboard, as long as the view's measure/dimension names don't move.
