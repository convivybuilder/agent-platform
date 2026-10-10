"""lib/agent-platform/venv_bootstrap.py: find the venv from the script, re-exec.

The end-to-end cases build a real (pip-less) venv and run a real script through
the bootstrap, since what matters is that the child interpreter is the venv's and
that the `sys.prefix`-based loop protection holds.
"""
import os
import subprocess
import sys
import venv
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib" / "agent-platform"))
import venv_bootstrap as VB  # noqa: E402

LIB = str(REPO / "lib" / "agent-platform")
SCRIPT = f'''import sys
sys.path.insert(0, {LIB!r})
import venv_bootstrap
venv_bootstrap._passwd_home = lambda: None  # the real user's venv must not leak in
venv_bootstrap.ensure(__file__, requires=("bootstrap_probe_pkg",))
import bootstrap_probe_pkg
print("prefix=" + sys.prefix)
print("pkg=" + bootstrap_probe_pkg.WHERE)
'''


def _make_venv(path: Path, with_pkg: bool = True) -> Path:
    venv.EnvBuilder(with_pip=False, symlinks=True).create(path)
    if with_pkg:
        site = next(path.glob("lib/python*/site-packages"))
        (site / "bootstrap_probe_pkg.py").write_text('WHERE = "venv"\n')
    return path


def _script(path: Path, body: str = SCRIPT) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def _run(script, home, extra_env=None):
    env = {"PATH": os.environ["PATH"], "HOME": str(home)}
    env.update(extra_env or {})
    # The base interpreter, not this run's venv: prefer_venv leaves a venv alone,
    # and the probe package must only be reachable through the bootstrap.
    base = getattr(sys, "_base_executable", sys.executable)
    return subprocess.run([base, str(script)], env=env,
                          capture_output=True, text=True, timeout=60)


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


def test_found_beside_the_invoking_symlink(tmp_path, home):
    """~/.local/bin/x -> checkout/bin/x, venv at ~/.local/lib/knowledge/.venv."""
    checkout_script = _script(tmp_path / "checkout" / "bin" / "cmd")
    local = tmp_path / "local"
    (local / "bin").mkdir(parents=True)
    link = local / "bin" / "cmd"
    link.symlink_to(checkout_script)
    v = _make_venv(local / "lib" / "knowledge" / ".venv")
    r = _run(link, home)
    assert r.returncode == 0, r.stderr
    assert f"prefix={v}" in r.stdout
    assert "pkg=venv" in r.stdout


def test_found_in_a_deploy_area_beside_the_real_script(tmp_path, home):
    script = _script(tmp_path / "deploy" / "bin" / "cmd")
    (tmp_path / "deploy" / "DEPLOYED_COMMIT").write_text("commit: x\n")
    v = _make_venv(tmp_path / "deploy" / "venv")
    r = _run(script, home)
    assert r.returncode == 0, r.stderr
    assert f"prefix={v}" in r.stdout


def test_found_from_a_checkout_by_home(tmp_path, home):
    """A worktree copy has no venv of its own; $HOME's install.sh venv is used."""
    script = _script(tmp_path / "worktree" / "bin" / "cmd")
    v = _make_venv(home / ".local" / "lib" / "knowledge" / ".venv")
    r = _run(script, home)
    assert r.returncode == 0, r.stderr
    assert f"prefix={v}" in r.stdout


def test_missing_is_a_clear_error_not_a_traceback(tmp_path, home):
    script = _script(tmp_path / "worktree" / "bin" / "cmd")
    r = _run(script, home)
    assert r.returncode == 1
    assert "Traceback" not in r.stderr
    assert "bootstrap_probe_pkg" in r.stderr
    assert "no virtualenv was found" in r.stderr
    assert str(home / ".local" / "lib" / "knowledge" / ".venv") in r.stderr


def test_a_venv_lacking_the_package_errors_instead_of_looping(tmp_path, home):
    script = _script(tmp_path / "deploy" / "bin" / "cmd")
    (tmp_path / "deploy" / "DEPLOYED_COMMIT").write_text("commit: x\n")
    _make_venv(tmp_path / "deploy" / "venv", with_pkg=False)
    r = _run(script, home)
    assert r.returncode == 1
    assert "lack the Python package(s) bootstrap_probe_pkg" in r.stderr
    assert "Traceback" not in r.stderr


