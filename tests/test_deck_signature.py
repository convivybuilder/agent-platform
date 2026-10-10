"""deck_signature — verifying a Deck-signed knowledge save (library-protocol.md
§2.2), the second `--provenance person` path beside access_assertion.py.

Throwaway EC P-256 keys and certificates, built in-process with `cryptography`
— no `openssl` subprocess, no network. `urllib.request.urlopen` is replaced
per test for the registry call; `deck_signature.ROOT_CA_PATH` is repointed at
a tmp file for the chain check. Every refusal is its own test, the same shape
as tests/test_access_assertion.py's negative battery.
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

import deck_signature as ds

EMAIL = "operator@example.com"
MACHINE = "macbook"
DOCUMENT = "projects/platform/reference/seeded.md"
BASE_SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
CONTENT_SHA = "a" * 64


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


def make_signing_cert(root_key, root_cert, *, email=EMAIL, machine=MACHINE,
                      eku=(ds.SIGNING_PURPOSE_OID,), key_usage_signing=True,
                      ca=False, not_before=None, not_after=None, san=None,
                      key=None, wrong_key_for_sig=False):
    """A signing certificate the root issues, or one deliberately off-spec —
    every keyword lets a test break exactly one rule."""
    key = key or ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    not_before = not_before or (now - timedelta(days=1))
    not_after = not_after or (now + timedelta(days=365))
    if san is None:
        san = x509.SubjectAlternativeName([
            x509.RFC822Name(email),
            x509.UniformResourceIdentifier(f"convivy://signer/{machine}"),
        ])
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(email))
        .issuer_name(root_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(san, critical=False)
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    if eku is not None:
        builder = builder.add_extension(x509.ExtendedKeyUsage(eku), critical=True)
    if key_usage_signing:
        builder = builder.add_extension(
            x509.KeyUsage(digital_signature=True, content_commitment=False,
                          key_encipherment=False, data_encipherment=False,
                          key_agreement=False, key_cert_sign=False, crl_sign=False,
                          encipher_only=False, decipher_only=False),
            critical=True)
    signer_key = root_key
    cert = builder.sign(signer_key, hashes.SHA256())
    return key, cert


def make_machine_cert(root_key, root_cert, *, machine=MACHINE):
    """A §2.1 machine certificate — `clientAuth`, a `convivy://machine/` URI,
    no signing purpose. Must never verify as a signer."""
    key = ec.generate_private_key(ec.SECP256R1())
    san = x509.SubjectAlternativeName(
        [x509.UniformResourceIdentifier(f"convivy://machine/{machine}")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(machine)).issuer_name(root_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=365))
        .add_extension(san, critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=True)
        .sign(root_key, hashes.SHA256())
    )
    return key, cert


def pem(cert: x509.Certificate) -> str:
    return cert.public_bytes(Encoding.PEM).decode()


def signed_at_str(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def stdin_object(cert, *, signed_at=None, signature=None, extra=None) -> str:
    signed_at = signed_at or signed_at_str(datetime.now(timezone.utc))
    obj = {"proof": "deck-signature", "signed_at": signed_at,
           "signature": signature if signature is not None else "AA==",
           "certificate": pem(cert)}
    if extra:
        obj.update(extra)
    return json.dumps(obj)


def sign(key, envelope: bytes) -> str:
    return base64.b64encode(key.sign(envelope, ec.ECDSA(hashes.SHA256()))).decode()


@pytest.fixture
def root(tmp_path, monkeypatch):
    key, cert = make_root()
    path = tmp_path / "ca.crt"
    path.write_bytes(cert.public_bytes(Encoding.PEM))
    monkeypatch.setattr(ds, "ROOT_CA_PATH", path)
    return key, cert


@pytest.fixture
def registry_stub(monkeypatch):
    """Replaces the registry's HTTP call. `state["active"]` decides the
    answer; `state["raise"]`, when set, is raised instead of connecting."""
    monkeypatch.setattr(ds.signal_client, "token", lambda: "test-token")
    state = {"active": True, "raise": None, "requests": []}

    def fake_urlopen(req, timeout=None):
        state["requests"].append(req)
        if state["raise"] is not None:
            raise state["raise"]
        return _FakeResp(200, {"fingerprint_sha256": "x", "status": "active",
                               "active": state["active"]})

    monkeypatch.setattr(ds.urllib.request, "urlopen", fake_urlopen)
    return state


class _FakeResp:
    def __init__(self, status, body):
        self.status = status
        self._body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _sign_and_verify(root, cert, key, *, signed_at=None, edits_requested=False, now=None):
    """`verify()` an envelope this `key` actually signed for
    (DOCUMENT, BASE_SHA, CONTENT_SHA, signed_at), then run `pending`. Returns
    `(email, reason)`."""
    signed_at = signed_at or signed_at_str(now or datetime.now(timezone.utc))
    envelope = ds.build_envelope(DOCUMENT, BASE_SHA, CONTENT_SHA, signed_at)
    raw = stdin_object(cert, signed_at=signed_at, signature=sign(key, envelope))
    pending, reason = ds.verify(raw, edits_requested=edits_requested, now=now)
    if pending is None:
        return None, reason
    return pending(DOCUMENT, BASE_SHA, CONTENT_SHA)


# --- the envelope --------------------------------------------------------


def test_build_envelope_matches_the_wire_format():
    envelope = ds.build_envelope("projects/x.md", "ab" * 32, "cd" * 32,
                                  "2026-09-27T19:45:00Z")
    assert envelope == (
        b"convivy-kb-save/1\n"
        b"document:projects/x.md\n"
        b"base-sha:" + b"ab" * 32 + b"\n"
        b"content-sha256:" + b"cd" * 32 + b"\n"
        b"signed-at:2026-09-27T19:45:00Z\n"
    )


@pytest.mark.parametrize("bad", ["projects/x\n.md", "projects/x\r.md"])
def test_build_envelope_refuses_a_document_with_a_line_break(bad):
    with pytest.raises(ValueError):
        ds.build_envelope(bad, BASE_SHA, CONTENT_SHA, "2026-09-27T19:45:00Z")


# --- the accepted case ----------------------------------------------------


def test_the_accepted_case_records_the_certificates_person(root, registry_stub):
    root_key, root_cert = root
    key, cert = make_signing_cert(root_key, root_cert)
    email, reason = _sign_and_verify(root, cert, key)
    assert (email, reason) == (EMAIL, "")


def test_the_registry_is_asked_with_the_protocol_header_and_the_agent_credential(
        root, registry_stub):
    root_key, root_cert = root
    key, cert = make_signing_cert(root_key, root_cert)
    _sign_and_verify(root, cert, key)
    assert len(registry_stub["requests"]) == 1
    req = registry_stub["requests"][0]
    assert req.headers.get("Library-protocol") == "1"
    assert req.headers.get("Authorization") == "Bearer test-token"
    assert req.full_url.endswith(f"/signing-certificates/{ds.fingerprint_sha256(cert)}")


# --- malformed stdin -------------------------------------------------------


@pytest.mark.parametrize("raw", [
    "not json", "[]", "{}", '{"proof": "deck-signature"}',
    json.dumps({"proof": "deck-signature", "signed_at": "x", "signature": "y",
                "certificate": "z", "extra": "nope"}),
    json.dumps({"proof": "access", "signed_at": "x", "signature": "y", "certificate": "z"}),
    json.dumps({"proof": "deck-signature", "signed_at": 1, "signature": "y", "certificate": "z"}),
])
def test_a_malformed_stdin_object_is_refused(raw):
    pending, reason = ds.verify(raw, edits_requested=False)
    assert pending is None
    assert reason == "malformed"


# --- whole-file only --------------------------------------------------------


def test_edits_are_refused_before_any_certificate_work(root, registry_stub):
    root_key, root_cert = root
    key, cert = make_signing_cert(root_key, root_cert)
    raw = stdin_object(cert, signature=sign(key, b"whatever"))
    pending, reason = ds.verify(raw, edits_requested=True)
    assert (pending, reason) == (None, "edits-not-allowed")
    assert registry_stub["requests"] == []


# --- signed_at -------------------------------------------------------------


def test_a_malformed_signed_at_is_refused(root):
    root_key, root_cert = root
    key, cert = make_signing_cert(root_key, root_cert)
    raw = stdin_object(cert, signed_at="2026-09-27 19:45:00", signature=sign(key, b"x"))
    pending, reason = ds.verify(raw, edits_requested=False)
    assert (pending, reason) == (None, "bad-signed-at")


@pytest.mark.parametrize("offset_seconds,ok", [
    (-300, True), (-301, False),   # 300s in the past: boundary in, one past it out
    (60, True), (61, False),       # 60s in the future: boundary in, one past it out
    (0, True),
])
def test_signed_at_window_edges(root, registry_stub, offset_seconds, ok):
    """`signed_at = now + offset_seconds`: negative is in the past, positive in
    the future, matching library-protocol.md §2.2's 300s-past/60s-future window."""
    root_key, root_cert = root
    key, cert = make_signing_cert(root_key, root_cert)
    # Whole seconds: `signed_at` travels as a whole-second string, so a `now`
    # carrying microseconds would round-trip inexactly and flake at the edges.
    now = datetime.now(timezone.utc).replace(microsecond=0)
    signed_at = now + timedelta(seconds=offset_seconds)
    email, reason = _sign_and_verify(root, cert, key, signed_at=signed_at_str(signed_at), now=now)
    if ok:
        assert (email, reason) == (EMAIL, "")
    else:
        assert email is None
        assert reason == "signed-at-window"


