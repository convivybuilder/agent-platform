# agent-platform

Backup + source-of-truth for the Convivy agent platform's machine-local code.

This repo exists so that the platform — `agentctl` and friends, the knowledge-base
watcher, the daily summary cron, the MCP server bridging the SQLite knowledge
index to Claude Code, the daytime `co` wrapper — survives mini loss and can be
restored on a fresh machine in minutes rather than rebuilt from session history.

It is **not** where agents do their daily work. Product code lives in
`Convivy/{constance,vivy,vivy-ownerrez-sandbox-tools}`; knowledge docs live in
`Convivy/knowledge`; this repo holds the *runtime infrastructure* that operates
on those other three.

## What's in here

```
bin/                       11 platform binaries (Python + bash)
  agentctl                  Spawn / list / status / kill / budget / logs agent runs
  agent-summary             Daily 7am cron — Slack summary of yesterday's runs
  co                        Daytime wrapper: `claude` with the planning-orchestrator system prompt
  knowledge-autopull        Every-10-min cron — `git pull` on ~/Convivy/knowledge if clean
  knowledge-ctx             Individuate per-profile agents + skills into a repo's .claude/
  knowledge-watcher         Live-indexes ~/Convivy/knowledge → ~/.knowledge-index/index.db
  mcp-kb-server             MCP stdio server exposing kb_search / kb_get / kb_list_topics
  notion-migrate            [retired 2026-05-28 — Notion sunset] one-shot Notion → KB migration tool; binary preserved as historical artifact
  worktrees-cleanup         Reap stale agent worktrees with `git worktree remove`
  worktrees-status          Table of every primary + extra worktree across all repos

lib/
  agent-platform/           Shared Python modules imported by bin/
    SCHEMA.md                 Pointer stub → KB `agent-platform/contracts/run-dir-schema`
    budget.py                 Daily cost gating ($30 soft / $80 hard)
    registry.py               ~/agent-runs/active.json with fcntl.flock concurrency
    slack.py                  Webhook posting + Block Kit helpers + Keychain lookup
    telemetry.py              Run-id, meta/outcome/cost JSON writers, validators
    worktrees.py              git worktree allocation / discovery / cleanup
  knowledge/                Knowledge-pipeline helpers
    knowledge.py              Frontmatter validator (mirrors KB SCHEMA.md)
    schema.sql                Canonical SQLite schema for the knowledge index

launchd/                   Three launchd plists (copied to ~/Library/LaunchAgents/)
  com.convivy.knowledge-watcher.plist     Live watcher; RunAtLoad + KeepAlive
  com.convivy.agent-summary.plist         7am daily; StartCalendarInterval
  com.convivy.knowledge-autopull.plist    Every 10min; StartInterval

requirements.txt           Top-level Python deps (mcp, watchdog, PyYAML, requests)
install.sh                 Idempotent installer: venv + symlinks + plist copy
```

`__pycache__/`, `*.pyc`, and `.venv/` are gitignored. Runtime state — `~/agent-runs/`,
`~/.knowledge-index/index.db`, Keychain webhook URLs, Claude Code's MCP registration —
is **not** in this repo by design. Those are per-machine state, not platform code.

## How edits flow

After `install.sh` runs, `~/.local/bin/<binary>` and `~/.local/lib/{agent-platform,knowledge}/<module>`
are symlinks back into this repo. That means:

- Edits to platform code go in **this repo's working tree**, not in `~/.local/`. (You'll be
  editing a symlink target, so it doesn't matter which path you open in your editor — the
  resolved file is the same — but `git status` only works from inside this repo.)
- A live edit takes effect immediately (no install step needed); subsequent agent invocations
  pick it up.
- Commit + push from this repo when the edit is settled.

The `launchd/` plists are an exception: they're **copied** to `~/Library/LaunchAgents/` rather
than symlinked, because `launchctl bootstrap` is finicky about symlinked plist paths. After
editing a plist in this repo, re-run `install.sh` to refresh the copy, then `bootout` + `bootstrap`
to pick up the new config.

## Restore on a fresh mini

```bash
# 1. Tooling prereqs (homebrew, python3.11, gh authenticated as the operator)
# 2. Clone the two repos
git clone https://github.com/convivy/knowledge.git       ~/Convivy/knowledge
git clone https://github.com/convivy/agent-platform.git  ~/Convivy/agent-platform

# 3. Install the platform
bash ~/Convivy/agent-platform/install.sh

# 4. Register the MCP server with Claude Code
claude mcp add kb -- ~/.local/bin/mcp-kb-server

# 5. Provision Slack webhooks
#    (follow ~/Convivy/knowledge/projects/agent-platform/runbooks/agent-slack-webhook-setup.md)

# 6. Activate launchd jobs (commands printed by install.sh at the end)
```

## Operator config

A handful of modules (`memory_archive.py`, `knowledge.py`, `kb_audit.py`,
`velocity-report`, `session-view`, `notion-migrate`) read this machine's operator
identity from one JSON file outside the repo, never from a hardcoded literal:
`$HOME/.config/convivy/operator.json` by default, or the path named by
`$CONVIVY_OPERATOR_CONFIG` (tests point this at a tmp file). See
`config/operator.example.json` for the template; copy it, rename it, and fill in
real values — never commit the real file. Every key is required; a missing or
malformed file makes each caller fail closed (refuse to archive, refuse to
validate, report "unconfigured") rather than fall back to a default.

| Key | Used by |
| --- | --- |
| `excluded_crews` | `memory_archive.py`'s exclusion list — crews whose memories are never archived |
| `operator_marker` | `knowledge.py`'s audience/author marker; `notion-migrate`'s default author |
| `roster_crew` | `knowledge.py`'s roster crew slug (`ALLOWED_CREWS`) |
| `github_login` | `kb_audit.py`'s `EXPECTED_COLLABORATORS` login; `velocity-report`'s `GH_OWNER` mapping |
| `tailnet_host` | `session-view`'s SSH hint |

`agentctl`, `co`, `knowledge-ctx`, etc. are now on PATH and the knowledge watcher is live.

## What's out of scope for this repo

- **Product repos** (constance, vivy, vivy-ownerrez-sandbox-tools, knowledge). They have
  their own remotes.
- **Per-machine state** (run directories, SQLite index, Keychain entries, MCP registrations).
- **Documentation that lives in `convivy/knowledge`** — runbooks (`agent-slack-webhook-setup`,
  `daytime-workflow`, `using-the-knowledge-base`), agent prompts, ADRs (`ARCH-026` etc.). When
  changing prompt behavior, edit there; when changing the harness binaries / launchd, edit here.
- **The dev-setup workshop** (`~/Convivy/dev-setup/`) — that's the design / decisions log; not a
  runtime artifact.

## Provenance

Phase 3 + 4.1 + 4.7 of the dev-setup roadmap built this code; #43 (this repo) was deferred
during initial build and resolved 2026-05-20. Detailed history lives in
`~/Convivy/dev-setup/README.md` Current-Status section and `~/Convivy/dev-setup/decisions.md`.
