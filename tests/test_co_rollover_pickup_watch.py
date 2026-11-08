"""co_rollover.run_pickup_watch / wait_for_pickup_ready — the detached watch
`bin/co` starts for a rollover successor, so the pickup is recorded by the
launcher rather than depending on the successor's model choosing to run
`/handoff-pickup`'s own `co-rollover --picked-up` (it twice did not:
2026-10-01, 2026-10-24).

Every wait here uses a fake `sleep`/`clock` pair, never a real one — these
tests must finish instantly regardless of the `timeout`/`poll_interval`
passed. `TT._command_of` is monkeypatched rather than calling a real `ps`.
"""
from __future__ import annotations

import subprocess

import co_rollover as CR


class _FakeClock:
    """A monotonic clock driven by `advance`, with `sleep` wired to it so a
    test controls exactly how many seconds pass per poll without a real
    delay."""

    def __init__(self):
        self.t = 0.0

    def clock(self) -> float:
        return self.t

    def sleep(self, secs: float) -> None:
        self.t += secs


def test_run_pickup_watch_runs_co_rollover_once_the_state_file_appears(tmp_path, monkeypatch):
    """THE NORMAL CASE: the side channel appears a little after this watch
    starts (`_cmd_spawn`'s write can still be running), and the exec'd pid is
    already running claude. Once both hold, `co-rollover --picked-up` runs
    exactly once, with `CLAUDE_CODE_SESSION_ID` set to the id `bin/co`
    generated."""
    handoff = tmp_path / "handoff.md"
    handoff.write_text("# handoff\n")
    side_channel = tmp_path / CR.SIDE_CHANNEL_NAME
    monkeypatch.setattr(CR.TT, "_command_of", lambda pid: "/opt/homebrew/bin/claude")

    fake = _FakeClock()
    real_sleep = fake.sleep

    def sleep_and_write(secs: float) -> None:
        real_sleep(secs)
        if not side_channel.exists():
            side_channel.write_text("{}")

    recorded = {}

    def fake_run(argv, env=None, **kwargs):
        recorded["argv"] = argv
        recorded["env"] = env
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    rc = CR.run_pickup_watch(
        str(handoff), 4242, "the-successor-session-id",
        timeout=100.0, log_dir=tmp_path, picked_up_argv=["co-rollover"],
        run=fake_run, sleep=sleep_and_write, clock=fake.clock)

    assert rc == 0
    assert recorded["argv"] == ["co-rollover", "--picked-up", str(handoff)]
    assert recorded["env"]["CLAUDE_CODE_SESSION_ID"] == "the-successor-session-id"
    assert not (tmp_path / CR.CLOSE_OUT_LOG_NAME).exists(), \
        "the success path must log nothing"


def test_run_pickup_watch_times_out_and_never_runs_co_rollover(tmp_path, monkeypatch):
    """THE STATE FILE NEVER APPEARS (a parked/failed spawn, or a predecessor
    that never ran `co-rollover` at all): the watch must time out, log one
    line naming the handoff, and never run `co-rollover`."""
    handoff = tmp_path / "handoff.md"
    monkeypatch.setattr(CR.TT, "_command_of", lambda pid: "/opt/homebrew/bin/claude")

    fake = _FakeClock()
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        raise AssertionError("co-rollover must never run when the state file never appears")

    rc = CR.run_pickup_watch(
        str(handoff), 4242, "sid", timeout=3.0, log_dir=tmp_path,
        run=fake_run, sleep=fake.sleep, clock=fake.clock)

    assert rc == 1
    assert calls == []
    log_text = (tmp_path / CR.CLOSE_OUT_LOG_NAME).read_text()
    assert "pickup-watch timeout" in log_text
    assert str(handoff) in log_text


def test_run_pickup_watch_never_runs_co_rollover_when_the_pid_is_not_claude(tmp_path, monkeypatch):
    """THE STATE FILE IS THERE BUT THE PID IS NOT CLAUDE: proof the successor
    actually started is missing, so the watch must time out without ever
    running `co-rollover` — a launch that died before `exec claude`, or
    execed something else, must never get a pickup recorded for a session
    that never existed."""
    handoff = tmp_path / "handoff.md"
    (tmp_path / CR.SIDE_CHANNEL_NAME).write_text("{}")
    monkeypatch.setattr(CR.TT, "_command_of", lambda pid: "/bin/zsh")

    fake = _FakeClock()
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        raise AssertionError("co-rollover must never run when the pid is not claude")

    rc = CR.run_pickup_watch(
        str(handoff), 4242, "sid", timeout=2.0, log_dir=tmp_path,
        run=fake_run, sleep=fake.sleep, clock=fake.clock)

    assert rc == 1
    assert calls == []


def test_run_pickup_watch_logs_a_nonzero_co_rollover_exit(tmp_path, monkeypatch):
    """`co-rollover --picked-up` itself can refuse (e.g. no side channel by
    the time it actually runs) — a non-zero exit is logged with its stderr,
    not swallowed."""
    handoff = tmp_path / "handoff.md"
    (tmp_path / CR.SIDE_CHANNEL_NAME).write_text("{}")
    monkeypatch.setattr(CR.TT, "_command_of", lambda pid: "/opt/homebrew/bin/claude")

    fake = _FakeClock()

    def fake_run(argv, env=None, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom\n")

    rc = CR.run_pickup_watch(
        str(handoff), 4242, "sid", timeout=5.0, log_dir=tmp_path,
        run=fake_run, sleep=fake.sleep, clock=fake.clock)

    assert rc == 1
    log_text = (tmp_path / CR.CLOSE_OUT_LOG_NAME).read_text()
    assert "exited 1" in log_text
    assert "boom" in log_text


def test_wait_for_pickup_ready_requires_both_conditions_at_once(tmp_path, monkeypatch):
    """Neither condition alone is enough: the file existing with a non-claude
    pid, and a claude pid with no file yet, must each keep waiting."""
    handoff = tmp_path / "handoff.md"
    side_channel = tmp_path / CR.SIDE_CHANNEL_NAME

    monkeypatch.setattr(CR.TT, "_command_of", lambda pid: "/bin/zsh")
    fake = _FakeClock()
    side_channel.write_text("{}")
    assert CR.wait_for_pickup_ready(str(handoff), 4242, timeout=1.0,
                                     sleep=fake.sleep, clock=fake.clock) is False

    side_channel.unlink()
    monkeypatch.setattr(CR.TT, "_command_of", lambda pid: "/opt/homebrew/bin/claude")
    fake2 = _FakeClock()
    assert CR.wait_for_pickup_ready(str(handoff), 4242, timeout=1.0,
                                     sleep=fake2.sleep, clock=fake2.clock) is False
