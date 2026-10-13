#!/bin/bash
# tests/bash/test_git_object_redirect_wrapper_scopes_to_own_repo.sh —
# issue_01M4QXDGVE43R2WYT01VDQQBPY.
#
# `dispatch_env`'s GIT_OBJECT_DIRECTORY / GIT_ALTERNATE_OBJECT_DIRECTORIES redirect is
# process-wide, so without the `git` wrapper it also installs on PATH, every git
# invocation the run makes — `git -C <another checkout> ...` included — reads objects
# only from the run's own private store and its own repository's shared `objects`,
# which is wrong for any OTHER repository. This test extracts the real wrapper script
# `dispatch_env.py` ships (`dispatch_env._GIT_WRAPPER_SCRIPT`, never a hand-copied
# duplicate of it) and runs it under /bin/bash (3.2 on the mini — ARCH-082) against two
# scratch repositories: the wrapper must let a cross-repository read see the OTHER
# repository's own objects, and must still leave the run's OWN repository's writes
# redirected to its private store.
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

SCRATCH=$(mktemp -d)
trap 'rm -rf "$SCRATCH"' EXIT

WRAPPER_DIR="$SCRATCH/wrapper"
mkdir -p "$WRAPPER_DIR"
PYTHONPATH="$REPO_ROOT/lib/agent-platform" /usr/bin/python3 -c '
import dispatch_env
import sys
sys.stdout.write(dispatch_env._GIT_WRAPPER_SCRIPT)
' > "$WRAPPER_DIR/git"
chmod +x "$WRAPPER_DIR/git"

A="$SCRATCH/A"
B="$SCRATCH/B"
PRIVATE="$SCRATCH/private-A"
mkdir -p "$A" "$B" "$PRIVATE"
git init -q -b main "$A"
git -C "$A" -c user.name=t -c user.email=t@t commit -q --allow-empty -m a
git init -q -b main "$B"
git -C "$B" -c user.name=t -c user.email=t@t commit -q --allow-empty -m b

# `command -v git` is wrong here: run from INSIDE a dispatched run (this test's own
# process, same as any other), the dispatching `dispatch_env` wrapper is first on THIS
# process's own PATH, so `command -v git` resolves to it instead of the real git binary.
# Feeding that back in as AGENTCTL_REAL_GIT for the wrapper under test is exactly the
# 2026-10-12 outage: the wrapper under test, given a self-referential AGENTCTL_REAL_GIT,
# recursed into the OTHER wrapper (this process's own), whose own AGENTCTL_REAL_GIT
# (inherited, unchanged) pointed at itself too, forking an unbounded chain — 9,256 bash
# processes between two runs before the host's per-user process limit stopped it. Walk
# $PATH by hand instead, skipping any entry that is itself a wrapper copy (a
# `/tmp/gitwrap-*` directory, or a `git` carrying the wrapper's marker line) — the same
# thing `dispatch_env._resolve_real_git` does for the dispatcher's own PATH, so this
# resolves to the actual git binary even when run inside a dispatched run.
GIT_WRAPPER_MARKER=$(PYTHONPATH="$REPO_ROOT/lib/agent-platform" /usr/bin/python3 -c '
import dispatch_env
import sys
sys.stdout.write(dispatch_env._GIT_WRAPPER_MARKER)
')

_find_real_git() {
    _old_ifs="$IFS"
    IFS=:
    for _dir in $PATH; do
        IFS="$_old_ifs"
        [ -n "$_dir" ] || _dir="."
        case "$_dir" in
            */gitwrap-*) continue ;;
        esac
        _candidate="$_dir/git"
        if [ -f "$_candidate" ] && [ -x "$_candidate" ]; then
            if grep -q "$GIT_WRAPPER_MARKER" "$_candidate" 2>/dev/null; then
                continue
            fi
            printf '%s\n' "$_candidate"
            return 0
        fi
        IFS=:
    done
    IFS="$_old_ifs"
    return 1
}

REAL_GIT=$(_find_real_git)
OWN_COMMON_DIR=$(cd "$A" && git rev-parse --path-format=absolute --git-common-dir)

# The failing case this wrapper fixes: a cross-repository read under the redirect.
out=$(AGENTCTL_REAL_GIT="$REAL_GIT" AGENTCTL_OWN_GIT_COMMON_DIR="$OWN_COMMON_DIR" \
      GIT_OBJECT_DIRECTORY="$PRIVATE" GIT_ALTERNATE_OBJECT_DIRECTORIES="$A/.git/objects" \
      /bin/bash "$WRAPPER_DIR/git" -C "$B" cat-file -t HEAD 2>&1)
