"""The Bosun's session registration over mutual TLS (platform/decisions/0092;
E12·5, and the registration half of E12·8).

**Stage one is a policy and audit layer. It is never containment or
isolation** (platform/decisions/0057) — nothing here stops a process that
already has command execution on a registered machine; it decides which
machine may register a session and records that decision.

The Individuator authenticates by presenting its Library-issued machine
certificate over TLS 1.3 with a client certificate required. This module
holds the server side of that exchange: the TLS context the listener binds
with, the three checks 0092 decision #1 makes on the presented certificate,
the one-shot registry lookup against the Library (library-protocol §2.3) that
decides whether registration may proceed, the session record it creates, the
helper the (not-yet-built) per-call endpoint will use to bind a session token
to the certificate that registered it, and revocation by machine name.

Every function here is a pure, directly callable unit — the HTTP/TLS sockets
in `RegistrationServer`/`OperatorServer` are thin IO glue around them, in the
repo's existing convention for socket servers (see `bin/session-view`,
`tests/test_session_view.py`): the sockets are not themselves the thing under
test.

The Library's answer is read fresh on every registration and never cached
(0092 decision #4): checking it on every later call would make every tool
call on every machine depend on the Library being reachable. So a session,
once registered, is bound to its certificate's fingerprint
(`check_session_binding`) rather than re-verified against the registry, and a
machine revoked in the Library keeps its live sessions until an operator runs
`agentctl bosun revoke-machine <name>` or the machine's certificate is
renewed. 0092 names this cost explicitly and accepts it.

THE PER-CALL AUTHORIZATION ENDPOINT (E12·3's endpoint half; E12·8's per-call
half) lives on this same mTLS listener, at `POST /authorize` — never a second
listener, because the binding this module already enforces for registration
is exactly the binding a per-call request needs too. `handle_authorization`
calls `check_session_binding` first and refuses on any binding failure with
its named reason before `bosun_authz.decide()` ever runs. Only once binding
holds does it build a `bosun_authz.SessionGrant` from the session row — its
`ceiling` deserialized through `bosun_authz.ceiling_entries_from_text`, the
vocabulary that module defines for the opaque TEXT column below — and call
`decide()`. **A call that executes (a `BoundaryGrant`) is allow/deny on its
own terms and is never checked against the ceiling** (platform/decisions/0057
"Two authorization shapes"; E12·8): the ceiling binds only a request that
names its target. Every call that reaches `decide()` writes one row to the
`authorization_audit` table in this module's own `SESSIONS_DB` — session id,
machine, the call's target, allow or deny, reason, and time — beside the
`sessions` table rather than in a second store.

**`decide()`'s check on the agent's own permission set is NOT WIRED.**
`handle_authorization` calls `decide()` with a constant, always-empty
`PermissionSet` (`_PERMISSION_SET_NOT_WIRED`, defined beside it) because no
agent identity reaches the Bosun under platform/decisions/0092 — see that
constant's comment. Do not deploy or rely on `/authorize` to enforce a tool
the Library has denied an agent until that wiring lands.
"""
from __future__ import annotations

import hashlib
import http.server
import json
import os
import re
import secrets
import socket
import socketserver
import sqlite3
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID

import bosun_authz as AU
import builder_root as BR
import vault_plane  # noqa: F401 — imported for its urlopen-hardening side effect

# --- configuration -------------------------------------------------------
#
# Every path below is read as a module attribute, not re-read from the
# environment per call, so a test overrides it with `monkeypatch.setattr`
# the same way tests/test_kb_writes.py repoints `kb_writes.DB` — see that
# file's `store` fixture for the pattern this module's tests copy.

# The Library's root certificate. THE SAME FILE and THE SAME env var
# `deck_signature.ROOT_CA_PATH` reads — it is the identical trust anchor, and
# an operator who relocates it need only set the one variable for every
# reader of it on this machine.
TRUST_ANCHOR_PATH = Path(os.environ.get(
    "LIBRARY_TLS_CA", str(Path.home() / ".library" / "tls" / "ca.crt")))

# The macstudio server certificate and key (0092 decision #6). No default path:
# the certificate does not exist until the Library's ceremony with the operator issues
# it, and a guessed default would look configured when nothing has been
# deployed there. `build_server_ssl_context` refuses to start without both.
SERVER_CERT_PATH = os.environ.get("BOSUN_TLS_CERT") or None
SERVER_KEY_PATH = os.environ.get("BOSUN_TLS_KEY") or None

# The Library's plain loopback listener, where the `bosun-service` bearer
# token is presented (0092 decision #5; library-protocol §2.3). Configurable
# so a test points it at a local stub rather than the real address — and
# distinct from `vault_plane`'s address, which answers "where do the VAULTS
# live" and is the wrong question for a machine-registry lookup that only
# ever lives on the Library.
LIBRARY_LOOKUP_BASE_URL = os.environ.get("BOSUN_LIBRARY_BASE_URL", "http://127.0.0.1:8100")

# The lookup timeout is security-relevant configuration, not a literal
# (0092 Handoff) — too long and a wedged Library hangs every registration on
# the machine attempting one; too short and a slow-but-live Library reads as
# unreachable.
LIBRARY_LOOKUP_TIMEOUT_S = float(os.environ.get("BOSUN_LIBRARY_LOOKUP_TIMEOUT_S", "5"))

# The `bosun-service` token, read fresh on every lookup and never cached
# (0092 Handoff). The home is the PASSWD home of the account the Bosun runs
# as (`builder_root.real_account_home`), never `Path.home()` / `$HOME` —
# 0092 is explicit that the Bosun must never read this from another
# account's home, and `$HOME` is exactly the value an invoking process gets
# to set.
LOOKUP_TOKEN_PATH = Path(os.environ.get(
    "BOSUN_LIBRARY_VERIFY_TOKEN_PATH",
    str(BR.real_account_home() / ".config" / "bosun" / "library-machine-verify.token")))

