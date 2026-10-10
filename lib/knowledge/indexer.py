"""Indexing primitives for the knowledge base — the one implementation.

`knowledge-watcher` (the launchd daemon) and `agentctl kb sync` both index the
same corpus into the same SQLite database. Before this module they could not:
the parse/upsert/reconcile logic lived inside the watcher script, so anything
else that wanted to index had to reimplement it and would drift.

The split of responsibility between the two callers:

- **`agentctl kb sync`** owns commit -> reindex as ONE step. It fast-forwards
  the shared checkout and indexes what the fast-forward changed, so a write is
  readable the moment the write returns.
- **The watcher** stays as the backstop for edits that arrive outside a sync —
  someone editing a doc in the checkout by hand — and as the recovery path when
  a sync is broken.

Nothing here imports `watchdog`. That dependency belongs to the daemon alone,
and keeping it out is what lets `agentctl` (a different interpreter, no venv)
import this module directly.

Paths are read from the environment at import so tests can point the whole
module at a temporary corpus:

    KNOWLEDGE_ROOT   default <root>/knowledge, <root> from builder_root
    KB_INDEX_DB      default ~/.knowledge-index/index.db
    KB_INDEX_SCHEMA  default ~/.local/lib/knowledge/schema.sql
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import stat
import sys
import time
from pathlib import Path

import yaml

from knowledge import (RosterRead, RosterUnavailable, instruction_block_errors,
                       project_catalogue, project_dir_errors, validate_frontmatter)

# THE KNOWLEDGE TREE LIVES UNDER THE MACHINE'S ONE ROOT (company/decisions/0008),
# resolved by builder_root. lib/knowledge/builder_root.py is a repo symlink to the
# agent-platform copy, so there is one implementation, standard-library only,
# which is what makes it safe to import into the knowledge venv.
#
# The import FAILS CLOSED, and says so. It used to fall back to a guessed
# ~/<name>/knowledge during the window before install.sh linked builder_root into
# ~/.local/lib/knowledge; that window has passed (the deployed knowledge venv
# imports it), and an indexer reading a guessed tree would index the wrong corpus
# while looking healthy. So a missing resolver stops the indexer with a line
# naming the import that failed, rather than letting it quietly stop indexing.
try:
    import builder_root as _BR
except ImportError as _exc:
    print(f"indexer: cannot import builder_root ({_exc}); refusing to guess where the "
          f"knowledge tree is. Run install.sh to link lib/knowledge/builder_root.py.",
          file=sys.stderr)
    raise

# An explicit KNOWLEDGE_ROOT is honoured as given. The documented index repair
# points it at a detached worktree at origin/main, and tests point it at a temp
# corpus; neither carries a root marker, so the check below never applies to it.
_EXPLICIT_ROOT = os.environ.get("KNOWLEDGE_ROOT")
if _EXPLICIT_ROOT:
    KNOWLEDGE = Path(_EXPLICIT_ROOT)
else:
    KNOWLEDGE = _BR.root() / "knowledge"
PROJECTS = KNOWLEDGE / "projects"
DB = Path(os.environ.get("KB_INDEX_DB",
                         str(Path.home() / ".knowledge-index" / "index.db")))
# Beside this module rather than at the installed path, so the schema and the code
# that applies it always travel together (the ARCH-229 deploy split again).
SCHEMA = Path(os.environ.get("KB_INDEX_SCHEMA",
                             str(Path(__file__).resolve().parent / "schema.sql")))

# Python's sqlite3 default busy timeout is 5s. The FTS repair below (rebuild +
# VACUUM) measured at 6.46s end-to-end on the live 3.19 GB index, so a writer
# — the watcher and `kb sync`/`kb apply` all call `ensure_db()` independently
# and, by design, concurrently — that opened a connection during a repair
# could already outlast the default and raise `sqlite3.OperationalError:
# database is locked`, the same crash class the WAL fix above exists to close.
# 30s is roughly 5x the measured repair time, margin for a slower disk or a
# larger corpus. The repair itself is now also serialized (`_repair_lock_path`),
# so this is defense in depth for the ordinary writes (`upsert`,
# `delete_by_source_ref`) that can land while a repair holds the write lock.
WRITER_BUSY_TIMEOUT_MS = 30_000

# The repair lock (`_repair_lock_path`) is bounded by the same margin as the
# SQLite busy timeout above: a holder that DIES releases the OS-level flock for
# free, but a holder that HANGS without exiting (VACUUM stuck on a full disk or
# slow I/O) must not block every other `ensure_db()` caller forever — that
# blocks the watcher, `kb sync` and `kb apply` with nothing printed while they
# wait, and the live index is in the leaked state, so every writer takes this
# path on first call after deploy (security-reviewer, PR #559 round 2).
REPAIR_LOCK_TIMEOUT_S = WRITER_BUSY_TIMEOUT_MS / 1000
REPAIR_LOCK_POLL_S = 0.1


def _connect_writer() -> sqlite3.Connection:
    """A write connection with `WRITER_BUSY_TIMEOUT_MS`. Every write path in this
    module goes through this rather than a bare `sqlite3.connect`, so none of
    them are left at the 5s default that the FTS repair can outlast."""
    conn = sqlite3.connect(str(DB))
    conn.execute(f"PRAGMA busy_timeout={WRITER_BUSY_TIMEOUT_MS}")
    return conn


_ROOT_REPORTED = False


def report_unconfirmed_root() -> str | None:
    """REPORT-ONLY check that the knowledge tree is under this machine's confirmed
    root (company/decisions/0008). Returns the problem, or None, and never refuses.

    Runs only when KNOWLEDGE is exactly the resolved `<root>/knowledge`: not under
    an explicit KNOWLEDGE_ROOT, not when a caller has repointed the module, and not
    before builder_root is linked. A problem is printed to stderr once per process,
    so it shows in the watcher's and the kb server's logs without flooding them.
    Refusing is a later change, once these reports show no correctly set-up
    machine would be refused.
    """
    global _ROOT_REPORTED
    if _BR is None or _EXPLICIT_ROOT:
        return None
    root = _BR.root()
    if Path(KNOWLEDGE) != root / "knowledge":
        return None
    problem = _BR.marker_problem(root)
    if problem and not _ROOT_REPORTED:
        _ROOT_REPORTED = True
        # Names the root it checked and its resolved path, the same three facts
        # builder_root.report_unconfirmed() prints for the data root.
        try:
            shown = Path(root).resolve()
        except (OSError, RuntimeError, ValueError):
            shown = root
        # repr(), as builder_root.report_unconfirmed() prints, so a path holding
        # a newline cannot forge a line in the log (security-reviewer, #551).
        print(f"indexer: knowledge root not confirmed (report-only): root() = {str(shown)!r}: "
              f"{problem!r}", file=sys.stderr)
    return problem


# --- database ---------------------------------------------------------------


def ensure_db() -> None:
    """Apply the canonical schema idempotently; safe to call on every startup.

    Sets `journal_mode=WAL` (persisted in the database file itself, so this only
    has to happen once — every later connection, including the `mode=ro` readers
    that never call `ensure_db`, inherits it). Under the old `DELETE` mode, a
    reader that opened mid-write found a hot rollback journal and had to roll it
    back to get a consistent view, which a `mode=ro` connection cannot do —
    `resolve_agent` crashed on exactly that (`sqlite3.OperationalError: attempt
    to write a readonly database`). WAL readers instead read the last-committed
    snapshot out of the WAL/main-file pair without ever touching the main file.
    """
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect_writer()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        _migrate(conn)
        conn.executescript(SCHEMA.read_text())
        conn.commit()
        _repair_fts_leak(conn)
    finally:
        conn.close()


def _repair_lock_path() -> Path:
    """The lock file beside the index db, serializing `_repair_fts_leak`'s
    rebuild + VACUUM (see there) across the watcher, `kb sync` and `kb apply` —
    all three call `ensure_db()` independently and, by design, concurrently."""
    return DB.with_suffix(".repair.lock")


def _open_repair_lock_file(lock_path: Path):
    """Open the repair lock file without following a symlink at that path.

    `O_NOFOLLOW` means a symlink planted at `lock_path` raises instead of being
    opened and locked. Nobody has observed such a symlink on the live index —
    planting one already needs write access to the lock file's own directory —
    so this is defense in depth, not a response to a seen incident.

    Raises `OSError` on that symlink case (`ELOOP`) and on any other failure
    to open the lock file (`EPERM` on a directory this process can't write to,
    `ENOSPC` on a full disk). The caller (`_repair_fts_leak`) catches this and
    degrades the same way the poll-timeout path already does, rather than
    letting it crash `ensure_db`.
    """
    fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o644)
    return os.fdopen(fd, "w")


def _acquire_repair_lock(fh) -> bool:
    """Take `fh`'s flock, bounded by `REPAIR_LOCK_TIMEOUT_S`. Returns whether it
    was acquired.

    Polls `LOCK_EX | LOCK_NB` rather than blocking on `LOCK_EX` outright, so a
    holder that hangs (VACUUM stuck on a full disk or slow I/O, rather than a
    holder that died and freed the lock for free) cannot wedge every other
    `ensure_db()` caller forever. Prints one line the first time a caller
    actually has to wait, so a stall is visible instead of silent.
    """
    deadline = time.monotonic() + REPAIR_LOCK_TIMEOUT_S
    waited = False
    while True:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if not waited:
                _repair_log(f"indexer: waiting on docs_fts repair lock "
                            f"({_repair_lock_path()}), held by another caller")
                waited = True
            if time.monotonic() >= deadline:
                return False
            time.sleep(REPAIR_LOCK_POLL_S)


def _repair_log(msg: str) -> None:
    """`print(msg, file=sys.stderr)`, swallowing any exception the write
    itself raises. A full disk (`ENOSPC`) makes `sys.stderr.write` itself
    raise `OSError`; without this, that fires from inside
    `_repair_fts_leak`'s own failure handler and escapes past the guard the
    handler exists to provide (round 6). Every print in `_repair_fts_leak`
    and its helpers goes through this rather than a bare `print`."""
    try:
        print(msg, file=sys.stderr)
    except Exception:
        pass


def _fts_counts(conn: sqlite3.Connection) -> tuple[int, int]:
    docs_n = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    fts_n = conn.execute("SELECT COUNT(*) FROM docs_fts_docsize").fetchone()[0]
    return docs_n, fts_n


def _verify_fts_postcondition(conn: sqlite3.Connection, docs_n: int) -> None:
    """A count match is not proof the postings point at the right rows — that is
    exactly what the leak this repairs looked like before it was diagnosed:
    `docs_fts_docsize` can equal `docs`'s count while a stale posting still
    points at the wrong rowid, if some other row was dropped to compensate.
    It's also the shape a future SQLite whose VACUUM renumbers rowids would
    take: rebuild's postings would point at rowids VACUUM had already moved,
    counts would still match, and search would return wrong documents again.

    So this isn't a count: pick one row and confirm a word from its own body
    resolves back to its own id through `docs_fts`, the same table `kb_search`
    queries. Best-effort — a doc whose body has no word to search on (empty,
    or all short/non-alphabetic tokens) is skipped rather than treated as a
    failure.
    """
    if docs_n == 0:
        return
    row = conn.execute("SELECT id, body FROM docs LIMIT 1").fetchone()
    if row is None:
        return
    doc_id, body = row
    word = next((tok for tok in
                 (t.strip(".,;:()[]{}\"'`") for t in body.split())
                 if len(tok) >= 5 and tok.isalpha()), None)
    if word is None:
        _repair_log(f"indexer: docs_fts rebuild postcondition skipped — doc "
                    f"{doc_id!r}'s body has no word of 5+ letters to search on")
        return
    matched = {r[0] for r in conn.execute(
        "SELECT id FROM docs_fts WHERE docs_fts MATCH ?", (f"body:{word}",)
    ).fetchall()}
    if doc_id not in matched:
        _repair_log(f"indexer: docs_fts rebuild postcondition FAILED — searching "
                    f"{word!r} (from doc {doc_id!r}'s body) did not resolve to it "
                    f"(matched: {sorted(matched)!r})")


def _repair_fts_leak(conn: sqlite3.Connection) -> None:
    """Rebuild `docs_fts` when its postings have drifted from `docs`.

    `upsert` used to write with `INSERT OR REPLACE`, which SQLite implements as a
    delete-then-insert that does NOT fire the `docs_ad` AFTER DELETE trigger
    (recursive_triggers is off), so the old row's FTS postings were never
    removed; the REPLACE also gives the new row a fresh rowid, so nothing ever
    lines back up. Every re-index of an existing document leaked one stale
    posting into `docs_fts` this way, and since `docs_fts` is external-content
    keyed on `docs.rowid`, a leaked posting can point at whatever unrelated
    document later reuses that rowid — on the live index this made search return
    wrong documents (confirmed: 'bcrypt' matched 64 rows, 33 with no such word).

    `upsert` now writes a real UPDATE, which fires `docs_au` and keeps the rowid
    stable, so this is a no-op once a database has no more REPLACE-written rows
    behind it. It runs on every `ensure_db` (every write-path startup) because
    the check is two cheap COUNT queries; the rebuild + VACUUM only run when
    they disagree.

    THE REBUILD + VACUUM ARE SERIALIZED (`_repair_lock_path`). `ensure_db` is
    called independently by the launchd watcher and by `agentctl kb sync` / `kb
    apply`, which run concurrently by design — a second rebuild+VACUUM starting
    while the first is still running is real write contention against the same
    file, not merely wasted work. The cheap count check above runs unlocked
    (the common case: it's a no-op on every already-repaired database, and
    taking a lock for that would serialize every `ensure_db` call on every
    startup for nothing); only a caller that actually sees a mismatch takes the
    lock, and then RE-CHECKS the counts before doing anything, so a caller that
    waited while another repaired it in the meantime finds the counts already
    matching and skips its own rebuild+VACUUM.

    THE LOCK WAIT IS BOUNDED (`REPAIR_LOCK_TIMEOUT_S`, `_acquire_repair_lock`).
    A holder that dies releases the OS-level flock for free — the watcher/kb
    sync race test covers that — but a holder that HANGS without exiting
    (VACUUM stuck on a full disk or slow I/O) must not wedge every other
    `ensure_db()` caller forever. A caller that times out skips the repair for
    THIS call; the index stays in the leaked state (still fully readable and
    writable, just with the stale `docs_fts` postings this function exists to
    clear) until a later call retries it. Its own write does not simply
    "proceed": it still goes through `_connect_writer`'s own
    `WRITER_BUSY_TIMEOUT_MS`, so the total time this caller can spend blocked
    is roughly the repair-lock timeout plus the busy timeout, and the write
    can still raise `sqlite3.OperationalError: database is locked` if the
    repair holder is mid-VACUUM when the busy timeout also runs out.

    OPENING OR LOCKING THE LOCK FILE CAN ALSO FAIL OUTRIGHT
    (`_open_repair_lock_file`, `_acquire_repair_lock`): a symlink planted at
    the lock path (`ELOOP`, via `O_NOFOLLOW`), a lock directory this process
    can't write to (`EPERM`), or a full disk (`ENOSPC`) all raise `OSError`
    rather than blocking. That degrades the same way as the poll timeout
    above — skip the repair for this call, leave the index in its leaked
    state, let a later call retry — rather than crashing `ensure_db` and, in
    the watcher, crash-looping the launchd job.

    THE WHOLE BODY RUNS UNDER ONE GUARD (security-reviewer, PR #559 round 5,
    replacing the per-step `try/except` this docstring used to describe).
    Round 4 wrapped seven steps individually and missed an eighth: the
    `conn.rollback()` calls the rebuild and VACUUM handlers made on their own
    way out are themselves unguarded `sqlite3` calls, and on a corrupted
    database — today's live incident — ROLLBACK can raise
    `sqlite3.OperationalError` same as any other statement, escaping to the
    watcher again. Wrapping that one call would be the same move for the
    third round running; the class is "any statement in this function can
    raise", and a per-statement guard can only ever cover the statements
    someone already thought to check. So the fix is not a ninth
    `try/except` — it is one `try/except Exception` around the whole
    sequence (the unlocked count pre-check, the lock directory `mkdir`, the
    lock file open, the flock acquire, the re-check under the lock, the
    rebuild INSERT, the VACUUM, the postcondition count, and
    `_verify_fts_postcondition`), with a local `step` variable updated
    before each one so the single handler's line still names what failed.

    ROUND 7: `except (OSError, sqlite3.Error)` was still too narrow.
    `_verify_fts_postcondition` reads a row's `body` back and calls
    `.split()` on it; the schema declares that column `NOT NULL`, but that's
    enforced only on write, and the corrupted page this whole repair exists
    for — the live index really had one — can read back anything, including
    `NULL`, which raises `AttributeError`, not `sqlite3.Error`. The guard now
    catches `Exception`, which reaches that and anything else in the
    sequence above, while `KeyboardInterrupt` and `SystemExit` still
    propagate (both are `BaseException`, not `Exception`).

    Every `print` in this function and its helpers (`_acquire_repair_lock`,
    `_verify_fts_postcondition`) goes through `_repair_log`, not a bare
    `print`, including the handler's own failure line below: on a full disk
    (`ENOSPC`), writing that line to stderr itself raises `OSError` from
    inside the handler, the same bug the round-5 rollback fix closed, now in
    the logging path. `_repair_log` swallows that write failure, so
    reporting a step's failure can never itself become the thing that
    escapes. The informational lines between two `step =` assignments now
    each follow their own `step =`, so a failure while logging one of them
    is attributed to the step it actually happened in rather than the
    previous one.

    The handler's own rollback attempt is inside a nested
    `try/except Exception: pass` — a rollback that itself raises (the
    round-5 finding) is swallowed rather than escaping a second time. The
    lock, if ever opened, is released in a `finally` that cannot itself
    raise (`flock`/`close` failures there are swallowed too), so
    `ensure_db` always completes and a later call retries. This does not
    extend to `ensure_db`'s own schema application or to the callers' own
    writes (`upsert`, `delete_by_source_ref`): those still raise as before.
    """
    step = "docs_fts count pre-check"
    lock_path = _repair_lock_path()
    fh = None
    try:
        docs_n, fts_n = _fts_counts(conn)
        if docs_n == fts_n:
            return

        step = "repair lock directory create"
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        step = "repair lock open"
        fh = _open_repair_lock_file(lock_path)

        step = "repair lock acquire"
        acquired = _acquire_repair_lock(fh)
        if not acquired:
            _repair_log(f"indexer: TIMED OUT after {REPAIR_LOCK_TIMEOUT_S:.0f}s "
                        f"waiting on docs_fts repair lock ({lock_path}) — "
                        f"skipping repair for this call, the index stays in "
                        f"its leaked state, a later caller will retry")
            return

        step = "docs_fts count re-check"
        docs_n, fts_n = _fts_counts(conn)
        if docs_n == fts_n:
            _repair_log("indexer: docs_fts leak already repaired by a "
                        "concurrent caller — skipping")
            return

        step = "docs_fts rebuild"
        _repair_log(f"indexer: docs_fts leaked (docs={docs_n} rows, "
                    f"docs_fts_docsize={fts_n} rows) — rebuilding")
        conn.execute("INSERT INTO docs_fts(docs_fts) VALUES('rebuild')")
        conn.commit()

        step = "docs_fts VACUUM"
        conn.execute("VACUUM")

        step = "docs_fts postcondition count"
        after = conn.execute(
            "SELECT COUNT(*) FROM docs_fts_docsize").fetchone()[0]

        step = "docs_fts postcondition result"
        if after != docs_n:
            _repair_log(f"indexer: docs_fts rebuild FAILED to reconcile — "
                        f"docs={docs_n} but docs_fts_docsize={after} after "
                        f"rebuild+VACUUM")
        else:
            _repair_log(f"indexer: docs_fts rebuild done (docs_fts_docsize "
                        f"now {after} rows, matching docs={docs_n})")

        step = "docs_fts postcondition verify"
        _verify_fts_postcondition(conn, docs_n)
    except Exception as exc:
        _repair_log(f"indexer: {step} step FAILED ({exc}) — skipping the "
                    f"rest of the repair for this call, the index stays in "
                    f"its leaked state, a later caller will retry")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        if fh is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                fh.close()
            except OSError:
                pass


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns `schema.sql` gained after this particular database was created.

    Every CREATE in the schema file is `IF NOT EXISTS`, which is what makes
    re-application safe and also what makes a NEW COLUMN reach a new database
    and never an existing one. So the column has to be added here.

    Runs BEFORE the schema file, because `idx_docs_model` indexes an expression
    over `frontmatter_extra` and CREATE INDEX fails outright on a column that is
    not there yet.

    Backfilling is not this function's job and does not need to be: `verify_index`
    compares `frontmatter_extra` as well as `body_hash`, so every row this leaves
    at the DEFAULT reports stale, and `kb sync`'s existing self-heal refills them
    on its next pass.
    """
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "docs" not in tables:
        return  # fresh database — the schema file creates it complete

    columns = {r[1] for r in conn.execute("PRAGMA table_info(docs)")}
    if "frontmatter_extra" not in columns:
        conn.execute("ALTER TABLE docs "
                     "ADD COLUMN frontmatter_extra TEXT NOT NULL DEFAULT '{}'")


