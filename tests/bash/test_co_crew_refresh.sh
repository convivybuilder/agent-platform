#!/bin/bash
# `co -r` reaches the crew branch for every directory in `_co_crew_dirs`.
#
# THE FAILURE THIS COVERS, which the operator hit twice on the evening of 2026-09-08:
#
#     operator@mini shipwright % co -r
#     co: cwd is not a recognized product repo (got 'shipwright').
#
# The refresh path has several branches and `shipwright` was in none of them, so
# it fell to the generic `case` at the end of the file, which is a closed list
# that rejects anything not named in it. `placard` was in the same state and had
# never been individuated at all — no `.claude/` directory existed in it, so the
# `placard/daytime` profile had gone unused since the crew was registered.
#
# tests/test_co_crew_registration.py asserts the NAMES are in the lists. This
# asserts the BEHAVIOUR those names buy: `co -r` there pulls knowledge,
# re-individuates, and launches, rather than exiting 1 on an unrecognized repo.
# The two are separable — a name can be in `_co_crew_dirs` and still not reach
# the branch, which is exactly what an entry for a directory with no `.git`
# does (the `EA` case in test_co_opus_pin.sh).
#
# `co` is zsh (excluded from the ARCH-082 bash-3.2 contract by lint.sh's shebang
# filter); bash invoking it here is exactly how a login shell will.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

CO="$REPO_ROOT/bin/co"

# A fake HOME with a knowledge repo that has a REAL, reachable origin — a second
# local repo on disk. `_co_pull_knowledge` runs `agentctl kb sync`, which the stub
# from lib.sh answers by fetching and fast-forwarding that repo with no network
# and no credentials, so the refresh path runs for real rather than being stubbed
# past the part under test.
setup() { # -> echoes fake HOME
    local home crew
    home=$(mktemp -d)
    mkdir -p "$home/stubbin"
    write_kb_sync_stub "$home/stubbin"

    # The remote, then the checkout that tracks it.
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

    for crew in shipwright placard signals library; do
        mkdir -p "$home/Convivy/$crew/.claude/agents"
        : > "$home/Convivy/$crew/.claude/agents/planning-orchestrator.md"
        ( cd "$home/Convivy/$crew" && git init -q . )
    done
    # The control: a git repo that is nobody's crew.
    mkdir -p "$home/Convivy/some-random-repo/.claude/agents"
    : > "$home/Convivy/some-random-repo/.claude/agents/planning-orchestrator.md"
    ( cd "$home/Convivy/some-random-repo" && git init -q . )

    cat > "$home/stubbin/claude" <<'EOF'
#!/bin/bash
printf '%s\n' "$@" > "$CLAUDE_CALL_LOG"
exit 0
EOF
    chmod +x "$home/stubbin/claude"
    # Records that it was asked to re-individuate, which is the half of the
    # refresh a launch alone would not prove.
    # One line per call, the argv joined by spaces, so `use` is matched as the
    # subcommand rather than as a substring anywhere in an argument.
    cat > "$home/stubbin/knowledge-ctx" <<'EOF'
#!/bin/bash
printf '%s\n' "$*" >> "$KX_CALL_LOG"
# Stands in for knowledge-ctx's live-process guard: while a parent session is
# live in the directory, `use` aborts with exit 2.
if [ "$1" = use ] && [ "${KX_REFUSE_USE:-0}" = 1 ]; then
    echo "knowledge-ctx: a live claude session is running in this directory" >&2
    exit 2
fi
exit 0
EOF
    chmod +x "$home/stubbin/knowledge-ctx"
    # Answers the one query co makes, the session name of its own pane. Every
    # other tmux call is a no-op, and the real server is never reached.
    cat > "$home/stubbin/tmux" <<'EOF'
#!/bin/bash
case "$*" in
    *'#{session_name}'*) printf '%s\n' "${TMUX_STUB_SESSION:-}" ;;
esac
exit 0
EOF
    chmod +x "$home/stubbin/tmux"

    echo "$home"
}

