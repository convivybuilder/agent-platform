"""bin/co-rollover — real tmux, end to end (ADR-0069 Slice 5).

Runs against a PRIVATE, throwaway tmux server, isolated by giving it its own
`TMUX_TMPDIR` — NEVER the operator's real server, whose sockets live under the
real `$TMPDIR`. `tmux -L <name>` was the first instinct, but this repo's own
tmux callers (`tmux_transcript.run`, `co_rollover._run`, `bin/session-agent`)
all invoke bare `tmux` with no `-L` — naming a socket only here would put this
test's session on one socket while the code under test (`session_state`,
imported in-process) resolved a DIFFERENT one (the implicit "default" socket)
in the same directory, and the two would never meet. `TMUX_TMPDIR` isolation
keeps the "default" socket name but confines the whole server under a
directory this test owns and tears down — private by containment, not by
name.

`base-index 1` / `pane-base-index 1` are set via `-f <config>` on the very
first tmux command (which starts the server): the machine's real
`~/.tmux.conf` sets both, and Deck's window-numbering assumption
(`WindowLeaf.numbers` is `2...count`) depends on window 1 being the session's
own — matching that here is what makes "the successor becomes window 1"
after close-out a meaningful assertion rather than an artifact of tmux's
factory default of 0.

No real Claude session starts anywhere in this file. `co` is stubbed: it
prints the exact banner `verify_up` polls for, then `exec -a claude`s into a
long-lived `sleep` — a real process whose argv[0] this repo's own
`_is_claude()` recognises, without ever invoking the real `claude` CLI.

ENVIRONMENT, THE HARD PART. A pane's shell inherits `HOME` (and everything
else that isn't tmux's small auto-updated set — `DISPLAY`, `SSH_AUTH_SOCK`,
etc.) from the SESSION's environment as captured AT CREATION, not from
whichever later client happens to run `new-window` (verified experimentally:
a `HOME` set only on a later `new-window` call never reaches the pane; one set
at `new-session` time does). This machine's real `~/.zshenv` — sourced by
every zsh invocation, login or not — does `export PATH="$HOME/.local/bin:
$PATH"`, and `~/.local/bin/co` is the REAL `co`. So the session has to be
CREATED with the isolated `HOME`/`PATH` this file wants the pane to see, not
merely have `bin/co-rollover` invoked with them — `_env_for` builds that one
environment and every tmux-facing call in a test uses it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

import session_state as SS
import tmux_sandbox

REPO =Path(__file__).resolve().parent.parent
CO_ROLLOVER = REPO / "bin" / "co-rollover"

# CI run 34794779253 (agent-platform#566, round 3) failed all five tests in
# this file on the `python` job's then-hosted `ubuntu-latest` runner, before the
# job moved to `convivy-linux` — every one at
# `spawn_successor`'s `zsh -lc "co -r ... || exec zsh"`, with `verify_up`
# reporting an EMPTY captured pane for the full 30s timeout (never even the
# `|| exec zsh` fallback's shell). Reproduced locally by pointing an isolated
# tmux server's `new-window` at a nonexistent binary: tmux reports success
# (a valid `window_index`/`pane_id`, exactly what `spawn_successor` parses),
# then destroys the window within tens of milliseconds — `capture-pane`
# against it moments later returns "can't find window", the same shape as
# `_run` returning `None` on every poll. That is only possible if the
# runner's `zsh` itself doesn't exist for `new-window` to exec, not a `co`
# failure inside a working zsh (a missing `co` is exactly `stub_bin_never_up`
# below, and that one keeps the window alive via `|| exec zsh`, which needs
# zsh to already be running to have any effect).
# The runner's own published manifest for that exact image
# (actions/runner-images release `ubuntu24/20260907.300`, the "Runner Image"
# named in the job's "Set up job" step) names neither `zsh` nor `tmux` among
# its installed packages — and `tmux` demonstrably works in that same run (a
# real `new-session` returns a real pane id before the failure), so the
# manifest is not a complete inventory of the base image; `zsh` specifically
# is what a `which zsh` there would show absent, matching every symptom
# above.
# This can never happen on the machine `co-rollover` actually runs
# on: `bin/co-rollover` and the `/rollover` skill only ever run inside a real
# `co` session on this repo's own Mac (the mini, or a developer's machine),
# where `zsh` has shipped as the default system shell since macOS Catalina.
# `remain-on-exit` (added to `spawn_successor` in this same round) closes the
# diagnosability gap for every failure slow enough to matter in production —
# a hung `git pull`, the knowledge auth preflight — by keeping the pane's
# exit status readable instead of letting the window vanish with it; it
# cannot save a runner missing zsh entirely, because there the wrapper
# interpreter itself never starts long enough to set that option on.
_NO_ZSH_REASON = (
    "no zsh on this host — spawn_successor execs `zsh -lc ...` for the "
    "successor's pane, and this host has none (see the module-level comment "
    "above `_NO_ZSH_REASON` for the CI run this was diagnosed against). "
    "The real `co-rollover` never runs anywhere but this repo's own macOS "
    "machines, where zsh is the shipped default shell, so this is a "
    "CI-runner gap, not a reachable production failure."
)
pytestmark = pytest.mark.skipif(shutil.which("zsh") is None, reason=_NO_ZSH_REASON)


@pytest.fixture
def private_tmux(tmp_path, monkeypatch):
    reason = tmux_sandbox.sandbox_refusal()
    if reason:
        pytest.skip(reason)
    # A tmux unix-socket path is capped well under pytest's own `tmp_path`
    # (nested under the test's full name, easily 100+ bytes on its own) — a
    # `TMUX_TMPDIR` there fails with "File name too long" before tmux even
    # gets to isolate anything. `/tmp` directly keeps the whole path short.
    socket_dir = Path(tempfile.mkdtemp(prefix="cr-tmux-"))
    monkeypatch.setenv("TMUX_TMPDIR", str(socket_dir))
    monkeypatch.delenv("TMUX", raising=False)

    cfg = tmp_path / "tmux.conf"
    cfg.write_text("set -g base-index 1\nsetw -g pane-base-index 1\n")

    yield cfg

    # Named explicitly by `-S`, built from `socket_dir` above — NOT a bare
    # `tmux kill-server`, which resolves against whatever `TMUX_TMPDIR`/socket
    # is real in the ambient environment and would reach the operator's own
    # server the moment that reliance on `monkeypatch`'s teardown ordering
    # breaks. `tmux`'s own default socket path under a given `TMUX_TMPDIR` is
    # `<dir>/tmux-<uid>/default` (no `-L` was ever passed at session-creation
    # time, so the name is the factory default).
    socket_path = socket_dir / f"tmux-{os.getuid()}" / "default"
    subprocess.run(["tmux", "-S", str(socket_path), "kill-server"],
                   capture_output=True, timeout=10)
    shutil.rmtree(socket_dir, ignore_errors=True)


@pytest.fixture
def stub_bin(tmp_path):
    """A stub `co`: prints the banner `verify_up` polls for, then becomes a
    long-lived process named `claude` (argv[0], via `exec -a`) — no real
    Claude session starts."""
    d = tmp_path / "stubbin"
    d.mkdir()
    co = d / "co"
    co.write_text(
        "#!/bin/bash\n"
        "echo 'remote-control is active (stub)'\n"
        "exec -a claude sleep 600\n"
    )
    co.chmod(0o755)
    return d


@pytest.fixture
def stub_bin_never_up(tmp_path):
    """A stub `co` that never prints the remote-control banner — it fails
    immediately, the way a genuinely broken `co` would. Mirrors the real
    failure shape `spawn_successor`'s `co -r ... || exec zsh` fallback
    produces when `co` itself cannot run: the failing command's own
    "command not found" text (one of `verify_up`'s recognised failure
    signatures) lands in the pane, then the `|| exec zsh` fallback drops it
    into an interactive shell."""
    d = tmp_path / "stubbin-never-up"
    d.mkdir()
    co = d / "co"
    co.write_text(
        "#!/bin/bash\n"
        "echo 'co: command not found' >&2\n"
        "exit 127\n"
    )
    co.chmod(0o755)
    return d


def _env_for(tmp_path, stub_bin: Path) -> dict:
    """The ONE environment every tmux-facing call in a test uses — the
    session is created with it (which is what makes the pane see it; see the
    module docstring) and `bin/co-rollover` is invoked with it too.

    `HOME` is an isolated, empty directory: no `.zshenv` of the operator's to
    re-inject `~/.local/bin` (and its real `co`) ahead of the stub, and no
    real `~/.cache/co-session-health` for `rollover_policy` to write into.
    """
    home = tmp_path / "isolated-home"
    home.mkdir(exist_ok=True)
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PATH"] = os.pathsep.join([
        str(stub_bin), str(Path(sys.executable).parent),  # this checkout's .venv/bin (python3.11)
        "/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin",
        "/usr/bin", "/bin", "/usr/sbin", "/sbin",
    ])
    return env


def _windows(session: str, env: dict) -> list[str]:
    out = subprocess.run(["tmux", "list-windows", "-t", session, "-F", "#{window_index}"],
                         capture_output=True, text=True, timeout=10, env=env).stdout
    return out.split()


def _new_session(cfg_path: Path, session: str, env: dict) -> str:
    subprocess.run(["tmux", "-f", str(cfg_path), "new-session", "-d", "-s", session,
                    "-x", "80", "-y", "24"], env=env, check=True, timeout=10)
    return subprocess.run(["tmux", "list-panes", "-t", session, "-F", "#{pane_id}"],
                          capture_output=True, text=True, timeout=10, env=env).stdout.strip()


def _spawn_via_co_rollover(tmp_path, session: str, predecessor_pane: str, env: dict,
                           predecessor_session_id: str) -> Path:
    """Runs `bin/co-rollover` for real, as the predecessor would. Returns the
    handoff dir."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, env=env, timeout=10, check=True)

    handoff_dir = tmp_path / "handoff"
    handoff_dir.mkdir()
    (handoff_dir / "handoff.md").write_text("# handoff\n")

    co_env = dict(env)
    co_env["TMUX_PANE"] = predecessor_pane
    co_env["CLAUDE_CODE_SESSION_ID"] = predecessor_session_id
    co_env["CONVIVY_SESSION_DIR"] = str(handoff_dir)

    proc = subprocess.run([sys.executable, str(CO_ROLLOVER), "--product", "repo"],
                          cwd=repo_dir, env=co_env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    return handoff_dir


def test_co_rollover_spawns_verifies_and_selects_the_successor(
        private_tmux, stub_bin, tmp_path, monkeypatch):
    env = _env_for(tmp_path, stub_bin)
    # `SS.session_state` below now resolves `asks_open`/`asks_answered`
    # through `slate.ask_counts` (CQ-358), and `slate.state_root()` re-reads
    # `Path.home()` on every call — so the IN-PROCESS call needs the same
    # isolated `HOME` the subprocess env above already carries, or it would
    # create `~/.local/state/convivy/slate/<session>.json` for real.
    monkeypatch.setenv("HOME", env["HOME"])
    session = "co-rollover-test-spawn"
    predecessor_pane = _new_session(private_tmux, session, env)
    assert predecessor_pane

    _spawn_via_co_rollover(tmp_path, session, predecessor_pane, env, "pred-session-1")

    assert _windows(session, env) == ["1", "2"]

    active_window = subprocess.run(
        ["tmux", "display-message", "-t", session, "-p", "#{window_index}"],
        capture_output=True, text=True, timeout=10, env=env).stdout.strip()
    assert active_window == "2", "the active window must be the successor's, not the predecessor's"

    # `session-agent state <name>` -> `session_state.session_state`, called
    # in-process (this test's own process already has TMUX_TMPDIR pointed at
    # the private server, so it resolves the same one bin/co-rollover used).
    state = SS.session_state(session)
    assert state["alive"] is True

    # The rollover_policy state file bin/co-rollover wrote, under the
    # isolated HOME its own subprocess used — read directly rather than
    # through the in-process `rollover_policy` module, whose CACHE_DIR was
    # already resolved (at import time, by conftest.py) against THIS
    # process's real HOME.
    rolling = json.loads(
        (Path(env["HOME"]) / ".cache" / "co-session-health" /
         "rollover-pred-session-1.json").read_text())
    assert rolling["state"] == "rolling"
    assert rolling["successor_window"] == 2
    assert rolling["predecessor"]["pane_id"] == predecessor_pane


def test_co_rollover_close_out_never_fires_without_picked_up(private_tmux, stub_bin, tmp_path):
    env = _env_for(tmp_path, stub_bin)
    session = "co-rollover-test-no-pickup"
    predecessor_pane = _new_session(private_tmux, session, env)
    _spawn_via_co_rollover(tmp_path, session, predecessor_pane, env, "pred-session-2")

    # No `--picked-up` call at all. Give the (nonexistent) close-out every
    # chance to fire before asserting it did not.
    time.sleep(1.0)
    assert _windows(session, env) == ["1", "2"], "the predecessor's window must survive with no pickup"


def test_co_rollover_picked_up_closes_out_and_the_successor_becomes_window_1(
        private_tmux, stub_bin, tmp_path):
    env = _env_for(tmp_path, stub_bin)
    session = "co-rollover-test-pickup"
    predecessor_pane = _new_session(private_tmux, session, env)
    handoff_dir = _spawn_via_co_rollover(tmp_path, session, predecessor_pane, env, "pred-session-3")
    assert _windows(session, env) == ["1", "2"]

    picked_env = dict(env)
    picked_env["CLAUDE_CODE_SESSION_ID"] = "succ-session-3"

    proc = subprocess.run(
        [sys.executable, str(CO_ROLLOVER), "--picked-up", str(handoff_dir / "handoff.md")],
        env=picked_env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"

    # The close-out is DETACHED (kill-pane + move-window -r run in a spawned,
    # independent subprocess), so give it a moment to land rather than
    # asserting the instant `--picked-up` returns.
    deadline = time.monotonic() + 10.0
    windows = _windows(session, env)
    while windows != ["1"] and time.monotonic() < deadline:
        time.sleep(0.2)
        windows = _windows(session, env)
    assert windows == ["1"], f"expected only window 1 after close-out, got {windows}"

    side = json.loads((handoff_dir / "co-rollover-state.json").read_text())
    assert side["picked_up"] is True
    assert side["successor"]["session_id"] == "succ-session-3"


def test_co_rollover_picked_up_is_idempotent(private_tmux, stub_bin, tmp_path):
    """A retried `--picked-up` (the successor re-running it, or a duplicate
    dispatch) must not attempt a second kill-pane against an already-gone
    pane, and must not reset the pickup timestamp."""
    env = _env_for(tmp_path, stub_bin)
    session = "co-rollover-test-pickup-twice"
    predecessor_pane = _new_session(private_tmux, session, env)
    handoff_dir = _spawn_via_co_rollover(tmp_path, session, predecessor_pane, env, "pred-session-4")

    picked_env = dict(env)
    picked_env["CLAUDE_CODE_SESSION_ID"] = "succ-session-4"

    first = subprocess.run(
        [sys.executable, str(CO_ROLLOVER), "--picked-up", str(handoff_dir / "handoff.md")],
        env=picked_env, capture_output=True, text=True, timeout=30)
    assert first.returncode == 0

    deadline = time.monotonic() + 10.0
    while _windows(session, env) != ["1"] and time.monotonic() < deadline:
        time.sleep(0.2)
    assert _windows(session, env) == ["1"]

    first_side = json.loads((handoff_dir / "co-rollover-state.json").read_text())

    second = subprocess.run(
        [sys.executable, str(CO_ROLLOVER), "--picked-up", str(handoff_dir / "handoff.md")],
        env=picked_env, capture_output=True, text=True, timeout=30)
    assert second.returncode == 0, f"a retried --picked-up must not fail: {second.stderr!r}"
    time.sleep(0.5)

    assert _windows(session, env) == ["1"], "a second pickup must not disturb an already-closed-out session"
    second_side = json.loads((handoff_dir / "co-rollover-state.json").read_text())
    assert second_side["picked_up_at"] == first_side["picked_up_at"]


def test_co_rollover_failed_spawn_parks_and_leaves_no_side_channel(
        private_tmux, stub_bin_never_up, tmp_path):
    """THE CENTRAL SAFETY CLAIM, exercised through `bin/co-rollover` itself
    rather than only at the `co_rollover.py`/`rollover_policy.py` unit
    level: a failed verification exits non-zero, never marks `rolling` (no
    side channel for a successor to ever find — there is no successor), and
    now — Slice 6 — DOES write a `parked` rollover state so the failure is
    durable rather than reported-and-forgotten. The isolated `HOME` this
    test builds has no token file, so `signal_client.post_alert` no-ops
    (`"skip"`, never reaching a real API) and `issue_id` lands
    `None` — this test asserts the park happens regardless, the same
    "logging failure still parks" contract the CLI-level stubbed tests pin."""
    env = _env_for(tmp_path, stub_bin_never_up)
    session = "co-rollover-test-failed-spawn"
    predecessor_pane = _new_session(private_tmux, session, env)

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, env=env, timeout=10, check=True)
    handoff_dir = tmp_path / "handoff"
    handoff_dir.mkdir()
    (handoff_dir / "handoff.md").write_text("# handoff\n")

    co_env = dict(env)
    co_env["TMUX_PANE"] = predecessor_pane
    co_env["CLAUDE_CODE_SESSION_ID"] = "pred-session-failed"
    co_env["CONVIVY_SESSION_DIR"] = str(handoff_dir)

    proc = subprocess.run([sys.executable, str(CO_ROLLOVER), "--product", "repo"],
                          cwd=repo_dir, env=co_env, capture_output=True, text=True, timeout=60)
    assert proc.returncode != 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "did not come up" in proc.stderr
    assert "has parked" in proc.stderr

    # The failed successor's window is left in place (for the operator to
    # inspect), but the predecessor's own window (1) is untouched and
    # nothing was ever marked rolling.
    assert _windows(session, env) == ["1", "2"]
    assert not (handoff_dir / "co-rollover-state.json").exists(), \
        "a failed spawn must leave no side channel for a successor to find"
    rollover_state = (Path(env["HOME"]) / ".cache" / "co-session-health" /
                      "rollover-pred-session-failed.json")
    assert rollover_state.is_file(), "a failed spawn must record a durable parked state"
    state = json.loads(rollover_state.read_text())
    assert state["state"] == "parked"
    assert state["reason"] == "verify_up_failed"
    assert state["issue_id"] is None, "no token in the isolated HOME, so logging no-ops"
