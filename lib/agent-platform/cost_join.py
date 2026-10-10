"""cost_join — reconstruct agentic session trees and join cost down each dispatch chain.

Every agent run leaves a run-dir under <root>/agent-runs/<date>/ with meta.json
(who/what/when + parent_run), cost.json (usd), and outcome.json (PRs/tests/files).
This module walks those dirs, rebuilds the parent->child forest, and rolls each tree
up into a *session*: a top-level dispatch and everything it spawned, with cost summed
across the whole chain. It is the shared foundation for both item-2 dashboard views
(ARCH-191 / ARCH-239): the /activity session-activity log and the cost-per-outcome
rollup on /cost-performance.

Two session shapes come out, because there are two ways a parent links to a child
(see ARCH-239's corpus map):

  • agentctl session — a `run-*` root (parent_run is null) plus its `run-*` descendants
    (orchestrator -> workers -> sub-workers). Fully captured: every node has its own
    run-dir + cost.json. This covers overnight drains and interactive agentctl spawns.

  • interactive session — a co session, keyed by its Claude session UUID. Its children
    are the `sub-*` dirs the SubagentStop hook emits (ARCH-239 piece 2), whose
    parent_run is that UUID. The co session's OWN main-loop cost is not in any run-dir
    (it lives in ~/.claude/projects), so an interactive session's total here is its
    captured subagents only — the orchestrator main-loop is a known gap (phase 2).

Cross-day note: a run started near midnight UTC lands in its start date's dir while its
children may spill into the next; build_sessions indexes one extra day past the window
on each side so a straddling tree stays whole, then selects sessions by their root's
start time.
"""
from __future__ import annotations

import json
import math
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".local" / "lib" / "agent-platform"))
import registry as R  # noqa: E402
import telemetry as T  # noqa: E402

RUNS_ROOT = T.RUNS_ROOT

# Where Claude Code keeps interactive co-session transcripts: one <uuid>.jsonl per
# session, under a dir whose name is the sanitized cwd. The co session's own
# main-loop cost lives only here (never a run-dir), so an interactive session's
# main-loop is reconstructed by parsing this transcript (ARCH-239 gap fix).
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"

# Profiles whose runs are scheduled/autonomous rather than interactive, used to infer
# `origin` when an older meta.json predates the explicit origin field.
_SCHEDULED_PROFILES = {"overnight", "full_monty", "scheduled"}

# A ~/.claude/projects dir is a Convivy product's co-session transcript dir when its
# sanitized-cwd name carries this marker (every product lives under <root>/...). The
# main-loop-only discovery scan in build_sessions uses it to find co sessions whose cost
# lives only in their transcript (they dispatched no captured children), while skipping
# worktree dirs (dispatched workers) and agent-runs sidecars. A substring marker (not a
# $HOME-derived absolute prefix) keeps the scan portable across machines and test HOMEs.
# The scan bounds itself by transcript mtime so the corpus-wide walk stays affordable at
# the 15-min producer cadence (the concern ARCH-241 flagged), replacing the old PA-only
# allowlist: every product's interactive main-loop cost now surfaces.
_CONVIVY_DIR_MARKER = "convivy"


