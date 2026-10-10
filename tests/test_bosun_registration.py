"""bosun_registration — the Bosun's session registration over mutual TLS
(platform/decisions/0092; E12·5, and the registration half of E12·8).

A throwaway EC P-256 CA, built in-process with `cryptography` (already
pinned — no new dependency), issues every certificate here: a root, server
certificates (serverAuth), and machine certificates (clientAuth,
`convivy://machine/<name>`). The Library is stubbed with a REAL local
`http.server.HTTPServer` on an ephemeral port — not a monkeypatched
`urlopen` — the same `_Listener` shape tests/test_issue_client.py already
uses for exactly this reason: `library_machine_verify` makes a real
`urllib.request` call, so a server that really answers it is what proves
the request shape (headers, path, method) rather than the mock's idea of
it.
"""
from __future__ import annotations

import http.server
import json
import socket
import sqlite3
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

import bosun_registration as BRG

MACHINE = "macstudio"


# --- certificate fixtures --------------------------------------------------


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def make_root():
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    name = _name("Convivy test root")
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


def make_leaf(root_key, root_cert, *, cn="test-leaf", eku=None, san_uris=(),
              not_before=None, not_after=None):
    """A leaf certificate the throwaway root issues directly (no
    intermediate), with whatever EKU and URI SANs a test wants. `eku=None`
    omits the extension entirely; `eku=()` adds an empty one."""
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    not_before = not_before or (now - timedelta(days=1))
    not_after = not_after or (now + timedelta(days=365))
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(cn))
        .issuer_name(root_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
    )
    if san_uris:
        builder = builder.add_extension(
            x509.SubjectAlternativeName(
                [x509.UniformResourceIdentifier(u) for u in san_uris]),
            critical=False)
    if eku is not None:
        builder = builder.add_extension(x509.ExtendedKeyUsage(eku), critical=False)
    cert = builder.sign(root_key, hashes.SHA256())
    return key, cert


def make_machine_cert(root_key, root_cert, machine=MACHINE, **kw):
    kw.setdefault("eku", (ExtendedKeyUsageOID.CLIENT_AUTH,))
    kw.setdefault("san_uris", (f"convivy://machine/{machine}",))
    return make_leaf(root_key, root_cert, cn=machine, **kw)


def make_server_cert(root_key, root_cert, name="macstudio", **kw):
    kw.setdefault("eku", (ExtendedKeyUsageOID.SERVER_AUTH,))
    kw.setdefault("san_uris", (f"convivy://machine/{name}",))
    return make_leaf(root_key, root_cert, cn=name, **kw)


