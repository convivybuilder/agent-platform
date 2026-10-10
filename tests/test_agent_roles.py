"""`role_for` reads the `role` an agent's document declares.

The unit tests build a private index, so they run everywhere. The corpus tests
carry `needs_kb` and check the real documents, which is the only place a
document that disagrees with its own name can be seen.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest

from conftest import knowledge_repo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib" / "agent-platform"))
import agent_roles as AR  # noqa: E402

_SCHEMA = Path(__file__).resolve().parents[1] / "lib" / "knowledge" / "schema.sql"


def _index(tmp_path: Path, docs: dict[str, dict | None], kind: str = "agent") -> Path:
    """An index holding `{doc_id: frontmatter_extra}`. `None` stores the column's
    default, an agent document whose frontmatter declared nothing extra."""
    db = tmp_path / "index.db"
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA.read_text())
    for doc_id, fm in docs.items():
        conn.execute(
            "INSERT INTO docs (id, source, source_ref, kind, project, title, status, "
            "body, body_hash, updated_at, frontmatter_extra) VALUES "
            "(?, 'files', ?, ?, ?, 't', 'current', 'b', 'h', '2026-09-24', ?)",
            (doc_id, f"{doc_id}.md", kind, doc_id.split("/", 1)[0],
             json.dumps(fm) if fm is not None else "{}"))
    conn.commit()
    conn.close()
    return db


# --- what the issue names ----------------------------------------------------

def test_wb_reviewer_returns_the_role_its_document_declares(tmp_path):
    """The name says `reviewer` and the document says otherwise. The declared
    role wins, and a `reviewer` block never reaches a read-only guide checker."""
    db = _index(tmp_path, {"write-better/agents/wb-reviewer": {"role": "writing-reviewer"}})
    assert AR.role_for("write-better/agents/wb-reviewer", db_path=db) == "writing-reviewer"
    assert AR.role_for("wb-reviewer", db_path=db) == "writing-reviewer"


def test_a_crew_reviewer_returns_its_declared_role(tmp_path):
    db = _index(tmp_path, {"selector/agents/sel-reviewer": {"role": "reviewer"},
                           "deck/agents/deck-security-reviewer": {"role": "security-reviewer"}})
    assert AR.role_for("sel-reviewer", db_path=db) == "reviewer"
    assert AR.role_for("deck-security-reviewer", db_path=db) == "security-reviewer"


def test_a_name_ending_in_a_role_returns_the_role_the_document_declares(tmp_path):
    """`wb-` is a registered crew prefix and `reviewer` a real role, so the
    deleted name derivation returned `reviewer` here. The document declares
    `planner`, and that is the answer."""
    db = _index(tmp_path, {"x/agents/x-reviewer": {"role": "planner"}})
    assert AR.role_for("x-reviewer", db_path=db) == "planner"


def test_a_missing_document_returns_none_and_never_a_name_derived_role(tmp_path):
    """`sel-reviewer` would have derived `reviewer` from its name. With no
    document the answer is None, meaning bind by name."""
    db = _index(tmp_path, {"selector/agents/sel-coder": {"role": "coder"}})
    assert AR.role_for("sel-reviewer", db_path=db) is None
    assert AR.role_for("selector/agents/sel-reviewer", db_path=db) is None


def test_a_document_with_no_role_returns_none(tmp_path):
    db = _index(tmp_path, {"selector/agents/sel-reviewer": None,
                           "selector/agents/sel-coder": {"model": "sonnet"},
                           "selector/agents/sel-scout": {"role": ""}})
    assert AR.role_for("sel-reviewer", db_path=db) is None
    assert AR.role_for("sel-coder", db_path=db) is None
    assert AR.role_for("sel-scout", db_path=db) is None


def test_a_full_id_and_a_bare_name_resolve_the_same(tmp_path):
    db = _index(tmp_path, {"selector/agents/sel-reviewer": {"role": "reviewer"}})
    assert (AR.role_for("selector/agents/sel-reviewer", db_path=db)
            == AR.role_for("sel-reviewer", db_path=db) == "reviewer")


def _add(db: Path, doc_id: str, source_ref: str, kind: str, fm: dict | str) -> None:
    """One extra row whose `id` and `source_ref` are set independently, as a
    forged document's are. A str `fm` is stored verbatim (the schema rejects malformed JSON, so a
    non-object such as `[1]` is the unusable form)."""
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO docs (id, source, source_ref, kind, project, title, status, "
        "body, body_hash, updated_at, frontmatter_extra) VALUES "
        "(?, 'files', ?, ?, 'p', 't', 'current', 'b', 'h', '2026-09-24', ?)",
        (doc_id, source_ref, kind, fm if isinstance(fm, str) else json.dumps(fm)))
    conn.commit()
    conn.close()


def test_a_skill_under_agents_declares_no_role(tmp_path):
    """Pins the `kind = 'agent'` filter alone: the path is right, the kind is not."""
    db = _index(tmp_path, {})
    _add(db, "selector/agents/sel-reviewer", "projects/selector/agents/sel-reviewer.md",
         "skill", {"role": "reviewer"})
    assert AR.role_for("sel-reviewer", db_path=db) is None
    assert AR.role_for("selector/agents/sel-reviewer", db_path=db) is None


def test_an_agent_kind_outside_agents_declares_no_role(tmp_path):
    """Pins the path filter alone: the kind is right, the path is not."""
    db = _index(tmp_path, {})
    _add(db, "selector/skills/sel-reviewer", "projects/selector/skills/sel-reviewer.md",
         "agent", {"role": "reviewer"})
    assert AR.role_for("sel-reviewer", db_path=db) is None
    assert AR.role_for("selector/skills/sel-reviewer", db_path=db) is None


def test_a_forged_document_at_an_unguarded_path_is_never_a_candidate(tmp_path):
    """The write guard protects `/agents/` by PATH, and `id` is an author-written
    field. A `kind: agent` file at a decisions/ path is a candidate for neither
    lookup form, whatever `id` it carries, and the real document's answer is
    untouched by its presence."""
    db = _index(tmp_path, {"selector/agents/sel-reviewer": {"role": "reviewer"}})
    _add(db, "selector/agents/sel-coder", "projects/platform/decisions/0099-x.md",
         "agent", {"role": "coder"})
    assert AR.role_for("sel-reviewer", db_path=db) == "reviewer"
    assert AR.role_for("selector/agents/sel-reviewer", db_path=db) == "reviewer"
    assert AR.role_for("sel-coder", db_path=db) is None
    assert AR.role_for("selector/agents/sel-coder", db_path=db) is None


def test_a_forgery_that_took_over_a_real_agents_id_never_supplies_the_role(tmp_path):
    """The bypass as reported. `docs.id` is the primary key and the indexer
    upserts on it, so a forged file at a decisions/ path carrying a real agent's
    `id` leaves ONE row: the real id, the forged path, the forged role. Read by
    `id` (the earlier `role_for`) that row answers `coder`. Read by `source_ref`
    it is not an agent document, so the forged role is never returned."""
    db = _index(tmp_path, {})
    _add(db, "selector/agents/sel-reviewer", "projects/platform/decisions/0099-x.md",
         "agent", {"role": "coder"})
    assert AR.role_for("selector/agents/sel-reviewer", db_path=db) is None
    assert AR.role_for("sel-reviewer", db_path=db) is None


def test_a_bare_name_two_documents_disagree_on_raises_naming_them(tmp_path):
    """A bare name is ambiguous across projects. None would read as "bind by
    name" and silently withhold every `binds_agents` block, so it refuses. The
    full id says which was meant, and two documents that agree are not ambiguous."""
    db = _index(tmp_path, {"a/agents/dup": {"role": "reviewer"},
                           "b/agents/dup": {"role": "coder"},
                           "a/agents/same": {"role": "coder"},
                           "b/agents/same": {"role": "coder"}})
    with pytest.raises(AR.AgentRoleError) as e:
        AR.role_for("dup", db_path=db)
    assert "projects/a/agents/dup" in str(e.value) or "a/agents/dup.md" in str(e.value)
    assert "b/agents/dup.md" in str(e.value)
    assert AR.role_for("a/agents/dup", db_path=db) == "reviewer"
    assert AR.role_for("same", db_path=db) == "coder"


def test_a_matched_document_with_unusable_frontmatter_raises_naming_it(tmp_path):
    db = _index(tmp_path, {"selector/agents/sel-coder": {"role": "coder"}})
    _add(db, "selector/agents/sel-reviewer", "projects/selector/agents/sel-reviewer.md",
         "agent", "[1, 2]")
    with pytest.raises(AR.AgentRoleError, match="projects/selector/agents/sel-reviewer.md"):
        AR.role_for("sel-reviewer", db_path=db)
    with pytest.raises(AR.AgentRoleError, match="sel-reviewer.md"):
        AR.role_for("selector/agents/sel-reviewer", db_path=db)


def test_a_document_with_unusable_frontmatter_is_ignored_when_not_matched(tmp_path):
    """Only matched rows are parsed, so one bad document does not block every
    other agent's lookup."""
    db = _index(tmp_path, {"selector/agents/sel-coder": {"role": "coder"}})
    _add(db, "selector/agents/sel-reviewer", "projects/selector/agents/sel-reviewer.md",
         "agent", "[1, 2]")
    assert AR.role_for("sel-coder", db_path=db) == "coder"


