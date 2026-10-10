"""issue_01M4CWH0XFCV5CYMZVNDYSMP9C: a dispatched run's own environment carries
`dispatch_env`'s GIT_OBJECT_DIRECTORY / GIT_ALTERNATE_OBJECT_DIRECTORIES, redirecting
git at the run's private object store (agent-platform#793). conftest.py inherits that
environment, so every git call a fixture makes wrote into the run's private store
instead of the repo the fixture itself created — `git init --bare` left the new repo
with no objects/ directory, and a push into it failed outright (receive-pack, spawned
locally by the push, inherited the same redirect and could not recognise the bare repo
as a git repository at all). Reproduced by the co on 2026-10-07.

Run as a SUBPROCESS, with the three vars set in the parent environment the way a
dispatched run's own environment sets them, and conftest.py imported fresh inside it —
the in-process suite already imported conftest (and so already stripped these vars)
before this test file is even collected, so the only way to exercise the strip itself
is a fresh process that starts with them present.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _run_with_redirect_vars_set(tmp_path: Path, code: str) -> subprocess.CompletedProcess:
    private = tmp_path / "private-objects"
    private.mkdir()
    shared = tmp_path / "shared-objects"
    shared.mkdir()
    env = {**os.environ,
           "GIT_OBJECT_DIRECTORY": str(private),
           "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(shared),
           "GIT_DIR": str(tmp_path / "not-a-real-gitdir")}
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=120,
    )


def test_importing_conftest_strips_the_redirect_vars(tmp_path):
    result = _run_with_redirect_vars_set(tmp_path, textwrap.dedent("""
        import sys
        sys.path.insert(0, "tests")
        import conftest  # noqa: F401
        import os
        assert "GIT_OBJECT_DIRECTORY" not in os.environ
        assert "GIT_ALTERNATE_OBJECT_DIRECTORIES" not in os.environ
        assert "GIT_DIR" not in os.environ
        print("OK")
    """))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


def test_git_init_bare_creates_objects_after_importing_conftest(tmp_path):
    bare = tmp_path / "withenv.git"
    result = _run_with_redirect_vars_set(tmp_path, textwrap.dedent(f"""
        import sys
        sys.path.insert(0, "tests")
        import conftest  # noqa: F401
        import subprocess
        subprocess.run(["git", "init", "--bare", {str(bare)!r}], check=True)
        print("OK")
    """))
    assert result.returncode == 0, result.stdout + result.stderr
    assert (bare / "objects").is_dir()


def test_push_into_a_fixture_bare_repo_succeeds_after_importing_conftest(tmp_path):
    bare = tmp_path / "bare.git"
    work = tmp_path / "work"
    result = _run_with_redirect_vars_set(tmp_path, textwrap.dedent(f"""
        import sys
        sys.path.insert(0, "tests")
        import conftest  # noqa: F401
        import subprocess
        bare = {str(bare)!r}
        work = {str(work)!r}
        subprocess.run(["git", "init", "--bare", bare], check=True)
        subprocess.run(["git", "init", "-q", work], check=True)
        subprocess.run(["git", "-C", work, "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-q", "--allow-empty", "-m", "x"], check=True)
        subprocess.run(["git", "-C", work, "push", "-q", bare, "HEAD:refs/heads/main"],
                       check=True)
        print("OK")
    """))
    assert result.returncode == 0, result.stdout + result.stderr
    assert any((bare / "objects").rglob("*"))


def test_a_lib_module_imported_by_the_pin_loop_does_not_bake_in_the_redirect(tmp_path):
    """`primary_guard._SAFE_GIT_ENV` is a module-level `{**os.environ, ...}` snapshot
    taken at IMPORT time. conftest.py's own pin loop imports every lib module
    (`primary_guard` included) to pin it to this checkout's copy, so stripping the
    redirect vars from `os.environ` AFTER that loop runs is too late: the snapshot
    already baked them in, and every later `_SAFE_GIT_ENV`-based git call (`git
    status` in `primary_guard.snapshot`) stays redirected regardless. Caught by
    running `test_primary_guard.py` itself under a simulated dispatched-run
    environment — it failed wholesale the first time this fix landed after the
    pin loop instead of before it."""
    result = _run_with_redirect_vars_set(tmp_path, textwrap.dedent("""
        import sys
        sys.path.insert(0, "tests")
        import conftest  # noqa: F401
        import primary_guard as PG
        assert "GIT_OBJECT_DIRECTORY" not in PG._SAFE_GIT_ENV
        assert "GIT_ALTERNATE_OBJECT_DIRECTORIES" not in PG._SAFE_GIT_ENV
        assert "GIT_DIR" not in PG._SAFE_GIT_ENV
        print("OK")
    """))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout
