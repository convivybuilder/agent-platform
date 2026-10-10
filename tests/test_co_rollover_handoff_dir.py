"""The handoff directory, one per Claude session (co_rollover's handoff-directory section).

`agentctl session-dir`, `bin/co-rollover` and the ops MCP `handoff_write` tool all
resolve a session's directory through `co_rollover.session_handoff_dir`, so these
pin that function's own contract. The runs root is passed in, and HOME is a
temporary directory, so the pointer files never land under the real ~/.local/state.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pytest

import co_rollover as CR

NOW = datetime(2026, 10, 3, 9, 14)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return tmp_path


def _runs(tmp_path):
    runs = tmp_path / "agent-runs"
    runs.mkdir()
    return runs


def test_one_session_gets_the_same_directory_on_every_call(home):
    runs = _runs(home)
    first = CR.session_handoff_dir("sess-a", "vivy", runs_root=runs, now=NOW)
    second = CR.session_handoff_dir("sess-a", "vivy", runs_root=runs, now=NOW)
    assert first == second
    assert first == runs / "2026-10-03" / "co-vivy-0914"
    assert [p.name for p in (runs / "2026-10-03").iterdir()] == ["co-vivy-0914"]


def test_two_sessions_in_the_same_minute_get_separate_directories(home):
    runs = _runs(home)
    a = CR.session_handoff_dir("sess-a", "vivy", runs_root=runs, now=NOW)
    b = CR.session_handoff_dir("sess-b", "vivy", runs_root=runs, now=NOW)
    assert a != b
    assert a.is_dir() and b.is_dir()


def test_a_session_id_that_is_not_a_plain_key_is_refused_and_nothing_is_minted(home):
    runs = _runs(home)
    for bad in ("", "../escape", "a/b", ".hidden", "x" * 200):
        with pytest.raises(CR.HandoffDirError, match="not a usable key"):
            CR.session_handoff_dir(bad, "vivy", runs_root=runs, now=NOW)
    assert list(runs.iterdir()) == []


def test_a_product_that_is_not_a_slug_is_refused(home):
    runs = _runs(home)
    with pytest.raises(CR.HandoffDirError, match="lowercase slug"):
        CR.session_handoff_dir("sess-a", "Bad Product", runs_root=runs, now=NOW)
    assert list(runs.iterdir()) == []


def test_a_session_id_with_a_trailing_newline_is_refused_and_nothing_is_minted(home):
    runs = _runs(home)
    with pytest.raises(CR.HandoffDirError, match="not a usable key"):
        CR.session_handoff_dir("sess-a\n", "vivy", runs_root=runs, now=NOW)
    assert list(runs.iterdir()) == []


def test_a_product_with_a_trailing_newline_is_refused_and_nothing_is_minted(home):
    runs = _runs(home)
    with pytest.raises(CR.HandoffDirError, match="lowercase slug"):
        CR.session_handoff_dir("sess-a", "vivy\n", runs_root=runs, now=NOW)
    assert list(runs.iterdir()) == []


def _record(session_id, target):
    pointer = CR.handoff_pointer_root() / session_id
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(f"{target}\n")


def test_a_pointer_naming_a_directory_outside_the_runs_root_is_refused(home):
    runs = _runs(home)
    outside = home / "constance"
    outside.mkdir()
    _record("sess-a", outside)
    with pytest.raises(CR.HandoffDirError, match="outside the agent-runs root"):
        CR.session_handoff_dir("sess-a", "vivy", runs_root=runs, now=NOW)
    assert list(outside.iterdir()) == []


def test_a_symlinked_directory_inside_the_runs_root_that_points_out_is_refused(home):
    runs = _runs(home)
    outside = home / "elsewhere"
    outside.mkdir()
    link = runs / "2026-10-03" / "co-vivy-0914"
    link.parent.mkdir()
    link.symlink_to(outside, target_is_directory=True)
    _record("sess-a", link)
    with pytest.raises(CR.HandoffDirError, match="is a symlink"):
        CR.session_handoff_dir("sess-a", "vivy", runs_root=runs, now=NOW)
    assert list(outside.iterdir()) == []


def test_a_dangling_pointer_symlink_is_refused_and_nothing_is_minted(home):
    """A dangling symlink reads as a missing file. It must be refused, not read as
    "no record", which would mint a replacement."""
    runs = _runs(home)
    pointer = CR.handoff_pointer_root() / "sess-a"
    pointer.parent.mkdir(parents=True)
    pointer.symlink_to(home / "nowhere")
    with pytest.raises(CR.HandoffDirError, match="is a dangling symlink"):
        CR.session_handoff_dir("sess-a", "vivy", runs_root=runs, now=NOW)
    assert list(runs.iterdir()) == []


def test_a_pointer_that_cannot_be_read_as_a_file_is_refused_and_nothing_is_minted(home):
    runs = _runs(home)
    pointer = CR.handoff_pointer_root() / "sess-a"
    pointer.mkdir(parents=True)
    with pytest.raises(CR.HandoffDirError, match="cannot be read"):
        CR.session_handoff_dir("sess-a", "vivy", runs_root=runs, now=NOW)
    assert list(runs.iterdir()) == []


def test_a_recorded_directory_that_has_gone_is_refused_not_recreated(home):
    runs = _runs(home)
    d = CR.session_handoff_dir("sess-a", "vivy", runs_root=runs, now=NOW)
    d.rmdir()
    with pytest.raises(CR.HandoffDirError, match="is missing"):
        CR.session_handoff_dir("sess-a", "vivy", runs_root=runs, now=NOW)
    assert not d.exists()


def test_a_caller_that_loses_the_record_race_uses_the_winners_directory(home, monkeypatch):
    """Another caller records its directory between this caller's read and its
    link. This caller must take the winner's directory and remove the empty one it
    minted, not record a second one."""
    runs = _runs(home)
    winner = runs / "2026-10-03" / "co-vivy-0914-9"
    real_link = os.link

    def racing_link(src, dst, *a, **k):
        winner.mkdir(parents=True)
        Path(dst).write_text(f"{winner}\n")
        return real_link(src, dst, *a, **k)

    monkeypatch.setattr(CR.os, "link", racing_link)
    got = CR.session_handoff_dir("sess-a", "vivy", runs_root=runs, now=NOW)
    assert got == winner
    assert not (runs / "2026-10-03" / "co-vivy-0914").exists()


def test_write_handoff_replaces_the_file_and_leaves_no_temp_behind(tmp_path):
    target = tmp_path / "handoff.md"
    CR.write_handoff(target, "first")
    CR.write_handoff(target, "second")
    assert target.read_text() == "second"
    assert [p.name for p in tmp_path.iterdir()] == ["handoff.md"]


def test_a_failed_replace_keeps_the_old_handoff_and_leaves_no_temp(tmp_path, monkeypatch):
    target = tmp_path / "handoff.md"
    target.write_text("old")

    def fail(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(CR.os, "replace", fail)
    with pytest.raises(OSError, match="disk full"):
        CR.write_handoff(target, "new")
    assert target.read_text() == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["handoff.md"]
