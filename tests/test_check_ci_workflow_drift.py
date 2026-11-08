"""`bin/check-ci-workflow-drift` (CQ·570, issue_01M5VPQ9Z92C58R3MPGDNCA7JM) —
the Bosun's read-only, convivy-bosun-App-authenticated check for a
registered repo whose declared `ci_workflows` (config/projects.json)
disagree with the pull-request workflow files GitHub actually lists for it.

No network and no `gh` binary anywhere in this file. `GA.request` is the one
I/O seam (mirroring `test_ci_result_capture.py`'s own `opener`/`GA.request`
fakes) — every test here either calls a pure transform directly or drives
`main()` end to end with a fake GitHub and a fake App-credential layer.

Every name below (repo, owner, product) is synthetic: no real crew, repo,
machine or person name appears anywhere in this file.
"""
from __future__ import annotations

import base64
import json
import urllib.parse
from pathlib import Path


# --- registry + GitHub fixtures ----------------------------------------------


def _write_registry(path, units: dict) -> None:
    path.write_text(json.dumps(units))


def _file_payload(text: str) -> dict:
    return {"content": base64.b64encode(text.encode()).decode(), "encoding": "base64"}


_CI_YML_PULL_REQUEST = "on:\n  pull_request:\n  push:\njobs:\n  build:\n    runs-on: ubuntu-latest\n"
_PUSH_ONLY_YML = "on:\n  push:\njobs:\n  build:\n    runs-on: ubuntu-latest\n"
_SCHEDULE_ONLY_YML = ("on:\n  schedule:\n    - cron: '0 6 * * *'\n  workflow_dispatch:\n"
                     "jobs:\n  build:\n    runs-on: ubuntu-latest\n")
_PATH_FILTERED_PULL_REQUEST_YML = (
    "on:\n  pull_request:\n    paths:\n      - 'scripts/one/**'\n"
    "jobs:\n  build:\n    runs-on: ubuntu-latest\n")
_BRANCHES_ONLY_PULL_REQUEST_YML = (
    "on:\n  pull_request:\n    branches:\n      - main\n"
    "jobs:\n  build:\n    runs-on: ubuntu-latest\n")
# The GitHub-Actions YAML gotcha this module's own docstring explains: an
# UNQUOTED `on:` key is read by YAML 1.1 (what `yaml.safe_load` implements)
# as the boolean `True`, not the string `"on"`.
_BARE_ON_KEY_YML = "on: pull_request\njobs:\n  build:\n    runs-on: ubuntu-latest\n"


_INSTALLATION_REPOS_URL = "https://api.github.com/installation/repositories"


class _FakeGitHub:
    """Routes `GA.request("GET", url, token, None, **k)` by exact URL,
    against a `{full_repo: spec}` table. `spec is None` means GitHub answers
    404 for the repo itself (the "registered but not on GitHub" case); a
    present `spec` is `{"default_branch": ..., "workflows": {name: yaml_text}}`,
    `workflows` defaulting to `{}` (no `.github/workflows` directory, GitHub's
    own 404 on that one path alone, not the repo).

    Also answers `GET /installation/repositories` (CQ·576's own pre-check):
    *installed* names which keys of *repos* the installation covers,
    defaulting to every key whose `spec` is not `None` — so a test that
    never cares about installation coverage keeps writing `spec: None` for
    "not on GitHub" exactly as before. *page_size*, when given, splits
    *installed* across pages of that size instead of answering in one;
    `total_count` is always the full count, matching GitHub's own
    response shape."""

    def __init__(self, repos: dict, *, installed: set[str] | None = None,
                 page_size: int | None = None):
        self.repos = repos
        self.installed = (set(installed) if installed is not None
                          else {k for k, v in repos.items() if v is not None})
        self.page_size = page_size
        self.calls: list[str] = []

    def __call__(self, method, url, token, body, opener=None):
        assert method == "GET"
        self.calls.append(url)
        if url.startswith(_INSTALLATION_REPOS_URL):
            return self._installation_repos_page(url)
        for full_repo, spec in self.repos.items():
            base = f"https://api.github.com/repos/{full_repo}"
            if url == base:
                if spec is None:
                    return 404, {}
                return 200, {"default_branch": spec.get("default_branch", "main")}
            if spec is None:
                continue
            branch = spec.get("default_branch", "main")
            workflows = spec.get("workflows", {})
            if url == f"{base}/contents/.github/workflows?ref={branch}":
                if not workflows:
                    return 404, {}
                return 200, [{"name": n, "type": "file", "path": f".github/workflows/{n}"}
                            for n in sorted(workflows)]
            for name, text in workflows.items():
                if url == f"{base}/contents/.github/workflows/{name}?ref={branch}":
                    return 200, _file_payload(text)
        raise AssertionError(f"unexpected URL in this test's fake GitHub: {url!r}")

    def _installation_repos_page(self, url):
        names = sorted(self.installed)
        size = self.page_size or max(len(names), 1)
        page = int(urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("page", ["1"])[0])
        chunk = names[(page - 1) * size: page * size]
        return 200, {"total_count": len(names),
                    "repositories": [{"full_name": n} for n in chunk]}


def _stub_github_app(mod, monkeypatch, *, installed_owners=("owner-a",)):
    """`main()`'s App-credential layer, faked the same way
    `test_ci_result_capture.py::_stub_github_app` fakes it: a usable key, a
    JWT, and one installation per owner in *installed_owners* — any owner
    NOT in that set raises `GA.NotInstalled`, GitHub's own confirmation the
    App has no installation there."""
    monkeypatch.setattr(
        mod.GA, "installed_app",
        lambda: mod.GA.InstalledApp(app_id="1234567", key_path=Path("/dev/null"),
                                    slug="test-app"))
    monkeypatch.setattr(mod.GA, "key_problem", lambda *a, **k: None)
    monkeypatch.setattr(mod.GA, "app_jwt", lambda *a, **k: "jwt.x.y")
    monkeypatch.setattr(mod.GA, "installation_token_payload",
                        lambda *a, **k: {"token": "ghs_x",
                                        "expires_at": "2099-01-01T00:00:00Z"})

    def fake_installation_for_owner(jwt, owner, **k):
        if owner not in installed_owners:
            raise mod.GA.NotInstalled(f"the install's GitHub App is not installed on {owner!r}")
        return {"id": 1}

    monkeypatch.setattr(mod.GA, "installation_for_owner", fake_installation_for_owner)


def _set_registry(mod, monkeypatch, tmp_path, units: dict) -> None:
    path = tmp_path / "projects.json"
    _write_registry(path, units)
    monkeypatch.setattr(mod.PR, "PROJECTS_JSON", path)
    mod.PR._load_raw.cache_clear()


def _unit(repos, *, ci_workflows=None, owner="owner-a", lifecycle="active",
         private=False, product="widgets") -> dict:
    unit = {
        "class": "project", "display": "Test Crew", "owner": "test-owner",
        "home_dir": "<root>/test-crew", "dirs": [], "crew": "test-crew",
        "lifecycle": lifecycle, "products": [product],
        "repos": list(repos), "aliases": [], "kb": "local",
        "task_text_public": False, "overnight": False,
        "github_owner": owner,
    }
    if ci_workflows is not None:
        unit["ci_workflows"] = ci_workflows
    if private:
        unit["private"] = True
    return unit


# --- pure transforms: _pull_request_trigger_class ----------------------------


def test_trigger_class_reads_the_boolean_on_key_gotcha(check_ci_workflow_drift):
    mod = check_ci_workflow_drift
    doc = mod.yaml.safe_load(_BARE_ON_KEY_YML)
    assert mod._pull_request_trigger_class(doc) == mod.EVERY_PR


def test_trigger_class_every_pr_for_a_mapping_naming_it_with_no_filter(check_ci_workflow_drift):
    mod = check_ci_workflow_drift
    doc = mod.yaml.safe_load(_CI_YML_PULL_REQUEST)
    assert mod._pull_request_trigger_class(doc) == mod.EVERY_PR


def test_trigger_class_every_pr_for_a_list_naming_it(check_ci_workflow_drift):
    """A list-form `on:` carries no filter keys at all, so `pull_request`
    found there always counts as `EVERY_PR` — must fail against the
    unfixed code, which has no three-way classification to test at all."""
    mod = check_ci_workflow_drift
    doc = {"on": ["push", "pull_request"]}
    assert mod._pull_request_trigger_class(doc) == mod.EVERY_PR


def test_trigger_class_filtered_pr_for_a_paths_filter(check_ci_workflow_drift):
    mod = check_ci_workflow_drift
    doc = mod.yaml.safe_load(_PATH_FILTERED_PULL_REQUEST_YML)
    assert mod._pull_request_trigger_class(doc) == mod.FILTERED_PR


def test_trigger_class_filtered_pr_for_a_paths_ignore_filter(check_ci_workflow_drift):
    mod = check_ci_workflow_drift
    doc = {"on": {"pull_request": {"paths-ignore": ["docs/**"]}}}
    assert mod._pull_request_trigger_class(doc) == mod.FILTERED_PR


