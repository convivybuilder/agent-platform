"""kb_get / kb_search body_sha256 projection (bin/mcp-kb-server,
issue_01M2MCYDJFHCWGAG1P5HKPDF8V).

The `docs` table has carried `body_hash` (sha256 of the body, written by
`lib/knowledge/indexer.py` at index time) all along; `_row_to_full_doc`
dropped it from the projection. These guard that both read tools now serve
it, read straight from the column rather than rehashed, under the name
`body_sha256` — and that it is never confused with `kb_writes.content_sha`,
a different digest over different bytes (see that module's docstring).
"""
from __future__ import annotations

import hashlib
import sqlite3

import indexer
import kb_writes


def _seed(db_path, doc_id, body, *, body_hash=None):
    """Write one fixture row via the real upsert path, isolated db already
    schema-initialized by the autouse `_isolate_knowledge_index_db` fixture."""
    fm = {
        "id": doc_id,
        "source": "files",
        "source_ref": f"projects/{doc_id}.md",
        "kind": "decision",
        "project": "platform",
        "title": "Fixture doc",
        "status": "current",
        "topics": [],
        "audience": [],
        "applies_to": [],
        "depends_on": [],
        "last_reviewed": None,
        "authors": [],
        "body": body,
        "body_hash": body_hash if body_hash is not None
                     else hashlib.sha256(body.encode()).hexdigest(),
        "updated_at": "2026-09-15T00:00:00Z",
    }
    indexer.upsert(fm)


def test_kb_get_returns_body_sha256_matching_sha256_of_body(
    mcp_kb_server, monkeypatch, _isolate_knowledge_index_db,
):
    db = _isolate_knowledge_index_db
    body = "This is the fixture document body.\n\nSecond paragraph."
    _seed(db, "platform/decisions/9001-fixture", body)
    monkeypatch.setattr(mcp_kb_server, "DB_PATH", db)

    result = mcp_kb_server.kb_get("platform/decisions/9001-fixture")

    assert result is not None
    assert result["body_sha256"] == hashlib.sha256(body.encode()).hexdigest()


def test_kb_get_body_sha256_is_none_for_legacy_row_with_null_hash(
    mcp_kb_server, monkeypatch, tmp_path,
):
    # A hand-built legacy schema where body_hash carries no NOT NULL
    # constraint, standing in for a row indexed before the column existed —
    # `_row_to_full_doc` must read it back as None rather than raise.
    db = tmp_path / "legacy-index.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE docs (
            id TEXT PRIMARY KEY, source TEXT NOT NULL, source_ref TEXT NOT NULL,
            kind TEXT NOT NULL, project TEXT NOT NULL, title TEXT NOT NULL,
            status TEXT NOT NULL, topics TEXT NOT NULL DEFAULT '[]',
            audience TEXT NOT NULL DEFAULT '[]', applies_to TEXT NOT NULL DEFAULT '[]',
            depends_on TEXT NOT NULL DEFAULT '[]', last_reviewed TEXT,
            authors TEXT NOT NULL DEFAULT '[]', body TEXT NOT NULL,
            body_hash TEXT, notion_view INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );
    """)
    conn.execute(
        "INSERT INTO docs (id, source, source_ref, kind, project, title, status, "
        "body, body_hash, updated_at) VALUES "
        "('platform/decisions/9002-legacy', 'files', "
        "'projects/platform/decisions/9002-legacy.md', 'decision', 'platform', "
        "'Legacy fixture', 'current', 'Legacy body.', NULL, "
        "'2026-01-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(mcp_kb_server, "DB_PATH", db)

    result = mcp_kb_server.kb_get("platform/decisions/9002-legacy")

    assert result is not None
    assert result["body_sha256"] is None


def test_kb_search_serves_body_sha256_on_hits(
    mcp_kb_server, monkeypatch, _isolate_knowledge_index_db,
):
    db = _isolate_knowledge_index_db
    body = "Unique needle-word content for the search fixture."
    _seed(db, "platform/decisions/9003-search-fixture", body)
    monkeypatch.setattr(mcp_kb_server, "DB_PATH", db)

    hits = mcp_kb_server.kb_search(query="needle-word")

    assert len(hits) == 1
    assert hits[0]["body_sha256"] == hashlib.sha256(body.encode()).hexdigest()


def test_kb_search_serves_body_sha256_on_structured_query(
    mcp_kb_server, monkeypatch, _isolate_knowledge_index_db,
):
    # No `query` — the non-FTS structured-query branch, a separate SELECT.
    db = _isolate_knowledge_index_db
    body = "Structured-query fixture body."
    _seed(db, "platform/decisions/9004-structured-fixture", body)
    monkeypatch.setattr(mcp_kb_server, "DB_PATH", db)

    hits = mcp_kb_server.kb_search(project="platform")

    assert len(hits) == 1
    assert hits[0]["body_sha256"] == hashlib.sha256(body.encode()).hexdigest()


def test_body_sha256_is_not_content_sha(
    mcp_kb_server, monkeypatch, _isolate_knowledge_index_db,
):
    """`body_sha256` (index `body_hash`, body only) and `kb_writes.content_sha`
    (whole file, frontmatter included) are two different digests over two
    different byte ranges — a frontmatter-only edit must move one and not
    the other, so the two must never collapse to the same value or name."""
    db = _isolate_knowledge_index_db
    body = "Body text shared by both hashes' input."
    full_file = f"---\nid: platform/decisions/9005-both-hashes\nstatus: current\n---\n\n{body}\n"
    _seed(db, "platform/decisions/9005-both-hashes", body)
    monkeypatch.setattr(mcp_kb_server, "DB_PATH", db)

    result = mcp_kb_server.kb_get("platform/decisions/9005-both-hashes")

    whole_file_sha = kb_writes.content_sha(full_file)
    assert result["body_sha256"] != whole_file_sha
    assert result["body_sha256"] == hashlib.sha256(body.encode()).hexdigest()
    assert "content_sha" not in result
