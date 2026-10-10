#!/bin/bash
# Tests for bin/co's convivy-deck branch re-individuating on `-r`
# (issue_01M35K490CAR4Q69B9TAR7Z39X, closing a duplicate of
# issue_01M2PCH82X17T26SB42MNJN53Z).
#
# THE BUG. PR #446 took convivy-deck out of LOCAL_AGENT_REPOS, so `agentctl
# spawn` has resolved deck's agents through the shared KB index (deck/daytime)
# since 2026-08-26 — a DISPATCHED deck agent has always gotten the profile.
# The convivy-deck branch of bin/co never called `_co_pull_knowledge` or
# `_co_reindividuate`, though: it only set /handoff-pickup and exec'd claude,
# so the interactive session's .claude/ carried only whatever was last
# hand-placed or checked into git. On 2026-09-22 the operator ran `co -r` across every
# crew and only Deck's interactive session failed to pick up the renamed
# platform skills, because it had never individuated anything.
#
# THE FIX mirrors the selector and substrate branches (see
# test_co_selector_individuate.sh): pull ~/Convivy/knowledge with the same
# failure handling every other `_co_reindividuate` caller uses, then call
# `_co_reindividuate daytime` before the exec on `-r`, gated on the FULL
# resolved path rather than the basename the branch itself matches on —
# `--force` clears .claude/agents and .claude/skills, so a same-basename
# near-miss directory must pull and launch but never individuate.
#
# `co` is zsh (excluded from the ARCH-082 bash-3.2 contract by lint.sh's
# shebang filter); bash driving it here is exactly how a login shell will.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

CO="$REPO_ROOT/bin/co"
REAL_GIT=$(command -v git)

_git_ident() { # <repo>
    git -C "$1" config user.email t@t
    git -C "$1" config user.name t
    git -C "$1" config commit.gpgsign false
}

# A fake HOME with a real ~/Convivy/knowledge tracking a real bare origin (the
# `-r` path pulls it before individuating, so a fake would abort first and
# every assertion below would pass for the wrong reason), the real
# ~/Convivy/convivy-deck with a crew fixture matching what convivy-deck still
# tracks in git today (a stand-in for the pre-fix, still-tracked state, so a
# regression back to "never reindividuate" would still leave the files it
# expects there and pass for the wrong reason), and a same-basename near-miss
# outside it.
setup() { # -> echoes fake HOME
    local home origin kb seed d
    home=$(mktemp -d)
    origin="$home/origin.git"
    kb="$home/Convivy/knowledge"

    git init -q --bare "$origin"
    git -C "$origin" symbolic-ref HEAD refs/heads/main
    seed=$(mktemp -d)
    git init -q "$seed/w"
    git -C "$seed/w" symbolic-ref HEAD refs/heads/main
    _git_ident "$seed/w"
    echo v1 > "$seed/w/seed.md"
    git -C "$seed/w" add -A
    git -C "$seed/w" commit -qm seed
    git -C "$seed/w" remote add origin "$origin"
    git -C "$seed/w" push -q -u origin main || {
        echo "  FIXTURE BROKEN: seed push failed; every assertion below is meaningless" >&2
        exit 1
    }
    rm -rf "$seed"

    mkdir -p "$(dirname "$kb")"
    git clone -q "$origin" "$kb"
    _git_ident "$kb"

    for d in Convivy/convivy-deck Convivy/worktrees/convivy-deck elsewhere/convivy-deck; do
        mkdir -p "$home/$d/.claude/agents"
        : > "$home/$d/.claude/agents/deck-coder.md"
        ( cd "$home/$d" && git init -q . )
    done

    mkdir -p "$home/stubbin"
    write_kb_sync_stub "$home/stubbin"
    cat > "$home/stubbin/claude" <<'EOF'
#!/bin/bash
printf '%s\n' "$@" > "$CLAUDE_CALL_LOG"
exit 0
EOF
    chmod +x "$home/stubbin/claude"
    # Logs its argv, then honours KCTX_RC so a failure can be simulated without
    # a second stub. Writes to the SAME spy log the git stub below writes to,
    # so the order of a pull's git calls relative to this one is observable.
    cat > "$home/stubbin/knowledge-ctx" <<'EOF'
#!/bin/bash
echo "$@" >> "$SPY_LOG"
exit "${KCTX_RC:-0}"
EOF
    chmod +x "$home/stubbin/knowledge-ctx"
    # A spy that logs every git invocation verbatim, then delegates to the real
    # git — same pattern test_co_knowledge_pull.sh uses. Sharing SPY_LOG with
    # the knowledge-ctx stub above is what makes the pull-before-render order
    # assertable rather than just "both happened".
    cat > "$home/stubbin/git" <<EOF
#!/bin/bash
echo "\$@" >> "\$SPY_LOG"
exec "$REAL_GIT" "\$@"
EOF
    chmod +x "$home/stubbin/git"

    echo "$home"
}

# Add a commit to origin so the knowledge checkout falls behind. Copied from
# test_co_knowledge_pull.sh's helper of the same name.
advance_origin() { # <home>
    local home=$1 push
    push=$(mktemp -d)
    git clone -q "$home/origin.git" "$push/w" || {
        echo "  FIXTURE BROKEN: cannot clone origin" >&2; exit 1; }
    _git_ident "$push/w"
    echo more >> "$push/w/seed.md"
    git -C "$push/w" add -A
    git -C "$push/w" commit -qm advance
    git -C "$push/w" push -q origin main || {
        echo "  FIXTURE BROKEN: cannot push to origin" >&2; exit 1; }
    rm -rf "$push"
}