# --- parsing ----------------------------------------------------------------


def split_frontmatter(text: str) -> tuple[object, str | None, str | None]:
    """`(frontmatter, body, None)`, or `(None, None, error)` when `text` carries no
    frontmatter this indexer can read. The one place the delimiter and YAML rules
    live, so `parse_doc` and `document_errors` cannot disagree."""
    if not text.startswith("---\n"):
        return None, None, "no frontmatter delimiter"
    parts = text.split("---\n", 2)
    if len(parts) < 3:
        return None, None, "malformed frontmatter"
    try:
        fm = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError as exc:
        return None, None, f"yaml parse error: {exc}"
    return fm, parts[2].strip(), None


def document_errors(text: str, rel: str | None = None) -> list[str]:
    """Why this indexer would drop a document with this text, or `[]` when it would
    index it: `parse_doc`'s own checks, for a caller holding text rather than a file
    under KNOWLEDGE. `agentctl kb apply` and `kb propose` ask this before they
    commit, so a write the index would drop is refused rather than reported as
    created and then invisible (issue_01M2BVA23ZC5EJHXXZN5VZB9VZ).

    `rel` is the repo-relative path the text will be written at. A write to
    `projects/<new>/` creates that directory, so the first document of a project
    the checkout does not hold yet is judged as if its directory existed."""
    fm, body, err = split_frontmatter(text)
    if err:
        return [err]
    try:
        return _validation_errors(fm, body, rel)
    except RosterUnavailable as exc:
        # A write the index cannot check is refused, so it cannot reach main and go
        # unindexed. The write is retried once the Library can be read.
        return [f"the indexer cannot check the Library's roster ({exc})"]


