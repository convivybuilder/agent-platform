"""Leases and a write journal for KB writes (ARCH-408).

Two protections that do different jobs, and both are needed:

- **The lease** stops two sessions doing work that will be thrown away. It is
  advisory — holding it is not what makes a write safe.
- **The content-sha precondition** stops the lost update. It is mandatory, and
  it is checked inside the write lock against the file as it exists on fresh
  `origin/main`, so nothing can slip between the check and the commit.

The lease semantics deliberately copy the ops plane's issue claims rather than
inventing a second vocabulary: a TTL, a silent same-holder refresh, a takeover
once the TTL has passed, and a journal entry recording the takeover. A co that
checks out a doc and then dies must not hold it forever.

The store is its own SQLite file, beside the search index and deliberately not
inside it. `~/.knowledge-index/index.db` is derived state — it can be deleted
and rebuilt from git at any time, and `kb sync --full` does exactly that. Leases
are operational state that must survive a reindex, so mixing them would make the
index undeletable.

Paths come from the environment so tests can point at a temporary store:

    KB_WRITES_DB     default ~/.knowledge-index/kb-writes.db
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB = Path(os.environ.get("KB_WRITES_DB",
                         str(Path.home() / ".knowledge-index" / "kb-writes.db")))

# Four hours, against the issue queue's 24. An issue claim spans a work item; a
# doc lease spans one editing pass inside a session. A dead session should stop
# blocking a contended doc within the same working day, not the next one.
LEASE_TTL_SECONDS = 4 * 3600

# The journal's column list, shared verbatim between the fresh-database SCHEMA
# below and `_migrate_journal_author_kind`'s ALTER TABLE, so an old database
# brought up to date and a new one created from scratch land on the exact same
# shape.
JOURNAL_COLUMNS_SQL = """(
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    verb         TEXT NOT NULL,
    source_ref   TEXT,
    -- The account that RAN the write (a run-id or `session:<user>`). Says
    -- nothing about who COMPOSED it — that is author_kind/person below.
    holder       TEXT,
    -- The write-policy tier the write was made under: `in-session`,
    -- `person`, `proposal`, or a NO_LONGER_DIRECT kind kb_audit still reads
    -- historically. A different fact from author_kind: `in-session` and
    -- `proposal` are both an agent's doing, told apart only by who typed it.
    provenance   TEXT,
    -- Who composed the write. NULL means unknown — every row from before this
    -- column existed, never reinterpreted — and reads fail-closed as not a
    -- person. `agent` is every writer but one; `person` is set only by the
    -- verified Cloudflare Access path in `agentctl kb apply`, and only
    -- alongside the `person` column below.
    author_kind  TEXT CHECK (author_kind IN ('person', 'agent') OR author_kind IS NULL),
    -- The named person, set if and only if author_kind = 'person'.
    person       TEXT CHECK ((author_kind = 'person') = (person IS NOT NULL)),
    result       TEXT NOT NULL,
    detail       TEXT
)"""

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS leases (
    source_ref   TEXT PRIMARY KEY,
    holder       TEXT NOT NULL,
    acquired_at  TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    note         TEXT
);
CREATE TABLE IF NOT EXISTS journal {JOURNAL_COLUMNS_SQL};
CREATE INDEX IF NOT EXISTS idx_journal_ts  ON journal(ts);
CREATE INDEX IF NOT EXISTS idx_journal_ref ON journal(source_ref);
"""


def _now() -> datetime:
    """Current UTC time. A seam so tests can drive lease expiry without sleeping."""
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


# Every ALTER TABLE this migration runs completes in low single-digit
# milliseconds, so this is far more than the slowest disk this runs on should
# need — made explicit (this module has no other connection timeout to match)
# rather than left to the sqlite3 driver's own 5s default, so a second process
# waiting on the migrating one's write lock (see _migrate_journal_author_kind)
# gets a stated grace period instead of an implicit one.
MIGRATION_BUSY_TIMEOUT_MS = 5_000


