# open-flow-bi

Self-hosted Jira flow metrics: full issue + changelog history extracted into Postgres, modeled in
Cube, and visualized in Superset — no SaaS, no per-seat licensing.

## How it works

```mermaid
flowchart LR
    jira["Jira<br/>Cloud · Server · DC"]

    subgraph cli["flowbi CLI"]
        direction TB
        extract["extract<br/>issues + changelog"]
        fields["fields<br/>discover · promote"]
        transform["transform"]
    end

    subgraph pg["PostgreSQL"]
        direction TB
        raw[("jira_raw<br/>issues · changelog")]
        ops[("flowbi_ops<br/>field selection · dirty queue")]
        analytics[("analytics<br/>issue · status intervals · bridge tables")]
    end

    parquet[("Parquet files<br/>out/")]
    cube["Cube<br/>semantic model · flow view"]
    superset["Superset<br/>charts · dashboards"]
    user(("You"))

    jira -- "REST API<br/>incremental" --> extract
    extract --> raw
    extract -. "marks changed issues" .-> ops
    extract -. "--destination filesystem" .-> parquet
    fields --> ops
    raw --> transform
    ops --> transform
    transform --> analytics
    analytics --> cube
    raw -- "fast path" --> cube
    cube -- "SQL API" --> superset
    superset --> user
```

