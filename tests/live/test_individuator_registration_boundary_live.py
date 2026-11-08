"""Live check: nothing readable as a file from inside a run registers a
session with the Bosun in the Individuator's place (CQ·447 slice 4;
platform/decisions/0092).

That decision is explicit about what the Individuator's socket actually
admits: "every process running as the operator's account can use the
socket, every agent included," and the Bosun sees the same machine
certificate on all of it — the endpoint "proves which machine called, not
which process," and "this is a policy and audit layer, and gives no
containment" (0092, "What it does not protect" / "The catch"). A call to
that socket from the operator uid registering a session is the designed
behaviour, not a gap, and the positive control below exists to show exactly
that — telling a refused attempt apart from a Bosun that is simply down.

The property this file proves is narrower: nothing a caller can read AS A
FILE, or build from files alone, registers a session. Four attempts, every
one a caller without the Individuator daemon's own cooperation:

1. A plain HTTPS POST to the Bosun's `/sessions` endpoint, no client
   certificate. Refused by the TLS handshake itself (`build_server_ssl_context`
   sets `CERT_REQUIRED`): `check_client_certificate` is never reached.
2. A mutual-TLS POST signed by a throwaway CA the Bosun's trust anchor does
   not chain to. Refused the same way, for the same reason.
3. A mutual-TLS POST with a SELF-signed certificate whose SAN names the
   Bosun's own host — read at run time off the URI SAN the Bosun's own
   server certificate presents in the TLS handshake
   (`_bosun_server_san_machine_name` below), never written here as a
   literal — not issued by anyone, so the same chain check refuses it too,
   and the TLS alert the server sends back names the chain/issuer problem
   (`_ATTEMPT_3_ALERTS` below).
4. A mutual-TLS POST with a certificate valid for some OTHER machine, found
   by STAT alone at one documented, operator-reachable location — and, only
   when a match exists, opened and loaded to present it in the handshake,
   the same act the daemon itself performs, never one that prints or writes
   the key. Nothing is deployed there yet — `bosun_registration`'s own
   module docstring says the file-held clientAuth certificate plan it is
   written against is "now in progress", not landed — so this case is
   expected to record that and skip itself until it does. Unlike attempts
   1-3, such a certificate CAN pass the TLS chain check, so the Bosun may
   refuse it at the application layer instead of the handshake: this attempt
   accepts either an HTTP refusal naming one of `_ATTEMPT_4_REFUSAL_REASONS`
   or a TLS alert in `_ATTEMPT_4_ALERTS`, and fails on anything else —
   including a 2xx, a connection reset, a plain `OSError`, or a refusal
   naming an unrelated reason — rather than reading it as a refusal.

THE POSITIVE CONTROL (opt-in `AGENTCTL_LIVE_INDIVIDUATOR_POSITIVE=1`) is what
tells a refused attempt apart from a Bosun that is simply down: it makes the
one call the daemon's own socket is designed to accept — a registration
from this process's own real, unsandboxed uid, the same account an operator
or an agent both run as on this single-operator machine — and asserts the
daemon actually registers a session and a matching row lands in the Bosun's
own `sessions.db`. There is no `bin/agentctl` subcommand that wraps this
socket yet (checked: `bin/agentctl bosun` has only `revoke-machine`), so the
positive control calls the socket directly — the path such a subcommand
would wrap once one exists.

Run it by hand, after merge, against the real deployed Bosun and daemon:

    AGENTCTL_LIVE_INDIVIDUATOR=1 .venv/bin/python -m pytest -s \\
        tests/live/test_individuator_registration_boundary_live.py

    AGENTCTL_LIVE_INDIVIDUATOR=1 AGENTCTL_LIVE_INDIVIDUATOR_POSITIVE=1 \\
        .venv/bin/python -m pytest -s \\
        tests/live/test_individuator_registration_boundary_live.py

Not part of the CI gate: CI has neither the real Bosun's server certificate
(0092 decision #6) nor the real daemon's socket, and the positive control
spends a real registration.

Nothing here reads a credential file for its own sake. Attempts 2 and 3
build and sign their own throwaway certificates in `tmp_path` rather than
touching the real Library root; attempt 4 finds a candidate by `stat`
alone, never reading a file the test's own uid cannot already read, and
opens a readable match only to present it in the TLS handshake — the same
act the daemon itself performs, never one that prints or writes the key;
the positive control sends a fixed, fictitious registration body over the
socket, never anything read from disk. No test deletes or revokes the
session the positive control creates.
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import ssl
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

import builder_root as BR
import bosun_registration as BRG

pytestmark = pytest.mark.skipif(
    os.environ.get("AGENTCTL_LIVE_INDIVIDUATOR") != "1",
    reason="live Individuator registration-boundary check: set "
           "AGENTCTL_LIVE_INDIVIDUATOR=1 to probe the real deployed Bosun and "
           "the real Individuator daemon")

# `individuator/Sources/IndividuatorCore/Paths.swift`'s own
# `defaultDaemonSocketPath` — there is no Python build of that package to
# import the constant from, so this mirrors it by value; override for a test
# rig whose daemon binds elsewhere.
DAEMON_SOCKET_PATH = os.environ.get(
    "AGENTCTL_LIVE_INDIVIDUATOR_SOCKET", "/var/db/individuator-daemon/register.sock")

# The real Bosun's registration listener. `BRG.REGISTRATION_HOST` is a BIND
# address (0.0.0.0 by default) — not something a client connects to — so the
# default here is loopback, since the Bosun and the Individuator both run on
# this same machine.
BOSUN_HOST = os.environ.get("AGENTCTL_LIVE_INDIVIDUATOR_BOSUN_HOST", "127.0.0.1")
BOSUN_PORT = int(os.environ.get("AGENTCTL_LIVE_INDIVIDUATOR_BOSUN_PORT", str(BRG.REGISTRATION_PORT)))

REGISTRATION_BODY = {"account": "live-boundary-check", "caller_kind": "agent",
                     "ceiling": "[]", "protocol_version": "1"}

# One documented, operator-reachable candidate location for attempt 4 — see
# this module's own docstring on why this is expected to be empty today.
ANOTHER_MACHINE_CERT_DIR = BR.real_account_home() / ".config" / "convivy" / "machine-certs"

_MACHINE_SAN_PREFIX = "convivy://machine/"

# OpenSSL alert reason codes (`ssl.SSLError.reason`) for the TLS 1.3 alerts
# the Bosun's own handshake sends back when it refuses a client's
# certificate (`build_server_ssl_context` + `check_client_certificate`,
# `lib/agent-platform/bosun_registration.py`) — never the exception
# message text, which is this stack's own wording and not a stable
# contract. A server that requires a client certificate and gets none
# sends `certificate_required`; one that cannot verify the presented chain
# sends `unknown_ca`, though some stacks send `bad_certificate` instead.
_ATTEMPT_1_ALERTS = ("TLSV13_ALERT_CERTIFICATE_REQUIRED",)
_ATTEMPT_2_ALERTS = ("TLSV1_ALERT_UNKNOWN_CA", "SSLV3_ALERT_BAD_CERTIFICATE")
_ATTEMPT_3_ALERTS = ("TLSV1_ALERT_UNKNOWN_CA", "SSLV3_ALERT_BAD_CERTIFICATE")

# Attempt 4's certificate chains to the Library's real root (it is issued for
# some OTHER machine, not a throwaway), so `check_client_certificate`'s own
# shape checks pass and the Bosun's TLS listener may still complete the
# handshake — unlike attempts 1-3, which never get past it. But some OpenSSL
# stacks run their own post-handshake chain verification and can still alert
# here; this is the same pair attempts 2/3 accept, for the same reason
# (`_refusal_mismatch`'s own docstring).
_ATTEMPT_4_ALERTS = ("TLSV1_ALERT_UNKNOWN_CA", "SSLV3_ALERT_BAD_CERTIFICATE")

# `handle_registration`'s own refusal reasons (`lib/agent-platform/
# bosun_registration.py`) that can name a certificate whose TLS chain is
# valid but which is NOT this machine's: the Library's registry has no row
# for this fingerprint (`machine_unknown`), the row it has is revoked or
# expired (`machine_inactive`), the SAN's claimed machine does not match the
# fingerprint's own registered machine (`machine_mismatch`), or the
# fingerprint's machine is active but its key custody is not in
# `REQUIRED_CUSTODY` — the file-held case this directory's own certificates
# are expected to be, per this module's own docstring
# (`machine_custody_not_hardware`). Every other named reason in
# `_REFUSAL_HTTP_STATUS` (`bad_certificate`, `eku_missing_clientauth`,
# `san_not_exactly_one`, `malformed_body`, `malformed_ceiling`,
# `lookup_credential_missing`, `library_route_missing`, `library_error`,
# `library_unreachable`, `library_malformed`) means something else is
# wrong — a shape problem this well-formed certificate should never trigger,
# or a Bosun/Library-side fault — and must fail this attempt rather than
# pass it.
_ATTEMPT_4_REFUSAL_REASONS = frozenset({
    "machine_unknown", "machine_inactive", "machine_mismatch",
    "machine_custody_not_hardware",
})


def _refusal_mismatch(exc: BaseException, allowed_reasons: tuple[str, ...]) -> str | None:
    """None if `exc` is a genuine refusal — an `ssl.SSLError` whose OpenSSL
    alert `.reason` is one of `allowed_reasons` — otherwise a message
    naming `exc`'s type and reason, for the caller to fail on. A plain
    `OSError` (connection refused, reset, timed out) carries no `.reason`
    and always mismatches; so does an `ssl.SSLError` whose `.reason` names
    a different alert. Never reads the exception's message text."""
    reason = getattr(exc, "reason", None) if isinstance(exc, ssl.SSLError) else None
    if reason in allowed_reasons:
        return None
    return f"not refused with an expected TLS alert: {type(exc).__name__}, reason={reason!r}"


