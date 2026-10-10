#!/bin/bash
# Tests for the rollover-successor launch path (issue_01M2F7ZVFYH2WXFC6BX8GCW2ZN).
#
# THE BUG. `co_rollover.spawn_successor` launches the successor as
# `co -r "/handoff-pickup <path>"`, one argv word. Three branches of bin/co
# parse their first positional argument with `case "${1:-}" in ... *) agent=$1
# ;; esac` — the `*-commercial` branch, the company-level `convivy-commercial`
# branch, and the generic product-branch tail. All three read that whole
# prompt string as an AGENT NAME, so `.claude/agents/<the-whole-prompt>.md`
# never exists and the launch fails "not individuated" instead of rolling
# over. Fixed by treating a leading `/` as "this is a prompt, not an agent
# name" in all three case statements.
#
# THE ADJACENT RISK. Every `-r` branch that calls `knowledge-ctx use <profile>
# --force` runs it while the PREDECESSOR's pane is still live in the same cwd
# — not this process's ancestor, so ARCH-023's live-process guard cannot
# exempt it, and none of bin/co's call sites pass --exempt-pid (only agentctl
# does). `_co_reindividuate` skips that call when co_rollover.spawn_successor's
# CO_ROLLOVER_SUCCESSOR marker is set, so a rollover successor never races that
# guard; the knowledge pull still happens either way.
#
# THE MARKER-LEAK CLASS (round 3, PR #574). Round 2 scoped the marker to the
# `co` command itself (not the fallback shell) and had `_co_reindividuate`
# unset it — but only about half of bin/co's launch branches ever call that
# helper. Every self-contained branch (hollis, selector, convivy-app-kit,
# convivy-deck, write-better, nyh-architecture, Cookbook/cookbook-app, and the
# brand-new-project generic tail below) `exec`s claude directly and never
# reached the unset, so the marker rode into the successor's whole `claude`
# process tree on any of those. The fix moved the unset to the very top of
# bin/co, before any branch can run, so it no longer matters whether a given
# branch calls `_co_reindividuate` at all.
#
# `co` is zsh (excluded from the ARCH-082 bash-3.2 contract by lint.sh's
# shebang filter); bash invoking it here is exactly how a login shell will.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

CO="$REPO_ROOT/bin/co"
PROMPT="/handoff-pickup /x/handoff.md"

