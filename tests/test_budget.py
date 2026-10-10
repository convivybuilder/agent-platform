"""Budget gating (lib/agent-platform/budget.py).

The risk these guard (ARCH-065): the live spend number the caps gate on must
be exact for completed runs and a growing estimate for in-flight ones — the
2026-05-26 failure was a $28-of-$41 night invisible because completed-only cost
peaked at $11.94 and neither cap fired. And mark_crossed must be idempotent so
the user gets ONE Slack ping per threshold per day, not one per spawn.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import budget
import pytest


def _mk_run(root, name, *, cost=None, partial=None, started_min_ago=None, now=None,
            origin=None, parent_run=None):
    """Create a run dir under today_dir(); cost.json if `cost` given (completed),
    cost-partial.json if `partial` given (in-flight, real spend recorded so
    far), meta.json with started_at if `started_min_ago` given (in-flight).
    `origin` writes that origin into meta.json (and creates meta.json even for a
    completed-only run, so origin-scoped sums can see it). `parent_run` records
    the dispatching run's id, which marks that parent as an orchestrator."""
    d = budget.today_dir()
    d.mkdir(parents=True, exist_ok=True)
    rd = d / name
    rd.mkdir()
    if cost is not None:
        (rd / "cost.json").write_text(json.dumps({"usd": cost}))
    if partial is not None:
        (rd / "cost-partial.json").write_text(json.dumps(
            {"usd": partial, "tokens_in": 0, "tokens_out": 0,
             "cost_source": "estimate_fallback", "by_model": {}, "written_at": "x"}))
    meta = {}
    if started_min_ago is not None:
        t0 = (now or datetime.now(timezone.utc)) - timedelta(minutes=started_min_ago)
        meta["started_at"] = t0.strftime("%Y-%m-%dT%H:%M:%SZ")
    if origin is not None:
        meta["origin"] = origin
    if parent_run is not None:
        meta["parent_run"] = parent_run
    if meta:
        (rd / "meta.json").write_text(json.dumps(meta))
    return rd


def test_today_total_sums_completed_and_skips_noise(runs_root):
    _mk_run(runs_root, "run-a", cost=1.5)
    _mk_run(runs_root, "run-b", cost=2.0)
    _mk_run(runs_root, "run-c")  # no cost.json — in-flight, not counted here
    # malformed cost.json is skipped, not fatal
    bad = budget.today_dir() / "run-bad"
    bad.mkdir()
    (bad / "cost.json").write_text("{not json")
    # a non-run- dir is ignored
    (budget.today_dir() / "notarun").mkdir()
    assert budget.today_total_usd() == 3.5


def test_today_total_zero_when_no_dir(runs_root):
    assert budget.today_total_usd() == 0.0


def test_inflight_estimate_uses_elapsed_times_rate(runs_root):
    now = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)
    _mk_run(runs_root, "run-live", started_min_ago=10, now=now)  # 10 min * 0.15
    _mk_run(runs_root, "run-done", cost=5.0)  # completed → excluded from estimate
    assert budget.inflight_estimate_usd(now=now) == pytest.approx(1.5)


def test_inflight_estimate_prefers_recorded_partial_over_flat_rate(runs_root):
    """A run with a recorded cost-partial.json is valued at that REAL figure,
    not the flat elapsed*rate estimate — 240 min at 0.15/min would be $36, but
    a mostly-idle orchestrator's recorded partial says $0.42 and that must win
    (issue_01M1D1HNMGCAKH89ZWY3XSJPGS / agent-platform#26's false alert)."""
    now = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)
    _mk_run(runs_root, "run-live", started_min_ago=240, now=now, partial=0.42)
    assert budget.inflight_estimate_usd(now=now) == pytest.approx(0.42)


def test_inflight_estimate_partial_zero_is_still_preferred(runs_root):
    """A recorded partial of exactly $0.00 (a run that hasn't logged a priced
    turn yet but did checkpoint) must not be treated as falsy/absent — it is a
    real recorded figure and must still beat the flat-rate estimate."""
    now = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)
    _mk_run(runs_root, "run-live", started_min_ago=10, now=now, partial=0.0)
    assert budget.inflight_estimate_usd(now=now) == pytest.approx(0.0)


def test_inflight_estimate_falls_back_to_flat_rate_without_a_partial(runs_root):
    """A run too young to have checkpointed (no cost-partial.json at all) still
    gets the flat elapsed*rate estimate — the fallback this change must keep."""
    now = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)
    _mk_run(runs_root, "run-live", started_min_ago=10, now=now)  # no partial
    assert budget.inflight_estimate_usd(now=now) == pytest.approx(1.5)


