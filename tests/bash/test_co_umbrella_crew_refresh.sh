#!/bin/bash
# `co -r` renders an OAK crew's daytime profile when, and only when, the repo
# is ready for it.
#
# THE FAILURE THIS COVERS: the business-area-umbrella branch (bin/co,
# `_co_umbrella_refresh`) launched claude for any repo directly under
# <root>/oak/ and never called
# `knowledge-ctx use`, so an OAK crew's .claude/skills was never refreshed and
# a stale /handoff-pickup broke a rollover. oak-acme and oak-initech ship
# with a .gitignore naming `.claude/` and a daytime profile in the knowledge
# repo; oak-globex tracks an example-reviewer agent and two skills under
# `.claude/` in git and does not ignore it, and `knowledge-ctx use --force`
# rmtrees `.claude/agents` and `.claude/skills` before writing, which would
# destroy that tracked work.
#
# `co` is zsh (excluded from the ARCH-082 bash-3.2 contract by lint.sh's
# shebang filter); bash invoking it here is exactly how a login shell will.
#
# The umbrella directory itself is no longer a bin/co literal: it is read
# from the registry's own `_business_umbrella_dir` key
# (`projects.business_umbrella_dirname()`). This file builds its own small
# registry at run time, naming whatever directory `setup()` actually created
# below rather than a second literal here, so this file's own name does not
# also have to appear in `tests/fixtures/rich_registry.json`.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

CO="$REPO_ROOT/bin/co"

assert_not_contains() { # <haystack> <needle> <msg>
    case "$1" in
        *"$2"*)
            _FAIL=$((_FAIL + 1))
            echo "  FAIL: $3"
            echo "        found: [$2]"
            echo "        in:    [$1]"
            ;;
        *) _PASS=$((_PASS + 1)) ;;
    esac
}

# A fake HOME with a knowledge repo that has a REAL, reachable origin (same
# shape as test_co_crew_refresh.sh), plus four <root>/oak/<project> fixtures:
#   oak-acme        ignores .claude/, a daytime profile resolves
#   oak-globex      tracks a hand-written agent under .claude/, does not ignore it
#   oak-initech     ignores .claude/, no daytime profile resolves
#   oak-hooli       tracks .claude/skills/handoff-pickup/SKILL.md, does not ignore it
setup() { # -> echoes fake HOME
    local home
    home=$(cd "$(mktemp -d)" && pwd -P)
    mkdir -p "$home/stubbin"
    write_kb_sync_stub "$home/stubbin"

    git init -q --bare "$home/kb-remote.git"
    git init -q "$home/Convivy/knowledge"
    (
        cd "$home/Convivy/knowledge" || exit 1
        git config user.email t@example.com
        git config user.name Test
        echo seed > seed.txt
        git add seed.txt
        git commit -qm seed
        git branch -M main
        git remote add origin "$home/kb-remote.git"
        git push -q -u origin main
    ) >/dev/null 2>&1

    mkdir -p "$home/Convivy/oak/oak-acme"
    echo ".claude/" > "$home/Convivy/oak/oak-acme/.gitignore"
    ( cd "$home/Convivy/oak/oak-acme" && git init -q . )

    mkdir -p "$home/Convivy/oak/oak-globex/.claude/agents"
    echo "# example-reviewer" > "$home/Convivy/oak/oak-globex/.claude/agents/example-reviewer.md"
    (
        cd "$home/Convivy/oak/oak-globex" || exit 1
        git init -q .
        git config user.email t@example.com
        git config user.name Test
        git add .claude/agents/example-reviewer.md
        git commit -qm "track example-reviewer"
    ) >/dev/null 2>&1

    mkdir -p "$home/Convivy/oak/oak-initech"
    echo ".claude/" > "$home/Convivy/oak/oak-initech/.gitignore"
    ( cd "$home/Convivy/oak/oak-initech" && git init -q . )

    mkdir -p "$home/Convivy/oak/oak-hooli/.claude/skills/handoff-pickup"
    echo "# handoff-pickup" > "$home/Convivy/oak/oak-hooli/.claude/skills/handoff-pickup/SKILL.md"
    (
        cd "$home/Convivy/oak/oak-hooli" || exit 1
        git init -q .
        git config user.email t@example.com
        git config user.name Test
        git add .claude/skills/handoff-pickup/SKILL.md
        git commit -qm "track handoff-pickup skill"
    ) >/dev/null 2>&1

    cat > "$home/stubbin/claude" <<'EOF'
#!/bin/bash
printf '%s\n' "$@" > "$CLAUDE_CALL_LOG"
exit 0
EOF
    chmod +x "$home/stubbin/claude"

    # `list` answers whether a daytime profile resolves, keyed on cwd's
    # basename via KX_LIST_DAYTIME_FOR (a space-separated allowlist) — the
    # read-only shape bin/co's `_co_umbrella_refresh` actually calls, never `use`
    # to find out. `use` is logged like every other refresh test's stub.
    cat > "$home/stubbin/knowledge-ctx" <<'EOF'
#!/bin/bash
printf '%s\n' "$*" >> "$KX_CALL_LOG"
proj=$(basename "$PWD")
if [ "$1" = list ]; then
    printf 'profiles for %s:\n' "$proj"
    case " ${KX_LIST_DAYTIME_FOR:-} " in
        *" $proj "*) printf '  daytime\n' ;;
        *) printf '  other\n' ;;
    esac
    exit 0
