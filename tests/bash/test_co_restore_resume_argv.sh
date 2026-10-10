#!/bin/bash
# The command line `co restore` types into a resumed pane, run through bin/co's real
# argument parsing. Before this test, co_restore.py's tests recorded the typed argv
# from a stubbed `co`, so a form bin/co could not parse (`co --resume <id> <prompt>`,
# which read `--resume` as an agent name) passed every test while the crew never
# started. Here the argv comes from co_restore.co_command itself, the line is typed
# into zsh with `co` resolving to the real bin/co, and only `claude` is a stub, so
# the assertions are on what claude is handed.
#
# restore types no --root-config, because bin/co's root is the machine's real root.
# This test prepends `--root-config <sandbox>` before the typed arguments. bin/co
# reads that flag before its first positional, so the rest of the line is unchanged.
#
# Cases, each a crew shape restore can meet:
#   1. no agent, an unrecognised directory          -> the generic tail
#   2. no agent, selector-backend (a profiled dir)  -> the profiled branch, which
#      takes only a bare `co` or a leading `/` prompt as its own
#   3. coder, an unrecognised directory             -> the agent path
#   4. coder, selector-backend                      -> the profiled branch must NOT take it
#   5. no agent, an individuated directory          -> the agent path with the default agent
#
# claude, knowledge-ctx and session-agent are stubs. Nothing reaches a live tmux
# server, a real checkout or the knowledge index: bin/co only reads tmux under $TMUX,
# and this runner unsets TMUX and TMUX_PANE.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

CO="$REPO_ROOT/bin/co"
LIB="$REPO_ROOT/lib/agent-platform"
SID="0a1b2c3d-0000-4000-8000-000000000042"

# typed_args <agent-or-empty>: the arguments co_restore.co_command gives `co`, one line.
typed_args() {
    python3 -c '
import shlex, sys
sys.path.insert(0, sys.argv[1])
import co_restore as CR
agent = sys.argv[3] or None
t = CR.Transcript(session_id=sys.argv[2], project_dir="x", mtime=0.0, entrypoint="cli",
                  cwd=None, title=None, agent=agent)
print(shlex.join(CR.co_command(t)[1:]))
' "$LIB" "$SID" "$1"
}

# setup <home>: a stub bin with `co` linked to the real bin/co, a stub claude that
# records its argv one per line, and a stub knowledge-ctx that answers $MOCK_KC_EXIT.
setup() {
    local home=$1
    mkdir -p "$home/stubbin" "$home/Convivy/brandnew" "$home/Convivy/selector-backend"
    ln -s "$CO" "$home/stubbin/co"
    cat > "$home/stubbin/claude" <<'EOF'
#!/bin/bash
printf '%s\n' "$@" > "$CLAUDE_CALL_LOG"
exit 0
EOF
    cat > "$home/stubbin/knowledge-ctx" <<'EOF'
#!/bin/bash
exit "${MOCK_KC_EXIT:-42}"
EOF
    chmod +x "$home/stubbin/claude" "$home/stubbin/knowledge-ctx"
    # selector-backend is a git checkout: bin/co's profiled branch matches the git toplevel.
    git init -q "$home/Convivy/selector-backend"
}

# run_restore <home> <cwd-name> <agent-or-empty>: types the restore line into zsh.
# Sets RC, ARGV (claude's argv, one per line) and STDERR.
run_restore() {
    local home=$1 cwd=$2 agent=$3 args call_log err_log
    args=$(typed_args "$agent")
    call_log="$home/claude.argv"
    err_log="$home/stderr"
    rm -f "$call_log" "$err_log"
    ( cd "$home/Convivy/$cwd" && env -u TMUX -u TMUX_PANE \
        HOME="$home" PATH="$home/stubbin:$PATH" CLAUDE_CALL_LOG="$call_log" \
        MOCK_KC_EXIT="${MOCK_KC_EXIT:-42}" \
        /bin/zsh -c "co --root-config '$(co_root_config "$home")' $args" \
        >/dev/null 2>"$err_log" )
    RC=$?
    ARGV=$(cat "$call_log" 2>/dev/null || true)
    STDERR=$(cat "$err_log")
}

