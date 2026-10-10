#!/bin/bash
# install-deck-ci-runner.sh — register the self-hosted GitHub Actions runner that
# builds convivy-deck on the mini (Deck WS0.6, gate G2).
#
# WHY SELF-HOSTED. GitHub-hosted macOS bills at 10.3x the Linux rate ($0.062 vs
# $0.006 per minute), in a month already projecting ~78,400 Actions minutes
# against a 50,000 allowance — so every Deck minute would land in overage.
# Self-hosting costs zero Actions minutes. If contention ever becomes the
# problem, moving to hosted macOS is a decision surfaced with that cost named,
# never a silent degradation.
#
# WHAT THIS RUNNER IS. One instance, running ONE job at a time, scoped to the
# convivy-deck repo ONLY. The mini already carries the MusicBrainz Postgres
# mirror, Docker, orient, the gateway, and soon a second macOS tenant; a Swift
# build that fans out would starve a 16GB machine.
#
# THE SECURITY POSTURE, stated because a runner executes PR code on this machine:
#   * Repo-scoped, not org-scoped. An org runner would execute code from every
#     repo, including ones a wider set of people can open PRs against.
#   * The repo is PRIVATE. A public repo with a self-hosted runner lets any fork
#     PR run arbitrary code on the machine — the documented worst case, and the
#     reason this script refuses to register against a public repo.
#     THIS CHECK IS INSTALL-TIME ONLY. It cannot see a later flip to public in
#     repo settings, so the Deck workflow re-asserts privacy on every job before
#     checking out PR code. That companion guard is REQUIRED, not optional; this
#     script is only half the control.
#   * CI runs AS THE OPERATOR. Any workflow step can read ~/.config/convivy/llm-gateway.env,
#     ~/.ssh, ~/.claude, and this login session's Keychain. File permissions do
#     not help — the runner is the owner. Accepted for a private repo whose only
#     PR author is the machine owner; the target state if that changes is a
#     dedicated `deck-runner` macOS user. See convivy-deck/CLAUDE.md.
#   * No repo secrets reach the runner: the workflow requests none.
#   * The runner environment never carries ANTHROPIC_API_KEY (ARCH-146). CI
#     asserts this per job so it stays true rather than being remembered.
#   * It runs in the GUI session (the PA-services precedent) because XCUITest
#     needs a window server.
#
# bash 3.2 only (ARCH-082). Absolute paths (ARCH-086). The plist install uses
# bootout + bootstrap, not kickstart (ARCH-104).
#
# Usage:
#   bash sbin/install-deck-ci-runner.sh            # install + register + start
#   bash sbin/install-deck-ci-runner.sh --check    # report state, change nothing

set -u

REPO_SLUG="convivy/convivy-deck"
RUNNER_DIR="$HOME/.local/share/actions-runner-deck"
LABELS="self-hosted,macOS,deck"
PLIST_LABEL="com.convivy.deck-ci-runner"
PLIST="$HOME/Library/LaunchAgents/$PLIST_LABEL.plist"
# Run records live under builder_root.data_root() (company/decisions/0008):
# BUILDER_ROOT when set, else the machine's root, printed by bin/builder-root.
# The path is baked into the runner's plist below, so it is resolved here, once.
_self_path="${BASH_SOURCE[0]}"; [ -L "$_self_path" ] && _self_path="$(readlink "$_self_path")"
_HERE="$(dirname "$_self_path")"
DATA_ROOT="${BUILDER_ROOT:-$("$_HERE/../bin/builder-root")}" || exit 1
# shellcheck source=/dev/null
. "$_HERE/../lib/safe-path.sh"
# `safe_which`, not a bare `command -v`: it checks a fixed list of system
# directories, never the invoking shell's PATH, so there is no PATH for a
# planted `gh` under the running account's home or a worktree to ever be
# chosen (gh holds this human's GitHub auth — platform/decisions/0090's
# Handoff).
GH="$(safe_which gh || true)"
LOG_DIR="$DATA_ROOT/agent-runs"

say()  { printf '\033[1;34m▸\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m!\033[0m %s\n' "$1" >&2; }
die()  { printf '\033[1;31m✗\033[0m %s\n' "$1" >&2; exit 1; }

CHECK_ONLY=0
for a in "$@"; do
  [ "$a" = "--check" ] && CHECK_ONLY=1
done

# --- preconditions ----------------------------------------------------------

[ -n "$GH" ] && [ -x "$GH" ] || die "gh not found on PATH"

VIS=$("$GH" repo view "$REPO_SLUG" --json visibility --jq .visibility 2>/dev/null) \
  || die "cannot read $REPO_SLUG — is gh authenticated?"

# THE REFUSAL THAT MATTERS. A self-hosted runner on a PUBLIC repo executes code
# from any fork's pull request. That is arbitrary code execution on the mini by
# anyone on the internet, and it is the single documented way this setup becomes
# catastrophic rather than merely risky.
if [ "$VIS" != "PRIVATE" ]; then
  die "$REPO_SLUG is $VIS. Refusing: a self-hosted runner on a public repo runs fork-PR code on this machine."
fi
say "Repo $REPO_SLUG is PRIVATE — ok."

ARCH=$(/usr/bin/uname -m)
case "$ARCH" in
  arm64) RUNNER_ARCH="osx-arm64" ;;
  x86_64) RUNNER_ARCH="osx-x64" ;;
  *) die "unsupported arch: $ARCH" ;;
esac

