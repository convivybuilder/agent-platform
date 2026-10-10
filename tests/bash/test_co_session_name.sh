#!/bin/bash
# Tests for bin/co passing `claude --name` (the remote picker's row title).
#
# WHAT THIS IS FOR. With `--name` unset the picker falls back to a
# conversation-derived summary — "Restamp", "Merged", "Situation assessment" —
# accurate about the last thing said and useless for telling one crew from
# another. The row's second line shows branch and device, which is `main` on the
# same machine for every crew by construction, so it can never distinguish
# anything either. The operator could not tell their sessions apart on their phone.
#
# NOT THE SAME FIELD as CLAUDE_REMOTE_CONTROL_SESSION_NAME_PREFIX, which co has
# set for a while: that namespaces the remote-control CONNECTION, and exists
# because unset, claude falls back to the hostname and every row read `mini-…`.
#
# `co` is zsh; bash invoking it here is how a login shell will.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

CO="$REPO_ROOT/bin/co"

setup() { # -> echoes fake HOME
    local home d
    home=$(mktemp -d)
    mkdir -p "$home/stubbin"
    for d in Convivy/library Convivy/agent-platform Convivy/convivy-orient \
             Convivy/CFO; do
        mkdir -p "$home/$d/.claude/agents"
        : > "$home/$d/.claude/agents/scout.md"
        ( cd "$home/$d" && git init -q . )
    done
    mkdir -p "$home/Convivy/ask"          # deliberately not a repo
    cat > "$home/stubbin/claude" <<'EOF'
#!/bin/bash
printf '%s\n' "$@" > "$CLAUDE_CALL_LOG"
exit 0
EOF
    chmod +x "$home/stubbin/claude"
    # Exit code is driven per-case: the generic new-project tail is reached only
    # when `knowledge-ctx list` exits 42 (the "profile has no agent for this
    # project" signal), so a stub that always returns 0 never gets there — which
    # is why the first version of the generic-tail test logged no argv at all.
    cat > "$home/stubbin/knowledge-ctx" <<'EOF'
#!/bin/bash
exit ${KX_STATUS:-0}
EOF
    chmod +x "$home/stubbin/knowledge-ctx"
    echo "$home"
}

# run_co_in <home> <dir> [env assignments...] -> sets CLAUDE_ARGS
run_co_in() {
    local home=$1 rel=$2; shift 2
    local call_log; call_log=$(mktemp)
    ( cd "$home/$rel" && HOME="$home" PATH="$home/stubbin:$PATH" \
      CLAUDE_CALL_LOG="$call_log" "$@" /bin/zsh "$CO" --root-config "$(co_root_config "$home")" ${CO_EXTRA_ARGS:-} >/dev/null 2>&1 )
    CLAUDE_ARGS=$(cat "$call_log")
    rm -f "$call_log"
}

# The stub prints one argv element per line, so a name and its flag land on two
# ADJACENT lines. Matching the pair is what proves the value reached `--name`
# rather than merely appearing somewhere in argv — a name that is also a model
# string or a path would otherwise pass a bare grep.
assert_name() { # <expected> <dir> <home>
    local want=$1 rel=$2 home=$3
    run_co_in "$home" "$rel"
    local got
    got=$(printf '%s\n' "$CLAUDE_ARGS" | grep -A1 -x -- '--name' | tail -1)
    assert_eq "$want" "$got" "$rel launches with --name '$want'"
}

H=$(setup)

assert_name "Bosun"   "Convivy/agent-platform" "$H"
assert_name "Library" "Convivy/library"        "$H"
assert_name "Orient"  "Convivy/convivy-orient" "$H"
assert_name "CFO"     "Convivy/CFO"            "$H"

# Ask with no topic: the bare crew name is all there is.
assert_name "ask" "Convivy/ask" "$H"

# ASK IS THE ONE CASE A CREW NAME DOES NOT IDENTIFY. The eight crews get one row
# each; ask gets one per topic, so four rows reading "ask" reproduces the very
# problem this change fixes, one level down. /newchat already carries the topic
# in the prefix it exports.
run_co_in "$H" "Convivy/ask" env CLAUDE_REMOTE_CONTROL_SESSION_NAME_PREFIX=ask-new-session-audio-setup
got=$(printf '%s\n' "$CLAUDE_ARGS" | grep -A1 -x -- '--name' | tail -1)
assert_eq "Ask · audio setup" "$got" "an ask topic reaches the picker row"

# A HOSTILE TOPIC IS DROPPED, NOT RENDERED. The label reaches tmux's status-left,
# which is a FORMAT STRING — `#(cmd)` in one runs through /bin/sh whenever the
# status line redraws with a client attached (verified against the live exploit;
# a detached session never renders, so it is easy to "test" this and see
# nothing). /newchat's instruction to slugify the topic is prose addressed to an
# assistant, not a guard, and the ask surface reads Gmail, Drive and Slack into
# the session that runs it — so the value can be shaped by someone else's content.
run_co_in "$H" "Convivy/ask" env \
    "CLAUDE_REMOTE_CONTROL_SESSION_NAME_PREFIX=ask-new-session-x #(touch /tmp/co-injection-marker)"
got=$(printf '%s\n' "$CLAUDE_ARGS" | grep -A1 -x -- '--name' | tail -1)
assert_eq "ask" "$got" "a topic that is not a plain slug is dropped entirely"
assert_eq "0" "$(printf '%s' "$CLAUDE_ARGS" | grep -c '#(' || true)" \
    "no tmux format sequence survives into the session name"

