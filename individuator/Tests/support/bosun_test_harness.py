#!/usr/bin/env python3
"""A throwaway `bosun_registration.RegistrationServer` for the Swift
mutual-TLS client's integration test (CQ·447 slice 3).

Not part of the Python test suite — invoked only as a subprocess by
`individuator/Tests/IndividuatorNetworkTests/BosunRegistrationClientIntegrationTests.swift`,
and never by pytest or the bash harness. Lives under `Tests/support/`
(not `Tests/IndividuatorNetworkTests/`) so SwiftPM never scans it as Swift
source for any target.

Protocol on stdin/stdout, line-delimited:
  (on startup)        -> prints "PORT <port>"
  "QUERY_SESSIONS\n"  -> prints "SESSIONS <count>"
  "QUIT\n"            -> exits 0

The Library registry lookup (`library_machine_verify`) and the lookup token
(`read_lookup_token`) are monkeypatched to a fixed, always-active,
hardware-custody answer for whichever machine name the connecting client's
certificate names — this test exercises the Swift CLIENT's own certificate
checks and the registration round-trip, not `bosun_registration`'s own
Library-lookup logic, which `tests/test_bosun_registration.py` already
covers.
"""
from __future__ import annotations

import argparse
import sqlite3
import ssl
import sys
import threading
from pathlib import Path


def _describe_handshake_error(exc: BaseException) -> str:
    """The full text of a server-side handshake/connection failure: for
    `ssl.SSLError`, its `reason` (e.g. `CERTIFICATE_VERIFY_FAILED`) and
    `library` (e.g. `SSL`) alongside the OpenSSL message BoringSSL's
    `uncleanShutdown`/`ioOnClosedChannel` on the Swift client side never
    carries, because the client only observes the peer closing without a
    TLS alert — this detail exists only on the server that raised it.
    """
    if isinstance(exc, ssl.SSLError):
        return (f"ssl.SSLError reason={exc.reason!r} library={exc.library!r} "
                f"strerror={exc.strerror!r} args={exc.args!r}")
    return f"{type(exc).__qualname__}: {exc}"


def _log_server_handshake_errors() -> None:
    """Wraps `ssl.SSLContext.wrap_socket`, `ssl.SSLSocket.do_handshake` and
    `ssl.SSLSocket.unwrap` (the three calls `bosun_registration
    .RegistrationServer` makes around every accepted connection — the
    first two in `_handshake_then_serve`, the third in `shutdown_request`,
    round 4's close_notify fix) so a handshake, connection or shutdown
    failure is printed to this harness's own stderr before being re-raised
    unchanged. `bosun_registration.py` itself swallows all three silently
    (`except (ssl.SSLError, OSError): ... return`/`pass`, with no log
    line) — that is production's own choice for a dev-only server, not
    something this throwaway harness changes; this wraps the stdlib calls
    from the OUTSIDE instead; of the three call sites inside the module,
    this changes none of them, and a test pass never depends on these
    lines being printed.
    """
    original_wrap_socket = ssl.SSLContext.wrap_socket
    original_do_handshake = ssl.SSLSocket.do_handshake
    original_unwrap = ssl.SSLSocket.unwrap

    def logging_wrap_socket(self, *args, **kwargs):
        try:
            return original_wrap_socket(self, *args, **kwargs)
        except (ssl.SSLError, OSError) as exc:
            print(f"bosun_test_harness: wrap_socket failed: {_describe_handshake_error(exc)}",
                  file=sys.stderr, flush=True)
            raise

    def logging_do_handshake(self, *args, **kwargs):
        try:
            return original_do_handshake(self, *args, **kwargs)
        except (ssl.SSLError, OSError) as exc:
            print(f"bosun_test_harness: do_handshake failed: {_describe_handshake_error(exc)}",
                  file=sys.stderr, flush=True)
            raise

    def logging_unwrap(self, *args, **kwargs):
        try:
            return original_unwrap(self, *args, **kwargs)
        except (ssl.SSLError, OSError) as exc:
            print(f"bosun_test_harness: unwrap (close_notify) failed: {_describe_handshake_error(exc)}",
                  file=sys.stderr, flush=True)
            raise

    ssl.SSLContext.wrap_socket = logging_wrap_socket
    ssl.SSLSocket.do_handshake = logging_do_handshake
    ssl.SSLSocket.unwrap = logging_unwrap


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lib-dir", required=True, help="path to lib/agent-platform")
    parser.add_argument("--trust-anchor", required=True)
    parser.add_argument("--server-cert", required=True)
    parser.add_argument("--server-key", required=True)
    parser.add_argument("--sessions-db", required=True)
    args = parser.parse_args()

    _log_server_handshake_errors()

    sys.path.insert(0, args.lib_dir)
    import bosun_registration as BR  # noqa: E402

    BR.SESSIONS_DB = Path(args.sessions_db)

    def fake_read_lookup_token(path=None):
        return "test-token"

    def fake_library_machine_verify(fingerprint_sha256, expected_machine, *, base_url=None, token, timeout=None):
        return BR.LookupResult(
            True, status="active", machine=expected_machine, custody="hardware")

    BR.read_lookup_token = fake_read_lookup_token
    BR.library_machine_verify = fake_library_machine_verify

    ssl_context = BR.build_server_ssl_context(
        trust_anchor_path=Path(args.trust_anchor),
        server_cert_path=args.server_cert, server_key_path=args.server_key)

    server = BR.RegistrationServer(("127.0.0.1", 0), ssl_context)
    port = server.socket.getsockname()[1]

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print(f"PORT {port}", flush=True)

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if line == "QUERY_SESSIONS":
            conn = sqlite3.connect(str(BR.SESSIONS_DB))
            try:
                count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            finally:
                conn.close()
            print(f"SESSIONS {count}", flush=True)
        elif line == "QUIT":
            break

    server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