def test_trigger_class_every_pr_for_a_branches_filter(check_ci_workflow_drift):
    """Renamed from `test_trigger_class_filtered_pr_for_a_branches_filter`
    (asserted `FILTERED_PR`): `branches` narrows by target branch, not by
    what the pull request changed, so every pull request this check is
    about — which targets the repo's default branch — still runs it
    (issue_01M5Z5EN8TP04QBFM2Z7X8MPSJ / CQ·578). Must fail against
    f1a280bbe, which classifies this `FILTERED_PR`."""
    mod = check_ci_workflow_drift
    doc = {"on": {"pull_request": {"branches": ["main"]}}}
    assert mod._pull_request_trigger_class(doc) == mod.EVERY_PR


def test_trigger_class_every_pr_for_a_branches_filter_with_types(check_ci_workflow_drift):
    """Must fail against f1a280bbe, same reason as the plain `branches`
    case above."""
    mod = check_ci_workflow_drift
    doc = {"on": {"pull_request": {"branches": ["main"], "types": ["opened", "synchronize"]}}}
    assert mod._pull_request_trigger_class(doc) == mod.EVERY_PR


def test_trigger_class_every_pr_for_a_branches_ignore_filter(check_ci_workflow_drift):
    """Must fail against f1a280bbe, which classifies this `FILTERED_PR`."""
    mod = check_ci_workflow_drift
    doc = {"on": {"pull_request": {"branches-ignore": ["gh-pages"]}}}
    assert mod._pull_request_trigger_class(doc) == mod.EVERY_PR


def test_trigger_class_not_pr_for_push_only(check_ci_workflow_drift):
    mod = check_ci_workflow_drift
    doc = mod.yaml.safe_load(_PUSH_ONLY_YML)
    assert mod._pull_request_trigger_class(doc) == mod.NOT_PR


def test_trigger_class_not_pr_for_schedule_only(check_ci_workflow_drift):
    mod = check_ci_workflow_drift
    doc = mod.yaml.safe_load(_SCHEDULE_ONLY_YML)
    assert mod._pull_request_trigger_class(doc) == mod.NOT_PR


def test_trigger_class_not_pr_for_pull_request_target(check_ci_workflow_drift):
    """`pull_request_target` is a different trigger — this check is asked
    about `pull_request` alone (the module docstring)."""
    mod = check_ci_workflow_drift
    doc = {"on": ["pull_request_target"]}
    assert mod._pull_request_trigger_class(doc) == mod.NOT_PR


# --- declared_rows: which repos this check walks -----------------------------


def test_declared_rows_includes_a_repo_whose_ci_workflows_is_empty(
        check_ci_workflow_drift, monkeypatch, tmp_path):
    """The exact shape of the CQ·569 bug: a unit declaring `ci_workflows:
    {repo: []}` must still produce a row here (with `declared == ()`),
    never be silently dropped the way `ci_result_capture.tracked_pairs`
    drops it."""
    mod = check_ci_workflow_drift
    _set_registry(mod, monkeypatch, tmp_path, {
        "widget-crew": _unit(["widget-one"], ci_workflows={"widget-one": []}),
    })
    rows = mod.declared_rows()
    assert rows == ({"repo": "owner-a/widget-one", "product": "widget-crew",
                     "declared": ()},)


def test_declared_rows_defaults_to_ci_yml_when_unspecified(
        check_ci_workflow_drift, monkeypatch, tmp_path):
    mod = check_ci_workflow_drift
    _set_registry(mod, monkeypatch, tmp_path, {
        "widget-crew": _unit(["widget-one"]),
    })
    rows = mod.declared_rows()
    assert rows[0]["declared"] == ("ci.yml",)


def test_declared_rows_skips_a_private_unit(check_ci_workflow_drift, monkeypatch, tmp_path):
    mod = check_ci_workflow_drift
    _set_registry(mod, monkeypatch, tmp_path, {
        "widget-crew": _unit(["widget-one"]),
        "secret-crew": _unit(["secret-one"], private=True),
    })
    rows = mod.declared_rows()
    assert [r["repo"] for r in rows] == ["owner-a/widget-one"]


def test_declared_rows_skips_an_inactive_unit(check_ci_workflow_drift, monkeypatch, tmp_path):
    mod = check_ci_workflow_drift
    _set_registry(mod, monkeypatch, tmp_path, {
        "widget-crew": _unit(["widget-one"]),
        "paused-crew": _unit(["paused-one"], lifecycle="paused"),
    })
    rows = mod.declared_rows()
    assert [r["repo"] for r in rows] == ["owner-a/widget-one"]


# --- repo_listing: GitHub's own list -----------------------------------------


def test_repo_listing_returns_not_found_for_a_404_repo(check_ci_workflow_drift, monkeypatch):
    mod = check_ci_workflow_drift
    gh = _FakeGitHub({"owner-a/ghost-one": None})
    monkeypatch.setattr(mod.GA, "request", gh)

    status, detail, listed = mod.repo_listing("owner-a/ghost-one", "tok")

    assert status == "not_found"
    assert detail is None
    assert listed is False


def test_repo_listing_is_empty_when_there_is_no_workflows_directory(
        check_ci_workflow_drift, monkeypatch):
    mod = check_ci_workflow_drift
    gh = _FakeGitHub({"owner-a/bare-one": {"workflows": {}}})
    monkeypatch.setattr(mod.GA, "request", gh)

    status, detail, listed = mod.repo_listing("owner-a/bare-one", "tok")

    assert (status, detail) == ("ok", {})
    assert listed is False, (
        "a 404 on the directory path alone is not a 200 listing — it proves "
        "the repo has no workflows directory, not that contents:read works")


def test_repo_listing_classifies_each_workflow_files_own_trigger(
        check_ci_workflow_drift, monkeypatch):
    """`repo_listing` hands back every workflow file's own trigger class,
    never a bare pull-request-triggered name list — the caller needs each
    file's class to compare like with like."""
    mod = check_ci_workflow_drift
    gh = _FakeGitHub({"owner-a/mixed-one": {"workflows": {
        "ci.yml": _CI_YML_PULL_REQUEST,
        "release.yml": _PUSH_ONLY_YML,
        "scheduled.yml": _SCHEDULE_ONLY_YML,
        "scoped.yml": _PATH_FILTERED_PULL_REQUEST_YML,
    }}})
    monkeypatch.setattr(mod.GA, "request", gh)

    status, detail, listed = mod.repo_listing("owner-a/mixed-one", "tok")

    assert status == "ok"
    assert detail == {
        "ci.yml": mod.EVERY_PR,
        "release.yml": mod.NOT_PR,
        "scheduled.yml": mod.NOT_PR,
        "scoped.yml": mod.FILTERED_PR,
    }
    assert listed is True


def test_repo_listing_reports_a_non_404_listing_failure_as_an_error(
        check_ci_workflow_drift, monkeypatch):
    mod = check_ci_workflow_drift

    def gh(method, url, token, body, opener=None):
        return 403, {"message": "Forbidden"}

    monkeypatch.setattr(mod.GA, "request", gh)

    status, detail, listed = mod.repo_listing("owner-a/locked-one", "tok")

    assert status == "error"
    assert "403" in detail
    assert listed is False


# --- repo_listing: the real `GA.request`, a refresher, and the actual wire --


class _Resp:
    """Stands in for `http.client.HTTPResponse` — just enough for
    `github_app.request`'s own `with opener(req, timeout=30) as resp:
    resp.status, resp.read()`."""

    def __init__(self, status, payload):
        self.status = status
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_repo_listing_sends_the_refreshers_actual_token_not_its_object(
        check_ci_workflow_drift, monkeypatch):
    """On 0b81f5f8b `mint_tokens` stores the `GhTokenRefresher` object itself
    in `tokens[owner]`, and `repo_listing` hands that object straight to
    `GA.request` as `bearer`; `github_app.request` then builds the
    `Authorization` header as `f"Bearer {bearer}"` from the object's own
    `str()`, never its token. This drives `GA.request` for real (no
    `GA.request` monkeypatch, unlike every test above) so the header is
    built exactly as production builds it, and fails on 0b81f5f8b because
    the header carries the refresher's `repr()` rather than `"ghs_fixed"`."""
    mod = check_ci_workflow_drift

    monkeypatch.setattr(mod.GA, "installation_token_payload",
                        lambda *a, **k: {"token": "ghs_fixed",
                                        "expires_at": "2099-01-01T00:00:00Z"})
    refresher = mod.CRC.GhTokenRefresher(
        "jwt.x.y", "1", permissions=mod.CRC._READ_ONLY_PERMISSIONS)

    seen_tokens = []

    def opener(req, timeout=None):
        seen_tokens.append(req.get_header("Authorization"))
        url = req.full_url
        if url == "https://api.github.com/repos/owner-a/widget-one":
            return _Resp(200, {"default_branch": "main"})
        if url == ("https://api.github.com/repos/owner-a/widget-one"
                  "/contents/.github/workflows?ref=main"):
            return _Resp(200, [])
        raise AssertionError(f"unexpected URL: {url!r}")

    status, detail, listed = mod.repo_listing("owner-a/widget-one", refresher, opener=opener)

    assert (status, detail) == ("ok", {})
    assert listed is True
    assert seen_tokens == ["Bearer ghs_fixed", "Bearer ghs_fixed"]


