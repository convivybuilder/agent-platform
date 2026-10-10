"""Audience admission reads the Library's roster per decision (lib/knowledge/knowledge.py).

The indexer used to gate `audience` on a set literal read once at import by the
long-running knowledge-watcher, so a roster change did nothing until a restart and a
revocation kept admitting documents. These tests pin the replacement: the roster
comes from GET /roster and is cached for ROSTER_TTL_S. A name the Library does not
serve is refused. A roster that cannot be read defers the document, and the indexer
leaves its row as it is until a scan or event can read the roster. Nothing falls
back to a copy.

No test here reaches the Library. urlopen is replaced with a fake that serves the
body each test sets, and the conftest guard would refuse a real call anyway.

Each test drives the public result of the function it names, so on a checkout
without the roster read, or with the as-built refusal, it fails on its own
assertion line.
"""
from __future__ import annotations

import http.client
import io
import json
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import indexer
import knowledge
import signal_client

REPO = Path(__file__).resolve().parent.parent
SCHEMA = REPO / "lib" / "knowledge" / "schema.sql"

ALPHA = "projects/platform/reference/alpha.md"
BETA = "projects/platform/reference/beta.md"
GAMMA = "projects/platform/reference/gamma.md"

DOC = """---
id: platform/reference/{slug}
kind: reference
project: platform
title: A doc
status: current
audience: [architect]
last_reviewed: 2026-07-24
authors: [operator]
---

{body}
"""


def _ok_doc(**over):
    fm = {
        "id": "agent-platform/agents/coder",
        "kind": "agent",
        "project": "agent-platform",
        "title": "Coder",
        "status": "current",
    }
    fm.update(over)
    return fm


# conftest stubs the read for every test; this module tests the read itself, so it
# puts the real one back. None on a checkout without the read, where the assertions
# below then fail on their own lines.
_REAL_FETCH = getattr(knowledge, "_fetch_roster", None)


@pytest.fixture(autouse=True)
def _the_real_roster_read(monkeypatch):
    monkeypatch.setattr(knowledge, "_fetch_roster", _REAL_FETCH, raising=False)


class _Served:
    """What the fake Library answers. `body` is the response bytes; `error` makes
    urlopen raise instead; `cut` makes the body read fail mid-way, as a dropped
    connection does. `urls` records every URL requested."""

    def __init__(self):
        self.body = b""
        self.error = None
        self.cut = False
        self.urls = []

    def set_roster(self, *names):
        self.body = json.dumps({
            "agents": [{"id": i, "name": n} for i, n in enumerate(names, 1)],
            "count": len(names),
        }).encode()
        self.error = None
        self.cut = False

    def unreachable(self):
        self.error = urllib.error.URLError("connection refused")

    def cut_mid_body(self):
        self.cut = True


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _CutResponse(_Response):
    def read(self, *args):
        # What http.client raises when the peer closes before Content-Length is met.
        raise http.client.IncompleteRead(b'{"agents": [{"id": 1, "na')


@pytest.fixture
def served(monkeypatch):
    s = _Served()

    def fake_urlopen(req, timeout=None):
        s.urls.append(req.full_url)
        if s.error is not None:
            raise s.error
        if s.cut:
            return _CutResponse(s.body)
        return _Response(s.body)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(signal_client, "token", lambda: "test-token")
    monkeypatch.setattr(knowledge, "_roster_cache", None, raising=False)
    s.set_roster("coder", "operator")
    return s