fi
exit 0
EOF
    chmod +x "$home/stubbin/knowledge-ctx"

    cat > "$home/stubbin/tmux" <<'EOF'
#!/bin/bash
case "$*" in
    *'#{session_name}'*) printf '%s\n' "${TMUX_STUB_SESSION:-}" ;;
esac
exit 0
EOF
    chmod +x "$home/stubbin/tmux"

    # The one directory `setup()` created above besides the knowledge repo
    # is the umbrella itself — named here by reading it back, not by a
    # second literal, so the registry this test hands `co` always matches
    # whatever this file's own fixtures are actually called.
    local umbrella_name
    umbrella_name=$(basename "$(find "$home/Convivy" -mindepth 1 -maxdepth 1 -type d ! -name knowledge | head -n 1)")
    printf '{"_business_umbrella_dir": "%s"}' "$umbrella_name" > "$home/umbrella-registry.json"

    echo "$home"
}

# run_co_r_in <home> <dir-relative-to-home> [env assignments...]
#   -> sets RC, CLAUDE_ARGS, KX_ARGS, ERR
run_co_r_in() {
    local home=$1 rel=$2; shift 2
    local call_log kx_log err_log
    call_log=$(mktemp); kx_log=$(mktemp); err_log=$(mktemp)
    ( cd "$home/$rel" && HOME="$home" PATH="$home/stubbin:$PATH" \
      CLAUDE_CALL_LOG="$call_log" KX_CALL_LOG="$kx_log" \
      env -u TMUX -u TMUX_PANE "$@" \
      /bin/zsh "$CO" --root-config "$(co_root_config "$home" "$home/umbrella-registry.json")" -r >/dev/null 2>"$err_log" )
    RC=$?
    CLAUDE_ARGS=$(cat "$call_log")
    KX_ARGS=$(cat "$kx_log")
    ERR=$(cat "$err_log")
    rm -f "$call_log" "$kx_log" "$err_log"
}

H=$(setup)

echo "== oak-acme: ignores .claude/ and a daytime profile resolves — co -r renders =="
run_co_r_in "$H" "Convivy/oak/oak-acme" KX_LIST_DAYTIME_FOR=oak-acme
assert_rc 0 "$RC" "oak-acme: co -r exits 0"
assert_contains "$KX_ARGS" "use daytime --force" \
    "oak-acme: co -r runs knowledge-ctx use daytime --force"
assert_contains "$CLAUDE_ARGS" "--model" "oak-acme: co -r reaches the launch"
assert_contains "$CLAUDE_ARGS" "/handoff-pickup" \
    "oak-acme: a rendered co -r launches with /handoff-pickup (the render just wrote the skill)"

