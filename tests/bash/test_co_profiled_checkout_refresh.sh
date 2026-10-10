#!/bin/bash
# `co -r` re-individuates the profiled checkouts that never rendered (floor slice 5).
#
# `knowledge-ctx list` resolves a `daytime` profile in eight checkouts that no `co`
# branch ever individuated: constance-landing, constance-updates, convivy-app-kit,
# convivy-services, hollis, selector-backend, write-better and www. Sessions there
# got none of the rendered instruction blocks, agents or skills.
#
# FOUR OF THE EIGHT ARE NOT RE-INDIVIDUATED ON PURPOSE. `knowledge-ctx use --force`
# rmtrees `.claude/agents` and `.claude/skills`, and convivy-app-kit,
# convivy-services, hollis and write-better hold hand-written agents there. They
# are asserted NOT re-individuated below, so adding one to bin/co's
# `_co_profiled_dirs` is a visible decision that has to touch this file.
#
# THE RENDER IS SKIPPED WHERE GIT DOES NOT IGNORE `.claude/`. knowledge-ctx writes no
# ignore entry, and constance-landing and constance-updates publish sites, so a render
# there leaves agent prompts untracked. Those two fixtures carry no .gitignore, the
# other two ignore `.claude/`, mirroring the real checkouts on 2026-09-24.
#
# ONLY A BARE `co` OR A `/`-PROMPT ENTERS THE BRANCH: `co coder` in selector-backend
# must still reach `--agent coder`, as it did before the branch existed.
#
# The gate is the FULL resolved path. A same-basename directory elsewhere is the
# control: with a basename match `--force` would clear its agents and skills.
#
# knowledge-ctx, claude and tmux are stubs; nothing here reaches a real checkout, a
# real tmux server or the real knowledge index. bin/co calls tmux only under $TMUX
# (the label and session-name code), which a test run from a tmux pane inherits, so
# the runner unsets TMUX and TMUX_PANE and the tmux stub logs any call it still gets.

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

IGNORING="selector-backend www"
UNIGNORED="constance-landing constance-updates"
RENDERED="$UNIGNORED $IGNORING"

setup() { # -> echoes fake HOME
    local home d
    home=$(mktemp -d)
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

    for d in $RENDERED convivy-app-kit convivy-services hollis write-better; do
        mkdir -p "$home/Convivy/$d"
        ( cd "$home/Convivy/$d" && git init -q . )
    done
    # Same basenames, wrong place.
    for d in www hollis; do
        mkdir -p "$home/elsewhere/$d"
        ( cd "$home/elsewhere/$d" && git init -q . )
    done

    for d in $IGNORING; do
        echo ".claude/" > "$home/Convivy/$d/.gitignore"
    done
    mkdir -p "$home/Convivy/selector-backend/.claude/agents"
    : > "$home/Convivy/selector-backend/.claude/agents/coder.md"
    cat > "$home/stubbin/tmux" <<'STUB'
#!/bin/bash
printf '%s\n' "$*" >> "$TMUX_CALL_LOG"
exit 1
STUB
    cat > "$home/stubbin/claude" <<'STUB'
#!/bin/bash
printf '%s\n' "$@" > "$CLAUDE_CALL_LOG"
exit 0
STUB
    # `use` is recorded; every call succeeds, so `list` resolves everywhere.
    cat > "$home/stubbin/knowledge-ctx" <<'STUB'
#!/bin/bash
printf '%s\n' "$*" >> "$KX_CALL_LOG"
exit 0
STUB
    chmod +x "$home/stubbin/tmux" "$home/stubbin/claude" "$home/stubbin/knowledge-ctx"
    echo "$home"
}

