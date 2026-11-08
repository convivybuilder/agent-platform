"""`agentctl crew register` — one crew, one row, no hand edit.

issue_01M3YN6857H5J541G73Y9P3KA1: Shipwright's single-account rehearsal
created a crew with a directory, a tmux session and a bead but no row in the
registry, and the work-queue tools refused to file its epics because they
resolve `product` through it.

CQ·564 (the 2026-10-25 amendment to platform/decisions/0098) moved the
registry onto the Library: `register()` sends the row to the Library's
`POST /registry/crews` (CQ·242, library#334) and writes NO FILE anywhere, on
any outcome. Every test here uses a FAKE OPENER standing in for the Library
and, for the ci_workflows tests, for GitHub too — the same pattern
`tests/test_github_mint.py` uses for `GA.mint`/`GA.mint_token`, so this suite
needs no real socket and no `sandbox.network.allowLocalBinding` — and a
synthetic crew, repo and owner name throughout. None reads or writes any
deploy area; the tests that assert "nothing was written" check the real
committed config/projects.seed.json's bytes too, as the strongest version
of that claim.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

import crew_registration as CR
import github_app as GA
import projects

REPO = Path(__file__).resolve().parents[1]


def _spec(**kw) -> CR.CrewSpec:
    base = dict(slug="recipe-units", display="Recipe Units", repos=["recipe-units"])
    base.update(kw)
    return CR.CrewSpec(**base)


# --- a fake opener standing in for the Library's POST /registry/crews ------


class _Resp:
    """A `urlopen`-shaped response a fake opener returns directly, with no
    exception, for ANY status — the same shape `GA.request`'s own fakes in
    `tests/test_github_mint.py` use, and what `_post_crew_row` is written
    to accept alongside a real `HTTPError`."""

    def __init__(self, status: int, payload: dict):
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeLibrary:
    """Stands in for the Library's `POST /registry/crews`: a `status`/`body`
    override when given, else an in-memory `store` that refuses a slug
    already claimed — real registration behaviour, synthesized rather than
    asked of a real service. Used two ways:

    - as an `opener` passed to `CR.register(..., opener=stub)`, exercising
      the real request-building and status-handling code in
      `_post_crew_row` with no real socket;
    - via `stub.post`, a drop-in for `CR._post_crew_row` itself, for the
      CLI-level tests that go through `cmd_crew_register` — `register()`
      calls that with no `opener` of its own to inject into, so those tests
      replace the function rather than its transport.
    """

    def __init__(self, *, status: int | None = None, body: dict | None = None):
        self.requests: list[dict] = []
        self.store: dict[str, Any] = {}
        self.status = status
        self.body = body

    def __call__(self, req, timeout=None) -> _Resp:
        payload = json.loads(req.data) if req.data else {}
        self.requests.append({"method": req.get_method(), "url": req.full_url,
                              "headers": dict(req.header_items()), "body": payload})
        status, body = self._answer(payload)
        return _Resp(status, body)

    def post(self, body: dict, *, opener=None) -> dict:
        """The shape `CR._post_crew_row` has, for monkeypatching it
        directly in a CLI-level test."""
        self.requests.append({"method": "POST", "url": "/registry/crews", "body": body})
        status, resp_body = self._answer(body)
        if status == 201:
            return resp_body
        message = resp_body.get("error") or f"HTTP {status}"
        hint = f" ({resp_body['hint']})" if resp_body.get("hint") else ""
        verb = "refused" if status in (400, 409) else "failed to"
        raise CR.RegistrationRefused(
            f"the Library {verb} register {body.get('slug')!r} (HTTP {status}): "
            f"{message}{hint}")

    def _answer(self, payload: dict) -> tuple[int, dict]:
        if self.status is not None:
            return self.status, (self.body if self.body is not None else {})
        slug = payload.get("slug")
        if not slug or slug in self.store:
            return 409, {"error": f"{slug!r} already claimed"}
        row = {k: v for k, v in payload.items() if k != "slug"}
        row["updated_at"] = "2026-10-26T00:00:00Z"
        row["updated_by"] = "person_01TESTBOSUNWRITER00000000"
        self.store[slug] = row
        return 201, {**row, "slug": slug}


def _unreachable(req, timeout=None):
    """An opener raising the way a closed/unreachable socket does — never a
    status at all."""
    raise ConnectionRefusedError("[Errno 61] Connection refused")


@pytest.fixture
def writer_token(tmp_path, monkeypatch):
    """A readable writer-token file at the path `_registry_writer_token_file`
    resolves, under a `Path.home()` this fixture points at a scratch
    directory — never the real `~/.orient-control`."""
    home = tmp_path / "home"
    (home / ".orient-control").mkdir(parents=True)
    token_path = home / ".orient-control" / "agent-platform-registry-writer-token"
    token_path.write_text("writer-test-token\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: home)
    return token_path


@pytest.fixture
def no_ci_workflows(monkeypatch):
    """Every test that is not itself about the GitHub read stubs
    `_repo_ci_workflows` to a fixed answer, so `register()`'s own Library
    interaction is exercised with no real (or fake) GitHub call at all."""
    monkeypatch.setattr(CR, "_repo_ci_workflows", lambda repo, row, **kw: ["ci.yml"])


def _watched_files(tmp_path: Path) -> tuple[Path, bytes, bytes]:
    """A throwaway file standing in for "PROJECTS_JSON's target", plus the
    `before` bytes of it and of the real committed config/projects.seed.json —
    config/projects.json is gone from the repo (CQ·565), so the seed is this
    check's other real file — the two files every test below re-reads
    afterwards to show `register()` wrote to NEITHER, which is the whole
    point of CQ·564."""
    target = tmp_path / "projects.json"
    target.write_text('{"_meta": {"doc": "untouched"}}\n', encoding="utf-8")
    return target, target.read_bytes(), (REPO / "config" / "projects.seed.json").read_bytes()


def _assert_nothing_written(target: Path, before_target: bytes, before_repo: bytes) -> None:
    assert target.read_bytes() == before_target, "PROJECTS_JSON's target was written to"
    assert (REPO / "config" / "projects.seed.json").read_bytes() == before_repo, (
        "the committed config/projects.seed.json was written to")


# --- a new row is sent to the Library, with the writer token, and nothing is written


def test_register_sends_the_row_and_the_writer_token_and_writes_no_file(
        writer_token, no_ci_workflows, tmp_path):
    target, before_target, before_repo = _watched_files(tmp_path)
    stub = _FakeLibrary()

    row = CR.register(_spec(display="Recipe Units", aliases=["recipes"]), opener=stub)

    assert row["slug"] == "recipe-units"
    assert row["display"] == "Recipe Units"
    assert row["updated_at"] and row["updated_by"]

    assert len(stub.requests) == 1
    req = stub.requests[0]
    assert req["method"] == "POST"
    assert req["url"].endswith("/registry/crews")
    assert req["headers"]["Authorization"] == "Bearer writer-test-token"
    assert req["headers"]["Library-protocol"]
    assert req["body"]["slug"] == "recipe-units"
    assert req["body"]["display"] == "Recipe Units"
    assert req["body"]["repos"] == ["recipe-units"]
    assert req["body"]["aliases"] == ["recipes"]

    _assert_nothing_written(target, before_target, before_repo)


def test_registered_row_resolves_through_projects_py_in_the_same_process(
        writer_token, no_ci_workflows, monkeypatch):
    stub = _FakeLibrary()
    monkeypatch.setenv("AGENT_PLATFORM_REGISTRY_TOKEN", "reader-test-token")
    monkeypatch.setattr(projects, "PROJECTS_JSON", None)
    monkeypatch.setattr(projects.urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp(200, dict(stub.store)))
    projects._load_raw.cache_clear()

    CR.register(_spec(repos=["recipe-units", "recipe-units-backend"], aliases=["recipes"]),
               opener=stub)

    try:
        assert projects.get("recipe-units")["display"] == "Recipe Units"
        assert projects.project_for_repo("recipe-units-backend") == "recipe-units"
        assert projects.project_for_repo("recipes") == "recipe-units"
    finally:
        projects._load_raw.cache_clear()


def test_dry_run_builds_and_fills_the_row_but_sends_nothing(
        writer_token, no_ci_workflows, tmp_path):
    target, before_target, before_repo = _watched_files(tmp_path)
    stub = _FakeLibrary()

    row = CR.register(_spec(), dry_run=True, opener=stub)

    assert row["class"] == "project"
    assert row["ci_workflows"] == {"recipe-units": ["ci.yml"]}
    assert stub.requests == [], "a dry run must send nothing to the Library"
    _assert_nothing_written(target, before_target, before_repo)


# --- the Library's answer decides the outcome -------------------------------


def test_a_400_is_reported_with_the_stub_error_and_writes_nothing(
        writer_token, no_ci_workflows, tmp_path):
    target, before_target, before_repo = _watched_files(tmp_path)
    stub = _FakeLibrary(status=400, body={"error": "display must be a non-empty string"})

    with pytest.raises(CR.RegistrationRefused, match="display must be a non-empty string"):
        CR.register(_spec(), opener=stub)
    _assert_nothing_written(target, before_target, before_repo)


def test_a_409_is_reported_with_the_stub_error_and_writes_nothing(
        writer_token, no_ci_workflows, tmp_path):
    target, before_target, before_repo = _watched_files(tmp_path)
    stub = _FakeLibrary(status=409, body={
        "error": "'recipe-units' is already claimed", "hint": "pick a different slug"})

    with pytest.raises(CR.RegistrationRefused, match="already claimed") as exc_info:
        CR.register(_spec(), opener=stub)
    assert "pick a different slug" in str(exc_info.value)
    _assert_nothing_written(target, before_target, before_repo)


def test_an_unreachable_library_fails_and_is_never_read_as_success(
        writer_token, no_ci_workflows, tmp_path):
    target, before_target, before_repo = _watched_files(tmp_path)

    with pytest.raises(CR.RegistrationRefused):
        CR.register(_spec(), opener=_unreachable)
    _assert_nothing_written(target, before_target, before_repo)


def test_a_503_fails_and_is_never_read_as_success(writer_token, no_ci_workflows, tmp_path):
    target, before_target, before_repo = _watched_files(tmp_path)
    stub = _FakeLibrary(status=503, body={"error": "empty registry"})

    with pytest.raises(CR.RegistrationRefused):
        CR.register(_spec(), opener=stub)
    _assert_nothing_written(target, before_target, before_repo)


def test_a_403_fails_and_is_never_read_as_success(writer_token, no_ci_workflows, tmp_path):
    target, before_target, before_repo = _watched_files(tmp_path)
    stub = _FakeLibrary(status=403, body={"error": "forbidden: missing registry.write"})

    with pytest.raises(CR.RegistrationRefused):
        CR.register(_spec(), opener=stub)
    _assert_nothing_written(target, before_target, before_repo)


def test_a_401_fails_and_is_never_read_as_success(writer_token, no_ci_workflows, tmp_path):
    target, before_target, before_repo = _watched_files(tmp_path)
    stub = _FakeLibrary(status=401, body={"error": "unknown token"})

    with pytest.raises(CR.RegistrationRefused):
        CR.register(_spec(), opener=stub)
    _assert_nothing_written(target, before_target, before_repo)


def test_a_missing_token_file_fails_and_names_the_path(tmp_path, monkeypatch, no_ci_workflows):
    stub = _FakeLibrary()
    home = tmp_path / "no-orient-control-here"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)

    with pytest.raises(CR.RegistrationRefused) as exc_info:
        CR.register(_spec(), opener=stub)
    expected_path = home / ".orient-control" / "agent-platform-registry-writer-token"
    assert str(expected_path) in str(exc_info.value)
    assert stub.requests == [], "no request should be made with no token"


# --- after a 201, projects.py's cache is cleared in the same process -------


def test_after_a_201_a_projects_py_read_in_the_same_process_sees_the_new_crew(
        writer_token, no_ci_workflows, monkeypatch):
    stub = _FakeLibrary()
    monkeypatch.setenv("AGENT_PLATFORM_REGISTRY_TOKEN", "reader-test-token")
    monkeypatch.setattr(projects, "PROJECTS_JSON", None)
    monkeypatch.setattr(projects.urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp(200, dict(stub.store)))
    projects._load_raw.cache_clear()
    assert projects.get("recipe-units") is None  # primed the cache with "not yet registered"

    try:
        CR.register(_spec(), opener=stub)
        assert projects.get("recipe-units") is not None
        assert projects.get("recipe-units")["display"] == "Recipe Units"
    finally:
        projects._load_raw.cache_clear()


# --- field validation (build_row's own pure checks, no registry to read) ---


@pytest.mark.parametrize("bad_slug", ["Recipe-Units", "-recipe", "recipe units", ""])
def test_bad_slug_is_refused_before_any_request(bad_slug, writer_token, no_ci_workflows):
    stub = _FakeLibrary()
    with pytest.raises(CR.RegistrationRefused):
        CR.register(_spec(slug=bad_slug, repos=[bad_slug or "x"]), opener=stub)
    assert stub.requests == []


def test_kb_project_is_refused_when_kb_is_local(writer_token, no_ci_workflows):
    stub = _FakeLibrary()
    with pytest.raises(CR.RegistrationRefused):
        CR.register(_spec(kb="local", kb_project="recipe-units"), opener=stub)
    assert stub.requests == []


def test_a_regex_breaking_kb_project_is_refused(writer_token, no_ci_workflows):
    """A `kb_project` that is not valid regex syntax reaches
    `knowledge.id_pattern_for`'s alternation unless it is refused here first
    — an unbalanced paren broke `re.compile()` at knowledge.py's own import
    (security-reviewer, PR #978 round 2)."""
    stub = _FakeLibrary()
    with pytest.raises(CR.RegistrationRefused, match="kb_project"):
        CR.register(_spec(kb_project="widget(crew"), opener=stub)
    assert stub.requests == []


def test_unknown_model_tier_is_refused(writer_token, no_ci_workflows):
    stub = _FakeLibrary()
    with pytest.raises(CR.RegistrationRefused):
        CR.register(_spec(model_tier="sonnett"), opener=stub)
    assert stub.requests == []


@pytest.mark.parametrize("bad_prefix", ["acme", "", "acme_", "-acme"])
def test_malformed_agent_prefix_is_refused_before_any_request(
        bad_prefix, writer_token, no_ci_workflows):
    """`projects.agent_prefixes()` raises `ValueError` on any row whose
    `agent_prefix` is not a non-empty string ending in `-`. Checked here, on
    the write side, so a bad value refuses before it ever reaches the live
    registry — the shape `app_prefixes.internal_app_prefixes()` degrades to
    `()` for AFTER the fact, which is one crew too late for every other
    crew's prefix lookups in the same process."""
    stub = _FakeLibrary()
    with pytest.raises(CR.RegistrationRefused, match="agent_prefix"):
        CR.register(_spec(agent_prefix=bad_prefix), opener=stub)
    assert stub.requests == []


def test_well_formed_agent_prefix_is_sent_and_resolves(
        writer_token, no_ci_workflows, monkeypatch):
    stub = _FakeLibrary()
    monkeypatch.setenv("AGENT_PLATFORM_REGISTRY_TOKEN", "reader-test-token")
    monkeypatch.setattr(projects, "PROJECTS_JSON", None)
    monkeypatch.setattr(projects.urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp(200, dict(stub.store)))
    projects._load_raw.cache_clear()

    try:
        row = CR.register(_spec(agent_prefix="acme-"), opener=stub)
        assert row["agent_prefix"] == "acme-"
        assert stub.requests[0]["body"]["agent_prefix"] == "acme-"
        assert "acme-" in projects.all_agent_prefixes()
    finally:
        projects._load_raw.cache_clear()


def test_task_text_public_is_always_sent_explicitly(writer_token, no_ci_workflows):
    stub = _FakeLibrary()
    row = CR.register(_spec(), opener=stub)
    assert row["task_text_public"] is False


def test_optional_fields_are_omitted_when_not_requested(writer_token, no_ci_workflows):
    stub = _FakeLibrary()
    CR.register(_spec(), opener=stub)
    body = stub.requests[0]["body"]
    for absent in ("private", "model_tier", "agent_prefix", "data_local", "note", "created_by"):
        assert absent not in body


# --- ADR-0043: a private row must come out internally consistent ----------


def test_a_private_crew_with_default_kb_is_refused(writer_token, no_ci_workflows):
    stub = _FakeLibrary()
    with pytest.raises(CR.RegistrationRefused, match="kb='central'"):
        CR.register(_spec(private=True), opener=stub)
    assert stub.requests == []


def test_a_private_crew_with_consistent_fields_is_accepted(writer_token, no_ci_workflows):
    stub = _FakeLibrary()
    row = CR.register(_spec(private=True, kb="local"), opener=stub)
    assert row["private"] is True
    assert row["kb"] == "local"


# --- ci_workflows: filled from a live GitHub read, never from caller input --


@pytest.fixture()
def rsa_key(tmp_path):
    key = tmp_path / "app.pem"
    subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"], check=True,
                   capture_output=True)
    key.chmod(0o600)
    return key


