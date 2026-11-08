"""`bin/github-activity-capture` (E25, issue_01M4ZB5C5ZX0MV206064MTRAE0) — the
launchd entry point that walks every convivy-bosun App installation's
repositories and calls `lib/agent-platform/github_activity.py`'s
`capture_repository` for each one.

No network anywhere in this file: `GA.installations`/`GA.installation_token`/
`GA.request` and this script's own `list_installation_repos` are all
monkeypatched, and `github_activity.capture_repository` itself is replaced
with a fake that records what it was called with — the per-repository read
and signal shapes are already covered by tests/test_github_activity_capture.py
and tests/test_github_activity_signals.py. This file covers everything this
script adds on top: which installations and repositories get read, under
which token, which credential gates the whole pass, and the per-repository
cursor save.

`--audit-window` (CQ·494, E25 PR 4) gets its own section below: the same
installation/repository monkeypatching as above, plus a fake `GET
/whoami`/`GET /signals` Library (routed by URL, since `whoami` and
`_read_ingested_urls` build real requests) and `github_activity`'s own
fetchers (`fetch_review_comments`, `fetch_conversation_comments`,
`fetch_updated_pull_requests`, `fetch_reviews`) monkeypatched the same way
`capture_repository` is above.
"""
from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SIGNAL_ENV = "GITHUB_ACTIVITY_CAPTURE_TOKEN"


@pytest.fixture(autouse=True)
def _state_dir(github_activity_capture, tmp_path, monkeypatch):
    """Every test gets its own cursor `STATE_DIR` on the `github_activity`
    module this script's cursor calls go through, never the real
    `~/.local/state/agent-platform/github-activity-capture/`."""
    monkeypatch.setattr(github_activity_capture.GACT, "STATE_DIR",
                        tmp_path / "github-activity-capture-state")


@pytest.fixture(autouse=True)
def _signal_credential(monkeypatch):
    """Every test gets a posting credential by default — the no-credential
    path is its own test below, which deletes it again."""
    monkeypatch.setenv(SIGNAL_ENV, "test-secret")
    yield
    monkeypatch.delenv(SIGNAL_ENV, raising=False)


_CLEAN_RESULT = {
    "signals": {"review_verdict": 0, "changelog": 0, "review_comment": 0, "pr_comment": 0},
    "posts": {"created": 0, "duplicate": 0, "skip": 0, "error": 0},
    "read_errors": 0,
    "may_advance": True,
    "new_cursor": "2026-10-15T00:00:00Z",
}


class _FakeCapture:
    """A stand-in for `github_activity.capture_repository`: records every
    `(repo, cursor, token)` it was called with, raises whatever
    `raise_by_repo` names for that repo, and otherwise returns
    `result_by_repo[repo]` (or `_CLEAN_RESULT` for any repo not named)."""

    def __init__(self, *, result_by_repo=None, raise_by_repo=None):
        self.result_by_repo = result_by_repo or {}
        self.raise_by_repo = raise_by_repo or {}
        self.calls: list[tuple[str, object, str]] = []

    def __call__(self, repo, cursor, token, post, pass_start, *, opener=None):
        self.calls.append((repo, cursor, token))
        if repo in self.raise_by_repo:
            raise self.raise_by_repo[repo]
        return self.result_by_repo.get(repo, dict(_CLEAN_RESULT))


def _stub_app_auth(github_activity_capture, monkeypatch):
    monkeypatch.setattr(
        github_activity_capture.GA, "installed_app",
        lambda: github_activity_capture.GA.InstalledApp(
            app_id="1234567", key_path=Path("/dev/null"), slug="test-app"))
    monkeypatch.setattr(github_activity_capture.GA, "key_problem", lambda *a, **k: None)
    monkeypatch.setattr(github_activity_capture.GA, "app_jwt", lambda *a, **k: "jwt.x.y")


def _wire(github_activity_capture, monkeypatch, *, installations, repos_by_token,
         fake_capture, mint_raises=None, installations_raises=None,
         list_repos_raises=None, rate_limit_raises=None):
    """Wiring shared by every `main()` test below. `mint_raises` maps an
    installation id (as a string) to an `Exception` its token mint should
    raise instead of succeeding — the shape `test_pr_state_capture.py`'s own
    `_wire_multi_owner_main` uses for a per-owner failure. `installations_raises`
    is an `Exception` `GA.installations` itself should raise. `list_repos_raises`
    and `rate_limit_raises` each map a token (as `repos_by_token` does) to an
    `Exception` that call should raise for that owner's token instead of
    succeeding."""
    _stub_app_auth(github_activity_capture, monkeypatch)

    def fake_installations(jwt, **k):
        if installations_raises:
            raise installations_raises
        return installations

    monkeypatch.setattr(github_activity_capture.GA, "installations", fake_installations)

    minted: list[tuple[str, dict]] = []

    def fake_mint(jwt, inst_id, *, permissions=None, repositories=None, opener=None):
        minted.append((inst_id, permissions))
        if mint_raises and inst_id in mint_raises:
            raise mint_raises[inst_id]
        return f"token-for-{inst_id}"

    monkeypatch.setattr(github_activity_capture.GA, "installation_token", fake_mint)

    def fake_list_repos(token, *, opener=None):
        if list_repos_raises and token in list_repos_raises:
            raise list_repos_raises[token]
        return repos_by_token[token]

    monkeypatch.setattr(github_activity_capture, "list_installation_repos", fake_list_repos)

    def fake_request(method, url, bearer, body, opener=None):
        assert url.endswith("/rate_limit"), f"unexpected GA.request call: {url}"
        if rate_limit_raises and bearer in rate_limit_raises:
            raise rate_limit_raises[bearer]
        return 200, {"resources": {"core": {"used": 1, "limit": 5000}}}

    monkeypatch.setattr(github_activity_capture.GA, "request", fake_request)
    monkeypatch.setattr(github_activity_capture.GACT, "capture_repository", fake_capture)
    monkeypatch.setattr(github_activity_capture.sys, "argv", ["github-activity-capture"])
    return minted


# --- token scope: one mint per installation, read-only, no `issues` ----------


