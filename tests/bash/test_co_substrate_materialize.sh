#!/bin/bash
# Tests for bin/co's substrate branch re-individuating on `-r`.
#
# THE BUG. The branch skipped `knowledge-ctx use` with the comment "No worktree
# to re-individuate". That was true once and stopped being true when substrate
# gained a knowledge mapping: the project table
# `projects.EXPLICIT_PROJECT_BY_NAME` carries `"substrate": "platform"`, so
# `platform/daytime` is substrate's profile and renders `platform/agents/scout`
# plus five skills. The premise expired; the skip outlived it.
#
# What that cost, measured 2026-08-26: substrate's tracked `/start-overnight`
# still pointed a co at `knowledge/projects/shared/agents/overnight-orchestrator.md`
# for the playbook it deliberately does not duplicate. That document had moved to
# `projects/agent-platform/agents/`, and the dead path was corrected in the KB
# (knowledge#289). Nothing propagated the fix, because nothing here individuated.
#
# THE PROPERTY THESE TESTS PIN IS THE GATE AS MUCH AS THE CALL. `--force`
# rmtrees `.claude/agents` and `.claude/skills` before rewriting, so the
# individuate step is gated on the FULL PATH while the branch it sits in matches
# a basename. Launching Claude in a wrong `substrate` is a risk this branch
# already accepts; DELETING two trees in one is a new one, and the asymmetry is
# what keeps it from being introduced. A near-miss must pull knowledge and
# launch, and must NOT individuate.
#
# `co` is zsh (excluded from the ARCH-082 bash-3.2 contract by lint.sh's
# shebang filter); bash driving it here is exactly how a login shell will.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

CO="$REPO_ROOT/bin/co"

_git_ident() { # <repo>
    git -C "$1" config user.email t@t
    git -C "$1" config user.name t
    git -C "$1" config commit.gpgsign false
}

# A fake HOME with a real ~/Convivy/knowledge tracking a real bare origin (the
# `-r` path pulls it before individuating, so a fake would abort first and every
# assertion below would pass for the wrong reason), the real
# ~/Convivy/substrate, and a same-basename near-miss outside it.
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

    for d in Convivy/substrate Convivy/worktrees/substrate elsewhere/substrate; do
        mkdir -p "$home/$d/.claude/agents"
        : > "$home/$d/.claude/agents/scout.md"
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
    # a second stub.
    cat > "$home/stubbin/knowledge-ctx" <<'EOF'
#!/bin/bash
echo "$@" >> "$KCTX_SPY_LOG"
exit "${KCTX_RC:-0}"
EOF
    chmod +x "$home/stubbin/knowledge-ctx"

    echo "$home"
}

# run_co <home> <dir-under-home> [-r] -> sets RC, STDERR, KCTX_CALLS, CLAUDE_ARGS
run_co() {
    local home=$1 dir=$2; shift 2
    local stderr_log spy_log claude_log
    stderr_log=$(mktemp); spy_log=$(mktemp); claude_log=$(mktemp)
    ( cd "$home/$dir" && \
      HOME="$home" PATH="$home/stubbin:$PATH" \
      CLAUDE_CALL_LOG="$claude_log" KCTX_SPY_LOG="$spy_log" KCTX_RC="${KCTX_RC:-0}" \
      /bin/zsh "$CO" --root-config "$(co_root_config "$home")" "$@" >/dev/null 2>"$stderr_log" )
    RC=$?
    STDERR=$(cat "$stderr_log")
    KCTX_CALLS=$(cat "$spy_log")
    CLAUDE_ARGS=$(cat "$claude_log")
    rm -f "$stderr_log" "$spy_log" "$claude_log"
}

echo "== co -r in ~/Convivy/substrate re-individuates =="
H=$(setup)
run_co "$H" Convivy/substrate -r
assert_rc 0 "$RC" "substrate -r succeeds"
assert_eq "1" "$(echo "$KCTX_CALLS" | grep -qx 'use daytime --force' && echo 1 || echo 0)" \
    "knowledge-ctx is called as 'use daytime --force'"
assert_eq "1" "$([ -n "$CLAUDE_ARGS" ] && echo 1 || echo 0)" \
    "claude still launches after individuating"
rm -rf "$H"

echo "== plain co does NOT individuate =="
# Individuating rmtrees two trees, so it belongs to the explicit refresh a
# person asked for, not to every launch.
H=$(setup)
run_co "$H" Convivy/substrate
assert_rc 0 "$RC" "substrate without -r succeeds"
assert_eq "" "$KCTX_CALLS" "knowledge-ctx is not invoked at all"
assert_eq "1" "$([ -n "$CLAUDE_ARGS" ] && echo 1 || echo 0)" "claude still launches"
rm -rf "$H"

echo "== a same-basename near-miss is NOT individuated =="
# The branch matches a basename; the individuate step matches the full path.
# Without that asymmetry, `--force` would rmtree .claude/agents and
# .claude/skills in any directory on disk named `substrate`.
for near in Convivy/worktrees/substrate elsewhere/substrate; do
    H=$(setup)
    run_co "$H" "$near" -r
    assert_eq "0" "$(echo "$KCTX_CALLS" | grep -qx 'use daytime --force' && echo 1 || echo 0)" \
        "$near: knowledge-ctx use is not invoked"
    assert_eq "1" "$([ -f "$H/$near/.claude/agents/scout.md" ] && echo 1 || echo 0)" \
        "$near: its .claude/agents survives"
    rm -rf "$H"
done

echo "== the gate still fires when ~/Convivy is itself a symlink =="
# THE BUG CLASS THIS FILE'S OWN COMMENTS INVOKE, PINNED RATHER THAN ASSERTED.
# Resolving `$HOME` alone and appending the rest as a literal leaves the
# identical bug one segment lower: a symlink at ~/Convivy — an external volume,
# a bind mount, a relocate-for-space move — keeps an unresolved segment on the
# gate side while `repo_root` is a realpath from `git rev-parse
# --show-toplevel`, so the two never compare equal and the individuate silently
# never fires (security-reviewer, PR #404). That direction fails SAFE, which is
# exactly why nothing would report it: the drift this branch exists to fix would
# simply go on not being fixed.
#
# The fake HOME already exercises a symlink in $HOME itself, since `mktemp -d`
# returns /var/... and /var is a symlink to /private/var on macOS. This puts one
# at the NEXT segment, which that cannot catch. The sibling
# test_co_crew_cockpits.sh carries the same case for the same gate; a reviewer
# verified this one by hand, and a hand check is not a guard.
H=$(setup)
mv "$H/Convivy" "$H/Convivy.real"
ln -s "$H/Convivy.real" "$H/Convivy"
run_co "$H" Convivy/substrate -r
assert_rc 0 "$RC" "symlinked ~/Convivy: substrate -r still succeeds"
assert_eq "1" "$(echo "$KCTX_CALLS" | grep -qx 'use daytime --force' && echo 1 || echo 0)" \
    "symlinked ~/Convivy: the individuate still fires"
rm -rf "$H"

echo "== a failed individuate aborts before launching =="
# The same contract the crew-cockpit branch keeps: a stale crew is worse than no
# session, and the usual cause is a live session holding the directory.
H=$(setup)
KCTX_RC=1 run_co "$H" Convivy/substrate -r
assert_rc 1 "$RC" "substrate -r aborts when knowledge-ctx fails"
assert_eq "" "$CLAUDE_ARGS" "claude is never launched"
assert_eq "1" "$(echo "$STDERR" | grep -q 'knowledge-ctx use failed' && echo 1 || echo 0)" \
    "the loud abort message fires"
rm -rf "$H"

finish
