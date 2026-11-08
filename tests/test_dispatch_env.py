"""dispatch_env — the shared TMUX/TMUX_PANE strip + private TMUX_TMPDIR for
any subprocess that starts a claude process or an agent run
(issue_01M2EG40BSYN7MXM2EWBT9VKBN).
"""
from __future__ import annotations

import os
import shlex
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


@pytest.fixture(autouse=True)
def _reset_private_git_wrapper_dir_singleton():
    """Same contract as `_reset_private_tmpdir_singleton`, for `_private_git_wrapper_dir()`."""
    DE._git_wrapper_dir = None
    yield
    if DE._git_wrapper_dir is not None:
        import shutil
        shutil.rmtree(DE._git_wrapper_dir, ignore_errors=True)
        DE._git_wrapper_dir = None


@pytest.fixture(autouse=True)
def _reset_private_swift_wrapper_dir_singleton():
    """Same contract as `_reset_private_tmpdir_singleton`, for `_private_swift_wrapper_dir()`."""
    DE._swift_wrapper_dir = None
    yield
    if DE._swift_wrapper_dir is not None:
        import shutil
        shutil.rmtree(DE._swift_wrapper_dir, ignore_errors=True)
        DE._swift_wrapper_dir = None


def test_strips_tmux_and_tmux_pane_from_the_default_environment(monkeypatch):
    monkeypatch.setenv("TMUX", "/private/tmp/tmux-501/default,1314,3")
    monkeypatch.setenv("TMUX_PANE", "%3")
    monkeypatch.setenv("PATH", "/usr/bin")

    env = DE.dispatch_env()

    assert "TMUX" not in env
    assert "TMUX_PANE" not in env
    # everything else still passes through -- PATH gets the swift wrapper dir
    # prepended unconditionally (below), so check the tail rather than equality.
    assert env["PATH"].endswith(os.pathsep + "/usr/bin")


@pytest.mark.parametrize("deny_push", [False, True])
def test_strips_the_co_credential_path_so_no_spawned_run_inherits_it(monkeypatch, deny_push):
    """`spawn` builds the run's env from the calling process's os.environ through here, so a
    path exported in the calling shell would reach the run. bin/co exports the path nowhere,
    so only a hand export puts it there. The strip has to hold on both the coder path and the reviewer path,
    which passes deny_push=True."""
    monkeypatch.setenv("ORIENT_OPS_CREW_TOKEN_FILE", "/tmp/co-credential")
    monkeypatch.setenv("PATH", "/usr/bin")

    env = DE.dispatch_env(deny_push=deny_push)

    assert "ORIENT_OPS_CREW_TOKEN_FILE" not in env
    assert env["PATH"].endswith(os.pathsep + "/usr/bin")  # everything else still passes through


def test_strips_tmux_from_an_explicit_base_dict():
    base = {"TMUX": "/tmp/x,1,1", "TMUX_PANE": "%0", "PATH": "/x"}

    env = DE.dispatch_env(base)

    assert "TMUX" not in env
    assert "TMUX_PANE" not in env
    assert env["PATH"].endswith(os.pathsep + "/x")


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
    assert env["PATH"].endswith(os.pathsep + "/usr/bin")


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


# --- the redirect is process-wide, so it must be scoped to the run's own repository
# (issue_01M4QXDGVE43R2WYT01VDQQBPY) --------------------------------------------------

def _repo_with_a_commit_at(path, message):
    path.mkdir()
    _git("init", "-q", "-b", "main", cwd=path)
    _git("config", "user.email", "t@example.com", cwd=path)
    _git("config", "user.name", "t", cwd=path)
    # Git's detached auto-maintenance after a commit briefly holds objects/maintenance.lock, which races a test snapshotting this repo's objects directory.
    _git("config", "maintenance.auto", "false", cwd=path)
    _git("config", "gc.auto", "0", cwd=path)
    _git("commit", "-q", "--allow-empty", "-m", message, cwd=path)
    return path


def test_object_store_redirect_sets_the_wrapper_vars_and_path(tmp_path):
    private = tmp_path / "private-objects"
    private.mkdir()
    shared = tmp_path / "shared" / "objects"
    shared.mkdir(parents=True)

    env = DE.dispatch_env(private_object_dir=private, shared_object_dir=shared)

    assert env["AGENTCTL_OWN_GIT_COMMON_DIR"] == os.path.realpath(str(shared.parent))
    assert env["AGENTCTL_REAL_GIT"]
    assert Path(env["AGENTCTL_REAL_GIT"]).is_file()
    wrapper_dir = env["PATH"].split(os.pathsep)[0]
    assert (Path(wrapper_dir) / "git").is_file()
    assert os.access(Path(wrapper_dir) / "git", os.X_OK)


def test_object_store_redirect_wrapper_vars_absent_without_a_private_dir():
    env = DE.dispatch_env()

    assert "AGENTCTL_REAL_GIT" not in env
    assert "AGENTCTL_OWN_GIT_COMMON_DIR" not in env