def test_each_installation_mints_its_own_token_with_exactly_pull_requests_and_metadata_read(
        github_activity_capture, monkeypatch):
    """Two installations (`convivy`, `convivybuilder`) each get their own
    mint, each scoped to exactly `{pull_requests: read, metadata: read}` —
    red if a token were shared between the two installations, if the scope
    were wider than these two reads, or if `issues` appeared in it at all."""
    fake_capture = _FakeCapture()
    minted = _wire(
        github_activity_capture, monkeypatch,
        installations=[{"id": 111, "account": {"login": "convivy"}},
                      {"id": 222, "account": {"login": "convivybuilder"}}],
        repos_by_token={
            "token-for-111": [{"full_name": "convivy/agent-platform", "archived": False}],
            "token-for-222": [{"full_name": "convivybuilder/homebrew-tap", "archived": False}],
        },
        fake_capture=fake_capture,
    )

    rc = github_activity_capture.main()

    assert rc == 0
    assert {m[0] for m in minted} == {"111", "222"}, "both installations must mint their own token"
    for inst_id, permissions in minted:
        assert permissions == {"pull_requests": "read", "metadata": "read"}, (
            f"installation {inst_id}'s token must be scoped to exactly these two reads")
        assert "issues" not in permissions, "this job's tokens must carry no issues permission"

    tokens_by_repo = {repo: token for repo, cursor, token in fake_capture.calls}
    assert tokens_by_repo["convivy/agent-platform"] == "token-for-111"
    assert tokens_by_repo["convivybuilder/homebrew-tap"] == "token-for-222"
    assert tokens_by_repo["convivy/agent-platform"] != tokens_by_repo["convivybuilder/homebrew-tap"], (
        "each installation's repositories must be read under its OWN token, not a shared one")


def test_a_failed_mint_for_one_owner_still_reads_every_other_owner_and_exits_1(
        github_activity_capture, monkeypatch, capsys):
    """`ackme`'s mint fails; `convivy` is still read in full and its
    repository still captured, but the pass exits 1 overall. `ackme` is
    named to sort BEFORE `convivy` (`main()` walks installations sorted by
    login) — red against a version that returns on the first mint failure
    instead of continuing to the next owner, which this ordering is what
    catches: with the failing owner sorted LAST (as `convivybuilder` would
    be, after `convivy`), such a bug would still pass by accident."""
    fake_capture = _FakeCapture()
    import github_app as GA
    _wire(
        github_activity_capture, monkeypatch,
        installations=[{"id": 111, "account": {"login": "convivy"}},
                      {"id": 222, "account": {"login": "ackme"}}],
        repos_by_token={
            "token-for-111": [{"full_name": "convivy/agent-platform", "archived": False}],
        },
        fake_capture=fake_capture,
        mint_raises={"222": GA.Unreadable("GitHub refused an installation token (500)")},
    )

    rc = github_activity_capture.main()

    err = capsys.readouterr().err
    assert rc == 1
    assert "ackme" in err
    repos_captured = {repo for repo, cursor, token in fake_capture.calls}
    assert repos_captured == {"convivy/agent-platform"}, (
        "convivy must still be read even though ackme's mint failed earlier in the walk")


# --- repository filtering: archived, sandbox, private-project ----------------


@pytest.mark.rich_registry
def test_archived_sandbox_and_private_project_repos_are_never_requested(
        github_activity_capture, monkeypatch):
    """Of five repositories the installation reaches, only one is not
    excluded: an archived repo, the two named sandbox-tool repos, and a
    repository belonging to a BLACK BOX project (`ledger`, in the rich test
    registry) are all skipped before `capture_repository` is ever called."""
    fake_capture = _FakeCapture()
    repos = [
        {"full_name": "convivy/agent-platform", "archived": False},
        {"full_name": "convivy/archived-thing", "archived": True},
        {"full_name": "convivy/finch-ownerrez-sandbox-tools", "archived": False},
        {"full_name": "convivy/harbor-sevenshifts-sandbox-tools", "archived": False},
        {"full_name": "convivy/LEDGER", "archived": False},
    ]
    _wire(
        github_activity_capture, monkeypatch,
        installations=[{"id": 111, "account": {"login": "convivy"}}],
        repos_by_token={"token-for-111": repos},
        fake_capture=fake_capture,
    )

    rc = github_activity_capture.main()

    assert rc == 0
    requested = {repo for repo, cursor, token in fake_capture.calls}
    assert requested == {"convivy/agent-platform"}, (
        f"only the one non-excluded repository may be captured; got {requested}")


@pytest.mark.rich_registry
def test_sandbox_repos_matches_the_registrys_own_exclusion_list(github_activity_capture):
    """Pinned against the registry's own accessor, not a copy of the repo
    names — fails red if SANDBOX_REPOS drifts back to a hardcoded set."""
    import projects
    expected = {projects.repo_github_slug(r)
                for r in projects.activity_capture_excluded_repos()}
    assert github_activity_capture.SANDBOX_REPOS == expected


# --- repository filtering: unsafe basename (security review round 1, CQ·605) -


def test_a_repo_whose_basename_is_dot_or_dot_dot_is_refused_and_logged(
        github_activity_capture, monkeypatch, capsys):
    """`example-org/.` and `example-org/..` are refused before
    `capture_repository` ever runs, each with its own stderr line naming the
    refused `full_name`, and an ordinary repository in the same installation
    is still captured. Fails red against the unfixed code, which passed
    every `full_name` straight to `capture_repository` with no basename
    check at all — that version would capture all three and print neither
    stderr line."""
    fake_capture = _FakeCapture()
    repos = [
        {"full_name": "example-org/.", "archived": False},
        {"full_name": "example-org/..", "archived": False},
        {"full_name": "example-org/widget-repo", "archived": False},
    ]
    _wire(
        github_activity_capture, monkeypatch,
        installations=[{"id": 111, "account": {"login": "example-org"}}],
        repos_by_token={"token-for-111": repos},
        fake_capture=fake_capture,
    )

    rc = github_activity_capture.main()

    err = capsys.readouterr().err
    assert rc == 0
    requested = {repo for repo, cursor, token in fake_capture.calls}
    assert requested == {"example-org/widget-repo"}, (
        f"only the safe-basename repository may be captured; got {requested}")
    assert repr("example-org/.") in err, (
        "the refusal must name the exact full_name refused, not just the owner")
    assert repr("example-org/..") in err, (
        "the refusal must name the exact full_name refused, not just the owner")


