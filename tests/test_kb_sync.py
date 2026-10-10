"""The KB indexer and `agentctl kb sync` — commit->reindex as one owned step.

The failure these cover is the one that actually happened: a fast-forward lands
a batch of changed docs, the watcher's filesystem observer misses them, and the
index serves stale content until a human notices and kickstarts launchd. So the
integration tests drive real git repositories rather than mocking the plumbing —
the bug lived in the seam between git and the index, which a mock would hide.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from argparse import Namespace
from pathlib import Path

import pytest

import indexer
import knowledge

REPO = Path(__file__).resolve().parent.parent
SCHEMA = REPO / "lib" / "knowledge" / "schema.sql"

DOC = """---
id: platform/reference/{slug}
kind: reference
project: platform
title: {title}
status: current
audience: [architect]
last_reviewed: 2026-07-24
authors: [operator]
---

{body}
"""


def _write_doc(root: Path, slug: str, *, title: str = "A doc", body: str = "Body text.") -> Path:
    path = root / "projects" / "platform" / "reference" / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DOC.format(slug=slug, title=title, body=body))
    return path


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    """A knowledge corpus + its own index, with the indexer repointed at both."""
    root = tmp_path / "knowledge"
    (root / "projects" / "platform" / "reference").mkdir(parents=True)
    monkeypatch.setattr(indexer, "KNOWLEDGE", root)
    monkeypatch.setattr(indexer, "PROJECTS", root / "projects")
    monkeypatch.setattr(indexer, "DB", tmp_path / "index.db")
    monkeypatch.setattr(indexer, "SCHEMA", SCHEMA)
    indexer.ensure_db()
    return root


def _rows(db: Path) -> dict:
    conn = sqlite3.connect(str(db))
    try:
        return {r[0]: r[1] for r in
                conn.execute("SELECT source_ref, body_hash FROM docs").fetchall()}
    finally:
        conn.close()


# --- indexer -----------------------------------------------------------------


def test_index_paths_indexes_a_valid_doc(corpus, tmp_path):
    _write_doc(corpus, "alpha")
    res = indexer.index_paths(["projects/platform/reference/alpha.md"])
    assert res["indexed"] == 1
    assert res["invalid"] == []
    assert "projects/platform/reference/alpha.md" in _rows(tmp_path / "index.db")


def test_index_paths_removes_a_doc_deleted_on_disk(corpus, tmp_path):
    """A fast-forward that DELETES a doc must drop its row. Indexing only the
    files that still exist would leave the deleted doc readable forever."""
    path = _write_doc(corpus, "doomed")
    indexer.index_paths(["projects/platform/reference/doomed.md"])
    assert _rows(tmp_path / "index.db")

    path.unlink()
    res = indexer.index_paths(["projects/platform/reference/doomed.md"])
    assert res["removed"] == 1
    assert _rows(tmp_path / "index.db") == {}


def test_index_paths_skips_non_docs(corpus):
    """git reports every changed path; only docs under projects/ are indexable."""
    res = indexer.index_paths(["README.md", "scripts/validate.py",
                               "projects/platform/reference/notes.txt"])
    assert res["skipped"] == 3
    assert res["indexed"] == 0


def test_index_paths_reports_invalid_without_indexing_it(corpus, tmp_path):
    """An invalid doc must be ABSENT, not present-and-wrong: resolve_agent and
    kb_search both trust what they read back."""
    bad = corpus / "projects" / "platform" / "reference" / "bad.md"
    bad.write_text("---\nid: platform/reference/bad\nkind: nonsense-kind\n---\n\nBody.\n")
    res = indexer.index_paths(["projects/platform/reference/bad.md"])
    assert res["indexed"] == 0
    assert len(res["invalid"]) == 1
    assert _rows(tmp_path / "index.db") == {}


def test_verify_passes_on_a_consistent_index(corpus):
    _write_doc(corpus, "alpha")
    indexer.index_paths(["projects/platform/reference/alpha.md"])
    assert indexer.verify_index()["ok"] is True


def test_verify_detects_a_stale_row(corpus):
    """The exact shape the observer produced: the row exists, the body moved on."""
    _write_doc(corpus, "alpha", body="Original.")
    indexer.index_paths(["projects/platform/reference/alpha.md"])
    _write_doc(corpus, "alpha", body="Edited, and never reindexed.")

    v = indexer.verify_index()
    assert v["ok"] is False
    assert v["stale"] == ["projects/platform/reference/alpha.md"]
    assert v["missing"] == [] and v["extra"] == []


# --- docs_fts leak (INSERT OR REPLACE never fired the delete trigger) --------


def _fts_counts(db: Path) -> tuple[int, int]:
    conn = sqlite3.connect(str(db))
    try:
        docs_n = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
        fts_n = conn.execute("SELECT COUNT(*) FROM docs_fts_docsize").fetchone()[0]
        return docs_n, fts_n
    finally:
        conn.close()


def test_reindexing_the_same_doc_repeatedly_does_not_leak_fts_rows(corpus, tmp_path):
    """`INSERT OR REPLACE` deletes-and-reinserts a row without firing the AFTER
    DELETE trigger (recursive_triggers is off), and gives the new row a fresh
    rowid, so `docs_fts`'s external-content posting for the old rowid is never
    retired. Re-indexing the same doc 5 times must never grow docs_fts_docsize
    past what docs holds — on the live index it grew unbounded, 477,569 stale
    postings against 789 real rows."""
    _write_doc(corpus, "alpha")
    for _ in range(5):
        indexer.index_paths(["projects/platform/reference/alpha.md"])
    docs_n, fts_n = _fts_counts(tmp_path / "index.db")
    assert docs_n == 1
    assert fts_n == docs_n


def test_searching_a_term_only_in_the_old_body_returns_nothing_after_an_edit(corpus, tmp_path):
    """`docs_fts` itself, not just a query joined back to `docs`, must lose the
    old posting: a join happens to hide an orphaned rowid that no longer matches
    any doc, but once a freed rowid is reused by a later, unrelated document (as
    happened on the live index, whose rowids were reused after the index was
    emptied), that same orphaned posting resurfaces as a false match on whatever
    document now holds the rowid — 'bcrypt' matched 33 rows that never contained
    the word. Querying `docs_fts` directly catches the leak at its source."""
    _write_doc(corpus, "alpha", body="Body mentions xylophonezzz uniquely.")
    indexer.index_paths(["projects/platform/reference/alpha.md"])
    _write_doc(corpus, "alpha", body="Edited body; the old word is gone.")
    indexer.index_paths(["projects/platform/reference/alpha.md"])

    conn = sqlite3.connect(str(tmp_path / "index.db"))
    try:
        rows = conn.execute(
            "SELECT rowid FROM docs_fts WHERE docs_fts MATCH 'xylophonezzz'"
        ).fetchall()
    finally:
        conn.close()
    assert rows == []


def _plant_a_replace_leak(db: Path) -> None:
    """Reproduce, directly in SQL, the exact leak `INSERT OR REPLACE` left on the
    live index: REPLACE an existing row so it gets a new rowid without firing
    `docs_ad`, stranding its old FTS posting."""
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT id, source, source_ref, kind, project, title, status, topics, "
            "audience, applies_to, depends_on, last_reviewed, authors, body, "
            "body_hash, notion_view, updated_at, frontmatter_extra FROM docs LIMIT 1"
        ).fetchone()
        conn.execute(
            "INSERT OR REPLACE INTO docs (id, source, source_ref, kind, project, "
            "title, status, topics, audience, applies_to, depends_on, "
            "last_reviewed, authors, body, body_hash, notion_view, updated_at, "
            "frontmatter_extra) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            row,
        )
        conn.commit()
    finally:
        conn.close()


def test_repair_brings_a_leaked_db_back_to_matching_counts(corpus, tmp_path):
    """`ensure_db`'s repair: when docs_fts_docsize disagrees with docs, rebuild
    and vacuum, and end with the two matching again."""
    _write_doc(corpus, "alpha")
    indexer.index_paths(["projects/platform/reference/alpha.md"])
    db = tmp_path / "index.db"

    _plant_a_replace_leak(db)
    docs_n, fts_n = _fts_counts(db)
    assert fts_n > docs_n, "the plant must actually leak, or this test proves nothing"

    indexer.ensure_db()

    docs_n2, fts_n2 = _fts_counts(db)
    assert docs_n2 == docs_n
    assert fts_n2 == docs_n2


def test_two_ensure_db_calls_racing_a_leaked_db_repair_it_exactly_once(corpus, tmp_path):
    """The scenario this fixes: the watcher (launchd) and `agentctl kb sync` /
    `kb apply` all call `ensure_db()` independently and, by design,
    concurrently, and the live index IS in the leaked state right now — the
    first `ensure_db()` call anywhere after deploy runs the repair while a
    second can land at any moment. Two real OS processes hitting `ensure_db()`
    on the same leaked db at once must not both run rebuild+VACUUM against it.

    The assertion is a count of the "docs_fts leaked ... rebuilding" log line
    across both processes' stderr, not just "neither raised": the lock's
    re-check-after-acquire means that line can print from at most one of them
    no matter how the two interleave — if A finishes before B's own check
    even runs, B's unlocked pre-check already sees the counts equal and
    returns silently; if they overlap, B blocks on the lock and its RE-check
    sees what A already fixed. Either way the line prints once. Without the
    lock this fails at 2: both processes see the mismatch independently
    (neither waits on the other at all) and both print it, verified by
    reverting `_repair_fts_leak` to run unlocked and re-running this test."""
    _write_doc(corpus, "alpha")
    indexer.index_paths(["projects/platform/reference/alpha.md"])
    db = tmp_path / "index.db"
    _plant_a_replace_leak(db)
    docs_n, fts_n = _fts_counts(db)
    assert fts_n > docs_n, "the plant must actually leak, or this test proves nothing"

    env = {**os.environ, "KB_INDEX_DB": str(db),
           "PYTHONPATH": os.pathsep.join([str(REPO / "lib" / "knowledge"),
                                          str(REPO / "lib" / "agent-platform")])}
    script = "import indexer; indexer.ensure_db()"
    procs = [
        subprocess.Popen([sys.executable, "-c", script], env=env,
                          stderr=subprocess.PIPE, text=True)
        for _ in range(2)
    ]
    results = [p.communicate(timeout=30) for p in procs]

    for p, (_out, err) in zip(procs, results):
        assert p.returncode == 0, f"ensure_db() raised in a concurrent caller: {err}"

    rebuild_count = sum(err.count("docs_fts leaked") for _out, err in results)
    assert rebuild_count == 1, (
        f"expected exactly one of the two racing callers to run the rebuild, "
        f"got {rebuild_count}: {[err for _out, err in results]}"
    )

    docs_n2, fts_n2 = _fts_counts(db)
    assert docs_n2 == docs_n
    assert fts_n2 == docs_n2


def test_repair_lock_times_out_when_a_holder_hangs_without_releasing(
    corpus, tmp_path, monkeypatch, capsys
):
    """The bug this fixes: the repair lock used to be an unbounded
    `flock(LOCK_EX)`. A holder that DIES releases it for free (the race test
    above covers that), but a holder that HANGS without exiting — VACUUM stuck
    on a full disk or slow I/O — blocked every other `ensure_db()` caller
    forever, with nothing printed while they waited, and the live index sat in
    its leaked state the whole time. Reproduce the hang with a real second
    process that takes the lock and never releases it inside this test's
    window, and confirm the repair now gives up within the bound, says so
    loudly, leaves the mismatch unrepaired, and lets the caller's own write
    still go through."""
    _write_doc(corpus, "alpha")
    indexer.index_paths(["projects/platform/reference/alpha.md"])
    db = tmp_path / "index.db"
    _plant_a_replace_leak(db)
    docs_n, fts_n = _fts_counts(db)
    assert fts_n > docs_n, "the plant must actually leak, or this test proves nothing"

    monkeypatch.setattr(indexer, "REPAIR_LOCK_TIMEOUT_S", 0.5)
    monkeypatch.setattr(indexer, "REPAIR_LOCK_POLL_S", 0.05)

    lock_path = indexer._repair_lock_path()
    marker = tmp_path / "holder-ready"
    holder_script = (
        "import fcntl, time\n"
        f"fh = open({str(lock_path)!r}, 'w')\n"
        "fcntl.flock(fh.fileno(), fcntl.LOCK_EX)\n"
        f"open({str(marker)!r}, 'w').close()\n"
        "time.sleep(10)\n"
    )
    holder = subprocess.Popen([sys.executable, "-c", holder_script])
    try:
        deadline = time.monotonic() + 5
        while not marker.exists():
            assert time.monotonic() < deadline, "holder process never took the lock"
            time.sleep(0.05)

        conn = sqlite3.connect(str(db))
        try:
            started = time.monotonic()
            indexer._repair_fts_leak(conn)
            elapsed = time.monotonic() - started
        finally:
            conn.close()
    finally:
        holder.terminate()
        holder.wait(timeout=5)

    assert elapsed < 3, f"bounded at ~0.5s, took {elapsed}s — the lock wait is unbounded again"

    err = capsys.readouterr().err
    assert "TIMED OUT" in err
    assert str(lock_path) in err

    # Unrepaired: the leak this test planted is still there.
    docs_n2, fts_n2 = _fts_counts(db)
    assert (docs_n2, fts_n2) == (docs_n, fts_n)

    # But the caller's own write still proceeds — the timeout skips the
    # repair, not the write.
    _write_doc(corpus, "beta")
    res = indexer.index_paths(["projects/platform/reference/beta.md"])
    assert res["indexed"] == 1


def test_repair_lock_open_degrades_when_the_lock_path_is_a_symlink(
    corpus, tmp_path, capsys
):
    """The bug this fixes: `_open_repair_lock_file`'s `open()` (`O_NOFOLLOW`)
    raises `OSError` (`ELOOP`) when a symlink sits at the lock path, and
    nothing caught it — `ensure_db` let the exception reach the caller, and
    `bin/knowledge-watcher` calls `ensure_db` with no try/except under launchd
    `KeepAlive`, so this crash-looped the watcher every `ThrottleInterval`
    (10s). Confirm `ensure_db` now degrades instead: it returns without
    raising, prints one line naming the lock path, leaves the leak unrepaired,
    and a later write still succeeds."""
    _write_doc(corpus, "alpha")
    indexer.index_paths(["projects/platform/reference/alpha.md"])
    db = tmp_path / "index.db"
    _plant_a_replace_leak(db)
    docs_n, fts_n = _fts_counts(db)
    assert fts_n > docs_n, "the plant must actually leak, or this test proves nothing"

    lock_path = indexer._repair_lock_path()
    target = tmp_path / "not-the-lock-file"
    target.touch()
    lock_path.symlink_to(target)

    indexer.ensure_db()  # must not raise

    err = capsys.readouterr().err
    assert "repair lock open step FAILED" in err
    assert str(lock_path) in err

    # Unrepaired: the leak this test planted is still there.
    docs_n2, fts_n2 = _fts_counts(db)
    assert (docs_n2, fts_n2) == (docs_n, fts_n)

    # But the caller's own write still succeeds — the open failure skips the
    # repair, not the write.
    _write_doc(corpus, "beta")
    res = indexer.index_paths(["projects/platform/reference/beta.md"])
    assert res["indexed"] == 1


def test_repair_lock_open_degrades_when_the_lock_file_is_unwritable(
    corpus, tmp_path, capsys
):
    """Same degrade path as the symlink case above, for a different `OSError`
    at the same `open()` call: a lock file this process can't write to
    (`EPERM`/`EACCES`), standing in for the `ENOSPC` a full disk would raise
    there."""
    _write_doc(corpus, "alpha")
    indexer.index_paths(["projects/platform/reference/alpha.md"])
    db = tmp_path / "index.db"
    _plant_a_replace_leak(db)
    docs_n, fts_n = _fts_counts(db)
    assert fts_n > docs_n, "the plant must actually leak, or this test proves nothing"

    lock_path = indexer._repair_lock_path()
    lock_path.touch()
    os.chmod(lock_path, 0o444)

    indexer.ensure_db()  # must not raise

    err = capsys.readouterr().err
    assert "repair lock open step FAILED" in err
    assert str(lock_path) in err

    docs_n2, fts_n2 = _fts_counts(db)
    assert (docs_n2, fts_n2) == (docs_n, fts_n)

    _write_doc(corpus, "beta")
    res = indexer.index_paths(["projects/platform/reference/beta.md"])
    assert res["indexed"] == 1


class _FakeWrongMatch:
    """Wraps a real connection and returns a WRONG id for the `docs_fts MATCH`
    query only. `docs_fts` is external-content (`content='docs'`): the column
    values a query returns are read live from `docs` by rowid, not stored in
    the FTS index itself, so hand-planting a stale posting via INSERT does not
    reproduce a resolve-to-the-wrong-row result — SQLite still resolves it
    back to whatever row currently owns that rowid. Faking the query result is
    what makes `_verify_fts_postcondition`'s comparison testable without
    needing SQLite itself to actually mis-resolve a rowid."""

    def __init__(self, real: sqlite3.Connection):
        self._real = real

    def execute(self, sql, *args):
        if "docs_fts MATCH" in sql:
            class _FakeCursor:
                def fetchall(self_inner):
                    return [("platform/reference/somewhere-else",)]
            return _FakeCursor()
        return self._real.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._real, name)


class _FakeNullBody:
    """Wraps a real connection and returns a NULL `body` for the
    postcondition's own row read (`SELECT id, body FROM docs LIMIT 1`),
    reproducing what a corrupted page can read back even though the schema
    declares `body` `NOT NULL` — that constraint is enforced only on write,
    and the corruption this whole repair exists for is exactly a page that
    reads back something the write path never would have allowed."""

    def __init__(self, real: sqlite3.Connection):
        self._real = real

    def execute(self, sql, *args):
        if sql.strip() == "SELECT id, body FROM docs LIMIT 1":
            class _FakeCursor:
                def fetchone(self_inner):
                    return ("platform/reference/alpha", None)
            return _FakeCursor()
        return self._real.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_repair_postcondition_catches_a_search_that_resolves_to_the_wrong_row(
    corpus, tmp_path, capsys
):
    """A count match is not proof the postings point at the right rows — the
    exact shape a future SQLite whose VACUUM renumbers rowids would leave:
    counts still balance, but a word from a doc's own body no longer resolves
    back to that doc's own id. Confirm the postcondition — not the count check
    `_repair_fts_leak` already does — is what catches it."""
    _write_doc(corpus, "alpha", body="Body mentions xylophonezzz uniquely.")
    indexer.index_paths(["projects/platform/reference/alpha.md"])
    db = tmp_path / "index.db"

    real = sqlite3.connect(str(db))
    try:
        docs_n = real.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
        indexer._verify_fts_postcondition(_FakeWrongMatch(real), docs_n)
    finally:
        real.close()

    err = capsys.readouterr().err
    assert "postcondition FAILED" in err
    assert "platform/reference/alpha" in err


def test_repair_postcondition_logs_when_no_word_is_long_enough_to_check(
    corpus, tmp_path, capsys
):
    """The postcondition used to no-op silently when the chosen row had no word
    of 5+ letters, which reads identically in the logs to a postcondition that
    ran and passed. Confirm it now says the check was skipped and why."""
    _write_doc(corpus, "alpha", body="a be it or on.")
    indexer.index_paths(["projects/platform/reference/alpha.md"])
    db = tmp_path / "index.db"

    conn = sqlite3.connect(str(db))
    try:
        docs_n = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
        indexer._verify_fts_postcondition(conn, docs_n)
    finally:
        conn.close()

    err = capsys.readouterr().err
    assert "postcondition skipped" in err
    assert "platform/reference/alpha" in err


class _FakeMismatchAfterVacuum:
    """Wraps a real connection and lies about the post-VACUUM count exactly
    once, so `_repair_fts_leak`'s mismatch-after-repair branch can be tested
    without needing SQLite itself to ever actually fail to reconcile."""

    def __init__(self, real: sqlite3.Connection):
        self._real = real
        self._vacuumed = False

    def execute(self, sql, *args):
        if sql.strip() == "VACUUM":
            self._vacuumed = True
        cur = self._real.execute(sql, *args)
        if self._vacuumed and sql.strip() == "SELECT COUNT(*) FROM docs_fts_docsize":
            class _FakeCursor:
                def fetchone(self_inner):
                    return (9999,)
            return _FakeCursor()
        return cur

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_repair_logs_a_loud_mismatch_when_the_rebuild_does_not_reconcile(
    corpus, tmp_path, capsys
):
    """`_repair_fts_leak` used to log `matching docs=N` with no comparison — a
    rebuild that silently failed to reconcile would report success. Confirm
    the after-count is actually compared to `docs`'s, using a fake connection
    to force the mismatch since a real rebuild+VACUUM always reconciles."""
    _write_doc(corpus, "alpha")
    indexer.index_paths(["projects/platform/reference/alpha.md"])
    db = tmp_path / "index.db"
    _plant_a_replace_leak(db)

    real = sqlite3.connect(str(db))
    try:
        indexer._repair_fts_leak(_FakeMismatchAfterVacuum(real))
    finally:
        real.close()

    err = capsys.readouterr().err
    assert "rebuild FAILED to reconcile" in err


class _FailAt:
    """Wraps a real sqlite3 connection and raises exactly once, at one named
    point in `_repair_fts_leak`'s sequence — the one fake behind the
    parametrized degrade test below, replacing the one-fake-per-step classes
    round 4 needed (`_FakeRaisesOnCount`, `_FakeRaisesOnRebuild`,
    `_FakeRaisesOnVacuum`). `fail_rollback=True` additionally makes
    `.rollback()` raise, standing in for the round-5 finding: a rollback
    against a corrupted database can fail in its own right, same as any
    other statement."""

    _COUNT_DOCS = "SELECT COUNT(*) FROM docs"
    _COUNT_FTS = "SELECT COUNT(*) FROM docs_fts_docsize"
    _REBUILD = "INSERT INTO docs_fts(docs_fts) VALUES('rebuild')"
    _VACUUM = "VACUUM"

    def __init__(self, real: sqlite3.Connection, step: str, fail_rollback: bool = False):
        self._real = real
        self._step = step
        self._fail_rollback = fail_rollback
        self._docs_count_calls = 0
        self._fts_count_calls = 0

    def execute(self, sql, *args):
        s = sql.strip()
        if s == self._COUNT_DOCS:
            self._docs_count_calls += 1
            # 1st call is the unlocked pre-check, 2nd is the re-check under
            # the lock — `_fts_counts` is called at both points with
            # identical SQL, so the call count is what tells them apart.
            if self._step == "pre-check" and self._docs_count_calls == 1:
                raise sqlite3.OperationalError("boom: pre-check")
            if self._step == "re-check" and self._docs_count_calls == 2:
                raise sqlite3.OperationalError("boom: re-check")
        elif s == self._COUNT_FTS:
            self._fts_count_calls += 1
            # 3rd call to this exact SQL is the explicit postcondition count
            # (the 1st and 2nd are the pre-check and re-check above).
            if self._step == "postcondition-count" and self._fts_count_calls == 3:
                raise sqlite3.OperationalError("boom: postcondition-count")
        elif s == self._REBUILD and self._step == "rebuild":
            raise sqlite3.OperationalError("boom: rebuild")
        elif s == self._VACUUM and self._step == "vacuum":
            raise sqlite3.OperationalError("boom: vacuum")
        return self._real.execute(sql, *args)

    def rollback(self):
        if self._fail_rollback:
            raise sqlite3.OperationalError("boom: rollback itself")
        return self._real.rollback()

    def __getattr__(self, name):
        return getattr(self._real, name)


def _inject_repair_failure(step: str, monkeypatch, real: sqlite3.Connection):
    """Arrange for exactly one call inside `_repair_fts_leak` to raise, at
    `step`, and return the connection object the test should pass in. The
    non-SQL steps (mkdir, lock open, flock, postcondition verify) patch the
    indexer function that performs them, one-shot — firing once and then
    delegating to the original — so a second, unpatched call (this test's
    lock-release check) exercises the real code path rather than raising
    again forever."""
    if step == "mkdir":
        lock_dir = indexer._repair_lock_path().parent
        orig_mkdir = Path.mkdir
        state = {"fired": False}

        def fake_mkdir(self, *a, **kw):
            if not state["fired"] and self == lock_dir:
                state["fired"] = True
                raise OSError("boom: mkdir")
            return orig_mkdir(self, *a, **kw)

        monkeypatch.setattr(Path, "mkdir", fake_mkdir)
        return real
    if step == "lock-open":
        orig_open = indexer._open_repair_lock_file
        state = {"fired": False}

        def fake_open(lock_path):
            if not state["fired"]:
                state["fired"] = True
                raise OSError("boom: lock-open")
            return orig_open(lock_path)

        monkeypatch.setattr(indexer, "_open_repair_lock_file", fake_open)
        return real
    if step == "flock":
        orig_acquire = indexer._acquire_repair_lock
        state = {"fired": False}

        def fake_acquire(fh):
            if not state["fired"]:
                state["fired"] = True
                raise OSError("boom: flock")
            return orig_acquire(fh)

        monkeypatch.setattr(indexer, "_acquire_repair_lock", fake_acquire)
        return real
    if step == "postcondition-verify":
        orig_verify = indexer._verify_fts_postcondition
        state = {"fired": False}

        def fake_verify(conn_, docs_n):
            if not state["fired"]:
                state["fired"] = True
                raise sqlite3.OperationalError("boom: postcondition-verify")
            return orig_verify(conn_, docs_n)

        monkeypatch.setattr(indexer, "_verify_fts_postcondition", fake_verify)
        return real
    if step == "rollback":
        # Fails at the rebuild step, same as "rebuild", but also makes the
        # handler's own rollback attempt raise.
        return _FailAt(real, "rebuild", fail_rollback=True)
    return _FailAt(real, step)


_REPAIR_STEPS = [
    "pre-check",
    "mkdir",
    "lock-open",
    "flock",
    "re-check",
    "rebuild",
    "vacuum",
    "postcondition-count",
    "postcondition-verify",
    "rollback",
]

# The exact `step` local `_repair_fts_leak` sets before each operation —
# what its single handler's "{step} step FAILED" line names.
_REPAIR_STEP_LABELS = {
    "pre-check": "docs_fts count pre-check",
    "mkdir": "repair lock directory create",
    "lock-open": "repair lock open",
    "flock": "repair lock acquire",
    "re-check": "docs_fts count re-check",
    "rebuild": "docs_fts rebuild",
    "vacuum": "docs_fts VACUUM",
    "postcondition-count": "docs_fts postcondition count",
    "postcondition-verify": "docs_fts postcondition verify",
    "rollback": "docs_fts rebuild",  # rollback fails while handling the rebuild raise
}


@pytest.mark.parametrize("step", _REPAIR_STEPS)
def test_repair_degrades_at_every_step(step, corpus, tmp_path, monkeypatch, capsys):
    """The finding this fixes (security-reviewer, PR #559 round 5): round 4
    wrapped seven steps in their own `try/except` and missed an eighth — the
    `conn.rollback()` calls the rebuild and VACUUM handlers made on their own
    way out were themselves unguarded, and on a corrupted database (today's
    live incident) ROLLBACK can raise `sqlite3.OperationalError` same as any
    other statement. `_repair_fts_leak` now runs its whole body under ONE
    `try/except Exception`, so every step — including a
    rollback that itself fails — degrades the same way: no raise, one line
    naming the step, the lock released, and a later write still lands.
    Parametrized over every step the function takes, plus the
    rollback-raises-too case, so a raise anywhere in the sequence is covered
    by construction rather than by whichever step someone thought to guard
    next."""
    _write_doc(corpus, "alpha")
    indexer.index_paths(["projects/platform/reference/alpha.md"])
    db = tmp_path / "index.db"
    _plant_a_replace_leak(db)
    docs_n, fts_n = _fts_counts(db)
    assert fts_n > docs_n, "the plant must actually leak, or this test proves nothing"

    real = sqlite3.connect(str(db))
    conn = _inject_repair_failure(step, monkeypatch, real)
    try:
        indexer._repair_fts_leak(conn)  # must not raise
    finally:
        real.close()

    err = capsys.readouterr().err
    assert f"{_REPAIR_STEP_LABELS[step]} step FAILED" in err, err
    assert "boom" in err, err

    # The lock is released even on failure: a second `ensure_db()` call must
    # neither hang waiting on it nor raise, and — the one-shot fakes above
    # having fired already — must actually finish the repair this time.
    started = time.monotonic()
    indexer.ensure_db()  # must not raise
    elapsed = time.monotonic() - started
    assert elapsed < 5, f"lock apparently not released — took {elapsed}s"
    docs_n2, fts_n2 = _fts_counts(db)
    assert docs_n2 == fts_n2, "a second call must be able to take the lock and repair"

    # And a caller's own write still lands — the repair failure skips the
    # repair, not the write.
    _write_doc(corpus, "beta")
    res = indexer.index_paths(["projects/platform/reference/beta.md"])
    assert res["indexed"] == 1


def test_repair_degrades_when_a_corrupted_row_reads_back_a_null_body(
    corpus, tmp_path, monkeypatch, capsys
):
    """Round 6's guard was `except (OSError, sqlite3.Error)`, which does not
    catch `AttributeError` — and `_verify_fts_postcondition` calls
    `body.split()` on a row it reads straight out of `docs`. The `body`
    column is `NOT NULL`, but only on write; a corrupted page (what this
    whole repair exists for, and what the live index actually had) can read
    back `NULL` regardless, and `None.split()` raises `AttributeError`. That
    must degrade the same way as every other step, not escape past the
    guard."""
    _write_doc(corpus, "alpha")
    indexer.index_paths(["projects/platform/reference/alpha.md"])
    db = tmp_path / "index.db"
    _plant_a_replace_leak(db)

    real = sqlite3.connect(str(db))
    try:
        indexer._repair_fts_leak(_FakeNullBody(real))  # must not raise
    finally:
        real.close()

    err = capsys.readouterr().err
    assert "docs_fts postcondition verify step FAILED" in err, err

    # The lock is released even on this failure, same as every other step.
    started = time.monotonic()
    indexer.ensure_db()  # must not raise
    elapsed = time.monotonic() - started
    assert elapsed < 5, f"lock apparently not released — took {elapsed}s"
    docs_n2, fts_n2 = _fts_counts(db)
    assert docs_n2 == fts_n2, "a second call must be able to take the lock and repair"


def test_repair_degrades_when_writing_the_failure_line_itself_raises(
    corpus, tmp_path, monkeypatch
):
    """Round 6 wrapped the whole repair in one guard but left the handler's
    own `print(...)` unguarded: on a full disk (`ENOSPC`), writing the
    failure line to stderr itself raises `OSError` from inside the handler
    and escapes — the same bug as the round-5 rollback fix, now in the
    logging path. Force the rebuild step to fail (`_FailAt`) and every
    stderr write to fail too, and confirm `_repair_fts_leak` still swallows
    the compound failure, releases the lock, and leaves the index repairable
    by a later call."""
    _write_doc(corpus, "alpha")
    indexer.index_paths(["projects/platform/reference/alpha.md"])
    db = tmp_path / "index.db"
    _plant_a_replace_leak(db)

    orig_write = sys.stderr.write

    def failing_write(s):
        raise OSError("No space left on device")

    real = sqlite3.connect(str(db))
    conn = _inject_repair_failure("rebuild", monkeypatch, real)
    try:
        # A plain assignment, restored in `finally` — NOT `monkeypatch`,
        # whose `corpus` fixture already holds patches (`indexer.DB` among
        # them) that a mid-test `monkeypatch.undo()` would revert too,
        # pointing the second `ensure_db()` call below at the wrong database.
        sys.stderr.write = failing_write
        indexer._repair_fts_leak(conn)  # must not raise
    finally:
        sys.stderr.write = orig_write
        real.close()

    started = time.monotonic()
    indexer.ensure_db()  # must not raise; the lock must have been released
    elapsed = time.monotonic() - started
    assert elapsed < 5, f"lock apparently not released — took {elapsed}s"
    docs_n, fts_n = _fts_counts(db)
    assert docs_n == fts_n, "a second call must be able to take the lock and repair"


# --- WAL: readers must never see a hot rollback journal ----------------------


def test_wal_reader_can_read_after_a_writer_crashes_mid_transaction(corpus, tmp_path):
    """The crash this fixes: `resolve_agent` opened `mode=ro` and hit
    `sqlite3.OperationalError: attempt to write a readonly database`. That error
    is specific to a HOT journal — one left by a writer that is gone (crashed or
    killed) but whose transaction was never rolled back, which is different from
    a writer merely still in flight (a live writer holds a lock a reader can read
    around without touching the journal at all). Reproducing it needs a real
    writer PROCESS that dies mid-transaction, so its lock is released but its
    journal is left hot, and a table with enough pages that the write actually
    reaches the OS file instead of sitting in SQLite's page cache. Under
    journal_mode=DELETE, a `mode=ro` reader finding that hot journal must roll it
    back into the main file to read consistently, and cannot; under WAL the
    reader reads the last-committed snapshot out of the WAL/main-file pair and
    never touches the main file, so it must succeed here."""
    _write_doc(corpus, "alpha")
    indexer.index_paths(["projects/platform/reference/alpha.md"])
    db = tmp_path / "index.db"

    conn = sqlite3.connect(str(db))
    conn.executemany(
        "INSERT INTO docs (id, source, source_ref, kind, project, title, status, "
        "body, body_hash, updated_at) VALUES "
        "(?, 'files', ?, 'reference', 'platform', 'Bulk', 'current', ?, 'x', "
        "'2026-01-01T00:00:00Z')",
        [(f"platform/reference/bulk{i}", f"projects/platform/reference/bulk{i}.md",
          "z" * 4000) for i in range(300)],
    )
    conn.commit()
    conn.close()

    crash = (
        "import sqlite3, os\n"
        f"conn = sqlite3.connect({str(db)!r})\n"
        "conn.execute('PRAGMA cache_size=1')\n"
        "conn.execute('BEGIN')\n"
        "conn.execute('UPDATE docs SET body = ?', ('y' * 4000,))\n"
        "os._exit(1)\n"
    )
    subprocess.run([sys.executable, "-c", crash], check=False)

    reader = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = reader.execute("SELECT COUNT(*) FROM docs").fetchall()
    finally:
        reader.close()
    assert rows == [(301,)]


def test_a_reader_can_open_when_no_writer_is_connected_and_wal_files_are_absent(
    corpus, tmp_path
):
    """A regression guard, not a proof of the WAL fix: this steady-state case (no
    writer connected, sidecars already checkpointed away) opens and reads fine
    under `journal_mode=DELETE` too — a `mode=ro` connection only needs to roll
    a journal forward when one exists, and there is none here. It passes
    identically before and after this PR; kept because it documents the
    no-writer baseline the PR body's "case (b)" measurement describes, so a
    future change to that baseline shows up here even though this test cannot
    tell WAL and DELETE apart."""
    _write_doc(corpus, "alpha")
    indexer.index_paths(["projects/platform/reference/alpha.md"])
    db = tmp_path / "index.db"

    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(db) + suffix)
        if sidecar.exists():
            sidecar.unlink()
    assert not Path(str(db) + "-wal").exists()
    assert not Path(str(db) + "-shm").exists()

    reader = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = reader.execute("SELECT id FROM docs").fetchall()
    finally:
        reader.close()
    assert rows == [("platform/reference/alpha",)]


# --- frontmatter the columns do not name -------------------------------------


AGENT_DOC = """---
id: platform/agents/scout
kind: agent
project: platform
title: Scout
status: current
audience: [scout]
last_reviewed: 2026-09-08
authors: [operator]
trigger: Use to search the corpus read-only.
tools: [Read, Grep, Glob]
model: sonnet
model_floor: haiku
---

Scout searches and reports.
"""

AGENT_REL = "projects/platform/agents/scout.md"


def _write_agent(root: Path, text: str = AGENT_DOC) -> Path:
    path = root / "projects" / "platform" / "agents" / "scout.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _extra(db: Path, source_ref: str) -> dict:
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT frontmatter_extra FROM docs WHERE source_ref = ?",
            (source_ref,),
        ).fetchone()
    finally:
        conn.close()
    return json.loads(row[0])


def test_indexing_carries_the_fields_no_column_names(corpus, tmp_path):
    """`tools`, `model` and `trigger` were dropped at index time for as long as
    the table existed, so no read surface over the index could serve a whole
    agent definition — `knowledge-ctx` had to reopen the source markdown off
    local disk to recover them, which is the seam the content move exists to
    close. `model_floor` is here too: it is the field the three-column fix would
    have missed, and the reason this is one JSON column."""
    _write_agent(corpus)
    assert indexer.index_paths([AGENT_REL])["indexed"] == 1

    extra = _extra(tmp_path / "index.db", AGENT_REL)
    assert extra["tools"] == ["Read", "Grep", "Glob"]
    assert extra["model"] == "sonnet"
    assert extra["trigger"] == "Use to search the corpus read-only."
    assert extra["model_floor"] == "haiku"


def test_a_field_with_its_own_column_is_not_duplicated_into_the_extra(corpus, tmp_path):
    """Every column'd field, and every field `parse_doc` injects, stays out —
    `updated_at` above all. It is a fresh timestamp on every parse, so storing it
    here would make each verify pass report every row stale and each self-heal
    rewrite the whole corpus, forever."""
    _write_agent(corpus)
    indexer.index_paths([AGENT_REL])

    extra = _extra(tmp_path / "index.db", AGENT_REL)
    assert set(extra) == {"trigger", "tools", "model", "model_floor"}


def test_a_date_valued_field_serializes_rather_than_raising(corpus, tmp_path):
    """PyYAML decodes a bare `decided: 2026-08-11` to a `datetime.date`, which
    json cannot encode. Seven decision records in the corpus carry one, so
    without the coercion indexing them would raise instead of dropping a field."""
    _write_agent(corpus, AGENT_DOC.replace("model_floor: haiku",
                                           "decided: 2026-08-11"))
    assert indexer.index_paths([AGENT_REL])["indexed"] == 1
    assert _extra(tmp_path / "index.db", AGENT_REL)["decided"] == "2026-08-11"


def test_extra_fields_is_stable_across_two_parses(corpus):
    """Verification compares a stored serialization against a freshly computed
    one, so an unstable key order would read as a stale row on every pass."""
    path = _write_agent(corpus)
    first, _ = indexer.parse_doc(path)
    second, _ = indexer.parse_doc(path)
    assert indexer.extra_fields(first) == indexer.extra_fields(second)


def test_verify_reports_a_row_whose_extra_fields_drifted(corpus):
    """The body is untouched and the frontmatter moved, which is exactly the shape
    a database migrated to this column is in: every row's body matches its file and
    every row's `frontmatter_extra` is the empty default. Comparing `body_hash`
    alone called that healthy."""
    _write_agent(corpus)
    indexer.index_paths([AGENT_REL])
    assert indexer.verify_index()["ok"] is True

    _write_agent(corpus, AGENT_DOC.replace("model: sonnet", "model: opus"))
    v = indexer.verify_index()
    assert v["ok"] is False
    assert v["stale"] == [AGENT_REL]
    assert v["missing"] == [] and v["extra"] == []


def test_a_full_scan_backfills_an_unmigrated_row(corpus, tmp_path):
    """The backfill path, end to end: a row at the migration default is stale, and
    the same reconcile `kb sync` already self-heals with is what refills it. No
    separate backfill step exists, and this is why none is needed."""
    _write_agent(corpus)
    indexer.index_paths([AGENT_REL])

    db = tmp_path / "index.db"
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE docs SET frontmatter_extra = '{}'")
    conn.commit()
    conn.close()
    assert indexer.verify_index()["stale"] == [AGENT_REL]

    indexer.full_scan_and_reconcile()
    assert indexer.verify_index()["ok"] is True
    assert _extra(db, AGENT_REL)["model"] == "sonnet"


def test_ensure_db_adds_the_column_to_a_database_created_before_it(tmp_path, monkeypatch):
    """`schema.sql` is all `CREATE ... IF NOT EXISTS`, so a new column in it reaches
    a new database and never an existing one. The live index is an existing one."""
    db = tmp_path / "old-index.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE docs (
            id TEXT PRIMARY KEY, source TEXT NOT NULL, source_ref TEXT NOT NULL,
            kind TEXT NOT NULL, project TEXT NOT NULL, title TEXT NOT NULL,
            status TEXT NOT NULL, topics TEXT NOT NULL DEFAULT '[]',
            audience TEXT NOT NULL DEFAULT '[]', applies_to TEXT NOT NULL DEFAULT '[]',
            depends_on TEXT NOT NULL DEFAULT '[]', last_reviewed TEXT,
            authors TEXT NOT NULL DEFAULT '[]', body TEXT NOT NULL,
            body_hash TEXT NOT NULL, notion_view INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );
    """)
    conn.execute(
        "INSERT INTO docs (id, source, source_ref, kind, project, title, status, "
        "body, body_hash, updated_at) VALUES "
        "('platform/agents/scout', 'files', ?, 'agent', 'platform', 'Scout', "
        "'current', 'Body.', 'abc', '2026-09-08T00:00:00Z')",
        (AGENT_REL,),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(indexer, "DB", db)
    monkeypatch.setattr(indexer, "SCHEMA", SCHEMA)
    indexer.ensure_db()

    conn = sqlite3.connect(str(db))
    try:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(docs)")}
        assert "frontmatter_extra" in columns
        # The pre-existing row survives, at the default rather than dropped.
        assert conn.execute(
            "SELECT frontmatter_extra FROM docs WHERE source_ref = ?", (AGENT_REL,)
        ).fetchone()[0] == "{}"
        # The expression index indexes the new column, so it can only have been
        # created after the migration ran. Ordering the two the other way round
        # fails here with "no such column".
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND name = 'idx_docs_model'"
        ).fetchone() is not None
    finally:
        conn.close()


