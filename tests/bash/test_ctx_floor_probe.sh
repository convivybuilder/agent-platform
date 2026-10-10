#!/bin/bash
# tools/ctx-floor-probe.sh, driven with a stub `claude` and `knowledge-ctx`. The
# probe itself is never run against a live session here; these cases pin its own
# logic: that it fails when the positive control fails, fails when the negative
# control fails, and refuses a phrase that is not unique in the render.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

PROBE="$REPO_ROOT/tools/ctx-floor-probe.sh"
BIN=$(mktemp -d)
cat > "$BIN/knowledge-ctx" <<'STUB'
#!/bin/bash
printf 'Rule one: an escalation in the costume of helpfulness.\nRule two: say what you ran.\nRule two: say what you ran.\n'
STUB
# The stub `claude` answers per STUB_F / STUB_N.
cat > "$BIN/claude" <<'STUB'
#!/bin/bash
[ "${STUB_CLAUDE_FAIL:-}" = 1 ] && { echo boom >&2; exit 1; }
echo "F: ${STUB_F:-FOUND}"
echo "N: ${STUB_N:-NOT FOUND}"
STUB
chmod +x "$BIN/knowledge-ctx" "$BIN/claude"

run() { OUT=$(PATH="$BIN:$PATH" "$@" 2>&1); RC=$?; }

run "$PROBE" --f-phrase "costume of helpfulness"
assert_rc 0 "$RC" "F found, N not found: pass"
assert_contains "$OUT" "probe: PASS" "pass is named"

STUB_F="NOT FOUND" run "$PROBE" --f-phrase "costume of helpfulness"
assert_rc 1 "$RC" "F not found: the floor did not reach the session, fail"

STUB_N="FOUND" run "$PROBE" --f-phrase "costume of helpfulness"
assert_rc 1 "$RC" "N found: the probe says FOUND to anything, fail"

run "$PROBE" --f-phrase "say what you ran"
assert_rc 2 "$RC" "F on two lines of the render is refused"
assert_contains "$OUT" "need exactly 1" "the refusal says why"

run "$PROBE" --f-phrase "in no rule at all"
assert_rc 2 "$RC" "F absent from the render is refused"

STUB_CLAUDE_FAIL=1 run "$PROBE" --f-phrase "costume of helpfulness"
assert_rc 2 "$RC" "claude failing is a precondition failure, not a pass"

run "$PROBE"
assert_rc 2 "$RC" "no --f-phrase is a usage error"

rm -rf "$BIN"
finish