def test_inflight_estimate_falls_back_on_a_corrupt_partial(runs_root):
    """A malformed cost-partial.json degrades to the flat-rate fallback rather
    than crashing the estimate or silently valuing the run at $0."""
    now = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)
    rd = _mk_run(runs_root, "run-live", started_min_ago=10, now=now)
    (rd / "cost-partial.json").write_text("{not json")
    assert budget.inflight_estimate_usd(now=now) == pytest.approx(1.5)


def test_inflight_estimate_falls_back_on_a_partial_missing_cost_source(runs_root):
    """A partial with no cost_source can never reach a consumer marked as
    anything (operator, 2026-09-15) — `read_cost_partial` treats it as unreadable,
    so `_partial_usd` falls this run back to the flat-rate estimate exactly
    as it does for a corrupt file, not to the (unmarked) recorded figure."""
    now = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)
    rd = _mk_run(runs_root, "run-live", started_min_ago=10, now=now)
    (rd / "cost-partial.json").write_text(json.dumps(
        {"usd": 99.0, "tokens_in": 0, "tokens_out": 0, "by_model": {}, "written_at": "x"}))
    assert budget.inflight_estimate_usd(now=now) == pytest.approx(1.5)


def test_inflight_estimate_falls_back_on_a_partial_with_unknown_cost_source(runs_root):
    now = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)
    rd = _mk_run(runs_root, "run-live", started_min_ago=10, now=now)
    (rd / "cost-partial.json").write_text(json.dumps(
        {"usd": 99.0, "tokens_in": 0, "tokens_out": 0, "cost_source": "made_up_source",
         "by_model": {}, "written_at": "x"}))
    assert budget.inflight_estimate_usd(now=now) == pytest.approx(1.5)


def _write_partial(rd, usd):
    """Write cost-partial.json with an arbitrary (possibly invalid) `usd`,
    bypassing _mk_run's clamp-agnostic helper so a test can plant exactly the
    value under test."""
    (rd / "cost-partial.json").write_text(json.dumps(
        {"usd": usd, "tokens_in": 0, "tokens_out": 0,
         "cost_source": "estimate_fallback", "by_model": {}, "written_at": "x"}))


@pytest.mark.parametrize("bad_usd", [-999999.0, float("nan"), float("inf"), float("-inf")])
def test_partial_usd_rejects_non_finite_and_negative_values(runs_root, bad_usd):
    """Round 2 (security-reviewer finding on agent-platform#592):
    cost-partial.json is writable by the run it measures, so `_partial_usd`
    must trust only a finite, non-negative number — a negative, NaN, or
    infinite `usd` is rejected (falls back to None, same as a missing/corrupt
    file) rather than being summed into the pool-wide estimate as-is."""
    rd = _mk_run(runs_root, "run-live")
    _write_partial(rd, bad_usd)
    assert budget._partial_usd(rd) is None


def test_partial_usd_rejects_a_non_numeric_string(runs_root):
    rd = _mk_run(runs_root, "run-live")
    _write_partial(rd, "not-a-number")
    assert budget._partial_usd(rd) is None


def test_partial_usd_accepts_zero_and_a_normal_positive_value(runs_root):
    rd = _mk_run(runs_root, "run-live")
    _write_partial(rd, 0.0)
    assert budget._partial_usd(rd) == pytest.approx(0.0)
    _write_partial(rd, 4.2)
    assert budget._partial_usd(rd) == pytest.approx(4.2)


def test_partial_usd_is_none_when_the_partial_names_unpriced_models(runs_root):
    """A partial with `unpriced_models` carries `usd` 0 (or only the priced
    floor), which is not the run's spend; returning it would skip the flat
    burn-rate fallback and hide the run from the caps until finalize."""
    now = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)
    rd = _mk_run(runs_root, "run-live", started_min_ago=10, now=now)
    for usd in (0.0, 0.75):
        (rd / "cost-partial.json").write_text(json.dumps(
            {"usd": usd, "tokens_in": 1, "tokens_out": 1, "cost_source": "estimate_fallback",
             "by_model": {}, "unpriced_models": ["mystery/model-x"], "written_at": "x"}))
        assert budget._partial_usd(rd) is None
        assert budget.inflight_estimate_usd(now=now) == pytest.approx(1.5)   # 10 min * 0.15


def test_partial_usd_without_unpriced_models_is_unchanged(runs_root):
    rd = _mk_run(runs_root, "run-live")
    (rd / "cost-partial.json").write_text(json.dumps(
        {"usd": 3.0, "tokens_in": 1, "tokens_out": 1, "cost_source": "estimate_fallback",
         "by_model": {}, "unpriced_models": [], "written_at": "x"}))
    assert budget._partial_usd(rd) == pytest.approx(3.0)


@pytest.mark.parametrize("bad_usd", [10**400, {"nested": "dict"}, [1, 2, 3]],
                         ids=["huge_int", "nested_dict", "list"])