def _journal_missing_author_kind(conn: sqlite3.Connection) -> bool:
    """Whether `journal` exists and still lacks `author_kind`. A plain read —
    no lock — so this can be called before deciding whether a migration is
    needed at all, and again inside the write lock to guard against a
    concurrent migrator finishing first."""
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "journal" not in tables:
        return False
    columns = {r[1] for r in conn.execute("PRAGMA table_info(journal)")}
    return "author_kind" not in columns


def _migrate_journal_author_kind(conn: sqlite3.Connection) -> None:
    """Add `author_kind`/`person` to a `journal` table created before this pair
    existed.

    Mirrors `indexer._migrate`: the schema's `CREATE TABLE IF NOT EXISTS`
    below only reaches a brand-new database, so an existing table needs its
    own ALTER TABLE, run first because the schema step is then a no-op. Two
    statements, not one, because SQLite's ALTER TABLE ADD COLUMN adds exactly
    one column per call; the second column's CHECK references the first, so
    it must land after. Both CHECKs are copied verbatim from
    `JOURNAL_COLUMNS_SQL`, so a table migrated here and a table created fresh
    end up with the identical constraint, not two that could drift apart.

    Existing rows get NULL in both columns — nothing here reinterprets a
    blank `provenance` row as anyone's write, agent or person.

    `ensure_db` calls this on every open, migrated database included, so the
    check for whether a migration is even needed runs first and unlocked —
    taking BEGIN IMMEDIATE up front made every open of an already-migrated db
    serialize on a write lock it never needed (reviewer, PR #691 round 2:
    eight concurrent reads against a migrated db at roughly twice the time,
    with higher variance, versus 9418ce8). Only when the columns are missing
    does this take the lock, re-checking inside it so a second process
    opening the same unmigrated db concurrently blocks, finds the columns
    already there, and does nothing — rather than racing the first process's
    ALTER and crashing with `duplicate column name` (reviewer, PR #691 round
    1).
    """
    conn.execute(f"PRAGMA busy_timeout={MIGRATION_BUSY_TIMEOUT_MS}")
    if not _journal_missing_author_kind(conn):
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        if _journal_missing_author_kind(conn):
            conn.execute(
                "ALTER TABLE journal ADD COLUMN author_kind TEXT "
                "CHECK (author_kind IN ('person', 'agent') OR author_kind IS NULL)")
            conn.execute(
                "ALTER TABLE journal ADD COLUMN person TEXT "
                "CHECK ((author_kind = 'person') = (person IS NOT NULL))")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def ensure_db() -> None:
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB))
    try:
        _migrate_journal_author_kind(conn)
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _connect() -> sqlite3.Connection:
    ensure_db()
    return sqlite3.connect(str(DB))


# --- content hashing ---------------------------------------------------------


def content_sha(text: str) -> str:
    """The precondition hash: sha256 over the WHOLE file, frontmatter included.

    Deliberately not the index's `body_hash`, which covers the body alone. An
    edit that changes only frontmatter — a status flip, an audience change — must
    still invalidate a concurrent writer's base, and a body-only hash would call
    those two versions identical.
    """
    return hashlib.sha256(text.encode()).hexdigest()


# --- leases ------------------------------------------------------------------