def test_resolve_real_git_skips_an_inherited_wrapper_elsewhere_on_path(tmp_path):
    """route (b) of the 2026-10-12 20:52 outage: a `dispatch_env()` call made from
    INSIDE an already-dispatched run inherits an ANCESTOR process's wrapper directory
    already first on PATH, under a tmp path this call's own `_private_git_wrapper_dir()`
    never created, so a plain `entry != wrapper_dir` filter lets it through.
    `_resolve_real_git` must skip that directory too, by recognizing its `git` as a
    wrapper copy (the marker), never by running anything — this builds only a plain
    fake `git` script that prints a word, plus a separate file carrying the marker
    text, and never executes either as a wrapper."""
    inherited_wrapper_dir = tmp_path / "inherited-wrapper"
    inherited_wrapper_dir.mkdir()
    fake_wrapper = inherited_wrapper_dir / "git"
    fake_wrapper.write_text(f"#!/bin/bash\n# {DE._GIT_WRAPPER_MARKER}\necho should-never-run\n")
    fake_wrapper.chmod(0o755)

    real_dir = tmp_path / "real"
    real_dir.mkdir()
    fake_real_git = real_dir / "git"
    fake_real_git.write_text("#!/bin/bash\necho real-git\n")
    fake_real_git.chmod(0o755)

    path = os.pathsep.join([str(inherited_wrapper_dir), str(real_dir)])

    # wrapper_dir names a THIRD, unrelated directory — never the inherited one above —
    # so a pass here proves the marker-based skip did the work, not the plain equality
    # filter.
    resolved = DE._resolve_real_git(path, wrapper_dir=str(tmp_path / "some-other-wrapper-dir"))

    assert resolved == str(fake_real_git)


# --- OUTAGE, 2026-10-12: AGENTCTL_REAL_GIT resolved to a copy of the wrapper itself,
# whose probe (a command substitution) forked a child running the same script with the
# same self-referential env, which forked the next the same way — 9,256 bash processes
# between two runs before the host's per-user process limit stopped it. The wrapper now
# refuses, before any exec or probe, whenever AGENTCTL_REAL_GIT resolves to a copy of
# itself rather than the real git binary. These tests run only the FIXED script (never
# a reverted one — that revert is exactly the fork bomb above) and bound every run with
# subprocess's own timeout, which a passing run never approaches: the refusal fires on
# the very first invocation, before anything forks. -----------------------------------

def test_git_wrapper_script_carries_its_own_marker_line():
    """`_agentctl_is_wrapper_copy` in the script tells a second run's wrapper copy
    apart from the real git binary by this exact line — it has to stay present."""
    assert DE._GIT_WRAPPER_MARKER in DE._GIT_WRAPPER_SCRIPT


