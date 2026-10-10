"""`co-working-on` — the writer half of Deck's cockpit "working on" line.

The reader, `session_state.working_on`, is tested against the file this script
writes in `tests/test_session_verbs.py`. These tests pin the writer's own
contract: it resolves the tmux session by name (the same name the reader is
keyed by), refuses to record nothing, sanitises and bounds the text before it
ever touches disk, and fails loudly on stderr rather than looking like it
worked — unlike `co-session-health`, which must never break a render.
"""
from __future__ import annotations

import json
import time

import pytest


@pytest.fixture
def cwo(co_working_on):
    return co_working_on


@pytest.fixture
def cache(cwo, tmp_path, monkeypatch):
    monkeypatch.setattr(cwo, "CACHE_DIR", tmp_path)
    return tmp_path


def _in_tmux(cwo, monkeypatch, name="deck", rc=0):
    """Stub `_tmux_session_name`'s two dependencies: the `$TMUX` gate and the
    `tmux display-message` call itself, so no test here shells out for real."""
    monkeypatch.setenv("TMUX", "/tmp/tmux-501/default,12345,0")

    class _Done:
        def __init__(self):
            self.stdout = name + "\n"
            self.returncode = rc

    monkeypatch.setattr(cwo.subprocess, "run", lambda *a, **k: _Done())


def _recorded(cache, key="deck"):
    return json.loads((cache / f"working-{key}.json").read_text())


# --- the happy path -------------------------------------------------------


def test_records_the_text_under_the_tmux_session_name(cwo, cache, monkeypatch):
    _in_tmux(cwo, monkeypatch, name="deck")
    rc = cwo.main(["chasing the pagination bug"])
    assert rc == 0
    got = _recorded(cache)
    assert got["text"] == "chasing the pagination bug"
    assert isinstance(got["written_at"], float)


def test_the_key_matches_the_readers_own_sanitisation(cwo, cache, monkeypatch):
    """The writer and `session_state.working_on` must agree on one filename for
    the same session name, or the reader never finds what this wrote."""
    _in_tmux(cwo, monkeypatch, name="convivy-deck")
    cwo.main(["doing a thing"])
    assert (cache / "working-convivy-deck.json").exists()


# --- refusals ---------------------------------------------------------------


def test_an_empty_line_is_refused(cwo, cache, monkeypatch):
    _in_tmux(cwo, monkeypatch)
    assert cwo.main(["   "]) == 2
    assert list(cache.iterdir()) == []


def test_outside_tmux_refuses_rather_than_guessing_a_session(cwo, cache, monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    rc = cwo.main(["doing a thing"])
    assert rc == 1
    assert list(cache.iterdir()) == []


def test_a_failed_tmux_call_refuses_the_same_way(cwo, cache, monkeypatch):
    _in_tmux(cwo, monkeypatch, rc=1)
    assert cwo.main(["doing a thing"]) == 1
    assert list(cache.iterdir()) == []


# --- the text is untrusted display content ----------------------------------


def test_the_text_is_bounded_before_it_reaches_disk(cwo, cache, monkeypatch):
    _in_tmux(cwo, monkeypatch)
    long_text = "word " * 100   # well past MAX_LEN, no single long alnum run
    cwo.main([long_text])
    got = _recorded(cache)["text"]
    assert len(got) <= cwo.MAX_LEN + len("...")
    assert got.endswith("...")


def test_ansi_escapes_cannot_reach_the_recorded_text(cwo, cache, monkeypatch):
    _in_tmux(cwo, monkeypatch)
    cwo.main(["\x1b[31mred\x1b[0m and more"])
    got = _recorded(cache)["text"]
    assert "\x1b" not in got


# --- the session-name key scheme --------------------------------------------


@pytest.mark.parametrize("hostile,expected", [
    ("../../etc/passwd", "etcpasswd"),
    ("a/b:c", "abc"),
])
def test_session_key_keeps_only_the_readers_charset(cwo, hostile, expected):
    assert cwo._session_key(hostile) == expected


def test_a_session_name_with_no_usable_characters_refuses(cwo, cache, monkeypatch):
    _in_tmux(cwo, monkeypatch, name="/:/")
    assert cwo.main(["doing a thing"]) == 1
    assert list(cache.iterdir()) == []


# MARK: - `started_at`, the clock "since" reads from


def test_re_announcing_the_same_text_keeps_the_original_start(cwo, cache):
    """The whole reason there are two timestamps.

    A co that says the same thing again is confirming it is still on that work,
    not starting it over. If `started_at` moved, a display reading "working on X
    since …" would report a task begun an hour ago as begun just now — a wrong
    answer that looks precise, which is worse than showing no duration.
    """
    W = cwo
    W._write("sess", "chasing the pagination bug")
    first = json.loads((cache / "working-sess.json").read_text())

    time.sleep(0.01)
    W._write("sess", "chasing the pagination bug")
    second = json.loads((cache / "working-sess.json").read_text())

    assert second["started_at"] == first["started_at"], "same work, same start"
    assert second["written_at"] > first["written_at"], "but freshly confirmed"


def test_new_text_starts_its_own_clock(cwo, cache):
    """New text is new work, so the duration resets rather than accumulating
    against a sentence nobody is working on any more."""
    W = cwo
    W._write("sess", "chasing the pagination bug")
    first = json.loads((cache / "working-sess.json").read_text())

    time.sleep(0.01)
    W._write("sess", "writing the release notes")
    second = json.loads((cache / "working-sess.json").read_text())

    assert second["started_at"] > first["started_at"]


def test_an_unreadable_previous_recording_starts_now(cwo, cache):
    """A missing history is not a reason to refuse a write. Same
    fail-to-the-present posture every other read in this script takes."""
    W = cwo
    (cache / "working-sess.json").write_text("{ not json")

    W._write("sess", "chasing the pagination bug")
    written = json.loads((cache / "working-sess.json").read_text())

    assert written["started_at"] == written["written_at"]


def test_same_text_with_no_previous_start_falls_to_now(cwo, cache):
    """The upgrade path, and the one a plausible "improvement" would break.

    A file written before `started_at` existed has the same text and no start.
    Falling back to its `written_at` reads like a better duration estimate and
    is wrong: it would report work begun hours ago as begun at the moment of
    the last re-announcement. Starting the clock now is the honest answer —
    the duration is unknown, so it begins being known here.
    """
    W = cwo
    before = time.time() - 3600
    (cache / "working-sess.json").write_text(json.dumps(
        {"text": "chasing the pagination bug", "written_at": before}))

    W._write("sess", "chasing the pagination bug")
    written = json.loads((cache / "working-sess.json").read_text())

    assert written["started_at"] == written["written_at"], "clock starts now"
    assert written["started_at"] > before, "never inherited from written_at"


def test_a_non_dict_previous_recording_starts_now(cwo, cache):
    """Valid JSON that is not an object — `_started_at`'s isinstance guard,
    which a `.get()` on a list would otherwise raise straight through."""
    W = cwo
    (cache / "working-sess.json").write_text('["not", "an", "object"]')

    W._write("sess", "chasing the pagination bug")
    written = json.loads((cache / "working-sess.json").read_text())

    assert written["started_at"] == written["written_at"]
