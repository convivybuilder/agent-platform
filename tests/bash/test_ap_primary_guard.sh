#!/bin/bash
# Tests for bin/install-agent-platform-primary-guard (ARCH-396): hooks that keep
# the agent-platform PRIMARY checkout on main.
#
# The primary-vs-worktree distinction is the load-bearing carve-out — a guard that
# also blocked worktree commits would break the exact flow it exists to encourage
# (ARCH-386). Exercised against a real repo + a real linked worktree under bash 3.2.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

INSTALLER="$REPO_ROOT/bin/install-agent-platform-primary-guard"

setup() { # -> echoes fake HOME (with an installed agent-platform primary)
    local home repo
    home=$(mktemp -d)
    repo="$home/Convivy/agent-platform"
    mkdir -p "$repo"
    git -C "$repo" init -q
    git -C "$repo" symbolic-ref HEAD refs/heads/main   # deterministic on any runner
    git -C "$repo" config user.email t@t; git -C "$repo" config user.name t
    git -C "$repo" config commit.gpgsign false
    echo seed > "$repo/seed.md"
    git -C "$repo" add -A; git -C "$repo" commit -qm seed
    HOME="$home" sh "$INSTALLER" --root-config "$(co_root_config "$home")" >/dev/null 2>&1
    echo "$home"
}

try_commit() { # <dir> <file> -> ok|blocked
    echo x >> "$1/$2" 2>/dev/null || echo x > "$1/$2"
    git -C "$1" add -A >/dev/null 2>&1
    if git -C "$1" commit -qm t >/dev/null 2>&1; then echo ok; else echo blocked; fi
}

# --- 1. both hooks installed + marked ----------------------------------------
home=$(setup); repo="$home/Convivy/agent-platform"
assert_eq "yes" "$([ -x "$repo/.git/hooks/pre-commit" ] && [ -x "$repo/.git/hooks/post-checkout" ] && echo yes || echo no)" \
    "pre-commit and post-checkout are installed and executable"
assert_eq "yes" "$(grep -q 'primary-guard (ARCH-396)' "$repo/.git/hooks/pre-commit" && echo yes || echo no)" \
    "the pre-commit hook carries our marker"
assert_eq "yes" "$(grep -q "git worktree add $home/Convivy/worktrees/agent-platform" \
    "$repo/.git/hooks/pre-commit" && echo yes || echo no)" \
    "the BLOCKED message's worktree-add line names THIS machine's resolved root, not a literal ~/Convivy"

# --- 2. a commit in the PRIMARY is blocked (any branch) ----------------------
git -C "$repo" checkout -q main
assert_eq "blocked" "$(try_commit "$repo" a.md)" "a commit in the primary on main is refused"
git -C "$repo" checkout -qb feature/x 2>/dev/null
assert_eq "blocked" "$(try_commit "$repo" b.md)" "a commit in the primary on a feature branch is refused"

# --- 3. a commit in a WORKTREE is allowed (the carve-out) ---------------------
# This is the whole point: worktrees share the common hooks dir, so the hook runs
# there too, and only is_primary keeps it from blocking the desired flow.
git -C "$repo" checkout -q main
wt="$repo-wt"
git -C "$repo" worktree add -q "$wt" -b work origin-none 2>/dev/null || git -C "$repo" worktree add -q "$wt" -b work
assert_eq "ok" "$(try_commit "$wt" c.md)" "a commit in a linked worktree is allowed"
git -C "$repo" worktree remove --force "$wt" 2>/dev/null

# --- 4. --no-verify overrides in the primary ---------------------------------
git -C "$repo" checkout -q main
echo y >> "$repo/d.md"; git -C "$repo" add -A >/dev/null 2>&1
if git -C "$repo" commit -q --no-verify -m ov >/dev/null 2>&1; then ov=ok; else ov=blocked; fi
assert_eq "ok" "$ov" "--no-verify overrides the primary commit block"

