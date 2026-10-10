"""Retention for `<root>/agent-runs` (issue_01M109N0BN6N0H47XFASSN2FJJ, bosun).

THE TREE THIS BOUNDS. Every dispatched agent leaves a run directory under
`agent-runs/<day>/` and nothing has ever deleted from it: 11GB across 101 day
directories and 14,000 run directories, growing about 109MB a day. `run-observer`
walks the whole tree nightly, so one script that inflates it raises the floor of
every future sweep permanently.

THE KEY, AND WHY IT IS SAFE DESPITE BEING FORGEABLE. Every timestamp on a run
directory — name, mtime, atime, birthtime — is same-UID writable by the party
being retained, so none of them can be trusted the way a purge watermark is
trusted in `hook_log_retention.py`. The day-directory NAME is used anyway,
because the predicate placed on it is a closed TWO-SIDED calendar band rather
than a one-sided cutoff:

    keep a day directory iff its name parses as a real calendar date AND that
    date falls within [today - D, today]. Act on it otherwise.

Both ends. A future-dated directory (`2099-01-01`) is outside the band on the
HIGH side and is deleted on the very first pass — no name buys permanence,
because every value a forger can choose leads to deletion, sooner if it tries to
be clever. A one-sided `if day < cutoff` is the bug this module exists to
reject: it passes almost every test you could write for it and restores exactly
the permanence the design removes.

TWO INDEPENDENT MECHANISMS, mirroring `hook_log_retention.py`'s purge-plus-
backstop shape for the same reason: the mechanisms answer different questions,
and one failing must not stop the other.

  1. **Thin at THIN_DAYS.** Reduce a run directory to the JSON facts its
     long-range consumers read (KEEP_FILES) and delete everything else — the
     transcript, tool-call log, harness stdio, prompts. The keep set is ~32MB
     across the whole 101-day corpus; the drop set is essentially the rest.
  2. **Prune at PRUNE_DAYS.** Delete the whole day directory. `bin/agent-
     activity-report` re-reads 90 days of run directories every 15 minutes, so
     100 gives ten days of slack past that floor.

WHAT THIS DOES NOT CLOSE. A flood created inside today's window is unreachable
by either mechanism — retention converts "permanent" into "100 nights", not into
"bounded". The Library batch endpoint is the fix for peak sweep cost, and it is
a separate repo.

NO SIZE OR COUNT BOUND, DELIBERATELY. That is the design most people reach for
and it must not be built here: it couples one cheap `mkdir` to one destroyed
real run directory, because eviction under a budget ranks over attacker-
writable keys. The calendar band chooses nothing — each directory's fate is a
pure function of its own name and today's date, so creating a million
directories changes no other directory's fate.

Every policy function here takes `today` as a parameter, never reading the
clock itself, so the boundary is testable — the `hook_log_retention.py`
convention.
"""
from __future__ import annotations

import datetime
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".local" / "lib" / "agent-platform"))
import telemetry as T  # noqa: E402

RUNS_ROOT = T.RUNS_ROOT

THIN_DAYS = 35
PRUNE_DAYS = 100

# Character for character, `run-observer`'s day-directory pattern. `\Z`, not
# `$`: in Python `$` also matches before a trailing newline, and a filename may
# legally contain one. Scope positively by this pattern and never by an
# exclusion list — the root holds `active.json`, `hook-log/`, `stale-runs.jsonl`,
# `scorecard/`, `velocity/`, `activity/`, `overnight/`, `queues/` and about 130
# more entries this module must never touch.
DAY_DIR_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")

# The facts a run directory's long-range consumers read: cost_join (meta.json,
# cost.json), outcome/PR tracking (outcome.json, pr-link.json), and the
# review/policy/doc trail (policy.json, verdict.json, docs_consulted.json,
# subagent.json). Kept as an ALLOWLIST rather than a denylist of the bulk files
# — the run-dir contract states the file list is open, not closed, so a new
# per-run file defaults to being dropped rather than silently exempted.
KEEP_FILES = {
    "meta.json",
    "cost.json",
    "outcome.json",
    "pr-link.json",
    "policy.json",
    "verdict.json",
    "docs_consulted.json",
    "subagent.json",
    # A session's directory stays in the day bucket of its first call, so a
    # long-lived session can write its handoff into a bucket past THIN_DAYS.
    "handoff.md",
}