# run_co <home> <dir-under-home> [-r] -> sets RC, STDERR, SPY, CLAUDE_ARGS
run_co() {
    local home=$1 dir=$2; shift 2
    local stderr_log spy_log claude_log
    stderr_log=$(mktemp); spy_log=$(mktemp); claude_log=$(mktemp)
    ( cd "$home/$dir" && \
      HOME="$home" PATH="$home/stubbin:$PATH" \
      CLAUDE_CALL_LOG="$claude_log" SPY_LOG="$spy_log" AGENTCTL_LOG="$spy_log" KCTX_RC="${KCTX_RC:-0}" \
      /bin/zsh "$CO" --root-config "$(co_root_config "$home")" "$@" >/dev/null 2>"$stderr_log" )
    RC=$?
    STDERR=$(cat "$stderr_log")
    SPY=$(cat "$spy_log")
    CLAUDE_ARGS=$(cat "$claude_log")
    rm -f "$stderr_log" "$spy_log" "$claude_log"
}

echo "== co -r in ~/Convivy/convivy-deck re-individuates from the daytime profile, after pulling =="
H=$(setup)
run_co "$H" Convivy/convivy-deck -r
assert_rc 0 "$RC" "deck -r succeeds"
assert_eq "1" "$(echo "$SPY" | grep -qx 'use daytime --force' && echo 1 || echo 0)" \
    "knowledge-ctx is called as 'use daytime --force'"
assert_eq "1" "$([ -n "$CLAUDE_ARGS" ] && echo 1 || echo 0)" \
    "claude still launches after individuating"
# THE ORDER, NOT JUST THAT BOTH HAPPENED: a regression that individuates
# BEFORE pulling would still pass every assertion above while rendering a
# profile that is up to 10 minutes stale, the same class of bug this PR fixes.
# The pull is `agentctl kb sync` now (the stub logs it into the same SPY_LOG), not
# a `git merge` from co itself.
pull_line=$(echo "$SPY" | grep -n -x 'agentctl kb sync' | head -1 | cut -d: -f1)
render_line=$(echo "$SPY" | grep -n -x 'use daytime --force' | head -1 | cut -d: -f1)
assert_eq "1" "$([ -n "$pull_line" ] && [ -n "$render_line" ] && [ "$pull_line" -lt "$render_line" ] && echo 1 || echo 0)" \
    "the knowledge pull (agentctl kb sync) runs before knowledge-ctx use daytime --force"
rm -rf "$H"

echo "== plain co does NOT individuate =="
# Individuating clears two trees, so it belongs to the explicit refresh a
# person asked for, not to every launch.
H=$(setup)
run_co "$H" Convivy/convivy-deck
assert_rc 0 "$RC" "deck without -r succeeds"
assert_eq "0" "$(echo "$SPY" | grep -qx 'use daytime --force' && echo 1 || echo 0)" \
    "knowledge-ctx is not invoked at all"
assert_eq "1" "$([ -n "$CLAUDE_ARGS" ] && echo 1 || echo 0)" "claude still launches"
rm -rf "$H"

echo "== a same-basename near-miss is NOT individuated, but still pulls and launches =="
# The branch matches a basename; the individuate step matches the full path.
# Without that asymmetry, `--force` would clear .claude/agents and
# .claude/skills in any directory on disk named `convivy-deck`.
for near in Convivy/worktrees/convivy-deck elsewhere/convivy-deck; do
    H=$(setup)
    run_co "$H" "$near" -r
    assert_rc 0 "$RC" "$near: co -r still succeeds"
    assert_eq "0" "$(echo "$SPY" | grep -qx 'use daytime --force' && echo 1 || echo 0)" \
        "$near: knowledge-ctx use is not invoked"
    assert_eq "1" "$([ -f "$H/$near/.claude/agents/deck-coder.md" ] && echo 1 || echo 0)" \
        "$near: its .claude/agents survives"
    assert_eq "1" "$([ -n "$CLAUDE_ARGS" ] && echo 1 || echo 0)" \
        "$near: claude still launches"
    rm -rf "$H"
done

echo "== a failed individuate aborts before launching =="
# The same contract the selector and substrate branches keep: a stale crew is
# worse than no session, and the usual cause is a live session holding the
# directory.
H=$(setup)
KCTX_RC=1 run_co "$H" Convivy/convivy-deck -r
assert_rc 1 "$RC" "deck -r aborts when knowledge-ctx fails"
assert_eq "" "$CLAUDE_ARGS" "claude is never launched"
assert_eq "1" "$(echo "$STDERR" | grep -q 'knowledge-ctx use failed' && echo 1 || echo 0)" \
    "the loud abort message fires"
rm -rf "$H"

echo "== a failed pull aborts before individuating or launching =="
# Same contract _co_pull_knowledge's other callers keep (test_co_knowledge_pull.sh's
# diverged-checkout case): a diverged ~/Convivy/knowledge can't fast-forward,
# so the pull refuses rather than forcing, and nothing downstream runs.
H=$(setup)
advance_origin "$H"
echo local > "$H/Convivy/knowledge/localonly.md"
git -C "$H/Convivy/knowledge" add -A
git -C "$H/Convivy/knowledge" commit -qm "local-only"
run_co "$H" Convivy/convivy-deck -r
assert_rc 1 "$RC" "deck -r aborts when the knowledge pull fails"
assert_eq "" "$CLAUDE_ARGS" "claude is never launched"
assert_eq "0" "$(echo "$SPY" | grep -qx 'use daytime --force' && echo 1 || echo 0)" \
    "knowledge-ctx is never reached"
assert_eq "1" "$(echo "$STDERR" | grep -q 'knowledge pull failed' && echo 1 || echo 0)" \
    "the loud pull-failure abort message fires"
rm -rf "$H"

finish
