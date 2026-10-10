"""`bin/session-agent`'s `slate`/`slate-note`/`slate-edit`/`slate-delete`/
`slate-send` verbs — Deck's side of the Slate (`slate.py`, CQ-358,
platform/decisions/0091).

These pin the CLI's own contract: argv/stdin handling, the JSON envelope
(`{"ok": true, ...}` / `{"ok": false, "error": "..."}`), and — for
`slate-send` — that delivery goes through `session_input.send_message` and
that the card changes ONLY once that call has succeeded, AND ONLY ONCE THE
TRANSCRIPT CONFIRMS IT (CQ-420 / issue_01M4BNS7KKGDWBPF1XFPV8XMPV): an empty
or unreadable input box is not proof of delivery. Everything about the
store's own transitions, locking and message-text construction is
`test_slate.py`'s job; this drives the real `slate` module (through the
session-scoped `session_agent_cli` fixture's own `S`) against an isolated
`$HOME`. Most tests here use the `confirmed_delivery` fixture to stub the
receipt as instantly confirmed, since their own focus is the Slate
transition; the "slate-send's receipt" section at the bottom exercises
`SI.transcript_offset`/`SI.await_delivery` for real, against a real
transcript file, which is the one other external effect this layer owns
besides `SI.send_message` itself.
"""
from __future__ import annotations

import copy
import io
import json

import pytest


@pytest.fixture
def sa(session_agent_cli):
    return session_agent_cli


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _run(sa, monkeypatch, argv, stdin=None):
    monkeypatch.setattr(sa.sys, "argv", ["session-agent", *argv])
    if stdin is not None:
        monkeypatch.setattr(sa.sys, "stdin", io.StringIO(stdin))
    return sa.main()


def _emitted(sa, monkeypatch):
    calls = []
    monkeypatch.setattr(sa, "_emit", calls.append)
    return calls


def _board(sa, key="deck"):
    return json.loads(sa.S.board_path(key).read_text())


@pytest.fixture
def resolved_pane(sa, monkeypatch):
    """Stub `slate-send`'s one-time pane resolution (CQ-420 pane agreement)
    to a fixed `(pane_id, claude_pid)`, for a test whose focus is not that
    mechanism itself — which gets its own tests under "pane agreement"
    below."""
    monkeypatch.setattr(sa.SI, "resolve_pane", lambda name: ("%3", 4242))


@pytest.fixture
def confirmed_delivery(sa, monkeypatch, resolved_pane):
    """Stub `slate-send`'s transcript receipt (CQ-420) as instantly
    confirmed, for a test whose focus is the Slate transition rather than
    the confirmation itself — `SI.transcript_offset`/`SI.await_delivery`
    are exercised directly, against a real transcript file, by the tests
    under "slate-send's receipt" below."""
    monkeypatch.setattr(sa.SI, "transcript_offset", lambda name, pane=None: (None, 0))
    monkeypatch.setattr(sa.SI, "await_delivery", lambda *a, **k: None)


# --- slate -------------------------------------------------------------------


def test_slate_lists_open_asks_and_draft_notes_without_all(sa, monkeypatch):
    emitted = _emitted(sa, monkeypatch)
    open_id = sa.S.post_ask("deck", "what port?")
    resolved_id = sa.S.post_ask("deck", "resolved?")
    sa.S.resolve_ask("deck", resolved_id, "done")
    draft_id = sa.S.create_note("deck", "fyi")

    rc = _run(sa, monkeypatch, ["slate", "deck"])

    assert rc == 0
    [env] = emitted
    assert env["ok"] is True
    assert env["session"] == "deck"
    assert {c["id"] for c in env["cards"]} == {open_id, draft_id}


def test_slate_all_includes_resolved_and_sent(sa, monkeypatch):
    emitted = _emitted(sa, monkeypatch)
    resolved_id = sa.S.post_ask("deck", "resolved?")
    sa.S.resolve_ask("deck", resolved_id, "done")

    rc = _run(sa, monkeypatch, ["slate", "deck", "--all"])

    assert rc == 0
    [env] = emitted
    assert resolved_id in {c["id"] for c in env["cards"]}


def test_slate_on_a_corrupt_board_reports_ok_false(sa, monkeypatch, home):
    sa.S.board_path("deck").parent.mkdir(parents=True)
    sa.S.board_path("deck").write_bytes(b"{ not json")
    emitted = _emitted(sa, monkeypatch)

    rc = _run(sa, monkeypatch, ["slate", "deck"])

    assert rc == 1
    assert emitted[0]["ok"] is False