def test_repo_listing_resolves_a_refreshers_token_fresh_at_each_call(
        check_ci_workflow_drift, monkeypatch):
    """A refresher whose held token is always past its own expiry (set once,
    in the past) re-mints on every `.token()` call — so three GitHub calls
    (the repo itself, the workflow directory, one workflow file) must carry
    three different tokens, never the one resolved at the top of the
    function and reused."""
    mod = check_ci_workflow_drift
    minted = []

    def fake_mint(jwt, inst_id, *, permissions=None, opener=None):
        minted.append(1)
        return {"token": f"ghs_{len(minted)}", "expires_at": "2000-01-01T00:00:00Z"}

    monkeypatch.setattr(mod.GA, "installation_token_payload", fake_mint)
    refresher = mod.CRC.GhTokenRefresher(
        "jwt.x.y", "1", permissions=mod.CRC._READ_ONLY_PERMISSIONS)

    seen_tokens = []

    def opener(req, timeout=None):
        seen_tokens.append(req.get_header("Authorization"))
        url = req.full_url
        if url == "https://api.github.com/repos/owner-a/widget-one":
            return _Resp(200, {"default_branch": "main"})
        if url == ("https://api.github.com/repos/owner-a/widget-one"
                  "/contents/.github/workflows?ref=main"):
            return _Resp(200, [{"name": "ci.yml", "type": "file"}])
        if url == ("https://api.github.com/repos/owner-a/widget-one"
                  "/contents/.github/workflows/ci.yml?ref=main"):
            return _Resp(200, _file_payload(_CI_YML_PULL_REQUEST))
        raise AssertionError(f"unexpected URL: {url!r}")

    status, detail, listed = mod.repo_listing("owner-a/widget-one", refresher, opener=opener)

    assert (status, detail) == ("ok", {"ci.yml": mod.EVERY_PR})
    assert listed is True
    assert seen_tokens == ["Bearer ghs_1", "Bearer ghs_2", "Bearer ghs_3"]


# --- mint_tokens --------------------------------------------------------------


def test_mint_tokens_reports_a_notinstalled_owner_as_unchecked(
        check_ci_workflow_drift, monkeypatch):
    mod = check_ci_workflow_drift

    def fake_installation_for_owner(jwt, owner, **k):
        if owner == "owner-b":
            raise mod.GA.NotInstalled(f"not installed on {owner!r}")
        return {"id": 1}

    monkeypatch.setattr(mod.GA, "installation_for_owner", fake_installation_for_owner)
    monkeypatch.setattr(mod.GA, "installation_token_payload",
                        lambda *a, **k: {"token": "ghs_x",
                                        "expires_at": "2099-01-01T00:00:00Z"})
    monkeypatch.setattr(mod.GA, "request", _FakeGitHub({}, installed={"owner-a/widget-one"}))

    tokens, unchecked, not_found = mod.mint_tokens(
        "jwt.x.y", {"owner-a": ["widget-one"], "owner-b": ["orphan-one"]})

    assert set(unchecked) == {"owner-b"}
    assert isinstance(unchecked["owner-b"], mod.GA.NotInstalled)
    assert set(tokens) == {"owner-a"}
    assert not_found == {}


def test_mint_tokens_mints_with_the_contents_read_permission(
        check_ci_workflow_drift, monkeypatch):
    """Drives the real mint call path — `mint_tokens` ->
    `CRC.GhTokenRefresher.token()` -> `CRC.GhTokenRefresher._mint()` ->
    `GA.installation_token_payload` — rather than stubbing the refresher
    itself out, so this fails if `mint_tokens` ever goes back to minting
    with `CRC._READ_ONLY_PERMISSIONS` (no `contents` key) the way CQ·570's
    own production run did."""
    mod = check_ci_workflow_drift
    monkeypatch.setattr(mod.GA, "installation_for_owner", lambda jwt, owner, **k: {"id": 1})
    monkeypatch.setattr(mod.GA, "request", _FakeGitHub({}, installed={"owner-a/widget-one"}))
    seen_permissions = []

    def fake_mint(jwt, inst_id, *, permissions=None, repositories=None, opener=None):
        seen_permissions.append(permissions)
        return {"token": "ghs_x", "expires_at": "2099-01-01T00:00:00Z"}

    monkeypatch.setattr(mod.GA, "installation_token_payload", fake_mint)

    tokens, unchecked, not_found = mod.mint_tokens("jwt.x.y", {"owner-a": ["widget-one"]})

    assert unchecked == {}
    assert not_found == {}
    assert set(tokens) == {"owner-a"}
    # one metadata-only mint (the installation-repos pre-check, CQ·576) and
    # one contents-read mint (the actual drift-reading token) per owner.
    assert len(seen_permissions) == 2
    assert seen_permissions[0] == mod._INSTALLATION_LIST_PERMISSIONS
    assert seen_permissions[1]["contents"] == "read"


def test_mint_tokens_scopes_the_token_to_only_the_owners_checked_repos(
        check_ci_workflow_drift, monkeypatch):
    """CQ·570 round 1's security finding: `contents: read` on an unscoped
    mint lets the token read every repo the installation can see, not just
    the ones this pass is checking. `owner_repos` must reach GitHub's own
    `repositories=` unchanged."""
    mod = check_ci_workflow_drift
    monkeypatch.setattr(mod.GA, "installation_for_owner", lambda jwt, owner, **k: {"id": 1})
    monkeypatch.setattr(mod.GA, "request", _FakeGitHub({}, installed={"owner-a/widget-one"}))
    seen_repositories = []

    def fake_mint(jwt, inst_id, *, permissions=None, repositories=None, opener=None):
        seen_repositories.append(repositories)
        return {"token": "ghs_x", "expires_at": "2099-01-01T00:00:00Z"}

    monkeypatch.setattr(mod.GA, "installation_token_payload", fake_mint)

    tokens, unchecked, not_found = mod.mint_tokens("jwt.x.y", {"owner-a": ["widget-one"]})

    assert unchecked == {}
    assert not_found == {}
    # the metadata pre-check mint is unscoped (`repositories=None`); only
    # the contents-read mint is scoped, to exactly the covered repos.
    assert seen_repositories == [None, ["widget-one"]]


# --- _installation_repos: which of an owner's repos the installation covers -


def test_installation_repos_pages_through_every_page(check_ci_workflow_drift, monkeypatch):
    """CQ·576: `GET /installation/repositories` is read page by page until
    the response's own `total_count` is reached — reading only the first
    page would wrongly leave a later page's repos out of *covered*, and
    `mint_tokens` would then report them `not_found` even though the
    installation does cover them. Must fail against 4a45b7217, which has
    no `_installation_repos` function at all."""
    mod = check_ci_workflow_drift
    monkeypatch.setattr(mod.GA, "installation_token_payload",
                        lambda *a, **k: {"token": "ghs_x", "expires_at": "2099-01-01T00:00:00Z"})
    gh = _FakeGitHub({}, installed={"owner-a/repo-one", "owner-a/repo-two"}, page_size=1)
    monkeypatch.setattr(mod.GA, "request", gh)

    covered = mod._installation_repos("jwt.x.y", "1")

    assert covered == {"owner-a/repo-one", "owner-a/repo-two"}
    pages = [c for c in gh.calls if c.startswith(_INSTALLATION_REPOS_URL)]
    assert len(pages) == 2


def test_mint_tokens_reports_a_failed_installation_listing_as_unchecked(
        check_ci_workflow_drift, monkeypatch):
    """CQ·576: when the installation's own repository listing fails (here,
    a plain 500 on every page), the owner is `unchecked` with that
    failure's own reason — never read as "none of this owner's repos are
    covered", which would wrongly report every repo `not_found` instead of
    refusing to compare it at all."""
    mod = check_ci_workflow_drift
    monkeypatch.setattr(mod.GA, "installation_for_owner", lambda jwt, owner, **k: {"id": 1})
    monkeypatch.setattr(mod.GA, "installation_token_payload",
                        lambda *a, **k: {"token": "ghs_x", "expires_at": "2099-01-01T00:00:00Z"})
    monkeypatch.setattr(mod.GA, "request",
                        lambda *a, **k: (500, {"message": "Internal Server Error"}))

    tokens, unchecked, not_found = mod.mint_tokens("jwt.x.y", {"owner-a": ["widget-one"]})

    assert tokens == {}
    assert not_found == {}
    assert set(unchecked) == {"owner-a"}
    assert "500" in str(unchecked["owner-a"])


def test_mint_tokens_reports_a_failed_metadata_mint_as_unchecked(
        check_ci_workflow_drift, monkeypatch):
    """CQ·576: the metadata-only mint `_installation_repos` needs can itself
    be refused by GitHub, before any listing call is even made — also
    `unchecked`, with that refusal's own reason, never `not_found`."""
    mod = check_ci_workflow_drift
    monkeypatch.setattr(mod.GA, "installation_for_owner", lambda jwt, owner, **k: {"id": 1})

    def fake_mint(jwt, inst_id, *, permissions=None, repositories=None, opener=None):
        raise mod.GA.Unreadable("GitHub refused an installation token (500): no message")

    monkeypatch.setattr(mod.GA, "installation_token_payload", fake_mint)

    tokens, unchecked, not_found = mod.mint_tokens("jwt.x.y", {"owner-a": ["widget-one"]})

    assert tokens == {}
    assert not_found == {}
    assert set(unchecked) == {"owner-a"}
    assert "500" in str(unchecked["owner-a"])


# --- _owner_repos: the per-owner scope mint_tokens is handed ------------------


