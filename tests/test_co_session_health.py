"""The billed-subscription statusline segment (bin/co-session-health, ARCH-339).

Two Claude Max subscriptions share this machine (ARCH-252), and nothing in a co
window said which one the session was spending. The segment answers that.

What these tests guard (ARCH-407 corrected ARCH-339):
  - The segment shows the LIVE slot — the one credential the machine is on now,
    which every session shares and bills. A `claude-sub use` is reflected within
    the TTL, not frozen at launch (the ARCH-339 bug: a session read its launch
    account forever while actually billing whatever the switch moved to).
  - The Keychain read is cached SHARED with a short TTL, so it runs at most once
    per TTL window, not on every keystroke-debounced render.
  - A transient resolve failure keeps the last good value; only a failure with no
    prior value degrades to a visible `sub ?`, never to a broken/missing line.
  - Dead-session sidecars are swept so the cache dir does not grow without bound.
"""
from __future__ import annotations

import json
import time

import pytest


@pytest.fixture(scope="session")
def csh():
    from conftest import _load_cli
    return _load_cli("co-session-health")


@pytest.fixture
def cache(csh, tmp_path, monkeypatch):
    monkeypatch.setattr(csh, "SUB_CACHE_DIR", str(tmp_path / "cache"))
    return tmp_path


def _resolves(csh, monkeypatch, value):
    """Stub _resolve_sub, counting how many times it actually runs."""
    calls = []

    def fake():
        calls.append(1)
        return value

    monkeypatch.setattr(csh, "_resolve_sub", fake)
    return calls


def test_segment_renders_slot_and_account(csh, cache, monkeypatch):
    account = "operator@example.com"
    _, _, domain = account.partition("@")
    _resolves(csh, monkeypatch, f"nyh {account}")
    seg = csh._sub_segment("sess-1")
    assert "sub nyh (operator)" in seg
    # Only the local part renders — the line is width-tight, and `_sub_label`
    # does `email.split('@')[0]`. Asserting the DOMAIN absent covers the whole
    # address, since the domain is a substring of it, and also catches a leak
    # of the domain alone.
    #
    # DERIVED FROM THE FIXTURE, NEVER A LITERAL. This assertion once named a
    # domain outright; when the fixture's address changed the literal stayed
    # behind, testing for a string no input contained — passing whatever
    # `_sub_label` did, while still reading like a guard.
    assert domain not in seg


def test_resolve_runs_once_per_ttl_window(csh, cache, monkeypatch):
    calls = _resolves(csh, monkeypatch, "convivy operator@example.com")
    for _ in range(5):
        seg = csh._sub_segment("sess-2")
    assert "sub convivy (operator)" in seg
    assert len(calls) == 1, "within the TTL the Keychain read must not re-run per render"


def test_a_switch_is_reflected_after_the_ttl(csh, cache, monkeypatch):
    """The ARCH-339 bug, inverted into the guard. `claude-sub use` swaps one
    machine-wide credential, so a running session bills the new account once its
    launch token refreshes — the segment must follow, not freeze at launch. With
    the TTL elapsed, the switch shows."""
    monkeypatch.setattr(csh, "SUB_TTL_S", 0)                     # every render re-resolves
    _resolves(csh, monkeypatch, "convivy operator@example.com")
    assert "sub convivy (operator)" in csh._sub_segment("sess-3")
    _resolves(csh, monkeypatch, "nyh operator@example.com")  # the switch
    assert "sub nyh (operator)" in csh._sub_segment("sess-3")   # SAME session follows it


def test_a_transient_resolve_failure_keeps_the_last_good_value(csh, cache, monkeypatch):
    """A locked Keychain read returns '?' — but a correct display must not flip to
    'sub ?' over a blip. The last good value holds until a resolve succeeds."""
    monkeypatch.setattr(csh, "SUB_TTL_S", 0)
    _resolves(csh, monkeypatch, "nyh operator@example.com")
    assert "sub nyh (operator)" in csh._sub_segment("sess-x")
    _resolves(csh, monkeypatch, "?")                             # transient failure
    assert "sub nyh (operator)" in csh._sub_segment("sess-x")   # not "?"


def test_failed_resolve_is_visible_not_silent(csh, cache, monkeypatch):
    _resolves(csh, monkeypatch, "?")
    assert "sub ?" in csh._sub_segment("sess-5")


def test_the_segment_renders_without_a_session_id(csh, cache, monkeypatch):
    """The slot is machine-wide, so the segment no longer needs a session_id — the
    credential exists whether or not this render carries one."""
    _resolves(csh, monkeypatch, "convivy operator@example.com")
    assert "sub convivy (operator)" in csh._sub_segment(None)
    assert "sub convivy (operator)" in csh._sub_segment("")


