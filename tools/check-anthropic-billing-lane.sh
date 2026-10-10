#!/bin/bash
# SessionStart guard — warn when a Claude Code session is set to bill the Anthropic
# API instead of the Max subscription (substrate ARCH-146 / the env-guard residual).
#
# THE LEAK (ARCH-146): when ANTHROPIC_API_KEY is present in a `co`/`claude`
# session's environment and there is NO gateway ANTHROPIC_BASE_URL, Claude Code
# authenticates with that API key and bills Anthropic **console credits** instead
# of the Max **subscription** — silently. ccusage counts tokens regardless of the
# billing lane, so #agent-cost shows a "subscription estimate" and the leak stays
# invisible until the monthly bill. The interactive co fleet billed PA's key 43.4M
# tokens in 3 days this way before it was caught.
#
# FIRE CONDITION: ANTHROPIC_API_KEY set AND ANTHROPIC_BASE_URL unset. This is the
# exact leak signature. The two legitimate states are silent:
#   - clean subscription session: ANTHROPIC_API_KEY unset (→ subscription OAuth);
#   - gateway lane (`co -m deepseek|kimi|gpt5`, ARCH-132): key set WITH a base URL
#     (a litellm virtual key routed to 127.0.0.1:4000 — bills neither console nor
#     subscription incorrectly, it routes to the gateway on purpose).
# A default `co`/`claude` launch sets neither var, so a dirty inherited key with no
# base URL is the only way to trip this — which is precisely what we want to catch.
#
# WHY A HOOK, not a co preflight: the leak persisted on the raw `claude` /
# remote-control / FleetView path that BYPASSES the co wrapper (co's gateway-lane
# block at bin/co:96-97 is the only place base URL gets set). A SessionStart hook
# fires inside Claude Code on EVERY launch path — co, raw claude, FleetView,
# agentctl-spawned workers — so it covers the path co cannot. The custom
# Anthropic-SDK apps (PA, the constance/vivy answers bots) are NOT Claude Code and
# do not run Claude Code hooks, so they are excluded automatically — correctly, as
# an SDK app MUST use a raw API key and legitimately bills the API.
#
# OUTPUT: a SessionStart hook. The JSON on stdout (hookSpecificOutput.additional
# Context) injects the warning into the assistant's context so it surfaces to the
# operator on the first turn; the stderr banner is a best-effort live echo. Always
# exit 0 — this WARNS, it never blocks a session (a false block would lock the
# operator out).
#
# No external dependency (the warning JSON is a static literal — no jq/python), so
# the guard cannot fail a session by a missing tool. Bash 3.2 safe (ARCH-082).
#
# Lives in the agent-platform repo (versioned + reviewable); referenced by absolute
# path from ~/.claude/settings.json, which is user config and not under git (like
# block-pr-merge.sh and ~/.claude/CLAUDE.md — re-add this hook when provisioning a
# new box).

set -u

if [ -n "${ANTHROPIC_API_KEY:-}" ] && [ -z "${ANTHROPIC_BASE_URL:-}" ]; then
  # Live banner (best-effort; stderr).
  printf '%s\n' "⚠️  BILLING-LANE WARNING (ARCH-146): ANTHROPIC_API_KEY is set but ANTHROPIC_BASE_URL is unset — this Claude Code session bills the Anthropic API (console credits), NOT the Max subscription. If unintended: exit, open a FRESH shell (not 'exec zsh -l' — that re-inherits the key), and relaunch via 'co'." >&2

  # Inject into the assistant's context (static JSON literal — no escaping hazards,
  # the message below contains no double quotes or backslashes).
  printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"BILLING-LANE WARNING (ARCH-146): ANTHROPIC_API_KEY is set in this Claude Code session with no ANTHROPIC_BASE_URL, so it is billing the Anthropic API (console credits) instead of the Max subscription. Surface this to the user immediately and recommend exiting and relaunching via co from a fresh shell (not exec zsh -l, which re-inherits the key). The gateway lane co -m deepseek|kimi|gpt5 is fine and would not trip this."}}'
fi

exit 0