def test_owner_repos_excludes_a_private_units_repo_sharing_the_public_ones_owner(
        check_ci_workflow_drift, monkeypatch, tmp_path):
    """The registry's live shape CQ·570 round 1 flagged: an active public
    unit and a `private: true` unit sharing one `github_owner`. The token
    minted to check the public repo must never be scoped to cover the
    private one too — `declared_rows` already excludes the private unit's
    repo, so it must never reach `_owner_repos` in the first place."""
    mod = check_ci_workflow_drift
    _set_registry(mod, monkeypatch, tmp_path, {
        "widget-crew": _unit(["widget-one"], ci_workflows={"widget-one": ["ci.yml"]}),
        "secret-crew": _unit(["secret-one"], private=True),
    })
    rows = mod.declared_rows()

    assert mod._owner_repos(rows) == {"owner-a": ["widget-one"]}


def test_main_scopes_the_mint_to_only_the_public_repo_when_a_private_unit_shares_the_owner(
        check_ci_workflow_drift, monkeypatch, tmp_path, capsys):
    """End to end: the installation-token mint for `owner-a` carries
    `repositories=["widget-one"]` alone — never `secret-one`, the
    `private: true` unit's repo sharing the same `github_owner`."""
    mod = check_ci_workflow_drift
    _stub_github_app(mod, monkeypatch)
    _set_registry(mod, monkeypatch, tmp_path, {
        "widget-crew": _unit(["widget-one"], ci_workflows={"widget-one": ["ci.yml"]}),
        "secret-crew": _unit(["secret-one"], private=True),
    })
    gh = _FakeGitHub({"owner-a/widget-one": {"workflows": {"ci.yml": _CI_YML_PULL_REQUEST}}})
    monkeypatch.setattr(mod.GA, "request", gh)
    seen_repositories = []

    def fake_mint(jwt, inst_id, *, permissions=None, repositories=None, opener=None):
        seen_repositories.append(repositories)
        return {"token": "ghs_x", "expires_at": "2099-01-01T00:00:00Z"}

    monkeypatch.setattr(mod.GA, "installation_token_payload", fake_mint)
    monkeypatch.setattr(mod.SC, "post_alert", lambda *a, **k: "created")
    monkeypatch.setattr(mod.sys, "argv", ["check-ci-workflow-drift"])

    rc = mod.main()

    assert rc == 0
    # the metadata pre-check mint (CQ·576) is unscoped (`repositories=None`);
    # the contents-read mint is the one scoped to `["widget-one"]` alone.
    assert seen_repositories == [None, ["widget-one"]]


def test_mint_tokens_is_never_called_for_an_owner_whose_units_are_all_skipped(
        check_ci_workflow_drift, monkeypatch, tmp_path):
    """An owner with no repo left to check after `declared_rows` excludes
    its skipped units gets no mint call at all — not even one GitHub asks
    to refuse."""
    mod = check_ci_workflow_drift
    _stub_github_app(mod, monkeypatch)
    _set_registry(mod, monkeypatch, tmp_path, {
        "widget-crew": _unit(["widget-one"], ci_workflows={"widget-one": ["ci.yml"]}),
        "secret-crew": _unit(["secret-one"], private=True, owner="owner-b"),
        "paused-crew": _unit(["paused-one"], lifecycle="paused", owner="owner-c"),
    })
    gh = _FakeGitHub({"owner-a/widget-one": {"workflows": {"ci.yml": _CI_YML_PULL_REQUEST}}})
    monkeypatch.setattr(mod.GA, "request", gh)
    minted_for = []

    def fake_installation_for_owner(jwt, owner, **k):
        minted_for.append(owner)
        return {"id": 1}

    monkeypatch.setattr(mod.GA, "installation_for_owner", fake_installation_for_owner)
    monkeypatch.setattr(mod.SC, "post_alert", lambda *a, **k: "created")
    monkeypatch.setattr(mod.sys, "argv", ["check-ci-workflow-drift"])

    rc = mod.main()

    assert rc == 0
    assert minted_for == ["owner-a"]


# --- _report: the alert it posts ---------------------------------------------


def _report_row(repo="owner-a/widget-one", product="widgets", declared=()):
    return {"repo": repo, "product": product, "declared": tuple(declared)}


def test_report_carries_its_title_in_the_posted_body(check_ci_workflow_drift, monkeypatch):
    mod = check_ci_workflow_drift
    posted = []
    monkeypatch.setattr(mod.SC, "post_alert",
                        lambda *a, **k: posted.append(a) or "created")

    mod._report(_report_row(), "drift", ["ci.yml"], "owner-a", "2026-10-26", dry_run=False)

    assert len(posted) == 1
    _stream, body = posted[0]
    assert body.startswith("[ci-workflow-drift] owner-a/widget-one: declared [] != GitHub")


def test_report_names_the_crew_registry_not_a_file(check_ci_workflow_drift, monkeypatch):
    mod = check_ci_workflow_drift
    posted = []
    monkeypatch.setattr(mod.SC, "post_alert",
                        lambda *a, **k: posted.append(a) or "created")

    mod._report(_report_row(), "drift", ["ci.yml"], "owner-a", "2026-10-26", dry_run=False)

    _stream, body = posted[0]
    assert "config/projects.json" not in body
    assert "crew registry" in body


# --- main(): end to end, GA and the wire both faked --------------------------


def test_main_reports_a_declared_empty_list_against_a_github_list_holding_ci_yml(
        check_ci_workflow_drift, monkeypatch, tmp_path, capsys):
    mod = check_ci_workflow_drift
    _stub_github_app(mod, monkeypatch)
    _set_registry(mod, monkeypatch, tmp_path, {
        "widget-crew": _unit(["widget-one"], ci_workflows={"widget-one": []}),
    })
    gh = _FakeGitHub({"owner-a/widget-one": {"workflows": {"ci.yml": _CI_YML_PULL_REQUEST}}})
    monkeypatch.setattr(mod.GA, "request", gh)
    posted = []
    monkeypatch.setattr(mod.SC, "post_alert",
                        lambda *a, **k: posted.append(k) or "created")
    monkeypatch.setattr(mod.sys, "argv", ["check-ci-workflow-drift"])

    rc = mod.main()

    assert rc == 0
    out = capsys.readouterr().out
    assert "owner-a/widget-one" in out
    assert "declared []" in out
    assert len(posted) == 1
    assert posted[0]["dedup_key"].startswith("ci-workflow-drift:owner-a/widget-one:drift:")


def test_main_does_not_report_a_match(check_ci_workflow_drift, monkeypatch, tmp_path, capsys):
    mod = check_ci_workflow_drift
    _stub_github_app(mod, monkeypatch)
    _set_registry(mod, monkeypatch, tmp_path, {
        "widget-crew": _unit(["widget-one"], ci_workflows={"widget-one": ["ci.yml"]}),
    })
    gh = _FakeGitHub({"owner-a/widget-one": {"workflows": {"ci.yml": _CI_YML_PULL_REQUEST}}})
    monkeypatch.setattr(mod.GA, "request", gh)
    posted = []
    monkeypatch.setattr(mod.SC, "post_alert", lambda *a, **k: posted.append(k) or "created")
    monkeypatch.setattr(mod.sys, "argv", ["check-ci-workflow-drift"])

    rc = mod.main()

    assert rc == 0
    assert posted == []
    assert "0 finding(s)" in capsys.readouterr().out


def test_main_reports_a_404_as_not_on_github_and_still_succeeds(
        check_ci_workflow_drift, monkeypatch, tmp_path, capsys):
    """A repo-level 404 is reported as "registered but not on GitHub" and
    never failed — but (CQ·570's own regression) a 404 proves nothing about
    whether the mint's `contents: read` grant still works, so it does not by
    itself keep the run's exit code at 0. A second repo under the same owner
    that DOES get a real 200 listing is what keeps this run's exit at 0;
    `test_main_exits_nonzero_when_no_repo_is_listed` below covers the ghost
    repo alone."""
    mod = check_ci_workflow_drift
    _stub_github_app(mod, monkeypatch)
    _set_registry(mod, monkeypatch, tmp_path, {
        "widget-crew": _unit(["ghost-one", "widget-one"],
                             ci_workflows={"ghost-one": ["ci.yml"],
                                          "widget-one": ["ci.yml"]}),
    })
    gh = _FakeGitHub({
        "owner-a/ghost-one": None,
        "owner-a/widget-one": {"workflows": {"ci.yml": _CI_YML_PULL_REQUEST}},
    })
    monkeypatch.setattr(mod.GA, "request", gh)
    posted = []
    monkeypatch.setattr(mod.SC, "post_alert", lambda *a, **k: posted.append(k) or "created")
    monkeypatch.setattr(mod.sys, "argv", ["check-ci-workflow-drift"])

    rc = mod.main()

    assert rc == 0
    out = capsys.readouterr().out
    assert "registered but not on GitHub" in out
    assert len(posted) == 1
    assert posted[0]["dedup_key"].startswith("ci-workflow-drift:owner-a/ghost-one:not_found:")


