#!/bin/bash
# Tests for lib/link-farm.sh's primary-checkout guard: the farm is the deployed
# platform library — every launchd job imports from it — so linking it from a
# linked worktree puts unmerged branch code live, which is the 2026-07-30
# incident (38 of 40 farm links pointed at a still-open PR's worktree). The
# guard refuses anything but the primary checkout unless --allow-non-primary is
# passed.
#
# Every run is hermetic: HOME points at a tmp dir, so $HOME/.local/lib is the
# fake HOME's, never the live farm.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

LINK_FARM="$REPO_ROOT/lib/link-farm.sh"

# Build a fake HOME holding a primary checkout with a committed lib module, and
# a linked worktree of it. Echoes the fake HOME.
setup() { # -> echoes fake HOME
    local home repo wt
    home=$(mktemp -d)
    repo="$home/Convivy/agent-platform"
    wt="$home/Convivy/worktrees/agent-platform.test"

    mkdir -p "$repo/lib/agent-platform"
    git -C "$repo" init -q
    git -C "$repo" symbolic-ref HEAD refs/heads/main
    _git_ident "$repo"
    echo "# a" > "$repo/lib/agent-platform/a.py"
    git -C "$repo" add -A
    git -C "$repo" commit -qm seed
    git -C "$repo" worktree add -q -b feat/test "$wt" || {
        echo "  FIXTURE BROKEN: cannot add worktree; every assertion below is meaningless" >&2
        exit 1
    }
    echo "$home"
}

_git_ident() { # <repo>
    git -C "$1" config user.email t@t
    git -C "$1" config user.name t
    git -C "$1" config commit.gpgsign false
}

run_farm() { # <home> <checkout> [extra args...] -> echoes rc, stderr in $home/farm.err
    local home=$1 checkout=$2
    shift 2
    HOME="$home" bash "$LINK_FARM" "$checkout" "$@" >/dev/null 2>"$home/farm.err"
    echo $?
}

# --- 1. a worktree checkout is refused ----------------------------------------
H=$(setup)
rc=$(run_farm "$H" "$H/Convivy/worktrees/agent-platform.test")
assert_rc 1 "$rc" "linking from a worktree exits non-zero"
case "$(cat "$H/farm.err")" in
    *REFUSING*primary*) _PASS=$((_PASS + 1));;
    *) _FAIL=$((_FAIL + 1)); echo "  FAIL: worktree refusal message does not say what it refused and why";;
esac
assert_eq "0" "$(test -e "$H/.local/lib/agent-platform" && echo 1 || echo 0)" "refused run creates no farm"
rm -rf "$H"

# --- 2. --allow-non-primary links from the worktree anyway ---------------------
H=$(setup)
rc=$(run_farm "$H" "$H/Convivy/worktrees/agent-platform.test" --allow-non-primary)
assert_rc 0 "$rc" "--allow-non-primary links from a worktree"
assert_eq "1" "$(test -L "$H/.local/lib/agent-platform/a.py" && echo 1 || echo 0)" "worktree link created with the flag"
rm -rf "$H"

# --- 3. the primary checkout is accepted ---------------------------------------
H=$(setup)
rc=$(run_farm "$H" "$H/Convivy/agent-platform")
assert_rc 0 "$rc" "the primary checkout links"
assert_eq "1" "$(test -L "$H/.local/lib/agent-platform/a.py" && echo 1 || echo 0)" "primary link created"
rm -rf "$H"

# --- 4. a non-git copy is refused ----------------------------------------------
H=$(setup)
mkdir -p "$H/copy/lib/agent-platform"
cp "$H/Convivy/agent-platform/lib/agent-platform/a.py" "$H/copy/lib/agent-platform/"
rc=$(run_farm "$H" "$H/copy")
assert_rc 1 "$rc" "a non-git copy is refused"
case "$(cat "$H/farm.err")" in
    *REFUSING*) _PASS=$((_PASS + 1));;
    *) _FAIL=$((_FAIL + 1)); echo "  FAIL: non-git copy refusal is not loud";;
esac
rm -rf "$H"

