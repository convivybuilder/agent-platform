"""co_rollover — the successor-takeover mechanics (ADR-0069 Slice 5).

These are the pure-logic tests: every tmux call is a mock, exercising argv
construction, output parsing, and the side channel. The real-tmux,
end-to-end exercise of `bin/co-rollover` itself lives in
`test_co_rollover_real_tmux.py`, on a private, throwaway tmux server.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

import co_rollover as CR
import rollover_policy as RP
import session_input as SI
import session_state as SS
import tmux_transcript as TT


# --- current_session_and_pane -----------------------------------------------


def test_current_session_and_pane_reads_tmux_pane_from_the_environment(monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "%3")
    calls = []

    def fake_run(argv, timeout=10.0):
        calls.append(argv)
        return "mysession\n"

    monkeypatch.setattr(CR, "_run", fake_run)
    session, pane = CR.current_session_and_pane()
    assert session == "mysession"
    assert pane == "%3"
    assert calls == [["tmux", "display-message", "-p", "-t", "%3", "#{session_name}"]]


def test_current_session_and_pane_raises_with_no_tmux_pane(monkeypatch):
    monkeypatch.delenv("TMUX_PANE", raising=False)
    with pytest.raises(CR.CoRolloverError, match="TMUX_PANE"):
        CR.current_session_and_pane()


def test_current_session_and_pane_raises_when_tmux_cannot_resolve_it(monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "%3")
    monkeypatch.setattr(CR, "_run", lambda argv, timeout=10.0: None)
    with pytest.raises(CR.CoRolloverError, match="could not resolve"):
        CR.current_session_and_pane()


# --- pane_identity (Slice 5 round 2: the close-out's restart guard) --------


def test_pane_identity_parses_pid_and_session_id(monkeypatch):
    calls = []

    def fake_run(argv, timeout=10.0):
        calls.append(argv)
        return "12345 $3\n"

    monkeypatch.setattr(CR, "_run", fake_run)
    got = CR.pane_identity("%7")
    assert got == {"pane_pid": 12345, "tmux_session_id": "$3"}
    assert calls == [["tmux", "display-message", "-p", "-t", "%7", "#{pane_pid} #{session_id}"]]


def test_pane_identity_is_none_when_tmux_cannot_resolve_it(monkeypatch):
    monkeypatch.setattr(CR, "_run", lambda argv, timeout=10.0: None)
    assert CR.pane_identity("%7") is None


def test_pane_identity_is_none_on_malformed_output(monkeypatch):
    monkeypatch.setattr(CR, "_run", lambda argv, timeout=10.0: "not-numeric $3\n")
    assert CR.pane_identity("%7") is None


# --- pane_window_index (Slice 6: a parked session's issue body) ------------


def test_pane_window_index_parses_the_index(monkeypatch):
    calls = []

    def fake_run(argv, timeout=10.0):
        calls.append(argv)
        return "3\n"

    monkeypatch.setattr(CR, "_run", fake_run)
    assert CR.pane_window_index("%7") == 3
    assert calls == [["tmux", "display-message", "-p", "-t", "%7", "#{window_index}"]]


def test_pane_window_index_is_none_when_tmux_cannot_resolve_it(monkeypatch):
    monkeypatch.setattr(CR, "_run", lambda argv, timeout=10.0: None)
    assert CR.pane_window_index("%7") is None


def test_pane_window_index_is_none_on_non_numeric_output(monkeypatch):
    monkeypatch.setattr(CR, "_run", lambda argv, timeout=10.0: "not-a-window\n")
    assert CR.pane_window_index("%7") is None


# --- spawn_successor ---------------------------------------------------------


def test_spawn_successor_builds_the_new_window_command(monkeypatch):
    calls = []

    def fake_run(argv, timeout=10.0):
        calls.append(argv)
        return "2 %7\n"

    monkeypatch.setattr(CR, "_run", fake_run)
    got = CR.spawn_successor("deck", "/repo", "deck", "/runs/handoff.md")
    assert got == {"window_index": 2, "pane_id": "%7"}

    new_window, remain_on_exit = calls
    argv = new_window
    assert argv[:5] == ["tmux", "new-window", "-d", "-a", "-t"]
    assert "deck:" in argv
    assert "-c" in argv and "/repo" in argv
    assert "-e" in argv
    env_arg = argv[argv.index("-e") + 1]
    assert env_arg == "CLAUDE_REMOTE_CONTROL_SESSION_NAME_PREFIX=deck-rollover"
    # CO_ROLLOVER_SUCCESSOR=1 is bin/co's `_co_reindividuate` marker. It must
    # NOT be a tmux `-e` env var on the whole window (round 2, PR #574): `-e`
    # lands in the environment of the wrapper shell itself, so when `co -r`
    # fails and `|| exec zsh` replaces the wrapper with an interactive shell,
    # that shell would still carry the marker and a by-hand retry there would
    # still skip the forced re-individuate. Only ONE `-e` reaches tmux now.
    assert argv.count("-e") == 1
    env_args = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
    assert not any("CO_ROLLOVER_SUCCESSOR" in e for e in env_args)
    assert argv[-3:-1] == ["zsh", "-lc"]
    shell_cmd = argv[-1]
    assert "CO_ROLLOVER_SUCCESSOR" not in argv[:-1]
    # The marker is instead a shell prefix assignment scoped to the `co`
    # command alone, so the `|| exec zsh` fallback never inherits it.
    before, sep, after = shell_cmd.partition("||")
    assert sep, f"expected a || fallback in {shell_cmd!r}"
    assert before.strip().startswith("CO_ROLLOVER_SUCCESSOR=1 co -r")
    assert "CO_ROLLOVER_SUCCESSOR" not in after
    assert after.strip() == "exec zsh"
    assert "/handoff-pickup /runs/handoff.md" in shell_cmd

    # `remain-on-exit`, set on the exact window `new-window` just reported —
    # what keeps a pane whose process has already exited (and its exit
    # status) readable instead of the window vanishing with it (round 3,
    # agent-platform#566: CI's `convivy-linux` runner has no zsh (not verified on
    # ci1), so
    # `new-window`'s own exec of it fails and the window disappears within
    # milliseconds; this is what makes any SLOWER failure diagnosable).
    assert remain_on_exit == ["tmux", "set-window-option", "-t", "deck:2",
                              "remain-on-exit", "on"]


def test_spawn_successor_quotes_a_handoff_path_with_a_space(monkeypatch):
    calls = []

    def fake_run(argv, timeout=10.0):
        calls.append(argv)
        return "1 %1\n"

    monkeypatch.setattr(CR, "_run", fake_run)
    CR.spawn_successor("deck", "/repo", "deck", "/runs/2026 09 13/handoff.md")
    shell_cmd = calls[0][-1]
    # The whole `/handoff-pickup <path>` prompt must survive as ONE argument to
    # `co -r`, not be split by the space into several words.
    assert "co -r '/handoff-pickup /runs/2026 09 13/handoff.md'" in shell_cmd


def test_spawn_successor_raises_when_tmux_fails(monkeypatch):
    monkeypatch.setattr(CR, "_run", lambda argv, timeout=10.0: None)
    with pytest.raises(CR.CoRolloverError, match="new-window"):
        CR.spawn_successor("deck", "/repo", "deck", "/runs/handoff.md")


def test_spawn_successor_raises_on_malformed_output(monkeypatch):
    monkeypatch.setattr(CR, "_run", lambda argv, timeout=10.0: "not-a-pane\n")
    with pytest.raises(CR.CoRolloverError, match="unexpected"):
        CR.spawn_successor("deck", "/repo", "deck", "/runs/handoff.md")


def test_spawn_successor_tolerates_remain_on_exit_failing(monkeypatch):
    """`_run` never raises, but `spawn_successor` must not treat the
    `remain-on-exit` call's own failure (a window already gone by the time it
    runs — the instant-exec-failure race `remain-on-exit` can lose) as a
    reason to fail the whole spawn; the window/pane it already parsed from
    `new-window` are still what the caller needs."""
    outputs = iter(["2 %7\n", None])
    monkeypatch.setattr(CR, "_run", lambda argv, timeout=10.0: next(outputs))
    got = CR.spawn_successor("deck", "/repo", "deck", "/runs/handoff.md")
    assert got == {"window_index": 2, "pane_id": "%7"}


# --- spawn_successor: CO_ROLLOVER_EXEMPT_PID (issue_01M2FDBJS8GMA16DX4XNBVH2K6) ---


def test_spawn_successor_adds_the_exempt_pid_prefix_when_one_is_given(monkeypatch):
    calls = []

    def fake_run(argv, timeout=10.0):
        calls.append(argv)
        return "2 %7\n"

    monkeypatch.setattr(CR, "_run", fake_run)
    CR.spawn_successor("deck", "/repo", "deck", "/runs/handoff.md", exempt_pids=[4242])

    new_window = calls[0]
    # Never a tmux `-e` on the window for either marker — same leak class
    # round 2 of PR #574 closed for CO_ROLLOVER_SUCCESSOR.
    env_args = [new_window[i + 1] for i, a in enumerate(new_window) if a == "-e"]
    assert not any("CO_ROLLOVER" in e for e in env_args)

    shell_cmd = new_window[-1]
    before, sep, after = shell_cmd.partition("||")
    assert sep, f"expected a || fallback in {shell_cmd!r}"
    # Both prefix assignments on the `co` command, in this order, before the
    # `||` fallback — the fallback shell (`exec zsh`) must carry neither.
    assert before.strip().startswith("CO_ROLLOVER_SUCCESSOR=1 CO_ROLLOVER_EXEMPT_PID=4242 co -r")
    assert "CO_ROLLOVER" not in after
    assert after.strip() == "exec zsh"


def test_spawn_successor_joins_multiple_exempt_pids_with_a_comma(monkeypatch):
    """Round 2, issue_01M2FDBJS8GMA16DX4XNBVH2K6: several claude pids can
    legitimately share one pane (the computer-use-fork case
    `tmux_transcript.claude_pids_under` documents), so every one of them must
    reach the successor, not just the first."""
    calls = []

    def fake_run(argv, timeout=10.0):
        calls.append(argv)
        return "2 %7\n"

    monkeypatch.setattr(CR, "_run", fake_run)
    CR.spawn_successor("deck", "/repo", "deck", "/runs/handoff.md", exempt_pids=[111, 222])

    shell_cmd = calls[0][-1]
    before = shell_cmd.partition("||")[0]
    assert before.strip().startswith(
        "CO_ROLLOVER_SUCCESSOR=1 CO_ROLLOVER_EXEMPT_PID=111,222 co -r")


def test_spawn_successor_omits_the_exempt_pid_prefix_when_none_is_given(monkeypatch):
    calls = []

    def fake_run(argv, timeout=10.0):
        calls.append(argv)
        return "2 %7\n"

    monkeypatch.setattr(CR, "_run", fake_run)
    CR.spawn_successor("deck", "/repo", "deck", "/runs/handoff.md", exempt_pids=None)

    shell_cmd = calls[0][-1]
    before = shell_cmd.partition("||")[0]
    assert before.strip().startswith("CO_ROLLOVER_SUCCESSOR=1 co -r")
    assert "CO_ROLLOVER_EXEMPT_PID" not in shell_cmd


def test_spawn_successor_omits_the_exempt_pid_prefix_when_the_list_is_empty(monkeypatch):
    calls = []

    def fake_run(argv, timeout=10.0):
        calls.append(argv)
        return "2 %7\n"

    monkeypatch.setattr(CR, "_run", fake_run)
    CR.spawn_successor("deck", "/repo", "deck", "/runs/handoff.md", exempt_pids=[])

    shell_cmd = calls[0][-1]
    assert "CO_ROLLOVER_EXEMPT_PID" not in shell_cmd


# --- spawn_successor: CO_ROLLOVER_PERMISSION_MODE (ADR-0069, 2026-09-16) ----


def test_spawn_successor_adds_the_permission_mode_prefix_when_one_is_given(monkeypatch):
    calls = []

    def fake_run(argv, timeout=10.0):
        calls.append(argv)
        return "2 %7\n"

    monkeypatch.setattr(CR, "_run", fake_run)
    CR.spawn_successor("deck", "/repo", "deck", "/runs/handoff.md", permission_mode="plan")

    new_window = calls[0]
    # Never a tmux `-e` on the window — same leak class round 2 of PR #574
    # closed for CO_ROLLOVER_SUCCESSOR, extended to this third marker.
    env_args = [new_window[i + 1] for i, a in enumerate(new_window) if a == "-e"]
    assert not any("CO_ROLLOVER" in e for e in env_args)

    shell_cmd = new_window[-1]
    before, sep, after = shell_cmd.partition("||")
    assert sep, f"expected a || fallback in {shell_cmd!r}"
    assert before.strip().startswith("CO_ROLLOVER_SUCCESSOR=1 CO_ROLLOVER_PERMISSION_MODE=plan co -r")
    assert "CO_ROLLOVER" not in after
    assert after.strip() == "exec zsh"


def test_spawn_successor_orders_permission_mode_after_exempt_pid(monkeypatch):
    """All three markers combined: SUCCESSOR, then EXEMPT_PID, then
    PERMISSION_MODE, matching the order `bin/co-rollover`'s `_cmd_spawn`
    resolves them in."""
    calls = []

    def fake_run(argv, timeout=10.0):
        calls.append(argv)
        return "2 %7\n"

    monkeypatch.setattr(CR, "_run", fake_run)
    CR.spawn_successor("deck", "/repo", "deck", "/runs/handoff.md",
                       exempt_pids=[4242], permission_mode="acceptEdits")

    shell_cmd = calls[0][-1]
    before = shell_cmd.partition("||")[0]
    assert before.strip().startswith(
        "CO_ROLLOVER_SUCCESSOR=1 CO_ROLLOVER_EXEMPT_PID=4242 "
        "CO_ROLLOVER_PERMISSION_MODE=acceptEdits co -r")


def test_spawn_successor_omits_the_permission_mode_prefix_when_none_is_given(monkeypatch):
    calls = []

    def fake_run(argv, timeout=10.0):
        calls.append(argv)
        return "2 %7\n"

    monkeypatch.setattr(CR, "_run", fake_run)
    CR.spawn_successor("deck", "/repo", "deck", "/runs/handoff.md", permission_mode=None)

    shell_cmd = calls[0][-1]
    before = shell_cmd.partition("||")[0]
    assert before.strip().startswith("CO_ROLLOVER_SUCCESSOR=1 co -r")
    assert "CO_ROLLOVER_PERMISSION_MODE" not in shell_cmd


def test_spawn_successor_omits_the_permission_mode_prefix_when_it_is_empty(monkeypatch):
    calls = []

    def fake_run(argv, timeout=10.0):
        calls.append(argv)
        return "2 %7\n"

    monkeypatch.setattr(CR, "_run", fake_run)
    CR.spawn_successor("deck", "/repo", "deck", "/runs/handoff.md", permission_mode="")

    shell_cmd = calls[0][-1]
    assert "CO_ROLLOVER_PERMISSION_MODE" not in shell_cmd


# --- predecessor_claude_pids --------------------------------------------------


def _tree(monkeypatch, *, children: dict[int, list[int]], commands: dict[int, str]):
    """Inject a process tree instead of shelling out to `ps` — the same
    mocking `test_tmux_transcript.py`'s own `_tree` helper uses, applied here
    to pin `predecessor_claude_pids`'s use of `claude_pids_for_pane` from the
    calling side rather than re-deriving the tree walk itself."""
    monkeypatch.setattr(TT, "_children", lambda pid: children.get(pid, []))
    monkeypatch.setattr(TT, "_command_of", lambda pid: commands.get(pid, "/bin/zsh -l"))


def test_predecessor_claude_pids_finds_claude_under_the_pane_shell(monkeypatch):
    _tree(monkeypatch, children={111: [222]}, commands={111: "-zsh", 222: "claude"})
    assert CR.predecessor_claude_pids(111) == [222]


def test_predecessor_claude_pids_finds_an_execd_claude_pane(monkeypatch):
    _tree(monkeypatch, children={}, commands={111: "claude"})
    assert CR.predecessor_claude_pids(111) == [111]


def test_predecessor_claude_pids_is_empty_when_no_claude_is_found(monkeypatch):
    _tree(monkeypatch, children={111: [222]}, commands={111: "-zsh", 222: "vim"})
    assert CR.predecessor_claude_pids(111) == []


def test_predecessor_claude_pids_returns_every_pid_sharing_the_pane(monkeypatch):
    """The 2026-07-22 computer-use-fork shape `tmux_transcript.claude_pids_
    under`'s own docstring documents: several claude processes legitimately
    at the same level beneath one pane. All of them must come back, not just
    the first, or the second would trip knowledge-ctx's live-process guard
    as if it were a genuinely different session."""
    _tree(monkeypatch, children={111: [222, 333]},
          commands={111: "-zsh", 222: "claude", 333: "claude"})
    assert CR.predecessor_claude_pids(111) == [222, 333]


# --- verify_up ---------------------------------------------------------------


def test_verify_up_recognises_the_remote_control_banner(monkeypatch):
    monkeypatch.setattr(CR, "_run", lambda argv, timeout=10.0: "some text\nremote-control is active\n")
    ok, text = CR.verify_up("deck", 2, sleep=lambda s: None, clock=lambda: 0.0)
    assert ok is True
    assert "remote-control is active" in text


def test_verify_up_recognises_a_known_failure_signature(monkeypatch):
    monkeypatch.setattr(CR, "_run", lambda argv, timeout=10.0: "knowledge-ctx use failed; aborting.")
    ok, text = CR.verify_up("deck", 2, sleep=lambda s: None, clock=lambda: 0.0)
    assert ok is False
    assert "aborting" in text


def test_verify_up_recognises_tmux_own_dead_pane_banner(monkeypatch):
    """`remain-on-exit` (set by `spawn_successor`) keeps a pane whose process
    already exited readable instead of destroying the window with it —
    `capture-pane` then returns tmux's own banner, carrying the exit status,
    in place of the prompt. `verify_up` must call that a failure rather than
    spinning out the full timeout on text that will never match UP_RE."""
    monkeypatch.setattr(CR, "_run",
                        lambda argv, timeout=10.0: "Pane is dead (status 127, Mon Jan 1 00:00:00 2026)")
    ok, text = CR.verify_up("deck", 2, sleep=lambda s: None, clock=lambda: 0.0)
    assert ok is False
    assert "status 127" in text


def test_verify_up_polls_until_it_sees_the_banner(monkeypatch):
    outputs = iter(["zsh\n", "zsh\n", "remote-control is active\n"])
    sleeps = []
    monkeypatch.setattr(CR, "_run", lambda argv, timeout=10.0: next(outputs))
    ok, text = CR.verify_up("deck", 2, sleep=lambda s: sleeps.append(s), clock=lambda: 0.0)
    assert ok is True
    assert len(sleeps) == 2


def test_verify_up_times_out_reporting_the_last_pane_text(monkeypatch):
    clock = iter([0.0, 5.0, 40.0])  # third read is past the 30s deadline
    monkeypatch.setattr(CR, "_run", lambda argv, timeout=10.0: "still zsh\n")
    ok, text = CR.verify_up("deck", 2, timeout=30.0, sleep=lambda s: None,
                            clock=lambda: next(clock))
    assert ok is False
    assert "still zsh" in text


# The pane text knowledge-ctx's live-claude refusal leaves behind when `co`
# falls back to the predecessor's profile (bin/knowledge-ctx's pre-flight, then
# bin/co `_co_reindividuate`). The session comes up after it.
_LIVE_CLAUDE_FALLBACK = (
    "co: refresh — rollover successor: knowledge-ctx use daytime --force\n"
    "knowledge-ctx: writing standard write-better: composed via platform/instruction-\n"
    "✗ live Claude Code processes detected in target — aborting.\n"
    "  pid 11368  cwd=/srv/convivy/shipwright\n"
    "NOTHING WAS WRITTEN — the plan above was not applied.\n"
    "co: refresh — rollover successor: knowledge-ctx refused (a live claude in\n"
    "    cwd that isn't the predecessor's own) — falling back to the\n"
    "    predecessor's already-individuated profile (verified\n"
    "    .claude/.active-profile names daytime)\n"
)


def test_verify_up_counts_a_recoverable_refusal_as_up_once_the_banner_appears(monkeypatch):
    """A successor that prints knowledge-ctx's live-claude refusal and its
    fallback, then the remote-control banner, is up. Before the fix the bare
    word "aborting" in the refusal failed the rollover on the first capture,
    so co never selected the successor's window and parked a live predecessor."""
    outputs = iter([_LIVE_CLAUDE_FALLBACK, _LIVE_CLAUDE_FALLBACK + "remote-control is active\n"])
    monkeypatch.setattr(CR, "_run", lambda argv, timeout=10.0: next(outputs))
    ok, text = CR.verify_up("shipwright", 2, sleep=lambda s: None, clock=lambda: 0.0)
    assert ok is True
    assert "remote-control is active" in text


