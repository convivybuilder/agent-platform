"""Cost-window math (bin/agent-summary subwindow_bounds — ARCH-091).

The risk this guards: the overnight/daytime digest split. If the window
boundaries are off (or straddle midnight wrong), overnight spend mixes into the
daytime report or vice versa — the $0-cost-report class of bug. The window:
  overnight → [fire @ 01:00, fire @ 07:00), labeled fire
  daytime   → [fire-1 @ 07:00, fire @ 01:00), labeled fire-1
"""
from __future__ import annotations

from datetime import date

import pytest


def test_subwindow_overnight(agent_summary):
    fd = date(2026, 5, 31)
    start, end, label = agent_summary.subwindow_bounds(fd, "overnight")
    assert start.hour == agent_summary.DAY_BOUNDARY_HOUR  # 01:00
    assert end.hour == agent_summary.DIGEST_HOUR          # 07:00
    assert start.date() == fd and end.date() == fd
    assert label == fd
    assert start < end


def test_subwindow_daytime_spans_prior_day(agent_summary):
    fd = date(2026, 5, 31)
    start, end, label = agent_summary.subwindow_bounds(fd, "daytime")
    # starts the prior day at the digest hour, ends today at the day boundary
    assert start.hour == agent_summary.DIGEST_HOUR
    assert start.date() == date(2026, 5, 30)
    assert end.hour == agent_summary.DAY_BOUNDARY_HOUR
    assert end.date() == fd
    assert label == date(2026, 5, 30)
    assert start < end


def test_subwindow_unknown_window_raises(agent_summary):
    with pytest.raises(ValueError, match="unknown window"):
        agent_summary.subwindow_bounds(date(2026, 5, 31), "weekly")


def test_parse_iso_handles_z_and_none(agent_summary):
    assert agent_summary.parse_iso(None) is None
    dt = agent_summary.parse_iso("2026-05-31T08:00:02Z")
    assert (dt.year, dt.month, dt.day, dt.hour) == (2026, 5, 31, 8)


# --- per-provider / per-model breakdown (the operator's #agent-cost subcategories) ------


def _run(billing_source, by_model=None, usd=None, model=None, agent=None):
    cost = {"by_model": by_model} if by_model is not None else {}
    if usd is not None:
        cost["usd"] = usd
    meta = {"billing_source": billing_source}
    if model is not None:
        meta["model"] = model
    if agent is not None:
        meta["agent"] = agent
    return {"meta": meta, "cost": cost}


def test_is_metered(agent_summary):
    assert agent_summary.is_metered("provider_api:deepseek")
    assert agent_summary.is_metered("provider_api:openai")
    assert not agent_summary.is_metered("subscription_credit")
    assert not agent_summary.is_metered("")
    assert not agent_summary.is_metered(None)


def test_fmt_usd_precision(agent_summary):
    assert agent_summary.fmt_usd(0.2041) == "$0.2041"   # sub-$1 → 4dp (metered)
    assert agent_summary.fmt_usd(42.5) == "$42.50"       # ≥$1 → 2dp


def test_aggregate_by_model_groups_and_sums(agent_summary):
    runs = [
        _run("subscription_credit", {"claude-opus-4-7": {"usd": 10.0, "tokens_in": 100, "tokens_out": 5}}),
        _run("subscription_credit", {"claude-opus-4-7": {"usd": 5.0, "tokens_in": 50, "tokens_out": 3}}),
        _run("subscription_credit", {"claude-sonnet-4-6": {"usd": 2.0, "tokens_in": 20, "tokens_out": 2}}),
        _run("provider_api:deepseek", {"deepseek/deepseek-v4-flash": {"usd": 0.2041, "tokens_in": 7, "tokens_out": 4}}),
    ]
    agg = agent_summary.aggregate_by_model(runs)
    assert set(agg) == {"subscription_credit", "provider_api:deepseek"}
    opus = agg["subscription_credit"]["models"]["claude-opus-4-7"]
    assert opus["usd"] == 15.0 and opus["runs"] == 2 and opus["tokens_in"] == 150
    assert agg["subscription_credit"]["usd"] == 17.0
    ds = agg["provider_api:deepseek"]["models"]["deepseek/deepseek-v4-flash"]
    assert ds["usd"] == 0.2041 and ds["runs"] == 1


def test_aggregate_tracks_per_agent_within_model(agent_summary):
    # one model, multiple agents → each agent's spend + run count is split out
    runs = [
        _run("subscription_credit", {"claude-sonnet-4-6": {"usd": 3.0}}, agent="reviewer"),
        _run("subscription_credit", {"claude-sonnet-4-6": {"usd": 1.0}}, agent="reviewer"),
        _run("subscription_credit", {"claude-sonnet-4-6": {"usd": 2.0}}, agent="explainer"),
    ]
    agents = agent_summary.aggregate_by_model(runs)["subscription_credit"]["models"]["claude-sonnet-4-6"]["agents"]
    assert agents["reviewer"]["usd"] == 4.0 and agents["reviewer"]["runs"] == 2
    assert agents["explainer"]["usd"] == 2.0 and agents["explainer"]["runs"] == 1


def test_aggregate_fallback_to_meta_model_when_no_by_model(agent_summary):
    # a run that booked cost but wrote no by_model is attributed to meta.model
    runs = [_run("provider_api:openai", by_model=None, usd=0.5, model="openai/gpt-5")]
    agg = agent_summary.aggregate_by_model(runs)
    assert agg["provider_api:openai"]["models"]["openai/gpt-5"]["usd"] == 0.5


def test_aggregate_skips_zero_cost_no_split_runs(agent_summary):
    # budget_refused / no-cost runs (empty by_model, usd 0) contribute nothing
    runs = [_run("subscription_credit", by_model={}, usd=0.0)]
    assert agent_summary.aggregate_by_model(runs) == {}


def test_metered_subscription_split(agent_summary):
    runs = [
        _run("subscription_credit", {"claude-opus-4-7": {"usd": 42.54}}),
        _run("provider_api:deepseek", {"deepseek/deepseek-v4-flash": {"usd": 0.2041}}),
    ]
    metered, subscription = agent_summary.metered_subscription_split(
        agent_summary.aggregate_by_model(runs))
    assert metered == 0.2041
    assert subscription == 42.54


def test_cost_by_model_section_renders_split_and_rows(agent_summary):
    runs = [
        _run("subscription_credit", {"claude-opus-4-7": {"usd": 42.54, "tokens_in": 1, "tokens_out": 1}},
             agent="overnight-orchestrator"),
        _run("provider_api:deepseek", {"deepseek/deepseek-v4-flash": {"usd": 0.2041, "tokens_in": 1, "tokens_out": 1}},
             agent="bug-fixer"),
    ]
    out = agent_summary.cost_by_model_section(runs)
    assert "Real metered API spend: $0.2041" in out
    assert "subscription-credit (list-price proxy): $42.54" in out
    assert "`claude-opus-4-7`" in out and "`deepseek/deepseek-v4-flash`" in out
    assert "DeepSeek · metered API" in out
    # per-agent rows present under their model
    assert "overnight-orchestrator" in out and "bug-fixer" in out
    assert "all agents" in out  # bold model-subtotal row


def test_cost_by_model_section_empty(agent_summary):
    assert "No attributable spend" in agent_summary.cost_by_model_section([])