def der(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(Encoding.DER)


def write_pem_cert(path, cert):
    path.write_bytes(cert.public_bytes(Encoding.PEM))


def write_pem_key(path, key):
    path.write_bytes(key.private_bytes(
        Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption()))


@pytest.fixture
def root():
    return make_root()


# --- the Library stub: a REAL local HTTP server ----------------------------


class _LibraryStub:
    """A real local HTTP server answering `GET /machine-certificates/{fp}`.
    `reply(path, headers) -> (status, body_bytes_or_None)`; returning a
    `None` body means "drop the connection" (simulates unreachable)."""

    def __init__(self, reply):
        self.requests = []
        self._reply = reply
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests.append({"path": self.path, "headers": dict(self.headers)})
                status, body = outer._reply(self.path, dict(self.headers))
                if body is None:
                    self.close_connection = True
                    return
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.base_url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self._thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def library_stub():
    made = []

    def make(reply):
        stub = _LibraryStub(reply)
        made.append(stub)
        return stub
    yield make
    for s in made:
        s.close()


def _json_reply(status, obj):
    return lambda path, headers: (status, json.dumps(obj).encode("utf-8"))


# --- session store: SQLite under a tmp path ---------------------------------


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(BRG, "SESSIONS_DB", tmp_path / "sessions.db")
    BRG.ensure_db()
    return tmp_path / "sessions.db"


@pytest.fixture(autouse=True)
def token_file(tmp_path, monkeypatch):
    """Autouse (round 7): a registration driven through `handle_registration`
    reads a real credential file off disk (`LOOKUP_TOKEN_PATH`), so without
    this every test in the file is one missing opt-in away from reading the
    invoking account's actual `~/.config/bosun/library-machine-verify.token`
    and sending it to whatever stub the test stands up — round 6 fixed one
    instance of exactly that by adding this fixture to a single test; two
    more were still latent (round 6 review). Making it autouse, the same way
    `store` above isolates `SESSIONS_DB` for every test, closes the whole
    class instead of depending on each new test remembering the opt-in. A
    test that needs the file absent (e.g.
    `test_read_lookup_token_is_none_when_the_file_is_absent`) still wins by
    re-pointing `LOOKUP_TOKEN_PATH` itself after this fixture runs.
    """
    path = tmp_path / "library-machine-verify.token"
    path.write_text("test-bosun-service-token\n")
    monkeypatch.setattr(BRG, "LOOKUP_TOKEN_PATH", path)
    return path


# ============================================================================
# ensure_db_once (round 2 fix): the schema script runs once per process,
# not on every register/revoke/binding-check call
# ============================================================================


def test_ensure_db_once_runs_the_schema_script_exactly_once(monkeypatch):
    calls = []
    monkeypatch.setattr(BRG, "_db_ensured", False)
    monkeypatch.setattr(BRG, "ensure_db", lambda: calls.append(1))
    BRG.ensure_db_once()
    BRG.ensure_db_once()
    BRG.ensure_db_once()
    assert calls == [1]


def test_operator_server_construction_creates_the_schema_without_a_prior_ensure_db_call(
        tmp_path, monkeypatch):
    """Server start is one of the two places the fix calls `ensure_db_once`
    (the other is `RegistrationServer.__init__`, exercised end to end by
    every `live_registration_server` test) — so a fresh process that only
    constructs a server, never calling `BRG.ensure_db()` itself, still gets
    a working store."""
    fresh_db = tmp_path / "fresh" / "sessions.db"
    monkeypatch.setattr(BRG, "SESSIONS_DB", fresh_db)
    monkeypatch.setattr(BRG, "_db_ensured", False)
    assert not fresh_db.exists()

    server = BRG.OperatorServer(("127.0.0.1", 0))
    try:
        assert fresh_db.exists()
        conn = sqlite3.connect(str(fresh_db))
        try:
            tables = {r[0] for r in
                     conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        assert "sessions" in tables
    finally:
        server.server_close()


# ============================================================================
# client-certificate checks (0092 decision #1) — pure functions
# ============================================================================


def test_a_valid_machine_certificate_is_accepted(root):
    root_key, root_cert = root
    _key, cert = make_machine_cert(root_key, root_cert, machine="macstudio")
    check = BRG.check_client_certificate(der(cert))
    assert check.reason is None
    assert check.machine == "macstudio"


def test_a_server_certificate_presented_as_a_client_is_refused_on_the_eku_check(root):
    """Required test (0092 Handoff)."""
    root_key, root_cert = root
    _key, cert = make_server_cert(root_key, root_cert, name="macstudio")
    check = BRG.check_client_certificate(der(cert))
    assert check.reason == "eku_missing_clientauth"
    assert check.machine is None


def test_a_certificate_with_no_eku_extension_is_refused(root):
    root_key, root_cert = root
    _key, cert = make_leaf(root_key, root_cert, eku=None,
                           san_uris=("convivy://machine/macstudio",))
    check = BRG.check_client_certificate(der(cert))
    assert check.reason == "eku_missing_clientauth"


def test_a_chain_valid_certificate_with_no_uri_san_is_refused(root):
    """Required test (0092 Handoff): zero URI SANs refuses."""
    root_key, root_cert = root
    _key, cert = make_machine_cert(root_key, root_cert, san_uris=())
    check = BRG.check_client_certificate(der(cert))
    assert check.reason == "san_not_exactly_one"


def test_a_chain_valid_certificate_with_two_uri_sans_is_refused(root):
    """Required test (0092 Handoff): two URI SANs refuses."""
    root_key, root_cert = root
    _key, cert = make_machine_cert(
        root_key, root_cert,
        san_uris=("convivy://machine/macstudio", "convivy://machine/macbook"))
    check = BRG.check_client_certificate(der(cert))
    assert check.reason == "san_not_exactly_one"


def test_a_uri_san_not_matching_the_machine_scheme_is_refused(root):
    root_key, root_cert = root
    _key, cert = make_machine_cert(root_key, root_cert,
                                   san_uris=("convivy://signer/macstudio",))
    check = BRG.check_client_certificate(der(cert))
    assert check.reason == "san_not_exactly_one"


def test_check_client_certificate_never_reads_the_common_name():
    """The common name is never identity (0092 decision #1) — a certificate
    whose CN disagrees with its SAN still resolves to the SAN's name."""
    root_key, root_cert = make_root()
    _key, cert = make_leaf(root_key, root_cert, cn="not-the-machine",
                           eku=(ExtendedKeyUsageOID.CLIENT_AUTH,),
                           san_uris=("convivy://machine/macstudio",))
    check = BRG.check_client_certificate(der(cert))
    assert check.machine == "macstudio"


def test_fingerprint_sha256_der_matches_a_direct_hash(root):
    import hashlib
    root_key, root_cert = root
    _key, cert = make_machine_cert(root_key, root_cert)
    d = der(cert)
    assert BRG.fingerprint_sha256_der(d) == hashlib.sha256(d).hexdigest()


# ============================================================================
# the registry lookup (0092 decision #4; library-protocol §2.3)
# ============================================================================


def test_library_machine_verify_accepts_an_active_matching_row(library_stub):
    stub = library_stub(_json_reply(200, {
        "machine": MACHINE, "fingerprint_sha256": "f" * 64, "status": "active",
        "active": True, "custody": "hardware", "not_after": "2099-01-01T00:00:00Z"}))
    result = BRG.library_machine_verify("f" * 64, MACHINE, base_url=stub.base_url,
                                        token="tok", timeout=2)
    assert result.ok is True
    assert result.reason is None
    # The exact request shape the Library expects (library-protocol §2.3).
    assert stub.requests[0]["path"] == f"/machine-certificates/{'f' * 64}"
    assert stub.requests[0]["headers"]["Authorization"] == "Bearer tok"
    assert stub.requests[0]["headers"]["Library-Protocol"] == "1"


def test_library_machine_verify_refuses_404_as_route_missing(library_stub):
    stub = library_stub(_json_reply(404, {"detail": "not found"}))
    result = BRG.library_machine_verify("f" * 64, MACHINE, base_url=stub.base_url,
                                        token="tok", timeout=2)
    assert (result.ok, result.reason) == (False, "library_route_missing")


@pytest.mark.parametrize("status", [400, 403, 500])
def test_library_machine_verify_refuses_other_non_200_as_library_error(library_stub, status):
    stub = library_stub(_json_reply(status, {"detail": "no"}))
    result = BRG.library_machine_verify("f" * 64, MACHINE, base_url=stub.base_url,
                                        token="tok", timeout=2)
    assert (result.ok, result.reason) == (False, "library_error")


def test_library_machine_verify_refuses_a_timeout_as_unreachable(library_stub):
    stub = library_stub(lambda path, headers: (200, None))  # never replies
    result = BRG.library_machine_verify("f" * 64, MACHINE, base_url=stub.base_url,
                                        token="tok", timeout=0.2)
    assert (result.ok, result.reason) == (False, "library_unreachable")


def test_library_machine_verify_refuses_a_connect_failure_as_unreachable():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    unused_port = sock.getsockname()[1]
    sock.close()  # closed immediately — nothing listens here
    result = BRG.library_machine_verify(
        "f" * 64, MACHINE, base_url=f"http://127.0.0.1:{unused_port}",
        token="tok", timeout=2)
    assert (result.ok, result.reason) == (False, "library_unreachable")


def test_library_machine_verify_refuses_malformed_json(library_stub):
    stub = library_stub(lambda path, headers: (200, b"not json"))
    result = BRG.library_machine_verify("f" * 64, MACHINE, base_url=stub.base_url,
                                        token="tok", timeout=2)
    assert (result.ok, result.reason) == (False, "library_malformed")


def test_library_machine_verify_refuses_active_as_a_string(library_stub):
    """Required test (0092 Handoff): `{"active": "true"}` (a string)."""
    stub = library_stub(_json_reply(200, {"active": "true"}))
    result = BRG.library_machine_verify("f" * 64, MACHINE, base_url=stub.base_url,
                                        token="tok", timeout=2)
    assert (result.ok, result.reason) == (False, "library_malformed")


def test_library_machine_verify_refuses_an_active_row_whose_active_field_is_a_string(
        library_stub):
    """Distinct from the test above: `status` really is `"active"` here, so
    this is the one case that reaches the `active is not True` check itself
    rather than being caught earlier by the missing/unknown-status check."""
    stub = library_stub(_json_reply(200, {
        "machine": MACHINE, "fingerprint_sha256": "f" * 64, "status": "active",
        "active": "true", "custody": "hardware", "not_after": "2099-01-01T00:00:00Z"}))
    result = BRG.library_machine_verify("f" * 64, MACHINE, base_url=stub.base_url,
                                        token="tok", timeout=2)
    assert (result.ok, result.reason) == (False, "library_malformed")


def test_library_machine_verify_refuses_an_unknown_fingerprint(library_stub):
    stub = library_stub(_json_reply(
        200, {"fingerprint_sha256": "f" * 64, "status": "unknown", "active": False}))
    result = BRG.library_machine_verify("f" * 64, MACHINE, base_url=stub.base_url,
                                        token="tok", timeout=2)
    assert (result.ok, result.reason) == (False, "machine_unknown")
    assert result.machine is None  # names no machine


def test_library_machine_verify_refuses_a_revoked_row_naming_its_status(library_stub):
    stub = library_stub(_json_reply(200, {
        "machine": MACHINE, "fingerprint_sha256": "f" * 64, "status": "revoked",
        "active": False, "custody": "hardware", "not_after": "2099-01-01T00:00:00Z"}))
    result = BRG.library_machine_verify("f" * 64, MACHINE, base_url=stub.base_url,
                                        token="tok", timeout=2)
    assert (result.ok, result.reason, result.status) == (False, "machine_inactive", "revoked")
    assert result.machine == MACHINE


def test_library_machine_verify_refuses_an_expired_row_naming_its_status(library_stub):
    stub = library_stub(_json_reply(200, {
        "machine": MACHINE, "fingerprint_sha256": "f" * 64, "status": "expired",
        "active": False, "custody": "hardware", "not_after": "2020-01-01T00:00:00Z"}))
    result = BRG.library_machine_verify("f" * 64, MACHINE, base_url=stub.base_url,
                                        token="tok", timeout=2)
    assert (result.ok, result.reason, result.status) == (False, "machine_inactive", "expired")


def test_library_machine_verify_refuses_a_name_mismatch(library_stub):
    stub = library_stub(_json_reply(200, {
        "machine": "some-other-machine", "fingerprint_sha256": "f" * 64,
        "status": "active", "active": True, "custody": "hardware",
        "not_after": "2099-01-01T00:00:00Z"}))
    result = BRG.library_machine_verify("f" * 64, MACHINE, base_url=stub.base_url,
                                        token="tok", timeout=2)
    assert (result.ok, result.reason) == (False, "machine_mismatch")
    assert result.machine == "some-other-machine"


def test_read_lookup_token_reads_the_configured_file(token_file):
    assert BRG.read_lookup_token() == "test-bosun-service-token"


def test_read_lookup_token_is_none_when_the_file_is_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(BRG, "LOOKUP_TOKEN_PATH", tmp_path / "does-not-exist.token")
    assert BRG.read_lookup_token() is None


# ============================================================================
# session store (0092 decision #2)
# ============================================================================


def test_register_session_returns_a_token_bound_to_the_fingerprint():
    created = BRG.register_session(machine=MACHINE, fingerprint_sha256="f" * 64,
                                   account="operator", caller_kind="operator",
                                   ceiling="full", protocol_version="1")
    assert created["session_id"]
    assert created["session_token"]
    session, reason = BRG.check_session_binding(created["session_token"], "f" * 64)
    assert reason is None
    assert session["machine"] == MACHINE
    assert session["account"] == "operator"


def test_a_session_token_presented_under_a_different_certificate_is_refused():
    """Required test (0092 Handoff + this slice's brief)."""
    created = BRG.register_session(machine=MACHINE, fingerprint_sha256="f" * 64,
                                   account="operator", caller_kind="operator",
                                   ceiling="full", protocol_version="1")
    session, reason = BRG.check_session_binding(created["session_token"], "e" * 64)
    assert session is None
    assert reason == "session_machine_mismatch"


def test_an_unrecognised_token_is_refused_as_session_unknown():
    session, reason = BRG.check_session_binding("not-a-real-token", "f" * 64)
    assert session is None
    assert reason == "session_unknown"


def test_revoke_sessions_for_machine_ends_only_that_machines_live_sessions():
    a = BRG.register_session(machine="macstudio", fingerprint_sha256="a" * 64,
                             account="operator", caller_kind="operator",
                             ceiling="full", protocol_version="1")
    b = BRG.register_session(machine="macstudio", fingerprint_sha256="b" * 64,
                             account="operator", caller_kind="agent",
                             ceiling="full", protocol_version="1")
    other = BRG.register_session(machine="macbook", fingerprint_sha256="c" * 64,
                                 account="operator", caller_kind="operator",
                                 ceiling="full", protocol_version="1")
    count = BRG.revoke_sessions_for_machine("macstudio")
    assert count == 2
    assert BRG.check_session_binding(a["session_token"], "a" * 64)[1] == "session_unknown"
    assert BRG.check_session_binding(b["session_token"], "b" * 64)[1] == "session_unknown"
    # a different machine's sessions survive.
    session, reason = BRG.check_session_binding(other["session_token"], "c" * 64)
    assert reason is None
    assert session["machine"] == "macbook"


def test_revoke_sessions_for_machine_is_idempotent():
    BRG.register_session(machine="macstudio", fingerprint_sha256="a" * 64, account="operator",
                         caller_kind="operator", ceiling="full", protocol_version="1")
    assert BRG.revoke_sessions_for_machine("macstudio") == 1
    assert BRG.revoke_sessions_for_machine("macstudio") == 0


def test_revoke_sessions_for_an_unregistered_machine_name_ends_nothing():
    assert BRG.revoke_sessions_for_machine("never-registered") == 0


def test_a_machine_revoked_in_the_registry_is_refused_at_its_next_registration_lookup_while_its_session_survives_until_revoke_by_machine(
        library_stub):
    """Required test (0092 Handoff): combines the two pieces round 1's
    review found tested only separately —
    `test_library_machine_verify_refuses_a_revoked_row_naming_its_status`
    (the lookup alone) and
    `test_revoke_sessions_for_machine_ends_only_that_machines_live_sessions`
    (the CLI-path revoke alone) — into the one ordered flow 0092 names:
    a registry revocation is seen at the next registration lookup, but does
    not by itself touch an existing session (0092's "catch" — per-call
    binding consults no registry); only an explicit revoke-by-machine ends
    it."""
    live = BRG.register_session(machine=MACHINE, fingerprint_sha256="f" * 64,
                                account="operator", caller_kind="operator",
                                ceiling="full", protocol_version="1")

    stub = library_stub(_json_reply(200, {
        "machine": MACHINE, "fingerprint_sha256": "f" * 64, "status": "revoked",
        "active": False, "custody": "hardware", "not_after": "2099-01-01T00:00:00Z"}))
    result = BRG.library_machine_verify("f" * 64, MACHINE, base_url=stub.base_url,
                                        token="tok", timeout=2)
    assert (result.ok, result.reason, result.status) == (False, "machine_inactive", "revoked")

    session, reason = BRG.check_session_binding(live["session_token"], "f" * 64)
    assert reason is None
    assert session["machine"] == MACHINE

    count = BRG.revoke_sessions_for_machine(MACHINE)
    assert count == 1
    assert BRG.check_session_binding(live["session_token"], "f" * 64)[1] == "session_unknown"


# ============================================================================
# registration orchestration (handle_registration)
# ============================================================================


def _body(**overrides):
    base = {"account": "operator", "caller_kind": "operator", "ceiling": "full",
            "protocol_version": "1"}
    base.update(overrides)
    return base


def test_handle_registration_succeeds_end_to_end(root, library_stub, token_file, monkeypatch):
    root_key, root_cert = root
    _key, cert = make_machine_cert(root_key, root_cert, machine=MACHINE)
    d = der(cert)
    fp = BRG.fingerprint_sha256_der(d)
    stub = library_stub(_json_reply(200, {
        "machine": MACHINE, "fingerprint_sha256": fp, "status": "active",
        "active": True, "custody": "hardware", "not_after": "2099-01-01T00:00:00Z"}))
    monkeypatch.setattr(BRG, "LIBRARY_LOOKUP_BASE_URL", stub.base_url)

    outcome = BRG.handle_registration(d, _body())

    assert outcome.ok is True
    session, reason = BRG.check_session_binding(outcome.session_token, fp)
    assert reason is None
    assert session["machine"] == MACHINE
    assert session["account"] == "operator"
    assert session["caller_kind"] == "operator"


def test_handle_registration_ignores_a_machine_field_in_the_body(
        root, library_stub, token_file, monkeypatch):
    """0092 decision #2: the machine comes from the handshake."""
    root_key, root_cert = root
    _key, cert = make_machine_cert(root_key, root_cert, machine=MACHINE)
    d = der(cert)
    fp = BRG.fingerprint_sha256_der(d)
    stub = library_stub(_json_reply(200, {
        "machine": MACHINE, "fingerprint_sha256": fp, "status": "active",
        "active": True, "custody": "hardware", "not_after": "2099-01-01T00:00:00Z"}))
    monkeypatch.setattr(BRG, "LIBRARY_LOOKUP_BASE_URL", stub.base_url)

    outcome = BRG.handle_registration(d, _body(machine="someone-elses-machine"))

    assert outcome.ok is True
    session, _ = BRG.check_session_binding(outcome.session_token, fp)
    assert session["machine"] == MACHINE


@pytest.mark.parametrize("missing", ["account", "caller_kind", "ceiling", "protocol_version"])
def test_handle_registration_refuses_a_body_missing_a_required_key(root, missing):
    root_key, root_cert = root
    _key, cert = make_machine_cert(root_key, root_cert)
    body = _body()
    del body[missing]
    outcome = BRG.handle_registration(der(cert), body)
    assert outcome.ok is False
    assert outcome.reason == "malformed_body"


def test_handle_registration_refuses_an_unknown_caller_kind(root):
    root_key, root_cert = root
    _key, cert = make_machine_cert(root_key, root_cert)
    outcome = BRG.handle_registration(der(cert), _body(caller_kind="superuser"))
    assert outcome.ok is False
    assert outcome.reason == "malformed_body"


def test_handle_registration_refuses_a_ceiling_this_vocabulary_cannot_parse(root):
    """A ceiling that `bosun_authz.ceiling_entries_from_text` cannot read is
    refused at registration, before it is ever stored — so `/authorize`
    never has to treat a stored row as unreadable."""
    root_key, root_cert = root
    _key, cert = make_machine_cert(root_key, root_cert)
    outcome = BRG.handle_registration(der(cert), _body(ceiling="not json and not 'full'"))
    assert outcome.ok is False
    assert outcome.reason == "malformed_ceiling"


def test_handle_registration_refuses_a_ceiling_array_containing_the_full_sentinel(root):
    """FIX (security suggestion, round 2): `'["*"]'` is refused the same as
    any other unparseable ceiling — `"full"` is the one spelling of the
    unrestricted sentinel; an array can still cover everything a kind can
    name (e.g. `["path:/"]`), so this refusal narrows the sentinel's
    spelling, not what an array can grant (round 3)."""
    root_key, root_cert = root
    _key, cert = make_machine_cert(root_key, root_cert)
    outcome = BRG.handle_registration(der(cert), _body(ceiling='["*"]'))
    assert outcome.ok is False
    assert outcome.reason == "malformed_ceiling"


def test_handle_registration_refuses_on_the_cert_check_before_touching_the_body(root):
    root_key, root_cert = root
    _key, cert = make_server_cert(root_key, root_cert)
    outcome = BRG.handle_registration(der(cert), {"not": "even a valid body"})
    assert outcome.ok is False
    assert outcome.reason == "eku_missing_clientauth"


def test_handle_registration_sends_no_request_when_the_lookup_credential_is_missing(
        root, tmp_path, monkeypatch):
    """Required test (0092 Handoff): `lookup_credential_missing` with no
    request sent to the Library."""
    root_key, root_cert = root
    _key, cert = make_machine_cert(root_key, root_cert)
    monkeypatch.setattr(BRG, "LOOKUP_TOKEN_PATH", tmp_path / "absent.token")

    calls = []
    monkeypatch.setattr(BRG, "library_machine_verify",
                        lambda *a, **k: calls.append((a, k)) or BRG.LookupResult(True))

    outcome = BRG.handle_registration(der(cert), _body())

    assert outcome.ok is False
    assert outcome.reason == "lookup_credential_missing"
    assert calls == []  # no request sent


def test_handle_registration_revokes_other_live_sessions_when_the_row_reads_revoked(
        root, library_stub, token_file, monkeypatch):
    live = BRG.register_session(machine=MACHINE, fingerprint_sha256="old-fp",
                                account="operator", caller_kind="operator",
                                ceiling="full", protocol_version="1")
    root_key, root_cert = root
    _key, cert = make_machine_cert(root_key, root_cert, machine=MACHINE)
    d = der(cert)
    fp = BRG.fingerprint_sha256_der(d)
    stub = library_stub(_json_reply(200, {
        "machine": MACHINE, "fingerprint_sha256": fp, "status": "revoked",
        "active": False, "custody": "hardware", "not_after": "2099-01-01T00:00:00Z"}))
    monkeypatch.setattr(BRG, "LIBRARY_LOOKUP_BASE_URL", stub.base_url)

    outcome = BRG.handle_registration(d, _body())

    assert outcome.ok is False
    assert outcome.reason == "machine_inactive"
    assert BRG.check_session_binding(live["session_token"], "old-fp")[1] == "session_unknown"


def test_handle_registration_revokes_other_live_sessions_when_the_row_reads_expired(
        root, library_stub, token_file, monkeypatch):
    live = BRG.register_session(machine=MACHINE, fingerprint_sha256="old-fp",
                                account="operator", caller_kind="operator",
                                ceiling="full", protocol_version="1")
    root_key, root_cert = root
    _key, cert = make_machine_cert(root_key, root_cert, machine=MACHINE)
    d = der(cert)
    fp = BRG.fingerprint_sha256_der(d)
    stub = library_stub(_json_reply(200, {
        "machine": MACHINE, "fingerprint_sha256": fp, "status": "expired",
        "active": False, "custody": "hardware", "not_after": "2020-01-01T00:00:00Z"}))
    monkeypatch.setattr(BRG, "LIBRARY_LOOKUP_BASE_URL", stub.base_url)

    outcome = BRG.handle_registration(d, _body())

    assert outcome.ok is False
    assert outcome.reason == "machine_inactive"
    assert BRG.check_session_binding(live["session_token"], "old-fp")[1] == "session_unknown"


def test_an_unknown_fingerprint_claiming_a_live_machines_name_leaves_its_sessions_alive(
        root, library_stub, token_file, monkeypatch):
    """Required test (0092 Handoff): an unknown answer names no machine and
    revokes nothing, even when the presented certificate's own SAN claims a
    machine name that does have live sessions."""
    live = BRG.register_session(machine=MACHINE, fingerprint_sha256="old-fp",
                                account="operator", caller_kind="operator",
                                ceiling="full", protocol_version="1")
    root_key, root_cert = root
    _key, cert = make_machine_cert(root_key, root_cert, machine=MACHINE)
    d = der(cert)
    fp = BRG.fingerprint_sha256_der(d)
    stub = library_stub(_json_reply(
        200, {"fingerprint_sha256": fp, "status": "unknown", "active": False}))
    monkeypatch.setattr(BRG, "LIBRARY_LOOKUP_BASE_URL", stub.base_url)

    outcome = BRG.handle_registration(d, _body())

    assert outcome.ok is False
    assert outcome.reason == "machine_unknown"
    session, reason = BRG.check_session_binding(live["session_token"], "old-fp")
    assert reason is None
    assert session["machine"] == MACHINE


# ============================================================================
# the per-call authorization endpoint (E12·3's endpoint half; E12·8's
# per-call half) — handle_authorization, on the pure in-process helpers, the
# same style as handle_registration's own tests above.
# ============================================================================


def _authorize_body(**overrides):
    base = {"session_token": "tok", "tool_name": "Read", "input": {"file_path": "/work/x"}}
    base.update(overrides)
    return base


def _registered(root, machine=MACHINE, ceiling="full"):
    """A registered session plus the DER and fingerprint of the machine
    certificate that registered it, for presenting on the `/authorize`
    call."""
    root_key, root_cert = root
    _key, cert = make_machine_cert(root_key, root_cert, machine=machine)
    d = der(cert)
    fp = BRG.fingerprint_sha256_der(d)
    created = BRG.register_session(machine=machine, fingerprint_sha256=fp, account="operator",
                                   caller_kind="operator", ceiling=ceiling,
                                   protocol_version="1")
    return created, d, fp


def test_handle_authorization_refuses_a_request_missing_the_session_token(root, monkeypatch):
    monkeypatch.setattr(BRG.AU, "decide",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("decide called")))
    _created, d, _fp = _registered(root)
    body = _authorize_body()
    del body["session_token"]
    outcome = BRG.handle_authorization(d, body)
    assert outcome.refused == "malformed_request"
    assert outcome.decision is None


def test_handle_authorization_refuses_a_token_under_a_different_certificate(root, monkeypatch):
    """Required test: a token presented under a different certificate is
    refused before decide() runs."""
    monkeypatch.setattr(BRG.AU, "decide",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("decide called")))
    created, _d, _fp = _registered(root)
    root_key, root_cert = root
    _key, other_cert = make_machine_cert(root_key, root_cert, machine="macbook")
    outcome = BRG.handle_authorization(
        der(other_cert), _authorize_body(session_token=created["session_token"]))
    assert outcome.refused == "session_machine_mismatch"
    assert outcome.decision is None