# --- slate-note ----------------------------------------------------------


def test_slate_note_creates_a_draft_and_returns_the_card(sa, monkeypatch):
    emitted = _emitted(sa, monkeypatch)

    rc = _run(sa, monkeypatch, ["slate-note", "deck", "--text", "ship it"])

    assert rc == 0
    [env] = emitted
    assert env["ok"] is True
    assert env["card"]["kind"] == "note"
    assert env["card"]["state"] == "draft"
    assert env["card"]["text"] == "ship it"
    assert _board(sa)["cards"][0]["text"] == "ship it"


def test_slate_note_dash_reads_from_stdin(sa, monkeypatch):
    emitted = _emitted(sa, monkeypatch)

    rc = _run(sa, monkeypatch, ["slate-note", "deck", "--text", "-"], stdin="from stdin\n")

    assert rc == 0
    assert emitted[0]["card"]["text"] == "from stdin"


def test_slate_note_refuses_empty_text(sa, monkeypatch):
    emitted = _emitted(sa, monkeypatch)

    rc = _run(sa, monkeypatch, ["slate-note", "deck", "--text", "   "])

    assert rc == 1
    assert emitted[0]["ok"] is False
    assert not sa.S.state_root().exists() or not sa.S.board_path("deck").exists()


# --- slate-edit ------------------------------------------------------------


def test_slate_edit_replaces_a_draft_notes_text(sa, monkeypatch):
    note_id = sa.S.create_note("deck", "first draft")
    emitted = _emitted(sa, monkeypatch)

    rc = _run(sa, monkeypatch, ["slate-edit", "deck", note_id, "--text", "second draft"])

    assert rc == 0
    assert emitted[0]["card"]["text"] == "second draft"


def test_slate_edit_replaces_an_asks_answer_draft(sa, monkeypatch):
    card_id = sa.S.post_ask("deck", "what port?")
    emitted = _emitted(sa, monkeypatch)

    rc = _run(sa, monkeypatch, ["slate-edit", "deck", card_id, "--text", "8080"])

    assert rc == 0
    assert emitted[0]["card"]["answer_draft"] == "8080"
    assert emitted[0]["card"]["state"] == "open"


def test_slate_edit_dash_reads_from_stdin(sa, monkeypatch):
    note_id = sa.S.create_note("deck", "first draft")
    emitted = _emitted(sa, monkeypatch)

    rc = _run(sa, monkeypatch, ["slate-edit", "deck", note_id, "--text", "-"],
              stdin="from stdin")

    assert rc == 0
    assert emitted[0]["card"]["text"] == "from stdin"


def test_slate_edit_refuses_a_sent_note(sa, monkeypatch):
    note_id = sa.S.create_note("deck", "fyi")
    sa.S.send_note("deck", note_id)
    emitted = _emitted(sa, monkeypatch)

    rc = _run(sa, monkeypatch, ["slate-edit", "deck", note_id, "--text", "too late"])

    assert rc == 1
    assert emitted[0]["ok"] is False


def test_slate_edit_refuses_an_unknown_id(sa, monkeypatch):
    emitted = _emitted(sa, monkeypatch)

    rc = _run(sa, monkeypatch, ["slate-edit", "deck", "a-ffffff", "--text", "x"])

    assert rc == 1
    assert emitted[0]["ok"] is False


# --- slate-delete ----------------------------------------------------------


def test_slate_delete_removes_a_draft(sa, monkeypatch):
    note_id = sa.S.create_note("deck", "fyi")
    emitted = _emitted(sa, monkeypatch)

    rc = _run(sa, monkeypatch, ["slate-delete", "deck", note_id])

    assert rc == 0
    assert emitted[0] == {"ok": True, "id": note_id}
    assert _board(sa)["cards"] == []


def test_slate_delete_refuses_an_ask(sa, monkeypatch):
    card_id = sa.S.post_ask("deck", "what port?")
    emitted = _emitted(sa, monkeypatch)

    rc = _run(sa, monkeypatch, ["slate-delete", "deck", card_id])

    assert rc == 1
    assert emitted[0]["ok"] is False
    assert len(_board(sa)["cards"]) == 1


# --- slate-send --------------------------------------------------------------


