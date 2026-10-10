#!/bin/bash
# PostToolUse nudge — the working-on line goes stale at moments the machine can
# already see, so say so THEN rather than on a timer (the operator, 2026-08-27).
#
# THE MEASUREMENT THAT SHAPED THIS. Across the fleet on 2026-08-27: 18 co
# sessions live in tmux, 7 had ever recorded a working-on line, the freshest of
# those was 7.9 hours old, and the re-announce path — same text, refreshed
# liveness, `started_at` left alone — had been used ZERO times since it was
# built. So the line is not merely drifting; for most sessions it does not exist.
#
# WHY NOT A TIMER, which was the first idea and is the obvious one. An hourly
# "btw, update your line" fires on a clock uncorrelated with when the work
# changed, and it asks the co to notice the very thing it just failed to notice.
# Worse, it corrupts the data it means to improve: a co told to UPDATE rewrites
# the text, and any new text resets `started_at`, so a nagged fleet would report
# "how long has this been the work" as "time since the last nag". The zero
# re-announces above are the evidence — nobody repeats text verbatim
# unprompted, and a prompt makes rewording more likely, not less.
#
# WHAT THIS DOES INSTEAD. It fires on the commands that already CARRY the
# answer — a dispatch's `--task`, a PR's `--title`, an epic checkout's title —
# and prints two facts side by side: what just happened, and what the line says
# plus its age. It does not judge whether they match; it cannot, and a wrong
# guess about relatedness would be worse than none. The co reads both strings
# and decides, which is the one comparison it is actually well placed to make.
#
# SILENT WHENEVER THE LINE IS FRESH, so a co that sets its line and then
# dispatches four reviewers is not nudged four times. The threshold is shared
# with the status-line segment in `bin/co-session-health`; a test pins the two
# to the same value, because a drift between them would make one surface say
# "stale" while the other stayed quiet, and nobody would know which was right.
#
# Fail-open, like every guard here: an unparseable payload, no jq, no tmux, or
# an unreadable record all exit silently. This is a nicety, and a nicety that
# can wedge a session is not one.
# Bash 3.2 safe — runs under the mini's /bin/bash 3.2 (ARCH-082).

set -u

# Paired with WORKING_ON_STALE_S in bin/co-session-health. See header.
STALE_S=1800

_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
. "$_HERE/../lib/safe-path.sh" 2>/dev/null || true

# Resolve jq from a fixed list of system directories (lib/safe-path.sh;
# never PATH). Fail open if absent.
JQ="$(safe_which jq 2>/dev/null || true)"
[ -z "$JQ" ] && exit 0

# Bounded read. The heredoc short-circuit below catches the largest realistic
# payloads, but a very long single-line command would otherwise cost per-turn
# latency proportional to its length across several sed and jq passes. 256KB is
# far above any real tool call and far below anything worth waiting for.
input="$(head -c 262144)"
[ -z "$input" ] && exit 0

# A dispatched worker has no working-on line and no cockpit pane — nudging it
# would be noise aimed at nobody. Same exemption enforce-dispatch-notify.sh
# takes, and for the same reason.
[ -n "${AGENTCTL_RUN_ID:-}" ] && exit 0

# Nothing is keyed outside tmux, so there is nothing honest to compare against.
[ -z "${TMUX:-}" ] && exit 0
name="$(tmux display-message -p '#S' 2>/dev/null)" || exit 0
[ -z "$name" ] && exit 0
key="$(printf '%s' "$name" | LC_ALL=C tr -cd 'A-Za-z0-9_-' | cut -c1-64)"
[ -z "$key" ] && exit 0

record="$HOME/.cache/co-session-health/working-${key}.json"

# --- what just happened -------------------------------------------------------
#
# ONE SHAPE, and it reads structured input rather than guessing from text. The
# `.tool_input.command` extraction that used to sit here went with the Bash
# branch: nothing downstream consumed it, and leaving a dangling read of
# attacker-influenced input is how the next reader comes to wonder what depends
# on it. Both round-5 reviewers found it; it is gone.
tool="$(printf '%s' "$input" | "$JQ" -r '.tool_name // empty' 2>/dev/null)"
event=""

