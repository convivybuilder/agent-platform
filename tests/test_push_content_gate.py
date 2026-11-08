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


def _merge(repo: Path, env: dict, *args: str) -> sp.CompletedProcess:
    return _run("-C", str(repo), "-c", "user.email=t@t.example", "-c", "user.name=t",
                "merge", *args, env=env)


def _init_repo(repo: Path, env: dict, branch: str = "main") -> None:
    assert _run("init", "-q", "-b", branch, str(repo), env=env).returncode == 0


def _init_bare(path: Path, env: dict, branch: str = "main") -> None:
    assert _run("init", "-q", "--bare", "-b", branch, str(path), env=env).returncode == 0


def _install_hook(repo: Path, repo_name: str, gate: Path = GATE) -> None:
    """A `pre-push` hook shaped exactly like the stub a later PR installs:
    prepend the clone's own repository name, pass everything else straight
    through (git's own `$1`/`$2` and stdin). `gate` defaults to this
    checkout's own `bin/push-content-gate`."""
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "pre-push"
    hook.write_text(f'#!/bin/sh\nexec "{gate}" "{repo_name}" "$@"\n')
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
    exercised here too, never stubbed out.

    `origin` is a real remote, added pointing at the checkout itself, with
    `refs/heads/main` set to the same commit as `refs/remotes/origin/main`
    below — `_export_shipwright_tree` fetches `origin`'s `main` before
    archiving, so this needs a real `origin` remote with a real `main`
    branch to fetch from, even though fetching a checkout from itself
    changes nothing it already has."""
    checkout = root / "shipwright"
    sp.run(["git", "init", "-q", "-b", "trunk", str(checkout)], check=True)
    write_engine(checkout)
    sp.run(["git", "-C", str(checkout), "add", "-A"], check=True)
    sp.run(["git", "-C", str(checkout), "-c", "user.email=t@t.example",
            "-c", "user.name=t", "commit", "-q", "-m", "engine"], check=True)
    sp.run(["git", "-C", str(checkout), "update-ref", "refs/remotes/origin/main", "HEAD"],
           check=True)
    sp.run(["git", "-C", str(checkout), "update-ref", "refs/heads/main", "HEAD"], check=True)
    sp.run(["git", "-C", str(checkout), "remote", "add", "origin", str(checkout)], check=True)
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
    pointed at this test's own fixture config, never the machine's real one."""
    env = dict(os.environ)
    env["HOME"] = _FAKE_HOME
    env["CONVIVY_OPERATOR_CONFIG"] = str(operator_config)
    return env


def _repo_pair(tmp_path, gate_env, repo_name="agent-platform", gate=GATE):
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
    _install_hook(repo, repo_name, gate)
    return repo, bare, base_sha


# --- a clean push ------------------------------------------------------------

def test_a_clean_push_succeeds_and_the_remote_gets_the_ref(tmp_path, gate_env):
    repo, bare, base_sha = _repo_pair(tmp_path, gate_env)
    result = _run("-C", str(repo), "push", "origin", "main", env=gate_env)
    assert result.returncode == 0, result.stderr
    assert _remote_ref_sha(bare, gate_env) == base_sha


# --- a refused finding --------------------------------------------------------

def test_a_marker_the_fake_engine_refuses_at_the_tip_is_refused(tmp_path, gate_env):
    repo, bare, base_sha = _repo_pair(tmp_path, gate_env)
    # `FAKE_ENGINE_FAIL_REF`, not the blanket `FAKE_ENGINE_FINDINGS`: the gate
    # now also scans this root commit's base (the empty tree, since it has no
    # parent), and a marker the engine reported at BOTH refs would read as
    # nothing new — tying it to `base_sha` alone is what keeps this a marker
    # the push actually adds.
    env = {**gate_env, "FAKE_ENGINE_FAIL_REF": base_sha}
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
    # scan_commits stops at the first refusal: bad_sha is scanned, and then its
    # own base (base_sha, its parent and the commit already on the remote) is
    # scanned too, to see whether the marker is new. good_sha is never reached.
    assert call_log.read_text().splitlines() == [bad_sha, base_sha]
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