def test_ensure_db_is_idempotent_on_an_already_migrated_database(corpus, tmp_path):
    """The watcher and every `kb sync` call it on startup."""
    _write_agent(corpus)
    indexer.index_paths([AGENT_REL])
    indexer.ensure_db()
    indexer.ensure_db()
    assert _extra(tmp_path / "index.db", AGENT_REL)["model"] == "sonnet"


def test_a_model_pin_is_queryable_without_reading_every_body(corpus, tmp_path):
    """The affordance the per-field columns were wanted for. `json_extract` over
    this column answers it, and `idx_docs_model` is the index that serves it."""
    _write_agent(corpus)
    indexer.index_paths([AGENT_REL])

    conn = sqlite3.connect(str(tmp_path / "index.db"))
    try:
        rows = conn.execute(
            "SELECT id FROM docs WHERE json_extract(frontmatter_extra, '$.model') = ?",
            ("sonnet",),
        ).fetchall()
    finally:
        conn.close()
    assert [r[0] for r in rows] == ["platform/agents/scout"]


def test_verify_detects_missing_and_extra(corpus):
    _write_doc(corpus, "indexed-ok")
    indexer.index_paths(["projects/platform/reference/indexed-ok.md"])
    _write_doc(corpus, "never-indexed")          # on disk, not in the index
    (corpus / "projects" / "platform" / "reference" / "indexed-ok.md").unlink()  # reverse

    v = indexer.verify_index()
    assert v["ok"] is False
    assert v["missing"] == ["projects/platform/reference/never-indexed.md"]
    assert v["extra"] == ["projects/platform/reference/indexed-ok.md"]


