#!/bin/bash
# `co -r` re-individuates the profiled checkouts that never rendered (floor slice 5).
#
# `knowledge-ctx list` resolves a `daytime` profile in eight checkouts that no `co`
# branch ever individuated: harbor-landing, harbor-updates, a two-repo crew's
# own kit and services checkouts, atlas, beacon-backend, the writing-guide crew
# (`styleguide`, below — bin/co's own self-contained-crew flag, not this
# branch) and www. Sessions there got none of the rendered instruction blocks,
# agents or skills.
#
# FOUR OF THE EIGHT ARE NOT RE-INDIVIDUATED ON PURPOSE. `knowledge-ctx use --force`
# rmtrees `.claude/agents` and `.claude/skills`, and the two-repo crew's kit
# and services checkouts, atlas and styleguide hold hand-written agents there.
# They are asserted NOT re-individuated below, so adding one to bin/co's
# `_co_profiled_dirs` is a visible decision that has to touch this file.
#
# A SYNTHETIC REGISTRY, BUILT BELOW, NOT THE SHARED tests/fixtures/rich_registry.json
# — bin/co resolves all of this through `projects.profiled_dirs()` and the
# `PROJECTS_REGISTRY` seam (co_root_config's second argument), so this file
# declares exactly the units that property needs (made-up repo basenames
# throughout, naming no real Convivy crew) rather than depending on the large
# shared fixture's unrelated shape.
#
# THE RENDER IS SKIPPED WHERE GIT DOES NOT IGNORE `.claude/`. knowledge-ctx writes no
# ignore entry, and harbor-landing and harbor-updates publish sites, so a render
# there leaves agent prompts untracked. Those two fixtures carry no .gitignore, the
# other two ignore `.claude/`, mirroring the real checkouts on 2026-09-24.
#
# ONLY A BARE `co` OR A `/`-PROMPT ENTERS THE BRANCH: `co coder` in beacon-backend
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

IGNORING="beacon-backend www"
UNIGNORED="harbor-landing harbor-updates"
RENDERED="$UNIGNORED $IGNORING"

HAND_WRITTEN="foundry-kit foundry-services atlas styleguide"

# A synthetic registry naming only what projects.profiled_dirs() and the
# self-contained-crew flags need: RENDERED's three units declare the
# checkouts under test as `profiled_dirs`; the hand-written-agents group
# declares none (a self-contained flag of its own, same as the real registry
# uses for these shapes), so bin/co never reaches the re-individuation branch
# for them. Made-up repo basenames throughout, naming no real Convivy crew.
write_synthetic_registry() { # <path>
    cat > "$1" <<'JSON'
{
  "harbor": {
    "class": "product", "display": "Harbor", "owner": "test-owner",
    "home_dir": "<root>/harbor", "dirs": [],
    "repos": ["harbor", "harbor-landing", "harbor-updates"], "aliases": [],
    "crew": "harbor", "lifecycle": "active", "products": ["harbor"],
    "profiled_dirs": ["<root>/harbor-landing", "<root>/harbor-updates"],
    "kb": "local"
  },
  "beacon": {
    "class": "product", "display": "Beacon", "owner": "test-owner",
    "home_dir": "<root>/beacon", "dirs": [],
    "repos": ["beacon", "beacon-backend"], "aliases": [],
    "crew": "beacon", "lifecycle": "active", "products": ["beacon"],
    "profiled_dirs": ["<root>/beacon-backend"],
    "kb": "local"
  },
  "orient": {
    "class": "project", "display": "Orient", "owner": "test-owner",
    "home_dir": "<root>/orient", "dirs": [],
    "repos": ["orient", "www"], "aliases": [],
    "crew": "orient", "lifecycle": "active", "products": [],
    "profiled_dirs": ["<root>/www"],
    "kb": "local"
  },
  "harness-kit": {
    "class": "project", "display": "Harness Kit", "owner": "test-owner",
    "home_dir": "<root>/foundry-kit", "dirs": ["<root>/foundry-services"],
    "repos": ["foundry-kit", "foundry-services"], "aliases": [],
    "crew": "harness-kit", "lifecycle": "active", "products": [],
    "co_self_contained": true, "kb": "local"
  },
  "atlas": {
    "class": "product", "display": "Atlas", "owner": "test-owner",
    "home_dir": "<root>/atlas", "dirs": [], "repos": ["atlas"], "aliases": [],
    "crew": "atlas", "lifecycle": "active", "products": ["atlas"],
    "co_self_contained": true, "kb": "local"
  },
  "styleguide": {
    "class": "project", "display": "Write Better", "owner": "test-owner",
    "home_dir": "<root>/styleguide", "dirs": [], "repos": ["styleguide"],
    "aliases": [], "crew": "styleguide", "lifecycle": "active", "products": [],
    "co_writing_guide": true, "kb": "local"
  }
}
JSON
}