echo "== oak-globex: tracks a file under .claude/ and does not ignore it — never rendered =="
BEFORE=$(cat "$H/Convivy/oak/oak-globex/.claude/agents/example-reviewer.md")
run_co_r_in "$H" "Convivy/oak/oak-globex" KX_LIST_DAYTIME_FOR=oak-globex
assert_rc 0 "$RC" "oak-globex: co -r still exits 0"
assert_not_contains "$KX_ARGS" "use" \
    "oak-globex: co -r never calls knowledge-ctx use (would clear its tracked agent)"
assert_contains "$ERR" "git does not ignore .claude/" \
    "oak-globex: co -r says why it skipped the render"
AFTER=$(cat "$H/Convivy/oak/oak-globex/.claude/agents/example-reviewer.md")
assert_eq "$BEFORE" "$AFTER" \
    "oak-globex: the tracked example-reviewer agent is unchanged"
assert_contains "$CLAUDE_ARGS" "--model" "oak-globex: co -r still reaches the launch"
# This fixture carries no .claude/skills/handoff-pickup/SKILL.md (unlike a
# project that ships one hand-tracked alongside its unrendered agent): a
# skipped render with no tracked skill either must launch with no prompt.
assert_not_contains "$CLAUDE_ARGS" "/handoff-pickup" \
    "oak-globex: a skipped render with no tracked skill launches with no prompt"

echo "== oak-initech: ignores .claude/ but no daytime profile resolves — not rendered =="
run_co_r_in "$H" "Convivy/oak/oak-initech"
assert_rc 0 "$RC" "oak-initech: co -r still exits 0"
assert_not_contains "$KX_ARGS" "use" \
    "oak-initech: co -r never calls knowledge-ctx use (no profile resolves)"
assert_contains "$ERR" "no daytime profile resolves" \
    "oak-initech: co -r says why it skipped the render"
assert_contains "$CLAUDE_ARGS" "--model" "oak-initech: co -r still reaches the launch"
# THE FAILURE THIS FIX COVERS: a new OAK project whose .gitignore lands
# before its daytime profile does. Before the fix, this skipped-render case
# still set /handoff-pickup unconditionally, and since nothing had ever
# rendered .claude/skills/handoff-pickup, the launch failed with "Unknown
# command" — fails against the PR's pre-fix head (506cd17c1).
assert_not_contains "$CLAUDE_ARGS" "/handoff-pickup" \
    "oak-initech: a skipped render with no profile and no tracked skill launches with no prompt"

echo "== oak-hooli: tracks .claude/skills/handoff-pickup/SKILL.md and does not ignore it — never rendered, but the skill is already there =="
BEFORE=$(cat "$H/Convivy/oak/oak-hooli/.claude/skills/handoff-pickup/SKILL.md")
run_co_r_in "$H" "Convivy/oak/oak-hooli" KX_LIST_DAYTIME_FOR=oak-hooli
assert_rc 0 "$RC" "oak-hooli: co -r still exits 0"
assert_not_contains "$KX_ARGS" "use" \
    "oak-hooli: co -r never calls knowledge-ctx use (would clear its tracked skill)"
assert_contains "$ERR" "git does not ignore .claude/" \
    "oak-hooli: co -r says why it skipped the render"
AFTER=$(cat "$H/Convivy/oak/oak-hooli/.claude/skills/handoff-pickup/SKILL.md")
assert_eq "$BEFORE" "$AFTER" \
    "oak-hooli: the tracked handoff-pickup skill is unchanged"
assert_contains "$CLAUDE_ARGS" "--model" "oak-hooli: co -r still reaches the launch"
# THE CASE ROUND 2 LEFT UNTESTED: a skipped render for a project that
# tracks the skill in git and does not ignore .claude/ must still launch
# with /handoff-pickup, via the fallback
# `-f "$repo_root/.claude/skills/handoff-pickup/SKILL.md"` disjunct
# (bin/co's handoff-pickup gate) rather than the `_co_umbrella_rendered` one.
assert_contains "$CLAUDE_ARGS" "/handoff-pickup" \
    "oak-hooli: a skipped render with the skill already tracked in git still launches with /handoff-pickup"

rm -rf "$H"
finish
