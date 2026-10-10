"""LiteLLM custom callback: writes per-request spend records to a JSONL log.

CORRELATION (Phase C C1 follow-up, 2026-06-01):
  agentctl sets ANTHROPIC_CUSTOM_HEADERS in the subprocess env for gateway
  models. The Anthropic SDK (Python and TypeScript) reads this env var and
  injects the listed headers into every upstream API request. Those headers
  arrive at the LiteLLM proxy and are stored in
  kwargs["litellm_params"]["metadata"]["headers"] by the pre-call utils
  (litellm_pre_call_utils.py line ~996, fixed in #17477).

  Headers injected by agentctl for every gateway spawn:
    x-agentctl-run-id   — agentctl run identifier
    x-agentctl-agent    — agent name (e.g. "coder")
    x-agentctl-task     — short task label (truncated to 80 chars)
    x-agentctl-run-dir  — absolute path to the run directory

  When x-agentctl-run-dir is present this callback writes to
  <run_dir>/gateway_spend.jsonl so _finalize_run can source exact cost from
  the per-run log. Without it (PA seam, non-agentctl call, or old proxy
  version) the fallback writes to the global per-day log at
  ~/Convivy/agent-runs/gateway-spend/YYYY-MM-DD.jsonl.

CACHE-SPLIT BEACON (2026-06-03):
  On 2026-06-02, DeepSeek's V4-flash rollout returned usage without the
  normalized cache hit/miss split on ~41% of records, while still giving
  LiteLLM enough to price the cache discount. Those records logged correct
  cost but no cache_read/cache_miss fields — indistinguishable from a
  non-caching provider, so the gap stayed invisible until a forensic spend
  audit. To make a recurrence self-announcing, _record stamps
  cache_split="unreported" when usage is present and the prompt is large
  enough (CACHE_SPLIT_EXPECTED_MIN_PROMPT) that a caching provider should have
  reported a split but didn't.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

# The writer resolves the run-records root the same way its reader,
# bin/agent-cost-report, does (builder_root.runs_root(), company/decisions/0008).
# A SIBLING import, because the gateway does not run from the checkout: install.sh
# copies this file into the pinned ~/.local/lib/llm-gateway, the plist's only
# PYTHONPATH entry, and copies builder_root.py beside it. A path relative to this
# file names nothing there (security review, PR 538).
# tests/test_spend_callback.py runs install.sh's own copy loop and imports this
# from the result, so a copy list that drops the resolver fails there.
import builder_root as _BR

try:
    from litellm.integrations.custom_logger import CustomLogger
    _BASE = CustomLogger
except ImportError:
    # Allow importing this module outside the gateway venv (e.g. for inspection)
    # without blowing up. The class body below is still valid Python.
    _BASE = object


GATEWAY_SPEND_ROOT = _BR.runs_root() / "gateway-spend"

# Prompt size at/above which a prompt-caching provider should report a cache
# hit/miss split. When a record this large carries no split, that's an anomaly
# worth beaconing (see _record); below it, a missing split is unremarkable —
# tiny prompts aren't worth caching — so we stay quiet to avoid noise. DeepSeek
# caches prefixes of even moderate prompts, and the 2026-06-02 V4-flash gap was
# on ~80k-token requests, so 1024 cleanly separates real agent calls from pings.
CACHE_SPLIT_EXPECTED_MIN_PROMPT = 1024


def _today_log() -> Path:
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    GATEWAY_SPEND_ROOT.mkdir(parents=True, exist_ok=True)
    return GATEWAY_SPEND_ROOT / f"{date}.jsonl"


def _correlation_from_kwargs(kwargs: dict) -> dict:
    """Extract agentctl correlation fields from LiteLLM callback kwargs.

    Returns a dict with keys run_id, agent, task, run_dir (all strings,
    empty string when absent).

    The proxy stores the cleaned request headers in DIFFERENT places by
    endpoint: /v1/chat/completions (OpenAI format) uses
    litellm_params["metadata"]["headers"], while /v1/messages (Anthropic
    format — what `claude -p` speaks through ANTHROPIC_BASE_URL) uses
    litellm_params["litellm_metadata"]["headers"]. On the /v1/messages path a
    `metadata` dict is still present but carries no headers, so we must check
    BOTH containers (and the nested requester_metadata) and pick whichever
    actually has x-agentctl-* keys — not short-circuit on the first dict.
    """
    litellm_params = kwargs.get("litellm_params") or {}
    headers: dict = {}
    for container in (litellm_params.get("metadata"),
                      litellm_params.get("litellm_metadata")):
        if not isinstance(container, dict):
            continue
        h = container.get("headers")
        if not h:
            rm = container.get("requester_metadata")
            h = rm.get("headers") if isinstance(rm, dict) else None
        if isinstance(h, dict) and any(
            str(k).lower().startswith("x-agentctl") for k in h
        ):
            headers = h
            break
    return {
        "run_id": headers.get("x-agentctl-run-id") or "",
        "agent": headers.get("x-agentctl-agent") or "",
        "task": headers.get("x-agentctl-task") or "",
        "run_dir": headers.get("x-agentctl-run-dir") or "",
    }


def _cache_tokens(usage, tokens_in: int) -> tuple[int | None, int | None]:
    """Return (cache_read_tokens, cache_miss_tokens), or (None, None) when the
    provider reports no cache split.

    Prompt-caching providers (DeepSeek, etc.) bill cache *reads* at a steep
    discount and cache *misses* at the full input rate — DeepSeek's read rate is
    ~50x below its miss rate — so the hit/miss split, not the raw input count, is
    what drives real cost. `tokens_in` (prompt_tokens) alone hides it: a run that
    is 95% cache reads and one that is 95% misses report the same prompt_tokens
    but differ ~30x in dollars. Logging the split makes the spend log auditable
    against the provider's bill instead of inferred.

    LiteLLM normalizes cache reads to usage.prompt_tokens_details.cached_tokens
    (OpenAI shape); DeepSeek's raw response also carries prompt_cache_hit_tokens /
    prompt_cache_miss_tokens. Check the normalized field first, then the raw
    fields; derive miss = total_input - reads when only the read count is known.
    """
    if usage is None:
        return None, None

    read = None
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None:
        c = getattr(details, "cached_tokens", None)
        if c is None and isinstance(details, dict):
            c = details.get("cached_tokens")
        if c is not None:
            read = int(c)

    raw_hit = getattr(usage, "prompt_cache_hit_tokens", None)
    raw_miss = getattr(usage, "prompt_cache_miss_tokens", None)
    if read is None and raw_hit is not None:
        read = int(raw_hit)

    if read is None and raw_miss is None:
        return None, None  # provider reported no cache information at all

    miss = int(raw_miss) if raw_miss is not None else None
    if miss is None and read is not None:
        miss = max(0, int(tokens_in) - read)
    if read is None and miss is not None:
        read = max(0, int(tokens_in) - miss)
    return read, miss


# Fallback per-token pricing for trial gateway models that LiteLLM's built-in
# price map can't price — for these, `response_cost` comes back 0 and real spend
# would log as $0 (the 2026-06-20 incident: glm-5.2 billed ~$9.30 at z.ai but the
# gateway log + every cost view read $0). config.yaml pins `model_info` for these
# deployments, but that pricing doesn't reach `response_cost` on the /v1/messages
# path. So we recompute here. PREFER the deployment's own model_info from kwargs
# (_deployment_prices — single source of truth, auto-covers any pinned model);
# fall back to this table when model_info isn't delivered, keyed by model id or
# basename. Rates per token: in = cache-miss / full input; cache_read = cache-hit
# input; out. MUST mirror gateway/config.yaml `model_info` — one entry per model
# whose config block is marked "absent from the installed LiteLLM map" (glm /
# mimo / kimi). Models LiteLLM DOES price (deepseek, kimi-k2.7-code) are
# deliberately omitted: their response_cost is non-zero, so this fallback never
# fires for them.
# Authoritative membership test: a model needs an entry here iff LiteLLM CANNOT
# price it (litellm.get_model_info raises → response_cost 0). The config.yaml
# "absent from the LiteLLM map" comments are NOT reliable for this (verified
# 2026-06-20: they had kimi-k2.6 and kimi-k2.7-code backwards). Check ground
# truth with the gateway venv when adding a gateway model:
#   .local/venv-llm-gateway/bin/python3 -c "import litellm; litellm.get_model_info('<provider/model>')"
# Confirmed unpriced (need an entry): zai/glm-5.2, openai/mimo-v2.5-pro,
# moonshot/kimi-k2.7-code. Priced by LiteLLM (no entry needed): deepseek-chat,
# moonshot/kimi-k2.6. Rates per token, mirror config.yaml model_info.
_PINNED_PRICES = {
    # config.yaml model_name: glm   (zai/glm-5.2)
    "glm-5.2":          {"in": 0.0000014,    "cache_read": 0.00000026,   "out": 0.0000044},
    # config.yaml model_name: mimo  (openai/mimo-v2.5-pro)
    "mimo-v2.5-pro":    {"in": 0.000000435,  "cache_read": 0.0000000036, "out": 0.00000087},
    # config.yaml model_name: kimi-code  (moonshot/kimi-k2.7-code — unpriced by LiteLLM)
    "kimi-k2.7-code":   {"in": 0.00000095,   "cache_read": 0.00000019,   "out": 0.000004},
}


def _deployment_prices(kwargs: dict) -> dict | None:
    """Per-token pricing from the deployment's `model_info` (config.yaml), if
    LiteLLM surfaced it in the callback kwargs. Single source of truth — covers
    any pinned model automatically. None when no usable pricing is present."""
    litellm_params = kwargs.get("litellm_params") or {}
    for mi in (litellm_params.get("model_info"), kwargs.get("model_info")):
        if isinstance(mi, dict) and mi.get("input_cost_per_token"):
            in_rate = float(mi["input_cost_per_token"])
            return {
                "in": in_rate,
                "cache_read": float(mi.get("cache_read_input_token_cost") or in_rate),
                "out": float(mi.get("output_cost_per_token") or 0.0),
            }
    return None


def _fallback_cost(kwargs: dict, model: str, tokens_in: int, tokens_out: int,
                   cache_read: int | None, cache_miss: int | None) -> float:
    """Recompute request cost when LiteLLM's response_cost is 0 (unpriced model).
    Cache-aware: cache reads bill at the discounted rate, the remaining input at
    the full (cache-miss) rate. Prefers deployment model_info, then the pinned
    table (by full model id or basename). Returns 0.0 when no pricing is known."""
    prices = (_deployment_prices(kwargs)
              or _PINNED_PRICES.get(model)
              or _PINNED_PRICES.get(model.split("/")[-1]))
    if not prices:
        return 0.0
    reads = cache_read or 0
    misses = cache_miss if cache_miss is not None else max(0, int(tokens_in) - reads)
    return (reads * prices["cache_read"]
            + misses * prices["in"]
            + int(tokens_out) * prices["out"])


class SpendCallback(_BASE):
    """Appends one JSONL line per completed LiteLLM request.

    Writes to the per-run gateway_spend.jsonl when x-agentctl-run-dir is
    present in the request headers (agentctl gateway spawn), otherwise to
    the global per-day fallback log (PA seam or non-agentctl call).
    """

    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        self._record(kwargs, response_obj)

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        self._record(kwargs, response_obj)

    def _record(self, kwargs, response_obj) -> None:
        try:
            # Intentionally not logged: kwargs["messages"] (prompt content) and
            # kwargs["api_key"] (secret). Only cost/token/correlation fields are written.
            model = kwargs.get("model") or ""
            usage = getattr(response_obj, "usage", None)
            tokens_in = getattr(usage, "prompt_tokens", 0) or 0
            tokens_out = getattr(usage, "completion_tokens", 0) or 0
            request_id = getattr(response_obj, "id", None) or ""
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            cache_read, cache_miss = _cache_tokens(usage, tokens_in)

            # LiteLLM prices most models into response_cost. For trial models
            # absent from its map (glm-5.2, mimo) it returns 0 — recompute from
            # the deployment's pinned pricing so real spend isn't logged as $0
            # (ARCH-207 follow-up; the 2026-06-20 z.ai $9.30-as-$0 incident).
            cost = kwargs.get("response_cost") or 0.0
            if not cost:
                cost = _fallback_cost(kwargs, model, tokens_in, tokens_out,
                                      cache_read, cache_miss)

            corr = _correlation_from_kwargs(kwargs)
            record = {
                "ts": ts,
                "model": model,
                "tokens_in": int(tokens_in),
                "tokens_out": int(tokens_out),
                "usd": round(float(cost), 6),
                "request_id": request_id,
            }
            # Cache split (when the provider reports it) — the lever behind real
            # cost on prompt-caching models; lets the spend log be reconciled
            # against the provider bill rather than inferred.
            if cache_read is not None:
                record["cache_read_tokens"] = cache_read
            if cache_miss is not None:
                record["cache_miss_tokens"] = cache_miss
            # Beacon: usage is present and the prompt is large enough that a
            # caching provider should report a hit/miss split, yet none came
            # through. Stamp it so the gap is dashboard-countable instead of
            # indistinguishable from a non-caching provider. DeepSeek's
            # 2026-06-02 V4-flash rollout silently dropped the normalized split
            # on ~41% of that day's records while still pricing the discount;
            # the gap was invisible until a forensic dig. This makes a
            # recurrence self-announcing. (cache_read/cache_miss are both-set or
            # both-None from _cache_tokens, so testing cache_read covers both.)
            if (cache_read is None and usage is not None
                    and int(tokens_in) >= CACHE_SPLIT_EXPECTED_MIN_PROMPT):
                record["cache_split"] = "unreported"
            if corr["run_id"]:
                record["run_id"] = corr["run_id"]
            if corr["agent"]:
                record["agent"] = corr["agent"]
            if corr["task"]:
                record["task"] = corr["task"]

            run_dir_str = corr["run_dir"]
            if run_dir_str:
                log_path = Path(run_dir_str) / "gateway_spend.jsonl"
            else:
                log_path = _today_log()

            with log_path.open("a") as f:
                f.write(json.dumps(record, separators=(",", ":")) + "\n")
        except Exception:
            # Never let a logging failure propagate to the proxy request handler.
            pass


# LiteLLM resolves the litellm_settings.callbacks string by importing the named
# object and using it AS-IS — it does not instantiate a class. Point the config
# at this module-level INSTANCE (not the SpendCallback class), or every hook is
# invoked unbound and fails with "missing 1 required positional argument: 'self'".
spend_callback_instance = SpendCallback()