# A fake HOME with a real ~/Convivy/knowledge tracking a real bare origin (the
# `-r` path pulls it before individuating, so a fake would abort first and
# every assertion below would pass for the wrong reason), plus the three
# fixtures the parse fix and the rollover marker both need to reach:
# constance-commercial (`*-commercial`), convivy-commercial (its own branch),
# and vivy (the generic product-branch tail, which requires cwd's basename be
# in its own `-r` allowlist).
setup() { # -> echoes fake HOME
    local home
    home=$(mktemp -d)
    mkdir -p "$home/stubbin"
    write_kb_sync_stub "$home/stubbin"

    git init -q --bare "$home/kb-remote.git"
    git init -q "$home/Convivy/knowledge"
    (
        cd "$home/Convivy/knowledge" || exit 1
        git config user.email t@example.com
        git config user.name Test
        echo seed > seed.txt
        git add seed.txt
        git commit -qm seed
        git branch -M main
        git remote add origin "$home/kb-remote.git"
        git push -q -u origin main
    ) >/dev/null 2>&1

    mkdir -p "$home/Convivy/constance-commercial/.claude/agents"
    for a in product-manager product-analyst some-other-agent; do
        : > "$home/Convivy/constance-commercial/.claude/agents/$a.md"
    done
    ( cd "$home/Convivy/constance-commercial" && git init -q . )

    mkdir -p "$home/Convivy/convivy-commercial/.claude/agents"
    : > "$home/Convivy/convivy-commercial/.claude/agents/positioning-strategist.md"
    ( cd "$home/Convivy/convivy-commercial" && git init -q . )

    mkdir -p "$home/Convivy/vivy/.claude/agents"
    : > "$home/Convivy/vivy/.claude/agents/planning-orchestrator.md"
    # The predecessor's individuation, as knowledge-ctx writes it
    # (`<project>/<profile>\n`). _co_reindividuate's skips only apply when
    # this is present and names the launched profile; the tests near the end
    # rewrite or remove it.
    echo "vivy/daytime" > "$home/Convivy/vivy/.claude/.active-profile"
    ( cd "$home/Convivy/vivy" && git init -q . )

    # A brand-new, unrecognized directory — no .claude/agents/*.md, matches no
    # named-product branch — to reach the generic tail (bin/co's Deck
    # New Project path), which `exec`s claude directly with no
    # `_co_reindividuate` call in the way at all.
    mkdir -p "$home/Convivy/brand-new-project"
    ( cd "$home/Convivy/brand-new-project" && git init -q . )

    # Records argv AND whether CO_ROLLOVER_SUCCESSOR/CO_ROLLOVER_EXEMPT_PID
    # reached this process — `_co_reindividuate` (or, for the pid, the top of
    # bin/co directly) is supposed to consume both markers before `exec
    # claude` ever runs, so a leak shows up here as a non-empty env log line.
    cat > "$home/stubbin/claude" <<'EOF'
#!/bin/bash
printf '%s\n' "$@" > "$CLAUDE_CALL_LOG"
: > "$CLAUDE_ENV_LOG"
if [[ -n ${CO_ROLLOVER_SUCCESSOR+set} ]]; then
    echo "CO_ROLLOVER_SUCCESSOR=${CO_ROLLOVER_SUCCESSOR}" >> "$CLAUDE_ENV_LOG"
fi
if [[ -n ${CO_ROLLOVER_EXEMPT_PID+set} ]]; then
    echo "CO_ROLLOVER_EXEMPT_PID=${CO_ROLLOVER_EXEMPT_PID}" >> "$CLAUDE_ENV_LOG"
fi
exit 0
EOF
    chmod +x "$home/stubbin/claude"
    # Appends its argv as one line, so a caller can grep -Fxq for an exact
    # "use <profile> --force" call — or confirm it was never made. `list`
    # exits 42 (knowledge-ctx's own NO_PROJECT_MAPPING code) unconditionally,
    # which is what sends the brand-new-project fixture down bin/co's generic
    # tail rather than its loud "not individuated" error — no other call site
    # in this file ever invokes `list`, so this doesn't change any existing
    # assertion.
    # KX_REFUSE, when set in the caller's environment (see run_co below),
    # makes this stub exit 2 after logging its call — the exact code
    # knowledge-ctx's own live-process pre-flight uses for "a genuinely
    # different live claude is in cwd" (bin/knowledge-ctx's `abort(..., 2)`).
    cat > "$home/stubbin/knowledge-ctx" <<'EOF'
#!/bin/bash
if [[ ${1:-} == list ]]; then
    exit 42
fi
echo "$@" >> "$KX_CALL_LOG"
if [[ -n ${KX_REFUSE:-} ]]; then
    exit 2
fi
exit 0
EOF
    chmod +x "$home/stubbin/knowledge-ctx"

    echo "$home"
}

# run_co <home> <dir-under-home> [co args...] -> sets RC, CLAUDE_ARGS, CLAUDE_ENV, KX_CALLS, ERR
# Honours CO_ROLLOVER_SUCCESSOR, CO_ROLLOVER_EXEMPT_PID, and KX_REFUSE from
# the caller's environment if set.
run_co() {
    local home=$1 rel=$2; shift 2
    local call_log kx_log env_log err_log
    call_log=$(mktemp); kx_log=$(mktemp); env_log=$(mktemp); err_log=$(mktemp)
    # A literal `VAR=value` token before the command is what bash recognizes as
    # a prefix assignment — one produced by expanding another parameter (the
    # `${X:+VAR=val}` shape) is parsed as a plain word instead and bash tries
    # to execute it as a command. So this is exported unconditionally, empty
    # when the caller didn't set it (bin/co's `-n` check treats empty the same
    # as unset).
    ( cd "$home/$rel" && HOME="$home" PATH="$home/stubbin:$PATH" \
      CLAUDE_CALL_LOG="$call_log" KX_CALL_LOG="$kx_log" CLAUDE_ENV_LOG="$env_log" \
      CO_ROLLOVER_SUCCESSOR="${CO_ROLLOVER_SUCCESSOR:-}" \
      CO_ROLLOVER_EXEMPT_PID="${CO_ROLLOVER_EXEMPT_PID:-}" \
      KX_REFUSE="${KX_REFUSE:-}" \
      /bin/zsh "$CO" --root-config "$(co_root_config "$home")" "$@" >/dev/null 2>"$err_log" )
    RC=$?
    CLAUDE_ARGS=$(cat "$call_log")
    CLAUDE_ENV=$(cat "$env_log")
    KX_CALLS=$(cat "$kx_log")
    ERR=$(cat "$err_log")
    rm -f "$call_log" "$kx_log" "$env_log" "$err_log"
}

