"""The normalize-and-truncate transform `agentctl` uses to turn free text
into a branch-safe slug (CQ·354).

Factored out of `bin/agentctl`'s `_slug`, byte-for-byte, so that anything
needing to predict or recognize a fragment of a branch name a dispatched
worker will get — `issue_gate.py`'s unlinked-branch scan, today — calls the
SAME transform `agentctl` itself applies when it builds `derive_purpose`'s
`agent/<purpose>` branch names, rather than a second implementation that can
silently drift from it. `bin/agentctl` imports `slugify` back as its own
`_slug` rather than keeping a local copy.
"""
from __future__ import annotations

import re

_DEFAULT_MAXLEN = 32


def slugify(text: str, *, prefix: str = "", maxlen: int = _DEFAULT_MAXLEN) -> str:
    """Lowercase `text`, collapse every run of characters outside
    `[a-z0-9-]` to a single `-`, strip leading/trailing `-`, then truncate
    to `maxlen` characters (stripping any `-` the truncation leaves
    trailing). `prefix`, when given, is prepended raw and does NOT count
    against `maxlen` — the caller's responsibility to keep it ref/path-safe
    on its own, same as before this moved."""
    s = re.sub(r"[^a-z0-9-]+", "-", text.lower()).strip("-")
    s = re.sub(r"-+", "-", s)[:maxlen].strip("-")
    return f"{prefix}-{s}" if prefix and s else (s or prefix or "task")
