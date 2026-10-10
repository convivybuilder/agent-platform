"""Tests for gateway/spend_callback.py (Phase C C1 follow-up, 2026-06-01).

Covers:
  - _correlation_from_kwargs extracts run_id/agent/task/run_dir from headers
  - SpendCallback._record writes to per-run log when x-agentctl-run-dir is present
  - SpendCallback._record writes to global fallback when no run_dir in headers
  - SpendCallback._record includes correlation fields in the row
  - SpendCallback._record omits optional fields when headers are absent
  - _correlation_from_kwargs handles missing litellm_params/metadata/headers gracefully
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CALLBACK_PATH = REPO / "gateway" / "spend_callback.py"


@pytest.fixture(scope="module")
def spend_callback_mod():
    spec = importlib.util.spec_from_file_location("spend_callback", CALLBACK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_pinned_gateway_copy_imports_its_resolver(tmp_path):
    """The gateway runs from the copy install.sh makes into ~/.local/lib/llm-gateway,
    with that dir as its only PYTHONPATH entry, never from this checkout (ARCH-116).
    So run install.sh's OWN phase-3 copy loop into a scratch dir and import the
    callback from there alone. A copy list that drops a module the callback needs
    fails here rather than at the next gateway restart (security review, PR 538:
    a repo-relative import named nothing in the pinned copy)."""
    text = (REPO / "install.sh").read_text()
    start = text.index('for src in "$REPO"/gateway/*.py')
    loop = text[start:text.index("\ndone", start) + len("\ndone")]
    pinned = tmp_path / "llm-gateway"
    pinned.mkdir()
    subprocess.run(["bash", "-c", f'REPO="{REPO}"; GATEWAY_LIB="{pinned}"\n{loop}'],
                   check=True)
    data = tmp_path / "data"
    got = subprocess.run(
        [sys.executable, "-c", "import spend_callback as s; print(s.GATEWAY_SPEND_ROOT)"],
        cwd=str(tmp_path), capture_output=True, text=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(pinned),
             "BUILDER_ROOT": str(data), "HOME": str(tmp_path)})
    assert got.returncode == 0, got.stderr
    assert got.stdout.strip() == str(data / "agent-runs" / "gateway-spend")


class _MockUsage:
    def __init__(self, prompt_tokens=10, completion_tokens=5):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _PromptTokensDetails:
    def __init__(self, cached_tokens):
        self.cached_tokens = cached_tokens


class _MockUsageCached(_MockUsage):
    """Usage with the LiteLLM-normalized cache-read field (OpenAI shape)."""
    def __init__(self, prompt_tokens=1000, completion_tokens=50, cached_tokens=900):
        super().__init__(prompt_tokens, completion_tokens)
        self.prompt_tokens_details = _PromptTokensDetails(cached_tokens)


class _MockUsageDeepSeekRaw(_MockUsage):
    """Usage with DeepSeek's raw cache fields passed through by LiteLLM."""
    def __init__(self, prompt_tokens=1000, completion_tokens=50,
                 prompt_cache_hit_tokens=900, prompt_cache_miss_tokens=100):
        super().__init__(prompt_tokens, completion_tokens)
        self.prompt_cache_hit_tokens = prompt_cache_hit_tokens
        self.prompt_cache_miss_tokens = prompt_cache_miss_tokens


class _MockResponse:
    def __init__(self, request_id="req-test-1", usage=None):
        self.id = request_id
        self.usage = usage or _MockUsage()


def _make_kwargs(*, run_dir="", run_id="", agent="", task="", cost=0.001):
    headers = {}
    if run_dir:
        headers["x-agentctl-run-dir"] = run_dir
    if run_id:
        headers["x-agentctl-run-id"] = run_id
    if agent:
        headers["x-agentctl-agent"] = agent
    if task:
        headers["x-agentctl-task"] = task
    return {
        "response_cost": cost,
        "model": "deepseek/deepseek-v4-flash",
        "litellm_params": {"metadata": {"headers": headers}},
    }


# --- _correlation_from_kwargs ---

