"""Unix-socket client for the root Individuator daemon's register socket
(E12-2 slice S4a, agent-platform/architecture/e12-2-individuator-build-plan
section "S4. Dispatch and co sessions register through the local
Individuator").

Standard library only: this module runs inside `agentctl spawn`'s detached
supervisor, past the double-fork `_detach_and_run` does with a bare
`os.fork()` and no exec in between, where a dependency this process has not
already fully initialized (e.g. an HTTPS stack) is unsafe to touch for the
first time (see `_mint_run_github_token_out_of_process`'s docstring in
`bin/agentctl` for the crash this exact shape caused once already).

Talks only the wire shape `RegisterRequestHandler.swift` actually implements:
one JSON object per line, written to and read from the Unix-domain socket at
`DEFAULT_SOCKET_PATH`. `register` is the only operation this module sends;
`authorize` is a different slice's (S3's) concern and has no caller here.

Every failure this module can observe — the socket file absent, the
connection refused, a connect or read timeout, a reply that is not one
well-formed JSON line, or a reply whose `ok` field is not `true` — comes
back as `RegisterResult(ok=False, reason=...)`, never an exception. The
caller has exactly one thing to do with any refusal reason, which is refuse
the run the same way (`registration_refused`), so there is no case that
needs telling apart by exception type.

The whole exchange (connect, write, every read) is bounded by ONE absolute
deadline, not by re-arming a fixed per-call timeout before each socket
operation: a peer that sends one byte just inside each `settimeout` window
could otherwise hold this call open indefinitely, which is exactly the hang
this module's own fail-closed contract exists to prevent. See `register`
and `_register_over_connected_socket` for where the deadline is set and
recomputed.
"""
from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import dataclass

DEFAULT_SOCKET_PATH = "/var/db/individuator-daemon/register.sock"

# Where `bin/install-individuator` (its own `--launch-daemons-dir`, default
# unchanged) copies the daemon's LaunchDaemon plist on a real install, and
# the ONLY path this module treats as "the Individuator is installed" (the
# operator, 2026-11-03: a fresh Builder install, with no daemon, must still
# dispatch).
# `--launch-daemons-dir` is a test seam for the installer; no flag or
# environment variable on the spawn side ever changes this constant — the
# only way a test redirects it is `monkeypatch.setattr(individuator_client,
# "DEFAULT_LAUNCH_DAEMONS_DIR", ...)`, the same seam `DEFAULT_SOCKET_PATH`
# already relies on, which nothing a dispatched run controls can reach.
DEFAULT_LAUNCH_DAEMONS_DIR = "/Library/LaunchDaemons"
INDIVIDUATOR_PLIST_NAME = "com.convivy.individuator.plist"

# Bounded so a hung or malicious peer at the other end of the socket cannot
# make a dispatch wait forever — this is a local control socket restricted
# to a handful of admitted uids (`PeerUIDGate`), not a listener exposed to
# any network, so a few seconds is generous rather than tight.
CONNECT_TIMEOUT_S = 5.0
READ_TIMEOUT_S = 5.0

# Bounds the register reply the same way `SocketIO.readLineBounded` bounds
# the request on the daemon side — a reply that never sends a newline must
# not make this read grow without limit.
_MAX_REPLY_BYTES = 64 * 1024


@dataclass(frozen=True)
class RegisterResult:
    """What `register` returns.

    `ok=True` carries `handle` and `session_id` and nothing else — the exact
    shape `RegisterRequestHandler.registerResponseBody`'s `.registered` case
    writes back (`{"ok": true, "session_id": ..., "handle": ...}`). `ok=False`
    carries a named `reason` and never a handle: every refusal branch below
    returns before a `handle` field is ever read off the wire into this
    struct.

    `skipped` is set only by `_register_agent_session`'s own pre-check
    (never by anything in this module, which only ever talks to a daemon
    that is installed) when `individuator_installed()` is `False`: `ok=True`
    with no `handle`/`session_id`, naming why no daemon was ever dialed —
    the one caller of this field (`_register_agent_session`'s call sites in
    `bin/agentctl`) uses it to record `individuator_registered: false` and
    `individuator_skipped: <skipped>` in `meta.json` instead of a real
    registration.
    """
    ok: bool
    handle: str | None = None
    session_id: str | None = None
    reason: str | None = None
    skipped: str | None = None