def _run_wrapper_against(tmp_path, wrapper_path, real_git):
    env = {**os.environ, "AGENTCTL_REAL_GIT": real_git,
           "AGENTCTL_OWN_GIT_COMMON_DIR": str(tmp_path),
           "GIT_OBJECT_DIRECTORY": str(tmp_path / "private"),
           "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(tmp_path / "shared")}
    return subprocess.run([str(wrapper_path), "-C", str(tmp_path), "rev-parse", "HEAD"],
                          capture_output=True, text=True, timeout=10, env=env)


def test_wrapper_refuses_when_agentctl_real_git_is_its_own_file(tmp_path):
    """The exact incident shape: AGENTCTL_REAL_GIT names this wrapper's own file."""
    wrapper = tmp_path / "git"
    wrapper.write_text(DE._GIT_WRAPPER_SCRIPT)
    wrapper.chmod(0o755)

    result = _run_wrapper_against(tmp_path, wrapper, str(wrapper))

    assert result.returncode == 127, (result.returncode, result.stdout, result.stderr)
    assert "AGENTCTL_REAL_GIT" in result.stderr


def test_wrapper_refuses_when_agentctl_real_git_is_a_different_copy_of_the_wrapper(tmp_path):
    """The $0 comparison alone would miss this: a SECOND run's wrapper is a different
    file. Only the marker line (shared, verbatim, across every copy of the script)
    catches it."""
    wrapper = tmp_path / "git"
    wrapper.write_text(DE._GIT_WRAPPER_SCRIPT)
    wrapper.chmod(0o755)
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    copy = other_dir / "git"
    copy.write_text(DE._GIT_WRAPPER_SCRIPT)
    copy.chmod(0o755)

    result = _run_wrapper_against(tmp_path, wrapper, str(copy))

    assert result.returncode == 127, (result.returncode, result.stdout, result.stderr)
    assert "AGENTCTL_REAL_GIT" in result.stderr


# --- OUTAGE, 2026-10-17 08:11-08:25 MST: a test in another repository took
# `shutil.which("git")` as "the real git" -- which, inside a dispatched run, resolved to
# THIS wrapper -- then ran a subprocess whose env held only PATH=<dir holding a git
# stub>. With AGENTCTL_REAL_GIT unset, the OLD script fell back to the bare name `git`
# and resolved IT through that PATH too, landing back on the stub, which called `git`
# again, forking the next generation the same way, until the operator's account hit its
# 10,122-of-10,666 process ceiling. The wrapper must never exec, probe, or resolve a bare
# `git` through PATH at all -- AGENTCTL_REAL_GIT unset now falls back to an absolute path
# baked into the script at write time, and the script refuses outright (before any exec
# or probe) when neither value is an absolute path to an executable file. These tests
# write the wrapper the same way `_private_git_wrapper_dir` does (`_write_wrapper`,
# substituting @@BAKED_REAL_GIT@@ exactly as production does) and build every stub that
# calls back into `git` with `_write_capped_git_stub`, which gives it its own hard cap, so
# a regression back to a PATH search can never grow this test's own process count without
# bound. A stub that only prints a fixed line and never calls `git` carries no such cap --
# it cannot recurse either way. --------------------------------------------------------

def _write_capped_git_stub(stub_dir, counter, cap=3):
    """Write a `git` stub at `stub_dir / "git"` that re-execs `git` on every call -- so it
    would recurse through PATH without bound if the wrapper regressed and ever entered it
    -- but refuses once `counter` (one line appended per call) reaches `cap`. Every test
    stub in this file that calls back into `git` must be built through this helper, so a
    future one can't be added without the cap."""
    stub = stub_dir / "git"
    stub.write_text(f"""#!/bin/bash
echo x >> {shlex.quote(str(counter))}
count=$(wc -l < {shlex.quote(str(counter))})
if [ "$count" -ge {cap} ]; then
    exit 99
fi
exec git "$@"
""")
    stub.chmod(0o755)
    return stub


def _write_wrapper(path, baked_real_git):
    """Write `_GIT_WRAPPER_SCRIPT` to `path` with `@@BAKED_REAL_GIT@@` substituted the
    same way `_private_git_wrapper_dir` substitutes it in production: shlex.quote()'d,
    so a path holding a space or a shell metacharacter round-trips as one literal
    value."""
    script = DE._GIT_WRAPPER_SCRIPT.replace("@@BAKED_REAL_GIT@@", shlex.quote(baked_real_git))
    path.write_text(script)
    path.chmod(0o755)


def _real_git_for_tests():
    """A real git binary's path, safe to use as a test's baked fallback even when this
    test process is itself running inside a dispatched run (this process's own ambient
    PATH then has a wrapper first): `_resolve_real_git` already skips any PATH entry
    whose `git` is a wrapper copy, the same way `dispatch_env()` itself resolves
    AGENTCTL_REAL_GIT, so it lands on the real binary either way."""
    return DE._resolve_real_git(os.environ.get("PATH", os.defpath), wrapper_dir="")


def test_wrapper_runs_the_baked_real_git_without_resolving_a_path_stub(tmp_path):
    """AGENTCTL_REAL_GIT unset, PATH pointing only at a stub that calls back into
    `git`: the wrapper must run its own baked real git and never enter the stub at
    all. The stub's own hard cap at its third entry (exit 99) is the test's safety net
    — it fires only if this regresses, and bounds the damage to 3 processes instead of
    an unbounded fork chain."""
    counter = tmp_path / "counter"
    stub_dir = tmp_path / "stubpath"
    stub_dir.mkdir()
    _write_capped_git_stub(stub_dir, counter)
    wrapper = tmp_path / "git-wrapper"
    _write_wrapper(wrapper, _real_git_for_tests())

    result = subprocess.run([str(wrapper), "--version"], env={"PATH": str(stub_dir)},
                            capture_output=True, text=True, timeout=10)

    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    assert "git version" in result.stdout
    assert not counter.exists() or counter.read_text() == "", \
        "the stub was entered -- the wrapper resolved a bare 'git' through PATH"


def test_wrapper_refuses_when_agentctl_real_git_unset_and_baked_value_empty(tmp_path):
    """No AGENTCTL_REAL_GIT and no usable baked fallback: the wrapper must refuse with
    exit 127 and a stderr line, and must never enter the PATH stub at all."""
    counter = tmp_path / "counter"
    stub_dir = tmp_path / "stubpath"
    stub_dir.mkdir()
    _write_capped_git_stub(stub_dir, counter)
    wrapper = tmp_path / "git-wrapper"
    _write_wrapper(wrapper, "")

    result = subprocess.run([str(wrapper), "--version"], env={"PATH": str(stub_dir)},
                            capture_output=True, text=True, timeout=10)

    assert result.returncode == 127, (result.returncode, result.stdout, result.stderr)
    assert result.stderr.strip() != ""
    assert not counter.exists(), \
        "the stub was entered -- the wrapper resolved a bare 'git' through PATH"


def test_resolve_real_git_returns_nothing_usable_when_path_has_no_real_git(tmp_path):
    """Must never return the bare name `"git"` — that name would have to be resolved
    through $PATH by something to be of any use, which is the exact lookup this fix
    removes."""
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    resolved = DE._resolve_real_git(str(empty_dir), wrapper_dir="/does/not/matter")

    assert resolved in ("", None)
    assert resolved != "git"


def test_baked_real_git_path_with_space_and_dollar_round_trips_intact(tmp_path):
    """A baked path holding a space and a `$` must reach the wrapper's `real_git`
    variable as one literal value, never shell-expanded or split -- a fake "real git"
    that only prints a fixed line proves it ran, rather than failing some other way."""
    weird_dir = tmp_path / "weird path with $dollar"
    weird_dir.mkdir()
    fake_real_git = weird_dir / "git"
    fake_real_git.write_text("#!/bin/bash\necho weird-real-git-ran\n")
    fake_real_git.chmod(0o755)
    wrapper = tmp_path / "git-wrapper"
    _write_wrapper(wrapper, str(fake_real_git))

    result = subprocess.run([str(wrapper), "--version"], env={"PATH": "/usr/bin:/bin"},
                            capture_output=True, text=True, timeout=10)

    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    assert result.stdout.strip() == "weird-real-git-ran"


def test_object_store_redirect_lets_a_different_repositorys_own_git_read_its_own_objects(tmp_path):
    """The defect this closes, reproduced in plain git with no sandbox and no spawned
    run: `dispatch_env`'s own GIT_OBJECT_DIRECTORY / GIT_ALTERNATE_OBJECT_DIRECTORIES
    are process-wide, so before this fix, `git -C <another repository>` read ONLY from
    repository A's private/shared stores and reported A's own commit as a 'bad object'
    for repository B, even though B has that object in its own, real store. This test
    fails on origin/main (confirmed: `fatal: git cat-file: could not get object info`,
    the exact failure the occurrence's report names) and passes with the wrapper."""
    a = _repo_with_a_commit_at(tmp_path / "a", "commit in A")
    b = _repo_with_a_commit_at(tmp_path / "b", "commit in B")
    private = tmp_path / "private-objects"
    private.mkdir()
    shared = a / ".git" / "objects"
    env = DE.dispatch_env(private_object_dir=private, shared_object_dir=shared)
    b_head = _git("rev-parse", "HEAD", cwd=b).stdout.strip()

    result = _git("-C", str(b), "cat-file", "-t", b_head, cwd=tmp_path, env=env, check=False)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "commit", (result.stdout, result.stderr)


def test_object_store_redirect_still_redirects_the_runs_own_repository(tmp_path):
    """The converse the wrapper must not break: a commit made against the run's OWN
    repository (A) under the same env still lands in A's private object store, never
    in A's own shared `objects` directory — exactly `test_object_store_redirect_keeps_a_
    real_fetch_past_the_default_unpack_limit_to_the_private_dir`'s property, now checked
    with the wrapper on PATH too."""
    a = _repo_with_a_commit_at(tmp_path / "a", "commit in A")
    private = tmp_path / "private-objects"
    private.mkdir()
    shared = a / ".git" / "objects"
    env = DE.dispatch_env(private_object_dir=private, shared_object_dir=shared)
    before_shared = sorted(shared.rglob("*"))

    result = _git("-C", str(a), "-c", "user.name=t", "-c", "user.email=t@t",
                  "commit", "-q", "--allow-empty", "-m", "second commit",
                  cwd=tmp_path, env=env, check=False)

    assert result.returncode == 0, result.stderr
    after_shared = sorted(shared.rglob("*"))
    assert after_shared == before_shared, (
        f"the own-repository commit wrote under the shared objects dir: "
        f"{set(after_shared) - set(before_shared)}")
    assert any(private.rglob("*")), "the own-repository commit should have written to the private dir"


def test_a_runs_git_init_bare_creates_a_usable_repository_and_can_be_pushed_to(tmp_path):
    """issue_01M4R8KTY7THR8HYGJA849N3AH. `git init -q --bare <path>`, issued from inside
    the run's own worktree (no -C/--git-dir of its own — the shape the live confinement
    test actually runs), creates a repository that doesn't exist yet, so the wrapper's
    run's-own-repository probe can't resolve it. Before this fix, the redirect was left
    in place, the new repository's objects landed in the run's private store instead of
    `<path>/objects`, and a push to it failed. Fails on origin/main before this fix
    (confirmed: `remote.git/objects` absent, and the push's `fatal: ... does not appear
    to be a git repository`)."""
    a = _repo_with_a_commit_at(tmp_path / "a", "commit in A")
    private = tmp_path / "private-objects"
    private.mkdir()
    shared = a / ".git" / "objects"
    env = DE.dispatch_env(private_object_dir=private, shared_object_dir=shared)
    remote = tmp_path / "remote.git"

    init_result = _git("init", "-q", "--bare", str(remote), cwd=a, env=env, check=False)
    assert init_result.returncode == 0, init_result.stderr
    assert (remote / "objects").is_dir(), "the new bare repo has no objects directory of its own"

    push_result = _git("push", "-q", str(remote), "HEAD:refs/heads/sandbox-check",
                       cwd=a, env=env, check=False)
    assert push_result.returncode == 0, push_result.stderr
    show_ref = _git("--git-dir", str(remote), "show-ref", cwd=tmp_path)
    assert "refs/heads/sandbox-check" in show_ref.stdout, show_ref.stdout


def test_a_runs_git_clone_creates_a_usable_checkout(tmp_path):
    """Same gap, the other direction. `git clone <src> <dst>`, issued from inside the
    run's own worktree, creates a destination repository that doesn't exist yet either.
    Fails on origin/main before this fix (confirmed: the destination's `.git/objects` is
    empty and `git log` there reports no commits)."""
    a = _repo_with_a_commit_at(tmp_path / "a", "commit in A")
    b = _repo_with_a_commit_at(tmp_path / "b", "commit in B")
    private = tmp_path / "private-objects"
    private.mkdir()
    shared = a / ".git" / "objects"
    env = DE.dispatch_env(private_object_dir=private, shared_object_dir=shared)
    dest = tmp_path / "clone-dest"

    clone_result = _git("clone", "-q", str(b), str(dest), cwd=a, env=env, check=False)
    assert clone_result.returncode == 0, clone_result.stderr
    assert any((dest / ".git" / "objects").rglob("*")), "the clone's own objects directory is empty"

    log = _git("log", "--format=%s", "-1", cwd=dest, env=env)
    assert log.stdout.strip() == "commit in B"


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


# --- sixth gap: no git identity resolves for the operator's account -------------

def test_identity_resolves_true_when_git_var_succeeds(monkeypatch):
    monkeypatch.setattr(DE.subprocess, "run",
                        lambda *a, **kw: subprocess.CompletedProcess(a, 0))
    assert DE._git_identity_resolves({}, None) is True


def test_identity_resolves_false_when_git_var_fails(monkeypatch):
    monkeypatch.setattr(DE.subprocess, "run",
                        lambda *a, **kw: subprocess.CompletedProcess(a, 128))
    assert DE._git_identity_resolves({}, None) is False


def test_identity_resolves_false_when_git_itself_cannot_be_run(monkeypatch):
    def _raise(*a, **kw):
        raise OSError("no such file or directory: git")
    monkeypatch.setattr(DE.subprocess, "run", _raise)
    assert DE._git_identity_resolves({}, None) is False


def test_dispatch_env_sets_the_fallback_identity_when_none_resolves(tmp_path):
    """Functional proof, not a mock: a repo whose own `user.useConfigOnly` disables
    git's GECOS/hostname auto-identity fallback reproduces, without needing a bare
    passwd entry or a scrubbed $HOME, the exact "no identity anywhere" shape
    measured on the adopter's Linux account. `dispatch_env` must fill in all four
    identity vars, and a real commit under the returned env must then succeed."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.useConfigOnly", "true", cwd=repo)

    env = DE.dispatch_env({**os.environ, "PATH": os.environ["PATH"]}, worktree=repo)

    assert env["GIT_AUTHOR_NAME"] == DE._FALLBACK_COMMIT_NAME
    assert env["GIT_AUTHOR_EMAIL"] == DE._FALLBACK_COMMIT_EMAIL
    assert env["GIT_COMMITTER_NAME"] == DE._FALLBACK_COMMIT_NAME
    assert env["GIT_COMMITTER_EMAIL"] == DE._FALLBACK_COMMIT_EMAIL

    (repo / "f.txt").write_text("x")
    _git("add", "f.txt", cwd=repo, env=env)
    commit = subprocess.run(["git", "commit", "-q", "-m", "x"], cwd=str(repo), env=env,
                            capture_output=True, text=True, timeout=10)
    assert commit.returncode == 0, commit.stderr


def test_dispatch_env_leaves_a_configured_identity_unchanged(tmp_path):
    """A repo with its own local `user.name`/`user.email` resolves the probe on its
    own — `dispatch_env` adds no override, the same "a configured identity always
    wins" contract `_kb_commit` (bin/agentctl) already holds for a kb commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.name", "Someone Else", cwd=repo)
    _git("config", "user.email", "someone@example.com", cwd=repo)

    env = DE.dispatch_env({**os.environ, "PATH": os.environ["PATH"]}, worktree=repo)

    for key in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        assert key not in env