# --- posting credential: no GitHub read and no shared-token fallback --------


def test_no_credential_exits_1_with_zero_github_requests(
        github_activity_capture, monkeypatch, tmp_path):
    """Neither `GITHUB_ACTIVITY_CAPTURE_TOKEN` nor the token file is set: the
    pass refuses before touching GitHub at all, and never falls back to the
    shared agent-platform token the way `pr-state-capture`/
    `ci-result-capture` do."""
    monkeypatch.delenv(SIGNAL_ENV, raising=False)
    monkeypatch.setattr(github_activity_capture, "SIGNAL_TOKEN_FILE", tmp_path / "missing")

    def must_not_be_called(*a, **k):
        raise AssertionError("no GitHub call may happen without a posting credential")

    monkeypatch.setattr(github_activity_capture.GA, "key_problem", must_not_be_called)
    monkeypatch.setattr(github_activity_capture.GA, "app_jwt", must_not_be_called)
    monkeypatch.setattr(github_activity_capture.GA, "installations", must_not_be_called)
    monkeypatch.setattr(github_activity_capture.GA, "installation_token", must_not_be_called)
    monkeypatch.setattr(github_activity_capture.GA, "request", must_not_be_called)
    monkeypatch.setattr(github_activity_capture, "list_installation_repos", must_not_be_called)
    monkeypatch.setattr(github_activity_capture.GACT, "capture_repository", must_not_be_called)
    monkeypatch.setattr(github_activity_capture.sys, "argv", ["github-activity-capture"])

    rc = github_activity_capture.main()

    assert rc == 1


# --- note (a), #910 round: a transport error costs only its own repository --


def test_a_transport_error_on_one_repo_does_not_abort_the_rest_of_the_pass(
        github_activity_capture, monkeypatch, capsys):
    """`convivy/flaky`'s read raises a raw `URLError` (the shape
    `github_app.request` does not catch — see the module docstring's "A
    TRANSPORT ERROR ... IS CAUGHT HERE" section): the pass still reads
    `convivy/ok` and saves ITS cursor, `convivy/flaky`'s own cursor is left
    exactly where it was, and the pass exits 1.

    Red against a version of `main()` with no per-repository `except
    OSError`: the uncaught `URLError` would propagate out of the whole loop,
    so `convivy/ok` would never be reached and `fake_capture.calls` would
    hold only `convivy/flaky`."""
    import urllib.error

    fake_capture = _FakeCapture(
        raise_by_repo={"convivy/flaky": urllib.error.URLError("connection reset")},
        result_by_repo={"convivy/ok": {**_CLEAN_RESULT, "new_cursor": "2026-10-15T01:00:00Z"}},
    )
    _wire(
        github_activity_capture, monkeypatch,
        installations=[{"id": 111, "account": {"login": "convivy"}}],
        repos_by_token={"token-for-111": [
            {"full_name": "convivy/flaky", "archived": False},
            {"full_name": "convivy/ok", "archived": False},
        ]},
        fake_capture=fake_capture,
    )
    github_activity_capture.GACT.save_cursor({"convivy/flaky": "2026-10-14T00:00:00Z"})

    rc = github_activity_capture.main()

    err = capsys.readouterr().err
    assert rc == 1
    assert "convivy/flaky" in err
    repos_captured = {repo for repo, cursor, token in fake_capture.calls}
    assert repos_captured == {"convivy/flaky", "convivy/ok"}, (
        "convivy/ok must still be read after convivy/flaky's transport error")

    cursor = github_activity_capture.GACT.load_cursor()
    assert cursor["convivy/flaky"] == "2026-10-14T00:00:00Z", (
        "a repository whose read raised a transport error must keep its old cursor")
    assert cursor["convivy/ok"] == "2026-10-15T01:00:00Z"


# --- transport errors at the other four GitHub calls main() makes (#916) ----


def test_installations_transport_error_exits_1_with_no_repository_captured(
        github_activity_capture, monkeypatch, capsys):
    """`GA.installations` itself raises a raw `ConnectionResetError` (the
    shape `github_app.request` does not catch): the pass has no installation
    list to work from at all, so it logs the transport error and returns 1
    before ever reaching the per-owner loop — `capture_repository` is never
    called.

    Red against a version of `main()` with no `except OSError` around
    `GA.installations`: the `ConnectionResetError` would propagate out of
    `main()` uncaught (see `test_no_subprocess_gh_cli_or_personal_credential_
    read_anywhere`'s sibling tests for why no such propagation may reach the
    launchd entry point)."""
    fake_capture = _FakeCapture()
    _wire(
        github_activity_capture, monkeypatch,
        installations=[{"id": 111, "account": {"login": "convivy"}}],
        repos_by_token={},
        fake_capture=fake_capture,
        installations_raises=ConnectionResetError("connection reset by peer"),
    )

    rc = github_activity_capture.main()

    err = capsys.readouterr().err
    assert rc == 1
    assert "ConnectionResetError" in err
    assert fake_capture.calls == [], "no repository may be captured without an installation list"


def test_installation_token_transport_error_skips_owner_continues_to_next(
        github_activity_capture, monkeypatch, capsys):
    """`ackme`'s token mint raises a raw `urllib.error.URLError`: `ackme` is
    skipped (logged, `exit_code = 1`) and `convivy` is still read and
    captured in full. `ackme` is named to sort BEFORE `convivy` so a version
    that aborts the whole pass on this owner's failure cannot pass by
    accident (mirrors `test_a_failed_mint_for_one_owner_still_reads_every_
    other_owner_and_exits_1`'s ordering reasoning, for the `GA.Unreadable`
    case this test's `OSError` sibling covers).

    Red against a version of `main()` with no `except OSError` around
    `GA.installation_token`: the `URLError` would propagate out of `main()`
    uncaught, so `convivy` would never be reached."""
    import urllib.error

    fake_capture = _FakeCapture()
    _wire(
        github_activity_capture, monkeypatch,
        installations=[{"id": 111, "account": {"login": "convivy"}},
                      {"id": 222, "account": {"login": "ackme"}}],
        repos_by_token={
            "token-for-111": [{"full_name": "convivy/agent-platform", "archived": False}],
        },
        fake_capture=fake_capture,
        mint_raises={"222": urllib.error.URLError("connection reset")},
    )

    rc = github_activity_capture.main()

    err = capsys.readouterr().err
    assert rc == 1
    assert "ackme" in err
    assert "URLError" in err
    repos_captured = {repo for repo, cursor, token in fake_capture.calls}
    assert repos_captured == {"convivy/agent-platform"}, (
        "convivy must still be read even though ackme's token mint raised a transport error")


