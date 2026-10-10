"""A symlinked file inside the watched tree must never be indexed.

security-reviewer (run-8f2c6d62) confirmed by running the module: a symlink
dereferences cleanly through `parse_doc`'s `path.read_text()`, so a symlink
pointing outside the knowledge root gets indexed with an ordinary-looking
`source_ref`, content and all. The gap predates the directory-walk change
this PR makes (shared by `index_one`'s single-file path and
`full_scan_and_reconcile`'s `rglob`); the fix belongs in `parse_doc`, the one
place every ingestion pass reads a file, so the walk, a single file event and
the full scan all refuse it the same way.

The knowledge tree holds no symlinks today (`find $HOME/Convivy/knowledge
-type l -not -path '*/.git/*'` returned 0, with a positive control), so this
refusal drops no current document.
"""
from __future__ import annotations

import shutil
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


def _write_doc(root: Path, rel_dir: Path, slug: str, *, title: str = "A doc",
              body: str = "Body text.") -> Path:
    path = rel_dir / f"{slug}.md"
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


def _rows(db: Path) -> set:
    conn = sqlite3.connect(str(db))
    try:
        return {r[0] for r in conn.execute("SELECT source_ref FROM docs").fetchall()}
    finally:
        conn.close()


def test_index_one_refuses_a_symlinked_file(corpus, tmp_path):
    """The single-file event path (`handle_path` -> `index_one` -> `parse_doc`)."""
    outside = tmp_path / "outside.md"
    outside.write_text(DOC.format(slug="evil", title="Evil", body="Secret body."))

    link = corpus / "projects" / "platform" / "reference" / "evil.md"
    link.symlink_to(outside)

    action, err = indexer.index_one(link)

    assert err is not None, "a symlink must be refused, not silently skipped"
    assert "symlink" in err
    assert "projects/platform/reference/evil.md" not in _rows(tmp_path / "index.db")


def test_full_scan_refuses_a_symlinked_file(corpus, tmp_path):
    """`full_scan_and_reconcile`'s `rglob` walk."""
    outside = tmp_path / "outside.md"
    outside.write_text(DOC.format(slug="evil", title="Evil", body="Secret body."))

    link = corpus / "projects" / "platform" / "reference" / "evil.md"
    link.symlink_to(outside)

    result = indexer.full_scan_and_reconcile()

    assert any("symlink" in err for _, err in result["invalid"]), result["invalid"]
    assert "projects/platform/reference/evil.md" not in _rows(tmp_path / "index.db")


def test_directory_walk_refuses_a_symlinked_file(corpus, tmp_path):
    """The walk this PR adds for a directory-created/moved event
    (`knowledge-watcher`'s `index_new_directory`, which calls `handle_path` on
    every file it finds) must not index a symlink either."""
    outside = tmp_path / "outside.md"
    outside.write_text(DOC.format(slug="evil", title="Evil", body="Secret body."))

    new_dir = corpus / "projects" / "platform" / "reference" / "dropped-in"
    new_dir.mkdir()
    link = new_dir / "evil.md"
    link.symlink_to(outside)

    for path in new_dir.rglob("*"):
        if path.is_file():
            indexer.index_one(path)

    assert "projects/platform/reference/dropped-in/evil.md" not in _rows(tmp_path / "index.db")


def test_a_path_resolving_outside_the_root_via_a_symlinked_ancestor_is_refused(corpus, tmp_path):
    """Not a symlinked leaf file this time -- a symlinked ANCESTOR directory, so
    the leaf path itself is an ordinary file whose resolved location is still
    outside KNOWLEDGE. `parse_doc` must catch this by resolving the whole path,
    not merely checking whether the leaf is a symlink."""
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    (outside_dir / "evil.md").write_text(
        DOC.format(slug="evil", title="Evil", body="Secret body."))

    linked_dir = corpus / "projects" / "platform" / "reference" / "linked"
    linked_dir.symlink_to(outside_dir)

    leaf = linked_dir / "evil.md"
    assert not leaf.is_symlink(), "the leaf itself is an ordinary file"

    fm, err = indexer.parse_doc(leaf)

    assert fm is None
    assert err is not None and "outside" in err


def test_parse_doc_refuses_a_file_swapped_for_a_symlink_after_the_check(
        corpus, tmp_path, monkeypatch):
    """security-reviewer (run-b75f48e0): `_refuse_unsafe_path`'s check and the
    read that used to follow it (`path.read_text()`) were two separate steps,
    so a writer could swap the checked file for a symlink in the window
    between them. Patch the check to perform that exact swap as its own side
    effect — landing the race in the one window it describes — and confirm
    the swapped-in symlink's target is never read into the index."""
    outside = tmp_path / "outside.md"
    outside.write_text(DOC.format(slug="evil", title="Evil", body="Secret body."))

    real = _write_doc(corpus, corpus / "projects" / "platform" / "reference",
                      "doc", title="Real doc", body="Real body.")

    original_check = indexer._refuse_unsafe_path

    def _check_then_swap_for_a_symlink(path):
        result = original_check(path)
        if path == real:
            real.unlink()
            real.symlink_to(outside)
        return result

    monkeypatch.setattr(indexer, "_refuse_unsafe_path", _check_then_swap_for_a_symlink)

    fm, err = indexer.parse_doc(real)

    assert fm is None
    assert err is not None and "symlink" in err


def test_parse_doc_refuses_an_ancestor_swapped_for_a_symlink_after_the_check(
        corpus, tmp_path, monkeypatch):
    """security-reviewer (PR #737 round 3, run-0ea3a971): `_refuse_unsafe_path`'s
    whole-path `resolve()` and `_open_doc_file`'s open are two separate walks of
    the same path string, taken at two different instants — the test above lands
    the race at the LEAF; this one lands it one level up, at an ANCESTOR
    directory, which a leaf-only `O_NOFOLLOW` open can never catch: the leaf
    itself genuinely is a regular, non-symlink file right up to the moment it is
    opened, so there is nothing for `O_NOFOLLOW` on the leaf alone to refuse. It
    is the directory the leaf sits in that gets swapped, between the whole-path
    resolve check and the open that follows it, and a path-based open of the
    leaf then walks straight through the swapped ancestor to whatever now sits
    there under the same name — reachable through ordinary `kb sync`/watcher
    concurrency on a type-changing fast-forward."""
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    (outside_dir / "doc.md").write_text(
        DOC.format(slug="evil", title="Evil", body="Secret body."))

    real_dir = corpus / "projects" / "platform" / "reference" / "realdir"
    leaf = _write_doc(corpus, real_dir, "doc", title="Real doc", body="Real body.")

    original_check = indexer._refuse_unsafe_path

    def _check_then_swap_ancestor_for_a_symlink(path):
        result = original_check(path)
        if path == leaf:
            shutil.rmtree(real_dir)
            real_dir.symlink_to(outside_dir)
        return result

    monkeypatch.setattr(indexer, "_refuse_unsafe_path", _check_then_swap_ancestor_for_a_symlink)

    fm, err = indexer.parse_doc(leaf)

    assert fm is None, f"the ancestor swap must be refused, not read through: {fm}"
    assert err is not None and "symlink" in err
