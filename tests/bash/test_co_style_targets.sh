#!/bin/bash
# Tests for bin/co's `_apply_co_style` naming the tmux target on every call.
#
# THE BUG. The six tmux calls in `_apply_co_style` passed no `-t`. From a pane
# created detached (`tmux new-window -d`, `tmux new-session -d`) tmux resolves an
# untargeted command to the CURRENT session and its ACTIVE window rather than to
# the pane running it, so a co launched that way restyled a session it was not
# in and renamed a window it was not in. On 2026-09-06 a spawned Back chat
# renamed the operator's main Ask window, which Deck drew as that session being lost
# (issue_01M1VTRHKRERY7MJEYCF4WR2G2).
#
# THE FIX pins every call to `$TMUX_PANE`: tmux resolves a session option set
# against a pane to that pane's session and a rename to that pane's window
# (confirmed on 3.6a). A tmux spy on PATH records argv, so these assert what co
# actually asks tmux to do, without a tmux server and without the run restyling
# whatever window the test suite happens to be in — which is exactly what the
# untargeted calls did to a developer's own window on every local run.
#
# `co` is zsh; bash driving it here is how a login shell will.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

CO="$REPO_ROOT/bin/co"

setup() { # -> echoes fake HOME
    local home
    home=$(mktemp -d)
    mkdir -p "$home/stubbin" "$home/Convivy/agent-platform/.claude/agents"
    : > "$home/Convivy/agent-platform/.claude/agents/scout.md"
    ( cd "$home/Convivy/agent-platform" && git init -q . )
    cat > "$home/stubbin/claude" <<'STUB'
#!/bin/bash
exit 0
STUB
    cat > "$home/stubbin/knowledge-ctx" <<'STUB'
#!/bin/bash
exit 0
STUB
    cat > "$home/stubbin/tmux" <<'STUB'
#!/bin/bash
printf '%s\n' "$*" >> "$TMUX_SPY_LOG"
exit 0
STUB
    chmod +x "$home/stubbin/claude" "$home/stubbin/knowledge-ctx" "$home/stubbin/tmux"
    echo "$home"
}

# run_co <home> [env assignments...] -> sets TMUX_CALLS (one tmux argv per line)
run_co() {
    local home=$1; shift
    local log; log=$(mktemp)
    ( cd "$home/Convivy/agent-platform" && HOME="$home" PATH="$home/stubbin:$PATH" \
      TMUX_SPY_LOG="$log" env "$@" /bin/zsh "$CO" --root-config "$(co_root_config "$home")" >/dev/null 2>&1 )
    TMUX_CALLS=$(cat "$log")
    rm -f "$log"
}

H=$(setup)

# --- 1. inside tmux with a pane id, every call names that pane -------------------
run_co "$H" TMUX=/tmp/tmux-spy/default,1,0 TMUX_PANE=%7
n_calls=$(printf '%s\n' "$TMUX_CALLS" | grep -c . || true)
assert_eq "6" "$n_calls" "co issues its six tmux calls under a tmux pane"
n_pinned=$(printf '%s\n' "$TMUX_CALLS" | grep -c -- '-t %7 ' || true)
assert_eq "$n_calls" "$n_pinned" "every tmux call carries -t <this pane>"
assert_contains "$TMUX_CALLS" "rename-window -t %7 Bosun" "the rename targets this pane's window"
assert_contains "$TMUX_CALLS" "set-option -t %7 allow-passthrough on" "allow-passthrough targets this pane"
assert_contains "$TMUX_CALLS" "set-option -t %7 status-left " "status-left targets this pane's session"

# --- 1b. a black box's colours reach tmux: EA is gold with brown text ------------
# The operator named the pair on 2026-09-06; the hex values are the code's. The foreground is
# explicit because the default white is unreadable on a light accent, the same
# reason Orient and Library pass one. The status line is where the pair is visible.
mkdir -p "$H/Convivy/EA"
log=$(mktemp)
( cd "$H/Convivy/EA" && HOME="$H" PATH="$H/stubbin:$PATH" TMUX_SPY_LOG="$log" \
  env TMUX=/tmp/tmux-spy/default,1,0 TMUX_PANE=%7 /bin/zsh "$CO" --root-config "$(co_root_config "$H")" >/dev/null 2>&1 )
ea_calls=$(cat "$log"); rm -f "$log"
assert_contains "$ea_calls" "status-style bg=#c8a24a,fg=#5a3e1b" "EA's status line is gold with brown text"
assert_contains "$ea_calls" "rename-window -t %7 EA" "EA's window carries its display name"

# --- 2. inside tmux with no pane id, NOTHING is called --------------------------
# THIS TEST PREVIOUSLY ASSERTED THE DEFECT AS CORRECT. It required all six calls
# to go out untargeted, on the reasoning that a co with no `$TMUX_PANE` has
# nothing better to name. There is something better and it is doing nothing:
# untargeted, tmux resolves to the current session's ACTIVE window, so the calls
# restyle and rename a window this co does not own.
#
# That is not hypothetical and it is not cosmetic. On 2026-09-16 the `bosun`
# session's window was renamed `operator.com` while the Bosun session inside it
# was healthy, and earlier that evening `bosun:1` wore the label `Shipwright`
# (issue_01M2PXARYZ2HA08G9MJ4QPQYW9). It was first read as a failed rollover.
# The same mechanism had already renamed the operator's main Ask window on 2026-09-06
# (issue_01M1VTRHKRERY7MJEYCF4WR2G2) — `$TMUX_PANE` was the fix then, and it
# closed only the case where the variable is present.
#
# So the old assertions are inverted deliberately. A test pinning the buggy
# behaviour as correct is why this recurred: the previous fix shipped green with
# its own defect asserted.
run_co "$H" TMUX=/tmp/tmux-spy/default,1,0 TMUX_PANE=
assert_eq "" "$TMUX_CALLS" "with no pane id co calls tmux not at all"

# --- 3. outside tmux, tmux is never called -------------------------------------
run_co "$H" TMUX= TMUX_PANE=
assert_eq "" "$TMUX_CALLS" "outside tmux co does not call tmux"

rm -rf "$H"
finish