# The session table. Its own SQLite file under the Bosun's state directory,
# `<data root>/bosun/sessions.db` — the same `<data root>/<name>` shape
# `builder_root.runs_root()` already uses for `agent-runs`, so this is a
# second instance of an established pattern rather than a new one. SQLite
# over a flat file because sessions are looked up by token and by machine
# name from concurrent requests, and `kb_writes.py`'s lease/journal store is
# this repo's own precedent for "small, concurrently-written, operational
# state gets its own SQLite file" (see that module's docstring).
SESSIONS_DB = Path(os.environ.get(
    "BOSUN_SESSIONS_DB", str(BR.data_root() / "bosun" / "sessions.db")))

# The operator-only revoke-by-machine route. Loopback only, by address alone
# — the same property ADR-0057 relies on for the mini's existing control
# socket ("mode 0600 refuses it at connect(), before any code runs"); binding
# to 127.0.0.1 gives the equivalent property for a TCP listener. It can only
# deny sessions, never widen one, which is why 0092's Handoff asks for no
# further authorization on top.
OPERATOR_HOST = "127.0.0.1"
OPERATOR_PORT = int(os.environ.get("BOSUN_OPERATOR_PORT", "8711"))

# The mTLS registration listener. Individuators on other machines reach this
# one, so unlike the operator listener above it is not loopback-only.
REGISTRATION_HOST = os.environ.get("BOSUN_REGISTRATION_HOST", "0.0.0.0")
REGISTRATION_PORT = int(os.environ.get("BOSUN_REGISTRATION_PORT", "8710"))

# The per-connection TLS handshake timeout, security-relevant configuration
# for the same reason the lookup timeout above is: REGISTRATION_HOST defaults
# to 0.0.0.0, reachable from any machine before a certificate is even
# presented, so a peer that opens a connection and sends no ClientHello must
# not be able to hold it open indefinitely. Bounding the handshake (below)
# is what makes that true; this is only how long it may take.
TLS_HANDSHAKE_TIMEOUT_S = float(os.environ.get("BOSUN_TLS_HANDSHAKE_TIMEOUT_S", "10"))

# The per-connection request timeout, security-relevant configuration for the
# same reason the handshake timeout above is: once the handshake succeeds,
# the request line, headers and body are read with no bound of their own
# unless one is applied here — round 2 of this PR's review found that a peer
# presenting any valid machine certificate could complete the handshake and
# then hold its thread forever by sending nothing, or a `Content-Length`
# header followed by a trickle. This is a wall-clock deadline for the whole
# request, not a constant re-applied on every read (`_RequestDeadlineSSLSocket`
# below) — a constant per-call timeout bounds only a single blocking read, and
# a peer that sends one byte just inside that window on every call never
# trips it.
TLS_REQUEST_TIMEOUT_S = float(os.environ.get("BOSUN_TLS_REQUEST_TIMEOUT_S", "30"))

# The response-write timeout, security-relevant for the same reason as the
# two above: round 4 of this PR's review found that `_RequestDeadlineSSLSocket`
# bounded reads only, so the write that sends the response ran under whatever
# timeout the last read happened to leave on the socket. When the request
# body arrived late in the request deadline's window, that leftover was
# almost nothing, and the write could time out after `register_session` had
# already committed the session row — the client would never receive its
# `session_token` for a registration that had, in fact, succeeded. The
# response write needs a bound of its own, independent of how much of the
# read deadline is left (`_RequestDeadlineSSLSocket.send` below).
TLS_RESPONSE_TIMEOUT_S = float(os.environ.get("BOSUN_TLS_RESPONSE_TIMEOUT_S", "10"))


# --- TLS preflight (0092 decision #1; Handoff) ----------------------------


class PreflightError(RuntimeError):
    """A named reason the Bosun's TLS listener refuses to start.

    Raised rather than letting `ssl`/`socket` fail with whatever exception
    they happen to raise, so a missing trust anchor and a missing server key
    are told apart in the one place that matters: before the listener binds
    at all.
    """


def build_server_ssl_context(*, trust_anchor_path: Path | None = None,
                              server_cert_path: str | None = None,
                              server_key_path: str | None = None) -> ssl.SSLContext:
    """TLS 1.3 only, a client certificate required, the Library's root as the
    only trust anchor, no early data (0092 decision #1).

    Arguments default to this module's own configuration; a caller passes
    its own only to test one precondition at a time.

    **CPython's `ssl` module exposes no 0-RTT / early-data interface at
    all** — there is no flag this sets to turn early data off, because
    nothing in the stdlib stack offers to turn it on. That is 0092's own
    stated guess ("the Bosun's TLS stack is CPython's `ssl`, which exposes
    no 0-RTT interface") and this context's "no early data" property rests
    on it rather than on a setting below. If a future stack gains such an
    interface, the guess is wrong and this must gain an explicit refusal.
    """
    trust_anchor_path = TRUST_ANCHOR_PATH if trust_anchor_path is None else trust_anchor_path
    server_cert_path = SERVER_CERT_PATH if server_cert_path is None else server_cert_path
    server_key_path = SERVER_KEY_PATH if server_key_path is None else server_key_path

    if not server_cert_path or not server_key_path:
        raise PreflightError(
            "no server certificate configured (BOSUN_TLS_CERT / BOSUN_TLS_KEY) — "
            "the macstudio certificate is not issued until the Library's ceremony with "
            "the operator (0092 decision #6)")
    if not trust_anchor_path.is_file():
        raise PreflightError(
            f"trust anchor {trust_anchor_path} is missing or not a regular file")
    if not Path(server_cert_path).is_file():
        raise PreflightError(
            f"server certificate {server_cert_path} is missing or not a regular file")
    if not Path(server_key_path).is_file():
        raise PreflightError(
            f"server key {server_key_path} is missing or not a regular file")
    if not ssl.HAS_TLSv1_3:
        raise PreflightError("this interpreter's ssl module has no TLS 1.3 support")

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.maximum_version = ssl.TLSVersion.TLSv1_3
    ctx.verify_mode = ssl.CERT_REQUIRED
    try:
        ctx.load_verify_locations(cafile=str(trust_anchor_path))
    except ssl.SSLError as exc:
        raise PreflightError(
            f"trust anchor {trust_anchor_path} could not be loaded: {exc}") from exc
    try:
        ctx.load_cert_chain(certfile=str(server_cert_path), keyfile=str(server_key_path))
    except (ssl.SSLError, OSError) as exc:
        raise PreflightError(
            f"server certificate/key could not be loaded: {exc}") from exc
    return ctx


