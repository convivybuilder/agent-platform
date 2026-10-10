"""bin/co-rollover — `_cmd_spawn`/`_cmd_picked_up`'s own ordering and argv
logic (ADR-0069 Slice 5, PR #566 round 2).

Loaded as a module via the `co_rollover_cli` fixture (`tests/conftest.py`'s
`_load_cli`), so these exercise the SCRIPT's own functions rather than only
the `co_rollover.py`/`rollover_policy.py` library calls they make. Every
tmux-facing and filesystem-facing call is monkeypatched — nothing here
touches a real tmux server, subprocess, or `~/.cache`.
"""
from __future__ import annotations

import argparse

import pytest


@pytest.fixture
def spawn_env(co_rollover_cli, monkeypatch, tmp_path):
    """A `_cmd_spawn` call wired for a clean success, with every step
    monkeypatched to a recording stub — each test overrides exactly the one
    call it wants to fail. Returns `(mod, calls)`, where `calls` records the
    order `write_side_channel`/`mark_rolling` actually ran in.
    """
    mod = co_rollover_cli
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "pred-1")
    monkeypatch.setattr(mod.CR, "git_toplevel", lambda: str(tmp_path))

    handoff_dir = tmp_path / "handoff"
    handoff_dir.mkdir()
    (handoff_dir / "handoff.md").write_text("# handoff\n")
    monkeypatch.setattr(mod, "_handoff_dir", lambda product: handoff_dir)

    monkeypatch.setattr(mod.CR, "current_session_and_pane", lambda: ("deck", "%1"))
    monkeypatch.setattr(mod.CR, "pane_identity",
                        lambda pane_id: {"pane_pid": 111, "tmux_session_id": "$0"})
    # No predecessor claude pid resolves by default — individual tests below
    # override this to pin what `spawn_successor` is called with.
    monkeypatch.setattr(mod.CR, "predecessor_claude_pids", lambda pane_pid: [])
    monkeypatch.setattr(mod.CR, "spawn_successor",
                        lambda *a, **k: {"window_index": 2, "pane_id": "%2"})
    monkeypatch.setattr(mod.CR, "verify_up",
                        lambda *a, **k: (True, "remote-control is active"))
    monkeypatch.setattr(mod.CR, "select_window", lambda *a, **k: None)

    calls = []
    monkeypatch.setattr(mod.CR, "write_side_channel",
                        lambda *a, **k: calls.append("write_side_channel"))
    monkeypatch.setattr(mod.RP, "mark_rolling",
                        lambda *a, **k: calls.append("mark_rolling"))
    monkeypatch.setattr(mod.CR, "clear_remain_on_exit", lambda *a, **k: True)
    # Slice 6: every failure path now logs a signal and parks. Default to
    # inert no-op stubs so a test that only cares about the SUCCESS path (or
    # about some other failure detail) never reaches the real ops plane —
    # individual Slice 6 tests below replace these with recording stubs.
    monkeypatch.setattr(mod.CR, "pane_window_index", lambda pane_id: None)
    monkeypatch.setattr(mod.SC, "post_alert", lambda *a, **k: "skip")
    monkeypatch.setattr(mod.RP, "mark_parked", lambda *a, **k: {})
    # A cwd outside every private crew, so a park's signal is logged. The registry is not read here;
    # the black-box gate has its own test below.
    monkeypatch.setattr(mod, "private_home_for", lambda cwd: None)
    return mod, calls


def test_the_side_channel_is_written_before_mark_rolling(spawn_env):
    mod, calls = spawn_env
    rc = mod._cmd_spawn(argparse.Namespace(product="repo"))
    assert rc == 0
    assert calls == ["write_side_channel", "mark_rolling"]


def test_a_write_side_channel_failure_never_calls_mark_rolling(spawn_env, monkeypatch):
    """THE BUG THIS CLOSES: with the side channel written first, a failure
    there must leave the predecessor unfenced — `mark_rolling` (which
    nothing then clears automatically; see `rollover_policy.mark_rolling`'s
    docstring) must never run."""
    mod, calls = spawn_env

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(mod.CR, "write_side_channel", boom)

    with pytest.raises(OSError, match="disk full"):
        mod._cmd_spawn(argparse.Namespace(product="repo"))
    assert calls == [], "mark_rolling must not run when the side channel write failed"


