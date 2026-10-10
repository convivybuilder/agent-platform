#!/bin/bash
# Tests for bin/co's generic tail (multiple-sessions-per-project design, Phase
# 3): a directory none of co's product-specific branches recognises, with
# nothing individuated in it either, launches plain Claude Code instead of
# failing. Deck's New Project flow hits exactly this — a brand-new directory,
# `co -r` run in it before anything has ever activated it.
#
# THE DISTINCTION THAT MATTERS MOST: a KNOWN product whose agents are merely
# un-individuated must keep failing with the loud "not individuated" error —
# that message tells a person to run `knowledge-ctx use daytime`, and silently
# swallowing it into a plain-claude launch would hide a real misconfiguration.
#
# THE SECOND DISTINCTION, added after a security review found the first
# implementation conflated it: `knowledge-ctx list` failing is NOT the same
# signal as `knowledge-ctx list` affirmatively saying "no project mapping".
# knowledge-ctx missing from PATH, or crashing for an unrelated reason, must
# be treated as INCONCLUSIVE and fall through to the same loud error — never
# read as "unknown directory, launch freely". Only knowledge-ctx's own
# NO_PROJECT_MAPPING exit code (42; bin/knowledge-ctx) is the affirmative
# signal that takes the generic tail.
#
# All cases below are driven against the real `bin/co`, under a fake HOME,
# with a stub `claude` prepended to PATH — it records its argv instead of
# launching anything. Most cases also stub `knowledge-ctx`, whose exit code
# stands in for "is this cwd a recognized KB project" (MOCK_KC_EXIT); the
# PATH-missing cases below deliberately do NOT stub it.
#
# `co` is zsh (excluded from the ARCH-082 bash-3.2 contract by lint.sh's
# shebang filter); bash invoking it here is exactly how a login shell will.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

CO="$REPO_ROOT/bin/co"

# A PATH with every `.local/bin`-ish entry stripped, so a test that wants
# knowledge-ctx genuinely absent isn't accidentally rescued by the real one
# already installed on this machine's PATH.
PATH_WITHOUT_LOCAL_BIN=$(printf '%s' "$PATH" | tr ':' '\n' | grep -v '\.local/bin' | paste -sd: -)

# Fake HOME with a stub bin/ on PATH ahead of the real one, and one target
# directory under Convivy to run `co` from. Echoes the fake HOME.
setup() { # -> echoes fake HOME
    local home
    home=$(mktemp -d)
    mkdir -p "$home/Convivy/brandnew" "$home/stubbin"

    cat > "$home/stubbin/claude" <<'EOF'
#!/bin/bash
# Records argv instead of launching anything real.
printf '%s\n' "$@" > "$CLAUDE_CALL_LOG"
exit 0
EOF
    chmod +x "$home/stubbin/claude"

    echo "$home"
}

# Adds a stub knowledge-ctx to an already-`setup`'d fake HOME, exiting
# $MOCK_KC_EXIT. 42 is knowledge-ctx's real NO_PROJECT_MAPPING code (the
# only affirmative "unknown" signal); other values stand in for a known
# project (0), or knowledge-ctx failing for a reason unrelated to project
# resolution (anything else — a crash, an internal die() at the default
# code, etc).
stub_knowledge_ctx() { # <home>
    local home=$1
    cat > "$home/stubbin/knowledge-ctx" <<'EOF'
#!/bin/bash
exit "${MOCK_KC_EXIT:-42}"
EOF
    chmod +x "$home/stubbin/knowledge-ctx"
}

# run_co <home> [co-args...] -> nothing; sets $RC, $CLAUDE_ARGS, $STDERR
# PATH defaults to the stub bin + the real PATH (so git etc. resolve); pass
# CO_TEST_PATH to override for a case that must NOT find a real knowledge-ctx.
run_co() {
    local home=$1; shift
    local call_log stderr_log path
    call_log=$(mktemp)
    stderr_log=$(mktemp)
    path="${CO_TEST_PATH:-$home/stubbin:$PATH}"
    ( cd "$home/Convivy/brandnew" && \
      HOME="$home" PATH="$path" CLAUDE_CALL_LOG="$call_log" \
      MOCK_KC_EXIT="${MOCK_KC_EXIT:-42}" /bin/zsh "$CO" --root-config "$(co_root_config "$home")" "$@" \
      >/dev/null 2>"$stderr_log" )
    RC=$?
    CLAUDE_ARGS=$(cat "$call_log")
    STDERR=$(cat "$stderr_log")
    rm -f "$call_log" "$stderr_log"
}