| Stage | Command | What happens |
|---|---|---|
| **1. Extract** | `flowbi extract issues` / `extract changelog` | Pulls new and changed issues and their full status history from Jira into `jira_raw`. Loads are incremental and safe to re-run. Changed issues are queued for the next transform. |
| **2. Choose fields** | `flowbi fields discover` / `promote` | Measures how often each Jira field is filled in, then records which ones to turn into real columns. Every change is versioned. |
| **3. Transform** | `flowbi transform` | Rebuilds only the queued issues into `analytics`: one row per issue with your promoted columns, plus one row per status period for cycle time. Jira isn't called again. |
| **4. Model** | Cube (`cube/model/`) | Turns the tables into named dimensions and measures, exposed together as one `flow` view. |
| **5. Explore** | Superset | Build charts and dashboards in the browser without writing SQL. |

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
- Docker, for the Postgres/Cube/Superset stack, and optionally for running the pipeline itself as
  a container (see "Running with Docker")
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
| `FLOWBI_JIRA_BASE_URL` | always | e.g. `https://your-domain.atlassian.net` or `https://jira.example.com` |
| `FLOWBI_JIRA_EMAIL` + `FLOWBI_JIRA_API_TOKEN` | Jira Cloud | the Atlassian account's email + an API token — see "Connecting to your own Jira" |
| `FLOWBI_JIRA_PAT` | Jira Server/DC | personal access token |
| `FLOWBI_JIRA_DEPLOYMENT` | optional | `cloud` or `server` — skips auto-detection; set it for mTLS-protected instances |
| `FLOWBI_JIRA_CLIENT_CERT_B64` + `FLOWBI_JIRA_CLIENT_KEY_B64` | mTLS only | client certificate + private key, each a base64-encoded PEM, set together |
| `REQUESTS_CA_BUNDLE` | TLS-inspecting proxy only | path to a CA bundle (public CAs + your network's root CA) |
| `FLOWBI_JIRA_PROJECT` | optional | limit extraction to one project key, e.g. `KAFKA` |
| `FLOWBI_JIRA_INSTANCE_ID` | optional | stable name for this Jira in the database; defaults to the URL's host |
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

To point this at your own Jira, see "Connecting to your own Jira" below.

## Connecting to your own Jira

Set `FLOWBI_JIRA_BASE_URL` plus the credentials for your Jira type, then check the connection with
`uv run flowbi doctor` before extracting anything. `flowbi` detects Cloud vs Server/Data Center on
its own, and picks the matching API and authentication.

### Jira Cloud (`*.atlassian.net`)

See [Jira Cloud setup](#jira-cloud-setup) below. It covers the settings, network
requirements, verification steps and known limitations in one place.

### Jira Server / Data Center

```bash
FLOWBI_JIRA_BASE_URL=https://jira.example.com
FLOWBI_JIRA_PAT=<personal access token>     # Jira: Profile → Personal Access Tokens
FLOWBI_JIRA_EMAIL=                          # not used on Server/DC
FLOWBI_JIRA_API_TOKEN=
```

### Jira behind mutual TLS (client certificate)

Some self-hosted Jira instances also require a client certificate. Without it, every request fails with
a connection reset (`RemoteDisconnected` / `Connection aborted`) before Jira returns any response,
which looks like a firewall problem. Get a certificate (usually a `.pfx`) from your PKI team, then:

```bash
# convert the .pfx to PEM (run locally; don't commit the output files)
openssl pkcs12 -in service.pfx -clcerts -nokeys -out client-cert.pem -legacy
openssl pkcs12 -in service.pfx -nocerts -nodes  -out client-key.pem  -legacy

# base64-encode each onto a single line, for .env
base64 -w0 client-cert.pem      # -> FLOWBI_JIRA_CLIENT_CERT_B64
base64 -w0 client-key.pem       # -> FLOWBI_JIRA_CLIENT_KEY_B64
```

```bash
FLOWBI_JIRA_DEPLOYMENT=server               # or cloud; skips the unauthenticated detection call
FLOWBI_JIRA_CLIENT_CERT_B64=<base64 of client-cert.pem>
FLOWBI_JIRA_CLIENT_KEY_B64=<base64 of client-key.pem>
```

Every Jira command logs one `jira_connection` line with what's in effect: deployment type,
whether it was declared or detected, whether mTLS is configured, and which CA bundle is used. It
never logs any key material. On Cloud Foundry, that line in `cf logs` is the first thing to check.

Delete the local `.pem` files afterwards. `flowbi` writes them to a private temporary directory only
for the duration of each command. Setting only one of the two variables is an error.
`flowbi doctor` shows "mTLS client cert: configured" when both are set.

### Behind a TLS-inspecting proxy

If `flowbi doctor` fails with an SSL certificate verification error, your network is re-signing
HTTPS traffic with its own root certificate. Build a combined bundle of the public CAs plus that
root, then point `REQUESTS_CA_BUNDLE` at it:

```bash
uv run python scripts/make_ca_bundle.py --out combined-ca.pem   # public CAs + your OS's trusted roots, then a check
export REQUESTS_CA_BUNDLE=$PWD/combined-ca.pem                  # PowerShell: $env:REQUESTS_CA_BUNDLE = "$PWD\combined-ca.pem"
```

It must be the combined file: pointing it at that root alone breaks every other HTTPS host.
Antivirus TLS scanning (e.g. Norton Web/Mail Shield) does the same re-signing, and the script picks
its root up the same way. The Docker stack uses its own copy of this bundle, `.build-ca.pem` (see
"Running with Docker").

### Troubleshooting

| Symptom | Likely cause |
|---|---|
| `RemoteDisconnected` / `Connection aborted`, no HTTP status | mTLS required: set the client certificate (above), or a firewall is blocking the host |
| `SSLError` / `certificate verify failed` | TLS-inspecting proxy: set `REQUESTS_CA_BUNDLE` (above) |
| `doctor` shows "Account timezone: unavailable (check credentials)" | wrong token, or (Cloud) email and token from different accounts |
| "Jira Cloud rejected the credentials" | email and token wrong, expired, or from different accounts |
| Cloud extraction succeeds but loads 0 issues | the account can't browse that project, or `FLOWBI_JIRA_PROJECT` is wrong |
| `401` | wrong or expired token/PAT |

## Jira Cloud setup

Everything needed to point open-flow-bi at an Atlassian Cloud site (`*.atlassian.net`), in one
place. The same `openflowbi/core` Docker image runs both Cloud and Server/DC: the Jira type is chosen
at runtime from the settings below, so switching to Cloud needs no rebuild.

### 1. Create the credentials

1. Use a **dedicated service account** with browse access to the projects you need. flowbi reads
   Jira with that account's permissions, so issues it can't see are never extracted.
2. Signed in as that account, create a **classic API token** at
   https://id.atlassian.com/manage-profile/security/api-tokens. Scoped API tokens (routed through
   `api.atlassian.com`) aren't supported yet.
3. If your organization manages Atlassian accounts through SSO (Atlassian Guard), ask your Atlassian admin
   to allow API tokens for the service account. Some org policies disable them.

### 2. Settings

Set these in `.env` (locally and for `docker compose run --rm flowbi ...`), or as environment
variables on your deployment platform:

```bash
FLOWBI_JIRA_BASE_URL=https://your-site.atlassian.net
FLOWBI_JIRA_DEPLOYMENT=cloud                     # skips auto-detection
FLOWBI_JIRA_EMAIL=svc-flowbi@example.com    # must own the token below
FLOWBI_JIRA_API_TOKEN=<api token>
FLOWBI_JIRA_PAT=                                 # must be empty: clear the Server/DC placeholder
FLOWBI_JIRA_PROJECT=ABC                          # start with one project
FLOWBI_JIRA_CLIENT_CERT_B64=                     # leave empty: Atlassian Cloud doesn't use client certificates
FLOWBI_JIRA_CLIENT_KEY_B64=
```

The email and token are used together (HTTP Basic auth); a token on its own won't work.

### 3. Network requirements

| If your network has... | Configure |
|---|---|
| TLS inspection (Zscaler, a proxy, antivirus) | Run `uv run python scripts/make_ca_bundle.py` on a machine inside that network and check that every line says `OK`, including `your-site.atlassian.net`. Docker uses the resulting `.build-ca.pem`; a local run needs `REQUESTS_CA_BUNDLE` pointing at it (see [above](#behind-a-tls-inspecting-proxy)) |
| An explicit outbound HTTP proxy | Add `HTTPS_PROXY=http://<proxy-host>:<port>` and `NO_PROXY=postgres,localhost` to `.env`. Compose passes `.env` into the container |
| An Atlassian IP allowlist | Add the egress IP of the machine or platform running flowbi. Otherwise every request is rejected, even with valid credentials |
| A firewall or egress rules | Allow outbound HTTPS (443) to `*.atlassian.net` |

### 4. Verify

```bash
uv run flowbi doctor
# expect: Deployment = Cloud, an account timezone, and "mTLS client cert: not configured"
uv run flowbi extract issues    --destination postgres --limit 20
uv run flowbi extract changelog --destination postgres --limit 20
```

With Docker, prefix each command with `docker compose run --rm flowbi` (for example
`docker compose run --rm flowbi flowbi doctor`).

Check that the changelog came through the fast path:

```sql
SELECT source, count(*) FROM jira_raw.issue_changelog GROUP BY source;
-- mostly 'expand' is expected; 'bulkfetch' and 'per_issue' are slower fallbacks
```

Only then move to `--limit 0`, one project at a time.

### 5. Things to know about Cloud

- **Rate limits are shared across the whole Atlassian site**, roughly 65,000 points an hour for every
  integration together. flowbi doesn't yet slow itself down as that budget runs low, so a large
  backfill can crowd out other tools on the same site. Extract one project at a time, use `--limit`,
  and run large first loads outside working hours.
- **Wrong credentials stop the run.** Cloud can answer a search made with bad credentials with an
  empty result instead of a `401`. flowbi checks the account first (`GET /myself`) and stops with
  "Jira Cloud rejected the credentials..." rather than loading nothing.
- **Search results take seconds to minutes to appear.** Cloud's search index is eventually
  consistent. Each incremental run re-reads the last hour to catch late arrivals, so nothing is
  missed.
- **Changelog authors are empty on Cloud.** Atlassian removed the field flowbi currently reads for
  privacy reasons. Status history and cycle times are unaffected.
- **Changelogs come from a three-step fallback.** flowbi first asks for them with the search, then
  uses Cloud's bulk changelog endpoint (still experimental at Atlassian), then fetches one issue at a
  time. Each step is tried automatically if the previous one isn't available.

### Cloud troubleshooting

| Symptom | Likely cause |
|---|---|
| "Jira Cloud rejected the credentials" | email and token wrong, expired, from different accounts, or API tokens disabled by your org's policy |
| `doctor` shows "Account timezone: unavailable" | same as above |
| Extraction succeeds but loads 0 issues | the service account can't browse the project, or `FLOWBI_JIRA_PROJECT` is wrong |
| `certificate verify failed` | TLS inspection: regenerate the CA bundle (step 3) |
| Connection timeout or refused | firewall, proxy (`HTTPS_PROXY`) or Atlassian IP allowlist (step 3) |
| `429 Too Many Requests` | the site-wide rate limit is exhausted: wait, then continue with a smaller `--limit` |

## CLI

```bash
uv run flowbi --help
uv run flowbi doctor                            # deployment type, connectivity, credentials, mTLS
uv run flowbi extract fields --sink table       # preview available fields
uv run flowbi extract issues --limit N          # extract issues
uv run flowbi extract changelog --limit N       # extract full changelog history
uv run flowbi extract changelog --reset-watermark   # re-walk all changelogs from the start (backfill)
uv run flowbi quality check <table>             # validate extracted data
uv run flowbi fields discover [--project X]     # refresh field fill-rate stats (needs Postgres)
uv run flowbi fields list [--min-fill 0.1]      # show fields sorted by fill rate
uv run flowbi transform [--rebuild-all] [--project X]   # materialize promoted columns + status intervals
uv run flowbi fields request-rebuild [--project X]      # queue a rebuild for `flowbi transform` to drain
```

`--limit` bounds every extract command — useful while testing before a full run. It defaults
to 20; `--limit 0` removes the bound, so a full walk always has to be asked for.

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
FLOWBI_JIRA_PROJECT=KAFKA uv run flowbi extract issues --limit 0 --destination postgres
FLOWBI_JIRA_PROJECT=KAFKA uv run flowbi extract changelog --limit 0 --destination postgres
# repeat for HIVE, HADOOP, ZOOKEEPER, ...
```

```powershell
# PowerShell
$env:FLOWBI_JIRA_PROJECT = "KAFKA"
uv run flowbi extract issues --limit 0 --destination postgres
uv run flowbi extract changelog --limit 0 --destination postgres
# repeat for HIVE, HADOOP, ZOOKEEPER, ...
```

`--limit 0` walks the whole project; without it, each command stops at the CLI default of 20.
`extract changelog` is the slow part (per-issue changelog
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

Once you've found a field worth having, select it. This records the decision and queues a full
rebuild of it for the next `flowbi transform`; it doesn't touch Jira or rebuild anything by itself:

```bash
uv run flowbi fields promote "Story Points" --column story_points   # add --schema-type if the name is ambiguous
uv run flowbi fields demote "Story Points"                          # never deletes anything, just marks it
uv run flowbi fields export > config/fields.yml                     # the current selection as YAML
uv run flowbi fields import config/fields.yml                       # re-import it (always writes a new version)
```

Every save writes a brand-new version rather than editing in place, so `field_selection` doubles as
a full audit trail of who decided what, when. Column names that already exist in `analytics.issue`
(`issue_key`, `created_at`, ...) or name an existing `analytics` table (`issue`,
`issue_status_interval`) are rejected.

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
once. `promote` and `import` queue a full rebuild of the new selection themselves, so the next
`flowbi transform` (incremental or not) fills a newly promoted column for every issue, not just the
dirty ones. To queue one by hand — e.g. from a script — use `flowbi fields request-rebuild
[--project X]`; the next `flowbi transform` call drains the queue.

`flowbi transform` only rebuilds an issue's status intervals once its changelog has been recorded as
fully extracted, and reports the rest as "skipped (incomplete changelog)". Changelogs extracted before
that record existed count as incomplete, so their issues keep their previous intervals. To backfill,
re-walk the changelog from the start:

```bash
uv run flowbi extract changelog --destination postgres --limit 0 --reset-watermark
uv run flowbi transform     # re-extracted issues are marked dirty, so incremental is enough
```

On Server/DC this fetches every issue's changelog again, which is slow. To spread it out, run the
first command with e.g. `--limit 1000`, then repeat it **without** `--reset-watermark` until the
skipped count reaches zero. Only the first run should reset.

## Dashboard: Cube + Superset

A browser-based dashboard for building charts against the extracted data — no SQL required.

**One-time setup**, once you have Postgres data (see above):

1. **Promote the two fields the Cube model reads.** On a fresh database, every chart otherwise fails
   with `relation "analytics.fix_version" does not exist`:

   ```bash
   uv run flowbi fields discover
   uv run flowbi fields promote "Resolution" --column resolution_name --schema-type resolution
   uv run flowbi fields promote "Fix Version/s" --column fix_version --target bridge_table
   uv run flowbi transform
   ```

2. **Create the database roles, then start Cube and Superset.** The reader password must be `.env`'s
   `CUBE_READER_PW`, so it's read from there:

   ```bash
   reader=$(grep '^CUBE_READER_PW=' .env | cut -d= -f2- | tr -d '\r')
   docker exec -i open-flow-bi-postgres-1 psql -U flowbi -d openflowbi \
     -v writer_pw=flowbi_writer_local -v reader_pw="$reader" -f - < migrations/sql/roles.sql
   docker exec -i open-flow-bi-postgres-1 psql -U flowbi -d openflowbi \
     -f - < migrations/sql/cube_reader_grants.sql
   docker compose up -d cube superset
   ```

The scripts are piped in on stdin (`-f -`) because they live on your machine, not inside the
Postgres container. PowerShell has no `<`; see step 5 of "Running with Docker" below for the
PowerShell form.

`roles.sql` is safe to re-run. It makes `flowbi_writer` the owner of `jira_raw`, `flowbi_ops` and
`analytics`, and of every table in them, so the pipeline can run as `flowbi_writer` instead of the
bootstrap superuser. To do that, point `FLOWBI_POSTGRES_DSN` at `flowbi_writer` for `alembic`,
`flowbi extract` and `flowbi transform` alike. If you keep running any of them as the superuser,
re-run `roles.sql` afterwards to hand the new tables over.

**Build a chart**, at http://localhost:8088 (first login: `admin` / your `.env`'s `SUPERSET_ADMIN_PW`):

1. **Settings → Database Connections → + Database → PostgreSQL**: host `cube`, port `15432`,
   database `db`, and `.env`'s `CUBE_SQL_USER` / `CUBE_SQL_PASSWORD` as username and password. Step 5
   of "Running with Docker" below has the details.
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

## Running with Docker

The pipeline also ships as a container image, `openflowbi/core`, the same image a Cloud Foundry
deployment would run. It holds only the runtime: every package is installed from a prebuilt wheel
(nothing is compiled), there are no dev tools, it runs as a non-root user, and it has no entrypoint,
so every run names its own command. The `flowbi` service in `docker-compose.yml` runs it against the
compose Postgres: `docker compose run --rm flowbi <command>`. It's behind a `tools` profile, so a
plain `docker compose up` never starts it.

This walkthrough starts from nothing (no images, no volumes) and ends with a bar chart in Superset.
It needs Docker and a filled-in `.env` (see "Configuration"; the `.env.example` defaults work as-is
against Apache's public Jira). Commands are the same in bash and PowerShell unless shown separately.

### 1. Create the CA bundle (once)

The stack needs a CA bundle at `.build-ca.pem` in the repo root. It's used three times:
- during the image build, so `uv` can download wheels;
- when `flowbi` runs, so it can reach Jira over HTTPS;
- when Superset first starts, so `pip` can install its Postgres driver.

It's passed to the build as a secret and mounted read-only into the containers, never copied into an
image. The file is gitignored and excluded from the build context.

Create it with:

```bash
uv run python scripts/make_ca_bundle.py
```

This writes the public CAs plus every root certificate your operating system already trusts
(Windows certificate store, macOS keychains, or the Linux system bundle). Anything that re-signs
HTTPS on your network, such as a proxy, Zscaler or antivirus TLS scanning, installs its
root there, so you don't need to know which tool it is or where its certificate lives. Without one,
the extra roots do no harm.

The script then checks the file the way a container will, over HTTPS to pypi.org and your Jira host
(from `.env`):

```
wrote .build-ca.pem: 121 public CAs + 41 from this machine's trust store
  pypi.org: OK (certificate issued by Norton Web/Mail Shield)
  issues.apache.org: OK (certificate issued by Norton Web/Mail Shield)
```

`OK` on every line means you're done. `FAILED` means something re-signs that traffic with a root
this machine doesn't trust either. Get that root certificate from your IT team, append it to the
file, and run the check again with `--check-host <host>`. Re-run the script whenever your proxy,
antivirus or network root certificate changes.

To keep the bundle somewhere else, write it with `--out <path>` and set `FLOWBI_BUILD_CA_BUNDLE` to
that path.

### 2. Build the image

```bash
docker compose --profile tools build flowbi
```

The equivalent without compose:
`docker build --platform linux/amd64 --secret id=ca,src=.build-ca.pem -t openflowbi/core:dev .`

The build fails, rather than compiling anything, if a dependency has no prebuilt wheel.

### 3. Smoke-test the image

```bash
docker images openflowbi/core:dev                                    # size
docker run --rm openflowbi/core:dev                                  # prints `flowbi --help`
docker run --rm openflowbi/core:dev python -c "import pandera.pyarrow, pyarrow, psycopg2; print('ok')"
```

### 4. Load data into Postgres

```bash
docker compose up -d postgres
docker compose run --rm flowbi flowbi doctor
docker compose run --rm flowbi alembic upgrade head
docker compose run --rm flowbi flowbi extract issues    --destination postgres --limit 50
docker compose run --rm flowbi flowbi extract changelog --destination postgres --limit 50
```

- `doctor` should show the deployment and version. Against the anonymous Apache default,
  "Account timezone: unavailable" is expected.
- Use `--limit 0` instead of `--limit 50` to extract a whole project.
- Settings come from `.env`, except `FLOWBI_POSTGRES_DSN`: inside compose, Postgres is
  `postgres:5432`, not `localhost:5433`, and the service sets that itself.
- State is kept in named volumes: `pgdata` (the database) and `flowbi_dlt` (dlt's incremental
  watermarks). `./out` is mounted for `--destination filesystem` runs.

The Cube model reads two promoted fields that a fresh database doesn't have yet: `resolution_name`,
a column, and `analytics.fix_version`, a bridge table. Without them every chart fails with
`relation "analytics.fix_version" does not exist`. Discover the fields, promote both, then build the
`analytics` tables:

```bash
docker compose run --rm flowbi flowbi fields discover
docker compose run --rm flowbi flowbi fields list --min-fill 0.1          # optional: see what was found
docker compose run --rm flowbi flowbi fields promote "Resolution" --column resolution_name --schema-type resolution
docker compose run --rm flowbi flowbi fields promote "Fix Version/s" --column fix_version --target bridge_table
docker compose run --rm flowbi flowbi transform
docker compose run --rm flowbi flowbi quality check issue_changelog --destination postgres
```

`transform` should report rows written to `analytics.issue` and the bridge table, and on a fresh
database no "skipped (incomplete changelog)". Issues that `extract issues` loaded but
`extract changelog` didn't reach are skipped, because the two commands walk separately; use the same
`--limit` on both, or `--limit 0`.

Check the tables:

```bash
docker exec -it open-flow-bi-postgres-1 psql -U flowbi -d openflowbi -c "\dt jira_raw.*" -c "\dt analytics.*"
docker exec -it open-flow-bi-postgres-1 psql -U flowbi -d openflowbi -c "SELECT fields->'status'->>'name' AS status, count(*) FROM jira_raw.issues GROUP BY 1 ORDER BY 2 DESC"
```

Remember the second query's counts; the chart in step 5 should show the same numbers.

### 5. Start the dashboard and build a bar chart

Create the database roles Cube reads with. The reader password is read from `.env`, so it always
matches the `CUBE_READER_PW` that Cube connects with. Pick any writer password, and reuse it on
re-runs:

```bash
# bash
reader=$(grep '^CUBE_READER_PW=' .env | cut -d= -f2- | tr -d '\r')
docker exec -i open-flow-bi-postgres-1 psql -U flowbi -d openflowbi \
  -v writer_pw=flowbi_writer_local -v reader_pw="$reader" -f - < migrations/sql/roles.sql
docker exec -i open-flow-bi-postgres-1 psql -U flowbi -d openflowbi -f - < migrations/sql/cube_reader_grants.sql
```

```powershell
# PowerShell
$reader = (Select-String '^CUBE_READER_PW=(.*)' .env).Matches[0].Groups[1].Value
Get-Content migrations/sql/roles.sql -Raw | docker exec -i open-flow-bi-postgres-1 psql -U flowbi -d openflowbi -v writer_pw=flowbi_writer_local -v reader_pw=$reader -f -
Get-Content migrations/sql/cube_reader_grants.sql -Raw | docker exec -i open-flow-bi-postgres-1 psql -U flowbi -d openflowbi -f -
```

Both scripts should finish without `ERROR`, and both are safe to re-run. The `flowbi` service
connects as the bootstrap superuser, so re-run them whenever a run creates new tables, such as after
promoting another field into a bridge table, then `docker compose restart cube`. Otherwise Cube
can't read the new tables.

```bash
docker compose up -d cube superset
docker compose logs -f superset      # wait for the server to start, then Ctrl+C (Superset keeps running)
```

Superset's first start takes a few minutes: it installs its Postgres driver (through
`.build-ca.pem`) and sets up its own metadata. Then go to http://localhost:8088 and log in as `admin`
with your `SUPERSET_ADMIN_PW`:

1. **Settings → Database Connections → + Database → PostgreSQL.** Fill in the form:

   | Field | Value |
   |---|---|
   | Host | `cube` (not `localhost`: Superset reaches Cube over the compose network) |
   | Port | `15432` |
   | Database name | `db` |
   | Username | `CUBE_SQL_USER` from `.env` (`superset` by default) |
   | Password | `CUBE_SQL_PASSWORD` from `.env` |

   Or click "Connect this database with a SQLAlchemy URI string instead" and enter
   `postgresql://superset:YOUR_CUBE_SQL_PASSWORD@cube:15432/db` with the real values (URL-encode
   `@ : / #` in the password). **Test Connection**, then **Connect**.
2. **Datasets → + Dataset**: that database, schema `public`, table `flow`.
3. **Charts → + Chart → Bar Chart**: X-axis `status_name`, metric `count` (aggregate MAX; `count` is
   already a Cube measure). **Update chart**: the bars should match step 4's counts.
4. **Save**, adding it to a new dashboard. Reload the page to confirm it persisted.

If you created the `flow` dataset before a promotion, re-sync it: **Datasets → `flow` → Edit →
Columns → Sync columns from source**. For the cycle-time and resolution charts, see the examples
under "Dashboard: Cube + Superset" above.

### Starting over

```bash
docker compose --profile tools down -v      # removes the containers and every volume of this stack
```

This deletes the database, dlt's watermarks and Superset's saved charts. Delete `pgdata` and
`flowbi_dlt` together or not at all: an empty database with old watermarks makes `extract` skip
every issue older than them.

### Troubleshooting Docker

| Symptom | Fix |
|---|---|
| `failed to stat ...\.build-ca.pem` | create it (step 1) |
| `SSL: CERTIFICATE_VERIFY_FAILED` during the build or from `doctor` | `.build-ca.pem` is missing your proxy's root certificate: re-run `uv run python scripts/make_ca_bundle.py` (step 1) and check that every line says `OK`. It's mounted at runtime, so `doctor` needs no rebuild; a failed build does |
| PowerShell: `The '<' operator is reserved for future use` | a bash command was pasted into PowerShell; use the PowerShell variant (pipe with `Get-Content ... -Raw \|`) |
| `fields list` says "No fields found" | run `flowbi fields discover` first (step 4) |
| `PermissionError: ... '/home/flowbi/.dlt/pipelines'` | a `flowbi_dlt` volume created by an older image: `docker volume rm open-flow-bi_flowbi_dlt`, rebuild, retry |
| `dependency failed to start: container ... postgres-1 is unhealthy` | `docker compose logs postgres`; a first start initializes the database, so run the command again |
| Chart: `relation "analytics.fix_version" does not exist` or `column "resolution_name" does not exist` | the promotions at the end of step 4 haven't run; run them, re-run step 5's two scripts, then `docker compose restart cube` |
| `transform` reports many "skipped (incomplete changelog)" | see `--reset-watermark` under "Field discovery and materialization" |
| Superset: `The password provided for username "" is incorrect` | the Username field was left blank, or the URI still contains `<...>` placeholders; enter the real `CUBE_SQL_USER`/`CUBE_SQL_PASSWORD` (step 5) |
| Superset Test Connection fails | check `CUBE_SQL_USER`/`CUBE_SQL_PASSWORD` in `.env`, then `docker compose logs cube` |
| Chart: `password authentication failed for user "cube_reader"` | `roles.sql` ran with a `reader_pw` other than `.env`'s `CUBE_READER_PW`. Re-run it with the right value, then `docker compose restart cube`. To test, connect over the network: `psql -h postgres -U cube_reader` inside the Postgres container. A plain `psql` there is trusted without a password, so it proves nothing |
| Superset logs `CERTIFICATE_VERIFY_FAILED` installing `psycopg2-binary` | Superset's start-up `pip install` also uses `.build-ca.pem`: re-run the script (step 1), then `docker compose up -d --force-recreate superset` |

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
2. `uv run flowbi fields promote "Field Name" --column your_column_name` — records the decision and
   queues its rebuild.
3. `uv run flowbi transform` — materializes `your_column_name` as a real column in
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

Bridge tables now have a cube too (`cube/model/cubes/fix_version.yml`) — see the "Array-valued fields"
subsection below.

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

`analytics.fix_version` is wired into Cube (`cube/model/cubes/fix_version.yml`, joined to `issues`
one-to-many, same shape as `issue_status_interval.yml`) and exposed in the `flow` view as
`fix_version_name`/`fix_version_count` — aliased because this cube's own `count` measure counts
(issue, fix-version) pairs, not issues (an issue with 3 fix versions counts 3 times), which is a
different meaning from the view's top-level `count`. Live-verified: `flow.fix_version_count` grouped
by `flow.fix_version_name` matches `analytics.fix_version`'s real distribution exactly (`3.6.0` → 725,
`0.9.0.0` → 500, `3.5.0` → 446, ...). If you promote a *different* array field to its own bridge
table, it needs the same small cube built fresh — `fix_version.yml` is the reference to copy, the
same way `issue.yml`'s `resolution_name` is the reference for wide-table promoted columns.

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
Python package works the same way: `uv run python -c "import duckdb; print(duckdb.sql(...))"`. It's
a dev dependency, installed by `uv sync` but left out of the Docker image.)

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
- Cloud deployment. The application image exists (see "Running with Docker"); deployment manifests
  don't yet, and the stack runs locally under `docker-compose` only
