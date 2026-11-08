#!/bin/bash
# Tests for `_co_apply_product_style`/`projects.co_style()` resolving a raw
# basename against a unit's `repos` list, not only its own slug (round 2,
# reviewer, PR #991 — non-blocking, substance).
#
# THE GAP. `_co_apply_product_style`'s two callers (the `-commercial` branch,
# after stripping that suffix, and the generic daytime fallback's own styling
# arm) hand `projects.co_style()` a bare git-toplevel basename. That basename
# may name a SECONDARY repo under some unit's `repos` rather than the unit's
# own slug or home-directory basename — a real product's own secondary repo
# on origin/main, which a hardcoded case gave that product's own colour
# explicitly before this PR's registry cutover dropped it. An exact-slug-only
# lookup falls through to the unstyled grey fallback for every name like that.
#
# A MADE-UP REGISTRY, not the operator's own checked-in config/projects.json:
# no real crew in this file's reach has a secondary repo whose basename
# differs from both its slug and its home directory, so this fixture invents
# one (`acme`, with a secondary repo `acme-sandbox-tools`) rather than
# borrowing a real crew's name for a case the real registry does not carry.
#
# `--resolve` is the harness: it answers which crew (if any) `_apply_co_style`
# resolved, without starting a session (see `_co_exec_claude`'s own comment),
# and a bare launch's OSC 1337 `SetProfile=co-<style_id>` line is read the
# same way `test_co_resolve_adopter_crew.sh` already reads it.
#
# `co` is zsh (excluded from the ARCH-082 bash-3.2 contract by lint.sh's
# shebang filter); bash invoking it here is exactly how a login shell will.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

CO="$REPO_ROOT/bin/co"

# A registry naming one synthetic crew, `acme`, whose `repos` lists both its
# own slug and a secondary checkout basename, `acme-sandbox-tools` — the
# same shape a real product's own row carries for its own secondary repo in
# the operator's checked-in registry, reproduced here with a made-up name.
write_secondary_repo_registry() { # <path>
    cat > "$1" <<'JSON'
{
  "acme": {
    "class": "project", "display": "Acme", "owner": "convivy",
    "home_dir": "<root>/acme", "dirs": [], "crew": "acme",
    "lifecycle": "active", "products": [],
    "repos": ["acme", "acme-sandbox-tools"], "aliases": [], "kb": "local",
    "co_accent": "#112233", "co_fg": "#445566"
  }
}
JSON
}

setup() { # -> echoes fake HOME
    local home
    home=$(mktemp -d)
    mkdir -p "$home/stubbin"
    write_secondary_repo_registry "$home/secondary-repo-registry.json"

    # acme-sandbox-tools: a secondary checkout of acme's repo, basename
    # differs from acme's own slug AND its home-directory basename
    # (`<root>/acme`) — already individuated, so this reaches the styling
    # arm on a bare launch, the same fixture shape test_co_resolve_adopter_crew.sh
    # uses for its own adopter-crew case.
    mkdir -p "$home/Convivy/acme-sandbox-tools/.claude/agents"
    : > "$home/Convivy/acme-sandbox-tools/.claude/agents/planning-orchestrator.md"
    ( cd "$home/Convivy/acme-sandbox-tools" && git init -q . )

    # The control: a basename no unit's repos list names at all.
    mkdir -p "$home/Convivy/acme-unheard-of/.claude/agents"
    : > "$home/Convivy/acme-unheard-of/.claude/agents/planning-orchestrator.md"
    ( cd "$home/Convivy/acme-unheard-of" && git init -q . )

    cat > "$home/stubbin/claude" <<'EOF'
#!/bin/bash
printf '%s\n' "$@" > "$CLAUDE_CALL_LOG"
exit 0
EOF
    chmod +x "$home/stubbin/claude"
    echo "$home"
}

# run_co <home> <dir-relative-to-home> [co-args...] -> RC, CLAUDE_ARGS, OUT, ERR
run_co() {
    local home=$1 rel=$2; shift 2
    local call_log out_log err_log
    call_log=$(mktemp); out_log=$(mktemp); err_log=$(mktemp)
    # TMUX/TMUX_PANE forced empty (test_co_resolve_adopter_crew.sh's own
    # reasoning): a bash test must never touch a real tmux server, and this
    # makes `_apply_co_style` take its deterministic non-tmux OSC branch.
    ( cd "$home/$rel" && HOME="$home" PATH="$home/stubbin:$PATH" \
      CLAUDE_CALL_LOG="$call_log" TMUX="" TMUX_PANE="" \
      /bin/zsh "$CO" \
      --root-config "$(co_root_config "$home" "$home/secondary-repo-registry.json")" \
      "$@" >"$out_log" 2>"$err_log" )
    RC=$?
    CLAUDE_ARGS=$(cat "$call_log"); OUT=$(cat "$out_log"); ERR=$(cat "$err_log")
    rm -f "$call_log" "$out_log" "$err_log"
}

H=$(setup)

echo "== co --resolve names the owning unit, not the secondary-repo basename =="
run_co "$H" Convivy/acme-sandbox-tools --resolve
assert_rc 0 "$RC" "acme-sandbox-tools: co --resolve exits 0"
assert_contains "$OUT" "crew=acme " "acme-sandbox-tools: resolves to acme, the owning unit's own slug"

echo "== bare co applies that unit's style to the secondary-repo basename =="
run_co "$H" Convivy/acme-sandbox-tools
assert_rc 0 "$RC" "acme-sandbox-tools: bare co launches"
assert_contains "$OUT" "SetProfile=co-acme" \
    "acme-sandbox-tools: the OSC profile uses acme's own style_id, not the basename"
# `SetProfile=co-acme` alone is also a substring of the UNFIXED, basename-keyed
# profile (`co-acme-sandbox-tools`), so the positive check above cannot tell
# the two apart on its own — this rules out the unfixed form explicitly.
assert_eq "0" "$(printf '%s' "$OUT" | grep -c 'SetProfile=co-acme-sandbox-tools' || true)" \
    "acme-sandbox-tools: NOT the basename-keyed profile id"

echo "== a basename no unit's repos list names keeps the generic slate fallback =="
# `_co_apply_product_style` still returns 1 for this name (no unit claims it),
# so the generic `*)` arm's OWN fallback fires — slate grey, keyed to the raw
# basename itself (bin/co's `#5b6b7a` case) — rather than picking up acme's
# style for a name acme's `repos` never listed.
run_co "$H" Convivy/acme-unheard-of
assert_rc 0 "$RC" "acme-unheard-of: bare co still launches"
assert_contains "$OUT" "SetProfile=co-acme-unheard-of" \
    "acme-unheard-of: styled generically off its own basename, not acme's"

rm -rf "$H"
finish
