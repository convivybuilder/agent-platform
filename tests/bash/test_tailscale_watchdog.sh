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

finish