def test_a_missing_index_raises_rather_than_reading_as_bind_by_name(tmp_path):
    """None for every agent at once would leave every role-scoped instruction
    block binding nothing, with the same result as a corpus with no such agents."""
    with pytest.raises(FileNotFoundError):
        AR.role_for("sel-reviewer", db_path=tmp_path / "absent.db")


def test_the_default_index_follows_kb_index_db(tmp_path, monkeypatch):
    db = _index(tmp_path, {"selector/agents/sel-reviewer": {"role": "reviewer"}})
    monkeypatch.setenv("KB_INDEX_DB", str(db))
    assert AR.role_for("sel-reviewer") == "reviewer"


def test_nothing_derives_a_role_from_a_name():
    """The prefix derivation and its exception map are gone, so there is no
    second record of who holds which role to drift from the documents."""
    for name in ("_EXCEPTIONS", "base_name", "agents_for_role", "INTERNAL_APP_PREFIXES"):
        assert not hasattr(AR, name), f"{name} is the name-derived mapping this module deleted"


# --- the real corpus -----------------------------------------------------------

def _roster() -> list[Path]:
    """Every agent document in the knowledge checkout, through `knowledge_repo()`
    so a machine whose root is elsewhere is honoured."""
    return sorted((knowledge_repo() / "projects").glob("*/agents/*.md"))


