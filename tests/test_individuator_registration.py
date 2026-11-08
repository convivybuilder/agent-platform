"""`agentctl spawn`'s detached supervisor registers an agent session with the
local Individuator daemon before any agent process starts (E12-2 slice S4a,
agent-platform/architecture/e12-2-individuator-build-plan section "S4.
Dispatch and co sessions register through the local Individuator"), unless
`individuator_client.individuator_installed()` finds no LaunchDaemon plist
at all (the operator, 2026-11-03: a fresh install with no daemon — e.g. Builder
0.7.0's first dispatch — must still launch), in which case it skips
registration entirely rather than refusing.

Drives `agentctl._run_session` directly, the same seam
`tests/test_agentctl.py`'s own `_run_failing_spawn_session`/
`_run_coder_session_capturing_popen` use. Never binds a real socket path —
this sandbox refuses `bind()` (confirmed while writing this file: a plain
`/tmp` path raises `PermissionError`) — and never touches the real daemon
socket at `/var/db/individuator-daemon/register.sock`. Where a test needs a
daemon that actually answers, it runs `individuator_client._register_over_
connected_socket` for real over one end of a `socket.socketpair()`, the same
choice `test_individuator_client.py` and `tests/test_bosun_registration.py`
make. None of this exists on `main` (no registration call in `_run_session`,
no `registration_refused` status, no `individuator_client` module), so every
test below fails there.
"""
from __future__ import annotations

import json
import socket
import types
from pathlib import Path

import pytest

import bosun_authz as AU
import individuator_client as IC

from test_individuator_client import _FakeDaemon

# Same fix as `tests/test_agentctl.py`'s own module-wide mark (CQ·138): driving
# `_run_session`/`_spawn_claude` for real creates a tool-session scratch
# directory (and a reviewer's review-draft directory) under the account's REAL
# home unless redirected — this file's own `_drive_run_session` does exactly
# that drive.
pytestmark = pytest.mark.usefixtures("_tool_session_scratch_dir_off_the_real_account_home")


class _PopenReached(BaseException):
    """Raised by the fake `subprocess.Popen` below instead of starting a real
    process, carrying what `_spawn_claude` was about to launch.

    A `BaseException`, NOT an `Exception` — same reason as
    `test_agentctl.py`'s own `_PopenCaptured`: `_run_session`'s
    `proc = _spawn_claude(model)` call site catches bare `Exception`, which
    would otherwise swallow this into an `errored` outcome and hide whether
    Popen was ever reached."""

    def __init__(self, cmd, env):
        self.cmd = cmd
        self.env = env


def _drive_run_session(agentctl, tmp_path, monkeypatch, *, label: str):
    """Drive `_run_session` far enough to either refuse at registration, or
    reach (and stop at) `_spawn_claude`'s `subprocess.Popen` call. The
    caller must already have monkeypatched `agentctl.IC.register` (or left
    `DEFAULT_SOCKET_PATH` pointed at nothing, for the socket-absent case).
    Returns `(run_dir, worktree, rc, popen_call)`: `rc` is `_run_session`'s
    return value when it returned normally (a registration refusal), `None`
    when `_PopenReached` interrupted it instead; `popen_call` is a
    `_PopenReached` carrying the captured `cmd`/`env` when Popen was
    reached, else `None`."""
    run_dir = tmp_path / f"run-{label}"
    run_dir.mkdir()
    worktree = tmp_path / f"wt-{label}"
    worktree.mkdir()
    stub = tmp_path / "claude-stub"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)

    def _capture_popen(cmd, cwd=None, stdout=None, stderr=None, env=None, stdin=None, **_kw):
        raise _PopenReached(cmd, env)

    monkeypatch.setattr(agentctl, "CLAUDE_BIN", str(stub))
    monkeypatch.setattr(agentctl.subprocess, "Popen", _capture_popen)
    monkeypatch.setattr(agentctl, "_run_origin", lambda parent=None: "interactive")
    monkeypatch.setattr(agentctl, "_preflight_python_worktree", lambda wt: None)
    monkeypatch.setattr(agentctl, "_finalize_run", lambda **kw: None)
    monkeypatch.setattr(agentctl.R, "reserve_slot", lambda entry: None)
    monkeypatch.setattr(agentctl.R, "attach_pid",
                        lambda run_id, pid, pid_started_at=None, **_kw: None)
    monkeypatch.setattr(agentctl.R, "remove_run", lambda rid: True)

    args = types.SimpleNamespace(agent="reviewer", task="t", parent=None,
                                 dry_run=False, label=None, model=None, reason=None)
    rc = None
    popen_call = None
    try:
        rc = agentctl._run_session(
            args=args, agent={"body": "sys", "model_alias": "sonnet"},
            run_id=f"run-{label}", run_dir=run_dir, worktree=worktree,
            profile="p", repo="agent-platform",
            started_at="2026-06-11T00:00:00Z", user_prompt="u")
    except _PopenReached as e:
        popen_call = e
    return run_dir, worktree, rc, popen_call


