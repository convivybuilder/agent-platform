#!/bin/bash
# The BLACK BOX transport wall (platform/decisions/0043).
#
# WHAT THIS GUARDS. A black box crew is one whose content must never reach the
# Library and whose agents must never reach Orient. The control that delivers that
# is `--strict-mcp-config` on the CFO launch: it starts the session with zero MCP
# servers, so `ops` and `kb` are not in the session at all. Those two are in the
# GLOBAL mcpServers block of ~/.claude.json, so without this flag every session in
# every directory has them — a CFO session ran 20 hours with issue_create and
# kb_create in reach.
#
# WHY A PERMISSION RULE WOULD NOT DO. The same exec passes
# `--permission-mode bypassPermissions`, which is precisely what skips permission
# checks, so a `permissions.deny` entry would be bypassed. The two flags travelling
# together is the whole point, and the third assertion below pins it: if someone
# later drops bypassPermissions the deny-rule reasoning changes, and if someone
# drops strict-mcp-config the wall is gone while the session still looks right.
#
# ASK MUST NEVER GET THE FLAG. It strips the account-level claude.ai connectors too
# (Gmail and Calendar verified absent under it), and those connectors are the point
# of Ask. Its wall is the inverted MCP default in ADR-0043, which is not built.
set -u
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
. "$HERE/lib.sh"

CO="$REPO_ROOT/bin/co"

setup() { # -> echoes fake HOME
    local home
    home=$(mktemp -d)
    mkdir -p "$home/stubbin" "$home/Convivy/CFO/.claude/agents" "$home/Convivy/ask" \
             "$home/Convivy/private-a" \
             "$home/Convivy/EA"
    ( cd "$home/Convivy/CFO" && git init -q . )
    cat > "$home/stubbin/claude" <<'EOF'
#!/bin/bash
printf '%s\n' "$@" > "$CLAUDE_CALL_LOG"
exit 0
EOF
    chmod +x "$home/stubbin/claude"
    # A registry naming one synthetic black-box crew, for the two sections below
    # that exercise `_co_private_crew` without a real arm of its own in bin/co —
    # unused by every other section here, which reads the operator's own
    # checked-in config/projects.json (CFO, EA, ask are not personal names).
    write_private_registry "$home/synthetic-private-registry.json" private-a private-a.example
    echo "$home"
}

run_co_in() { # <home> <rel> [args...] -> RC, CLAUDE_ARGS
    local home=$1 rel=$2; shift 2
    local call_log root_config; call_log=$(mktemp)
    if [ -n "${REGISTRY_OVERRIDE:-}" ]; then
        root_config=$(co_root_config "$home" "$REGISTRY_OVERRIDE")
    else
        root_config=$(co_root_config "$home")
    fi
    ( cd "$home/$rel" && HOME="$home" PATH="$home/stubbin:$PATH" \
      CLAUDE_CALL_LOG="$call_log" /bin/zsh "$CO" --root-config "$root_config" "$@" >/dev/null 2>&1 )
    RC=$?
    CLAUDE_ARGS=$(cat "$call_log")
    rm -f "$call_log"
}

has_arg() { echo "$CLAUDE_ARGS" | grep -qx -- "$1" && echo 1 || echo 0; }

echo "== CFO launches with the transport wall =="
H=$(setup)
run_co_in "$H" "Convivy/CFO"
assert_eq "1" "$(has_arg '--strict-mcp-config')" \
    "CFO: --strict-mcp-config present — ops and kb are not in the session"
assert_eq "1" "$(has_arg '--permission-mode')" \
    "CFO: still launches a session"
assert_eq "0" "$(has_arg '--agent')" \
    "CFO: no --agent; the directory's CLAUDE.md supplies the role"
rm -rf "$H"

echo "== the wall survives -r, which is the mode that bypasses permissions =="
H=$(setup)
run_co_in "$H" "Convivy/CFO" -r
assert_eq "1" "$(has_arg '--strict-mcp-config')" \
    "CFO -r: the wall holds on the refresh path too"
rm -rf "$H"

# NOT TESTED HERE, deliberately: the second `ask` gate at bin/co:369. It is fixed
# for consistency, but a symlinked ask directory launches IDENTICAL argv whether the
# gate resolves or not — the fall-through also reaches a bare launch — so the only
# observable difference is the tmux styling and the transcript namespace, neither of
# which this argv-capturing harness can see. A test was written, mutation-tested,
# and found to pass against the broken gate as well as the fixed one, so it was
# removed rather than shipped. An assertion that cannot fail is worse than none.

echo "== ask must NOT get the flag: it would strip the account connectors =="
H=$(setup)
run_co_in "$H" "Convivy/ask"
assert_eq "1" "$(has_arg '--permission-mode')" \
    "ask: launches"
