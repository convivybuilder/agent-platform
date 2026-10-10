"""limit_resume — the scheduled job that wakes a co session stopped on a usage
limit, once, after the limit resets (issue_01M3PXMTR0PFF6QH46F8SN76CZ).

Fixture record shapes below are reduced-field reconstructions of what was read
from real transcripts under `~/.claude/projects` from the 2026-09-29 incident,
not real transcript content — no real session id, uuid, or path appears here.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from datetime import date
from pathlib import Path

import pytest

import limit_resume as LR
import tmux_sandbox
import tmux_transcript as TT
import usage_window as UW

# The real epoch from the 2026-09-29 incident (03:00 MST), used throughout so
# `resume_text` and the reset-gate tests read against a fact, not an arbitrary
# number.
RESETS_AT = 1790676000


# --- resume_text --------------------------------------------------------------


def test_resume_text_names_the_reset_time_in_mst():
    text = LR.resume_text(RESETS_AT)
    assert text == (
        "The usage limit reset at 03:00 MST. Re-check any runs that died on it "
        "with `agentctl status`, re-dispatch what failed, and continue your "
        "Course.")


def test_resume_text_carries_nothing_else():
    """The template has exactly one substitution slot. A future edit that adds
    a second `{...}` would raise on `.format`, which is the point — a resume
    prompt must never grow content from wherever it is called."""
    assert LR.RESUME_TEMPLATE.count("{") == 1


# --- _plausible_resets_at: what may reach datetime.fromtimestamp --------------


def test_plausible_resets_at_accepts_a_real_epoch_value():
    assert LR._plausible_resets_at(RESETS_AT)
    assert LR._plausible_resets_at(float(RESETS_AT))


def test_plausible_resets_at_rejects_non_finite_values():
    """`json.loads` parses the non-standard `NaN`/`Infinity`/`-Infinity`
    tokens by default, so these are exactly what a malformed or forged
    transcript line can carry past a bare `isinstance(x, (int, float))`
    check — each must read as implausible, not crash `fromtimestamp`."""
    assert not LR._plausible_resets_at(float("nan"))
    assert not LR._plausible_resets_at(float("inf"))
    assert not LR._plausible_resets_at(float("-inf"))
    assert not LR._plausible_resets_at(1e400)  # overflows to float("inf") itself


def test_plausible_resets_at_rejects_out_of_range_values():
    assert not LR._plausible_resets_at(-1e20)  # a huge negative resetsAt
    assert not LR._plausible_resets_at(0)
    assert not LR._plausible_resets_at(-1)


def test_plausible_resets_at_boundary_matches_datetime_max():
    assert LR._plausible_resets_at(LR._MAX_PLAUSIBLE_RESETS_AT)
    assert not LR._plausible_resets_at(LR._MAX_PLAUSIBLE_RESETS_AT + 1)


def test_plausible_resets_at_rejects_bool_and_non_numeric():
    """`True`/`False` are `int` subtypes in Python; `json.loads` never
    produces either for a `resetsAt` field, but the guard must not trust the
    type alone."""
    assert not LR._plausible_resets_at(True)
    assert not LR._plausible_resets_at(False)
    assert not LR._plausible_resets_at("1790676000")
    assert not LR._plausible_resets_at(None)


# A bare oversized digit-string literal in JSON (no decimal point or
# exponent) parses as a Python `int` with no float rounding — distinct from
# `1e400`, which is already a `float` (`inf`) by the time it reaches this
# module. `int('9' * 400)` is a few hundred digits, comfortably past
# `sys.float_info.max`.
_OVERSIZED_INT_RESETS_AT = int("9" * 400)


def test_plausible_resets_at_rejects_an_oversized_int_without_raising():
    """The security-reviewer's reproduction on PR #715 round 2:
    `math.isfinite` converts its argument to a C double before checking it,
    so calling it before the magnitude check raises `OverflowError` for an
    `int` this large rather than returning `False`. Checking the magnitude
    bound first must reject this value without raising anything."""
    assert not LR._plausible_resets_at(_OVERSIZED_INT_RESETS_AT)


# --- stop_record ---------------------------------------------------------------


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def _wrap_up_note(uuid="uuid-wrap-1"):
    """Reduced-field reconstruction of the harness's injected note, matching
    the real record's `type`/`isMeta`/`usageLimitNote` shape."""
    return {"type": "user", "isMeta": True, "turnCompanion": True,
            "usageLimitNote": "wrap_up", "uuid": uuid,
            "message": {"role": "user", "content": "[Usage limit reached...]"}}


def _release_note(uuid="uuid-release-1"):
    return {"type": "user", "isMeta": True, "turnCompanion": True,
            "usageLimitNote": "release", "uuid": uuid,
            "message": {"role": "user", "content": "[Earlier usage-limit notes...]"}}


