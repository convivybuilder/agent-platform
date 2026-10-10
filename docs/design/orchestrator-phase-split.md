# Orchestrator phase-split — design (lever C of orchestrator-token-reduction)

> **RETIRED 2026-09-10 (the operator).** Everything below describes a CRON-FIRED design.
> `com.convivy.overnight-orchestrator` and the rest of the overnight fleet were
> deleted, not disabled — a disabled job came back after a reboot. Autonomous
> work returns event-triggered rather than on a timer. `bin/launch-overnight`
> and the stage agents STAY and are what `/start-overnight` runs interactively,
> so read this as the design of the machinery, not of a schedule that fires.
> The per-night budget caps lived in the deleted plist's env and now have no
> default; see the note in `bin/launch-overnight`.

**Author:** `platform-architect` · **Date:** 2026-06-27 · **Status:** design, for the steward / the operator's approve-redirect
**Decision:** ARCH-268 (2026-06-27) — build lever C: replace the single long-running overnight-orchestrator session with a deterministic launcher that sequences stage agents, each fresh-context, reading the prior stage's durable artifacts.
**Parent scope:** `docs/design/orchestrator-token-reduction.md` (lever C section). **Mirrors:** ARCH-122 (deterministic weekly-curation launcher) + ARCH-123 (durable Phase-1→2 worklist).
**Optimizes for:** structural context-capping and idempotent, independently-resumable stages. Cost reduction is a secondary, expected byproduct — the justification is reliability + idempotency.

> **Naming.** "Phase C" already names the multi-LLM gateway program (`docs/design/phase-c-proposal.md`, where A/B/C = test-foundation / platform-crew / model-gateway). This document is **lever C of orchestrator-token-reduction**, unrelated. To avoid a second collision on the word "phase": this doc calls the four coarse launcher-sequenced processes **stage agents** (triage / fix-drain / verify / digest), and reserves **internal phase** for the fine-grained units `overnight_manifest` already tracks (`phase-0`, `bug-sweep`, `bug-fix`, `architect`, `product-queue`, `kb-sync`). One stage agent owns several internal phases.

---

## 1. Current state

`launch-overnight` fires from the `com.convivy.overnight-orchestrator` launchd job at 01:00 every night. It branches on the day-of-week (ARCH-097): Mon–Wed arch-led drain, Thu–Sat standard drain, each against one product (`nightly_product_for_dow`); Sunday a gated catch-up across products whose queue still carries real items. The branch resolves a mode + product, then does **one thing**: `agentctl spawn overnight-orchestrator --on <product> --profile overnight --task "<2KB mode framing>" --detach`. The launcher exits within seconds; the orchestrator runs autonomously in its own process group.

That single orchestrator process then runs the **entire night** in one `claude -p` session: Phase 0 (resolve prior approvals) → bug queue (sweep + fix-drain) → features queue → architect pass (arch-led only) → product queue → KB factual-sync → digest. Its governing prompt is `agent-platform/agents/overnight-orchestrator.md`, 329 lines carrying the full invariant set (ARCH-027/055/058/064/070/106/129/198/254/255 and more).

This is the ARCH-123 failure mode by construction. The session accumulates context across ~3 hours and ~573 API turns; every tool call re-reads the whole ~150K working set at the cache-read rate (84.1M cache-read tokens, ~$12, measured 2026-06-10). Worse than cost: the accumulating context is fragile. A mid-run compaction can drop the orchestrator's belief about what it has done (the reason `bug-fix-worklist.md` exists, ARCH-123), and an outright death abandons the night's remaining work (the 2026-05-27 vivy drain quit ~33 min in). Two layers of machinery exist **only to paper over this**:

- **ARCH-160 resume** — `overnight_manifest` (the orchestrator marks each internal phase done as a checkpoint) + `overnight-resume-watchdog` (polls the manifest; if the wave's run is dead and incomplete, re-spawns the orchestrator, which adopts the manifest and resumes from the first incomplete phase, bounded to 2 resumes).
- **ARCH-254 init-verify + ARCH-129 token discipline** — prompt-level guards that mitigate, but do not remove, the cost and fragility of one long context.

Phase-splitting removes the failure mode at the root: cap context size structurally so no 150K working set exists to re-read, compact, or lose.

### What the budget machinery already does (load-bearing for the design)

`budget.py` keys the per-night caps ($100 soft / $160 hard, ARCH-229, set in the orchestrator plist env) on `today_dir()` = `telemetry.platform_day()` (the **local** calendar day), summing `usd` across every `scheduled`-origin run dir in that day's directory. The cap is **already a sum over many processes** — today it spans the orchestrator plus all its `agentctl`-spawned workers. It is not keyed to a single run. The 01:00–~09:00 overnight window sits inside one local calendar day, so every process of the night shares one `platform_day` bucket. **This is the keystone of the design: splitting one orchestrator session into N stage-agent processes changes nothing about how the cap is computed.**

---

## 2. Proposed structure

Replace the one `--task`-framed orchestrator spawn with a **deterministic stage sequence** the launcher drives, mirroring `launch-weekly-curation` (ARCH-122: foreground, sequential, each agent reads what the prior one wrote) and extending ARCH-123's durable worklist into the inter-stage contract.

### The four stage agents

| stage agent | internal phases it owns | dispatches (sub-agents) | runs a review cycle? | reads | writes |
|---|---|---|---|---|---|
| **`overnight-triage`** | `phase-0`, `bug-sweep`, features-enumerate, `architect` (arch-led: **produce proposals only**), `product-queue` (**read**) | `bug-investigator`, `architect` | no (single-pass workers, no PRs) | Slack reactions, freshness markers, product queue, architect inputs | **`worklist.md`** (the unified build list), pending-approval messages to `#<product>-proposals`, updated queue markers |
| **`overnight-fix-drain`** (one agent **per worklist item**) | `bug-fix`, features-build, product-queue-exec, architect-proposal-exec | `coder` / `refactorer` / `bug-fixer` + `reviewer` + `security-reviewer` | **yes** — the full ARCH-055/058/106/198/027/068/255 cycle | its one `worklist.md` row | the item's `item-<id>.outcome.json` (PR URL, findings across rounds, verdict, ✅/defer state) |
| **`overnight-verify`** | completeness-gate (ARCH-123), claim-verify (ARCH-030), `kb-sync` (ARCH-048) | `knowledge-scribe`, `kb-sync-verifier` (via `agentctl scribe verify`) | no | `worklist.md` + every `item-*.outcome.json`, merged PRs | `digest-input.md` (reconciled, claim-verified), synced KB docs |
| **`digest`** (reuse `explainer`, digest mode — ARCH-018) | digest | — | no | `digest-input.md` + all run dirs | the single `#overnight-digest` post + per-run `explanation.md` |

**The principle is this.** The triage stage decides *what to build* (read-heavy, dispatch-light, no PRs) and emits a flat durable worklist. The fix-drain stage *builds it*, and is realized as **one fresh-context agent per worklist item** — the longest, most context-hungry, most death-prone work is bounded to a single PR's review churn per process (worker + 2 reviewers + ≤3 cycle rounds + CI), never the whole night's. The verify stage *reconciles and proves* (completeness gate, claim verification, KB-sync). The digest stage *reports*. Each stage agent boots with a focused ~60–90-line prompt carrying only the invariants it owns, against the 329-line monolith re-read 573 times today.

### Why per-item fix-drain is the core of the win

The single accumulating context is the failure mode (ARCH-123/160). The bug-fix / queue-drain work is where context balloons: each PR runs a worker, two reviewers, up to three auto-cycle rounds, a CI gate, and a stamp gate, and the orchestrator holds **all of it** across **all items** in one context today. One fix-drain agent per item caps each process at one item's churn. Idempotency is already per-item and durable: the ✅/PR/defer state lives in Slack reactions (ARCH-014) and the worklist row, so a dead item-agent is re-driven by re-reading the worklist and skipping settled rows — exactly today's intra-phase idempotency, now at the process boundary. The 4-concurrent ceiling (ARCH-006) means one item-agent (which itself peaks at worker + 2 reviewers = 3 slots, + itself = 4) consumes the whole budget, so the launcher serializes item-agents one at a time. That matches today's effective serialization under concurrency pressure and makes it deterministic.

---

## 3. Inter-stage artifact contracts

Every downstream stage reconstructs what it needs from durable artifacts on disk, never from in-context memory. All artifacts live under the **night's shared artifact directory**, keyed per product so a re-fire finds them:

```
~/Convivy/agent-runs/overnight/<product>/<platform-day>/
  ledger.json          # the launcher-owned stage ledger (§4, replaces the LLM-written manifest)
  worklist.md          # triage → fix-drain (extends ARCH-123 bug-fix-worklist.md)
  item-<id>.outcome.json   # one per worklist item, written by that item's fix-drain agent
  digest-input.md      # verify → digest
```

### `worklist.md` (triage writes, fix-drain reads) — extends ARCH-123

One `[ ]` row per actionable build item, unifying every source the triage stage drains. Each row is self-contained — a fix-drain agent reads exactly its row and needs nothing else from triage's context:

```
- [ ] id=bug-0007  source=bug-queue  slack=C0B2KUEAYJW/1719... gh=convivy/vivy#511
      worker=bug-fixer  purpose=bugfix-511  diagnosis=$HOME/.../diagnosis-511.md
      title="[nightly-bugfix] fix calendar-merge off-by-one"
- [ ] id=feat-0002  source=features    slack=C0BCU.../1719...  worker=coder  purpose=feat-export
      title="add CSV export to the bookings table"
- [ ] id=pq-0004    source=product-queue  queue-line=12  worker=refactorer  purpose=split-owner-svc
      title="split OwnerService per ARCH-211 proposal"  proposal=$HOME/.../proposal.md
- [ ] id=arch-0001  source=architect    proposal=$HOME/.../arch-findings.md#p3  worker=coder
      title="extract retry helper (architect agent-tractable proposal)"
```

Required per row: stable `id`, `source`, the originating `slack:<channel>/<ts>` and/or `gh=<repo>#<n>` backlink, the resolved `worker` + worktree `purpose`, the one-line `title`, and a pointer to any diagnosis/proposal doc. The freshness-scoping (ARCH-197) is applied **at worklist construction** — triage only emits rows for new-since-marker work — so fix-drain never re-decides scope.

### `item-<id>.outcome.json` (each fix-drain item writes, verify reads)

The durable per-item result the verify stage reconciles against, so it never trusts a fix-drain agent's vanished context:

```json
{
  "id": "bug-0007", "source": "bug-queue",
  "slack": "C0B2KUEAYJW/1719...", "gh": "convivy/vivy#511",
  "pr_url": "https://github.com/convivy/vivy/pull/640",
  "state": "ready-to-merge",      // ready-to-merge | ci-red | churning | deferred | failed | stale-closed
  "review_status": "PASS",         // agentctl review-status result
  "ci": "green",
  "findings": [ /* every finding, every round, with severity (ARCH-255) */ ],
  "closed_loop": true,             // ✅ + threaded PR link added
  "defer_reason": null
}
```

**Exactly one outcome file per worklist row — the launcher backfills a failure.** A fix-drain item-agent normally writes its own outcome. If an item-agent exhausts the 2-respawn cap (§5) and produces no outcome, the **launcher** writes a minimal failed outcome for that row — `{"id": …, "source": …, "state": "failed", "defer_reason": "fix-drain exhausted 2 respawns"}` — so the verify stage always finds exactly one outcome per worklist row. This keeps the ARCH-123 completeness tally uniform (N enumerated = ready-to-merge + ci-red + churning + deferred + failed + stale-closed) and gives the digest an honest "this item failed, needs the operator's eyes" line rather than a silently missing row.

### `digest-input.md` (verify writes, digest reads)

The reconciled, claim-verified digest payload — the existing orchestrator "Deliverable" digest contract (ARCH-018/226), now produced by the verify stage from the worklist + item outcomes rather than from one session's memory. Carries the per-item action affordances (ARCH-226), the balanced bug-phase tally (ARCH-123 completeness gate), the artifact paths, and the KB-sync results.

---

## 4. The invariant-carrying table

The hard part: every invariant that lives in the single-session design must have a home in the phase-split. For each, where it lives now and what changes.

| Invariant | Today (single session) | In the phase-split | What changes |
|---|---|---|---|
| **ARCH-160 manifest / resume** | The orchestrator *prompt* writes `overnight-manifest mark` after each internal phase; `overnight-resume-watchdog` re-spawns the dead session, which adopts the manifest. | **Structural — the stage-agent process boundary IS the checkpoint.** The launcher (not an LLM) writes a **`ledger.json`** recording each stage agent's clean exit. A stage that exited 0 is a settled checkpoint; the launcher resumes the sequence from the first unsettled stage. `overnight_manifest.py`'s data shape survives almost intact, but the **writer moves off the prompt onto the deterministic launcher** (the ARCH-055 lesson again), and a "checkpoint" now means *a process exited cleanly*, not *a prompt issued a mark*. | The LLM no longer self-reports phase completion. `overnight-manifest mark/begin/finish` calls leave the stage prompts. |
| **`overnight-resume-watchdog`** | Polls the manifest; re-spawns the dead **LLM session**. | **Retired in its LLM-session role.** A dead *stage agent* needs no watchdog — the foreground launcher sees its non-zero exit immediately and re-spawns that one stage (bounded, §4 launcher). The watchdog's singleton-lock + bounded-respawn + `#overnight-digest` alert machinery is **repurposed** to relaunch a dead **launcher** (the only remaining long-lived process, and it holds zero LLM context, so its death is cheap and fully recoverable from `ledger.json`). | Watch target changes from `is_wave_alive(LLM run)` to `is_launcher_alive(ledger)`. The per-LLM-session classify() logic retires; the relaunch plumbing stays. |
| **ARCH-075 freshness-skip** | Orchestrator checks `<product>.<task>.json` before `bug-sweep` / `bug-fix` / `arch-review`. | **Per-stage markers, scoping at worklist-construction.** `overnight-triage` checks the `bug-sweep` + `arch-review` markers (it owns sweep + architect-produce) and emits ARCH-197-scoped rows only. The `bug-fix` marker's scoping is already baked into the worklist (triage excludes pre-marker-drained items). `mark-fresh` (the daytime cost-shift writer) is **unchanged**. | Each stage checks only the markers for the phases it owns; no behavior change to the marker contract. |
| **ARCH-055 publish gate** | Orchestrator publishes before every reviewer + every cycle round. | **Owned by `overnight-fix-drain`**, per item, unchanged. The item-agent runs `agentctl publish --repo <product> --purpose <purpose>` before each reviewer dispatch and on every round, captures `published-sha:`. | Gate moves into the per-item agent; mechanics identical. |
| **ARCH-058 PR-open gate** | Orchestrator opens every PR via `agentctl pr-open ... --run <worker-run-id>`. | **Owned by `overnight-fix-drain`**, per item. The item-agent opens the PR after first publish, before reviewers; never re-opens on cycle rounds. | Moves into the item-agent. |
| **ARCH-106 CI gate** | Orchestrator gates on `gh pr checks --watch` after reviews go clean. | **Owned by `overnight-fix-drain`**, per item. CI-red → another cycle round within the shared cap; CI-red at cap → `state=ci-red` in the item outcome. | Moves into the item-agent; the foreground-wait discipline (ARCH-070) applies inside it. |
| **ARCH-198 stamp gate** | Orchestrator runs `agentctl review-status --pr <N>` as the final ready-to-merge assertion. | **Owned by `overnight-fix-drain`**, per item — `review-status` PASS recorded as `review_status:"PASS"` in the item outcome; the verify stage re-reads that, never infers from prose. | Moves into the item-agent; the deterministic assertion is exactly preserved. |
| **ARCH-055/058/106/198 — "which stage owns each gate"** | All in one session. | **All four publish/PR-open/CI/stamp gates live in `overnight-fix-drain`, per PR.** This is the task's explicit requirement: the fix-drain stage owns the full review cycle per PR. The triage and verify stages open **no PRs** and run **no review cycle**. | Concentrates the entire ready-to-merge gate chain in one stage type. |
| **ARCH-064 detached-spawn dispatch** | Orchestrator dispatches every worker via detached `agentctl spawn`, never an in-process Task/Agent tool. | **Unchanged within each stage.** Each stage agent dispatches its sub-agents (triage→bug-investigator/architect; fix-drain→worker+reviewers; verify→scribe) via detached `agentctl spawn`. The **launcher**'s dispatch of stage agents is itself `agentctl spawn` (foreground), the ARCH-122 shape. | No change to the dispatch primitive; it now operates at two levels (launcher→stage, stage→worker). |
| **ARCH-070 wait discipline** | Orchestrator uses foreground `agentctl wait`, never yields its turn. | **Unchanged within each stage** — a fix-drain item-agent foreground-waits on its worker + reviewers exactly as today. **The launcher** foreground-waits on each stage agent (it is a bash process, not a `claude -p` turn, so "ending the turn" has no analogue — it blocks naturally). | The catastrophic "end-the-turn-to-wait" failure mode shrinks: stage agents are short-lived, and the launcher's wait is an ordinary blocking shell call. |
| **ARCH-129 token discipline** | Batch calls, one wait per batch, file-redirect fat output — all to fight the 150K accumulating context. | **Still applies *within* a stage, but the structural cap does the heavy lifting.** No stage accumulates a night's worth of context, so the per-turn re-read cost is bounded by the stage's own small working set. The prompt-level batching rules stay in each stage prompt as hygiene. | The invariant's *motivation* (one giant context) is largely removed; the rules remain as cheap hygiene. |
| **ARCH-254 init-verify** | Orchestrator runs `agentctl verify-init` on each worker between spawn and wait. | **Two levels.** Each stage agent runs `verify-init` on its sub-agents (unchanged). **The launcher** runs `agentctl verify-init` on each stage agent it spawns, before waiting — a stage agent that dies pre-init is re-spawned once by the launcher rather than burning a 30-min wait. | Adds launcher-level init-verify for the stage agents; sub-agent init-verify unchanged. |
| **ARCH-255 revise-to-day-session-bar** | Orchestrator hands reviewer findings verbatim + severity to the re-dispatched worker, incorporates non-blocking findings. | **Owned by `overnight-fix-drain`**, per item — the item-agent runs the cycle and carries findings verbatim across rounds into `item-*.outcome.json`. | Moves into the item-agent; the under-revise visibility (every finding recorded) is preserved in the item outcome. |
| **ARCH-027/068 review-every-PR + 3-cycle cap** | Orchestrator runs reviewer + security-reviewer + auto-cycle (cap 3) on every PR, no change-type exemption. | **Owned by `overnight-fix-drain`**, per item. The cap is naturally per-item (one PR per agent), which is exactly today's per-PR cap. | Moves into the item-agent; "review every PR including trivial" holds by construction (every item is a PR-producing agent). |
| **ARCH-006 4-concurrent ceiling** | Implicit: one orchestrator session physically cannot exceed 4 concurrent spawns, so the cap enforces itself. | **Explicit, and it is why fix-drain serializes.** Each fix-drain item-agent peaks at 4 slots (itself + worker + 2 reviewers), so it saturates the ceiling alone; the **launcher** therefore runs item-agents one at a time (§2). Value unchanged; enforcement moves from an implicit single-session property to an explicit launcher decision. | The launcher serializes the fix-drain fan rather than fanning out independent items — the throughput trade-off the canary must measure (PRs-landed/night + wall-clock). |
| **Per-night budget caps ($100 soft / $160 hard, ARCH-229)** | `budget.py` sums `scheduled`-origin runs in `today_dir()`; the **hard** cap refuses spawns platform-wide; the **soft** cap is a prompt wind-down (ARCH-071: stop opening new pipelines, finish in-flight). | **Hard cap: unchanged and automatically spans the sequence** — every stage agent and every sub-agent is `scheduled`-origin in one `platform_day` bucket, so `agentctl spawn` refuses across the whole night regardless of process count (§1 keystone). **Soft cap wind-down: becomes deterministic.** The **launcher** checks `agentctl budget` before spawning each new fix-drain item-agent; soft crossed → stop starting new items, let the in-flight item finish (ARCH-071's "finish what's started" now means the current item-agent runs to its clean ready-to-merge state), then go straight to verify + digest on what landed. | The wind-down decision moves from prompt judgment to a launcher gate — a reliability improvement. `budget.py` is untouched. |

---

## 5. The launcher design

`launch-overnight` keeps its ARCH-097 front half verbatim — the EXIT-trap alert (ARCH-083), the `AGENT_RUN_ORIGIN=scheduled` tag, `nightly_product_for_dow`, the dow `case` (1–3 arch-led / 4–6 standard / 7 catch-up), the Sunday `queue_real_items` gate, the ARCH-164 daytime-claim suppression. **The three modes and per-product day-of-week selection survive untouched because they were always launcher-resident** — the launcher already computes mode + product before spawning. What changes is the back half: instead of one detached `--task` spawn, it runs the stage sequence.

### Sequencing (mirrors `launch-weekly-curation`, ARCH-122)

For the resolved `(mode, product)`, the launcher runs **foreground, sequential**:

```
run_stage triage    → writes worklist.md
read worklist.md     → for each unsettled item, BUDGET-GATE then run_stage fix-drain(item)  (serialized, 1 at a time)
run_stage verify     → writes digest-input.md
run_stage digest     → posts to #overnight-digest
```

Each `run_stage` is `agentctl spawn <stage-agent> --on <product> --profile overnight --task "<stage brief, mode-threaded>"` run **foreground** (blocking to the stage's `outcome.json`), preceded by `agentctl verify-init` (ARCH-254). The launcher holds **zero LLM context** — it is a bash sequencer, the ARCH-122 shape. Arch-led mode threads the architect-produce step into the triage brief; standard/catch-up omit it. This is the one place `bash 3.2` compatibility is load-bearing (ARCH-082): the stage list and per-mode branching use indexed arrays + `case`, never `declare -A`.

### `ledger.json` and resume (structural)

Before each stage the launcher records `stage=<name> state=started`; on clean exit, `state=done`; on the whole sequence, `completed_at`. **Resume is reading the ledger:** a re-fired or relaunched `launch-overnight` finds tonight's ledger, and if it is incomplete and same-`platform-day`, resumes from the first non-`done` stage. Fix-drain resumes finer: it re-reads `worklist.md` and skips items whose `item-*.outcome.json` is settled (✅/ci-red/churning/deferred/failed/stale-closed) — the same intra-phase idempotency ARCH-160 relied on, now keyed on durable per-item outcomes.

### Phase failure (bounded re-spawn)

A stage agent that exits non-zero is re-spawned by the launcher up to **2 times** (mirrors `RESUME_WATCHDOG_RESUME_CAP`); still failing → the launcher marks that stage `failed` in the ledger, alerts `#overnight-digest` (ARCH-083 trap shape), and **continues to the stages that can still run** (a failed fix-drain item does not block verify+digest on the items that landed; a failed triage aborts the night with an alert, since downstream stages have no worklist). A dead **launcher** is relaunched by the repurposed resume-watchdog (§4), which reads the ledger and re-fires from the first unsettled stage.

### No-op / freshness-skip

A stage with nothing to do exits 0 having written an empty-but-valid artifact: triage with an empty worklist (every queue empty or freshness-scoped to zero) writes `worklist.md` with zero rows; the launcher reads zero items, skips the fix-drain fan entirely, and verify+digest emit the honest "0 PRs — here's why" digest (ARCH-018 honesty). A freshness-fresh `bug-sweep`/`arch-review` marker scopes triage's emitted rows (ARCH-197), exactly as the single session scoped its phases. Sunday catch-up's per-product gate is unchanged: a product with zero real queue items never enters the stage sequence.

---

## 6. Roster changes

Three new agent slugs — `overnight-triage`, `overnight-fix-drain`, `overnight-verify`. The digest stage **reuses `explainer` in digest mode** (ARCH-018), no new slug. Each new agent gets a focused profile under `shared/agents/` decomposed from `overnight-orchestrator.md`: shared dispatch invariants (ARCH-064/070/129/254/006) in each, plus only the gates that stage owns (triage: phase-0 + freshness + architect-produce + worklist contract; fix-drain: ARCH-055/058/106/198/027/068/255 + the per-item outcome contract; verify: ARCH-030 + ARCH-048 KB-sync + ARCH-123 completeness gate). The 329-line monolith shrinks to three ~70–90-line prompts, each with a narrower `tools:` grant (least-privilege: triage needs no `pr-open`/`publish`; verify needs no reviewer spawn).

**Three-place registration** (each new slug, the ARCH-099 convention):

1. `lib/knowledge/knowledge.py` — add the three slugs to `ALLOWED_AUDIENCE`.
2. `~/Convivy/knowledge/scripts/validate.py` — add them to its mirrored `ALLOWED_AUDIENCE` (the comment notes it mirrors agent-platform; a miss is a warning, not a failure, but keep them in lockstep).
3. `~/Convivy/knowledge/projects/knowledge/reference/frontmatter-schema.md` — add the three to the roster table (the canonical `audience` values block).
4. **Watcher restart** — after the knowledge-repo edits land, restart `knowledge-watcher` so the SQLite index picks up the new agent docs (the standard post-roster-change step).

The `overnight-orchestrator` slug is **retained** through the revert window (§7) — its profile stays in the tree, dormant behind the flag, until the single-session path is deleted.

---

## 7. PR sequence — never leave the 1am path half-migrated

Build-behind-a-flag, then atomic cutover with a one-product canary. A partial landing must never break tonight's drain.

**PR1 — additive scaffolding, nothing wired.** New stage-agent profiles + the three-place roster registration + watcher restart + `worklist.md`/`item-outcome`/`ledger.json` schema helpers in `lib/agent-platform/`. `launch-overnight` is **untouched**; the 1am path still spawns the single-session orchestrator. No behavior change. *Ships dark.*

**PR2 — the phase-launcher behind an OFF-by-default flag.** `launch-overnight` reads `OVERNIGHT_PHASE_SPLIT` (and an optional `OVERNIGHT_PHASE_SPLIT_PRODUCTS` allowlist): **unset/0 → today's single-session spawn, byte-identical routing**; set → the stage sequence (§5). Add a `--canary <product>` affordance so the operator can hand-run one product's night phase-split. The default-off flag means PR2 merging changes nothing about tonight.

**PR3 — one-product canary.** Enable phase-split for a single product via `OVERNIGHT_PHASE_SPLIT_PRODUCTS=sous-chef` (lowest customer-facing blast radius). This enablement is a **plist env edit** (the orchestrator plist's `EnvironmentVariables`) — a **high-risk launchd change** the steward / `platform-coder` makes, not an autonomous edit (§9). Run a full week (sous-chef gets one arch-led + one standard night); watch `orchestrator-tokens` and the reliability signal (did every stage settle; did the ledger resume cleanly on any death).

**PR4 — atomic cutover + dormant revert path.** Flip the default to phase-split for all products. Retire `overnight-resume-watchdog`'s LLM-session role (repurpose to launcher-relaunch), retire the single-session `--task` framing constants from `launch-overnight`. **Keep the single-session codepath reachable behind `OVERNIGHT_PHASE_SPLIT=0`** for one release cycle (the revert path, §8) before any deletion PR.

Each PR carries its own tests (§ test plan) and is independently shippable; PR1–PR2 are inert to the live path, so a stall between any two PRs leaves tonight's drain exactly as it is today.

---

## 8. Revert path (the operator's explicit guardrail)

The single-session design must stay revertible if cost spikes or reliability regresses after cutover. The mechanism:

- **The flag is the switch.** Through the revert window (PR4 + one release cycle), `OVERNIGHT_PHASE_SPLIT=0` in the orchestrator plist env restores the single-session spawn on the next 01:00 fire. No code revert, no redeploy — `launch-overnight` is live-on-edit (`~/.local/bin` symlinks the repo), so the next launchd fire reads the new env. Because the single-session codepath and the `overnight-orchestrator` profile are kept dormant-but-present until the revert window closes, the fallback is a one-line env flip.
- **The tripwire is `orchestrator-tokens`.** The nightly `bin/orchestrator-tokens` job is the instrument that measured the lever-A/B baseline; it reports per-night requests / cache-reads / cost. After cutover, if it shows a cost spike (or the morning reliability read shows stages failing to settle), flip the flag to 0 and surface to the steward. Phase-split should *lower* cost (smaller contexts), so a spike is a signal something is wrong, not an expected cost of the new shape.
- **Kill-switch granularity.** `OVERNIGHT_PHASE_SPLIT_PRODUCTS` lets the revert be **per-product** — fall back only the product that regressed, keeping the rest on phase-split. The deletion PR that finally removes the single-session path is gated on a clean revert-window (no flag flips, `orchestrator-tokens` stable or down).

---

## 9. High-risk operations (for `platform-coder` to halt-and-surface)

| Step | Trigger | Halt-and-surface |
|---|---|---|
| PR2/PR3 `launch-overnight` edit | **launchd-critical bash, ARCH-082 floor** | The new sequencing must be `bash 3.2`-clean (indexed arrays + `case`, no `declare -A`, no `${var,,}`, no `mapfile`). The bash lint job (`tests/bash/lint.sh`, the bash-4 guard) is a hard gate; a red lint halts. |
| PR3 canary enablement + PR4 cutover | **launchd plist env change** (`com.convivy.overnight-orchestrator.plist` `EnvironmentVariables`) | A plist edit is high-risk and **not autonomous** — `platform-coder` proposes the exact env diff and halts for the steward / the operator to apply + `launchctl` reload. This doc does not edit the plist. |
| PR4 watchdog repurpose | **resume/concurrency safety rail** (ARCH-160) | Changing what `overnight-resume-watchdog` watches touches the night's last-line recovery. `platform-coder` lands it behind the same flag and keeps the LLM-session classify() path until the revert window closes. |
| Per-night budget cap spanning | **budget rail** (ARCH-229) | `budget.py` is **not** edited (§1 keystone — the cap already spans). If any step is tempted to touch budget keying, halt: the design's correctness depends on leaving it alone. |
| PR1 roster + watcher restart | **KB validator / watcher** | The three-place registration must stay in lockstep (a `validate.py` miss is only a warning, so it can silently drift); the watcher restart is required for the index to see the new agents. |

No step crosses into `constance`/`vivy`/`sous-chef` product code, `install.sh`, dependency changes, or destructive git — all out of scope here.

---

## 10. Test plan

Test-as-touched is mandatory (ARCH-098/099). Phase A suites over the touched paths:

- **`tests/bash/test_launch_overnight.sh`** (exists — the crown-jewel routing test). **Extend:** flag-off path is byte-identical to today's routing (same `nightly_product_for_dow`, same dow→mode); flag-on path sequences the right stage agents per mode (arch-led threads architect-produce into triage, standard/catch-up omit it); the Sunday per-product gate still governs whether a product enters the sequence; the soft-cap budget gate stops the launcher starting a new fix-drain item. Keep neutralizing side effects (`spawn_product`/`run_stage` stubbed) as the existing test does.
- **`test_overnight_manifest.py`** (exists). **Extend / re-home:** the manifest data-shape tests carry over to the launcher-written `ledger.json` (fresh / resume-from-first-unsettled / complete / stale-window). The writer is now deterministic, so add a test that the launcher marks `done` only on a stage's clean exit.
- **`test_resume_watchdog.py`** (exists). **Rework:** the `classify()` verdicts re-point from "is the LLM wave alive" to "is the launcher alive / is the ledger settled." The singleton-lock + bounded-respawn + give-up-alert tests carry over to the launcher-relaunch role.
- **`test_budget.py`** (exists — covers the cap). **Add:** an assertion that a *sequence* of `scheduled`-origin run dirs (triage + N fix-drain items + verify + digest) in one `today_dir()` sums to one capped total — proving the cap spans the phase sequence with no `budget.py` change. This path is otherwise untested for the multi-process-night case.
- **`test_knowledge.py`** (exists — `validate_frontmatter`). **Add:** the three new agent profiles validate, and the three new slugs are in `ALLOWED_AUDIENCE`.
- **NEW `test_overnight_worklist.py`** — the `worklist.md` writer/reader and `item-*.outcome.json` round-trip: a fix-drain agent's row is fully self-contained; the verify stage reconciles the worklist against item outcomes and balances the ARCH-123 completeness tally (N enumerated = fixed + churning + deferred). This contract is the new load-bearing handoff and is **currently uncovered** (no such artifact exists yet).
- **NEW `test_overnight_phase_launcher.sh`** (bash) — the stage-sequencing logic in isolation: no-op (empty worklist → skip fix-drain fan), bounded stage re-spawn (2× then mark-failed-and-continue), triage-fail aborts vs fix-drain-item-fail continues, resume-from-ledger picks the first unsettled stage. Must run under `/bin/bash` (3.2) per the suite contract.

`telemetry.py` and `slack.py` paths the stages touch are in the README's "NOT yet covered" set; any new helper added in `lib/agent-platform/` ships with its own unit test (tests-as-touched).

---

## 11. Open design questions (defaults picked, ARCH-020 style)

1. **Fix-drain grain — per-item vs per-phase batch.** *Default: per-item* (one fresh agent per worklist row). It gives the tightest context cap (one PR's churn per process) and the cleanest idempotency (the item's ✅/PR/defer state is already durable). Per-phase batch would re-accumulate context across items — the failure mode we are removing. *Redirect if* the per-item spawn overhead (one `agentctl spawn` + init per PR) proves material against the token saving; the canary's `orchestrator-tokens` read settles it.
2. **Launcher execution model — foreground-blocking vs detach-and-chain.** *Default: foreground-blocking* (the ARCH-122 `launch-weekly-curation` shape). The launcher holds zero LLM context, so blocking for the night is cheap and makes the sequence deterministic and trivially resumable from the ledger. Detach-and-chain would need a watcher to advance stages — more moving parts for no context saving.
3. **Launcher-death resume — repurpose the resume-watchdog vs a plist `KeepAlive`.** *Default: repurpose `overnight-resume-watchdog`* to relaunch a dead launcher (it already has the singleton + bounded-respawn + alert machinery, and avoids a plist edit). A plist `KeepAlive` is simpler but touches launchd (high-risk) and would restart the launcher on *any* exit, including clean completion. *Flag for the steward* — this is the one structural choice with launchd-adjacent risk.
4. **Distinct stage agents vs one task-framed agent.** *Default: distinct agents* (`overnight-triage`/`overnight-fix-drain`/`overnight-verify`). Distinct profiles are what deliver the context cap at the *system-prompt* layer (three ~80-line prompts vs the 329-line monolith re-read every turn) and give least-privilege tool grants. One task-framed agent would re-load the full prompt per stage, forfeiting much of the win. This is why the roster grows by three (§6).
5. **Digest — reuse `explainer` digest-mode vs a new `overnight-digest` agent.** *Default: reuse `explainer`* (ARCH-018 already routes the morning post through it). No new slug, no behavior change to the digest contract; the only change is its input (`digest-input.md` from verify, vs the orchestrator's in-session digest).
6. **KB-sync placement — fold into verify vs its own stage.** *Default: fold into `overnight-verify`* (it is post-drain batch work that must not starve the real drain, ARCH-048, and verify is already the post-build reconciliation stage). Kept as a distinct internal-phase marker in the ledger so it resumes independently. *Redirect if* KB-sync's `scribe → verify → apply` chain proves heavy enough to warrant its own context.
7. **Worklist scope — unified `worklist.md` vs per-source files.** *Default: one unified `worklist.md`* extending ARCH-123's `bug-fix-worklist.md` to carry bugs + features + product-queue + architect-tractable proposals, each row tagged `source=`. One file is one thing for the launcher to iterate and the verify stage to reconcile.

---

## What this design explicitly does not change

- **`budget.py`** — the per-night cap already spans every `scheduled` run in the day bucket (§1 keystone). Untouched.
- **The plist** — this doc proposes the canary/cutover env flips but does not edit `com.convivy.overnight-orchestrator.plist`; the steward applies them (§9).
- **`mark-fresh` / the ARCH-075 marker contract** — the freshness markers and their daytime writer are unchanged; only *which stage reads which marker* moves.
- **The digest contract (ARCH-018/226)** — same `#overnight-digest` single post, same action affordances; only its producer (verify stage) and input (`digest-input.md`) change.
- **The ARCH-097 mode + per-product day-of-week routing** — the dow `case`, `nightly_product_for_dow`, the Sunday gate, ARCH-164 claim suppression: all kept verbatim.
- **Product code** (`constance`/`vivy`/`sous-chef`), `install.sh`, dependencies, the KB individuation guards — all out of remit.
- **The ARCH-020 no-autonomous-merge contract** — every stage still lands ready-to-merge PRs only; the operator merges in the morning. The phase-split changes *where the work runs*, never the human-in-the-loop gate.
