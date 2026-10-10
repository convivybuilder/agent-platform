"""Telemetry helpers for the agent platform.

Stateless, stdlib-only. Reads/writes the per-run directory structure under
<root>/agent-runs/<YYYY-MM-DD>/<run-id>/, defined by the KB contract
`agent-platform/contracts/run-dir-schema` (SCHEMA.md here is a pointer stub).

Used by `agentctl` (3.3) and any downstream tool that needs to read run data
(daily summary 3.6, cost monitor 3.7, explainer auto-trigger 3.9).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, IO, Iterable

import builder_root as BR
RUNS_ROOT = BR.runs_root()

REQUIRED_META_FIELDS = (
    "run_id", "agent", "task", "started_at", "repo", "worktree", "profile", "model",
)
REQUIRED_RUN_FILES = (
    "meta.json",
    "prompts/system.md",
    "prompts/user.md",
    "transcript.jsonl",
    "tool_calls.jsonl",
    "docs_consulted.json",
    "cost.json",
    "outcome.json",
)
VALID_EXIT_STATUSES = {"completed", "killed", "errored", "budget_refused", "running",
                       # a wave the resume-watchdog superseded by re-spawning the
                       # orchestrator after it died mid-drain (ARCH-160) — terminal,
                       # not an error; the 05:00 healthcheck treats it as fine.
                       "superseded",
                       # a worker (coder, bug-fixer) that committed its fix but exited
                       # before the orchestrator called `agentctl publish` — the work
                       # exists only in the local worktree and the PR head never moved.
                       # Detected by _finalize_run; distinct from "errored" so scorecards
                       # can attribute the failure to the publish-contract gap, not a
                       # general run error. TRANSIENT: agentctl publish --run <id>
                       # promotes this to "completed" after a verified push, so only
                       # runs where publish was never called retain this status.
                       "committed_not_pushed",
                       # a worker that committed in a repo with no remote at all
                       # (issue_01M2EKF1YCM97QDTZ2EHS6W5NX). `agentctl publish` has
                       # nowhere to push to there, so this names the work "local by
                       # design" instead of asking for a publish that can't happen.
                       # Distinct from committed_not_pushed and, unlike it, not
                       # promoted by `agentctl publish` — there is no push to verify.
                       "committed_local_only",
                       # spawn refused after retrying the concurrency admission for
                       # the full back-off budget — distinct from "errored" so callers
                       # (fix-drain, review-status) can tell "never started" from "ran
                       # and crashed". The error_reason field in outcome.json names the
                       # breached limit and how long retries ran.
                       "capacity_refused",
                       # the run wrote into a PRIMARY checkout it does not own —
                       # files appeared there, pre-existing uncommitted work was
                       # destroyed, or HEAD moved (issue_01KZ7E0YNV84W55796CZ8ETX6X:
                       # 56 such runs across 29 days, 15 agent types and 7 repos,
                       # 8 of them irrecoverable git operations). Distinct from
                       # "errored" because the run's own work may be perfectly fine
                       # and the damage is somewhere else entirely, which is what
                       # made it invisible for a month. TERMINAL, unlike
                       # committed_not_pushed which it overrides: publish must not
                       # promote a run that touched a checkout it does not own.
                       "primary_mutated"}
# Subscription rate-limit states, in SEVERITY ORDER — the list is the ordering,
# so `RATE_LIMIT_SEVERITY.index()` is what compares two of them and there is no
# second copy to drift. Values observed across every transcript on the box:
#   allowed         — inside the window
#   allowed_warning — inside it, past the warning threshold
#   rejected        — the window is full and the request was turned away
RATE_LIMIT_SEVERITY = ("allowed", "allowed_warning", "rejected")
VALID_RATE_LIMIT_STATUSES = frozenset(RATE_LIMIT_SEVERITY)

VALID_BILLING_SOURCES = {
    "subscription_credit", "api_fallback", "interactive", "unknown",
    # Phase C gateway providers (ARCH-102); adding a provider here forces an
    # explicit decision and catches typos at write_meta time.
    "provider_api:openai", "provider_api:deepseek", "provider_api:moonshot",
    "provider_api:openrouter", "provider_api:zai",
    # NOTE: the mimo trial model (ARCH-201) routes via litellm's openai handler
    # to Xiaomi's base, so it bills as provider_api:openai (already listed), not
    # a separate provider_api:xiaomi. glm (ARCH-201) uses the native zai/ provider
    # → provider_api:zai, added above.
    # A run on the Codex CLI, priced at OpenAI's API rates (the operator, 2026-09-23). Not
    # `provider_api:openai`, which is the LiteLLM gateway's metered lane.
    "codex_api",
}
CODEX_BILLING_SOURCE = "codex_api"

VALID_COST_SOURCES = {"anthropic_cli", "gateway_spend_log", "estimate_fallback",
                      "codex_turn_usage"}

# Claude list pricing, USD per million tokens (ARCH-239). Source: the live
# pricing page, https://platform.claude.com/docs/en/about-claude/pricing (fetched
# 2026-09-24), + the prompt-caching multipliers (write = 1.25x input for the
# 5-minute TTL the harness uses; read = 0.1x input, with two exceptions: Fable 5.1
# and Mythos 5.1 read at 0.025x input (page footnote 1), and Opus 5.5 reads at
# 0.05x input, $0.20 on $4 (footnote 2)). `cache_write` is the 5-minute TTL rate;
# `cache_write_1h` is the 1-hour TTL rate (2x input), re-fetched from the same
# page's "1h cache writes" column on 2026-10-07 — Claude Code on a subscription
# writes at the 1-hour TTL, billed separately from the 5-minute rate (see
# cost_from_usage). Used by cost_from_usage
# to price Task-tool subagents, which (unlike agentctl's `claude -p` runs) don't
# self-report a `usd` figure — only token usage. Cost is computed and FROZEN into
# the run-dir at emit time so a later price change can't rewrite historical runs
# (the operator's call). Keyed on the bare model family; resolve_pricing() strips the
# `[1m]` / dated-snapshot suffixes before lookup. Keep in sync at each price change.
# A model absent from this table is NOT free: cost_from_usage marks it `unpriced`
# with no dollar figure, and every consumer that totals dollars shows it as
# unmeasured (issue_01M3BH6C6VK73R8HSF0TN88C6T).
CLAUDE_PRICING_PER_MTOK = {
    # Fable 5.1 and Mythos 5.1 share one price; a `claude-fable-5-1` dispatch
    # (`agentctl spawn --model claude-fable-5-1 --reason ...`) priced at $0 until
    # these rows landed.
    "claude-fable-5-1":  {"input": 10.0, "output": 50.0, "cache_write": 12.50, "cache_write_1h": 20.0, "cache_read": 0.25},
    "claude-mythos-5-1": {"input": 10.0, "output": 50.0, "cache_write": 12.50, "cache_write_1h": 20.0, "cache_read": 0.25},
    # Fable 5 is the top tier at 2x Opus, and ARCH-134 exists because an unpinned
    # Fable default burned a subscription unnoticed. Until this row landed it was
    # the one model the table did not price, so every Fable run recorded usd=0.0 —
    # the cost guards read zero as free rather than as unmeasured, which is the
    # exact blind spot ARCH-134 was written about. Standard list, not the
    # promotional rate in effect at time of writing (same reasoning as Sonnet 5
    # below). Dispatch stays a deliberate per-session operator call.
    "claude-fable-5":    {"input": 10.0, "output": 50.0, "cache_write": 12.50, "cache_write_1h": 20.0, "cache_read": 1.00},
    # Opus 5 is the interactive-co / roster Opus tier (supersedes 4.8; same list
    # price). 4.8 stays priced below so historical runs recorded on it still cost.
    "claude-opus-5":     {"input": 5.0, "output": 25.0, "cache_write": 6.25, "cache_write_1h": 10.0, "cache_read": 0.50},
    "claude-opus-4-8":   {"input": 5.0, "output": 25.0, "cache_write": 6.25, "cache_write_1h": 10.0, "cache_read": 0.50},
    # Opus 5.5 was missing from this table, so every session on it priced at $0.
    "claude-opus-5-5":   {"input": 4.0, "output": 20.0, "cache_write": 5.00, "cache_write_1h": 8.0, "cache_read": 0.20},
    # Sonnet 5 (ARCH-282) is the product-co main-loop tier. The pricing page's
    # footnote 3 says the introductory $2/$10 became standard and the $3/$15 rise
    # will not occur; the row previously carried $3/$15 and read 50% high. Do not
    # revert it to $3/$15.
    "claude-sonnet-5":   {"input": 2.0, "output": 10.0, "cache_write": 2.50, "cache_write_1h": 4.0, "cache_read": 0.20},
    # Sonnet 5.5 had no row at all, so every run on it (the review-model eval's
    # baseline) priced as unpriced/None. Read from the live pricing page on
    # 2026-10-07: base input $2, 5-minute cache write $2.50, 1-hour cache write
    # $4, output $10. The page disagrees with itself on cache reads: the model
    # table gives $0.20 (the standard 0.1x input, same as claude-sonnet-5), but
    # the prompt-caching section gives 0.05x ("$0.10 USD on Claude Sonnet 5.5").
    # This row uses the model table's $0.20 — the same rate every other
    # non-footnoted row uses — rather than the prompt-caching section's figure.
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_write": 2.50, "cache_write_1h": 4.0, "cache_read": 0.20},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_write_1h": 6.0, "cache_read": 0.30},
    "claude-haiku-4-5":  {"input": 1.0, "output":  5.0, "cache_write": 1.25, "cache_write_1h": 2.0, "cache_read": 0.10},
}

# OpenAI API list pricing for the models the Codex CLI serves, USD per million
# tokens, read from developers.openai.com/api/docs/pricing on 2026-09-23. Rows
# carry the Claude table's four keys so `cost_from_usage` prices them; OpenAI's
# caching is automatic with no write premium, so `cache_write` is the plain input
# rate. `gpt-reserve` and `codex-auto-review` publish no rate and have NO row on
# purpose: a missing row is refused at dispatch (`cost_from_codex_usage` raises),
# where a zero row would record every run of them as free.
CODEX_PRICING_PER_MTOK = {
    "gpt-6-astra":   {"input": 10.00, "output": 50.00, "cache_write": 10.00, "cache_read": 1.00},
    "gpt-5.6-sol":   {"input":  4.00, "output": 20.00, "cache_write":  4.00, "cache_read": 0.40},
    "gpt-5.6-terra": {"input":  2.00, "output": 12.00, "cache_write":  2.00, "cache_read": 0.20},
    "gpt-5.6-luna":  {"input":  0.20, "output":  1.20, "cache_write":  0.20, "cache_read": 0.02},
    "gpt-5.5":       {"input":  5.00, "output": 30.00, "cache_write":  5.00, "cache_read": 0.50},
}

# A run's origin: who initiated the spawn (ARCH-156). `scheduled` = a launchd
# launcher (overnight, weekly-consulting) set AGENT_RUN_ORIGIN in the env, which
# propagates through every descendant agentctl spawn; `curation` = the weekly
# curation launcher, split into its own budget pool so a heavy overnight can't
# starve it (ARCH-320); `interactive` = dispatched from a human `co` session (the
# env-unset default); `full_monty` = a deliberate subscription-headroom burn
# driven by `agentctl full-monty` (ARCH-202). The daily budget caps gate on the
# AUTONOMOUS origins (`scheduled` + `full_monty` + `curation`) — interactive spend
# is the operator's deliberate, in-the-loop work and must not trip the autonomous-runaway
# tripwire. Each capped origin is gated against its POOL's cap (budget.POOL_ORIGINS):
# `scheduled`/`full_monty` share the `overnight` pool, `curation` has its own.
VALID_ORIGINS = {"scheduled", "interactive", "full_monty", "curation"}
DEFAULT_ORIGIN = "interactive"

# How a run's model was resolved (model-lane epic issue_01KZH2GYGTVHFXZ055GEZVH2ZJ,
# issue_01M174CH86JBCP7291PS0TM097): one field rather than a boolean, because the
# epic's definition of done requires telling a harness fail-over apart from an
# override, which a boolean can't. `roster_default` is the agent's `model:`
# frontmatter, unaltered. An accepted, model-changing `--model` is one of three
# provenance values (issue_01M3NTZP9W8WEYWVERW2F9NARP), most specific first:
# `item_override` read off a work item's model_choice through the Library's
# client-gated route; `co_override` when the dispatching process's own
# environment carried CLAUDECODE (an agent session); `typed_override` otherwise
# (a person at a terminal, claimed rather than proven). `operator_override` is
# the pre-provenance value written before that change landed — kept valid
# because meta.json and Archives records written before it still carry it, but
# nothing writes it anymore. `harness_fallback` is the ARCH-131/
# `_respawn_in_place` cross-provider fail-over; `full_monty_forced` is the
# ARCH-202 burn forcing a gateway model onto sonnet.
VALID_MODEL_CHOICES = {
    "roster_default", "item_override", "co_override", "typed_override",
    "operator_override", "harness_fallback", "full_monty_forced",
}

# Where a run's per-run dollar cap came from (the operator's 2026-10-07 ruling, slate
# a-1ommg6; agentctl `_run_cost_cap_for_agent`). `grant` is a Library checkout
# grant's own `budget.cap_usd` (which may itself be `None` — the Library
# deciding there is no cap is still a grant-sourced decision). `default` and
# `env` are `_run_cost_cap`'s existing DEFAULT_RUN_COST_CAP_USD fallback and
# AGENTCTL_RUN_COST_CAP override, for a dispatch with no grant (it doesn't call
# checkout yet). `none` is a subscription model with no grant — no dollar cap
# applies; the daily budget gate covers it instead.
VALID_COST_CAP_SOURCES = {"grant", "default", "env", "none"}


def now_iso() -> str:
    """Return current UTC time in ISO-8601 with seconds precision + Z suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def platform_day() -> str:
    """The platform's run-dir + budget day key, in LOCAL time (ARCH-156).

    Deliberately local, not UTC: the run-dir date buckets the daily budget, and
    a UTC key rolls the day at 17:00 MST, folding an evening's interactive co
    work into the *next* day's overnight-budget pool (the 2026-06-14 false
    soft-cap crossing). A local-day key keeps a calendar day's spend in one
    bucket. `agent-summary` already fires on `datetime.now().astimezone().date()`,
    so a local run-dir key also aligns the daily summary with the dirs it reads.

    Timestamps stay UTC (`now_iso`); only the day *bucket* is local.
    """
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def today_dir() -> str:
    return platform_day()