# --- 5. exported GIT_DIR + GIT_WORK_TREE do not bypass the guard ----------------
# The guard answers "is this the PRIMARY checkout" from git's own path
# resolution. A shell that exports GIT_DIR=<primary .git> and
# GIT_WORK_TREE=<worktree> makes git report the primary's git-dir (and common
# dir) while --show-toplevel returns the worktree, so all three checks agree on
# a non-primary checkout. The guard must scrub those variables and still refuse.
H=$(setup)
repo="$H/Convivy/agent-platform"
wt="$H/Convivy/worktrees/agent-platform.test"
rc=$(HOME="$H" GIT_DIR="$repo/.git" GIT_WORK_TREE="$wt" bash "$LINK_FARM" "$wt" >/dev/null 2>"$H/farm.err"; echo $?)
assert_rc 1 "$rc" "exported GIT_DIR+GIT_WORK_TREE does not bypass the guard"
case "$(cat "$H/farm.err")" in
    *REFUSING*) _PASS=$((_PASS + 1));;
    *) _FAIL=$((_FAIL + 1)); echo "  FAIL: GIT_DIR+GIT_WORK_TREE bypass produced no refusal";;
esac
assert_eq "0" "$(test -e "$H/.local/lib/agent-platform" && echo 1 || echo 0)" "bypassed run creates no farm"
rm -rf "$H"

# --- 6. correctly-set GIT_DIR/GIT_WORK_TREE for the primary still links ---------
# Scrubbing must not break a caller that exported the PRIMARY's own values: git
# rediscovers them from the checkout, so the guard still accepts it.
H=$(setup)
repo="$H/Convivy/agent-platform"
rc=$(HOME="$H" GIT_DIR="$repo/.git" GIT_WORK_TREE="$repo" bash "$LINK_FARM" "$repo" >/dev/null 2>"$H/farm.err"; echo $?)
assert_rc 0 "$rc" "GIT_DIR/GIT_WORK_TREE set to the primary's own values still links"
assert_eq "1" "$(test -L "$H/.local/lib/agent-platform/a.py" && echo 1 || echo 0)" "primary link created with env set"
rm -rf "$H"

# --- 7. the refusal names the resolved root, from the checkout's own sibling ---
# --- bin/builder-root, never a literal ~/Convivy or $HOME/Convivy -------------
H=$(setup)
wt="$H/Convivy/worktrees/agent-platform.test"
mkdir -p "$wt/bin"
cat > "$wt/bin/builder-root" <<'EOF'
#!/bin/sh
echo "/some/other/root"
EOF
chmod +x "$wt/bin/builder-root"
rc=$(run_farm "$H" "$wt")
assert_rc 1 "$rc" "still refused with a builder-root sibling present"
assert_contains "$(cat "$H/farm.err")" "/some/other/root/agent-platform" \
    "the fix line names the root resolved from the checkout's own builder-root"
assert_eq "0" "$(grep -c "$H/Convivy/agent-platform," "$H/farm.err")" \
    "the fix line is not the old hardcoded \$HOME/Convivy/agent-platform, ignoring the stub"
rm -rf "$H"

# --- 8. --prune removes a dangling symlink into the repo's own lib dir ---------
H=$(setup)
repo="$H/Convivy/agent-platform"
mkdir -p "$H/.local/lib/agent-platform"
ln -s "$repo/lib/agent-platform/ghost.py" "$H/.local/lib/agent-platform/ghost.py"
rc=$(run_farm "$H" "$repo" --prune)
assert_rc 0 "$rc" "--prune run exits 0"
assert_eq "0" "$(test -L "$H/.local/lib/agent-platform/ghost.py" && echo 1 || echo 0)" \
    "a dangling symlink into the repo's lib dir is pruned with --prune"
rm -rf "$H"

# --- 9. the same link is NOT pruned without --prune -----------------------------
# THE REGRESSION TEST FOR THE OPT-IN, and the most important case here: three
# callers share this script and only one of them (install.sh) may ever delete.
H=$(setup)
repo="$H/Convivy/agent-platform"
mkdir -p "$H/.local/lib/agent-platform"
ln -s "$repo/lib/agent-platform/ghost.py" "$H/.local/lib/agent-platform/ghost.py"
rc=$(run_farm "$H" "$repo")
assert_rc 0 "$rc" "a plain run (no --prune) exits 0"
assert_eq "1" "$(test -L "$H/.local/lib/agent-platform/ghost.py" && echo 1 || echo 0)" \
    "the dangling symlink survives a run with no --prune"
rm -rf "$H"

# --- 10. a .preinstall.<ts> regular file is never touched, with or without ------
# --- --prune. It is a real file from a pre-farm install, not a symlink. --------
H=$(setup)
repo="$H/Convivy/agent-platform"
mkdir -p "$H/.local/lib/agent-platform"
echo "backup" > "$H/.local/lib/agent-platform/a.py.preinstall.1234567890"
rc=$(run_farm "$H" "$repo")
assert_rc 0 "$rc" "a plain run exits 0 with a preinstall backup present"
assert_eq "1" "$(test -f "$H/.local/lib/agent-platform/a.py.preinstall.1234567890" && echo 1 || echo 0)" \
    "the preinstall backup survives a plain run"
