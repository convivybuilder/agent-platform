# Phase C — multi-LLM runner/gateway: design proposal

**Author:** `platform-architect` · **Date:** 2026-05-31 · **Status:** design, for the operator's approve/redirect
**Program:** multi-LLM model support, Phase C (A = test/CI foundation, ARCH-098; B = platform crew + dispatch target, ARCH-099; C = this).

This is a design, not a build. It proposes structure for `platform-planner` + `platform-coder` to land as a ready-to-merge PR stack. Numbers in `[brackets]` mark decisions where I picked a default and say why; redirect any of them.

---

## Recommendation in one paragraph

Keep `claude -p` as the single runtime for every agent, Anthropic and non-Anthropic alike. Route non-Anthropic agents to their providers by pointing that one process at a **local LiteLLM proxy** through `ANTHROPIC_BASE_URL`, with a model alias the proxy maps to the real provider. Anthropic agents are left byte-identical to today (no base URL, direct to `api.anthropic.com`). The proxy becomes the source of truth for real per-provider dollars; agentctl writes those dollars into the same `cost.json` the budget machinery already reads, so ARCH-065 inherits real-cost accounting with no change to `budget.py`. Performance observability (per-(agent, model) review-pass and CI-first-pass rates) and the tuning loop come in a second phase once at least one agent has run on a cheaper model long enough to score. C1 ships the gateway, routing, and cost; C2 ships the scorecard and the gate.

---

## Current state

**One runner.** Every dispatched agent runs through `agentctl spawn` → `_run_session` (`bin/agentctl:396`), which resolves `model = model_id(agent.get("model_alias"))` (`:406`), builds `cmd = [claude, -p, …, --model, model, --append-system-prompt, body, user_prompt]` (`:436-463`), and `Popen`s it (`:472`) with a `proc_env` carrying the run-id family of vars (`:465-471`). The transcript is drained by `_stream_transcript` (with the ARCH-069 reaper) and `_finalize_run` writes the artifacts. There is one streaming path, one reaper, one finalize. `design-room` (`bin/design-room`) is a second, lighter `claude -p` caller that also passes `--model`.

**Model resolution.** `model_id()` (`:134`) maps three aliases — `opus`→`claude-opus-4-7`, `sonnet`→`claude-sonnet-4-6`, `haiku`→`claude-haiku-4-5` (`MODEL_ALIASES`, `:43`) — and passes any unknown string through as a literal model ID. Per-agent model is the `model:` frontmatter (ARCH-079: architect pinned to the literal `claude-opus-4-8`, the rest on the aliases). No gateway, LiteLLM, OpenAI, or non-Anthropic code exists anywhere in the repo.

**Cost.** `_finalize_run` (`:761`) reads `total_cost_usd` from the CLI's terminal `result` event and writes it as `by_model[model]["usd"]` into `cost.json` (`:765-776`). `budget.py` (ARCH-065) sums that `usd` across today's completed run dirs (`today_total_usd`, `:56`) and adds an in-flight estimate at a flat `$0.15/min` per active run (`inflight_estimate_usd`, `:84`); `live_total_usd` (`:120`) is what the spawn gate and `budget-watchdog` cap on. `total_cost_usd` is computed by the Claude CLI from **its own** Anthropic pricing table.

**The PA consumer.** The personal-assistant app (ARCH-101) routes all model calls through a one-module `llm/` seam, Anthropic-backed in v1, with Phase C swapped in behind the same interface later. The PA repo is a self-contained internal app, deliberately off the Convivy substrate and off product. It is **Phase C's first real consumer** but it is not substrate code; this proposal designs the gateway *contract* the PA seam consumes and stops at the repo boundary.

---

## Design principle and the routing hook

**Principle: one runner, one new moving part.** The expensive, already-hardened machinery is the spawn → stream → reap → finalize pipeline (ARCH-064/065/069/070). Anything that forks it per provider multiplies that surface and re-opens settled incidents. So the runner stays `claude -p` for all models, and the only new component is a gateway that speaks Anthropic Messages inbound and fans out to providers.

Two ways to reach a non-Anthropic model were on the table:

| Option | How | Blast radius | Verdict |
|---|---|---|---|
| **A. Base-URL + gateway** (recommended) | Set `ANTHROPIC_BASE_URL` on the spawn's `proc_env` for non-Anthropic aliases; an Anthropic-compatible proxy translates to the provider | One conditional in env construction; finalize cost-merge. `claude -p`, streaming, reaper, telemetry all unchanged | **Recommend** |
| B. Per-provider runner | agentctl branches to a provider SDK (OpenAI, etc.) for non-Anthropic agents, with its own request/stream/cost code | Re-implements `_stream_transcript`, the ARCH-069 reaper, `_finalize_run`, the meta/cost schema per provider; second telemetry path to keep in sync | Reject — multiplies the incident surface for no gain |

Option A is the "clean hook" the brief names. `claude -p` already honors `ANTHROPIC_BASE_URL`; LiteLLM already exposes an Anthropic-format `/v1/messages` endpoint and maps a model name to any provider. The agent loop, tool-use, streaming, and the reaper do not know or care that bytes went to a proxy. **Anthropic agents never get the env var, so their path is unchanged** — this is what keeps blast radius near zero and satisfies the brief's "Anthropic stays direct."

---

## The gateway

### Tech: LiteLLM proxy `[recommended]`

LiteLLM proxy is the recommended gateway: it natively accepts the Anthropic Messages format inbound (so `claude -p` talks to it unmodified), maps model aliases to providers (OpenAI, Google, Mistral, Together, OpenRouter, local), tracks **real per-request cost** from its own per-provider pricing tables, and gates access with virtual keys. Alternatives considered:

- **A thin custom Anthropic↔OpenAI shim** (a small FastAPI app we own). Rejected for C1: we would own translation fidelity, cost tables, retries, and streaming edge cases that LiteLLM already maintains. Worth reconsidering only if LiteLLM's dependency weight or translation fidelity proves a problem (see Risks).
- **OpenRouter as the gateway** (a hosted multi-provider endpoint). It solves provider fan-out but puts billing and a third party in the hot path of every autonomous run, and gives weaker local cost attribution per run-id. Keep it as a *provider behind* LiteLLM, not as the gateway itself.

### Where it runs

A launchd job on the mini, `com.convivy.llm-gateway.plist` `[name]`, `RunAtLoad` + `KeepAlive`, bound to **`127.0.0.1:<port>` only** (localhost; never externally exposed — the providers are reached outbound, nothing should reach the proxy inbound from off-box). Like the other plists it is machine config, untracked in git; bootstrap (`launchctl bootstrap`) is an operator step, not an agent action.

**Dependency isolation `[recommended]`:** run the proxy from a **dedicated venv** (`~/.local/venv-llm-gateway` or similar), not the platform `lib` venv. LiteLLM pulls a heavy transitive tree; keeping it out of the platform venv protects the test/CI gate and the lib import path. This means LiteLLM is **not** added to the repo's `requirements*.txt` (those govern the platform lib + tests); the gateway venv has its own pinned requirements file under the repo (e.g. `gateway/requirements.txt`) installed by its own step. Touching `requirements*.txt` is a high-risk trigger (ARCH conventions); this isolation deliberately avoids it.

The launchd-fired wrapper, if any shell is involved, is bash-3.2-safe (ARCH-082): prefer a direct `ProgramArguments` exec of the venv's `litellm` binary with a config path, no shell at all, which sidesteps the 3.2 floor entirely.

### Secrets

Provider API keys live in the **macOS Keychain**, never in git. The LiteLLM `config.yaml` references `os.environ/PROVIDER_API_KEY` placeholders only — no literal secrets — so the config is safe to track in the repo (`gateway/config.yaml`). The launchd job's `EnvironmentVariables` are populated at bootstrap from Keychain (`security find-generic-password -w …`), or a tiny boot shim exports them before exec. The proxy is protected by a **LiteLLM virtual key** (its master key + a per-consumer virtual key); agentctl and the PA seam each read their virtual key from Keychain at spawn time and pass it as the `ANTHROPIC_API_KEY` to the proxy. A grep guard in tests asserts the tracked config holds no secret-shaped literals.

### Anthropic passthrough

