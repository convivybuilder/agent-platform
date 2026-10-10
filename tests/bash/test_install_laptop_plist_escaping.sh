#!/bin/bash
# install-laptop.sh interpolates CONVIVY_OPEN_BASE into the launchd plist it
# generates for the laptop's convivy-open-server. resolve-open-base.sh
# validates a value it reads fresh over ssh, but returns an existing
# CONVIVY_OPEN_BASE= line from the config file completely UNVALIDATED (an
# operator's own edit, or a stale/corrupted file, is trusted as-is) — so a
# config value carrying XML metacharacters reached the plist's <string> text
# node raw, corrupting the file's XML structure. security-reviewer finding,
# PR #561 round 1.
#
# Runs only the part of install-laptop.sh through plist generation (extracted
# up to the heredoc's closing `PLIST` line) — everything after that talks to
# the REAL launchd (`launchctl load`), which no test may do.
set -u
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

SCRIPT="$REPO_ROOT/tools/convivy-open/install-laptop.sh"
BLOCK=$(sed -n '1,/^PLIST$/p' "$SCRIPT")

setup() { # -> echoes fake HOME, with stub hostname/ssh, no pre-existing config
    local home
    home=$(mktemp -d)
    mkdir -p "$home/stubbin"
    printf '#!/bin/sh\necho laptop\n' > "$home/stubbin/hostname"
    printf '#!/bin/sh\necho ssh-should-not-run-here >&2\nexit 1\n' > "$home/stubbin/ssh"
    chmod +x "$home/stubbin/hostname" "$home/stubbin/ssh"
    # A real ~/Library/LaunchAgents always exists on the operator's Mac; the
    # extracted block (unlike the full script) never creates it.
    mkdir -p "$home/Library/LaunchAgents"
    echo "$home"
}

run_block() { # <home> -> sets RC, OUT; runs the pre-launchctl block of install-laptop.sh
    OUT=$(HOME="$1" PATH="$1/stubbin:$PATH" bash -c "$BLOCK" "$SCRIPT" 2>&1)
    RC=$?
}

echo "== a config value carrying XML metacharacters is escaped, never injected raw =="
H=$(setup)
mkdir -p "$H/.config/convivy"
printf 'CONVIVY_OPEN_BASE=/srv/convivy/mount<script>&"y\n' > "$H/.config/convivy/convivy-open.env"
run_block "$H"
assert_rc 0 "$RC" "the install block completes"
assert_eq "0" "$(printf '%s' "$OUT" | grep -c 'ssh-should-not-run-here')" \
    "an existing config value short-circuits resolve-open-base.sh before it touches ssh"
plist="$H/Library/LaunchAgents/com.convivy.openserver.plist"
assert_eq "1" "$([ -e "$plist" ] && echo 1 || echo 0)" "the plist is written"
assert_eq "0" "$(grep -c '<script>' "$plist")" "the raw tag never lands unescaped in the plist"
assert_contains "$(cat "$plist")" "&lt;script&gt;" "the angle brackets are escaped"
assert_contains "$(cat "$plist")" "&amp;" "the ampersand is escaped"
if command -v plutil >/dev/null 2>&1; then
    plutil -lint "$plist" >/dev/null 2>&1
    assert_rc 0 "$?" "the generated plist is well-formed XML"
fi
rm -rf "$H"

echo "== an ordinary path with no XML metacharacters passes through unchanged =="
H=$(setup)
mkdir -p "$H/.config/convivy"
printf 'CONVIVY_OPEN_BASE=/srv/convivy/external/builder\n' > "$H/.config/convivy/convivy-open.env"
run_block "$H"
assert_rc 0 "$RC" "the install block completes"
plist="$H/Library/LaunchAgents/com.convivy.openserver.plist"
assert_contains "$(cat "$plist")" "<string>/srv/convivy/external/builder</string>" \
    "an ordinary value is not mangled by escaping"
if command -v plutil >/dev/null 2>&1; then
    plutil -lint "$plist" >/dev/null 2>&1
    assert_rc 0 "$?" "the generated plist is well-formed XML"
fi
rm -rf "$H"

finish
