"""lib/agent-platform/forwarding_transport.py, the HTTP/1.1 client over the
Individuator's forwarding socket (E12-2 slice S5c).

**Never binds a real listening Unix socket.** A sandboxed test run may refuse
local socket binding outright (the same constraint
`individuator_client.py`'s own docstring and `tests/test_bosun_registration.py`
already name for the register socket), so every test here either drives
`_exchange` directly over one end of a `socket.socketpair()` — a connected
pair that needs no `bind()` — or calls the public `request()` against a path
this process controls without ever creating a listener on it.
"""
from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib" / "agent-platform"))

import forwarding_transport as FT  # noqa: E402


def _serve(server_sock, response_chunks, *, delay_s=0.0, read_request=True):
    """Drains the request off `server_sock` (so the client's `sendall` never
    blocks on a full socket buffer) and writes `response_chunks` back one at
    a time, sleeping `delay_s` between them."""
    if read_request:
        server_sock.settimeout(5)
        try:
            while b"\r\n\r\n" not in server_sock.recv(4096):
                pass
        except socket.timeout:
            pass
    for chunk in response_chunks:
        if delay_s:
            time.sleep(delay_s)
        try:
            server_sock.sendall(chunk)
        except OSError:
            return
    server_sock.close()


def _run_exchange(response_chunks, *, delay_s=0.0, timeout_s=5.0, read_request=True,
                   method="GET", target="/items", headers=None, body=b""):
    client_sock, server_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    thread = threading.Thread(
        target=_serve, args=(server_sock, response_chunks),
        kwargs={"delay_s": delay_s, "read_request": read_request})
    thread.start()
    try:
        deadline = time.monotonic() + timeout_s
        return FT._exchange(
            client_sock, method=method, target=target,
            headers=headers or [("Host", "x")], body=body, deadline=deadline)
    finally:
        thread.join(timeout=5)
        client_sock.close()


# --- a whole response, and one split across reads --------------------------

def test_a_whole_response_in_one_write():
    result = _run_exchange([
        b"HTTP/1.1 200 OK\r\nContent-Length: 13\r\nContent-Type: application/json\r\n\r\n"
        b'{"ok": true}\n',
    ])
    assert result.ok
    assert result.response.status == 200
    assert result.response.body == b'{"ok": true}\n'
    assert result.response.header("Content-Type") == "application/json"


def test_a_response_split_across_many_small_reads():
    """The head split mid-header, and the body split mid-value — neither
    boundary coincides with a `recv` call, which is what would happen were
    this transport to assume one `recv` returns one logical unit."""
    whole = b"HTTP/1.1 200 OK\r\nContent-Length: 11\r\n\r\nhello world"
    chunks = [whole[i:i + 3] for i in range(0, len(whole), 3)]
    result = _run_exchange(chunks)
    assert result.ok
    assert result.response.status == 200
    assert result.response.body == b"hello world"


def test_a_chunked_response_split_across_reads():
    whole = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
              b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n")
    chunks = [whole[i:i + 4] for i in range(0, len(whole), 4)]
    result = _run_exchange(chunks)
    assert result.ok
    assert result.response.body == b"hello world"


def test_a_response_with_no_framing_header_has_no_body():
    result = _run_exchange([b"HTTP/1.1 204 No Content\r\n\r\n"])
    assert result.ok
    assert result.response.status == 204
    assert result.response.body == b""


def test_reason_phrase_and_repeated_headers_are_preserved():
    result = _run_exchange([
        b"HTTP/1.1 201 Created\r\nSet-Cookie: a=1\r\nSet-Cookie: b=2\r\nContent-Length: 0\r\n\r\n",
    ])
    assert result.ok
    assert result.response.reason_phrase == "Created"
    assert [v for k, v in result.response.headers if k == "Set-Cookie"] == ["a=1", "b=2"]


# --- the request this module builds -----------------------------------------

