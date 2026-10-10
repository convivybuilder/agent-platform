"""Resolve a git ref by its exact name, with none of git's search rules.

`rev-parse`, `merge`, `checkout`, `show` and `log` on even a fully qualified name
such as `refs/remotes/origin/main` fall through to `refs/refs/remotes/origin/main`,
`refs/tags/refs/remotes/origin/main` and `refs/heads/refs/remotes/origin/main` once
the real ref is gone (a remote that renamed its default branch, then `fetch
--prune`). A tag pushed to the remote with that literal name is then read as main.
`show-ref --verify` matches the one name, so callers resolve the sha here, act on
it, and refuse when this returns None.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ORIGIN_MAIN_REF = "refs/remotes/origin/main"


def exact_ref_sha(root: Path | str, ref: str) -> str | None:
    """The sha `ref` holds in the repository at `root`, or None when that exact
    ref is absent or git could not be run."""
    try:
        r = subprocess.run(["git", "-C", str(root), "show-ref", "--verify", "--hash", ref],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    sha = r.stdout.strip()
    if r.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
        return None
    return sha


def origin_main_sha(root: Path | str) -> str | None:
    return exact_ref_sha(root, ORIGIN_MAIN_REF)