def _api_error_record(uuid="uuid-apierr-1", error="rate_limit"):
    """Reduced-field reconstruction of the CLI's synthetic 429 message."""
    return {"type": "assistant", "isApiErrorMessage": True, "error": error,
            "apiErrorStatus": 429, "uuid": uuid,
            "message": {"model": "<synthetic>", "role": "assistant",
                        "content": [{"type": "text", "text": "You've hit your..."}]}}


def _human_turn(uuid="uuid-human-1"):
    return {"type": "user", "uuid": uuid,
            "message": {"role": "user", "content": "keep going"}}


def _assistant_turn(uuid="uuid-asst-1"):
    return {"type": "assistant", "uuid": uuid,
            "message": {"role": "assistant",
                        "content": [{"type": "text", "text": "done"}]}}


def test_the_wrap_up_note_is_the_stop_record(tmp_path):
    t = tmp_path / "s.jsonl"
    _write_jsonl(t, [_human_turn(), _assistant_turn(), _wrap_up_note("u1")])
    assert LR.stop_record(t) == {"uuid": "u1", "kind": "wrap_up"}


def test_the_api_error_record_is_the_stop_record(tmp_path):
    t = tmp_path / "s.jsonl"
    _write_jsonl(t, [_human_turn(), _api_error_record("u2")])
    assert LR.stop_record(t) == {"uuid": "u2", "kind": "api_error"}


def test_a_later_human_turn_is_not_a_stop(tmp_path):
    """The positive control for the negative below: the same file, minus the
    turn that came after the note, IS a stop — so this is the note's presence
    that matters, not something else about the fixture."""
    t = tmp_path / "s.jsonl"
    _write_jsonl(t, [_wrap_up_note("u1")])
    assert LR.stop_record(t) is not None

    t2 = tmp_path / "s2.jsonl"
    _write_jsonl(t2, [_wrap_up_note("u1"), _human_turn("u-later")])
    assert LR.stop_record(t2) is None


def test_a_later_assistant_turn_is_not_a_stop(tmp_path):
    t = tmp_path / "s.jsonl"
    _write_jsonl(t, [_api_error_record("u2"), _assistant_turn("u-later")])
    assert LR.stop_record(t) is None


def test_the_release_note_after_wrap_up_is_not_a_stop(tmp_path):
    """The harness's own clearance note, once a session already resumed
    naturally (a background waiter, or the operator typing) — must not read as a fresh
    stop worth re-prompting."""
    t = tmp_path / "s.jsonl"
    _write_jsonl(t, [_wrap_up_note("u1"), _release_note("u3")])
    assert LR.stop_record(t) is None


def test_an_api_error_that_is_not_a_rate_limit_is_not_a_stop(tmp_path):
    """`isApiErrorMessage` alone is not the signal — a 500 or a network blip
    sets it too. Only `error: "rate_limit"` names the limit specifically."""
    t = tmp_path / "s.jsonl"
    _write_jsonl(t, [_api_error_record("u4", error="overloaded")])
    assert LR.stop_record(t) is None


def test_an_empty_transcript_is_not_a_stop(tmp_path):
    t = tmp_path / "s.jsonl"
    t.write_text("")
    assert LR.stop_record(t) is None


def test_a_missing_transcript_is_not_a_stop(tmp_path):
    assert LR.stop_record(tmp_path / "nope.jsonl") is None


def test_a_truncated_trailing_line_falls_back_to_the_last_complete_one(tmp_path):
    """The transcript is appended to live; a process that died or is still
    mid-write can leave a partial final line. That must not be read as the
    stop record, nor crash the check — the last COMPLETE line still is one."""
    t = tmp_path / "s.jsonl"
    _write_jsonl(t, [_wrap_up_note("u1")])
    with t.open("a") as f:
        f.write('{"type":"user","isMeta":true,"usageLimitN')  # no trailing newline
    assert LR.stop_record(t) == {"uuid": "u1", "kind": "wrap_up"}


def test_a_record_with_no_uuid_is_not_a_stop(tmp_path):
    t = tmp_path / "s.jsonl"
    note = _wrap_up_note()
    del note["uuid"]
    _write_jsonl(t, [note])
    assert LR.stop_record(t) is None


# --- latest_five_hour_window ---------------------------------------------------


def _run_dir(root: Path, run_id: str) -> Path:
    d = root / date.today().isoformat() / run_id
    d.mkdir(parents=True)
    (d / "transcript.jsonl").write_text("")
    return d


