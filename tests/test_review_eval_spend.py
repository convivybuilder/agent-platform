"""lib/agent-platform/review_eval_spend.py — the review-replay eval harness's
spend cap and cost estimate (agent-platform#807). Synthetic prices and
synthetic corpus entries throughout; no real token counts or review content.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib" / "agent-platform"))
import review_eval_spend as RES  # noqa: E402


def test_load_gateway_prices_reads_pinned_model_info(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("""
model_list:
  - model_name: deepseek
    litellm_params:
      model: deepseek/deepseek-v4-flash
    model_info:
      input_cost_per_token: 0.00000014
      cache_read_input_token_cost: 0.0000000028
      output_cost_per_token: 0.00000028
  - model_name: gpt5
    litellm_params:
      model: openai/gpt-5
""")
    prices = RES.load_gateway_prices(config)
    assert prices["deepseek"] == {"in": 0.00000014, "cache_read": 0.0000000028,
                                  "out": 0.00000028}
    # gpt5 has no model_info block (LiteLLM prices it from its own map) — skipped,
    # not defaulted to zero.
    assert "gpt5" not in prices


def test_estimate_cost_prices_every_token_as_a_cache_miss_by_default():
    # prices are dollars PER TOKEN, matching the real gateway's model_info rates
    prices = {"in": 1e-6, "cache_read": 1e-7, "out": 2e-6}
    entries = [{"tokens_in": 1_000_000, "tokens_out": 1_000_000}]
    usd = RES.estimate_cost(entries, prices)
    assert usd == 1.0 + 2.0  # 1M in at $1/Mtok, 1M out at $2/Mtok


def test_estimate_cost_applies_a_cache_hit_ratio_when_given():
    prices = {"in": 1e-6, "cache_read": 1e-7, "out": 0.0}
    entries = [{"tokens_in": 1_000_000, "tokens_out": 0}]
    usd = RES.estimate_cost(entries, prices, cache_hit_ratio=0.9)
    # 90% reads at $0.1/Mtok, 10% misses at $1/Mtok
    assert abs(usd - (900_000 * 1e-7 + 100_000 * 1e-6)) < 1e-9


def test_historical_cache_hit_ratio_none_when_no_record_reports_a_split():
    records = [{"model": "deepseek", "tokens_in": 1000}]  # no cache_read_tokens
    assert RES.historical_cache_hit_ratio("deepseek", records) is None


def test_historical_cache_hit_ratio_aggregates_across_records():
    records = [
        {"model": "deepseek", "tokens_in": 1000, "cache_read_tokens": 900},
        {"model": "deepseek", "tokens_in": 1000, "cache_read_tokens": 100},
        {"model": "kimi", "tokens_in": 1000, "cache_read_tokens": 1000},  # other model
    ]
    ratio = RES.historical_cache_hit_ratio("deepseek", records)
    assert abs(ratio - 0.5) < 1e-9


def test_estimate_batch_names_the_pricing_basis_per_model():
    corpus = {"confirmed": [{"tokens_in": 1000, "tokens_out": 100}], "clean": []}
    prices = {"deepseek": {"in": 1e-6, "cache_read": 1e-7, "out": 2e-6}}
    out = RES.estimate_batch(corpus, ["deepseek", "kimi"], prices)
    assert out["deepseek"]["usd"] is not None
    assert "cache miss" in out["deepseek"]["note"]
    assert out["kimi"]["usd"] is None and "no pinned pricing" in out["kimi"]["note"]

    out2 = RES.estimate_batch(corpus, ["deepseek"], prices,
                              cache_hit_ratios={"deepseek": 0.42})
    assert "42%" in out2["deepseek"]["note"]


# --- CapTracker ---------------------------------------------------------------


def test_cap_tracker_stops_one_model_without_stopping_another():
    cap = RES.CapTracker(total_cap=100.0, per_model_cap={"deepseek": 5.0, "kimi": 50.0})
    cap.record("deepseek", 5.0)
    assert cap.can_dispatch("deepseek") is False
    assert cap.can_dispatch("kimi") is True


def test_cap_tracker_total_cap_stops_every_model():
    cap = RES.CapTracker(total_cap=10.0, per_model_cap={"deepseek": 50.0, "kimi": 50.0})
    cap.record("deepseek", 10.0)
    assert cap.can_dispatch("deepseek") is False
    assert cap.can_dispatch("kimi") is False


def test_cap_tracker_resumes_from_already_spent():
    cap = RES.CapTracker(total_cap=100.0, per_model_cap={"deepseek": 5.0},
                         already_spent={"deepseek": 4.99})
    assert cap.can_dispatch("deepseek") is True
    cap.record("deepseek", 0.02)
    assert cap.can_dispatch("deepseek") is False


def test_cap_tracker_model_with_no_configured_cap_is_unbounded_per_model():
    cap = RES.CapTracker(total_cap=100.0, per_model_cap={})
    cap.record("sonnet-5-5", 99.0)
    assert cap.can_dispatch("sonnet-5-5") is True
    cap.record("sonnet-5-5", 1.0)
    assert cap.can_dispatch("sonnet-5-5") is False  # total cap still applies


# --- CapTracker.reserve / .reconcile (round-1 revise, agent-platform#813) -----


def test_reserve_sees_an_unreconciled_reservation_immediately():
    """The gap `can_dispatch`+`record` left: a reservation that has not yet
    been reconciled to real spend must still block a second reservation
    against the same cap — this is the atomicity `reserve` exists to add."""
    cap = RES.CapTracker(total_cap=100.0, per_model_cap={"deepseek": 5.0})
    assert cap.reserve("deepseek", 4.0) is True
    assert cap.reserve("deepseek", 1.5) is False  # 4.0 + 1.5 would meet/exceed the $5 cap
    assert cap.reserve("kimi", 0.01) is True  # a different model is unaffected


def test_reserve_concurrent_workers_cannot_overshoot_the_total_cap():
    """50 threads each try to reserve $1 against a $5 total cap, with no
    `reconcile` in between — the exact shape of the race round 1 found:
    several workers passing a check before any of them recorded anything.
    `reserve`'s lock must make this behave as if every call ran one at a
    time, however they are actually interleaved."""
    cap = RES.CapTracker(total_cap=5.0, per_model_cap={})
    results = []
    results_lock = threading.Lock()

    def worker():
        ok = cap.reserve("deepseek", 1.0)
        with results_lock:
            results.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(results) == 4  # spent lands at 0,1,2,3 before the 5th would hit the cap
    assert cap.total_spent == 4.0
    assert cap.total_spent < cap.total_cap  # never overshoots


def test_reserve_concurrent_workers_cannot_overshoot_a_per_model_cap():
    cap = RES.CapTracker(total_cap=100.0, per_model_cap={"deepseek": 5.0})
    results = []
    results_lock = threading.Lock()

    def worker():
        ok = cap.reserve("deepseek", 1.0)
        with results_lock:
            results.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(results) == 4
    assert cap.model_spent("deepseek") == 4.0
    assert cap.model_spent("deepseek") < cap.model_cap("deepseek")


def test_reconcile_corrects_a_reservation_to_the_real_spend():
    cap = RES.CapTracker(total_cap=100.0, per_model_cap={})
    assert cap.reserve("deepseek", 2.0) is True
    cap.reconcile("deepseek", 2.0, 3.5)  # the estimate undershot the real cost
    assert cap.model_spent("deepseek") == 3.5


def test_reconcile_then_reserve_reflects_the_real_spend_not_the_estimate():
    cap = RES.CapTracker(total_cap=100.0, per_model_cap={"deepseek": 5.0})
    assert cap.reserve("deepseek", 0.0) is True  # an unestimated entry, as in the harness
    cap.reconcile("deepseek", 0.0, 5.0)  # the real replay cost the whole cap
    assert cap.reserve("deepseek", 0.0) is False  # the next reservation sees the real figure