@pytest.mark.parametrize("fatal_text", [
    "Pane is dead (status 1, Mon Jan 1 00:00:00 2026)",
    "zsh:1: command not found: claude",
    "co: refresh — knowledge-ctx use failed; aborting.",
    "co: refresh — knowledge pull failed; aborting.",
    "co: refresh — rollover successor: daytime was never individuated in this\n"
    "    directory, and a live claude here blocked individuating it.",
    # knowledge-ctx's own fatal line is followed by co's "use failed" line
    # (bin/knowledge-ctx's container check, then bin/co `_co_reindividuate`);
    # the second line is the one that matches.
    "✗ /srv/claude-test/.claude/agents is a symlink, not a directory this render owns — "
    "aborting before writing anything.\n"
    "co: refresh — knowledge-ctx use failed; aborting.",
])
def test_verify_up_fails_fast_on_a_fatal_text_without_waiting_for_the_deadline(
        monkeypatch, fatal_text):
    monkeypatch.setattr(CR, "_run", lambda argv, timeout=10.0: fatal_text)
    # A sleep here means the fatal text was not matched; fail rather than poll.
    ok, text = CR.verify_up("shipwright", 2,
                            sleep=lambda s: pytest.fail("waited on a fatal pane"),
                            clock=lambda: 0.0)
    assert ok is False


