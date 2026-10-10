#!/bin/bash
# tests/bash/lib.sh — tiny assertion + sourcing helpers for the bash test
# foundation (substrate ARCH-098). Sourced by each test_*.sh.
#
# MUST run under /bin/bash, which is 3.2.57 on both macOS CI runners and the
# deployment mini. Homebrew/Linux bash is 5.x and would NOT exercise the 3.2
# constraints whose violation (declare -A) silently killed an overnight in
# ARCH-082 — so this is the only place that class of bug is caught.

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
_BASH_TEST_MOCKS=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/mocks
_PASS=0
_FAIL=0

# NO BASH TEST WRITES THE LIVE RUN RECORDS, whether run from run.sh or on its own.
# The launchers resolve run records through builder_root.data_root(), so
# a test file run standalone needs a data root as well. run.sh's export wins.
: "${BUILDER_ROOT:=$(mktemp -d)}"
export BUILDER_ROOT

# Every bash test that reaches `knowledge.py` through a subprocess (any python3
# call that imports agent_policy or knowledge, directly or transitively) gets a
# synthetic operator config, never this machine's real
# $HOME/.config/convivy/operator.json. `knowledge.py` reads it at MODULE IMPORT
# TIME to build ALLOWED_AUDIENCE/NON_AGENT_AUDIENCE, and raises
# OperatorConfigError, fail-closed by design (CQ·428), when the file is
# missing — which it always is on the CI runner, and on any machine with no
# $HOME/.config/convivy. Same keys and values as tests/conftest.py's synthetic
# operator.json, so both harnesses read the same fixture shape.
_BASH_TEST_OPERATOR_CONFIG_DIR=$(mktemp -d)
CONVIVY_OPERATOR_CONFIG="$_BASH_TEST_OPERATOR_CONFIG_DIR/operator.json"
cat > "$CONVIVY_OPERATOR_CONFIG" <<'JSON'
{
  "excluded_crews": ["private-a", "private-b", "private-c", "private-d",
                      "private-e", "private-f", "private-g"],
  "operator_marker": "operator",
  "roster_crew": "operator-crew",
  "github_login": "operator-login",
  "tailnet_host": "tailnet.example"
}
JSON
export CONVIVY_OPERATOR_CONFIG

# NO BASH TEST MAY REACH THE HOST'S REAL LAUNCHD THROUGH A BARE `launchctl`
# CALL. This stub is a PATH prepend, so it covers only `launchctl` invoked by
# bare command name; a call by absolute path (`/bin/launchctl`, as
# bin/arch129-remeasure:67 makes) is not intercepted by anything here.
# `gui/$(id -u)/<label>` addresses the actual per-user launchd domain no
# matter what HOME a test sandboxes, so an unstubbed `launchctl print`/
# `kickstart` inside code under test finds and can restart a REAL daemon —
# measured 2026-09-13:
# install-agent-platform-shared's own suite SIGTERMed the live
# com.convivy.knowledge-watcher roughly 200 times in an hour, because its
# production-deploy-area guard was comparing paths derived from the sandboxed
# HOME to each other rather than checking what launchd actually had loaded.
#
# Prepended to PATH at source time, because this must cover a test that runs
# an installer as a SUBPROCESS (`sh "$INSTALLER" ...`), not only code sourced
# into this shell. `print`
# reports nothing (as launchd does for a label that isn't loaded); kickstart,
# bootout, bootstrap, stop and kill refuse loudly rather than silently
# no-opping, so a test exercising one of those paths fails visibly instead of
# passing by accident. A test that needs its own launchctl behaviour (to
# exercise a restart path, say) prepends its own stub directory afterward —
# PATH search order gives the later, more specific one priority.
_BASH_TEST_LAUNCHCTL_STUB_DIR=$(mktemp -d)
LAUNCHCTL_STUB_LOG="$_BASH_TEST_LAUNCHCTL_STUB_DIR/launchctl.log"
export LAUNCHCTL_STUB_LOG
cat > "$_BASH_TEST_LAUNCHCTL_STUB_DIR/launchctl" <<'STUB'
#!/bin/bash
printf '%s\n' "$*" >> "${LAUNCHCTL_STUB_LOG:-/dev/null}"
case "$1" in
    print)
        exit 1
        ;;
    kickstart|bootout|bootstrap|stop|kill)
        echo "launchctl stub (tests/bash/lib.sh): refusing to $1 against the host's real launchd — this is the bash test suite, not production ($*)" >&2
        exit 1
        ;;
    *)
        exit 0
        ;;
esac
STUB
chmod +x "$_BASH_TEST_LAUNCHCTL_STUB_DIR/launchctl"
PATH="$_BASH_TEST_LAUNCHCTL_STUB_DIR:$PATH"
export PATH

