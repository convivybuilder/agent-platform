"""Retention for the hook-event spool (Deck WS1.1).

THE DISCLOSURE THIS PINS. The spool holds full user prompts and tool inputs and
nothing deleted any of it. These tests pin the two mechanisms that now do, and —
more importantly — the boundaries where deleting would be WRONG: today's
directory (which still has a live writer), and lines the reducer has not claimed.
"""
from __future__ import annotations

import datetime
import importlib.util
import json
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parent.parent / "lib" / "agent-platform"


@pytest.fixture
def R():
    spec = importlib.util.spec_from_file_location(
        "hook_log_retention", LIB / "hook_log_retention.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TODAY = datetime.date(2026, 7, 20)


def _day(root: Path, day: str) -> Path:
    d = root / day
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write(day_dir: Path, session: str, timestamps: list[str]) -> Path:
    p = day_dir / f"{session}.jsonl"
    p.write_text("\n".join(
        json.dumps({"ts": ts, "event": "Stop", "payload": {"prompt": "secret"}})
        for ts in timestamps) + "\n", encoding="utf-8")
    return p


def _watermark(state_dir: Path, session: str, ts: str) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / f"{session}.json").write_text(
        json.dumps({"indexed_through": ts}), encoding="utf-8")


# --- the backstop: the guarantee everything else sits on top of ---------------

def test_backstop_deletes_a_day_past_the_cutoff(R, tmp_path):
    _write(_day(tmp_path, "2026-07-01"), "s1", ["2026-07-01T10:00:00+00:00"])
    stats = R.apply_backstop(tmp_path, TODAY, days=14)
    assert stats["days_deleted"] == 1
    assert not (tmp_path / "2026-07-01").exists()


def test_backstop_keeps_a_day_inside_the_window(R, tmp_path):
    _write(_day(tmp_path, "2026-07-10"), "s1", ["2026-07-10T10:00:00+00:00"])
    stats = R.apply_backstop(tmp_path, TODAY, days=14)
    assert stats["days_deleted"] == 0
    assert (tmp_path / "2026-07-10").exists()


def test_backstop_deletes_regardless_of_index_state(R, tmp_path):
    """The whole point: a reducer outage must not stop pruning.

    No watermark exists for this session, so purge-after-index would keep every
    line forever. The backstop takes the day anyway.
    """
    _write(_day(tmp_path, "2026-06-01"), "never-indexed", ["2026-06-01T10:00:00+00:00"])
    R.run(tmp_path, TODAY, days=14, state_dir=tmp_path / ".index-state")
    assert not (tmp_path / "2026-06-01").exists()


def test_backstop_boundary_is_exact(R, tmp_path):
    """today - 14 goes; today - 13 stays. Pinning the boundary, not the vibe."""
    _write(_day(tmp_path, "2026-07-06"), "s1", ["2026-07-06T10:00:00+00:00"])  # -14
    _write(_day(tmp_path, "2026-07-07"), "s2", ["2026-07-07T10:00:00+00:00"])  # -13
    R.apply_backstop(tmp_path, TODAY, days=14)
    assert not (tmp_path / "2026-07-06").exists()
    assert (tmp_path / "2026-07-07").exists()


# --- today's directory is never touched --------------------------------------

def test_todays_dir_is_never_pruned(R, tmp_path):
    """THE APPEND-RACE GUARD. The emitter is still appending to today's files.

    A rewrite-and-rename here would silently drop whatever was appended between
    read and rename. The whole design rests on closed days having no writer, so
    this is the assertion that keeps the rewrite safe.
    """
    d = _day(tmp_path, "2026-07-20")
    _write(d, "live", ["2026-07-20T01:00:00+00:00"])
    _watermark(tmp_path / ".index-state", "live", "2026-07-20T23:59:59+00:00")

    stats = R.purge_after_index(tmp_path, TODAY, state_dir=tmp_path / ".index-state")

    assert stats["lines_dropped"] == 0
    assert len((d / "live.jsonl").read_text().strip().splitlines()) == 1


def test_backstop_never_deletes_today_even_with_a_tiny_window(R, tmp_path):
    _write(_day(tmp_path, "2026-07-20"), "live", ["2026-07-20T01:00:00+00:00"])
    R.apply_backstop(tmp_path, TODAY, days=1)
    assert (tmp_path / "2026-07-20").exists()


