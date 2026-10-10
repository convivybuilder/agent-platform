"""usage_window — the shared five-hour window reader (issue_01M3PZ6NBJ5XWD8F2G5EK5WB3C,
issue_01M3PZ6J0D55EFC8GGAYMN9SH4).

Builds run directories under a scratch `runs_root` by hand rather than through
`telemetry.create_run_dir`, so each test controls the transcript's exact bytes
and the directory's mtime independently.
"""
from __future__ import annotations

import json
import os
import time
import unittest.mock as mock
from datetime import datetime, timedelta, timezone

import pytest

import usage_window as UW


def _utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _utc_yesterday() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")


def _make_run(runs_root, run_id, *, day=None, lines=(), mtime=None):
    """A run directory with a `transcript.jsonl` of *lines* (already JSON
    strings), under *day* (default: today, UTC). Returns the transcript path.
    `mtime`, when given, is applied to the transcript after writing, so a
    test can control scan order independent of write order."""
    day_dir = runs_root / (day or _utc_today())
    run_dir = day_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    transcript = run_dir / "transcript.jsonl"
    transcript.write_text("\n".join(lines) + ("\n" if lines else ""))
    if mtime is not None:
        os.utime(transcript, (mtime, mtime))
    return transcript


def _event(*, five_hour=None, seven_day=None, status="allowed", is_using_overage=False,
          top_level_only=False):
    info = {"status": status, "isUsingOverage": is_using_overage}
    if top_level_only:
        # The older shape `_scan_rate_limits`'s own tests use: no
        # `unifiedWindows` at all, just top-level fields for whichever window
        # triggered the event.
        if five_hour is not None:
            info.update(rateLimitType="five_hour", **five_hour)
        return json.dumps({"type": "rate_limit_event", "rate_limit_info": info})
    windows = {}
    if five_hour is not None:
        windows["five_hour"] = five_hour
    if seven_day is not None:
        windows["seven_day"] = seven_day
    info["unifiedWindows"] = windows
    return json.dumps({"type": "rate_limit_event", "rate_limit_info": info})


def test_no_runs_root_at_all_is_no_state(tmp_path):
    assert UW.read_five_hour_window(tmp_path / "does-not-exist") is None


def test_empty_runs_root_is_no_state(tmp_path):
    runs_root = tmp_path / "agent-runs"
    runs_root.mkdir()
    assert UW.read_five_hour_window(runs_root) is None


def test_a_run_with_no_rate_limit_event_is_no_state(tmp_path):
    runs_root = tmp_path / "agent-runs"
    _make_run(runs_root, "run-a", lines=['{"type": "assistant"}'])
    assert UW.read_five_hour_window(runs_root) is None


def test_reads_the_five_hour_window_out_of_unified_windows(tmp_path):
    runs_root = tmp_path / "agent-runs"
    line = _event(
        five_hour={"utilization": 0.62, "resetsAt": 1790676000},
        seven_day={"utilization": 0.22, "resetsAt": 1791111600},
        status="allowed_warning", is_using_overage=False,
    )
    _make_run(runs_root, "run-a", lines=[line])
    got = UW.read_five_hour_window(runs_root, now=1790676000 - 3600)
    assert got == {
        "utilization": 0.62,
        "resets_at": 1790676000,
        "status": "allowed_warning",
        "is_using_overage": False,
    }


def test_never_confuses_a_seven_day_reading_for_a_five_hour_one(tmp_path):
    """A top-level-only event (no `unifiedWindows`) whose `rateLimitType` is
    `seven_day` carries top-level `utilization`/`resetsAt` for the SEVEN-DAY
    window, not the five-hour one — reading it as five-hour would report a
    session's window as whatever the seven-day one happens to be. This
    reader only trusts `unifiedWindows.five_hour`, so a shape lacking it must
    be skipped, not misread."""
    runs_root = tmp_path / "agent-runs"
    line = _event(five_hour={"utilization": 0.99, "resetsAt": 1}, top_level_only=True)
    # `_event(top_level_only=True)` puts `rateLimitType`/`utilization`/`resetsAt`
    # at the top level, exactly the shape with no `unifiedWindows` at all.
    _make_run(runs_root, "run-a", lines=[line])
    assert UW.read_five_hour_window(runs_root) is None