def test_slate_send_delivers_a_note_and_marks_it_sent(sa, monkeypatch, confirmed_delivery):
    note_id = sa.S.create_note("deck", "ship it")
    calls = []
    monkeypatch.setattr(sa.SI, "send_message",
                        lambda name, text, pane_id=None: calls.append((name, text)) or "%3")
    emitted = _emitted(sa, monkeypatch)

    rc = _run(sa, monkeypatch, ["slate-send", "deck", note_id])

    assert rc == 0
    [env] = emitted
    assert env["ok"] is True
    assert env["pane"] == "%3"
    assert env["card"]["state"] == "sent"
    assert calls == [("deck", f"[slate {note_id}] Note from the operator:\nship it")]


def test_slate_send_delivers_an_answered_ask(sa, monkeypatch, confirmed_delivery):
    card_id = sa.S.post_ask("deck", "what port?")
    sa.S.edit_card("deck", card_id, "8080")
    calls = []
    monkeypatch.setattr(sa.SI, "send_message",
                        lambda name, text, pane_id=None: calls.append((name, text)) or "%3")
    emitted = _emitted(sa, monkeypatch)

    rc = _run(sa, monkeypatch, ["slate-send", "deck", card_id])

    assert rc == 0
    [env] = emitted
    assert env["card"]["state"] == "answered"
    assert env["card"]["answer"] == "8080"
    assert calls == [("deck",
                      f"[slate {card_id}] Your question: what port?\nMy answer: 8080")]


def test_slate_send_refuses_an_empty_answer_draft_before_ever_calling_send(sa, monkeypatch):
    """`prepare_send`'s own pre-delivery check — pinned at the CLI level too,
    so a caller of THIS layer cannot observe a message reaching the pane for
    an ask with nothing to send."""
    card_id = sa.S.post_ask("deck", "what port?")
    calls = []
    monkeypatch.setattr(sa.SI, "send_message", lambda name, text: calls.append(1) or "%3")
    emitted = _emitted(sa, monkeypatch)

    rc = _run(sa, monkeypatch, ["slate-send", "deck", card_id])

    assert rc == 1
    assert emitted[0]["ok"] is False
    assert calls == []
    assert _board(sa)["cards"][0]["state"] == "open"


def test_a_failed_delivery_leaves_the_card_unchanged(sa, monkeypatch, confirmed_delivery):
    card_id = sa.S.post_ask("deck", "what port?")
    sa.S.edit_card("deck", card_id, "8080")

    def refuse(name, text, pane_id=None):
        raise sa.SI.InputError("this session is waiting on a question")

    monkeypatch.setattr(sa.SI, "send_message", refuse)
    emitted = _emitted(sa, monkeypatch)
    before = sa.S.board_path("deck").read_bytes()

    rc = _run(sa, monkeypatch, ["slate-send", "deck", card_id])

    assert rc == 1
    assert emitted[0]["ok"] is False
    assert "waiting on a question" in emitted[0]["error"]
    assert sa.S.board_path("deck").read_bytes() == before


def test_a_delivery_that_cannot_find_the_pane_leaves_the_card_unchanged(
        sa, monkeypatch):
    """`SI.resolve_pane` resolves a live tmux pane for `args.name` ONCE,
    before the offset read or the send (CQ-420 pane agreement), and raises
    `SI.JoinError` (not `InputError`) when none exists — a real case for
    `slate-send`, since the id it addresses is the Slate's own and names no
    pane at all. `SI.send_message` must never be reached."""
    card_id = sa.S.post_ask("deck", "what port?")
    sa.S.edit_card("deck", card_id, "8080")

    def refuse(name):
        raise sa.SI.JoinError(f"no claude process found in any pane of {name!r}")

    monkeypatch.setattr(sa.SI, "resolve_pane", refuse)
    calls = []
    monkeypatch.setattr(sa.SI, "send_message",
                        lambda name, text, pane_id=None: calls.append(1) or "%3")
    emitted = _emitted(sa, monkeypatch)
    before = sa.S.board_path("deck").read_bytes()

    rc = _run(sa, monkeypatch, ["slate-send", "deck", card_id])

    assert rc == 1
    assert emitted[0]["ok"] is False
    assert calls == []
    assert sa.S.board_path("deck").read_bytes() == before


