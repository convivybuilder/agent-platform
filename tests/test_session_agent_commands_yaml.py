"""`bin/session-agent commands` end to end, for the `venv_bootstrap.ensure(...,
optional=True)` wiring ahead of the `session_commands` import: a clean Linux
install's system python has no PyYAML, and Deck's slash-command menu should
still show a command's description or a skill's declared name once a venv
serves it, and names only when none does.

Also covers the flip side of that wiring: `ensure()` sits in the `commands`
verb's own branch, not at import time, because `commands` is the only verb
that reaches `session_commands`'s yaml-parsing `_frontmatter`. `state` is
what Deck polls every tick, so it must never pay that call's subprocess probe
or a re-exec, even when a serving venv is sitting right where `commands`
would have found one.

A REAL SUBPROCESS, deliberately, not the `session_agent_cli` fixture in
`test_session_agent_cli.py`: that fixture loads the file as a module, so its
`if __name__ == "__main__":` guard around the `ensure()` call never runs, and
the only way to prove the wiring fires is to actually run the script.

The interpreter under test is a bare venv with no PyYAML — built fresh per
test rather than trusting the ambient one, since the pytest venv installs
PyYAML for `knowledge-ctx` and would hide the Linux case this exists for.
`commands` also needs a session NAME, but resolving one through real tmux
would need the binary in CI, which this repo does not install for it
(session_state.py's own tmux calls degrade to `state: unknown` on any OSError,
including "no such file", so stripping tmux off PATH exercises that same
degrade path without needing tmux at all — and gives `cwd: None`, so only
`$HOME/.claude` is listed, which is all a skill-under-home fixture needs).

`candidate_venvs`' passwd-home candidate reads the real system passwd entry
for this uid, ignoring `$HOME` entirely — on a workstation with its own
knowledge venv already provisioned, that is a second, uncontrolled candidate
ahead of the `$HOME` one these tests set up. A `sitecustomize.py` on
`PYTHONPATH`, loaded by Python's own startup before `session-agent` runs,
points it at a directory that does not exist instead.
"""
from __future__ import annotations

import json
import subprocess
import venv
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "bin" / "session-agent"


def _bare_python(path: Path) -> Path:
    """A venv with no PyYAML — the Linux-system-python stand-in."""
    venv.EnvBuilder(with_pip=False, symlinks=True).create(path)
    return path


def _venv_with_fake_yaml(path: Path) -> Path:
    """A venv whose `import yaml; yaml.safe_load(...)` works, without a real
    PyYAML install (no network/pip in this sandbox). Only `key: value` lines
    are needed: every frontmatter block this test writes is that simple."""
    venv.EnvBuilder(with_pip=False, symlinks=True).create(path)
    site = next(path.glob("lib/python*/site-packages"))
    (site / "yaml.py").write_text(
        "def safe_load(text):\n"
        "    out = {}\n"
        "    for line in text.splitlines():\n"
        "        line = line.strip()\n"
        "        if not line or ':' not in line:\n"
        "            continue\n"
        "        k, _, v = line.partition(':')\n"
        "        out[k.strip()] = v.strip()\n"
        "    return out\n")
    return path


def _skill(home: Path, description: str) -> None:
    manifest = home / ".claude" / "skills" / "handoff-leave" / "SKILL.md"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(f"---\ndescription: {description}\n---\n# handoff-leave\n")


def _venv_with_invocation_marker(path: Path, marker: Path) -> Path:
    """A venv whose `bin/python3` is a stand-in that only ever records being
    invoked, at the exact spot `commands`' serving venv would sit. Proves a
    negative honestly: the marker is a real, `_has_interpreter`-passing
    candidate `find_venv` would return if `ensure()` looked, so its absence
    after a run means the probe or the re-exec seam was never reached, not
    that there was nothing to find."""
    py_dir = path / "bin"
    py_dir.mkdir(parents=True)
    py = py_dir / "python3"
    py.write_text(f"#!/bin/sh\necho invoked >> {marker}\nexit 0\n")
    py.chmod(0o755)
    return path


def _no_real_passwd_home(tmp_path: Path) -> Path:
    shim = tmp_path / "siteshim"
    shim.mkdir()
    (shim / "sitecustomize.py").write_text(
        "import pwd\n"
        "class _FakePw:\n"
        "    pw_dir = '/nonexistent-passwd-home-for-tests'\n"
        "pwd.getpwuid = lambda uid: _FakePw()\n")
    return shim


def _run_commands(python: Path, home: Path, tmp_path: Path) -> dict:
    empty_bin = tmp_path / "bin"
    empty_bin.mkdir(exist_ok=True)
    r = subprocess.run(
        [str(python), str(SCRIPT), "commands", "zz-no-such-tmux-session"],
        env={"HOME": str(home), "PATH": str(empty_bin),
             "PYTHONPATH": str(_no_real_passwd_home(tmp_path))},
        capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stderr
    return json.loads(r.stdout)


def test_a_description_shows_up_under_a_serving_venv(tmp_path):
    home = tmp_path / "home"
    _skill(home, "write a handoff")
    system_python = _bare_python(tmp_path / "system-python")
    _venv_with_fake_yaml(home / ".local" / "lib" / "knowledge" / ".venv")

    out = _run_commands(system_python / "bin" / "python3", home, tmp_path)

    row = next(c for c in out["commands"] if c["name"] == "handoff-leave")
    assert row["description"] == "write a handoff"


def test_only_names_show_with_no_serving_venv_and_the_process_still_exits_0(tmp_path):
    home = tmp_path / "home"
    _skill(home, "write a handoff")
    system_python = _bare_python(tmp_path / "system-python")
    # No venv anywhere under home: PyYAML is unreachable, same as a clean
    # Linux install session-agent's own steward asked this fixed for.

    out = _run_commands(system_python / "bin" / "python3", home, tmp_path)

    row = next(c for c in out["commands"] if c["name"] == "handoff-leave")
    assert row["name"] == "handoff-leave"
    assert row["description"] is None


def test_state_never_probes_or_reexecs_even_with_a_serving_venv_present(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    marker = tmp_path / "venv-invoked.log"
    system_python = _bare_python(tmp_path / "system-python")
    _venv_with_invocation_marker(
        home / ".local" / "lib" / "knowledge" / ".venv", marker)
    empty_bin = tmp_path / "bin"
    empty_bin.mkdir()

    r = subprocess.run(
        [str(system_python / "bin" / "python3"), str(SCRIPT),
         "state", "zz-no-such-tmux-session"],
        env={"HOME": str(home), "PATH": str(empty_bin),
             "PYTHONPATH": str(_no_real_passwd_home(tmp_path))},
        capture_output=True, text=True, timeout=30)

    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stderr
    state = json.loads(r.stdout)
    assert state["session"] == "zz-no-such-tmux-session"
    assert not marker.exists(), (
        "state reached venv_bootstrap.ensure()'s probe or re-exec seam; "
        "only commands should ever call it")