def test_list_installation_repos_transport_error_skips_owner_continues_to_next(
        github_activity_capture, monkeypatch, capsys):
    """`ackme`'s repository listing raises a raw `ConnectionResetError`:
    `ackme` is skipped (logged, `exit_code = 1`) and `convivy` is still read
    and captured in full, `ackme` again sorting BEFORE `convivy` for the same
    reason as the token-mint case above.

    Red against a version of `main()` with no `except OSError` around
    `list_installation_repos`: the `ConnectionResetError` would propagate out
    of `main()` uncaught, so `convivy` would never be reached."""
    fake_capture = _FakeCapture()
    _wire(
        github_activity_capture, monkeypatch,
        installations=[{"id": 111, "account": {"login": "convivy"}},
                      {"id": 222, "account": {"login": "ackme"}}],
        repos_by_token={
            "token-for-111": [{"full_name": "convivy/agent-platform", "archived": False}],
        },
        fake_capture=fake_capture,
        list_repos_raises={"token-for-222": ConnectionResetError("connection reset by peer")},
    )

    rc = github_activity_capture.main()

    err = capsys.readouterr().err
    assert rc == 1
    assert "ackme" in err
    assert "ConnectionResetError" in err
    repos_captured = {repo for repo, cursor, token in fake_capture.calls}
    assert repos_captured == {"convivy/agent-platform"}, (
        "convivy must still be read even though ackme's repository listing raised a transport error")


def test_rate_limit_transport_error_does_not_block_that_owners_repositories_or_set_exit_code(
        github_activity_capture, monkeypatch, capsys):
    """The `GET /rate_limit` read raises a raw `TimeoutError`: the pass logs
    the existing "rate limit could not be read" line naming the error, and
    still reads and captures `convivy/agent-platform` — the read is
    diagnostic only (see the module docstring), so it must not set
    `exit_code` by itself, unlike the other three sites above.

    Red against a version of `main()` with no `except OSError` around the
    `GET /rate_limit` call: the `TimeoutError` would propagate out of
    `main()` uncaught, so `convivy/agent-platform` would never be reached."""
    fake_capture = _FakeCapture()
    _wire(
        github_activity_capture, monkeypatch,
        installations=[{"id": 111, "account": {"login": "convivy"}}],
        repos_by_token={
            "token-for-111": [{"full_name": "convivy/agent-platform", "archived": False}],
        },
        fake_capture=fake_capture,
        rate_limit_raises={"token-for-111": TimeoutError("timed out")},
    )

    rc = github_activity_capture.main()

    err = capsys.readouterr().err
    assert rc == 0, "a diagnostic-only rate-limit read failure must not fail the pass by itself"
    assert "rate limit could not be read" in err
    assert "TimeoutError" in err
    repos_captured = {repo for repo, cursor, token in fake_capture.calls}
    assert repos_captured == {"convivy/agent-platform"}, (
        "convivy/agent-platform must still be captured even though its owner's rate-limit read failed")


# --- a truncated GitHub body ends a repository's read, not the whole pass ---
# (issue_01M6D8HW0TRFRDPF1RG9JKV11Z)


def test_a_truncated_first_repository_keeps_its_cursor_and_still_reads_the_next(
        github_activity_capture, monkeypatch, capsys):
    """End to end, through the REAL `GACT.capture_repository` and the REAL
    `github_app.request` (neither is faked here, unlike every test above):
    `convivy/flaky`'s first page truncates mid-body
    (`http.client.IncompleteRead`, the shape GitHub's own occurrence takes —
    see the work item's account of the job's 48-traceback error log).
    `github_app.request` raises `ConnectionError` for it, which is an
    `OSError` `main()`'s per-repository `except OSError` catches:
    `convivy/flaky` keeps its old cursor and the pass still reads
    `convivy/ok` (sorted after it) and exits 1.

    Red against a version of `github_app.request` that lets
    `http.client.IncompleteRead` escape as a bare `HTTPException`: `except
    OSError` would miss it, the traceback would propagate out of `main()`
    uncaught, and `convivy/ok` would never be read — exactly the failure
    the work item describes."""
    import http.client

    _stub_app_auth(github_activity_capture, monkeypatch)
    monkeypatch.setattr(github_activity_capture.GA, "installations",
                        lambda jwt, **k: [{"id": 111, "account": {"login": "convivy"}}])
    monkeypatch.setattr(github_activity_capture.GA, "installation_token",
                        lambda jwt, inst_id, **k: "token-for-111")
    monkeypatch.setattr(
        github_activity_capture, "list_installation_repos",
        lambda token, **k: [{"full_name": "convivy/flaky", "archived": False},
                           {"full_name": "convivy/ok", "archived": False}])
    monkeypatch.setattr(github_activity_capture.sys, "argv", ["github-activity-capture"])

    class _JsonResp:
        def __init__(self, payload):
            self.status = 200
            self._body = github_activity_capture.json.dumps(payload).encode()

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _TruncatedResp:
        status = 200

        def read(self):
            raise http.client.IncompleteRead(b"partial", 500)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        if req.full_url.endswith("/rate_limit"):
            return _JsonResp({"resources": {"core": {"used": 1, "limit": 5000}}})
        if "convivy/flaky" in req.full_url:
            return _TruncatedResp()
        return _JsonResp([])

    monkeypatch.setattr(github_activity_capture.urllib.request, "urlopen", fake_urlopen)
    github_activity_capture.GACT.save_cursor({"convivy/flaky": "2026-10-14T00:00:00Z"})

    rc = github_activity_capture.main()

    err = capsys.readouterr().err
    assert rc == 1
    assert "convivy/flaky" in err
    assert "ConnectionError" in err

    cursor = github_activity_capture.GACT.load_cursor()
    assert cursor["convivy/flaky"] == "2026-10-14T00:00:00Z", (
        "the truncated repository must keep its old cursor")
    assert "convivy/ok" in cursor, (
        "convivy/ok must still be read and have its own cursor saved after "
        "convivy/flaky's truncated read")


