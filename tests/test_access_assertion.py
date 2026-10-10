"""access_assertion: the Cloudflare Access assertion verifier.

Real RSA keys and real RS256 signatures made with `openssl`, the way
github_app.py signs. No network: `urllib.request.urlopen` is replaced with a
fake JWKS endpoint, and the config and cache files live under tmp_path.

The negative battery is what makes a second copy of Orient's verifier safe to
hold, so each refusal is its own test.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import stat
import subprocess
import time
import urllib.request

import pytest

import access_assertion as aa

TEAM = "example-team.cloudflareaccess.com"
AUD = "aud-tag-1"
KID = "kid-1"


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _openssl(*args, data=None):
    r = subprocess.run(["openssl", *args], input=data, capture_output=True, check=True)
    return r.stdout


@pytest.fixture(scope="module")
def keypair(tmp_path_factory):
    d = tmp_path_factory.mktemp("rsa")
    pem = d / "key.pem"
    _openssl("genrsa", "-out", str(pem), "2048")
    out = _openssl("rsa", "-in", str(pem), "-noout", "-modulus").decode().strip()
    n = int(out.split("=", 1)[1], 16)
    return pem, n


def sign(pem, signing_input: bytes) -> bytes:
    return _openssl("dgst", "-sha256", "-sign", str(pem), data=signing_input)


def make_token(pem, *, claims=None, header=None, sig=None, mutate=None):
    now = int(time.time())
    c = {"email": "operator@Example.com", "iss": f"https://{TEAM}", "aud": [AUD],
         "exp": now + 3600, "nbf": now - 10}
    c.update(claims or {})
    h = {"alg": "RS256", "kid": KID, "typ": "JWT"}
    h.update(header or {})
    h_b64 = b64u(json.dumps(h).encode())
    p_b64 = b64u(json.dumps(c).encode())
    signing_input = f"{h_b64}.{p_b64}".encode()
    s = sign(pem, signing_input) if sig is None else sig
    if mutate:
        return mutate(h_b64, p_b64, s)
    return f"{h_b64}.{p_b64}.{b64u(s)}"


class FakeResp:
    def __init__(self, body: bytes):
        self.body = body
        self.asked = None

    def read(self, n=-1):
        self.asked = n
        return self.body if n < 0 else self.body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def env(tmp_path, monkeypatch, keypair):
    pem, n = keypair
    cfg = tmp_path / "kb-person.json"
    cfg.write_text(json.dumps({"team_domain": TEAM, "aud": AUD, "persons": ["operator@Example.com"]}))
    monkeypatch.setenv("KB_PERSON_CONFIG", str(cfg))
    cache = tmp_path / "state" / "access-jwks.json"
    monkeypatch.setenv("KB_PERSON_JWKS_CACHE", str(cache))
    jwks = {"keys": [{"kty": "RSA", "kid": KID, "n": b64u(n.to_bytes(256, "big")),
                      "e": b64u((65537).to_bytes(3, "big"))}]}
    state = {"calls": 0, "fail": False, "body": json.dumps(jwks).encode(), "resp": None}

    def fake_urlopen(url, *a, **k):
        assert url == f"https://{TEAM}/cdn-cgi/access/certs"
        state["calls"] += 1
        if state["fail"]:
            raise OSError("unreachable")
        state["resp"] = FakeResp(state["body"])
        return state["resp"]

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    state.update(pem=pem, n=n, cache=cache, cfg=cfg, jwks=jwks)
    return state


def age_cache(env, seconds):
    doc = json.loads(env["cache"].read_text())
    doc["fetched"] = time.time() - seconds
    env["cache"].write_text(json.dumps(doc))


def test_a_valid_token_verifies_and_returns_the_claims(env):
    ok, claims, reason = aa.verify(make_token(env["pem"]))
    assert (ok, reason) == (True, "ok")
    assert claims["email"] == "operator@Example.com"


def test_config_lowercases_and_normalizes(env):
    assert aa.config() == {"team_domain": TEAM, "aud": AUD, "persons": ["operator@example.com"]}


# ---- the negative battery: each must refuse, and for its own reason ----

def refused(env, token, reason):
    ok, _claims, why = aa.verify(token)
    assert ok is False
    assert why == reason


def test_tampered_payload_refused(env):
    def mutate(h, p, s):
        c = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
        c["email"] = "eve@example.com"
        return f"{h}.{b64u(json.dumps(c).encode())}.{b64u(s)}"
    refused(env, make_token(env["pem"], mutate=mutate), "bad-signature")


def test_tampered_signature_refused(env):
    def mutate(h, p, s):
        return f"{h}.{p}.{b64u(s[:-1] + bytes([s[-1] ^ 1]))}"
    refused(env, make_token(env["pem"], mutate=mutate), "bad-signature")


def test_signature_of_the_wrong_length_refused(env):
    good = sign(env["pem"], b"x")
    refused(env, make_token(env["pem"], sig=good[1:]), "bad-signature")
    refused(env, make_token(env["pem"], sig=good + b"\x00"), "bad-signature")


def test_a_valid_signature_with_a_leading_zero_byte_prepended_refused(env):
    # Same integer as a genuine signature, one byte too long: only the length
    # check separates it from a valid one.
    signing_input = b"m"
    good = sign(env["pem"], signing_input)
    assert aa._rsa_pkcs1_verify(env["n"], 65537, signing_input, good)
    assert not aa._rsa_pkcs1_verify(env["n"], 65537, signing_input, b"\x00" + good)


def test_signature_integer_at_or_above_the_modulus_refused(env):
    n = env["n"]
    # Same byte length as the modulus, numerically >= n.
    assert not aa._rsa_pkcs1_verify(n, 65537, b"m", n.to_bytes(256, "big"))
    refused(env, make_token(env["pem"], sig=n.to_bytes(256, "big")), "bad-signature")
    refused(env, make_token(env["pem"], sig=b"\xff" * 256), "bad-signature")


def test_a_genuine_signature_plus_the_modulus_refused(env):
    # s + n is the same residue mod n, so it verifies unless s >= n is refused
    # first. Retry over messages until s + n still fits in the modulus's length.
    n = env["n"]
    for i in range(64):
        msg = f"m{i}".encode()
        good = sign(env["pem"], msg)
        s = int.from_bytes(good, "big")
        if s + n < 1 << 2048:
            assert aa._rsa_pkcs1_verify(n, 65537, msg, good)
            assert not aa._rsa_pkcs1_verify(n, 65537, msg, (s + n).to_bytes(256, "big"))
            return
    pytest.fail("no signature small enough to add the modulus to")


def test_unknown_key_id_refused(env):
    refused(env, make_token(env["pem"], header={"kid": "other"}), "unknown-kid")


def test_alg_none_refused(env):
    def mutate(h, p, s):
        hdr = b64u(json.dumps({"alg": "none", "kid": KID}).encode())
        return f"{hdr}.{p}."
    refused(env, make_token(env["pem"], mutate=mutate), "bad-alg")


def test_hs256_signed_with_the_modulus_as_hmac_key_refused(env):
    def mutate(h, p, s):
        hdr = b64u(json.dumps({"alg": "HS256", "kid": KID}).encode())
        mac = hmac.new(env["n"].to_bytes(256, "big"), f"{hdr}.{p}".encode(), hashlib.sha256)
        return f"{hdr}.{p}.{b64u(mac.digest())}"
    refused(env, make_token(env["pem"], mutate=mutate), "bad-alg")


def test_issuer_of_another_team_refused(env):
    refused(env, make_token(env["pem"], claims={"iss": "https://other.cloudflareaccess.com"}),
            "bad-iss")


def test_audience_of_another_application_refused(env):
    refused(env, make_token(env["pem"], claims={"aud": ["someone-else"]}), "bad-aud")


def test_a_string_audience_matching_the_pin_verifies(env):
    ok, _c, why = aa.verify(make_token(env["pem"], claims={"aud": AUD}))
    assert (ok, why) == (True, "ok")


def test_expired_more_than_leeway_refused(env):
    refused(env, make_token(env["pem"], claims={"exp": int(time.time()) - 120}), "expired")


def test_expired_inside_leeway_still_verifies(env):
    ok, _c, why = aa.verify(make_token(env["pem"], claims={"exp": int(time.time()) - 30}))
    assert (ok, why) == (True, "ok")


def test_not_yet_valid_more_than_leeway_refused(env):
    refused(env, make_token(env["pem"], claims={"nbf": int(time.time()) + 120}), "not-yet-valid")


def test_a_token_without_exp_refused(env):
    def mutate(h, p, s):
        c = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
        del c["exp"]
        p2 = b64u(json.dumps(c).encode())
        return f"{h}.{p2}.{b64u(sign(env['pem'], f'{h}.{p2}'.encode()))}"
    refused(env, make_token(env["pem"], mutate=mutate), "bad-times")


@pytest.mark.parametrize("claim", ["exp", "nbf"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_exp_or_nbf_refused(env, claim, value):
    refused(env, make_token(env["pem"], claims={claim: value}), "bad-times")


def test_a_refusal_carries_no_claims(env):
    def mutate(h, p, s):
        c = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
        c["email"] = "eve@example.com"
        return f"{h}.{b64u(json.dumps(c).encode())}.{b64u(s)}"
    ok, claims, why = aa.verify(make_token(env["pem"], mutate=mutate))
    assert (ok, why) == (False, "bad-signature")
    assert claims == {}


@pytest.mark.parametrize("token", ["", "a.b", "a.b.c.d", "!!.!!.!!"])
def test_malformed_tokens_refused(env, token):
    ok, _c, why = aa.verify(token)
    assert ok is False
    assert why in ("malformed", "undecodable")


# ---- config: fail closed ----

def test_missing_config_is_the_closed_config_and_refuses(env):
    env["cfg"].unlink()
    assert aa.config() == {"team_domain": "", "aud": "", "persons": []}
    refused(env, make_token(env["pem"]), "no-team-domain")


def test_unreadable_config_is_the_closed_config_and_refuses(env):
    env["cfg"].unlink()
    env["cfg"].mkdir()  # reading a directory raises, and works when run as root
    assert aa.config() == {"team_domain": "", "aud": "", "persons": []}
    refused(env, make_token(env["pem"]), "no-team-domain")


def test_non_json_config_is_the_closed_config_and_refuses(env):
    env["cfg"].write_text("not json {")
    assert aa.config() == {"team_domain": "", "aud": "", "persons": []}
    refused(env, make_token(env["pem"]), "no-team-domain")


def test_config_without_an_audience_refuses(env):
    env["cfg"].write_text(json.dumps({"team_domain": TEAM, "persons": []}))
    refused(env, make_token(env["pem"]), "no-aud")


def test_a_team_domain_that_is_not_a_bare_host_refuses(env):
    env["cfg"].write_text(json.dumps({"team_domain": "evil.example/x?", "aud": AUD}))
    refused(env, make_token(env["pem"]), "no-team-domain")
    assert env["calls"] == 0


# ---- the key cache ----

def test_a_key_id_absent_from_a_fresh_cache_refetches(env):
    assert aa.jwks_key(TEAM, KID) is not None
    assert env["calls"] == 1
    assert aa.jwks_key(TEAM, "rotated") is None
    assert env["calls"] == 2


def test_a_key_id_present_inside_the_ttl_does_not_refetch(env):
    assert aa.jwks_key(TEAM, KID) is not None
    assert aa.jwks_key(TEAM, KID) is not None
    assert aa.verify(make_token(env["pem"]))[0] is True
    assert env["calls"] == 1


def test_a_cache_past_the_ttl_refetches(env):
    aa.jwks_key(TEAM, KID)
    age_cache(env, aa.CACHE_TTL_S + 5)
    aa.jwks_key(TEAM, KID)
    assert env["calls"] == 2


def test_a_rotated_in_key_is_found_after_the_refetch(env):
    aa.jwks_key(TEAM, KID)
    env["jwks"]["keys"].append({"kty": "RSA", "kid": "new", "n": env["jwks"]["keys"][0]["n"],
                                "e": env["jwks"]["keys"][0]["e"]})
    env["body"] = json.dumps(env["jwks"]).encode()
    assert aa.jwks_key(TEAM, "new") is not None


def test_fetch_failure_uses_a_cached_key_inside_the_ceiling(env):
    aa.jwks_key(TEAM, KID)
    age_cache(env, aa.STALE_CEILING_S - 3600)
    env["fail"] = True
    ok, _c, why = aa.verify(make_token(env["pem"]))
    assert (ok, why) == (True, "ok")


def test_fetch_failure_refuses_a_cache_past_the_ceiling(env):
    aa.jwks_key(TEAM, KID)
    age_cache(env, aa.STALE_CEILING_S + 3600)
    env["fail"] = True
    refused(env, make_token(env["pem"]), "jwks-unavailable")


def test_fetch_failure_with_no_matching_cached_key_refuses(env):
    aa.jwks_key(TEAM, KID)
    env["fail"] = True
    refused(env, make_token(env["pem"], header={"kid": "other"}), "jwks-unavailable")


def test_fetch_failure_with_an_empty_cache_refuses(env):
    env["fail"] = True
    refused(env, make_token(env["pem"]), "jwks-unavailable")


def test_a_key_revoked_upstream_is_not_served_from_the_cache(env):
    # The case the ceiling exists to bound: Cloudflare is reachable and no longer
    # lists KID, so the cached copy must not be used.
    assert aa.verify(make_token(env["pem"]))[0] is True
    age_cache(env, aa.CACHE_TTL_S + 5)
    env["jwks"]["keys"][0]["kid"] = "replacement"
    env["body"] = json.dumps(env["jwks"]).encode()
    refused(env, make_token(env["pem"]), "unknown-kid")


def test_the_ceiling_is_one_day():
    assert aa.STALE_CEILING_S == 24 * 3600


def test_a_cache_stamped_in_the_future_is_not_trusted_on_fetch_failure(env):
    aa.jwks_key(TEAM, KID)
    age_cache(env, -10 * 24 * 3600)
    env["fail"] = True
    refused(env, make_token(env["pem"]), "jwks-unavailable")


def test_a_cache_for_another_url_is_ignored(env):
    aa.jwks_key(TEAM, KID)
    doc = json.loads(env["cache"].read_text())
    doc["url"] = "https://other.cloudflareaccess.com/cdn-cgi/access/certs"
    env["cache"].write_text(json.dumps(doc))
    env["fail"] = True
    assert aa.jwks_key(TEAM, KID) is None


def test_the_cache_file_is_written_mode_0600(env):
    aa.jwks_key(TEAM, KID)
    assert stat.S_IMODE(os.stat(env["cache"]).st_mode) == 0o600


def test_the_cache_is_replaced_at_0600_even_if_a_loose_file_existed(env):
    env["cache"].parent.mkdir(parents=True)
    env["cache"].write_text("{}")
    os.chmod(env["cache"], 0o644)
    aa.jwks_key(TEAM, KID)
    assert stat.S_IMODE(os.stat(env["cache"]).st_mode) == 0o600


def test_an_oversized_response_is_refused_rather_than_read(env):
    env["body"] = b"x" * (aa.MAX_JWKS_BYTES * 4)
    assert aa.jwks_key(TEAM, KID) is None
    assert env["resp"].asked == aa.MAX_JWKS_BYTES + 1
    assert not env["cache"].exists()
    refused(env, make_token(env["pem"]), "jwks-unavailable")


def test_a_response_one_byte_over_the_cap_is_refused_even_though_it_parses(env):
    env["body"] = env["body"] + b" " * (aa.MAX_JWKS_BYTES + 1 - len(env["body"]))
    assert len(env["body"]) == aa.MAX_JWKS_BYTES + 1
    assert aa.jwks_key(TEAM, KID) is None
    assert not env["cache"].exists()


def test_a_response_exactly_at_the_cap_is_accepted(env):
    env["body"] = env["body"] + b" " * (aa.MAX_JWKS_BYTES - len(env["body"]))
    assert aa.jwks_key(TEAM, KID) is not None


def test_an_alg_carrying_control_text_is_refused_with_the_fixed_code(env):
    # The header is unverified, and the reason is printed and journaled by the cage.
    evil = "RS256\n\x1b[31mINJECTED\x1b[0m"

    def mutate(h, p, s):
        hdr = b64u(json.dumps({"alg": evil, "kid": KID}).encode())
        return f"{hdr}.{p}.{b64u(s)}"
    ok, _claims, why = aa.verify(make_token(env["pem"], mutate=mutate))
    assert (ok, why) == (False, "bad-alg")
    assert "INJECTED" not in why and "\n" not in why and "\x1b" not in why
