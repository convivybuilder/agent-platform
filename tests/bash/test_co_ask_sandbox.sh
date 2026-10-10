#!/bin/bash
# The ask launch's sandbox settings (bin/co, _co_ask_sandbox_settings).
#
# WHAT THIS GUARDS. Ask launches with bypassPermissions, so its Bash and its in-process
# tools could read and write the co-only and Library credential files (~/.orient-control/,
# ~/.library/, the Library's GitHub App key and config, the signal store's own token file,
# and the LiteLLM key file). The launch passes a sandbox denyRead/denyWrite and a
# permissions.deny (Read/Write scoped to those paths; Edit/Grep/Glob outright) with
# --settings. Edit is denied outright, not scoped like Read/Write, because a probe found
# a path-scoped Edit(<path>) rule inert on Claude Code 2.1.292 (see Review round 3 in the
# PR body).
# Ask is detected by _co_in_ask_tree (ask itself and every subdirectory, e.g.
# ask/handoffs), not by exact-path equality, so a launch one level down gets the same
# settings. A crew launch, or a directory merely named like ask (ask-notes), passes none,
# so every non-ask launch stays exactly as it was.
#
# The argv is captured through a claude stub, as in test_co_black_box.sh. What the
# settings do at run time is probed live, not here.
set -u
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
. "$HERE/lib.sh"

CO="$REPO_ROOT/bin/co"

setup() { # -> echoes fake HOME
    local home
    home=$(mktemp -d)
    mkdir -p "$home/stubbin" "$home/Convivy/CFO/.claude/agents" "$home/Convivy/ask" \
             "$home/Convivy/ask/handoffs" "$home/Convivy/ask-notes" \
             "$home/Convivy/operator" "$home/Convivy/EA"
    ( cd "$home/Convivy/CFO" && git init -q . )
    cat > "$home/stubbin/claude" <<'EOF'
#!/bin/bash
printf '%s\n' "$@" > "$CLAUDE_CALL_LOG"
exit 0
EOF
    chmod +x "$home/stubbin/claude"
    echo "$home"
}

run_co_in() { # <home> <rel> [args...] -> RC, CLAUDE_ARGS
    local home=$1 rel=$2; shift 2
    local call_log; call_log=$(mktemp)
    ( cd "$home/$rel" && HOME="$home" PATH="$home/stubbin:$PATH" \
      CLAUDE_CALL_LOG="$call_log" /bin/zsh "$CO" --root-config "$(co_root_config "$home")" "$@" >/dev/null 2>&1 )
    RC=$?
    CLAUDE_ARGS=$(cat "$call_log")
    rm -f "$call_log"
}

# The argument that follows --settings, or nothing when the flag is absent.
settings_arg() { echo "$CLAUDE_ARGS" | sed -n '/^--settings$/{n;p;}'; }

# 1 when $2 occurs in $1 as a fixed string, else 0.
contains() { printf '%s' "$1" | grep -qF -- "$2" && echo 1 || echo 0; }

echo "== ask passes the sandbox settings =="
H=$(setup)
run_co_in "$H" "Convivy/ask"
assert_eq "0" "$RC" "ask: co launches (exit 0 from the stub)"
assert_eq "1" "$(contains "$CLAUDE_ARGS" '--settings')" "ask: --settings is passed"
S=$(settings_arg)
assert_eq "1" "$(contains "$S" '"enabled":true')" \
    "ask: the sandbox is enabled"
assert_eq "1" "$(contains "$S" '"failIfUnavailable":true')" \
    "ask: a sandbox that cannot start refuses the launch rather than running unsandboxed"
assert_eq "1" "$(contains "$S" '"allowUnsandboxedCommands":false')" \
    "ask: the unsandboxed retry is closed, so the deny cannot be stepped around"
assert_eq "1" "$(contains "$S" "\"denyRead\":[\"$H/.orient-control\"")" \
    "ask: denyRead covers the whole ~/.orient-control directory"
assert_eq "1" "$(contains "$S" "\"$H/.library\"")" \
    "ask: denyRead covers the whole ~/.library directory"
assert_eq "1" "$(contains "$S" "\"$H/.config/convivy/litellm-keys.json\"")" \
    "ask: denyRead covers the LiteLLM gateway key file"
assert_eq "1" "$(contains "$S" "\"$H/.config/convivy/library-app.pem\"")" \
    "ask: denyRead covers the Library GitHub App's private key"
assert_eq "1" "$(contains "$S" "\"$H/.config/convivy/library-app.json\"")" \
    "ask: denyRead covers the Library GitHub App's config"