# --- cursor saved per repository, not batched at the end of the pass --------


def test_cursor_is_saved_after_each_repository_not_batched_at_the_end(
        github_activity_capture, monkeypatch):
    """Every repository's cursor is written to disk as soon as it finishes —
    so a pass that stops partway (a deadline, a crash) still keeps every
    already-finished repository's cursor advance, rather than losing it to a
    single end-of-pass write that never ran.

    Red against a version that collects every repository's new cursor and
    calls `save_cursor` once after the loop: `save_calls` would hold a
    single entry with both repositories already in it, not one call per
    repository building up incrementally."""
    save_calls: list[dict] = []
    real_save_cursor = github_activity_capture.GACT.save_cursor

    def recording_save(cursor):
        save_calls.append(dict(cursor))
        real_save_cursor(cursor)

    monkeypatch.setattr(github_activity_capture.GACT, "save_cursor", recording_save)

    fake_capture = _FakeCapture(result_by_repo={
        "convivy/a": {**_CLEAN_RESULT, "new_cursor": "A"},
        "convivy/b": {**_CLEAN_RESULT, "new_cursor": "B"},
    })
    _wire(
        github_activity_capture, monkeypatch,
        installations=[{"id": 111, "account": {"login": "convivy"}}],
        repos_by_token={"token-for-111": [
            {"full_name": "convivy/a", "archived": False},
            {"full_name": "convivy/b", "archived": False},
        ]},
        fake_capture=fake_capture,
    )

    rc = github_activity_capture.main()

    assert rc == 0
    assert len(save_calls) == 2, "the cursor must be saved once per repository, not once for the pass"
    assert save_calls[0] == {"convivy/a": "A"}, (
        "convivy/a's cursor must already be on disk before convivy/b is even processed")
    assert save_calls[1] == {"convivy/a": "A", "convivy/b": "B"}


# --- static check: no subprocess, no `gh` CLI, no personal-token read -------


def test_no_subprocess_gh_cli_or_personal_credential_read_anywhere():
    """A static check over both files this job is built from. No
    `subprocess` (or any other process-exec primitive) appears anywhere,
    which rules out a `gh` CLI call entirely — there is no other way to
    invoke an external process from either file — and neither file reads
    `GH_TOKEN`, `GITHUB_TOKEN` or a `gh` config directory, the operator's
    personal GitHub credential surfaces platform/decisions/0051 forbids a
    new capture job from touching.

    Plant-checked by hand: adding `import subprocess` or a literal
    `os.environ["GITHUB_TOKEN"]` to either file turns this red."""
    text = ((REPO / "bin" / "github-activity-capture").read_text()
           + (REPO / "lib" / "agent-platform" / "github_activity.py").read_text())
    for marker in ("subprocess", "os.system(", "os.popen(", "os.exec"):
        assert marker not in text, f"{marker!r} would let this job shell out, including to `gh`"
    for marker in ("GH_TOKEN", "GITHUB_TOKEN", ".config/gh", "GH_CONFIG"):
        assert marker not in text, f"{marker!r} reads a personal GitHub credential surface"


# --- `--audit-window`: a read-only check (CQ·494, E25 PR 4) -----------------
#
# Synthetic owner/repo/identity names throughout, per this PR's own rule: no
# registered crew, product, organization or person name may appear here.

_WINDOW_START = "2026-10-20T00:00:00Z"
_WINDOW_END = "2026-10-21T00:00:00Z"
_IN_WINDOW = "2026-10-20T12:00:00Z"
_REPO = "example-owner/example-repo"
_REPO_BASENAME = "example-repo"  # CQ·605: a signal's own `product` is the bare repo name
_IDENTITY = "example-github-activity-capture-identity"
_OTHER_IDENTITY = "example-other-identity"


class _AuditResp:
    def __init__(self, status, payload):
        self.status = status
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeAuditLibrary:
    """Routes `GET /whoami` and `GET /signals` by URL, the way
    `urllib.request.urlopen` is called — `whoami` and `_read_ingested_urls`
    build real requests, so this is the fake Library those two functions
    actually exercise (every other call in these tests is monkeypatched at
    the function level, the same way `_wire` above does it). `rows` is the
    Library's full `GET /signals` corpus; `kind`/`product`/`since` are
    applied here the same way `_FakeSignalsLibrary` in test_agentctl.py
    applies them for a different route."""

    def __init__(self, *, actor, rows=None):
        self.actor = actor
        self.rows = list(rows or [])
        self.requests: list[str] = []

    def __call__(self, req, timeout=None):
        self.requests.append(req.full_url)
        parts = urllib.parse.urlsplit(req.full_url)
        if parts.path.endswith("/whoami"):
            return _AuditResp(200, {"actor": self.actor, "kind": "agent",
                                    "products": ["*"], "capabilities": []})
        query = urllib.parse.parse_qs(parts.query)
        kind = (query.get("kind") or [None])[0]
        product = (query.get("product") or [None])[0]
        since = (query.get("since") or [None])[0]
        rows = [r for r in self.rows
               if r["kind"] == kind and r["product"] == product
               and (since is None or (r.get("produced_at") or "") >= since)]
        return _AuditResp(200, {"signals": rows, "count": len(rows), "total": len(rows),
                                "offset": 0, "truncated": False})