def _patch_register_over_socketpair(agentctl, monkeypatch, **daemon_kwargs):
    """Points `agentctl.IC.register` at a fake daemon served over one end of
    a `socket.socketpair()`, while still exercising the real
    `IC._register_over_connected_socket` wire logic `register()` itself
    calls after a real `connect()` — never a `bind()`ed socket (this
    sandbox refuses one; see this module's docstring), and never the real
    daemon socket. Returns the `_FakeDaemon` so a test can read
    `.received_request`."""
    daemon_end, client_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    daemon = _FakeDaemon(daemon_end, **daemon_kwargs)

    def _fake_register(*, caller_kind, ceiling, protocol_version="1", **_kw):
        return IC._register_over_connected_socket(
            client_end, caller_kind=caller_kind, ceiling=ceiling,
            protocol_version=protocol_version, read_timeout_s=5.0)

    monkeypatch.setattr(agentctl.IC, "register", _fake_register)
    return daemon


def test_registration_refused_when_the_socket_is_absent_no_process_starts(
        agentctl, tmp_path, monkeypatch):
    """No fake daemon needed: a path nothing has ever connected to refuses
    the same way under the real `IC.register`, with no socket binding at
    all — `DEFAULT_SOCKET_PATH` just needs to name a path with no daemon.
    Relies on `conftest.py`'s own autouse default for `individuator_
    installed` (stubbed `True`, so this is the "Individuator installed,
    daemon down" case, not the "not installed" skip) — see
    `test_registration_refused_when_installed_but_the_socket_is_absent`,
    below, for the same case checked against the real, path-reading
    `individuator_installed` rather than that stub.

    Restores the real `register` over `conftest.py`'s own autouse default
    (which stubs every spawn's registration to succeed, so the suite's many
    unrelated spawn tests don't all have to reach a real daemon)."""
    monkeypatch.setattr(agentctl.IC, "register", agentctl._real_individuator_register)
    monkeypatch.setattr(agentctl.IC, "DEFAULT_SOCKET_PATH", str(tmp_path / "absent.sock"))
    run_dir, _worktree, rc, popen_call = _drive_run_session(
        agentctl, tmp_path, monkeypatch, label="absent")

    assert rc == 2
    assert popen_call is None, "no agent process must start when registration refuses"
    outcome = json.loads((run_dir / "outcome.json").read_text())
    assert outcome["exit_status"] == "registration_refused"
    assert "individuator_socket_absent" in outcome["error_reason"]


def test_registration_refused_when_bosun_authz_is_unavailable(agentctl, tmp_path, monkeypatch):
    """`AU is None` only when the `bosun_authz` import failed at process
    start (an unlinked install.sh symlink) — `_register_agent_session`'s own
    `ceiling_text is None` branch, reached with no fake daemon needed since
    the ceiling is never computed. Before this slice, an unavailable
    `bosun_authz` import only degraded `authz replay`; this branch makes it
    a hard dependency of every spawn instead (D4)."""
    monkeypatch.setattr(agentctl, "AU", None)
    run_dir, _worktree, rc, popen_call = _drive_run_session(
        agentctl, tmp_path, monkeypatch, label="noauthz")

    assert rc == 2
    assert popen_call is None, "no agent process must start when bosun_authz is unavailable"
    outcome = json.loads((run_dir / "outcome.json").read_text())
    assert outcome["exit_status"] == "registration_refused"
    assert "bosun_authz_unavailable" in outcome["error_reason"]


def test_registration_refused_with_the_daemons_own_reason(agentctl, tmp_path, monkeypatch):
    _patch_register_over_socketpair(
        agentctl, monkeypatch, reply={"ok": False, "refused": "peer_uid_not_admitted"})
    run_dir, _worktree, rc, popen_call = _drive_run_session(
        agentctl, tmp_path, monkeypatch, label="refused")

    assert rc == 2
    assert popen_call is None
    outcome = json.loads((run_dir / "outcome.json").read_text())
    assert outcome["exit_status"] == "registration_refused"
    assert "peer_uid_not_admitted" in outcome["error_reason"]


def test_registration_refused_on_a_timeout_with_its_own_reason(agentctl, tmp_path, monkeypatch):
    daemon_end, client_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    daemon = _FakeDaemon(daemon_end, hang_s=1.0)

    def _fake_register(*, caller_kind, ceiling, protocol_version="1", **_kw):
        return IC._register_over_connected_socket(
            client_end, caller_kind=caller_kind, ceiling=ceiling,
            protocol_version=protocol_version, read_timeout_s=0.1)
    monkeypatch.setattr(agentctl.IC, "register", _fake_register)

    run_dir, _worktree, rc, popen_call = _drive_run_session(
        agentctl, tmp_path, monkeypatch, label="timeout")
    daemon.join()

    assert rc == 2
    assert popen_call is None
    outcome = json.loads((run_dir / "outcome.json").read_text())
    assert outcome["exit_status"] == "registration_refused"
    assert "individuator_read_timeout" in outcome["error_reason"]