def _append_rate_limit(run_dir: Path, resets_at, status, *, rate_limit_type="five_hour",
                       nested=True, extra_info=None):
    info = {"status": status, "rateLimitType": rate_limit_type}
    if extra_info:
        info.update(extra_info)
    if nested:
        info["unifiedWindows"] = {
            "five_hour": {"utilization": 0.5, "resetsAt": resets_at},
            "seven_day": {"utilization": 0.1, "resetsAt": resets_at + 1_000_000},
        }
    else:
        info["resetsAt"] = resets_at
    with (run_dir / "transcript.jsonl").open("a") as f:
        f.write(json.dumps({"type": "rate_limit_event", "uuid": "u",
                            "rate_limit_info": info}) + "\n")
    return run_dir / "transcript.jsonl"


def test_the_nested_unifiedWindows_value_is_used(tmp_path, monkeypatch):
    monkeypatch.setattr(TT, "RUNS_ROOT", tmp_path)
    d = _run_dir(tmp_path, "run-a")
    _append_rate_limit(d, RESETS_AT, "rejected", nested=True)
    got = LR.latest_five_hour_window()
    assert got == {"resets_at": RESETS_AT, "status": "rejected"}


def test_a_flat_resetsAt_is_the_fallback_without_unifiedWindows(tmp_path, monkeypatch):
    monkeypatch.setattr(TT, "RUNS_ROOT", tmp_path)
    d = _run_dir(tmp_path, "run-a")
    _append_rate_limit(d, RESETS_AT, "allowed", nested=False)
    got = LR.latest_five_hour_window()
    assert got == {"resets_at": RESETS_AT, "status": "allowed"}


def test_no_rate_limit_info_anywhere_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(TT, "RUNS_ROOT", tmp_path)
    (tmp_path / date.today().isoformat()).mkdir(parents=True)
    assert LR.latest_five_hour_window() is None


def test_a_seven_day_only_event_is_not_mistaken_for_a_five_hour_one(tmp_path, monkeypatch):
    monkeypatch.setattr(TT, "RUNS_ROOT", tmp_path)
    d = _run_dir(tmp_path, "run-a")
    _append_rate_limit(d, RESETS_AT, "allowed_warning", rate_limit_type="seven_day")
    assert LR.latest_five_hour_window() is None


def test_the_most_recently_touched_run_wins(tmp_path, monkeypatch):
    """Two runs, two different resetsAt (a window that has since rolled over);
    the run whose transcript was touched more recently is trusted."""
    monkeypatch.setattr(TT, "RUNS_ROOT", tmp_path)
    older = _run_dir(tmp_path, "run-old")
    newer = _run_dir(tmp_path, "run-new")
    t_old = _append_rate_limit(older, RESETS_AT, "rejected")
    t_new = _append_rate_limit(newer, RESETS_AT + 18_000, "allowed")
    now = LR._mtime_or_never(t_new) + 10
    os.utime(t_old, (now - 100, now - 100))
    os.utime(t_new, (now, now))
    assert LR.latest_five_hour_window() == {"resets_at": RESETS_AT + 18_000, "status": "allowed"}


def test_the_last_sample_within_one_transcript_wins(tmp_path, monkeypatch):
    """The quota can flip mid-run (a status going from allowed to rejected);
    the last line in the file is the newest thing that run ever reported."""
    monkeypatch.setattr(TT, "RUNS_ROOT", tmp_path)
    d = _run_dir(tmp_path, "run-a")
    _append_rate_limit(d, RESETS_AT, "allowed")
    _append_rate_limit(d, RESETS_AT, "rejected")
    assert LR.latest_five_hour_window() == {"resets_at": RESETS_AT, "status": "rejected"}


def test_a_truncated_transcript_survives_and_still_finds_the_earlier_sample(tmp_path, monkeypatch):
    monkeypatch.setattr(TT, "RUNS_ROOT", tmp_path)
    d = _run_dir(tmp_path, "run-a")
    _append_rate_limit(d, RESETS_AT, "rejected")
    with (d / "transcript.jsonl").open("a") as f:
        f.write('{"type":"rate_limit_event","rate_limit_info":{"isUsing')
    assert LR.latest_five_hour_window() == {"resets_at": RESETS_AT, "status": "rejected"}


def test_nonfinite_resetsAt_samples_are_skipped_not_returned(tmp_path, monkeypatch):
    """`json.loads` of `NaN`/`Infinity`/`-Infinity`/an overflowed literal is
    each what a malformed or forged transcript line can carry as `resetsAt`;
    none of them may come back as the window — every one must be treated
    exactly like a sample that failed to parse at all."""
    monkeypatch.setattr(TT, "RUNS_ROOT", tmp_path)
    for i, bad in enumerate([float("nan"), float("inf"), float("-inf"), 1e400, -1e20]):
        d = _run_dir(tmp_path, f"run-bad-{i}")
        _append_rate_limit(d, bad, "rejected")
    assert LR.latest_five_hour_window() is None


