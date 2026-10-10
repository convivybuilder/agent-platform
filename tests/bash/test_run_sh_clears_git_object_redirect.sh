#!/bin/bash
# tests/bash/test_run_sh_clears_git_object_redirect.sh — issue_01M4CWH0XFCV5CYMZVNDYSMP9C.
#
# A dispatched run's own environment carries dispatch_env's GIT_OBJECT_DIRECTORY /
# GIT_ALTERNATE_OBJECT_DIRECTORIES, redirecting git at the run's private object
# store (agent-platform#793). Inherited by run.sh, this redirected a test file's
# `git init --bare` into that private store instead of the repo the test itself
# created, leaving it with no objects/ directory — reproduced by the co on
# 2026-10-07, and a push into such a repo fails outright (the push's own local
# receive-pack, spawned as a subprocess, inherits the same redirect and no longer
# recognises the target as a git repository at all).
#
# Runs a COPY of the real run.sh in its own scratch directory, alongside one
# synthetic test file that performs the git init/push check, with the three vars
# set the way a dispatched run's own parent environment sets them. Never invoked
# against the real tests/bash directory itself — doing that would recurse into
# this very file.
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

SCRATCH=$(mktemp -d)
PRIVATE=$(mktemp -d)
SHARED=$(mktemp -d)
trap 'rm -rf "$SCRATCH" "$PRIVATE" "$SHARED"' EXIT

cp "$HERE/run.sh" "$SCRATCH/run.sh"

cat > "$SCRATCH/test_object_redirect_check.sh" <<'EOF'
#!/bin/bash
bare="$BUILDER_ROOT/withenv.git"
git init --bare "$bare" >/dev/null 2>&1
if [ ! -d "$bare/objects" ]; then
    echo "FAIL: git init --bare left no objects/ directory" >&2
    exit 1
fi
work="$BUILDER_ROOT/work"
git init -q "$work"
git -C "$work" -c user.name=t -c user.email=t@t commit -q --allow-empty -m x
if ! git -C "$work" push -q "$bare" HEAD:refs/heads/main; then
    echo "FAIL: push into the bare repo failed" >&2
    exit 1
fi
exit 0
EOF
chmod +x "$SCRATCH/test_object_redirect_check.sh"

out=$(GIT_OBJECT_DIRECTORY="$PRIVATE" GIT_ALTERNATE_OBJECT_DIRECTORIES="$SHARED" \
      GIT_DIR="$SCRATCH/not-a-real-gitdir" /bin/bash "$SCRATCH/run.sh" 2>&1)
rc=$?

assert_rc 0 "$rc" "run.sh (copy) exits 0 with GIT_OBJECT_DIRECTORY set in its parent env"
assert_contains "$out" "ALL BASH TESTS PASSED" \
    "the synthetic git-init/push check inside run.sh's own copy passed"

finish
