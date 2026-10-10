#!/bin/bash
# Tests for bin/mark-fresh (ARCH-075 cost-shift markers). Run as a subprocess
# with a fake $HOME so it never writes the REAL freshness dir (a stray marker
# would silently suppress a real overnight phase).
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

MF="$REPO_ROOT/bin/mark-fresh"
FAKE_HOME=$(mktemp -d)

# valid args -> exit 0 + a correct marker
HOME="$FAKE_HOME" BUILDER_ROOT="$FAKE_HOME/Convivy" MARK_FRESH_VIA=test /bin/bash "$MF" constance bug-fix >/dev/null
assert_rc 0 "$?" "valid args exit 0"
marker="$FAKE_HOME/Convivy/agent-runs/freshness/constance.bug-fix.json"
[ -f "$marker" ]; assert_rc 0 "$?" "marker file created at expected path"
grep -q '"product": "constance"' "$marker"; assert_rc 0 "$?" "marker records product"
grep -q '"task": "bug-fix"' "$marker"; assert_rc 0 "$?" "marker records task"
grep -q '"via": "test"' "$marker"; assert_rc 0 "$?" "marker reflects MARK_FRESH_VIA"

# invalid product -> exit 2
HOME="$FAKE_HOME" BUILDER_ROOT="$FAKE_HOME/Convivy" /bin/bash "$MF" notaproduct bug-fix >/dev/null 2>&1
assert_rc 2 "$?" "invalid product exits 2"

# invalid task -> exit 2
HOME="$FAKE_HOME" BUILDER_ROOT="$FAKE_HOME/Convivy" /bin/bash "$MF" constance notatask >/dev/null 2>&1
assert_rc 2 "$?" "invalid task exits 2"

# wrong arg count -> usage exit 1
HOME="$FAKE_HOME" BUILDER_ROOT="$FAKE_HOME/Convivy" /bin/bash "$MF" constance >/dev/null 2>&1
assert_rc 1 "$?" "missing arg exits 1 (usage)"

rm -rf "$FAKE_HOME"
finish