def _fake_transport(*, contents_status=200, workflow_items=None):
    """A fake opener answering the org-installation lookup, the access-token
    mint, the revoke, the `.github/workflows` contents listing, AND —
    `register()` shares one `opener` across both calls — a 201 for
    `POST /registry/crews`, so these tests can exercise the WHOLE pipeline
    (the GitHub fill feeding the row that reaches the Library) with one
    fake. Extends `tests/test_github_mint.py`'s own fake-opener pattern with
    the routes this module's own calls add."""
    calls: list[str] = []

    def opener(req, timeout=None):
        calls.append(req.full_url)
        method = req.get_method()
        url = req.full_url
        if method == "GET" and url.endswith("/installation"):
            return _Resp(200, {"id": 1, "account": {"login": "convivy"}})
        if method == "POST" and url.endswith("/access_tokens"):
            body = json.loads(req.data)
            return _Resp(201, {"token": "ghs_test", "permissions": body.get("permissions"),
                               "repository_selection": "selected",
                               "expires_at": "2026-10-17T12:00:00Z"})
        if method == "DELETE" and url.endswith("/installation/token"):
            return _Resp(204, {})
        if method == "GET" and url.endswith("/contents/.github/workflows"):
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
    return opener, calls


def test_ci_workflows_fills_the_row_from_a_stubbed_github_listing(
        rsa_key, tmp_path, monkeypatch, writer_token):
    monkeypatch.setattr(GA, "installed_app",
                        lambda: GA.InstalledApp(app_id="1234567", key_path=rsa_key,
                                                slug="test-app"))
    monkeypatch.setattr(GA, "mint_log_path", lambda: tmp_path / "mint-log.jsonl")
    opener, calls = _fake_transport(workflow_items=[
        {"name": "ci.yml", "type": "file"},
        {"name": "e2e.yaml", "type": "file"},
        {"name": "README.md", "type": "file"},
        {"name": "actions", "type": "dir"},
    ])

    row = CR.register(_spec(), opener=opener)

    assert row["ci_workflows"] == {"recipe-units": ["ci.yml", "e2e.yaml"]}
    assert any(u.endswith("/registry/crews") for u in calls)


