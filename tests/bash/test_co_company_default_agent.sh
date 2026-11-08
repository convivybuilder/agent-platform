#!/bin/bash
# bin/co's company-workspace branch reads its default agent from the
# registry's `co_default_agent` field rather than hardcoding
# `positioning-strategist` (issue_01M69PFRTWVXQ6Z82G35MM6P2D).
#
# WHY. The company workspace unit is being renamed, and under a company
# decision that crew holds all four commercial roles
# (positioning-strategist, commercial-writer, product-analyst,
# product-manager), with product-manager the one used day to day. The
# renamed directory does not match the `*-commercial` glob (where
# product-manager is already the default), so the only way to make
# product-manager the default for the renamed crew without re-hardcoding a
# name in bin/co is a registry field (platform decision 0100: a crew's
# settings come from the registry).
#
# A SYNTHETIC REGISTRY, not the operator's own northwind-commercial: `acme-hq`
# is flagged `company_workspace: true` with no `*-commercial` suffix, so it
# exercises only the dedicated branch, never the generic `*-commercial` one.
# `zeta-commercial` is the regression control for that other branch, proving
# this change leaves it alone.
#
# `co` is zsh (excluded from the ARCH-082 bash-3.2 contract by lint.sh's
# shebang filter); bash invoking it here is exactly how a login shell will.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

CO="$REPO_ROOT/bin/co"
PROMPT="/handoff-pickup /x/handoff.md"

setup() { # -> echoes fake HOME
    local home
    home=$(mktemp -d)
    mkdir -p "$home/stubbin"
    write_kb_sync_stub "$home/stubbin"

    # A real knowledge checkout with a real bare origin — _co_pull_knowledge
    # runs `git checkout main` for real before the stubbed `agentctl kb sync`.
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

    # acme-hq: the company-workspace unit under test. All four roles
    # individuated, matching the company decision that this crew holds all
    # four — so no case below can fail on "not individuated".
    mkdir -p "$home/Convivy/acme-hq/.claude/agents"
    for a in positioning-strategist commercial-writer product-analyst product-manager; do
        : > "$home/Convivy/acme-hq/.claude/agents/$a.md"
    done
    ( cd "$home/Convivy/acme-hq" && git init -q . )

    # zeta-commercial: the regression control for the generic `*-commercial`
    # branch, untouched by this change and never flagged company_workspace.
    mkdir -p "$home/Convivy/zeta-commercial/.claude/agents"
    : > "$home/Convivy/zeta-commercial/.claude/agents/product-manager.md"
    ( cd "$home/Convivy/zeta-commercial" && git init -q . )

    cat > "$home/stubbin/claude" <<'EOF'
#!/bin/bash
printf '%s\n' "$@" > "${CLAUDE_CALL_LOG:-/dev/null}"
exit 0
EOF
    chmod +x "$home/stubbin/claude"

    # Logs its call as one line and exits 0 unconditionally — none of the
    # cases below reach the generic tail's `knowledge-ctx list`, since every
    # fixture directory is pre-individuated.
    cat > "$home/stubbin/knowledge-ctx" <<'EOF'
#!/bin/bash
[ -n "${KX_CALL_LOG:-}" ] && echo "$@" >> "$KX_CALL_LOG"
exit 0
EOF
    chmod +x "$home/stubbin/knowledge-ctx"

    echo "$home"
}

# write_registry <path> [co_default_agent-value]: acme-hq flagged
# company_workspace, with or without the field under test, plus
# zeta-commercial (an ordinary unit — the *-commercial branch needs no
# registry row at all, but giving it one matches the operator's real shape).
write_registry() { # <path> [default-value]
    local path=$1 default=${2:-}
    local field_line=""
    if [ -n "$default" ]; then
        field_line="\"co_default_agent\": \"$default\","
    fi
    cat > "$path" <<JSON
{
  "acme-hq": {
    "class": "project", "display": "Acme HQ", "owner": "convivy",
    "home_dir": "<root>/acme-hq", "dirs": [], "crew": "acme-hq",
    "lifecycle": "active", "products": [], "repos": ["acme-hq"],
    "aliases": [], "kb": "local", "company_workspace": true, $field_line
    "task_text_public": true
  }
}
JSON
}

