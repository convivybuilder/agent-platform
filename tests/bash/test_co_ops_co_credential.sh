#!/bin/bash
# Tests for the co-only credential's delivery in bin/co. A crew co launch passes the credential
# path in the --mcp-config bin/co gives the claude it execs, which is the ops server's source for
# it. bin/co exports the variable nowhere. The claude's environment carries it only when the shell
# that ran `co` exported it by hand; a crew launch from a clean shell therefore gives no descendant
# (the co's Bash tool, a `claude -p` it starts, a tmux server) the path. The path is in claude's
# argv, where `ps` shows it; it is not a secret. A private crew launch and an ask launch carry
# neither the config nor the variable, even when their parent shell exported one. Ask is excluded
# by directory (<root>/ask and anything under it), not by the private flag: it is a black-box crew
# the registry does not mark private, because the account connectors it needs are stripped by
# --strict-mcp-config.
#
# The file name is left out of this test on purpose: tests/test_ops_co_credential_confined.py
# allows only bin/co and bin/mcp-ops-server to name it. So the check is that the path sits under
# the orient-control directory and not at the agent token, which pins the delivery without
# repeating the name.
#
# `co` is zsh; bash invokes it here, as a login shell would.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

CO="$REPO_ROOT/bin/co"

_is_true() { # <command...> -> echoes 1 when it succeeds, 0 when it fails
    if "$@"; then echo 1; else echo 0; fi
}

_has() { # <needle> <text> -> echoes 1 when text contains needle as a fixed string, 0 otherwise
    if printf '%s\n' "$2" | grep -Fq -- "$1"; then echo 1; else echo 0; fi
}

_matches() { # <value> <glob> -> echoes 1 when value matches the glob, 0 otherwise
    case "$1" in
        $2) echo 1 ;;
        *) echo 0 ;;
    esac
}

H=$(mktemp -d)
mkdir -p "$H/stubbin" "$H/Convivy/library/.claude/agents" "$H/Convivy/ask/sub" \
         "$H/Convivy/ask-notes" "$H/Convivy/CFO/.claude/agents"
: > "$H/Convivy/library/.claude/agents/scout.md"
( cd "$H/Convivy/library" && git init -q . )
( cd "$H/Convivy/CFO" && git init -q . )

# The claude stub records its argv and the inherited credential variable, one per line.
cat > "$H/stubbin/claude" <<'EOF'
#!/bin/bash
printf '%s\n' "$@" > "$CLAUDE_CALL_LOG"
printf '%s\n' "${ORIENT_OPS_CO_TOKEN_FILE-unset}" > "$CLAUDE_ENV_LOG"
exit 0
EOF
chmod +x "$H/stubbin/claude"
# bin/co runs issues-waiting in the co shell, before it execs claude, so this stub
# records what the shell itself carries.
cat > "$H/stubbin/issues-waiting" <<'EOF'
#!/bin/bash
printf '%s\n' "${ORIENT_OPS_CO_TOKEN_FILE-unset}" > "$CO_SHELL_LOG"
exit 0
EOF
chmod +x "$H/stubbin/issues-waiting"
# Every tmux call records the variable it sees, so a server that inherited it would show here.
cat > "$H/stubbin/tmux" <<'EOF'
#!/bin/bash
printf '%s\n' "${ORIENT_OPS_CO_TOKEN_FILE-unset}" >> "$TMUX_CALL_LOG"
exit 0
EOF
chmod +x "$H/stubbin/tmux"
cat > "$H/stubbin/knowledge-ctx" <<'EOF'
#!/bin/bash
exit "${KX_EXIT:-0}"
EOF
chmod +x "$H/stubbin/knowledge-ctx"

call_log=$(mktemp)
env_log=$(mktemp)
shell_log=$(mktemp)
tmux_log=$(mktemp)

run_co() { # <rel> [inherited value] -> fills call_log, env_log, shell_log and tmux_log
    local rel=$1 inherited=${2-}
    rm -f "$call_log" "$env_log" "$shell_log"
    : > "$tmux_log"
    (
        cd "$H/$rel" || exit 1
        unset CO_DEFAULT_MODEL ORIENT_OPS_CO_TOKEN_FILE
        if [ -n "$inherited" ]; then export ORIENT_OPS_CO_TOKEN_FILE="$inherited"; fi
        # bin/co styles the window only inside tmux (TMUX, and a pane to target), so the
        # launch runs as a co shell does; the stub tmux answers for the server.
        HOME="$H" PATH="$H/stubbin:$PATH" CLAUDE_CALL_LOG="$call_log" \
            CLAUDE_ENV_LOG="$env_log" CO_SHELL_LOG="$shell_log" TMUX_CALL_LOG="$tmux_log" \
            TMUX="$H/tmux-socket,1,0" TMUX_PANE=%0 \
            /bin/zsh "$CO" --root-config "$(co_root_config "$H")" >/dev/null 2>&1
    )
}

echo "== a crew co launch passes the credential path to its ops server, in the MCP config =="
run_co "Convivy/library"
assert_eq 1 "$(_is_true [ -s "$call_log" ])" "claude was reached (positive control)"
assert_eq 1 "$(_is_true grep -qx -- '--mcp-config' "$call_log")" \
    "the launch passes --mcp-config"
config=$(sed -n 2p "$call_log")
assert_eq 1 "$(_has '"mcpServers"' "$config")" \
    "the MCP config is the value that follows --mcp-config (positive control)"
assert_eq 1 "$(_has '"ops":{' "$config")" "the MCP config names the ops server"
assert_eq 1 "$(printf '%s' "$config" | /usr/bin/python3 -c 'import json, sys; json.load(sys.stdin)' \
    >/dev/null 2>&1 && echo 1 || echo 0)" "the MCP config parses as JSON"