def new_run_id() -> str:
    """8 lowercase hex chars: 16**8 ≈ 4.3B values; same-day collision risk is nil.

    Hex, not base32. This previously read "base32-ish ... ~1.1T", which is 32**8 —
    the base32 keyspace, off by ~256x. Worth naming because the wrong figure had
    already been copied into the schema doc.
    """
    return f"run-{secrets.token_hex(4)}"


def run_dir(run_id: str, *, date: str | None = None) -> Path:
    """Resolve the on-disk directory for a run."""
    return RUNS_ROOT / (date or today_dir()) / run_id


# --- owner-only write primitives ----------------------------------------------


def write_owner_only(path: Path, content: str | bytes, *, append: bool = False) -> None:
    """Write content to path at mode 0600, immune to umask.

    Uses os.fchmod rather than relying on the mode argument to os.open, which
    is umask-masked. Corrects the mode even when the file pre-existed at 0644.
    `append=True` opens without truncating, for log files with multiple writers.
    O_NOFOLLOW refuses to follow a symlink at the final path component.
    """
    flags = os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW | (os.O_APPEND if append else os.O_TRUNC)
    data = content.encode() if isinstance(content, str) else content
    fd = os.open(str(path), flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, data)
    finally:
        os.close(fd)


def open_owner_only(path: Path, *, append: bool = False) -> IO[bytes]:
    """Open path at mode 0600 for binary writing; returns a file object.

    Suitable for subprocess.Popen's stderr parameter. Caller owns the returned
    file object. Uses os.fchmod so the mode is not umask-masked.
    O_NOFOLLOW refuses to follow a symlink at the final path component.
    """
    flags = os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW | (os.O_APPEND if append else os.O_TRUNC)
    fd = os.open(str(path), flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, "ab" if append else "wb")
    except BaseException:
        os.close(fd)
        raise


