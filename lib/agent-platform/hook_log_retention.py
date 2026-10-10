"""Retention for the Claude Code hook-event spool (ARCH-342 class; Deck WS1.1).

THE DISCLOSURE THIS CLOSES. `tools/session-event-log.py` appends every hook event
to `<root>/agent-runs/hook-log/<UTC-day>/<session-id>.jsonl`. A
`UserPromptSubmit` payload carries the user's full typed prompt, and tool inputs
carry whatever a command line can. Nothing deleted any of it, so the corpus grew
without bound and the window widened every day.

THE POLICY (the operator, 2026-07-19). Two mechanisms, deliberately independent:

  1. **Purge after index.** A line is deleted once the reducer has extracted its
     state. The reducer records how far it got; this module deletes up to that
     watermark and no further.
  2. **A 14-day backstop.** Any day 14 or more days old is deleted outright,
     whatever the index says (the boundary is inclusive: 14 days back goes,
     13 stays). A reducer outage must not silently stop pruning
     and leave prompt text accumulating. 14 days is double the longest plausible
     unattended outage (a week away from the machine) and half the 30-day
     fallback that was considered.

**The reducer does not exist yet (it lands in WS1.3), so the backstop is the only
pruning in force today.** That is the intended order: retention ships before
anything reads the logs, not after.

WHAT IS NOT IN SCOPE. Every record carries `transcript_path`, which points at a
Claude Code transcript under `~/.claude/projects/`. Pruning a line does not touch
that file, so the conversation it names survives independently. Those transcripts
are Anthropic's to manage (observed retention is a rolling ~30 days — see the KB
contract `agent-platform/contracts/claude-transcript-jsonl`) and pruning them is a
separate decision about a corpus `cost_join.py` actively depends on. Stated
plainly here so nobody reads "purged" as "the conversation is gone."

THE APPEND RACE, AND WHY THERE ISN'T ONE. The emitter appends to the file named
by the *current* UTC day. Once that day rolls over, no emitter will ever open
that file again, so a closed day's files have no writer. This module therefore
**never touches today's directory**, which is what makes a plain
rewrite-and-rename safe: there is no in-flight append to lose. Every function
here takes `today` explicitly rather than reading the clock, so the boundary is
testable.
"""
from __future__ import annotations

import datetime
import json
import os
import shutil
import tempfile
from pathlib import Path

import builder_root as BR
LOG_ROOT = BR.runs_root() / "hook-log"

# The reducer's watermark files. One per session, holding the timestamp through
# which that session's events have been reduced into state.
INDEX_STATE_DIR = LOG_ROOT / ".index-state"

BACKSTOP_DAYS = 14

# Dirs 0700, files 0600 — the emitter's modes. A pruned file must not come back
# more readable than the one it replaced.
DIR_MODE = 0o700
FILE_MODE = 0o600


def utc_today() -> datetime.date:
    """Today in UTC, matching the emitter's day-directory convention.

    Deliberately UTC, unlike the run-dir day key, which is local (ARCH-156). The
    emitter files a record by UTC day; a local key here would delete a directory
    the emitter still considers current.
    """
    return datetime.datetime.now(datetime.timezone.utc).date()


def _parse_day(name: str) -> datetime.date | None:
    try:
        return datetime.datetime.strptime(name, "%Y-%m-%d").date()
    except ValueError:
        return None


def day_dirs(root: Path) -> list[tuple[datetime.date, Path]]:
    """Every parseable `YYYY-MM-DD` directory under root, oldest first.

    Anything that does not parse as a date is skipped rather than guessed at.
    The spool root also holds `.metadata_never_index` and `.index-state`, and a
    stray directory must never be a deletion candidate.
    """
    out = []
    if not root.is_dir():
        return out
    for p in sorted(root.iterdir()):
        if not p.is_dir():
            continue
        day = _parse_day(p.name)
        if day is not None:
            out.append((day, p))
    return out


