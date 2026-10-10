"""limit_resume — wake each co session that stopped on a usage limit, once,
after the limit resets (issue_01M3PXMTR0PFF6QH46F8SN76CZ).

THE INCIDENT THIS ANSWERS. On 2026-09-29 at 02:31 MST the monthly spend limit
refused every call. The harness injected a `usageLimitNote: "wrap_up"` message
into each co's transcript and every co ended its turn there; every dispatched
run died the same way, an `isApiErrorMessage` record with `error: "rate_limit"`
as the transcript's last line. The window reset at 03:00, and nothing woke the
idle sessions back up — six crews sat for about five hours until the operator typed.
This module is the fix: a periodic, external process (see `bin/limit-resume`)
that notices the reset and sends each stopped session one resume prompt.

WHY EXTERNAL AND NOT A STATUS-LINE MODE. `co-session-health` only runs when
the TUI renders a turn's status line — the exact failure mode here is a
session taking NO turns at all, so a per-turn hook can never fire for it. Only
something scheduled from outside the session (launchd) can notice a session
that has gone quiet.

TWO DIFFERENT "WE HIT THE LIMIT" SHAPES, and this module reads both, from two
different places:

  * A DISPATCHED run's transcript carries a `rate_limit_event` with a
    `rate_limit_info` object nesting `unifiedWindows.five_hour.{utilization,
    resetsAt}` — the CLI's own quota telemetry, present on ordinary turns, not
    only failures. `latest_five_hour_window` reads this, across every run this
    machine has recorded, to answer "when does the current window reset".
    `bin/agentctl`'s `_scan_rate_limits` (~line 6232) parses the same
    `rate_limit_info` shape for a different purpose (per-run overage
    aggregation) and discards `resetsAt`; re-deriving it there would touch a
    finalize path fed straight into `outcome.json`, so this mirrors its
    defensive parsing (substring pre-filter, per-line try/except, `isinstance`
    guards against a truncated or forged transcript) rather than extending it.
  * A CO SESSION's own transcript, when ITS next turn is the one that gets
    rejected, carries the record `stop_record` recognises: either the
    synthetic assistant message the CLI writes for a 429
    (`isApiErrorMessage: true`, `error: "rate_limit"`), or the meta note the
    harness injects to make the co wrap up (`usageLimitNote: "wrap_up"`). Both
    were read verbatim from real transcripts under `~/.claude/projects` from
    last night's incident, not invented from the issue text.

NO PRIVACY BRANCH, DELIBERATELY. A black-box crew's session is only ever acted
on through ITS OWN transcript, resolved by `tmux_transcript.
transcript_for_tmux_session` — which already searches every registered
private crew's own run root (ADR-0043) alongside the shared one. This module
never asks whether a session belongs to a private crew; it asks only what
that session's own transcript shows, which is exactly "skipped unless its own
transcript shows the same stop" with no special case to get wrong.

THE PANE CHECK IS ONE READ, NOT TWO. `session_input._read_input_box` answers
both "is this session sitting at an idle, empty composer" and "is there a
person's draft in it" in a single `capture-pane`: a real box holding text
means a draft (skip, never overwritten — a resume prompt is not worth clearing
someone's half-typed message for); no box-shaped region at all covers a
mid-turn session, a picker/gate, or an unrecognised Claude Code version alike,
and all three answer "cannot confirm idle" the same way (skip). Deliberately
narrower than `session_state`'s hook-derived state machine, whose own module
docstring rejects the screen as a source of STATE — this asks the same narrow
question `session_input` itself is built to ask (does the box hold text or
not), never what state the session is "in".

ONCE PER SESSION PER LIMIT EVENT. The state file keys on the STOPPED
transcript's own path stem (its session id) plus the stopping record's own
`uuid` — not the tmux session name, which a rollover can hand to a new co with
a fresh session id and no memory of the old stop, and not a bare "already
prompted this session" flag, which would never fire again after a second,
later limit event. Recorded only once `send` actually succeeds, so a
transient send failure is retried on the next run rather than silently
swallowed.
"""
from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import tmux_transcript as TT
import session_input as SI
import usage_window as UW