def _validation_errors(fm: object, body: str | None,
                       rel: str | None = None, roster: RosterRead | None = None) -> list[str]:
    """Every reason to refuse a parsed document. The instruction-block rules need
    the body, which `validate_frontmatter` never sees, so they run here, on the
    one path both `document_errors` and `parse_doc` take.

    `roster` is the pass's read (`RosterRead`). Both roster checks take the same one,
    so a document is deferred only on the roster's answer for its pass.

    The allowed projects come from the `projects/` directories of THIS module's
    KNOWLEDGE tree, read on every call, so a project added to the tree validates
    without a restart of the long-running watcher."""
    parts = (rel or "").split("/")
    pending = parts[1] if len(parts) > 2 and parts[0] == "projects" else None
    catalogue = project_catalogue(KNOWLEDGE, pending=pending)
    errs = project_dir_errors(catalogue, rel) if rel else []
    # One read for both roster-dependent checks, so a document is judged on one answer
    # even when the cache bound passes between them.
    read = RosterRead() if roster is None else roster
    # Both roster-dependent checks run even when the first could not finish, so a
    # refusal either of them holds is not lost behind a deferral. A document is
    # deferred only when the roster is the one thing it could not decide.
    unknown: RosterUnavailable | None = None
    try:
        errs += validate_frontmatter(fm, catalogue, read)
    except RosterUnavailable as exc:
        errs += exc.errors
        unknown = exc
    if isinstance(fm, dict) and fm.get("kind") == "instruction-block":
        try:
            errs += instruction_block_errors(fm, body or "", read)
        except RosterUnavailable as exc:
            errs += exc.errors
            unknown = unknown or exc
    # `stored_json` needs no roster, so it runs before the deferral is raised: a document
    # it cannot store is refused during an outage, not deferred.
    if isinstance(fm, dict) and not errs:
        try:
            stored_json(fm)
        except (TypeError, ValueError, RecursionError) as exc:
            errs.append(f"frontmatter cannot be stored: {type(exc).__name__}: {exc}")
    if unknown is not None and not errs:
        raise unknown
    return errs


