#!/bin/bash
# Tests for bin/co's nyh-architecture arm, the `-r` product-repo rejection
# path, and the raw-$(pwd)/$PWD stderr prints round 2 of
# issue_01M3Q2DJMB5R37HZD9SMVTS323 found still open after round 1.
#
# THE BUG. `_apply_co_style` writes the name it is given into OSC 1337
# (`profile_name="co-$style_id"`), OSC 0 (the non-tmux title), tmux
# status-left and tmux rename-window. #709 gated the generic `*)` arm's call
# AT THAT CALL SITE, on the same rule `projects._NAME_OK` enforces
# (lib/agent-platform/projects.py:408). The nyh-architecture arm
# (<crews-root>/nyh-architecture/<repo>) sets `nyh_proj=$(basename
# "$repo_root")` and, for any repo that is not nyh-park-villa or nyh-etc,
# calls `_apply_co_style "$nyh_proj" ... "$nyh_proj" ...` with no check of
# its own — a basename carrying a raw ESC or BEL byte (the filesystem
# permits any byte but `/` and NUL; `git worktree add` or an agent's own
# `os.mkdir` can produce one) reached every one of those sinks unescaped.
#
# ROUND 1 of PR #741 moved the gate into `_apply_co_style` itself and
# escaped the `co -r` rejection path's three stderr prints, but left several
# more raw `$(pwd)`/`$PWD`/`$(basename ...)` prints open — found by round-1
# review (security-reviewer run-4031c569, reviewer run-15aacbb6): the generic
# daytime fallback's "not individuated" message (the single least-restricted
# path in this file — reached by plain `co` in any repo with no agent
# individuated yet) and the `*-commercial` glob-suffix arm's matching
# message, both printing `$(pwd)` raw. ROUND 2 escaped every such print in
# the file with the same `${(V)}` treatment, regardless of whether the
# branch it is in also gates on a closed, hardcoded set.
#
# `co` is zsh (excluded from the ARCH-082 bash-3.2 contract by lint.sh's
# shebang filter); bash invoking it here is exactly how a login shell will.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

CO="$REPO_ROOT/bin/co"

# A raw BEL (0x07) inside a directory basename — ANSI-C quoting so the byte
# is literal, not the two-character notation `\a`. Same fixture shape as
# test_co_resolve_adopter_crew.sh's EVIL_NAME.
EVIL_NAME=$'evil\ainjected'
# A well-formed nyh-architecture name that is neither of the two named
# projects, so it exercises the `*)` sub-arm (nyh_label=nyh_proj) rather than
# a hardcoded label.
GOOD_NAME="nyh-newproject"

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

# A fake HOME with <root>/nyh-architecture/<repo> for both names, plus a
# bare, unregistered repo directly under <root> (for the `-r` rejection
# path, which is keyed on the repo's own basename, not on nyh-architecture).
setup() { # -> echoes fake HOME
    local home
    # `pwd -P` (fully resolved, no symlink segments), not the raw `mktemp -d`
    # output: on macOS `/var/folders/...` is itself a symlink to
    # `/private/var/folders/...`, and the nyh-architecture branch compares
    # `$(dirname "$repo_root")` (`git rev-parse --show-toplevel`'s realpath)
    # to `$_co_crews_root/nyh-architecture` by exact string equality, with
    # `$_co_crews_root` returned UNRESOLVED (builder_root.root()'s
    # documented behaviour, so an error message shows what was actually
    # configured). A HOME under the unresolved spelling makes that
    # comparison fail in this harness for a reason that has nothing to do
    # with the fix under test.
    home=$(cd "$(mktemp -d)" && pwd -P)
    mkdir -p "$home/stubbin"
    write_kb_sync_stub "$home/stubbin"

    mkdir -p "$home/Convivy/nyh-architecture/$EVIL_NAME"
    ( cd "$home/Convivy/nyh-architecture/$EVIL_NAME" && git init -q . )

    mkdir -p "$home/Convivy/nyh-architecture/$GOOD_NAME"
    ( cd "$home/Convivy/nyh-architecture/$GOOD_NAME" && git init -q . )

    # The `-r` rejection fixture: a plain repo with no registry row and no
    # projects/<name>/ in the knowledge tree, so `_co_is_an_adopter_crew`
    # refuses it and the stderr message fires. Bare git tree; `co -r` pulls
    # knowledge via the stubbed `agentctl kb sync` before this gate runs, so
    # a real, if empty, knowledge checkout is needed the way
    # test_co_resolve_adopter_crew.sh's "nope" fixture needs one.
    git init -q --bare "$home/kb-remote.git"
    git init -q "$home/Convivy/knowledge"
    (
        cd "$home/Convivy/knowledge" || exit 1
        git config user.email t@example.com
        git config user.name Test
        git commit -q --allow-empty -m seed
        git branch -M main
        git remote add origin "$home/kb-remote.git"
        git push -q -u origin main
    ) >/dev/null 2>&1
    mkdir -p "$home/Convivy/$EVIL_NAME"
    ( cd "$home/Convivy/$EVIL_NAME" && git init -q . )

    # The `*-commercial` glob-suffix fixture: any basename ending in
    # `-commercial` reaches bin/co's commercial arm, not a closed set like
    # the five sites PR #741's round-1 body enumerated.
    mkdir -p "$home/Convivy/$EVIL_NAME-commercial"
    ( cd "$home/Convivy/$EVIL_NAME-commercial" && git init -q . )

    cat > "$home/stubbin/claude" <<'EOF'
#!/bin/bash
exit 0
EOF
    chmod +x "$home/stubbin/claude"
    cat > "$home/stubbin/knowledge-ctx" <<'EOF'
#!/bin/bash
exit 0
EOF
    chmod +x "$home/stubbin/knowledge-ctx"

    echo "$home"
}

