"""The GitHub token minter (`github_app.mint`/`github_app.mint_token`, E10 S1,
platform/specs/github-credential-seam): one function every verb mints the
convivy-bosun App's installation token through, and the owner-only mint log
every mint and revoke appends to.

`test_github_app.py` covers the lower-level primitives (`app_jwt`,
`installation_for_owner`, `installation_token`, `revoke_installation_token`)
this module builds on; this file is the mint/log layer itself.

No real key, Keychain, `~/.config/gh` or `~/.config/convivy` anywhere here —
every key is a fresh RSA key generated into `tmp_path`, and every network call
goes through a fake opener that never leaves this process.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

import github_app as GA
import operator_config

APP_ID = "555555"
REAL_TOKEN = "ghs_should_never_appear_in_the_log"

# A sentinel distinct from `None`, so `_fake_api`'s `installation_permissions`
# can tell "not given, use the default" apart from "given as None" (which
# means an installation lookup response with no `permissions` key at all).
_UNSET = object()


@pytest.fixture()
def rsa_key(tmp_path):
    key = tmp_path / "app.pem"
    subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"], check=True,
                   capture_output=True)
    key.chmod(0o600)
    return key


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


def _fake_api(*, inst_id=161212648, token=REAL_TOKEN, granted_permissions=None,
             repository_selection="selected", expires_at="2026-10-17T12:00:00Z",
             revoke_status=204, installation_permissions=_UNSET):
    """A fake opener answering the org-installation lookup, the access-tokens
    POST and the token DELETE, recording every request it sees. When
    `granted_permissions` is given, the token response reports THAT rather
    than echoing back what was requested — the only way a test can tell
    "logged the grant" apart from "logged the ask". `revoke_status` lets a
    test make the DELETE fail (any status other than 204) without an
    exception, the same way GitHub itself would answer a revoke it refuses.

    `installation_permissions` is the `permissions` dict the installation
    lookup itself reports (`GET /orgs/{owner}/installation`'s own
    `permissions` key, read on the real convivy-bosun App on 2026-11-01).
    Defaults to an installation that holds `workflows`, matching that real
    install, so a test exercising a verb other than `publish`'s `workflows`
    carve-out need not care; passing `None` omits the `permissions` key from
    the lookup response entirely, as an installation with no `permissions`
    key at all would."""
    calls = []
    inst_permissions = ({"workflows": "write"} if installation_permissions is _UNSET
                        else installation_permissions)

    def opener(req, timeout=None):
        calls.append({"method": req.get_method(), "url": req.full_url,
                      "body": json.loads(req.data) if req.data else None})
        if req.get_method() == "GET" and req.full_url.endswith("/installation"):
            inst = {"id": inst_id, "account": {"login": "convivy"}}
            if inst_permissions is not None:
                inst["permissions"] = inst_permissions
            return _Resp(200, inst)
        if req.get_method() == "POST" and req.full_url.endswith("/access_tokens"):
            body = json.loads(req.data)
            perms = granted_permissions if granted_permissions is not None \
                else body.get("permissions")
            return _Resp(201, {"token": token, "permissions": perms,
                               "repository_selection": repository_selection,
                               "expires_at": expires_at})
        if req.get_method() == "DELETE" and req.full_url.endswith("/installation/token"):
            return _Resp(revoke_status, {})
        raise AssertionError(f"unexpected {req.get_method()} {req.full_url}")
    return opener, calls


# `rehearsal` is excluded from the two generic parametrizations just below:
# unlike every other verb, it refuses `convivy/agent-platform` outright (its
# repository and owner are restricted — see the dedicated section near the
# bottom of this file), so it needs its own repo and its own operator-config
# stub rather than the bare "any repo, any install" shape those two share.
_GENERIC_VERBS = sorted(v for v in GA.VERB_PERMISSIONS if v != "rehearsal")


def test_verb_permissions_release_is_contents_write_alone():
    """`release` (agentctl's `release create`, company/decisions/0027) asks
    GitHub for `contents:write` and nothing else — a release carries no PR,
    so no `pull_requests` scope at all."""
    assert GA.VERB_PERMISSIONS["release"] == {"contents": "write"}


@pytest.mark.parametrize("verb", _GENERIC_VERBS)
def test_mint_scopes_the_request_to_one_repo_and_the_verbs_own_permissions(
        rsa_key, tmp_path, monkeypatch, verb):
    """Fails against origin/main on `AttributeError: module 'github_app' has
    no attribute 'mint'` — `mint` does not exist there yet."""
    monkeypatch.setattr(GA, "mint_log_path", lambda: tmp_path / "mint-log.jsonl")
    opener, calls = _fake_api()

    payload = GA.mint("convivy/agent-platform", verb, key_path=rsa_key, app_id=APP_ID,
                      opener=opener)

    post = next(c for c in calls if c["method"] == "POST")
    assert post["body"] == {"repositories": ["agent-platform"],
                            "permissions": GA.VERB_PERMISSIONS[verb]}
    assert payload["token"] == REAL_TOKEN


@pytest.mark.parametrize("verb", _GENERIC_VERBS)
def test_mint_payload_matches_expected_granted_permissions_when_github_adds_metadata(
        rsa_key, tmp_path, monkeypatch, verb):
    """Pins the comparison tests/live/test_github_mint_live.py relies on: when
    GitHub's response adds `metadata: read` to the grant (as it does on every
    real installation token — observed against the real convivy-bosun App on
    2026-10-18, every verb), the granted permissions equal
    `GA.expected_granted_permissions(verb)`, not the bare
    `VERB_PERMISSIONS[verb]` the request asked for.

    Fails against origin/main on `AttributeError: module 'github_app' has no
    attribute 'expected_granted_permissions'` — that function does not exist
    there yet."""
    monkeypatch.setattr(GA, "mint_log_path", lambda: tmp_path / "mint-log.jsonl")
    granted = {**GA.VERB_PERMISSIONS[verb], "metadata": "read"}
    opener, _ = _fake_api(granted_permissions=granted)

    payload = GA.mint("convivy/agent-platform", verb, key_path=rsa_key, app_id=APP_ID,
                      opener=opener)

    assert payload.get("permissions") == GA.expected_granted_permissions(verb)


# --- publish's `workflows` carve-out (issue_01M6C7SDJCJR99T01WV5K3YFN8) -----
#
# `workflows` is the one key `VERB_PERMISSIONS["publish"]` names that `mint`
# asks GitHub for conditionally: only when the installation it already read
# (to get its id) carries `workflows` in its own `permissions`. Every other
# key on every verb, publish included, is requested unconditionally and
# refused loudly by GitHub when the installation lacks it — these four tests
# pin that this carve-out reaches no further than the one key on the one verb.

def test_publish_omits_workflows_when_the_installation_lacks_it(
        rsa_key, tmp_path, monkeypatch):
    """Fails against today's main: `mint` has no installation-permissions
    check at all, so it always requests `VERB_PERMISSIONS["publish"]`
    verbatim, `workflows` included — this assertion's `post["body"]` would
    carry `workflows: write`."""
    monkeypatch.setattr(GA, "mint_log_path", lambda: tmp_path / "mint-log.jsonl")
    opener, calls = _fake_api(installation_permissions={"contents": "write"})

    GA.mint("convivy/agent-platform", "publish", key_path=rsa_key, app_id=APP_ID,
           opener=opener)

    post = next(c for c in calls if c["method"] == "POST")
    assert post["body"]["permissions"] == {"contents": "write", "pull_requests": "read"}
    assert "workflows" not in post["body"]["permissions"]


def test_publish_includes_workflows_when_the_installation_holds_it(
        rsa_key, tmp_path, monkeypatch):
    """Fails against today's main on 422 (simulated here as a plain assertion
    against the request body): an installation that DOES hold `workflows`
    must still get it on the publish request, so this carve-out does not
    narrow the Convivy install's own publish mint."""
    monkeypatch.setattr(GA, "mint_log_path", lambda: tmp_path / "mint-log.jsonl")
    opener, calls = _fake_api(installation_permissions={"workflows": "write",
                                                         "contents": "write"})

    GA.mint("convivy/agent-platform", "publish", key_path=rsa_key, app_id=APP_ID,
           opener=opener)

    post = next(c for c in calls if c["method"] == "POST")
    assert post["body"]["permissions"] == GA.VERB_PERMISSIONS["publish"]
    assert post["body"]["permissions"]["workflows"] == "write"


