"""`bin/session-agent`'s own argv/dispatch layer, as opposed to the state
machinery it calls into (tested against the `SS`/`SI` fixtures in
`test_session_verbs.py`).

THE `--follow` PRINTING LOOP (`_follow`/`_emit`). 101 orphaned `session-agent
state <name> --follow` processes were found live on the mini on 2026-08-12,
some running for days. Deck starts one follower per open pane over ssh; when
Deck quits, a pane closes, or the connection drops, the ssh channel dies and
the remote follower is left running. `session_state.follow_states` closes
half of that: a per-tick check that exits once this process has been
reparented to launchd. This file covers the other half, which lives here
because it is about the WRITE side, not the read side: a session that keeps
changing state still hits `BrokenPipeError` on its next `_emit`, because the
pipe closed out from under it before the parent died. `_follow` must catch
that and exit quietly rather than let it escape as a traceback.

THE `rollover`/`rollover-policy` VERBS (Slice 3, ADR-0069's write side). What
these pin is the DISPATCH — the right underlying call gets the right
argument, and `rollover-policy`'s `PolicyError` becomes a loud, non-zero exit
rather than a swallowed one. The write side's own logic (what each action
does to the override file, the composer-draft and held-queue refusals) is
`session_state.rollover_command`, tested in `test_session_verbs.py`; the
policy validation is `rollover_policy.apply_policy_updates`, tested in
`test_rollover_policy.py`.
"""
from __future__ import annotations

import os
import types

import pytest


@pytest.fixture
def emitted(session_agent_cli, monkeypatch):
    """Replace `_emit` with a list that records what was printed, instead of
    actually printing — keeps these tests off real stdout."""
    calls = []
    monkeypatch.setattr(session_agent_cli, "_emit", calls.append)
    return calls


def test_follow_prints_every_state_the_generator_yields(session_agent_cli, emitted, monkeypatch):
    monkeypatch.setattr(
        session_agent_cli.SS, "follow_states",
        lambda name, interval: iter([{"state": "idle"}, {"state": "working"}]))

    rc = session_agent_cli._follow("deck", 0)

    assert rc == 0
    assert emitted == [{"state": "idle"}, {"state": "working"}]


def test_follow_returns_cleanly_when_the_generator_just_ends(session_agent_cli, emitted, monkeypatch):
    """The orphan-exit path in `follow_states` ends the generator normally —
    no exception, so there is nothing here to recover from."""
    monkeypatch.setattr(
        session_agent_cli.SS, "follow_states",
        lambda name, interval: iter([{"state": "idle"}]))

    rc = session_agent_cli._follow("deck", 0)

    assert rc == 0
    assert emitted == [{"state": "idle"}]


def test_follow_catches_a_broken_pipe_without_a_traceback(session_agent_cli, monkeypatch):
    """A reader gone mid-session — Deck quit, the pane closed — surfaces as
    `BrokenPipeError` on the next write, since a quiet session emits nothing
    for hours and so cannot discover a dead reader any sooner. `_follow` must
    not let it propagate."""
    def two_states(name, interval):
        yield {"state": "idle"}
        yield {"state": "working"}  # never reached: the first _emit raises

    def raising_emit(obj):
        raise BrokenPipeError()

    recovered = {"called": False}

    monkeypatch.setattr(session_agent_cli.SS, "follow_states", two_states)
    monkeypatch.setattr(session_agent_cli, "_emit", raising_emit)
    monkeypatch.setattr(session_agent_cli, "_recover_from_broken_pipe",
                         lambda: recovered.__setitem__("called", True))

    rc = session_agent_cli._follow("deck", 0)

    assert rc == 0, "a dead reader is this loop's normal way to end, not a failure"
    assert recovered["called"]