def test_a_successful_spawn_clears_remain_on_exit_after_verify_up(spawn_env, monkeypatch):
    """Round 4, agent-platform#566: `spawn_successor` sets `remain-on-exit on`
    so a failed spawn stays diagnosable, but nothing ever turned it back off
    — a permanent dead leaf on Deck's rail once a successful successor's
    session later ends normally. The off call must run once the successor is
    confirmed up (recorded here via `select_window`, which only runs on that
    path), targeting the exact window `spawn_successor` reported."""
    mod, calls = spawn_env
    seen = []
    monkeypatch.setattr(mod.CR, "clear_remain_on_exit",
                        lambda session_name, window_index: seen.append(
                            (session_name, window_index)) or True)

    rc = mod._cmd_spawn(argparse.Namespace(product="repo"))
    assert rc == 0
    assert seen == [("deck", 2)], "clear_remain_on_exit must target the successor's own window"


def test_a_failed_spawn_does_not_clear_remain_on_exit(spawn_env, monkeypatch):
    mod, calls = spawn_env
    monkeypatch.setattr(mod.CR, "verify_up", lambda *a, **k: (False, "still zsh\n"))
    cleared = []
    monkeypatch.setattr(mod.CR, "clear_remain_on_exit",
                        lambda *a, **k: cleared.append(1) or True)

    rc = mod._cmd_spawn(argparse.Namespace(product="repo"))
    assert rc == 1
    assert cleared == [], "a failed spawn must never reach the remain-on-exit-off call"


def test_a_remain_on_exit_clear_failure_does_not_fail_the_rollover(spawn_env, monkeypatch, capsys):
    mod, calls = spawn_env
    monkeypatch.setattr(mod.CR, "clear_remain_on_exit", lambda *a, **k: False)

    rc = mod._cmd_spawn(argparse.Namespace(product="repo"))
    assert rc == 0, "the off call's own failure must not fail an otherwise-successful rollover"
    assert calls == ["write_side_channel", "mark_rolling"], \
        "the rest of the spawn sequence must still complete"
    assert "remain-on-exit" in capsys.readouterr().err


def test_spawn_passes_the_resolved_predecessor_claude_pids_to_spawn_successor(
        spawn_env, monkeypatch):
    """`_cmd_spawn` resolves the predecessor's own claude pids from the pane
    pid `pane_identity` already recorded, and passes them straight through to
    `spawn_successor` as `exempt_pids` (issue_01M2FDBJS8GMA16DX4XNBVH2K6)."""
    mod, calls = spawn_env
    monkeypatch.setattr(mod.CR, "predecessor_claude_pids",
                        lambda pane_pid: [9999] if pane_pid == 111 else [])
    seen = {}
    monkeypatch.setattr(mod.CR, "spawn_successor",
                        lambda *a, **k: seen.update(k) or {"window_index": 2, "pane_id": "%2"})

    rc = mod._cmd_spawn(argparse.Namespace(product="repo"))
    assert rc == 0
    assert seen.get("exempt_pids") == [9999]


def test_spawn_passes_every_resolved_predecessor_claude_pid(spawn_env, monkeypatch):
    """More than one claude pid can legitimately share the predecessor's
    pane (the computer-use-fork shape); all of them must reach
    `spawn_successor`, not just the first."""
    mod, calls = spawn_env
    monkeypatch.setattr(mod.CR, "predecessor_claude_pids",
                        lambda pane_pid: [111, 222] if pane_pid == 111 else [])
    seen = {}
    monkeypatch.setattr(mod.CR, "spawn_successor",
                        lambda *a, **k: seen.update(k) or {"window_index": 2, "pane_id": "%2"})

    rc = mod._cmd_spawn(argparse.Namespace(product="repo"))
    assert rc == 0
    assert seen.get("exempt_pids") == [111, 222]


def test_spawn_passes_no_exempt_pids_when_none_resolves(spawn_env, monkeypatch):
    mod, calls = spawn_env
    monkeypatch.setattr(mod.CR, "predecessor_claude_pids", lambda pane_pid: [])
    seen = {}
    monkeypatch.setattr(mod.CR, "spawn_successor",
                        lambda *a, **k: seen.update(k) or {"window_index": 2, "pane_id": "%2"})

    rc = mod._cmd_spawn(argparse.Namespace(product="repo"))
    assert rc == 0
    assert seen.get("exempt_pids") == []


# --- _cmd_spawn: the predecessor's permission mode (ADR-0069, 2026-09-16) --