def test_verify_up_fails_a_refusal_that_never_shows_the_banner_by_the_deadline(monkeypatch):
    clock = iter([0.0, 5.0, 40.0])  # third read is past the 30s deadline
    monkeypatch.setattr(CR, "_run", lambda argv, timeout=10.0: _LIVE_CLAUDE_FALLBACK)
    ok, text = CR.verify_up("shipwright", 2, timeout=30.0, sleep=lambda s: None,
                            clock=lambda: next(clock))
    assert ok is False
    assert "aborting" in text


# --- select_window / kill_pane / renumber_windows / close_out ----------------


def test_select_window_targets_the_exact_session_and_window(monkeypatch):
    calls = []
    monkeypatch.setattr(CR, "_run", lambda argv, timeout=10.0: calls.append(argv))
    CR.select_window("deck", 2)
    assert calls == [["tmux", "select-window", "-t", "deck:2"]]


def test_clear_remain_on_exit_targets_the_exact_window_and_turns_it_off(monkeypatch):
    calls = []
    monkeypatch.setattr(CR, "_run", lambda argv, timeout=10.0: calls.append(argv) or "")
    ok = CR.clear_remain_on_exit("deck", 2)
    assert ok is True
    assert calls == [["tmux", "set-window-option", "-t", "deck:2", "remain-on-exit", "off"]]


