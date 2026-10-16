#!/bin/bash
# tests/bash/test_tailscale_watchdog.sh — the watchdog's state handling cannot be
# stopped by what it reads off disk.
#
# The watchdog guards the only route into a headless machine, so the failure that
# matters is not "it did the wrong thing" but "it stopped running", and every
# instance of that found so far arrived the same way: a value read from a state
# file reaching a shell arithmetic expansion. Two rounds of review found two
# separate doors into it — content that is not a number at all, then content that
# is all digits but has a leading zero, which shell arithmetic reads as octal, so
# "089" aborts the script and "017" quietly means 15.
#
# Both were found by a person constructing the case, neither by anything that
# runs. That is what this file changes.
#
# Run under /bin/bash (3.2.57), per ARCH-082.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

WATCHDOG="$REPO_ROOT/bin/tailscale-watchdog"

[ -f "$WATCHDOG" ] || {
    echo "  FAIL: no $WATCHDOG"
    _FAIL=$((_FAIL + 1))
    finish
    exit 1
}

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# read_int, lifted out of the script and given the two globals it uses. Lifting
# rather than reimplementing: a copy of the logic would pass while the real one
# was broken, which is the whole failure being guarded against.
build_probe() {
    {
        echo 'set -u'
        echo 'STATE="$1"; LOG=$STATE/watchdog.log'
        echo 'log() { echo "$*" >> "$LOG"; }'
        sed -n '/^read_int() {/,/^}/p' "$WATCHDOG"
        cat <<'PROBE'
f=$STATE/v
printf '%s\n' "$2" > "$f"
v=$(read_int "$f")
# The expansion that used to stop the script dead. If read_int let something
# through, this is where it shows.
n=$(( v + 1 ))
echo "value=$v next=$n"
PROBE
    } > "$WORK/probe.sh"
}

check_value() { # <stored> <expected read_int> <msg>
    build_probe
    out=$(/bin/sh "$WORK/probe.sh" "$WORK" "$1" 2>&1)
    rc=$?
    assert_rc 0 "$rc" "$3: the script survived reading it (output: $out)"
    # ANCHORED ON THE TRAILING FIELD. `assert_contains` is a plain substring
    # test, so the needle "value=0" also matches the UNREPAIRED output
    # "value=017" — which made the 017, 007 and 000 cases pass against the very
    # bug they were written for. Found by a reviewer reverting the fix and
    # watching the suite stay green: 31 passed, 0 failed, against code known to
    # be broken. Matching through to " next=" makes the field end the needle.
    assert_contains "$out" "value=$2 next=" "$3: read as $2"
}

echo "== a state file cannot stop the script =="

# Ordinary values pass through untouched.
check_value "0" "0" "zero"
check_value "12" "12" "a plain count"
check_value "999999" "999999" "a large plain count"

# Round 1's door: content that is not a number.
check_value "not-a-number!!" "0" "punctuation"
check_value "garbage" "0" "a bare word, which set -u reads as an unset variable"
check_value "" "0" "an empty file, the ordinary first-boot case"

# Round 2's door: all digits, but octal to the shell.
check_value "089" "0" "089, which is not valid octal and aborts the shell"
check_value "017" "0" "017, which would silently mean 15"
check_value "007" "0" "007"
check_value "000" "0" "000"

# Overflow.
check_value "99999999999999999999999999" "0" "a 26-digit value"

echo "== a repaired file is safe on the next read =="

# The repair writes back to the same file it read, so the second read must agree
# with the first. A repair that wrote something the next read rejects again would
# loop forever without ever being wrong enough to notice.
build_probe
printf '089\n' > "$WORK/v"
{
    echo 'set -u'
    echo 'STATE="$1"; LOG=$STATE/watchdog.log'
    echo 'log() { echo "$*" >> "$LOG"; }'
    sed -n '/^read_int() {/,/^}/p' "$WATCHDOG"
    cat <<'PROBE'
f=$STATE/v
a=$(read_int "$f")
b=$(read_int "$f")
c=$(read_int "$f")
echo "reads=$a,$b,$c"
PROBE
} > "$WORK/repeat.sh"
out=$(/bin/sh "$WORK/repeat.sh" "$WORK" 2>&1)
assert_contains "$out" "reads=0,0,0" "a repaired file reads the same way every time"

echo "== the damage is logged, not swallowed =="

build_probe
rm -f "$WORK/watchdog.log"
/bin/sh "$WORK/probe.sh" "$WORK" "089" >/dev/null 2>&1
logged=$(cat "$WORK/watchdog.log" 2>/dev/null)
assert_contains "$logged" "octal" "an octal-looking value says so in the log"