def test_spawn_passes_the_predecessors_current_permission_mode_to_spawn_successor(
        spawn_env, monkeypatch):
    """`_cmd_spawn` reads the predecessor's CURRENT mode off `rollover_policy.
    read_state` — kept fresh by `tools/rollover-trigger.py`'s `PreToolUse`
    branch, which fires for this very invocation before `spawn_successor`
    ever runs — and passes it straight through."""
    mod, calls = spawn_env
    monkeypatch.setattr(mod.RP, "read_state",
                        lambda session_id, *a, **k: {"permission_mode": "plan"})
    seen = {}
    monkeypatch.setattr(mod.CR, "spawn_successor",
                        lambda *a, **k: seen.update(k) or {"window_index": 2, "pane_id": "%2"})

    rc = mod._cmd_spawn(argparse.Namespace(product="repo"))
    assert rc == 0
    assert seen.get("permission_mode") == "plan"


def test_spawn_passes_none_when_no_permission_mode_is_on_record(spawn_env, monkeypatch):
    """No PreToolUse firing has ever recorded a mode for this session (or the
    hook never fired at all) — `read_state`'s own default reports `None`, and
    that must reach `spawn_successor` unchanged so `bin/co` falls back to
    bypassPermissions, exactly today's behaviour."""
    mod, calls = spawn_env
    monkeypatch.setattr(mod.RP, "read_state",
                        lambda session_id, *a, **k: {"permission_mode": None})
    seen = {}
    monkeypatch.setattr(mod.CR, "spawn_successor",
                        lambda *a, **k: seen.update(k) or {"window_index": 2, "pane_id": "%2"})

    rc = mod._cmd_spawn(argparse.Namespace(product="repo"))
    assert rc == 0
    assert "permission_mode" in seen, "must be passed explicitly, not merely absent"
    assert seen["permission_mode"] is None


def test_a_pane_identity_failure_refuses_before_spawning_anything(
        co_rollover_cli, monkeypatch, tmp_path):
    mod = co_rollover_cli
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "pred-1")
    monkeypatch.setattr(mod.CR, "current_session_and_pane", lambda: ("deck", "%1"))
    monkeypatch.setattr(mod.CR, "pane_identity", lambda pane_id: None)
    spawned = []
    monkeypatch.setattr(mod.CR, "spawn_successor", lambda *a, **k: spawned.append(1))

    rc = mod._cmd_spawn(argparse.Namespace(product="repo"))
    assert rc == 1
    assert spawned == [], "an unresolvable predecessor identity must refuse before spawning"


# --- Slice 6: a failed spawn parks and files one operator issue ------------


def _stub_park_reporting(mod, monkeypatch, *, post_status):
    """Wires `SC.post_alert`/`RP.mark_parked` to recording stubs and returns
    `(posted, parked)`, the kwargs each was called with. `post_status` is the
    status the signal call answers. `pane_window_index` is stubbed too — nothing
    in this file touches a real tmux server."""
    monkeypatch.setattr(mod.CR, "pane_window_index", lambda pane_id: 1)
    posted = {}

    def fake_post_alert(stream, body, *, product="platform", dedup_key=None, **kwargs):
        posted.update(stream=stream, body=body, product=product, dedup_key=dedup_key,
                      timeout=kwargs.get("timeout"))
        return post_status

    monkeypatch.setattr(mod.SC, "post_alert", fake_post_alert)
    parked = {}

    def fake_mark_parked(session_id, **kwargs):
        parked["session_id"] = session_id
        parked.update(kwargs)
        return {}

    monkeypatch.setattr(mod.RP, "mark_parked", fake_mark_parked)
    return posted, parked


def test_a_spawn_successor_failure_parks_and_logs_an_operator_signal(
        spawn_env, monkeypatch, capsys):
    mod, calls = spawn_env
    monkeypatch.setattr(mod.CR, "spawn_successor", lambda *a, **k: (_ for _ in ()).throw(
        mod.CR.CoRolloverError("tmux new-window failed to create the successor's window")))
    posted, parked = _stub_park_reporting(
        mod, monkeypatch, post_status="created")

    rc = mod._cmd_spawn(argparse.Namespace(product="repo"))

    assert rc == 1
    assert posted["stream"] == "co-rollover-park"
    assert posted["timeout"] <= 15, "a park must not wait out the delivery budget"
    assert "tmux new-window failed" in posted["body"]
    assert parked["session_id"] == "pred-1"
    assert parked["reason"] == mod._REASON_SPAWN_ERROR
    assert parked["issue_id"] is None
    assert parked["issue_status"] == "created"
    assert "logged a signal" in capsys.readouterr().err


