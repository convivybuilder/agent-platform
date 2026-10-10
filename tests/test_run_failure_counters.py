"""`agentctl wait` and `agentctl status` must say how a non-zero run ended and what it
spent, from the files a finished run already has on disk.

THE GAP (E11·4). A run that burns millions of tokens and reports only `errored` reads
the same as one that died on a bad argument. An unattended orchestrator retries into
the same wall, and an attended one reaches a confident wrong cause from a two-line
stderr. The directory held the diagnosis; the operator-facing commands did not print it.

These tests write synthetic run directories under a temporary runs root, never the
real agent-runs tree, and call the real `cmd_status` and `_wait_for_runs` functions.
"""
from __future__ import annotations

import json
import os
import threading
from types import SimpleNamespace

import pytest

import telemetry as T

RUN_DAY = "2026-07-04"
BLOCKING_MEANING = ("the run's outcome reports terminal_reason blocking_limit. "
                    "Read the run's transcript before retrying.")


@pytest.fixture
def runs(agentctl, tmp_path, monkeypatch):
    root = tmp_path / "agent-runs"
    root.mkdir()
    monkeypatch.setattr(agentctl, "RUNS_ROOT", root)
    monkeypatch.setattr(T, "RUNS_ROOT", root)
    return root


def _make_run(root, rid, *, exit_status, terminal_reason=None, cost=True,
              tool_calls=3, files_modified=("a.py",)):
    rd = root / RUN_DAY / rid
    rd.mkdir(parents=True)
    (rd / "meta.json").write_text(json.dumps({
        "run_id": rid, "agent": "bug-fixer", "task": "t", "repo": "synthetic-repo",
        "worktree": "/synthetic/wt", "profile": "p", "model": "synthetic-model",
        "started_at": "2026-07-04T00:00:00Z", "pid": None}))
    outcome = {"exit_status": exit_status, "files_modified": list(files_modified),
               "files_added": [], "files_deleted": []}
    if terminal_reason is not None:
        outcome["terminal_reason"] = terminal_reason
    (rd / "outcome.json").write_text(json.dumps(outcome))
    if cost:
        (rd / "cost.json").write_text(json.dumps({
            "tokens_in": 4000, "tokens_out": 500, "usd": 0.0,
            "cost_source": "gateway_spend_log",
            "by_model": {"deepseek/synthetic-model": {
                "tokens_in": 4000, "tokens_out": 500, "usd": 0.0}}}))
    if tool_calls is not None:
        (rd / "tool_calls.jsonl").write_text(
            "".join(json.dumps({"tool_name": "Read"}) + "\n" for _ in range(tool_calls)))
    return rd


def _returns_within(fn, seconds=5):
    """Call `fn` on a worker thread. A call that blocks fails the assertion below
    instead of hanging the suite; a call that raises fails with its error."""
    outcome = {}

    def call():
        try:
            outcome["rc"] = fn()
        except Exception as exc:  # reported by the assertion below
            outcome["error"] = repr(exc)

    worker = threading.Thread(target=call, daemon=True)
    worker.start()
    worker.join(seconds)
    assert not worker.is_alive(), "call did not return: it blocked on a run-directory file"
    assert "error" not in outcome, outcome["error"]
    return outcome["rc"]


def test_wait_prints_terminal_reason_counters_and_the_named_message(agentctl, runs, capsys):
    _make_run(runs, "run-synth01", exit_status="errored", terminal_reason="blocking_limit")

    rc = agentctl._wait_for_runs(["run-synth01"], timeout=5, poll=0.01)
    captured = capsys.readouterr()

    assert rc == 4, captured.err
    assert ("run-synth01  errored  $0.0000  terminal_reason=blocking_limit"
            "  model=deepseek/synthetic-model  tokens=4500  tool_calls=3  files=1"
            ) in captured.out.splitlines(), captured.out
    assert f"wait: run-synth01 blocking_limit — {BLOCKING_MEANING}" in captured.err, \
        captured.err


def test_wait_prints_unknown_for_a_missing_cost_json_never_zero(agentctl, runs, capsys):
    _make_run(runs, "run-synth02", exit_status="errored", cost=False, tool_calls=0)

    rc = agentctl._wait_for_runs(["run-synth02"], timeout=5, poll=0.01)
    captured = capsys.readouterr()

    assert rc == 4, captured.err
    line = next(ln for ln in captured.out.splitlines() if ln.startswith("run-synth02"))
    assert line.endswith(
        "terminal_reason=unknown  model=unknown  tokens=unknown  tool_calls=0  files=1"), line
    assert "tokens=0" not in line, line


def test_a_successful_run_keeps_its_exact_wait_output(agentctl, runs, capsys):
    _make_run(runs, "run-synth03", exit_status="completed")

    rc = agentctl._wait_for_runs(["run-synth03"], timeout=5, poll=0.01)
    captured = capsys.readouterr()

    assert rc == 0, captured.err
    assert captured.out == "run-synth03  completed  $0.0000\n", captured.out
    assert "FAILED" not in captured.err, captured.err