def test_publish_omits_workflows_when_the_installation_has_no_permissions_key_at_all(
        rsa_key, tmp_path, monkeypatch):
    """An installation lookup that reports no `permissions` key at all (rather
    than an empty one) must be read the same as one that lacks `workflows` —
    `mint` must not crash on a missing key and must not request `workflows`.
    Fails against today's main on `AttributeError`/422 the same way as the
    first test in this group."""
    monkeypatch.setattr(GA, "mint_log_path", lambda: tmp_path / "mint-log.jsonl")
    opener, calls = _fake_api(installation_permissions=None)

    GA.mint("convivy/agent-platform", "publish", key_path=rsa_key, app_id=APP_ID,
           opener=opener)

    post = next(c for c in calls if c["method"] == "POST")
    assert "workflows" not in post["body"]["permissions"]


def test_a_verb_other_than_publish_is_unaffected_by_a_missing_workflows_permission(
        rsa_key, tmp_path, monkeypatch):
    """The carve-out names `publish` explicitly: a different verb's table
    (none of which name `workflows`) is requested unchanged regardless of
    what the installation holds, and an installation lacking `contents`
    still gets `contents:write` requested on publish — this carve-out does
    not become a general intersection against the installation's grants."""
    monkeypatch.setattr(GA, "mint_log_path", lambda: tmp_path / "mint-log.jsonl")
    opener, calls = _fake_api(installation_permissions={})

    GA.mint("convivy/agent-platform", "pr-ready", key_path=rsa_key, app_id=APP_ID,
           opener=opener)
    GA.mint("convivy/agent-platform", "publish", key_path=rsa_key, app_id=APP_ID,
           opener=opener)

    posts = [c for c in calls if c["method"] == "POST"]
    assert posts[0]["body"]["permissions"] == GA.VERB_PERMISSIONS["pr-ready"]
    assert posts[1]["body"]["permissions"]["contents"] == "write"
    assert "workflows" not in posts[1]["body"]["permissions"]


