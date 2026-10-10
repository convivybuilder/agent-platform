#!/usr/bin/env bash
# Run this ON YOUR LAPTOP (not the mini), e.g.:
#   bash <the mini's root, mounted>/agent-platform/tools/convivy-open/install-laptop.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
BIN="$HOME/.local/bin"
PLIST="$HOME/Library/LaunchAgents/com.convivy.openserver.plist"

if [ "$(hostname -s)" = "mini" ]; then
  echo "Refusing to run: this looks like the mini, not the laptop." >&2
  exit 1
fi

mkdir -p "$BIN"
cp "$HERE/convivy-open-server"   "$BIN/convivy-open-server"
cp "$HERE/convivy-open"          "$BIN/convivy-open"
cp "$HERE/convivy-open.zsh"      "$BIN/convivy-open.zsh"
cp "$HERE/typora-goto"           "$BIN/typora-goto"
cp "$HERE/resolve-open-base.sh"  "$BIN/resolve-open-base.sh"
chmod +x "$BIN/convivy-open-server" "$BIN/convivy-open" "$BIN/typora-goto" "$BIN/resolve-open-base.sh"

# This laptop's CONVIVY_OPEN_BASE — the SMB-mounted mirror of the mini's root
# (company/decisions/0008), not a fixed path assumed in advance. Cached in a
# config file so a later run (with the mini unreachable) reuses it rather
# than re-asking; delete the file to force a fresh resolution. See
# resolve-open-base.sh.
CONVIVY_OPEN_CONFIG="$HOME/.config/convivy/convivy-open.env"
CONVIVY_OPEN_BASE="$("$BIN/resolve-open-base.sh" "$CONVIVY_OPEN_CONFIG")" || {
  echo "Cannot resolve this laptop's CONVIVY_OPEN_BASE (see the error above) — install refused." >&2
  exit 1
}
echo "CONVIVY_OPEN_BASE: $CONVIVY_OPEN_BASE"

# XML-escape before this reaches the plist's <string> text node below.
# resolve-open-base.sh validates a value it read fresh over ssh, but returns an
# existing CONVIVY_OPEN_BASE= line from the config file completely unvalidated
# (an operator's own edit is trusted as-is) — so this is the one point that
# must not assume the value is already safe.
xml_escape() {
  printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'
}
CONVIVY_OPEN_BASE_XML="$(xml_escape "$CONVIVY_OPEN_BASE")"

# The SMB share's user segment — CONVIVY_OPEN_BASE is "/Volumes/<user>/<rest>"
# (resolve-open-base.sh), so the same <user> names the share to auto-mount.
# XML-escaped for the same reason CONVIVY_OPEN_BASE_XML is above: an existing
# config value is untrusted input reaching a plist <string> text node.
CONVIVY_OPEN_MOUNT_USER="$(xml_escape "$(printf '%s\n' "$CONVIVY_OPEN_BASE" | cut -d/ -f3)")"

# open-md-link — the iTerm cmd-click handler (ARCH-154/155). It lives at ~/bin (the
# path the co-*.json Semantic-History command references), NOT ~/.local/bin — deploy
# it there unchanged so the click wiring keeps resolving.
mkdir -p "$HOME/bin"
cp "$HERE/open-md-link" "$HOME/bin/open-md-link" && chmod +x "$HOME/bin/open-md-link"

# The cve/cvd/cvl shell helpers and a bare `convivy-open` test call it by name,
# so ~/.local/bin must be on PATH. (iTerm cmd-click uses the absolute path, so
# it's unaffected — but a bare-name miss here is the usual "command not found"
# when you test the CLI by hand.)
case ":$PATH:" in
  *":$BIN:"*) : ;;
  *)
    echo "WARNING: $BIN is not on your PATH — a bare 'convivy-open' won't resolve."
    echo "  Fix:  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc && source ~/.zshrc"
    ;;
esac

# Build the line-jump helper app. It carries its own TCC identity so macOS will
# prompt to allow it (Accessibility) on first use — unlike the launchd daemon,
# which can't be granted Accessibility reliably. Drives Typora's Find for &line=.
APP="$HOME/Applications/ConvivyTyporaGoto.app"
mkdir -p "$HOME/Applications"
if [ -d "$APP" ]; then
  # Rebuilding changes the app's code signature and resets its Accessibility
  # grant — so keep an existing app. To pick up a new typora-goto.applescript,
  # delete the app and re-run (then re-grant Accessibility once).
  echo "App already present: $APP (kept, to preserve its Accessibility grant)"
else
  /usr/bin/osacompile -o "$APP" "$HERE/typora-goto.applescript" \
    && echo "Built $APP — now grant it Accessibility (Privacy & Security → Accessibility)" \
    || echo "WARNING: osacompile failed; line-jump will fall back to plain open"
fi

# Generate the LaunchAgent with THIS machine's resolved server path. launchd does
# NOT expand $HOME or ~ in ProgramArguments, so the path must be written out
# absolute. We generate it here rather than copy a static plist — the static one
# had the mini's home directory (.local/bin/...) baked in, which silently
# fails to launch on the laptop when the username differs, leaving port 8765 to
# whatever else grabs it (and a misleading stock 404 for /open links).
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.convivy.openserver</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>$BIN/convivy-open-server</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>CONVIVY_OPEN_BASE</key><string>$CONVIVY_OPEN_BASE_XML</string>
        <key>CONVIVY_OPEN_MOUNT</key><string>smb://mini.local/$CONVIVY_OPEN_MOUNT_USER</string>
        <key>CONVIVY_OPEN_PORT</key><string>8765</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>/tmp/convivy-open-server.log</string>
    <key>StandardErrorPath</key><string>/tmp/convivy-open-server.log</string>
</dict>
</plist>
PLIST

launchctl unload "$PLIST" 2>/dev/null || true

# Free port 8765 if a stray server (e.g. a plain `python -m http.server`) holds
# it — that's what produces the misleading "File not found / Nothing matches the
# given URI" stock 404 for /open links.
if lsof -nP -iTCP:8765 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port 8765 already in use — freeing it:"
  lsof -nP -iTCP:8765 -sTCP:LISTEN
  lsof -ti tcp:8765 | xargs kill 2>/dev/null || true
  sleep 1
fi

launchctl load "$PLIST"
sleep 1

echo "Installed → $BIN/convivy-open-server (plist: $PLIST)"
echo "Health check:"
health="$(curl -s http://localhost:8765/health || true)"
if [ "$health" = "ok" ]; then
  echo "  ok — convivy-open-server is live"
else
  echo "  NOT the convivy-open-server (got: '${health:-no response}'). Check /tmp/convivy-open-server.log"
fi
echo "Base path: $CONVIVY_OPEN_BASE  (edit env vars in $PLIST to change)"
echo
echo "Cmd-click (open a mini-side file path locally by clicking it in iTerm):"
echo "  handled by iTerm Semantic History calling ~/bin/open-md-link (the Typora"
echo "  line-jump handler — it does the mini-home->/Volumes translation itself). That"
echo "  command ships in the co-*.json DynamicProfiles ('Always run command' ->"
echo "  $HOME/bin/open-md-link \1 \2), deployed laptop-side by"
echo "  the operator's own ssh-alias installer — run that too, reopen iTerm."
echo "  (convivy-open, installed above, serves the :8765 Slack-link path + CLI, NOT"
echo "  cmd-click.)"