def test_a_verify_up_failure_parks_and_logs_a_signal_with_the_captured_pane_text(
        spawn_env, monkeypatch, capsys):
    mod, calls = spawn_env
    monkeypatch.setattr(mod.CR, "verify_up",
                        lambda *a, **k: (False, "line one\nFAILED: boom\n"))
    posted, parked = _stub_park_reporting(
        mod, monkeypatch, post_status="created")

    rc = mod._cmd_spawn(argparse.Namespace(product="repo"))

    assert rc == 1
    assert "line one" in posted["body"]
    assert "FAILED: boom" in posted["body"]
    assert parked["reason"] == mod._REASON_VERIFY_UP_FAILED
    assert parked["issue_status"] == "created"
    assert "logged a signal" in capsys.readouterr().err


def test_a_retried_spawn_for_the_same_handoff_reuses_the_dedup_key(spawn_env, monkeypatch):
    """The dedup_key holds across a retry against the same predecessor session
    and the same unmodified handoff — the store answers the second call
    `duplicate` rather than logging a second signal."""
    mod, calls = spawn_env
    monkeypatch.setattr(mod.CR, "verify_up", lambda *a, **k: (False, "still zsh\n"))
    monkeypatch.setattr(mod.CR, "pane_window_index", lambda pane_id: 1)
    dedup_keys = []

    def fake_post_alert(stream, body, *, product="platform", dedup_key=None, **kwargs):
        dedup_keys.append(dedup_key)
        return "created" if len(dedup_keys) == 1 else "duplicate"

    monkeypatch.setattr(mod.SC, "post_alert", fake_post_alert)
    monkeypatch.setattr(mod.RP, "mark_parked", lambda *a, **k: {})

    rc1 = mod._cmd_spawn(argparse.Namespace(product="repo"))
    rc2 = mod._cmd_spawn(argparse.Namespace(product="repo"))

    assert rc1 == 1 and rc2 == 1
    assert len(dedup_keys) == 2
    assert dedup_keys[0] is not None
    assert dedup_keys[0] == dedup_keys[1], "same predecessor + same handoff must reuse the dedup_key"


def test_a_logging_failure_still_parks_and_reports(spawn_env, monkeypatch, capsys):
    """`signal_client.post_alert` never raises — a down loopback or a missing
    token both come back as a status. The park must still happen, and the
    operator must still be told directly."""
    mod, calls = spawn_env
    monkeypatch.setattr(mod.CR, "verify_up", lambda *a, **k: (False, "still zsh\n"))
    posted, parked = _stub_park_reporting(
        mod, monkeypatch, post_status="error:500")

    rc = mod._cmd_spawn(argparse.Namespace(product="repo"))

    assert rc == 1
    assert parked["issue_id"] is None
    assert parked["reason"] == mod._REASON_VERIFY_UP_FAILED
    assert parked["issue_status"] == "error:500"
    assert "could not log a signal" in capsys.readouterr().err.lower()


def test_a_logging_failure_names_the_status_instead_of_pointing_at_scrollback(
        spawn_env, monkeypatch, capsys):
    """THE MESSAGE USED TO SAY "(see above)" AND NOTHING WAS EVER ABOVE.
    `post_alert` returns its error and prints nothing, so the operator was sent
    to look for a diagnostic that did not exist — which is why the one real
    parked state on this machine sat for four days with `issue_id: None` and
    no recorded reason.

    Both assertions matter and neither implies the other: naming the status is
    what makes the failure diagnosable, and the absence of "see above" is what
    stops the message sending the reader somewhere empty."""
    mod, calls = spawn_env
    monkeypatch.setattr(mod.CR, "verify_up", lambda *a, **k: (False, "still zsh\n"))
    posted, parked = _stub_park_reporting(
        mod, monkeypatch, post_status="error:400")

    mod._cmd_spawn(argparse.Namespace(product="repo"))

    err = capsys.readouterr().err
    assert "error:400" in err, "the logging status is the only record of WHY it failed"
    assert "see above" not in err.lower(), \
        "nothing is ever printed above; pointing there is what hid this"
    assert parked["issue_status"] == "error:400", \
        "the state file must carry the reason too — stderr is gone by the time anyone looks"