def _safe_body_summary(body: dict | None) -> str:
    """A description of `body` safe to print or put into a message: the
    `refused` field if present, and the body's serialized length. Never the
    body itself, which may carry a session token or a machine name from the
    Library's own record. Every attempt below, and the positive control,
    describes a response through this (or `_safe_http_summary`) rather than
    formatting the body itself."""
    reason = body.get("refused") if isinstance(body, dict) else None
    body_len = len(json.dumps(body)) if body is not None else 0
    return f"reason={reason!r}, body_len={body_len}"


def _safe_http_summary(status: int, body: dict | None) -> str:
    """`_safe_body_summary`, prefixed with the HTTP status."""
    return f"HTTP {status}, {_safe_body_summary(body)}"


def _attempt_4_http_mismatch(status: int, body: dict | None) -> str | None:
    """None if `status`/`body` is an HTTP refusal naming one of
    `_ATTEMPT_4_REFUSAL_REASONS` — otherwise a message for the caller to
    fail on. Never the body itself; see `_safe_http_summary`."""
    reason = body.get("refused") if isinstance(body, dict) else None
    if status != 200 and reason in _ATTEMPT_4_REFUSAL_REASONS:
        return None
    return (f"not a named refusal of another machine's certificate: "
            f"{_safe_http_summary(status, body)}")


