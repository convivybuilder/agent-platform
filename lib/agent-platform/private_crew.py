"""Which crew directory (if any) a session's `cwd` belongs to, for anything that
writes or reads a private crew's data.

THE SINGLE SOURCE, deliberately. `session-event-log.py` (the hook writer) and
`session_state.py`/`tmux_transcript.py` (the readers) all have to agree on
which crews are private and where their home directories are, or a reader
searches a location the writer never wrote to and reports a healthy session as
`unknown` — exactly what happened to a private crew (agent-platform#421 /
issue_01M0GVMBXCX10F0R3ZAGHGEKFZ): the writer learned to route a private
crew's hook log to `<crew home>/hook-log/` and this module did not exist yet,
so the reader kept searching the shared root and found nothing. ADR-0043's
own black-box decision diagnoses the identical shape one directory over —
patching per call site had already left five — and argues the control belongs
at a single gate. This is that gate, for the "which directory is this" half.

THE FAIL-DIRECTION IS NOT DECIDED HERE. `private_home_for` returns a tri-state
answer — a path, `None`, or `UNKNOWN` — and leaves what to DO with `UNKNOWN` to
the caller, because the safe answer is opposite on each side. The writer
cannot tell whether a session is private must still write SOMETHING (ADR-0043:
a privacy control must not fail open), so it treats `UNKNOWN` as "assume
private" and strips content rather than dropping the event. A reader has no
such obligation: guessing the shared root when privacy can't be determined
risks confidently reporting `unknown` for a session that is actually running
in a private crew's own directory, so the safe answer there is to say `unknown`
honestly rather than search a location that might be wrong. A helper that
picked one policy would make the other side's failure mode unsafe.
"""

from __future__ import annotations

import json
import os
from typing import Any

import projects

# Read fresh on every call, never cached: a hook is a one-shot process with
# nothing to cache into, and a stale answer here would be a privacy decision
# made from an old registry. None (the default, every real launch) means the
# registry `projects.py` itself resolves: `PROJECTS_JSON` when a caller or
# test set it, else the Library's `GET /registry` (CQ·563/CQ·565 — this
# module used to resolve its own hardcoded `<root>/agent-platform/config/
# projects.json`, independent of `projects.PROJECTS_JSON`, because the
# registry used to live only in that file; it does not any more, so there is
# nothing left for a second, divergent default to answer for). A caller or
# test may still set `_REGISTRY` to a file explicitly, read directly rather
# than through `projects.py` — the seam `tests/test_run_register_api.py`,
# `tests/test_check_primary_checkouts.py`, `tests/test_check_serving_
# checkouts.py` and `tests/test_session_verbs.py` all use to pin this
# module's privacy read to a fixture independent of whatever `projects.py`'s
# OWN snapshot is doing in the same test.
_REGISTRY: str | None = None


def _load_registry() -> dict[str, Any]:
    if _REGISTRY is not None:
        with open(_REGISTRY, encoding="utf-8") as fh:
            return json.load(fh)
    return projects.raw_document()

# Returned when the registry (or the `cwd` handed in) cannot be read, which is
# NOT the same as "not private" — see the module docstring for why the two
# callers disagree about what to do with it.
UNKNOWN = object()


def _homes_for(field: str) -> list[str]:
    """Existing home directories of every crew the registry marks `<field>: true`.

    Raises on an unreadable or malformed registry rather than swallowing the
    error itself — the caller decides what "cannot tell" means for it, per the
    module docstring, so the primitive reports the fact rather than a policy.

    A per-entry failure is not a file-level one and does not raise. A unit whose
    `home_dir` is missing, empty or not a string is skipped, as is one whose home is not a directory
    on disk. If that leaves no homes, `private_home_for`/`walled_off_home_for` return None, not
    UNKNOWN. The registry's private crews, read live from `projects.private_slugs()`, all carry a
    `home_dir`, so no entry takes this path today.
    """
    reg = _load_registry()
    homes = []
    for slug, unit in reg.items():
        if slug.startswith("_") or not isinstance(unit, dict):
            continue
        if unit.get(field) is not True:
            continue
        raw = unit.get("home_dir")
        if not isinstance(raw, str) or not raw:
            continue
        # projects.registry_home, never expanduser: a home resolves against the
        # machine's root, and a value it cannot place raises, which
        # private_home_for turns into UNKNOWN (fail closed).
        path = str(projects.registry_home(raw))
        if os.path.isdir(path):
            homes.append(path)
    return homes