# --- a push to an explicit URL (agentctl publish) resolves back to a --------
# configured remote, rather than being scanned as a brand-new, remote-less
# push every time -------------------------------------------------------------

def _url_repo_pair(tmp_path, gate_env, *, origin_url: str, push_url: str,
                    repo_name: str = "agent-platform"):
    """A working repo like `_repo_pair`'s, except `origin` is a GitHub remote
    named by URL rather than reachable over the network, with `main` ALREADY
    pushed, for real, to `real_bare` (standing in for a previous publish —
    the gate's own refresh-before-scan now re-fetches `refs/remotes/origin/*`
    from exactly this bare repo before every scan, so a merely-local
    `update-ref` claiming a history `real_bare` does not actually have would
    be pruned away rather than trusted), and a local `insteadOf` rewrite so a
    `git push <push_url> ...` call's real transport lands there too, while
    the `pre-push` hook still sees `push_url` itself as `$1` — exactly as git
    hands a real `pre-push` hook the URL a caller gave on the command line,
    never a rewritten one (confirmed against a real `git push` to an
    `insteadOf`-rewritten URL before this test was written)."""
    repo = tmp_path / "work"
    real_bare = tmp_path / "real-bare.git"
    _init_repo(repo, gate_env)
    base_sha = _commit(repo, gate_env, "base")
    _init_bare(real_bare, gate_env)
    assert _run("-C", str(repo), "push", f"file://{real_bare}", "main",
                env=gate_env).returncode == 0
    assert _run("-C", str(repo), "remote", "add", "origin", origin_url,
                env=gate_env).returncode == 0
    assert _run("-C", str(repo), "config", f"url.file://{real_bare}.insteadOf", push_url,
                env=gate_env).returncode == 0
    _install_hook(repo, repo_name)
    return repo, real_bare, base_sha


def test_a_new_branch_pushed_to_a_url_scans_only_commits_not_on_the_matching_remote(
        tmp_path, gate_env):
    """`agentctl publish` pushes a new branch to an explicit GitHub URL, not
    to `origin` by name, while `origin`'s own URL names the same repository
    in SSH form. The gate must resolve the URL back to `origin` and bound its
    scan to `refs/remotes/origin/*`, exactly as a push to `origin` by name
    would. A marker the fake engine refuses lives only in `base_sha`, already
    on `refs/remotes/origin/main`: this fails before the fix (the gate scans
    the whole history, reaches `base_sha`, and is refused) and passes after
    it (the gate scans only `new_sha`)."""
    push_url = "https://github.com/acme/widgets.git"
    repo, _real_bare, base_sha = _url_repo_pair(
        tmp_path, gate_env, origin_url="git@github.com:acme/widgets.git", push_url=push_url,
    )
    checkout = _run("-C", str(repo), "checkout", "-q", "-b", "feature", env=gate_env)
    assert checkout.returncode == 0, checkout.stderr
    new_sha = _commit(repo, gate_env, "feature work", content="feature\n")

    call_log = tmp_path / "url-feature-calls.log"
    env = {**gate_env, "FAKE_ENGINE_CALL_LOG": str(call_log),
           "FAKE_ENGINE_FAIL_REF": base_sha}
    pushed = _run("-C", str(repo), "push", push_url, "feature", env=env)
    assert pushed.returncode == 0, pushed.stderr
    assert call_log.read_text().splitlines() == [new_sha]


def test_a_new_branch_pushed_to_a_url_matches_origin_regardless_of_case_or_a_missing_dot_git(
        tmp_path, gate_env):
    """`origin` here is `https://GitHub.com/Acme/Widgets` — mixed case and no
    trailing `.git` — while the push goes to the lowercase, `.git`-suffixed
    form. The two must still be read as the same repository."""
    push_url = "https://github.com/acme/widgets.git"
    repo, _real_bare, base_sha = _url_repo_pair(
        tmp_path, gate_env, origin_url="https://GitHub.com/Acme/Widgets", push_url=push_url,
    )
    checkout = _run("-C", str(repo), "checkout", "-q", "-b", "feature", env=gate_env)
    assert checkout.returncode == 0, checkout.stderr
    new_sha = _commit(repo, gate_env, "feature work", content="feature\n")

    call_log = tmp_path / "url-feature-case-calls.log"
    env = {**gate_env, "FAKE_ENGINE_CALL_LOG": str(call_log)}
    pushed = _run("-C", str(repo), "push", push_url, "feature", env=env)
    assert pushed.returncode == 0, pushed.stderr
    assert call_log.read_text().splitlines() == [new_sha]
    assert base_sha not in call_log.read_text()