def test_nothing_happens_when_the_running_interpreter_has_the_package(tmp_path, home):
    script = _script(tmp_path / "worktree" / "bin" / "cmd")
    pkgdir = tmp_path / "pkgs"
    pkgdir.mkdir()
    (pkgdir / "bootstrap_probe_pkg.py").write_text('WHERE = "ambient"\n')
    r = _run(script, home, {"PYTHONPATH": str(pkgdir)})
    assert r.returncode == 0, r.stderr
    assert "pkg=ambient" in r.stdout


def test_prefer_venv_reexecs_a_stdlib_command_and_carries_on_without_one(tmp_path, home):
    body = (f"import sys\nsys.path.insert(0, {LIB!r})\n"
            "import venv_bootstrap\nvenv_bootstrap._passwd_home = lambda: None\n"
            "venv_bootstrap.ensure(__file__, prefer_venv=True)\n"
            "print('prefix=' + sys.prefix)\n")
    script = _script(tmp_path / "worktree" / "bin" / "cmd", body)
    bare = _run(script, home)
    assert bare.returncode == 0, bare.stderr
    assert f"prefix={home}" not in bare.stdout
    v = _make_venv(home / ".local" / "lib" / "knowledge" / ".venv", with_pkg=False)
    withvenv = _run(script, home)
    assert withvenv.returncode == 0, withvenv.stderr
    assert f"prefix={v}" in withvenv.stdout


def test_candidates_include_the_passwd_home_so_an_empty_home_still_resolves():
    cands = VB.candidate_venvs("/x/bin/cmd")
    assert Path("/x/venv") not in cands  # no DEPLOYED_COMMIT beside /x/bin
    assert Path("/x/lib/knowledge/.venv") in cands
    import pwd
    pw = Path(pwd.getpwuid(os.getuid()).pw_dir)
    assert pw / ".local" / "lib" / "knowledge" / ".venv" in cands


def test_a_packageless_venv_at_the_checkout_root_is_not_taken(tmp_path, home):
    """A dev `venv/` beside a checkout's bin/ has no DEPLOYED_COMMIT and no
    package; the knowledge venv further down the list must still be used."""
    script = _script(tmp_path / "checkout" / "bin" / "cmd")
    _make_venv(tmp_path / "checkout" / "venv", with_pkg=False)
    good = _make_venv(home / ".local" / "lib" / "knowledge" / ".venv")
    r = _run(script, home)
    assert r.returncode == 0, r.stderr
    assert f"prefix={good}" in r.stdout


def test_a_packageless_venv_earlier_in_the_list_is_skipped_for_a_good_one(tmp_path, home):
    """Even with the deploy marker (so candidate 1 is admitted), a venv that
    cannot import the package loses to a later one that can."""
    script = _script(tmp_path / "deploy" / "bin" / "cmd")
    (tmp_path / "deploy" / "DEPLOYED_COMMIT").write_text("commit: x\n")
    _make_venv(tmp_path / "deploy" / "venv", with_pkg=False)
    good = _make_venv(home / ".local" / "lib" / "knowledge" / ".venv")
    r = _run(script, home)
    assert r.returncode == 0, r.stderr
    assert f"prefix={good}" in r.stdout


def test_a_non_venv_interpreter_that_passes_the_probe_execs_once_then_exits_1(tmp_path, home):
    """`bin/python3` is not a real venv (no pyvenv.cfg, so sys.prefix stays at
    the base) and answers the probe with success but never provides the
    package. Only the environment marker stops the second re-exec; the script
    appends one byte per run, so the count is the number of passes."""
    fake = home / ".local" / "lib" / "knowledge" / ".venv" / "bin"
    fake.mkdir(parents=True)
    base = getattr(sys, "_base_executable", sys.executable)
    wrapper = fake / "python3"
    wrapper.write_text(f'#!/bin/sh\n[ "$1" = "-c" ] && exit 0\nexec {base} "$@"\n')
    wrapper.chmod(0o755)
    counter = tmp_path / "passes"
    body = (f"import sys\nsys.path.insert(0, {LIB!r})\n"
            "import venv_bootstrap\nvenv_bootstrap._passwd_home = lambda: None\n"
            f"open({str(counter)!r}, 'a').write('x')\n"
            "venv_bootstrap.ensure(__file__, requires=('bootstrap_probe_pkg',))\n")
    script = _script(tmp_path / "worktree" / "bin" / "cmd", body)
    r = _run(script, home)
    assert r.returncode == 1, r.stderr
    assert counter.read_text() == "xx", "expected the original pass plus one re-exec"
    assert "will not re-exec again" in r.stderr
    assert "Traceback" not in r.stderr


