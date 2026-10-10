"""bin/agentctl's own sys.path resolution (ARCH-229).

agentctl used to insert `Path.home() / ".local" / "lib" / "agent-platform"` at
import time: a directory that, on a dev machine, is a per-file symlink farm
into whatever the primary checkout happens to be on, and on CI does not exist
at all. Whichever copy of `bin/agentctl` actually ran, that line always
imported the SAME farm, so a worktree's own agentctl silently ran main's
review_protocol instead of its own.

The fix resolves the lib dir from `Path(__file__).resolve()` instead, so each
of the three real callers gets its OWN answer:
  - a worktree's own copy of `bin/agentctl` (a real file) imports that
    worktree's own `lib/agent-platform`
  - `~/.local/bin/agentctl` (a symlink into the primary checkout) resolves
    through the symlink to the primary checkout's own lib
  - a standalone deploy-area copy (agent-platform#469's shape) imports that
    deploy area's own lib, decoupled from whatever the primary checkout is on

These tests build REAL, self-consistent copies of this repo's own
`lib/agent-platform` (copying the actual files rather than hand-written stubs,
so nothing here can drift out of sync with what the real modules need at
import time) and tag one file per copy with a marker attribute, so a test can
tell which copy actually got imported rather than merely that something did.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
AGENTCTL_SRC = REPO / "bin" / "agentctl"
LIB_SRC = REPO / "lib" / "agent-platform"


def _build_checkout(dest: Path, marker: str) -> Path:
    """A self-consistent copy of this repo's bin/agentctl + lib/agent-platform
    under `dest`, with telemetry.py tagged `TEST_LIB_MARKER = marker`. Returns
    the copied bin/agentctl path."""
    (dest / "bin").mkdir(parents=True)
    shutil.copy2(AGENTCTL_SRC, dest / "bin" / "agentctl")
    (dest / "bin" / "agentctl").chmod(0o755)
    shutil.copytree(LIB_SRC, dest / "lib" / "agent-platform")
    with (dest / "lib" / "agent-platform" / "telemetry.py").open("a") as f:
        f.write(f"\nTEST_LIB_MARKER = {marker!r}\n")
    return dest / "bin" / "agentctl"


def _run_and_identify(agentctl_path: Path, tmp_path: Path) -> subprocess.CompletedProcess:
    """Run `<agentctl_path> --help` via runpy, then report which `telemetry`
    module ended up in sys.modules (file + marker) after agentctl's own
    sys.path/import logic ran.

    A bare `subprocess.run([str(agentctl_path), "--help"])` would only prove
    exit 0 -- that's true whether agentctl imported the right lib, the wrong
    lib, or (on a machine with a real ~/.local farm) production code. Driving
    it through runpy in a `-c` this test controls lets it inspect
    `sys.modules["telemetry"]` after agentctl's import runs, with no
    instrumentation added to agentctl's own source.

    HOME is pointed at an empty directory so the result is deterministic
    regardless of what the machine running this test actually has under
    ~/.local -- this is what makes the old, unfixed `Path.home()`-based line
    fail with a clean ModuleNotFoundError here instead of silently importing
    whatever this developer's machine happens to have farm-linked.
    """
    probe = (
        "import runpy, sys\n"
        f"sys.argv = [{str(agentctl_path)!r}, '--help']\n"
        "try:\n"
        f"    runpy.run_path({str(agentctl_path)!r}, run_name='__main__')\n"
        "except SystemExit:\n"
        "    pass\n"
        "t = sys.modules.get('telemetry')\n"
        "print('TELEMETRY_FILE=' + (t.__file__ if t else 'MISSING'))\n"
        "print('TELEMETRY_MARKER=' + repr(getattr(t, 'TEST_LIB_MARKER', None)))\n"
    )
    empty_home = tmp_path / "empty_home"
    empty_home.mkdir(exist_ok=True)
    env = dict(os.environ)
    env["HOME"] = str(empty_home)
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, timeout=30, env=env,
    )


def _marker(result: subprocess.CompletedProcess) -> str | None:
    for line in result.stdout.splitlines():
        if line.startswith("TELEMETRY_MARKER="):
            return line[len("TELEMETRY_MARKER="):]
    return None


def _file(result: subprocess.CompletedProcess) -> str | None:
    for line in result.stdout.splitlines():
        if line.startswith("TELEMETRY_FILE="):
            return line[len("TELEMETRY_FILE="):]
    return None


def test_worktree_copy_imports_its_own_lib(tmp_path):
    """Case 3: a worktree's own real bin/agentctl file must import that same
    worktree's own lib/agent-platform, not ~/.local's farm (empty HOME here)."""
    worktree = tmp_path / "worktree"
    agentctl_path = _build_checkout(worktree, marker="worktree")

    result = _run_and_identify(agentctl_path, tmp_path)

    assert result.returncode == 0, result.stderr
    assert _marker(result) == "'worktree'"
    assert _file(result) == str(worktree / "lib" / "agent-platform" / "telemetry.py")


def test_symlink_into_checkout_resolves_through_to_checkouts_lib(tmp_path):
    """Case 2: `~/.local/bin/agentctl` is a symlink into the primary checkout
    (ARCH-229). Invoking it must resolve through the symlink to the PRIMARY
    checkout's own lib, so a person typing `agentctl` still runs their edits."""
    primary = tmp_path / "primary"
    _build_checkout(primary, marker="primary")

    local_bin = tmp_path / "local_bin"
    local_bin.mkdir()
    symlinked_agentctl = local_bin / "agentctl"
    symlinked_agentctl.symlink_to(primary / "bin" / "agentctl")

    result = _run_and_identify(symlinked_agentctl, tmp_path)

    assert result.returncode == 0, result.stderr
    assert _marker(result) == "'primary'"
    assert _file(result) == str(primary / "lib" / "agent-platform" / "telemetry.py")


