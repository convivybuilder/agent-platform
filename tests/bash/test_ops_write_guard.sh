#!/bin/bash
# tests/bash/test_ops_write_guard.sh — the ops-write guard is complete and it
# FAILS, not just records.
#
# The python suite's `_no_live_ops_writes` autouse fixture fails any test that
# reaches the live ops plane. The bash suite's equivalent is two halves: the
# mocks/ doubles shadow every plane-writing client a test points PYTHONPATH at
# them for (signal_client, issue_client, decision_client, notify — enumerated
# in this file's own `for client in ...` loop below and in mocks/_mock_base.py's
# docstring), and run.sh fails a test whose exercised code path leaves an
# unacknowledged write in OVERNIGHT_OPS_GUARD. These tests pin both halves,
# including the acceptance property this issue came from: a live emit inlined
# into a bin script fails the suite rather than posting a row to the plane.
#
# Run under /bin/bash (3.2.57), per ARCH-082.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

# The bash CI job (ci.yml) has no setup-python, so the interpreter must be
# found on PATH. An absent one FAILS the test rather than skipping it: a
# skipped meta-test is an unverified guard, the exact hole this file closes.
PY=$(command -v python3.11 || command -v python3)
[ -n "$PY" ] || {
    echo "  FAIL: no python3.11 or python3 on PATH — cannot verify the ops-write guard"
    _FAIL=$((_FAIL + 1))
    finish
    exit 1
}

# Resolve an inline write the way a bash-exercised script's inline python block
# would: the same PYTHONPATH indirection ($_BASH_TEST_MOCKS ahead of the
# installed lib), the same interpreter, the same swallowed exit code.
plane_write() { # <python-code>
    PYTHONPATH="$_BASH_TEST_MOCKS:$HOME/.local/lib/agent-platform" "$PY" -c "$1" >/dev/null 2>&1
    return 0
}

echo "-- every plane-writing client resolves to the double --"
for client in signal_client issue_client decision_client notify; do
    resolved=$(PYTHONPATH="$_BASH_TEST_MOCKS:$HOME/.local/lib/agent-platform" \
        "$PY" -c "import $client; print($client.__file__)" 2>/dev/null)
    assert_contains "$resolved" "$_BASH_TEST_MOCKS/$client.py" \
        "$client resolves to the mock, not the installed lib"
done

# The deliberate writes below must land in THIS test's guard file, not the one
# the parent run.sh is watching — otherwise this test would fail itself.
guard=$(mktemp)
export OVERNIGHT_OPS_GUARD="$guard"

echo "-- an unacknowledged write lands in the guard file --"
plane_write 'import signal_client; signal_client.post_alert("guard-probe", "unanticipated signal write")'
assert_contains "$(cat "$guard")" '"module": "signal_client"' \
    "an unacknowledged signal write names its module"
assert_contains "$(cat "$guard")" '"fn": "post_alert"' \
    "and the call"

plane_write 'import issue_client; issue_client.comment_on_issue("issue_probe", "probe note")'
assert_contains "$(cat "$guard")" '"module": "issue_client"' \
    "an unacknowledged issue write names its module"
assert_contains "$(cat "$guard")" '"fn": "comment_on_issue"' \
    "and the call"

plane_write 'import decision_client; decision_client.post_decision("agent-platform", "proposal", "probe")'
assert_contains "$(cat "$guard")" '"module": "decision_client"' \
    "an unacknowledged decision write names its module"

plane_write 'import notify; notify.push("budget-hard-cap", "probe push")'
assert_contains "$(cat "$guard")" '"module": "notify"' \
    "an unacknowledged push names its module"
assert_contains "$(cat "$guard")" '"fn": "push"' \
    "and the call"

plane_write 'import signal_client; signal_client.some_future_write("z")'
assert_contains "$(cat "$guard")" '"fn": "some_future_write"' \
    "a client function this double has no stub for is still captured"

echo "-- an acknowledged write goes to OVERNIGHT_MOCK_LOG, not the guard --"
ack=$(mktemp)
: > "$guard"   # only the acknowledged write below could land in the guard
OVERNIGHT_MOCK_LOG="$ack" plane_write \
    'import signal_client; signal_client.post_alert("ack-probe", "y")'
assert_contains "$(cat "$ack")" "ack-probe" \
    "an acknowledged write is recorded to the mock log"
assert_eq "" "$(cat "$guard")" \
    "an acknowledged write does NOT trip the guard"
rm -f "$ack"

echo "-- run.sh fails a test whose code path inlines a plane write --"
tmp=$(mktemp -d)
cp "$HERE/run.sh" "$tmp/run.sh"
chmod +x "$tmp/run.sh"
cat > "$tmp/test_plane_writer.sh" <<EOF
#!/bin/bash
# A test that inlines a live emit the way the 2026-07-29 defect did: the
# emitting function is NOT stubbed, so the block runs and the write must be
# caught by the guard rather than reaching the plane.
PYTHONPATH="$_BASH_TEST_MOCKS:\$HOME/.local/lib/agent-platform" \\
    "$PY" -c \\
    'import signal_client; signal_client.post_alert("inline-live-emit", "boom")' \\
    >/dev/null 2>&1
exit 0
EOF
chmod +x "$tmp/test_plane_writer.sh"
/bin/bash "$tmp/run.sh" > "$tmp/out.log" 2>&1
assert_rc 1 "$?" "run.sh FAILS a test that inlines a plane write"
assert_contains "$(cat "$tmp/out.log")" "unacknowledged operating-plane write" \
    "and names the guard as the reason"
assert_contains "$(cat "$tmp/out.log")" "inline-live-emit" \
    "and shows the intercepted write"
rm -rf "$tmp"

rm -f "$guard"
finish
