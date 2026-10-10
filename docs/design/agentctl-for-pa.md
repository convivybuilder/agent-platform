# agentctl-for-PA: reliable dispatch for the personal-assistant dev crew

**Status:** Design proposal (decision-forcing). The architect proposes; nothing here is built.
**Author:** platform-architect
**Date:** 2026-06-16
**Decision owner:** the operator

## The problem

PA's dev crew (`pa-coder`, `pa-reviewer`, `pa-security-reviewer`, `pa-architect`, `pa-planner`, defined in PA's own `.claude/agents/`) dispatches via the **Task tool** — in-process Claude Code subagents — per ARCH-101's "self-contained, not on the substrate" call. The orchestrator backgrounds a reviewer and waits for the Task-tool **completion notification** to deliver the returned `VERDICT`. When a reviewer finishes and dematerializes, that async notification can drop, so the orchestrator never sees the verdict and hangs (a 25-minute hold on PR #166).

The returned verdict is the only fabrication-proof channel: `pa-coder` has unrestricted `Bash` and can forge `gh pr comment` approvals (the "#86" incident), so PA's CLAUDE.md correctly forbids trusting a PR comment as evidence of review. That leaves the Task return value as the sole trustworthy signal, and the Task return value is exactly what drops.

`agentctl` already solved this on the product path. `agentctl wait` is a **foreground, poll-based** completion mechanism (ARCH-070): it blocks on the presence of `outcome.json`, written by `_finalize_run` and guaranteed even for a lingering process by the ARCH-069 reaper. No async notification, nothing to drop. The verdict is recovered the same way `cmd_scribe_verify` already recovers a `kb-sync-verifier` verdict: `_parse_verifier_verdict` scans the finalized transcript for the `VERDICT:` line. The transcript is written by agentctl's own stream loop, not by the agent's `Bash`, so it is as fabrication-proof as the Task return value — and it survives the run.

PA also emits **zero run telemetry** today. The model-scorecard harvests it GitHub-only (`--repo-scorecard personal-assistant`, reading the `model:` PR label per ARCH-169), so PA's dev-crew cost and per-agent quality are invisible.

## What couples agentctl to the shared knowledge index (and what doesn't)

I read the real code. The coupling is narrow:

| Concern | Coupled to `~/Convivy/knowledge` / `~/.knowledge-index`? |
|---|---|
| `resolve_agent` | **YES.** Queries `~/.knowledge-index/index.db` (`kind='agent'`), reads source from `~/Convivy/knowledge/`. **This is the only hard coupling.** |
| `activate_profile` (`knowledge-ctx use --force`) | **YES, indirectly.** Individuates a knowledge-ctx *profile* into the worktree. PA has no profile and must never get one. |
| `default_profile` | Soft. Keyed on `projects.resolve_project`, which reads `projects.EXPLICIT_PROJECT_BY_NAME` and then a `projects/<repo>/` directory; PA is absent from both, so `default_profile` refuses it (exit 1) unless the knowledge checkout holds `projects/platform/profiles/default.yaml`, which then serves it. |
| worktree allocation (`worktrees.py`) | **NO.** Pure git plumbing under `~/Convivy/`. PA is already a depth-1 primary that `discover_primaries()` finds; `~/Convivy/worktrees/personal-assistant.<purpose>` fits the scheme unchanged. |
| telemetry / run-dirs (`telemetry.py`) | **NO.** Run-dirs land in `~/Convivy/agent-runs/`; nothing knowledge-indexed. |
| `cmd_wait`, `cmd_publish`, `cmd_pr_open`, budget, registry | **NO.** All repo-agnostic, keyed on a `repo` string. |
| scribe / kb-sync loop | **NO auto-enrollment.** `scribe verify` / `scribe apply` run **only** when explicitly invoked with a patch. Nothing walks a repo list and enrolls PA. |

The takeaway: migrating PA onto agentctl is almost entirely free of the index. Exactly one function, `resolve_agent`, must learn to find PA's local agents, and one function, `activate_profile`, must be skipped for PA.

---

## The central decision: agent resolution

