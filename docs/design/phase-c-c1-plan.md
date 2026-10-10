# Phase C — C1 build plan: ready-to-merge PR stack

**Author:** `platform-planner` · **Date:** 2026-05-31 · **Status:** plan, for `platform-coder` to execute
**Source design (the spec):** [`phase-c-proposal.md`](phase-c-proposal.md), approved as-is by the operator (substrate ARCH-102, 2026-05-31). Do not re-litigate; this plan operationalizes it.
**Scope:** C1 only — gateway + routing + real cost + one well-netted agent moved. C2 (scorecard, Slack digest, perf-gate automation, per-model burn rate) is out.

---

## Summary (the PR stack at a glance)

Four PRs in `agent-platform`, recommended serial order (deps below). Each lands ready-to-merge but unmerged (ARCH-020/100); the operator merges in order. The full gate (`pytest` + `/bin/bash tests/bash/run.sh` + `/bin/bash tests/bash/lint.sh`) is green before each PR opens.

| # | Title | Touches | Depends on |
|---|---|---|---|
| **PR-1** | Gateway config + launchd plist + secret-guard test | `gateway/config.yaml`, `gateway/requirements.txt`, `launchd/com.convivy.llm-gateway.plist`, `tests/test_gateway_config.py` | — |
| **PR-2** | Routing helpers — `MODEL_ALIASES`, `is_gateway_model`, `gateway_env` (pure, no call-site change) | `bin/agentctl` (defs only), `tests/test_agentctl.py`, `tests/test_gateway_routing.py` | — |
| **PR-3** | Cost-merge in `_finalize_run` + `cost_source` schema + telemetry coverage backfill | `bin/agentctl` (`_finalize_run`), `lib/agent-platform/telemetry.py` (`write_cost`, `VALID_BILLING_SOURCES`), `tests/test_agentctl.py`, `tests/test_telemetry.py` (new), `tests/test_budget.py` | — |
| **PR-4** | Wire routing in `_run_session` + honest `billing_source` | `bin/agentctl` (`_run_session`), `tests/test_agentctl.py` | PR-2, PR-3 |

PR-1, PR-2, PR-3 can be reviewed in any order (independent). PR-4 needs the helpers from PR-2 imported and the `cost_source` schema from PR-3 wired, so it merges last.

Bootstrap (the operator's manual steps after PR-1 merges) + the C1 end-to-end proof (knowledge-repo edit + first run observation) are listed in the bootstrap checklist at the end. **`platform-coder` does NOT execute the bootstrap or the knowledge-repo edit.**

---

## PR-1 — Gateway config + launchd plist + secret-guard test

**Goal:** land the tracked, secret-free gateway config so the operator can bootstrap the proxy. No agentctl behavior change.

**Files created:**

1. **`gateway/config.yaml`** — LiteLLM proxy config.
   - `model_list:` mapping three aliases to LiteLLM provider strings:
     - `gpt5` → `openai/gpt-5` — direct OpenAI (standard LiteLLM mapping)
     - `deepseek` → `deepseek/deepseek-chat` — direct DeepSeek (DeepSeek-V3 is the cheap workhorse; coder confirms the exact model id against LiteLLM's current registry — `deepseek-chat` vs `deepseek-v3` — and picks the cheapest stable string. Flag, don't block.)
     - `kimi` → `moonshot/moonshot-v1-32k` — Moonshot direct. If the cheapest Kimi K2 variant is only on OpenRouter at planning time, fall back to `openrouter/moonshotai/kimi-k2`; flag the choice.
   - API keys referenced as `os.environ/OPENAI_API_KEY`, `os.environ/DEEPSEEK_API_KEY`, `os.environ/MOONSHOT_API_KEY` — literals **must not** appear.
   - `general_settings:` master-key reference (`os.environ/LITELLM_MASTER_KEY`), `database_url` unset (no Postgres dep for C1; SQLite/JSONL spend log only).
   - `litellm_settings:` custom callback registered (see callback below).
   - Bind: `host: 127.0.0.1`, `port: 4000` `[recommended; coder picks any free localhost port; record it]`.

2. **`gateway/spend_callback.py`** — LiteLLM custom callback. Receives per-request metadata `{AGENTCTL_RUN_ID, AGENTCTL_RUN_DIR}`; on each completed call, appends one JSONL line to `<AGENTCTL_RUN_DIR>/gateway_spend.jsonl` with `{ts, model, tokens_in, tokens_out, usd, request_id}`. Uses LiteLLM's computed real-provider cost. Fail-closed: if the run dir doesn't exist (request from PA seam, not agentctl), write to a fallback `~/Convivy/agent-runs/gateway-spend/YYYY-MM-DD.jsonl` so PA spend is never silently dropped.

3. **`gateway/requirements.txt`** — pinned `litellm[proxy]==<version>` + any callback deps. **This file governs the isolated `~/.local/venv-llm-gateway` venv only.** It is NOT read by the repo's pytest, CI, or `install.sh`. The platform `requirements*.txt` and `pyproject.toml` are **deliberately not touched** (a high-risk trigger per ARCH conventions; the architect's design hinges on this isolation).

