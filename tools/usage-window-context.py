#!/usr/bin/python3
"""usage-window-context — every co session sees the five-hour and weekly usage windows.

Fires on `UserPromptSubmit` and a throttled `PostToolUse` (issue_
01M3PZ6NBJ5XWD8F2G5EK5WB3C, the operator 2026-09-29, relayed by the steward: "no to
extra usage on autonomous work"). On 2026-09-29 co sessions dispatched new
work overnight into an exhausted five-hour window because nothing in their
context said it was nearly spent; the sibling change
(issue_01M3PZ6J0D55EFC8GGAYMN9SH4) holds `agentctl spawn` itself at the
limit, and this one lets a session read that coming before it hits it, so it
can finish open work before starting new work.

Reads `usage_window.read_five_hour_window` and `read_seven_day_window`, the
readers issue_01M3PZ6NBJ5XWD8F2G5EK5WB3C and the dispatch hold share (see
that module's docstring for why it scans run records rather than the
session's own transcript, which never carries this signal).

THREE BANDS FOR THE WEEKLY WINDOW, keyed on its `utilization`:
  * below `SHORT_CUTOFF` (0.5) — nothing. The first half of a window is not
    actionable; the issue's own example marks 50% as the silent floor, so
    the visible short line starts exactly where the silent range the issue
    described ends, rather than at a second, unexplained number.
  * `SHORT_CUTOFF` to `WEEKLY_HOLD_CUTOFF` — one short line.
  * at or above `WEEKLY_HOLD_CUTOFF` (0.99, the operator, 2026-10-08, the same
    number `bin/agentctl` holds on) — every dispatch waits until the weekly
    reset, revise rounds and reviews included.

THE FIVE-HOUR WINDOW HAS NO UTILIZATION CUTOFF: the five-hour limit the
operator's plan used to carry is gone (issue_01M6ABPVXQM5HVWR29JQYZKWMP), and
`bin/agentctl` no longer holds a dispatch on it. Its utilization still gets a
`SHORT_CUTOFF`-and-above line, same as before, just never a hold one — the
line stays a "short" one at any utilization below 1.0 and above. The
five-hour window holds only when its reading carries `isUsingOverage`, at any
utilization, the same severity as the weekly hold: every dispatch waits,
revise rounds and reviews included, until that window's own reset.

Each window that is above silence gets its own line, with its reset time; a
weekly reset is shown with its weekday, since a seven-day window's reset is
not a time of day that identifies it.

THROTTLED ON PostToolUse ONLY. `UserPromptSubmit` fires once per human turn
and always shows the current line (when there is one) — that is the moment
a session is about to decide what to do next. `PostToolUse` fires on every
tool call, so it re-injects at most once per `THROTTLE_SECONDS`, except a
crossing INTO the hold band (weekly utilization reaching `WEEKLY_HOLD_CUTOFF`,
or a window starting to carry `isUsingOverage`) still fires at once: that
crossing is the one event this hook exists to make sure a session sees
promptly, not on the next throttle tick up to `THROTTLE_SECONDS` later.
Throttle state is kept per session under the co-session-health cache dir,
the way `rollover_policy`'s own per-session state is (see its module
docstring) — `last_band` is updated on every firing so a later crossing is
always compared against the true last-seen band, and `last_injected_at`
only on a firing that actually emitted text, so the throttle clock measures
time since the session last SAW the line, not time since it was last
checked.

ACCOUNT RESOLUTION IS CACHED PER SESSION ON PostToolUse. `UW.live_account()`
runs `claude-sub current` and `claude-sub list`, each its own subprocess with
its own timeout. `UserPromptSubmit` fires once per human turn, so it resolves
the account fresh every time — that is the moment a session is about to act
on it. `PostToolUse` fires on every tool call, and the throttle above then
prints nothing for most of them, so resolving fresh on every one would spend
that subprocess pair on calls that emit no text. A `PostToolUse` firing
instead reuses the session's last-resolved account — the digest only, never
the email — when it was resolved less than `ACCOUNT_CACHE_SECONDS` ago, and
resolves fresh otherwise; an unknown account (`None`) is cached and reused
the same way. The readings themselves are still read fresh on every
`PostToolUse` (a crossing into `hold` must still fire at once); only the
account lookup behind them is cached.

SAFETY CONTRACT, same as `tools/rollover-trigger.py`:
  1. ALWAYS exit 0.
  2. Fail open on anything unreadable: no window state recorded, a missing
     or corrupt throttle-state file, an import that fails — every one of
     those is a reason to say nothing, never a reason to block or crash.
  3. Skip a session this hook must not act on, before touching any state:
     a dispatched worker (`AGENTCTL_RUN_ID` set), a session outside tmux,
     and a subagent's own tool call reaching this hook through the parent
     session's PostToolUse stream (`agent_id` present) — the same three
     skips `tools/rollover-trigger.py` applies, for the same reasons (see
     its own docstring).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Matches `rollover_policy.CACHE_DIR` / `session_state.VITALS_DIR` — the same
# cache directory co-session-health already writes vitals and rollover state
# into.
CACHE_DIR = Path.home() / ".cache" / "co-session-health"

SHORT_CUTOFF = 0.5
# The weekly hold cutoff: `bin/agentctl`'s WEEKLY_HOLD_UTILIZATION (the operator,
# 2026-10-04: "Let's make it 96% instead of 90%"; raised to 99% on 2026-10-08,
# "99% is the right number for the weekly usage threshold"). Its own constant, so the
# two windows' thresholds move independently.
WEEKLY_HOLD_CUTOFF = 0.99

# At most one re-injection per PostToolUse-driven firing every 15 minutes,
# outside a band crossing. The windows move slowly (five hours and seven days
# wide) and the only threshold with a consequence is a hold cutoff, which
# bypasses this and fires at once — so 15 minutes trades a little staleness in
# the short line for not repeating it on every tool call.
THROTTLE_SECONDS = 15 * 60

# How long a `PostToolUse` firing may reuse the session's last-resolved
# account before resolving it again — see ACCOUNT RESOLUTION above.
ACCOUNT_CACHE_SECONDS = 60

MST = timezone(timedelta(hours=-7))

_SESSION_ID_RE = re.compile(r"[A-Za-z0-9_-]+")

FIVE_HOUR_RESET_FMT = "%H:%M MST"
SEVEN_DAY_RESET_FMT = "%a %H:%M MST"


def _read_payload() -> dict:
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    try:
        payload = json.loads(raw)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _session_key(session_id: str) -> str:
    return "".join(_SESSION_ID_RE.findall(session_id))[:64]


def _state_path(session_id: str) -> Path | None:
    key = _session_key(session_id)
    if not key:
        return None
    return CACHE_DIR / f"usage-window-{key}.json"


def _read_state(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(path: Path, state: dict) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(state))
        os.replace(tmp, path)
    except OSError:
        pass


def _band(utilization: float, hold_cutoff: float | None) -> str:
    """`hold_cutoff=None` for a window with no utilization hold at all (the
    five-hour window): utilization alone then never reaches `hold`, no
    matter how high — the caller still raises it to `hold` separately on
    `isUsingOverage`."""
    if hold_cutoff is not None and utilization >= hold_cutoff:
        return "hold"
    if utilization >= SHORT_CUTOFF:
        return "short"
    return "quiet"


def _fmt_reset(resets_at: int, reset_fmt: str) -> str:
    dt = datetime.fromtimestamp(resets_at, tz=timezone.utc).astimezone(MST)
    return dt.strftime(reset_fmt)


def _compose(windows: list) -> tuple[str, str]:
    """`(band, text)` for the windows above silence, where *windows* holds one
    `(label, reading, hold_cutoff, reset_fmt)` per window and `reading` is
    `None` when that window has none.

    The overall band is `hold` if any window is held, else `short` if any is
    above `SHORT_CUTOFF`, else `quiet` with no text. The text has one phrase
    per window above silence, with that window's reset. While nothing is held
    it reads "Usage: ..."; once something is, it adds the sentence about the
    dispatch hold. Every window that reaches `hold` here — the weekly window
    at its threshold, or the five-hour window on overage — holds every
    dispatch, revise rounds and reviews included, until its own reset
    (`_rate_limit_hold_decision`); there is no other way either window
    reaches `hold`, so the sentence always says "every dispatch waits"."""
    shown = []
    for label, reading, hold_cutoff, reset_fmt in windows:
        if reading is None:
            continue
        band = _band(reading["utilization"], hold_cutoff)
        # Overage holds at any utilization, as in `_rate_limit_hold_decision`.
        # Only the five-hour reading carries the flag (the seven-day reader drops it).
        if reading.get("is_using_overage"):
            band = "hold"
        if band == "quiet":
            continue
        pct = round(reading["utilization"] * 100)
        reset = _fmt_reset(reading["resets_at"], reset_fmt)
        shown.append((band, label, pct, reset, reading, reset_fmt))
    if not shown:
        return "quiet", ""
    if all(band == "short" for band, *_ in shown):
        phrases = [f"{label} window {pct}%, resets {reset}" for _, label, pct, reset, _, _ in shown]
        return "short", "Usage: " + "; ".join(phrases)
    phrases = [f"{label} window at {pct}%, resets {reset}" for _, label, pct, reset, _, _ in shown]
    head = "; ".join(phrases)
    head = head[0].upper() + head[1:]
    held = [entry for entry in shown if entry[0] == "hold"]
    return "hold", _every_dispatch_text(head, held)


def _every_dispatch_text(head: str, held: list) -> str:
    """The hold sentence: every dispatch waits until the later reset of the
    held windows, revise rounds and reviews included."""
    until = max(held, key=lambda entry: entry[4]["resets_at"])
    return (f"{head}. Every dispatch waits until "
            f"{_fmt_reset(until[4]['resets_at'], until[5])}, revise rounds "
            "and reviews included, and extra usage is off for autonomous work.")


def _cached_account(state: dict, now: float) -> tuple[bool, str | None]:
    """`(hit, account)` — *hit* is true when *state* holds an account digest
    resolved less than `ACCOUNT_CACHE_SECONDS` ago, in which case *account* is
    that cached value (an unknown account caches and reuses as `None` the
    same way as a known digest). A missing, stale or malformed cache entry —
    including the corrupt-state-file case, which `_read_state` already turns
    into `{}` — is a miss, not an error: the caller resolves fresh."""
    resolved_at = state.get("account_resolved_at")
    if not isinstance(resolved_at, (int, float)):
        return False, None
    if now - resolved_at >= ACCOUNT_CACHE_SECONDS:
        return False, None
    account = state.get("account")
    if account is not None and not isinstance(account, str):
        return False, None
    return True, account


def _should_emit(event: str, text: str, band: str, state: dict, now: float) -> bool:
    if not text:
        return False
    if event == "UserPromptSubmit":
        return True
    # PostToolUse: throttled, except a fresh crossing into `hold`.
    last_band = state.get("last_band")
    if last_band is None:
        return True
    if band == "hold" and last_band != "hold":
        return True
    last_at = state.get("last_injected_at")
    if not isinstance(last_at, (int, float)):
        return True
    return (now - last_at) >= THROTTLE_SECONDS


def _emit_context(event_name: str, text: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {"hookEventName": event_name, "additionalContext": text},
    }))


def main() -> int:
    # Inside the guarded region (never at module level): the SAFETY CONTRACT
    # promises a failed import fails open, and only a `try` around `main()`
    # itself can deliver that — an import that raises at module load happens
    # before that `try` is ever reached.
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                     "..", "lib", "agent-platform"))
    import builder_root as BR
    import usage_window as UW

    payload = _read_payload()
    event = payload.get("hook_event_name")
    if not isinstance(event, str):
        event = sys.argv[1] if len(sys.argv) > 1 else ""
    if event not in ("UserPromptSubmit", "PostToolUse"):
        return 0

    if os.environ.get("AGENTCTL_RUN_ID"):
        return 0
    if not os.environ.get("TMUX"):
        return 0
    if payload.get("agent_id"):
        return 0

    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return 0

    state_path = _state_path(session_id)
    state = _read_state(state_path) if state_path else {}
    now = time.time()

    if event == "PostToolUse":
        hit, account = _cached_account(state, now)
    else:
        hit, account = False, None
    if hit:
        account_resolved_at = state.get("account_resolved_at")
    else:
        account = UW.live_account()
        account_resolved_at = now

    new_state = dict(state)
    new_state["account"] = account
    new_state["account_resolved_at"] = account_resolved_at

    runs_root = BR.runs_root()
    five_hour = UW.read_five_hour_window(runs_root, account=account)
    seven_day = UW.read_seven_day_window(runs_root, account=account)
    if five_hour is None and seven_day is None:
        if state_path:
            _write_state(state_path, new_state)
        return 0

    band, text = _compose([
        ("five-hour", five_hour, None, FIVE_HOUR_RESET_FMT),
        ("seven-day", seven_day, WEEKLY_HOLD_CUTOFF, SEVEN_DAY_RESET_FMT),
    ])

    emit = _should_emit(event, text, band, state, now)
    new_state["last_band"] = band
    if emit:
        new_state["last_injected_at"] = now

    if state_path:
        _write_state(state_path, new_state)

    if emit:
        _emit_context(event, text)
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except BaseException:
        rc = 0
    sys.exit(rc or 0)