# --- creation -----------------------------------------------------------------


def _ensure_runs_root() -> None:
    """Create RUNS_ROOT if absent and pin it to 0700.

    The SessionStart hook also does this, but autonomous dispatches (scheduled,
    launchd) never fire that hook. Calling this from create_run_dir closes the
    gap: the traversal gate is set on the first run regardless of how the session
    started.
    """
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    # Assumes single-user ownership of RUNS_ROOT. On a shared host where RUNS_ROOT
    # was created by another user, os.chmod raises PermissionError here — surfacing
    # the misconfiguration rather than silently continuing.
    os.chmod(RUNS_ROOT, 0o700)


def create_run_dir(run_id: str, *, date: str | None = None) -> Path:
    """Make the run directory + initialize empty append-files and a prompts/ dir."""
    _ensure_runs_root()
    d = run_dir(run_id, date=date)
    d.mkdir(parents=True, exist_ok=False)
    os.chmod(d, 0o700)
    (d / "prompts").mkdir()
    os.chmod(d / "prompts", 0o700)
    # Initialize append-only files at 0o600 so a credential in a tool result
    # is not readable by other users on a shared host. write_owner_only uses
    # fchmod rather than relying on os.open's umask-masked mode argument.
    for name in ("transcript.jsonl", "tool_calls.jsonl"):
        write_owner_only(d / name, b"")
    return d


# --- write paths --------------------------------------------------------------


def write_meta(d: Path, **fields: Any) -> None:
    """Write meta.json with required fields enforced.

    `ended_at`, `parent_run`, `pid`, `billing_source` default to sensible
    placeholders so the file is always shape-complete (consumers don't have to
    handle missing keys).
    """
    missing = [f for f in REQUIRED_META_FIELDS if f not in fields]
    if missing:
        raise ValueError(f"meta.json missing required fields: {missing}")
    # `update_meta` refuses to move `model_resolved` (ARCH-378), but a second
    # `write_meta` over an existing run dir would reset it in one operation and
    # defeat that freeze silently. No caller does this today — the fail-over path
    # uses update_meta — so this guards a future crash-recovery or retry path
    # rather than a live bug. Carry the original value forward instead of
    # raising: refusing to write meta at all would be a worse failure than
    # preserving the field the freeze exists to protect.
    existing = d / "meta.json"
    if existing.exists():
        try:
            prior = json.loads(existing.read_text()).get("model_resolved")
        except (OSError, json.JSONDecodeError):
            prior = None
        if prior:
            fields = {**fields, "model_resolved": prior}
    payload = {
        "run_id": fields["run_id"],
        "agent": fields["agent"],
        "task": fields["task"],
        # Short work-unit slug for the dashboard Task column (ARCH-239), e.g.
        # "recursion-guard". Optional — None for runs dispatched before --label
        # adoption; the dashboard falls back to truncating `task`.
        "label": fields.get("label"),
        "started_at": fields["started_at"],
        "ended_at": fields.get("ended_at"),
        "parent_run": fields.get("parent_run"),
        "repo": fields["repo"],
        "worktree": fields["worktree"],
        "profile": fields["profile"],
        "model": fields["model"],
        "pid": fields.get("pid"),
        "billing_source": fields.get("billing_source", "unknown"),
        "origin": fields.get("origin", DEFAULT_ORIGIN),
        # The commit a review-class run was pinned to (ARCH-184 `--review-sha`).
        # None for every non-review run. The review-stamp authenticity gate
        # (ARCH-193) reads it back to confirm a posted APPROVE's reviewed-commit
        # matches the commit its run actually reviewed — so a stale reviewer
        # run-id can't be replayed onto a newer head.
        "review_sha": fields.get("review_sha"),
        # The PR number a `--pr N` dispatch was aimed at, else None. Recorded so a
        # run dir can be traced back to its PR without parsing the free-text task,
        # and so the live-review guard can tell "another review of THIS PR" from
        # "a review of a different PR in the same repo" — repo+agent alone cannot,
        # which is what made that guard recommend killing healthy reviews.
        "pr": fields.get("pr"),
        # The model the roster RESOLVED for this run, frozen at spawn (ARCH-378).
        # `model` is the model the run ended up on, which an ARCH-131 fail-over
        # rewrites in place — so without this there is no record of what was
        # asked for, and `model:` reads identically whether it was chosen or
        # substituted. Keeping both is what lets `agentctl status` say which.
        "model_resolved": fields.get("model_resolved") or fields.get("model"),
        # The originating co session UUID (CLAUDE_CODE_SESSION_ID) for an
        # interactive `agentctl spawn`, else None. cost_join nests the spawn under
        # its co session on /activity instead of orphaning it as a top-level row.
        "co_session": fields.get("co_session"),
        # The ARCH-268 phase-split night-wave key (`<product>/<day>`) for a stage/
        # item spawn, else None. cost_join nests all of one night's stage runs
        # under a single synthetic "overnight night" row (canary follow-up).
        "overnight_wave": fields.get("overnight_wave"),
        # The agent definition file `resolve_agent()` actually read for this run
        # (`agent["source"]`), else None. OPTIONAL, not in REQUIRED_META_FIELDS —
        # every meta.json already on disk predates this field and must keep
        # validating. `repo` alone can't say whether a dispatch reached a
        # black-box crew: `--agents-dir` is honoured before `repo` is even
        # looked at, so `--on constance --agents-dir <root>/CFO/.claude/agents`
        # writes `repo: "constance"` while loading a CFO agent. This is the
        # second signal `projects.dispatch_is_private` needs to catch that,
        # which `session_state._dispatched_by`'s privacy filter reads
        # (agent-platform#465 round 2).
        "agent_source": fields.get("agent_source"),
        # How `model` was resolved — see VALID_MODEL_CHOICES. None for every
        # meta.json predating this field (optional, like agent_source above),
        # and for the budget_refused early-refusal path, which never reaches a
        # real model decision.
        "model_choice": fields.get("model_choice"),
        # The work-item id this dispatch is for (`agentctl spawn --issue`,
        # model-lane epic issue_01M174CH86JBCP7291PS0TM097 — the same field the
        # cost-attribution epic's issue_01M17FR9CECM9J146M50TK8CGT needs to join
        # run cost back to a work item). None when not supplied: left null
        # rather than guessed out of free-text `task`.
        "issue": fields.get("issue"),
        # The usage hold (issue_01M3PZ6J0D55EFC8GGAYMN9SH4): when this dispatch
        # waited for the five-hour window to clear rather than run into extra
        # usage, `held_until` is the UTC instant it waited for (ISO-8601 Z) and
        # `hold_reason` names why. Both None for the (overwhelmingly common)
        # dispatch that never held.
        "held_until": fields.get("held_until"),
        "hold_reason": fields.get("hold_reason"),
        # Which of VALID_COST_CAP_SOURCES set this run's dollar cap, so
        # `agentctl status` can show which applied. None for every meta.json
        # predating this field (optional, like agent_source/model_choice above).
        "cost_cap_source": fields.get("cost_cap_source"),
    }
    if payload["billing_source"] not in VALID_BILLING_SOURCES:
        raise ValueError(f"invalid billing_source: {payload['billing_source']}")
    if payload["origin"] not in VALID_ORIGINS:
        raise ValueError(f"invalid origin: {payload['origin']}")
    if payload["model_choice"] is not None and payload["model_choice"] not in VALID_MODEL_CHOICES:
        raise ValueError(f"invalid model_choice: {payload['model_choice']}")
    if (payload["cost_cap_source"] is not None
            and payload["cost_cap_source"] not in VALID_COST_CAP_SOURCES):
        raise ValueError(f"invalid cost_cap_source: {payload['cost_cap_source']}")
    write_owner_only(d / "meta.json", json.dumps(payload, indent=2) + "\n")