assert_eq "0" "$(has_arg '--strict-mcp-config')" \
    "ask: NO --strict-mcp-config — Gmail/Calendar are the point of Ask"
rm -rf "$H"

# THE DANGEROUS DIRECTION, which the lookalike test below does NOT cover. That one
# checks a directory that resembles CFO and must not match. These check the reverse:
# a session physically INSIDE CFO's real directory that reaches it by another
# spelling. Both were demonstrated by security-reviewer on PR #409 against the
# literal-comparison version of this gate, and both launched with no wall.
echo "== a symlink into CFO still gets the wall =="
H=$(setup)
ln -s "$H/Convivy/CFO" "$H/cfo-shortcut"
run_co_in "$H" "cfo-shortcut"
assert_eq "1" "$(has_arg '--strict-mcp-config')" \
    "symlinked path: the wall applies — \$PWD is LOGICAL, so zsh does not resolve
     the link into it and a literal comparison misses while the session sits in the
     real finance directory"
rm -rf "$H"

echo "== a differently-cased path still gets the wall =="
# The boot volume is case-INSENSITIVE by default, so `cd ~/convivy/cfo` lands in the
# real directory while \$PWD keeps the case that was typed. Skipped where the
# filesystem is case-sensitive, since there the directory genuinely does not exist.
H=$(setup)
if ( cd "$H/convivy/cfo" 2>/dev/null ); then
    run_co_in "$H" "convivy/cfo"
    assert_eq "1" "$(has_arg '--strict-mcp-config')" \
        "lowercase path on a case-insensitive volume: the wall applies"
else
    echo "  (skipped: case-sensitive filesystem, the lowercase path does not resolve)"
fi
rm -rf "$H"

# THE WALL IS KEYED ON THE REGISTRY, NOT ON A LITERAL PATH PER CREW. EA is the second
# black box (2026-09-06) and has no arm of its own in bin/co: it takes the wall
# because config/projects.json marks it `private: true`. The fake HOME's registry is
# the checkout's own (private_crew resolves it from its tree), and `~/Convivy/EA`
# expands under the fake HOME, so this exercises the real lookup end to end.
echo "== EA, registered private with no arm of its own, gets the wall =="
H=$(setup)
run_co_in "$H" "Convivy/EA"
assert_eq "1" "$(has_arg '--strict-mcp-config')" \
    "EA: --strict-mcp-config present from the registry alone"
assert_eq "1" "$(has_arg '--permission-mode')" \
    "EA: still launches a session"
rm -rf "$H"

echo "== a subdirectory of a black box gets the wall =="
# The literal form matched the root only; a session in CFO/2025/ was as much inside
# CFO's data as one at its root and launched with the shared servers attached.
H=$(setup)
mkdir -p "$H/Convivy/CFO/2025"
run_co_in "$H" "Convivy/CFO/2025"
assert_eq "1" "$(has_arg '--strict-mcp-config')" \
    "CFO/2025: the wall walks up to the crew home"
rm -rf "$H"

echo "== when co cannot tell, it refuses to launch =="
# The helper is python3 on the fake PATH here, so a python3 that cannot answer stands
# in for an unreadable registry or a missing lib. Refusing is the fail-closed
# direction ADR-0043 requires: no claude call at all, and a non-zero exit.
H=$(setup)
cat > "$H/stubbin/python3" <<'STUB'
#!/bin/bash
exit 3
STUB
chmod +x "$H/stubbin/python3"
mkdir -p "$H/Convivy/plain" && ( cd "$H/Convivy/plain" && git init -q . )
run_co_in "$H" "Convivy/plain"
assert_eq "1" "$RC" "cannot-tell: co exits non-zero"
assert_eq "" "$CLAUDE_ARGS" "cannot-tell: claude is never launched"
rm -rf "$H"

echo "== ask is excluded by name: a registry that calls it private is refused =="
# The private branch runs BEFORE ask's own branch, so this reaches the guard rather
# than the exec that used to shadow it. A python3 stub stands in for a registry
# marking ask private; co must launch nothing and say what to fix, since the flag
# would strip the account connectors that are the point of Ask.
H=$(setup)
cat > "$H/stubbin/python3" <<'STUB'
#!/bin/bash
printf 'ask\tAsk\n'
exit 0
STUB
chmod +x "$H/stubbin/python3"
run_co_in "$H" "Convivy/ask"
assert_eq "1" "$RC" "ask-marked-private: co exits non-zero"
assert_eq "" "$CLAUDE_ARGS" "ask-marked-private: claude is never launched, with or without the flag"
rm -rf "$H"