def test_verify_does_not_count_an_invalid_doc_as_missing(corpus):
    """An invalid doc is SUPPOSED to be absent. Counting it as missing would make
    verification cry wolf on every corpus that has one — and the real corpus does."""
    bad = corpus / "projects" / "platform" / "reference" / "bad.md"
    bad.write_text("---\nid: platform/reference/bad\nkind: nonsense-kind\n---\n\nBody.\n")
    v = indexer.verify_index()
    assert v["ok"] is True
    assert len(v["invalid"]) == 1


def test_index_one_drops_the_row_of_a_now_invalid_doc(corpus, tmp_path):
    """`index_one` is the single-doc primitive the watcher's event handler and the
    targeted pass BOTH go through. It exists because the delete-on-invalid rule
    was originally written separately into each, and the copy nobody was looking
    at is the one that got it wrong."""
    _write_doc(corpus, "alpha")
    assert indexer.index_one(corpus / "projects/platform/reference/alpha.md") == ("upserted", None)
    assert _rows(tmp_path / "index.db")

    bad = corpus / "projects" / "platform" / "reference" / "alpha.md"
    bad.write_text("---\nid: platform/reference/alpha\nkind: nonsense-kind\n---\n\nBody.\n")
    action, err = indexer.index_one(bad)

    assert action == "removed" and err is not None
    assert _rows(tmp_path / "index.db") == {}