def test_correlation_full_headers(spend_callback_mod):
    kwargs = _make_kwargs(run_dir="/tmp/run-1", run_id="run-1", agent="coder", task="fix bug")
    corr = spend_callback_mod._correlation_from_kwargs(kwargs)
    assert corr["run_id"] == "run-1"
    assert corr["agent"] == "coder"
    assert corr["task"] == "fix bug"
    assert corr["run_dir"] == "/tmp/run-1"


def test_correlation_no_headers(spend_callback_mod):
    corr = spend_callback_mod._correlation_from_kwargs({})
    assert corr == {"run_id": "", "agent": "", "task": "", "run_dir": ""}


def test_correlation_litellm_metadata_key(spend_callback_mod):
    kwargs = {
        "litellm_params": {
            "litellm_metadata": {
                "headers": {"x-agentctl-run-id": "run-xyz", "x-agentctl-run-dir": "/tmp/r"}
            }
        }
    }
    corr = spend_callback_mod._correlation_from_kwargs(kwargs)
    assert corr["run_id"] == "run-xyz"
    assert corr["run_dir"] == "/tmp/r"


def test_correlation_messages_endpoint_shape(spend_callback_mod):
    """The /v1/messages path (what `claude -p` speaks) puts headers in
    litellm_metadata while a headerless `metadata` dict is also present. The
    extractor must NOT short-circuit on `metadata` — it must find the headers
    in litellm_metadata. Regression for the gateway-cost-correlation bug."""
    kwargs = {
        "litellm_params": {
            "metadata": {"headers": {"host": "127.0.0.1:4000"}},  # present, no agentctl keys
            "litellm_metadata": {
                "headers": {
                    "x-agentctl-run-id": "run-msg",
                    "x-agentctl-agent": "bug-fixer",
                    "x-agentctl-task": "real task",
                    "x-agentctl-run-dir": "/tmp/run-msg",
                }
            },
        }
    }
    corr = spend_callback_mod._correlation_from_kwargs(kwargs)
    assert corr["run_id"] == "run-msg"
    assert corr["agent"] == "bug-fixer"
    assert corr["run_dir"] == "/tmp/run-msg"


def test_correlation_requester_metadata_headers(spend_callback_mod):
    """Headers nested under metadata.requester_metadata.headers are also found."""
    kwargs = {
        "litellm_params": {
            "metadata": {
                "requester_metadata": {
                    "headers": {"x-agentctl-run-id": "rm-1", "x-agentctl-run-dir": "/tmp/rm"}
                }
            }
        }
    }
    corr = spend_callback_mod._correlation_from_kwargs(kwargs)
    assert corr["run_id"] == "rm-1"
    assert corr["run_dir"] == "/tmp/rm"


def test_correlation_missing_litellm_params(spend_callback_mod):
    corr = spend_callback_mod._correlation_from_kwargs({"response_cost": 0.001})
    assert corr["run_id"] == ""
    assert corr["run_dir"] == ""


# --- SpendCallback._record writes to per-run log ---

def test_record_writes_to_per_run_log_when_run_dir_set(spend_callback_mod, tmp_path):
    cb = spend_callback_mod.SpendCallback()
    kwargs = _make_kwargs(run_dir=str(tmp_path), run_id="run-abc", agent="coder", task="fix it")
    cb._record(kwargs, _MockResponse(usage=_MockUsage(100, 50)))

    log = tmp_path / "gateway_spend.jsonl"
    assert log.exists(), "per-run gateway_spend.jsonl must be created"
    row = json.loads(log.read_text().strip())
    assert row["run_id"] == "run-abc"
    assert row["agent"] == "coder"
    assert row["task"] == "fix it"
    assert row["usd"] == pytest.approx(0.001, rel=1e-5)
    assert row["tokens_in"] == 100
    assert row["tokens_out"] == 50


