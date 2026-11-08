"""Tests for `agentctl review-ci` (platform/specs/reviewers-run-under-their-allow-list):
the one CI read a review-class dispatch's allow-list grants, in place of raw `gh api`.

NO TEST HERE CALLS GITHUB. `agentctl._gh_api_json` — the one function that would shell
out to `gh` — is always stubbed; a positive-control test confirms the stub is actually
consulted, not bypassed.
"""
from __future__ import annotations

import os
import subprocess
import sys
import types
from pathlib import Path

_AGENTCTL_BIN = Path(__file__).resolve().parent.parent / "bin" / "agentctl"


def _run(agentctl, monkeypatch, *, env=None, api=None):
    """Run `cmd_review_ci` with `env` as the run's environment (replacing
    AGENTCTL_REPO/AGENTCTL_REVIEW_SHA/GH_TOKEN wholesale, never merging onto
    the real process's own) and `api` standing in for `_gh_api_json` — a
    callable `path -> dict | None`, or `None` to make every call a bug
    (nothing here should reach it when the env vars are absent). `_gh_api_json`
    now also takes a minted `env=` kwarg (platform/decisions/0051) that this
    suite's autouse `_default_pr_gh_token` fixture fills with a plain
    environment copy, never `None` — every stand-in here accepts (and
    ignores) it, the same way the real function's own callers do when they
    have none of their own to pass.

    `GH_TOKEN` is cleared the same way the other two are: this process's own
    (the operator's interactive session carries one) must never leak into a
    test of the mint path below — `cmd_review_ci` now reads with an inherited
    `GH_TOKEN` rather than minting one (regression 2, a dispatched run's own
    sandbox denies the mint)."""
    monkeypatch.delenv("AGENTCTL_REPO", raising=False)
    monkeypatch.delenv("AGENTCTL_REVIEW_SHA", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)

    def _refuse(path, env=None):
        raise AssertionError(f"_gh_api_json called with {path!r} when it must not be")

    monkeypatch.setattr(agentctl, "_gh_api_json", api or _refuse)
    return agentctl.cmd_review_ci(types.SimpleNamespace())


def test_review_ci_takes_no_arguments():
    """The grant is `Bash(agentctl review-ci)`, an exact command with no flag
    space — the subparser must accept none. Run as a real subprocess (no CLI
    parser object is exposed to import) so this is the same argv parsing a
    reviewer's Bash call would hit."""
    env = {k: v for k, v in os.environ.items()
          if k not in ("AGENTCTL_REPO", "AGENTCTL_REVIEW_SHA")}
    refused = subprocess.run(
        [sys.executable, str(_AGENTCTL_BIN), "review-ci", "--pr", "1"],
        capture_output=True, text=True, timeout=30, env=env)
    assert refused.returncode != 0
    assert "unrecognized arguments" in refused.stderr

    accepted = subprocess.run(
        [sys.executable, str(_AGENTCTL_BIN), "review-ci"],
        capture_output=True, text=True, timeout=30, env=env)
    # Neither env var is set in THIS subprocess's own env — refuses for that
    # reason (exit 1), never an argparse error (exit 2), confirming the bare
    # form is accepted at the parser layer.
    assert accepted.returncode == 1
    assert "unrecognized arguments" not in accepted.stderr


def test_review_ci_refuses_with_neither_env_var_set_and_calls_no_api(agentctl, monkeypatch):
    code = _run(agentctl, monkeypatch, env={})
    assert code == 1


def test_review_ci_refuses_with_only_repo_set(agentctl, monkeypatch):
    code = _run(agentctl, monkeypatch, env={"AGENTCTL_REPO": "agent-platform"})
    assert code == 1


def test_review_ci_refuses_with_only_sha_set(agentctl, monkeypatch):
    code = _run(agentctl, monkeypatch, env={"AGENTCTL_REVIEW_SHA": "a" * 40})
    assert code == 1


def test_review_ci_reads_only_the_two_env_vars_never_argv(agentctl, monkeypatch, capsys):
    """Positive control: with both vars set, the stub IS consulted — proving the
    refusal tests above are refusing on a real check, not a stub that is simply
    never reached."""
    calls = []

    def api(path, env=None):
        calls.append(path)
        return {"check_runs": []}

    code = _run(agentctl, monkeypatch,
               env={"AGENTCTL_REPO": "agent-platform", "AGENTCTL_REVIEW_SHA": "a" * 40},
               api=api)
    assert code == 0
    assert calls == [f"repos/convivy/agent-platform/commits/{'a' * 40}/check-runs"]


def test_review_ci_issues_only_a_get_never_gh_api_directly(agentctl, monkeypatch):
    """The one GitHub call goes through `_gh_api_json` (a GET-only `gh api <path>`
    wrapper), never a `subprocess.run` this test didn't stub — if `cmd_review_ci`
    ever grew a second, unstubbed call site, this would catch it."""
    def boom(*a, **k):
        raise AssertionError("cmd_review_ci called subprocess.run directly")

    monkeypatch.setattr(agentctl.subprocess, "run", boom)
    code = _run(agentctl, monkeypatch,
               env={"AGENTCTL_REPO": "agent-platform", "AGENTCTL_REVIEW_SHA": "a" * 40},
               api=lambda path, env=None: {"check_runs": []})
    assert code == 0


