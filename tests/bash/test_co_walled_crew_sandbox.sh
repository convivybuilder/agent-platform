#!/bin/bash
# The walled-off crew's launch sandbox settings (platform/decisions/0100;
# bin/co, _co_walled_sandbox_settings).
#
# WHAT THIS GUARDS. The walled-off crew launches with bypassPermissions, so its Bash
# and its in-process tools could read and write the co-only and Library credential
# files (~/.orient-control/, ~/.library/, the Library's GitHub App key and config,
# the signal store's own token file, and the LiteLLM key file). The launch passes a
# sandbox denyRead/denyWrite and a permissions.deny (Read/Write scoped to those
# paths; Edit/Grep/Glob outright) with --settings. Edit is denied outright, not
# scoped like Read/Write, because a probe found a path-scoped Edit(<path>) rule
# inert on Claude Code 2.1.292 (see Review round 3 in the PR body).
# The crew is detected by the early walled-off check (bin/co, `walled_off_home_for`:
# its registered home and every subdirectory, e.g. <home>/handoffs), not by exact-path
# equality, so a launch one level down gets the same settings. A crew launch, or a
# directory merely named like it (a "-notes" sibling), passes none, so every
# non-walled launch stays exactly as it was.
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
    mkdir -p "$home/stubbin" "$home/Convivy/LEDGER/.claude/agents" "$home/Convivy/parlor" \
             "$home/Convivy/parlor/handoffs" "$home/Convivy/parlor-notes" \
             "$home/Convivy/operator" "$home/Convivy/SATCHEL"
    ( cd "$home/Convivy/LEDGER" && git init -q . )
    cat > "$home/stubbin/claude" <<'EOF'
#!/bin/bash
printf '%s\n' "$@" > "$CLAUDE_CALL_LOG"
exit 0
EOF
    chmod +x "$home/stubbin/claude"
    # A registry naming one synthetic walled-off crew, matching the "parlor"
    # directory above — unrelated to the rich fixture's own units.
    write_walled_off_registry "$home/walled-registry.json" parlor Parlor
    echo "$home"
}

run_co_in() { # <home> <rel> [args...] -> RC, CLAUDE_ARGS
    local home=$1 rel=$2; shift 2
    local call_log; call_log=$(mktemp)
    ( cd "$home/$rel" && HOME="$home" PATH="$home/stubbin:$PATH" \
      CLAUDE_CALL_LOG="$call_log" /bin/zsh "$CO" --root-config "$(co_root_config "$home" "$home/walled-registry.json")" "$@" >/dev/null 2>&1 )
    RC=$?
    CLAUDE_ARGS=$(cat "$call_log")
    rm -f "$call_log"
}

# The argument that follows --settings, or nothing when the flag is absent.
settings_arg() { echo "$CLAUDE_ARGS" | sed -n '/^--settings$/{n;p;}'; }

# 1 when $2 occurs in $1 as a fixed string, else 0.
contains() { printf '%s' "$1" | grep -qF -- "$2" && echo 1 || echo 0; }

echo "== the walled-off crew passes the sandbox settings =="
H=$(setup)
run_co_in "$H" "Convivy/parlor"
assert_eq "0" "$RC" "parlor: co launches (exit 0 from the stub)"
assert_eq "1" "$(contains "$CLAUDE_ARGS" '--settings')" "parlor: --settings is passed"
S=$(settings_arg)
assert_eq "1" "$(contains "$S" '"enabled":true')" \
    "parlor: the sandbox is enabled"
assert_eq "1" "$(contains "$S" '"failIfUnavailable":true')" \
    "parlor: a sandbox that cannot start refuses the launch rather than running unsandboxed"
assert_eq "1" "$(contains "$S" '"allowUnsandboxedCommands":false')" \
    "parlor: the unsandboxed retry is closed, so the deny cannot be stepped around"