def test_an_unknown_verb_is_refused_before_the_opener_is_ever_called(rsa_key, tmp_path):
    """Fails against origin/main the same way as the test above: `mint` is not
    there to raise anything."""
    def _boom(req, timeout=None):
        raise AssertionError("an unknown verb must be refused before any network call")

    with pytest.raises(GA.Unreadable) as e:
        GA.mint("convivy/agent-platform", "delete-everything", key_path=rsa_key,
               app_id=APP_ID, opener=_boom)
    assert "delete-everything" in str(e.value)


def test_the_tokens_bytes_appear_nowhere_in_the_log_file(rsa_key, tmp_path, monkeypatch):
    """Fails against origin/main on the missing `mint_log_path`/`mint`
    attributes — there is no log file to make this assertion about yet."""
    log_path = tmp_path / "mint-log.jsonl"
    monkeypatch.setattr(GA, "mint_log_path", lambda: log_path)
    opener, _ = _fake_api(token="ghs_SECRET_VALUE_NEVER_LOGGED")

    GA.mint("convivy/agent-platform", "read", key_path=rsa_key, app_id=APP_ID, opener=opener)

    assert "ghs_SECRET_VALUE_NEVER_LOGGED" not in log_path.read_text()


def test_mint_token_revokes_when_the_block_raises(rsa_key, tmp_path, monkeypatch):
    """Fails against origin/main on `AttributeError: module 'github_app' has
    no attribute 'mint_token'`."""
    monkeypatch.setattr(GA, "mint_log_path", lambda: tmp_path / "mint-log.jsonl")
    opener, calls = _fake_api()

    class _Boom(Exception):
        pass

    with pytest.raises(_Boom):
        with GA.mint_token("convivy/agent-platform", "publish", key_path=rsa_key,
                           app_id=APP_ID, opener=opener) as token:
            assert token == REAL_TOKEN
            raise _Boom()

    deletes = [c for c in calls if c["method"] == "DELETE"]
    assert len(deletes) == 1


def test_mint_token_revokes_on_a_clean_exit_too(rsa_key, tmp_path, monkeypatch):
    monkeypatch.setattr(GA, "mint_log_path", lambda: tmp_path / "mint-log.jsonl")
    opener, calls = _fake_api()

    with GA.mint_token("convivy/agent-platform", "pr-open", key_path=rsa_key,
                       app_id=APP_ID, opener=opener) as token:
        assert token == REAL_TOKEN

    deletes = [c for c in calls if c["method"] == "DELETE"]
    assert len(deletes) == 1


