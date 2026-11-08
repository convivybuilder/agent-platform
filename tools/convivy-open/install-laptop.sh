#!/usr/bin/env bash
# Run this ON YOUR LAPTOP (not the mini), e.g.:
#   bash <the mini's root, mounted>/agent-platform/tools/convivy-open/install-laptop.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
BIN="$HOME/.local/bin"

if [ "$(hostname -s)" = "mini" ]; then
  echo "Refusing to run: this looks like the mini, not the laptop." >&2
  exit 1
fi

mkdir -p "$BIN"
cp "$HERE/convivy-open"          "$BIN/convivy-open"
cp "$HERE/convivy-open.zsh"      "$BIN/convivy-open.zsh"
cp "$HERE/resolve-open-base.sh"  "$BIN/resolve-open-base.sh"
chmod +x "$BIN/convivy-open" "$BIN/resolve-open-base.sh"

# This laptop's CONVIVY_OPEN_BASE — the SMB-mounted mirror of the mini's root
# (company/decisions/0008), not a fixed path assumed in advance. Cached in a
# config file so a later run (with the mini unreachable) reuses it rather
# than re-asking; delete the file to force a fresh resolution. See
# resolve-open-base.sh. Read by the convivy-open CLI, not by anything this
# script starts.
CONVIVY_OPEN_CONFIG="$HOME/.config/convivy/convivy-open.env"
CONVIVY_OPEN_BASE="$("$BIN/resolve-open-base.sh" "$CONVIVY_OPEN_CONFIG")" || {
  echo "Cannot resolve this laptop's CONVIVY_OPEN_BASE (see the error above) — install refused." >&2
  exit 1
}
echo "CONVIVY_OPEN_BASE: $CONVIVY_OPEN_BASE"

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

echo "Installed → $BIN/convivy-open (CLI), base path: $CONVIVY_OPEN_BASE"
echo
echo "Cmd-click (open a mini-side file path locally by clicking it in iTerm):"
echo "  handled by iTerm Semantic History calling ~/bin/open-md-link (the Typora"
echo "  line-jump handler — it does the mini-home->/Volumes translation itself). That"
echo "  command ships in the co-*.json DynamicProfiles ('Always run command' ->"
echo "  $HOME/bin/open-md-link \1 \2), deployed laptop-side by"
echo "  the operator's own ssh-alias installer — run that too, reopen iTerm."
echo "  (convivy-open, installed above, is the terminal CLI, NOT cmd-click.)"
