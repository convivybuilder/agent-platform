#!/bin/bash
#
# install-sshd-watchdog.sh — root-side install for the sshd watchdog (ARCH-310).
#
# The watchdog's fix path (systemsetup + a system-domain job) needs root, so its
# script must be root-owned and its plist must live in /Library/LaunchDaemons.
# This is the one privileged step; the user-context notify half installs through
# the normal install.sh (symlink) + gui-domain bootstrap.
#
# Run once:  sudo bash sbin/install-sshd-watchdog.sh
#
# bash 3.2-safe (ARCH-082). Idempotent — re-running re-copies + re-bootstraps.

set -eu

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PLIST=/Library/LaunchDaemons/com.convivy.sshd-watchdog.plist

if [ "$(id -u)" != "0" ]; then
  echo "must run as root: sudo bash sbin/install-sshd-watchdog.sh" >&2
  exit 2
fi

mkdir -p /usr/local/sbin /var/log/sshd-watchdog/events
# Set perms explicitly rather than trusting the ambient umask: a world-writable
# events dir would let a non-root user drop fake event files (false Slack alerts)
# or pre-seed a marker filename to suppress a real one.
chmod 755 /var/log/sshd-watchdog /var/log/sshd-watchdog/events

# Root-owned copy of the daemon script (NOT a symlink into the user repo tree).
install -o root -g wheel -m 755 "$REPO/sbin/sshd-watchdog" /usr/local/sbin/sshd-watchdog
install -o root -g wheel -m 644 "$REPO/launchd/com.convivy.sshd-watchdog.plist" "$PLIST"

launchctl bootout system/com.convivy.sshd-watchdog 2>/dev/null || true
launchctl bootstrap system "$PLIST"

echo "sshd-watchdog installed + bootstrapped into the system domain."
launchctl print system/com.convivy.sshd-watchdog 2>/dev/null | grep -E 'state|program|path' || true