def test_handle_authorization_refuses_a_revoked_session(root, monkeypatch):
    """Required test: a revoked session is refused before decide() runs."""
    monkeypatch.setattr(BRG.AU, "decide",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("decide called")))
    created, d, _fp = _registered(root)
    BRG.revoke_sessions_for_machine(MACHINE)
    outcome = BRG.handle_authorization(
        d, _authorize_body(session_token=created["session_token"]))
    assert outcome.refused == "session_unknown"
    assert outcome.decision is None


@pytest.mark.parametrize("body_overrides", [
    {"tool_name": 7},
    {"tool_name": ""},
    {"input": "not-a-dict"},
])
def test_handle_authorization_refuses_a_malformed_request(root, body_overrides):
    """Required test: a malformed request is refused with its reason."""
    created, d, _fp = _registered(root)
    outcome = BRG.handle_authorization(
        d, _authorize_body(session_token=created["session_token"], **body_overrides))
    assert outcome.refused == "malformed_request"


def test_handle_authorization_allows_a_named_target_call_inside_a_reduced_ceiling(root):
    """E12·8's test, inside half."""
    ceiling = BRG.AU.ceiling_text_from_entries({BRG.AU.scope(BRG.AU.PATH, "/work/allowed")})
    created, d, _fp = _registered(root, ceiling=ceiling)
    outcome = BRG.handle_authorization(d, _authorize_body(
        session_token=created["session_token"], tool_name="Read",
        input={"file_path": "/work/allowed/file.txt"}))
    assert outcome.refused is None
    assert outcome.decision.allow is True
    assert isinstance(outcome.decision.authorization, BRG.AU.ObjectScopedAuthorization)


