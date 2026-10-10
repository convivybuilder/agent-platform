#!/bin/bash
# Tests for bin/co's `_co_pull_knowledge`, exercised through the crew-cockpit
# `-r` path (`co -r` in ~/Convivy/library), which is where the operator hit this live.
#
# THE BUG. `_co_pull_knowledge` used to run bare `git pull --ff-only`, which
# fetches then merges `FETCH_HEAD`. `~/Convivy/knowledge` has a SECOND regular
# fetcher of the same checkout: the `com.convivy.knowledge-autopull` launchd job
# (`agentctl kb sync`), on a 600s timer. Two fetches landing in the same window
# interleave their writes to FETCH_HEAD; a corrupted file with more than one
# line marked "for merge" makes `git pull --ff-only` fail with "Cannot
# fast-forward to multiple branches" even though `origin/main` itself is fine.
# This blocked `co -r` outright (2026-08-20, the operator's 08:19 session).
#
# THE FIX fetches straight into the `origin/main` tracking ref and merges from
# THAT ref, never consulting FETCH_HEAD, and that pattern now lives in
# `agentctl kb sync`, which `_co_pull_knowledge` calls instead of running git's
# network commands itself. The reason is the credential: `co`'s own
# `git fetch origin` went over the SSH remote as the operator's personal key, while
# `kb sync` fetches over https as the convivy-library App. So the stub
# `agentctl` (lib.sh's write_kb_sync_stub) stands in for the App-authenticated
# fetch, and what THESE tests pin is what `co` itself does:
#   - it runs `agentctl kb sync`, exactly once, and no network git of its own
#     (no fetch, pull, merge or ls-remote), and never `gh`;
#   - it tells the three outcomes apart: success carries on, a fetch that failed
#     with exit 0 ("likely offline") carries on WITH a warning, and a non-zero
#     exit aborts the launch.
#
# The interleaved-FETCH_HEAD race itself is not reproduced here — it needs two
# processes racing on a single file write, which is inherently timing-dependent
# and would make this suite flaky. Instead these tests pin the INVARIANT that
# closes the whole bug class: `_co_pull_knowledge` must never run a bare
# `git pull`. A git spy on PATH makes that a behavioural assertion, not a source
# grep; the stub runs the real git by absolute path, so the spy sees only `co`.
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

# Fake HOME with a real ~/Convivy/knowledge checkout tracking a real bare
# origin, plus ~/Convivy/library as an individuated crew cockpit. Echoes the
# fake HOME.
#
# The bare origin's HEAD is set to `main` EXPLICITLY (same fixture bug
# test_agent_platform_autopull.sh documents): `git init --bare` points HEAD at
# init.defaultBranch, `master` on the GitHub macOS runner and `main` locally,
# and cloning a repo whose HEAD names a nonexistent ref produces an EMPTY
# working tree that fails every assertion below for the wrong reason.
setup() { # -> echoes fake HOME
    local home origin kb lib
    home=$(mktemp -d)
    origin="$home/origin.git"
    kb="$home/Convivy/knowledge"
    lib="$home/Convivy/library"

    git init -q --bare "$origin"
    git -C "$origin" symbolic-ref HEAD refs/heads/main

    # Seed origin from an INIT'd throwaway working copy, never a clone of it —
    # cloning an empty bare repo prints "cloned an empty repository" and (on
    # some git versions) leaves an empty working tree, burying every assertion
    # below under a spurious failure (the exact fixture bug
    # test_agent_platform_autopull.sh's setup() documents).
    local seed
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

    mkdir -p "$lib/.claude/agents"
    : > "$lib/.claude/agents/scout.md"
    ( cd "$lib" && git init -q . )

    mkdir -p "$home/stubbin"
    cat > "$home/stubbin/claude" <<'EOF'
#!/bin/bash
printf '%s\n' "$@" > "$CLAUDE_CALL_LOG"
exit 0
EOF
    chmod +x "$home/stubbin/claude"
    cat > "$home/stubbin/knowledge-ctx" <<'EOF'
#!/bin/bash
exit 0
EOF
    chmod +x "$home/stubbin/knowledge-ctx"
    # A spy that logs every git invocation verbatim, then delegates to the real
    # git. Lets the tests assert on WHAT `_co_pull_knowledge` ran, not just on
    # the end state — the actual invariant this file exists to pin.
    cat > "$home/stubbin/git" <<EOF
#!/bin/bash
echo "\$@" >> "\$GIT_SPY_LOG"
exec "$REAL_GIT" "\$@"
EOF
    chmod +x "$home/stubbin/git"
    # `gh` must never be reached: the old preflight ran `gh auth login` as the operator.
    cat > "$home/stubbin/gh" <<'EOF'
#!/bin/bash
echo "$@" >> "$GH_SPY_LOG"
exit 1
EOF
    chmod +x "$home/stubbin/gh"
    write_kb_sync_stub "$home/stubbin"

    echo "$home"
}