# --- the certificate --------------------------------------------------------


def test_a_certificate_that_does_not_parse_is_refused(root):
    raw = json.dumps({"proof": "deck-signature",
                      "signed_at": signed_at_str(datetime.now(timezone.utc)),
                      "signature": "AA==", "certificate": "not a certificate"})
    pending, reason = ds.verify(raw, edits_requested=False)
    assert (pending, reason) == (None, "bad-certificate")


def test_a_machine_certificate_is_refused_as_a_signer(root):
    root_key, root_cert = root
    key, cert = make_machine_cert(root_key, root_cert)
    raw = stdin_object(cert, signature=sign(key, b"x"))
    pending, reason = ds.verify(raw, edits_requested=False)
    assert (pending, reason) == (None, "wrong-purpose")


def test_a_certificate_with_both_signing_and_client_auth_purpose_is_refused(root):
    root_key, root_cert = root
    key, cert = make_signing_cert(
        root_key, root_cert, eku=(ds.SIGNING_PURPOSE_OID, ExtendedKeyUsageOID.CLIENT_AUTH))
    raw = stdin_object(cert, signature=sign(key, b"x"))
    pending, reason = ds.verify(raw, edits_requested=False)
    assert (pending, reason) == (None, "wrong-purpose")


@pytest.mark.parametrize("san", [
    x509.SubjectAlternativeName([x509.RFC822Name(EMAIL)]),
    x509.SubjectAlternativeName([x509.UniformResourceIdentifier(f"convivy://signer/{MACHINE}")]),
    x509.SubjectAlternativeName([x509.RFC822Name(EMAIL), x509.RFC822Name("eve@example.com")]),
    x509.SubjectAlternativeName([x509.RFC822Name(EMAIL),
                                 x509.UniformResourceIdentifier(f"convivy://machine/{MACHINE}")]),
])
def test_a_certificate_with_the_wrong_san_shape_is_refused(root, san):
    root_key, root_cert = root
    key, cert = make_signing_cert(root_key, root_cert, san=san)
    raw = stdin_object(cert, signature=sign(key, b"x"))
    pending, reason = ds.verify(raw, edits_requested=False)
    assert (pending, reason) == (None, "bad-san")


