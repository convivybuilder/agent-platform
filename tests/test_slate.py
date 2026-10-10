"""slate — the per-crew Slate store (`platform/decisions/0091`, CQ-357).

`bin/co-ask` is the writer `test_co_ask.py` pins; these tests pin the store
itself: file shape, locking, durability, corruption handling and every card
transition `slate.py` defines. CQ-358's `session-agent slate*` verbs build on
this same module — `answer_ask`/`create_note`/`send_note` are exercised here
even though `co-ask` never calls them.
"""
from __future__ import annotations

import copy
import itertools
import json
import os
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import slate as S


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """Every test gets its own fake home, so no test touches the real
    ~/.local/state. `state_root()` re-reads `$HOME` on every call (see its
    docstring), so this is enough — no module attribute to repoint."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# --- the empty board ---------------------------------------------------------


def test_a_missing_file_reads_as_an_empty_board(home):
    assert S.load("crew") == {"version": 1, "session": "crew", "cards": []}


def test_reading_does_not_create_a_board_file(home):
    """A read still has to lock, which creates the directory and the lock
    file — but it must never create a board file for a crew that never
    posted anything."""
    S.load("crew")
    assert not S.board_path("crew").exists()


# --- modes -------------------------------------------------------------------


def test_the_directory_is_created_at_0700(home):
    S.post_ask("crew", "what port?")
    assert _mode(S.state_root()) == 0o700


def test_a_looser_pre_existing_directory_is_tightened(home):
    S.state_root().mkdir(parents=True)
    os.chmod(S.state_root(), 0o755)
    S.post_ask("crew", "what port?")
    assert _mode(S.state_root()) == 0o700


def test_the_board_file_is_created_at_0600(home):
    S.post_ask("crew", "what port?")
    assert _mode(S.board_path("crew")) == 0o600


def test_a_looser_pre_existing_board_file_is_tightened_on_write(home):
    S.state_root().mkdir(parents=True)
    path = S.board_path("crew")
    path.write_text(json.dumps({"version": 1, "session": "crew", "cards": []}))
    os.chmod(path, 0o644)
    S.post_ask("crew", "what port?")
    assert _mode(path) == 0o600


def test_the_lock_file_is_created_at_0600(home):
    S.post_ask("crew", "what port?")
    assert _mode(S.lock_path("crew")) == 0o600


def test_a_looser_pre_existing_lock_file_is_tightened(home):
    S.state_root().mkdir(parents=True)
    S.lock_path("crew").touch()
    os.chmod(S.lock_path("crew"), 0o644)
    S.post_ask("crew", "what port?")
    assert _mode(S.lock_path("crew")) == 0o600


# --- corruption ----------------------------------------------------------


def test_a_corrupt_file_is_never_overwritten_by_a_write(home):
    path = S.board_path("crew")
    path.parent.mkdir(parents=True)
    bad = b"{ not json at all"
    path.write_bytes(bad)
    with pytest.raises(S.SlateCorruptError):
        S.post_ask("crew", "what port?")
    assert path.read_bytes() == bad


def test_loading_a_corrupt_file_raises_the_distinct_error(home):
    path = S.board_path("crew")
    path.parent.mkdir(parents=True)
    path.write_text("[1, 2, 3]")   # valid JSON, not a board object
    with pytest.raises(S.SlateCorruptError):
        S.load("crew")


# --- ask transitions -----------------------------------------------------


def test_post_ask_creates_an_open_card(home):
    card_id = S.post_ask("crew", "  what port?  ")
    assert card_id.startswith("a-")
    cards = S.load("crew")["cards"]
    assert len(cards) == 1
    card = cards[0]
    assert card["kind"] == "ask"
    assert card["state"] == "open"
    assert card["question"] == "what port?"
    assert card["answer_draft"] is None
    assert card["answer"] is None
    assert card["outcome"] is None
    assert card["answered_at"] is None
    assert card["resolved_at"] is None
    assert card["created_at"] == card["updated_at"]


def test_post_ask_refuses_an_empty_question(home):
    with pytest.raises(ValueError):
        S.post_ask("crew", "   ")
    assert not S.state_root().exists()


def test_answer_then_resolve(home):
    card_id = S.post_ask("crew", "what port?")
    S.answer_ask("crew", card_id, "8080")
    card = S.load("crew")["cards"][0]
    assert card["state"] == "answered"
    assert card["answer"] == "8080"
    assert card["answered_at"] is not None

    S.resolve_ask("crew", card_id, "used 8080")
    card = S.load("crew")["cards"][0]
    assert card["state"] == "resolved"
    assert card["outcome"] == "used 8080"
    assert card["resolved_at"] is not None


def test_resolve_directly_from_open(home):
    card_id = S.post_ask("crew", "what port?")
    S.resolve_ask("crew", card_id, "never mind")
    assert S.load("crew")["cards"][0]["state"] == "resolved"


def test_re_answering_an_answered_ask_stays_answered(home):
    card_id = S.post_ask("crew", "what port?")
    S.answer_ask("crew", card_id, "8080")
    S.answer_ask("crew", card_id, "8081")
    card = S.load("crew")["cards"][0]
    assert card["state"] == "answered"
    assert card["answer"] == "8081"


def test_resolving_a_resolved_ask_is_refused(home):
    card_id = S.post_ask("crew", "what port?")
    S.resolve_ask("crew", card_id, "done")
    with pytest.raises(S.SlateTransitionError):
        S.resolve_ask("crew", card_id, "again")


def test_answering_a_resolved_ask_is_refused(home):
    card_id = S.post_ask("crew", "what port?")
    S.resolve_ask("crew", card_id, "done")
    with pytest.raises(S.SlateTransitionError):
        S.answer_ask("crew", card_id, "8080")


def test_resolving_an_unknown_id_is_refused(home):
    S.post_ask("crew", "what port?")
    with pytest.raises(S.SlateNotFoundError):
        S.resolve_ask("crew", "a-ffffff", "done")


def test_resolving_a_note_is_refused(home):
    """Matches the specific message so this pins the `kind` guard itself, not
    just the fact that a note's state ("draft"/"sent") also isn't in
    `_ASK_RESOLVE_FROM` — which would refuse it for an unrelated reason even
    with the kind guard deleted."""
    note_id = S.create_note("crew", "fyi")
    with pytest.raises(S.SlateTransitionError, match="is a note, not an ask"):
        S.resolve_ask("crew", note_id, "done")


def test_answering_a_note_is_refused(home):
    note_id = S.create_note("crew", "fyi")
    with pytest.raises(S.SlateTransitionError, match="is a note, not an ask"):
        S.answer_ask("crew", note_id, "8080")


def test_a_refused_transition_writes_nothing(home):
    card_id = S.post_ask("crew", "what port?")
    S.resolve_ask("crew", card_id, "done")
    before = S.board_path("crew").read_bytes()
    with pytest.raises(S.SlateTransitionError):
        S.resolve_ask("crew", card_id, "again")
    assert S.board_path("crew").read_bytes() == before


def test_an_empty_outcome_is_refused(home):
    card_id = S.post_ask("crew", "what port?")
    with pytest.raises(ValueError):
        S.resolve_ask("crew", card_id, "   ")
    assert S.load("crew")["cards"][0]["state"] == "open"


def test_an_empty_answer_is_refused(home):
    card_id = S.post_ask("crew", "what port?")
    with pytest.raises(ValueError):
        S.answer_ask("crew", card_id, "   ")
    assert S.load("crew")["cards"][0]["state"] == "open"


# --- the write ceiling -----------------------------------------------------


def test_a_post_that_would_push_the_board_over_the_ceiling_is_refused(home):
    """A write whose serialised board would be over `_BOARD_MAX_BYTES` is refused
    before anything lands, so the file stays byte for byte and reads still work.
    A board written over the ceiling would be refused by every later read."""
    S.post_ask("crew", "what port?")
    before = S.board_path("crew").read_bytes()
    with pytest.raises(S.SlateBoardTooLargeError, match=str(S._BOARD_MAX_BYTES)):
        S.post_ask("crew", "x" * S._BOARD_MAX_BYTES)
    assert S.board_path("crew").read_bytes() == before
    assert [c["question"] for c in S.open_asks("crew")] == ["what port?"]


def test_an_answer_that_would_push_the_board_over_the_ceiling_is_refused(home):
    card_id = S.post_ask("crew", "what port?")
    before = S.board_path("crew").read_bytes()
    with pytest.raises(S.SlateBoardTooLargeError):
        S.answer_ask("crew", card_id, "x" * S._BOARD_MAX_BYTES)
    assert S.board_path("crew").read_bytes() == before
    assert S.load("crew")["cards"][0]["state"] == "open"


# --- note transitions ----------------------------------------------------


def test_create_note_is_a_draft(home):
    note_id = S.create_note("crew", "remember to deploy")
    assert note_id.startswith("n-")
    card = S.load("crew")["cards"][0]
    assert card["kind"] == "note"
    assert card["state"] == "draft"
    assert card["text"] == "remember to deploy"
    assert card["sent_at"] is None


def test_send_note_marks_it_sent(home):
    note_id = S.create_note("crew", "remember to deploy")
    S.send_note("crew", note_id)
    card = S.load("crew")["cards"][0]
    assert card["state"] == "sent"
    assert card["sent_at"] is not None


def test_sending_an_already_sent_note_is_refused(home):
    note_id = S.create_note("crew", "remember to deploy")
    S.send_note("crew", note_id)
    with pytest.raises(S.SlateTransitionError):
        S.send_note("crew", note_id)


def test_sending_an_ask_is_refused(home):
    """Matches the specific message for the same reason the note-side
    equivalents do: an ask's state ("open"/"answered"/"resolved") also isn't
    in `_NOTE_SEND_FROM`, so only the message proves the `kind` guard fired."""
    card_id = S.post_ask("crew", "what port?")
    with pytest.raises(S.SlateTransitionError, match="is an ask, not a note"):
        S.send_note("crew", card_id)


def test_sending_an_unknown_id_is_refused(home):
    with pytest.raises(S.SlateNotFoundError):
        S.send_note("crew", "n-ffffff")


def test_create_note_refuses_empty_text(home):
    with pytest.raises(ValueError):
        S.create_note("crew", "   ")
    assert not S.state_root().exists()


# --- open_asks -------------------------------------------------------------


def test_open_asks_excludes_resolved_and_keeps_order(home):
    first = S.post_ask("crew", "first?")
    second = S.post_ask("crew", "second?")
    S.resolve_ask("crew", first, "done")
    assert [c["id"] for c in S.open_asks("crew")] == [second]


def test_open_asks_includes_answered(home):
    card_id = S.post_ask("crew", "what port?")
    S.answer_ask("crew", card_id, "8080")
    assert [c["id"] for c in S.open_asks("crew")] == [card_id]


def test_open_asks_excludes_notes(home):
    S.create_note("crew", "fyi")
    card_id = S.post_ask("crew", "what port?")
    assert [c["id"] for c in S.open_asks("crew")] == [card_id]


# --- ask_counts (CQ-358, platform/decisions/0091) ---------------------------


def test_ask_counts_on_a_missing_board_is_zero_and_zero(home):
    assert S.ask_counts("crew") == {"asks_open": 0, "asks_answered": 0}


def test_ask_counts_splits_open_and_answered_and_excludes_resolved(home):
    """The plant check: swapping this for one count that sums `open` and
    `answered` together still passes a board with only one of the two
    populated, so the board below carries BOTH plus a resolved ask and a
    note, and asserts the exact split rather than just a total."""
    S.post_ask("crew", "open?")
    answered_id = S.post_ask("crew", "answered?")
    S.answer_ask("crew", answered_id, "yes")
    resolved_id = S.post_ask("crew", "resolved?")
    S.resolve_ask("crew", resolved_id, "done")
    S.create_note("crew", "fyi")

    assert S.ask_counts("crew") == {"asks_open": 1, "asks_answered": 1}


def test_ask_counts_on_a_corrupt_board_raises_rather_than_lying(home):
    """`session_state.slate_counts` is the layer that turns this into 0/0 plus
    `slate_error`; the store itself must not silently swallow corruption."""
    path = S.board_path("crew")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"{ not json at all")
    with pytest.raises(S.SlateCorruptError):
        S.ask_counts("crew")


def test_ask_counts_on_a_board_path_that_is_a_directory_raises_rather_than_crashing(home):
    """`open()` on a directory raises `IsADirectoryError`, an `OSError` the
    unhardened `_read_raw` did not catch — this is the class
    `security-reviewer` found on #752: the board directory is one shared 0700
    tree writable by every process running as the operator's account, so any
    of them can put a directory at another crew's board path."""
    path = S.board_path("crew")
    path.parent.mkdir(parents=True)
    path.mkdir()
    with pytest.raises(S.SlateCorruptError):
        S.ask_counts("crew")