def _declared(p: Path) -> str | None:
    m = re.search(r"^role:[ \t]*(\S+)[ \t]*$", p.read_text(errors="ignore"), re.M)
    return m.group(1) if m else None


def _doc_id(p: Path) -> str:
    return f"{p.parent.parent.name}/agents/{p.stem}"


@pytest.mark.needs_kb
def test_every_agent_in_the_corpus_resolves_to_the_role_its_file_declares():
    """Both sides computed: the file's `role:` line against what `role_for` reads
    back from the index. A document that loses its role, or an index that has not
    caught up with the checkout, fails here."""
    roster = _roster()
    assert roster, "the corpus has no agent documents, which cannot be right"
    wrong = {_doc_id(p): (_declared(p), AR.role_for(_doc_id(p))) for p in roster
             if AR.role_for(_doc_id(p)) != _declared(p)}
    assert wrong == {}, f"(declared in file, resolved from index): {wrong}"
    assert all(_declared(p) for p in roster), "an agent document declares no role"


@pytest.mark.needs_kb
def test_the_real_wb_reviewer_declares_writing_reviewer():
    assert AR.role_for("write-better/agents/wb-reviewer") == "writing-reviewer"
    assert AR.role_for("wb-reviewer") == "writing-reviewer"


@pytest.mark.needs_kb
def test_a_role_suffixed_name_can_still_fill_another_role():
    """Pins the case the deleted derivation got wrong against the real corpus:
    every agent whose name ends in `-reviewer` and whose document declares a
    different role resolves to the declared one. `wb-reviewer` is the one today,
    and the assertion holds if there are more."""
    odd = [p for p in _roster() if p.stem.endswith("reviewer") and _declared(p) != "reviewer"
           and _declared(p) != "security-reviewer"]
    assert any(p.stem == "wb-reviewer" for p in odd), \
        "wb-reviewer no longer declares a role other than the one its name ends in"
    for p in odd:
        assert AR.role_for(_doc_id(p)) == _declared(p)


@pytest.mark.needs_kb
def test_the_forcing_function_is_registered_to_actually_run():
    """A SKIPPED TEST AND A PASSING ONE PRODUCE THE SAME EVIDENCE. CI checks out
    only `agent-platform`, so a corpus test that lacks `needs_kb` skips there and
    `bin/check-kb-guards`, which selects strictly `-m needs_kb`, never runs it.
    Asked of pytest's own marks rather than the source text."""
    for fn in (test_every_agent_in_the_corpus_resolves_to_the_role_its_file_declares,
               test_the_real_wb_reviewer_declares_writing_reviewer,
               test_a_role_suffixed_name_can_still_fill_another_role):
        marks = {m.name for m in getattr(fn, "pytestmark", [])}
        assert "needs_kb" in marks, (
            f"{fn.__name__} reads the corpus but is not marked needs_kb, so it "
            f"runs on no automatic path")