def test_last_event_in_the_file_wins(tmp_path):
    runs_root = tmp_path / "agent-runs"
    lines = [
        _event(five_hour={"utilization": 0.10, "resetsAt": 1}),
        _event(five_hour={"utilization": 0.85, "resetsAt": 2}),
        _event(five_hour={"utilization": 0.40, "resetsAt": 3}),
    ]
    _make_run(runs_root, "run-a", lines=lines)
    got = UW.read_five_hour_window(runs_root, now=0)
    assert got["utilization"] == 0.40
    assert got["resets_at"] == 3


def test_freshest_run_by_transcript_mtime_wins(tmp_path):
    runs_root = tmp_path / "agent-runs"
    now = time.time()
    _make_run(runs_root, "run-old",
             lines=[_event(five_hour={"utilization": 0.10, "resetsAt": 1})],
             mtime=now - 3600)
    _make_run(runs_root, "run-new",
             lines=[_event(five_hour={"utilization": 0.90, "resetsAt": 2})],
             mtime=now)
    got = UW.read_five_hour_window(runs_root, now=0)
    assert got["utilization"] == 0.90


def test_a_run_with_no_reading_does_not_block_an_older_run_that_has_one(tmp_path):
    """The freshest run by mtime is scanned first; if IT has no matching
    event, the scan continues to the next-freshest rather than stopping."""
    runs_root = tmp_path / "agent-runs"
    now = time.time()
    _make_run(runs_root, "run-has-data",
             lines=[_event(five_hour={"utilization": 0.55, "resetsAt": 9})],
             mtime=now - 60)
    _make_run(runs_root, "run-no-data", lines=['{"type": "assistant"}'], mtime=now)
    got = UW.read_five_hour_window(runs_root, now=0)
    assert got["utilization"] == 0.55


def test_falls_back_to_yesterday_when_today_has_no_run_dirs(tmp_path):
    runs_root = tmp_path / "agent-runs"
    _make_run(runs_root, "run-y", day=_utc_yesterday(),
             lines=[_event(five_hour={"utilization": 0.77, "resetsAt": 5})])
    got = UW.read_five_hour_window(runs_root, now=0)
    assert got["utilization"] == 0.77


def test_does_not_fall_back_to_yesterday_when_today_has_run_dirs(tmp_path):
    """Today having run directories (even ones with no reading) is enough to
    skip the yesterday listing entirely — the common case pays for one
    directory listing, not two."""
    runs_root = tmp_path / "agent-runs"
    _make_run(runs_root, "run-y", day=_utc_yesterday(),
             lines=[_event(five_hour={"utilization": 0.77, "resetsAt": 5})])
    _make_run(runs_root, "run-t", lines=['{"type": "assistant"}'])
    assert UW.read_five_hour_window(runs_root) is None


def test_survives_a_transcript_truncated_mid_write(tmp_path):
    runs_root = tmp_path / "agent-runs"
    good = _event(five_hour={"utilization": 0.5, "resetsAt": 1})
    transcript = _make_run(runs_root, "run-a", lines=[good])
    with transcript.open("a") as fh:
        fh.write('{"type":"rate_limit_event","rate_limit_info":{"unifiedW')
    got = UW.read_five_hour_window(runs_root, now=0)
    assert got["utilization"] == 0.5


