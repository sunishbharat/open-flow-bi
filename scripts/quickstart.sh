#!/usr/bin/env bash
# OpenFlowBI quickstart: from a fresh clone to a running dashboard.
#
# Automates docs/docker.md: writes .env, creates the CA bundle, gets the
# openflowbi/core image (uses one already here, downloads a prebuilt one, or builds it),
# starts Postgres, loads Jira data, promotes the two fields the Cube model reads, applies the
# database roles, then starts Cube and Superset.
#
# Run from bash (Git Bash on Windows, macOS, Linux, WSL):
#   bash scripts/quickstart.sh              # asks once: install with defaults, or customize
#   bash scripts/quickstart.sh --yes        # defaults, no questions at all
#   bash scripts/quickstart.sh --limit 0    # load the whole project (default: 50 issues)
#   bash scripts/quickstart.sh --image ghcr.io/<owner>/openflowbi-core:<version>
#                                           # download this prebuilt image instead of building
#   bash scripts/quickstart.sh --verbose    # show every command's full output as it runs
#   bash scripts/quickstart.sh --no-gum     # plain prompts even if gum is installed
#
# With defaults, the only questions are the ones with no safe answer: the Jira address and
# token, when .env doesn't have them. Customize asks about everything.
#
# Uses Charm's gum (https://github.com/charmbracelet/gum) for menus, prompts and spinners when
# it's installed, and numbered menus and plain prompts when it isn't. Git Bash's own window
# (mintty) may not pass keyboard input to gum, so there it's off unless QUICKSTART_GUM=1;
# Windows Terminal and the VS Code terminal are fine.
#
# Command output goes to .quickstart.log; the screen shows one line per task. Colour is off
# when NO_COLOR is set or the output isn't a terminal. Symbols are plain ASCII when the locale
# isn't UTF-8, TERM=dumb or QUICKSTART_ASCII=1.
#
# Safe to re-run: existing .env values are never overwritten, and every step either skips
# what's already done or repeats it harmlessly (extracts are incremental).

set -euo pipefail
shopt -s extglob