echo "== a registered crew with no directory yet does not break the others =="
# EA is registered before its directory exists. The helper iterates the private
# set, whose order follows the per-process hash seed, so a missing home that
# raises on comparison broke CFO's launch on about half of all runs — in CI, and
# never locally, because this fixture used to create every private home. Eight
# fresh processes cover both orders; after the fix every one carries the wall.
H=$(setup)
rm -rf "$H/Convivy/EA"
walled=0
for _i in 1 2 3 4 5 6 7 8; do
    run_co_in "$H" "Convivy/CFO"
    [ "$(has_arg '--strict-mcp-config')" = "1" ] && walled=$((walled + 1))
done
assert_eq "8" "$walled" "CFO gets the wall on every launch while EA's directory is absent"
rm -rf "$H"

echo "== a lookalike directory does not take the CFO branch =="
H=$(setup)
mkdir -p "$H/Convivy/CFO-scratch" && ( cd "$H/Convivy/CFO-scratch" && git init -q . )
run_co_in "$H" "Convivy/CFO-scratch"
assert_eq "0" "$(has_arg '--strict-mcp-config')" \
    "CFO-scratch: does not match the CFO branch"
rm -rf "$H"

# A third black box, on EA's terms: a bare directory with no arm of its own in
# bin/co, private from the registry alone. Launching it without the wall would put
# kb and ops in the session of the crew holding its private data. Run against the
# synthetic registry `setup` wrote, not the operator's own checked-in
# config/projects.json, which names this crew for real under its own slug (the
# PROJECTS_REGISTRY seam bin/co and co_root_config share).
echo "== a crew registered private with no arm of its own gets the wall =="
H=$(setup)
REGISTRY_OVERRIDE="$H/synthetic-private-registry.json" run_co_in "$H" "Convivy/private-a"
assert_eq "1" "$(has_arg '--strict-mcp-config')" \
    "private-a: --strict-mcp-config present from the registry alone"
assert_eq "1" "$(has_arg '--permission-mode')" \
    "private-a: still launches a session"
rm -rf "$H"

# A black-box crew's app sessions are tmux sessions named <slug>--back and so on,
# in subfolders of the crew. Every one used to launch under the crew's own display
# name, so the picker could not tell them apart. A stub tmux answers
# `display-message` with the session name; the real one is never called.
echo "== a subcrew launch is named for its tmux session =="
H=$(setup)
mkdir -p "$H/Convivy/private-a/Back"
cat > "$H/stubbin/tmux" <<'EOF'
#!/bin/bash
[ "$1" = display-message ] && printf '%s\n' "$STUB_TMUX_SESSION"
exit 0
EOF
chmod +x "$H/stubbin/tmux"
name_in() { # <rel> <tmux-session> -> echoes the value following --name
    local call_log; call_log=$(mktemp)
    ( cd "$H/$1" && HOME="$H" PATH="$H/stubbin:$PATH" TMUX=/tmp/stub-tmux,1,0 TMUX_PANE=%1 \
      STUB_TMUX_SESSION="$2" CLAUDE_CALL_LOG="$call_log" \
      /bin/zsh "$CO" --root-config "$(co_root_config "$H" "$H/synthetic-private-registry.json")" >/dev/null 2>&1 )
    awk '$0=="--name"{getline; print; exit}' "$call_log"
    rm -f "$call_log"
}
assert_eq "private-a--back" "$(name_in Convivy/private-a/Back private-a--back)" \
    "private-a--back: the launch takes its session's name"
assert_eq "private-a.example" "$(name_in Convivy/private-a private-a)" \
    "the parent session keeps the crew's display name"
assert_eq "private-a.example" "$(name_in Convivy/private-a/Back ea--back)" \
    "a session carrying another crew's prefix does not relabel this one"
rm -rf "$H"

# CONDITION (a), 2026-09-11: with no root, co cannot tell which crews are black boxes,
# so it stops rather than launch. A copy of co beside a builder-root that fails.
echo "== co refuses to launch when the machine's root cannot be resolved =="
H=$(setup)
cobin=$(mktemp -d)
cp "$CO" "$cobin/co"
printf '#!/bin/sh\nexit 1\n' > "$cobin/builder-root"
chmod +x "$cobin/co" "$cobin/builder-root"
call_log=$(mktemp)
( cd "$H/Convivy/CFO" && HOME="$H" PATH="$H/stubbin:$PATH" CLAUDE_CALL_LOG="$call_log" \
  /bin/zsh "$cobin/co" >/dev/null 2>&1 )
rc=$?
assert_rc 1 "$rc" "co exits 1 when builder-root cannot resolve the root"
assert_eq "" "$(cat "$call_log")" "and no claude session starts"
rm -rf "$H" "$cobin" "$call_log"

finish
