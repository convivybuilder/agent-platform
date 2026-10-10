"""lib/agent-platform/review_eval_replay.py — the review-replay eval
harness's replay planner and dispatcher (agent-platform#807).

No real `agentctl`/`claude`/network call anywhere here: `run_subprocess` is
always a stub. Every corpus entry and review body is synthetic.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib" / "agent-platform"))
import review_eval_replay as RER  # noqa: E402
import review_eval_spend as RES  # noqa: E402


def _entry(tmp_path, entry_id="agent-platform/1/reviewer/aaaaaaaaaaaa", **overrides):
    prompts = tmp_path / "prompts" / entry_id.replace("/", "_")
    prompts.mkdir(parents=True, exist_ok=True)
    user_path = prompts / "user.md"
    user_path.write_text(overrides.pop("task_text", "Review agent-platform#1 at aaaaaaa.\n"))
    base = {"entry_id": entry_id, "repo": "agent-platform", "pr": "1",
           "role": "reviewer", "sha": "a" * 40, "kind": "confirmed",
           "user_prompt_path": str(user_path)}
    base.update(overrides)
    return base


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# --- build_spawn_argv ---------------------------------------------------------


def test_build_spawn_argv_pins_review_sha_never_pr_and_sets_eval_mode(tmp_path):
    entry = _entry(tmp_path, task_text="synthetic brief text\n")
    arm = RER.ModelArm(key="deepseek", model="deepseek", is_gateway=True)
    argv = RER.build_spawn_argv(entry, arm)

    assert argv[:2] == ["agentctl", "spawn"]
    assert "reviewer" in argv
    assert "--pr" not in argv
    assert "--review-sha" in argv and argv[argv.index("--review-sha") + 1] == "a" * 40
    assert "--eval-mode" in argv
    assert argv[argv.index("--model") + 1] == "deepseek"
    assert argv[argv.index("--task") + 1] == "synthetic brief text\n"


def test_build_spawn_argv_includes_effort_and_agents_dir_when_set(tmp_path):
    entry = _entry(tmp_path)
    arm = RER.ModelArm(key="sonnet-medium", model="sonnet", effort="medium",
                       agents_dir="/tmp/variant-agents")
    argv = RER.build_spawn_argv(entry, arm)
    assert argv[argv.index("--effort") + 1] == "medium"
    assert argv[argv.index("--agents-dir") + 1] == "/tmp/variant-agents"


# --- result_path / load_existing_results / plan_replays ----------------------


def test_result_path_is_filesystem_safe(tmp_path):
    p = RER.result_path(tmp_path, "agent-platform/1/reviewer/aaaaaaaaaaaa", "deepseek")
    assert p.parent == tmp_path
    assert "/" not in p.name[:-5] or p.name.count("/") == 0
    assert p.name == "agent-platform__1__reviewer__aaaaaaaaaaaa__deepseek.json"


def test_load_existing_results_keys_by_entry_and_model(tmp_path):
    (tmp_path / "x.json").write_text(json.dumps({"entry_id": "e1", "model_key": "deepseek"}))
    (tmp_path / "y.json").write_text(json.dumps({"entry_id": "e2", "model_key": "kimi"}))
    (tmp_path / "bad.json").write_text("not json")
    existing = RER.load_existing_results(tmp_path)
    assert set(existing) == {("e1", "deepseek"), ("e2", "kimi")}


def test_plan_replays_skips_pairs_with_existing_results(tmp_path):
    entries = [_entry(tmp_path, entry_id="e1"), _entry(tmp_path, entry_id="e2")]
    arms = [RER.ModelArm(key="deepseek", model="deepseek", is_gateway=True),
           RER.ModelArm(key="kimi", model="kimi", is_gateway=True)]
    existing = {("e1", "deepseek"): {}}
    planned = RER.plan_replays(entries, arms, existing)
    pairs = {(e["entry_id"], a.key) for e, a in planned}
    assert pairs == {("e1", "kimi"), ("e2", "deepseek"), ("e2", "kimi")}


# --- run_one -------------------------------------------------------------------


def _make_run_dir(runs_root, run_id, *, verdict="APPROVE", body="synthetic body",
                  tokens_in=100, tokens_out=10, usd=0.05, exit_status="completed"):
    run_dir = runs_root / "2026-09-25" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(json.dumps({"run_id": run_id, "eval_mode": True}))
    (run_dir / "cost.json").write_text(json.dumps(
        {"tokens_in": tokens_in, "tokens_out": tokens_out, "usd": usd,
         "cost_source": "gateway_spend_log"}))
    (run_dir / "outcome.json").write_text(json.dumps({"exit_status": exit_status}))
    (run_dir / "eval-review.json").write_text(json.dumps(
        {"verdict": verdict, "body": body, "run_id": run_id}))
    return run_dir


def test_run_one_parses_run_id_and_collects_the_run_dirs_result(tmp_path):
    entry = _entry(tmp_path)
    arm = RER.ModelArm(key="deepseek", model="deepseek", is_gateway=True)
    _make_run_dir(tmp_path, "run-abc123")

    calls = []

    def stub_subprocess(argv, **kw):
        calls.append(argv)
        if argv[1] == "spawn":
            return _Proc(returncode=0, stderr="spawned run-abc123 (detached)  agent=reviewer  worktree=/x\n")
        assert argv[:2] == ["agentctl", "wait"]
        assert argv[2] == "run-abc123"
        return _Proc(returncode=0)

    result = RER.run_one(entry, arm, runs_root=tmp_path, run_subprocess=stub_subprocess)

    assert result["run_id"] == "run-abc123"
    assert result["verdict"] == "APPROVE"
    assert result["usd"] == 0.05
    assert result["tokens_in"] == 100
    assert "error" not in result
    assert len(calls) == 2


def test_run_one_records_a_spawn_failure_without_raising(tmp_path):
    entry = _entry(tmp_path)
    arm = RER.ModelArm(key="deepseek", model="deepseek", is_gateway=True)

    def stub_subprocess(argv, **kw):
        return _Proc(returncode=2, stderr="error: cannot spawn — capacity refused\n")

    result = RER.run_one(entry, arm, runs_root=tmp_path, run_subprocess=stub_subprocess)

    assert result["run_id"] is None
    assert result["error"] == "spawn"
    assert result["usd"] == 0.0


def test_run_one_records_a_wait_failure_but_still_reads_whatever_cost_exists(tmp_path):
    entry = _entry(tmp_path)
    arm = RER.ModelArm(key="deepseek", model="deepseek", is_gateway=True)
    _make_run_dir(tmp_path, "run-timedout", verdict=None, exit_status="killed")

    def stub_subprocess(argv, **kw):
        if argv[1] == "spawn":
            return _Proc(stderr="spawned run-timedout (detached)\n")
        return _Proc(returncode=2)  # agentctl wait: timeout elapsed, run still alive

    result = RER.run_one(entry, arm, runs_root=tmp_path, run_subprocess=stub_subprocess)
    assert result["run_id"] == "run-timedout"
    assert "error" in result and "wait exit 2" in result["error"]
    assert result["usd"] == 0.05  # cost.json was still written before the timeout


# --- run_batch: resumability, concurrency, spend cap --------------------------


def test_run_batch_is_resumable_across_calls(tmp_path):
    results_dir = tmp_path / "results"
    runs_root = tmp_path / "runs"
    entries = [_entry(tmp_path, entry_id="e1"), _entry(tmp_path, entry_id="e2")]
    arm = RER.ModelArm(key="deepseek", model="deepseek", is_gateway=True)
    calls = []

    def stub_subprocess(argv, **kw):
        calls.append(argv)
        if argv[1] == "spawn":
            run_id = f"run-{len(calls)}"
            _make_run_dir(runs_root, run_id)
            return _Proc(stderr=f"spawned {run_id} (detached)\n")
        return _Proc(returncode=0)

    first = RER.run_batch(entries, [arm], results_dir=results_dir, runs_root=runs_root,
                          concurrency=1, run_subprocess=stub_subprocess)
    assert len(first) == 2
    spawn_calls_after_first = sum(1 for c in calls if c[1] == "spawn")

    # Rerun the batch: both pairs already have result files, so nothing new dispatches.
    second = RER.run_batch(entries, [arm], results_dir=results_dir, runs_root=runs_root,
                           concurrency=1, run_subprocess=stub_subprocess)
    assert second == []
    assert sum(1 for c in calls if c[1] == "spawn") == spawn_calls_after_first


def test_run_batch_stops_dispatching_a_capped_model_without_stopping_another(tmp_path):
    results_dir = tmp_path / "results"
    runs_root = tmp_path / "runs"
    entries = [_entry(tmp_path, entry_id=f"e{i}") for i in range(4)]
    capped = RER.ModelArm(key="deepseek", model="deepseek", is_gateway=True)
    uncapped = RER.ModelArm(key="kimi", model="kimi", is_gateway=True)
    cap = RES.CapTracker(total_cap=100.0, per_model_cap={"deepseek": 0.05, "kimi": 100.0})

    counter = {"n": 0}

    def stub_subprocess(argv, **kw):
        if argv[1] == "spawn":
            counter["n"] += 1
            run_id = f"run-{counter['n']}"
            _make_run_dir(runs_root, run_id, usd=0.05)
            return _Proc(stderr=f"spawned {run_id} (detached)\n")
        return _Proc(returncode=0)

    results = RER.run_batch(entries, [capped, uncapped], results_dir=results_dir,
                            runs_root=runs_root, concurrency=1, cap_tracker=cap,
                            run_subprocess=stub_subprocess)

    deepseek_results = [r for r in results if r["model_key"] == "deepseek"]
    kimi_results = [r for r in results if r["model_key"] == "kimi"]
    assert len(kimi_results) == 4 and all(not r["not_run"] for r in kimi_results)
    # deepseek: the first entry spends the whole $0.05 cap; the rest must be
    # reported not_run, never silently dropped and never counted as failures.
    assert len(deepseek_results) == 4
    assert sum(1 for r in deepseek_results if not r["not_run"]) == 1
    assert sum(1 for r in deepseek_results if r["not_run"]) == 3
    for r in deepseek_results:
        if r["not_run"]:
            assert "error" not in r
            assert r["usd"] == 0.0


def test_run_batch_bounds_concurrency(tmp_path):
    results_dir = tmp_path / "results"
    runs_root = tmp_path / "runs"
    entries = [_entry(tmp_path, entry_id=f"e{i}") for i in range(6)]
    arm = RER.ModelArm(key="deepseek", model="deepseek", is_gateway=True)

    in_flight = {"n": 0, "peak": 0}
    lock = threading.Lock()
    counter = {"n": 0}

    def stub_subprocess(argv, **kw):
        if argv[1] != "spawn":
            return _Proc(returncode=0)
        with lock:
            in_flight["n"] += 1
            in_flight["peak"] = max(in_flight["peak"], in_flight["n"])
        time.sleep(0.05)
        with lock:
            in_flight["n"] -= 1
            counter["n"] += 1
            run_id = f"run-{counter['n']}"
        _make_run_dir(runs_root, run_id)
        return _Proc(stderr=f"spawned {run_id} (detached)\n")

    RER.run_batch(entries, [arm], results_dir=results_dir, runs_root=runs_root,
                  concurrency=3, run_subprocess=stub_subprocess)

    assert in_flight["peak"] <= 3


def test_run_batch_concurrent_workers_cannot_overshoot_the_spend_cap(tmp_path):
    """Round-1 review (agent-platform#813): `can_dispatch` (checked at
    dispatch time) and `record` (only called once the WHOLE replay finishes)
    left a gap several concurrent workers could all pass before any of them
    recorded anything. 20 entries, concurrency 5, a $5 per-model cap, each
    replay costing $1 (both the entry's own recorded cost — what `_worker`
    reserves against — and the real cost the stub reports, so the test
    isolates the overshoot question from reservation-estimate drift): if the
    reserve/reconcile wiring in `run_batch` is NOT atomic, several of the
    first 5 concurrently-dispatched entries pass the cap check before any of
    them records, and the recorded spend ends up over $5."""
    results_dir = tmp_path / "results"
    runs_root = tmp_path / "runs"
    entries = [_entry(tmp_path, entry_id=f"e{i}", usd=1.0) for i in range(20)]
    arm = RER.ModelArm(key="deepseek", model="deepseek", is_gateway=True)
    cap = RES.CapTracker(total_cap=100.0, per_model_cap={"deepseek": 5.0})

    counter = {"n": 0}
    lock = threading.Lock()

    def stub_subprocess(argv, **kw):
        if argv[1] != "spawn":
            return _Proc(returncode=0)
        # Every worker reaches this at roughly the same time (concurrency=5,
        # all 20 entries queued at once) — the sleep widens the window a
        # non-atomic check-then-later-record pair would race inside.
        time.sleep(0.02)
        with lock:
            counter["n"] += 1
            run_id = f"run-{counter['n']}"
        _make_run_dir(runs_root, run_id, usd=1.0)
        return _Proc(stderr=f"spawned {run_id} (detached)\n")

    results = RER.run_batch(entries, [arm], results_dir=results_dir, runs_root=runs_root,
                            concurrency=5, cap_tracker=cap, run_subprocess=stub_subprocess)

    ran = [r for r in results if not r["not_run"]]
    assert len(ran) == 4  # $5 cap, $1 each: the 5th reservation would meet the cap
    assert cap.model_spent("deepseek") == 4.0
    assert cap.model_spent("deepseek") < cap.model_cap("deepseek")
