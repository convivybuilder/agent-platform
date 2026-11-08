"""`needs_kb`'s skip condition (epic E33 slice 2, PR #1028 round 2).

A dispatched run's sandbox denies DATA reads (open, listdir) under the real
account home while leaving METADATA reads (`stat`, `is_dir()`) open on a path
outside the read allowlist — which the real knowledge checkout always is.
`pytest_runtest_setup` used to decide the `needs_kb` skip from `is_dir()`
alone, so a dispatched coder's own pytest run read a present-but-unreadable
checkout as PRESENT and let the guard's body run, where it then either hit a
loud `PermissionError` or a silently empty listing (`Path.glob` swallows the
same error from its own internal `os.scandir`). `_kb_checkout_listable` is
the fix, tested here directly against a simulated denial — never against a
real path on this machine, since this suite runs on a machine with no such
checkout either way.
"""
from __future__ import annotations

import os
import warnings
from pathlib import Path

import pytest

from conftest import _kb_checkout_listable, pytest_runtest_setup


def test_a_present_but_unlistable_checkout_is_treated_as_absent(tmp_path, monkeypatch):
    """Simulates the sandbox's own split: the directory exists (`is_dir()`
    would say so), but listing it raises `PermissionError`, the way
    `os.listdir` does under the denial this guard exists to survive."""
    root = tmp_path / "projects"
    root.mkdir()
    assert root.is_dir()

    real_listdir = os.listdir

    def _denied(path="."):
        if Path(os.fspath(path)) == root:
            raise PermissionError(1, "Operation not permitted", str(path))
        return real_listdir(path)

    monkeypatch.setattr(os, "listdir", _denied)
    assert _kb_checkout_listable(root) is False


def test_a_listable_checkout_is_true(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    (root / "agent-platform").mkdir()
    assert _kb_checkout_listable(root) is True


def test_a_missing_checkout_is_false_not_unlistable(tmp_path):
    """An absent checkout (CI's own case) must read exactly as it always
    did: False, with no warning raised — `_kb_checkout_listable` catches
    `FileNotFoundError` the same as `PermissionError`, both answering
    'cannot be used as the real KB here'."""
    root = tmp_path / "never-created"
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert _kb_checkout_listable(root) is False


class _FakeMarker:
    pass


class _FakeItem:
    def __init__(self, marker) -> None:
        self._marker = marker

    def get_closest_marker(self, name):
        return self._marker if name == "needs_kb" else None


def test_pytest_runtest_setup_skips_a_needs_kb_test_against_an_unlistable_checkout(
        tmp_path, monkeypatch):
    """The integration point: before this fix, `pytest_runtest_setup` asked
    only `is_dir()` and let a needs_kb test run its body against a checkout
    this fixture makes present but unreadable. After the fix it skips, the
    same outcome an absent checkout has always produced."""
    root = tmp_path / "knowledge"
    (root / "projects").mkdir(parents=True)

    def _fake_knowledge_repo():
        return root

    monkeypatch.setattr("conftest.knowledge_repo", _fake_knowledge_repo)

    real_listdir = os.listdir
    denied = root / "projects"

    def _denied(path="."):
        if Path(os.fspath(path)) == denied:
            raise PermissionError(1, "Operation not permitted", str(path))
        return real_listdir(path)

    monkeypatch.setattr(os, "listdir", _denied)

    item = _FakeItem(_FakeMarker())
    with pytest.raises(pytest.skip.Exception, match="no knowledge checkout"):
        pytest_runtest_setup(item)


def test_pytest_runtest_setup_does_not_skip_a_needs_kb_test_against_a_listable_checkout(
        tmp_path, monkeypatch):
    root = tmp_path / "knowledge"
    (root / "projects" / "agent-platform").mkdir(parents=True)
    monkeypatch.setattr("conftest.knowledge_repo", lambda: root)

    item = _FakeItem(_FakeMarker())
    pytest_runtest_setup(item)  # must not raise


def test_pytest_runtest_setup_is_a_no_op_for_a_test_with_no_marker(tmp_path, monkeypatch):
    """A test that never carries `needs_kb` must never consult the checkout at
    all, readable or not — `knowledge_repo()` is never even called."""
    def _boom():
        raise AssertionError("knowledge_repo() must not be called for an unmarked test")

    monkeypatch.setattr("conftest.knowledge_repo", _boom)
    item = _FakeItem(None)
    pytest_runtest_setup(item)  # must not raise