rm -f "$WORK/watchdog.log"
/bin/sh "$WORK/probe.sh" "$WORK" "12" >/dev/null 2>&1
quiet=$(cat "$WORK/watchdog.log" 2>/dev/null)
assert_eq "" "$quiet" "a healthy value logs nothing, so the log stays skimmable"

echo "== the health probe is bounded and never crashes =="

{
    echo 'set -u'
    echo 'TS="$1"; HEALTH_TIMEOUT="$2"'
    sed -n '/^healthy() {/,/^}/p' "$WATCHDOG"
    echo 'if healthy; then echo RESULT=healthy; else echo RESULT=unhealthy; fi'
} > "$WORK/health.sh"

stub_ts() { printf '#!/bin/sh\ncat <<EOJ\n%s\nEOJ\n' "$1" > "$WORK/ts"; chmod +x "$WORK/ts"; }

stub_ts '{"BackendState":"Running","Self":{"Online":true}}'
out=$(/bin/sh "$WORK/health.sh" "$WORK/ts" 5 2>&1)
assert_contains "$out" "RESULT=healthy" "a running, online node is healthy"

stub_ts '{"BackendState":"Running","Self":{"Online":false}}'
out=$(/bin/sh "$WORK/health.sh" "$WORK/ts" 5 2>&1)
assert_contains "$out" "RESULT=unhealthy" "running but offline is unhealthy, which is the case BackendState alone misses"

# Valid JSON that is not an object. `.get` would raise past the handler if the
# answer were read outside the try.
for body in 'null' '[1,2,3]' '42' '"a string"' 'not json at all'; do
    stub_ts "$body"
    out=$(/bin/sh "$WORK/health.sh" "$WORK/ts" 5 2>&1)
    assert_contains "$out" "RESULT=unhealthy" "$body is unhealthy rather than a crash"
done

# A CLI that never answers costs one bounded probe, not the watchdog.
printf '#!/bin/sh\nsleep 60\n' > "$WORK/ts"; chmod +x "$WORK/ts"
started=$(date +%s)
out=$(/bin/sh "$WORK/health.sh" "$WORK/ts" 3 2>&1)
elapsed=$(( $(date +%s) - started ))
assert_contains "$out" "RESULT=unhealthy" "a hanging CLI is unhealthy"
if [ "$elapsed" -le 15 ]; then
    _PASS=$((_PASS + 1))
else
    _FAIL=$((_FAIL + 1))
    echo "  FAIL: the health probe was not bounded — took ${elapsed}s against a 3s bound"
fi

# A CLI that is not there at all.
out=$(/bin/sh "$WORK/health.sh" "$WORK/does-not-exist" 3 2>&1)
assert_contains "$out" "RESULT=unhealthy" "a missing CLI is unhealthy"

echo "== the lock lets one run at a time, and releases only what it took =="

# The lock runs at the top of the script rather than inside a function, so it
# cannot be lifted the way read_int and healthy are. It is exercised end to end
# instead, against a scratch state directory and a stub CLI that reports healthy
# so the run stops before any recovery action.
printf '#!/bin/sh\ncat <<EOJ\n{"BackendState":"Running","Self":{"Online":true}}\nEOJ\n' > "$WORK/ts-ok"
chmod +x "$WORK/ts-ok"

run_watchdog() { # <state dir>
    TAILSCALE_WATCHDOG_CLI="$WORK/ts-ok" TAILSCALE_WATCHDOG_STATE="$1" /bin/sh "$WATCHDOG"
}

# An ordinary run takes the lock and gives it back.
ST="$WORK/s1"; mkdir -p "$ST"
run_watchdog "$ST"; rc=$?
assert_rc 0 "$rc" "a healthy run exits 0"
if [ -d "$ST/lock" ]; then
    _FAIL=$((_FAIL + 1)); echo "  FAIL: the lock outlived a run that finished"
else
    _PASS=$((_PASS + 1))
fi

# A lock somebody else holds stops this run, and — the property that matters —
# this run does NOT remove it on the way out. Releasing a lock it never took is
# what lets a third run walk into the holder's critical section.
ST="$WORK/s2"; mkdir -p "$ST/lock"
echo 999999 > "$ST/lock/pid"
run_watchdog "$ST"; rc=$?
assert_rc 0 "$rc" "a run that finds the lock held exits quietly"
if [ -d "$ST/lock" ]; then
    _PASS=$((_PASS + 1))
else
    _FAIL=$((_FAIL + 1)); echo "  FAIL: a run that never took the lock removed it anyway"
fi
assert_eq "999999" "$(cat "$ST/lock/pid" 2>/dev/null)" "the holder's pid is left alone"