YES=0
VERBOSE=0
NO_GUM=0
USE_GUM=0
LIMIT=50
LIMIT_GIVEN=0
MODE=defaults # or custom
STEP=0
STEPS=(
    "Check prerequisites"
    "Connect to Jira"
    "Certificates for HTTPS"
    "Get the application image"
    "Start the database"
    "Load your Jira data"
    "Start the dashboard"
)
TOTAL=${#STEPS[@]}
# The name docker-compose.yml's flowbi service runs. A downloaded image is tagged with it.
IMAGE=openflowbi/core:dev
IMAGE_SOURCE=""
LOG=.quickstart.log
SUPERSET_URL=http://localhost:8088

usage() {
    awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --yes | -y) YES=1 ;;
        --verbose | -v) VERBOSE=1 ;;
        --no-gum) NO_GUM=1 ;;
        --limit)
            [[ $# -ge 2 && $2 =~ ^[0-9]+$ ]] || { echo "--limit needs a number (0 = everything)" >&2; exit 2; }
            LIMIT=$2
            LIMIT_GIVEN=1
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
VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -n 1)

# --- look and feel ---------------------------------------------------------------------------

# Spinners and cursor control need a real terminal; colour also respects NO_COLOR.
IS_TTY=0
if [[ -t 1 && ${TERM:-} != dumb ]]; then IS_TTY=1; fi
if [[ $IS_TTY == 1 && -z ${NO_COLOR:-} ]]; then
    BOLD=$'\033[1m' DIM=$'\033[2m' RESET=$'\033[0m'
    RED=$'\033[31m' GREEN=$'\033[32m' YELLOW=$'\033[33m' BLUE=$'\033[34m' CYAN=$'\033[36m'
else
    BOLD="" DIM="" RESET="" RED="" GREEN="" YELLOW="" BLUE="" CYAN=""
fi

locale_name=${LC_ALL:-${LC_CTYPE:-${LANG:-}}}
if [[ -z ${QUICKSTART_ASCII:-} && ${TERM:-} != dumb && $locale_name == *[Uu][Tt][Ff]?(-)8* ]]; then
    OK_MARK="✔" FAIL_MARK="✖" WARN_MARK="⚠" ASK_MARK="?" POINTER="›" TO="→" SEP="·" LEADER="·"
    FRAMES=("⠋" "⠙" "⠹" "⠸" "⠼" "⠴" "⠦" "⠧" "⠇" "⠏")
    H="─" TL="╭" TR="╮" BL="╰" BR="╯" V="│"
else
    OK_MARK="[x]" FAIL_MARK="[-]" WARN_MARK="[!]" ASK_MARK="?" POINTER=">" TO="->" SEP="-" LEADER="."
    FRAMES=("|" "/" "-" "\\")
    H="-" TL="+" TR="+" BL="+" BR="+" V="|"
fi

# Done lines end at this column, so their times line up on the right.
COLS=$(tput cols 2> /dev/null || echo 80)
[[ $COLS =~ ^[0-9]+$ ]] || COLS=80
WIDTH=$((COLS < 66 ? COLS - 2 : 64))

# Two spaces before a symbol, four before a note.
ok()    { printf '  %s%s%s %s\n' "$GREEN" "$OK_MARK" "$RESET" "$*"; }
warn()  { printf '  %s%s%s %s\n' "$YELLOW" "$WARN_MARK" "$RESET" "$*"; }
note()  { printf '    %s%s%s\n' "$DIM" "$*" "$RESET"; }
aside() { printf '  %s%s %s%s\n' "$DIM" "$SEP" "$*" "$RESET"; }
clear_line() { if [[ $IS_TTY == 1 ]]; then printf '\r\033[K\033[?25h'; fi; }

repeat() {
    local out="" i
    for ((i = 0; i < $2; i++)); do out+=$1; done
    printf '%s' "$out"
}

# Visible length of a string: colour codes don't take up columns.
visible_len() {
    local plain=${1//$'\033['*([0-9;])m/}
    printf '%s' "${#plain}"
}

# done_line "label" "time" [mark colour]: "✔ label ········ 3s", the time right-aligned.
done_line() {
    local label=$1 time=$2 mark=${3:-$OK_MARK} colour=${4:-$GREEN} fill
    fill=$((WIDTH - 2 - $(visible_len "$mark") - 1 - $(visible_len "$label") - 2 - ${#time}))
    ((fill < 2)) && fill=2
    printf '  %s%s%s %s %s%s %s%s\n' "$colour" "$mark" "$RESET" "$label" \
        "$DIM" "$(repeat "$LEADER" "$fill")" "$time" "$RESET"
}

# box "line"...: a frame as wide as its longest line.
box() {
    local line width=0 len
    for line in "$@"; do
        len=$(visible_len "$line")
        ((len > width)) && width=$len
    done
    printf '%s%s%s%s%s\n' "$DIM" "$TL" "$(repeat "$H" $((width + 2)))" "$TR" "$RESET"
    for line in "$@"; do
        len=$(visible_len "$line")
        printf '%s%s%s %s%*s %s%s%s\n' "$DIM" "$V" "$RESET" "$line" $((width - len)) "" "$DIM" "$V" "$RESET"
    done
    printf '%s%s%s%s%s\n' "$DIM" "$BL" "$(repeat "$H" $((width + 2)))" "$BR" "$RESET"
}

# The tail of a failed command (or --verbose output), indented as notes.
output_block() {
    local line
    while IFS= read -r line; do
        printf '    %s%s%s %s\n' "$DIM" "$V" "$RESET" "${line%$'\r'}"
    done
}

elapsed() {
    local s=$((SECONDS - $1))
    if ((s < 60)); then printf '%ss' "$s"; else printf '%sm %02ss' $((s / 60)) $((s % 60)); fi
}

step() {
    STEP=$1
    printf '\n%s[%s/%s] %s%s\n' "$BOLD" "$STEP" "$TOTAL" "${STEPS[STEP - 1]}" "$RESET"
}

die() {
    local where=""
    if ((STEP > 0)); then where=" at [$STEP/$TOTAL] ${STEPS[STEP - 1]}"; fi
    clear_line
    {
        printf '\n  %s%s%s %sStopped%s%s\n' "$RED" "$FAIL_MARK" "$RESET" "$BOLD" "$where" "$RESET"
        printf '%s\n' "$*" | while IFS= read -r line; do printf '    %s\n' "$line"; done
        printf '    %sFull log: %s. Fix the problem, then run the script again;%s\n' "$DIM" "$LOG" "$RESET"
        printf '    %sfinished work is skipped.%s\n' "$DIM" "$RESET"
    } >&2
    exit 1
}

# --- prompts -----------------------------------------------------------------------------------
#
# Every answer is echoed back as a "✔ question: answer" line. gum clears its own UI, and in
# plain mode the prompt lines are erased first (on a terminal), so the scrollback reads the
# same either way.

ERASE=0 # plain prompts on a terminal: replace the prompt lines with the answer line

answered() { ok "$1: $2"; }

# Esc / Ctrl+C inside a gum prompt exits it with 130 (1 for some versions): stop cleanly.
gum_cancelled() {
    printf '\n'
    warn "Cancelled. Run the script again to continue."
    exit 130
}

# read_reply VAR [-s]: one line from stdin. End of input stops the script instead of
# looping on an empty answer, so a closed stdin never hangs.
# The local has an unusual name: callers pass their own `reply`, and a local of the same name
# would swallow the answer (bash scoping is dynamic).
read_reply() {
    local _line
    if [[ ${2:-} == -s ]]; then
        IFS= read -r -s _line || die "There's no input to answer the question: run this in a terminal, or use --yes."
        printf '\n'
    else
        IFS= read -r _line || die "There's no input to answer the question: run this in a terminal, or use --yes."
    fi
    printf -v "$1" '%s' "${_line%$'\r'}"
}

erase_lines() { if ((ERASE == 1 && $1 > 0)); then printf '\033[%sA\033[J' "$1"; fi; }

prompt_line() { # prompt_line "question" "hint": the hint is the only bracketed part
    printf '  %s%s%s %s' "$CYAN$BOLD" "$ASK_MARK" "$RESET" "$1"
    if [[ -n $2 ]]; then printf ' %s[%s]%s' "$DIM" "$2" "$RESET"; fi
    printf ' %s%s%s ' "$CYAN" "$POINTER" "$RESET"
}

# ask VAR "question" default [shown]: free text; Enter keeps the default. The answer is echoed
# as "question: answer", or as [shown] when given; a shown of "-" echoes nothing.
ask() {
    local reply
    if [[ $USE_GUM == 1 ]]; then
        reply=$(gum input --header "$2" --value "$3" --placeholder "${3:-type here}" --prompt "$POINTER ") \
            || gum_cancelled
    else
        prompt_line "$2" "$3"
        read_reply reply
        erase_lines 1
    fi
    reply=${reply:-$3}
    if [[ ${4:-} != - ]]; then answered "$2" "${4:-${reply:-blank}}"; fi
    printf -v "$1" '%s' "$reply"
}

# ask_secret VAR "question": hidden input, never echoed or logged.
ask_secret() {
    local reply=""
    while [[ -z $reply ]]; do
        if [[ $USE_GUM == 1 ]]; then
            reply=$(gum input --password --header "$2" --placeholder "typing is hidden" \
                --prompt "$POINTER ") || gum_cancelled
        else
            prompt_line "$2" "hidden"
            read_reply reply -s
            erase_lines 1
        fi
    done
    answered "$2" "saved"
    printf -v "$1" '%s' "$reply"
}

# choose VAR "question" default_index option...: a menu. Sets VAR to the chosen option's
# number (1-based) and CHOICE to its text. The caller echoes the answer.
CHOICE=""
mode_no="" keep="" next="" pick="" # menu answers, set by choose() through printf -v
choose() {
    local var=$1 question=$2 default=$3 reply n i lines
    shift 3
    n=$#
    if [[ $USE_GUM == 1 ]]; then
        local -a opts=("$@")
        reply=$(gum choose --header "$question" --selected "${opts[default - 1]}" "${opts[@]}") \
            || gum_cancelled
        for ((i = 1; i <= n; i++)); do
            if [[ ${opts[i - 1]} == "$reply" ]]; then printf -v "$var" '%s' "$i"; fi
        done
        CHOICE=$reply
        return
    fi
    printf '  %s%s%s %s\n' "$CYAN$BOLD" "$ASK_MARK" "$RESET" "$question"
    for ((i = 1; i <= n; i++)); do printf '    %s%s)%s %s\n' "$CYAN" "$i" "$RESET" "${!i}"; done
    lines=$((n + 1))
    while :; do
        prompt_line "Choose 1-$n" "$default"
        read_reply reply
        lines=$((lines + 1))
        reply=${reply:-$default}
        if [[ $reply =~ ^[0-9]+$ ]] && ((reply >= 1 && reply <= n)); then break; fi
    done
    erase_lines "$lines"
    printf -v "$var" '%s' "$reply"
    CHOICE=${!reply}
}

# --- running commands --------------------------------------------------------------------------

CHILD=""
on_exit() {
    if [[ -n $CHILD ]]; then kill "$CHILD" 2> /dev/null || true; fi
    if [[ $IS_TTY == 1 ]]; then printf '\033[?25h'; fi
}
trap on_exit EXIT
trap 'clear_line; printf "\n"; warn "Interrupted. Run the script again to continue."; exit 130' INT

spin_frame() { # spin_frame i start "label"
    printf '\r  %s%s%s %s %s%s%s' "$CYAN" "${FRAMES[$1 % ${#FRAMES[@]}]}" "$RESET" "$3" \
        "$DIM" "$(elapsed "$2")" "$RESET"
}

# task [--no-done] [--hide-cmd] [--stdin FILE] "label" command...
# Runs the command with a spinner and all of its output in $LOG, then prints one done line.
# A failure prints its last 20 lines instead. --no-done skips the done line (the caller prints
# its own summary from the log, between LAST_MARK and the end). --hide-cmd keeps the command
# line out of the log (it carries passwords).
LAST_MARK=0
LAST_TIME=""
task() {
    local print_done=1 hide=0 stdin=/dev/null
    while [[ $1 == --* ]]; do
        case $1 in
            --no-done) print_done=0 ;;
            --hide-cmd) hide=1 ;;
            --stdin) stdin=$2; shift ;;
        esac
        shift
    done
    local label=$1
    shift
    local start=$SECONDS status=0 i=0

    printf '\n=== %s\n' "$label" >> "$LOG"
    if ((hide == 0)); then printf '$ %s\n' "$*" >> "$LOG"; fi
    LAST_MARK=$(wc -l < "$LOG")

    if [[ $VERBOSE == 1 ]]; then
        printf '  %s%s%s %s\n' "$BLUE" "$POINTER" "$RESET" "$label"
        "$@" < "$stdin" 2>&1 | tee -a "$LOG" | output_block || status=$?
    elif [[ $USE_GUM == 1 ]]; then
        # gum runs a separate program, not a shell function: the redirections go through bash.
        # shellcheck disable=SC2016 # expanded by the inner bash, on purpose
        gum spin --spinner dot --title "$label" -- \
            bash -c 's=$1 f=$2; shift 2; "$@" < "$s" >> "$f" 2>&1' _ "$stdin" "$LOG" "$@" \
            || status=$?
    elif [[ $IS_TTY == 1 ]]; then
        "$@" < "$stdin" >> "$LOG" 2>&1 &
        CHILD=$!
        printf '\033[?25l'
        while kill -0 "$CHILD" 2> /dev/null; do
            spin_frame "$i" "$start" "$label"
            i=$((i + 1))
            sleep 0.1
        done
        wait "$CHILD" || status=$?
        CHILD=""
        clear_line
    else
        "$@" < "$stdin" >> "$LOG" 2>&1 || status=$?
    fi

    LAST_TIME=$(elapsed "$start")
    if ((status == 0)); then
        if ((print_done == 1)); then done_line "$label" "$LAST_TIME"; fi
        return 0
    fi
    done_line "$label" "$LAST_TIME" "$FAIL_MARK" "$RED"
    if ((VERBOSE == 0)); then tail -n +"$((LAST_MARK + 1))" "$LOG" | tail -n 20 | output_block; fi
    return 1
}

# The output of the last task, from the log.
last_output() { tail -n +"$((LAST_MARK + 1))" "$LOG" | tr -d '\r'; }

# wait_until "label" timeout_seconds command...: spinner until the command succeeds.
wait_until() {
    local label=$1 timeout=$2 start=$SECONDS i=0
    shift 2
    if [[ $USE_GUM == 1 ]]; then
        # shellcheck disable=SC2016 # expanded by the inner bash, on purpose
        if gum spin --spinner dot --title "$label" -- bash -c \
            'end=$((SECONDS + $1)); shift; until "$@" > /dev/null 2>&1; do
                 if ((SECONDS >= end)); then exit 1; fi; sleep 2; done' _ "$timeout" "$@"; then
            done_line "$label" "$(elapsed "$start")"
            return 0
        fi
        done_line "$label" "$(elapsed "$start")" "$FAIL_MARK" "$RED"
        return 1
    fi
    if [[ $IS_TTY == 1 ]]; then printf '\033[?25l'; fi
    while :; do
        if ((i % 20 == 0)) && "$@" > /dev/null 2>&1; then break; fi
        if ((SECONDS - start >= timeout)); then
            clear_line
            done_line "$label" "$(elapsed "$start")" "$FAIL_MARK" "$RED"
            return 1
        fi
        if [[ $IS_TTY == 1 ]]; then spin_frame "$i" "$start" "$label"; fi
        i=$((i + 1))
        sleep 0.1
    done
    clear_line
    done_line "$label" "$(elapsed "$start")"
}

# check "label" command...: an instant yes/no check, no spinner.
check() {
    local label=$1
    shift
    if "$@" > /dev/null 2>&1; then ok "$label"; else return 1; fi
}

# Runs a command in the openflowbi/core image against the compose Postgres. -T: no TTY,
# which Git Bash's terminal can't hand to docker. An array, not a function, because
# `task` may hand the command to gum, which can't call shell functions.
FB=(docker compose run --rm -T flowbi)

# --- gum detection -------------------------------------------------------------------------

# gum gets keyboard input only from a real console. Git Bash's own window (mintty) isn't one
# unless it runs inside Windows Terminal; QUICKSTART_GUM=1 overrides this guess.
gum_terminal_ok() {
    [[ ${TERM_PROGRAM:-} != mintty || -n ${WT_SESSION:-} || ${QUICKSTART_GUM:-} == 1 ]]
}

# gum input --header needs gum 0.11 or newer.
gum_recent_enough() {
    local ver major minor
    ver=$(gum --version 2> /dev/null | grep -oE '[0-9]+\.[0-9]+' | head -n 1) || return 1
    major=${ver%%.*}
    minor=${ver#*.}
    ((major > 0 || minor >= 11))
}

# Decides USE_GUM. GUM_NOTE is shown under step 1 when gum would help but isn't used.
GUM_NOTE=""
setup_gum() {
    # No prompts at all, or no terminal on both ends: nothing for gum to do.
    if [[ $NO_GUM == 1 || $YES == 1 || $IS_TTY == 0 || ! -t 0 ]]; then return; fi
    if ! command -v gum > /dev/null; then
        GUM_NOTE="gum not found, using plain prompts (optional: brew install gum / winget install charmbracelet.gum)"
    elif ! gum_terminal_ok; then
        GUM_NOTE="gum may not get keystrokes in Git Bash's own window, using plain prompts; QUICKSTART_GUM=1 tries it"
    elif ! gum_recent_enough; then
        GUM_NOTE="gum is older than 0.11, using plain prompts; update it to use it here"
    else
        USE_GUM=1
    fi
}

# --- .env helpers --------------------------------------------------------------------------

env_get() { # env_get KEY [file]
    { grep -E "^$1=" "${2:-.env}" 2> /dev/null || true; } | tail -n 1 | cut -d= -f2- | tr -d '\r'
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

GENERATED=0
env_set_if_blank() {
    if [[ -z $(env_get "$1") ]]; then
        env_set "$1" "$2"
        GENERATED=$((GENERATED + 1))
    fi
}

# Hex only, so a generated password is safe in a URL, a psql -v value and .env alike.
gen_secret() { head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; }

host_of() {
    local h=${1#*://}
    printf '%s' "${h%%/*}"
}

limit_text() { if [[ $1 == 0 ]]; then printf 'All'; else printf '%s' "$1"; fi; }

# =========================================================================================

printf 'OpenFlowBI quickstart v%s started %s\n' "$VERSION" "$(date)" > "$LOG"
setup_gum
printf 'prompts: %s\n' "$([[ $USE_GUM == 1 ]] && echo "gum $(gum --version 2> /dev/null)" || echo plain)" >> "$LOG"
if [[ $USE_GUM == 0 && $IS_TTY == 1 && -t 0 ]]; then ERASE=1; fi

echo
box "${BOLD}OpenFlowBI quickstart${RESET} ${DIM}$SEP v$VERSION${RESET}" \
    "Jira $TO Postgres $TO Cube $TO Superset"
printf '  %sSafe to re-run %s Ctrl+C anytime %s log: %s%s\n' "$DIM" "$SEP" "$SEP" "$LOG" "$RESET"
echo

# What "defaults" means here: the Jira in .env (or .env.example, which .env starts from).
env_src=.env
[[ -f .env ]] || env_src=.env.example
d_url=$(env_get FLOWBI_JIRA_BASE_URL "$env_src")
d_project=$(env_get FLOWBI_JIRA_PROJECT "$env_src")
if [[ -n $d_url ]]; then
    defaults_text="Defaults: the Jira in .env, $(host_of "$d_url")${d_project:+, project $d_project}"
else
    defaults_text="Defaults: asks only for your Jira address and token"
fi
defaults_text+=", $(limit_text "$LIMIT") issues"
[[ $LIMIT == 0 ]] && defaults_text=${defaults_text/All issues/all issues}

if [[ $YES == 1 ]]; then
    answered "Install" "with defaults, no questions (--yes)"
else
    note "$defaults_text"
    choose mode_no "How do you want to install?" 1 "Install with defaults" "Customize" "Cancel"
    case $mode_no in
        1) answered "Install" "with defaults" ;;
        2) MODE=custom; answered "Install" "customize" ;;
        3) ok "Cancelled. Nothing was changed."; exit 0 ;;
    esac
fi

# -----------------------------------------------------------------------------------------
step 1
check "Docker is installed" command -v docker \
    || die "Docker isn't installed. Install Docker Desktop: https://www.docker.com/products/docker-desktop/"
check "Docker is running" docker info \
    || die "Docker isn't running. Start Docker Desktop, wait until it says it's running, then run this again."
check "Docker Compose v2 is available" docker compose version \
    || die "'docker compose' (Compose v2) wasn't found. Update Docker Desktop."
check "curl is available" command -v curl \
    || die "curl wasn't found; it's needed to check that the dashboard is up."
if [[ -n $GUM_NOTE ]]; then aside "$GUM_NOTE"; fi

# -----------------------------------------------------------------------------------------
step 2
if [[ ! -f .env ]]; then
    cp .env.example .env
    ok "Created .env from .env.example"
fi

# need VALUE_KEY "what": stop under --yes when a value with no safe default is missing.
need() {
    if [[ $YES == 1 ]]; then
        die "$1 isn't set in .env, and it has no default. Add it to .env, or run the script
without --yes to be asked for it."
    fi
}

# ask_token KEY "question": in Customize, an existing token can be kept or replaced; with
# defaults it's asked for only when missing.
ask_token() {
    local value
    if [[ -n $(env_get "$1") ]]; then
        [[ $MODE == custom ]] || return 0
        choose keep "$2" 1 "Keep the one in .env" "Enter a new one"
        if [[ $keep == 1 ]]; then
            answered "$2" "kept"
            return
        fi
    else
        need "$1"
    fi
    ask_secret value "$2"
    env_set "$1" "$value"
}

# Server/DC or Cloud: the declared type, else a guess from the address.
jira_type_of() {
    local t
    t=$(env_get FLOWBI_JIRA_DEPLOYMENT)
    if [[ -z $t ]]; then
        if [[ $1 == *atlassian.net* ]]; then t=cloud; else t=server; fi
    fi
    printf '%s' "$t"
}

change_jira=0
url=$(env_get FLOWBI_JIRA_BASE_URL)
if [[ -z $url ]]; then
    need FLOWBI_JIRA_BASE_URL
    change_jira=1
elif [[ $MODE == custom ]]; then
    p=$(env_get FLOWBI_JIRA_PROJECT)
    choose keep "Jira settings" 1 "Keep $(host_of "$url")${p:+ $SEP project $p}" "Change them"
    if [[ $keep == 1 ]]; then answered "Jira settings" "kept"; else change_jira=1; fi
fi

project="" email="" type_no=""                  # set by ask()/choose(), through printf -v
if ((change_jira == 1)); then
    ask url "Jira address" "$url"
    [[ -n $url ]] || die "The Jira address is needed."
    url=${url%/}
    env_set FLOWBI_JIRA_BASE_URL "$url"
    if [[ $MODE == custom ]]; then
        ask project "Project key, blank for all projects" "$(env_get FLOWBI_JIRA_PROJECT)"
        env_set FLOWBI_JIRA_PROJECT "$project"
        choose type_no "Jira type" "$([[ $(jira_type_of "$url") == cloud ]] && echo 2 || echo 1)" \
            "Server or Data Center" "Cloud"
        answered "Jira type" "$CHOICE"
        env_set FLOWBI_JIRA_DEPLOYMENT "$([[ $type_no == 2 ]] && echo cloud || echo server)"
    fi
fi

if [[ $(jira_type_of "$url") == cloud ]]; then
    if [[ $MODE == custom && $change_jira == 1 || -z $(env_get FLOWBI_JIRA_EMAIL) ]]; then
        [[ -n $(env_get FLOWBI_JIRA_EMAIL) ]] || need FLOWBI_JIRA_EMAIL
        ask email "Atlassian account email" "$(env_get FLOWBI_JIRA_EMAIL)"
        env_set FLOWBI_JIRA_EMAIL "$email"
    fi
    ask_token FLOWBI_JIRA_API_TOKEN "API token"
else
    if [[ -z $(env_get FLOWBI_JIRA_PAT) || $MODE == custom ]]; then
        note "For Apache's public Jira, which needs no account, any token works: dummy-anonymous"
    fi
    ask_token FLOWBI_JIRA_PAT "Personal access token"
fi

env_set_if_blank CUBE_READER_PW "$(gen_secret 16)"
env_set_if_blank CUBE_SQL_USER superset
env_set_if_blank CUBE_SQL_PASSWORD "$(gen_secret 16)"
env_set_if_blank SUPERSET_SECRET_KEY "$(gen_secret 32)"
env_set_if_blank SUPERSET_ADMIN_PW "$(gen_secret 12)"
env_set_if_blank FLOWBI_WRITER_PW "$(gen_secret 16)"
if ((GENERATED > 0)); then ok "Generated $GENERATED passwords and keys"; fi
project=$(env_get FLOWBI_JIRA_PROJECT)
ok ".env is ready  ${DIM}$(host_of "$(env_get FLOWBI_JIRA_BASE_URL)")${project:+ $SEP project $project}${RESET}"

# -----------------------------------------------------------------------------------------
step 3
CA_FILE=${FLOWBI_BUILD_CA_BUNDLE:-.build-ca.pem}
if [[ -s $CA_FILE ]]; then
    ok "$CA_FILE is already here"
    note "If certificate errors appear later, delete it and run the script again."
elif command -v uv > /dev/null; then
    task "Build $CA_FILE and test it" uv run python scripts/make_ca_bundle.py --out "$CA_FILE" \
        || die "The certificate check failed: see the FAILED line above, and docs/docker.md,
step 1."
else
    # shellcheck disable=SC2016 # $1 is expanded by the inner bash, on purpose
    task "Copy the public certificates" \
        bash -c 'docker run --rm python:3.12-slim-bookworm cat /etc/ssl/certs/ca-certificates.crt > "$1"' \
        _ "$CA_FILE" \
        || die "Couldn't read the public certificates from the Python image."
    warn "uv isn't installed, so only the public certificates are used"
    note "That works unless antivirus or a company proxy inspects HTTPS here. If so,"
    note "install uv (https://docs.astral.sh/uv/), delete $CA_FILE and run this again."
fi

# -----------------------------------------------------------------------------------------
step 4

# Download REF (unless it's already on this machine) and tag it as $IMAGE, the name the
# compose flowbi service runs. Explicit `|| return 1`s: errexit is off inside an `if`.
pull_and_link() {
    if ! docker image inspect "$1" > /dev/null 2>&1; then
        task "Download $1" docker pull "$1" || return 1
    fi
    if [[ $1 != "$IMAGE" ]]; then
        docker tag "$1" "$IMAGE" > /dev/null 2>&1 || return 1
    fi
    docker run --rm "$IMAGE" flowbi version > /dev/null 2>&1 \
        || die "$1 doesn't run 'flowbi', so it isn't an openflowbi/core image."
    ok "Using $1"
}

build_image() {
    note "Building takes a few minutes the first time."
    task "Build the image" docker compose --profile tools build flowbi \
        || die "The image build failed: see the lines above."
}

# Try REF; if it can't be downloaded, build instead (Customize asks first).
pull_or_build() {
    if pull_and_link "$1"; then
        env_set FLOWBI_IMAGE_SOURCE "$1"
        return
    fi
    warn "Couldn't download $1"
    note "Check the name and tag. A private ghcr.io image needs 'docker login ghcr.io' first."
    if [[ $MODE == custom ]]; then
        choose next "What now?" 1 "Build from source" "Stop"
        answered "What now" "$CHOICE"
        [[ $next == 1 ]] || die "The application image is needed: run this again to download or build it."
    fi
    build_image
}

known=$(env_get FLOWBI_IMAGE_SOURCE)
have_local=0
if docker image inspect "$IMAGE" > /dev/null 2>&1; then have_local=1; fi

if [[ -n $IMAGE_SOURCE ]]; then
    # --image given: the user asked for exactly this image.
    answered "Image" "download $IMAGE_SOURCE (--image)"
    pull_or_build "$IMAGE_SOURCE"
else
    options=()
    if ((have_local == 1)); then options+=("Use the one on this machine"); fi
    options+=("Download a prebuilt image" "Build from source")
    # Default: the local image, else the one downloaded last time, else a build.
    if ((have_local == 1)); then pick=1; elif [[ -n $known ]]; then pick=$((have_local + 1)); else pick=$((have_local + 2)); fi
    if [[ $MODE == custom ]]; then
        choose pick "Image" "$pick" "${options[@]}"
    fi
    answered "Image" "${options[pick - 1]}"
    case ${options[pick - 1]} in
        Use*) ok "Using $IMAGE" ;;
        Download*)
            ref=$known
            if [[ $MODE == custom || -z $ref ]]; then
                ask ref "Image to download, e.g. ghcr.io/<owner>/openflowbi-core:<version>" "$known"
            fi
            [[ -n $ref ]] || die "No image name was given."
            pull_or_build "$ref"
            ;;
        Build*) build_image ;;
    esac