# --- client-certificate checks (0092 decision #1) -------------------------

_SAN_URI_RE = re.compile(r"^convivy://machine/([a-z0-9][a-z0-9-]{0,63})$")


@dataclass(frozen=True)
class CertCheck:
    """`machine` is set only when `reason` is None."""
    machine: str | None
    reason: str | None


def fingerprint_sha256_der(der: bytes) -> str:
    """SHA-256 of a certificate's DER bytes, 64 lowercase hex characters —
    the registry's key (library-protocol §2.3, computed the way §2.1 does)."""
    return hashlib.sha256(der).hexdigest()


def check_client_certificate(der: bytes) -> CertCheck:
    """0092 decision #1's three checks, in order, on a DER-encoded leaf
    certificate.

    Chain validity against the Library's root is NOT checked here — it is
    enforced by the TLS handshake itself (`CERT_REQUIRED` plus the trust
    anchor loaded in `build_server_ssl_context`), so a certificate reaching
    this function already chains to the root. This function makes the checks
    the handshake does not: the EKU, and the SAN's cardinality and shape.
    The common name and every header are never read for identity — there is
    simply no code path here that reads either.
    """
    try:
        cert = x509.load_der_x509_certificate(der)
    except ValueError:
        return CertCheck(None, "bad_certificate")

    try:
        eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    except x509.ExtensionNotFound:
        return CertCheck(None, "eku_missing_clientauth")
    if ExtendedKeyUsageOID.CLIENT_AUTH not in eku:
        return CertCheck(None, "eku_missing_clientauth")

    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return CertCheck(None, "san_not_exactly_one")
    uris = [n.value for n in san if isinstance(n, x509.UniformResourceIdentifier)]
    if len(uris) != 1:
        return CertCheck(None, "san_not_exactly_one")
    match = _SAN_URI_RE.match(uris[0])
    if not match:
        return CertCheck(None, "san_not_exactly_one")
    return CertCheck(match.group(1), None)


# --- the registry lookup (0092 decision #4; library-protocol §2.3) -------

_LIBRARY_STATUSES = frozenset({"active", "revoked", "expired", "unknown"})


@dataclass(frozen=True)
class LookupResult:
    ok: bool
    reason: str | None = None
    status: str | None = None
    machine: str | None = None


def read_lookup_token(path: Path | None = None) -> str | None:
    """The `bosun-service` bearer token, read fresh — never cached (0092
    decision #4). None when the file is absent or unreadable, which the
    caller must turn into `lookup_credential_missing` *before* sending any
    request to the Library."""
    p = LOOKUP_TOKEN_PATH if path is None else path
    try:
        text = p.read_text()
    except OSError:
        return None
    text = text.strip()
    return text or None


