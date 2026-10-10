#!/bin/bash
# Tests for bin/co's `_co_refresh_floor`, called from `_co_pull_knowledge`,
# exercised through the crew-cockpit `-r` path like test_co_knowledge_pull.sh.
#
# THE RULE. `co` refreshes ~/.claude/CLAUDE.md ONLY when that file is already a
# floor render: `.floor-manifest.json` exists beside it and names that output
# path. Before the steward's manual cutover the file is the hand-written one,
# holding rules no fleet block carries, and rendering over it would drop them
# from every new session. Otherwise `co` does nothing to it and prints one line
# saying the floor is not cut over.
#
# `knowledge-ctx` is a stub on PATH that logs its arguments; nothing here reaches
# the real one, and HOME is a temp dir throughout, so the real ~/.claude/CLAUDE.md
# is never read or written. The last case asserts that.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

CO="$REPO_ROOT/bin/co"
REAL_GIT=$(command -v git)
REAL_CLAUDE_MD="$(cd ~ && pwd)/.claude/CLAUDE.md"
_real_state() { # inode + content hash of the real file, or "absent"
    if [ -e "$REAL_CLAUDE_MD" ]; then
        echo "$(ls -iL "$REAL_CLAUDE_MD" | awk '{print $1}') $(shasum "$REAL_CLAUDE_MD" | awk '{print $1}')"
    else
        echo absent
    fi
}
REAL_BEFORE=$(_real_state)

# Fake HOME: a knowledge checkout tracking a bare origin (so `git checkout main`
# and the kb sync stub work), a `library` crew cockpit, stubs for claude,
# knowledge-ctx (logging) and agentctl. Echoes the fake HOME.
setup() {
    local home origin kb lib seed
    home=$(mktemp -d)
    origin="$home/origin.git"
    kb="$home/Convivy/knowledge"
    lib="$home/Convivy/library"
    git init -q --bare "$origin"
    git -C "$origin" symbolic-ref HEAD refs/heads/main
    seed=$(mktemp -d)
    git init -q "$seed/w"
    git -C "$seed/w" symbolic-ref HEAD refs/heads/main
    git -C "$seed/w" config user.email t@t
    git -C "$seed/w" config user.name t
    git -C "$seed/w" config commit.gpgsign false
    echo v1 > "$seed/w/seed.md"
    git -C "$seed/w" add -A
    git -C "$seed/w" commit -qm seed
    git -C "$seed/w" remote add origin "$origin"
    git -C "$seed/w" push -q -u origin main || {
        echo "  FIXTURE BROKEN: seed push failed" >&2; exit 1; }
    rm -rf "$seed"
    mkdir -p "$(dirname "$kb")"
    git clone -q "$origin" "$kb"
    mkdir -p "$lib/.claude/agents"
    : > "$lib/.claude/agents/scout.md"
    ( cd "$lib" && git init -q . )
    mkdir -p "$home/stubbin" "$home/.claude"
    printf '#!/bin/bash\nexit 0\n' > "$home/stubbin/claude"
    cat > "$home/stubbin/knowledge-ctx" <<'STUB'
#!/bin/bash
echo "$*" >> "$KCTX_LOG"
if [ "$1" = floor ]; then exit "${KCTX_FLOOR_RC:-0}"; fi
exit 0
STUB
    chmod +x "$home/stubbin/claude" "$home/stubbin/knowledge-ctx"
    write_kb_sync_stub "$home/stubbin"
    echo "$home"
}

# run_co_r <home> -> RC, STDERR, FLOOR_CALLS (the `floor` invocations only)
run_co_r() {
    local home=$1 err kctx
    err=$(mktemp); kctx=$(mktemp)
    ( cd "$home/Convivy/library" && \
      HOME="$home" PATH="$home/stubbin:$PATH" KCTX_LOG="$kctx" \
      AGENTCTL_STUB_GIT="$REAL_GIT" \
      /bin/zsh "$CO" --root-config "$(co_root_config "$home")" -r >/dev/null 2>"$err" )
    RC=$?
    STDERR=$(cat "$err")
    FLOOR_CALLS=$(grep '^floor' "$kctx" || true)
    rm -f "$err" "$kctx"
}

echo "== no floor manifest: the hand-written file is left alone, one line says so =="
H=$(setup)
printf 'hand-written rules\n' > "$H/.claude/CLAUDE.md"
run_co_r "$H"
assert_rc 0 "$RC" "not cut over: co -r succeeds"
assert_eq "" "$FLOOR_CALLS" "not cut over: knowledge-ctx floor is never called"
assert_eq "hand-written rules" "$(cat "$H/.claude/CLAUDE.md")" "not cut over: the file is untouched"
assert_eq "1" "$(echo "$STDERR" | grep -c 'floor not cut over')" "not cut over: exactly one line says so"
rm -rf "$H"

echo "== manifest names this file: co refreshes it =="
H=$(setup)
printf 'a floor render\n' > "$H/.claude/CLAUDE.md"
printf '{\n  "output": "%s/.claude/CLAUDE.md",\n  "blocks": []\n}\n' "$H" > "$H/.claude/.floor-manifest.json"
run_co_r "$H"
assert_rc 0 "$RC" "cut over: co -r succeeds"
assert_eq "floor --output $H/.claude/CLAUDE.md" "$FLOOR_CALLS" "cut over: floor is called once, with the output path"
assert_eq "0" "$(echo "$STDERR" | grep -c 'floor not cut over')" "cut over: no not-cut-over line"
rm -rf "$H"

echo "== manifest names a different file: not this floor, so nothing runs =="
H=$(setup)
printf 'hand-written rules\n' > "$H/.claude/CLAUDE.md"
printf '{\n  "output": "/somewhere/else/CLAUDE.md",\n  "blocks": []\n}\n' > "$H/.claude/.floor-manifest.json"
run_co_r "$H"
assert_eq "" "$FLOOR_CALLS" "foreign manifest: floor is never called"
assert_contains "$STDERR" "floor not cut over" "foreign manifest: co says so"
rm -rf "$H"

echo "== a failing floor render warns and the session still starts =="
H=$(setup)
printf '{\n  "output": "%s/.claude/CLAUDE.md"\n}\n' "$H" > "$H/.claude/.floor-manifest.json"
KCTX_FLOOR_RC=1 run_co_r "$H"
assert_rc 0 "$RC" "failed floor: co -r still exits 0"
assert_contains "$STDERR" "knowledge-ctx floor failed" "failed floor: the warning is printed"
rm -rf "$H"

echo "== the real ~/.claude/CLAUDE.md was not touched by any case above =="
assert_eq "$REAL_BEFORE" "$(_real_state)" "real file: same inode and content as before"

finish