def test_handle_authorization_denies_a_named_target_call_outside_a_reduced_ceiling(root):
    """E12·8's test, outside half."""
    ceiling = BRG.AU.ceiling_text_from_entries({BRG.AU.scope(BRG.AU.PATH, "/work/allowed")})
    created, d, _fp = _registered(root, ceiling=ceiling)
    outcome = BRG.handle_authorization(d, _authorize_body(
        session_token=created["session_token"], tool_name="Read",
        input={"file_path": "/etc/passwd"}))
    assert outcome.refused is None
    assert outcome.decision.allow is False
    assert "ceiling" in outcome.decision.reason


def test_handle_authorization_boundary_scoped_call_is_not_refused_by_the_ceiling(root):
    """Required test: a boundary-scoped (executing) call is not refused by
    the ceiling — platform/decisions/0057's "two authorization shapes"."""
    ceiling = BRG.AU.ceiling_text_from_entries({BRG.AU.scope(BRG.AU.PATH, "/work/allowed")})
    created, d, _fp = _registered(root, ceiling=ceiling)
    outcome = BRG.handle_authorization(d, _authorize_body(
        session_token=created["session_token"], tool_name="Bash",
        input={"command": "rm -rf /etc"}))
    assert outcome.refused is None
    assert outcome.decision.allow is True
    assert isinstance(outcome.decision.authorization, BRG.AU.BoundaryGrant)


def test_handle_authorization_writes_one_audit_record_per_decision(root):
    """Required test: every decision writes one audit record."""
    created, d, _fp = _registered(root)
    BRG.handle_authorization(d, _authorize_body(session_token=created["session_token"],
                                                 tool_name="Read",
                                                 input={"file_path": "/work/x"}))
    BRG.handle_authorization(d, _authorize_body(session_token=created["session_token"],
                                                 tool_name="Bash",
                                                 input={"command": "ls"}))
    records = BRG.audit_records_for_session(created["session_id"])
    assert len(records) == 2
    assert records[0]["session_id"] == created["session_id"]
    assert records[0]["machine"] == MACHINE
    assert records[0]["tool_name"] == "Read"
    assert records[0]["allow"] == 1
    assert records[0]["reason"]
    assert records[0]["at"]
    assert records[1]["tool_name"] == "Bash"


def test_handle_authorization_a_refusal_before_decide_writes_no_audit_record(root):
    created, d, _fp = _registered(root)
    BRG.revoke_sessions_for_machine(MACHINE)
    outcome = BRG.handle_authorization(
        d, _authorize_body(session_token=created["session_token"]))
    assert outcome.refused == "session_unknown"
    assert BRG.audit_records_for_session(created["session_id"]) == []


def test_handle_authorization_ignores_a_leftover_availability_column(root):
    """Authorization does not depend on any availability value. A row whose
    leftover `availability_lever` column says a non-strict position, written
    straight into the database, still gets the strict answer: a Bash call is
    allowed as a boundary grant. (The column is kept for existing sessions.db
    files; see `_SCHEMA`.)"""
    created, d, _fp = _registered(root, ceiling="full")
    conn = sqlite3.connect(str(BRG.SESSIONS_DB))
    try:
        conn.execute("UPDATE sessions SET availability_lever = 'degraded_offline' WHERE id = ?",
                     (created["session_id"],))
        conn.commit()
    finally:
        conn.close()

    outcome = BRG.handle_authorization(d, _authorize_body(
        session_token=created["session_token"], tool_name="Bash",
        input={"command": "ls"}))

    assert outcome.refused is None
    assert outcome.decision.allow is True
    assert isinstance(outcome.decision.authorization, BRG.AU.BoundaryGrant)


def test_handle_authorization_denied_named_target_call_records_its_object_ref(root):
    """FIX (reviewer): `decide()` computes the object a denied named-target
    call named only for the reason text, never for the returned
    `Decision.authorization` (which stays None on every deny path). The
    audit row must still record the target the request named."""
    ceiling = BRG.AU.ceiling_text_from_entries({BRG.AU.scope(BRG.AU.PATH, "/work/allowed")})
    created, d, _fp = _registered(root, ceiling=ceiling)

    outcome = BRG.handle_authorization(d, _authorize_body(
        session_token=created["session_token"], tool_name="Read",
        input={"file_path": "/etc/passwd"}))

    assert outcome.decision.allow is False
    records = BRG.audit_records_for_session(created["session_id"])
    assert len(records) == 1
    assert records[0]["allow"] == 0
    assert records[0]["object_ref"] == BRG.AU.scope(BRG.AU.PATH, "/etc/passwd")


def test_handle_authorization_allows_a_denied_tool__GAP_no_permission_set_wiring(root):
    """DOCUMENTS A GAP, NOT A GUARANTEE — INVERT THIS TEST WHEN THE WIRING
    LANDS. `handle_authorization` calls `bosun_authz.decide()` with a
    constant, always-empty `PermissionSet` (`BRG._PERMISSION_SET_NOT_WIRED`)
    because no agent identity reaches the Bosun under
    platform/decisions/0092 (round-1 security-review blocking finding). So a
    tool name that a populated `PermissionSet` would deny is allowed through
    `/authorize` today, regardless of the agent it actually belongs to. Once
    the wiring lands (see the filed work item on
    `lib/agent-platform/bosun_registration.py`'s `handle_authorization`),
    this test's final assertion must flip to `is False` and its name must
    drop the GAP/invert marker."""
    created, d, _fp = _registered(root, ceiling="full")
    tool_name = "Read"

    # Sanity: a populated PermissionSet really would deny this tool — the
    # check this endpoint does not perform.
    assert BRG.AU.PermissionSet(denied=frozenset({tool_name})).denies(tool_name) is True

    outcome = BRG.handle_authorization(d, _authorize_body(
        session_token=created["session_token"], tool_name=tool_name,
        input={"file_path": "/work/x"}))

    assert outcome.refused is None
    assert outcome.decision.allow is True


# ============================================================================
# TLS preflight (0092 decision #1; Handoff) — structural properties
# ============================================================================


@pytest.fixture
def tls_material(tmp_path, root):
    root_key, root_cert = root
    ca_path = tmp_path / "ca.crt"
    write_pem_cert(ca_path, root_cert)
    server_key, server_cert = make_server_cert(root_key, root_cert, name="macstudio")
    cert_path, key_path = tmp_path / "server.crt", tmp_path / "server.key"
    write_pem_cert(cert_path, server_cert)
    write_pem_key(key_path, server_key)
    return ca_path, cert_path, key_path, root_key, root_cert


def test_preflight_refuses_without_a_server_certificate(tls_material):
    ca_path, _cert, _key, _rk, _rc = tls_material
    with pytest.raises(BRG.PreflightError, match="no server certificate"):
        BRG.build_server_ssl_context(trust_anchor_path=ca_path,
                                     server_cert_path=None, server_key_path=None)


def test_preflight_refuses_a_missing_trust_anchor(tmp_path, tls_material):
    _ca, cert_path, key_path, _rk, _rc = tls_material
    with pytest.raises(BRG.PreflightError, match="trust anchor"):
        BRG.build_server_ssl_context(trust_anchor_path=tmp_path / "no-such-ca.crt",
                                     server_cert_path=str(cert_path),
                                     server_key_path=str(key_path))


def test_preflight_builds_a_tls13_only_cert_required_context(tls_material):
    ca_path, cert_path, key_path, _rk, _rc = tls_material
    ctx = BRG.build_server_ssl_context(trust_anchor_path=ca_path,
                                       server_cert_path=str(cert_path),
                                       server_key_path=str(key_path))
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_3
    assert ctx.maximum_version == ssl.TLSVersion.TLSv1_3
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_preflight_refuses_when_the_interpreter_has_no_tls13(tls_material, monkeypatch):
    ca_path, cert_path, key_path, _rk, _rc = tls_material
    monkeypatch.setattr(ssl, "HAS_TLSv1_3", False)
    with pytest.raises(BRG.PreflightError, match="TLS 1.3"):
        BRG.build_server_ssl_context(trust_anchor_path=ca_path,
                                     server_cert_path=str(cert_path),
                                     server_key_path=str(key_path))


# ============================================================================
# the real mTLS listener — end to end over actual sockets
# ============================================================================


@pytest.fixture
def live_registration_server(tls_material, token_file):
    ca_path, cert_path, key_path, root_key, root_cert = tls_material
    ctx = BRG.build_server_ssl_context(trust_anchor_path=ca_path,
                                       server_cert_path=str(cert_path),
                                       server_key_path=str(key_path))
    server = BRG.RegistrationServer(("127.0.0.1", 0), ctx)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, port, root_key, root_cert
    server.shutdown()
    server.server_close()


def _client_ssl_context(ca_path, *, certfile=None, keyfile=None):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(cafile=str(ca_path))
    ctx.check_hostname = False
    if certfile:
        ctx.load_cert_chain(certfile=str(certfile), keyfile=str(keyfile))
    return ctx


def _post(port, ca_path, body, *, path="/sessions", certfile=None, keyfile=None, raw_http=None):
    """A raw mTLS POST, returning (status, json_body) — or raising on a
    handshake failure, for the no-cert case."""
    ctx = _client_ssl_context(ca_path, certfile=certfile, keyfile=keyfile)
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        with ctx.wrap_socket(sock, server_hostname="macstudio") as tls:
            payload = raw_http if raw_http is not None else json.dumps(body).encode()
            request = (f"POST {path} HTTP/1.1\r\nHost: macstudio\r\n"
                       f"Content-Length: {len(payload)}\r\n"
                       f"X-Client-Cert: spoofed\r\nConnection: close\r\n\r\n").encode() + payload
            tls.sendall(request)
            resp = b""
            while True:
                chunk = tls.recv(4096)
                if not chunk:
                    break
                resp += chunk
    head, _, raw_body = resp.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1])
    return status, json.loads(raw_body) if raw_body else None


