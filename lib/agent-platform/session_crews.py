"""session_crews — the crew specs `new-crew` writes, served to Deck as one envelope.

WHY THIS EXISTS. Crew creation has one path (the operator, 2026-09-03): the steward's
`new-crew` skill makes the directory and the tmux session and writes a spec per
crew to `~/.config/convivy/deck/crews.d/<id>.json`. Deck reads its own config
on the laptop, so nothing read those files — a crew created through the only
sanctioned path got a rail row and no cockpit (placard, 2026-09-05). Deck
already opens an ssh connection to this host every poll, so this verb is the
read side: the specs, as `{"crews": [...]}`, shaped like `sessions` so Deck's
decode discipline applies (a row it cannot parse is skipped; only a malformed
top-level payload is a failure).

THE FILES ARE DATA. They are written by an agent and read by this host, and a
spec's `workingDirectory` reaches an ssh command line on Deck's side. Every
field is type-checked and bounded here, unknown keys are dropped, and a file
that is not a JSON object naming its own filename as `id` is skipped rather
than served. Nothing from a spec reaches a shell in this module.

TWO FIELDS DEFAULT FROM THE HOST when a spec leaves them out, because a crew
without them has a rail row and no cockpit (Deck notes on
issue_01M1MW2E4G5SJPWNCMNH0KCJES, 2026-09-06). `project` defaults to the
registry slug whose home directory contains `workingDirectory`, by inode, the
way the transport wall matches a directory; `bead` defaults to the one bead the
index holds for that project. A spec's own value always wins — `bead` and
`project` are orthogonal on purpose (a crew can work another product's bead,
shipwright does) — and `derived` names which fields the host filled, so a
reader can tell a choice from a default.

A PRIVATE CREW'S ROW IS NOT SERVED, and that is ADR-0043's posture rather
than a choice made here. `bin/run-register-api` drops a black-box crew's row
whole from `/runs` and `/sessions`; this verb is the same class of surface
(unauthenticated the way every session-agent verb is), and `ea.json` was on
the host with its real directory and `project: ea` the day this shipped
(security-reviewer, knowledge#362). A row is dropped when its `project` names
a private slug, when its `workingDirectory` lies under a private crew's home
by inode (a symlink into one included), or when the question cannot be
answered — no usable `workingDirectory`, or a registry this host cannot read,
in which case nothing is served at all. Failing closed here costs a rail row;
failing open would put a private crew's path and Library linkage on every
poll. An explicit `project` must also be a registered slug, or it is treated
as absent and derived, so a spec cannot bind a cockpit to a project it
merely names. THE REGISTRY IS READ ONCE PER LISTING — private slugs, their
homes, the registered slugs and the derivation candidates, all from one
snapshot — so withholding and derivation cannot disagree within a call, the way `run-register-api` snapshots
slugs and homes together (its own docstring names the two-fresh-reads bug it
replaced; security-reviewer, knowledge#362 round 2).

DEGRADES, NEVER FAILS. No `crews.d`, an absent index, a spec with a field it
cannot vouch for: each reads as fewer rows or fewer fields, never an error,
because this runs on a poll that must not error a whole rail. The one
exception is the registry, above, where the safe answer is an empty list.
"""
from __future__ import annotations

import json
import os
import pwd
import re
import sqlite3
import sys
from pathlib import Path

# Siblings resolve from THIS directory first, the way session_state pins
# itself. This module imports only `projects` on purpose: importing
# session_lifecycle here re-adds the deployed farm to sys.path mid-way through
# the test suite's alphabetical import of this directory, and `session_input`
# then resolved from ~/.local/lib instead of the tree under test.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import projects  # noqa: E402


def _real_account_home() -> Path:
    """The account's home from the passwd database, not `$HOME` — the same
    choice session_lifecycle makes, for the same reason: an ssh or launchd
    invocation carries whatever `$HOME` it was given, and a crews.d located
    from it would move with it."""
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError):
        return Path.home()


# Read fresh per call and never cached: a spec is written by another process,
# and a stale answer here is a crew Deck cannot see.
CREWS_D = _real_account_home() / ".config" / "convivy" / "deck" / "crews.d"
KB_INDEX = _real_account_home() / ".knowledge-index" / "index.db"