class _FakePagedAuditLibrary:
    """A `GET /signals` fake that pages by the `offset` `_read_ingested_urls`
    sends, rather than `_FakeAuditLibrary`'s single always-whole-corpus
    answer above — built to exercise that function's own continuation loop.
    `rows_by_offset` maps the `offset` the code is expected to send to the
    page it gets back for that offset; `truncated_offsets` names which of
    those answers still carries `truncated: true`. An offset not in
    `rows_by_offset` answers with an empty, non-truncated page, so a wrong
    offset shows up as a miss rather than silently reusing a page."""

    def __init__(self, *, actor, rows_by_offset, truncated_offsets=()):
        self.actor = actor
        self.rows_by_offset = rows_by_offset
        self.truncated_offsets = set(truncated_offsets)
        self.requests: list[str] = []

    def __call__(self, req, timeout=None):
        self.requests.append(req.full_url)
        parts = urllib.parse.urlsplit(req.full_url)
        if parts.path.endswith("/whoami"):
            return _AuditResp(200, {"actor": self.actor, "kind": "agent",
                                    "products": ["*"], "capabilities": []})
        offset = int((urllib.parse.parse_qs(parts.query).get("offset") or ["0"])[0])
        rows = self.rows_by_offset.get(offset, [])
        return _AuditResp(200, {"signals": rows, "count": len(rows), "total": len(rows),
                                "offset": offset, "truncated": offset in self.truncated_offsets})


class _FakeAlwaysTruncatedLibrary:
    """Answers every `GET /signals` page with one (non-matching) row and
    `truncated: true`, forever — the Library-never-finishes case
    `_MAX_SIGNAL_PAGES` exists to bound in `_read_ingested_urls`."""

    def __init__(self, *, actor):
        self.actor = actor
        self.calls = 0

    def __call__(self, req, timeout=None):
        parts = urllib.parse.urlsplit(req.full_url)
        if parts.path.endswith("/whoami"):
            return _AuditResp(200, {"actor": self.actor, "kind": "agent",
                                    "products": ["*"], "capabilities": []})
        self.calls += 1
        row = _signal_row("changelog", _REPO_BASENAME, f"https://github.com/{_REPO}/pull/999",
                          self.actor)
        return _AuditResp(200, {"signals": [row], "count": 1, "total": 1,
                                "offset": 0, "truncated": True})


def _signal_row(kind, product, url, ingested_by, produced_at=_IN_WINDOW):
    return {"kind": kind, "product": product, "refs": {"url": url},
           "ingested_by": ingested_by, "produced_at": produced_at}


def _rc(pr_number, *, created_at=_IN_WINDOW, body="a review comment", comment_id=501):
    return {"id": comment_id,
           "pull_request_url": f"https://api.github.com/repos/{_REPO}/pulls/{pr_number}",
           "body": body, "user": {"login": "example-reviewer"},
           "html_url": f"https://github.com/{_REPO}/pull/{pr_number}#discussion_r{comment_id}",
           "created_at": created_at}


def _ic(pr_number, *, created_at=_IN_WINDOW, body="a conversation comment", comment_id=601):
    return {"id": comment_id,
           "issue_url": f"https://api.github.com/repos/{_REPO}/issues/{pr_number}",
           "body": body, "user": {"login": "example-commenter"},
           "html_url": f"https://github.com/{_REPO}/pull/{pr_number}#issuecomment-{comment_id}",
           "created_at": created_at}


def _pr(number, *, merged_at=_IN_WINDOW, title="a change", body="", updated_at=_IN_WINDOW):
    return {"number": number, "updated_at": updated_at, "merged_at": merged_at,
           "title": title, "body": body, "user": {"login": "example-author"},
           "html_url": f"https://github.com/{_REPO}/pull/{number}"}


def _review(review_id, *, pr_number=1, state="APPROVED", body="looks good",
           submitted_at=_IN_WINDOW):
    return {"id": review_id, "state": state, "body": body,
           "user": {"login": "example-reviewer"},
           "html_url": f"https://github.com/{_REPO}/pull/{pr_number}#pullrequestreview-{review_id}",
           "submitted_at": submitted_at}


def _must_not_post(*a, **k):
    raise AssertionError("the audit must never post a signal")


def _wire_audit(github_activity_capture, monkeypatch, *, repo=_REPO, extra_repos=()):
    """Installation/repository discovery, stubbed exactly as `_wire` above
    stubs it for `main()`, plus the one guard every audit test needs: a
    `post_signal` that fails the test if the audit ever calls it.
    `extra_repos` names additional `full_name`s the installation also
    reaches, alongside `repo` — used by the unsafe-basename test below to
    give the audit a second repository it must skip."""
    _stub_app_auth(github_activity_capture, monkeypatch)
    monkeypatch.setattr(github_activity_capture.GA, "installations",
                        lambda jwt, **k: [{"id": 111, "account": {"login": "example-owner"}}])
    monkeypatch.setattr(github_activity_capture.GA, "installation_token",
                        lambda jwt, inst_id, **k: "token-for-111")
    monkeypatch.setattr(
        github_activity_capture, "list_installation_repos",
        lambda token, **k: [{"full_name": repo, "archived": False}]
                          + [{"full_name": r, "archived": False} for r in extra_repos])
    monkeypatch.setattr(github_activity_capture.SC, "post_signal", _must_not_post)


def _wire_github_events(github_activity_capture, monkeypatch, *, review_comments=(),
                        conversation_comments=(), prs=(), reviews_by_pr=None):
    """`github_activity.py`'s own fetchers, monkeypatched to return canned
    GitHub JSON — the audit's fake GitHub. Reviews are keyed by PR number
    so `fetch_reviews` answers for the PR `_collect_window_events` asks
    about, the same shape `reviews_by_pr` already uses in
    `test_github_activity_capture.py`'s `_FakeGitHub`."""
    reviews_by_pr = reviews_by_pr or {}
    monkeypatch.setattr(github_activity_capture.GACT, "fetch_review_comments",
                        lambda r, since, token, **k: list(review_comments))
    monkeypatch.setattr(github_activity_capture.GACT, "fetch_conversation_comments",
                        lambda r, since, token, **k: list(conversation_comments))
    monkeypatch.setattr(github_activity_capture.GACT, "fetch_updated_pull_requests",
                        lambda r, since, token, **k: list(prs))
    monkeypatch.setattr(github_activity_capture.GACT, "fetch_reviews",
                        lambda r, number, token, **k: list(reviews_by_pr.get(number, [])))


