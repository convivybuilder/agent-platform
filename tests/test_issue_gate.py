"""The overnight dequeue gate (ARCH-361 Part B).

The failure being pinned: on 2026-07-17 the overnight drain opened convivy/vivy
#971 for work a daytime co was already doing on #944 — a duplicate BRANCH. A
claim check alone would not have caught it, because the branch existed without a
live claim. So these tests care most about the two-signal rule and about the
gate failing closed when it cannot see.
"""
from __future__ import annotations

import base64
import contextlib
import http.client
import io
import json
import shlex
import socket
import subprocess
import tempfile
import threading
from pathlib import Path

import pytest

import branch_naming as BN
import issue_gate


@pytest.fixture(autouse=True)
def _no_test_holds_a_real_minter_configuration(monkeypatch, tmp_path_factory):
    """FAIL CLOSED, the same contract `tests/conftest.py`'s
    `_no_test_holds_the_library_app_key` holds for the Library's App key:
    most of this file's tests predate CQ·407 and call `_resolve_token` (via
    `_github_repo_for`/`branches_referencing_issues`) without configuring a
    minter at all. `_github_app_dir()` falls back to the real
    `~/.config/convivy/github-app` when `_GITHUB_APP_DIR_ENV` is unset, and
    `_minter_bin_dir()` always returns the real `~/.local/bin` — it takes no
    env override, so tests redirect it by monkeypatching the function itself
    — so once a machine running these tests has configured a real minter
    (shipwright CQ·59), every one of those tests would start resolving
    tokens through the real minter — including running it and minting a
    live token — instead of through the stubbed `gh` they were written
    against. Pointing `_GITHUB_APP_DIR_ENV` and `_minter_bin_dir` at a fresh
    tmp dir each by default makes `_minter_configured` false and the minter
    absent from `_minter_bin_dir()` for every test that doesn't say
    otherwise; `_configure_minter` and `_point_minter_bin_dir_at`/
    `_stub_minter` override these for the tests that mean to exercise the
    minter path."""
    monkeypatch.setenv(issue_gate._GITHUB_APP_DIR_ENV,
                       str(tmp_path_factory.mktemp("no-real-github-app-dir")))
    monkeypatch.setattr(issue_gate, "_minter_bin_dir",
                        lambda: tmp_path_factory.mktemp("no-real-minter-bin-dir"))


def _issue(iid="issue_01A", **kw):
    d = {"id": iid, "title": "t", "type": "bug", "refs": {}}
    d.update(kw)
    return d


def _fake_id(tag, rest="Z" * 14):
    """A syntactically realistic issue id — `issue_` + a 26-character ULID
    shape — for tests that exercise the real fragment match
    (`_issue_id_fragment` needs at least `_ID_FRAGMENT_LEN` normalized
    characters to produce a fragment at all, which `_issue()`'s own
    deliberately-short default id ("issue_01A") never does). `tag` fills the
    first 12 characters (the part `_ID_FRAGMENT_LEN=18` actually reads, past
    the 6-character literal "issue-"); two ids built with the same `tag`
    share a fragment on purpose, for the collision tests."""
    tag12 = (tag * 12)[:12]
    return f"issue_{tag12}{rest}"[:32]


# --- shared stand-ins -----------------------------------------------------------
#
# ROUND 9: `branches_referencing_issues` makes exactly one subprocess call
# (`gh auth token`, still the real `gh` binary — `_resolve_gh_token`'s
# docstring says why that one stays as it is) and two in-process HTTPS calls
# (`_github_api_get`, for the registry read and the branch scan). `_fake_gh`
# stands in for the first; `_FakeOpener`/`_FakeResponse` stand in for the
# second, via `issue_gate._opener`, which every call to `_github_api_get`
# goes through.

_DEFAULT_REGISTRY = {
    "vivy": {"repos": ["vivy"]},
    "constance": {"repos": ["constance"]},
    "sous-chef": {"repos": ["sous-chef"]},
}

_REGISTRY_URL = issue_gate._API_BASE + issue_gate._CONTENTS_PATH.format(
    slug=issue_gate._REGISTRY_REPO_SLUG, path=issue_gate._REGISTRY_PATH)


def _branches_url(slug):
    return issue_gate._API_BASE + issue_gate._MATCHING_REFS_PATH.format(slug=slug)


def _registry_contents_bytes(registry=None):
    """The GitHub contents API's own JSON shape: `content` is the file's
    bytes, base64-encoded."""
    body = json.dumps(_DEFAULT_REGISTRY if registry is None else registry).encode()
    return json.dumps({"content": base64.b64encode(body).decode(), "encoding": "base64"}).encode()


def _fake_gh(tmp_path, name, *, token="faketoken", returncode=0, stderr=""):
    """A `gh` stand-in for `gh auth token` — the only subprocess call left
    in this module after round 9. Every invocation's argv is appended to
    `<name>-argv`, so a test can confirm it was called, and called exactly
    once."""
    bin_dir = tmp_path / f"{name}-bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    invoked = tmp_path / f"{name}-argv"
    script = (
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" >> {shlex.quote(str(invoked))}\n'
        f'printf -- "---\\n" >> {shlex.quote(str(invoked))}\n'
        f'printf "%s\\n" {shlex.quote(token)}\n'
    )
    if stderr:
        script += f'printf \'%s\\n\' {shlex.quote(stderr)} >&2\n'
    script += f'exit {returncode}\n'
    gh.write_text(script)
    gh.chmod(0o755)
    return gh, invoked


def _calls_from(invoked):
    """The argv lists from a `_fake_gh` invocation-log file, in call order."""
    text = invoked.read_text()
    return [c.splitlines() for c in text.split("---\n") if c.strip()]


