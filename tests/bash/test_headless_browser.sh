#!/bin/bash
# tests/bash/test_headless_browser.sh — the URL handler records the link, and its
# lock does not eat somebody else's.
#
# This handler is the only place a URL goes on a machine with no browser, so a
# line it loses is a login nobody can complete. Its locking has been wrong in
# three consecutive review rounds — released unconditionally, then released on
# "did I ever create it" rather than "do I still own it" — and none of it could
# be tested, because the shell lives inside an AppleScript string and the only
# thing it would ever write to was the live log.
#
# Two things make it testable: the directory is a parameter of `record_to`, which
# `on open location` calls with the real path, and the compiled handler can be
# invoked directly rather than only by LaunchServices delivering a URL. So these
# drive the REAL handler, not a shell copy of it — a copy would pass while the
# thing on disk was broken, which is the failure this whole file exists to catch.
#
# A PARAMETER RATHER THAN AN ENVIRONMENT VARIABLE, deliberately. The first version
# read the directory from `HEADLESS_BROWSER_STATE`, which a reviewer pointed out
# any process running as this user could set once with `launchctl setenv` — no
# filesystem write, nothing restarted — to redirect every future OAuth code out of
# the log, silently. Making the tests easier to write is not a reason to give the
# deployed handler an input the deployed handler does not need.
#
# Run under /bin/bash (3.2.57), per ARCH-082.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

SRC="$REPO_ROOT/headless/HeadlessBrowser.applescript"

[ -f "$SRC" ] || {
    echo "  FAIL: no $SRC"
    _FAIL=$((_FAIL + 1))
    finish
    exit 1
}

# osacompile and osascript are macOS-only, and `do shell script` needs a usable
# scripting environment. A runner without one SKIPS loudly rather than failing:
# reddening CI over an environment difference is worse than saying plainly that
# this guard went unverified here. The bash job runs on macOS whenever code
# changed, which is the case that matters.
if ! command -v osacompile >/dev/null 2>&1 || ! osascript -e 'return 1' >/dev/null 2>&1; then
    echo "  SKIP: no usable osacompile/osascript here — the URL handler was NOT verified"
    finish
    exit 0
fi

WORK=$(mktemp -d)
SCPT="$WORK/handler.scpt"
trap 'rm -rf "$WORK"' EXIT

osacompile -o "$SCPT" "$SRC" 2>"$WORK/compile.err" || {
    echo "  FAIL: the handler does not compile"
    cat "$WORK/compile.err"
    _FAIL=$((_FAIL + 1))
    finish
    exit 1
}
_PASS=$((_PASS + 1))   # it compiles

# The directory is a PARAMETER, never an environment variable. `record_to` is
# what `on open location` calls with the real path, so these drive the same code
# the deployed handler runs while naming their own scratch directory — and
# nothing outside the .applescript can move where a delivered URL is written.
deliver() { # <state dir> <url>
    osascript \
        -e "set h to load script POSIX file \"$SCPT\"" \
        -e "tell h to record_to(\"$2\", \"$1\")" >/dev/null 2>&1
}

echo "== the link is recorded, and only this user can read it =="

ST="$WORK/s1"
deliver "$ST" "https://example.com/auth?code=abc123&state=xyz"
assert_contains "$(cat "$ST/urls.log" 2>/dev/null)" "https://example.com/auth?code=abc123&state=xyz" \
    "the URL is recorded verbatim, query string and all"
assert_eq "700" "$(stat -f '%Lp' "$ST" 2>/dev/null)" "the directory is not readable by anybody else"
assert_eq "600" "$(stat -f '%Lp' "$ST/urls.log" 2>/dev/null)" "nor is the file"

# The URL is attacker-adjacent — whatever asked for a browser chose it — and it
# reaches a shell. It must arrive as text rather than as syntax.
ST="$WORK/s2"
deliver "$ST" 'https://example.com/x?q=$(touch /tmp/hb-pwned);`id`&z=1'
if [ -e /tmp/hb-pwned ]; then
    _FAIL=$((_FAIL + 1)); echo "  FAIL: a URL executed shell it should have been quoted out of"
    rm -f /tmp/hb-pwned
else
    _PASS=$((_PASS + 1))
fi
assert_contains "$(cat "$ST/urls.log" 2>/dev/null)" 'touch /tmp/hb-pwned' \
    "and it was still written down literally"

echo "== the log stays bounded =="

ST="$WORK/s3"; mkdir -p "$ST"
i=0
while [ $i -lt 205 ]; do echo "seed line $i" >> "$ST/urls.log"; i=$((i + 1)); done
deliver "$ST" "https://example.com/last"
lines=$(wc -l < "$ST/urls.log" | tr -d ' ')
assert_eq "200" "$lines" "the log is capped rather than growing forever"
assert_contains "$(tail -1 "$ST/urls.log")" "https://example.com/last" "and the newest line survives the trim"

echo "== the lock is not taken from whoever holds it =="

# A lock somebody else holds, recently. This handler cannot take it, waits its
# bounded five seconds, and writes anyway — losing the link is worse than racing
# the trim. What it must NOT do is remove a lock it never owned, which is how a
# third handler ends up inside somebody's critical section.
ST="$WORK/s4"; mkdir -p "$ST/.lock"
echo 999999 > "$ST/.lock/owner"
deliver "$ST" "https://example.com/impatient"
assert_contains "$(cat "$ST/urls.log" 2>/dev/null)" "https://example.com/impatient" \
    "a handler that cannot take the lock still records the link"
if [ -d "$ST/.lock" ]; then
    _PASS=$((_PASS + 1))
else
    _FAIL=$((_FAIL + 1)); echo "  FAIL: a handler removed a lock it never took"
fi
assert_eq "999999" "$(cat "$ST/.lock/owner" 2>/dev/null)" "the holder's token is left alone"

# An abandoned lock is recovered by age, because tracking ownership removed the
# accidental recovery that releasing unconditionally used to provide. Without
# this, one killed handler makes every later one wait the full five seconds
# forever.
ST="$WORK/s5"; mkdir -p "$ST/.lock"
echo 999999 > "$ST/.lock/owner"
touch -t 202001010000 "$ST/.lock"
deliver "$ST" "https://example.com/after-orphan"
assert_contains "$(cat "$ST/urls.log" 2>/dev/null)" "https://example.com/after-orphan" \
    "an orphaned lock does not stop the link being recorded"
if [ -d "$ST/.lock" ]; then
    _FAIL=$((_FAIL + 1)); echo "  FAIL: the orphaned lock was never taken over, or never released"
else
    _PASS=$((_PASS + 1))
fi

finish
