# Lab-product template and conformance checklist

This document explains the Convivy lab-product pattern (ARCH-243/177/101) and lists the 8 conditions a new lab app must satisfy to conform to it. The pattern codifies what Personal Assistant proved in practice and what Selector followed as the second instance.

`bin/create-lab-app` in this repo scaffolds the skeleton. Run it first; use this document to verify that the result is fully wired.

## What a lab product is

A lab product is a personal, single-user, system-of-record app in the Convivy suite — Private Assistant owns life-facts, Selector owns music, Sous Chef owns cooking, and new apps extend that pattern. Each owns a domain of the user's data, runs on the mini, and is non-multitenant by design.

The key structural properties, per ARCH-243 and ARCH-101:

- **Private by construction.** The content KB is local and gitignored; it has no path into git, the shared knowledge index, or any network service.
- **First-class agentic deployment.** Each app has its own `agentctl`-dispatched crew, its own telemetry, its own review cycle, and its own overnight drain.
- **Consumes shared infra.** Apps depend on `convivy-app-kit` for the LLM seam, cost guard, and trust-boundary framework (ARCH-249).

The data-locality and trust-boundary invariant that governs the whole suite is the canonical statement at `knowledge/projects/platform/decisions/0006-suite-data-locality-trust-boundary-invariant.md` (ARCH-248). That ADR is the authoritative "why" for the data-locality points in the checklist below.

## The 8-point conformance checklist

A new lab app satisfies the pattern when all 8 of the following hold.

### 1. Repo

The app lives in a private GitHub repo. Its `.gitignore` covers the `data/` directory (the content KB) and all secrets files (tokens, credentials, `.env`). Nothing personal ever enters git.

### 2. Crew

The app has a full crew in `.claude/agents/<prefix>-{architect,planner,coder,reviewer,security-reviewer,overnight-orchestrator}.md`, an orchestrating `CLAUDE.md` charter, and a matching `bin/co` tab-styling entry so the session is visually distinct. The crew is the only path through which the app's code changes; the steward never edits product code directly.

### 3. Dispatch

The app's repo name is registered in **two** tuples in `bin/agentctl`, and they answer different questions. `LOCAL_AGENT_REPOS` makes the crew dispatchable by repo-local resolution: `agentctl` reads agent definitions from `.claude/agents/` in the app's repo and keeps them off the shared knowledge index (ARCH-177). A profile-less dispatch (`<repo>/none`) is the right default. `REPO_LOCAL_DOCS` is what keeps the app's KB local — it is the list `scribe verify` and `scribe apply` refuse against.

They were a single tuple until 2026-08-25, when the content move separated them so a crew could migrate to the Reference Room without also gaining shared-KB write access. **Registering in only `LOCAL_AGENT_REPOS` no longer keeps the KB local**, and nothing fails if you forget the second — the app works and the missing refusal is an absence rather than an error.

### 4. Telemetry

Workers emit run-dirs and `pr-link.json` so the scorecard can harvest PR velocity. The scorecard has a `CI_WORKFLOW[<repo>]` entry and the app has a velocity bucket. Two dedicated Slack channels exist — `#<prefix>-features` (the 🚀 worklist) and `#<prefix>-proposals` (design proposals needing the operator's approval) — and `slack.py` maps the repo to those channels.

### 5. Knowledge split

Engineering docs (ADRs, plans, runbooks, design briefs) go to the shared knowledge base under `knowledge/projects/<app>/`, with the project added to `scripts/validate.py` `ALLOWED_PROJECTS` and the id-regex pattern. Content KB stays local. The app's decision log lives in the app's own `docs/decisions/` ADR sequence, off the shared index, because decisions are engineering docs for this app only.

### 6. Runtime infra

The app depends on `convivy-app-kit` for the `think()` LLM seam, the runtime cost guard, and the trust-boundary API framework (ARCH-249). The `think()` seam is the single provider chokepoint and the BYO-LLM swap point; every model call in every feature goes through it.

### 7. Overnight

The app has its own scoped overnight drain, gated on 🚀-marked items in `#<prefix>-features` (ARCH-243 Option 1 — each lab product gets its own drain, not a slot in the shared constance/vivy rotation). The `<prefix>-overnight-orchestrator` agent drives it; it dispatches the crew, runs the review cycle, and leaves a morning digest. It never modifies code or merges PRs.

### 8. Gates

Every PR the crew opens runs the full review cycle — `<prefix>-reviewer` plus `<prefix>-security-reviewer`, findings posted to the PR via `agentctl review-post`, revise-and-re-review on any substantive finding, within the review-round cap (for a pull request that changes code or any file an agent is instructed to act on, the cap is `review_rounds_code` in the platform's agent policy file, or eleven rounds when that file has no `review_rounds_code` line; for one that changes only documents, the cap is `review_rounds_per_role` in that file) — before it is labeled ready to merge. A pull request that changes code or any file an agent is instructed to act on runs rounds past the document cap only while no finding in the latest round repeats one an earlier round raised; a repeat in a round past the document cap stops the cycle as the cap does, and the PR is left open for the operator. The operator is always the merge gate; no agent merges, ever (ARCH-020).

## Using `bin/create-lab-app`

`bin/create-lab-app <app-name> [--prefix <x>] [--dry-run]` creates the skeleton that satisfies checklist items 1, 2, and (partially) 7 above — the repo structure, `.gitignore`, `CLAUDE.md`, agent files, `docs/build-queue.md`, `docs/decisions/0001-*-creation.md`, `kb/`, `.claude/settings.local.json`, and `git init`. Items 3–8 require touching shared or external systems; the script prints those as a post-creation manual checklist rather than auto-wiring them.

Once the skeleton is created, fill in the `[TODO]` placeholders in `CLAUDE.md` and `docs/build-queue.md`, work through the manual checklist, then open the first `co` session.

## Design references

- ARCH-243 — the lab-product classification: own repo, crew, telemetry, review cycle, local KB
- ARCH-177 — repo-local agent resolution (`LOCAL_AGENT_REPOS`). Since 2026-08-25 the KB-local half
  is `REPO_LOCAL_DOCS`, a separate tuple; ARCH-177 set both at once and this doc described them as one
- ARCH-101 — PA's original content-local rule; the origin of the gitignored `data/` pattern
- ARCH-248 — the decision that consolidated the data-locality rule across all suite apps; the canonical invariant is shared ADR 0006 (`knowledge/projects/platform/decisions/0006-suite-data-locality-trust-boundary-invariant.md`)
- ARCH-249 — the `create-lab-app` scaffold + `convivy-app-kit` platformization work this document is part of