def test_a_url_naming_a_different_repository_scans_the_whole_history(tmp_path, gate_env):
    """No configured remote names the same repository as `push_url`, so the
    gate cannot resolve it to one — today's fail-closed behaviour (scanning
    everything reachable from the pushed branch) must stand, rather than
    trusting `origin`'s unrelated refs just because none matched."""
    push_url = "https://github.com/acme/widgets.git"
    repo, _real_bare, base_sha = _url_repo_pair(
        tmp_path, gate_env, origin_url="git@github.com:other-owner/other-repo.git",
        push_url=push_url,
    )
    checkout = _run("-C", str(repo), "checkout", "-q", "-b", "feature", env=gate_env)
    assert checkout.returncode == 0, checkout.stderr
    new_sha = _commit(repo, gate_env, "feature work", content="feature\n")

    call_log = tmp_path / "url-mismatch-calls.log"
    env = {**gate_env, "FAKE_ENGINE_CALL_LOG": str(call_log)}
    pushed = _run("-C", str(repo), "push", push_url, "feature", env=env)
    assert pushed.returncode == 0, pushed.stderr
    assert call_log.read_text().splitlines() == [base_sha, new_sha]


def test_a_push_to_a_named_remote_still_scans_only_that_remotes_own_new_commits(
        tmp_path, gate_env):
    """A push to `origin` by name — never a URL — must scan exactly what it
    scanned before this fix: `_resolve_remote_name` is a no-op for a bare
    remote name, since it never parses as a repository URL."""
    repo, bare, base_sha = _repo_pair(tmp_path, gate_env)
    base_push = _run("-C", str(repo), "push", "origin", "main", env=gate_env)
    assert base_push.returncode == 0, base_push.stderr

    checkout = _run("-C", str(repo), "checkout", "-q", "-b", "feature", env=gate_env)
    assert checkout.returncode == 0, checkout.stderr
    new_sha = _commit(repo, gate_env, "feature work", content="feature\n")

    call_log = tmp_path / "origin-by-name-calls.log"
    env = {**gate_env, "FAKE_ENGINE_CALL_LOG": str(call_log)}
    pushed = _run("-C", str(repo), "push", "origin", "feature", env=env)
    assert pushed.returncode == 0, pushed.stderr
    assert call_log.read_text().splitlines() == [new_sha]
    assert base_sha not in call_log.read_text()
    assert _remote_ref_sha(bare, gate_env, "refs/heads/feature") == new_sha


# --- an UPDATE to an existing ref scans the same `--remotes=`/`--exclude=`
# --- lookup a brand-new ref already used, not the plain `remote_sha..local_sha`
# --- range it used before: a merge of `origin/main` into an already-pushed
# --- branch must not read every name `main` gained since the fork as added by
# --- the merge (the four 2026-10-23 occurrences this fixes) -----------------