# value_after <flag>: the argv entry that follows <flag>, or nothing.
value_after() {
    printf '%s\n' "$ARGV" | awk -v f="$1" 'p { print; exit } $0 == f { p = 1 }'
}

has_arg() { # <arg>
    printf '%s\n' "$ARGV" | grep -qxF -- "$1" && echo yes || echo no
}

# --- 1. no agent, unrecognised directory: the generic tail ------------------------
H=$(mktemp -d); setup "$H"
MOCK_KC_EXIT=42 run_restore "$H" brandnew ""
assert_rc 0 "$RC" "no agent, unrecognised directory: co exits 0 ($(echo "$STDERR" | head -1))"
assert_eq "$SID" "$(value_after --resume)" "no agent, unrecognised directory: claude gets --resume <session id>"
assert_eq "no" "$(has_arg --agent)" "no agent, unrecognised directory: claude gets no --agent"
rm -rf "$H"

# --- 2. no agent, selector-backend: the profiled branch -------------------------
H=$(mktemp -d); setup "$H"
MOCK_KC_EXIT=0 run_restore "$H" selector-backend ""
assert_rc 0 "$RC" "no agent, selector-backend: co exits 0 ($(echo "$STDERR" | head -1))"
assert_eq "$SID" "$(value_after --resume)" "no agent, selector-backend: claude gets --resume <session id>"
assert_eq "no" "$(has_arg --agent)" "no agent, selector-backend: claude gets no --agent"
rm -rf "$H"

# --- 3. coder, unrecognised directory: the agent path ----------------------------
H=$(mktemp -d); setup "$H"
mkdir -p "$H/Convivy/brandnew/.claude/agents"
: > "$H/Convivy/brandnew/.claude/agents/coder.md"
MOCK_KC_EXIT=42 run_restore "$H" brandnew coder
assert_rc 0 "$RC" "coder, unrecognised directory: co exits 0 ($(echo "$STDERR" | head -1))"
assert_eq "coder" "$(value_after --agent)" "coder, unrecognised directory: claude gets --agent coder"
assert_eq "$SID" "$(value_after --resume)" "coder, unrecognised directory: claude gets --resume <session id>"
rm -rf "$H"

# --- 4. coder, selector-backend: the profiled branch does not take an agent crew --
H=$(mktemp -d); setup "$H"
mkdir -p "$H/Convivy/selector-backend/.claude/agents"
: > "$H/Convivy/selector-backend/.claude/agents/coder.md"
MOCK_KC_EXIT=0 run_restore "$H" selector-backend coder
assert_rc 0 "$RC" "coder, selector-backend: co exits 0 ($(echo "$STDERR" | head -1))"
assert_eq "coder" "$(value_after --agent)" "coder, selector-backend: claude gets --agent coder"
assert_eq "$SID" "$(value_after --resume)" "coder, selector-backend: claude gets --resume <session id>"
rm -rf "$H"

# --- 5. no agent, an individuated directory: the default agent, as a bare prompt --
H=$(mktemp -d); setup "$H"
mkdir -p "$H/Convivy/brandnew/.claude/agents"
: > "$H/Convivy/brandnew/.claude/agents/planning-orchestrator.md"
MOCK_KC_EXIT=42 run_restore "$H" brandnew ""
assert_rc 0 "$RC" "no agent, individuated directory: co exits 0 ($(echo "$STDERR" | head -1))"
assert_eq "planning-orchestrator" "$(value_after --agent)" \
    "no agent, individuated directory: claude gets the default agent, as a leading / prompt does"
assert_eq "$SID" "$(value_after --resume)" "no agent, individuated directory: claude gets --resume <session id>"
rm -rf "$H"

finish
