"""Codex lane slice 3: what a Codex run costs, which budget pool it lands in, and what
caps and refusals apply to it.

NO TEST HERE RUNS CODEX. The usage block priced below is the `turn.completed` event
the conformance probe recorded on 2026-09-24 (fixtures/codex_turn_completed_probe.json).
"""
import json
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import budget
import pytest
import telemetry as T

LIVE_USAGE = json.loads(
    (Path(__file__).parent / "fixtures" / "codex_turn_completed_probe.json").read_text()
)["usage"]


# --- pricing -------------------------------------------------------------------

def test_the_live_probe_usage_prices_to_the_dollar_figure_derived_by_hand():
    """55,870 input of which 52,608 cached, 474 output, on gpt-6-astra
    ($10 input / $1 cached / $50 output per Mtok). Cached is a subset of input, so
    3,262 fresh * 10 + 52,608 * 1 + 474 * 50 = 32,620 + 52,608 + 23,700 = 108,928
    per Mtok = $0.108928. Pricing input_tokens whole AND cached again would give
    $0.611628, which this pins against."""
    by_model = T.cost_from_codex_usage("gpt-6-astra", LIVE_USAGE)
    assert by_model["gpt-6-astra"]["usd"] == 0.108928
    assert by_model["gpt-6-astra"]["tokens_in"] == 55870
    assert by_model["gpt-6-astra"]["tokens_out"] == 474


def test_cached_tokens_are_priced_once_at_the_cached_rate():
    usage = {"input_tokens": 1_000_000, "cached_input_tokens": 1_000_000, "output_tokens": 0}
    assert T.cost_from_codex_usage("gpt-6-astra", usage)["gpt-6-astra"]["usd"] == 1.0


def test_cache_write_tokens_are_priced_at_the_plain_input_rate():
    usage = {"input_tokens": 0, "cache_write_input_tokens": 1_000_000, "output_tokens": 0}
    assert T.cost_from_codex_usage("gpt-6-astra", usage)["gpt-6-astra"]["usd"] == 10.0


def test_every_published_codex_row_is_priced_and_resolves():
    table = {"gpt-6-astra": (10.0, 1.0, 50.0), "gpt-5.6-sol": (4.0, 0.4, 20.0),
             "gpt-5.6-terra": (2.0, 0.2, 12.0), "gpt-5.6-luna": (0.2, 0.02, 1.2),
             "gpt-5.5": (5.0, 0.5, 30.0)}
    for slug, (inp, cached, out) in table.items():
        row = T.resolve_pricing(slug)
        assert row is not None, slug
        assert (row["input"], row["cache_read"], row["output"]) == (inp, cached, out), slug


@pytest.mark.parametrize("slug", ["gpt-reserve", "codex-auto-review"])
def test_an_unpublished_slug_has_no_row_and_is_never_priced_at_zero(slug):
    assert T.resolve_pricing(slug) is None
    with pytest.raises(ValueError, match="no published price"):
        T.cost_from_codex_usage(slug, LIVE_USAGE)


# --- billing and cost sources ---------------------------------------------------

def test_write_meta_accepts_the_codex_billing_source(tmp_path):
    d = tmp_path / "run-x"
    d.mkdir()
    T.write_meta(d, run_id="run-x", agent="coder", task="t", started_at=T.now_iso(),
                 repo="agent-platform", worktree=str(tmp_path), profile="p",
                 model="gpt-6-astra", billing_source=T.CODEX_BILLING_SOURCE)
    assert json.loads((d / "meta.json").read_text())["billing_source"] == "codex_api"


def test_the_codex_cost_source_is_accepted_in_all_four_places(tmp_path):
    by_model = T.cost_from_codex_usage("gpt-6-astra", LIVE_USAGE)
    T.write_cost(tmp_path, by_model=by_model, cost_source="codex_turn_usage")
    assert T.read_cost_guarded(tmp_path)["cost_source"] == "codex_turn_usage"
    T.write_cost_partial(tmp_path, by_model=by_model, cost_source="codex_turn_usage")
    assert T.read_cost_partial(tmp_path)["cost_source"] == "codex_turn_usage"