def test_a_chain_to_another_root_is_refused(root, registry_stub):
    other_key, other_cert = make_root()
    key, cert = make_signing_cert(other_key, other_cert)
    raw = stdin_object(cert, signature=sign(key, b"x"))
    pending, reason = ds.verify(raw, edits_requested=False)
    assert (pending, reason) == (None, "chain-untrusted")
    assert registry_stub["requests"] == []


def test_an_unavailable_root_ca_file_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "ROOT_CA_PATH", tmp_path / "does-not-exist.crt")
    key, cert = make_root()  # a self-signed cert stands in; never reached
    raw = stdin_object(cert, signature=sign(key, b"x"))
    pending, reason = ds.verify(raw, edits_requested=False)
    assert (pending, reason) == (None, "root-ca-unavailable")


@pytest.mark.parametrize("case", ["not_yet_valid", "expired"])
def test_a_certificate_not_valid_at_signed_at_or_now_is_refused(root, case):
    """`signed_at` stays inside the replay window regardless — this isolates
    the certificate's OWN validity period from the window check above."""
    root_key, root_cert = root
    now = datetime.now(timezone.utc)
    if case == "not_yet_valid":
        not_before, not_after = now + timedelta(days=1), now + timedelta(days=365)
    else:
        not_before, not_after = now - timedelta(days=400), now - timedelta(days=1)
    signed_at = now
    key, cert = make_signing_cert(root_key, root_cert, not_before=not_before,
                                  not_after=not_after)
    envelope = ds.build_envelope(DOCUMENT, BASE_SHA, CONTENT_SHA, signed_at_str(signed_at))
    raw = stdin_object(cert, signed_at=signed_at_str(signed_at), signature=sign(key, envelope))
    pending, reason = ds.verify(raw, edits_requested=False, now=now)
    assert (pending, reason) == (None, "cert-expired")


# --- the signature, and what it collapses ----------------------------------