fi

# -----------------------------------------------------------------------------------------
step 5
task "Start Postgres" docker compose up -d --wait postgres \
    || die "Postgres didn't start. See: docker compose logs postgres"
task --no-done "Check the Jira connection" "${FB[@]}" flowbi doctor \
    || die "Couldn't reach Jira. Check the address and token: run the script again and choose
Customize, then change the Jira settings. A CERTIFICATE_VERIFY_FAILED error means antivirus or
a proxy inspects HTTPS here: install uv, delete $CA_FILE and run the script again."
doctor=$(last_output)
deployment=$(grep -m 1 'Deployment' <<< "$doctor" | grep -oE 'Server/DC|Cloud' || true)
done_line "Jira answers  ${deployment:-Jira} $SEP $(host_of "$(env_get FLOWBI_JIRA_BASE_URL)")" "$LAST_TIME"
if grep -m 1 'Account timezone' <<< "$doctor" | grep -q unavailable; then
    warn "Account timezone unavailable"
    note "Expected for anonymous access, such as Apache's public Jira. Otherwise check the token."
fi
task "Create the database tables" "${FB[@]}" alembic upgrade head \
    || die "Creating the database tables failed: see the lines above."

# -----------------------------------------------------------------------------------------
step 6
if [[ $MODE == custom && $LIMIT_GIVEN == 0 ]]; then
    choose pick "Issues to load" 1 "50 (quick try)" "500" "All (can take hours)" "Custom number"
    case $pick in
        1) LIMIT=50 ;;
        2) LIMIT=500 ;;
        3) LIMIT=0 ;;
        4)
            LIMIT=""
            until [[ $LIMIT =~ ^[0-9]+$ ]]; do ask LIMIT "Number of issues, 0 for all" "1000" "-"; done
            ;;
    esac
