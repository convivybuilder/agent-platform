#!/bin/bash
# Tests for tools/block-pr-merge.sh — the PreToolUse(Bash) guard.
# Black-box: feed crafted tool-input JSON on stdin (+ env), assert the hook's
# permission decision. Covers the ARCH-020/100 merge block AND the ARCH-103
# protect-main agent-push block (gated on AGENTCTL_RUN_ID).
#
# The hook fails OPEN without jq, so deny-assertions would spuriously pass-as-
# allow; skip the whole suite (still exit 0) when jq is absent rather than emit
# false greens. CI (macos-latest) has jq.
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

HOOK="$REPO_ROOT/tools/block-pr-merge.sh"

if ! command -v jq >/dev/null 2>&1; then
    echo "  SKIP: jq absent — hook fails open, decisions not exercised"
    finish
    exit 0
fi

# decision <command> -> echoes "deny" or "allow". Env (e.g. AGENTCTL_RUN_ID) is
# inherited from the caller, so set/unset it on the calling line.
decision() {
    local out dec
    out=$(jq -n --arg c "$1" '{tool_input:{command:$c}}' | /bin/bash "$HOOK" 2>/dev/null)
    # Empty output (allow) → jq yields nothing → default to "allow".
    dec=$(printf '%s' "$out" | jq -r '.hookSpecificOutput.permissionDecision // "allow"' 2>/dev/null)
    [ -z "$dec" ] && dec=allow
    echo "$dec"
}

# --- merge block (existing behavior, locked) ---
assert_eq deny  "$(decision 'gh pr merge 5 --squash')"            "gh pr merge is denied"
assert_eq deny  "$(decision 'gh api -X PUT repos/o/r/pulls/5/merge')" "REST merge endpoint denied"
assert_eq allow "$(decision 'gh pr view 5')"                       "gh pr view passes"
assert_eq allow "$(decision 'git merge feature')"                  "local git merge passes"
assert_eq allow "$(decision 'echo gh pr merge 5')"                 "merge mentioned in echo passes"

# --- protect-main: agent session (AGENTCTL_RUN_ID set) ---
export AGENTCTL_RUN_ID=run-testxx
assert_eq deny  "$(decision 'git push origin main')"               "agent push to main denied"
assert_eq deny  "$(decision 'git push origin HEAD:main')"          "agent push HEAD:main denied"
assert_eq deny  "$(decision 'git push origin :main')"              "agent delete main denied"
assert_eq deny  "$(decision 'git push -f origin main')"            "agent force-push main denied"
assert_eq deny  "$(decision 'git add -A; git push origin main')"   "agent push main after ; denied"
assert_eq allow "$(decision 'git push origin phase-c-pr1')"        "agent push to PR branch passes"
assert_eq allow "$(decision 'git push origin main-feature')"       "branch main-feature not matched"
assert_eq allow "$(decision 'git push origin feature-main')"       "branch feature-main not matched"
assert_eq allow "$(decision 'git push origin mainline')"           "branch mainline not matched"
assert_eq allow "$(decision 'git push origin foo | grep main')"    "main after pipe not a push target"
assert_eq allow "$(decision 'echo "git push origin main"')"        "push to main mentioned in echo passes"
unset AGENTCTL_RUN_ID

# --- protect-main: steward/operator session (AGENTCTL_RUN_ID unset) keeps direct-to-main ---
assert_eq allow "$(env -u AGENTCTL_RUN_ID /bin/bash -c '
    out=$(jq -n --arg c "git push origin main" "{tool_input:{command:\$c}}" | /bin/bash "'"$HOOK"'" 2>/dev/null)
    case "$out" in *deny*) echo deny;; *) echo allow;; esac')" \
    "steward (no AGENTCTL_RUN_ID) push to main passes"

finish