@pytest.mark.skipif(os.getuid() == 0, reason="root ignores a file's permission bits")
def test_ask_counts_on_a_permission_denied_board_raises_rather_than_crashing(home):
    path = S.board_path("crew")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"{}")
    os.chmod(path, 0o000)
    try:
        with pytest.raises(S.SlateCorruptError):
            S.ask_counts("crew")
    finally:
        os.chmod(path, 0o600)


def test_ask_counts_on_a_board_over_the_byte_ceiling_raises_rather_than_crashing(home):
    path = S.board_path("crew")
    path.parent.mkdir(parents=True)
    oversized = json.dumps({"version": 1, "session": "crew",
                             "cards": [{"pad": "x" * S._BOARD_MAX_BYTES}]})
    path.write_bytes(oversized.encode())
    assert len(oversized) > S._BOARD_MAX_BYTES
    with pytest.raises(S.SlateCorruptError):
        S.ask_counts("crew")


def test_ask_counts_on_a_nesting_bomb_well_under_the_ceiling_raises_rather_than_crashing(home):
    """A few KB of `[[[[…` is thousands of levels deep — well past the
    interpreter's recursion limit, and raising `RecursionError`, long before
    any byte ceiling would see it as large."""
    path = S.board_path("crew")
    path.parent.mkdir(parents=True)
    bomb = b"[" * 5_000
    assert len(bomb) < S._BOARD_MAX_BYTES
    path.write_bytes(bomb)
    with pytest.raises(S.SlateCorruptError):
        S.ask_counts("crew")