def test_record_writes_to_global_fallback_when_no_run_dir(spend_callback_mod, tmp_path, monkeypatch):
    monkeypatch.setattr(spend_callback_mod, "GATEWAY_SPEND_ROOT", tmp_path / "gateway-spend")
    cb = spend_callback_mod.SpendCallback()
    kwargs = _make_kwargs(run_id="run-no-dir", agent="coder")
    cb._record(kwargs, _MockResponse())

    date_files = list((tmp_path / "gateway-spend").glob("*.jsonl"))
    assert len(date_files) == 1, "global per-day fallback log must be created"
    row = json.loads(date_files[0].read_text().strip())
    assert row.get("run_id") == "run-no-dir"
    assert "run_dir" not in row


def test_record_omits_optional_fields_when_absent(spend_callback_mod, tmp_path):
    cb = spend_callback_mod.SpendCallback()
    kwargs = _make_kwargs(run_dir=str(tmp_path))
    cb._record(kwargs, _MockResponse())

    row = json.loads((tmp_path / "gateway_spend.jsonl").read_text().strip())
    assert "run_id" not in row
    assert "agent" not in row
    assert "task" not in row
    assert "ts" in row
    assert "model" in row
    assert "usd" in row


def test_record_appends_multiple_rows(spend_callback_mod, tmp_path):
    cb = spend_callback_mod.SpendCallback()
    for i in range(3):
        kwargs = _make_kwargs(run_dir=str(tmp_path), run_id=f"run-{i}", cost=0.001 * (i + 1))
        cb._record(kwargs, _MockResponse(request_id=f"req-{i}"))

    rows = [(tmp_path / "gateway_spend.jsonl").read_text().strip().splitlines()]
    assert len(rows[0]) == 3
    parsed = [json.loads(r) for r in rows[0]]
    assert [r["run_id"] for r in parsed] == ["run-0", "run-1", "run-2"]


def test_record_never_raises(spend_callback_mod, tmp_path):
    cb = spend_callback_mod.SpendCallback()
    cb._record({}, None)
    cb._record(None, None)


# --- Cache hit/miss split capture (auditability of real cost) ---

def test_cache_split_from_normalized_details(spend_callback_mod, tmp_path):
    """LiteLLM normalizes cache reads to prompt_tokens_details.cached_tokens;
    misses derive from total_input - reads."""
    cb = spend_callback_mod.SpendCallback()
    kwargs = _make_kwargs(run_dir=str(tmp_path), run_id="run-c")
    cb._record(kwargs, _MockResponse(usage=_MockUsageCached(1000, 50, cached_tokens=900)))

    row = json.loads((tmp_path / "gateway_spend.jsonl").read_text().strip())
    assert row["cache_read_tokens"] == 900
    assert row["cache_miss_tokens"] == 100  # 1000 - 900


def test_cache_split_from_deepseek_raw_fields(spend_callback_mod, tmp_path):
    """DeepSeek's raw prompt_cache_hit_tokens / prompt_cache_miss_tokens are
    captured directly when present."""
    cb = spend_callback_mod.SpendCallback()
    kwargs = _make_kwargs(run_dir=str(tmp_path), run_id="run-d")
    cb._record(kwargs, _MockResponse(
        usage=_MockUsageDeepSeekRaw(prompt_cache_hit_tokens=800, prompt_cache_miss_tokens=200)))

    row = json.loads((tmp_path / "gateway_spend.jsonl").read_text().strip())
    assert row["cache_read_tokens"] == 800
    assert row["cache_miss_tokens"] == 200


def test_cache_fields_omitted_when_no_split_reported(spend_callback_mod, tmp_path):
    """A provider/usage object with no cache info writes no cache fields — old
    rows stay clean, no spurious zeros."""
    cb = spend_callback_mod.SpendCallback()
    kwargs = _make_kwargs(run_dir=str(tmp_path))
    cb._record(kwargs, _MockResponse(usage=_MockUsage(100, 50)))

    row = json.loads((tmp_path / "gateway_spend.jsonl").read_text().strip())
    assert "cache_read_tokens" not in row
    assert "cache_miss_tokens" not in row


def test_cache_tokens_helper_handles_none_usage(spend_callback_mod):
    assert spend_callback_mod._cache_tokens(None, 0) == (None, None)


# --- Cache-split beacon (2026-06-02 DeepSeek V4-flash gap detector) ---

