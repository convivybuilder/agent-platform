#!/bin/bash
# Tests for the $0 symlink-resolution hop cap duplicated across
# bin/appkit-autoupdate, bin/knowledge-autopull, bin/launch-weekly-curation,
# bin/launch-weekly-kb-audit and bin/arch129-remeasure (round 7, both
# reviewers). The cap stops the readlink loop that resolves each script's own
# location from spinning forever when $0's symlink chain cycles. Nothing
# pinned it before this file: `grep -n 'hops\|cycle' tests/bash/*.sh
# tests/*.py` found no match, so a future refactor of that loop could drop the
# cap and nothing would catch it.
#
# WHAT THIS TEST CAN AND CANNOT CLAIM. A script cannot execute THROUGH a live
# symlink cycle: any open() on a path that resolves through a genuine cycle is
# rejected by the kernel with ELOOP before the script body ever runs (both
# reviewers confirmed this independently in round 7). So the case this test
# drives — installing one of these scripts behind a real cyclic symlink and
# running it normally — is not reachable at all; the kernel refuses the
# invocation on its own. What the hop cap actually protects against is $0
# arriving as a string not backed by kernel-resolved filesystem symlinks (set
# directly, or inherited from a caller), or a symlink chain mutated mid-walk
# so that no single `readlink` call — which reads one level at a time, unlike
# open() — ever sees the whole cycle at once. This test drives exactly that:
# it builds a real two-file symlink cycle (a -> b -> a) on disk, then runs the
# script via `bash -c`, which assigns $0 from an argument rather than opening
# it as a file. The cycle's symlinks are only ever touched one `readlink` hop
# at a time this way, so ELOOP never fires and the script's own loop is what
# has to stop the spin. A claim that this test proves the cap prevents a hang
# during a normal, kernel-resolved invocation would be false.
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

SCRIPTS="appkit-autoupdate knowledge-autopull launch-weekly-curation launch-weekly-kb-audit arch129-remeasure"

# Run "$@" in the background, bounded to <secs>, output captured to <outfile>.
# Echoes the child's exit code. No dependency on GNU coreutils' `timeout`,
# which is not guaranteed to exist on every runner this suite targets — a
# background job plus a `sleep`-and-`kill` watchdog is portable to bash 3.2.
#
# The watchdog subshell redirects its own stdout/stderr to /dev/null rather
# than inheriting the caller's: the caller reads this function's result via
# `rc=$(_bounded_run ...)`, and a background job that still holds a copy of
# that pipe's write end keeps the pipe open — and the command substitution
# blocking on it — for the watchdog's full sleep, even after $pid has long
# since exited and this function has echoed its result.
_bounded_run() { # <secs> <outfile> <cmd...>
    local secs=$1 outfile=$2
    shift 2
    "$@" >"$outfile" 2>&1 &
    local pid=$!
    ( sleep "$secs"; kill -9 "$pid" 2>/dev/null ) >/dev/null 2>&1 &
    local watchdog=$!
    wait "$pid" 2>/dev/null
    local rc=$?
    kill "$watchdog" 2>/dev/null
    wait "$watchdog" 2>/dev/null
    echo "$rc"
}

for name in $SCRIPTS; do
    SCRIPT="$REPO_ROOT/bin/$name"
    DIR=$(mktemp -d)
    ln -s "$DIR/b" "$DIR/a"
    ln -s "$DIR/a" "$DIR/b"

    OUT="$DIR/out.log"
    rc=$(_bounded_run 5 "$OUT" /bin/bash -c "$(cat "$SCRIPT")" "$DIR/a")

    assert_eq "1" "$rc" \
        "$name: a real two-file \$0 symlink cycle (a -> b -> a) exits non-zero via the hop cap instead of hanging"
    assert_contains "$(cat "$OUT")" "symlink cycle" \
        "$name: the abort message names the symlink cycle"

    rm -rf "$DIR"
done

echo "  $_PASS passed, $_FAIL failed"
[ "$_FAIL" -eq 0 ]