def test_ask_counts_on_a_fifo_board_returns_promptly_rather_than_hanging(home):
    """Round 3 (security-reviewer finding on #752): a board path replaced by
    a named pipe with no writer makes a plain `open(path, "rb")` block on
    the `open()` call itself, forever — `session_states()` reads every
    crew's board in one synchronous loop, so one such board would freeze
    the whole poll, not just its own crew. `_read_raw` must refuse a FIFO
    before ever blocking on it; the SIGALRM below is a hang detector, not
    the pass condition."""
    import signal
    path = S.board_path("crew")
    path.parent.mkdir(parents=True)
    os.mkfifo(path)
    signal.signal(signal.SIGALRM, lambda *a: (_ for _ in ()).throw(
        AssertionError("ask_counts hung on a writerless FIFO board")))
    signal.alarm(10)
    try:
        with pytest.raises(S.SlateCorruptError):
            S.ask_counts("crew")
    finally:
        signal.alarm(0)


def test_a_mutating_verb_on_a_fifo_board_refuses_and_releases_the_lock(home):
    """The same FIFO, through `mutate()` (`post_ask`) rather than `load()`
    (`ask_counts`): `mutate()` takes the crew's exclusive flock *before*
    `_read_raw` ever runs, so an unbounded hang there would also block every
    other reader or writer of this crew's board, including a legitimate
    `co-ask`. The refusal must still release that lock — a `post_ask` on the
    same crew right after must succeed, not find the board still held."""
    import signal
    path = S.board_path("crew")
    path.parent.mkdir(parents=True)
    os.mkfifo(path)
    signal.signal(signal.SIGALRM, lambda *a: (_ for _ in ()).throw(
        AssertionError("post_ask hung on a writerless FIFO board")))
    signal.alarm(10)
    try:
        with pytest.raises(S.SlateCorruptError):
            S.post_ask("crew", "what port?")
    finally:
        signal.alarm(0)
    path.unlink()
    card_id = S.post_ask("crew", "second ask after the refusal")
    assert card_id