def lease_for(source_ref: str, *, now: datetime | None = None) -> dict | None:
    """The ACTIVE lease on a doc, or None. An expired lease reads as absent."""
    now = now or _now()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT source_ref, holder, acquired_at, expires_at, note "
            "FROM leases WHERE source_ref = ?", (source_ref,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    lease = {"source_ref": row[0], "holder": row[1], "acquired_at": row[2],
             "expires_at": row[3], "note": row[4]}
    if _parse(lease["expires_at"]) <= now:
        return None
    return lease


def acquire(source_ref: str, holder: str, *, ttl: int | None = None,
            note: str | None = None, now: datetime | None = None) -> tuple[dict | None, dict | None]:
    """Take a lease. Returns (lease, None) or (None, conflict).

    Three outcomes, matching `claim_issue`:
      - free, or the previous lease has expired -> granted (a takeover is journaled)
      - already held by THIS holder -> refreshed silently, so a re-checkout in the
        same session is not an error
      - held by someone else and unexpired -> conflict, naming the holder and the
        expiry so the caller can say who to wait for and how long
    """
    if not source_ref or not holder:
        raise ValueError("source_ref and holder are required")
    now = now or _now()
    ttl = LEASE_TTL_SECONDS if ttl is None else ttl

    existing = lease_for(source_ref, now=now)
    if existing and existing["holder"] != holder:
        return None, {"error": "leased", "source_ref": source_ref,
                      "holder": existing["holder"], "expires_at": existing["expires_at"]}

    took_over = False
    if not existing:
        conn = _connect()
        try:
            stale = conn.execute("SELECT holder FROM leases WHERE source_ref = ?",
                                 (source_ref,)).fetchone()
        finally:
            conn.close()
        took_over = bool(stale) and stale[0] != holder

    lease = {"source_ref": source_ref, "holder": holder,
             "acquired_at": _iso(now),
             "expires_at": _iso(now + timedelta(seconds=ttl)),
             "note": note}
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO leases (source_ref, holder, acquired_at, expires_at, note) "
            "VALUES (?,?,?,?,?)",
            (lease["source_ref"], lease["holder"], lease["acquired_at"],
             lease["expires_at"], lease["note"]))
        conn.commit()
    finally:
        conn.close()

    if took_over:
        journal("checkout", source_ref=source_ref, holder=holder, result="takeover",
                detail="previous lease had expired", now=now)
    return lease, None


def release(source_ref: str, holder: str, *, now: datetime | None = None) -> bool:
    """Drop a lease early. True if this holder held it.

    False rather than an exception when someone else holds it: releasing a lease
    you do not hold is a no-op worth reporting, not a crash, and session-end
    cleanup should never be the thing that fails a run.
    """
    now = now or _now()
    active = lease_for(source_ref, now=now)
    if not active or active["holder"] != holder:
        return False
    conn = _connect()
    try:
        conn.execute("DELETE FROM leases WHERE source_ref = ?", (source_ref,))
        conn.commit()
    finally:
        conn.close()
    return True


def active_leases(*, now: datetime | None = None) -> list[dict]:
    """Every unexpired lease, soonest expiry first. For debugging and the health surface."""
    now = now or _now()
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT source_ref, holder, acquired_at, expires_at, note "
            "FROM leases ORDER BY expires_at").fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        if _parse(r[3]) <= now:
            continue
        out.append({"source_ref": r[0], "holder": r[1], "acquired_at": r[2],
                    "expires_at": r[3], "note": r[4]})
    return out


def purge_expired(*, now: datetime | None = None) -> int:
    """Delete expired lease rows. Housekeeping only — `lease_for` already treats
    an expired row as absent, so this changes no decision, just the table size."""
    now = now or _now()
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM leases WHERE expires_at <= ?", (_iso(now),))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# --- journal -----------------------------------------------------------------


def _journal_insert(verb: str, *, source_ref: str | None, holder: str | None,
                     provenance: str | None, author_kind: str, person: str | None,
                     result: str, detail: str | None, now: datetime | None) -> None:
    """Shared insert body for `journal` and `journal_person`. Best-effort by design.

    The git log is the durable audit surface — every cage commit carries a
    `KB-Write:` trailer. This table adds what git cannot hold: the REFUSALS, and
    the lease takeovers, neither of which produce a commit. A journal failure
    must never fail the write it is describing, so this swallows its own errors.
    """
    now = now or _now()
    try:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO journal (ts, verb, source_ref, holder, provenance, "
                "author_kind, person, result, detail) VALUES (?,?,?,?,?,?,?,?,?)",
                (_iso(now), verb, source_ref, holder, provenance, author_kind, person,
                 result, detail))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        # Deliberately broad. The contract above is that recording a write can
        # never fail the write, and the ways this can fail are not only
        # sqlite3.Error — `ensure_db` alone can raise OSError on a read-only
        # filesystem or an uncreatable directory. Narrowing this to the errors
        # imagined today is how the guarantee gets broken by a new one tomorrow.
        pass


