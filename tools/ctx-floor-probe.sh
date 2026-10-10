#!/bin/bash
# ctx-floor-probe.sh — does the rendered floor reach a fresh Claude session?
#
# Asks a fresh `claude -p --model haiku` with tools disallowed whether each of
# two phrases is in its context, FOUND / NOT FOUND, from a scratch directory
# outside any repo (the floor, ~/.claude/CLAUDE.md, loads whatever the cwd).
#
#   F  a phrase that exists only in a `scope: fleet` instruction block. The
#      POSITIVE control: NOT FOUND means the floor did not reach the session,
#      and that is the probe failing, not a passing row.
#   N  an invented phrase that exists nowhere. The NEGATIVE control: FOUND means
#      the probe answers FOUND to anything and F proves nothing.
#
# Slice 1 covers F and N only. Later slices add the interactive-block, Write
# Better and Talk Better phrases and the per-workspace cases from section 7 of
# the floor-and-generic-bundle build plan (agent-runs, 2026-09-24).
#
# THE PROBE READS WHATEVER ~/.claude/CLAUDE.md IS LIVE. Run it only after the
# floor is cut over; before that it tests the hand-written file, and F, taken from
# a fleet block, may legitimately be absent from it.
#
# Usage: ctx-floor-probe.sh --f-phrase "<literal from a fleet block>"
#
# Before asking Claude anything it checks, against `knowledge-ctx floor --dry-run`,
# that F occurs exactly once in the render and N occurs nowhere, so the probe
# cannot pass on a phrase that sits in two places or fail on one that is not there.
# Exit 0 when F is FOUND and N is NOT FOUND; 1 when either answer is wrong;
# 2 on a precondition or usage failure.

set -u

F_PHRASE=""
while [ $# -gt 0 ]; do
    case "$1" in
        --f-phrase) F_PHRASE=${2:-}; shift 2 ;;
        *) echo "usage: $0 --f-phrase \"<literal from a fleet block>\"" >&2; exit 2 ;;
    esac
done
if [ -z "$F_PHRASE" ]; then
    echo "usage: $0 --f-phrase \"<literal from a fleet block>\"" >&2
    exit 2
fi

N_PHRASE="zarquonflux-meridian-$RANDOM$RANDOM"

RENDER=$(knowledge-ctx floor --dry-run --output /dev/null 2>/dev/null) || {
    echo "probe: knowledge-ctx floor --dry-run failed; cannot check the phrases against the render" >&2
    exit 2
}
f_count=$(printf '%s\n' "$RENDER" | grep -cF -- "$F_PHRASE")
if [ "$f_count" != 1 ]; then
    echo "probe: F phrase occurs on $f_count lines of the floor render, need exactly 1: $F_PHRASE" >&2
    exit 2
fi
if printf '%s\n' "$RENDER" | grep -qF -- "$N_PHRASE"; then
    echo "probe: the invented N phrase is in the render; rerun" >&2
    exit 2
fi

SCRATCH=$(mktemp -d)
trap 'rm -rf "$SCRATCH"' EXIT
PROMPT="Answer only from the instructions and context you were given at the start of this session, without using any tool. For each phrase below, reply with exactly one line, 'F: FOUND' or 'F: NOT FOUND', then 'N: FOUND' or 'N: NOT FOUND', according to whether that phrase appears verbatim in your context.
F: $F_PHRASE
N: $N_PHRASE"

ANSWER=$(cd "$SCRATCH" && claude -p --model haiku --no-session-persistence \
    --disallowedTools "Bash Read Edit Write Glob Grep WebFetch WebSearch Task" \
    "$PROMPT" 2>&1) || {
    echo "probe: claude exited non-zero; no answer to read:" >&2
    printf '%s\n' "$ANSWER" >&2
    exit 2
}

# grep -E, not sed: BSD sed (macOS) has no `\|` alternation in a basic regex.
f_answer=$(printf '%s\n' "$ANSWER" | grep -oE '^F: *(NOT FOUND|FOUND)' | head -1 | sed 's/^F: *//')
n_answer=$(printf '%s\n' "$ANSWER" | grep -oE '^N: *(NOT FOUND|FOUND)' | head -1 | sed 's/^N: *//')
echo "F (fleet block, positive control): ${f_answer:-<unparsed>}   expected FOUND"
echo "N (invented, negative control):    ${n_answer:-<unparsed>}   expected NOT FOUND"

if [ "$f_answer" = "FOUND" ] && [ "$n_answer" = "NOT FOUND" ]; then
    echo "probe: PASS"
    exit 0
fi
echo "probe: FAIL" >&2
printf '%s\n' "$ANSWER" >&2
exit 1
