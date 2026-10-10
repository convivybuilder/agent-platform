"""The review-replay eval harness's replay planner and dispatcher
(agent-platform#807).

Each (corpus entry, model arm) pair is one `agentctl spawn --eval-mode`
dispatch: the same roster role (reviewer/security-reviewer), checked out at
the entry's historical `sha` via `--review-sha` (never `--pr`, which
self-resolves the PR's LIVE head and could route a review-class dispatch
through `sudo review-launch`), with the candidate model substituted and
`--eval-mode` set so the run can never post to GitHub or record a review
stamp. Enforced at the PERMISSION LAYER (round-1 revise, agent-platform#813):
`tool_policy.resolve`'s eval branch grants neither `gh` nor `agentctl
review-post` under the `dontAsk` mode eval mode forces unconditionally
(bin/agentctl's `_spawn_claude`/`_disallowed_tools`), so there is no route to
post regardless of what the replayed brief tells the model to do — see
`bin/agentctl`'s `_eval_mode_system_prompt` for what the model does instead
(writes its verdict to `eval-review.json` in its own run directory, which
`collect_run_result` below reads). Dispatched as the real `agentctl` CLI via
subprocess, not an in-process import — two replays running concurrently
must not race agentctl's own module-level memo caches.

Resumable: an (entry, model) pair that already has a result file under
`results_dir` is skipped. Bounded concurrency: at most `concurrency` replays
in flight. Spend-capped: a gateway model that reaches its own `CapTracker`
cap stops dispatching ONLY that model; every other model's queue continues,
and the model's remaining pairs are recorded `not_run`, never as failures.
"""
from __future__ import annotations

import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

REASON = "review-replay eval harness (agent-platform#807)"

_RUN_ID_RE = re.compile(r"\bspawned (run-[A-Za-z0-9_-]+)")


@dataclass(frozen=True)
class ModelArm:
    """One candidate configuration to replay the corpus against. `key` is the
    name used in result filenames and report columns — for the two sonnet
    noise-floor runs, two DIFFERENT keys over the SAME model/effort/
    agents_dir, so they land as two independent result sets."""
    key: str
    model: str  # the --model value agentctl's MODEL_ALIASES / choices accepts
    effort: str | None = None
    agents_dir: str | None = None
    is_gateway: bool = False


def build_spawn_argv(entry: dict, arm: ModelArm, *, reason: str = REASON) -> list[str]:
    """The `agentctl spawn` argv for one replay. `--task` carries the
    original corpus entry's user prompt verbatim (the "same brief" the
    harness's spec requires) — read from `user_prompt_path`, not
    re-synthesized, so a wording drift in this module can't quietly change
    what the candidate is actually asked."""
    task = Path(entry["user_prompt_path"]).read_text()
    argv = ["agentctl", "spawn", entry["role"], "--on", entry["repo"],
           "--review-sha", entry["sha"], "--eval-mode",
           "--model", arm.model, "--reason", reason, "--task", task]
    if arm.effort:
        argv += ["--effort", arm.effort]
    if arm.agents_dir:
        argv += ["--agents-dir", arm.agents_dir]
    return argv


def result_path(results_dir: Path, entry_id: str, model_key: str) -> Path:
    safe = entry_id.replace("/", "__")
    return results_dir / f"{safe}__{model_key}.json"


def load_existing_results(results_dir: Path) -> dict[tuple[str, str], dict]:
    """Every result already on disk under `results_dir`, keyed by
    (entry_id, model_key) — what `plan_replays` resumes from."""
    out: dict[tuple[str, str], dict] = {}
    if not results_dir.is_dir():
        return out
    for path in results_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        key = (data.get("entry_id"), data.get("model_key"))
        if key[0] and key[1]:
            out[key] = data
    return out


def plan_replays(entries: list[dict], arms: list[ModelArm],
                 existing: dict[tuple[str, str], dict]) -> list[tuple[dict, ModelArm]]:
    """Every (entry, arm) pair that has no result yet — the resumable queue."""
    out = []
    for entry in entries:
        for arm in arms:
            if (entry["entry_id"], arm.key) not in existing:
                out.append((entry, arm))
    return out


def _parse_run_id(spawn_stdout: str, spawn_stderr: str) -> str | None:
    m = _RUN_ID_RE.search(spawn_stdout) or _RUN_ID_RE.search(spawn_stderr)
    return m.group(1) if m else None


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def find_run_dir(runs_root: Path, run_id: str) -> Path | None:
    """The same resolution `review_protocol.find_run_dir` does, duplicated
    narrowly rather than imported: that function raises `AmbiguousRunDir` for
    the merge-gate's authenticity use, which an eval replay has no stamp to
    authenticate and no reason to refuse over — the newest match is good
    enough here, not a trust decision."""
    matches = sorted(p for p in runs_root.glob("20*-*-*/") if (p / run_id).is_dir())
    return (matches[-1] / run_id) if matches else None


