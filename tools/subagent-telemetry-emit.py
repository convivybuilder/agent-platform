#!/usr/bin/env python3.11
"""SubagentStop telemetry emitter (ARCH-239).

A Claude Code `SubagentStop` hook. When a Task-tool subagent finishes, write the
same run-dir telemetry (meta.json / cost.json / outcome.json) that `agentctl`
spawns produce — so in-session subagents (the steward's and every co's
reviewers / coders / explorers, which agentctl never sees) become visible to the
item-2 cost/session dashboards.

Reads the hook JSON on stdin (`agent_id`, `agent_type`, `agent_transcript_path`,
`session_id`, `cwd`), parses the subagent's OWN transcript for usage / model /
turns / the task prompt, prices the usage via `telemetry.cost_from_usage` (Task
subagents, unlike agentctl's `claude -p` runs, don't self-report a `usd` figure),
and writes a run-dir keyed `parent_run=<session_id>`. Cost is computed and frozen
at emit time (the operator's call) so a later price change can't rewrite historical runs.

Invariant: **always exit 0.** Telemetry must never block a subagent from
stopping — any error is swallowed.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Resolve the telemetry module: prefer the repo working tree (tools/ -> ../lib),
# fall back to the deployed ~/.local lib. Hooks run with a thin PATH, so this is
# explicit rather than relying on PYTHONPATH.
for _cand in (
    Path(__file__).resolve().parent.parent / "lib" / "agent-platform",
    Path.home() / ".local" / "lib" / "agent-platform",
):
    if (_cand / "telemetry.py").exists():
        sys.path.insert(0, str(_cand))
        break

try:
    import telemetry as T
except Exception:
    sys.exit(0)  # telemetry unavailable → never block the subagent


def _resolve_repo(cwd: str) -> str:
    """The real product for a subagent's cwd, even when it ran in a git worktree.

    Dispatched workers (a co's reviewer/coder, a pa-crew agent) run in a worktree
    under `.../worktrees/<name>`, whose basename is a worktree label — `agent-<hex>`
    or `<product>.<agent>-<slug>` — NOT the product. Taking `Path(cwd).name` stamped
    those labels as `repo`, which then surfaced as garbage Product chips and phantom
    top-level sessions on /activity. `git --git-common-dir` points at the PRIMARY
    repo's `.git` regardless of which worktree we're in, so its parent dir's basename
    is the product. Fall back to the cwd basename when cwd isn't a git repo."""
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            common = Path(out.stdout.strip())
            if not common.is_absolute():
                common = (Path(cwd) / common).resolve()
            # common == <primary-repo>/.git  →  parent == <primary-repo>
            name = common.parent.name
            if name:
                return name
    except (OSError, subprocess.SubprocessError):
        pass
    return Path(cwd).name or "unknown"


def _first_text(content) -> str:
    """The task prompt = the first user message's text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                return b.get("text", "") or ""
    return ""


def _emit() -> None:
    try:
        ev = json.loads(sys.stdin.read())
    except Exception:
        return
    if not isinstance(ev, dict):
        return

    agent_id = (ev.get("agent_id") or "").strip()
    agent_type = (ev.get("agent_type") or "subagent").strip() or "subagent"
    tpath = ev.get("agent_transcript_path") or ""
    session_id = ev.get("session_id") or None
    cwd = ev.get("cwd") or os.getcwd()

    if not tpath or not Path(tpath).exists():
        return

    usage_by_model: dict[str, dict[str, int]] = {}
    task = ""
    turns = 0
    first_ts = last_ts = None
    dedup = T.UsageDedup()

    for line in Path(tpath).read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        ts = rec.get("timestamp")
        if ts:
            first_ts = first_ts or ts
            last_ts = ts
        typ = rec.get("type")
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
        if typ == "user" and not task:
            task = _first_text(msg.get("content"))
        elif typ == "assistant":
            model = msg.get("model")
            u = msg.get("usage")
            if model and isinstance(u, dict):
                # Claude Code writes the same assistant response into a transcript
                # more than once (same message.id + requestId, identical usage), so
                # summing every occurrence double-counts. T.UsageDedup is the shared
                # dedup every summing site uses (same fix as
                # cost_join.co_main_loop_node, ARCH-383); count a keyless record
                # rather than collapse distinct ones.
                if dedup.is_duplicate(msg.get("id"), rec.get("requestId")):
                    continue
                turns += 1
                acc = usage_by_model.setdefault(model, {})
                for k in ("input_tokens", "output_tokens",
                          "cache_read_input_tokens", "cache_creation_input_tokens"):
                    acc[k] = acc.get(k, 0) + int(u.get(k, 0) or 0)
                # TTL breakdown (1h vs 5m cache writes), when present — needed
                # so T.cost_from_usage prices a 1-hour write at the 1-hour
                # rate instead of the 5-minute rate.
                cc = u.get("cache_creation")
                if isinstance(cc, dict):
                    acc_cc = acc.setdefault("cache_creation", {})
                    for k in ("ephemeral_1h_input_tokens", "ephemeral_5m_input_tokens"):
                        acc_cc[k] = acc_cc.get(k, 0) + int(cc.get(k, 0) or 0)

    # One run-dir per subagent instance; agent_id is unique per Task subagent.
    # Sanitize before it becomes a path segment — a `/`-bearing agent_id would
    # nest the run-dir oddly within the runs root (no escape, but unwanted).
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", agent_id)
    run_id = f"sub-{safe_id}" if safe_id else T.new_run_id()
    try:
        run_dir = T.create_run_dir(run_id)
    except FileExistsError:
        return  # already emitted (hook re-fire) — don't double-count

    repo = _resolve_repo(cwd)
    model = next(iter(usage_by_model), "claude")
    started = first_ts or T.now_iso()
    ended = last_ts or started

    T.write_meta(
        run_dir, run_id=run_id, agent=agent_type,
        task=(task[:2000] or agent_type), label=None,
        started_at=started, ended_at=ended, parent_run=session_id,
        repo=repo, worktree=cwd, profile=f"{repo}/subagent", model=model,
        pid=None, billing_source="subscription_credit", origin=T.DEFAULT_ORIGIN,
    )
    # Shape-complete run-dir (REQUIRED_RUN_FILES): prompts + docs_consulted, even
    # though a Task subagent has no knowledge-ctx profile.
    T.write_prompt(run_dir, system="", user=(task[:4000]))
    T.write_docs_consulted(run_dir, profile=f"{repo}/subagent",
                           individuated_agents=[], individuated_skills=[],
                           queried_at_runtime=[])
    T.write_cost(run_dir, by_model=T.cost_from_usage(usage_by_model, where=run_id),
                 cost_source="estimate_fallback")
    T.write_outcome(run_dir, exit_status="completed")
    # Turn count + provenance (not in the core schema; the dashboard reads this
    # for the Turns column and to tell hook-emitted run-dirs from agentctl ones).
    (run_dir / "subagent.json").write_text(json.dumps(
        {"turns": turns, "agent_id": agent_id, "source": "SubagentStop"}, indent=2) + "\n")


if __name__ == "__main__":
    try:
        _emit()
    except Exception:
        pass
    sys.exit(0)