def individuator_installed(launch_daemons_dir: str | None = None) -> bool:
    """Whether the Individuator's root daemon is installed on this machine —
    the ONLY thing this checks is the LaunchDaemon plist
    `bin/install-individuator` copies in
    (`<launch_daemons_dir>/INDIVIDUATOR_PLIST_NAME`), never whether the
    daemon is actually running: a down daemon with its plist still in place
    must still refuse registration (`register`'s own
    `individuator_socket_absent`/`individuator_connect_failed` reasons), not
    read as "not installed" and silently skip.

    `launch_daemons_dir` defaults to `None` and is resolved against this
    module's own `DEFAULT_LAUNCH_DAEMONS_DIR` global INSIDE the function
    body, the same reason `register`'s `socket_path` does: a test patches
    `individuator_client.DEFAULT_LAUNCH_DAEMONS_DIR` to redirect every
    caller that passes no explicit override, which is every call
    `bin/agentctl` ever makes.

    macOS-only by construction, not by a platform check: launchd loads a
    system daemon only from `/Library/LaunchDaemons`, and
    `bin/install-individuator` only ever writes a macOS LaunchDaemon plist
    there, so a Linux machine — with no `/Library` at all — reads as not
    installed purely because `os.path.isfile` finds nothing, the same path
    this function would take on a Mac that never ran the installer. That is
    correct today. It stops being correct the day something installs a
    systemd unit instead, at which point THIS function has to grow an
    equivalent check deliberately — it will not happen on its own.
    """
    if launch_daemons_dir is None:
        launch_daemons_dir = DEFAULT_LAUNCH_DAEMONS_DIR
    return os.path.isfile(os.path.join(launch_daemons_dir, INDIVIDUATOR_PLIST_NAME))


def register(*, caller_kind: str, ceiling: str, protocol_version: str = "1",
             socket_path: str | None = None,
             connect_timeout_s: float | None = None,
             read_timeout_s: float | None = None) -> RegisterResult:
    """Register one session with the local Individuator daemon and return
    its reply.

    `ceiling` is the opaque TEXT the daemon stores and the Bosun later
    interprets (`bosun_authz.ceiling_text_from_entries`'s output shape) —
    this module gives it no meaning of its own, the same way the daemon
    does not (platform/decisions/0092 Handoff).

    `socket_path`/`connect_timeout_s`/`read_timeout_s` default to `None` and
    are resolved against this module's own globals INSIDE the function body
    rather than as ordinary default-argument values, so a test (or any other
    caller) can point a call with no explicit override at a fake socket by
    patching `individuator_client.DEFAULT_SOCKET_PATH` — an argument default
    bound at `def` time would freeze the real path into every call made
    before the patch, immune to it afterwards.

    `connect_timeout_s + read_timeout_s` is the ABSOLUTE deadline for the
    whole exchange — connect, write and every read — computed once here and
    threaded through to `_register_over_connected_socket`, which recomputes
    the remaining time before each socket operation rather than re-arming
    either fixed timeout. See the module docstring for why a fixed per-call
    timeout alone does not bound the call.
    """
    if socket_path is None:
        socket_path = DEFAULT_SOCKET_PATH
    if connect_timeout_s is None:
        connect_timeout_s = CONNECT_TIMEOUT_S
    if read_timeout_s is None:
        read_timeout_s = READ_TIMEOUT_S
    deadline = time.monotonic() + connect_timeout_s + read_timeout_s

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return RegisterResult(ok=False, reason="individuator_deadline_exceeded")
        sock.settimeout(remaining)
        try:
            sock.connect(socket_path)
        except FileNotFoundError:
            return RegisterResult(ok=False, reason="individuator_socket_absent")
        except (ConnectionRefusedError, PermissionError, NotADirectoryError,
                socket.timeout, OSError) as exc:
            return RegisterResult(ok=False, reason=f"individuator_connect_failed: {exc}")

        return _register_over_connected_socket(
            sock, caller_kind=caller_kind, ceiling=ceiling,
            protocol_version=protocol_version, read_timeout_s=read_timeout_s,
            deadline=deadline)
    finally:
        sock.close()