def test_live_server_registers_a_valid_machine_over_real_mtls(
        tmp_path, live_registration_server, library_stub, monkeypatch):
    server, port, root_key, root_cert = live_registration_server
    ca_path = tmp_path / "ca-for-client.crt"
    write_pem_cert(ca_path, root_cert)
    key, cert = make_machine_cert(root_key, root_cert, machine=MACHINE)
    cert_path, key_path = tmp_path / "machine.crt", tmp_path / "machine.key"
    write_pem_cert(cert_path, cert)
    write_pem_key(key_path, key)
    fp = BRG.fingerprint_sha256_der(der(cert))

    stub = library_stub(_json_reply(200, {
        "machine": MACHINE, "fingerprint_sha256": fp, "status": "active",
        "active": True, "custody": "hardware", "not_after": "2099-01-01T00:00:00Z"}))
    monkeypatch.setattr(BRG, "LIBRARY_LOOKUP_BASE_URL", stub.base_url)

    body = {"account": "operator", "caller_kind": "operator", "ceiling": "full",
            "protocol_version": "1"}
    status, payload = _post(port, ca_path, body, certfile=cert_path, keyfile=key_path)

    assert status == 200
    assert payload["session_token"]
    session, reason = BRG.check_session_binding(payload["session_token"], fp)
    assert reason is None
    assert session["machine"] == MACHINE


def test_live_server_authorizes_a_call_over_the_same_mtls_listener(
        tmp_path, live_registration_server, library_stub, monkeypatch):
    """`/authorize` lives on the same mTLS listener `/sessions` does — never
    a second one (this slice's brief)."""
    server, port, root_key, root_cert = live_registration_server
    ca_path = tmp_path / "ca-for-client.crt"
    write_pem_cert(ca_path, root_cert)
    key, cert = make_machine_cert(root_key, root_cert, machine=MACHINE)
    cert_path, key_path = tmp_path / "machine.crt", tmp_path / "machine.key"
    write_pem_cert(cert_path, cert)
    write_pem_key(key_path, key)
    fp = BRG.fingerprint_sha256_der(der(cert))

    stub = library_stub(_json_reply(200, {
        "machine": MACHINE, "fingerprint_sha256": fp, "status": "active",
        "active": True, "custody": "hardware", "not_after": "2099-01-01T00:00:00Z"}))
    monkeypatch.setattr(BRG, "LIBRARY_LOOKUP_BASE_URL", stub.base_url)

    reg_body = {"account": "operator", "caller_kind": "operator", "ceiling": "full",
                "protocol_version": "1"}
    status, payload = _post(port, ca_path, reg_body, certfile=cert_path, keyfile=key_path)
    assert status == 200
    token = payload["session_token"]

    authz_body = {"session_token": token, "tool_name": "Read",
                  "input": {"file_path": "/work/x"}}
    status, payload = _post(port, ca_path, authz_body, path="/authorize",
                            certfile=cert_path, keyfile=key_path)

    assert status == 200
    assert payload["allow"] is True
    assert payload["authorization"]["kind"] == "object"


def test_live_server_refuses_a_server_cert_presented_as_a_client(
        tmp_path, live_registration_server):
    """Required test (0092 Handoff), exercised through the real listener.

    OpenSSL's own client-purpose check — applied during `CERT_REQUIRED`
    verification independently of this module's `check_client_certificate`
    — already rejects a certificate whose EKU names `serverAuth` and not
    `clientAuth`, so this fails at the handshake itself rather than
    reaching the application and getting our 401 JSON body. The pure-unit
    test above (`..._refused_on_the_eku_check`) is what proves OUR refusal
    reason; this one proves the live listener refuses the same input too,
    end to end, and documents that it does so one layer earlier than ours."""
    server, port, root_key, root_cert = live_registration_server
    ca_path = tmp_path / "ca-for-client.crt"
    write_pem_cert(ca_path, root_cert)
    key, cert = make_server_cert(root_key, root_cert, name="not-a-machine")
    cert_path, key_path = tmp_path / "server-as-client.crt", tmp_path / "server-as-client.key"
    write_pem_cert(cert_path, cert)
    write_pem_key(key_path, key)

    with pytest.raises((ssl.SSLError, ConnectionError, OSError)):
        _post(port, ca_path, {}, certfile=cert_path, keyfile=key_path)


def test_live_server_refuses_a_connection_with_no_client_certificate(
        tmp_path, live_registration_server):
    """Required test (0092 Handoff): no peer certificate is refused —
    CERT_REQUIRED fails the handshake itself, before any X-Client-Cert
    header the request carries could ever be read."""
    server, port, root_key, root_cert = live_registration_server
    ca_path = tmp_path / "ca-for-client.crt"
    write_pem_cert(ca_path, root_cert)

    with pytest.raises((ssl.SSLError, ConnectionError, OSError)):
        _post(port, ca_path, {})


def test_a_stalled_handshake_does_not_block_other_registrations_and_times_out(
        tmp_path, tls_material, library_stub, token_file, monkeypatch):
    """Security fix, round 2: the listening socket must never be wrapped in
    TLS (that makes `accept()` itself perform the handshake, inside this
    server's single-threaded accept loop). `REGISTRATION_HOST` defaults to
    `0.0.0.0` — reachable from any machine, no certificate needed — so a
    peer that opens a TCP connection and sends no ClientHello must not be
    able to hold every other registration hostage.

    Confirmed to fail against 485b104a: a throwaway reproduction against
    that commit's `RegistrationServer` (which wraps the listening socket)
    showed a real client's handshake there timing out at the client's own
    5-second socket timeout while an open-but-silent connection sat in the
    accept loop; against the fix below it completes in under a second."""
    ca_path, cert_path, key_path, root_key, root_cert = tls_material
    monkeypatch.setattr(BRG, "TLS_HANDSHAKE_TIMEOUT_S", 0.5)
    ctx = BRG.build_server_ssl_context(trust_anchor_path=ca_path,
                                       server_cert_path=str(cert_path),
                                       server_key_path=str(key_path))
    server = BRG.RegistrationServer(("127.0.0.1", 0), ctx)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # A peer that opens a connection and sends no ClientHello.
        stalled = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            # While that connection is still open, a real client with a
            # valid certificate still registers, well under the stalled
            # connection's own handshake timeout.
            key, cert = make_machine_cert(root_key, root_cert, machine=MACHINE)
            client_cert_path, client_key_path = tmp_path / "m.crt", tmp_path / "m.key"
            write_pem_cert(client_cert_path, cert)
            write_pem_key(client_key_path, key)
            fp = BRG.fingerprint_sha256_der(der(cert))
            stub = library_stub(_json_reply(200, {
                "machine": MACHINE, "fingerprint_sha256": fp, "status": "active",
                "active": True, "custody": "hardware", "not_after": "2099-01-01T00:00:00Z"}))
            monkeypatch.setattr(BRG, "LIBRARY_LOOKUP_BASE_URL", stub.base_url)
            body = {"account": "operator", "caller_kind": "operator", "ceiling": "full",
                    "protocol_version": "1"}

            start = time.monotonic()
            status, payload = _post(port, ca_path, body, certfile=client_cert_path,
                                    keyfile=client_key_path)
            elapsed = time.monotonic() - start

            assert status == 200
            assert payload["session_token"]
            assert elapsed < BRG.TLS_HANDSHAKE_TIMEOUT_S, (
                "the real client waited on the stalled connection's handshake")

            # The stalled connection is then closed after its own handshake
            # times out, affecting only itself.
            stalled.settimeout(5)
            assert stalled.recv(1) == b""
        finally:
            stalled.close()
    finally:
        server.shutdown()
        server.server_close()


def test_a_stalled_handshakes_socket_is_closed_explicitly_not_only_by_garbage_collection(
        tmp_path, tls_material, monkeypatch):
    """Round 3, finding 2: `wrap_socket()` detaches `request`'s file
    descriptor onto the new SSL-wrapped object before any handshake is
    attempted (unconditional on whether the handshake succeeds), so a timed-
    out handshake's `except` branch must close THAT object — closing
    `request` afterward is a no-op on an already-detached, fd-less object.

    Before this fix, the fd was only ever reclaimed because CPython's
    refcounting freed the local `tls_request` once `_handshake_then_serve`
    returned with nothing else referencing it — true in practice, but not a
    guarantee the code made explicitly. This test keeps a second, external
    reference to the socket alive for the whole test (`captured`), so
    refcounting cannot be what closes it: only an explicit `.close()` call
    in the code under test can. Confirmed to fail against 3d7af669: with the
    external reference holding it open, `stalled.recv(1)` never observes
    the old code's GC-driven close and times out at its own bound (5s)
    instead of hanging the suite.
    """
    ca_path, cert_path, key_path, root_key, root_cert = tls_material
    monkeypatch.setattr(BRG, "TLS_HANDSHAKE_TIMEOUT_S", 0.5)
    ctx = BRG.build_server_ssl_context(trust_anchor_path=ca_path,
                                       server_cert_path=str(cert_path),
                                       server_key_path=str(key_path))
    server = BRG.RegistrationServer(("127.0.0.1", 0), ctx)
    port = server.server_address[1]

    captured = []
    real_wrap_socket = server._ssl_context.wrap_socket

    def capturing_wrap_socket(*args, **kwargs):
        tls_request = real_wrap_socket(*args, **kwargs)
        captured.append(tls_request)  # an external reference GC can't see past
        return tls_request

    monkeypatch.setattr(server._ssl_context, "wrap_socket", capturing_wrap_socket)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        stalled = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            stalled.settimeout(5)
            assert stalled.recv(1) == b""
        finally:
            stalled.close()

        assert len(captured) == 1, "expected exactly one handshake attempt"
        assert captured[0].fileno() == -1, (
            "the handshake's socket was not explicitly closed — an external "
            "reference kept it alive, so garbage collection could not have "
            "been what closed it")
    finally:
        server.shutdown()
        server.server_close()