# The session-name charset every session-agent verb shares
# (tmux_transcript.SESSION_NAME_RE); copied rather than imported for the reason
# above, and pinned equal by a test.
SESSION_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")

MAX_SPEC_BYTES = 64 * 1024
MAX_CREWS = 200
MAX_CHILD_SESSIONS = 50
MAX_TEXT = 256
MAX_PATH = 1024

# tmux's `#{session_id}`: `$` and digits, nothing else.
TMUX_SESSION_ID_RE = re.compile(r"\$\d{1,9}")
# A Library document id, the shape `knowledge.ID_PATTERN` admits minus the
# project allowlist — the index, not this module, says whether it exists.
DOC_ID_RE = re.compile(r"[a-z][a-z0-9-]*/[a-z][a-z-]*/[a-z0-9][a-z0-9-]*")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _text(value, *, limit: int = MAX_TEXT, pattern: re.Pattern | None = None) -> str | None:
    """A bounded, control-free string, or None. `pattern` must fullmatch."""
    if not isinstance(value, str) or not value or len(value) > limit:
        return None
    if _CONTROL.search(value):
        return None
    if pattern is not None and not pattern.fullmatch(value):
        return None
    return value


def _child(row) -> dict | None:
    """One pinned child session, or None when the row cannot be trusted."""
    if not isinstance(row, dict):
        return None
    name = _text(row.get("name"), pattern=SESSION_NAME_RE)
    if name is None:
        return None
    return {
        "name": name,
        "displayName": _text(row.get("displayName")),
        "sessionId": _text(row.get("sessionId"), limit=16, pattern=TMUX_SESSION_ID_RE),
    }


def _candidate_homes(registered: set[str], private: set[str]) -> list[tuple[str, Path]]:
    """The `(slug, home)` pairs a cockpit may derive its project from, from the
    listing's one snapshot: ACTIVE, PUBLIC slugs whose home exists. A sunset
    project is not a cockpit's home, and a private one is never derived (a
    directory under one is withheld before derivation runs)."""
    homes = []
    for slug in sorted(registered):
        if slug in private:
            continue
        home = projects.home_path(slug)
        if home is not None and home.is_dir():
            homes.append((slug, home))
    return homes


def _project_for(working_directory: str | None,
                 candidates: list[tuple[str, Path]]) -> str | None:
    """The candidate slug whose home contains `working_directory`, by inode.

    Walks up from the directory so a crew whose cockpit sits one level inside
    its project's home still resolves. Compared with `samefile`, not by string,
    for the reason `private_crew` gives: a symlink or a differently-cased path
    reaches the same directory under another spelling. `candidates` comes from
    the listing's snapshot, so derivation and withholding read one registry.
    The directory absent: None, and the spec's row is served without it.
    """
    if not working_directory:
        return None
    try:
        homes = candidates
        here = Path(working_directory)
        if not here.is_dir():
            return None
        for _ in range(64):
            for slug, home in homes:
                try:
                    if os.path.samefile(here, home):
                        return slug
                except OSError:
                    continue
            parent = here.parent
            if parent == here:
                return None
            here = parent
    except Exception:  # noqa: BLE001 - a default, never a failure
        return None
    return None


