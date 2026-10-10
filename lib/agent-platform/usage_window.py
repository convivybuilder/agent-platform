"""usage_window — the shared reader for the account's five-hour usage window.

Two changes need this at once (both dispatched 2026-09-29, relayed by the
steward from the operator's "no to extra usage on autonomous work"):

  * issue_01M3PZ6J0D55EFC8GGAYMN9SH4 — `agentctl spawn` holds a new dispatch
    when the window is exhausted.
  * issue_01M3PZ6NBJ5XWD8F2G5EK5WB3C (this change) — every co session sees
    the window in its context, so it can finish open work before it starts
    new work.

Both need the same fact: the freshest recorded `unifiedWindows.five_hour`
reading. This module is that one reader, so neither has to duplicate the
parsing `bin/agentctl`'s `_scan_rate_limits` already does for a single run's
transcript at finalize time.

The weekly `unifiedWindows.seven_day` reading is read the same way by
`read_seven_day_window`, from the same transcript lines (the weekly dispatch
hold, 2026-10-04; `bin/agentctl`'s `_rate_limit_hold_decision` takes both).
`read_five_hour_window` is unchanged by it.

SCANS RUN RECORDS, NOT A DEDICATED STATE FILE. The dispatch-hold issue names
a state file written at finalize as an alternative ("whichever is simpler to
keep correct"). This reader takes the scan instead: a finalize-time write
means editing `_finalize_run` in `bin/agentctl`, which is the exact function
the dispatch-hold change is editing concurrently in a sibling worktree
(neither change had landed as of this one's dispatch). Scanning existing run
records needs no coordination with that edit. Whichever of the two PRs
merges second should read what the first one actually built and switch this
module to it if that turns out simpler — see this change's PR body.

WHY A SCAN AND NOT THE SESSION'S OWN TRANSCRIPT. A co session's interactive
transcript (`~/.claude/projects/*/*.jsonl`) never carries a `rate_limit_info`
event — checked directly against several thousand real transcripts on this
machine, all empty. Headless dispatched runs (`claude -p --output-format
stream-json`, what `agentctl spawn` starts) do carry it, in their own
`transcript.jsonl` under `<runs_root>/<day>/<run-id>/`. The account's
five-hour window is shared across every process using it, so the freshest
reading from ANY recent dispatched run is as current an answer as the
account has, whether or not the asking co session dispatched anything
itself.

BOUNDED, for a reader a hook calls on a fast path. At most `MAX_CANDIDATES`
run transcripts are opened, newest-`mtime` first, and each read is a tail
read (`TAIL_BYTES`) rather than the whole file, the same posture
`bin/co-session-health` already uses for its own per-turn transcript scan.
Only today's UTC run-day directory is listed; only when it holds no run
directories at all (the first requests of a new UTC day) does this fall back
to listing yesterday's — the common case never pays for two directory
listings.
"""
from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_FIVE_HOUR_KEY = "five_hour"
_SEVEN_DAY_KEY = "seven_day"

MAX_CANDIDATES = 8
TAIL_BYTES = 65_536

# A window cannot legitimately reset more than its own length after `now`.
# `_RESETS_AT_SLACK_SECONDS` widens that to absorb clock skew between the
# machine that wrote the reading and the one reading it; beyond
# `now + <window length> + _RESETS_AT_SLACK_SECONDS` a `resetsAt` is treated
# as malformed and skipped, the same as any other bad value, rather than
# trusted as a real reset time however far out it claims to be.
FIVE_HOUR_WINDOW_SECONDS = 5 * 60 * 60
SEVEN_DAY_WINDOW_SECONDS = 7 * 24 * 60 * 60
_RESETS_AT_SLACK_SECONDS = 60 * 60


def read_five_hour_window(runs_root: Path, now: float | None = None) -> dict | None:
    """The freshest recorded five-hour window reading under *runs_root* that
    has not yet reset, or `None` when nothing has ever recorded one.

    Returns `{"utilization": float, "resets_at": int, "status": str | None,
    "is_using_overage": bool}`. `resets_at` is Unix seconds, the same unit
    `rate_limit_info.resetsAt`/`unifiedWindows.five_hour.resetsAt` already
    carry.

    *now* (Unix seconds, default `time.time()`) is the "current time" a
    reading's `resets_at` is checked against: a reading at or past its own
    reset no longer describes the account's live window, so it counts as no
    reading and the scan moves to the next-freshest candidate — the same as a
    transcript with no matching event at all. Callers that need a fixed
    clock (tests) pass it explicitly.

    A reading is kept only if its `utilization` is finite and not below 0
    (no upper bound: it can exceed 1.0) and its `resetsAt` passes
    `resets_at_is_plausible`.

    Never raises: an unreadable runs root, a missing day directory, a
    transcript that vanishes mid-scan, a malformed line, a non-finite or
    negative utilization or an implausible `resetsAt` in one — every one of
    those reads as "nothing found" rather than an error. Both this reader's callers feed a fast path (a
    spawn preflight, a per-tool-call hook) that must never block or crash on
    a scan that came up empty.
    """
    return _read_window(runs_root, _tail_five_hour_reading, now)


def read_seven_day_window(runs_root: Path, now: float | None = None) -> dict | None:
    """The seven-day twin of `read_five_hour_window`: the freshest recorded
    `unifiedWindows.seven_day` reading that has not yet reset, or `None`.

    Same return shape without `is_using_overage` (see `_tail_seven_day_reading`),
    same validation and expiry rules, same scan. A seven-day `resetsAt` may be
    up to a seven-day window ahead of `now`, so
    its plausibility bound is `SEVEN_DAY_WINDOW_SECONDS`, not the five-hour
    one. A transcript carrying only a five-hour reading is no seven-day
    reading, and the scan moves on to the next candidate."""
    return _read_window(runs_root, _tail_seven_day_reading, now)