def test_a_successful_log_records_its_status_too(spawn_env, monkeypatch):
    """Recorded on the success path as well, so a reader of the state can tell
    `created` from `duplicate` from a caller that never set the field. A field
    written only on failure would leave the success case indistinguishable from
    an old park, which is the ambiguity this whole change removes."""
    mod, calls = spawn_env
    monkeypatch.setattr(mod.CR, "verify_up", lambda *a, **k: (False, "still zsh\n"))
    posted, parked = _stub_park_reporting(
        mod, monkeypatch, post_status="created")

    mod._cmd_spawn(argparse.Namespace(product="repo"))

    assert parked["issue_id"] is None
    assert parked["issue_status"] == "created"


def test_the_park_records_the_product_string_it_submitted(spawn_env, monkeypatch):
    """`issue_product` records the `product` the signal was submitted under, and
    `product` here defaults to `basename(cwd)`. What is asserted is that the
    recorded value is the one actually passed to `post_alert`, rather than a
    value this function reconstructs afterwards. See
    `rollover_policy.DEFAULT_STATE["issue_product"]`."""
    mod, calls = spawn_env
    monkeypatch.setattr(mod.CR, "verify_up", lambda *a, **k: (False, "still zsh\n"))
    posted, parked = _stub_park_reporting(
        mod, monkeypatch, post_status="created")

    mod._cmd_spawn(argparse.Namespace(product="repo"))

    assert parked["issue_product"] == "repo"
    assert parked["issue_product"] == posted["product"], \
        "the recorded product must be the string actually passed to post_alert, " \
        "not one recomputed later"


def test_the_park_signal_carries_the_medium_severity_its_item_was_filed_at(spawn_env, monkeypatch):
    mod, calls = spawn_env
    monkeypatch.setattr(mod.CR, "verify_up", lambda *a, **k: (False, "still zsh\n"))
    seen = {}

    def fake_post_alert(stream, body, **kwargs):
        seen.update(kwargs)
        return "created"

    monkeypatch.setattr(mod.SC, "post_alert", fake_post_alert)
    mod._cmd_spawn(argparse.Namespace(product="repo"))

    assert seen["meta"] == {"severity": "medium"}


def test_a_park_inside_a_black_box_crew_logs_no_signal_and_says_so_in_the_state(
        spawn_env, monkeypatch, capsys):
    """ADR-0043: a black box crew logs no signals. The handoff and the successor's pane are the
    session's own text, so the gate comes before the post. The park still happens."""
    mod, calls = spawn_env
    monkeypatch.setattr(mod.CR, "verify_up", lambda *a, **k: (False, "still zsh\n"))
    monkeypatch.setattr(mod, "private_home_for", lambda cwd: "/crews/CFO")
    posted, parked = _stub_park_reporting(mod, monkeypatch, post_status="created")

    rc = mod._cmd_spawn(argparse.Namespace(product="repo"))

    assert rc == 1
    assert posted == {}, "a black box session's text reaches no signal"
    assert parked["reason"] == mod._REASON_VERIFY_UP_FAILED
    assert parked["issue_status"] == "black-box"
    err = capsys.readouterr().err
    assert "black-box crew, which logs no signal" in err
    assert "could not log" not in err, "a deliberate skip is not a failed log"


# --- _cmd_picked_up: skip a redundant close-out on a retry ------------------
#
# Round 2 (PR #588): `_cmd_picked_up` now reads `picked_up` itself off
# `read_side_channel`, BEFORE ever calling `mark_picked_up` — forwarding runs
# in between the two. `mark_picked_up` is mocked below only to supply
# `picked_up_this_call`, the fact that actually gates the close-out; the
# `picked_up` field on the side-channel dict returned by `read_side_channel`
# is the separate, earlier gate that decides whether forwarding runs at all.


def _side_channel_data(*, picked_up: bool, with_forwarding_identifiers: bool = True) -> dict:
    data = {
        "picked_up": picked_up,
        "predecessor": {"pane_id": "%1", "pane_pid": 111, "tmux_session_id": "$0",
                        "session_id": "pred-1"},
        "successor": {"pane_id": "%2", "session_id": None},
        "tmux_session": "deck",
    }
    if not with_forwarding_identifiers:
        data["successor"] = {"pane_id": None, "session_id": None}
    return data


