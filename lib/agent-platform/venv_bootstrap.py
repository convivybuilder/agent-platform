"""Find the knowledge venv from where a command sits and re-exec into it.

Why this exists: a command's shebang cannot name a machine. `#!/Users/<name>/...`
and `#!/opt/homebrew/bin/python3.11` both fail with "required file not found"
(exit 127) on any other machine, and `co` then reports a missing agent instead of
the missing interpreter. Every command now starts with `#!/usr/bin/env python3`,
and one that needs the venv's packages calls `ensure()` before importing them.

Three modes, chosen by the caller:

* `requires=("mcp", ...)` — the command cannot run without those packages. If the
  running interpreter already imports them, nothing happens (CI, the deploy
  area's own venv, a test run). Otherwise re-exec into the venv, or exit 1 with
  one message naming what was missing and where we looked.
* `prefer_venv=True` — the command is stdlib-only, but it has always run on the
  venv's interpreter (3.11). If the running interpreter is not already a venv,
  re-exec into one if it exists, and otherwise carry on with what is running.
* `requires=("yaml",), optional=True` — the command runs without `requires`, but
  is better with them (session-agent, so Deck's slash-command menu can show a
  command's description instead of just its name). If the running interpreter
  already has every module, return at once — no subprocess probe, so the common
  case (Homebrew's python on the Mac, which already has PyYAML) costs nothing
  measurable. Otherwise re-exec into the first candidate venv that serves
  `requires`; when none serves, or this process is already a re-exec, return and
  let the caller's own fail-soft (a lazy, guarded `import`) carry on without them.

Where the venv is looked for, in order. Each is derived from the script, never a
literal home directory:

1. A deploy area, in either of the two layouts, each gated by its own marker:
   * `<script's real dir>/../venv`, only when `<real dir>/../DEPLOYED_COMMIT`
     exists. A deploy area (`~/.local/deploy/<name>/`,
     install-agent-platform-shared) holds `bin/` and `venv/` beside that marker.
   * `<script's real dir>/../.venv`, only when
     `<real dir>/../.installed-by-install-backend` is a file. Shipwright's
     install-backend marks its area that way and builds `.venv`.
   A bare `venv/` or `.venv/` at a checkout's root is the usual dev venv
   (gitignored) and must not be taken for the knowledge venv.
2. `<script's own dir>/../lib/knowledge/.venv` — measured from the path the
   command was INVOKED by, unresolved. `~/.local/bin/x` is a symlink into a
   checkout, so its real dir is the checkout, but the invoked path is still
   `~/.local/bin`, beside the `~/.local/lib/knowledge/.venv` install.sh builds.
3. The passwd home's `.local/lib/knowledge/.venv`, then `$HOME`'s. A command run
   straight from a checkout or a worktree (so neither 1 nor 2 lands) still finds
   the venv install.sh built for the user. `$HOME` is caller-controlled, so it
   comes second and covers a machine where passwd yields nothing.

In `requires` mode a candidate is used only if its interpreter can find every
required module; one that cannot is skipped and the next is tried. Before
re-exec, `_VENV_BOOTSTRAPPED` is set in the environment and a second pass will
not re-exec, so an interpreter that is not a real venv (no `pyvenv.cfg`, so
`sys.prefix` never changes) cannot loop. A process that is fit to run removes
the marker again, so it does not leak to the commands it spawns.

Candidate 2 is skipped when `sys.argv[0]` is relative: it would resolve against
the caller's cwd.

Must stay importable by the system python3 (3.9 on macOS): no 3.10+ syntax.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

VENV_PYTHON = Path("bin") / "python3"
MARKER = "_VENV_BOOTSTRAPPED"
PROBE_TIMEOUT = 10


def _passwd_home() -> str | None:
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_dir
    except (ImportError, KeyError):
        return None


def candidate_venvs(script: str, invoked: str | None = None) -> list[Path]:
    """Every place the venv may be, in the order it is tried. Existence is not
    checked here."""
    real = Path(script).resolve()
    cands = []
    if (real.parent.parent / "DEPLOYED_COMMIT").is_file():
        cands.append(real.parent.parent / "venv")
    if (real.parent.parent / ".installed-by-install-backend").is_file():
        cands.append(real.parent.parent / ".venv")
    # A relative argv[0] would resolve against the caller's cwd, which the
    # caller controls, so it is not a place to look for a venv.
    if invoked is None or os.path.isabs(invoked):
        seen = Path(os.path.abspath(invoked or script))
        cands.append(seen.parent.parent / "lib" / "knowledge" / ".venv")
    for home in (_passwd_home(), os.environ.get("HOME")):
        if home:
            cands.append(Path(home) / ".local" / "lib" / "knowledge" / ".venv")
    out: list[Path] = []
    for c in cands:
        if c not in out:
            out.append(c)
    return out


def _has_interpreter(cand: Path) -> bool:
    py = cand / VENV_PYTHON
    return py.is_file() and os.access(py, os.X_OK)


def _serves(cand: Path, requires: tuple[str, ...]) -> bool:
    """True when the candidate's interpreter finds every required module."""
    if not requires:
        return True
    code = ("import importlib.util, sys; "
            "sys.exit(any(importlib.util.find_spec(m) is None for m in sys.argv[1:]))")
    try:
        r = subprocess.run([str(cand / VENV_PYTHON), "-c", code, *requires],
                           stdin=subprocess.DEVNULL, capture_output=True,
                           timeout=PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def find_venv(script: str, invoked: str | None = None,
              requires: tuple[str, ...] = ()) -> Path | None:
    """The first candidate with an executable interpreter that can import
    `requires`, or None."""
    for cand in candidate_venvs(script, invoked):
        if _has_interpreter(cand) and _serves(cand, requires):
            return cand
    return None


def _in_venv(venv: Path) -> bool:
    return os.path.realpath(sys.prefix) == os.path.realpath(venv)


def ensure(script: str, requires: tuple[str, ...] = (), prefer_venv: bool = False,
           optional: bool = False, *, _exec=os.execv) -> None:
    """Returns when the running interpreter is fit to import what the command
    needs; otherwise re-execs into the venv (not returning), returns having
    found nothing (`optional=True`), or exits 1.

    `script` is the command's `__file__`; the invoked path is `sys.argv[0]`.
    """
    missing = [m for m in requires if importlib.util.find_spec(m) is None]
    # The marker spans only the one exec it guards. A process that is fit
    # clears it so its own children (agentctl, knowledge-ctx, ...) may
    # re-exec for themselves; one that still lacks the modules keeps it.
    if prefer_venv:
        if sys.prefix != sys.base_prefix:
            os.environ.pop(MARKER, None)
            return
    elif not missing:
        os.environ.pop(MARKER, None)
        return

    # optional: a re-exec that still lacks the module (the marker is already
    # set) must not try again — return and let the caller's own fail-soft
    # carry on, rather than probing candidates a second time for nothing.
    if optional and os.environ.get(MARKER):
        os.environ.pop(MARKER, None)
        return

    invoked = sys.argv[0] if sys.argv and sys.argv[0] else None
    venv = find_venv(script, invoked, requires)
    if venv is not None and not _in_venv(venv) and not os.environ.get(MARKER):
        os.environ[MARKER] = "1"
        py = str(venv / VENV_PYTHON)
        _exec(py, [py] + sys.argv)
        return  # only reached when _exec is stubbed
    if not missing or optional:
        os.environ.pop(MARKER, None)
        return  # prefer_venv (or optional), with no venv to move to: carry on

    prog = os.path.basename(script)
    lacking = [c for c in candidate_venvs(script, invoked) if _has_interpreter(c)]
    if venv is not None:
        print(f"{prog}: needs the Python package(s) {', '.join(missing)}. "
              f"{venv} has them, but this process was already re-executed once "
              f"and still lacks them, so it will not re-exec again.",
              file=sys.stderr)
    elif lacking:
        print(f"{prog}: the virtualenv(s) "
              f"{', '.join(str(c) for c in lacking)} lack the Python package(s) "
              f"{', '.join(missing)}. Re-run install.sh to reinstall "
              f"requirements.txt into it.", file=sys.stderr)
    else:
        looked = "\n".join(f"  {c}" for c in candidate_venvs(script, invoked))
        print(f"{prog}: needs the Python package(s) {', '.join(missing)}, which "
              f"{sys.executable} does not have, and no virtualenv was found. "
              f"Looked in:\n{looked}\nRun install.sh, which creates "
              f"$HOME/.local/lib/knowledge/.venv.", file=sys.stderr)
    sys.exit(1)