def parse_iso(s: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp, tolerating a trailing Z and fractional seconds.

    meta timestamps come in two flavours: `2026-06-03T08:00:01Z` (agentctl) and
    `2026-06-24T21:56:38.643Z` (the SubagentStop hook). Both must round-trip."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _duration_s(meta: dict) -> float | None:
    started, ended = parse_iso(meta.get("started_at")), parse_iso(meta.get("ended_at"))
    if not started or not ended:
        return None
    return max(0.0, (ended - started).total_seconds())


def _infer_origin(meta: dict) -> str:
    origin = meta.get("origin")
    if origin:
        return origin
    if meta.get("profile") in _SCHEDULED_PROFILES:
        return "scheduled"
    return "interactive"


def _files_changed(outcome: dict) -> int:
    return sum(
        len(outcome.get(k) or [])
        for k in ("files_added", "files_modified", "files_deleted")
    )


def _usd_fields(priced_usd, unpriced_models: list[str], cost_missing: bool = False) -> dict:
    """The `usd` + unmeasured marker every node carries.

    A node with no usable cost record (`cost_missing`: a run still in flight, or a
    cost.json that is missing, unparseable, not a JSON object, or has a usd that is absent,
    not a number, negative or non-finite) or with any model `resolve_pricing` could not
    price has `usd: None`
    (nothing may sum it as zero). `cost_missing` says which of the two
    it is, and `unpriced_models` names the unpriced models; `priced_usd` keeps the
    dollars that were priced, a floor for a total. A recorded, fully priced node
    carries `usd` as a float, `cost_missing` False and an empty `unpriced_models`."""
    p = round(float(priced_usd or 0.0), 6)
    if cost_missing or unpriced_models:
        return {"usd": None, "priced_usd": p, "unpriced_models": list(unpriced_models),
                "cost_missing": cost_missing}
    return {"usd": p, "priced_usd": p, "unpriced_models": [], "cost_missing": False}


def node_usd_floor(n: dict) -> float:
    """A node's dollars for a total: `usd`, or the priced floor when unmeasured.
    Only for a sum that also carries the unmeasured marker; never display this
    alone for a node with `unpriced_models`."""
    return n["usd"] if n.get("usd") is not None else float(n.get("priced_usd") or 0.0)


def _usable_cost(cost) -> bool:
    """Whether a cost.json record can be summed: a JSON object whose usd is a finite number
    at or above zero. The run always writes a top-level usd, so a missing or null one
    is a damaged or hand-made record, not a measured $0. json.loads accepts NaN and
    Infinity, so a negative or non-finite usd is not a measurement either."""
    if not isinstance(cost, dict):
        return False
    usd = cost.get("usd")
    # type(), not isinstance(): isinstance(True, int) holds, and a JSON boolean is not a sum.
    # type(None) is not in the tuple, so a missing or null usd fails here too.
    return type(usd) in (int, float) and math.isfinite(usd) and usd >= 0


def load_node(run_dir: Path) -> dict | None:
    """Read one run-dir into a flat node dict. Returns None for a dir with no meta.

    DO NOT rely on this to keep `co-*` dirs out of the corpus. It used to say the
    None covered "a co-* sidecar", which stopped being true when WS1.2 gave co
    sessions an identity record: a `co-<uuid>` dir now HAS a meta.json, and this
    function called on one returns a node.

    What keeps them out is the NAME FILTER in `index_nodes()` (`run-`/`sub-` only)
    and the three `startswith("run-")` walks in `budget.py`. A co session's spend
    is represented exactly once, by `co_main_loop_node()` reading the transcript;
    admitting the dir as well would double-count every interactive session.
    Pinned by `test_a_co_session_record_never_enters_the_corpus`.
    """
    try:
        meta = T.read_meta(run_dir)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if not meta:
        return None
    cost_missing = False
    # ValueError covers JSONDecodeError and UnicodeDecodeError: a cost.json cut off or
    # written as non-UTF-8 is unmeasured, the same as one that is missing.
    try:
        cost = T.read_cost(run_dir)
    except (FileNotFoundError, ValueError):
        cost = None
    if not _usable_cost(cost):
        cost, cost_missing = {}, True
    try:
        outcome = T.read_outcome(run_dir)
    except (FileNotFoundError, json.JSONDecodeError):
        outcome = {}

    name = run_dir.name
    kind = "sub" if name.startswith("sub-") else "run"
    by_model = cost.get("by_model") or {}
    unpriced = sorted(m for m, v in by_model.items() if v.get("unpriced"))
    turns = None
    if kind == "sub":
        try:
            turns = json.loads((run_dir / "subagent.json").read_text()).get("turns")
        except (FileNotFoundError, json.JSONDecodeError, AttributeError):
            turns = None

    # The real "a PR was opened" signal is pr-link.json (agentctl pr-open), not
    # outcome.json["prs_opened"] (empty in practice). This is the outcome key for
    # the cost-per-outcome rollup (View A).
    pr_url = pr_number = None
    try:
        pl = json.loads((run_dir / "pr-link.json").read_text())
        pr_url, pr_number = pl.get("pr_url"), pl.get("pr_number")
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    return {
        "run_id": name,
        "kind": kind,
        "dir": str(run_dir),
        "date": run_dir.parent.name,
        "agent": meta.get("agent") or "?",
        "task": meta.get("task") or "",
        "label": meta.get("label"),
        "model": meta.get("model") or "?",
        "product": _canonical_product(meta.get("repo")),
        "origin": _infer_origin(meta),
        "profile": meta.get("profile") or "",
        "parent_run": meta.get("parent_run"),
        "co_session": meta.get("co_session"),
        "overnight_wave": meta.get("overnight_wave"),
        # The work item the dispatch named (`--issue`), the PR a `--pr` dispatch aimed
        # at, the raw repo that PR number is scoped to, and the billing lane. Read by
        # build_item_costs; `repo` stays raw because product is a display bucket.
        "issue": meta.get("issue"),
        "pr": meta.get("pr"),
        "repo": meta.get("repo") or "",
        "billing_source": meta.get("billing_source") or "unknown",
        "worktree": meta.get("worktree") or "",
        "started_at": meta.get("started_at"),
        "ended_at": meta.get("ended_at"),
        "duration_s": _duration_s(meta),
        **_usd_fields(cost.get("usd"), unpriced, cost_missing=cost_missing),
        "cost_source": cost.get("cost_source") or "anthropic_cli",
        "models": {m: (None if v.get("unpriced") else round(float(v.get("usd") or 0.0), 6))
                   for m, v in by_model.items()},
        "turns": turns,
        "exit_status": outcome.get("exit_status") or ("running" if not cost else "unknown"),
        "prs_opened": outcome.get("prs_opened") or [],
        "pr_url": pr_url,
        "pr_number": pr_number,
        "tests_run": outcome.get("tests_run"),
        "tests_passed": outcome.get("tests_passed"),
        "files_changed": _files_changed(outcome),
        # Subscription quota while this run executed (ARCH-442). Carried through
        # so a spend view can say whether a day was inside the plan — the dollar
        # figures beside them are API-RATE ESTIMATES on subscription usage and
        # cannot answer that. `None` means the run reported no quota at all,
        # which is different from `False`, and a consumer must keep them apart.
        #
        # ACCOUNT-WIDE, NOT PER-RUN. The window belongs to the subscription, so
        # this says the account was in overage while the run executed, never that
        # this run's dollars came out of credits. A per-run cost column beside a
        # per-account flag invites exactly that misreading; label it.
        "overage_seen": outcome.get("overage_seen"),
        "peak_utilization": outcome.get("peak_utilization"),
        "rate_limit_status": outcome.get("rate_limit_status"),
    }


# Telemetry string → display, for the three NYH projects (ARCH-426 amendment). An
# explicit table rather than the `nyh-*` prefix match it replaces: a prefix would
# silently swallow a fourth property added later into whichever branch it happened
# to hit, and a new building should appear as itself or not at all.
_NYH_DISPLAY = {
    "nyh-arch-tools": "NYH Arch Tools",
    "nyh-etc": "East Texas Circle",
    "nyh-park-villa": "Park Villa",
    "nyh-architecture": "NYH Arch Tools",
}


def _canonical_product(product: str | None) -> str:
    """Collapse a raw repo/product string to its canonical product bucket.

    The NYH repos under the <root>/nyh-architecture/ umbrella are THREE PROJECTS,
    not one, and each reports for itself (ARCH-426 amendment). They were folded into
    a single "NYH Studio" bucket while the umbrella directory was the only thing
    holding them together; the operator's call is that per-property velocity and spend is the
    more useful view, so the fold is gone. NYH Studio survives as the PRODUCT all
    three build, which is where the family grouping now lives.

    `nyh-architecture` — the bare umbrella directory — folds to NYH Arch Tools. It
    is one historical co session from 2026-06-08, before the per-repo sessions
    existed, and arch-tools is the studio-wide project of the three. Stating the
    cost plainly: that single session is attributed to a project it may not have
    been working on. The alternative was leaving it as a raw one-row sliver.

    All three stay data-local: none of their displays is in agent-activity-report's
    _PUBLIC_TASK_PRODUCTS, so task/label remain redacted on the shared surface.

    Hollis (formerly "Personal Assistant", renamed 2026-07-01, ARCH-290) is
    tagged three ways during and after the rename: "hollis" (the renamed
    interactive co, by cwd basename), "personal-assistant" (the still-`pa`
    overnight launcher's PRODUCT until the Wave-2 operational rename), and the
    legacy "pa" alias. Fold all three into "Hollis" so the whole history and both
    live sources land in one bucket. Like "NYH Studio", "Hollis" is data-local —
    absent from _PUBLIC_TASK_PRODUCTS, so task/label stay redacted."""
    p = product or "?"
    if p in _NYH_DISPLAY:
        return _NYH_DISPLAY[p]
    if p in ("hollis", "personal-assistant", "pa"):
        return "Hollis"
    # The Keel split into two crews on 2026-08-17 (the operator), and its spend history is
    # tagged `keel` — one string covering work that is now the Library's and the
    # Bosun's. It folds to the Bosun, which inherits the platform code the Keel's
    # runs overwhelmingly touched. Attribution before the split is therefore
    # approximate in one direction, and saying so beats a raw `keel` sliver that
    # rolls up to nothing.
    if p == "keel":
        return "Bosun"
    return p


def _resolve_product(cwd: str | None) -> str:
    """The product for a co session's cwd, resolving a worktree back to its primary
    repo via `git --git-common-dir` (same rule as the SubagentStop hook). Falls back
    to the cwd basename, then '?'. Used to give a main-loop-only session (ARCH-243
    discovery below) a real product instead of leaving it '?'."""
    if not cwd:
        return "?"
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            common = Path(out.stdout.strip())
            if not common.is_absolute():
                common = (Path(cwd) / common).resolve()
            if common.parent.name:
                return common.parent.name
    except (OSError, subprocess.SubprocessError):
        pass
    return Path(cwd).name or "?"


def _uuid_transcript(uuid: str) -> Path | None:
    """The ~/.claude/projects/<sanitized-cwd>/<uuid>.jsonl transcript for a session
    UUID, or None if pruned. The parent dir name is the sanitized cwd, which is how
    _is_worker_session tells a real co session (product root) from a dispatched
    worker (a `.../worktrees/...` cwd)."""
    return next(iter(CLAUDE_PROJECTS.glob(f"*/{uuid}.jsonl")), None)


def _is_worker_session(uuid: str) -> bool:
    """True if this UUID is a dispatched worker, not a co session the operator drove.

    A worker (a co's reviewer/coder, a pa-crew agent) runs in a git worktree, so its
    transcript dir is a sanitized `.../worktrees/...` path. Its Task subagents stamp
    parent_run=<worker UUID>, which would otherwise surface the worker as a phantom
    top-level `interactive` session with a garbage product (the worktree basename).
    The worker's own cost is already captured in its run-dir node inside the real
    session tree, so we drop the phantom row. (Its Task-subagents' cost is not
    re-nested under the worker — a known small-$ gap tracked for the run-id⇄UUID
    join; better than mis-attributing it to a bogus product.)"""
    t = _uuid_transcript(uuid)
    return t is not None and "worktrees" in t.parent.name


def co_main_loop_node(uuid: str) -> dict | None:
    """Reconstruct an interactive co session's OWN main-loop cost from its transcript.

    The co session's main-loop turns (the orchestrator's Opus reasoning) leave no
    run-dir — only ~/.claude/projects/<sanitized-cwd>/<uuid>.jsonl. This sums the
    per-turn `usage` across that transcript the same way the SubagentStop hook sums a
    subagent's, prices it via telemetry.cost_from_usage, and returns a depth-0 node so
    an interactive session shows main-loop + workers (no double-count: subagent usage
    lives in the subagents' own transcripts, not the parent's). Returns None if no
    transcript is found (e.g. a session whose store was pruned)."""
    tpath = _uuid_transcript(uuid)
    if tpath is None:
        return None

    usage_by_model: dict[str, dict[str, int]] = {}
    turns = 0
    first_ts = last_ts = None
    cwd = None
    dedup = T.UsageDedup()
    try:
        lines = tpath.read_text(errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = rec.get("timestamp")
        if ts:
            first_ts = first_ts or ts
            last_ts = ts
        cwd = cwd or rec.get("cwd")
        if rec.get("type") != "assistant":
            continue
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
        model = msg.get("model")
        u = msg.get("usage")
        # Skip synthetic/interrupt turns (model "<synthetic>") — no real usage.
        if not model or model.startswith("<") or not isinstance(u, dict):
            continue
        # Claude Code writes the same assistant response into the transcript more
        # than once — same message.id + requestId, byte-identical usage (up to 4x
        # in a long session). Summing every occurrence double-counts, so an
        # interactive co main-loop reads ~2x its real cost (a $215 session reconciled
        # to $112 deduped, ARCH-383). ccusage — the pricer /spending uses — dedupes
        # on this key, so match it; T.UsageDedup is the shared implementation
        # every summing site uses rather than each hand-rolling this `seen` set.
        if dedup.is_duplicate(msg.get("id"), rec.get("requestId")):
            continue
        turns += 1
        acc = usage_by_model.setdefault(model, {})
        for k in ("input_tokens", "output_tokens",
                  "cache_read_input_tokens", "cache_creation_input_tokens"):
            acc[k] = acc.get(k, 0) + int(u.get(k, 0) or 0)
        # TTL breakdown (1h vs 5m cache writes), when the turn's usage carries
        # one — Claude Code on a subscription writes at the 1-hour TTL, and
        # T.cost_from_usage needs this to price it at the 1-hour rate instead
        # of the 5-minute rate `cache_creation_input_tokens` alone implies.
        cc = u.get("cache_creation")
        if isinstance(cc, dict):
            acc_cc = acc.setdefault("cache_creation", {})
            for k in ("ephemeral_1h_input_tokens", "ephemeral_5m_input_tokens"):
                acc_cc[k] = acc_cc.get(k, 0) + int(cc.get(k, 0) or 0)

    if not usage_by_model:
        return None
    priced = T.cost_from_usage(usage_by_model, where=f"session {uuid}")
    priced_usd = sum(v["usd"] for v in priced.values() if v.get("usd") is not None)
    unpriced = sorted(m for m, v in priced.items() if v.get("unpriced"))
    models = {m: v["usd"] for m, v in priced.items()}
    started = first_ts
    ended = last_ts
    dur = None
    s, e = parse_iso(started), parse_iso(ended)
    if s and e:
        dur = max(0.0, (e - s).total_seconds())
    # The busiest model is the main-loop model (Opus); label the node with it.
    main_model = max(priced.items(), key=lambda kv: kv[1]["tokens_out"])[0] if priced else "?"

    return {
        "run_id": f"co-{uuid[:8]}",
        "kind": "co",
        "dir": str(tpath),
        "date": tpath.parent.name,
        "agent": "co-main-loop",
        "task": "Interactive co session main loop",
        "label": "main loop",
        "model": main_model,
        "product": _canonical_product(_resolve_product(cwd)),
        "origin": "interactive",
        "profile": "co",
        "parent_run": None,
        "started_at": started,
        "ended_at": ended,
        "duration_s": dur,
        **_usd_fields(priced_usd, unpriced),
        "cost_source": "estimate_fallback",
        "models": models,
        "turns": turns,
        "exit_status": "completed",
        "prs_opened": [],
        "pr_url": None,
        "pr_number": None,
        "tests_run": None,
        "tests_passed": None,
        "files_changed": 0,
        "depth": 0,
    }


def _date_dirs(start: datetime, end: datetime) -> list[Path]:
    """The agent-runs date dirs spanning [start, end), padded one day each side so a
    tree that straddles a UTC midnight stays whole. Date dirs are named by local day
    (telemetry.platform_day), so pad in calendar days, not UTC."""
    lo = (start - timedelta(days=1)).date()
    hi = (end + timedelta(days=1)).date()
    out = []
    d = lo
    while d <= hi:
        p = RUNS_ROOT / d.isoformat()
        if p.is_dir():
            out.append(p)
        d += timedelta(days=1)
    return out


def index_nodes(start: datetime, end: datetime) -> dict[str, dict]:
    """Build {run_id: node} across the padded date window."""
    nodes: dict[str, dict] = {}
    for day in _date_dirs(start, end):
        for p in day.iterdir():
            if not p.is_dir():
                continue
            if not (p.name.startswith("run-") or p.name.startswith("sub-")):
                continue
            if p.name in nodes:
                continue
            node = load_node(p)
            if node is not None:
                nodes[node["run_id"]] = node
    return nodes


def _aggregate(nodes: list[dict]) -> dict:
    """Roll a flat list of session nodes into models{}, total_usd, prs, status.

    `total_usd` sums the priced dollars, so with any `unpriced_models` it is a
    floor; it is None when nothing was priced at all, never $0 for spend nobody
    measured. Callers show `unpriced_models` beside it."""
    models: dict[str, float | None] = {}
    total = 0.0
    prs: list = []
    unpriced: set[str] = set()
    for n in nodes:
        total += node_usd_floor(n)
        unpriced.update(n.get("unpriced_models") or [])
        for m, u in n["models"].items():
            if u is None:
                models.setdefault(m, None)
            else:
                models[m] = round((models.get(m) or 0.0) + u, 6)
        prs.extend(n["prs_opened"])
    total = round(total, 6)
    return {
        "total_usd": None if unpriced and total == 0.0 else total,
        "unpriced_models": sorted(unpriced),
        "models": models,
        "prs_opened": prs,
    }


def _span(nodes: list[dict]) -> tuple[str | None, str | None, float | None]:
    starts = [parse_iso(n["started_at"]) for n in nodes if n["started_at"]]
    ends = [parse_iso(n["ended_at"]) for n in nodes if n["ended_at"]]
    s = min(starts) if starts else None
    e = max(ends) if ends else None
    dur = (e - s).total_seconds() if (s and e) else None
    return (
        s.isoformat().replace("+00:00", "Z") if s else None,
        e.isoformat().replace("+00:00", "Z") if e else None,
        dur,
    )


def build_sessions(start: datetime, end: datetime) -> list[dict]:
    """Reconstruct session trees whose root started in [start, end).

    Returns a list of session dicts, newest root first. Each session carries a
    flattened `nodes` list (with `depth`) for the drill-down view."""
    nodes = index_nodes(start, end)

    # children map for run-* trees; UUID buckets for sub-* (interactive) sessions;
    # wave buckets for ARCH-268 phase-split nights.
    run_children: dict[str, list[str]] = {}
    interactive: dict[str, list[str]] = {}
    waves: dict[str, list[str]] = {}
    run_roots: list[str] = []

    for run_id, n in nodes.items():
        parent = n["parent_run"]
        if n["kind"] == "sub":
            # parent is a co session UUID; no run-dir matches it.
            interactive.setdefault(parent or "?", []).append(run_id)
        elif parent is not None and parent in nodes:
            run_children.setdefault(parent, []).append(run_id)
        elif n.get("co_session"):
            # An interactive `agentctl spawn` from a co session (reviewer/coder/
            # security-reviewer): no parent_run (the co main-loop is a transcript,
            # not a run-dir), but it stamped its co UUID. Nest it under that co
            # session alongside the captured Task subagents instead of orphaning
            # it as a top-level row (operator 2026-06-27).
            interactive.setdefault(n["co_session"], []).append(run_id)
        elif n.get("overnight_wave"):
            # A phase-split stage agent (triage/fix-drain/verify/digest): the bash
            # launcher is not a co session, so it has no parent_run and no
            # co_session, but it stamped the night's wave key. Nest all of one
            # night's stage runs under one synthetic "overnight night" row instead
            # of orphaning each as its own top-level row (ARCH-268 canary follow-up).
            waves.setdefault(n["overnight_wave"], []).append(run_id)
        else:
            run_roots.append(run_id)  # top-level agentctl run (or orphaned root)

    def collect(root_id: str) -> list[dict]:
        """DFS the run-* subtree rooted at root_id, tagging each node with depth."""
        out: list[dict] = []
        stack = [(root_id, 0)]
        seen: set[str] = set()
        while stack:
            rid, depth = stack.pop()
            if rid in seen or rid not in nodes:
                continue
            seen.add(rid)
            node = dict(nodes[rid], depth=depth)
            out.append(node)
            for child in run_children.get(rid, []):
                stack.append((child, depth + 1))
        return out

    sessions: list[dict] = []

    # Orphaned overnight workers: a close-loop / cycle-2 re-dispatch fires after the
    # orchestrator run ended, in an env without AGENT_WAVE or the orchestrator's
    # AGENTCTL_RUN_ID, so it carries no parent_run/co_session/overnight_wave and would
    # land here as its own top-level row (operator 2026-06-30: "Close-loop mode for PR #706"
    # showing standalone). A scheduled-origin run root that dispatched nothing — a leaf,
    # unlike an orchestrator, which has children — is such an orphan; group these by
    # (product, local day) into one synthetic follow-up row per product-night, mirroring
    # the wave synthesis below. Stamping the wave at dispatch is the real fix (tracked
    # ARCH follow-up); this keeps the dashboard correct meanwhile and is robust to any
    # future orphaning.
    orphan_workers: dict[tuple[str, str], list[str]] = {}
    real_roots: list[str] = []
    for root_id in run_roots:
        n = nodes[root_id]
        if n["origin"] == "scheduled" and root_id not in run_children:
            orphan_workers.setdefault((n["product"], n["date"]), []).append(root_id)
        else:
            real_roots.append(root_id)

    # agentctl sessions: one per run-* root.
    for root_id in real_roots:
        root = nodes[root_id]
        rs = parse_iso(root["started_at"])
        if rs is None or not (start <= rs < end):
            continue
        tree = collect(root_id)
        agg = _aggregate(tree)
        s, e, dur = _span(tree)
        sessions.append({
            "session_id": root_id,
            "kind": "agentctl",
            "root_run_id": root_id,
            "origin": root["origin"],
            "product": root["product"],
            "agent": root["agent"],
            "label": root["label"] or _short_task(root["task"]),
            "task": root["task"],
            "model": root["model"],
            "started_at": s or root["started_at"],
            "ended_at": e,
            "duration_s": dur if dur is not None else root["duration_s"],
            "worker_count": max(0, len(tree) - 1),
            "status": root["exit_status"],
            "main_loop_captured": True,
            **agg,
            "nodes": sorted(tree, key=lambda x: (x["depth"], x.get("started_at") or "")),
        })

    # Synthetic per-(product, night) row grouping each night's orphaned follow-up
    # workers, so they read as one overnight follow-up instead of a scatter of
    # standalone "Close-loop mode for PR #N" rows.
    for (product, day), rids in orphan_workers.items():
        tree = [dict(nodes[r], depth=0) for r in rids]
        s, e, dur = _span(tree)
        rs = parse_iso(s)
        if rs is None or not (start <= rs < end):
            continue
        agg = _aggregate(tree)
        sessions.append({
            "session_id": f"overnight-followup:{product}/{day}",
            "kind": "overnight",
            "root_run_id": None,
            "origin": "scheduled",
            "product": product,
            "agent": "overnight-followup",
            "label": f"overnight follow-up · {product} · {day}",
            "task": f"Overnight close-loop / re-review follow-ups ({product}, {day})",
            "model": next((node.get("model") for node in tree
                           if node.get("model") and node.get("model") != "?"), "?"),
            "started_at": s,
            "ended_at": e,
            "duration_s": dur,
            "worker_count": len(tree),
            "status": "scheduled",
            "main_loop_captured": False,
            **agg,
            "nodes": sorted(tree, key=lambda x: x.get("started_at") or "", reverse=True),
        })

    # interactive sessions: one per co session UUID. The co main-loop cost is
    # reconstructed from the session transcript (ARCH-239 gap fix); the captured
    # Task subagents are its children.
    for uuid, child_ids in interactive.items():
        # A dispatched worker (its Task subagents stamped parent_run=<worker UUID>) is
        # not a co session the operator drove — drop it so it doesn't surface as a phantom
        # top-level row with a worktree-basename product (#3b). The worker's own cost
        # already lives in its run-dir node inside the real session tree.
        if _is_worker_session(uuid):
            continue
        kids: list[dict] = []
        for c in child_ids:
            if c not in nodes:
                continue
            if nodes[c]["kind"] == "sub":
                kids.append(dict(nodes[c], depth=1))
            else:
                # An interactive agentctl spawn — collect it and any run-* subtree
                # it dispatched, offset one level under the co main-loop (depth 0).
                kids.extend(dict(sub, depth=sub["depth"] + 1) for sub in collect(c))
        main_loop = co_main_loop_node(uuid)
        if not kids and main_loop is None:
            continue
        # SDK-worker case #3b misses via transcript-location: a pa-crew agent keyed by
        # an `agent-<hex>` id leaves no product-root `<uuid>.jsonl`, so main_loop is
        # None. If every captured child is a Task subagent that ran in a git worktree,
        # this is a dispatched worker, not a co session the operator drove — suppress the phantom
        # row (a real co's Task subagents run in its product-root cwd, never a worktree).
        sub_kids = [k for k in kids if k.get("kind") == "sub"]
        if (main_loop is None and sub_kids and len(sub_kids) == len(kids)
                and all("/worktrees/" in (k.get("worktree") or "") for k in sub_kids)):
            continue
        # Product = the co's OWN cwd-resolved product (authoritative), not a child's.
        # A child that ran in a worktree carried a worktree-basename product before the
        # hook fix, and picking the first such child mislabeled the whole session (a PA
        # co with pa-crew subagents showed product `agent-<hex>`). Prefer the main loop;
        # fall back to the kids' majority product when the transcript is pruned.
        if main_loop is not None and main_loop["product"] not in ("?", None):
            product = main_loop["product"]
        else:
            cand = [k["product"] for k in kids if k["product"] not in ("?", None)]
            product = Counter(cand).most_common(1)[0][0] if cand else "?"
        tree = list(kids)
        if main_loop is not None:
            main_loop["product"] = product
            tree = [main_loop] + kids
        # Span/start over the whole tree, so the main loop (which starts first)
        # anchors the session start.
        s, e, dur = _span(tree)
        rs = parse_iso(s)
        if rs is None or not (start <= rs < end):
            continue
        agg = _aggregate(tree)
        sessions.append({
            "session_id": uuid,
            "kind": "interactive",
            "root_run_id": None,
            "origin": "interactive",
            "product": product,
            "agent": "co-session",
            "label": f"interactive · {product}",
            "task": "Interactive co session (main loop + captured subagents)"
                    if main_loop else
                    "Interactive co session (captured subagents only; transcript not found)",
            "model": main_loop["model"] if main_loop else next((k["model"] for k in kids), "?"),
            "started_at": s,
            "ended_at": e,
            "duration_s": dur,
            "worker_count": len(kids),
            "status": "interactive",
            "main_loop_captured": main_loop is not None,
            **agg,
            "nodes": sorted(tree, key=lambda x: (x.get("depth", 0), x.get("started_at") or "")),
        })

    # Deterministic-launcher waves: one synthetic session per wave key. The
    # launcher (ARCH-268 overnight, ARCH-122 weekly curation) is a bash sequencer
    # with no run of its own, so unlike an agentctl orchestrator there is no real
    # root run to collect under; we synthesize a row grouping the top-level runs it
    # spawned, each carrying the shared wave key, with each run's --parent'd workers
    # nested beneath it. The wave key is either the overnight `<product>/<day>` form
    # (no colon) or an explicit `<kind>:<scope>` form (e.g. `curation:2026-06-28`)
    # that names its own kind/label.
    for wave, stage_ids in waves.items():
        tree: list[dict] = []
        for sid in stage_ids:
            tree.extend(collect(sid))  # stage root at depth 0, its workers nested
        if not tree:
            continue
        s, e, dur = _span(tree)
        rs = parse_iso(s)
        if rs is None or not (start <= rs < end):
            continue
        agg = _aggregate(tree)
        wave_kind, sep, scope = wave.partition(":")
        if sep:  # explicit `<kind>:<scope>` wave (curation, etc.)
            kind = wave_kind
            product = "all"  # one run spans every product it swept
            label = f"{wave_kind} · {scope}"
            task = f"Weekly {wave_kind} run ({scope})"
            agent = f"{wave_kind}-run"
            session_id = wave  # already `<kind>:<scope>`, don't double the kind
        else:  # overnight `<product>/<day>` (back-compat)
            kind = "overnight"
            product, _, day = wave.partition("/")
            product = product or "?"
            label = f"overnight · {product} · {day}" if day else f"overnight · {product}"
            task = f"Overnight phase-split night ({wave})"
            agent = "overnight-night"
            session_id = f"overnight:{wave}"
        sessions.append({
            "session_id": session_id,
            "kind": kind,
            "root_run_id": None,
            "origin": "scheduled",
            "product": product,
            "agent": agent,
            "label": label,
            "task": task,
            "model": next((node.get("model") for node in tree if node.get("model") and node.get("model") != "?"), "?"),
            "started_at": s,
            "ended_at": e,
            "duration_s": dur,
            # workers = every node beyond the top-level run roots themselves.
            "worker_count": max(0, len(tree) - len(stage_ids)),
            "status": "scheduled",
            "main_loop_captured": False,
            **agg,
            "nodes": sorted(tree, key=lambda x: (x["depth"], x.get("started_at") or "")),
        })

    # Interactive co sessions whose cost lives ONLY in the transcript — a session that
    # dispatched no captured children never enters `interactive` above. The dominant co
    # cost is the main loop (ARCH-280), so dropping these under-reports spend badly
    # (operator 2026-06-30: four of five all-day sessions and ~$600 missing). Scan every
    # Convivy product-root transcript dir — excluding worktree dirs (dispatched workers,
    # #3b) and agent-runs sidecars — bounded to transcripts *touched within the window*
    # so the 15-min producer never re-parses the whole multi-GB corpus. A uuid already
    # surfaced (it had children) is skipped before the read, so only genuinely
    # main-loop-only sessions pay the parse cost. Product comes from the transcript's
    # own cwd (co_main_loop_node → _resolve_product), replacing the old PA-only allowlist.
    surfaced_uuids = {s["session_id"] for s in sessions if s.get("kind") == "interactive"}
    surfaced_uuids |= set(interactive.keys())
    # Lower-bound only: a file last written before the window can't hold an in-window
    # session, so skip it (the perf win — old transcripts aren't re-parsed). Don't
    # upper-bound: a long session that started in-window may be touched after it, and
    # the authoritative `start <= rs < end` check on the reconstructed start still
    # selects correctly. Pad a day for cross-midnight-local skew.
    lo_ts = (start - timedelta(days=1)).timestamp()
    for tdir in CLAUDE_PROJECTS.iterdir():
        name = tdir.name.lower()
        if _CONVIVY_DIR_MARKER not in name or "worktrees" in name or "agent-runs" in name:
            continue
        if not tdir.is_dir():
            continue
        for tpath in tdir.glob("*.jsonl"):
            uuid = tpath.stem
            if uuid in surfaced_uuids:
                continue  # already surfaced (it had captured children)
            try:
                if tpath.stat().st_mtime < lo_ts:
                    continue  # untouched since before the window — don't parse it
            except OSError:
                continue
            main_loop = co_main_loop_node(uuid)
            if main_loop is None:
                continue
            product = main_loop["product"]
            main_loop = dict(main_loop, depth=0)
            s, e, dur = _span([main_loop])
            rs = parse_iso(s)
            if rs is None or not (start <= rs < end):
                continue
            surfaced_uuids.add(uuid)
            agg = _aggregate([main_loop])
            sessions.append({
                "session_id": uuid,
                "kind": "interactive",
                "root_run_id": None,
                "origin": "interactive",
                "product": product,
                "agent": "co-session",
                "label": f"interactive · {product}",
                "task": "Interactive co session (main loop only; no subagents dispatched)",
                "model": main_loop["model"],
                "started_at": s,
                "ended_at": e,
                "duration_s": dur,
                "worker_count": 0,
                "status": "interactive",
                "main_loop_captured": True,
                **agg,
                "nodes": [main_loop],
            })

    sessions.sort(key=lambda x: x.get("started_at") or "", reverse=True)
    return sessions


def _short_task(task: str, n: int = 80) -> str:
    """A compact label from a long free-text task (first line, clipped)."""
    if not task:
        return ""
    first = task.strip().splitlines()[0]
    return first if len(first) <= n else first[: n - 1] + "…"


def sessions_for_days(days: int) -> list[dict]:
    """Convenience: sessions whose root started within the last `days` (local)."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    return build_sessions(start, end)


def build_outcomes(start: datetime, end: datetime) -> list[dict]:
    """Cost-per-outcome rollup (View A): one row per PR, cost summed across the
    dispatch chain that produced it.

    An outcome is a run-dir that opened a PR (has pr-link.json). Its cost is the
    subtree cost rooted at that node — the producing agent (coder/refactorer/
    bug-fixer) plus anything it spawned. Known limitation: reviewers/security-
    reviewers are usually dispatched as the orchestrator's *siblings* of the
    producing node, not its children, so their cost is attributed to the session,
    not folded into the PR here; review cost lands in the session total (View B),
    not the per-PR chain. Merge status isn't joined (the run-dir records opened,
    not merged) — that's a scorecard cross-ref left for later."""
    nodes = index_nodes(start, end)

    run_children: dict[str, list[str]] = {}
    for run_id, n in nodes.items():
        if n["kind"] == "sub":
            continue  # sub-* link by UUID and don't open PRs
        p = n["parent_run"]
        if p and p in nodes:
            run_children.setdefault(p, []).append(run_id)

    def subtree_usd(rid: str) -> tuple[float | None, list[str], bool]:
        total, stack, seen, unpriced, missing = 0.0, [rid], set(), set(), False
        while stack:
            x = stack.pop()
            if x in seen or x not in nodes:
                continue
            seen.add(x)
            total += node_usd_floor(nodes[x])
            unpriced.update(nodes[x].get("unpriced_models") or [])
            missing = missing or nodes[x]["cost_missing"]
            stack.extend(run_children.get(x, []))
        total = round(total, 6)
        unmeasured = bool(unpriced) or missing
        return ((None if unmeasured and total == 0.0 else total),
                sorted(unpriced), missing)

    def root_of(rid: str) -> str:
        cur = nodes[rid]
        while cur.get("parent_run") and cur["parent_run"] in nodes:
            rid = cur["parent_run"]
            cur = nodes[rid]
        return rid

    outcomes: list[dict] = []
    for rid, n in nodes.items():
        if not n.get("pr_url"):
            continue
        st = parse_iso(n["started_at"])
        if st is None or not (start <= st < end):
            continue
        root_id = root_of(rid)
        root = nodes.get(root_id, n)
        chain_usd, chain_unpriced, chain_missing = subtree_usd(rid)
        outcomes.append({
            "pr_url": n["pr_url"],
            "pr_number": n["pr_number"],
            "product": n["product"],
            "agent": n["agent"],
            "model": n["model"],
            "date": n["started_at"],
            "node_usd": n["usd"],
            "chain_usd": chain_usd,
            "unpriced_models": chain_unpriced,
            # A chain with a cost-missing run is a floor: its total is the priced
            # dollars, and this says some run in it has no cost record.
            "chain_cost_missing": chain_missing,
            "node_run_id": rid,
            "session_root": root_id,
            "session_origin": root["origin"],
        })
    outcomes.sort(key=lambda x: x["chain_usd"] or 0.0, reverse=True)
    return outcomes


def outcomes_for_days(days: int) -> list[dict]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    return build_outcomes(start, end)


# --- cost by work item (E16·4) ----------------------------------------------
#
# Joins each run's cost to the work item it served, from run directories only. The
# Library's per-run record is the authoritative one (platform/decisions/0020); these
# totals are the Bosun's half and say so wherever they are shown.
#
# Attribution, in order:
#   • a run whose started_at does not parse is unattributed (no-start);
#   • a supervisor (registry.is_supervisor) served several items, so it stays
#     unattributed with its own total and its cost is never split;
#   • a sub-* directory is a Task subagent (subagent). The SubagentStop hook stamps its
#     parent_run with the Claude session UUID (tools/subagent-telemetry-emit.py). The
#     same UUID is meta.co_session on each parentless run that session dispatched
#     (bin/agentctl _co_session), and one session can dispatch runs against several
#     items, so a match names a session, not an item. No sub-* is joined to an item;
#   • meta.issue, when the item it names is in the list;
#   • else the run's PR, when exactly one item's linked PR is that PR;
#   • else unattributed.
# A run whose PR links to a different item than its meta.issue is counted as a
# disagreement, and meta.issue wins.
#
# Attribution is read-time: it is computed each time the report runs, from the items'
# current PR links. Relinking an item's PR therefore moves every earlier run on that
# PR that lacks meta.issue to the new item.


def _in_window(node: dict, start: datetime, end: datetime) -> bool:
    """Whether a run started in [start, end). A run whose started_at does not parse
    has no time to test, so its directory date decides instead (the directory is named
    for the start date, see the module docstring). Such a run is then filed under
    no-start by build_item_costs, so it is still counted in the report."""
    started = parse_iso(node["started_at"])
    if started is None:
        return start.date().isoformat() <= node["date"] <= end.date().isoformat()
    return start <= started < end


def _norm_url(url: str) -> str:
    return str(url).strip().rstrip("/").lower()


# A PR link the join indexes: a convivy pull page, and nothing else.
_CONVIVY_PR_URL = re.compile(r"https://github\.com/convivy/[^/]+/pull/\d+")


def _index_prs(items: list[dict]) -> tuple[dict[str, set[str]], dict[tuple[str, str], set[str]]]:
    """Item ids by linked PR: by pr_url, and by (repo name, PR number) from pr_repo
    ("owner/name") and pr. A PR number is only meaningful with its repo.

    Only convivy's PRs are indexed. The Library stores a filer's refs as given, so a link
    to another owner's repo, or to a URL that is not a convivy pull page, joins no run.
    build_item_costs states the trust that remains."""
    by_url: dict[str, set[str]] = {}
    by_number: dict[tuple[str, str], set[str]] = {}
    for it in items:
        url = _norm_url(it["pr_url"]) if it.get("pr_url") else ""
        if _CONVIVY_PR_URL.fullmatch(url):
            by_url.setdefault(url, set()).add(it["id"])
        owner, sep, name = str(it.get("pr_repo") or "").partition("/")
        if sep and owner.lower() == "convivy" and name and it.get("pr") is not None:
            by_number.setdefault((name, str(it["pr"])), set()).add(it["id"])
    return by_url, by_number


def _linked_items(node: dict, by_url: dict, by_number: dict) -> set[str]:
    """Item ids whose linked PR is this run's PR. The run's PR number is matched with
    its own meta.repo, never with the item's product: product is a display bucket
    and does not name a repo."""
    found: set[str] = set()
    if node.get("pr_url"):
        found |= by_url.get(_norm_url(node["pr_url"]), set())
    number = node["pr_number"] if node.get("pr_number") is not None else node.get("pr")
    if number is not None and node.get("repo"):
        found |= by_number.get((node["repo"], str(number)), set())
    return found


def _plus(total: float | None, usd: float | None) -> float | None:
    """Add one run's dollars. The total is None once any run's usd is None (an unpriced
    model or no cost record), so an unmeasured run is never summed as $0."""
    if total is None or usd is None:
        return None
    return round(total + usd, 6)


def _lanes(nodes: list[dict]) -> dict[str, dict]:
    """Totals per billing lane (`billing_source`, raw). Lanes are never summed into
    one figure: each carries its own agent and model split, and the count of runs
    whose cost record is missing (`cost_missing_runs`)."""
    lanes: dict[str, dict] = {}
    for n in nodes:
        lane = lanes.setdefault(n["billing_source"], {
            "runs": 0, "usd": 0.0, "unpriced_models": set(), "cost_missing_runs": 0,
            "by_agent": {}, "by_model": {}})
        lane["runs"] += 1
        lane["usd"] = _plus(lane["usd"], n["usd"])
        lane["cost_missing_runs"] += int(n["cost_missing"])
        lane["unpriced_models"].update(n["unpriced_models"])
        lane["by_agent"][n["agent"]] = _plus(lane["by_agent"].get(n["agent"], 0.0), n["usd"])
        for m, usd in n["models"].items():
            lane["by_model"][m] = _plus(lane["by_model"].get(m, 0.0), usd)
    return {
        bs: {**lane,
             "unpriced_models": sorted(lane["unpriced_models"]),
             "by_agent": dict(sorted(lane["by_agent"].items())),
             "by_model": dict(sorted(lane["by_model"].items()))}
        for bs, lane in sorted(lanes.items())
    }


def build_item_costs(start: datetime, end: datetime, items: list[dict]) -> dict:
    """Run cost by the work item each run served, for runs that started in [start, end).

    `items` is the Library's work-item list in any state. It is an argument, so this
    makes no network call. Returns:
      items           one row per input item, in input order: run_count, run_ids,
                      attribution (runs per how they joined), by_billing_source
      unattributed    count, run_ids, by_reason and by_billing_source for every run
                      that joined no item, so each run sits in a row or here. Reasons:
                      no-start (unparseable started_at), multi-item (a supervisor),
                      subagent (a sub-* Task subagent), issue-not-listed, pr-ambiguous,
                      no-link
      disagreements   runs whose meta.issue names a different item from the one
                      their PR links to; meta.issue wins
      run_count       runs read in the window
    A None total means some run in that lane has no cost record or an unpriced model;
    cost_missing_runs counts the first kind, and the names of the second are in
    unpriced_models beside it. Attribution is read-time (see the section note above).

    Trust. A PR link is an item field the Library stores as the filer supplied it, so
    anyone who can file an item can link a convivy PR. The runs on that PR that carry no
    meta.issue then join the filer's item. The effect stops at this report's attribution.
    When two items link the same PR, its runs are unattributed (pr-ambiguous), so a forged
    link to a PR that a real item already links is visible there. A forged link can also
    raise `disagreements` for a run that carries meta.issue, without moving that run. The
    join adds no further guard against this."""
    by_url, by_number = _index_prs(items)
    item_ids = {it["id"] for it in items}
    joined: dict[str, list[dict]] = {it["id"]: [] for it in items}
    how: dict[str, Counter] = {it["id"]: Counter() for it in items}
    loose: list[tuple[dict, str]] = []
    disagreements = 0

    runs = [n for n in index_nodes(start, end).values() if _in_window(n, start, end)]
    for node in runs:
        if parse_iso(node["started_at"]) is None:
            loose.append((node, "no-start"))
            continue
        if R.is_supervisor(node["agent"]):
            loose.append((node, "multi-item"))
            continue
        if node["kind"] == "sub":
            loose.append((node, "subagent"))
            continue
        issue = node.get("issue")
        linked = _linked_items(node, by_url, by_number)
        pr_item = next(iter(linked)) if len(linked) == 1 else None
        if issue and pr_item and pr_item != issue:
            disagreements += 1
        if issue in item_ids:
            joined[issue].append(node)
            how[issue]["meta.issue"] += 1
        elif issue:
            loose.append((node, "issue-not-listed"))
        elif pr_item:
            joined[pr_item].append(node)
            how[pr_item]["pr"] += 1
        elif linked:
            loose.append((node, "pr-ambiguous"))
        else:
            loose.append((node, "no-link"))

    rows = [{
        "id": it["id"],
        "product": it.get("product"),
        "state": it.get("state"),
        "title": it.get("title"),
        "run_count": len(joined[it["id"]]),
        "run_ids": sorted(n["run_id"] for n in joined[it["id"]]),
        "attribution": {"meta.issue": how[it["id"]]["meta.issue"],
                        "pr": how[it["id"]]["pr"]},
        "by_billing_source": _lanes(joined[it["id"]]),
    } for it in items]
    return {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "run_count": len(runs),
        "items": rows,
        "unattributed": {
            "count": len(loose),
            "run_ids": sorted(n["run_id"] for n, _ in loose),
            "by_reason": dict(sorted(Counter(reason for _, reason in loose).items())),
            "by_billing_source": _lanes([n for n, _ in loose]),
        },
        "disagreements": disagreements,
    }


# --- cost by epic and by Course (E16·2) --------------------------------------
#
# Joins build_item_costs' per-item rows up one more level: to the epic each
# item's spend counts against, and the Course that orders those epics. The
# ledger can't supply these figures yet (no writer, no lane column, uncapped
# projects aren't recorded — platform/decisions/0020's successor), so this is
# built from run records and says "cannot be measured" wherever that leaves a
# gap, rather than showing a partial total.
#
# The grouping is the Library's own rule for a ledger entry's epic_id
# (store.py 3511-3515), reproduced here so a run-record figure files under the
# same key the ledger will one day be checked against: an epic counts as
# itself, a child counts under its immediate parent (one hop, never walked
# further), and a parentless non-epic goes under CORRECTIONS.

CORRECTIONS = "corrections"  # mirrors the Library's LEDGER_EPIC_CORRECTIONS (store.py:3117)

# Mirrors issue_client.RESOLVED_STATES / the Library's ITEM_RESOLUTIONS
# (store.py:1044). Kept local rather than imported: cost_join reads only run
# directories and never talks to the Library, and this is a closed set the
# Library itself pins in a client/server agreement test.
_FINISHED_STATES = frozenset({"fixed", "wontfix", "duplicate", "redirected"})


def _epic_for_item(item: dict) -> str:
    """The epic id an item's spend rolls up to, by the Library's own rule: an
    epic counts as itself; a non-epic with a parent counts under that parent,
    whether or not the parent itself resolves to a real row (the Library's rule
    is syntactic on the item's own fields, not a walk of the parent chain); a
    parentless non-epic counts under CORRECTIONS."""
    if item.get("type") == "epic":
        return item["id"]
    if item.get("parent_id"):
        return item["parent_id"]
    return CORRECTIONS


def _merge_lane_dicts(lane_dicts: list[dict[str, dict]]) -> dict[str, dict]:
    """Combine several `_lanes()`-shaped dicts into one, the same way `_lanes`
    itself combines nodes into a lane: a billing_source's `runs` adds, `usd`
    adds through `_plus` (None once any input is None), `by_agent`/`by_model`
    add per key the same way, and `unpriced_models`/`cost_missing_runs` union
    and sum. One billing_source never mixes with another — the no-cross-lane-
    summing guarantee `_lanes` keeps, extended one altitude up (several items'
    lanes rolled into one epic's, or several epics' into a Course's)."""
    merged: dict[str, dict] = {}
    for lanes in lane_dicts:
        for bs, lane in lanes.items():
            acc = merged.setdefault(bs, {
                "runs": 0, "usd": 0.0, "unpriced_models": set(),
                "cost_missing_runs": 0, "by_agent": {}, "by_model": {}})
            acc["runs"] += lane["runs"]
            acc["usd"] = _plus(acc["usd"], lane["usd"])
            acc["cost_missing_runs"] += lane["cost_missing_runs"]
            acc["unpriced_models"].update(lane["unpriced_models"])
            for agent, usd in lane["by_agent"].items():
                acc["by_agent"][agent] = _plus(acc["by_agent"].get(agent, 0.0), usd)
            for model, usd in lane["by_model"].items():
                acc["by_model"][model] = _plus(acc["by_model"].get(model, 0.0), usd)
    return {
        bs: {**lane, "unpriced_models": sorted(lane["unpriced_models"]),
             "by_agent": dict(sorted(lane["by_agent"].items())),
             "by_model": dict(sorted(lane["by_model"].items()))}
        for bs, lane in sorted(merged.items())
    }


def build_course_costs(start: datetime, end: datetime, items: list[dict],
                       course: dict, epic_progress: dict[str, dict | None]) -> dict:
    """Run cost grouped by epic and rolled up to one Course, from
    build_item_costs' per-item rows.

    `course` is the Course dict `issue_client.get_course` returns: its
    `entries` is the ordered list of epic ids this function reports on. An
    epic with attributed cost that is not one of this Course's entries is
    outside what THIS Course can report, and does not appear here — report it
    against whichever Course actually orders it. `epic_progress` maps each
    entry id to the `progress` dict `issue_client.get_item` returned for it
    (or None where that read was skipped or carried none).

    Returns:
      window        the run-cost window, as build_item_costs reports it
      epics         one row per Course entry, IN COURSE ORDER: id, title,
                    state, position (one of "reached"/"in_flight"/
                    "remaining"/"missing", from the Course's own `position`),
                    progress (from epic_progress), run_count, and lanes
                    (billing_source -> totals/by_model/by_agent/runs — never
                    summed across lanes, and never collapsed into one
                    aggregate key on the row)
      corrections   the same `lanes` shape, for items that grouped under
                    CORRECTIONS, plus the item ids that contributed
      totals        this Course's own epics' lanes, merged across epics but
                    never across lanes: billing_source -> totals/by_model
      unattributed  build_item_costs' own block, unchanged
      cannot_be_measured   True when none of the Course's own epics have any
                    run attributed to them in this window — distinct from a
                    lane that totals exactly $0 for runs that were measured
      reason        set beside cannot_be_measured when it is True, else None
    """
    item_costs = build_item_costs(start, end, items)
    rows_by_id = {row["id"]: row for row in item_costs["items"]}

    group_lanes: dict[str, list[dict]] = {}
    group_item_ids: dict[str, list[str]] = {}
    group_runs: dict[str, int] = {}
    for it in items:
        row = rows_by_id.get(it["id"])
        if not row or not row["run_count"]:
            continue
        epic_id = _epic_for_item(it)
        group_lanes.setdefault(epic_id, []).append(row["by_billing_source"])
        group_item_ids.setdefault(epic_id, []).append(it["id"])
        group_runs[epic_id] = group_runs.get(epic_id, 0) + row["run_count"]

    entries = course.get("entries") or []
    position = course.get("position") or {}
    reached = set(position.get("reached_ids") or [])
    in_flight = set(position.get("in_flight_ids") or [])
    remaining = set(position.get("remaining_ids") or [])
    resolved = {it["id"]: it for it in (course.get("items") or []) if "missing" not in it}

    def _position_of(eid: str) -> str:
        if eid in reached:
            return "reached"
        if eid in in_flight:
            return "in_flight"
        if eid in remaining:
            return "remaining"
        return "missing"

    epics = []
    for eid in entries:
        row = resolved.get(eid) or {}
        epics.append({
            "id": eid,
            "title": row.get("title"),
            "state": row.get("state"),
            "position": _position_of(eid),
            "progress": epic_progress.get(eid),
            "run_count": group_runs.get(eid, 0),
            "lanes": _merge_lane_dicts(group_lanes.get(eid, [])),
        })

    corrections = {
        "item_ids": sorted(group_item_ids.get(CORRECTIONS, [])),
        "run_count": group_runs.get(CORRECTIONS, 0),
        "lanes": _merge_lane_dicts(group_lanes.get(CORRECTIONS, [])),
    }

    cannot_be_measured = not any(e["run_count"] for e in epics)
    reason = None
    if cannot_be_measured:
        reason = ("this Course has no entries" if not entries else
                  "none of this Course's epics have any run attributed to "
                  "them in this window")

    return {
        "window": item_costs["window"],
        "epics": epics,
        "corrections": corrections,
        "totals": _merge_lane_dicts([e["lanes"] for e in epics]),
        "unattributed": item_costs["unattributed"],
        "cannot_be_measured": cannot_be_measured,
        "reason": reason,
    }


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    values = sorted(values)
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return round((values[mid - 1] + values[mid]) / 2, 6)


def build_persona_table(start: datetime, end: datetime, items: list[dict]) -> dict:
    """Cost by persona (the dispatched agent) x model x work-item type — the
    comparison E16's spec calls for: "a persona's cost is comparable across
    models on the same class of work."

    Reuses build_item_costs' own attribution (meta.issue first, then the run's
    PR), so a run lands here exactly where the per-item report placed it; a run
    that report left unattributed never enters this table either.

    Grouped at (agent, model, item type). Lanes are never summed within a
    group: each carries `runs`, `usd`, `unpriced_models`, `cost_missing_runs`,
    `finished_items` and `median_finished_item_usd`. A FINISHED item is one
    whose work-item state is terminal (fixed, wontfix, duplicate, redirected).
    The median is taken over THAT ITEM'S OWN total in this lane — summed
    across all of its runs, not only this group's — so it answers "what did a
    finished item like this end up costing", not "what did this group's slice
    of it cost"; an item with no priced cost in the lane is left out of the
    median rather than counted as $0, the same floor every total in this
    module keeps.

    Returns {"window", "rows": [{"agent", "model", "item_type", "runs",
    "lanes": {billing_source: {...}}}]}. A row exists only for a combination
    some run actually produced — there is no zero-filled cross product."""
    item_costs = build_item_costs(start, end, items)
    nodes = index_nodes(start, end)
    item_lane_totals = {row["id"]: row["by_billing_source"] for row in item_costs["items"]}
    by_item_id = {it["id"]: it for it in items}

    buckets: dict[tuple[str, str, str], dict] = {}
    for row in item_costs["items"]:
        item = by_item_id.get(row["id"]) or {}
        item_type = item.get("type") or "?"
        finished = item.get("state") in _FINISHED_STATES
        for rid in row["run_ids"]:
            node = nodes.get(rid)
            if node is None:
                continue
            bucket = buckets.setdefault(
                (node["agent"], node["model"], item_type),
                {"runs": 0, "lanes": {}, "finished_item_ids": {}})
            bucket["runs"] += 1
            lane = node["billing_source"]
            lb = bucket["lanes"].setdefault(lane, {
                "runs": 0, "usd": 0.0, "unpriced_models": set(), "cost_missing_runs": 0})
            lb["runs"] += 1
            lb["usd"] = _plus(lb["usd"], node["usd"])
            lb["cost_missing_runs"] += int(node["cost_missing"])
            lb["unpriced_models"].update(node["unpriced_models"])
            if finished:
                bucket["finished_item_ids"].setdefault(lane, set()).add(row["id"])

    rows = []
    for (agent, model, item_type), bucket in sorted(buckets.items()):
        lanes = {}
        for lane, lb in sorted(bucket["lanes"].items()):
            finished_ids = bucket["finished_item_ids"].get(lane, set())
            finished_totals = sorted(
                usd for iid in finished_ids
                if (usd := item_lane_totals.get(iid, {}).get(lane, {}).get("usd")) is not None)
            lanes[lane] = {
                "runs": lb["runs"], "usd": lb["usd"],
                "unpriced_models": sorted(lb["unpriced_models"]),
                "cost_missing_runs": lb["cost_missing_runs"],
                "finished_items": len(finished_ids),
                "median_finished_item_usd": _median(finished_totals),
            }
        rows.append({"agent": agent, "model": model, "item_type": item_type,
                    "runs": bucket["runs"], "lanes": lanes})
    return {"window": item_costs["window"], "rows": rows}
