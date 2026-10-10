#!/bin/bash
# tests/bash/test_install_deck_ci_runner_gh_resolution.sh — sbin/install-deck-
# ci-runner.sh's GH= line resolves through lib/safe-path.sh's `safe_which`,
# not a bare `command -v gh` (platform/decisions/0090's Handoff): `gh` holds
# whoever runs this installer's GitHub auth, so a planted `gh` ahead on PATH,
# or an empty PATH, must not change the answer — `safe_which` checks a fixed
# list of system directories and never reads $PATH or $HOME at all. Run under
# /bin/bash (3.2.57), per ARCH-082.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"
# shellcheck source=/dev/null
. "$REPO_ROOT/lib/safe-path.sh"

SCRIPT="$REPO_ROOT/sbin/install-deck-ci-runner.sh"
[ -f "$SCRIPT" ] || { echo "  FAIL: no $SCRIPT"; _FAIL=$((_FAIL + 1)); finish; exit 1; }

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

start=$(grep -n '^GH="\$(safe_which gh' "$SCRIPT" | head -1 | cut -d: -f1)
if [ -z "$start" ]; then
    echo "  FAIL: could not find the GH= resolution line to extract"
    _FAIL=$((_FAIL + 1))
    finish
    exit 1
fi
probe="$WORK/probe.sh"
{
    echo 'set -u'
    echo ". '$REPO_ROOT/lib/safe-path.sh'"
    sed -n "${start}p" "$SCRIPT"
    echo 'echo "GH=$GH"'
} > "$probe"

# --- the fixed list, with no PATH at all -------------------------------------
#
# No literal path here: a Homebrew install sits at /opt/homebrew/bin on
# Apple Silicon, /usr/local/bin on Intel, and the GitHub Actions macOS
# runner puts `gh` somewhere else again (round 13 — this test first ran on
# that runner once the PR left draft, and the literal was wrong there). The
# expectation is computed the same way `safe_which` resolves: the first
# `_safe_path_system_dirs` entry holding an executable regular file named
# `gh`.

expected_gh=$(safe_which gh); have_gh=$?
if [ "$have_gh" -ne 0 ]; then
    echo "  SKIP: gh is not on any _safe_path_system_dirs() entry on this machine"
else
    out=$(PATH="" /bin/sh "$probe" 2>&1)
    assert_eq "GH=$expected_gh" "$out" \
        "install-deck-ci-runner.sh: gh resolves from the fixed system list with no PATH at all"

    # --- the plant: a fake-home gh ahead on PATH is not chosen ---------------

    fake_home="$WORK/fake-home"
    planted_dir="$fake_home/.local/bin"
    mkdir -p "$planted_dir"
    printf '#!/bin/sh\necho planted\n' > "$planted_dir/gh"
    chmod +x "$planted_dir/gh"
    out=$(HOME="$fake_home" PATH="$planted_dir" /bin/sh "$probe" 2>&1)
    case "$out" in
        "GH=$fake_home"*)
            _FAIL=$((_FAIL + 1))
            echo "  FAIL: install-deck-ci-runner.sh: a planted gh under a fake HOME was chosen: $out"
            ;;
        *)
            _PASS=$((_PASS + 1))
            ;;
    esac
    assert_eq "GH=$expected_gh" "$out" \
        "install-deck-ci-runner.sh: a planted gh under a fake HOME still resolves to the real gh"
fi

finish