class _FakeResponse:
    """Stand-in for what `OpenerDirector.open()` returns: a context manager
    with `.read()` and a `.headers.get(...)`-capable `Link` header."""

    def __init__(self, body: bytes, link: str | None = None):
        self._body = body
        self.headers = {"Link": link} if link else {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeResponseThatFailsToRead:
    """Stand-in for a response whose `.read()` raises partway through — the
    `h.getresponse()` call inside `opener.open()` succeeded (unlike an
    `HTTPError`/`URLError`/timeout, which fail before a response object
    exists at all), but the body never arrived whole, as
    `http.client.IncompleteRead` models for a connection closed mid-body."""

    headers: dict = {}

    def __init__(self, read_error: BaseException):
        self._read_error = read_error

    def read(self):
        raise self._read_error

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeOpener:
    """Stand-in for `issue_gate._opener()`'s return value, patched in via
    `monkeypatch.setattr(issue_gate, "_opener", lambda: opener)`. `by_url`
    maps an exact request URL to one of: `(body_bytes, link_header_or_None)`,
    an exception instance to raise when `.open()` itself is called (an
    `HTTPError`/`URLError`/timeout/`http.client.HTTPException` from
    `h.getresponse()`), or a response-like object (e.g.
    `_FakeResponseThatFailsToRead`) returned as-is, for a failure that only
    happens once the caller calls `.read()` on an otherwise-successful
    `.open()`. Every opened `Request` is appended to `.requests`, so a test
    can inspect what headers were actually sent."""

    def __init__(self, by_url):
        self.by_url = dict(by_url)
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        entry = self.by_url[req.full_url]
        if isinstance(entry, BaseException):
            raise entry
        if isinstance(entry, tuple):
            body, link = entry
            return _FakeResponse(body, link)
        return entry


def _stub_registry(monkeypatch, *, registry=None, raw_body=None, error=None):
    """Stub just the contents-API call the registry read makes."""
    if error is not None:
        entry = error
    elif raw_body is not None:
        entry = (raw_body if isinstance(raw_body, bytes) else raw_body.encode(), None)
    else:
        entry = (_registry_contents_bytes(registry), None)
    opener = _FakeOpener({_REGISTRY_URL: entry})
    monkeypatch.setattr(issue_gate, "_opener", lambda: opener)
    return opener


def _branch_for_issue(issue_id, *, task_lead="Fix work item ", agent_name="platform-coder",
                      suffix="-abcd1234"):
    """A plausible `agent/<purpose>` branch name for `issue_id`, built the
    same way `bin/agentctl`'s `derive_purpose` builds one: `task_lead +
    issue_id` stands in for a dispatch brief's `--task` text,
    `branch_naming.slugify` (the SAME transform `_issue_id_fragment` reads
    off of) truncates it to 32 characters and prepends `agent_name` OUTSIDE
    that budget, and `suffix` stands in for `_task_hash`'s 8 hex characters.
    The default `task_lead` is exactly the one measured against a real
    branch in `_issue_id_fragment`'s docstring
    (`agent/platform-coder-fix-work-item-issue-01m3wjmxzh5h-1ff2b000`)."""
    return f"agent/{BN.slugify(task_lead + issue_id, prefix=agent_name)}{suffix}"


def _stub_scan(monkeypatch, tmp_path, *, registry=None, agent_branches=(), slug="convivy/vivy",
               token="faketoken", registry_error=None, branches_error=None, name="scan"):
    """Stub the whole `branches_referencing_issues` path: `gh auth token`
    (via `_fake_gh` + `safe_which`) and both HTTPS calls (via a single
    `_FakeOpener` covering the registry URL and `slug`'s branches URL).
    `agent_branches` is a list of `agent/<purpose>` branch names (or
    already-built `{"ref": ...}` dicts, for a malformed-entry test)."""
    gh, invoked = _fake_gh(tmp_path, name, token=token)
    monkeypatch.setattr(issue_gate.SP, "safe_which",
                        lambda n: str(gh) if n == "gh" else None)
    registry_entry = (registry_error if registry_error is not None
                      else (_registry_contents_bytes(registry), None))
    if branches_error is not None:
        branches_entry = branches_error
    else:
        branches_entry = (json.dumps(
            [{"ref": f"refs/heads/{b}"} if isinstance(b, str) else b
             for b in agent_branches]).encode(), None)
    opener = _FakeOpener({_REGISTRY_URL: registry_entry, _branches_url(slug): branches_entry})
    monkeypatch.setattr(issue_gate, "_opener", lambda: opener)
    return gh, invoked, opener


# --- the eligibility rule -----------------------------------------------------


def test_a_free_issue_is_eligible(monkeypatch):
    monkeypatch.setattr(issue_gate, "branches_referencing_issues", lambda product, ids, **k: {})
    eligible, skipped = issue_gate.eligible_issues("vivy", items=[_issue()])
    assert [i["id"] for i in eligible] == ["issue_01A"]
    assert skipped == []


def test_an_issue_with_a_linked_pr_is_skipped(monkeypatch):
    monkeypatch.setattr(issue_gate, "branches_referencing_issues", lambda product, ids, **k: {})
    eligible, skipped = issue_gate.eligible_issues(
        "vivy", items=[_issue(refs={"pr": "convivy/vivy#42"})])
    assert eligible == []
    assert "PR is already linked" in skipped[0][1]


def test_an_issue_with_a_linked_branch_is_skipped(monkeypatch):
    monkeypatch.setattr(issue_gate, "branches_referencing_issues", lambda product, ids, **k: {})
    eligible, skipped = issue_gate.eligible_issues(
        "vivy", items=[_issue(refs={"branch": "agent/some-other-branch"})])
    assert eligible == []
    assert "branch is already linked" in skipped[0][1]


def test_an_unlinked_remote_branch_still_blocks_the_issue(monkeypatch):
    """THE 2026-07-17 CASE. The issue's refs are empty — nobody linked anything —
    but a branch exists on the remote. refs alone would call this free and hand a
    second worker the same work."""
    monkeypatch.setattr(issue_gate, "branches_referencing_issues",
                        lambda product, ids, **k: {"issue_01A": "agent/platform-coder-x-abcd1234"})
    eligible, skipped = issue_gate.eligible_issues("vivy", items=[_issue(refs={})])
    assert eligible == []
    assert "agent/platform-coder-x-abcd1234" in skipped[0][1]
    assert "matches this issue's id fragment" in skipped[0][1]


def test_a_branch_for_a_different_issue_does_not_block(monkeypatch):
    """The gate must not be so eager it stalls the whole queue."""
    monkeypatch.setattr(issue_gate, "branches_referencing_issues",
                        lambda product, ids, **k: {"issue_09Z": "agent/platform-coder-x-abcd1234"})
    eligible, _ = issue_gate.eligible_issues("vivy", items=[_issue()])
    assert [i["id"] for i in eligible] == ["issue_01A"]


# --- CQ·354: matching the branches agentctl actually creates --------------------


def test_a_branch_from_agentctls_own_naming_function_blocks_its_issue(
    agentctl, monkeypatch, tmp_path,
):
    """End to end, through the REAL scan (only the HTTP/gh layer is
    stubbed): a branch name built via `agentctl`'s own `derive_purpose` +
    `worktrees.branch_for_purpose` — not a copy of their logic — for a task
    naming this issue must make the gate skip it."""
    iid = _fake_id("c", rest="REALISTIC00000")
    task = f"Fix work item {iid} (CQ·999)"
    purpose = agentctl.derive_purpose("platform-coder", task)
    branch = agentctl.W.branch_for_purpose(purpose)
    assert branch.startswith("agent/"), branch

    _stub_scan(monkeypatch, tmp_path, agent_branches=[branch], name="realbranch")
    eligible, skipped = issue_gate.eligible_issues("vivy", items=[_issue(iid)])
    assert eligible == []
    assert branch in skipped[0][1]


def test_a_shared_fragment_collision_blocks_both_issues(monkeypatch, tmp_path):
    """Two different issues whose ids share `_ID_FRAGMENT_LEN` leading
    characters collide on one fragment. The gate's answer, per
    `_issue_id_fragment`'s docstring, is to fail closed for BOTH rather than
    guess which one a matching branch actually belongs to."""
    id_a = _fake_id("q", rest="FIRSTISSUE0000")
    id_b = _fake_id("q", rest="SECONDISSUE000")
    assert id_a != id_b
    assert issue_gate._issue_id_fragment(id_a) == issue_gate._issue_id_fragment(id_b), \
        "the fixture must reproduce a real fragment collision"

    branch = _branch_for_issue(id_a)  # one real branch; both ids' fragments match it
    _stub_scan(monkeypatch, tmp_path, agent_branches=[branch], name="collision")
    eligible, skipped = issue_gate.eligible_issues(
        "vivy", items=[_issue(id_a), _issue(id_b)])
    assert eligible == []
    assert {i["id"] for i, _ in skipped} == {id_a, id_b}


def test_the_gate_splits_a_mixed_queue(monkeypatch):
    monkeypatch.setattr(issue_gate, "branches_referencing_issues",
                        lambda product, ids, **k: {"issue_03C": "agent/platform-coder-x-abcd1234"})
    items = [_issue("issue_01A"),
             _issue("issue_02B", refs={"pr": "convivy/vivy#7"}),
             _issue("issue_03C"),
             _issue("issue_04D")]
    eligible, skipped = issue_gate.eligible_issues("vivy", items=items)
    assert [i["id"] for i in eligible] == ["issue_01A", "issue_04D"]
    assert {i["id"] for i, _ in skipped} == {"issue_02B", "issue_03C"}


def test_an_empty_queue_is_not_an_error(monkeypatch):
    """A quiet night must not look like a broken gate."""
    assert issue_gate.eligible_issues("vivy", items=[]) == ([], [])


def test_malformed_refs_do_not_crash_the_gate(monkeypatch):
    """refs is agent-written JSON; a non-dict there must not take the night down."""
    monkeypatch.setattr(issue_gate, "branches_referencing_issues", lambda product, ids, **k: {})
    eligible, _ = issue_gate.eligible_issues(
        "vivy", items=[_issue(refs="not-a-dict"), _issue("issue_02B", refs=None)])
    assert len(eligible) == 2


# --- failing closed -----------------------------------------------------------


def test_a_failed_branch_scan_holds_back_every_issue(monkeypatch):
    """Fail CLOSED. Without the scan the gate cannot tell a free issue from one
    that already has a branch, and duplicating work costs more than deferring it
    by a night."""
    def boom(product, ids, **k):
        raise issue_gate.BranchScanError("no network")
    monkeypatch.setattr(issue_gate, "branches_referencing_issues", boom)
    eligible, skipped = issue_gate.eligible_issues(
        "vivy", items=[_issue(), _issue("issue_02B")])
    assert eligible == []
    assert len(skipped) == 2
    assert all("branch scan unavailable" in r for _, r in skipped)


def test_an_unregistered_product_raises_rather_than_reporting_no_branches(monkeypatch, tmp_path):
    """The dangerous failure is returning an empty set, which reads as 'nothing is
    in flight' and green-lights every issue."""
    # the default registry has no such key
    _stub_scan(monkeypatch, tmp_path, name="unregistered", slug="convivy/not-a-real-product")
    with pytest.raises(issue_gate.BranchScanError, match="not a registered project"):
        issue_gate.branches_referencing_issues("not-a-real-product", ["issue_01A"])


# --- the owner/repo source (rounds 7-9) -----------------------------------------
#
# Round 7 moved off a checkout's `.git/config` remote to `config/projects.json`,
# read via `projects.get` — but that resolves into this process's OWN working
# tree, which a dispatched coder's worktree of agent-platform shares live,
# writable refs and config with. Round 8 moved the registry read onto the
# contents API, still run through `gh`; round 9 moved it onto an in-process
# HTTPS call (`_github_api_get`), stubbed below via `issue_gate._opener` —
# never the real network, and never the real `gh`.


def test_github_repo_for_reads_the_registry_at_origin_main_not_a_checkout(monkeypatch, tmp_path):
    gh, _ = _fake_gh(tmp_path, "registry")
    monkeypatch.setattr(issue_gate.SP, "safe_which",
                        lambda name: str(gh) if name == "gh" else None)
    _stub_registry(monkeypatch)
    assert issue_gate._github_repo_for("vivy") == "convivy/vivy"
    assert issue_gate._github_repo_for("constance") == "convivy/constance"
    assert issue_gate._github_repo_for("sous-chef") == "convivy/sous-chef"


def test_the_registry_read_never_touches_a_local_checkout(monkeypatch, tmp_path):
    """The read goes through `_github_api_get` against the contents API,
    never `git -C <checkout>` — the same class of lever round 7 removed from
    the branch scan itself (round-7 review's second finding, PR comment
    5939963599)."""
    gh, _ = _fake_gh(tmp_path, "registry-checkout")
    monkeypatch.setattr(issue_gate.SP, "safe_which",
                        lambda name: str(gh) if name == "gh" else None)
    opener = _stub_registry(monkeypatch)
    assert issue_gate._github_repo_for("vivy") == "convivy/vivy"
    assert len(opener.requests) == 1
    assert opener.requests[0].full_url == _REGISTRY_URL
    assert ("repos/convivy/agent-platform/contents/config/projects.json?ref=main"
           in opener.requests[0].full_url)


def test_github_owner_defaults_to_convivy_when_absent(monkeypatch, tmp_path):
    gh, _ = _fake_gh(tmp_path, "defowner")
    monkeypatch.setattr(issue_gate.SP, "safe_which",
                        lambda name: str(gh) if name == "gh" else None)
    _stub_registry(monkeypatch, registry={"vivy": {"repos": ["vivy"]}})
    assert issue_gate._github_repo_for("vivy") == "convivy/vivy"


def test_github_owner_field_overrides_the_default(monkeypatch, tmp_path):
    """The round-7 review's finding: `owner` is documented metadata, not a
    GitHub account, and reads `nyh` for projects whose repos actually live
    under `operator-account`. `github_owner` is the field that answers this."""
    gh, _ = _fake_gh(tmp_path, "nyh")
    monkeypatch.setattr(issue_gate.SP, "safe_which",
                        lambda name: str(gh) if name == "gh" else None)
    _stub_registry(monkeypatch, registry={
        "nyh-arch-tools": {"repos": ["nyh-arch-tools"], "owner": "nyh",
                           "github_owner": "operator-account"},
    })
    assert issue_gate._github_repo_for("nyh-arch-tools") == "operator-account/nyh-arch-tools"


def test_repo_owners_entry_overrides_github_owner_for_that_one_repo(monkeypatch, tmp_path):
    """shipwright's own repo stays under convivy; its `homebrew-tap` entry in
    `repo_owners` names convivybuilder instead, and this reads it for repos[0]
    without touching `github_owner`. Before `_github_repo_for` read
    `repo_owners` at all (it only read `github_owner`, which is unit-wide),
    this returned convivy/homebrew-tap."""
    gh, _ = _fake_gh(tmp_path, "repoowner")
    monkeypatch.setattr(issue_gate.SP, "safe_which",
                        lambda name: str(gh) if name == "gh" else None)
    _stub_registry(monkeypatch, registry={
        "shipwright": {"repos": ["homebrew-tap", "shipwright"],
                       "repo_owners": {"homebrew-tap": "convivybuilder"}},
    })
    assert issue_gate._github_repo_for("shipwright") == "convivybuilder/homebrew-tap"


def test_a_product_with_no_repos_fails_closed(monkeypatch, tmp_path):
    gh, _ = _fake_gh(tmp_path, "norepos")
    monkeypatch.setattr(issue_gate.SP, "safe_which",
                        lambda name: str(gh) if name == "gh" else None)
    _stub_registry(monkeypatch, registry={"thing": {"repos": []}})
    with pytest.raises(issue_gate.BranchScanError, match="no repos"):
        issue_gate._github_repo_for("thing")


def test_a_malformed_registry_payload_fails_closed(monkeypatch, tmp_path):
    """The contents API answers with a well-formed envelope, but the
    base64-decoded file inside it is not valid JSON — the registry file
    itself is malformed, as opposed to the outer HTTP response being
    garbage (covered by `test_the_registry_read_raises_on_unparseable_json`
    below)."""
    gh, _ = _fake_gh(tmp_path, "badregistry")
    monkeypatch.setattr(issue_gate.SP, "safe_which",
                        lambda name: str(gh) if name == "gh" else None)
    bad_content = base64.b64encode(b"not json at all").decode()
    _stub_registry(monkeypatch, raw_body=json.dumps(
        {"content": bad_content, "encoding": "base64"}))
    with pytest.raises(issue_gate.BranchScanError, match="could not be parsed"):
        issue_gate._github_repo_for("vivy")


def test_a_registry_response_missing_content_fails_closed(monkeypatch, tmp_path):
    """A missing/wrong-type field in an otherwise well-formed contents
    response must fail closed too, not be read as an empty registry."""
    gh, _ = _fake_gh(tmp_path, "nocontentfield")
    monkeypatch.setattr(issue_gate.SP, "safe_which",
                        lambda name: str(gh) if name == "gh" else None)
    _stub_registry(monkeypatch, raw_body=json.dumps({"encoding": "base64"}))
    with pytest.raises(issue_gate.BranchScanError, match="could not be parsed"):
        issue_gate._github_repo_for("vivy")


def test_a_non_utf8_registry_file_fails_closed(monkeypatch, tmp_path):
    """The outer contents-API envelope is well-formed JSON, but the
    base64-decoded registry file inside it is bytes that aren't valid in the
    encoding `json.loads` detects — `UnicodeDecodeError`, a `ValueError`
    subclass the registry read's except clause already names, confirming
    the same gap `_github_api_get` has one layer down also reaches this
    second parse site (round-10 security review, PR comment on #736)."""
    gh, _ = _fake_gh(tmp_path, "nonutf8registry")
    monkeypatch.setattr(issue_gate.SP, "safe_which",
                        lambda name: str(gh) if name == "gh" else None)
    bad_content = base64.b64encode(b"\xff\xfe{").decode()
    _stub_registry(monkeypatch, raw_body=json.dumps(
        {"content": bad_content, "encoding": "base64"}))
    with pytest.raises(issue_gate.BranchScanError, match="could not be parsed"):
        issue_gate._github_repo_for("vivy")


def test_a_deeply_nested_registry_file_fails_closed(monkeypatch, tmp_path):
    """Same as the non-UTF-8 case, but the decoded file is nested deeply
    enough to blow `json.loads`'s recursive parser's `RecursionError` — not
    a `ValueError` at all, so it is not covered by naming `ValueError` alone
    (round-10 security review, PR comment on #736)."""
    gh, _ = _fake_gh(tmp_path, "nestedregistry")
    monkeypatch.setattr(issue_gate.SP, "safe_which",
                        lambda name: str(gh) if name == "gh" else None)
    bad_content = base64.b64encode(b"[" * 100_000).decode()
    _stub_registry(monkeypatch, raw_body=json.dumps(
        {"content": bad_content, "encoding": "base64"}))
    with pytest.raises(issue_gate.BranchScanError, match="could not be parsed"):
        issue_gate._github_repo_for("vivy")


def test_the_registry_read_raises_on_unparseable_outer_json(monkeypatch, tmp_path):
    """The contents API response itself is not valid JSON at all — a
    network-level or proxy-level corruption, distinct from the file inside
    it being malformed."""
    gh, _ = _fake_gh(tmp_path, "badouter")
    monkeypatch.setattr(issue_gate.SP, "safe_which",
                        lambda name: str(gh) if name == "gh" else None)
    _stub_registry(monkeypatch, raw_body=b"not json at all")
    with pytest.raises(issue_gate.BranchScanError, match="unparseable JSON"):
        issue_gate._github_repo_for("vivy")


def test_the_registry_read_raises_on_an_http_error(monkeypatch, tmp_path):
    """Covers a 404 from a misresolved owner (round-7 review's finding 4)
    alongside any other HTTP error status on the registry read."""
    gh, _ = _fake_gh(tmp_path, "registry404")
    monkeypatch.setattr(issue_gate.SP, "safe_which",
                        lambda name: str(gh) if name == "gh" else None)
    err = issue_gate.urllib.error.HTTPError(_REGISTRY_URL, 404, "Not Found", {}, io.BytesIO(b""))
    _stub_registry(monkeypatch, error=err)
    with pytest.raises(issue_gate.BranchScanError, match="404"):
        issue_gate._github_repo_for("vivy")


# --- resolving the token (round 8) ----------------------------------------------


def test_a_missing_gh_token_fails_closed(monkeypatch):
    def fake_run(cmd, **kw):
        class R:
            returncode = 1
            stdout = ""
            stderr = "gh: not logged in to any hosts"
        return R()
    monkeypatch.setattr(issue_gate.subprocess, "run", fake_run)
    with pytest.raises(issue_gate.BranchScanError, match="gh auth token"):
        issue_gate._resolve_gh_token("/usr/bin/gh", timeout=5)


def test_an_empty_gh_token_fails_closed(monkeypatch):
    """Exit 0 with blank output must not be mistaken for a usable token."""
    def fake_run(cmd, **kw):
        class R:
            returncode = 0
            stdout = "\n"
            stderr = ""
        return R()
    monkeypatch.setattr(issue_gate.subprocess, "run", fake_run)
    with pytest.raises(issue_gate.BranchScanError, match="gh auth token"):
        issue_gate._resolve_gh_token("/usr/bin/gh", timeout=5)


def test_a_timed_out_gh_token_call_fails_closed(monkeypatch):
    def fake_run(cmd, **kw):
        raise issue_gate.subprocess.TimeoutExpired(cmd=cmd, timeout=1)
    monkeypatch.setattr(issue_gate.subprocess, "run", fake_run)
    with pytest.raises(issue_gate.BranchScanError, match="gh auth token failed"):
        issue_gate._resolve_gh_token("/usr/bin/gh", timeout=5)


def test_the_token_is_resolved_once_and_reused_for_both_api_calls(monkeypatch, tmp_path):
    """One `gh auth token` call feeds both the registry read and the branch
    scan — not a separate resolution per call, and not a second subprocess
    call of any kind."""
    calls = []
    real_run = issue_gate.subprocess.run
    gh, _, opener = _stub_scan(monkeypatch, tmp_path, token="shared-tok",
                               agent_branches=[], name="sharedtoken")

    def recording_run(cmd, **kw):
        calls.append(cmd)
        return real_run(cmd, **kw)

    monkeypatch.setattr(issue_gate.subprocess, "run", recording_run)
    issue_gate.branches_referencing_issues("vivy", ["issue_01A"])
    assert len(calls) == 1, calls
    assert "auth" in calls[0] and "token" in calls[0]
    assert all(r.get_header("Authorization") == "Bearer shared-tok" for r in opener.requests)


# --- the App-token minter (CQ·407) ----------------------------------------------
#
# On a single-account Builder install (shipwright epic E11), agents
# authenticate to GitHub as a GitHub App rather than as the operator, so
# there is no `gh auth token` to read. When the install has configured an
# App-token minter (`github-app-token`, shipwright#143/comment 4926:
# configured by an `app-id` file under `~/.config/convivy/github-app`,
# `BUILDER_GITHUB_APP_DIR`-overridable), `_resolve_token` runs it instead of
# `gh auth token`, and never falls back to `gh` when the minter fails —
# a `gh` fallback would authenticate as the operator, exactly what E11
# removes.


def _stub_minter(tmp_path, name, *, token="minted-tok", returncode=0, stderr="",
                 sleep=None):
    """A `github-app-token` stand-in, linked into its own bin directory. The
    caller points `_minter_bin_dir()` at that directory
    (`_point_minter_bin_dir_at`) so it finds it the way the real install
    links the minter into Builder's own `~/.local/bin`."""
    bin_dir = tmp_path / f"{name}-bin"
    bin_dir.mkdir(exist_ok=True)
    minter = bin_dir / issue_gate._MINTER_NAME
    invoked = tmp_path / f"{name}-argv"
    script = "#!/bin/sh\n"
    if sleep is not None:
        script += f"sleep {sleep}\n"
    script += f'printf "%s\\n" "$@" >> {shlex.quote(str(invoked))}\n'
    if token:
        script += f'printf "%s\\n" {shlex.quote(token)}\n'
    if stderr:
        script += f'printf \'%s\\n\' {shlex.quote(stderr)} >&2\n'
    script += f"exit {returncode}\n"
    minter.write_text(script)
    minter.chmod(0o755)
    return bin_dir, invoked


def _point_minter_bin_dir_at(monkeypatch, bin_dir):
    """Replaces `_minter_bin_dir()` itself, the test seam, with one that
    returns `bin_dir` in place of the real `~/.local/bin`."""
    monkeypatch.setattr(issue_gate, "_minter_bin_dir", lambda: bin_dir)


def _configure_minter(monkeypatch, tmp_path):
    """An `app-id` file under a fresh `BUILDER_GITHUB_APP_DIR`, i.e. "a
    minter is configured" — the only thing `_minter_configured` checks."""
    app_dir = tmp_path / "github-app"
    app_dir.mkdir()
    (app_dir / "app-id").write_text("424242\n")
    monkeypatch.setenv(issue_gate._GITHUB_APP_DIR_ENV, str(app_dir))
    return app_dir


def _forbid_gh(monkeypatch):
    """`gh` must never be resolved, let alone run, while a minter is
    configured — a silent fallback would authenticate as the operator.
    Wraps `subprocess.run` rather than replacing it outright, since
    `_resolve_minted_token` itself runs the minter through the same
    function; only an invocation of `gh` is forbidden."""
    def fail_safe_which(name, **k):
        raise AssertionError(f"safe_which must not be called when a minter is configured "
                             f"(got {name!r})")
    monkeypatch.setattr(issue_gate.SP, "safe_which", fail_safe_which)

    real_run = issue_gate.subprocess.run

    def guarded_run(cmd, **kw):
        if Path(cmd[0]).name == "gh":
            raise AssertionError(f"subprocess.run must not be called for gh (got {cmd!r})")
        return real_run(cmd, **kw)
    monkeypatch.setattr(issue_gate.subprocess, "run", guarded_run)


def test_a_configured_minters_token_is_used_and_gh_is_never_called(monkeypatch, tmp_path):
    _configure_minter(monkeypatch, tmp_path)
    bin_dir, invoked = _stub_minter(tmp_path, "ok", token="ghs_minted")
    _point_minter_bin_dir_at(monkeypatch, bin_dir)
    _forbid_gh(monkeypatch)

    assert issue_gate._resolve_token() == "ghs_minted"
    assert invoked.read_text().strip() == "", "run with no arguments"


def test_a_minter_exiting_nonzero_is_refused_naming_the_minter(monkeypatch, tmp_path):
    _configure_minter(monkeypatch, tmp_path)
    bin_dir, _ = _stub_minter(tmp_path, "fail", returncode=1, token="",
                              stderr="github-app-token: app.pem is missing")
    _point_minter_bin_dir_at(monkeypatch, bin_dir)
    _forbid_gh(monkeypatch)

    with pytest.raises(issue_gate.BranchScanError, match="github-app-token") as exc:
        issue_gate._resolve_token()
    assert "app.pem is missing" in str(exc.value)


def test_a_minter_that_times_out_is_refused_naming_the_minter(monkeypatch, tmp_path):
    _configure_minter(monkeypatch, tmp_path)
    bin_dir, _ = _stub_minter(tmp_path, "slow", sleep=5)
    _point_minter_bin_dir_at(monkeypatch, bin_dir)
    _forbid_gh(monkeypatch)

    with pytest.raises(issue_gate.BranchScanError, match="github-app-token"):
        issue_gate._resolve_minted_token(timeout=0.2)


def test_a_minter_printing_nothing_is_refused_naming_the_minter(monkeypatch, tmp_path):
    _configure_minter(monkeypatch, tmp_path)
    bin_dir, _ = _stub_minter(tmp_path, "empty", token="", returncode=0)
    _point_minter_bin_dir_at(monkeypatch, bin_dir)
    _forbid_gh(monkeypatch)

    with pytest.raises(issue_gate.BranchScanError, match="github-app-token"):
        issue_gate._resolve_token()


def test_a_minter_absent_from_its_bin_dir_is_refused_naming_the_minter(monkeypatch, tmp_path):
    _configure_minter(monkeypatch, tmp_path)
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    _point_minter_bin_dir_at(monkeypatch, empty_bin)
    _forbid_gh(monkeypatch)

    with pytest.raises(issue_gate.BranchScanError, match="github-app-token"):
        issue_gate._resolve_token()


def test_with_no_app_id_file_the_gh_path_runs_as_today(monkeypatch, tmp_path):
    app_dir = tmp_path / "github-app"
    app_dir.mkdir()
    monkeypatch.setenv(issue_gate._GITHUB_APP_DIR_ENV, str(app_dir))
    gh, invoked = _fake_gh(tmp_path, "unconfigured", token="gh-token")
    monkeypatch.setattr(issue_gate.SP, "safe_which",
                        lambda n: str(gh) if n == "gh" else None)

    assert issue_gate._resolve_token() == "gh-token"
    assert len(_calls_from(invoked)) == 1


def test_a_configured_minters_token_flows_through_the_scan(monkeypatch, tmp_path):
    """The minted token, not a `gh` one, is what both the registry read and
    the branch scan send as their `Authorization` header."""
    _configure_minter(monkeypatch, tmp_path)
    bin_dir, invoked = _stub_minter(tmp_path, "scan", token="ghs_scan_tok")
    _point_minter_bin_dir_at(monkeypatch, bin_dir)
    _forbid_gh(monkeypatch)
    opener = _FakeOpener({
        _REGISTRY_URL: (_registry_contents_bytes(), None),
        _branches_url("convivy/vivy"): (b"[]", None),
    })
    monkeypatch.setattr(issue_gate, "_opener", lambda: opener)

    issue_gate.branches_referencing_issues("vivy", ["issue_01A"])
    assert len(invoked.read_text().splitlines()) == 1, "the minter is run exactly once, shared"
    assert all(r.get_header("Authorization") == "Bearer ghs_scan_tok" for r in opener.requests)


# --- the HTTPS call itself, in-process (round 9) --------------------------------
#
# Round 8's per-call `GH_CONFIG_DIR` closed the deterministic hole in round 7's
# environment inheritance, but not a timing one: a same-account process could
# watch for the fixed-prefix temp directory and plant a `config.yml` into it
# between `mkdtemp()` and `gh`'s `execve()` (round-8 security review, PR
# comment on #736). Round 9 drops `gh` the binary from both API calls
# entirely — no process, so no config file for anything to race into.


def test_the_token_is_sent_only_as_the_authorization_header(monkeypatch):
    opener = _FakeOpener({_branches_url("convivy/vivy"): (b"[]", None)})
    monkeypatch.setattr(issue_gate, "_opener", lambda: opener)
    issue_gate._github_api_get(
        issue_gate._MATCHING_REFS_PATH.format(slug="convivy/vivy"),
        token="sekrit", timeout=5)
    assert len(opener.requests) == 1
    req = opener.requests[0]
    assert req.get_header("Authorization") == "Bearer sekrit"
    assert "sekrit" not in req.full_url


def test_pagination_follows_link_rel_next_across_pages(monkeypatch):
    page1 = _branches_url("convivy/vivy")
    page2 = "https://api.github.com/repositories/1/git/matching-refs/heads/issue/?page=2"
    opener = _FakeOpener({
        page1: (json.dumps([{"ref": "refs/heads/issue/issue_01A"}]).encode(),
               f'<{page2}>; rel="next"'),
        page2: (json.dumps([{"ref": "refs/heads/issue/issue_02B"}]).encode(), None),
    })
    monkeypatch.setattr(issue_gate, "_opener", lambda: opener)
    result = issue_gate._github_api_get(
        issue_gate._MATCHING_REFS_PATH.format(slug="convivy/vivy"),
        token="t", timeout=5, paginate=True)
    assert result == [{"ref": "refs/heads/issue/issue_01A"}, {"ref": "refs/heads/issue/issue_02B"}]
    assert len(opener.requests) == 2


def test_a_next_link_with_other_relations_is_still_followed(monkeypatch):
    """A real `Link` header carries `rel="next"` alongside `rel="last"` (and
    sometimes `rel="first"`/`rel="prev"`), comma-separated — only `next`
    must be followed."""
    page1 = _branches_url("convivy/vivy")
    page2 = "https://api.github.com/repositories/1/git/matching-refs/heads/issue/?page=2"
    last = "https://api.github.com/repositories/1/git/matching-refs/heads/issue/?page=9"
    opener = _FakeOpener({
        page1: (json.dumps([{"ref": "refs/heads/issue/issue_01A"}]).encode(),
               f'<{page2}>; rel="next", <{last}>; rel="last"'),
        page2: (json.dumps([]).encode(), None),
    })
    monkeypatch.setattr(issue_gate, "_opener", lambda: opener)
    result = issue_gate._github_api_get(
        issue_gate._MATCHING_REFS_PATH.format(slug="convivy/vivy"),
        token="t", timeout=5, paginate=True)
    assert result == [{"ref": "refs/heads/issue/issue_01A"}]
    assert opener.requests[1].full_url == page2


def test_a_next_link_to_another_host_is_refused(monkeypatch):
    page1 = _branches_url("convivy/vivy")
    opener = _FakeOpener({page1: (b"[]", '<https://evil.example/steal>; rel="next"')})
    monkeypatch.setattr(issue_gate, "_opener", lambda: opener)
    with pytest.raises(issue_gate.BranchScanError, match="api.github.com"):
        issue_gate._github_api_get(
            issue_gate._MATCHING_REFS_PATH.format(slug="convivy/vivy"),
            token="t", timeout=5, paginate=True)


def test_a_next_link_over_plain_http_is_refused(monkeypatch):
    """Not just the host — the scheme too, so a downgrade can't strip the
    token off TLS on the next hop."""
    page1 = _branches_url("convivy/vivy")
    opener = _FakeOpener({page1: (b"[]", '<http://api.github.com/next>; rel="next"')})
    monkeypatch.setattr(issue_gate, "_opener", lambda: opener)
    with pytest.raises(issue_gate.BranchScanError, match="api.github.com"):
        issue_gate._github_api_get(
            issue_gate._MATCHING_REFS_PATH.format(slug="convivy/vivy"),
            token="t", timeout=5, paginate=True)


def test_a_next_link_with_an_unparseable_host_is_refused(monkeypatch):
    """`urllib.parse.urlsplit` raises a bare `ValueError` ("Invalid IPv6
    URL") on a malformed bracketed netloc, before `_next_page_url`'s own
    off-host check ever runs — confirmed directly against CPython:
    `urlsplit('https://[bad/x')` raises. Neither this function's prior code
    nor `_github_api_get`'s call site caught anything but `BranchScanError`
    at that line, so this would otherwise escape both uncaught (round-11
    security review, PR comment on #736)."""
    url = _branches_url("convivy/vivy")
    opener = _FakeOpener({url: (b"[]", '<https://[bad/x>; rel="next"')})
    monkeypatch.setattr(issue_gate, "_opener", lambda: opener)
    with pytest.raises(issue_gate.BranchScanError, match="unparseable next URL"):
        issue_gate._github_api_get(
            issue_gate._MATCHING_REFS_PATH.format(slug="convivy/vivy"),
            token="t", timeout=5, paginate=True)


def test_an_unparseable_next_link_makes_eligible_issues_fail_closed(monkeypatch, tmp_path):
    """The same malformed `Link` header, reached through the real
    `eligible_issues` -> `branches_referencing_issues` -> `_github_api_get`
    path rather than a mocked `branches_referencing_issues` — confirming the
    new `BranchScanError` actually reaches `eligible_issues`'s own `except`
    and holds back every issue, instead of escaping past it and aborting the
    caller's whole per-product loop the way it would have for
    `bin/issue-queue-drain`'s `_claimable`/`main`, which wrapped no call in
    this module in any `try`/`except` of their own (that script is since
    deleted; `eligible_issues` has to stay defensive on its own regardless of
    what today's caller, `bin/issue-eligible`, does around it)."""
    _stub_scan(monkeypatch, tmp_path, registry={"vivy": {"repos": ["vivy"]}},
              branches_error=(b"[]", '<https://[bad/x>; rel="next"'))
    eligible, skipped = issue_gate.eligible_issues("vivy", items=[_issue(), _issue("issue_02B")])
    assert eligible == []
    assert len(skipped) == 2
    assert all("branch scan unavailable" in r for _, r in skipped)


@pytest.mark.parametrize("status,reason", [(404, "Not Found"), (403, "Forbidden")])
def test_an_http_error_status_raises_branch_scan_error(monkeypatch, status, reason):
    url = _branches_url("convivy/vivy")
    err = issue_gate.urllib.error.HTTPError(url, status, reason, {}, io.BytesIO(b""))
    opener = _FakeOpener({url: err})
    monkeypatch.setattr(issue_gate, "_opener", lambda: opener)
    with pytest.raises(issue_gate.BranchScanError, match=str(status)):
        issue_gate._github_api_get(
            issue_gate._MATCHING_REFS_PATH.format(slug="convivy/vivy"), token="t", timeout=5)


def test_a_url_error_raises_branch_scan_error(monkeypatch):
    url = _branches_url("convivy/vivy")
    opener = _FakeOpener({url: issue_gate.urllib.error.URLError("no route to host")})
    monkeypatch.setattr(issue_gate, "_opener", lambda: opener)
    with pytest.raises(issue_gate.BranchScanError):
        issue_gate._github_api_get(
            issue_gate._MATCHING_REFS_PATH.format(slug="convivy/vivy"), token="t", timeout=5)


def test_a_timeout_raises_branch_scan_error(monkeypatch):
    url = _branches_url("convivy/vivy")
    opener = _FakeOpener({url: TimeoutError("timed out")})
    monkeypatch.setattr(issue_gate, "_opener", lambda: opener)
    with pytest.raises(issue_gate.BranchScanError):
        issue_gate._github_api_get(
            issue_gate._MATCHING_REFS_PATH.format(slug="convivy/vivy"), token="t", timeout=5)


def test_a_bad_status_line_raises_branch_scan_error(monkeypatch):
    """`http.client.BadStatusLine` (a malformed HTTP status line) comes from
    `h.getresponse()` inside `opener.open()`, same as `URLError`/timeout, but
    is not an `OSError` subclass — round-9 review, PR comment on #736."""
    url = _branches_url("convivy/vivy")
    opener = _FakeOpener({url: http.client.BadStatusLine("garbage")})
    monkeypatch.setattr(issue_gate, "_opener", lambda: opener)
    with pytest.raises(issue_gate.BranchScanError):
        issue_gate._github_api_get(
            issue_gate._MATCHING_REFS_PATH.format(slug="convivy/vivy"), token="t", timeout=5)


def test_an_incomplete_read_raises_branch_scan_error(monkeypatch):
    """`http.client.IncompleteRead` (a response truncated mid-body) comes
    from `resp.read()`, after `opener.open()` already returned a response
    object — also not an `OSError` subclass. A truncated response during the
    branch scan must not escape as an unhandled exception and abort a
    caller's whole per-product loop (round-9 review, PR comment on #736)."""
    url = _branches_url("convivy/vivy")
    response = _FakeResponseThatFailsToRead(http.client.IncompleteRead(b"", 10))
    opener = _FakeOpener({url: response})
    monkeypatch.setattr(issue_gate, "_opener", lambda: opener)
    with pytest.raises(issue_gate.BranchScanError):
        issue_gate._github_api_get(
            issue_gate._MATCHING_REFS_PATH.format(slug="convivy/vivy"), token="t", timeout=5)


def test_malformed_json_raises_branch_scan_error(monkeypatch):
    url = _branches_url("convivy/vivy")
    opener = _FakeOpener({url: (b"not json at all", None)})
    monkeypatch.setattr(issue_gate, "_opener", lambda: opener)
    with pytest.raises(issue_gate.BranchScanError, match="JSON"):
        issue_gate._github_api_get(
            issue_gate._MATCHING_REFS_PATH.format(slug="convivy/vivy"), token="t", timeout=5)


def test_a_non_utf8_body_raises_branch_scan_error(monkeypatch):
    """`json.loads` decodes bytes itself before parsing; a body whose bytes
    aren't valid in the encoding it detects raises `UnicodeDecodeError` —a
    `ValueError` subclass, but NOT a `json.JSONDecodeError` subclass, so the
    narrower except clause round 10 shipped does not catch it (round-10
    security review, PR comment on #736). Modeled on the same corrupted-byte
    class `BadStatusLine`/`IncompleteRead` cover one layer up."""
    url = _branches_url("convivy/vivy")
    opener = _FakeOpener({url: (b"\xff\xfe{", None)})
    monkeypatch.setattr(issue_gate, "_opener", lambda: opener)
    with pytest.raises(issue_gate.BranchScanError, match="JSON"):
        issue_gate._github_api_get(
            issue_gate._MATCHING_REFS_PATH.format(slug="convivy/vivy"), token="t", timeout=5)


def test_a_deeply_nested_body_raises_branch_scan_error(monkeypatch):
    """A pathologically nested body blows `json.loads`'s own recursive
    parser past Python's recursion limit, raising `RecursionError` — not a
    `ValueError` at all, so neither this nor the prior round's except clause
    catches it without naming it (round-10 security review, PR comment on
    #736)."""
    url = _branches_url("convivy/vivy")
    opener = _FakeOpener({url: (b"[" * 100_000, None)})
    monkeypatch.setattr(issue_gate, "_opener", lambda: opener)
    with pytest.raises(issue_gate.BranchScanError, match="JSON"):
        issue_gate._github_api_get(
            issue_gate._MATCHING_REFS_PATH.format(slug="convivy/vivy"), token="t", timeout=5)


def test_a_non_array_paginated_response_raises_branch_scan_error(monkeypatch):
    """`paginate=True` is only ever used on an endpoint documented to return
    a JSON array; a body that is some other shape must not be read as an
    empty or partial result."""
    url = _branches_url("convivy/vivy")
    opener = _FakeOpener({url: (json.dumps({"message": "not an array"}).encode(), None)})
    monkeypatch.setattr(issue_gate, "_opener", lambda: opener)
    with pytest.raises(issue_gate.BranchScanError, match="not a JSON array"):
        issue_gate._github_api_get(
            issue_gate._MATCHING_REFS_PATH.format(slug="convivy/vivy"),
            token="t", timeout=5, paginate=True)


def test_pagination_past_the_page_ceiling_raises_branch_scan_error(monkeypatch):
    """A `Link` header that never stops pointing `rel="next"` — a misbehaving
    or cycling pagination, exactly what `_MAX_API_PAGES` exists to turn into
    one loud failure rather than an all-night loop (round 9). The same URL
    is served every time with a `next` link back to itself, so the opener is
    called exactly `_MAX_API_PAGES` times before the ceiling trips; an
    off-by-one in the loop would change that count."""
    url = _branches_url("convivy/vivy")
    page = (json.dumps([{"ref": "refs/heads/issue/issue_01A"}]).encode(),
            f'<{url}>; rel="next"')
    opener = _FakeOpener({url: page})
    monkeypatch.setattr(issue_gate, "_opener", lambda: opener)
    with pytest.raises(issue_gate.BranchScanError, match=str(issue_gate._MAX_API_PAGES)):
        issue_gate._github_api_get(
            issue_gate._MATCHING_REFS_PATH.format(slug="convivy/vivy"),
            token="t", timeout=5, paginate=True)
    assert len(opener.requests) == issue_gate._MAX_API_PAGES


def test_a_contents_response_decodes_to_the_registry(monkeypatch):
    registry = {"vivy": {"repos": ["vivy"]}}
    opener = _FakeOpener({_REGISTRY_URL: (_registry_contents_bytes(registry), None)})
    monkeypatch.setattr(issue_gate, "_opener", lambda: opener)
    assert issue_gate._registry_at_origin_main(token="t") == registry


def test_proxy_environment_variables_are_never_used(monkeypatch):
    """`_opener()` builds its `ProxyHandler` from an explicit empty dict, not
    from the environment — proxy variables are agent-settable and would
    otherwise route the call (and the bearer token riding on it) through a
    listener of the agent's choosing. Calling `_opener()` itself never opens
    a connection, so this needs no network stub."""
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1/")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1/")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1/")
    captured = {}
    real_build_opener = issue_gate.urllib.request.build_opener

    def recording_build_opener(*handlers):
        captured["handlers"] = handlers
        return real_build_opener(*handlers)

    monkeypatch.setattr(issue_gate.urllib.request, "build_opener", recording_build_opener)
    issue_gate._opener()
    proxy_handlers = [h for h in captured["handlers"]
                      if isinstance(h, issue_gate.urllib.request.ProxyHandler)]
    assert proxy_handlers, "no ProxyHandler was passed to build_opener"
    assert all(h.proxies == {} for h in proxy_handlers), \
        "the ProxyHandler must be built from an explicit empty dict, never the environment"


