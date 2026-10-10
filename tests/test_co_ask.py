"""`bin/co-ask` — the co's side of the Slate (`slate.py`, platform/decisions/0091).

These pin the CLI's own contract: stdin/`-`-arg handling, exit codes, the
outside-tmux refusal, and `list`'s escaping. The store's own transitions,
locking and durability are `test_slate.py`'s job — this file stubs the tmux
lookup and drives everything else through the real `slate` module against an
isolated `$HOME`.
"""
from __future__ import annotations

import io
import json

import pytest


@pytest.fixture
def ca(co_ask_cli):
    return co_ask_cli


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _in_tmux(ca, monkeypatch, name="deck"):
    monkeypatch.setattr(ca.S, "tmux_session_name", lambda: name)


def _board(ca, key="deck"):
    return json.loads(ca.S.board_path(key).read_text())


def _ask_id(ca, monkeypatch, capsys, question="what port?"):
    _in_tmux(ca, monkeypatch)
    assert ca.main(["ask", question]) == 0
    return capsys.readouterr().out.strip()


# --- outside tmux ------------------------------------------------------------


def test_outside_tmux_refuses_and_writes_nothing(ca, home, monkeypatch, capsys):
    monkeypatch.setattr(ca.S, "tmux_session_name", lambda: None)
    rc = ca.main(["ask", "what port?"])
    assert rc == 1
    assert capsys.readouterr().err
    assert not ca.S.state_root().exists()


def test_a_session_name_with_no_usable_characters_refuses(ca, home, monkeypatch):
    _in_tmux(ca, monkeypatch, name="/:/")
    rc = ca.main(["ask", "what port?"])
    assert rc == 1
    assert not ca.S.state_root().exists()


# --- ask -----------------------------------------------------------------


def test_ask_prints_only_the_bare_id(ca, home, monkeypatch, capsys):
    _in_tmux(ca, monkeypatch)
    rc = ca.main(["ask", "what port?"])
    assert rc == 0
    out = capsys.readouterr().out
    card_id = out.strip()
    assert card_id.startswith("a-")
    assert out == card_id + "\n"
    assert _board(ca)["cards"][0]["question"] == "what port?"


def test_ask_dash_reads_from_stdin(ca, home, monkeypatch, capsys):
    _in_tmux(ca, monkeypatch)
    monkeypatch.setattr(ca.sys, "stdin", io.StringIO("what port, exactly?\n"))
    rc = ca.main(["ask", "-"])
    assert rc == 0
    assert _board(ca)["cards"][0]["question"] == "what port, exactly?"


def test_an_empty_question_is_refused(ca, home, monkeypatch):
    _in_tmux(ca, monkeypatch)
    rc = ca.main(["ask", "   "])
    assert rc == 2
    assert not ca.S.board_path("deck").exists()


def test_a_hostile_stdin_question_round_trips_byte_for_byte(ca, home, monkeypatch, capsys):
    _in_tmux(ca, monkeypatch)
    hostile = "it's a \"test\" with $VAR and `cmd` and\na newline"
    monkeypatch.setattr(ca.sys, "stdin", io.StringIO(hostile))
    rc = ca.main(["ask", "-"])
    assert rc == 0
    assert _board(ca)["cards"][0]["question"] == hostile


# --- resolve -----------------------------------------------------------------


def test_resolve_closes_an_open_ask(ca, home, monkeypatch, capsys):
    card_id = _ask_id(ca, monkeypatch, capsys)
    rc = ca.main(["resolve", card_id, "used 8080"])
    assert rc == 0
    card = _board(ca)["cards"][0]
    assert card["state"] == "resolved"
    assert card["outcome"] == "used 8080"


def test_resolve_dash_reads_the_outcome_from_stdin(ca, home, monkeypatch, capsys):
    card_id = _ask_id(ca, monkeypatch, capsys)
    monkeypatch.setattr(ca.sys, "stdin", io.StringIO("used 8080\n"))
    rc = ca.main(["resolve", card_id, "-"])
    assert rc == 0
    assert _board(ca)["cards"][0]["outcome"] == "used 8080"


def test_resolve_refuses_an_unknown_id(ca, home, monkeypatch, capsys):
    _in_tmux(ca, monkeypatch)
    rc = ca.main(["resolve", "a-ffffff", "done"])
    assert rc != 0
    assert capsys.readouterr().err


def test_resolve_refuses_a_note_id(ca, home, monkeypatch, capsys):
    _in_tmux(ca, monkeypatch)
    note_id = ca.S.create_note("deck", "fyi")
    rc = ca.main(["resolve", note_id, "done"])
    assert rc != 0
    assert capsys.readouterr().err


def test_resolve_refuses_an_empty_outcome(ca, home, monkeypatch, capsys):
    card_id = _ask_id(ca, monkeypatch, capsys)
    rc = ca.main(["resolve", card_id, "   "])
    assert rc == 2
    assert _board(ca)["cards"][0]["state"] == "open"


def test_resolving_an_already_resolved_ask_is_refused(ca, home, monkeypatch, capsys):
    card_id = _ask_id(ca, monkeypatch, capsys)
    assert ca.main(["resolve", card_id, "done"]) == 0
    capsys.readouterr()
    rc = ca.main(["resolve", card_id, "again"])
    assert rc != 0
    assert capsys.readouterr().err


# --- list --------------------------------------------------------------------


def test_list_prints_open_and_answered_but_not_resolved(ca, home, monkeypatch, capsys):
    _in_tmux(ca, monkeypatch)
    open_id = ca.S.post_ask("deck", "still open?")
    answered_id = ca.S.post_ask("deck", "answered one?")
    ca.S.answer_ask("deck", answered_id, "yes")
    resolved_id = ca.S.post_ask("deck", "resolved one?")
    ca.S.resolve_ask("deck", resolved_id, "done")

    rc = ca.main(["list"])
    assert rc == 0
    lines = capsys.readouterr().out.splitlines()
    rows = {line.split("\t")[0]: line.split("\t")[1] for line in lines}
    assert rows[open_id] == "open"
    assert rows[answered_id] == "answered"
    assert resolved_id not in rows


def test_list_is_empty_when_there_are_no_open_asks(ca, home, monkeypatch, capsys):
    _in_tmux(ca, monkeypatch)
    rc = ca.main(["list"])
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_list_escapes_tabs_and_newlines_in_the_question(ca, home, monkeypatch, capsys):
    _in_tmux(ca, monkeypatch)
    ca.S.post_ask("deck", "line one\tand\ttabs\nand a newline")
    rc = ca.main(["list"])
    assert rc == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    fields = lines[0].split("\t")
    assert len(fields) == 3
    assert fields[2] == "line one\\tand\\ttabs\\nand a newline"


def test_list_escapes_a_literal_backslash_too(ca, home, monkeypatch, capsys):
    _in_tmux(ca, monkeypatch)
    ca.S.post_ask("deck", r"a \t that is not a tab")
    assert ca.main(["list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    fields = lines[0].split("\t")
    assert len(fields) == 3
    assert fields[2] == r"a \\t that is not a tab"
