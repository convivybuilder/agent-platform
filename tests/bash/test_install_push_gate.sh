#!/bin/bash
# Tests for bin/install-push-gate — the installer that puts the content
# gate's pre-push hook into each engine clone (agent-platform, library,
# convivy-orient, convivy-deck).
#
# Every clone here is a real, from-scratch git repository under a temp root
# named with --root-config (company/decisions/0008), never a real clone —
# bin/install-push-gate's own warning is never to run it, or install.sh,
# without --root-config pointing at a temp root, and this file honours that.
# A real `git push` to a temp `file://` bare remote exercises the installed
# stub exactly as production would, with a stand-in `push-content-gate` this
# file writes and controls completely, standing in for the deploy area's
# real copy.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

INSTALLER="$REPO_ROOT/bin/install-push-gate"
MARKER="managed-by: agent-platform push-gate"
CLONES="agent-platform library convivy-orient convivy-deck"

_git_ident() { # <repo>
    git -C "$1" config user.email t@t
    git -C "$1" config user.name t
    git -C "$1" config commit.gpgsign false
}

# _mode <file> -> octal mode (e.g. "755"), BSD stat (macOS) or GNU stat (CI
# running Linux bash under /bin/bash, if ever) — whichever this host has.
_mode() {
    stat -f '%Lp' "$1" 2>/dev/null || stat -c '%a' "$1" 2>/dev/null
}

# seed_clone <path>: a minimal, real git repo standing in for one engine
# clone — just enough for the installer to find a .git/hooks directory.
seed_clone() { # <path>
    mkdir -p "$1"
    git -C "$1" init -q
    git -C "$1" symbolic-ref HEAD refs/heads/main
    _git_ident "$1"
}

# setup [<clones to seed>] -> echoes a fake HOME, with $HOME/Convivy/<repo>
# seeded as a real git repo for each name given (default: all four).
setup() {
    local home clones repo
    home=$(mktemp -d)
    clones="${1:-$CLONES}"
    for repo in $clones; do
        seed_clone "$home/Convivy/$repo"
    done
    echo "$home"
}

# write_stand_in_py <dir>: a venv/bin/python3 wrapper at <dir>/venv/bin,
# calling this host's own python3 — standing in for the real pinned
# interpreter install-agent-platform-shared builds, which these tests never
# build for real.
write_stand_in_py() { # <deploy dir>
    mkdir -p "$1/venv/bin"
    printf '#!/bin/sh\nexec python3 "$@"\n' > "$1/venv/bin/python3"
    chmod +x "$1/venv/bin/python3"
}

# run_installer <home> [extra installer args...]
run_installer() {
    local home=$1; shift
    HOME="$home" sh "$INSTALLER" --root-config "$(co_root_config "$home")" "$@"
}

hook_of() { echo "$1/Convivy/$2/.git/hooks/pre-push"; } # <home> <repo>

# --- 1. installs into the four temp clones, each stub carrying its own
#        clone's repository name ---------------------------------------------
home=$(setup)
rc=0
run_installer "$home" >/dev/null 2>"$home/err.log" || rc=$?
assert_rc 0 "$rc" "a fresh install exits 0"
for repo in $CLONES; do
    hook=$(hook_of "$home" "$repo")
    assert_eq "yes" "$([ -x "$hook" ] && echo yes || echo no)" \
        "$repo: pre-push hook is installed and executable"
    assert_eq "755" "$(_mode "$hook")" "$repo: hook mode is 0755"
    assert_eq "yes" "$(sed -n 2p "$hook" | grep -qF "$MARKER" && echo yes || echo no)" \
        "$repo: marker is the hook's second line"
    assert_contains "$(cat "$hook")" "exec \"\$PY\" \"\$GATE\" \"$repo\" \"\$@\"" \
        "$repo: stub execs the gate naming its own clone's repository"
done
rm -rf "$home"

# --- 2. a clone that does not exist on this machine is skipped, not an
#        error ------------------------------------------------------------
home=$(setup "agent-platform library convivy-orient")   # convivy-deck absent
rc=0
out=$(run_installer "$home" 2>&1) || rc=$?
assert_rc 0 "$rc" "a missing clone does not fail the install"
assert_eq "no" "$(printf '%s' "$out" | grep -qi "convivy-deck" && echo yes || echo no)" \
    "nothing is said about the missing clone"
assert_eq "no" "$([ -e "$home/Convivy/convivy-deck" ] && echo yes || echo no)" \
    "the missing clone is not created"
for repo in agent-platform library convivy-orient; do
    assert_eq "yes" "$([ -x "$(hook_of "$home" "$repo")" ] && echo yes || echo no)" \
        "$repo: still installed despite a sibling clone being missing"
done
rm -rf "$home"