# run_co_r_in <home> <dir-relative-to-home> [env assignments...]
#   -> sets RC, CLAUDE_ARGS, KX_ARGS, ERR
# The env assignments reach co through `env`, after TMUX and TMUX_PANE are
# removed, so a test that wants a tmux pane sets both itself.
run_co_r_in() {
    local home=$1 rel=$2; shift 2
    local call_log kx_log err_log
    call_log=$(mktemp); kx_log=$(mktemp); err_log=$(mktemp)
    ( cd "$home/$rel" && HOME="$home" PATH="$home/stubbin:$PATH" \
      CLAUDE_CALL_LOG="$call_log" KX_CALL_LOG="$kx_log" \
      env -u TMUX -u TMUX_PANE "$@" \
      /bin/zsh "$CO" --root-config "$(co_root_config "$home")" -r >/dev/null 2>"$err_log" )
    RC=$?
    CLAUDE_ARGS=$(cat "$call_log")
    KX_ARGS=$(cat "$kx_log")
    ERR=$(cat "$err_log")
    rm -f "$call_log" "$kx_log" "$err_log"
}

H=$(setup)

echo "== the crews registered on 2026-09-08 and signals (2026-09-16) =="
for crew in shipwright placard signals; do
    run_co_r_in "$H" "Convivy/$crew"
    assert_rc 0 "$RC" "$crew: co -r exits 0 rather than rejecting the cwd"
    assert_contains "$ERR" "$crew crew" \
        "$crew: co -r announces the crew branch it took"
    assert_contains "$KX_ARGS" "use" \
        "$crew: co -r re-individuates via knowledge-ctx use"
    assert_contains "$CLAUDE_ARGS" "--model" \
        "$crew: co -r reaches the launch rather than aborting first"
done

echo "== the regression the operator reported, stated as the string they saw =="
# Asserted on the message rather than only on the exit code: `co -r` has several
# ways to exit 1 (knowledge pull failure, knowledge-ctx failure), and this test
# would pass on any of them if it only checked the code.
for crew in shipwright placard signals; do
    run_co_r_in "$H" "Convivy/$crew"
    case "$ERR" in
        *"not a recognized product repo"*)
            _FAIL=$((_FAIL + 1))
            echo "  FAIL: $crew: co -r still rejects the cwd" ;;
        *) _PASS=$((_PASS + 1)) ;;
    esac
done

echo "== an already-registered crew is unaffected =="
run_co_r_in "$H" "Convivy/library"
assert_rc 0 "$RC" "library: co -r still exits 0"
assert_contains "$ERR" "library crew" "library: co -r still takes the crew branch"

echo "== a repo that is nobody's crew is still refused =="
# The closed list stays closed. A fix that made `-r` accept anything would pass
# every assertion above and lose the check that tells someone they are in the
# wrong directory.
run_co_r_in "$H" "Convivy/some-random-repo"
assert_rc 1 "$RC" "an unregistered repo: co -r exits 1"
assert_contains "$ERR" "not a recognized product repo" \
    "an unregistered repo: co -r says why"

# Counts the `use` calls in KX_ARGS. Used as a positive control too: the plain
# crew case below must count 1, or the zero from the subcrew case proves nothing.
kx_use_calls() { printf '%s\n' "$KX_ARGS" | grep -c '^use'; }

echo "== a subcrew child under a live parent session skips knowledge-ctx use =="
# 2026-10-06: `co -r` in shipwright--site while the parent Shipwright session was
# live in the same directory. The parent had already individuated it, and the
# live-process guard refused the child's `use` (exit 2). The child launches
# without re-individuating, and the guard stays untouched.
run_co_r_in "$H" "Convivy/shipwright" \
    TMUX=/tmp/fake-tmux,1,0 TMUX_PANE=%0 \
    TMUX_STUB_SESSION=shipwright--site KX_REFUSE_USE=1
assert_rc 0 "$RC" "shipwright--site: co -r exits 0 under a live parent"
assert_contains "$ERR" "skipping knowledge-ctx use --force" \
    "shipwright--site: co -r says it skipped the re-individuate"
assert_contains "$CLAUDE_ARGS" "--model" \
    "shipwright--site: co -r reaches the launch"
assert_eq 0 "$(kx_use_calls)" "shipwright--site: knowledge-ctx use is never called"

echo "== a plain crew session still re-individuates =="
run_co_r_in "$H" "Convivy/shipwright" \
    TMUX=/tmp/fake-tmux,1,0 TMUX_PANE=%0 TMUX_STUB_SESSION=shipwright
assert_rc 0 "$RC" "shipwright (no --): co -r exits 0"
assert_eq 1 "$(kx_use_calls)" "shipwright (no --): knowledge-ctx use is called once"
assert_contains "$ERR" "knowledge-ctx use daytime --force" \
    "shipwright (no --): co -r announces the re-individuate"

rm -rf "$H"
finish
