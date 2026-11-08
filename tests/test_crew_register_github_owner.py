"""`agentctl crew register --github-owner` (issue_01M649HFEHDGNSTKE4SYT9MBPX,
CQ·599): a crew whose repository lives under an account other than convivy —
the operator's own personal GitHub account is the case that filed this —
could not register, because `register` had no way to set the new unit's
`github_owner` and `projects.repo_github_slug` falls back to `convivy/<repo>`,
which does not exist there. On 2026-10-29 that fallback made the
`.github/workflows` read answer 422, refusing registration outright.

Every test here uses a synthetic crew ("widget") and a synthetic GitHub
account ("acme-person") with a FAKE OPENER standing in for both GitHub and
the Library — `tests/test_crew_registration.py`'s own `_fake_transport`
pattern, extended to answer the `.github/workflows` contents read and the
access-token mint with whatever status a test asks for. No test calls
GitHub for real.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest

import crew_registration as CR
import github_app as GA
import projects

OWNER = "acme-person"


def _spec(**kw) -> CR.CrewSpec:
    base = dict(slug="widget", display="Widget", repos=["widget"], github_owner=OWNER)
    base.update(kw)
    return CR.CrewSpec(**base)


class _Resp:
    """A `urlopen`-shaped response returned directly, with no exception, for
    ANY status — `tests/test_crew_registration.py`'s own `_Resp`."""

    def __init__(self, status: int, payload):
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def writer_token(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".orient-control").mkdir(parents=True)
    token_path = home / ".orient-control" / "agent-platform-registry-writer-token"
    token_path.write_text("writer-test-token\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: home)
    return token_path


@pytest.fixture()
def rsa_key(tmp_path):
    key = tmp_path / "app.pem"
    subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"], check=True,
                   capture_output=True)
    key.chmod(0o600)
    return key


def _fake_transport(*, contents_status=200, workflow_items=None, mint_status=201):
    """A fake opener answering the org-installation lookup, the access-token
    mint, the revoke, the `.github/workflows` contents listing, and a 201 for
    `POST /registry/crews` — `tests/test_crew_registration.py`'s own
    `_fake_transport`, with two additions this suite needs: `mint_status`,
    standing in for GitHub refusing to scope a token to a repository name the
    installation does not have (the 422 the real incident hit when it read
    `.github/workflows` for a repo under the wrong, nonexistent, slug), and
    `slugs_seen`, which records the `owner/repo` string the `.github/
    workflows` contents read targeted — the one call whose URL embeds the
    full slug (the mint's own request body carries a bare repo name, by
    GitHub's own `access_tokens` shape, not `owner/repo`) — so a test can
    assert on it directly."""
    calls: list[str] = []
    slugs_seen: list[str] = []

    def opener(req, timeout=None):
        calls.append(req.full_url)
        method = req.get_method()
        url = req.full_url
        if method == "GET" and url.endswith("/installation"):
            owner = url.split("/orgs/")[-1].split("/")[0] if "/orgs/" in url else \
                url.split("/users/")[-1].split("/")[0]
            return _Resp(200, {"id": 1, "account": {"login": owner}})
        if method == "POST" and url.endswith("/access_tokens"):
            body = json.loads(req.data)
            if mint_status != 201:
                return _Resp(mint_status, {"message": "Validation Failed"})
            return _Resp(201, {"token": "ghs_test", "permissions": body.get("permissions"),
                               "repository_selection": "selected",
                               "expires_at": "2026-10-17T12:00:00Z"})
        if method == "DELETE" and url.endswith("/installation/token"):
            return _Resp(204, {})
        if method == "GET" and "/contents/.github/workflows" in url:
            repo_part = url.split("/repos/", 1)[1].split("/contents/", 1)[0]
            slugs_seen.append(repo_part)
            if contents_status != 200:
                return _Resp(contents_status, {"message": "error"})
            return _Resp(200, workflow_items if workflow_items is not None else [])
        if method == "POST" and url.endswith("/registry/crews"):
            body = json.loads(req.data)
            row = {k: v for k, v in body.items() if k != "slug"}
            row["updated_at"] = "2026-10-26T00:00:00Z"
            row["updated_by"] = "person_01TESTBOSUNWRITER00000000"
            return _Resp(201, {**row, "slug": body.get("slug")})
        raise AssertionError(f"unexpected {method} {url}")
    return opener, calls, slugs_seen


def _register(spec, opener, monkeypatch, rsa_key, tmp_path):
    monkeypatch.setattr(GA, "installed_app",
                        lambda: GA.InstalledApp(app_id="1234567", key_path=rsa_key,
                                                slug="test-app"))
    monkeypatch.setattr(GA, "mint_log_path", lambda: tmp_path / "mint-log.jsonl")
    return CR.register(spec, opener=opener)


# --- the written row carries github_owner -----------------------------------


def test_the_written_row_carries_github_owner(writer_token, rsa_key, tmp_path, monkeypatch):
    opener, _, _ = _fake_transport()
    row = _register(_spec(), opener, monkeypatch, rsa_key, tmp_path)
    assert row["github_owner"] == OWNER


# --- every read targets <account>/<repo> ------------------------------------