def library_machine_verify(fingerprint_sha256: str, expected_machine: str, *,
                            base_url: str | None = None, token: str,
                            timeout: float | None = None) -> LookupResult:
    """`GET /machine-certificates/{fingerprint_sha256}` on the Library
    (library-protocol §2.3), with the 0092 Handoff's exact refusal taxonomy.

    Accepts only HTTP 200 with `active` exactly the JSON boolean `true` and
    `machine` equal to `expected_machine`. Every other shape refuses with one
    of 0092's named reasons; `not_after` and `custody` are never read here —
    they are recorded by the caller and never decide (0092 Handoff).
    """
    base_url = LIBRARY_LOOKUP_BASE_URL if base_url is None else base_url
    timeout = LIBRARY_LOOKUP_TIMEOUT_S if timeout is None else timeout
    req = urllib.request.Request(
        f"{base_url}/machine-certificates/{fingerprint_sha256}",
        headers={"Authorization": f"Bearer {token}", "Library-Protocol": "1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return LookupResult(False, "library_route_missing")
        return LookupResult(False, "library_error")
    except (urllib.error.URLError, OSError, TimeoutError):
        return LookupResult(False, "library_unreachable")

    try:
        body = json.loads(raw)
    except ValueError:
        return LookupResult(False, "library_malformed")
    if not isinstance(body, dict):
        return LookupResult(False, "library_malformed")

    status = body.get("status")
    if status not in _LIBRARY_STATUSES:
        return LookupResult(False, "library_malformed")
    if status == "unknown":
        # An unknown answer names no machine (library-protocol §2.3's
        # unknown body carries no `machine` key) and revokes nothing — the
        # caller must not substitute the certificate's claimed name here.
        return LookupResult(False, "machine_unknown")
    if status in ("revoked", "expired"):
        machine = body.get("machine")
        return LookupResult(False, "machine_inactive", status=status,
                            machine=machine if isinstance(machine, str) else None)

    # status == "active"
    if body.get("active") is not True:
        return LookupResult(False, "library_malformed")
    machine = body.get("machine")
    if machine != expected_machine:
        return LookupResult(False, "machine_mismatch",
                            machine=machine if isinstance(machine, str) else None)
    return LookupResult(True, status="active", machine=machine)


# --- session store (SQLite; 0092 decision #2) -----------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id                  TEXT PRIMARY KEY,
    token_sha256        TEXT NOT NULL UNIQUE,
    machine             TEXT NOT NULL,
    fingerprint_sha256  TEXT NOT NULL,
    account             TEXT NOT NULL,
    caller_kind         TEXT NOT NULL CHECK (caller_kind IN ('operator', 'agent')),
    ceiling             TEXT NOT NULL,
    -- Unused since platform/decisions/0057's 2026-10-03 ruling (the Bosun is
    -- strict only). Kept rather than dropped: sessions.db files already on
    -- disk declare it NOT NULL with no default, and CREATE TABLE IF NOT EXISTS
    -- never alters them, so an INSERT that omitted it would fail there. Every
    -- row is written 'strict'; nothing reads it.
    availability_lever  TEXT NOT NULL
        CHECK (availability_lever IN ('strict', 'cached_grants', 'degraded_offline')),
    protocol_version    TEXT NOT NULL,
    registered_at       TEXT NOT NULL,
    revoked_at          TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_machine ON sessions(machine);
CREATE INDEX IF NOT EXISTS idx_sessions_token   ON sessions(token_sha256);

CREATE TABLE IF NOT EXISTS authorization_audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    machine     TEXT NOT NULL,
    tool_name   TEXT NOT NULL,
    object_ref  TEXT,
    allow       INTEGER NOT NULL CHECK (allow IN (0, 1)),
    reason      TEXT NOT NULL,
    at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_authz_audit_session ON authorization_audit(session_id);
"""

_db_ensured = False


def ensure_db() -> None:
    """Create the schema if it is not already there. Idempotent
    (`CREATE ... IF NOT EXISTS`), but a full `executescript` + `commit` all
    the same — callers on the hot path use `ensure_db_once` instead."""
    SESSIONS_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(SESSIONS_DB))
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def ensure_db_once() -> None:
    """`ensure_db`, but only the first time this process calls it — the
    listeners call it once at startup (`RegistrationServer.__init__`,
    `OperatorServer.__init__`), and `_connect` below calls it too so a
    caller that touches the store without going through either listener
    (an agentctl subcommand added later, a test) still gets the schema
    lazily on first use, without paying `executescript` + `commit` on
    every single register/revoke/binding-check call the way `_connect`
    used to by calling `ensure_db` directly."""
    global _db_ensured
    if not _db_ensured:
        ensure_db()
        _db_ensured = True


def _connect() -> sqlite3.Connection:
    ensure_db_once()
    conn = sqlite3.connect(str(SESSIONS_DB))
    conn.row_factory = sqlite3.Row
    return conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def register_session(*, machine: str, fingerprint_sha256: str, account: str,
                      caller_kind: str, ceiling: str, protocol_version: str) -> dict:
    """Insert a new session row and mint its token.

    Only `token_sha256` is stored — never the plaintext — the same choice
    this platform already makes for every other bearer credential it stores
    (`deck_signature`, `kb_writes`'s lease holder tokens are the exception
    that proves the rule: they are not secrets). The plaintext is returned
    once, here, and nowhere else.

    Returns `{"session_id", "session_token"}`.
    """
    session_id = secrets.token_hex(16)
    token = secrets.token_urlsafe(32)
    conn = _connect()
    try:
        # `availability_lever` is NOT NULL on existing sessions.db files (see
        # `_SCHEMA`), so the literal 'strict' is written here; no caller sends a value.
        conn.execute(
            "INSERT INTO sessions (id, token_sha256, machine, fingerprint_sha256, "
            "account, caller_kind, ceiling, availability_lever, protocol_version, "
            "registered_at, revoked_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'strict', ?, ?, NULL)",
            (session_id, _token_sha256(token), machine, fingerprint_sha256, account,
             caller_kind, ceiling, protocol_version, _now_iso()))
        conn.commit()
    finally:
        conn.close()
    return {"session_id": session_id, "session_token": token}


def revoke_sessions_for_machine(machine: str) -> int:
    """End every live session registered for `machine`. Returns how many
    were ended — the count `agentctl bosun revoke-machine` prints, and the
    count a registration lookup's own revocation (below) relies on too."""
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE sessions SET revoked_at = ? WHERE machine = ? AND revoked_at IS NULL",
            (_now_iso(), machine))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def check_session_binding(token: str, presented_fingerprint: str) -> tuple[dict | None, str | None]:
    """The helper the later per-call authorization endpoint (E12·8's sibling
    slice) will use: a live session for `token`, only when
    `presented_fingerprint` — the fingerprint of the certificate on the
    connection presenting it — equals the one that registered it (0092
    decision #2). No registry lookup; the comparison needs none.

    Returns `(session, None)` on success, else `(None, reason)`:
    `session_unknown` for a token this table has never seen or has already
    revoked, `session_machine_mismatch` for a live session presented over a
    different certificate.
    """
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM sessions WHERE token_sha256 = ? AND revoked_at IS NULL",
            (_token_sha256(token),)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None, "session_unknown"
    session = dict(row)
    if session["fingerprint_sha256"] != presented_fingerprint:
        return None, "session_machine_mismatch"
    return session, None


# --- the per-call authorization audit (E12·3/E12·8 clause 5) --------------