def journal(verb: str, *, source_ref: str | None = None, holder: str | None = None,
            provenance: str | None = None, result: str = "ok",
            detail: str | None = None, now: datetime | None = None) -> None:
    """Record a write attempt made by an agent — every writer but one.

    `author_kind` is always `'agent'` here, fixed rather than a parameter: the
    one exception is `journal_person`, a separate call that a caller reaches
    only by having a verified person to name. There is no kwarg on this
    function that a caller could pass `'person'` into by mistake.
    """
    _journal_insert(verb, source_ref=source_ref, holder=holder, provenance=provenance,
                    author_kind="agent", person=None, result=result, detail=detail,
                    now=now)


def journal_person(verb: str, *, person: str, source_ref: str | None = None,
                    holder: str | None = None, provenance: str | None = None,
                    result: str = "ok", detail: str | None = None,
                    now: datetime | None = None) -> None:
    """Record a write composed by a named, verified person.

    `person` is required and is never a caller-supplied string taken on
    trust: the only call site reaching this today is the Cloudflare Access
    path in `agentctl kb apply` (access_assertion.py), and only with the
    email that a verified assertion returned. Every other writer calls
    `journal` above, which can only ever record `'agent'` — this separate
    function is what makes recording `'person'` a deliberate call rather than
    a value a generic keyword could produce by accident.
    """
    if not person:
        raise ValueError("journal_person requires a verified person")
    _journal_insert(verb, source_ref=source_ref, holder=holder, provenance=provenance,
                    author_kind="person", person=person, result=result, detail=detail,
                    now=now)