def test_audit_window_matches_one_event_of_each_kind_and_exits_0(
        github_activity_capture, monkeypatch, capsys):
    """One review, one merge, one review comment and one conversation
    comment, each already ingested under this credential's own identity:
    every kind reports matched=1 missed=0, no miss line is printed, and the
    pass exits 0."""
    _wire_audit(github_activity_capture, monkeypatch)
    _wire_github_events(
        github_activity_capture, monkeypatch,
        review_comments=[_rc(1)], conversation_comments=[_ic(1)],
        prs=[_pr(1)], reviews_by_pr={1: [_review(901)]})
    rows = [
        _signal_row("review_comment", _REPO_BASENAME,
                   f"https://github.com/{_REPO}/pull/1#discussion_r501", _IDENTITY),
        _signal_row("pr_comment", _REPO_BASENAME,
                   f"https://github.com/{_REPO}/pull/1#issuecomment-601", _IDENTITY),
        _signal_row("changelog", _REPO_BASENAME, f"https://github.com/{_REPO}/pull/1", _IDENTITY),
        _signal_row("review_verdict", _REPO_BASENAME,
                   f"https://github.com/{_REPO}/pull/1#pullrequestreview-901", _IDENTITY),
    ]
    fake_library = _FakeAuditLibrary(actor=_IDENTITY, rows=rows)

    rc = github_activity_capture.run_audit(_WINDOW_START, _WINDOW_END, opener=fake_library)

    out = capsys.readouterr().out
    assert rc == 0
    assert "miss " not in out
    for kind in ("review_verdict", "changelog", "review_comment", "pr_comment"):
        assert f"{kind} matched=1 missed=0" in out


def test_audit_window_skips_a_repo_whose_basename_is_unsafe_and_reports_no_misses_for_it(
        github_activity_capture, monkeypatch, capsys):
    """`example-org/..`'s basename fails `GACT.is_safe_basename` the same way
    `main()`'s posting loop already refuses it (round 1, CQ·605): the audit
    must skip it too, before `_collect_window_events`/`_classify_window_events`
    ever run for it, rather than reading its real merge event back and
    reporting it a miss because `main()` never posted a signal for it in the
    first place (round 2 finding, CQ·605). `example-owner/example-repo`
    stays wired as usual and still matches.

    Red against the pre-round-2 code: `run_audit()`'s loop had no basename
    skip, so it called `_collect_window_events`/`_classify_window_events`
    for `example-org/..` too, queried the Library with
    `product=GACT._repo_basename("example-org/..") == ".."`, found no row
    (the only stored row's product is `example-repo`), and reported that
    repo's one merge event a miss — `"changelog matched=1 missed=1"` and a
    `"miss "` line, not the assertions below."""
    _wire_audit(github_activity_capture, monkeypatch, extra_repos=("example-org/..",))
    _wire_github_events(github_activity_capture, monkeypatch, prs=[_pr(1)])
    rows = [_signal_row("changelog", _REPO_BASENAME, f"https://github.com/{_REPO}/pull/1",
                        _IDENTITY)]
    fake_library = _FakeAuditLibrary(actor=_IDENTITY, rows=rows)

    rc = github_activity_capture.run_audit(_WINDOW_START, _WINDOW_END, opener=fake_library)

    out, err = capsys.readouterr()
    assert rc == 0
    assert "miss " not in out
    assert "changelog matched=1 missed=0" in out
    assert repr("example-org/..") in err, (
        "the refusal must name the exact full_name refused")


def test_audit_window_follows_truncated_continuation_to_a_second_page(
        github_activity_capture, monkeypatch, capsys):
    """`_read_ingested_urls`'s own page loop: a `truncated: true` first page
    must not be read as the whole corpus. Page 1 holds two rows that do not
    match this merge's URL; the matching row is on page 2 only. The audit
    reports the merge matched only if it followed the continuation into
    page 2, and the second `/signals` request must carry the `offset`
    `_read_ingested_urls` computes — the row count page 1 returned — not a
    hardcoded or missing one."""
    _wire_audit(github_activity_capture, monkeypatch)
    _wire_github_events(github_activity_capture, monkeypatch, prs=[_pr(1)])
    page_1 = [
        _signal_row("changelog", _REPO_BASENAME, f"https://github.com/{_REPO}/pull/901", _IDENTITY),
        _signal_row("changelog", _REPO_BASENAME, f"https://github.com/{_REPO}/pull/902", _IDENTITY),
    ]
    page_2 = [_signal_row("changelog", _REPO_BASENAME, f"https://github.com/{_REPO}/pull/1",
                          _IDENTITY)]
    fake_library = _FakePagedAuditLibrary(
        actor=_IDENTITY, rows_by_offset={0: page_1, len(page_1): page_2},
        truncated_offsets={0})

    rc = github_activity_capture.run_audit(_WINDOW_START, _WINDOW_END, opener=fake_library)

    out = capsys.readouterr().out
    assert rc == 0
    assert "changelog matched=1 missed=0" in out
    signal_requests = [u for u in fake_library.requests if "/signals" in u]
    assert len(signal_requests) == 2, "page 2 was never requested"
    second_offset = urllib.parse.parse_qs(
        urllib.parse.urlsplit(signal_requests[1]).query)["offset"][0]
    assert second_offset == str(len(page_1))


def test_audit_window_refuses_when_signals_stay_truncated_past_the_page_limit(
        github_activity_capture, monkeypatch, capsys):
    """A Library that never stops answering `truncated: true` must not be
    read forever: `_read_ingested_urls` refuses, naming `_MAX_SIGNAL_PAGES`,
    rather than treating a partial read as the whole corpus — the guard the
    page-2 test above proves is reachable. The pass exits 1, and the
    event's own kind must not be reported as a clean match."""
    _wire_audit(github_activity_capture, monkeypatch)
    _wire_github_events(github_activity_capture, monkeypatch, prs=[_pr(1)])
    fake_library = _FakeAlwaysTruncatedLibrary(actor=_IDENTITY)

    rc = github_activity_capture.run_audit(_WINDOW_START, _WINDOW_END, opener=fake_library)

    out, err = capsys.readouterr()
    limit = github_activity_capture._MAX_SIGNAL_PAGES
    assert rc == 1
    assert f"still truncated after {limit} pages" in err
    assert "changelog matched=1 missed=0" not in out
    assert fake_library.calls == limit