# run_co_in <home> <dir-relative-to-home> [co args...]
#   -> RC, CLAUDE_ARGS, KX_ARGS, ERR, TMUX_CALLS
run_co_in() {
    local home=$1 rel=$2 call_log kx_log err_log tmux_log
    shift 2
    call_log=$(mktemp); kx_log=$(mktemp); err_log=$(mktemp); tmux_log=$(mktemp)
    ( cd "$home/$rel" && env -u TMUX -u TMUX_PANE HOME="$home" PATH="$home/stubbin:$PATH" \
      CLAUDE_CALL_LOG="$call_log" KX_CALL_LOG="$kx_log" TMUX_CALL_LOG="$tmux_log" \
      /bin/zsh "$CO" --root-config "$(co_root_config "$home")" "$@" >/dev/null 2>"$err_log" )
    RC=$?
    CLAUDE_ARGS=$(cat "$call_log"); KX_ARGS=$(cat "$kx_log"); ERR=$(cat "$err_log")
    TMUX_CALLS=$(cat "$tmux_log")
    rm -f "$call_log" "$kx_log" "$err_log" "$tmux_log"
    assert_eq "" "$TMUX_CALLS" "no tmux call: co ${*:-(no args)} in $rel"
}
run_co_r_in() { run_co_in "$1" "$2" -r; }

H=$(setup)

echo "== each profiled checkout that ignores .claude/ re-individuates =="
for d in $IGNORING; do
    run_co_r_in "$H" "Convivy/$d"
    assert_rc 0 "$RC" "$d: co -r exits 0"
    assert_contains "$KX_ARGS" "use daytime --force" \
        "$d: co -r runs knowledge-ctx use daytime --force"
    assert_contains "$CLAUDE_ARGS" "--model" "$d: co -r reaches the launch"
    assert_contains "$CLAUDE_ARGS" "/handoff-pickup" "$d: a rendered co -r launches with /handoff-pickup"
done

echo "== a checkout whose git does not ignore .claude/ is not re-individuated =="
for d in $UNIGNORED; do
    run_co_r_in "$H" "Convivy/$d"
    assert_rc 0 "$RC" "$d: co -r still launches"
    assert_not_contains "$KX_ARGS" "use" "$d: co -r does not run knowledge-ctx use"
    assert_contains "$ERR" "git does not ignore .claude/" "$d: co -r says why it skipped"
    assert_contains "$ERR" ".gitignore" "$d: co -r says what the owner must add"
    assert_contains "$CLAUDE_ARGS" "--model" "$d: co -r still reaches the launch"
    assert_not_contains "$CLAUDE_ARGS" "/handoff-pickup" \
        "$d: a skipped render launches with no prompt (the skill was never individuated)"
done

echo "== a global ignore of .claude/ does not count; only the repo's own rules do =="
# The fixtures' HOME is fake, so this global excludes file is read only through the
# explicit override below; a run that honoured it would render constance-landing.
mkdir -p "$H/.config/git"
echo ".claude/" > "$H/.config/git/ignore"
run_co_r_in "$H" "Convivy/constance-landing"
assert_not_contains "$KX_ARGS" "use" "global ignore: constance-landing is not re-individuated"
assert_contains "$ERR" "git does not ignore .claude/" "global ignore: co -r says why it skipped"
# A repo rule that covers only part of what the render writes does not pass either.
echo ".claude/agents/" > "$H/Convivy/constance-updates/.gitignore"
run_co_r_in "$H" "Convivy/constance-updates"
assert_not_contains "$KX_ARGS" "use" "partial ignore: constance-updates is not re-individuated"
rm -f "$H/Convivy/constance-updates/.gitignore" "$H/.config/git/ignore"

echo "== a named agent still reaches --agent, not a prompt =="
run_co_in "$H" "Convivy/selector-backend" coder
assert_rc 0 "$RC" "selector-backend: co coder exits 0"
assert_contains "$CLAUDE_ARGS" "--agent" "selector-backend: co coder passes --agent"
assert_contains "$CLAUDE_ARGS" "coder" "selector-backend: co coder names the agent"

echo "== the four with hand-written agents are left alone =="
for d in convivy-app-kit convivy-services hollis write-better; do
    run_co_r_in "$H" "Convivy/$d"
    assert_not_contains "$KX_ARGS" "use" \
        "$d: co -r does not run knowledge-ctx use (would clear its agents)"
done

echo "== a same-basename directory elsewhere is not re-individuated =="
for d in www hollis; do
    run_co_r_in "$H" "elsewhere/$d"
    assert_not_contains "$KX_ARGS" "use" \
        "elsewhere/$d: co -r does not run knowledge-ctx use"
done

rm -rf "$H"
finish