def test_a_retried_picked_up_does_not_spawn_a_second_close_out(co_rollover_cli, monkeypatch):
    mod = co_rollover_cli
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "succ-1")
    monkeypatch.setattr(mod.CR, "read_side_channel",
                        lambda *a, **k: _side_channel_data(picked_up=True))

    def _must_not_be_called(*a, **k):
        raise AssertionError("a completed pickup must not forward or re-flip the gate")

    monkeypatch.setattr(mod.CR, "forward_late_messages", _must_not_be_called)
    monkeypatch.setattr(mod.CR, "mark_picked_up", _must_not_be_called)
    popens = []
    monkeypatch.setattr(mod.subprocess, "Popen", lambda *a, **k: popens.append((a, k)))

    rc = mod._cmd_picked_up(argparse.Namespace(picked_up="/tmp/handoff.md"))
    assert rc == 0
    assert popens == [], "a retried --picked-up must not spawn another close-out"


def test_the_first_picked_up_spawns_close_out_with_the_recorded_identity(
        co_rollover_cli, monkeypatch):
    mod = co_rollover_cli
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "succ-1")
    monkeypatch.setattr(mod.CR, "read_side_channel",
                        lambda *a, **k: _side_channel_data(picked_up=False))
    monkeypatch.setattr(mod.CR, "forward_late_messages", lambda *a, **k: (0, []))
    monkeypatch.setattr(mod.CR, "mark_picked_up",
                        lambda *a, **k: {"picked_up_this_call": True})
    popens = []
    monkeypatch.setattr(mod.subprocess, "Popen", lambda argv, **k: popens.append(argv))

    rc = mod._cmd_picked_up(argparse.Namespace(picked_up="/tmp/handoff.md"))
    assert rc == 0
    (argv,) = popens
    assert argv[-4:] == ["deck", "%1", "111", "$0"], \
        "close-out must receive the tmux session, pane id, and the identity to verify"


# --- _cmd_picked_up: forwarding a late message (Slice 7, ADR-0069) ----------


def test_the_first_picked_up_forwards_late_messages_once(co_rollover_cli, monkeypatch, capsys):
    mod = co_rollover_cli
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "succ-1")
    monkeypatch.setattr(mod.CR, "read_side_channel",
                        lambda *a, **k: _side_channel_data(picked_up=False))
    monkeypatch.setattr(mod.CR, "mark_picked_up",
                        lambda *a, **k: {"picked_up_this_call": True})
    monkeypatch.setattr(mod.subprocess, "Popen", lambda *a, **k: None)
    calls = []

    def fake_forward(handoff_path, predecessor_session_id, successor_pane_id, tmux_session,
                     **kwargs):
        calls.append((str(handoff_path), predecessor_session_id, successor_pane_id, tmux_session))
        return 2, []

    monkeypatch.setattr(mod.CR, "forward_late_messages", fake_forward)

    rc = mod._cmd_picked_up(argparse.Namespace(picked_up="/tmp/handoff.md"))

    assert rc == 0
    assert calls == [("/tmp/handoff.md", "pred-1", "%2", "deck")]
    assert "forwarded 2 held message" in capsys.readouterr().out


def test_a_retried_picked_up_after_a_completed_pickup_does_not_forward_again(
        co_rollover_cli, monkeypatch):
    mod = co_rollover_cli
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "succ-1")
    monkeypatch.setattr(mod.CR, "read_side_channel",
                        lambda *a, **k: _side_channel_data(picked_up=True))
    calls = []
    monkeypatch.setattr(mod.CR, "forward_late_messages",
                        lambda *a, **k: calls.append(1) or (0, []))

    rc = mod._cmd_picked_up(argparse.Namespace(picked_up="/tmp/handoff.md"))

    assert rc == 0
    assert calls == [], "a retry of an already-completed pickup must not re-read the queue"