def test_status_shows_the_counters_and_named_message_for_an_ended_run(
        agentctl, runs, capsys):
    _make_run(runs, "run-synth04", exit_status="errored", terminal_reason="blocking_limit")

    rc = agentctl.cmd_status(SimpleNamespace(run_id="run-synth04"))
    out = capsys.readouterr().out

    assert rc == 0
    assert "terminal_reason: blocking_limit" in out, out
    assert f"meaning:      {BLOCKING_MEANING}" in out, out
    assert ("counters:     model=deepseek/synthetic-model  tokens=4500  "
            "tool_calls=3  files=1") in out, out


def test_status_prints_unknown_counters_when_cost_and_reason_are_absent(
        agentctl, runs, capsys):
    _make_run(runs, "run-synth05", exit_status="errored", cost=False, tool_calls=0)

    agentctl.cmd_status(SimpleNamespace(run_id="run-synth05"))
    out = capsys.readouterr().out

    assert "terminal_reason:" not in out, out
    assert "counters:     model=unknown  tokens=unknown  tool_calls=0  files=1" in out, out


def test_status_prints_counters_for_a_successful_run(agentctl, runs, capsys):
    _make_run(runs, "run-synth06", exit_status="completed")

    rc = agentctl.cmd_status(SimpleNamespace(run_id="run-synth06"))
    out = capsys.readouterr().out

    assert rc == 0
    assert ("counters:     model=deepseek/synthetic-model  tokens=4500  "
            "tool_calls=3  files=1") in out, out


def test_a_fifo_tool_calls_file_prints_unknown_and_wait_returns(agentctl, runs, capsys):
    rd = _make_run(runs, "run-synth07", exit_status="errored",
                   terminal_reason="blocking_limit", tool_calls=None)
    os.mkfifo(rd / "tool_calls.jsonl")

    rc = _returns_within(lambda: agentctl._wait_for_runs(["run-synth07"],
                                                         timeout=5, poll=0.01))
    out = capsys.readouterr().out

    assert rc == 4
    assert out.splitlines()[-1].endswith(
        "terminal_reason=blocking_limit  model=deepseek/synthetic-model  tokens=4500"
        "  tool_calls=unknown  files=1"), out


def test_a_symlinked_cost_json_prints_unknown_not_its_target(agentctl, runs, capsys,
                                                             tmp_path):
    rd = _make_run(runs, "run-synth08", exit_status="errored", cost=False)
    target = tmp_path / "elsewhere-cost.json"
    target.write_text(json.dumps({
        "tokens_in": 4000, "tokens_out": 500, "usd": 0.0,
        "cost_source": "gateway_spend_log",
        "by_model": {"deepseek/synthetic-model": {"tokens_in": 4000, "tokens_out": 500}}}))
    os.symlink(target, rd / "cost.json")

    rc = _returns_within(lambda: agentctl._wait_for_runs(["run-synth08"],
                                                         timeout=5, poll=0.01))
    out = capsys.readouterr().out

    assert rc == 4
    assert out.splitlines()[-1].endswith(
        "model=unknown  tokens=unknown  tool_calls=3  files=1"), out


def test_an_infinite_token_count_prints_unknown_and_wait_returns(agentctl, runs, capsys):
    rd = _make_run(runs, "run-synth09", exit_status="errored", cost=False)
    (rd / "cost.json").write_text(
        '{"tokens_in": 1e400, "tokens_out": 500, "usd": 0.0, '
        '"cost_source": "gateway_spend_log", '
        '"by_model": {"deepseek/synthetic-model": {"tokens_in": 1, "tokens_out": 1}}}')

    rc = _returns_within(lambda: agentctl._wait_for_runs(["run-synth09"],
                                                         timeout=5, poll=0.01))
    out = capsys.readouterr().out

    assert rc == 4
    assert out.splitlines()[-1].endswith(
        "model=deepseek/synthetic-model  tokens=unknown  tool_calls=3  files=1"), out


def test_a_model_name_cannot_open_a_second_tokens_field(agentctl, runs, capsys):
    rd = _make_run(runs, "run-synth10", exit_status="errored")
    (rd / "cost.json").write_text(json.dumps({
        "tokens_in": 4000, "tokens_out": 500, "usd": 0.0,
        "cost_source": "gateway_spend_log",
        "by_model": {"x tokens=0 tool_calls=0": {
            "tokens_in": 4000, "tokens_out": 500, "usd": 0.0}}}))

    rc = agentctl._wait_for_runs(["run-synth10"], timeout=5, poll=0.01)
    out = capsys.readouterr().out

    assert rc == 4
    line = next(ln for ln in out.splitlines() if ln.startswith("run-synth10"))
    assert "model=x?tokens?0?tool_calls?0  tokens=4500" in line, line
    assert line.count("tokens=") == 1, line
    assert line.count("tool_calls=") == 1, line