def _refuse_unsafe_path(path: Path) -> str | None:
    """Why `path` looks unsafe to index on account of WHERE it resolves, or
    `None` if that much looks safe. DIAGNOSTIC ONLY — see `_open_doc_file`
    for the check that actually makes the read safe.

    This used to be the enforcement too: a single `path.resolve()` taken at
    one instant, trusted by a separate, later `open()`. `_open_doc_file`'s
    own whole-path open was already shown to race that (security-reviewer,
    PR #737 run-b75f48e0) — a writer could swap the LEAF for a symlink
    between this function's check and that open. Round 3 closed the leaf
    case with `O_NOFOLLOW` on the open, but the open was still by PATH, so
    the SAME race reopens one level up: this function's `resolve()` walks
    the whole path, including every ancestor directory, at one instant, and
    a writer can swap an ANCESTOR for a symlink before the later path-based
    open walks that same path again — the leaf itself never stops being an
    ordinary, non-symlink file, so a leaf-only `O_NOFOLLOW` has nothing to
    refuse (security-reviewer, PR #737 round 3, run-0ea3a971, reachable
    through ordinary `kb sync`/watcher concurrency on a type-changing
    fast-forward). Closing that needs the open itself to never re-walk a
    path string at all, which is what `_open_doc_file` now does — see there.

    So this function stays only for the error message: it can name "resolves
    outside the knowledge root" or "is a symlink" before anything touches the
    file, which is a more specific diagnostic than `_open_doc_file`'s
    component-open failure (`ELOOP`/`ENOTDIR` on whichever path component
    didn't open as expected) would otherwise give. `parse_doc` always
    follows this with `_open_doc_file`, whose result is the one trusted.
    """
    if path.is_symlink():
        return f"refusing to index a symlink ({path})"
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        return f"refusing to index {path}: cannot resolve ({exc})"
    try:
        resolved.relative_to(KNOWLEDGE.resolve())
    except ValueError:
        return f"refusing to index {path}: resolves to {resolved}, outside the knowledge root"
    return None


