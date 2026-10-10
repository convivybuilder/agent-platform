#!/bin/bash
# CI-visible wiring test for bin/co's CLAUDE_CODE_THRIFTY_SONIC pin (ARCH-292's
# sibling: reviewer finding on PR #505). The only other guard for this pin,
# tests/test_claude_thrifty_sonic_pin.py, spawns the real `claude` binary and
# never runs on CI's `convivy-linux` runner (no `claude` on PATH there, not
# verified on ci1), so
# nothing CI-visible was checking that `bin/co` actually sets the var on a
# bypassPermissions launch — a later edit moving the export inside a
# conditional would ship green.
#
# This does not prove the var suppresses the real steer (that needs the live
# binary and a subscription token — see test_claude_thrifty_sonic_pin.py's own
# docstring); it proves the WIRING: that every bypassPermissions launch path
# actually sets CLAUDE_CODE_THRIFTY_SONIC=0 in the spawned process's
# environment. Same stub-and-capture shape as test_co_opus_pin.sh, extended to
# dump env alongside argv since `CLAUDE_CODE_THRIFTY_SONIC` is not a flag.
#
# `co` is zsh (excluded from the ARCH-082 bash-3.2 contract by lint.sh's
# shebang filter); bash invoking it here is exactly how a login shell will.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

CO="$REPO_ROOT/bin/co"

# A fake HOME with one ordinary, unpinned repo — the generic-tail target
# (test_co_generic_tail.sh's own fixture shape). CLAUDE_CODE_THRIFTY_SONIC is
# exported once near the top of bin/co, ahead of every branch that picks a
# launch path, so an ordinary unrecognized repo exercises the same export an
# orchestrator or crew-cockpit launch would.
setup() { # -> echoes fake HOME
    local home
    home=$(mktemp -d)
    mkdir -p "$home/Convivy/some-repo" "$home/stubbin"
    ( cd "$home/Convivy/some-repo" && git init -q . )

    cat > "$home/stubbin/claude" <<'EOF'
#!/bin/bash
# Records argv AND env instead of launching anything real.
printf '%s\n' "$@" > "$CLAUDE_CALL_LOG"
env > "$CLAUDE_CALL_ENV_LOG"
exit 0
EOF
    chmod +x "$home/stubbin/claude"
    cat > "$home/stubbin/knowledge-ctx" <<'EOF'
#!/bin/bash
exit 42
EOF
    chmod +x "$home/stubbin/knowledge-ctx"

    echo "$home"
}

# run_co <home> -> sets RC, CLAUDE_ARGS, CLAUDE_ENV
run_co() {
    local home=$1
    local call_log env_log
    call_log=$(mktemp)
    env_log=$(mktemp)
    ( cd "$home/Convivy/some-repo" && HOME="$home" PATH="$home/stubbin:$PATH" \
      CLAUDE_CALL_LOG="$call_log" CLAUDE_CALL_ENV_LOG="$env_log" \
      /bin/zsh "$CO" --root-config "$(co_root_config "$home")" >/dev/null 2>&1 )
    RC=$?
    CLAUDE_ARGS=$(cat "$call_log")
    CLAUDE_ENV=$(cat "$env_log")
    rm -f "$call_log" "$env_log"
}

H=$(setup)
run_co "$H"
assert_rc 0 "$RC" "co launches cleanly against the generic-tail target"
assert_eq "1" "$(echo "$CLAUDE_ARGS" | grep -q -- '--permission-mode' && echo 1 || echo 0)" \
    "sanity: this launch path really does pass --permission-mode (bypassPermissions)"
assert_eq "1" "$(echo "$CLAUDE_ENV" | grep -qx 'CLAUDE_CODE_THRIFTY_SONIC=0' && echo 1 || echo 0)" \
    "the spawned claude process env carries CLAUDE_CODE_THRIFTY_SONIC=0"
rm -rf "$H"

finish
