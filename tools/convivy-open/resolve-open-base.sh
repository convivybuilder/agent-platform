#!/usr/bin/env bash
# resolve-open-base.sh — this laptop's CONVIVY_OPEN_BASE, the SMB-mounted
# mirror of the mini's root (company/decisions/0008), without assuming a
# fixed path: the mini's root can be configured to anything.
#
# Usage: resolve-open-base.sh <config-file>
# Prints CONVIVY_OPEN_BASE to stdout. NEVER OVERWRITES an existing config: a
# config already holding a non-empty CONVIVY_OPEN_BASE= line is read as-is and
# nothing is written, so an operator's manual edit (or a prior run) sticks.
# Only when the config has no usable value does this read the mini's root
# over ssh (refusing and leaving the file untouched if that fails) and derive
# this laptop's mirror path from it, writing both to the config:
#   CONVIVY_OPEN_BASE=<the laptop's SMB-mounted mirror>   (read by this tool)
#   MINI_OPEN_BASE=<the mini's own root, verbatim>         (for recognizing a
#                                                            pasted mini path)
#
# CONVIVY_OPEN_BASE the ENVIRONMENT VARIABLE still overrides both of these at
# runtime (convivy-open / convivy-open-server check it first) — this script
# only decides what the config file, and so the generated plist, defaults to.
set -euo pipefail

CONFIG="${1:?usage: resolve-open-base.sh <config-file>}"

_read_key() { # <file> <key> -> value or empty
  [ -f "$1" ] || return 0
  sed -n "s/^$2=//p" "$1" | tail -1
}

existing_base="$(_read_key "$CONFIG" CONVIVY_OPEN_BASE)"
if [ -n "$existing_base" ]; then
  printf '%s\n' "$existing_base"
  exit 0
fi

MINI_ROOT="$(ssh -o BatchMode=yes -o ConnectTimeout=10 mini \
  '~/.local/deploy/agent-platform-shared/bin/builder-root --root' 2>/dev/null)" || MINI_ROOT=""

# Reject more than one line BEFORE any other check. The path-shape case below
# and the character-class grep after it both test the value one line at a
# time (a `case` glob's `*` matches across embedded newlines, and grep splits
# on them), so a two-line answer where each line individually looks like a
# safe absolute path passes both, with its second line then landing,
# unvalidated, in the config file, the plist and the server's environment.
case "$MINI_ROOT" in
  *$'\n'*|*$'\r'*)
    echo "resolve-open-base.sh: the mini's answer spans more than one line; refusing rather than guessing. CONVIVY_OPEN_BASE not written." >&2
    exit 1
    ;;
esac

case "$MINI_ROOT" in
  /*..*|*/../*) MINI_ROOT="" ;;
  /*) ;;
  *) MINI_ROOT="" ;;
esac
if [ -z "$MINI_ROOT" ] || printf '%s' "$MINI_ROOT" | grep -q '[^A-Za-z0-9._/-]'; then
  echo "resolve-open-base.sh: cannot read the mini's root (ssh mini ~/.local/deploy/agent-platform-shared/bin/builder-root --root); CONVIVY_OPEN_BASE not written." >&2
  exit 1
fi

# The laptop's mirror: the SMB share exposes the mini's account home as
# /Volumes/<user>, so <home-root>/<user>/<rest> on the mini reads as
# /Volumes/<user>/<rest> here. <home-root> is this account's home directory's
# parent (/Users on macOS) as the directory service reports it — not a
# hardcoded literal, and not $HOME, which a caller can set to anything and
# which the real account's home-root convention does not depend on. Refuse
# rather than guess if the mini's root does not live under its account home
# (the SMB share cannot reach it).
_this_home="$(dscacheutil -q user -a name "$(whoami)" 2>/dev/null | awk '/^dir: / {print $2}')"
_home_root="$(dirname "${_this_home:-$HOME}")"
case "$MINI_ROOT" in
  "$_home_root"/*/*)
    _user="$(printf '%s\n' "$MINI_ROOT" | cut -d/ -f3)"
    _rest="$(printf '%s\n' "$MINI_ROOT" | cut -d/ -f4-)"
    LAPTOP_BASE="/Volumes/$_user/$_rest"
    ;;
  *)
    echo "resolve-open-base.sh: the mini's root ($MINI_ROOT) is not under its account home — no SMB-mounted mirror path exists; CONVIVY_OPEN_BASE not written." >&2
    exit 1
    ;;
esac

mkdir -p "$(dirname "$CONFIG")"
{
  printf 'CONVIVY_OPEN_BASE=%s\n' "$LAPTOP_BASE"
  printf 'MINI_OPEN_BASE=%s\n' "$MINI_ROOT"
} > "$CONFIG"
chmod 600 "$CONFIG"
printf '%s\n' "$LAPTOP_BASE"
