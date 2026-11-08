"""Unit tests for `lib/agent-platform/individuator_client.py` (E12-2 slice
S4a): the Unix-socket client `agentctl spawn` uses to register an agent
session with the root Individuator daemon before launch.

This module does not exist on `main`, so every test below fails there with
`ModuleNotFoundError: No module named 'individuator_client'`.

The daemon-response tests drive a FAKE daemon over one end of a
`socket.socketpair()`, never a `bind()`ed/listening socket and never the
real daemon socket (`/var/db/individuator-daemon/register.sock`) — the same
choice `tests/test_bosun_registration.py` makes for its own Unix-socket
protocol tests, and for the same reason: a real listening socket needs a
sandbox that allows local binding, which this suite must not assume
(confirmed here: `bind()` on an ordinary `/tmp` path raises
`PermissionError` under this repo's own sandboxed test run)."""
from __future__ import annotations

import json
import socket
import threading
import time

import pytest

import individuator_client as IC


@pytest.fixture(autouse=True)
def _real_register(agentctl, monkeypatch):
    """`conftest.py`'s own autouse default stubs every spawn's registration
    to succeed, so the rest of the suite's many unrelated spawn tests never
    reach a real daemon — but `agentctl.IC` IS this module (`individuator_
    client`), the same object, not a copy, so that stub would otherwise
    shadow the very function this file exists to test. Restores the real
    `register` and the real `individuator_installed` (that default also
    stubs the latter `True`, for the same reason), which — being requested
    here, in this module, rather than in conftest.py — applies after that
    default (pytest instantiates autouse fixtures from a parent conftest
    before one defined in the test module itself) and so wins for every
    test in this file."""
    monkeypatch.setattr(agentctl.IC, "register", agentctl._real_individuator_register)
    monkeypatch.setattr(agentctl.IC, "individuator_installed",
                        agentctl._real_individuator_installed)


class _FakeDaemon:
    """A real Unix-domain peer for exactly one connection, over one end of a
    `socket.socketpair()` the caller hands it — never a path, never
    `bind()`/`listen()`/`accept()`. Records the one JSON request line it
    received (as a parsed dict) and replies with `reply` (a dict, serialized
    the way `SocketIO.writeJSONLine` does: one `\\n`-terminated JSON line),
    `raw_reply` (raw bytes, for a malformed-reply case `json.dumps` cannot
    produce), or nothing at all for `hang_s` seconds (a daemon that accepted
    the connection but never answers)."""

    def __init__(self, daemon_end: socket.socket, *, reply: dict | None = None,
                 raw_reply: bytes | None = None, hang_s: float = 0.0):
        self.received_request: dict | None = None
        self._sock = daemon_end
        self._reply = reply
        self._raw_reply = raw_reply
        self._hang_s = hang_s
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        self._sock.settimeout(10.0)
        try:
            data = b""
            while b"\n" not in data:
                chunk = self._sock.recv(4096)
                if not chunk:
                    break
                data += chunk
            line = data.split(b"\n", 1)[0]
            if line:
                self.received_request = json.loads(line.decode("utf-8"))
        except OSError:
            pass
        if self._hang_s:
            time.sleep(self._hang_s)
            self._sock.close()
            return
        try:
            if self._raw_reply is not None:
                self._sock.sendall(self._raw_reply)
            elif self._reply is not None:
                self._sock.sendall((json.dumps(self._reply) + "\n").encode("utf-8"))
        except OSError:
            pass
        self._sock.close()

    def join(self) -> None:
        self._thread.join(timeout=5.0)


def _client_against_fake_daemon(**daemon_kwargs):
    """Builds a connected pair, starts a `_FakeDaemon` on one end, and
    returns `(daemon, client_end)` — the caller drives
    `individuator_client._register_over_connected_socket(client_end, ...)`
    directly, the same seam `register()` itself calls after a real
    `connect()`."""
    daemon_end, client_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    daemon = _FakeDaemon(daemon_end, **daemon_kwargs)
    return daemon, client_end


def test_register_refuses_with_a_named_reason_when_the_socket_is_absent(tmp_path):
    """No daemon anywhere near this path — the ordinary shape of the daemon
    being down, which must refuse rather than hang or raise. Needs no fake
    daemon at all: connecting to a path nothing has ever bound fails the
    same way regardless of what this sandbox allows."""
    result = IC.register(caller_kind="agent", ceiling="[]",
                         socket_path=str(tmp_path / "absent.sock"))
    assert result.ok is False
    assert result.handle is None
    assert result.reason == "individuator_socket_absent"


