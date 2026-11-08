"""The shared GitHub App primitives (`lib/agent-platform/github_app.py`),
extracted from `kb_merge.py` so `bin/pr-state-capture` can authenticate as a
different App without duplicating the JWT/token code. `test_kb_merge.py`
still exercises the same primitives through kb_merge's thin wrappers — this
file is the direct coverage of the shared implementation itself.
"""
from __future__ import annotations

import base64
import http.client
import io
import json
import os
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
    token = GA.app_jwt(key, "555555", now=1_000_000)
    header, claims, sig = token.split(".")
    assert json.loads(_unb64(header)) == {"alg": "RS256", "typ": "JWT"}
    assert json.loads(_unb64(claims)) == {"iat": 999_940, "exp": 1_000_540, "iss": "555555"}
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
    token = GA.app_jwt(key, "555555", now=1_000_000)
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


def test_graphql_returns_the_data_object_on_a_clean_200():
    seen = {}

    def opener(req, timeout=None):
        seen.update(url=req.full_url, method=req.get_method(),
                    auth=req.get_header("Authorization"), body=json.loads(req.data))
        return _Resp(200, {"data": {"repository": {"pullRequests": {"nodes": []}}}})

    data = GA.graphql("query { x }", {"owner": "convivy", "name": "knowledge"},
                      "tok", opener=opener)
    assert data == {"repository": {"pullRequests": {"nodes": []}}}
    assert seen["url"] == f"{GA.API}/graphql"
    assert seen["method"] == "POST" and seen["auth"] == "Bearer tok"
    assert seen["body"] == {"query": "query { x }",
                            "variables": {"owner": "convivy", "name": "knowledge"}}


def test_graphql_raises_unreadable_on_a_non_200():
    def opener(req, timeout=None):
        return _Resp(401, {"message": "Bad credentials"})

    with pytest.raises(GA.Unreadable) as e:
        GA.graphql("query { x }", {}, "tok", opener=opener)
    assert "401" in str(e.value) and "Bad credentials" in str(e.value)


def test_graphql_raises_unreadable_on_an_errors_array():
    """GitHub answers GraphQL partial failures with a 200 and a top-level
    `errors` array (a field it could not resolve) rather than a non-200 —
    `request` would read that as success, so `graphql` must check for it
    itself."""
    def opener(req, timeout=None):
        return _Resp(200, {"data": None, "errors": [{"message": "Could not resolve to a Repository"}]})

    with pytest.raises(GA.Unreadable) as e:
        GA.graphql("query { x }", {}, "tok", opener=opener)
    assert "Could not resolve to a Repository" in str(e.value)


def test_graphql_raises_unreadable_when_data_is_missing():
    def opener(req, timeout=None):
        return _Resp(200, {})

    with pytest.raises(GA.Unreadable):
        GA.graphql("query { x }", {}, "tok", opener=opener)


class _TruncatedResp:
    """A 200 whose body GitHub cut off mid-read — `resp.read()` raises
    `http.client.IncompleteRead`, the shape a truncated pull-list page takes
    on the real job's error log (issue_01M6D8HW0TRFRDPF1RG9JKV11Z)."""

    status = 200

    def read(self):
        raise http.client.IncompleteRead(b"partial", 100)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_request_raises_connectionerror_on_a_truncated_success_body():
    """Red against main at e9c10f2f: `request` only caught `urllib.error.
    HTTPError`, so `resp.read()` raising `http.client.IncompleteRead` on the
    success path propagated as that bare `HTTPException`, never an
    `OSError` — which is why `except OSError` around `GACT.capture_repository`
    in `bin/github-activity-capture` missed it (see that script's module
    docstring, "A TRANSPORT ERROR" section)."""
    def opener(req, timeout=None):
        return _TruncatedResp()

    with pytest.raises(ConnectionError) as e:
        GA.request("GET", "https://api.github.com/repos/x/y/pulls", "tok", None, opener=opener)
    assert isinstance(e.value, OSError)
    assert "https://api.github.com/repos/x/y/pulls" in str(e.value)
    assert isinstance(e.value.__cause__, http.client.IncompleteRead)


