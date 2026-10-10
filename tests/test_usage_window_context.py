"""tools/usage-window-context.py — every co session sees the five-hour usage
window (issue_01M3PZ6NBJ5XWD8F2G5EK5WB3C).

Drives the hook exactly as Claude Code does, the same posture
`test_rollover_trigger.py` uses for its sibling hook: a JSON payload on
stdin, a fresh process per firing, `HOME` redirected per test so the
throttle-state file never touches the real machine, and `BUILDER_ROOT`
redirected so `usage_window.read_five_hour_window` scans a scratch run-records
tree rather than this machine's real `agent-runs`.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "tools" / "usage-window-context.py"
# The hook imports both lazily (inside main(), after a sys.path.insert) rather
# than at module level, but they are still on the import path a 3.10+-only
# construct would crash on — test_the_pinned_files_parse_under_python_3_9_grammar
# checks them alongside the hook file itself.
_HOOK_LOCAL_IMPORTS = (
    REPO / "lib" / "agent-platform" / "usage_window.py",
    REPO / "lib" / "agent-platform" / "builder_root.py",
)

# Matches `usage-window-context.py`'s own `MST` — used only to compute the
# expected reset text from a *resets_at* this test file picks, never to
# duplicate the hook's band/threshold logic.
MST = timezone(timedelta(hours=-7))


def _utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _mst_string(resets_at: int) -> str:
    return datetime.fromtimestamp(resets_at, tz=timezone.utc).astimezone(MST).strftime("%H:%M MST")


def _future_resets_at(seconds_from_now: int = 3600) -> int:
    """A `resetsAt` guaranteed to be in the future of the real wall clock the
    hook reads (it takes no `now` override, unlike the reader's unit tests) —
    a fixed past timestamp would make every seeded reading here expire the
    moment real time passes it."""
    return int(time.time()) + seconds_from_now


def _seed_reading(tmp_path, *, utilization, resets_at=None, run_id="run-seed",
                  seven_day_utilization=0.10, seven_day_resets_at=None,
                  with_seven_day=True, is_using_overage=False):
    """One dispatched-run transcript reporting *utilization* for the
    five-hour window (and, unless `with_seven_day` is false, the weekly window
    at *seven_day_utilization*, default quiet), under the scratch
    `BUILDER_ROOT` `_run` points at. `is_using_overage` sets the account-level
    `isUsingOverage` flag on the line."""
    if resets_at is None:
        resets_at = _future_resets_at()
    if seven_day_resets_at is None:
        seven_day_resets_at = resets_at + 500_000
    day_dir = tmp_path / "data-root" / "agent-runs" / _utc_today()
    run_dir = day_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    windows = {"five_hour": {"utilization": utilization, "resetsAt": resets_at}}
    if with_seven_day:
        windows["seven_day"] = {"utilization": seven_day_utilization,
                                "resetsAt": seven_day_resets_at}
    line = json.dumps({
        "type": "rate_limit_event",
        "rate_limit_info": {
            "status": "allowed_warning",
            "isUsingOverage": is_using_overage,
            "unifiedWindows": windows,
        },
    })
    (run_dir / "transcript.jsonl").write_text(line + "\n")


# Runs the hook file with `time.time` pinned to argv[1], so a test can seed real
# timestamps that are in the past by the wall clock. The hook takes no `now`
# override, and every expiry check it makes goes through `time.time`.
_PINNED_CLOCK_RUNNER = (
    "import runpy, sys, time\n"
    "pinned = float(sys.argv[1])\n"
    "time.time = lambda: pinned\n"
    "sys.argv = sys.argv[2:]\n"
    "runpy.run_path(sys.argv[0], run_name='__main__')\n"
)


def _run(tmp_path, payload, *, tmux="/tmp/tmux-test,1,0", agentctl_run_id=None,
        hook=None, clock=None):
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    env["BUILDER_ROOT"] = str(tmp_path / "data-root")
    if tmux is None:
        env.pop("TMUX", None)
    else:
        env["TMUX"] = tmux
    if agentctl_run_id is None:
        env.pop("AGENTCTL_RUN_ID", None)
    else:
        env["AGENTCTL_RUN_ID"] = agentctl_run_id
    if clock is None:
        argv = [sys.executable, str(hook or HOOK)]
    else:
        argv = [sys.executable, "-c", _PINNED_CLOCK_RUNNER, str(clock), str(hook or HOOK)]
    proc = subprocess.run(argv, input=json.dumps(payload),
                          capture_output=True, text=True, env=env)
    return proc


def _payload(session_id, event, **extra):
    out = {"session_id": session_id, "hook_event_name": event}
    out.update(extra)
    return out


def _context(proc) -> str:
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["hookEventName"] in ("UserPromptSubmit", "PostToolUse")
    return out["hookSpecificOutput"]["additionalContext"]


# --- no state recorded --------------------------------------------------------

def test_no_state_recorded_emits_nothing(tmp_path):
    (tmp_path / "data-root" / "agent-runs").mkdir(parents=True)
    proc = _run(tmp_path, _payload("s1", "UserPromptSubmit"))
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_no_runs_root_at_all_emits_nothing(tmp_path):
    proc = _run(tmp_path, _payload("s1", "UserPromptSubmit"))
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


# --- the three bands -----------------------------------------------------------

def test_below_cutoff_is_silent(tmp_path):
    _seed_reading(tmp_path, utilization=0.31)
    proc = _run(tmp_path, _payload("s1", "UserPromptSubmit"))
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_below_hold_cutoff_shows_the_short_line(tmp_path):
    resets_at = _future_resets_at()
    _seed_reading(tmp_path, utilization=0.62, resets_at=resets_at)
    proc = _run(tmp_path, _payload("s1", "UserPromptSubmit"))
    assert proc.returncode == 0
    ctx = _context(proc)
    assert ctx == f"Usage: five-hour window 62%, resets {_mst_string(resets_at)}"


def test_at_or_above_hold_cutoff_shows_the_full_text_with_mst_reset(tmp_path):
    resets_at = _future_resets_at()
    _seed_reading(tmp_path, utilization=0.93, resets_at=resets_at)
    proc = _run(tmp_path, _payload("s1", "UserPromptSubmit"))
    assert proc.returncode == 0
    ctx = _context(proc)
    assert ctx == (
        f"Five-hour window at 93%, resets {_mst_string(resets_at)}. New dispatches will wait "
        "until then, and extra usage is off for autonomous work. Finish open "
        "work before starting new work."
    )


def test_exactly_at_hold_cutoff_is_the_hold_text_not_the_short_line(tmp_path):
    _seed_reading(tmp_path, utilization=0.90)
    proc = _run(tmp_path, _payload("s1", "UserPromptSubmit"))
    ctx = _context(proc)
    assert ctx.startswith("Five-hour window at 90%")


def test_exactly_at_short_cutoff_shows_the_short_line_not_silence(tmp_path):
    _seed_reading(tmp_path, utilization=0.50)
    proc = _run(tmp_path, _payload("s1", "UserPromptSubmit"))
    ctx = _context(proc)
    assert ctx.startswith("Usage: five-hour window 50%")


# --- the throttle, PostToolUse only ---------------------------------------------

def test_posttooluse_throttles_a_repeat_in_the_same_band(tmp_path):
    _seed_reading(tmp_path, utilization=0.62)
    first = _run(tmp_path, _payload("s1", "PostToolUse"))
    assert first.stdout.strip() != ""
    second = _run(tmp_path, _payload("s1", "PostToolUse"))
    assert second.returncode == 0
    assert second.stdout.strip() == ""


def test_userpromptsubmit_is_never_throttled(tmp_path):
    _seed_reading(tmp_path, utilization=0.62)
    first = _run(tmp_path, _payload("s1", "UserPromptSubmit"))
    second = _run(tmp_path, _payload("s1", "UserPromptSubmit"))
    assert first.stdout.strip() != ""
    assert second.stdout.strip() != ""
    assert _context(first) == _context(second)


def test_posttooluse_reinjects_once_the_throttle_window_has_elapsed(tmp_path):
    _seed_reading(tmp_path, utilization=0.62)
    first = _run(tmp_path, _payload("s1", "PostToolUse"))
    assert first.stdout.strip() != ""
    state_path = tmp_path / ".cache" / "co-session-health" / "usage-window-s1.json"
    state = json.loads(state_path.read_text())
    state["last_injected_at"] = time.time() - 20 * 60   # past THROTTLE_SECONDS (15m)
    state_path.write_text(json.dumps(state))
    second = _run(tmp_path, _payload("s1", "PostToolUse"))
    assert second.stdout.strip() != ""


# --- immediate re-inject on a band change ---------------------------------------

def test_posttooluse_reinjects_at_once_on_crossing_into_hold(tmp_path):
    _seed_reading(tmp_path, utilization=0.62)
    first = _run(tmp_path, _payload("s1", "PostToolUse"))
    assert first.stdout.strip() != ""
    # Same session, band now crosses into hold — re-seed the scratch runs
    # root with a fresh reading rather than mutating the first run's
    # transcript, so the freshest-mtime scan in `usage_window` picks it up.
    _seed_reading(tmp_path, utilization=0.95, run_id="run-seed-2")
    second = _run(tmp_path, _payload("s1", "PostToolUse"))
    assert second.returncode == 0
    ctx = _context(second)
    assert ctx.startswith("Five-hour window at 95%")


def test_posttooluse_does_not_reinject_on_a_change_that_stays_below_hold(tmp_path):
    """Only a crossing INTO `hold` bypasses the throttle — short-line
    utilization moving (0.55 -> 0.70, still `short`) does not."""
    _seed_reading(tmp_path, utilization=0.55)
    first = _run(tmp_path, _payload("s1", "PostToolUse"))
    assert first.stdout.strip() != ""
    _seed_reading(tmp_path, utilization=0.70, run_id="run-seed-2")
    second = _run(tmp_path, _payload("s1", "PostToolUse"))
    assert second.stdout.strip() == ""


# --- the three fast-exit skips (shared with tools/rollover-trigger.py) ---------

def test_a_dispatched_run_is_skipped(tmp_path):
    _seed_reading(tmp_path, utilization=0.95)
    proc = _run(tmp_path, _payload("s1", "UserPromptSubmit"), agentctl_run_id="run-x")
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_outside_tmux_is_skipped(tmp_path):
    _seed_reading(tmp_path, utilization=0.95)
    proc = _run(tmp_path, _payload("s1", "UserPromptSubmit"), tmux=None)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_a_subagents_own_tool_call_is_skipped(tmp_path):
    _seed_reading(tmp_path, utilization=0.95)
    proc = _run(tmp_path, _payload("s1", "PostToolUse", agent_id="a7ffafc777248e22e"))
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_always_exits_zero_on_unreadable_stdin(tmp_path):
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    env["BUILDER_ROOT"] = str(tmp_path / "data-root")
    env["TMUX"] = "/tmp/tmux-test,1,0"
    proc = subprocess.run([sys.executable, str(HOOK)], input="not json at all",
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


# --- expiry: a reading past its own resetsAt is silence, not the stale text ----

def test_a_reading_past_its_resets_at_emits_nothing(tmp_path):
    """A 95% reading whose `resetsAt` is already behind the real wall clock
    must not print the hold text — the exact case that kept the hold band
    displayed long after the window had actually reset."""
    _seed_reading(tmp_path, utilization=0.95, resets_at=int(time.time()) - 3600)
    proc = _run(tmp_path, _payload("s1", "UserPromptSubmit"))
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


# --- a failed import fails open ------------------------------------------------

def test_a_failing_import_yields_exit_0_and_empty_stdout(tmp_path):
    """A copy of the hook run from a location with no sibling
    `lib/agent-platform` (an empty dir stands in for a partial deploy or a
    stale symlink) fails its `import usage_window` — the SAFETY CONTRACT
    promises that fails open rather than crashing with a traceback."""
    _seed_reading(tmp_path, utilization=0.95)
    isolated_dir = tmp_path / "isolated"
    isolated_dir.mkdir()
    isolated_hook = isolated_dir / "usage-window-context.py"
    isolated_hook.write_text(HOOK.read_text())
    proc = _run(tmp_path, _payload("s1", "UserPromptSubmit"), hook=isolated_hook)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""
    assert proc.stderr.strip() == ""


# --- runs under the system interpreter the hooks config actually invokes ----

def test_imports_under_the_system_python3(tmp_path):
    """This hook is registered in the hooks config with no interpreter named
    (`platform/decisions/0090`'s Handoff), so Claude Code execs the file by
    its own shebang, `#!/usr/bin/python3` — macOS and Linux's fixed,
    root-owned system interpreter, 3.9 on the mini that actually runs these
    hooks. Run directly under that binary rather than `sys.executable` (this
    repo's newer venv interpreter, which every other test in this file
    uses): a 3.10+-only construct in this file, or in `usage_window` or
    `builder_root` (both imported lazily, inside `main()`), would pass every
    other test here and still crash at import under the interpreter actually
    used.

    This alone does not prove the property everywhere this suite runs:
    `/usr/bin/python3` is whatever version the HOST ships, which on CI's
    `convivy-linux` runner is 3.10+ (not verified on ci1), not 3.9 — see
    `test_the_pinned_files_parse_under_python_3_9_grammar` below for the
    check that holds regardless of which interpreter actually runs it."""
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    env["BUILDER_ROOT"] = str(tmp_path / "data-root")
    env.pop("TMUX", None)
    env.pop("AGENTCTL_RUN_ID", None)
    proc = subprocess.run(["/usr/bin/python3", str(HOOK)], input="{}",
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr


def test_the_pinned_files_parse_under_python_3_9_grammar():
    """The check above runs under whatever `/usr/bin/python3` is on the HOST,
    which is 3.9 only on the mini — CI's `convivy-linux` runner ships 3.10+ (not verified on ci1), so a
    reintroduced 3.10-only construct (a `match` statement, `except*`, ...)
    would pass every test in this file there and still crash on the mini.
    `ast.parse`'s `feature_version` gates the grammar the parser accepts, not
    which interpreter is running it, so this holds the same on every host —
    confirmed by planting a `match` statement here and watching it fail under
    both this repo's venv interpreter and a real 3.9 one.

    Checks the hook itself and `usage_window` and `builder_root`
    (`_HOOK_LOCAL_IMPORTS`), the modules it imports lazily — the same breadth
    the real-import test above has when it actually runs under 3.9.

    This does not catch every possible 3.10-only construct — a runtime-only
    difference such as a bare `int | None` evaluated outside a deferred
    annotation is semantics, not grammar, and `ast.parse` cannot see it — but
    it is the mechanism that actually fails in CI, which a fixed-interpreter
    subprocess call does not."""
    for path in (HOOK, *_HOOK_LOCAL_IMPORTS):
        ast.parse(path.read_text(), filename=str(path), feature_version=(3, 9))


# --- the weekly (seven_day) window: its own line, its own cutoff, its own reset -

def _weekly_hold_tail(weekly_reset: int) -> str:
    return (f" Every dispatch waits until {_weekday_string(weekly_reset)}, revise rounds "
            "and reviews included, and extra usage is off for autonomous work.")


def _weekday_string(resets_at: int) -> str:
    """The weekly reset as the hook formats it (`%a %H:%M MST`), computed here
    from *resets_at* rather than copied from the hook's own constant."""
    return datetime.fromtimestamp(resets_at, tz=timezone.utc).astimezone(MST).strftime("%a %H:%M MST")


def _six_days_out() -> int:
    return int(time.time()) + 6 * 24 * 3600


def test_weekly_alone_short_shows_its_own_line_with_weekday_reset(tmp_path):
    weekly_reset = _six_days_out()
    _seed_reading(tmp_path, utilization=0.31, seven_day_utilization=0.72,
                  seven_day_resets_at=weekly_reset)
    proc = _run(tmp_path, _payload("s1", "UserPromptSubmit"))
    assert _context(proc) == f"Usage: seven-day window 72%, resets {_weekday_string(weekly_reset)}"


def test_weekly_at_96_percent_is_a_short_line_not_a_hold(tmp_path):
    """The weekly hold cutoff is 0.99 (the operator, 2026-10-08), not the five-hour 0.9
    or its own former 0.96: 96% weekly is inside the short band."""
    weekly_reset = _six_days_out()
    _seed_reading(tmp_path, utilization=0.31, seven_day_utilization=0.96,
                  seven_day_resets_at=weekly_reset)
    ctx = _context(_run(tmp_path, _payload("s1", "UserPromptSubmit")))
    assert ctx == f"Usage: seven-day window 96%, resets {_weekday_string(weekly_reset)}"
    assert "New dispatches" not in ctx


def test_weekly_at_the_99_cutoff_is_the_hold_text_naming_the_weekly_window(tmp_path):
    weekly_reset = _six_days_out()
    _seed_reading(tmp_path, utilization=0.31, seven_day_utilization=0.99,
                  seven_day_resets_at=weekly_reset)
    ctx = _context(_run(tmp_path, _payload("s1", "UserPromptSubmit")))
    assert ctx == (
        f"Seven-day window at 99%, resets {_weekday_string(weekly_reset)}."
        + _weekly_hold_tail(weekly_reset)
    )


def test_both_windows_short_shows_both_lines_with_their_own_resets(tmp_path):
    five_reset = int(time.time()) + 3600
    weekly_reset = _six_days_out()
    _seed_reading(tmp_path, utilization=0.62, resets_at=five_reset,
                  seven_day_utilization=0.72, seven_day_resets_at=weekly_reset)
    ctx = _context(_run(tmp_path, _payload("s1", "UserPromptSubmit")))
    assert ctx == (
        f"Usage: five-hour window 62%, resets {_mst_string(five_reset)}; "
        f"seven-day window 72%, resets {_weekday_string(weekly_reset)}"
    )


@pytest.mark.parametrize("five_hour_later", [False, True])
def test_both_windows_held_names_the_weekly_wait_and_the_five_hour_reset_apart(
        tmp_path, five_hour_later):
    """Every dispatch waits on the weekly reset. A new dispatch waits on the
    later of the two held resets, whichever window that is: the weekly reset
    when it falls after the five-hour one, the five-hour reset when it falls
    after the weekly one (a weekly reset under five hours away). When the
    weekly reset is the later one, the every-dispatch sentence already names it,
    so no new-dispatch sentence follows."""
    if five_hour_later:
        weekly_reset = int(time.time()) + 1800
        five_reset = int(time.time()) + 3600
        new_dispatch = f" New dispatches will wait until {_mst_string(five_reset)}."
    else:
        five_reset = int(time.time()) + 3600
        weekly_reset = _six_days_out()
        new_dispatch = ""
    _seed_reading(tmp_path, utilization=0.93, resets_at=five_reset,
                  seven_day_utilization=0.99, seven_day_resets_at=weekly_reset)
    ctx = _context(_run(tmp_path, _payload("s1", "UserPromptSubmit")))
    assert ctx == (
        f"Five-hour window at 93%, resets {_mst_string(five_reset)}; "
        f"seven-day window at 99%, resets {_weekday_string(weekly_reset)}."
        + _weekly_hold_tail(weekly_reset)
        + new_dispatch
    )


def test_five_hour_held_and_weekly_short_waits_for_the_five_hour_reset(tmp_path):
    """Only the five-hour window holds, so the wait ends at its reset, and the
    weekly line is still shown with its own reset."""
    five_reset = int(time.time()) + 3600
    weekly_reset = _six_days_out()
    _seed_reading(tmp_path, utilization=0.93, resets_at=five_reset,
                  seven_day_utilization=0.72, seven_day_resets_at=weekly_reset)
    ctx = _context(_run(tmp_path, _payload("s1", "UserPromptSubmit")))
    assert ctx == (
        f"Five-hour window at 93%, resets {_mst_string(five_reset)}; "
        f"seven-day window at 72%, resets {_weekday_string(weekly_reset)}."
        f" New dispatches will wait until {_mst_string(five_reset)}, and extra "
        "usage is off for autonomous work. Finish open work before starting new work."
    )


def test_a_weekly_reading_past_its_reset_emits_no_weekly_line(tmp_path):
    _seed_reading(tmp_path, utilization=0.31, seven_day_utilization=0.99,
                  seven_day_resets_at=int(time.time()) - 3600)
    proc = _run(tmp_path, _payload("s1", "UserPromptSubmit"))
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_a_transcript_with_five_hour_but_no_seven_day_shows_only_the_five_hour_line(tmp_path):
    resets_at = _future_resets_at()
    _seed_reading(tmp_path, utilization=0.62, resets_at=resets_at, with_seven_day=False)
    ctx = _context(_run(tmp_path, _payload("s1", "UserPromptSubmit")))
    assert ctx == f"Usage: five-hour window 62%, resets {_mst_string(resets_at)}"


def test_a_weekly_crossing_into_hold_reinjects_at_once(tmp_path):
    """The throttle bypass on a band crossing applies to the weekly window too:
    the overall band moves from short to hold when the weekly cutoff is
    reached, and that is re-injected without waiting out the throttle."""
    weekly_reset = _six_days_out()
    _seed_reading(tmp_path, utilization=0.62, seven_day_utilization=0.72,
                  seven_day_resets_at=weekly_reset)
    first = _run(tmp_path, _payload("s1", "PostToolUse"))
    assert first.stdout.strip() != ""
    _seed_reading(tmp_path, utilization=0.62, seven_day_utilization=0.99,
                  seven_day_resets_at=weekly_reset, run_id="run-seed-2")
    second = _run(tmp_path, _payload("s1", "PostToolUse"))
    assert second.returncode == 0
    assert "seven-day window at 99%" in _context(second)


# --- five-hour overage: holds at low utilization, and names the later reset ----

# The real case (run-266c1b55 and four other runs under agent-runs, the
# rate_limit_info lines): seven-day at 1.0 resetting 1790013600, five-hour at
# 0.03 on overage resetting 1790018400. The timestamps are real; the clock is
# pinned to just before them, because they are long past by the wall clock.
REAL_PINNED_NOW = 1790000000
REAL_WEEKLY_RESET = 1790013600
REAL_FIVE_HOUR_RESET = 1790018400


def test_real_overage_case_names_the_later_five_hour_reset(tmp_path):
    """Round 7: the hook showed only the weekly sentence, which said every
    dispatch waits until the weekly reset. `_rate_limit_hold_decision` holds
    every dispatch, `--pr` runs included, until the five-hour reset."""
    _seed_reading(tmp_path, utilization=0.03, resets_at=REAL_FIVE_HOUR_RESET,
                  is_using_overage=True, seven_day_utilization=1,
                  seven_day_resets_at=REAL_WEEKLY_RESET)
    proc = _run(tmp_path, _payload("s1", "UserPromptSubmit"), clock=REAL_PINNED_NOW)
    assert proc.returncode == 0
    assert proc.stdout.strip(), proc.stderr
    ctx = _context(proc)
    five = _mst_string(REAL_FIVE_HOUR_RESET)
    assert ctx == (
        f"Five-hour window at 3%, resets {five}; "
        f"seven-day window at 100%, resets {_weekday_string(REAL_WEEKLY_RESET)}."
        f" Every dispatch waits until {five}, revise rounds and reviews included,"
        " and extra usage is off for autonomous work."
    )


def test_five_hour_overage_with_no_weekly_hold_says_every_dispatch_waits(tmp_path):
    """Round 7: on origin/main a five-hour reading below 0.5 prints nothing, so
    an overage hold at 3% was silent. It now holds the text, with revise rounds
    included, because `--pr` runs wait on overage too."""
    _seed_reading(tmp_path, utilization=0.03, resets_at=REAL_FIVE_HOUR_RESET,
                  is_using_overage=True)
    proc = _run(tmp_path, _payload("s1", "UserPromptSubmit"), clock=REAL_PINNED_NOW)
    assert proc.returncode == 0
    assert proc.stdout.strip(), proc.stderr
    five = _mst_string(REAL_FIVE_HOUR_RESET)
    assert _context(proc) == (
        f"Five-hour window at 3%, resets {five}. Every dispatch waits until {five}, "
        "revise rounds and reviews included, and extra usage is off for autonomous work."
    )


def test_five_hour_low_utilization_without_overage_stays_silent(tmp_path):
    """Overage at 0.03 is the hold; the same 0.03 with no overage flag stays
    silent, so the new band reads the flag and not the utilization."""
    _seed_reading(tmp_path, utilization=0.03, resets_at=REAL_FIVE_HOUR_RESET,
                  is_using_overage=False)
    proc = _run(tmp_path, _payload("s1", "UserPromptSubmit"), clock=REAL_PINNED_NOW)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_hook_names_the_release_time_the_hold_decision_uses(tmp_path, agentctl):
    """The hook does not import `bin/agentctl` (a 15k-line CLI, too heavy for a
    per-prompt hook), so it restates the overage rule. This holds the two to
    the same time: the reset `_rate_limit_hold_decision` holds a `--pr` run to
    must be the one the hook names, for the real case's readings."""
    now = datetime.fromtimestamp(REAL_PINNED_NOW, tz=timezone.utc)
    five_hour = {"utilization": 0.03, "resets_at": REAL_FIVE_HOUR_RESET,
                 "status": None, "is_using_overage": True}
    seven_day = {"utilization": 1.0, "resets_at": REAL_WEEKLY_RESET, "status": None}
    held = agentctl._rate_limit_hold_decision(five_hour, now=now, pr_round_fix=True,
                                              seven_day=seven_day)
    assert held is not None
    held_until = int(held[0].timestamp())
    _seed_reading(tmp_path, utilization=0.03, resets_at=REAL_FIVE_HOUR_RESET,
                  is_using_overage=True, seven_day_utilization=1,
                  seven_day_resets_at=REAL_WEEKLY_RESET)
    proc = _run(tmp_path, _payload("s1", "UserPromptSubmit"), clock=REAL_PINNED_NOW)
    assert proc.stdout.strip(), proc.stderr
    assert f"Every dispatch waits until {_mst_string(held_until)}" in _context(proc)


# --- the hook's restated cutoffs and held set, against the hold decision -------
#
# The hook cannot import bin/agentctl, so it restates both hold cutoffs and the
# held set. These tests pin each restatement to the decision it mirrors: the
# constants by equality, and every window state, in both orders of the two
# resets, by comparing the hook's text with `_rate_limit_hold_decision` for a
# `--pr` run and for a fresh one.

@pytest.fixture(scope="module")
def hook_module():
    """`tools/usage-window-context.py` loaded by path. The file name has a dash,
    so it is not importable by name."""
    spec = importlib.util.spec_from_file_location("usage_window_context", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hook_cutoffs_are_the_agentctl_hold_constants(hook_module, agentctl):
    """Each hook cutoff equals the constant its hold decision reads, so moving one
    side alone fails here before any text does."""
    assert hook_module.HOLD_CUTOFF == agentctl.RATE_LIMIT_HOLD_UTILIZATION
    assert hook_module.WEEKLY_HOLD_CUTOFF == agentctl.WEEKLY_HOLD_UTILIZATION


# Window states the hook bands, as `usage_window` returns them. The five-hour
# state is (utilization, is_using_overage); the seven-day state is utilization.
# "between-cutoffs" is 0.93: above the five-hour cutoff, below the weekly one.
FIVE_HOUR_STATES = {
    "absent": None,
    "quiet": (0.10, False),
    "short": (0.62, False),
    "at-cutoff": (0.90, False),
    "overage": (0.03, True),
}
SEVEN_DAY_STATES = {
    "absent": None,
    "quiet": 0.10,
    "short": 0.72,
    "between-cutoffs": 0.93,
    "at-cutoff": 0.99,
    "full": 1.0,
}
# (five-hour reset, weekly reset). The real case is "five-hour-later"; the other
# order puts the weekly reset after the five-hour one.
RESET_ORDERS = {
    "five-hour-later": (REAL_FIVE_HOUR_RESET, REAL_WEEKLY_RESET),
    "weekly-later": (REAL_WEEKLY_RESET, REAL_FIVE_HOUR_RESET),
}
MATRIX_NOW = datetime.fromtimestamp(REAL_PINNED_NOW, tz=timezone.utc)


def _hook_clock(instant: datetime, five_reset: int, weekly_reset: int) -> str:
    """`instant` as the hook prints a reset. The two resets never coincide in the
    matrix, so the instant alone identifies its window and its format."""
    stamp = int(instant.timestamp())
    if stamp == weekly_reset:
        return _weekday_string(stamp)
    assert stamp == five_reset, "the hold decision returned a time neither window resets at"
    return _mst_string(stamp)


@pytest.mark.parametrize("order", sorted(RESET_ORDERS))
@pytest.mark.parametrize("seven_day", sorted(SEVEN_DAY_STATES))
@pytest.mark.parametrize("five_hour", sorted(FIVE_HOUR_STATES))
def test_hook_and_hold_decision_agree_on_every_window_state(
        hook_module, agentctl, five_hour, seven_day, order):
    """For each combination: the hook shows a hold exactly when the decision
    holds a fresh run; it says every dispatch waits exactly when the decision
    holds a `--pr` run; and each time it prints is the time the decision returns."""
    five_reset, weekly_reset = RESET_ORDERS[order]
    five = None
    if FIVE_HOUR_STATES[five_hour] is not None:
        utilization, overage = FIVE_HOUR_STATES[five_hour]
        five = {"utilization": utilization, "resets_at": five_reset,
                "status": None, "is_using_overage": overage}
    seven = None
    if SEVEN_DAY_STATES[seven_day] is not None:
        seven = {"utilization": SEVEN_DAY_STATES[seven_day],
                 "resets_at": weekly_reset, "status": None}
    band, text = hook_module._compose([
        ("five-hour", five, hook_module.HOLD_CUTOFF, hook_module.FIVE_HOUR_RESET_FMT),
        ("seven-day", seven, hook_module.WEEKLY_HOLD_CUTOFF, hook_module.SEVEN_DAY_RESET_FMT),
    ])
    fresh = agentctl._rate_limit_hold_decision(five, now=MATRIX_NOW, pr_round_fix=False,
                                               seven_day=seven)
    revise = agentctl._rate_limit_hold_decision(five, now=MATRIX_NOW, pr_round_fix=True,
                                                seven_day=seven)

    assert (band == "hold") == (fresh is not None)
    revise_match = re.search(r"Every dispatch waits until ([^,]+), revise rounds", text)
    assert (revise_match is not None) == (revise is not None)
    if revise is not None:
        assert revise_match.group(1) == _hook_clock(revise[0], five_reset, weekly_reset)
    if fresh is not None:
        fresh_match = re.search(r"New dispatches will wait until ([^,.]+)[,.]", text)
        named = (fresh_match or revise_match).group(1)
        if named == "then":
            # One window is shown, and "then" names its reset: the five-hour one,
            # the only window that holds a fresh run without holding a `--pr` run.
            assert _hook_clock(fresh[0], five_reset, weekly_reset) == _mst_string(five_reset)
        else:
            assert named == _hook_clock(fresh[0], five_reset, weekly_reset)


def test_weekly_reset_later_than_overage_reset_names_the_weekly_reset(tmp_path):
    """The overage ends first and the weekly reset is later. Every dispatch waits
    for the later reset, the weekly one, and the text names it once: a new
    dispatch waits for the same instant, so no second sentence follows."""
    _seed_reading(tmp_path, utilization=0.03, resets_at=REAL_WEEKLY_RESET,
                  is_using_overage=True, seven_day_utilization=1,
                  seven_day_resets_at=REAL_FIVE_HOUR_RESET)
    proc = _run(tmp_path, _payload("s1", "UserPromptSubmit"), clock=REAL_PINNED_NOW)
    assert proc.returncode == 0
    assert proc.stdout.strip(), proc.stderr
    weekly = _weekday_string(REAL_FIVE_HOUR_RESET)
    assert _context(proc) == (
        f"Five-hour window at 3%, resets {_mst_string(REAL_WEEKLY_RESET)}; "
        f"seven-day window at 100%, resets {weekly}."
        f" Every dispatch waits until {weekly}, revise rounds and reviews included,"
        " and extra usage is off for autonomous work."
    )
