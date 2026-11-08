#!/bin/bash
# `co --resolve` for a black-box crew (platform/decisions/0043) must answer
# `crew=...` whenever `_co_private_crew`'s registry match succeeds, whatever
# the registry's own `display` field looks like.
#
# THE BUG (work item for ADR-0043's run-corpus gate; found while reading
# `crew_doctor.check_co_resolves` and bin/co's `--resolve` path, as that work
# item's own step 6 asks): `_apply_co_style`'s name gate checks `style_id`
# (always the registry's own slug, held to `_NAME_OK` at registration time)
# AND `label` (defaults to the registry's free-text `display` field) with one
# combined `||` — a BAD LABEL alone used to fail the WHOLE gate, so
# `_apply_co_style` returned without ever reaching its own `--resolve` print,
# and the private-crew branch fell through to `_co_exec_claude`'s "no crew
# resolves" answer. `crew_doctor.check_co_resolves` reads that as MISSING —
# a correctly-registered, launchable private crew reported broken because its
# `display` string happened to carry an apostrophe.
#
# THE FIX separates the two failures: a bad style_id still answers "no crew
# resolves" (there is no safe identifier to key anything on), but a bad label
# with a GOOD style_id now answers `crew=<style_id> label=<style_id>` under
# `--resolve` instead — the registry's slug substituted for its own display,
# since the slug is already proven gate-clean. A REAL (non-resolve) launch is
# unchanged: still no OSC, no tmux write, nothing printed for that label.
set -u
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
. "$HERE/lib.sh"

CO="$REPO_ROOT/bin/co"

setup() { # -> echoes fake HOME
    local home
    home=$(mktemp -d)
    mkdir -p "$home/stubbin" "$home/Convivy/nightjar"
    echo "$home"
}

run_resolve_in() { # <home> <rel> -> RC, OUT
    local home=$1 rel=$2
    local out_log; out_log=$(mktemp)
    ( cd "$home/$rel" && HOME="$home" PATH="$home/stubbin:$PATH" \
      /bin/zsh "$CO" --root-config "$(co_root_config "$home" "$REGISTRY_OVERRIDE")" \
          --resolve >"$out_log" 2>/dev/null )
    RC=$?
    OUT=$(cat "$out_log")
    rm -f "$out_log"
}

echo "== a private crew whose display carries an apostrophe still resolves =="
H=$(setup)
write_private_registry "$H/registry.json" nightjar "Nightjar's Desk"
REGISTRY_OVERRIDE="$H/registry.json" run_resolve_in "$H" "Convivy/nightjar"
assert_eq "0" "$RC" "nightjar: co --resolve exits 0"
assert_contains "$OUT" "crew=nightjar" \
    "nightjar: co --resolve still names the crew despite the bad-charset display"
rm -rf "$H"

echo "== a private crew with a clean display still reports that display as the label =="
H=$(setup)
write_private_registry "$H/registry.json" nightjar "Nightjar Desk"
REGISTRY_OVERRIDE="$H/registry.json" run_resolve_in "$H" "Convivy/nightjar"
assert_eq "0" "$RC" "nightjar (clean display): co --resolve exits 0"
assert_contains "$OUT" "crew=nightjar label=Nightjar Desk" \
    "nightjar (clean display): the clean display is still used as the label, unchanged"
rm -rf "$H"

finish