# A lock older than the staleness bound is assumed abandoned and taken over,
# because a watchdog its own leftovers can disable forever is the failure this
# whole file is about.
ST="$WORK/s3"; mkdir -p "$ST/lock"
touch -t 202001010000 "$ST/lock"
run_watchdog "$ST"; rc=$?
assert_rc 0 "$rc" "a run takes over an abandoned lock"
assert_contains "$(cat "$ST/watchdog.log" 2>/dev/null)" "abandoned run" "the takeover says so in the log"
if [ -d "$ST/lock" ]; then
    _FAIL=$((_FAIL + 1)); echo "  FAIL: the taken-over lock was not released"
else
    _PASS=$((_PASS + 1))
fi

# Two ticks seeing the SAME stale lock must not both take it. Remove-then-create
# lets both succeed — each removes and recreates in turn, each writes its own
# pid, and both proceed believing they hold it alone, which is two watchdogs
# duplicating every recovery action. The takeover is a rename so exactly one can
# win, and the log is where that shows: one takeover line, not two.
ST="$WORK/s6"; mkdir -p "$ST/lock"
echo 999999 > "$ST/lock/pid"
touch -t 202001010000 "$ST/lock"
run_watchdog "$ST" >/dev/null 2>&1 &
run_watchdog "$ST" >/dev/null 2>&1 &
wait
# `grep -c` prints 0 AND exits 1 when nothing matches, so an `|| echo 0` fallback
# appends a second zero and the failure message reads "0\n0".
takeovers=$(grep -c "abandoned run" "$ST/watchdog.log" 2>/dev/null | tail -1)
[ -n "$takeovers" ] || takeovers=0
assert_eq "1" "$takeovers" "two ticks racing one stale lock produce exactly one takeover"

echo "== the healthy path resets what an outage left behind =="

ST="$WORK/s4"; mkdir -p "$ST"
echo 2 > "$ST/consecutive-failures"
run_watchdog "$ST" >/dev/null 2>&1
assert_eq "0" "$(cat "$ST/consecutive-failures" 2>/dev/null)" "recovering resets the failure count"
assert_contains "$(cat "$ST/watchdog.log" 2>/dev/null)" "RECOVERED" "the recovery edge is logged"

# A healthy run that was already at zero says nothing, so the log stays skimmable
# across the 1440 times a day this runs.
ST="$WORK/s5"; mkdir -p "$ST"
run_watchdog "$ST" >/dev/null 2>&1
assert_eq "" "$(cat "$ST/watchdog.log" 2>/dev/null)" "a healthy run with nothing to report logs nothing"

echo "== the lock's age survives a stat that is not the one this machine has =="

# THE SAME SHAPE THIS FILE'S HEADER DESCRIBES, one layer out: not a value read
# off disk, but a value read off a TOOL whose flags mean different things on
# different systems. `stat -f` is BSD format; on GNU coreutils `-f` is
# `--file-system`, so the old line printed a multi-line filesystem dump to
# stdout and exited 1 — the `|| echo 0` fallback DID fire, but too late: the
# dump was already inside the command substitution's stdout before `||` ran,
# so the fallback's "0" landed after the dump instead of replacing it, and
# `_held` held the dump-plus-0. `[ "$_held" -gt 0 ]` then errored on a
# non-integer and read false, so the staleness test fell through to the
# takeover path — A LIVE LOCK READ AS STALE ON EVERY TICK, on the one machine
# class this watchdog exists to keep reachable. Found by another crew running
# it in debian:bookworm-slim, not by anything here, which is the gap this
# case closes.
#
# The stub is the GNU parse rather than a guess at it: `-c FMT` answers, and
# `-f` treats its operands as filesystems, printing a dump for one that exists
# and failing for `%m`, which is not a path.
_gnu_stat_dir="$WORK/gnubin"; mkdir -p "$_gnu_stat_dir"
cat > "$_gnu_stat_dir/stat" <<'GNUSTAT'
#!/bin/sh
if [ "$1" = "-c" ]; then
    case "$2" in
        %Y) /usr/bin/stat -f '%m' "$3" ;;
        *)  exit 1 ;;
    esac
    exit 0
fi
shift
_rc=0
for _op in "$@"; do
    if [ -e "$_op" ]; then
        printf '  File: "%s"\n    Type: overlayfs\nBlock size: 4096\n' "$_op"
    else
        echo "stat: cannot read file system information for '$_op'" >&2
        _rc=1
    fi
done
exit $_rc
GNUSTAT
chmod +x "$_gnu_stat_dir/stat"

