"""`_repo_has_ci_workflows` (bin/agentctl) — the empty-rollup-twin helper
`review-status` asks whether a repo has PR CI at all.

The registry's own `ci_workflows` table, read via `projects.ci_workflows_entry_for_slug`
against the `owner/repo` slug `review-status --repo` passes, answers True outright when
it names a NON-EMPTY list. An EMPTY declared list does NOT answer by itself — that field
is written once at crew registration and goes stale (CQ·569 found five registered repos
declaring `[]` that had since grown real CI) — so `[]` still makes a live GitHub call,
filtered to exclude only GitHub's own `pages-build-deployment` (event `dynamic`, never a
pull request) while keeping every other `dynamic/` workflow (CodeQL's default setup, for
one, which DOES run on pull requests). Only when no row names the repo at all does this
fall back to the unchanged, unfiltered live count.
"""
from __future__ import annotations

import json
import subprocess

import pytest

_PAGES_DEPLOYMENT_PATH = "dynamic/pages/pages-build-deployment"


@pytest.fixture
def write_registry(agentctl, tmp_path, monkeypatch):
    """Points the registry loader at a file this test writes, driving
    `_repo_has_ci_workflows` through the REAL `projects.ci_workflows_entry_for_slug`
    lookup rather than a stub of it (or of `ci_workflows_entry`) — the same technique
    `test_projects.py`'s `temp_registry`/`crafted_registry` fixtures use."""
    def _write(units: dict) -> None:
        path = tmp_path / "projects.json"
        path.write_text(json.dumps(units))
        monkeypatch.setattr(agentctl.projects, "PROJECTS_JSON", path)
        agentctl.projects._load_raw.cache_clear()
    yield _write
    agentctl.projects._load_raw.cache_clear()


def _unit(repos, repo_owners=None, ci_workflows=None, aliases=()):
    unit = {"repos": list(repos), "aliases": list(aliases)}
    if repo_owners is not None:
        unit["repo_owners"] = repo_owners
    if ci_workflows is not None:
        unit["ci_workflows"] = ci_workflows
    return unit


def _refuse_github_calls(monkeypatch, agentctl):
    """Fails the test the moment anything calls `gh`, for the cases where the
    registry alone must answer."""
    def _refuse(cmd, **kwargs):
        raise AssertionError(f"must not call GitHub when the registry already "
                              f"answers: {cmd!r}")
    monkeypatch.setattr(agentctl.subprocess, "run", _refuse)


def _fake_gh_workflow_count(count: int):
    """The unfiltered-count `gh api ... | length` shape the no-row live
    fallback has always used, unchanged by this round."""
    def _run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=f"{count}\n", stderr="")
    return _run


def _fake_gh_workflow_paths(paths):
    """The path-listing shape the `[]`-row live fallback reads, so it can
    filter out `pages-build-deployment` by exact path."""
    def _run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(paths), stderr="")
    return _run


# --- the registry answers a NON-EMPTY declaration; GitHub is never asked ----

def test_registry_non_empty_list_reports_pr_ci_without_a_github_call(
        agentctl, write_registry, monkeypatch):
    write_registry({
        "synthetic-crew": _unit(
            ["synthetic-repo"], repo_owners={"synthetic-repo": "synthetic-owner"},
            ci_workflows={"synthetic-repo": ["lint.yml"]}),
    })
    _refuse_github_calls(monkeypatch, agentctl)
    assert agentctl._repo_has_ci_workflows("synthetic-owner/synthetic-repo") is True


# --- the registry says nothing; the live fallback is unchanged ---------------

def test_a_repo_named_in_no_row_still_reads_the_live_workflow_count(agentctl, monkeypatch):
    monkeypatch.setattr(agentctl.projects, "ci_workflows_entry_for_slug", lambda repo: None)
    monkeypatch.setattr(agentctl.subprocess, "run", _fake_gh_workflow_count(2))
    assert agentctl._repo_has_ci_workflows("synthetic-org/unregistered-repo") is True


def test_a_repo_named_in_no_row_and_with_no_live_workflows_reports_no_pr_ci(
        agentctl, monkeypatch):
    monkeypatch.setattr(agentctl.projects, "ci_workflows_entry_for_slug", lambda repo: None)
    monkeypatch.setattr(agentctl.subprocess, "run", _fake_gh_workflow_count(0))
    assert agentctl._repo_has_ci_workflows("synthetic-org/unregistered-repo") is False


# --- the slug must resolve through the real registry lookup, owner included -