def test_ci_workflows_an_empty_listing_gives_an_explicit_empty_list(
        rsa_key, tmp_path, monkeypatch, writer_token):
    monkeypatch.setattr(GA, "installed_app",
                        lambda: GA.InstalledApp(app_id="1234567", key_path=rsa_key,
                                                slug="test-app"))
    monkeypatch.setattr(GA, "mint_log_path", lambda: tmp_path / "mint-log.jsonl")
    opener, _ = _fake_transport(contents_status=404)

    row = CR.register(_spec(), opener=opener)

    assert row["ci_workflows"] == {"recipe-units": []}


def test_ci_workflows_an_unreadable_listing_refuses_before_any_library_request(
        rsa_key, tmp_path, monkeypatch, writer_token):
    monkeypatch.setattr(GA, "installed_app",
                        lambda: GA.InstalledApp(app_id="1234567", key_path=rsa_key,
                                                slug="test-app"))
    monkeypatch.setattr(GA, "mint_log_path", lambda: tmp_path / "mint-log.jsonl")
    opener, calls = _fake_transport(contents_status=500)

    with pytest.raises(CR.RegistrationRefused, match="recipe-units"):
        CR.register(_spec(), opener=opener)
    assert not any(u.endswith("/registry/crews") for u in calls), (
        "the Library must not be asked when a repo's CI can't be read")


