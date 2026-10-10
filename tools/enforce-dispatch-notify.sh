#!/bin/bash
# PreToolUse(Bash) guard — an interactive dispatch must be able to notify its
# caller (substrate ARCH-367).
#
# THE PROBLEM. `agentctl spawn` daemonizes: double-fork + setsid, deliberately
# out of the caller's process tree. Nothing can then tell the calling session the
# run finished — `_finalize_run` posts nothing, there is no background-completion
# hook, and Claude Code exposes no way to wake or inject into a session from
# outside (verified against the docs 2026-07-18, not assumed). So the ONLY
# completion signal available is a process the CALLER holds: a backgrounded
# `agentctl spawn --wait`, whose exit the caller's harness reports.
#
# Co's across every product sat idle after their reviewers had already finished
# (the operator, 2026-07-18). Printing a hint fixed the affordance but not the guarantee —
# guidance drifts. This hook is the guarantee.
#
# THE RULE, for an INTERACTIVE session only:
#   - `agentctl spawn` NOT backgrounded          -> DENY, with the correct form.
#   - `agentctl spawn` backgrounded, no --wait   -> ALLOW, rewritten to add --wait.
#   - anything else                              -> untouched.
# A dispatch that cannot notify its caller is therefore unissuable, rather than
# merely discouraged.
#
# WHY REWRITE RATHER THAN DENY THE SECOND CASE: `updatedInput` can change the
# command but CANNOT flip `run_in_background` (tested — the flipped value is
# ignored and the call still runs inline). So backgrounding is the one thing only
# the model can do, and it is the one thing we deny on; --wait we can simply add.
#
# SCOPE, deliberately narrow:
#   - Only a real `agentctl spawn` INVOCATION, anchored to a command-segment
#     boundary, so a command that merely MENTIONS the string in an echo, a commit
#     message, or a doc edit passes untouched. Same anchoring rule as
#     block-pr-merge.sh, and '|' is deliberately not a separator.
#   - Only when AGENTCTL_RUN_ID is UNSET, i.e. an interactive co. A dispatched
#     agent (overnight orchestrator, any worker that itself dispatches) is exempt:
#     under `claude -p` ending a turn ends the session, so those MUST block in the
#     foreground and background-waiting would abandon the run mid-flight
#     (ARCH-070). Getting this backwards would break the overnight drain.
#   - `--foreground` / `--attach` dispatches are exempt: they already block, so
#     the caller is already held until the run ends.
#   - `--dry-run` is exempt: nothing is dispatched to wait for.
#
# Fail-open by design: if stdin can't be parsed (jq missing, unexpected shape),
# the command is allowed through rather than blocking ALL bash on the machine.
# A guard that can wedge every session is worse than the gap it closes.
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

# A dispatched agent's own spawns are exempt — see header (ARCH-070).
[ -n "${AGENTCTL_RUN_ID:-}" ] && exit 0

# A heredoc makes this guard blind: its body is DATA, but it looks like command
# text, and a line inside it beginning `agentctl spawn` is indistinguishable from
# a real invocation by any regex we can afford here. Reasoning about it wrongly
# is worse than not reasoning: an un-backgrounded dispatch would be falsely
# denied, and a backgrounded one would have --wait spliced INTO the heredoc body,
# silently corrupting whatever file or message it writes. Writing a doc that
# quotes a dispatch is routine, so this is a common shape, not a corner case.
# Skip the whole command.
case "$cmd" in
  *"<<"*) exit 0 ;;
esac

# Normalize for segment matching: tab -> space, newline -> ';' (a real separator).
# Collapsing a newline to a space would erase the boundary and let a dispatch on
# its own line slip past the anchor.
norm="$(printf '%s' "$cmd" | tr '\t' ' ' | tr '\n' ';')"

SEG='(^|[;&(])[[:space:]]*'
printf '%s' "$norm" | grep -Eq "${SEG}(agentctl|[^[:space:]]*/agentctl)[[:space:]]+spawn([[:space:]]|$)" || exit 0

