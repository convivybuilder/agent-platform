#!/bin/bash
# tests/bash/test_python3_resolution_in_live_review_guard.sh —
# tools/block-push-under-live-review.sh resolves python3 through
# `safe_which` (lib/safe-path.sh), which checks a fixed, ordered list of
# system directories and never reads $PATH or $HOME — so neither an empty
# PATH nor a planted `python3` ahead of the real one on PATH/HOME changes
# the answer. Round 5: this hook moved jq's resolution to safe_which in an
# earlier round but still resolved python3 via `command -v`, which a
# dispatched agent could redirect by planting a `python3` ahead of the real
# one on the operator's interactive PATH (e.g. /opt/homebrew/bin,
# ~/.local/bin). This exercises the real PY= line extracted out of the
# shipped guard, not a reimplementation. Run under /bin/bash (3.2.57), per
# ARCH-082.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

SCRIPT="$REPO_ROOT/tools/block-push-under-live-review.sh"
[ -f "$SCRIPT" ] || { echo "  FAIL: no $SCRIPT"; _FAIL=$((_FAIL + 1)); finish; exit $?; }

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# Lift just the PY= resolution line out of the guard, sourcing
# lib/safe-path.sh first exactly as the guard itself does.
start=$(grep -n '^PY="\$(' "$SCRIPT" | head -1 | cut -d: -f1)
if [ -z "$start" ]; then
    echo "  FAIL: block-push-under-live-review.sh: could not find the PY= resolution line to extract"
    _FAIL=$((_FAIL + 1))
else
    probe="$WORK/probe.sh"
    {
        echo 'set -u'
        echo ". '$REPO_ROOT/lib/safe-path.sh'"
        sed -n "${start}p" "$SCRIPT"
        echo 'echo "PY=$PY"'
    } > "$probe"
    out=$(PATH="" /bin/sh "$probe" 2>&1)
    assert_eq "PY=/usr/bin/python3" "$out" \
        "block-push-under-live-review.sh: python3 resolves from the fixed system list with no PATH at all"

    # THE PLANT: a fake-home python3 ahead of the real one on PATH must not
    # be chosen — safe_which never reads PATH or HOME, so the real
    # /usr/bin/python3 is the answer regardless. A malicious plant here would
    # otherwise run inside the git-push guard with the operator's privileges,
    # or silently defeat the ARCH-374 live-review check by printing nothing.
    fake_home="$WORK/fake-home"
    planted_dir="$fake_home/.local/bin"
    mkdir -p "$planted_dir"
    marker="$WORK/planted-python3-ran"
    printf '#!/bin/sh\ntouch "%s"\n' "$marker" > "$planted_dir/python3"
    chmod +x "$planted_dir/python3"
    probe2="$WORK/probe2.sh"
    {
        echo 'set -u'
        echo ". '$REPO_ROOT/lib/safe-path.sh'"
        sed -n "${start}p" "$SCRIPT"
        echo 'echo "PY=$PY"'
    } > "$probe2"
    out2=$(HOME="$fake_home" PATH="$planted_dir" /bin/sh "$probe2" 2>&1)
    assert_eq "PY=/usr/bin/python3" "$out2" \
        "block-push-under-live-review.sh: a planted python3 under a fake HOME is not chosen"

    # THE SAME PLANT, run end to end through the real guard on a real `git
    # push` command: the planted python3 writes a marker file if it is ever
    # exec'd. If the guard still resolved python3 via `command -v` (PATH), it
    # would run the planted interpreter — with the operator's own privileges,
    # inside an interactive git push — instead of the real one.
    rm -f "$marker"
    printf '{"tool_input":{"command":"git push"}}' | \
        HOME="$fake_home" PATH="$planted_dir:/usr/bin:/bin" \
        /bin/bash "$SCRIPT" >/dev/null 2>&1
    if [ -e "$marker" ]; then
        _FAIL=$((_FAIL + 1))
        echo "  FAIL: block-push-under-live-review.sh: the planted python3 ran inside the real guard"
    else
        _PASS=$((_PASS + 1))
    fi
fi

finish
