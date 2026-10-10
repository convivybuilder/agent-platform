"""bin/orchestrator-tokens (ARCH-129) — the per-night token/turn report.

The risk this guards: a wrong aggregation would make the A+B prompt changes
look better or worse than they are, and the C decision gate (phase-split the
night) keys off these numbers.
"""
from __future__ import annotations

import json


def _line(usage=None, tools=(), msg_id=None, request_id=None):
    content = [{"type": "tool_use", "name": n, "input": {}} for n in tools]
    msg = {"content": content}
    if usage:
        msg["usage"] = usage
    if msg_id is not None:
        msg["id"] = msg_id
    rec = {"message": msg}
    if request_id is not None:
        rec["requestId"] = request_id
    return json.dumps(rec)


def test_usage_totals_aggregates_requests_and_cache_split(orchestrator_tokens):
    lines = [
        _line(usage={"input_tokens": 100, "cache_creation_input_tokens": 900,
                     "cache_read_input_tokens": 0, "output_tokens": 50},
              tools=("Bash",)),
        _line(usage={"input_tokens": 10, "cache_creation_input_tokens": 0,
                     "cache_read_input_tokens": 1990, "output_tokens": 25},
              tools=("Bash", "Read")),          # batched calls: one request, two tools
        "not json",                              # tolerated
        _line(tools=("Write",)),                 # tool_use without usage: counted as a call only
    ]
    t = orchestrator_tokens.usage_totals(lines)
    assert t["api_requests"] == 2
    assert t["cache_read_tokens"] == 1990
    assert t["uncached_input_tokens"] == 1010   # input + cache_creation
    assert t["output_tokens"] == 75
    assert t["avg_context_tokens"] == (1990 + 1010) // 2
    assert t["tool_calls"] == 4
    assert t["tools"] == {"Bash": 2, "Read": 1, "Write": 1}


def test_usage_totals_empty(orchestrator_tokens):
    t = orchestrator_tokens.usage_totals([])
    assert t["api_requests"] == 0 and t["avg_context_tokens"] == 0


_USAGE = {"input_tokens": 100, "cache_creation_input_tokens": 0,
          "cache_read_input_tokens": 0, "output_tokens": 50}


def test_usage_totals_dedupes_a_message_written_four_times(orchestrator_tokens):
    """Claude Code writes the same assistant message's usage up to 4x in a long
    session, byte-identical. Summing every occurrence over-reports api_requests
    and every token count by that multiple."""
    line = _line(usage=_USAGE, msg_id="msg_1", request_id="req_1")
    t = orchestrator_tokens.usage_totals([line, line, line, line])
    assert t["api_requests"] == 1
    assert t["uncached_input_tokens"] == 100
    assert t["output_tokens"] == 50


def test_usage_totals_counts_two_distinct_messages_separately(orchestrator_tokens):
    lines = [
        _line(usage=_USAGE, msg_id="msg_1", request_id="req_1"),
        _line(usage=_USAGE, msg_id="msg_2", request_id="req_2"),
    ]
    t = orchestrator_tokens.usage_totals(lines)
    assert t["api_requests"] == 2
    assert t["uncached_input_tokens"] == 200
    assert t["output_tokens"] == 100


def test_usage_totals_counts_a_keyless_record_once_not_dropped(orchestrator_tokens):
    """A record with neither message.id nor requestId is counted, never
    dropped and never collapsed with another keyless record."""
    lines = [_line(usage=_USAGE), _line(usage=_USAGE)]
    t = orchestrator_tokens.usage_totals(lines)
    assert t["api_requests"] == 2
    assert t["uncached_input_tokens"] == 200


def test_orchestrator_runs_filters_by_agent(orchestrator_tokens, tmp_path):
    a = tmp_path / "run-orch"
    a.mkdir()
    (a / "meta.json").write_text(json.dumps({"agent": "overnight-orchestrator"}))
    b = tmp_path / "run-worker"
    b.mkdir()
    (b / "meta.json").write_text(json.dumps({"agent": "bug-fixer"}))
    runs = orchestrator_tokens.orchestrator_runs(tmp_path)
    assert [r.name for r, _ in runs] == ["run-orch"]