class _Clock:
    now = 0.0

    def monotonic(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(knowledge, "time", c, raising=False)
    return c


def _past_the_staleness_bound(clock):
    clock.now += knowledge.ROSTER_TTL_S + 1


def test_admission_follows_the_roster_the_library_serves(served):
    served.set_roster("coder", "operator", "new-crew-agent")
    assert knowledge.validate_frontmatter(_ok_doc(audience=["new-crew-agent"])) == []


def test_the_roster_is_read_from_get_roster(served):
    knowledge.validate_frontmatter(_ok_doc(audience=["coder"]))
    assert len(served.urls) == 1 and served.urls[0].endswith("/roster"), served.urls


def test_a_removed_name_is_refused_once_the_staleness_bound_passes(served, clock):
    assert knowledge.validate_frontmatter(_ok_doc(audience=["coder"])) == []

    served.set_roster("operator")
    clock.now = 29.0
    assert knowledge.validate_frontmatter(_ok_doc(audience=["coder"])) == [], (
        "inside the 30 s bound the cached roster still answers")

    clock.now = 31.0
    errs = knowledge.validate_frontmatter(_ok_doc(audience=["coder"]))
    assert any("audience value not in roster: coder" in e for e in errs), errs


def test_an_unreachable_library_defers_the_decision(served):
    served.unreachable()
    with pytest.raises(knowledge.RosterUnavailable):
        knowledge.validate_frontmatter(_ok_doc(audience=["coder"]))


@pytest.mark.parametrize("body", [
    b"not json",
    b'["coder"]',
    b'{"agents": "coder"}',
    b'{"agents": ["coder"]}',
    b'{"agents": [{"id": 1}]}',
    b'{"agents": []}',
    b'{"agents": [{"id": 1, "name": ""}]}',
    b'{"agents": [{"id": 1, "name": "   "}]}',
])
def test_a_malformed_roster_answer_defers_the_decision(served, body):
    served.body = body
    with pytest.raises(knowledge.RosterUnavailable):
        knowledge.validate_frontmatter(_ok_doc(audience=["coder"]))


def test_a_body_cut_off_mid_read_defers_the_decision(served):
    served.cut_mid_body()
    with pytest.raises(knowledge.RosterUnavailable):
        knowledge.validate_frontmatter(_ok_doc(audience=["coder"]))


def test_binds_agents_defers_when_the_roster_is_unavailable(served):
    served.unreachable()
    fm = {"scope": "agent", "binds_crews": [], "binds_agents": ["coder"]}
    with pytest.raises(knowledge.RosterUnavailable):
        knowledge.instruction_block_errors(fm, "A rule.")


def test_binds_agents_refuses_a_person_during_an_outage_without_reading_the_roster(served):
    """A person is decided by NON_AGENT_AUDIENCE, which needs no roster, so the
    outage does not stand between the block and its refusal."""
    served.unreachable()
    fm = {"scope": "agent", "binds_crews": [], "binds_agents": [knowledge.OPERATOR_MARKER]}
    errs = knowledge.instruction_block_errors(fm, "A rule.")
    assert any("names a person or a group" in e for e in errs), errs


def test_no_credential_defers_the_decision(served, monkeypatch):
    monkeypatch.setattr(signal_client, "token", lambda: None)
    with pytest.raises(knowledge.RosterUnavailable):
        knowledge.validate_frontmatter(_ok_doc(audience=["coder"]))


def test_a_failed_read_is_remembered_for_the_bound_and_a_later_success_replaces_it(
        served, clock):
    served.unreachable()
    with pytest.raises(knowledge.RosterUnavailable):
        knowledge.validate_frontmatter(_ok_doc(audience=["coder"]))

    served.set_roster("coder")
    clock.now = 29.0
    with pytest.raises(knowledge.RosterUnavailable):
        knowledge.validate_frontmatter(_ok_doc(audience=["coder"]))
    assert len(served.urls) == 1, "inside the bound the failure answers without a request"

    _past_the_staleness_bound(clock)
    assert knowledge.validate_frontmatter(_ok_doc(audience=["coder"])) == []
    assert len(served.urls) == 2, "past the bound the Library is asked again"


# --- the indexer: an outage defers a document, it does not refuse it -----------
#
# Each scan below first runs with the Library readable, then with it unreachable or
# malformed. The staleness bound is passed with the fake clock so each scan reads the
# Library rather than the cache.


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    root = tmp_path / "knowledge"
    (root / "projects" / "platform" / "reference").mkdir(parents=True)
    monkeypatch.setattr(indexer, "KNOWLEDGE", root)
    monkeypatch.setattr(indexer, "PROJECTS", root / "projects")
    monkeypatch.setattr(indexer, "DB", tmp_path / "index.db")
    monkeypatch.setattr(indexer, "SCHEMA", SCHEMA)
    indexer.ensure_db()
    return root


def _doc(root: Path, rel: str, body: str = "Body text.") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DOC.format(slug=path.stem, body=body))
    return path


def _rows() -> dict:
    conn = sqlite3.connect(str(indexer.DB))
    try:
        return {r[0]: r[1] for r in
                conn.execute("SELECT source_ref, body_hash FROM docs").fetchall()}
    finally:
        conn.close()


def _snapshot() -> list:
    """Every column of every indexed file row, so any change to a row shows."""
    conn = sqlite3.connect(str(indexer.DB))
    try:
        return sorted(conn.execute(
            "SELECT * FROM docs WHERE source = 'files'").fetchall())
    finally:
        conn.close()


def _deferred_refs(res: dict) -> list[str]:
    return [rel for rel, _ in res.get("deferred", [])]


def _refused_refs(res: dict) -> list[str]:
    return [rel for rel, _ in res.get("refused", [])]


def _ids_at(ref: str) -> list[str]:
    conn = sqlite3.connect(str(indexer.DB))
    try:
        return [r[0] for r in conn.execute(
            "SELECT id FROM docs WHERE source = 'files' AND source_ref = ?", (ref,))]
    finally:
        conn.close()


class _Walk:
    """Stands in for `indexer.PROJECTS`, so the walk yields only the paths given. A
    case-only rename on a case-insensitive volume leaves the old spelling resolving
    under `is_file()` while the walk yields only the new spelling. Linux CI cannot make
    that happen with real files, so the walk is what changes here."""

    def __init__(self, paths):
        self.paths = list(paths)

    def rglob(self, pattern):
        return iter(self.paths)


OLD_SPELLING = "projects/platform/reference/Alpha.md"


def _case_only_rename_during_an_outage(corpus, served, clock, monkeypatch) -> None:
    served.set_roster("architect")
    (corpus / OLD_SPELLING).write_text(DOC.format(slug="alpha", body="Body text."))
    indexer.full_scan_and_reconcile()
    assert OLD_SPELLING in _rows()

    # The same document under the new spelling, with the same id. Both spellings exist on
    # disk, so `is_file()` on the old one is True, as it is after the rename on APFS.
    new = corpus / ALPHA
    new.write_text(DOC.format(slug="alpha", body="Body text."))
    served.unreachable()
    _past_the_staleness_bound(clock)
    monkeypatch.setattr(indexer, "PROJECTS", _Walk([new]))