# The stub records one argv element per line, so this pulls the value
# adjacent to --agent rather than substring-matching (which would also pass
# on an agent name that happens to appear inside the prompt argument).
_agent_value() { # <argv-log-text> -> echoes the value following --agent, or ""
    printf '%s\n' "$1" | awk '$0=="--agent"{getline; print; exit}'
}

# True iff the prompt reached claude as ONE argv element, not split apart.
_has_prompt_line() { # <argv-log-text>
    printf '%s\n' "$1" | grep -Fxq "$PROMPT"
}

H=$(setup)

echo "== rollover's /handoff-pickup prompt is no longer read as an agent name =="

run_co "$H" Convivy/constance-commercial -r "$PROMPT"
assert_rc 0 "$RC" "*-commercial: co -r with the rollover prompt exits 0"
assert_eq "product-manager" "$(_agent_value "$CLAUDE_ARGS")" \
    "*-commercial: rollover prompt takes the branch's default agent (product-manager)"
assert_eq "1" "$(_has_prompt_line "$CLAUDE_ARGS" && echo 1 || echo 0)" \
    "*-commercial: the prompt reaches claude as its own argv element"

run_co "$H" Convivy/convivy-commercial -r "$PROMPT"
assert_rc 0 "$RC" "convivy-commercial: co -r with the rollover prompt exits 0"
assert_eq "positioning-strategist" "$(_agent_value "$CLAUDE_ARGS")" \
    "convivy-commercial: rollover prompt takes the branch's default agent (positioning-strategist)"
assert_eq "1" "$(_has_prompt_line "$CLAUDE_ARGS" && echo 1 || echo 0)" \
    "convivy-commercial: the prompt reaches claude as its own argv element"

run_co "$H" Convivy/vivy -r "$PROMPT"
assert_rc 0 "$RC" "product branch: co -r with the rollover prompt exits 0"
assert_eq "planning-orchestrator" "$(_agent_value "$CLAUDE_ARGS")" \
    "product branch: rollover prompt takes the branch's default agent (planning-orchestrator)"
assert_eq "1" "$(_has_prompt_line "$CLAUDE_ARGS" && echo 1 || echo 0)" \
    "product branch: the prompt reaches claude as its own argv element"

echo "== regression pins: ordinary agent selection in the commercial fixture is unaffected =="

run_co "$H" Convivy/constance-commercial pa
assert_rc 0 "$RC" "co pa: exits 0"
assert_eq "product-analyst" "$(_agent_value "$CLAUDE_ARGS")" "co pa: still selects product-analyst"

run_co "$H" Convivy/constance-commercial some-other-agent
assert_rc 0 "$RC" "co <explicit-agent>: exits 0"
assert_eq "some-other-agent" "$(_agent_value "$CLAUDE_ARGS")" \
    "co <explicit-agent>: the passthrough arm still selects an unlisted agent name"

echo "== CO_ROLLOVER_SUCCESSOR skips the forced re-individuate, the pull still runs =="

CO_ROLLOVER_SUCCESSOR=1 run_co "$H" Convivy/vivy -r "$PROMPT"
assert_rc 0 "$RC" "marked rollover successor: co -r still exits 0"
assert_eq "planning-orchestrator" "$(_agent_value "$CLAUDE_ARGS")" \
    "marked rollover successor: still launches the branch's default agent"
assert_eq "1" "$(_has_prompt_line "$CLAUDE_ARGS" && echo 1 || echo 0)" \
    "marked rollover successor: the prompt still reaches claude"
assert_eq "" "$(echo "$KX_CALLS" | grep -x 'use daytime --force')" \
    "marked rollover successor: knowledge-ctx use --force is never called"