def collect_run_result(run_dir: Path) -> dict:
    """What a finished (or killed/errored) replay run directory says about
    itself: verdict + body (from `eval-review.json`, written by the replayed
    model itself via `Write` — its eval-mode system-prompt directive
    (`bin/agentctl`'s `_eval_mode_system_prompt`) tells it to, since `gh` and
    `agentctl review-post` are both denied at the permission layer — or read
    nothing if the model never wrote one, e.g. a run that errored before
    finishing its review), cost, and outcome."""
    cost = _read_json(run_dir / "cost.json") or {}
    outcome = _read_json(run_dir / "outcome.json") or {}
    eval_review = _read_json(run_dir / "eval-review.json")
    return {
        "verdict": eval_review.get("verdict") if eval_review else None,
        "body": eval_review.get("body") if eval_review else None,
        "tokens_in": int(cost.get("tokens_in") or 0),
        "tokens_out": int(cost.get("tokens_out") or 0),
        "usd": float(cost.get("usd") or 0.0),
        "cost_source": cost.get("cost_source"),
        "exit_status": outcome.get("exit_status"),
        "terminal_reason": outcome.get("terminal_reason"),
    }


def run_one(entry: dict, arm: ModelArm, *, runs_root: Path,
           run_subprocess: Callable = subprocess.run,
           wait_timeout: float = 1800.0) -> dict:
    """Dispatch one (entry, arm) replay, wait for it, and return its result
    dict — never raises: a spawn/wait failure is recorded as an errored
    result (cost still read from whatever run dir exists), so one bad replay
    does not take the batch down."""
    base = {"entry_id": entry["entry_id"], "model_key": arm.key,
           "repo": entry["repo"], "pr": entry["pr"], "kind": entry["kind"]}
    argv = build_spawn_argv(entry, arm)
    spawn = run_subprocess(argv, capture_output=True, text=True)
    run_id = _parse_run_id(spawn.stdout or "", spawn.stderr or "")
    if run_id is None:
        return {**base, "run_id": None, "error": "spawn",
               "spawn_exit": spawn.returncode,
               "spawn_stderr": (spawn.stderr or "").strip(),
               "usd": 0.0, "tokens_in": 0, "tokens_out": 0, "verdict": None,
               "not_run": False}
    wait = run_subprocess(["agentctl", "wait", run_id, "--timeout", str(wait_timeout)],
                          capture_output=True, text=True)
    run_dir = find_run_dir(runs_root, run_id)
    collected = collect_run_result(run_dir) if run_dir else {
        "verdict": None, "body": None, "tokens_in": 0, "tokens_out": 0,
        "usd": 0.0, "cost_source": None, "exit_status": None, "terminal_reason": None,
    }
    result = {**base, "run_id": run_id, "wait_exit": wait.returncode, **collected,
              "not_run": False}
    if wait.returncode not in (0,):
        result["error"] = f"wait exit {wait.returncode}"
    return result


def run_batch(entries: list[dict], arms: list[ModelArm], *, results_dir: Path,
              runs_root: Path, concurrency: int = 3,
              cap_tracker=None, run_subprocess: Callable = subprocess.run,
              wait_timeout: float = 1800.0) -> list[dict]:
    """Run every not-yet-done (entry, arm) pair, writing one result file per
    pair as it finishes (so a crash mid-batch loses nothing already done),
    and return the full list of results for this call (both freshly run and
    skip-resumed are NOT included here — callers that want the full picture
    read `load_existing_results` again after this returns)."""
    results_dir.mkdir(parents=True, exist_ok=True)
    existing = load_existing_results(results_dir)
    queue = plan_replays(entries, arms, existing)
    if not queue:
        return []

    def _worker(entry, arm):
        # Checked AT DISPATCH TIME, not once before the pool starts: a long
        # queue for one model can cross its cap mid-batch as earlier replays on
        # the SAME model finish and report real cost, and "stop dispatching new
        # replays when the cap is reached" means stopping as that happens, not
        # only refusing what was already known-over-cap before this call began.
        #
        # RESERVE, not check-then-record (round-1 revise, agent-platform#813):
        # the previous `can_dispatch()`-then-`record()` pair left a gap between
        # the check and the write — `record` only ran once the WHOLE replay
        # finished, so several workers could each pass the check before any of
        # them had recorded anything, a bounded overshoot of up to
        # `concurrency - 1` replays' worth of spend. `reserve` makes the check
        # and the write one atomic step (`CapTracker`'s own lock), so the next
        # worker's reservation sees this one immediately. The ESTIMATE reserved
        # is the corpus entry's own recorded cost (the ORIGINAL review's spend,
        # a different model/provider, so an approximation rather than a
        # prediction of this replay's real cost) — reconciled to the real
        # figure the moment this replay's own result is known, which is what
        # keeps the approximation from mattering: it only has to be close
        # enough to make the next `reserve` see roughly the right running
        # total, not to be exact.
        estimated = float(entry.get("usd") or 0.0)
        if (cap_tracker is not None and arm.is_gateway
                and not cap_tracker.reserve(arm.key, estimated)):
            result = {"entry_id": entry["entry_id"], "model_key": arm.key,
                      "repo": entry["repo"], "pr": entry["pr"], "kind": entry["kind"],
                      "run_id": None, "verdict": None, "usd": 0.0,
                      "tokens_in": 0, "tokens_out": 0, "not_run": True,
                      "reason": f"{arm.key} reached its spend cap"}
        else:
            result = run_one(entry, arm, runs_root=runs_root,
                             run_subprocess=run_subprocess, wait_timeout=wait_timeout)
            if cap_tracker is not None and arm.is_gateway:
                cap_tracker.reconcile(arm.key, estimated, result.get("usd") or 0.0)
        result_path(results_dir, entry["entry_id"], arm.key).write_text(
            json.dumps(result, indent=2))
        return result

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = [pool.submit(_worker, entry, arm) for entry, arm in queue]
        return [f.result() for f in futures]
