"""`kb sync` and the direct write path (`kb apply`, `kb checkout`) on a
knowledge repo whose origin is not GitHub — company/decisions/0009,
issue_01M3M67C7VB7ZVYQ0MBKHZVYBS.

A fresh Builder install creates the knowledge repo with a local bare
`origin`, and there is no App installed on it to authenticate as: the
convivy-library App is installed on `convivy/knowledge` on GitHub, and
nothing else. `_kb_origin_is_local` is the one function that decides which
branch a caller takes — true only for a local filesystem path or a
`file://` URL, so a GitHub remote, an unrecognised network remote, or a
local-looking `origin` a rewrite actually sends to GitHub all take the
App-identity branch and fail closed rather than pushing over plain git as
the operator's own ssh identity. It is tested directly first; the CLI-level tests
below confirm the branch it picks is the one each command actually uses.

Driven against real git repositories, like the App-identity cage tests this
complements. The `library_identity` fixture those tests use forces the App
branch regardless of origin — exactly the wrong tool here, since the whole
point is to watch the REAL `_kb_library` pick the plain-git branch on its
own. The autouse `_no_test_holds_the_library_app_key` fixture (conftest.py)
still applies: `KEY_PATH` never resolves to a real key in this process, so
any test here that reached for the App identity would get the refusal, not
a live token — which is exactly how the GitHub-origin tests below prove no
network call is attempted.
"""
from __future__ import annotations

import sqlite3
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

import indexer
import kb_writes as kw

REPO = Path(__file__).resolve().parent.parent
SCHEMA = REPO / "lib" / "knowledge" / "schema.sql"

DOC = """---
id: platform/reference/{slug}
kind: reference
project: platform
title: {title}
status: current
audience: [architect]
last_reviewed: 2026-09-28
authors: [operator]
---

{body}
"""

SEEDED = "projects/platform/reference/seeded.md"


def _git(repo: Path, *a):
    return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, check=False)


def _write_doc(root: Path, slug: str, *, title: str = "A doc", body: str = "Body.") -> Path:
    path = root / "projects" / "platform" / "reference" / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DOC.format(slug=slug, title=title, body=body))
    return path


def _patch_indexer_and_writes(monkeypatch, agentctl, checkout: Path, tmp_path: Path) -> Path:
    """Every test here drives `cmd_kb_sync` and/or `cmd_kb_apply`, and both touch
    the KB index and the leases db on the way — pointed at throwaway files so
    no test can reach the real ones, same as `test_kb_sync.py`'s `kb_repos`."""
    db = tmp_path / "index.db"
    monkeypatch.setattr(indexer, "KNOWLEDGE", checkout)
    monkeypatch.setattr(indexer, "PROJECTS", checkout / "projects")
    monkeypatch.setattr(indexer, "DB", db)
    monkeypatch.setattr(indexer, "SCHEMA", SCHEMA)
    monkeypatch.setattr(agentctl, "_kb_indexer", lambda: indexer)
    monkeypatch.setattr(kw, "DB", tmp_path / "kb-writes.db")
    monkeypatch.setattr(agentctl, "_kb_writes", lambda: kw)
    kw.ensure_db()
    return db