# --- get_card ----------------------------------------------------------------


def test_get_card_returns_the_matching_card(home):
    card_id = S.post_ask("crew", "what port?")
    assert S.get_card("crew", card_id)["id"] == card_id


def test_get_card_raises_on_an_unknown_id(home):
    with pytest.raises(S.SlateNotFoundError):
        S.get_card("crew", "a-ffffff")


# --- list_cards: hiding resolved asks and sent notes (CQ-358) ---------------


def test_list_cards_hides_a_resolved_ask_and_a_sent_note_by_default(home):
    open_id = S.post_ask("crew", "open?")
    resolved_id = S.post_ask("crew", "resolved?")
    S.resolve_ask("crew", resolved_id, "done")
    draft_id = S.create_note("crew", "fyi")
    sent_id = S.create_note("crew", "ship it")
    S.send_note("crew", sent_id)

    visible = {c["id"] for c in S.list_cards("crew")}
    assert visible == {open_id, draft_id}


def test_list_cards_shows_everything_with_include_all(home):
    resolved_id = S.post_ask("crew", "resolved?")
    S.resolve_ask("crew", resolved_id, "done")
    sent_id = S.create_note("crew", "ship it")
    S.send_note("crew", sent_id)

    visible = {c["id"] for c in S.list_cards("crew", include_all=True)}
    assert visible == {resolved_id, sent_id}