def test_a_crash_before_the_gate_flips_leaves_a_retry_that_still_forwards(
        co_rollover_cli, monkeypatch, capsys):
    """Round 2, PR #588's blocking finding: `picked_up` used to flip BEFORE
    forwarding ran, so a crash between the two permanently lost a held
    message (a retry saw `picked_up` already true and skipped forwarding
    forever). Model that crash as a first `--picked-up` call whose
    `mark_picked_up` never runs (the process died first) — the side channel
    it reads back on the retry is exactly what a real crash would leave
    behind: `picked_up` still `False`. The retry must still call
    `forward_late_messages`, so the message it carries is not silently
    dropped forever, even though this can re-send a message an earlier,
    interrupted attempt already got out (a possible duplicate, accepted
    deliberately — see the module docstring)."""
    mod = co_rollover_cli
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "succ-1")
    # `picked_up` never becomes True in this fake side channel — standing in
    # for a process that died before `mark_picked_up`'s write ever landed.
    monkeypatch.setattr(mod.CR, "read_side_channel",
                        lambda *a, **k: _side_channel_data(picked_up=False))
    monkeypatch.setattr(mod.subprocess, "Popen", lambda *a, **k: None)
    forward_calls = []

    def fake_forward(*a, **k):
        forward_calls.append(1)
        return 1, []

    monkeypatch.setattr(mod.CR, "forward_late_messages", fake_forward)

    # Call 1: models the crash — `mark_picked_up` raises as if the process
    # were killed mid-write, so the gate never flips and nothing after this
    # point in `_cmd_picked_up` runs.
    def _crash(*a, **k):
        raise mod.CR.CoRolloverError("simulated crash: process killed before the write landed")

    monkeypatch.setattr(mod.CR, "mark_picked_up", _crash)
    rc1 = mod._cmd_picked_up(argparse.Namespace(picked_up="/tmp/handoff.md"))
    assert rc1 == 1
    assert len(forward_calls) == 1, "forwarding must have run before the simulated crash"

    # Call 2: the retry. The side channel still reports `picked_up: False`
    # (the crash never let it flip), so forwarding must run again rather
    # than being permanently skipped.
    monkeypatch.setattr(mod.CR, "mark_picked_up",
                        lambda *a, **k: {"picked_up_this_call": True})
    rc2 = mod._cmd_picked_up(argparse.Namespace(picked_up="/tmp/handoff.md"))
    assert rc2 == 0
    assert len(forward_calls) == 2, \
        "a retry after a crash before the gate flipped must still deliver the message"


def test_a_forwarding_failure_does_not_prevent_the_close_out(co_rollover_cli, monkeypatch, capsys):
    """The close-out still runs, and the failure reaches stderr — never
    silently swallowed, and never a reason to skip closing out the
    predecessor."""
    mod = co_rollover_cli
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "succ-1")
    monkeypatch.setattr(mod.CR, "read_side_channel",
                        lambda *a, **k: _side_channel_data(picked_up=False))
    monkeypatch.setattr(mod.CR, "mark_picked_up",
                        lambda *a, **k: {"picked_up_this_call": True})
    monkeypatch.setattr(mod.CR, "forward_late_messages",
                        lambda *a, **k: (0, ["could not forward a held message: boom"]))
    popens = []
    monkeypatch.setattr(mod.subprocess, "Popen", lambda argv, **k: popens.append(argv))

    rc = mod._cmd_picked_up(argparse.Namespace(picked_up="/tmp/handoff.md"))

    assert rc == 0
    assert len(popens) == 1, "the close-out must still run despite the forwarding failure"
    assert "could not forward a held message: boom" in capsys.readouterr().err


def test_a_missing_forwarding_identifier_on_first_pickup_says_so(
        co_rollover_cli, monkeypatch, capsys):
    """Non-blocking finding, round 2 PR #588: a missing identifier used to
    skip forwarding with no output at all, indistinguishable from "nothing
    was queued." It must now say which identifier was missing, on stderr,
    and log it the same way a forwarding failure is logged."""
    mod = co_rollover_cli
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "succ-1")
    monkeypatch.setattr(
        mod.CR, "read_side_channel",
        lambda *a, **k: _side_channel_data(picked_up=False, with_forwarding_identifiers=False))
    monkeypatch.setattr(mod.CR, "mark_picked_up",
                        lambda *a, **k: {"picked_up_this_call": True})
    monkeypatch.setattr(mod.subprocess, "Popen", lambda *a, **k: None)

    def _must_not_be_called(*a, **k):
        raise AssertionError("forwarding must not run without every required identifier")

    monkeypatch.setattr(mod.CR, "forward_late_messages", _must_not_be_called)
    logged = []
    monkeypatch.setattr(mod.CR, "log_forwarding_skipped", lambda missing, **k: logged.append(missing))

    rc = mod._cmd_picked_up(argparse.Namespace(picked_up="/tmp/handoff.md"))

    assert rc == 0
    err = capsys.readouterr().err
    assert "missing" in err and "successor pane id" in err
    assert logged and "successor pane id" in logged[0]