def update_meta(d: Path, **updates: Any) -> None:
    """Read-modify-write of meta.json. Used at run end to fill in `ended_at`, etc.

    Two fields are **write-once** — set by `write_meta` at spawn and frozen
    thereafter, so `update_meta` can't be a back door that mutates them:
      - `origin` (budget-load-bearing, ARCH-156): a `scheduled`→`interactive`
        flip would slip a run's spend out of the capped pool.
      - `review_sha` (ARCH-193): the commit a review run was pinned to, which the
        stamp-authenticity gate binds against. Repointing it at a newer head
        would re-open the stale-replay vector the gate closes.
      - `model_resolved` (ARCH-378): the model the roster resolved at spawn. The
        ARCH-131 fail-over rewrites `model` in place by design, so this is the
        only record of what was actually asked for — a writable one could be
        rewritten by the very fail-over it exists to expose.
    Both fields' values can be individually valid, so a validity check alone
    wouldn't catch a flip — we compare the incoming value against the on-disk one
    *before* merging.

    `model_choice`, when present, is validated against VALID_MODEL_CHOICES —
    `write_meta` already does this, but the ARCH-131 fail-over writes
    `model_choice="harness_fallback"` through THIS function (via
    `_respawn_in_place`), not `write_meta`, and every caller today passes a
    hardcoded valid literal only by convention. Extending the same field
    discipline here closes that gap before a future caller can silently write
    garbage into a field the model-lane epic's Archives record now depends on.
    """
    path = d / "meta.json"
    cur = json.loads(path.read_text())
    for frozen in ("origin", "review_sha", "model_resolved"):
        if frozen in updates and updates[frozen] != cur.get(frozen):
            raise ValueError(
                f"{frozen} is immutable: cannot change "
                f"{cur.get(frozen)!r} to {updates[frozen]!r}"
            )
    if updates.get("model_choice") is not None and updates["model_choice"] not in VALID_MODEL_CHOICES:
        raise ValueError(f"invalid model_choice: {updates['model_choice']}")
    if (updates.get("cost_cap_source") is not None
            and updates["cost_cap_source"] not in VALID_COST_CAP_SOURCES):
        raise ValueError(f"invalid cost_cap_source: {updates['cost_cap_source']}")
    cur.update(updates)
    write_owner_only(path, json.dumps(cur, indent=2) + "\n")


def write_prompt(d: Path, *, system: str, user: str) -> None:
    write_owner_only(d / "prompts" / "system.md", system)
    write_owner_only(d / "prompts" / "user.md", user)


def append_transcript(d: Path, event: dict[str, Any]) -> None:
    with (d / "transcript.jsonl").open("a") as f:
        f.write(json.dumps(event, separators=(",", ":")) + "\n")


def append_tool_call(d: Path, *, tool_name: str, input: dict, result_summary: str,
                     duration_ms: int, is_error: bool = False,
                     timestamp: str | None = None) -> None:
    entry = {
        "timestamp": timestamp or now_iso(),
        "tool_name": tool_name,
        "input": input,
        "result_summary": result_summary,
        "duration_ms": duration_ms,
        "is_error": is_error,
    }
    with (d / "tool_calls.jsonl").open("a") as f:
        f.write(json.dumps(entry, separators=(",", ":")) + "\n")


def write_docs_consulted(d: Path, *, profile: str,
                         individuated_agents: list[str],
                         individuated_skills: list[str],
                         queried_at_runtime: list[dict],
                         materialized_documents: list[dict] | None = None) -> None:
    activation = {
        "agents": sorted(individuated_agents),
        "skills": sorted(individuated_skills),
    }
    payload = {
        "profile": profile,
        # `materialized_at_activation` is the pre-0088 key (Deck reads it);
        # both carry the same value until its readers move to the new one.
        "materialized_at_activation": activation,
        "individuated_at_activation": activation,
        "queried_at_runtime": queried_at_runtime,
        # The full document set this run was given — the agent file, every
        # skill, each instruction block composed into its CLAUDE.md and every
        # other profile document — one {id, kind, body_sha256} per entry (an
        # instruction block also carries `scope`), outliving the worktree
        # `individuated_agents`/`individuated_skills` above read by name only
        # (work item issue_01M3BFGE9QE8339CK5RNVP5XQC). Named `docs` rather
        # than something `_activation`-suffixed: Library Phase 5 is expected
        # to replace this field's producer with the call slip's own `docs[]`,
        # so the shape here is chosen to already match that, not this file's
        # older convention.
        "docs": materialized_documents or [],
    }
    write_owner_only(d / "docs_consulted.json", json.dumps(payload, indent=2) + "\n")


def cost_payload(by_model: dict[str, dict[str, Any]], cost_source: str) -> dict[str, Any]:
    """The cost.json document for `by_model`: top-level totals derived, source tagged.

    Split out of `write_cost` so a tool that recomputes a stored record builds it
    the same way the first write did."""
    if cost_source not in VALID_COST_SOURCES:
        raise ValueError(f"invalid cost_source: {cost_source!r}; must be one of {VALID_COST_SOURCES}")
    tokens_in = sum(m.get("tokens_in", 0) for m in by_model.values())
    tokens_out = sum(m.get("tokens_out", 0) for m in by_model.values())
    usd = round(sum(m.get("usd") or 0.0 for m in by_model.values()), 4)
    unpriced_models = sorted(k for k, m in by_model.items() if m.get("unpriced"))
    return {
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "usd": usd,
        "cost_source": cost_source,
        "by_model": by_model,
        **({"unpriced_models": unpriced_models} if unpriced_models else {}),
    }