def local_today() -> datetime.date:
    """Today as a LOCAL calendar date, matching `telemetry.platform_day()`.

    Deliberately local, unlike `hook_log_retention.utc_today()`: the emitter
    that names a hook-log day directory files by UTC day, but the dispatcher
    that names a run-dir day directory files by LOCAL day (ARCH-156). Reading
    the clock as UTC here would treat the directory being written right now as
    a candidate.
    """
    return datetime.datetime.strptime(T.platform_day(), "%Y-%m-%d").date()


def parse_day_name(name: str) -> datetime.date | None:
    """The real calendar date `name` names, or None if it does not name one.

    Distinct from matching `DAY_DIR_RE`: a name can match that pattern
    character for character and still fail here (`2026-13-45`). Both cases —
    fails `DAY_DIR_RE`, or matches it but fails here — must not be confused:
    the former is never a retention candidate at all (see `day_dir_candidates`);
    the latter IS a candidate, and `in_band` treats it as unable to satisfy the
    band, which is what deletes it rather than leaving it to survive forever on
    a cutoff comparison that never fires.
    """
    try:
        return datetime.datetime.strptime(name, "%Y-%m-%d").date()
    except ValueError:
        return None


def day_dir_candidates(root: Path) -> list[Path]:
    """Every retention candidate at `root`: a name matching `DAY_DIR_RE`.

    Symlinked entries are skipped, never followed — the same rule `run-
    observer` applies to the identical walk. Positive scope by pattern only:
    everything else at the root (the registry, the hook-log spool, scorecard
    ledgers, ~130 other entries) is left strictly alone because it never
    matches the pattern, not because it is named on an exclusion list.
    """
    out: list[Path] = []
    if not root.is_dir():
        return out
    for p in sorted(root.iterdir()):
        if not DAY_DIR_RE.match(p.name):
            continue
        if p.is_symlink() or not p.is_dir():
            continue
        out.append(p)
    return out


def in_band(day: datetime.date, today: datetime.date, days: int) -> bool:
    """Whether `day` falls in the closed band `[today - days, today]`.

    Both ends. A future `day` (today's date walked past by a forged name, or a
    clock skew) fails the high end exactly as an old `day` fails the low end —
    there is no comparison here that a future date silently satisfies.
    """
    return today - datetime.timedelta(days=days) <= day <= today


def should_act(name: str, today: datetime.date, days: int) -> bool:
    """Whether the day directory named `name` is a target under a `days` window.

    True when `name` matches the day pattern but does not parse as a real date
    (act on it — nothing else ever will, since no cutoff comparison can fire on
    a value that never parses), or when it parses but falls outside the closed
    band on either side. False only when it parses AND falls inside the band.
    """
    day = parse_day_name(name)
    if day is None:
        return True
    return not in_band(day, today, days)


def _rmtree_named(path: Path) -> None:
    """`shutil.rmtree(path)`, naming and re-raising instead of hiding errors.

    `ignore_errors=True` swallows a partial-deletion failure at the OS-call
    level before any surrounding try/except sees it, so a tree that gets
    mostly-but-not-fully deleted reads identically to a clean success. `onerror`
    lets each failing call be named and logged, then re-raised so the caller's
    existing per-directory try/except counts it — `bin/run-retention`'s one
    line per pass is this job's only other signal.
    """
    failures: list[str] = []

    def onerror(func, err_path, exc_info):
        failures.append(f"{err_path}: {exc_info[1]}")

    shutil.rmtree(path, onerror=onerror)
    if failures:
        for msg in failures:
            print(f"run-retention: rmtree error: {msg}", file=sys.stderr)
        raise OSError(f"{len(failures)} error(s) removing {path}: {failures[0]}")


def thin_run_dir(run_dir: Path) -> int:
    """Delete everything in `run_dir` except `KEEP_FILES`. Returns files/dirs dropped.

    Allowlist by name, not by extension or shape — `cost.json` and `meta.json`
    are never candidates for deletion here, which is the invariant that keeps a
    thinned run from ever reading as in-flight (in-flight is encoded as
    meta.json present, cost.json absent).
    """
    dropped = 0
    for child in sorted(run_dir.iterdir()):
        if child.name in KEEP_FILES:
            continue
        if child.is_symlink():
            child.unlink(missing_ok=True)
        elif child.is_dir():
            _rmtree_named(child)
        else:
            child.unlink(missing_ok=True)
        dropped += 1
    return dropped


