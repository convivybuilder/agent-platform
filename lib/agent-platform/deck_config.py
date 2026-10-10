"""deck_config — seed Convivy's Deck config without discarding the user's.

THE BUG THIS FIXES. The installer seeded `~/.config/convivy/deck/theme.json`
only when the file was absent, on the reasoning that a present file is user
state and copying over it would discard every theme picked in the app. The
reasoning is right; the granularity was wrong. Deck writes that same file on any
preference change, so a single Cmd-minus creates it — after which the installer
sees a file, leaves it alone, and the user never receives Convivy's session
mapping at all. Observed: the operator's Hollis rendered blue in Deck where iTerm renders
pink, because his file existed and carried no `sessions` block.

So the unit of "already present" is a KEY, not a FILE. A key the user has is
theirs and is never touched. A key they lack is filled from the seed.

THE RAIL IS DERIVED, NOT DUPLICATED. Session working directories are already
declared once, in the ssh aliases the operator's own ssh-alias installer writes
(`tmux new -s <name> -c <workdir>`). Parsing them there means this repo holds one
copy of that list rather than two, so there is no second list to forget to
update. Display names are data rather than derivation, because no transformation
turns `svc` into `Convivy Kit`.

**That guarantee is about the repo, not about an installed config.** Once a user
has a `rail` key it is theirs and is never touched, so a session added to the
aliases later does NOT appear on their rail on the next install. This is
deliberate: a rail is ordered and curated, and once the key exists nothing here
can tell "never customised" from "deliberately removed rows I do not want."
Re-adding a row someone deleted would be the worse error, and it would
contradict the rule that `"rail": []` is a choice. A user who wants the current
list deletes the `rail` key and re-runs.
"""
from __future__ import annotations

import json
import re

# `tmux new -s <session> -c <workdir>`, the create half of each alias's
# attach-or-create. Anchored on the flags rather than on line shape, since the
# RemoteCommand line also carries an OSC profile escape and an `attach` clause.
# The workdir stops before `)`, which closes the `ensure-server && (...)` group
# the aliases wrap around attach-or-create. No workdir holds `)` (see
# `_SAFE_WORKDIR`), so this loses nothing.
_TMUX_NEW = re.compile(r"tmux\s+new\s+-s\s+([A-Za-z0-9._-]+)\s+-c\s+([^\s)]+)")

# Keys whose values are themselves maps of user choices, merged one level down.
# `sessions` is the reason this module exists: a user with three themes picked
# must keep those three and gain the rest.
_MERGE_NESTED = ("sessions",)

# Mirrors Deck's own `validateWorkingDirectory`: absolute or `~/`-relative, no
# `..`, and a charset with no shell metacharacter in it. Deck applies this again
# on read, so a value failing here is one Deck would drop anyway — writing it
# would produce a config whose rail silently loses directories.
#
# Enforced at EXTRACTION rather than left to the caller. Today the only input is
# this repo's own installer script, but the function is public, and a caller who
# later points it at a user-editable ssh config should not be the one holding
# the guard: the workdir ends up interpolated into a remote shell command.
_SAFE_WORKDIR = re.compile(r"(?:/|~/)[A-Za-z0-9._\-/~]*\Z")


def _safe_workdir(path: str) -> bool:
    return bool(path) and ".." not in path and bool(_SAFE_WORKDIR.match(path))


def rail_from_ssh_config(text: str, names: dict | None = None) -> list[dict]:
    """Rail rows derived from the ssh aliases, in the order they appear there.

    Order is load-bearing — the rail sorts stably so muscle memory survives — and
    the ssh config's order is the one the operator already reads top to bottom.

    A session seen twice keeps its first position. `name` is emitted only when it
    differs from the session name, since Deck falls back to the session name and
    writing `{"session": "deck", "name": "deck"}` is noise.

    A workdir that fails `_safe_workdir` costs the row its directory, not its
    place on the rail — the same degradation Deck's own parser applies, so the
    session still attaches and only a first-create loses its starting point.
    """
    names = names or {}
    rows: list[dict] = []
    seen = set()
    for session, workdir in _TMUX_NEW.findall(text):
        if session in seen:
            continue
        seen.add(session)
        row = {"session": session}
        display = names.get(session)
        if display and display != session:
            row["name"] = display
        if _safe_workdir(workdir):
            row["workdir"] = workdir
        rows.append(row)
    return rows