def _open_doc_file(path: Path):
    """Open `path` for text reading, by walking from a descriptor on
    `KNOWLEDGE` through every path component in turn — `O_NOFOLLOW` on each,
    `O_DIRECTORY` on every component but the leaf — rather than opening the
    leaf by its full path string.

    THIS, NOT `_refuse_unsafe_path`, IS WHAT MAKES THE READ SAFE.
    `_refuse_unsafe_path`'s whole-path `resolve()` and a path-based open of
    the leaf are two separate walks of the same path, taken at two
    different instants: round 3 made the open itself `O_NOFOLLOW`
    (closing a symlinked LEAF, security-reviewer run-b75f48e0), but the
    open was still `os.open(str(path), ...)` — one path string, resolved by
    the kernel ancestor-by-ancestor same as `resolve()` was — so a writer
    could swap an ANCESTOR directory for a symlink in the window between
    the two walks and the open would follow it straight through, same as
    `resolve()` would have (security-reviewer, PR #737 round 3,
    run-0ea3a971). `knowledge-ctx`'s `_open_root_fd`/`_open_container_fd`
    close the equivalent gap for the `.claude` tree the same way: a
    descriptor, once opened, is bound to the inode it named; resolving the
    NEXT path component relative to THAT descriptor (`dir_fd=`) rather than
    by a fresh path string means nothing a writer does to any ancestor
    after it was opened can redirect where the chain goes next. By the time
    this function reaches the leaf, every directory on the way to it has
    been proven, at the instant each was opened, to be a real directory,
    not a symlink — there is no string left to re-walk and no window left
    for a later swap to land in.

    `path` is assumed to be under `KNOWLEDGE` (every caller already requires
    this — `parse_doc`'s own `rel = path.relative_to(KNOWLEDGE)` after this
    call would raise otherwise); a path that is not returns an error rather
    than raising. A component equal to `.` or `..` is refused outright —
    `os.open(dir_fd=...)` resolves `..` by walking back up the real
    directory tree same as any path lookup, which is exactly the kind of
    string re-walk this function exists to avoid, so letting one through
    would reopen the gap one level further up for no legitimate reason (no
    caller ever constructs a knowledge-doc path containing one).

    THE LEAF'S `st_nlink` IS ALSO CHECKED, FOR A HARD LINK RATHER THAN A
    SYMLINK (security-reviewer, PR #737 round 5). `O_NOFOLLOW` refuses a
    symlink because a symlink is a different directory entry POINTING at an
    inode; a hard link is an ordinary directory entry — exactly like the
    legitimate leaf this whole chain exists to open — that happens to name
    the SAME inode as some other path, possibly outside `KNOWLEDGE`
    entirely. Nothing in the `dir_fd` chain above distinguishes that leaf
    from any other regular file: `O_NOFOLLOW` does not fire (there is no
    symlink to refuse) and `fstat` reports an ordinary regular file, so a
    hard link planted inside the knowledge tree to a file elsewhere on the
    same volume — a secrets file, confirmed reachable by the reviewer: the
    knowledge checkout and a machine-local secrets directory share one APFS
    device, and `ln` across them succeeds — would be read and indexed,
    content and all, with every other check in this function passing. A hard
    link's `st_nlink` is 2 or more (every directory entry naming the same
    inode counts); an ordinary file written by `kb_create`, `kb_update`,
    `kb_propose`'s apply, a hand-edit, or a `git checkout` of the knowledge
    repo is 1, since none of those ever hardlink into the working tree
    (confirmed: a recursive search for a working-tree file with more than
    one link, over both the live knowledge checkout and this repo's own
    working tree, returned zero rows, against a positive control of an
    `ln`-created hard link that the same search caught). So refusing
    `st_nlink != 1` costs no legitimate document.

    Returns `(file_object, None)`, open and unread, or `(None, error)`. The
    caller owns closing the returned file object.
    """
    try:
        rel = path.relative_to(KNOWLEDGE)
    except ValueError:
        return None, f"refusing to index {path}: not under the knowledge root ({KNOWLEDGE})"
    parts = rel.parts
    if not parts or any(part in (".", "..") for part in parts):
        return None, f"refusing to index {path}: path has no safe component walk"

    try:
        dir_fd = os.open(str(KNOWLEDGE), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        return None, f"refusing to index {path}: cannot open the knowledge root ({exc})"

    opened = [dir_fd]
    try:
        for i, part in enumerate(parts):
            is_leaf = i == len(parts) - 1
            flags = os.O_RDONLY | os.O_NOFOLLOW
            if not is_leaf:
                flags |= os.O_DIRECTORY
            try:
                fd = os.open(part, flags, dir_fd=dir_fd)
            except OSError as exc:
                walked = "/".join(parts[:i + 1])
                return None, (f"refusing to index {path}: cannot open {walked!r} "
                              f"without following a symlink ({exc})")
            opened.append(fd)
            dir_fd = fd

        try:
            st = os.fstat(dir_fd)
            if not stat.S_ISREG(st.st_mode):
                return None, f"refusing to index {path}: not a regular file"
            if st.st_nlink != 1:
                return None, (f"refusing to index {path}: hard link "
                              f"(st_nlink={st.st_nlink})")
        except OSError as exc:
            return None, f"refusing to index {path}: cannot stat the opened file ({exc})"

        f = os.fdopen(dir_fd)
        opened.pop()  # f now owns this fd; the finally below must not close it too
        return f, None
    finally:
        for fd in opened:
            try:
                os.close(fd)
            except OSError:
                pass


def parse_doc(path: Path, roster: RosterRead | None = None) -> tuple[dict | None, str | None]:
    """Read + validate a knowledge doc. Returns (fm_with_body, None) or (None, error).

    A doc that fails validation returns an error and is never indexed — a row
    with broken metadata is worse than an absent one, because `resolve_agent`
    and `kb_search` both trust what they read back. A doc with no refusal other than
    the roster's, when the roster cannot be read, raises RosterUnavailable instead:
    the caller defers it, and its row, if any, is left as it is. `roster` is the
    pass's read (`RosterRead`); without one the check reads the roster itself.

    `_refuse_unsafe_path` runs first, before anything touches the file's content, purely
    as a cheap, specific diagnostic; `_open_doc_file` is what actually makes the read
    safe, by walking to the file through a chain of fds rather than opening its path a
    second time (see there for why the two cannot be collapsed into one check-then-read
    step).
    """
    unsafe = _refuse_unsafe_path(path)
    if unsafe:
        return None, unsafe

    f, err = _open_doc_file(path)
    if err:
        return None, err
    try:
        text = f.read()
    except Exception as exc:
        return None, f"read failed: {exc}"
    finally:
        f.close()

    fm, body, err = split_frontmatter(text)
    if err:
        return None, err
    rel = path.relative_to(KNOWLEDGE)
    errs = _validation_errors(fm, body, str(rel), roster)
    if errs:
        return None, "; ".join(errs)

    fm["body"] = body
    fm["body_hash"] = hashlib.sha256(body.encode()).hexdigest()
    fm["source"] = "files"
    fm["source_ref"] = str(rel)
    fm["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # PyYAML decodes ISO date scalars to datetime.date objects.
    lr = fm.get("last_reviewed")
    if hasattr(lr, "isoformat"):
        fm["last_reviewed"] = lr.isoformat()

    return fm, None


# --- writes -----------------------------------------------------------------


# The frontmatter keys `docs` gives a column of their own, which is also every key
# `parse_doc` injects. Anything else in a document's frontmatter goes to
# `frontmatter_extra`, so a field nobody anticipated still reaches a reader.
COLUMN_FIELDS = frozenset({
    "id", "source", "source_ref", "kind", "project", "title", "status",
    "topics", "audience", "applies_to", "depends_on",
    "last_reviewed", "authors", "body", "body_hash",
    "notion_view", "updated_at",
})


def extra_fields(fm: dict) -> str:
    """Serialize the frontmatter fields no column names, as canonical JSON.

    Sorted keys and `default=str` make the output a function of the document
    alone, which is what lets `verify_index` compare a stored value against a
    freshly parsed one. Without the sort, two runs over the same file could
    produce different text and every row would read as stale; without the
    coercion, a document with a bare `decided: 2026-08-11` would raise, since
    PyYAML hands back a `datetime.date` and json cannot encode one.
    """
    return json.dumps({k: v for k, v in fm.items() if k not in COLUMN_FIELDS},
                      sort_keys=True, default=str)


# The list columns, in the order `upsert` binds them.
_LIST_COLUMNS = ("topics", "audience", "applies_to", "depends_on", "authors")


def stored_json(fm: dict) -> dict:
    """The JSON text `upsert` writes for this document's list columns and its
    `frontmatter_extra`. Raises TypeError or ValueError on a value that cannot be
    stored: a date inside a list, a mapping whose keys cannot be sorted together or
    are not JSON keys, a YAML anchor that refers to itself.

    Validation calls this same function rather than predicting it, so a document is
    refused for exactly the shapes `upsert` would raise on, and one malformed file
    cannot stop a whole-tree scan (security-reviewer, #601 rounds 1 and 2)."""
    return {"lists": [json.dumps(fm.get(f, [])) for f in _LIST_COLUMNS],
            "extra": extra_fields(fm)}


def upsert(fm: dict) -> None:
    """Write one document's row. An UPDATE on conflict, never a REPLACE.

    `INSERT OR REPLACE` deletes the conflicting row and inserts a new one with a
    new rowid, which does not fire `docs_ad` (SQLite skips delete triggers for a
    REPLACE unless recursive_triggers is on) and leaves `docs_fts` holding a
    posting for a rowid `docs` no longer has — see `_repair_fts_leak`. An
    `ON CONFLICT DO UPDATE` is a real UPDATE: it fires `docs_au`, which retires
    the old FTS posting and writes the new one, and it never changes the row's
    rowid, so the two tables cannot drift apart on a re-index.
    """
    stored = stored_json(fm)
    conn = _connect_writer()
    try:
        conn.execute(
            """
            INSERT INTO docs (
                id, source, source_ref, kind, project, title, status,
                topics, audience, applies_to, depends_on,
                last_reviewed, authors, body, body_hash,
                notion_view, updated_at, frontmatter_extra
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                source = excluded.source,
                source_ref = excluded.source_ref,
                kind = excluded.kind,
                project = excluded.project,
                title = excluded.title,
                status = excluded.status,
                topics = excluded.topics,
                audience = excluded.audience,
                applies_to = excluded.applies_to,
                depends_on = excluded.depends_on,
                last_reviewed = excluded.last_reviewed,
                authors = excluded.authors,
                body = excluded.body,
                body_hash = excluded.body_hash,
                notion_view = excluded.notion_view,
                updated_at = excluded.updated_at,
                frontmatter_extra = excluded.frontmatter_extra
            """,
            (
                fm["id"],
                fm["source"],
                fm["source_ref"],
                fm["kind"],
                fm["project"],
                fm["title"],
                fm["status"],
                *stored["lists"][:4],
                fm.get("last_reviewed"),
                stored["lists"][4],
                fm["body"],
                fm["body_hash"],
                int(bool(fm.get("notion_view", False))),
                fm["updated_at"],
                stored["extra"],
            ),
        )
        conn.commit()
    finally:
        conn.close()


def delete_by_source_ref(source_ref: str) -> None:
    conn = _connect_writer()
    try:
        conn.execute(
            "DELETE FROM docs WHERE source = 'files' AND source_ref = ?",
            (source_ref,),
        )
        conn.commit()
    finally:
        conn.close()


def _keeps_row(outage: bool) -> bool:
    """Whether the indexed rows survive the pass. The one rule every deletion decides by:
    the full scan, `verify_index`, `index_one`, `index_paths` and the watcher all reach it,
    through `_retire_path` or the full scan's reconcile.

    Outside an outage nothing is kept, so a row the pass did not account for goes. During
    an outage every row is kept, whatever its path, its id and whether its file still
    exists, until the first pass that reads the roster. A document that needs the roster is
    deferred and a refused one is not admitted, so neither touches its row. A document that
    needs no roster is admitted as usual."""
    return outage


# The reason a path whose file is gone is deferred with during an outage. The watcher and
# every caller that prints a gone-path deferral use this text, so one path reads the same
# everywhere. A roster deferral prints the roster's own reason instead.
GONE_REASON = "the file is gone; nothing is removed while the roster cannot be read"


def row_exists(rel: str) -> bool:
    """Whether the index holds a row at `rel`."""
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        return conn.execute(
            "SELECT 1 FROM docs WHERE source = 'files' AND source_ref = ? LIMIT 1", (rel,)
        ).fetchone() is not None
    finally:
        conn.close()


def kept_suffix(rel: str) -> str:
    """The ` (row kept)` that a deferred or refused line ends with. It is printed only when
    a row is there to keep, so a document that was never indexed does not claim one."""
    return " (row kept)" if row_exists(rel) else ""


def _retire_path(rel: str, outage: bool) -> bool:
    """Delete the rows at `rel`, unless `_keeps_row` keeps them. Returns True when they
    were kept, so the caller reports the path as deferred rather than removed."""
    if _keeps_row(outage):
        return True
    conn = _connect_writer()
    try:
        conn.execute("DELETE FROM docs WHERE source = 'files' AND source_ref = ?", (rel,))
        conn.commit()
    finally:
        conn.close()
    return False


def retire_vanished(rel: str, roster: RosterRead | None = None) -> bool:
    """Retire the rows of a path whose file is gone. Returns True when they were kept.

    No document is at the path, so nothing refuses it and only the outage keeps its rows.
    A moved document is a vanished path plus a new path with the same `id`. The new path
    is deferred during an outage, and the old row stays until the roster reads, which is
    the same result the full scan reaches."""
    read = RosterRead() if roster is None else roster
    return _retire_path(rel, read.unreadable())


# --- indexing passes --------------------------------------------------------


def is_indexable_rel(rel: str) -> bool:
    """Whether the indexer will serve this repo-relative path: a `.md` under `projects/`.

    The one rule for what the KB retrieves. Exporting it lets the write cage
    refuse a document the indexer would silently ignore, so a write cannot
    report success for a file no agent can ever retrieve.
    """
    return rel.endswith(".md") and rel.startswith("projects/")


def index_one(path: Path, roster: RosterRead | None = None) -> tuple[str, str | None]:
    """Bring the index into line with ONE doc on disk. The single-doc primitive.

    Every caller that indexes a single document goes through here — the targeted
    pass below, and the watcher's filesystem event handler. It exists because the
    delete-on-invalid rule was originally written into each of those separately,
    and the copy that got it wrong was the one nobody was looking at.

    Returns (action, error) where action is "upserted", "removed", "deferred", "refused",
    or "skipped". An invalid doc reports BOTH: action "removed" (its row is gone) and
    the validation error (so the caller can say why). A deferred doc reports action
    "deferred" with the reason in the error slot, and its row is left as it is: the
    roster could not be read, so the document is neither admitted nor refused. During an
    outage an invalid doc is refused, not deferred: it reports "refused" with its error,
    and no row is deleted (see `_keeps_row`).

    `roster` is the caller's pass read (`RosterRead`). A batch passes one object through
    every call, so its documents all see the same roster. The watcher passes none, and
    this call's own read decides the one event.
    """
    try:
        rel = str(path.relative_to(KNOWLEDGE))
    except ValueError:
        return "skipped", None
    if not is_indexable_rel(rel):
        return "skipped", None
    read = RosterRead() if roster is None else roster

    # A dangling symlink exists as a link, so it is walked and refused below, not treated
    # as a removal. The full scan walks it the same way.
    if not (path.exists() or path.is_symlink()):
        if retire_vanished(rel, read):
            return "deferred", GONE_REASON
        return "removed", None

    try:
        fm, err = parse_doc(path, read)
    except RosterUnavailable as exc:
        return "deferred", str(exc)
    if err:
        # A doc that no longer validates stops being served. Keeping its
        # last-good row would hand `resolve_agent` and `kb_search` metadata the
        # validator has already rejected, which they cannot detect — and would
        # leave `verify_index` reporting an `extra` row it can never clear. During an
        # outage it is refused and not admitted, and its row stays as it was (see `_keeps_row`).
        if _retire_path(rel, read.unreadable()):
            return "refused", err
        return "removed", err

    upsert(fm)
    return "upserted", None


def index_paths(rels: list[str]) -> dict:
    """Index a specific set of repo-relative paths. The targeted pass `kb sync` runs.

    `rels` are paths as git reports them (relative to the repo root). A path that
    no longer exists on disk is deleted from the index rather than skipped, which
    is what makes this correct for a fast-forward that removed a doc. While the roster
    cannot be read, a row is kept as the full scan keeps it (see `_keeps_row`).
    Non-`.md` paths and anything outside `projects/` are ignored, matching what the
    full scan walks.

    Returns {"indexed": n, "removed": n, "invalid": [(rel, error)],
             "deferred": [(rel, reason)], "refused": [(rel, error)], "skipped": n}.
    """
    report_unconfirmed_root()
    result: dict = {"indexed": 0, "removed": 0, "invalid": [], "deferred": [], "refused": [],
                    "skipped": 0}
    # One roster read for the whole pass; every path below is decided against it.
    roster = RosterRead()
    roster.unreadable()
    for rel in rels:
        action, err = index_one(KNOWLEDGE / rel, roster)
        if action == "deferred":
            result["deferred"].append((rel, err))
        elif action == "refused":
            result["refused"].append((rel, err))
        elif err:
            result["invalid"].append((rel, err))
            result["removed"] += 1
        elif action == "upserted":
            result["indexed"] += 1
        elif action == "removed":
            result["removed"] += 1
        else:
            result["skipped"] += 1
    return result


def full_scan_and_reconcile() -> dict:
    """Walk projects/, upsert valid docs, and drop the rows the walk shows are stale.

    While the roster is readable, a row whose path and id were not both seen is dropped.
    While it cannot be read, no row is dropped (see `_keeps_row`), and a refused document
    is reported as refused rather than invalid.

    The recovery pass: the watcher runs it at startup and `kb sync` runs it when
    verification finds the index and the working tree disagree.
    """
    report_unconfirmed_root()
    result: dict = {"indexed": 0, "removed": 0, "removed_refs": [], "invalid": [],
                    "deferred": [], "refused": []}
    # One roster read for the whole scan, decided at its start. Every document is checked
    # against that read, so the scan's mode cannot change partway through.
    roster = RosterRead()
    outage = roster.unreadable()
    # A document is seen under its path and its id together. A row whose path and id were
    # both seen is current; any other row for that path is stale.
    seen: set[tuple[str, str | None]] = set()
    for path in PROJECTS.rglob("*.md"):
        rel = str(path.relative_to(KNOWLEDGE))
        try:
            fm, err = parse_doc(path, roster)
        except RosterUnavailable as exc:
            result["deferred"].append((rel, str(exc)))
            continue
        if err:
            if outage:
                result["refused"].append((rel, err))
            else:
                result["invalid"].append((rel, err))
            continue
        # Only a doc that INDEXED counts as seen. Marking every
        # file on disk as seen — which is what this did when it lived in the
        # watcher — protects the stale row of a doc whose frontmatter broke, so the
        # reconcile can never drop it and `verify_index` reports `extra` on every
        # future run.
        seen.add((rel, fm["id"]))
        upsert(fm)
        result["indexed"] += 1

    conn = _connect_writer()
    try:
        rows = conn.execute("SELECT source_ref, id FROM docs WHERE source = 'files'").fetchall()
        for ref, doc_id in sorted(rows, key=lambda row: row[0]):
            if (ref, doc_id) in seen or _keeps_row(outage):
                continue
            # By id as well as path: the path may hold a fresh row under another id, and
            # that row is current.
            conn.execute(
                "DELETE FROM docs WHERE source = 'files' AND source_ref = ? AND id = ?",
                (ref, doc_id),
            )
            result["removed"] += 1
            result["removed_refs"].append(ref)
        conn.commit()
    finally:
        conn.close()
    return result


# --- verification -----------------------------------------------------------


def verify_index() -> dict:
    """Compare the index against the working tree. The evidence a self-heal acts on.

    Two independent checks, because they fail differently. `missing` and `extra`
    catch a doc the index never learned about or never dropped; `stale` catches a
    doc whose row exists but whose CONTENT no longer matches the file, which is
    the shape the watcher's observer produced when it missed a batched change.

    Content means the body and the unschema'd frontmatter, not the body alone.
    Comparing `body_hash` by itself would have called every row healthy on the
    day `frontmatter_extra` was added — every one of them sitting at the DEFAULT
    `{}` with the file's `tools` and `model` nowhere in the index, and this
    function reporting ok. A check that reads the same whether or not the thing
    it watches is broken is not a check. It is also what backfills the column:
    `kb sync` heals what this reports.

    A doc that fails validation is expected to be absent from the index, so it is
    reported separately and never counted as missing. A deferred doc (the roster
    could not be read) has no content to compare, so it is never missing, stale or
    extra. It is listed under `deferred`, and the first pass that reads the roster
    parses it again, so it is admitted or refreshed then. During an outage a refused doc
    is listed under `refused` instead, since it is refused on every pass. A refused doc
    keeps its row during an outage, and nothing compares that row, so `refused_kept` lists
    the refused paths that still have one. Those are not expected absent.

    A row is current only when its path and its id are both on disk. A row whose path
    holds a document with another id is extra, unless the roster cannot be read: then no
    row is extra (see `_keeps_row`).

    Returns {"ok": bool, "missing": [...], "extra": [...], "stale": [...],
             "invalid": [...], "deferred": [...], "deferred_reasons": {rel: reason},
             "refused": [...], "refused_reasons": {rel: error}, "refused_kept": [...],
             "disk": n, "indexed": n}.
    `deferred_reasons` holds the roster's failure for each deferred path. `refused_reasons`
    holds the refusal's text for each refused path, which is the text the full scan and
    `index_one` report for it.
    """
    # Decided once, the same way the full scan decides it, so the two agree on every row.
    roster = RosterRead()
    outage = roster.unreadable()
    on_disk: dict[tuple[str, str], tuple[str, str]] = {}
    invalid: list[tuple[str, str]] = []
    deferred: dict[str, str] = {}
    refused: dict[str, str] = {}
    for path in PROJECTS.rglob("*.md"):
        rel = str(path.relative_to(KNOWLEDGE))
        try:
            fm, err = parse_doc(path, roster)
        except RosterUnavailable as exc:
            deferred[rel] = str(exc)
            continue
        if err:
            if outage:
                refused[rel] = err
            else:
                invalid.append((rel, err))
            continue
        on_disk[(rel, fm["id"])] = (fm["body_hash"], extra_fields(fm))

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT source_ref, body_hash, frontmatter_extra, id "
            "FROM docs WHERE source = 'files'"
        ).fetchall()
    finally:
        conn.close()
    indexed = {(ref, doc_id): (h, x) for ref, h, x, doc_id in rows}
    indexed_refs = {ref for ref, _ in indexed}

    missing = sorted({rel for rel, doc_id in on_disk if (rel, doc_id) not in indexed})
    extra = sorted({ref for ref, doc_id in indexed
                    if (ref, doc_id) not in on_disk and not _keeps_row(outage)})
    stale = sorted(rel for (rel, doc_id), content in on_disk.items()
                   if (rel, doc_id) in indexed and indexed[(rel, doc_id)] != content)

    return {
        "ok": not (missing or extra or stale),
        "missing": missing,
        "extra": extra,
        "stale": stale,
        "invalid": invalid,
        "deferred": sorted(deferred),
        "deferred_reasons": deferred,
        "refused": sorted(refused),
        "refused_reasons": refused,
        "refused_kept": sorted(rel for rel in refused if rel in indexed_refs),
        "disk": len(on_disk),
        "indexed": len(indexed),
    }
