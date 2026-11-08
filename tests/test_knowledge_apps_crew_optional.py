"""`apps_crew` is optional in the operator config: it names a crew only some
installs have, so a missing key must mean "this install has none", never a
reason for `knowledge.py` to fail at import.

Both tests run in a FRESH subprocess with their own operator config, never
through `conftest.py`'s module-scoped fixture — that fixture always supplies
`apps_crew` (so that `test_allowed_crews_is_the_registrys_crew_set` and
`test_the_renamed_private_crew_resolves_and_its_old_key_is_gone` in
test_projects.py keep checking the real, present-key case), and `knowledge.py`
reads the key at MODULE IMPORT TIME, before any in-process fixture could
repoint it.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB_AGENT_PLATFORM = ROOT / "lib" / "agent-platform"
LIB_KNOWLEDGE = ROOT / "lib" / "knowledge"


def _operator_config_without_apps_crew(tmp_path: Path) -> Path:
    path = tmp_path / "operator.json"
    path.write_text(json.dumps({
        "excluded_crews": [],
        "operator_marker": "operator",
        "roster_crew": "operator-crew",
        "github_login": "operator-login",
        "tailnet_host": "tailnet.example",
    }))
    return path


def _registry_with_crews(tmp_path: Path, crews: list[str]) -> Path:
    # CQ·586: `knowledge.ALLOWED_CREWS` is derived from the registry
    # (`projects.legal_crews()`) rather than a hand-kept literal, so a test
    # of its content needs its own registry rather than reading the real one
    # — this subprocess has no PROJECTS_JSON by default and no Library to ask.
    path = tmp_path / "projects.json"
    path.write_text(json.dumps({slug: {"crew": slug} for slug in crews}))
    return path


def _run(code: str, tmp_path: Path, config_path: Path,
         projects_json: Path | None = None) -> subprocess.CompletedProcess:
    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": f"{LIB_AGENT_PLATFORM}:{LIB_KNOWLEDGE}",
        "CONVIVY_OPERATOR_CONFIG": str(config_path),
        # Isolates registry.py's data root from this machine's real
        # ~/Convivy/agent-runs, same reasoning as conftest.py's own BUILDER_ROOT.
        "BUILDER_ROOT": str(tmp_path / "builder-root"),
    }
    if projects_json is not None:
        env["PROJECTS_JSON"] = str(projects_json)
    return subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env,
                           capture_output=True, text=True, check=False)


def test_knowledge_imports_with_no_apps_crew_key(tmp_path):
    config = _operator_config_without_apps_crew(tmp_path)
    registry = _registry_with_crews(tmp_path, ["bosun", "library", "steward", "operator-crew"])
    code = (
        "import knowledge\n"
        "assert knowledge.APPS_CREW is None, knowledge.APPS_CREW\n"
        "assert None not in knowledge.ALLOWED_CREWS, knowledge.ALLOWED_CREWS\n"
        "assert {'bosun', 'library', 'steward', knowledge.ROSTER_CREW} <= knowledge.ALLOWED_CREWS\n"
        "print('OK')\n"
    )
    out = _run(code, tmp_path, config, projects_json=registry)
    assert out.returncode == 0, out.stderr
    assert "OK" in out.stdout


def test_registry_limits_does_not_raise_with_no_apps_crew_key(tmp_path):
    """The chain the round-1 reviewer traced: bin/agentctl -> registry.limits()
    -> agent_policy -> knowledge. `agent_policy` imports `knowledge` at module
    level, OUTSIDE `limits()`'s own try/except (it falls back only on a
    failure from reading the policy FILE, not on the import that precedes it)
    — so on head 535a8f88c, a missing `apps_crew` raised `OperatorConfigError`
    straight out of this call, crashing every `agentctl spawn` and the
    knowledge-watcher job on an install with no apps crew."""
    config = _operator_config_without_apps_crew(tmp_path)
    # `agent_policy` imports `knowledge` at module level, which now computes
    # ALLOWED_CREWS from the registry (CQ·586) — a PROJECTS_JSON here, even an
    # empty one, keeps this subprocess (which sets no HOME of its own) from
    # resolving `Path.home()` to this machine's real credential file and
    # reaching for the real Library.
    registry = _registry_with_crews(tmp_path, [])
    code = (
        "import registry\n"
        "limits = registry.limits()\n"
        "print('OK', limits)\n"
    )
    out = _run(code, tmp_path, config, projects_json=registry)
    assert out.returncode == 0, out.stderr
    assert "OK" in out.stdout


def test_knowledge_still_raises_on_an_unreadable_operator_config(tmp_path):
    """The fix is the missing KEY, not the missing/broken FILE: `get_optional`
    raises exactly as `get()` does when the file itself can't be trusted, and
    that must still stop the import cold."""
    missing_config = tmp_path / "does-not-exist.json"
    code = "import knowledge\n"
    out = _run(code, tmp_path, missing_config)
    assert out.returncode != 0
    assert "OperatorConfigError" in out.stderr
    assert "not readable" in out.stderr


def test_knowledge_still_raises_on_invalid_json(tmp_path):
    config = tmp_path / "operator.json"
    config.write_text("{not valid json")
    code = "import knowledge\n"
    out = _run(code, tmp_path, config)
    assert out.returncode != 0
    assert "OperatorConfigError" in out.stderr
    assert "not valid JSON" in out.stderr