# A lock created NOW is live, so a second tick must stand down rather than take
# it over. That is the assertion the defect broke.
ST="$WORK/s6"; mkdir -p "$ST"
# EXTRACTED BETWEEN ANCHORS THAT EXIST IN THE BROKEN VERSION TOO. A first
# version of this sed anchored on `_held=0`, a line only the FIXED code has, so
# reverting the fix made the probe extract nothing and the case failed because
# it found no code rather than because the code misbehaved — a plant check that
# goes red for the wrong reason is not one. `if ! mkdir` and `_now=$(date`
# bracket the age read in both versions, so the probe runs whatever is there.
_probe="$WORK/lockage.sh"
{
    echo 'LOCK="$1"'
    sed -n '/if ! mkdir "\$LOCK"/,/_now=\$(date/p' "$WATCHDOG" \
        | sed -e '1d' -e '$d'
    echo 'echo "held=${_held-UNSET}"'
} > "$_probe"

mkdir -p "$ST/lock"
out=$(PATH="$_gnu_stat_dir:$PATH" /bin/sh "$_probe" "$ST/lock" 2>&1)
assert_rc 0 "$?" "the age read survives a GNU-style stat (output: $out)"
case "$out" in
    held=[0-9]*) _held_ok=yes ;;
    *)           _held_ok=no ;;
esac
assert_eq "yes" "$_held_ok" "a GNU-style stat yields an integer mtime, not a filesystem dump"

# THE `case` IS A STATEMENT, NOT A `$( )` SUBSTITUTION, and that is a bash 3.2
# requirement rather than style. 3.2 parses `$(...)` by counting parentheses, so
# the `)` that closes a case PATTERN ends the substitution early; bash 4 fixed
# it. Written inline, this assertion passed under Homebrew bash 5.3 and failed
# under `/bin/bash` 3.2 — which is the version `run.sh` invokes on purpose
# (ARCH-082), and the only one that matches the mini.
case "$out" in
    *overlayfs*|*"File:"*) _dump_leaked=yes ;;
    *)                     _dump_leaked=no ;;
esac
assert_eq "no" "$_dump_leaked" "no filesystem dump leaks into the age"

# And the BSD path this machine actually runs still answers.
out=$(PATH="/usr/bin:/bin" /bin/sh "$_probe" "$ST/lock" 2>&1)
case "$out" in
    held=[0-9]*) _held_ok=yes ;;
    *)           _held_ok=no ;;
esac
assert_eq "yes" "$_held_ok" "the BSD path still yields an integer mtime"

# STATE's default tracks $HOME rather than a baked-in account name, so the
# resolved path doesn't silently keep naming the old account once the job runs
# as someone else (platform/decisions/0090).
_state_probe="$WORK/state-default.sh"
{
    echo 'set -u'
    sed -n '/^STATE=/p' "$WATCHDOG"
    echo 'echo "$STATE"'
} > "$_state_probe"
_fake_home="$WORK/fake-home"
out=$(env -u TAILSCALE_WATCHDOG_STATE HOME="$_fake_home" /bin/sh "$_state_probe")
assert_eq "$_fake_home/.local/state/tailscale-watchdog" "$out" \
    "STATE's default is \$HOME-relative, not a hardcoded account"

echo "== step 2 restores a tunnel on a node with non-default prefs, and keeps its services =="
#
# On 2026-10-15 a plain `tailscale up` was refused on a node with non-default
# prefs ("changing settings via 'tailscale up' requires mentioning all
# non-default flags"), and running the CLI's own suggested re-run also
# cleared AdvertiseServices. Everything below is synthetic: no real keys, no
# real node IDs, Config filled with an obvious placeholder.

PLACEHOLDER_KEY="privkey:PLACEHOLDER0000000000000000000000000000000000000000000000000000"

EXITNODE_PREFS_BEFORE="$WORK/exitnode-before.json"
cat > "$EXITNODE_PREFS_BEFORE" <<JSON
{
  "WantRunning": false,
  "AdvertiseRoutes": ["0.0.0.0/0", "::/0"],
  "AdvertiseServices": ["svc:a", "svc:b", "svc:c", "svc:d"],
  "Config": {"PrivateNodeKey": "$PLACEHOLDER_KEY", "NodeID": "nSYNTHETIC00000001"}
}
JSON

EXITNODE_PREFS_AFTER="$WORK/exitnode-after.json"
cat > "$EXITNODE_PREFS_AFTER" <<JSON
{
  "WantRunning": true,
  "AdvertiseRoutes": ["0.0.0.0/0", "::/0"],
  "AdvertiseServices": [],
  "Config": {"PrivateNodeKey": "$PLACEHOLDER_KEY", "NodeID": "nSYNTHETIC00000001"}
}
JSON