def test_a_nonfinite_newest_sample_falls_back_to_the_next_valid_one(tmp_path, monkeypatch):
    """The positive control for the case above: in the SAME transcript, a
    malformed sample after a good one must not blank out the good one —
    `latest_five_hour_window` still returns the last VALID sample."""
    monkeypatch.setattr(TT, "RUNS_ROOT", tmp_path)
    d = _run_dir(tmp_path, "run-a")
    _append_rate_limit(d, RESETS_AT, "allowed")
    _append_rate_limit(d, float("nan"), "rejected")
    assert LR.latest_five_hour_window() == {"resets_at": RESETS_AT, "status": "allowed"}


def test_an_oversized_int_resetsAt_sample_is_skipped_not_returned(tmp_path, monkeypatch):
    """A 400-digit `resetsAt` inside a real `rate_limit_info` transcript
    line — `json.loads` produces a Python `int` outside float range, not a
    float literal like `1e400` — must be skipped the same as any other
    implausible sample, never raise out of `_five_hour_samples` past its own
    `except OSError`."""
    monkeypatch.setattr(TT, "RUNS_ROOT", tmp_path)
    d = _run_dir(tmp_path, "run-bad-int")
    _append_rate_limit(d, _OVERSIZED_INT_RESETS_AT, "rejected")
    assert LR.latest_five_hour_window() is None


def test_an_oversized_int_newest_sample_falls_back_to_the_next_valid_one(tmp_path, monkeypatch):
    """The positive control for the case above: in the SAME transcript, an
    oversized-int sample after a good one must not blank out the good one."""
    monkeypatch.setattr(TT, "RUNS_ROOT", tmp_path)
    d = _run_dir(tmp_path, "run-a")
    _append_rate_limit(d, RESETS_AT, "allowed")
    _append_rate_limit(d, _OVERSIZED_INT_RESETS_AT, "rejected")
    assert LR.latest_five_hour_window() == {"resets_at": RESETS_AT, "status": "allowed"}


# --- state file ------------------------------------------------------------------


def test_state_round_trips(tmp_path):
    path = tmp_path / "sub" / "prompted.json"
    LR.save_state(path, {"a:1": {"session_name": "x", "prompted_at": 1.0}})
    assert LR.load_state(path) == {"a:1": {"session_name": "x", "prompted_at": 1.0}}


def test_a_missing_state_file_loads_empty(tmp_path):
    assert LR.load_state(tmp_path / "nope.json") == {}


def test_a_corrupt_state_file_loads_empty_rather_than_raising(tmp_path):
    path = tmp_path / "prompted.json"
    path.write_text("{not json")
    assert LR.load_state(path) == {}


# --- run_once: the orchestration, entirely with injected fakes ------------------


class _Fake:
    """A minimal stand-in for the tmux/session-input seams `run_once` takes as
    keyword arguments. Each session is `name -> (transcript_path, box)`; `box`
    is the string `_read_input_box` would return, or `None` for "unreadable"."""

    def __init__(self, sessions: dict):
        self.sessions = sessions
        self.sent: list[tuple[str, str]] = []
        self.send_should_fail: set[str] = set()

    def names(self):
        return list(self.sessions.keys())

    def resolve(self, name):
        path, _box = self.sessions[name]
        if path is None:
            raise TT.JoinError(f"no transcript for {name!r}")
        return path

    def box(self, name):
        _path, box = self.sessions[name]
        return box

    def send(self, name, text):
        if name in self.send_should_fail:
            raise RuntimeError("tmux rejected the paste")
        self.sent.append((name, text))


def _fixture_transcript(tmp_path, name, records):
    path = tmp_path / f"{name}.jsonl"
    _write_jsonl(path, records)
    return path


def _run(fake, window, *, now, state_path):
    return LR.run_once(now=now, session_names=fake.names, resolve_transcript=fake.resolve,
                       read_box=fake.box, send=fake.send, state_path=state_path,
                       window=window, log=lambda *_: None)


def test_a_session_stopped_on_the_limit_is_prompted_after_reset(tmp_path):
    t = _fixture_transcript(tmp_path, "sess-1", [_wrap_up_note("u1")])
    fake = _Fake({"deck": (t, "")})
    window = {"resets_at": RESETS_AT, "status": "rejected"}
    result = _run(fake, window, now=RESETS_AT + 60, state_path=tmp_path / "state.json")
    assert fake.sent == [("deck", LR.resume_text(RESETS_AT))]
    assert result["prompted"] == [("deck", "sess-1:u1")]


def test_not_prompted_before_the_reset(tmp_path):
    """The positive control above shows the same fixture DOES get sent once
    resets_at has passed; this is the same session, one second early."""
    t = _fixture_transcript(tmp_path, "sess-1", [_wrap_up_note("u1")])
    fake = _Fake({"deck": (t, "")})
    window = {"resets_at": RESETS_AT, "status": "rejected"}
    result = _run(fake, window, now=RESETS_AT - 1, state_path=tmp_path / "state.json")
    assert fake.sent == []
    assert result["prompted"] == []