def test_a_planted_gh_config_cannot_affect_the_scan(monkeypatch, tmp_path):
    """The round-7/round-8 reproduction (PR comment 5939772429, and the
    round-8 security review's TOCTOU finding on #736): a `GH_CONFIG_DIR`
    pointed at a `config.yml` carrying `http_unix_socket` used to be able to
    reroute a `gh api` call through a local socket. ROUND 9: `gh` is never
    run for the registry read or the branch scan any more, only for `gh auth
    token`, which issues no HTTP request — so the planted key has nothing
    left to act on. Plants it anyway and proves the socket is never dialed.
    """
    sock_dir = tempfile.mkdtemp(prefix="ig-sock-")
    sock_path = Path(sock_dir) / "evil.sock"
    connections = []
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    srv.listen(1)
    srv.settimeout(2)  # a safety net only; closing srv below unblocks accept() sooner

    def serve_once():
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        connections.append(True)
        with contextlib.suppress(OSError):
            conn.close()

    thread = threading.Thread(target=serve_once, daemon=True)
    thread.start()
    try:
        config_dir = tmp_path / "planted-gh-config"
        config_dir.mkdir()
        (config_dir / "config.yml").write_text(f"http_unix_socket: {sock_path}\n")
        monkeypatch.setenv("GH_CONFIG_DIR", str(config_dir))
        iid = _fake_id("a")
        _stub_scan(monkeypatch, tmp_path, agent_branches=[_branch_for_issue(iid)],
                  name="plantedcfg")

        result = issue_gate.branches_referencing_issues("vivy", [iid])
        assert result == {iid: _branch_for_issue(iid)}
    finally:
        # Closing the listening socket unblocks a still-waiting accept()
        # immediately, rather than leaving the thread to sit out its full
        # settimeout — the call above has already finished by this point.
        srv.close()
        thread.join(timeout=3)
        with contextlib.suppress(OSError):
            sock_path.unlink()
        with contextlib.suppress(OSError):
            Path(sock_dir).rmdir()

    assert not connections, "a planted http_unix_socket must never be dialed"