rc=$?
assert_rc 0 "$rc" "git -C <other repo> cat-file -t HEAD exits 0 under the wrapper"
assert_eq "commit" "$out" "git -C <other repo> cat-file -t HEAD reports B's own commit, not 'bad object'"

# The converse the wrapper must not break: the run's OWN repository still redirects a
# new write to the private store, never the shared objects directory.
before_private=$(find "$PRIVATE" -type f | wc -l | tr -d ' ')
AGENTCTL_REAL_GIT="$REAL_GIT" AGENTCTL_OWN_GIT_COMMON_DIR="$OWN_COMMON_DIR" \
  GIT_OBJECT_DIRECTORY="$PRIVATE" GIT_ALTERNATE_OBJECT_DIRECTORIES="$A/.git/objects" \
  /bin/bash "$WRAPPER_DIR/git" -C "$A" -c user.name=t -c user.email=t@t \
  commit -q --allow-empty -m "own-repo redirected commit"
rc=$?
assert_rc 0 "$rc" "the run's own repository still commits under the wrapper"
after_private=$(find "$PRIVATE" -type f | wc -l | tr -d ' ')
grew="false"
[ "$after_private" -gt "$before_private" ] && grew="true"
assert_eq "true" "$grew" "the own-repository commit's new object landed in the private dir"

# issue_01M4R8KTY7THR8HYGJA849N3AH: `git init` and `git clone` create a repository that
# doesn't exist yet, so the cross-repository probe above can't resolve it. Run these from
# INSIDE the run's own worktree ($A, cwd, no -C) -- the exact shape of the reported
# failure (`git init -q --bare <scratch>/remote.git && git push ...`, issued from the
# run's own worktree with no -C/--git-dir of its own) -- so the probe, unfixed, would
# resolve the run's own repository and wrongly leave the redirect in place. Under that
# bug, the new repository's objects land in the run's private store instead of its own
# `objects` directory, and a push to it then fails with "does not appear to be a git
# repository".
INIT_BARE="$SCRATCH/init-bare/remote.git"
(cd "$A" && AGENTCTL_REAL_GIT="$REAL_GIT" AGENTCTL_OWN_GIT_COMMON_DIR="$OWN_COMMON_DIR" \
  GIT_OBJECT_DIRECTORY="$PRIVATE" GIT_ALTERNATE_OBJECT_DIRECTORIES="$A/.git/objects" \
  "$WRAPPER_DIR/git" init -q --bare "$INIT_BARE")
rc=$?
assert_rc 0 "$rc" "git init --bare under the redirect exits 0"
has_objects="false"
[ -d "$INIT_BARE/objects" ] && has_objects="true"
assert_eq "true" "$has_objects" "git init --bare under the redirect creates its own objects directory"

push_out=$(cd "$A" && AGENTCTL_REAL_GIT="$REAL_GIT" AGENTCTL_OWN_GIT_COMMON_DIR="$OWN_COMMON_DIR" \
  GIT_OBJECT_DIRECTORY="$PRIVATE" GIT_ALTERNATE_OBJECT_DIRECTORIES="$A/.git/objects" \
  "$WRAPPER_DIR/git" push -q "$INIT_BARE" HEAD:refs/heads/sandbox-check 2>&1 && echo PUSH_OK)
assert_contains "$push_out" "PUSH_OK" "a push to a git-init --bare target under the redirect succeeds"

# `git clone`: same gap, the other direction -- the clone's own objects directory, not
# the run's private store, must hold the transferred objects. Also issued from inside the
# run's own worktree, no -C.
CLONE_DEST="$SCRATCH/clone-dest"
(cd "$A" && AGENTCTL_REAL_GIT="$REAL_GIT" AGENTCTL_OWN_GIT_COMMON_DIR="$OWN_COMMON_DIR" \
  GIT_OBJECT_DIRECTORY="$PRIVATE" GIT_ALTERNATE_OBJECT_DIRECTORIES="$A/.git/objects" \
  "$WRAPPER_DIR/git" clone -q "$B" "$CLONE_DEST")
rc=$?
assert_rc 0 "$rc" "git clone under the redirect exits 0"
assert_eq "b" "$(git -C "$CLONE_DEST" log --format=%s -1 2>&1)" "the clone's own history reads B's commit"
clone_has_objects="false"
[ -n "$(find "$CLONE_DEST/.git/objects" -type f 2>/dev/null)" ] && clone_has_objects="true"
assert_eq "true" "$clone_has_objects" "git clone under the redirect writes the clone's own objects directory"

finish
