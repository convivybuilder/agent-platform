#!/bin/bash
# Tests for bin/co's crew-cockpit branch: ~/Convivy/{library,agent-platform,
# convivy-orient}, the three directories the 2026-08-17 crew split created.
# They launch bare Claude Code with NO --agent, the way substrate and the other
# named cockpits do, because a crew co holds the dispatch role itself rather
# than handing it to a planning-orchestrator none of them individuates.
#
# THE PROPERTY THESE TESTS EXIST TO GUARD IS THE GATE, not the launch. This is a
# `--permission-mode bypassPermissions` branch, so what it matches decides where
# unrestricted Claude starts. Two ways that goes wrong, and one of them nearly
# shipped:
#
#   1. Matching on BASENAME instead of full path. The first implementation did,
#      following its siblings. `library` is a generic directory name — unlike
#      `hollis` or `convivy-app-kit` — so a scratch clone or a
#      `git worktree add ../library` anywhere on disk would have taken the
#      branch and launched bypassPermissions in unvetted content
#      (security-reviewer, PR #404). The gate is now three absolute paths.
#
#   2. Loosening the match to catch near-misses. A worktree is named
#      `agent-platform.<purpose>` by house convention, so a prefix or substring
#      match would route every reviewer worktree into the no-agent crew path.
#
# A near-miss must fall through to the pre-existing "not individuated" error,
# which is loud and tells a person what to do. Silently launching bare
# bypassPermissions Claude instead is what this file is here to catch.
#
# `co` is zsh (excluded from the ARCH-082 bash-3.2 contract by lint.sh's
# shebang filter); bash invoking it here is exactly how a login shell will.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

CO="$REPO_ROOT/bin/co"

# A fake HOME holding the three real cockpits plus every near-miss. Each is a
# git repo, because the branch reads `git rev-parse --show-toplevel`, and each
# carries an individuated agent so a fall-through cannot be confused with the
# brand-new-directory generic tail (that path is test_co_generic_tail.sh's).
setup() { # -> echoes fake HOME
    local home d
    home=$(mktemp -d)
    mkdir -p "$home/stubbin"

    for d in Convivy/library Convivy/agent-platform Convivy/convivy-orient \
             Convivy/library2 Convivy/worktrees/agent-platform.reviewer \
             Convivy/nested/library elsewhere/library; do
        mkdir -p "$home/$d/.claude/agents"
        : > "$home/$d/.claude/agents/scout.md"
        ( cd "$home/$d" && git init -q . )
    done

    cat > "$home/stubbin/claude" <<'EOF'
#!/bin/bash
printf '%s\n' "$@" > "$CLAUDE_CALL_LOG"
exit 0
EOF
    chmod +x "$home/stubbin/claude"

    echo "$home"
}

# run_co_in <home> <dir-relative-to-home> [args...] -> sets RC, CLAUDE_ARGS, STDERR
run_co_in() {
    local home=$1 rel=$2
    shift 2
    local call_log stderr_log
    call_log=$(mktemp)
    stderr_log=$(mktemp)
    ( cd "$home/$rel" && \
      HOME="$home" PATH="$home/stubbin:$PATH" CLAUDE_CALL_LOG="$call_log" \
      /bin/zsh "$CO" --root-config "$(co_root_config "$home")" "$@" >/dev/null 2>"$stderr_log" )
    RC=$?
    CLAUDE_ARGS=$(cat "$call_log")
    STDERR=$(cat "$stderr_log")
    rm -f "$call_log" "$stderr_log"
}

echo "== the three crew cockpits launch bare claude, with no agent =="
for crew in library agent-platform convivy-orient; do
    H=$(setup)
    run_co_in "$H" "Convivy/$crew"
    assert_eq "1" "$(echo "$CLAUDE_ARGS" | grep -q -- '--permission-mode' && echo 1 || echo 0)" \
        "$crew: launches claude"
    assert_eq "0" "$(echo "$CLAUDE_ARGS" | grep -q -- '--agent' && echo 1 || echo 0)" \
        "$crew: launches with NO --agent — the charter supplies the role"
    assert_eq "0" "$(echo "$STDERR" | grep -q 'is not individuated' && echo 1 || echo 0)" \
        "$crew: never reaches the generic agent-selection path"
    rm -rf "$H"
done

echo "== the gate resolves the WHOLE path, not just \$HOME =="
# The first fix here resolved $HOME and appended "/Convivy" as a literal, which
# left the identical bug one segment lower: a symlink at ~/Convivy — an external
# volume, a bind mount, a relocate-for-space move — makes the array side keep an
# unresolved segment while repo_root is a realpath, so the two never compare
# equal and the branch silently never fires (security-reviewer, PR #404).
#
# The fake HOME above already exercises a symlink in $HOME itself, because
# mktemp -d returns /var/... and /var is a symlink to /private/var on macOS.
# This case puts one at the NEXT segment, which that one cannot catch.
H=$(setup)
mv "$H/Convivy" "$H/Convivy.real"
ln -s "$H/Convivy.real" "$H/Convivy"
for crew in library agent-platform convivy-orient; do
    run_co_in "$H" "Convivy/$crew"
    assert_eq "1" "$(echo "$CLAUDE_ARGS" | grep -q -- '--permission-mode' && echo 1 || echo 0)" \
        "$crew: still matches when ~/Convivy is itself a symlink"
done
rm -rf "$H"

echo "== a near-miss never takes a bypassPermissions branch =="
# The gate is three absolute paths, so each of these differs from a real
# cockpit in exactly one way a looser match would have missed.
for near in \
    "Convivy/library2|a longer name sharing the prefix" \
    "Convivy/worktrees/agent-platform.reviewer|a house-convention worktree dir" \
    "Convivy/nested/library|the right basename at the wrong depth" \
    "elsewhere/library|the right basename outside ~/Convivy"
do
    rel=${near%%|*}
    why=${near#*|}
    H=$(setup)
    run_co_in "$H" "$rel"
    assert_eq "0" "$(echo "$CLAUDE_ARGS" | grep -q -- '--permission-mode' && echo 1 || echo 0)" \
        "$rel ($why): must NOT launch bypassPermissions claude"
    rm -rf "$H"
done

finish
