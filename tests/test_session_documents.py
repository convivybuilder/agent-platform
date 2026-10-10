"""`session_documents.session_documents` — the record `knowledge-ctx use`
writes about which knowledge documents a checkout's profile brought.

Against constructed `.claude/` trees, the same way `test_session_commands.py`
tests `session_commands.loaded_commands` — this module reads a directory, not
a live session, so a session name never has to resolve to anything real here.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parent.parent / "lib" / "agent-platform"


@pytest.fixture()
def session_documents():
    """Loaded from this tree by path, the way the other session modules are —
    the deployed farm shadows `lib/` on sys.path and a test that imported by
    name would exercise main's copy instead of the branch's."""
    spec = importlib.util.spec_from_file_location(
        "session_documents", LIB / "session_documents.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def project(tmp_path):
    root = tmp_path / "project"
    (root / ".claude").mkdir(parents=True)
    return root


def _individuate(project, *, profile="agent-platform/daytime",
                  materialized_at="2026-09-14T18:00:00+00:00",
                  documents=None, record_text=None):
    """Write `.active-profile` and `.profile-documents.json` the way
    `knowledge-ctx use` does, so these tests exercise the read side against
    the exact shape the write side produces."""
    claude = project / ".claude"
    claude.joinpath(".active-profile").write_text(f"{profile}\n")
    if record_text is None:
        record_text = json.dumps({
            "profile": profile,
            "materialized_at": materialized_at,
            "documents": documents if documents is not None else [],
        })
    claude.joinpath(".profile-documents.json").write_text(record_text)


# --- the four "no answer" reasons --------------------------------------------


def test_no_cwd_is_no_such_session(session_documents):
    assert session_documents.session_documents(None) == {"reason": "no_such_session"}


def test_no_active_profile_is_not_materialized(session_documents, project):
    out = session_documents.session_documents(str(project))
    assert out == {"reason": "not_materialized"}


def test_active_profile_with_no_record_is_no_document_record(session_documents, project):
    """A checkout individuated by a knowledge-ctx older than this record has
    `.active-profile` but never wrote `.profile-documents.json`."""
    project.joinpath(".claude", ".active-profile").write_text("agent-platform/daytime\n")
    out = session_documents.session_documents(str(project))
    assert out == {"reason": "no_document_record"}


def test_malformed_json_is_unreadable_record(session_documents, project):
    _individuate(project, record_text="{not json")
    out = session_documents.session_documents(str(project))
    assert out == {"reason": "unreadable_record"}


def test_valid_json_with_the_wrong_shape_is_unreadable_record(session_documents, project):
    """Parses fine, but is not the record `knowledge-ctx` writes — a list
    where the contract promises an object with `documents`."""
    _individuate(project, record_text=json.dumps(["not", "the", "right", "shape"]))
    out = session_documents.session_documents(str(project))
    assert out == {"reason": "unreadable_record"}


def test_reads_either_timestamp_key_and_answers_under_both(session_documents, project):
    """Decision 0088: a record written before the rename carries only
    `materialized_at`; a new one may carry only `individuated_at`. Either reads,
    and the answer carries the value under both keys."""
    for key in ("materialized_at", "individuated_at"):
        _individuate(project, record_text=json.dumps({
            "profile": "agent-platform/daytime", key: "2026-09-14T18:00:00+00:00",
            "documents": [],
        }))
        out = session_documents.session_documents(str(project))
        assert out["materialized_at"] == "2026-09-14T18:00:00+00:00"
        assert out["individuated_at"] == "2026-09-14T18:00:00+00:00"


def test_a_record_with_neither_timestamp_is_unreadable_record(session_documents, project):
    _individuate(project, record_text=json.dumps(
        {"profile": "agent-platform/daytime", "documents": []}))
    assert session_documents.session_documents(str(project)) == {
        "reason": "unreadable_record"}


def test_a_missing_documents_field_is_unreadable_record(session_documents, project):
    _individuate(project, record_text=json.dumps({
        "profile": "agent-platform/daytime",
        "materialized_at": "2026-09-14T18:00:00+00:00",
    }))
    out = session_documents.session_documents(str(project))
    assert out == {"reason": "unreadable_record"}


def test_a_document_missing_its_kind_is_unreadable_record(session_documents, project):
    _individuate(project, documents=[{"id": "shared/decisions/0001"}])
    out = session_documents.session_documents(str(project))
    assert out == {"reason": "unreadable_record"}


def test_non_utf8_bytes_in_the_record_are_unreadable_record(session_documents, project):
    """`read_text()` decodes UTF-8; raw non-UTF-8 bytes (a truncated write mid-
    `individuate()`, or a corrupted file) must raise into a reason, never an
    uncaught `UnicodeDecodeError` — a `ValueError` subclass the original
    `except (OSError, json.JSONDecodeError)` didn't cover."""
    project.joinpath(".claude", ".active-profile").write_text("agent-platform/daytime\n")
    record = project / ".claude" / ".profile-documents.json"
    record.write_bytes(b"\xff\xfe\x00\x01broken")

    out = session_documents.session_documents(str(project))

    assert out == {"reason": "unreadable_record"}