def test_partial_usd_rejects_non_numeric_shapes_without_raising(runs_root, bad_usd):
    """Round 3 (security-reviewer finding on agent-platform#592): round 2's
    `except (TypeError, ValueError)` didn't catch `OverflowError` —
    `float(10**400)` raises it, not `ValueError` — so a run forging a
    huge-integer `usd` crashed `budget.status()` instead of degrading to the
    flat-rate fallback like every other forged value. `_partial_usd` now
    catches `Exception` broadly (this file), so a value that isn't even a
    number at all (a dict, a list) degrades the same way rather than needing
    its own named exception type."""
    rd = _mk_run(runs_root, "run-live")
    _write_partial(rd, bad_usd)
    assert budget._partial_usd(rd) is None


def test_inflight_estimate_falls_back_to_flat_rate_on_a_huge_integer_usd(runs_root):
    """The pool-wide path, not just the unit-level `_partial_usd`, must
    survive the same forged value — confirmed to raise `OverflowError`
    uncaught against the pre-round-3 code, since `_partial_usd`'s own
    `except (TypeError, ValueError)` didn't catch it there either."""
    now = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)
    rd = _mk_run(runs_root, "run-live", started_min_ago=10, now=now)  # 10 min * 0.15
    _write_partial(rd, 10**400)
    assert budget.inflight_estimate_usd(now=now) == pytest.approx(1.5)


def test_status_survives_a_huge_integer_partial_usd(runs_root):
    """`budget.status()` is what `bin/agentctl`'s `_budget_gate` (checked
    synchronously before every spawn) and `bin/budget-watchdog`'s poll loop
    actually call — confirm the fix holds all the way up that call chain,
    not just at `_partial_usd`'s own boundary, since either caller crashing
    on one run's forged file would refuse every future spawn for the pool."""
    rd = _mk_run(runs_root, "run-live", started_min_ago=1, origin="scheduled")
    _write_partial(rd, 10**400)
    result = budget.status(pool="overnight")
    assert result["estimated_inflight_usd"] >= 0.0


def test_inflight_estimate_falls_back_to_flat_rate_on_a_forged_negative_partial(runs_root):
    """A run that wrote itself a large negative `cost-partial.json` `usd`
    (e.g. `echo '{"usd": -999999}' > $AGENTCTL_RUN_DIR/cost-partial.json`, the
    round-2 security-reviewer PoC) must not drag the pool-wide in-flight
    estimate below what it would be with no partial at all — the clamp in
    `_partial_usd` falls the run back to the flat elapsed*rate estimate
    exactly as if it had never checkpointed."""
    now = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)
    rd = _mk_run(runs_root, "run-live", started_min_ago=10, now=now)  # 10 min * 0.15
    _write_partial(rd, -999999.0)
    assert budget.inflight_estimate_usd(now=now) == pytest.approx(1.5)


@pytest.mark.parametrize("bad_cost_source", [["a", "b"], {"x": 1}], ids=["list", "dict"])
def test_inflight_estimate_falls_back_to_flat_rate_on_a_non_hashable_cost_source(runs_root, bad_cost_source):
    """Round 6 (security-reviewer finding on agent-platform#592): a
    `cost_source` of a list or dict made `read_cost_partial`'s membership
    test raise `TypeError` (unhashable), which escaped uncaught up through
    `_partial_usd` before this fix — confirm the pool-wide estimate degrades
    to the flat-rate fallback for such a run instead of crashing."""
    now = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)
    rd = _mk_run(runs_root, "run-live", started_min_ago=10, now=now)  # 10 min * 0.15
    (rd / "cost-partial.json").write_text(json.dumps(
        {"usd": 99.0, "tokens_in": 0, "tokens_out": 0, "cost_source": bad_cost_source,
         "by_model": {}, "written_at": "x"}))
    assert budget.inflight_estimate_usd(now=now) == pytest.approx(1.5)


def test_status_survives_a_non_hashable_cost_source_and_uses_the_flat_rate(runs_root):
    """`budget.status()` is the actual call chain `_budget_gate` (before every
    spawn) and `bin/budget-watchdog`'s poll loop use — confirm the fix holds
    there too, not just at `_partial_usd`'s own boundary, and that the run's
    contribution is the flat-rate estimate (1 min * default 0.15/min), not
    the forged `usd` figure or a crash."""
    rd = _mk_run(runs_root, "run-live", started_min_ago=1, origin="scheduled")
    (rd / "cost-partial.json").write_text(json.dumps(
        {"usd": 999.0, "tokens_in": 0, "tokens_out": 0, "cost_source": ["a", "b"],
         "by_model": {}, "written_at": "x"}))
    result = budget.status(pool="overnight")
    assert result["estimated_inflight_usd"] == pytest.approx(0.15, abs=0.01)