# A watermark may sit slightly ahead of the clock — the reducer's host may skew,
# and it stamps its own time. It may not sit two days ahead. See the rejection
# reasons in `indexed_through`.
MAX_WATERMARK_SKEW = datetime.timedelta(hours=48)


def _valid_watermark(value: str, *, now: datetime.datetime | None = None) -> bool:
    """Whether a watermark is safe to delete against.

    Two rejections, both learned from the security review of this module:

    **Format.** The comparison against a record's `ts` is a STRING compare, which
    is only correct when both sides use the emitter's `isoformat()` shape ending
    `+00:00`. A `Z`-suffixed watermark silently OVER-DELETES: `.` (46) < `Z` (90),
    so `...T10:30:00.999999+00:00 <= ...T10:30:00Z` is true and a record after the
    watermark gets dropped. Rejecting the format is the fix; a comment warning the
    reducer's author is not.

    **Horizon.** Nothing bounded how far ahead a watermark could be. A reducer bug
    writing a max-sentinel (`9999-12-31...`) as a completion marker would satisfy
    the comparison for EVERY line and delete a whole session's log, including
    lines it never processed. That surface opens the day WS1.3 ships, so the
    guard lands here, in the code that does the deleting, rather than in the code
    that does the writing.
    """
    if not value.endswith("+00:00"):
        return False
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        return False
    # No tzinfo check: anything that passes the `+00:00` guard above and parses
    # is necessarily aware, so the branch would be unreachable and would imply a
    # path that matters.
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return parsed <= now + MAX_WATERMARK_SKEW


def indexed_through(session_id: str, *, state_dir: Path = INDEX_STATE_DIR,
                    now: datetime.datetime | None = None) -> str | None:
    """The timestamp through which the reducer has consumed this session.

    Returns None when there is no watermark, or when the one present is not safe
    to delete against — no file, unreadable, not a dict, missing the field, empty,
    wrong timestamp format, or too far in the future.

    None means "nothing is known to be indexed", so the purge-after-index pass
    deletes nothing and the backstop remains the only thing that prunes. Failing
    toward retention is the right direction for a *correctness* bug; the backstop
    is what stops that from becoming unbounded.
    """
    path = state_dir / f"{session_id}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("indexed_through")
    if not isinstance(value, str) or not value:
        return None
    if not _valid_watermark(value, now=now):
        return None
    return value


def _line_ts(line: str) -> str | None:
    """The record's `ts`, or None if the line is not a parseable record."""
    try:
        rec = json.loads(line)
    except Exception:
        return None
    if not isinstance(rec, dict):
        return None
    ts = rec.get("ts")
    return ts if isinstance(ts, str) else None


def prune_file(path: Path, watermark: str | None) -> tuple[int, int]:
    """Drop lines whose `ts` is at or before the watermark. Returns (kept, dropped).

    A line that does not parse, or carries no `ts`, is KEPT. We cannot show it has
    been indexed, and deleting a record we cannot read is the wrong way to be
    wrong. The backstop collects it later regardless.

    The rewrite goes through a temp file in the same directory and an atomic
    rename, so a crash mid-prune leaves the original intact rather than a
    half-written log.
    """
    if watermark is None:
        return (0, 0)

    try:
        original = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return (0, 0)

    keep = []
    dropped = 0
    for line in original:
        if not line.strip():
            continue
        ts = _line_ts(line)
        # String compare, which is correct ONLY because both sides use the
        # emitter's `datetime.now(timezone.utc).isoformat()` shape — `+00:00`,
        # optional microseconds — where lexicographic order matches chronological
        # order. Any other shape is rejected upstream by `_valid_watermark`
        # rather than tolerated here; a `Z`-suffixed watermark would over-delete
        # within its own second.
        if ts is not None and ts <= watermark:
            dropped += 1
            continue
        keep.append(line)

    if dropped == 0:
        return (len(keep), 0)

    if not keep:
        path.unlink(missing_ok=True)
        return (0, dropped)

    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=TEMP_PREFIX)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(keep) + "\n")
        os.chmod(tmp, FILE_MODE)
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise
    return (len(keep), dropped)


