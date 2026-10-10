"""`agentctl wait` must say so when the WAITER is killed, not the run.

THE AMBIGUITY THIS CLOSES (ARCH-367). A backgrounded `agentctl wait` is subject to
the harness's task-lifetime cap. On a long run the harness kills the WAITER while the
run — setsid'd out of the caller's process tree — keeps working. Killed mid-`sleep`,
the waiter printed nothing, so its death looked exactly like a finished dispatch.

That silence is dangerous rather than merely confusing: the intuitive recovery is to
redispatch, which puts a second worker into the same worktree on the same branch,
racing the first. Five instances in one vivy co session across 2026-07-21/22.

These tests drive the REAL binary as a subprocess and send it a real signal, because
the thing under test is what a dying process manages to emit. A mocked signal proves
nothing about that.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

AGENTCTL = Path(__file__).resolve().parents[1] / "bin" / "agentctl"


LIB = Path(__file__).resolve().parents[1] / "lib" / "agent-platform"


def _make_run(home: Path, run_id: str, *, finished: bool = False) -> Path:
    # agentctl resolves both its lib dir and RUNS_ROOT from HOME, so a temp HOME
    # needs the lib linked in or the binary cannot import telemetry.
    libdir = home / ".local" / "lib"
    libdir.mkdir(parents=True, exist_ok=True)
    link = libdir / "agent-platform"
    if not link.exists():
        link.symlink_to(LIB)
    rd = home / "Convivy" / "agent-runs" / "2026-07-23" / run_id
    rd.mkdir(parents=True)
    if finished:
        (rd / "outcome.json").write_text(json.dumps({"exit_status": "completed"}))
    return rd


def _spawn_waiter(home: Path, *run_ids: str) -> subprocess.Popen:
    # HOME still places the lib link; BUILDER_ROOT places the run records, which
    # builder_root.runs_root() no longer derives from HOME.
    env = {**os.environ, "HOME": str(home), "BUILDER_ROOT": str(home / "Convivy"),
           "PYTHONUNBUFFERED": "1"}
    return subprocess.Popen(
        [sys.executable, str(AGENTCTL), "wait", *run_ids, "--poll", "0.1"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)


def _wait_until_polling(proc: subprocess.Popen, timeout: float = 30.0) -> str:
    """Block until the waiter has installed its handler and entered the poll loop.

    Reads the `wait: watching …` line the waiter emits immediately after installing
    the handler, which is a REAL readiness signal. The first version of this helper
    slept a fixed 0.25s and returned — long enough on an idle laptop, and a flake
    waiting to happen under CI load, where a SIGTERM landing before the handler is
    installed gives Python's default -15 and fails the very test written to prove
    the handler exists (reviewer, #214).

    Returns the output consumed while waiting, so the caller can prepend it to what
    it reads after signalling.
    """
    deadline = time.monotonic() + timeout
    seen = []
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            seen.append(proc.stdout.read() if proc.stdout else "")
            pytest.fail(f"waiter exited early (rc={proc.returncode}):\n{''.join(seen)}")
        line = proc.stdout.readline() if proc.stdout else ""
        if line:
            seen.append(line)
            if "watching" in line:
                return "".join(seen)
    pytest.fail(f"waiter never reported readiness within {timeout}s:\n{''.join(seen)}")


def test_a_sigtermed_waiter_says_the_run_is_still_alive(tmp_path):
    _make_run(tmp_path, "run-abc123")
    proc = _spawn_waiter(tmp_path, "run-abc123")
    try:
        early = _wait_until_polling(proc)
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=30)
        out = early + out
    finally:
        if proc.poll() is None:
            proc.kill()

    # The three things a co needs, in the output it will actually read.
    assert "run-abc123" in out, out
    low = out.lower()
    assert "waiter" in low, f"must name the waiter as the thing that died:\n{out}"
    assert "not the run" in low or "still running" in low, out
    assert "agentctl wait run-abc123" in out, f"must give the re-attach command:\n{out}"
    assert "redispatch" in low.replace("-", ""), \
        f"must warn against redispatching into the same worktree:\n{out}"


def test_the_killed_waiter_names_the_harness_as_the_killer(tmp_path):
    """THE TWO REAL KILLERS. A backgrounded waiter dies either to the harness's
    task-lifetime cap (ARCH-367) or to its memory-pressure reaper — both are the
    harness, neither the run, and only the task notification tells them apart. The
    printed line must name the harness and point at the notification rather than
    asserting a specific cause the notification may contradict, and every line
    after it (still running, re-attach, status, do-not-redispatch) must be exactly
    what it was before this line changed."""
    _make_run(tmp_path, "run-abc123")
    proc = _spawn_waiter(tmp_path, "run-abc123")
    try:
        early = _wait_until_polling(proc)
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=30)
        out = early + out
    finally:
        if proc.poll() is None:
            proc.kill()

    assert (
        "wait: THIS WAITER was killed (SIGTERM) by the harness — usually its "
        "task-lifetime cap; read the task notification for what it says. The "
        "run(s) below are NOT affected."
    ) in out.replace("\n", " "), out
    assert "wait: still running, not the run that died: run-abc123" in out, out
    assert "wait: re-attach (backgrounded) with:  agentctl wait run-abc123" in out, out
    assert "wait: or check liveness with:         agentctl status run-abc123" in out, out
    assert (
        "wait: do NOT redispatch before checking — a second worker in the "
        "same worktree races the first."
    ) in out.replace("\n", " "), out


def test_the_killed_waiter_exits_with_its_own_documented_code(tmp_path):
    """A caller branching on $? must tell "waiter killed" from "runs finished" (0),
    "no run dir" (1) and "--timeout elapsed" (2).

    Asserts the SPECIFIC code, not merely "not 0/1/2". Python's default SIGTERM
    death already returns -15, which satisfies the loose form without any handler
    installed — a test that passes before the fix proves nothing."""
    _make_run(tmp_path, "run-abc123")
    proc = _spawn_waiter(tmp_path, "run-abc123")
    try:
        _wait_until_polling(proc)  # this test asserts on the exit code, not the text
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 3, \
        f"expected the documented killed-waiter code 3, got {proc.returncode}"


def test_sighup_is_handled_too(tmp_path):
    """A backgrounded waiter can be HUPped rather than TERMed when its session goes
    away. Same situation, so it must produce the same message."""
    _make_run(tmp_path, "run-abc123")
    proc = _spawn_waiter(tmp_path, "run-abc123")
    try:
        early = _wait_until_polling(proc)
        proc.send_signal(signal.SIGHUP)
        out, _ = proc.communicate(timeout=30)
        out = early + out
    finally:
        if proc.poll() is None:
            proc.kill()
    assert "run-abc123" in out and "waiter" in out.lower(), out


def test_only_the_unfinished_runs_are_reported_as_still_running(tmp_path):
    """With several run-ids, a co must not be told to re-attach to one that already
    finished — that would send it back to wait on a completed run forever."""
    _make_run(tmp_path, "run-done11", finished=True)
    _make_run(tmp_path, "run-live22")
    proc = _spawn_waiter(tmp_path, "run-done11", "run-live22")
    try:
        early = _wait_until_polling(proc)
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=30)
        out = early + out
    finally:
        if proc.poll() is None:
            proc.kill()
    assert "run-live22" in out, out
    reattach = [ln for ln in out.splitlines() if "agentctl wait" in ln]
    assert reattach, f"no re-attach line:\n{out}"
    assert "run-done11" not in " ".join(reattach), \
        f"must not tell the caller to re-attach to a finished run:\n{out}"


@pytest.fixture
def restore_signals():
    """The disposition tests install handlers in the TEST process; put them back."""
    saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP)}
    yield
    for s, h in saved.items():
        signal.signal(s, h)


def _load_agentctl():
    import importlib.machinery
    import importlib.util
    spec = importlib.util.spec_from_loader(
        "agentctl_sig", loader=importlib.machinery.SourceFileLoader(
            "agentctl_sig", str(AGENTCTL)))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_handler_is_uninstalled_once_the_wait_has_its_answer(tmp_path, restore_signals):
    """THE BLOCKING FINDING (reviewer, #214). The handler was installed before the
    poll loop and never removed, so a SIGTERM arriving while the outcome lines print
    — after the runs had finished — exited 3 on a wait that SUCCEEDED, and
    `spawn --wait` propagated that to its caller as a failure.

    Asserted on the signal DISPOSITION rather than by racing a real signal into the
    print window: that window is a few milliseconds, so a timing-based test would be
    flaky in both directions and would prove nothing on the runs where it passed."""
    m = _load_agentctl()
    dirs = {"run-abc123": tmp_path}

    m._install_killed_waiter_handler(["run-abc123"], dirs)
    assert signal.getsignal(signal.SIGTERM) not in (signal.SIG_DFL, signal.SIG_IGN), \
        "handler must be installed while the waiter is blocked"

    m._uninstall_killed_waiter_handler()
    for sig in (signal.SIGTERM, signal.SIGHUP):
        assert signal.getsignal(sig) is signal.SIG_DFL, \
            f"{sig.name} must be back to default once the wait has decided its exit"


def test_a_normal_completion_is_unchanged(tmp_path):
    """The handler must not disturb the ordinary path: a run that finishes normally
    still exits 0 and prints its outcome line, with no killed-waiter language."""
    _make_run(tmp_path, "run-abc123", finished=True)
    proc = _spawn_waiter(tmp_path, "run-abc123")
    out, _ = proc.communicate(timeout=15)
    assert proc.returncode == 0, out
    assert "run-abc123" in out and "completed" in out, out
    assert "waiter" not in out.lower(), f"clean exit must not mention the waiter:\n{out}"