TAG_PREFS="$WORK/tag-prefs.json"
cat > "$TAG_PREFS" <<JSON
{
  "WantRunning": false,
  "AdvertiseTags": ["tag:apps"],
  "Config": {"PrivateNodeKey": "$PLACEHOLDER_KEY", "NodeID": "nSYNTHETIC00000002"}
}
JSON

DEFAULT_PREFS="$WORK/default-prefs.json"
cat > "$DEFAULT_PREFS" <<JSON
{
  "WantRunning": false,
  "Config": {"PrivateNodeKey": "$PLACEHOLDER_KEY", "NodeID": "nSYNTHETIC00000003"}
}
JSON

# A process whose full command line ends in the exact string step 1's pgrep
# looks for, so step 1 (already covered elsewhere) stands aside and every run
# below reaches step 2 directly rather than sleeping 20s and shelling out to
# the real `open -a Tailscale`.
FAKE_APP_DIR="$WORK/Tailscale.app/Contents/MacOS"
mkdir -p "$FAKE_APP_DIR"
printf '#!/bin/sh\nsleep 600\n' > "$FAKE_APP_DIR/Tailscale"
chmod +x "$FAKE_APP_DIR/Tailscale"
"$FAKE_APP_DIR/Tailscale" &
FAKE_APP_PID=$!
trap 'kill "$FAKE_APP_PID" 2>/dev/null; rm -rf "$WORK"' EXIT

# A stub CLI whose `up` answers depend on exactly the flags it was called
# with, and which records every call it receives, in order, to $STUB_LOG.
# `debug prefs` answers from a before/after pair of fixtures, switching once
# `up` has been called with the flags that are supposed to succeed — the
# stand-in for the real CLI's observed side effect of clearing
# AdvertiseServices on that exact call.
write_ts_stub() {
    cat > "$WORK/ts" <<'STUB'
#!/bin/sh
printf '%s\n' "$*" >> "$STUB_LOG"

if [ "$1" = "status" ]; then
    # Always unhealthy, so every scenario below reaches step 2 the same way
    # and nothing here depends on the final RECOVERED/STILL DOWN branch.
    echo '{"BackendState":"Running","Self":{"Online":false}}'
    exit 0
fi

case "$1" in
    debug)
        if [ -f "$STUB_STATE/cleared" ]; then
            cat "$STUB_PREFS_AFTER"
        else
            cat "$STUB_PREFS_BEFORE"
        fi
        exit 0
        ;;
    up)
        shift
        if [ "$STUB_SCENARIO" = "default" ]; then
            exit 0
        fi
        if [ "$*" = "--timeout=30s" ]; then
            echo "changing settings via 'tailscale up' requires mentioning all non-default flags. To proceed, either re-run your command with --reset or use the command below to explicitly mention the current value of all non-default settings: tailscale up --timeout=30s $STUB_SUGGESTED"
            exit 1
        fi
        if [ -n "$STUB_REQUIRED" ] && [ "$*" = "--timeout=30s $STUB_REQUIRED" ]; then
            touch "$STUB_STATE/cleared"
            exit 0
        fi
        echo "unexpected up args, should not have been run: $*" >&2
        exit 1
        ;;
    serve)
        exit 0
        ;;
    *)
        exit 0
        ;;
esac
STUB
    chmod +x "$WORK/ts"
}

# <dirname> <scenario> <prefs before> <prefs after> <suggested extra flags> <flags required for success>
run_scenario() {
    st="$WORK/$1"
    mkdir -p "$st"
    # Pre-seeded past THRESHOLD and past COOLDOWN, so this one run goes
    # straight through to step 1 (which stands aside, above) and step 2,
    # rather than needing three real ticks to get there.
    echo 2 > "$st/consecutive-failures"
    echo 0 > "$st/last-action"
    stubstate="$WORK/stubstate-$1"
    rm -rf "$stubstate"
    mkdir -p "$stubstate"
    STUB_LOG="$WORK/stublog-$1"
    rm -f "$STUB_LOG"
    STUB_STATE="$stubstate"
    STUB_SCENARIO="$2"
    STUB_PREFS_BEFORE="$3"
    STUB_PREFS_AFTER="$4"
    STUB_SUGGESTED="$5"
    STUB_REQUIRED="$6"
    export STUB_LOG STUB_STATE STUB_SCENARIO STUB_PREFS_BEFORE STUB_PREFS_AFTER STUB_SUGGESTED STUB_REQUIRED
    write_ts_stub
    TAILSCALE_WATCHDOG_CLI="$WORK/ts" TAILSCALE_WATCHDOG_STATE="$st" /bin/sh "$WATCHDOG" >/dev/null 2>&1
}