# --- the branch scan itself (GitHub API) ----------------------------------------
#
# `gh` is always stubbed at the `safe_which` boundary below, and the two HTTPS
# calls always go through a `_FakeOpener` — never the real system `gh` and
# never the real network.


def test_scan_finds_issue_branches_via_the_api(monkeypatch, tmp_path):
    id_a, id_b = _fake_id("a"), _fake_id("b")
    branch_a, branch_b = _branch_for_issue(id_a), _branch_for_issue(id_b)
    _stub_scan(monkeypatch, tmp_path, agent_branches=[branch_a, branch_b], name="found")
    assert issue_gate.branches_referencing_issues("vivy", [id_a, id_b]) == \
        {id_a: branch_a, id_b: branch_b}


def test_scan_matches_a_branch_with_trailing_text_after_the_fragment(monkeypatch, tmp_path):
    """A retry dispatch's branch carries the task-hash suffix after the id
    fragment (`_branch_for_issue`'s own default), and a human-renamed or
    hand-pushed branch could carry anything else after it too — the match is
    substring containment, not an exact-tail comparison, so either still
    counts as work in flight."""
    iid = _fake_id("a")
    branch = _branch_for_issue(iid, suffix="-retry-2-abcd1234")
    _stub_scan(monkeypatch, tmp_path, agent_branches=[branch], name="suffixed")
    assert issue_gate.branches_referencing_issues("vivy", [iid]) == {iid: branch}