def test_slate_send_refuses_an_answer_the_board_cannot_hold_before_delivery(sa, monkeypatch):
    """The size check `prepare_send` runs happens before `send_message`, so an
    answer whose write would be refused never reaches the pane. The board is
    set to exactly the ceiling with its answer draft in place, as the store
    test does."""
    card_id = sa.S.post_ask("deck", "x")
    with sa.S.mutate("deck") as board:
        board["cards"][0]["answer_draft"] = "8080"
        base = len((json.dumps(board, indent=2) + "\n").encode("utf-8"))
        board["cards"][0]["question"] = "x" * (sa.S._BOARD_MAX_BYTES - base + 1)
    before = sa.S.board_path("deck").read_bytes()
    calls = []
    monkeypatch.setattr(sa.SI, "send_message", lambda name, text: calls.append(1) or "%3")
    emitted = _emitted(sa, monkeypatch)

    rc = _run(sa, monkeypatch, ["slate-send", "deck", card_id])

    assert rc == 1
    assert emitted[0]["ok"] is False
    assert calls == []
    assert sa.S.board_path("deck").read_bytes() == before


def test_slate_send_refuses_an_answer_on_a_board_with_no_session_key_before_delivery(sa, monkeypatch):
    """A board file with no `session` key, padded so the answer's write fits only
    without that key. `answer_ask` writes the key, so the send must be refused
    before the pane is reached; `prepare_send` sizes the key for that reason."""
    card_id = sa.S.post_ask("deck", "x")
    sa.S.edit_card("deck", card_id, "8080")
    board = sa.S.load("deck")
    del board["session"]
    applied = copy.deepcopy(board)
    sa.S._apply_answer(applied["cards"][0], "8080", "2026-10-04T00:00:00Z")
    base = len((json.dumps(applied, indent=2) + "\n").encode("utf-8"))
    board["cards"][0]["question"] = "x" * (sa.S._BOARD_MAX_BYTES - base + 1)
    sa.S.board_path("deck").write_text(json.dumps(board, indent=2) + "\n")
    before = sa.S.board_path("deck").read_bytes()
    calls = []
    monkeypatch.setattr(sa.SI, "send_message", lambda name, text: calls.append(1) or "%3")
    emitted = _emitted(sa, monkeypatch)

    rc = _run(sa, monkeypatch, ["slate-send", "deck", card_id])

    assert rc == 1
    assert emitted[0]["ok"] is False
    assert calls == []
    assert sa.S.board_path("deck").read_bytes() == before


def test_slate_send_on_a_resolved_ask_is_refused_and_never_calls_send(sa, monkeypatch):
    card_id = sa.S.post_ask("deck", "what port?")
    sa.S.resolve_ask("deck", card_id, "done")
    calls = []
    monkeypatch.setattr(sa.SI, "send_message", lambda name, text: calls.append(1) or "%3")
    emitted = _emitted(sa, monkeypatch)

    rc = _run(sa, monkeypatch, ["slate-send", "deck", card_id])

    assert rc == 1
    assert emitted[0]["ok"] is False
    assert calls == []


def test_a_second_send_on_an_answered_ask_overwrites_the_answer(
        sa, monkeypatch, confirmed_delivery):
    card_id = sa.S.post_ask("deck", "what port?")
    sa.S.edit_card("deck", card_id, "8080")
    monkeypatch.setattr(sa.SI, "send_message", lambda name, text, pane_id=None: "%3")
    assert _run(sa, monkeypatch, ["slate-send", "deck", card_id]) == 0

    sa.S.edit_card("deck", card_id, "8081")
    emitted = _emitted(sa, monkeypatch)
    rc = _run(sa, monkeypatch, ["slate-send", "deck", card_id])

    assert rc == 0
    card = emitted[0]["card"]
    assert card["state"] == "answered"
    assert card["answer"] == "8081"
    assert card["answer_draft"] == "8081", "answer_draft is not cleared on send"


# --- slate-send's receipt: confirming delivery in the transcript (CQ-420) ---
#
# `SI.transcript_offset`/`SI.await_delivery` are exercised for real here,
# against a real transcript file, rather than stubbed as they are above —
# this is the one other external effect `slate-send` owns besides
# `SI.send_message` itself, and it is the whole point of this work item: an
# empty or unreadable input box is not proof of delivery, so a card must not
# move to `answered`/`sent` on that alone.