def test_mint_revokes_the_token_and_raises_when_the_log_write_fails(
        rsa_key, tmp_path, monkeypatch):
    """A failed mint-log write must not leave a live, unlogged token — `mint`
    revokes it before raising. Fails against origin/main on the missing
    `mint`/`_append_mint_log` attributes.

    Raises `GA.Unreadable`, not the bare `OSError` the log write itself
    raised (round 1 of #927, finding 2): every caller of `mint` catches
    `Unreadable` for its other refusals, and a bare `OSError` escaping here
    would crash that caller uncaught instead of producing a clean refusal.

    The revoke here succeeds (DELETE answers 204), so the raised message
    says the token was revoked (round 2 of #927, finding 1 — the message
    must say what happened, not assume it)."""
    log_path = tmp_path / "mint-log.jsonl"
    monkeypatch.setattr(GA, "mint_log_path", lambda: log_path)

    def _boom_append(entry):
        raise OSError("disk full")
    monkeypatch.setattr(GA, "_append_mint_log", _boom_append)
    opener, calls = _fake_api()

    with pytest.raises(GA.Unreadable) as e:
        GA.mint("convivy/agent-platform", "read", key_path=rsa_key, app_id=APP_ID,
               opener=opener)
    assert str(log_path) in str(e.value)
    assert isinstance(e.value.__cause__, OSError)
    assert "was revoked" in str(e.value)
    assert "not be revoked" not in str(e.value)

    deletes = [c for c in calls if c["method"] == "DELETE"]
    assert len(deletes) == 1


def test_mint_says_the_token_was_not_revoked_when_the_revoke_itself_fails(
        rsa_key, tmp_path, monkeypatch):
    """The companion to the test above: when the mint-log write fails AND the
    revoke GitHub answers to clean up after it also fails, the raised
    message must say the token was NOT revoked and name its expiry — not
    claim, as origin/main's wording does unconditionally, that it was
    revoked when nobody checked GitHub's answer to the DELETE (round 2 of
    #927, finding 1).

    Fails against 360692ed6 on `assert "was not revoked"`: that commit's
    message reads "the just-minted token was revoked" even though the DELETE
    below answers 500, because it never looks at `revoke_installation_token`'s
    return value."""
    log_path = tmp_path / "mint-log.jsonl"
    monkeypatch.setattr(GA, "mint_log_path", lambda: log_path)

    def _boom_append(entry):
        raise OSError("disk full")
    monkeypatch.setattr(GA, "_append_mint_log", _boom_append)
    opener, calls = _fake_api(revoke_status=500, expires_at="2026-10-17T13:00:00Z")

    with pytest.raises(GA.Unreadable) as e:
        GA.mint("convivy/agent-platform", "read", key_path=rsa_key, app_id=APP_ID,
               opener=opener)
    assert "was not revoked" in str(e.value)
    assert "2026-10-17T13:00:00Z" in str(e.value)

    deletes = [c for c in calls if c["method"] == "DELETE"]
    assert len(deletes) == 1


def test_the_log_records_what_github_granted_not_what_was_requested(
        rsa_key, tmp_path, monkeypatch):
    """Fails against origin/main the same way as the other `mint` tests — and
    would also fail against an implementation that logged the REQUEST body
    instead of the response, since the fake here grants something narrower
    than `publish` asks for."""
    log_path = tmp_path / "mint-log.jsonl"
    monkeypatch.setattr(GA, "mint_log_path", lambda: log_path)
    granted = {"contents": "read"}  # narrower than VERB_PERMISSIONS["publish"]
    opener, _ = _fake_api(granted_permissions=granted)

    GA.mint("convivy/agent-platform", "publish", key_path=rsa_key, app_id=APP_ID,
           opener=opener)

    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["permissions"] == granted
    assert entry["permissions"] != GA.VERB_PERMISSIONS["publish"]
    assert entry["verb"] == "publish"
    assert entry["repo"] == "convivy/agent-platform"
    assert entry["repository_selection"] == "selected"
    assert "token" not in entry


def test_the_log_file_is_created_0600_in_a_0700_directory(rsa_key, tmp_path, monkeypatch):
    log_path = tmp_path / "state" / "github-mint" / "mint-log.jsonl"
    monkeypatch.setattr(GA, "mint_log_path", lambda: log_path)
    opener, _ = _fake_api()

    GA.mint("convivy/agent-platform", "read", key_path=rsa_key, app_id=APP_ID, opener=opener)

    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(log_path.parent.stat().st_mode) == 0o700