def test_request_raises_connectionerror_on_a_truncated_http_error_body():
    """The same truncation, but on an `HTTPError`'s own `e.read()` — a
    truncated body is not special to 2xx responses."""
    class _TruncatedHTTPError(urllib.error.HTTPError):
        def read(self, *a, **k):
            raise http.client.IncompleteRead(b"partial", 50)

    def opener(req, timeout=None):
        raise _TruncatedHTTPError(req.full_url, 409, "Conflict", {}, None)

    with pytest.raises(ConnectionError) as e:
        GA.request("GET", "https://api.github.com/repos/x/y/pulls", "tok", None, opener=opener)
    assert isinstance(e.value, OSError)
    assert isinstance(e.value.__cause__, http.client.IncompleteRead)


# --- push_env/scrub (E10 S2, platform/specs/github-credential-seam) --------


def test_push_env_scopes_the_authorization_header_to_the_given_url():
    env = GA.push_env("ghs_tok", "https://github.com/convivy/agent-platform.git", base={})
    count = int(env["GIT_CONFIG_COUNT"])
    pairs = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(count)}
    assert pairs["credential.helper"] == ""
    basic = base64.b64encode(b"x-access-token:ghs_tok").decode()
    assert pairs["http.https://github.com/convivy/agent-platform.git.extraHeader"] == \
        f"Authorization: Basic {basic}"
    assert env["GIT_ALLOW_PROTOCOL"] == "https"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_ASKPASS"] == ""
    assert env["SSH_ASKPASS"] == ""


def test_push_env_removes_every_inherited_git_trace_variable():
    """A trace would print the Authorization header, token included, to stderr
    or a configured trace file — removing it is the mechanism, not a style
    choice (see the module's own `push_env` docstring)."""
    base = {"GIT_TRACE": "1", "GIT_TRACE_PACKET": "2", "GIT_TRACE_CURL": "3",
           "PATH": "/usr/bin"}
    env = GA.push_env("ghs_tok", "https://github.com/x/y.git", base=base)
    assert not any(k.startswith("GIT_TRACE") for k in env)
    assert env["PATH"] == "/usr/bin"


def test_push_env_appends_after_any_pre_existing_git_config_pairs():
    """`base` may already carry `GIT_CONFIG_*` pairs of its own (a dispatched
    run's own object-store redirect, for one) — `push_env` must append after
    them, not start back at index 0 and overwrite one."""
    base = {"GIT_CONFIG_COUNT": "1",
           "GIT_CONFIG_KEY_0": "safe.directory",
           "GIT_CONFIG_VALUE_0": "*"}
    env = GA.push_env("ghs_tok", "https://github.com/x/y.git", base=base)
    assert env["GIT_CONFIG_COUNT"] == "3"
    assert env["GIT_CONFIG_KEY_0"] == "safe.directory"
    assert env["GIT_CONFIG_VALUE_0"] == "*"
    assert env["GIT_CONFIG_KEY_1"] == "credential.helper"
    assert env["GIT_CONFIG_VALUE_1"] == ""
    assert env["GIT_CONFIG_KEY_2"] == "http.https://github.com/x/y.git.extraHeader"


def test_push_env_does_not_mutate_the_base_dict_or_os_environ(monkeypatch):
    base = {"GIT_CONFIG_COUNT": "0"}
    before = dict(base)
    GA.push_env("ghs_tok", "https://github.com/x/y.git", base=base)
    assert base == before

    monkeypatch.setenv("PUSH_ENV_SENTINEL", "1")
    os_before = dict(os.environ)
    GA.push_env("ghs_tok", "https://github.com/x/y.git")
    assert dict(os.environ) == os_before


