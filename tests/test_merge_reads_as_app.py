"""`kb merge` and `docs merge` make their `gh` reads with a read token from the
install's GitHub App, not the operator's ambient `gh` login.

The operator logged his own `gh` credential out of the machine that serves
sessions (company/decisions/0027 item 3), and every read these two commands
made through `gh` failed from then on. `_merge_reads_as_app` swaps the process
environment for `_gh_run_env(repo, "read")`'s copy for the length of each read
phase. These tests stub `_gh_run_env` with one that yields a recognisable
token and records what it was asked for, then check that the token is in
the environment exactly while the reads run.
"""
from __future__ import annotations

import argparse
import os
import types
from contextlib import contextmanager

import docs_merge as DM
import github_app as GA
import kb_merge as KM
import pytest

TOKEN = "ghs_test_read_token"
REPO = "convivy/knowledge"


@pytest.fixture
def mints(agentctl, monkeypatch):
    """Replace `_gh_run_env` with one that records each `(repo, verb)` it is
    asked for and yields the environment plus `GH_TOKEN=TOKEN`."""
    asked = []

    @contextmanager
    def fake(repo_slug, verb):
        asked.append((repo_slug, verb))
        env = dict(os.environ)
        env["GH_TOKEN"] = TOKEN
        yield env

    monkeypatch.setattr(agentctl, "_gh_run_env", fake)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    return asked


@pytest.fixture
def kb_merge_stubs(agentctl, monkeypatch):
    monkeypatch.setattr(agentctl, "_kb_refuse_if_not_github", lambda *_a: False)
    monkeypatch.setattr(KM, "load_config", lambda *a, **k: types.SimpleNamespace(repo=REPO))
    monkeypatch.setattr(agentctl, "_kb_library_git", lambda *a, **k: (object(), None))


def _kb_args():
    return argparse.Namespace(pr=7, repo=None, dry_run=False, check_wait=1, head_wait=1)


def test_the_block_installs_the_minted_environment_and_restores_it(agentctl, mints):
    with agentctl._merge_reads_as_app(REPO):
        assert os.environ.get("GH_TOKEN") == TOKEN
    assert "GH_TOKEN" not in os.environ
    assert mints == [(REPO, "read")]


def test_the_block_restores_the_environment_when_a_read_raises(agentctl, mints):
    with pytest.raises(RuntimeError), agentctl._merge_reads_as_app(REPO):
        raise RuntimeError("a read failed")
    assert "GH_TOKEN" not in os.environ


def test_kb_merge_makes_its_first_read_with_the_app_token(
        agentctl, mints, kb_merge_stubs, monkeypatch):
    seen = []

    def fake_read(repo, pr, library):
        seen.append(os.environ.get("GH_TOKEN"))
        raise RuntimeError("stop after the read")

    monkeypatch.setattr(agentctl, "_kb_merge_read", fake_read)

    rc = agentctl.cmd_kb_merge(_kb_args())

    assert rc == 2
    assert seen == [TOKEN]
    assert mints == [(REPO, "read")]
    assert "GH_TOKEN" not in os.environ


def test_kb_merge_refuses_without_reading_when_the_mint_fails(
        agentctl, kb_merge_stubs, monkeypatch, capsys):
    @contextmanager
    def failing(repo_slug, verb):
        raise GA.Unreadable("no App key")
        yield  # pragma: no cover

    def never_read(*a, **k):
        raise AssertionError("read without a token")

    monkeypatch.setattr(agentctl, "_gh_run_env", failing)
    monkeypatch.setattr(agentctl, "_kb_merge_read", never_read)

    rc = agentctl.cmd_kb_merge(_kb_args())

    assert rc == 2
    assert "cannot mint a read token" in capsys.readouterr().err


def test_kb_merge_rebuild_phase_reads_under_its_own_mint(
        agentctl, mints, kb_merge_stubs, monkeypatch):
    """The first read's block has closed before the rebuild starts, so the
    rebuild's reads need, and get, a second mint."""
    facts = types.SimpleNamespace(head_sha="a" * 40)
    seen = []

    def fake_read(repo, pr, library):
        return facts, facts.head_sha, {}

    def fake_rebuild(repo, pr, f, config, token):
        seen.append(os.environ.get("GH_TOKEN"))
        return None, "could not rebuild"

    monkeypatch.setattr(agentctl, "_kb_merge_read", fake_read)
    monkeypatch.setattr(KM, "refusals", lambda f, c: ["the branch does not contain main"])
    monkeypatch.setattr(KM, "rebuild_worth_trying", lambda f: True)
    monkeypatch.setattr(agentctl, "_kb_merge_write_token", lambda c: ("write-token", None))
    monkeypatch.setattr(agentctl, "_kb_merge_rebuild", fake_rebuild)

    rc = agentctl.cmd_kb_merge(_kb_args())

    assert rc == 1
    assert seen == [TOKEN]
    assert mints == [(REPO, "read"), (REPO, "read")]
    assert "GH_TOKEN" not in os.environ


def test_docs_merge_reads_with_the_app_token(agentctl, mints, monkeypatch):
    seen = []

    def fake_pr_json(pr, fields, repo=None):
        seen.append(("pr", os.environ.get("GH_TOKEN")))
        raise RuntimeError("stop after the PR read")

    def fake_review(*a, **k):
        seen.append(("review", os.environ.get("GH_TOKEN")))
        return types.SimpleNamespace(head="", ok=False, reasons=["x"], stamps=[],
                                     read_error=None)

    monkeypatch.setattr(DM, "load_config", lambda *a, **k: (_ for _ in ()).throw(
        DM.Unreadable("no config")))
    monkeypatch.setattr(agentctl, "_kb_library_git", lambda *a, **k: (None, None))
    monkeypatch.setattr(agentctl, "_gh_pr_json", fake_pr_json)
    monkeypatch.setattr(agentctl, "_review_status_decision", fake_review)

    rc = agentctl.cmd_docs_merge(argparse.Namespace(pr=7, repo="convivy/library",
                                                    dry_run=True))

    assert rc == 1
    assert seen == [("pr", TOKEN), ("review", TOKEN)]
    assert mints == [("convivy/library", "read")]
    assert "GH_TOKEN" not in os.environ
