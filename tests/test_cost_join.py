"""cost_join — session-tree reconstruction + cost rollup (ARCH-191 / ARCH-239).

The risks these guard:
  • a dispatch chain must join across date-dir boundaries (an overnight run that
    crosses midnight UTC) or the orchestrator's cost is split from its workers';
  • both cost.json shapes (with and without `cost_source`) must price identically;
  • an interactive co session's OWN main-loop cost (transcript-only, no run-dir)
    must be joined in, with NO double-count of its subagents;
  • cost-per-outcome must sum the subtree rooted at the PR-opening node.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import cost_join as cj
import pytest


@pytest.fixture
def cj_root(tmp_path, monkeypatch):
    """Repoint cost_join at isolated tmp run-root + projects dirs."""
    runs = tmp_path / "agent-runs"
    runs.mkdir()
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr(cj, "RUNS_ROOT", runs)
    monkeypatch.setattr(cj, "CLAUDE_PROJECTS", projects)
    return tmp_path


def _mk_run(runs, date, name, *, usd=0.0, by_model=None, cost_source=None,
            agent="coder", model="claude-sonnet-4-6", parent_run=None,
            started=None, ended=None, repo="vivy", origin=None, profile="",
            pr=None, exit_status="completed", co_session=None, overnight_wave=None):
    d = runs / "agent-runs" / date / name
    d.mkdir(parents=True)
    meta = {"run_id": name, "agent": agent, "model": model, "repo": repo,
            "parent_run": parent_run, "profile": profile}
    if co_session:
        meta["co_session"] = co_session
    if overnight_wave:
        meta["overnight_wave"] = overnight_wave
    if started:
        meta["started_at"] = started
    if ended:
        meta["ended_at"] = ended
    if origin:
        meta["origin"] = origin
    (d / "meta.json").write_text(json.dumps(meta))
    cost = {"usd": usd, "by_model": by_model or {model: {"usd": usd}}}
    if cost_source:
        cost["cost_source"] = cost_source
    (d / "cost.json").write_text(json.dumps(cost))
    (d / "outcome.json").write_text(json.dumps({"exit_status": exit_status}))
    if pr:
        (d / "pr-link.json").write_text(json.dumps(
            {"pr_url": f"https://github.com/convivy/{repo}/pull/{pr}", "pr_number": pr}))
    return d


def _mk_sub(runs, date, agent_id, *, uuid, usd=0.0, agent="reviewer",
            model="claude-sonnet-4-6", started=None, repo="vivy"):
    d = runs / "agent-runs" / date / f"sub-{agent_id}"
    d.mkdir(parents=True)
    meta = {"run_id": f"sub-{agent_id}", "agent": agent, "model": model,
            "repo": repo, "parent_run": uuid, "origin": "interactive"}
    if started:
        meta["started_at"] = started
    (d / "meta.json").write_text(json.dumps(meta))
    (d / "cost.json").write_text(json.dumps(
        {"usd": usd, "by_model": {model: {"usd": usd}}, "cost_source": "estimate_fallback"}))
    (d / "outcome.json").write_text(json.dumps({"exit_status": "completed"}))
    (d / "subagent.json").write_text(json.dumps({"turns": 12, "agent_id": agent_id, "source": "SubagentStop"}))
    return d


def _mk_transcript(root, uuid, turns, projdir="-Users-example-Convivy-vivy", cwd=None):
    """Write a co-session transcript: turns is a list of (model, usage-dict).

    `cwd` (the session's working dir, recorded per Claude Code record) is what
    co_main_loop_node resolves the product from; pass a non-existent path so the git
    resolution fails and falls back to the basename — hermetic and product == basename."""
    proj = root / "projects" / projdir
    proj.mkdir(parents=True, exist_ok=True)
    lines = []
    for i, (model, usage) in enumerate(turns):
        rec = {
            "type": "assistant",
            "timestamp": f"2026-06-10T12:0{i}:00.000Z",
            "message": {"model": model, "usage": usage},
        }
        if cwd:
            rec["cwd"] = cwd
        lines.append(json.dumps(rec))
    (proj / f"{uuid}.jsonl").write_text("\n".join(lines))


WIN_START = datetime(2026, 6, 1, tzinfo=timezone.utc)
WIN_END = datetime(2026, 6, 30, tzinfo=timezone.utc)


def test_canonical_product_reports_the_three_nyh_projects_separately():
    # The NYH repos are three PROJECTS, each reporting for itself (ARCH-426
    # amendment, operator, 2026-07-31). They were folded into one "NYH Studio" bucket
    # while the umbrella directory was the only thing holding them together; NYH
    # Studio survives as the product all three build.
    assert cj._canonical_product("nyh-arch-tools") == "NYH Arch Tools"
    assert cj._canonical_product("nyh-etc") == "East Texas Circle"
    assert cj._canonical_product("nyh-park-villa") == "Park Villa"
    # the bare umbrella is one historical session (2026-06-08), attributed to the
    # studio-wide project rather than left as a raw one-row sliver
    assert cj._canonical_product("nyh-architecture") == "NYH Arch Tools"
    # A FOURTH property must appear as itself or not at all — the table replaced a
    # `nyh-*` prefix match precisely so a new building cannot be swallowed silently.
    assert cj._canonical_product("nyh-somewhere-new") == "nyh-somewhere-new"
    # Hollis (formerly personal-assistant, ARCH-290) folds its three tags into one.
    assert cj._canonical_product("hollis") == "Hollis"
    assert cj._canonical_product("personal-assistant") == "Hollis"
    assert cj._canonical_product("pa") == "Hollis"
    # other products pass through untouched; None → "?"
    assert cj._canonical_product("vivy") == "vivy"
    assert cj._canonical_product(None) == "?"


def test_agentctl_tree_sums_cost_across_chain(cj_root):
    _mk_run(cj_root, "2026-06-10", "run-root", usd=10.0, agent="overnight-orchestrator",
            parent_run=None, started="2026-06-10T08:00:00Z", ended="2026-06-10T09:00:00Z",
            origin="scheduled")
    _mk_run(cj_root, "2026-06-10", "run-w1", usd=2.0, parent_run="run-root",
            started="2026-06-10T08:10:00Z")
    _mk_run(cj_root, "2026-06-10", "run-w2", usd=3.0, parent_run="run-root",
            started="2026-06-10T08:20:00Z")
    sessions = cj.build_sessions(WIN_START, WIN_END)
    assert len(sessions) == 1
    s = sessions[0]
    assert s["kind"] == "agentctl"
    assert s["worker_count"] == 2
    assert s["total_usd"] == pytest.approx(15.0)
    assert s["origin"] == "scheduled"
    assert len(s["nodes"]) == 3


def test_missing_cost_source_prices_same(cj_root):
    """Shape B (no cost_source) is treated as anthropic_cli, usd unchanged."""
    _mk_run(cj_root, "2026-06-10", "run-a", usd=4.0, cost_source=None,
            parent_run=None, started="2026-06-10T08:00:00Z")
    [s] = cj.build_sessions(WIN_START, WIN_END)
    assert s["total_usd"] == pytest.approx(4.0)
    assert s["nodes"][0]["cost_source"] == "anthropic_cli"


def test_tree_joins_across_date_boundary(cj_root):
    """A child landing in the next day's dir still joins its parent."""
    _mk_run(cj_root, "2026-06-10", "run-root", usd=1.0, parent_run=None,
            started="2026-06-10T23:30:00Z")
    _mk_run(cj_root, "2026-06-11", "run-child", usd=5.0, parent_run="run-root",
            started="2026-06-11T00:10:00Z")
    [s] = cj.build_sessions(WIN_START, WIN_END)
    assert s["worker_count"] == 1
    assert s["total_usd"] == pytest.approx(6.0)


def test_root_outside_window_excluded(cj_root):
    _mk_run(cj_root, "2026-05-01", "run-old", usd=9.0, parent_run=None,
            started="2026-05-01T08:00:00Z")
    assert cj.build_sessions(WIN_START, WIN_END) == []


def test_co_main_loop_node_prices_transcript(cj_root):
    uuid = "11111111-2222-3333-4444-555555555555"
    # Opus 4.8: input 5/Mtok, output 25/Mtok -> 1000*5e-6 + 500*25e-6 = 0.0175
    _mk_transcript(cj_root, uuid, [
        ("claude-opus-4-8", {"input_tokens": 1000, "output_tokens": 500,
                             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}),
        ("<synthetic>", {"input_tokens": 9999}),  # skipped
    ])
    node = cj.co_main_loop_node(uuid)
    assert node is not None
    assert node["kind"] == "co"
    assert node["turns"] == 1  # synthetic turn not counted
    assert node["usd"] == pytest.approx(0.0175)
    assert node["model"] == "claude-opus-4-8"


def test_co_main_loop_node_prices_a_1h_cache_write_at_the_1h_rate(cj_root):
    """A transcript turn's `usage.cache_creation` TTL breakdown must survive
    `co_main_loop_node`'s per-turn accumulation into `T.cost_from_usage`.
    Sonnet 5: input $2/Mtok, 1h cache write $4/Mtok — a turn with no
    input/output tokens and 1M 1h-cache-write tokens prices to exactly $4
    only if the breakdown reached cost_from_usage; dropping it (falling
    back to the flat `cache_creation_input_tokens` field, which this turn
    leaves at 0) would price it at $0."""
    uuid = "66666666-7777-8888-9999-000000000000"
    _mk_transcript(cj_root, uuid, [
        ("claude-sonnet-5", {"input_tokens": 0, "output_tokens": 0,
                             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
                             "cache_creation": {"ephemeral_1h_input_tokens": 1_000_000,
                                                "ephemeral_5m_input_tokens": 0}}),
    ])
    node = cj.co_main_loop_node(uuid)
    assert node is not None
    assert node["usd"] == pytest.approx(4.0)


def test_co_main_loop_missing_transcript_returns_none(cj_root):
    assert cj.co_main_loop_node("no-such-uuid") is None


def test_co_main_loop_dedupes_repeated_message_id(cj_root):
    # Claude Code writes the same assistant response into the transcript more than
    # once (identical message.id + requestId, byte-identical usage). Summing each
    # occurrence double-counts a co main-loop's cost (~2x observed, ARCH-383); the
    # reconstruction must count a repeated response once, matching ccusage.
    uuid = "dddddddd-0000-1111-2222-333333333333"
    proj = cj_root / "projects" / "-Users-example-Convivy-vivy"
    proj.mkdir(parents=True, exist_ok=True)
    usage = {"input_tokens": 1000, "output_tokens": 500,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}

    def rec(mid, rid):
        return json.dumps({"type": "assistant", "timestamp": "2026-06-10T12:00:00.000Z",
                           "requestId": rid,
                           "message": {"id": mid, "model": "claude-opus-4-8", "usage": usage}})

    # msg_A written 3x (a duplicate response), plus a distinct msg_B. Opus 4.8 at
    # 1000 in / 500 out = 0.0175 each → two unique responses = 0.035, NOT 4×0.0175.
    (proj / f"{uuid}.jsonl").write_text("\n".join([
        rec("msg_A", "req_A"), rec("msg_A", "req_A"), rec("msg_A", "req_A"),
        rec("msg_B", "req_B"),
    ]))
    node = cj.co_main_loop_node(uuid)
    assert node is not None
    assert node["turns"] == 2                       # two unique responses, not four records
    assert node["usd"] == pytest.approx(0.035)      # deduped; raw-sum would be 0.07


def test_co_main_loop_counts_keyless_records(cj_root):
    # A record with neither message.id nor requestId is counted, never collapsed
    # with other keyless records (the dedup guard only fires on a present key).
    uuid = "eeeeeeee-0000-1111-2222-333333333333"
    proj = cj_root / "projects" / "-Users-example-Convivy-vivy"
    proj.mkdir(parents=True, exist_ok=True)
    u = {"input_tokens": 1000, "output_tokens": 500}

    def keyless():
        return json.dumps({"type": "assistant", "timestamp": "2026-06-10T12:00:00.000Z",
                           "message": {"model": "claude-opus-4-8", "usage": u}})

    (proj / f"{uuid}.jsonl").write_text("\n".join([keyless(), keyless()]))
    node = cj.co_main_loop_node(uuid)
    assert node["turns"] == 2                        # both counted despite identical usage
    assert node["usd"] == pytest.approx(0.035)


def test_interactive_session_joins_main_loop_and_subs(cj_root):
    uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    _mk_transcript(cj_root, uuid, [
        ("claude-opus-4-8", {"input_tokens": 2000, "output_tokens": 1000,
                             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}),
    ])  # cost = 2000*5e-6 + 1000*25e-6 = 0.035
    _mk_sub(cj_root, "2026-06-10", "sub1", uuid=uuid, usd=0.5,
            started="2026-06-10T12:05:00Z")
    _mk_sub(cj_root, "2026-06-10", "sub2", uuid=uuid, usd=0.25,
            started="2026-06-10T12:06:00Z")
    [s] = cj.build_sessions(WIN_START, WIN_END)
    assert s["kind"] == "interactive"
    assert s["main_loop_captured"] is True
    assert s["worker_count"] == 2  # subagents only
    # main-loop 0.035 + 0.5 + 0.25
    assert s["total_usd"] == pytest.approx(0.785)
    depths = sorted(n["depth"] for n in s["nodes"])
    assert depths == [0, 1, 1]  # one main-loop root + two workers


def test_interactive_agentctl_spawn_nests_under_co_session(cj_root):
    """An interactive `agentctl spawn` (run-*, no parent_run) that stamped its co
    UUID nests under the co session, not as a top-level root (operator, 2026-06-27)."""
    uuid = "11111111-2222-3333-4444-555555555555"
    _mk_transcript(cj_root, uuid, [
        ("claude-opus-4-8", {"input_tokens": 2000, "output_tokens": 1000,
                             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}),
    ])  # main-loop cost 0.035
    _mk_sub(cj_root, "2026-06-10", "subA", uuid=uuid, usd=0.25,
            started="2026-06-10T12:05:00Z")
    # the reviewer I dispatched: a run-*, parent_run None, co_session = the co UUID
    _mk_run(cj_root, "2026-06-10", "run-rev", usd=0.6, agent="reviewer",
            parent_run=None, co_session=uuid, origin="interactive",
            started="2026-06-10T12:10:00Z")
    sessions = cj.build_sessions(WIN_START, WIN_END)
    assert len(sessions) == 1                 # one co session, no orphan root
    [s] = sessions
    assert s["kind"] == "interactive"
    assert s["worker_count"] == 2             # captured sub + agentctl reviewer
    assert s["total_usd"] == pytest.approx(0.885)  # 0.035 + 0.25 + 0.6
    agents = sorted(n["agent"] for n in s["nodes"])
    assert "reviewer" in agents
    assert sorted(n["depth"] for n in s["nodes"]) == [0, 1, 1]


def test_scheduled_spawn_with_parent_ignores_co_session(cj_root):
    """An orchestrator worker carries --parent (a run-id) and nests by run-id;
    a co_session value, if present, must not pull it into an interactive bucket."""
    _mk_run(cj_root, "2026-06-10", "run-orch", usd=1.0, agent="overnight-orchestrator",
            parent_run=None, origin="scheduled", started="2026-06-10T01:00:00Z")
    _mk_run(cj_root, "2026-06-10", "run-w", usd=2.0, agent="coder",
            parent_run="run-orch", co_session="should-be-ignored",
            origin="scheduled", started="2026-06-10T01:05:00Z")
    [s] = cj.build_sessions(WIN_START, WIN_END)
    assert s["kind"] == "agentctl"
    assert s["root_run_id"] == "run-orch"
    assert s["worker_count"] == 1
    assert s["total_usd"] == pytest.approx(3.0)


def test_overnight_wave_groups_stage_runs_into_one_night(cj_root):
    """ARCH-268 phase-split stage runs (no parent_run, no co_session) sharing a
    wave key nest under one synthetic 'overnight night' row instead of orphaning
    each as a top-level root (canary follow-up 2026-06-27)."""
    wave = "sous-chef/2026-06-10"
    for stage, agent, t in [
        ("triage", "overnight-triage", "01:00:00"),
        ("fixdrain", "overnight-fix-drain", "01:10:00"),
        ("verify", "overnight-verify", "02:00:00"),
        ("digest", "explainer", "02:30:00"),
    ]:
        _mk_run(cj_root, "2026-06-10", f"run-{stage}", usd=1.0, agent=agent,
                parent_run=None, overnight_wave=wave, origin="scheduled",
                repo="sous-chef", started=f"2026-06-10T{t}Z")
    # a fix-drain worker nests by run-id (it carries the inherited wave too, but
    # parent_run takes precedence) — it must NOT spawn a second night row.
    _mk_run(cj_root, "2026-06-10", "run-fd-worker", usd=2.0, agent="coder",
            parent_run="run-fixdrain", overnight_wave=wave, origin="scheduled",
            repo="sous-chef", started="2026-06-10T01:15:00Z")
    sessions = cj.build_sessions(WIN_START, WIN_END)
    assert len(sessions) == 1                       # one night, no orphan roots
    [s] = sessions
    assert s["kind"] == "overnight"
    assert s["session_id"] == f"overnight:{wave}"
    assert s["product"] == "sous-chef"
    assert s["label"] == "overnight · sous-chef · 2026-06-10"
    assert s["worker_count"] == 1                    # 5 nodes - 4 stage roots
    assert s["total_usd"] == pytest.approx(6.0)      # 4 stages + 1 worker
    # the worker is nested one level under its fix-drain stage root.
    depths = sorted(n["depth"] for n in s["nodes"])
    assert depths == [0, 0, 0, 0, 1]


def test_curation_wave_groups_dispatches_into_one_row(cj_root):
    """ARCH-122 weekly-curation dispatches (security-auditor per product +
    observability-scribe + curator), sharing an explicit `curation:<date>` wave,
    nest under one synthetic 'curation' row instead of orphaning each as its own
    top-level scheduled session (operator, 2026-06-28)."""
    wave = "curation:2026-06-10"
    for agent, repo, t in [
        ("security-auditor", "constance", "10:01:00"),
        ("security-auditor", "vivy", "10:11:00"),
        ("security-auditor", "sous-chef", "10:23:00"),
        ("observability-scribe", "sous-chef", "10:30:00"),
        ("curator", "sous-chef", "10:37:00"),
    ]:
        _mk_run(cj_root, "2026-06-10", f"run-{agent}-{repo}", usd=1.0, agent=agent,
                parent_run=None, overnight_wave=wave, origin="scheduled",
                repo=repo, started=f"2026-06-10T{t}Z")
    sessions = cj.build_sessions(WIN_START, WIN_END)
    assert len(sessions) == 1                        # one curation row, no orphans
    [s] = sessions
    assert s["kind"] == "curation"
    assert s["session_id"] == wave                   # not doubled (curation:curation:…)
    assert s["product"] == "all"                     # spans every swept product
    assert s["label"] == "curation · 2026-06-10"
    assert s["origin"] == "scheduled"
    assert s["worker_count"] == 0                    # 5 top-level dispatches, no sub-workers
    assert s["total_usd"] == pytest.approx(5.0)
    assert sorted(n["depth"] for n in s["nodes"]) == [0, 0, 0, 0, 0]


def test_interactive_without_transcript_marks_gap(cj_root):
    uuid = "ffffffff-0000-1111-2222-333333333333"
    _mk_sub(cj_root, "2026-06-10", "subx", uuid=uuid, usd=0.5,
            started="2026-06-10T12:05:00Z")
    [s] = cj.build_sessions(WIN_START, WIN_END)
    assert s["main_loop_captured"] is False
    assert s["total_usd"] == pytest.approx(0.5)


def test_build_outcomes_sums_subtree_per_pr(cj_root):
    _mk_run(cj_root, "2026-06-10", "run-orch", usd=1.0, parent_run=None,
            agent="overnight-orchestrator", started="2026-06-10T08:00:00Z")
    _mk_run(cj_root, "2026-06-10", "run-coder", usd=4.0, parent_run="run-orch",
            agent="coder", started="2026-06-10T08:10:00Z", pr=512)
    _mk_run(cj_root, "2026-06-10", "run-coder-helper", usd=1.5, parent_run="run-coder",
            agent="coder", started="2026-06-10T08:20:00Z")
    outcomes = cj.build_outcomes(WIN_START, WIN_END)
    assert len(outcomes) == 1
    o = outcomes[0]
    assert o["pr_number"] == 512
    assert o["product"] == "vivy"
    # subtree of run-coder = 4.0 + 1.5 (its helper); the orchestrator is NOT included
    assert o["chain_usd"] == pytest.approx(5.5)
    assert o["node_usd"] == pytest.approx(4.0)
    assert o["session_root"] == "run-orch"


def test_build_outcomes_empty_when_no_pr(cj_root):
    _mk_run(cj_root, "2026-06-10", "run-a", usd=1.0, parent_run=None,
            started="2026-06-10T08:00:00Z")
    assert cj.build_outcomes(WIN_START, WIN_END) == []


# --- main-loop-only surfacing (all products, not just PA) -----------------------

_PA_DIR = "-Users-example-Convivy-personal-assistant"


def test_lab_product_mainloop_only_session_surfaced(cj_root):
    """A PA co session that dispatched NO subagents (no sub-* run-dir) is surfaced
    from its transcript alone — the cost that was invisible before ARCH-243."""
    uuid = "deadbeef-0000-1111-2222-333333333333"
    _mk_transcript(cj_root, uuid, [
        ("claude-sonnet-4-6", {"input_tokens": 100000, "output_tokens": 20000,
                               "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}),
    ], projdir=_PA_DIR, cwd="/repo/personal-assistant")
    [s] = cj.build_sessions(WIN_START, WIN_END)
    assert s["kind"] == "interactive"
    assert s["product"] == "Hollis"
    assert s["worker_count"] == 0
    assert s["main_loop_captured"] is True
    assert s["total_usd"] > 0
    assert len(s["nodes"]) == 1 and s["nodes"][0]["depth"] == 0


def test_lab_product_with_subagents_not_double_surfaced(cj_root):
    """A PA session that DID dispatch a subagent is surfaced once (via the normal
    interactive path with its child), not also re-added by the main-loop scan."""
    uuid = "cafef00d-4444-5555-6666-777777777777"
    _mk_transcript(cj_root, uuid, [
        ("claude-sonnet-4-6", {"input_tokens": 5000, "output_tokens": 1000,
                               "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}),
    ], projdir=_PA_DIR, cwd="/repo/personal-assistant")
    _mk_sub(cj_root, "2026-06-10", "pasub", uuid=uuid, usd=0.4,
            agent="pa-reviewer", repo="personal-assistant", started="2026-06-10T12:05:00Z")
    sessions = [s for s in cj.build_sessions(WIN_START, WIN_END)
                if s["session_id"] == uuid]
    assert len(sessions) == 1            # exactly one, no double-count
    assert sessions[0]["worker_count"] == 1
    assert sessions[0]["product"] == "Hollis"


def test_mainloop_scan_surfaces_every_product(cj_root):
    """The scan is no longer PA-scoped (operator, 2026-06-30): a vivy co session with no
    subagents surfaces too — its main-loop cost was the dominant, previously-dropped
    spend (ARCH-280). Product comes from the transcript's own cwd."""
    uuid = "0badf00d-8888-9999-aaaa-bbbbbbbbbbbb"
    _mk_transcript(cj_root, uuid, [
        ("claude-opus-4-8", {"input_tokens": 1000, "output_tokens": 500,
                             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}),
    ], projdir="-Users-example-Convivy-vivy", cwd="/repo/vivy")
    [s] = cj.build_sessions(WIN_START, WIN_END)
    assert s["kind"] == "interactive"
    assert s["product"] == "vivy"
    assert s["worker_count"] == 0
    assert s["main_loop_captured"] is True


def test_mainloop_scan_skips_worktree_dirs(cj_root):
    """A transcript in a worktree dir (a dispatched worker, #3b) is NOT surfaced as a
    top-level main-loop session — only real product-root co sessions are."""
    uuid = "beadfeed-1111-2222-3333-444444444444"
    _mk_transcript(cj_root, uuid, [
        ("claude-opus-4-8", {"input_tokens": 1000, "output_tokens": 500,
                             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}),
    ], projdir="-Users-example-Convivy-worktrees-vivy-coder-fix-x", cwd="/repo/worktrees/x")
    assert cj.build_sessions(WIN_START, WIN_END) == []


def test_worker_session_suppressed_when_transcript_in_worktree(cj_root):
    """A dispatched worker (its Task subagents stamped parent_run=<worker UUID>, and
    its own transcript lives in a worktree dir) does NOT surface as a phantom top-level
    interactive row — #3b. Without the guard it showed a worktree-basename product."""
    worker_uuid = "9999aaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    _mk_transcript(cj_root, worker_uuid, [
        ("claude-sonnet-4-6", {"input_tokens": 1000, "output_tokens": 200}),
    ], projdir="-Users-example-Convivy-worktrees-vivy-reviewer-pr-9", cwd="/repo/worktrees/r")
    _mk_sub(cj_root, "2026-06-10", "wsub", uuid=worker_uuid, usd=0.3,
            started="2026-06-10T12:05:00Z")
    assert cj.build_sessions(WIN_START, WIN_END) == []


def test_sdk_worker_session_suppressed_by_worktree_children(cj_root):
    """An SDK worker keyed by an `agent-<hex>` id leaves no product-root transcript, so
    the transcript check can't see it; it's suppressed because every captured child ran
    in a worktree (#3b, kids-based signal)."""
    _mk_sub(cj_root, "2026-06-10", "sdksub", uuid="agent-a562bc71051ec4c2d", usd=0.4,
            started="2026-06-10T12:05:00Z", repo="agent-a562bc71051ec4c2d")
    # give the sub a worktree cwd so the kids-based guard fires
    sub_meta = cj_root / "agent-runs" / "2026-06-10" / "sub-sdksub" / "meta.json"
    m = json.loads(sub_meta.read_text())
    m["worktree"] = "/srv/convivy/personal-assistant/.claude/worktrees/agent-a562bc71051ec4c2d"
    sub_meta.write_text(json.dumps(m))
    assert cj.build_sessions(WIN_START, WIN_END) == []


def test_co_session_product_from_mainloop_not_worktree_child(cj_root):
    """A real co session whose captured subagent carries a stale worktree-basename
    product is still labeled with the co's OWN product (from its main-loop cwd), not
    the child's garbage value — the fix for `product == agent-<hex>` phantom labels."""
    uuid = "12ab34cd-5555-6666-7777-888888888888"
    _mk_transcript(cj_root, uuid, [
        ("claude-opus-4-8", {"input_tokens": 2000, "output_tokens": 1000}),
    ], projdir=_PA_DIR, cwd="/repo/personal-assistant")
    _mk_sub(cj_root, "2026-06-10", "gsub", uuid=uuid, usd=0.4,
            started="2026-06-10T12:05:00Z", repo="agent-abc123deadbeef")  # garbage product
    [s] = [x for x in cj.build_sessions(WIN_START, WIN_END) if x["session_id"] == uuid]
    assert s["product"] == "Hollis"


def test_orphaned_scheduled_workers_grouped_into_followup(cj_root):
    """Overnight close-loop / cycle-2 workers that lost their parent link (no
    parent_run, no wave) group into one synthetic per-(product, night) follow-up row
    instead of scattering as standalone top-level sessions — #4b."""
    _mk_run(cj_root, "2026-06-10", "run-cl1", usd=0.4, agent="bug-fixer", parent_run=None,
            origin="scheduled", repo="vivy", started="2026-06-10T09:28:00Z",
            ended="2026-06-10T09:29:00Z")
    _mk_run(cj_root, "2026-06-10", "run-cl2", usd=0.6, agent="reviewer", parent_run=None,
            origin="scheduled", repo="vivy", started="2026-06-10T09:40:00Z",
            ended="2026-06-10T09:41:00Z")
    followups = [s for s in cj.build_sessions(WIN_START, WIN_END)
                 if s["agent"] == "overnight-followup"]
    assert len(followups) == 1
    f = followups[0]
    assert f["product"] == "vivy"
    assert f["worker_count"] == 2
    assert f["total_usd"] == pytest.approx(1.0)
    assert f["session_id"] == "overnight-followup:vivy/2026-06-10"


def test_scheduled_orchestrator_with_children_not_grouped(cj_root):
    """A scheduled run that DID dispatch children is a real orchestrator session, not an
    orphan — it must stay its own agentctl row, not fold into a follow-up (#4b guard)."""
    _mk_run(cj_root, "2026-06-10", "run-orch", usd=1.0, agent="overnight-orchestrator",
            parent_run=None, origin="scheduled", repo="vivy", started="2026-06-10T08:00:00Z",
            ended="2026-06-10T09:00:00Z")
    _mk_run(cj_root, "2026-06-10", "run-w", usd=2.0, parent_run="run-orch",
            origin="scheduled", repo="vivy", started="2026-06-10T08:10:00Z")
    sessions = cj.build_sessions(WIN_START, WIN_END)
    assert [s["agent"] for s in sessions if s["agent"] == "overnight-followup"] == []
    [s] = sessions
    assert s["kind"] == "agentctl" and s["worker_count"] == 1


def test_mainloop_transcript_outside_window_excluded(cj_root):
    """A PA transcript whose turns predate the window is not surfaced (mirrors the
    agentctl/interactive window guards) — PR #56 reviewer gap."""
    uuid = "11112222-3333-4444-5555-666677778888"
    proj = cj_root / "projects" / _PA_DIR
    proj.mkdir(parents=True, exist_ok=True)
    (proj / f"{uuid}.jsonl").write_text(json.dumps({
        "type": "assistant", "timestamp": "2026-05-01T12:00:00.000Z",
        "message": {"model": "claude-sonnet-4-6",
                    "usage": {"input_tokens": 1000, "output_tokens": 200}},
    }))
    assert cj.build_sessions(WIN_START, WIN_END) == []


# --- producer redacts data-local task/label before the shared surface -----------
# Fail-closed after PR #92 broadened the scan: only the engineering allowlist shows
# task/label; every other product (PA, ask, selector, NYH, unknown) is redacted.

def test_producer_redacts_pa_task_and_label(agent_activity_report, monkeypatch):
    """The /activity snapshot MUST NOT carry PA task/label text (personal content),
    but MUST keep cost/agent/model/product. Non-PA rows are unaffected."""
    aar = agent_activity_report
    pa_session = {
        "session_id": "u1", "kind": "interactive", "product": "personal-assistant",
        "agent": "co-session", "label": "secret personal label", "task": "draft email to mom about X",
        "total_usd": 9.0, "worker_count": 1,
        "nodes": [{"product": "personal-assistant", "agent": "pa-coder",
                   "task": "private content here", "label": "leaky", "usd": 9.0}],
    }
    vivy_session = {
        "session_id": "u2", "kind": "agentctl", "product": "vivy",
        "agent": "coder", "label": "recursion-guard", "task": "Fix the recursion guard",
        "total_usd": 3.0, "worker_count": 0, "nodes": [],
    }
    monkeypatch.setattr(aar.cj, "sessions_for_days", lambda days: [pa_session, vivy_session])
    snap = aar.build_snapshot(30)
    pa = next(s for s in snap["sessions"] if s["product"] == "personal-assistant")
    vivy = next(s for s in snap["sessions"] if s["product"] == "vivy")
    # PA: content redacted, cost/agent/product preserved
    assert pa["task"] == aar._REDACTED and pa["label"] == aar._REDACTED
    assert pa["nodes"][0]["task"] == aar._REDACTED and pa["nodes"][0]["label"] is None
    assert pa["total_usd"] == 9.0 and pa["agent"] == "co-session"
    # vivy: untouched (real task text trimmed, not redacted)
    assert vivy["task"] == "Fix the recursion guard" and vivy["label"] == "recursion-guard"


def test_producer_redacts_cross_product_child_node(agent_activity_report, monkeypatch):
    """PR #56 security finding: a PA session's child node tagged with a DIFFERENT
    product must still be redacted — the gate keys on the session, not just the node,
    so a cross-product / mistagged child can't leak personal content."""
    aar = agent_activity_report
    pa_session = {
        "session_id": "u1", "kind": "interactive", "product": "personal-assistant",
        "agent": "co-session", "label": "x", "task": "y", "total_usd": 1.0, "worker_count": 1,
        "nodes": [{"product": "vivy", "agent": "coder",  # mistagged child, NOT personal-assistant
                   "task": "leaked personal content", "label": "leaky", "usd": 1.0}],
    }
    monkeypatch.setattr(aar.cj, "sessions_for_days", lambda days: [pa_session])
    [pa] = aar.build_snapshot(30)["sessions"]
    assert pa["nodes"][0]["task"] == aar._REDACTED and pa["nodes"][0]["label"] is None


def test_producer_redacts_non_pa_data_local_products(agent_activity_report, monkeypatch):
    """Fail-closed redaction (PR #92): a data-local product that is NOT personal-assistant
    — `ask`/`selector`/NYH, now surfaced by the opt-out scan — must also redact task/label,
    and an unknown product redacts by default. Only the engineering allowlist shows text."""
    aar = agent_activity_report
    ask_session = {
        "session_id": "u1", "kind": "interactive", "product": "ask",
        "agent": "co-session", "label": "personal", "task": "what should I say to my landlord",
        "total_usd": 5.0, "worker_count": 0, "nodes": [],
    }
    unknown_session = {
        "session_id": "u2", "kind": "interactive", "product": "some-new-product",
        "agent": "co-session", "label": "l", "task": "t", "total_usd": 1.0,
        "worker_count": 0, "nodes": [],
    }
    monkeypatch.setattr(aar.cj, "sessions_for_days",
                        lambda days: [ask_session, unknown_session])
    snap = aar.build_snapshot(30)
    for s in snap["sessions"]:
        assert s["task"] == aar._REDACTED and s["label"] == aar._REDACTED
        assert s["total_usd"] in (5.0, 1.0) and s["agent"] == "co-session"  # metadata kept


def test_producer_session_allowlist_drops_unknown_fields(agent_activity_report, monkeypatch):
    """PR #56 security hardening: _trim_session is a fail-closed allowlist — a future
    content-bearing session field passes through ONLY if added to _SESSION_KEEP."""
    aar = agent_activity_report
    sess = {
        "session_id": "u1", "kind": "agentctl", "product": "vivy", "agent": "coder",
        "label": "ok", "task": "ok", "total_usd": 1.0, "worker_count": 0, "nodes": [],
        "prompt_preview": "SOME FUTURE FIELD WITH RAW CONTENT",  # not in _SESSION_KEEP
    }
    monkeypatch.setattr(aar.cj, "sessions_for_days", lambda days: [sess])
    [out] = aar.build_snapshot(30)["sessions"]
    assert "prompt_preview" not in out                 # dropped by the allowlist
    assert out["total_usd"] == 1.0 and out["task"] == "ok"  # known fields preserved


# --- the co-session record must never become a corpus node (Deck WS1.2) -------

def test_a_co_session_record_never_enters_the_corpus(cj_root):
    """THE INVARIANT THIS PINS, and the reason it needs pinning NOW.

    WS1.2 writes ~/Convivy/agent-runs/<day>/co-<uuid>/meta.json. Before it, a co-*
    dir carried no meta, so `load_node()` returned None and the corpus was safe by
    accident. It no longer is: the dir HAS a meta, and the only thing keeping it
    out is the name-prefix filter in index_nodes().

    The second assertion is the point. It documents that meta-absence no longer
    protects, so if anyone ever relaxes the prefix filter the FIRST assertion
    fails and names this comment.
    """
    day = cj_root / "agent-runs" / "2026-07-19"
    day.mkdir(parents=True, exist_ok=True)
    co = day / "co-0199c7de-1234-4abc-8def-0123456789ab"
    co.mkdir()
    (co / "meta.json").write_text(json.dumps({
        "kind": "co-session", "session_id": "0199c7de", "started_at": "2026-07-19T10:00:00Z",
        "cwd": "/srv/convivy/substrate", "product": "substrate",
    }), encoding="utf-8")

    nodes = cj.index_nodes(datetime(2026, 7, 18, tzinfo=timezone.utc),
                           datetime(2026, 7, 21, tzinfo=timezone.utc))

    assert not any(k.startswith("co-") for k in nodes), (
        "a co-* dir entered the corpus — the index_nodes prefix filter is what "
        "keeps interactive sessions from being counted twice (once from the "
        "record, once from the transcript). See load_node's docstring.")

    # Meta-absence no longer protects: called directly, load_node DOES build a node.
    assert cj.load_node(co) is not None, (
        "if this now returns None the record shape changed; the assertion above "
        "is no longer testing what it claims to test")


def test_the_quota_fields_ride_through_to_the_row(cj_root):
    """A spend view cannot answer "were we inside the plan" from the dollar
    figures beside it — those are API-rate estimates on subscription usage
    (ARCH-174). These three fields are the answer, so they have to survive the
    join, and `None` has to survive as `None`."""
    d = _mk_run(cj_root, "2026-06-10", "run-cjq1", usd=1.0)
    outcome = json.loads((d / "outcome.json").read_text())
    outcome.update(overage_seen=True, peak_utilization=0.96,
                   rate_limit_status="rejected")
    (d / "outcome.json").write_text(json.dumps(outcome))

    quiet = _mk_run(cj_root, "2026-06-10", "run-cjq2", usd=1.0)

    node = cj.load_node(d)
    assert node["overage_seen"] is True
    assert node["peak_utilization"] == 0.96
    assert node["rate_limit_status"] == "rejected"
    # None, NOT False: "we never heard" is not "we heard, and we were inside".
    assert cj.load_node(quiet)["overage_seen"] is None


# --- an unpriced model is unmeasured, never $0 --------------------------------

_UNKNOWN_USAGE = {"input_tokens": 1000, "output_tokens": 500,
                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}


def test_co_main_loop_node_marks_an_unknown_model_unpriced(cj_root):
    uuid = "aaaaaaaa-2222-3333-4444-555555555555"
    _mk_transcript(cj_root, uuid, [("mystery/model-x", _UNKNOWN_USAGE)])
    node = cj.co_main_loop_node(uuid)
    assert node["usd"] is None
    assert node["unpriced_models"] == ["mystery/model-x"]
    assert node["models"] == {"mystery/model-x": None}


def test_co_main_loop_node_prices_opus_5_5(cj_root):
    """The seen bug: a crew session on Opus 5.5 added $0."""
    uuid = "bbbbbbbb-2222-3333-4444-555555555555"
    _mk_transcript(cj_root, uuid, [("claude-opus-5-5", {
        "input_tokens": 1_000_000, "output_tokens": 100_000,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})])
    node = cj.co_main_loop_node(uuid)
    assert node["usd"] == pytest.approx(6.0)
    assert node["unpriced_models"] == []


def test_a_mixed_session_total_is_a_floor_with_the_unmeasured_named(cj_root):
    uuid = "cccccccc-2222-3333-4444-555555555555"
    _mk_transcript(cj_root, uuid, [
        ("claude-opus-4-8", {"input_tokens": 1000, "output_tokens": 500}),
        ("mystery/model-x", _UNKNOWN_USAGE)])
    node = cj.co_main_loop_node(uuid)
    assert node["usd"] is None and node["priced_usd"] == pytest.approx(0.0175)
    agg = cj._aggregate([node])
    assert agg["total_usd"] == pytest.approx(0.0175)
    assert agg["unpriced_models"] == ["mystery/model-x"]


def test_an_all_unpriced_session_total_is_none_not_zero(cj_root):
    uuid = "dddddddd-2222-3333-4444-555555555555"
    _mk_transcript(cj_root, uuid, [("mystery/model-x", _UNKNOWN_USAGE)])
    [sess] = [s for s in cj.build_sessions(WIN_START, WIN_END) if s["session_id"] == uuid]
    assert sess["total_usd"] is None
    assert sess["unpriced_models"] == ["mystery/model-x"]
    assert sess["models"] == {"mystery/model-x": None}


def test_a_stored_unpriced_subagent_node_reads_unmeasured(cj_root):
    """A sub-* cost.json written by write_cost after an unknown-model turn."""
    d = _mk_sub(cj_root, "2026-06-10", "abc", uuid="u-1", usd=0.0, model="mystery/model-x")
    (d / "cost.json").write_text(json.dumps({
        "usd": 0.0, "unpriced_models": ["mystery/model-x"], "cost_source": "estimate_fallback",
        "by_model": {"mystery/model-x": {"usd": None, "unpriced": True, "tokens_in": 5}}}))
    node = cj.load_node(d)
    assert node["usd"] is None
    assert node["unpriced_models"] == ["mystery/model-x"]
    assert node["models"] == {"mystery/model-x": None}


def test_build_outcomes_never_sums_an_unpriced_chain_as_zero(cj_root):
    _mk_run(cj_root, "2026-06-10", "run-coder", usd=0.0, agent="coder",
            started="2026-06-10T08:10:00Z", pr=512, model="mystery/model-x",
            by_model={"mystery/model-x": {"usd": None, "unpriced": True}})
    cost_path = cj_root / "agent-runs" / "2026-06-10" / "run-coder" / "cost.json"
    cost = json.loads(cost_path.read_text())
    cost["unpriced_models"] = ["mystery/model-x"]
    cost_path.write_text(json.dumps(cost))
    [o] = cj.build_outcomes(WIN_START, WIN_END)
    assert o["node_usd"] is None and o["chain_usd"] is None
    assert o["unpriced_models"] == ["mystery/model-x"]


def _mk_attributed_run(root, date, name, *, agent="coder", repo="vivy",
                       model="claude-sonnet-4-6", billing_source="subscription_credit",
                       usd=1.0, by_model=None, issue=None, pr=None, pr_url=None,
                       started="2026-06-10T08:00:00Z", task=""):
    """A run dir carrying the work-item fields build_item_costs reads: meta.issue,
    meta.pr, meta.repo, meta.billing_source, and pr-link.json for a PR URL."""
    d = root / "agent-runs" / date / name
    d.mkdir(parents=True)
    meta = {"run_id": name, "agent": agent, "model": model, "repo": repo, "task": task,
            "parent_run": None, "profile": "", "started_at": started,
            "billing_source": billing_source}
    if issue is not None:
        meta["issue"] = issue
    if pr is not None:
        meta["pr"] = pr
    (d / "meta.json").write_text(json.dumps(meta))
    (d / "cost.json").write_text(json.dumps(
        {"usd": usd, "by_model": by_model or {model: {"usd": usd}}}))
    (d / "outcome.json").write_text(json.dumps({"exit_status": "completed"}))
    if pr_url:
        (d / "pr-link.json").write_text(json.dumps({"pr_url": pr_url, "pr_number": pr}))
    return d


def _item(item_id, *, product="vivy", pr_url=None, pr_repo=None, pr=None, state="pr-open",
         type="task", parent_id=None):
    """A work item as the Library's list returns it, with only the fields the join reads
    plus the state, title, type and parent_id the per-epic report reads."""
    return {"id": item_id, "product": product, "state": state, "title": item_id,
            "pr_url": pr_url, "pr_repo": pr_repo, "pr": pr,
            "type": type, "parent_id": parent_id}


def _all_placed_run_ids(report):
    """Every run id the report places, each once: in a row or the unattributed bucket."""
    placed = [rid for row in report["items"] for rid in row["run_ids"]]
    return sorted(placed + report["unattributed"]["run_ids"])


def test_load_node_carries_the_item_pr_repo_and_billing_lane(cj_root):
    _mk_attributed_run(cj_root, "2026-06-10", "run-a", issue="issue_A", pr=7,
                       repo="vivy", billing_source="provider_api:openai")
    node = cj.load_node(cj_root / "agent-runs" / "2026-06-10" / "run-a")
    assert node["issue"] == "issue_A"
    assert node["pr"] == 7
    assert node["repo"] == "vivy"
    assert node["billing_source"] == "provider_api:openai"


def test_meta_issue_wins_over_the_item_its_pr_links_to(cj_root):
    """meta.issue names A; the PR the run opened is linked to B. The run joins A, the
    disagreement is counted, and B gets nothing from it."""
    _mk_attributed_run(cj_root, "2026-06-10", "run-a", issue="issue_A", pr=9, usd=2.0,
                       pr_url="https://github.com/convivy/vivy/pull/9")
    items = [_item("issue_A"),
             _item("issue_B", pr_url="https://github.com/convivy/vivy/pull/9")]
    report = cj.build_item_costs(WIN_START, WIN_END, items)
    rows = {r["id"]: r for r in report["items"]}
    assert rows["issue_A"]["run_ids"] == ["run-a"]
    assert rows["issue_A"]["attribution"] == {"meta.issue": 1, "pr": 0}
    assert rows["issue_B"]["run_count"] == 0
    assert report["disagreements"] == 1
    assert report["unattributed"]["count"] == 0


def test_a_pr_only_run_joins_through_its_pr_url(cj_root):
    """No meta.issue. The item's product is not the run's repo here, so a join by
    product would miss; only the PR URL can attribute the run."""
    _mk_attributed_run(cj_root, "2026-06-10", "run-coder", issue=None, pr=5, usd=3.0,
                       pr_url="https://github.com/convivy/vivy/pull/5")
    items = [_item("issue_B", product="constance",
                   pr_url="https://github.com/convivy/vivy/pull/5")]
    report = cj.build_item_costs(WIN_START, WIN_END, items)
    [row] = report["items"]
    assert row["run_ids"] == ["run-coder"]
    assert row["attribution"] == {"meta.issue": 0, "pr": 1}
    assert row["by_billing_source"]["subscription_credit"]["usd"] == 3.0
    assert report["disagreements"] == 0


def test_a_pr_number_joins_through_its_repo_and_never_through_the_product(cj_root):
    """A reviewer has meta.pr but no pr-link.json, so only repo and number can match.
    Item X has the run's product (vivy) but its PR is in constance; item Y has the
    run's repo in pr_repo. The run must join Y, not X."""
    _mk_attributed_run(cj_root, "2026-06-10", "run-reviewer", agent="reviewer",
                       repo="vivy", pr=7, usd=0.5)
    items = [_item("issue_X", product="vivy", pr_repo="convivy/constance", pr=7),
             _item("issue_Y", product="constance", pr_repo="convivy/vivy", pr=7)]
    report = cj.build_item_costs(WIN_START, WIN_END, items)
    rows = {r["id"]: r for r in report["items"]}
    assert rows["issue_Y"]["run_ids"] == ["run-reviewer"]
    assert rows["issue_Y"]["attribution"]["pr"] == 1
    assert rows["issue_X"]["run_count"] == 0
    assert report["unattributed"]["count"] == 0


def test_a_supervisor_run_stays_unattributed_and_is_never_split(cj_root):
    """The orchestrator served both items; its own $10 is not divided between them.
    Each child joins its own item at its own cost."""
    _mk_attributed_run(cj_root, "2026-06-10", "run-orch", agent="planning-orchestrator",
                       usd=10.0)
    for child, issue, usd in (("run-a", "issue_A", 2.0), ("run-b", "issue_B", 3.0)):
        d = _mk_attributed_run(cj_root, "2026-06-10", child, issue=issue, usd=usd)
        meta = json.loads((d / "meta.json").read_text())
        meta["parent_run"] = "run-orch"
        (d / "meta.json").write_text(json.dumps(meta))
    items = [_item("issue_A"), _item("issue_B")]
    report = cj.build_item_costs(WIN_START, WIN_END, items)
    rows = {r["id"]: r for r in report["items"]}
    assert rows["issue_A"]["by_billing_source"]["subscription_credit"]["usd"] == 2.0
    assert rows["issue_B"]["by_billing_source"]["subscription_credit"]["usd"] == 3.0
    assert report["unattributed"]["run_ids"] == ["run-orch"]
    assert report["unattributed"]["by_reason"] == {"multi-item": 1}
    assert report["unattributed"]["by_billing_source"]["subscription_credit"]["usd"] == 10.0


def test_subscription_credit_and_provider_api_on_one_item_are_separate_lanes(cj_root):
    _mk_attributed_run(cj_root, "2026-06-10", "run-sub", issue="issue_A", usd=2.0,
                       billing_source="subscription_credit")
    _mk_attributed_run(cj_root, "2026-06-11", "run-api", issue="issue_A", usd=3.0,
                       billing_source="provider_api:openai", started="2026-06-11T08:00:00Z")
    [row] = cj.build_item_costs(WIN_START, WIN_END, [_item("issue_A")])["items"]
    lanes = row["by_billing_source"]
    assert set(lanes) == {"subscription_credit", "provider_api:openai"}
    assert lanes["subscription_credit"]["usd"] == 2.0
    assert lanes["provider_api:openai"]["usd"] == 3.0
    # No combined figure at the row or in any lane.
    assert "usd" not in row and "total_usd" not in row
    assert 5.0 not in [v for v in row.values() if isinstance(v, float)]


def test_an_unpriced_model_gives_none_and_its_name_never_zero(cj_root):
    _mk_attributed_run(cj_root, "2026-06-10", "run-x", issue="issue_A", usd=0.0,
                       model="mystery/model-x",
                       by_model={"mystery/model-x": {"usd": None, "unpriced": True}})
    _mk_attributed_run(cj_root, "2026-06-11", "run-y", issue="issue_A", usd=1.0,
                       started="2026-06-11T08:00:00Z")
    [row] = cj.build_item_costs(WIN_START, WIN_END, [_item("issue_A")])["items"]
    lane = row["by_billing_source"]["subscription_credit"]
    assert lane["usd"] is None
    assert lane["unpriced_models"] == ["mystery/model-x"]
    assert lane["by_model"]["mystery/model-x"] is None


def test_every_run_sits_in_one_row_or_the_unattributed_bucket(cj_root):
    """Conservation. run-brief's task text names issue_A, but its meta records no item,
    so the run must land unattributed with its cost; the task text is never read. The
    sub-* run is the subagent exclusion. run-old sits in a June dir but started in
    May, so the start-time window, not the directory, is what excludes it."""
    _mk_attributed_run(cj_root, "2026-06-10", "run-a", issue="issue_A", usd=1.0)
    _mk_attributed_run(cj_root, "2026-06-10", "run-pr", pr=5, usd=2.0,
                       pr_url="https://github.com/convivy/vivy/pull/5")
    _mk_attributed_run(cj_root, "2026-06-10", "run-brief", usd=4.0,
                       task="Fix issue_A: the flag parser")
    _mk_sub(cj_root, "2026-06-10", "abc", uuid="co-uuid", usd=0.25,
            started="2026-06-10T08:00:00Z")
    _mk_attributed_run(cj_root, "2026-06-10", "run-old", issue="issue_A", usd=9.0,
                       started="2026-05-31T23:00:00Z")
    items = [_item("issue_A"), _item("issue_B", pr_url="https://github.com/convivy/vivy/pull/5")]
    report = cj.build_item_costs(WIN_START, WIN_END, items)
    assert report["run_count"] == 4
    assert _all_placed_run_ids(report) == sorted(["run-a", "run-pr", "run-brief", "sub-abc"])
    assert report["unattributed"]["by_reason"] == {"subagent": 1, "no-link": 1}
    assert report["unattributed"]["by_billing_source"]["subscription_credit"]["usd"] == 4.0


def test_a_pr_two_items_link_to_is_unattributed_not_split(cj_root):
    _mk_attributed_run(cj_root, "2026-06-10", "run-pr", pr=5, usd=2.0,
                       pr_url="https://github.com/convivy/vivy/pull/5")
    items = [_item("issue_A", pr_url="https://github.com/convivy/vivy/pull/5"),
             _item("issue_B", pr_url="https://github.com/convivy/vivy/pull/5")]
    report = cj.build_item_costs(WIN_START, WIN_END, items)
    assert all(row["run_count"] == 0 for row in report["items"])
    assert report["unattributed"]["by_reason"] == {"pr-ambiguous": 1}


@pytest.mark.parametrize("pr_repo", ["someone/vivy", "convivyx/vivy", "vivy"])
def test_an_item_linking_a_repo_outside_convivy_joins_nothing(cj_root, pr_repo):
    """POST /items stores a filer's pr_repo unchanged, so the join indexes only convivy's
    repos. The run's repo name and PR number match the item's, and the run still joins
    nothing: the owner is not convivy (or there is no owner to check)."""
    _mk_attributed_run(cj_root, "2026-06-10", "run-pr", repo="vivy", pr=7, usd=2.0)
    report = cj.build_item_costs(WIN_START, WIN_END, [_item("issue_X", pr_repo=pr_repo, pr=7)])
    assert report["items"][0]["run_count"] == 0
    assert report["unattributed"]["by_reason"] == {"no-link": 1}


@pytest.mark.parametrize("pr_url", [
    "https://github.com/someone/vivy/pull/5",
    "https://github.com/convivyx/vivy/pull/5",
    "http://github.com/convivy/vivy/pull/5",
    "https://github.com/convivy/vivy/issues/5",
])
def test_an_item_linking_a_url_that_is_not_a_convivy_pull_joins_nothing(cj_root, pr_url):
    """The run's own PR link is the same URL as the item's, so only the item's pr_url could
    join it. A URL that is not https://github.com/convivy/<repo>/pull/<n> is not indexed."""
    _mk_attributed_run(cj_root, "2026-06-10", "run-pr", repo="vivy", pr=5, usd=2.0,
                       pr_url=pr_url)
    report = cj.build_item_costs(WIN_START, WIN_END, [_item("issue_B", pr_url=pr_url)])
    assert report["items"][0]["run_count"] == 0
    assert report["unattributed"]["by_reason"] == {"no-link": 1}


def test_a_meta_issue_missing_from_the_list_stays_unattributed_and_disagrees(cj_root):
    """meta.issue names an item the list does not hold, and the PR links to A. There is
    no row to join, so the run stays unattributed rather than falling through to A; the
    disagreement is still counted."""
    _mk_attributed_run(cj_root, "2026-06-10", "run-a", issue="issue_GONE", pr=3, usd=1.0,
                       pr_url="https://github.com/convivy/vivy/pull/3")
    items = [_item("issue_A", pr_url="https://github.com/convivy/vivy/pull/3")]
    report = cj.build_item_costs(WIN_START, WIN_END, items)
    assert report["items"][0]["run_count"] == 0
    assert report["unattributed"]["by_reason"] == {"issue-not-listed": 1}
    assert report["disagreements"] == 1


@pytest.mark.parametrize("damage", ["missing", "unparseable"])
def test_a_run_with_no_cost_record_is_unmeasured_never_zero(cj_root, damage):
    """A run still in flight has no cost.json, and a write cut off part-way leaves one
    that does not parse. Neither is a measurement, so the run's usd is None and it is
    counted as cost-missing; it does not add $0 to its item's lane."""
    live = _mk_attributed_run(cj_root, "2026-06-10", "run-live", issue="issue_A", usd=2.0)
    if damage == "missing":
        (live / "cost.json").unlink()
    else:
        (live / "cost.json").write_text("{not json")
    _mk_attributed_run(cj_root, "2026-06-11", "run-done", issue="issue_A", usd=1.0,
                       started="2026-06-11T08:00:00Z")
    node = cj.load_node(live)
    assert node["usd"] is None
    assert node["cost_missing"] is True
    [row] = cj.build_item_costs(WIN_START, WIN_END, [_item("issue_A")])["items"]
    lane = row["by_billing_source"]["subscription_credit"]
    assert lane["usd"] is None
    assert lane["cost_missing_runs"] == 1
    assert lane["runs"] == 2


@pytest.mark.parametrize("damage", [
    "not-utf8", "array-root", "null-root", "negative-usd", "nan-usd", "infinite-usd",
    "true-usd", "false-usd", "no-usd-key", "null-usd"])
def test_a_cost_record_that_is_not_a_measurement_is_unmeasured_never_summed(cj_root, damage):
    """The run writes its own cost.json, so one can hold bytes that are not UTF-8, a root
    that is not an object, or a usd that is absent, null, negative, not finite or a JSON
    boolean (json.loads reads NaN and Infinity, and True sums as 1). Each is unmeasured: it
    does not crash the report and adds nothing to its lane. An absent usd is not a $0
    measurement, even when by_model prices the run. The report still counts the run."""
    live = _mk_attributed_run(cj_root, "2026-06-10", "run-live", issue="issue_A", usd=2.0)
    cost_path = live / "cost.json"
    if damage == "not-utf8":
        cost_path.write_bytes(b'{"usd": 2.0, "note": "\xff\xfe"}')
    elif damage == "array-root":
        cost_path.write_text("[]")
    elif damage == "null-root":
        cost_path.write_text("null")
    elif damage == "negative-usd":
        cost_path.write_text('{"usd": -50.0, "by_model": {}}')
    elif damage == "nan-usd":
        cost_path.write_text('{"usd": NaN, "by_model": {}}')
    elif damage == "true-usd":
        cost_path.write_text('{"usd": true, "by_model": {}}')
    elif damage == "false-usd":
        cost_path.write_text('{"usd": false, "by_model": {}}')
    elif damage == "no-usd-key":
        cost_path.write_text('{"by_model": {"claude-sonnet-5": {"usd": 3.0}}}')
    elif damage == "null-usd":
        cost_path.write_text('{"usd": null, "by_model": {"claude-sonnet-5": {"usd": 3.0}}}')
    else:
        cost_path.write_text('{"usd": Infinity, "by_model": {}}')
    _mk_attributed_run(cj_root, "2026-06-11", "run-done", issue="issue_A", usd=1.0,
                       started="2026-06-11T08:00:00Z")
    report = cj.build_item_costs(WIN_START, WIN_END, [_item("issue_A")])
    [row] = report["items"]
    assert row["run_count"] == 2
    lane = row["by_billing_source"]["subscription_credit"]
    assert lane["usd"] is None
    assert lane["cost_missing_runs"] == 1


@pytest.mark.parametrize("damage", ["no-usd-key", "null-usd", "negative-usd"])
def test_an_unusable_cost_record_with_no_exit_status_reads_running(cj_root, damage):
    """An unusable cost.json is reset to {} before exit_status is read, so a run whose outcome
    has no exit_status reads "running", the same as a run still in flight. A measured record
    with no exit_status reads "unknown" instead."""
    live = _mk_attributed_run(cj_root, "2026-06-10", "run-live", issue="issue_A", usd=2.0)
    (live / "outcome.json").write_text("{}")
    (live / "cost.json").write_text({
        "no-usd-key": '{"by_model": {"claude-sonnet-5": {"usd": 3.0}}}',
        "null-usd": '{"usd": null, "by_model": {"claude-sonnet-5": {"usd": 3.0}}}',
        "negative-usd": '{"usd": -50.0, "by_model": {}}',
    }[damage])
    assert cj.load_node(live)["exit_status"] == "running"


def test_a_chain_with_a_cost_missing_run_reads_unmeasured_not_zero(cj_root):
    """A single run with no cost record has a null node_usd, so its chain total is null
    too. A chain total of 0.0 beside a null node reads as a measured zero."""
    run = _mk_run(cj_root, "2026-06-10", "run-coder", usd=0.0, agent="coder",
                  started="2026-06-10T08:10:00Z", pr=512)
    (run / "cost.json").unlink()
    [o] = cj.build_outcomes(WIN_START, WIN_END)
    assert o["node_usd"] is None
    assert o["chain_usd"] is None
    assert o["chain_cost_missing"] is True


def test_a_chain_with_a_cost_missing_child_is_a_floor_that_says_so(cj_root):
    _mk_run(cj_root, "2026-06-10", "run-coder", usd=4.0, agent="coder",
            started="2026-06-10T08:10:00Z", pr=512)
    child = _mk_run(cj_root, "2026-06-10", "run-child", usd=1.0, parent_run="run-coder",
                    started="2026-06-10T08:20:00Z")
    (child / "cost.json").unlink()
    [o] = cj.build_outcomes(WIN_START, WIN_END)
    assert o["chain_usd"] == 4.0
    assert o["chain_cost_missing"] is True


def test_a_run_whose_start_does_not_parse_is_unattributed_as_no_start(cj_root):
    """An unreadable started_at leaves no time to window the run by, so it is filed under
    no-start rather than dropped: the run sits in a row or in unattributed."""
    _mk_attributed_run(cj_root, "2026-06-10", "run-a", issue="issue_A", usd=1.0,
                       started="not-a-timestamp")
    report = cj.build_item_costs(WIN_START, WIN_END, [_item("issue_A")])
    assert report["run_count"] == 1
    assert report["items"][0]["run_count"] == 0
    assert report["unattributed"]["by_reason"] == {"no-start": 1}
    assert _all_placed_run_ids(report) == ["run-a"]
    assert report["unattributed"]["by_billing_source"]["subscription_credit"]["usd"] == 1.0


def test_a_subagent_stays_unattributed_when_its_parent_run_holds_an_item(cj_root):
    """The SubagentStop hook stamps parent_run with the Claude session UUID
    (tools/subagent-telemetry-emit.py). That UUID is also meta.co_session on the parentless
    runs its session dispatched, and one session can dispatch runs on several items. So a
    match names a session, not an item, and the sub-* stays unattributed even though its
    worker is attributed. No sub-* join is built."""
    _mk_attributed_run(cj_root, "2026-06-10", "run-worker", issue="issue_A", usd=1.0)
    _mk_sub(cj_root, "2026-06-10", "task-1", uuid="worker-session-uuid", usd=0.5,
            started="2026-06-10T08:00:00Z")
    report = cj.build_item_costs(WIN_START, WIN_END, [_item("issue_A")])
    [row] = report["items"]
    assert row["run_ids"] == ["run-worker"]
    assert report["unattributed"]["run_ids"] == ["sub-task-1"]
    assert report["unattributed"]["by_reason"] == {"subagent": 1}


def test_producer_carries_the_unmeasured_marker(agent_activity_report, monkeypatch):
    aar = agent_activity_report
    sess = {"session_id": "u1", "kind": "interactive", "product": "vivy", "agent": "co-session",
            "total_usd": None, "unpriced_models": ["mystery/model-x"], "worker_count": 0,
            "nodes": [{"run_id": "co-u1", "kind": "co", "depth": 0, "usd": None,
                       "unpriced_models": ["mystery/model-x"]}]}
    monkeypatch.setattr(aar.cj, "sessions_for_days", lambda days: [sess])
    [out] = aar.build_snapshot(30)["sessions"]
    assert out["total_usd"] is None and out["unpriced_models"] == ["mystery/model-x"]
    assert out["nodes"][0]["usd"] is None and out["nodes"][0]["unpriced_models"] == ["mystery/model-x"]


# --- cost by epic and by Course (E16·2) --------------------------------------


def _course(entries, *, reached=(), in_flight=(), remaining=(), resolved_items=()):
    """A Course dict as issue_client.get_course returns it, with only the fields
    build_course_costs reads: the ordered entry ids, their resolved rows, and
    the Course's own `position`."""
    missing = len(entries) - len(reached) - len(in_flight) - len(remaining)
    return {
        "id": "course_1", "product": "vivy", "entries": list(entries),
        "items": list(resolved_items),
        "position": {
            "entries": len(entries), "reached": len(reached), "reached_ids": list(reached),
            "in_flight": len(in_flight), "in_flight_ids": list(in_flight),
            "remaining": len(remaining), "remaining_ids": list(remaining),
            "missing": missing,
        },
    }


def test_epic_for_item_counts_an_epic_as_itself():
    assert cj._epic_for_item(
        {"id": "issue_E", "type": "epic", "parent_id": None}, set()) == "issue_E"


def test_epic_for_item_counts_an_epic_as_itself_even_with_a_parent():
    """The Library's rule checks type before parent_id — a sub-epic under another
    epic still counts its own spend against itself, not its parent."""
    assert cj._epic_for_item(
        {"id": "issue_E", "type": "epic", "parent_id": "issue_Grand"}, set()) == "issue_E"


def test_epic_for_item_counts_a_child_under_its_immediate_parent():
    assert cj._epic_for_item(
        {"id": "issue_C", "type": "task", "parent_id": "issue_E"}, set()) == "issue_E"


def test_epic_for_item_counts_a_parentless_non_epic_under_corrections():
    assert cj._epic_for_item(
        {"id": "issue_X", "type": "bug", "parent_id": None}, set()) == cj.CORRECTIONS


def test_epic_for_item_counts_a_queue_parented_non_epic_under_corrections():
    """One-home-per-work-item (library/specs/one-home-per-work-item): once the
    Library backfills, a correction's parent_id names its product's corrections
    queue instead of being null. It must bucket exactly like a null parent."""
    assert cj._epic_for_item(
        {"id": "issue_X", "type": "bug", "parent_id": "cq_01ABC"},
        {"cq_01ABC"}) == cj.CORRECTIONS


def test_epic_for_item_never_reads_the_cq_prefix_itself():
    """The plan rules the `cq_` prefix out as a way to tell a queue from an
    epic: only a hit in the looked-up queue_ids counts. An id that merely
    LOOKS like a queue, but isn't in the set this run resolved, is still read
    as an (unlisted) epic parent."""
    assert cj._epic_for_item(
        {"id": "issue_X", "type": "bug", "parent_id": "cq_not_a_real_queue"},
        {"cq_01ABC"}) == "cq_not_a_real_queue"


def test_a_two_lane_epic_gives_two_lane_rows_and_no_aggregate_key(cj_root):
    _mk_attributed_run(cj_root, "2026-06-10", "run-sub", issue="issue_C", usd=2.0,
                       billing_source="subscription_credit")
    _mk_attributed_run(cj_root, "2026-06-11", "run-api", issue="issue_C", usd=3.0,
                       billing_source="provider_api:openai", started="2026-06-11T08:00:00Z")
    items = [_item("issue_E", type="epic", parent_id=None),
             _item("issue_C", type="task", parent_id="issue_E")]
    course = _course(["issue_E"], remaining=["issue_E"],
                     resolved_items=[{"id": "issue_E", "title": "Epic E", "state": "open"}])
    report = cj.build_course_costs(WIN_START, WIN_END, items, course, {"issue_E": None}, set())
    [epic] = report["epics"]
    assert set(epic["lanes"]) == {"subscription_credit", "provider_api:openai"}
    assert epic["lanes"]["subscription_credit"]["usd"] == 2.0
    assert epic["lanes"]["provider_api:openai"]["usd"] == 3.0
    assert "usd" not in epic and "total_usd" not in epic
    assert epic["position"] == "remaining"
    assert epic["run_count"] == 2
    # Course totals carry the same two lanes, never one combined figure.
    assert set(report["totals"]) == {"subscription_credit", "provider_api:openai"}
    assert report["cannot_be_measured"] is False


def test_an_unattributed_run_stays_out_of_every_epic_and_sits_in_its_own_total(cj_root):
    _mk_attributed_run(cj_root, "2026-06-10", "run-c", issue="issue_C", usd=1.0)
    _mk_attributed_run(cj_root, "2026-06-10", "run-loose", usd=5.0, task="no item named here")
    items = [_item("issue_E", type="epic", parent_id=None),
             _item("issue_C", type="task", parent_id="issue_E")]
    course = _course(["issue_E"], remaining=["issue_E"],
                     resolved_items=[{"id": "issue_E", "title": "Epic E", "state": "open"}])
    report = cj.build_course_costs(WIN_START, WIN_END, items, course, {"issue_E": None}, set())
    [epic] = report["epics"]
    assert epic["run_count"] == 1
    assert "run-loose" not in str(epic["lanes"])
    assert report["unattributed"]["run_ids"] == ["run-loose"]
    assert report["unattributed"]["by_billing_source"]["subscription_credit"]["usd"] == 5.0
    assert report["cannot_be_measured"] is False


def test_a_parentless_non_epic_lands_under_corrections_not_an_epic(cj_root):
    _mk_attributed_run(cj_root, "2026-06-10", "run-x", issue="issue_X", usd=4.0)
    items = [_item("issue_E", type="epic", parent_id=None),
             _item("issue_X", type="bug", parent_id=None)]
    course = _course(["issue_E"], remaining=["issue_E"],
                     resolved_items=[{"id": "issue_E", "title": "Epic E", "state": "open"}])
    report = cj.build_course_costs(WIN_START, WIN_END, items, course, {"issue_E": None}, set())
    [epic] = report["epics"]
    assert epic["run_count"] == 0
    assert report["corrections"]["item_ids"] == ["issue_X"]
    assert report["corrections"]["run_count"] == 1
    assert report["corrections"]["lanes"]["subscription_credit"]["usd"] == 4.0
    # Nothing under corrections counts toward whether the Course can be measured.
    assert report["cannot_be_measured"] is True


def test_a_queue_parented_non_epic_lands_under_corrections_not_an_epic(cj_root):
    """Once the Library backfills (one-home-per-work-item), a correction's
    parent_id names its product's corrections queue rather than being null.
    Passing that id's set in as queue_ids must bucket it exactly like the
    null-parent case above, not drop it under a bare `cq_…` epic key."""
    _mk_attributed_run(cj_root, "2026-06-10", "run-x", issue="issue_X", usd=4.0)
    items = [_item("issue_E", type="epic", parent_id=None),
             _item("issue_X", type="bug", parent_id="cq_01ABC")]
    course = _course(["issue_E"], remaining=["issue_E"],
                     resolved_items=[{"id": "issue_E", "title": "Epic E", "state": "open"}])
    report = cj.build_course_costs(WIN_START, WIN_END, items, course, {"issue_E": None},
                                   {"cq_01ABC"})
    [epic] = report["epics"]
    assert epic["run_count"] == 0
    assert report["corrections"]["item_ids"] == ["issue_X"]
    assert report["corrections"]["run_count"] == 1
    assert report["corrections"]["lanes"]["subscription_credit"]["usd"] == 4.0
    assert report["corrections"]["cannot_be_measured"] is False
    assert report["corrections"]["reason"] is None


def test_a_failed_queue_lookup_reports_corrections_cannot_be_measured(cj_root):
    """`queue_ids=None` is what a caller passes when the Library's corrections-
    queue lookup failed for a reason other than "the route doesn't exist yet"
    (a 5xx, a parse failure). The spend must not quietly land under an epic
    (or vanish): the corrections bucket itself reports the gap."""
    _mk_attributed_run(cj_root, "2026-06-10", "run-x", issue="issue_X", usd=4.0)
    items = [_item("issue_E", type="epic", parent_id=None),
             _item("issue_X", type="bug", parent_id="cq_01ABC")]
    course = _course(["issue_E"], remaining=["issue_E"],
                     resolved_items=[{"id": "issue_E", "title": "Epic E", "state": "open"}])
    report = cj.build_course_costs(WIN_START, WIN_END, items, course, {"issue_E": None}, None)
    [epic] = report["epics"]
    # issue_X's unresolved parent is never mistaken for issue_E's epic total.
    assert epic["run_count"] == 0
    assert report["corrections"]["cannot_be_measured"] is True
    assert report["corrections"]["reason"]
    assert report["corrections"]["item_ids"] == []
    assert report["corrections"]["run_count"] == 0


def test_a_course_with_no_attributed_runs_cannot_be_measured_distinct_from_zero(cj_root):
    """No runs at all: cannot_be_measured, with a reason, and no lane claiming $0."""
    items = [_item("issue_E", type="epic", parent_id=None)]
    course = _course(["issue_E"], remaining=["issue_E"],
                     resolved_items=[{"id": "issue_E", "title": "Epic E", "state": "open"}])
    report = cj.build_course_costs(WIN_START, WIN_END, items, course, {"issue_E": None}, set())
    assert report["cannot_be_measured"] is True
    assert report["reason"]
    [epic] = report["epics"]
    assert epic["lanes"] == {}
    assert report["totals"] == {}


def test_a_measured_free_run_reads_zero_not_cannot_be_measured(cj_root):
    """A run that is genuinely $0 (priced, just free) is a measured zero, the
    opposite of cannot_be_measured — the two must never read the same."""
    _mk_attributed_run(cj_root, "2026-06-10", "run-free", issue="issue_C", usd=0.0)
    items = [_item("issue_E", type="epic", parent_id=None),
             _item("issue_C", type="task", parent_id="issue_E")]
    course = _course(["issue_E"], remaining=["issue_E"],
                     resolved_items=[{"id": "issue_E", "title": "Epic E", "state": "open"}])
    report = cj.build_course_costs(WIN_START, WIN_END, items, course, {"issue_E": None}, set())
    assert report["cannot_be_measured"] is False
    [epic] = report["epics"]
    assert epic["lanes"]["subscription_credit"]["usd"] == 0.0


def test_the_epics_position_reflects_the_courses_own_position(cj_root):
    _mk_attributed_run(cj_root, "2026-06-10", "run-a", issue="issue_A", usd=1.0)
    _mk_attributed_run(cj_root, "2026-06-10", "run-b", issue="issue_B", usd=1.0)
    items = [_item("issue_A", type="epic", parent_id=None),
             _item("issue_B", type="epic", parent_id=None)]
    course = _course(["issue_A", "issue_B"], reached=["issue_A"], in_flight=["issue_B"],
                     resolved_items=[{"id": "issue_A", "title": "A", "state": "fixed"},
                                     {"id": "issue_B", "title": "B", "state": "claimed"}])
    report = cj.build_course_costs(WIN_START, WIN_END, items, course,
                                   {"issue_A": None, "issue_B": None}, set())
    positions = {e["id"]: e["position"] for e in report["epics"]}
    assert positions == {"issue_A": "reached", "issue_B": "in_flight"}


def test_the_epics_progress_comes_from_the_passed_in_mapping(cj_root):
    _mk_attributed_run(cj_root, "2026-06-10", "run-a", issue="issue_A", usd=1.0)
    items = [_item("issue_A", type="epic", parent_id=None)]
    course = _course(["issue_A"], remaining=["issue_A"],
                     resolved_items=[{"id": "issue_A", "title": "A", "state": "open"}])
    progress = {"children": 3, "percent_terminal": 0.5}
    report = cj.build_course_costs(WIN_START, WIN_END, items, course, {"issue_A": progress}, set())
    [epic] = report["epics"]
    assert epic["progress"] == progress


# --- ledger reconciliation (E16·5) -------------------------------------------


def test_build_epic_run_costs_groups_runs_by_epic_at_run_granularity(cj_root):
    _mk_attributed_run(cj_root, "2026-06-10", "run-a", issue="issue_C", usd=2.0,
                       billing_source="provider_api:openai")
    items = [_item("issue_E", type="epic", parent_id=None),
             _item("issue_C", type="task", parent_id="issue_E")]
    costs = cj.build_epic_run_costs(WIN_START, WIN_END, items, set())
    assert costs == {"issue_E": {"run-a": {"priced_usd": 2.0,
                                           "billing_source": "provider_api:openai"}}}


def test_build_epic_run_costs_uses_priced_usd_not_usd_for_an_unpriced_model(cj_root):
    """model-scorecard's `cost_usd` and this module's per-run `usd` are not the
    same figure for a run with an unpriced model: `usd` reads None (unmeasured),
    while both share the raw cost.json figure as `priced_usd` — the number a
    ledger entry (posted from that same raw figure) must be compared against."""
    _mk_attributed_run(cj_root, "2026-06-10", "run-a", issue="issue_C", usd=3.5,
                       by_model={"mystery/model-x": {"usd": 3.5, "unpriced": True}})
    node = cj.load_node(cj_root / "agent-runs" / "2026-06-10" / "run-a")
    assert node["usd"] is None  # unmeasured, because of the unpriced model
    items = [_item("issue_E", type="epic", parent_id=None),
             _item("issue_C", type="task", parent_id="issue_E")]
    costs = cj.build_epic_run_costs(WIN_START, WIN_END, items, set())
    assert costs["issue_E"]["run-a"]["priced_usd"] == 3.5  # the same raw figure either way


def _ledger_entry(run_id, usd, *, lane=None, voided_at=None):
    return {"run_id": run_id, "usd": usd, "lane": lane, "voided_at": voided_at}


def test_reconcile_epic_cost_matches_when_the_figures_agree():
    run_costs = {"run-a": {"priced_usd": 2.0, "billing_source": "provider_api:openai"}}
    entries = [_ledger_entry("run-a", 2.0, lane="gateway")]
    result = cj.reconcile_epic_cost(run_costs, entries, {"run-a": "issue_E"}, "issue_E")
    assert result["ok"] is True
    assert result["mismatches"] == []
    assert result["run_total_by_lane"] == {"provider_api:openai": 2.0}
    assert result["ledger_total_by_lane"] == {"gateway": 2.0}


def test_reconcile_epic_cost_catches_a_planted_amount_divergence():
    """The core E16·5 requirement: the same run_id, a different ledger amount
    from the run record, caught as a mismatch and failing the `ok` flag."""
    run_costs = {"run-a": {"priced_usd": 2.0, "billing_source": "provider_api:openai"}}
    entries = [_ledger_entry("run-a", 9.99, lane="gateway")]
    result = cj.reconcile_epic_cost(run_costs, entries, {"run-a": "issue_E"}, "issue_E")
    assert result["ok"] is False
    assert result["mismatches"] == [{"run_id": "run-a", "kind": "amount",
                                     "run_usd": 2.0, "ledger_usd": 9.99}]


def test_reconcile_epic_cost_catches_a_run_filed_under_a_different_epic():
    """cost_join attributes run-a to issue_E1; the ledger's own epic_id for it
    (reflected here by which epic's filtered fetch returned it) is issue_E2."""
    run_epic_map = {"run-a": "issue_E1"}
    entries = [_ledger_entry("run-a", 2.0, lane="gateway")]
    result = cj.reconcile_epic_cost({}, entries, run_epic_map, "issue_E2")
    assert result["ok"] is False
    assert result["mismatches"] == [{"run_id": "run-a", "kind": "epic",
                                     "cost_join_epic_id": "issue_E1",
                                     "ledger_epic_id": "issue_E2"}]


def test_reconcile_epic_cost_a_run_absent_from_the_ledger_is_not_a_mismatch():
    """A run posted before E16·5, or to a product with no live ledger, is
    reported as run_only rather than failing the reconciliation."""
    run_costs = {"run-a": {"priced_usd": 2.0, "billing_source": "provider_api:openai"}}
    result = cj.reconcile_epic_cost(run_costs, [], {"run-a": "issue_E"}, "issue_E")
    assert result["ok"] is True
    assert result["run_only"] == {"count": 1, "usd": 2.0, "run_ids": ["run-a"]}


def test_reconcile_epic_cost_a_run_only_in_the_ledger_is_not_a_mismatch_either():
    """A run the ledger holds for this epic that cost_join did not attribute to
    ANY of this Course's epics (e.g. ledger history outside --days, or a run
    `run_epic_map` has no entry for) is reported, not flagged — only a
    `run_epic_map` entry naming a DIFFERENT epic is a mismatch."""
    entries = [_ledger_entry("run-old", 1.5, lane="gateway")]
    result = cj.reconcile_epic_cost({}, entries, {}, "issue_E")
    assert result["ok"] is True
    assert result["ledger_only"] == {"count": 1, "usd": 1.5, "run_ids": ["run-old"]}


def test_reconcile_epic_cost_ignores_a_voided_entry():
    run_costs = {"run-a": {"priced_usd": 2.0, "billing_source": "provider_api:openai"}}
    entries = [_ledger_entry("run-a", 9.99, lane="gateway", voided_at="2026-10-01T00:00:00Z")]
    result = cj.reconcile_epic_cost(run_costs, entries, {"run-a": "issue_E"}, "issue_E")
    # a voided entry is dropped before anything else, so run-a reads as absent
    # from the ledger — not as a $9.99-vs-$2.00 mismatch.
    assert result["ok"] is True
    assert result["run_only"] == {"count": 1, "usd": 2.0, "run_ids": ["run-a"]}
    assert result["ledger_only"] == {"count": 0, "usd": 0.0, "run_ids": []}


def test_reconcile_epic_cost_never_sums_across_lanes():
    run_costs = {
        "run-sub": {"priced_usd": 2.0, "billing_source": "subscription_credit"},
        "run-api": {"priced_usd": 3.0, "billing_source": "provider_api:openai"},
    }
    entries = [_ledger_entry("run-sub", 2.0, lane="subscription"),
              _ledger_entry("run-api", 3.0, lane="gateway")]
    run_epic_map = {"run-sub": "issue_E", "run-api": "issue_E"}
    result = cj.reconcile_epic_cost(run_costs, entries, run_epic_map, "issue_E")
    assert result["ok"] is True
    assert result["run_total_by_lane"] == {"subscription_credit": 2.0,
                                           "provider_api:openai": 3.0}
    assert result["ledger_total_by_lane"] == {"subscription": 2.0, "gateway": 3.0}


# --- cost per outcome: agent x model x work-item class (E16·3) ---------------


def test_outcome_table_separates_agent_model_and_class(cj_root):
    """Two agents, two models, one item planned under an epic and one a
    correction with no parent: each lands in its own (agent, model, class)
    row, keyed on WHERE the item lives, never its (retiring) `type` field."""
    _mk_attributed_run(cj_root, "2026-06-10", "run-fix", agent="coder",
                       model="claude-sonnet-4-6", issue="issue_FIX", usd=2.0,
                       billing_source="subscription_credit")
    _mk_attributed_run(cj_root, "2026-06-11", "run-wont", agent="reviewer",
                       model="claude-opus-5-5", issue="issue_WONT", usd=5.0,
                       billing_source="provider_api:openai", started="2026-06-11T08:00:00Z")
    items = [_item("issue_EPIC1", type="epic", parent_id=None),
             _item("issue_FIX", type="task", state="fixed", parent_id="issue_EPIC1"),
             _item("issue_WONT", type="task", state="wontfix", parent_id=None)]
    table = cj.build_outcome_table(WIN_START, WIN_END, items, set())
    keys = {(r["agent"], r["model"], r["class"]) for r in table["rows"]}
    assert keys == {("coder", "claude-sonnet-4-6", "epic"),
                    ("reviewer", "claude-opus-5-5", cj.CORRECTIONS)}


def test_outcome_table_keeps_fixed_and_abandoned_apart(cj_root):
    _mk_attributed_run(cj_root, "2026-06-10", "run-fix", agent="coder", issue="issue_FIX",
                       usd=2.0, billing_source="subscription_credit")
    _mk_attributed_run(cj_root, "2026-06-11", "run-wont", agent="reviewer",
                       model="claude-opus-5-5", issue="issue_WONT", usd=5.0,
                       billing_source="provider_api:openai", started="2026-06-11T08:00:00Z")
    items = [_item("issue_EPIC1", type="epic", parent_id=None),
             _item("issue_FIX", type="task", state="fixed", parent_id="issue_EPIC1"),
             _item("issue_WONT", type="task", state="wontfix", parent_id=None)]
    rows = {(r["agent"], r["class"]): r for r in
           cj.build_outcome_table(WIN_START, WIN_END, items, set())["rows"]}

    fixed_lane = rows[("coder", "epic")]["lanes"]["subscription_credit"]
    assert fixed_lane["usd"] == 2.0
    assert fixed_lane["fixed_items"] == 1
    assert fixed_lane["median_fixed_item_usd"] == 2.0
    assert fixed_lane["abandoned_items"] == 0
    assert fixed_lane["abandoned_usd"] == 0.0

    abandoned_lane = rows[("reviewer", cj.CORRECTIONS)]["lanes"]["provider_api:openai"]
    assert abandoned_lane["usd"] == 5.0
    assert abandoned_lane["fixed_items"] == 0
    assert abandoned_lane["median_fixed_item_usd"] is None
    assert abandoned_lane["abandoned_items"] == 1
    assert abandoned_lane["abandoned_usd"] == 5.0


def test_outcome_table_keeps_lanes_apart_within_a_bucket(cj_root):
    _mk_attributed_run(cj_root, "2026-06-10", "run-sub", agent="coder", issue="issue_A", usd=2.0,
                       billing_source="subscription_credit")
    _mk_attributed_run(cj_root, "2026-06-11", "run-api", agent="coder", issue="issue_A", usd=3.0,
                       billing_source="provider_api:openai", started="2026-06-11T08:00:00Z")
    items = [_item("issue_A", type="task", parent_id=None)]
    [row] = cj.build_outcome_table(WIN_START, WIN_END, items, set())["rows"]
    assert set(row["lanes"]) == {"subscription_credit", "provider_api:openai"}
    assert row["lanes"]["subscription_credit"]["usd"] == 2.0
    assert row["lanes"]["provider_api:openai"]["usd"] == 3.0


def test_outcome_table_median_is_over_fixed_items_own_total_cost_abandoned_never_enters_it(
        cj_root):
    """Four items of the same class, same agent/model, totals 1/5/3/100 in the
    lane: item 1 and 3 are fixed (median 2), item 2 is abandoned (wontfix, cost
    5) and item 4 is still open. The abandoned item's cost must never enter
    median_fixed_item_usd, and the open item's cost enters neither total."""
    for name, issue, usd in (("run-1", "issue_1", 1.0), ("run-2", "issue_2", 5.0),
                             ("run-3", "issue_3", 3.0), ("run-4", "issue_4", 100.0)):
        _mk_attributed_run(cj_root, "2026-06-10", name, agent="coder", issue=issue, usd=usd)
    items = [_item("issue_1", type="task", state="fixed", parent_id=None),
             _item("issue_2", type="task", state="wontfix", parent_id=None),
             _item("issue_3", type="task", state="fixed", parent_id=None),
             _item("issue_4", type="task", state="open", parent_id=None)]
    [row] = cj.build_outcome_table(WIN_START, WIN_END, items, set())["rows"]
    lane = row["lanes"]["subscription_credit"]
    assert lane["fixed_items"] == 2
    assert lane["median_fixed_item_usd"] == 2.0
    assert lane["abandoned_items"] == 1
    assert lane["abandoned_usd"] == 5.0


def test_outcome_table_has_no_zero_filled_cross_product(cj_root):
    """Only a combination some run actually produced gets a row."""
    _mk_attributed_run(cj_root, "2026-06-10", "run-a", agent="coder", issue="issue_A", usd=1.0)
    items = [_item("issue_A", type="task", parent_id=None)]
    table = cj.build_outcome_table(WIN_START, WIN_END, items, set())
    assert len(table["rows"]) == 1


def test_outcome_table_coverage_counts_a_run_with_no_work_item_id(cj_root):
    _mk_attributed_run(cj_root, "2026-06-10", "run-fix", agent="coder",
                       model="claude-sonnet-4-6", issue="issue_FIX", usd=2.0)
    _mk_attributed_run(cj_root, "2026-06-11", "run-loose", agent="coder",
                       model="claude-sonnet-4-6", usd=9.0, started="2026-06-11T08:00:00Z")
    items = [_item("issue_FIX", type="task", state="fixed", parent_id=None)]
    table = cj.build_outcome_table(WIN_START, WIN_END, items, set())
    assert table["coverage"]["by_agent"]["coder"] == {
        "runs_with_work_item": 1, "runs_without_work_item": 1}
    assert table["coverage"]["by_model"]["claude-sonnet-4-6"] == {
        "runs_with_work_item": 1, "runs_without_work_item": 1}


def test_outcome_table_coverage_counts_a_pr_link_only_run_as_covered(cj_root):
    """A run with no meta.issue that build_item_costs still joins through its
    PR link lands in a row, same as test_every_run_sits_in_one_row_or_the_
    unattributed_bucket establishes for build_item_costs itself — so coverage
    must count it as covered, never as unexplained alongside a row that
    already explains its spend."""
    _mk_attributed_run(cj_root, "2026-06-10", "run-pr", agent="coder",
                       model="claude-sonnet-4-6", pr=5, usd=3.0,
                       pr_url="https://github.com/convivy/vivy/pull/5")
    items = [_item("issue_A", type="task", parent_id=None,
                   pr_url="https://github.com/convivy/vivy/pull/5")]
    table = cj.build_outcome_table(WIN_START, WIN_END, items, set())
    assert len(table["rows"]) == 1
    assert table["coverage"]["by_agent"]["coder"] == {
        "runs_with_work_item": 1, "runs_without_work_item": 0}


def test_outcome_table_an_empty_window_cannot_be_measured(cj_root):
    table = cj.build_outcome_table(WIN_START, WIN_END, [], set())
    assert table["rows"] == []
    assert table["cannot_be_measured"] is True
    assert table["reason"]


def test_outcome_table_a_measured_zero_run_is_not_cannot_be_measured(cj_root):
    """A run that was actually $0 (priced, just free) must read as a measured
    zero, never cannot_be_measured — the two must never read the same."""
    _mk_attributed_run(cj_root, "2026-06-10", "run-free", issue="issue_A", usd=0.0)
    items = [_item("issue_A", type="task", parent_id=None)]
    table = cj.build_outcome_table(WIN_START, WIN_END, items, set())
    assert table["cannot_be_measured"] is False
    [row] = table["rows"]
    assert row["lanes"]["subscription_credit"]["usd"] == 0.0


def test_outcome_table_a_failed_queue_lookup_leaves_an_ambiguous_item_out_of_rows(cj_root):
    """`queue_ids=None` means a non-epic item's parent could name a queue or an
    epic. An item with such a parent is left out of `rows` rather than guessed
    into a class, and since `rows` cannot explain its run's spend either, that
    run counts as unexplained in `coverage` too — and because every attributed
    run in the window landed on such an item, the table itself reads
    cannot_be_measured, with a reason distinct from "nothing was attributed"."""
    _mk_attributed_run(cj_root, "2026-06-10", "run-x", agent="coder",
                       model="claude-sonnet-4-6", issue="issue_X", usd=4.0)
    items = [_item("issue_X", type="task", parent_id="cq_01ABC")]
    table = cj.build_outcome_table(WIN_START, WIN_END, items, None)
    assert table["rows"] == []
    assert table["coverage"]["by_agent"]["coder"] == {
        "runs_with_work_item": 0, "runs_without_work_item": 1}
    assert table["cannot_be_measured"] is True
    assert table["reason"] == (
        "the corrections-queue lookup failed, so a parent that names a queue "
        "cannot be told apart from one that names an epic")