def test_deploy_area_copy_imports_its_own_lib(tmp_path):
    """Case 1: a standalone deploy-area copy (agent-platform#469's shape --
    this area does not exist on this machine yet, so this test constructs the
    layout it will have: bin/agentctl and lib/agent-platform as real-file
    siblings, decoupled from whatever the primary checkout is on)."""
    deploy = tmp_path / "deploy" / "agent-platform-shared"
    agentctl_path = _build_checkout(deploy, marker="deploy")

    result = _run_and_identify(agentctl_path, tmp_path)

    assert result.returncode == 0, result.stderr
    assert _marker(result) == "'deploy'"
    assert _file(result) == str(deploy / "lib" / "agent-platform" / "telemetry.py")


def test_worktree_and_symlinked_primary_resolve_to_different_libs(tmp_path):
    """The bug this fix closes: every caller used to share ONE deployed farm
    regardless of which checkout's agentctl actually ran, so a worktree's own
    agentctl silently imported main's review_protocol. Building both a
    worktree copy and a primary-plus-symlink copy side by side and asserting
    they resolve to different files is the direct check that the skew is
    gone, not just that each case works in isolation."""
    worktree = tmp_path / "worktree"
    worktree_agentctl = _build_checkout(worktree, marker="worktree")

    primary = tmp_path / "primary"
    _build_checkout(primary, marker="primary")
    local_bin = tmp_path / "local_bin"
    local_bin.mkdir()
    symlinked_agentctl = local_bin / "agentctl"
    symlinked_agentctl.symlink_to(primary / "bin" / "agentctl")

    worktree_result = _run_and_identify(worktree_agentctl, tmp_path)
    primary_result = _run_and_identify(symlinked_agentctl, tmp_path)

    assert _marker(worktree_result) == "'worktree'"
    assert _marker(primary_result) == "'primary'"
    assert _file(worktree_result) != _file(primary_result)
