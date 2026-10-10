# Alert writing standard (ARCH-313)

Every automated alert this platform posts to Slack must be **self-explanatory to a reader with zero prior context** — the operator, on their phone, who does not remember the contents of 300 architecture documents. An alert that states a metric or names an ARCH number and assumes the reader can fill in the rest has failed.

## What every alert must convey

Four things, woven into plain prose (not labelled fields):

1. **What process fired it** — name the job/check, so the reader knows what's talking.
2. **What that process watches** — and *why* the threshold exists, in one clause. What does the number mean?
3. **What exactly tripped** — the specific condition and its values, **including the sample size** for any rate wherever the underlying data carries it (`92% — 11 of 12`, not a bare `92%`). A few metrics don't yet record their denominator (e.g. `metric-alerts`' `ci-low` reads a pass-rate float from `health.json` with no run count); those stay a bare rate until a data-model update adds the count — a known gap, not a licence to drop sample sizes where the count is available.
4. **What to do** — a concrete next step or command, not "investigate."

## Voice

- **Prose, not labels.** A short bolded/emoji headline sentence, then sentences. No `*What:* … *Triggered:* … *Fix:*` scaffolding — that turns each element into a labelled fragment, the exact robotic tell we're removing.
- **Plain, not dramatic.** State the fact. "higher than usual," not "ran hot"; "12 of 13 merged," not "crushing it." Editorial colour is a missed opportunity for a real number.
- **Self-contained.** Spell out what a lever / threshold / mechanism *is* inline. An ARCH number can be a trailing pointer for the curious, never the explanation itself.
- **Concrete remediation.** Name the actual action ("switch Hollis back to opus-4-8", "revert to the single long-running session"), not "consider reverting."

## Trigger discipline

- **Edge-trigger; don't dump.** An alert fires on a real threshold crossing and is silent otherwise. A metric line posted every run regardless of whether anything is wrong is a report, not an alert — either edge-trigger it (silent unless anomalous) or move it to a dashboard. Silence must mean healthy.
- A genuine **scheduled digest** (the Monday waiting-on-the-operator escalator, the morning overnight digest) is not an alert and stays scheduled — but it still gets this standard's plain, self-contained prose.

## Worked examples

**Good (the model already had one) — subscription spend crossing:**
> 💸 Subscription spend crossed $350 today (API-rate est. now $365.13). Daytime/interactive subscription spend is uncapped by design, so this is a $50-increment heads-up to catch a runaway subagent early. It's the ccusage API-rate estimate (mostly flat under the Max plan), not out-of-pocket. Run `agent-cost-report` for the per-model breakdown.

It names the trigger and value, says why it fires, caveats the estimate, and gives the next command.

**Rewritten — a model-gate breach (was: `hollis model gate: sonnet-4-6 merge 92% vs opus-4-8 100% (>5pts below)`):**
> ⚠️ On Hollis, the cheaper model is merging fewer PRs than the premium one. Over the last 12 Hollis PRs, sonnet-4-6 merged 11 (92%) against opus-4-8's 100%. This check compares the two so we know when a cheaper model stops keeping pace and isn't worth the savings; the gap has passed the 5-point line we flag on. Twelve PRs is a small sample, so one reading isn't decisive — if the gap holds for a few more days, switch Hollis back to opus-4-8. Full breakdown: run `model-scorecard`.

**Rewritten — overnight orchestrator cost, now edge-triggered (was a nightly metric dump):**
> ⚠️ Overnight orchestrator spend was higher than usual last night: $19 across Hollis and sous-chef, above the ~$12–14 single-session baseline we hold it to. This tracks the cost of running the overnight orchestrator as a fresh agent per stage (triage → fix-drain → verify → digest) instead of one long session — a change made for reliability, on the agreement that a sustained overage means switching back. One night over isn't sustained; if it keeps climbing over the next several nights, revert the orchestrator to the single long-running session. History: `orchestrator-tokens-history.jsonl`.

On a normal night (under baseline) that one posts nothing.

## Applying it

The sweep rewrites the platform's alerts to this standard tranche by tranche (ARCH-313). New alerts are written to it from the start. When an alert is a bullet in a grouped digest (e.g. `metric-alerts`), keep each line self-contained — the metric with its sample size and a short what-to-do — rather than a full four-sentence block per bullet.
