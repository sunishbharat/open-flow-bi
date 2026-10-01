#!/usr/bin/env bash
# Local quickstart: from a fresh clone to a running dashboard, asking before each step.
#
# Automates README "Running with Docker": writes .env, creates the CA bundle, gets the
# openflowbi/core image (uses one already here, downloads a prebuilt one, or builds it),
# starts Postgres, loads Jira data, promotes the two fields the Cube model reads, applies the
# database roles, then starts Cube and Superset.
#
# Run from bash (Git Bash on Windows, macOS, Linux, WSL):
#   ./scripts/quickstart.sh              # asks before each step
#   ./scripts/quickstart.sh --limit 0    # extract the whole project (default: 50 issues)
#   ./scripts/quickstart.sh --image ghcr.io/<owner>/openflowbi-core:<version>
#                                        # download this prebuilt image instead of building
#   ./scripts/quickstart.sh --yes        # accept every default, no questions
#
# Safe to re-run: existing .env values are never overwritten, and every step either skips
# what's already done or repeats it harmlessly (extracts are incremental).

set -euo pipefail

YES=0
LIMIT=50
STEP=0
TOTAL=8
# The name docker-compose.yml's flowbi service runs. A downloaded image is tagged with it.
IMAGE=openflowbi/core:dev
IMAGE_SOURCE=""

