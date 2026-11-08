"""bin/co-rollover-pickup-watch — `main`'s own argv parsing and wiring into
`co_rollover.run_pickup_watch`. `co_rollover.run_pickup_watch` itself (the
wait/run logic) is tested directly in test_co_rollover_pickup_watch.py; this
file only checks the CLI passes through what it was given.
"""
from __future__ import annotations


def test_main_passes_its_argv_straight_through_to_run_pickup_watch(
        co_rollover_pickup_watch_cli, monkeypatch):
    mod = co_rollover_pickup_watch_cli
    seen = {}

    def fake_run_pickup_watch(handoff_path, claude_pid, session_id, *, timeout, picked_up_argv):
        seen.update(handoff_path=handoff_path, claude_pid=claude_pid, session_id=session_id,
                    timeout=timeout, picked_up_argv=picked_up_argv)
        return 0

    monkeypatch.setattr(mod.CR, "run_pickup_watch", fake_run_pickup_watch)
    monkeypatch.setattr(mod.sys, "argv", [
        "co-rollover-pickup-watch",
        "--handoff-path", "/x/handoff.md",
        "--claude-pid", "4242",
        "--session-id", "the-session-id",
        "--timeout", "7.5",
    ])

    rc = mod.main()

    assert rc == 0
    assert seen["handoff_path"] == "/x/handoff.md"
    assert seen["claude_pid"] == 4242
    assert seen["session_id"] == "the-session-id"
    assert seen["timeout"] == 7.5
    assert seen["picked_up_argv"] == [str(mod._CO_ROLLOVER)]


def test_main_defaults_the_timeout_to_120_seconds(co_rollover_pickup_watch_cli, monkeypatch):
    mod = co_rollover_pickup_watch_cli
    seen = {}

    def fake_run_pickup_watch(handoff_path, claude_pid, session_id, *, timeout, picked_up_argv):
        seen["timeout"] = timeout
        return 0

    monkeypatch.setattr(mod.CR, "run_pickup_watch", fake_run_pickup_watch)
    monkeypatch.setattr(mod.sys, "argv", [
        "co-rollover-pickup-watch",
        "--handoff-path", "/x/handoff.md",
        "--claude-pid", "4242",
        "--session-id", "the-session-id",
    ])

    mod.main()

    assert seen["timeout"] == 120.0


def test_main_returns_run_pickup_watchs_own_exit_code(co_rollover_pickup_watch_cli, monkeypatch):
    mod = co_rollover_pickup_watch_cli
    monkeypatch.setattr(mod.CR, "run_pickup_watch", lambda *a, **k: 1)
    monkeypatch.setattr(mod.sys, "argv", [
        "co-rollover-pickup-watch",
        "--handoff-path", "/x/handoff.md",
        "--claude-pid", "4242",
        "--session-id", "sid",
    ])

    assert mod.main() == 1