def test_scan_ignores_an_agent_branch_for_no_known_issue(monkeypatch, tmp_path):
    """A branch on the remote that matches none of `issue_ids` — a worker
    dispatched for something other than a work item — must not show up as
    "in flight" for an id it has nothing to do with."""
    known = _fake_id("a")
    _stub_scan(monkeypatch, tmp_path, agent_branches=[_branch_for_issue(_fake_id("z"))],
              name="unrelated")
    assert issue_gate.branches_referencing_issues("vivy", [known]) == {}


def test_scan_of_a_repo_with_no_agent_branches_is_empty_not_an_error(monkeypatch, tmp_path):
    _stub_scan(monkeypatch, tmp_path, agent_branches=[], name="empty")
    assert issue_gate.branches_referencing_issues("vivy", [_fake_id("a")]) == {}


def test_scan_ignores_a_malformed_entry_in_the_result_array(monkeypatch, tmp_path):
    """A real `matching-refs` response is a uniform array of ref objects, but
    this must not crash on a non-dict element or one missing `ref`."""
    gh, _ = _fake_gh(tmp_path, "malformedentry")
    monkeypatch.setattr(issue_gate.SP, "safe_which",
                        lambda name: str(gh) if name == "gh" else None)
    iid = _fake_id("a")
    branch = _branch_for_issue(iid)
    raw_entries = ["not-a-dict-just-a-string", {"no_ref_field": True},
                  {"ref": f"refs/heads/{branch}"}]
    opener = _FakeOpener({
        _REGISTRY_URL: (_registry_contents_bytes(), None),
        _branches_url("convivy/vivy"): (json.dumps(raw_entries).encode(), None),
    })
    monkeypatch.setattr(issue_gate, "_opener", lambda: opener)
    assert issue_gate.branches_referencing_issues("vivy", [iid]) == {iid: branch}