def write_cost(d: Path, *, by_model: dict[str, dict[str, Any]],
               cost_source: str = "anthropic_cli") -> dict[str, Any]:
    """Write cost.json and return the payload written. by_model is
    {model_id: {tokens_in, tokens_out, usd}}. Top-level totals are derived.
    cost_source tags how the usd figure was obtained.

    The return value lets a caller that needs these numbers again (the
    finalize-time Archives record — `_post_model_resolution_signal`) use the
    value it just computed instead of reading cost.json back off disk."""
    payload = cost_payload(by_model, cost_source)
    write_owner_only(d / "cost.json", json.dumps(payload, indent=2) + "\n")
    return payload


COST_PARTIAL_FILENAME = "cost-partial.json"


def write_cost_partial(d: Path, *, by_model: dict[str, dict[str, Any]],
                       cost_source: str) -> dict[str, Any]:
    """Write `cost-partial.json`: a run's spend recorded so far, while it is
    still executing. `cost_source` is required and keyword-only, with no
    default (the operator, 2026-09-15, relayed from Deck): Deck draws an
    `estimate_fallback` figure marked approximate, and that marking survives
    only as far as the field travels with the number, so nothing may write
    this file without stating which kind of number it wrote. What `usd`
    means depends on `cost_source`:

    - `gateway_spend_log` — real per-request metered spend, summed from the
      run's own `gateway_spend.jsonl` up to `written_at` (ISO-8601, added
      here). Monotonic and a genuine floor on true spend: it only grows.
    - `estimate_fallback` — the run's own summed `message.usage` tokens,
      priced at the `CLAUDE_PRICING_PER_MTOK` list rate. This is NOT the
      CLI's billed total (there is no mid-run equivalent of `total_cost_usd`)
      and is NOT a floor: the round-1 reviewer priced five finished
      Anthropic-billed runs from one day through this same method against
      their actual `cost.json` figure and measured -12% to +16% divergence,
      in both directions. That sample is enough to act on — it's why this
      figure is marked `estimate_fallback` rather than trusted outright —
      but it is not a bound, and a consumer must not quote -12%/+16% as one.
      A consumer must not extrapolate past `written_at` either way, in
      either branch.

    Two consumer rules follow directly from the marking having to survive
    (Deck's own commitments, recorded here since this is where the marking
    originates): an `estimate_fallback` figure is never displayed without its
    `cost_source`, and never summed with a `gateway_spend_log` figure into one
    total — the two are different kinds of number and combining them would
    destroy the distinction this field exists to carry.

    A SIBLING of `cost.json`, deliberately, not a shared file. `budget.py`
    reads "meta.json present, cost.json absent" as its in-flight test, and
    `cost_join.load_node`, `budget-watchdog`'s `active_run_count`, and
    `run_retention.thin_run_dir`'s KEEP_FILES invariant all lean on the same
    reading — cost.json's mere PRESENCE means "this run is done" to every one
    of them. Landing a partial figure into that file would flip that meaning
    out from under readers that never expected it to move. `write_cost`'s
    finalisation write is unaffected by this file's existence and supersedes
    it the moment it lands — a reader MUST check for `cost.json` first and
    treat this file as stale/ignorable once that exists.

    Written atomically: `tempfile.mkstemp` in the same directory — a random
    suffix, `O_EXCL`-created, so the name can't be guessed and pre-planted —
    then `os.replace`, so a concurrent reader never observes a torn/partial
    JSON body and a symlink can't be substituted for the temp file before
    it's created. The file is rewritten in place on the SAME run dir, from
    the SAME process, repeatedly over the run's lifetime.

    Trust boundary: this file, like meta.json, is writable by the run it
    measures (a dispatched run gets `AGENTCTL_RUN_DIR` pointed at its own run
    dir and unrestricted Bash under `bypassPermissions`). The in-flight
    figure assumes a cooperating run; one that wanted to hide its own spend
    from the pool-wide estimate could already do so by rewriting its own
    meta.json `started_at`. `budget._partial_usd` clamps a recorded `usd` to
    a finite, non-negative number, which bounds a forged file to hiding, at
    most, its own spend — no further plausibility guard (e.g. an
    elapsed-time bound) is built here, because no forging run has been
    observed forging the *value*. This bound is only true because
    `read_cost_partial` refuses to open anything but a regular file first
    (round 3, security-reviewer finding on agent-platform#592): a FIFO
    planted at this name, with no writer, hung every reader of the run's
    directory indefinitely — `agentctl`'s own `_budget_gate` (called
    synchronously before every spawn) and `bin/budget-watchdog`'s poll loop
    both call through `budget.status()` → `_partial_usd` into this file, so
    that was a pool-wide denial of service, not a bounded per-run lie. A
    forged *type* at this path is now refused before the forged *value* is
    ever reached.

    `by_model` / `cost_source` share `write_cost`'s shape and validation;
    totals are derived the same way."""
    if cost_source not in VALID_COST_SOURCES:
        raise ValueError(f"invalid cost_source: {cost_source!r}; must be one of {VALID_COST_SOURCES}")
    tokens_in = sum(m.get("tokens_in", 0) for m in by_model.values())
    tokens_out = sum(m.get("tokens_out", 0) for m in by_model.values())
    usd = round(sum(m.get("usd") or 0.0 for m in by_model.values()), 4)
    unpriced_models = sorted(k for k, m in by_model.items() if m.get("unpriced"))
    payload = {
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "usd": usd,
        "cost_source": cost_source,
        "by_model": by_model,
        **({"unpriced_models": unpriced_models} if unpriced_models else {}),
        "written_at": now_iso(),
    }
    fd, tmp_name = tempfile.mkstemp(prefix=f".{COST_PARTIAL_FILENAME}.", suffix=".tmp",
                                    dir=str(d))
    tmp = Path(tmp_name)
    try:
        try:
            os.write(fd, (json.dumps(payload, indent=2) + "\n").encode())
        finally:
            os.close(fd)
    except BaseException:
        # A write failure (e.g. ENOSPC) must not leave the temp file behind —
        # round 2 left this gap (reviewer suggestion, agent-platform#592):
        # unlink before re-raising rather than stranding a stray fragment in
        # the run's own owner-only directory.
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, d / COST_PARTIAL_FILENAME)
    return payload


_COST_PARTIAL_MAX_BYTES = 64 * 1024