def test_a_non_run_directory_entry_is_ignored(tmp_path):
    """`sub-*`, `_diag` and similar siblings of `run-*` under a day directory
    must not be treated as a run — mirrors what `_live_runs` in
    `bin/co-session-health` already filters `active.json` on, applied here to
    directory names instead."""
    runs_root = tmp_path / "agent-runs"
    day_dir = runs_root / _utc_today()
    (day_dir / "sub-abc123").mkdir(parents=True)
    (day_dir / "sub-abc123" / "transcript.jsonl").write_text(
        _event(five_hour={"utilization": 0.99, "resetsAt": 1}) + "\n")
    assert UW.read_five_hour_window(runs_root) is None


def test_bounded_scan_stops_at_max_candidates(tmp_path, monkeypatch):
    """With the candidate cap lowered to 1, only the single freshest
    transcript is opened — an older one carrying a reading is never reached."""
    monkeypatch.setattr(UW, "MAX_CANDIDATES", 1)
    runs_root = tmp_path / "agent-runs"
    now = time.time()
    _make_run(runs_root, "run-has-data",
             lines=[_event(five_hour={"utilization": 0.55, "resetsAt": 9})],
             mtime=now - 60)
    _make_run(runs_root, "run-no-data", lines=['{"type": "assistant"}'], mtime=now)
    assert UW.read_five_hour_window(runs_root) is None


def test_is_using_overage_true_is_carried(tmp_path):
    runs_root = tmp_path / "agent-runs"
    line = _event(five_hour={"utilization": 0.30, "resetsAt": 1}, is_using_overage=True)
    _make_run(runs_root, "run-a", lines=[line])
    assert UW.read_five_hour_window(runs_root, now=0)["is_using_overage"] is True


# --- expiry: a reading past its own resetsAt counts as no reading -------------

def test_a_reading_past_its_resets_at_counts_as_no_reading(tmp_path):
    runs_root = tmp_path / "agent-runs"
    line = _event(five_hour={"utilization": 0.95, "resetsAt": 1_000_000})
    _make_run(runs_root, "run-a", lines=[line])
    assert UW.read_five_hour_window(runs_root, now=1_000_001) is None


def test_a_reading_exactly_at_its_resets_at_counts_as_no_reading(tmp_path):
    """`resets_at` at or before `now` is expired — the boundary itself, not
    only strictly past it."""
    runs_root = tmp_path / "agent-runs"
    line = _event(five_hour={"utilization": 0.95, "resetsAt": 1_000_000})
    _make_run(runs_root, "run-a", lines=[line])
    assert UW.read_five_hour_window(runs_root, now=1_000_000) is None


def test_a_reading_before_its_resets_at_is_still_returned(tmp_path):
    runs_root = tmp_path / "agent-runs"
    line = _event(five_hour={"utilization": 0.95, "resetsAt": 1_000_000})
    _make_run(runs_root, "run-a", lines=[line])
    got = UW.read_five_hour_window(runs_root, now=999_999)
    assert got["utilization"] == 0.95


def test_an_expired_reading_does_not_block_an_older_run_with_a_live_one(tmp_path):
    """The freshest run's reading has already reset; the scan falls through
    to the next-freshest run, exactly as it does for a run with no matching
    event at all."""
    runs_root = tmp_path / "agent-runs"
    now = time.time()
    _make_run(runs_root, "run-live",
             lines=[_event(five_hour={"utilization": 0.40, "resetsAt": 1_000_000})],
             mtime=now - 60)
    _make_run(runs_root, "run-expired",
             lines=[_event(five_hour={"utilization": 0.95, "resetsAt": 1})],
             mtime=now)
    got = UW.read_five_hour_window(runs_root, now=1_000_000 - 1)
    assert got["utilization"] == 0.40


def test_defaults_now_to_the_real_clock(tmp_path):
    """With no `now` given, a `resetsAt` far in the past is still treated as
    expired against the real wall clock."""
    runs_root = tmp_path / "agent-runs"
    line = _event(five_hour={"utilization": 0.95, "resetsAt": 1})
    _make_run(runs_root, "run-a", lines=[line])
    assert UW.read_five_hour_window(runs_root) is None


# --- non-finite and out-of-range numbers are skipped, like any malformed line --