4. **`launchd/com.convivy.llm-gateway.plist.example`** (tracked) + the live `launchd/com.convivy.llm-gateway.plist` (**gitignored, not tracked**) — the operator's call 2026-05-31, overriding the planner's track-the-live-file proposal. Reasoning: the other `launchd/*.plist` files are tracked because they are **secret-free**; this is the first plist carrying provider keys, so the convention does not apply. Track a placeholder-only `.example` (recovery is `cp .example → .plist`, fill, bootstrap — see the bootstrap checklist); gitignore the live file so a populated plist can never be committed. The secret-guard test runs against the `.example`. Add `launchd/com.convivy.llm-gateway.plist` to `.gitignore` (the live file only; the `.example` stays tracked). Both files share this shape:
   - `Label`: `com.convivy.llm-gateway`
   - `ProgramArguments`: **no shell** — direct exec of `$HOME/.local/venv-llm-gateway/bin/litellm` with `--config $HOME/Convivy/agent-platform/gateway/config.yaml --port 4000 --host 127.0.0.1`. Sidesteps the bash-3.2 floor (ARCH-082) by construction — no shell anywhere on the path.
   - `RunAtLoad`: `true`, `KeepAlive`: `true`.
   - `EnvironmentVariables`: `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, `MOONSHOT_API_KEY`, `LITELLM_MASTER_KEY` — in the tracked `.example` these are empty-string placeholders, NOT secrets. The operator's bootstrap copies the `.example` to the live (gitignored) plist and populates them from Keychain. The secret-guard test enforces placeholders-only on the tracked `.example`.
   - `StandardOutPath`/`StandardErrorPath`: `~/Convivy/agent-runs/llm-gateway.{out,err}.log`.

5. **`tests/test_gateway_config.py`** (new pytest module):
   - `test_config_yaml_has_no_secret_literals`: parse `gateway/config.yaml`, walk every string value, assert none match common API-key shapes (`re.search(r"sk-[A-Za-z0-9]{16,}|sk-proj-|sk-ant-|gsk_|...", value)` — exact regex set chosen from observed key prefixes for OpenAI / Anthropic / DeepSeek / Moonshot). Also assert every `api_key:` field starts with `os.environ/`.
   - `test_plist_example_has_no_secret_literals`: parse the tracked `launchd/com.convivy.llm-gateway.plist.example`, walk `EnvironmentVariables`, assert every value is empty-string or a clear placeholder, never a secret-shaped literal.
   - `test_live_plist_is_gitignored`: assert `launchd/com.convivy.llm-gateway.plist` (the live, populated file) is matched by `.gitignore` and is not tracked. Defends the "no populated plist in git" guarantee.
   - `test_plist_example_program_arguments_is_no_shell`: assert `ProgramArguments[0]` in the `.example` is the venv `litellm` binary path, NOT `/bin/bash`/`/bin/sh`/`zsh`. (Defends against a future drift back to a shell wrapper that would re-introduce the bash-3.2 risk.)

**Acceptance:**
- `pytest tests/test_gateway_config.py` passes.
- `/bin/bash tests/bash/lint.sh` passes (no new bash files; lint is no-op for the plist).
- `git grep -E 'sk-[A-Za-z0-9]{16,}|sk-ant-|gsk_'` returns nothing in tracked files.
- `plutil -lint launchd/com.convivy.llm-gateway.plist.example` exits 0 (coder runs locally; document the check). The live `.plist` is gitignored and not created by the coder.
- `.gitignore` carries an entry for `launchd/com.convivy.llm-gateway.plist` (live file only; the `.example` stays tracked).

**No agentctl change. No behavior change. Anthropic agents unaffected by construction.**

---

## PR-2 — Routing helpers (pure, no call-site change)

**Goal:** land the pure helpers + their unit tests as a self-contained, behavior-free PR. This is the highest-confidence piece per the proposal's test plan; isolating it makes the routing predicate provably correct before PR-4 wires it in.

**`bin/agentctl` edits:**

1. **Extend `MODEL_ALIASES`** (currently `bin/agentctl:43`):
   ```python
   MODEL_ALIASES = {
       "opus":   "claude-opus-4-7",
       "sonnet": "claude-sonnet-4-6",
       "haiku":  "claude-haiku-4-5",
       # gateway-served (Phase C1; routed via ANTHROPIC_BASE_URL when used)
       "gpt5":     "openai/gpt-5",
       "deepseek": "deepseek/deepseek-chat",
       "kimi":     "moonshot/moonshot-v1-32k",
   }
   ```
   Coder picks exact provider strings per PR-1 notes and keeps them in lockstep with `gateway/config.yaml` (a single test asserts the alias values match the `model_list` keys in the YAML, so drift fails the build).

2. **`GATEWAY_MODEL_PREFIXES`** — a new module-level constant, an explicit allowlist of provider prefixes the gateway serves:
   ```python
   GATEWAY_MODEL_PREFIXES = ("openai/", "deepseek/", "moonshot/", "openrouter/")
   ```
   The "openrouter/" entry is included for the fallback path noted in PR-1 (if Kimi K2 ends up via OpenRouter). Adding a new gateway provider in C2/later is one tuple entry + one YAML row.

3. **`is_gateway_model(model_id_str: str) -> bool`** — new pure helper. Returns `True` iff `model_id_str` starts with any prefix in `GATEWAY_MODEL_PREFIXES`. Anything else (Anthropic literals like `claude-opus-4-7`, unknown literals passed through `model_id()`) returns `False`. **Fails closed to direct** — this is the safety property the proposal calls out (unknown alias → today's Anthropic-direct behavior, never silently routed to a proxy that lacks it).

4. **`gateway_env(model: str, base_env: dict, *, virtual_key: str, gateway_url: str) -> dict`** — new pure helper. If `is_gateway_model(model)`: return `{**base_env, "ANTHROPIC_BASE_URL": gateway_url, "ANTHROPIC_API_KEY": virtual_key}`. Else: return `base_env` unchanged (the **byte-identical** Anthropic guarantee — a dict-equality test pins this).

5. **`_gateway_url() -> str`** and **`_gateway_virtual_key() -> str`** — small helpers that resolve the gateway URL (constant `http://127.0.0.1:4000`, matched to PR-1's port) and the virtual key (from Keychain via `security find-generic-password -w -s litellm-agentctl-virtual-key`). The Keychain lookup is wrapped so it can be monkey-patched in tests; tests use a fake. **Not wired into `_run_session` in this PR** — only the helpers and their unit tests land here.