PA's agents live in `$HOME/Convivy/personal-assistant/.claude/agents/*.md` and are **deliberately not** in the shared index (ARCH-101). The shared index feeds `team.convivy.com/orient`, where contractors have a tier. Pushing PA's agent defs there is a PII-adjacent boundary breach in itself (it leaks PA's internal structure into a contractor-visible surface), independent of run transcripts. So the agents must stay out of the index, and agentctl must resolve them from PA's local `.claude/agents/` instead.

The frontmatter is already compatible: `pa-coder.md` etc. carry `model:` and `tools:` in exactly the form `_parse_frontmatter` reads. Only the *lookup* needs to change.

### Option A — `--agents-dir` flag: explicit repo-local agent source

Add `--agents-dir PATH` to `spawn` (and `scribe verify`). When passed, `resolve_agent` reads `<dir>/<name>.md` directly off disk and skips the index entirely.

```
agentctl spawn pa-reviewer --on personal-assistant \
    --agents-dir ~/Convivy/personal-assistant/.claude/agents \
    --task "Review PR #167" --detach
```

- **Pros:** Smallest change. Zero index involvement — the resolver never touches the DB on this path, so there is no vector by construction. Fully explicit: the caller names where agents come from. No new config, no new index, no watcher. The fail-closed property is trivial (no flag → today's behavior, byte-identical).
- **Cons:** The caller must pass the path every time. Mitigated by PA's orchestrator wrapping it (the path is a constant), but it is boilerplate, and a forgotten flag falls back to the shared index and errors (`agent 'pa-reviewer' not found in knowledge index`) — a loud, safe failure, not a silent one.

### Option B — repo-local resolution by convention (`<repo>/.claude/agents/` first, index fallback)

`resolve_agent` takes the `repo` it is spawning on. It first looks for `~/Convivy/<repo>/.claude/agents/<name>.md`; if found, it reads it off disk and returns, never touching the index. Only if absent does it fall through to the index query.

- **Pros:** No new flag; the orchestrator's command line stays clean. Generalizes: any repo with a local `.claude/agents/` (a future second internal app) gets the same treatment for free. PA's agents stay 100% out of the index with zero ceremony.
- **Cons:** Resolution order is implicit. A product agent (`coder`) spawned `--on personal-assistant` would resolve to PA's roster if a name collided — but PA's names are `pa-`-prefixed and disjoint from the product roster, so no real collision exists. The behavior is "local repo agents shadow the index, scoped to that repo," which is a clean rule but one more rule. Resolution now depends on `repo`, so the `--on` value must be correct before the agent is resolved (it already is — `cmd_spawn` resolves `repo = args.on` first).

### Option C — a separate PA-scoped index

Stand up a second SQLite index for PA, mirroring the shared one's `kind='agent'` rows, indexed by a PA-local watcher, queried when `--on personal-assistant`.

- **Pros:** Symmetric with the shared path; PA gets the same indexed-lookup machinery.
- **Cons:** Heaviest by far. A second watcher, a second DB, a second schema to keep in sync, for five agent files that change rarely. It rebuilds the index coupling PA was designed to avoid, just pointed at a different file. No upside over reading the five files directly. **Reject.**

### Recommendation: **Option B**, with Option A's flag as an explicit override

Option B is the right default. PA's agents stay out of the shared index with no caller ceremony, the rule ("a repo's local `.claude/agents/` shadows the index for spawns on that repo") is clean and generalizes to the next internal app, and the index is never queried on the PA path — so the PII-into-orient vector is closed by construction, not by discipline.

Keep Option A's `--agents-dir` as an explicit override for the rare case where the agents aren't at the conventional path (or to force a specific source in a test). It costs one optional flag and makes the resolution source auditable when needed.

Concretely, `resolve_agent` gains a `repo` parameter and this order:
1. If `--agents-dir` was passed, read `<dir>/<name>.md`; error if absent.
2. Else if `~/Convivy/<repo>/.claude/agents/<name>.md` exists, read it; return.
3. Else query the shared index (today's path, unchanged).

Step 2's existence check is the entire boundary mechanism. When it hits, the function returns before any `sqlite3.connect`, so PA spawns never read — and a fortiori never write — the shared index.

---

## Data-boundary guarantees (the crux — each PII vector, explicitly closed)

ARCH-101's worry is PA's PII (personal emails, calendar, reminders) reaching the shared, contractor-visible knowledge base. Three vectors, each closed:

### 1. Agent resolution → never touches the shared index

Closed by Option B above. A spawn `--on personal-assistant` resolves its agent from `~/Convivy/personal-assistant/.claude/agents/` and returns before the index query runs. PA's agent definitions are never read from, and never written to, `~/Convivy/knowledge` or `~/.knowledge-index`. They stay off `team.convivy.com/orient`. **Guardrail:** the existence check at `~/Convivy/<repo>/.claude/agents/<name>.md` must run *before* `sqlite3.connect` — do NOT connect first and fall back, or a missing-file race could leak the query.

### 2. KB-sync / scribe loop → PA is never enrolled

This is the real PII-into-KB vector: `agentctl scribe verify` → `scribe apply` is the path that writes run-derived facts into shared, indexed KB docs under `~/Convivy/knowledge`. The design keeps PA out of it, and the code already makes this the default:

- `scribe verify` and `scribe apply` run **only** on explicit invocation with a patch sidecar. Nothing in agentctl walks a repo list and auto-enrolls a repo. PA stays out simply by never being invoked — no opt-out flag is needed, because there is no opt-in.
- **Guardrail (defense in depth):** `cmd_scribe_apply` already refuses any target outside the knowledge root. Add one more refusal: **reject `repo == "personal-assistant"` (and any repo with a local `.claude/agents/`) at the top of both `scribe verify` and `scribe apply`.** This makes "PA never reaches the scribe cage" a mechanical invariant, not a convention someone could forget. PA's own knowledge is its in-repo `docs/` tree (ARCH-101); it has no business in the shared KB.
- PA's `model:`-label harvest path (ARCH-169) is untouched and reads only GitHub PR metadata, never PA's `data/`.

### 3. Run artifacts → shared run-dir is acceptable; enforce a synthetic-fixtures rule

agentctl run-dirs land in `~/Convivy/agent-runs/<date>/<run-id>/` (local mini, shared across products). This dir holds `transcript.jsonl`, `tool_calls.jsonl`, `meta.json`, `cost.json`. **`~/Convivy/agent-runs/` is local-only and not indexed** — it is not the shared *knowledge* base, it never syncs to a remote, and the knowledge-watcher does not walk it. So a PA run-dir there does not cross the ARCH-101 boundary the way a KB doc would.

The residual risk is a transcript *carrying* real PII (a reviewer reads a fixture that happens to be the operator's real inbox). Two controls:

- **Recommended rule (put in PA's CLAUDE.md + `docs/conventions.md`): the dev crew operates on synthetic fixtures only, never real `data/`.** PA's `data/` is gitignored and personal; a *dev* task (write code, review a diff, run tests) has no reason to read it, and tests should run against committed synthetic fixtures. Make this a hard convention so a transcript can't carry personal data in the first place. This is the cleanest control: it removes the PII at the source rather than trying to scrub artifacts.
- **Decision on run-dir location:** keep PA run-dirs in the shared `~/Convivy/agent-runs/` (don't scope them separately). The dir is already local-only and unindexed, separating it buys little, and it keeps `cmd_wait`/`cmd_status`/`_find_run_dir`/the scorecard harvester all working unchanged. The synthetic-fixtures rule, not run-dir relocation, is the real safeguard.

**One more guardrail worth stating for the LLM reader:** PA's `pa-coder` has unrestricted `Bash`. Under agentctl it runs in a worktree exactly as product `coder` does, but the synthetic-fixtures rule must be in its prompt as a `do NOT read real data/` instruction, so the agent itself won't pull PII into a transcript.

---

## What changes, what stays the same

### Changes in agentctl (small, additive, fail-closed)

1. **`resolve_agent(name, repo, agents_dir=None)`** — add the repo-local lookup (Option B) and the `--agents-dir` override (Option A). Index path unchanged when neither hits. (~15 lines.)
2. **`--agents-dir` flag** on `spawn` (and `scribe verify`, for symmetry, though PA never uses scribe). Optional; absent → today's behavior.
3. **Skip `activate_profile` for a repo-local-agent spawn.** PA has no knowledge-ctx profile and must not get one individuated into its worktree — and skipping it sidesteps the ARCH-135/ARCH-080 live-process collision entirely (the very class of failure that breaks daytime product dispatch). Gate: if the agent resolved from a repo-local source (no profile concept), do NOT call `activate_profile`; the worktree is used as-is. This also means `default_profile` is not called for PA: `_spawn_dispatch` takes the `<repo>/none` sentinel for any agent resolved from a repo-local source (`bin/agentctl:4370`), so PA needs no `personal-assistant` entry in `projects.EXPLICIT_PROJECT_BY_NAME`. Without that gate, `default_profile` would refuse PA with exit 1 on a checkout that has no `projects/platform/profiles/default.yaml`. Store a sentinel profile (e.g. `personal-assistant/none`) in `meta.json` for the scorecard's sake.
4. **Scribe hard-refusal of `personal-assistant`** (guarantee 2 above).
5. **Worktree-per-reviewer** comes for free: `derive_purpose` already routes `reviewer` to a dedicated worktree (`_DEDICATED_WORKTREE_WORKERS`). PA's `pa-reviewer` won't match that name; either add `pa-`-prefixed names to the set, or (cleaner) match the *resolved* agent's role. Low-stakes; PA reviewers are read-only and can share refs. Decide at build time.

### Changes in PA's CLAUDE.md / orchestrator

- Replace "dispatch with the Task tool (`subagent_type: pa-coder`)" with `agentctl spawn pa-coder --on personal-assistant --detach` + foreground `agentctl wait <run-id>` (ARCH-070).
- The verdict is recovered from the finalized transcript (the agentctl-written, fabrication-proof channel), exactly as `cmd_scribe_verify` does it. Keep the existing rule that a `gh pr comment` is never evidence; now the trustworthy channel is the transcript instead of the Task return.
- Add the **synthetic-fixtures-only** convention to CLAUDE.md and `docs/conventions.md`, and a `do NOT read real data/` line to `pa-coder` / `pa-reviewer` prompts.
- The orchestrator already stamps the `model:` PR label (ARCH-169); keep it. With telemetry now flowing, the scorecard can later join on the run-dir too, but the label stays as the GitHub-only fallback.

### Stays the same

- ARCH-101's boundary: PA agents + `data/` + in-repo `docs/` stay off the substrate index. This design *strengthens* that (a mechanical scribe-refusal where there was only convention).
- The whole spawn → stream → reap → finalize pipeline, `wait`, `publish`, `pr-open`, budget, registry, worktree allocation — used unchanged.
- The no-autonomous-merge gate (ARCH-020/100), the operator as merge gate.

---

## Migration path (incremental, each step independently shippable)

1. **agentctl `resolve_agent` + `--agents-dir` + repo-local lookup**, with a unit test that a spawn `--on personal-assistant` never opens the index (assert no DB connection). Ships behind the new path; product spawns byte-identical.
2. **Skip `activate_profile` for repo-local agents**; sentinel profile in meta. Test a PA `--dry-run` spawn produces a run-dir with no knowledge-ctx individuation.
3. **Scribe hard-refusal of `personal-assistant`.** Tiny, pure guard; test it refuses.
4. **One real PA spawn end-to-end** (`pa-reviewer` on a throwaway PR, `--detach` + `agentctl wait`), confirm `outcome.json` + transcript-verdict recovery, confirm `~/.knowledge-index` and `~/Convivy/knowledge` are untouched (git status clean, no new index rows).
5. **Flip PA's orchestrator** in CLAUDE.md from Task to agentctl; add the synthetic-fixtures convention. This is PA-repo work — coordinate, since there are live PA `co` sessions (auto-memory `feedback_active_claude_sessions`).
6. **Wire PA into the telemetry scorecard** (the run-join path now has data); keep the `model:`-label path as fallback.

Steps 1–4 are pure agent-platform changes the steward owns and can land via the normal review/revise cycle. Step 5 touches PA and must be coordinated. The Task path keeps working throughout, so this is a no-flag-day migration: PA can run both ways until step 5 flips it.

---

## Risks / open questions for the operator

1. **Approve Option B (repo-local resolution by convention) over Option A (explicit `--agents-dir` every call)?** B is my recommendation — clean default, generalizes, index untouched by construction. A is available as the override. (This is the one decision that shapes everything else.)
2. **Synthetic-fixtures-only for the PA dev crew — agree to make it a hard convention?** This is the load-bearing PII control on run artifacts. If PA dev tasks genuinely need to read real `data/` (I don't think they should), the run-dir story needs more (scoped run-dir + scrub), and we should talk.
3. **Mechanical scribe-refusal of `personal-assistant`** — agree to add the belt-and-suspenders guard, even though nothing auto-enrolls PA today? I recommend yes; it converts a convention into an invariant for ~3 lines.
4. **`MAX_CONCURRENT = 4` is shared across all products.** A PA review cycle now consumes slots from the same pool as product overnight work. PA work is interactive/daytime and bursty, so collisions should be rare, but if a PA cycle and a product overnight wave overlap, one waits. Acceptable, or does PA want its own budget/concurrency scope? (I'd leave it shared and revisit only if it bites.)
5. **Worktree-per-`pa-reviewer`:** match by `pa-`-prefixed name or by resolved role? Trivial; flagging only so it's a conscious choice at build time.
6. **PA's budget origin.** PA spawns from an interactive `co` session inherit `origin=interactive` (uncapped, tracked) per ARCH-156 — correct, since PA dev work is human-in-the-loop. No change needed; noting it so the budget behavior is understood.
