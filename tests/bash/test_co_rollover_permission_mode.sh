#!/bin/bash
# Tests for a rollover successor's permission mode (issue_01M35E0WAGSPEHBA18ZSY6CXJZ).
#
# ADR-0069, the operator's 2026-09-16 ruling: "let's just leave the mode the same,
# it's not like it's worse after the rollover." Before this fix every launch
# site in bin/co passed `--permission-mode bypassPermissions` literally, so a
# session an operator had narrowed by hand to `plan` or `default` came back
# from a rollover with permission checks skipped — widening authority at
# exactly the boundary the ruling was about.
#
# `co_rollover.spawn_successor` now passes the predecessor's captured mode as
# CO_ROLLOVER_PERMISSION_MODE, a third prefix assignment alongside
# CO_ROLLOVER_SUCCESSOR and CO_ROLLOVER_EXEMPT_PID. This file exercises
# bin/co's own half of that: a known mode reaches claude's own
# --permission-mode, an unknown or missing one falls back to
# bypassPermissions (today's fixed behaviour), and a launch with no rollover
# marker at all is unaffected.
#
# `co` is zsh (excluded from the ARCH-082 bash-3.2 contract by lint.sh's
# shebang filter); bash invoking it here is exactly how a login shell will.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

CO="$REPO_ROOT/bin/co"
PROMPT="/handoff-pickup /x/handoff.md"

# A fake HOME with a real ~/Convivy/knowledge tracking a real bare origin (the
# `-r` path pulls it before individuating, so a fake would abort first and
# every assertion below would pass for the wrong reason), plus one product
# fixture (the generic product-branch tail, which requires cwd's basename be
# in its own `-r` allowlist — `vivy` is).
setup() { # -> echoes fake HOME
    local home
    home=$(mktemp -d)
    mkdir -p "$home/stubbin"
    write_kb_sync_stub "$home/stubbin"

    git init -q --bare "$home/kb-remote.git"
    git init -q "$home/Convivy/knowledge"
    (
        cd "$home/Convivy/knowledge" || exit 1
        git config user.email t@example.com
        git config user.name Test
        echo seed > seed.txt
        git add seed.txt
        git commit -qm seed
        git branch -M main
        git remote add origin "$home/kb-remote.git"
        git push -q -u origin main
    ) >/dev/null 2>&1

    mkdir -p "$home/Convivy/vivy/.claude/agents"
    : > "$home/Convivy/vivy/.claude/agents/planning-orchestrator.md"
    ( cd "$home/Convivy/vivy" && git init -q . )

    # Records argv AND whether any of the three rollover markers reached this
    # process — bin/co is supposed to consume all three before `exec claude`
    # ever runs, same discipline test_co_rollover_successor.sh already pins
    # for the other two.
    cat > "$home/stubbin/claude" <<'EOF'
#!/bin/bash
printf '%s\n' "$@" > "$CLAUDE_CALL_LOG"
: > "$CLAUDE_ENV_LOG"
if [[ -n ${CO_ROLLOVER_SUCCESSOR+set} ]]; then
    echo "CO_ROLLOVER_SUCCESSOR=${CO_ROLLOVER_SUCCESSOR}" >> "$CLAUDE_ENV_LOG"
fi
if [[ -n ${CO_ROLLOVER_EXEMPT_PID+set} ]]; then
    echo "CO_ROLLOVER_EXEMPT_PID=${CO_ROLLOVER_EXEMPT_PID}" >> "$CLAUDE_ENV_LOG"
fi
if [[ -n ${CO_ROLLOVER_PERMISSION_MODE+set} ]]; then
    echo "CO_ROLLOVER_PERMISSION_MODE=${CO_ROLLOVER_PERMISSION_MODE}" >> "$CLAUDE_ENV_LOG"
fi
exit 0
EOF
    chmod +x "$home/stubbin/claude"

    cat > "$home/stubbin/knowledge-ctx" <<'EOF'
#!/bin/bash
if [[ ${1:-} == list ]]; then
    exit 42
fi
echo "$@" >> "$KX_CALL_LOG"
exit 0
EOF
    chmod +x "$home/stubbin/knowledge-ctx"

    echo "$home"
}