def test_an_outage_keeps_an_indexed_audience_doc_exactly_as_it_is(corpus, served, clock):
    served.set_roster("architect")
    _doc(corpus, ALPHA)
    indexer.full_scan_and_reconcile()
    before = _snapshot()
    assert ALPHA in _rows()

    served.unreachable()
    _past_the_staleness_bound(clock)
    res = indexer.full_scan_and_reconcile()

    assert _snapshot() == before, "an outage must not drop, update or add a row"
    assert res["removed"] == 0 and res["removed_refs"] == []
    assert _deferred_refs(res) == [ALPHA]


def test_a_new_audience_doc_is_admitted_on_the_first_scan_after_the_roster_is_readable(
        corpus, served, clock):
    served.set_roster("architect")
    served.unreachable()
    _doc(corpus, BETA)
    res = indexer.full_scan_and_reconcile()

    assert BETA not in _rows(), "nothing new is admitted during an outage"
    assert _deferred_refs(res) == [BETA]

    served.set_roster("architect")
    _past_the_staleness_bound(clock)
    indexer.full_scan_and_reconcile()
    assert BETA in _rows()


def test_a_changed_indexed_doc_keeps_its_old_row_during_an_outage(corpus, served, clock):
    served.set_roster("architect")
    _doc(corpus, ALPHA, body="First.")
    indexer.full_scan_and_reconcile()
    old_hash = _rows()[ALPHA]

    _doc(corpus, ALPHA, body="Second.")
    served.unreachable()
    _past_the_staleness_bound(clock)
    res = indexer.full_scan_and_reconcile()

    assert _rows().get(ALPHA) == old_hash, "the old row stays until the roster is readable"
    assert _deferred_refs(res) == [ALPHA]

    served.set_roster("architect")
    _past_the_staleness_bound(clock)
    indexer.full_scan_and_reconcile()
    assert _rows().get(ALPHA) != old_hash, "the first readable scan takes the new content"


def test_the_event_path_defers_a_changed_doc_and_keeps_its_row(corpus, served, clock):
    served.set_roster("architect")
    path = _doc(corpus, ALPHA, body="First.")
    assert indexer.index_one(path) == ("upserted", None)
    old_hash = _rows()[ALPHA]

    _doc(corpus, ALPHA, body="Second.")
    served.unreachable()
    _past_the_staleness_bound(clock)
    action, err = indexer.index_one(path)

    assert action == "deferred", (action, err)
    assert "connection refused" in err, err
    assert _rows().get(ALPHA) == old_hash


def test_the_event_path_does_not_admit_a_new_doc_during_an_outage(corpus, served):
    served.unreachable()
    path = _doc(corpus, BETA)
    action, _ = indexer.index_one(path)
    assert action == "deferred", action
    assert BETA not in _rows()


def test_a_name_the_library_no_longer_serves_is_refused_and_pruned_on_a_full_scan(
        corpus, served, clock):
    served.set_roster("architect")
    _doc(corpus, ALPHA)
    indexer.full_scan_and_reconcile()
    assert ALPHA in _rows()

    served.set_roster("operator")
    _past_the_staleness_bound(clock)
    res = indexer.full_scan_and_reconcile()

    assert ALPHA not in _rows()
    assert [rel for rel, _ in res["invalid"]] == [ALPHA]
    assert res["removed_refs"] == [ALPHA]


@pytest.mark.parametrize("body", [
    b"not json",
    b'{"agents": "architect"}',
    b'{"agents": [{"id": 1}]}',
    b'{"agents": []}',
    b'{"agents": [{"id": 1, "name": ""}]}',
])
def test_a_malformed_roster_defers_an_outage_scan_like_an_unreachable_library(
        corpus, served, clock, body):
    served.set_roster("architect")
    _doc(corpus, ALPHA)
    indexer.full_scan_and_reconcile()
    before = _snapshot()

    served.body = body
    _past_the_staleness_bound(clock)
    res = indexer.full_scan_and_reconcile()

    assert _snapshot() == before, "the indexed row survives an unusable roster"
    assert _deferred_refs(res) == [ALPHA]
    assert res.get("invalid") == [], res


def test_a_body_cut_off_mid_read_defers_an_outage_scan_and_keeps_the_row(
        corpus, served, clock):
    served.set_roster("architect")
    _doc(corpus, ALPHA)
    indexer.full_scan_and_reconcile()
    before = _snapshot()

    served.cut_mid_body()
    _past_the_staleness_bound(clock)
    res = indexer.full_scan_and_reconcile()

    assert _snapshot() == before
    assert _deferred_refs(res) == [ALPHA]
    assert res.get("invalid") == [], res


def test_an_outage_scan_over_many_audience_docs_asks_the_library_once(corpus, served, clock):
    served.set_roster("architect")
    slugs = [f"doc{i}" for i in range(5)]
    for slug in slugs:
        _doc(corpus, f"projects/platform/reference/{slug}.md")
    indexer.full_scan_and_reconcile()
    assert len(served.urls) == 1

    served.unreachable()
    _past_the_staleness_bound(clock)
    res = indexer.full_scan_and_reconcile()

    assert len(res["deferred"]) == len(slugs), res
    assert len(served.urls) == 2, (
        f"one failed read covers the whole scan; the scan made {len(served.urls) - 1}")