# Add a commit to origin so the knowledge checkout falls behind.
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

kb_head() { git -C "$1/Convivy/knowledge" rev-parse HEAD; }

# run_co_r <home> -> sets RC, STDERR, GIT_CALLS (one line per git invocation `co`
# itself makes), AGENTCTL_CALLS, GH_CALLS, CLAUDE_ARGS (empty when claude never ran)
run_co_r() {
    local home=$1
    local stderr_log spy_log agentctl_log gh_log claude_log
    stderr_log=$(mktemp)
    spy_log=$(mktemp)
    agentctl_log=$(mktemp)
    gh_log=$(mktemp)
    claude_log=$(mktemp)
    ( cd "$home/Convivy/library" && \
      HOME="$home" PATH="$home/stubbin:$PATH" \
      CLAUDE_CALL_LOG="$claude_log" GIT_SPY_LOG="$spy_log" \
      AGENTCTL_LOG="$agentctl_log" AGENTCTL_STUB_GIT="$REAL_GIT" GH_SPY_LOG="$gh_log" \
      /bin/zsh "$CO" --root-config "$(co_root_config "$home")" -r >/dev/null 2>"$stderr_log" )
    RC=$?
    STDERR=$(cat "$stderr_log")
    GIT_CALLS=$(cat "$spy_log")
    AGENTCTL_CALLS=$(cat "$agentctl_log")
    GH_CALLS=$(cat "$gh_log")
    CLAUDE_ARGS=$(cat "$claude_log")
    rm -f "$stderr_log" "$spy_log" "$agentctl_log" "$gh_log" "$claude_log"
}

echo "== clean fast-forward moves the knowledge checkout =="
H=$(setup)
before=$(kb_head "$H")
advance_origin "$H"
run_co_r "$H"
after=$(kb_head "$H")
assert_rc 0 "$RC" "clean fast-forward: co -r succeeds"
if [ "$before" != "$after" ]; then _PASS=$((_PASS + 1)); else
    _FAIL=$((_FAIL + 1)); echo "  FAIL: clean fast-forward did not move the checkout"; fi
rm -rf "$H"

echo "== co runs no network git of its own, and never gh =="
# This is the credential fix, pinned behaviourally. Any fetch, pull, merge or
# ls-remote from `co` would go over the knowledge checkout's SSH remote as the operator;
# `gh` is what the retired preflight reached for. The pull, including the
# tracking-ref merge that keeps FETCH_HEAD out of it, is `agentctl kb sync`'s.
H=$(setup)
advance_origin "$H"
run_co_r "$H"
assert_rc 0 "$RC" "fast-forward with spy git still succeeds"
assert_eq "agentctl kb sync" "$AGENTCTL_CALLS" "co calls agentctl kb sync, once, with no other arguments"
assert_eq "0" "$(echo "$GIT_CALLS" | grep -c '^pull\b' || true)" \
    "no invocation of git starts with 'pull'"
assert_eq "0" "$(echo "$GIT_CALLS" | grep -cE '(^| )(fetch|merge|ls-remote)( |$)' || true)" \
    "co itself runs no git fetch, merge or ls-remote"
assert_eq "1" "$(echo "$GIT_CALLS" | grep -cx 'checkout main' || true)" \
    "co still puts the checkout on main before the sync"
assert_eq "" "$GH_CALLS" "gh is never called"
assert_eq "1" "$(echo "$STDERR" | grep -q 'kb sync: up to date, verified' && echo 1 || echo 0)" \
    "kb sync's own output reaches the terminal"
assert_eq "0" "$(echo "$STDERR" | grep -q 'fetching the knowledge remote failed' && echo 1 || echo 0)" \
    "a successful sync prints no offline warning"
rm -rf "$H"

echo "== a diverged checkout refuses rather than forcing =="
H=$(setup)
advance_origin "$H"
echo local > "$H/Convivy/knowledge/localonly.md"
git -C "$H/Convivy/knowledge" add -A
git -C "$H/Convivy/knowledge" commit -qm "local-only"
before=$(kb_head "$H")
run_co_r "$H"
assert_rc 1 "$RC" "diverged checkout: co -r aborts loudly"
assert_eq "$before" "$(kb_head "$H")" "diverged checkout does not move HEAD"
assert_eq "1" "$(echo "$STDERR" | grep -q 'knowledge pull failed' && echo 1 || echo 0)" \
    "diverged checkout: the loud abort message fires"
