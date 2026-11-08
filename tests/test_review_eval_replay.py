"""lib/agent-platform/review_eval_replay.py — the review-replay eval
harness's replay planner and dispatcher (agent-platform#807).

No real `agentctl`/`claude`/network call anywhere here: `run_subprocess` is
always a stub. Every corpus entry and review body is synthetic.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib" / "agent-platform"))
import registry  # noqa: E402
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
    _make_run_dir(tmp_path, "run-killed", verdict=None, exit_status="killed")

    def stub_subprocess(argv, **kw):
        if argv[1] == "spawn":
            return _Proc(stderr="spawned run-killed (detached)\n")
        assert argv[1] == "wait"
        return _Proc(returncode=4)  # agentctl wait: the run ENDED (killed) — not a timeout

    result = RER.run_one(entry, arm, runs_root=tmp_path, run_subprocess=stub_subprocess)
    assert result["run_id"] == "run-killed"
    assert "error" in result and "wait exit 4" in result["error"]
    assert result["usd"] == 0.05  # cost.json was still written before the run ended


def test_run_one_kills_and_reconfirms_after_a_wait_timeout(tmp_path):
    """Round 2 (agent-platform#940, reviewer + security-reviewer): `agentctl
    wait` exiting 2 means the run is STILL ALIVE, not merely that something
    went wrong — `run_one` must kill it and confirm it has ended before
    returning, rather than releasing `run_batch`'s per-entry lock onto a run
    that may still be running."""
    entry = _entry(tmp_path)
    arm = RER.ModelArm(key="deepseek", model="deepseek", is_gateway=True)
    _make_run_dir(tmp_path, "run-hung", verdict=None, exit_status="killed")
    calls = []

    def stub_subprocess(argv, **kw):
        calls.append(list(argv))
        if argv[1] == "spawn":
            return _Proc(stderr="spawned run-hung (detached)\n")
        if argv[1] == "wait":
            wait_calls = sum(1 for c in calls if c[1] == "wait")
            if wait_calls == 1:
                return _Proc(returncode=2)  # timeout elapsed, run still alive
            assert argv[-2:] == ["--timeout", "0"]  # the confirmation wait never times out itself
            return _Proc(returncode=4)  # confirmed ended (killed)
        assert argv[1] == "kill"
        assert argv[2] == "run-hung"
        assert "--final" in argv
        return _Proc(returncode=0)

    result = RER.run_one(entry, arm, runs_root=tmp_path, run_subprocess=stub_subprocess)

    assert sum(1 for c in calls if c[1] == "kill") == 1
    assert sum(1 for c in calls if c[1] == "wait") == 2
    assert result["run_id"] == "run-hung"
    assert "error" in result
    assert "wait exit 2" in result["error"] and "killed" in result["error"]


def test_run_one_kills_and_reconfirms_after_the_waiter_itself_is_killed(tmp_path):
    """Round 3 (agent-platform#940, reviewer): `agentctl wait` exiting 3 is
    `EXIT_WAITER_KILLED` (`bin/agentctl`) — the WAITER process died, not the
    run, so the run's liveness is exactly as unknown as it was before the
    wait started. `run_one` must kill and confirm on a 3 exactly as it does
    on a 2, never releasing the lock onto a run it never confirmed ended."""
    entry = _entry(tmp_path)
    arm = RER.ModelArm(key="deepseek", model="deepseek", is_gateway=True)
    _make_run_dir(tmp_path, "run-waiterdied", verdict=None, exit_status="killed")
    calls = []

    def stub_subprocess(argv, **kw):
        calls.append(list(argv))
        if argv[1] == "spawn":
            return _Proc(stderr="spawned run-waiterdied (detached)\n")
        if argv[1] == "wait":
            wait_calls = sum(1 for c in calls if c[1] == "wait")
            if wait_calls == 1:
                return _Proc(returncode=3)  # the WAITER was killed, run status unknown
            assert argv[-2:] == ["--timeout", "0"]  # the confirmation wait never times out itself
            return _Proc(returncode=4)  # confirmed ended (killed)
        assert argv[1] == "kill"
        assert argv[2] == "run-waiterdied"
        assert "--final" in argv
        return _Proc(returncode=0)

    result = RER.run_one(entry, arm, runs_root=tmp_path, run_subprocess=stub_subprocess)

    assert sum(1 for c in calls if c[1] == "kill") == 1
    assert sum(1 for c in calls if c[1] == "wait") == 2
    assert result["run_id"] == "run-waiterdied"
    assert "error" in result
    assert "wait exit 3" in result["error"] and "killed" in result["error"]


def test_run_one_does_not_kill_on_a_non_timeout_wait_failure(tmp_path):
    """A `wait` exit of 1 means `_wait_for_runs` never found a run dir for
    this id — nothing was dispatched under it as far as this wait could
    tell, so there is nothing to kill. Only exit 2 (timeout) or 3 (the
    waiter itself killed) — both of which leave the run's liveness
    unresolved — call `agentctl kill`."""
    entry = _entry(tmp_path)
    arm = RER.ModelArm(key="deepseek", model="deepseek", is_gateway=True)
    _make_run_dir(tmp_path, "run-unresolved", verdict=None, exit_status="killed")
    calls = []

    def stub_subprocess(argv, **kw):
        calls.append(list(argv))
        if argv[1] == "spawn":
            return _Proc(stderr="spawned run-unresolved (detached)\n")
        assert argv[1] == "wait"
        return _Proc(returncode=1)  # e.g. the run id didn't resolve for `wait`

    result = RER.run_one(entry, arm, runs_root=tmp_path, run_subprocess=stub_subprocess)

    assert not any(c[1] == "kill" for c in calls)
    assert result["error"] == "wait exit 1"


def test_run_one_retries_the_kill_confirm_with_backoff_and_says_why(tmp_path, capsys):
    """Round 3 (agent-platform#940, security-reviewer): the kill+confirm
    loop holds `run_batch`'s per-entry lock for its whole duration, so a
    confirmation that keeps coming back unresolved (1 or 3) must say why it
    is retrying rather than looping silently, and must back off between
    attempts instead of hammering `agentctl` with no delay."""
    entry = _entry(tmp_path)
    arm = RER.ModelArm(key="deepseek", model="deepseek", is_gateway=True)
    _make_run_dir(tmp_path, "run-stuck", verdict=None, exit_status="killed")
    calls = []
    sleeps = []

    def stub_subprocess(argv, **kw):
        calls.append(list(argv))
        if argv[1] == "spawn":
            return _Proc(stderr="spawned run-stuck (detached)\n")
        if argv[1] == "wait":
            wait_calls = sum(1 for c in calls if c[1] == "wait")
            if wait_calls == 1:
                return _Proc(returncode=2)  # timeout elapsed, run still alive
            confirm_calls = wait_calls - 1
            if confirm_calls <= 2:
                return _Proc(returncode=3)  # confirmation's own wait got killed too
            return _Proc(returncode=0)  # third confirmation: ended
        assert argv[1] == "kill"
        return _Proc(returncode=0)

    result = RER.run_one(entry, arm, runs_root=tmp_path, run_subprocess=stub_subprocess,
                         sleep=lambda s: sleeps.append(s))

    assert sum(1 for c in calls if c[1] == "kill") == 3
    assert sum(1 for c in calls if c[1] == "wait") == 4  # 1 initial + 3 confirmations
    assert sleeps == [1.0, 2.0]
    err_lines = [line for line in capsys.readouterr().err.splitlines()
                if "run-stuck" in line]
    assert len(err_lines) == 2
    assert all("3" in line for line in err_lines)
    assert result["run_id"] == "run-stuck"


# --- run_batch: resumability, concurrency, spend cap --------------------------


def test_run_batch_is_resumable_across_calls(tmp_path, runs_root):
    results_dir = tmp_path / "results"
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


def test_run_batch_stops_dispatching_a_capped_model_without_stopping_another(tmp_path, runs_root):
    results_dir = tmp_path / "results"
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


def test_run_batch_bounds_concurrency(tmp_path, runs_root):
    results_dir = tmp_path / "results"
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


def test_run_batch_concurrent_workers_cannot_overshoot_the_spend_cap(tmp_path, runs_root):
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


# --- run_batch: no two arms of the same entry overlap (agent-platform#940 round 1) ---


def test_interleave_by_entry_keeps_every_pair_without_dropping_or_duplicating():
    entries = [{"entry_id": f"e{i}"} for i in range(3)]
    arms = [RER.ModelArm(key="a1", model="a1"), RER.ModelArm(key="a2", model="a2"),
           RER.ModelArm(key="a3", model="a3")]
    queue = [(e, a) for e in entries for a in arms]  # entry-major, as plan_replays returns it

    out = RER._interleave_by_entry(queue)

    assert sorted((e["entry_id"], a.key) for e, a in out) == \
        sorted((e["entry_id"], a.key) for e, a in queue)
    # More than one entry is queued, so no two consecutive pairs share an entry —
    # the ordering that would otherwise idle the pool against the per-entry guard.
    for (e1, _), (e2, _) in zip(out, out[1:]):
        assert e1["entry_id"] != e2["entry_id"]


def test_run_batch_never_overlaps_two_arms_of_the_same_entry(monkeypatch, tmp_path, runs_root):
    """The permission layer can only deny a sibling scratch directory that
    already exists when a dispatch's own settings render
    (`run_sandbox._existing_eval_sibling_dirs`), so two arms of the SAME
    entry running at once could let one read the other's draft verdict.
    `run_batch` must make that impossible itself."""
    results_dir = tmp_path / "results"
    entries = [_entry(tmp_path, entry_id=f"e{i}") for i in range(4)]
    arms = [RER.ModelArm(key="arm1", model="arm1", is_gateway=True),
           RER.ModelArm(key="arm2", model="arm2", is_gateway=True)]

    lock = threading.Lock()
    intervals = []  # (entry_id, start, end)

    def stub_run_one(entry, arm, *, runs_root, run_subprocess, wait_timeout):
        start = time.monotonic()
        time.sleep(0.03)
        end = time.monotonic()
        with lock:
            intervals.append((entry["entry_id"], start, end))
        return {"entry_id": entry["entry_id"], "model_key": arm.key,
               "repo": entry["repo"], "pr": entry["pr"], "kind": entry["kind"],
               "run_id": f"run-{entry['entry_id']}-{arm.key}", "verdict": "APPROVE",
               "usd": 0.0, "tokens_in": 0, "tokens_out": 0, "not_run": False}

    monkeypatch.setattr(RER, "run_one", stub_run_one)

    results = RER.run_batch(entries, arms, results_dir=results_dir, runs_root=runs_root,
                            concurrency=4)

    assert len(results) == len(entries) * len(arms)
    by_entry: dict[str, list[tuple[float, float]]] = {}
    for entry_id, start, end in intervals:
        by_entry.setdefault(entry_id, []).append((start, end))
    for entry_id, spans in by_entry.items():
        spans.sort()
        for (_, end), (next_start, _) in zip(spans, spans[1:]):
            assert end <= next_start, f"{entry_id}: two arms overlapped"

    # The guard must not serialize the whole batch: some pair from two
    # DIFFERENT entries has to overlap, or concurrency=4 bought nothing.
    def _overlaps(a, b):
        (_, a_start, a_end), (_, b_start, b_end) = a, b
        return a_start < b_end and b_start < a_end

    assert any(_overlaps(a, b) for i, a in enumerate(intervals)
              for b in intervals[i + 1:] if a[0] != b[0])


def test_run_batch_with_multiple_arms_resumes_exactly_the_remaining_pairs(tmp_path, runs_root):
    results_dir = tmp_path / "results"
    entries = [_entry(tmp_path, entry_id=f"e{i}") for i in range(3)]
    arms = [RER.ModelArm(key="arm1", model="arm1", is_gateway=True),
           RER.ModelArm(key="arm2", model="arm2", is_gateway=True)]
    results_dir.mkdir(parents=True)
    RER.result_path(results_dir, "e1", "arm1").write_text(
        json.dumps({"entry_id": "e1", "model_key": "arm1"}))

    counter = {"n": 0}

    def stub_subprocess(argv, **kw):
        if argv[1] != "spawn":
            return _Proc(returncode=0)
        counter["n"] += 1
        run_id = f"run-{counter['n']}"
        _make_run_dir(runs_root, run_id)
        return _Proc(stderr=f"spawned {run_id} (detached)\n")

    results = RER.run_batch(entries, arms, results_dir=results_dir, runs_root=runs_root,
                            concurrency=4, run_subprocess=stub_subprocess)

    ran_pairs = [(r["entry_id"], r["model_key"]) for r in results]
    expected = {(e["entry_id"], a.key) for e in entries for a in arms} - {("e1", "arm1")}
    assert set(ran_pairs) == expected
    assert len(ran_pairs) == len(expected)  # no duplicates


# --- run_batch: the per-entry lock survives a wait timeout (agent-platform#940 round 2) ---


def test_run_batch_holds_the_lock_until_a_timed_out_arm_is_killed_and_confirmed_ended(
        tmp_path, runs_root):
    """Round 1's lock is released the instant `run_one` RETURNS. Before round
    2, `run_one` returned as soon as `agentctl wait` returned — including on
    a timeout, which leaves the run genuinely alive — so the second arm of
    the SAME entry could start beside a first arm that never actually
    finished. `run_one` now kills a timed-out run and confirms it ended
    before returning; this proves the second arm still cannot start until
    that kill is confirmed."""
    results_dir = tmp_path / "results"
    entries = [_entry(tmp_path, entry_id="e0")]
    arm1 = RER.ModelArm(key="arm1", model="arm1", is_gateway=True)
    arm2 = RER.ModelArm(key="arm2", model="arm2", is_gateway=True)

    lock = threading.Lock()
    state = {"wait_calls_for_arm1": 0, "kill_called": False, "arm2_started": False}

    def stub_subprocess(argv, **kw):
        if argv[1] == "spawn":
            model = argv[argv.index("--model") + 1]
            run_id = f"run-{model}"
            _make_run_dir(runs_root, run_id, exit_status="killed")
            if model == "arm2":
                with lock:
                    state["arm2_started"] = True
                    assert state["kill_called"], \
                        "arm2 started before arm1's timed-out run was killed"
            return _Proc(stderr=f"spawned {run_id} (detached)\n")
        if argv[1] == "wait":
            run_id = argv[2]
            if run_id == "run-arm1":
                with lock:
                    state["wait_calls_for_arm1"] += 1
                    first = state["wait_calls_for_arm1"] == 1
                if first:
                    return _Proc(returncode=2)  # timeout elapsed, run still alive
                assert argv[-2:] == ["--timeout", "0"]
                return _Proc(returncode=4)  # confirmed ended (killed)
            return _Proc(returncode=0)  # arm2's own wait: plain success
        assert argv[1] == "kill"
        assert argv[2] == "run-arm1"
        with lock:
            state["kill_called"] = True
        return _Proc(returncode=0)

    results = RER.run_batch(entries, [arm1, arm2], results_dir=results_dir, runs_root=runs_root,
                            concurrency=2, run_subprocess=stub_subprocess)

    assert state["kill_called"]
    assert state["arm2_started"]
    assert len(results) == 2


# --- run_batch: refuses to start beside a live eval-mode run (agent-platform#940 round 2) ---


def _register_live_run(run_id: str) -> None:
    """A registry entry whose pid is THIS test process's own — `_is_alive`
    (and so `registry.list_runs()`'s own reaping) reads it as alive for as
    long as the test is running, with no real subprocess needed."""
    registry.add_run({"run_id": run_id, "agent": "reviewer", "task": "t",
                      "started_at": "2026-10-18T00:00:00Z", "repo": "agent-platform",
                      "worktree": "/tmp", "pid": os.getpid()})


def test_run_batch_refuses_to_start_beside_a_live_eval_run(tmp_path, runs_root):
    results_dir = tmp_path / "results"
    entries = [_entry(tmp_path, entry_id="e0")]
    arm = RER.ModelArm(key="arm1", model="arm1", is_gateway=True)

    _make_run_dir(runs_root, "run-live-eval")  # meta.json: eval_mode true
    _register_live_run("run-live-eval")

    calls = []

    def stub_subprocess(argv, **kw):
        calls.append(argv)
        return _Proc(returncode=0)

    with pytest.raises(RER.LiveEvalRunsError) as excinfo:
        RER.run_batch(entries, [arm], results_dir=results_dir, runs_root=runs_root,
                      concurrency=1, run_subprocess=stub_subprocess)

    assert excinfo.value.run_ids == ["run-live-eval"]
    assert "run-live-eval" in str(excinfo.value)
    assert calls == []  # nothing was dispatched


def test_run_batch_starts_beside_an_ended_eval_run_or_a_live_non_eval_run(tmp_path, runs_root):
    results_dir = tmp_path / "results"
    entries = [_entry(tmp_path, entry_id="e0")]
    arm = RER.ModelArm(key="arm1", model="arm1", is_gateway=True)

    # An eval run that already ENDED is not in the registry at all — the real
    # dispatcher removes its entry at finalize — so there is nothing to add
    # to the registry for it; its run dir alone must not trigger a refusal.
    _make_run_dir(runs_root, "run-ended-eval")

    # A LIVE run that is NOT an eval dispatch (no `eval_mode` in its meta.json).
    non_eval_dir = runs_root / "2026-09-25" / "run-live-reviewer"
    non_eval_dir.mkdir(parents=True)
    (non_eval_dir / "meta.json").write_text(json.dumps({"run_id": "run-live-reviewer"}))
    _register_live_run("run-live-reviewer")

    counter = {"n": 0}

    def stub_subprocess(argv, **kw):
        if argv[1] != "spawn":
            return _Proc(returncode=0)
        counter["n"] += 1
        run_id = f"run-{counter['n']}"
        _make_run_dir(runs_root, run_id)
        return _Proc(stderr=f"spawned {run_id} (detached)\n")

    results = RER.run_batch(entries, [arm], results_dir=results_dir, runs_root=runs_root,
                            concurrency=1, run_subprocess=stub_subprocess)
    assert len(results) == 1