def test_passwd_home_is_tried_before_HOME(monkeypatch):
    monkeypatch.setattr(VB, "_passwd_home", lambda: "/pw")
    monkeypatch.setenv("HOME", "/caller")
    tail = Path(".local/lib/knowledge/.venv")
    cands = VB.candidate_venvs("/x/bin/cmd")
    assert cands.index(Path("/pw") / tail) < cands.index(Path("/caller") / tail)


def test_home_is_used_when_passwd_gives_nothing(monkeypatch):
    monkeypatch.setattr(VB, "_passwd_home", lambda: None)
    monkeypatch.setenv("HOME", "/caller")
    assert Path("/caller/.local/lib/knowledge/.venv") in VB.candidate_venvs("/x/bin/cmd")


def test_the_deploy_candidate_needs_its_marker(tmp_path):
    script = _script(tmp_path / "d" / "bin" / "cmd")
    assert tmp_path / "d" / "venv" not in VB.candidate_venvs(str(script))
    (tmp_path / "d" / "DEPLOYED_COMMIT").write_text("commit: x\n")
    assert (tmp_path / "d").resolve() / "venv" in VB.candidate_venvs(str(script))


def test_found_in_an_install_backend_area_beside_the_real_script(tmp_path, home):
    """Shipwright's install-backend marks its area with a file and builds `.venv`."""
    script = _script(tmp_path / "deploy" / "bin" / "cmd")
    (tmp_path / "deploy" / ".installed-by-install-backend").write_text("")
    v = _make_venv(tmp_path / "deploy" / ".venv")
    assert (tmp_path / "deploy").resolve() / ".venv" in VB.candidate_venvs(str(script))
    r = _run(script, home)
    assert r.returncode == 0, r.stderr
    assert f"prefix={v}" in r.stdout
    assert "pkg=venv" in r.stdout


def test_a_dot_venv_beside_neither_marker_is_not_a_candidate(tmp_path, home):
    """A developer's checkout `.venv` has no marker beside it, and a directory
    of that name is not the marker file."""
    script = _script(tmp_path / "checkout" / "bin" / "cmd")
    _make_venv(tmp_path / "checkout" / ".venv")
    assert tmp_path / "checkout" / ".venv" not in VB.candidate_venvs(str(script))
    (tmp_path / "checkout" / ".installed-by-install-backend").mkdir()
    assert (tmp_path / "checkout").resolve() / ".venv" not in VB.candidate_venvs(str(script))
    good = _make_venv(home / ".local" / "lib" / "knowledge" / ".venv")
    r = _run(script, home)
    assert r.returncode == 0, r.stderr
    assert f"prefix={good}" in r.stdout


def test_an_install_backend_marker_without_a_venv_falls_through(tmp_path, home):
    script = _script(tmp_path / "deploy" / "bin" / "cmd")
    (tmp_path / "deploy" / ".installed-by-install-backend").write_text("")
    good = _make_venv(home / ".local" / "lib" / "knowledge" / ".venv")
    r = _run(script, home)
    assert r.returncode == 0, r.stderr
    assert f"prefix={good}" in r.stdout


def test_the_deploy_commit_candidate_comes_before_the_install_backend_one(tmp_path):
    script = _script(tmp_path / "d" / "bin" / "cmd")
    (tmp_path / "d" / "DEPLOYED_COMMIT").write_text("commit: x\n")
    (tmp_path / "d" / ".installed-by-install-backend").write_text("")
    d = (tmp_path / "d").resolve()
    cands = VB.candidate_venvs(str(script))
    assert cands.index(d / "venv") < cands.index(d / ".venv") < cands.index(
        Path(os.path.abspath(script)).parent.parent / "lib" / "knowledge" / ".venv")


def _stub_exec(calls):
    return lambda py, argv: calls.append((py, argv))


def test_a_second_ensure_with_the_marker_set_exits_1_without_exec(tmp_path, monkeypatch):
    v = _make_venv(tmp_path / "deploy" / "venv")
    script = _script(tmp_path / "deploy" / "bin" / "cmd")
    (tmp_path / "deploy" / "DEPLOYED_COMMIT").write_text("commit: x\n")
    monkeypatch.setenv(VB.MARKER, "1")
    calls = []
    with pytest.raises(SystemExit) as e:
        VB.ensure(str(script), requires=("bootstrap_probe_pkg",), _exec=_stub_exec(calls))
    assert e.value.code == 1
    assert calls == []
    assert v.is_dir()