# The reset time in the resume prompt is always read out in the zone the CLI's
# own quota message names ("your session limit resets 3am (America/Phoenix)"),
# regardless of where this machine's own TZ is set, so the text matches what a
# co would have seen in its own transcript if it could still read one.
_PHOENIX = ZoneInfo("America/Phoenix")

# The whole message, fixed. It carries no content from anywhere else — no
# session name, no run id, nothing derived from what the session was doing —
# because this text reaches a session by being typed into its live input box,
# and a resume prompt is not the place to smuggle anything else in.
RESUME_TEMPLATE = (
    "The usage limit reset at {time}. Re-check any runs that died on it with "
    "`agentctl status`, re-dispatch what failed, and continue your Course."
)


def resume_text(resets_at: float) -> str:
    when = datetime.fromtimestamp(resets_at, tz=_PHOENIX).strftime("%H:%M MST")
    return RESUME_TEMPLATE.format(time=when)


# `datetime.fromtimestamp` raises past this: it is `datetime.max` (9999-12-31
# 23:59:59 UTC) as epoch seconds, the last value it accepts before raising
# `ValueError`/`OverflowError` regardless of the target zone's offset.
_MAX_PLAUSIBLE_RESETS_AT = 253_402_300_799.0


def _plausible_resets_at(value) -> bool:
    """`True` for a `resetsAt` this module can safely hand to
    `datetime.fromtimestamp`: a real number, not `bool` (a `json.loads` of
    `true`/`false` is technically an `int` subtype), and a positive
    epoch-seconds value small enough that `fromtimestamp` accepts it. A
    `resetsAt` that fails this is exactly as untrustworthy as a line that
    fails to parse as JSON at all — skipped, not raised on.

    The magnitude bound (`0 < value <= _MAX_PLAUSIBLE_RESETS_AT`) is checked
    BEFORE `math.isfinite`, not after: `json.loads` of a bare oversized
    integer literal (no decimal point or exponent, e.g. a few hundred digits)
    produces a Python `int` with no float rounding, and `math.isfinite`
    converts its argument to a C double before checking it, so it raises
    `OverflowError` on such a value rather than returning `False` — round 2's
    own defect, reopened by an int where round 1's fix only covered float
    overflow (`1e400`). Comparing an arbitrary-precision `int` to a float
    bound with `<`/`<=` never raises in Python, so the bound is safe to check
    first; it also already excludes `nan`/`inf`/`-inf` on its own (every
    comparison with `nan` is `False`, and `inf`/`-inf` fail the bound), which
    is what makes it safe to reach `math.isfinite` afterward — kept for the
    reader, not load-bearing once the bound has passed."""
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and 0 < value <= _MAX_PLAUSIBLE_RESETS_AT and math.isfinite(value))


# A genuine five-hour window's `resetsAt`, read at any point during that
# window, is at most five hours ahead of `now`. `usage_window.
# resets_at_is_plausible` widens that by `_RESETS_AT_SLACK_SECONDS` for clock
# skew between this machine and whatever wrote the transcript, and this
# module calls that same function rather than carrying its own copy of the
# bound, so the two readers of the same `unifiedWindows.five_hour.resetsAt`
# field agree by construction, not because two constants happen to match.
#
# WHAT THIS BOUND DOES AND DOES NOT DO. It limits how far past `now` a
# SINGLE `resetsAt` value may claim to be: one five-hour window plus the
# slack, never a date chosen by whoever wrote the transcript. It does not
# limit how many times a session gets to present a value inside that bound.
# A run that keeps appending fresh `rate_limit_info` events, each with a
# `resetsAt` inside the bound at the time it is read, is trusted the same
# way any other run's telemetry is trusted here — this scan has no notion of
# "the first reading ever seen for this run" to check a later one against,
# and none was added: no occurrence of a dispatched run forging
# `rate_limit_info` has been named, the same ground round 2 of PR #715
# declined cross-run corroboration on.