assert_eq "1" "$(contains "$S" "\"denyRead\":[\"$H/.orient-control\"")" \
    "parlor: denyRead covers the whole ~/.orient-control directory"
assert_eq "1" "$(contains "$S" "\"$H/.library\"")" \
    "parlor: denyRead covers the whole ~/.library directory"
assert_eq "1" "$(contains "$S" "\"$H/.local/state/library\"")" \
    "parlor: denyRead covers the whole ~/.local/state/library directory"
assert_eq "1" "$(contains "$S" "\"$H/.config/convivy/litellm-keys.json\"")" \
    "parlor: denyRead covers the LiteLLM gateway key file"
assert_eq "1" "$(contains "$S" "\"$H/.config/convivy/library-app.pem\"")" \
    "parlor: denyRead covers the Library GitHub App's private key"
assert_eq "1" "$(contains "$S" "\"$H/.config/convivy/library-app.json\"")" \
    "parlor: denyRead covers the Library GitHub App's config"
assert_eq "1" "$(contains "$S" "\"$H/Convivy/convivy-orient/api-tokens.json\"")" \
    "parlor: denyRead covers the signal store's own token file"
assert_eq "1" "$(contains "$S" "\"denyWrite\":[\"$H/.orient-control\"")" \
    "parlor: denyWrite covers the whole ~/.orient-control directory"
assert_eq "1" "$(contains "$S" "\"Read(/$H/.orient-control/**)\"")" \
    "parlor: the Read tool is denied the whole ~/.orient-control directory"
assert_eq "1" "$(contains "$S" "\"Write(/$H/.orient-control/**)\"")" \
    "parlor: the Write tool is denied the whole ~/.orient-control directory"
assert_eq "1" "$(contains "$S" '"Edit"')" \
    "parlor: the Edit tool is denied outright (a path-scoped Edit rule probed as inert)"
assert_eq "1" "$(contains "$S" "\"Read(/$H/.library/**)\"")" \
    "parlor: the Read tool is denied the whole ~/.library directory"
assert_eq "1" "$(contains "$S" "\"Read(/$H/.local/state/library/**)\"")" \
    "parlor: the Read tool is denied the whole ~/.local/state/library directory"
assert_eq "1" "$(contains "$S" "\"Write(/$H/.local/state/library/**)\"")" \
    "parlor: the Write tool is denied the whole ~/.local/state/library directory"
assert_eq "1" "$(contains "$S" "\"Read(/$H/.config/convivy/litellm-keys.json)\"")" \
    "parlor: the Read tool is denied the LiteLLM gateway key file"
assert_eq "1" "$(contains "$S" "\"Read(/$H/.config/convivy/library-app.pem)\"")" \
    "parlor: the Read tool is denied the Library GitHub App's private key"
assert_eq "1" "$(contains "$S" "\"Read(/$H/.config/convivy/library-app.json)\"")" \
    "parlor: the Read tool is denied the Library GitHub App's config"
assert_eq "1" "$(contains "$S" "\"Read(/$H/Convivy/convivy-orient/api-tokens.json)\"")" \
    "parlor: the Read tool is denied the signal store's own token file"
assert_eq "1" "$(contains "$S" '"Grep"')" \
    "parlor: the Grep tool is denied outright"
assert_eq "1" "$(contains "$S" '"Glob"')" \
    "parlor: the Glob tool is denied outright"
assert_eq "1" "$(contains "$CLAUDE_ARGS" '--permission-mode')" \
    "parlor: the bypassPermissions launch is unchanged"
rm -rf "$H"

echo "== a launch from a subdirectory of the walled-off crew gets the same sandbox settings =="
H=$(setup)
run_co_in "$H" "Convivy/parlor/handoffs"
assert_eq "1" "$(contains "$CLAUDE_ARGS" '--settings')" \
    "parlor/handoffs: --settings is passed"
