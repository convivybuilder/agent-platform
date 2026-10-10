"""Active-runs registry (lib/agent-platform/registry.py).

The risk these guard: the ARCH-006/206/422 concurrency model — a per-team worker
cap (coordination limit) and a live-supervisor cap (fairness limit), both under a
higher global ceiling (raw-resource limit). If any of the three checks is off, or a
dead-PID run isn't reaped before the count, the platform either over-spawns (blows
the budget / over-coordinates a team / swaps the mini) or unjustly blocks a
legitimate spawn. The supervisor cap specifically guards the 2026-07-29 starvation:
nine orchestrators held nine of sixteen slots all night and constance PR #555 was
refused a reviewer nine times, which under draft-gated CI made it unmergeable. Required-field + duplicate validation guard
malformed registry entries that crash later readers.
"""
from __future__ import annotations

import os

import pytest
import registry


def _entry(run_id, *, pid=None, parent=None, agent="coder"):
    return {
        "run_id": run_id,
        "agent": agent,
        "task": "t",
        "started_at": "2026-05-31T00:00:00Z",
        "repo": "constance",
        "worktree": "/tmp/wt",
        "pid": pid if pid is not None else os.getpid(),  # default: alive
        "parent_run": parent,  # None = top-level (orchestrator/ad-hoc); else a team worker
    }


def test_add_run_then_get_and_list(runs_root):
    registry.add_run(_entry("run-1"))
    assert registry.get_run("run-1")["agent"] == "coder"
    assert [r["run_id"] for r in registry.list_runs()] == ["run-1"]


def test_add_run_missing_field_raises(runs_root):
    bad = _entry("run-x")
    del bad["worktree"]
    with pytest.raises(ValueError, match="missing fields"):
        registry.add_run(bad)


def test_add_run_duplicate_run_id_raises(runs_root):
    registry.add_run(_entry("run-dup"))
    with pytest.raises(ValueError, match="already registered"):
        registry.add_run(_entry("run-dup"))


def test_global_ceiling_enforced(runs_root):
    # top-level runs (no parent) are bounded only by the global ceiling
    for i in range(registry.MAX_CONCURRENT_DEFAULT):  # fill to the global limit
        registry.add_run(_entry(f"run-{i}"))
    ok, current, _ = registry.can_spawn()
    assert ok is False
    assert len(current) == registry.MAX_CONCURRENT_DEFAULT
    with pytest.raises(registry.ConcurrencyError, match="global") as ei:
        registry.add_run(_entry("run-over"))
    assert len(ei.value.current) == registry.MAX_CONCURRENT_DEFAULT


def test_per_team_worker_cap_enforced(runs_root):
    # an orchestrator (top-level) + max workers under it = team at its worker cap
    registry.add_run(_entry("orch-1"))
    for i in range(registry.MAX_WORKERS_PER_TEAM_DEFAULT):
        registry.add_run(_entry(f"w-{i}", parent="orch-1"))
    # a further worker on the SAME team is refused (coordination limit)
    assert registry.can_spawn("orch-1")[0] is False
    with pytest.raises(registry.ConcurrencyError, match="worker cap"):
        registry.add_run(_entry("w-over", parent="orch-1"))


def test_independent_teams_do_not_share_the_worker_cap(runs_root):
    # the whole point (ARCH-206): a second team gets its own pool; one full team
    # does not block another team's workers.
    registry.add_run(_entry("orch-1"))
    for i in range(registry.MAX_WORKERS_PER_TEAM_DEFAULT):
        registry.add_run(_entry(f"a-{i}", parent="orch-1"))
    assert registry.can_spawn("orch-1")[0] is False           # team 1 full
    registry.add_run(_entry("orch-2"))
    assert registry.can_spawn("orch-2")[0] is True            # team 2 has room
    registry.add_run(_entry("b-0", parent="orch-2"))          # and admits a worker
    assert registry.get_run("b-0")["parent_run"] == "orch-2"


def test_full_team_still_allows_top_level_and_other_teams(runs_root):
    registry.add_run(_entry("orch-1"))
    for i in range(registry.MAX_WORKERS_PER_TEAM_DEFAULT):
        registry.add_run(_entry(f"w-{i}", parent="orch-1"))
    assert registry.can_spawn("orch-1")[0] is False  # this team is full...
    assert registry.can_spawn()[0] is True           # ...but a top-level run is fine
    assert registry.can_spawn("orch-2")[0] is True   # ...and another team is fine


