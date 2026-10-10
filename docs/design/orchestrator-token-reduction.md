# Orchestrator token reduction — scope

**Date:** 2026-06-10 · **Author:** substrate steward · **Status:** A+B approved by the operator 2026-06-10 and shipped (ARCH-129); C gated on the post-A+B measurement
**Relates to:** ARCH-117 (orchestrator Opus→Sonnet), ARCH-122 (deterministic weekly pipeline), ARCH-123 (durable Phase-1→2 worklist), ARCH-055 (don't make load-bearing steps prompt-reliant)

## The problem, quantified

ARCH-117 cut the orchestrator's unit price 5× (Opus→Sonnet), but the nightly cost is back to Opus-era levels because token volume tripled:

| night | model | tokens_in | cost |
|---|---|---|---|
| typical Opus night (May) | opus | 5–12M | $5–10 |
| 2026-06-03 (ARCH-117 trigger) | opus | 68.8M | $40.81 |
| 2026-06-09 | sonnet | 21.9M | $9.65 |
| 2026-06-10 | sonnet | 27.8M | $11.98 |

(06-04 → 06-08 show $0 — the outage window, no clean read.)

The 06-10 transcript decomposes the volume: **573 API turns, 84.1M cache-read tokens, 1.64M uncached input, 147K output**. ~98% of input is prompt-cache re-reads: the orchestrator pays (turns × working-context size) every night, at the cache-read rate. One turn every ~19 seconds over ~3 hours is polling, not reasoning. At current trend this is $300–400/month, and it grows with both the night's activity and the context the orchestrator accumulates.

## Levers, in order of value-for-effort

**A. Cut the request count (quick win, est. 40–60% with B).** The tool-call profile (332 calls: `agentctl wait`×34, `spawn`×32, `status`×17, `logs`×16, run-dir `cat`×11 + `find`×8) shows blocking waits are already used — the waste is one-request-per-call accumulation around ~32 worker dispatches: serial calls that could batch into one message (N parallel calls = one request = one context re-read), one wait per worker instead of per batch, and redundant inspection (`status` after `wait` re-asks what `wait` printed; repeated `cat`/`find` re-reads of artifacts already seen). Prompt-level: batch independent calls, wait on groups, drop redundant inspection. Verify with `orchestrator-tokens` after one night.

**B. Working-set hygiene (quick win, multiplies A).** The other factor is context size. Tool outputs that re-enter context every turn (CI logs, worker transcripts, long `gh` output) should land in files with only a summary line in-context — several already do (ARCH-123's `bug-fix-worklist.md`, `architect-findings.md`). Audit the fattest tool results in the transcript and cap or file-redirect the top offenders. Prompt-level, no platform code.

**C. Phase-split the night (structural, the ARCH-122 pattern).** Replace the single 3-hour conversation with a deterministic launcher dispatching sequential phase agents (triage → fix-drain → verify → digest), each starting with a fresh, small context and reading the prior phase's durable artifacts. ARCH-123 already proved the artifact handoff; ARCH-122 already proved the launcher shape. This caps context size structurally (no 150K working set to re-read 573 times) and removes the compaction failure mode that caused ARCH-123. Real build: launcher script, phase prompts, artifact contracts. Do only if A+B don't get us under target.

**D. Cheaper watcher model.** Largely subsumed by A (blocking waits make a dedicated watcher pointless). Not recommended.

## Proposed sequence

1. **Instrument** — ✅ `bin/orchestrator-tokens` (reproduces the baseline: 573 requests, 84.1M cache reads, avg context 150K, $11.98).
2. **A + B** — ✅ shipped as the ARCH-129 token-discipline section in `overnight-orchestrator.md`. Re-measure after one clean night.
3. **Decision gate:** if cost/night > ~$3 after A+B, build C.

**Target:** <$3/night (~$90/month) at current activity levels.

## What this does not touch

Worker model tiering (already done — deepseek volume path), the daily budget gates (agentctl enforces them regardless), and the quality bar (A and B change *how often the orchestrator re-reads*, not what it reads or decides; C is the only structural change and is gated).