fi
answered "Issues to load" "$(limit_text "$LIMIT")"

task "Load issues" "${FB[@]}" flowbi extract issues --destination postgres --limit "$LIMIT" \
    || die "Loading issues failed: see the lines above."
task "Load issue history" "${FB[@]}" flowbi extract changelog --destination postgres --limit "$LIMIT" \
    || die "Loading the issue history failed: see the lines above."
task "Discover Jira fields" "${FB[@]}" flowbi fields discover \
    || die "Field discovery failed: see the lines above."

task --no-done "Read the field selection" "${FB[@]}" flowbi fields export \
    || die "Reading the field selection failed: see the lines above."
selection=$(last_output)
# True when COLUMN is already promoted (and not demoted) in the current field selection.
promoted() { grep -A2 "column_name: $1\$" <<< "$selection" | grep -q 'deprecated: false'; }

if promoted resolution_name; then
    ok "Resolution field ${DIM}already prepared${RESET}"
else
    task "Prepare the Resolution field" \
        "${FB[@]}" flowbi fields promote "Resolution" --column resolution_name --schema-type resolution \
        || die "Preparing the Resolution field failed: see the lines above."
fi
if promoted fix_version; then
    ok "Fix Version field ${DIM}already prepared${RESET}"
else
    # The field is "Fix Version/s" on Server/DC and "Fix versions" on Cloud. A bash -c, not
    # a function, so gum can run it too.
    # shellcheck disable=SC2016 # expanded by the inner bash, on purpose
    task "Prepare the Fix Version field" bash -c \
        '"$@" "Fix Version/s" --column fix_version --target bridge_table ||
         "$@" "Fix versions" --column fix_version --target bridge_table' \
        _ "${FB[@]}" flowbi fields promote \
        || die "Preparing the Fix Version field failed: see the lines above."
