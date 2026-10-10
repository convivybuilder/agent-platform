# agent-platform tests (Phase A)

Test + CI foundation for the substrate platform code, per substrate ARCH-098.
Established as the safety net **before** standing up an agent build-crew for
this repo (the platform crew, Phase B) — agent-authored changes to
launchd-critical, bash-3.2 code aren't trusted on review alone (ARCH-082 was a
silent `declare -A` break that killed an overnight).

## Running locally

```bash
python3.11 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest          # python logic tests
/bin/bash tests/bash/run.sh         # bash tests — MUST use /bin/bash (3.2)
/bin/bash tests/bash/lint.sh        # bash syntax + bash-4 (ARCH-082) guard
```

The python tests load the lib modules from THIS checkout (conftest puts
`lib/agent-platform` + `lib/knowledge` on `sys.path`) and the hyphenated `bin/`
CLIs via `importlib` (see `conftest.py`). The bash tests **must** run under
`/bin/bash`: it's the 3.2.57 Apple build on macOS + the mini; Homebrew/Linux
bash is 5.x and would not exercise the 3.2 contract. CI runs the bash job on
`macos-latest` for exactly this reason.

## Live checks (not in CI)

`tests/live/` holds checks that need a real `claude` run and skip unless opted
in. The confinement check runs one dispatched `platform-coder` on haiku against
stand-in credentials, to show the sandbox refuses the reads and writes Option A and
knowledge#793 forbid (Slate a-ow0q4o). It leaves a run record and removes its
worktree:

```bash
AGENTCTL_LIVE_CONFINEMENT=1 .venv/bin/python -m pytest \
    tests/live/test_dispatched_run_confinement.py -s
```

The verdict-path check runs one dispatched `reviewer` pinned to a stand-in PR, and one
`platform-coder` that must be refused when it calls the verdict tool. The Library and
`gh` are stand-ins on 127.0.0.1 and on PATH, so no record reaches the production Library
and no PR is touched (knowledge#793):

```bash
AGENTCTL_LIVE_CONFINEMENT=1 .venv/bin/python -m pytest \
    tests/live/test_reviewer_verdict_path.py -s
```

Tests that start a real tmux server skip inside a dispatched run's sandbox, which
refuses tmux sockets (`tests/tmux_sandbox.py`). CI runs them on its Linux runner.

## What Phase A covers (the high-value seed)

The riskiest logic — the silent-overnight-killers and the dispatch/cost/KB
correctness paths:

- **budget gating** — live spend, in-flight estimate, idempotent crossing
  markers (`test_budget.py`)
- **concurrency cap** — registry per-team worker cap + global ceiling + stale reap (`test_registry.py`)
- **worktree allocation** — path/branch derivation, busy-refusal, porcelain
  parse (`test_worktrees.py`)
- **ARCH-091 cost windows** — `agent-summary` overnight/daytime bounds
  (`test_agent_summary.py`)
- **KB mapping** — `knowledge-ctx` detect_project (the function Phase B extends
  to add agent-platform) (`test_knowledge_ctx.py`)
- **frontmatter validation** — `knowledge.validate_frontmatter` (`test_knowledge.py`)
- **dispatch helpers** — agentctl model_id / derive_purpose / _slug /
  _parse_frontmatter / default_profile (`test_agentctl.py`)
- **cost-shift markers** — `mark-fresh` (`tests/bash/test_mark_fresh.sh`)

## NOT yet covered (grows via tests-as-touched)

Deliberately deferred from the seed — real value, but not the riskiest paths.
The standing rule (enforced by the platform conventions doc, Phase B): every
platform PR adds tests for what it touches.

- `telemetry.py` validation, `slack.py` linkify/chunk, `mcp-kb-server` kb_search
- `knowledge-watcher` parse/reconcile
- `design-room`, thin wrappers (`slack-react`, `worktrees-status`)
- `co` + `knowledge-autopull` are **zsh**, out of the bash-3.2 harness (a
  separate zsh track if needed); `co` is interactive (smoke-only)
- `notion-migrate` — legacy (Notion sunset 2026-05-28); not tested

## Known issue surfaced by the suite

`test_agentctl.py::test_default_profile_sous_chef_known_gap` is an **xfail**:
`agentctl`'s project table (then `PRODUCT_BY_REPO`, now `projects.EXPLICIT_PROJECT_BY_NAME`) was missing `sous-chef` (predates its onboarding),
so `default_profile` falls back to `shared/`. Masked today because callers pass
`--profile` explicitly. Fix the dict alongside the in-flight `--purpose` change
in agentctl; the test flips to xpass.