def test_dispatch_env_without_a_worktree_probes_with_cwd_none(monkeypatch):
    """No `worktree` (a non-repo agent, or a caller that has none to give) probes
    with `cwd=None` rather than skipping the probe outright, and still sets the
    fallback when that probe fails."""
    seen_cwd = []

    def _fake(env, cwd):
        seen_cwd.append(cwd)
        return False
    monkeypatch.setattr(DE, "_git_identity_resolves", _fake)

    env = DE.dispatch_env()

    assert seen_cwd == [None]
    assert env["GIT_AUTHOR_NAME"] == DE._FALLBACK_COMMIT_NAME


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


# --- the swift wrapper: SwiftPM's own sandbox-exec around manifest/plugin
# compilation fails inside this run's own sandbox (2026-10-21, deck-coder
# run-ccc745b2) --------------------------------------------------------------
#
# `swift build`/`swift test` inside a dispatched run fails with
# "sandbox-exec: sandbox_apply: Operation not permitted" while compiling
# Package.swift — a sandbox can't be applied inside a sandbox. SwiftPM has no
# environment variable or config setting for this (measured 2026-10-21:
# SWIFTC_DISABLE_SANDBOX governs swiftc's own sandboxing, not the manifest/
# plugin sandbox, and has no effect on the failure). `--disable-sandbox` is
# the only thing that does. This wrapper, put first on every dispatched run's
# PATH by `dispatch_env()` unconditionally, adds that flag to the subcommands
# that compile a manifest (build/test/run/package) when the caller hasn't
# already passed it, and follows the git wrapper's exact
# never-resolve-a-bare-name-through-PATH discipline (the OUTAGE comments
# above `_GIT_WRAPPER_SCRIPT`).

