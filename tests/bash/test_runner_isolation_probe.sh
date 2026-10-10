#!/bin/bash
# Tests for scripts/ci/runner-isolation-probe.sh (CQ-417).
#
# netstat, lsof, curl and nc are ALL stubbed on PATH — no real network call
# ever happens in this test (the real script hits loopback ports). `ls` is
# stubbed for three paths: the operator's home and its `.claude` (named by
# the OPERATOR_HOME env var the probe reads; this suite points it at a
# synthetic fixture, never the real operator's data) and /tmp/cc-socks
# (hardcoded in the probe, so there is no way to parameterize it from a
# test). Stubbing the command is the only way to exercise both the
# "refused" and "readable" branches safely for all three.
#
# Item 5's own-home check reads the mode with `stat`, which reports a path's
# real mode to its own owner regardless of that mode's group/other bits, so
# it runs against a real mktemp fixture with real chmod. Its other-runner-home
# check instead calls `ls`, and this test process owns that fixture too — an
# owner always keeps rwx on its own directory no matter what group/other
# chmod says, so `ls` would succeed at every mode and could never exercise
# the "unreadable" branch. That one stays a stub, keyed on the fixture's
# path, same as the three hardcoded operator-data paths below.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

PROBE="$REPO_ROOT/scripts/ci/runner-isolation-probe.sh"
BIN=$(mktemp -d)
CURL_LOG="$BIN/.curl.log"

# netstat stub: one canned `-anv -p tcp` table, ignoring its arguments,
# unless STUB_NETSTAT_EMPTY=yes (reproduces the round-1 defect: on the real
# runner's macOS build, netstat run as ap-runner enumerated zero rows even
# though real listeners existed). Ports: 4000 (LiteLLM, known/multi-route),
# 8100 (Library, known), 9999 (unrecognised — exercises the "/" fallback),
# 8325 (inside the tennis app's 8321-8330 range — exercises the
# numeric-range branch). 54321 is ESTABLISHED, not LISTEN, and must never be
# probed.
cat > "$BIN/netstat" <<'STUB'
#!/bin/bash
if [ "${STUB_NETSTAT_EMPTY:-}" = "yes" ]; then
    cat <<'EMPTY'
Active Internet connections (including servers)
Proto Recv-Q Send-Q  Local Address          Foreign Address        (state)
EMPTY
    exit 0
fi
cat <<'TABLE'
Active Internet connections (including servers)
Proto Recv-Q Send-Q  Local Address          Foreign Address        (state)    rhiwat  shiwat    pid   epid  state       options
tcp4       0      0  127.0.0.1.4000         *.*                    LISTEN      131072  131072    100      0 0x100 0x6
tcp4       0      0  127.0.0.1.8100         *.*                    LISTEN      131072  131072    101      0 0x100 0x6
tcp4       0      0  *.9999                 *.*                    LISTEN      131072  131072    102      0 0x100 0x6
tcp4       0      0  127.0.0.1.8325         *.*                    LISTEN      131072  131072    103      0 0x100 0x6
tcp4       0      0  127.0.0.1.54321        10.0.0.5.443           ESTABLISHED 131072  131072    104      0 0x100 0x6
TABLE
STUB

# lsof stub: by default finds nothing (keeps every existing assertion keyed
# to netstat's canned ports only); STUB_LSOF_EXTRA=yes adds one listener
# (port 7777) that netstat does NOT report, to exercise the "extra listener
# found only by lsof, outside the fixed list" path.
cat > "$BIN/lsof" <<'STUB'
#!/bin/bash
if [ "${STUB_LSOF_EXTRA:-}" = "yes" ]; then
    cat <<'TABLE'
COMMAND   PID   USER   FD   TYPE DEVICE SIZE/OFF NODE NAME
python   12345 ap-runner  5u  IPv4 0x1          0t0  TCP 127.0.0.1:7777 (LISTEN)
TABLE
fi
exit 0
STUB

# curl stub: logs every invocation's URL (last arg) to CURL_LOG, and proves
# the real script's `-o /dev/null` is what keeps a response body out of this
# script's own stdout — it prints a body marker ONLY when `-o /dev/null` is
# missing from the argv it was called with, which never happens through the
# real script. STUB_CURL_RC controls curl's own exit code (connection
# refused vs reachable).
cat > "$BIN/curl" <<STUB
#!/bin/bash
printf '%s\n' "\$*" >> "$CURL_LOG"
has_o_devnull=0
has_w=0
prev=""
for a in "\$@"; do
    if [ "\$prev" = "-o" ] && [ "\$a" = "/dev/null" ]; then has_o_devnull=1; fi
    [ "\$a" = "-w" ] && has_w=1
    prev="\$a"
done
if [ "\$has_o_devnull" -ne 1 ]; then
    echo "SECRET_RESPONSE_BODY_LEAK"