def test_an_outage_keeps_an_indexed_doc_whose_id_is_broken_until_the_roster_reads(
        corpus, served, clock):
    """The file's id is edited to one the validator refuses. During an outage the refusal is
    deferred and the row stays, as for every refusal. The first readable scan refuses the
    file and removes the row."""
    served.set_roster("architect")
    path = _doc(corpus, ALPHA)
    indexer.full_scan_and_reconcile()
    assert ALPHA in _rows()

    path.write_text(DOC.format(slug="alpha", body="Body text.")
                    .replace("id: platform/reference/alpha", "id: broken"))
    served.unreachable()
    _past_the_staleness_bound(clock)
    res = indexer.full_scan_and_reconcile()

    assert _ids_at(ALPHA) == ["platform/reference/alpha"], "the outage keeps the old row"
    assert _refused_refs(res) == [ALPHA], res
    assert "invalid id format" in res["refused"][0][1], res
    assert res["deferred"] == [] and res["invalid"] == [], res

    served.set_roster("architect")
    _past_the_staleness_bound(clock)
    res = indexer.full_scan_and_reconcile()
    assert ALPHA not in _rows(), "the first readable scan removes the refused document's row"
    assert [rel for rel, _ in res["invalid"]] == [ALPHA], res


def test_a_write_with_a_broken_id_during_an_outage_reports_the_id_not_the_roster(
        corpus, served):
    served.unreachable()
    text = DOC.format(slug="gamma", body="x").replace("id: platform/reference/gamma",
                                                       "id: broken")
    errs = indexer.document_errors(text, GAMMA)
    assert any("invalid id format" in e for e in errs), errs
    assert not any("cannot check the Library's roster" in e for e in errs), errs


def test_verify_index_during_an_outage_neither_fails_nor_calls_an_indexed_doc_extra(
        corpus, served, clock):
    served.set_roster("architect")
    _doc(corpus, ALPHA)
    indexer.full_scan_and_reconcile()

    served.unreachable()
    _past_the_staleness_bound(clock)
    v = indexer.verify_index()

    assert v["extra"] == [], v
    assert v.get("deferred") == [ALPHA], v


def test_a_write_the_index_cannot_check_is_refused_with_its_cause(corpus, served):
    served.unreachable()
    errs = indexer.document_errors(DOC.format(slug="gamma", body="x"), GAMMA)
    assert len(errs) == 1 and "cannot check the Library's roster" in errs[0], errs


BLOCK = "projects/platform/instruction-blocks/one.md"
BLOCK_TEXT = """---
id: platform/instruction-blocks/one
kind: instruction-block
project: platform
title: One
status: current
scope: agent
audience: [architect]
binds_agents: [coder]
last_reviewed: 2026-09-16
authors: [library]
---

Body text.
"""


def test_a_write_checks_both_roster_fields_against_the_one_read_its_validation_starts_with(
        corpus, served, clock, monkeypatch):
    """`audience` and `binds_agents` are both checked against the roster. The Library
    fails after the validation's first read, and the second check still passes on that
    read, so the write is not refused by a read the cache bound let it make."""
    served.set_roster("architect", "coder")
    _library_changes_after_one_read(monkeypatch, clock, served, served.unreachable)

    errs = indexer.document_errors(BLOCK_TEXT, BLOCK)

    assert errs == [], errs
    assert len(served.urls) == 1, served.urls


def test_a_doc_moved_during_an_outage_keeps_its_row_until_the_roster_reads(
        corpus, served, clock):
    """A moved document is deferred under its new path. Its row under the old path
    stays with its content until the roster reads, and the first readable scan moves it
    to the new path, leaving one row. A removed document is kept too: its file is gone,
    but nothing is refused, so its row goes only when the roster reads."""
    served.set_roster("architect")
    _doc(corpus, ALPHA)
    _doc(corpus, GAMMA)
    indexer.full_scan_and_reconcile()
    before = _snapshot()

    (corpus / ALPHA).rename(corpus / BETA)
    (corpus / GAMMA).unlink()
    served.unreachable()
    _past_the_staleness_bound(clock)
    res = indexer.full_scan_and_reconcile()

    assert _snapshot() == before, "an outage keeps every indexed row, moved or removed"
    assert BETA not in _rows(), "nothing is admitted at the new path during an outage"
    assert _deferred_refs(res) == [BETA], res
    assert res["removed_refs"] == [], res
    assert indexer.verify_index()["extra"] == []

    served.set_roster("architect")
    _past_the_staleness_bound(clock)
    indexer.full_scan_and_reconcile()
    assert [r[2] for r in _snapshot()] == [BETA], "the first readable scan leaves one row, at the new path"


def test_a_rename_in_one_kb_sync_pass_keeps_the_row_during_an_outage(corpus, served, clock):
    """`kb sync` hands `index_paths` the removed path and the added path of a rename in
    one list, in no particular order. The row must survive whichever comes first."""
    served.set_roster("architect")
    _doc(corpus, ALPHA)
    indexer.full_scan_and_reconcile()

    (corpus / ALPHA).rename(corpus / BETA)
    served.unreachable()
    _past_the_staleness_bound(clock)
    res = indexer.index_paths([ALPHA, BETA])

    assert ALPHA in _rows(), "the old path's row survives the pass that deferred its new path"
    assert BETA not in _rows()
    assert res["removed"] == 0, res
    assert sorted(_deferred_refs(res)) == [ALPHA, BETA], res

    served.set_roster("architect")
    _past_the_staleness_bound(clock)
    indexer.index_paths([ALPHA, BETA])
    assert [r[2] for r in _snapshot()] == [BETA]