def test_global_ceiling_still_bounds_many_teams(runs_root):
    # per-team caps don't let total runs exceed the global raw-resource ceiling
    n = 0
    team = 0
    # add teams of (1 orch + workers) until another full team wouldn't fit
    while n + 1 + registry.MAX_WORKERS_PER_TEAM_DEFAULT <= registry.MAX_CONCURRENT_DEFAULT:
        registry.add_run(_entry(f"orch-{team}"))
        n += 1
        for i in range(registry.MAX_WORKERS_PER_TEAM_DEFAULT):
            registry.add_run(_entry(f"orch-{team}-w{i}", parent=f"orch-{team}"))
            n += 1
        team += 1
    # fill remaining slots with top-level runs, then the global ceiling refuses more
    while n < registry.MAX_CONCURRENT_DEFAULT:
        registry.add_run(_entry(f"top-{n}"))
        n += 1
    assert registry.can_spawn()[0] is False
    with pytest.raises(registry.ConcurrencyError, match="global"):
        registry.add_run(_entry("one-too-many"))


def test_dead_pid_is_reaped_before_cap(runs_root, monkeypatch):
    # Fill the registry, but mark every existing run's pid dead. A new spawn
    # must reap them and succeed rather than be falsely blocked.
    for i in range(registry.MAX_CONCURRENT_DEFAULT):
        registry.add_run(_entry(f"run-{i}"))
    monkeypatch.setattr(registry, "_is_alive", lambda pid: False)
    reaped = registry.reap_stale()
    assert len(reaped) == registry.MAX_CONCURRENT_DEFAULT
    # stale log written
    assert (runs_root / "stale-runs.jsonl").exists()
    # now under cap again
    ok, current, _ = registry.can_spawn()
    assert ok is True and current == []


def test_can_spawn_reaps_so_dead_run_does_not_block(runs_root, monkeypatch):
    registry.add_run(_entry("run-dead", pid=os.getpid()))
    monkeypatch.setattr(registry, "_is_alive", lambda pid: False)
    ok, current, _ = registry.can_spawn()
    assert ok is True
    assert current == []


def test_reason_global_for_worker_at_global_ceiling(runs_root):
    # PR #43 finding: a worker spawn that hits the GLOBAL ceiling (its team has
    # room) must report the global limit, not a misleading "team" message.
    for i in range(registry.MAX_CONCURRENT_DEFAULT):
        registry.add_run(_entry(f"top-{i}"))  # fill global with top-level runs
    ok, _current, reason = registry.can_spawn("orch-x")  # team orch-x has 0 workers
    assert ok is False
    assert "global" in reason and "worker cap" not in reason


def test_reason_team_for_worker_at_team_cap(runs_root):
    registry.add_run(_entry("orch-1"))
    for i in range(registry.MAX_WORKERS_PER_TEAM_DEFAULT):
        registry.add_run(_entry(f"w-{i}", parent="orch-1"))
    ok, _current, reason = registry.can_spawn("orch-1")
    assert ok is False
    assert "worker cap" in reason


def test_read_drops_the_limits_a_registry_stored(runs_root):
    """The limits come from the policy file. A pre-policy active.json stored all
    three, and nothing reads them, so `_read` drops them, `null` included."""
    import json
    (runs_root / "active.json").write_text(json.dumps(
        {"version": 1, "max_concurrent": 8, "max_workers_per_team": 2,
         "max_live_supervisors": None, "runs": []}))
    assert registry._read() == {"version": 1, "runs": []}


def test_remove_run(runs_root):
    registry.add_run(_entry("run-1"))
    assert registry.remove_run("run-1") is True
    assert registry.remove_run("run-1") is False
    assert registry.get_run("run-1") is None


def test_is_alive_probe():
    assert registry._is_alive(os.getpid()) is True
    assert registry._is_alive(None) is False
    assert registry._is_alive(-1) is False


# --- two-phase add (reserve_slot / attach_pid) --------------------------------