rm -rf "$H"

echo "== already current: still succeeds, checkout unmoved =="
H=$(setup)
before=$(kb_head "$H")
run_co_r "$H"
assert_rc 0 "$RC" "already current: co -r still succeeds"
assert_eq "$before" "$(kb_head "$H")" "already current: HEAD does not move"
rm -rf "$H"

echo "== fetch failed with exit 0 (offline): the session still starts, with a warning =="
H=$(setup)
advance_origin "$H"
before=$(kb_head "$H")
KB_SYNC_MODE=offline run_co_r "$H"
assert_rc 0 "$RC" "offline: co -r still exits 0"
assert_eq "1" "$([ -n "$CLAUDE_ARGS" ] && echo 1 || echo 0)" "offline: claude still launches"
assert_eq "$before" "$(kb_head "$H")" "offline: the checkout is unmoved"
assert_contains "$STDERR" "fetch failed (likely offline)" "offline: kb sync's own line is shown"
assert_contains "$STDERR" "fetching the knowledge remote failed — see above" \
    "offline: co warns, and points at kb sync's line rather than naming a cause"
rm -rf "$H"

echo "== kb sync exits 1, could not run or refused (stub: identity refused): the launch aborts, visibly =="
H=$(setup)
advance_origin "$H"
before=$(kb_head "$H")
KB_SYNC_MODE=refused run_co_r "$H"
assert_rc 1 "$RC" "refused: co -r exits 1"
assert_eq "" "$CLAUDE_ARGS" "refused: claude never launches"
assert_eq "$before" "$(kb_head "$H")" "refused: the checkout is unmoved"
assert_contains "$STDERR" "refused — cannot obtain the convivy-library App identity" \
    "refused: kb sync's own reason is shown"
assert_contains "$STDERR" "agentctl kb sync exited 1" "refused: co names the exit code"
assert_contains "$STDERR" "the refresh did not complete" "refused: the abort line says the refresh did not complete"
assert_contains "$STDERR" "knowledge pull failed" "refused: the loud abort message fires"
assert_eq "0" "$(echo "$STDERR" | grep -q 'fetching the knowledge remote failed' && echo 1 || echo 0)" \
    "refused: it is not reported as offline"
assert_eq "" "$GH_CALLS" "refused: no fallback to gh"
rm -rf "$H"

echo "== index inconsistent after a heal (exit 2): the launch aborts =="
H=$(setup)
KB_SYNC_MODE=unverified run_co_r "$H"
assert_rc 1 "$RC" "unverified: co -r exits 1"
assert_eq "" "$CLAUDE_ARGS" "unverified: claude never launches"
assert_contains "$STDERR" "STILL INCONSISTENT" "unverified: kb sync's own line is shown"
assert_contains "$STDERR" "agentctl kb sync exited 2" "unverified: co names the exit code"
# Exit 2 says nothing about the checkout: `kb sync` exits 2 after a fast-forward and
# also on the origin/main == HEAD path, where nothing moved. A line saying the
# checkout "was not refreshed" is false for the first, and one saying it was is
# false for the second. The message must hold for exit 1 and exit 2 alike, so it
# says only that the refresh did not complete. (This stub's `unverified` mode
# happens not to move the checkout; the assertion does not depend on that.)
assert_contains "$STDERR" "the refresh did not complete" "unverified: the abort line is true for exit 2"
assert_eq "0" "$(echo "$STDERR" | grep -q 'was not refreshed' && echo 1 || echo 0)" \
    "unverified: it does not claim the checkout was left unrefreshed"
rm -rf "$H"

echo "== the offline line co matches on is the one agentctl prints =="
# co branches on the TEXT of kb sync's offline message, because exit 0 covers both
# a clean sync and a failed fetch. The stub above prints whatever THIS file says,
# so a reworded message in bin/agentctl would leave every offline test green while
# a real session started on a stale checkout unannounced. The co side of the match
# is covered by the offline case itself; this pins the agentctl side.
assert_eq "1" "$(grep -q 'kb sync: fetch failed (likely offline)' "$REPO_ROOT/bin/agentctl" && echo 1 || echo 0)" \
    "bin/agentctl still prints 'kb sync: fetch failed (likely offline)'"

finish