@pytest.fixture
def local_origin_repos(tmp_path, monkeypatch, agentctl):
    """A knowledge checkout whose `origin` is a local bare repo — the exact
    topology a solo, no-GitHub Builder install has. `library_identity` is
    deliberately not a dependency: the real `_kb_library` runs unstubbed."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "--quiet", "--bare", "--initial-branch=main")

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "--quiet", "--initial-branch=main")
    _git(seed, "config", "user.email", "t@example.com")
    _git(seed, "config", "user.name", "T")
    _write_doc(seed, "seeded")
    _git(seed, "add", "-A")
    _git(seed, "commit", "--quiet", "-m", "seed")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "--quiet", "-u", "origin", "main")

    checkout = tmp_path / "knowledge"
    subprocess.run(["git", "clone", "--quiet", str(origin), str(checkout)],
                   capture_output=True, text=True, check=True)
    _git(checkout, "config", "user.email", "t@example.com")
    _git(checkout, "config", "user.name", "T")

    monkeypatch.setattr(agentctl, "KNOWLEDGE_ROOT", checkout)
    monkeypatch.setattr(agentctl, "SCRIBE_WORKTREE", tmp_path / "cage-wt")
    db = _patch_indexer_and_writes(monkeypatch, agentctl, checkout, tmp_path)
    return Namespace(origin=origin, seed=seed, checkout=checkout, tmp=tmp_path, db=db)


@pytest.fixture
def github_origin_checkout(tmp_path, monkeypatch, agentctl):
    """A knowledge checkout whose `origin` NAMES github.com but is never
    contacted: the autouse fixture leaves `KEY_PATH` unusable, so the
    App-identity refusal fires at the key-read stage, before any fetch, push
    or `gh` call — the same reason the App-identity cage tests never touch
    the real GitHub either."""
    checkout = tmp_path / "knowledge"
    checkout.mkdir()
    _git(checkout, "init", "--quiet", "--initial-branch=main")
    _git(checkout, "config", "user.email", "t@example.com")
    _git(checkout, "config", "user.name", "T")
    _git(checkout, "remote", "add", "origin", "https://github.com/convivy/knowledge.git")

    monkeypatch.setattr(agentctl, "KNOWLEDGE_ROOT", checkout)
    monkeypatch.setattr(agentctl, "SCRIBE_WORKTREE", tmp_path / "cage-wt")
    _patch_indexer_and_writes(monkeypatch, agentctl, checkout, tmp_path)
    return checkout


@pytest.fixture(params=["https://gitlab.com/convivy/knowledge.git",
                        "ssh://github.com/convivy/knowledge.git"])
def network_origin_checkout(request, tmp_path, monkeypatch, agentctl):
    """A knowledge checkout whose `origin` is a network remote that is neither
    local nor github.com by the old literal pattern: a different host, and a
    GitHub URL form (`ssh://github.com/...` with no `git@` user) that pattern
    never matched. Never contacted, same as `github_origin_checkout` — the
    App-identity refusal fires at the key-read stage, before any fetch."""
    checkout = tmp_path / "knowledge"
    checkout.mkdir()
    _git(checkout, "init", "--quiet", "--initial-branch=main")
    _git(checkout, "config", "user.email", "t@example.com")
    _git(checkout, "config", "user.name", "T")
    _git(checkout, "remote", "add", "origin", request.param)

    monkeypatch.setattr(agentctl, "KNOWLEDGE_ROOT", checkout)
    monkeypatch.setattr(agentctl, "SCRIBE_WORKTREE", tmp_path / "cage-wt")
    _patch_indexer_and_writes(monkeypatch, agentctl, checkout, tmp_path)
    return checkout


def _sync_args(**kw_):
    base = {"full": False, "verify_only": False, "no_heal": False, "dry_run": False,
            "quiet": False}
    base.update(kw_)
    return Namespace(**base)


def _apply_args(doc, base_sha, provenance="in-session", **kw_):
    base = {"doc": doc, "base_sha": base_sha, "provenance": provenance,
            "edits": None, "content": None, "content_file": None, "edits_file": None,
            "message": None, "holder": "run-test",
            "allow_overwrite": False, "dry_run": False, "no_push": False,
            "no_sync": True}
    base.update(kw_)
    return Namespace(**base)


def _propose_args(doc, **kw_):
    base = {"doc": doc, "title": "A proposed change", "rationale": None,
            "base_sha": None, "edits": None, "content": None,
            "content_file": None, "edits_file": None, "holder": "run-test",
            "allow_overwrite": False, "dry_run": False, "delete": False, "also": [],
            "batch": None, "batch_file": None}
    base.update(kw_)
    return Namespace(**base)


def _merge_args(pr=7, repo=None, dry_run=False):
    return Namespace(pr=pr, repo=repo, dry_run=dry_run)


# --- `_kb_origin_is_local`: the one function every path reads --------------


def test_origin_is_not_local_for_the_https_and_ssh_github_forms(tmp_path, agentctl):
    """Regression: these three forms took the App branch under the old
    `_kb_origin_is_github` pattern-match decision, and must still take it now
    that the decision is inverted to "is this local"."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    for url in ("https://github.com/convivy/knowledge.git",
                "git@github.com:convivy/knowledge.git",
                "ssh://git@github.com/convivy/knowledge.git"):
        _git(repo, "remote", "remove", "origin")
        _git(repo, "remote", "add", "origin", url)
        assert agentctl._kb_origin_is_local(repo) is False, url


def test_origin_is_local_for_a_local_path_or_file_url(tmp_path, agentctl):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    bare = tmp_path / "bare-origin"
    for url in (str(bare), f"file://{bare}"):
        _git(repo, "remote", "remove", "origin")
        _git(repo, "remote", "add", "origin", url)
        assert agentctl._kb_origin_is_local(repo) is True, url