def test_a_silent_post_handshake_connection_is_closed_after_the_request_timeout(
        tmp_path, tls_material, library_stub, token_file, monkeypatch):
    """Round 3, finding 1: `tls_request.settimeout(None)` (3d7af669) cleared
    the handshake's bound entirely before `finish_request` ever ran, and
    nothing re-applied one — a peer presenting any valid machine certificate
    could complete the handshake and then send nothing at all, holding its
    thread and socket forever. Confirmed to fail against 3d7af669, bounded
    so a regression hangs for at most `silent.settimeout(5)`'s 5 seconds
    rather than hanging the suite.
    """
    ca_path, cert_path, key_path, root_key, root_cert = tls_material
    monkeypatch.setattr(BRG, "TLS_REQUEST_TIMEOUT_S", 0.5)
    ctx = BRG.build_server_ssl_context(trust_anchor_path=ca_path,
                                       server_cert_path=str(cert_path),
                                       server_key_path=str(key_path))
    server = BRG.RegistrationServer(("127.0.0.1", 0), ctx)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        key, cert = make_machine_cert(root_key, root_cert, machine=MACHINE)
        cert_path2, key_path2 = tmp_path / "silent.crt", tmp_path / "silent.key"
        write_pem_cert(cert_path2, cert)
        write_pem_key(key_path2, key)
        client_ctx = _client_ssl_context(ca_path, certfile=cert_path2, keyfile=key_path2)
        raw = socket.create_connection(("127.0.0.1", port), timeout=5)
        silent = client_ctx.wrap_socket(raw, server_hostname="macstudio")
        try:
            # The handshake completes — a real, valid machine certificate —
            # and then the client sends nothing at all.

            # While that connection sits open, a second client with its own
            # valid certificate still registers, well under the silent
            # connection's own request timeout.
            key2, cert2 = make_machine_cert(root_key, root_cert, machine=MACHINE)
            cp2, kp2 = tmp_path / "m2.crt", tmp_path / "m2.key"
            write_pem_cert(cp2, cert2)
            write_pem_key(kp2, key2)
            fp2 = BRG.fingerprint_sha256_der(der(cert2))
            stub = library_stub(_json_reply(200, {
                "machine": MACHINE, "fingerprint_sha256": fp2, "status": "active",
                "active": True, "custody": "hardware", "not_after": "2099-01-01T00:00:00Z"}))
            monkeypatch.setattr(BRG, "LIBRARY_LOOKUP_BASE_URL", stub.base_url)
            body = {"account": "operator", "caller_kind": "operator", "ceiling": "full",
                    "protocol_version": "1"}
            start = time.monotonic()
            status, payload = _post(port, ca_path, body, certfile=cp2, keyfile=kp2)
            elapsed = time.monotonic() - start
            assert status == 200
            assert payload["session_token"]
            assert elapsed < BRG.TLS_REQUEST_TIMEOUT_S, (
                "the real client waited on the silent connection's request")

            # The silent connection is then closed once its own request
            # deadline elapses, affecting only itself.
            silent.settimeout(5)
            assert silent.recv(1) == b""
        finally:
            silent.close()
    finally:
        server.shutdown()
        server.server_close()


def test_a_connection_that_stalls_mid_body_is_closed_after_the_request_timeout(
        tmp_path, tls_material, monkeypatch):
    """Round 3, finding 1's second instance: a `Content-Length` header
    followed by a trickle must not reset the request's bound one read at a
    time — `_RequestDeadlineSSLSocket` recomputes the time left before each
    individual read rather than reapplying a constant, so a single byte
    sent just inside the per-call window on every call still can't hold the
    connection past the deadline. Confirmed to fail against 3d7af669,
    bounded by `stalled.settimeout(5)` so a regression fails rather than
    hangs the suite.
    """
    ca_path, cert_path, key_path, root_key, root_cert = tls_material
    monkeypatch.setattr(BRG, "TLS_REQUEST_TIMEOUT_S", 0.5)
    ctx = BRG.build_server_ssl_context(trust_anchor_path=ca_path,
                                       server_cert_path=str(cert_path),
                                       server_key_path=str(key_path))
    server = BRG.RegistrationServer(("127.0.0.1", 0), ctx)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        key, cert = make_machine_cert(root_key, root_cert, machine=MACHINE)
        cp, kp = tmp_path / "trickle.crt", tmp_path / "trickle.key"
        write_pem_cert(cp, cert)
        write_pem_key(kp, key)
        client_ctx = _client_ssl_context(ca_path, certfile=cp, keyfile=kp)
        raw = socket.create_connection(("127.0.0.1", port), timeout=5)
        stalled = client_ctx.wrap_socket(raw, server_hostname="macstudio")
        try:
            # A full request line and headers declaring a 1000-byte body,
            # of which only the first byte ever arrives.
            request_head = (b"POST /sessions HTTP/1.1\r\nHost: macstudio\r\n"
                             b"Content-Length: 1000\r\n\r\n")
            stalled.sendall(request_head + b"x")

            stalled.settimeout(5)
            assert stalled.recv(1) == b""
        finally:
            stalled.close()
    finally:
        server.shutdown()
        server.server_close()


def test_an_authorize_connection_that_stalls_mid_body_is_closed_after_the_request_timeout(
        tmp_path, tls_material, monkeypatch):
    """The merge of #754's round 3 onto this slice: `/authorize` is routed
    through the same `do_POST` as `/sessions` and must get the same request
    deadline, since both run on the one mTLS listener. A valid machine
    certificate completes the handshake, then declares a `Content-Length`
    for its `/authorize` body and sends only the first byte of it — the
    same trickle `test_a_connection_that_stalls_mid_body_is_closed_after_the_request_timeout`
    proves against `/sessions`, here against `/authorize` instead. Bounded
    by `stalled.settimeout(5)` so a regression fails rather than hangs the
    suite.
    """
    ca_path, cert_path, key_path, root_key, root_cert = tls_material
    monkeypatch.setattr(BRG, "TLS_REQUEST_TIMEOUT_S", 0.5)
    ctx = BRG.build_server_ssl_context(trust_anchor_path=ca_path,
                                       server_cert_path=str(cert_path),
                                       server_key_path=str(key_path))
    server = BRG.RegistrationServer(("127.0.0.1", 0), ctx)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        key, cert = make_machine_cert(root_key, root_cert, machine=MACHINE)
        cp, kp = tmp_path / "authz-trickle.crt", tmp_path / "authz-trickle.key"
        write_pem_cert(cp, cert)
        write_pem_key(kp, key)
        client_ctx = _client_ssl_context(ca_path, certfile=cp, keyfile=kp)
        raw = socket.create_connection(("127.0.0.1", port), timeout=5)
        stalled = client_ctx.wrap_socket(raw, server_hostname="macstudio")
        try:
            # A full request line and headers declaring a 1000-byte
            # /authorize body, of which only the first byte ever arrives.
            request_head = (b"POST /authorize HTTP/1.1\r\nHost: macstudio\r\n"
                             b"Content-Length: 1000\r\n\r\n")
            stalled.sendall(request_head + b"x")

            stalled.settimeout(5)
            assert stalled.recv(1) == b""
        finally:
            stalled.close()
    finally:
        server.shutdown()
        server.server_close()


def test_a_response_after_a_near_exhausted_request_deadline_still_delivers_the_session_token(
        tmp_path, tls_material, library_stub, token_file, monkeypatch):
    """Round 4 finding: `_RequestDeadlineSSLSocket.recv_into` shrinks the
    socket's timeout to the time left on the request deadline and, at
    fced6663, never restored it before the response write — so a request
    whose body arrived near the end of `TLS_REQUEST_TIMEOUT_S`'s window
    left the write with almost no timeout of its own, and a client could
    get its connection closed with no `session_token` for a registration
    that had already committed server-side. `TLS_RESPONSE_TIMEOUT_S` now
    gives the write a bound independent of how much of the read deadline
    is left.

    The discriminating check is the timeout actually configured on the
    socket at the moment each response write hits the wire (captured via a
    spy on `_RequestDeadlineSSLSocket.send` itself, the exact call the fix
    sets the timeout in — checking it any earlier, e.g. before `_write_json`
    even starts, would just see whatever the last *read* left behind in
    both the fixed and unfixed code, since nothing proactively re-arms the
    timeout ahead of when a write actually happens). Real loopback writes
    of a response this small usually complete in microseconds regardless of
    the configured timeout, so asserting on the client's receipt alone
    would pass against fced6663 too, by luck rather than by the fix. At
    fced6663 the captured value can never reach `TLS_REQUEST_TIMEOUT_S`
    itself — some nonzero time necessarily elapses between arming the
    deadline and the last read — while `TLS_RESPONSE_TIMEOUT_S` here is set
    well above it. So asserting the captured value is at least
    `TLS_REQUEST_TIMEOUT_S` fails against fced6663 deterministically, by
    construction, regardless of how much of the request deadline this
    test's own sleep happens to leave; it does not rely on a real socket
    timeout actually firing.

    `token_file` (round 6; round 7 made it autouse): this test originally
    omitted the fixture — round 6's commit message claimed every sibling
    test already took it, which was false (round 6 review found two more
    instances, `tests/test_bosun_registration.py:1233` and `:1317`, still
    latent) — so `read_lookup_token()` fell through to the module's real
    default `LOOKUP_TOKEN_PATH` — the invoking account's actual
    `~/.config/bosun/library-machine-verify.token`. On a machine that
    happens to have that file (a dev box that has run the live Bosun), the
    registration still reached 200 by accident; on GitHub's runner, which
    has no such file, `read_lookup_token()` returns `None` and
    `handle_registration` refuses with `lookup_credential_missing` — a 503
    this test's own sleep has nothing to do with (see round 6 PR body for
    the reproduction). This test fails against `3d7af669` with an
    `AttributeError` at `monkeypatch.setattr(BRG, "TLS_REQUEST_TIMEOUT_S",
    ...)`, not on its own assertion: `3d7af669` predates the deadline
    mechanism this test targets, so there is nothing there for the name to
    resolve to.
    """
    ca_path, cert_path, key_path, root_key, root_cert = tls_material
    monkeypatch.setattr(BRG, "TLS_REQUEST_TIMEOUT_S", 1.0)
    monkeypatch.setattr(BRG, "TLS_RESPONSE_TIMEOUT_S", 5.0, raising=False)

    captured_timeout = []
    original_send = BRG._RequestDeadlineSSLSocket.send

    def spying_send(self, *args, **kwargs):
        result = original_send(self, *args, **kwargs)
        captured_timeout.append(self.gettimeout())
        return result

    monkeypatch.setattr(BRG._RequestDeadlineSSLSocket, "send", spying_send)

    ctx = BRG.build_server_ssl_context(trust_anchor_path=ca_path,
                                       server_cert_path=str(cert_path),
                                       server_key_path=str(key_path))
    server = BRG.RegistrationServer(("127.0.0.1", 0), ctx)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        key, cert = make_machine_cert(root_key, root_cert, machine=MACHINE)
        cp, kp = tmp_path / "late-body.crt", tmp_path / "late-body.key"
        write_pem_cert(cp, cert)
        write_pem_key(kp, key)
        fp = BRG.fingerprint_sha256_der(der(cert))
        stub = library_stub(_json_reply(200, {
            "machine": MACHINE, "fingerprint_sha256": fp, "status": "active",
            "active": True, "custody": "hardware", "not_after": "2099-01-01T00:00:00Z"}))
        monkeypatch.setattr(BRG, "LIBRARY_LOOKUP_BASE_URL", stub.base_url)

        client_ctx = _client_ssl_context(ca_path, certfile=cp, keyfile=kp)
        raw = socket.create_connection(("127.0.0.1", port), timeout=5)
        tls = client_ctx.wrap_socket(raw, server_hostname="macstudio")
        try:
            body = {"account": "operator", "caller_kind": "operator", "ceiling": "full",
                    "protocol_version": "1"}
            payload = json.dumps(body).encode()
            request_head = (f"POST /sessions HTTP/1.1\r\nHost: macstudio\r\n"
                             f"Content-Length: {len(payload)}\r\n"
                             f"Connection: close\r\n\r\n").encode()
            # The request line, headers and all but the last byte of the
            # body arrive immediately; the last byte arrives only once most
            # of the request deadline has elapsed.
            tls.sendall(request_head + payload[:-1])
            time.sleep(0.7)
            tls.sendall(payload[-1:])

            resp = b""
            while True:
                chunk = tls.recv(4096)
                if not chunk:
                    break
                resp += chunk
        finally:
            tls.close()
    finally:
        server.shutdown()
        server.server_close()

    head, _, raw_body = resp.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1])
    assert status == 200, f"response body was {raw_body!r}"
    payload_out = json.loads(raw_body)
    assert payload_out["session_token"], (
        "the client never received a session_token for a registration "
        "that in fact succeeded server-side")
    assert captured_timeout, "the response write never went through the overridden send()"
    assert all(t >= BRG.TLS_REQUEST_TIMEOUT_S for t in captured_timeout), (
        f"a response write's configured timeout ({captured_timeout!r}) came from "
        "the near-exhausted request deadline, not from TLS_RESPONSE_TIMEOUT_S")


def test_a_client_that_stops_reading_the_response_is_still_closed_within_the_write_timeout(
        tmp_path, tls_material, library_stub, monkeypatch):
    """Round 4 fix, the security property it must preserve: the round-3
    security review found that a peer which stops reading the response
    cannot hold the thread open, as a side effect of the write inheriting
    the request deadline's leftover timeout. Giving the write its own,
    independent `TLS_RESPONSE_TIMEOUT_S` must not drop that bound — a
    generous *request* deadline here (so that bound alone would never
    explain a close) together with a short response timeout, and a client
    that sends its request and then does not read at all for well past the
    response timeout, confirms the write itself is still bounded.

    The response is padded to tens of megabytes, far past any OS socket
    buffer's default capacity, and the client sleeps rather than reading at
    all. If the write were unbounded, that much idle time would be enough
    for the whole payload to still land in the client's buffers once it
    finally reads; a response cut short instead shows the server actually
    stopped writing partway through, bound by `TLS_RESPONSE_TIMEOUT_S`
    rather than by how much the client happened to buffer.
    """
    ca_path, cert_path, key_path, root_key, root_cert = tls_material
    monkeypatch.setattr(BRG, "TLS_REQUEST_TIMEOUT_S", 30.0)
    monkeypatch.setattr(BRG, "TLS_RESPONSE_TIMEOUT_S", 0.5, raising=False)

    pad_size = 20_000_000
    original_write_json = BRG._write_json

    def padded_write_json(handler, status, payload):
        return original_write_json(handler, status, {**payload, "_pad": "x" * pad_size})

    monkeypatch.setattr(BRG, "_write_json", padded_write_json)

    ctx = BRG.build_server_ssl_context(trust_anchor_path=ca_path,
                                       server_cert_path=str(cert_path),
                                       server_key_path=str(key_path))
    server = BRG.RegistrationServer(("127.0.0.1", 0), ctx)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        key, cert = make_machine_cert(root_key, root_cert, machine=MACHINE)
        cp, kp = tmp_path / "stop-reading.crt", tmp_path / "stop-reading.key"
        write_pem_cert(cp, cert)
        write_pem_key(kp, key)
        fp = BRG.fingerprint_sha256_der(der(cert))
        stub = library_stub(_json_reply(200, {
            "machine": MACHINE, "fingerprint_sha256": fp, "status": "active",
            "active": True, "custody": "hardware", "not_after": "2099-01-01T00:00:00Z"}))
        monkeypatch.setattr(BRG, "LIBRARY_LOOKUP_BASE_URL", stub.base_url)

        client_ctx = _client_ssl_context(ca_path, certfile=cp, keyfile=kp)
        raw = socket.create_connection(("127.0.0.1", port), timeout=5)
        tls = client_ctx.wrap_socket(raw, server_hostname="macstudio")
        try:
            body = {"account": "operator", "caller_kind": "operator", "ceiling": "full",
                    "protocol_version": "1"}
            payload = json.dumps(body).encode()
            request = (f"POST /sessions HTTP/1.1\r\nHost: macstudio\r\n"
                       f"Content-Length: {len(payload)}\r\n"
                       f"Connection: close\r\n\r\n").encode() + payload
            tls.sendall(request)

            # Not reading at all for well past the response timeout, rather
            # than racing to catch the close at the exact moment it happens.
            time.sleep(BRG.TLS_RESPONSE_TIMEOUT_S + 2.0)

            tls.settimeout(5)
            received = b""
            while True:
                chunk = tls.recv(65536)
                if not chunk:
                    break
                received += chunk
            assert len(received) < pad_size, (
                "the full padded response arrived despite the client never reading — "
                "the write was never actually bounded, so this run doesn't exercise "
                "the timeout at all")
        finally:
            tls.close()
    finally:
        server.shutdown()
        server.server_close()


def test_an_authorize_request_whose_body_arrives_late_still_receives_its_full_response(
        tmp_path, tls_material, library_stub, token_file, monkeypatch):
    """The merge of #754's round 4 onto this slice: the response write's
    own timeout (`TLS_RESPONSE_TIMEOUT_S`) must cover `/authorize` the same
    as `/sessions` — `_do_authorize` calls `_write_json` through the exact
    same `wfile.write` → `_RequestDeadlineSSLSocket.send` path `_do_sessions`
    does, with no `/authorize`-specific code in the fix. A session is
    registered first over its own connection; the `/authorize` request that
    follows sends its headers immediately but its body's last byte only
    once most of `TLS_REQUEST_TIMEOUT_S` has elapsed.

    A receipt-only assertion here would not be discriminating:
    `test_a_response_after_a_near_exhausted_request_deadline_still_delivers_the_session_token`
    already found that a response this small, over loopback, completes in
    microseconds regardless of which timeout is configured, so it would
    pass at `fced6663` by luck even with the fix absent (confirmed by
    hand: commenting out the `arm_response_timeout` call that test guards
    still left this test green). That test's own fix is to capture the
    timeout actually configured on the socket at the moment of the write,
    via a spy on `_RequestDeadlineSSLSocket.send`, and this test does the
    same for `/authorize`: at `fced6663` the captured value can never
    reach `TLS_REQUEST_TIMEOUT_S` itself (some nonzero time elapses
    between arming the deadline and the late body byte), while
    `TLS_RESPONSE_TIMEOUT_S` here is set well above it, so asserting the
    captured value is at least `TLS_REQUEST_TIMEOUT_S` fails against
    `fced6663` deterministically rather than by how much of the deadline
    the late byte happens to leave. Round 5 replaced the reapplied per-call
    `settimeout` with a shared `_response_deadline`, so the captured value
    here is the time left on that deadline rather than a reapplied
    constant; for this test's single, unpaced write the two are
    indistinguishable (the deadline has barely moved), so the assertion
    is unchanged.

    `token_file`: the registration this test makes before its `/authorize`
    call goes through `handle_registration`'s real Library-credential
    lookup, the same one round 6 found a sibling test (`test_a_response_
    after_a_near_exhausted_request_deadline_still_delivers_the_session_
    token`) omitting this fixture for — without it, `read_lookup_token()`
    falls through to the real `~/.config/bosun/library-machine-verify.token`
    on whatever machine runs the suite. Confirmed failing the same way
    (`assert 503 == 200` on the registration) with that file moved aside.
    """
    ca_path, cert_path, key_path, root_key, root_cert = tls_material
    monkeypatch.setattr(BRG, "TLS_REQUEST_TIMEOUT_S", 1.0)
    monkeypatch.setattr(BRG, "TLS_RESPONSE_TIMEOUT_S", 5.0, raising=False)

    captured_timeout = []
    original_send = BRG._RequestDeadlineSSLSocket.send

    def spying_send(self, *args, **kwargs):
        result = original_send(self, *args, **kwargs)
        captured_timeout.append(self.gettimeout())
        return result

    monkeypatch.setattr(BRG._RequestDeadlineSSLSocket, "send", spying_send)

    ctx = BRG.build_server_ssl_context(trust_anchor_path=ca_path,
                                       server_cert_path=str(cert_path),
                                       server_key_path=str(key_path))
    server = BRG.RegistrationServer(("127.0.0.1", 0), ctx)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        key, cert = make_machine_cert(root_key, root_cert, machine=MACHINE)
        cp, kp = tmp_path / "authz-late-body.crt", tmp_path / "authz-late-body.key"
        write_pem_cert(cp, cert)
        write_pem_key(kp, key)
        fp = BRG.fingerprint_sha256_der(der(cert))
        stub = library_stub(_json_reply(200, {
            "machine": MACHINE, "fingerprint_sha256": fp, "status": "active",
            "active": True, "custody": "hardware", "not_after": "2099-01-01T00:00:00Z"}))
        monkeypatch.setattr(BRG, "LIBRARY_LOOKUP_BASE_URL", stub.base_url)

        reg_body = {"account": "operator", "caller_kind": "operator", "ceiling": "full",
                    "protocol_version": "1"}
        reg_status, reg_payload = _post(port, ca_path, reg_body, certfile=cp, keyfile=kp)
        assert reg_status == 200, f"registration response was {reg_payload!r}"
        token = reg_payload["session_token"]
        # The registration above is itself one request/response on this
        # server, going through the same spied `send`; only the /authorize
        # write below is the one under test.
        captured_timeout.clear()

        authz_body = {"session_token": token, "tool_name": "Read",
                      "input": {"file_path": "/work/x"}}
        payload = json.dumps(authz_body).encode()
        request_head = (f"POST /authorize HTTP/1.1\r\nHost: macstudio\r\n"
                         f"Content-Length: {len(payload)}\r\n"
                         f"Connection: close\r\n\r\n").encode()

        client_ctx = _client_ssl_context(ca_path, certfile=cp, keyfile=kp)
        raw = socket.create_connection(("127.0.0.1", port), timeout=5)
        tls = client_ctx.wrap_socket(raw, server_hostname="macstudio")
        try:
            # The request line, headers and all but the last byte of the
            # body arrive immediately; the last byte arrives only once most
            # of the request deadline has elapsed.
            tls.sendall(request_head + payload[:-1])
            time.sleep(0.7)
            tls.sendall(payload[-1:])

            tls.settimeout(5)
            resp = b""
            while True:
                chunk = tls.recv(4096)
                if not chunk:
                    break
                resp += chunk
        finally:
            tls.close()
    finally:
        server.shutdown()
        server.server_close()

    head, _, raw_body = resp.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1])
    assert status == 200, (
        f"the /authorize response never arrived intact (status line: {head!r}) — "
        "a response write bound by the near-exhausted request deadline instead of "
        "TLS_RESPONSE_TIMEOUT_S would time out here")
    payload_out = json.loads(raw_body)
    assert payload_out["allow"] is True, (
        "the client never received its allow decision for an /authorize call "
        "that in fact succeeded server-side")
    assert captured_timeout, "the /authorize response write never went through the overridden send()"
    assert all(t >= BRG.TLS_REQUEST_TIMEOUT_S for t in captured_timeout), (
        f"an /authorize response write's configured timeout ({captured_timeout!r}) came "
        "from the near-exhausted request deadline, not from TLS_RESPONSE_TIMEOUT_S")