# --- 3. running it twice leaves every stub byte-identical (idempotent), and
#        a second run that changed nothing writes and prints nothing --------
home=$(setup)
run_installer "$home" >/dev/null 2>&1
snapshot=$(mktemp -d)
for repo in $CLONES; do
    hook=$(hook_of "$home" "$repo")
    cp "$hook" "$snapshot/$repo"
    # mtime, not just content: a byte-for-byte rewrite would also leave the
    # content identical, so mtime is what tells "wrote nothing" from "wrote
    # the same bytes again". Both macOS (BSD stat) and Linux (GNU stat) are
    # covered; one of the two always works.
    (stat -f '%m' "$hook" 2>/dev/null || stat -c '%Y' "$hook" 2>/dev/null) > "$snapshot/$repo.mtime"
done
# A filesystem with second-granularity mtimes needs the two runs to land in
# different seconds, or an unchanged mtime would also be what a real rewrite
# looks like a second later. Belt-and-suspenders with the "no ✓ printed"
# assertion below, which does not depend on timing at all.
sleep 1
rc=0
out=$(run_installer "$home" 2>&1) || rc=$?
assert_rc 0 "$rc" "a second install exits 0"
assert_eq "no" "$(printf '%s' "$out" | grep -q '✓' && echo yes || echo no)" \
    "a second install with nothing changed prints no ✓ line"
for repo in $CLONES; do
    hook=$(hook_of "$home" "$repo")
    assert_eq "same" "$(cmp -s "$snapshot/$repo" "$hook" && echo same || echo different)" \
        "$repo: stub is byte-identical after a second install"
    assert_eq "755" "$(_mode "$hook")" "$repo: mode stays 0755 after a second install"
    assert_eq "$(cat "$snapshot/$repo.mtime")" \
        "$(stat -f '%m' "$hook" 2>/dev/null || stat -c '%Y' "$hook" 2>/dev/null)" \
        "$repo: mtime is unchanged after a second install (nothing was written)"
done
rm -rf "$home" "$snapshot"

# --- 3b. a stub with a changed byte is rewritten, and its ✓ line printed ----
home=$(setup "agent-platform")
run_installer "$home" >/dev/null 2>&1
hook=$(hook_of "$home" "agent-platform")
printf '\n# a byte that was not there before\n' >> "$hook"
rc=0
out=$(run_installer "$home" 2>&1) || rc=$?
assert_rc 0 "$rc" "a changed stub still exits 0"
assert_contains "$out" "✓ $hook" "agent-platform: the ✓ line is printed for a rewritten stub"
assert_eq "no" "$(grep -q "a byte that was not there before" "$hook" && echo yes || echo no)" \
    "agent-platform: the changed byte is gone; the stub was rewritten"
assert_eq "755" "$(_mode "$hook")" "agent-platform: the rewritten stub is mode 0755"
rm -rf "$home"

# --- 3c. a stub with mode 0644 is set to 0755, and its ✓ line printed -------
home=$(setup "agent-platform")
run_installer "$home" >/dev/null 2>&1
hook=$(hook_of "$home" "agent-platform")
before=$(cat "$hook")
chmod 0644 "$hook"
rc=0
out=$(run_installer "$home" 2>&1) || rc=$?
assert_rc 0 "$rc" "a mode-0644 stub still exits 0"
assert_contains "$out" "✓ $hook" "agent-platform: the ✓ line is printed when only the mode was wrong"
assert_eq "755" "$(_mode "$hook")" "agent-platform: the mode is fixed to 0755"
assert_eq "$before" "$(cat "$hook")" "agent-platform: the content is untouched when only the mode was wrong"
rm -rf "$home"

# --- 4. a foreign pre-push hook is left byte-identical; the other clones
#        still get theirs; the installer exits non-zero -----------------------
home=$(setup)
foreign=$(hook_of "$home" "library")
printf '#!/bin/sh\necho foreign\n' > "$foreign"
chmod +x "$foreign"
foreign_before=$(cat "$foreign")
rc=0
out=$(run_installer "$home" 2>&1) || rc=$?
assert_eq "yes" "$([ "$rc" -ne 0 ] && echo yes || echo no)" \
    "a foreign pre-push hook makes the installer exit non-zero"
assert_eq "$foreign_before" "$(cat "$foreign")" \
    "the foreign hook is left byte-identical"
assert_contains "$out" "library" "the installer names the clone with the foreign hook"
for repo in agent-platform convivy-orient convivy-deck; do
    assert_eq "yes" "$(sed -n 2p "$(hook_of "$home" "$repo")" | grep -qF "$MARKER" && echo yes || echo no)" \
        "$repo: still installed despite library's foreign hook"
done
rm -rf "$home"