def test_cache_split_beacon_on_large_prompt_without_split(spend_callback_mod, tmp_path):
    """Usage present, prompt large enough that a caching provider should report
    a split, but none came through (the DeepSeek 2026-06-02 transient): stamp an
    explicit 'unreported' beacon so the gap is countable, not silent."""
    cb = spend_callback_mod.SpendCallback()
    kwargs = _make_kwargs(run_dir=str(tmp_path), run_id="run-gap")
    cb._record(kwargs, _MockResponse(usage=_MockUsage(80000, 300)))

    row = json.loads((tmp_path / "gateway_spend.jsonl").read_text().strip())
    assert row.get("cache_split") == "unreported"
    assert "cache_read_tokens" not in row
    assert "cache_miss_tokens" not in row


def test_cache_split_beacon_quiet_on_small_prompt(spend_callback_mod, tmp_path):
    """A tiny prompt with no cache info is unremarkable — no beacon, no noise."""
    cb = spend_callback_mod.SpendCallback()
    kwargs = _make_kwargs(run_dir=str(tmp_path))
    cb._record(kwargs, _MockResponse(usage=_MockUsage(100, 50)))

    row = json.loads((tmp_path / "gateway_spend.jsonl").read_text().strip())
    assert "cache_split" not in row


def test_cache_split_beacon_quiet_when_split_present(spend_callback_mod, tmp_path):
    """When the split IS captured, no beacon — the two are mutually exclusive,
    even on a large prompt."""
    cb = spend_callback_mod.SpendCallback()
    kwargs = _make_kwargs(run_dir=str(tmp_path))
    cb._record(kwargs, _MockResponse(
        usage=_MockUsageCached(80000, 300, cached_tokens=79000)))

    row = json.loads((tmp_path / "gateway_spend.jsonl").read_text().strip())
    assert "cache_split" not in row
    assert row["cache_read_tokens"] == 79000


def test_cache_split_beacon_quiet_when_no_usage(spend_callback_mod, tmp_path):
    """No usage object at all (e.g. health-probe fallback rows) gets no beacon —
    tokens_in is 0, well below the threshold."""
    cb = spend_callback_mod.SpendCallback()

    class _NoUsage:
        id = "req-nousage"
        usage = None

    kwargs = _make_kwargs(run_dir=str(tmp_path))
    cb._record(kwargs, _NoUsage())

    row = json.loads((tmp_path / "gateway_spend.jsonl").read_text().strip())
    assert "cache_split" not in row


# --- Fallback pricing for models LiteLLM can't price (ARCH-207 follow-up) ---
# The 2026-06-20 incident: glm-5.2 billed ~$9.30 at z.ai but response_cost came
# back 0 (model absent from LiteLLM's price map), so every row logged usd:0 and
# real spend was invisible everywhere but z.ai's own dashboard.

def _priced_kwargs(*, model, cost=0.0, model_info=None, run_dir=""):
    headers = {"x-agentctl-run-dir": run_dir} if run_dir else {}
    lp = {"metadata": {"headers": headers}}
    if model_info is not None:
        lp["model_info"] = model_info
    return {"response_cost": cost, "model": model, "litellm_params": lp}


def test_fallback_prices_glm_from_pinned_table(spend_callback_mod, tmp_path):
    cb = spend_callback_mod.SpendCallback()
    kwargs = _priced_kwargs(model="glm-5.2", cost=0.0, run_dir=str(tmp_path))
    # 900 cache reads + 100 cache misses + 50 output
    cb._record(kwargs, _MockResponse(usage=_MockUsageCached(1000, 50, cached_tokens=900)))
    row = json.loads((tmp_path / "gateway_spend.jsonl").read_text().strip())
    # 900*2.6e-7 + 100*1.4e-6 + 50*4.4e-6 = 0.000594
    assert row["usd"] == pytest.approx(0.000594, rel=1e-6)
    assert row["usd"] > 0  # the whole point: not logged as $0