# -- an exit-node-shaped node: refused, rerun with exactly the suggested flag,
#    then every cleared service is re-advertised ------------------------------
run_scenario "exitnode" "exitnode" "$EXITNODE_PREFS_BEFORE" "$EXITNODE_PREFS_AFTER" \
    "--advertise-exit-node" "--advertise-exit-node"
calls=$(grep -E '^(debug|up|serve)' "$WORK/stublog-exitnode")
expected=$(printf '%s\n' \
    "debug prefs" \
    "up --timeout=30s" \
    "up --timeout=30s --advertise-exit-node" \
    "debug prefs" \
    "serve advertise svc:a" \
    "serve advertise svc:b" \
    "serve advertise svc:c" \
    "serve advertise svc:d")
assert_eq "$expected" "$calls" \
    "an exit-node-shaped node: the exact rerun and re-advertise argv, in order"

# -- a tag-shaped node: refused, rerun with exactly the suggested flag, and
#    nothing to re-advertise since there were no services to begin with -------
run_scenario "tagnode" "tag" "$TAG_PREFS" "$TAG_PREFS" \
    "--advertise-tags=tag:apps" "--advertise-tags=tag:apps"
calls=$(grep -E '^(debug|up|serve)' "$WORK/stublog-tagnode")
expected=$(printf '%s\n' \
    "debug prefs" \
    "up --timeout=30s" \
    "up --timeout=30s --advertise-tags=tag:apps" \
    "debug prefs")
assert_eq "$expected" "$calls" \
    "a tag-shaped node: the exact rerun argv, in order, and nothing re-advertised"

# -- a default node: bare `up` just succeeds; no rerun, no re-advertise -------
run_scenario "defaultnode" "default" "$DEFAULT_PREFS" "$DEFAULT_PREFS" "" ""
calls=$(grep -E '^(debug|up|serve)' "$WORK/stublog-defaultnode")
expected=$(printf '%s\n' "debug prefs" "up --timeout=30s" "debug prefs")
assert_eq "$expected" "$calls" "a default node: bare up succeeds, no rerun, no re-advertise"

# -- a suggestion containing --reset: refused outright, never run -------------
run_scenario "badreset" "badreset" "$EXITNODE_PREFS_BEFORE" "$EXITNODE_PREFS_BEFORE" \
    "--reset --advertise-exit-node" ""
calls=$(grep -E '^(debug|up|serve)' "$WORK/stublog-badreset")
expected=$(printf '%s\n' "debug prefs" "up --timeout=30s" "debug prefs")
assert_eq "$expected" "$calls" \
    "a suggestion containing --reset: only the bare, refused up is ever run"
# Anchored on RERUN_REFUSED, not just "--reset": that substring is also in the
# CLI's raw refusal message, which the log holds either way, so a needle of
# "--reset" alone would pass just as well against code with no allowlist at
# all. RERUN_REFUSED is printed only by the allowlist check itself.
assert_contains "$(cat "$WORK/badreset/watchdog.log" 2>/dev/null)" \
    "RERUN_REFUSED: suggestion held a flag outside the allowlist: --reset" \
    "...and the refusal names --reset as the disallowed flag"

# -- a suggestion with a flag outside the allowlist: refused outright ---------
run_scenario "badoutside" "badoutside" "$EXITNODE_PREFS_BEFORE" "$EXITNODE_PREFS_BEFORE" \
    "--some-unlisted-flag" ""
calls=$(grep -E '^(debug|up|serve)' "$WORK/stublog-badoutside")
expected=$(printf '%s\n' "debug prefs" "up --timeout=30s" "debug prefs")
assert_eq "$expected" "$calls" \
    "a suggestion with a flag outside the allowlist: only the bare, refused up is ever run"
assert_contains "$(cat "$WORK/badoutside/watchdog.log" 2>/dev/null)" \
    "RERUN_REFUSED: suggestion held a flag outside the allowlist: --some-unlisted-flag" \
    "...and the refusal names the disallowed flag"

# -- a suggestion containing --operator: refused outright, never run. This flag
#    sets which local user may operate tailscaled, not a restated node setting,
#    so it must never be on ALLOWED_UP_FLAGS even though the CLI can suggest it
#    on a node with a non-default operator. ------------------------------------
run_scenario "badoperator" "badoperator" "$EXITNODE_PREFS_BEFORE" "$EXITNODE_PREFS_BEFORE" \
    "--operator=synthetic-test-user" ""
calls=$(grep -E '^(debug|up|serve)' "$WORK/stublog-badoperator")
expected=$(printf '%s\n' "debug prefs" "up --timeout=30s" "debug prefs")
assert_eq "$expected" "$calls" \
    "a suggestion containing --operator: only the bare, refused up is ever run"