def recent_journal(limit: int = 50, *, verb: str | None = None) -> list[dict]:
    conn = _connect()
    try:
        if verb:
            rows = conn.execute(
                "SELECT ts, verb, source_ref, holder, provenance, author_kind, person, "
                "result, detail FROM journal WHERE verb = ? ORDER BY id DESC LIMIT ?",
                (verb, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT ts, verb, source_ref, holder, provenance, author_kind, person, "
                "result, detail FROM journal ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()
    return [{"ts": r[0], "verb": r[1], "source_ref": r[2], "holder": r[3],
             "provenance": r[4], "author_kind": r[5], "person": r[6],
             "result": r[7], "detail": r[8]} for r in rows]


def as_json(obj) -> str:
    return json.dumps(obj, indent=2, sort_keys=True)


# --- write policy ------------------------------------------------------------

# Who may write straight to main, and on what evidence (ARCH-081 made mechanical,
# narrowed twice on 2026-09-13 by platform/decisions/0081 — first at about 21:55
# MST, then amended about 22:20 MST once the Library reported what the first
# ruling left standing).
#
# THIS IS THE WRITE-POLICY TIER, a different fact from the journal's
# `author_kind`. `in-session` and `proposal` are both an agent's doing; what
# tells them apart is the tier the write was made under, not who typed it.
# `author_kind` answers who composed the write instead — 'agent' for all of
# these but `person`, which is the one tier where the two happen to line up
# because it is granted only on a verified human assertion.
#
#   in-session     a decision the operator states himself, in that session, written
#                  directly and approved there (the operator, 2026-09-13:
#                  platform/decisions/0081). It does NOT cover a co's own
#                  corrections, reflows, readings or migrations — those go
#                  through kb_propose even with the operator present. The cage cannot
#                  tell a stated decision from any other in-session write, so
#                  this rests on the caller's own attestation, which is why
#                  every such write is journaled and carries a `KB-Write:`
#                  trailer for the weekly review.
#   person         a person typed this in the Orient editor. The cage accepts
#                  it only when a Cloudflare Access assertion arrives on stdin
#                  and verifies (access_assertion.py), and records the verified
#                  email in a `KB-Person:` trailer. That check is `agentctl kb
#                  apply`'s, not this module's: `policy_for` answers only which
#                  paths the provenance may reach. An agent has no assertion to
#                  present, so `kb_update` and `kb_create` refuse it outright.
#   proposal       everything else: it does not go to main at all, it opens a PR
#
# `verifier-pass`, `generated`, `keel` and `steward` are RECOGNIZED but no
# longer DIRECT.
#
#   `verifier-pass` (a kb-sync-verifier PASS on a merged PR) came off on the operator's
#   instruction — "Remove the verifier-pass" — once the Library reported it had
#   never actually been used.
#
#   `generated` (a registered automated generator writing only the paths it
#   owned, e.g. `ai-briefing`'s daily briefs) came off because the operator discontinued
#   what it existed for: "let's discontinue the daily briefs. They are a
#   distraction for now." The provenance is left with no caller.
#
#   `keel` and `steward` were an interactive session's mechanical/registry-edit
#   provenance, under two successive crew names, and both widened `in-session`
#   to cover any edit the session attested to — exactly what the ruling narrows
#   away: "in-session" now covers only a decision the operator states himself, with a PR
#   for everything else, "including my own edits" (the steward asked this in
#   session; the operator answered "YEs").
#
# All four are KEPT in PROVENANCE_KINDS rather than dropped, because they are
# written into journal rows and `KB-Write:` trailers already on disk: 48
# `generated` and 8 `steward` trailers on the knowledge repo's main, plus 3
# journal rows naming `keel`. kb_audit.py has to keep reading those as
# legitimate direct writes under the policy in force when they landed. A
# caller passing any of the four now gets the same shape of refusal `proposal`
# gets: named and pointed at `kb_propose`, never "unknown provenance".
DIRECT_PROVENANCE = ("in-session", "person")
NO_LONGER_DIRECT = ("verifier-pass", "keel", "steward", "generated")
PROVENANCE_KINDS = DIRECT_PROVENANCE + NO_LONGER_DIRECT + ("proposal",)

# The paths a `generated` claim was allowed to write while `generated` was
# still a direct provenance. `generated` no longer grants a write of its own
# (platform/decisions/0081, amended 2026-09-13) — this stays only because
# kb_audit.py uses it to judge a historical `generated` trailer against the
# bound that was in force when that commit landed, not to grant anything new.
# One entry per registered generator, with the PROJECT NAMED — not a wildcard
# segment.
#
#   projects/company/briefs/       ai-briefing, transformation (ARCH-372)
#   projects/platform/briefs/      ai-briefing, engineering (ARCH-372)
#
# Naming the projects is what made "adding a generator is a deliberate edit
# here" true while the grant was live, and it is what lets the audit still
# tell a historical claim on one of these paths from a claim anywhere else. A
# wildcard segment would have silently widened the old grant, and would now
# silently widen what the audit accepts as honest history. Security review of
# #245 caught the first version doing exactly that.
GENERATED_PROJECTS = ("company", "platform")
GENERATED_PATH_RE = re.compile(
    r"^projects/(?:" + "|".join(GENERATED_PROJECTS) + r")/briefs/[a-z0-9][a-z0-9.-]*\.md$")

# Paths that take the PR path under EVERY provenance.
#
# The roster the platform dispatches, and the profiles it activates, live inside
# the repo the crew edits. A bad commit here is a platform-wide outage: the
# watcher skips an invalid agent doc, and `resolve_agent` then reports the agent
# missing. Forcing these through review is today's discipline made enforceable
# rather than remembered.
#
# BOTH ARE GUARDED BY SEGMENT, and the roster's segment is new here (PR G). It
# used to be a `PR_ONLY_PREFIXES` entry naming `projects/shared/agents/`
# literally, which Phase 0 step 5 then re-filed onto `projects/agent-platform/`.
# A path-anchored guard survives that move only if someone remembers to edit it,
# and it fails SILENTLY when they do not: the cage keeps working and simply stops
# covering the 29 files that define what every agent may do. The migration had to
# carry both paths for three PRs to keep the window empty, and that whole dance
# was the prefix's cost, not the move's.
#
# `/profiles/` needed none of it — it matches wherever a profile sits, so it
# followed the re-file on its own. `/agents/` now does the same. The prefix
# mechanism is gone rather than emptied, because a guard listing one path reads
# like a decision about which paths are protected, when the real rule is that an
# agent document is PR-only wherever it lives.
#
# Over-matching is the safe direction here. A segment guards `<anything>/agents/`,
# so a future project's roster directory is covered the moment it exists rather
# than after someone notices.
#
# `/instruction-blocks/` JOINS THEM BECAUSE THE KIND GREW INTO THE RATIONALE
# ABOVE, not because a new risk was imagined. Until agent-platform#615 an
# instruction block was an ordinary document nothing read; `bin/knowledge-ctx`
# now composes every block whose selectors bind a session into that session's
# CLAUDE.md, and `agentctl spawn` names the agent so the agent-scoped ones
# resolve. A `scope: fleet` block carries no selectors and binds every session on
# the machine. That is the same sentence as the first paragraph here — a bad
# commit is a platform-wide change to what every agent does — arriving at a path
# the list predates.
#
# It closes the direct-write route that `in-session` leaves (platform/decisions/
# 0081 refused the other four): it is honoured rather than verified, and this
# guard is what makes the high-blast-radius paths refuse it under EVERY
# provenance, `person` included. The roster is the
# precedent, and the argument is the same one: today's discipline made
# enforceable rather than remembered.
PR_ONLY_SEGMENTS = ("/profiles/", "/agents/", "/instruction-blocks/")

# The agent policy file: one per knowledge repository, at `platform`, never per
# project, so it is guarded as one EXACT path rather than a segment. Shipwright's
# rule is that no agent merges a PR that changes it, so nothing may write it
# directly under any provenance. `.yaml` only: a reader looking for that name
# never loads a `.yml`.
POLICY_PATH = "projects/platform/policy/agent-policy.yaml"


# What each guarded segment protects, and what a bad commit to it costs. The
# refusal quotes this, and `agentctl kb apply` writes that refusal verbatim into
# the write journal's `detail` column — so a wrong clause here is not a cosmetic
# slip, it is a permanent audit record saying the write was refused for a reason
# that did not apply to it.
PR_ONLY_REASONS = {
    "/profiles/": ("a profile path",
                   "a bad commit here breaks agent dispatch platform-wide"),
    "/agents/": ("a roster path",
                 "a bad commit here breaks agent dispatch platform-wide"),
    "/instruction-blocks/": ("an instruction-block path",
                             "a block is composed into the CLAUDE.md of every "
                             "session its selectors bind, so a bad commit here "
                             "changes what agents obey"),
    POLICY_PATH: ("the agent policy file",
                  "it sets what agents may merge and how many may run, so a bad "
                  "commit here changes the limits on every agent"),
}


def pr_only_segment(source_ref: str) -> str | None:
    """The guarded segment (or the exact policy path) `source_ref` matches, or None.

    Split from `is_pr_only` so a refusal can say which protection it hit and
    what that one costs, rather than attributing every guarded path to the
    roster. Same folding, one place, so the two answers cannot disagree.
    """
    ref = source_ref.replace("\\", "/").casefold()
    if ref == POLICY_PATH.casefold():
        return POLICY_PATH
    for seg in PR_ONLY_SEGMENTS:
        if seg.casefold() in ref:
            return seg
    return None


def is_pr_only(source_ref: str) -> bool:
    """True if this path may only ever be changed through a reviewed PR.

    CASE-INSENSITIVE, and that is load-bearing rather than lenient. macOS ships a
    case-insensitive filesystem, so `PROJECTS/AGENT-PLATFORM/AGENTS/reviewer.md`
    opens the very same inode as the lowercase path and git stages the real roster
    entry — while a case-SENSITIVE match reads it as an ordinary doc and waves it
    through. That defeats the only hard control this module has, on the platform
    it actually runs on. Found by security review of #243 and reproduced live
    before fixing.

    Backslashes are folded to forward slashes first, so a separator swap cannot
    hide a guarded segment either.

    Delegates to `pr_only_segment` rather than repeating the fold, so the guard
    and the refusal that explains it can never answer differently.
    """
    return pr_only_segment(source_ref) is not None


def policy_for(source_ref: str, provenance: str) -> tuple[bool, str]:
    """May this write go straight to main? Returns (allowed, reason-if-not).

    The refusal always names `kb_propose` as the way through, so a caller that
    hits the policy learns the alternative instead of only the prohibition.
    """
    if provenance not in PROVENANCE_KINDS:
        return False, (f"unknown provenance {provenance!r} "
                       f"(expected one of: {', '.join(PROVENANCE_KINDS)})")
    seg = pr_only_segment(source_ref)
    if seg is not None:
        what, cost = PR_ONLY_REASONS[seg]
        return False, (f"{source_ref} is {what}, which takes the PR path under "
                       f"every provenance — {cost}. Use kb_propose.")
    if provenance in NO_LONGER_DIRECT:
        return False, (f"provenance {provenance!r} no longer writes to main directly "
                       f"(platform/decisions/0081, the operator, 2026-09-13): only a decision "
                       f"the operator states himself, in session, writes directly under "
                       f"'in-session' — everything else takes kb_propose, which opens "
                       f"a branch and a PR for review.")
    if provenance == "proposal":
        return False, ("provenance 'proposal' does not write to main by definition. "
                       "Use kb_propose, which opens a branch and a PR for review.")
    return True, ""


# A holder identity is a run-id or `session:<user>`. Constrained so it cannot
# carry a newline into the commit message and forge a second trailer line — the
# audit surface must not be corruptible by the thing it audits.
HOLDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,63}$")


def valid_holder(holder: str) -> bool:
    return bool(HOLDER_RE.match(holder or ""))


def message_is_trailer_safe(message: str | None) -> bool:
    """False if a caller-supplied commit body would inject its own trailer.

    Without this, `--message` (or a holder carrying a newline) can plant a second
    `KB-Write:` line, or a `KB-Person:` line naming a person the cage never
    verified. The bypass audit greps commit bodies for exactly that
    string, so a forged line is not cosmetic — it is the audit reading an
    attacker's claim instead of the cage's record.
    """
    if not message:
        return True
    return not any(ln.lstrip().startswith(("KB-Write:", "KB-Person:"))
                   for ln in message.splitlines())


def write_trailer(verb: str, source_ref: str, provenance: str, holder: str) -> str:
    """The machine-readable commit trailer that makes the git log the audit surface.

    Any commit on knowledge/main without one of these and without a merge parent
    is a write that bypassed the cage, which is a policy breach worth a signal.

    Every field is validated by the caller before it reaches here; the assert is
    a last line of defence rather than the check, because a trailer that lies is
    worse than no trailer at all.
    """
    if not valid_holder(holder):
        raise ValueError(f"invalid holder {holder!r}")
    for field in (verb, source_ref, provenance):
        # Whitespace, not just newlines. The trailer is four space-separated
        # fields, so a source_ref containing a space produces a structurally
        # malformed line that an audit scanner splitting on whitespace misparses.
        # No policy depends on trailer content, so this is audit fidelity rather
        # than a control — but a record the audit reads wrongly is worth as
        # little as one it cannot read at all.
        if any(c.isspace() for c in field):
            raise ValueError(f"trailer field {field!r} may not contain whitespace")
    return f"KB-Write: {verb} {source_ref} {provenance} {holder}"


# A conservative ceiling on the verified email that reaches a commit message.
MAX_PERSON_LEN = 254


def person_trailer(email: str) -> str:
    """The `KB-Person:` commit trailer naming the email the cage verified.

    The caller lowercases and strips it first; this raises on anything that
    would break the one-line shape, since a line carrying whitespace could
    smuggle a second field or trailer into the record the audit reads.
    """
    if not email or len(email) > MAX_PERSON_LEN or any(c.isspace() for c in email):
        raise ValueError(f"person {email!r} may not be empty, over {MAX_PERSON_LEN} "
                         f"characters, or contain whitespace")
    return f"KB-Person: {email}"