def _attempt_4_exc_mismatch(exc: BaseException) -> str | None:
    """None if `exc` is a genuine TLS-layer refusal of a certificate valid
    for another machine (`_ATTEMPT_4_ALERTS`) — otherwise a message for the
    caller to fail on. Delegates to `_refusal_mismatch`, the same check
    attempts 1-3 use."""
    return _refusal_mismatch(exc, _ATTEMPT_4_ALERTS)


# ---------------------------------------------------------------------------
# certificate fixtures — every one built here, in tmp_path, never read from disk
# ---------------------------------------------------------------------------


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def make_root(cn: str = "throwaway test root"):
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    name = _name(cn)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def make_machine_cert(root_key, root_cert, *, machine: str):
    """A clientAuth leaf, signed by `root_key`/`root_cert`, naming `machine`
    — the shape `check_client_certificate` expects, used here only to
    present a cert the Bosun's OWN trust anchor does not know (attempt 2)."""
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(machine))
        .issuer_name(root_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.UniformResourceIdentifier(f"{_MACHINE_SAN_PREFIX}{machine}")]),
            critical=False)
        .add_extension(x509.ExtendedKeyUsage((ExtendedKeyUsageOID.CLIENT_AUTH,)), critical=False)
        .sign(root_key, hashes.SHA256())
    )
    return key, cert