assert_contains "$(cat "$WORK/badoperator/watchdog.log" 2>/dev/null)" \
    "RERUN_REFUSED: suggestion held a flag outside the allowlist: --operator=synthetic-test-user" \
    "...and the refusal names --operator as the disallowed flag"

# -- a suggestion containing --exit-node: refused outright, never run. This flag
#    sets the node's selected exit-node target, not a restated node setting, so
#    it must never be on ALLOWED_UP_FLAGS even though the CLI can suggest it on
#    a node with a non-default selected exit node. -----------------------------
run_scenario "badexitnode" "badexitnode" "$EXITNODE_PREFS_BEFORE" "$EXITNODE_PREFS_BEFORE" \
    "--exit-node=192.0.2.1" ""
calls=$(grep -E '^(debug|up|serve)' "$WORK/stublog-badexitnode")
expected=$(printf '%s\n' "debug prefs" "up --timeout=30s" "debug prefs")
assert_eq "$expected" "$calls" \
    "a suggestion containing --exit-node: only the bare, refused up is ever run"
assert_contains "$(cat "$WORK/badexitnode/watchdog.log" 2>/dev/null)" \
    "RERUN_REFUSED: suggestion held a flag outside the allowlist: --exit-node=192.0.2.1" \
    "...and the refusal names --exit-node as the disallowed flag"

# -- a suggestion holding a bare, non-flag token alongside allowlisted flags:
#    refused outright by the non-flag-token branch, never the allowlist branch,
#    and never run. -------------------------------------------------------------
run_scenario "badtoken" "badtoken" "$EXITNODE_PREFS_BEFORE" "$EXITNODE_PREFS_BEFORE" \
    "--advertise-exit-node example-word" ""
calls=$(grep -E '^(debug|up|serve)' "$WORK/stublog-badtoken")
expected=$(printf '%s\n' "debug prefs" "up --timeout=30s" "debug prefs")
assert_eq "$expected" "$calls" \
    "a suggestion holding a non-flag token: only the bare, refused up is ever run"
assert_contains "$(cat "$WORK/badtoken/watchdog.log" 2>/dev/null)" \
    "RERUN_REFUSED: suggestion held a non-flag token: example-word" \
    "...and the refusal names the non-flag token"

echo "== step 2 detects and recovers the exact 2026-10-15 App Store relaunch state =="
#
# At 06:43 MST on 2026-10-15 an App Store update to the system extension
# relaunched Tailscale with the app process still running, the CLI's status
# reporting BackendState "Stopped" (no NeedsLogin, no AuthURL — a
# configuration fault, not an auth one), and WantRunning left false. That is
# the shape step 1 cannot see, since the app never stopped; step 2 is what
# must recognise it. This drives that shape through THRESHOLD separate
# invocations from a clean state, the way the real watchdog ticks every
# 60s, rather than pre-seeding past the threshold the way the scenarios
# above do: the property under test is that detection reaches step 2 at the
# right tick, not only that step 2 recovers once reached.

ST="$WORK/appstore"; mkdir -p "$ST"
_appstore_stubstate="$WORK/stubstate-appstore"
rm -rf "$_appstore_stubstate"; mkdir -p "$_appstore_stubstate"
STUB_LOG="$WORK/stublog-appstore"
rm -f "$STUB_LOG"
STUB_STATE="$_appstore_stubstate"
STUB_PREFS_BEFORE="$EXITNODE_PREFS_BEFORE"
STUB_PREFS_AFTER="$EXITNODE_PREFS_AFTER"
STUB_SUGGESTED="--advertise-exit-node"
STUB_REQUIRED="--advertise-exit-node"
export STUB_LOG STUB_STATE STUB_PREFS_BEFORE STUB_PREFS_AFTER STUB_SUGGESTED STUB_REQUIRED

cat > "$WORK/ts-appstore" <<'STUB'
#!/bin/sh
printf '%s\n' "$*" >> "$STUB_LOG"

if [ "$1" = "status" ]; then
    # The exact detection surface: BackendState "Stopped" while the app
    # process is still running. Flips to Running only once the allowlisted
    # rerun below has actually succeeded, standing in for the real recovery.
    if [ -f "$STUB_STATE/cleared" ]; then
        echo '{"BackendState":"Running","Self":{"Online":true}}'
    else
        echo '{"BackendState":"Stopped","Self":{"Online":false}}'
    fi
    exit 0
fi