def test_main_reports_an_owner_with_no_installation_as_unchecked(
        check_ci_workflow_drift, monkeypatch, tmp_path, capsys):
    """A second repo under an INSTALLED owner that gets a real 200 listing
    is what keeps this run's exit at 0 (CQ·570 round 1: an owner whose mint
    fails is never queried and never counts toward `listed`, so an
    unchecked owner alone — covered by
    `test_main_exits_nonzero_when_every_owners_mint_fails` — now exits 1)."""
    mod = check_ci_workflow_drift
    _stub_github_app(mod, monkeypatch, installed_owners=("owner-a",))
    _set_registry(mod, monkeypatch, tmp_path, {
        "widget-crew": _unit(["widget-one"], ci_workflows={"widget-one": ["ci.yml"]}),
        "orphan-crew": _unit(["orphan-one"], ci_workflows={"orphan-one": ["ci.yml"]},
                            owner="owner-b"),
    })
    gh = _FakeGitHub({"owner-a/widget-one": {"workflows": {"ci.yml": _CI_YML_PULL_REQUEST}}})
    monkeypatch.setattr(mod.GA, "request", gh)
    posted = []
    monkeypatch.setattr(mod.SC, "post_alert", lambda *a, **k: posted.append(k) or "created")
    monkeypatch.setattr(mod.sys, "argv", ["check-ci-workflow-drift"])

    rc = mod.main()

    assert rc == 0
    out = capsys.readouterr().out
    assert "unchecked" in out
    assert "owner-b" in out
    assert "no convivy-bosun installation" in out, (
        "round 2 of CQ·570: the confirmed-no-installation case keeps this sentence")
    for call in gh.calls:
        assert "orphan-one" not in call, "an unchecked owner's repo must never be queried on GitHub"
    assert len(posted) == 1
    assert posted[0]["dedup_key"].startswith("ci-workflow-drift:owner-b/orphan-one:unchecked:")


def test_main_reports_a_repo_the_installation_does_not_cover_as_not_found_never_unchecked(
        check_ci_workflow_drift, monkeypatch, tmp_path, capsys):
    """CQ·576. Before this fix (4a45b7217), a repo the installation does not
    cover was learned only by GitHub refusing the WHOLE owner's scoped
    `contents: read` mint with 422 — which failed every repo under that
    owner, `steady-one` included, as "unchecked". This fix learns coverage
    first, through `GET /installation/repositories` (`owner-b`'s own
    installed set here excludes `orphan-one`, holds `steady-one`): the
    contents mint is scoped to `steady-one` alone, `orphan-one` is reported
    "registered but not on GitHub" and never attempted, and `steady-one`
    is still compared against GitHub. This must fail against 4a45b7217,
    which reports BOTH `owner-b` repos "unchecked" with a 422 reason."""
    mod = check_ci_workflow_drift
    _stub_github_app(mod, monkeypatch, installed_owners=("owner-a", "owner-b"))
    _set_registry(mod, monkeypatch, tmp_path, {
        "widget-crew": _unit(["widget-one"], ci_workflows={"widget-one": ["ci.yml"]}),
        "orphan-crew": _unit(["orphan-one", "steady-one"],
                            ci_workflows={"orphan-one": ["ci.yml"], "steady-one": ["ci.yml"]},
                            owner="owner-b"),
    })
    gh = _FakeGitHub({
        "owner-a/widget-one": {"workflows": {"ci.yml": _CI_YML_PULL_REQUEST}},
        "owner-b/steady-one": {"workflows": {"ci.yml": _CI_YML_PULL_REQUEST}},
    }, installed={"owner-a/widget-one", "owner-b/steady-one"})
    monkeypatch.setattr(mod.GA, "request", gh)
    seen_repositories = []

    def fake_mint(jwt, inst_id, *, permissions=None, repositories=None, opener=None):
        seen_repositories.append(repositories)
        return {"token": "ghs_x", "expires_at": "2099-01-01T00:00:00Z"}

    monkeypatch.setattr(mod.GA, "installation_token_payload", fake_mint)
    posted = []
    monkeypatch.setattr(mod.SC, "post_alert", lambda *a, **k: posted.append(k) or "created")
    monkeypatch.setattr(mod.sys, "argv", ["check-ci-workflow-drift"])

    rc = mod.main()

    assert rc == 0
    out = capsys.readouterr().out
    assert "owner-b/orphan-one: registered but not on GitHub" in out
    assert "unchecked" not in out
    for call in gh.calls:
        assert "orphan-one" not in call, "a not-covered repo must never be queried on GitHub"
    assert ["steady-one"] in seen_repositories
    assert ["orphan-one"] not in seen_repositories
    assert ["orphan-one", "steady-one"] not in seen_repositories
    assert len(posted) == 1
    assert posted[0]["dedup_key"].startswith("ci-workflow-drift:owner-b/orphan-one:not_found:")


def test_main_scopes_the_contents_mint_to_exactly_the_installations_covered_subset(
        check_ci_workflow_drift, monkeypatch, tmp_path, capsys):
    """CQ·576: of three repos registered under one owner, the installation's
    own listing covers two. The contents-read mint receives exactly those
    two bare names, the third is `not_found`, and the two covered repos
    are each compared against GitHub (both match here, through
    `_FakeGitHub`'s own `ci.yml`, so neither adds a `drift` finding). Must
    fail against 4a45b7217, which has no installation listing and instead
    scopes the one contents mint to all three names."""
    mod = check_ci_workflow_drift
    _stub_github_app(mod, monkeypatch)
    _set_registry(mod, monkeypatch, tmp_path, {
        "widget-crew": _unit(["repo-one", "repo-two", "repo-three"],
                             ci_workflows={"repo-one": ["ci.yml"], "repo-two": ["ci.yml"],
                                          "repo-three": ["ci.yml"]}),
    })
    gh = _FakeGitHub({
        "owner-a/repo-one": {"workflows": {"ci.yml": _CI_YML_PULL_REQUEST}},
        "owner-a/repo-two": {"workflows": {"ci.yml": _CI_YML_PULL_REQUEST}},
    }, installed={"owner-a/repo-one", "owner-a/repo-two"})
    monkeypatch.setattr(mod.GA, "request", gh)
    seen_repositories = []

    def fake_mint(jwt, inst_id, *, permissions=None, repositories=None, opener=None):
        seen_repositories.append(repositories)
        return {"token": "ghs_x", "expires_at": "2099-01-01T00:00:00Z"}

    monkeypatch.setattr(mod.GA, "installation_token_payload", fake_mint)
    posted = []
    monkeypatch.setattr(mod.SC, "post_alert", lambda *a, **k: posted.append(k) or "created")
    monkeypatch.setattr(mod.sys, "argv", ["check-ci-workflow-drift"])

    rc = mod.main()

    assert rc == 0
    assert seen_repositories[-1] == ["repo-one", "repo-two"]
    out = capsys.readouterr().out
    assert "owner-a/repo-three: registered but not on GitHub" in out
    for call in gh.calls:
        assert "repo-three" not in call
    assert len(posted) == 1
    assert posted[0]["dedup_key"].startswith("ci-workflow-drift:owner-a/repo-three:not_found:")


def test_main_exits_nonzero_when_the_installation_covers_none_of_an_owners_repos(
        check_ci_workflow_drift, monkeypatch, tmp_path, capsys):
    """CQ·576: an owner whose installation covers none of its registered
    repos gets no contents mint at all; every one of its repos is
    `not_found`, and with no other owner's repo listed this pass, the run
    exits 1 — a pass where every repo came back `not_found` proved nothing
    about whether `contents: read` still works. Must fail against
    4a45b7217, which has no installation listing and would instead mint a
    `contents: read` token scoped to both repos (this fake does not
    refuse it, unlike real GitHub) and report them matched, exiting 0."""
    mod = check_ci_workflow_drift
    _stub_github_app(mod, monkeypatch)
    _set_registry(mod, monkeypatch, tmp_path, {
        "widget-crew": _unit(["ghost-one", "ghost-two"],
                             ci_workflows={"ghost-one": ["ci.yml"], "ghost-two": ["ci.yml"]}),
    })
    gh = _FakeGitHub({}, installed=set())
    monkeypatch.setattr(mod.GA, "request", gh)
    posted = []
    monkeypatch.setattr(mod.SC, "post_alert", lambda *a, **k: posted.append(k) or "created")
    monkeypatch.setattr(mod.sys, "argv", ["check-ci-workflow-drift"])

    rc = mod.main()

    out = capsys.readouterr().out
    assert "owner-a/ghost-one: registered but not on GitHub" in out
    assert "owner-a/ghost-two: registered but not on GitHub" in out
    assert "unchecked" not in out
    for call in gh.calls:
        assert "ghost-one" not in call and "ghost-two" not in call
    assert len(posted) == 2
    assert rc == 1


def test_main_skips_a_private_unit_and_an_inactive_unit(
        check_ci_workflow_drift, monkeypatch, tmp_path, capsys):
    mod = check_ci_workflow_drift
    _stub_github_app(mod, monkeypatch)
    _set_registry(mod, monkeypatch, tmp_path, {
        "widget-crew": _unit(["widget-one"], ci_workflows={"widget-one": ["ci.yml"]}),
        "secret-crew": _unit(["secret-one"], private=True),
        "paused-crew": _unit(["paused-one"], lifecycle="paused"),
    })
    gh = _FakeGitHub({"owner-a/widget-one": {"workflows": {"ci.yml": _CI_YML_PULL_REQUEST}}})
    monkeypatch.setattr(mod.GA, "request", gh)
    monkeypatch.setattr(mod.SC, "post_alert", lambda *a, **k: "created")
    monkeypatch.setattr(mod.sys, "argv", ["check-ci-workflow-drift"])

    rc = mod.main()

    assert rc == 0
    for call in gh.calls:
        assert "secret-one" not in call
        assert "paused-one" not in call
    assert "0 finding(s) across 1 repo(s)" in capsys.readouterr().out