def test_clear_remain_on_exit_returns_false_without_raising_on_failure(monkeypatch):
    monkeypatch.setattr(CR, "_run", lambda argv, timeout=10.0: None)
    ok = CR.clear_remain_on_exit("deck", 2)
    assert ok is False


def test_close_out_kills_the_pane_then_renumbers_in_order(monkeypatch):
    """The legacy/no-check path: a caller supplying neither expected value
    gets the original unconditional behaviour."""
    calls = []
    monkeypatch.setattr(CR, "_run", lambda argv, timeout=10.0: calls.append(argv))
    CR.close_out("deck", "%1")
    assert calls == [
        ["tmux", "kill-pane", "-t", "%1"],
        ["tmux", "move-window", "-r", "-t", "deck"],
    ]


def test_close_out_kills_when_the_recorded_identity_still_matches(monkeypatch):
    calls = []

    def fake_run(argv, timeout=10.0):
        if argv[:2] == ["tmux", "display-message"]:
            return "111 $2\n"
        calls.append(argv)
        return ""

    monkeypatch.setattr(CR, "_run", fake_run)
    CR.close_out("deck", "%1", expected_pane_pid=111, expected_tmux_session_id="$2")
    assert calls == [
        ["tmux", "kill-pane", "-t", "%1"],
        ["tmux", "move-window", "-r", "-t", "deck"],
    ]