# --- installed_app(): the install's own GitHub App, never a literal ---------
# (issue_01M69GD1WEN67MZPSBQZD02CDN)


def _write_app_files(directory, *, app_id="1234567", slug="test-app", key=True):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "app-id").write_text(f"{app_id}\n")
    if slug is not None:
        (directory / "app-slug").write_text(f"{slug}\n")
    if key:
        (directory / "app.pem").write_text("placeholder-key")


def test_installed_app_reads_id_key_path_and_slug_from_the_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(GA.BR, "real_account_home", lambda: tmp_path)
    directory = tmp_path / ".config" / "convivy" / "github-app"
    _write_app_files(directory, app_id="7654321", slug="acme-bosun")

    app = GA.installed_app()

    assert app.app_id == "7654321"
    assert app.key_path == directory / "app.pem"
    assert app.slug == "acme-bosun"


def test_installed_app_raises_naming_the_path_when_app_id_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(GA.BR, "real_account_home", lambda: tmp_path)
    directory = tmp_path / ".config" / "convivy" / "github-app"
    directory.mkdir(parents=True)
    (directory / "app.pem").write_text("placeholder-key")

    with pytest.raises(GA.Unreadable) as e:
        GA.installed_app()
    assert str(directory / "app-id") in str(e.value)
    assert "create-github-app" in str(e.value)


def test_installed_app_raises_naming_the_path_when_app_id_is_unreadable(tmp_path, monkeypatch):
    monkeypatch.setattr(GA.BR, "real_account_home", lambda: tmp_path)
    directory = tmp_path / ".config" / "convivy" / "github-app"
    _write_app_files(directory)
    (directory / "app-id").chmod(0o000)
    try:
        with pytest.raises(GA.Unreadable) as e:
            GA.installed_app()
        assert str(directory / "app-id") in str(e.value)
    finally:
        (directory / "app-id").chmod(0o600)  # restore so tmp_path cleanup can delete it


def test_installed_app_raises_naming_the_path_when_app_id_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(GA.BR, "real_account_home", lambda: tmp_path)
    directory = tmp_path / ".config" / "convivy" / "github-app"
    directory.mkdir(parents=True)
    (directory / "app-id").write_text("")
    (directory / "app.pem").write_text("placeholder-key")

    with pytest.raises(GA.Unreadable) as e:
        GA.installed_app()
    assert str(directory / "app-id") in str(e.value)


def test_installed_app_raises_naming_the_path_when_the_key_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(GA.BR, "real_account_home", lambda: tmp_path)
    directory = tmp_path / ".config" / "convivy" / "github-app"
    _write_app_files(directory, key=False)

    with pytest.raises(GA.Unreadable) as e:
        GA.installed_app()
    assert str(directory / "app.pem") in str(e.value)
    assert "create-github-app" in str(e.value)


def test_installed_app_a_missing_slug_resolves_to_none_not_an_error(tmp_path, monkeypatch):
    """connect-github's own check requires only `app-id` and `app.pem` — a slug
    not yet recorded is not an error, and every caller that builds a `[bot]`
    identity from it must handle `None` rather than assume a slug exists."""
    monkeypatch.setattr(GA.BR, "real_account_home", lambda: tmp_path)
    directory = tmp_path / ".config" / "convivy" / "github-app"
    _write_app_files(directory, slug=None)

    app = GA.installed_app()

    assert app.slug is None


def test_installed_app_resolves_fresh_on_every_call_not_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(GA.BR, "real_account_home", lambda: tmp_path)
    directory = tmp_path / ".config" / "convivy" / "github-app"
    _write_app_files(directory, app_id="1111111")

    assert GA.installed_app().app_id == "1111111"

    (directory / "app-id").write_text("2222222\n")

    assert GA.installed_app().app_id == "2222222"


