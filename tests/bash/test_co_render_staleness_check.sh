#!/bin/bash
# A plain `co` launch (no `-r`) notices a stale render and refreshes it on
# its own (issue_01M4GJRCDNKCV4QREH68KSRS3W).
#
# THE OCCURRENCE: on 2026-10-09 vivy-commercial (and four other crews) were
# still running instruction blocks a KB change had replaced days earlier,
# because only `co -r` ever re-renders and nobody remembered to pass it.
# `_co_refresh_if_stale` runs `knowledge-ctx check` on every plain launch of
# an already-individuated workspace and, on exit 1 (behind) or 3 (cannot
# tell), takes the same refresh path `-r` takes for that workspace.
#
# `co` is zsh (excluded from the ARCH-082 bash-3.2 contract by lint.sh's
# shebang filter); bash invoking it here is exactly how a login shell will.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

CO="$REPO_ROOT/bin/co"

# A fake HOME with a knowledge repo that has a REAL, reachable origin (same
# shape as test_co_crew_refresh.sh), plus one crew-cockpit directory
# (shipwright) and one `*-commercial` directory (vivy-commercial — the exact
# branch the occurrence happened on) each already individuated:
# `.claude/.active-profile` present, so `_co_refresh_if_stale`'s guard does
# not short-circuit on "never rendered here".
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

    mkdir -p "$home/Convivy/shipwright/.claude/agents"
    : > "$home/Convivy/shipwright/.claude/agents/planning-orchestrator.md"
    printf 'platform/daytime\n' > "$home/Convivy/shipwright/.claude/.active-profile"
    ( cd "$home/Convivy/shipwright" && git init -q . )

    mkdir -p "$home/Convivy/vivy-commercial/.claude/agents"
    : > "$home/Convivy/vivy-commercial/.claude/agents/product-manager.md"
    printf 'vivy/commercial\n' > "$home/Convivy/vivy-commercial/.claude/.active-profile"
    ( cd "$home/Convivy/vivy-commercial" && git init -q . )

    # A crew-cockpit directory never individuated at all — no
    # `.claude/.active-profile` — so `_co_refresh_if_stale` must behave
    # exactly as before this existed: never calls `knowledge-ctx check`.
    mkdir -p "$home/Convivy/placard/.claude/agents"
    : > "$home/Convivy/placard/.claude/agents/planning-orchestrator.md"
    ( cd "$home/Convivy/placard" && git init -q . )

    # ROUND 1 BLOCKING FINDING: substrate/selector/deck gate `-r`'s own
    # `knowledge-ctx use --force` on the FULL canonical path, not the
    # basename match that selects the branch, because `--force` rmtrees
    # `.claude/agents` and `.claude/skills` — an off-path clone sharing the
    # basename must never have those deleted. Each is already individuated
    # (so `_co_refresh_if_stale` does not short-circuit) but lives under
    # `elsewhere/`, never at `$_co_crews_root/<name>`, so the automatic
    # staleness path must apply the identical gate `-r` does and never call
    # `knowledge-ctx use` there.
    mkdir -p "$home/Convivy/elsewhere/substrate/.claude/agents"
    : > "$home/Convivy/elsewhere/substrate/.claude/agents/planning-orchestrator.md"
    printf 'platform/daytime\n' > "$home/Convivy/elsewhere/substrate/.claude/.active-profile"
    ( cd "$home/Convivy/elsewhere/substrate" && git init -q . )

    mkdir -p "$home/Convivy/elsewhere/selector/.claude/agents"
    : > "$home/Convivy/elsewhere/selector/.claude/agents/planning-orchestrator.md"
    printf 'selector/daytime\n' > "$home/Convivy/elsewhere/selector/.claude/.active-profile"
    ( cd "$home/Convivy/elsewhere/selector" && git init -q . )

    mkdir -p "$home/Convivy/elsewhere/convivy-deck/.claude/agents"
    : > "$home/Convivy/elsewhere/convivy-deck/.claude/agents/planning-orchestrator.md"
    printf 'deck/daytime\n' > "$home/Convivy/elsewhere/convivy-deck/.claude/.active-profile"
    ( cd "$home/Convivy/elsewhere/convivy-deck" && git init -q . )

    # The profiled-dirs branch's own extra gate is different: the FULL path
    # matches (this IS the canonical `$_co_crews_root/www`), but git does
    # not ignore `.claude/` there, so `-r` itself skips the render. Already
    # individuated and stale; the automatic path must apply that same
    # git-ignore gate rather than calling `knowledge-ctx use` regardless.
    mkdir -p "$home/Convivy/www/.claude/agents"
    : > "$home/Convivy/www/.claude/agents/planning-orchestrator.md"
    printf 'platform/daytime\n' > "$home/Convivy/www/.claude/.active-profile"
    ( cd "$home/Convivy/www" && git init -q . )

    cat > "$home/stubbin/claude" <<'EOF'