# Already blocking, or dispatching nothing at all — no waiter needed either way.
printf '%s' "$norm" | grep -Eq '(^|[[:space:]])--(foreground|attach|dry-run)([[:space:]]|=|$)' && exit 0

bg="$(printf '%s' "$input" | "$JQ" -r '.tool_input.run_in_background // false' 2>/dev/null)"

if [ "$bg" != "true" ]; then
  "$JQ" -n --arg r "BLOCKED by ARCH-367: a dispatch you cannot be notified about. \
\`agentctl spawn\` daemonizes the run out of your process tree, so nothing will tell you when it \
finishes — you would sit idle while the agent has already posted its review. Re-issue this exact \
command with the Bash tool's run_in_background parameter set to true; this hook will add --wait for \
you, and its completion is what wakes you. (Watching it live instead? Pass --foreground, which \
blocks and is exempt.)" \
    '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$r}}'
  exit 0
fi

# Backgrounded and real: ensure it blocks, so the task's exit means "run finished"
# rather than "dispatch returned". Adding --wait to a command that already has it
# would be harmless but noisy, so only add when absent.
if printf '%s' "$norm" | grep -Eq '(^|[[:space:]])--wait([[:space:]]|=|$)'; then
  exit 0
fi

# REWRITING IS THE DANGEROUS PATH, so it is the narrow one. A deny is visible and
# recoverable — the caller re-issues the command. A wrong rewrite mutates the
# command silently and the caller runs something it never wrote. So we only
# rewrite when the dispatch is unambiguously the command itself, and deny in
# every other case rather than guessing.
#
# Two invocations in one command is exactly such a case: the substitution would
# wait on the first and leave the second unwaited, which looks correct and is not.
count="$(printf '%s' "$norm" \
  | grep -oE "(^|[;&(])[[:space:]]*([^[:space:]]*/)?agentctl[[:space:]]+spawn([[:space:]]|\$)" \
  | wc -l | tr -d ' ')"

# The safe shape: the dispatch leads the command, optionally behind `cd … &&`
# style prefixes that contain no quote characters. A quote before the match means
# the match may sit INSIDE a string, where it is data, not a command.
prefix="$(printf '%s' "$norm" | sed -E \
  "s#(([^[:space:]]*/)?agentctl[[:space:]]+spawn([[:space:]]|\$)).*##")"
quotes="$(printf '%s' "$prefix" | tr -cd "\"'" | wc -c | tr -d ' ')"

if [ "$count" != "1" ] || [ "$quotes" != "0" ]; then
  "$JQ" -n --arg r "BLOCKED by ARCH-367: this dispatch needs a waiter, and the command is not a \
shape I can add one to safely (more than one dispatch, or the dispatch sits inside a quoted \
string). Issue the dispatch as its own command with --wait, backgrounded — one dispatch per \
command, so its completion is what wakes you." \
    '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$r}}'
  exit 0
fi

# Insert --wait immediately after the `spawn` token, NOT at the end of the command.
# Appending to the end breaks any compound: `agentctl spawn X | tail -3` would
# become `... | tail -3 --wait`, handing the flag to tail. Piping a dispatch to
# grep out its run-id is a common shape, so that is reachable, not theoretical.
# argparse accepts the flag before the agent positional, so insertion is safe.
rewritten="$(printf '%s' "$cmd" | sed -E \
  "s#(^|[;&(][[:space:]]*)(([^[:space:]]*/)?agentctl[[:space:]]+spawn)([[:space:]]|\$)#\1\2 --wait\4#")"

# If the substitution did not take (an anchoring the sed and the grep disagree
# on), emit nothing rather than a command we did not intend. Allowing an
# un-waited dispatch is a smaller failure than rewriting a command wrongly.
if [ "$rewritten" = "$cmd" ]; then
  exit 0
fi

"$JQ" -n --arg c "$rewritten" \
  '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"allow",
    updatedInput:{command:$c},
    permissionDecisionReason:"ARCH-367: added --wait so this backgrounded dispatch blocks until the run finishes; its completion is what notifies you."}}'
exit 0
