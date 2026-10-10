"""A hard link inside the watched tree to a file elsewhere on the same volume
must never be indexed.

security-reviewer (PR #737 round 5, run-f085d502): `_open_doc_file`'s
`dir_fd`/`O_NOFOLLOW` chain (round 4) refuses a SYMLINK at any component,
but a hard link is an ordinary directory entry naming the same inode as some
other path — `O_NOFOLLOW` never fires and `fstat` reports an ordinary
regular file, so the chain opened and indexed it like any other document.
The reviewer confirmed `$HOME/Convivy/knowledge` and
`$HOME/Convivy/.bot-secrets/*.env` share one APFS device and that
`ln` across them succeeds.

The knowledge tree holds no hard-linked working-tree files today (`find
$HOME/Convivy/knowledge -type f -links +1` and the same check
over this repo's own working tree both returned 0 rows, against a positive
control: the same `find` over a scratch directory holding an `ln`-created
hard link did report it), so this refusal drops no current document.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

import indexer

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


def _rows(db: Path) -> set:
    conn = sqlite3.connect(str(db))
    try:
        return {r[0] for r in conn.execute("SELECT source_ref FROM docs").fetchall()}
    finally:
        conn.close()


def test_a_hardlinked_document_is_refused(corpus, tmp_path):
    """A hard link to a file outside the knowledge root, same volume, planted
    inside the tree: `O_NOFOLLOW` never fires (it is not a symlink), so only
    the `st_nlink` check can catch it."""
    secret = tmp_path / "secret.env"
    secret.write_text(DOC.format(slug="evil", title="Evil", body="Secret body."))

    link = corpus / "projects" / "platform" / "reference" / "evil.md"
    os.link(secret, link)
    assert not link.is_symlink(), "a hard link is an ordinary directory entry"

    fm, err = indexer.parse_doc(link)

    assert fm is None, f"a hard link must be refused, not read through: {fm}"
    assert err is not None and "hard link" in err

    action, err = indexer.index_one(link)
    assert err is not None and "hard link" in err
    assert "projects/platform/reference/evil.md" not in _rows(tmp_path / "index.db")


def test_an_ordinary_document_with_one_link_still_indexes(corpus, tmp_path):
    """The st_nlink check must not refuse the common case: a document written
    with no other directory entry naming its inode."""
    path = corpus / "projects" / "platform" / "reference" / "doc.md"
    path.write_text(DOC.format(slug="doc", title="A doc", body="Body text."))
    assert os.stat(path).st_nlink == 1

    fm, err = indexer.parse_doc(path)
    assert err is None, err
    assert fm["title"] == "A doc"

    action, err = indexer.index_one(path)
    assert action == "upserted" and err is None
    assert "projects/platform/reference/doc.md" in _rows(tmp_path / "index.db")
