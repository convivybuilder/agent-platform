#!/usr/bin/env bash
# lib/safe-path.sh — resolve a credentialed or guard-deciding tool (gh, jq)
# from a fixed, ordered list of system directories, and from nowhere else.
#
# Round 2 and round 3 built `safe_which` as a PATH denylist: drop any PATH
# entry under the running account's home or under a worktree. Every round
# found another writable place the denylist missed — a relative entry, a
# differently-cased one, then /tmp and /var/tmp (world-writable, sticky, and
# not under any account's home), then a bash/Python parity gap on a bare
# `worktrees` directory. A denylist loses this race by construction: there is
# always one more writable directory nobody enumerated yet.
#
# This is an allowlist instead, and it never reads $PATH at all: it checks
# the fixed directories in _SAFE_PATH_DIRS below and returns the first
# <dir>/<name> that is an executable regular file. A dispatched agent's PATH
# has nothing to shadow, because nothing it can set is ever consulted.
#
# See lib/agent-platform/safe_path.py for the Python sibling — one shared
# design per language, so each call site does not independently get (and
# later drift from) the directory list. tests/test_shared_safe_path_dirs.py
# asserts the two lists are equal.
#
# Remaining gap: on this machine today, /opt/homebrew/bin is owned by
# the operator's own account, group admin, mode drwxrwxr-x — writable by anyone in the admin
# group. While dispatched agents still run as the operator (before the
# two-account install, platform/decisions/0090), an agent can still plant a
# binary there. The `agents` account that install creates is not a member of
# admin, so once it lands, this directory is no longer writable by the
# account this code runs as. Until then, this is a known, open gap — not
# claimed closed.
#
# USAGE
#   safe_which <name> [<colon-separated dirs>]
#     prints an absolute path on stdout, exit 0; prints nothing and exits 1
#     if not found. The second argument exists for tests only — no
#     production caller passes it; omitted, it defaults to the fixed system
#     list below.
#
# Bash 3.2 safe (ARCH-082): no associative arrays, no ${var,,}. /usr/bin/tr
# and /usr/bin/sort by absolute path, not bare `tr`/`sort` left to PATH: this
# function runs precisely while PATH may be thin or hostile, the same reason
# every lookup it guards goes through it in the first place.

# Sorts a colon-separated list ordinally (byte order), matching Python's
# `sorted()` on the same strings regardless of the caller's locale. A bare
# `/usr/bin/sort` follows LC_ALL/LC_COLLATE, so under a locale such as
# en_US.UTF-8 it orders case- and punctuation-insensitively and disagrees
# with Python's always-ordinal `sorted()` on names like "Zebra"/"_under"/
# "apple" — pinning LC_ALL=C makes bash's sort ordinal too. Exists as its own
# function so tests/bash/test_safe_path.sh can exercise it directly with
# crafted names, without needing write access to /opt (root-owned) to
# produce a multi-entry case.
_safe_path_sort_colon_list() { # <colon-separated list> -> sorted colon-separated list on stdout
  printf '%s' "$1" | /usr/bin/tr ':' '\n' | LC_ALL=C /usr/bin/sort | /usr/bin/tr '\n' ':'
}

# Standard system directories, checked in this order. The Homebrew-equivalent
# entry is a sorted glob over /opt, not a literal /opt/homebrew/bin —
# matching session_view.py's _node_candidates elsewhere: a literal Homebrew
# path is a machine-specific path shipwright/tools/builder-paths refuses in
# code that runs, and the glob finds whatever this machine — or none — puts
# under /opt, without naming a brand, resolving to the identical
# /opt/homebrew/bin on this machine today.
_safe_path_system_dirs() {
  local opt_dir opt_dirs=""
  for opt_dir in /opt/*/bin; do
    [ -d "$opt_dir" ] || continue
    opt_dirs="$opt_dirs:$opt_dir"
  done
  # Sorted, so the order this contributes is deterministic rather than
  # whatever order the filesystem glob happens to return.
  if [ -n "$opt_dirs" ]; then
    opt_dirs=$(_safe_path_sort_colon_list "${opt_dirs#:}")
    opt_dirs="${opt_dirs%:}"
  fi
  printf '%s\n' "/usr/bin:/bin:/usr/sbin:/sbin${opt_dirs:+:$opt_dirs}:/usr/local/bin"
}

safe_which() {
  local name="$1" dirs="${2:-}" dir saved_ifs="$IFS"
  [ -n "$dirs" ] || dirs="$(_safe_path_system_dirs)"
  IFS=:
  for dir in $dirs; do
    IFS="$saved_ifs"
    [ -n "$dir" ] || continue
    if [ -f "$dir/$name" ] && [ -x "$dir/$name" ]; then
      printf '%s\n' "$dir/$name"
      return 0
    fi
    IFS=:
  done
  IFS="$saved_ifs"
  return 1
}