TEMP_PREFIX = ".prune-"


def _sweep_orphan_temps(day_dir: Path) -> int:
    """Remove `.prune-*` temp files a killed run left behind.

    `mkstemp` + `os.replace` is crash-safe for the DATA — the original survives
    an interrupted rewrite. What it leaves is the temp file: a SIGKILL between
    the two strands a `.prune-XXXXXX` holding that session's kept lines. It is
    0600 so it is not a disclosure, but nothing collected it, and `glob("*.jsonl")`
    never sees it, so orphans would accumulate silently and forever.

    Only ever called on a closed day dir, where no run is legitimately mid-write.
    """
    swept = 0
    for p in day_dir.glob(f"{TEMP_PREFIX}*"):
        try:
            p.unlink()
            swept += 1
        except OSError:
            pass
    return swept


def state_dir_for(root: Path) -> Path:
    """The watermark directory belonging to a given spool root.

    Derived from `root` rather than defaulting to the live tree. A default
    anchored to LOG_ROOT means `--root /tmp/whatever` reads PRODUCTION
    watermarks and deletes from the test tree accordingly — the two roots
    silently disagree about which sessions are indexed. Caught by running the
    CLI against a scratch spool; the unit tests missed it because they always
    passed `state_dir` explicitly.
    """
    return root / ".index-state"


def purge_after_index(root: Path, today: datetime.date, *,
                      state_dir: Path | None = None) -> dict:
    """Delete indexed lines from every CLOSED day directory.

    Today's directory is skipped: its files still have a live writer, and a
    rewrite would drop whatever the emitter appended between read and rename.
    """
    state_dir = state_dir if state_dir is not None else state_dir_for(root)
    stats = {"files": 0, "lines_dropped": 0, "days": 0, "errors": 0, "orphans": 0}
    for day, day_dir in day_dirs(root):
        if day >= today:
            continue
        stats["days"] += 1
        stats["orphans"] += _sweep_orphan_temps(day_dir)
        for f in sorted(day_dir.glob("*.jsonl")):
            # Per-file, so one unreadable file cannot take down the pass. The
            # backstop is the guarantee everything else sits on, and it runs
            # AFTER this; letting a permissions error propagate would cancel it
            # for the day and leave prompt text sitting past its window.
            try:
                watermark = indexed_through(f.stem, state_dir=state_dir)
                _, dropped = prune_file(f, watermark)
            except Exception:
                stats["errors"] += 1
                continue
            if dropped:
                stats["files"] += 1
                stats["lines_dropped"] += dropped
    return stats


def apply_backstop(root: Path, today: datetime.date, *,
                   days: int = BACKSTOP_DAYS) -> dict:
    """Delete whole day directories older than the backstop, index or no index.

    Whole-directory deletion rather than line-level: past the backstop nothing in
    the day is worth keeping, and removing the directory cannot leave a partially
    pruned file behind.

    This is the guarantee. Everything else is an optimization on top of it.
    """
    cutoff = today - datetime.timedelta(days=days)
    stats = {"days_deleted": 0, "deleted": []}
    for day, day_dir in day_dirs(root):
        if day >= today:
            continue
        if day > cutoff:
            continue
        shutil.rmtree(day_dir, ignore_errors=True)
        stats["days_deleted"] += 1
        stats["deleted"].append(day_dir.name)
    return stats


def run(root: Path = LOG_ROOT, today: datetime.date | None = None, *,
        days: int = BACKSTOP_DAYS, state_dir: Path | None = None) -> dict:
    """One retention pass: purge indexed lines, then apply the backstop.

    Order matters only for reporting — the backstop deletes whole days the purge
    may have just touched. Running the purge first keeps its line count honest
    for days that survive.
    """
    today = today or utc_today()
    purged = purge_after_index(root, today, state_dir=state_dir)
    backstopped = apply_backstop(root, today, days=days)
    return {"purged": purged, "backstop": backstopped}