def _read_window(runs_root: Path, tail, now: float | None) -> dict | None:
    """The shared candidate walk behind both window readers: the first
    candidate, freshest-mtime first, whose `tail(path, now)` reading has not
    yet reset. `tail` is one of the `_tail_*_reading` functions below."""
    if now is None:
        now = time.time()
    try:
        candidates = _recent_transcripts(Path(runs_root))
    except OSError:
        return None
    for path in candidates:
        try:
            reading = tail(path, now)
        except Exception:
            continue
        if reading is not None and reading["resets_at"] > now:
            return reading
    return None


def _recent_transcripts(runs_root: Path) -> list[Path]:
    """Up to `MAX_CANDIDATES` run transcripts, newest `transcript.jsonl`
    mtime first."""
    today = datetime.now(timezone.utc).date()
    day_dir = runs_root / today.strftime("%Y-%m-%d")
    stats = _stat_run_transcripts(day_dir)
    if not stats:
        yesterday = today - timedelta(days=1)
        stats = _stat_run_transcripts(runs_root / yesterday.strftime("%Y-%m-%d"))
    stats.sort(key=lambda pair: pair[0], reverse=True)
    return [path for _, path in stats[:MAX_CANDIDATES]]


def _stat_run_transcripts(day_dir: Path) -> list[tuple[float, Path]]:
    stats: list[tuple[float, Path]] = []
    try:
        entries = os.scandir(day_dir)
    except OSError:
        return stats
    with entries:
        for entry in entries:
            if not entry.name.startswith("run-"):
                continue
            transcript = Path(entry.path) / "transcript.jsonl"
            try:
                mtime = transcript.stat().st_mtime
            except OSError:
                continue
            stats.append((mtime, transcript))
    return stats


def _is_finite_number(value: object) -> bool:
    """`True` for an `int`/`float` (never `bool`) that `math.isfinite`
    accepts — excludes `inf`/`-inf`/`nan`, including a value that only
    became one of those because the JSON source held an out-of-range literal
    like `1e400`, which Python's float parser silently overflows to `inf`
    rather than rejecting. Never raises: `math.isfinite` itself raises
    `OverflowError` on a Python `int` too large to convert to `float` (a bare
    oversized JSON integer literal), which this catches rather than passing
    up to a caller that may not be guarding this call."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def resets_at_is_plausible(value: object, now: float,
                           window_seconds: float = FIVE_HOUR_WINDOW_SECONDS) -> bool:
    """`True` for a `resetsAt` this module — or another reader of the same
    `unifiedWindows.<window>.resetsAt` field, such as `limit_resume.run_once`
    — should trust: a finite, non-`bool` number, positive, and no further
    ahead of *now* than one window (`window_seconds`, default five hours)
    plus `_RESETS_AT_SLACK_SECONDS` of clock-skew allowance. The `_tail_*`
    readers apply this bound to every reading they accept; a second reader
    calling this function instead of copying the arithmetic is what keeps the
    two bounds equal by construction rather than by two constants happening to
    match."""
    return _is_finite_number(value) and 0 < value <= now + window_seconds + _RESETS_AT_SLACK_SECONDS


def _tail_five_hour_reading(path: Path, now: float) -> dict | None:
    """The last `unifiedWindows.five_hour` reading in the tail of *path*, or
    `None`."""
    return _tail_window_reading(path, _FIVE_HOUR_KEY, FIVE_HOUR_WINDOW_SECONDS, now)


def _tail_seven_day_reading(path: Path, now: float) -> dict | None:
    """The last `unifiedWindows.seven_day` reading in the tail of *path*, or
    `None`. The `is_using_overage` key is dropped: `isUsingOverage` is
    account-level, so the seven-day line carries the same flag as the
    five-hour one, and only the five-hour reading may hold on it."""
    reading = _tail_window_reading(path, _SEVEN_DAY_KEY, SEVEN_DAY_WINDOW_SECONDS, now)
    if reading is not None:
        del reading["is_using_overage"]
    return reading


def _tail_window_reading(path: Path, window_key: str, window_seconds: float,
                         now: float) -> dict | None:
    """The last `unifiedWindows.<window_key>` reading in the tail of *path*,
    or `None`. "Last" because a run's transcript accumulates `rate_limit_info`
    events over its whole lifetime; the freshest is whichever appears latest
    in file order, and the tail is where that is.

    *window_seconds* and *now* bound how far out a `resetsAt` may plausibly be
    — see `_RESETS_AT_SLACK_SECONDS`."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - TAIL_BYTES))
            chunk = fh.read().decode("utf-8", "ignore")
    except OSError:
        return None
    found = None
    for line in chunk.split("\n"):
        if '"rate_limit_info"' not in line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue  # a partial line, cut by the tail seek or a live write
        if not isinstance(event, dict):
            continue
        info = event.get("rate_limit_info")
        if not isinstance(info, dict):
            continue
        windows = info.get("unifiedWindows")
        if not isinstance(windows, dict):
            continue
        window = windows.get(window_key)
        if not isinstance(window, dict):
            continue
        utilization = window.get("utilization")
        resets_at = window.get("resetsAt")
        if not _is_finite_number(utilization) or utilization < 0:
            continue
        if not resets_at_is_plausible(resets_at, now, window_seconds):
            continue
        status = info.get("status")
        found = {
            "utilization": float(utilization),
            "resets_at": int(resets_at),
            "status": status if isinstance(status, str) else None,
            "is_using_overage": bool(info.get("isUsingOverage")),
        }
    return found
