"""Verify a Deck-signed knowledge save (library-protocol.md §2.2, the second
`--provenance person` path beside access_assertion.py).

Deck's editor signs each save with a Touch-ID-gated Secure Enclave key. The
Library's root certifies the key as a signing certificate naming the person
and holds a registry answering whether it is still active; the signature
itself never passes through the Library. `agentctl kb apply` is the verifier:
it rebuilds the signed envelope from the write it is about to make, checks the
certificate's chain, purpose and validity, asks the registry, and records the
certificate's person only once every check passes.

Two phases, because a signature covers the write's own canonical path and the
exact bytes about to land — neither of which `kb apply` knows until after the
cage worktree is open and the edit is applied:

  `verify()` does everything that needs only the stdin object and the clock:
  shape, the `--edits` refusal, the `signed_at` window, and the certificate's
  profile, chain and validity. It returns a `pending` closure rather than a
  person, so nothing here is trusted as a person's write yet.

  `pending(document, base_sha, content_sha256)`, called once those three are
  final, builds the envelope, verifies the signature over it, and — only once
  that verifies — asks the registry. The signature is what proves the
  certificate signed THIS save; asking the registry before that would spend a
  network call, and a `person` credit, on an assertion nothing has proven yet.

`build_envelope` always takes its four values from `kb apply`'s own state
(the canonical path it resolved, the `--base-sha` it is holding the write to,
the sha256 of the exact bytes it is about to write, and the `signed_at` string
that traveled with the save) — never from a claim inside the signed object,
which carries no such fields to begin with. That is what makes "wrong
document", "stale base" and "bytes that differ from the hash" collapse into
the same outcome: the envelope `kb apply` builds does not match what was
signed, so the signature fails to verify.

Every refusal is a fixed reason code, in the same spirit as
`access_assertion.verify`'s: never certificate or token text.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import SignatureAlgorithmOID

import signal_client
import vault_plane

# The Library's trust anchor. THE SAME FILE the Library's own mTLS listener
# reads (its `mtls.py` module's `LIBRARY_TLS_CA`, default
# `~/.library/tls/ca.crt`) — this is the identical root certificate, and an
# operator who relocates it need only set the one variable for both readers
# rather than learning a second name for the same file. It is a public
# certificate, not a secret: reading it needs no credential.
ROOT_CA_PATH = Path(os.environ.get(
    "LIBRARY_TLS_CA", str(Path.home() / ".library" / "tls" / "ca.crt")))

# library-protocol.md §2.2: the signing-only extended-key-usage purpose, on the
# UUID arc of ITU-T X.667 (fa47bfa2-1f8a-4d3d-8e88-d3cab3caa98d).
SIGNING_PURPOSE_OID = x509.ObjectIdentifier("2.25.332679538808142797592760871621889796493")

# The replay window: a `signed_at` this far in the past, or this far in the
# future, is refused.
BEFORE_WINDOW_S = 300
AFTER_WINDOW_S = 60

REGISTRY_TIMEOUT_S = 5

_REQUIRED_KEYS = frozenset({"proof", "signed_at", "signature", "certificate"})
_SIGNED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_SIGNER_URI_RE = re.compile(r"^convivy://signer/([a-z0-9][a-z0-9-]{0,63})$")

PendingVerifier = Callable[[str, str, str], "tuple[str | None, str]"]


def build_envelope(document: str, base_sha: str, content_sha256: str, signed_at: str) -> bytes:
    """The exact bytes a signature covers (library-protocol.md §2.2).

    Raises ValueError on a document path containing `\\n` or `\\r`, before
    building anything — such a path could smuggle extra envelope lines.
    """
    if "\n" in document or "\r" in document:
        raise ValueError("document path cannot be signed: contains a line break")
    return (
        "convivy-kb-save/1\n"
        f"document:{document}\n"
        f"base-sha:{base_sha}\n"
        f"content-sha256:{content_sha256}\n"
        f"signed-at:{signed_at}\n"
    ).encode("utf-8")


def _parse_signed_at(raw: str) -> datetime | None:
    if not isinstance(raw, str) or not _SIGNED_AT_RE.match(raw):
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _signed_at_in_window(signed_at: datetime, now: datetime) -> bool:
    delta = (now - signed_at).total_seconds()
    return -AFTER_WINDOW_S <= delta <= BEFORE_WINDOW_S


def parse_stdin(raw: str) -> tuple[dict | None, str]:
    """The stdin object's four required fields, or `(None, "malformed")`.

    Exactly `{proof, signed_at, signature, certificate}` — an extra or missing
    key, a non-string value, or `proof` not `"deck-signature"` all refuse the
    same way, since none of them is a claim worth naming to the caller.
    """
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return None, "malformed"
    if not isinstance(obj, dict) or set(obj) != _REQUIRED_KEYS:
        return None, "malformed"
    if obj.get("proof") != "deck-signature":
        return None, "malformed"
    for key in ("signed_at", "signature", "certificate"):
        if not isinstance(obj[key], str) or not obj[key]:
            return None, "malformed"
    return obj, ""


def _load_certificate(pem: str) -> x509.Certificate | None:
    try:
        return x509.load_pem_x509_certificate(pem.encode("utf-8"))
    except (ValueError, TypeError, UnicodeEncodeError):
        return None


def _load_root() -> x509.Certificate | None:
    try:
        return x509.load_pem_x509_certificate(ROOT_CA_PATH.read_bytes())
    except (OSError, ValueError):
        return None


def _is_p256_ec_key(public_key) -> bool:
    return (isinstance(public_key, ec.EllipticCurvePublicKey)
            and public_key.curve.name == "secp256r1")


def _san_email_and_machine(cert: x509.Certificate) -> tuple[str, str] | None:
    """The certificate's `(email, machine)`, or None.

    Exactly one `rfc822Name` and one `uniformResourceIdentifier` matching
    `convivy://signer/<machine>` — a certificate carrying any other name, or
    either of these twice, is refused (library-protocol.md §2.2).
    """
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return None
    names = list(san)
    if len(names) != 2:
        return None
    emails = [n.value for n in names if isinstance(n, x509.RFC822Name)]
    uris = [n.value for n in names if isinstance(n, x509.UniformResourceIdentifier)]
    if len(emails) != 1 or len(uris) != 1:
        return None
    email = emails[0].strip().lower()
    if not email or any(c.isspace() for c in email):
        return None
    m = _SIGNER_URI_RE.match(uris[0])
    if not m:
        return None
    return email, m.group(1)


def _has_signing_purpose_only(cert: x509.Certificate) -> bool:
    try:
        eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
    except x509.ExtensionNotFound:
        return False
    return eku.critical and list(eku.value) == [SIGNING_PURPOSE_OID]


def _has_signing_key_usage_only(cert: x509.Certificate) -> bool:
    try:
        ext = cert.extensions.get_extension_for_class(x509.KeyUsage)
    except x509.ExtensionNotFound:
        return False
    if not ext.critical:
        return False
    ku = ext.value
    return (ku.digital_signature and not ku.content_commitment and not ku.key_encipherment
            and not ku.data_encipherment and not ku.key_agreement
            and not ku.key_cert_sign and not ku.crl_sign)


def _is_leaf(cert: x509.Certificate) -> bool:
    try:
        return cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False
    except x509.ExtensionNotFound:
        return False


def _chains_to_root(cert: x509.Certificate, root: x509.Certificate) -> bool:
    """Whether `root` signed `cert` directly — no intermediate, as the root-ca
    ceremony issues it (library-protocol.md §2.2)."""
    if cert.issuer != root.subject:
        return False
    root_key = root.public_key()
    if not _is_p256_ec_key(root_key):
        return False
    try:
        root_key.verify(cert.signature, cert.tbs_certificate_bytes,
                        ec.ECDSA(cert.signature_hash_algorithm))
        return True
    except InvalidSignature:
        return False


def _validate_certificate(cert: x509.Certificate, *, root: x509.Certificate,
                          signed_at: datetime, now: datetime) -> tuple[str | None, str]:
    """`(email, "")` once the certificate's key, profile, chain and validity
    period all check out, or `(None, reason)`.

    Does NOT verify the save's signature: that needs the envelope, which needs
    the write's own canonical path and final bytes, not known yet.
    """
    if not _is_p256_ec_key(cert.public_key()):
        return None, "bad-certificate"
    if cert.signature_algorithm_oid != SignatureAlgorithmOID.ECDSA_WITH_SHA256:
        return None, "bad-certificate"
    if not _is_leaf(cert):
        return None, "bad-certificate"
    if not _has_signing_purpose_only(cert):
        return None, "wrong-purpose"
    if _san_email_and_machine(cert) is None:
        return None, "bad-san"
    if not _has_signing_key_usage_only(cert):
        return None, "bad-certificate"
    if not _chains_to_root(cert, root):
        return None, "chain-untrusted"
    not_before, not_after = cert.not_valid_before_utc, cert.not_valid_after_utc
    if not (not_before <= signed_at <= not_after) or not (not_before <= now <= not_after):
        return None, "cert-expired"
    email, _machine = _san_email_and_machine(cert)
    return email, ""


def _verify_signature(cert: x509.Certificate, envelope: bytes, signature_b64: str) -> bool:
    try:
        signature = base64.b64decode(signature_b64, validate=True)
    except (ValueError, TypeError):
        return False
    try:
        cert.public_key().verify(signature, envelope, ec.ECDSA(hashes.SHA256()))
        return True
    except InvalidSignature:
        return False


def fingerprint_sha256(cert: x509.Certificate) -> str:
    """The SHA-256 of the certificate's DER, in 64 lowercase hex characters —
    the registry's key (library-protocol.md §2.2)."""
    return hashlib.sha256(cert.public_bytes(Encoding.DER)).hexdigest()


