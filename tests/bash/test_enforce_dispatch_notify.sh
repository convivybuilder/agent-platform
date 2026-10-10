#!/bin/bash
# Tests for tools/enforce-dispatch-notify.sh — the PreToolUse(Bash) guard that
# makes an un-notifiable dispatch unissuable (ARCH-367).
#
# Black-box: feed crafted tool-input JSON on stdin (+ env), assert the decision
# and, where the hook rewrites, the resulting command.
#
# The hook fails OPEN without jq, so deny-assertions would spuriously pass-as-
# allow; skip the suite rather than emit false greens. CI (macos-latest) has jq.
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

HOOK="$REPO_ROOT/tools/enforce-dispatch-notify.sh"

if ! command -v jq >/dev/null 2>&1; then
    echo "  SKIP: jq absent — hook fails open, decisions not exercised"
    finish
    exit 0
fi

# decision <command> [background] -> "deny" | "allow" | "pass"
# "pass" means the hook emitted nothing at all: the call is untouched, which is
# distinct from an explicit "allow" that carries a rewrite.
decision() {
    local out bg=${2:-false}
    out=$(jq -n --arg c "$1" --argjson b "$bg" \
          '{tool_input:{command:$c, run_in_background:$b}}' | /bin/bash "$HOOK" 2>/dev/null)
    [ -z "$out" ] && { echo pass; return; }
    printf '%s' "$out" | jq -r '.hookSpecificOutput.permissionDecision // "pass"' 2>/dev/null
}

# rewritten <command> -> the command the hook substitutes (empty if none)
rewritten() {
    jq -n --arg c "$1" --argjson b true '{tool_input:{command:$c, run_in_background:$b}}' \
      | /bin/bash "$HOOK" 2>/dev/null \
      | jq -r '.hookSpecificOutput.updatedInput.command // ""' 2>/dev/null
}

SPAWN='agentctl spawn reviewer --on agent-platform --pr 155 --task "review it"'

# --- the core rule: interactive, not backgrounded -> denied ------------------
unset AGENTCTL_RUN_ID
assert_eq deny  "$(decision "$SPAWN")"                    "un-backgrounded dispatch denied"
assert_eq deny  "$(decision "cd /tmp && $SPAWN")"         "denied after && too"
assert_eq deny  "$(decision "$SPAWN; echo done")"         "denied when first in a chain"
assert_eq deny  "$(decision "$HOME/.local/bin/agentctl spawn coder --on vivy")" \
                                                          "full-path agentctl denied"

# --- backgrounded -> allowed, with --wait added ------------------------------
assert_eq allow "$(decision "$SPAWN" true)"               "backgrounded dispatch allowed"
assert_eq 'agentctl spawn --wait reviewer --on agent-platform --pr 155 --task "review it"' \
                "$(rewritten "$SPAWN")"                   "--wait inserted after the spawn token"

# THE BUG THIS PINS. --wait used to be appended to the END of the whole command,
# so a piped dispatch handed the flag to the wrong program. Piping a dispatch to
# pull its run-id out is a common shape, so this was reachable, not theoretical.
assert_eq 'agentctl spawn --wait reviewer --on vivy | tail -3' \
                "$(rewritten 'agentctl spawn reviewer --on vivy | tail -3')" \
                                                          "pipe: flag goes to spawn, not tail"
assert_eq 'cd /tmp && agentctl spawn --wait coder --on vivy' \
                "$(rewritten 'cd /tmp && agentctl spawn coder --on vivy')" \
                                                          "prefix compound rewrites correctly"
assert_eq 'agentctl spawn --wait coder --on vivy; echo done' \
                "$(rewritten 'agentctl spawn coder --on vivy; echo done')" \
                                                          "trailing segment untouched"
assert_eq "$HOME/.local/bin/agentctl spawn --wait coder --on vivy" \
                "$(rewritten "$HOME/.local/bin/agentctl spawn coder --on vivy")" \
                                                          "full-path invocation rewrites correctly"

