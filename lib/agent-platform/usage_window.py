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

BOTH THE TODAY AND YESTERDAY RUN-DAY DIRECTORIES ARE LISTED, EVERY CALL, AND
KEYED ON THE DISPATCHER'S OWN DAY, NOT UTC. Run-day directories are named by
`telemetry.today_dir` (`platform_day`, ARCH-156), which buckets on LOCAL
time on purpose so a day's spend stays in one bucket; this reader used to
bucket on UTC instead, and used yesterday's directory only as a fallback
when today's held no run directories at all. Around local midnight (17:00
UTC on this machine's MST) that UTC key is already "tomorrow" by the
dispatcher's own local key, so today's directory (by this reader's old UTC
clock) could hold only today's few earliest runs while the freshest reading
sat in what the dispatcher calls yesterday — and since that "today"
directory was non-empty, the fallback never fired at all (the 2026-10-12
07:35 UTC occurrence: 4 runs in the UTC-today directory, no fallback, the
freshest reading sitting in the local-yesterday directory's run-46c945f6).
Listing both of the dispatcher's own day buckets every time, and merging by
transcript mtime, removes the chance of disagreeing about which bucket
"today" names.

BOUNDED, for a reader a hook calls on a fast path, by THREE independent
caps: `MAX_CANDIDATES` run transcripts (newest `mtime` first across the two
day directories), `MAX_BYTES_READ_PER_FILE` bytes read from any one
candidate, AND `MAX_BYTES_READ` total bytes read across every candidate in
the walk. Each candidate is read back from its end in at most two reads
(first `TAIL_BYTES`, then, only if that misses, one single jump straight to
the full per-file cap) rather than a single tail read, because a reading
can sit earlier in a transcript than its last `TAIL_BYTES` — a run's
transcript keeps writing long after its last `rate_limit_info` event, so a
long run can carry its freshest reading well before the tail (the same
2026-10-12 occurrence: an 8-candidate scan's transcripts mostly had no
reading in their last 64 KiB at all).

THE PER-FILE CAP EXISTS BECAUSE THE TOTAL CAP ALONE LET ONE FILE SPEND THE
WHOLE WALK'S BUDGET. Before `MAX_BYTES_READ_PER_FILE`, a single candidate's
`tail` call was handed the walk's entire remaining `budget` as its own
`max_bytes`, so one large, reading-less transcript at the newest mtime could
read all the way to `MAX_BYTES_READ` by itself, leave nothing for the next
candidate, and return `None` even though an older candidate held a live
reading — found in a real dispatched run's transcript with a 5.5 MB gap
between two `rate_limit_info` events (byte offsets 1145846 and 6677116),
bigger than the old single 4 MiB total. Each candidate now gets only
`min(MAX_BYTES_READ_PER_FILE, <budget left in the walk>)`, so no one
candidate can exhaust the walk; `MAX_BYTES_READ` still bounds the total so
the walk as a whole cannot read without limit.

`MAX_BYTES_READ_PER_FILE` is 8 MiB and `MAX_BYTES_READ` is 32 MiB (four
times the per-file cap), so a reading up to the measured 5.5 MB gap is found
in a single candidate with headroom to spare, and the walk still admits
several full-cap candidates before giving up.

A FIRST VERSION OF THE PER-FILE CAP CUT THE REAL SCAN DEPTH BELOW
`MAX_BYTES_READ_PER_FILE` ITSELF. `_tail_window_reading`'s chunk-growth loop
re-reads the file from its growing start point to the end on every step
(needed so a reading found in the larger chunk is still correctly the
file's true last one, never an earlier one a smaller chunk already ruled
out) and counted every one of those overlapping re-reads against the
per-file cap, so the cap was being spent on the SAME bytes more than once —
a candidate stopped only 700-900 KB deep in practice instead of the full
1 MiB cap it was meant to guarantee, which is short of the 5.5 MB gap this
cap exists to cover. Fixed by sizing the growth schedule to two steps
instead of many — `TAIL_BYTES` first, then (if that misses) one single jump
straight to the full `max_bytes` — so the cap is spent on at most one
overlapping re-read (`TAIL_BYTES` extra) rather than on every intermediate
step of a geometric growth, and the real depth searched reaches `max_bytes`
itself.
"""
from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import telemetry as T

_FIVE_HOUR_KEY = "five_hour"
_SEVEN_DAY_KEY = "seven_day"

MAX_CANDIDATES = 32
TAIL_BYTES = 65_536
# Sized so a reading up to the measured 5.5 MB gap between two
# `rate_limit_info` events in a real transcript (byte offsets 1145846 and
# 6677116 — see the module docstring) is found with headroom: 8 MiB per
# file, 32 MiB (four per-file caps) across the whole walk.
MAX_BYTES_READ_PER_FILE = 8 * 1024 * 1024
MAX_BYTES_READ = 32 * 1024 * 1024

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
    candidate, freshest-mtime first, whose `tail(path, now, per_file_budget)`
    reading has not yet reset. `tail` is one of the `_tail_*_reading`
    functions below; it returns `(reading_or_None, bytes_read)`.

    `budget` starts at `MAX_BYTES_READ` and is spent across every candidate
    in this one call, so the total I/O this walk does is capped regardless
    of how many candidates `MAX_CANDIDATES` would otherwise let it reach.
    Each individual candidate's `tail` call is further capped at
    `min(MAX_BYTES_READ_PER_FILE, budget)` rather than handed the whole
    remaining `budget` — otherwise one large, reading-less transcript could
    spend the entire walk's budget by itself and leave none for the next
    candidate (see the module docstring's 5.5 MB occurrence)."""
    if now is None:
        now = time.time()
    try:
        candidates = _recent_transcripts(Path(runs_root))
    except OSError:
        return None
    budget = MAX_BYTES_READ
    for path in candidates:
        if budget <= 0:
            break
        per_file_budget = min(MAX_BYTES_READ_PER_FILE, budget)
        try:
            reading, consumed = tail(path, now, per_file_budget)
        except Exception:
            continue
        budget -= consumed
        if reading is not None and reading["resets_at"] > now:
            return reading
    return None


def _recent_transcripts(runs_root: Path) -> list[Path]:
    """Up to `MAX_CANDIDATES` run transcripts, newest `transcript.jsonl`
    mtime first, across BOTH today's and yesterday's run-day directories
    (`telemetry.today_dir`'s own local-time day key, not UTC — see the
    module docstring). Both are listed and merged every call, not only as a
    fallback when today's is empty: a day directory having runs is no
    guarantee the freshest reading is in one of them, so sorting the merged
    set by mtime is what finds the true freshest candidate across the
    boundary between the two."""
    today_str = T.today_dir()
    yesterday_str = (datetime.strptime(today_str, "%Y-%m-%d").date()
                      - timedelta(days=1)).strftime("%Y-%m-%d")
    stats = _stat_run_transcripts(runs_root / today_str)
    stats += _stat_run_transcripts(runs_root / yesterday_str)
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


def _tail_five_hour_reading(path: Path, now: float, max_bytes: int) -> tuple[dict | None, int]:
    """The last `unifiedWindows.five_hour` reading found in *path*, reading
    back from its end in at most two reads bounded by *max_bytes*, or
    `None`. Returns `(reading_or_None, bytes_read)`."""
    return _tail_window_reading(path, _FIVE_HOUR_KEY, FIVE_HOUR_WINDOW_SECONDS, now, max_bytes)


def _tail_seven_day_reading(path: Path, now: float, max_bytes: int) -> tuple[dict | None, int]:
    """The last `unifiedWindows.seven_day` reading found in *path*, or
    `None`. The `is_using_overage` key is dropped: `isUsingOverage` is
    account-level, so the seven-day line carries the same flag as the
    five-hour one, and only the five-hour reading may hold on it."""
    reading, consumed = _tail_window_reading(path, _SEVEN_DAY_KEY, SEVEN_DAY_WINDOW_SECONDS,
                                              now, max_bytes)
    if reading is not None:
        del reading["is_using_overage"]
    return reading, consumed


def _tail_window_reading(path: Path, window_key: str, window_seconds: float,
                         now: float, max_bytes: int) -> tuple[dict | None, int]:
    """The last `unifiedWindows.<window_key>` reading in *path*, or `None`.
    "Last" because a run's transcript accumulates `rate_limit_info` events
    over its whole lifetime; the freshest is whichever appears latest in
    file order.

    Reads back from the file's end in at most two steps: `TAIL_BYTES` first,
    then, only if that misses, one single jump straight to the full
    *max_bytes* depth (this candidate's own `min(MAX_BYTES_READ_PER_FILE,
    <budget left in the walk>)` cap) — never more than `TAIL_BYTES +
    max_bytes` total across the two reads, so the cap is spent on at most
    one overlapping re-read rather than on every step of a geometric growth
    (see the module docstring's "A FIRST VERSION..." paragraph). The second
    read re-parses the whole `max_bytes` window rather than only the part
    beyond the first `TAIL_BYTES`: a match in that window is still the true
    last reading in the file, never an earlier one the first, smaller read
    already ruled out, and parsing the whole window is what makes this safe
    against a `rate_limit_info` line that straddles the boundary between the
    two reads.

    *window_seconds* and *now* bound how far out a `resetsAt` may plausibly
    be — see `_RESETS_AT_SLACK_SECONDS`. Returns `(reading_or_None,
    bytes_read)`, *bytes_read* being the sum of every chunk actually read,
    for the caller to deduct from its own budget."""
    try:
        size = path.stat().st_size
    except OSError:
        return None, 0
    read_so_far = 0
    chunk_size = min(TAIL_BYTES, size, max_bytes)
    while chunk_size > 0:
        try:
            with open(path, "rb") as fh:
                fh.seek(size - chunk_size)
                data = fh.read(chunk_size)
        except OSError:
            return None, read_so_far
        read_so_far += len(data)
        found = _parse_window_reading(data.decode("utf-8", "ignore"), window_key,
                                       window_seconds, now)
        if found is not None:
            return found, read_so_far
        if chunk_size >= size or chunk_size >= max_bytes:
            return None, read_so_far
        chunk_size = min(max_bytes, size)
    return None, read_so_far


def _parse_window_reading(chunk: str, window_key: str, window_seconds: float,
                          now: float) -> dict | None:
    """The last `unifiedWindows.<window_key>` reading among *chunk*'s lines,
    or `None` — the per-chunk parse `_tail_window_reading` re-runs on every
    growth step."""
    found = None
    for line in chunk.split("\n"):
        if '"rate_limit_info"' not in line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue  # a partial line, cut by the chunk seek or a live write
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