def test_list_cards_shows_an_answered_ask_without_all(home):
    """Answered is not resolved — only resolved asks and sent notes hide."""
    card_id = S.post_ask("crew", "what port?")
    S.answer_ask("crew", card_id, "8080")
    assert [c["id"] for c in S.list_cards("crew")] == [card_id]


# --- edit_card: a draft note's text, or an ask's answer_draft (CQ-358) ------


def test_edit_card_replaces_a_draft_notes_text(home):
    note_id = S.create_note("crew", "first draft")
    S.edit_card("crew", note_id, "second draft")
    assert S.get_card("crew", note_id)["text"] == "second draft"


def test_edit_card_replaces_an_open_asks_answer_draft(home):
    card_id = S.post_ask("crew", "what port?")
    S.edit_card("crew", card_id, "maybe 8080")
    card = S.get_card("crew", card_id)
    assert card["answer_draft"] == "maybe 8080"
    assert card["state"] == "open"


def test_edit_card_replaces_an_answered_asks_answer_draft(home):
    card_id = S.post_ask("crew", "what port?")
    S.answer_ask("crew", card_id, "8080")
    S.edit_card("crew", card_id, "8081 actually")
    card = S.get_card("crew", card_id)
    assert card["answer_draft"] == "8081 actually"
    assert card["answer"] == "8080", "editing the draft must not touch the sent answer"
    assert card["state"] == "answered"


def test_edit_card_allows_empty_text(home):
    """No server-side non-empty check on an edit — only the two ADR-named
    state refusals below."""
    note_id = S.create_note("crew", "first draft")
    S.edit_card("crew", note_id, "   ")
    assert S.get_card("crew", note_id)["text"] == "   "