def test_a_finished_runs_cost_partial_is_never_read_once_cost_json_exists(runs_root):
    """cost.json's presence must supersede a stale cost-partial.json left
    behind from before finalisation — a completed run is counted exactly by
    today_total_usd() and must contribute NOTHING to the in-flight estimate,
    regardless of what its (now stale) partial says."""
    now = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)
    rd = _mk_run(runs_root, "run-done", cost=5.0)
    (rd / "cost-partial.json").write_text(json.dumps({"usd": 999.0}))
    assert budget.inflight_estimate_usd(now=now) == pytest.approx(0.0)
    assert budget.today_total_usd() == pytest.approx(5.0)


def test_inflight_estimate_skips_dead_pid_runs(runs_root):
    """A run whose recorded pid is gone burns nothing and must not accrue
    estimate (the 2026-06-11 $139.86 phantom that refused the day's spawns).
    A recorded ALIVE pid and a meta with no pid at all both keep estimating
    (the runaway-agent protection, and fail-safe for legacy metas)."""
    import os

    now = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)
    dead = _mk_run(runs_root, "run-dead", started_min_ago=600, now=now)
    meta = json.loads((dead / "meta.json").read_text())
    meta["pid"] = 2 ** 22 + 1  # beyond macOS/Linux default pid ranges
    (dead / "meta.json").write_text(json.dumps(meta))

    alive = _mk_run(runs_root, "run-alive", started_min_ago=10, now=now)
    meta = json.loads((alive / "meta.json").read_text())
    meta["pid"] = os.getpid()
    (alive / "meta.json").write_text(json.dumps(meta))

    _mk_run(runs_root, "run-nopid", started_min_ago=10, now=now)

    # only run-alive (10 min) + run-nopid (10 min) at 0.15/min
    assert budget.inflight_estimate_usd(now=now) == pytest.approx(3.0)


def test_inflight_estimate_excludes_parent_orchestrator(runs_root):
    """A parent (orchestrator) run is excluded from the in-flight estimate: its
    children are counted live by today_total_usd, so estimating the parent's
    wall-clock at the leaf burn rate would double-count them (ARCH-178). A
    plain in-flight leaf still estimates."""
    now = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)
    # The orchestrator: in-flight (no cost.json), referenced as a child's parent.
    _mk_run(runs_root, "run-orch", started_min_ago=240, now=now)
    # A child names run-orch as its parent — this is what marks run-orch a parent.
    _mk_run(runs_root, "run-child", cost=4.0, parent_run="run-orch")
    # An unrelated in-flight leaf still accrues estimate.
    _mk_run(runs_root, "run-leaf", started_min_ago=10, now=now)
    # Only run-leaf (10 min * 0.15); run-orch's 240 min is excluded.
    assert budget.inflight_estimate_usd(now=now) == pytest.approx(1.5)


def test_inflight_estimate_matches_parent_by_meta_run_id(runs_root):
    """A parent is matched whether the child's parent_run is the dir name or the
    meta `run_id` value (real runs carry run_id in meta)."""
    now = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)
    orch = _mk_run(runs_root, "run-orch", started_min_ago=200, now=now)
    meta = json.loads((orch / "meta.json").read_text())
    meta["run_id"] = "run-orch"
    (orch / "meta.json").write_text(json.dumps(meta))
    _mk_run(runs_root, "run-child", cost=2.0, parent_run="run-orch")
    assert budget.inflight_estimate_usd(now=now) == pytest.approx(0.0)


def test_regression_2026_06_17_orchestrator_no_false_soft_cap(runs_root, monkeypatch):
    """End-to-end pin of the 2026-06-17 false fire: an in-flight overnight
    orchestrator plus its completed children must read as the children's real
    cost, not children + a leaf-rate estimate of the parent's wall-clock. The
    $50 soft cap must NOT cross when true autonomous spend is ~$15."""
    monkeypatch.setenv(budget.ENV_SOFT, "50")
    monkeypatch.delenv(budget.ENV_HARD, raising=False)
    now = datetime(2026, 6, 17, 12, 0, 0, tzinfo=timezone.utc)  # 05:00 MST
    # The overnight-orchestrator: scheduled, in-flight 240 min, parent of children.
    _mk_run(runs_root, "run-orch", started_min_ago=240, now=now, origin="scheduled")
    # Children: completed, scheduled, ~$15 total — the real spend so far.
    for i, c in enumerate([5.0, 4.0, 3.0, 3.0]):
        _mk_run(runs_root, f"run-c{i}", cost=c, origin="scheduled",
                parent_run="run-orch")
    st = budget.status()
    # completed children counted exactly; orchestrator wall-clock NOT estimated
    assert st["completed_usd"] == pytest.approx(15.0)
    assert st["estimated_inflight_usd"] == pytest.approx(0.0)
    assert st["spent_today_usd"] == pytest.approx(15.0)
    assert st["soft_crossed"] is False  # would have been True (~$51) before the fix