# --- purge after index --------------------------------------------------------

def test_purge_drops_only_lines_at_or_before_the_watermark(R, tmp_path):
    d = _day(tmp_path, "2026-07-19")
    _write(d, "s1", [
        "2026-07-19T01:00:00+00:00",
        "2026-07-19T02:00:00+00:00",
        "2026-07-19T03:00:00+00:00",
    ])
    _watermark(tmp_path / ".index-state", "s1", "2026-07-19T02:00:00+00:00")

    stats = R.purge_after_index(tmp_path, TODAY, state_dir=tmp_path / ".index-state")

    assert stats["lines_dropped"] == 2
    remaining = [json.loads(x)["ts"]
                 for x in (d / "s1.jsonl").read_text().strip().splitlines()]
    assert remaining == ["2026-07-19T03:00:00+00:00"]


def test_no_watermark_means_nothing_is_purged(R, tmp_path):
    """Absent reducer state must not be read as 'everything is indexed'.

    This is the failure direction that matters: the reducer does not exist yet
    (WS1.3), so every session is in this state today. Getting it backwards would
    delete the entire corpus on first run.
    """
    d = _day(tmp_path, "2026-07-19")
    _write(d, "s1", ["2026-07-19T01:00:00+00:00", "2026-07-19T02:00:00+00:00"])

    stats = R.purge_after_index(tmp_path, TODAY, state_dir=tmp_path / ".index-state")

    assert stats["lines_dropped"] == 0
    assert len((d / "s1.jsonl").read_text().strip().splitlines()) == 2


@pytest.mark.parametrize("body", [
    '{"indexed_through": ""}',
    '{"nope": "2026-07-19T02:00:00+00:00"}',
    '["not", "a", "dict"]',
    'not json at all',
])
def test_a_malformed_watermark_purges_nothing(R, tmp_path, body):
    state = tmp_path / ".index-state"
    state.mkdir(parents=True)
    (state / "s1.json").write_text(body, encoding="utf-8")
    d = _day(tmp_path, "2026-07-19")
    _write(d, "s1", ["2026-07-19T01:00:00+00:00"])

    stats = R.purge_after_index(tmp_path, TODAY, state_dir=state)

    assert stats["lines_dropped"] == 0


def test_an_unreadable_line_is_kept(R, tmp_path):
    """Deleting a record we cannot parse is the wrong way to be wrong.

    We cannot show it was indexed. The backstop collects it later regardless.
    """
    d = _day(tmp_path, "2026-07-19")
    p = d / "s1.jsonl"
    p.write_text('{"ts": "2026-07-19T01:00:00+00:00"}\n'
                 'garbage not json\n'
                 '{"no_ts": true}\n', encoding="utf-8")
    _watermark(tmp_path / ".index-state", "s1", "2026-07-19T23:00:00+00:00")

    R.purge_after_index(tmp_path, TODAY, state_dir=tmp_path / ".index-state")

    remaining = p.read_text().strip().splitlines()
    assert remaining == ['garbage not json', '{"no_ts": true}']


def test_a_fully_purged_file_is_removed(R, tmp_path):
    d = _day(tmp_path, "2026-07-19")
    p = _write(d, "s1", ["2026-07-19T01:00:00+00:00"])
    _watermark(tmp_path / ".index-state", "s1", "2026-07-19T23:00:00+00:00")

    R.purge_after_index(tmp_path, TODAY, state_dir=tmp_path / ".index-state")

    assert not p.exists()


def test_a_pruned_file_keeps_owner_only_mode(R, tmp_path):
    """A rewrite must not return the file more readable than it was.

    The emitter sets 0600 via umask; the rewrite creates a new inode, so the mode
    has to be re-applied explicitly or it inherits the process umask.
    """
    d = _day(tmp_path, "2026-07-19")
    p = _write(d, "s1", ["2026-07-19T01:00:00+00:00", "2026-07-19T05:00:00+00:00"])
    p.chmod(0o600)
    _watermark(tmp_path / ".index-state", "s1", "2026-07-19T02:00:00+00:00")

    R.purge_after_index(tmp_path, TODAY, state_dir=tmp_path / ".index-state")

    assert p.exists()
    assert (p.stat().st_mode & 0o777) == 0o600