def test_scan_ignores_a_non_agent_ref(monkeypatch, tmp_path):
    """The matching-refs query itself is scoped to `heads/agent/`, but this
    also guards the parse against a ref GitHub returned that does not
    actually start with that literal prefix (defensive, same posture as the
    malformed-entry test above)."""
    gh, _ = _fake_gh(tmp_path, "nonagent")
    monkeypatch.setattr(issue_gate.SP, "safe_which",
                        lambda name: str(gh) if name == "gh" else None)
    opener = _FakeOpener({
        _REGISTRY_URL: (_registry_contents_bytes(), None),
        _branches_url("convivy/vivy"): (
            json.dumps([{"ref": "refs/heads/agentx/not-really-agent"}]).encode(), None),
    })
    monkeypatch.setattr(issue_gate, "_opener", lambda: opener)
    assert issue_gate.branches_referencing_issues("vivy", [_fake_id("a")]) == {}


def test_the_scan_requests_the_matching_refs_endpoint_and_paginates(monkeypatch, tmp_path):
    """The exact `matching-refs` path built from the registry's owner/repo —
    not from any checkout's remote — and pagination is followed via `Link`,
    not an `--paginate` flag (there is no `gh` process left to pass it to)."""
    gh, invoked, opener = _stub_scan(monkeypatch, tmp_path, agent_branches=[], name="argv")
    issue_gate.branches_referencing_issues("vivy", [_fake_id("a")])
    branch_reqs = [r for r in opener.requests if r.full_url == _branches_url("convivy/vivy")]
    assert len(branch_reqs) == 1, opener.requests
    assert branch_reqs[0].full_url.startswith(
        "https://api.github.com/repos/convivy/vivy/git/matching-refs/heads/agent/")