def test_inflight_estimate_honors_burn_rate_env(runs_root, monkeypatch):
    monkeypatch.setenv(budget.ENV_BURN, "0.30")
    now = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)
    _mk_run(runs_root, "run-live", started_min_ago=10, now=now)
    assert budget.inflight_estimate_usd(now=now) == pytest.approx(3.0)


def test_live_total_is_completed_plus_inflight(runs_root):
    now = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)
    _mk_run(runs_root, "run-done", cost=4.0)
    _mk_run(runs_root, "run-live", started_min_ago=20, now=now)  # est 3.0
    # live_total_usd uses its own now(); assert completed floor + that estimate adds
    assert budget.today_total_usd() == 4.0
    assert budget.live_total_usd() >= 4.0


def test_get_thresholds_default_env_and_override(runs_root, monkeypatch):
    monkeypatch.delenv(budget.ENV_SOFT, raising=False)
    monkeypatch.delenv(budget.ENV_HARD, raising=False)
    assert budget.get_thresholds() == (30.0, 160.0)
    monkeypatch.setenv(budget.ENV_SOFT, "45")
    monkeypatch.setenv(budget.ENV_HARD, "120")
    assert budget.get_thresholds() == (45.0, 120.0)
    # per-spawn override beats the env soft value; hard still from env
    assert budget.get_thresholds(soft_override=70) == (70.0, 120.0)


def test_day_override_file_takes_precedence(runs_root, monkeypatch):
    monkeypatch.setenv(budget.ENV_SOFT, "30")
    monkeypatch.setenv(budget.ENV_HARD, "80")
    budget.today_dir().mkdir(parents=True, exist_ok=True)
    assert budget.get_thresholds() == (30.0, 80.0)  # no file → env
    f = budget.today_dir() / budget.DAY_OVERRIDE_NAME
    f.write_text(json.dumps({"soft": 200, "hard": 300}))
    assert budget.get_thresholds() == (200.0, 300.0)  # file beats env
    # per-spawn soft_override still wins for soft; hard from the override file
    assert budget.get_thresholds(soft_override=70) == (70.0, 300.0)


def test_day_override_partial_and_malformed_fail_open(runs_root, monkeypatch):
    monkeypatch.setenv(budget.ENV_SOFT, "30")
    monkeypatch.setenv(budget.ENV_HARD, "80")
    budget.today_dir().mkdir(parents=True, exist_ok=True)
    f = budget.today_dir() / budget.DAY_OVERRIDE_NAME
    f.write_text(json.dumps({"hard": 250}))      # only hard → soft falls back to env
    assert budget.get_thresholds() == (30.0, 250.0)
    f.write_text("not json{")                     # malformed → fail-open to env
    assert budget.get_thresholds() == (30.0, 80.0)


def test_status_flags_crossings(runs_root, monkeypatch):
    # Clear the production threshold overrides (the dev shell carries
    # ENV_SOFT=50, which made $35 read as not-crossed) so the test pins the
    # library defaults, not the machine's env.
    monkeypatch.delenv(budget.ENV_SOFT, raising=False)
    monkeypatch.delenv(budget.ENV_HARD, raising=False)
    _mk_run(runs_root, "run-done", cost=35.0, origin="scheduled")
    st = budget.status()  # default soft 30 / hard 160
    assert st["completed_usd"] == 35.0
    assert st["soft_crossed"] is True
    assert st["hard_crossed"] is False
    assert st["soft_marker_exists"] is False


def test_status_gates_on_scheduled_only_not_interactive(runs_root, monkeypatch):
    """The caps gate on scheduled-origin spend; interactive (co-session) spend
    is reported but never trips a crossing (ARCH-156 — the 2026-06-14 false
    soft-cap fire, where an evening of interactive work crossed the cap)."""
    monkeypatch.delenv(budget.ENV_SOFT, raising=False)
    monkeypatch.delenv(budget.ENV_HARD, raising=False)
    _mk_run(runs_root, "run-int", cost=40.0, origin="interactive")
    _mk_run(runs_root, "run-sched", cost=5.0, origin="scheduled")
    st = budget.status()  # default soft 30
    assert st["completed_usd"] == 5.0          # only scheduled counts
    assert st["spent_today_usd"] == 5.0
    assert st["interactive_today_usd"] == 40.0  # visible, separate
    assert st["soft_crossed"] is False          # $40 interactive does NOT cross


