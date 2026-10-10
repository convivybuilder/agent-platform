"""dispatch_env — the shared TMUX/TMUX_PANE strip + private TMUX_TMPDIR for
any subprocess that starts a claude process or an agent run
(issue_01M2EG40BSYN7MXM2EWBT9VKBN).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import dispatch_env as DE


@pytest.fixture(autouse=True)
def _reset_private_tmpdir_singleton():
    """`_private_tmux_tmpdir()` caches one directory per process. Reset the
    cache around every test so each gets its own, and clean up anything a
    test's `dispatch_env()` call created (mirroring the `atexit` cleanup a
    real process gets, since tests never exit the interpreter)."""
    DE._tmux_tmpdir = None
    yield
    if DE._tmux_tmpdir is not None:
        import shutil
        shutil.rmtree(DE._tmux_tmpdir, ignore_errors=True)
        DE._tmux_tmpdir = None


@pytest.fixture(autouse=True)
def _reset_private_gh_config_dir_singleton():
    """Same contract as `_reset_private_tmpdir_singleton`, for `_private_gh_config_dir()`."""
    DE._gh_config_dir = None
    yield
    if DE._gh_config_dir is not None:
        import shutil
        shutil.rmtree(DE._gh_config_dir, ignore_errors=True)
        DE._gh_config_dir = None


def test_strips_tmux_and_tmux_pane_from_the_default_environment(monkeypatch):
    monkeypatch.setenv("TMUX", "/private/tmp/tmux-501/default,1314,3")
    monkeypatch.setenv("TMUX_PANE", "%3")
    monkeypatch.setenv("PATH", "/usr/bin")

    env = DE.dispatch_env()

    assert "TMUX" not in env
    assert "TMUX_PANE" not in env
    assert env["PATH"] == "/usr/bin"  # everything else still passes through


@pytest.mark.parametrize("deny_push", [False, True])
def test_strips_the_co_credential_path_so_no_spawned_run_inherits_it(monkeypatch, deny_push):
    """`spawn` builds the run's env from the calling process's os.environ through here, so a
    path exported in the calling shell would reach the run. bin/co exports the path nowhere,
    so only a hand export puts it there. The strip has to hold on both the coder path and the reviewer path,
    which passes deny_push=True."""
    monkeypatch.setenv("ORIENT_OPS_CO_TOKEN_FILE", "/tmp/co-credential")
    monkeypatch.setenv("PATH", "/usr/bin")

    env = DE.dispatch_env(deny_push=deny_push)

    assert "ORIENT_OPS_CO_TOKEN_FILE" not in env
    assert env["PATH"] == "/usr/bin"  # everything else still passes through


def test_strips_tmux_from_an_explicit_base_dict():
    base = {"TMUX": "/tmp/x,1,1", "TMUX_PANE": "%0", "PATH": "/x"}

    env = DE.dispatch_env(base)

    assert "TMUX" not in env
    assert "TMUX_PANE" not in env
    assert env["PATH"] == "/x"


def test_does_not_mutate_the_base_dict():
    base = {"TMUX": "/tmp/x,1,1", "PATH": "/x"}

    DE.dispatch_env(base)

    assert base == {"TMUX": "/tmp/x,1,1", "PATH": "/x"}


def test_no_op_when_tmux_is_already_absent(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin")

    env = DE.dispatch_env()

    assert "TMUX" not in env
    assert "TMUX_PANE" not in env
    assert env["PATH"] == "/usr/bin"


# --- TMUX_TMPDIR: closing the same-UID default-socket route ------------------
#
# Stripping TMUX/TMUX_PANE alone (the tests above) only closes tmux's
# inherited-pointer route to the dispatcher's live server. With TMUX and
# TMUX_TMPDIR both absent, tmux falls back to the UID-scoped default socket
# (/tmp/tmux-<uid>/default) — the socket every real co session runs on
# (security-reviewer, PR #567 round 1). These tests drive dispatch_env() with
# the parent's TMUX_TMPDIR unset (the real shape: co sessions never set it) and
# assert the child gets a private one instead, so an unqualified `tmux` in the
# child can never resolve that default socket.

def test_sets_tmux_tmpdir_when_parent_has_none(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_TMPDIR", raising=False)

    env = DE.dispatch_env()

    assert "TMUX_TMPDIR" in env
    assert os.path.isdir(env["TMUX_TMPDIR"])
    # not the bare /tmp a broken fallback would produce, and not tempfile's
    # default (macOS $TMPDIR, a long per-user path — see the module docstring)
    assert env["TMUX_TMPDIR"] != "/tmp"
    assert not env["TMUX_TMPDIR"].startswith("/var/folders")


def test_tmux_tmpdir_overrides_an_inherited_value():
    """A base dict that already carries a TMUX_TMPDIR (e.g. a nested
    dispatch_env() call inside a full-monty driver's own env) still gets the
    private directory, not the inherited value passed through unchanged —
    the resolved socket must never depend on what an ancestor process set."""
    base = {"TMUX_TMPDIR": "/some/inherited/path", "PATH": "/x"}

    env = DE.dispatch_env(base)

    assert env["TMUX_TMPDIR"] != "/some/inherited/path"
    assert os.path.isdir(env["TMUX_TMPDIR"])


def test_tmux_tmpdir_path_is_short_enough_for_a_unix_socket():
    """macOS caps sockaddr_un.sun_path at ~104 bytes and tmux appends
    "tmux-<uid>/default" to TMUX_TMPDIR — the exact regression this guards is
    a directory long enough to make that combined path unusable (measured:
    the run's own agent-runs dir came to 84 bytes with no headroom left for a
    longer $HOME or run-id)."""
    env = DE.dispatch_env()

    resolved_socket = env["TMUX_TMPDIR"] + f"/tmux-{os.getuid()}/default"
    assert len(resolved_socket) < 100


def test_tmux_tmpdir_is_reused_across_calls_in_the_same_process():
    """One per process, not one per call — every dispatch_env() call in a
    given process (a respawn/retry loop, or a long-lived full-monty driver
    launching several children) is dispatching on behalf of the same run."""
    first = DE.dispatch_env()["TMUX_TMPDIR"]
    second = DE.dispatch_env()["TMUX_TMPDIR"]

    assert first == second


def test_private_tmux_tmpdir_registers_atexit_cleanup(monkeypatch):
    """The directory is removed when the process that created it exits —
    simulated here by calling the registered atexit callback directly rather
    than exiting the interpreter."""
    registered = []
    monkeypatch.setattr(DE.atexit, "register",
                        lambda fn, *a, **kw: registered.append((fn, a, kw)))

    path = DE._private_tmux_tmpdir()
    assert os.path.isdir(path)
    assert len(registered) == 1

    fn, args, kwargs = registered[0]
    fn(*args, **kwargs)

    assert not os.path.exists(path)


# --- cleanup_private_tmux_tmpdir: the explicit call for an os._exit() path ---
#
# atexit never fires on a path that calls os._exit() (the normal termination
# for both daemonized grandchildren in bin/agentctl, not just their
# SIGKILLed case — reviewer, PR #567 round 2). cleanup_private_tmux_tmpdir()
# is the function those sites call explicitly instead.

def test_cleanup_private_tmux_tmpdir_removes_a_created_directory():
    path = DE.dispatch_env()["TMUX_TMPDIR"]
    assert os.path.isdir(path)

    DE.cleanup_private_tmux_tmpdir()

    assert not os.path.exists(path)


def test_cleanup_private_tmux_tmpdir_is_a_no_op_when_nothing_was_created():
    """No dispatch_env() call happened in this process (the fixture above
    resets the singleton to None) — cleanup must not raise or create
    anything just by being called."""
    assert DE._tmux_tmpdir is None

    DE.cleanup_private_tmux_tmpdir()  # must not raise

    assert DE._tmux_tmpdir is None


def test_cleanup_private_tmux_tmpdir_is_idempotent():
    path = DE.dispatch_env()["TMUX_TMPDIR"]

    DE.cleanup_private_tmux_tmpdir()
    DE.cleanup_private_tmux_tmpdir()  # must not raise the second time either

    assert not os.path.exists(path)
    assert DE._tmux_tmpdir is None


def test_cleanup_private_tmux_tmpdir_never_raises_on_an_already_removed_directory():
    """The directory could already be gone by the time cleanup runs (e.g. an
    operator manually cleared /tmp) — cleanup must still not raise."""
    path = DE.dispatch_env()["TMUX_TMPDIR"]
    import shutil
    shutil.rmtree(path)

    DE.cleanup_private_tmux_tmpdir()  # must not raise

    assert DE._tmux_tmpdir is None


# --- deny_push: a reviewer/security-reviewer git that refuses to push to a
# network remote (issue_01M3MMJFKSE5ZCA5YNXCJ1YCAK) --------------------------
#
# On 2026-09-28 a reviewer's `cd` into a not-yet-created scratch directory
# failed, the shell stayed in the reviewer's own worktree, and the `git push`
# meant for a throwaway repo pushed a stray branch to the real
# convivy/agent-platform instead — twice, on PR #698. `deny_push=True`
# rewrites a push to a network URL prefix to a scheme with no registered git
# remote helper, so git refuses before it ever dials out — except to a
# remote configured with an explicit `remote.<name>.pushurl`, which git does
# not rewrite with `pushInsteadOf`.

def _git(*args, cwd, env=None, check=True):
    env = dict(env or os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")  # never hang on a credential prompt
    result = subprocess.run(["git", *args], cwd=str(cwd), env=env,
                            capture_output=True, text=True, timeout=10)
    if check:
        assert result.returncode == 0, f"git {args}: {result.stderr}"
    return result


def _repo_with_a_commit(tmp_path, name="src"):
    repo = tmp_path / name
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    _git("config", "commit.gpgsign", "false", cwd=repo)
    (repo / "f.txt").write_text("x\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "x", cwd=repo)
    return repo


def test_deny_push_sets_a_pushinsteadof_rewrite_for_every_network_prefix():
    env = DE.dispatch_env(deny_push=True)

    count = int(env["GIT_CONFIG_COUNT"])
    pairs = [(env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"]) for i in range(count)]
    key = f"url.{DE._PUSH_REFUSAL_BASE}.pushInsteadOf"
    values = {v for k, v in pairs if k == key}
    assert values == set(DE._PUSH_REFUSAL_PREFIXES)


def test_deny_push_false_adds_no_git_config_when_nothing_else_needs_it(monkeypatch):
    # Round 6 dropped the unconditional `_OBJECT_STORE_CONFIG`: with deny_push=False and
    # no object-store redirect requested, nothing needs GIT_CONFIG_*, so it stays absent
    # rather than being manufactured.
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)

    env = DE.dispatch_env()

    assert "GIT_CONFIG_COUNT" not in env


def test_deny_push_layers_onto_git_config_entries_already_in_base():
    base = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "user.name",
            "GIT_CONFIG_VALUE_0": "someone", "PATH": "/x"}

    env = DE.dispatch_env(base, deny_push=True)

    assert env["GIT_CONFIG_KEY_0"] == "user.name"
    assert env["GIT_CONFIG_VALUE_0"] == "someone"
    assert int(env["GIT_CONFIG_COUNT"]) == 1 + len(DE._PUSH_REFUSAL_PREFIXES)


# --- the object-store redirect: a dispatched run's own git writes new objects to a
# private store and reads the shared one only as an alternate (knowledge#793 round 6) --
#
# Rounds 3-5 each narrowed the shared `objects` directory's write grant by name
# (`objects/info`, an enumerated pack/index file, `objects/pack` wholesale) and each
# round's reviewer found the next name still open, down to a structural gap no name-based
# narrowing could close: an existing loose object could be deleted and recreated under the
# same hash-named path, since `rm` only needs write permission on its fanout directory.
# `run_sandbox` now denies `objects` wholesale; these two vars are the other side of that
# — where the run's own git writes instead, and how it still reads what the shared store
# already has.

def test_object_store_redirect_sets_git_object_directory_and_alternate():
    env = DE.dispatch_env(private_object_dir="/private/obj", shared_object_dir="/shared/obj")

    assert env["GIT_OBJECT_DIRECTORY"] == "/private/obj"
    assert env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] == "/shared/obj"


def test_object_store_redirect_is_absent_without_a_private_dir():
    # A dispatch with no git_dir at all (a non-repo agent) passes neither — nothing in
    # the run's own env should claim an object-store redirect that was never wired.
    env = DE.dispatch_env()

    assert "GIT_OBJECT_DIRECTORY" not in env
    assert "GIT_ALTERNATE_OBJECT_DIRECTORIES" not in env


def test_object_store_redirect_keeps_git_config_entries_already_in_base():
    base = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "user.name",
            "GIT_CONFIG_VALUE_0": "someone", "PATH": "/x"}

    env = DE.dispatch_env(base, private_object_dir="/private/obj", shared_object_dir="/shared/obj")

    assert env["GIT_CONFIG_KEY_0"] == "user.name"
    assert env["GIT_CONFIG_VALUE_0"] == "someone"
    assert env["GIT_CONFIG_COUNT"] == "1"  # unchanged — the redirect is plain env, not GIT_CONFIG_*


def test_object_store_redirect_keeps_a_real_fetch_past_the_default_unpack_limit_to_the_private_dir(tmp_path):
    """git's default `transfer.unpackLimit` is 100: a fetch that receives more objects than
    that writes them as a pack instead of unpacking them. Plain git, no sandbox: a fetch
    past that limit still lands entirely under the private directory (as a pack, which is
    fine — nothing but `import_private_objects` ever reads it there) and writes NOTHING
    under the shared store's `objects`. 150 commits, each touching its own file, puts the
    transfer well past 100 objects (a new blob, tree and commit per commit)."""
    src = tmp_path / "src"
    src.mkdir()
    _git("init", "-q", "-b", "main", cwd=src)
    for i in range(150):
        (src / f"f{i}.txt").write_text(f"{i}\n")
        _git("add", "-A", cwd=src)
        _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", str(i), cwd=src)

    dst = tmp_path / "dst"
    dst.mkdir()
    _git("init", "-q", "-b", "main", cwd=dst)
    _git("remote", "add", "origin", str(src), cwd=dst)
    shared = dst / ".git" / "objects"
    private = tmp_path / "private-objects"
    private.mkdir()
    before = sorted(shared.rglob("*"))
    env = DE.dispatch_env(private_object_dir=private, shared_object_dir=shared)

    result = _git("fetch", "origin", "main", cwd=dst, env=env, check=False)

    assert result.returncode == 0, result.stderr
    after = sorted(shared.rglob("*"))
    assert after == before, f"the fetch wrote under the shared objects dir: {set(after) - set(before)}"
    assert any(private.rglob("*")), "the fetch should have written something under the private dir"


def test_deny_push_refuses_an_https_push_without_contacting_the_network(tmp_path):
    """The functional check, in real git: a push to a URL that is never
    contacted (an https://github.com/ path that doesn't exist) must fail —
    and fail from the missing remote helper, not a DNS/TCP timeout, proving
    git refuses before it ever dials out. The 10s timeout in `_git` is the
    backstop: if the refusal doesn't engage, git tries the network for real
    and this test times out instead of hanging CI."""
    repo = _repo_with_a_commit(tmp_path)
    env = DE.dispatch_env(deny_push=True)

    result = _git("push",
                  "https://github.com/convivy/agentctl-test-must-never-be-contacted.git",
                  "HEAD:refs/heads/x", cwd=repo, env=env, check=False)

    assert result.returncode != 0
    assert "agentctl-reviewer-no-push" in result.stderr


def test_deny_push_refuses_a_scp_style_ssh_push(tmp_path):
    repo = _repo_with_a_commit(tmp_path)
    env = DE.dispatch_env(deny_push=True)

    result = _git("push",
                  "git@github.com:convivy/agentctl-test-must-never-be-contacted.git",
                  "HEAD:refs/heads/x", cwd=repo, env=env, check=False)

    assert result.returncode != 0
    assert "agentctl-reviewer-no-push" in result.stderr


def test_deny_push_leaves_a_local_bare_remote_pushable(tmp_path):
    """A local path starts with none of the rewritten network prefixes — the
    refusal must not reach a push that was never going to leave the machine."""
    repo = _repo_with_a_commit(tmp_path)
    bare = tmp_path / "bare.git"
    _git("init", "-q", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    env = DE.dispatch_env(deny_push=True)

    result = _git("push", str(bare), "HEAD:refs/heads/main", cwd=repo, env=env, check=False)

    assert result.returncode == 0, result.stderr


# --- background-task/timeout env: a dispatched run never backgrounds a
# command (issue_01M3N2CFM6D17GD7B5TWM323F3) -----------------------------
#
# A dispatched `claude -p` run whose Bash test command outlasts its Bash
# tool timeout gets that command moved to the background, ends its turn to
# wait on it, and never resumes — measured seven times. These two vars close
# it: CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1 keeps a long Bash call in the
# foreground instead of backgrounding it, and BASH_DEFAULT_TIMEOUT_MS=600000
# raises the timeout a Bash call gets when it passes none.

def test_background_task_env_present_for_a_coder_role(monkeypatch):
    # This test process is itself a dispatched run, so the ambient
    # environment already carries both vars — delete them first so a
    # pre-fix dispatch_env() that merely passes an inherited value through
    # can't make this pass for the wrong reason.
    monkeypatch.delenv("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS", raising=False)
    monkeypatch.delenv("BASH_DEFAULT_TIMEOUT_MS", raising=False)

    env = DE.dispatch_env(deny_push=False)

    assert env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"
    assert env["BASH_DEFAULT_TIMEOUT_MS"] == "600000"


def test_background_task_env_present_for_a_reviewer_role(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS", raising=False)
    monkeypatch.delenv("BASH_DEFAULT_TIMEOUT_MS", raising=False)

    env = DE.dispatch_env(deny_push=True)

    assert env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"
    assert env["BASH_DEFAULT_TIMEOUT_MS"] == "600000"


def test_background_task_env_overwrites_a_value_already_in_base():
    base = {"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "0",
            "BASH_DEFAULT_TIMEOUT_MS": "120000", "PATH": "/x"}

    env = DE.dispatch_env(base)

    assert env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"
    assert env["BASH_DEFAULT_TIMEOUT_MS"] == "600000"


def test_object_store_redirect_keeps_every_real_git_operation_off_the_shared_objects_dir(tmp_path):
    """Plain git, no sandbox: a commit, a rebase, a fetch past the default unpack limit,
    and a push from a linked worktree (with a sibling worktree present, off the same
    primary) all succeed under the object-store redirect, and NONE of them write under
    the shared git directory's `objects` — the property `run_sandbox`'s wholesale deny
    depends on actually being true of ordinary git, not just denied by the sandbox."""
    primary = tmp_path / "primary"
    bare = tmp_path / "origin.git"
    primary.mkdir()
    bare.mkdir()
    _git("init", "-q", "--bare", "-b", "main", cwd=bare)
    _git("init", "-q", "-b", "main", cwd=primary)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty",
        "-m", "base", cwd=primary)
    _git("remote", "add", "origin", str(bare), cwd=primary)
    _git("push", "-q", "origin", "main", cwd=primary)

    wt = tmp_path / "wt"
    _git("worktree", "add", "-q", "-b", "agent/redirect-check", str(wt), cwd=primary)
    sibling = tmp_path / "sibling"
    _git("worktree", "add", "-q", "-b", "sibling-branch", str(sibling), cwd=primary)

    common = Path(_git("rev-parse", "--path-format=absolute", "--git-common-dir",
                       cwd=wt).stdout.strip())
    shared = common / "objects"
    private = tmp_path / "private-objects"
    private.mkdir()
    env = DE.dispatch_env(private_object_dir=private, shared_object_dir=shared)
    before = sorted(shared.rglob("*"))

    (wt / "f.py").write_text("v1\n")
    _git("add", "-A", cwd=wt, env=env)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "c1", cwd=wt, env=env)
    (wt / "f.py").write_text("v2\n")
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-am", "c2", cwd=wt, env=env)
    rebase = _git("-c", "user.name=t", "-c", "user.email=t@t", "rebase", "-q", "main",
                  cwd=wt, env=env, check=False)
    assert rebase.returncode == 0, rebase.stderr

    # A fetch past git's default transfer.unpackLimit (100), from a SEPARATE clone (so it
    # is a genuine new-object transfer rather than content `wt` already has locally).
    bigfetch_src = tmp_path / "bigfetch-src"
    subprocess.run(["git", "clone", "-q", str(bare), str(bigfetch_src)], check=True)
    _git("checkout", "-q", "-b", "bigfetch", cwd=bigfetch_src)
    for i in range(150):
        (bigfetch_src / f"f{i}.txt").write_text(f"{i}\n")
        _git("add", "-A", cwd=bigfetch_src)
        _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", str(i),
            cwd=bigfetch_src)
    _git("push", "-q", "origin", "bigfetch", cwd=bigfetch_src)
    fetch = _git("fetch", "-q", "origin", "bigfetch", cwd=wt, env=env, check=False)
    assert fetch.returncode == 0, fetch.stderr

    push = _git("push", "-q", "origin", "HEAD:agent/redirect-check", cwd=wt, env=env, check=False)
    assert push.returncode == 0, push.stderr

    after = sorted(shared.rglob("*"))
    assert after == before, f"a git operation wrote under the shared objects dir: {set(after) - set(before)}"
    assert any(private.rglob("*")), "the operations above should have written something to the private dir"
    pushed = subprocess.run(["git", "--git-dir", str(bare), "log", "--oneline", "agent/redirect-check"],
                           capture_output=True, text=True, check=True).stdout
    assert "c2" in pushed and "c1" in pushed


def test_deny_push_leaves_fetch_from_a_local_remote_unaffected(tmp_path):
    """`pushInsteadOf` (never `insteadOf`) only rewrites the URL git resolves
    for a push — a clone/fetch under the same env must still reach a local
    remote unchanged."""
    repo = _repo_with_a_commit(tmp_path)
    bare = tmp_path / "bare2.git"
    _git("init", "-q", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    _git("push", "-q", str(bare), "HEAD:refs/heads/main", cwd=repo)
    clone = tmp_path / "clone"
    env = DE.dispatch_env(deny_push=True)

    result = _git("clone", "-q", str(bare), str(clone), cwd=tmp_path, env=env, check=False)

    assert result.returncode == 0, result.stderr


# --- the git-credential-helper route to the operator's gh token (CQ·418) ----------------
#
# On 2026-10-07 two security-reviewer runs ran `git credential fill`, including
# `-c credential.helper='!gh auth git-credential'`, and git's configured helper — `gh`,
# reading its keyring-stored token — printed the operator's live token into their
# transcripts. Every dispatch now unconditionally redirects GH_CONFIG_DIR, GIT_CONFIG_
# GLOBAL/SYSTEM and GIT_CONFIG_NOSYSTEM, and carries this run's own minted, read-only
# `github_token` as GH_TOKEN/GITHUB_TOKEN instead.

def test_git_config_redirect_is_present_by_default():
    env = DE.dispatch_env()

    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["GIT_CONFIG_SYSTEM"] == "/dev/null"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_git_config_redirect_overwrites_a_value_already_in_base():
    base = {"GIT_CONFIG_GLOBAL": "/some/operators/.gitconfig",
            "GIT_CONFIG_SYSTEM": "/some/operators/etc/gitconfig",
            "GIT_CONFIG_NOSYSTEM": "0", "GIT_TERMINAL_PROMPT": "1", "PATH": "/x"}

    env = DE.dispatch_env(base)

    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["GIT_CONFIG_SYSTEM"] == "/dev/null"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_git_config_redirect_hides_a_real_gitconfig_credential_helper(tmp_path):
    """Functional proof behind the env-var assertions above: a `credential.helper`
    configured in $HOME/.gitconfig — the exact shape of the operator's real
    ~/.gitconfig (`credential.https://github.com.helper = !gh auth git-credential`)
    — must not be visible to git once GIT_CONFIG_GLOBAL is redirected, even though
    $HOME still points at the directory that holds it."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".gitconfig").write_text(
        '[credential "https://github.com"]\n\thelper = !echo would-print-the-operators-token; exit 1\n')
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)

    env = DE.dispatch_env({**os.environ, "HOME": str(fake_home)})

    result = subprocess.run(
        ["git", "config", "--get", "credential.https://github.com.helper"],
        cwd=str(repo), env=env, capture_output=True, text=True, timeout=10)

    assert result.returncode != 0, (
        f"the fake $HOME/.gitconfig's credential helper was still visible: {result.stdout!r}")
    assert "would-print" not in result.stdout