usage() {
    sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --yes | -y) YES=1 ;;
        --limit)
            [[ $# -ge 2 && $2 =~ ^[0-9]+$ ]] || { echo "--limit needs a number (0 = everything)" >&2; exit 2; }
            LIMIT=$2
            shift
            ;;
        --image)
            [[ $# -ge 2 && -n $2 ]] || { echo "--image needs an image reference" >&2; exit 2; }
            IMAGE_SOURCE=$2
            shift
            ;;
        --help | -h) usage ;;
        *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
    esac
    shift
done

cd "$(dirname "$0")/.."
# Git Bash rewrites absolute container paths (/etc/..., /certs/...) into Windows paths.
export MSYS_NO_PATHCONV=1

# --- output and prompts ------------------------------------------------------------------

say()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32m%s\033[0m\n' "$*"; }
warn() { printf '    \033[33m%s\033[0m\n' "$*"; }
die()  { printf '\n\033[31mStopped at step %s: %s\033[0m\n' "$STEP" "$*" >&2; exit 1; }
run()  { printf '    $ %s\n' "$*"; "$@"; }

# Runs a command in the openflowbi/core image against the compose Postgres. -T: no TTY,
# which Git Bash's terminal can't hand to docker.
fb() { run docker compose run --rm -T flowbi "$@"; }

step() {
    STEP=$1
    say "Step $1/$TOTAL: $2"
}

# Ask before a step; "n" stops the script (re-running resumes, nothing is lost).
confirm() {
    [[ $YES == 1 ]] && return 0
    local reply
    read -r -p "    Continue? [Y/n] " reply
    case "$reply" in
        [nN]*) echo "    Stopped. Run the script again to continue from here."; exit 0 ;;
    esac
}

# yesno "question" default(y|n) -> exit status 0 for yes
yesno() {
    local reply default=$2
    if [[ $YES == 1 ]]; then reply=$default; else
        read -r -p "    $1 [$([[ $default == y ]] && echo Y/n || echo y/N)] " reply
        reply=${reply:-$default}
    fi
    [[ $reply == [yY]* ]]
}

# ask VAR "question" default
ask() {
    local reply
    if [[ $YES == 1 ]]; then reply=$3; else
        read -r -p "    $2 [$3]: " reply
        reply=${reply:-$3}
    fi
    printf -v "$1" '%s' "$reply"
}

# ask_secret VAR "question": hidden input, never echoed or logged
ask_secret() {
    local reply=""
    while [[ -z $reply ]]; do
        read -r -s -p "    $2: " reply
        echo
    done
    printf -v "$1" '%s' "$reply"
}

# --- .env helpers --------------------------------------------------------------------------

env_get() {
    { grep -E "^$1=" .env 2>/dev/null || true; } | tail -n 1 | cut -d= -f2- | tr -d '\r'
}

# Replace KEY's line, or append it. Values go through the environment, not awk -v, so
# backslashes in a token survive unchanged.
env_set() {
    if grep -qE "^$1=" .env; then
        K=$1 V=$2 awk -F= '$1 == ENVIRON["K"] { print ENVIRON["K"] "=" ENVIRON["V"]; next } { print }' \
            .env > .env.tmp
        mv .env.tmp .env
    else
        printf '%s=%s\n' "$1" "$2" >> .env
    fi
}

env_set_if_blank() {
    if [[ -z $(env_get "$1") ]]; then
        env_set "$1" "$2"
        ok "$1: generated"
    fi
}

# Hex only, so a generated password is safe in a URL, a psql -v value and .env alike.
gen_secret() { head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; }

# =========================================================================================

step 1 "Check prerequisites"
command -v docker > /dev/null || die "docker not found. Install Docker Desktop (or Docker Engine) first."
docker info > /dev/null 2>&1 || die "Docker isn't running. Start Docker Desktop, then run this again."
docker compose version > /dev/null 2>&1 || die "'docker compose' (Compose v2) not found."
command -v curl > /dev/null || die "curl not found; it's needed to check that Superset is up."
ok "docker, docker compose and curl found; Docker is running"

# -----------------------------------------------------------------------------------------
step 2 "Configure .env (Jira connection and generated passwords)"
echo "    Asks for your Jira details; passwords for Postgres roles, Cube and Superset are"
echo "    generated. Existing values in .env are kept."
confirm

if [[ ! -f .env ]]; then
    run cp .env.example .env
fi

# set_token KEY "question": keep the current value if the user wants to, else ask (hidden).
set_token() {
    if [[ -n $(env_get "$1") ]] && yesno "Keep the $1 already in .env?" y; then
        return
    fi
    local value
    ask_secret value "$2"
    env_set "$1" "$value"
}

url="" project="" jira_type="" email="" # set by ask(), through printf -v
if [[ $YES == 0 ]] && ! yesno "Keep the Jira settings already in .env ($(env_get FLOWBI_JIRA_BASE_URL))?" y; then
    ask url "Jira base URL" "$(env_get FLOWBI_JIRA_BASE_URL)"
    env_set FLOWBI_JIRA_BASE_URL "${url%/}"
    ask project "Project key (blank = all projects the account can see)" "$(env_get FLOWBI_JIRA_PROJECT)"
    env_set FLOWBI_JIRA_PROJECT "$project"

    default_type=server
    [[ $url == *atlassian.net* ]] && default_type=cloud
    ask jira_type "Jira type: cloud or server" "$default_type"
    case "$jira_type" in
        cloud)
            ask email "Atlassian account email" "$(env_get FLOWBI_JIRA_EMAIL)"
            env_set FLOWBI_JIRA_EMAIL "$email"
            set_token FLOWBI_JIRA_API_TOKEN "API token (input hidden)"
            env_set FLOWBI_JIRA_DEPLOYMENT cloud
            ;;
        server)
            echo "    For Apache's public Jira (no account), the token is: dummy-anonymous"
            set_token FLOWBI_JIRA_PAT "Personal access token (input hidden)"
            env_set FLOWBI_JIRA_DEPLOYMENT server
            ;;
        *) die "Jira type must be 'cloud' or 'server', not '$jira_type'." ;;
    esac
    ok "Jira settings written to .env"
fi

env_set_if_blank CUBE_READER_PW "$(gen_secret 16)"
env_set_if_blank CUBE_SQL_USER superset
env_set_if_blank CUBE_SQL_PASSWORD "$(gen_secret 16)"
env_set_if_blank SUPERSET_SECRET_KEY "$(gen_secret 32)"
env_set_if_blank SUPERSET_ADMIN_PW "$(gen_secret 12)"
env_set_if_blank FLOWBI_WRITER_PW "$(gen_secret 16)"
ok ".env is complete"

# -----------------------------------------------------------------------------------------
step 3 "CA bundle for HTTPS (.build-ca.pem)"
CA_FILE=${FLOWBI_BUILD_CA_BUNDLE:-.build-ca.pem}
if [[ -s $CA_FILE ]]; then
    ok "$CA_FILE exists. If HTTPS errors appear later, delete it and run this again."
else
    echo "    The image build and the containers trust the certificates in this file. It also"
    echo "    picks up roots added by antivirus HTTPS scanning or a corporate proxy."
    confirm
    if command -v uv > /dev/null; then
        run uv run python scripts/make_ca_bundle.py --out "$CA_FILE" \
            || die "the bundle check failed (see the FAILED line above, and README 'Running with Docker' step 1)."
    else
        warn "uv not found: using the public CAs from the Python base image instead. That works"
        warn "unless HTTPS on this machine is inspected by antivirus or a proxy; then install uv"
        warn "and delete $CA_FILE before running this again."
        printf '    $ docker run --rm python:3.12-slim-bookworm cat /etc/ssl/certs/ca-certificates.crt > %s\n' "$CA_FILE"
        docker run --rm python:3.12-slim-bookworm cat /etc/ssl/certs/ca-certificates.crt > "$CA_FILE" \
            || die "could not read the CA certificates from the Python image."
    fi
    ok "wrote $CA_FILE"
fi

# -----------------------------------------------------------------------------------------
step 4 "Get the $IMAGE image (use, download or build)"

# Download REF (unless it's already on this machine) and tag it as $IMAGE, the name the
# compose flowbi service runs. Explicit `|| return 1`s: errexit is off inside an `if`.
pull_and_link() {
    if ! docker image inspect "$1" > /dev/null 2>&1; then
        run docker pull "$1" || return 1
    fi
    if [[ $1 != "$IMAGE" ]]; then
        run docker tag "$1" "$IMAGE" || return 1
    fi
    docker run --rm "$IMAGE" flowbi version > /dev/null 2>&1 \
        || die "$1 doesn't run 'flowbi'; it isn't an openflowbi/core image."
    ok "using $1 as $IMAGE"
}

build_image() {
    echo "    A few minutes the first time. Every package installs from a prebuilt wheel."
    run docker compose --profile tools build flowbi || die "the image build failed (output above)."
}

# Try REF; if it can't be downloaded, offer to build instead.
pull_or_offer_build() {
    if pull_and_link "$1"; then
        env_set FLOWBI_IMAGE_SOURCE "$1"
        return
    fi
    warn "Couldn't download $1. Check the name and tag; a private ghcr.io image needs"
    warn "'docker login ghcr.io' first."
    if yesno "Build the image from source instead?" y; then
        build_image
    else
        die "flowbi needs the $IMAGE image: run this again to download or build it."
    fi
}

if [[ -n $IMAGE_SOURCE ]]; then
    # --image given: the user asked for exactly this image.
    pull_or_offer_build "$IMAGE_SOURCE"
elif docker image inspect "$IMAGE" > /dev/null 2>&1 \
    && yesno "$IMAGE is already on this machine. Use it? (n: download or build a newer one)" y; then
    ok "using the existing $IMAGE"
else
    # Offered first: the image remembered from a previous download, if any.
    known=$(env_get FLOWBI_IMAGE_SOURCE)
    ref=""
    if yesno "Is there a prebuilt image to download (e.g. ghcr.io/<owner>/openflowbi-core:<version>)?" \
        "$([[ -n $known ]] && echo y || echo n)"; then
        ask ref "Image to download" "$known"
        [[ -n $ref ]] || die "no image given."
        pull_or_offer_build "$ref"
    elif yesno "Build the image from source now?" y; then
        build_image
    else
        die "flowbi needs the $IMAGE image: run this again to download or build it."
    fi
fi

# -----------------------------------------------------------------------------------------
step 5 "Start Postgres, check the Jira connection, create the schema"
confirm
run docker compose up -d --wait postgres || die "Postgres didn't become healthy: docker compose logs postgres"
fb flowbi doctor || die "flowbi couldn't reach Jira. Check the URL and credentials in .env (step 2).
    A CERTIFICATE_VERIFY_FAILED error means HTTPS here is re-signed: delete $CA_FILE,
    install uv and run this again so the bundle includes this machine's roots."
fb alembic upgrade head || die "the database migration failed (output above)."

# -----------------------------------------------------------------------------------------
step 6 "Load Jira data into Postgres"
if [[ $YES == 0 ]]; then
    ask LIMIT "How many issues to extract (0 = all of them; a large project can take hours)" "$LIMIT"
    [[ $LIMIT =~ ^[0-9]+$ ]] || die "the issue count must be a number."
fi
echo "    Extracts up to $LIMIT issues and their changelogs ($([[ $LIMIT == 0 ]] && echo 'no limit' || echo "limit $LIMIT")),"
echo "    promotes the Resolution and Fix Version fields the dashboard reads, then builds the"
echo "    analytics tables. Re-running continues where the last run stopped."
confirm

fb flowbi extract issues --destination postgres --limit "$LIMIT" || die "extracting issues failed."
fb flowbi extract changelog --destination postgres --limit "$LIMIT" || die "extracting changelogs failed."
fb flowbi fields discover || die "field discovery failed."

# True when COLUMN is already promoted (and not demoted) in the current field selection.
promoted() {
    docker compose run --rm -T flowbi flowbi fields export 2> /dev/null | tr -d '\r' \
        | grep -A2 "column_name: $1\$" | grep -q 'deprecated: false'
}

if promoted resolution_name; then
    ok "Resolution is already promoted"
else
    fb flowbi fields promote "Resolution" --column resolution_name --schema-type resolution \
        || die "promoting Resolution failed."
fi

if promoted fix_version; then
    ok "Fix Version is already promoted"
# The field is "Fix Version/s" on Server/DC and "Fix versions" on Cloud.
elif ! fb flowbi fields promote "Fix Version/s" --column fix_version --target bridge_table; then
    warn "No 'Fix Version/s' field; trying the Cloud name, 'Fix versions'."
    fb flowbi fields promote "Fix versions" --column fix_version --target bridge_table \
        || die "promoting the fix-version field failed."
fi

fb flowbi transform || die "building the analytics tables failed."

# -----------------------------------------------------------------------------------------
step 7 "Database roles, then start Cube and Superset"
echo "    Creates the flowbi_writer and cube_reader roles with the passwords from .env, grants"
echo "    Cube read access, then starts Cube and Superset (Superset's first start takes a few"
echo "    minutes)."
confirm

# Printed by hand, not through run(): the real command line carries the passwords.
echo '    $ docker compose exec -T postgres psql ... -v writer_pw=*** -v reader_pw=*** -f - < migrations/sql/roles.sql'
docker compose exec -T postgres psql -q -U flowbi -d openflowbi \
    -v writer_pw="$(env_get FLOWBI_WRITER_PW)" -v reader_pw="$(env_get CUBE_READER_PW)" \
    -f - < migrations/sql/roles.sql || die "applying migrations/sql/roles.sql failed."
echo '    $ docker compose exec -T postgres psql ... -f - < migrations/sql/cube_reader_grants.sql'
docker compose exec -T postgres psql -q -U flowbi -d openflowbi -v ON_ERROR_STOP=1 \
    -f - < migrations/sql/cube_reader_grants.sql || die "applying cube_reader_grants.sql failed."

run docker compose up -d cube superset
# Cube caches its database connection: a restart makes it see the new grants and tables.
run docker compose restart cube

printf '    Waiting for Superset at http://localhost:8088 '
for _ in $(seq 1 120); do
    if curl -fsS http://localhost:8088/health > /dev/null 2>&1; then
        echo
        ok "Superset is up"
        break
    fi
    printf '.'
    sleep 5
done
curl -fsS http://localhost:8088/health > /dev/null 2>&1 \
    || die "Superset didn't start within 10 minutes: docker compose logs superset"

# -----------------------------------------------------------------------------------------
step 8 "Done"
cat << EOF

    Dashboard: http://localhost:8088
      user     admin
      password $(env_get SUPERSET_ADMIN_PW)   (SUPERSET_ADMIN_PW in .env)

    First time in Superset, connect it to Cube:
      Settings > Database Connections > + Database > PostgreSQL >
      "Connect this database with a SQLAlchemy URI string instead":
        postgresql://$(env_get CUBE_SQL_USER):$(env_get CUBE_SQL_PASSWORD)@cube:15432/db
      Then Datasets > + Dataset > that database, schema public, table flow,
      and build a chart (README "Running with Docker", step 5).

    Later, to pull new Jira changes, run this script again (it skips finished steps),
    or just:
      docker compose run --rm flowbi flowbi extract issues    --destination postgres --limit 0
      docker compose run --rm flowbi flowbi extract changelog --destination postgres --limit 0
      docker compose run --rm flowbi flowbi transform

    Stop the stack: docker compose stop     Remove everything: docker compose --profile tools down -v
EOF

if [[ $YES == 0 ]] && yesno "Open the dashboard in your browser now?" y; then
    url=http://localhost:8088
    { command -v start > /dev/null && start "$url"; } \
        || { command -v open > /dev/null && open "$url"; } \
        || { command -v xdg-open > /dev/null && xdg-open "$url"; } \
        || warn "Couldn't open a browser; go to $url"
fi