def test_status_counts_full_monty_in_the_capped_pool(runs_root, monkeypatch):
    """ARCH-202: full_monty is an autonomous (capped) origin, so its spend counts
    against the cap alongside scheduled — interactive still excluded."""
    monkeypatch.delenv(budget.ENV_SOFT, raising=False)
    monkeypatch.delenv(budget.ENV_HARD, raising=False)
    _mk_run(runs_root, "run-fm", cost=20.0, origin="full_monty")
    _mk_run(runs_root, "run-sched", cost=5.0, origin="scheduled")
    _mk_run(runs_root, "run-int", cost=40.0, origin="interactive")
    st = budget.status()
    assert st["completed_usd"] == 25.0          # full_monty + scheduled
    assert st["interactive_today_usd"] == 40.0  # still excluded from the cap
    assert "full_monty" in budget.CAPPED_ORIGINS


def test_full_monty_respects_elevated_cap_env(runs_root, monkeypatch):
    """The driver raises the caps via the budget env vars; a $1500 full_monty
    spend is under a $1700 soft cap (would have tripped the default $30)."""
    monkeypatch.setenv(budget.ENV_SOFT, "1700")
    monkeypatch.setenv(budget.ENV_HARD, "1900")
    _mk_run(runs_root, "run-fm", cost=1500.0, origin="full_monty")
    st = budget.status()
    assert st["spent_today_usd"] == 1500.0
    assert st["soft_crossed"] is False  # under the elevated soft cap
    assert st["hard_crossed"] is False


def test_untagged_run_defaults_to_interactive_and_is_excluded(runs_root, monkeypatch):
    """A run with no origin (pre-ARCH-156 or corrupt meta) fails OUT of the
    capped pool — never falsely into it."""
    monkeypatch.delenv(budget.ENV_SOFT, raising=False)
    monkeypatch.delenv(budget.ENV_HARD, raising=False)
    _mk_run(runs_root, "run-legacy", cost=50.0)  # no meta.json at all
    st = budget.status()
    assert st["spent_today_usd"] == 0.0
    assert st["interactive_today_usd"] == 50.0
    assert st["soft_crossed"] is False


def test_today_total_origin_filter(runs_root):
    _mk_run(runs_root, "run-s1", cost=2.0, origin="scheduled")
    _mk_run(runs_root, "run-s2", cost=3.0, origin="scheduled")
    _mk_run(runs_root, "run-i1", cost=10.0, origin="interactive")
    assert budget.today_total_usd(origin="scheduled") == 5.0
    assert budget.today_total_usd(origin="interactive") == 10.0
    assert budget.today_total_usd() == 15.0  # None = all origins


def test_inflight_estimate_origin_filter(runs_root):
    """The origin filter must apply to the in-flight estimate too, not just
    completed cost — otherwise a running interactive worker inflates the
    scheduled pool (the path the reviewer flagged as untested)."""
    now = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)
    _mk_run(runs_root, "run-s", started_min_ago=10, now=now, origin="scheduled")
    _mk_run(runs_root, "run-i", started_min_ago=20, now=now, origin="interactive")
    # scheduled only: 10 min * 0.15
    assert budget.inflight_estimate_usd(now=now, origin="scheduled") == pytest.approx(1.5)
    # interactive only: 20 min * 0.15
    assert budget.inflight_estimate_usd(now=now, origin="interactive") == pytest.approx(3.0)
    # None = both
    assert budget.inflight_estimate_usd(now=now) == pytest.approx(4.5)


def test_mark_crossed_is_idempotent_per_day(runs_root):
    assert budget.mark_crossed("soft") is True   # first crossing today
    assert budget.mark_crossed("soft") is False  # already marked
    assert budget.mark_crossed("hard") is True   # independent threshold
    assert budget.mark_crossed("hard") is False


def test_mark_crossed_rejects_unknown_kind(runs_root):
    with pytest.raises(ValueError):
        budget.mark_crossed("medium")


# --- ARCH-320: weekly curation runs in its own budget pool ---


def test_pool_mapping_and_capped_origins(runs_root):
    """curation is a capped origin in its OWN pool; scheduled + full_monty share
    the overnight pool; interactive is uncapped (unmapped)."""
    assert budget.POOL_BY_ORIGIN["scheduled"] == "overnight"
    assert budget.POOL_BY_ORIGIN["full_monty"] == "overnight"
    assert budget.POOL_BY_ORIGIN["curation"] == "curation"
    assert "curation" in budget.CAPPED_ORIGINS
    assert "interactive" not in budget.POOL_BY_ORIGIN


def test_curation_pool_has_its_own_defaults(runs_root, monkeypatch):
    """The curation pool carries its own (soft, hard); the overnight env vars must
    NOT move it (that's the whole point — it doesn't share the overnight ceiling)."""
    monkeypatch.setenv(budget.ENV_SOFT, "1700")   # full_monty raising overnight
    monkeypatch.setenv(budget.ENV_HARD, "1900")
    assert budget.get_thresholds("curation") == (35.0, 50.0)   # unmoved by env
    assert budget.get_thresholds("overnight") == (1700.0, 1900.0)  # env applies here