def test_index_one_skips_paths_outside_the_corpus(corpus, tmp_path):
    """The watcher observes the whole repo, so it hands over paths outside
    projects/ and files that are not docs."""
    assert indexer.index_one(corpus / "README.md") == ("skipped", None)
    assert indexer.index_one(corpus / "projects/platform/reference/x.txt") == ("skipped", None)
    assert indexer.index_one(Path("/somewhere/else/doc.md")) == ("skipped", None)


def test_a_doc_that_becomes_invalid_loses_its_row(corpus, tmp_path):
    """The valid->invalid case, which is where a stale row used to become permanent.

    Keeping the last-good row would serve content the validator now rejects, and
    `verify_index` would report it as `extra` on every future run — a failure the
    self-heal could not clear, because the reconcile protected the same row.
    """
    _write_doc(corpus, "alpha", body="Valid at first.")
    indexer.index_paths(["projects/platform/reference/alpha.md"])
    assert _rows(tmp_path / "index.db")

    bad = corpus / "projects" / "platform" / "reference" / "alpha.md"
    bad.write_text("---\nid: platform/reference/alpha\nkind: nonsense-kind\n---\n\nBody.\n")
    res = indexer.index_paths(["projects/platform/reference/alpha.md"])

    assert len(res["invalid"]) == 1
    assert _rows(tmp_path / "index.db") == {}
    assert indexer.verify_index()["ok"] is True


