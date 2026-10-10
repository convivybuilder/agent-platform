"""Regression coverage for issue_01M2C0K5C228VGJRV04THX67Q4: a local test run
with no `builder_root.CONFIG` deleted every row from the live knowledge index.

`indexer.py` is not changed by this fix — `full_scan_and_reconcile()` deleting
every row it does not see under `PROJECTS` is correct, intended behaviour when
`PROJECTS` and `DB` genuinely describe the same tree. The bug was that a test
could leave `DB` pointed at the real index while `PROJECTS` pointed at nothing.
The fix is entirely in test isolation (`conftest.py`'s `_isolate_knowledge_index_db`
and the `machine` fixture in `test_indexer_root_marker.py`), so this file pins
both halves: the mechanism the incident exploited, and the guard that now stands
between a no-config run and the real index.
"""
import os
import sqlite3
from pathlib import Path

import indexer

SCHEMA = Path(__file__).resolve().parent.parent / "lib" / "knowledge" / "schema.sql"


def _sentinel_fm() -> dict:
    return {
        "id": "platform/reference/sentinel",
        "source": "files",
        "source_ref": "sentinel.md",
        "kind": "reference",
        "project": "platform",
        "title": "Sentinel",
        "status": "current",
        "body": "keep me",
        "body_hash": "abc123",
        "updated_at": "2026-01-01T00:00:00Z",
    }


def test_full_scan_and_reconcile_deletes_everything_it_is_pointed_at_when_projects_is_empty(
        monkeypatch, tmp_path):
    """The mechanism the incident exploited, isolated and pinned directly: with
    `PROJECTS` resolving to a directory that does not exist — what a missing
    `builder_root.CONFIG` produces — `full_scan_and_reconcile()` treats every row
    in whichever `DB` it is bound to as stale and deletes it. This is exactly what
    happened to the real index on 2026-09-12, reproduced here against a throwaway
    file standing in for it. This test does not depend on the fix below: it
    documents why the fix has to exist."""
    db = tmp_path / "stands-in-for-a-live-index.db"
    monkeypatch.setattr(indexer, "DB", db)
    monkeypatch.setattr(indexer, "SCHEMA", SCHEMA)
    indexer.ensure_db()
    indexer.upsert(_sentinel_fm())

    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("SELECT source_ref FROM docs").fetchall() == [("sentinel.md",)]
    finally:
        conn.close()

    monkeypatch.setattr(indexer, "PROJECTS", tmp_path / "no-such-builder-root" / "projects")
    indexer.full_scan_and_reconcile()

    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("SELECT source_ref FROM docs").fetchall() == []
    finally:
        conn.close()


def test_every_test_gets_its_own_temp_index_db_before_the_test_body_runs():
    """The guard itself, asserted directly. Without this test touching
    `indexer.DB` at all, the autouse fixture in `conftest.py` has already
    repointed it to a fresh, schema-initialized file of its own — never at the
    module-level default (`KB_INDEX_DB`, or the real `~/.knowledge-index/index.db`
    with no override) that every test in this process would otherwise share.

    Checked against the module-level default rather than a specific temp path,
    because the fixture uses `tmp_path_factory` (a directory of its own, not this
    test's `tmp_path`) precisely so it never collides with a test that repurposes
    `tmp_path` as a directory it asserts is exactly empty
    (`test_co_working_on.py`'s `cache` fixture is one). Delete the fixture from
    `conftest.py` and this assertion is the one that catches it: `indexer.DB`
    would then equal `module_default`, shared for the whole pytest run rather
    than per test.
    """
    module_default = Path(os.environ.get(
        "KB_INDEX_DB", str(Path.home() / ".knowledge-index" / "index.db")))
    assert indexer.DB != module_default
    assert indexer.DB.exists(), "the fixture must apply the schema, not just set a path"

    conn = sqlite3.connect(str(indexer.DB))
    try:
        assert conn.execute("SELECT source_ref FROM docs").fetchall() == []
    finally:
        conn.close()

    indexer.upsert(_sentinel_fm())
    conn = sqlite3.connect(str(indexer.DB))
    try:
        assert conn.execute("SELECT source_ref FROM docs").fetchall() == [("sentinel.md",)]
    finally:
        conn.close()
