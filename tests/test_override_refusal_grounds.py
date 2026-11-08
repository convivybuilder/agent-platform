"""Pins the policy-plane epic's own definition of done (work item E2·3,
issue_01M35KKNRBG36CEG04702CB04K): "A test asserts that no outcome-data or
taste ground can produce a refusal of an operator's override — the records
move defaults, not permissions."

The policy plane is going to make cost-per-outcome data available and use it
to move a persona's DEFAULT model. An operator's `--model` override must stay
permitted no matter what those records say — today nothing would fail if a
future refusal ground read them. This seeds a REAL, disfavouring record of
both kinds the DoD names — a cost-per-outcome record at the path `cost_join`
(`work-item-cost --outcomes`) reads, and a judged-taste record at the path
model-scorecard's own judge ledger writes — under the exact RUNS_ROOT
`_model_override_refusal` and `cmd_spawn`'s preflight read, then drives both
with an interactive-origin override and asserts it still goes through
(`tests/test_agentctl.py`'s own
`test_override_refusal_origin_permits_interactive_co_outside_any_worktree`
and `test_cmd_spawn_allows_model_changing_override_with_reason` are the two
patterns this mirrors — kept in a file of its own because three other pull
requests are editing test_agentctl.py right now).

Reuses the session-scoped `agentctl` fixture from tests/conftest.py, the same
way tests/test_agentctl_models.py does — no local redefinition needed.
"""
from __future__ import annotations

import json
import types
from datetime import datetime, timezone

import cost_join as cj


def _cmd_spawn_args(*, model, reason, parent=None):
    """The minimal argparse.Namespace cmd_spawn's preflight reads before any
    worktree/run-dir side effect. Mirrors tests/test_agentctl.py's own
    `_cmd_spawn_args`, kept local rather than imported from that file since it
    is being edited by other pull requests concurrently."""
    return types.SimpleNamespace(
        agent="reviewer", task="t", on="widget-crew", profile=None,
        agents_dir=None, parent=parent, pr=None, review_sha=None,
        model=model, reason=reason,
    )


def _seed_outcome_run(runs_root, date, run_id, *, model, exit_status, usd):
    """A real run dir at `<runs_root>/<date>/<run_id>/{meta,cost,outcome}.json`
    — exactly the three files `cost_join.load_node` reads (its own docstring),
    which `work-item-cost --outcomes` and `cost_join.build_outcome_table` read
    through. Written by hand, the same way `tests/test_cost_join.py`'s own
    `_mk_run` does, rather than through telemetry's write_* helpers, which
    enforce fields (e.g. `worktree`, `profile`) this cost-per-outcome read
    never looks at."""
    d = runs_root / date / run_id
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({
        "run_id": run_id, "agent": "coder", "model": model, "repo": "agent-platform",
        "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "origin": "interactive",
    }))
    (d / "cost.json").write_text(json.dumps(
        {"usd": usd, "by_model": {model: {"usd": usd}}}))
    (d / "outcome.json").write_text(json.dumps({"exit_status": exit_status}))
    return d


def _seed_judge_row(judge_path, *, model, fix_quality, review_thoroughness):
    """One real row appended to model-scorecard's own judge ledger shape
    (`build_judge_row`, bin/model-scorecard) — a judged-taste record of the
    kind the DoD's "taste ground" names."""
    judge_path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "run_id": "run-judged", "date": "2026-10-01", "agent": "coder",
        "model": model, "repo": "agent-platform", "pr_number": 1,
        "review_thoroughness": review_thoroughness, "fix_quality": fix_quality,
        "flagged_issues": [], "rationale": "synthetic test record",
        "judge_model": "judge-synthetic", "judge_cost_usd": 0.01,
        "judged_at": "2026-10-01T00:00:00Z",
    }
    with judge_path.open("a") as f:
        f.write(json.dumps(row) + "\n")


# The two model names a real `--model opus` dispatch actually resolves
# (agentctl.MODEL_ALIASES): the roster default `resolve_agent` is stubbed to
# below, and the override `_cmd_spawn_args` passes.
_BASELINE_MODEL = "claude-sonnet-5"
_OVERRIDE_MODEL = "claude-opus-5-5"