Anthropic agents do **not** route through the gateway at all — agentctl leaves `ANTHROPIC_BASE_URL`/`ANTHROPIC_API_KEY` unset for them, so they go direct to `api.anthropic.com` exactly as today. The gateway only ever serves non-Anthropic providers. This is the minimal-blast-radius choice and the reason existing Claude agents are provably unaffected: their code path does not change by a single env var. (Routing Anthropic *through* the proxy for unified accounting was considered and rejected for C1 — it would put the proxy in the hot path of every overnight run for no cost-observability gain, since Anthropic cost is already exact from the CLI.)

---

## agentctl integration

Minimal, and concentrated in `bin/agentctl`:

1. **Alias namespace.** Extend `MODEL_ALIASES` with the non-Anthropic aliases the gateway serves, e.g. `[gpt5 → openai/gpt-5, kimi → openrouter/…, qwen → …]` (final roster is the operator's call + what providers we sign up). The alias value is the **gateway-side model name**, which already encodes the provider.

2. **A routing predicate.** A pure helper `is_gateway_model(model_id_str) -> bool` `[recommended: explicit allowlist/prefix set, not "not Anthropic"]` — an explicit set of gateway-served model IDs (or a `provider/` prefix convention) is safer than negating "is it Anthropic," because an unknown literal passed through `model_id()` should fail closed to *direct* (today's behavior), not silently get routed to a proxy that lacks it.

3. **Conditional env injection.** In `_run_session`, after `model = model_id(...)` (`:406`), build the spawn env through a new pure helper `gateway_env(model, base_env) -> dict`: if `is_gateway_model(model)`, add `ANTHROPIC_BASE_URL` + the gateway virtual key; else return `base_env` unchanged. This is the entire routing change — one function, one call site for the real spawn (and the same helper applied to `design-room` only if a foreign-model panelist is ever wanted, which ARCH-079 says it is not — see "not changing").

4. **Honest `billing_source`.** `write_meta` currently hardcodes `billing_source="subscription_credit"` (`:482` and siblings). For a gateway run, record the real source, e.g. `billing_source="provider_api:<provider>"`, so telemetry and any future billing reconciliation are not silently wrong. Small, mechanical, threads `model`/provider into the existing `write_meta` calls.

No change to the concurrency cap, the worktree machinery, profile activation, the reaper, or the stream loop.

---

## Cost accounting — the crux

**The problem:** the CLI's `total_cost_usd` is computed from Anthropic's pricing. For a model served via the gateway, that figure is wrong (priced as if Anthropic) or zero. If we keep sourcing cost from the CLI event, every non-Anthropic run mis-accounts, and the budget caps (which gate real money post-cliff) gate on fiction.

**The fix — gateway is the cost source of truth for non-Anthropic runs, and it lands in the same `cost.json`:**

- Tag each gateway request with the run-id. agentctl already exports `AGENTCTL_RUN_ID` into `proc_env`; pass it to the proxy as request metadata (LiteLLM honors a `user`/`metadata` field, surfaceable via a header `claude -p` can carry, or via the virtual-key→run mapping). The proxy's logging callback writes the **real** per-request cost keyed by run-id to a local spend log (JSONL/SQLite under the run-artifacts tree) `[recommended: a per-run `gateway_spend.jsonl` the proxy appends to]`.
- `_finalize_run` gains a branch: **if this run used a gateway model, source `usd` from the gateway spend log for this run-id**, not from `total_cost_usd`. It still records tokens from the stream event. The Anthropic branch is unchanged.
- Because `cost.json`'s `usd` now carries real provider dollars for gateway runs, **`budget.py` needs no change** — `today_total_usd` sums the real number, `live_total_usd` and both caps inherit it, and `budget-watchdog` + `agentctl budget` report it. This is the elegant part: real-cost accounting reduces to "make `cost.json` true," and ARCH-065 already consumes `cost.json`.

**Fail-closed:** if a gateway run finalizes with **no** spend record (proxy crashed, callback dropped), do not write `usd: 0` (that would hide spend from the cap). Fall back to the in-flight estimate for that run and flag the run (`cost_source: "estimate_fallback"` in `cost.json`) so the gap is visible, never silent.

**Known C1 bias:** `inflight_estimate_usd`'s flat `$0.15/min` overestimates a cheap model mid-flight, so the *live* number runs hot for gateway runs until they finalize (completed cost stays exact). That is a conservative tripwire, acceptable for C1; a per-model burn rate is a C2 nicety, not a C1 blocker.

---

## Performance metric and the tuning loop (C2)

**Metric `[recommended]`: per-(agent, model) review-pass rate and CI-first-pass rate**, harvested from existing telemetry, with rework rate as a secondary signal.

Justification: the pipeline already manufactures these signals at no extra cost. Every worker PR gets a `reviewer` + `security-reviewer` pass (ARCH-068) whose verdicts are in the run dirs, and CI green/red is the ready-to-merge gate. So "did model M, running as agent A, produce work that passed review and CI on the first try" is computable by aggregating `outcome.json` + PR review state across run dirs, keyed by the `(agent, model)` already in `meta.json`. Task-success and human-rating were considered and deferred: task-success needs a labeling step the pipeline does not produce, and human rating does not scale to the autonomous lane. Rework rate (follow-up commits to the same PR, reopened reviews) is the natural second axis and is also already in git/PR history.

**Tuning loop shape:** a weekly **model scorecard** (a new read-only harvester, candidate `bin/model-scorecard` or a `co`-dispatchable agent) aggregates per-(agent, model) {review-pass %, CI-first-pass %, rework %, real $/run} over a trailing window and posts it to a Slack digest. The loop is human-in-the-loop by default (ARCH-020): the scorecard *recommends* dial moves; the operator turns the ARCH-079 model dial. The gate (below) is what makes a move safe.

---

## Phasing

**C1 — gateway + routing + real cost. Ships first; PA seam usable after this.**
- LiteLLM proxy on launchd (localhost, isolated venv, Keychain secrets, tracked secret-free config).
- agentctl: alias namespace, `is_gateway_model`, `gateway_env`, conditional env injection, honest `billing_source`.
- Cost: gateway spend log + `_finalize_run` gateway branch + fail-closed fallback. Budget unchanged.
- Route **one** low-risk agent to one non-Anthropic model behind the guardrail (below) to prove the whole loop end-to-end on real runs.
- PA seam: the gateway URL + virtual-key + alias namespace are the published contract; the `pa-*` crew wires the PA `llm/` seam to it in their repo (not this PR stack).

**C2 — performance observability + tuning loop.**
- The scorecard harvester + Slack digest.
- The perf gate automation (or a documented manual gate if automation is premature).
- Per-model burn rate for the in-flight estimate.
- Broaden the set of agents on cheaper models as the scorecard earns confidence.

---

## Quality guardrail — which agents move, and the gate

Anchored on ARCH-079's cost model (cost ≈ capability-tier × token-volume; the volume hog is the overnight worker tree, not human-paced work):

- **Stay premium, do not move:** `architect`/`platform-architect` (highest blast radius), the orchestrators and judgment roles (`overnight-orchestrator`, `planning-orchestrator`, `planner`, `curator`, `design-critic`, `product-manager`), and interactive `co` + `design-room` (human-in-the-loop, creative, low-volume). ARCH-079 already says so.
- **Candidates to move (the mechanical, high-volume lane):** `coder`, `refactorer`, `bug-fixer`, `test-writer`, and the reviewers — but **only one at a time, behind the gate**, starting in C1 with a single agent `[recommended: `test-writer` or `bug-fixer` — bounded scope, strong existing review + CI net to catch a regression]`.
- **The gate:** a cheaper model is adopted for an agent only if its trailing review-pass and CI-first-pass rates stay within `[N points, default 5]` of that agent's premium-model baseline; if it drops below, revert the dial. Until C2's scorecard exists, the gate is run manually by the operator reading the first weeks of runs — C1 deliberately moves only one well-netted agent so a regression is cheap and visible.

The existing safety nets (ARCH-068 every-PR review, CI-green ready-to-merge, ARCH-066 honest test-run reporting) are exactly what make moving the mechanical lane safe: a worse model shows up as failed review or red CI, not as silently shipped bad code.

---

## Test plan (Phase A harness, tests-as-touched, ARCH-098/099)

Everything new is structured as pure, network-free helpers so it is unit-testable under the existing pytest harness:

- **`is_gateway_model` / `gateway_env`** — new pytest module: gateway aliases route (env carries `ANTHROPIC_BASE_URL` + key), Anthropic models do **not** (env unchanged byte-for-byte), unknown literals fail closed to direct. This is the highest-value test — it pins the "Anthropic path unchanged" guarantee.
- **`MODEL_ALIASES`/`model_id` extension** — extend the existing agentctl dispatch-helper tests for the new aliases.
- **Cost merge in `_finalize_run`** — new tests: a gateway run sources `usd` from a fixture spend log; the no-record case writes the `estimate_fallback` flag rather than `usd: 0`; the Anthropic branch is unchanged. **`telemetry`/`write_cost` is on the explicitly-uncovered list in `tests/README.md`** — touching the cost-merge means adding that coverage now (test-as-touched on a previously-uncovered path).
- **Budget inheritance** — extend `today_total_usd` tests with a non-Anthropic `cost.json` fixture to confirm real dollars sum and the caps see them; assert `budget.py` itself is untouched.
- **Gateway config secret-guard** — a test asserting the tracked `gateway/config.yaml` contains only `os.environ/…` placeholders, no secret-shaped literals.
- **`billing_source`** — assert a gateway run's `meta.json` records the provider source, not `subscription_credit`.
- **Bash lint** — any launchd wrapper shell passes `tests/bash/lint.sh` (bash-3.2 guard). Preferring a no-shell `ProgramArguments` exec avoids this surface.

The full gate (`pytest` + `/bin/bash tests/bash/run.sh` + `lint.sh`) green before each PR opens. **No-autonomous-merge (ARCH-020/100):** every change lands as a ready-to-merge, unmerged PR; the launchd plist + Keychain entries + venv install are the operator's bootstrap steps, not agent actions.

---

## Risks

- **LiteLLM dependency weight / Python fidelity.** Heavy transitive tree; mitigated by the isolated gateway venv so it never touches the platform lib/test path. If it still proves fragile, the custom-shim fallback is the escape hatch.
- **Claude Code ↔ non-Anthropic translation fidelity.** Some agents lean on Claude-specific tool-use formatting, system-prompt handling, or extended thinking; a foreign model behind the Anthropic adapter may behave differently. Mitigated by moving **one** well-netted agent first and reading real runs before broadening — the perf gate is the instrument.
- **Cost-source gaps.** A dropped spend record would under-count real money if it wrote `usd: 0`. Mitigated by fail-closed fallback + an explicit `cost_source` flag so a gap is visible to the cap and the digest, never silent (the ARCH-065/069 lesson: an invisible cost is the dangerous one).
- **In-flight estimate runs hot for cheap models** until finalize (flat burn rate). Conservative, so it errs toward tripping the cap early, not late; per-model rate deferred to C2.
- **New SPOF in the non-Anthropic spawn path.** If the proxy is down, gateway-routed spawns must **fail fast and loud**, not silently fall back to Anthropic (that would hide the cost intent and surprise the budget). Anthropic agents are unaffected by a proxy outage by construction. A health check + the launchd `KeepAlive` are the mitigations.
- **Secrets handling.** Keys in Keychain + launchd env must never reach logs or the tracked config; the secret-guard test + localhost-only binding are the guards.

---

## Blast radius

- **`bin/agentctl`** — `MODEL_ALIASES`; new `is_gateway_model` + `gateway_env`; the env-injection call in `_run_session`; the `_finalize_run` cost-merge branch; `billing_source` in the `write_meta` calls. The Anthropic path is unchanged.
- **New files** — `gateway/config.yaml` (tracked, secret-free), `gateway/requirements.txt` (gateway venv), a launchd plist (untracked, operator-bootstrapped), optionally a no-shell launcher, and (C2) a scorecard harvester.
- **Unchanged consumers that inherit the benefit** — `budget.py`, `budget-watchdog`, `agentctl budget` (real dollars via `cost.json`, no edit); `overnight-orchestrator` (spawns workers; unaffected unless an agent's alias is non-Anthropic, and then only that worker routes).
- **Out-of-tree consumer** — the PA `llm/` seam consumes the published gateway contract; the `pa-*` crew implements that in the personal-assistant repo.
- **Requirements** — deliberately **not** touched (`requirements*.txt` / `pyproject.toml` stay as-is); the gateway venv is isolated.

---

## What I'm explicitly not changing

- **`budget.py`** — the cost-source fix lands upstream in `_finalize_run`, so the budget module and its caps are untouched. (Per-model burn rate is a *possible* C2 edit, flagged, not taken here.)
- **The reaper, stream loop, concurrency cap, worktree machinery, profile activation** — none are on the routing path.
- **`design-room` foreign-model routing** — design-room is human-in-the-loop creative work, premium by ARCH-079; the routing helper is written so it *could* apply there, but C1 does not.
- **Anthropic agents and the overnight schedule** — no model moves for the premium roster; `launch-overnight` is not touched.
- **The PA repo** — out of substrate remit (ARCH-101); I design the contract, the `pa-*` crew builds their side.
- **Routing Anthropic through the proxy** — rejected for C1; Anthropic stays direct.

---

## High-risk operations (for `platform-coder` to halt-and-surface on)

- **launchd job definition** (new `com.convivy.llm-gateway.plist`) — a safety-rail trigger. The plist is untracked machine config; `platform-coder` proposes its content but does **not** bootstrap it. The operator bootstraps. Halt-and-surface if anything wants the gateway in the overnight critical path before it is proven.
- **Budget/cost path** (`_finalize_run` cost-merge) — touches the money-accounting that the caps gate on. The fail-closed fallback is mandatory, not optional; surface any case where a gateway run could finalize with `usd: 0`.
- **Secrets** (Keychain + launchd env + the tracked config) — surface on any code path that could write a key to a log, the transcript, or a tracked file.
- **Dependencies** — the gateway venv must stay isolated; surface (do not silently proceed) if anything requires adding LiteLLM to the repo's `requirements*.txt` or `pyproject.toml`.
- **install.sh** — not expected to change; surface if the gateway install tempts an edit to the symlink installer.

---

## Draft ADR (for the steward to mint as ARCH-NNN)

**Title:** Phase C — one Anthropic-compatible gateway for non-Anthropic agents; `claude -p` stays the universal runner; gateway is the real-cost source of truth.

**Decision.** Non-Anthropic agents run through the same `claude -p` runtime, routed to a local LiteLLM proxy via `ANTHROPIC_BASE_URL`; Anthropic agents stay direct (unchanged). The proxy is the source of truth for real per-provider dollars, which agentctl writes into `cost.json`, so the ARCH-065 budget machinery inherits real-cost accounting unchanged. C1 ships gateway + routing + cost and makes the PA seam usable; C2 ships per-(agent, model) review-pass/CI-first-pass observability and the tuning loop. Cheaper models are adopted one agent at a time behind a perf gate; the premium roster (architect, orchestrators, interactive, design-room) is unchanged per ARCH-079.

**Why.** Forking the spawn/stream/reap/finalize pipeline per provider would multiply the incident surface (ARCH-064/065/069). The base-URL hook keeps one runner and one new component. Sourcing real cost into `cost.json` reuses the existing budget consumer instead of re-plumbing it. Moving the mechanical lane (not the judgment lane) matches ARCH-079's volume×tier cost model, and the existing every-PR-review + CI gate (ARCH-068) is the net that makes a model regression visible.

**Relates to:** ARCH-098/099 (Phase A/B), ARCH-065 (live budget accounting), ARCH-069 (the reaper this preserves), ARCH-079 (model tiering / the dial), ARCH-082 (bash-3.2 floor), ARCH-101 (the PA `llm/` seam, first consumer), ARCH-020/100 (no autonomous merge).

---

## Open questions for the operator

1. **Provider roster** — which non-Anthropic providers/models to sign up first (drives the alias namespace and the Keychain entries).
2. **First agent to move** — `[recommended: test-writer or bug-fixer]` for the C1 end-to-end proof; confirm or redirect.
3. **Gate threshold** — `[default: within 5 points of the premium baseline]` for review-pass/CI-first-pass.
4. **Deliverable location** — this doc is at `agent-platform/docs/design/phase-c-proposal.md` per the brief; confirm `docs/design/` is the home for platform design docs (it did not exist before this run).