def test_no_resetsAt_on_record_means_nothing_happens(tmp_path, monkeypatch):
    t = _fixture_transcript(tmp_path, "sess-1", [_wrap_up_note("u1")])
    fake = _Fake({"deck": (t, "")})
    # window=None with no rate_limit_info anywhere on record (empty RUNS_ROOT)
    monkeypatch.setattr(TT, "RUNS_ROOT", tmp_path / "empty-runs-root")
    (tmp_path / "empty-runs-root" / date.today().isoformat()).mkdir(parents=True)
    result = LR.run_once(now=RESETS_AT + 60, session_names=fake.names,
                         resolve_transcript=fake.resolve, read_box=fake.box,
                         send=fake.send, state_path=tmp_path / "state.json",
                         window=None, log=lambda *_: None)
    assert fake.sent == []
    assert result["prompted"] == []


def test_a_session_not_stopped_on_the_limit_is_not_prompted(tmp_path):
    t = _fixture_transcript(tmp_path, "sess-1", [_human_turn(), _assistant_turn()])
    fake = _Fake({"deck": (t, "")})
    window = {"resets_at": RESETS_AT, "status": "rejected"}
    result = _run(fake, window, now=RESETS_AT + 60, state_path=tmp_path / "state.json")
    assert fake.sent == []
    assert any("not stopped" in reason for _n, reason in result["skipped"])


def test_a_session_with_pending_typed_text_is_skipped(tmp_path):
    t = _fixture_transcript(tmp_path, "sess-1", [_wrap_up_note("u1")])
    fake = _Fake({"deck": (t, "half a draft")})
    window = {"resets_at": RESETS_AT, "status": "rejected"}
    result = _run(fake, window, now=RESETS_AT + 60, state_path=tmp_path / "state.json")
    assert fake.sent == []
    assert any("pending typed text" in reason for _n, reason in result["skipped"])


def test_a_session_that_cannot_be_confirmed_idle_is_skipped(tmp_path):
    """`box is None` covers a mid-turn session, a gate/picker, or an
    unrecognised Claude Code version alike — all three must skip, never send."""
    t = _fixture_transcript(tmp_path, "sess-1", [_wrap_up_note("u1")])
    fake = _Fake({"deck": (t, None)})
    window = {"resets_at": RESETS_AT, "status": "rejected"}
    result = _run(fake, window, now=RESETS_AT + 60, state_path=tmp_path / "state.json")
    assert fake.sent == []
    assert any("not confirmed idle" in reason for _n, reason in result["skipped"])


def test_a_session_with_no_resolvable_transcript_is_skipped(tmp_path):
    fake = _Fake({"scratch-shell": (None, "")})
    window = {"resets_at": RESETS_AT, "status": "rejected"}
    result = _run(fake, window, now=RESETS_AT + 60, state_path=tmp_path / "state.json")
    assert fake.sent == []
    assert any("no transcript" in reason for _n, reason in result["skipped"])


def test_a_second_run_sends_nothing_more(tmp_path):
    t = _fixture_transcript(tmp_path, "sess-1", [_wrap_up_note("u1")])
    fake = _Fake({"deck": (t, "")})
    window = {"resets_at": RESETS_AT, "status": "rejected"}
    state_path = tmp_path / "state.json"
    _run(fake, window, now=RESETS_AT + 60, state_path=state_path)
    assert len(fake.sent) == 1
    _run(fake, window, now=RESETS_AT + 300, state_path=state_path)
    assert len(fake.sent) == 1  # unchanged


def test_a_new_limit_event_on_the_same_session_prompts_again(tmp_path):
    t = _fixture_transcript(tmp_path, "sess-1", [_wrap_up_note("u1")])
    fake = _Fake({"deck": (t, "")})
    window = {"resets_at": RESETS_AT, "status": "rejected"}
    state_path = tmp_path / "state.json"
    _run(fake, window, now=RESETS_AT + 60, state_path=state_path)
    assert len(fake.sent) == 1

    # A fresh limit event: same session, new note (a later reset window).
    t.write_text("")
    _write_jsonl(t, [_wrap_up_note("u-second")])
    window2 = {"resets_at": RESETS_AT + 18_000, "status": "rejected"}
    _run(fake, window2, now=RESETS_AT + 18_060, state_path=state_path)
    assert len(fake.sent) == 2


def test_a_send_failure_is_not_recorded_and_is_retried_next_run(tmp_path):
    t = _fixture_transcript(tmp_path, "sess-1", [_wrap_up_note("u1")])
    fake = _Fake({"deck": (t, "")})
    fake.send_should_fail.add("deck")
    window = {"resets_at": RESETS_AT, "status": "rejected"}
    state_path = tmp_path / "state.json"
    result = _run(fake, window, now=RESETS_AT + 60, state_path=state_path)
    assert fake.sent == []
    assert any("send failed" in reason for _n, reason in result["skipped"])

    fake.send_should_fail.discard("deck")
    result2 = _run(fake, window, now=RESETS_AT + 120, state_path=state_path)
    assert len(fake.sent) == 1
    assert result2["prompted"] == [("deck", "sess-1:u1")]


