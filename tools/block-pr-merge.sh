#!/bin/bash
# PreToolUse(Bash) guard — agents never merge PRs (substrate ARCH-020 / ARCH-100).
#
# Wired into ~/.claude/settings.json as a user-level PreToolUse hook matching the
# Bash tool. It runs BEFORE every bash command in every `co`/claude session on
# this machine and DENIES any PR-merge command — even under bypassPermissions,
# and even if the operator said "ship it" / "merge it" in the session. Merging is a human
# action the operator performs themself (the GitHub UI); an agent running the merge is the
# exact gate ARCH-020 exists to hold, and prompt text alone proved bypassable
# (a vivy co read "ship it" as merge authorization and squash-merged, 2026-05-31).
#
# MENTION vs INVOCATION: the match is anchored to a command-segment boundary
# (start-of-string or after a real separator ; & ( ), so a command that merely
# MENTIONS the merge string in a commit message, echo, or doc edit passes, while
# an actual invocation is blocked. Pipe '|' is deliberately NOT a separator: you
# never pipe INTO a merge command, and treating '|' as one false-positives on
# grep alternation patterns like 'a\|gh pr merge'.
#
# Scope, deliberately narrow:
#   - the gh pr merge subcommand (the CLI path)
#   - an actual gh-api/curl/wget call hitting a merge endpoint:
#       REST  .../pulls/<n>/merge   or   GraphQL mergePullRequest
#   - a `git push` to the protected branch `main` FROM A DISPATCHED-AGENT
#     SESSION ONLY (gated on AGENTCTL_RUN_ID being set). Agents land changes via
#     a reviewed, CI-gated PR (substrate ARCH-103); a direct push to main skips
#     that gate, and the merge branches above leave it open. The interactive
#     steward / the operator session has AGENTCTL_RUN_ID unset and keeps direct-to-main
#     (the producer-based line, ARCH-103). GitHub branch protection can't draw
#     this line — agents, the steward, and the operator all push as the same GitHub
#     identity, so it would be all-or-nothing; this hook is the enforcement.
# It does NOT touch `git merge` (local branch merges are legitimate), nor
# gh pr view/checks/create, nor a bare endpoint token in prose (the REST/GraphQL
# branch requires an actual api/curl/wget command too), nor any non-main push,
# nor any push from a non-agent (steward/the operator) session.
#
# Fail-open by design: if stdin can't be parsed (e.g. jq missing), the hook lets
# the command through rather than blocking ALL bash. The agent-prompt layer
# (planning-orchestrator + ADR 0001) is the backstop. Bash 3.2 safe — runs under
# the mini's /bin/bash 3.2 (ARCH-082).
#
# Lives in the agent-platform repo (versioned + reviewable); referenced by
# absolute path from ~/.claude/settings.json, which is user config and not under
# git (like ~/.claude/CLAUDE.md — re-add this hook when provisioning a new box).

set -u

_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
. "$_HERE/../lib/safe-path.sh" 2>/dev/null || true

# Resolve jq from a fixed list of system directories (lib/safe-path.sh;
# never PATH). Fail open if absent.
JQ="$(safe_which jq 2>/dev/null || true)"
[ -z "$JQ" ] && exit 0   # can't parse → don't block anything

input="$(cat)"
cmd="$(printf '%s' "$input" | "$JQ" -r '.tool_input.command // empty' 2>/dev/null)"
[ -z "$cmd" ] && exit 0

# Normalize for segment matching: tab → space, newline → ';' (a real shell
# separator). Collapsing a newline to a space would erase the boundary, letting
# a merge on its own line slip past the anchor below.
norm="$(printf '%s' "$cmd" | tr '\t' ' ' | tr '\n' ';')"

# Command-segment boundary: start-of-string, or just after a real separator
# (; & or an opening paren). NOT '|' — see header.
SEG='(^|[;&(])[[:space:]]*'

deny() {
  "$JQ" -n --arg r "$1" \
    '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$r}}'
  exit 0
}

# 1. The gh CLI merge subcommand as an actual command (segment-anchored).
if printf '%s' "$norm" | grep -Eq "${SEG}gh[[:space:]]+pr[[:space:]]+merge"; then
  deny "BLOCKED by ARCH-020 (hard hook): agents never merge PRs. 'ship it' / 'merge it' is NOT authorization to run the merge yourself — surface the PR as ready (post the URL) and let the operator merge it in the GitHub UI. Then run the post-merge cycle once they have."
fi

# 2. An actual api/curl/wget call (segment-anchored) that hits a merge endpoint.
#    Both conditions required, so a prose token alone passes.
if printf '%s' "$norm" | grep -Eq "${SEG}(gh[[:space:]]+api|curl|wget)" \
   && printf '%s' "$norm" | grep -Eq '/pulls/[0-9]+/merge|mergePullRequest'; then
  deny "BLOCKED by ARCH-020 (hard hook): agents never merge PRs (REST/GraphQL merge endpoint). Surface the PR as ready; the operator merges in the GitHub UI."
fi

# 3. A direct push to main from a DISPATCHED-AGENT session (AGENTCTL_RUN_ID set).
#    The steward/the operator session has it unset → this branch is skipped → direct push
#    stays open for them (ARCH-103 producer-based line). The `main` token must be
#    a refspec word in the same push segment: matches `origin main`, `HEAD:main`,
#    `main:main`, `:main` (delete), `+main` (force). `[^;&|]*` keeps it inside the
#    one push segment, so `git push origin x | grep main` does not trip. Branch
#    names like `main-feature` / `feature-main` / `mainline` are NOT matched (the
#    `main` token must be bounded by space/colon/separator/end). A bare `git push`
#    (no refspec, current branch → upstream) is NOT caught — agents are prompted
#    to work on PR branches; documented residual. Same mention-in-prose strictness
#    as the merge branches above (a commit message naming the push at a segment
#    boundary is denied in agent sessions — reword, per the ARCH-100 decision).
if [ -n "${AGENTCTL_RUN_ID:-}" ]; then
  if printf '%s' "$norm" | grep -Eq "${SEG}git[[:space:]]+push[^;&|]*[[:space:]:+]main([[:space:]:;&]|\$)"; then
    deny "BLOCKED (ARCH-103, protect-main): a dispatched agent may not push to main. Push to a feature branch and open a PR — the reviewer + security-reviewer + CI gate it and the operator merges. (This session is a dispatched run: AGENTCTL_RUN_ID is set. main is steward/operator-only.)"
  fi
fi

exit 0
