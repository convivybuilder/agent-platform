"""Daily-budget tracking for the agent platform.

Stateless. Compares today's spend against two thresholds:

- **soft warn** ($30/day default, override via `DAILY_BUDGET_USD` env or
  per-spawn `--budget-warn`): print a warning + prompt-if-tty; auto-allow
  if non-interactive (so overnight launchd doesn't die at $30.01).
- **hard cap** ($160/day default, override via `BUDGET_HARD_LIMIT_USD` env):
  refuse new spawns unconditionally. The agent's `agentctl spawn` writes a
  telemetry record with `exit_status: budget_refused`.

"Spend" is a **live** number (ARCH-065): exact completed cost (sum of `usd`
across today's finished run dirs' cost.json) PLUS an estimate of in-flight
spend. The estimate exists because the only ground-truth USD figure used to
be the CLI's `total_cost_usd`, emitted once at run *exit* — so a long-lived
run's cost was invisible until it finished. On 2026-05-26 the two overnight
orchestrators were $28 of a $41 night but each only wrote cost.json at the
very end, so the completed-only sum peaked at $11.94 and neither cap fired.

Each active *leaf* run (meta.json present, cost.json not yet written, and not
itself a parent) is now valued at its own RECORDED cost-partial.json `usd`
when `_partial_usd` accepts it — `agentctl` checkpoints this every ~30s from
the same sources the finalisation write reads
(issue_01M1D1HNMGCAKH89ZWY3XSJPGS). For a gateway run this is real metered
spend, a floor. For an Anthropic-billed run (`cost_source:
estimate_fallback`) it is the run's own summed token usage priced at list
rate, NOT the CLI's billed total — the round-1 reviewer on this PR measured
it at -12% to +16% against the eventual `cost.json` figure across five
finished runs from one day, in both directions. That sample is enough to act
on, not a bound, and not to be quoted as one — so it can itself under-report
live spend; see `write_cost_partial`'s
docstring for the full trust-boundary discussion. `_partial_usd` trusts only
a finite, non-negative recorded `usd` (the file is writable by the run it
measures, the same trust boundary as meta.json). The flat `elapsed_minutes *
BURN_RATE_USD_PER_MIN` estimate (empirically ~$0.16/min observed for an
actively-generating worker; default 0.15, override via
`BUDGET_BURN_RATE_USD_PER_MIN`) is now only the fallback for a run too young
to have written a partial yet, one running gateway-less/spend-log-less before
its first checkpoint, or one whose recorded partial fails that clamp. This is
also what fixes agent-platform#26's false soft-cap alert: an orchestrator
blocked on `agentctl wait` burns almost nothing while it waits, and its own
recorded partial now says so directly instead of being charged the leaf rate
calibrated for active generation.

**Parents are excluded from the estimate (ARCH-178).** An orchestrator's
children now each write their own cost.json and are counted live and exactly
by `today_total_usd()` as they finish. Estimating the parent's multi-hour
wall-clock at the leaf burn rate — while it sits mostly idle on `agentctl
wait` — double-counts that already-counted work: on 2026-06-17 the
overnight-orchestrator's 240-min in-flight estimate ($36) stacked on its
already-counted children (~$13) and tripped the $50 soft cap when true
autonomous spend was ~$26. See `inflight_estimate_usd`.

First-crossing markers (`.soft-budget-crossed` / `.hard-budget-crossed`)
live in the per-day run dir; downstream consumers (the spawn gate, the
budget-watchdog, the daily summary) use them to fire a single notification
per threshold per day, not one per crossing observation.

**Origin scoping (ARCH-156, extended ARCH-202).** The caps gate on the autonomous
origins — `scheduled` (launchd-launched: overnight, weekly-curation,
weekly-consulting) plus `full_monty` (the `agentctl full-monty` headroom burn) —
and every agentctl spawn descending from them. A full-monty run is capped at the
elevated ceiling its driver sets via the budget env vars. `interactive`-origin spend —
workers dispatched from a human `co` session — is tracked for visibility but
never capped: it's the operator's deliberate, in-the-loop work, not the autonomous
runaway this tripwire guards. Before this, the pool commingled both, so an
evening of interactive co work could trip the "overnight" soft cap (the
2026-06-14 false 06:52 crossing) and push the *hard* cap toward refusing
legitimate spawns. The day bucket is also local now (`telemetry.platform_day`),
so an evening no longer folds into the next day's overnight pool.
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

import telemetry as T

import builder_root as BR
RUNS_ROOT = BR.runs_root()

DEFAULT_SOFT_USD = 30.0
# Raised 80 → 160 (2026-07-10, ARCH-336) to match the orchestrator plist's
# BUDGET_HARD_LIMIT_USD=160 and the recalibration's projected cost. The $80
# default was a floor for a smaller roster; a normal full drain across the grown
# product set now legitimately costs ~$90, so contexts WITHOUT the plist env (the
# budget-watchdog, an interactive launcher introspection) were under-enforcing at
# $80 while the launchd overnight itself ran at 160. Both agree at 160 now. Soft
# stays 30 as an early warn (advisory; spawns continue).
DEFAULT_HARD_USD = 160.0
# Weekly curation runs in its OWN pool so a heavy overnight can't starve it
# (ARCH-320, the 2026-07-05 Sunday-stacking incident: the 01:00 overnight wave
# spent the shared $80 pool before the weekly-curation scribe+curator could run,
# and they were refused). Auditors stay on Opus, so curation is priced generously.
CURATION_SOFT_USD = 35.0
CURATION_HARD_USD = 50.0
DEFAULT_BURN_RATE = 0.15  # USD/min per active run; see module docstring

# Autonomous spend is gated PER POOL, not as one commingled total (ARCH-320).
# Each capped origin belongs to exactly one pool; a spawn is gated against its
# own pool's cap, so an overnight burst and the weekly curation no longer
# contend for the same ceiling. `interactive` is uncapped (absent here) — it's
# the operator's in-the-loop work. ARCH-156 established the origin split; ARCH-320 split
# the pool. `full_monty` (ARCH-202) shares the `overnight` pool: it's a
# deliberate driver-run subscription burn that raises that pool's cap via
# ENV_SOFT/ENV_HARD for the duration.
POOL_ORIGINS: dict[str, tuple[str, ...]] = {
    "overnight": ("scheduled", "full_monty"),
    "curation": ("curation",),
}
POOL_BY_ORIGIN = {o: pool for pool, origins in POOL_ORIGINS.items() for o in origins}
CAPPED_ORIGINS = tuple(POOL_BY_ORIGIN)
DEFAULT_POOL = "overnight"
POOL_DEFAULTS = {  # (soft_usd, hard_usd) per pool
    "overnight": (DEFAULT_SOFT_USD, DEFAULT_HARD_USD),
    "curation": (CURATION_SOFT_USD, CURATION_HARD_USD),
}

ENV_SOFT = "DAILY_BUDGET_USD"
ENV_HARD = "BUDGET_HARD_LIMIT_USD"
ENV_BURN = "BUDGET_BURN_RATE_USD_PER_MIN"


def marker_name(kind: str, pool: str = DEFAULT_POOL) -> str:
    """First-crossing marker filename for (kind, pool). The `overnight` pool keeps
    the legacy unsuffixed names (`.soft-budget-crossed` / `.hard-budget-crossed`)
    so existing readers (agent-summary, ops tooling) are unaffected; other pools
    get a `-<pool>` suffix."""
    if kind not in ("soft", "hard"):
        raise ValueError(f"unknown marker kind: {kind!r}")
    base = f".{kind}-budget-crossed"
    return base if pool == DEFAULT_POOL else f"{base}-{pool}"


# Legacy overnight-pool marker constants, kept for back-compat imports.
SOFT_MARKER_NAME = ".soft-budget-crossed"
HARD_MARKER_NAME = ".hard-budget-crossed"


def today_dir() -> Path:
    return RUNS_ROOT / T.platform_day()


def _run_origin(run_dir: Path) -> str:
    """A run's origin from its meta.json; `interactive` when untagged/unreadable.

    Untagged = an old run dir written before ARCH-156, or one whose meta is
    missing/corrupt. Defaulting those to `interactive` means they fall OUT of
    the capped pool, which fails safe (no false refusal) — the cap protects
    against autonomous runaway, and an unattributable run isn't provably one.

    Catches `Exception` broadly (round 4, agent-platform#592): meta.json is
    JSON fully controlled by the run being measured, and a narrower tuple let
    a pathologically-nested body raise `RecursionError` (a `RuntimeError`,
    not caught by `(json.JSONDecodeError, ValueError, OSError)`) straight out
    of `today_total_usd()`/`inflight_estimate_usd()`, which every spawn's
    budget gate and `bin/budget-watchdog`'s poll loop call synchronously.
    """
    try:
        meta = json.loads((run_dir / "meta.json").read_text())
        origin = meta.get("origin")
        return origin if origin in T.VALID_ORIGINS else T.DEFAULT_ORIGIN
    except Exception:
        return T.DEFAULT_ORIGIN


def today_total_usd(*, origin: str | None = None) -> float:
    """Sum `usd` across today's *completed* run dirs' cost.json files. Exact.

    `origin` filters to runs of that origin (`scheduled` / `interactive`);
    `None` counts every run regardless of origin.
    """
    d = today_dir()
    if not d.exists():
        return 0.0
    total = 0.0
    for run_dir in d.iterdir():
        if not run_dir.is_dir() or not run_dir.name.startswith("run-"):
            continue
        cost_path = run_dir / "cost.json"
        if not cost_path.exists():
            continue
        if origin is not None and _run_origin(run_dir) != origin:
            continue
        try:
            cost = json.loads(cost_path.read_text())
            total += float(cost.get("usd", 0.0) or 0.0)
        except Exception:
            # A run's own cost.json is JSON fully controlled by that run
            # (round 4, agent-platform#592) — catch broadly, not an
            # enumerated tuple, so one run's malformed/pathological file
            # (e.g. a deeply nested body raising RecursionError) degrades
            # only that run's contribution rather than escaping to crash
            # every spawn's budget gate.
            continue
    return round(total, 4)


def burn_rate() -> float:
    """USD/min charged against each active run. Env-overridable."""
    try:
        return float(os.environ.get(ENV_BURN, DEFAULT_BURN_RATE))
    except ValueError:
        return DEFAULT_BURN_RATE


def _pid_alive(pid: int | None) -> bool:
    """True when pid is recorded and the process exists. A missing/invalid pid
    counts as alive (fail-safe: keep estimating rather than undercount)."""
    if not pid:
        return True
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, ValueError, TypeError):
        return True


def _partial_usd(run_dir: Path) -> float | None:
    """A run's own recorded `cost-partial.json` `usd`, or None when it hasn't
    written one yet, is unreadable, or fails the clamp below — the caller
    falls back to the flat burn-rate estimate in every one of those cases.
    `T.read_cost_partial` already treats a missing/corrupt/non-regular file as
    "none"; this extracts and validates the one field this module needs.

    Trust boundary: `cost-partial.json` is writable by the run it measures
    (see `write_cost_partial`'s docstring), so this accepts only a finite,
    non-negative number — rejecting a negative, NaN, infinite, or non-numeric
    `usd` and falling back to the flat rate for that run instead. That bounds
    one run's own forged file to hiding, at most, its own spend from the
    pool-wide in-flight estimate (which it could already do by rewriting its
    own meta.json `started_at`); it can't drag the pool's figure below what
    it itself is spending, let alone the whole pool's — and, since
    `read_cost_partial` refuses a non-regular file before ever opening it
    (round 3), can't hang the reader either, so this bound is now the whole
    story rather than one of two. No further plausibility bound (e.g. against
    `elapsed_minutes * rate`) is applied — no run has been observed forging
    this file's `usd` field, and adding one here would just re-derive the
    same flat-rate estimate this file exists to improve on.

    Catches `Exception` broadly rather than an enumerated tuple (round 3,
    security-reviewer finding on agent-platform#592): `usd` is JSON fully
    controlled by the run being measured, and round 2's `except (TypeError,
    ValueError)` still let a huge integer (`10**400`) raise `OverflowError`
    out of `float()` uncaught, crashing `budget.status()`. The exact shape a
    forging run could write isn't enumerable, so this degrades on anything
    rather than re-typing to each newly observed failure mode.

    The `T.read_cost_partial(run_dir)` call itself is inside this same try
    (round 4, agent-platform#592): a 64 KiB body of nested brackets parses
    inside `read_cost_partial`'s own `json.loads` and raises `RecursionError`
    — a `RuntimeError`, outside `read_cost_partial`'s own `(JSONDecodeError,
    ValueError, UnicodeDecodeError)` tuple — so with the call sitting outside
    this handler it escaped `_partial_usd` uncaught and crashed every caller
    up the chain (`inflight_estimate_usd`, `status()`, `_budget_gate` before
    every spawn, `bin/budget-watchdog`'s poll loop)."""
    try:
        partial = T.read_cost_partial(run_dir)
        if partial is None:
            return None
        if partial.get("unpriced_models"):
            return None
        usd = float(partial.get("usd", 0.0) or 0.0)
        if not math.isfinite(usd) or usd < 0:
            return None
        return usd
    except Exception:
        return None


def _parent_run_ids(d: Path) -> set[str]:
    """Run identities that are some other run's recorded parent — i.e. runs that
    have dispatched children (orchestrators).

    A child's meta.json records `parent_run` = the dispatching run's id; this
    collects every such value across today's run dirs. Used to exclude parents
    from the in-flight estimate (see `inflight_estimate_usd`).
    """
    parents: set[str] = set()
    for run_dir in d.iterdir():
        if not run_dir.is_dir() or not run_dir.name.startswith("run-"):
            continue
        try:
            meta = json.loads((run_dir / "meta.json").read_text())
        except Exception:
            # Broad, not enumerated (round 4, agent-platform#592) — meta.json
            # is JSON fully controlled by the run being measured; see
            # `_run_origin`'s docstring for the RecursionError this guards.
            continue
        parent = meta.get("parent_run")
        if parent:
            parents.add(parent)
    return parents


def inflight_estimate_usd(*, now: datetime | None = None, origin: str | None = None) -> float:
    """Estimate the cost of today's still-running *leaf* runs.

    `origin` filters to runs of that origin (`scheduled` / `interactive`);
    `None` estimates every in-flight run regardless of origin.

    A run is in-flight when its dir has meta.json but no cost.json yet
    (cost.json is written only at exit). Each such run is valued at its own
    recorded `cost-partial.json` `usd` when `_partial_usd` accepts it — real
    metered spend for a gateway run, or a list-price valuation of summed
    usage for an Anthropic-billed one that can diverge from the eventual
    `cost.json` figure in either direction (see `write_cost_partial`'s
    docstring) — and at `elapsed_minutes * burn_rate()` as the fallback for a
    run too young to have written one yet, or whose recorded partial the
    clamp rejected. No elapsed cap on the flat-rate fallback: a hung run's
    estimate grows without bound on purpose, so a stuck/runaway agent trips
    the cap instead of silently burning (the 2026-05-26 vivy failure mode). A
    recorded partial is used exactly as written, never extrapolated forward
    past its own `written_at` — see `write_cost_partial`'s docstring on why
    staleness is left visible rather than papered over.

    **Parent (orchestrator) runs are excluded (ARCH-178).** The burn rate is
    calibrated for a leaf worker actively generating tokens (~$0.15/min). An
    orchestrator spends most of its multi-hour wall-clock blocked on
    `agentctl wait`, burning far less (~$0.04/min observed) — but the children
    it waits on each write their own cost.json and are counted exactly and
    *live* by `today_total_usd()`. Estimating the parent's wall-clock at the
    leaf rate therefore double-counts work already counted as completed
    children: on 2026-06-17 the overnight-orchestrator's 240-min in-flight
    estimate ($36) stacked on top of its already-counted children (~$13) and
    tripped the soft cap at $50.54 when true autonomous spend was ~$26. The
    parent's own slow burn lands exactly at exit; the live blind spot is small
    and bounded (~$11/night) versus the original invisible-children failure the
    estimate guards. A parent is any run whose id appears as another run's
    `parent_run`.

    A run whose recorded pid is GONE is excluded: a dead process burns
    nothing, and counting it indefinitely is how three workers killed at
    01:34–01:47 on 2026-06-11 grew a $139.86 phantom estimate that tripped
    the hard cap and refused the day's spawns. The unbounded growth stays
    for alive pids — that's the runaway-agent protection this estimate
    exists for; the dead-pid case is a bookkeeping leak, not a runaway.
    """
    d = today_dir()
    if not d.exists():
        return 0.0
    now = now or datetime.now(timezone.utc)
    rate = burn_rate()
    parents = _parent_run_ids(d)
    total = 0.0
    for run_dir in d.iterdir():
        if not run_dir.is_dir() or not run_dir.name.startswith("run-"):
            continue
        if (run_dir / "cost.json").exists():
            continue  # completed — counted exactly by today_total_usd()
        meta_path = run_dir / "meta.json"
        if not meta_path.exists():
            continue
        if origin is not None and _run_origin(run_dir) != origin:
            continue
        try:
            meta = json.loads(meta_path.read_text())
            # A parent's children are counted live by today_total_usd(); its own
            # wall-clock at the leaf burn rate would double-count them (ARCH-178).
            if run_dir.name in parents or meta.get("run_id") in parents:
                continue
            started = meta.get("started_at")
            if not started:
                continue
            if not _pid_alive(meta.get("pid")):
                continue  # died without finalizing — burns nothing
            partial_usd = _partial_usd(run_dir)
            if partial_usd is not None:
                total += partial_usd
            else:
                t0 = datetime.fromisoformat(started.replace("Z", "+00:00"))
                elapsed_min = max(0.0, (now - t0).total_seconds() / 60.0)
                total += elapsed_min * rate
        except Exception:
            # Broad, not enumerated (round 4, agent-platform#592): meta.json
            # is JSON fully controlled by the run being measured, and a
            # narrower tuple let a pathologically-nested body's
            # RecursionError escape this per-run unit and crash the whole
            # estimate for every other run in the pool.
            continue
    return round(total, 4)


def live_total_usd(*, origin: str | None = None) -> float:
    """Exact completed cost + in-flight estimate, optionally scoped to `origin`."""
    return round(today_total_usd(origin=origin) + inflight_estimate_usd(origin=origin), 4)


DAY_OVERRIDE_NAME = ".budget-override.json"


def day_override(pool: str = DEFAULT_POOL) -> dict:
    """Optional per-day cap override for `pool` at `today_dir()/.budget-override.json`.
    Two accepted shapes:
      - flat `{"soft": <usd>, "hard": <usd>}` (either key optional) → applies to
        the `overnight` pool, back-compat with the ARCH-202 full-monty raise.
      - per-pool `{"overnight": {...}, "curation": {...}}` → the block for `pool`.
    A deliberate raise for a day with an authorized one-off burn: the burn's spend
    counts toward its pool's total, so under the small default cap it would
    otherwise refuse the rest of the day's spawns in that pool. Lives in the date
    dir, so it auto-expires at the next platform day. Empty when absent, unparseable,
    or with no entry for `pool` (fail-open to env/defaults)."""
    f = today_dir() / DAY_OVERRIDE_NAME
    if not f.exists():
        return {}
    try:
        d = json.loads(f.read_text())
    except Exception:
        return {}
    if isinstance(d.get(pool), dict):
        src = d[pool]                       # per-pool shape
    elif pool == DEFAULT_POOL and ("soft" in d or "hard" in d):
        src = d                             # flat shape = overnight pool
    else:
        return {}
    try:
        return {k: float(src[k]) for k in ("soft", "hard") if k in src}
    except Exception:
        return {}


def get_thresholds(pool: str = DEFAULT_POOL, *,
                   soft_override: float | None = None) -> tuple[float, float]:
    """Resolve (soft, hard) for `pool`. Precedence: per-spawn `soft_override`
    (soft only) > per-day override file > env vars (overnight pool only, where
    full_monty raises the ceiling) > per-pool defaults."""
    d_soft, d_hard = POOL_DEFAULTS[pool]
    ov = day_override(pool)
    # ENV_SOFT/ENV_HARD historically raise the overnight pool (full_monty); scope
    # them there so they don't silently move the curation cap too.
    env_soft = os.environ.get(ENV_SOFT) if pool == DEFAULT_POOL else None
    env_hard = os.environ.get(ENV_HARD) if pool == DEFAULT_POOL else None
    if soft_override is not None:
        soft = float(soft_override)
    elif "soft" in ov:
        soft = ov["soft"]
    elif env_soft is not None:
        soft = float(env_soft)
    else:
        soft = d_soft
    if "hard" in ov:
        hard = ov["hard"]
    elif env_hard is not None:
        hard = float(env_hard)
    else:
        hard = d_hard
    return (soft, hard)


def status(pool: str = DEFAULT_POOL, *, soft_override: float | None = None) -> dict:
    """Live budget snapshot for `pool` (completed + in-flight estimate). Pure; no
    side effects.

    `spent_today_usd` and the crossing flags scope to `pool`'s origins (ARCH-320):
    `overnight` = scheduled + full_monty, `curation` = curation. Interactive spend
    is reported alongside (`interactive_today_usd`) for visibility but never gates.
    """
    soft, hard = get_thresholds(pool, soft_override=soft_override)
    origins = POOL_ORIGINS[pool]
    completed = sum(today_total_usd(origin=o) for o in origins)
    inflight = sum(inflight_estimate_usd(origin=o) for o in origins)
    spent = round(completed + inflight, 4)
    interactive = live_total_usd(origin="interactive")
    return {
        "pool": pool,
        "spent_today_usd": spent,
        "completed_usd": completed,
        "estimated_inflight_usd": inflight,
        "interactive_today_usd": interactive,
        "soft_usd": soft,
        "hard_usd": hard,
        "soft_crossed": spent >= soft,
        "hard_crossed": spent >= hard,
        "soft_marker_exists": (today_dir() / marker_name("soft", pool)).exists(),
        "hard_marker_exists": (today_dir() / marker_name("hard", pool)).exists(),
    }


def mark_crossed(kind: str, pool: str = DEFAULT_POOL) -> bool:
    """Touch the first-crossing marker for (kind, pool). Returns True if it was
    newly created (this is the first crossing today for that pool), False if it
    already existed.

    Downstream notifiers should fire only when this returns True so the user
    gets one Slack ping per day per (pool, threshold), not one per spawn after
    the threshold is hit.
    """
    marker = today_dir() / marker_name(kind, pool)
    today_dir().mkdir(parents=True, exist_ok=True)
    if marker.exists():
        return False
    marker.touch()
    return True