def test_an_update_merging_in_a_name_already_on_main_passes(tmp_path, gate_env):
    """THE FIX. Before it, `_shas_for_line` scanned an update as
    `remote_sha..local_sha`, which includes every commit a merge of
    `origin/main` brings in — so a name `main` already carries, merged into
    an already-pushed branch, read as newly added by the merge. Confirmed by
    hand against `origin/main`'s own `bin/push-content-gate` (pre-fix),
    against this exact scenario: pushing the merge is refused there, printing

        push refused by the content gate: agent-platform at <main_marker_sha
        short> (1 of 2): publish-engine: agent-platform at <main_marker_sha>
        (deadbeef9) would be refused by a release. No value is printed; open
        each file to judge it.
        publish-engine found 1 thing(s) at <main_marker_sha short> that
        <feature_sha short> does not have (agent-platform); the push would
        add:
          tests/fixture.py:3: an email address
        nothing was sent

    treating `main_marker_sha` — already on `origin/main`, not something
    this push adds — as one of the push's two new commits. After the fix,
    the same push passes."""
    repo, bare, base_sha = _repo_pair(tmp_path, gate_env)
    base_push = _run("-C", str(repo), "push", "origin", "main", env=gate_env)
    assert base_push.returncode == 0, base_push.stderr

    checkout = _run("-C", str(repo), "checkout", "-q", "-b", "feature", env=gate_env)
    assert checkout.returncode == 0, checkout.stderr
    feature_sha = _commit(repo, gate_env, "feature work", filename="feature.txt",
                          content="feature\n")
    feature_push = _run("-C", str(repo), "push", "origin", "feature", env=gate_env)
    assert feature_push.returncode == 0, feature_push.stderr

    # `main` gains a commit the fake engine refuses, pushed for real so the
    # remote's own `origin/main` (not just this clone's local `main`) has it.
    # A different file than `feature`'s own commit touched, so the merge below
    # has nothing to conflict over.
    back_to_main = _run("-C", str(repo), "checkout", "-q", "main", env=gate_env)
    assert back_to_main.returncode == 0, back_to_main.stderr
    main_marker_sha = _commit(repo, gate_env, "adds a marker on main", filename="marker.txt",
                              content="marker\n")
    main_push = _run("-C", str(repo), "push", "origin", "main", env=gate_env)
    assert main_push.returncode == 0, main_push.stderr

    back_to_feature = _run("-C", str(repo), "checkout", "-q", "feature", env=gate_env)
    assert back_to_feature.returncode == 0, back_to_feature.stderr
    merge = _merge(repo, gate_env, "--no-edit", "main")
    assert merge.returncode == 0, merge.stderr
    merge_sha = _run("-C", str(repo), "rev-parse", "HEAD", env=gate_env).stdout.strip()

    call_log = tmp_path / "merge-calls.log"
    env = {**gate_env, "FAKE_ENGINE_CALL_LOG": str(call_log), "FAKE_ENGINE_FAIL_REF": main_marker_sha}
    pushed = _run("-C", str(repo), "push", "origin", "feature", env=env)
    assert pushed.returncode == 0, pushed.stderr
    # Only the merge commit itself is new to the remote; main_marker_sha,
    # already on origin/main, is excluded and never asked about at all.
    assert call_log.read_text().splitlines() == [merge_sha]
    assert feature_sha not in call_log.read_text()
    assert main_marker_sha not in call_log.read_text()
    assert _remote_ref_sha(bare, gate_env, "refs/heads/feature") == merge_sha


def test_an_update_with_one_new_clean_commit_on_an_already_pushed_branch_passes(
        tmp_path, gate_env):
    """A branch already on the remote, carrying a name from before the gate
    existed (agent-platform's own `origin/main` does, by the thousand), gets
    one new, clean commit on top — that commit adds nothing, so the push
    must pass, and the old commit must never be re-scanned."""
    repo, bare, base_sha = _repo_pair(tmp_path, gate_env)
    base_push = _run("-C", str(repo), "push", "origin", "main", env=gate_env)
    assert base_push.returncode == 0, base_push.stderr

    new_sha = _commit(repo, gate_env, "clean follow-up", content="clean\n")
    call_log = tmp_path / "follow-up-calls.log"
    env = {**gate_env, "FAKE_ENGINE_CALL_LOG": str(call_log), "FAKE_ENGINE_FAIL_REF": base_sha}
    pushed = _run("-C", str(repo), "push", "origin", "main", env=env)
    assert pushed.returncode == 0, pushed.stderr
    assert call_log.read_text().splitlines() == [new_sha]
    assert base_sha not in call_log.read_text()
    assert _remote_ref_sha(bare, gate_env) == new_sha


# --- a run's own remote-tracking ref cannot stand in for the remote ----------
# A dispatched run is granted write access to exactly one remote-tracking
# ref, `refs/remotes/origin/<its own branch>` (`run_sandbox.py`). These three
# cases cover that ref being used as a decoy: planted at an earlier commit
# holding a refused name so the `--remotes=origin` scan below reads it as
# already on the remote, for both ways `_shas_for_line` reaches that scan,
# plus a control proving the fix for it does not widen the scan generally.