def _raw_line(five_hour_json: str) -> str:
    """A `rate_limit_info` line whose `five_hour` object is *five_hour_json*
    verbatim — used instead of `_event()` so a caller can put a raw,
    non-JSON.dumps-able token (a bare `NaN`, an exponent that overflows) into
    the transcript exactly as a real one could carry it."""
    return json.dumps({
        "type": "rate_limit_event",
        "rate_limit_info": {
            "status": "allowed_warning",
            "isUsingOverage": False,
            "unifiedWindows": {"five_hour": "__PLACEHOLDER__"},
        },
    }).replace('"__PLACEHOLDER__"', five_hour_json)


def _older_valid_run(runs_root, now):
    _make_run(runs_root, "run-older-valid",
             lines=[_event(five_hour={"utilization": 0.42, "resetsAt": int(now) + 10_000})],
             mtime=now - 60)


def test_an_overflowing_resets_at_is_skipped_not_raised(tmp_path):
    """`1e400` is a valid JSON number token; Python's float parser overflows
    it to `inf`. Before the finiteness check this reached `int()` and raised
    `OverflowError`, breaking the reader's "never raises" contract."""
    runs_root = tmp_path / "agent-runs"
    now = time.time()
    _older_valid_run(runs_root, now)
    _make_run(runs_root, "run-bad",
             lines=[_raw_line('{"utilization": 0.95, "resetsAt": 1e400}')],
             mtime=now)
    got = UW.read_five_hour_window(runs_root, now=now)
    assert got["utilization"] == 0.42


def test_an_overflowing_utilization_is_skipped_not_raised(tmp_path):
    runs_root = tmp_path / "agent-runs"
    now = time.time()
    _older_valid_run(runs_root, now)
    _make_run(runs_root, "run-bad",
             lines=[_raw_line('{"utilization": 1e400, "resetsAt": %d}' % (int(now) + 10_000))],
             mtime=now)
    got = UW.read_five_hour_window(runs_root, now=now)
    assert got["utilization"] == 0.42


def test_a_nan_utilization_is_skipped(tmp_path):
    """A bare `NaN` token is valid input to Python's (non-strict) JSON
    parser, so this reaches the reader as a real `float('nan')`, not a
    `JSONDecodeError`."""
    runs_root = tmp_path / "agent-runs"
    now = time.time()
    _older_valid_run(runs_root, now)
    _make_run(runs_root, "run-bad",
             lines=[_raw_line('{"utilization": NaN, "resetsAt": %d}' % (int(now) + 10_000))],
             mtime=now)
    got = UW.read_five_hour_window(runs_root, now=now)
    assert got["utilization"] == 0.42


def test_a_negative_utilization_is_skipped(tmp_path):
    runs_root = tmp_path / "agent-runs"
    now = time.time()
    _older_valid_run(runs_root, now)
    _make_run(runs_root, "run-bad",
             lines=[_raw_line('{"utilization": -0.5, "resetsAt": %d}' % (int(now) + 10_000))],
             mtime=now)
    got = UW.read_five_hour_window(runs_root, now=now)
    assert got["utilization"] == 0.42


# --- resets_at bounded relative to `now`, not by an absolute ceiling ----------

def test_a_resets_at_50_years_out_is_skipped(tmp_path):
    """A finite `resetsAt` far beyond any real five-hour window is exactly
    the nonsensical date `_RESETS_AT_SLACK_SECONDS` exists to catch — an
    absolute ceiling far in the future (year 2286) would have let this
    through."""
    runs_root = tmp_path / "agent-runs"
    now = time.time()
    _older_valid_run(runs_root, now)
    line = _event(five_hour={"utilization": 0.95, "resetsAt": int(now) + 50 * 365 * 24 * 3600})
    _make_run(runs_root, "run-bad", lines=[line], mtime=now)
    got = UW.read_five_hour_window(runs_root, now=now)
    assert got["utilization"] == 0.42