def _reservation(run_id, *, parent=None, started_at=None):
    """An entry dict suitable for reserve_slot (no pid field)."""
    return {
        "run_id": run_id,
        "agent": "coder",
        "task": "t",
        "started_at": started_at or registry._now_iso(),
        "repo": "agent-platform",
        "worktree": "/tmp/wt",
        "parent_run": parent,
    }


def test_reserve_slot_then_attach_happy_path(runs_root):
    """reserve_slot → attach_pid is the normal two-phase flow: the run appears
    with reserved=True (pid=None) after the first call, and with a real pid and
    no reserved flag after the second."""
    registry.reserve_slot(_reservation("run-r1"))
    # After reserve: entry is present with reserved=True and pid=None.
    entry = registry.get_run("run-r1")
    assert entry is not None
    assert entry.get("reserved") is True
    assert entry.get("pid") is None

    registry.attach_pid("run-r1", os.getpid())
    entry = registry.get_run("run-r1")
    assert entry["pid"] == os.getpid()
    assert "reserved" not in entry


def test_reserve_slot_counts_against_capacity(runs_root):
    """A reserved slot occupies a concurrency slot — it blocks a subsequent
    spawn that would push over the global ceiling."""
    # Fill to one below the ceiling with plain add_run entries.
    for i in range(registry.MAX_CONCURRENT_DEFAULT - 1):
        registry.add_run(_entry(f"run-{i}"))
    # One reserved slot fills it to exactly the ceiling.
    registry.reserve_slot(_reservation("run-res"))
    ok, _runs, reason = registry.can_spawn()
    assert ok is False
    assert "global" in reason


def test_reserve_slot_not_reaped_within_timeout(runs_root):
    """A fresh reservation is kept alive by the reaper (the process has not
    started yet — there is no pid to probe)."""
    registry.reserve_slot(_reservation("run-fresh"))
    reaped = registry.reap_stale()
    assert not reaped
    assert registry.get_run("run-fresh") is not None


def test_reserve_slot_reaped_after_timeout(runs_root, monkeypatch):
    """A reservation whose started_at is older than RESERVATION_TIMEOUT_SECONDS
    is treated as orphaned (the spawning process crashed before attach_pid) and
    reaped so it doesn't block future runs permanently."""
    expired_ts = "2020-01-01T00:00:00Z"
    entry = _reservation("run-old")
    entry["started_at"] = expired_ts
    registry.reserve_slot(entry)
    # The reaper must drop the orphaned reservation.
    reaped = registry.reap_stale()
    assert any(r["run_id"] == "run-old" for r in reaped)
    assert registry.get_run("run-old") is None


def test_reserve_slot_missing_field_raises(runs_root):
    bad = _reservation("run-bad")
    del bad["repo"]
    with pytest.raises(ValueError, match="missing fields"):
        registry.reserve_slot(bad)


def test_attach_pid_unknown_run_id_raises(runs_root):
    with pytest.raises(KeyError, match="not found"):
        registry.attach_pid("no-such-run", os.getpid())


def test_reserve_then_remove_releases_slot(runs_root):
    """remove_run on a reserved entry releases the slot so the next spawn
    can admit — this is the cleanup path when _spawn_claude raises."""
    # Fill to one below the ceiling.
    for i in range(registry.MAX_CONCURRENT_DEFAULT - 1):
        registry.add_run(_entry(f"run-{i}"))
    registry.reserve_slot(_reservation("run-res"))
    # At the ceiling — another spawn is blocked.
    ok, _, _ = registry.can_spawn()
    assert ok is False
    # Remove the reservation (spawn failed).
    registry.remove_run("run-res")
    ok, _, _ = registry.can_spawn()
    assert ok is True


def test_retry_then_succeed_pattern(runs_root, monkeypatch):
    """Simulate the agentctl retry loop: first N attempts hit the ceiling; once
    a slot frees, reserve_slot succeeds and attach_pid completes the entry."""
    # Fill the ceiling.
    for i in range(registry.MAX_CONCURRENT_DEFAULT):
        registry.add_run(_entry(f"run-{i}"))
    ok, _, _ = registry.can_spawn()
    assert ok is False  # sanity

    # Simulate one slot freeing: mark run-0's pid as dead so the reaper drops it.
    monkeypatch.setattr(registry, "_is_alive", lambda pid: pid != os.getpid())
    # reserve_slot reaps before checking capacity, so the dead entry clears the way.
    registry.reserve_slot(_reservation("run-retry"))
    entry = registry.get_run("run-retry")
    assert entry is not None and entry.get("reserved") is True
    registry.attach_pid("run-retry", os.getpid())
    assert registry.get_run("run-retry")["pid"] == os.getpid()