# co_root_config <fake-home> [<registry-json>]: write a session-agent.env naming
# <fake-home>/Convivy as the machine's root and print its path, for `co
# --root-config`. co's root is builder_root.root(), which ignores HOME, so a test
# that fakes HOME for co's crews must also point the root there, explicitly, the
# one way root() can be moved.
#
# The optional second argument also writes PROJECTS_REGISTRY=<that path> — the
# seam bin/co reads at every site that resolves config/projects.json, so a test
# needing a private or sonnet-tier crew the operator's own checked-in registry
# does not (and must not) name can hand it a fixture instead
# (write_private_registry below writes one). Omitted, co falls back to its own
# config/projects.json exactly as it did before this seam existed.
co_root_config() {
    mkdir -p "$1/Convivy"
    {
        printf 'CREWS_ROOT=%s\n' "$1/Convivy"
        [ -n "${2:-}" ] && printf 'PROJECTS_REGISTRY=%s\n' "$2"
    } > "$1/session-agent.env"
    printf '%s\n' "$1/session-agent.env"
}

# write_private_registry <path> <slug> <display>: a registry naming exactly one
# active, `private: true` crew, for a test that exercises bin/co's private-crew
# wall (`_co_private_crew`, the transport wall at platform/decisions/0043) without
# touching the operator's own checked-in config/projects.json or the real crew
# slug registered there. Paired with co_root_config's PROJECTS_REGISTRY argument.
write_private_registry() { # <path> <slug> <display>
    cat > "$1" <<JSON
{
  "$2": {
    "class": "project", "display": "$3", "owner": "convivy",
    "home_dir": "<root>/$2", "dirs": [], "crew": "$2",
    "lifecycle": "active", "products": [], "repos": ["$2"],
    "aliases": [], "kb": "local", "private": true
  }
}
JSON
}

# write_kb_sync_stub <stubbin>: an `agentctl` for `co -r`'s knowledge refresh.
#
# `co` no longer fetches for itself: `_co_pull_knowledge` runs `agentctl kb sync`,
# which authenticates as the convivy-library App. A fixture that puts <stubbin>
# first on PATH and has no stub would run the REAL agentctl against the real
# machine's knowledge checkout and index, so every test that drives `co -r` past
# a crew branch calls this. The stub honours only `kb sync`, and refuses anything
# else so a stray call fails loudly rather than answering.
#
# It works on $HOME/Convivy/knowledge (the fixtures' fake HOME, which
# co_root_config makes the machine's root) through the `git` on PATH, so a spy git
# still sees the fetch and merge, unless AGENTCTL_STUB_GIT names another git.
# KB_SYNC_MODE picks the outcome, mirroring the three `cmd_kb_sync` has:
#   ok (default)  fetch, fast-forward, exit 0
#   offline       "fetch failed (likely offline)" on stderr, exit 0, nothing moved
#   refused       "refused — cannot obtain the convivy-library App identity", exit 1
#   unverified    "STILL INCONSISTENT after a full reconcile" on stderr, exit 2
#                 (the real one exits 2 whether or not a fast-forward preceded it)
# A checkout that will not fast-forward exits 1, as `kb sync` does. Each call is
# appended to AGENTCTL_LOG when that names a file.
write_kb_sync_stub() { # <stubbin>
    cat > "$1/agentctl" <<'EOF'
#!/bin/bash
if [ "$1 $2" != "kb sync" ]; then
    echo "agentctl stub: only 'kb sync' is stubbed, got: $*" >&2
    exit 99
fi
[ -n "${AGENTCTL_LOG:-}" ] && echo "agentctl $*" >> "$AGENTCTL_LOG"
git=${AGENTCTL_STUB_GIT:-git}
kb="$HOME/Convivy/knowledge"
case "${KB_SYNC_MODE:-ok}" in
    offline)
        echo "kb sync: fetch failed (likely offline) — index left as-is" >&2
        exit 0 ;;
    refused)
        echo "kb sync: refused — cannot obtain the convivy-library App identity — stub. The index is left as-is; it does not fall back to a personal credential." >&2
        exit 1 ;;
    unverified)
        echo "kb sync: STILL INCONSISTENT after a full reconcile — 1 missing, 0 extra, 0 stale. This is a real defect, not drift." >&2
        exit 2 ;;
esac
"$git" -C "$kb" fetch -q origin main:refs/remotes/origin/main || exit 1
if ! "$git" -C "$kb" merge -q --ff-only origin/main; then
    echo "kb sync: fast-forward REFUSED — the checkout has diverged or has uncommitted edits." >&2
    exit 1
fi
echo "kb sync: up to date, verified"
EOF
    chmod +x "$1/agentctl"
}

assert_eq() { # <expected> <actual> <msg>
    if [ "$1" = "$2" ]; then
        _PASS=$((_PASS + 1))
    else
        _FAIL=$((_FAIL + 1))
        echo "  FAIL: $3"
        echo "        expected: [$1]"
        echo "        actual:   [$2]"
    fi
}

assert_rc() { assert_eq "$1" "$2" "$3"; } # <expected_rc> <actual_rc> <msg>

assert_contains() { # <haystack> <needle> <msg>
    case "$1" in
        *"$2"*)
            _PASS=$((_PASS + 1))
            ;;
        *)
            _FAIL=$((_FAIL + 1))
            echo "  FAIL: $3"
            echo "        looked for: [$2]"
            echo "        in:         [$1]"
            ;;
    esac
}

finish() {
    echo "  $_PASS passed, $_FAIL failed"
    [ "$_FAIL" -eq 0 ]
}