def test_edit_card_refuses_a_sent_note(home):
    note_id = S.create_note("crew", "fyi")
    S.send_note("crew", note_id)
    with pytest.raises(S.SlateTransitionError):
        S.edit_card("crew", note_id, "too late")


def test_edit_card_refuses_a_resolved_ask(home):
    card_id = S.post_ask("crew", "what port?")
    S.resolve_ask("crew", card_id, "done")
    with pytest.raises(S.SlateTransitionError):
        S.edit_card("crew", card_id, "too late")


def test_edit_card_raises_on_an_unknown_id(home):
    with pytest.raises(S.SlateNotFoundError):
        S.edit_card("crew", "a-ffffff", "x")


def test_a_refused_edit_writes_nothing(home):
    note_id = S.create_note("crew", "fyi")
    S.send_note("crew", note_id)
    before = S.board_path("crew").read_bytes()
    with pytest.raises(S.SlateTransitionError):
        S.edit_card("crew", note_id, "too late")
    assert S.board_path("crew").read_bytes() == before


# --- delete_note: drafts only (CQ-358) --------------------------------------


def test_delete_note_removes_a_draft(home):
    note_id = S.create_note("crew", "fyi")
    S.delete_note("crew", note_id)
    assert S.load("crew")["cards"] == []


def test_delete_note_refuses_a_sent_note(home):
    note_id = S.create_note("crew", "fyi")
    S.send_note("crew", note_id)
    with pytest.raises(S.SlateTransitionError):
        S.delete_note("crew", note_id)
    assert len(S.load("crew")["cards"]) == 1


def test_delete_note_refuses_an_ask(home):
    """Matches the specific message for the same reason the sibling checks
    elsewhere in this file do: an ask's state isn't in the note-only set
    either, so only the message proves the `kind` guard fired."""
    card_id = S.post_ask("crew", "what port?")
    with pytest.raises(S.SlateTransitionError, match="is an ask, not a note"):
        S.delete_note("crew", card_id)
    assert len(S.load("crew")["cards"]) == 1


def test_delete_note_raises_on_an_unknown_id(home):
    with pytest.raises(S.SlateNotFoundError):
        S.delete_note("crew", "n-ffffff")


# --- the exact message texts (CQ-358, platform/decisions/0091) -------------


def test_ask_message_matches_the_adrs_exact_text():
    assert (S.ask_message("what port?", "a-7f3k2q", "8080") ==
            "[slate a-7f3k2q] Your question: what port?\nMy answer: 8080")


def test_note_message_matches_the_adrs_exact_text():
    assert (S.note_message("n-9d1x4m", "ship it") ==
            "[slate n-9d1x4m] Note from the operator:\nship it")


# --- prepare_send / finalize_send (CQ-358) ----------------------------------


def test_prepare_send_builds_the_ask_message_and_does_not_mutate(home):
    card_id = S.post_ask("crew", "what port?")
    S.edit_card("crew", card_id, "8080")
    before = S.board_path("crew").read_bytes()

    card, message = S.prepare_send("crew", card_id)

    assert card["id"] == card_id
    assert message == f"[slate {card_id}] Your question: what port?\nMy answer: 8080"
    assert S.board_path("crew").read_bytes() == before


def test_prepare_send_builds_the_note_message_and_does_not_mutate(home):
    note_id = S.create_note("crew", "ship it")
    before = S.board_path("crew").read_bytes()

    card, message = S.prepare_send("crew", note_id)

    assert card["id"] == note_id
    assert message == f"[slate {note_id}] Note from the operator:\nship it"
    assert S.board_path("crew").read_bytes() == before


def test_prepare_send_refuses_an_ask_with_no_answer_draft(home):
    card_id = S.post_ask("crew", "what port?")
    with pytest.raises(ValueError):
        S.prepare_send("crew", card_id)
    assert S.get_card("crew", card_id)["state"] == "open"