def merge(existing: dict, seed: dict) -> tuple[dict, list[str]]:
    """Seed into existing without overwriting anything the user set.

    Returns the merged config and a list of what was filled, so the installer can
    report what it did rather than claiming a blanket success. Reporting nothing
    filled is a useful answer, not a failure.

    A key the user holds wins, including a key holding an empty value: `"rail":
    []` is a user saying they want no fleet rows, and refilling it would override
    a deliberate choice with a default. Only an ABSENT key is filled.

    Does not mutate either argument. The nested merge below copies before it
    writes, since a shallow `dict()` would leave the caller's inner dict aliased
    and quietly populate it — a promise this docstring's first line would then be
    breaking.
    """
    merged = dict(existing)
    filled: list[str] = []

    for key, seed_value in seed.items():
        if key not in merged:
            merged[key] = seed_value
            filled.append(key)
            continue
        if key in _MERGE_NESTED and isinstance(merged[key], dict) and isinstance(seed_value, dict):
            added = [k for k in seed_value if k not in merged[key]]
            if added:
                # Copy before writing, or this writes through to the caller's own
                # dict: `dict(existing)` above is shallow, so merged[key] and
                # existing[key] are the same object until this line breaks it.
                merged[key] = dict(merged[key])
                # The user's own entries are preserved by only writing absent
                # keys, rather than by dict-union order, which would depend on
                # argument order and read as a bug waiting to happen.
                for k in added:
                    merged[key][k] = seed_value[k]
                filled.extend(f"{key}.{k}" for k in sorted(added))

    return merged, filled


def seed_document(theme_json: str, ssh_config: str) -> dict:
    """The full seed: Convivy's theme mapping plus a rail derived from the aliases.

    `railNames` is consumed here rather than passed through, since it exists only
    to label rail rows and would be meaningless in the installed file.

    A RAIL NAME IS NOT A PROJECT DISPLAY, and nothing binds the two. ARCH-426 draws
    the line explicitly: the Keel owns what projects exist, the operator owns which
    are pinned to the rail and what they are called there. So `railNames` carries
    keys that are no project at all (`ask`, the `*-commercial` sites, and `steward`
    — whose crew was retired on 2026-08-06 and whose rail key is the operator's to
    keep or drop, not this module's), omits registered projects with no co session
    of their own, and may legitimately disagree on
    a label — `svc` is "Convivy Kit" here and "App Kit" in the registry. Do not
    "fix" that by making one mirror the other; a drift test between them would
    assert a coupling this architecture rejects.

    What DOES have to track the registry is a rail name that has gone plain wrong,
    which is how the NYH split reached this file: three properties were renamed and
    two rail labels kept the old names (reviewer, PR #339).
    """
    doc = json.loads(theme_json)
    names = doc.pop("railNames", {})
    rail = rail_from_ssh_config(ssh_config, names)
    if rail:
        doc["rail"] = rail
    return doc


def merge_file(existing_text: str | None, theme_json: str, ssh_config: str) -> tuple[str, list[str]]:
    """Merge for the installer: text in, text out, plus what was filled.

    An unparseable existing file is treated as absent rather than as a reason to
    fail. The alternative is refusing to install because the user hand-edited
    their config into invalid JSON, which helps nobody — and Deck itself already
    degrades a malformed config to defaults, so this matches what the app does.
    """
    existing: dict = {}
    if existing_text:
        try:
            loaded = json.loads(existing_text)
            if isinstance(loaded, dict):
                existing = loaded
        except ValueError:
            existing = {}

    merged, filled = merge(existing, seed_document(theme_json, ssh_config))
    # Sorted keys so a hand-edited file and an installer-written one stay
    # diffable, matching what Deck's own writer does.
    return json.dumps(merged, indent=2, sort_keys=True) + "\n", filled
