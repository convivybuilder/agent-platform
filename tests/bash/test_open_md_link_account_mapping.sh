#!/bin/bash
# tests/bash/test_open_md_link_account_mapping.sh — open-md-link maps a
# mini-supplied path to its laptop SMB mirror using the ACCOUNT NAME IN THE
# PATH ITSELF, not this laptop's own $HOME (platform/decisions/0090: under
# the two-account design the mini can run as a different account than this
# laptop does, e.g. mini path /Users/exampleuser/... while this laptop's $HOME
# names a different account — the old `"$HOME"/*` match would then simply not fire).
#
# open-md-link is a zsh script (iTerm2 Semantic History handler) driving
# Typora/osascript for its full job, so this test lifts just the path-mapping
# block out and runs it under /bin/zsh — the same extraction style
# tests/bash/test_jq_resolution_without_homebrew.sh uses, exercising what
# ships rather than a reimplementation of it. Run under /bin/bash (3.2.57),
# per ARCH-082 (this harness file itself, not the zsh code it drives).

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

SCRIPT="$REPO_ROOT/tools/convivy-open/open-md-link"
[ -f "$SCRIPT" ] || { echo "  FAIL: no $SCRIPT"; _FAIL=$((_FAIL + 1)); finish; exit 1; }

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

start=$(grep -n '^_this_home=' "$SCRIPT" | head -1 | cut -d: -f1)
end=$(awk -v s="$start" 'NR>=s && /^esac$/ {print NR; exit}' "$SCRIPT")
if [ -z "$start" ] || [ -z "$end" ]; then
    echo "  FAIL: could not find the account-mapping block to extract"
    _FAIL=$((_FAIL + 1))
    finish
    exit 1
fi

probe="$WORK/probe.zsh"
{
    echo 'emulate -L zsh'
    echo 'set -u'
    echo 'remote_path="$1"'
    sed -n "${start},${end}p" "$SCRIPT"
    echo 'printf "%s\n" "$local_path"'
} > "$probe"

# This machine's real home root (almost certainly /Users on macOS), read the
# same way the script itself does — not asserted against a literal, so this
# test is not itself a new machine-specific-path offender.
real_home_root=$(/usr/bin/dscacheutil -q user -a name "$(/usr/bin/whoami)" 2>/dev/null \
    | /usr/bin/awk '/^dir: / {print $2}' | xargs dirname)
[ -z "$real_home_root" ] && real_home_root="$(dirname "$HOME")"

# --- the plant: a DIFFERENT account than this laptop's own $HOME -----------

mini_path="$real_home_root/exampleuser/Convivy/doc.md"
out=$(/bin/zsh "$probe" "$mini_path")
assert_eq "/Volumes/exampleuser/Convivy/doc.md" "$out" \
    "a mini path naming a different account than \$HOME still maps (not left unmapped)"

# --- same account name as $HOME still works (the common case today) -------

mini_path2="$real_home_root/$(/usr/bin/whoami)/Convivy/doc.md"
out2=$(/bin/zsh "$probe" "$mini_path2")
assert_eq "/Volumes/$(/usr/bin/whoami)/Convivy/doc.md" "$out2" \
    "a mini path naming this same account still maps correctly"

# --- a path outside any account home is passed through unchanged -----------

out3=$(/bin/zsh "$probe" "/tmp/not-under-a-home/doc.md")
assert_eq "/tmp/not-under-a-home/doc.md" "$out3" \
    "a path outside any account home is passed through, not guessed at"

finish