case "$1" in
    debug)
        if [ -f "$STUB_STATE/cleared" ]; then
            cat "$STUB_PREFS_AFTER"
        else
            cat "$STUB_PREFS_BEFORE"
        fi
        exit 0
        ;;
    up)
        shift
        if [ "$*" = "--timeout=30s" ]; then
            echo "changing settings via 'tailscale up' requires mentioning all non-default flags. To proceed, either re-run your command with --reset or use the command below to explicitly mention the current value of all non-default settings: tailscale up --timeout=30s $STUB_SUGGESTED"
            exit 1
        fi
        if [ -n "$STUB_REQUIRED" ] && [ "$*" = "--timeout=30s $STUB_REQUIRED" ]; then
            touch "$STUB_STATE/cleared"
            exit 0
        fi
        echo "unexpected up args, should not have been run: $*" >&2
        exit 1
        ;;
    serve)
        exit 0
        ;;
    *)
        exit 0
        ;;
esac
STUB
chmod +x "$WORK/ts-appstore"

# Reuses the app process already running for the scenarios above (its full
# command line matches step 1's pgrep pattern), rather than starting a
# second one: the app staying up throughout is exactly the fixture step 1
# needs to stand aside in every tick below.
run_appstore_tick() {
    TAILSCALE_WATCHDOG_CLI="$WORK/ts-appstore" TAILSCALE_WATCHDOG_STATE="$ST" /bin/sh "$WATCHDOG" >/dev/null 2>&1
}

run_appstore_tick   # check 1/3
run_appstore_tick   # check 2/3
log_pre=$(cat "$ST/watchdog.log" 2>/dev/null)
assert_contains "$log_pre" "DOWN — check 1/3 failed" \
    "appstore relaunch: the first bad observation is logged"
assert_contains "$log_pre" "DOWN — check 2/3 failed" \
    "appstore relaunch: the second bad observation is logged"
case "$log_pre" in
    *ACTION:*) _premature=yes ;;
    *)         _premature=no ;;
esac
assert_eq "no" "$_premature" \
    "appstore relaunch: nothing is done before the threshold is reached"

run_appstore_tick   # check 3/3: the threshold, where step 2 must act
log_act=$(cat "$ST/watchdog.log" 2>/dev/null)
assert_contains "$log_act" "DOWN — check 3/3 failed" \
    "appstore relaunch: the third observation reaches the threshold"
case "$log_act" in
    *"ACTION: app not running"*) _step1_ran=yes ;;
    *)                            _step1_ran=no ;;
esac
assert_eq "no" "$_step1_ran" \
    "appstore relaunch: step 1 is not taken, because the app process is running"
assert_contains "$log_act" "ACTION: tailscale up --timeout=30s" \
    "appstore relaunch: step 2 runs the bare up"
assert_contains "$log_act" "RECOVERED after tailscale up" \
    "appstore relaunch: step 2's allowlisted rerun recovers the node"
case "$log_act" in
    *"STILL DOWN"*) _needed_human=yes ;;
    *)              _needed_human=no ;;
esac
assert_eq "no" "$_needed_human" \
    "appstore relaunch: recovery inside the threshold attempt never says a human is needed"

run_appstore_tick   # now healthy: must be quiet
log_quiet=$(cat "$ST/watchdog.log" 2>/dev/null)
assert_eq "$log_act" "$log_quiet" \
    "appstore relaunch: once recovered, the next tick logs nothing new"

calls=$(grep -E '^(status|debug|up|serve)' "$STUB_LOG")
expected=$(printf '%s\n' \
    "status --json" \
    "status --json" \
    "status --json" \
    "debug prefs" \
    "up --timeout=30s" \
    "up --timeout=30s --advertise-exit-node" \
    "debug prefs" \
    "serve advertise svc:a" \
    "serve advertise svc:b" \
    "serve advertise svc:c" \
    "serve advertise svc:d" \
    "status --json" \
    "status --json")
assert_eq "$expected" "$calls" \
    "appstore relaunch: the exact argv sequence across all four ticks, in order"

echo "== the node's private key never reaches anything the watchdog writes =="

# Positive control first: the same needle really is in the fixture that
# legitimately holds it, so an empty result below means "absent", not "this
# grep can't find anything."
found_in_fixture=$(grep -l "PLACEHOLDER0000000000000000000000000000000000000000000000000000" \
    "$EXITNODE_PREFS_BEFORE")
assert_contains "$found_in_fixture" "$EXITNODE_PREFS_BEFORE" \
    "sanity: the fixture itself holds the placeholder key (positive control)"

leaked=$(grep -rl "PLACEHOLDER0000000000000000000000000000000000000000000000000000" \
    "$WORK/exitnode" "$WORK/tagnode" "$WORK/defaultnode" "$WORK/badreset" "$WORK/badoutside" \
    "$WORK/badoperator" "$WORK/badexitnode" "$WORK/badtoken" 2>/dev/null)
assert_eq "" "$leaked" \
    "no file under a run's STATE directory contains the fixture's placeholder private key"

finish