def _seed_disfavouring_records(agentctl, tmp_path, monkeypatch):
    """Real on-disk outcome and scorecard records, at the RUNS_ROOT
    `cost_join` and model-scorecard's own writers use, that make
    `_OVERRIDE_MODEL` the worst choice on both measures named in the DoD: the
    costliest FAILED outcome, and the worst judged taste. `_BASELINE_MODEL`
    gets the opposite of both, so a ground that compared the two would favour
    it every time.

    `cj.RUNS_ROOT` is pointed here, and so are `agentctl.RUNS_ROOT` and
    `telemetry.RUNS_ROOT` — every binding `bin/agentctl` holds (its own
    module-level `RUNS_ROOT`, bound at import from `builder_root.runs_root()`,
    and the separate `telemetry.RUNS_ROOT` it reads directly as `T.RUNS_ROOT`
    at a few call sites) — so a ground that read through any of them would
    still see these records, the same way `tests/test_agentctl.py` repoints
    both `agentctl.RUNS_ROOT` and `T.RUNS_ROOT` together everywhere it seeds a
    run under a temporary root. agentctl's run-registry origin checks
    (sources 2/3 of `_model_override_refusal`, independent of this data) are
    neutralised separately, below."""
    import telemetry as T
    runs_root = tmp_path / "agent-runs"
    runs_root.mkdir()
    monkeypatch.setattr(cj, "RUNS_ROOT", runs_root)
    monkeypatch.setattr(agentctl, "RUNS_ROOT", runs_root)
    monkeypatch.setattr(T, "RUNS_ROOT", runs_root)
    today = datetime.now(timezone.utc).date().isoformat()

    _seed_outcome_run(runs_root, today, "run-outcome-override",
                      model=_OVERRIDE_MODEL, exit_status="errored", usd=500.0)
    _seed_outcome_run(runs_root, today, "run-outcome-baseline",
                      model=_BASELINE_MODEL, exit_status="completed", usd=0.01)

    judge_path = runs_root / "scorecard" / "judge.jsonl"
    _seed_judge_row(judge_path, model=_OVERRIDE_MODEL,
                    fix_quality=1, review_thoroughness=1)
    _seed_judge_row(judge_path, model=_BASELINE_MODEL,
                    fix_quality=5, review_thoroughness=5)


def _neutralise_registry_origin_sources(agentctl, tmp_path, monkeypatch):
    """Sources 2/3 of `_model_override_refusal` read this process's own
    AGENTCTL_RUN_ID and the live run registry — neither is this test's
    concern, so both are cleared/emptied exactly as
    `test_override_refusal_origin_permits_interactive_co_outside_any_worktree`
    (tests/test_agentctl.py) does, isolating source 1 (`_run_origin`, which
    resolves plain `interactive` with no parent and no env override) as the
    only one in play."""
    empty_dir = tmp_path / "not-a-worktree"
    empty_dir.mkdir()
    monkeypatch.setattr(agentctl.R, "list_runs", lambda: [])
    monkeypatch.chdir(empty_dir)
    monkeypatch.delenv("AGENTCTL_RUN_ID", raising=False)
    monkeypatch.delenv("AGENT_RUN_ORIGIN", raising=False)


def test_model_override_refusal_permits_despite_disfavouring_records(
        agentctl, tmp_path, monkeypatch):
    """The DoD's rule, driven directly at `_model_override_refusal`: a real,
    disfavouring cost-per-outcome record and a real, disfavouring judged-taste
    record sit exactly where cost_join and model-scorecard read them, and the
    interactive-origin override is still permitted (returns None)."""
    _seed_disfavouring_records(agentctl, tmp_path, monkeypatch)
    _neutralise_registry_origin_sources(agentctl, tmp_path, monkeypatch)

    assert agentctl._model_override_refusal(None) is None, (
        "an outcome-data or taste record moved whether the override was "
        "permitted, not just what a default would have been")


def test_cmd_spawn_reaches_dispatch_despite_disfavouring_records(
        agentctl, tmp_path, monkeypatch):
    """The actual CLI entry point: cmd_spawn's own preflight (the path a real
    `--model` override takes) must reach R.can_spawn — i.e. not refuse —
    with the same disfavouring records in place. Mirrors tests/test_agentctl.
    py's `test_cmd_spawn_allows_model_changing_override_with_reason`."""
    from conftest import stub_profile_listing
    stub_profile_listing(agentctl, monkeypatch)

    _seed_disfavouring_records(agentctl, tmp_path, monkeypatch)
    _neutralise_registry_origin_sources(agentctl, tmp_path, monkeypatch)

    monkeypatch.setattr(agentctl, "_warn_deployed_tree", lambda: None)
    monkeypatch.setattr(agentctl, "_spawn_lib_gate", lambda: None)
    monkeypatch.setattr(agentctl, "resolve_agent",
                        lambda name, repo=None, agents_dir=None: {"model_alias": "sonnet"})
    reached = {}

    def _mark_reached(*a, **kw):
        reached["yes"] = True
        return False, [], "stopped here on purpose"
    monkeypatch.setattr(agentctl.R, "can_spawn", _mark_reached)

    args = _cmd_spawn_args(
        model="opus",
        reason="cost-per-outcome and taste records favour the baseline model here, "
               "but this task needs the stronger model's reasoning")
    agentctl.cmd_spawn(args)
    assert reached.get("yes"), (
        "a ground reading outcome or taste records refused an operator's override")