def test_full_scan_drops_the_row_of_a_now_invalid_doc(corpus, tmp_path):
    """The same hole on the reconcile path — the one the self-heal runs.

    If the full scan cannot drop this row, a verification failure is unrecoverable:
    every `kb sync` exits 2 forever with no way out short of editing the database.
    """
    _write_doc(corpus, "alpha")
    indexer.full_scan_and_reconcile()
    assert _rows(tmp_path / "index.db")

    bad = corpus / "projects" / "platform" / "reference" / "alpha.md"
    bad.write_text("---\nid: platform/reference/alpha\nkind: nonsense-kind\n---\n\nBody.\n")
    res = indexer.full_scan_and_reconcile()

    assert res["removed"] == 1
    assert res["removed_refs"] == ["projects/platform/reference/alpha.md"]
    assert _rows(tmp_path / "index.db") == {}
    assert indexer.verify_index()["ok"] is True


def test_kb_sync_self_heal_terminates_on_a_corrupted_doc(kb_repos, agentctl):
    """End to end: a commit that breaks a previously-valid doc must not strand the
    sync in a permanent exit-2 loop."""
    agentctl.cmd_kb_sync(_args(full=True))

    bad = kb_repos.seed / "projects" / "platform" / "reference" / "seeded.md"
    bad.write_text("---\nid: platform/reference/seeded\nkind: nonsense-kind\n---\n\nBody.\n")
    _git(kb_repos.seed, "add", "-A")
    _git(kb_repos.seed, "commit", "--quiet", "-m", "break seeded")
    _git(kb_repos.seed, "push", "--quiet", "origin", "main")

    assert agentctl.cmd_kb_sync(_args()) == 0
    assert indexer.verify_index()["ok"] is True
    # and it stays healthy on the next cycle rather than oscillating
    assert agentctl.cmd_kb_sync(_args(full=True)) == 0


def test_full_scan_reconciles_out_a_vanished_doc(corpus, tmp_path):
    path = _write_doc(corpus, "gone")
    indexer.full_scan_and_reconcile()
    assert _rows(tmp_path / "index.db")

    path.unlink()
    res = indexer.full_scan_and_reconcile()
    assert res["removed"] == 1
    assert _rows(tmp_path / "index.db") == {}


# --- agentctl kb sync ---------------------------------------------------------


def _git(repo: Path, *a):
    return subprocess.run(["git", "-C", str(repo), *a],
                          capture_output=True, text=True, check=False)


