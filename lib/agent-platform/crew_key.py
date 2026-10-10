"""crew_key — the one sanitiser and tmux-name lookup every per-crew file keys by.

A crew's per-session files — `co-working-on`'s "working on" line, the Slate
(`platform/decisions/0091`) — are all keyed by the crew's tmux session name,
reduced to a safe filename charset. That reduction and the tmux lookup behind
it were written twice already (`bin/co-working-on`'s `_KEY_RE`/`_session_key`/
`_tmux_session_name`, and `session_state.py`'s equivalent `_SESSION_ID_RE`-based
form for `working_on()`). A third write, for the Slate, is what this module
exists to refuse: `bin/co-working-on`, `session_state.working_on`, `slate.py`
and `bin/co-ask` all import it rather than each keeping its own copy.
"""
from __future__ import annotations

import os
import re
import subprocess

_KEY_RE = re.compile(r"[^A-Za-z0-9_-]")

# Matches `bin/co-working-on`'s own bound: a key is a filename component, not
# free text, and 64 characters is long past any real tmux session name.
_MAX_LEN = 64


def session_key(name: str) -> str:
    """The crew-key charset applied to *name*, capped at `_MAX_LEN`.

    Every character outside `[A-Za-z0-9_-]` is dropped rather than replaced, so
    two names that differ only in punctuation can collide — the same tradeoff
    `bin/co-working-on` already made, kept rather than tightened here so every
    importer keeps agreeing on one key for one tmux session name.
    """
    return _KEY_RE.sub("", str(name or ""))[:_MAX_LEN]


def tmux_session_name() -> str | None:
    """The tmux session this process is running inside, or None.

    `$TMUX` is checked first so a plain (non-tmux) process fails fast and
    quiet, without shelling out to a `tmux` that would just say "no server
    running".
    """
    if not os.environ.get("TMUX"):
        return None
    try:
        done = subprocess.run(["tmux", "display-message", "-p", "#S"],
                               capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    name = done.stdout.strip()
    return name if done.returncode == 0 and name else None