# --- 5. a stub whose gate file is missing refuses a push to a temp file://
#        bare remote, and the remote gets no ref -----------------------------
home=$(setup "agent-platform")
run_installer "$home" >/dev/null 2>&1
# The interpreter exists and is executable; the gate file (deploy/bin/push-
# content-gate) is never written, so the stub's second check fires.
write_stand_in_py "$home/.local/deploy/agent-platform-shared"

repo="$home/Convivy/agent-platform"
echo hello > "$repo/f.txt"
git -C "$repo" add -A
git -C "$repo" commit -qm seed
bare="$home/bare.git"
git init -q --bare -b main "$bare"
git -C "$repo" remote add origin "file://$bare"

rc=0
out=$(HOME="$home" git -C "$repo" push origin main 2>&1) || rc=$?
assert_eq "yes" "$([ "$rc" -ne 0 ] && echo yes || echo no)" \
    "a push is refused when the gate file is missing"
assert_contains "$out" "nothing was sent" "the refusal says nothing was sent"
assert_eq "no" "$(git -C "$bare" rev-parse --verify refs/heads/main >/dev/null 2>&1 && echo yes || echo no)" \
    "the bare remote gets no ref"
rm -rf "$home"

# --- 6. a stand-in gate exiting 0 lets a push through, and is run with the
#        pre-push argv and stdin git itself hands the hook -------------------
home=$(setup "agent-platform")
deploy="$home/deploy"
write_stand_in_py "$deploy"
mkdir -p "$deploy/bin"
argv_log="$home/argv.log"
stdin_log="$home/stdin.log"
cat > "$deploy/bin/push-content-gate" <<PYEOF
#!/usr/bin/env python3
import sys
open("$argv_log", "w").write(repr(sys.argv))
open("$stdin_log", "w").write(sys.stdin.read())
sys.exit(0)
PYEOF
chmod +x "$deploy/bin/push-content-gate"

run_installer "$home" --deploy-dir "$deploy" >/dev/null 2>&1

repo="$home/Convivy/agent-platform"
echo hello > "$repo/f.txt"
git -C "$repo" add -A
git -C "$repo" commit -qm seed
bare="$home/bare.git"
git init -q --bare -b main "$bare"
git -C "$repo" remote add origin "file://$bare"

# Push from a git worktree of the clone: the common hooks directory the
# installer wrote into gates every worktree too, same as the primary clone.
wt="$home/agent-platform-wt"
git -C "$repo" worktree add -q "$wt" -b feature main
echo more >> "$wt/f.txt"
git -C "$wt" add -A
_git_ident "$wt"
git -C "$wt" commit -qm "from a worktree"

rc=0
HOME="$home" git -C "$wt" push origin feature >/dev/null 2>&1 || rc=$?
assert_rc 0 "$rc" "a push gated by a stand-in exiting 0 succeeds"
assert_contains "$(cat "$argv_log")" "'agent-platform'" \
    "the stand-in sees the clone's own repository name"
assert_contains "$(cat "$argv_log")" "'origin'" \
    "the stand-in sees git's own remote name"
assert_contains "$(cat "$argv_log")" "file://$bare" \
    "the stand-in sees git's own remote url"
assert_contains "$(cat "$stdin_log")" "refs/heads/feature" \
    "the stand-in's stdin carries the pre-push ref line"
assert_eq "yes" "$(git -C "$bare" rev-parse --verify refs/heads/feature >/dev/null 2>&1 && echo yes || echo no)" \
    "the bare remote gets the ref"
rm -rf "$home"

# --- 7. the same stand-in exiting 1 refuses the push -------------------------
home=$(setup "agent-platform")
deploy="$home/deploy"
write_stand_in_py "$deploy"
mkdir -p "$deploy/bin"
cat > "$deploy/bin/push-content-gate" <<'PYEOF'
#!/usr/bin/env python3
import sys
sys.stdin.read()
sys.exit(1)
PYEOF
chmod +x "$deploy/bin/push-content-gate"

run_installer "$home" --deploy-dir "$deploy" >/dev/null 2>&1

repo="$home/Convivy/agent-platform"
echo hello > "$repo/f.txt"
git -C "$repo" add -A
git -C "$repo" commit -qm seed
bare="$home/bare.git"
git init -q --bare -b main "$bare"
git -C "$repo" remote add origin "file://$bare"

rc=0
HOME="$home" git -C "$repo" push origin main >/dev/null 2>&1 || rc=$?
assert_eq "yes" "$([ "$rc" -ne 0 ] && echo yes || echo no)" \
    "a stand-in exiting 1 refuses the push"
assert_eq "no" "$(git -C "$bare" rev-parse --verify refs/heads/main >/dev/null 2>&1 && echo yes || echo no)" \
    "the bare remote gets no ref"
rm -rf "$home"

finish