def private_homes() -> list[str]:
    """Existing home directories of every crew the registry marks `private: true`.
    See `_homes_for`'s own docstring for the shared contract."""
    return _homes_for("private")


def walled_off_homes() -> list[str]:
    """Existing home directories of every crew the registry marks `walled_off: true`
    (platform/decisions/0100 — the operator's own private crew, registered so the
    platform knows it exists but never centrally indexed). See `_homes_for`'s own
    docstring for the shared contract."""
    return _homes_for("walled_off")


def is_private_slug(slug):
    """Whether the registry marks this project slug `private: true`.

    `True`, `False`, or `UNKNOWN` when the registry cannot be read — the same
    tri-state `private_home_for` returns, and for the same reason: the safe
    answer differs by caller, so this reports the fact and leaves the policy.

    **A SLUG LOOKUP, NOT A PATH RECONSTRUCTION.** A caller holding a project
    name and asking "is this crew private" was previously obliged to compose
    `<root>/<slug>` and hand it to `private_home_for`, which assumes every
    crew's home is a direct, identically-named child of the machine's root. Three
    entries in this registry already break that shape by nesting one level
    deeper, and a private crew's slug can be lowercase while its `home_dir`
    keeps a different case, so the reconstruction is a guess that happens to
    work for a private crew only by chance. This asks the registry the
    question directly.

    **MATCHED CASE-INSENSITIVELY.** The registry's slugs are lowercase and a
    caller's value comes from wherever it came from; for a privacy question,
    matching more broadly withholds more, which is the safe direction.
    """
    if not isinstance(slug, str) or not slug:
        return UNKNOWN
    try:
        reg = _load_registry()
    except Exception:
        return UNKNOWN
    if not isinstance(reg, dict):
        return UNKNOWN
    want = slug.strip().lower()
    for key, unit in reg.items():
        if key.startswith("_") or not isinstance(unit, dict):
            continue
        if key.strip().lower() == want:
            return unit.get("private") is True
    return False


def _home_for(cwd, homes):
    """The entry of `homes` containing `cwd`, `None` if none, `UNKNOWN` on error —
    the inode-comparison, upward-walking core `private_home_for` and
    `walled_off_home_for` both share, so a change to the walk cannot drift
    between the two.

    COMPARISON IS BY INODE, not by string, and that is the whole correctness of
    this function. A string comparison misses a session that reached the
    directory by another spelling: this machine's boot volume is
    case-insensitive by default, so `cd ~/builder/Acme` lands in the real
    directory under a different name, and a symlink lands there under a
    different path entirely. `bin/co`'s transport wall had exactly that bug
    (security-reviewer, PR #409) and `os.path.samefile` is the form that cannot
    have it, since it asks the filesystem rather than the string.

    Walks up from `cwd`, so a subdirectory of one of `homes` matches too.

    A missing, `None`, non-string, or empty `cwd` is `UNKNOWN`, not `None`: it
    is exactly as unable to tell "is this inside one of `homes`" as an
    unreadable registry, so it must take the same fail-closed path rather than
    being read as "not inside one" (reviewer, PR #413, run-bb8a514a).
    """
    if not isinstance(cwd, str) or not cwd:
        return UNKNOWN
    if not homes:
        return None
    try:
        here = os.path.realpath(cwd)
        if not os.path.isdir(here):
            return UNKNOWN
        seen = 0
        while seen < 64:
            for home in homes:
                try:
                    if os.path.samefile(here, home):
                        return home
                except OSError:
                    continue
            parent = os.path.dirname(here)
            if parent == here:
                return None
            here = parent
            seen += 1
        return None
    except Exception:
        return UNKNOWN


def private_home_for(cwd):
    """The private crew home containing `cwd`, `None` if none, `UNKNOWN` on error.
    See `_home_for`'s own docstring for the comparison and fail-closed rules."""
    try:
        homes = private_homes()
    except Exception:
        return UNKNOWN
    return _home_for(cwd, homes)


def walled_off_home_for(cwd):
    """The walled-off crew home (platform/decisions/0100) containing `cwd`, `None`
    if none, `UNKNOWN` on error. Mirrors `private_home_for`'s contract exactly —
    see `_home_for`'s own docstring for the comparison and fail-closed rules."""
    try:
        homes = walled_off_homes()
    except Exception:
        return UNKNOWN
    return _home_for(cwd, homes)