# --- the current window: scanning every run this machine has recorded -------

# How far back to look for a run whose transcript ever reported the quota.
# Bounded so a machine with years of history does not walk all of it every
# few minutes; two days covers any window that has not yet reset plus the one
# before it, which is all this ever needs.
DEFAULT_RUN_LOOKBACK_DAYS = 2

# How many of the most-recently-touched run transcripts to open looking for a
# sample, once sorted by mtime. A bound, not a measurement: cheap protection
# against a pathological day with thousands of short runs and none of them
# carrying quota telemetry.
DEFAULT_RUN_SCAN_LIMIT = 200


def _run_dirs(days: int) -> "list[Path]":
    """Every `run-*` dispatched-run directory in the last *days* day-buckets,
    across the shared run root and every registered private crew's own
    (`tmux_transcript._run_roots`, ADR-0043) — the same day-bucket search
    `tmux_transcript._recent_run_dirs` already does for `co-*/meta.json`,
    reused here for `run-*` instead."""
    dirs: list[Path] = []
    for day in TT._recent_run_dirs(days=days):
        dirs.extend(day.glob("run-*"))
    return dirs


def _mtime_or_never(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return float("-inf")


def _five_hour_samples(run_dir: Path):
    """Yield `{"resets_at": epoch, "status": str}` for every five-hour
    `rate_limit_info` event in *run_dir*'s transcript, in file order (so the
    last one yielded is the newest one this run ever reported).

    Mirrors `agentctl._scan_rate_limits`'s defensive parsing on purpose: a
    substring pre-filter keeps the common (no match) line cheap, `errors=
    "replace"` and a per-line `json.JSONDecodeError` catch survive a
    transcript truncated mid-flush by a still-running or just-killed process,
    and every `dict` access is `isinstance`-guarded against a line that only
    happens to contain the substring. `resetsAt` itself is further checked by
    `_plausible_resets_at` — `json.loads` parses the non-standard
    `NaN`/`Infinity`/`-Infinity` tokens by default, and any of those (or a
    huge finite value) would later raise out of `datetime.fromtimestamp` in
    `resume_text`, so a sample carrying one is skipped here, the same as a
    line that fails to parse at all. A failure to open or read the file at
    all yields nothing rather than raising — one bad run dir must not stop the
    scan of every other one.
    """
    transcript = run_dir / "transcript.jsonl"
    if not transcript.exists():
        return
    try:
        with transcript.open(errors="replace") as f:
            for line in f:
                if '"rate_limit_info"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue  # a partial last line from a run that died mid-flush
                if not isinstance(event, dict):
                    continue
                info = event.get("rate_limit_info")
                if not isinstance(info, dict):
                    continue
                if info.get("rateLimitType") != "five_hour":
                    continue
                status = info.get("status")
                if not isinstance(status, str):
                    continue
                # The nested field is what actually carries this window's own
                # resetsAt; the flat top-level `resetsAt` is a fallback for a
                # shape that predates `unifiedWindows` or omits it.
                resets_at = None
                windows = info.get("unifiedWindows")
                if isinstance(windows, dict):
                    five_hour = windows.get("five_hour")
                    if isinstance(five_hour, dict):
                        resets_at = five_hour.get("resetsAt")
                if resets_at is None:
                    resets_at = info.get("resetsAt")
                if _plausible_resets_at(resets_at):
                    yield {"resets_at": resets_at, "status": status}
    except OSError:
        return


def latest_five_hour_window(days: int = DEFAULT_RUN_LOOKBACK_DAYS,
                            limit_dirs: int = DEFAULT_RUN_SCAN_LIMIT) -> "dict | None":
    """The newest five-hour window `{"resets_at": epoch, "status": str}` any
    run this machine recorded, or `None` if nothing ever reported one.

    "Newest" is approximated by the run TRANSCRIPT's own mtime, since a
    `rate_limit_event` carries no timestamp of its own (confirmed against
    real transcripts under `<RUNS_ROOT>` — every one that reports quota omits
    it). That is exact enough for this job: the five-hour window's `resetsAt`
    is constant for the whole window, so any run active inside it reports the
    same value, and the only place approximation could matter is within a few
    minutes of an actual reset — precisely the gap a few-minutes-cadence job
    already accepts.
    """
    run_dirs = sorted(_run_dirs(days), key=lambda d: _mtime_or_never(d / "transcript.jsonl"),
                      reverse=True)[:limit_dirs]
    for run_dir in run_dirs:
        samples = list(_five_hour_samples(run_dir))
        if samples:
            return samples[-1]
    return None


# --- did a session's OWN transcript stop on the limit? ----------------------

# How much of the transcript's tail to read looking for its last record.
# Mirrors `co-session-health.TAIL_BYTES`: a transcript can be large, and only
# the last (complete) line is ever wanted here.
TAIL_BYTES = 300_000


def _last_json_object(path) -> "dict | None":
    """The last complete, parseable JSON object in the tail of *path*, or
    `None`. A trailing partial line (a live process still appending) is
    skipped in favour of the last COMPLETE one, never mistaken for it."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - TAIL_BYTES))
            chunk = fh.read().decode("utf-8", "ignore")
    except OSError:
        return None
    for line in reversed(chunk.split("\n")):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        return obj if isinstance(obj, dict) else None
    return None


def stop_record(transcript_path) -> "dict | None":
    """`{"uuid": ..., "kind": "wrap_up" | "api_error"}` if *transcript_path*'s
    own last record is the usage-limit stop condition, else `None`.

    Checking only the LAST record is what "with no later human turn" reduces
    to: if anything at all came after the stop (a human turn, an assistant
    reply, the harness's own `usageLimitNote: "release"`), that later record
    is what is last instead, and this returns `None`. Verified against real
    transcripts from the 2026-09-29 incident: a session that never resumed
    still had the `wrap_up` note as its literal last line, hours later.

    `error == "rate_limit"` rather than matching the message text ("You've
    hit your monthly spend limit") on purpose — the class is a contract the
    CLI sets deliberately; the sentence is prose that will be reworded, the
    same reasoning `agentctl.api_death_is_retryable` already applies to a
    death's status code over its message.
    """
    record = _last_json_object(transcript_path)
    if not isinstance(record, dict):
        return None
    uuid = record.get("uuid")
    if not isinstance(uuid, str) or not uuid:
        return None
    if (record.get("type") == "user" and record.get("isMeta") is True
            and record.get("usageLimitNote") == "wrap_up"):
        return {"uuid": uuid, "kind": "wrap_up"}
    if (record.get("type") == "assistant" and record.get("isApiErrorMessage") is True
            and record.get("error") == "rate_limit"):
        return {"uuid": uuid, "kind": "api_error"}
    return None


# --- once per session per limit event ---------------------------------------


def default_state_path() -> Path:
    return Path.home() / ".cache" / "limit-resume" / "prompted.json"


def load_state(path: Path) -> dict:
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path: Path, state: dict) -> None:
    """Atomic write: a pid-unique temp file plus `os.replace`, mirroring
    `co-working-on._write` — a reader must never see a half-written file."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}")
    try:
        tmp.unlink()
    except OSError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(state, sort_keys=True))
    os.replace(tmp, path)


def _stop_key(transcript_path, stop: dict) -> str:
    return f"{Path(transcript_path).stem}:{stop['uuid']}"


# --- the real wiring, injectable for tests -----------------------------------


def _default_session_names() -> "list[str]":
    return list(TT.session_table().keys())


def _default_resolve_transcript(name: str) -> Path:
    return TT.transcript_for_tmux_session(name)


def _default_read_box(name: str) -> "str | None":
    pane_id, _claude_pid = TT.claude_pane(name)
    return SI._read_input_box(pane_id)


def _default_send(name: str, text: str) -> None:
    SI.send_message(name, text)


def run_once(*, now: "float | None" = None,
            session_names=None,
            resolve_transcript=None,
            read_box=None,
            send=None,
            state_path: "Path | None" = None,
            runs_days: int = DEFAULT_RUN_LOOKBACK_DAYS,
            window: "dict | None" = None,
            log=print) -> dict:
    """Prompt every live co session whose own transcript shows it is stopped
    on a limit that has since reset, at most once per session per limit
    event. Returns `{"prompted": [(name, key), ...], "skipped": [(name,
    reason), ...]}`, mainly so a test (or `bin/limit-resume`'s caller) can
    read exactly what happened without parsing the log.

    Every I/O seam is a keyword argument defaulting to the real tmux/session
    wiring above, so the decision logic here — reset gate, stop detection,
    dedup, pane-idle gate — is fully exercisable with no tmux involved at
    all; `bin/limit-resume` calls this with every default in place.
    """
    now = time.time() if now is None else now
    session_names = session_names or _default_session_names
    resolve_transcript = resolve_transcript or _default_resolve_transcript
    read_box = read_box or _default_read_box
    send = send or _default_send
    state_path = state_path or default_state_path()

    if window is None:
        window = latest_five_hour_window(days=runs_days)

    prompted: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []

    if not isinstance(window, dict) or not _plausible_resets_at(window.get("resets_at")):
        log("limit-resume: no rate_limit_info with a usable resetsAt on record; nothing to do")
        return {"prompted": prompted, "skipped": skipped}

    resets_at = window["resets_at"]
    if not UW.resets_at_is_plausible(resets_at, now):
        log(f"limit-resume: resetsAt {resets_at} is further out than a five-hour window "
            f"reaches from now ({now}); treating it as no usable window")
        return {"prompted": prompted, "skipped": skipped}

    if now <= resets_at:
        log(f"limit-resume: the five-hour window resets at {resets_at}; not yet ({now})")
        return {"prompted": prompted, "skipped": skipped}

    state = load_state(state_path)

    for name in session_names():
        try:
            transcript = resolve_transcript(name)
        except Exception as exc:  # noqa: BLE001 — one bad session must not stop the rest
            reason = f"no transcript: {exc}"
            skipped.append((name, reason))
            log(f"limit-resume: {name}: {reason}")
            continue

        stop = stop_record(transcript)
        if stop is None:
            reason = "not stopped on the limit"
            skipped.append((name, reason))
            log(f"limit-resume: {name}: {reason}")
            continue

        key = _stop_key(transcript, stop)
        if key in state:
            reason = f"already prompted for this limit event ({key})"
            skipped.append((name, reason))
            log(f"limit-resume: {name}: {reason}")
            continue

        try:
            box = read_box(name)
        except Exception as exc:  # noqa: BLE001 — same posture as resolve_transcript above
            reason = f"could not read the input box: {exc}"
            skipped.append((name, reason))
            log(f"limit-resume: {name}: {reason}")
            continue

        if box is None:
            reason = "not confirmed idle at its prompt (mid-turn, a gate, or unreadable)"
            skipped.append((name, reason))
            log(f"limit-resume: {name}: {reason}")
            continue

        if box.strip():
            reason = "pending typed text in the pane"
            skipped.append((name, reason))
            log(f"limit-resume: {name}: {reason}")
            continue

        try:
            text = resume_text(resets_at)
            send(name, text)
        except Exception as exc:  # noqa: BLE001 — retried on the next run, not fatal here
            reason = f"send failed: {exc}"
            skipped.append((name, reason))
            log(f"limit-resume: {name}: {reason}")
            continue

        state[key] = {"session_name": name, "prompted_at": now}
        save_state(state_path, state)
        prompted.append((name, key))
        log(f"limit-resume: {name}: prompted (limit event {key})")

    return {"prompted": prompted, "skipped": skipped}