assert_eq 1 "$(_has "\"ORIENT_OPS_CO_TOKEN_FILE\":\"$H/.orient-control/" "$config")" \
    "the MCP config's ops server gets the credential path under the orient-control directory"
assert_eq 0 "$(_has "\"ORIENT_OPS_CO_TOKEN_FILE\":\"$H/.orient-control/ops-mcp-token\"" "$config")" \
    "the credential path is not the agent token file"
assert_eq 1 "$(_has '"VAULTS_ON_LIBRARY":"1"' "$config")" \
    "the MCP config pins the Library plane, as the user-scope entry does"

echo "== a crew launch from a clean shell gives claude no path, so no descendant inherits one =="
assert_eq "unset" "$(cat "$env_log")" \
    "the claude environment carries no credential path"

echo "== the co shell and every tmux call it makes carry no path =="
assert_eq 1 "$(_is_true [ -s "$shell_log" ])" "the co shell reached issues-waiting (positive control)"
assert_eq "unset" "$(cat "$shell_log")" \
    "the co shell does not export the credential path before its tmux calls"
assert_eq 1 "$(_is_true [ -s "$tmux_log" ])" "tmux was called (positive control)"
assert_eq 0 "$(_is_true grep -qv '^unset$' "$tmux_log")" \
    "every tmux call sees no credential path"

echo "== an ask launch carries neither the config nor the variable, even one its parent shell exported =="
run_co "Convivy/ask" "$H/inherited-co-credential"
assert_eq 1 "$(_is_true [ -s "$call_log" ])" "ask: claude was reached (positive control)"
assert_eq "unset" "$(cat "$env_log")" \
    "ask: the claude environment carries no credential path"
assert_eq 0 "$(_is_true grep -qx -- '--mcp-config' "$call_log")" \
    "ask: the launch passes no --mcp-config"

echo "== an ask launch denies every tool from the ops and kb servers at launch =="
ask_denials=$(grep '^--disallowedTools=' "$call_log")
assert_eq 1 "$(_matches "$ask_denials" '*mcp__ops*')" \
    "ask: the launch denies the ops server's tools"
assert_eq 1 "$(_matches "$ask_denials" '*mcp__kb*')" \
    "ask: the launch denies the kb server's tools"

echo "== a subdirectory of ask gets ask's treatment, even where knowledge-ctx finds no project =="
# knowledge-ctx exits 42 here, so the launch reaches the generic tail if ask's check misses it.
export KX_EXIT=42
run_co "Convivy/ask/sub" "$H/inherited-co-credential"
unset KX_EXIT
assert_eq 1 "$(_is_true [ -s "$call_log" ])" "ask/sub: claude was reached (positive control)"
assert_eq "unset" "$(cat "$env_log")" \
    "ask/sub: the claude environment carries no credential path"
assert_eq 0 "$(_is_true grep -qx -- '--mcp-config' "$call_log")" \
    "ask/sub: the launch passes no --mcp-config"
sub_denials=$(grep '^--disallowedTools=' "$call_log")
assert_eq 1 "$(_matches "$sub_denials" '*mcp__ops*')" \
    "ask/sub: the launch denies the ops server's tools"
assert_eq 1 "$(_matches "$sub_denials" '*mcp__kb*')" \
    "ask/sub: the launch denies the kb server's tools"

echo "== a sibling of ask such as ask-notes is not treated as ask: _co_in_ask_tree requires the slash =="
# knowledge-ctx exits 42 here, so a directory that is not under ask reaches the generic tail,
# the same branch ask/sub reached before _co_in_ask_tree existed. This is the negative control
# for the prefix-match risk the glob "$ask"* would have: ask-notes starts with the string "ask"
# but is not a path under it.
export KX_EXIT=42
run_co "Convivy/ask-notes" "$H/inherited-co-credential"
unset KX_EXIT
assert_eq 1 "$(_is_true [ -s "$call_log" ])" "ask-notes: claude was reached (positive control)"
assert_eq 1 "$(_is_true grep -qx -- '--mcp-config' "$call_log")" \
    "ask-notes: the launch passes --mcp-config, the generic tail's credential config"
notes_config=$(sed -n 2p "$call_log")
assert_eq 1 "$(_has "\"ORIENT_OPS_CO_TOKEN_FILE\":\"$H/.orient-control/" "$notes_config")" \
    "ask-notes: the MCP config's ops server gets the credential path, as the generic tail's does"
assert_eq 0 "$(_is_true grep -q '^--disallowedTools=' "$call_log")" \
    "ask-notes: the launch passes no --disallowedTools"

echo "== a crew launch passes no tool denial =="
run_co "Convivy/library"
assert_eq 1 "$(_is_true [ -s "$call_log" ])" "library: claude was reached (positive control)"
assert_eq 0 "$(_is_true grep -q '^--disallowedTools=' "$call_log")" \
    "library: the launch passes no --disallowedTools"

echo "== a private crew launch carries neither the config nor the variable, even one its parent shell exported =="
run_co "Convivy/CFO" "$H/inherited-co-credential"
assert_eq 1 "$(_is_true [ -s "$call_log" ])" "CFO: claude was reached (positive control)"
assert_eq 1 "$(_is_true grep -qx -- '--strict-mcp-config' "$call_log")" \
    "CFO: the launch is the private one (--strict-mcp-config present)"
assert_eq 0 "$(_is_true grep -qx -- '--mcp-config' "$call_log")" \
    "CFO: the launch passes no --mcp-config"
assert_eq "unset" "$(cat "$env_log")" \
    "CFO: the claude environment carries no credential path"

rm -f "$call_log" "$env_log" "$shell_log" "$tmux_log"
rm -rf "$H"

finish