def test_prepare_send_refuses_an_ask_with_a_whitespace_only_answer_draft(home):
    card_id = S.post_ask("crew", "what port?")
    S.edit_card("crew", card_id, "   ")
    with pytest.raises(ValueError):
        S.prepare_send("crew", card_id)


def test_prepare_send_refuses_a_sent_note(home):
    note_id = S.create_note("crew", "fyi")
    S.send_note("crew", note_id)
    with pytest.raises(S.SlateTransitionError):
        S.prepare_send("crew", note_id)


def test_prepare_send_refuses_a_resolved_ask(home):
    card_id = S.post_ask("crew", "what port?")
    S.resolve_ask("crew", card_id, "done")
    with pytest.raises(S.SlateTransitionError):
        S.prepare_send("crew", card_id)


def test_an_answer_that_would_push_the_board_over_the_ceiling_is_refused_before_delivery(home):
    """`prepare_send` sizes the board with the answer applied, so an answer that
    `answer_ask` would be refused for is refused before any caller delivers it.
    The board is set to exactly `_BOARD_MAX_BYTES`, which a write allows, with
    its answer draft in place; the answer's own fields then take it over."""
    card_id = S.post_ask("crew", "x")
    with S.mutate("crew") as board:
        board["cards"][0]["answer_draft"] = "8080"
        base = len((json.dumps(board, indent=2) + "\n").encode("utf-8"))
        board["cards"][0]["question"] = "x" * (S._BOARD_MAX_BYTES - base + 1)
    before = S.board_path("crew").read_bytes()
    assert len(before) == S._BOARD_MAX_BYTES
    with pytest.raises(S.SlateBoardTooLargeError):
        S.prepare_send("crew", card_id)
    assert S.board_path("crew").read_bytes() == before


def test_a_note_send_that_would_push_the_board_over_the_ceiling_is_refused_before_delivery(home):
    """The same sizing for a note: `sent_at` adds a timestamp to a board that
    was at the ceiling, so the send is refused before delivery."""
    note_id = S.create_note("crew", "x")
    with S.mutate("crew") as board:
        base = len((json.dumps(board, indent=2) + "\n").encode("utf-8"))
        board["cards"][0]["text"] = "x" * (S._BOARD_MAX_BYTES - base + 1)
    before = S.board_path("crew").read_bytes()
    assert len(before) == S._BOARD_MAX_BYTES
    with pytest.raises(S.SlateBoardTooLargeError):
        S.prepare_send("crew", note_id)
    assert S.board_path("crew").read_bytes() == before


def test_a_board_with_no_session_key_is_sized_with_the_key_the_answer_writes(home):
    """`answer_ask`'s `mutate` writes the `session` key, so its bytes count toward
    the ceiling. A board file with no key must be sized with it too, or
    `prepare_send` passes and delivers an answer that `answer_ask` then refuses.
    The question is padded so the board with the answer applied and no key is
    exactly at the ceiling: that write fits, and the key's bytes push it over."""
    card_id = S.post_ask("crew", "x")
    S.edit_card("crew", card_id, "8080")
    board = S.load("crew")
    del board["session"]
    applied = copy.deepcopy(board)
    S._apply_answer(applied["cards"][0], "8080", "2026-10-04T00:00:00Z")
    base = len((json.dumps(applied, indent=2) + "\n").encode("utf-8"))
    board["cards"][0]["question"] = "x" * (S._BOARD_MAX_BYTES - base + 1)
    S.board_path("crew").write_text(json.dumps(board, indent=2) + "\n")
    assert len(S.board_path("crew").read_bytes()) <= S._BOARD_MAX_BYTES
    with pytest.raises(S.SlateBoardTooLargeError):
        S.prepare_send("crew", card_id)
    with pytest.raises(S.SlateBoardTooLargeError):
        S.answer_ask("crew", card_id, "8080")


def test_finalize_send_marks_a_note_sent(home):
    note_id = S.create_note("crew", "ship it")
    card = S.finalize_send("crew", note_id, "note")
    assert card["state"] == "sent"
    assert card["sent_at"] is not None