def test_a_pre_existing_log_directory_and_file_are_corrected_to_0700_and_0600(
        rsa_key, tmp_path, monkeypatch):
    """`mkdir`'s and `os.open`'s mode arguments are no-ops on a directory or
    file that already exists (round 1 of #927, finding 1) — `_append_mint_log`
    must correct both with an explicit chmod/fchmod rather than relying on
    `mkdir(mode=...)` and `os.open(..., 0o600)` alone, which only apply at
    creation and are masked by the umask even then.

    Fails against 6aa4fda2b: `assert stat.S_IMODE(log_dir.stat().st_mode) ==
    0o700` — AssertionError: assert 493 == 448 (0o755 survives untouched)."""
    log_dir = tmp_path / "state" / "github-mint"
    log_dir.mkdir(parents=True, mode=0o755)
    log_dir.chmod(0o755)
    log_path = log_dir / "mint-log.jsonl"
    log_path.write_text("")
    log_path.chmod(0o644)
    monkeypatch.setattr(GA, "mint_log_path", lambda: log_path)
    opener, _ = _fake_api()

    GA.mint("convivy/agent-platform", "read", key_path=rsa_key, app_id=APP_ID, opener=opener)

    assert stat.S_IMODE(log_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600


# --- GA.gh_env (E10 S3, platform/specs/github-credential-seam) ----------------
# The environment one `gh` subprocess call runs in as the convivy-bosun App:
# `GH_TOKEN`, `GH_PROMPT_DISABLED=1`, and a fresh, empty `GH_CONFIG_DIR` —
# created on entry and removed on exit — so `gh` can neither read the
# operator's `~/.config/gh/hosts.yml` nor write a new one.

def test_gh_env_carries_the_token_and_disables_prompts():
    """Fails against origin/main on `AttributeError: module 'github_app' has
    no attribute 'gh_env'` — it does not exist there yet."""
    with GA.gh_env("ghs_the_token") as env:
        assert env["GH_TOKEN"] == "ghs_the_token"
        assert env["GH_PROMPT_DISABLED"] == "1"


def test_gh_env_config_dir_is_empty_while_the_block_runs():
    with GA.gh_env("ghs_the_token") as env:
        config_dir = Path(env["GH_CONFIG_DIR"])
        assert config_dir.is_dir()
        assert list(config_dir.iterdir()) == []


def test_gh_env_config_dir_is_removed_once_the_block_exits():
    with GA.gh_env("ghs_the_token") as env:
        config_dir = Path(env["GH_CONFIG_DIR"])
    assert not config_dir.exists()


def test_gh_env_config_dir_is_removed_even_when_the_block_raises():
    class _Boom(Exception):
        pass

    captured = {}
    with pytest.raises(_Boom):
        with GA.gh_env("ghs_the_token") as env:
            captured["dir"] = Path(env["GH_CONFIG_DIR"])
            raise _Boom()
    assert not captured["dir"].exists()


def test_gh_env_starts_from_the_given_base_not_the_real_process_environment():
    """`GA.gh_env` builds its environment from a given `base=` dict rather than
    a fresh copy of the real process's `os.environ` — and the real process's
    `os.environ` is left untouched either way. (`_gh_run_env` itself calls
    `gh_env(token)` with no `base`; this test pins `gh_env`'s own contract.)"""
    before = os.environ.get("GH_TOKEN")
    with GA.gh_env("ghs_the_token", base={"ONLY": "this"}) as env:
        assert env["ONLY"] == "this"
        assert env["GH_TOKEN"] == "ghs_the_token"
    assert os.environ.get("GH_TOKEN") == before, "this test's own session may already carry one"
    assert "ONLY" not in os.environ


# --- GA.read_token_env / GA.read_token_pool (company/decisions/0027, moving
# the launchd scorecards and harvesters off the operator's own `gh` login) --
#
# Both compose `mint_token("read", ...)` + `gh_env`, so `installed_app` is the
# one extra seam these need beyond `_fake_api`'s opener — stubbed here rather
# than reading the real `~/.config/convivy/github-app`.

def _stub_installed_app(monkeypatch, rsa_key):
    monkeypatch.setattr(GA, "installed_app",
                        lambda: GA.InstalledApp(app_id=APP_ID, key_path=rsa_key, slug=None))


def test_read_token_env_yields_a_gh_env_carrying_the_minted_token(rsa_key, tmp_path, monkeypatch):
    monkeypatch.setattr(GA, "mint_log_path", lambda: tmp_path / "mint-log.jsonl")
    _stub_installed_app(monkeypatch, rsa_key)
    opener, calls = _fake_api()

    with GA.read_token_env("convivy/widget", opener=opener) as env:
        assert env["GH_TOKEN"] == REAL_TOKEN
        assert env["GH_CONFIG_DIR"]

    post = next(c for c in calls if c["method"] == "POST")
    assert post["body"] == {"repositories": ["widget"],
                            "permissions": GA.VERB_PERMISSIONS["read"]}
    assert any(c["method"] == "DELETE" for c in calls), "the token must be revoked on exit"


def test_read_token_env_raises_unreadable_before_any_gh_env_when_the_mint_fails(
        rsa_key, tmp_path, monkeypatch):
    monkeypatch.setattr(GA, "mint_log_path", lambda: tmp_path / "mint-log.jsonl")
    _stub_installed_app(monkeypatch, rsa_key)

    def opener(req, timeout=None):
        if req.get_method() == "GET" and req.full_url.endswith("/installation"):
            return _Resp(404, {})
        raise AssertionError(f"unexpected {req.get_method()} {req.full_url}")

    with pytest.raises(GA.NotInstalled):
        with GA.read_token_env("convivy/widget", opener=opener):
            raise AssertionError("must not enter the block on a mint failure")


def test_read_token_pool_mints_once_per_repository_across_several_env_for_calls(
        rsa_key, tmp_path, monkeypatch):
    """Two `env_for` calls for the same repo, interleaved with a different
    repo, mint exactly once per repo — the pool's whole reason to exist over
    opening and closing `read_token_env` at each call (`had_follow_up_bug`,
    `was_reverted` and `ci_first_pass` in model-scorecard revisit a repo this
    way across one `harvest()` pass)."""
    monkeypatch.setattr(GA, "mint_log_path", lambda: tmp_path / "mint-log.jsonl")
    _stub_installed_app(monkeypatch, rsa_key)
    opener, calls = _fake_api()

    with GA.read_token_pool(opener=opener) as env_for:
        first_widget = env_for("convivy/widget")
        env_for("convivy/gadget")
        second_widget = env_for("convivy/widget")
        assert first_widget is second_widget

    posts = [c for c in calls if c["method"] == "POST"]
    assert sorted(p["body"]["repositories"][0] for p in posts) == ["gadget", "widget"]
    assert sum(1 for c in calls if c["method"] == "DELETE") == 2


def test_read_token_pool_revokes_every_minted_repo_token_on_close(
        rsa_key, tmp_path, monkeypatch):
    monkeypatch.setattr(GA, "mint_log_path", lambda: tmp_path / "mint-log.jsonl")
    _stub_installed_app(monkeypatch, rsa_key)
    opener, calls = _fake_api()

    with GA.read_token_pool(opener=opener) as env_for:
        env_for("convivy/widget")
        env_for("convivy/gadget")

    assert sum(1 for c in calls if c["method"] == "DELETE") == 2


def test_read_token_pool_does_not_cache_a_failed_mint_and_retries_on_the_next_call(
        rsa_key, tmp_path, monkeypatch):
    """A repository whose mint failed is not poisoned for the rest of the
    pass: a caller that catches `Unreadable`, takes its own gh-failure path,
    and later asks the pool for the same repository again (a second ledger
    row joining the same repo) gets a fresh mint attempt, not a replayed
    failure with no network call behind it."""
    monkeypatch.setattr(GA, "mint_log_path", lambda: tmp_path / "mint-log.jsonl")
    _stub_installed_app(monkeypatch, rsa_key)
    attempts = {"n": 0}

    def opener(req, timeout=None):
        if req.get_method() == "GET" and req.full_url.endswith("/installation"):
            attempts["n"] += 1
            # The first mint attempt's org AND user lookups (`installation_for_owner`'s
            # own fallback) both 404, so that whole attempt raises NotInstalled;
            # every lookup from the second mint attempt onward succeeds.
            if attempts["n"] <= 2:
                return _Resp(404, {})
            return _Resp(200, {"id": 161212648, "account": {"login": "convivy"}})
        if req.get_method() == "POST" and req.full_url.endswith("/access_tokens"):
            return _Resp(201, {"token": REAL_TOKEN, "permissions": GA.VERB_PERMISSIONS["read"],
                               "repository_selection": "selected",
                               "expires_at": "2026-10-17T12:00:00Z"})
        if req.get_method() == "DELETE" and req.full_url.endswith("/installation/token"):
            return _Resp(204, {})
        raise AssertionError(f"unexpected {req.get_method()} {req.full_url}")

    with GA.read_token_pool(opener=opener) as env_for:
        with pytest.raises(GA.NotInstalled):
            env_for("convivy/widget")
        env = env_for("convivy/widget")
        assert env["GH_TOKEN"] == REAL_TOKEN
    assert attempts["n"] == 3


# --- the `rehearsal` verb's own guard (E10 S11, platform/specs/github-credential-
# -seam, amended by knowledge#1113) ------------------------------------------
#
# Unlike every other verb, `rehearsal` is refused for a repository and owner
# `mint` itself decides, before any network call, rather than minting for
# whatever `repo_slug` the caller passed. `_rehearsal_operator_config` writes
# a throwaway operator config naming `rehearsal_repository_owners`, wired in
# through `CONVIVY_OPERATOR_CONFIG` exactly as `tests/test_operator_config.py`
# does — never this machine's real file. Every owner and repository below is
# synthetic (example-org, example-person, widget-repo).

def _rehearsal_operator_config(tmp_path: Path, owners) -> Path:
    path = tmp_path / "operator.json"
    data = {} if owners is None else {"rehearsal_repository_owners": owners}
    path.write_text(json.dumps(data))
    return path


def _no_request_opener(req, timeout=None):
    raise AssertionError("rehearsal must be refused before any network call")


def test_rehearsal_is_refused_for_a_listed_owners_other_repository(
        rsa_key, tmp_path, monkeypatch):
    """Fails against a build with no `rehearsal` verb at all, and against one
    that checks the owner but not the fixed repository name."""
    monkeypatch.setattr(GA, "mint_log_path", lambda: tmp_path / "mint-log.jsonl")
    monkeypatch.setenv(operator_config.ENV_VAR,
                       str(_rehearsal_operator_config(tmp_path, ["example-org"])))

    with pytest.raises(GA.Unreadable):
        GA.mint("example-org/widget-repo", "rehearsal", key_path=rsa_key,
               app_id=APP_ID, opener=_no_request_opener)


@pytest.mark.parametrize(
    "repo_slug",
    [
        "example-org/builder-merge-rehearsal-old",
        "example-org/old-builder-merge-rehearsal",
    ],
    ids=["suffixed", "prefixed"],
)
def test_rehearsal_is_refused_for_a_look_alike_repository_name(
        rsa_key, tmp_path, monkeypatch, repo_slug):
    """Exact equality on `name`, never a prefix or substring match (S11,
    amended text): a listed owner's `builder-merge-rehearsal-old` or
    `old-builder-merge-rehearsal` is not `builder-merge-rehearsal`."""
    monkeypatch.setattr(GA, "mint_log_path", lambda: tmp_path / "mint-log.jsonl")
    monkeypatch.setenv(operator_config.ENV_VAR,
                       str(_rehearsal_operator_config(tmp_path, ["example-org"])))

    with pytest.raises(GA.Unreadable):
        GA.mint(repo_slug, "rehearsal", key_path=rsa_key,
               app_id=APP_ID, opener=_no_request_opener)


def test_rehearsal_is_refused_for_the_rehearsal_repository_under_a_different_case_owner(
        rsa_key, tmp_path, monkeypatch):
    """Exact equality on `owner`, case-sensitive: a case mismatch is refused
    even though it names the same account on GitHub."""
    monkeypatch.setattr(GA, "mint_log_path", lambda: tmp_path / "mint-log.jsonl")
    monkeypatch.setenv(operator_config.ENV_VAR,
                       str(_rehearsal_operator_config(tmp_path, ["example-org"])))

    with pytest.raises(GA.Unreadable):
        GA.mint(f"Example-Org/{GA.REHEARSAL_REPOSITORY_NAME}", "rehearsal",
               key_path=rsa_key, app_id=APP_ID, opener=_no_request_opener)


def test_rehearsal_is_refused_for_the_rehearsal_repository_under_an_unlisted_owner(
        rsa_key, tmp_path, monkeypatch):
    monkeypatch.setattr(GA, "mint_log_path", lambda: tmp_path / "mint-log.jsonl")
    monkeypatch.setenv(operator_config.ENV_VAR,
                       str(_rehearsal_operator_config(tmp_path, ["example-org"])))

    with pytest.raises(GA.Unreadable):
        GA.mint(f"example-person/{GA.REHEARSAL_REPOSITORY_NAME}", "rehearsal",
               key_path=rsa_key, app_id=APP_ID, opener=_no_request_opener)


@pytest.mark.parametrize(
    "owners,owner",
    [
        (None, "example-org"),
        ([], "example-org"),
        ("example-org", "example-org"),
        (["example-org", 7], "another-org"),
    ],
    ids=["list-missing", "list-empty", "string-not-list", "list-has-non-string-entry"],
)
def test_rehearsal_is_refused_for_any_repository_when_the_owner_list_is_malformed(
        rsa_key, tmp_path, monkeypatch, owners, owner):
    """Every malformed shape of `rehearsal_repository_owners` is refused
    before any network call, never treated as a successful match: a missing
    or empty list; a string in `owners`'s place (exact equality on a list
    element — never a substring test — refuses outright even for an owner
    that is itself a substring of that string, here the whole string); and a
    list holding a non-string entry, where that entry simply cannot equal
    the string `owner` this function compares (so it is never a false match
    and never raises), tested against an owner that is in neither the list
    nor a substring of its string entry."""
    monkeypatch.setattr(GA, "mint_log_path", lambda: tmp_path / "mint-log.jsonl")
    monkeypatch.setenv(operator_config.ENV_VAR,
                       str(_rehearsal_operator_config(tmp_path, owners)))

    with pytest.raises(GA.Unreadable):
        GA.mint(f"{owner}/{GA.REHEARSAL_REPOSITORY_NAME}", "rehearsal",
               key_path=rsa_key, app_id=APP_ID, opener=_no_request_opener)


def test_rehearsal_is_refused_when_the_operator_config_file_is_missing(
        rsa_key, tmp_path, monkeypatch):
    monkeypatch.setattr(GA, "mint_log_path", lambda: tmp_path / "mint-log.jsonl")
    monkeypatch.setenv(operator_config.ENV_VAR, str(tmp_path / "no-such-operator.json"))

    with pytest.raises(GA.Unreadable):
        GA.mint(f"example-org/{GA.REHEARSAL_REPOSITORY_NAME}", "rehearsal",
               key_path=rsa_key, app_id=APP_ID, opener=_no_request_opener)


def test_rehearsal_is_refused_when_the_operator_config_file_is_unreadable(
        rsa_key, tmp_path, monkeypatch):
    monkeypatch.setattr(GA, "mint_log_path", lambda: tmp_path / "mint-log.jsonl")
    config_path = _rehearsal_operator_config(tmp_path, ["example-org"])
    config_path.chmod(0o000)
    monkeypatch.setenv(operator_config.ENV_VAR, str(config_path))
    try:
        with pytest.raises(GA.Unreadable):
            GA.mint(f"example-org/{GA.REHEARSAL_REPOSITORY_NAME}", "rehearsal",
                   key_path=rsa_key, app_id=APP_ID, opener=_no_request_opener)
    finally:
        config_path.chmod(0o600)


def test_rehearsal_sends_a_scoped_request_for_a_listed_owners_rehearsal_repository(
        rsa_key, tmp_path, monkeypatch):
    monkeypatch.setattr(GA, "mint_log_path", lambda: tmp_path / "mint-log.jsonl")
    monkeypatch.setenv(operator_config.ENV_VAR,
                       str(_rehearsal_operator_config(tmp_path, ["example-org"])))
    opener, calls = _fake_api()

    payload = GA.mint(f"example-org/{GA.REHEARSAL_REPOSITORY_NAME}", "rehearsal",
                      key_path=rsa_key, app_id=APP_ID, opener=opener)

    post = next(c for c in calls if c["method"] == "POST")
    assert post["body"] == {"repositories": [GA.REHEARSAL_REPOSITORY_NAME],
                            "permissions": {"contents": "write",
                                           "pull_requests": "write"}}
    assert payload["token"] == REAL_TOKEN


# --- run-control's own table entry (`agentctl run-rerun`/`run-cancel`) ------
#
# The operator's 2026-11-05 ruling raised the App's Actions permission from
# read to write for these two calls alone: `run-control` must carry exactly
# `actions: write`, nothing wider (not `contents`, not `pull_requests`).
# `run-control` is already exercised by the two generic parametrizations
# above through `_GENERIC_VERBS` (every verb but `rehearsal`); this test pins
# the table entry itself, so a future edit widening it is caught here even if
# it kept the generic shape those tests check.

def test_run_control_verb_permissions_is_exactly_actions_write():
    """Fails against a build with no `run-control` entry at all, and against
    one that named any key besides `actions` or any level besides `write`."""
    assert GA.VERB_PERMISSIONS["run-control"] == {"actions": "write"}