assert_contains "$ERR" "rollover successor: skipping knowledge-ctx use --force" \
    "marked rollover successor: the skip is announced"
assert_contains "$ERR" "pulling $H/Convivy/knowledge" \
    "marked rollover successor: the knowledge pull still runs"
# The marker must be consumed inside _co_reindividuate, not just read — it is
# an ordinary exported var on the successor window's initial process, and
# `exec claude` preserves environment, so anything short of an `unset` before
# that exec rides straight into claude's process (and everything it forks).
assert_eq "" "$CLAUDE_ENV" \
    "marked rollover successor: CO_ROLLOVER_SUCCESSOR does not reach claude's environment"

echo "== CO_ROLLOVER_EXEMPT_PID: a marked successor with a valid pid re-individuates, exempted =="

# issue_01M2FDBJS8GMA16DX4XNBVH2K6: with a predecessor pid to exempt, the
# successor no longer skips outright — it re-individuates with
# --exempt-pid so a real knowledge-repo change since the predecessor's own
# last activation still lands.
CO_ROLLOVER_SUCCESSOR=1 CO_ROLLOVER_EXEMPT_PID=12345 run_co "$H" Convivy/vivy -r "$PROMPT"
assert_rc 0 "$RC" "marked successor + valid exempt pid: co -r still exits 0"
assert_contains "$KX_CALLS" "use daytime --force --exempt-pid 12345" \
    "marked successor + valid exempt pid: knowledge-ctx use --force --exempt-pid 12345 is called"
assert_eq "" "$(echo "$ERR" | grep -F 'skipping knowledge-ctx use --force')" \
    "marked successor + valid exempt pid: the skip is NOT announced"
assert_eq "" "$CLAUDE_ENV" \
    "marked successor + valid exempt pid: neither marker reaches claude's environment"

echo "== CO_ROLLOVER_EXEMPT_PID: a non-digit or empty value keeps today's skip =="

CO_ROLLOVER_SUCCESSOR=1 CO_ROLLOVER_EXEMPT_PID="not-a-pid" run_co "$H" Convivy/vivy -r "$PROMPT"
assert_rc 0 "$RC" "marked successor + non-digit exempt pid: co -r still exits 0"
assert_eq "" "$(echo "$KX_CALLS" | grep -x 'use daytime --force')" \
    "marked successor + non-digit exempt pid: knowledge-ctx use --force is never called"
assert_eq "" "$(echo "$KX_CALLS" | grep -F -- '--exempt-pid')" \
    "marked successor + non-digit exempt pid: --exempt-pid is never passed"
assert_contains "$ERR" "rollover successor: skipping knowledge-ctx use --force" \
    "marked successor + non-digit exempt pid: the skip is announced"
assert_eq "" "$CLAUDE_ENV" \
    "marked successor + non-digit exempt pid: neither marker reaches claude's environment"

CO_ROLLOVER_SUCCESSOR=1 CO_ROLLOVER_EXEMPT_PID="" run_co "$H" Convivy/vivy -r "$PROMPT"
assert_rc 0 "$RC" "marked successor + empty exempt pid: co -r still exits 0"
assert_contains "$ERR" "rollover successor: skipping knowledge-ctx use --force" \
    "marked successor + empty exempt pid: the skip is announced"
assert_eq "" "$CLAUDE_ENV" \
    "marked successor + empty exempt pid: neither marker reaches claude's environment"

# A malformed list — one bad element anywhere in it — is treated as fully
# absent, not partially applied: bin/co validates the WHOLE comma list before
# accepting any of it.
CO_ROLLOVER_SUCCESSOR=1 CO_ROLLOVER_EXEMPT_PID="111,abc" run_co "$H" Convivy/vivy -r "$PROMPT"
assert_rc 0 "$RC" "marked successor + malformed pid list: co -r still exits 0"
assert_eq "" "$(echo "$KX_CALLS" | grep -F -- '--exempt-pid')" \
    "marked successor + malformed pid list: --exempt-pid is never passed"
assert_contains "$ERR" "rollover successor: skipping knowledge-ctx use --force" \
    "marked successor + malformed pid list: the skip is announced"
assert_eq "" "$CLAUDE_ENV" \
    "marked successor + malformed pid list: neither marker reaches claude's environment"

