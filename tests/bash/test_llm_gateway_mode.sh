#!/bin/bash
# Behavioral test for gateway/llm-gateway-run.sh's secrets-file mode read: the
# 4-digit-to-3-digit normalisation and the 600/400 accept-or-refuse decision
# (PR #633, round 2, reviewer finding 3). The watchdog's identical BSD-vs-GNU
# `stat` fix got a real stub-based end-to-end test; this one had only a
# source-text substring check (test_gateway_config.py::
# test_launcher_refuses_a_loose_secrets_file), which cannot catch a wrong
# normalisation or a wrong accept/refuse call.
#
# This drives _gateway_normalise_mode directly with raw octal strings — the
# exact text `stat -c '%a'`/`stat -f '%Lp'` would print — rather than through a
# stubbed `stat` binary, because the guard's whole job is refusing a
# world-readable secrets file and a stat-path seam would let a fake stat that
# prints 600 defeat it. The launcher is sourceable for exactly this function
# without running its main body (see the BASH_SOURCE guard in the launcher);
# the live path always calls the function with output from the hardcoded
# /usr/bin/stat, never through this test.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

LAUNCHER="$REPO_ROOT/gateway/llm-gateway-run.sh"

# shellcheck source=/dev/null
. "$LAUNCHER"

check() { # <raw> <expected perms> <expected rc: 0 accept, 1 refuse>
    local raw=$1 want_perms=$2 want_rc=$3 got_perms got_rc
    got_perms=$(_gateway_normalise_mode "$raw")
    got_rc=$?
    assert_eq "$want_perms" "$got_perms" "stat text '$raw' normalises to '$want_perms'"
    assert_eq "$want_rc" "$got_rc" "stat text '$raw' (mode $want_perms) returns rc $want_rc"
}

echo "== three-digit modes pass through unchanged =="
check "600" "600" 0
check "400" "400" 0
check "644" "644" 1

echo "== four-digit modes drop the special bit, then decide on what's left =="
check "2600" "600" 0   # setgid + 0600 — equally private, accepted
check "4600" "600" 0   # setuid + 0600 — equally private, accepted
check "1600" "600" 0   # sticky + 0600 — equally private, accepted
check "7777" "777" 1   # every bit set — world-readable, refused

echo "== unparsable text is neither accepted nor refused outright — it signals retry =="
_out=$(_gateway_normalise_mode "")
_rc=$?
assert_eq "" "$_out" "empty stat output normalises to empty"
assert_eq "2" "$_rc" "empty stat output returns 2 (caller retries the other stat form)"

# The GNU-vs-BSD trap itself: a filesystem dump, not a mode, landing in the
# variable. `_gateway_normalise_mode` must reject it rather than pass any of
# its digits through as a mode.
_dump="1234567 512 4294967295 100 100 0 9092755 2 0 0 100000 100000"
_out=$(_gateway_normalise_mode "$_dump")
_rc=$?
assert_eq "" "$_out" "a filesystem-dump string normalises to empty, not a false mode"
assert_eq "2" "$_rc" "a filesystem-dump string returns 2 (caller retries), not 0 or 1"

finish