# run_co <home> <dir-relative-to-home> [co-args...] -> RC, ERR, OUT
run_co() {
    local home=$1 rel=$2; shift 2
    local err_log out_log
    err_log=$(mktemp); out_log=$(mktemp)
    # TMUX/TMUX_PANE FORCED EMPTY so `_apply_co_style` takes its non-tmux
    # branch deterministically and this test never touches a real tmux
    # server (never-xtrace-co / a-dispatched-run-inherits-my-panes-tmux).
    ( cd "$home/$rel" && HOME="$home" PATH="$home/stubbin:$PATH" \
      TMUX="" TMUX_PANE="" \
      /bin/zsh "$CO" --root-config "$(co_root_config "$home")" "$@" \
      >"$out_log" 2>"$err_log" )
    RC=$?
    ERR=$(cat "$err_log"); OUT=$(cat "$out_log")
    rm -f "$err_log" "$out_log"
}

# run_co_tmux <home> <dir-relative-to-home> -> RC, TMUX_CALLS
run_co_tmux() {
    local home=$1 rel=$2
    local log; log=$(mktemp)
    cat > "$home/stubbin/tmux" <<'STUB'
#!/bin/bash
printf '%s\n' "$*" >> "$TMUX_SPY_LOG"
exit 0
STUB
    chmod +x "$home/stubbin/tmux"
    ( cd "$home/$rel" && HOME="$home" PATH="$home/stubbin:$PATH" \
      TMUX_SPY_LOG="$log" TMUX=/tmp/tmux-spy/default,1,0 TMUX_PANE=%7 \
      /bin/zsh "$CO" --root-config "$(co_root_config "$home")" \
      >/dev/null 2>&1 )
    RC=$?
    TMUX_CALLS=$(cat "$log")
    rm -f "$log" "$home/stubbin/tmux"
}

H=$(setup)

echo "== nyh-architecture: an ESC/BEL basename launches unstyled (non-tmux) =="
run_co "$H" "Convivy/nyh-architecture/$EVIL_NAME"
assert_rc 0 "$RC" "evil nyh name: co still launches"
assert_not_contains "$OUT" "SetProfile=" \
    "evil nyh name: the OSC 1337 sink is never reached"
assert_not_contains "$OUT" "evil" \
    "evil nyh name: no trace of the raw basename reaches stdout"

echo "== nyh-architecture: an ESC/BEL basename writes no tmux escape either =="
run_co_tmux "$H" "Convivy/nyh-architecture/$EVIL_NAME"
assert_rc 0 "$RC" "evil nyh name (tmux): co still launches"
assert_eq "" "$TMUX_CALLS" "evil nyh name: _apply_co_style issues no tmux call at all"

echo "== nyh-architecture: a well-formed custom name still styles (non-tmux) =="
run_co "$H" "Convivy/nyh-architecture/$GOOD_NAME"
assert_rc 0 "$RC" "good nyh name: co launches"
assert_contains "$OUT" "SetProfile=co-$GOOD_NAME" \
    "good nyh name: the OSC 1337 sink fires with the real name"

echo "== nyh-architecture: a well-formed custom name still styles tmux =="
run_co_tmux "$H" "Convivy/nyh-architecture/$GOOD_NAME"
assert_rc 0 "$RC" "good nyh name (tmux): co launches"
assert_contains "$TMUX_CALLS" "rename-window -t %7 $GOOD_NAME" \
    "good nyh name: the window is renamed for it"