def test_a_targeted_pass_keeps_a_removed_docs_row_during_an_outage(corpus, served, clock):
    """`kb sync`'s targeted pass follows the full scan's outage rule: a removed file with no
    document deferred under its id keeps its row while the roster cannot be read, and the
    first readable pass removes it."""
    served.set_roster("architect")
    _doc(corpus, GAMMA)
    indexer.full_scan_and_reconcile()
    before = _snapshot()
    assert GAMMA in _rows()

    (corpus / GAMMA).unlink()
    served.unreachable()
    _past_the_staleness_bound(clock)
    res = indexer.index_paths([GAMMA])

    assert _snapshot() == before, "an outage keeps a removed document's row"
    assert res["removed"] == 0, res
    assert _deferred_refs(res) == [GAMMA], res

    served.set_roster("architect")
    _past_the_staleness_bound(clock)
    indexer.index_paths([GAMMA])
    assert GAMMA not in _rows(), "the first readable pass removes the row"


def test_verify_index_during_a_move_outage_does_not_call_the_kept_row_extra(
        corpus, served, clock):
    """Without this, `kb sync` would report the kept row as extra, heal with a full scan
    that cannot clear it, and print STILL INCONSISTENT on every pass of the outage."""
    served.set_roster("architect")
    _doc(corpus, ALPHA)
    indexer.full_scan_and_reconcile()

    (corpus / ALPHA).rename(corpus / BETA)
    served.unreachable()
    _past_the_staleness_bound(clock)
    v = indexer.verify_index()

    assert v["extra"] == [], v
    assert v["deferred"] == [BETA], v


def test_a_refused_file_sharing_a_deferred_id_keeps_its_row_during_an_outage(
        corpus, served, clock):
    """ALPHA is still on disk and now fails a check the roster does not decide (status). BETA,
    with the same id, is deferred in the same scan. Both are outage outcomes, so ALPHA's row
    stays and `verify_index` reports no extra row. The first readable scan refuses ALPHA,
    admits BETA under the shared id, and removes ALPHA's row."""
    served.set_roster("architect")
    alpha = _doc(corpus, ALPHA)
    indexer.full_scan_and_reconcile()
    assert ALPHA in _rows()

    alpha.write_text(DOC.format(slug="alpha", body="Body text.")
                     .replace("status: current", "status: bogus"))
    (corpus / BETA).write_text(DOC.format(slug="alpha", body="Body text."))
    served.unreachable()
    _past_the_staleness_bound(clock)
    v = indexer.verify_index()
    assert v["extra"] == [] and ALPHA in v["refused"] and v["deferred"] == [BETA], v

    before = _snapshot()
    res = indexer.full_scan_and_reconcile()
    assert _snapshot() == before, "a refused file's row stays though a deferred file shares its id"
    assert _refused_refs(res) == [ALPHA] and _deferred_refs(res) == [BETA], res
    assert res["invalid"] == [] and res["removed"] == 0, res

    served.set_roster("architect")
    _past_the_staleness_bound(clock)
    res = indexer.full_scan_and_reconcile()
    assert ALPHA not in _rows(), "the first readable scan removes the refused file's row"
    assert [rel for rel, _ in res["invalid"]] == [ALPHA], res


def _refused_row_at_entry(entry: str, path: Path) -> list[str]:
    """Runs one entry point over a refused file during an outage, and returns the paths it
    reports as refused. `verify_index` must also report no extra row, and no deferral."""
    if entry == "full_scan":
        res = indexer.full_scan_and_reconcile()
        assert res["deferred"] == [], res
        return _refused_refs(res)
    if entry == "index_paths":
        res = indexer.index_paths([ALPHA])
        assert res["deferred"] == [], res
        return _refused_refs(res)
    if entry == "index_one":
        action, _ = indexer.index_one(path)
        return [ALPHA] if action == "refused" else []
    v = indexer.verify_index()
    assert v["extra"] == [] and v["deferred"] == [], v
    return v["refused"]


@pytest.mark.parametrize("entry", ["full_scan", "index_paths", "index_one", "verify_index"])
def test_a_refused_document_keeps_its_row_at_every_entry_point_during_an_outage(
        corpus, served, clock, entry):
    """ALPHA is edited to fail a check the roster does not decide, under its own id. Each
    entry point keeps the row during the outage and reports the refusal as refused, not
    deferred and not invalid. The first readable scan removes the row."""
    served.set_roster("architect")
    path = _doc(corpus, ALPHA)
    indexer.full_scan_and_reconcile()
    before = _snapshot()

    path.write_text(DOC.format(slug="alpha", body="Body text.")
                    .replace("status: current", "status: bogus"))
    served.unreachable()
    _past_the_staleness_bound(clock)
    assert _refused_row_at_entry(entry, path) == [ALPHA]
    assert _snapshot() == before, "an outage keeps a refused document's row"

    served.set_roster("architect")
    _past_the_staleness_bound(clock)
    indexer.full_scan_and_reconcile()
    assert ALPHA not in _rows(), "the first readable scan removes the refused document's row"


def test_a_case_only_rename_during_an_outage_keeps_the_row(corpus, served, clock, monkeypatch):
    """The walk yields only the new spelling, and the old spelling is not refused, so its
    row stays. The rule never asks whether the stored spelling still resolves, which it
    does on a case-insensitive volume after a case-only rename."""
    _case_only_rename_during_an_outage(corpus, served, clock, monkeypatch)
    res = indexer.full_scan_and_reconcile()

    assert OLD_SPELLING in _rows(), "the old spelling's row stays while the roster is unreadable"
    assert _deferred_refs(res) == [ALPHA], res


