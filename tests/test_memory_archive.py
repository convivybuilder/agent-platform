"""Tests for `memory_archive` (`platform/decisions/0089`) — the scheduled job
that moves each co's cold `reference`/`project` memories into the Archives
and never a `feedback` memory.

Every network seam (`poster`, `searcher`, `blob_getter`) is a fake function
passed in directly; nothing here makes a real HTTP call.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
from pathlib import Path

import memory_archive as MA
import operator_config

NOW = time.time()
OLD = NOW - 40 * 86400   # 40 days ago: cold by mtime
RECENT = NOW - 5 * 86400  # 5 days ago: not cold by mtime


def _memory_text(name: str, mtype: str, *, description: str = "a description",
                 body: str = "The body of the memory.", top_level_type: bool = False) -> str:
    if top_level_type:
        type_block = f"type: {mtype}\n"
    else:
        type_block = f"metadata:\n  type: {mtype}\n"
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"{type_block}"
        "---\n\n"
        f"{body}\n"
    )


def _write_memory(memory_dir: Path, name: str, mtype: str, *, mtime: float,
                  **kw) -> Path:
    memory_dir.mkdir(parents=True, exist_ok=True)
    path = memory_dir / f"{name}.md"
    path.write_text(_memory_text(name, mtype, **kw), encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


# --- is_excluded_slug / crew_homes -------------------------------------------


_EXCLUDED = frozenset({"private-a", "private-b", "private-c", "private-d",
                       "private-e", "private-f", "private-g"})


def test_a_subcrew_of_an_excluded_crew_is_excluded():
    assert MA.is_excluded_slug("private-a--car", _EXCLUDED)
    assert MA.is_excluded_slug("private-b--anything", _EXCLUDED)
    assert MA.is_excluded_slug("private-c", _EXCLUDED)
    assert MA.is_excluded_slug("private-d", _EXCLUDED)
    assert MA.is_excluded_slug("private-e", _EXCLUDED)
    assert MA.is_excluded_slug("private-f", _EXCLUDED)
    assert MA.is_excluded_slug("private-g", _EXCLUDED)
    assert MA.is_excluded_slug("private-f--x", _EXCLUDED)
    # positive control: an unrelated crew, and one that merely shares the
    # separator with no excluded parent, are not excluded.
    assert not MA.is_excluded_slug("bosun", _EXCLUDED)
    assert not MA.is_excluded_slug("bosun--subcrew", _EXCLUDED)


def test_an_excluded_slug_in_a_dir_name_does_not_exclude_a_non_excluded_crew(tmp_path):
    """Every real `~/.claude/projects` dirname starts with this machine's own
    home directory, so a substring test on an excluded crew's slug would
    exclude everything if that slug happened to be a substring of $HOME.
    `crew_homes` must decide purely on the registry SLUG."""
    bosun_home = tmp_path / "home" / "private-a" / "work" / "agent-platform"
    bosun_home.mkdir(parents=True)

    def fake_units():
        return {"bosun": {}, "private-a": {}}

    def fake_home_path(slug):
        if slug == "bosun":
            return bosun_home
        if slug == "private-a":
            return tmp_path / "home" / "private-a" / "work" / "private-a"
        return None

    homes = MA.crew_homes(_EXCLUDED, units=fake_units, home_path=fake_home_path)
    assert MA._sanitize_dirname(bosun_home) in homes
    assert homes[MA._sanitize_dirname(bosun_home)] == "bosun"
    # private-a itself (the excluded crew, not a substring match) is excluded.
    assert all(slug != "private-a" for slug in homes.values())


# --- read_candidate / candidates_in -------------------------------------------


def test_a_feedback_memory_is_never_selected_even_when_cold(tmp_path):
    memory_dir = tmp_path / "memory"
    _write_memory(memory_dir, "a-feedback-rule", "feedback", mtime=OLD)
    candidates = MA.candidates_in(memory_dir)
    assert candidates == []


def test_reference_and_project_types_are_both_candidates(tmp_path):
    memory_dir = tmp_path / "memory"
    _write_memory(memory_dir, "a-reference-doc", "reference", mtime=OLD)
    _write_memory(memory_dir, "a-project-note", "project", mtime=OLD)
    names = {c["name"] for c in MA.candidates_in(memory_dir)}
    assert names == {"a-reference-doc", "a-project-note"}


def test_memory_md_itself_is_never_a_candidate(tmp_path):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "MEMORY.md").write_text("- [x](x.md) — hook\n")
    assert MA.candidates_in(memory_dir) == []


def test_a_top_level_type_field_is_recognized_for_older_files(tmp_path):
    memory_dir = tmp_path / "memory"
    _write_memory(memory_dir, "old-style", "reference", mtime=OLD, top_level_type=True)
    candidates = MA.candidates_in(memory_dir)
    assert [c["name"] for c in candidates] == ["old-style"]


def test_invalid_utf8_is_skipped(tmp_path):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(parents=True)
    path = memory_dir / "bad.md"
    path.write_bytes(b"---\nname: bad\n\xff\xfe---\n\nbody\n")
    assert MA.read_candidate(path) is None


# --- is_mtime_cold -------------------------------------------------------------


def test_mtime_under_30_days_is_not_cold(tmp_path):
    memory_dir = tmp_path / "memory"
    path = _write_memory(memory_dir, "fresh", "reference", mtime=RECENT)
    assert not MA.is_mtime_cold(path, NOW)


def test_mtime_at_or_past_30_days_is_cold(tmp_path):
    memory_dir = tmp_path / "memory"
    path = _write_memory(memory_dir, "stale", "reference", mtime=OLD)
    assert MA.is_mtime_cold(path, NOW)


# --- _name_line_occurs / find_read_names --------------------------------------


def test_the_name_prefix_collision_foo_versus_foo_bar():
    line_for_foo_bar = r'{"body": "...name: foo-bar\nmore text..."}' + "\n"
    assert MA._name_line_occurs(line_for_foo_bar, "foo-bar")
    assert not MA._name_line_occurs(line_for_foo_bar, "foo")


def test_the_name_line_boundary_also_accepts_a_closing_quote():
    line = r'{"snippet": "name: exact-name"}' + "\n"
    assert MA._name_line_occurs(line, "exact-name")


def test_a_name_line_in_a_nested_subagent_jsonl_is_not_cold(tmp_path, monkeypatch):
    project_dir = tmp_path / "project"
    memory_dir = project_dir / "memory"
    _write_memory(memory_dir, "read-elsewhere", "reference", mtime=OLD)
    _write_memory(memory_dir, "never-read", "reference", mtime=OLD)

    nested = project_dir / "subagents" / "sub-1"
    nested.mkdir(parents=True)
    (nested / "transcript.jsonl").write_text(
        r'{"message": {"content": "...name: read-elsewhere\nrest of body..."}}' + "\n",
        encoding="utf-8")

    cold = MA.cold_candidates(memory_dir, project_dir, NOW)
    assert [c["name"] for c in cold] == ["never-read"]


def test_find_read_names_streams_and_returns_only_matched_names(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)
    (project_dir / "t.jsonl").write_text(
        r'irrelevant line with no name field' + "\n"
        + r'{"x": "name: alpha\n"}' + "\n",
        encoding="utf-8")
    found = MA.find_read_names(project_dir, {"alpha", "beta"})
    assert found == {"alpha"}


def test_find_read_names_with_no_candidates_makes_no_filesystem_walk(tmp_path):
    # A directory that does not exist would raise on rglob if this were ever
    # called; the empty-candidate short-circuit must return before that.
    assert MA.find_read_names(tmp_path / "does-not-exist", set()) == set()


# --- build_payload -------------------------------------------------------------


def test_the_posted_content_equals_the_files_bytes(tmp_path):
    memory_dir = tmp_path / "memory"
    path = _write_memory(memory_dir, "a-memory", "reference", mtime=OLD)
    candidate = MA.read_candidate(path)
    payload = MA.build_payload("bosun", "bosun", candidate)
    assert payload["content"] == path.read_bytes().decode("utf-8")
    assert payload["content"] == candidate["raw"].decode("utf-8")


def test_body_and_content_are_not_the_same_string(tmp_path):
    memory_dir = tmp_path / "memory"
    path = _write_memory(memory_dir, "a-memory", "reference", mtime=OLD,
                         description="a synopsis", body="the real body text")
    candidate = MA.read_candidate(path)
    payload = MA.build_payload("bosun", "bosun", candidate)
    assert payload["body"] != payload["content"]
    assert "a-memory" in payload["body"]
    assert "a synopsis" in payload["body"]
    assert "the real body text" in payload["body"]


def test_body_is_clipped_at_the_cap(tmp_path):
    memory_dir = tmp_path / "memory"
    path = _write_memory(memory_dir, "a-memory", "reference", mtime=OLD,
                         body="x" * 300_000)
    candidate = MA.read_candidate(path)
    payload = MA.build_payload("bosun", "bosun", candidate)
    assert len(payload["body"]) == MA.MAX_BODY_CHARS


def test_dedup_key_changes_with_content(tmp_path):
    memory_dir = tmp_path / "memory"
    p1 = _write_memory(memory_dir, "m", "reference", mtime=OLD, body="one")
    c1 = MA.read_candidate(p1)
    payload1 = MA.build_payload("bosun", "bosun", c1)
    p1.write_text(_memory_text("m", "reference", body="two"), encoding="utf-8")
    os.utime(p1, (OLD, OLD))
    c2 = MA.read_candidate(p1)
    payload2 = MA.build_payload("bosun", "bosun", c2)
    assert payload1["dedup_key"] != payload2["dedup_key"]


# --- move_one: the recall gate -------------------------------------------------


class _Recorder:
    def __init__(self):
        self.posted = []
        self.searched = []
        self.blobbed = []


def _fake_poster(recorder, signal_id="sig-1", succeed=True):
    def poster(payload):
        recorder.posted.append(payload)
        return signal_id if succeed else None
    return poster


def _fake_searcher(recorder, rows):
    def searcher(name):
        recorder.searched.append(name)
        return rows
    return searcher


def _fake_blob_getter(recorder, blob):
    def blob_getter(signal_id):
        recorder.blobbed.append(signal_id)
        return blob
    return blob_getter


def _memory_md_with_two_lines(memory_dir: Path, name_a: str, name_b: str) -> Path:
    md = memory_dir / "MEMORY.md"
    md.write_text(
        f"- [{name_a}]({name_a}.md) — hook a\n"
        f"- [{name_b}]({name_b}.md) — hook b\n",
        encoding="utf-8")
    return md


def test_a_search_miss_leaves_the_file_and_index_line_intact(tmp_path):
    memory_dir = tmp_path / "memory"
    path = _write_memory(memory_dir, "m1", "reference", mtime=OLD)
    md = _memory_md_with_two_lines(memory_dir, "m1", "other")
    candidate = MA.read_candidate(path)
    rec = _Recorder()
    status, reason = MA.move_one("bosun", "bosun", candidate, md,
                                 poster=_fake_poster(rec),
                                 searcher=_fake_searcher(rec, []),
                                 blob_getter=_fake_blob_getter(rec, b"unused"))
    assert status == "kept"
    assert "search" in reason
    assert path.exists()
    assert "m1.md" in md.read_text()


def test_an_id_missing_from_results_leaves_the_file_and_index_line_intact(tmp_path):
    memory_dir = tmp_path / "memory"
    path = _write_memory(memory_dir, "m1", "reference", mtime=OLD)
    md = _memory_md_with_two_lines(memory_dir, "m1", "other")
    candidate = MA.read_candidate(path)
    rec = _Recorder()
    status, reason = MA.move_one(
        "bosun", "bosun", candidate, md,
        poster=_fake_poster(rec, signal_id="sig-1"),
        searcher=_fake_searcher(rec, [{"id": "sig-OTHER"}]),
        blob_getter=_fake_blob_getter(rec, b"unused"))
    assert status == "kept"
    assert "sig-1" in reason
    assert path.exists()
    assert "m1.md" in md.read_text()


def test_a_blob_byte_mismatch_leaves_the_file_and_index_line_intact(tmp_path):
    memory_dir = tmp_path / "memory"
    path = _write_memory(memory_dir, "m1", "reference", mtime=OLD)
    md = _memory_md_with_two_lines(memory_dir, "m1", "other")
    candidate = MA.read_candidate(path)
    rec = _Recorder()
    status, reason = MA.move_one(
        "bosun", "bosun", candidate, md,
        poster=_fake_poster(rec, signal_id="sig-1"),
        searcher=_fake_searcher(rec, [{"id": "sig-1"}]),
        blob_getter=_fake_blob_getter(rec, b"not the same bytes at all"))
    assert status == "kept"
    assert "blob" in reason
    assert path.exists()
    assert "m1.md" in md.read_text()


def test_a_write_failure_leaves_the_file_and_index_line_intact(tmp_path):
    memory_dir = tmp_path / "memory"
    path = _write_memory(memory_dir, "m1", "reference", mtime=OLD)
    md = _memory_md_with_two_lines(memory_dir, "m1", "other")
    candidate = MA.read_candidate(path)
    rec = _Recorder()
    status, reason = MA.move_one(
        "bosun", "bosun", candidate, md,
        poster=_fake_poster(rec, succeed=False),
        searcher=_fake_searcher(rec, []),
        blob_getter=_fake_blob_getter(rec, b"unused"))
    assert status == "kept"
    assert path.exists()
    assert "m1.md" in md.read_text()
    assert rec.searched == []  # never reached the search step


def test_an_exception_from_any_seam_is_treated_as_a_miss(tmp_path):
    memory_dir = tmp_path / "memory"
    path = _write_memory(memory_dir, "m1", "reference", mtime=OLD)
    md = _memory_md_with_two_lines(memory_dir, "m1", "other")
    candidate = MA.read_candidate(path)

    def raising_poster(payload):
        raise RuntimeError("boom")

    status, reason = MA.move_one(
        "bosun", "bosun", candidate, md,
        poster=raising_poster,
        searcher=lambda name: [],
        blob_getter=lambda sid: b"")
    assert status == "kept"
    assert "RuntimeError" in reason
    assert path.exists()


def test_a_passing_move_deletes_the_file_and_only_its_own_index_line(tmp_path):
    memory_dir = tmp_path / "memory"
    path_a = _write_memory(memory_dir, "move-me", "reference", mtime=OLD)
    _write_memory(memory_dir, "keep-me", "reference", mtime=OLD)
    md = _memory_md_with_two_lines(memory_dir, "move-me", "keep-me")
    other_line_before = [ln for ln in md.read_text().splitlines(keepends=True)
                         if "keep-me" in ln][0]

    candidate = MA.read_candidate(path_a)
    rec = _Recorder()
    status, reason = MA.move_one(
        "bosun", "bosun", candidate, md,
        poster=_fake_poster(rec, signal_id="sig-1"),
        searcher=_fake_searcher(rec, [{"id": "sig-1"}, {"id": "sig-2"}]),
        blob_getter=_fake_blob_getter(rec, candidate["raw"]))

    assert status == "moved", reason
    assert not path_a.exists()
    text_after = md.read_text()
    assert "move-me.md" not in text_after
    other_line_after = [ln for ln in text_after.splitlines(keepends=True)
                        if "keep-me" in ln][0]
    assert other_line_after == other_line_before


def test_an_index_edit_failure_leaves_the_file_and_is_reported_as_kept(tmp_path, monkeypatch):
    """Round 1 finding: the index edit must happen before the unlink, so a
    failure here (a write error on `MEMORY.md`, a non-UTF-8 read) must never
    have touched the file."""
    memory_dir = tmp_path / "memory"
    path = _write_memory(memory_dir, "m1", "reference", mtime=OLD)
    md = _memory_md_with_two_lines(memory_dir, "m1", "other")
    candidate = MA.read_candidate(path)
    rec = _Recorder()

    def raising_remove(memory_md_path, filename):
        raise OSError("disk full")

    monkeypatch.setattr(MA, "remove_memory_md_line", raising_remove)
    status, reason = MA.move_one(
        "bosun", "bosun", candidate, md,
        poster=_fake_poster(rec, signal_id="sig-1"),
        searcher=_fake_searcher(rec, [{"id": "sig-1"}]),
        blob_getter=_fake_blob_getter(rec, candidate["raw"]))
    assert status == "kept"
    assert "index edit" in reason
    assert path.exists()
    assert "m1.md" in md.read_text()


def test_an_unlink_failure_after_the_index_edit_is_reported_as_orphaned(tmp_path, monkeypatch):
    """The index line is already gone when the unlink fails, so this is
    neither "kept" (the file is not intact any more) nor "moved" (the file
    is still on disk) — its own outcome, "orphaned"."""
    memory_dir = tmp_path / "memory"
    path = _write_memory(memory_dir, "m1", "reference", mtime=OLD)
    md = _memory_md_with_two_lines(memory_dir, "m1", "other")
    candidate = MA.read_candidate(path)
    rec = _Recorder()

    def raising_unlink(self, *a, **kw):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "unlink", raising_unlink)
    status, reason = MA.move_one(
        "bosun", "bosun", candidate, md,
        poster=_fake_poster(rec, signal_id="sig-1"),
        searcher=_fake_searcher(rec, [{"id": "sig-1"}]),
        blob_getter=_fake_blob_getter(rec, candidate["raw"]))
    assert status == "orphaned"
    assert "delete failed" in reason
    assert path.exists()
    assert "m1.md" not in md.read_text()


def test_a_second_run_after_orphaning_completes_the_move(tmp_path, monkeypatch):
    """The state `move_one` leaves behind when the unlink fails — file
    present, index line gone — heals on a later run: the file is still
    cold, the write's `dedup_key` returns the same id, the index edit is a
    no-op, and the unlink is retried."""
    memory_dir = tmp_path / "memory"
    path = _write_memory(memory_dir, "m1", "reference", mtime=OLD)
    md = _memory_md_with_two_lines(memory_dir, "m1", "other")
    candidate = MA.read_candidate(path)
    rec = _Recorder()

    def raising_unlink(self, *a, **kw):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "unlink", raising_unlink)
    status, _reason = MA.move_one(
        "bosun", "bosun", candidate, md,
        poster=_fake_poster(rec, signal_id="sig-1"),
        searcher=_fake_searcher(rec, [{"id": "sig-1"}]),
        blob_getter=_fake_blob_getter(rec, candidate["raw"]))
    assert status == "orphaned"
    assert path.exists()
    assert "m1.md" not in md.read_text()

    monkeypatch.undo()  # the retry gets the real Path.unlink back

    candidate_again = MA.read_candidate(path)
    status2, reason2 = MA.move_one(
        "bosun", "bosun", candidate_again, md,
        poster=_fake_poster(rec, signal_id="sig-1"),  # dedup: same id as before
        searcher=_fake_searcher(rec, [{"id": "sig-1"}]),
        blob_getter=_fake_blob_getter(rec, candidate_again["raw"]))
    assert status2 == "moved", reason2
    assert not path.exists()


def test_move_one_asks_for_the_recalled_blob_of_the_posted_id(tmp_path):
    """The blob fetch must be keyed on the id the WRITE returned, not merely
    the first search result — a search can return several rows for a common
    name."""
    memory_dir = tmp_path / "memory"
    path = _write_memory(memory_dir, "m1", "reference", mtime=OLD)
    md = _memory_md_with_two_lines(memory_dir, "m1", "other")
    candidate = MA.read_candidate(path)
    rec = _Recorder()
    MA.move_one(
        "bosun", "bosun", candidate, md,
        poster=_fake_poster(rec, signal_id="sig-2"),
        searcher=_fake_searcher(rec, [{"id": "sig-1"}, {"id": "sig-2"}]),
        blob_getter=_fake_blob_getter(rec, candidate["raw"]))
    assert rec.blobbed == ["sig-2"]


# --- process_crew / run: dry-run and the whole walk ----------------------------


def _registered_project(tmp_path, slug: str) -> tuple[Path, Path]:
    """A `~/.claude/projects/<dir>` tree for *slug*, returning (home_dir,
    project_dir). `home_dir` is a plain tmp directory standing in for the
    crew's real home; `project_dir` is the matching `~/.claude/projects`
    entry, named exactly as `_sanitize_dirname(home_dir)` would name it."""
    home_dir = tmp_path / "homes" / slug
    home_dir.mkdir(parents=True)
    claude_projects = tmp_path / "claude-projects"
    claude_projects.mkdir(parents=True, exist_ok=True)
    project_dir = claude_projects / MA._sanitize_dirname(home_dir)
    project_dir.mkdir(parents=True)
    return home_dir, project_dir


def test_a_folder_that_maps_to_no_registered_home_is_untouched(tmp_path):
    home_dir, project_dir = _registered_project(tmp_path, "bosun")
    claude_projects = project_dir.parent
    _write_memory(project_dir / "memory", "cold-one", "reference", mtime=OLD)

    # An extra directory nobody's home resolves to — a scratchpad-shaped one.
    stray = claude_projects / "-private-tmp-claude-501-scratchpad"
    (stray / "memory").mkdir(parents=True)
    _write_memory(stray / "memory", "stray-memory", "reference", mtime=OLD)

    def fake_units():
        return {"bosun": {}}

    def fake_home_path(slug):
        return home_dir if slug == "bosun" else None

    rec = _Recorder()
    summary = MA.run(now=NOW, claude_projects_dir=claude_projects,
                     units=fake_units, home_path=fake_home_path,
                     poster=_fake_poster(rec, signal_id="sig-1"),
                     searcher=_fake_searcher(rec, [{"id": "sig-1"}]),
                     blob_getter=lambda sid: (project_dir / "memory" / "cold-one.md").read_bytes(),
                     log=lambda *_: None)

    assert "bosun" in summary
    assert (stray / "memory" / "stray-memory.md").exists()  # never touched


def test_dry_run_makes_no_call_and_changes_no_file(tmp_path):
    home_dir, project_dir = _registered_project(tmp_path, "bosun")
    claude_projects = project_dir.parent
    memory_dir = project_dir / "memory"
    path = _write_memory(memory_dir, "cold-one", "reference", mtime=OLD)
    md = _memory_md_with_two_lines(memory_dir, "cold-one", "keep-me")
    before_bytes = path.read_bytes()
    before_md = md.read_bytes()

    def fake_units():
        return {"bosun": {}}

    def fake_home_path(slug):
        return home_dir if slug == "bosun" else None

    def _boom(*_a, **_kw):
        raise AssertionError("dry-run must never call a network seam")

    summary = MA.run(now=NOW, claude_projects_dir=claude_projects,
                     units=fake_units, home_path=fake_home_path,
                     poster=_boom, searcher=_boom, blob_getter=_boom,
                     dry_run=True, log=lambda *_: None)

    assert summary["bosun"]["cold"] == 1
    assert summary["bosun"]["moved"] == 0
    assert path.exists()
    assert path.read_bytes() == before_bytes
    assert md.read_bytes() == before_md


def test_run_moves_a_cold_memory_end_to_end(tmp_path):
    home_dir, project_dir = _registered_project(tmp_path, "bosun")
    claude_projects = project_dir.parent
    memory_dir = project_dir / "memory"
    path = _write_memory(memory_dir, "cold-one", "reference", mtime=OLD)
    raw = path.read_bytes()
    _memory_md_with_two_lines(memory_dir, "cold-one", "keep-me")

    def fake_units():
        return {"bosun": {}}

    def fake_home_path(slug):
        return home_dir if slug == "bosun" else None

    rec = _Recorder()
    summary = MA.run(
        now=NOW, claude_projects_dir=claude_projects,
        units=fake_units, home_path=fake_home_path,
        poster=_fake_poster(rec, signal_id="sig-1"),
        searcher=_fake_searcher(rec, [{"id": "sig-1"}]),
        blob_getter=_fake_blob_getter(rec, raw),
        log=lambda *_: None)

    assert summary["bosun"]["moved"] == 1
    assert not path.exists()
    md_text = (memory_dir / "MEMORY.md").read_text()
    assert "cold-one.md" not in md_text
    assert "keep-me.md" in md_text
    assert rec.posted[0]["actor"] == "bosun"
    assert rec.posted[0]["product"] == "bosun"


def test_an_excluded_crews_memory_folder_is_never_processed(tmp_path):
    home_dir, project_dir = _registered_project(tmp_path, "private-a")
    claude_projects = project_dir.parent
    _write_memory(project_dir / "memory", "cold-one", "reference", mtime=OLD)

    def fake_units():
        return {"private-a": {}}

    def fake_home_path(slug):
        return home_dir if slug == "private-a" else None

    def _boom(*_a, **_kw):
        raise AssertionError("an excluded crew must never reach a network seam")

    summary = MA.run(now=NOW, claude_projects_dir=claude_projects,
                     units=fake_units, home_path=fake_home_path,
                     poster=_boom, searcher=_boom, blob_getter=_boom,
                     log=lambda *_: None)
    assert summary == {}
    assert (project_dir / "memory" / "cold-one.md").exists()


def test_a_second_excluded_crews_memory_folder_is_never_processed(tmp_path):
    home_dir, project_dir = _registered_project(tmp_path, "private-f")
    claude_projects = project_dir.parent
    _write_memory(project_dir / "memory", "cold-one", "reference", mtime=OLD)

    def fake_units():
        return {"private-f": {}}

    def fake_home_path(slug):
        return home_dir if slug == "private-f" else None

    def _boom(*_a, **_kw):
        raise AssertionError("an excluded crew must never reach a network seam")

    summary = MA.run(now=NOW, claude_projects_dir=claude_projects,
                     units=fake_units, home_path=fake_home_path,
                     poster=_boom, searcher=_boom, blob_getter=_boom,
                     log=lambda *_: None)
    assert summary == {}
    assert (project_dir / "memory" / "cold-one.md").exists()


# --- the default I/O: the real Archives calls, stubbed at the transport ------


class _JsonResp:
    def __init__(self, status, body):
        self.status = status
        self._body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


class _BlobResp:
    def __init__(self, status, raw: bytes):
        self.status = status
        self._raw = raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._raw


def test_default_poster_requests_signals_and_returns_the_id(monkeypatch):
    monkeypatch.setattr(MA.signal_client, "token", lambda: "t")
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        return _JsonResp(201, {"id": "sig-1"})

    monkeypatch.setattr(MA.urllib.request, "urlopen", fake_urlopen)
    assert MA._default_poster({"kind": "memory"}) == "sig-1"
    assert captured["url"].endswith("/api/signals")
    assert captured["body"] == {"kind": "memory"}


def test_default_poster_returns_none_without_a_token(monkeypatch):
    monkeypatch.setattr(MA.signal_client, "token", lambda: None)

    def boom(*_a, **_kw):
        raise AssertionError("must not make a network call without a token")
    monkeypatch.setattr(MA.urllib.request, "urlopen", boom)
    assert MA._default_poster({"kind": "memory"}) is None


def test_default_searcher_reads_the_signals_key(monkeypatch):
    """The store's search response carries results under `signals` —
    `bin/mcp-ops-server`'s `signal_search` reads the same key from the same
    route (`GET /api/signals/search`)."""
    monkeypatch.setattr(MA.signal_client, "token", lambda: "t")
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        return _JsonResp(200, {"signals": [{"id": "sig-1"}]})

    monkeypatch.setattr(MA.urllib.request, "urlopen", fake_urlopen)
    results = MA._default_searcher("a-memory")
    assert results == [{"id": "sig-1"}]
    assert "/api/signals/search" in captured["url"]


def test_default_blob_getter_requests_the_blob_path_and_returns_raw_bytes(monkeypatch):
    """`GET /api/signals/{id}` carries no `content` field at all; the byte-exact
    body lives only at `/blob`. The recall gate's byte comparison depends on
    this fetching that route, unparsed."""
    monkeypatch.setattr(MA.signal_client, "token", lambda: "t")
    captured = {}
    raw = "some bytes, not necessarily JSON at all ☃".encode("utf-8")

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        return _BlobResp(200, raw)

    monkeypatch.setattr(MA.urllib.request, "urlopen", fake_urlopen)
    result = MA._default_blob_getter("sig-1")
    assert captured["url"].endswith("/api/signals/sig-1/blob")
    assert result == raw


def test_default_blob_getter_returns_none_on_404(monkeypatch):
    monkeypatch.setattr(MA.signal_client, "token", lambda: "t")

    def raise_404(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(MA.urllib.request, "urlopen", raise_404)
    assert MA._default_blob_getter("missing") is None


def test_default_blob_getter_returns_none_without_a_token(monkeypatch):
    monkeypatch.setattr(MA.signal_client, "token", lambda: None)

    def boom(*_a, **_kw):
        raise AssertionError("must not make a network call without a token")
    monkeypatch.setattr(MA.urllib.request, "urlopen", boom)
    assert MA._default_blob_getter("sig-1") is None


# --- bin/memory-archive: the entry point's own lock ----------------------------


def test_the_entry_point_skips_when_the_lock_is_already_held(memory_archive_cli, tmp_path, monkeypatch):
    import fcntl
    monkeypatch.setattr(memory_archive_cli, "LOCK_PATH", tmp_path / "run.lock")
    calls = []
    monkeypatch.setattr(memory_archive_cli.MA, "run", lambda **kw: calls.append(kw))
    monkeypatch.setattr("sys.argv", ["memory-archive"])

    held_fd = os.open(str(tmp_path / "run.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(held_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        rc = memory_archive_cli.main()
    finally:
        fcntl.flock(held_fd, fcntl.LOCK_UN)
        os.close(held_fd)

    assert rc == 0
    assert calls == []


def test_the_entry_point_runs_once_when_unlocked(memory_archive_cli, tmp_path, monkeypatch):
    monkeypatch.setattr(memory_archive_cli, "LOCK_PATH", tmp_path / "run.lock")
    calls = []
    monkeypatch.setattr(memory_archive_cli.MA, "run", lambda **kw: calls.append(kw))
    monkeypatch.setattr("sys.argv", ["memory-archive"])
    assert memory_archive_cli.main() == 0
    assert calls == [{"dry_run": False}]


def test_the_entry_point_passes_dry_run_through(memory_archive_cli, tmp_path, monkeypatch):
    monkeypatch.setattr(memory_archive_cli, "LOCK_PATH", tmp_path / "run.lock")
    calls = []
    monkeypatch.setattr(memory_archive_cli.MA, "run", lambda **kw: calls.append(kw))
    monkeypatch.setattr("sys.argv", ["memory-archive", "--dry-run"])
    assert memory_archive_cli.main() == 0
    assert calls == [{"dry_run": True}]


# --- operator config FAIL CLOSED: excluded_crews -----------------------------


def _unprocessed_bosun(tmp_path):
    """A single registered, non-excluded crew with one cold candidate — used
    by the three fail-closed tests below to assert nothing is archived even
    though nothing here would otherwise be excluded."""
    home_dir, project_dir = _registered_project(tmp_path, "bosun")
    _write_memory(project_dir / "memory", "cold-one", "reference", mtime=OLD)

    def fake_units():
        return {"bosun": {}}

    def fake_home_path(slug):
        return home_dir if slug == "bosun" else None

    def _boom(*_a, **_kw):
        raise AssertionError("a broken operator config must never reach a network seam")

    return project_dir, fake_units, fake_home_path, _boom


def test_a_missing_operator_config_file_archives_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CONVIVY_OPERATOR_CONFIG", str(tmp_path / "does-not-exist.json"))
    project_dir, fake_units, fake_home_path, _boom = _unprocessed_bosun(tmp_path)

    summary = MA.run(now=NOW, claude_projects_dir=project_dir.parent,
                     units=fake_units, home_path=fake_home_path,
                     poster=_boom, searcher=_boom, blob_getter=_boom,
                     log=lambda *_: None)
    assert summary == {}
    assert (project_dir / "memory" / "cold-one.md").exists()
    assert "archiving nothing" in capsys.readouterr().err


def test_a_malformed_operator_config_file_archives_nothing(tmp_path, monkeypatch, capsys):
    bad = tmp_path / "operator.json"
    bad.write_text("{not valid json")
    monkeypatch.setenv("CONVIVY_OPERATOR_CONFIG", str(bad))
    project_dir, fake_units, fake_home_path, _boom = _unprocessed_bosun(tmp_path)

    summary = MA.run(now=NOW, claude_projects_dir=project_dir.parent,
                     units=fake_units, home_path=fake_home_path,
                     poster=_boom, searcher=_boom, blob_getter=_boom,
                     log=lambda *_: None)
    assert summary == {}
    assert (project_dir / "memory" / "cold-one.md").exists()
    assert "archiving nothing" in capsys.readouterr().err


def test_a_config_read_that_fails_on_a_later_call_still_excludes_the_configured_crew(
        tmp_path, monkeypatch):
    """Round 2 finding. `run()`'s fail-closed gate loaded `excluded_crews` once, but
    `crew_homes` called `is_excluded_slug`, which re-read the config PER CREW via
    `_load_excluded_crews() or frozenset()` — so a config that failed on that later
    read excluded nothing, and an excluded crew's memories reached the network seam.
    `run()` now loads the set exactly once and passes it down, so a later read
    failure cannot reach `is_excluded_slug` at all. Fails against 066767cb: the
    first read succeeds (passing the fail-closed gate), the second — inside
    `is_excluded_slug` — fails, `or frozenset()` turns that into "nothing excluded",
    and the excluded crew's cold memory reaches `_boom`."""
    home_dir, project_dir = _registered_project(tmp_path, "private-a")
    _write_memory(project_dir / "memory", "cold-one", "reference", mtime=OLD)

    def fake_units():
        return {"private-a": {}}

    def fake_home_path(slug):
        return home_dir if slug == "private-a" else None

    def _boom(*_a, **_kw):
        raise AssertionError("an excluded crew must never reach a network seam, "
                             "even when a later config read fails")

    real_get = operator_config.get
    calls = {"n": 0}

    def flaky_get(key):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_get(key)
        raise operator_config.OperatorConfigError("simulated failure on a later read")

    monkeypatch.setattr(operator_config, "get", flaky_get)

    summary = MA.run(now=NOW, claude_projects_dir=project_dir.parent,
                     units=fake_units, home_path=fake_home_path,
                     poster=_boom, searcher=_boom, blob_getter=_boom,
                     log=lambda *_: None)
    assert summary == {}
    assert (project_dir / "memory" / "cold-one.md").exists()
    assert calls["n"] == 1


def test_an_empty_excluded_crews_list_archives_nothing(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "operator.json"
    cfg.write_text(json.dumps({"excluded_crews": []}))
    monkeypatch.setenv("CONVIVY_OPERATOR_CONFIG", str(cfg))
    project_dir, fake_units, fake_home_path, _boom = _unprocessed_bosun(tmp_path)

    summary = MA.run(now=NOW, claude_projects_dir=project_dir.parent,
                     units=fake_units, home_path=fake_home_path,
                     poster=_boom, searcher=_boom, blob_getter=_boom,
                     log=lambda *_: None)
    assert summary == {}
    assert (project_dir / "memory" / "cold-one.md").exists()
    assert "archiving nothing" in capsys.readouterr().err
