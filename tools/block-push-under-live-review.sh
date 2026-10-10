#!/bin/bash
# PreToolUse(Bash) guard — do not push while a review is running against the
# commit that review is pinned to (substrate ARCH-374).
#
# THE PROBLEM. A review-class dispatch pins itself to an exact commit (ARCH-184)
# and its stamp is rejected if the head has moved since (ARCH-193, stale replay).
# Those two rules are individually right and compose into a trap: dispatch two
# reviewers, act on the first one's findings, push the fix — and the SECOND
# reviewer, still running, is now reviewing a commit that is no longer the head.
# It finishes, posts a thorough APPROVE, and the gate silently discards it.
#
# The review WAS done, on the right code, by the right agent, and it is thrown
# away. The cost is a wasted run plus a gate that reports "no review posted"
# while a complete review sits in the PR thread.
#
# The steward hit this THREE TIMES in one session (2026-07-19), the third time on
# a comment-only change, having written the rule down after the second. That is
# the signature of something that needs a mechanism rather than a habit: the
# knowledge does not prevent the mistake, because the mistake happens at a moment
# when you are thinking about the fix, not about the other reviewer.
#
# THE RULE, for an INTERACTIVE session only:
#   - `git push` while a live review run for this repo is pinned to a commit
#     that is NOT what you are about to push -> DENY, naming the run and the
#     two ways forward (wait for it, or kill it and re-dispatch after).
#   - anything else -> untouched.
#
# WHY DENY RATHER THAN WARN. A warning here is printed at the exact moment the
# author is focused on something else; that is how the first two happened. The
# escape hatch is explicit: kill the doomed run, or wait. Both are one command.
#
# SCOPE, deliberately narrow:
#   - Only a real `git push` invocation, anchored to a command-segment boundary,
#     so a command that merely MENTIONS the string in an echo, a commit message,
#     or a doc edit passes untouched. Same anchoring rule as block-pr-merge.sh.
#   - Only when AGENTCTL_RUN_ID is UNSET, i.e. an interactive co. A dispatched
#     worker pushing its own branch is not the situation this guards.
#   - Only when a live review run is pinned to a DIFFERENT commit. A review
#     pinned to exactly what you are pushing is not invalidated by pushing it.
#
# Fail-open by design: if stdin cannot be parsed, if jq or python is missing, or
# if the run registry cannot be read, the push is ALLOWED. A guard that can wedge
# every push on the machine is worse than the gap it closes.
# Bash 3.2 safe — runs under the mini's /bin/bash 3.2 (ARCH-082).

set -u

_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
. "$_HERE/../lib/safe-path.sh" 2>/dev/null || true

# Resolve jq from a fixed list of system directories (lib/safe-path.sh;
# never PATH). Fail open if absent.
JQ="$(safe_which jq 2>/dev/null || true)"
[ -z "$JQ" ] && exit 0   # can't parse -> never block

input="$(cat)"
cmd="$(printf '%s' "$input" | "$JQ" -r '.tool_input.command // empty' 2>/dev/null)"
[ -z "$cmd" ] && exit 0

# A dispatched agent pushing its own branch is not what this guards.
[ -n "${AGENTCTL_RUN_ID:-}" ] && exit 0

# Anchored to a command-segment boundary: `git push` at the start, or after ; & (
# — deliberately NOT after '|', matching block-pr-merge.sh, so `echo ... | grep
# "git push"` is untouched.
printf '%s' "$cmd" | grep -Eq '(^|[;&(][[:space:]]*)([^[:space:]]*/)?git[[:space:]]+push([[:space:]]|$)' || exit 0

# Resolve python3 from the same fixed list as jq above (lib/safe-path.sh;
# never PATH). Fail open if absent — same rationale as the jq resolution.
PY="$(safe_which python3 2>/dev/null || true)"
[ -z "$PY" ] && exit 0

verdict="$("$PY" "$(dirname "$0")/live-review-check.py" 2>/dev/null)"

[ -z "$verdict" ] && exit 0

"$JQ" -n --arg r "$verdict" \
  '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",
    permissionDecisionReason:$r}}'
exit 0