def test_registered_session_proceeds_and_leaks_the_handle_nowhere(
        agentctl, tmp_path, monkeypatch):
    """With the fake daemon present, the session registers, `_spawn_claude`
    is reached (proving the agent process was allowed to start), and the
    handle appears in no file under the run directory or the rest of
    `tmp_path`, nor in the captured environment or argv."""
    handle = "SECRET-HANDLE-9f3a7c21-do-not-persist"
    daemon = _patch_register_over_socketpair(
        agentctl, monkeypatch,
        reply={"ok": True, "session_id": "sess-xyz", "handle": handle})

    run_dir, worktree, rc, popen_call = _drive_run_session(
        agentctl, tmp_path, monkeypatch, label="ok")
    daemon.join()

    assert rc is None, "_run_session must not return normally once Popen is reached"
    assert popen_call is not None, "a successful registration must let _spawn_claude run"

    # The request carried caller_kind=agent and the run's own ceiling.
    expected_ceiling = AU.ceiling_text_from_entries([AU.scope(AU.PATH, str(worktree))])
    assert daemon.received_request["caller_kind"] == "agent"
    assert daemon.received_request["ceiling"] == expected_ceiling

    # meta.json records that a session was registered, its caller_kind and
    # session_id — never the handle.
    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["individuator_registered"] is True
    assert meta["individuator_caller_kind"] == "agent"
    assert meta["individuator_session_id"] == "sess-xyz"

    # The handle's bytes appear in no file under the run directory or
    # anywhere else under this test's scratch tree (standing in for the run
    # directory and the shared runs root — the handle is a plain Python
    # local that is never passed to any T.* or R.* call, so it can't reach
    # either), nor in the captured environment or argv.
    for path in Path(tmp_path).rglob("*"):
        if path.is_file():
            assert handle not in path.read_bytes().decode("utf-8", errors="replace"), (
                f"handle leaked into {path}")
    assert handle not in " ".join(str(c) for c in popen_call.cmd), "handle leaked into argv"
    for key, value in (popen_call.env or {}).items():
        assert handle not in str(value), f"handle leaked into env var {key}"


def test_registration_skipped_when_the_individuator_is_not_installed(
        agentctl, tmp_path, monkeypatch):
    """No plist anywhere near `DEFAULT_LAUNCH_DAEMONS_DIR` — the ordinary
    shape of a fresh install with no Individuator at all (e.g. Builder
    0.7.0's first dispatch). The run must launch without ever dialing a
    daemon: `IC.register` is left as `conftest.py`'s own autouse stub
    (never called, since `_register_agent_session` returns before reaching
    it), while `individuator_installed` is restored to the real,
    path-reading function pointed at a directory with nothing in it."""
    monkeypatch.setattr(agentctl.IC, "individuator_installed",
                        agentctl._real_individuator_installed)
    monkeypatch.setattr(agentctl.IC, "DEFAULT_LAUNCH_DAEMONS_DIR",
                        str(tmp_path / "no-individuator-here"))

    run_dir, _worktree, rc, popen_call = _drive_run_session(
        agentctl, tmp_path, monkeypatch, label="skip")

    assert rc is None, "_run_session must not return normally once Popen is reached"
    assert popen_call is not None, "a missing Individuator install must not block dispatch"
    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["individuator_registered"] is False
    assert meta["individuator_skipped"] == "not_installed"


def test_registration_refused_when_installed_but_the_socket_is_absent(
        agentctl, tmp_path, monkeypatch):
    """The Individuator IS installed — a real plist on disk, checked by the
    real `individuator_installed` rather than the conftest stub — but its
    daemon is down. This must still refuse the run exactly as before this
    change (`test_registration_refused_when_the_socket_is_absent_no_
    process_starts`, above): a down daemon whose plist is still in place
    must never read as "not installed"."""
    launch_daemons_dir = tmp_path / "LaunchDaemons"
    launch_daemons_dir.mkdir()
    (launch_daemons_dir / agentctl.IC.INDIVIDUATOR_PLIST_NAME).write_text("<plist/>\n")
    monkeypatch.setattr(agentctl.IC, "individuator_installed",
                        agentctl._real_individuator_installed)
    monkeypatch.setattr(agentctl.IC, "DEFAULT_LAUNCH_DAEMONS_DIR", str(launch_daemons_dir))
    monkeypatch.setattr(agentctl.IC, "register", agentctl._real_individuator_register)
    monkeypatch.setattr(agentctl.IC, "DEFAULT_SOCKET_PATH", str(tmp_path / "absent.sock"))

    run_dir, _worktree, rc, popen_call = _drive_run_session(
        agentctl, tmp_path, monkeypatch, label="installed-socket-absent")

    assert rc == 2
    assert popen_call is None, "no agent process must start when registration refuses"
    outcome = json.loads((run_dir / "outcome.json").read_text())
    assert outcome["exit_status"] == "registration_refused"
    assert "individuator_socket_absent" in outcome["error_reason"]