def test_capacity_refused_exit_status_is_valid():
    """capacity_refused must be a recognised exit_status so write_outcome
    doesn't raise when the retry budget exhausts."""
    import telemetry as T
    assert "capacity_refused" in T.VALID_EXIT_STATUSES


def test_write_outcome_records_error_reason(tmp_path):
    """write_outcome persists error_reason in outcome.json when provided."""
    import json
    import telemetry as T
    T.write_outcome(tmp_path, exit_status="capacity_refused",
                    error_reason="global concurrency ceiling (16/16) — retried for 300s")
    out = json.loads((tmp_path / "outcome.json").read_text())
    assert out["exit_status"] == "capacity_refused"
    assert "global concurrency ceiling" in out["error_reason"]


def test_write_outcome_omits_error_reason_when_none(tmp_path):
    """error_reason is absent from the payload when not supplied, keeping
    outcome.json backwards-compatible for callers that don't set it."""
    import json
    import telemetry as T
    T.write_outcome(tmp_path, exit_status="errored")
    out = json.loads((tmp_path / "outcome.json").read_text())
    assert "error_reason" not in out


def test_supervisor_predicate_matches_roles_and_app_prefixes():
    """`is_supervisor` decides which runs the fairness cap counts, so a false
    negative silently re-opens the starvation for that role."""
    for name in ("planning-orchestrator", "overnight-orchestrator", "overnight-triage",
                 "overnight-fix-drain", "overnight-verify",
                 "hollis-overnight-orchestrator", "sel-planning-orchestrator",
                 "hollis-overnight-verify"):
        assert registry.is_supervisor(name) is True, name
    for name in ("coder", "reviewer", "security-reviewer", "bug-fixer", "architect",
                 None, ""):
        assert registry.is_supervisor(name) is False, name


def test_supervisor_predicate_errs_toward_counting():
    """The predicate mirrors `is_orchestration_agent` and matches "orchestrator" as
    a SUBSTRING, so an unanticipated `*-orchestrator*` name counts as a supervisor.

    That is deliberate and it is the safe direction: a false positive caps one extra
    run, while a false negative silently re-opens the starvation for that role — the
    failure this cap exists to prevent. Pinned so nobody "tightens" it to an exact
    match and reintroduces the hole."""
    assert registry.is_supervisor("planning-orchestrator-helper") is True
    assert registry.is_supervisor("some-new-orchestrator") is True
    # a worker name with no orchestrator substring and no stage-agent suffix is not
    assert registry.is_supervisor("coder-helper") is False


def test_live_supervisor_cap_enforced(runs_root):
    """The 2026-07-29 failure, pinned: supervisors past the cap are refused while
    the global ceiling still has room."""
    for i in range(registry.MAX_LIVE_SUPERVISORS_DEFAULT):
        registry.add_run(_entry(f"orch-{i}", agent="planning-orchestrator"))
    # global ceiling is NOT the binding constraint here — that is the whole point
    assert len(registry.list_runs()) < registry.MAX_CONCURRENT_DEFAULT
    ok, _current, reason = registry.can_spawn(None, "planning-orchestrator")
    assert ok is False
    assert "live supervisor cap" in reason
    with pytest.raises(registry.ConcurrencyError, match="live supervisor cap"):
        registry.add_run(_entry("orch-over", agent="planning-orchestrator"))


