#!/bin/bash
# Tests for bin/co's main-loop model: Opus unless `_co_sonnet_crews` names the
# crew (operator, 2026-09-16). The default was once Sonnet, reached Opus only through
# three lists, and let placard, shipwright and Signals each launch on Sonnet
# unnoticed; the first case below is that Signals directory.
#
# The remaining cases are the directories that used to need a special path to
# reach Opus (a bare directory with no git toplevel, a -commercial repo, the
# capital-CFO directory, a private crew's subfolder), the listed Sonnet crews,
# and the two overrides.
#
# `co` is zsh (excluded from the ARCH-082 bash-3.2 contract by lint.sh's
# shebang filter); bash invoking it here is exactly how a login shell will.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

CO="$REPO_ROOT/bin/co"
OPUS='claude-opus-5-5[1m]'
SONNET='claude-sonnet-5[1m]'

# A fake HOME holding all six targets, each git-initialized (except `ask`,
# deliberately not a repo) and individuated with a dummy agent so a
# non-individuated generic-tail fallthrough can't be confused with the pin
# actually taking effect. Echoes the fake HOME.
setup() { # -> echoes fake HOME
    local home d
    home=$(mktemp -d)
    mkdir -p "$home/stubbin"

    for d in Convivy/library Convivy/agent-platform Convivy/convivy-orient \
             Convivy/CFO; do
        mkdir -p "$home/$d/.claude/agents"
        : > "$home/$d/.claude/agents/scout.md"
        ( cd "$home/$d" && git init -q . )
    done
    # convivy-commercial hardcodes agent=positioning-strategist (bin/co ~line 679).
    mkdir -p "$home/Convivy/convivy-commercial/.claude/agents"
    : > "$home/Convivy/convivy-commercial/.claude/agents/positioning-strategist.md"
    ( cd "$home/Convivy/convivy-commercial" && git init -q . )
    # `ask` is deliberately NOT a git repo — that absence is the case this
    # file exists to cover. `EA` is the same shape and is here for the same
    # reason: it is a bare directory on disk, so an entry in `_co_crew_dirs`
    # would be unreachable code and the pin would silently not apply.
    mkdir -p "$home/Convivy/ask"
    mkdir -p "$home/Convivy/EA"
    # The operator's own private crew IS a git repo, as it is on disk (synthesized
    # here as "private-a" against a fixture registry, below), so the `$PWD` pin
    # must win over a toplevel whose basename the brand `case` does not list.
    mkdir -p "$home/Convivy/private-a"
    ( cd "$home/Convivy/private-a" && git init -q . )
    write_private_registry "$home/synthetic-private-registry.json" private-a private-a.example

    cat > "$home/stubbin/claude" <<'EOF'
#!/bin/bash
printf '%s\n' "$@" > "$CLAUDE_CALL_LOG"
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

# run_co_in <home> <dir-relative-to-home> -> sets RC, CLAUDE_ARGS
#
# `unset CO_DEFAULT_MODEL` first: the operator's ~/.zshenv exports it as the operator's
# machine-global override, and since it is exported it survives into this
# subshell's environment same as any other inherited var — a `VAR=val cmd`
# prefix only ADDS variables for that command, it does not strip an
# already-exported one. Left inherited, every assert_opus/assert_sonnet below
# would pass by riding CO_DEFAULT_MODEL's value rather than by exercising
# `_co_sonnet_crews`, the thing this file exists to pin (silently, once
# CO_DEFAULT_MODEL happens to equal $OPUS, which is exactly the failure mode
# that hid 13 cases failing locally while CI, with no zshenv sourced, stayed
# green).
run_co_in() {
    local home=$1 rel=$2
    local call_log root_config
    call_log=$(mktemp)
    if [ -n "${REGISTRY_OVERRIDE:-}" ]; then
        root_config=$(co_root_config "$home" "$REGISTRY_OVERRIDE")
    else
        root_config=$(co_root_config "$home")
    fi
    ( cd "$home/$rel" && unset CO_DEFAULT_MODEL && HOME="$home" PATH="$home/stubbin:$PATH" \
      CLAUDE_CALL_LOG="$call_log" /bin/zsh "$CO" --root-config "$root_config" >/dev/null 2>&1 )
    RC=$?
    CLAUDE_ARGS=$(cat "$call_log")
    rm -f "$call_log"
}

# The stub records one argv element per line (`printf '%s\n' "$@"`), so
# `--model` and its value are on TWO ADJACENT lines, never one combined
# string — checked with awk rather than a substring grep, which would also
# pass on values from two unrelated flags landing next to each other by luck.
_model_value() { # <argv-log-text> -> echoes the value following --model, or ""
    printf '%s\n' "$1" | awk '$0=="--model"{getline; print; exit}'
}

assert_opus() { # <label> <dir-relative-to-home> <home>
    local label=$1 rel=$2 home=$3
    run_co_in "$home" "$rel"
    assert_eq "$OPUS" "$(_model_value "$CLAUDE_ARGS")" "$label: launches on Opus"
}

assert_sonnet() { # <label> <dir-relative-to-home> <home>
    local label=$1 rel=$2 home=$3
    run_co_in "$home" "$rel"
    assert_eq "$SONNET" "$(_model_value "$CLAUDE_ARGS")" "$label: launches on Sonnet"
}

H=$(setup)
echo "== a crew nobody registered or listed launches on Opus =="
# THE CASE THIS CHANGE EXISTS FOR. Signals' `sig-` directory was in no list and
# no registry, and on the old tree it fell to the silent Sonnet `*)`.
mkdir -p "$H/Convivy/sig-signals/.claude/agents"
: > "$H/Convivy/sig-signals/.claude/agents/planning-orchestrator.md"
( cd "$H/Convivy/sig-signals" && git init -q . )
assert_opus "sig-signals (unregistered, unlisted)" "Convivy/sig-signals" "$H"

echo "== directories that once needed their own path to Opus still get it =="
assert_opus "library"            "Convivy/library"            "$H"
assert_opus "agent-platform"     "Convivy/agent-platform"     "$H"
assert_opus "convivy-orient"     "Convivy/convivy-orient"     "$H"
assert_opus "ask (no git toplevel)" "Convivy/ask"             "$H"
assert_opus "EA (no git toplevel)"  "Convivy/EA"              "$H"
assert_opus "convivy-commercial" "Convivy/convivy-commercial" "$H"
assert_opus "CFO"                "Convivy/CFO"                "$H"
REGISTRY_OVERRIDE="$H/synthetic-private-registry.json"
assert_opus "private-a"          "Convivy/private-a"          "$H"
mkdir -p "$H/Convivy/private-a/Back" "$H/Convivy/EA/2026"
( cd "$H/Convivy/private-a/Back" && git init -q . )
assert_opus "private-a/Back (private subfolder)" "Convivy/private-a/Back" "$H"
unset REGISTRY_OVERRIDE
assert_opus "EA/2026 (private subfolder)"        "Convivy/EA/2026"        "$H"

echo "== the crews listed in _co_sonnet_crews launch on Sonnet =="
mkdir -p "$H/Convivy/write-better/.claude/agents" \
         "$H/Convivy/nyh-architecture/nyh-etc/.claude/agents"
: > "$H/Convivy/write-better/.claude/agents/planning-orchestrator.md"
: > "$H/Convivy/nyh-architecture/nyh-etc/.claude/agents/planning-orchestrator.md"
( cd "$H/Convivy/write-better" && git init -q . )
( cd "$H/Convivy/nyh-architecture/nyh-etc" && git init -q . )
assert_sonnet "write-better" "Convivy/write-better" "$H"
assert_sonnet "nyh-etc (nested under nyh-architecture)" "Convivy/nyh-architecture/nyh-etc" "$H"

echo "== an explicit model still wins, on either tier =="
call_log=$(mktemp)
( cd "$H/Convivy/sig-signals" && HOME="$H" PATH="$H/stubbin:$PATH" CLAUDE_CALL_LOG="$call_log" \
  /bin/zsh "$CO" --root-config "$(co_root_config "$H")" -m sonnet >/dev/null 2>&1 )
assert_eq "sonnet" "$(_model_value "$(cat "$call_log")")" "sig-signals -m sonnet: the flag wins"
( cd "$H/Convivy/write-better" && HOME="$H" PATH="$H/stubbin:$PATH" CLAUDE_CALL_LOG="$call_log" \
  CO_DEFAULT_MODEL=claude-haiku-4-5 /bin/zsh "$CO" --root-config "$(co_root_config "$H")" >/dev/null 2>&1 )
assert_eq "claude-haiku-4-5" "$(_model_value "$(cat "$call_log")")" \
    "write-better with CO_DEFAULT_MODEL: the machine-global override wins"
( cd "$H/Convivy/private-a/Back" && HOME="$H" PATH="$H/stubbin:$PATH" CLAUDE_CALL_LOG="$call_log" \
  /bin/zsh "$CO" --root-config "$(co_root_config "$H" "$H/synthetic-private-registry.json")" -m sonnet >/dev/null 2>&1 )
assert_eq "sonnet" "$(_model_value "$(cat "$call_log")")" "private-a/Back -m sonnet: the flag wins"
rm -f "$call_log"
rm -rf "$H"

finish
