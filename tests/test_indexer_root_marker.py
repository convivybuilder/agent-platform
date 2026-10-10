"""The indexer's report-only check that the knowledge tree sits under this
machine's confirmed root (company/decisions/0008)."""
import importlib.util
import types
from pathlib import Path

import pytest

import indexer

ROOT = Path(__file__).resolve().parent.parent


def _real_builder_root():
    spec = importlib.util.spec_from_file_location(
        "builder_root_for_test", ROOT / "lib" / "agent-platform" / "builder_root.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """The indexer pointed at a temporary machine root whose knowledge tree is
    exactly <root>/knowledge, as it is when resolved rather than overridden."""
    root = tmp_path / "builder"
    (root / "knowledge").mkdir(parents=True)
    real = _real_builder_root()
    stub = types.SimpleNamespace(root=lambda: root, marker_problem=real.marker_problem)
    monkeypatch.setattr(indexer, "_BR", stub)
    monkeypatch.setattr(indexer, "_EXPLICIT_ROOT", None)
    monkeypatch.setattr(indexer, "KNOWLEDGE", root / "knowledge")
    # PROJECTS follows KNOWLEDGE the same way it does in production (`PROJECTS =
    # KNOWLEDGE / "projects"`, indexer.py:65). Left unpatched, a full scan below
    # would walk whatever PROJECTS resolved to at import — on 2026-09-12 that was
    # a directory a missing builder_root.CONFIG made nonexistent, which is exactly
    # what let full_scan_and_reconcile() delete every row of the real index
    # (issue_01M2C0K5C228VGJRV04THX67Q4).
    monkeypatch.setattr(indexer, "PROJECTS", root / "knowledge" / "projects")
    monkeypatch.setattr(indexer, "_ROOT_REPORTED", False)
    return root


def test_a_confirmed_root_reports_nothing(machine, capsys):
    (machine / ".builder-root").write_text(f"{machine.resolve()}\n")
    assert indexer.report_unconfirmed_root() is None
    assert capsys.readouterr().err == ""


def test_an_unconfirmed_root_is_reported_once_and_never_refused(machine, capsys):
    first = indexer.report_unconfirmed_root()
    second = indexer.report_unconfirmed_root()
    err = capsys.readouterr().err
    assert first and second, "the problem is returned every time"
    assert err.count("knowledge root not confirmed (report-only)") == 1
    assert f"root() = {str(machine.resolve())!r}: " in err, \
        "the line names the root and its resolved path"


def test_a_stale_tree_with_its_own_old_marker_is_still_reported(machine, capsys):
    """The case the condition names: a moved tree's marker names where it USED to
    be, which is not the configured root."""
    (machine / ".builder-root").write_text("/srv/convivy/moved-elsewhere\n")
    assert indexer.report_unconfirmed_root()


def test_an_explicit_knowledge_root_is_exempt(machine, monkeypatch, capsys):
    """The documented index repair points KNOWLEDGE_ROOT at a detached worktree,
    which has no marker; the check must not apply to it."""
    monkeypatch.setattr(indexer, "_EXPLICIT_ROOT", "/tmp/kb-main")
    assert indexer.report_unconfirmed_root() is None
    assert capsys.readouterr().err == ""


def test_a_repointed_module_is_exempt(machine, monkeypatch, capsys, tmp_path):
    """Tests and tools that set indexer.KNOWLEDGE directly are not reading the
    machine's root, so there is nothing of the root's to confirm."""
    monkeypatch.setattr(indexer, "KNOWLEDGE", tmp_path / "some-corpus")
    assert indexer.report_unconfirmed_root() is None


def test_no_resolver_means_no_check(machine, monkeypatch):
    monkeypatch.setattr(indexer, "_BR", None)
    assert indexer.report_unconfirmed_root() is None


def test_an_indexer_without_its_resolver_refuses_loudly(tmp_path):
    """No guessed knowledge tree when builder_root cannot be imported: the import
    fails, and stderr names what failed (company/decisions/0008). A fresh process
    against copies of the two modules with no builder_root beside them.
    `operator_config` IS copied alongside, with a config of its own, so that
    this failure is the builder_root one under test rather than knowledge.py's
    own unrelated operator-config import, which runs first."""
    import json
    import shutil
    import subprocess
    import sys
    for name in ("indexer.py", "knowledge.py"):
        shutil.copy(ROOT / "lib" / "knowledge" / name, tmp_path / name)
    shutil.copy(ROOT / "lib" / "agent-platform" / "operator_config.py", tmp_path / "operator_config.py")
    operator_cfg = tmp_path / "operator.json"
    operator_cfg.write_text(json.dumps({"excluded_crews": [], "operator_marker": "operator",
                                        "roster_crew": "operator-crew"}))
    out = subprocess.run([sys.executable, "-c", "import indexer"], cwd=tmp_path,
                         env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(tmp_path),
                              "CONVIVY_OPERATOR_CONFIG": str(operator_cfg)},
                         capture_output=True, text=True, check=False)
    assert out.returncode != 0, "the indexer must not load without its resolver"
    assert "cannot import builder_root" in out.stderr, out.stderr


def test_both_ingestion_passes_run_the_check(machine, monkeypatch):
    calls = []
    monkeypatch.setattr(indexer, "report_unconfirmed_root", lambda: calls.append(1))
    monkeypatch.setattr(indexer, "index_one", lambda p, roster=None: ("skipped", None))
    indexer.index_paths(["projects/x.md"])
    result = indexer.full_scan_and_reconcile()
    assert len(calls) == 2
    # PROJECTS is the fixture's own empty tree, so the scan finds nothing to
    # index and nothing to reconcile away — asserted rather than swallowed, so a
    # future full_scan_and_reconcile() that raises here is a failure, not silence.
    assert result["indexed"] == 0
    assert result["removed"] == 0
