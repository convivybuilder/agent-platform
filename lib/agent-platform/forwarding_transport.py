"""A standard-library HTTP/1.1 client over the Individuator's forwarding
socket (E12-2 slice S5c, agent-platform/architecture/e12-2-individuator-build-plan
section "S5. The forwarding endpoint lets a sessions-only host reach the
Keel"; platform/decisions/0092, "The Individuator carries a sessions-only
host's clients to the Keel").

Sends exactly one HTTP/1.1 request per connection — this module never keeps
a connection open for a second request, matching the forwarding socket's own
rule that each local request gets its own upstream connection. The response
is read by its declared framing, `Content-Length` or chunked
`Transfer-Encoding`, never by waiting for the peer to close.

The whole exchange — connect, write the request, and every read of the
response — is bounded by ONE absolute deadline, computed once and
recomputed before each socket operation, the same shape
`individuator_client.py` already uses for the daemon's register socket and
for the identical reason: a peer that trickles one byte inside every
individual `settimeout` window must not be able to hold a caller open
indefinitely by never finishing.

Every failure this module can observe — the socket file absent, the
connection refused, a connect or read timeout, a response whose head or body
exceeds this module's size bound, or a response this cannot parse as
HTTP/1.1 — comes back as `ForwardResult(ok=False, reason=...)`, never an
exception raised to the caller.
"""
from __future__ import annotations

import socket
import time
from dataclasses import dataclass

DEFAULT_SOCKET_PATH = "/var/db/individuator-daemon/forward.sock"

# Bounded so a hung or malicious peer at the other end of the socket cannot
# make a caller wait forever. The forwarding socket is a local control
# socket restricted to a handful of admitted uids (0092's `PeerUIDGate`
# equivalent for this socket), not a listener exposed to any network, so a
# few seconds for the handshake and a longer allowance for the Keel's own
# round trip is generous rather than tight.
CONNECT_TIMEOUT_S = 10.0
RESPONSE_TIMEOUT_S = 30.0

# Bounds the response head (status line + headers) and body the same way
# `individuator_client._MAX_REPLY_BYTES` bounds its own reply: a peer that
# never sends the framing this module is waiting for must not make this
# read grow without limit.
_MAX_HEAD_BYTES = 64 * 1024
_MAX_BODY_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class ForwardResponse:
    """One parsed HTTP/1.1 response. `headers` preserves the order and
    repetition the wire carried — this module does not fold header lines,
    which is left to a reader that needs that shape."""
    status: int
    reason_phrase: str
    headers: tuple[tuple[str, str], ...]
    body: bytes

    def header(self, name: str) -> str | None:
        """The first header named `name`, case-insensitively, or None."""
        lname = name.lower()
        for key, value in self.headers:
            if key.lower() == lname:
                return value
        return None


@dataclass(frozen=True)
class ForwardResult:
    """What `request` returns. `ok=True` carries `response` and never a
    `reason`; `ok=False` carries a named `reason` and never a `response` —
    every refusal branch below returns before a response is read off the
    wire into this struct."""
    ok: bool
    response: ForwardResponse | None = None
    reason: str | None = None


class _TimedOut(Exception):
    pass


class _ConnectionClosed(Exception):
    pass


class _HeadTooLarge(Exception):
    pass


class _BodyTooLarge(Exception):
    pass


