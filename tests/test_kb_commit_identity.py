"""`_kb_commit`'s fallback identity — Shipwright issue on agent-platform
fc54a97, 2026-09-28: on a fresh, no-GitHub Builder install (the local-origin
path PR #698 added), `kb_create` failed with `kb apply: git commit failed:
Author identity unknown` because a fresh adopter account has no
`user.name`/`user.email` anywhere and `_PlainGitLibrary.commit_env()` returns
`None`, so the commit took whatever identity `git` already had — none.

Every scenario here forces git's own identity resolution to actually run
(`env=None`, or a hand-built env dict), never a stub — the same shape as
`test_kb_non_github_origin.py`, which drives the real `_kb_library` rather
than mocking it.

`user.useConfigOnly = true` (set via `GIT_CONFIG_KEY_0`/`GIT_CONFIG_VALUE_0` in
the "no identity" tests) is what makes "no identity anywhere" reproducible on
any machine. Without it, git falls back to guessing a name and email from the
OS user database and hostname: that guess succeeds on a normal developer
machine (a real GECOS name, a resolvable hostname) and only fails on a
container whose GECOS is empty or whose hostname has no domain.
`useConfigOnly` disables the guess outright, producing the same "Author
identity unknown" banner the bug report quoted, on any machine.

Round 2 (PR #700): the tests above call `_kb_commit` directly, which shows
the helper works but not that `kb apply` — the path `kb_create` reaches —
actually calls it on the failing topology. `test_kb_apply_falls_back_to_builder_end_to_end_with_no_identity_anywhere`
below drives `cmd_kb_apply` itself, reusing `test_kb_non_github_origin.py`'s
local-bare-origin fixtures rather than building new ones, with the checkout
left with no `user.name`/`user.email` in its own config (unlike that file's
`local_origin_repos`, which sets one) and the whole process environment
isolated to have none anywhere else either. That the fixture reproduces the
Shipwright report against the pre-fix `bin/agentctl` was confirmed by hand
against `origin/main`, not as a suite test — see the PR body's Round 2
section for the command and the failing assertion.
"""
from __future__ import annotations

import os
import subprocess
from argparse import Namespace
from pathlib import Path

import test_kb_non_github_origin as kb_non_github


def _git(repo: Path, *a, env=None):
    return subprocess.run(["git", "-C", str(repo), *a], capture_output=True,
                          text=True, env=env)