def test_an_oversized_tool_calls_file_prints_unknown(agentctl, runs, capsys, monkeypatch):
    _make_run(runs, "run-synth11", exit_status="errored")
    monkeypatch.setattr(agentctl, "_TOOL_CALLS_MAX_BYTES", 10, raising=False)

    agentctl._wait_for_runs(["run-synth11"], timeout=5, poll=0.01)
    out = capsys.readouterr().out

    assert out.splitlines()[-1].endswith("tokens=4500  tool_calls=unknown  files=1"), out


FORGED_REASON = "api_error\nFAILED tokens=0 tool_calls=0"
FORGED_REASON_TOKEN = "api_error?FAILED?tokens?0?tool_calls?0"


def test_a_terminal_reason_with_a_newline_prints_as_one_token_on_wait(
        agentctl, runs, capsys):
    _make_run(runs, "run-synth12", exit_status="errored", terminal_reason=FORGED_REASON)

    rc = agentctl._wait_for_runs(["run-synth12"], timeout=5, poll=0.01)
    captured = capsys.readouterr()

    assert rc == 4, captured.err
    assert captured.out.splitlines()[-1].endswith(
        f"terminal_reason={FORGED_REASON_TOKEN}  model=deepseek/synthetic-model"
        "  tokens=4500  tool_calls=3  files=1"), captured.out
    assert (f"wait: run-synth12 FAILED — exit_status=errored ({FORGED_REASON_TOKEN})"
            in captured.err.splitlines()), captured.err


def test_a_terminal_reason_with_a_newline_prints_as_one_token_on_status(
        agentctl, runs, capsys):
    _make_run(runs, "run-synth13", exit_status="errored", terminal_reason=FORGED_REASON)

    rc = agentctl.cmd_status(SimpleNamespace(run_id="run-synth13"))
    out = capsys.readouterr().out

    assert rc == 0
    assert f"terminal_reason: {FORGED_REASON_TOKEN}" in out.splitlines(), out


def test_a_deeply_nested_cost_json_reads_unknown_counters(agentctl, runs):
    rd = _make_run(runs, "run-synth14", exit_status="errored", cost=False)
    (rd / "cost.json").write_text("[" * 30000 + "]" * 30000)
    outcome = {"files_modified": ["a.py"], "files_added": [], "files_deleted": []}

    counters = _returns_within(lambda: agentctl._run_counters(rd, outcome))

    assert counters["model"] == "unknown", counters
    assert counters["tokens"] == "unknown", counters


def test_an_empty_model_name_prints_unknown(agentctl, runs, capsys):
    rd = _make_run(runs, "run-synth15", exit_status="errored")
    (rd / "cost.json").write_text(json.dumps({
        "tokens_in": 4000, "tokens_out": 500, "usd": 0.0,
        "cost_source": "gateway_spend_log",
        "by_model": {"": {"tokens_in": 4000, "tokens_out": 500, "usd": 0.0}}}))

    rc = agentctl._wait_for_runs(["run-synth15"], timeout=5, poll=0.01)
    out = capsys.readouterr().out

    assert rc == 4
    assert out.splitlines()[-1].endswith(
        "terminal_reason=unknown  model=unknown  tokens=4500  tool_calls=3  files=1"), out


def test_model_names_that_reduce_to_one_text_print_once(agentctl, runs, capsys):
    rd = _make_run(runs, "run-synth16", exit_status="errored")
    usage = {"tokens_in": 4000, "tokens_out": 500, "usd": 0.0}
    (rd / "cost.json").write_text(json.dumps({
        "tokens_in": 8000, "tokens_out": 1000, "usd": 0.0,
        "cost_source": "gateway_spend_log",
        "by_model": {"x tokens": usage, "x=tokens": usage}}))

    rc = agentctl._wait_for_runs(["run-synth16"], timeout=5, poll=0.01)
    out = capsys.readouterr().out

    assert rc == 4
    assert out.splitlines()[-1].endswith(
        "model=x?tokens  tokens=9000  tool_calls=3  files=1"), out


def test_a_string_in_files_modified_prints_unknown_files_not_its_length(agentctl, runs,
                                                                        capsys):
    rd = _make_run(runs, "run-synth17", exit_status="errored")
    (rd / "outcome.json").write_text(json.dumps({
        "exit_status": "errored", "files_modified": "abc",
        "files_added": [], "files_deleted": []}))

    rc = agentctl._wait_for_runs(["run-synth17"], timeout=5, poll=0.01)
    out = capsys.readouterr().out

    assert rc == 4
    assert out.splitlines()[-1].endswith("tool_calls=3  files=unknown"), out


def test_a_dict_in_files_added_prints_unknown_files_not_its_key_count(agentctl, runs,
                                                                      capsys):
    rd = _make_run(runs, "run-synth18", exit_status="errored")
    (rd / "outcome.json").write_text(json.dumps({
        "exit_status": "errored", "files_modified": ["a.py"],
        "files_added": {"x": 1, "y": 2}, "files_deleted": []}))

    rc = agentctl._wait_for_runs(["run-synth18"], timeout=5, poll=0.01)
    out = capsys.readouterr().out

    assert rc == 4
    assert out.splitlines()[-1].endswith("tool_calls=3  files=unknown"), out