echo "== CO_ROLLOVER_EXEMPT_PID: a comma-separated list adds one --exempt-pid per pid =="

# issue_01M2FDBJS8GMA16DX4XNBVH2K6 round 2: several claude pids can share one
# pane (tmux_transcript.claude_pids_under's documented computer-use-fork
# shape), so co_rollover.spawn_successor passes every one of them through as
# a comma-separated list, and bin/co must plumb one --exempt-pid per pid.
CO_ROLLOVER_SUCCESSOR=1 CO_ROLLOVER_EXEMPT_PID=111,222 run_co "$H" Convivy/vivy -r "$PROMPT"
assert_rc 0 "$RC" "marked successor + multiple exempt pids: co -r still exits 0"
assert_contains "$KX_CALLS" "use daytime --force --exempt-pid 111 --exempt-pid 222" \
    "marked successor + multiple exempt pids: one --exempt-pid per pid is passed"
assert_eq "" "$CLAUDE_ENV" \
    "marked successor + multiple exempt pids: neither marker reaches claude's environment"

echo "== CO_ROLLOVER_EXEMPT_PID without CO_ROLLOVER_SUCCESSOR adds no --exempt-pid =="

# The exemption only ever applies to a marked rollover successor's forced
# re-individuate — an exempt-pid list with no CO_ROLLOVER_SUCCESSOR must not
# reach knowledge-ctx at all, and the ordinary (non-rollover) --force call
# still runs.
CO_ROLLOVER_EXEMPT_PID=12345 run_co "$H" Convivy/vivy -r "$PROMPT"
assert_rc 0 "$RC" "unmarked launch + exempt pid: co -r still exits 0"
assert_contains "$KX_CALLS" "use daytime --force" \
    "unmarked launch + exempt pid: the ordinary --force call still runs"
assert_eq "" "$(echo "$KX_CALLS" | grep -F -- '--exempt-pid')" \
    "unmarked launch + exempt pid: --exempt-pid is never passed without the successor marker"
assert_eq "" "$CLAUDE_ENV" \
    "unmarked launch + exempt pid: neither marker reaches claude's environment"

echo "== a refused re-individuate falls back to the skip instead of exit 1 =="

# A marked successor's --force call can still be refused (knowledge-ctx's own
# live-process guard, exit code 2) when a genuinely different live claude —
# not one of the exempted pids — is in cwd. That must not strand the
# operator in the `|| exec zsh` fallback: co falls back to the same skip an
# unresolved pid takes, and claude still launches on the predecessor's
# already-individuated profile.
KX_REFUSE=1 CO_ROLLOVER_SUCCESSOR=1 CO_ROLLOVER_EXEMPT_PID=12345 run_co "$H" Convivy/vivy -r "$PROMPT"
assert_rc 0 "$RC" "marked successor + refused re-individuate: co -r still exits 0"
assert_eq "1" "$(_has_prompt_line "$CLAUDE_ARGS" && echo 1 || echo 0)" \
    "marked successor + refused re-individuate: claude still launches"
assert_contains "$KX_CALLS" "use daytime --force --exempt-pid 12345" \
    "marked successor + refused re-individuate: knowledge-ctx use --force --exempt-pid was attempted"
assert_contains "$ERR" "knowledge-ctx refused" \
    "marked successor + refused re-individuate: the fallback is announced"
assert_eq "" "$CLAUDE_ENV" \
    "marked successor + refused re-individuate: neither marker reaches claude's environment"

echo "== the marker doesn't leak on a branch that never calls _co_reindividuate =="

# The generic tail (bin/co's Deck New Project path) execs claude directly —
# no knowledge-ctx profile, no _co_reindividuate call anywhere on this
# branch. Before round 3 the marker was only ever unset inside that helper,
# so this branch rode CO_ROLLOVER_SUCCESSOR straight into claude's
# environment; the fix moved the unset to the top of bin/co, before any
# branch dispatches, so it must be gone here too. CO_ROLLOVER_EXEMPT_PID is
# consumed at that same top-of-file point, so a valid pid must not leak here
# either, even though this branch never calls knowledge-ctx at all.
CO_ROLLOVER_SUCCESSOR=1 CO_ROLLOVER_EXEMPT_PID=12345 run_co "$H" Convivy/brand-new-project -r "$PROMPT"
assert_rc 0 "$RC" "brand-new-project (marked): co -r still exits 0"
assert_eq "1" "$(_has_prompt_line "$CLAUDE_ARGS" && echo 1 || echo 0)" \
    "brand-new-project (marked): the prompt still reaches claude"
