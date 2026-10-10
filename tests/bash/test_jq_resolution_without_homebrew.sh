#!/bin/bash
# tests/bash/test_jq_resolution_without_homebrew.sh — the four jq-resolution
# guards and codex-hook-shim.py resolve jq through `safe_which`
# (lib/safe-path.sh), which checks a fixed, ordered list of system
# directories and never reads $PATH or $HOME — so neither an empty PATH nor a
# planted `jq` ahead of the real one on PATH/HOME changes the answer. This
# test exercises the real two-line JQ= fragment extracted out of each shipped
# guard, and the real `if` condition extracted out of codex-hook-shim.py,
# rather than reimplementing either — a regression in the shipped line is
# what this catches; tests/test_safe_path.py and tests/bash/test_safe_path.sh
# cover `safe_which` itself. Run under /bin/bash (3.2.57), per ARCH-082.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

GUARDS="block-pr-merge.sh block-pkill-option-order.sh block-push-under-live-review.sh enforce-dispatch-notify.sh nudge-working-on.sh"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

for g in $GUARDS; do
    script="$REPO_ROOT/tools/$g"
    [ -f "$script" ] || { echo "  FAIL: no $script"; _FAIL=$((_FAIL + 1)); continue; }

    # Lift just the JQ= resolution line out of the guard, sourcing
    # lib/safe-path.sh first exactly as the guard itself does.
    probe="$WORK/probe.sh"
    start=$(grep -n '^JQ="\$(safe_which jq' "$script" | head -1 | cut -d: -f1)
    if [ -z "$start" ]; then
        echo "  FAIL: $g: could not find the JQ= resolution line to extract"
        _FAIL=$((_FAIL + 1))
        continue
    fi
    {
        echo 'set -u'
        echo ". '$REPO_ROOT/lib/safe-path.sh'"
        sed -n "${start}p" "$script"
        echo 'echo "JQ=$JQ"'
    } > "$probe"
    out=$(PATH="" /bin/sh "$probe" 2>&1)
    assert_eq "JQ=/usr/bin/jq" "$out" \
        "$g: jq resolves from the fixed system list with no PATH at all"

    # THE PLANT: a fake-home jq ahead of the real one on PATH must not be
    # chosen — safe_which never reads PATH or HOME, so the real /usr/bin/jq
    # is the answer regardless.
    fake_home="$WORK/fake-home-$g"
    planted_dir="$fake_home/.local/bin"
    mkdir -p "$planted_dir"
    printf '#!/bin/sh\necho ""\n' > "$planted_dir/jq"   # malicious: parses nothing
    chmod +x "$planted_dir/jq"
    probe2="$WORK/probe2-$g.sh"
    {
        echo 'set -u'
        echo ". '$REPO_ROOT/lib/safe-path.sh'"
        sed -n "${start}p" "$script"
        echo 'echo "JQ=$JQ"'
    } > "$probe2"
    out2=$(HOME="$fake_home" PATH="$planted_dir" /bin/sh "$probe2" 2>&1)
    assert_eq "JQ=/usr/bin/jq" "$out2" \
        "$g: a planted jq under a fake HOME is not chosen"

    # THE SAME PLANT, run end to end through block-pr-merge.sh specifically
    # (the one guard whose verdict a mis-parsed command directly flips): if
    # the planted jq — which parses every command as empty — were chosen
    # instead of being excluded, a real `gh pr merge` would read as no
    # command at all and sail through undenied.
    if [ "$g" = "block-pr-merge.sh" ]; then
        payload='{"tool_input":{"command":"gh pr merge 1 --squash"}}'
        out3=$(printf '%s' "$payload" | HOME="$fake_home" \
               PATH="$planted_dir:/usr/bin:/bin" \
               /bin/bash "$script" 2>&1)
        assert_contains "$out3" '"permissionDecision": "deny"' \
            "$g: a planted jq under a fake HOME must not defeat the command parse"
    fi
done

# codex-hook-shim.py carries the same check in Python. Extract the real `if`
# line (rather than reimplementing it) so this exercises what ships, not a copy
# of it.
shim="$REPO_ROOT/tools/codex-hook-shim.py"
cond=$(grep -n 'SP\.safe_which("jq")' "$shim" | head -1 | cut -d: -f2- | sed -E 's/^[[:space:]]*if (.*):$/\1/')
if [ -z "$cond" ]; then
    echo "  FAIL: codex-hook-shim.py: could not find the jq-presence condition to extract"
    _FAIL=$((_FAIL + 1))
else
    PATH="" /usr/bin/python3 -c "
import os, sys
sys.path.insert(0, '$REPO_ROOT/lib/agent-platform')
import safe_path as SP
jq_missing = $cond
assert jq_missing is False, 'expected jq to be found with no PATH at all, got jq_missing=' + repr(jq_missing)
"
    assert_rc 0 "$?" "codex-hook-shim.py: the real jq-presence condition still finds /usr/bin/jq with no PATH at all"

    # THE PLANT, same condition: a fake-home jq ahead on PATH must not satisfy
    # the condition — proven directly against the extracted line, not a copy.
    fake_home="$WORK/fake-home-shim"
    planted_dir="$fake_home/.local/bin"
    mkdir -p "$planted_dir"
    printf '#!/bin/sh\n' > "$planted_dir/jq"
    chmod +x "$planted_dir/jq"
    HOME="$fake_home" PATH="$planted_dir" /usr/bin/python3 -c "
import sys
sys.path.insert(0, '$REPO_ROOT/lib/agent-platform')
import safe_path as SP
jq_missing = $cond
assert jq_missing is False, 'a planted jq under a fake HOME must not make jq read as missing, got jq_missing=' + repr(jq_missing)
"
    assert_rc 0 "$?" "codex-hook-shim.py: a planted fake-home jq does not make the real jq read as missing"
fi

finish