def test_a_resets_at_of_1e15_is_skipped(tmp_path):
    runs_root = tmp_path / "agent-runs"
    now = time.time()
    _older_valid_run(runs_root, now)
    line = _event(five_hour={"utilization": 0.95, "resetsAt": 1e15})
    _make_run(runs_root, "run-bad", lines=[line], mtime=now)
    got = UW.read_five_hour_window(runs_root, now=now)
    assert got["utilization"] == 0.42


def test_a_resets_at_just_inside_the_plausible_bound_is_kept(tmp_path):
    runs_root = tmp_path / "agent-runs"
    now = time.time()
    bound = now + UW.FIVE_HOUR_WINDOW_SECONDS + UW._RESETS_AT_SLACK_SECONDS
    line = _event(five_hour={"utilization": 0.5, "resetsAt": int(bound)})
    _make_run(runs_root, "run-a", lines=[line])
    got = UW.read_five_hour_window(runs_root, now=now)
    assert got["resets_at"] == int(bound)


def test_a_resets_at_just_outside_the_plausible_bound_is_skipped(tmp_path):
    runs_root = tmp_path / "agent-runs"
    now = time.time()
    _older_valid_run(runs_root, now)
    bound = now + UW.FIVE_HOUR_WINDOW_SECONDS + UW._RESETS_AT_SLACK_SECONDS
    line = _event(five_hour={"utilization": 0.95, "resetsAt": int(bound) + 1})
    _make_run(runs_root, "run-bad", lines=[line], mtime=now)
    got = UW.read_five_hour_window(runs_root, now=now)
    assert got["utilization"] == 0.42


def test_a_zero_resets_at_is_skipped(tmp_path):
    runs_root = tmp_path / "agent-runs"
    now = time.time()
    _older_valid_run(runs_root, now)
    line = _event(five_hour={"utilization": 0.95, "resetsAt": 0})
    _make_run(runs_root, "run-bad", lines=[line], mtime=now)
    got = UW.read_five_hour_window(runs_root, now=now)
    assert got["utilization"] == 0.42


def test_a_negative_resets_at_is_skipped(tmp_path):
    runs_root = tmp_path / "agent-runs"
    now = time.time()
    _older_valid_run(runs_root, now)
    line = _event(five_hour={"utilization": 0.95, "resetsAt": -1})
    _make_run(runs_root, "run-bad", lines=[line], mtime=now)
    got = UW.read_five_hour_window(runs_root, now=now)
    assert got["utilization"] == 0.42


# --- a bare oversized integer literal (not a float overflow) is skipped too ---

_HUGE_INT = int("9" * 400)


def test_is_finite_number_returns_false_for_an_int_too_large_for_float():
    """`math.isfinite` itself raises `OverflowError` for a Python `int`
    outside `float` range. `read_five_hour_window`'s outer per-file
    `try/except` happens to catch that today, so a test that only calls
    `read_five_hour_window` can't tell this apart from a version of
    `_is_finite_number` that still raises — this calls it directly."""
    assert UW._is_finite_number(_HUGE_INT) is False


def test_a_400_digit_resets_at_is_skipped_not_raised(tmp_path):
    """A 400-digit JSON integer literal has no decimal point or exponent, so
    it never becomes `inf` at parse time the way `1e400` does — it reaches
    `_is_finite_number` as a genuine Python `int` outside `float` range,
    which `math.isfinite` itself cannot accept without raising."""
    runs_root = tmp_path / "agent-runs"
    now = time.time()
    _older_valid_run(runs_root, now)
    line = _event(five_hour={"utilization": 0.95, "resetsAt": _HUGE_INT})
    _make_run(runs_root, "run-bad", lines=[line], mtime=now)
    got = UW.read_five_hour_window(runs_root, now=now)
    assert got["utilization"] == 0.42