def test_a_codex_slug_is_not_billed_to_the_anthropic_subscription(agentctl):
    assert agentctl._billing_source_for("gpt-6-astra") == "codex_api"
    assert agentctl._billing_source_for("gpt-reserve") == "codex_api"
    # a gateway-served OpenAI id keeps its own lane
    assert agentctl._billing_source_for("openai/gpt-5") == "provider_api:openai"


# --- the budget pool -----------------------------------------------------------

def _run(root, name, *, usd, billing_source, origin="scheduled", in_flight=False):
    rd = budget.today_dir() / name
    rd.mkdir(parents=True)
    meta = {"billing_source": billing_source, "origin": origin}
    if in_flight:
        meta["started_at"] = (datetime.now(timezone.utc) - timedelta(minutes=10)
                              ).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        (rd / "cost.json").write_text(json.dumps({"usd": usd}))
    (rd / "meta.json").write_text(json.dumps(meta))


def test_a_run_cannot_leave_the_budget_by_writing_billing_source_into_its_meta(runs_root):
    """The run's own subprocess can rewrite meta.json while it executes, so meta.json's
    billing_source cannot decide whether a run counts toward the cap. A Claude run
    claiming codex_api still counts in the total, the origin total and the in-flight
    estimate."""
    _run(runs_root, "run-claimed", usd=2.0, billing_source="codex_api")
    _run(runs_root, "run-claimed-live", usd=0, billing_source="codex_api", in_flight=True)
    assert budget.today_total_usd() >= 2.0
    assert budget.today_total_usd(origin="scheduled") >= 2.0
    assert budget.inflight_estimate_usd() > 0.0


# --- caps and the dispatch refusal ----------------------------------------------

def test_a_codex_model_gets_a_per_run_cost_cap_and_token_cap(agentctl, monkeypatch):
    monkeypatch.delenv("AGENTCTL_RUN_COST_CAP", raising=False)
    monkeypatch.delenv("AGENTCTL_RUN_TOKEN_CAP", raising=False)
    assert agentctl._run_cost_cap("gpt-6-astra") == agentctl.DEFAULT_RUN_COST_CAP_USD
    assert agentctl._run_token_cap("gpt-6-astra") == agentctl.DEFAULT_RUN_TOKEN_CAP
    monkeypatch.setenv("AGENTCTL_RUN_COST_CAP", "3")
    assert agentctl._run_cost_cap("gpt-5.5") == 3.0
    assert agentctl._run_cost_cap("claude-opus-4-7") is None


def test_an_unpriced_codex_slug_is_refused_before_any_guard_check(agentctl, tmp_path):
    for slug in ("gpt-reserve", "codex-auto-review"):
        refusal = agentctl.codex_dispatch_refusal(slug, tmp_path)
        assert refusal is not None and "no published price" in refusal, slug
    # control: a priced slug is refused for some other reason here (no guards
    # installed under tmp_path), so the pricing message is what distinguishes them
    other = agentctl.codex_dispatch_refusal("gpt-6-astra", tmp_path)
    assert "published price" not in (other or "")


def test_spawn_refuses_an_unpriced_codex_slug(agentctl, monkeypatch, capsys):
    from conftest import stub_profile_listing
    stub_profile_listing(agentctl, monkeypatch)
    monkeypatch.setattr(agentctl, "_warn_deployed_tree", lambda: None)
    monkeypatch.setattr(agentctl, "_spawn_lib_gate", lambda: None)
    monkeypatch.setattr(agentctl, "resolve_agent",
                        lambda name, repo=None, agents_dir=None: {"model_alias": "gpt-reserve"})
    monkeypatch.setattr(agentctl.R, "can_spawn", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("must be refused before R.can_spawn")))
    args = types.SimpleNamespace(agent="reviewer", task="t", on="agent-platform", profile=None,
                                 agents_dir=None, parent=None, pr=None, review_sha=None,
                                 model=None, reason=None)
    assert agentctl.cmd_spawn(args) == 2
    assert "no published price" in capsys.readouterr().err