def test_origin_is_not_local_for_an_unrecognised_network_remote(tmp_path, agentctl):
    """WHAT the inversion is for: a network remote that never matched the old
    literal github.com pattern — a different host, or a GitHub URL form the
    pattern missed (`ssh://github.com/...` with no `git@` user) — must still
    fail closed to the App branch rather than default to plain git, which
    would push as the operator's own ssh identity."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    for url in ("https://gitlab.com/convivy/knowledge.git",
                "ssh://github.com/convivy/knowledge.git",
                "git@example.com:convivy/knowledge.git"):
        _git(repo, "remote", "remove", "origin")
        _git(repo, "remote", "add", "origin", url)
        assert agentctl._kb_origin_is_local(repo) is False, url


def test_origin_is_local_when_there_is_no_origin_at_all(tmp_path, agentctl):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    assert agentctl._kb_origin_is_local(repo) is True


def test_a_pushurl_or_insteadof_rewrite_to_github_defeats_a_local_fetch_url(tmp_path, agentctl):
    """A local-looking `origin` is not enough: `_kb_origin_is_local` reads what
    `git` will actually push to, so a `pushurl` or a `pushInsteadOf` rewrite
    that sends the push side to github.com must fail closed to the App
    branch even though the fetch side alone still resolves to a local path."""
    bare = tmp_path / "bare-origin"
    bare.mkdir()
    _git(bare, "init", "--quiet", "--bare")

    pushurl_repo = tmp_path / "r-pushurl"
    pushurl_repo.mkdir()
    _git(pushurl_repo, "init", "--quiet")
    _git(pushurl_repo, "remote", "add", "origin", str(bare))
    _git(pushurl_repo, "remote", "set-url", "--push", "origin",
        "https://github.com/convivy/knowledge.git")
    assert agentctl._kb_origin_is_local(pushurl_repo) is False

    insteadof_repo = tmp_path / "r-insteadof"
    insteadof_repo.mkdir()
    _git(insteadof_repo, "init", "--quiet")
    _git(insteadof_repo, "remote", "add", "origin", str(bare))
    _git(insteadof_repo, "config",
        "url.https://github.com/convivy/knowledge.git.pushInsteadOf", str(bare))
    assert agentctl._kb_origin_is_local(insteadof_repo) is False


def test_a_second_pushurl_to_github_defeats_a_local_first_pushurl(tmp_path, agentctl):
    """`git push origin <ref>` pushes to EVERY configured pushurl, not just the
    first, so `_kb_origin_is_local` must judge the whole set. A single-URL
    read (`git remote get-url --push`, with no `--all`) returns only the
    first pushurl — the local one here — and reads this origin as local; that
    is the exact shape that let a push reach GitHub over plain git as the operator's
    own ssh identity."""
    bare = tmp_path / "bare-origin"
    bare.mkdir()
    _git(bare, "init", "--quiet", "--bare")

    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "remote", "set-url", "--add", "--push", "origin", str(bare))
    _git(repo, "remote", "set-url", "--add", "--push", "origin",
        "git@github.com:convivy/knowledge.git")
    assert agentctl._kb_origin_is_local(repo) is False


def test_a_second_fetch_url_to_github_defeats_a_local_first_fetch_url(tmp_path, agentctl):
    """The same additive shape on the fetch side (`remote.origin.url` taking
    more than one value): a single-URL read returns only the first entry, so
    this must be read from the full set too."""
    bare = tmp_path / "bare-origin"
    bare.mkdir()
    _git(bare, "init", "--quiet", "--bare")

    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "remote", "set-url", "--add", "origin",
        "git@github.com:convivy/knowledge.git")
    assert agentctl._kb_origin_is_local(repo) is False


def test_two_local_pushurls_stay_local(tmp_path, agentctl):
    """The additive case is not a blanket refusal: two pushurls that are BOTH
    local must still take the plain-git branch."""
    bare1 = tmp_path / "bare-origin-1"
    bare1.mkdir()
    _git(bare1, "init", "--quiet", "--bare")
    bare2 = tmp_path / "bare-origin-2"
    bare2.mkdir()
    _git(bare2, "init", "--quiet", "--bare")

    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    _git(repo, "remote", "add", "origin", str(bare1))
    _git(repo, "remote", "set-url", "--add", "--push", "origin", str(bare1))
    _git(repo, "remote", "set-url", "--add", "--push", "origin", str(bare2))
    assert agentctl._kb_origin_is_local(repo) is True


def test_a_get_url_failure_other_than_a_missing_remote_fails_closed(tmp_path, agentctl):
    """`_kb_origin_urls` must tell "no such remote" (vacuously local) apart
    from any other `git remote get-url` failure, and the latter must fail
    closed to the App branch — an unreadable side could just as easily be
    hiding a GitHub URL as a local one. A corrupt config value that makes
    `git` itself error (not merely "no such remote") stands in for any such
    failure."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    _git(repo, "remote", "add", "origin", str(tmp_path / "bare-origin"))

    config = repo / ".git" / "config"
    text = config.read_text()
    assert '[remote "origin"]' in text
    text = text.replace('[remote "origin"]', '[remote "origin"]\n\tbroken = "unterminated')
    config.write_text(text)

    r = _git(repo, "remote", "get-url", "--all", "origin")
    assert r.returncode != 0
    assert "no such remote" not in r.stderr.lower()

    assert agentctl._kb_origin_is_local(repo) is False