def test_pools_do_not_contend(runs_root, monkeypatch):
    """A maxed overnight pool leaves the curation pool untouched — the 2026-07-05
    Sunday-stacking incident this fix targets."""
    monkeypatch.delenv(budget.ENV_SOFT, raising=False)
    monkeypatch.delenv(budget.ENV_HARD, raising=False)
    # Overnight wave has blown well past its $160 hard cap.
    _mk_run(runs_root, "run-ov", cost=170.0, origin="scheduled")
    # Curation has spent only $10 — well under its own $50 hard cap.
    _mk_run(runs_root, "run-cur", cost=10.0, origin="curation")
    ov = budget.status("overnight")
    cur = budget.status("curation")
    assert ov["spent_today_usd"] == 170.0 and ov["hard_crossed"] is True
    assert cur["spent_today_usd"] == 10.0 and cur["hard_crossed"] is False
    assert cur["soft_crossed"] is False   # $10 < $35 curation soft
    assert cur["pool"] == "curation"


def test_per_pool_day_override(runs_root):
    """The override file accepts a per-pool shape; a flat shape stays overnight-only
    and must not move the curation cap."""
    budget.today_dir().mkdir(parents=True, exist_ok=True)
    f = budget.today_dir() / budget.DAY_OVERRIDE_NAME
    # per-pool shape
    f.write_text(json.dumps({"overnight": {"hard": 150}, "curation": {"hard": 70}}))
    assert budget.get_thresholds("overnight")[1] == 150.0
    assert budget.get_thresholds("curation")[1] == 70.0
    # flat shape = overnight only; curation falls back to its default $50
    f.write_text(json.dumps({"hard": 150}))
    assert budget.get_thresholds("overnight")[1] == 150.0
    assert budget.get_thresholds("curation") == (35.0, 50.0)


def test_mark_crossed_markers_are_per_pool(runs_root):
    """Crossing the overnight pool must not suppress the curation pool's first-cross
    alert (they use distinct marker files)."""
    assert budget.marker_name("soft", "overnight") == ".soft-budget-crossed"
    assert budget.marker_name("soft", "curation") == ".soft-budget-crossed-curation"
    assert budget.mark_crossed("soft", "overnight") is True
    assert budget.mark_crossed("soft", "overnight") is False
    assert budget.mark_crossed("soft", "curation") is True   # independent pool
    assert budget.mark_crossed("soft", "curation") is False


# --- PR-3: gateway cost.json is summed correctly by budget.py (ARCH-102) ---


def test_today_total_usd_inherits_real_dollars_from_gateway_cost_json(runs_root):
    """budget.today_total_usd() sums gateway cost.json usd correctly.

    The gateway's real cost lands in cost.json via _finalize_run; budget.py
    inherits it without any edit. This test pins that inheritance path.
    """
    d = budget.today_dir()
    d.mkdir(parents=True, exist_ok=True)
    rd = d / "run-gateway"
    rd.mkdir()
    # Write a cost.json that looks like a completed gateway run
    (rd / "cost.json").write_text(json.dumps({
        "tokens_in": 500, "tokens_out": 200, "usd": 0.0023,
        "cost_source": "gateway_spend_log",
        "by_model": {
            "deepseek/deepseek-v4-flash": {"tokens_in": 500, "tokens_out": 200, "usd": 0.0023}
        },
    }))
    assert budget.today_total_usd() == pytest.approx(0.0023)



# --- Round 4: a pathologically nested JSON body must not crash the budget
# path (agent-platform#592). A 64 KiB body of nested `[` brackets is valid
# JSON syntax but raises `RecursionError` out of `json.loads` — a
# `RuntimeError`, not caught by any of this module's pre-round-4 exception
# tuples (`(json.JSONDecodeError, ValueError, OSError)` /
# `(json.JSONDecodeError, ValueError, UnicodeDecodeError)`). Every site below
# is on the path `status()` / `inflight_estimate_usd()` / `today_total_usd()`
# / `_budget_gate` calls before every spawn, plus `bin/budget-watchdog`'s poll
# loop. ---

_NESTED_BRACKETS = "[" * (64 * 1024)


def test_today_total_usd_survives_a_pathologically_nested_cost_json(runs_root):
    """Confirmed to raise RecursionError uncaught against pre-round-4 code
    (the `except (json.JSONDecodeError, ValueError, OSError)` in
    `today_total_usd` doesn't catch it) — the malformed run must be skipped,
    not crash the sum for every other run."""
    rd = budget.today_dir() / "run-cost-bomb"
    rd.mkdir(parents=True)
    (rd / "cost.json").write_text(_NESTED_BRACKETS)
    _mk_run(runs_root, "run-good", cost=2.0)
    assert budget.today_total_usd() == pytest.approx(2.0)


