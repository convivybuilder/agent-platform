"""CQ·586: two things this repo used to hand-keep now come from the project
registry instead — `knowledge.ALLOWED_CREWS` (`projects.legal_crews()`) and
`bin/ci1-heartbeat`'s repository list (`projects.ci1_heartbeat_repos()`).
Every registry here is invented for the test, named after nothing a real
Convivy crew or repo answers to.

The crew tests run in a FRESH subprocess, the same reason
test_knowledge_apps_crew_optional.py's do: `knowledge.py` reads the registry
at MODULE IMPORT TIME, before any in-process fixture could repoint it.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LIB_AGENT_PLATFORM = REPO / "lib" / "agent-platform"
LIB_KNOWLEDGE = REPO / "lib" / "knowledge"


def _operator_config(tmp_path: Path) -> Path:
    path = tmp_path / "operator.json"
    path.write_text(json.dumps({
        "excluded_crews": [],
        "operator_marker": "operator",
        "roster_crew": "operator-crew",
        "github_login": "operator-login",
        "tailnet_host": "tailnet.example",
    }))
    return path


def _run(code: str, tmp_path: Path, config_path: Path,
         projects_json: Path | None) -> subprocess.CompletedProcess:
    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": f"{LIB_AGENT_PLATFORM}:{LIB_KNOWLEDGE}",
        "CONVIVY_OPERATOR_CONFIG": str(config_path),
        "BUILDER_ROOT": str(tmp_path / "builder-root"),
    }
    if projects_json is not None:
        env["PROJECTS_JSON"] = str(projects_json)
    return subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env,
                           capture_output=True, text=True, check=False)


def test_allowed_crews_follows_the_registrys_crew_set(tmp_path):
    config = _operator_config(tmp_path)
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({
        "widget-crew": {"crew": "widget-crew"},
        "globex-crew": {"crew": "globex-crew"},
    }))
    code = (
        "import knowledge\n"
        "assert {'widget-crew', 'globex-crew'} <= knowledge.ALLOWED_CREWS, knowledge.ALLOWED_CREWS\n"
        "print('OK')\n"
    )
    out = _run(code, tmp_path, config, registry)
    assert out.returncode == 0, out.stderr
    assert "OK" in out.stdout


def test_a_crew_absent_from_the_registry_is_not_allowed(tmp_path):
    config = _operator_config(tmp_path)
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"widget-crew": {"crew": "widget-crew"}}))
    code = (
        "import knowledge\n"
        "assert 'globex-crew' not in knowledge.ALLOWED_CREWS, knowledge.ALLOWED_CREWS\n"
        "print('OK')\n"
    )
    out = _run(code, tmp_path, config, registry)
    assert out.returncode == 0, out.stderr
    assert "OK" in out.stdout


def test_an_unreadable_registry_refuses_every_binds_crews_value(tmp_path):
    config = _operator_config(tmp_path)
    # A PROJECTS_JSON naming a file that is not there: `_load_raw()` raises,
    # `knowledge._legal_crews()` catches it and returns an empty frozenset —
    # never "every crew allowed".
    registry = tmp_path / "does-not-exist.json"
    code = (
        "import knowledge\n"
        "assert knowledge.ALLOWED_CREWS == frozenset({'operator-crew'}), knowledge.ALLOWED_CREWS\n"
        "fm = {'scope': 'crew', 'binds_crews': ['widget-crew']}\n"
        "errs = knowledge.instruction_block_errors(fm, 'body text')\n"
        "assert any('unknown crew' in e for e in errs), errs\n"
        "print('OK')\n"
    )
    out = _run(code, tmp_path, config, registry)
    assert out.returncode == 0, out.stderr
    assert "OK" in out.stdout


# --- the LIVE crew set `instruction_block_errors` checks against, as of the --------
# call rather than this module's own import (`knowledge._current_allowed_crews()`,
# the long-running-daemon fix round 1 of this PR adds). Each test writes its
# registry change to the SAME file `PROJECTS_JSON` already names, in the SAME
# subprocess that imported `knowledge` once, with no reimport and no cache
# clear — the daemon case (`bin/knowledge-watcher`, `bin/mcp-kb-server`) that
# never calls `projects._load_raw.cache_clear()`. The mtime bump after each
# rewrite (inside the subprocess code, not here) is needed because
# `_load_raw_live()` keys its cache on (resolved path, mtime_ns, size), and
# two writes close together can otherwise land on the same mtime_ns and read
# back the cached, now-stale, parse.

def test_a_crew_added_after_import_is_accepted_with_no_reimport(tmp_path):
    config = _operator_config(tmp_path)
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"widget-crew": {"crew": "widget-crew"}}))
    code = (
        "import json, os, time\n"
        "import knowledge\n"
        "assert 'globex-crew' not in knowledge.ALLOWED_CREWS, knowledge.ALLOWED_CREWS\n"
        f"registry = {str(registry)!r}\n"
        "data = json.loads(open(registry).read())\n"
        "data['globex-crew'] = {'crew': 'globex-crew'}\n"
        "open(registry, 'w').write(json.dumps(data))\n"
        "future = time.time() + 5\n"
        "os.utime(registry, (future, future))\n"
        "fm = {'scope': 'crew', 'binds_crews': ['globex-crew']}\n"
        "errs = knowledge.instruction_block_errors(fm, 'body text')\n"
        "assert errs == [], errs\n"
        "print('OK')\n"
    )
    out = _run(code, tmp_path, config, registry)
    assert out.returncode == 0, out.stderr
    assert "OK" in out.stdout


def test_a_registry_unreadable_at_import_and_readable_at_call_time_accepts_a_crew(tmp_path):
    config = _operator_config(tmp_path)
    # Does not exist YET: unreadable at import, so the import-time baseline's
    # registry-derived half is empty — the outage case.
    registry = tmp_path / "projects.json"
    code = (
        "import json\n"
        "import knowledge\n"
        "assert 'widget-crew' not in knowledge.ALLOWED_CREWS, knowledge.ALLOWED_CREWS\n"
        f"registry = {str(registry)!r}\n"
        "open(registry, 'w').write(json.dumps({'widget-crew': {'crew': 'widget-crew'}}))\n"
        "fm = {'scope': 'crew', 'binds_crews': ['widget-crew']}\n"
        "errs = knowledge.instruction_block_errors(fm, 'body text')\n"
        "assert errs == [], errs\n"
        "print('OK')\n"
    )
    out = _run(code, tmp_path, config, registry)
    assert out.returncode == 0, out.stderr
    assert "OK" in out.stdout


def test_a_registry_unreadable_at_call_time_refuses_a_registered_crew(tmp_path):
    config = _operator_config(tmp_path)
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"widget-crew": {"crew": "widget-crew"}}))
    code = (
        "import os\n"
        "import knowledge\n"
        "assert 'widget-crew' in knowledge.ALLOWED_CREWS, knowledge.ALLOWED_CREWS\n"
        f"registry = {str(registry)!r}\n"
        "os.remove(registry)\n"
        "fm = {'scope': 'crew', 'binds_crews': ['widget-crew']}\n"
        "errs = knowledge.instruction_block_errors(fm, 'body text')\n"
        "assert any('unknown crew' in e for e in errs), errs\n"
        "print('OK')\n"
    )
    out = _run(code, tmp_path, config, registry)
    assert out.returncode == 0, out.stderr
    assert "OK" in out.stdout


def test_a_crew_removed_after_import_is_refused(tmp_path):
    config = _operator_config(tmp_path)
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({
        "widget-crew": {"crew": "widget-crew"},
        "globex-crew": {"crew": "globex-crew"},
    }))
    code = (
        "import json, os, time\n"
        "import knowledge\n"
        "assert 'globex-crew' in knowledge.ALLOWED_CREWS, knowledge.ALLOWED_CREWS\n"
        f"registry = {str(registry)!r}\n"
        "open(registry, 'w').write(json.dumps({'widget-crew': {'crew': 'widget-crew'}}))\n"
        "future = time.time() + 5\n"
        "os.utime(registry, (future, future))\n"
        "fm = {'scope': 'crew', 'binds_crews': ['globex-crew']}\n"
        "errs = knowledge.instruction_block_errors(fm, 'body text')\n"
        "assert any('unknown crew' in e for e in errs), errs\n"
        "print('OK')\n"
    )
    out = _run(code, tmp_path, config, registry)
    assert out.returncode == 0, out.stderr
    assert "OK" in out.stdout


# --- bin/ci1-heartbeat: the repository list follows the registry -----------------

def _load_heartbeat():
    loader = importlib.machinery.SourceFileLoader(
        "ci1_heartbeat_registry_test", str(REPO / "bin" / "ci1-heartbeat"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def hb():
    return _load_heartbeat()


def test_heartbeats_repository_list_follows_the_registry(hb, monkeypatch, tmp_path):
    import projects
    registry = tmp_path / "projects.json"
    repos = ["acme/widget-ci", "acme/globex-ci"]
    registry.write_text(json.dumps({"_ci1_heartbeat_repos": repos}))
    monkeypatch.setattr(projects, "PROJECTS_JSON", registry)
    projects._load_raw.cache_clear()
    assert hb.load_repos() == repos


def test_an_unreadable_registry_is_an_error_for_the_heartbeat(hb, monkeypatch, tmp_path):
    import projects
    monkeypatch.setattr(projects, "PROJECTS_JSON", tmp_path / "does-not-exist.json")
    projects._load_raw.cache_clear()
    with pytest.raises(hb.CheckFailed, match="repository list"):
        hb.load_repos()