def test_origin_check_cannot_be_steered_by_an_environment_variable(tmp_path, agentctl, monkeypatch):
    """WHAT #1: nothing but `origin`'s own resolved URLs may choose the branch.
    Plant every plausible override name and confirm none of them move the
    answer."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    _git(repo, "remote", "add", "origin", str(tmp_path / "bare"))
    for var, val in [("GITHUB_TOKEN", "x"), ("GH_TOKEN", "x"),
                     ("KB_FORCE_GITHUB", "1"), ("AGENTCTL_KB_GITHUB", "true"),
                     ("KNOWLEDGE_ORIGIN_IS_GITHUB", "1"), ("GIT_CONFIG_COUNT", "0")]:
        monkeypatch.setenv(var, val)
    assert agentctl._kb_origin_is_local(repo) is True


# --- `kb sync` on a local origin: plain git, no App identity ---------------


def test_kb_sync_fast_forwards_a_local_origin_with_no_app_key(local_origin_repos, agentctl):
    _write_doc(local_origin_repos.seed, "arrived", body="New content.")
    _git(local_origin_repos.seed, "add", "-A")
    _git(local_origin_repos.seed, "commit", "--quiet", "-m", "add arrived")
    _git(local_origin_repos.seed, "push", "--quiet", "origin", "main")

    assert agentctl.cmd_kb_sync(_sync_args()) == 0

    conn = sqlite3.connect(str(local_origin_repos.db))
    try:
        rows = {r[0] for r in conn.execute("SELECT source_ref FROM docs")}
    finally:
        conn.close()
    assert "projects/platform/reference/arrived.md" in rows


# --- `kb apply` on a local origin: plain git, no App identity --------------


def _seeded_base_sha(checkout: Path) -> str:
    r = _git(checkout, "fetch", "--quiet", "origin", "main")
    assert r.returncode == 0
    out = _git(checkout, "show", f"origin/main:{SEEDED}")
    return kw.content_sha(out.stdout if out.returncode == 0 else "")


def test_kb_apply_lands_a_commit_in_the_local_bare_origin_with_no_app_key(local_origin_repos, agentctl):
    base_sha = _seeded_base_sha(local_origin_repos.checkout)

    rc = agentctl.cmd_kb_apply(_apply_args(
        SEEDED, base_sha, edits='[{"old": "Body.", "new": "Edited via plain git."}]'))
    assert rc == 0

    out = _git(local_origin_repos.origin, "show", f"main:{SEEDED}")
    assert out.returncode == 0
    assert "Edited via plain git." in out.stdout


def test_kb_apply_commits_under_gits_own_configured_identity_on_a_local_origin(
        local_origin_repos, agentctl):
    """WHAT #2: with no App involved, authorship is whatever `git` already
    resolves for the cage worktree — not forced to any particular name here,
    just NOT the App's bot identity, which would be wrong on a repo the App
    was never installed on."""
    base_sha = _seeded_base_sha(local_origin_repos.checkout)
    rc = agentctl.cmd_kb_apply(_apply_args(
        SEEDED, base_sha, edits='[{"old": "Body.", "new": "Edited."}]'))
    assert rc == 0

    author = _git(local_origin_repos.origin, "log", "-1", "--format=%an <%ae>", "main").stdout.strip()
    assert author != "convivy-library[bot] <328494887+convivy-library[bot]@users.noreply.github.com>"
    assert author == "T <t@example.com>"


def test_kb_checkout_reads_a_local_origin_with_no_app_key(local_origin_repos, agentctl):
    """`kb checkout` (`_kb_blob_at_origin`) is the read half of the direct write
    path — `kb apply` needs its content-sha, and `kb_create` (the MCP tool)
    reaches it the same way. It shares `_kb_library_git`, so it takes the
    plain-git branch here too."""
    rc = agentctl.cmd_kb_checkout(Namespace(doc=SEEDED, holder="run-test", note=None, sha_only=False))
    assert rc == 0


# --- App-identity paths still refuse, unchanged, on a GitHub origin --------


def test_kb_sync_still_refuses_on_a_github_origin_with_no_app_key(github_origin_checkout, agentctl, capsys):
    rc = agentctl.cmd_kb_sync(_sync_args())
    assert rc == 1
    assert "cannot obtain the convivy-library App identity" in capsys.readouterr().err


def test_kb_apply_still_refuses_on_a_github_origin_with_no_app_key(github_origin_checkout, agentctl, capsys):
    rc = agentctl.cmd_kb_apply(_apply_args(SEEDED, kw.content_sha(""), content="x"))
    assert rc == 2
    assert "cannot obtain the convivy-library App identity" in capsys.readouterr().err


# --- an unrecognised network origin also takes the App branch, with its own ----
# --- refusal wording, so it never reads like a missing key on a real GitHub ---
# --- install --------------------------------------------------------------


def test_kb_sync_refuses_on_an_unrecognised_network_origin_without_looking_like_a_missing_key(
        network_origin_checkout, agentctl, capsys):
    rc = agentctl.cmd_kb_sync(_sync_args())
    assert rc == 1
    err = capsys.readouterr().err
    assert "neither a local repository nor a GitHub remote" in err
    assert "cannot obtain the convivy-library App identity" not in err


def test_kb_apply_refuses_on_an_unrecognised_network_origin_without_looking_like_a_missing_key(
        network_origin_checkout, agentctl, capsys):
    rc = agentctl.cmd_kb_apply(_apply_args(SEEDED, kw.content_sha(""), content="x"))
    assert rc == 2
    err = capsys.readouterr().err
    assert "neither a local repository nor a GitHub remote" in err
    assert "cannot obtain the convivy-library App identity" not in err


# --- `kb propose` / `kb merge` stay GitHub-only ------------------------------


def test_kb_propose_refuses_before_acting_on_a_local_origin(local_origin_repos, agentctl, capsys):
    rc = agentctl.cmd_kb_propose(_propose_args(SEEDED, content="x"))
    assert rc == 2
    err = capsys.readouterr().err
    assert "GitHub connected" in err
    # Refused before any git work: the cage worktree was never created.
    assert not (local_origin_repos.tmp / "cage-wt").exists()


def test_kb_merge_refuses_before_acting_on_a_local_origin(local_origin_repos, agentctl, capsys):
    rc = agentctl.cmd_kb_merge(_merge_args())
    assert rc == 2
    assert "GitHub connected" in capsys.readouterr().err


def test_kb_propose_and_kb_merge_still_reach_the_app_identity_on_a_github_origin(
        github_origin_checkout, agentctl, capsys):
    """The GitHub-only refusal must not fire on a GitHub origin — the App-key
    refusal underneath it does, exactly as today."""
    rc = agentctl.cmd_kb_propose(_propose_args(SEEDED, content="x"))
    assert rc == 2
    err = capsys.readouterr().err
    assert "GitHub connected" not in err
    assert "cannot obtain the convivy-library App identity" in err

    rc = agentctl.cmd_kb_merge(_merge_args())
    assert rc == 2
    err = capsys.readouterr().err
    assert "GitHub connected" not in err
    assert "cannot obtain the convivy-library App identity" in err


def test_kb_propose_and_kb_merge_reach_the_app_path_on_an_unrecognised_network_origin(
        network_origin_checkout, agentctl, capsys):
    """Not local, so not the GitHub-only refusal (`_kb_refuse_if_not_github`) —
    `kb propose`/`kb merge` still reach the App-identity attempt, exactly as on
    a GitHub origin, and refuse with the unrecognised-origin wording rather
    than a missing-key one."""
    rc = agentctl.cmd_kb_propose(_propose_args(SEEDED, content="x"))
    assert rc == 2
    err = capsys.readouterr().err
    assert "GitHub connected" not in err
    assert "neither a local repository nor a GitHub remote" in err

    rc = agentctl.cmd_kb_merge(_merge_args())
    assert rc == 2
    err = capsys.readouterr().err
    assert "GitHub connected" not in err
    assert "neither a local repository nor a GitHub remote" in err