setup() { # -> echoes fake HOME
    local home d
    home=$(mktemp -d)
    mkdir -p "$home/stubbin"
    write_kb_sync_stub "$home/stubbin"
    write_synthetic_registry "$home/synthetic-registry.json"
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

    for d in $RENDERED $HAND_WRITTEN; do
        mkdir -p "$home/Convivy/$d"
        ( cd "$home/Convivy/$d" && git init -q . )
    done
    # Same basenames, wrong place.
    for d in www atlas; do
        mkdir -p "$home/elsewhere/$d"
        ( cd "$home/elsewhere/$d" && git init -q . )
    done

    for d in $IGNORING; do
        echo ".claude/" > "$home/Convivy/$d/.gitignore"
    done
    mkdir -p "$home/Convivy/beacon-backend/.claude/agents"
    : > "$home/Convivy/beacon-backend/.claude/agents/coder.md"
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
      /bin/zsh "$CO" --root-config "$(co_root_config "$home" "$home/synthetic-registry.json")" "$@" >/dev/null 2>"$err_log" )
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
# explicit override below; a run that honoured it would render harbor-landing.
mkdir -p "$H/.config/git"
echo ".claude/" > "$H/.config/git/ignore"
run_co_r_in "$H" "Convivy/harbor-landing"
assert_not_contains "$KX_ARGS" "use" "global ignore: harbor-landing is not re-individuated"
assert_contains "$ERR" "git does not ignore .claude/" "global ignore: co -r says why it skipped"
# A repo rule that covers only part of what the render writes does not pass either.
echo ".claude/agents/" > "$H/Convivy/harbor-updates/.gitignore"
run_co_r_in "$H" "Convivy/harbor-updates"
assert_not_contains "$KX_ARGS" "use" "partial ignore: harbor-updates is not re-individuated"
rm -f "$H/Convivy/harbor-updates/.gitignore" "$H/.config/git/ignore"

echo "== a named agent still reaches --agent, not a prompt =="
run_co_in "$H" "Convivy/beacon-backend" coder
assert_rc 0 "$RC" "beacon-backend: co coder exits 0"
assert_contains "$CLAUDE_ARGS" "--agent" "beacon-backend: co coder passes --agent"
assert_contains "$CLAUDE_ARGS" "coder" "beacon-backend: co coder names the agent"

echo "== the four with hand-written agents are left alone =="
for d in $HAND_WRITTEN; do
    run_co_r_in "$H" "Convivy/$d"
    assert_not_contains "$KX_ARGS" "use" \
        "$d: co -r does not run knowledge-ctx use (would clear its agents)"
done

echo "== a same-basename directory elsewhere is not re-individuated =="
for d in www atlas; do
    run_co_r_in "$H" "elsewhere/$d"
    assert_not_contains "$KX_ARGS" "use" \
        "elsewhere/$d: co -r does not run knowledge-ctx use"
done

rm -rf "$H"
finish
