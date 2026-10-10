"""Every Python command under bin/ starts with `#!/usr/bin/env python3[.11]`.

`#!/Users/<name>/.local/lib/knowledge/.venv/bin/python3` and
`#!/opt/homebrew/bin/python3.11` exist on one Mac and nowhere else. On any other
machine the kernel answers "required file not found" (exit 127), so `co` could
not run `knowledge-ctx` and no session got the kb or ops tools. Ten commands
carried them until issue_01M3B8JEDA2RVKZ7X343CNEZ71. A command that needs the
venv's packages now finds it through `lib/agent-platform/venv_bootstrap.py`.
"""
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# `env` resolves the interpreter from PATH, so neither form names a machine. The
# versioned form is what 29 commands carried before this change (they need 3.10+).
PORTABLE = ("/usr/bin/env python3", "/usr/bin/env python3.11")
# co-session-health runs only under the system python on the Mac that hosts it
# and needs nothing from the venv, so its fixed path is left as it was.
#
# review-launch is reached only through `sudo /opt/builder/libexec/
# review-launch` (platform/decisions/0090) — never symlinked into
# `~/.local/bin` the way an ordinary `bin/` command is, so it never runs on
# "any other machine" in the sense this module's docstring means. Round 2 of
# #732, finding 5: an `env`-resolved interpreter there depends on PATH/
# sudoers' `secure_path`, which this script must not depend on; the fixed
# absolute path is the fix, not the bug the rest of this test guards against.
# Round 3 of #732, finding 2: `-I` (isolated mode — no PYTHONPATH, no user
# site directory) is appended because this interpreter is owned by the
# OPERATOR's account, not root's.
ALLOWED = {"co-session-health": "/usr/bin/python3",
          "review-launch": "/opt/homebrew/bin/python3 -I"}


def _shebang(path: Path):
    try:
        with path.open("rb") as fh:
            first = fh.readline(512)
    except OSError:
        return None
    if not first.startswith(b"#!"):
        return None
    return first[2:].decode("utf-8", "replace").strip()


def test_every_python_shebang_is_portable_or_allowlisted():
    offenders = []
    for path in sorted((REPO / "bin").iterdir()):
        if not path.is_file():
            continue
        line = _shebang(path)
        if not line or "python" not in line:
            continue
        if line != ALLOWED.get(path.name) and line not in PORTABLE:
            offenders.append(f"bin/{path.name}: #!{line}")
    assert not offenders, (f"a Python shebang must be one of {PORTABLE}:\n"
                           + "\n".join(offenders))


def test_the_scan_sees_shebangs():
    """Positive control: an empty scan would pass the test above for any reason."""
    seen = [p for p in (REPO / "bin").iterdir() if p.is_file() and _shebang(p)]
    assert len(seen) > 20
    assert _shebang(REPO / "bin" / "knowledge-ctx") == "/usr/bin/env python3"


def test_review_launch_shebang_is_an_absolute_interpreter_path_not_env():
    """Round 2 of #732, finding 5: `review-launch` runs only under `sudo`
    (platform/decisions/0090), so its shebang must not resolve an
    interpreter through `PATH`/`env` at all — the one property that made
    `#!/usr/bin/env python3.11` wrong here even though it is PORTABLE for
    every ordinary `bin/` command."""
    line = _shebang(REPO / "bin" / "review-launch")
    assert line == ALLOWED.get("review-launch"), (
        f"expected the fixed absolute interpreter path in ALLOWED['review-launch'], "
        f"got {line!r}")
    assert not line.startswith("/usr/bin/env"), (
        f"review-launch's shebang still resolves {line!r} through PATH")


def _git_index_modes():
    """Mode bits for every tracked `bin/` file, read from the git index via
    `git ls-files -s` rather than `git ls-tree HEAD` — the index reflects a
    staged-but-uncommitted `chmod`, which is the state this check runs in
    right before a commit."""
    out = subprocess.run(
        ["git", "ls-files", "-s", "bin/"], cwd=REPO,
        capture_output=True, text=True, check=True,
    ).stdout
    modes = {}
    for line in out.splitlines():
        mode, _sha, stage_and_name = line.split(maxsplit=2)
        _stage, name = stage_and_name.split("\t", 1)
        modes[name] = int(mode, 8)
    return modes


def test_every_file_with_a_shebang_is_executable():
    """`co-ask` shipped at mode 100644 while every sibling `bin/` command is
    100755: `install.sh` symlinks it onto PATH unchanged, so running `co-ask`
    hit "Permission denied". The fixture that drives these CLIs in tests loads
    them with `SourceFileLoader`, which ignores the mode bit, so nothing else
    in this suite would have caught it.
    """
    index_modes = _git_index_modes()
    offenders = []
    for path in sorted((REPO / "bin").iterdir()):
        if not path.is_file() or not _shebang(path):
            continue
        rel = f"bin/{path.name}"
        if not os.access(path, os.X_OK):
            offenders.append(f"{rel}: not executable on disk")
        mode = index_modes.get(rel)
        if mode is not None and not mode & 0o111:
            offenders.append(f"{rel}: not executable in the git index (mode {oct(mode)})")
    assert not offenders, "\n".join(offenders)