def test_verify_index_during_a_case_only_rename_outage_does_not_call_the_kept_row_extra(
        corpus, served, clock, monkeypatch):
    _case_only_rename_during_an_outage(corpus, served, clock, monkeypatch)
    v = indexer.verify_index()

    assert v["extra"] == [], v
    assert v["deferred"] == [ALPHA], v


def test_an_outage_keeps_an_unstorable_indexed_doc_until_the_roster_reads(
        corpus, served, clock):
    """`stored_json` needs no roster, so a document it cannot store is refused during an
    outage, even though it names an audience the roster would have to decide. The refusal
    is deferred, and the row stays until the roster reads."""
    served.set_roster("architect")
    path = _doc(corpus, ALPHA)
    indexer.full_scan_and_reconcile()
    assert ALPHA in _rows()

    # A mapping with int and str keys: `json.dumps(sort_keys=True)` raises TypeError on it.
    text = DOC.format(slug="alpha", body="Body text.").replace(
        "title: A doc", "title: A doc\nx: {1: a, b: c}")
    path.write_text(text)
    served.unreachable()
    _past_the_staleness_bound(clock)
    errs = indexer.document_errors(text, ALPHA)
    assert any("frontmatter cannot be stored" in e for e in errs), errs
    res = indexer.full_scan_and_reconcile()

    assert ALPHA in _rows(), "a document that cannot be stored keeps its row during an outage"
    assert _refused_refs(res) == [ALPHA], res
    assert "frontmatter cannot be stored" in res["refused"][0][1], res
    assert res["invalid"] == [], res

    served.set_roster("architect")
    _past_the_staleness_bound(clock)
    res = indexer.full_scan_and_reconcile()
    assert ALPHA not in _rows(), "the first readable scan removes it"
    assert [rel for rel, _ in res["invalid"]] == [ALPHA], res


def test_a_full_scan_after_an_id_change_leaves_one_row_for_the_path(corpus, served):
    served.set_roster("architect")
    path = _doc(corpus, ALPHA)
    indexer.full_scan_and_reconcile()
    assert _ids_at(ALPHA) == ["platform/reference/alpha"]

    path.write_text(DOC.format(slug="alpha", body="Body text.")
                    .replace("id: platform/reference/alpha", "id: platform/reference/renamed"))
    indexer.full_scan_and_reconcile()

    assert _ids_at(ALPHA) == ["platform/reference/renamed"], _ids_at(ALPHA)


def test_verify_index_flags_a_row_whose_id_the_file_no_longer_carries(corpus, served):
    served.set_roster("architect")
    path = _doc(corpus, ALPHA)
    indexer.full_scan_and_reconcile()

    path.write_text(DOC.format(slug="alpha", body="Body text.")
                    .replace("id: platform/reference/alpha", "id: platform/reference/renamed"))
    v = indexer.verify_index()

    assert v["ok"] is False, v
    assert ALPHA in v["extra"] and ALPHA in v["missing"], v


def test_a_path_whose_id_changed_during_an_outage_keeps_the_old_row(corpus, served, clock):
    """The file at ALPHA now carries another id and is deferred. The walk did not refuse
    ALPHA, so the row under the old id stays until the roster reads, and the first
    readable scan replaces it with the row under the new id."""
    served.set_roster("architect")
    path = _doc(corpus, ALPHA)
    indexer.full_scan_and_reconcile()
    assert _ids_at(ALPHA) == ["platform/reference/alpha"]

    path.write_text(DOC.format(slug="alpha", body="Body text.")
                    .replace("id: platform/reference/alpha", "id: platform/reference/renamed"))
    served.unreachable()
    _past_the_staleness_bound(clock)
    res = indexer.full_scan_and_reconcile()

    assert _ids_at(ALPHA) == ["platform/reference/alpha"], "the old row stays during the outage"
    assert _deferred_refs(res) == [ALPHA], res
    assert indexer.verify_index()["extra"] == []

    served.set_roster("architect")
    _past_the_staleness_bound(clock)
    indexer.full_scan_and_reconcile()
    assert _ids_at(ALPHA) == ["platform/reference/renamed"], _ids_at(ALPHA)


@pytest.mark.parametrize("z_needs_roster", [True, False],
                         ids=["z-needs-the-roster", "z-needs-no-roster"])
def test_an_outage_keeps_a_row_whose_path_a_new_document_takes(
        corpus, served, clock, z_needs_roster):
    """X is indexed at ALPHA and moves to BETA during the outage, so BETA is deferred. A
    new document Z is written at ALPHA. The walk reaches ALPHA and does not refuse it, so
    X's row stays at ALPHA until the roster reads. Z is admitted when it needs no roster
    and deferred when it does."""
    served.set_roster("architect")
    _doc(corpus, ALPHA)
    indexer.full_scan_and_reconcile()
    x_before = [r for r in _snapshot() if r[0] == "platform/reference/alpha"]
    assert len(x_before) == 1

    (corpus / ALPHA).rename(corpus / BETA)
    zeta = DOC.format(slug="zeta", body="Body text.")
    if not z_needs_roster:
        zeta = zeta.replace("audience: [architect]\n", "")
    (corpus / ALPHA).write_text(zeta)
    served.unreachable()
    _past_the_staleness_bound(clock)
    res = indexer.full_scan_and_reconcile()

    assert [r for r in _snapshot() if r[0] == "platform/reference/alpha"] == x_before, (
        "X's row stays exactly as it was, at ALPHA")
    expected = [BETA] if not z_needs_roster else sorted([ALPHA, BETA])
    assert sorted(_deferred_refs(res)) == expected, res
    assert indexer.verify_index()["extra"] == []

    served.set_roster("architect")
    _past_the_staleness_bound(clock)
    indexer.full_scan_and_reconcile()
    assert _ids_at(ALPHA) == ["platform/reference/zeta"], _ids_at(ALPHA)
    assert _ids_at(BETA) == ["platform/reference/alpha"], _ids_at(BETA)
    assert len(_snapshot()) == 2