#!/bin/bash
printf '%s\n' "$@" > "$CLAUDE_CALL_LOG"
exit 0
EOF
    chmod +x "$home/stubbin/claude"

    # Records every call (one line per call, argv joined by spaces).
    # `check`'s exit code is KX_CHECK_RC (default 0 — current). `use`'s
    # exit code is 2 (the live-process refusal) when KX_REFUSE_USE=1, so a
    # refused refresh can be exercised without a real live claude process.
    cat > "$home/stubbin/knowledge-ctx" <<'EOF'
#!/bin/bash
printf '%s\n' "$*" >> "$KX_CALL_LOG"
if [ "$1" = check ]; then
    exit "${KX_CHECK_RC:-0}"
fi
if [ "$1" = use ] && [ "${KX_REFUSE_USE:-0}" = 1 ]; then
    echo "knowledge-ctx: a live claude session is running in this directory" >&2
    exit 2
fi
exit 0
EOF
    chmod +x "$home/stubbin/knowledge-ctx"

    cat > "$home/stubbin/tmux" <<'EOF'
#!/bin/bash
case "$*" in
    *'#{session_name}'*) printf '%s\n' "${TMUX_STUB_SESSION:-}" ;;
esac
exit 0
EOF
    chmod +x "$home/stubbin/tmux"

    echo "$home"
}

# run_co_in <home> <dir-relative-to-home> [co-args...] [-- env assignments...]
#   -> sets RC, CLAUDE_ARGS, KX_ARGS, ERR
run_co_in() { # <home> <rel> [extra env assignments...] -- <co args...>
    local home=$1 rel=$2; shift 2
    local -a envs=() coargs=()
    local seen_sep=0
    for a in "$@"; do
        if [ "$seen_sep" = 0 ] && [ "$a" = "--" ]; then
            seen_sep=1
            continue
        fi
        if [ "$seen_sep" = 0 ]; then envs+=("$a"); else coargs+=("$a"); fi
    done
    local call_log kx_log err_log
    call_log=$(mktemp); kx_log=$(mktemp); err_log=$(mktemp)
    ( cd "$home/$rel" && HOME="$home" PATH="$home/stubbin:$PATH" \
      CLAUDE_CALL_LOG="$call_log" KX_CALL_LOG="$kx_log" \
      env -u TMUX -u TMUX_PANE "${envs[@]}" \
      /bin/zsh "$CO" --root-config "$(co_root_config "$home")" "${coargs[@]}" \
      >/dev/null 2>"$err_log" )
    RC=$?
    CLAUDE_ARGS=$(cat "$call_log")
    KX_ARGS=$(cat "$kx_log")
    ERR=$(cat "$err_log")
    rm -f "$call_log" "$kx_log" "$err_log"
}

kx_calls_for() { printf '%s\n' "$KX_ARGS" | grep -c "^$1"; } # <subcommand>

H=$(setup)

echo "== an individuated crew-cockpit workspace: check runs on a plain launch =="
run_co_in "$H" "Convivy/shipwright" KX_CHECK_RC=0 --
assert_rc 0 "$RC" "shipwright: plain co still exits 0"
assert_eq 1 "$(kx_calls_for check)" "shipwright: knowledge-ctx check is called once"
assert_contains "$CLAUDE_ARGS" "--model" "shipwright: launch is reached"

echo "== check reports current (exit 0): no refresh, no use call =="
run_co_in "$H" "Convivy/shipwright" KX_CHECK_RC=0 --
assert_eq 0 "$(kx_calls_for use)" "current: knowledge-ctx use is never called"
assert_contains "$CLAUDE_ARGS" "--model" "current: launch is still reached"

echo "== check reports behind (exit 1): refresh runs, then launch =="
run_co_in "$H" "Convivy/shipwright" KX_CHECK_RC=1 --
assert_rc 0 "$RC" "behind: plain co still exits 0"
assert_eq 1 "$(kx_calls_for use)" "behind: knowledge-ctx use is called once"
assert_contains "$ERR" "behind" "behind: co says the render is behind"
assert_contains "$CLAUDE_ARGS" "--model" "behind: launch is reached after the refresh"

echo "== check cannot tell (exit 3): refresh runs, then launch =="
run_co_in "$H" "Convivy/shipwright" KX_CHECK_RC=3 --
assert_rc 0 "$RC" "cannot tell: plain co still exits 0"
assert_eq 1 "$(kx_calls_for use)" "cannot tell: knowledge-ctx use is called once"
assert_contains "$ERR" "cannot tell" "cannot tell: co says it could not tell"
assert_contains "$CLAUDE_ARGS" "--model" "cannot tell: launch is reached after the refresh"

echo "== a refused refresh still launches, on the existing render =="
run_co_in "$H" "Convivy/shipwright" KX_CHECK_RC=1 KX_REFUSE_USE=1 --
assert_rc 0 "$RC" "refused refresh: plain co still exits 0 rather than aborting"
assert_contains "$ERR" "refresh failed or was refused" \
    "refused refresh: co says the refresh did not complete"
assert_contains "$CLAUDE_ARGS" "--model" \
    "refused refresh: launch is reached anyway, on the existing render"
