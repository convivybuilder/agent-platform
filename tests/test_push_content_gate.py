"""push-content-gate — the pre-push-time half of the content gate.

Every test here runs the real `bin/push-content-gate` as a subprocess, with a
real `git push` from a temp working repo to a temp bare `file://` remote, the
gate installed as that temp repo's own `.git/hooks/pre-push` (the shape the
real stub a later PR installs will take: `exec push-content-gate <repo> "$@"`,
passing git's own `pre-push` arguments and stdin through unchanged).

The engine `publish_scan.scan_commits` calls is stubbed exactly the way
`tests/test_publish_scan.py` stubs it: the operator config's
`shipwright_checkout`, `builder_private_names_file` and
`builder_machine_names_file` keys are pointed at temp paths holding a real,
tiny `bin/publish-engine` script this file controls completely (the same
`_write_fake_engine`/`_FAKE_ENGINE` fixture `test_publish_scan.py` already
defines, imported rather than copied), so every test here exercises
`push-content-gate`'s own parsing of a real `pre-push` stdin payload and a
real subprocess exit, never a canned return value standing in for one.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess as sp
import tempfile
import textwrap
from pathlib import Path

import pytest

from test_publish_scan import _write_fake_engine

REPO = Path(__file__).resolve().parent.parent
GATE = REPO / "bin" / "push-content-gate"

# An empty, real directory so a subprocess's $HOME finds no real operator
# config or credential under it — every test below points
# CONVIVY_OPERATOR_CONFIG at its own fixture file instead.
_FAKE_HOME = tempfile.mkdtemp(prefix="push-content-gate-test-home-")


def _run(*args, cwd=None, env=None, input=None):
    return sp.run(["git", *args], cwd=cwd, env=env, input=input,
                  capture_output=True, text=True)


def _commit(repo: Path, env: dict, message: str, *, filename: str = "f.txt",
            content: str = "hello\n") -> str:
    (repo / filename).write_text(content)
    assert _run("-C", str(repo), "add", "-A", env=env).returncode == 0
    result = _run("-C", str(repo), "-c", "user.email=t@t.example", "-c", "user.name=t",
                  "commit", "-q", "-m", message, env=env)
    assert result.returncode == 0, result.stderr
    return _run("-C", str(repo), "rev-parse", "HEAD", env=env).stdout.strip()


def _init_repo(repo: Path, env: dict, branch: str = "main") -> None:
    assert _run("init", "-q", "-b", branch, str(repo), env=env).returncode == 0


def _init_bare(path: Path, env: dict, branch: str = "main") -> None:
    assert _run("init", "-q", "--bare", "-b", branch, str(path), env=env).returncode == 0


def _install_hook(repo: Path, repo_name: str) -> None:
    """A `pre-push` hook shaped exactly like the stub a later PR installs:
    prepend the clone's own repository name, pass everything else straight
    through (git's own `$1`/`$2` and stdin)."""
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "pre-push"
    hook.write_text(f'#!/bin/sh\nexec "{GATE}" "{repo_name}" "$@"\n')
    hook.chmod(hook.stat().st_mode | stat.S_IEXEC)


def _remote_ref_sha(bare: Path, env: dict, ref: str = "refs/heads/main") -> str | None:
    result = _run("-C", str(bare), "rev-parse", "--verify", ref, env=env)
    return result.stdout.strip() if result.returncode == 0 else None


@pytest.fixture
def names_files(tmp_path):
    private = tmp_path / "private-names"
    machine = tmp_path / "machine-names"
    private.write_text("Someone\n")
    machine.write_text("somehost\n")
    return private, machine


def _shipwright_checkout_with(root: Path, write_engine) -> Path:
    """A real git repository under `root`, whose `origin/main` holds
    whatever `write_engine(checkout)` writes into it as `bin/publish-engine`
    — mirrors `test_publish_scan.py`'s own `_real_shipwright_checkout`, so
    `publish_scan._export_shipwright_tree`'s real `git archive` call is
    exercised here too, never stubbed out."""
    checkout = root / "shipwright"
    sp.run(["git", "init", "-q", "-b", "trunk", str(checkout)], check=True)
    write_engine(checkout)
    sp.run(["git", "-C", str(checkout), "add", "-A"], check=True)
    sp.run(["git", "-C", str(checkout), "-c", "user.email=t@t.example",
            "-c", "user.name=t", "commit", "-q", "-m", "engine"], check=True)
    sp.run(["git", "-C", str(checkout), "update-ref", "refs/remotes/origin/main", "HEAD"],
           check=True)
    return checkout


@pytest.fixture
def operator_config(tmp_path, names_files):
    """The operator config file `publish_scan`'s resolvers read, pointed at a
    fresh Shipwright checkout holding the shared fake engine, and at the two
    names files above."""
    checkout = _shipwright_checkout_with(tmp_path, _write_fake_engine)

    private, machine = names_files
    cfg = tmp_path / "operator.json"
    cfg.write_text(json.dumps({
        "shipwright_checkout": str(checkout),
        "builder_private_names_file": str(private),
        "builder_machine_names_file": str(machine),
    }))
    return cfg


@pytest.fixture
def gate_env(operator_config):
    """The environment every `git push` in this file runs under: this
    process's own environment (so the deployed `git` on PATH — a wrapper on
    a dispatched run, same as `test_publish_scan.py` already relies on —
    resolves and behaves ordinarily against these foreign temp repos), with
    $HOME repointed at a fake, empty directory and CONVIVY_OPERATOR_CONFIG
    pointed at this test's own fixture config, never the machine's real
    one."""
    env = dict(os.environ)
    env["HOME"] = _FAKE_HOME
    env["CONVIVY_OPERATOR_CONFIG"] = str(operator_config)
    return env


def _repo_pair(tmp_path, gate_env, repo_name="agent-platform"):
    """A working repo with one clean commit on `main`, a bare `file://`
    remote, `origin` pointed at it, and the gate installed as this repo's
    own `pre-push` hook for `repo_name`."""
    repo = tmp_path / "work"
    bare = tmp_path / "bare.git"
    _init_repo(repo, gate_env)
    base_sha = _commit(repo, gate_env, "base")
    _init_bare(bare, gate_env)
    assert _run("-C", str(repo), "remote", "add", "origin", f"file://{bare}",
                env=gate_env).returncode == 0
    _install_hook(repo, repo_name)
    return repo, bare, base_sha


# --- a clean push ------------------------------------------------------------

def test_a_clean_push_succeeds_and_the_remote_gets_the_ref(tmp_path, gate_env):
    repo, bare, base_sha = _repo_pair(tmp_path, gate_env)
    result = _run("-C", str(repo), "push", "origin", "main", env=gate_env)
    assert result.returncode == 0, result.stderr
    assert _remote_ref_sha(bare, gate_env) == base_sha


# --- a refused finding --------------------------------------------------------

def test_a_marker_the_fake_engine_refuses_at_the_tip_is_refused(tmp_path, gate_env):
    repo, bare, _base_sha = _repo_pair(tmp_path, gate_env)
    env = {**gate_env, "FAKE_ENGINE_FINDINGS": "1"}
    result = _run("-C", str(repo), "push", "origin", "main", env=env)
    assert result.returncode != 0
    assert "push refused by the content gate" in result.stderr
    assert "nothing was sent" in result.stderr
    assert _remote_ref_sha(bare, gate_env) is None


def test_a_marker_added_then_removed_in_the_same_push_is_still_refused(tmp_path, gate_env):
    """A finding in a commit that is NOT the push's tip must still refuse —
    the finding reached a ref on the remote the instant the first commit
    did, regardless of what a later commit in the same push undoes."""
    repo, bare, base_sha = _repo_pair(tmp_path, gate_env)
    # base_sha is pushed first, cleanly, so later pushes are ranges, not new refs.
    clean = _run("-C", str(repo), "push", "origin", "main", env=gate_env)
    assert clean.returncode == 0, clean.stderr

    bad_sha = _commit(repo, gate_env, "adds a marker", content="marker\n")
    good_sha = _commit(repo, gate_env, "removes the marker", content="clean again\n")
    env = {**gate_env, "FAKE_ENGINE_FAIL_REF": bad_sha,
           "FAKE_ENGINE_CALL_LOG": str(tmp_path / "calls.log")}
    result = _run("-C", str(repo), "push", "origin", "main", env=env)
    assert result.returncode != 0
    assert bad_sha[:9] in result.stderr
    assert _remote_ref_sha(bare, gate_env) == base_sha  # unchanged by the refused push
    call_log = (tmp_path / "calls.log")
    # scan_commits stops at the first refusal: bad_sha is scanned, good_sha never is.
    assert call_log.read_text().splitlines() == [bad_sha]
    assert good_sha not in call_log.read_text()


# --- a branch deletion never calls the engine ---------------------------------

def test_a_branch_deletion_never_calls_the_engine(tmp_path, gate_env):
    repo, bare, base_sha = _repo_pair(tmp_path, gate_env)
    other = _run("-C", str(repo), "checkout", "-q", "-b", "scratch", env=gate_env)
    assert other.returncode == 0, other.stderr
    pushed = _run("-C", str(repo), "push", "origin", "scratch", env=gate_env)
    assert pushed.returncode == 0, pushed.stderr
    assert _remote_ref_sha(bare, gate_env, "refs/heads/scratch") == base_sha

    call_log = tmp_path / "deletion-calls.log"
    env = {**gate_env, "FAKE_ENGINE_CALL_LOG": str(call_log)}
    deleted = _run("-C", str(repo), "push", "origin", "--delete", "scratch", env=env)
    assert deleted.returncode == 0, deleted.stderr
    assert _remote_ref_sha(bare, gate_env, "refs/heads/scratch") is None
    assert not call_log.exists()


# --- a new branch scans only the commits not already on the remote -----------

def test_a_new_branch_calls_the_engine_only_for_commits_not_on_the_remote(tmp_path, gate_env):
    repo, bare, base_sha = _repo_pair(tmp_path, gate_env)
    base_push = _run("-C", str(repo), "push", "origin", "main", env=gate_env)
    assert base_push.returncode == 0, base_push.stderr

    checkout = _run("-C", str(repo), "checkout", "-q", "-b", "feature", env=gate_env)
    assert checkout.returncode == 0, checkout.stderr
    new_sha = _commit(repo, gate_env, "feature work", content="feature\n")

    call_log = tmp_path / "feature-calls.log"
    env = {**gate_env, "FAKE_ENGINE_CALL_LOG": str(call_log)}
    pushed = _run("-C", str(repo), "push", "origin", "feature", env=env)
    assert pushed.returncode == 0, pushed.stderr
    assert call_log.read_text().splitlines() == [new_sha]
    assert base_sha not in call_log.read_text()


# --- engine exit codes --------------------------------------------------------

def test_an_engine_exit_of_2_is_refused(tmp_path, gate_env):
    repo, bare, base_sha = _repo_pair(tmp_path, gate_env)
    env = {**gate_env, "FAKE_ENGINE_REFUSED": "1"}
    result = _run("-C", str(repo), "push", "origin", "main", env=env)
    assert result.returncode != 0
    assert "push refused by the content gate" in result.stderr
    assert _remote_ref_sha(bare, gate_env) is None


_FINDING_SAYS_COULD_NOT_RUN_ENGINE = textwrap.dedent('''\
    #!/usr/bin/env python3
    import argparse, sys
    p = argparse.ArgumentParser(prog="publish-engine")
    p.add_argument("name", choices=["agent-platform", "library", "write-better"])
    p.add_argument("--from", dest="source", required=True)
    p.add_argument("--ref", required=True)
    p.add_argument("--scan-only", action="store_true")
    args = p.parse_args()
    print(f"publish-engine: {args.name} at {args.ref} (deadbeef9) would be refused "
          f"by a release. No value is printed; open each file to judge it.\\n"
          f"  content scan (1):\\n    tests/fixture.py:3: a string that could not run "
          f"as a shell command")
    sys.exit(1)
''')


def _write_finding_says_could_not_run_engine(root: Path) -> Path:
    engine = root / "bin" / "publish-engine"
    engine.parent.mkdir(parents=True, exist_ok=True)
    engine.write_text(_FINDING_SAYS_COULD_NOT_RUN_ENGINE)
    engine.chmod(engine.stat().st_mode | stat.S_IEXEC)
    return root


def test_a_finding_whose_text_contains_could_not_run_is_not_mislabelled(tmp_path, gate_env, operator_config):
    """A finding (exit 1) whose echoed text happens to contain the words
    "could not run" must not be read as a scan failure — `main()` no longer
    guesses a label from `PublishScanRefused`'s message text at all."""
    cfg = json.loads(operator_config.read_text())
    finding_checkout = _shipwright_checkout_with(
        tmp_path / "finding", _write_finding_says_could_not_run_engine
    )
    cfg["shipwright_checkout"] = str(finding_checkout)
    cfg_path = tmp_path / "finding-operator.json"
    cfg_path.write_text(json.dumps(cfg))

    env = {**gate_env, "CONVIVY_OPERATOR_CONFIG": str(cfg_path)}
    repo, bare, base_sha = _repo_pair(tmp_path, env)
    result = _run("-C", str(repo), "push", "origin", "main", env=env)
    assert result.returncode != 0
    assert "push refused by the content gate" in result.stderr
    assert "the gate fails closed" not in result.stderr
    assert _remote_ref_sha(bare, gate_env) is None


_SIGINT_ENGINE = textwrap.dedent('''\
    #!/usr/bin/env python3
    import argparse, os, signal, sys
    p = argparse.ArgumentParser(prog="publish-engine")
    p.add_argument("name", choices=["agent-platform", "library", "write-better"])
    p.add_argument("--from", dest="source", required=True)
    p.add_argument("--ref", required=True)
    p.add_argument("--scan-only", action="store_true")
    p.parse_args()
    os.kill(os.getpid(), signal.SIGINT)
''')


def _write_sigint_engine(root: Path) -> Path:
    engine = root / "bin" / "publish-engine"
    engine.parent.mkdir(parents=True, exist_ok=True)
    engine.write_text(_SIGINT_ENGINE)
    engine.chmod(engine.stat().st_mode | stat.S_IEXEC)
    return root


def test_an_engine_killed_by_sigint_is_refused(tmp_path, gate_env, operator_config):
    """`os.kill(os.getpid(), signal.SIGINT)` makes the engine subprocess exit
    with returncode -2 — a death by signal, distinct from an ordinary exit(2)
    the test above covers, and `publish_scan` must refuse it the same way."""
    cfg = json.loads(operator_config.read_text())
    sigint_checkout = _shipwright_checkout_with(tmp_path / "sigint", _write_sigint_engine)
    cfg["shipwright_checkout"] = str(sigint_checkout)
    cfg_path = tmp_path / "sigint-operator.json"
    cfg_path.write_text(json.dumps(cfg))

    env = {**gate_env, "CONVIVY_OPERATOR_CONFIG": str(cfg_path)}
    repo, bare, base_sha = _repo_pair(tmp_path, env)
    result = _run("-C", str(repo), "push", "origin", "main", env=env)
    assert result.returncode != 0
    assert "push refused by the content gate" in result.stderr
    assert "exit -2" in result.stderr
    assert _remote_ref_sha(bare, gate_env) is None


# --- a missing names file ------------------------------------------------------

def test_a_missing_names_file_is_refused(tmp_path, gate_env, operator_config):
    cfg = json.loads(operator_config.read_text())
    cfg["builder_private_names_file"] = str(tmp_path / "no-such-private-names")
    cfg_path = tmp_path / "missing-names-operator.json"
    cfg_path.write_text(json.dumps(cfg))
    env = {**gate_env, "CONVIVY_OPERATOR_CONFIG": str(cfg_path)}
    repo, bare, base_sha = _repo_pair(tmp_path, env)
    result = _run("-C", str(repo), "push", "origin", "main", env=env)
    assert result.returncode != 0
    assert "push refused by the content gate" in result.stderr
    assert _remote_ref_sha(bare, gate_env) is None


# --- a repository publish-engine does not know --------------------------------

def test_a_repository_the_engine_does_not_know_is_refused(tmp_path, gate_env):
    repo, bare, base_sha = _repo_pair(tmp_path, gate_env, repo_name="sous-chef")
    result = _run("-C", str(repo), "push", "origin", "main", env=gate_env)
    assert result.returncode != 0
    assert "push refused by the content gate" in result.stderr
    assert "misconfigured" in result.stderr
    assert _remote_ref_sha(bare, gate_env) is None