def test_a_forged_signature_is_refused(root, registry_stub):
    root_key, root_cert = root
    key, cert = make_signing_cert(root_key, root_cert)
    signed_at = signed_at_str(datetime.now(timezone.utc))
    envelope = ds.build_envelope(DOCUMENT, BASE_SHA, CONTENT_SHA, signed_at)
    sig = sign(key, envelope)
    tampered = sig[:-4] + ("AAAA" if sig[-4:] != "AAAA" else "BBBB")
    raw = stdin_object(cert, signed_at=signed_at, signature=tampered)
    pending, reason = ds.verify(raw, edits_requested=False)
    assert pending is not None
    email, reason = pending(DOCUMENT, BASE_SHA, CONTENT_SHA)
    assert (email, reason) == (None, "bad-signature")
    assert registry_stub["requests"] == []


def test_a_signature_for_a_different_document_is_refused(root, registry_stub):
    root_key, root_cert = root
    key, cert = make_signing_cert(root_key, root_cert)
    email, reason = _sign_and_verify(root, cert, key)  # signed for DOCUMENT
    assert (email, reason) == (EMAIL, "")
    # A fresh envelope, signed for a DIFFERENT document, verified against the
    # write kb apply is actually about to make.
    signed_at = signed_at_str(datetime.now(timezone.utc))
    wrong_envelope = ds.build_envelope("projects/other.md", BASE_SHA, CONTENT_SHA, signed_at)
    raw = stdin_object(cert, signed_at=signed_at, signature=sign(key, wrong_envelope))
    pending, reason = ds.verify(raw, edits_requested=False)
    assert pending is not None
    email, reason = pending(DOCUMENT, BASE_SHA, CONTENT_SHA)
    assert (email, reason) == (None, "bad-signature")


def test_a_signature_for_different_bytes_is_refused(root, registry_stub):
    root_key, root_cert = root
    key, cert = make_signing_cert(root_key, root_cert)
    signed_at = signed_at_str(datetime.now(timezone.utc))
    signed_for_other_bytes = ds.build_envelope(DOCUMENT, BASE_SHA, "b" * 64, signed_at)
    raw = stdin_object(cert, signed_at=signed_at, signature=sign(key, signed_for_other_bytes))
    pending, reason = ds.verify(raw, edits_requested=False)
    assert pending is not None
    email, reason = pending(DOCUMENT, BASE_SHA, CONTENT_SHA)  # the ACTUAL bytes' hash
    assert (email, reason) == (None, "bad-signature")


def test_a_signature_for_an_older_base_is_refused(root, registry_stub):
    root_key, root_cert = root
    key, cert = make_signing_cert(root_key, root_cert)
    signed_at = signed_at_str(datetime.now(timezone.utc))
    signed_for_old_base = ds.build_envelope(DOCUMENT, "old" + "0" * 61, CONTENT_SHA, signed_at)
    raw = stdin_object(cert, signed_at=signed_at, signature=sign(key, signed_for_old_base))
    pending, reason = ds.verify(raw, edits_requested=False)
    assert pending is not None
    email, reason = pending(DOCUMENT, BASE_SHA, CONTENT_SHA)  # the CURRENT base
    assert (email, reason) == (None, "bad-signature")


# --- the registry ------------------------------------------------------------


def test_a_revoked_certificate_is_refused(root, registry_stub):
    registry_stub["active"] = False
    root_key, root_cert = root
    key, cert = make_signing_cert(root_key, root_cert)
    email, reason = _sign_and_verify(root, cert, key)
    assert (email, reason) == (None, "not-active")


def test_an_unreachable_library_is_refused(root, registry_stub):
    registry_stub["raise"] = OSError("connection refused")
    root_key, root_cert = root
    key, cert = make_signing_cert(root_key, root_cert)
    email, reason = _sign_and_verify(root, cert, key)
    assert (email, reason) == (None, "library-unreachable")


def test_a_library_too_old_to_serve_the_route_answers_404_and_is_not_active(
        root, registry_stub):
    registry_stub["raise"] = urllib.error.HTTPError(
        "http://127.0.0.1:8100/x", 404, "not found", {}, None)
    root_key, root_cert = root
    key, cert = make_signing_cert(root_key, root_cert)
    email, reason = _sign_and_verify(root, cert, key)
    assert (email, reason) == (None, "not-active")


def test_no_agent_credential_is_library_unreachable(root, monkeypatch):
    monkeypatch.setattr(ds.signal_client, "token", lambda: None)
    root_key, root_cert = root
    key, cert = make_signing_cert(root_key, root_cert)
    email, reason = _sign_and_verify(root, cert, key)
    assert (email, reason) == (None, "library-unreachable")
