"""The shared GitHub App primitives (`lib/agent-platform/github_app.py`),
extracted from `kb_merge.py` so `bin/pr-state-capture` can authenticate as a
different App without duplicating the JWT/token code. `test_kb_merge.py`
still exercises the same primitives through kb_merge's thin wrappers — this
file is the direct coverage of the shared implementation itself.
"""
from __future__ import annotations

import base64
import io
import json
import subprocess
import urllib.error

import pytest

import github_app as GA

HEAD = "a" * 40


def _unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


@pytest.fixture()
def rsa_key(tmp_path):
    key = tmp_path / "app.pem"
    subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"], check=True,
                   capture_output=True)
    key.chmod(0o600)
    pub = tmp_path / "app.pub"
    subprocess.run(["openssl", "rsa", "-in", str(key), "-pubout", "-out", str(pub)],
                   check=True, capture_output=True)
    return key, pub


def test_the_app_jwt_verifies_against_the_keys_public_half(rsa_key, tmp_path):
    key, pub = rsa_key
    token = GA.app_jwt(key, "4666673", now=1_000_000)
    header, claims, sig = token.split(".")
    assert json.loads(_unb64(header)) == {"alg": "RS256", "typ": "JWT"}
    assert json.loads(_unb64(claims)) == {"iat": 999_940, "exp": 1_000_540, "iss": "4666673"}
    (tmp_path / "sig").write_bytes(_unb64(sig))
    r = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(pub),
                        "-signature", str(tmp_path / "sig")],
                       input=f"{header}.{claims}".encode(), capture_output=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_app_jwt_signs_by_absolute_openssl_path_not_through_path(rsa_key, monkeypatch):
    """Round 3 of #732, finding 2: at head e9c10f2f `app_jwt` ran the bare
    `"openssl"`, resolved through this process's own `PATH` — a property of
    `/etc/sudoers`' `secure_path` once this module runs as root under `sudo`
    (`bin/review-launch`), not of this module itself. Breaking `PATH`
    entirely must not break signing once the lookup is gone."""
    key, _ = rsa_key
    monkeypatch.setenv("PATH", "/nonexistent-dir-should-never-resolve-anything")
    token = GA.app_jwt(key, "4666673", now=1_000_000)
    assert token.count(".") == 2  # header.claims.signature — it actually signed


def test_a_key_others_can_read_is_refused(rsa_key):
    key, _ = rsa_key
    assert GA.key_problem(key) is None
    key.chmod(0o644)
    assert "must be 600" in GA.key_problem(key)


def test_a_missing_key_names_the_label_a_caller_supplies(tmp_path):
    assert "convivy-bosun App" in GA.key_problem(tmp_path / "nope.pem", label="the convivy-bosun App")
    assert "the App" in GA.key_problem(tmp_path / "nope.pem")  # default label


def test_a_symlinked_key_is_refused(rsa_key, tmp_path):
    key, _ = rsa_key
    link = tmp_path / "link.pem"
    link.symlink_to(key)
    assert "not a regular file" in GA.key_problem(link)


class _Resp:
    def __init__(self, status, payload):
        self.status = status
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_installation_token_unscoped_when_neither_repos_nor_permissions_given():
    seen = {}

    def opener(req, timeout=None):
        seen.update(url=req.full_url, method=req.get_method(),
                    auth=req.get_header("Authorization"), body=req.data)
        return _Resp(201, {"token": "ghs_listing"})

    assert GA.installation_token("jwt.x.y", "161212648", opener=opener) == "ghs_listing"
    assert seen["url"].endswith("/app/installations/161212648/access_tokens")
    assert seen["method"] == "POST" and seen["auth"] == "Bearer jwt.x.y"
    assert seen["body"] is None


def test_installation_token_scoped_when_repos_and_permissions_given():
    seen = {}

    def opener(req, timeout=None):
        seen.update(body=json.loads(req.data))
        return _Resp(201, {"token": "ghs_x"})

    tok = GA.installation_token("jwt.x.y", "1", repositories=["knowledge"],
                                permissions={"contents": "write"}, opener=opener)
    assert tok == "ghs_x"
    assert seen["body"] == {"repositories": ["knowledge"], "permissions": {"contents": "write"}}


def test_a_refused_installation_token_names_githubs_message():
    def opener(req, timeout=None):
        return _Resp(403, {"message": "Resource not accessible by integration"})

    with pytest.raises(GA.Unreadable) as e:
        GA.installation_token("jwt.x.y", "1", opener=opener)
    assert "403" in str(e.value) and "Resource not accessible" in str(e.value)


def _routes(table):
    seen = []

    def opener(req, timeout=None):
        seen.append((req.get_method(), req.full_url, req.get_header("Authorization"),
                     json.loads(req.data) if req.data else None))
        for (method, frag), resp in table.items():
            if req.get_method() == method and frag in req.full_url:
                return _Resp(*resp)
        raise AssertionError(f"unexpected {req.get_method()} {req.full_url}")
    return opener, seen