def test_github_token_sets_gh_token_and_github_token():
    env = DE.dispatch_env(github_token="ghs_run_specific")

    assert env["GH_TOKEN"] == "ghs_run_specific"
    assert env["GITHUB_TOKEN"] == "ghs_run_specific"


def test_no_github_token_leaves_neither_var_set_even_when_base_carries_one():
    """Never falls back to a value already in *base* — there is no legitimate source
    for GH_TOKEN/GITHUB_TOKEN there today, and a dispatch that mints no token (the
    mint failed, or this is a non-GitHub test) must not let one through regardless."""
    base = {"GH_TOKEN": "operators-ambient-token", "GITHUB_TOKEN": "operators-ambient-token",
            "PATH": "/x"}

    env = DE.dispatch_env(base)

    assert "GH_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env


def test_github_token_overwrites_whatever_was_already_in_base():
    base = {"GH_TOKEN": "operators-ambient-token", "GITHUB_TOKEN": "operators-ambient-token",
            "PATH": "/x"}

    env = DE.dispatch_env(base, github_token="ghs_run_specific")

    assert env["GH_TOKEN"] == "ghs_run_specific"
    assert env["GITHUB_TOKEN"] == "ghs_run_specific"


def test_gh_config_dir_is_a_private_empty_directory():
    env = DE.dispatch_env()

    gh_config_dir = Path(env["GH_CONFIG_DIR"])
    assert gh_config_dir.is_dir()
    assert list(gh_config_dir.iterdir()) == []  # no hosts.yml of its own