def make_self_signed_machine_cert(*, machine: str):
    """A certificate with no issuer but itself — attempt 3's self-signed
    certificate whose subject names the Bosun's own host. Carries the same
    clientAuth EKU and machine SAN a real one would, so the ONLY thing wrong
    with it is that nothing signed it."""
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    name = _name(machine)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.UniformResourceIdentifier(f"{_MACHINE_SAN_PREFIX}{machine}")]),
            critical=False)
        .add_extension(x509.ExtendedKeyUsage((ExtendedKeyUsageOID.CLIENT_AUTH,)), critical=False)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def write_pem_cert(path: Path, cert) -> None:
    path.write_bytes(cert.public_bytes(Encoding.PEM))


def write_pem_key(path: Path, key) -> None:
    path.write_bytes(key.private_bytes(
        Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption()))


# ---------------------------------------------------------------------------
# the real mTLS listener — one raw POST per attempt
# ---------------------------------------------------------------------------


def _client_ssl_context(*, certfile: Path | None = None, keyfile: Path | None = None) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # This test probes the CLIENT-authentication boundary, never the
    # server's own identity — verifying the server's chain would need the
    # real Library root, which this file must never read (see its own
    # docstring). Disabling it here changes nothing about whether the
    # SERVER accepts or refuses OUR certificate, which is the whole thing
    # under test.
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    if certfile is not None:
        ctx.load_cert_chain(certfile=str(certfile), keyfile=str(keyfile))
    return ctx


def _post_sessions(*, certfile: Path | None = None, keyfile: Path | None = None,
                    timeout: float = 10) -> tuple[int, dict | None]:
    """One raw mTLS POST to `/sessions`. Raises `ssl.SSLError`/`OSError` on a
    handshake or connection failure — every attempt below decides what that
    means for itself."""
    ctx = _client_ssl_context(certfile=certfile, keyfile=keyfile)
    with socket.create_connection((BOSUN_HOST, BOSUN_PORT), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=None) as tls:
            payload = json.dumps(REGISTRATION_BODY).encode()
            request = (f"POST /sessions HTTP/1.1\r\nHost: {BOSUN_HOST}\r\n"
                       f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n").encode() + payload
            tls.sendall(request)
            resp = b""
            while True:
                chunk = tls.recv(4096)
                if not chunk:
                    break
                resp += chunk
    head, _, raw_body = resp.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1])
    return status, (json.loads(raw_body) if raw_body else None)


def _bosun_reachable() -> bool:
    """A bare TCP probe, no TLS — tells "nothing is listening" (the Bosun
    is not deployed on this machine yet, 0092 decision #6) apart from "TLS
    refused us", which is the only thing the rest of this test means to
    measure."""
    try:
        with socket.create_connection((BOSUN_HOST, BOSUN_PORT), timeout=5):
            return True
    except OSError:
        return False


