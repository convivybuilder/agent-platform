"""The review-replay eval harness's spend cap and cost estimate
(agent-platform#807).

Only gateway-served candidates (deepseek, kimi, kimi-code, glm) spend real
dollars against a cap here — the Anthropic baselines are subscription-billed
and are reported separately (tokens + API-list-equivalent dollars), never
counted against `CapTracker`. The replay runner decides which is which (it
already knows each model's provider); this module just tracks and estimates.
"""
from __future__ import annotations

import threading
from pathlib import Path

import yaml

DEFAULT_TOTAL_CAP_USD = 50.0
DEFAULT_PER_MODEL_CAP_USD: dict[str, float] = {
    "deepseek": 15.0,
    "kimi": 12.0,
    "kimi-code": 12.0,
    "glm": 15.0,
}


def load_gateway_prices(config_path: Path) -> dict[str, dict[str, float]]:
    """Per-token pricing by gateway model-group name (`gateway/config.yaml`'s
    `model_list[].model_name` — the same names `GATEWAY_MODEL_GROUPS` in
    bin/agentctl routes `--model` to), read from each entry's pinned
    `model_info`. An entry with no `model_info` block is skipped — this
    harness only prices a model it has a pinned rate for, the same "a missing
    row is refused rather than silently free" rule telemetry.py's own pricing
    tables carry."""
    data = yaml.safe_load(Path(config_path).read_text()) or {}
    prices: dict[str, dict[str, float]] = {}
    for entry in data.get("model_list") or []:
        name = entry.get("model_name")
        info = entry.get("model_info") or {}
        if not name or not info.get("input_cost_per_token"):
            continue
        in_rate = float(info["input_cost_per_token"])
        prices[name] = {
            "in": in_rate,
            "cache_read": float(info.get("cache_read_input_token_cost") or in_rate),
            "out": float(info.get("output_cost_per_token") or 0.0),
        }
    return prices


def historical_cache_hit_ratio(model: str, spend_records) -> float | None:
    """The aggregate cache-read fraction of input tokens for `model` across
    `spend_records` (dicts shaped like a `gateway_spend.jsonl` line: `model`,
    `tokens_in`, optional `cache_read_tokens`) that report a cache split.
    None when no record for this model reports one — the caller then prices
    every token as a cache miss, per the harness's default."""
    total_in = 0
    total_read = 0
    saw_split = False
    for r in spend_records:
        if r.get("model") != model:
            continue
        read = r.get("cache_read_tokens")
        if read is None:
            continue
        saw_split = True
        total_in += int(r.get("tokens_in") or 0)
        total_read += int(read)
    if not saw_split or total_in <= 0:
        return None
    return total_read / total_in


def estimate_cost(entries, prices: dict[str, float], *,
                  cache_hit_ratio: float | None = None) -> float:
    """Sum of `entries`' (dict-shaped, `tokens_in`/`tokens_out`) cost at
    `prices`. Every input token prices as a cache MISS unless
    `cache_hit_ratio` says otherwise — the harness's explicit default,
    because an estimate run before any replay has no gateway spend log of its
    own to read a split from."""
    total = 0.0
    for e in entries:
        tokens_in = int(e.get("tokens_in") or 0)
        tokens_out = int(e.get("tokens_out") or 0)
        if cache_hit_ratio:
            reads = tokens_in * cache_hit_ratio
            misses = tokens_in - reads
        else:
            reads, misses = 0.0, float(tokens_in)
        total += reads * prices["cache_read"] + misses * prices["in"] + tokens_out * prices["out"]
    return total


def estimate_batch(corpus: dict, model_keys: list[str], prices: dict[str, dict],
                   *, cache_hit_ratios: dict[str, float] | None = None) -> dict[str, dict]:
    """Per-model estimate over the whole corpus (confirmed + clean), with the
    pricing basis named per model so the printed estimate can say which it
    used, per the harness's spend-cap requirement."""
    cache_hit_ratios = cache_hit_ratios or {}
    entries = list(corpus.get("confirmed") or []) + list(corpus.get("clean") or [])
    out: dict[str, dict] = {}
    for model in model_keys:
        p = prices.get(model)
        if p is None:
            out[model] = {"usd": None,
                         "note": f"no pinned pricing for {model!r} in gateway/config.yaml"}
            continue
        ratio = cache_hit_ratios.get(model)
        usd = estimate_cost(entries, p, cache_hit_ratio=ratio)
        note = (f"historical cache-hit ratio {ratio:.0%} from the gateway spend log"
               if ratio else
               "no historical cache-split evidence for this model; priced every "
               "token as a cache miss")
        out[model] = {"usd": usd, "note": note}
    return out