def test_register_returns_the_daemons_named_refusal():
    daemon, client_end = _client_against_fake_daemon(
        reply={"ok": False, "refused": "peer_uid_not_admitted"})
    try:
        result = IC._register_over_connected_socket(
            client_end, caller_kind="agent", ceiling="[]", protocol_version="1",
            read_timeout_s=5.0)
    finally:
        client_end.close()
        daemon.join()
    assert result.ok is False
    assert result.handle is None
    assert result.reason == "peer_uid_not_admitted"


def test_register_times_out_with_a_named_reason_when_the_daemon_never_answers():
    daemon, client_end = _client_against_fake_daemon(hang_s=1.0)
    try:
        result = IC._register_over_connected_socket(
            client_end, caller_kind="agent", ceiling="[]", protocol_version="1",
            read_timeout_s=0.1)
    finally:
        client_end.close()
        daemon.join()
    assert result.ok is False
    assert result.reason == "individuator_read_timeout"


def test_register_returns_the_handle_and_session_id_on_success():
    daemon, client_end = _client_against_fake_daemon(
        reply={"ok": True, "session_id": "sess-1", "handle": "deadbeef"})
    try:
        result = IC._register_over_connected_socket(
            client_end, caller_kind="agent", ceiling='["path:/work/allowed"]',
            protocol_version="1", read_timeout_s=5.0)
    finally:
        client_end.close()
        daemon.join()
    assert result.ok is True
    assert result.handle == "deadbeef"
    assert result.session_id == "sess-1"
    assert result.reason is None


def test_register_refuses_a_reply_that_is_not_valid_json():
    daemon, client_end = _client_against_fake_daemon(raw_reply=b"not json at all\n")
    try:
        result = IC._register_over_connected_socket(
            client_end, caller_kind="agent", ceiling="[]", protocol_version="1",
            read_timeout_s=5.0)
    finally:
        client_end.close()
        daemon.join()
    assert result.ok is False
    assert result.reason == "individuator_malformed_reply"


def test_register_refuses_a_success_reply_with_no_handle():
    """`ok: true` with a missing/empty `handle` must not be treated as a
    successful registration — the daemon's own success shape always carries
    one (`RegisterRequestHandler.registerResponseBody`'s `.registered`
    case)."""
    daemon, client_end = _client_against_fake_daemon(reply={"ok": True, "session_id": "sess-1"})
    try:
        result = IC._register_over_connected_socket(
            client_end, caller_kind="agent", ceiling="[]", protocol_version="1",
            read_timeout_s=5.0)
    finally:
        client_end.close()
        daemon.join()
    assert result.ok is False
    assert result.handle is None
    assert result.reason == "individuator_malformed_reply"


def test_register_refuses_an_ok_reply_with_no_refusal_name():
    """`ok: false` with no `refused` string still refuses — never surfaces as
    a success for lack of a name to blame."""
    daemon, client_end = _client_against_fake_daemon(reply={"ok": False})
    try:
        result = IC._register_over_connected_socket(
            client_end, caller_kind="agent", ceiling="[]", protocol_version="1",
            read_timeout_s=5.0)
    finally:
        client_end.close()
        daemon.join()
    assert result.ok is False
    assert result.reason == "individuator_refused"


def test_register_sends_the_wire_shape_the_daemon_expects():
    """`RegisterRequestHandler.handleRegister` reads `op`, `caller_kind`,
    `ceiling` (a String) and `protocol_version` off the request — exactly
    these four keys, nothing else, matching what the real daemon's Swift
    handler parses."""
    daemon, client_end = _client_against_fake_daemon(
        reply={"ok": True, "session_id": "sess-1", "handle": "h"})
    try:
        IC._register_over_connected_socket(
            client_end, caller_kind="agent", ceiling='["path:/work/allowed"]',
            protocol_version="1", read_timeout_s=5.0)
    finally:
        client_end.close()
        daemon.join()
    assert daemon.received_request == {
        "op": "register",
        "caller_kind": "agent",
        "ceiling": '["path:/work/allowed"]',
        "protocol_version": "1",
    }