def _repo_with_one_staged_file(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet", "--initial-branch=main")
    (repo / "f.txt").write_text("hi\n")
    _git(repo, "add", "f.txt")
    return repo


def _isolated_env_with_no_identity_anywhere(monkeypatch, tmp_path: Path) -> None:
    """No `user.name`/`user.email` in any config, and none of git's own
    guess-from-the-OS fallbacks either — `useConfigOnly` disables the guess
    outright, which is what makes this reproducible on any machine (see the
    module docstring). Sets these on `os.environ` via `monkeypatch`, so a
    caller that passes `env=None` (as `_PlainGitLibrary.commit_env()` does)
    inherits them, exactly as the real subprocess call does."""
    home = tmp_path / "empty-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # A path that is never written to — the point of the "no config file was
    # written" assertion below is that the fallback identity never lands here.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "unwritten.gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.useConfigOnly")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(var, raising=False)


def test_the_probe_itself_fails_with_no_identity_anywhere(tmp_path, monkeypatch):
    """Confirms the isolation helper actually reproduces the failure the fix
    handles, independently of `_kb_commit` — if this assertion ever stops
    holding (a git upgrade changes the guess fallback, say), every test below
    would pass for the wrong reason."""
    repo = _repo_with_one_staged_file(tmp_path)
    _isolated_env_with_no_identity_anywhere(monkeypatch, tmp_path)
    r = _git(repo, "var", "GIT_AUTHOR_IDENT")
    assert r.returncode != 0
    assert "Author identity unknown" in r.stdout + r.stderr


def test_commit_falls_back_to_builder_when_no_identity_resolves_anywhere(
        tmp_path, monkeypatch, agentctl):
    repo = _repo_with_one_staged_file(tmp_path)
    _isolated_env_with_no_identity_anywhere(monkeypatch, tmp_path)

    r = agentctl._kb_commit(repo, "add a file", env=None)
    assert r.returncode == 0, r.stderr

    log = _git(repo, "log", "-1", "--format=%an <%ae> / %cn <%ce>").stdout.strip()
    assert log == "Builder <builder@localhost> / Builder <builder@localhost>"
    assert not (tmp_path / "unwritten.gitconfig").exists()
    # Never handed to `-c`, so it never lands in the repo's own config either.
    assert "Builder" not in _git(repo, "config", "--list").stdout


def test_a_user_identity_in_the_isolated_global_config_wins(tmp_path, monkeypatch, agentctl):
    repo = _repo_with_one_staged_file(tmp_path)
    home = tmp_path / "home-with-identity"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    global_config = tmp_path / "global.gitconfig"
    global_config.write_text("[user]\n\tname = Ada Adopter\n\temail = ada@example.com\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(var, raising=False)

    r = agentctl._kb_commit(repo, "add a file", env=None)
    assert r.returncode == 0, r.stderr

    log = _git(repo, "log", "-1", "--format=%an <%ae> / %cn <%ce>").stdout.strip()
    assert log == "Ada Adopter <ada@example.com> / Ada Adopter <ada@example.com>"


def test_git_author_and_committer_env_vars_win(tmp_path, monkeypatch, agentctl):
    """`GIT_AUTHOR_NAME`/`GIT_AUTHOR_EMAIL` (and their `COMMITTER` counterparts)
    outrank config — an operator's own environment identity, exactly like
    `kb_merge.commit_env`'s shape, must win over the Builder fallback even
    where config resolves nothing at all."""
    repo = _repo_with_one_staged_file(tmp_path)
    env = dict(**{"HOME": str(tmp_path / "empty-home-2")})
    (tmp_path / "empty-home-2").mkdir()
    env["GIT_CONFIG_GLOBAL"] = str(tmp_path / "unwritten-2.gitconfig")
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "user.useConfigOnly"
    env["GIT_CONFIG_VALUE_0"] = "true"
    env["GIT_AUTHOR_NAME"] = "Env Author"
    env["GIT_AUTHOR_EMAIL"] = "author@example.com"
    env["GIT_COMMITTER_NAME"] = "Env Author"
    env["GIT_COMMITTER_EMAIL"] = "author@example.com"
    env["PATH"] = os.environ.get("PATH", "")

    r = agentctl._kb_commit(repo, "add a file", env=env)
    assert r.returncode == 0, r.stderr

    log = _git(repo, "log", "-1", "--format=%an <%ae> / %cn <%ce>").stdout.strip()
    assert log == "Env Author <author@example.com> / Env Author <author@example.com>"
    assert not (tmp_path / "unwritten-2.gitconfig").exists()


def test_the_app_path_environment_is_unchanged(tmp_path, monkeypatch, agentctl):
    """`_KbLibrary.commit_env()` (`kb_merge.commit_env`) always supplies both
    `GIT_AUTHOR_*` and `GIT_COMMITTER_*` for the App's bot identity, so the
    probe always succeeds and the Builder fallback is never reached — even
    with the surrounding environment isolated to have no identity at all,
    which is the scenario that WOULD trigger the fallback for `env=None`."""
    import kb_merge as KM

    repo = _repo_with_one_staged_file(tmp_path)
    _isolated_env_with_no_identity_anywhere(monkeypatch, tmp_path)

    app_env = KM.commit_env()
    r = agentctl._kb_commit(repo, "add a file", env=app_env)
    assert r.returncode == 0, r.stderr

    log = _git(repo, "log", "-1", "--format=%an <%ae> / %cn <%ce>").stdout.strip()
    assert log == f"{KM.commit_name()} <{KM.commit_email()}> / {KM.commit_name()} <{KM.commit_email()}>"
    assert "Builder" not in log


# --- end to end: `kb apply` on a local origin, with no identity anywhere ----


def _local_origin_no_identity(tmp_path, monkeypatch, agentctl):
    """The exact topology from the Shipwright report: a local bare `origin`
    (`test_kb_non_github_origin.py`'s `local_origin_repos` shape) and, unlike
    that fixture, a checkout left with no `user.name`/`user.email` in its own
    config, in a process environment isolated to have none anywhere else
    either (`_isolated_env_with_no_identity_anywhere`). The seed repo still
    gets a local identity, via `git config` in its own `.git/config` —
    `useConfigOnly` only disables git's OS-guess fallback, not ordinary
    per-repo config, so seeding a commit there is unaffected."""
    _isolated_env_with_no_identity_anywhere(monkeypatch, tmp_path)

    origin = tmp_path / "origin"
    origin.mkdir()
    kb_non_github._git(origin, "init", "--quiet", "--bare", "--initial-branch=main")

    seed = tmp_path / "seed"
    seed.mkdir()
    kb_non_github._git(seed, "init", "--quiet", "--initial-branch=main")
    kb_non_github._git(seed, "config", "user.email", "t@example.com")
    kb_non_github._git(seed, "config", "user.name", "T")
    kb_non_github._write_doc(seed, "seeded")
    kb_non_github._git(seed, "add", "-A")
    kb_non_github._git(seed, "commit", "--quiet", "-m", "seed")
    kb_non_github._git(seed, "remote", "add", "origin", str(origin))
    kb_non_github._git(seed, "push", "--quiet", "-u", "origin", "main")

    checkout = tmp_path / "knowledge"
    r = subprocess.run(["git", "clone", "--quiet", str(origin), str(checkout)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    # No `git config user.name`/`user.email` here — the point under test.

    monkeypatch.setattr(agentctl, "KNOWLEDGE_ROOT", checkout)
    monkeypatch.setattr(agentctl, "SCRIBE_WORKTREE", tmp_path / "cage-wt")
    db = kb_non_github._patch_indexer_and_writes(monkeypatch, agentctl, checkout, tmp_path)

    return Namespace(origin=origin, seed=seed, checkout=checkout, tmp=tmp_path, db=db)


def test_kb_apply_falls_back_to_builder_end_to_end_with_no_identity_anywhere(
        tmp_path, monkeypatch, agentctl):
    repos = _local_origin_no_identity(tmp_path, monkeypatch, agentctl)
    base_sha = kb_non_github._seeded_base_sha(repos.checkout)

    rc = agentctl.cmd_kb_apply(kb_non_github._apply_args(
        kb_non_github.SEEDED, base_sha,
        edits='[{"old": "Body.", "new": "Edited with no identity anywhere."}]'))
    assert rc == 0

    out = _git(repos.origin, "show", "main:" + kb_non_github.SEEDED)
    assert out.returncode == 0
    assert "Edited with no identity anywhere." in out.stdout

    log = _git(repos.origin, "log", "-1", "--format=%an <%ae> / %cn <%ce>", "main").stdout.strip()
    assert log == "Builder <builder@localhost> / Builder <builder@localhost>"

    message = _git(repos.origin, "log", "-1", "--format=%B", "main").stdout
    assert "KB-Write: update" in message

    assert not (tmp_path / "unwritten.gitconfig").exists()
    # Never handed to `-c`, so it never lands in the checkout's own config
    # (shared with the cage worktree `kb apply` actually commits in) either.
    assert "Builder" not in _git(repos.checkout, "config", "--list").stdout
