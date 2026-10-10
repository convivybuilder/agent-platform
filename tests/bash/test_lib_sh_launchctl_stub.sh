#!/bin/bash
# tests/bash/lib.sh puts a launchctl stub first on PATH for every test that
# sources it, precisely so a bash test exercising code that shells out to
# launchctl can never reach the host's real launchd (`gui/$(id -u)/<label>`
# addresses the real per-user domain no matter what HOME a test sandboxes).
# This is the harness mechanism itself under test, not one script's use of it:
# a test that forgets its own stub must fail loudly rather than silently
# reaching production, and `print` must read as "nothing loaded" rather than
# hanging or erroring in a way a caller might mistake for something else.
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

out=$(launchctl print "gui/$(id -u)/com.convivy.knowledge-watcher" 2>&1)
rc=$?
assert_rc 1 "$rc" "an unstubbed 'launchctl print' (lib.sh's default) reports the job as not loaded"
assert_eq "" "$out" "'print' produces no output, same shape as a real not-loaded label"

for verb in kickstart bootout bootstrap stop kill; do
    out=$(launchctl "$verb" -k "gui/$(id -u)/com.convivy.knowledge-watcher" 2>&1)
    rc=$?
    assert_eq "0" "$([ "$rc" -ne 0 ] && echo 0 || echo 1)" \
        "a bare 'launchctl $verb' with no test-supplied stub is refused, not silently accepted"
    assert_contains "$out" "refusing" \
        "the refusal names itself so a test relying on this would see why it failed"
done

which_launchctl=$(command -v launchctl)
assert_contains "$which_launchctl" "$(dirname "$LAUNCHCTL_STUB_LOG")" \
    "the stub, not the real /usr/bin/launchctl, is what PATH resolves first"

finish