assert_contains "$TMUX_CALLS" "set-option -t %7 status-left  $GOOD_NAME  " \
    "good nyh name: status-left carries the label"

echo "== nyh-architecture: Park Villa (a hardcoded label with a space) is unaffected =="
mkdir -p "$H/Convivy/nyh-architecture/nyh-park-villa"
( cd "$H/Convivy/nyh-architecture/nyh-park-villa" && git init -q . ) >/dev/null 2>&1
run_co_tmux "$H" "Convivy/nyh-architecture/nyh-park-villa"
assert_rc 0 "$RC" "Park Villa: co launches"
assert_contains "$TMUX_CALLS" "rename-window -t %7 Park Villa" \
    "Park Villa: a label with a space still styles, the sink's broader charset allows it"

echo "== _apply_co_style: a valid style_id with a control-byte label isolates the label-charset half of the gate =="
# EVIL_NAME above fails style_id's _NAME_OK charset AND label's broader charset at
# once, so it cannot tell whether the label-charset half of the `||` does anything —
# a suite that only exercised EVIL_NAME would not catch that half being silently
# dropped (reviewer, PR #741 round 1). The private-crew branch's style_id ("cfo",
# hardcoded) and label (the registry's "display" field, independent of style_id) are
# the real caller the round-1 review named via the ask-topic path's shape — a fixed,
# always-valid style_id paired with a separately-sourced label. python3 is stubbed
# the same way test_co_black_box.sh stands in for a private-crew registry read
# (`printf 'ask\tAsk\n'`), here answering a label containing a raw BEL.
mkdir -p "$H/Convivy/style-gate-probe"
cat > "$H/stubbin/python3" <<'STUB'
#!/bin/bash
printf 'cfo\tEvil\aLabel\n'
exit 0
STUB
chmod +x "$H/stubbin/python3"
run_co "$H" "Convivy/style-gate-probe"
assert_rc 0 "$RC" "valid style_id, evil label: co still launches"
assert_not_contains "$OUT" "SetProfile=" \
    "valid style_id, evil label: the OSC 1337 sink is never reached"
assert_not_contains "$OUT" $'\a' \
    "valid style_id, evil label: no raw BEL byte reaches stdout"
rm -f "$H/stubbin/python3"

echo "== generic daytime fallback: an ESC/BEL cwd is escaped in the 'not individuated' message =="
# The single least-restricted path in bin/co: a plain repo, no `.claude/agents/*.md`
# individuated, and knowledge-ctx answering anything other than NO_PROJECT_MAPPING
# (42) — the stubbed knowledge-ctx here always exits 0 — falls through the
# brand-new-project tail to this message, raw `$(pwd)` and all before this round.
run_co "$H" "Convivy/$EVIL_NAME"
assert_rc 1 "$RC" "generic fallback, evil cwd: co exits 1 (no agent individuated)"
assert_contains "$ERR" "is not individuated" "generic fallback, evil cwd: co says why"
assert_contains "$ERR" 'evil^Ginjected' \
    "generic fallback, evil cwd: the BEL byte is rendered as a visible ^G escape"
assert_not_contains "$ERR" $'evil\ainjected' \
    "generic fallback, evil cwd: no raw BEL byte reaches stderr"

echo "== *-commercial glob arm: an ESC/BEL cwd is escaped in the 'not individuated' message =="
# A glob SUFFIX match ($(basename "\$repo_root") == *-commercial), not a closed set:
# any attacker-named directory ending in -commercial reaches this with no -r needed.
run_co "$H" "Convivy/$EVIL_NAME-commercial"
assert_rc 1 "$RC" "*-commercial, evil cwd: co exits 1 (no agent individuated)"
assert_contains "$ERR" "not individuated" "*-commercial, evil cwd: co says why"
assert_contains "$ERR" 'evil^Ginjected-commercial' \
    "*-commercial, evil cwd: the BEL byte is rendered as a visible ^G escape"
assert_not_contains "$ERR" $'evil\ainjected-commercial' \
    "*-commercial, evil cwd: no raw BEL byte reaches stderr"

echo "== co -r: an ESC/BEL basename is escaped, not raw, in the rejection message =="
run_co "$H" "Convivy/$EVIL_NAME" -r
assert_rc 1 "$RC" "evil repo name: co -r exits 1"
assert_contains "$ERR" "not a recognized product repo" "evil repo name: co -r says why"
assert_contains "$ERR" 'evil^Ginjected' \
    "evil repo name: the BEL byte is rendered as a visible ^G escape"
assert_not_contains "$ERR" $'evil\ainjected' \
    "evil repo name: no raw BEL byte reaches stderr"

rm -rf "$H"
finish