def test_close_out_skips_and_logs_on_an_identity_mismatch(monkeypatch, tmp_path):
    """THE BUG THIS CLOSES: a tmux server restart reassigns pane ids and
    session ids from zero, so `%1` recorded before a restart can by now name
    an unrelated LIVE pane. A mismatch must kill and renumber nothing."""
    kills = []

    def fake_run(argv, timeout=10.0):
        if argv[:2] == ["tmux", "display-message"]:
            return "999 $9\n"  # an unrelated live pane's real identity
        kills.append(argv)
        return ""

    monkeypatch.setattr(CR, "_run", fake_run)
    CR.close_out("deck", "%1", expected_pane_pid=111, expected_tmux_session_id="$2",
                 log_dir=tmp_path)

    assert kills == [], "a mismatched identity must kill and renumber nothing"
    log_text = (tmp_path / CR.CLOSE_OUT_LOG_NAME).read_text()
    assert "%1" in log_text and "999" in log_text and "$9" in log_text


def test_close_out_skips_when_the_pane_no_longer_resolves_at_all(monkeypatch, tmp_path):
    kills = []

    def fake_run(argv, timeout=10.0):
        if argv[:2] == ["tmux", "display-message"]:
            return None
        kills.append(argv)
        return ""

    monkeypatch.setattr(CR, "_run", fake_run)
    CR.close_out("deck", "%1", expected_pane_pid=111, expected_tmux_session_id="$2",
                 log_dir=tmp_path)

    assert kills == []
    assert (tmp_path / CR.CLOSE_OUT_LOG_NAME).exists()