def record_authorization_decision(*, session_id: str, machine: str, tool_name: str,
                                    object_ref: str | None, decision: "AU.Decision") -> None:
    """One row per call that reaches `bosun_authz.decide()` — session id,
    machine, the call's target, allow or deny, reason, time — in this
    module's own `SESSIONS_DB`, beside the `sessions` table rather than in a
    second store.

    `object_ref` is the caller's own job to compute (round-2 reviewer
    finding): `decision.authorization.object_ref` is only set on the ALLOW
    branch of an object-scoped request, because `decide()` denies before
    ever constructing an `ObjectScopedAuthorization`, so a denied
    named-target call's audit row used to carry no object at all even
    though the request named one. This function records whatever
    `object_ref` it is given, without re-deriving it from `decision` —
    `handle_authorization` derives it once, independent of allow/deny, via
    the same classifier `decide()` uses."""
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO authorization_audit (session_id, machine, tool_name, object_ref, "
            "allow, reason, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, machine, tool_name, object_ref,
             1 if decision.allow else 0, decision.reason, _now_iso()))
        conn.commit()
    finally:
        conn.close()


def audit_records_for_session(session_id: str) -> list[dict]:
    """Every audit row for one session, oldest first — a test/operator read,
    never consulted by `handle_authorization` itself."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM authorization_audit WHERE session_id = ? ORDER BY id",
            (session_id,)).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


# --- registration orchestration -------------------------------------------

_REQUIRED_BODY_KEYS = frozenset({"account", "caller_kind", "ceiling", "protocol_version"})
_CALLER_KINDS = frozenset({"operator", "agent"})


@dataclass(frozen=True)
class RegistrationOutcome:
    ok: bool
    reason: str | None = None
    session_id: str | None = None
    session_token: str | None = None
    detail: dict = field(default_factory=dict)


def handle_registration(peer_cert_der: bytes, body: object) -> RegistrationOutcome:
    """The full registration flow: the client-certificate checks (0092
    decision #1), the registry lookup (0092 decision #4), and — only once
    both pass — the session record and its token (0092 decision #2).

    The machine comes from the handshake. A `machine` key in `body` is
    ignored and logged, never read as identity (0092 decision #2).
    """
    check = check_client_certificate(peer_cert_der)
    if check.reason:
        return RegistrationOutcome(False, check.reason)
    machine = check.machine

    if not isinstance(body, dict) or not _REQUIRED_BODY_KEYS <= set(body):
        return RegistrationOutcome(False, "malformed_body")
    if "machine" in body:
        print(f"bosun_registration: ignoring machine={body['machine']!r} in the "
              f"registration body for {machine!r} — the machine comes from the "
              f"handshake (0092 decision #2)", file=sys.stderr)

    account = body.get("account")
    caller_kind = body.get("caller_kind")
    ceiling = body.get("ceiling")
    protocol_version = body.get("protocol_version")
    if (not isinstance(account, str) or not account
            or caller_kind not in _CALLER_KINDS
            or not isinstance(ceiling, str) or not ceiling
            or not isinstance(protocol_version, str) or not protocol_version):
        return RegistrationOutcome(False, "malformed_body")
    try:
        AU.ceiling_entries_from_text(ceiling)
    except ValueError:
        return RegistrationOutcome(False, "malformed_ceiling")

    token = read_lookup_token()
    if token is None:
        return RegistrationOutcome(False, "lookup_credential_missing")

    fingerprint = fingerprint_sha256_der(peer_cert_der)
    result = library_machine_verify(fingerprint, machine, token=token)

    if result.reason == "machine_inactive" and result.machine:
        revoke_sessions_for_machine(result.machine)

    if not result.ok:
        detail = {"status": result.status} if result.status else {}
        return RegistrationOutcome(False, result.reason, detail=detail)

    created = register_session(
        machine=machine, fingerprint_sha256=fingerprint, account=account,
        caller_kind=caller_kind, ceiling=ceiling, protocol_version=protocol_version)
    return RegistrationOutcome(True, session_id=created["session_id"],
                               session_token=created["session_token"])


# --- the per-call authorization endpoint ----------------------------------
# (E12·3's endpoint half; E12·8's per-call half)

_AUTHORIZE_REQUIRED_KEYS = frozenset({"session_token", "tool_name"})

# THE LIBRARY'S PER-AGENT TOOL POLICY IS NOT CONSULTED BY /authorize. No agent
# identity reaches the Bosun under platform/decisions/0092: the registration
# body carries `account`, `caller_kind`, `ceiling` and `protocol_version`
# (0092 Handoff "Registration body"), and the session record it writes
# carries the same set plus the certificate fingerprint (0092 Handoff
# "Session record") — nothing names the agent on either side. So
# `bosun_authz.decide()`'s check 2 ("a tool the Library already denied this
# agent is denied here too, regardless of class or ceiling") can never fire
# through this endpoint: it is always evaluated against this empty, constant
# set. Wiring it needs a protocol decision 0092 does not make — how an
# agent's identity reaches the Bosun, at registration or per call — and
# until that lands, `bosun_authz.permissions_from_policy_record` (the
# function built for this) is unused here on purpose.
#
# DO NOT DEPLOY OR RELY ON THIS ENDPOINT FOR PERMISSION-SET ENFORCEMENT
# until that wiring exists. Nothing calls it yet (no Individuator, no
# plist, no server certificate — 0092 decision #6), so today this is a
# recorded gap rather than a live exposure; see
# `test_handle_authorization_allows_a_denied_tool__GAP_no_permission_set_wiring`
# and the PR body's "Not yet wired" section.
_PERMISSION_SET_NOT_WIRED = AU.PermissionSet()


@dataclass(frozen=True)
class AuthorizationOutcome:
    """The per-call authorization flow's result.

    `refused` is set when the call is refused before `bosun_authz.decide()`
    ever runs: a malformed request, or a session-binding failure
    (`check_session_binding`'s `session_unknown` / `session_machine_mismatch`
    — which also covers a revoked session, since that function's query
    excludes one). `decision` is set only once `decide()` has actually run,
    and is its whole answer — allow or deny, reason, and scoped
    authorization. The two are mutually exclusive."""
    refused: str | None = None
    decision: "AU.Decision | None" = None


def _object_ref_for_audit(tool_name: str, tool_input: dict) -> str | None:
    """The object the request named, for the audit row, independent of
    whether `decide()` allows or denies it (round-2 reviewer finding):
    `decide()` only attaches `object_ref` to the `ObjectScopedAuthorization`
    it builds on its allow branch, so a denied named-target call's audit row
    used to carry no object even though the request named one. Uses the
    same classifier and extractor `decide()` itself uses, rather than
    changing `decide()`'s return type, so #751's already-passed `decide()`
    contract stays untouched. An executing (boundary-scoped) call keeps no
    object — it names none (platform/decisions/0057 "two authorization
    shapes") — and a path this module cannot judge lexically (a relative
    path, or one with a `..` segment) also keeps none, the same as
    `decide()` itself denies it without ever extracting one.
    """
    if AU.classify(tool_name, tool_input) != AU.NAMES_TARGET:
        return None
    try:
        return AU.extract_object(tool_name, tool_input)
    except ValueError:
        return None


def handle_authorization(peer_cert_der: bytes | None, body: object, *,
                          now: datetime | None = None) -> AuthorizationOutcome:
    """The full per-call authorization flow, on the same mTLS listener
    registration uses (never a second one): strict validation of the
    request shape, the session-binding check (0092 decision #2), and only
    once both pass, `decide()` itself with the session's declared ceiling —
    with one audit row for every call that reaches it. Both the shape
    check and the binding check refuse before `decide()` ever runs.

    `body` must be exactly `{"session_token": str, "tool_name": str}` plus
    an optional `"input"` (a dict, defaulting to `{}` when absent) — the
    shape `bosun_authz.decide()`'s `request` argument takes, with the token
    alongside it. Anything else is `malformed_request`.

    **`decide()` is called with an always-empty permission set
    (`_PERMISSION_SET_NOT_WIRED`).** No agent identity reaches the Bosun
    under platform/decisions/0092, so the Library's per-agent tool policy is
    never consulted here — see that constant's comment. Do not deploy or
    rely on this endpoint to enforce a denied tool until that wiring lands.
    """
    if not isinstance(body, dict) or not _AUTHORIZE_REQUIRED_KEYS <= set(body):
        return AuthorizationOutcome(refused="malformed_request")
    token = body.get("session_token")
    tool_name = body.get("tool_name")
    tool_input = body.get("input", {})
    if tool_input is None:
        tool_input = {}
    if (not isinstance(token, str) or not token
            or not isinstance(tool_name, str) or not tool_name
            or not isinstance(tool_input, dict)):
        return AuthorizationOutcome(refused="malformed_request")

    if not peer_cert_der:
        return AuthorizationOutcome(refused="bad_certificate")
    fingerprint = fingerprint_sha256_der(peer_cert_der)
    session, reason = check_session_binding(token, fingerprint)
    if session is None:
        return AuthorizationOutcome(refused=reason)

    try:
        ceiling = AU.ceiling_entries_from_text(session["ceiling"])
    except ValueError:
        # A row this module itself wrote is now unreadable by the vocabulary
        # it defines — a stored-data fault, not the caller's request, since
        # `handle_registration` already refuses a ceiling that fails this
        # same parse before it is ever stored.
        return AuthorizationOutcome(refused="ceiling_unreadable")

    grant = AU.SessionGrant(session_id=session["id"], ceiling=ceiling,
                            revoked_at=None)  # check_session_binding only returns a live row
    decision = AU.decide({"tool_name": tool_name, "input": tool_input}, grant,
                         _PERMISSION_SET_NOT_WIRED, now or datetime.now(timezone.utc))
    object_ref = _object_ref_for_audit(tool_name, tool_input)
    record_authorization_decision(session_id=session["id"], machine=session["machine"],
                                  tool_name=tool_name, object_ref=object_ref, decision=decision)
    return AuthorizationOutcome(decision=decision)


def _authorization_to_dict(authorization) -> dict | None:
    if isinstance(authorization, AU.ObjectScopedAuthorization):
        return {"kind": "object", "object_ref": authorization.object_ref}
    if isinstance(authorization, AU.BoundaryGrant):
        return {"kind": "boundary", "tool_name": authorization.tool_name}
    return None


# --- HTTP/TLS plumbing — thin IO glue, not itself the thing under test ----

# HTTP statuses for each refusal reason. 0092/library-protocol pin 401 for
# the client-certificate shape checks; everything else is this module's own
# choice: 400 for a malformed request body, 503 for a Library-side failure
# the machine cannot fix by retrying with different credentials, and 403 for
# a Library answer that is clear and negative.
_REFUSAL_HTTP_STATUS = {
    "bad_certificate": 401,
    "eku_missing_clientauth": 401,
    "san_not_exactly_one": 401,
    "malformed_body": 400,
    "malformed_ceiling": 400,
    "lookup_credential_missing": 503,
    "library_route_missing": 503,
    "library_error": 503,
    "library_unreachable": 503,
    "library_malformed": 503,
    "machine_unknown": 403,
    "machine_inactive": 403,
    "machine_mismatch": 403,
}

# HTTP statuses for `/authorize`'s pre-`decide()` refusals. 401 for a missing
# or mismatched session binding (the same status a missing client
# certificate gets above, since both are "this caller is not who it must be
# to proceed"); 400 for a request this endpoint cannot parse; 500 for
# `ceiling_unreadable`, a stored-data fault rather than anything the caller
# sent. `decide()`'s own allow/deny is never a refusal — it is always 200,
# carried in the JSON body's `allow` field, because asking and getting an
# answer is success even when the answer is deny.
_AUTHORIZE_REFUSAL_HTTP_STATUS = {
    "malformed_request": 400,
    "bad_certificate": 401,
    "session_unknown": 401,
    "session_machine_mismatch": 401,
    "ceiling_unreadable": 500,
}


def _write_json(handler: http.server.BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    data = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


class _RegistrationHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A002 — stdlib signature
        pass  # a dev-only server; nothing here is a deployed daemon yet

    def do_POST(self):
        # Every path below returns after exactly one request; keep-alive
        # would otherwise let a benign client (or a dishonest one) hold its
        # thread open indefinitely past the response, since nothing else
        # bounds an idle keep-alive read (round 3 finding). This applies to
        # /authorize the same as /sessions — both run on this one listener.
        self.close_connection = True
        if self.path == "/sessions":
            self._do_sessions()
        elif self.path == "/authorize":
            self._do_authorize()
        else:
            self.send_response(404)
            self.end_headers()

    def _read_json_body(self):
        """Returns the parsed body, or `None` for an empty or unparseable
        one — `None` is itself a valid JSON value, so both handlers below
        treat it the same as unparseable: neither is the dict they require."""
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def _do_sessions(self):
        der = self.connection.getpeercert(binary_form=True)
        if not der:
            _write_json(self, 401, {"refused": "bad_certificate"})
            return
        body = self._read_json_body()
        outcome = handle_registration(der, body)
        if outcome.ok:
            _write_json(self, 200, {"session_id": outcome.session_id,
                                     "session_token": outcome.session_token})
        else:
            status = _REFUSAL_HTTP_STATUS.get(outcome.reason, 403)
            _write_json(self, status, {"refused": outcome.reason, **outcome.detail})

    def _do_authorize(self):
        der = self.connection.getpeercert(binary_form=True)
        body = self._read_json_body()
        outcome = handle_authorization(der, body)
        if outcome.refused:
            status = _AUTHORIZE_REFUSAL_HTTP_STATUS.get(outcome.refused, 401)
            _write_json(self, status, {"refused": outcome.refused})
            return
        decision = outcome.decision
        _write_json(self, 200, {"allow": decision.allow, "reason": decision.reason,
                                 "authorization": _authorization_to_dict(decision.authorization)})


class _RequestDeadlineSSLSocket(ssl.SSLSocket):
    """An `ssl.SSLSocket` whose reads respect a wall-clock deadline once
    `arm_request_deadline` sets one, and whose writes respect a separate,
    independent timeout once `arm_response_timeout` sets one, rather than
    both sharing whatever `settimeout` left behind.

    `settimeout` only bounds a single blocking call: a peer that sends one
    byte of the request just inside that window on every call never trips
    it, and can hold the thread for as long as it likes. Overriding
    `recv_into` — the one call every buffered read on `rfile` goes through
    (`socket.makefile`'s `SocketIO.readinto`), so this covers the request
    line, every header line and `rfile.read(length)` for the body alike —
    to recompute the time left before each individual read, and shrink that
    read's own timeout to match, turns the bound into a deadline for the
    whole request instead of a constant reapplied on every call.

    The response write must not inherit that same shrinking value: once the
    request has been read, the time left on the read deadline can be almost
    nothing, and a write governed by it can time out after the handler has
    already committed its side effect (round 4 finding). Overriding `send`
    — every buffered write on `wfile` goes through it (`socket.makefile`'s
    `SocketIO.write` calls `self._sock.send`, and `ssl.SSLSocket.sendall`
    itself loops over `self.send`), covering `send_response`/`send_header`/
    `end_headers` and the body write alike — gives the response its own
    bound, independent of the read deadline.

    That bound must itself be a deadline, not a value reapplied on every
    call: `sendall` loops over `send` once per partial write, so a peer that
    reads the response a few bytes at a time, each read landing just inside
    a fixed per-call `settimeout`, resets that timeout in full on every
    partial send and is never bound at all (round 5 finding — the same
    failure `recv_into`'s deadline tracking above exists to prevent,
    reintroduced on the write side). `send` anchors `self._response_deadline`
    from `time.monotonic()` plus `self._response_timeout_s` on its own first
    call for this connection, then recomputes the time left before every
    later call, exactly as `recv_into` does for `self._deadline` — so the
    whole response, however many partial writes it takes, is bound by one
    deadline of `TLS_RESPONSE_TIMEOUT_S` from the first byte sent.

    Anchoring on `send`'s own first call, rather than when
    `arm_response_timeout` is called, is what makes that one deadline cover
    every response path on this connection: the normal reply, a refusal,
    and `http.server`'s own `send_error` for a malformed request line, which
    runs inside `parse_request()` before `do_POST` is ever reached and so
    before any response has been sent. `arm_response_timeout` only records
    the configured duration; the first `send()` call on this connection,
    whichever response path makes it, is what starts the clock.

    Does not affect `do_handshake()`: that runs before either bound is
    armed, and in CPython reads and writes the raw file descriptor directly
    rather than going through this class's Python-level `recv_into`/`send`,
    so it stays governed by `settimeout` alone, exactly as before this
    class existed.
    """
    _deadline: float | None = None
    _response_timeout_s: float | None = None
    _response_deadline: float | None = None

    def arm_request_deadline(self, timeout_s: float) -> None:
        self._deadline = time.monotonic() + timeout_s

    def arm_response_timeout(self, timeout_s: float) -> None:
        self._response_timeout_s = timeout_s

    def recv_into(self, *args, **kwargs):
        if self._deadline is not None:
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout("bosun registration: request deadline exceeded")
            self.settimeout(remaining)
        return super().recv_into(*args, **kwargs)

    def send(self, *args, **kwargs):
        if self._response_timeout_s is not None:
            if self._response_deadline is None:
                self._response_deadline = time.monotonic() + self._response_timeout_s
            remaining = self._response_deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout("bosun registration: response deadline exceeded")
            self.settimeout(remaining)
        return super().send(*args, **kwargs)


class RegistrationServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """The mTLS listener Individuators register against.

    The listening socket stays a PLAIN TCP socket — it is never wrapped in
    `ssl_context` — so `accept()`, which runs in this server's single
    accept loop (`ThreadingMixIn` only threads what happens after a
    connection is accepted), never performs a TLS handshake. Wrapping the
    listening socket, as an earlier version of this class did, makes
    `accept()` return an already-handshaken `SSLSocket`, which means the
    handshake itself runs inside that one accept loop: any peer that opens
    a TCP connection and then sends no ClientHello (`REGISTRATION_HOST`
    defaults to `0.0.0.0`, so no certificate is needed to do this) blocks
    `accept()` indefinitely, which blocks every other machine's
    registration — unauthenticated, no certificate required.

    So the handshake happens later and elsewhere: `process_request` below
    overrides `ThreadingMixIn`'s own method to wrap the just-accepted plain
    socket and call `do_handshake()` inside the per-connection thread
    `ThreadingMixIn` already spawns, bounded by `TLS_HANDSHAKE_TIMEOUT_S`. A
    stalled or failing handshake times out; the thread that ran it
    explicitly closes whichever object — `tls_request` once `wrap_socket`
    has returned one, `request` before that — actually holds the
    connection's file descriptor, rather than leaving that to CPython's
    reference counting. A handshake that succeeds is then bounded by its
    own wall-clock deadline for the request that follows
    (`TLS_REQUEST_TIMEOUT_S`, via `_RequestDeadlineSSLSocket` above), its
    response write bounded by a separate timeout of its own
    (`TLS_RESPONSE_TIMEOUT_S`, same class), and is closed by the server
    after that one request regardless of keep-alive (`_RegistrationHandler.
    do_POST` forces `close_connection`). None of this ever touches the
    accept loop or any other connection.
    """
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], ssl_context: ssl.SSLContext,
                 handshake_timeout_s: float | None = None,
                 request_timeout_s: float | None = None,
                 response_timeout_s: float | None = None):
        super().__init__(address, _RegistrationHandler)
        ssl_context.sslsocket_class = _RequestDeadlineSSLSocket
        self._ssl_context = ssl_context
        self._handshake_timeout_s = (
            TLS_HANDSHAKE_TIMEOUT_S if handshake_timeout_s is None else handshake_timeout_s)
        self._request_timeout_s = (
            TLS_REQUEST_TIMEOUT_S if request_timeout_s is None else request_timeout_s)
        self._response_timeout_s = (
            TLS_RESPONSE_TIMEOUT_S if response_timeout_s is None else response_timeout_s)
        ensure_db_once()

    def process_request(self, request, client_address):
        """Overrides `ThreadingMixIn.process_request` rather than
        `finish_request`, because the thread it spawns must also be the one
        that calls `shutdown_request` — `shutdown_request` closes the
        socket object it is given, and after `wrap_socket` that must be the
        TLS-wrapped object, not the plain one `accept()` returned."""
        thread = threading.Thread(
            target=self._handshake_then_serve, args=(request, client_address))
        thread.daemon = self.daemon_threads
        thread.start()

    def _handshake_then_serve(self, request, client_address):
        # `wrap_socket` detaches `request`'s file descriptor onto the new
        # SSL-wrapped object as soon as it is called, unconditionally on
        # whether the handshake that follows succeeds (CPython's
        # `ssl.SSLSocket._create` calls `sock.detach()` before doing
        # anything else). So once `wrap_socket` has returned, `request` is
        # an empty, fd-less husk and `tls_request` is the object that
        # actually owns the connection — closing the wrong one on a failed
        # handshake is a no-op, not a close.
        request.settimeout(self._handshake_timeout_s)
        tls_request = None
        try:
            tls_request = self._ssl_context.wrap_socket(
                request, server_side=True, do_handshake_on_connect=False)
            tls_request.do_handshake()
        except (ssl.SSLError, OSError):
            try:
                (tls_request if tls_request is not None else request).close()
            except OSError:
                pass
            return
        tls_request.arm_request_deadline(self._request_timeout_s)
        tls_request.arm_response_timeout(self._response_timeout_s)
        try:
            self.finish_request(tls_request, client_address)
        except Exception:
            self.handle_error(tls_request, client_address)
        finally:
            self.shutdown_request(tls_request)


class _OperatorHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    _PREFIX = "/revoke-machine/"

    def log_message(self, fmt, *args):  # noqa: A002
        pass

    def do_POST(self):
        if not self.path.startswith(self._PREFIX):
            self.send_response(404)
            self.end_headers()
            return
        machine = urllib.parse.unquote(self.path[len(self._PREFIX):])
        if not machine:
            self.send_response(400)
            self.end_headers()
            return
        count = revoke_sessions_for_machine(machine)
        _write_json(self, 200, {"machine": machine, "revoked": count})


class OperatorServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """The plain loopback listener `agentctl bosun revoke-machine` calls.
    Trusted by address alone (bound to 127.0.0.1), the same property
    platform/decisions/0057 already relies on for the mini's control socket."""
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], handler_class: type = _OperatorHandler):
        super().__init__(address, handler_class)
        ensure_db_once()