ONE_INSTALL = [{"id": 161212648, "account": {"login": "convivy"}}]


def test_single_installation_returns_the_one_installation_owned_by_owner():
    opener, seen = _routes({("GET", "/app/installations"): (200, ONE_INSTALL)})
    inst = GA.single_installation("jwt.x.y", "convivy", opener=opener)
    assert inst == ONE_INSTALL[0]
    assert seen == [("GET", seen[0][1], "Bearer jwt.x.y", None)]


@pytest.mark.parametrize("installs, named", [
    (ONE_INSTALL * 2, "count"),
    ([], "count"),
    ([{"id": 1, "account": {"login": "someone-else"}}], "owner"),
])
def test_single_installation_refuses_by_name(installs, named):
    opener, _ = _routes({("GET", "/app/installations"): (200, installs)})
    with pytest.raises(GA.Unreadable) as e:
        GA.single_installation("jwt.x.y", "convivy", opener=opener)
    assert str(e.value).startswith(named + ":"), e.value


def test_installation_for_owner_reads_the_org_endpoint_directly():
    opener, seen = _routes({("GET", "/orgs/convivybuilder/installation"):
                            (200, {"id": 7, "account": {"login": "convivybuilder"}})})
    inst = GA.installation_for_owner("jwt.x.y", "convivybuilder", opener=opener)
    assert inst == {"id": 7, "account": {"login": "convivybuilder"}}
    assert seen == [("GET", seen[0][1], "Bearer jwt.x.y", None)]
    assert "/orgs/convivybuilder/installation" in seen[0][1]


def test_installation_for_owner_falls_back_to_the_user_endpoint_on_404():
    opener, seen = _routes({
        ("GET", "/orgs/someuser/installation"): (404, {"message": "Not Found"}),
        ("GET", "/users/someuser/installation"): (200, {"id": 9, "account": {"login": "someuser"}}),
    })
    inst = GA.installation_for_owner("jwt.x.y", "someuser", opener=opener)
    assert inst == {"id": 9, "account": {"login": "someuser"}}
    assert len(seen) == 2


def test_installation_for_owner_raises_not_installed_when_both_lookups_404():
    """Both the org and the user endpoint answering 404 is GitHub's own
    confirmation there is no installation — `GA.NotInstalled`, not a plain
    `GA.Unreadable`, so `bin/pr-state-capture` can skip the owner rather
    than fail the pass (operator's decision, 2026-10-15)."""
    opener, _ = _routes({
        ("GET", "/orgs/nobody/installation"): (404, {"message": "Not Found"}),
        ("GET", "/users/nobody/installation"): (404, {"message": "Not Found"}),
    })
    with pytest.raises(GA.NotInstalled) as e:
        GA.installation_for_owner("jwt.x.y", "nobody", label="the convivy-bosun App",
                                  opener=opener)
    assert "the convivy-bosun App is not installed on 'nobody'" in str(e.value)
    assert isinstance(e.value, GA.Unreadable)  # every other caller still catches it as one


def test_installation_for_owner_names_a_non_404_failure_without_guessing_user():
    opener, seen = _routes({("GET", "/orgs/convivy/installation"): (500, {})})
    with pytest.raises(GA.Unreadable) as e:
        GA.installation_for_owner("jwt.x.y", "convivy", opener=opener)
    assert "could not be read (500)" in str(e.value)
    assert not isinstance(e.value, GA.NotInstalled)  # a failed read, not a confirmed absence
    assert len(seen) == 1  # never falls back to /users on a non-404


def test_installation_for_owner_a_non_404_on_the_second_lookup_is_unreadable_not_not_installed():
    """A 404 on the org endpoint followed by a non-404 on the user endpoint
    (a GitHub 500, say) must not be read as "not installed" — that would let
    a transient GitHub failure be skipped under the new owner-skip rule as
    though it were a confirmed absence."""
    opener, seen = _routes({
        ("GET", "/orgs/someuser/installation"): (404, {"message": "Not Found"}),
        ("GET", "/users/someuser/installation"): (500, {}),
    })
    with pytest.raises(GA.Unreadable) as e:
        GA.installation_for_owner("jwt.x.y", "someuser", opener=opener)
    assert "could not be read (500)" in str(e.value)
    assert not isinstance(e.value, GA.NotInstalled)
    assert len(seen) == 2


def test_installations_list_read_failure_is_unreadable():
    def opener(req, timeout=None):
        return _Resp(500, {})

    with pytest.raises(GA.Unreadable):
        GA.installations("jwt.x.y", opener=opener)


def test_request_reports_an_http_error_as_a_status_and_payload():
    def opener(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 409, "Conflict", {},
                                     io.BytesIO(b'{"message": "Head branch was modified."}'))

    code, payload = GA.request("PUT", "https://api.github.com/x", "tok", {"a": 1}, opener=opener)
    assert code == 409 and payload == {"message": "Head branch was modified."}