# --- the side channel ---------------------------------------------------------


def test_side_channel_round_trips(tmp_path):
    handoff = tmp_path / "handoff.md"
    handoff.write_text("# handoff")
    data = {"tmux_session": "deck", "predecessor": {"session_id": "s1", "pane_id": "%1"},
            "successor": {"session_id": None, "pane_id": "%2"}, "successor_window": 2,
            "picked_up": False, "picked_up_at": None}
    CR.write_side_channel(handoff, data)
    assert CR.read_side_channel(handoff) == data


def test_read_side_channel_is_none_when_absent(tmp_path):
    assert CR.read_side_channel(tmp_path / "handoff.md") is None


def test_read_side_channel_is_none_when_unparseable(tmp_path):
    handoff = tmp_path / "handoff.md"
    handoff.write_text("# handoff")
    (tmp_path / CR.SIDE_CHANNEL_NAME).write_text("not json")
    assert CR.read_side_channel(handoff) is None


# --- mark_picked_up ------------------------------------------------------------


def test_mark_picked_up_raises_with_no_side_channel(tmp_path):
    with pytest.raises(CR.CoRolloverError, match="no co-rollover state"):
        CR.mark_picked_up(tmp_path / "handoff.md", "succ-1")


def test_mark_picked_up_records_pickup_and_fills_in_the_successor_id(tmp_path, monkeypatch):
    monkeypatch.setattr(RP, "CACHE_DIR", tmp_path / "cache")
    handoff = tmp_path / "handoff.md"
    handoff.write_text("# handoff")
    CR.write_side_channel(handoff, {
        "tmux_session": "deck", "predecessor": {"session_id": "pred-1", "pane_id": "%1"},
        "successor": {"session_id": None, "pane_id": "%2"}, "successor_window": 2,
        "picked_up": False, "picked_up_at": None,
    })
    RP.mark_rolling("pred-1", predecessor={"session_id": "pred-1", "pane_id": "%1"},
                    successor={"session_id": None, "pane_id": "%2"}, successor_window=2)

    got = CR.mark_picked_up(handoff, "succ-1", now=1234.0)

    assert got["picked_up"] is True
    assert got["picked_up_at"] == 1234.0
    assert got["picked_up_this_call"] is True
    assert got["successor"]["session_id"] == "succ-1"
    assert CR.read_side_channel(handoff)["picked_up"] is True
    assert "picked_up_this_call" not in CR.read_side_channel(handoff), \
        "picked_up_this_call is call-specific and must never be persisted"
    assert RP.read_state("pred-1")["successor"]["session_id"] == "succ-1"