def test_swift_wrapper_script_carries_its_own_marker_line():
    """`_agentctl_is_swift_wrapper_copy` in the script tells a second run's wrapper
    copy apart from the real swift binary by this exact line — it has to stay
    present."""
    assert DE._SWIFT_WRAPPER_MARKER in DE._SWIFT_WRAPPER_SCRIPT


def _write_fake_real_swift(path):
    """A minimal executable standing in for a real `swift` binary, for a test that only
    needs `_resolve_real_swift` to find SOMETHING on PATH and never actually runs it —
    a CI runner and most non-Xcode hosts have no real `swift` at all, so these tests
    must never depend on the host's own toolchain."""
    path.write_text("#!/bin/bash\nexit 0\n")
    path.chmod(0o755)


def test_swift_wrapper_is_first_on_path_and_contains_an_executable_swift(tmp_path, monkeypatch):
    real_swift_dir = tmp_path / "real-swift-bin"
    real_swift_dir.mkdir()
    fake_real_swift = real_swift_dir / "swift"
    _write_fake_real_swift(fake_real_swift)
    monkeypatch.setenv("PATH", str(real_swift_dir))

    env = DE.dispatch_env()

    assert env["AGENTCTL_REAL_SWIFT"] == str(fake_real_swift)
    assert Path(env["AGENTCTL_REAL_SWIFT"]).is_file()
    wrapper_dir = env["PATH"].split(os.pathsep)[0]
    assert (Path(wrapper_dir) / "swift").is_file()
    assert os.access(Path(wrapper_dir) / "swift", os.X_OK)


def test_swift_wrapper_present_unconditionally_even_with_the_object_store_redirect(tmp_path, monkeypatch):
    """The swift wrapper is wired independently of the git object-store redirect — a
    dispatch that also gets the git wrapper (private_object_dir/shared_object_dir both
    given) must still carry AGENTCTL_REAL_SWIFT and a swift wrapper on PATH, alongside
    the git wrapper."""
    private = tmp_path / "private-objects"
    private.mkdir()
    shared = tmp_path / "shared" / "objects"
    shared.mkdir(parents=True)
    real_swift_dir = tmp_path / "real-swift-bin"
    real_swift_dir.mkdir()
    _write_fake_real_swift(real_swift_dir / "swift")
    monkeypatch.setenv("PATH", str(real_swift_dir))

    env = DE.dispatch_env(private_object_dir=private, shared_object_dir=shared)

    assert env["AGENTCTL_REAL_SWIFT"]
    path_entries = env["PATH"].split(os.pathsep)
    assert (Path(path_entries[0]) / "git").is_file()
    assert (Path(path_entries[1]) / "swift").is_file()


