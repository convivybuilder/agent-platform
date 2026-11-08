#!/bin/bash
# The BLACK BOX transport wall (platform/decisions/0043).
#
# WHAT THIS GUARDS. A black box crew is one whose content must never reach the
# Library and whose agents must never reach Orient. The control that delivers that
# is `--strict-mcp-config` on the LEDGER launch: it starts the session with zero MCP
# servers, so `ops` and `kb` are not in the session at all. Those two are in the
# GLOBAL mcpServers block of ~/.claude.json, so without this flag every session in
# every directory has them — a LEDGER session ran 20 hours with issue_create and
# kb_create in reach.
#
# WHY A PERMISSION RULE WOULD NOT DO. The same exec passes
# `--permission-mode bypassPermissions`, which is precisely what skips permission
# checks, so a `permissions.deny` entry would be bypassed. The two flags travelling
# together is the whole point, and the third assertion below pins it: if someone
# later drops bypassPermissions the deny-rule reasoning changes, and if someone
# drops strict-mcp-config the wall is gone while the session still looks right.
#
# THE WALLED-OFF CREW (platform/decisions/0100) MUST NEVER GET THE FLAG. It strips
# the account-level claude.ai connectors too (Gmail and Calendar verified absent
# under it), and those connectors are the point of that crew. Its wall is the
# inverted MCP default in ADR-0043, which is not built.
set -u
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
. "$HERE/lib.sh"

CO="$REPO_ROOT/bin/co"

setup() { # -> echoes fake HOME
    local home
    home=$(mktemp -d)
    mkdir -p "$home/stubbin" "$home/Convivy/LEDGER/.claude/agents" "$home/Convivy/parlor" \
             "$home/Convivy/private-a" \
             "$home/Convivy/SATCHEL"
    ( cd "$home/Convivy/LEDGER" && git init -q . )
    cat > "$home/stubbin/claude" <<'EOF'
#!/bin/bash
printf '%s\n' "$@" > "$CLAUDE_CALL_LOG"
exit 0
EOF
    chmod +x "$home/stubbin/claude"
    # A registry naming one synthetic black-box crew, for the two sections below
    # that exercise `_co_private_crew` without a real arm of its own in bin/co —
    # unused by every other section here, which reads the made-up, production-shaped
    # `tests/fixtures/rich_registry.json` (LEDGER, SATCHEL are not personal names).
    write_private_registry "$home/synthetic-private-registry.json" private-a private-a.example
    # A registry naming one synthetic walled-off crew (platform/decisions/0100),
    # for the "must never get the flag" section below, which needs the walled-off
    # directory to resolve against something — unused by every other section.
    write_walled_off_registry "$home/walled-registry.json" parlor Parlor
    echo "$home"
}

run_co_in() { # <home> <rel> [args...] -> RC, CLAUDE_ARGS
    local home=$1 rel=$2; shift 2
    local call_log root_config; call_log=$(mktemp)
    if [ -n "${REGISTRY_OVERRIDE:-}" ]; then
        root_config=$(co_root_config "$home" "$REGISTRY_OVERRIDE")
    else
        root_config=$(co_root_config "$home" "$REPO_ROOT/tests/fixtures/rich_registry.json")
    fi
    ( cd "$home/$rel" && HOME="$home" PATH="$home/stubbin:$PATH" \
      CLAUDE_CALL_LOG="$call_log" /bin/zsh "$CO" --root-config "$root_config" "$@" >/dev/null 2>&1 )
    RC=$?
    CLAUDE_ARGS=$(cat "$call_log")
    rm -f "$call_log"
}

has_arg() { echo "$CLAUDE_ARGS" | grep -qx -- "$1" && echo 1 || echo 0; }

echo "== LEDGER launches with the transport wall =="
H=$(setup)
run_co_in "$H" "Convivy/LEDGER"
assert_eq "1" "$(has_arg '--strict-mcp-config')" \
    "LEDGER: --strict-mcp-config present — ops and kb are not in the session"