def test_main_excludes_a_non_pull_request_workflow_from_githubs_list(
        check_ci_workflow_drift, monkeypatch, tmp_path, capsys):
    """A repo declaring only `ci.yml` whose GitHub-side files also include a
    push-only workflow is a MATCH, not a drift — the push-only file is
    excluded from GitHub's own list before the two are compared."""
    mod = check_ci_workflow_drift
    _stub_github_app(mod, monkeypatch)
    _set_registry(mod, monkeypatch, tmp_path, {
        "widget-crew": _unit(["widget-one"], ci_workflows={"widget-one": ["ci.yml"]}),
    })
    gh = _FakeGitHub({"owner-a/widget-one": {"workflows": {
        "ci.yml": _CI_YML_PULL_REQUEST,
        "release.yml": _PUSH_ONLY_YML,
    }}})
    monkeypatch.setattr(mod.GA, "request", gh)
    posted = []
    monkeypatch.setattr(mod.SC, "post_alert", lambda *a, **k: posted.append(k) or "created")
    monkeypatch.setattr(mod.sys, "argv", ["check-ci-workflow-drift"])

    rc = mod.main()

    assert rc == 0
    assert posted == []
    assert "0 finding(s)" in capsys.readouterr().out


# --- schedule-only and path-filtered workflows: like with like --------------


def test_main_raises_no_finding_for_a_declared_branches_filtered_workflow(
        check_ci_workflow_drift, monkeypatch, tmp_path, capsys):
    """A declared workflow whose `pull_request` trigger is only
    `{branches: [main]}` is `EVERY_PR`, not `FILTERED_PR`: every pull
    request targets a repo's default branch, so none is excluded, and the
    match raises no finding at all — neither drift nor `path_filtered`.
    Must fail against f1a280bbe, which classifies this `FILTERED_PR` and
    reports it `path_filtered`."""
    mod = check_ci_workflow_drift
    _stub_github_app(mod, monkeypatch)
    _set_registry(mod, monkeypatch, tmp_path, {
        "widget-crew": _unit(["widget-one"], ci_workflows={"widget-one": ["ci.yml"]}),
    })
    gh = _FakeGitHub({"owner-a/widget-one": {"workflows": {
        "ci.yml": _BRANCHES_ONLY_PULL_REQUEST_YML,
    }}})
    monkeypatch.setattr(mod.GA, "request", gh)
    posted = []
    monkeypatch.setattr(mod.SC, "post_alert", lambda *a, **k: posted.append(k) or "created")
    monkeypatch.setattr(mod.sys, "argv", ["check-ci-workflow-drift"])

    rc = mod.main()

    assert rc == 0
    assert posted == []
    assert "0 finding(s)" in capsys.readouterr().out


def test_main_raises_no_drift_for_a_declared_schedule_only_workflow(
        check_ci_workflow_drift, monkeypatch, tmp_path, capsys):
    """A repo declaring a schedule-only (`NOT_PR`) workflow alongside
    `ci.yml` is a match — the schedule-only file is never required to run
    on every pull request, so it never counts against the declared list.
    Must fail against the unfixed code, which folds a declared name
    present on GitHub but not `pull_request`-triggered into the same
    mismatch it uses for a genuinely missing file."""
    mod = check_ci_workflow_drift
    _stub_github_app(mod, monkeypatch)
    _set_registry(mod, monkeypatch, tmp_path, {
        "widget-crew": _unit(["widget-one"],
                             ci_workflows={"widget-one": ["ci.yml", "scheduled.yml"]}),
    })
    gh = _FakeGitHub({"owner-a/widget-one": {"workflows": {
        "ci.yml": _CI_YML_PULL_REQUEST,
        "scheduled.yml": _SCHEDULE_ONLY_YML,
    }}})
    monkeypatch.setattr(mod.GA, "request", gh)
    posted = []
    monkeypatch.setattr(mod.SC, "post_alert", lambda *a, **k: posted.append(k) or "created")
    monkeypatch.setattr(mod.sys, "argv", ["check-ci-workflow-drift"])

    rc = mod.main()

    assert rc == 0
    assert posted == []
    assert "0 finding(s)" in capsys.readouterr().out


def test_main_reports_drift_for_a_declared_workflow_with_no_file_on_github(
        check_ci_workflow_drift, monkeypatch, tmp_path, capsys):
    """A declared name with no file on GitHub at all is drift, whatever
    else GitHub does list — this is unaffected by the classification fix,
    and must still fail against a change that stops comparing at all."""
    mod = check_ci_workflow_drift
    _stub_github_app(mod, monkeypatch)
    _set_registry(mod, monkeypatch, tmp_path, {
        "widget-crew": _unit(["widget-one"],
                             ci_workflows={"widget-one": ["ci.yml", "missing.yml"]}),
    })
    gh = _FakeGitHub({"owner-a/widget-one": {"workflows": {"ci.yml": _CI_YML_PULL_REQUEST}}})
    monkeypatch.setattr(mod.GA, "request", gh)
    posted = []
    monkeypatch.setattr(mod.SC, "post_alert", lambda *a, **k: posted.append(k) or "created")
    monkeypatch.setattr(mod.sys, "argv", ["check-ci-workflow-drift"])

    rc = mod.main()

    assert rc == 0
    out = capsys.readouterr().out
    assert "declared ['ci.yml', 'missing.yml'] != GitHub ['ci.yml']" in out
    assert len(posted) == 1
    assert posted[0]["meta"]["finding"] == "drift"


def test_main_reports_an_undeclared_path_filtered_workflow_as_path_filtered_not_drift(
        check_ci_workflow_drift, monkeypatch, tmp_path, capsys):
    """An undeclared workflow whose `pull_request` trigger carries a
    `paths` filter gets its own `path_filtered` finding and raises no
    drift — requiring it on every pull request would leave one outside
    the path waiting on a check that never runs. Must fail against the
    unfixed code, which has no `path_filtered` finding kind at all and
    would read this as an `EVERY_PR` file, reporting drift instead."""
    mod = check_ci_workflow_drift
    _stub_github_app(mod, monkeypatch)
    _set_registry(mod, monkeypatch, tmp_path, {
        "widget-crew": _unit(["widget-one"], ci_workflows={"widget-one": ["ci.yml"]}),
    })
    gh = _FakeGitHub({"owner-a/widget-one": {"workflows": {
        "ci.yml": _CI_YML_PULL_REQUEST,
        "scoped.yml": _PATH_FILTERED_PULL_REQUEST_YML,
    }}})
    monkeypatch.setattr(mod.GA, "request", gh)
    posted = []
    monkeypatch.setattr(mod.SC, "post_alert", lambda *a, **k: posted.append(k) or "created")
    monkeypatch.setattr(mod.sys, "argv", ["check-ci-workflow-drift"])

    rc = mod.main()

    assert rc == 0
    out = capsys.readouterr().out
    assert "scoped.yml is undeclared" in out
    assert "never compared for drift" in out
    assert len(posted) == 1
    assert posted[0]["meta"]["finding"] == "path_filtered"


def test_main_reports_a_declared_path_filtered_workflow_as_path_filtered_not_drift(
        check_ci_workflow_drift, monkeypatch, tmp_path, capsys):
    """The same path-filtered workflow, declared this time: still
    `path_filtered`, not drift, and the line says `declared` rather than
    `undeclared` — the exact shape of the two false rows PR #1046's first
    post-merge run produced (issue_01M5Z5EN8TP04QBFM2Z7X8MPSJ)."""
    mod = check_ci_workflow_drift
    _stub_github_app(mod, monkeypatch)
    _set_registry(mod, monkeypatch, tmp_path, {
        "widget-crew": _unit(["widget-one"],
                             ci_workflows={"widget-one": ["ci.yml", "scoped.yml"]}),
    })
    gh = _FakeGitHub({"owner-a/widget-one": {"workflows": {
        "ci.yml": _CI_YML_PULL_REQUEST,
        "scoped.yml": _PATH_FILTERED_PULL_REQUEST_YML,
    }}})
    monkeypatch.setattr(mod.GA, "request", gh)
    posted = []
    monkeypatch.setattr(mod.SC, "post_alert", lambda *a, **k: posted.append(k) or "created")
    monkeypatch.setattr(mod.sys, "argv", ["check-ci-workflow-drift"])

    rc = mod.main()

    assert rc == 0
    out = capsys.readouterr().out
    assert "scoped.yml is declared" in out
    assert "never compared for drift" in out
    assert len(posted) == 1
    assert posted[0]["meta"]["finding"] == "path_filtered"