S=$(settings_arg)
assert_eq "1" "$(contains "$S" "\"denyRead\":[\"$H/.orient-control\"")" \
    "parlor/handoffs: denyRead covers ~/.orient-control, same as the crew's own home"
assert_eq "1" "$(contains "$S" "\"Read(/$H/.orient-control/**)\"")" \
    "parlor/handoffs: the Read tool is denied ~/.orient-control, same as the crew's own home"
rm -rf "$H"

echo "== a sibling such as parlor-notes is not treated as the walled-off crew =="
H=$(setup)
run_co_in "$H" "Convivy/parlor-notes"
assert_eq "0" "$(contains "$CLAUDE_ARGS" '--settings')" \
    "parlor-notes: no --settings — a directory name prefixed with the crew's name but not under it is not the crew"
rm -rf "$H"

echo "== the early walled-off check itself refuses to launch when it cannot tell =="
# Isolated from the pre-existing private-crew gate a few hundred lines down in
# bin/co: a registry a MALFORMED FILE would also break reaches BOTH gates, so
# that alone cannot tell whether this one, rather than the older one, is what
# refused. Here the registry is valid and empty — the private-crew gate would
# read it fine and find nothing private — but python3 is stubbed to fail ONLY
# the heredoc calling `walled_off_home_for` (matched by that string, the one
# unique to it), passing every other call through to the real interpreter.
# Convivy/parlor must still never launch: this early gate runs first and on its
# own "cannot tell" refuses outright, before the private-crew gate is ever reached.
H=$(setup)
REAL_PYTHON3=$(command -v python3)
cat > "$H/stubbin/python3" <<EOF
#!/bin/bash
if [ "\$1" = "-" ]; then
    shift
    script=\$(cat)
    case "\$script" in
        *walled_off_home_for*) exit 3 ;;
    esac
    printf '%s' "\$script" | "$REAL_PYTHON3" - "\$@"
else
    exec "$REAL_PYTHON3" "\$@"
fi
EOF
chmod +x "$H/stubbin/python3"
call_log=$(mktemp)
( cd "$H/Convivy/parlor" && HOME="$H" PATH="$H/stubbin:$PATH" CLAUDE_CALL_LOG="$call_log" \
  /bin/zsh "$CO" --root-config "$(co_root_config "$H")" >/dev/null 2>&1 )
RC=$?
assert_eq "1" "$RC" "early check cannot tell: co exits non-zero"
assert_eq "" "$(cat "$call_log")" "early check cannot tell: claude is never launched"
rm -f "$call_log"
rm -rf "$H"

echo "== an ordinary, unregistered repo passes no sandbox settings =="
H=$(setup)
run_co_in "$H" "Convivy/LEDGER"
assert_eq "0" "$(contains "$CLAUDE_ARGS" '--settings')" \
    "LEDGER: no --settings"
assert_eq "0" "$(contains "$CLAUDE_ARGS" 'sandbox')" \
    "LEDGER: nothing in the argv mentions the sandbox"
# A co session's launch is unchanged by the dispatched-run-only `claudeMdExcludes`
# key (run_sandbox.settings, context trim phase 2, issue_01M4D32KA2F99QGKABZ8HV2RG5):
# bin/co never imports run_sandbox and never passes --settings for a crew launch, so
# the key cannot appear in this argv at all.
assert_eq "0" "$(contains "$CLAUDE_ARGS" 'claudeMdExcludes')" \
    "LEDGER: nothing in the argv mentions claudeMdExcludes"
rm -rf "$H"

echo "== a generic repository launch passes no sandbox settings =="
H=$(setup)
mkdir -p "$H/Convivy/LEDGER-scratch" && ( cd "$H/Convivy/LEDGER-scratch" && git init -q . )
run_co_in "$H" "Convivy/LEDGER-scratch"
assert_eq "0" "$(contains "$CLAUDE_ARGS" '--settings')" \
    "repository: no --settings"
rm -rf "$H"

finish