class _Malformed(Exception):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def request(
    method: str, target: str, headers: list[tuple[str, str]], body: bytes = b"", *,
    socket_path: str | None = None,
    connect_timeout_s: float | None = None,
    response_timeout_s: float | None = None,
) -> ForwardResult:
    """Send one HTTP/1.1 request to the forwarding socket and return its
    response.

    `target` is the request target exactly as it should appear on the
    request line (path plus an optional `?query`, origin form) — this
    module does not build it from a URL, so a caller forwards the byte-for-
    byte target it already matched or built, the same discipline 0092 asks
    of the Individuator's own matcher.

    `socket_path`/`connect_timeout_s`/`response_timeout_s` default to
    `None` and are resolved against this module's own globals INSIDE the
    function body, rather than as ordinary default-argument values, so a
    test can point a call with no explicit override at a fake socket by
    patching `forwarding_transport.DEFAULT_SOCKET_PATH` — an argument
    default bound at `def` time would freeze the real path into every call
    made before the patch, immune to it afterwards (the same reasoning
    `individuator_client.register` already documents for its own
    defaults).
    """
    if socket_path is None:
        socket_path = DEFAULT_SOCKET_PATH
    if connect_timeout_s is None:
        connect_timeout_s = CONNECT_TIMEOUT_S
    if response_timeout_s is None:
        response_timeout_s = RESPONSE_TIMEOUT_S
    deadline = time.monotonic() + connect_timeout_s + response_timeout_s

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return ForwardResult(ok=False, reason="forwarding_deadline_exceeded")
        sock.settimeout(remaining)
        try:
            sock.connect(socket_path)
        except FileNotFoundError:
            return ForwardResult(ok=False, reason="forwarding_socket_absent")
        except (ConnectionRefusedError, PermissionError, NotADirectoryError,
                socket.timeout, OSError) as exc:
            return ForwardResult(ok=False, reason=f"forwarding_connect_failed: {exc}")

        return _exchange(sock, method=method, target=target, headers=headers, body=body,
                          deadline=deadline)
    finally:
        sock.close()


def _build_request_bytes(
    method: str, target: str, headers: list[tuple[str, str]], body: bytes,
) -> bytes:
    lines = [f"{method} {target} HTTP/1.1\r\n".encode("ascii")]
    for name, value in headers:
        lines.append(f"{name}: {value}\r\n".encode("latin-1"))
    lines.append(b"\r\n")
    return b"".join(lines) + body


def _exchange(
    sock: socket.socket, *, method: str, target: str, headers: list[tuple[str, str]],
    body: bytes, deadline: float,
) -> ForwardResult:
    request_bytes = _build_request_bytes(method, target, headers, body)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return ForwardResult(ok=False, reason="forwarding_deadline_exceeded")
    try:
        sock.settimeout(remaining)
        sock.sendall(request_bytes)
    except (socket.timeout, OSError) as exc:
        return ForwardResult(ok=False, reason=f"forwarding_write_failed: {exc}")

    reader = _BufferedReader(sock, deadline)
    try:
        status_line, header_block = reader.read_head()
    except _TimedOut:
        return ForwardResult(ok=False, reason="forwarding_read_timeout")
    except _HeadTooLarge:
        return ForwardResult(ok=False, reason="forwarding_head_too_large")
    except _ConnectionClosed:
        return ForwardResult(ok=False, reason="forwarding_connection_closed")
    except OSError as exc:
        return ForwardResult(ok=False, reason=f"forwarding_read_failed: {exc}")

    parsed = _parse_status_and_headers(status_line, header_block)
    if parsed is None:
        return ForwardResult(ok=False, reason="forwarding_malformed_response")
    status, reason_phrase, resp_headers = parsed

    try:
        resp_body = reader.read_body(resp_headers)
    except _TimedOut:
        return ForwardResult(ok=False, reason="forwarding_read_timeout")
    except _BodyTooLarge:
        return ForwardResult(ok=False, reason="forwarding_body_too_large")
    except _ConnectionClosed:
        return ForwardResult(ok=False, reason="forwarding_connection_closed")
    except _Malformed as exc:
        return ForwardResult(ok=False, reason=f"forwarding_malformed_response: {exc.detail}")
    except OSError as exc:
        return ForwardResult(ok=False, reason=f"forwarding_read_failed: {exc}")

    return ForwardResult(ok=True, response=ForwardResponse(
        status=status, reason_phrase=reason_phrase, headers=tuple(resp_headers), body=resp_body))


def _header(headers: list[tuple[str, str]], name: str) -> str | None:
    lname = name.lower()
    for key, value in headers:
        if key.lower() == lname:
            return value
    return None