def test_session_id_cannot_escape_the_cache_dir(csh, cache, monkeypatch):
    """session_id still reaches a filesystem path through the per-session vitals
    sidecar, so the traversal scrub must still hold there — the sub label itself
    is now a fixed `sub-current` and never interpolates the id."""
    assert csh._session_key("../../etc/passwd") == "etcpasswd"
    csh._write_vitals("../../etc/passwd", {"x": 1})
    written = {p.name for p in (cache / "cache").iterdir()}
    assert written == {"vitals-etcpasswd.json"}


def test_resolver_never_raises_on_a_broken_claude_sub(csh, monkeypatch):
    monkeypatch.setattr(csh, "CLAUDE_SUB", "/nonexistent/claude-sub")
    assert csh._resolve_sub() == "?"


def test_a_failed_cache_write_still_renders_the_segment(csh, cache, monkeypatch):
    """An unwritable cache (full disk, bad permissions) must not swallow the
    segment: this render already resolved the label, so it shows it."""
    _resolves(csh, monkeypatch, "convivy operator@example.com")

    def boom(*a, **kw):
        raise PermissionError("read-only filesystem")

    monkeypatch.setattr(csh.os, "makedirs", boom)
    assert "sub convivy (operator)" in csh._sub_segment("sess-6")


def test_cache_file_is_owner_only(csh, cache, monkeypatch):
    _resolves(csh, monkeypatch, "convivy operator@example.com")
    csh._sub_segment("sess-7")
    written = (cache / "cache" / "sub-current")
    assert written.stat().st_mode & 0o777 == 0o600


def test_stale_session_sidecars_are_swept_but_live_slot_is_kept(csh, cache, monkeypatch):
    """The 108-file accumulation the launch-frozen cache left behind. A dead
    session's sidecars age out; `sub-current` never does."""
    monkeypatch.setattr(csh, "SUB_TTL_S", 0)
    d = cache / "cache"
    d.mkdir(parents=True, exist_ok=True)
    old = csh.time.time() - csh.SUB_GC_AGE_S - 1
    for name in ("sub-deadsession", "vitals-deadsession.json"):
        p = d / name
        p.write_text("x")
        csh.os.utime(p, (old, old))
    _resolves(csh, monkeypatch, "nyh operator@example.com")
    csh._sub_segment("sess-live")                                # triggers a resolve + sweep
    names = {p.name for p in d.iterdir()}
    assert "sub-deadsession" not in names and "vitals-deadsession.json" not in names
    assert "sub-current" in names


def test_ansi_escapes_in_the_label_cannot_repaint_the_terminal(csh, cache, monkeypatch):
    """slot/email come from Keychain attributes and land in an ANSI-colored line.
    A label carrying escapes must be stripped, not rendered."""
    _resolves(csh, monkeypatch, "\x1b[2Jconvivy operator@example.com")
    seg = csh._sub_segment("sess-8")
    assert "\x1b[2J" not in seg
    assert "sub convivy (operator)" in seg


def test_an_all_escapes_label_reads_as_unresolved(csh, cache, monkeypatch):
    """Stripping can empty a label. `sub ?` says what happened; `sub ` says nothing."""
    _resolves(csh, monkeypatch, "\x1b[2J\x1b[H")
    assert "sub ?" in csh._sub_segment("sess-9")


def test_timeouts_are_short_enough_for_a_status_line(csh):
    """The resolve sits in front of a session's first render; 2x this is the
    worst delay the operator can feel."""
    assert csh.SUB_TIMEOUT_S <= 3


# --- the PR the session is working on (operator, 2026-07-24) -------------------------


def test_the_pr_is_taken_from_the_statusline_payload(csh):
    """Claude Code hands the PR to the status-line command as pr.{number,url,
    review_state} — the same badge the TUI footer draws. Recorded from there
    rather than derived from the branch, which would be a gh call per turn."""
    pr = csh._pr({"pr": {"number": 33,
                         "url": "https://github.com/convivy/convivy-deck/pull/33",
                         "review_state": "approved"}})
    assert pr == {"number": 33,
                  "url": "https://github.com/convivy/convivy-deck/pull/33",
                  "review_state": "approved"}