def test_mark_picked_up_is_idempotent_and_keeps_the_first_timestamp(tmp_path, monkeypatch):
    monkeypatch.setattr(RP, "CACHE_DIR", tmp_path / "cache")
    handoff = tmp_path / "handoff.md"
    handoff.write_text("# handoff")
    CR.write_side_channel(handoff, {
        "tmux_session": "deck", "predecessor": {"session_id": "pred-1", "pane_id": "%1"},
        "successor": {"session_id": None, "pane_id": "%2"}, "successor_window": 2,
        "picked_up": False, "picked_up_at": None,
    })
    RP.mark_rolling("pred-1", predecessor={"session_id": "pred-1", "pane_id": "%1"},
                    successor={"session_id": None, "pane_id": "%2"}, successor_window=2)

    first = CR.mark_picked_up(handoff, "succ-1", now=1000.0)
    second = CR.mark_picked_up(handoff, "succ-1", now=2000.0)

    assert first["picked_up_at"] == 1000.0
    assert first["picked_up_this_call"] is True
    assert second["picked_up_at"] == 1000.0, "a retried pickup must not reset the clock"
    assert second["picked_up_this_call"] is False, \
        "a retry must report it was NOT the call that flipped picked_up"


# --- forward_late_messages (Slice 7, epic issue_01M1S2X8ENKYEGSDE3H0REFD3G) ---
#
# Idempotency is the CALLER's, not this function's own, and only ONE-SIDED
# (round 2, PR #588): `bin/co-rollover` calls this BEFORE `mark_picked_up`
# flips the side channel's `picked_up` gate (see test_co_rollover_cli.py), so
# a retry that follows a crash before that flip re-reads the predecessor's
# queue and can re-send a message an earlier, interrupted call already
# delivered — deliberately, so a crash can never turn into a silently,
# permanently dropped message instead. So these tests pin the FILTER and the
# DISPATCH, not a dedup mechanism this function does not have; the crash-then-
# retry behavior itself is covered in test_co_rollover_cli.py, where the
# ordering this depends on actually lives.


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _handoff_with_mtime(tmp_path, mtime: float):
    handoff = tmp_path / "handoff.md"
    handoff.write_text("# handoff")
    os.utime(handoff, (mtime, mtime))
    return handoff


CUTOFF = 2_000_000_000.0


@pytest.fixture
def forward_env(tmp_path, monkeypatch):
    """`forward_late_messages` wired to a handoff with a fixed mtime and a
    stubbed queue read; each test overrides `_held_messages_with_timestamps`
    and/or `send_message` for its own scenario. Returns the handoff path."""
    handoff = _handoff_with_mtime(tmp_path, CUTOFF)
    monkeypatch.setattr(TT, "transcript_for_session_id", lambda sid: tmp_path / "t.jsonl")
    return handoff


