"""session_documents — which knowledge documents a session's checkout brought.

Deck's cockpit is specified to list "the documents that came with the
checkout" (`deck/bead/deck`), each opening in the side pane. `bin/knowledge-ctx`
already resolves that set on every `use`/`--force` and writes it to
`.claude/.profile-documents.json` (issue_01M0Y6NVCECKQMDS74R1KZ1WT4) — one
`{id, kind}` per document the active profile matched, across every kind, not
only the ones rendered to disk. This module reads that record back; it never
recomputes the set, because doing so would need the KB index and the profile
YAML, neither of which is this call's business, and because the record is
supposed to describe what the checkout actually holds even after the profile
or the corpus has moved on since the last individuation.
"""
from __future__ import annotations

import json
from pathlib import Path

# Mirrors knowledge_ctx._PROFILE_DOCUMENTS_NAME. The two sit either side of the
# deployed-lib seam (bin/ tools import from ~/.local/lib), so a shared import
# would break every branch until the lib half merged — the same shape as
# worktrees._MANIFEST_NAME / knowledge_ctx._MANIFEST_NAME, and pinned equal by
# a test for the same reason.
_PROFILE_DOCUMENTS_NAME = ".profile-documents.json"


def _individuated_at(data: dict) -> object:
    """The record's timestamp under the new key, else the pre-0088 one."""
    return data.get("individuated_at", data.get("materialized_at"))


def _has_valid_shape(data: object) -> bool:
    """A record that parses as JSON is still `unreadable_record` unless it has
    exactly the shape `knowledge-ctx` writes: a string `profile`, a string
    `individuated_at` (or the pre-0088 `materialized_at`), and a `documents` list of `{id, kind}` string pairs."""
    if not isinstance(data, dict):
        return False
    if not isinstance(data.get("profile"), str):
        return False
    if not isinstance(_individuated_at(data), str):
        return False
    documents = data.get("documents")
    if not isinstance(documents, list):
        return False
    for doc in documents:
        if not isinstance(doc, dict):
            return False
        if not isinstance(doc.get("id"), str) or not isinstance(doc.get("kind"), str):
            return False
    return True


def session_documents(cwd: str | None) -> dict:
    """The knowledge documents individuated into `cwd`'s `.claude/`, or a
    reason there is no answer.

    `cwd` is the session's working directory, already resolved by the caller
    the same way `session-agent commands` resolves it — this module knows
    nothing about tmux or sessions, only about a directory's `.claude/`, so it
    stays testable against a constructed tree exactly like `session_commands`
    is.

    Never returns an empty `documents` list in place of a reason: an absent
    `cwd`, a missing `.active-profile` and a missing or malformed record are
    each a distinct `reason`, because each points a caller at a different fix
    (the session isn't resolvable / it hasn't been individuated in this repo /
    its knowledge-ctx predates this record / the record on disk is corrupt).
    """
    if not cwd:
        return {"reason": "no_such_session"}

    claude_dir = Path(cwd) / ".claude"
    if not (claude_dir / ".active-profile").is_file():
        return {"reason": "not_materialized"}

    record_path = claude_dir / _PROFILE_DOCUMENTS_NAME
    if not record_path.is_file():
        return {"reason": "no_document_record"}

    try:
        data = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"reason": "unreadable_record"}
    if not _has_valid_shape(data):
        return {"reason": "unreadable_record"}

    documents = [{"id": doc["id"], "kind": doc["kind"]} for doc in data["documents"]]
    return {
        "profile": data["profile"],
        "materialized_at": _individuated_at(data),
        "individuated_at": _individuated_at(data),
        "documents": sorted(documents, key=lambda d: d["id"]),
    }