def _log_lines(sa):
    path = sa.slate_send_log_path()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_slate_send_marks_answered_once_the_transcript_confirms_it(
        sa, monkeypatch, tmp_path, home, resolved_pane):
    card_id = sa.S.post_ask("deck", "what port?")
    sa.S.edit_card("deck", card_id, "8080")
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("")
    monkeypatch.setattr(sa.SI, "transcript_offset", lambda name, pane=None: (transcript, 0))

    def fake_send(name, text, pane_id=None):
        # The real delivery path, measured live against Claude Code 2.1.292
        # (see `session_input.await_delivery`'s module note): a plain `user`
        # record carrying the exact text sent.
        with open(transcript, "a") as fh:
            fh.write(json.dumps({"type": "user", "message": {"content": text}}) + "\n")
        return "%3"

    monkeypatch.setattr(sa.SI, "send_message", fake_send)
    emitted = _emitted(sa, monkeypatch)

    rc = _run(sa, monkeypatch, ["slate-send", "deck", card_id])

    assert rc == 0
    [env] = emitted
    assert env["ok"] is True
    assert env["card"]["state"] == "answered"
    [last] = _log_lines(sa)
    assert last["session"] == "deck"
    assert last["pane"] == "%3"
    assert last["transcript"] == str(transcript)
    assert last["card_id"] == card_id
    assert last["result"] == "confirmed"


def test_slate_send_leaves_the_ask_open_and_exits_nonzero_without_a_transcript_entry(
        sa, monkeypatch, tmp_path, home, resolved_pane):
    """The box empties (or reads unreadable — see `session_input`'s own
    module note on the 2.1.292 border-colour drift), but nothing matching
    ever lands in the transcript. The ask must stay `open`, the command
    must exit non-zero, and the failure must say delivery was not
    confirmed — not report success on the empty box alone."""
    card_id = sa.S.post_ask("deck", "what port?")
    sa.S.edit_card("deck", card_id, "8080")
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("")
    monkeypatch.setattr(sa.SI, "transcript_offset", lambda name, pane=None: (transcript, 0))
    monkeypatch.setattr(sa.SI, "send_message", lambda name, text, pane_id=None: "%3")
    # The real deadline is seconds; the clock is driven rather than slept
    # through, the same discipline `test_unqueue_stops_polling_once_the_
    # deadline_passes` uses in `test_session_verbs.py`.
    monkeypatch.setattr(sa.SI.time, "sleep", lambda _s: None)
    ticks = iter([0.0] + [sa.SI.SLATE_DELIVERY_DEADLINE_SECONDS + 1.0] * 50)
    monkeypatch.setattr(sa.SI.time, "monotonic", lambda: next(ticks))
    emitted = _emitted(sa, monkeypatch)
    before = sa.S.board_path("deck").read_bytes()

    rc = _run(sa, monkeypatch, ["slate-send", "deck", card_id])

    assert rc == 1
    assert emitted[0]["ok"] is False
    assert "could not confirm delivery" in emitted[0]["error"]
    assert sa.S.board_path("deck").read_bytes() == before, \
        "the ask must stay open; an unconfirmed send must not mark it answered"
    [last] = _log_lines(sa)
    assert last["pane"] == "%3"
    assert last["transcript"] == str(transcript)
    assert last["result"].startswith("not confirmed")


def test_a_timed_out_delivery_never_logs_the_question_or_answer_text(
        sa, monkeypatch, tmp_path, home, resolved_pane):
    """A timeout's `InputError` used to carry the sent text's first line
    verbatim (`could not confirm delivery ...: no new transcript entry
    carried {first_line!r} ...`), and `_log_slate_send` writes `str(e)` into
    `~/.local/state/convivy/session-agent.log` on exactly this path — the
    success path logs no text at all, and a question can quote private
    data. Neither the question nor the answer may appear in the durable log
    or in the error this command emits."""
    card_id = sa.S.post_ask("deck", "what is the database password?")
    sa.S.edit_card("deck", card_id, "hunter2")
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("")
    monkeypatch.setattr(sa.SI, "transcript_offset", lambda name, pane=None: (transcript, 0))
    monkeypatch.setattr(sa.SI, "send_message", lambda name, text, pane_id=None: "%3")
    monkeypatch.setattr(sa.SI.time, "sleep", lambda _s: None)
    ticks = iter([0.0] + [sa.SI.SLATE_DELIVERY_DEADLINE_SECONDS + 1.0] * 50)
    monkeypatch.setattr(sa.SI.time, "monotonic", lambda: next(ticks))
    emitted = _emitted(sa, monkeypatch)

    rc = _run(sa, monkeypatch, ["slate-send", "deck", card_id])

    assert rc == 1
    assert emitted[0]["ok"] is False
    assert "database password" not in emitted[0]["error"]
    assert "hunter2" not in emitted[0]["error"]
    log_text = sa.slate_send_log_path().read_text()
    assert "database password" not in log_text
    assert "hunter2" not in log_text
    [last] = _log_lines(sa)
    assert last["result"].startswith("not confirmed")