def test_a_400_digit_utilization_is_skipped_not_raised(tmp_path):
    runs_root = tmp_path / "agent-runs"
    now = time.time()
    _older_valid_run(runs_root, now)
    line = _event(five_hour={"utilization": _HUGE_INT, "resetsAt": int(now) + 10_000})
    _make_run(runs_root, "run-bad", lines=[line], mtime=now)
    got = UW.read_five_hour_window(runs_root, now=now)
    assert got["utilization"] == 0.42


def test_a_file_that_raises_while_reading_does_not_block_an_older_run_with_a_live_one(tmp_path):
    """The per-file guard in `read_five_hour_window` catches an exception
    from a single file's read (defense in depth beyond the value checks
    above) and keeps scanning the next-freshest candidate — the reader's
    "never raises" contract holds for the dispatch hold, a second caller of
    this same function, not only for this hook."""
    runs_root = tmp_path / "agent-runs"
    now = time.time()
    _older_valid_run(runs_root, now)
    _make_run(runs_root, "run-bad", lines=['{"type": "assistant"}'], mtime=now)

    real = UW._tail_five_hour_reading

    def _boom(path, now):
        if path.parent.name == "run-bad":
            raise RuntimeError("boom")
        return real(path, now)

    with mock.patch.object(UW, "_tail_five_hour_reading", side_effect=_boom):
        got = UW.read_five_hour_window(runs_root, now=now)
    assert got["utilization"] == 0.42


# --- the weekly (seven_day) window: the same reader, a seven-day bound ---------
#
# The two `rate_limit_info.unifiedWindows` objects below are copied verbatim
# (only the rate-limit fields, no transcript content) from run record
# `2026-10-04/run-613ba33b/transcript.jsonl`, line 180, on the machine that
# writes them. Every `seven_day` object in the 2026-10-04 run records has
# exactly the `utilization` and `resetsAt` keys this fixture carries.

_REAL_FIVE_HOUR = {"utilization": 0.81, "resetsAt": 1791173400}
_REAL_SEVEN_DAY = {"utilization": 0.73, "resetsAt": 1791223200}

_SEVEN_DAY_BOUND_SECONDS = UW.SEVEN_DAY_WINDOW_SECONDS + UW._RESETS_AT_SLACK_SECONDS


def _older_valid_weekly_run(runs_root, now):
    """An older run with a live five-hour AND seven-day reading, so a test
    that refuses the freshest run's seven-day reading can show the scan fell
    through to this one."""
    _make_run(runs_root, "run-older-valid",
              lines=[_event(five_hour={"utilization": 0.42, "resetsAt": int(now) + 10_000},
                            seven_day={"utilization": 0.31, "resetsAt": int(now) + 100_000})],
              mtime=now - 60)


def test_reads_the_seven_day_window_out_of_the_real_shape(tmp_path):
    runs_root = tmp_path / "agent-runs"
    line = _event(five_hour=_REAL_FIVE_HOUR, seven_day=_REAL_SEVEN_DAY,
                  status="allowed")
    _make_run(runs_root, "run-a", lines=[line])
    got = UW.read_seven_day_window(runs_root, now=_REAL_SEVEN_DAY["resetsAt"] - 3600)
    assert got == {
        "utilization": 0.73,
        "resets_at": 1791223200,
        "status": "allowed",
    }


def test_a_seven_day_reset_six_days_out_is_accepted(tmp_path):
    """A weekly window legitimately resets up to seven days ahead; six days
    ahead is well inside that bound."""
    runs_root = tmp_path / "agent-runs"
    now = 1_000_000
    six_days_out = now + 6 * 24 * 60 * 60
    _make_run(runs_root, "run-a",
              lines=[_event(seven_day={"utilization": 0.5, "resetsAt": six_days_out})])
    got = UW.read_seven_day_window(runs_root, now=now)
    assert got["resets_at"] == six_days_out