# --- 5. post-checkout WARNS switching the primary to a non-main branch --------
# (It cannot block; we assert the warning reaches stderr.)
git -C "$repo" checkout -q main
warn="$(git -C "$repo" checkout -qb feature/warn-me 2>&1)"
assert_eq "yes" "$(printf '%s' "$warn" | grep -q "PRIMARY is now on 'feature/warn-me'" && echo yes || echo no)" \
    "switching the primary to a non-main branch warns"
# switching back to main is quiet
quiet="$(git -C "$repo" checkout -q main 2>&1)"
assert_eq "" "$(printf '%s' "$quiet" | grep 'PRIMARY is now on')" "switching back to main does not warn"
# detached HEAD warns too — a `checkout <sha>` also strands cron on an arbitrary
# commit (#206 review). Match the DETACHED-specific line so a named-branch warning
# can't satisfy this by accident.
det="$(git -C "$repo" checkout -q --detach HEAD 2>&1)"
assert_eq "yes" "$(printf '%s' "$det" | grep -q "PRIMARY is now in DETACHED HEAD" && echo yes || echo no)" \
    "switching the primary to a detached HEAD warns"
rm -rf "$home"

# --- 6. a foreign pre-commit hook is not clobbered ---------------------------
home=$(mktemp -d); repo="$home/Convivy/agent-platform"; mkdir -p "$repo"
git -C "$repo" init -q
git -C "$repo" symbolic-ref HEAD refs/heads/main
printf '#!/bin/sh\necho foreign\n' > "$repo/.git/hooks/pre-commit"
chmod +x "$repo/.git/hooks/pre-commit"
HOME="$home" sh "$INSTALLER" --root-config "$(co_root_config "$home")" >/dev/null 2>&1
assert_eq "yes" "$(grep -q foreign "$repo/.git/hooks/pre-commit" && echo yes || echo no)" \
    "a pre-existing foreign pre-commit is left untouched"
assert_eq "no" "$(grep -q 'primary-guard (ARCH-396)' "$repo/.git/hooks/pre-commit" && echo yes || echo no)" \
    "and our marker is not written over it"
# The post-checkout path is guarded identically by write_hook — pin it too so the
# two are not silently allowed to diverge (#206 review).
printf '#!/bin/sh\necho foreign-pc\n' > "$repo/.git/hooks/post-checkout"
chmod +x "$repo/.git/hooks/post-checkout"
HOME="$home" sh "$INSTALLER" --root-config "$(co_root_config "$home")" >/dev/null 2>&1
assert_eq "yes" "$(grep -q foreign-pc "$repo/.git/hooks/post-checkout" && echo yes || echo no)" \
    "a pre-existing foreign post-checkout is left untouched"
rm -rf "$home"

# --- 7. idempotent -----------------------------------------------------------
home=$(setup); repo="$home/Convivy/agent-platform"
HOME="$home" sh "$INSTALLER" --root-config "$(co_root_config "$home")" >/dev/null 2>&1
assert_eq "1" "$(grep -c 'managed-by: agent-platform primary-guard' "$repo/.git/hooks/pre-commit")" \
    "re-install leaves exactly one managed-by marker in pre-commit (idempotent)"
rm -rf "$home"

# --- 8. the machine's root decides which checkout is guarded, not HOME --------
# company/decisions/0008: the installer finds the primary through bin/builder-root,
# which HOME does not move, and a test names the root with --root-config.
rh=$(mktemp -d); elsewhere=$(mktemp -d); repo="$rh/Convivy/agent-platform"
mkdir -p "$repo"; git -C "$repo" init -q
HOME="$elsewhere" sh "$INSTALLER" --root-config "$(co_root_config "$rh")" >/dev/null 2>&1
assert_eq "yes" "$([ -x "$repo/.git/hooks/pre-commit" ] && echo yes || echo no)" \
    "the hooks land in the checkout under the configured root"
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