def test_fallback_prices_mimo_from_pinned_table(spend_callback_mod, tmp_path):
    cb = spend_callback_mod.SpendCallback()
    kwargs = _priced_kwargs(model="openai/mimo-v2.5-pro", cost=0.0, run_dir=str(tmp_path))
    # basename lookup (openai/mimo-v2.5-pro -> mimo-v2.5-pro); 900 read/100 miss/50 out
    cb._record(kwargs, _MockResponse(usage=_MockUsageCached(1000, 50, cached_tokens=900)))
    row = json.loads((tmp_path / "gateway_spend.jsonl").read_text().strip())
    # 900*3.6e-9 + 100*4.35e-7 + 50*8.7e-7 = 0.00000324 + 0.0000435 + 0.0000435 = 0.00009024
    # (stored rounded to 6 decimals → 0.00009; abs tolerance absorbs the rounding)
    assert row["usd"] == pytest.approx(0.00009024, abs=1e-6)
    assert row["usd"] > 0


def test_fallback_prices_kimi_code_from_pinned_table(spend_callback_mod, tmp_path):
    cb = spend_callback_mod.SpendCallback()
    kwargs = _priced_kwargs(model="kimi-k2.7-code", cost=0.0, run_dir=str(tmp_path))
    cb._record(kwargs, _MockResponse(usage=_MockUsageCached(1000, 50, cached_tokens=900)))
    row = json.loads((tmp_path / "gateway_spend.jsonl").read_text().strip())
    # 900*1.9e-7 + 100*9.5e-7 + 50*4e-6 = 0.000171 + 0.000095 + 0.0002 = 0.000466
    assert row["usd"] == pytest.approx(0.000466, rel=1e-6)
    assert row["usd"] > 0


def _config_deployments():
    """(model_name, litellm_params.model, model_info-rates-or-None) per config.yaml
    model_list deployment. Pure regex parse — no yaml dep, runs in any venv."""
    import re
    cfg = (REPO / "gateway" / "config.yaml").read_text()
    out = []
    for block in re.split(r"\n  - model_name: ", cfg)[1:]:
        name = block.split("\n", 1)[0].strip()
        lp = re.search(r"model:\s*(\S+)", block)
        rates = {}
        for field, pat in (("in", "input_cost_per_token"),
                           ("cache_read", "cache_read_input_token_cost"),
                           ("out", "output_cost_per_token")):
            m = re.search(rf"{pat}:\s*([0-9.eE-]+)", block)
            if m:
                rates[field] = float(m.group(1))
        out.append((name, lp.group(1) if lp else None, rates or None))
    return out


def test_pinned_table_rates_match_config(spend_callback_mod):
    """Every _PINNED_PRICES entry must mirror the matching config.yaml model_info
    rates exactly — catches drift between the fallback table and the config. Runs
    in any venv (no litellm needed)."""
    table = spend_callback_mod._PINNED_PRICES
    by_basename = {lp.split("/")[-1]: rates
                   for _name, lp, rates in _config_deployments() if lp and rates}
    for key, prices in table.items():
        assert key in by_basename, f"_PINNED_PRICES[{key!r}] has no config.yaml model_info to mirror"
        for field in ("in", "cache_read", "out"):
            assert prices[field] == by_basename[key][field], \
                f"{key} {field} drifted from config.yaml model_info"


def test_pinned_table_covers_every_litellm_unpriced_model(spend_callback_mod):
    """Ground-truth coverage: every config deployment LiteLLM CANNOT price
    (get_model_info raises → response_cost 0) MUST have a _PINNED_PRICES entry,
    else its real spend logs $0 (the 2026-06-20 incident). Uses litellm directly
    (not the config comments, which were verified unreliable). Skips where litellm
    isn't importable (the main test venv); enforced in the gateway venv / any env
    that has it."""
    litellm = pytest.importorskip("litellm")
    table = spend_callback_mod._PINNED_PRICES
    for name, lp_model, _rates in _config_deployments():
        if not lp_model:
            continue
        try:
            litellm.get_model_info(lp_model)
            priced = True
        except Exception:
            priced = False
        if not priced:
            key = lp_model.split("/")[-1]
            assert key in table, (
                f"{name} ({lp_model}) is not priced by LiteLLM but has no "
                f"_PINNED_PRICES entry (key {key!r}) — its real spend would log $0")