def test_a_seven_day_resets_at_beyond_seven_days_plus_slack_is_skipped(tmp_path):
    runs_root = tmp_path / "agent-runs"
    now = time.time()
    _older_valid_weekly_run(runs_root, now)
    line = _event(seven_day={"utilization": 0.95, "resetsAt": int(now) + _SEVEN_DAY_BOUND_SECONDS + 1})
    _make_run(runs_root, "run-bad", lines=[line], mtime=now)
    got = UW.read_seven_day_window(runs_root, now=now)
    assert got["utilization"] == 0.31


def test_a_seven_day_resets_at_exactly_at_the_plausible_bound_is_kept(tmp_path):
    runs_root = tmp_path / "agent-runs"
    now = time.time()
    bound = int(now + _SEVEN_DAY_BOUND_SECONDS)
    _make_run(runs_root, "run-a",
              lines=[_event(seven_day={"utilization": 0.5, "resetsAt": bound})])
    assert UW.read_seven_day_window(runs_root, now=now)["resets_at"] == bound


def test_a_five_hour_resets_at_six_days_out_is_still_refused(tmp_path):
    """The five-hour bound is unchanged: a six-day `resetsAt` is plausible for
    the weekly window and implausible for the five-hour one."""
    runs_root = tmp_path / "agent-runs"
    now = 1_000_000
    six_days_out = now + 6 * 24 * 60 * 60
    _make_run(runs_root, "run-a",
              lines=[_event(five_hour={"utilization": 0.5, "resetsAt": six_days_out})])
    assert UW.read_five_hour_window(runs_root, now=now) is None


def test_a_past_seven_day_resets_at_counts_as_no_reading(tmp_path):
    runs_root = tmp_path / "agent-runs"
    now = time.time()
    _older_valid_weekly_run(runs_root, now)
    _make_run(runs_root, "run-expired",
              lines=[_event(seven_day={"utilization": 0.95, "resetsAt": int(now) - 1})],
              mtime=now)
    got = UW.read_seven_day_window(runs_root, now=now)
    assert got["utilization"] == 0.31


def test_a_transcript_with_five_hour_but_no_seven_day_has_no_weekly_reading(tmp_path):
    runs_root = tmp_path / "agent-runs"
    _make_run(runs_root, "run-a",
              lines=[_event(five_hour={"utilization": 0.9, "resetsAt": 1_003_600})])
    assert UW.read_seven_day_window(runs_root, now=1_000_000) is None
    assert UW.read_five_hour_window(runs_root, now=1_000_000)["utilization"] == 0.9


def test_a_transcript_with_seven_day_but_no_five_hour_has_no_five_hour_reading(tmp_path):
    runs_root = tmp_path / "agent-runs"
    _make_run(runs_root, "run-a",
              lines=[_event(seven_day={"utilization": 0.8, "resetsAt": 1_003_600})])
    assert UW.read_five_hour_window(runs_root, now=1_000_000) is None
    assert UW.read_seven_day_window(runs_root, now=1_000_000)["utilization"] == 0.8


def test_a_seven_day_reading_drops_the_account_level_overage_flag(tmp_path):
    """`isUsingOverage` is account-level and the weekly reading must not hold on
    it, so the reader does not return it at all; the five-hour reader still
    carries it."""
    runs_root = tmp_path / "agent-runs"
    line = _event(five_hour={"utilization": 0.30, "resetsAt": 1_003_600},
                  seven_day={"utilization": 0.97, "resetsAt": 1_003_600}, is_using_overage=True)
    _make_run(runs_root, "run-a", lines=[line])
    weekly = UW.read_seven_day_window(runs_root, now=1_000_000)
    assert weekly["utilization"] == 0.97
    assert "is_using_overage" not in weekly
    assert UW.read_five_hour_window(runs_root, now=1_000_000)["is_using_overage"] is True


def test_no_runs_root_at_all_is_no_weekly_state(tmp_path):
    assert UW.read_seven_day_window(tmp_path / "does-not-exist") is None