if [ "$CHECK_ONLY" -eq 1 ]; then
  say "Check only — nothing will change."
  echo "  runner dir : $RUNNER_DIR $([ -d "$RUNNER_DIR" ] && echo '(present)' || echo '(absent)')"
  echo "  plist      : $PLIST $([ -f "$PLIST" ] && echo '(present)' || echo '(absent)')"
  # By name, not total_count: a count cannot tell you whether THIS runner is
  # registered if the repo ever gains another.
  echo "  registered : $("$GH" api "repos/$REPO_SLUG/actions/runners" --jq '[.runners[] | select(.name == "mini-deck")] | length' 2>/dev/null || echo '?')  (named mini-deck)"
  echo "  job state  : $(/bin/launchctl print "gui/$(/usr/bin/id -u)/$PLIST_LABEL" 2>/dev/null | /usr/bin/grep -E '^\s+state' || echo 'not loaded')"
  exit 0
fi

# Deliberately AFTER the --check exit: inspecting state must work from a normal
# dev shell. The guard is about what gets baked into the runner environment at
# install, and --check installs nothing.
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  die "ANTHROPIC_API_KEY is set in this shell. Installing from here would risk baking it into the runner env (ARCH-146)."
fi

# --- runner software --------------------------------------------------------

# Test for a concrete binary, not the directory. A failed download or extract
# leaves the dir present but empty, and a directory-existence check would then
# skip the download and fail confusingly at ./config.sh instead of recovering.
if [ ! -x "$RUNNER_DIR/run.sh" ]; then
  say "Downloading the runner ($RUNNER_ARCH)"
  /bin/rm -rf "$RUNNER_DIR"
  /bin/mkdir -p "$RUNNER_DIR" || die "cannot create $RUNNER_DIR"
  VER=$("$GH" api repos/actions/runner/releases/latest --jq '.tag_name' | /usr/bin/sed 's/^v//')
  [ -n "$VER" ] || die "could not resolve the latest runner version"
  URL="https://github.com/actions/runner/releases/download/v${VER}/actions-runner-${RUNNER_ARCH}-${VER}.tar.gz"
  # Remove the half-built dir on failure so the next run re-downloads rather
  # than inheriting the wreckage.
  /usr/bin/curl -fsSL "$URL" -o "$RUNNER_DIR/runner.tar.gz" \
    || { /bin/rm -rf "$RUNNER_DIR"; die "download failed: $URL"; }
  /usr/bin/tar -xzf "$RUNNER_DIR/runner.tar.gz" -C "$RUNNER_DIR" \
    || { /bin/rm -rf "$RUNNER_DIR"; die "extract failed"; }
  /bin/rm -f "$RUNNER_DIR/runner.tar.gz"
  [ -x "$RUNNER_DIR/run.sh" ] || { /bin/rm -rf "$RUNNER_DIR"; die "archive did not contain run.sh"; }
  say "Runner $VER unpacked into $RUNNER_DIR"
else
  say "Runner already unpacked — reusing $RUNNER_DIR"
fi

# --- registration -----------------------------------------------------------

if [ ! -f "$RUNNER_DIR/.runner" ]; then
  say "Registering with $REPO_SLUG"
  # A registration token is short-lived (about an hour) and single-use. It is
  # passed on argv to the vendor's config script, which is how that script takes
  # it; it is never written to disk by us and never echoed.
  TOKEN=$("$GH" api -X POST "repos/$REPO_SLUG/actions/runners/registration-token" --jq '.token') \
    || die "could not mint a registration token"
  [ -n "$TOKEN" ] || die "empty registration token"
  ( cd "$RUNNER_DIR" && ./config.sh \
      --url "https://github.com/$REPO_SLUG" \
      --token "$TOKEN" \
      --name "mini-deck" \
      --labels "$LABELS" \
      --work "_work" \
      --unattended \
      --replace ) || die "runner registration failed"
  unset TOKEN
  say "Registered as 'mini-deck' with labels: $LABELS"
else
  say "Already registered — leaving the existing registration alone"
fi

# --- launchd (GUI session: XCUITest needs a window server) -------------------

say "Installing $PLIST_LABEL"
/bin/mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"

# The runner's job needs gh's directory on PATH too; derived from where this
# install found it (above) rather than hardcoded, so this works on either
# Homebrew prefix.
GH_DIR="$(dirname "$GH")"

/bin/cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PLIST_LABEL</string>

    <key>ProgramArguments</key>
    <array>
        <string>$RUNNER_DIR/run.sh</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$RUNNER_DIR</string>

    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <!-- launchd's default 10s throttle would restart a crash-looping run.sh six
         times a minute, each time re-reading whatever job state broke it. 60s
         bounds that without masking a genuine outage. -->
    <key>ThrottleInterval</key>
    <integer>60</integer>

    <key>StandardOutPath</key>
    <string>$LOG_DIR/deck-ci-runner.out.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/deck-ci-runner.err.log</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$GH_DIR:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>HOME</key>
        <string>$HOME</string>
    </dict>
</dict>
</plist>
PLISTEOF

/usr/bin/plutil -lint "$PLIST" >/dev/null || die "generated plist is malformed"

# EnvironmentVariables changes need bootout + bootstrap; kickstart reuses the old
# job definition (ARCH-104).
/bin/launchctl bootout "gui/$(/usr/bin/id -u)/$PLIST_LABEL" 2>/dev/null
/bin/launchctl bootstrap "gui/$(/usr/bin/id -u)" "$PLIST" || die "bootstrap failed"

say "Installed."
warn "The workflow-side privacy assertion in convivy-deck is REQUIRED — this"
warn "script's refusal is install-time only and cannot see a later public flip."
say "Verify:"
echo "  launchctl print gui/\$(id -u)/$PLIST_LABEL | grep -E 'state|pid'"
echo "  gh api repos/$REPO_SLUG/actions/runners --jq '.runners[] | \"\\(.name) \\(.status)\"'"
echo "  tail -f $LOG_DIR/deck-ci-runner.out.log"