def test_one_sessions_failure_does_not_stop_the_rest(tmp_path):
    good = _fixture_transcript(tmp_path, "sess-good", [_wrap_up_note("u-good")])
    fake = _Fake({"broken": (None, ""), "deck": (good, "")})
    window = {"resets_at": RESETS_AT, "status": "rejected"}
    result = _run(fake, window, now=RESETS_AT + 60, state_path=tmp_path / "state.json")
    assert fake.sent == [("deck", LR.resume_text(RESETS_AT))]
    assert any(n == "broken" for n, _r in result["skipped"])


def test_a_nonfinite_window_resetsAt_does_not_crash_run_once(tmp_path):
    """The security-reviewer's reproduction on #715: a `window["resets_at"]`
    of NaN bypasses a bare `now <= resets_at` comparison (every comparison
    with NaN is `False`), and Infinity/-Infinity/an overflowed literal each
    raise out of `datetime.fromtimestamp` inside `resume_text`, uncaught,
    which killed the whole scheduled run. None of these may prompt or raise.
    Round 2 reopened the same crash via a bare oversized `int` (no float
    literal involved) raising `OverflowError` out of `_plausible_resets_at`
    itself, past `run_once`'s own window-validity gate — included here
    alongside the round-1 shapes since it is the same "must not raise"
    contract on the same call."""
    t = _fixture_transcript(tmp_path, "sess-1", [_wrap_up_note("u1")])
    bad_values = [float("nan"), float("inf"), float("-inf"), 1e400, -1e20,
                  _OVERSIZED_INT_RESETS_AT]
    for i, bad in enumerate(bad_values):
        fake = _Fake({"deck": (t, "")})
        window = {"resets_at": bad, "status": "rejected"}
        result = _run(fake, window, now=RESETS_AT + 60, state_path=tmp_path / f"state-{i}.json")
        assert fake.sent == []
        assert result["prompted"] == []


def test_a_far_future_resetsAt_is_treated_as_no_usable_window(tmp_path):
    """`usage_window.resets_at_is_plausible` applied here, the same call PR
    #714's reader makes on the same field: a `resetsAt` finite and inside
    `_MAX_PLAUSIBLE_RESETS_AT` (so `_plausible_resets_at` alone would accept
    it) but further out than a five-hour window plus the shared clock-skew
    slack can reach from `now` is not a real five-hour window — a forged or
    drifted value like this would otherwise suppress every wake on this
    session until that date."""
    now = RESETS_AT
    far_future = now + UW.FIVE_HOUR_WINDOW_SECONDS + UW._RESETS_AT_SLACK_SECONDS + 1
    t = _fixture_transcript(tmp_path, "sess-1", [_wrap_up_note("u1")])
    fake = _Fake({"deck": (t, "")})
    window = {"resets_at": far_future, "status": "rejected"}
    logs = []
    result = LR.run_once(now=now, session_names=fake.names, resolve_transcript=fake.resolve,
                         read_box=fake.box, send=fake.send, state_path=tmp_path / "state.json",
                         window=window, log=logs.append)
    assert fake.sent == []
    assert result["prompted"] == []
    assert any("no usable window" in msg for msg in logs)


def test_a_resetsAt_just_within_the_shared_bound_reaches_the_ordinary_reset_gate(tmp_path):
    """The positive control for the case above: a `resetsAt` exactly at the
    bound `usage_window.resets_at_is_plausible` shares with `read_five_hour_
    window` (a genuine five-hour window plus the slack) must still reach the
    ordinary "not yet reset" gate, not the far-future rejection — the bound
    must not shrink a real window's own five-hour width."""
    now = RESETS_AT
    just_inside = now + UW.FIVE_HOUR_WINDOW_SECONDS + UW._RESETS_AT_SLACK_SECONDS
    t = _fixture_transcript(tmp_path, "sess-1", [_wrap_up_note("u1")])
    fake = _Fake({"deck": (t, "")})
    window = {"resets_at": just_inside, "status": "rejected"}
    logs = []
    result = LR.run_once(now=now, session_names=fake.names, resolve_transcript=fake.resolve,
                         read_box=fake.box, send=fake.send, state_path=tmp_path / "state.json",
                         window=window, log=logs.append)
    assert fake.sent == []
    assert result["prompted"] == []
    assert any("not yet" in msg for msg in logs)
    assert not any("no usable window" in msg for msg in logs)