def test_installed_app_is_not_overridden_by_any_environment_variable(tmp_path, monkeypatch):
    """No environment variable overrides the resolved directory — only the real
    account home does."""
    monkeypatch.setattr(GA.BR, "real_account_home", lambda: tmp_path)
    directory = tmp_path / ".config" / "convivy" / "github-app"
    _write_app_files(directory, app_id="1234567")
    monkeypatch.setenv("GITHUB_APP_ID", "9999999")
    monkeypatch.setenv("CONVIVY_GITHUB_APP_DIR", "/nonexistent")

    app = GA.installed_app()

    assert app.app_id == "1234567"


def test_installed_app_dir_is_read_from_the_real_account_home_not_env_home(
        tmp_path, monkeypatch):
    """A dispatched run's sandboxed `$HOME` must not redirect this resolver —
    only `builder_root.real_account_home` (the passwd database) does."""
    real_home = tmp_path / "real-home"
    fake_home = tmp_path / "sandboxed-home"
    monkeypatch.setattr(GA.BR, "real_account_home", lambda: real_home)
    monkeypatch.setenv("HOME", str(fake_home))
    directory = real_home / ".config" / "convivy" / "github-app"
    _write_app_files(directory, app_id="1234567")

    app = GA.installed_app()

    assert app.app_id == "1234567"
    assert app.key_path == directory / "app.pem"


def test_installed_app_resolves_a_symlinked_key_so_key_problem_passes(tmp_path, monkeypatch):
    """On an install that keeps the App key under its older name, `app.pem` is a
    symlink to `bosun-app.pem` (`run_sandbox.py`'s `DENIED_HOME_FILES` comment
    names both for exactly this). `key_problem`'s `lstat()` refuses a symlink
    outright regardless of its target, so `installed_app()` must hand back the
    key's real, resolved path for `key_problem` to check (CQ·612,
    issue_01M69GD1WEN67MZPSBQZD02CDN)."""
    monkeypatch.setattr(GA.BR, "real_account_home", lambda: tmp_path)
    directory = tmp_path / ".config" / "convivy" / "github-app"
    directory.mkdir(parents=True)
    (directory / "app-id").write_text("1234567\n")
    real_key = tmp_path / ".config" / "convivy" / "bosun-app.pem"
    real_key.write_bytes(b"fake-key-bytes")
    real_key.chmod(0o600)
    (directory / "app.pem").symlink_to(real_key)

    app = GA.installed_app()

    assert app.key_path == real_key.resolve()
    assert GA.key_problem(app.key_path) is None


def test_installed_app_a_dangling_key_symlink_refuses(tmp_path, monkeypatch):
    """`app.pem` pointing at a target that does not exist is caught by the
    unresolved `is_file()` check, before `Path.resolve()` ever runs — naming
    `app.pem` itself, not the missing target."""
    monkeypatch.setattr(GA.BR, "real_account_home", lambda: tmp_path)
    directory = tmp_path / ".config" / "convivy" / "github-app"
    directory.mkdir(parents=True)
    (directory / "app-id").write_text("1234567\n")
    (directory / "app.pem").symlink_to(tmp_path / "nowhere.pem")

    with pytest.raises(GA.Unreadable) as e:
        GA.installed_app()
    assert str(directory / "app.pem") in str(e.value)


def test_installed_app_resolves_a_symlink_to_a_wrong_mode_key_which_then_refuses(
        tmp_path, monkeypatch):
    """Resolving the symlink does not relax `key_problem`'s own checks — a key
    at mode 0644 behind the symlink is still refused, now against the real
    file rather than the (always-refused) link itself."""
    monkeypatch.setattr(GA.BR, "real_account_home", lambda: tmp_path)
    directory = tmp_path / ".config" / "convivy" / "github-app"
    directory.mkdir(parents=True)
    (directory / "app-id").write_text("1234567\n")
    real_key = tmp_path / ".config" / "convivy" / "bosun-app.pem"
    real_key.write_bytes(b"fake-key-bytes")
    real_key.chmod(0o644)
    (directory / "app.pem").symlink_to(real_key)

    app = GA.installed_app()

    problem = GA.key_problem(app.key_path)
    assert problem is not None
    assert "mode" in problem