def test_main_reports_an_undeclared_unfiltered_pull_request_workflow_as_drift(
        check_ci_workflow_drift, monkeypatch, tmp_path, capsys):
    """An undeclared `EVERY_PR` file is still drift — the comparison only
    stops treating a filtered or non-PR file this way, never a plain
    unfiltered `pull_request` trigger."""
    mod = check_ci_workflow_drift
    _stub_github_app(mod, monkeypatch)
    _set_registry(mod, monkeypatch, tmp_path, {
        "widget-crew": _unit(["widget-one"], ci_workflows={"widget-one": ["ci.yml"]}),
    })
    gh = _FakeGitHub({"owner-a/widget-one": {"workflows": {
        "ci.yml": _CI_YML_PULL_REQUEST,
        "second.yml": _CI_YML_PULL_REQUEST,
    }}})
    monkeypatch.setattr(mod.GA, "request", gh)
    posted = []
    monkeypatch.setattr(mod.SC, "post_alert", lambda *a, **k: posted.append(k) or "created")
    monkeypatch.setattr(mod.sys, "argv", ["check-ci-workflow-drift"])

    rc = mod.main()

    assert rc == 0
    out = capsys.readouterr().out
    assert "declared ['ci.yml'] != GitHub ['ci.yml', 'second.yml']" in out
    assert len(posted) == 1
    assert posted[0]["meta"]["finding"] == "drift"


def test_main_raises_nothing_for_an_undeclared_pull_request_target_workflow(
        check_ci_workflow_drift, monkeypatch, tmp_path, capsys):
    """`pull_request_target` is `NOT_PR` — undeclared and unreported,
    exactly like a push-only or schedule-only file."""
    mod = check_ci_workflow_drift
    _stub_github_app(mod, monkeypatch)
    _set_registry(mod, monkeypatch, tmp_path, {
        "widget-crew": _unit(["widget-one"], ci_workflows={"widget-one": ["ci.yml"]}),
    })
    gh = _FakeGitHub({"owner-a/widget-one": {"workflows": {
        "ci.yml": _CI_YML_PULL_REQUEST,
        "fork.yml": "on:\n  pull_request_target:\njobs:\n  build:\n    runs-on: ubuntu-latest\n",
    }}})
    monkeypatch.setattr(mod.GA, "request", gh)
    posted = []
    monkeypatch.setattr(mod.SC, "post_alert", lambda *a, **k: posted.append(k) or "created")
    monkeypatch.setattr(mod.sys, "argv", ["check-ci-workflow-drift"])

    rc = mod.main()

    assert rc == 0
    assert posted == []
    assert "0 finding(s)" in capsys.readouterr().out


def test_main_reports_an_erroring_repo_as_could_not_be_checked(
        check_ci_workflow_drift, monkeypatch, tmp_path, capsys):
    """One repo's workflow listing comes back 403 while another repo under
    the same INSTALLED owner succeeds: the 403 is its own finding, through
    `_report`, and (the exit rule added 2026-10-27) it fails the run anyway
    — `owner-a`'s installation is confirmed, so a repo of its that "could
    not be checked" is never excused by a sibling repo that matched."""
    mod = check_ci_workflow_drift
    _stub_github_app(mod, monkeypatch)
    _set_registry(mod, monkeypatch, tmp_path, {
        "widget-crew": _unit(["widget-one", "flaky-one"],
                             ci_workflows={"widget-one": ["ci.yml"], "flaky-one": ["ci.yml"]}),
    })
    fake_ok = _FakeGitHub({"owner-a/widget-one": {"workflows": {"ci.yml": _CI_YML_PULL_REQUEST}}},
                         installed={"owner-a/widget-one", "owner-a/flaky-one"})

    def gh(method, url, token, body, opener=None):
        if "flaky-one" in url:
            return 403, {"message": "Forbidden"}
        return fake_ok(method, url, token, body, opener=opener)

    monkeypatch.setattr(mod.GA, "request", gh)
    posted = []
    monkeypatch.setattr(mod.SC, "post_alert", lambda *a, **k: posted.append(k) or "created")
    monkeypatch.setattr(mod.sys, "argv", ["check-ci-workflow-drift"])

    rc = mod.main()

    out = capsys.readouterr().out
    assert "owner-a/flaky-one: could not be checked" in out
    assert rc != 0, "an installed owner's repo that could not be checked must fail the run"
    assert len(posted) == 1
    assert posted[0]["dedup_key"].startswith("ci-workflow-drift:owner-a/flaky-one:error:")


# --- the exit rule: an installed owner's own unchecked/error findings -------


def test_main_exits_nonzero_when_an_installed_owners_contents_mint_refuses(
        check_ci_workflow_drift, monkeypatch, tmp_path, capsys):
    """`owner-a`'s installation is confirmed and its installation-repository
    listing confirms it covers `widget-one`, but the contents-read mint
    itself is refused (422) — `unchecked`, with a plain `GA.Unreadable`,
    never a `GA.NotInstalled`. `owner-b`'s repo is listed and matches. This
    is the production shape (a run that read 4 of 34 repos still exited 0,
    the steward, 2026-10-27): `rows and not listed` alone does not trigger,
    since `owner-b`'s repo WAS listed, so the exit rule is what must fail
    this run. Must fail against 4a45b7217, whose exit code only looks at
    whether anything at all was listed."""
    mod = check_ci_workflow_drift
    _stub_github_app(mod, monkeypatch, installed_owners=("owner-a", "owner-b"))
    _set_registry(mod, monkeypatch, tmp_path, {
        "widget-crew": _unit(["widget-one"], ci_workflows={"widget-one": ["ci.yml"]}),
        "steady-crew": _unit(["steady-one"], ci_workflows={"steady-one": ["ci.yml"]},
                            owner="owner-b"),
    })
    gh = _FakeGitHub({"owner-b/steady-one": {"workflows": {"ci.yml": _CI_YML_PULL_REQUEST}}},
                     installed={"owner-a/widget-one", "owner-b/steady-one"})
    monkeypatch.setattr(mod.GA, "request", gh)

    def fake_mint(jwt, inst_id, *, permissions=None, repositories=None, opener=None):
        if permissions and "contents" in permissions and repositories == ["widget-one"]:
            raise mod.GA.Unreadable("GitHub refused an installation token (422): no good")
        return {"token": "ghs_x", "expires_at": "2099-01-01T00:00:00Z"}

    monkeypatch.setattr(mod.GA, "installation_token_payload", fake_mint)
    posted = []
    monkeypatch.setattr(mod.SC, "post_alert", lambda *a, **k: posted.append(k) or "created")
    monkeypatch.setattr(mod.sys, "argv", ["check-ci-workflow-drift"])

    rc = mod.main()

    out = capsys.readouterr().out
    assert "owner-a/widget-one: unchecked" in out
    assert "no convivy-bosun installation" not in out
    assert rc != 0
    assert len(posted) == 1


def test_main_exits_nonzero_when_an_owners_installation_lookup_is_ambiguous(
        check_ci_workflow_drift, monkeypatch, tmp_path, capsys):
    """`owner-a`'s installation lookup itself fails ambiguously (a plain
    `GA.Unreadable`, e.g. a 500 on both the org and user lookup) — neither
    a confirmed installation nor GitHub's own confirmed absence
    (`GA.NotInstalled`). This must fail the run exactly like any other
    unchecked repo (exit-code case 3, module docstring), not read as the
    confirmed-no-installation case. `owner-b`'s repo is listed and
    matches, so `rows and not listed` alone (case 2) does not trigger and
    the exit rule is what must fail this run."""
    mod = check_ci_workflow_drift
    monkeypatch.setattr(
        mod.GA, "installed_app",
        lambda: mod.GA.InstalledApp(app_id="1234567", key_path=Path("/dev/null"),
                                    slug="test-app"))
    monkeypatch.setattr(mod.GA, "key_problem", lambda *a, **k: None)
    monkeypatch.setattr(mod.GA, "app_jwt", lambda *a, **k: "jwt.x.y")
    _set_registry(mod, monkeypatch, tmp_path, {
        "widget-crew": _unit(["widget-one"], ci_workflows={"widget-one": ["ci.yml"]}),
        "steady-crew": _unit(["steady-one"], ci_workflows={"steady-one": ["ci.yml"]},
                            owner="owner-b"),
    })
    gh = _FakeGitHub({"owner-b/steady-one": {"workflows": {"ci.yml": _CI_YML_PULL_REQUEST}}},
                     installed={"owner-b/steady-one"})
    monkeypatch.setattr(mod.GA, "request", gh)
    monkeypatch.setattr(mod.GA, "installation_token_payload",
                        lambda *a, **k: {"token": "ghs_x", "expires_at": "2099-01-01T00:00:00Z"})

    def fake_installation_for_owner(jwt, owner, **k):
        if owner == "owner-a":
            raise mod.GA.Unreadable(f"{owner!r}'s installation could not be read (500)")
        return {"id": 1}

    monkeypatch.setattr(mod.GA, "installation_for_owner", fake_installation_for_owner)
    posted = []
    monkeypatch.setattr(mod.SC, "post_alert", lambda *a, **k: posted.append(k) or "created")
    monkeypatch.setattr(mod.sys, "argv", ["check-ci-workflow-drift"])

    rc = mod.main()

    out = capsys.readouterr().out
    assert "owner-a/widget-one: unchecked" in out
    assert "no convivy-bosun installation" not in out
    assert rc != 0
    assert len(posted) == 1


