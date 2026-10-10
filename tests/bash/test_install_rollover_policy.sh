#!/bin/bash
# install-rollover-policy seeds ~/.config/convivy/rollover.json (ADR-0069), once.
# ROLLOVER_POLICY_FILE overrides the seeded path so this never touches a real HOME.
set -u
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

SCRIPT="$REPO_ROOT/bin/install-rollover-policy"

echo "== a missing file is seeded with the observe-mode defaults =="
D=$(mktemp -d)
FILE="$D/rollover.json"
OUT=$(ROLLOVER_POLICY_FILE="$FILE" sh "$SCRIPT" 2>&1)
RC=$?
assert_rc 0 "$RC" "install-rollover-policy exits 0 seeding a fresh file"
assert_eq 1 "$([ -f "$FILE" ] && echo 1 || echo 0)" "the file was created"
assert_contains "$(cat "$FILE")" '"mode": "observe"' "seeded in observe mode"
assert_contains "$(cat "$FILE")" '"seek_pct": 40' "seeded with the 40% seek threshold"
assert_contains "$(cat "$FILE")" '"floor_pct": 50' "seeded with the 50% floor"
assert_contains "$(cat "$FILE")" '"seek_call_cap": null' "seek_call_cap starts null pending its own measurement"
assert_contains "$OUT" "seeded" "the run says what it did"
rm -rf "$D"

echo "== an existing file, even a deliberately empty one, is never overwritten =="
D=$(mktemp -d)
FILE="$D/rollover.json"
mkdir -p "$D"
printf '{}' > "$FILE"
OUT=$(ROLLOVER_POLICY_FILE="$FILE" sh "$SCRIPT" 2>&1)
RC=$?
assert_rc 0 "$RC" "install-rollover-policy exits 0 against an existing file"
assert_eq '{}' "$(cat "$FILE")" "the operator's file is left byte-for-byte alone"
assert_contains "$OUT" "already exists" "the run says it left it alone"
rm -rf "$D"

echo "== re-running after a real seed is idempotent =="
D=$(mktemp -d)
FILE="$D/rollover.json"
ROLLOVER_POLICY_FILE="$FILE" sh "$SCRIPT" >/dev/null 2>&1
FIRST=$(cat "$FILE")
ROLLOVER_POLICY_FILE="$FILE" sh "$SCRIPT" >/dev/null 2>&1
assert_eq "$FIRST" "$(cat "$FILE")" "a second run changes nothing"
rm -rf "$D"

finish
