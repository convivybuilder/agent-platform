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
"""
from __future__ import annotations

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


def test_archived_sandbox_and_private_project_repos_are_never_requested(
        github_activity_capture, monkeypatch):
    """Of five repositories the installation reaches, only one is not
    excluded: an archived repo, the two named sandbox-tool repos, and a
    repository belonging to a BLACK BOX project (`cfo`, in the real
    `config/projects.json`) are all skipped before `capture_repository` is
    ever called."""
    fake_capture = _FakeCapture()
    repos = [
        {"full_name": "convivy/agent-platform", "archived": False},
        {"full_name": "convivy/archived-thing", "archived": True},
        {"full_name": "convivy/vivy-ownerrez-sandbox-tools", "archived": False},
        {"full_name": "convivy/constance-sevenshifts-sandbox-tools", "archived": False},
        {"full_name": "convivy/CFO", "archived": False},
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