# --- the CLI wiring: `agentctl crew register` --------------------------------


def _cli_args(**overrides) -> argparse.Namespace:
    """The namespace `agentctl`'s own argparser would build for `crew
    register`, every default spelled out so this test cannot pass by
    accident of an attribute neither side set."""
    base = dict(
        slug="recipe-units", display="Recipe Units", repos=None, aliases=[],
        products=[], crew=None, owner="convivy", github_owner=None,
        cls="project", kb="central",
        kb_project=None, home_dir=None, dirs=[], lifecycle="active",
        model_tier=None, task_text_public=False,
        private=False, data_local=False, agent_prefix=None,
        crew_cockpit=None, note=None, dry_run=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.fixture
def fake_post(monkeypatch, no_ci_workflows):
    """`cmd_crew_register` goes through `CR.register` with no `opener` seam
    of its own, so these CLI-level tests replace `CR._post_crew_row`
    directly — the shape `_FakeLibrary.post` has."""
    stub = _FakeLibrary()
    monkeypatch.setattr(CR, "_post_crew_row", stub.post)
    return stub


def test_cmd_crew_register_sends_the_row_to_the_library(agentctl, fake_post, capsys):
    rc = agentctl.cmd_crew_register(_cli_args())
    assert rc == 0
    out = capsys.readouterr().out
    assert "registered 'recipe-units'" in out
    assert "Library" in out
    assert fake_post.requests[0]["body"]["display"] == "Recipe Units"


def test_cmd_crew_register_refuses_a_409_and_exits_nonzero(agentctl, monkeypatch,
                                                            no_ci_workflows, capsys):
    stub = _FakeLibrary(status=409, body={"error": "'recipe-units' already claimed"})
    monkeypatch.setattr(CR, "_post_crew_row", stub.post)
    rc = agentctl.cmd_crew_register(_cli_args())
    assert rc != 0
    err = capsys.readouterr().err
    assert "refused" in err and "already claimed" in err


def test_cmd_crew_register_dry_run_sends_nothing(agentctl, fake_post, capsys):
    rc = agentctl.cmd_crew_register(_cli_args(dry_run=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "would register" in out
    assert fake_post.requests == []


# --- `created_by`, read from the operator config's `person_id` --------------


_PERSON_ID = "person_01ARZ3NDEKTSV4RRFFQ69G5FAV"


def test_cmd_crew_register_writes_created_by_from_operator_config(
        agentctl, tmp_path, monkeypatch, fake_post, capsys):
    config_path = tmp_path / "operator.json"
    config_path.write_text(json.dumps({"person_id": _PERSON_ID}))
    monkeypatch.setenv("CONVIVY_OPERATOR_CONFIG", str(config_path))
    rc = agentctl.cmd_crew_register(_cli_args())
    assert rc == 0
    assert fake_post.requests[0]["body"]["created_by"] == _PERSON_ID


def test_cmd_crew_register_without_person_id_omits_created_by_and_says_why(
        agentctl, tmp_path, monkeypatch, fake_post, capsys):
    config_path = tmp_path / "operator.json"
    config_path.write_text(json.dumps({"roster_crew": "operator-crew"}))
    monkeypatch.setenv("CONVIVY_OPERATOR_CONFIG", str(config_path))
    rc = agentctl.cmd_crew_register(_cli_args())
    assert rc == 0
    err = capsys.readouterr().err
    assert "did not record a creator" in err
    assert "person_id" in err
    assert "created_by" not in fake_post.requests[0]["body"]