**Tests (`tests/test_agentctl.py` extension + new `tests/test_gateway_routing.py`):**

- Extend `test_model_id_aliases_and_passthrough` to cover the three new aliases (`gpt5`, `deepseek`, `kimi`) resolve to their `provider/model` strings; unknown literals still pass through.
- `test_is_gateway_model_anthropic_returns_false` — every Anthropic model_id (literal + via `MODEL_ALIASES`) returns `False`.
- `test_is_gateway_model_gateway_aliases_return_true` — each of `gpt5`/`deepseek`/`kimi` resolved through `model_id()` returns `True`.
- `test_is_gateway_model_unknown_literal_fails_closed_to_direct` — `is_gateway_model("some-random-model-id")` returns `False`. **This is the load-bearing safety test.**
- `test_gateway_env_anthropic_is_byte_identical` — `gateway_env("claude-opus-4-7", {"FOO": "bar"})` returns exactly `{"FOO": "bar"}` — dict-equal, no extra keys. The proposal's "Anthropic path unchanged" guarantee, pinned mechanically.
- `test_gateway_env_gateway_model_adds_base_url_and_key` — `gateway_env("openai/gpt-5", {"FOO": "bar"}, virtual_key="vk", gateway_url="http://127.0.0.1:4000")` returns `{"FOO": "bar", "ANTHROPIC_BASE_URL": "http://127.0.0.1:4000", "ANTHROPIC_API_KEY": "vk"}`.
- `test_gateway_aliases_match_yaml_model_list` — parse `gateway/config.yaml` and assert each `MODEL_ALIASES` gateway value matches a `model_list[*].model_name` (or `litellm_params.model`) row, so a coder later editing one without the other fails the gate.