fi
if [ "\$has_w" -eq 1 ]; then
    echo "\${STUB_CURL_STATUS:-200} \${STUB_CURL_BYTES:-42}"
fi
exit "\${STUB_CURL_RC:-0}"
STUB

# ls stub: per-path pass/fail switches, default refuse (exit 1) — the safe
# default a forgotten switch should fail toward. The other-runner-home path
# is not fixed, so it's matched by value against STUB_LS_OTHER_HOME_PATH
# (set per-test, read here at run time, not at this heredoc's creation
# time). Any other path falls through to the real /bin/ls — nothing else
# this script probes resolves outside this test's own mktemp fixtures.
cat > "$BIN/ls" <<'STUB'
#!/bin/bash
case "$1" in
    "$OPERATOR_HOME") [ "${STUB_LS_HOME:-}" = "ok" ] && exit 0 || exit 1 ;;
    "$OPERATOR_HOME/.claude") [ "${STUB_LS_CLAUDE:-}" = "ok" ] && exit 0 || exit 1 ;;
    /tmp/cc-socks) [ "${STUB_LS_SOCKS:-}" = "ok" ] && exit 0 || exit 1 ;;
    "${STUB_LS_OTHER_HOME_PATH:-}") [ "${STUB_LS_OTHER_HOME:-}" = "ok" ] && exit 0 || exit 1 ;;
    *) exec /bin/ls "$@" ;;
esac
STUB

# nc stub: the Postgres unix-socket connect check.
cat > "$BIN/nc" <<'STUB'
#!/bin/bash
[ "${STUB_NC_PG:-}" = "ok" ] && exit 0 || exit 1
STUB

chmod +x "$BIN/netstat" "$BIN/lsof" "$BIN/curl" "$BIN/ls" "$BIN/nc"

# Item-5 fixtures: a fake /Users tree, entirely under this mktemp dir, never
# the real /Users. HOMES/ap-runner is the job user's own home (700 by
# default, the safe case); RUNNER_HOMES_ROOT points the probe at HOMES
# instead of the real /Users so its `*-runner` glob only ever sees fixtures.
# HOMES/operator-home is the synthetic account OPERATOR_HOME names for item
# d, never the real operator's home.
HOMES=$(mktemp -d)
OWN_HOME="$HOMES/ap-runner"
OPERATOR_HOME="$HOMES/operator-home"
mkdir -p "$OWN_HOME" "$OPERATOR_HOME"
chmod 700 "$OWN_HOME"

run() {
    : > "$CURL_LOG"
    OUT=$(PATH="$BIN:$PATH" HOME="${TEST_HOME:-$OWN_HOME}" RUNNER_HOMES_ROOT="$HOMES" \
        OPERATOR_HOME="$OPERATOR_HOME" /bin/bash "$PROBE" 2>&1)
    RC=$?
}

# A separate path for literally unsetting $HOME: a plain assignment can only
# set or empty it, never remove it from the environment bash's `set -u`
# then sees as absent.
run_with_home_unset() {
    : > "$CURL_LOG"
    OUT=$(env -u HOME PATH="$BIN:$PATH" RUNNER_HOMES_ROOT="$HOMES" \
        OPERATOR_HOME="$OPERATOR_HOME" /bin/bash "$PROBE" 2>&1)
    RC=$?
}

# Same split for $OPERATOR_HOME: CI sets it from a repository variable
# (`vars.OPERATOR_HOME` in ci.yml), which is unset on a fork PR or a
# workflow run predating that variable's creation, so both "never set" and
# "set to empty" need their own path, neither reachable through a plain
# assignment alone.
run_with_operator_home_unset() {
    : > "$CURL_LOG"
    OUT=$(env -u OPERATOR_HOME PATH="$BIN:$PATH" HOME="${TEST_HOME:-$OWN_HOME}" \
        RUNNER_HOMES_ROOT="$HOMES" /bin/bash "$PROBE" 2>&1)
    RC=$?
}

run_with_operator_home_empty() {
    : > "$CURL_LOG"
    OUT=$(PATH="$BIN:$PATH" HOME="${TEST_HOME:-$OWN_HOME}" RUNNER_HOMES_ROOT="$HOMES" \
        OPERATOR_HOME="" /bin/bash "$PROBE" 2>&1)
    RC=$?
}

# --- all isolation checks refuse: PASS, exit 0 ---
run
assert_rc 0 "$RC" "every check refuses: probe passes"
assert_contains "$OUT" "ISOLATION PROBE: PASS" "pass is named"
assert_contains "$OUT" "ok: $OPERATOR_HOME is not listable" "home-dir refusal reported"
assert_contains "$OUT" "ok: /tmp/.s.PGSQL.5432 refused or absent" "postgres-socket refusal reported"