def test_a_dangling_symlink_at_an_indexed_path_keeps_its_row_until_the_roster_reads(
        corpus, served, clock):
    """The walk yields the dangling symlink and refuses it at its own path. During an outage
    the refusal is reported as refused and the row stays. The first readable scan removes it."""
    served.set_roster("architect")
    _doc(corpus, ALPHA)
    indexer.full_scan_and_reconcile()
    assert ALPHA in _rows()

    (corpus / ALPHA).unlink()
    (corpus / ALPHA).symlink_to(corpus / "projects" / "platform" / "reference" / "nowhere.md")
    served.unreachable()
    _past_the_staleness_bound(clock)
    res = indexer.full_scan_and_reconcile()

    assert ALPHA in _rows(), "a dangling symlink keeps its row during an outage"
    assert _refused_refs(res) == [ALPHA] and res["deferred"] == [], res
    assert res["invalid"] == [], res

    served.set_roster("architect")
    _past_the_staleness_bound(clock)
    indexer.full_scan_and_reconcile()
    assert ALPHA not in _rows(), "the first readable scan removes the row"


def test_a_document_that_needs_no_roster_is_upserted_during_an_outage(corpus, served):
    served.unreachable()
    plain = "projects/platform/reference/plain.md"
    (corpus / plain).write_text(
        DOC.format(slug="plain", body="Body text.").replace("audience: [architect]\n", ""))
    res = indexer.full_scan_and_reconcile()

    assert _ids_at(plain) == ["platform/reference/plain"], res
    assert res["deferred"] == [], res


def test_a_no_roster_document_moved_onto_the_source_id_repoints_its_row_during_an_outage(
        corpus, served, clock):
    """The one write an outage allows to an indexed row. A document that needs no roster,
    carrying the indexed row's `id`, is upserted at its new path, so the row's `source_ref`
    moves there. Nothing is deleted, and the row keeps its id. This pins behaviour that
    predates the round, so it passes on the head it was written against."""
    served.set_roster("architect")
    _doc(corpus, ALPHA)
    indexer.full_scan_and_reconcile()
    (corpus / ALPHA).unlink()
    (corpus / BETA).write_text(
        DOC.format(slug="alpha", body="Body text.").replace("audience: [architect]\n", ""))
    served.unreachable()
    _past_the_staleness_bound(clock)

    res = indexer.full_scan_and_reconcile()

    assert _ids_at(BETA) == ["platform/reference/alpha"], res
    assert _ids_at(ALPHA) == [], res
    assert res["deferred"] == [], res


def _library_changes_after_one_read(monkeypatch, clock, served, change) -> None:
    """Runs `change` once, after the pass's first GET /roster has been answered or has
    failed, and moves the clock past the staleness bound. A pass that reads again now
    reaches the Library, and `served.urls` shows the read."""
    real = urllib.request.urlopen

    def once(req, timeout=None):
        try:
            return real(req, timeout=timeout)
        finally:
            if len(served.urls) == 1:
                change()
                _past_the_staleness_bound(clock)

    monkeypatch.setattr(urllib.request, "urlopen", once)


def _run_pass(run_pass: str, rels: list[str]) -> dict:
    if run_pass == "full_scan":
        return indexer.full_scan_and_reconcile()
    return indexer.index_paths(rels)


@pytest.mark.parametrize("run_pass", ["full_scan", "index_paths"])
def test_a_pass_whose_library_fails_after_its_first_read_defers_nothing(
        corpus, served, clock, monkeypatch, run_pass):
    """A pass reads the roster once, at its start. X moves from ALPHA to BETA, and the
    Library goes down after that read. BETA is admitted on the start read and takes X's
    row, so nothing is deferred and the pass makes no second read."""
    served.set_roster("architect")
    _doc(corpus, ALPHA)
    indexer.full_scan_and_reconcile()
    (corpus / ALPHA).rename(corpus / BETA)
    served.urls.clear()
    _past_the_staleness_bound(clock)
    _library_changes_after_one_read(monkeypatch, clock, served, served.unreachable)

    res = _run_pass(run_pass, [ALPHA, BETA])

    assert res["deferred"] == [], res
    assert _ids_at(BETA) == ["platform/reference/alpha"], res
    assert _ids_at(ALPHA) == [], "the old row goes because the document moved"
    assert len(served.urls) == 1, served.urls


def test_verify_index_whose_library_fails_after_its_first_read_defers_nothing(
        corpus, served, clock, monkeypatch):
    served.set_roster("architect")
    _doc(corpus, ALPHA)
    indexer.full_scan_and_reconcile()
    (corpus / ALPHA).rename(corpus / BETA)
    served.urls.clear()
    _past_the_staleness_bound(clock)
    _library_changes_after_one_read(monkeypatch, clock, served, served.unreachable)

    v = indexer.verify_index()

    assert v["deferred"] == [], v
    assert v["missing"] == [BETA], v
    assert v["extra"] == [ALPHA], "with the start read, X's old row is stale, not kept"
    assert len(served.urls) == 1, served.urls