# --- backgrounded and already correct -> untouched ---------------------------
assert_eq pass  "$(decision "$SPAWN --wait" true)"        "existing --wait left alone"

# --- exempt: already blocking, or dispatching nothing ------------------------
assert_eq pass  "$(decision "$SPAWN --foreground")"       "--foreground exempt (already blocks)"
assert_eq pass  "$(decision "$SPAWN --attach")"           "--attach exempt"
assert_eq pass  "$(decision "$SPAWN --dry-run")"          "--dry-run exempt (nothing dispatched)"

# --- exempt: a dispatched agent must block in the FOREGROUND (ARCH-070) ------
# Getting this backwards would have the overnight orchestrator background its
# waits, end its turn, and abandon every worker mid-flight.
export AGENTCTL_RUN_ID=run-testxx
assert_eq pass  "$(decision "$SPAWN")"                    "agent session exempt, un-backgrounded"
assert_eq pass  "$(decision "$SPAWN" true)"               "agent session exempt, backgrounded"
unset AGENTCTL_RUN_ID

# --- mention vs invocation ---------------------------------------------------
assert_eq pass  "$(decision 'echo "run agentctl spawn reviewer"')" "mention in echo passes"
assert_eq pass  "$(decision 'git commit -m "use agentctl spawn now"')" "mention in commit msg passes"
assert_eq pass  "$(decision 'grep -r "agentctl spawn" docs/')"    "mention in grep passes"

# --- other agentctl subcommands are none of this hook's business -------------
assert_eq pass  "$(decision 'agentctl status run-abc')"           "status passes"
assert_eq pass  "$(decision 'agentctl wait run-abc')"             "wait passes"
assert_eq pass  "$(decision 'agentctl review-post --pr 1')"       "review-post passes"
assert_eq pass  "$(decision 'agentctl spawner --thing')"          "spawn as a prefix does not match"

# --- unrelated commands ------------------------------------------------------
assert_eq pass  "$(decision 'ls -la')"                            "ordinary command passes"
assert_eq pass  "$(decision 'gh pr view 155')"                    "gh passes"

# --- heredocs: the body is DATA, so the whole command is skipped -------------
# THE BUG THIS PINS (reviewer, PR #156). A line inside a heredoc beginning
# `agentctl spawn` is indistinguishable from a real invocation by regex. Before
# this, such a command was DENIED when not backgrounded and silently CORRUPTED
# when backgrounded — --wait spliced into the heredoc body, mangling whatever
# file or message it wrote. Writing a doc that quotes a dispatch is routine.
HEREDOC='cat >> notes.md <<EOF
agentctl spawn reviewer --on vivy
EOF'
assert_eq pass  "$(decision "$HEREDOC")"                  "heredoc body not denied"
assert_eq pass  "$(decision "$HEREDOC" true)"             "heredoc body not rewritten"
assert_eq ""    "$(rewritten "$HEREDOC")"                 "heredoc body left byte-identical"

# --- ambiguous shapes are DENIED, never silently rewritten -------------------
# A deny is visible and recoverable; a wrong rewrite runs a command nobody wrote.
assert_eq deny  "$(decision 'agentctl spawn a --on x; agentctl spawn b --on y' true)"                                                           "two dispatches denied, not half-fixed"
assert_eq deny  "$(decision 'echo "; agentctl spawn x" && agentctl spawn y --on z' true)"                                                           "dispatch behind a quote is denied"

# --- fail-open on unparseable input -----------------------------------------
# A guard that can wedge every bash call on the machine is worse than the gap.
out=$(printf 'not json at all' | /bin/bash "$HOOK" 2>/dev/null)
assert_eq "" "$out" "unparseable stdin emits nothing (fails open)"
out=$(jq -n '{tool_input:{}}' | /bin/bash "$HOOK" 2>/dev/null)
assert_eq "" "$out" "missing command emits nothing (fails open)"

finish