def test_dispatch_env_on_a_host_with_no_swift_at_all_still_installs_the_wrapper_but_refuses(tmp_path, monkeypatch):
    """A host with no `swift` anywhere on PATH at all -- CI, and most non-Xcode hosts --
    must behave exactly as it does today: no AGENTCTL_REAL_SWIFT, and `swift` still
    fails. The wrapper is installed unconditionally regardless, so the failure comes
    from its own 127 refusal rather than a bare "command not found", but it is still a
    127, never a successful run of something unintended."""
    empty_path_dir = tmp_path / "no-swift-here"
    empty_path_dir.mkdir()
    monkeypatch.setenv("PATH", str(empty_path_dir))

    env = DE.dispatch_env()

    assert "AGENTCTL_REAL_SWIFT" not in env
    wrapper_dir = env["PATH"].split(os.pathsep)[0]
    assert (Path(wrapper_dir) / "swift").is_file()

    result = subprocess.run([str(Path(wrapper_dir) / "swift"), "--version"],
                            capture_output=True, text=True, timeout=10,
                            env={"PATH": str(empty_path_dir)})

    assert result.returncode == 127, (result.returncode, result.stdout, result.stderr)
    assert "AGENTCTL_REAL_SWIFT" in result.stderr


def test_swift_wrapper_dir_is_reused_across_calls_in_the_same_process():
    first = DE.dispatch_env()["PATH"].split(os.pathsep)[0]
    second = DE.dispatch_env()["PATH"].split(os.pathsep)[0]

    assert first == second


def test_cleanup_private_swift_wrapper_dir_removes_a_created_directory():
    DE.dispatch_env()
    path = DE._swift_wrapper_dir
    assert path is not None and os.path.isdir(path)

    DE.cleanup_private_swift_wrapper_dir()

    assert not os.path.exists(path)
    assert DE._swift_wrapper_dir is None


def test_cleanup_private_swift_wrapper_dir_is_a_no_op_when_nothing_was_created():
    assert DE._swift_wrapper_dir is None

    DE.cleanup_private_swift_wrapper_dir()  # must not raise

    assert DE._swift_wrapper_dir is None


def test_cleanup_private_swift_wrapper_dir_is_idempotent():
    DE.dispatch_env()
    path = DE._swift_wrapper_dir

    DE.cleanup_private_swift_wrapper_dir()
    DE.cleanup_private_swift_wrapper_dir()  # must not raise the second time either

    assert not os.path.exists(path)
    assert DE._swift_wrapper_dir is None


def _write_swift_wrapper(path, baked_real_swift):
    """Write `_SWIFT_WRAPPER_SCRIPT` to `path` with `@@BAKED_REAL_SWIFT@@`
    substituted the same way `_private_swift_wrapper_dir` substitutes it in
    production: shlex.quote()'d, so a path holding a space or a shell
    metacharacter round-trips as one literal value."""
    script = DE._SWIFT_WRAPPER_SCRIPT.replace("@@BAKED_REAL_SWIFT@@", shlex.quote(baked_real_swift))
    path.write_text(script)
    path.chmod(0o755)


def _write_argv_recording_swift_stub(path, out_file):
    """Write a `swift` stub at `path` that records its received argv (one argument
    per line, overwritten on every call) to `out_file` and exits 0. Never calls back
    into `swift`, so it can never recurse either way — safe to use as the wrapper's
    AGENTCTL_REAL_SWIFT target in every functional test below."""
    path.write_text(f"""#!/bin/bash
printf '%s\\n' "$@" > {shlex.quote(str(out_file))}
""")
    path.chmod(0o755)


@pytest.mark.parametrize("subcommand", ["build", "test", "run"])
def test_swift_wrapper_adds_disable_sandbox_for_manifest_compiling_subcommands(tmp_path, subcommand):
    out = tmp_path / "argv"
    stub = tmp_path / "real-swift"
    _write_argv_recording_swift_stub(stub, out)
    wrapper = tmp_path / "swift-wrapper"
    _write_swift_wrapper(wrapper, str(stub))

    result = subprocess.run([str(wrapper), subcommand, "--verbose"],
                            capture_output=True, text=True, timeout=10)

    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    assert out.read_text().splitlines() == [subcommand, "--disable-sandbox", "--verbose"]


def test_swift_wrapper_adds_disable_sandbox_for_a_package_subcommand(tmp_path):
    """Every `package` subcommand loads the manifest, so the flag goes on
    unconditionally for `package`, not only for a fixed allow-list of its
    subcommands."""
    out = tmp_path / "argv"
    stub = tmp_path / "real-swift"
    _write_argv_recording_swift_stub(stub, out)
    wrapper = tmp_path / "swift-wrapper"
    _write_swift_wrapper(wrapper, str(stub))

    result = subprocess.run([str(wrapper), "package", "resolve"],
                            capture_output=True, text=True, timeout=10)

    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    assert out.read_text().splitlines() == ["package", "--disable-sandbox", "resolve"]