def test_follow_does_not_recover_a_pipe_that_never_broke(session_agent_cli, emitted, monkeypatch):
    recovered = {"called": False}
    monkeypatch.setattr(
        session_agent_cli.SS, "follow_states",
        lambda name, interval: iter([{"state": "idle"}]))
    monkeypatch.setattr(session_agent_cli, "_recover_from_broken_pipe",
                         lambda: recovered.__setitem__("called", True))

    session_agent_cli._follow("deck", 0)

    assert recovered["called"] is False


def test_recover_from_broken_pipe_closes_and_repoints_stdout(session_agent_cli, monkeypatch):
    """So nothing later in the process — including Python's own shutdown
    flush — re-raises on the same dead pipe."""
    class FakeStdout:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    fake = FakeStdout()
    monkeypatch.setattr(session_agent_cli.sys, "stdout", fake)

    session_agent_cli._recover_from_broken_pipe()

    assert fake.closed
    assert session_agent_cli.sys.stdout is not fake
    assert session_agent_cli.sys.stdout.name == os.devnull


def test_recover_from_broken_pipe_survives_a_stdout_that_wont_close(session_agent_cli, monkeypatch):
    """A close() failing on an already-broken pipe must not stop the repoint —
    the whole point is to make the NEXT write safe."""
    class HostileStdout:
        def close(self):
            raise OSError("already gone")

    monkeypatch.setattr(session_agent_cli.sys, "stdout", HostileStdout())

    session_agent_cli._recover_from_broken_pipe()  # must not raise

    assert session_agent_cli.sys.stdout.name == os.devnull


# MARK: - `crews-root`


def test_crews_root_emits_the_library_value(session_agent_cli, emitted, monkeypatch):
    monkeypatch.setattr(session_agent_cli.SL, "creatable_root", lambda: "/srv/convivy")
    monkeypatch.setattr(session_agent_cli.sys, "argv", ["session-agent", "crews-root"])

    rc = session_agent_cli.main()

    assert rc == 0
    assert emitted == [{"root": "/srv/convivy"}]


def test_crews_root_takes_no_arguments(session_agent_cli, monkeypatch):
    monkeypatch.setattr(session_agent_cli.SL, "creatable_root", lambda: "/srv/convivy")
    monkeypatch.setattr(session_agent_cli, "_emit", lambda obj: None)
    monkeypatch.setattr(session_agent_cli.sys, "argv",
                         ["session-agent", "crews-root", "unexpected"])

    with pytest.raises(SystemExit):
        session_agent_cli.main()


# MARK: - `ensure-server`
#
# The helper's own behaviour is tested in `test_session_lifecycle.py`. What
# belongs here is the verb: that it calls the helper, exits 0 when it returns,
# and turns a refusal into a non-zero exit with the reason on stderr. The ssh
# aliases chain `attach || new` on that exit code, so a silent 0 would let a
# refused server still create a session.


def test_ensure_server_exits_0_when_the_helper_returns(session_agent_cli, emitted, monkeypatch):
    calls = []
    monkeypatch.setattr(session_agent_cli.SL, "ensure_default_server",
                        lambda *a, **kw: calls.append(True))
    monkeypatch.setattr(session_agent_cli.sys, "argv", ["session-agent", "ensure-server"])

    rc = session_agent_cli.main()

    assert rc == 0
    assert calls == [True]
    assert emitted == [{"ok": True}]


def test_ensure_server_refusal_exits_1_with_the_reason_on_stderr(
        session_agent_cli, emitted, monkeypatch, capsys):
    def refused(*a, **kw):
        raise session_agent_cli.SL.TmuxServerError(
            "the keeper session did not appear within 10 seconds")

    monkeypatch.setattr(session_agent_cli.SL, "ensure_default_server", refused)
    monkeypatch.setattr(session_agent_cli.sys, "argv", ["session-agent", "ensure-server"])

    rc = session_agent_cli.main()

    assert rc == 1
    assert emitted and emitted[0]["ok"] is False
    assert "did not appear" in capsys.readouterr().err


# MARK: - `start`'s --create-dir wiring