assert_eq "1" "$(has_arg '--permission-mode')" \
    "LEDGER: still launches a session"
assert_eq "0" "$(has_arg '--agent')" \
    "LEDGER: no --agent; the directory's CLAUDE.md supplies the role"
rm -rf "$H"

echo "== the wall survives -r, which is the mode that bypasses permissions =="
H=$(setup)
run_co_in "$H" "Convivy/LEDGER" -r
assert_eq "1" "$(has_arg '--strict-mcp-config')" \
    "LEDGER -r: the wall holds on the refresh path too"
rm -rf "$H"

# NOT TESTED HERE, deliberately: the second walled-off gate at bin/co's
# private-crew refusal block. It is fixed for consistency, but a symlinked walled-off
# directory launches IDENTICAL argv whether the gate resolves or not — the
# fall-through also reaches a bare launch — so the only observable difference is the
# tmux styling and the transcript namespace, neither of which this argv-capturing
# harness can see. A test was written, mutation-tested, and found to pass against
# the broken gate as well as the fixed one, so it was removed rather than shipped.
# An assertion that cannot fail is worse than none.

echo "== the walled-off crew must NOT get the flag: it would strip the account connectors =="
H=$(setup)
REGISTRY_OVERRIDE="$H/walled-registry.json" run_co_in "$H" "Convivy/parlor"
assert_eq "1" "$(has_arg '--permission-mode')" \
    "parlor: launches"
assert_eq "0" "$(has_arg '--strict-mcp-config')" \
    "parlor: NO --strict-mcp-config — Gmail/Calendar are the point of that crew"
rm -rf "$H"

# THE DANGEROUS DIRECTION, which the lookalike test below does NOT cover. That one
# checks a directory that resembles LEDGER and must not match. These check the reverse:
# a session physically INSIDE LEDGER's real directory that reaches it by another
# spelling. Both were demonstrated by security-reviewer on PR #409 against the
# literal-comparison version of this gate, and both launched with no wall.
echo "== a symlink into LEDGER still gets the wall =="
H=$(setup)
ln -s "$H/Convivy/LEDGER" "$H/ledger-shortcut"
run_co_in "$H" "ledger-shortcut"
assert_eq "1" "$(has_arg '--strict-mcp-config')" \
    "symlinked path: the wall applies — \$PWD is LOGICAL, so zsh does not resolve
     the link into it and a literal comparison misses while the session sits in the
     real finance directory"
rm -rf "$H"

echo "== a differently-cased path still gets the wall =="
# The boot volume is case-INSENSITIVE by default, so `cd ~/convivy/ledger` lands in the
# real directory while \$PWD keeps the case that was typed. Skipped where the
# filesystem is case-sensitive, since there the directory genuinely does not exist.
H=$(setup)
if ( cd "$H/convivy/ledger" 2>/dev/null ); then
    run_co_in "$H" "convivy/ledger"
    assert_eq "1" "$(has_arg '--strict-mcp-config')" \
        "lowercase path on a case-insensitive volume: the wall applies"
else
    echo "  (skipped: case-sensitive filesystem, the lowercase path does not resolve)"
fi
rm -rf "$H"

# THE WALL IS KEYED ON THE REGISTRY, NOT ON A LITERAL PATH PER CREW. SATCHEL is the second
# black box (2026-09-06) and has no arm of its own in bin/co: it takes the wall
# because the registry marks it `private: true`. The fake HOME's registry is
# the fixture `run_co_in` points at, and `~/Convivy/SATCHEL` expands under the fake
# HOME, so this exercises the real lookup end to end.
echo "== SATCHEL, registered private with no arm of its own, gets the wall =="
H=$(setup)
run_co_in "$H" "Convivy/SATCHEL"
assert_eq "1" "$(has_arg '--strict-mcp-config')" \
    "SATCHEL: --strict-mcp-config present from the registry alone"
assert_eq "1" "$(has_arg '--permission-mode')" \
    "SATCHEL: still launches a session"
rm -rf "$H"