def _bosun_server_san_machine_name() -> str:
    """The name on the Bosun's OWN server certificate, read from its URI SAN
    (`convivy://machine/<name>`, the shape 0092 decision #1 defines for a
    CLIENT certificate — the server's carries the identical shape per 0092
    decision #6), off a plain TLS handshake that presents no client
    certificate of its own.

    The server sends its certificate in the handshake's first round trip,
    before it has any need to know whether — or what — the client will
    present, so this works whether or not the connection goes on to
    complete mutual authentication. Attempt 3 needs this value for its own
    self-signed certificate's subject; this file never writes the Bosun's
    host name as a literal.
    """
    ctx = _client_ssl_context()
    with socket.create_connection((BOSUN_HOST, BOSUN_PORT), timeout=10) as sock:
        with ctx.wrap_socket(sock, server_hostname=None) as tls:
            der = tls.getpeercert(binary_form=True)
    assert der, "no server certificate observed in the Bosun's TLS handshake"
    cert = x509.load_der_x509_certificate(der)
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    for uri in san.get_values_for_type(x509.UniformResourceIdentifier):
        if uri.startswith(_MACHINE_SAN_PREFIX):
            return uri[len(_MACHINE_SAN_PREFIX):]
    raise AssertionError(
        "the Bosun's server certificate carries no convivy://machine/<name> URI SAN")


def _require_bosun_reachable(attempt: str) -> str:
    """Re-probes the Bosun immediately before `attempt`: a full TCP connect
    plus reading its server certificate — `_bosun_server_san_machine_name`
    already does both, so this calls it. The one-time `_bosun_reachable()`
    probe at the top of the test cannot see the Bosun going down, wedging,
    or dropping the connection between then and this attempt's own POST;
    a failure caught here fails the test outright as Bosun-unreachable,
    never read as one of the attempt's own refusals."""
    try:
        return _bosun_server_san_machine_name()
    except (ssl.SSLError, OSError) as exc:
        pytest.fail(f"{attempt}: Bosun unreachable immediately before this attempt "
                    f"({type(exc).__name__}: {exc}) — not counted as a refusal")


# ---------------------------------------------------------------------------
# the daemon's local Unix socket — one raw request (the positive control only)
# ---------------------------------------------------------------------------