def test_no_pr_is_none_rather_than_a_half_object(csh):
    """Absent until a PR is found for the branch, and dropped once it merges — so
    None is the ordinary case, and a payload with no usable number yields it."""
    assert csh._pr({}) is None
    assert csh._pr({"pr": None}) is None
    assert csh._pr({"pr": {"url": "x"}}) is None          # no number
    assert csh._pr({"pr": {"number": "33"}}) is None       # number not an int


def test_a_pr_with_only_a_number_keeps_its_optional_fields_none(csh):
    assert csh._pr({"pr": {"number": 7}}) == {
        "number": 7, "url": None, "review_state": None}


# --- the working-on segment (operator, 2026-08-27) ---------------------------------
#
# WHAT THESE GUARD, and why the segment exists at all. Measured across the fleet
# on 2026-08-27: 18 co sessions live in tmux, 7 had ever recorded a working-on
# line, the freshest of those was 7.9 hours old, and the re-announce path had
# been used zero times. The dominant failure is a line NEVER SET — which a co
# cannot see today, because the age is drawn in Deck's cockpit and the co is not
# looking at it.
#
# The load-bearing property is that the segment is SILENT when the line is
# fresh. That makes an empty string mean two different things — "fresh" and
# "something went wrong" — so every test below pins which one it is by moving
# the record rather than by asserting emptiness alone.



@pytest.fixture
def working_on(csh, cache, monkeypatch):
    """A session keyed `testsess`, with the pane→name mapping pre-seeded so no
    test shells out to a tmux that is not there."""
    import os
    cache_dir = csh.SUB_CACHE_DIR
    os.makedirs(cache_dir, exist_ok=True)
    monkeypatch.setenv("TMUX", "/tmp/fake,1,0")
    monkeypatch.setenv("TMUX_PANE", "%99")
    with open(os.path.join(cache_dir, "pane-99"), "w") as fh:
        fh.write("testsess")

    def record(age_seconds=None, body=None):
        path = os.path.join(cache_dir, "working-testsess.json")
        if body is not None:
            with open(path, "w") as fh:
                fh.write(body)
            return path
        with open(path, "w") as fh:
            json.dump({"text": "chasing the pagination bug",
                       "written_at": time.time() - age_seconds,
                       "started_at": time.time() - age_seconds}, fh)
        return path

    return record


def test_a_line_never_set_says_so_rather_than_staying_quiet(csh, working_on):
    """THE MAJORITY CASE. 11 of 18 sessions were here when this was written."""
    assert "never set" in csh._working_on_segment()


def test_a_fresh_line_draws_nothing(csh, working_on):
    working_on(age_seconds=60)
    assert csh._working_on_segment() == ""


def test_a_stale_line_shows_its_age(csh, working_on):
    working_on(age_seconds=45 * 60)
    assert "45m" in csh._working_on_segment()


def test_the_threshold_is_the_thing_being_tested_not_the_clock(csh, working_on):
    """Both sides of the boundary, so a changed default cannot pass by luck."""
    working_on(age_seconds=csh.WORKING_ON_STALE_S - 30)
    assert csh._working_on_segment() == ""
    working_on(age_seconds=csh.WORKING_ON_STALE_S + 30)
    assert csh._working_on_segment() != ""


def test_ages_read_in_one_unit(csh, working_on):
    for seconds, expected in ((45 * 60, "45m"), (5 * 3600, "5h"), (3 * 86400, "3d")):
        working_on(age_seconds=seconds)
        assert expected in csh._working_on_segment()


def test_the_segment_never_instructs_only_states(csh, working_on):
    """DELIBERATE, AND THE REASON IS THE SECOND TIMESTAMP. A co told to "update"
    rewrites the text, and any new text resets `started_at` — so a nagging
    segment would convert "how long has this been the work" into "time since the
    co was last nagged". An age is a fact a co cannot comply with cosmetically.
    """
    working_on(age_seconds=5 * 3600)
    rendered = csh._working_on_segment().lower()
    for imperative in ("update", "please", "run ", "should", "remember"):
        assert imperative not in rendered


def test_a_malformed_record_reads_as_no_record(csh, working_on):
    working_on(body="{ not json at all")
    assert "never set" in csh._working_on_segment()


def test_a_record_whose_timestamp_is_not_a_number_reads_as_no_record(csh, working_on):
    """Guards the arithmetic: a string `written_at` must not raise out of a
    status line, and must not be silently treated as age zero either."""
    working_on(body=json.dumps({"text": "x", "written_at": "yesterday"}))
    assert "never set" in csh._working_on_segment()


def test_outside_tmux_the_segment_is_silent(csh, working_on, monkeypatch):
    """Nothing is keyed outside tmux, so there is nothing honest to say."""
    monkeypatch.delenv("TMUX", raising=False)
    assert csh._working_on_segment() == ""


