#!/bin/bash
# tests/bash/test_install_laptop_no_server.sh
#
# install-laptop.sh no longer installs the retired convivy-open-server
# launchd job: a laptop install must write no com.convivy.openserver plist
# and must never call launchctl about that label, while still installing
# open-md-link (the iTerm cmd-click handler) and the convivy-open CLI.
#
# hostname and osacompile are stubbed so the real install runs end to end
# against a scratch HOME; launchctl is already stubbed and logged by
# lib.sh (sourced below) for every bash test, never the host's real launchd.
# A pre-existing CONVIVY_OPEN_BASE in the config file short-circuits
# resolve-open-base.sh before it would otherwise ssh out.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

SCRIPT="$REPO_ROOT/tools/convivy-open/install-laptop.sh"

STUBBIN=$(mktemp -d)
printf '#!/bin/sh\necho laptop\n' > "$STUBBIN/hostname"
printf '#!/bin/sh\nexit 0\n' > "$STUBBIN/osacompile"
chmod +x "$STUBBIN/hostname" "$STUBBIN/osacompile"

H=$(mktemp -d)
mkdir -p "$H/.config/convivy" "$H/convivy-base"
printf 'CONVIVY_OPEN_BASE=%s\n' "$H/convivy-base" > "$H/.config/convivy/convivy-open.env"

: > "$LAUNCHCTL_STUB_LOG"
OUT=$(HOME="$H" PATH="$STUBBIN:$PATH" /bin/bash "$SCRIPT" 2>&1)
RC=$?

assert_rc 0 "$RC" "install-laptop.sh completes"

PLIST="$H/Library/LaunchAgents/com.convivy.openserver.plist"
assert_eq "0" "$([ -e "$PLIST" ] && echo 1 || echo 0)" \
    "no com.convivy.openserver plist is written"
assert_eq "" "$(cat "$LAUNCHCTL_STUB_LOG" 2>/dev/null)" \
    "launchctl is never invoked by a laptop install"
assert_eq "1" "$([ -x "$H/bin/open-md-link" ] && echo 1 || echo 0)" \
    "open-md-link is still installed and executable"
assert_eq "1" "$([ -x "$H/.local/bin/convivy-open" ] && echo 1 || echo 0)" \
    "the convivy-open CLI is still installed"
assert_eq "0" "$([ -e "$H/.local/bin/convivy-open-server" ] && echo 1 || echo 0)" \
    "the retired server binary is not installed"

rm -rf "$STUBBIN" "$H"
finish
