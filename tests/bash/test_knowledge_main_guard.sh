#!/bin/bash
# Tests for bin/install-knowledge-main-guard (ARCH-395): the pre-commit hook that
# refuses direct commits to the shared knowledge checkout's `main`.
#
# The carve-outs are the load-bearing part — a guard that also blocked scribe
# apply (detached HEAD) or every branch commit would break the KB pipeline it is
# meant to protect. Each is exercised against a real git repo under /bin/bash 3.2.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

INSTALLER="$REPO_ROOT/bin/install-knowledge-main-guard"

# The installer finds the knowledge checkout under the machine's root, which HOME
# does not move (company/decisions/0008), so a hermetic test names a tmp root
# holding a fake knowledge repo with --root-config, then installs and drives real
# commits through the hook.
setup() { # -> echoes fake HOME
    local home repo
    home=$(mktemp -d)
    repo="$home/Convivy/knowledge"
    mkdir -p "$repo"
    git -C "$repo" init -q
    # Force the branch to `main`. `git init`'s default is the runner's
    # init.defaultBranch — `master` on the GitHub macOS runner — so assuming
    # `main` fails there (this test went red on CI exactly that way). Set the
    # unborn HEAD before the first commit so it is deterministic everywhere.
    git -C "$repo" symbolic-ref HEAD refs/heads/main
    git -C "$repo" config user.email t@t; git -C "$repo" config user.name t
    git -C "$repo" config commit.gpgsign false
    echo seed > "$repo/seed.md"
    git -C "$repo" add -A; git -C "$repo" commit -qm seed
    HOME="$home" sh "$INSTALLER" --root-config "$(co_root_config "$home")" >/dev/null 2>&1
    echo "$home"
}

# commit_ok <repo> -> "ok" if a commit on the current HEAD succeeds, else "blocked"
try_commit() { # <repo> <file>
    echo x >> "$1/$2" 2>/dev/null || echo x > "$1/$2"
    git -C "$1" add -A >/dev/null 2>&1
    if git -C "$1" commit -qm "test commit" >/dev/null 2>&1; then echo ok; else echo blocked; fi
}

# --- 1. installed, and it is our hook ----------------------------------------
home=$(setup); repo="$home/Convivy/knowledge"
assert_eq "yes" "$([ -x "$repo/.git/hooks/pre-commit" ] && echo yes || echo no)" \
    "the pre-commit hook is installed and executable"
assert_eq "yes" "$(grep -q knowledge-main-guard "$repo/.git/hooks/pre-commit" && echo yes || echo no)" \
    "the hook carries our marker"

# --- 2. a commit on main is BLOCKED ------------------------------------------
git -C "$repo" checkout -q main
assert_eq "blocked" "$(try_commit "$repo" a.md)" "a direct commit to main is refused"

# --- 3. a commit on a feature branch PASSES ----------------------------------
git -C "$repo" checkout -qb feature/x
assert_eq "ok" "$(try_commit "$repo" b.md)" "a commit on a branch is allowed"

# --- 4. a commit from a real DETACHED linked worktree passes ------------------
# This is the kb-apply scenario exactly: `git worktree add --detach` creates
# a linked worktree whose commits resolve hooks back through the shared common
# hooks dir (where the guard lives), and whose HEAD is detached — so the guard's
# branch check finds no branch and passes it. If it did not, every KB
# factual-sync would break. A same-repo `checkout --detach` would test the
# branch logic but not the worktree hook-sharing that makes this load-bearing.
git -C "$repo" checkout -q main
wt="$repo-scribe-wt"
git -C "$repo" worktree add -q --detach "$wt" main
echo scribe > "$wt/c.md"; git -C "$wt" add -A >/dev/null 2>&1
if git -C "$wt" commit -qm "kb-apply-style commit" >/dev/null 2>&1; then dt=ok; else dt=blocked; fi
assert_eq "ok" "$dt" "a detached linked-worktree commit (kb apply) is allowed"
git -C "$repo" worktree remove --force "$wt" 2>/dev/null

# --- 5. --no-verify overrides on main ----------------------------------------
git -C "$repo" checkout -q main
echo y >> "$repo/d.md"; git -C "$repo" add -A >/dev/null 2>&1
if git -C "$repo" commit -q --no-verify -m override >/dev/null 2>&1; then ov=ok; else ov=blocked; fi
assert_eq "ok" "$ov" "--no-verify overrides the guard on main"
rm -rf "$home"

# --- 6. a foreign pre-commit hook is NOT clobbered ---------------------------
home=$(mktemp -d); repo="$home/Convivy/knowledge"; mkdir -p "$repo"
git -C "$repo" init -q
git -C "$repo" symbolic-ref HEAD refs/heads/main
printf '#!/bin/sh\necho foreign\n' > "$repo/.git/hooks/pre-commit"
chmod +x "$repo/.git/hooks/pre-commit"
HOME="$home" sh "$INSTALLER" --root-config "$(co_root_config "$home")" >/dev/null 2>&1
assert_eq "yes" "$(grep -q foreign "$repo/.git/hooks/pre-commit" && echo yes || echo no)" \
    "a pre-existing foreign pre-commit hook is left untouched"
assert_eq "no" "$(grep -q knowledge-main-guard "$repo/.git/hooks/pre-commit" && echo yes || echo no)" \
    "and our marker is NOT written over it"
rm -rf "$home"

# --- 7. idempotent: a second install rewrites cleanly, no duplication ---------
home=$(setup); repo="$home/Convivy/knowledge"
HOME="$home" sh "$INSTALLER" --root-config "$(co_root_config "$home")" >/dev/null 2>&1
assert_eq "1" "$(grep -c 'managed-by: agent-platform knowledge-main-guard' "$repo/.git/hooks/pre-commit")" \
    "re-install leaves exactly one managed-by marker (idempotent)"
rm -rf "$home"

# --- 8. the machine's root decides which checkout is guarded, not HOME --------
rh=$(mktemp -d); elsewhere=$(mktemp -d); repo="$rh/Convivy/knowledge"
mkdir -p "$repo"; git -C "$repo" init -q
HOME="$elsewhere" sh "$INSTALLER" --root-config "$(co_root_config "$rh")" >/dev/null 2>&1
assert_eq "yes" "$([ -x "$repo/.git/hooks/pre-commit" ] && echo yes || echo no)" \
    "the hook lands in the knowledge checkout under the configured root"
assert_eq "no" "$([ -e "$elsewhere/Convivy" ] && echo yes || echo no)" \
    "and HOME is not consulted"
rm -rf "$rh" "$elsewhere"

# --- 9. a root config that names no root exits 1, which install.sh reports ----
nr=$(mktemp -d)
out=$(HOME="$nr" sh "$INSTALLER" --root-config "$nr/no-such.env" 2>&1); rc=$?
assert_rc 1 "$rc" "a config naming no root exits 1 (install.sh reports a non-fatal skip)"
assert_contains "$out" "cannot resolve the machine's root" "and says why"
rm -rf "$nr"

finish
