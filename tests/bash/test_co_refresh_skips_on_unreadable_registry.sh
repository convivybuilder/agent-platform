#!/bin/bash
# Tests for `co -r`'s generic daytime validator (`_co_generic_daytime_refresh`)
# when its OWN registry read fails (round 2, reviewer, PR #991 — non-blocking,
# substance).
#
# THE FINDING. On origin/main, this validator matched its four hardcoded
# product names with no registry read at all before falling to
# `_co_is_an_adopter_crew`. The registry cutover made BOTH legs
# (`_co_generic_refresh_allowed`, `_co_is_an_adopter_crew`) read
# config/projects.json, and the caller's `_co_generic_daytime_refresh || exit
# 1` turned a read failure there into a hard exit for `-r`.
#
# THE FIX gives this validator its OWN registry-readable check
# (`_co_registry_readable`) ahead of those two legs, returning 2 — distinct
# from 1, "reads cleanly but isn't a recognized product" — so the caller
# skips the refresh (no knowledge pull, no re-individuation) and falls
# through to the launch, printing why, rather than exiting.
#
# WHAT THIS TEST CANNOT SHOW, stated because the obvious fixture does not
# prove it: a WHOLLY unreadable registry file (corrupt JSON, missing, no
# python3) never reaches this validator at all — the walled-off-crew check and
# `_co_private_crew` (ADR-0043 and platform/decisions/0100) already refuse to
# launch unconditionally, for every directory and every co invocation, the
# moment EITHER OWN registry read fails, well before
# `_co_generic_daytime_refresh` runs. Those gates predate this PR and this fix
# does not touch them. Confirmed directly: pointing `--root-config`'s registry
# at a file that is not JSON makes `co -r` exit 1 with "cannot tell whether
# ... is inside the walled-off crew" — never reaching this validator's own
# message.
#
# So this fixture is narrower and more honest than "corrupt the whole
# registry": a python3 wrapper that fails ONLY the specific heredoc
# `_co_registry_readable` runs (matched by the two-line body unique to it —
# see `projects._load_raw.cache_clear()` immediately followed by
# `projects.load()`, which no other heredoc in this file has), while every
# other registry read in the same `co -r` invocation — including the
# walled-off check and `_co_private_crew`'s own, which both run first — keeps
# working normally. That is a real, if narrow, way for this validator's
# dependency to fail on its own (a `projects` import that raises partway
# through loading, a disk error that lands on this one read and not the
# other) without claiming a scenario this file's own earlier gates already
# rule out end to end.
#
# `co` is zsh (excluded from the ARCH-082 bash-3.2 contract by lint.sh's
# shebang filter); bash invoking it here is exactly how a login shell will.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

CO="$REPO_ROOT/bin/co"
REAL_PYTHON3=$(command -v python3)

setup() { # -> echoes fake HOME
    local home
    home=$(mktemp -d)
    mkdir -p "$home/stubbin"

    # A made-up, already-individuated directory — not `knowledge` or
    # `dev-setup` (the two literal legacy names this validator never reads
    # the registry for at all), and not a name any real crew owns, so this
    # exercises the validator's generic `*)` leg precisely.
    mkdir -p "$home/Convivy/widget/.claude/agents"
    : > "$home/Convivy/widget/.claude/agents/planning-orchestrator.md"
    ( cd "$home/Convivy/widget" && git init -q . )

    cat > "$home/stubbin/claude" <<'EOF'
#!/bin/bash
printf '%s\n' "$@" > "$CLAUDE_CALL_LOG"
exit 0
EOF
    chmod +x "$home/stubbin/claude"

    # Records every call, so a skipped refresh's absence is provable rather
    # than assumed.
    cat > "$home/stubbin/knowledge-ctx" <<'EOF'
#!/bin/bash
printf '%s\n' "$*" >> "$KX_CALL_LOG"
exit 0
EOF
    chmod +x "$home/stubbin/knowledge-ctx"

    write_kb_sync_stub "$home/stubbin"

    # Fails ONLY `_co_registry_readable`'s own heredoc (see header for why
    # this is matched surgically rather than breaking python3 wholesale, or
    # the registry file itself, which trips an earlier, unrelated gate).
    # `bin/builder-root` (co's own bootstrap, run via its `#!/usr/bin/env
    # python3` shebang rather than a `python3 - ...<<PY` heredoc) invokes
    # this same stub with a script PATH as `$1`, never `-`, so that case
    # passes straight through unexamined — reading its stdin here would
    # otherwise block on nothing and starve it of its own argv.
    cat > "$home/stubbin/python3" <<EOF
#!/bin/bash
if [ "\$1" = "-" ]; then
    shift
    script=\$(cat)
    case "\$script" in
        *'projects._load_raw.cache_clear()
    projects.load()'*)
            exit 3 ;;
    esac
    printf '%s' "\$script" | "$REAL_PYTHON3" - "\$@"
else
    exec "$REAL_PYTHON3" "\$@"
fi
EOF
    chmod +x "$home/stubbin/python3"

    echo "$home"
}

# run_co <home> <dir-relative-to-home> [co-args...] -> RC, CLAUDE_ARGS, KX_ARGS, AGENTCTL_ARGS, ERR
run_co() {
    local home=$1 rel=$2; shift 2
    local call_log kx_log agentctl_log err_log
    call_log=$(mktemp); kx_log=$(mktemp); agentctl_log=$(mktemp); err_log=$(mktemp)
    ( cd "$home/$rel" && HOME="$home" PATH="$home/stubbin:$PATH" \
      CLAUDE_CALL_LOG="$call_log" KX_CALL_LOG="$kx_log" AGENTCTL_LOG="$agentctl_log" \
      TMUX="" TMUX_PANE="" \
      /bin/zsh "$CO" --root-config "$(co_root_config "$home")" "$@" \
      >/dev/null 2>"$err_log" )
    RC=$?
    CLAUDE_ARGS=$(cat "$call_log"); KX_ARGS=$(cat "$kx_log")
    AGENTCTL_ARGS=$(cat "$agentctl_log"); ERR=$(cat "$err_log")
    rm -f "$call_log" "$kx_log" "$agentctl_log" "$err_log"
}

H=$(setup)

echo "== co -r launches rather than exiting when its own registry read fails =="
run_co "$H" Convivy/widget -r
assert_rc 0 "$RC" "widget -r: co launches rather than hard-exiting"
assert_eq "1" "$(printf '%s' "$CLAUDE_ARGS" | grep -qc . && echo 1 || echo 0)" \
    "widget -r: claude is actually launched"

echo "== the skip is announced, with why =="
assert_contains "$ERR" "registry is unreadable" \
    "widget -r: stderr says the registry could not be read"
assert_contains "$ERR" "skipping the refresh" \
    "widget -r: stderr says the refresh was skipped"

echo "== the refresh itself never runs =="
assert_eq "" "$KX_ARGS" "widget -r: knowledge-ctx is never called (no re-individuation)"
assert_eq "" "$AGENTCTL_ARGS" "widget -r: agentctl kb sync is never called (no knowledge pull)"

rm -rf "$H"
finish