assert_eq "" "$CLAUDE_ENV" \
    "brand-new-project (marked): neither marker reaches claude's environment"

echo "== an ordinary co -r (no marker) still individuates =="

run_co "$H" Convivy/vivy -r
assert_rc 0 "$RC" "ordinary co -r: exits 0"
assert_contains "$KX_CALLS" "use daytime --force" \
    "ordinary co -r: knowledge-ctx use --force is still called"

echo "== the skips require the predecessor's .active-profile to name the launched profile =="

# Deck's /rollover produced a successor with no skills and no deck-* agents
# because .claude held no .active-profile: both skips assumed an individuation
# that was never there. Site 1 is the no-pid skip; site 2 is the refusal
# fallback. Each now runs / fails unless the marker is present and matches.
MARKER="$H/Convivy/vivy/.claude/.active-profile"

echo "vivy/daytime" > "$MARKER"
CO_ROLLOVER_SUCCESSOR=1 run_co "$H" Convivy/vivy -r "$PROMPT"
assert_rc 0 "$RC" "site 1, matching .active-profile: exits 0"
assert_eq "" "$(echo "$KX_CALLS" | grep -x 'use daytime --force')" \
    "site 1, matching .active-profile: knowledge-ctx use --force is not called"
assert_contains "$ERR" "verified .claude/.active-profile names daytime" \
    "site 1, matching .active-profile: the skip says it verified the marker"

rm -f "$MARKER"
CO_ROLLOVER_SUCCESSOR=1 run_co "$H" Convivy/vivy -r "$PROMPT"
assert_rc 0 "$RC" "site 1, no .active-profile: exits 0"
assert_contains "$KX_CALLS" "use daytime --force" \
    "site 1, no .active-profile: knowledge-ctx use --force runs"
assert_eq "" "$(echo "$KX_CALLS" | grep -F -- '--exempt-pid')" \
    "site 1, no .active-profile: no --exempt-pid is passed"
assert_eq "" "$(echo "$ERR" | grep -F 'skipping knowledge-ctx use --force')" \
    "site 1, no .active-profile: the skip is not announced"

echo "vivy/commercial" > "$MARKER"
CO_ROLLOVER_SUCCESSOR=1 run_co "$H" Convivy/vivy -r "$PROMPT"
assert_rc 0 "$RC" "site 1, .active-profile names a different profile: exits 0"
assert_contains "$KX_CALLS" "use daytime --force" \
    "site 1, .active-profile names a different profile: knowledge-ctx use --force runs"
assert_eq "" "$(echo "$ERR" | grep -F 'skipping knowledge-ctx use --force')" \
    "site 1, .active-profile names a different profile: the skip is not announced"

echo "vivy/daytime" > "$MARKER"
KX_REFUSE=1 CO_ROLLOVER_SUCCESSOR=1 CO_ROLLOVER_EXEMPT_PID=12345 run_co "$H" Convivy/vivy -r "$PROMPT"
assert_rc 0 "$RC" "site 2, matching .active-profile: falls back, exits 0"
assert_contains "$ERR" "names daytime)" \
    "site 2, matching .active-profile: the fallback says it verified the marker"
assert_eq "1" "$(_has_prompt_line "$CLAUDE_ARGS" && echo 1 || echo 0)" \
    "site 2, matching .active-profile: claude still launches"

rm -f "$MARKER"
KX_REFUSE=1 CO_ROLLOVER_SUCCESSOR=1 CO_ROLLOVER_EXEMPT_PID=12345 run_co "$H" Convivy/vivy -r "$PROMPT"
assert_rc 1 "$RC" "site 2, no .active-profile: the launch fails with exit 1"
assert_contains "$ERR" "daytime was never individuated in this" \
    "site 2, no .active-profile: the message says it was never individuated"
assert_contains "$ERR" "co -r" \
    "site 2, no .active-profile: the message names the recovery"
assert_eq "" "$CLAUDE_ARGS" \
    "site 2, no .active-profile: claude is never launched"

rm -rf "$H"
finish