@pytest.mark.parametrize("run_pass", ["full_scan", "index_paths"])
def test_a_pass_that_finds_the_library_down_at_its_start_stays_in_outage(
        corpus, served, clock, monkeypatch, run_pass):
    """The start read fails and the Library answers later in the pass. The pass is in
    outage mode from its start, so BETA and the new GAMMA are deferred, and X keeps its
    row at ALPHA. The later answer changes nothing."""
    served.set_roster("architect")
    _doc(corpus, ALPHA)
    indexer.full_scan_and_reconcile()
    (corpus / ALPHA).rename(corpus / BETA)
    _doc(corpus, GAMMA)
    served.unreachable()
    served.urls.clear()
    _past_the_staleness_bound(clock)
    _library_changes_after_one_read(
        monkeypatch, clock, served, lambda: served.set_roster("architect"))

    res = _run_pass(run_pass, [ALPHA, BETA, GAMMA])

    assert {BETA, GAMMA} <= set(_deferred_refs(res)), res
    assert _ids_at(ALPHA) == ["platform/reference/alpha"], "the outage keeps X's row"
    assert _ids_at(BETA) == [] and _ids_at(GAMMA) == [], "nothing is admitted in an outage"
    assert len(served.urls) == 1, served.urls


def test_verify_index_that_finds_the_library_down_at_its_start_defers_every_roster_document(
        corpus, served, clock, monkeypatch):
    served.set_roster("architect")
    _doc(corpus, ALPHA)
    indexer.full_scan_and_reconcile()
    (corpus / ALPHA).rename(corpus / BETA)
    _doc(corpus, GAMMA)
    served.unreachable()
    served.urls.clear()
    _past_the_staleness_bound(clock)
    _library_changes_after_one_read(
        monkeypatch, clock, served, lambda: served.set_roster("architect"))

    v = indexer.verify_index()

    assert sorted(v["deferred"]) == [BETA, GAMMA], v
    assert v["extra"] == [] and v["missing"] == [], v
    assert len(served.urls) == 1, served.urls


def test_an_outage_refusal_keeps_the_row_of_a_path_whose_refused_document_has_another_id(
        corpus, served, clock):
    """X is indexed at ALPHA and moves to BETA, which is deferred. Z is written at ALPHA
    with its own id and refused on status, a check the roster does not decide. Z's refusal
    carries Z's id, not X's, so X's row stays until the roster reads."""
    served.set_roster("architect")
    _doc(corpus, ALPHA)
    indexer.full_scan_and_reconcile()

    (corpus / ALPHA).rename(corpus / BETA)
    (corpus / ALPHA).write_text(DOC.format(slug="zeta", body="Body text.")
                                .replace("status: current", "status: bogus"))
    served.unreachable()
    _past_the_staleness_bound(clock)
    res = indexer.full_scan_and_reconcile()

    assert _refused_refs(res) == [ALPHA] and _deferred_refs(res) == [BETA], res
    assert res["invalid"] == [], res
    assert _ids_at(ALPHA) == ["platform/reference/alpha"], "Z's refusal does not remove X's row"
    assert indexer.verify_index()["extra"] == []

    served.set_roster("architect")
    _past_the_staleness_bound(clock)
    indexer.full_scan_and_reconcile()
    assert _ids_at(ALPHA) == [], "the first readable scan has no row at ALPHA"
    assert _ids_at(BETA) == ["platform/reference/alpha"]


def test_an_outage_keeps_the_row_of_a_refused_file_whose_frontmatter_cannot_be_read(
        corpus, served, clock):
    """A refused file whose frontmatter cannot be read is refused during an outage, and its
    row stays. The first readable scan removes the row."""
    served.set_roster("architect")
    path = _doc(corpus, ALPHA)
    indexer.full_scan_and_reconcile()

    path.write_text("---\nid: [unclosed\n---\n\nBody text.\n")
    served.unreachable()
    _past_the_staleness_bound(clock)
    res = indexer.full_scan_and_reconcile()

    assert _refused_refs(res) == [ALPHA], res
    assert res["invalid"] == [], res
    assert _ids_at(ALPHA) == ["platform/reference/alpha"], "the outage keeps the row"

    served.set_roster("architect")
    _past_the_staleness_bound(clock)
    indexer.full_scan_and_reconcile()
    assert _ids_at(ALPHA) == [], "the first readable scan removes it"


def test_a_targeted_pass_refuses_a_dangling_symlink_and_keeps_its_row_during_an_outage(
        corpus, served, clock):
    """The targeted pass walks a dangling link as the full scan does: it is refused at its
    own path, so the reason names the symlink, and its row stays until the roster reads."""
    served.set_roster("architect")
    _doc(corpus, ALPHA)
    indexer.full_scan_and_reconcile()

    (corpus / ALPHA).unlink()
    (corpus / ALPHA).symlink_to(corpus / "projects" / "platform" / "reference" / "nowhere.md")
    served.unreachable()
    _past_the_staleness_bound(clock)
    res = indexer.index_paths([ALPHA])

    assert ALPHA in _rows(), "the outage keeps the row of a refused link"
    assert _refused_refs(res) == [ALPHA] and res["deferred"] == [], res
    assert "symlink" in res["refused"][0][1], res

    served.set_roster("architect")
    _past_the_staleness_bound(clock)
    indexer.index_paths([ALPHA])
    assert ALPHA not in _rows(), "the first readable pass removes it"