def test_main_exits_zero_when_findings_are_only_not_found_and_notinstalled(
        check_ci_workflow_drift, monkeypatch, tmp_path, capsys):
    """`owner-b` is confirmed to have no convivy-bosun installation at all
    (`GA.NotInstalled`), and `owner-c`'s own installation-repository listing
    confirms it does not cover `ghost-one` (`not_found`) — neither finding
    is ever a failure on its own. `owner-a`'s repo is listed and matches,
    so the run exits 0."""
    mod = check_ci_workflow_drift
    _stub_github_app(mod, monkeypatch, installed_owners=("owner-a", "owner-c"))
    _set_registry(mod, monkeypatch, tmp_path, {
        "widget-crew": _unit(["widget-one"], ci_workflows={"widget-one": ["ci.yml"]}),
        "orphan-crew": _unit(["orphan-one"], ci_workflows={"orphan-one": ["ci.yml"]},
                            owner="owner-b"),
        "ghost-crew": _unit(["ghost-one"], ci_workflows={"ghost-one": ["ci.yml"]},
                            owner="owner-c"),
    })
    gh = _FakeGitHub({"owner-a/widget-one": {"workflows": {"ci.yml": _CI_YML_PULL_REQUEST}}},
                     installed={"owner-a/widget-one"})
    monkeypatch.setattr(mod.GA, "request", gh)
    posted = []
    monkeypatch.setattr(mod.SC, "post_alert", lambda *a, **k: posted.append(k) or "created")
    monkeypatch.setattr(mod.sys, "argv", ["check-ci-workflow-drift"])

    rc = mod.main()

    out = capsys.readouterr().out
    assert "owner-b/orphan-one: unchecked" in out
    assert "no convivy-bosun installation for 'owner-b'" in out
    assert "owner-c/ghost-one: registered but not on GitHub" in out
    assert rc == 0
    assert len(posted) == 2


def test_main_unchecked_notinstalled_finding_tells_a_person_to_install_the_app(
        check_ci_workflow_drift, monkeypatch, tmp_path, capsys):
    """The NotInstalled finding names the owner AND tells a person to
    install the convivy-bosun App on it, so the daily report states what is
    missing and who must act (the steward, 2026-10-27)."""
    mod = check_ci_workflow_drift
    _stub_github_app(mod, monkeypatch, installed_owners=())
    _set_registry(mod, monkeypatch, tmp_path, {
        "orphan-crew": _unit(["orphan-one"], ci_workflows={"orphan-one": ["ci.yml"]},
                            owner="owner-zed"),
    })
    monkeypatch.setattr(mod.GA, "request", _FakeGitHub({}))
    monkeypatch.setattr(mod.SC, "post_alert", lambda *a, **k: "created")
    monkeypatch.setattr(mod.sys, "argv", ["check-ci-workflow-drift"])

    mod.main()

    out = capsys.readouterr().out
    assert ("a person must install the convivy-bosun App on 'owner-zed'" in out)


def test_main_exits_nonzero_when_every_attempted_repo_could_not_be_checked(
        check_ci_workflow_drift, monkeypatch, tmp_path, capsys):
    """No repo's listing succeeded this pass (every one came back 500) — the
    run proved nothing, and the exit code says so."""
    mod = check_ci_workflow_drift
    _stub_github_app(mod, monkeypatch)
    _set_registry(mod, monkeypatch, tmp_path, {
        "widget-crew": _unit(["flaky-one"], ci_workflows={"flaky-one": ["ci.yml"]}),
    })

    def gh(method, url, token, body, opener=None):
        return 500, {"message": "Internal Server Error"}

    monkeypatch.setattr(mod.GA, "request", gh)
    monkeypatch.setattr(mod.SC, "post_alert", lambda *a, **k: "created")
    monkeypatch.setattr(mod.sys, "argv", ["check-ci-workflow-drift"])

    rc = mod.main()

    assert rc == 1


def test_main_exits_nonzero_when_no_repo_is_listed(
        check_ci_workflow_drift, monkeypatch, tmp_path, capsys):
    """CQ·570's own production regression: every registered repo answers
    403 (an existing repo whose directory listing is refused) or 404 (a
    repo absent from GitHub) and NONE answers 200 — the exact shape measured
    by hand on 2026-10-26, where every repo "could not be checked" or was
    "registered but not on GitHub" and the run still exited 0. This must
    fail against `origin/main`, which folds a repo-level 404 into the same
    count as a real 200 listing and exits 0 here instead."""
    mod = check_ci_workflow_drift
    _stub_github_app(mod, monkeypatch)
    _set_registry(mod, monkeypatch, tmp_path, {
        "widget-crew": _unit(["ghost-one", "locked-one"],
                             ci_workflows={"ghost-one": ["ci.yml"],
                                          "locked-one": ["ci.yml"]}),
    })

    def gh(method, url, token, body, opener=None):
        if url.startswith(_INSTALLATION_REPOS_URL):
            return 200, {"total_count": 2, "repositories": [
                {"full_name": "owner-a/ghost-one"}, {"full_name": "owner-a/locked-one"}]}
        if "ghost-one" in url:
            return 404, {}
        return 403, {"message": "Forbidden"}

    monkeypatch.setattr(mod.GA, "request", gh)
    monkeypatch.setattr(mod.SC, "post_alert", lambda *a, **k: "created")
    monkeypatch.setattr(mod.sys, "argv", ["check-ci-workflow-drift"])

    rc = mod.main()

    out = capsys.readouterr().out
    assert "registered but not on GitHub" in out
    assert "could not be checked" in out
    assert rc == 1


def test_main_exits_nonzero_when_every_owners_mint_fails(
        check_ci_workflow_drift, monkeypatch, tmp_path, capsys):
    """CQ·570 round 1's second finding: a failed mint folds into
    "unchecked" for that owner, and when EVERY owner's mint fails, nothing
    is ever attempted and `listed` stays 0 — this must fail exactly like a
    run where every listing was refused, not read as a quiet, empty-looking
    pass."""
    mod = check_ci_workflow_drift
    _stub_github_app(mod, monkeypatch, installed_owners=())  # no owner installed
    _set_registry(mod, monkeypatch, tmp_path, {
        "widget-crew": _unit(["widget-one"], ci_workflows={"widget-one": ["ci.yml"]}),
    })
    gh = _FakeGitHub({})  # must never be asked anything — no token to ask with
    monkeypatch.setattr(mod.GA, "request", gh)
    monkeypatch.setattr(mod.SC, "post_alert", lambda *a, **k: "created")
    monkeypatch.setattr(mod.sys, "argv", ["check-ci-workflow-drift"])

    rc = mod.main()

    out = capsys.readouterr().out
    assert "unchecked" in out
    assert gh.calls == []
    assert rc == 1


def test_main_refuses_cleanly_when_the_key_is_missing(check_ci_workflow_drift, monkeypatch):
    mod = check_ci_workflow_drift
    monkeypatch.setattr(
        mod.GA, "installed_app",
        lambda: mod.GA.InstalledApp(app_id="1234567", key_path=Path("/dev/null"),
                                    slug="test-app"))
    monkeypatch.setattr(mod.GA, "key_problem",
                        lambda *a, **k: "no App key at /no/such/path")
    monkeypatch.setattr(mod.sys, "argv", ["check-ci-workflow-drift"])
    assert mod.main() == 1


def test_main_refuses_cleanly_when_the_install_has_no_app(
        check_ci_workflow_drift, monkeypatch):
    """The install resolver itself is where a fresh install with no App set
    up at all is refused — before `key_problem` is ever reached."""
    mod = check_ci_workflow_drift

    def _raise():
        raise mod.GA.Unreadable("no App id at .../app-id")
    monkeypatch.setattr(mod.GA, "installed_app", _raise)
    monkeypatch.setattr(mod.sys, "argv", ["check-ci-workflow-drift"])
    assert mod.main() == 1


def test_main_refuses_when_the_registry_cannot_be_read(
        check_ci_workflow_drift, monkeypatch, tmp_path):
    mod = check_ci_workflow_drift
    monkeypatch.setattr(
        mod.GA, "installed_app",
        lambda: mod.GA.InstalledApp(app_id="1234567", key_path=Path("/dev/null"),
                                    slug="test-app"))
    monkeypatch.setattr(mod.GA, "key_problem", lambda *a, **k: None)
    monkeypatch.setattr(mod.PR, "PROJECTS_JSON", tmp_path / "no-such-file.json")
    mod.PR._load_raw.cache_clear()
    monkeypatch.setattr(mod.sys, "argv", ["check-ci-workflow-drift"])
    assert mod.main() == 1


def test_main_no_report_mode_prints_but_does_not_post(
        check_ci_workflow_drift, monkeypatch, tmp_path, capsys):
    """`--no-report`: this check's own dry-run/stdout mode, safe to run by
    hand after a merge or plist reload."""
    mod = check_ci_workflow_drift
    _stub_github_app(mod, monkeypatch)
    _set_registry(mod, monkeypatch, tmp_path, {
        "widget-crew": _unit(["widget-one"], ci_workflows={"widget-one": []}),
    })
    gh = _FakeGitHub({"owner-a/widget-one": {"workflows": {"ci.yml": _CI_YML_PULL_REQUEST}}})
    monkeypatch.setattr(mod.GA, "request", gh)
    posted = []
    monkeypatch.setattr(mod.SC, "post_alert", lambda *a, **k: posted.append(k) or "created")
    monkeypatch.setattr(mod.sys, "argv", ["check-ci-workflow-drift", "--no-report"])

    rc = mod.main()

    assert rc == 0
    assert posted == []
    assert "owner-a/widget-one" in capsys.readouterr().out