# run_co <home> <registry> <dir-relative-to-home> [co-args...] -> RC,
# CLAUDE_ARGS, KX_CALLS, OUT, ERR
run_co() {
    local home=$1 registry=$2 rel=$3; shift 3
    local call_log kx_log out_log err_log
    call_log=$(mktemp); kx_log=$(mktemp); out_log=$(mktemp); err_log=$(mktemp)
    ( cd "$home/$rel" && HOME="$home" PATH="$home/stubbin:$PATH" \
      CLAUDE_CALL_LOG="$call_log" KX_CALL_LOG="$kx_log" TMUX="" TMUX_PANE="" \
      /bin/zsh "$CO" --root-config "$(co_root_config "$home" "$registry")" \
      "$@" >"$out_log" 2>"$err_log" )
    RC=$?
    CLAUDE_ARGS=$(cat "$call_log"); KX_CALLS=$(cat "$kx_log")
    OUT=$(cat "$out_log"); ERR=$(cat "$err_log")
    rm -f "$call_log" "$kx_log" "$out_log" "$err_log"
}

_agent_value() { # <argv-log-text> -> echoes the value following --agent, or ""
    printf '%s\n' "$1" | awk '$0=="--agent"{getline; print; exit}'
}

_has_prompt_line() { # <argv-log-text> -> 0 iff $PROMPT reached claude as one argv element
    printf '%s\n' "$1" | grep -Fxq "$PROMPT"
}

# The auto-pickup on a bare `co -r` sets `/handoff-pickup` alone (`set --
# /handoff-pickup`, no path) — distinct from $PROMPT above, which carries a
# path and is only ever typed by rollover's own spawn_successor.
_has_bare_pickup_line() { # <argv-log-text>
    printf '%s\n' "$1" | grep -Fxq "/handoff-pickup"
}

H=$(setup)
REG="$H/registry.json"

echo "== field absent: the default stays positioning-strategist =="

write_registry "$REG"

run_co "$H" "$REG" Convivy/acme-hq
assert_rc 0 "$RC" "no args, field absent: exits 0"
assert_eq "positioning-strategist" "$(_agent_value "$CLAUDE_ARGS")" \
    "no args, field absent: launches positioning-strategist"

run_co "$H" "$REG" Convivy/acme-hq pm
assert_rc 0 "$RC" "pm, field absent: exits 0"
assert_eq "product-manager" "$(_agent_value "$CLAUDE_ARGS")" \
    "pm, field absent: selects product-manager"

run_co "$H" "$REG" Convivy/acme-hq ps
assert_eq "positioning-strategist" "$(_agent_value "$CLAUDE_ARGS")" \
    "ps, field absent: still selects positioning-strategist explicitly"

echo "== field absent: co -r with no args adds /handoff-pickup on the default, not on another role =="

run_co "$H" "$REG" Convivy/acme-hq -r
assert_rc 0 "$RC" "co -r, no args, field absent: exits 0"
assert_eq "positioning-strategist" "$(_agent_value "$CLAUDE_ARGS")" \
    "co -r, no args, field absent: launches the resolved default"
assert_eq "1" "$(_has_bare_pickup_line "$CLAUDE_ARGS" && echo 1 || echo 0)" \
    "co -r, no args, field absent: /handoff-pickup was added for the default role"

run_co "$H" "$REG" Convivy/acme-hq -r pa
assert_rc 0 "$RC" "co -r pa, field absent: exits 0"
assert_eq "product-analyst" "$(_agent_value "$CLAUDE_ARGS")" \
    "co -r pa, field absent: launches product-analyst"
assert_eq "0" "$(_has_bare_pickup_line "$CLAUDE_ARGS" && echo 1 || echo 0)" \
    "co -r pa, field absent: /handoff-pickup is NOT added for a non-default role"

echo "== field absent: /handoff-pickup <path> keeps the resolved default =="

run_co "$H" "$REG" Convivy/acme-hq -r "$PROMPT"
assert_rc 0 "$RC" "rollover prompt, field absent: exits 0"
assert_eq "positioning-strategist" "$(_agent_value "$CLAUDE_ARGS")" \
    "rollover prompt, field absent: keeps the resolved default"