def test_a_resetsAt_beyond_the_old_15_minute_margin_is_now_accepted(tmp_path):
    """Round 3 bounded this at `now + 5h + 15min`, a number that disagreed
    with `usage_window`'s own `now + 5h + 1h` reader of the same field
    (round 4's finding). A `resetsAt` in the gap between the two — past the
    old, narrower margin but inside the shared one — must now reach the
    ordinary "not yet reset" gate rather than being rejected as far-future."""
    now = RESETS_AT
    between_old_and_new_margin = now + UW.FIVE_HOUR_WINDOW_SECONDS + 30 * 60
    assert now + UW.FIVE_HOUR_WINDOW_SECONDS + 15 * 60 < between_old_and_new_margin
    assert between_old_and_new_margin <= now + UW.FIVE_HOUR_WINDOW_SECONDS + UW._RESETS_AT_SLACK_SECONDS
    t = _fixture_transcript(tmp_path, "sess-1", [_wrap_up_note("u1")])
    fake = _Fake({"deck": (t, "")})
    window = {"resets_at": between_old_and_new_margin, "status": "rejected"}
    logs = []
    result = LR.run_once(now=now, session_names=fake.names, resolve_transcript=fake.resolve,
                         read_box=fake.box, send=fake.send, state_path=tmp_path / "state.json",
                         window=window, log=logs.append)
    assert fake.sent == []
    assert result["prompted"] == []
    assert any("not yet" in msg for msg in logs)
    assert not any("no usable window" in msg for msg in logs)


def test_a_resume_text_failure_for_one_session_does_not_stop_the_rest(tmp_path, monkeypatch):
    """Requirement: `resume_text` runs inside the loop's own per-session
    error handling, so a formatting failure on one session (whatever its
    cause) cannot stop the sessions after it from being prompted."""
    good = _fixture_transcript(tmp_path, "sess-good", [_wrap_up_note("u-good")])
    bad = _fixture_transcript(tmp_path, "sess-bad", [_wrap_up_note("u-bad")])
    fake = _Fake({"broken-fmt": (bad, ""), "deck": (good, "")})
    window = {"resets_at": RESETS_AT, "status": "rejected"}

    real_resume_text = LR.resume_text
    calls = {"n": 0}

    def flaky(resets_at):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OverflowError("timestamp out of range for platform time_t")
        return real_resume_text(resets_at)

    monkeypatch.setattr(LR, "resume_text", flaky)

    result = _run(fake, window, now=RESETS_AT + 60, state_path=tmp_path / "state.json")
    assert any(n == "broken-fmt" and "send failed" in r for n, r in result["skipped"])
    assert result["prompted"] == [("deck", "sess-good:u-good")]


def test_a_session_whose_transcript_lives_under_a_private_crew_home_is_handled_identically(tmp_path):
    """No privacy branch exists in `run_once` — a black-box crew's session is
    prompted or skipped purely on what ITS OWN transcript shows, wherever the
    transcript resolver found it. `resolve_transcript` (the real
    `tmux_transcript.transcript_for_tmux_session`) is what already searches a
    private crew's own run root (ADR-0043); this only has to show that path
    location plays no other part in the decision."""
    private_home = tmp_path / "private-crew-home" / "agent-runs"
    private_home.mkdir(parents=True)
    t = private_home / "sess-private.jsonl"
    _write_jsonl(t, [_wrap_up_note("u-private")])
    fake = _Fake({"blackbox": (t, "")})
    window = {"resets_at": RESETS_AT, "status": "rejected"}
    result = _run(fake, window, now=RESETS_AT + 60, state_path=tmp_path / "state.json")
    assert fake.sent == [("blackbox", LR.resume_text(RESETS_AT))]
    assert result["prompted"] == [("blackbox", "sess-private:u-private")]


def test_guards_hold_even_against_a_sender_that_never_refuses(tmp_path):
    """`send` here always succeeds — so every skip below has to come from
    `run_once`'s own guards, not from the sender happening to refuse. Each
    negative is paired with the SAME fixture's positive control (the one
    changed condition that lets it through), so a guard that silently always
    skipped would be caught here too.
    """
    window = {"resets_at": RESETS_AT, "status": "rejected"}

    stopped = _fixture_transcript(tmp_path, "stopped", [_wrap_up_note("u1")])
    not_stopped = _fixture_transcript(tmp_path, "not-stopped", [_human_turn()])

    # before reset: nothing sent, even though the session IS stopped.
    fake = _Fake({"deck": (stopped, "")})
    _run(fake, window, now=RESETS_AT - 1, state_path=tmp_path / "s1.json")
    assert fake.sent == []

    # after reset, not stopped: nothing sent.
    fake = _Fake({"deck": (not_stopped, "")})
    _run(fake, window, now=RESETS_AT + 60, state_path=tmp_path / "s2.json")
    assert fake.sent == []

    # after reset, stopped, pending draft: nothing sent.
    fake = _Fake({"deck": (stopped, "still typing")})
    _run(fake, window, now=RESETS_AT + 60, state_path=tmp_path / "s3.json")
    assert fake.sent == []

    # after reset, stopped, already prompted for this event: nothing MORE sent.
    fake = _Fake({"deck": (stopped, "")})
    state_path = tmp_path / "s4.json"
    _run(fake, window, now=RESETS_AT + 60, state_path=state_path)
    assert len(fake.sent) == 1
    _run(fake, window, now=RESETS_AT + 120, state_path=state_path)
    assert len(fake.sent) == 1

    # positive control: after reset, stopped, empty box, first time — sent.
    fake = _Fake({"deck": (stopped, "")})
    _run(fake, window, now=RESETS_AT + 60, state_path=tmp_path / "s5.json")
    assert len(fake.sent) == 1