# --- a readable path fails the run ---
STUB_LS_HOME=ok run
assert_rc 1 "$RC" "a listable home dir fails the run"
assert_contains "$OUT" "FAIL: $OPERATOR_HOME is listable by ap-runner" "the specific failure is named"
assert_contains "$OUT" "ISOLATION PROBE: FAIL" "fail is named"

STUB_LS_CLAUDE=ok run
assert_rc 1 "$RC" "a readable .claude fails the run"
assert_contains "$OUT" "FAIL: $OPERATOR_HOME/.claude is readable by ap-runner" "the specific failure is named"

STUB_LS_SOCKS=ok run
assert_rc 1 "$RC" "a listable /tmp/cc-socks fails the run"
assert_contains "$OUT" "FAIL: /tmp/cc-socks is listable by ap-runner" "the specific failure is named"

# --- OPERATOR_HOME unset or empty refuses (ci.yml revise 3: CI now passes
# it in from a repository variable rather than naming a path in the
# workflow, so both "never set" and "set to empty" are real CI states) ---
run_with_operator_home_unset
assert_rc 1 "$RC" "an unset OPERATOR_HOME fails the run"
assert_contains "$OUT" "FAIL: OPERATOR_HOME is unset or not a directory" "the specific failure is named"

run_with_operator_home_empty
assert_rc 1 "$RC" "an empty OPERATOR_HOME fails the run"
assert_contains "$OUT" "FAIL: OPERATOR_HOME is unset or not a directory" "the specific failure is named"

STUB_NC_PG=ok run
assert_rc 1 "$RC" "a connectable postgres socket fails the run"
assert_contains "$OUT" "FAIL: connected to /tmp/.s.PGSQL.5432" "the specific failure is named"

# --- a response body is never echoed ---
run
assert_contains "$OUT" "" "sanity: run produced output"
case "$OUT" in
    *SECRET_RESPONSE_BODY_LEAK*)
        assert_eq "no leak" "LEAKED" "a response body reached the script's own stdout" ;;
    *)
        assert_eq "no leak" "no leak" "no response body reached the script's own stdout" ;;
esac

# --- known ports probe their real/given route; unknown and ranged ports
# fall back correctly. Every curl call is logged with its full argv. ---
run
assert_contains "$(cat "$CURL_LOG")" "http://127.0.0.1:4000/v1/models" "LiteLLM /v1/models probed"
assert_contains "$(cat "$CURL_LOG")" "http://127.0.0.1:4000/key/info" "LiteLLM /key/info probed"
assert_contains "$(cat "$CURL_LOG")" "http://127.0.0.1:8100/items" "Library /items probed (derived from vault_plane.py)"
assert_contains "$(cat "$CURL_LOG")" "http://127.0.0.1:9999/" "unrecognised port falls back to /"
assert_contains "$(cat "$CURL_LOG")" "http://127.0.0.1:8325/health" "tennis-range port (8321-8330) probes /health"
case "$(cat "$CURL_LOG")" in
    *":54321"*)
        assert_eq "not probed" "PROBED" "an ESTABLISHED (non-LISTEN) row must never be probed" ;;
    *)
        assert_eq "not probed" "not probed" "an ESTABLISHED (non-LISTEN) row is never probed" ;;
esac

# --- round-2 fix (CQ-417 item 3): every port the work item names is still
# requested on its listed route even when enumeration (netstat AND lsof)
# sees nothing, reproducing the real runner's round-1 defect where netstat
# as ap-runner enumerated zero rows while five of these ports were actually
# listening. The old "(no listeners found)" wording must never reappear. ---
STUB_NETSTAT_EMPTY=yes run
assert_rc 0 "$RC" "empty enumeration does not fail the run (GET probes are informational)"
assert_contains "$OUT" "enumeration returned nothing (this tool cannot see other users' sockets here)" "the empty-enumeration message names why, not just that"
case "$OUT" in
    *"no listeners found"*)
        assert_eq "no stale wording" "STALE WORDING PRESENT" "the old 'no listeners found' wording must never appear" ;;
    *)
        assert_eq "no stale wording" "no stale wording" "the old 'no listeners found' wording does not appear" ;;
esac
FIXED_PROBE_LOG=$(cat "$CURL_LOG")
for fixed_probe in \
    "http://127.0.0.1:4000/v1/models" \
    "http://127.0.0.1:4000/key/info" \
    "http://127.0.0.1:8100/items" \
    "http://127.0.0.1:8099/api/issues" \
    "http://127.0.0.1:8787/health" \
    "http://127.0.0.1:8000/status" \
    "http://127.0.0.1:5151/health" \
    "http://127.0.0.1:4100/health" \
    "http://127.0.0.1:8765/health" \
    "http://127.0.0.1:8300/health" \
    "http://127.0.0.1:8080/version" \
    "http://127.0.0.1:8081/version" \
    "http://127.0.0.1:20241/metrics" \
    "http://127.0.0.1:50406/" \
    "http://127.0.0.1:8321/health" \
    "http://127.0.0.1:8330/health"; do
    assert_contains "$FIXED_PROBE_LOG" "$fixed_probe" "fixed-list port probed on its route with empty enumeration: $fixed_probe"