assert_eq "1" "$(_has_prompt_line "$CLAUDE_ARGS" && echo 1 || echo 0)" \
    "rollover prompt, field absent: the prompt reaches claude as its own argv element"

echo "== field set to product-manager: the default follows the registry =="

write_registry "$REG" "product-manager"

run_co "$H" "$REG" Convivy/acme-hq
assert_rc 0 "$RC" "no args, field=product-manager: exits 0"
assert_eq "product-manager" "$(_agent_value "$CLAUDE_ARGS")" \
    "no args, field=product-manager: launches product-manager"

run_co "$H" "$REG" Convivy/acme-hq ps
assert_rc 0 "$RC" "ps, field=product-manager: exits 0"
assert_eq "positioning-strategist" "$(_agent_value "$CLAUDE_ARGS")" \
    "ps, field=product-manager: still explicitly selects the strategist"

echo "== field set to product-manager: co -r with no args adds /handoff-pickup on the new default, not on another role =="

run_co "$H" "$REG" Convivy/acme-hq -r
assert_rc 0 "$RC" "co -r, no args, field=product-manager: exits 0"
assert_eq "product-manager" "$(_agent_value "$CLAUDE_ARGS")" \
    "co -r, no args, field=product-manager: launches the resolved default"
assert_eq "1" "$(_has_bare_pickup_line "$CLAUDE_ARGS" && echo 1 || echo 0)" \
    "co -r, no args, field=product-manager: /handoff-pickup was added for the default role"

run_co "$H" "$REG" Convivy/acme-hq -r ps
assert_rc 0 "$RC" "co -r ps, field=product-manager: exits 0"
assert_eq "positioning-strategist" "$(_agent_value "$CLAUDE_ARGS")" \
    "co -r ps, field=product-manager: launches the strategist, explicitly"
assert_eq "0" "$(_has_bare_pickup_line "$CLAUDE_ARGS" && echo 1 || echo 0)" \
    "co -r ps, field=product-manager: /handoff-pickup is NOT added for a non-default role"

echo "== field set to product-manager: /handoff-pickup <path> keeps the resolved (new) default =="

run_co "$H" "$REG" Convivy/acme-hq -r "$PROMPT"
assert_rc 0 "$RC" "rollover prompt, field=product-manager: exits 0"
assert_eq "product-manager" "$(_agent_value "$CLAUDE_ARGS")" \
    "rollover prompt, field=product-manager: keeps the resolved (new) default"
assert_eq "1" "$(_has_prompt_line "$CLAUDE_ARGS" && echo 1 || echo 0)" \
    "rollover prompt, field=product-manager: the prompt reaches claude as its own argv element"

echo "== a value with a slash or .. never reaches the agent-file path: falls back to positioning-strategist =="

# validate_registry refuses these at registration time; this is the SAME
# refusal bin/co itself re-checks (zsh =~) in case the on-disk registry
# bypassed that gate (a hand-edit, never run through crew_registration.register).
write_registry "$REG" "../evil"
run_co "$H" "$REG" Convivy/acme-hq
assert_rc 0 "$RC" "no args, field=../evil: exits 0"
assert_eq "positioning-strategist" "$(_agent_value "$CLAUDE_ARGS")" \
    "no args, field=../evil: falls back to positioning-strategist"

write_registry "$REG" "ops/evil"
run_co "$H" "$REG" Convivy/acme-hq
assert_rc 0 "$RC" "no args, field=ops/evil: exits 0"
assert_eq "positioning-strategist" "$(_agent_value "$CLAUDE_ARGS")" \
    "no args, field=ops/evil: falls back to positioning-strategist"

echo "== regression: a *-commercial directory still gets product-manager =="

run_co "$H" "$REG" Convivy/zeta-commercial
assert_rc 0 "$RC" "zeta-commercial: exits 0"
assert_eq "product-manager" "$(_agent_value "$CLAUDE_ARGS")" \
    "zeta-commercial: the generic *-commercial branch still defaults to product-manager"

rm -rf "$H"
finish