def test_audit_window_reports_a_row_ingested_by_another_identity_as_a_miss(
        github_activity_capture, monkeypatch, capsys):
    """A merge whose Library row carries a DIFFERENT credential's own
    identity (a still-running legacy writer's, during the overlap window
    the audit exists to check) is reported as a miss naming that identity,
    and the pass exits 1 — `POST /signals` sets `ingested_by` server-side,
    so a matching kind/product/url with the wrong `ingested_by` is exactly
    the shape such a writer leaves behind."""
    _wire_audit(github_activity_capture, monkeypatch)
    _wire_github_events(github_activity_capture, monkeypatch, prs=[_pr(1)])
    rows = [_signal_row("changelog", _REPO_BASENAME, f"https://github.com/{_REPO}/pull/1",
                        _OTHER_IDENTITY)]
    fake_library = _FakeAuditLibrary(actor=_IDENTITY, rows=rows)

    rc = github_activity_capture.run_audit(_WINDOW_START, _WINDOW_END, opener=fake_library)

    out = capsys.readouterr().out
    assert rc == 1
    assert "miss kind=changelog" in out
    assert _OTHER_IDENTITY in out
    assert "changelog matched=0 missed=1" in out


def test_audit_window_reports_no_row_at_all_as_a_miss(
        github_activity_capture, monkeypatch, capsys):
    _wire_audit(github_activity_capture, monkeypatch)
    _wire_github_events(github_activity_capture, monkeypatch, prs=[_pr(1)])
    fake_library = _FakeAuditLibrary(actor=_IDENTITY, rows=[])

    rc = github_activity_capture.run_audit(_WINDOW_START, _WINDOW_END, opener=fake_library)

    out = capsys.readouterr().out
    assert rc == 1
    assert "miss kind=changelog" in out and "reason=no row" in out
    assert "changelog matched=0 missed=1" in out


def test_audit_window_reports_an_empty_body_commented_review_as_a_skip_miss(
        github_activity_capture, monkeypatch, capsys):
    """An empty-body COMMENTED review is the same shell `signal_from_review`
    itself skips in the ordinary pass (`github_activity.py`'s own
    docstring) — classified here by that SAME transform, not a
    re-implementation, and reported as a miss with its own reason rather
    than "no row". No Library call is made for `review_verdict` at all:
    there was never a signal this event could have landed as."""
    _wire_audit(github_activity_capture, monkeypatch)
    empty_review = _review(902, state="COMMENTED", body="")
    _wire_github_events(github_activity_capture, monkeypatch,
                        prs=[_pr(1, merged_at=None)], reviews_by_pr={1: [empty_review]})
    fake_library = _FakeAuditLibrary(actor=_IDENTITY, rows=[])

    rc = github_activity_capture.run_audit(_WINDOW_START, _WINDOW_END, opener=fake_library)

    out = capsys.readouterr().out
    assert rc == 1
    assert "skipped by the empty-body rule" in out
    assert "review_verdict matched=0 missed=1" in out
    assert not any("kind=review_verdict" in u for u in fake_library.requests), (
        "a skipped event must never trigger a Library read for its kind")


def test_audit_window_never_posts_a_signal(github_activity_capture, monkeypatch, capsys):
    """DoD: `--audit-window` must never call the Library's post route. A
    `post_signal` that raises if called (set up by `_wire_audit`) must
    survive a full matched-and-missed pass without firing."""
    _wire_audit(github_activity_capture, monkeypatch)
    _wire_github_events(github_activity_capture, monkeypatch, prs=[_pr(1)])
    fake_library = _FakeAuditLibrary(actor=_IDENTITY, rows=[])

    rc = github_activity_capture.run_audit(_WINDOW_START, _WINDOW_END, opener=fake_library)

    assert rc == 1  # the miss from the empty Library above; posting still never happens


def test_audit_window_without_credential_exits_1_before_any_call(
        github_activity_capture, monkeypatch, tmp_path):
    """Same refusal path as `main()` (`_require_signal_token`, shared code):
    no credential, no GitHub App call, no `whoami`, nothing."""
    monkeypatch.delenv(SIGNAL_ENV, raising=False)
    monkeypatch.setattr(github_activity_capture, "SIGNAL_TOKEN_FILE", tmp_path / "missing")

    def must_not_be_called(*a, **k):
        raise AssertionError("no call may happen without a posting credential")

    monkeypatch.setattr(github_activity_capture.GA, "key_problem", must_not_be_called)
    monkeypatch.setattr(github_activity_capture.GA, "app_jwt", must_not_be_called)
    monkeypatch.setattr(github_activity_capture.GA, "installations", must_not_be_called)
    monkeypatch.setattr(github_activity_capture, "whoami", must_not_be_called)

    rc = github_activity_capture.run_audit(_WINDOW_START, _WINDOW_END)

    assert rc == 1


@pytest.mark.parametrize("end", ["2026-10-20T00:00:00Z", "2026-10-19T00:00:00Z"])
def test_audit_window_refuses_an_end_not_after_start(github_activity_capture, end, capsys):
    rc = github_activity_capture.run_audit(_WINDOW_START, end)
    assert rc == 1
    assert "must be after" in capsys.readouterr().err


def test_audit_window_refuses_a_window_longer_than_seven_days(github_activity_capture, capsys):
    rc = github_activity_capture.run_audit("2026-10-01T00:00:00Z", "2026-10-09T00:00:01Z")
    assert rc == 1
    assert "exceeds" in capsys.readouterr().err


def test_audit_window_refuses_an_unzoned_timestamp(github_activity_capture, capsys):
    rc = github_activity_capture.run_audit("2026-10-20T00:00:00", _WINDOW_END)
    assert rc == 1
    assert "no UTC zone" in capsys.readouterr().err


def test_audit_window_identity_read_failure_refuses_rather_than_a_blind_pass(
        github_activity_capture, monkeypatch, capsys):
    """`GET /whoami` unreachable must refuse (exit 1) rather than proceed
    with an unknown identity that could make every real miss read as a
    match — see `whoami`'s own docstring."""
    _wire_audit(github_activity_capture, monkeypatch)

    def raising_opener(req, timeout=None):
        raise ConnectionRefusedError("refused")

    rc = github_activity_capture.run_audit(_WINDOW_START, _WINDOW_END, opener=raising_opener)

    assert rc == 1
    assert "GET /whoami failed" in capsys.readouterr().err