def test_a_planted_tracking_ref_for_a_new_branch_is_not_trusted_as_already_scanned(
        tmp_path, gate_env):
    """`refs/remotes/origin/feature` is planted, by `git update-ref`, at an
    earlier commit on `feature` holding a name the fake engine refuses, with
    a later commit removing it — exactly what a run with write access to
    that one ref could do to its own branch. The push is a brand-new ref on
    the real remote (`remote_sha` all zero), so this exercises the first
    condition in `_shas_for_line`. Before the fix, the decoy makes the
    `--remotes=origin` exclusion read the early commit as already on the
    remote and the push succeeds, scanning only the later, clean commit;
    after the fix, the destination branch's own tracking ref is excluded
    from that exclusion and the push is refused."""
    push_url = "https://github.com/acme/widgets.git"
    repo, real_bare, _base_sha = _url_repo_pair(
        tmp_path, gate_env, origin_url="git@github.com:acme/widgets.git", push_url=push_url,
    )
    checkout = _run("-C", str(repo), "checkout", "-q", "-b", "feature", env=gate_env)
    assert checkout.returncode == 0, checkout.stderr
    bad_sha = _commit(repo, gate_env, "adds a marker", content="marker\n")
    _good_sha = _commit(repo, gate_env, "removes the marker", content="clean again\n")
    assert _run("-C", str(repo), "update-ref", "refs/remotes/origin/feature", bad_sha,
                env=gate_env).returncode == 0

    env = {**gate_env, "FAKE_ENGINE_FAIL_REF": bad_sha}
    result = _run("-C", str(repo), "push", push_url, "feature", env=env)
    assert result.returncode != 0
    assert bad_sha[:9] in result.stderr
    assert _remote_ref_sha(real_bare, gate_env, "refs/heads/feature") is None


def test_a_planted_tracking_ref_under_a_different_name_is_not_trusted_as_already_scanned(
        tmp_path, gate_env):
    """THE LIVE GAP (security review, PR #1002 round 1): `--exclude=` keeps
    only the push's OWN destination name out of the `--remotes=` sweep; a
    decoy planted at any OTHER name — exactly the write access a DIFFERENT
    dispatched run's own branch gives it under `run_sandbox.py`, since that
    access is keyed by ref PATH, not by which run is pushing — still reads
    as already-on-the-remote history, because nothing refreshes
    refs/remotes/<name>/* from the real remote before this scan runs.
    Before the fix: the decoy survives, `bad_sha` is excluded, and the push
    succeeds. After it: the hook's own prune-fetch, over `remote_url` (the
    exact URL git resolved for this push, git's own `pre-push` `$2`), makes
    refs/remotes/origin/* match the real remote first, deleting the decoy
    (the real remote has no `some-other-branch` ref at all), so `bad_sha`
    is reached and refused."""
    push_url = "https://github.com/acme/widgets.git"
    repo, real_bare, _base_sha = _url_repo_pair(
        tmp_path, gate_env, origin_url="git@github.com:acme/widgets.git", push_url=push_url,
    )
    checkout = _run("-C", str(repo), "checkout", "-q", "-b", "feature", env=gate_env)
    assert checkout.returncode == 0, checkout.stderr
    bad_sha = _commit(repo, gate_env, "adds a marker", content="marker\n")
    _good_sha = _commit(repo, gate_env, "removes the marker", content="clean again\n")
    # The decoy, under a name that is NOT the pushed branch's own.
    assert _run("-C", str(repo), "update-ref", "refs/remotes/origin/some-other-branch", bad_sha,
                env=gate_env).returncode == 0

    env = {**gate_env, "FAKE_ENGINE_FAIL_REF": bad_sha}
    result = _run("-C", str(repo), "push", push_url, "feature", env=env)
    assert result.returncode != 0, (
        "the decoy under a different name must not hide bad_sha from the scan")
    assert bad_sha[:9] in result.stderr
    assert _remote_ref_sha(real_bare, gate_env, "refs/heads/feature") is None