def test_the_pane_name_is_cached_rather_than_shelled_out_per_render(csh, cache, monkeypatch):
    """THE HOT PATH. A full render measured ~27 ms warm and `tmux
    display-message` ~5 ms, so resolving per render would tax every turn ~16%
    for one cosmetic segment."""
    import os
    calls = []

    def fake_run(*a, **k):
        calls.append(a)
        raise OSError("must not be reached")

    os.makedirs(csh.SUB_CACHE_DIR, exist_ok=True)
    monkeypatch.setenv("TMUX", "/tmp/fake,1,0")
    monkeypatch.setenv("TMUX_PANE", "%99")
    with open(os.path.join(csh.SUB_CACHE_DIR, "pane-99"), "w") as fh:
        fh.write("testsess")
    monkeypatch.setattr(csh.subprocess, "run", fake_run)

    for _ in range(5):
        csh._working_on_segment()
    assert calls == [], "the pane→session-name mapping was re-resolved per render"

@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_a_degenerate_float_stamp_reads_as_no_record(csh, working_on, literal):
    """`isinstance(x, float)` IS TRUE FOR NaN, and Python's json accepts the
    literal by default — `jq`, which the bash half of this pair uses, does not.
    So this shape is admitted by exactly one of the two sides.

    Before the `isfinite` guard, NaN reached `_fmt_age` where `int(nan // 60)`
    raised ValueError, uncaught, out of `main()` BEFORE the status line was
    written — blanking the context gauge, the model, the credential and the
    in-flight runs, none of which this segment has any business touching.
    """
    working_on(body='{"text": "x", "written_at": ' + literal + '}')
    assert "never set" in csh._working_on_segment()


def test_a_broken_segment_cannot_blank_the_status_line(csh, cache, monkeypatch, capsys):
    """THE MECHANISM, NOT THE INSTANCE. The isfinite guard closes the NaN case;
    this closes the class. A cosmetic segment must never be able to take the
    line down with it, whatever future bug it grows — so the call site swallows
    anything the segment raises, and this test asserts that by making it raise.
    """
    def explode():
        raise RuntimeError("a bug nobody predicted")

    monkeypatch.setattr(csh, "_working_on_segment", explode)
    monkeypatch.setattr(csh, "_read_payload", lambda: {
        "session_id": "s", "model": {"display_name": "Opus 5"},
        "context_window": {"input_tokens": 1000, "context_window": 200000}})
    monkeypatch.setattr(csh, "_write_vitals", lambda *a, **k: None)
    csh.main()
    out = capsys.readouterr().out
    assert "ctx" in out, "the status line was lost when the segment raised"

def test_a_broken_segment_says_so_on_stderr(csh, cache, monkeypatch, capsys):
    """A guard that hides a bug perfectly is the failure this file keeps finding
    elsewhere. The line survives; stderr says why, so a maintainer has something
    to notice."""
    monkeypatch.setattr(csh, "_working_on_segment",
                        lambda: (_ for _ in ()).throw(RuntimeError("planted")))
    monkeypatch.setattr(csh, "_read_payload", lambda: {
        "session_id": "s", "model": {"display_name": "Opus 5"},
        "context_window": {"input_tokens": 1000, "context_window": 200000}})
    monkeypatch.setattr(csh, "_write_vitals", lambda *a, **k: None)
    csh.main()
    captured = capsys.readouterr()
    assert "ctx" in captured.out, "the status line was lost"
    assert "planted" in captured.err, "the failure was swallowed with no trace"


# --- rollover: quoting the hook's last decision (ADR-0069) --------------------
#
# `tools/rollover-trigger.py` owns the zone; this segment only reads what it
# already wrote to `rollover-<session_id>.json` in the same cache dir the
# vitals sidecar uses. These tests write that file directly rather than
# running the hook, the same separation `_sub_segment`'s own tests draw
# between resolving a credential and rendering it.

def _write_rollover(cache, session_id, **fields):
    d = cache / "cache"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"rollover-{session_id}.json").write_text(json.dumps(fields))


def test_seeking_shows_the_count_and_cap(csh, cache):
    _write_rollover(cache, "s1", state="seeking", calls_seeking=12, cap=40)
    seg = csh._rollover_segment("s1")
    assert "seeking 12/40" in seg


def test_seeking_with_no_cap_shows_the_count_alone(csh, cache):
    _write_rollover(cache, "s1", state="seeking", calls_seeking=5, cap=None)
    seg = csh._rollover_segment("s1")
    assert "seeking 5" in seg
    assert "/" not in seg