**Acceptance:**
- Full gate green (`pytest`, `/bin/bash tests/bash/run.sh`, `/bin/bash tests/bash/lint.sh`).
- `grep -n "is_gateway_model\|gateway_env" bin/agentctl` finds the new defs but no call sites (proof of "no behavior change" until PR-4).

---

## PR-3 — Cost-merge in `_finalize_run` + `cost_source` schema + telemetry coverage backfill

**Goal:** make `cost.json` honest for gateway runs before any routing wires in. The Anthropic branch stays byte-identical. This PR also backfills the `telemetry.write_cost` coverage gap the cost-merge edit obliges (per `tests/README.md`'s "NOT yet covered" list).

**`lib/agent-platform/telemetry.py` edits:**

1. **`VALID_BILLING_SOURCES`** (line 34) — add per-provider entries:
   ```python
   VALID_BILLING_SOURCES = {
       "subscription_credit", "api_fallback", "interactive", "unknown",
       "provider_api:openai", "provider_api:deepseek", "provider_api:moonshot",
   }
   ```
   Strict exact-match (option chosen over a `provider_api:` prefix check) — adding a new provider in C2/later requires one line here, which catches typos and forces an explicit decision. If the C1 fallback to `openrouter/` for Kimi materializes, add `"provider_api:openrouter"` too — coder reconciles with PR-1's final alias roster.

2. **`write_cost`** (line 150) — accept a new keyword arg `cost_source: str`, default `"anthropic_cli"` for backward compat. Add it to the top-level payload:
   ```python
   def write_cost(d: Path, *, by_model: dict[str, dict[str, Any]],
                  cost_source: str = "anthropic_cli") -> None:
       ...
       payload = {
           "tokens_in": tokens_in,
           "tokens_out": tokens_out,
           "usd": usd,
           "cost_source": cost_source,
           "by_model": by_model,
       }
   ```
   Validation: `cost_source in {"anthropic_cli", "gateway_spend_log", "estimate_fallback"}`. Top-level (not per-row in `by_model`) because each run is one model — top-level is equivalent and simpler. `budget.py` reads `usd` and is **unaffected**.

**`bin/agentctl` edits (`_finalize_run`, line 761):**

After computing `model_used` (line 768), branch:

```python
if is_gateway_model(model_used):
    spend_log = run_dir / "gateway_spend.jsonl"
    real_usd, has_record = _read_gateway_spend(spend_log)  # new helper
    if has_record:
        by_model[model_used] = {
            "tokens_in": tokens_in,
            "tokens_out": tokens_out_from_log,  # prefer log over CLI event for gateway
            "usd": real_usd,
        }
        cost_source = "gateway_spend_log"
    else:
        # Fail-closed: do NOT write usd: 0. Fall back to the in-flight estimate
        # for this run's elapsed seconds and flag the gap.
        elapsed_min = _elapsed_minutes(run_dir)
        by_model[model_used] = {
            "tokens_in": tokens_in,
            "tokens_out": int(usage.get("output_tokens", 0) or 0),
            "usd": round(elapsed_min * 0.15, 4),  # the same $0.15/min as inflight
        }
        cost_source = "estimate_fallback"
else:
    # Anthropic branch — byte-identical to today.
    by_model[model_used] = {
        "tokens_in": tokens_in,
        "tokens_out": int(usage.get("output_tokens", 0) or 0),
        "usd": float(final_event.get("total_cost_usd", 0.0) or 0.0),
    }
    cost_source = "anthropic_cli"
T.write_cost(run_dir, by_model=by_model, cost_source=cost_source)
```

New helpers (pure, unit-testable):
- `_read_gateway_spend(path) -> (usd: float, has_record: bool)` — reads JSONL, sums `usd`, returns `(0.0, False)` if the file is missing or empty.
- `_elapsed_minutes(run_dir) -> float` — reads `meta.json`'s `started_at`/`ended_at` (`ended_at` is freshly written by `update_meta` immediately above the cost block).

**Tests:**

- New module `tests/test_telemetry.py` (backfills `telemetry.py`'s previously-uncovered validation surface, per `tests/README.md` — test-as-touched on a path the cost-merge now obliges):
  - `test_write_cost_default_cost_source_anthropic_cli` — calling `write_cost(d, by_model=...)` without `cost_source` writes `"anthropic_cli"`.
  - `test_write_cost_gateway_spend_log` — writes the `gateway_spend_log` value through.
  - `test_write_cost_estimate_fallback` — writes the `estimate_fallback` value through.
  - `test_write_cost_rejects_invalid_cost_source` — raises on a bogus value.
  - `test_write_meta_accepts_provider_api_billing_sources` — each new `provider_api:*` value passes validation; an unknown one raises.

- Extend `tests/test_agentctl.py`:
  - `test_finalize_anthropic_branch_byte_identical` — given a `final_event` with `model: claude-opus-4-7` and `total_cost_usd: 0.42`, the written `cost.json` has `usd: 0.42`, `cost_source: "anthropic_cli"`. (Pins the no-regression guarantee — the proposal's load-bearing claim.)
  - `test_finalize_gateway_sources_from_spend_log` — fixture: a `gateway_spend.jsonl` with two rows summing to `$0.0023`, `final_event` with `model: deepseek/deepseek-chat` and a (wrong) `total_cost_usd: 0.42`. Asserts `cost.json.usd == 0.0023`, `cost_source: "gateway_spend_log"` — the CLI's Anthropic-priced figure is **not** used.
  - `test_finalize_gateway_no_record_writes_estimate_fallback` — gateway model, no `gateway_spend.jsonl` file. Asserts `usd` is the in-flight estimate (positive, NOT zero), `cost_source: "estimate_fallback"`. **This is the proposal's "never write `usd: 0`" guarantee, pinned mechanically.**
  - `test_read_gateway_spend_handles_missing_and_empty_files` — `(0.0, False)` for both cases.

- Extend `tests/test_budget.py`:
  - `test_today_total_usd_inherits_real_dollars_from_gateway_cost_json` — fixture: a run dir with `cost.json` `{usd: 0.0023, cost_source: "gateway_spend_log"}`. Assert `budget.today_total_usd()` sums it correctly. The `live_total_usd` test extends naturally.
  - `test_budget_py_source_unchanged` — `hashlib.sha256(Path("lib/agent-platform/budget.py").read_bytes())` matches a checksum recorded in the test, OR (simpler) `git diff --stat HEAD~1 -- lib/agent-platform/budget.py` is empty in CI (the bash test harness can do the latter). Pick the simpler: the test asserts no diff vs main on `budget.py` within this PR's commit range. Coder picks the cleanest mechanism. **The point: "`budget.py` is not changing" is a hard guarantee in this plan, and it must be a test, not a comment.**

**Acceptance:**
- Full gate green.
- `git diff main -- lib/agent-platform/budget.py` is empty after the merge.
- A test failure on `test_finalize_gateway_no_record_writes_estimate_fallback` would block the PR (the fail-closed cost path is a **mandatory** acceptance criterion per the brief — not optional).

---

## PR-4 — Wire routing in `_run_session` + honest `billing_source`

**Goal:** the final wire-in. With PR-2's helpers and PR-3's cost-merge already merged, this is the smallest possible behavioral change: two call sites.

**`bin/agentctl` edits (`_run_session`, line 396):**

1. **Conditional env injection** — after `model = model_id(agent.get("model_alias"))` (line 406), in the non-dry-run branch (around line 465 where `proc_env` is built):
   ```python
   proc_env = {
       **os.environ,
       "AGENTCTL_RUN_ID": run_id,
       "AGENTCTL_AGENT_NAME": args.agent,
       "AGENTCTL_REPO": repo,
       "AGENTCTL_RUN_DIR": str(run_dir),
   }
   if is_gateway_model(model):
       proc_env = gateway_env(
           model, proc_env,
           virtual_key=_gateway_virtual_key(),
           gateway_url=_gateway_url(),
       )
   ```
   `AGENTCTL_RUN_DIR` is **already** in `proc_env` (line 470 — confirmed). The LiteLLM custom callback (PR-1) reads it from the request metadata to write `<run_dir>/gateway_spend.jsonl`. The plumbing chain is: env var → `claude -p` carries it as a header / metadata field to the proxy → callback writes the per-run spend log. **Coder verifies that `claude -p` propagates `AGENTCTL_RUN_*` env vars to the upstream request as headers / metadata** — if not, the callback falls back to its global per-day log (PR-1) and `_finalize_run` reads it via a run-id index. Surface this if the propagation doesn't work out of the box; do not silently degrade.

2. **Honest `billing_source`** — replace the two hardcoded `billing_source="subscription_credit"` calls (lines 413 and 482) with a derived value:
   ```python
   def _billing_source_for(model: str) -> str:
       if not is_gateway_model(model):
           return "subscription_credit"
       provider = model.split("/", 1)[0]  # "openai" / "deepseek" / "moonshot"
       return f"provider_api:{provider}"
   ```
   Threaded into both `write_meta(...)` calls.

3. **`design-room`** — out of scope for routing (ARCH-079: design-room is premium, human-in-the-loop creative). The proposal's "no foreign-model panelist" stance is honored — leave `bin/design-room` untouched.

**Tests (`tests/test_agentctl.py` extension):**

- `test_billing_source_for_anthropic` — Anthropic model returns `"subscription_credit"`.
- `test_billing_source_for_gateway_models` — each gateway alias resolved through `model_id()` returns the right `provider_api:<provider>` string.
- `test_billing_source_unknown_literal_returns_subscription_credit` — unknown literal (which `is_gateway_model` rejects) returns `"subscription_credit"`, NOT a `provider_api:` value. Pins the fail-closed guarantee end-to-end.
- A small "env construction" integration test that simulates `_run_session`'s `proc_env` build for two cases (Anthropic agent, gateway agent), asserts byte-identical for Anthropic and base-URL + key for gateway. (The unit helpers are already covered in PR-2; this test exists to pin the **call site**, not the helpers.)

**Acceptance:**
- Full gate green.
- `grep -n 'billing_source="subscription_credit"' bin/agentctl` finds zero literal hardcodes (only the helper).
- A dry-run spawn of an Anthropic agent shows `meta.json.billing_source: "subscription_credit"` (unchanged from today).
- No `AGENTCTL_RUN_DIR` regression — the env var is still propagated to all spawns (existing tests cover this; verify still green).

---

## The C1 end-to-end proof (the operator's checklist, not a coder PR)

After PR-1..PR-4 are merged and the gateway is bootstrapped, the C1 proof is:

1. **Flip `bug-fixer`'s frontmatter** — in `~/Convivy/knowledge/projects/agent-platform/agents/bug-fixer.md`, change `model: sonnet` to `model: deepseek` (the cheap gateway alias). **This edit is in the `knowledge` repo, not `agent-platform`** — out of `platform-coder`'s writable scope. The operator does this edit (one-line) and commits to `knowledge`. The knowledge-watcher / index refreshes; the next dispatched `bug-fixer` resolves `model: deepseek` and routes via the gateway.
2. **Dispatch one bug-fixer run** — through normal daytime dispatch (or `agentctl spawn bug-fixer --task "..."`).
3. **Observe** — in the run dir:
   - `meta.json.model`: `deepseek/deepseek-chat`
   - `meta.json.billing_source`: `provider_api:deepseek`
   - `cost.json.usd`: real DeepSeek dollars (small, e.g. `$0.001-$0.02`), **not** an Anthropic-priced figure
   - `cost.json.cost_source`: `gateway_spend_log`
   - `gateway_spend.jsonl`: at least one row with the real cost
4. **Verify review + CI net intact** — the bug-fixer's PR gets the standard ARCH-068 review + CI passes; if either falls below baseline, revert the frontmatter (one-line, instant).

This proves the whole loop: routing → real cost → honest accounting → budget caps see the truth → review/CI net catches a model regression.

---

## The operator's bootstrap checklist (manual steps, NOT agent actions)

After PR-1 merges (before PR-4 can be useful):

- [ ] **Create the venv:** `python3.11 -m venv ~/.local/venv-llm-gateway && ~/.local/venv-llm-gateway/bin/pip install -r ~/Convivy/agent-platform/gateway/requirements.txt`.
- [ ] **Populate Keychain entries** — six items:
  - `OPENAI_API_KEY` (service: `openai-api-key`)
  - `DEEPSEEK_API_KEY` (service: `deepseek-api-key`)
  - `MOONSHOT_API_KEY` (service: `moonshot-api-key`)
  - `LITELLM_MASTER_KEY` (service: `litellm-master-key`) — generate a random secret; the proxy's admin key
  - `litellm-agentctl-virtual-key` — generate via LiteLLM admin UI / CLI after the proxy is up; agentctl reads this
  - `litellm-pa-virtual-key` — same, separate virtual key for the PA `llm/` seam (handed off to the `pa-*` crew via the published contract; not used by agentctl)
- [ ] **Create the live plist from the template:** `cp ~/Convivy/agent-platform/launchd/com.convivy.llm-gateway.plist.example ~/Convivy/agent-platform/launchd/com.convivy.llm-gateway.plist`. The live file is gitignored — it never gets committed.
- [ ] **Populate plist EnvironmentVariables** — in the live (copied) plist, for each `*_API_KEY` and `LITELLM_MASTER_KEY`, write the Keychain value into the `EnvironmentVariables` dict, OR use a launchd-side shim that exports from Keychain before exec. Secrets are local-only.
- [ ] **Bootstrap the launchd job:** `launchctl bootstrap gui/$(id -u) ~/Convivy/agent-platform/launchd/com.convivy.llm-gateway.plist`. Verify with `launchctl print gui/$(id -u)/com.convivy.llm-gateway` and a `curl -s http://127.0.0.1:4000/v1/models -H "Authorization: Bearer <virtual-key>"`.
- [ ] **Health check** — confirm `127.0.0.1:4000` is reachable from localhost only; `nc -zv <external-ip> 4000` from another box should fail (the bind is loopback-only).

After PR-2..PR-4 are all merged AND the gateway is healthy:

- [ ] **Flip `bug-fixer`'s `model:` frontmatter** in the `knowledge` repo (see "C1 end-to-end proof" above). One-line edit, one commit, one push.
- [ ] **Dispatch and observe** the first run as described.

If anything fails, revert the frontmatter — that's the one-line rollback that takes bug-fixer back to `sonnet` instantly. The PRs themselves are non-rollback-required because all four are dormant for Anthropic agents until an agent's frontmatter names a gateway alias.

---

## Mandatory acceptance criteria (the gate, summarized)

These are non-optional — the brief and the proposal call them out as hard guarantees, mirrored here so the coder reads them and a reviewer can check them off:

1. **Fail-closed cost path** — `_finalize_run` for a gateway run with no spend record writes `cost_source: "estimate_fallback"` and a positive `usd`, **never `usd: 0`**. Pinned by `test_finalize_gateway_no_record_writes_estimate_fallback`. This is the ARCH-065/069 lesson made testable.
2. **Anthropic path byte-identical** — `gateway_env("claude-...", env)` returns `env` unchanged; `_finalize_run` Anthropic branch sources `usd` from `total_cost_usd` exactly as today; `cost.json.cost_source` for an Anthropic run is `"anthropic_cli"`. Pinned by `test_gateway_env_anthropic_is_byte_identical` and `test_finalize_anthropic_branch_byte_identical`.
3. **`budget.py` unchanged** — pinned by a test that asserts no diff vs main on `lib/agent-platform/budget.py`. Cost-truth lands upstream in `_finalize_run`; the budget module inherits real dollars from `cost.json` without an edit.
4. **`requirements*.txt` / `pyproject.toml` unchanged** — pinned by a test that asserts no diff vs main on those files. LiteLLM lives in the isolated `~/.local/venv-llm-gateway` venv only.
5. **Fail-closed routing** — `is_gateway_model("unknown-literal")` returns `False`; routing for an unknown literal goes to Anthropic-direct (today's behavior), never silently to the proxy. Pinned by `test_is_gateway_model_unknown_literal_fails_closed_to_direct`.
6. **No autonomous merge** — every PR lands ready-to-merge but unmerged (ARCH-020/100; the PreToolUse hook enforces this regardless).
7. **No bash-3.2 violations** — the plist's `ProgramArguments` is a no-shell direct exec; `/bin/bash tests/bash/lint.sh` green. No new bash files.
8. **No secret literals in tracked files** — pinned by `test_gateway_config.py`'s grep tests, and by the existing `git grep` pre-commit habit.

---

## What this plan deliberately does NOT do (honoring the proposal's "not changing" list)

- **`lib/agent-platform/budget.py`** — not touched. Cost-truth fix lands upstream in `_finalize_run` so the caps inherit real dollars from `cost.json`. The per-model burn rate is C2; the flat `$0.15/min` over-estimates a cheap mid-flight run conservatively, which is fine for C1.
- **`requirements*.txt` / `pyproject.toml` / `install.sh`** — not touched. The gateway venv is fully isolated. If anything tempts an edit to `install.sh`, the coder **halts and surfaces** rather than proceeding.
- **The reaper (`_reap_lingering`, ARCH-069), stream loop (`_stream_transcript`), concurrency cap (`registry.py`, ARCH-006), worktree machinery (`worktrees.py`), profile activation (`activate_profile`)** — none are on the routing path. Zero edits.
- **`design-room`** — premium per ARCH-079. The routing helper is generic enough that a future C2/C3 edit could apply it there, but C1 does not.
- **`launch-overnight` and the overnight schedule** — not touched. No premium-roster model moves; only `bug-fixer` flips, and only after the gateway is proven by the C1 dispatch + observation loop.
- **Anthropic agents and their dispatch** — provably unaffected: their `proc_env` is dict-equal to today, their `_finalize_run` branch is the existing code, their `meta.json.billing_source` stays `subscription_credit`.

---

## Published gateway contract (the PA `llm/` seam, ARCH-101 boundary)

C1 publishes the contract; the `pa-*` crew implements its consumer in the personal-assistant repo. **`platform-coder` does NOT touch the PA repo** — that boundary is hard.

- **URL:** `http://127.0.0.1:4000/v1/messages` (LiteLLM proxy, Anthropic Messages format). The proxy is loopback-only; the PA app runs on the same mini.
- **Auth:** `Authorization: Bearer <virtual-key>` (or `ANTHROPIC_API_KEY: <virtual-key>` if the SDK sets that header from the env var). The PA seam gets `litellm-pa-virtual-key` (its own Keychain entry), distinct from `litellm-agentctl-virtual-key`. Distinct virtual keys give per-consumer LiteLLM budget caps and per-consumer attribution in the spend log.
- **Model namespace (C1 roster):** `openai/gpt-5`, `deepseek/deepseek-chat`, `moonshot/moonshot-v1-32k` (subject to coder's PR-1 confirmation against LiteLLM's current registry; published namespace updates with the YAML).
- **Cost observability for PA-originated calls:** the spend callback's fallback writes to `~/Convivy/agent-runs/gateway-spend/YYYY-MM-DD.jsonl` when no `AGENTCTL_RUN_DIR` is in the request metadata, so PA spend is observable to the operator without being silently dropped. The PA crew's accounting is their concern; the substrate only guarantees the log exists.

This is the entire substrate-side contract. Stop at the repo boundary.

---

## High-risk halt-and-surface (mirrored from the proposal for the coder)

If any of these triggers fire during build, `platform-coder` **halts and surfaces** instead of proceeding:

- **Anything wanting the gateway in the overnight critical path before it's proven.** `launch-overnight` is unchanged in C1.
- **Any case where a gateway run could finalize with `usd: 0`** instead of `estimate_fallback` — the fail-closed branch is mandatory.
- **Any code path that writes a key to a log, transcript, or tracked file.** Secret-shape grep is in CI; the proxy callback must scrub or never log raw `api_key` values.
- **Any edit that touches `requirements*.txt` / `pyproject.toml`** — the isolation is the architecture, not a convenience.
- **Any edit to `install.sh`** — the gateway install is the venv, not the symlink installer.
- **Any edit that wants the `bug-fixer` frontmatter changed inside this PR stack** — that lives in the `knowledge` repo, the operator does it.

---

## C2 follow-on (explicitly out of this plan)

For traceability, the C2 items the proposal identifies and this C1 plan deliberately defers:

- The scorecard harvester (`bin/model-scorecard` candidate) aggregating per-(agent, model) review-pass / CI-first-pass / rework / real $/run.
- The weekly Slack digest of the scorecard.
- Perf-gate automation + the threshold number (the operator's lock #3: pin the number in C2 when there's data to measure against, not now).
- Per-model burn rate replacing the flat `$0.15/min` in-flight estimate.
- Broadening the cheap-model lane beyond `bug-fixer` (`coder`, `refactorer`, `test-writer`, reviewers — one at a time, behind the gate).

C2 is its own brief.

---

## Open questions for `platform-coder` to surface (don't block on these)

1. **Exact LiteLLM model strings** for DeepSeek (`deepseek-chat` vs `deepseek-v3`) and Kimi (Moonshot direct vs OpenRouter fallback) — coder picks the cheapest stable mapping per LiteLLM's current registry and flags the choice in the PR-1 description.
2. **`claude -p` env-var → upstream-request propagation** — does the headless CLI forward `AGENTCTL_RUN_DIR` as a header / metadata field automatically, or does the LiteLLM callback need to fall back to the global per-day log + a run-id index? Coder verifies during PR-1 build; if the propagation is missing, PR-3's `_read_gateway_spend` is widened to read from the global log filtered by run-id. Not a blocker — both paths are designable.
3. **LiteLLM virtual-key creation flow** — admin UI vs CLI command. Coder documents the exact command in PR-1's plist or a comment header so the operator's bootstrap step is unambiguous.

---

**End of C1 build plan.** Hand to `platform-coder`.