def test_run_origin_survives_a_pathologically_nested_meta_json(runs_root):
    """Confirmed to raise RecursionError uncaught against pre-round-4 code —
    `_run_origin` must degrade to `interactive` like any other
    unreadable/corrupt meta.json, not raise."""
    rd = budget.today_dir() / "run-meta-bomb"
    rd.mkdir(parents=True)
    (rd / "meta.json").write_text(_NESTED_BRACKETS)
    assert budget._run_origin(rd) == "interactive"


def test_inflight_estimate_survives_a_pathologically_nested_meta_json(runs_root):
    """Confirmed to raise RecursionError uncaught against pre-round-4 code —
    the `except (json.JSONDecodeError, ValueError, OSError)` around the
    per-run loop body in `inflight_estimate_usd` doesn't catch it, so it
    escaped that one run's iteration and crashed the whole estimate."""
    now = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)
    rd = budget.today_dir() / "run-meta-bomb"
    rd.mkdir(parents=True)
    (rd / "meta.json").write_text(_NESTED_BRACKETS)
    _mk_run(runs_root, "run-good", started_min_ago=10, now=now)  # 10 min * 0.15
    assert budget.inflight_estimate_usd(now=now) == pytest.approx(1.5)


def test_partial_usd_survives_a_pathologically_nested_cost_partial_json(runs_root):
    """Confirmed to raise RecursionError uncaught against pre-round-4 code:
    `T.read_cost_partial(run_dir)` sat OUTSIDE `_partial_usd`'s try, so the
    RecursionError from `read_cost_partial`'s own `json.loads` escaped
    `_partial_usd` entirely rather than degrading to None like every other
    unreadable/corrupt partial."""
    rd = _mk_run(runs_root, "run-live")
    (rd / "cost-partial.json").write_text(_NESTED_BRACKETS)
    assert budget._partial_usd(rd) is None


def test_inflight_estimate_survives_a_pathologically_nested_cost_partial_json(runs_root):
    now = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)
    rd = _mk_run(runs_root, "run-live", started_min_ago=10, now=now)  # 10 min * 0.15
    (rd / "cost-partial.json").write_text(_NESTED_BRACKETS)
    assert budget.inflight_estimate_usd(now=now) == pytest.approx(1.5)


def test_status_survives_every_pathological_shape_planted_at_once(runs_root):
    """The end-to-end pin: plant a 64 KiB nested-bracket body at
    cost-partial.json, meta.json, and cost.json, each on its own run, all in
    the same capped pool on the same day — `budget.status()` (the exact call
    `_budget_gate` makes before every spawn, and `bin/budget-watchdog`'s poll
    loop makes on a timer) must still return a number rather than crashing
    the pool for every other run."""
    now = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)

    partial_run = _mk_run(runs_root, "run-partial-bomb", started_min_ago=5,
                          now=now, origin="scheduled")
    (partial_run / "cost-partial.json").write_text(_NESTED_BRACKETS)

    meta_run = _mk_run(runs_root, "run-meta-bomb", started_min_ago=5,
                       now=now, origin="scheduled")
    (meta_run / "meta.json").write_text(_NESTED_BRACKETS)

    cost_run = budget.today_dir() / "run-cost-bomb"
    cost_run.mkdir(parents=True, exist_ok=True)
    (cost_run / "cost.json").write_text(_NESTED_BRACKETS)

    good = _mk_run(runs_root, "run-good", cost=2.0, origin="scheduled")
    assert good.exists()

    result = budget.status(pool="overnight")
    assert isinstance(result["spent_today_usd"], float)
    assert result["completed_usd"] == pytest.approx(2.0)


def test_a_co_session_record_never_reaches_the_budget(runs_root):
    """THE OTHER HALF OF THE WS1.2 INVARIANT, and the dangerous one.

    cost_join's prefix filter has a regression test; budget.py's three
    startswith("run-") guards did not. inflight_estimate_usd is where it would
    bite: a co-session record carries a valid `started_at`, so relaxing that
    guard would multiply every interactive session's elapsed wall-clock by the
    leaf burn rate and add it to AUTONOMOUS spend — the number the daily hard cap
    gates on.
    """
    co = budget.today_dir() / "co-0199c7de-1234-4abc-8def-0123456789ab"
    co.mkdir(parents=True)
    (co / "meta.json").write_text(json.dumps({
        "kind": "co-session", "session_id": "0199c7de",
        "started_at": datetime.now(timezone.utc).isoformat(), "origin": "scheduled",
        "cwd": "/a/b", "pid": 1}), encoding="utf-8")

    assert budget.today_total_usd() == 0.0
    assert budget.inflight_estimate_usd() == 0.0