def test_register_refuses_at_the_deadline_even_though_each_read_arrives_in_time():
    """A peer that sends one byte at a time, each one well inside the
    per-call read timeout, never finishing the line — before the absolute
    deadline, re-arming `read_timeout_s` before every `recv` let a peer like
    this hold the call open for as long as it kept trickling, contradicting
    this module's own fail-closed contract. `read_timeout_s` is deliberately
    large (10s, far longer than the trickle needs to run) so a per-call
    timeout alone would not catch this; the explicit `deadline`, far sooner,
    must still cut the call off."""
    daemon_end, client_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)

    def _trickle():
        try:
            for _ in range(40):
                daemon_end.sendall(b"x")
                time.sleep(0.05)
        except OSError:
            pass
        finally:
            daemon_end.close()

    thread = threading.Thread(target=_trickle, daemon=True)
    thread.start()
    started = time.monotonic()
    try:
        result = IC._register_over_connected_socket(
            client_end, caller_kind="agent", ceiling="[]", protocol_version="1",
            read_timeout_s=10.0, deadline=time.monotonic() + 0.3)
    finally:
        client_end.close()
        thread.join(timeout=5.0)
    elapsed = time.monotonic() - started

    assert result.ok is False
    assert result.reason == "individuator_read_timeout"
    assert elapsed < 2.0, f"call should refuse near the deadline, not the trickle's own pace (took {elapsed}s)"


def test_register_refuses_without_another_call_once_the_deadline_has_already_passed():
    """A deadline already in the past (e.g. connect alone used up the whole
    budget) must refuse immediately, never call `settimeout(0)` (non-blocking)
    or let a `None` timeout fall through to a blocking socket."""
    daemon_end, client_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        result = IC._register_over_connected_socket(
            client_end, caller_kind="agent", ceiling="[]", protocol_version="1",
            read_timeout_s=5.0, deadline=time.monotonic() - 1.0)
    finally:
        client_end.close()
        daemon_end.close()
    assert result.ok is False
    assert result.reason == "individuator_deadline_exceeded"


def test_the_default_socket_path_matches_the_daemons_own_default():
    """Pinned against `IndividuatorPaths.defaultDaemonSocketPath`
    (`individuator/Sources/IndividuatorCore/Paths.swift`) so the two sides
    cannot drift apart silently."""
    assert IC.DEFAULT_SOCKET_PATH == "/var/db/individuator-daemon/register.sock"


def test_individuator_installed_is_false_when_the_plist_is_absent(tmp_path, monkeypatch):
    """A fresh install with no plist at all — the ordinary shape of a
    machine that has never run `bin/install-individuator` (e.g. a fresh
    Builder install)."""
    monkeypatch.setattr(IC, "DEFAULT_LAUNCH_DAEMONS_DIR", str(tmp_path))
    assert IC.individuator_installed() is False


def test_individuator_installed_is_true_when_the_plist_is_present(tmp_path, monkeypatch):
    monkeypatch.setattr(IC, "DEFAULT_LAUNCH_DAEMONS_DIR", str(tmp_path))
    (tmp_path / IC.INDIVIDUATOR_PLIST_NAME).write_text("<plist/>\n")
    assert IC.individuator_installed() is True


def test_individuator_installed_ignores_a_plist_under_a_custom_directory(tmp_path, monkeypatch):
    """Only the ONE system path counts — `--launch-daemons-dir` is a test
    seam for `bin/install-individuator`, not something the spawn side ever
    reads, so a plist sitting under any other directory (here, a
    `--launch-daemons-dir` an installer test might have used) must read as
    not installed when `individuator_installed()` is called the way
    `agentctl` actually calls it: no explicit override, resolved against
    `DEFAULT_LAUNCH_DAEMONS_DIR` alone."""
    real_default_dir = tmp_path / "real-default"
    real_default_dir.mkdir()
    custom_dir = tmp_path / "custom-launch-daemons-dir"
    custom_dir.mkdir()
    (custom_dir / IC.INDIVIDUATOR_PLIST_NAME).write_text("<plist/>\n")

    monkeypatch.setattr(IC, "DEFAULT_LAUNCH_DAEMONS_DIR", str(real_default_dir))
    assert IC.individuator_installed() is False


def test_individuator_installed_is_false_when_the_launch_daemons_dir_does_not_exist():
    """Standing in for a Linux machine, where `/Library/LaunchDaemons`
    itself does not exist (there is no `/Library` at all): `os.path.isfile`
    on a path under a missing parent directory returns `False` rather than
    raising, so this reads as not installed exactly like a Mac that has
    never run the installer — no platform check needed or present."""
    assert IC.individuator_installed(
        "/this/path/does/not/exist/on/this/machine") is False
