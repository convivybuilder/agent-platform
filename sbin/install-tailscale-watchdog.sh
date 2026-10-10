#!/bin/bash
#
# install-tailscale-watchdog.sh — user-domain install for the Tailscale watchdog.
#
# WHY THIS FILE EXISTS AT ALL. launchd creates the FILE named by StandardOutPath
# and StandardErrorPath, but not the directories above it. The watchdog's plist
# points both at ~/.local/state/tailscale-watchdog/launchd.log, so on a machine
# where that directory does not exist yet, bootstrapping the job can fail before
# the script's own `mkdir -p` ever runs. The script cannot create the directory
# it needs in order to be started, so something outside it has to.
#
# It installs into the GUI domain rather than the system one: this watchdog runs
# `open -a Tailscale`, which needs the user's session, and it needs no root.
# That is the whole difference from install-sshd-watchdog.sh, whose fix path does
# need root.
#
# Run once:  bash sbin/install-tailscale-watchdog.sh
#
# bash 3.2-safe (ARCH-082). Idempotent — re-running re-bootstraps.

set -eu

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LABEL=com.convivy.tailscale-watchdog
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
STATE="$HOME/.local/state/tailscale-watchdog"

if [ "$(id -u)" = "0" ]; then
  echo "do not run this as root: the job belongs in your GUI domain, not the system one" >&2
  exit 2
fi

# The directory launchd will not create for itself. 700 because the log records
# when this machine was unreachable and what the Tailscale CLI said about it,
# which is nobody else's business on a shared box.
mkdir -p "$STATE" "$HOME/Library/LaunchAgents" "$HOME/.local/bin"
chmod 700 "$STATE"

# The plist names ~/.local/bin/tailscale-watchdog, and nothing else puts it
# there. Without this the install bootstraps a job pointing at a file that does
# not exist, which is the same shape as the missing state directory above: an
# install that reports success and leaves a job that cannot run.
#
# A SYMLINK INTO THE PRIMARY CHECKOUT, which is how every other bin here is
# installed — agentctl and launch-overnight are both symlinks to
# agent-platform/bin. That also means the deployed watchdog tracks the primary
# checkout, so it must stay on main; a copy instead would silently keep running
# whatever version was current the day somebody ran this.
if [ ! -f "$REPO/bin/tailscale-watchdog" ]; then
  echo "no $REPO/bin/tailscale-watchdog — run this from the checkout that has it" >&2
  exit 2
fi

# ONLY FROM THE PRIMARY CHECKOUT. The symlink below makes whatever that checkout
# has on disk the machine's live watchdog, so installing from a worktree points
# the job at a feature branch — unmerged code guarding the only route into a
# machine nobody can walk up to. link-farm.sh refuses the same thing for the same
# reason, after the 2026-07-30 incident where 38 of 40 links pointed at a
# feature-branch worktree; this is the identical test, and the reason it is here
# is that installing from a worktree is easy to do by accident.
#
# Fails closed: a non-git copy, a subdirectory, or anything git cannot resolve
# refuses rather than install from something unverifiable.
is_primary_checkout() {
  local git_dir common_dir toplevel
  unset GIT_DIR GIT_COMMON_DIR GIT_WORK_TREE
  git_dir=$(git -C "$REPO" rev-parse --path-format=absolute --git-dir 2>/dev/null) || return 1
  common_dir=$(git -C "$REPO" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || return 1
  toplevel=$(git -C "$REPO" rev-parse --show-toplevel 2>/dev/null) || return 1
  [ "$git_dir" = "$common_dir" ] && [ "$(cd "$REPO" && pwd -P)" = "$toplevel" ]
}

if ! is_primary_checkout; then
  # $REPO need not be a valid checkout here, so resolving its sibling
  # bin/builder-root (company/decisions/0008) is best-effort, not a fixed
  # HOME-derived guess.
  _root_hint="$("$REPO/bin/builder-root" --root 2>/dev/null || true)"
  echo "install-tailscale-watchdog.sh: REFUSING to install from $REPO." >&2
  echo "  That is not the primary checkout of agent-platform — a linked worktree," >&2
  echo "  a non-git copy, or a checkout git cannot verify. The symlink this would" >&2
  echo "  write makes that checkout's working tree the live watchdog, so a feature" >&2
  echo "  branch becomes the guard on the only route into this machine." >&2
  if [ -n "$_root_hint" ]; then
    echo "  Run it from $_root_hint/agent-platform, on main." >&2
  else
    echo "  Run it from the primary checkout of agent-platform (its root cannot be" >&2
    echo "  resolved from here), on main." >&2
  fi
  exit 2
fi

# Back up a REAL file found at the target before replacing it, the way
# link-farm.sh does for the same situation. This machine is in exactly that
# state: a hand-placed copy of an older watchdog sits there now, and `ln -sfn`
# would remove it with nothing kept. The pointer itself is disposable, but the
# asymmetry with the script whose primary-checkout test this otherwise copies
# verbatim is not worth keeping.
TARGET="$HOME/.local/bin/tailscale-watchdog"
if [ -e "$TARGET" ] && [ ! -L "$TARGET" ]; then
  cp -p "$TARGET" "$TARGET.preinstall.$(date +%s)"
  echo "  kept the previous copy at $TARGET.preinstall.*"
fi
ln -sfn "$REPO/bin/tailscale-watchdog" "$TARGET"

install -m 644 "$REPO/launchd/$LABEL.plist" "$PLIST"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "tailscale-watchdog installed + bootstrapped into the GUI domain."
echo "  state:  $STATE"
echo "  log:    $STATE/watchdog.log"
launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | grep -E 'state|program|path' || true