def test_the_first_ensure_sets_the_marker_and_execs(tmp_path, monkeypatch):
    _make_venv(tmp_path / "deploy" / "venv")
    script = _script(tmp_path / "deploy" / "bin" / "cmd")
    (tmp_path / "deploy" / "DEPLOYED_COMMIT").write_text("commit: x\n")
    monkeypatch.delenv(VB.MARKER, raising=False)
    calls = []
    VB.ensure(str(script), requires=("bootstrap_probe_pkg",), _exec=_stub_exec(calls))
    assert len(calls) == 1
    assert os.environ.get(VB.MARKER) == "1"
    monkeypatch.delenv(VB.MARKER)


def test_a_satisfied_ensure_clears_the_marker(monkeypatch):
    monkeypatch.setenv(VB.MARKER, "1")
    VB.ensure("/x/bin/cmd", requires=("os",), _exec=_stub_exec([]))
    assert VB.MARKER not in os.environ


def test_a_prefer_venv_process_already_in_a_venv_clears_the_marker(monkeypatch):
    monkeypatch.setenv(VB.MARKER, "1")
    monkeypatch.setattr(sys, "prefix", "/a-venv")
    monkeypatch.setattr(sys, "base_prefix", "/base")
    VB.ensure("/x/bin/cmd", prefer_venv=True, _exec=_stub_exec([]))
    assert VB.MARKER not in os.environ


def test_a_child_after_a_satisfied_bootstrap_does_not_see_the_marker(tmp_path, home):
    """The re-exec'd process is fit, so a command it spawns must be free to
    re-exec for itself."""
    body = (f"import os, subprocess, sys\nsys.path.insert(0, {LIB!r})\n"
            "import venv_bootstrap\nvenv_bootstrap._passwd_home = lambda: None\n"
            "venv_bootstrap.ensure(__file__, requires=('bootstrap_probe_pkg',))\n"
            "print('prefix=' + sys.prefix)\n"
            "print('child=' + subprocess.run([sys.executable, '-c', "
            "'import os; print(os.environ.get(\"_VENV_BOOTSTRAPPED\", \"unset\"))'], "
            "capture_output=True, text=True).stdout.strip())\n")
    script = _script(tmp_path / "worktree" / "bin" / "cmd", body)
    v = _make_venv(home / ".local" / "lib" / "knowledge" / ".venv")
    r = _run(script, home)
    assert r.returncode == 0, r.stderr
    assert f"prefix={v}" in r.stdout  # it did re-exec, so the marker was set once
    assert "child=unset" in r.stdout


def test_a_relative_argv0_is_not_a_candidate(tmp_path, monkeypatch):
    cwd = tmp_path / "cwd"
    (cwd / "lib" / "knowledge" / ".venv" / "bin").mkdir(parents=True)
    monkeypatch.chdir(cwd / "lib")  # abspath("bin/cmd") would give cwd/lib/bin/cmd
    (cwd / "lib" / "bin").mkdir()
    rel = VB.candidate_venvs("/x/bin/cmd", invoked="bin/cmd")
    assert Path(os.path.abspath("bin/cmd")).parent.parent / "lib" / "knowledge" / ".venv" not in rel
    assert Path("/x/lib/knowledge/.venv") not in rel  # nor the script's, via invoked
    absolute = VB.candidate_venvs("/x/bin/cmd", invoked="/y/bin/cmd")
    assert Path("/y/lib/knowledge/.venv") in absolute


OPTIONAL_SCRIPT = f'''import sys
sys.path.insert(0, {LIB!r})
import venv_bootstrap
venv_bootstrap._passwd_home = lambda: None  # the real user's venv must not leak in
venv_bootstrap.ensure(__file__, requires=("bootstrap_probe_pkg",), optional=True)
try:
    import bootstrap_probe_pkg
    print("pkg=" + bootstrap_probe_pkg.WHERE)
except ImportError:
    print("pkg=missing")
print("prefix=" + sys.prefix)
'''


def test_optional_returns_at_once_when_the_module_is_already_present(tmp_path, home):
    """The fast path: an interpreter that already has the module returns
    without a subprocess probe or a re-exec — the Mac case, where Homebrew's
    python already has PyYAML."""
    script = _script(tmp_path / "worktree" / "bin" / "cmd", OPTIONAL_SCRIPT)
    pkgdir = tmp_path / "pkgs"
    pkgdir.mkdir()
    (pkgdir / "bootstrap_probe_pkg.py").write_text('WHERE = "ambient"\n')
    r = _run(script, home, {"PYTHONPATH": str(pkgdir)})
    assert r.returncode == 0, r.stderr
    assert "pkg=ambient" in r.stdout
    assert f"prefix={home}" not in r.stdout  # no re-exec: base prefix, unchanged