fi

task "Build the reporting tables" "${FB[@]}" flowbi transform \
    || die "Building the reporting tables failed: see the lines above."

# -----------------------------------------------------------------------------------------
step 7
task --hide-cmd --stdin migrations/sql/roles.sql "Set up database access" \
    docker compose exec -T postgres psql -q -U flowbi -d openflowbi \
    -v writer_pw="$(env_get FLOWBI_WRITER_PW)" -v reader_pw="$(env_get CUBE_READER_PW)" -f - \
    || die "Applying migrations/sql/roles.sql failed: see the lines above."
task --stdin migrations/sql/cube_reader_grants.sql "Give Cube read access" \
    docker compose exec -T postgres psql -q -U flowbi -d openflowbi -v ON_ERROR_STOP=1 -f - \
    || die "Applying migrations/sql/cube_reader_grants.sql failed: see the lines above."
task "Start Cube and Superset" docker compose up -d cube superset \
    || die "Starting Cube and Superset failed: see the lines above."
# Cube caches its database connection: a restart makes it see the new grants and tables.
task "Reload Cube" docker compose restart cube \
    || die "Restarting Cube failed: see the lines above."
note "Superset's first start takes a few minutes."
wait_until "Wait for Superset" 600 curl -fsS "$SUPERSET_URL/health" \
    || die "Superset didn't start within 10 minutes. See: docker compose logs superset"