# THE TWO PATHS THAT WERE ACTUALLY BROKEN, exercised end to end rather than
# inferred from the source. Cookbook and the generic new-project tail call no
# _apply_co_style, so both launched with no name at all until review caught it.
# A static check alone would not have; these run the real script.
mkdir -p "$H/Convivy/Cookbook/.claude/agents"
: > "$H/Convivy/Cookbook/.claude/agents/scout.md"
( cd "$H/Convivy/Cookbook" && git init -q . )
assert_name "Cookbook" "Convivy/Cookbook" "$H"

# No individuated agent, so `_co_generic_agent_mds` is empty and this falls to
# the generic tail — Deck's New Project path. A brand-new project would
# otherwise be the one launch with no identity in the picker, which is when
# telling sessions apart matters most.
mkdir -p "$H/Convivy/brand-new-thing"
( cd "$H/Convivy/brand-new-thing" && git init -q . )
run_co_in "$H" "Convivy/brand-new-thing" env KX_STATUS=42
got=$(printf '%s\n' "$CLAUDE_ARGS" | grep -A1 -x -- '--name' | tail -1)
assert_eq "brand-new-thing" "$got" \
    "the generic new-project tail launches with the project's own name"

# THE SHAPE THE STRUCTURAL CHECK COULD NOT SEE. `knowledge` is a real repo with
# individuated agents that matches no arm of the final `case`, so it reached
# `exec` with the name array still empty and launched unnamed — the third
# instance of this bug in this PR, after Cookbook and the generic tail, and the
# first one hiding inside a `case` rather than at block level.
mkdir -p "$H/Convivy/knowledge/.claude/agents"
: > "$H/Convivy/knowledge/.claude/agents/reviewer.md"
( cd "$H/Convivy/knowledge" && git init -q . )
# `rev` because this directory is not a crew dir: a bare `co` there fails agent
# resolution before it ever reaches the exec, so the branch under test is only
# entered with an agent that is actually individuated. `rev` maps to `reviewer`.
CO_EXTRA_ARGS=rev
assert_name "knowledge" "Convivy/knowledge" "$H"
unset CO_EXTRA_ARGS

# AN EMPTY NAME MUST DROP THE FLAG, NOT SHIFT THE LINE. The array is spliced
# unquoted, and zsh drops an empty word from an unquoted array rather than
# passing it as "" — so (--name "") contributes only --name, every following
# word shifts left, and --name eats the value of --model, leaving the model
# string as a stray positional claude reads as the initial prompt. A broken
# launch, not a cosmetic blank. Reachable from any directory with no git repo,
# where repo_root is empty and so is the basename taken from it.
call_log=$(mktemp)
( cd "$H" && HOME="$H" PATH="$H/stubbin:$PATH" \
  CLAUDE_CALL_LOG="$call_log" KX_STATUS=42 /bin/zsh "$CO" --root-config "$(co_root_config "$H")" >/dev/null 2>&1 )
argv=$(cat "$call_log")
assert_eq "0" "$(printf '%s\n' "$argv" | grep -c -x -- '--name' || true)" \
    "no --name flag at all when the label would be empty"
assert_eq "1" "$(printf '%s\n' "$argv" | grep -c -x -- '--model' || true)" \
    "--model keeps its own value rather than being eaten by an empty --name"
rm -f "$call_log"

rm -rf "$H"

# STRUCTURAL, BY CONTROL FLOW RATHER THAN FILE ORDER. The first version of this
# counted `$_co_name_args` tokens on exec lines, which cannot tell a name that is
# set from one that is always EMPTY — and two sites were exactly that, Cookbook
# and the generic new-project tail, neither of which calls `_apply_co_style` at
# all. They were missed because the PR mapped each exec to its nearest preceding
# style call IN FILE ORDER; every top-level block ends in `exec`, so only one
# block ever runs and a call in an earlier branch is not reachable from a later
# one. Proximity is not control flow.
#
# WHAT THIS CHECK STILL CANNOT SEE, stated because it has had a blind spot in
# every round so far and the next reader should not assume otherwise: it tracks
# `case`/`esac` depth but NOT `if`/`fi`, so a setter sitting inside a bare `if`
# with no `else` would satisfy it while the untaken branch still launched
# unnamed. Nothing does that today — every setter is either top level or set on
# both arms — and it verifies a statement's PRESENCE, never that the value it
# produces is non-empty, which is a separate property the empty-name assertions
# below cover directly.
#
# So: each block that execs must itself either call `_apply_co_style` or set the
# name directly. A sixteenth site added in a new branch fails here.
flow=$(python3 - "$CO" <<'PY'
# A name must be set on a path the block ALWAYS takes. Counting a match anywhere
# between the block start and the exec is what missed `knowledge`: its only
# `_apply_co_style` calls sit inside `case` arms with no `*)`, so a repo matching
# no arm reaches exec unnamed while the text still appears in the span. Lines
# inside `case ... esac` are therefore ignored, and only an unconditional setter
# counts.
import re, sys
lines = open(sys.argv[1]).read().splitlines()
blocks, cur = [], None
for i, l in enumerate(lines, 1):
    if re.match(r"^(if|elif) ", l):
        cur = i
    if "exec claude --remote-control" in l:
        blocks.append((cur, i))
bad = []
for st, e in blocks:
    depth, ok = 0, False
    for l in lines[st - 1:e]:
        stripped = l.strip()
        if stripped.startswith("#"):
            continue
        if re.match(r"^case\b", stripped):
            depth += 1
        elif re.match(r"^esac\b", stripped):
            depth -= 1
        elif depth == 0 and re.search(r"_apply_co_style\s|_co_set_name\s", l):
            ok = True
    if not ok:
        bad.append(e)
print(len(bad))
PY
)
assert_eq "0" "$flow" "every exec block sets a session name on its own branch"

echo "  $_PASS passed, $_FAIL failed"
[ "$_FAIL" -eq 0 ]