def test_swift_wrapper_does_not_add_disable_sandbox_twice(tmp_path):
    out = tmp_path / "argv"
    stub = tmp_path / "real-swift"
    _write_argv_recording_swift_stub(stub, out)
    wrapper = tmp_path / "swift-wrapper"
    _write_swift_wrapper(wrapper, str(stub))

    result = subprocess.run([str(wrapper), "build", "--disable-sandbox", "--verbose"],
                            capture_output=True, text=True, timeout=10)

    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    argv = out.read_text().splitlines()
    assert argv == ["build", "--disable-sandbox", "--verbose"]
    assert argv.count("--disable-sandbox") == 1


def test_swift_wrapper_passes_other_subcommands_through_unchanged(tmp_path):
    out = tmp_path / "argv"
    stub = tmp_path / "real-swift"
    _write_argv_recording_swift_stub(stub, out)
    wrapper = tmp_path / "swift-wrapper"
    _write_swift_wrapper(wrapper, str(stub))

    result = subprocess.run([str(wrapper), "--version"],
                            capture_output=True, text=True, timeout=10)

    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    assert out.read_text().splitlines() == ["--version"]


def test_swift_wrapper_passes_no_arguments_through_unchanged(tmp_path):
    """`swift` with no arguments at all (the bare compiler/REPL invocation) must not
    crash the subcommand check on an unset `$1`."""
    out = tmp_path / "argv"
    stub = tmp_path / "real-swift"
    _write_argv_recording_swift_stub(stub, out)
    wrapper = tmp_path / "swift-wrapper"
    _write_swift_wrapper(wrapper, str(stub))

    result = subprocess.run([str(wrapper)],
                            capture_output=True, text=True, timeout=10)

    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    assert out.read_text() == "\n"  # printf with zero args still writes a bare newline


def _run_swift_wrapper_against(wrapper_path, real_swift, args):
    env = {**os.environ, "AGENTCTL_REAL_SWIFT": real_swift}
    return subprocess.run([str(wrapper_path), *args], capture_output=True, text=True,
                          timeout=10, env=env)


def test_swift_wrapper_refuses_when_agentctl_real_swift_is_its_own_file(tmp_path):
    """Mirrors the git wrapper's self-reference refusal
    (`test_wrapper_refuses_when_agentctl_real_git_is_its_own_file`): the exact incident
    shape is AGENTCTL_REAL_SWIFT naming this wrapper's own file."""
    wrapper = tmp_path / "swift"
    wrapper.write_text(DE._SWIFT_WRAPPER_SCRIPT)
    wrapper.chmod(0o755)

    result = _run_swift_wrapper_against(wrapper, str(wrapper), ["build"])

    assert result.returncode == 127, (result.returncode, result.stdout, result.stderr)
    assert "AGENTCTL_REAL_SWIFT" in result.stderr


def test_swift_wrapper_refuses_when_agentctl_real_swift_is_a_different_copy_of_the_wrapper(tmp_path):
    """The $0 comparison alone would miss this: a SECOND run's wrapper is a different
    file. Only the marker line (shared, verbatim, across every copy of the script)
    catches it."""
    wrapper = tmp_path / "swift"
    wrapper.write_text(DE._SWIFT_WRAPPER_SCRIPT)
    wrapper.chmod(0o755)
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    copy = other_dir / "swift"
    copy.write_text(DE._SWIFT_WRAPPER_SCRIPT)
    copy.chmod(0o755)

    result = _run_swift_wrapper_against(wrapper, str(copy), ["build"])

    assert result.returncode == 127, (result.returncode, result.stdout, result.stderr)
    assert "AGENTCTL_REAL_SWIFT" in result.stderr


def _write_capped_swift_stub(stub_dir, counter, cap=3):
    """Mirrors `_write_capped_git_stub`: a `swift` stub that re-execs `swift` on every
    call — so it would recurse through PATH without bound if the wrapper regressed and
    ever entered it — but refuses once `counter` (one line appended per call) reaches
    `cap`. Every test stub in this file that calls back into `swift` must be built
    through this helper, so a future one can't be added without the cap."""
    stub = stub_dir / "swift"
    stub.write_text(f"""#!/bin/bash
echo x >> {shlex.quote(str(counter))}
count=$(wc -l < {shlex.quote(str(counter))})
if [ "$count" -ge {cap} ]; then
    exit 99
fi
exec swift "$@"
""")
    stub.chmod(0o755)
    return stub


def test_swift_wrapper_runs_the_baked_real_swift_without_resolving_a_path_stub(tmp_path):
    """AGENTCTL_REAL_SWIFT unset, PATH pointing only at a stub that calls back into
    `swift`: the wrapper must run its own baked real swift and never enter the stub at
    all. The baked real swift is this test's own fake binary, never the host's —
    a CI runner and most non-Xcode hosts have no `swift` at all."""
    counter = tmp_path / "counter"
    stub_dir = tmp_path / "stubpath"
    stub_dir.mkdir()
    _write_capped_swift_stub(stub_dir, counter)
    out = tmp_path / "argv"
    real_swift = tmp_path / "real-swift"
    _write_argv_recording_swift_stub(real_swift, out)
    wrapper = tmp_path / "swift-wrapper"
    _write_swift_wrapper(wrapper, str(real_swift))

    result = subprocess.run([str(wrapper), "--version"], env={"PATH": str(stub_dir)},
                            capture_output=True, text=True, timeout=10)

    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    assert out.read_text().splitlines() == ["--version"]
    assert not counter.exists() or counter.read_text() == "", \
        "the stub was entered -- the wrapper resolved a bare 'swift' through PATH"