done

# --- an extra listener found only by lsof (outside the fixed list) is
# enumerated and probed with GET / only ---
STUB_LSOF_EXTRA=yes run
assert_contains "$OUT" "listening: 7777" "the lsof-only listener is enumerated"
assert_contains "$(cat "$CURL_LOG")" "http://127.0.0.1:7777/" "the lsof-only extra listener is probed with GET /"

# --- every probe is a GET: no -X/--request/-d/--data/-T/--upload-file/-F
# ever reaches curl's argv, across the full fixed-list + extra-listener run ---
run
case "$(cat "$CURL_LOG")" in
    *" -X "*|*"--request"*|*" -d "*|*"--data"*|*" -T "*|*"--upload-file"*|*" -F "*)
        assert_eq "GET only" "NON-GET METHOD USED" "a probe used a non-GET curl flag" ;;
    *)
        assert_eq "GET only" "GET only" "every probe uses curl's default GET method" ;;
esac

# --- the listener survey and GET results are reported, not judged ---
run
assert_contains "$OUT" "listening: 4000" "listener survey lists a discovered port"
assert_contains "$OUT" "port=4000 route=/v1/models status=200 bytes=42" "a GET result line reports port/route/status/bytes"

# --- curl itself refusing (service down) is reported, not fatal ---
STUB_CURL_RC=7 run
assert_rc 0 "$RC" "a refused connection does not fail the run on its own"
assert_contains "$OUT" "status=refused(curl_rc=7)" "a refused connection is reported as refused"

# --- item 5: the job user's own home ---
chmod 700 "$OWN_HOME"
run
assert_rc 0 "$RC" "a home at 700 passes"
assert_contains "$OUT" "own home ($OWN_HOME) mode: 700" "the mode found is printed"
assert_contains "$OUT" "ok: own home ($OWN_HOME) grants no group or other permission" "700 is named as ok"

for mode in 750 705 770; do
    chmod "$mode" "$OWN_HOME"
    run
    assert_rc 1 "$RC" "a home at $mode fails the run"
    assert_contains "$OUT" "own home ($OWN_HOME) mode: $mode" "the mode $mode is printed"
    assert_contains "$OUT" "FAIL: own home ($OWN_HOME) mode $mode grants group or other permission" "the specific failure names mode $mode"
done
chmod 700 "$OWN_HOME"

# --- round-2 fix: BSD `stat -f '%Lp'` strips mode 000's leading zeros to
# the bare string "0", which the round-1 pattern (`*00`, needing two
# trailing chars) missed and would have reported a false FAIL for ---
chmod 000 "$OWN_HOME"
run
assert_rc 0 "$RC" "a home at 000 passes"
assert_contains "$OUT" "own home ($OWN_HOME) mode: 0" "the bare \"0\" mode is printed"
assert_contains "$OUT" "ok: own home ($OWN_HOME) grants no group or other permission" "mode 000's bare \"0\" is named ok, not a false FAIL"
chmod 700 "$OWN_HOME"

run_with_home_unset
assert_rc 1 "$RC" "an unset \$HOME fails the run"
assert_contains "$OUT" "FAIL: \$HOME is unset or not a directory" "the specific failure is named"

# --- item 5: other runner homes ---
OTHER_HOME="$HOMES/deck-runner"
mkdir -p "$OTHER_HOME"

STUB_LS_OTHER_HOME_PATH="$OTHER_HOME" run
assert_rc 0 "$RC" "an unreadable other-runner home passes"
assert_contains "$OUT" "ok: $OTHER_HOME is not readable" "the other home's refusal is reported"

STUB_LS_OTHER_HOME_PATH="$OTHER_HOME" STUB_LS_OTHER_HOME=ok run
assert_rc 1 "$RC" "a readable other-runner home fails the run"
assert_contains "$OUT" "FAIL: $OTHER_HOME is readable by" "the specific failure is named"

rm -rf "$OTHER_HOME"
run
assert_rc 0 "$RC" "no other runner homes at all passes"
case "$OUT" in
    *"is readable by"*)
        assert_eq "no other-home failure" "FAILED" "a FAIL line appeared with no other runner home present" ;;
    *)
        assert_eq "no other-home failure" "no other-home failure" "no other-runner-home FAIL line appears" ;;
esac

rm -rf "$BIN" "$HOMES"
finish