def test_optional_reexecs_into_a_serving_venv(tmp_path, home):
    script = _script(tmp_path / "worktree" / "bin" / "cmd", OPTIONAL_SCRIPT)
    v = _make_venv(home / ".local" / "lib" / "knowledge" / ".venv")
    r = _run(script, home)
    assert r.returncode == 0, r.stderr
    assert f"prefix={v}" in r.stdout
    assert "pkg=venv" in r.stdout


def test_optional_returns_without_exiting_when_no_venv_serves(tmp_path, home):
    """The Linux case this mode exists for: no venv anywhere, and the caller's
    own fail-soft is what runs next — not `sys.exit(1)`."""
    script = _script(tmp_path / "worktree" / "bin" / "cmd", OPTIONAL_SCRIPT)
    r = _run(script, home)
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stderr
    assert "pkg=missing" in r.stdout


def test_optional_does_not_loop_when_the_reexeced_process_still_lacks_the_module(tmp_path, home):
    """A non-venv interpreter that passes the probe but never actually
    provides the package (as in the `requires`-mode loop-protection test):
    exactly one re-exec, then a quiet return, never a second attempt."""
    fake = home / ".local" / "lib" / "knowledge" / ".venv" / "bin"
    fake.mkdir(parents=True)
    base = getattr(sys, "_base_executable", sys.executable)
    wrapper = fake / "python3"
    wrapper.write_text(f'#!/bin/sh\n[ "$1" = "-c" ] && exit 0\nexec {base} "$@"\n')
    wrapper.chmod(0o755)
    counter = tmp_path / "passes"
    body = (f"import sys\nsys.path.insert(0, {LIB!r})\n"
            "import venv_bootstrap\nvenv_bootstrap._passwd_home = lambda: None\n"
            f"open({str(counter)!r}, 'a').write('x')\n"
            "venv_bootstrap.ensure(__file__, requires=('bootstrap_probe_pkg',), optional=True)\n"
            "print('done')\n")
    script = _script(tmp_path / "worktree" / "bin" / "cmd", body)
    r = _run(script, home)
    assert r.returncode == 0, r.stderr
    assert counter.read_text() == "xx", "expected the original pass plus one re-exec, no more"
    assert "done" in r.stdout
    assert "Traceback" not in r.stderr


def test_optional_with_marker_already_set_returns_without_exec(tmp_path, monkeypatch):
    _make_venv(tmp_path / "deploy" / "venv")
    script = _script(tmp_path / "deploy" / "bin" / "cmd")
    (tmp_path / "deploy" / "DEPLOYED_COMMIT").write_text("commit: x\n")
    monkeypatch.setenv(VB.MARKER, "1")
    calls = []
    VB.ensure(str(script), requires=("bootstrap_probe_pkg",), optional=True,
              _exec=_stub_exec(calls))
    assert calls == []
    assert VB.MARKER not in os.environ


def test_optional_execs_into_the_first_serving_venv(tmp_path, monkeypatch):
    _make_venv(tmp_path / "deploy" / "venv")
    script = _script(tmp_path / "deploy" / "bin" / "cmd")
    (tmp_path / "deploy" / "DEPLOYED_COMMIT").write_text("commit: x\n")
    monkeypatch.delenv(VB.MARKER, raising=False)
    calls = []
    VB.ensure(str(script), requires=("bootstrap_probe_pkg",), optional=True,
              _exec=_stub_exec(calls))
    assert len(calls) == 1
    assert os.environ.get(VB.MARKER) == "1"
    monkeypatch.delenv(VB.MARKER)


def test_optional_returns_quietly_when_no_venv_serves(tmp_path, monkeypatch):
    script = _script(tmp_path / "worktree" / "bin" / "cmd")
    monkeypatch.delenv(VB.MARKER, raising=False)
    calls = []
    VB.ensure(str(script), requires=("bootstrap_probe_pkg",), optional=True,
              _exec=_stub_exec(calls))
    assert calls == []
    assert VB.MARKER not in os.environ


def test_the_probe_gets_no_stdin(tmp_path, monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(VB.subprocess, "run", fake_run)
    assert VB._serves(tmp_path, ("mcp",))
    assert seen["stdin"] is subprocess.DEVNULL