def _bead_for(project: str | None) -> str | None:
    """The one bead the index holds for `project`, or None.

    Read-only, and None on anything unexpected: no index, no row, or more than
    one row, since guessing between two beads is worse than none.
    """
    if not project:
        return None
    try:
        conn = sqlite3.connect(f"file:{KB_INDEX}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT id FROM docs WHERE kind = 'bead' AND project = ? "
                "AND source = 'files' LIMIT 2", (project,)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if len(rows) != 1:
        return None
    return _text(rows[0][0], pattern=DOC_ID_RE)


def _private_homes(private: set[str]) -> list[Path]:
    """The existing home directories of the private slugs in one snapshot."""
    homes = []
    for slug in sorted(private):
        home = projects.home_path(slug)
        if home is not None and home.is_dir():
            homes.append(home)
    return homes


def _withheld(working_directory: str | None, project: str | None,
              private: set[str], private_homes: list[Path]) -> bool:
    """Whether a row must be dropped whole on ADR-0043 grounds.

    True when the spec names a private slug, when its directory lies under a
    private home by inode (walking up, so a subdirectory or a symlink into one
    counts), or when the answer cannot be determined — no usable directory —
    because a privacy control must not fail open. `private` and
    `private_homes` come from the one snapshot `crews()` takes per listing.
    """
    if project is not None and project in private:
        return True
    if not working_directory:
        return True
    try:
        here = Path(working_directory)
        if not here.is_dir():
            return True
        for _ in range(64):
            for home in private_homes:
                # An OSError comparing against a private home is "cannot
                # tell", and cannot tell withholds — the one place this used
                # to `continue` was the one place the function failed open
                # (security-reviewer, #507 round 1).
                if os.path.samefile(here, home):
                    return True
            parent = here.parent
            if parent == here:
                return False
            here = parent
    except OSError:
        return True
    return True  # a walk that never reached the root: cannot tell


def _crew(path: Path, private: set[str], private_homes: list[Path],
          registered: set[str], candidates: list[tuple[str, Path]]) -> dict | None:
    """One spec file as a served row, or None when the file is skipped or
    withheld."""
    # A BOUNDED READ, not stat-then-read: a writer swapping the file between
    # the two calls could make the read as large as it liked. Reading one byte
    # past the cap is what tells an over-limit file from one exactly at it.
    # `RecursionError` is caught by name: a 4 KiB file of nested brackets
    # raises it from json.loads, it is a RuntimeError rather than a ValueError,
    # and uncaught it took the whole listing down rather than one row
    # (security-reviewer, #507 round 1).
    try:
        with path.open("rb") as fh:
            data = fh.read(MAX_SPEC_BYTES + 1)
        if len(data) > MAX_SPEC_BYTES:
            return None
        spec = json.loads(data.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        return None
    if not isinstance(spec, dict):
        return None
    crew_id = _text(spec.get("id"), limit=64, pattern=SESSION_NAME_RE)
    # The filename is the write side's own claim about the id; a file that
    # disagrees with itself is not a spec this host will vouch for.
    if crew_id is None or crew_id != path.stem:
        return None

    working_directory = _text(spec.get("workingDirectory"), limit=MAX_PATH)
    if working_directory is not None and not working_directory.startswith("/"):
        working_directory = None

    derived: list[str] = []
    project = _text(spec.get("project"))
    if _withheld(working_directory, project, private, private_homes):
        return None
    # A named project binds a cockpit to that project's Library content, so it
    # has to be a registered slug; anything else is treated as absent.
    if project is not None and project not in registered:
        project = None
    if project is None:
        project = _project_for(working_directory, candidates)
        if project is not None:
            derived.append("project")
    bead = _text(spec.get("bead"), pattern=DOC_ID_RE)
    if bead is None:
        bead = _bead_for(project)
        if bead is not None:
            derived.append("bead")

    raw_sessions = spec.get("sessions")
    children = []
    if isinstance(raw_sessions, list):
        for row in raw_sessions[:MAX_CHILD_SESSIONS]:
            child = _child(row)
            if child is not None:
                children.append(child)

    return {
        "id": crew_id,
        "displayName": _text(spec.get("displayName")),
        "workingDirectory": working_directory,
        "sessionId": _text(spec.get("sessionId"), limit=16, pattern=TMUX_SESSION_ID_RE),
        "host": _text(spec.get("host")),
        "project": project,
        "bead": bead,
        "sessions": children,
        "derived": derived,
    }


def crews() -> dict:
    """Every crew spec under `crews.d` this host will serve, as
    `{"crews": [...]}`, sorted by id."""
    try:
        paths = sorted(p for p in CREWS_D.iterdir()
                       if p.suffix == ".json" and p.is_file())
    except OSError:
        return {"crews": []}
    # One snapshot of the registry for the whole listing. Unreadable means
    # nothing can be judged, so nothing is served (see the module docstring).
    try:
        private = set(projects.private_slugs())
        private_homes = _private_homes(private)
        registered = set(projects.active_slugs())
        candidates = _candidate_homes(registered, private)
    except Exception:  # noqa: BLE001 - fail closed, never open
        return {"crews": []}
    rows = []
    for path in paths[:MAX_CREWS]:
        # One bad spec costs one row, never the listing: whatever a file can
        # make the parser raise, the poll still serves every other crew.
        try:
            row = _crew(path, private, private_homes, registered, candidates)
        except Exception:  # noqa: BLE001 - a row, not the rail
            row = None
        if row is not None:
            rows.append(row)
    return {"crews": rows}