# run_co <home> <dir-under-home> [co args...] -> sets RC, CLAUDE_ARGS, CLAUDE_ENV, ERR
# Honours CO_ROLLOVER_SUCCESSOR and CO_ROLLOVER_PERMISSION_MODE from the
# caller's environment if set.
run_co() {
    local home=$1 rel=$2; shift 2
    local call_log env_log kx_log err_log
    call_log=$(mktemp); env_log=$(mktemp); kx_log=$(mktemp); err_log=$(mktemp)
    ( cd "$home/$rel" && HOME="$home" PATH="$home/stubbin:$PATH" \
      CLAUDE_CALL_LOG="$call_log" CLAUDE_ENV_LOG="$env_log" KX_CALL_LOG="$kx_log" \
      CO_ROLLOVER_SUCCESSOR="${CO_ROLLOVER_SUCCESSOR:-}" \
      CO_ROLLOVER_PERMISSION_MODE="${CO_ROLLOVER_PERMISSION_MODE:-}" \
      /bin/zsh "$CO" --root-config "$(co_root_config "$home")" "$@" >/dev/null 2>"$err_log" )
    RC=$?
    CLAUDE_ARGS=$(cat "$call_log")
    CLAUDE_ENV=$(cat "$env_log")
    ERR=$(cat "$err_log")
    rm -f "$call_log" "$env_log" "$kx_log" "$err_log"
}

# The stub records one argv element per line, so this pulls the value
# adjacent to --permission-mode rather than substring-matching.
_permission_mode_value() { # <argv-log-text> -> echoes the value following --permission-mode
    printf '%s\n' "$1" | awk '$0=="--permission-mode"{getline; print; exit}'
}

H=$(setup)

echo "== a rollover successor launches with the predecessor's current permission mode =="

CO_ROLLOVER_SUCCESSOR=1 CO_ROLLOVER_PERMISSION_MODE=plan run_co "$H" Convivy/vivy -r "$PROMPT"
assert_rc 0 "$RC" "marked successor + plan mode: co -r exits 0"
assert_eq "plan" "$(_permission_mode_value "$CLAUDE_ARGS")" \
    "marked successor + plan mode: claude launches with --permission-mode plan"
assert_eq "" "$CLAUDE_ENV" \
    "marked successor + plan mode: no rollover marker reaches claude's environment"

echo "== every known Claude Code mode is passed through unchanged =="

for mode in default acceptEdits bypassPermissions dontAsk; do
    CO_ROLLOVER_SUCCESSOR=1 CO_ROLLOVER_PERMISSION_MODE="$mode" run_co "$H" Convivy/vivy -r "$PROMPT"
    assert_rc 0 "$RC" "marked successor + $mode: co -r exits 0"
    assert_eq "$mode" "$(_permission_mode_value "$CLAUDE_ARGS")" \
        "marked successor + $mode: claude launches with --permission-mode $mode"
done

echo "== a missing mode falls back to bypassPermissions (today's behaviour) =="

CO_ROLLOVER_SUCCESSOR=1 run_co "$H" Convivy/vivy -r "$PROMPT"
assert_rc 0 "$RC" "marked successor, no mode captured: co -r exits 0"
assert_eq "bypassPermissions" "$(_permission_mode_value "$CLAUDE_ARGS")" \
    "marked successor, no mode captured: falls back to bypassPermissions"
assert_eq "" "$(echo "$ERR" | grep -F 'invalid permission mode')" \
    "marked successor, no mode captured: no fallback warning for a merely-missing mode"

echo "== an invalid mode falls back to bypassPermissions and says so =="

CO_ROLLOVER_SUCCESSOR=1 CO_ROLLOVER_PERMISSION_MODE="not-a-real-mode" run_co "$H" Convivy/vivy -r "$PROMPT"
assert_rc 0 "$RC" "marked successor + invalid mode: co -r exits 0"
assert_eq "bypassPermissions" "$(_permission_mode_value "$CLAUDE_ARGS")" \
    "marked successor + invalid mode: falls back to bypassPermissions"
assert_contains "$ERR" "invalid permission mode" \
    "marked successor + invalid mode: the fallback is announced on stderr"

echo "== a non-rollover launch ignores CO_ROLLOVER_PERMISSION_MODE entirely =="

CO_ROLLOVER_PERMISSION_MODE=plan run_co "$H" Convivy/vivy -r "$PROMPT"
assert_rc 0 "$RC" "unmarked launch + mode set: co -r exits 0"
assert_eq "bypassPermissions" "$(_permission_mode_value "$CLAUDE_ARGS")" \
    "unmarked launch + mode set: still launches with the fixed bypassPermissions"
assert_eq "" "$CLAUDE_ENV" \
    "unmarked launch + mode set: the marker does not reach claude's environment"

echo "== an ordinary co -r (no markers at all) is unaffected =="

run_co "$H" Convivy/vivy -r
assert_rc 0 "$RC" "ordinary co -r: exits 0"
assert_eq "bypassPermissions" "$(_permission_mode_value "$CLAUDE_ARGS")" \
    "ordinary co -r: still launches with bypassPermissions"
assert_eq "" "$CLAUDE_ENV" \
    "ordinary co -r: no rollover marker reaches claude's environment"

rm -rf "$H"
finish