def test_a_planted_symbolic_head_is_not_trusted_as_already_scanned(
        tmp_path, gate_env):
    """THE ROUND-2 GAP (security-reviewer): `--remotes=origin` globs in
    `refs/remotes/origin/HEAD` like any other single-segment name under
    that namespace, and `git fetch --prune` — confirmed by hand against a
    real fetch, not just read from the refspec — never removes a SYMBOLIC
    ref there, only a plain one (a plain decoy at this exact path is
    already pruned by the hook's own existing prune-fetch, same as the
    `some-other-branch` decoy above; a SYMBOLIC one survives it
    untouched, no matter how many times the fetch runs). The decoy here
    is `refs/remotes/origin/HEAD` symlinked at a local tag — tags are
    outside the fetch refspec's `refs/heads/*:refs/remotes/origin/*`
    namespace entirely, so the tag's own value survives the refresh too —
    holding `bad_sha`. Before the fix, `--remotes=origin` dereferences the
    symref to the tag to `bad_sha` and excludes it as already known; after
    it, `commits_new_to_remote` excludes `<remote_name>/HEAD` itself (by
    ref name, not by what it resolves to) from that sweep, so the decoy is
    never consulted and `bad_sha` is reached."""
    push_url = "https://github.com/acme/widgets.git"
    repo, real_bare, _base_sha = _url_repo_pair(
        tmp_path, gate_env, origin_url="git@github.com:acme/widgets.git", push_url=push_url,
    )
    checkout = _run("-C", str(repo), "checkout", "-q", "-b", "feature", env=gate_env)
    assert checkout.returncode == 0, checkout.stderr
    bad_sha = _commit(repo, gate_env, "adds a marker", content="marker\n")
    _good_sha = _commit(repo, gate_env, "removes the marker", content="clean again\n")
    # The decoy: a tag (never touched by any fetch over this refspec) holding
    # bad_sha, with refs/remotes/origin/HEAD symlinked at it — the one shape
    # that survives the refresh's own --prune untouched.
    assert _run("-C", str(repo), "tag", "decoy-target", bad_sha,
                env=gate_env).returncode == 0
    assert _run("-C", str(repo), "symbolic-ref", "refs/remotes/origin/HEAD",
                "refs/tags/decoy-target", env=gate_env).returncode == 0

    env = {**gate_env, "FAKE_ENGINE_FAIL_REF": bad_sha}
    result = _run("-C", str(repo), "push", push_url, "feature", env=env)
    assert result.returncode != 0, (
        "a decoy at refs/remotes/origin/HEAD must not hide bad_sha from the scan")
    assert bad_sha[:9] in result.stderr
    assert _remote_ref_sha(real_bare, gate_env, "refs/heads/feature") is None


def test_a_planted_symbolic_decoy_under_another_name_is_not_trusted_as_already_scanned(
        tmp_path, gate_env):
    """THE ROUND-3 GAP (reviewer, PR #1002 round 4): round 4's own fix
    excluded `refs/remotes/origin/HEAD` by name, but a SYMBOLIC ref under
    `refs/remotes/origin/*` survives `git fetch --prune` untouched
    regardless of its name (confirmed by hand against a real fetch — same
    mechanism as the `HEAD` case above), so a dispatched run can plant one
    under any OTHER name, `refs/remotes/origin/zzz-decoy` here, pointing at
    a local tag holding `bad_sha`. Fails on 98c89eee1: the hook's own
    prune-fetch leaves this decoy in place (it is symbolic, not plain, and
    `zzz-decoy` is not `HEAD`), `--remotes=origin` sweeps it in regardless
    of its name, and `bad_sha` is read as already known."""
    push_url = "https://github.com/acme/widgets.git"
    repo, real_bare, _base_sha = _url_repo_pair(
        tmp_path, gate_env, origin_url="git@github.com:acme/widgets.git", push_url=push_url,
    )
    checkout = _run("-C", str(repo), "checkout", "-q", "-b", "feature", env=gate_env)
    assert checkout.returncode == 0, checkout.stderr
    bad_sha = _commit(repo, gate_env, "adds a marker", content="marker\n")
    _good_sha = _commit(repo, gate_env, "removes the marker", content="clean again\n")
    # The decoy: a tag holding bad_sha, with refs/remotes/origin/zzz-decoy
    # symlinked at it — a name other than HEAD, the one shape that survives
    # the refresh's own --prune no matter what it is called.
    assert _run("-C", str(repo), "tag", "decoy-target", bad_sha,
                env=gate_env).returncode == 0
    assert _run("-C", str(repo), "symbolic-ref", "refs/remotes/origin/zzz-decoy",
                "refs/tags/decoy-target", env=gate_env).returncode == 0

    env = {**gate_env, "FAKE_ENGINE_FAIL_REF": bad_sha}
    result = _run("-C", str(repo), "push", push_url, "feature", env=env)
    assert result.returncode != 0, (
        "a decoy at refs/remotes/origin/zzz-decoy must not hide bad_sha from the scan")
    assert bad_sha[:9] in result.stderr
    assert _remote_ref_sha(real_bare, gate_env, "refs/heads/feature") is None