def test_create_dir_flag_reaches_session_lifecycle_start(session_agent_cli, monkeypatch):
    """The CLI flag is the only way a caller can opt in; if argparse or the
    call site drops it, `SL.start` silently falls back to the always-refuse
    default and Deck's New Project flow breaks again."""
    calls = []
    monkeypatch.setattr(session_agent_cli.SL, "start",
                         lambda *a, **kw: calls.append(kw) or {"ok": True})
    monkeypatch.setattr(session_agent_cli, "_emit", lambda obj: None)
    monkeypatch.setattr(session_agent_cli.sys, "argv",
                         ["session-agent", "start", "nyh", "--dir", "/tmp/x",
                          "--create-dir"])

    rc = session_agent_cli.main()

    assert rc == 0
    assert calls == [{"directory": "/tmp/x", "command": None, "create_dir": True}]


def test_without_the_flag_create_dir_defaults_false(session_agent_cli, monkeypatch):
    calls = []
    monkeypatch.setattr(session_agent_cli.SL, "start",
                         lambda *a, **kw: calls.append(kw) or {"ok": True})
    monkeypatch.setattr(session_agent_cli, "_emit", lambda obj: None)
    monkeypatch.setattr(session_agent_cli.sys, "argv",
                         ["session-agent", "start", "nyh", "--dir", "/tmp/x"])

    session_agent_cli.main()

    assert calls == [{"directory": "/tmp/x", "command": None, "create_dir": False}]


# MARK: - `read-file` / `write-file`
#
# The containment itself (symlinks, atomicity, the passwd-vs-$HOME anchor) is
# `session_files`'s own job and is tested directly in `test_session_files.py`.
# What belongs here is the CLI wiring: that `read-file`'s stdout carries raw
# bytes and nothing else, that a refusal never touches stdout on that verb
# (JSON there would be indistinguishable from a file that starts with `{`),
# and that `write-file` reads stdin and reports through the ordinary `_emit`
# contract like every other verb.


def _fake_binary_stream(written=None, to_read=b""):
    """A stand-in for `sys.stdout`/`sys.stdin`, whose `.buffer` is a
    read-only property under pytest's capture — this repoints the whole
    stream object rather than trying to patch that attribute directly."""
    sink = written if written is not None else []
    return types.SimpleNamespace(
        buffer=types.SimpleNamespace(
            write=sink.append, flush=lambda: None, read=lambda: to_read))


def test_read_file_writes_raw_bytes_to_stdout_and_returns_0(
        session_agent_cli, monkeypatch):
    monkeypatch.setattr(session_agent_cli.SF, "read_file",
                         lambda path: b"exact bytes, not JSON")
    monkeypatch.setattr(session_agent_cli.sys, "argv",
                         ["session-agent", "read-file", "/srv/convivy/notes.md"])
    written = []
    monkeypatch.setattr(session_agent_cli.sys, "stdout", _fake_binary_stream(written))

    rc = session_agent_cli.main()

    assert rc == 0
    assert written == [b"exact bytes, not JSON"]


def test_read_file_refusal_prints_to_stderr_only_never_stdout(
        session_agent_cli, monkeypatch, capsys):
    def refuse(path):
        raise session_agent_cli.SF.FileAccessError(f"outside the admitted roots: {path!r}")

    monkeypatch.setattr(session_agent_cli.SF, "read_file", refuse)
    monkeypatch.setattr(session_agent_cli.sys, "argv",
                         ["session-agent", "read-file", "/etc/passwd"])
    stdout_written = []
    monkeypatch.setattr(session_agent_cli.sys, "stdout", _fake_binary_stream(stdout_written))

    rc = session_agent_cli.main()

    assert rc == 1
    assert stdout_written == []
    assert "outside the admitted roots" in capsys.readouterr().err


