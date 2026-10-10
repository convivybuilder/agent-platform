"""Active-runs registry for the agent platform.

Owns `<root>/agent-runs/active.json` — the shared-state file tracking what's
currently running across `agentctl` invocations. Enforces concurrency and reaps
stale entries whose process has died.

Concurrency model (ARCH-006, revised by ARCH-206). The cap that matters is
**per-team**, not global: the original 4-agent ceiling existed because a single
orchestrator can't usefully coordinate more than ~4 workers — it's a coordination
limit. Two *independent* teams (two orchestrators, each with its own workers)
don't add to each other's coordination overhead, so each should get its own pool.
A "team" is an orchestrator plus the workers it dispatched, grouped by
`parent_run` (a worker carries its orchestrator's run-id; an orchestrator itself
is top-level, `parent_run=None`). So:

  - `max_workers_per_team` (default 4) caps the workers sharing one `parent_run`
    — the coordination limit. N teams → up to N×4 workers.
  - `max_concurrent` (default 16) is a higher GLOBAL ceiling that only guards raw
    resources (CPU/RAM/API), not coordination. Top-level runs (orchestrators,
    ad-hoc spawns with no parent) are bounded by this alone.
  - `max_live_supervisors` (default 4) caps how many SUPERVISORS may be live at
    once (ARCH-422). This is the fairness cap, and it is the one that was missing.

Why the third cap exists, and why raising `max_concurrent` was the wrong answer.
On 2026-07-29 the constance in-box drain opened PR #555 and then could not review
it: nine `agentctl spawn` attempts over ~55 minutes were refused with
`global concurrency ceiling (16/16)`. Nine of the sixteen slots held
`planning-orchestrator` runs, one per product, each holding its slot for the whole
night — so nine slots were permanently spoken for by supervisors and every worker
in the fleet contended for the remaining seven. Under draft-gated CI (ARCH-364) an
unreviewed PR never runs CI and cannot merge at all, so the starvation did not
merely delay work, it stranded it.

The operator's call was that every live supervisor must have three worker slots of its own
(four including itself). Nine supervisors × 4 = 36 concurrent runs, and a measured
run is ~300-384MB RSS on a 16GB mini already holding ~4.2GB wired for the OS and
the Docker VM. 36 runs is ~12.6GB and swaps. So the fix is NOT a bigger
`max_concurrent`: it is capping how many supervisors may be live, which makes the
entitlement affordable inside the existing resource ceiling. 16 / 4 = 4 live
supervisors, and the rest wave in as their predecessors exit.

The failure was fairness, not capacity — the machine was never out of resources,
it was out of slots that workers were allowed to have.

The three limits come from the agent policy file in the knowledge repository
(`projects/platform/policy/agent-policy.yaml`, parsed by `agent_policy`), at that
repository's origin/main and read once per process. They are not stored in
active.json. When the policy is missing or breaks a rule the limits are the
constants below, which equal the parser's fallback, and the reason is printed once
on stderr. There is no per-crew cap.

All read-modify-write operations happen under an exclusive fcntl.flock on
a separate lockfile so concurrent `agentctl spawn` invocations can't
corrupt the registry.
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

import builder_root as BR
RUNS_ROOT = BR.runs_root()
ACTIVE_PATH = RUNS_ROOT / "active.json"
LOCK_PATH = RUNS_ROOT / ".active.lock"
STALE_LOG = RUNS_ROOT / "stale-runs.jsonl"

# The limits in force when the policy file cannot be read: the numbers the policy
# file names for that case. tests/test_registry_policy.py pins them to agent_policy's own
# FALLBACK_* constants. They are literals here because agent_policy is imported
# only when a limit is read (see `limits`).
MAX_CONCURRENT_DEFAULT = 16        # GLOBAL raw-resource ceiling (ARCH-206; was 4)
MAX_WORKERS_PER_TEAM_DEFAULT = 4   # per-orchestrator coordination cap (ARCH-006/206)
MAX_LIVE_SUPERVISORS_DEFAULT = 4   # fairness cap: live supervisors (ARCH-422)
SCHEMA_VERSION = 1

# The knowledge repository whose origin/main holds the policy file.
KNOWLEDGE_REPO = BR.data_root() / "knowledge"

_LIMITS = None


def limits():
    """The `agent_policy.Policy` (or `Fallback`) the limits come from, read once per process.

    The first call reads the policy at the knowledge repo's origin/main and prints
    which source the limits came from. Every later call returns the same object,
    so a policy change reaches a process only when it starts. `can_spawn`, `add_run`
    and `reserve_slot` each call this BEFORE taking the registry lock (see
    `_locked`), so the git read never runs while the lock is held — a slow or hung
    read would otherwise stall every concurrent `agentctl spawn` on the machine,
    since each is a fresh process serializing on the same lock (round-1 finding on
    PR #694). Anything `AP.load`/`AP.policy_text_at_origin_main` does not already
    catch internally is caught here too, so a read that fails before the lock still
    yields the fallback and never an exception that stops the spawn.

    `agent_policy` is imported here, not at the top, because bin/run-register-api
    runs a deploy-pinned copy of this module beside a fixed list of siblings that
    does not include it, and that daemon only lists runs and never reads a limit.
    """
    global _LIMITS
    if _LIMITS is None:
        import agent_policy as AP
        try:
            policy = AP.load(AP.policy_text_at_origin_main(str(KNOWLEDGE_REPO)))
        except Exception as exc:  # noqa: BLE001 - any failure here must fall back, not raise
            policy = AP.Fallback(f"reading the policy raised {exc!r}")
        _LIMITS = policy
        if isinstance(_LIMITS, AP.Fallback):
            source = f"the fallback, because {_LIMITS.reason}"
        else:
            source = f"the policy file at origin/main of {KNOWLEDGE_REPO}"
        print(f"registry: concurrency limits from {source}: "
              f"max_concurrent={_LIMITS.max_concurrent} "
              f"max_workers_per_team={_LIMITS.max_workers_per_team} "
              f"max_live_supervisors={_LIMITS.max_live_supervisors}", file=sys.stderr)
    return _LIMITS

# Agents that supervise other agents rather than doing the work themselves. A
# supervisor holds its slot for the whole job while its workers come and go, which
# is exactly why an uncapped number of them starves the fleet (ARCH-422).
#
# THIS MUST STAY IN STEP WITH `is_orchestration_agent` in bin/orchestrator-tokens,
# which is the platform's existing answer to the same question and predates this.
# That predicate is `"orchestrator" in agent or agent in ORCHESTRATION_STAGE_AGENTS`,
# and this mirrors it deliberately: the first draft of this cap invented a separate
# frozenset and omitted `overnight-fix-drain` and `overnight-verify`, the ARCH-268
# phase-split stage agents. Both hold a slot and spawn workers, so the cap counted
# 4 while 5 supervisors were live — the exact hole this file's own comment warned
# about. Two definitions of one concept is how that happens (the ARCH-340 lesson).
#
# The substring test on "orchestrator" is intentional and covers every current and
# future `*-orchestrator`, including app-prefixed ones (`hollis-…`, `sel-…`) with no
# suffix gymnastics. It is more permissive than an exact set, and that is the safe
# direction: a false positive caps one extra run, a false negative silently
# re-opens the starvation for that role.
_SUPERVISOR_STAGE_AGENTS = frozenset({
    "overnight-triage",
    "overnight-fix-drain",
    "overnight-verify",
})


def is_supervisor(agent: str | None) -> bool:
    """True when `agent` is a supervisor role, including app-prefixed forms.

    Mirrors `is_orchestration_agent` (bin/orchestrator-tokens). The stage agents are
    matched by exact name or by an app prefix, since unlike the `*-orchestrator`
    names they carry no shared substring to test for.
    """
    if not agent:
        return False
    if "orchestrator" in agent:
        return True
    return any(agent == s or agent.endswith("-" + s)
               for s in _SUPERVISOR_STAGE_AGENTS)


REQUIRED_ENTRY_FIELDS = ("run_id", "agent", "task", "started_at", "repo", "worktree", "pid")

# Fields required for a two-phase reservation (no pid yet — attach_pid fills it in).
REQUIRED_RESERVATION_FIELDS = ("run_id", "agent", "task", "started_at", "repo", "worktree")

# A reserved entry (reserve_slot called, attach_pid not yet called) with no real
# pid is kept alive by the reaper for this long. If the spawning process crashes
# before attach_pid the slot is freed after this window rather than leaked forever.
RESERVATION_TIMEOUT_SECONDS = 60


class ConcurrencyError(RuntimeError):
    """Raised by add_run when a spawn would breach a cap (per-team or global).

    The message names which limit was hit. `.current` is the list of
    currently-active entries — formatters can use it to render "what's blocking
    you" output without re-reading the registry.
    """

    def __init__(self, message: str, *, current: list[dict]):
        super().__init__(message)
        self.current = current


# --- lock + IO ----------------------------------------------------------------


@contextmanager
def _locked() -> Iterator[None]:
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    # touch lockfile without truncating
    LOCK_PATH.touch(exist_ok=True)
    fd = os.open(str(LOCK_PATH), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# The limits this file used to store. They are not read any more (see `limits`).
_STORED_LIMIT_KEYS = ("max_concurrent", "max_workers_per_team", "max_live_supervisors")


def _migrate(reg: dict) -> dict:
    """Drop the limits a registry stored before they came from the policy file.

    This used to fill a missing `max_workers_per_team` and raise a legacy
    `max_concurrent` below its constant (ARCH-206), and every check read the
    stored values, so the constants in this module were the authority. The limits
    now come from `limits()` and nothing reads these keys, so a stored value could
    only mislead a person who edits it expecting an effect. That includes a stored
    `max_live_supervisors: null`, which the old check compared against a count and
    raised TypeError on. The keys are removed from the dict returned here and are
    gone from the file at the next write. Idempotent. Mutates and returns reg."""
    for key in _STORED_LIMIT_KEYS:
        reg.pop(key, None)
    return reg


def _read() -> dict:
    if not ACTIVE_PATH.exists():
        return {"version": SCHEMA_VERSION, "runs": []}
    return _migrate(json.loads(ACTIVE_PATH.read_text()))


def _write(reg: dict) -> None:
    tmp = ACTIVE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(reg, indent=2) + "\n")
    os.replace(tmp, ACTIVE_PATH)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- stale-run detection ------------------------------------------------------


def _is_alive(pid: int | None) -> bool:
    """True if a process with this pid exists. Signal-0 probe, no side effects."""
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # process exists but is owned by another user — still "alive"
        return True
    return True


def _log_stale(entries: list[dict]) -> None:
    if not entries:
        return
    STALE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with STALE_LOG.open("a") as f:
        for e in entries:
            f.write(json.dumps({"reaped_at": _now_iso(), **e}, separators=(",", ":")) + "\n")


def _reap_in_place(reg: dict) -> list[dict]:
    """Mutate reg: remove dead-PID entries from runs. Returns the dropped entries.
    Caller is responsible for persisting + logging; this is the under-lock half.

    Reserved entries (reserve_slot called, attach_pid not yet called) have no
    real pid. Keep them alive while they are recent; reap them after
    RESERVATION_TIMEOUT_SECONDS so a process that crashes between the two calls
    does not leak a slot permanently."""
    alive, stale = [], []
    now = datetime.now(timezone.utc)
    for r in reg.get("runs", []):
        if r.get("reserved"):
            try:
                ts = r["started_at"].replace("Z", "+00:00")
                age = (now - datetime.fromisoformat(ts)).total_seconds()
            except (KeyError, ValueError):
                age = RESERVATION_TIMEOUT_SECONDS + 1  # malformed — treat as expired
            (alive if age < RESERVATION_TIMEOUT_SECONDS else stale).append(r)
        elif _is_alive(r.get("pid")):
            alive.append(r)
        else:
            stale.append(r)
    reg["runs"] = alive
    return stale


# --- capacity (per-team + global) ---------------------------------------------


def _team_worker_count(runs: list[dict], parent_run: str | None) -> int:
    """Number of in-flight workers on team `parent_run` (entries whose parent_run
    matches). A None team has no workers by this measure — top-level runs
    (orchestrators, ad-hoc spawns) aren't anyone's coordinated workers."""
    if parent_run is None:
        return 0
    return sum(1 for r in runs if r.get("parent_run") == parent_run)


def _live_supervisor_count(runs: list[dict]) -> int:
    """Number of in-flight supervisor runs (ARCH-422)."""
    return sum(1 for r in runs if is_supervisor(r.get("agent")))


def _capacity(reg: dict, parent_run: str | None,
              agent: str | None, policy) -> tuple[bool, str | None]:
    """(ok, reason) for admitting one more run with the given parent_run.

    Enforces, in order: the per-team worker cap (only for a worker, i.e. a
    non-None parent_run), the live-supervisor fairness cap (only for a supervisor),
    and the global raw-resource ceiling. `reason` names the breached limit when ok
    is False, else None. ARCH-206, extended by ARCH-422.

    `agent` defaults to None so an older caller that omits it keeps working — but
    a None agent is not a supervisor, so it skips the fairness cap. That is the
    correct default for the ad-hoc spawns and workers that make up most callers,
    and it is why the supervisor cap is enforced HERE rather than trusted to each
    launcher: a launcher that forgets is a launcher that re-opens the starvation,
    which is the ARCH-311 lesson about prompt-driven gates applied to callers.

    `policy` is the already-read `limits()` result, passed in rather than read
    here: every caller of `_capacity` runs under the registry lock (`_locked`),
    and the policy read is a git read that must never happen while that lock is
    held (see `limits`). The caller reads it before taking the lock.
    """
    runs = reg.get("runs", [])
    if parent_run is not None:
        t_limit = policy.max_workers_per_team
        t_count = _team_worker_count(runs, parent_run)
        if t_count >= t_limit:
            return False, f"team {parent_run} at worker cap ({t_count}/{t_limit})"
    if is_supervisor(agent):
        s_limit = policy.max_live_supervisors
        s_count = _live_supervisor_count(runs)
        if s_count >= s_limit:
            # Named distinctly from the global ceiling on purpose: a caller that
            # sees this should WAIT for a supervisor to exit, not give up. The
            # 2026-07-29 drain gave up nine times against the global message and
            # stranded PR #555 unreviewed.
            return False, (f"live supervisor cap ({s_count}/{s_limit}) — "
                           f"wait for one to finish, then retry")
    g_limit = policy.max_concurrent
    if len(runs) >= g_limit:
        return False, f"global concurrency ceiling ({len(runs)}/{g_limit})"
    return True, None


# --- public API ---------------------------------------------------------------


def bootstrap() -> None:
    """Create active.json with an empty run list if it doesn't exist yet."""
    with _locked():
        if not ACTIVE_PATH.exists():
            _write({"version": SCHEMA_VERSION, "runs": []})


def reap_stale() -> list[dict]:
    """Drop dead-PID entries. Returns what was reaped (for logging upstream)."""
    with _locked():
        reg = _read()
        stale = _reap_in_place(reg)
        if stale:
            _write(reg)
            _log_stale(stale)
        return stale


def list_runs() -> list[dict]:
    """Return current runs after reaping stale entries."""
    with _locked():
        reg = _read()
        stale = _reap_in_place(reg)
        if stale:
            _write(reg)
            _log_stale(stale)
        return list(reg["runs"])


def can_spawn(parent_run: str | None = None,
              agent: str | None = None) -> tuple[bool, list[dict], str | None]:
    """Returns (ok, current_runs, reason). ok=False when admitting one more run for
    `parent_run` would breach the per-team worker cap (for a worker), the live
    supervisor cap (for a supervisor), or the global ceiling; `reason` names the
    breached limit (None when ok). Pass the dispatching orchestrator's run-id as
    `parent_run` for a worker spawn; leave None for a top-level run
    (orchestrator / ad-hoc).

    Pass `agent` so a supervisor spawn is checked against the fairness cap
    (ARCH-422). Omitting it treats the spawn as a non-supervisor, which is right
    for a worker or an ad-hoc spawn and wrong for a launcher fanning out
    orchestrators — `add_run`/`reserve_slot` read the agent off the entry and
    enforce it regardless, so a caller that omits it here gets a late refusal
    rather than an unenforced cap.

    Reaps stale entries first so a dead run doesn't unjustly block a spawn.

    Reads the policy via `limits()` BEFORE taking the lock (see `limits`), so the
    git read behind it never runs while the lock is held.
    """
    policy = limits()
    with _locked():
        reg = _read()
        stale = _reap_in_place(reg)
        if stale:
            _write(reg)
            _log_stale(stale)
        ok, reason = _capacity(reg, parent_run, agent, policy)
        return (ok, list(reg["runs"]), reason)


def add_run(entry: dict) -> None:
    """Add an entry to the registry under exclusive lock.

    Reaps stale entries first, then enforces concurrency. Required entry fields:
    run_id, agent, task, started_at, repo, worktree, pid. Optional: parent_run.
    """
    missing = [k for k in REQUIRED_ENTRY_FIELDS if k not in entry]
    if missing:
        raise ValueError(f"registry add_run missing fields: {missing}")

    # Read via limits() BEFORE taking the lock (see `limits`), so the git read
    # behind it never runs while the lock is held.
    policy = limits()
    with _locked():
        reg = _read()
        stale = _reap_in_place(reg)
        if stale:
            _log_stale(stale)
        ok, reason = _capacity(reg, entry.get("parent_run"), entry.get("agent"), policy)
        if not ok:
            if stale:
                _write(reg)  # persist the reap even though we're refusing
            raise ConcurrencyError(reason, current=list(reg["runs"]))
        if any(r["run_id"] == entry["run_id"] for r in reg["runs"]):
            raise ValueError(f"run_id already registered: {entry['run_id']}")
        reg["runs"].append({
            "run_id": entry["run_id"],
            "agent": entry["agent"],
            "task": entry["task"],
            "started_at": entry["started_at"],
            "repo": entry["repo"],
            "worktree": entry["worktree"],
            "pid": int(entry["pid"]),
            "pid_started_at": entry.get("pid_started_at"),
            "dispatcher_pid": entry.get("dispatcher_pid"),
            "dispatcher_pid_started_at": entry.get("dispatcher_pid_started_at"),
            "parent_run": entry.get("parent_run"),
        })
        _write(reg)


def reserve_slot(entry: dict) -> None:
    """Reserve a concurrency slot before starting the subprocess (two-phase add).

    Adds a placeholder entry with reserved=True and pid=None. Call attach_pid
    once the subprocess is running to fill in the real pid and clear the flag.
    Call remove_run on spawn failure to release the slot.

    Raises ConcurrencyError if either cap would be breached. Reaps stale entries
    first, including expired reservations, so dead runs don't block admission.
    """
    missing = [k for k in REQUIRED_RESERVATION_FIELDS if k not in entry]
    if missing:
        raise ValueError(f"reserve_slot missing fields: {missing}")
    # Read via limits() BEFORE taking the lock (see `limits`), so the git read
    # behind it never runs while the lock is held.
    policy = limits()
    with _locked():
        reg = _read()
        stale = _reap_in_place(reg)
        if stale:
            _log_stale(stale)
        ok, reason = _capacity(reg, entry.get("parent_run"), entry.get("agent"), policy)
        if not ok:
            if stale:
                _write(reg)  # persist the reap even though we're refusing
            raise ConcurrencyError(reason, current=list(reg["runs"]))
        if any(r["run_id"] == entry["run_id"] for r in reg["runs"]):
            raise ValueError(f"run_id already registered: {entry['run_id']}")
        reg["runs"].append({
            "run_id": entry["run_id"],
            "agent": entry["agent"],
            "task": entry["task"],
            "started_at": entry["started_at"],
            "repo": entry["repo"],
            "worktree": entry["worktree"],
            "pid": None,
            "dispatcher_pid": entry.get("dispatcher_pid"),
            "dispatcher_pid_started_at": entry.get("dispatcher_pid_started_at"),
            "parent_run": entry.get("parent_run"),
            "reserved": True,
        })
        _write(reg)


def attach_pid(run_id: str, pid: int, pid_started_at: str | None = None, *,
               dispatcher_pid: int | None = None,
               dispatcher_pid_started_at: str | None = None) -> None:
    """Fill in the real pid after the subprocess starts; clears reserved=True.

    `pid_started_at` (the OS's start-time string for this pid — bin/agentctl's
    `_pid_start_time`) is recorded alongside it so `_run_process_gone` can tell
    a live pid apart from one the OS has since recycled to an unrelated
    process. It is recorded HERE, not in the run's own meta.json: this
    registry is written only by the dispatcher under `fcntl.flock`, and its
    path is never exported into a run's environment the way the run
    directory is — the run itself has no path to this file and no reason to
    write it (security review, PR #457 round 2 — a run-writable copy in
    meta.json let a single bad write there condemn a live run on its own).

    `dispatcher_pid`/`dispatcher_pid_started_at` get identical treatment, one
    process over: `_died_without_finalizing` corroborates the DISPATCHER pid
    (the process that writes `outcome.json`, distinct from the child pid above)
    against these registry fields rather than the same-UID-writable copy in
    meta.json, for the reason PR #457 already closed for the child pid
    (security review, PR #641 round 1). Both are optional, defaulting to
    `None`, so a caller that only knows the child's pid (`dispatcher_pid` not
    yet computed) can still call this without disturbing an already-recorded
    dispatcher entry — passing `dispatcher_pid` is what writes it.

    Switches the reaper from the timestamp-based reservation check to the normal
    PID-liveness probe. Raises KeyError when run_id is absent (a leaked or
    double-reserved slot — shouldn't happen in normal flow).
    """
    with _locked():
        reg = _read()
        for r in reg["runs"]:
            if r["run_id"] == run_id:
                r["pid"] = int(pid)
                r["pid_started_at"] = pid_started_at
                if dispatcher_pid is not None:
                    r["dispatcher_pid"] = dispatcher_pid
                    r["dispatcher_pid_started_at"] = dispatcher_pid_started_at
                r.pop("reserved", None)
                _write(reg)
                return
        raise KeyError(f"run_id not found in registry: {run_id}")


def remove_run(run_id: str) -> bool:
    """Remove an entry. Returns True if it was present, False if it wasn't."""
    with _locked():
        reg = _read()
        before = len(reg.get("runs", []))
        reg["runs"] = [r for r in reg.get("runs", []) if r["run_id"] != run_id]
        if len(reg["runs"]) != before:
            _write(reg)
            return True
        return False


def get_run(run_id: str) -> dict | None:
    """Lookup a single entry by run_id. Returns None if not present."""
    with _locked():
        for r in _read().get("runs", []):
            if r["run_id"] == run_id:
                return r
    return None