def test_is_orchestration_agent_covers_single_and_phase_split(orchestrator_tokens):
    # Single-session orchestrator + the phase-split stage agents (ARCH-268) count.
    for a in ("overnight-orchestrator", "planning-orchestrator",
              "overnight-triage", "overnight-fix-drain", "overnight-verify"):
        assert orchestrator_tokens.is_orchestration_agent(a), a
    # Workers and the digest are NOT orchestration cost (parity with single-session).
    for a in ("coder", "bug-fixer", "reviewer", "security-reviewer", "explainer", ""):
        assert not orchestrator_tokens.is_orchestration_agent(a), a


def test_orchestrator_runs_includes_phase_split_stages(orchestrator_tokens, tmp_path):
    # A phase-split night: triage + N fix-drain + verify are the orchestration
    # runs; the workers they dispatch are not summed (same as the old session).
    for name, agent in [("run-triage", "overnight-triage"),
                        ("run-fd1", "overnight-fix-drain"),
                        ("run-fd2", "overnight-fix-drain"),
                        ("run-verify", "overnight-verify"),
                        ("run-coder", "coder")]:
        d = tmp_path / name
        d.mkdir()
        (d / "meta.json").write_text(json.dumps({"agent": agent}))
    got = sorted(r.name for r, _ in orchestrator_tokens.orchestrator_runs(tmp_path))
    assert got == ["run-fd1", "run-fd2", "run-triage", "run-verify"]


def test_record_appends_idempotently_and_drops_tools(orchestrator_tokens, tmp_path, monkeypatch):
    # The lever-C gate (ARCH-129) reads this history, so a double-run on the same
    # night must not double-count. Slack is stubbed to a dry-run no-op.
    monkeypatch.setenv("AGENTCTL_SLACK_DRY_RUN", "1")
    monkeypatch.setattr(orchestrator_tokens, "HISTORY", tmp_path / "history.jsonl")
    rec = {"date": "2026-06-28", "run": "run-orch", "model": "claude-sonnet-4-6",
           "product": "vivy", "usd": 2.5, "api_requests": 120,
           "cache_read_tokens": 5_000_000, "uncached_input_tokens": 100_000,
           "output_tokens": 40_000, "avg_context_tokens": 42_500,
           "tool_calls": 30, "tools": {"Bash": 20, "Read": 10}}
    orchestrator_tokens._record("2026-06-28", [rec])
    orchestrator_tokens._record("2026-06-28", [rec])  # same night again — must not duplicate
    lines = (tmp_path / "history.jsonl").read_text().splitlines()
    assert len(lines) == 1
    saved = json.loads(lines[0])
    assert saved["date"] == "2026-06-28" and saved["run"] == "run-orch"
    assert saved["usd"] == 2.5 and saved["api_requests"] == 120
    assert "tools" not in saved  # the verbose tool histogram is dropped from the log


def test_record_writes_history_for_every_night(orchestrator_tokens, tmp_path, monkeypatch):
    # ARCH-313: history is recorded every night (the Slack overage alert that used
    # to fire above the baseline was removed when Slack was retired; history is the
    # durable record regardless of spend level).

    def rec(run, usd):
        monkeypatch.setattr(orchestrator_tokens, "HISTORY", tmp_path / f"{run}.jsonl")
        return [{"date": "2026-07-04", "run": run, "usd": usd, "api_requests": 100,
                 "cache_read_tokens": 1_000_000, "product": "sous-chef",
                 "agent": "overnight-triage"}]

    # Under any baseline: history is written.
    orchestrator_tokens._record("2026-07-04", rec("under", 2.62))
    assert (tmp_path / "under.jsonl").exists() and (tmp_path / "under.jsonl").read_text().strip()
    # Above any baseline: history is also written.
    orchestrator_tokens._record("2026-07-04", rec("over", 19.0))
    assert (tmp_path / "over.jsonl").exists() and (tmp_path / "over.jsonl").read_text().strip()