def test_a_planted_tracking_ref_for_an_unfetched_remote_sha_update_is_still_refused(
        tmp_path, gate_env):
    """The same decoy, but the pushed branch is an UPDATE to a ref that
    already exists on the real remote with a commit `repo` has never
    fetched and so does not hold the object for — a second clone pushes an
    unrelated `feature` branch directly to the real remote first. `repo`'s
    own `feature`, built independently, never shares history with that
    commit, so pushing it there is a force-push whose reported `remote_sha`
    this worktree cannot resolve: the second condition in
    `_shas_for_line`. The decoy at `refs/remotes/origin/feature` must still
    not hide the refused commit from the `--remotes=origin` scan."""
    push_url = "https://github.com/acme/widgets.git"
    repo, real_bare, _base_sha = _url_repo_pair(
        tmp_path, gate_env, origin_url="git@github.com:acme/widgets.git", push_url=push_url,
    )

    other = tmp_path / "other"
    assert _run("clone", "-q", str(real_bare), str(other), env=gate_env).returncode == 0
    assert _run("-C", str(other), "checkout", "-q", "-b", "feature",
                env=gate_env).returncode == 0
    other_sha = _commit(other, gate_env, "unrelated remote-side work", content="remote-side\n")
    assert _run("-C", str(other), "push", "-q", "origin", "feature",
                env=gate_env).returncode == 0

    checkout = _run("-C", str(repo), "checkout", "-q", "-b", "feature", env=gate_env)
    assert checkout.returncode == 0, checkout.stderr
    bad_sha = _commit(repo, gate_env, "adds a marker", content="marker\n")
    _good_sha = _commit(repo, gate_env, "removes the marker", content="clean again\n")
    assert _run("-C", str(repo), "update-ref", "refs/remotes/origin/feature", bad_sha,
                env=gate_env).returncode == 0

    env = {**gate_env, "FAKE_ENGINE_FAIL_REF": bad_sha}
    result = _run("-C", str(repo), "push", "--force", push_url, "feature", env=env)
    assert result.returncode != 0
    assert bad_sha[:9] in result.stderr
    assert _remote_ref_sha(real_bare, gate_env, "refs/heads/feature") == other_sha


def test_a_new_branch_with_clean_commits_still_excludes_the_remotes_other_history(
        tmp_path, gate_env):
    """Positive control for the fix above: the new `--exclude=` names only
    `<remote_name>/feature` — this push's own destination branch — so
    `refs/remotes/origin/main`, standing in for a previous publish, must
    still exclude its own history from the scan exactly as the round-1 tests
    already require. A marker living only in `base_sha` would refuse the
    push if the exclusion fix had widened the scan to include it."""
    push_url = "https://github.com/acme/widgets.git"
    repo, _real_bare, base_sha = _url_repo_pair(
        tmp_path, gate_env, origin_url="git@github.com:acme/widgets.git", push_url=push_url,
    )
    checkout = _run("-C", str(repo), "checkout", "-q", "-b", "feature", env=gate_env)
    assert checkout.returncode == 0, checkout.stderr
    new_sha = _commit(repo, gate_env, "feature work", content="feature\n")

    call_log = tmp_path / "url-feature-positive-calls.log"
    env = {**gate_env, "FAKE_ENGINE_CALL_LOG": str(call_log), "FAKE_ENGINE_FAIL_REF": base_sha}
    pushed = _run("-C", str(repo), "push", push_url, "feature", env=env)
    assert pushed.returncode == 0, pushed.stderr
    assert call_log.read_text().splitlines() == [new_sha]
    assert base_sha not in call_log.read_text()


