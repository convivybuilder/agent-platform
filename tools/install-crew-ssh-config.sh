#!/usr/bin/env bash
# install-crew-ssh-config.sh — run on the LAPTOP to install/refresh the ssh host
# aliases and mosh launchers that connect to each crew's tmux session, one per
# project the registry names (CQ-603, E36 slice 6).
#
# The crew list, the per-crew directory and style, the crew machine's own
# hostname, the standby machine's own hostname and the ssh login name are
# never literals in this script: they are rendered on the crew machine by
# bin/render-ssh-config (from the project registry's own `crew`/`home_dir`
# fields plus that machine's own operator config — install.sh phase 5b) into
# the generated co-style directory this script reads over the mounted
# volume, under `ssh/`. This script's own job is installing what it finds
# there, plus learning this laptop's OWN crew-host value (below) so it can
# reach the crew machine at all before any rendered file is reachable, and
# separately reading the standby host OUT of the rendered directory itself
# (there is no second laptop-side config key for it) so it can read the
# standby machine's own root the same way, before installing any block that
# names it.
#
#   bash <a copy of agent-platform on this laptop>/tools/install-crew-ssh-config.sh
#
# Idempotent + non-destructive:
#  - Writes the managed host blocks to ~/.ssh/convivy-mini-tmux.conf (overwritten each run).
#  - Ensures ~/.ssh/config has `Include convivy-mini-tmux.conf` at the very top, so these
#    win over any older hand-written `Host mini*` blocks (ssh uses the first value it sees
#    for each keyword). You don't have to delete old blocks — they're overridden — but the
#    script points them out so you can tidy up if you like.
#  - Backs up ~/.ssh/config before modifying it. Safe to re-run anytime.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB="$(cd "$HERE/../lib/agent-platform" 2>/dev/null && pwd || true)"
if [ -z "$LIB" ]; then
  echo "Cannot find lib/agent-platform next to this script at $HERE/.. — run this from a" >&2
  echo "full copy of agent-platform, not a standalone copy of tools/." >&2
  exit 1
fi

# THIS LAPTOP'S OWN COPY OF THE CREW HOST (lib/agent-platform/operator_config.py,
# the same "tailnet_host" key install.sh's render reads on the crew machine —
# CQ-603). Needed before anything else: reaching the crew machine to read its
# root (root_of, below) — the first step that makes the mounted volume and the
# files rendered onto it reachable at all — has no other source for where to
# connect. A missing or unreadable operator config is a hard stop: guessing a
# host here is exactly what this script exists not to do.
CONFIG_OUT="$(python3 - "$LIB" <<'PYEOF'
import sys
sys.path.insert(0, sys.argv[1])
import operator_config
try:
    crew_host = operator_config.get_optional("tailnet_host")
except operator_config.OperatorConfigError as e:
    print(f"ERROR: {e}")
    sys.exit(1)
print(crew_host or "")
PYEOF
)" || { echo "$CONFIG_OUT" >&2; exit 1; }
CREW_HOST="$CONFIG_OUT"
if [ -z "$CREW_HOST" ]; then
  echo "No crew host configured: this laptop's own operator config needs a" >&2
  echo "'tailnet_host' key (see config/operator.example.json) naming the crew" >&2
  echo "machine to connect to. Nothing written." >&2
  exit 1
fi

# Refuse to run on the crew machine itself — this script is laptop-only. Compared
# against CREW_HOST's own short label rather than a literal name, since no server
# name is hardcoded here (CQ-603).
CREW_HOST_SHORT="${CREW_HOST%%.*}"
if [ "$(hostname -s)" = "$CREW_HOST_SHORT" ]; then
  echo "Refusing to run: this is the crew machine ($(hostname -s)), not a laptop. Run it on your LAPTOP." >&2
  exit 1
fi