def read_capped_regular_file(path: Path, max_bytes: int) -> bytes | None:
    """The bytes of `path`, or None when it is missing, not a regular file, larger
    than `max_bytes`, or unreadable. Use it for any file in a run's directory.

    That directory is writable by the run it measures (see `write_cost_partial`'s
    trust-boundary docstring), so the file's type is decided before anything can
    block on it. A plain `.read_text()` here would block forever on a FIFO planted
    at the name with no writer. `read_cost_partial` is on the synchronous budget-gate
    path checked before each spawn, so one misbehaving run would freeze spawning for
    its whole pool, not just under-report its own cost (round 3, security-reviewer
    finding on agent-platform#592).

    `os.lstat` decides first, without opening: a FIFO, socket, device, symlink or
    directory returns None immediately. The open that follows is `O_NOFOLLOW`
    (refuses a symlink substituted at the last instant) and `O_NONBLOCK` (an open of
    a FIFO without it can itself block, and the lstat check alone leaves a race
    between the lstat and the open). `fstat` on the descriptor re-checks `S_ISREG`
    before any read.

    The read is capped at `max_bytes`: a larger file is treated as unreadable rather
    than read in full. `read_cost_guarded` keeps its own copy of this guard; see its
    docstring for why."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        fst = os.fstat(fd)
        if not stat.S_ISREG(fst.st_mode):
            return None
        data = os.read(fd, max_bytes + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(data) > max_bytes:
        return None
    return data


def read_cost_partial(d: Path) -> dict[str, Any] | None:
    """The most recently recorded in-flight partial for a run, or None when
    none has been written yet (the run is too young — the caller falls back
    to an estimate), the file is missing/unreadable/corrupt/oversized, isn't a
    regular file, or its `cost_source` is missing or not one of
    `VALID_COST_SOURCES` (the operator, 2026-09-15) — a figure a consumer can't mark as
    a kind of number is treated the same as no figure at all, never returned
    unmarked.

    Does NOT check `cost.json` — callers that care about the completed/
    in-flight distinction must check for `cost.json` first and treat it as
    authoritative; this file is superseded the instant that one exists.

    The file is read through `read_capped_regular_file`, which refuses anything
    that is not a regular file before opening it. A genuine partial is a few
    hundred bytes; anything over `_COST_PARTIAL_MAX_BYTES` is unreadable."""
    data = read_capped_regular_file(d / COST_PARTIAL_FILENAME, _COST_PARTIAL_MAX_BYTES)
    if data is None:
        return None
    try:
        parsed = json.loads(data)
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    cost_source = parsed.get("cost_source")
    if not isinstance(cost_source, str) or cost_source not in VALID_COST_SOURCES:
        # A figure with no (or an unrecognised) cost_source can never reach a
        # consumer marked as anything, so it must never reach one at all
        # (the operator, 2026-09-15) — treated as unreadable, same as a missing file,
        # rather than returned unmarked for a caller to display or sum blind.
        # The isinstance check comes first (round 6, security-reviewer finding
        # on agent-platform#592): `cost_source not in VALID_COST_SOURCES` alone
        # calls `hash()` on the left-hand value as part of the `in` test, and a
        # run-written `cost_source` of a list or dict is unhashable — raising
        # TypeError out of this function instead of returning None like every
        # other malformed shape this same file is fully controlled by the run
        # it measures for.
        return None
    return parsed


_COST_MAX_BYTES = 64 * 1024


def read_cost_guarded(d: Path) -> dict[str, Any] | None:
    """The recorded final cost for a finished run, or None when `cost.json` is
    missing, unreadable, non-regular, oversized, corrupt, or carries no valid
    `cost_source` — the same fail-closed answer `read_cost_partial` gives for
    its own file, and for the same reason: this is `_agent_run_detail`'s
    (`session_state.py`) read for the session payload polled for every pinned
    session, not the one-shot `read_cost` above, which assumes a run directory
    that has already finished cleanly and is happy to raise on anything else.

    Deliberately a SEPARATE, duplicated guard rather than a call to
    `read_capped_regular_file`: that helper is shared by `read_cost_partial` and the
    `agentctl` run-directory readers, and the guard's shape was reached over six
    security-review rounds on agent-platform#592. A shared-helper edit could regress
    this reader, which is already hardened, so the few duplicated lines are kept
    against that risk. `cost.json` carries the same trust boundary (writable by the
    run it measures; see `write_cost_partial`'s docstring).

    `os.lstat` decides first, without opening — a FIFO, socket, device,
    symlink, or directory here returns None immediately, so a planted FIFO at
    this name cannot hang a caller waiting on a writer that will never come.
    The open that follows is `O_NOFOLLOW` (refuses a symlink substituted at
    the last instant) and `O_NONBLOCK` (opening a FIFO without it can itself
    block); `fstat` on the resulting descriptor re-checks `S_ISREG` before any
    read, closing the lstat-to-open race. The read is capped at
    `_COST_MAX_BYTES`; anything larger is treated as unreadable rather than
    read in full.

    Unlike `cost-partial.json`, `cost.json` is written by `write_owner_only`
    (truncate-in-place, not a tempfile+rename), so a reader can in principle
    observe a torn write mid-finalize — that lands here as a JSON parse
    failure, which already returns None, the same answer as every other
    unreadable case.

    A caller that needs to catch `RecursionError` (a pathological nested body
    escaping `json.loads`) wraps this call itself, the same way
    `budget._partial_usd` wraps `read_cost_partial` — not handled inside
    either reader."""
    path = d / "cost.json"
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        fst = os.fstat(fd)
        if not stat.S_ISREG(fst.st_mode):
            return None
        data = os.read(fd, _COST_MAX_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(data) > _COST_MAX_BYTES:
        return None
    try:
        parsed = json.loads(data)
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    cost_source = parsed.get("cost_source")
    if not isinstance(cost_source, str) or cost_source not in VALID_COST_SOURCES:
        # Same posture as read_cost_partial's identical check: a figure with
        # no (or an unrecognised) cost_source can never reach a consumer
        # marked as anything, so it is treated as unreadable rather than
        # returned unmarked. isinstance first, since `not in` alone would
        # call `hash()` on the left-hand value and raise on an unhashable
        # run-written cost_source (list/dict) instead of degrading.
        return None
    return parsed


def resolve_pricing(model: str) -> dict[str, float] | None:
    """Return the per-Mtok price row for a model, or None if unknown.

    A Codex slug resolves from CODEX_PRICING_PER_MTOK. Any other id is normalized
    to the bare family key in CLAUDE_PRICING_PER_MTOK: a `[1m]` context suffix and
    a trailing `-<8-digit-date>` snapshot are stripped (e.g. `claude-opus-4-8[1m]`
    and `claude-haiku-4-5-20251001` both map to their family row)."""
    m = (model or "").strip()
    if m in CODEX_PRICING_PER_MTOK:
        return CODEX_PRICING_PER_MTOK[m]
    if "[" in m:
        m = m.split("[", 1)[0]
    if m in CLAUDE_PRICING_PER_MTOK:
        return CLAUDE_PRICING_PER_MTOK[m]
    m2 = re.sub(r"-\d{8}$", "", m)
    return CLAUDE_PRICING_PER_MTOK.get(m2)


class UsageDedup:
    """Dedupes Claude Code assistant usage records on (message.id, requestId).

    Claude Code writes the same assistant response into a transcript or stream
    more than once — up to 4x in a long session — with byte-identical `usage`
    (ARCH-383); summing every occurrence over-reports by that multiple. Every
    site that sums usage from a transcript or stream shares this instead of
    hand-rolling the same `seen` set, so the key and the keep-first-occurrence
    rule can't drift between them.

    A record with neither id (`(None, None)`) is never treated as a duplicate
    of another keyless record — each one counts. `keyless` tracks how many
    were seen, for a caller that wants to report it."""

    def __init__(self) -> None:
        self._seen: set[tuple] = set()
        self.keyless = 0

    def is_duplicate(self, message_id: Any, request_id: Any) -> bool:
        """True when this (message_id, request_id) pair was already accepted —
        the caller must skip summing this record. Records the pair as accepted
        on its first call."""
        key = (message_id, request_id)
        if key == (None, None):
            self.keyless += 1
            return False
        if key in self._seen:
            return True
        self._seen.add(key)
        return False


def cost_from_usage(usage_by_model: dict[str, dict[str, int]],
                    where: str | None = None) -> dict[str, dict[str, Any]]:
    """Price token usage into a `by_model` dict ready for write_cost (ARCH-239).

    `usage_by_model` is {model: {input_tokens, output_tokens,
    cache_read_input_tokens, cache_creation_input_tokens, cache_creation}} (the
    fields a Claude transcript's `usage` records carry). `cache_creation`, when
    present, is the TTL breakdown `{ephemeral_1h_input_tokens,
    ephemeral_5m_input_tokens}` — Claude Code on a subscription writes at the
    1-hour TTL (2x input), billed separately from the 5-minute TTL (1.25x input)
    that `cache_creation_input_tokens` alone priced every write at. When
    `cache_creation` is absent, the flat `cache_creation_input_tokens` is priced
    at the 5-minute rate as before. Returns {model: {tokens_in, tokens_out,
    usd}} with `usd` computed from CLAUDE_PRICING_PER_MTOK. tokens_in counts the
    new + cache-write + cache-read input tokens; an unpriced model contributes
    its tokens with usd=0.0 (so the count is never lost) — the caller tags the
    result `cost_source="estimate_fallback"` since it's a list-price estimate.

    A model with no price row also raises a warning (`_warn_unpriced`), so someone
    adds its row; `where` names the run id or session it was seen in."""
    out: dict[str, dict[str, Any]] = {}
    for model, u in usage_by_model.items():
        new_in = int(u.get("input_tokens", 0) or 0)
        out_tok = int(u.get("output_tokens", 0) or 0)
        cache_read = int(u.get("cache_read_input_tokens", 0) or 0)
        breakdown = u.get("cache_creation")
        if isinstance(breakdown, dict):
            cache_write_1h = int(breakdown.get("ephemeral_1h_input_tokens", 0) or 0)
            cache_write_5m = int(breakdown.get("ephemeral_5m_input_tokens", 0) or 0)
        else:
            cache_write_1h = 0
            cache_write_5m = int(u.get("cache_creation_input_tokens", 0) or 0)
        cache_write_total = cache_write_1h + cache_write_5m
        p = resolve_pricing(model)
        if p:
            cache_write_1h_rate = p.get("cache_write_1h", p["cache_write"])
            usd = round(
                (new_in * p["input"] + out_tok * p["output"]
                 + cache_write_5m * p["cache_write"] + cache_write_1h * cache_write_1h_rate
                 + cache_read * p["cache_read"]) / 1_000_000,
                6,
            )
        else:
            usd = None
        out[model] = {
            "tokens_in": new_in + cache_write_total + cache_read,
            "tokens_out": out_tok,
            "usd": usd,
            **({} if p else {"unpriced": True}),
        }
        if not p:
            _warn_unpriced(model, where)
    return out


def _alert_claim_path(model: str, day: str) -> Path:
    """The once-per-day marker for one model: RUNS_ROOT/.unpriced-alerts/<day>/<slug>.

    The slug comes from a transcript a run writes, so it is reduced to a filename-safe
    charset; a hash of the original keeps two different raw slugs from colliding."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", model)[:80].lstrip(".") or "_"
    digest = hashlib.sha1(model.encode("utf-8", "replace")).hexdigest()[:8]
    return RUNS_ROOT / ".unpriced-alerts" / day / f"{safe}-{digest}"


def _post_unpriced_alert(model: str, where: str | None, day: str) -> None:
    """Post one operating-plane alert for `model`, at most once per `day`.

    The claim is an O_EXCL file, so two processes racing on the same model cannot
    both post. A failed post releases the claim so the next sighting retries. Never
    raises: this runs inside cost writing, which it must not break."""
    claim = _alert_claim_path(model, day)
    try:
        claim.parent.mkdir(parents=True, exist_ok=True)
        os.close(os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except FileExistsError:
        return
    except OSError as e:
        print(f"telemetry: unpriced-model alert state unwritable ({type(e).__name__}); "
              f"not posting", file=sys.stderr)
        return
    try:
        import signal_client as SC
        slug = SC.sanitise(model, max_len=80)
        seen = f" (seen in {SC.sanitise(where, max_len=80)})" if where else ""
        body = (f"Model {slug} has no price row, so its cost is recorded as unmeasured "
                f"rather than $0{seen}. Add its row to CLAUDE_PRICING_PER_MTOK in "
                f"lib/agent-platform/telemetry.py from "
                f"https://platform.claude.com/docs/en/about-claude/pricing.")
        # timeout=5 x 3 attempts + backoff (<=1.5s, <=3s) is at most 19.5s for a store
        # that stops answering (the timeout bounds each socket operation, so a store
        # that trickles bytes can exceed it), where the 30s default would hold the
        # SubagentStop hook's exit for ~95s.
        res = SC.post_alert("agent-platform", body, timeout=5,
                            dedup_key=f"unpriced-model:{day}:{slug}",
                            meta={"alert": "unpriced-model", "model": slug})
        failed = isinstance(res, str) and res.startswith("error")
    except Exception as e:
        res, failed = f"error:{type(e).__name__}", True
    if failed:
        print(f"telemetry: unpriced-model alert not delivered ({res})", file=sys.stderr)
        try:
            claim.unlink()
        except OSError:
            pass


def _run_detached(fn) -> None:
    """Run `fn` on its own thread so a slow post never delays the caller. The
    thread is non-daemon: a short-lived caller (the SubagentStop hook) waits for
    an in-flight post at exit rather than dropping it."""
    threading.Thread(target=fn, name="unpriced-model-alert").start()


_ALERT_RUNNER = _run_detached


def _warn_unpriced(model: str, where: str | None) -> None:
    """Say a model was priced as unmeasured: one stderr line every time, and one
    deduplicated operating-plane alert per model per day. Never raises.

    The post runs detached because `cost_from_usage` is called from agentctl's
    per-turn stream drain, and `post_alert` can block for several seconds when the
    plane is down."""
    try:
        label = re.sub(r"[^\x20-\x7e]", "?", model)[:80]
        print(f"telemetry: model {label} has no price row; cost recorded as unmeasured "
              f"(add it to CLAUDE_PRICING_PER_MTOK)", file=sys.stderr)
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if _alert_claim_path(model, day).exists():
            return
        _ALERT_RUNNER(lambda: _post_unpriced_alert(model, where, day))
    except Exception as e:
        print(f"telemetry: unpriced-model warning failed ({type(e).__name__})", file=sys.stderr)


def cost_from_codex_usage(model: str, usage: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Price the `usage` block of a Codex `turn.completed` event into a `by_model`
    dict for `write_cost(..., cost_source="codex_turn_usage")`.

    Codex reports `input_tokens` INCLUSIVE of `cached_input_tokens`, and
    `output_tokens` inclusive of `reasoning_output_tokens`. Measured on 2026-09-24
    from the token_count records in ~/.codex/sessions: in all five, `total_tokens`
    equals `input_tokens + output_tokens` although cached and reasoning counts
    were nonzero, so neither is additive. Pricing `input_tokens` at the input rate
    and `cached_input_tokens` again at the cached rate would charge cached tokens
    twice; the cached share is priced at the cached rate and only the remainder at
    the input rate. `cache_write_input_tokens` is priced at the plain input rate as
    an addition (it has been 0 in every record seen, so whether it is inside
    `input_tokens` is unmeasured; adding it can only over-report).

    Raises ValueError for a slug with no price row rather than returning usd=0.0,
    which every cost guard reads as free."""
    if model not in CODEX_PRICING_PER_MTOK:
        raise ValueError(f"no published price for Codex model {model!r}; refusing to record it at $0")
    input_total = int(usage.get("input_tokens", 0) or 0)
    cached = min(int(usage.get("cached_input_tokens", 0) or 0), input_total)
    return cost_from_usage({model: {
        "input_tokens": input_total - cached,
        "output_tokens": int(usage.get("output_tokens", 0) or 0),
        "cache_read_input_tokens": cached,
        "cache_creation_input_tokens": int(usage.get("cache_write_input_tokens", 0) or 0),
    }})


def write_outcome(d: Path, *, exit_status: str,
                  files_modified: list[str] | None = None,
                  files_added: list[str] | None = None,
                  files_deleted: list[str] | None = None,
                  prs_opened: list[str] | None = None,
                  tests_run: int | None = None,
                  tests_passed: int | None = None,
                  subagents_spawned: list[str] | None = None,
                  error_reason: str | None = None,
                  terminal_reason: str | None = None,
                  api_error_status: int | None = None,
                  api_error_message: str | None = None,
                  overage_seen: bool | None = None,
                  peak_utilization: float | None = None,
                  rate_limit_status: str | None = None) -> dict[str, Any]:
    if exit_status not in VALID_EXIT_STATUSES:
        raise ValueError(f"invalid exit_status: {exit_status}")
    if rate_limit_status is not None and rate_limit_status not in VALID_RATE_LIMIT_STATUSES:
        raise ValueError(f"invalid rate_limit_status: {rate_limit_status}")
    payload: dict[str, Any] = {
        "exit_status": exit_status,
        "files_modified": files_modified or [],
        "files_added": files_added or [],
        "files_deleted": files_deleted or [],
        "prs_opened": prs_opened or [],
        "tests_run": tests_run,
        "tests_passed": tests_passed,
        "subagents_spawned": subagents_spawned or [],
    }
    # error_reason and terminal_reason are DIFFERENT AXES and both are kept.
    # error_reason (#280) is free text naming why a run stopped — e.g. the
    # capacity refusal that arrives with exit_status=capacity_refused.
    # terminal_reason is the HARNESS's own classification of how the session
    # ended — "completed", "api_error", "aborted_streaming", "blocking_limit".
    # This function persists whatever it is handed; the caller is what strips,
    # caps and reduces the value to a token charset before it gets here (see
    # `_finalize_run`), so do not read a value out of an outcome.json as raw
    # harness output. It arrived with PR #278 recording
    # only "api_error" and now carries whatever the harness reported; absent
    # means the harness emitted no terminal record. api_error_status and
    # api_error_message accompany it on that ONE reason and no other.
    # A run can carry either axis, both, or neither.
    if error_reason is not None:
        payload["error_reason"] = error_reason
    if terminal_reason is not None:
        payload["terminal_reason"] = terminal_reason
    if api_error_status is not None:
        payload["api_error_status"] = api_error_status
    if api_error_message is not None:
        payload["api_error_message"] = api_error_message
    # Subscription quota, from the CLI's `rate_limit_event` stream events — the
    # same signal the TUI's extra-usage indicator reads (ARCH-442). Recorded
    # because nothing downstream could see it: the cost numbers are API-RATE
    # ESTIMATES on subscription usage that is mostly inside the plan, so
    # "$60 today" answers a different question from "we were in overage today",
    # and ARCH-174 says gate a cost squeeze on the second.
    #
    # Written only when the run actually reported quota, so a run whose stream
    # carried none stays silent rather than claiming a confident `false`. Absent
    # and false mean different things here and the difference is the whole point.
    if overage_seen is not None:
        payload["overage_seen"] = overage_seen
    if peak_utilization is not None:
        payload["peak_utilization"] = peak_utilization
    if rate_limit_status is not None:
        payload["rate_limit_status"] = rate_limit_status
    write_owner_only(d / "outcome.json", json.dumps(payload, indent=2) + "\n")
    return payload


# --- read paths --------------------------------------------------------------


def read_meta(d: Path) -> dict:
    return json.loads((d / "meta.json").read_text())


def read_outcome(d: Path) -> dict:
    return json.loads((d / "outcome.json").read_text())


def read_cost(d: Path) -> dict:
    return json.loads((d / "cost.json").read_text())


def iter_transcript(d: Path) -> Iterable[dict]:
    with (d / "transcript.jsonl").open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def iter_tool_calls(d: Path) -> Iterable[dict]:
    with (d / "tool_calls.jsonl").open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


# --- validation ---------------------------------------------------------------


def validate_run_dir(d: Path) -> list[str]:
    """Returns a list of error strings; empty list means structurally valid."""
    errs: list[str] = []
    if not d.exists() or not d.is_dir():
        return [f"missing run directory: {d}"]

    # required files
    for rel in REQUIRED_RUN_FILES:
        if not (d / rel).exists():
            errs.append(f"missing: {rel}")

    # JSON files must parse
    for rel in ("meta.json", "docs_consulted.json", "cost.json", "outcome.json"):
        p = d / rel
        if not p.exists():
            continue
        try:
            json.loads(p.read_text())
        except json.JSONDecodeError as exc:
            errs.append(f"invalid JSON: {rel} ({exc.msg} at {exc.lineno}:{exc.colno})")

    # meta.json required fields
    if (d / "meta.json").exists():
        try:
            meta = json.loads((d / "meta.json").read_text())
        except json.JSONDecodeError:
            pass
        else:
            for f in REQUIRED_META_FIELDS:
                if f not in meta:
                    errs.append(f"meta.json missing field: {f}")
            if "billing_source" in meta and meta["billing_source"] not in VALID_BILLING_SOURCES:
                errs.append(f"meta.json invalid billing_source: {meta['billing_source']}")
            if "origin" in meta and meta["origin"] not in VALID_ORIGINS:
                errs.append(f"meta.json invalid origin: {meta['origin']}")

    # transcript.jsonl line-by-line valid
    if (d / "transcript.jsonl").exists():
        with (d / "transcript.jsonl").open() as f:
            for n, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    json.loads(line)
                except json.JSONDecodeError:
                    errs.append(f"transcript.jsonl line {n} not valid JSON")

    # tool_calls.jsonl line-by-line valid
    if (d / "tool_calls.jsonl").exists():
        with (d / "tool_calls.jsonl").open() as f:
            for n, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    json.loads(line)
                except json.JSONDecodeError:
                    errs.append(f"tool_calls.jsonl line {n} not valid JSON")

    # outcome.json exit_status sanity (when present)
    if (d / "outcome.json").exists():
        try:
            outcome = json.loads((d / "outcome.json").read_text())
        except json.JSONDecodeError:
            pass
        else:
            status = outcome.get("exit_status")
            if status not in VALID_EXIT_STATUSES:
                errs.append(f"outcome.json invalid exit_status: {status!r}")

    return errs
