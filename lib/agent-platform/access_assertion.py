"""Verify a Cloudflare Access login assertion, the proof that a person acted.

The `person` provenance ("a person typed this") is honoured only when the write
arrives with an Access assertion this module verifies: the RS256 signature
against the team's published keys, the issuer, the pinned audience, and expiry.
Nothing calls `verify()` yet; the cage that will is a later slice.

Configuration is agentctl's own file, `~/.config/convivy/kb-person.json`
(`KB_PERSON_CONFIG` overrides it), holding `team_domain`, `aud` and `persons`.
It is deliberately not Orient's `jwt.json`: that file decides who may reach the
editor and carries an `enforce` switch, while this one decides whose assertion
authorizes a direct write to main, and always enforces.

The signing keys are cached at `~/.local/state/kb-person/access-jwks.json`
(`KB_PERSON_JWKS_CACHE` overrides it), mode 0600. agentctl runs once per write,
so an in-process cache would never hit; the file is what makes the common
verification a local operation.

When Cloudflare cannot be reached, a cached key matching the token's key id is
used only while the cache is under STALE_CEILING_S old. Past that, or with no
matching key, verification fails. There is no other fallback.

Stdlib only, like `agentctl`. The RSA verify is a copy of Orient's
`access_identity._rsa_pkcs1_verify` (RFC 8017 section 8.2.2); the negative
battery in tests/test_access_assertion.py is what makes holding a second copy
safe.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import re
import time
import urllib.request
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "convivy" / "kb-person.json"
JWKS_CACHE_PATH = Path.home() / ".local" / "state" / "kb-person" / "access-jwks.json"

# A cached key set is trusted without a fetch for ten minutes, as Orient's is.
CACHE_TTL_S = 600
# The most an unreachable Cloudflare can extend a cached key's life (the operator,
# 2026-09-23). Longer means a revoked signing key keeps verifying here for that
# long; shorter means an outage blocks saves sooner.
STALE_CEILING_S = 24 * 3600
FETCH_TIMEOUT_S = 5
# A real JWKS is a few KB. Read one byte past the cap to tell "too big" from "exact".
MAX_JWKS_BYTES = 256 * 1024
LEEWAY_S = 60

# DER prefix for the SHA-256 DigestInfo in EMSA-PKCS1-v1_5 (RFC 8017 section 9.2).
_SHA256_DIGESTINFO = bytes.fromhex("3031300d060960864801650304020105000420")
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


def _path(env: str, default: Path) -> Path:
    return Path(os.environ[env]) if os.environ.get(env) else default


def config() -> dict:
    """`{team_domain, aud, persons}`, or the fail-closed empty config.

    A missing, unreadable or malformed file yields empty `team_domain` and `aud`,
    which `verify()` refuses outright. Refusing is the only safe reading: the
    alternative, treating a config that could not be read as "nothing pinned",
    would accept some other team's tokens (the incident behind Orient's own
    `jwt_config` fail-closed default, issue_01M1F1H8RAQD2JGWQGDGW10M0B).
    """
    closed = {"team_domain": "", "aud": "", "persons": []}
    try:
        c = json.loads(_path("KB_PERSON_CONFIG", CONFIG_PATH).read_text())
        if not isinstance(c, dict):
            return closed
        persons = c.get("persons") or []
        return {
            "team_domain": str(c.get("team_domain") or "").strip().lower(),
            "aud": str(c.get("aud") or "").strip(),
            "persons": [str(p).strip().lower() for p in persons
                        if isinstance(p, str) and p.strip()],
        }
    except Exception:
        return closed


def _b64url(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _read_cache(url: str) -> dict | None:
    """The on-disk key cache for `url`, or None if absent, malformed or for another URL."""
    try:
        doc = json.loads(_path("KB_PERSON_JWKS_CACHE", JWKS_CACHE_PATH).read_text())
        if doc["url"] != url:
            return None
        keys = {kid: (int(v["n"], 16), int(v["e"], 16)) for kid, v in doc["keys"].items()}
        return {"fetched": float(doc["fetched"]), "keys": keys}
    except Exception:
        return None


def _write_cache(url: str, fetched: float, keys: dict) -> None:
    """Replace the cache atomically, mode 0600. Best effort: a cache that cannot
    be written costs the next call a fetch and changes no verification result."""
    path = _path("KB_PERSON_JWKS_CACHE", JWKS_CACHE_PATH)
    doc = {"url": url, "fetched": fetched,
           "keys": {kid: {"n": format(n, "x"), "e": format(e, "x")}
                    for kid, (n, e) in keys.items()}}
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(doc, f)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def _fetch_keys(url: str) -> dict:
    """Fetch and parse the team's key set. Raises on any failure, including a
    response larger than MAX_JWKS_BYTES, which is refused rather than read."""
    with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_S) as r:
        raw = r.read(MAX_JWKS_BYTES + 1)
    if len(raw) > MAX_JWKS_BYTES:
        raise ValueError("JWKS response too large")
    keys = {}
    for k in json.loads(raw).get("keys", []):
        if k.get("kty") == "RSA" and k.get("kid"):
            keys[k["kid"]] = (int.from_bytes(_b64url(k["n"]), "big"),
                              int.from_bytes(_b64url(k["e"]), "big"))
    return keys


def _lookup(team_domain: str, kid: str) -> tuple[tuple[int, int] | None, str]:
    """`((n, e), "")` for `kid`, or `(None, reason)`."""
    url = f"https://{team_domain}/cdn-cgi/access/certs"
    now = time.time()
    cache = _read_cache(url)
    if cache and 0 <= now - cache["fetched"] <= CACHE_TTL_S and kid in cache["keys"]:
        return cache["keys"][kid], ""
    try:
        keys = _fetch_keys(url)
    except Exception:
        # A public key stays valid while Cloudflare is unreachable, up to the
        # ceiling. A cache stamped in the future is refused, or its age would
        # never reach the ceiling.
        if cache and kid in cache["keys"] and -LEEWAY_S <= now - cache["fetched"] <= STALE_CEILING_S:
            return cache["keys"][kid], ""
        return None, "jwks-unavailable"
    _write_cache(url, now, keys)
    return (keys[kid], "") if kid in keys else (None, "unknown-kid")


def jwks_key(team_domain: str, kid: str) -> tuple[int, int] | None:
    """The `(n, e)` RSA key for `kid` in the team's key set, or None.

    Served from the on-disk cache inside CACHE_TTL_S. A key id the cache lacks
    forces a refetch, which is how key rotation arrives. If the fetch fails a
    matching cached key is used while the cache is under STALE_CEILING_S old.
    """
    return _lookup(team_domain, kid)[0]


def _rsa_pkcs1_verify(n: int, e: int, msg: bytes, sig: bytes) -> bool:
    """RSASSA-PKCS1-v1_5 verify with SHA-256 (RFC 8017 section 8.2.2)."""
    k = (n.bit_length() + 7) // 8
    if len(sig) != k:
        return False
    s = int.from_bytes(sig, "big")
    if s >= n:
        return False
    em = pow(s, e, n).to_bytes(k, "big")
    t = _SHA256_DIGESTINFO + hashlib.sha256(msg).digest()
    if k < len(t) + 11:
        return False
    expected = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return hmac.compare_digest(em, expected)


def verify(token: str) -> tuple[bool, dict, str]:
    """Verify an Access assertion. Returns `(ok, claims, reason)`.

    Checks the RS256 signature against the team's keys, `iss`, `aud` (which must
    be configured and must be among the token's), and `exp`/`nbf` with 60 seconds
    of leeway. `claims` is the verified payload when `ok` and `{}` on every
    refusal, so a caller that reads `claims["email"]` without checking `ok` finds
    nothing rather than an unsigned value. `reason` is always one of a fixed set
    of codes, never text taken from the token. The `persons` allowlist is the
    caller's check.
    """
    ok, claims, reason = _verify(token)
    return (True, claims, reason) if ok else (False, {}, reason)


def _verify(token: str) -> tuple[bool, dict, str]:
    cfg = config()
    team_domain, aud = cfg["team_domain"], cfg["aud"]
    if not team_domain or not _HOST_RE.match(team_domain):
        return False, {}, "no-team-domain"
    if not aud:
        return False, {}, "no-aud"
    if not isinstance(token, str):
        return False, {}, "malformed"
    parts = token.strip().split(".")
    if len(parts) != 3:
        return False, {}, "malformed"
    h_b64, p_b64, s_b64 = parts
    try:
        header = json.loads(_b64url(h_b64))
        claims = json.loads(_b64url(p_b64))
        sig = _b64url(s_b64)
    except Exception:
        return False, {}, "undecodable"
    if not isinstance(header, dict) or not isinstance(claims, dict):
        return False, {}, "undecodable"
    if header.get("alg") != "RS256":
        # A fixed code: the header is unverified caller text, and the reason
        # reaches stderr and the journal.
        return False, claims, "bad-alg"
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        return False, claims, "unknown-kid"
    key, why = _lookup(team_domain, kid)
    if not key:
        return False, claims, why
    if not _rsa_pkcs1_verify(key[0], key[1], f"{h_b64}.{p_b64}".encode("ascii"), sig):
        return False, claims, "bad-signature"
    if claims.get("iss") != f"https://{team_domain}":
        return False, claims, "bad-iss"
    try:
        exp = float(claims["exp"])
        nbf = float(claims.get("nbf", 0))
    except (KeyError, TypeError, ValueError):
        return False, claims, "bad-times"
    # NaN and infinity make every comparison below False, so the expiry check
    # would silently not run.
    if not (math.isfinite(exp) and math.isfinite(nbf)):
        return False, claims, "bad-times"
    now = time.time()
    if now > exp + LEEWAY_S:
        return False, claims, "expired"
    if now < nbf - LEEWAY_S:
        return False, claims, "not-yet-valid"
    token_aud = claims.get("aud", [])
    token_aud = [token_aud] if isinstance(token_aud, str) else token_aud
    if not isinstance(token_aud, list) or aud not in token_aud:
        return False, claims, "bad-aud"
    return True, claims, "ok"
