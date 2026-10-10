#!/bin/bash
# generic-bundle-context.sh — SessionStart hook: deliver the generic instruction
# bundle to a session whose workspace has no knowledge-ctx render.
#
# Prints Claude Code's SessionStart hook JSON,
#   {"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"..."}}
# holding `knowledge-ctx bundle --generic`, and nothing when a render exists.
#
# A render exists when `.claude/.active-profile` or `.claude/.profile-documents.json`
# is present in cwd or in its git toplevel. THE TEST IS THE MANIFEST, NOT THE
# `.claude` DIRECTORY: convivy-app-kit, convivy-services, hollis and write-better
# carry hand-written `.claude` trees and no render, so a directory test would
# starve exactly the workspaces this bundle exists for.
#
# A broken bundle must never block a session from starting: any failure prints
# nothing to stdout, one line to stderr, and exits 0. bash 3.2 only (ARCH-082).
#
# Usage: generic-bundle-context.sh [<part>]   (part: blocks, write-better,
# talk-better; none = the whole bundle). One hook per part keeps each under
# Claude Code's hook-context limit. THE ONE FAILURE THAT EXITS 1 is
# knowledge-ctx refusing a part for size, so the refusal is visible; exit 2 has
# its own meaning for a SessionStart hook and is never used. Every other
# failure, an unknown part included, still exits 0.

set -u

# Default to the knowledge-ctx beside this script's repo, not the one on PATH: a
# session started without ~/.local/bin on PATH would otherwise get no bundle
# silently. The script is usually reached through a symlink, so follow it by hand
# (`readlink -f` does not exist on macOS). KNOWLEDGE_CTX overrides.
self=$0
while [ -L "$self" ]; do
    target=$(readlink "$self")
    case $target in
        /*) self=$target ;;
        *) self=$(dirname "$self")/$target ;;
    esac
done
KCTX=${KNOWLEDGE_CTX:-$(cd "$(dirname "$self")/.." && pwd)/bin/knowledge-ctx}

has_manifest() {
    [ -e "$1/.claude/.active-profile" ] || [ -e "$1/.claude/.profile-documents.json" ]
}

cwd=$(pwd)
top=$(git -C "$cwd" rev-parse --show-toplevel 2>/dev/null || true)
if has_manifest "$cwd" || { [ -n "$top" ] && has_manifest "$top"; }; then
    exit 0
fi

errf=$(mktemp "${TMPDIR:-/tmp}/generic-bundle.XXXXXX" 2>/dev/null || echo /dev/null)
# ${1:+...} adds --part only when a part was given, and keeps it one quoted word.
bundle=$("$KCTX" bundle --generic ${1:+--part "$1"} 2>"$errf")
rc=$?
# The last non-empty stderr line names the cause; keep the hook's own report to one line.
cause=$(grep . "$errf" 2>/dev/null | tail -n 1 | tr -d '\r')
[ "$errf" = /dev/null ] || rm -f "$errf"
if [ "$rc" -ne 0 ] || [ -z "$bundle" ]; then
    echo "generic-bundle-context: knowledge-ctx bundle --generic${1:+ --part $1} failed (exit $rc) or printed nothing${cause:+: $cause}; no bundle delivered" >&2
    # A size refusal is the one exit 1. knowledge-ctx gives it the generic exit
    # code, so the phrase in its stderr line is what tells it from other failures.
    case $cause in
        *"-byte cap; refusing"*) exit 1 ;;
    esac
    exit 0
fi

if ! out=$(printf '%s' "$bundle" | python3 -c '
import json, sys
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": sys.stdin.read()}}))
' 2>/dev/null); then
    echo "generic-bundle-context: could not encode the bundle as JSON; no bundle delivered" >&2
    exit 0
fi
printf '%s\n' "$out"
exit 0