def test_an_update_with_a_decoy_at_its_own_destination_name_still_scans_the_earlier_commit(
        tmp_path, gate_env):
    """The decoy, this time for an ORDINARY UPDATE with a resolvable
    `remote_sha`: `refs/remotes/origin/main`, the one tracking ref a
    dispatched run can write directly (`run_sandbox.py`), is planted at a
    commit that carries a marker and was never actually pushed — ahead of
    the real remote tip, which git's own `pre-push` protocol reports as
    `base_sha` regardless of what the local ref claims.

    NOT A REGRESSION TEST AGAINST A DEMONSTRATED PAST BUG (reviewer, PR
    #1002 round 1): on `origin/main`'s OLD code, an update with a
    resolvable `remote_sha` never consulted `--remotes=`/`--exclude=` at
    all — it scanned the plain `remote_sha..local_sha` range, which already
    included `marker_sha` regardless of what the decoy claimed, so this
    exact scenario passes identically on the old code and the new. What
    this guards is the NEW, unified `commits_new_to_remote`'s own
    `--exclude=` argument, going forward: on the current code, the decoy
    sits exactly where `--exclude=` must name it (`origin/main`, this
    push's own destination) for the marker commit to still be reached and
    refused — a future change that dropped or mis-scoped that argument
    would surface here, even though no such regression has happened yet."""
    repo, bare, base_sha = _repo_pair(tmp_path, gate_env)
    base_push = _run("-C", str(repo), "push", "origin", "main", env=gate_env)
    assert base_push.returncode == 0, base_push.stderr

    marker_sha = _commit(repo, gate_env, "adds a marker", content="marker\n")
    _commit(repo, gate_env, "clean follow-up", content="clean\n")

    # The decoy: planted at the push's own destination name, pointing at the
    # marker commit as though it were already known to the remote — which it
    # is not; the real remote is still at base_sha.
    assert _run("-C", str(repo), "update-ref", "refs/remotes/origin/main", marker_sha,
                env=gate_env).returncode == 0

    call_log = tmp_path / "ordinary-update-decoy-calls.log"
    env = {**gate_env, "FAKE_ENGINE_CALL_LOG": str(call_log), "FAKE_ENGINE_FAIL_REF": marker_sha}
    result = _run("-C", str(repo), "push", "origin", "main", env=env)
    assert result.returncode != 0
    assert "push refused by the content gate" in result.stderr
    assert marker_sha[:9] in result.stderr
    assert _remote_ref_sha(bare, gate_env) == base_sha  # unchanged by the refused push
    assert marker_sha in call_log.read_text().splitlines()


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
    import argparse, os, sys
    p = argparse.ArgumentParser(prog="publish-engine")
    p.add_argument("name", choices=["agent-platform", "library", "write-better"])
    p.add_argument("--from", dest="source", required=True)
    p.add_argument("--ref", required=True)
    p.add_argument("--scan-only", action="store_true")
    args = p.parse_args()
    # Tied to one specific ref (the test's own single pushed commit), not
    # printed unconditionally: the gate also scans that commit's base (the
    # empty tree, since it is a root commit), and a finding echoed at both
    # refs alike would read as nothing new.
    if args.ref != os.environ.get("TARGET_REF"):
        print(f"publish-engine: {args.name} at {args.ref} (deadbeef9): scanned 1 "
              f"file(s); symlinks, .example siblings, content scan, secret-shaped "
              f"keys, operator registry names: nothing found")
        sys.exit(0)
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

    repo, bare, base_sha = _repo_pair(tmp_path, {**gate_env, "CONVIVY_OPERATOR_CONFIG": str(cfg_path)})
    env = {**gate_env, "CONVIVY_OPERATOR_CONFIG": str(cfg_path), "TARGET_REF": base_sha}
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
    repo, bare, base_sha = _repo_pair(tmp_path, gate_env, repo_name="initech")
    result = _run("-C", str(repo), "push", "origin", "main", env=gate_env)
    assert result.returncode != 0
    assert "push refused by the content gate" in result.stderr
    assert "misconfigured" in result.stderr
    assert _remote_ref_sha(bare, gate_env) is None