def _register_over_connected_socket(sock: socket.socket, *, caller_kind: str, ceiling: str,
                                    protocol_version: str, read_timeout_s: float,
                                    deadline: float | None = None) -> RegisterResult:
    """The wire exchange itself, over a socket `register` has already
    connected — split out so a test can drive it directly over one end of a
    `socket.socketpair()`, the same way `tests/test_bosun_registration.py`
    exercises `RegistrationServer`'s protocol logic without ever calling
    `bind()` (a real listening socket needs a sandbox that allows local
    binding, which this suite must not assume).

    `deadline` is an absolute `time.monotonic()` value covering both the
    write below and every read; `register` passes its own connect+read
    budget through here. `None` (every existing direct caller, including
    this module's own tests) falls back to `time.monotonic() +
    read_timeout_s`, so a caller that only cares about bounding the read
    keeps that one `read_timeout_s` knob. Each socket operation below
    recomputes the time remaining against this one deadline and refuses
    with a named reason, WITHOUT making another socket call, the moment
    that remainder is at or below zero — never `settimeout(0)` (non-blocking)
    and never `settimeout(None)` (blocking) by accident."""
    if deadline is None:
        deadline = time.monotonic() + read_timeout_s
    request = {
        "op": "register",
        "caller_kind": caller_kind,
        "ceiling": ceiling,
        "protocol_version": protocol_version,
    }
    line = (json.dumps(request) + "\n").encode("utf-8")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return RegisterResult(ok=False, reason="individuator_deadline_exceeded")
    try:
        sock.settimeout(remaining)
        sock.sendall(line)
    except (socket.timeout, OSError) as exc:
        return RegisterResult(ok=False, reason=f"individuator_write_failed: {exc}")

    try:
        raw = _read_reply_line(sock, deadline)
    except socket.timeout:
        return RegisterResult(ok=False, reason="individuator_read_timeout")
    except OSError as exc:
        return RegisterResult(ok=False, reason=f"individuator_read_failed: {exc}")

    return _parse_reply(raw)


def _read_reply_line(sock: socket.socket, deadline: float) -> bytes:
    """Read until a `\\n`-terminated line or EOF, bounded by
    `_MAX_REPLY_BYTES` AND by `deadline` (an absolute `time.monotonic()`
    value). Each `recv`'s own timeout is the time remaining before
    `deadline`, recomputed on every iteration — never the fixed
    `read_timeout_s` re-armed each time — so a peer that trickles one byte
    just inside every individual `recv` call still cannot hold this loop
    open past `deadline`. A remaining time at or below zero raises
    `socket.timeout` directly, without calling `recv` again."""
    chunks: list[bytes] = []
    total = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout("individuator reply deadline exceeded")
        sock.settimeout(remaining)
        chunk = sock.recv(4096)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if b"\n" in chunk:
            break
        if total > _MAX_REPLY_BYTES:
            raise OSError("individuator reply exceeded the maximum reply size")
    return b"".join(chunks).split(b"\n", 1)[0]


def _parse_reply(raw: bytes) -> RegisterResult:
    if not raw:
        return RegisterResult(ok=False, reason="individuator_no_reply")
    try:
        reply = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return RegisterResult(ok=False, reason="individuator_malformed_reply")
    if not isinstance(reply, dict):
        return RegisterResult(ok=False, reason="individuator_malformed_reply")

    if reply.get("ok") is not True:
        reason = reply.get("refused")
        if not isinstance(reason, str) or not reason:
            reason = "individuator_refused"
        return RegisterResult(ok=False, reason=reason)

    handle = reply.get("handle")
    session_id = reply.get("session_id")
    if not isinstance(handle, str) or not handle or not isinstance(session_id, str) or not session_id:
        return RegisterResult(ok=False, reason="individuator_malformed_reply")

    return RegisterResult(ok=True, handle=handle, session_id=session_id)