def _parse_status_and_headers(status_line: bytes, header_block: bytes):
    try:
        status_text = status_line.decode("latin-1")
    except UnicodeDecodeError:
        return None
    parts = status_text.split(" ", 2)
    if len(parts) < 2 or not parts[0].startswith("HTTP/1."):
        return None
    try:
        status = int(parts[1])
    except ValueError:
        return None
    if not (100 <= status <= 599):
        return None
    reason_phrase = parts[2] if len(parts) > 2 else ""
    headers: list[tuple[str, str]] = []
    if header_block:
        for line in header_block.split(b"\r\n"):
            if not line:
                continue
            name, sep, value = line.partition(b":")
            if not sep:
                return None
            try:
                headers.append((name.decode("latin-1").strip(), value.decode("latin-1").strip()))
            except UnicodeDecodeError:
                return None
    return status, reason_phrase, headers


class _BufferedReader:
    """Reads one response's head and body off `sock`, bounded by one
    absolute `deadline` recomputed before every `recv` — never by a fixed
    timeout re-armed on each call, which is exactly what would let a peer
    that sends one byte just inside each window hold this open past
    `deadline`."""

    def __init__(self, sock: socket.socket, deadline: float):
        self._sock = sock
        self._deadline = deadline
        self._buf = b""

    def _recv(self) -> bytes:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise _TimedOut()
        self._sock.settimeout(remaining)
        try:
            return self._sock.recv(4096)
        except socket.timeout:
            raise _TimedOut()

    def read_head(self) -> tuple[bytes, bytes]:
        while b"\r\n\r\n" not in self._buf:
            if len(self._buf) > _MAX_HEAD_BYTES:
                raise _HeadTooLarge()
            chunk = self._recv()
            if not chunk:
                raise _ConnectionClosed()
            self._buf += chunk
        head, _, rest = self._buf.partition(b"\r\n\r\n")
        if len(head) > _MAX_HEAD_BYTES:
            raise _HeadTooLarge()
        self._buf = rest
        status_line, _, header_block = head.partition(b"\r\n")
        return status_line, header_block

    def read_body(self, headers: list[tuple[str, str]]) -> bytes:
        transfer_encoding = _header(headers, "transfer-encoding")
        if transfer_encoding is not None and transfer_encoding.strip().lower() == "chunked":
            return self._read_chunked()
        content_length = _header(headers, "content-length")
        if content_length is not None:
            try:
                length = int(content_length.strip())
            except ValueError:
                raise _Malformed("non-integer content-length")
            if length < 0:
                raise _Malformed("negative content-length")
            if length > _MAX_BODY_BYTES:
                raise _BodyTooLarge()
            return self._read_exact(length)
        # No framing header at all: a response with no body (e.g. a 204, or
        # one answering a HEAD) — never read until the peer closes, which
        # this transport never waits for.
        return b""

    def _read_exact(self, length: int) -> bytes:
        while len(self._buf) < length:
            chunk = self._recv()
            if not chunk:
                raise _ConnectionClosed()
            self._buf += chunk
            if len(self._buf) > _MAX_BODY_BYTES:
                raise _BodyTooLarge()
        data, rest = self._buf[:length], self._buf[length:]
        self._buf = rest
        return data

    def _read_line(self) -> bytes:
        while b"\r\n" not in self._buf:
            if len(self._buf) > _MAX_HEAD_BYTES:
                raise _HeadTooLarge()
            chunk = self._recv()
            if not chunk:
                raise _ConnectionClosed()
            self._buf += chunk
        line, _, rest = self._buf.partition(b"\r\n")
        self._buf = rest
        return line

    def _read_chunked(self) -> bytes:
        out = bytearray()
        while True:
            size_line = self._read_line()
            size_text = size_line.split(b";", 1)[0].strip()
            try:
                size = int(size_text, 16)
            except ValueError:
                raise _Malformed("non-hex chunk size")
            if size < 0:
                raise _Malformed("negative chunk size")
            if size == 0:
                # The trailer section: zero or more header lines, then the
                # blank line that ends the body. Read and discard — this
                # transport has no caller that needs trailer fields.
                while self._read_line() != b"":
                    pass
                return bytes(out)
            out += self._read_exact(size)
            if len(out) > _MAX_BODY_BYTES:
                raise _BodyTooLarge()
            terminator = self._read_exact(2)
            if terminator != b"\r\n":
                raise _Malformed("missing chunk terminator")
