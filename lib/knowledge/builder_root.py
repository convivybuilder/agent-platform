"""The knowledge package's path to `lib/agent-platform/builder_root.py`, the
single implementation (company/decisions/0008).

This file used to be a git symlink to `../agent-platform/builder_root.py`, so
that both `import builder_root` call sites — this one and `bin/knowledge-ctx`'s
— shared the one implementation, standard-library only, which is what makes it
safe to import into the knowledge venv. An export of this repository writes a
symlink as its target's TEXT rather than following it, which would have shipped
this file as a copy of that docstring instead of the module it names. So this
compiles the real file's source and runs it directly in THIS module's own
`globals()` — the same thing Python's import machinery does to any module's
top-level code — so every name `lib/agent-platform/builder_root.py` defines
lands here exactly as if this were that file, with no second copy to drift.
"""
from __future__ import annotations

from pathlib import Path

_REAL_PATH = Path(__file__).resolve().parent.parent / "agent-platform" / "builder_root.py"
exec(compile(_REAL_PATH.read_text(), str(_REAL_PATH), "exec"), globals())
