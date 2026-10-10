"""subagent-telemetry-emit hook — product resolution (the operator 2026-06-30).

The SubagentStop hook used to stamp `repo` from the cwd basename, so a subagent
that ran in a git worktree (every dispatched worker does) recorded a worktree
label — `agent-<hex>` or `<product>.<agent>-<slug>` — as its product. Those
polluted the /activity Product filter and spawned phantom sessions. `_resolve_repo`
now resolves the worktree back to its primary repo; these guard that.
"""
from __future__ import annotations

import importlib.util
import io
import json
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "tools" / "subagent-telemetry-emit.py"


def _load_hook():
    """Import the hook module. Its `if __name__ == '__main__'` guard means the
    stdin-reading _emit() does NOT run on import — only the defs load."""
    spec = importlib.util.spec_from_file_location("subagent_telemetry_emit", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hook = _load_hook()


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _init_repo(path):
    path.mkdir(parents=True)
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    (path / "seed").write_text("x")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "init")


def test_resolve_repo_plain_repo(tmp_path):
    repo = tmp_path / "vivy"
    _init_repo(repo)
    assert hook._resolve_repo(str(repo)) == "vivy"


def test_resolve_repo_worktree_resolves_to_primary(tmp_path):
    """A worktree named `agent-<hex>` resolves to its primary repo's product, not the
    worktree basename — the actual bug the operator saw on /activity."""
    repo = tmp_path / "personal-assistant"
    _init_repo(repo)
    wt = tmp_path / "worktrees" / "agent-a562bc71051ec4c2d"
    _git(repo, "worktree", "add", "-b", "b1", str(wt))
    assert hook._resolve_repo(str(wt)) == "personal-assistant"


def test_resolve_repo_non_git_falls_back_to_basename(tmp_path):
    d = tmp_path / "loose-dir"
    d.mkdir()
    assert hook._resolve_repo(str(d)) == "loose-dir"


def test_emit_dedupes_repeated_message_id(tmp_path, monkeypatch):
    # The SubagentStop emitter sums a subagent transcript's usage; Claude Code writes
    # the same assistant response more than once (identical message.id + requestId),
    # so it must dedupe or the subagent's cost.json double-counts (same fix as
    # cost_join.co_main_loop_node, ARCH-383).
    monkeypatch.setattr(hook.T, "RUNS_ROOT", tmp_path / "runs")
    usage = {"input_tokens": 1000, "output_tokens": 500,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}

    def arec(mid, rid):
        return json.dumps({"type": "assistant", "timestamp": "2026-06-10T12:00:00Z",
                           "requestId": rid,
                           "message": {"id": mid, "model": "claude-opus-4-8", "usage": usage}})

    tpath = tmp_path / "sub.jsonl"
    tpath.write_text("\n".join([
        json.dumps({"type": "user", "message": {"content": "do X"}}),
        arec("msg_A", "req_A"), arec("msg_A", "req_A"),   # duplicate response
        arec("msg_B", "req_B"),
    ]))
    ev = {"agent_id": "a1", "agent_type": "reviewer",
          "agent_transcript_path": str(tpath), "session_id": "s1", "cwd": str(tmp_path)}
    monkeypatch.setattr(hook.sys, "stdin", io.StringIO(json.dumps(ev)))
    hook._emit()

    costs = list((tmp_path / "runs").rglob("cost.json"))
    assert len(costs) == 1
    cost = json.loads(costs[0].read_text())
    # two unique opus responses (0.0175 each) = 0.035, not 3 records × 0.0175 = 0.0525
    assert cost["usd"] == pytest.approx(0.035)


def test_emit_prices_a_1h_cache_write_at_the_1h_rate(tmp_path, monkeypatch):
    """A turn's `usage.cache_creation` TTL breakdown must survive `_emit`'s
    per-turn accumulation into the cost.json it writes through
    `T.cost_from_usage`. Sonnet 5: input $2/Mtok, 1h cache write $4/Mtok — a
    turn with no input/output tokens and 1M 1h-cache-write tokens prices to
    exactly $4 only if the breakdown reached cost_from_usage; dropping it
    (falling back to the flat `cache_creation_input_tokens` field, which
    this turn leaves at 0) would price it at $0."""
    monkeypatch.setattr(hook.T, "RUNS_ROOT", tmp_path / "runs")
    usage = {"input_tokens": 0, "output_tokens": 0,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
             "cache_creation": {"ephemeral_1h_input_tokens": 1_000_000,
                                "ephemeral_5m_input_tokens": 0}}

    tpath = tmp_path / "sub.jsonl"
    tpath.write_text("\n".join([
        json.dumps({"type": "user", "message": {"content": "do X"}}),
        json.dumps({"type": "assistant", "timestamp": "2026-06-10T12:00:00Z",
                   "requestId": "req_1",
                   "message": {"id": "msg_1", "model": "claude-sonnet-5", "usage": usage}}),
    ]))
    ev = {"agent_id": "a1h", "agent_type": "reviewer",
          "agent_transcript_path": str(tpath), "session_id": "s1", "cwd": str(tmp_path)}
    monkeypatch.setattr(hook.sys, "stdin", io.StringIO(json.dumps(ev)))
    hook._emit()

    costs = list((tmp_path / "runs").rglob("cost.json"))
    assert len(costs) == 1
    cost = json.loads(costs[0].read_text())
    assert cost["usd"] == pytest.approx(4.0)


def test_emit_counts_a_keyless_record_once_not_dropped(tmp_path, monkeypatch):
    """A record with neither message.id nor requestId is counted, never
    dropped and never collapsed with another keyless record."""
    monkeypatch.setattr(hook.T, "RUNS_ROOT", tmp_path / "runs")
    usage = {"input_tokens": 1000, "output_tokens": 500,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    keyless = json.dumps({"type": "assistant", "timestamp": "2026-06-10T12:00:00Z",
                          "message": {"model": "claude-opus-4-8", "usage": usage}})
    tpath = tmp_path / "sub.jsonl"
    tpath.write_text("\n".join([
        json.dumps({"type": "user", "message": {"content": "do X"}}),
        keyless,
    ]))
    ev = {"agent_id": "a2", "agent_type": "reviewer",
          "agent_transcript_path": str(tpath), "session_id": "s1", "cwd": str(tmp_path)}
    monkeypatch.setattr(hook.sys, "stdin", io.StringIO(json.dumps(ev)))
    hook._emit()

    costs = list((tmp_path / "runs").rglob("cost.json"))
    assert len(costs) == 1
    cost = json.loads(costs[0].read_text())
    assert cost["usd"] == pytest.approx(0.0175)   # one opus response, counted once