class CapTracker:
    """Live spend against a total cap and per-model caps while dispatching
    replays. A model that reaches its OWN cap stops only that model's
    remaining dispatches; every other model keeps going. `already_spent`
    seeds the tracker from a resumed batch's existing result files, so a
    rerun honours the cap across restarts rather than re-granting it.
    """

    def __init__(self, *, total_cap: float = DEFAULT_TOTAL_CAP_USD,
                per_model_cap: dict[str, float] | None = None,
                already_spent: dict[str, float] | None = None):
        self.total_cap = total_cap
        self.per_model_cap = dict(per_model_cap or DEFAULT_PER_MODEL_CAP_USD)
        self.spent: dict[str, float] = dict(already_spent or {})
        # `run_batch` calls `reserve`/`reconcile` from multiple worker threads
        # (bounded concurrency, agent-platform#807) — without this, concurrent
        # calls could race the read-modify-write on `self.spent` and under-count
        # real spend, the one way a cap enforcement bug is worse than none: it
        # would still LOOK like it capped. `reserve` additionally depends on this
        # lock for its atomicity GUARANTEE, not just thread-safety — see its
        # docstring.
        self._lock = threading.Lock()

    @property
    def total_spent(self) -> float:
        return sum(self.spent.values())

    def model_cap(self, model: str) -> float | None:
        return self.per_model_cap.get(model)

    def model_spent(self, model: str) -> float:
        return self.spent.get(model, 0.0)

    def can_dispatch(self, model: str) -> bool:
        """Whether a NEW replay on `model` may be dispatched — checked before
        every dispatch, not just once per batch, so spend recorded mid-batch
        by concurrent replays is seen by the next check."""
        with self._lock:
            if self.total_spent >= self.total_cap:
                return False
            cap = self.model_cap(model)
            if cap is not None and self.model_spent(model) >= cap:
                return False
            return True

    def record(self, model: str, usd: float) -> None:
        with self._lock:
            self.spent[model] = self.model_spent(model) + usd

    def reserve(self, model: str, estimated_usd: float) -> bool:
        """Atomically check-and-reserve `estimated_usd` of `model`'s spend
        against both caps, in ONE lock acquisition.

        Round-1 review (agent-platform#813): `can_dispatch` (check) and
        `record` (write) are separately locked but not atomic AS A PAIR —
        `run_batch` calls `can_dispatch` at dispatch time and only calls
        `record` once the WHOLE replay finishes (often minutes later), so
        several workers can each pass `can_dispatch` before any of them has
        recorded anything: a bounded overshoot of up to `concurrency - 1`
        replays' worth of spend past a cap. `reserve` closes that by making
        the check and the write one atomic step — the next worker's
        `reserve` sees this one's reservation immediately, under the same
        lock, rather than after the dispatch it gates has already returned.
        `reconcile` corrects the reservation to the real spend once it's
        known. Returns False (reserving nothing) when adding this estimate
        would meet or exceed either cap — the same `>=` boundary
        `can_dispatch` checks against already-recorded spend, generalized
        to also cover the reservation being made right now. (A zero
        estimate therefore refuses exactly when `can_dispatch` would: at or
        past the cap already, not before.)"""
        with self._lock:
            if self.total_spent + estimated_usd >= self.total_cap:
                return False
            cap = self.model_cap(model)
            if cap is not None and self.model_spent(model) + estimated_usd >= cap:
                return False
            self.spent[model] = self.model_spent(model) + estimated_usd
            return True

    def reconcile(self, model: str, estimated_usd: float, actual_usd: float) -> None:
        """Replace a `reserve`d estimate with the real spend once a replay
        finishes — the second half of the reserve/reconcile pair.

        Never refuses: the dispatch already happened, so there is nothing
        left to gate, only the ledger to correct. Correcting can push the
        ledger slightly over a cap that `reserve` placed right at the edge
        (the estimate undershot the real cost) — self-correcting on the
        NEXT `reserve` call, the same way the old check-then-record gap
        self-corrected, except now bounded by estimate error rather than by
        how many workers raced the check."""
        with self._lock:
            self.spent[model] = self.model_spent(model) + (actual_usd - estimated_usd)