# --- the spool root holds more than day dirs ---------------------------------

def test_non_date_entries_are_never_deletion_candidates(R, tmp_path):
    """`.metadata_never_index` and `.index-state` live in the same root.

    The Spotlight marker in particular is a security control — deleting it would
    let the indexer copy prompt text into an index outside the 0600 protection.
    """
    (tmp_path / ".metadata_never_index").touch()
    (tmp_path / ".index-state").mkdir()
    (tmp_path / "not-a-day").mkdir()
    _write(_day(tmp_path, "2026-06-01"), "s1", ["2026-06-01T10:00:00+00:00"])

    R.run(tmp_path, TODAY, days=14, state_dir=tmp_path / ".index-state")

    assert (tmp_path / ".metadata_never_index").exists()
    assert (tmp_path / ".index-state").exists()
    assert (tmp_path / "not-a-day").exists()
    assert not (tmp_path / "2026-06-01").exists()


def test_day_dirs_ignores_unparseable_names(R, tmp_path):
    (tmp_path / "2026-13-99").mkdir()
    (tmp_path / "2026-07-19").mkdir()
    assert [d.name for _, d in R.day_dirs(tmp_path)] == ["2026-07-19"]


def test_run_is_safe_on_a_missing_root(R, tmp_path):
    stats = R.run(tmp_path / "nope", TODAY, days=14)
    assert stats["backstop"]["days_deleted"] == 0


def test_utc_today_is_utc_not_local(R):
    """The emitter files by UTC day; a local key here would delete a live dir.

    Run dirs use a LOCAL day (ARCH-156) and this does not — the mismatch is
    deliberate and worth a test, because copying the run-dir convention would be
    the obvious mistake.
    """
    expected = datetime.datetime.now(datetime.timezone.utc).date()
    assert R.utc_today() == expected


# --- watermark validation (security review, round 1) -------------------------

NOW = datetime.datetime(2026, 7, 20, 12, 0, 0, tzinfo=datetime.timezone.utc)


def test_a_far_future_watermark_is_rejected(R, tmp_path):
    """THE OVER-DELETION THIS PINS.

    A reducer bug writing a max-sentinel as a completion marker would satisfy
    `ts <= watermark` for EVERY line and delete a whole session's log, including
    lines it never processed. Nothing bounded the horizon before this.
    """
    _watermark(tmp_path / ".index-state", "s1", "9999-12-31T23:59:59+00:00")
    d = _day(tmp_path, "2026-07-19")
    _write(d, "s1", ["2026-07-19T01:00:00+00:00", "2026-07-19T02:00:00+00:00"])

    stats = R.purge_after_index(tmp_path, TODAY, state_dir=tmp_path / ".index-state")

    assert stats["lines_dropped"] == 0
    assert len((d / "s1.jsonl").read_text().strip().splitlines()) == 2


def test_a_z_suffixed_watermark_is_rejected(R, tmp_path):
    """A `Z` watermark over-deletes within its own second.

    `.` (46) < `Z` (90), so `...T10:30:00.999999+00:00 <= ...T10:30:00Z` compares
    true and a record AFTER the watermark is dropped. The string compare is only
    sound for the emitter's `+00:00` shape, so anything else is refused rather
    than tolerated with a warning comment.
    """
    assert R._valid_watermark("2026-07-19T10:30:00Z", now=NOW) is False
    assert R._valid_watermark("2026-07-19T10:30:00+00:00", now=NOW) is True


def test_a_naive_or_unparseable_watermark_is_rejected(R):
    assert R._valid_watermark("2026-07-19T10:30:00", now=NOW) is False
    assert R._valid_watermark("nonsense+00:00", now=NOW) is False


def test_a_slightly_future_watermark_is_allowed(R):
    """Small skew is tolerated; the reducer stamps its own clock."""
    soon = (NOW + datetime.timedelta(hours=1)).isoformat()
    assert R._valid_watermark(soon, now=NOW) is True
    too_far = (NOW + datetime.timedelta(hours=72)).isoformat()
    assert R._valid_watermark(too_far, now=NOW) is False