def apply_thin(root: Path, today: datetime.date, *, days: int = THIN_DAYS) -> dict:
    """Thin every run/sub/co directory inside a day directory outside the band.

    Per-day-directory AND per-run-directory `try/except`, so one hostile
    directory (a planted fifo, a permissions trap, a directory that vanishes
    concurrently) costs that directory rather than the whole pass — the same
    reasoning `hook_log_retention.purge_after_index` applies per file, and
    `apply_prune` applies per day directory. Listing a day directory's
    children (`day_dir.iterdir()`) is itself something that can raise —
    `sorted()` forces the whole listing eagerly, so a directory that goes
    unreadable or vanishes between `day_dir_candidates` and here surfaces
    right here, not partway through iteration — and without this guard that
    exception would propagate out of `apply_thin` entirely, silently
    cancelling thinning for every day directory later in the sorted order.
    Kind-agnostic by construction: `run-`, `sub-` and `co-` directories are
    walked identically, with no per-kind regex.
    """
    stats = {"day_dirs": 0, "runs_thinned": 0, "errors": 0}
    for day_dir in day_dir_candidates(root):
        try:
            act = should_act(day_dir.name, today, days)
        except Exception:
            stats["errors"] += 1
            continue
        if not act:
            continue
        stats["day_dirs"] += 1
        try:
            run_dirs = sorted(day_dir.iterdir())
        except Exception as exc:
            stats["errors"] += 1
            print(f"run-retention: thin: {day_dir} unreadable, skipping: {exc}",
                  file=sys.stderr)
            continue
        for run_dir in run_dirs:
            if run_dir.is_symlink() or not run_dir.is_dir():
                continue
            try:
                thin_run_dir(run_dir)
                stats["runs_thinned"] += 1
            except Exception as exc:
                stats["errors"] += 1
                print(f"run-retention: thin: {run_dir} failed: {exc}", file=sys.stderr)
    return stats


def apply_prune(root: Path, today: datetime.date, *, days: int = PRUNE_DAYS) -> dict:
    """Delete whole day directories outside the `days` band. The guarantee.

    Whole-directory `shutil.rmtree`, not line- or file-level: past the prune
    window nothing in the day is worth keeping, and removing the directory
    outright cannot leave a partially thinned one behind. Per-directory
    `try/except` mirrors `hook_log_retention.apply_backstop`'s reasoning: one
    unreadable day must not cancel the pass for every other doomed day.
    """
    stats = {"days_deleted": 0, "deleted": [], "errors": 0}
    for day_dir in day_dir_candidates(root):
        try:
            act = should_act(day_dir.name, today, days)
        except Exception:
            stats["errors"] += 1
            continue
        if not act:
            continue
        try:
            _rmtree_named(day_dir)
        except Exception:
            stats["errors"] += 1
            continue
        stats["days_deleted"] += 1
        stats["deleted"].append(day_dir.name)
    return stats


def run(root: Path = RUNS_ROOT, today: datetime.date | None = None, *,
        thin_days: int = THIN_DAYS, prune_days: int = PRUNE_DAYS) -> dict:
    """One retention pass: thin, then prune — independently.

    Each half runs inside its own `try/except` so a defect in one (an
    unexpected exception escaping every inner guard) cannot cancel the other.
    Order is for reporting only: pruning a day thinning just touched is
    harmless, wasted work, not a correctness question, since thinning is
    idempotent and prune's `shutil.rmtree` removes whatever thinning left.
    """
    today = today or local_today()
    try:
        thinned = apply_thin(root, today, days=thin_days)
    except Exception as exc:
        thinned = {"day_dirs": 0, "runs_thinned": 0, "errors": 1, "exception": str(exc)}
    try:
        pruned = apply_prune(root, today, days=prune_days)
    except Exception as exc:
        pruned = {"days_deleted": 0, "deleted": [], "errors": 1, "exception": str(exc)}
    return {"thinned": thinned, "pruned": pruned}