assert_eq "1" "$(contains "$S" "\"$H/Convivy/convivy-orient/api-tokens.json\"")" \
    "ask: denyRead covers the signal store's own token file"
assert_eq "1" "$(contains "$S" "\"denyWrite\":[\"$H/.orient-control\"")" \
    "ask: denyWrite covers the whole ~/.orient-control directory"
assert_eq "1" "$(contains "$S" "\"Read(/$H/.orient-control/**)\"")" \
    "ask: the Read tool is denied the whole ~/.orient-control directory"
assert_eq "1" "$(contains "$S" "\"Write(/$H/.orient-control/**)\"")" \
    "ask: the Write tool is denied the whole ~/.orient-control directory"
assert_eq "1" "$(contains "$S" '"Edit"')" \
    "ask: the Edit tool is denied outright (a path-scoped Edit rule probed as inert)"
assert_eq "1" "$(contains "$S" "\"Read(/$H/.library/**)\"")" \
    "ask: the Read tool is denied the whole ~/.library directory"
assert_eq "1" "$(contains "$S" "\"Read(/$H/.config/convivy/litellm-keys.json)\"")" \
    "ask: the Read tool is denied the LiteLLM gateway key file"
assert_eq "1" "$(contains "$S" "\"Read(/$H/.config/convivy/library-app.pem)\"")" \
    "ask: the Read tool is denied the Library GitHub App's private key"
assert_eq "1" "$(contains "$S" "\"Read(/$H/.config/convivy/library-app.json)\"")" \
    "ask: the Read tool is denied the Library GitHub App's config"
assert_eq "1" "$(contains "$S" "\"Read(/$H/Convivy/convivy-orient/api-tokens.json)\"")" \
    "ask: the Read tool is denied the signal store's own token file"
assert_eq "1" "$(contains "$S" '"Grep"')" \
    "ask: the Grep tool is denied outright"
assert_eq "1" "$(contains "$S" '"Glob"')" \
    "ask: the Glob tool is denied outright"
assert_eq "1" "$(contains "$CLAUDE_ARGS" '--permission-mode')" \
    "ask: the bypassPermissions launch is unchanged"
rm -rf "$H"

echo "== a launch from a subdirectory of ask gets the same sandbox settings =="
H=$(setup)
run_co_in "$H" "Convivy/ask/handoffs"
assert_eq "1" "$(contains "$CLAUDE_ARGS" '--settings')" \
    "ask/handoffs: --settings is passed"
S=$(settings_arg)
assert_eq "1" "$(contains "$S" "\"denyRead\":[\"$H/.orient-control\"")" \
    "ask/handoffs: denyRead covers ~/.orient-control, same as ask itself"
assert_eq "1" "$(contains "$S" "\"Read(/$H/.orient-control/**)\"")" \
    "ask/handoffs: the Read tool is denied ~/.orient-control, same as ask itself"
rm -rf "$H"

echo "== a sibling of ask such as ask-notes is not treated as ask =="
H=$(setup)
run_co_in "$H" "Convivy/ask-notes"
assert_eq "0" "$(contains "$CLAUDE_ARGS" '--settings')" \
    "ask-notes: no --settings — a directory name prefixed with 'ask' but not under it is not ask"
rm -rf "$H"

echo "== a crew launch passes no sandbox settings =="
H=$(setup)
run_co_in "$H" "Convivy/CFO"
assert_eq "0" "$(contains "$CLAUDE_ARGS" '--settings')" \
    "CFO (black box): no --settings"
assert_eq "0" "$(contains "$CLAUDE_ARGS" 'sandbox')" \
    "CFO (black box): nothing in the argv mentions the sandbox"
# A co session's launch is unchanged by the dispatched-run-only `claudeMdExcludes`
# key (run_sandbox.settings, context trim phase 2, issue_01M4D32KA2F99QGKABZ8HV2RG5):
# bin/co never imports run_sandbox and never passes --settings for a crew launch, so
# the key cannot appear in this argv at all.
assert_eq "0" "$(contains "$CLAUDE_ARGS" 'claudeMdExcludes')" \
    "CFO (black box): nothing in the argv mentions claudeMdExcludes"
rm -rf "$H"

echo "== a generic repository launch passes no sandbox settings =="
H=$(setup)
mkdir -p "$H/Convivy/CFO-scratch" && ( cd "$H/Convivy/CFO-scratch" && git init -q . )
run_co_in "$H" "Convivy/CFO-scratch"
assert_eq "0" "$(contains "$CLAUDE_ARGS" '--settings')" \
    "repository: no --settings"
rm -rf "$H"

finish