def test_owner_qualified_slug_resolves_through_the_real_registry_to_its_empty_row(
        agentctl, write_registry, monkeypatch):
    """THE BUG this round fixes. `review-status --repo` passes a full
    `owner/repo` slug; a registry row's `repos`/`aliases` hold only bare
    names. This proves the lookup now matches on the resolved slug rather
    than never matching at all.

    Fails against the current head 2117184be: there, `_repo_has_ci_workflows`
    hands the whole slug to `ci_workflows_entry`, which only ever matches a
    bare registry name, so this row's `[]` is never reached — the live
    `pages-build-deployment` workflow is the only one reported, and round 1's
    unfiltered live branch reads that as PR CI (True), not False.
    """
    write_registry({
        "synthetic-crew": _unit(
            ["synthetic-repo"], repo_owners={"synthetic-repo": "synthetic-owner"},
            ci_workflows={"synthetic-repo": []}),
    })
    monkeypatch.setattr(agentctl.subprocess, "run",
                         _fake_gh_workflow_paths([_PAGES_DEPLOYMENT_PATH]))
    assert agentctl._repo_has_ci_workflows("synthetic-owner/synthetic-repo") is False


def test_slug_naming_a_different_owner_does_not_match_the_row(
        agentctl, write_registry, monkeypatch):
    """Same bare repo name, registered under `synthetic-owner` with a
    NON-EMPTY declaration — if the lookup matched on the bare name alone it
    would return True with no GitHub call. The caller's slug names a
    different owner, so the row must not match at all: this takes the no-row
    live fallback (the unchanged, unfiltered count) instead."""
    write_registry({
        "synthetic-crew": _unit(
            ["synthetic-repo"], repo_owners={"synthetic-repo": "synthetic-owner"},
            ci_workflows={"synthetic-repo": ["ci.yml"]}),
    })
    monkeypatch.setattr(agentctl.subprocess, "run", _fake_gh_workflow_count(0))
    assert agentctl._repo_has_ci_workflows("other-owner/synthetic-repo") is False


# --- an EMPTY declaration always makes the live call, filtered -------------

def test_empty_row_with_only_the_pages_deployment_workflow_live_reports_no_pr_ci(
        agentctl, write_registry, monkeypatch):
    write_registry({
        "synthetic-crew": _unit(
            ["synthetic-repo"], repo_owners={"synthetic-repo": "synthetic-owner"},
            ci_workflows={"synthetic-repo": []}),
    })
    monkeypatch.setattr(agentctl.subprocess, "run",
                         _fake_gh_workflow_paths([_PAGES_DEPLOYMENT_PATH]))
    assert agentctl._repo_has_ci_workflows("synthetic-owner/synthetic-repo") is False


def test_empty_row_with_a_live_repo_defined_workflow_reports_pr_ci(
        agentctl, write_registry, monkeypatch):
    """THE STALE-ROW CASE (CQ·569): the registry's `ci_workflows: []` was
    written once at registration and never refreshed, and the repo has since
    grown a real `.github/workflows/ci.yml`. Trusting the stale `[]` without
    a live check would silence the merge gate on exactly the repos where CI
    now exists.

    This supersedes round 1's `test_registry_empty_declaration_overrides_what_github_would_otherwise_report`,
    which asserted the opposite (`[]` wins over a live repo-defined workflow) —
    that assertion is the bug this round fixes, so it is wrong under the new
    rule. Fails against the current head 2117184be for two independent
    reasons: the slug never matches a row there, and even patched to match,
    round 1's `[]` branch returned False with no live call at all.
    """
    write_registry({
        "synthetic-crew": _unit(
            ["synthetic-repo"], repo_owners={"synthetic-repo": "synthetic-owner"},
            ci_workflows={"synthetic-repo": []}),
    })
    monkeypatch.setattr(agentctl.subprocess, "run",
                         _fake_gh_workflow_paths([".github/workflows/ci.yml"]))
    assert agentctl._repo_has_ci_workflows("synthetic-owner/synthetic-repo") is True


def test_empty_row_with_a_live_non_pages_dynamic_workflow_reports_pr_ci(
        agentctl, write_registry, monkeypatch):
    """Excludes `pages-build-deployment` by exact path only. A CodeQL
    default-setup workflow is also listed under `dynamic/` and DOES run on
    pull requests, so it must still count toward "has PR CI"."""
    write_registry({
        "synthetic-crew": _unit(
            ["synthetic-repo"], repo_owners={"synthetic-repo": "synthetic-owner"},
            ci_workflows={"synthetic-repo": []}),
    })
    monkeypatch.setattr(agentctl.subprocess, "run",
                         _fake_gh_workflow_paths(["dynamic/code-scanning/codeql"]))
    assert agentctl._repo_has_ci_workflows("synthetic-owner/synthetic-repo") is True


# --- an unreadable registry degrades to the live fallback, never a traceback -

def test_registry_unavailable_falls_back_to_the_live_read(agentctl, monkeypatch, capsys):
    def _boom(repo):
        raise agentctl.projects.RegistryUnavailable("stub: the Library is down")
    monkeypatch.setattr(agentctl.projects, "ci_workflows_entry_for_slug", _boom)
    monkeypatch.setattr(agentctl.subprocess, "run", _fake_gh_workflow_count(2))
    assert agentctl._repo_has_ci_workflows("synthetic-org/synthetic-repo") is True
    assert "could not read the registry" in capsys.readouterr().err