case "$tool" in
  *issue_checkout|*issue_claim)
    title="$(printf '%s' "$input" \
      | "$JQ" -r '(.tool_response // {}) | if type=="string" then . else tojson end' 2>/dev/null \
      | "$JQ" -R -r 'fromjson? // {} | .. | objects | select(has("title")) | .title' 2>/dev/null \
      | head -1)"
    # CAPPED LIKE ITS SIBLINGS, and for a sharper reason than they have. A
    # `--task` and a PR title are composed by this session; an ISSUE TITLE is
    # written by anyone who can file, and this branch injects it into the
    # model's context. The co already sees that title unfiltered in the tool
    # result, so this adds no exposure — but it re-presents it as the harness
    # narrating, which carries more apparent authority than a tool result does.
    # Bounding the length bounds how much attacker-shaped text one title can
    # carry; the `untrusted title:` framing below is what stops it reading as
    # the harness's own words.
    # FLATTENED BEFORE CAPPING, because the cap bounds length and not shape. A
    # label calling text untrusted is worth little if the text can appear to end
    # the quotation it sits in.
    #
    # THREE THINGS ACT HERE AND THEY ARE NOT THE SAME ONE — measured, because
    # the first version of this comment credited `tr` with all of it:
    #   - `jq --arg` escapes the value so it can never break the JSON envelope.
    #   - `head -1` on the extraction above truncates a title at its first
    #     newline. That is a SIDE EFFECT of its real job, picking one result out
    #     of a multi-object jq walk — not a defence anyone built, and a rewrite
    #     of that extraction would remove it silently.
    #   - `tr`'s `\000-\037` range covers newline (012) and carriage return
    #     (015) as well as tab — verified with `od`, not assumed from the range.
    #     So the newline is defended TWICE and only the first defence fires:
    #     `head -1` truncates before `tr` ever sees it. That redundancy is the
    #     point, since it means a rewrite of the extraction cannot silently
    #     remove the protection.
    # Both are asserted separately below: a tab, which only `tr` handles, and a
    # multi-line title end to end through the real pipeline.
    if [ -n "$title" ]; then
      flat="$(printf '%s' "$title" | LC_ALL=C tr '\000-\037\177' '    ' | cut -c1-90)"
      event="claimed an epic whose untrusted title reads: \"${flat}\""
    fi
    ;;
  # THE BASH BRANCH IS GONE, AND ITS ABSENCE IS THE FIX.
  #
  # This hook used to also fire on `agentctl spawn --task "…"` and
  # `gh pr create --title "…"` read out of a Bash command string. Four review
  # rounds found five distinct defects in that reading, and every one came from
  # the same place — deciding what a shell command DID by matching regexes
  # against its text:
  #
  #   1. a substring test fired on any command MENTIONING the phrase
  #   2. rejecting on any quote SWALLOWED real dispatches after a quoted argument
  #   3. parity over the prefix broke on an apostrophe in an earlier segment
  #   4. parity over the last segment FABRICATED a dispatch, because a `;` inside
  #      a still-open quote reads as a boundary once the opening quote is stripped
  #   5. and with the gate finally correct, the `--task` extraction was still
  #      GREEDY — it took the last occurrence in the command while the gate
  #      judged the first, so `agentctl spawn --help; echo "…; agentctl spawn
  #      attacker --task "…""` narrated a dispatch of "attacker" that never ran
  #
  # Fixes 1-4 each closed one shape and opened another. That is the signature of
  # an approach that cannot be finished, not of four unlucky bugs.
  #
  # WHY IT MATTERS MORE HERE THAN IN A LINTER. The text this parsed can carry an
  # issue title, a PR body or file contents, and whatever it produced was injected
  # into the model's context as the harness's own account of what just happened.
  # A rule an attacker can drive into inventing events is worse than no rule; a
  # missed nudge is only a missed reminder about work the co is doing anyway.
  #
  # THE RIGHT PLACE FOR THIS SIGNAL IS NOT A HOOK READING COMMAND TEXT. A dispatch
  # is an event the platform already knows it performed — `agentctl spawn` could
  # say so directly, with the agent and task as structured fields, and no parsing
  # anywhere. That is a change to the dispatch path rather than to this file, and
  # it is the shape to build if the dispatch nudge is wanted back.
  #
  # What remains below reads STRUCTURED input: a title out of a tool response's
  # JSON, by key, with no guessing about what the caller did.
esac

[ -z "$event" ] && exit 0

# --- what the line says -------------------------------------------------------
if [ ! -f "$record" ]; then
  "$JQ" -n --arg e "$event" --arg s "$name" \
    '{hookSpecificOutput:{hookEventName:"PostToolUse",additionalContext:
      ("You just \($e), and session \"\($s)\" has NEVER recorded what it is working on — "
       + "Deck'"'"'s cockpit shows this session as blank. Set it with:  co-working-on \"<what you are doing>\"")}}'
  exit 0
fi

written="$("$JQ" -r '.written_at // empty' "$record" 2>/dev/null)"
text="$("$JQ" -r '.text // empty' "$record" 2>/dev/null)"
# A stamp must be digits with AT MOST ONE dot. The charset test alone admits
# "1.2.3", which then reaches the arithmetic below as `1.2` after `%.*` strips
# only the last component — a bash arithmetic error rather than a number. Say
# nothing rather than guess, and rather than fail loudly in a hook that must not.
case "$written" in
  ''|*[!0-9.]*) exit 0 ;;
  *.*.*)        exit 0 ;;
esac

now="$(date +%s)"
age=$(( now - ${written%.*} ))
[ "$age" -lt "$STALE_S" ] && exit 0

if [ "$age" -lt 3600 ]; then agestr="$((age / 60))m"
elif [ "$age" -lt 86400 ]; then agestr="$((age / 3600))h"
else agestr="$((age / 86400))d"; fi

# BOTH STRINGS, NO VERDICT. The hook does not decide whether these describe the
# same work — it cannot, and being wrong about that is worse than being silent.
# It also does not say "update": re-announcing the SAME text is the correct move
# when the work has not changed, and rewording it would reset the duration.
"$JQ" -n --arg e "$event" --arg t "$text" --arg a "$agestr" \
  '{hookSpecificOutput:{hookEventName:"PostToolUse",additionalContext:
    ("You just \($e). Your working-on line, set \($a) ago, still says: \"\($t)\". "
     + "If the work has moved on, say so:  co-working-on \"<what you are doing now>\". "
     + "If it has NOT, re-run it with that same text verbatim — that refreshes liveness "
     + "without resetting how long this has been the work.")}}'
exit 0
