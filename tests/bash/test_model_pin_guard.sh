#!/bin/bash
# Tests for tools/model-pin-guard.sh — the SessionStart guard that restores
# ~/.claude/settings.json's "model" to the ARCH-134 pin. Runs the real hook
# against a fake HOME's settings.json; no `claude` process involved.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

HOOK="$REPO_ROOT/tools/model-pin-guard.sh"
PIN='claude-opus-5-5[1m]'

# settings_model <home> -> echoes the "model" key /bin/bash "$HOOK" left behind
settings_model() {
    python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("model"))' \
        "$1/.claude/settings.json"
}

echo "== a stale pin (the pre-#705 value) is restored to $PIN =="
H1=$(mktemp -d)
mkdir -p "$H1/.claude"
printf '{"model": "claude-opus-5[1m]"}\n' > "$H1/.claude/settings.json"
HOME="$H1" /bin/bash "$HOOK" >/dev/null 2>/dev/null
assert_eq "$PIN" "$(settings_model "$H1")" "stale pin rewritten to the current PIN"
rm -rf "$H1"

echo "== a missing model key is filled in =="
H2=$(mktemp -d)
mkdir -p "$H2/.claude"
printf '{}\n' > "$H2/.claude/settings.json"
HOME="$H2" /bin/bash "$HOOK" >/dev/null 2>/dev/null
assert_eq "$PIN" "$(settings_model "$H2")" "absent model key filled with the PIN"
rm -rf "$H2"

echo "== already-pinned settings are left untouched and exit 0 silently =="
H3=$(mktemp -d)
mkdir -p "$H3/.claude"
printf '{"model": "%s"}\n' "$PIN" > "$H3/.claude/settings.json"
OUT=$(HOME="$H3" /bin/bash "$HOOK" 2>&1)
RC=$?
assert_rc 0 "$RC" "already-pinned: exits 0"
assert_eq "" "$OUT" "already-pinned: no output"
assert_eq "$PIN" "$(settings_model "$H3")" "already-pinned: unchanged"
rm -rf "$H3"

finish