def test_supervisor_cap_still_admits_workers(runs_root):
    """The fix must not become the bug it replaces. With supervisors at their cap,
    a WORKER spawn still has to be admitted — otherwise the fleet is starved by the
    fairness cap instead of by the supervisors, which is no better."""
    for i in range(registry.MAX_LIVE_SUPERVISORS_DEFAULT):
        registry.add_run(_entry(f"orch-{i}", agent="planning-orchestrator"))
    assert registry.can_spawn(None, "planning-orchestrator")[0] is False
    # a worker under one of those supervisors is fine
    assert registry.can_spawn("orch-0", "reviewer")[0] is True
    registry.add_run(_entry("w-0", parent="orch-0", agent="reviewer"))
    # so is a top-level ad-hoc non-supervisor spawn (an interactive co reviewer)
    assert registry.can_spawn(None, "reviewer")[0] is True
    registry.add_run(_entry("adhoc-0", agent="reviewer"))


def test_supervisor_cap_omitting_agent_skips_the_check(runs_root):
    """can_spawn's `agent` defaults to None for older callers. A None agent is not
    a supervisor, so the fairness cap is skipped at preflight — but add_run reads
    the agent off the ENTRY and enforces it anyway, so the cap cannot be bypassed
    by omitting the argument."""
    for i in range(registry.MAX_LIVE_SUPERVISORS_DEFAULT):
        registry.add_run(_entry(f"orch-{i}", agent="planning-orchestrator"))
    assert registry.can_spawn(None)[0] is True          # preflight is blind...
    with pytest.raises(registry.ConcurrencyError, match="live supervisor cap"):
        registry.add_run(_entry("orch-over", agent="planning-orchestrator"))


def test_dead_supervisor_is_reaped_before_the_cap_is_applied(runs_root):
    """A crashed orchestrator must not hold a supervisor slot forever. Same
    invariant the global ceiling already has, applied to the new cap."""
    for i in range(registry.MAX_LIVE_SUPERVISORS_DEFAULT):
        registry.add_run(_entry(f"orch-{i}", agent="planning-orchestrator"))
    assert registry.can_spawn(None, "planning-orchestrator")[0] is False
    # kill one: rewrite its pid to a dead one, then confirm a slot frees
    import json
    reg = json.loads(registry.ACTIVE_PATH.read_text())
    for r in reg["runs"]:
        if r["run_id"] == "orch-0":
            r["pid"] = 999999999  # not a live pid
    registry.ACTIVE_PATH.write_text(json.dumps(reg))
    assert registry.can_spawn(None, "planning-orchestrator")[0] is True


def test_supervisor_cap_is_not_overridable_in_the_registry(runs_root):
    """The cap is the policy's. A value stored in active.json changes nothing, so
    a person editing it expecting an effect (or a stale copy) cannot lower or raise
    the cap; tests/test_registry_policy.py covers the policy file as the source."""
    import json
    for i in range(registry.MAX_LIVE_SUPERVISORS_DEFAULT - 1):
        registry.add_run(_entry(f"orch-{i}", agent="planning-orchestrator"))
    reg = json.loads(registry.ACTIVE_PATH.read_text())
    reg["max_live_supervisors"] = 1
    registry.ACTIVE_PATH.write_text(json.dumps(reg))
    assert registry.can_spawn(None, "planning-orchestrator")[0] is True


def test_supervisor_set_matches_orchestrator_tokens_stage_agents():
    """Pin the two definitions together. `bin/orchestrator-tokens` has answered
    "is this an orchestration agent" since ARCH-268, and the first draft of this cap
    invented a second, incomplete answer that omitted `overnight-fix-drain` and
    `overnight-verify`. Reading the canonical set here means a future stage agent
    added there fails this test instead of silently escaping the cap."""
    import pathlib
    import re
    src = pathlib.Path(__file__).resolve().parents[1] / "bin" / "orchestrator-tokens"
    text = src.read_text()
    m = re.search(r"ORCHESTRATION_STAGE_AGENTS\s*=\s*\{([^}]*)\}", text)
    assert m, "could not find ORCHESTRATION_STAGE_AGENTS in bin/orchestrator-tokens"
    canonical = set(re.findall(r'"([^"]+)"', m.group(1)))
    assert canonical, "parsed an empty canonical set — the regex needs updating"
    missing = canonical - set(registry._SUPERVISOR_STAGE_AGENTS)
    assert not missing, (
        f"stage agent(s) {sorted(missing)} are orchestration agents per "
        f"bin/orchestrator-tokens but are invisible to the ARCH-422 supervisor cap")
    for name in canonical:
        assert registry.is_supervisor(name) is True, name