def test_review_ci_prints_name_status_conclusion_and_run_id(agentctl, monkeypatch, capsys):
    payload = {"check_runs": [
        {"name": "pytest", "status": "completed", "conclusion": "success",
         "details_url": "https://github.com/convivy/agent-platform/actions/runs/123/job/456"},
        {"name": "lint", "status": "in_progress", "conclusion": None,
         "details_url": None},
    ]}
    code = _run(agentctl, monkeypatch,
               env={"AGENTCTL_REPO": "agent-platform", "AGENTCTL_REVIEW_SHA": "a" * 40},
               api=lambda path, env=None: payload)
    out = capsys.readouterr().out
    assert code == 0
    assert "pytest" in out and "status=completed" in out and "conclusion=success" in out
    assert "run_id=123" in out
    assert "lint" in out and "status=in_progress" in out and "run_id=?" in out


def test_review_ci_refuses_when_the_api_call_fails(agentctl, monkeypatch):
    code = _run(agentctl, monkeypatch,
               env={"AGENTCTL_REPO": "agent-platform", "AGENTCTL_REVIEW_SHA": "a" * 40},
               api=lambda path, env=None: None)
    assert code == 1


def test_review_ci_resolves_the_repo_to_its_github_slug(agentctl, monkeypatch):
    """`AGENTCTL_REPO` carries the bare short name (`args.on`'s own value, e.g.
    `"widget"`), never the owner/repo slug — `cmd_review_ci` must resolve it the
    same way `tool_policy.repo_slug` already does, not pass it through raw."""
    calls = []

    def api(path, env=None):
        calls.append(path)
        return {"check_runs": []}

    code = _run(agentctl, monkeypatch, env={"AGENTCTL_REPO": "widget", "AGENTCTL_REVIEW_SHA": "b" * 40}, api=api)
    assert code == 0
    assert calls == [f"repos/convivy/widget/commits/{'b' * 40}/check-runs"]


# --- platform/decisions/0051: this read mints its own `read` token --------------
#
# `cmd_review_ci` now reaches `_gh_api_json` through `_review_ci_runs_minted`,
# which mints a `read` token for the resolved slug (`_gh_run_env`) rather than
# running `gh` under the operator's ambient login. The suite's autouse
# `_default_pr_gh_token` fixture stubs `_gh_run_env` for every OTHER test in
# this file (a plain copy of the environment, no network); these two restore
# the real function and stub only `github_app`'s network-facing primitives, the
# same pattern `tests/test_agentctl.py`'s `_s3_stub_gh_app_mint` uses.

def _stub_gh_app_mint(monkeypatch, agentctl, tmp_path, *, token="ghs_review_ci_token"):
    monkeypatch.setattr(agentctl, "_gh_run_env", agentctl._real_gh_run_env)
    monkeypatch.setattr(agentctl.GA, "mint_log_path", lambda: tmp_path / "mint-log.jsonl")
    monkeypatch.setattr(
        agentctl.GA, "installed_app",
        lambda: agentctl.GA.InstalledApp(app_id="1234567", key_path=Path("/dev/null"),
                                         slug="test-app"))
    monkeypatch.setattr(agentctl.GA, "key_problem", lambda path, label=None: None)
    monkeypatch.setattr(agentctl.GA, "app_jwt", lambda path, app_id: "jwt.x.y")
    monkeypatch.setattr(agentctl.GA, "installation_for_owner",
                        lambda jwt, owner, *, label=None, opener=None:
                            {"id": 1, "account": {"login": owner}})

    def _token_payload(jwt, installation_id, *, repositories=None, permissions=None, opener=None):
        return {"token": token, "permissions": permissions,
                "repository_selection": "selected", "expires_at": "2026-10-17T12:00:00Z"}
    monkeypatch.setattr(agentctl.GA, "installation_token_payload", _token_payload)
    monkeypatch.setattr(agentctl.GA, "revoke_installation_token", lambda tok, opener=None: True)


def test_review_ci_reads_through_a_minted_read_token(agentctl, monkeypatch, tmp_path):
    """Fails without `_review_ci_runs_minted`: with the real mint restored, the
    `env` `_gh_api_json` receives must carry the minted `GH_TOKEN`, not the bare
    `os.environ` a stale `_gh_run_env` stub would yield."""
    _stub_gh_app_mint(monkeypatch, agentctl, tmp_path)
    seen_envs = []

    def api(path, env=None):
        seen_envs.append(env)
        return {"check_runs": []}

    code = _run(agentctl, monkeypatch,
               env={"AGENTCTL_REPO": "agent-platform", "AGENTCTL_REVIEW_SHA": "a" * 40},
               api=api)
    assert code == 0
    assert seen_envs and seen_envs[0] is not None
    assert seen_envs[0]["GH_TOKEN"] == "ghs_review_ci_token"
    assert seen_envs[0]["GH_PROMPT_DISABLED"] == "1"