def _raw_seven_day_line(seven_day_json: str) -> str:
    """A `rate_limit_info` line with the real five-hour object and a
    `seven_day` object that is *seven_day_json* verbatim, so a malformed
    weekly value reaches the reader exactly as a raw transcript token would."""
    return json.dumps({
        "type": "rate_limit_event",
        "rate_limit_info": {
            "status": "allowed",
            "isUsingOverage": False,
            "unifiedWindows": {"five_hour": _REAL_FIVE_HOUR, "seven_day": "__PLACEHOLDER__"},
        },
    }).replace('"__PLACEHOLDER__"', seven_day_json)


@pytest.mark.parametrize("seven_day_json", [
    '{"utilization": NaN, "resetsAt": __NOW__}',
    '{"utilization": -0.5, "resetsAt": __NOW__}',
    '{"utilization": 0.95, "resetsAt": 1e400}',
    '{"utilization": 1e400, "resetsAt": __NOW__}',
    '{"utilization": 0.95, "resetsAt": 0}',
    '{"utilization": 0.95, "resetsAt": true}',
    '{"utilization": 0.95}',
    '{"resetsAt": __NOW__}',
])
def test_a_malformed_seven_day_reading_is_skipped_like_a_missing_one(
        tmp_path, seven_day_json):
    """A bad weekly value is skipped the way a bad five-hour one is: the scan
    falls through to the older valid reading, and an absent weekly reading is
    no reading at all rather than a default."""
    runs_root = tmp_path / "agent-runs"
    now = time.time()
    _older_valid_weekly_run(runs_root, now)
    line = _raw_seven_day_line(seven_day_json.replace("__NOW__", str(int(now) + 10_000)))
    _make_run(runs_root, "run-bad", lines=[line], mtime=now)
    got = UW.read_seven_day_window(runs_root, now=now)
    assert got["utilization"] == 0.31


def test_a_seven_day_reader_that_raises_on_one_file_keeps_scanning(tmp_path):
    runs_root = tmp_path / "agent-runs"
    now = time.time()
    _older_valid_weekly_run(runs_root, now)
    _make_run(runs_root, "run-bad", lines=['{"type": "assistant"}'], mtime=now)

    real = UW._tail_seven_day_reading

    def _boom(path, now):
        if path.parent.name == "run-bad":
            raise RuntimeError("boom")
        return real(path, now)

    with mock.patch.object(UW, "_tail_seven_day_reading", side_effect=_boom):
        got = UW.read_seven_day_window(runs_root, now=now)
    assert got["utilization"] == 0.31


def test_the_seven_day_reader_takes_the_newest_reading_not_the_highest(tmp_path):
    """The freshest unexpired reading is the current one. An older 0.99 in
    the same window must not outrank the newer 0.10: the newest transcript
    is the one a later reading would land in."""
    runs_root = tmp_path / "agent-runs"
    now = time.time()
    _make_run(runs_root, "run-older",
              lines=[_event(seven_day={"utilization": 0.99, "resetsAt": int(now) + 100_000})],
              mtime=now - 60)
    _make_run(runs_root, "run-newer",
              lines=[_event(seven_day={"utilization": 0.10, "resetsAt": int(now) + 100_000})],
              mtime=now)
    got = UW.read_seven_day_window(runs_root, now=now)
    assert got["utilization"] == 0.10


def test_an_expired_newest_weekly_reading_is_skipped_for_the_live_older_one(tmp_path):
    """The newest transcript's reading has already reset, so the freshest live
    reading is the older 0.97 behind it."""
    runs_root = tmp_path / "agent-runs"
    now = time.time()
    _make_run(runs_root, "run-older",
              lines=[_event(seven_day={"utilization": 0.97, "resetsAt": int(now) + 100_000})],
              mtime=now - 60)
    _make_run(runs_root, "run-newest-expired",
              lines=[_event(seven_day={"utilization": 0.10, "resetsAt": int(now) - 1})],
              mtime=now)
    got = UW.read_seven_day_window(runs_root, now=now)
    assert got["utilization"] == 0.97