def test_write_file_reads_stdin_and_calls_session_files_write(
        session_agent_cli, monkeypatch, emitted):
    calls = []
    monkeypatch.setattr(session_agent_cli.SF, "write_file",
                         lambda path, content: calls.append((path, content)))
    monkeypatch.setattr(session_agent_cli.sys, "argv",
                         ["session-agent", "write-file", "/srv/convivy/notes.md"])
    monkeypatch.setattr(session_agent_cli.sys, "stdin",
                         _fake_binary_stream(to_read=b"new content"))

    rc = session_agent_cli.main()

    assert rc == 0
    assert calls == [("/srv/convivy/notes.md", b"new content")]
    assert emitted == [{"ok": True, "path": "/srv/convivy/notes.md", "bytes": len(b"new content")}]


def test_write_file_refusal_is_reported_through_the_shared_error_contract(
        session_agent_cli, monkeypatch, emitted, capsys):
    """Unlike `read-file`, `write-file` has no byte-stream purity constraint
    on stdout, so its refusal goes through the same `_emit` + stderr + exit 1
    path every other verb uses."""
    def refuse(path, content):
        raise session_agent_cli.SF.FileAccessError(f"cannot write {path!r}: boom")

    monkeypatch.setattr(session_agent_cli.SF, "write_file", refuse)
    monkeypatch.setattr(session_agent_cli.sys, "argv",
                         ["session-agent", "write-file", "/srv/convivy/notes.md"])
    monkeypatch.setattr(session_agent_cli.sys, "stdin", _fake_binary_stream(to_read=b"content"))

    rc = session_agent_cli.main()

    assert rc == 1
    assert emitted and emitted[0]["ok"] is False
    assert "cannot write" in capsys.readouterr().err


def test_crews_emits_the_envelope_the_module_builds(session_agent_cli, emitted, monkeypatch):
    """`session-agent crews` takes no name and answers about the host, the same
    as `sessions` and `crews-root`; the CLI's only job is to emit what
    session_crews.crews() returns, once, as the envelope Deck decodes."""
    monkeypatch.setattr(session_agent_cli.SCR, "crews",
                        lambda: {"crews": [{"id": "placard"}]})
    monkeypatch.setattr(session_agent_cli.sys, "argv", ["session-agent", "crews"])

    rc = session_agent_cli.main()

    assert rc == 0
    assert emitted == [{"crews": [{"id": "placard"}]}]


# MARK: - `documents`


def test_documents_resolves_cwd_the_same_way_commands_does_and_reports_it(
        session_agent_cli, emitted, monkeypatch):
    """The CLI's own job is resolution plus the `session` key; the record
    itself is `session_documents`'s, tested directly in
    `test_session_documents.py` against constructed `.claude/` trees."""
    monkeypatch.setattr(session_agent_cli.SS, "session_state",
                        lambda name: {"cwd": "/repo/checkout"})
    monkeypatch.setattr(session_agent_cli.SD, "session_documents",
                        lambda cwd: {"profile": "agent-platform/daytime",
                                     "materialized_at": "2026-09-14T18:00:00+00:00",
                                     "documents": []})
    monkeypatch.setattr(session_agent_cli.sys, "argv",
                        ["session-agent", "documents", "deck"])

    rc = session_agent_cli.main()

    assert rc == 0
    assert emitted == [{"session": "deck", "profile": "agent-platform/daytime",
                        "materialized_at": "2026-09-14T18:00:00+00:00",
                        "documents": []}]


def test_documents_reports_no_such_session_when_state_cannot_resolve_a_cwd(
        session_agent_cli, emitted, monkeypatch):
    monkeypatch.setattr(session_agent_cli.SS, "session_state",
                        lambda name: {"cwd": None})
    monkeypatch.setattr(session_agent_cli.SD, "session_documents",
                        lambda cwd: {"reason": "no_such_session"})
    monkeypatch.setattr(session_agent_cli.sys, "argv",
                        ["session-agent", "documents", "gone"])

    rc = session_agent_cli.main()

    assert rc == 0, "a reporting verb signals absence via `reason`, not the exit code"
    assert emitted == [{"session": "gone", "reason": "no_such_session"}]