assert_falls_through_to_individuated_error() { # <label>
    local label=$1
    assert_rc 1 "$RC" "$label: exits 1"
    assert_eq "1" "$(echo "$STDERR" | grep -q "is not individuated" && echo 1 || echo 0)" \
        "$label: the loud 'not individuated' message still fires"
    assert_eq "1" "$(echo "$STDERR" | grep -q "knowledge-ctx use daytime" && echo 1 || echo 0)" \
        "$label: the knowledge-ctx guidance is still printed"
    assert_eq "" "$CLAUDE_ARGS" "$label: claude is never launched"
}

# --- 1. unrecognized directory, no individuated agent -> plain claude ---------
H=$(setup); stub_knowledge_ctx "$H"
MOCK_KC_EXIT=42 run_co "$H"
assert_rc 0 "$RC" "an unrecognized brand-new directory launches cleanly"
assert_eq "1" "$(echo "$CLAUDE_ARGS" | grep -q -- '--remote-control' && echo 1 || echo 0)" \
    "claude is launched with --remote-control"
assert_eq "1" "$(echo "$CLAUDE_ARGS" | grep -q -- '--permission-mode' && echo 1 || echo 0)" \
    "claude is launched with --permission-mode preserved"
assert_eq "0" "$(echo "$CLAUDE_ARGS" | grep -q -- '--agent' && echo 1 || echo 0)" \
    "no --agent is passed for an unrecognized directory"
rm -rf "$H"

# --- 2. same, but with -r: a no-op KB pull, not an error ----------------------
H=$(setup); stub_knowledge_ctx "$H"
MOCK_KC_EXIT=42 run_co "$H" -r
assert_rc 0 "$RC" "-r in a brand-new directory does not error (no KB to pull)"
assert_eq "1" "$(echo "$CLAUDE_ARGS" | grep -qx -- '/handoff-pickup' && echo 1 || echo 0)" \
    "-r with no explicit prompt still resumes from /handoff-pickup"
rm -rf "$H"

# --- 3. known product (knowledge-ctx resolves it), agents un-individuated -----
# THE GATE THAT MUST NEVER SILENTLY DISAPPEAR. Even though nothing is
# individuated here either, this directory IS a recognized KB project, so the
# generic tail must not fire — the existing loud error is what a person needs.
H=$(setup); stub_knowledge_ctx "$H"
MOCK_KC_EXIT=0 run_co "$H"
assert_falls_through_to_individuated_error "known product, un-individuated agents"
rm -rf "$H"

# --- 4. an individuated agent takes the normal --agent path regardless ---------
H=$(setup); stub_knowledge_ctx "$H"
mkdir -p "$H/Convivy/brandnew/.claude/agents"
: > "$H/Convivy/brandnew/.claude/agents/planning-orchestrator.md"
MOCK_KC_EXIT=42 run_co "$H"
assert_rc 0 "$RC" "a directory with an individuated agent launches cleanly"
assert_eq "1" "$(echo "$CLAUDE_ARGS" | grep -q -- '--agent' && echo 1 || echo 0)" \
    "the normal --agent path is taken once something is individuated, even if unrecognized"
rm -rf "$H"

# --- 5. knowledge-ctx missing from PATH -> inconclusive, falls through --------
# THE CASE THE SECURITY REVIEW FOUND MISSING. A fresh worktree or a mini
# before knowledge-ctx is installed must NOT be read as "no project" just
# because the tool that would answer isn't there to ask.
H=$(setup)  # deliberately no stub_knowledge_ctx: the binary is simply absent
CO_TEST_PATH="$H/stubbin:$PATH_WITHOUT_LOCAL_BIN" run_co "$H"
assert_falls_through_to_individuated_error "knowledge-ctx missing from PATH"
rm -rf "$H"

# --- 6. knowledge-ctx present but fails for an unrelated reason ---------------
# A stub that exits non-zero WITHOUT being knowledge-ctx's own affirmative
# NO_PROJECT_MAPPING code — standing in for an internal crash, a permissions
# glitch, or ~/Convivy/knowledge missing/mid-move. Must be just as inconclusive
# as the tool being absent, not read as "no project".
H=$(setup); stub_knowledge_ctx "$H"
MOCK_KC_EXIT=1 run_co "$H"
assert_falls_through_to_individuated_error "knowledge-ctx present but fails unrelated"
rm -rf "$H"

finish