def test_a_response_written_in_slow_pieces_is_still_closed_within_one_write_timeout(
        tmp_path, tls_material, library_stub, monkeypatch):
    """Round 5 finding: a per-call `settimeout(TLS_RESPONSE_TIMEOUT_S)`
    reapplied on every `send()` bounds only a single call, and `sendall`
    loops over `send` once per partial write — a response delivered as a
    series of individually-quick writes, each well inside
    `TLS_RESPONSE_TIMEOUT_S` of the last, resets that per-call timeout in
    full on every one of them and is never bound at all. The round 4 fix
    (the previous test above) only covers a client that stops reading
    entirely, so the very first blocking call already has nothing to
    reset — it cannot tell a per-call timeout from a real deadline. This
    test is the one that can.

    A real client reading unevenly can't be used to force this
    deterministically: `_write_json`'s one `wfile.write()` call for the
    whole body is, from Python's side, a single atomic `send()` — OpenSSL's
    default (non-partial-write) mode makes one write of one buffer block
    until either the whole thing is sent or the call times out, with no
    second call for a slow reader to reset — and reproducing genuine
    multi-call backpressure through OS socket buffers was observed, on this
    very host, to depend on auto-tuned buffer sizes in a way that isn't
    stable across machines. So this test stands in for "the peer keeps
    reading, just slowly" with the one thing that distinguishes it from a
    full stop: a gap between successive writes. It writes the body as
    `num_pieces` separate `wfile.write()` calls, each one byte, with a real
    `time.sleep(piece_gap_s)` between them — `piece_gap_s` well under
    `TLS_RESPONSE_TIMEOUT_S`, so no single call, old or new code, ever
    blocks anywhere near its own per-call timeout. Whether that gap comes
    from the handler waiting on something or from a slow reader makes no
    difference to `send()`, which only ever sees wall-clock time between
    calls on `self` — so this exercises the exact mechanism the real,
    slow-reading scenario would, without depending on how any particular
    OS paces a fast reader against a tiny buffer.

    `num_pieces * piece_gap_s` is well past `TLS_RESPONSE_TIMEOUT_S`, so
    nothing but a real deadline can close the connection before all of them
    are written — and the client's own read loop is bounded well past
    that, so a regression here fails the margin assertion below rather
    than hanging the suite.
    """
    ca_path, cert_path, key_path, root_key, root_cert = tls_material
    monkeypatch.setattr(BRG, "TLS_REQUEST_TIMEOUT_S", 30.0)
    response_timeout_s = 0.5
    monkeypatch.setattr(BRG, "TLS_RESPONSE_TIMEOUT_S", response_timeout_s, raising=False)

    piece_gap_s = 0.1
    num_pieces = 40  # 40 * 0.1s = 4s if nothing bounds the total

    def paced_write_json(handler, status, payload):
        data = json.dumps(payload).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(data) + num_pieces))
        handler.end_headers()
        handler.wfile.write(data)
        for _ in range(num_pieces):
            time.sleep(piece_gap_s)
            handler.wfile.write(b"x")

    monkeypatch.setattr(BRG, "_write_json", paced_write_json)

    ctx = BRG.build_server_ssl_context(trust_anchor_path=ca_path,
                                       server_cert_path=str(cert_path),
                                       server_key_path=str(key_path))
    server = BRG.RegistrationServer(("127.0.0.1", 0), ctx)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        key, cert = make_machine_cert(root_key, root_cert, machine=MACHINE)
        cp, kp = tmp_path / "paced-write.crt", tmp_path / "paced-write.key"
        write_pem_cert(cp, cert)
        write_pem_key(kp, key)
        fp = BRG.fingerprint_sha256_der(der(cert))
        stub = library_stub(_json_reply(200, {
            "machine": MACHINE, "fingerprint_sha256": fp, "status": "active",
            "active": True, "custody": "hardware", "not_after": "2099-01-01T00:00:00Z"}))
        monkeypatch.setattr(BRG, "LIBRARY_LOOKUP_BASE_URL", stub.base_url)

        client_ctx = _client_ssl_context(ca_path, certfile=cp, keyfile=kp)
        raw = socket.create_connection(("127.0.0.1", port), timeout=5)
        tls = client_ctx.wrap_socket(raw, server_hostname="macstudio")
        try:
            body = {"account": "operator", "caller_kind": "operator", "ceiling": "full",
                    "protocol_version": "1"}
            payload = json.dumps(body).encode()
            request = (f"POST /sessions HTTP/1.1\r\nHost: macstudio\r\n"
                       f"Content-Length: {len(payload)}\r\n"
                       f"Connection: close\r\n\r\n").encode() + payload
            tls.sendall(request)
            start = time.monotonic()

            read_deadline = start + num_pieces * piece_gap_s + 2.0
            tls.settimeout(1.0)
            received = b""
            closed = False
            while time.monotonic() < read_deadline:
                try:
                    chunk = tls.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    closed = True
                    break
                if not chunk:
                    closed = True
                    break
                received += chunk
            elapsed = time.monotonic() - start
        finally:
            tls.close()
    finally:
        server.shutdown()
        server.server_close()

    assert closed, (
        f"the connection was still open after {num_pieces * piece_gap_s + 2.0}s — "
        "the write was never actually bounded by a deadline")
    margin_s = 2.0
    assert elapsed <= response_timeout_s + margin_s, (
        f"the connection took {elapsed:.2f}s to close, more than "
        f"TLS_RESPONSE_TIMEOUT_S ({response_timeout_s}s) plus a {margin_s}s margin — "
        "a per-call timeout reset on every quick, individually-bounded write, rather "
        "than one deadline for the whole response, is what let the paced writes "
        "keep the connection open")


def test_a_successful_registrations_connection_is_closed_by_the_server_after_the_response(
        tmp_path, live_registration_server, library_stub, monkeypatch):
    """Round 3, finding 1's keep-alive instance: HTTP/1.1 defaults to
    keep-alive, so even a fully benign, successfully-registered client that
    never sends `Connection: close` must not be able to hold its thread
    open afterward — `_RegistrationHandler.do_POST` now forces
    `close_connection` regardless of what the client asked for. The read
    loop below only ever exits via the server's own EOF (`if not chunk:
    break`); a server that kept the connection open would instead hang
    until the 5-second socket timeout the `with socket.create_connection`
    below sets, which is this test's bound against a regression hanging the
    suite.
    """
    server, port, root_key, root_cert = live_registration_server
    ca_path = tmp_path / "ca-for-client.crt"
    write_pem_cert(ca_path, root_cert)
    key, cert = make_machine_cert(root_key, root_cert, machine=MACHINE)
    cert_path, key_path = tmp_path / "machine.crt", tmp_path / "machine.key"
    write_pem_cert(cert_path, cert)
    write_pem_key(key_path, key)
    fp = BRG.fingerprint_sha256_der(der(cert))
    stub = library_stub(_json_reply(200, {
        "machine": MACHINE, "fingerprint_sha256": fp, "status": "active",
        "active": True, "custody": "hardware", "not_after": "2099-01-01T00:00:00Z"}))
    monkeypatch.setattr(BRG, "LIBRARY_LOOKUP_BASE_URL", stub.base_url)

    client_ctx = _client_ssl_context(ca_path, certfile=cert_path, keyfile=key_path)
    body = {"account": "operator", "caller_kind": "operator", "ceiling": "full",
            "protocol_version": "1"}
    payload = json.dumps(body).encode()
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        with client_ctx.wrap_socket(sock, server_hostname="macstudio") as tls:
            # No `Connection: close` — an HTTP/1.1 client that wants
            # keep-alive sends nothing special at all.
            request = (f"POST /sessions HTTP/1.1\r\nHost: macstudio\r\n"
                       f"Content-Length: {len(payload)}\r\n\r\n").encode() + payload
            tls.sendall(request)
            resp = b""
            while True:
                chunk = tls.recv(4096)
                if not chunk:
                    break
                resp += chunk
    head, _, raw_body = resp.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1])
    assert status == 200
    assert json.loads(raw_body)["session_token"]


# ============================================================================
# `agentctl bosun revoke-machine` — the operator CLI over the real listener
# ============================================================================


@pytest.fixture
def live_operator_server(monkeypatch):
    server = BRG.OperatorServer(("127.0.0.1", 0), BRG._OperatorHandler)
    monkeypatch.setattr(BRG, "OPERATOR_HOST", "127.0.0.1")
    monkeypatch.setattr(BRG, "OPERATOR_PORT", server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def test_agentctl_bosun_revoke_machine_prints_the_count(
        agentctl, live_operator_server, capsys):
    BRG.register_session(machine="macstudio", fingerprint_sha256="a" * 64, account="operator",
                         caller_kind="operator", ceiling="full", protocol_version="1")
    BRG.register_session(machine="macstudio", fingerprint_sha256="b" * 64, account="operator",
                         caller_kind="agent", ceiling="full", protocol_version="1")

    args = SimpleNamespace(name="macstudio")
    rc = agentctl.cmd_bosun_revoke_machine(args)

    assert rc == 0
    out = capsys.readouterr().out
    assert "ended 2 session(s)" in out
    assert "'macstudio'" in out


def test_agentctl_bosun_revoke_machine_reports_an_unreachable_bosun(agentctl, monkeypatch, capsys):
    monkeypatch.setattr(BRG, "OPERATOR_HOST", "127.0.0.1")
    monkeypatch.setattr(BRG, "OPERATOR_PORT", 1)  # nothing listens on port 1

    rc = agentctl.cmd_bosun_revoke_machine(SimpleNamespace(name="macstudio"))

    assert rc == 1
    assert "could not reach" in capsys.readouterr().err