echo "== a subdirectory of a black box gets the wall =="
# The literal form matched the root only; a session in LEDGER/2025/ was as much inside
# LEDGER's data as one at its root and launched with the shared servers attached.
H=$(setup)
mkdir -p "$H/Convivy/LEDGER/2025"
run_co_in "$H" "Convivy/LEDGER/2025"
assert_eq "1" "$(has_arg '--strict-mcp-config')" \
    "LEDGER/2025: the wall walks up to the crew home"
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

echo "== a walled-off home is never treated as a private black box =="
# `validate_registry` refuses a unit carrying both `walled_off` and `private`
# outright (platform/decisions/0100), but the private-crew refusal block checks
# `_co_walled_slug` again regardless — belt and suspenders, for a registry that
# got past that refusal some other way. This exercises that second check
# directly: a hand-crafted registry marking the SAME unit both, bypassing
# `validate_registry`, which bin/co's own registry read never runs. co must
# launch nothing and say what to fix, since the flag would strip the account
# connectors that are the point of the walled-off crew.
H=$(setup)
cat > "$H/both-flags-registry.json" <<JSON
{
  "parlor": {
    "class": "project", "display": "Parlor", "owner": "convivy",
    "home_dir": "<root>/parlor", "dirs": [], "crew": "parlor",
    "lifecycle": "active", "products": [], "repos": ["parlor"],
    "aliases": [], "kb": "local", "walled_off": true, "private": true
  }
}
JSON
REGISTRY_OVERRIDE="$H/both-flags-registry.json" run_co_in "$H" "Convivy/parlor"
assert_eq "1" "$RC" "parlor-marked-both: co exits non-zero"
assert_eq "" "$CLAUDE_ARGS" "parlor-marked-both: claude is never launched, with or without the flag"
rm -rf "$H"

echo "== a registered crew with no directory yet does not break the others =="
# SATCHEL is registered before its directory exists. The helper iterates the private
# set, whose order follows the per-process hash seed, so a missing home that
# raises on comparison broke LEDGER's launch on about half of all runs — in CI, and
# never locally, because this fixture used to create every private home. Eight
# fresh processes cover both orders; after the fix every one carries the wall.
H=$(setup)
rm -rf "$H/Convivy/SATCHEL"
walled=0
for _i in 1 2 3 4 5 6 7 8; do
    run_co_in "$H" "Convivy/LEDGER"
    [ "$(has_arg '--strict-mcp-config')" = "1" ] && walled=$((walled + 1))
done
assert_eq "8" "$walled" "LEDGER gets the wall on every launch while SATCHEL's directory is absent"
rm -rf "$H"

echo "== a lookalike directory does not take the LEDGER branch =="
H=$(setup)
mkdir -p "$H/Convivy/LEDGER-scratch" && ( cd "$H/Convivy/LEDGER-scratch" && git init -q . )
run_co_in "$H" "Convivy/LEDGER-scratch"
assert_eq "0" "$(has_arg '--strict-mcp-config')" \
    "LEDGER-scratch: does not match the LEDGER branch"
rm -rf "$H"

# A third black box, on SATCHEL's terms: a bare directory with no arm of its own in
# bin/co, private from the registry alone. Launching it without the wall would put
# kb and ops in the session of the crew holding its private data. Run against the
# synthetic registry `setup` wrote, not the fixture `run_co_in` otherwise reads,
# which names no crew under this slug (the PROJECTS_REGISTRY seam bin/co and
# co_root_config share).
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
assert_eq "private-a.example" "$(name_in Convivy/private-a/Back other-crew--back)" \
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
( cd "$H/Convivy/LEDGER" && HOME="$H" PATH="$H/stubbin:$PATH" CLAUDE_CALL_LOG="$call_log" \
  /bin/zsh "$cobin/co" >/dev/null 2>&1 )
rc=$?
assert_rc 1 "$rc" "co exits 1 when builder-root cannot resolve the root"
assert_eq "" "$(cat "$call_log")" "and no claude session starts"
rm -rf "$H" "$cobin" "$call_log"

finish