def test_roll_now_is_its_own_line(csh, cache):
    _write_rollover(cache, "s1", state="roll_now", calls_seeking=41, cap=40)
    seg = csh._rollover_segment("s1")
    assert "ROLL NOW" in seg


def test_ok_renders_nothing(csh, cache):
    _write_rollover(cache, "s1", state="ok", calls_seeking=0, cap=None)
    assert csh._rollover_segment("s1") == ""


def test_no_file_yet_renders_nothing(csh, cache):
    assert csh._rollover_segment("never-fired") == ""


def test_a_malformed_file_renders_nothing_rather_than_raising(csh, cache):
    d = cache / "cache"
    d.mkdir(parents=True, exist_ok=True)
    (d / "rollover-s1.json").write_text("not json")
    assert csh._rollover_segment("s1") == ""


def test_session_id_cannot_escape_the_rollover_directory(csh, cache):
    d = cache / "cache"
    d.mkdir(parents=True, exist_ok=True)
    (cache / "secret.json").write_text(json.dumps({"state": "roll_now"}))
    assert csh._rollover_segment("../secret") == ""


def test_a_broken_rollover_segment_cannot_blank_the_status_line(csh, cache, monkeypatch, capsys):
    """Same posture as `_working_on_segment`'s own guard: a cosmetic segment
    must never be able to take the whole status line down with it."""
    monkeypatch.setattr(csh, "_rollover_segment",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("planted")))
    monkeypatch.setattr(csh, "_read_payload", lambda: {
        "session_id": "s", "model": {"display_name": "Opus 5"},
        "context_window": {"input_tokens": 1000, "context_window": 200000}})
    monkeypatch.setattr(csh, "_write_vitals", lambda *a, **k: None)
    csh.main()
    captured = capsys.readouterr()
    assert "ctx" in captured.out, "the status line was lost when the segment raised"
    assert "planted" in captured.err, "the failure was swallowed with no trace"


def test_stale_rollover_sidecars_are_swept(csh, cache, monkeypatch):
    """The same accumulation shape `sub-*`/`vitals-*`/`pane-*` are already swept
    for: a dead session's `rollover-*.json` never gets rewritten, so it has to
    age out on the same sweep rather than growing without bound."""
    monkeypatch.setattr(csh, "SUB_TTL_S", 0)
    d = cache / "cache"
    d.mkdir(parents=True, exist_ok=True)
    old = csh.time.time() - csh.SUB_GC_AGE_S - 1
    p = d / "rollover-deadsession.json"
    p.write_text(json.dumps({"state": "seeking"}))
    csh.os.utime(p, (old, old))

    def fake_resolve():
        return "nyh operator@example.com"
    monkeypatch.setattr(csh, "_resolve_sub", fake_resolve)
    csh._sub_segment("sess-live")  # triggers a resolve + sweep, same as its sibling test

    names = {q.name for q in d.iterdir()}
    assert "rollover-deadsession.json" not in names


def test_stale_render_staleness_sidecars_are_swept_but_a_live_pair_is_kept(csh, cache, monkeypatch):
    """`tools/render-staleness-context.py`'s own baseline/fingerprint pair
    (round 1 finding, PR #839 review): one pair per session, never rewritten
    by anything once a session ends, the same accumulation shape `sub-*`/
    `vitals-*`/`pane-*`/`rollover-*` are already swept for."""
    monkeypatch.setattr(csh, "SUB_TTL_S", 0)
    d = cache / "cache"
    d.mkdir(parents=True, exist_ok=True)
    old = csh.time.time() - csh.SUB_GC_AGE_S - 1
    dead_baseline = d / "render-staleness-deadsession-baseline.json"
    dead_state = d / "render-staleness-deadsession.json"
    for p in (dead_baseline, dead_state):
        p.write_text("{}")
        csh.os.utime(p, (old, old))
    live_baseline = d / "render-staleness-livesession-baseline.json"
    live_state = d / "render-staleness-livesession.json"
    for p in (live_baseline, live_state):
        p.write_text("{}")

    def fake_resolve():
        return "nyh operator@example.com"
    monkeypatch.setattr(csh, "_resolve_sub", fake_resolve)
    csh._sub_segment("sess-live")  # triggers a resolve + sweep, same as its sibling test

    names = {q.name for q in d.iterdir()}
    assert "render-staleness-deadsession-baseline.json" not in names
    assert "render-staleness-deadsession.json" not in names
    assert "render-staleness-livesession-baseline.json" in names
    assert "render-staleness-livesession.json" in names