rc=$(run_farm "$H" "$repo" --prune)
assert_rc 0 "$rc" "a --prune run exits 0 with a preinstall backup present"
assert_eq "1" "$(test -f "$H/.local/lib/agent-platform/a.py.preinstall.1234567890" && echo 1 || echo 0)" \
    "the preinstall backup survives --prune (it is a regular file, never a symlink)"
rm -rf "$H"

# --- 11. a symlink pointing OUTSIDE $REPO/lib/agent-platform/ with a missing ----
# --- target is not pruned — an operator's own link is not ours to delete -------
H=$(setup)
repo="$H/Convivy/agent-platform"
mkdir -p "$H/.local/lib/agent-platform" "$H/elsewhere"
ln -s "$H/elsewhere/ghost.py" "$H/.local/lib/agent-platform/external.py"
rc=$(run_farm "$H" "$repo" --prune)
assert_rc 0 "$rc" "--prune run exits 0 with an external dangling symlink present"
assert_eq "1" "$(test -L "$H/.local/lib/agent-platform/external.py" && echo 1 || echo 0)" \
    "a dangling symlink outside the repo's lib dir is not pruned"
rm -rf "$H"

# --- 12. a symlink whose target EXISTS is not pruned ----------------------------
H=$(setup)
repo="$H/Convivy/agent-platform"
rc=$(run_farm "$H" "$repo")
assert_rc 0 "$rc" "initial link build exits 0"
rc=$(run_farm "$H" "$repo" --prune)
assert_rc 0 "$rc" "--prune run with a live target exits 0"
assert_eq "1" "$(test -L "$H/.local/lib/agent-platform/a.py" && echo 1 || echo 0)" \
    "a link whose target still exists survives --prune"
rm -rf "$H"

# --- 13. pruning does not fire when the primary-checkout guard refuses ---------
H=$(setup)
wt="$H/Convivy/worktrees/agent-platform.test"
mkdir -p "$H/.local/lib/agent-platform"
ln -s "$wt/lib/agent-platform/ghost.py" "$H/.local/lib/agent-platform/ghost.py"
rc=$(run_farm "$H" "$wt" --prune)
assert_rc 1 "$rc" "a refused non-primary checkout still exits non-zero with --prune"
assert_eq "1" "$(test -L "$H/.local/lib/agent-platform/ghost.py" && echo 1 || echo 0)" \
    "a stale link is not pruned when the primary-checkout guard refuses, even with --prune"
rm -rf "$H"

# --- 14. --prune with --allow-non-primary is REFUSED, and refused before any ---
# --- write. The escape hatch waives the guard; --prune deletes from the -------
# --- deployed library. Together a non-primary checkout can leave a module ------
# --- with NO link, which is worse than the wrong link the guard prevents. ------
H=$(setup)
wt="$H/Convivy/worktrees/agent-platform.test"
mkdir -p "$H/.local/lib/agent-platform"
ln -s "$wt/lib/agent-platform/ghost.py" "$H/.local/lib/agent-platform/ghost.py"
rc=$(run_farm "$H" "$wt" --allow-non-primary --prune)
assert_rc 2 "$rc" "--prune with --allow-non-primary is refused with a usage error"
assert_contains "$(cat "$H/farm.err")" "REFUSING --prune together with --allow-non-primary" \
    "the refusal names both flags"
assert_eq "1" "$(test -L "$H/.local/lib/agent-platform/ghost.py" && echo 1 || echo 0)" \
    "the stale link survives the refusal — nothing was deleted"
assert_eq "0" "$(test -L "$H/.local/lib/agent-platform/a.py" && echo 1 || echo 0)" \
    "and nothing was LINKED either — the refusal precedes every write"
rm -rf "$H"

# --- 15. --allow-non-primary alone still links from a worktree, unchanged ------
# --- by this PR. The refusal above must not have broken the escape hatch. ------
H=$(setup)
wt="$H/Convivy/worktrees/agent-platform.test"
rc=$(run_farm "$H" "$wt" --allow-non-primary)
assert_rc 0 "$rc" "--allow-non-primary alone still exits 0"
assert_eq "1" "$(test -L "$H/.local/lib/agent-platform/a.py" && echo 1 || echo 0)" \
    "--allow-non-primary alone still links from a non-primary checkout"
rm -rf "$H"

echo "  $_PASS passed, $_FAIL failed"
[ "$_FAIL" -eq 0 ]