def test_the_request_line_and_headers_sent_are_exactly_what_was_given():
    client_sock, server_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    received = {}

    def capture():
        server_sock.settimeout(5)
        data = b""
        while b"\r\n\r\n" not in data:
            data += server_sock.recv(4096)
        received["raw"] = data
        server_sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
        server_sock.close()

    thread = threading.Thread(target=capture)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        result = FT._exchange(
            client_sock, method="POST", target="/items?x=1",
            headers=[("Host", "keel"), ("Authorization", "Bearer tok"), ("Content-Length", "4")],
            body=b"body", deadline=deadline)
    finally:
        thread.join(timeout=5)
        client_sock.close()

    assert result.ok
    assert received["raw"].startswith(b"POST /items?x=1 HTTP/1.1\r\n")
    assert b"Host: keel\r\n" in received["raw"]
    assert b"Authorization: Bearer tok\r\n" in received["raw"]
    assert received["raw"].endswith(b"\r\n\r\nbody")


# --- failures, each with a named reason -------------------------------------

def test_a_read_timeout_is_named_and_bounded_by_the_overall_deadline():
    """The server sends one byte well inside the per-`recv` window, repeated
    past the OVERALL deadline. A transport that re-arms a fixed timeout on
    every call would let this run forever; one bounded by a single
    deadline, recomputed before each `recv`, must still time out."""
    whole = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"
    chunks = [whole[i:i + 1] for i in range(len(whole))]
    result = _run_exchange(chunks, delay_s=0.15, timeout_s=0.3)
    assert not result.ok
    assert result.reason == "forwarding_read_timeout"


def test_a_connection_closed_before_the_head_completes():
    result = _run_exchange([b"HTTP/1.1 200 O"])  # closes before the blank line
    assert not result.ok
    assert result.reason == "forwarding_connection_closed"


def test_a_malformed_status_line():
    result = _run_exchange([b"not an http response\r\n\r\n"])
    assert not result.ok
    assert result.reason == "forwarding_malformed_response"


def test_a_head_larger_than_the_bound_is_refused():
    big_header = b"X-Big: " + b"a" * (FT._MAX_HEAD_BYTES + 10) + b"\r\n"
    result = _run_exchange([b"HTTP/1.1 200 OK\r\n" + big_header + b"\r\n"])
    assert not result.ok
    assert result.reason == "forwarding_head_too_large"


def test_a_body_larger_than_the_bound_is_refused():
    length = FT._MAX_BODY_BYTES + 10
    result = _run_exchange(
        [f"HTTP/1.1 200 OK\r\nContent-Length: {length}\r\n\r\n".encode(), b"a" * 4096],
        timeout_s=2)
    assert not result.ok
    assert result.reason == "forwarding_body_too_large"


def test_the_socket_file_being_absent_is_named(tmp_path):
    result = FT.request("GET", "/items", [("Host", "x")], socket_path=str(tmp_path / "absent.sock"))
    assert not result.ok
    assert result.reason == "forwarding_socket_absent"


def test_a_connect_failure_against_a_non_socket_path_is_named(tmp_path):
    """Exercises `request()`'s own connect step end to end, through a real
    failure `connect()` raises for a path that exists but is not a socket —
    the same `OSError` family a stale, un-listened socket file would raise
    as `ConnectionRefusedError`. A sandboxed test run cannot bind a real
    listening socket to produce that exact errno (see the module
    docstring), so this is the connect failure this suite can construct
    without one."""
    not_a_socket = tmp_path / "not-a-socket"
    not_a_socket.write_text("not a socket")
    result = FT.request("GET", "/items", [("Host", "x")], socket_path=str(not_a_socket))
    assert not result.ok
    assert result.reason.startswith("forwarding_connect_failed")


def test_request_end_to_end_resolves_default_socket_path_lazily(monkeypatch, tmp_path):
    """`socket_path=None` (every real caller) must read `DEFAULT_SOCKET_PATH`
    at CALL time, not freeze whatever it was when this module loaded — the
    same reasoning `individuator_client.register` documents for its own
    defaults, and the thing that makes a test able to point it at a fake
    path at all."""
    sock_path = tmp_path / "forward.sock"
    monkeypatch.setattr(FT, "DEFAULT_SOCKET_PATH", str(sock_path))
    result = FT.request("GET", "/items", [("Host", "x")])
    assert not result.ok
    assert result.reason == "forwarding_socket_absent"