def test_swift_wrapper_refuses_when_agentctl_real_swift_unset_and_baked_value_empty(tmp_path):
    """No AGENTCTL_REAL_SWIFT and no usable baked fallback: the wrapper must refuse with
    exit 127 and a stderr line, and must never enter the PATH stub at all."""
    counter = tmp_path / "counter"
    stub_dir = tmp_path / "stubpath"
    stub_dir.mkdir()
    _write_capped_swift_stub(stub_dir, counter)
    wrapper = tmp_path / "swift-wrapper"
    _write_swift_wrapper(wrapper, "")

    result = subprocess.run([str(wrapper), "--version"], env={"PATH": str(stub_dir)},
                            capture_output=True, text=True, timeout=10)

    assert result.returncode == 127, (result.returncode, result.stdout, result.stderr)
    assert result.stderr.strip() != ""
    assert not counter.exists(), \
        "the stub was entered -- the wrapper resolved a bare 'swift' through PATH"


def test_resolve_real_swift_returns_nothing_usable_when_path_has_no_real_swift(tmp_path):
    """Must never return the bare name `"swift"` — that name would have to be resolved
    through $PATH by something to be of any use, which is the exact lookup this fix
    removes."""
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    resolved = DE._resolve_real_swift(str(empty_dir), wrapper_dir="/does/not/matter")

    assert resolved in ("", None)
    assert resolved != "swift"


def test_resolve_real_swift_skips_an_inherited_wrapper_elsewhere_on_path(tmp_path):
    """Mirrors `test_resolve_real_git_skips_an_inherited_wrapper_elsewhere_on_path`: a
    `dispatch_env()` call made from INSIDE an already-dispatched run inherits an
    ANCESTOR process's wrapper directory already first on PATH, under a tmp path this
    call's own `_private_swift_wrapper_dir()` never created. `_resolve_real_swift` must
    skip that directory too, by recognizing its `swift` as a wrapper copy, never by
    running anything."""
    inherited_wrapper_dir = tmp_path / "inherited-wrapper"
    inherited_wrapper_dir.mkdir()
    fake_wrapper = inherited_wrapper_dir / "swift"
    fake_wrapper.write_text(f"#!/bin/bash\n# {DE._SWIFT_WRAPPER_MARKER}\necho should-never-run\n")
    fake_wrapper.chmod(0o755)

    real_dir = tmp_path / "real"
    real_dir.mkdir()
    fake_real_swift = real_dir / "swift"
    fake_real_swift.write_text("#!/bin/bash\necho real-swift\n")
    fake_real_swift.chmod(0o755)

    path = os.pathsep.join([str(inherited_wrapper_dir), str(real_dir)])

    resolved = DE._resolve_real_swift(path, wrapper_dir=str(tmp_path / "some-other-wrapper-dir"))

    assert resolved == str(fake_real_swift)


def test_baked_real_swift_path_with_space_and_dollar_round_trips_intact(tmp_path):
    """A baked path holding a space and a `$` must reach the wrapper's `real_swift`
    variable as one literal value, never shell-expanded or split."""
    weird_dir = tmp_path / "weird path with $dollar"
    weird_dir.mkdir()
    fake_real_swift = weird_dir / "swift"
    fake_real_swift.write_text("#!/bin/bash\necho weird-real-swift-ran\n")
    fake_real_swift.chmod(0o755)
    wrapper = tmp_path / "swift-wrapper"
    _write_swift_wrapper(wrapper, str(fake_real_swift))

    result = subprocess.run([str(wrapper), "--version"], env={"PATH": "/usr/bin:/bin"},
                            capture_output=True, text=True, timeout=10)

    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    assert result.stdout.strip() == "weird-real-swift-ran"


def _write_env_dumping_swift_stub(path, out_file):
    """Write a `swift` stub at `path` that dumps the environment it actually received
    (`env`'s own output, one `NAME=VALUE` pair per line) to `out_file` and exits 0.
    Never calls back into `swift`, so it can never recurse either way."""
    path.write_text(f"""#!/bin/bash
env > {shlex.quote(str(out_file))}
""")
    path.chmod(0o755)


def test_swift_wrapper_strips_credentials_from_the_real_swift_env(tmp_path):
    """PR #965 round-1 security finding: `--disable-sandbox` removes SwiftPM's own
    network denial during manifest/plugin compilation, so a run's live GH_TOKEN/
    GITHUB_TOKEN (dispatch_env.py's `git_credential_guard_env` — a read-only GitHub
    App installation token, scoped to the run's one repository) and ANTHROPIC_API_KEY
    (bin/agentctl's `gateway_env` — a gateway-model dispatch's LiteLLM virtual key)
    must never reach manifest/plugin code through the wrapper's exec. An ordinary
    variable must still reach it unchanged."""
    out = tmp_path / "env-dump"
    stub = tmp_path / "real-swift"
    _write_env_dumping_swift_stub(stub, out)
    wrapper = tmp_path / "swift-wrapper"
    _write_swift_wrapper(wrapper, str(stub))
    env = {**os.environ,
          "GH_TOKEN": "fake-gh-token",
          "GITHUB_TOKEN": "fake-github-token",
          "ANTHROPIC_API_KEY": "fake-anthropic-key",
          "AN_ORDINARY_VAR": "should-still-arrive"}
    env.pop("AGENTCTL_REAL_SWIFT", None)

    result = subprocess.run([str(wrapper), "build"], capture_output=True, text=True,
                            timeout=10, env=env)

    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    dumped = out.read_text()
    assert "GH_TOKEN" not in dumped
    assert "GITHUB_TOKEN" not in dumped
    assert "ANTHROPIC_API_KEY" not in dumped
    assert "AN_ORDINARY_VAR=should-still-arrive" in dumped