def test_fallback_prefers_deployment_model_info(spend_callback_mod, tmp_path):
    # config.yaml model_info on the deployment is the single source of truth and
    # wins over the pinned table — covers any pinned model automatically.
    cb = spend_callback_mod.SpendCallback()
    mi = {"input_cost_per_token": 1e-6, "cache_read_input_token_cost": 1e-7,
          "output_cost_per_token": 2e-6}
    kwargs = _priced_kwargs(model="some-new-model", cost=0.0, model_info=mi, run_dir=str(tmp_path))
    cb._record(kwargs, _MockResponse(usage=_MockUsageCached(1000, 50, cached_tokens=900)))
    row = json.loads((tmp_path / "gateway_spend.jsonl").read_text().strip())
    # 900*1e-7 + 100*1e-6 + 50*2e-6 = 0.00009 + 0.0001 + 0.0001 = 0.00029
    assert row["usd"] == pytest.approx(0.00029, rel=1e-6)


def test_real_response_cost_is_not_overridden(spend_callback_mod, tmp_path):
    # when LiteLLM priced the model (response_cost > 0), keep it — don't recompute.
    cb = spend_callback_mod.SpendCallback()
    kwargs = _priced_kwargs(model="glm-5.2", cost=0.0042, run_dir=str(tmp_path))
    cb._record(kwargs, _MockResponse(usage=_MockUsageCached(1000, 50, cached_tokens=900)))
    row = json.loads((tmp_path / "gateway_spend.jsonl").read_text().strip())
    assert row["usd"] == pytest.approx(0.0042, rel=1e-6)


def test_fallback_zero_for_unknown_unpriced_model(spend_callback_mod, tmp_path):
    # an unpriced model with no table entry and no model_info stays 0 — no crash,
    # no fabricated number.
    cb = spend_callback_mod.SpendCallback()
    kwargs = _priced_kwargs(model="mystery/model-x", cost=0.0, run_dir=str(tmp_path))
    cb._record(kwargs, _MockResponse(usage=_MockUsage(100, 50)))
    row = json.loads((tmp_path / "gateway_spend.jsonl").read_text().strip())
    assert row["usd"] == 0.0


def test_fallback_no_cache_split_treats_all_input_as_miss(spend_callback_mod, tmp_path):
    # no cache info reported -> all input billed at the full (cache-miss) rate.
    cb = spend_callback_mod.SpendCallback()
    kwargs = _priced_kwargs(model="glm-5.2", cost=0.0, run_dir=str(tmp_path))
    cb._record(kwargs, _MockResponse(usage=_MockUsage(100, 50)))
    row = json.loads((tmp_path / "gateway_spend.jsonl").read_text().strip())
    # 100*1.4e-6 + 50*4.4e-6 = 0.00014 + 0.00022 = 0.00036
    assert row["usd"] == pytest.approx(0.00036, rel=1e-6)


# --- Proxy registration regression guard (the class-vs-instance trap) ---
# LiteLLM uses the litellm_settings.callbacks string AS-IS — it does not
# instantiate a class. Pointing it at the SpendCallback class makes every hook
# fire unbound ("missing 1 required positional argument: 'self'"), which only
# surfaces on a live call through the proxy — not in the unit tests above, which
# instantiate SpendCallback() themselves. These two tests fail closed on a
# regression to the class reference.

def test_module_exposes_callback_instance(spend_callback_mod):
    inst = getattr(spend_callback_mod, "spend_callback_instance", None)
    assert inst is not None, "spend_callback.py must expose a module-level instance"
    assert isinstance(inst, spend_callback_mod.SpendCallback), \
        "spend_callback_instance must be an INSTANCE, not the class"


def test_config_callback_points_at_instance():
    config = (REPO / "gateway" / "config.yaml").read_text()
    assert "spend_callback.spend_callback_instance" in config, \
        "config.yaml callbacks must reference the instance"
    # Guard against reverting to the bare class reference.
    import re
    assert not re.search(r"-\s*spend_callback\.SpendCallback\b", config), \
        "config.yaml must not register the SpendCallback class (fires hooks unbound)"