def test_the_skew_horizon_boundary_is_exact(R):
    """Pin 48h itself, not a bracket around it.

    1h-passes/72h-fails brackets the limit but would still pass if someone
    narrowed MAX_WATERMARK_SKEW to 2h. This fails if the constant moves at all.
    """
    at = (NOW + R.MAX_WATERMARK_SKEW).isoformat()
    just_past = (NOW + R.MAX_WATERMARK_SKEW + datetime.timedelta(seconds=1)).isoformat()
    assert R._valid_watermark(at, now=NOW) is True
    assert R._valid_watermark(just_past, now=NOW) is False
    assert R.MAX_WATERMARK_SKEW == datetime.timedelta(hours=48)


# --- the backstop must survive a failing purge pass --------------------------

def test_a_failing_file_does_not_cancel_the_backstop(R, tmp_path, monkeypatch):
    """THE GUARANTEE THIS PROTECTS.

    prune_file re-raises after cleanup. If that propagated, purge_after_index
    would abort before apply_backstop ever ran, so one unreadable file would
    silently cancel the only mechanism bounding the disclosure window.
    """
    d = _day(tmp_path, "2026-07-19")
    _write(d, "boom", ["2026-07-19T01:00:00+00:00"])
    _watermark(tmp_path / ".index-state", "boom", "2026-07-19T23:00:00+00:00")
    # The aged day is left EMPTY of .jsonl so only the failing file raises;
    # otherwise the patched prune_file would raise for it too and the error
    # count would stop being a clear signal.
    _day(tmp_path, "2026-06-01")

    def explode(path, watermark):
        raise PermissionError("nope")
    monkeypatch.setattr(R, "prune_file", explode)

    stats = R.run(tmp_path, TODAY, days=14, state_dir=tmp_path / ".index-state")

    assert stats["purged"]["errors"] == 1
    assert stats["backstop"]["days_deleted"] == 1
    assert not (tmp_path / "2026-06-01").exists()


# --- orphaned temp files ------------------------------------------------------

def test_orphaned_prune_temps_are_swept(R, tmp_path):
    """A SIGKILL between mkstemp and os.replace strands a .prune-* file.

    glob("*.jsonl") never sees it, so orphans would accumulate silently forever.
    """
    d = _day(tmp_path, "2026-07-19")
    orphan = d / ".prune-abc123"
    orphan.write_text("stranded\n", encoding="utf-8")
    _write(d, "s1", ["2026-07-19T01:00:00+00:00"])

    stats = R.purge_after_index(tmp_path, TODAY, state_dir=tmp_path / ".index-state")

    assert stats["orphans"] == 1
    assert not orphan.exists()


def test_todays_orphans_are_left_alone(R, tmp_path):
    """Today's dir is skipped entirely, so a live run's temp is never swept."""
    d = _day(tmp_path, "2026-07-20")
    live = d / ".prune-inflight"
    live.write_text("mid-write\n", encoding="utf-8")

    R.purge_after_index(tmp_path, TODAY, state_dir=tmp_path / ".index-state")

    assert live.exists()


def test_state_dir_follows_the_root_it_is_given(R, tmp_path):
    """THE BUG THIS PINS. `state_dir` defaulted to the LIVE spool's .index-state.

    A run against any other root therefore read PRODUCTION watermarks and deleted
    from the given tree accordingly — two roots silently disagreeing about which
    sessions are indexed. Every earlier test passed `state_dir` explicitly, so
    the suite could not see it; the CLI against a scratch spool could.
    """
    assert R.state_dir_for(tmp_path) == tmp_path / ".index-state"
    assert R.state_dir_for(tmp_path) != R.INDEX_STATE_DIR

    d = _day(tmp_path, "2026-07-19")
    _write(d, "s1", ["2026-07-19T01:00:00+00:00", "2026-07-19T03:00:00+00:00"])
    # Watermark under THIS root, and no state_dir argument at all.
    _watermark(tmp_path / ".index-state", "s1", "2026-07-19T02:00:00+00:00")

    stats = R.purge_after_index(tmp_path, TODAY)

    assert stats["lines_dropped"] == 1
