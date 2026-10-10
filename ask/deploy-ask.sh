#!/bin/bash
# deploy-ask.sh — sync the canonical ask-surface config to the live ~/Convivy/ask/.
#
# The ask surface (PA ADR-0027 / substrate ARCH-233) lives at ~/Convivy/ask/, a
# bare repo-free directory whose transcripts the PA app ingests. Its config is
# versioned HERE (agent-platform/ask/) for review + recovery, then deployed as
# plain copies — the live dir stays repo-free and its transcript slug is stable.
#
# Mapping:
#   ask/CLAUDE.md   -> ~/Convivy/ask/CLAUDE.md
#   ask/skills/*    -> ~/Convivy/ask/.claude/skills/*   (Claude Code reads project
#                      skills from <cwd>/.claude/skills/; the canonical can't live
#                      under .claude/ here because agent-platform gitignores it)
#
# Idempotent. Run after editing the canonical, or on a fresh mini.
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
DST="$HOME/Convivy/ask"

mkdir -p "$DST/.claude/skills"
cp "$SRC/CLAUDE.md" "$DST/CLAUDE.md"
cp -R "$SRC/skills/." "$DST/.claude/skills/"
echo "Deployed ask config -> $DST"
echo "  CLAUDE.md + .claude/skills/$(ls "$SRC/skills" | tr '\n' ' ')"