# --- resolving gh safely for the token call --------------------------------------


def test_the_token_call_resolves_gh_via_safe_which_not_a_bare_name(monkeypatch, tmp_path):
    """argv[0] has to be the absolute path `safe_which` computed, not a bare
    "gh" left to subprocess's own PATH search — the resolution has to be the
    one `safe_which` computed, or routing through it is pointless."""
    resolved = str(tmp_path / "real" / "bin" / "gh")
    resolved_dir = Path(resolved).parent
    resolved_dir.mkdir(parents=True)
    Path(resolved).write_text("#!/bin/sh\nprintf 'tok\\n'\n")
    Path(resolved).chmod(0o755)
    monkeypatch.setattr(issue_gate.SP, "safe_which",
                        lambda name: resolved if name == "gh" else None)
    seen = []
    real_run = issue_gate.subprocess.run

    def fake_run(cmd, **kw):
        seen.append(cmd)
        return real_run(cmd, **kw)

    monkeypatch.setattr(issue_gate.subprocess, "run", fake_run)
    opener = _FakeOpener({
        _REGISTRY_URL: (_registry_contents_bytes(), None),
        _branches_url("convivy/vivy"): (b"[]", None),
    })
    monkeypatch.setattr(issue_gate, "_opener", lambda: opener)
    issue_gate.branches_referencing_issues("vivy", [_fake_id("a")])
    assert seen, "subprocess.run was never called"
    assert all(cmd[0] == resolved for cmd in seen), seen


def test_the_scan_does_not_choose_a_planted_gh_under_a_fake_home(monkeypatch, tmp_path):
    """`safe_which` never reads PATH or HOME — the real end-to-end exercise of
    that protection, with no mock on `safe_which` itself: a `gh` planted
    under this account's home, with PATH pointed at it, must never be the one
    resolved. `subprocess.run` is still faked here so nothing is actually
    executed — the assertion is purely on which path was chosen."""
    fake_home = tmp_path / "fake-home"
    planted_dir = fake_home / ".local" / "bin"
    planted_dir.mkdir(parents=True)
    planted = planted_dir / "gh"
    planted.write_text("#!/bin/sh\necho planted\n")
    planted.chmod(0o755)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("PATH", str(planted_dir))
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        class R:
            returncode = 0
            stdout = "tok\n"
            stderr = ""
        return R()

    monkeypatch.setattr(issue_gate.subprocess, "run", fake_run)
    opener = _FakeOpener({
        _REGISTRY_URL: (_registry_contents_bytes(), None),
        _branches_url("convivy/vivy"): (b"[]", None),
    })
    monkeypatch.setattr(issue_gate, "_opener", lambda: opener)
    issue_gate.branches_referencing_issues("vivy", [_fake_id("a")])
    assert seen, "subprocess.run was never called"
    for cmd in seen:
        assert cmd[0] != str(planted), \
            f"a planted gh under HOME must not be chosen, got {cmd[0]!r}"
        assert str(planted_dir) not in cmd[0]


def test_the_scan_fails_closed_when_safe_which_finds_no_gh(monkeypatch):
    """A bare "gh" fallback would hand subprocess the unfiltered PATH to
    search — exactly the gap this module routes around. When `safe_which`
    finds nothing safe, this must raise BranchScanError, not fall back and
    run anyway."""
    monkeypatch.setattr(issue_gate.SP, "safe_which", lambda name, **k: None)

    def fake_run(*a, **k):
        raise AssertionError("subprocess.run must not be called when no safe gh was found")

    monkeypatch.setattr(issue_gate.subprocess, "run", fake_run)
    with pytest.raises(issue_gate.BranchScanError, match="no safe gh"):
        issue_gate.branches_referencing_issues("vivy", [_fake_id("a")])


# --- failing closed on a bad call ------------------------------------------------


def test_the_scan_raises_on_an_http_error(monkeypatch, tmp_path):
    """Covers a missing/expired token (401), a rate limit (403), and any
    other non-2xx alike — none of them are mistaken for a complete, empty
    result."""
    err = issue_gate.urllib.error.HTTPError(
        _branches_url("convivy/vivy"), 403, "rate limit exceeded", {}, io.BytesIO(b""))
    _stub_scan(monkeypatch, tmp_path, branches_error=err, name="ratelimited")
    with pytest.raises(issue_gate.BranchScanError, match="403"):
        issue_gate.branches_referencing_issues("vivy", [_fake_id("a")])


def test_the_scan_raises_on_a_timeout(monkeypatch, tmp_path):
    """The branch-scan call itself timing out, after the token and registry
    calls succeeded — `_resolve_gh_token`'s own timeout path is covered
    separately by `test_a_timed_out_gh_token_call_fails_closed`."""
    _stub_scan(monkeypatch, tmp_path, branches_error=TimeoutError("timed out"), name="timeout")
    with pytest.raises(issue_gate.BranchScanError, match="GitHub API GET"):
        issue_gate.branches_referencing_issues("vivy", [_fake_id("a")])