def registry_active(fingerprint: str, *, timeout: float = REGISTRY_TIMEOUT_S) -> tuple[bool, str]:
    """Whether the Library's registry answers this fingerprint as active.

    Returns `(True, "")` only on a 200 body carrying `"active": true`.
    `"library-unreachable"` is a connection failure or timeout — the Library
    was never reached; `"not-active"` covers everything else a reachable
    Library can answer (revoked, expired, unknown, a malformed body, or a
    Library too old to serve the route at all, which 404s).
    """
    tok = signal_client.token()
    if not tok:
        return False, "library-unreachable"
    req = urllib.request.Request(
        vault_plane.library_url(f"/signing-certificates/{fingerprint}"),
        headers=vault_plane.library_headers(tok))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return False, "not-active"
            body = json.loads(resp.read())
    except urllib.error.HTTPError:
        return False, "not-active"
    except (urllib.error.URLError, OSError, TimeoutError):
        return False, "library-unreachable"
    except ValueError:
        return False, "not-active"
    if not isinstance(body, dict) or body.get("active") is not True:
        return False, "not-active"
    return True, ""


def verify(raw: str, *, edits_requested: bool,
           now: datetime | None = None) -> tuple[PendingVerifier | None, str]:
    """Phase 1: everything checkable from the stdin object and the clock alone.

    Returns `(None, reason)` on any refusal, or `(pending, "")` once the
    shape, the `--edits` rule, the `signed_at` window and the certificate
    itself (profile, chain, validity) all check out. `pending(document,
    base_sha, content_sha256)` is phase 2: it builds the real envelope,
    verifies the signature over it, and only then asks the registry —
    returning `(email, "")` or `(None, reason)`.
    """
    now = now or datetime.now(timezone.utc)
    obj, reason = parse_stdin(raw)
    if obj is None:
        return None, reason
    if edits_requested:
        return None, "edits-not-allowed"
    signed_at = _parse_signed_at(obj["signed_at"])
    if signed_at is None:
        return None, "bad-signed-at"
    if not _signed_at_in_window(signed_at, now):
        return None, "signed-at-window"
    cert = _load_certificate(obj["certificate"])
    if cert is None:
        return None, "bad-certificate"
    root = _load_root()
    if root is None:
        return None, "root-ca-unavailable"
    email, reason = _validate_certificate(cert, root=root, signed_at=signed_at, now=now)
    if email is None:
        return None, reason
    signature_b64 = obj["signature"]
    signed_at_raw = obj["signed_at"]
    fingerprint = fingerprint_sha256(cert)

    def pending(document: str, base_sha: str, content_sha256: str) -> tuple[str | None, str]:
        try:
            envelope = build_envelope(document, base_sha, content_sha256, signed_at_raw)
        except ValueError:
            return None, "bad-document"
        if not _verify_signature(cert, envelope, signature_b64):
            return None, "bad-signature"
        active, why = registry_active(fingerprint)
        if not active:
            return None, why
        return email, ""

    return pending, ""