def test_documents_verb_survives_a_non_utf8_record_and_exits_0(
        session_agent_cli, emitted, monkeypatch, tmp_path):
    """Integration: unlike the other `documents` CLI tests, this leaves
    `SD.session_documents` unstubbed so the real read hits a real non-UTF-8
    `.profile-documents.json` on disk — the crash reviewers reproduced
    (`UnicodeDecodeError` escaping past `bin/session-agent`'s `documents` verb,
    which unlike `commands` doesn't wrap its lower-layer call in try/except)
    must instead come back as the CLI's ordinary `reason` envelope, exit 0."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    claude_dir.joinpath(".active-profile").write_text("agent-platform/daytime\n")
    claude_dir.joinpath(".profile-documents.json").write_bytes(b"\xff\xfe\x00\x01broken")

    monkeypatch.setattr(session_agent_cli.SS, "session_state",
                        lambda name: {"cwd": str(tmp_path)})
    monkeypatch.setattr(session_agent_cli.sys, "argv",
                        ["session-agent", "documents", "deck"])

    rc = session_agent_cli.main()

    assert rc == 0
    assert emitted == [{"session": "deck", "reason": "unreadable_record"}]


def test_documents_survives_session_state_raising(session_agent_cli, emitted, monkeypatch):
    """Same degrade as `commands`: a session whose state cannot be read at all
    is reported as unresolved rather than taking the whole verb down."""
    def raise_state(name):
        raise RuntimeError("boom")

    monkeypatch.setattr(session_agent_cli.SS, "session_state", raise_state)
    monkeypatch.setattr(session_agent_cli.SD, "session_documents",
                        lambda cwd: {"reason": "no_such_session"} if cwd is None
                        else {"documents": []})
    monkeypatch.setattr(session_agent_cli.sys, "argv",
                        ["session-agent", "documents", "gone"])

    rc = session_agent_cli.main()

    assert rc == 0
    assert emitted == [{"session": "gone", "reason": "no_such_session"}]


# --- `rollover` and `rollover-policy` (Slice 3, ADR-0069's write side) -----

def test_rollover_verb_dispatches_to_rollover_command_with_the_action(
        session_agent_cli, emitted, monkeypatch):
    calls = []
    monkeypatch.setattr(session_agent_cli.SS, "rollover_command",
                        lambda name, action: calls.append((name, action)) or
                        {"outcome": "armed", "reason": None, "rollover": {}})
    monkeypatch.setattr(session_agent_cli.sys, "argv",
                        ["session-agent", "rollover", "deck", "veto"])

    rc = session_agent_cli.main()

    assert rc == 0
    assert calls == [("deck", "veto")]
    assert emitted == [{"outcome": "armed", "reason": None, "rollover": {}}]


def test_rollover_verb_refuses_an_unrecognised_action(session_agent_cli, monkeypatch, capsys):
    monkeypatch.setattr(session_agent_cli.sys, "argv",
                        ["session-agent", "rollover", "deck", "nonsense"])
    with pytest.raises(SystemExit):
        session_agent_cli.main()
    assert "invalid choice" in capsys.readouterr().err


def test_rollover_policy_with_no_set_prints_the_effective_policy(
        session_agent_cli, emitted, monkeypatch):
    monkeypatch.setattr(session_agent_cli.RP, "load_policy",
                        lambda *a, **k: {"mode": "observe", "seek_pct": 70,
                                         "floor_pct": None, "seek_call_cap": None})
    monkeypatch.setattr(session_agent_cli.sys, "argv", ["session-agent", "rollover-policy"])

    rc = session_agent_cli.main()

    assert rc == 0
    assert emitted == [{"mode": "observe", "seek_pct": 70, "floor_pct": None,
                        "seek_call_cap": None}]


def test_rollover_policy_set_validates_applies_and_saves(session_agent_cli, emitted, monkeypatch):
    saved = {}
    monkeypatch.setattr(session_agent_cli.RP, "load_policy",
                        lambda *a, **k: dict(session_agent_cli.RP.DEFAULT_POLICY))
    monkeypatch.setattr(session_agent_cli.RP, "save_policy",
                        lambda policy, path=None: saved.update(policy))
    monkeypatch.setattr(session_agent_cli.sys, "argv",
                        ["session-agent", "rollover-policy", "--set", "seek_pct=55"])

    rc = session_agent_cli.main()

    assert rc == 0
    assert saved["seek_pct"] == 55
    assert emitted == [dict(session_agent_cli.RP.DEFAULT_POLICY)]


def test_rollover_policy_set_refuses_a_floor_at_or_below_seek(
        session_agent_cli, emitted, monkeypatch, capsys):
    """The refusal is loud and on the command line — `load_policy`'s own
    per-field degrade (for the HOOK's benefit) is a separate, more forgiving
    path this verb deliberately does not take; see `rollover_policy`'s
    "editing the policy" section."""
    monkeypatch.setattr(session_agent_cli.RP, "load_policy",
                        lambda *a, **k: dict(session_agent_cli.RP.DEFAULT_POLICY))
    monkeypatch.setattr(session_agent_cli.sys, "argv",
                        ["session-agent", "rollover-policy",
                         "--set", "seek_pct=80", "--set", "floor_pct=80"])

    rc = session_agent_cli.main()

    assert rc == 1
    assert "floor_pct" in capsys.readouterr().err
    assert emitted and emitted[0]["ok"] is False


# --- `held-items` (Slice 7, ADR-0069): JSON by default, markdown behind a flag --


def test_held_items_emits_json_by_default(session_agent_cli, emitted, monkeypatch):
    calls = []
    monkeypatch.setattr(
        session_agent_cli.SS, "held_items",
        lambda name: calls.append(name) or
        {"outcome": "ok", "draft": None, "held_messages": [], "rollover": {"state": "none"}})
    monkeypatch.setattr(session_agent_cli.sys, "argv", ["session-agent", "held-items", "deck"])

    rc = session_agent_cli.main()

    assert rc == 0
    assert calls == ["deck"]
    assert emitted == [
        {"outcome": "ok", "draft": None, "held_messages": [], "rollover": {"state": "none"}}]


def test_held_items_markdown_prints_the_pasteable_block_instead_of_json(
        session_agent_cli, emitted, monkeypatch, capsys):
    monkeypatch.setattr(
        session_agent_cli.SS, "held_items",
        lambda name: {"outcome": "ok", "draft": "half-typed",
                      "held_messages": [{"content": "hi", "queued_at": "2026-09-15T00:00:00Z"}],
                      "rollover": {"state": "seeking"}})
    monkeypatch.setattr(session_agent_cli.sys, "argv",
                        ["session-agent", "held-items", "deck", "--markdown"])

    rc = session_agent_cli.main()

    assert rc == 0
    assert emitted == [], "the markdown form must not also emit JSON"
    out = capsys.readouterr().out
    assert "Composer draft (unsent, in the pane): 'half-typed'" in out
    assert "[2026-09-15T00:00:00Z] 'hi'" in out
    assert "Rollover state: seeking" in out


def test_held_items_markdown_is_explicit_when_nothing_is_held(
        session_agent_cli, emitted, monkeypatch, capsys):
    monkeypatch.setattr(
        session_agent_cli.SS, "held_items",
        lambda name: {"outcome": "ok", "draft": None, "held_messages": [],
                      "rollover": {"state": "none"}})
    monkeypatch.setattr(session_agent_cli.sys, "argv",
                        ["session-agent", "held-items", "deck", "--markdown"])

    rc = session_agent_cli.main()

    assert rc == 0
    out = capsys.readouterr().out
    assert "Composer draft: none" in out
    assert "Queued messages: none" in out