@pytest.fixture
def kb_repos(tmp_path, monkeypatch, agentctl, library_identity):
    """An origin repo and a clone standing in for the shared checkout."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "--quiet", "--bare", "--initial-branch=main")

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "--quiet", "--initial-branch=main")
    _git(seed, "config", "user.email", "t@example.com")
    _git(seed, "config", "user.name", "T")
    _write_doc(seed, "seeded")
    _git(seed, "add", "-A")
    _git(seed, "commit", "--quiet", "-m", "seed")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "--quiet", "-u", "origin", "main")

    checkout = tmp_path / "checkout"
    subprocess.run(["git", "clone", "--quiet", str(origin), str(checkout)],
                   capture_output=True, text=True, check=True)
    _git(checkout, "config", "user.email", "t@example.com")
    _git(checkout, "config", "user.name", "T")

    monkeypatch.setattr(agentctl, "KNOWLEDGE_ROOT", checkout)
    monkeypatch.setattr(indexer, "KNOWLEDGE", checkout)
    monkeypatch.setattr(indexer, "PROJECTS", checkout / "projects")
    monkeypatch.setattr(indexer, "DB", tmp_path / "index.db")
    monkeypatch.setattr(indexer, "SCHEMA", SCHEMA)
    monkeypatch.setattr(agentctl, "_kb_indexer", lambda: indexer)
    return Namespace(origin=origin, seed=seed, checkout=checkout,
                     db=tmp_path / "index.db")


def _args(**kw):
    base = {"full": False, "verify_only": False, "no_heal": False, "dry_run": False,
            "quiet": False}
    base.update(kw)
    return Namespace(**base)


def test_kb_sync_bootstraps_before_touching_the_index(kb_repos, agentctl, monkeypatch):
    """`cmd_kb_sync` calls `venv_bootstrap.ensure` itself, as its first action,
    rather than relying on `_kb_indexer`'s import of `yaml` to be the first
    thing anything does (round-2 review, PR 695: a helper's own bootstrap runs
    wherever ITS caller happens to reach it, which is a place, not a
    guarantee). Proved by recording the order `venv_bootstrap.ensure` and
    `indexer.ensure_db` are actually called in, through the real
    `cmd_kb_sync`. `kb_repos` replaces `agentctl._kb_indexer` outright (to
    point it at this test's own indexer globals), so the only way `ensure`
    runs at all here is if `cmd_kb_sync` calls it directly."""
    order = []
    monkeypatch.setattr(agentctl.venv_bootstrap, "ensure",
                        lambda *a, **k: order.append("ensure"))
    real_ensure_db = indexer.ensure_db

    def recording_ensure_db(*a, **k):
        order.append("ensure_db")
        return real_ensure_db(*a, **k)

    monkeypatch.setattr(indexer, "ensure_db", recording_ensure_db)

    assert agentctl.cmd_kb_sync(_args()) == 0
    assert order == ["ensure", "ensure_db"], (
        f"venv_bootstrap.ensure must run before the index is touched; got {order}")


def test_kb_sync_indexes_what_the_fast_forward_brought(kb_repos, agentctl, capsys):
    """The headline case. A doc pushed elsewhere is readable from the index the
    moment sync returns — no separate watcher step, no staleness window."""
    _write_doc(kb_repos.seed, "arrived", body="New content.")
    _git(kb_repos.seed, "add", "-A")
    _git(kb_repos.seed, "commit", "--quiet", "-m", "add arrived")
    _git(kb_repos.seed, "push", "--quiet", "origin", "main")

    assert agentctl.cmd_kb_sync(_args()) == 0
    rows = _rows(kb_repos.db)
    assert "projects/platform/reference/arrived.md" in rows
    assert indexer.verify_index()["ok"] is True


def test_kb_sync_indexes_a_deletion(kb_repos, agentctl):
    """A fast-forward that removes a doc must remove its row too."""
    agentctl.cmd_kb_sync(_args(full=True))
    assert "projects/platform/reference/seeded.md" in _rows(kb_repos.db)

    (kb_repos.seed / "projects" / "platform" / "reference" / "seeded.md").unlink()
    _git(kb_repos.seed, "add", "-A")
    _git(kb_repos.seed, "commit", "--quiet", "-m", "drop seeded")
    _git(kb_repos.seed, "push", "--quiet", "origin", "main")

    assert agentctl.cmd_kb_sync(_args()) == 0
    assert "projects/platform/reference/seeded.md" not in _rows(kb_repos.db)


def test_kb_sync_says_what_it_verified_when_already_up_to_date(kb_repos, agentctl, capsys):
    """Silence used to be the up-to-date path's only output, so a caller could not
    tell "nothing to do, and the index is right" from "did nothing at all". An
    operator who registers a project and runs the documented command now reads
    a count, and reads `0` when the index is empty."""
    agentctl.cmd_kb_sync(_args(full=True))
    capsys.readouterr()
    assert agentctl.cmd_kb_sync(_args()) == 0
    out = capsys.readouterr().out
    assert "up to date" in out
    assert "1 docs indexed" in out


def test_kb_sync_quiet_drops_only_the_healthy_line(kb_repos, agentctl, capsys):
    """`--quiet` is what the 10-minute timer passes, so a healthy no-op cycle
    writes nothing to its log. It means the same on every path: the healthy
    `verified` line goes, and nothing else does — a fast-forward still prints
    its summary, and `--verify-only --quiet` is silent when the index is right."""
    agentctl.cmd_kb_sync(_args(full=True))
    capsys.readouterr()
    assert agentctl.cmd_kb_sync(_args(quiet=True)) == 0
    assert capsys.readouterr().out == ""
    assert agentctl.cmd_kb_sync(_args(verify_only=True, quiet=True)) == 0
    assert capsys.readouterr().out == ""

    _write_doc(kb_repos.seed, "arrived", body="New content.")
    _git(kb_repos.seed, "add", "-A")
    _git(kb_repos.seed, "commit", "--quiet", "-m", "add arrived")
    _git(kb_repos.seed, "push", "--quiet", "origin", "main")
    assert agentctl.cmd_kb_sync(_args(quiet=True)) == 0
    out = capsys.readouterr().out
    assert "1 indexed" in out
    assert "verified" not in out


def test_kb_sync_dry_run_when_up_to_date_reports_a_mismatch_without_healing(
        kb_repos, agentctl, capsys):
    """The up-to-date path used to exit 0 without looking, `--dry-run` or not.
    Now it verifies, and dry-run means the same as on `--verify-only`: report
    the mismatch, touch nothing, exit 2 so the caller knows the index is wrong."""
    agentctl.cmd_kb_sync(_args(full=True))
    conn = sqlite3.connect(str(kb_repos.db))
    conn.execute("UPDATE docs SET body_hash = 'wrong'")
    conn.commit()
    conn.close()
    capsys.readouterr()

    assert agentctl.cmd_kb_sync(_args(dry_run=True)) == 2
    assert "VERIFY FAILED" in capsys.readouterr().err
    assert set(_rows(kb_repos.db).values()) == {"wrong"}


def test_kb_sync_indexes_a_doc_the_validator_newly_accepts(kb_repos, agentctl, monkeypatch):
    """The placard case (issue_01M1TJM4D2ZMFK6NT5ZTZHXE2H). A doc the validator
    rejects at index time stays absent while the rule is unchanged. Changing the
    rule changes nothing in the knowledge tree — the validator's opinion of the file
    changed, not the file — so the fast-forward path has nothing to hand the
    indexer, and the checkout is already at origin/main. Verification is the
    only pass that re-reads every doc with the CURRENT validator, so it must run
    on the up-to-date path too, not only after a fast-forward.

    The rule that changes here is the kind roster: a project no longer needs a
    row, because the validator reads `projects/` from the tree (see
    `test_kb_sync_indexes_a_project_the_repo_holds_without_a_registration`)."""
    import knowledge

    rel = "projects/platform/reference/first.md"
    path = kb_repos.seed / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DOC.format(slug="first", title="First", body="Under a new kind.")
                    .replace("kind: reference", "kind: newkind"))
    _git(kb_repos.seed, "add", "-A")
    _git(kb_repos.seed, "commit", "--quiet", "-m", "a doc of an unregistered kind")
    _git(kb_repos.seed, "push", "--quiet", "origin", "main")

    assert agentctl.cmd_kb_sync(_args()) == 0
    assert rel not in _rows(kb_repos.db)

    monkeypatch.setattr(knowledge, "ALLOWED_KINDS", knowledge.ALLOWED_KINDS | {"newkind"})

    assert agentctl.cmd_kb_sync(_args()) == 0
    assert rel in _rows(kb_repos.db)
    assert indexer.verify_index()["ok"] is True


def test_kb_sync_indexes_a_project_the_repo_holds_without_a_registration(
        kb_repos, agentctl):
    """Site 4 of the seeding item: a document under a `projects/<name>/` the
    validator has no row for indexes on the first sync, with no registration."""
    rel = "projects/newproj/reference/first.md"
    path = kb_repos.seed / rel
    path.parent.mkdir(parents=True)
    path.write_text(DOC.format(slug="first", title="First", body="Under a new project.")
                    .replace("id: platform/reference/first", "id: newproj/reference/first")
                    .replace("project: platform", "project: newproj"))
    _git(kb_repos.seed, "add", "-A")
    _git(kb_repos.seed, "commit", "--quiet", "-m", "a doc under an unlisted project")
    _git(kb_repos.seed, "push", "--quiet", "origin", "main")

    assert agentctl.cmd_kb_sync(_args()) == 0
    assert rel in _rows(kb_repos.db)
    assert indexer.verify_index()["ok"] is True


def _push_a_dropped_document(kb_repos) -> str:
    rel = "projects/platform/reference/bad.md"
    (kb_repos.seed / rel).write_text(DOC.format(slug="bad", title="Bad", body="x")
                                     .replace("kind: reference", "kind: nonsense"))
    _git(kb_repos.seed, "add", "-A")
    _git(kb_repos.seed, "commit", "--quiet", "-m", "a document the validator drops")
    _git(kb_repos.seed, "push", "--quiet", "origin", "main")
    return rel


def _lines_naming(err: str, rel: str) -> list[str]:
    return [ln for ln in err.splitlines() if rel in ln]


def test_kb_sync_names_a_deferred_document_and_does_not_call_it_invalid(
        kb_repos, agentctl, capsys, monkeypatch):
    """A roster outage defers an audience document: the sync names it DEFERRED with
    the row kept, not INVALID, and still exits 0 because nothing is wrong on disk."""
    rel = "projects/platform/reference/deferred.md"
    (kb_repos.seed / rel).write_text(DOC.format(slug="deferred", title="Deferred", body="x"))
    _git(kb_repos.seed, "add", "-A")
    _git(kb_repos.seed, "commit", "--quiet", "-m", "an audience document during an outage")
    _git(kb_repos.seed, "push", "--quiet", "origin", "main")

    def unreachable():
        raise knowledge.RosterUnavailable("connection refused")

    monkeypatch.setattr(knowledge, "_fetch_roster", unreachable)
    monkeypatch.setattr(knowledge, "_roster_cache", None)
    capsys.readouterr()

    assert agentctl.cmd_kb_sync(_args()) == 0
    named = _lines_naming(capsys.readouterr().err, rel)
    assert len(named) == 1 and named[0].startswith("kb sync: DEFERRED "), named
    assert named[0].endswith("connection refused"), named
    assert "(row kept)" not in named[0], "a document never indexed has no row to keep"
    assert rel not in _rows(kb_repos.db)


def test_kb_sync_names_a_refused_document_on_the_targeted_pass_as_refused(
        kb_repos, agentctl, capsys, monkeypatch):
    """A pushed edit that the index refuses on its own grounds is named REFUSED on the
    targeted pass, not DEFERRED, and its row stays through the outage."""
    rel = "projects/platform/reference/seeded.md"
    agentctl.cmd_kb_sync(_args(full=True))
    seed_doc = kb_repos.seed / rel
    seed_doc.write_text(seed_doc.read_text().replace("status: current", "status: bogus"))
    _git(kb_repos.seed, "add", "-A")
    _git(kb_repos.seed, "commit", "--quiet", "-m", "a refused edit during an outage")
    _git(kb_repos.seed, "push", "--quiet", "origin", "main")

    def unreachable():
        raise knowledge.RosterUnavailable("connection refused")

    monkeypatch.setattr(knowledge, "_fetch_roster", unreachable)
    monkeypatch.setattr(knowledge, "_roster_cache", None)
    capsys.readouterr()

    assert agentctl.cmd_kb_sync(_args()) == 0
    named = _lines_naming(capsys.readouterr().err, rel)
    assert named == [f"kb sync: REFUSED {rel}: invalid status: bogus (row kept)"], named
    assert rel in _rows(kb_repos.db), "an outage keeps the refused document's row"


def test_kb_sync_verify_names_each_deferred_document_it_does_not_check(
        kb_repos, agentctl, capsys, monkeypatch):
    """The verify pass names a deferred document on its own line, so a healthy-looking
    pass cannot hide one. An already-indexed document keeps its row and is still named."""
    seeded = "projects/platform/reference/seeded.md"
    agentctl.cmd_kb_sync(_args(full=True))
    assert seeded in _rows(kb_repos.db)

    def unreachable():
        raise knowledge.RosterUnavailable("connection refused")

    monkeypatch.setattr(knowledge, "_fetch_roster", unreachable)
    monkeypatch.setattr(knowledge, "_roster_cache", None)
    capsys.readouterr()

    agentctl.cmd_kb_sync(_args(verify_only=True))
    named = _lines_naming(capsys.readouterr().err, seeded)
    assert named == [f"kb sync: DEFERRED {seeded}: connection refused (row kept)"], named
    assert seeded in _rows(kb_repos.db), "an outage keeps the indexed row"


def test_kb_sync_verify_does_not_call_a_refused_document_with_a_kept_row_checked(
        kb_repos, agentctl, capsys, monkeypatch):
    """During an outage a refused document keeps its row, and the verify pass never compares
    that row. Its line is REFUSED with the refusal. The healthy line counts it as not
    checked, so it reads `partly checked`, and it is not counted as expected absent."""
    seeded = "projects/platform/reference/seeded.md"
    agentctl.cmd_kb_sync(_args(full=True))
    assert seeded in _rows(kb_repos.db)
    path = kb_repos.checkout / seeded
    path.write_text(path.read_text().replace("status: current", "status: bogus"))

    def unreachable():
        raise knowledge.RosterUnavailable("connection refused")

    monkeypatch.setattr(knowledge, "_fetch_roster", unreachable)
    monkeypatch.setattr(knowledge, "_roster_cache", None)
    capsys.readouterr()

    assert agentctl.cmd_kb_sync(_args(verify_only=True)) == 0
    captured = capsys.readouterr()
    named = _lines_naming(captured.err, seeded)
    assert named == [f"kb sync: REFUSED {seeded}: invalid status: bogus (row kept)"], named
    out = captured.out
    assert "partly checked" in out and "1 not checked" in out, out
    assert "verified" not in out, out
    assert "0 invalid on disk (expected absent)" in out, out
    assert "1 invalid on disk" not in out, out
    assert seeded in _rows(kb_repos.db), "an outage keeps the refused document's row"

    assert agentctl.cmd_kb_sync(_args(verify_only=True, quiet=True)) == 0
    quiet = capsys.readouterr()
    assert "1 not checked: the Library's roster could not be read" in quiet.err, quiet
    assert "verified" not in quiet.out + quiet.err, quiet


def test_kb_sync_heal_does_not_call_a_refused_document_with_a_kept_row_checked(
        kb_repos, agentctl, capsys, monkeypatch):
    """A heal during an outage reports the same not-checked count as the healthy line. The
    no-roster document is missing, so the verify pass heals; the refused document keeps its
    row through the heal, and the healed line still counts it as not checked."""
    seeded = "projects/platform/reference/seeded.md"
    agentctl.cmd_kb_sync(_args(full=True))
    path = kb_repos.checkout / seeded
    path.write_text(path.read_text().replace("status: current", "status: bogus"))
    fresh = kb_repos.checkout / "projects/platform/reference/no-roster.md"
    fresh.write_text(DOC.format(slug="no-roster", title="No roster", body="x")
                     .replace("audience: [architect]\n", ""))

    def unreachable():
        raise knowledge.RosterUnavailable("connection refused")

    monkeypatch.setattr(knowledge, "_fetch_roster", unreachable)
    monkeypatch.setattr(knowledge, "_roster_cache", None)
    capsys.readouterr()

    assert agentctl.cmd_kb_sync(_args(verify_only=True)) == 0
    captured = capsys.readouterr()
    assert "VERIFY FAILED" in captured.err and "missing" in captured.err, captured.err
    assert "kb sync: healed — 2 docs indexed, 1 not checked\n" == captured.out, captured.out
    assert seeded in _rows(kb_repos.db), "an outage keeps the refused document's row"


def test_kb_sync_verify_calls_a_refused_document_with_no_row_expected_absent(
        kb_repos, agentctl, capsys, monkeypatch):
    """A document refused during an outage that was never indexed has no row to keep, so
    nothing serves it and the verify pass may call it expected absent. The seeded document
    is deferred by the outage, so the line reads `partly checked` with one not checked, and
    the refused document adds to the absent count only. The line is exact, so a refused
    document with no row cannot be counted as not checked without this test failing."""
    agentctl.cmd_kb_sync(_args(full=True))
    rel = "projects/platform/reference/refused-new.md"
    (kb_repos.checkout / rel).write_text(
        DOC.format(slug="refused-new", title="Refused new", body="x")
        .replace("status: current", "status: bogus"))

    def unreachable():
        raise knowledge.RosterUnavailable("connection refused")

    monkeypatch.setattr(knowledge, "_fetch_roster", unreachable)
    monkeypatch.setattr(knowledge, "_roster_cache", None)
    capsys.readouterr()

    assert agentctl.cmd_kb_sync(_args(verify_only=True)) == 0
    captured = capsys.readouterr()
    named = _lines_naming(captured.err, rel)
    assert named == [f"kb sync: REFUSED {rel}: invalid status: bogus"], named
    assert captured.out == (
        "kb sync: partly checked — 1 docs indexed, 1 invalid on disk "
        "(expected absent), 1 not checked\n"), captured.out
    assert rel not in _rows(kb_repos.db), "a document never indexed gets no row in an outage"


def test_kb_sync_verify_does_not_say_verified_over_a_document_it_did_not_check(
        kb_repos, agentctl, capsys, monkeypatch):
    """A roster-deferred document was not compared, so the healthy line says `partly
    checked` and counts it as not checked. The quiet line counts it the same way."""
    agentctl.cmd_kb_sync(_args(full=True))
    capsys.readouterr()

    def unreachable():
        raise knowledge.RosterUnavailable("connection refused")

    monkeypatch.setattr(knowledge, "_fetch_roster", unreachable)
    monkeypatch.setattr(knowledge, "_roster_cache", None)

    assert agentctl.cmd_kb_sync(_args(verify_only=True)) == 0
    out = capsys.readouterr().out
    assert "partly checked" in out and "1 not checked" in out, out
    assert "verified" not in out, out

    assert agentctl.cmd_kb_sync(_args(verify_only=True, quiet=True)) == 0
    err = capsys.readouterr().err
    assert "1 not checked: the Library's roster could not be read" in err, err


def test_a_never_indexed_deferred_doc_is_admitted_by_the_quiet_autopull_pass(
        kb_repos, agentctl, monkeypatch):
    """The 600 s autopull runs `kb sync --quiet`. With nothing new to fast-forward, that
    is the no-op path, which verifies and repairs. A document deferred during an outage
    must be admitted by that pass once the roster reads, with no `--full` and no edit."""
    rel = "projects/platform/reference/late.md"

    def unreachable():
        raise knowledge.RosterUnavailable("connection refused")

    monkeypatch.setattr(knowledge, "_fetch_roster", unreachable)
    monkeypatch.setattr(knowledge, "_roster_cache", None)
    (kb_repos.seed / rel).parent.mkdir(parents=True, exist_ok=True)
    (kb_repos.seed / rel).write_text(DOC.format(slug="late", title="Late", body="x"))
    _git(kb_repos.seed, "add", "-A")
    _git(kb_repos.seed, "commit", "--quiet", "-m", "a new audience document")
    _git(kb_repos.seed, "push", "--quiet", "origin", "main")

    assert agentctl.cmd_kb_sync(_args()) == 0
    assert rel not in _rows(kb_repos.db), "nothing is admitted during an outage"

    # The bound has passed by the next 600 s cycle, and the Library answers.
    monkeypatch.setattr(knowledge, "_fetch_roster", lambda: frozenset({"architect"}))
    monkeypatch.setattr(knowledge, "_roster_cache", None)
    assert agentctl.cmd_kb_sync(_args(quiet=True)) == 0
    assert rel in _rows(kb_repos.db), "the no-op pass must admit the deferred document"
    assert indexer.verify_index()["ok"] is True


def test_kb_sync_names_a_dropped_document_once_when_it_arrives(
        kb_repos, agentctl, capsys):
    """The sync that brings a document in prints its INVALID line; the verify pass
    that follows must not print a second line for the same document."""
    rel = _push_a_dropped_document(kb_repos)
    capsys.readouterr()

    assert agentctl.cmd_kb_sync(_args()) == 0
    named = _lines_naming(capsys.readouterr().err, rel)
    assert len(named) == 1 and named[0].startswith("kb sync: INVALID "), named

    assert agentctl.cmd_kb_sync(_args(full=True)) == 0
    named = _lines_naming(capsys.readouterr().err, rel)
    assert len(named) == 1 and named[0].startswith("kb sync: INVALID "), named


def test_kb_sync_names_each_dropped_document_on_demand(kb_repos, agentctl, capsys):
    """A document already dropped is named once per non-quiet verify, with its
    reason. Before, the healthy line said `N invalid on disk` and named none."""
    rel = _push_a_dropped_document(kb_repos)
    agentctl.cmd_kb_sync(_args())
    capsys.readouterr()

    assert agentctl.cmd_kb_sync(_args(verify_only=True)) == 0
    named = _lines_naming(capsys.readouterr().err, rel)
    assert named == [f"kb sync: DROPPED {rel}: invalid kind: nonsense"]


def test_kb_sync_quiet_names_a_dropped_document_once_and_then_stays_silent(
        kb_repos, agentctl, capsys):
    """knowledge-autopull runs `kb sync --quiet` every 600 s and logs stderr; a
    no-op cycle must write nothing, so a document that stays invalid is named in
    the cycle that brought it in and not again."""
    rel = _push_a_dropped_document(kb_repos)
    capsys.readouterr()

    assert agentctl.cmd_kb_sync(_args(quiet=True)) == 0
    named = _lines_naming(capsys.readouterr().err, rel)
    assert len(named) == 1 and named[0].startswith("kb sync: INVALID "), named

    for _ in range(3):
        assert agentctl.cmd_kb_sync(_args(quiet=True)) == 0
        assert agentctl.cmd_kb_sync(_args(verify_only=True, quiet=True)) == 0
    captured = capsys.readouterr()
    assert captured.err == "" and captured.out == ""


def test_kb_sync_refuses_a_diverged_checkout(kb_repos, agentctl, capsys):
    """Never force a fast-forward: the checkout may hold work that is not on
    origin, which is exactly the ARCH-395 divergence incident."""
    _write_doc(kb_repos.checkout, "local-only", body="Unpushed local work.")
    _git(kb_repos.checkout, "add", "-A")
    _git(kb_repos.checkout, "commit", "--quiet", "-m", "local work")

    _write_doc(kb_repos.seed, "remote-only")
    _git(kb_repos.seed, "add", "-A")
    _git(kb_repos.seed, "commit", "--quiet", "-m", "remote work")
    _git(kb_repos.seed, "push", "--quiet", "origin", "main")

    assert agentctl.cmd_kb_sync(_args()) == 1
    assert "REFUSED" in capsys.readouterr().err
    # the local commit survives
    assert (kb_repos.checkout / "projects/platform/reference/local-only.md").exists()


def test_kb_sync_heals_a_stale_index(kb_repos, agentctl):
    """The self-heal that replaces `launchctl kickstart`. A row that drifted out
    of step with disk is detected and reconciled without a human noticing."""
    agentctl.cmd_kb_sync(_args(full=True))
    conn = sqlite3.connect(str(kb_repos.db))
    conn.execute("UPDATE docs SET body_hash = 'wrong'")
    conn.commit()
    conn.close()
    assert indexer.verify_index()["ok"] is False

    assert agentctl.cmd_kb_sync(_args(verify_only=True)) == 0
    assert indexer.verify_index()["ok"] is True


def test_kb_sync_no_heal_reports_without_fixing(kb_repos, agentctl, capsys):
    """Exit 2 means 'the index is wrong', distinct from exit 1 'sync could not run'."""
    agentctl.cmd_kb_sync(_args(full=True))
    conn = sqlite3.connect(str(kb_repos.db))
    conn.execute("UPDATE docs SET body_hash = 'wrong'")
    conn.commit()
    conn.close()

    assert agentctl.cmd_kb_sync(_args(verify_only=True, no_heal=True)) == 2
    assert "VERIFY FAILED" in capsys.readouterr().err
    assert indexer.verify_index()["ok"] is False


def test_kb_sync_dry_run_does_not_fast_forward(kb_repos, agentctl):
    _write_doc(kb_repos.seed, "pending")
    _git(kb_repos.seed, "add", "-A")
    _git(kb_repos.seed, "commit", "--quiet", "-m", "pending")
    _git(kb_repos.seed, "push", "--quiet", "origin", "main")

    before = _git(kb_repos.checkout, "rev-parse", "HEAD").stdout.strip()
    assert agentctl.cmd_kb_sync(_args(dry_run=True)) == 0
    assert _git(kb_repos.checkout, "rev-parse", "HEAD").stdout.strip() == before
    assert "projects/platform/reference/pending.md" not in _rows(kb_repos.db)


# --- instruction blocks reach the index, and a broken one does not ------------

BLOCK_REL = "projects/platform/instruction-blocks/no-agent-merges-a-pull-request.md"
# The first real block's text, as convivy/knowledge's validator tests carry it.
FIRST_BLOCK = """---
id: platform/instruction-blocks/no-agent-merges-a-pull-request
kind: instruction-block
project: platform
title: No agent merges a pull request
status: current
scope: fleet
audience: [team]
topics: [merge-safety]
last_reviewed: 2026-09-16
authors: [library]
---

No agent merges a pull request. Merging is a person's decision.

There is one exception. The Library merges its own `kb/` pull requests to `convivy/knowledge` with `agentctl kb merge`, once review status reads PASS within the review round cap. A knowledge pull request built by hand is not a `kb/` pull request, and it waits for a person like any other.

Every other agent, on every repository, stops when review passes and leaves the merge to a person.
"""


def _write_block(root: Path, text: str) -> None:
    path = root / BLOCK_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_the_first_instruction_block_is_indexed(corpus, tmp_path):
    """Before this kind was mirrored the indexer refused it as `invalid kind`, so a
    block created through kb_create landed in the repo and was never served
    (issue_01M2N9YV3G52KBYNR614WAC5NY)."""
    _write_block(corpus, FIRST_BLOCK)
    assert indexer.document_errors(FIRST_BLOCK) == []
    assert indexer.index_paths([BLOCK_REL])["indexed"] == 1
    assert BLOCK_REL in _rows(tmp_path / "index.db")


def test_a_block_breaking_a_body_rule_is_refused_by_the_indexer(corpus, tmp_path):
    """The body rules run where the body is, so a block that passes frontmatter
    validation is still refused, and document_errors says why before a write."""
    broken = FIRST_BLOCK.replace("Merging is a person's decision.",
                                 "Merging is a person's decision, as noted above.")
    _write_block(corpus, broken)
    assert any("relative cross-reference" in e for e in indexer.document_errors(broken))
    assert indexer.index_paths([BLOCK_REL])["indexed"] == 0
    assert BLOCK_REL not in _rows(tmp_path / "index.db")


def test_a_malformed_document_does_not_stop_the_scan(corpus, tmp_path):
    """A stray `-` nests a list inside binds_agents; a list-valued id reached
    sqlite3. Either raised out of the scan, so no other document indexed."""
    _write_block(corpus, FIRST_BLOCK)
    bad = {
        "projects/platform/instruction-blocks/nested.md": FIRST_BLOCK.replace(
            "id: platform/instruction-blocks/no-agent-merges-a-pull-request",
            "id: platform/instruction-blocks/nested").replace(
            "scope: fleet", "scope: agent\nbinds_agents:\n  - - coder"),
        "projects/platform/decisions/list-id.md":
            "---\nid: [platform/decisions/x]\nkind: decision\nproject: platform\n"
            "title: T\nstatus: current\n---\nbody\n",
        "projects/platform/decisions/date-topic.md":
            "---\nid: platform/decisions/date-topic\nkind: decision\nproject: platform\n"
            "title: T\nstatus: current\ntopics:\n  - 2026-08-11\n---\nbody\n",
    }
    for rel, text in bad.items():
        (corpus / rel).parent.mkdir(parents=True, exist_ok=True)
        (corpus / rel).write_text(text)

    result = indexer.full_scan_and_reconcile()
    assert {rel for rel, _ in result["invalid"]} == set(bad)
    assert set(_rows(tmp_path / "index.db")) == {BLOCK_REL}


_VALID_HEAD = "---\nid: platform/decisions/{slug}\nkind: decision\nproject: platform\ntitle: T\nstatus: current\n"


@pytest.mark.parametrize("slug,extra,expected", [
    ("mixed-keys", "extra:\n  1: a\n  b: c\n", "TypeError"),
    ("date-key", "extra:\n  2026-08-11: a\n", "TypeError"),
    ("self-reference", "extra: &x [*x]\n", "ValueError"),
])
def test_frontmatter_upsert_cannot_store_is_refused_before_upsert(corpus, tmp_path, slug, extra, expected):
    """Validation calls the same serialization upsert uses, so every shape that
    would raise at write time is an `invalid` entry instead."""
    text = _VALID_HEAD.format(slug=slug) + extra + "---\nbody\n"
    rel = f"projects/platform/decisions/{slug}.md"
    (corpus / rel).parent.mkdir(parents=True, exist_ok=True)
    (corpus / rel).write_text(text)
    errs = indexer.document_errors(text)
    assert any(e.startswith(f"frontmatter cannot be stored: {expected}") for e in errs), errs
    result = indexer.full_scan_and_reconcile()
    assert [rel] == [r for r, _ in result["invalid"]]


# --- the fetch is made as convivy-library --------------------------------------
# `kb sync` runs on a timer with nobody watching. It fetched over the ssh remote, so
# retiring the operator's key would have left it reading "offline" and exiting 0
# with the index going stale. It fetches as the App now, and a missing identity is
# reported as itself.


def _advance_origin(kb_repos, slug="arrived"):
    _write_doc(kb_repos.seed, slug, body="New content.")
    _git(kb_repos.seed, "add", "-A")
    _git(kb_repos.seed, "commit", "--quiet", "-m", f"add {slug}")
    _git(kb_repos.seed, "push", "--quiet", "origin", "main")


def test_kb_sync_fetches_as_the_app_over_the_https_url(kb_repos, agentctl, library_identity):
    """One fetch, naming the https URL and the refspec that updates `origin/main`;
    the fixture fails a call that names `origin`. It asks for the read-only token:
    a fetch has no use for `contents: write`."""
    import kb_merge as KM
    _advance_origin(kb_repos)
    assert agentctl.cmd_kb_sync(_args()) == 0
    assert library_identity.calls == [("fetch", "--quiet", KM.push_url(), KM.MAIN_REFSPEC)]
    assert library_identity.mints == ["read"]
    assert "projects/platform/reference/arrived.md" in _rows(kb_repos.db)


def test_kb_sync_without_the_identity_says_so_and_does_not_read_as_offline(
        kb_repos, agentctl, library_identity, monkeypatch, capsys):
    """The suite holds no App key (conftest), so the real identity refuses. Exit 1,
    the stage named, and NOT the offline message, which exits 0 and would leave
    the index stale with nothing in the timer's log saying why."""
    monkeypatch.setattr(agentctl, "_kb_library", library_identity.real)
    _advance_origin(kb_repos)
    before = _git(kb_repos.checkout, "rev-parse", "HEAD").stdout
    assert agentctl.cmd_kb_sync(_args()) == 1
    err = capsys.readouterr().err
    assert "cannot obtain the convivy-library App identity" in err and "App key:" in err
    assert "offline" not in err
    assert _git(kb_repos.checkout, "rev-parse", "HEAD").stdout == before
    assert library_identity.calls == []


def test_kb_sync_still_treats_an_unreachable_github_as_offline(
        kb_repos, agentctl, monkeypatch, capsys):
    """The timer retries an outage, so it stays exit 0 with the index as it was."""
    import kb_merge as KM

    def down(pull_requests=False, read_only=False):
        raise KM.Unreachable("GitHub could not be reached: nodename nor servname provided")
    monkeypatch.setattr(agentctl, "_kb_library", down)
    assert agentctl.cmd_kb_sync(_args()) == 0
    assert "likely offline" in capsys.readouterr().err


def test_kb_sync_says_why_a_fetch_failed(kb_repos, agentctl, monkeypatch, capsys):
    """A rejected token and a dead network both fail the fetch. The reason is
    printed, so the offline message alone is not all a rejected token leaves."""
    class Refused(agentctl._KbLibrary):
        def __call__(self, root, *a):
            return subprocess.CompletedProcess(a, 128, "", "fatal: HTTP 403 Forbidden")
    monkeypatch.setattr(agentctl, "_kb_library", lambda **kw: Refused("t"))
    assert agentctl.cmd_kb_sync(_args()) == 0
    err = capsys.readouterr().err
    assert "likely offline" in err and "HTTP 403 Forbidden" in err


def test_kb_sync_verify_only_needs_no_identity(kb_repos, agentctl, monkeypatch):
    """`--verify-only` reads the index and the tree and fetches nothing, so it must
    not mint a token."""
    assert agentctl.cmd_kb_sync(_args(full=True)) == 0

    def no_identity(pull_requests=False, read_only=False):
        raise AssertionError("verify-only asked for the App identity")
    monkeypatch.setattr(agentctl, "_kb_library", no_identity)
    assert agentctl.cmd_kb_sync(_args(verify_only=True)) == 0