assert_eq 0 "$(printf '%s' "$ERR" | grep -c aborting)" \
    "refused refresh: no message on this continuing path says 'aborting'"

echo "== substrate: an off-path clone sharing the basename never calls knowledge-ctx use =="
run_co_in "$H" "Convivy/elsewhere/substrate" KX_CHECK_RC=1 --
assert_rc 0 "$RC" "substrate off-path: plain co still exits 0"
assert_eq 1 "$(kx_calls_for check)" "substrate off-path: knowledge-ctx check is called once"
assert_eq 0 "$(kx_calls_for use)" "substrate off-path: knowledge-ctx use is never called"
assert_contains "$CLAUDE_ARGS" "--model" "substrate off-path: launch is reached anyway"

echo "== selector: an off-path clone sharing the basename never calls knowledge-ctx use =="
run_co_in "$H" "Convivy/elsewhere/selector" KX_CHECK_RC=1 --
assert_rc 0 "$RC" "selector off-path: plain co still exits 0"
assert_eq 1 "$(kx_calls_for check)" "selector off-path: knowledge-ctx check is called once"
assert_eq 0 "$(kx_calls_for use)" "selector off-path: knowledge-ctx use is never called"
assert_contains "$CLAUDE_ARGS" "--model" "selector off-path: launch is reached anyway"

echo "== deck: an off-path clone sharing the basename never calls knowledge-ctx use =="
run_co_in "$H" "Convivy/elsewhere/convivy-deck" KX_CHECK_RC=1 --
assert_rc 0 "$RC" "deck off-path: plain co still exits 0"
assert_eq 1 "$(kx_calls_for check)" "deck off-path: knowledge-ctx check is called once"
assert_eq 0 "$(kx_calls_for use)" "deck off-path: knowledge-ctx use is never called"
assert_contains "$CLAUDE_ARGS" "--model" "deck off-path: launch is reached anyway"

echo "== profiled-dirs: an unignored .claude/ at the canonical path never calls knowledge-ctx use =="
run_co_in "$H" "Convivy/www" KX_CHECK_RC=1 --
assert_rc 0 "$RC" "www: plain co still exits 0"
assert_eq 1 "$(kx_calls_for check)" "www: knowledge-ctx check is called once"
assert_eq 0 "$(kx_calls_for use)" "www: knowledge-ctx use is never called"
assert_contains "$ERR" "git does not ignore .claude/" "www: co explains why it skipped"
assert_contains "$CLAUDE_ARGS" "--model" "www: launch is reached anyway"

echo "== a subcrew child's plain launch, stale render: the cheap skip fires, not a real refresh =="
run_co_in "$H" "Convivy/shipwright" KX_CHECK_RC=1 \
    TMUX=test-session TMUX_PANE=%0 TMUX_STUB_SESSION=shipwright--child --
assert_rc 0 "$RC" "subcrew: plain co still exits 0"
assert_eq 1 "$(kx_calls_for check)" "subcrew: knowledge-ctx check is called once"
assert_eq 0 "$(kx_calls_for use)" "subcrew: knowledge-ctx use is never called"
assert_contains "$ERR" "subcrew session: skipping knowledge-ctx use --force" \
    "subcrew: co takes the cheap subcrew skip rather than a real refresh"
assert_eq 0 "$(printf '%s' "$ERR" | grep -c aborting)" \
    "subcrew: no message on this continuing path says 'aborting'"
assert_contains "$CLAUDE_ARGS" "--model" "subcrew: launch is reached"

echo "== a never-individuated workspace behaves exactly as before =="
run_co_in "$H" "Convivy/placard" --
assert_rc 0 "$RC" "placard: plain co still exits 0"
assert_eq 0 "$(kx_calls_for check)" "placard: knowledge-ctx check is never called"
assert_eq 0 "$(kx_calls_for use)" "placard: knowledge-ctx use is never called"
assert_contains "$CLAUDE_ARGS" "--model" "placard: launch is reached"

echo "== an explicit co -r never calls check — it already refreshed =="
run_co_in "$H" "Convivy/shipwright" KX_CHECK_RC=1 -- -r
assert_rc 0 "$RC" "shipwright -r: still exits 0"
assert_eq 0 "$(kx_calls_for check)" "shipwright -r: knowledge-ctx check is never called"
assert_eq 1 "$(kx_calls_for use)" "shipwright -r: knowledge-ctx use is called once, by -r itself"

echo "== the vivy-commercial occurrence itself: a plain launch there refreshes too =="
run_co_in "$H" "Convivy/vivy-commercial" KX_CHECK_RC=1 --
assert_rc 0 "$RC" "vivy-commercial: plain co still exits 0"
assert_eq 1 "$(kx_calls_for check)" "vivy-commercial: knowledge-ctx check is called once"
assert_eq 1 "$(kx_calls_for use)" "vivy-commercial: knowledge-ctx use is called once"
assert_contains "$CLAUDE_ARGS" "--model" "vivy-commercial: launch is reached after the refresh"

rm -rf "$H"
finish