def test_gh_config_dir_overrides_an_inherited_value():
    base = {"GH_CONFIG_DIR": "/some/inherited/path", "PATH": "/x"}

    env = DE.dispatch_env(base)

    assert env["GH_CONFIG_DIR"] != "/some/inherited/path"
    assert os.path.isdir(env["GH_CONFIG_DIR"])


def test_gh_config_dir_is_reused_across_calls_in_the_same_process():
    first = DE.dispatch_env()["GH_CONFIG_DIR"]
    second = DE.dispatch_env()["GH_CONFIG_DIR"]

    assert first == second


def test_cleanup_private_gh_config_dir_removes_a_created_directory():
    path = DE.dispatch_env()["GH_CONFIG_DIR"]
    assert os.path.isdir(path)

    DE.cleanup_private_gh_config_dir()

    assert not os.path.exists(path)


def test_cleanup_private_gh_config_dir_is_a_no_op_when_nothing_was_created():
    assert DE._gh_config_dir is None

    DE.cleanup_private_gh_config_dir()  # must not raise

    assert DE._gh_config_dir is None


def test_cleanup_private_gh_config_dir_is_idempotent():
    path = DE.dispatch_env()["GH_CONFIG_DIR"]

    DE.cleanup_private_gh_config_dir()
    DE.cleanup_private_gh_config_dir()  # must not raise the second time either

    assert not os.path.exists(path)
    assert DE._gh_config_dir is None