# root_of <host> prints the root that <host> reports, or prints nothing. The ssh
# target is the `mini` alias with HostName forced to <host> on the command line,
# which overrides the HostName in any config, so a laptop whose config still points
# at an old crew machine can read the new one before the config is rewritten. The
# User and IdentityFile come from the existing `mini` block (written by a previous
# run of this script, or set up by hand).
# The root is read from the machine's deploy copy of builder-root (company/
# decisions/0008), because its path does not depend on the root it prints. Every
# directory below is one that machine opens, so this laptop's own layout says nothing
# about it. The charset check rejects a character outside the charset on any line. It
# does not reject a value of more than one line. A two-line root passes it, and then
# the sed below fails on the newline after `> "$MANAGED"` has truncated the managed
# file, so the file is left empty and the script exits.
root_of() {
  local answer
  answer="$(ssh -o BatchMode=yes -o ConnectTimeout=10 -o HostName="$1" mini \
    '~/.local/deploy/agent-platform-shared/bin/builder-root --root' 2>/dev/null)" || answer=""
  case "$answer" in
    /*..*|*/../*) answer="" ;;
    /*) ;;
    *) answer="" ;;
  esac
  if [ -n "$answer" ] && printf '%s' "$answer" | grep -q '[^A-Za-z0-9._/-]'; then
    answer=""
  fi
  printf '%s' "$answer"
}

# The crew root is read before anything is written, so a crew machine that cannot
# answer leaves every file as it was rather than aliases into a guessed path.
MINI_ROOT="$(root_of "$CREW_HOST")"
if [ -z "$MINI_ROOT" ]; then
  echo "Cannot read the root on $CREW_HOST (ssh -o HostName=$CREW_HOST mini ~/.local/deploy/agent-platform-shared/bin/builder-root --root); nothing written." >&2
  exit 1
fi
echo "The root on $CREW_HOST: $MINI_ROOT"

# Where this laptop reads the co-styling and ssh/mosh render files install.sh
# renders on the crew machine (CQ-590, CQ-603): $HOME/.local/state/agent-platform/
# co-style there, outside any repository. The SMB share that exposes that
# account's home mounts it at /Volumes/<user> (the same mirror
# resolve-open-base.sh derives CONVIVY_OPEN_BASE from) — <user> is MINI_ROOT's
# own account segment, read above, so no second ssh round trip is needed to find
# it. A MINI_ROOT with fewer than two segments past the root (never true for an
# account-home path) leaves CO_STYLE_SRC empty, and every block below that reads
# from it skips, the same as a missing mount. CONVIVY_MINI_VOLUME_ROOT overrides
# the mount's own root, for a test that cannot mount a real share.
CO_STYLE_SRC=""
case "$MINI_ROOT" in
  /*/*/*)
    co_style_user="$(printf '%s\n' "$MINI_ROOT" | cut -d/ -f3)"
    CO_STYLE_SRC="${CONVIVY_MINI_VOLUME_ROOT:-/Volumes}/$co_style_user/.local/state/agent-platform/co-style"
    ;;
esac

# The standby machine's own hostname, read out of the rendered directory
# itself (ssh/standby-host, written by bin/render-ssh-config) rather than
# this laptop's own operator config, which carries only tailnet_host (above)
# — see the header comment. Absent, or the mount unreadable, means no
# standby session block needs a root read at all: bin/render-ssh-config only
# writes this file non-empty when at least one standby session block is
# rendered.
STANDBY_HOST=""
if [ -n "$CO_STYLE_SRC" ] && [ -s "$CO_STYLE_SRC/ssh/standby-host" ]; then
  STANDBY_HOST="$(head -n 1 "$CO_STYLE_SRC/ssh/standby-host")"
fi

# Refuse to run on the standby machine itself too, same shape as the crew-host
# refusal above and for the same reason: this script is laptop-only. Checked
# only once a standby host was actually rendered (above) — with none rendered
# there is no standby short label to compare against, and that shape (nothing
# rendered yet) is handled further down, same as a missing crew render.
if [ -n "$STANDBY_HOST" ]; then
  STANDBY_HOST_SHORT="${STANDBY_HOST%%.*}"
  if [ "$(hostname -s)" = "$STANDBY_HOST_SHORT" ]; then
    echo "Refusing to run: this is the standby machine ($(hostname -s)), not a laptop. Run it on your LAPTOP." >&2
    exit 1
  fi
fi

# The standby root is optional, and read only when a standby host was found
# above. Without it, the standby session block(s) are skipped — named on
# stderr — rather than installed against a guessed path; the rest of the
# install still proceeds, same shape as a crew machine with nothing rendered.
STANDBY_ROOT=""
if [ -n "$STANDBY_HOST" ]; then
  STANDBY_ROOT="$(root_of "$STANDBY_HOST")"
  if [ -z "$STANDBY_ROOT" ]; then
    echo "Skipping the standby session block(s): cannot read the root on $STANDBY_HOST." >&2
  else
    echo "The root on $STANDBY_HOST: $STANDBY_ROOT"
  fi
fi

SSH_DIR="$HOME/.ssh"
CONF="$SSH_DIR/config"
MANAGED="$SSH_DIR/convivy-mini-tmux.conf"
INCLUDE_LINE="Include convivy-mini-tmux.conf"

# Every project alias and mosh launcher runs `session-agent ensure-server` before
# `tmux attach || tmux new`. A `new` with no server running forks the server from
# the login's sshd session, which cannot read the login Keychain (session_lifecycle.py,
# the 08:42 incident). ensure-server starts the server through the GUI-session keeper
# job, or exits 1 and stops the chain. The plain `ssh mini` alias runs no command.
# All of this is baked into the rendered hosts.conf this script installs, not
# written here.

mkdir -p "$SSH_DIR"
chmod 700 "$SSH_DIR"

# The managed host blocks: rendered on the crew machine (bin/render-ssh-config,
# CQ-603) with every crew-host/standby-host/ssh-user value already resolved —
# only `@MINI_ROOT@`, this laptop's own live reading of the crew machine's root
# above, is substituted here. Written EMPTY (just the header) when the render
# itself had nothing to write (no crew host or ssh user configured on the crew
# machine) or the source file cannot be read at all (mount not present) — never
# guessed.
#
# The standby session blocks (RENDERED_STANDBY_SESSIONS) are appended only
# when STANDBY_ROOT was read above — `@STANDBY_ROOT@` left unsubstituted
# would splice a literal placeholder into a remote `tmux new -c` argument, so
# a failed read drops the whole chunk rather than install it broken. Appended
# AFTER the crew blocks, so a crew's own row (if it also runs on the crew
# machine) stays first on Deck's rail (deck_config.py keeps the first row per
# tmux session it finds).
RENDERED_HOSTS="$CO_STYLE_SRC/ssh/hosts.conf"
RENDERED_STANDBY_SESSIONS="$CO_STYLE_SRC/ssh/standby-sessions.conf"
{
  echo "# Managed by agent-platform tools/install-crew-ssh-config.sh — do not hand-edit; re-run to update."
  if [ -n "$CO_STYLE_SRC" ] && [ -s "$RENDERED_HOSTS" ]; then
    sed "s|@MINI_ROOT@|$MINI_ROOT|g" "$RENDERED_HOSTS"
  else
    echo "# (nothing rendered yet at $RENDERED_HOSTS — re-run install.sh on the crew"
    echo "# machine, with its own operator config carrying a crew host and ssh user)"
  fi
  if [ -n "$STANDBY_ROOT" ] && [ -s "$RENDERED_STANDBY_SESSIONS" ]; then
    sed "s|@STANDBY_ROOT@|$STANDBY_ROOT|g" "$RENDERED_STANDBY_SESSIONS"
  fi
} > "$MANAGED"
chmod 600 "$MANAGED"
echo "Wrote $MANAGED"

# Ensure ~/.ssh/config includes it at the top (so the managed blocks win).
touch "$CONF"
if grep -qxF "$INCLUDE_LINE" "$CONF"; then
  echo "'$INCLUDE_LINE' already present in $CONF — left as is."
else
  backup="$CONF.bak.$(date +%Y%m%d-%H%M%S)"
  cp "$CONF" "$backup"
  tmp="$(mktemp)"
  { printf '%s\n\n' "$INCLUDE_LINE"; cat "$CONF"; } > "$tmp"
  mv "$tmp" "$CONF"
  chmod 600 "$CONF"
  echo "Prepended '$INCLUDE_LINE' to $CONF  (backup: $backup)"
fi

# Flag any old hand-written mini blocks that the include now overrides.
if grep -nE '^[[:space:]]*Host[[:space:]].*\bmini' "$CONF" >/dev/null 2>&1; then
  echo
  echo "Note: $CONF still has hand-written 'Host mini*' block(s). The managed"
  echo "include is read first so it wins, but you can delete these for tidiness:"
  grep -nE '^[[:space:]]*Host[[:space:]].*\bmini' "$CONF" || true
fi

# --- iTerm2 per-crew co styling (ARCH-077, deploy fixed in ARCH-078) -----
# The co wrapper runs on the crew machine and emits an OSC-1337 SetProfile
# escape per crew; that escape travels up the ssh/tmux pipe to THIS laptop's
# iTerm2, which can only honor it if the matching DynamicProfile exists
# locally. iTerm runs on the laptop, not the crew machine — so the profiles
# deploy here, alongside the ssh aliases, not in install.sh. Real-file copies
# (NOT symlinks): iTerm2's DynamicProfiles loader silently skips symlinked
# .json. Source is CO_STYLE_SRC/iterm, install.sh's rendered output read over
# the mounted volume (CQ-590 — no committed iterm/co-*.json exists anymore).
# Gate ONLY on the source profiles existing — NOT on iTerm.app's location.
# iTerm reads ~/Library/Application Support/iTerm2/ regardless of where the app
# bundle lives (it isn't always /Applications/iTerm.app), and the old hard
# check skipped the whole deploy on machines that keep iTerm elsewhere.
ITERM_SRC="$CO_STYLE_SRC/iterm"
ITERM_DST="$HOME/Library/Application Support/iTerm2/DynamicProfiles"
have_iterm_src=""
if [ -n "$ITERM_SRC" ]; then
  for f in "$ITERM_SRC"/co-*.json; do [ -e "$f" ] && have_iterm_src=1 && break; done
fi
if [ -n "$have_iterm_src" ]; then
  mkdir -p "$ITERM_DST"
  # Atomic per-file install: copy to a temp, then `mv` (a single rename(2)) into
  # place. A plain `cp` truncates-then-writes, leaving a partial file for a
  # moment — and iTerm2 watches the DynamicProfiles dir and re-parses on every
  # change, so it would catch the half-written file and surface "invalid JSON"
  # on each install run. The rename is atomic, so iTerm only ever sees a complete
  # file. The temp lives in the PARENT iTerm2 dir (same volume → mv renames
  # rather than copies; not the watched subdir → iTerm never parses the temp,
  # which it would if the temp sat in DynamicProfiles, dotfile or not).
  iterm_dir="$(dirname "$ITERM_DST")"
  installed_names=""
  for src in "$ITERM_SRC"/co-*.json; do
    dst="$ITERM_DST/$(basename "$src")"
    tmp="$(mktemp "$iterm_dir/.co-install.XXXXXX")" || continue
    if cp "$src" "$tmp"; then
      mv -f "$tmp" "$dst"
      installed_names="$installed_names $(basename "$src" .json)"
    else
      rm -f "$tmp"
    fi
  done
  echo
  echo "Installed iTerm2 co styling profiles -> $ITERM_DST"
  echo "  ($installed_names)"
  echo "  If they don't appear in iTerm Settings > Profiles, fully quit + reopen"
  echo "  iTerm once so it loads them at launch (your mini tmux sessions survive)."
else
  echo
  echo "Skipping iTerm2 co styling profiles — no iterm/co-*.json found at"
  echo "$ITERM_SRC (re-run from the laptop with the mini volume mounted)."
fi

# --- Deck session->theme mapping -------------------------------------------
# Deck ships a palette of color-named themes, never a table of crew session
# names — a mapping keyed to real crew names would be unreadable and
# uneditable for anyone else running Deck. So that mapping is rendered from
# the registry on the crew machine (CQ-590), beside the iTerm profiles it
# mirrors, into CO_STYLE_SRC/deck/theme.json — read from here instead of a
# committed `deck/theme.json` with no committed crew name. See deck/README.md.
#
# MERGED PER KEY, NOT PER FILE — and the granularity is the whole point.
# Copying over this path would discard every theme picked in the app, since
# Deck's picker writes here. But skipping a file that merely EXISTS is just as
# wrong: Deck writes this file on any preference change, so one Cmd-minus
# creates it, after which the installer saw a file, left it alone, and the user
# never received the registry's mapping at all.
#
# So a key the user holds is theirs and is untouched; a key they lack is filled.
# The logic lives in lib/agent-platform/deck_config.py because merging JSON in
# shell is how you get a corrupted config, and because it is tested there.
#
# The rail's working directories are DERIVED from the aliases rendered on the
# crew machine (CQ-603) rather than restated here, so this repo holds one copy
# of that list rather than two. Note what that does NOT promise: a user who
# already has a `rail` key keeps it untouched, so a session added to the
# registry later will not appear on their rail. Their rail is curated and we
# cannot tell a removed row from an unseen one. To take the current list,
# delete the `rail` key and re-run.
DECK_SRC="$CO_STYLE_SRC/deck"
DECK_LIB="$LIB"
DECK_DST="$HOME/.config/convivy/deck/theme.json"
if [ -n "$DECK_SRC" ] && [ -f "$DECK_SRC/theme.json" ] && [ -n "$DECK_LIB" ]; then
  mkdir -p "$(dirname "$DECK_DST")"
  # Same atomic install as above: write a temp beside the target, then
  # rename(2) into place, so Deck never reads a half-written config.
  tmp="$(mktemp "$(dirname "$DECK_DST")/.deck-theme.XXXXXX")" || tmp=""
  # Capture what was filled so the install reports the change it made rather
  # than a blanket success. "nothing to fill" is a useful answer on a re-run.
  if [ -n "$tmp" ] && filled="$(DECK_LIB="$DECK_LIB" python3 - \
      "$DECK_SRC/theme.json" "$MANAGED" "$DECK_DST" "$tmp" <<'PYEOF'
import os, pathlib, sys
sys.path.insert(0, os.environ["DECK_LIB"])
import deck_config

theme_path, ssh_script, dst, tmp = (pathlib.Path(a) for a in sys.argv[1:5])
existing = dst.read_text() if dst.exists() else None
merged, filled = deck_config.merge_file(
    existing, theme_path.read_text(), ssh_script.read_text())
tmp.write_text(merged)
print(len(filled))
PYEOF
  )"; then
    mv -f "$tmp" "$DECK_DST"
    echo
    if [ "$filled" = "0" ]; then
      echo "Deck config already complete -> $DECK_DST (nothing to fill)"
    else
      echo "Merged Deck config -> $DECK_DST ($filled key(s) filled)"
      echo "  Your own settings were kept; only keys you did not have were added."
    fi
  else
    [ -n "$tmp" ] && rm -f "$tmp"
    echo
    echo "Could not merge Deck config into $DECK_DST" >&2
  fi
fi

# --- mosh launchers (roaming-resilient) + local color set (ARCH-242) --------
# The ssh aliases above FREEZE on roaming/hotel links: a NAT rebind wedges the
# long-lived TCP, keystrokes stop arriving (output still drains), and only a
# fresh connection recovers. mosh (UDP state-sync) resumes across rebinds and IP
# changes with no reconnect — the fix for that freeze.
#
# Colors over mosh: mosh runs its OWN terminal emulator and drops iTerm2's
# proprietary OSC-1337 SetProfile, so the per-crew color can't ride up the
# mosh stream the way it does over ssh (the aliases above). Fix: emit SetProfile
# LOCALLY in this laptop's iTerm BEFORE launching mosh — iTerm switches profile
# directly, then mosh redraws inside the already-colored window. The in-session
# `co` re-emit becomes a harmless no-op over mosh (color is already set).
#
# These are shell functions: type the session name and drop the `ssh`. The
# `ssh <crew>` aliases above remain as a plain-ssh fallback. Profile + session +
# dir mirror the ssh blocks exactly — both rendered from the same table
# (bin/render-ssh-config, CQ-603).
MOSH_FUNCS="$HOME/.convivy-mosh-sessions.zsh"
RENDERED_MOSH="$CO_STYLE_SRC/ssh/mosh-sessions.conf"
{
  echo "# Managed by agent-platform tools/install-crew-ssh-config.sh — do not hand-edit; re-run to update."
  echo "# mosh launchers for the registry's crew sessions: roaming-resilient (survives NAT"
  echo "# rebind / IP change) and color-correct (SetProfile emitted locally, pre-mosh)."
  echo "# Usage: type the session name — drop the \`ssh\`."
  echo
} > "$MOSH_FUNCS"
mosh_names=""
if [ -n "$CO_STYLE_SRC" ] && [ -s "$RENDERED_MOSH" ]; then
  while IFS='|' read -r m_alias m_profile m_session m_dir; do
    [ -z "$m_alias" ] && continue
    # Connect to the crew host's tailnet name, NOT a LAN name. mosh pins the server
    # address for the whole session and cannot follow a server IP change; a LAN name
    # resolves to the machine's 2-hour-lease DHCP address, so an overnight session dies
    # unrecoverably when that LAN IP rolls. The tailnet address does not churn with the
    # LAN, so the mosh session survives a closed-laptop overnight and resumes in the
    # morning. CREW_HOST is also in the rendered shared Host line, so ssh's bootstrap
    # gets the User and IdentityFile for this name.
    printf "%s() {\n  printf '\\\\033]1337;SetProfile=%s\\\\a'\n  mosh ${CREW_HOST} -- sh -c '~/.local/bin/session-agent ensure-server && (tmux attach -d -t %s 2>/dev/null || tmux new -s %s -c %s)'\n}\n\n" \
      "$m_alias" "$m_profile" "$m_session" "$m_session" "${m_dir//@MINI_ROOT@/$MINI_ROOT}" >> "$MOSH_FUNCS"
    mosh_names="$mosh_names $m_alias"
  done < "$RENDERED_MOSH"
fi
chmod 600 "$MOSH_FUNCS"
echo "Wrote $MOSH_FUNCS"

# Ensure ~/.zshrc sources it (idempotent — like the ssh Include above).
ZSHRC="$HOME/.zshrc"
SRC_LINE="[ -f ~/.convivy-mosh-sessions.zsh ] && source ~/.convivy-mosh-sessions.zsh"
touch "$ZSHRC"
if grep -qxF "$SRC_LINE" "$ZSHRC"; then
  echo "mosh source line already present in $ZSHRC — left as is."
else
  printf '\n# Convivy mosh session launchers (agent-platform install-crew-ssh-config.sh)\n%s\n' "$SRC_LINE" >> "$ZSHRC"
  echo "Appended mosh source line to $ZSHRC"
fi

echo
echo "Done."
if [ -n "$mosh_names" ]; then
  echo "  mosh (roaming-resilient, recommended): $mosh_names"
  echo "  (open a NEW terminal tab, or run: source ~/.convivy-mosh-sessions.zsh — then type the name, no \`ssh\`)"
  echo "  ssh fallback (plain, freezes on roaming): ssh <name>"
else
  echo "  No crew sessions were rendered yet (re-run install.sh on the crew machine first)."
fi