def _register_over_socket(*, timeout: float = 10) -> dict:
    """One connect, one request, one read, against the real daemon socket —
    never retried. Returns `{"connected": bool, "error": str | None,
    "response": dict | None}`; `error` and `response` are mutually
    exclusive with each other and with a `connected=False` row holding no
    `response`."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(DAEMON_SOCKET_PATH)
    except OSError as exc:
        sock.close()
        return {"connected": False, "error": str(exc), "response": None}
    try:
        sock.sendall(json.dumps({
            "op": "register", "caller_kind": "agent",
            "ceiling": "[]", "protocol_version": "1",
        }).encode() + b"\n")
        raw = sock.recv(65536)
    finally:
        sock.close()
    try:
        return {"connected": True, "error": None, "response": json.loads(raw.decode("utf-8"))}
    except ValueError:
        return {"connected": True, "error": f"unparseable response, {len(raw)} bytes", "response": None}


# ---------------------------------------------------------------------------
# the Bosun's own session store — read-only, except for the positive control
# ---------------------------------------------------------------------------


def _resolve_sessions_db() -> Path:
    """The Bosun's real `sessions.db`, resolved independently of
    `tests/conftest.py`'s BUILDER_ROOT override. That override makes
    `bosun_registration.SESSIONS_DB` (imported above as `BRG.SESSIONS_DB`)
    point at a data root with no database on it — the bug this file exists
    to fix (CQ·601): every "a session row was written" check below compared
    0 to 0 against that path and passed no matter what the Bosun did.

    Honours BOSUN_SESSIONS_DB when set, the same override
    `bosun_registration` itself reads. Otherwise `BR.root()`, never
    `BR.data_root()`: `root()` is the one entry point in `builder_root.py`
    that is a TRUST ANCHOR and therefore never reads BUILDER_ROOT, so absent
    an explicit override it resolves to the same data root the deployed
    com.convivy.bosun-server computes."""
    override = os.environ.get("BOSUN_SESSIONS_DB")
    return Path(override) if override else BR.root() / "bosun" / "sessions.db"


SESSIONS_DB = _resolve_sessions_db()


def _open_sessions_db_readonly(db_path: Path) -> sqlite3.Connection:
    """A read-only connection to `db_path`, via sqlite3's `mode=ro` URI. A
    missing file fails here, at connect time, naming `db_path` — never
    treated as an empty database."""
    try:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError as exc:
        raise RuntimeError(
            f"the Bosun's sessions database is missing at {db_path} ({exc})") from None


def _session_count(db_path: Path = SESSIONS_DB) -> int:
    """Row count in `db_path`'s `sessions` table. A live run has no 0 to
    fall back to: a missing database (`_open_sessions_db_readonly`) or a
    missing `sessions` table fails outright, naming `db_path`, rather than
    counting as 0 — see `_resolve_sessions_db`."""
    conn = _open_sessions_db_readonly(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    except sqlite3.OperationalError as exc:
        raise RuntimeError(
            f"the Bosun's sessions database at {db_path} has no sessions table ({exc})") from None
    finally:
        conn.close()


def _session_row(session_id: str, db_path: Path = SESSIONS_DB) -> dict | None:
    conn = _open_sessions_db_readonly(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# attempt 4's candidate search — stat only, never opened
# ---------------------------------------------------------------------------


def _find_another_machine_cert() -> tuple[Path, Path] | None:
    """A (cert, key) pair for some OTHER machine's clientAuth certificate,
    reachable by the operator uid alone. The search itself is `is_file()`/
    `is_dir()` only — it never reads a file the test's own uid cannot
    already read. Its one caller hands a match to
    `ssl.SSLContext.load_cert_chain`, which DOES read the key's bytes, to
    present it in the TLS handshake — the same act the daemon itself
    performs when it registers with this certificate, never one that
    prints or writes the key elsewhere."""
    if not ANOTHER_MACHINE_CERT_DIR.is_dir():
        return None
    for cert in sorted(ANOTHER_MACHINE_CERT_DIR.glob("*.crt")):
        key = cert.with_suffix(".key")
        if key.is_file():
            return cert, key
    return None


# ---------------------------------------------------------------------------
# the test
# ---------------------------------------------------------------------------


def test_no_certificate_a_run_can_read_registers_a_session(tmp_path):
    if not _bosun_reachable():
        pytest.skip(f"the Bosun's registration listener is not reachable at "
                    f"{BOSUN_HOST}:{BOSUN_PORT} — nothing to probe")

    failed: list[str] = []

    def expect(ok: bool, why: str) -> None:
        if not ok:
            failed.append(why)
            print(f"FAILED CHECK: {why}")

    count_before = _session_count()

    # Attempt 1: no client certificate at all.
    _require_bosun_reachable("attempt 1")
    try:
        status, body = _post_sessions()
        expect(False, f"attempt 1 (no client cert): the server answered "
                      f"{_safe_http_summary(status, body)} instead of refusing the handshake")
    except (ssl.SSLError, OSError) as exc:
        mismatch = _refusal_mismatch(exc, _ATTEMPT_1_ALERTS)
        expect(mismatch is None, f"attempt 1: {mismatch}")
        if mismatch is None:
            print(f"[attempt 1] refused by TLS alert: reason={exc.reason}")
    expect(_session_count() == count_before, "attempt 1: a session row was written")

    # Attempt 2: a certificate signed by a trust anchor the Bosun does not know.
    _require_bosun_reachable("attempt 2")
    root_key, root_cert = make_root()
    key, cert = make_machine_cert(root_key, root_cert, machine="unknown-ca-machine")
    cert_path, key_path = tmp_path / "attempt2.crt", tmp_path / "attempt2.key"
    write_pem_cert(cert_path, cert)
    write_pem_key(key_path, key)
    try:
        status, body = _post_sessions(certfile=cert_path, keyfile=key_path)
        expect(False, f"attempt 2 (unknown CA): the server answered "
                      f"{_safe_http_summary(status, body)} instead of refusing the handshake")
    except (ssl.SSLError, OSError) as exc:
        mismatch = _refusal_mismatch(exc, _ATTEMPT_2_ALERTS)
        expect(mismatch is None, f"attempt 2: {mismatch}")
        if mismatch is None:
            print(f"[attempt 2] refused by TLS alert: reason={exc.reason}")
    expect(_session_count() == count_before, "attempt 2: a session row was written")

    # Attempt 3: a self-signed certificate whose subject names the Bosun's
    # own host, read off the Bosun's own server certificate. The same call
    # that reads the host name also serves as this attempt's reachability
    # recheck.
    bosun_host_name = _require_bosun_reachable("attempt 3")
    key, cert = make_self_signed_machine_cert(machine=bosun_host_name)
    cert_path, key_path = tmp_path / "attempt3.crt", tmp_path / "attempt3.key"
    write_pem_cert(cert_path, cert)
    write_pem_key(key_path, key)
    try:
        status, body = _post_sessions(certfile=cert_path, keyfile=key_path)
        expect(False, f"attempt 3 (self-signed): the server answered "
                      f"{_safe_http_summary(status, body)} instead of refusing the handshake")
    except (ssl.SSLError, OSError) as exc:
        mismatch = _refusal_mismatch(exc, _ATTEMPT_3_ALERTS)
        expect(mismatch is None, f"attempt 3: {mismatch}")
        if mismatch is None:
            print(f"[attempt 3] refused by TLS alert: reason={exc.reason}")
    expect(_session_count() == count_before, "attempt 3: a session row was written")

    # Attempt 4: a certificate valid for another machine, if one is reachable.
    found = _find_another_machine_cert()
    if found is None:
        print(f"[attempt 4] skipped: no operator-readable clientAuth certificate for another "
              f"machine was found under {ANOTHER_MACHINE_CERT_DIR}")
    else:
        _require_bosun_reachable("attempt 4")
        other_cert_path, other_key_path = found
        try:
            status, body = _post_sessions(certfile=other_cert_path, keyfile=other_key_path)
            mismatch = _attempt_4_http_mismatch(status, body)
            expect(mismatch is None, f"attempt 4: {mismatch}")
            if mismatch is None:
                print(f"[attempt 4] refused: HTTP {status}, reason={body.get('refused')!r}")
        except (ssl.SSLError, OSError) as exc:
            mismatch = _attempt_4_exc_mismatch(exc)
            expect(mismatch is None, f"attempt 4: {mismatch}")
            if mismatch is None:
                print(f"[attempt 4] refused by TLS alert: reason={exc.reason}")
        expect(_session_count() == count_before, "attempt 4: a session row was written")

    assert not failed, "\n".join(failed)

    # The positive control: this process's OWN (unsandboxed) call to the
    # daemon's socket, from the operator uid, expected to succeed — proof
    # that a refusal above means "refused", not "the Bosun is down", and a
    # demonstration of the designed behaviour 0092 states explicitly: any
    # process running as the operator account, an agent included, can use
    # this socket to register.
    if os.environ.get("AGENTCTL_LIVE_INDIVIDUATOR_POSITIVE") == "1":
        result = _register_over_socket()
        assert result["connected"], f"positive control: could not even connect: {result['error']}"
        response = result["response"]
        assert response is not None and response.get("ok") is True, (
            f"positive control: the daemon did not register a session: "
            f"{_safe_body_summary(response)} (error={result['error']})")
        session_id = response["session_id"]
        row = _session_row(session_id)
        assert row is not None, (
            f"positive control: the daemon returned session_id={session_id!r} but no "
            f"matching row exists in {SESSIONS_DB}")
        print(f"[positive control] registered session_id len={len(session_id)}, "
              f"custody={row.get('custody')!r}")
    else:
        print("[positive control] SKIPPED (AGENTCTL_LIVE_INDIVIDUATOR_POSITIVE unset) — "
              "the refusals above are not proven real; a Bosun that is down or wedged "
              "could have produced every one of them")