def test_mint_run_github_token_mints_with_the_resolved_app_not_a_literal(
        tmp_path, monkeypatch, rsa_key):
    """`bin/agentctl`'s `_mint_run_github_token`, exercised through `GA.mint`
    directly with a synthetic id and key, proving the old hard-coded App id
    is never read."""
    key, _ = rsa_key
    monkeypatch.setattr(GA.BR, "real_account_home", lambda: tmp_path)
    directory = tmp_path / ".config" / "convivy" / "github-app"
    directory.mkdir(parents=True)
    (directory / "app-id").write_text("9999999\n")
    (directory / "app.pem").write_bytes(key.read_bytes())
    (directory / "app.pem").chmod(0o600)
    monkeypatch.setattr(GA, "mint_log_path", lambda: tmp_path / "mint-log.jsonl")

    seen_app_ids = []
    real_app_jwt = GA.app_jwt

    def spy_app_jwt(key_path, app_id, now=None):
        seen_app_ids.append(app_id)
        return real_app_jwt(key_path, app_id, now=now)

    monkeypatch.setattr(GA, "app_jwt", spy_app_jwt)

    def opener(req, timeout=None):
        if "/orgs/" in req.full_url and req.full_url.endswith("/installation"):
            return _Resp(200, {"id": 42, "account": {"login": "acme"}})
        return _Resp(201, {"token": "ghs_x", "permissions": {"contents": "read"},
                           "repository_selection": "selected",
                           "expires_at": "2099-01-01T00:00:00Z"})

    app = GA.installed_app()
    payload = GA.mint("acme/widget", "read", key_path=app.key_path, app_id=app.app_id,
                      opener=opener)

    assert payload["token"] == "ghs_x"
    assert seen_app_ids == ["9999999"]


def test_a_fresh_install_with_no_bosun_app_pem_reaches_the_mint(tmp_path, monkeypatch, rsa_key):
    """A fresh install (Shipwright's rehearsal) has no `bosun-app.pem` at all —
    only `~/.config/convivy/github-app/{app-id,app.pem}` — and `installed_app()`
    must resolve from that alone, reaching the mint rather than failing on the
    old, now-absent path."""
    key, _ = rsa_key
    monkeypatch.setattr(GA.BR, "real_account_home", lambda: tmp_path)
    directory = tmp_path / ".config" / "convivy" / "github-app"
    directory.mkdir(parents=True)
    (directory / "app-id").write_text("1234567\n")
    (directory / "app.pem").write_bytes(key.read_bytes())
    (directory / "app.pem").chmod(0o600)
    monkeypatch.setattr(GA, "mint_log_path", lambda: tmp_path / "mint-log.jsonl")
    assert not (tmp_path / ".config" / "convivy" / "bosun-app.pem").exists()

    def opener(req, timeout=None):
        if "/orgs/" in req.full_url and req.full_url.endswith("/installation"):
            return _Resp(200, {"id": 42, "account": {"login": "acme"}})
        return _Resp(201, {"token": "ghs_x", "permissions": {"contents": "read"},
                           "repository_selection": "selected",
                           "expires_at": "2099-01-01T00:00:00Z"})

    app = GA.installed_app()
    payload = GA.mint("acme/widget", "read", key_path=app.key_path, app_id=app.app_id,
                      opener=opener)

    assert payload["token"] == "ghs_x"


def test_scrub_removes_the_token_and_its_basic_auth_encoding():
    token = "ghs_super_secret"
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    text = f"fatal: could not push, Authorization: Basic {basic} ({token})"
    scrubbed = GA.scrub(text, token)
    assert token not in scrubbed and basic not in scrubbed