echo
box "${GREEN}${OK_MARK}${RESET} ${BOLD}OpenFlowBI is ready${RESET}" \
    "" \
    "${DIM}Dashboard${RESET}  ${BOLD}$SUPERSET_URL${RESET}" \
    "${DIM}User${RESET}       admin" \
    "${DIM}Password${RESET}   in .env, SUPERSET_ADMIN_PW" \
    "" \
    "${DIM}Cube connection for Superset, first time only:${RESET}" \
    "${CYAN}postgresql://$(env_get CUBE_SQL_USER):$(env_get CUBE_SQL_PASSWORD)@cube:15432/db${RESET}" \
    "${DIM}Settings $TO Database Connections $TO + Database $TO PostgreSQL, then the URI${RESET}" \
    "" \
    "${DIM}Update${RESET}     bash scripts/quickstart.sh" \
    "${DIM}Stop${RESET}       docker compose stop"
echo

if [[ $MODE == custom ]]; then
    choose pick "Open the dashboard?" 1 "Open it in the browser" "Not now"
    answered "Open the dashboard" "$([[ $pick == 1 ]] && echo yes || echo "not now")"
    if [[ $pick == 1 ]]; then
        { command -v start > /dev/null && start "$SUPERSET_URL"; } \
            || { command -v open > /dev/null && open "$SUPERSET_URL"; } \
            || { command -v xdg-open > /dev/null && xdg-open "$SUPERSET_URL"; } \
            || warn "Couldn't open a browser; go to $SUPERSET_URL"
    fi
fi
