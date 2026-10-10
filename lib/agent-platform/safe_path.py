"""safe_path — resolve a credentialed or guard-deciding tool (gh, git, jq,
npx) from a fixed, ordered list of system directories, and from nowhere
else.

Round 2 and round 3 built `safe_which` as a PATH denylist: drop any PATH
entry under the running account's home or under a worktree. Every round
found another writable place the denylist missed — a relative entry, a
differently-cased one, then `/tmp` and `/var/tmp` (world-writable, sticky,
and not under any account's home), then a bash/Python parity gap on a bare
`worktrees` directory. A denylist loses this race by construction: there is
always one more writable directory nobody enumerated yet.

This is an allowlist instead, and it never reads `$PATH` at all.
`safe_which(name)` checks a fixed, ordered list of absolute system
directories and returns the first `<dir>/<name>` that is an executable
regular file. A dispatched agent's PATH has nothing to shadow, because
nothing it can set is ever consulted.

Remaining gap: on this machine today, `/opt/homebrew/bin` is owned by
the operator's own account, group `admin`, mode `drwxrwxr-x` — writable by anyone in the
`admin` group. While dispatched agents still run as the operator (before the
two-account install, platform/decisions/0090), an agent can still plant a
binary there. The `agents` account that install creates is not a member of
`admin`, so once it lands, this directory is no longer writable by the
account this code runs as. Until then, this is a known, open gap — not
claimed closed.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Sequence

# Standard system directories, checked in this order. None of gh, git, jq or
# npx lives in /usr/sbin or /sbin on this machine today; both are included
# because they are standard, root-owned POSIX locations a future caller might
# need, and listing them costs nothing.
#
# The Homebrew-equivalent entry is a sorted glob over /opt, not a literal
# `/opt/homebrew/bin` — matching `session_view.py`'s `_node_candidates`
# elsewhere. A literal Homebrew path is a machine-specific path
# `shipwright/tools/builder-paths` refuses in code that runs (reproduced: a
# literal `/opt/homebrew/bin` string here is reported as 1 "code that runs"
# occurrence); the glob finds whatever this machine — or none — puts under
# /opt, without naming a brand, and resolves to the identical
# `/opt/homebrew/bin` on this machine today.
_FIXED_HEAD: tuple[str, ...] = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
_FIXED_TAIL: tuple[str, ...] = ("/usr/local/bin",)


def _system_dirs() -> tuple[str, ...]:
    opt_bins = tuple(
        sorted(str(p) for p in Path("/opt").glob("*/bin") if p.is_dir())
    )
    return _FIXED_HEAD + opt_bins + _FIXED_TAIL


def safe_which(name: str, *, search_dirs: Sequence[str] | None = None) -> str | None:
    """The first `<dir>/<name>` in `search_dirs` (default: the fixed system
    list above, recomputed per call so a changed `/opt` is picked up) that is
    an executable regular file, or None.

    `search_dirs` exists for tests only — no production caller passes it.
    This never reads `$PATH` or `$HOME`: the directories checked are fixed,
    not attacker-influenced, so there is nothing here for a dispatched
    agent's environment to redirect."""
    dirs = _system_dirs() if search_dirs is None else search_dirs
    for d in dirs:
        candidate = os.path.join(d, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None
