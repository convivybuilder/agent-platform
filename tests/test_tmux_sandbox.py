"""tests/tmux_sandbox.py: the probe that makes real-tmux tests skip inside a run's sandbox.

The probe must say "sandboxed" only for tmux's own socket refusal. Any other failure
must leave the tests running, or a broken tmux would pass as a sandbox skip.
"""

import subprocess
from types import SimpleNamespace

import pytest

import tmux_sandbox


@pytest.fixture(autouse=True)
def _fresh_probe():
    tmux_sandbox.sandbox_refusal.cache_clear()
    yield
    tmux_sandbox.sandbox_refusal.cache_clear()


def _stub_probe(monkeypatch, *, start_rc, start_err):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if "start-server" in argv:
            return SimpleNamespace(returncode=start_rc, stdout="", stderr=start_err)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(tmux_sandbox.shutil, "which", lambda name: "/usr/bin/tmux")
    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_the_socket_refusal_is_a_sandbox_and_skips_with_the_reason(monkeypatch):
    _stub_probe(monkeypatch, start_rc=1,
                start_err="error creating /tmp/tmux-probe-x/probe (Operation not permitted)\n")
    reason = tmux_sandbox.sandbox_refusal()
    assert reason is not None
    assert "Operation not permitted" in reason and "runs in CI" in reason


def test_a_successful_start_is_not_a_sandbox(monkeypatch):
    calls = _stub_probe(monkeypatch, start_rc=0, start_err="")
    assert tmux_sandbox.sandbox_refusal() is None
    # The probe cleans up the server it started.
    assert any("kill-server" in argv for argv in calls)


def test_another_failure_is_not_mistaken_for_a_sandbox(monkeypatch):
    _stub_probe(monkeypatch, start_rc=1, start_err="protocol version mismatch\n")
    assert tmux_sandbox.sandbox_refusal() is None


def test_no_tmux_binary_is_not_a_sandbox(monkeypatch):
    monkeypatch.setattr(tmux_sandbox.shutil, "which", lambda name: None)
    assert tmux_sandbox.sandbox_refusal() is None