# --- round 6/7's findings stay closed: no checkout, no git or ssh process -------
#
# Round 6 pinned ssh/git config but still ran `git -C <checkout> ls-remote`.
# The round-6 security review (PR comment 5938296499) found three more ways a
# dispatched coder's write access to a product checkout reached that call:
# `core.askPass` (fires even with `GIT_TERMINAL_PROMPT=0` set),
# `GIT_CONFIG_PARAMETERS` (uncovered by the strip loop), and a repointed
# `origin`. All three were reproduced directly against round 6's actual code
# before this fix was written — the commands and their output (including the
# marker file created by the askpass plant) are in the PR body — and round
# 6's code no longer exists in this tree to run them against again. Round 7's
# fix removed the checkout from the call entirely, and round 9 removed `gh`
# itself from both API calls, so these tests prove it structurally: every
# subprocess invocation this function makes is recorded, and there is
# exactly one — `gh auth token` — not three.


def test_the_scan_never_execs_git_or_ssh_even_with_a_planted_askpass(monkeypatch, tmp_path):
    """Plants both of round 6's config-reachable levers at once: a product
    checkout's own `.git/config` setting `core.askPass` to a marker program,
    with `remote.origin.url` pointed at a non-GitHub https host (the exact
    round-6 finding), AND `GIT_CONFIG_PARAMETERS` carrying the same key in
    the CALLER's own environment (the finding that bypassed round 6's strip
    loop, with no checkout config needed at all). The one real subprocess
    call this module still makes (`gh auth token`) still runs correctly
    (stubbed, no network) and the marker never fires, because nothing in
    this module execs git or ssh any more."""
    marker = tmp_path / "askpass-marker"
    checkout = tmp_path / "product-checkout"
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    askpass = tmp_path / "plant-askpass.sh"
    askpass.write_text(f"#!/bin/sh\ntouch {marker}\necho fake-password\n")
    askpass.chmod(0o755)
    subprocess.run(["git", "-C", str(checkout), "config", "core.askPass", str(askpass)],
                   check=True)
    subprocess.run(["git", "-C", str(checkout), "config", "remote.origin.url",
                    "https://not-github.example/attacker/repo.git"], check=True)
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", f"'core.askPass={askpass}'")

    iid = _fake_id("a")
    branch = _branch_for_issue(iid)
    gh, _, _ = _stub_scan(monkeypatch, tmp_path, agent_branches=[branch], name="askpass")
    calls = []
    real_run = issue_gate.subprocess.run

    def recording_run(cmd, **kw):
        calls.append(cmd)
        return real_run(cmd, **kw)

    monkeypatch.setattr(issue_gate.subprocess, "run", recording_run)

    result = issue_gate.branches_referencing_issues("vivy", [iid])

    assert result == {iid: branch}
    assert not marker.exists(), "a planted core.askPass must never run"
    assert len(calls) == 1, calls
    assert calls[0][0] == str(gh), f"the one subprocess call must be gh, got {calls}"


def test_the_scan_never_reads_any_checkouts_origin(monkeypatch, tmp_path):
    """Round 6 review's third finding: repointing a checkout's `origin` at an
    empty mirror made the old scan see zero branches and hand out duplicate
    work. Round 7 keys on the registry (`config/projects.json`), not on any
    checkout's remote, so an origin planted anywhere — even one named exactly
    like the product this gate is scanning — changes nothing."""
    checkout = tmp_path / "vivy"
    empty_mirror = tmp_path / "empty-mirror.git"
    subprocess.run(["git", "init", "--bare", "-q", str(empty_mirror)], check=True)
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    subprocess.run(["git", "-C", str(checkout), "remote", "add", "origin", str(empty_mirror)],
                   check=True)

    iid = _fake_id("z")
    branch = _branch_for_issue(iid)
    _stub_scan(monkeypatch, tmp_path, agent_branches=[branch], name="origin")
    assert issue_gate.branches_referencing_issues("vivy", [iid]) == {iid: branch}


# --- unknown is not empty -----------------------------------------------------


def test_an_unreadable_queue_raises_rather_than_reporting_empty(monkeypatch):
    """The distinction the whole exit-code contract rests on. An empty list means
    'the queue is clear', and a caller acts on it by reporting a quiet night. If
    an unreachable API produced the same value, an outage would be
    indistinguishable from an empty queue and the drain would report success
    while blind — ARCH-311's silent no-op, one layer down."""
    import issue_client
    monkeypatch.setattr(issue_client, "token", lambda: "tok")

    def boom(*a, **k):
        raise OSError("connection refused")
    monkeypatch.setattr(issue_client.urllib.request, "urlopen", boom)
    with pytest.raises(issue_client.IssueQueueUnavailable):
        issue_client.list_claimable_issues("vivy")


def test_a_missing_token_is_unavailable_not_empty(monkeypatch):
    import issue_client
    monkeypatch.setattr(issue_client, "token", lambda: None)
    with pytest.raises(issue_client.IssueQueueUnavailable):
        issue_client.list_claimable_issues("vivy")


def test_a_malformed_payload_is_unavailable_not_empty(monkeypatch):
    """A 200 carrying something other than an items list means we did not get the
    queue, whatever the status code said."""
    import io
    import issue_client
    monkeypatch.setattr(issue_client, "token", lambda: "tok")

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(issue_client.urllib.request, "urlopen",
                        lambda *a, **k: _Resp(b'{"error": "boom"}'))
    with pytest.raises(issue_client.IssueQueueUnavailable):
        issue_client.list_claimable_issues("vivy")


def test_the_cli_exits_nonzero_when_the_queue_is_unreadable(issue_eligible, monkeypatch,
                                                            capsys):
    """bin/issue-eligible's documented contract: exit 0 means the gate RAN.
    launch-overnight reads a non-zero exit as 'claim nothing and say so'."""
    import issue_client
    cli = issue_eligible

    def boom(*a, **k):
        raise issue_client.IssueQueueUnavailable("api down")
    monkeypatch.setattr(issue_gate, "eligible_issues", boom)
    assert cli.main(["--product", "vivy"]) == 1
    assert "cannot read the issue queue" in capsys.readouterr().err


def test_the_cli_exits_zero_on_a_genuinely_empty_queue(issue_eligible, monkeypatch, capsys):
    """The other half: a quiet night is a success, not a failure."""
    cli = issue_eligible
    monkeypatch.setattr(issue_gate, "eligible_issues", lambda *a, **k: ([], []))
    assert cli.main(["--product", "vivy"]) == 0
    assert capsys.readouterr().out.strip() == ""


def _fake_urlopen(monkeypatch, by_state):
    """Serve canned /api/issues responses keyed by the state= query value."""
    import io
    import issue_client

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake(req, *a, **k):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        state = url.split("state=")[1].split("&")[0]
        import json as _j
        return _Resp(_j.dumps({"items": by_state.get(state, [])}).encode())

    monkeypatch.setattr(issue_client, "token", lambda: "tok")
    monkeypatch.setattr(issue_client.urllib.request, "urlopen", fake)


def test_a_server_predating_claimable_is_detected_not_read_as_empty(monkeypatch):
    """THE DEPLOY-ORDER TRAP. An orient without the claim spine treats `claimable`
    as a plain state, matches nothing, and answers 200 with an empty list —
    identical to an empty queue. Verified against the live API 2026-07-18: 0
    claimable while 13 were open. It fails closed, so nothing is duplicated, but
    it fails SILENTLY: the drain reports a clean night while the whole queue is
    invisible. That must be loud."""
    import issue_client
    _fake_urlopen(monkeypatch, {"claimable": [], "open": [{"id": "issue_01A"}]})
    with pytest.raises(issue_client.IssueQueueUnavailable) as e:
        issue_client.list_claimable_issues("vivy")
    assert "predates state=claimable" in str(e.value)


def test_a_genuinely_empty_queue_stays_empty(monkeypatch):
    """The other side: when nothing is open either, the queue really is clear and
    an empty list is the correct, non-alarming answer."""
    import issue_client
    _fake_urlopen(monkeypatch, {"claimable": [], "open": []})
    assert issue_client.list_claimable_issues("vivy") == []


def test_a_nonempty_claimable_result_skips_the_probe(monkeypatch):
    """The probe costs a request, so it must only ever run on the empty path."""
    import issue_client
    calls = []
    _fake_urlopen(monkeypatch, {"claimable": [{"id": "issue_01A"}], "open": []})
    real = issue_client.urllib.request.urlopen

    def counting(req, *a, **k):
        calls.append(getattr(req, "full_url", str(req)))
        return real(req, *a, **k)

    monkeypatch.setattr(issue_client.urllib.request, "urlopen", counting)
    assert len(issue_client.list_claimable_issues("vivy")) == 1
    assert len(calls) == 1, calls