def test_finalize_send_answers_an_ask(home):
    card_id = S.post_ask("crew", "what port?")
    card = S.finalize_send("crew", card_id, "ask", answer="8080")
    assert card["state"] == "answered"
    assert card["answer"] == "8080"
    assert card["answered_at"] is not None


def test_a_second_send_on_an_answered_ask_overwrites_the_answer(home, monkeypatch):
    """Comment 4602's ruling: re-sending an answered ask is allowed, updates
    `answer`/`answered_at`/`updated_at`, and leaves the state at `answered`.

    Both fields are already non-`None` after the FIRST send, so asserting
    only `is not None` on the second send's values would still pass a bug
    that stamps `answered_at`/`updated_at` only on the open→answered
    transition and leaves a resend's stamps untouched. The clock is fixed to
    a distinct, strictly increasing instant per call so the two sends cannot
    land in the same tick and pass by accident."""
    ticks = (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=n)
             for n in itertools.count())

    class _FakeDatetime:
        @staticmethod
        def now(tz=None):
            return next(ticks)

    monkeypatch.setattr(S, "datetime", _FakeDatetime)

    card_id = S.post_ask("crew", "what port?")
    S.edit_card("crew", card_id, "8080")
    first = S.finalize_send("crew", card_id, "ask", answer="8080")

    S.edit_card("crew", card_id, "8081")
    _, message = S.prepare_send("crew", card_id)
    second = S.finalize_send("crew", card_id, "ask", answer="8081")

    assert message == f"[slate {card_id}] Your question: what port?\nMy answer: 8081"
    assert second["state"] == "answered"
    assert second["answer"] == "8081"
    assert second["answered_at"] != first["answered_at"]
    assert second["updated_at"] != first["updated_at"]
    assert second["answer_draft"] == "8081", "answer_draft is not cleared on send"


def test_a_failed_delivery_leaves_the_card_unchanged(home):
    """`finalize_send` is never called when delivery (the caller's own
    `session_input.send_message`) raises — pinned at the CLI level in
    `test_session_agent_slate.py`; this is the store-level half: `prepare_send`
    alone must not have touched anything a failed delivery would need to
    leave standing."""
    card_id = S.post_ask("crew", "what port?")
    S.edit_card("crew", card_id, "8080")
    before = S.board_path("crew").read_bytes()
    S.prepare_send("crew", card_id)  # as far as a failed delivery ever gets
    assert S.board_path("crew").read_bytes() == before


# --- cards from two keys never collide --------------------------------------


def test_two_crews_keep_separate_boards(home):
    S.post_ask("alpha", "a question")
    S.post_ask("bravo", "another question")
    assert len(S.load("alpha")["cards"]) == 1
    assert len(S.load("bravo")["cards"]) == 1


# --- concurrency -------------------------------------------------------------

_APPEND_SCRIPT = """
import sys
sys.path.insert(0, sys.argv[1])
import slate
key, prefix = sys.argv[2], sys.argv[3]
for i in range(200):
    slate.post_ask(key, f"{prefix}-{i}")
"""


def test_two_processes_each_appending_200_cards_concurrently(home):
    """Two REAL OS processes, not threads — the thing `fcntl.flock` has to
    serialize across. Each posts 200 asks under the same key; the board must
    end up with all 400, still parseable, no id or question collided."""
    lib_dir = str(Path(__file__).resolve().parent.parent / "lib" / "agent-platform")
    env = {**os.environ, "HOME": str(home)}
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _APPEND_SCRIPT, lib_dir, "crew", f"p{n}"],
            env=env)
        for n in range(2)
    ]
    for p in procs:
        assert p.wait() == 0, "a concurrent writer process failed"

    board = json.loads(S.board_path("crew").read_text())
    assert len(board["cards"]) == 400
    assert len({c["id"] for c in board["cards"]}) == 400
    assert len({c["question"] for c in board["cards"]}) == 400