def test_an_unreadable_record_file_is_unreadable_record(session_documents, project):
    _individuate(project, documents=[{"id": "shared/decisions/0001", "kind": "decision"}])
    record = project / ".claude" / ".profile-documents.json"
    record.chmod(0o000)
    try:
        out = session_documents.session_documents(str(project))
    finally:
        record.chmod(0o644)
    assert out == {"reason": "unreadable_record"}


def test_the_documents_list_is_never_empty_in_place_of_a_reason(session_documents, project):
    """Every failure path returns a `reason`, never `{"documents": []}` — an
    empty list must mean "individuated, zero documents", not "couldn't tell"."""
    for out in (
        session_documents.session_documents(None),
        session_documents.session_documents(str(project)),
    ):
        assert "documents" not in out
        assert out.get("reason")


# --- the success shape --------------------------------------------------------


def test_an_individuated_checkout_returns_profile_timestamp_and_documents(
        session_documents, project):
    docs = [
        {"id": "agent-platform/agents/coder", "kind": "agent"},
        {"id": "shared/decisions/0001", "kind": "decision"},
    ]
    _individuate(project, profile="agent-platform/daytime",
                 materialized_at="2026-09-14T18:00:00+00:00", documents=docs)

    out = session_documents.session_documents(str(project))

    assert out == {
        "profile": "agent-platform/daytime",
        "materialized_at": "2026-09-14T18:00:00+00:00",
        "individuated_at": "2026-09-14T18:00:00+00:00",
        "documents": docs,
    }


def test_documents_are_sorted_by_id_regardless_of_on_disk_order(session_documents, project):
    """The contract promises sorted-by-id output; this must hold even if a
    future writer (or a hand-edited record) puts them in a different order."""
    _individuate(project, documents=[
        {"id": "zzz/agents/last", "kind": "agent"},
        {"id": "aaa/decisions/first", "kind": "decision"},
    ])
    out = session_documents.session_documents(str(project))
    assert [d["id"] for d in out["documents"]] == [
        "aaa/decisions/first", "zzz/agents/last"]


def test_a_document_entry_with_extra_keys_is_projected_to_id_and_kind(
        session_documents, project):
    """`knowledge-ctx` never writes an entry beyond `{id, kind}`, but a hand-
    edited or corrupted record could carry extra fields; the output must still
    match the spec's `{id, kind}` shape exactly, not pass extras through."""
    _individuate(project, record_text=json.dumps({
        "profile": "agent-platform/daytime",
        "materialized_at": "2026-09-14T18:00:00+00:00",
        "documents": [
            {"id": "shared/decisions/0001", "kind": "decision", "title": "extra"},
        ],
    }))
    out = session_documents.session_documents(str(project))
    assert out["documents"] == [{"id": "shared/decisions/0001", "kind": "decision"}]


def test_zero_documents_is_a_real_success_not_a_reason(session_documents, project):
    """A profile that legitimately matches nothing is an individuated checkout
    with an empty list — not indistinguishable from a failure to answer."""
    _individuate(project, documents=[])
    out = session_documents.session_documents(str(project))
    assert out["documents"] == []
    assert "reason" not in out


# --- the mirrored constant ----------------------------------------------------


def test_profile_documents_name_matches_knowledge_ctx(session_documents, knowledge_ctx):
    """The two constants sit either side of the deployed-lib seam (bin/ tools
    import from ~/.local/lib), so they are literals bound by this test rather
    than a shared import — the same shape as worktrees/knowledge_ctx's
    `_MANIFEST_NAME` pin. A drift here means `knowledge-ctx` writes a file this
    module never looks for."""
    assert (session_documents._PROFILE_DOCUMENTS_NAME
            == knowledge_ctx._PROFILE_DOCUMENTS_NAME)
