"""Knowledge-layer telemetry helper (lib/knowledge/kb_telemetry.py, ARCH-121).

Guards the contract the /kb dashboard + eval harness read: append() writes one
well-formed JSON line per call, stamps a UTC `ts` + caller fields, and NEVER
raises — telemetry must not break retrieval or individuation. Also pins the
defensive wrapper in knowledge-ctx (a missing telemetry module → no-op, not a
crash).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

LIB = Path(__file__).resolve().parents[1] / "lib" / "knowledge"
sys.path.insert(0, str(LIB))
import kb_telemetry  # noqa: E402

_ENV_KEYS = ("AGENTCTL_RUN_ID", "AGENTCTL_AGENT_NAME", "AGENTCTL_REPO")


def test_append_writes_one_line_with_utc_ts(tmp_path, monkeypatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    log = tmp_path / "q.jsonl"
    kb_telemetry.append(log, {"tool": "kb_search", "results": 3, "empty": False})
    kb_telemetry.append(log, {"tool": "kb_get", "found": True})
    lines = log.read_text().splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec["tool"] == "kb_search" and rec["results"] == 3 and rec["empty"] is False
    assert "ts" in rec and rec["ts"].endswith("+00:00")  # timezone-aware UTC
    assert "run_id" not in rec  # no caller env set


def test_append_includes_caller_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCTL_RUN_ID", "run-abc")
    monkeypatch.setenv("AGENTCTL_AGENT_NAME", "coder")
    monkeypatch.setenv("AGENTCTL_REPO", "vivy")
    log = tmp_path / "q.jsonl"
    kb_telemetry.append(log, {"tool": "kb_search"})
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["run_id"] == "run-abc" and rec["agent"] == "coder" and rec["repo"] == "vivy"


def test_caller_env_empty_when_unset(monkeypatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    assert kb_telemetry.caller_env() == {}


def test_append_never_raises_on_unwritable_path(tmp_path):
    # Parent dir missing and not created by append → open('a') raises internally;
    # append must swallow it and write nothing.
    bad = tmp_path / "nope" / "deeper" / "q.jsonl"
    kb_telemetry.append(bad, {"tool": "kb_search"})  # must not raise
    assert not bad.exists()


def test_knowledge_ctx_log_wrapper_delegates_and_tolerates_missing(knowledge_ctx, monkeypatch, tmp_path):
    calls = []

    class _Stub:
        QUERY_LOG = tmp_path / "q.jsonl"
        INDIVIDUATE_LOG = tmp_path / "m.jsonl"

        def append(self, path, rec):
            calls.append((path, rec))

    monkeypatch.setattr(knowledge_ctx, "kb_telemetry", _Stub())
    knowledge_ctx._log_individuation({"outcome": "written", "agents": 7})
    assert len(calls) == 1
    assert calls[0][0] == _Stub.INDIVIDUATE_LOG and calls[0][1]["outcome"] == "written"

    # Module absent (not installed) → wrapper is a no-op, never raises.
    monkeypatch.setattr(knowledge_ctx, "kb_telemetry", None)
    knowledge_ctx._log_individuation({"outcome": "noop"})