def test_every_github_read_targets_the_given_owner(
        writer_token, rsa_key, tmp_path, monkeypatch):
    opener, _, slugs_seen = _fake_transport(workflow_items=[
        {"name": "ci.yml", "type": "file"},
    ])
    row = _register(_spec(), opener, monkeypatch, rsa_key, tmp_path)
    assert row["ci_workflows"] == {"widget": ["ci.yml"]}
    assert slugs_seen, "no GitHub read was observed"
    for slug in slugs_seen:
        assert slug == f"{OWNER}/widget", f"a read targeted {slug!r}, not {OWNER}/widget"


# --- a bad account name is refused before any request -----------------------


@pytest.mark.parametrize("bad_owner", [
    "-acme", "acme-", "acme--person", "acme person", "acme_person", "",
    "a" * 40,
])
def test_a_bad_github_owner_is_refused_before_any_request(
        bad_owner, writer_token, rsa_key, tmp_path, monkeypatch):
    opener, calls, _ = _fake_transport()
    with pytest.raises(CR.RegistrationRefused, match="github_owner"):
        _register(_spec(github_owner=bad_owner), opener, monkeypatch, rsa_key, tmp_path)
    assert calls == [], "a bad github_owner must refuse before any GitHub or Library call"


# --- the empty-repository case registers with no ci_workflows ---------------


def test_an_empty_repository_registers_with_no_ci_workflows(
        writer_token, rsa_key, tmp_path, monkeypatch):
    """GitHub's contents API answers 404 "This repository is empty." for a
    repo with no default branch — the same status it answers for "no such
    directory" on a non-empty repo. Confirmed against real public GitHub
    repositories (not merely documentation): testdouble/contributing-tests
    (non-empty, missing `.github/workflows`) and two genuinely empty repos,
    libredesktop/libredesktop-events and meilix/meilix-art — both the repo
    root and the `.github/workflows` path on the two empty repos answer
    HTTP 404 with `{"message": "This repository is empty."}`.
    `_repo_ci_workflows` already treats 404 as "no workflows"; this exercises
    that path end to end with the owner-routing this item adds."""
    opener, _, _ = _fake_transport(contents_status=404)
    row = _register(_spec(), opener, monkeypatch, rsa_key, tmp_path)
    assert row["ci_workflows"] == {"widget": []}


# --- a missing repository still refuses --------------------------------------


def test_a_missing_repository_still_refuses(writer_token, rsa_key, tmp_path, monkeypatch):
    """The real incident (2026-10-29): scoping an installation token to a
    repository name the installation does not have answers 422 from GitHub's
    `POST .../access_tokens` — the occurrence that names this status, not a
    guess. `GA.installation_token_payload` already raises `Unreadable` on any
    non-201, and `_repo_ci_workflows` already converts that into
    `RegistrationRefused` before any Library call."""
    opener, calls, _ = _fake_transport(mint_status=422)
    with pytest.raises(CR.RegistrationRefused, match="widget"):
        _register(_spec(), opener, monkeypatch, rsa_key, tmp_path)
    assert not any(u.endswith("/registry/crews") for u in calls), (
        "the Library must not be asked when a repo cannot be confirmed")


# --- no option means the org slug --------------------------------------------


def test_no_option_means_the_org_slug(writer_token, rsa_key, tmp_path, monkeypatch):
    opener, _, slugs_seen = _fake_transport(workflow_items=[{"name": "ci.yml", "type": "file"}])
    row = _register(_spec(github_owner=None), opener, monkeypatch, rsa_key, tmp_path)
    assert "github_owner" not in row
    assert slugs_seen, "no GitHub read was observed"
    for slug in slugs_seen:
        assert slug == "convivy/widget"


def test_build_row_omits_github_owner_when_not_given():
    row = CR.build_row(_spec(github_owner=None))
    assert "github_owner" not in row
    assert projects.repo_github_slug("widget", unit=row) == "convivy/widget"


# --- CLI wiring: --github-owner reaches the written row ---------------------


def _cli_args(**overrides) -> argparse.Namespace:
    base = dict(
        slug="widget", display="Widget", repos=None, aliases=[],
        products=[], crew=None, owner="convivy", github_owner=OWNER,
        cls="project", kb="central", kb_project=None, home_dir=None, dirs=[],
        lifecycle="active", model_tier=None, task_text_public=False,
        private=False, data_local=False, agent_prefix=None,
        crew_cockpit=None, note=None, dry_run=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.fixture
def fake_post(monkeypatch):
    """`cmd_crew_register` goes through `CR.register` with no `opener` seam
    of its own — stub the GitHub read and the Library POST directly, the
    same split `tests/test_crew_registration.py`'s own `fake_post` fixture
    uses for its CLI-level tests."""
    monkeypatch.setattr(CR, "_repo_ci_workflows", lambda repo, row, **kw: [])
    posted: dict = {}

    def post(body, *, opener=None):
        posted["body"] = body
        return {**{k: v for k, v in body.items() if k != "slug"}, "slug": body["slug"]}

    monkeypatch.setattr(CR, "_post_crew_row", post)
    return posted


def test_cli_passes_github_owner_through_to_the_written_row(agentctl, fake_post):
    rc = agentctl.cmd_crew_register(_cli_args())
    assert rc == 0
    assert fake_post["body"]["github_owner"] == OWNER


def test_cli_without_the_option_omits_github_owner(agentctl, fake_post):
    rc = agentctl.cmd_crew_register(_cli_args(github_owner=None))
    assert rc == 0
    assert "github_owner" not in fake_post["body"]