def test_forwards_only_messages_queued_after_the_handoff_was_written(forward_env, monkeypatch):
    monkeypatch.setattr(SS, "_held_messages_with_timestamps", lambda recorded: [
        {"content": "already in the handoff", "queued_at": _iso(CUTOFF - 10)},
        {"content": "arrived after", "queued_at": _iso(CUTOFF + 10)},
    ])
    sent_calls = []
    monkeypatch.setattr(SI, "send_message",
                        lambda name, text, pane_id=None, skip_blocked_check=False:
                            sent_calls.append((name, text, pane_id, skip_blocked_check)))

    sent, failures = CR.forward_late_messages(forward_env, "pred-1", "%9", "deck")

    assert sent == 1
    assert failures == []
    assert sent_calls == [("deck", "arrived after", "%9", True)], \
        "must target the successor's own pane directly, not resolve by name, " \
        "and must pass the named skip_blocked_check exemption (CQ-420 round 2)"


def test_a_message_with_no_parseable_timestamp_is_not_forwarded(forward_env, monkeypatch):
    monkeypatch.setattr(SS, "_held_messages_with_timestamps", lambda recorded: [
        {"content": "unknown timing", "queued_at": None},
        {"content": "garbled", "queued_at": "not-a-timestamp"},
    ])
    sent_calls = []
    monkeypatch.setattr(SI, "send_message",
                        lambda name, text, pane_id=None, skip_blocked_check=False:
                            sent_calls.append(text))

    sent, failures = CR.forward_late_messages(forward_env, "pred-1", "%9", "deck")

    assert sent == 0
    assert sent_calls == []


def test_returns_zero_when_nothing_is_held(forward_env, monkeypatch):
    monkeypatch.setattr(SS, "_held_messages_with_timestamps", lambda recorded: [])
    sent_calls = []
    monkeypatch.setattr(SI, "send_message",
                        lambda name, text, pane_id=None, skip_blocked_check=False:
                            sent_calls.append(text))

    sent, failures = CR.forward_late_messages(forward_env, "pred-1", "%9", "deck")

    assert (sent, failures) == (0, [])
    assert sent_calls == []


def test_returns_zero_when_the_predecessor_transcript_cannot_be_resolved(
        tmp_path, monkeypatch):
    handoff = _handoff_with_mtime(tmp_path, CUTOFF)
    monkeypatch.setattr(TT, "transcript_for_session_id", lambda sid: None)
    sent_calls = []
    monkeypatch.setattr(SI, "send_message",
                        lambda name, text, pane_id=None, skip_blocked_check=False:
                            sent_calls.append(text))

    sent, failures = CR.forward_late_messages(handoff, "pred-1", "%9", "deck")

    assert (sent, failures) == (0, [])
    assert sent_calls == []


def test_never_reads_the_composer_draft(forward_env, monkeypatch):
    """Only the held QUEUE is read — a live draft is the operator's own
    unsent words, and the successor cannot finish it for them. Wiring
    `_read_input_box` to blow up pins that this function never calls it."""
    monkeypatch.setattr(SS, "_held_messages_with_timestamps", lambda recorded: [
        {"content": "arrived after", "queued_at": _iso(CUTOFF + 10)}])
    monkeypatch.setattr(SI, "send_message",
                        lambda name, text, pane_id=None, skip_blocked_check=False: None)

    def boom(pane_id):
        raise AssertionError("forward_late_messages must never read the composer draft")
    monkeypatch.setattr(SI, "_read_input_box", boom)

    sent, failures = CR.forward_late_messages(forward_env, "pred-1", "%9", "deck")
    assert sent == 1


def test_a_single_send_failure_is_logged_and_does_not_stop_the_rest(
        forward_env, monkeypatch, tmp_path):
    monkeypatch.setattr(SS, "_held_messages_with_timestamps", lambda recorded: [
        {"content": "fails", "queued_at": _iso(CUTOFF + 10)},
        {"content": "succeeds", "queued_at": _iso(CUTOFF + 20)},
    ])

    def flaky(name, text, pane_id=None, skip_blocked_check=False):
        if text == "fails":
            raise SI.InputError("this session is waiting on a question")

    monkeypatch.setattr(SI, "send_message", flaky)

    sent, failures = CR.forward_late_messages(forward_env, "pred-1", "%9", "deck",
                                              log_dir=tmp_path)

    assert sent == 1
    assert len(failures) == 1
    assert "waiting on a question" in failures[0]
    log = (tmp_path / CR.CLOSE_OUT_LOG_NAME).read_text()
    assert "could not forward a held message" in log