# --- real tmux: the default wiring, on a private/isolated server ---------------


@pytest.fixture
def private_tmux(monkeypatch):
    """Isolates a real tmux server by `TMUX_TMPDIR`, not `-L`: the code under
    test (`tmux_transcript.run`, via `session_table`/`claude_pane`) invokes bare
    `tmux` with no `-L`, so naming a socket only here would put this test's
    session on a socket the code under test never looks at (see
    `tests/test_co_rollover_real_tmux.py`'s `private_tmux` fixture for the full
    argument). Teardown targets the isolated socket explicitly by `-S`, never a
    bare `kill-server`, which would resolve against the operator's own server."""
    reason = tmux_sandbox.sandbox_refusal()
    if reason:
        pytest.skip(reason)
    socket_dir = Path(tempfile.mkdtemp(prefix="lr-tmux-"))
    monkeypatch.setenv("TMUX_TMPDIR", str(socket_dir))
    monkeypatch.delenv("TMUX", raising=False)
    yield
    socket_path = socket_dir / f"tmux-{os.getuid()}" / "default"
    subprocess.run(["tmux", "-S", str(socket_path), "kill-server"],
                   capture_output=True, timeout=10)
    shutil.rmtree(socket_dir, ignore_errors=True)


pytestmark_tmux = pytest.mark.skipif(shutil.which("tmux") is None, reason="no tmux on this host")


@pytestmark_tmux
def test_default_session_names_and_pane_resolution_against_a_real_tmux_session(private_tmux):
    session = "lrtest-wiring"
    subprocess.run(["tmux", "new-session", "-d", "-s", session,
                   "bash", "-c", "exec -a claude sleep 600"], check=True, timeout=10)
    try:
        assert session in LR._default_session_names()
        pane_id, claude_pid = TT.claude_pane(session)
        assert isinstance(claude_pid, int) and claude_pid > 0

        # No real Claude Code TUI is drawing a composer in this pane — a plain
        # `sleep` has no box-bordered region for `_read_input_box` to find, so
        # this must read `None` ("cannot confirm idle"), not crash and not
        # misread the bare shell as an empty composer.
        assert LR._default_read_box(session) is None
    finally:
        subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True, timeout=10)


@pytestmark_tmux
def test_default_resolve_transcript_raises_cleanly_with_no_run_dir_record(private_tmux, tmp_path, monkeypatch):
    monkeypatch.setattr(TT, "RUNS_ROOT", tmp_path)
    (tmp_path / date.today().isoformat()).mkdir(parents=True)
    session = "lrtest-notranscript"
    subprocess.run(["tmux", "new-session", "-d", "-s", session,
                   "bash", "-c", "exec -a claude sleep 600"], check=True, timeout=10)
    try:
        with pytest.raises(TT.JoinError):
            LR._default_resolve_transcript(session)
    finally:
        subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True, timeout=10)


# --- bin/limit-resume: the entry point's own lock ------------------------------


def test_the_entry_point_skips_when_the_lock_is_already_held(limit_resume_cli, tmp_path, monkeypatch, capsys):
    import fcntl
    monkeypatch.setattr(limit_resume_cli, "LOCK_PATH", tmp_path / "run.lock")
    calls = []
    monkeypatch.setattr(limit_resume_cli.LR, "run_once", lambda: calls.append(1))

    held_fd = os.open(str(tmp_path / "run.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(held_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        rc = limit_resume_cli.main()
    finally:
        fcntl.flock(held_fd, fcntl.LOCK_UN)
        os.close(held_fd)

    assert rc == 0
    assert calls == []
    assert "already in progress" in capsys.readouterr().out


def test_the_entry_point_runs_once_when_unlocked(limit_resume_cli, tmp_path, monkeypatch):
    monkeypatch.setattr(limit_resume_cli, "LOCK_PATH", tmp_path / "run.lock")
    calls = []
    monkeypatch.setattr(limit_resume_cli.LR, "run_once", lambda: calls.append(1))
    assert limit_resume_cli.main() == 0
    assert calls == [1]
