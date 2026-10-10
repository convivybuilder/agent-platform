"""operator_config — the one file naming this machine's human operator, read
from OUTSIDE the repo so that naming the operator never requires a commit.

Before this module, the operator's identity, crew slug, excluded-crew list,
GitHub login and tailnet host were hardcoded literals scattered across
memory_archive.py, knowledge.py, kb_audit.py, velocity-report and
session-view — every one of them a finding against `bin/publish-engine`'s
export gate (CQ·428), and every one a value that changes per deployment of
this platform, not per commit.

$CONVIVY_OPERATOR_CONFIG names the file; tests point it at a tmp_path file of
their own. Its absence falls back to `config_path()`'s default, this
machine's `$HOME/.config/convivy/operator.json` — never a path inside this
repo, so the file itself is never a candidate for export.

Every key is required and there is no in-repo default value for any of
them: a fallback here would let an unset key silently reach the wrong
operator identity, crew, or host into a scan, an archive job, or a published
document. `get()` raises `OperatorConfigError` naming the missing key and the
file read; each caller decides its own fail-closed shape (refuse to run,
report "unconfigured", skip the step) on top of that.

See `config/operator.example.json` for the full key list, with synthetic
values.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

ENV_VAR = "CONVIVY_OPERATOR_CONFIG"


class OperatorConfigError(RuntimeError):
    """The operator config file is missing, unreadable, not valid JSON, not a
    JSON object, or lacks a key a caller asked for."""


def config_path() -> Path:
    """`$CONVIVY_OPERATOR_CONFIG` if set, else `$HOME/.config/convivy/operator.json`.
    Read fresh on every call rather than cached, so a test's `monkeypatch.setenv`
    always takes effect on the next `get()`."""
    env = os.environ.get(ENV_VAR)
    return Path(env) if env else Path.home() / ".config" / "convivy" / "operator.json"


def _load(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        raise OperatorConfigError(f"operator config not readable at {path}: {e}") from e
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise OperatorConfigError(f"operator config at {path} is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise OperatorConfigError(f"operator config at {path} must be a JSON object")
    return data


def get(key: str) -> Any:
    """The configured value for *key*. Raises `OperatorConfigError`, naming
    *key* and the file's path, when the file is missing, unreadable,
    malformed, or lacks *key* — never a default, per the module docstring."""
    path = config_path()
    data = _load(path)
    if key not in data:
        raise OperatorConfigError(f"operator config at {path} has no key {key!r}")
    return data[key]