def test_review_ci_failed_mint_surfaces_a_named_error_and_calls_no_api(
        agentctl, monkeypatch, tmp_path, capsys):
    """A mint failure refuses by name and never reaches `_gh_api_json` at all —
    no fallback to the operator's ambient `gh` login."""
    _stub_gh_app_mint(monkeypatch, agentctl, tmp_path)

    def _boom(jwt, owner, *, label=None, opener=None):
        raise agentctl.GA.Unreadable("the installation could not be read (503)")
    monkeypatch.setattr(agentctl.GA, "installation_for_owner", _boom)

    code = _run(agentctl, monkeypatch,
               env={"AGENTCTL_REPO": "agent-platform", "AGENTCTL_REVIEW_SHA": "a" * 40},
               api=None)
    out = capsys.readouterr()

    assert code == 1
    assert "cannot mint a GitHub token" in out.err


# --- regression 2: a dispatched run's sandbox denies the mint, but already --
# holds its own GH_TOKEN -------------------------------------------------------
#
# `GA.installed_app()`'s key lives under the real account home, on
# `run_sandbox.DENIED_HOME_FILES` — denied to every dispatched run outright.
# Minting here always failed inside one with "no App id at .../github-app/
# app-id ([Errno 1] Operation not permitted" (the occurrence: run-6d293dbd,
# 2026-11-06). A dispatched run already holds its own `read` token in
# `GH_TOKEN` (and a private `GH_CONFIG_DIR`), set by `dispatch_env.
# git_credential_guard_env` before this process ever starts — these three
# confirm `cmd_review_ci` reads with that inherited environment instead of
# minting a second token, mints as before when there is none, and never
# falls back to a mint when the GH_TOKEN-present read itself fails.

def test_review_ci_with_gh_token_set_reads_with_that_env_and_mints_nothing(
        agentctl, monkeypatch):
    """Confirmed against the unfixed code: `_review_ci_runs_minted` was called
    regardless of `GH_TOKEN`, so the mint-call assertion below failed."""
    mint_calls = []
    monkeypatch.setattr(
        agentctl, "_review_ci_runs_minted",
        lambda repo, sha: (mint_calls.append((repo, sha)),
                          (None, None, "must not mint"))[1])
    seen_envs = []

    def api(path, env=None):
        seen_envs.append(env)
        return {"check_runs": []}

    code = _run(agentctl, monkeypatch,
               env={"AGENTCTL_REPO": "agent-platform", "AGENTCTL_REVIEW_SHA": "a" * 40,
                    "GH_TOKEN": "ghs_dispatched_runs_own_token"},
               api=api)
    assert code == 0
    assert not mint_calls, "cmd_review_ci minted despite GH_TOKEN already being set"
    assert seen_envs and seen_envs[0] is None, (
        "cmd_review_ci must read with the inherited ambient environment, not "
        "build a second one")


def test_review_ci_with_gh_token_set_never_touches_the_app_at_all(
        agentctl, monkeypatch, tmp_path):
    """With the REAL mint restored and the App entirely unreadable —
    `GA.installed_app()` raising the exact `Unreadable` the sandboxed
    occurrence reported — the GH_TOKEN-present path must still succeed:
    it never calls into `GA` at all.

    Confirmed against the unfixed code: the mint always ran regardless of
    `GH_TOKEN`, hit the same `GA.installed_app` failure, and the assertion
    below failed with code 1."""
    monkeypatch.setattr(agentctl, "_gh_run_env", agentctl._real_gh_run_env)

    def _boom():
        raise agentctl.GA.Unreadable(
            "no App id at .../github-app/app-id ([Errno 1] Operation not permitted)")
    monkeypatch.setattr(agentctl.GA, "installed_app", _boom)

    code = _run(agentctl, monkeypatch,
               env={"AGENTCTL_REPO": "agent-platform", "AGENTCTL_REVIEW_SHA": "a" * 40,
                    "GH_TOKEN": "ghs_dispatched_runs_own_token"},
               api=lambda path, env=None: {"check_runs": []})
    assert code == 0


def test_review_ci_without_gh_token_still_mints_as_before(agentctl, monkeypatch):
    """No `GH_TOKEN` (an operator's own interactive session, which carries no
    dispatched-run sandbox) keeps minting through `_review_ci_runs_minted`,
    exactly as before this regression's fix."""
    mint_calls = []

    def fake_minted(repo, sha):
        mint_calls.append((repo, sha))
        return "convivy/agent-platform", [], None
    monkeypatch.setattr(agentctl, "_review_ci_runs_minted", fake_minted)

    code = _run(agentctl, monkeypatch,
               env={"AGENTCTL_REPO": "agent-platform", "AGENTCTL_REVIEW_SHA": "a" * 40},
               api=None)
    assert code == 0
    assert mint_calls == [("agent-platform", "a" * 40)]
