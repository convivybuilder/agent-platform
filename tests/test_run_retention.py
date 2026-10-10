"""Retention for `~/Convivy/agent-runs` (issue_01M109N0BN6N0H47XFASSN2FJJ, bosun).

THE DISCLOSURE THIS PINS is not a leak — it is permanence. Every timestamp on a
run directory is same-UID forgeable, so the day-directory NAME is the key
anyway, and the whole design rests on the predicate being a closed TWO-SIDED
calendar band rather than a one-sided cutoff. These tests pin that band, and —
more importantly — the failure mode where a naive one-sided refactor passes
almost everything here except the one test that catches it.
"""
from __future__ import annotations

import datetime
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "lib" / "agent-platform"
RUN_RETENTION_BIN = REPO / "bin" / "run-retention"


@pytest.fixture
def R():
    spec = importlib.util.spec_from_file_location(
        "run_retention", LIB / "run_retention.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TODAY = datetime.date(2026, 8, 27)


def _day(root: Path, name: str) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _run_dir(day_dir: Path, name: str, files: dict) -> Path:
    d = day_dir / name
    d.mkdir(parents=True, exist_ok=True)
    for fname, content in files.items():
        (d / fname).write_text(content, encoding="utf-8")
    return d


def _full_run_files() -> dict:
    """A run directory shaped like a real one: the keep set plus the bulk."""
    return {
        "meta.json": json.dumps({"agent": "coder"}),
        "cost.json": json.dumps({"usd": 0.42}),
        "outcome.json": json.dumps({"exit": 0}),
        "pr-link.json": json.dumps({"pr": 1}),
        "policy.json": json.dumps({}),
        "verdict.json": json.dumps({}),
        "docs_consulted.json": json.dumps([]),
        "subagent.json": json.dumps({}),
        "handoff.md": "# handoff\n",
        "transcript.jsonl": '{"line": 1}\n' * 50,
        "tool_calls.jsonl": '{"tool": "Read"}\n',
        "detach.out.log": "stdout\n" * 100,
        "detach.err.log": "",
        "stderr.log": "",
        "gateway_spend.jsonl": '{"usd": 0.01}\n',
    }


# --- test 1: the future-dated directory, both the tree AND the classifier ----

def test_a_future_dated_day_is_a_prune_target_on_the_first_pass(R):
    """THE MOST IMPORTANT TEST IN THE SET.

    A naive `if day < cutoff` refactor passes every other test in this file and
    silently restores permanence for a forward-dated directory. Asserting the
    CLASSIFIER, not just an empty tree, is what separates "the band rejected
    it" from "rmtree happened to run for an unrelated reason."
    """
    assert R.should_act("2099-01-01", TODAY, R.PRUNE_DAYS) is True


def test_a_future_dated_day_directory_is_actually_deleted(R, tmp_path):
    _day(tmp_path, "2099-01-01")
    stats = R.apply_prune(tmp_path, TODAY, days=R.PRUNE_DAYS)
    assert stats["days_deleted"] == 1
    assert not (tmp_path / "2099-01-01").exists()


def test_a_one_sided_cutoff_would_have_kept_the_future_day(R):
    """Pins the contrast directly: a one-sided `day < cutoff` says False here,
    which is the bug. `should_act` must say True."""
    cutoff = TODAY - datetime.timedelta(days=R.PRUNE_DAYS)
    one_sided_would_act = datetime.date(2099, 1, 1) < cutoff
    assert one_sided_would_act is False
    assert R.should_act("2099-01-01", TODAY, R.PRUNE_DAYS) is True


# --- test 2: a day-pattern name that isn't a real date -----------------------

def test_an_unparseable_date_shaped_name_is_a_prune_target(R):
    assert R.should_act("2026-13-45", TODAY, R.PRUNE_DAYS) is True


def test_an_unparseable_date_shaped_directory_is_deleted(R, tmp_path):
    _day(tmp_path, "2026-13-45")
    stats = R.apply_prune(tmp_path, TODAY, days=R.PRUNE_DAYS)
    assert stats["days_deleted"] == 1
    assert not (tmp_path / "2026-13-45").exists()


# --- test 3: only day-pattern names are candidates ---------------------------

def test_only_day_pattern_entries_are_candidates_everything_else_survives(R, tmp_path):
    (tmp_path / "active.json").write_text('{"runs": []}', encoding="utf-8")
    (tmp_path / ".active.lock").touch()
    (tmp_path / "stale-runs.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "scorecard").mkdir()
    (tmp_path / "scorecard" / "ledger.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "hook-log").mkdir()
    doomed = _day(tmp_path, "2020-01-01")

    R.run(tmp_path, TODAY)

    assert not doomed.exists()
    assert (tmp_path / "active.json").read_text() == '{"runs": []}'
    assert (tmp_path / ".active.lock").exists()
    assert (tmp_path / "stale-runs.jsonl").read_text() == "{}\n"
    assert (tmp_path / "scorecard" / "ledger.jsonl").read_text() == "{}\n"
    assert (tmp_path / "hook-log").exists()


# --- test 4: thinning keeps the fact set, drops the bulk set, both by name ---

def test_thinning_keeps_facts_and_drops_bulk_both_asserted_by_name(R, tmp_path):
    old_day = TODAY - datetime.timedelta(days=R.THIN_DAYS + 1)
    day_dir = _day(tmp_path, old_day.isoformat())
    run_dir = _run_dir(day_dir, "run-0155b75e", _full_run_files())

    R.apply_thin(tmp_path, TODAY, days=R.THIN_DAYS)

    for keep in R.KEEP_FILES:
        assert (run_dir / keep).exists(), f"{keep} should survive thinning"
    for drop in ("transcript.jsonl", "tool_calls.jsonl", "detach.out.log",
                 "detach.err.log", "stderr.log", "gateway_spend.jsonl"):
        assert not (run_dir / drop).exists(), f"{drop} should be dropped by thinning"


def test_a_handoff_in_a_bucket_past_the_thin_window_survives_thinning(R, tmp_path):
    """A session keeps the day bucket of its first call, so its handoff.md can sit
    in a bucket 35+ days old. Thinning must keep it; the 100-day prune still bounds it."""
    old_day = TODAY - datetime.timedelta(days=36)
    day_dir = _day(tmp_path, old_day.isoformat())
    run_dir = _run_dir(day_dir, "co-vivy-0914", {"handoff.md": "# handoff\n",
                                                 "transcript.jsonl": "x\n"})

    R.apply_thin(tmp_path, TODAY, days=R.THIN_DAYS)

    assert (run_dir / "handoff.md").exists()
    assert (run_dir / "handoff.md").read_text() == "# handoff\n"
    assert not (run_dir / "transcript.jsonl").exists()


def test_thinning_does_not_delete_everything(R, tmp_path):
    """An allowlist/denylist inversion is silent in the direction that deletes
    the keep set too — a count-only check on the drop side would still pass."""
    old_day = TODAY - datetime.timedelta(days=R.THIN_DAYS + 1)
    day_dir = _day(tmp_path, old_day.isoformat())
    run_dir = _run_dir(day_dir, "run-aaaaaaaa", _full_run_files())

    R.apply_thin(tmp_path, TODAY, days=R.THIN_DAYS)

    remaining = {p.name for p in run_dir.iterdir()}
    assert remaining == R.KEEP_FILES


def test_thinning_removes_the_prompts_directory(R, tmp_path):
    old_day = TODAY - datetime.timedelta(days=R.THIN_DAYS + 1)
    day_dir = _day(tmp_path, old_day.isoformat())
    run_dir = _run_dir(day_dir, "run-bbbbbbbb", _full_run_files())
    (run_dir / "prompts").mkdir()
    (run_dir / "prompts" / "0.txt").write_text("hi", encoding="utf-8")

    R.apply_thin(tmp_path, TODAY, days=R.THIN_DAYS)

    assert not (run_dir / "prompts").exists()


# --- test 5: a thinned run never reads as in-flight --------------------------

def test_thinning_never_deletes_cost_json_or_orphans_meta_json(R, tmp_path):
    """In-flight is encoded as absence: meta.json present, cost.json absent.

    Thinning must never produce that shape for a run that was NOT in flight —
    thousands of month-old runs reading as live would feed the daily budget's
    leaf-burn-rate estimate and could trip the autonomous hard cap. Since both
    files are in KEEP_FILES this holds by construction; this test is the
    tripwire if that ever stops being true.
    """
    old_day = TODAY - datetime.timedelta(days=R.THIN_DAYS + 10)
    day_dir = _day(tmp_path, old_day.isoformat())
    run_dir = _run_dir(day_dir, "run-cccccccc", _full_run_files())

    R.apply_thin(tmp_path, TODAY, days=R.THIN_DAYS)

    assert (run_dir / "cost.json").exists()
    assert (run_dir / "meta.json").exists()


def test_a_still_running_looking_run_inside_the_thin_window_is_untouched(R, tmp_path):
    """A run with meta.json but no cost.json yet (genuinely in flight) that
    happens to sit in an old-enough day must not have its meta.json touched —
    thinning only ever removes files outside KEEP_FILES, never keep-set files,
    regardless of which are present."""
    old_day = TODAY - datetime.timedelta(days=R.THIN_DAYS + 1)
    day_dir = _day(tmp_path, old_day.isoformat())
    run_dir = _run_dir(day_dir, "run-dddddddd", {"meta.json": "{}"})

    R.apply_thin(tmp_path, TODAY, days=R.THIN_DAYS)

    assert (run_dir / "meta.json").exists()
    assert not (run_dir / "cost.json").exists()


# --- test 6: the boundary is local, not UTC ----------------------------------

def test_local_today_matches_platform_day_not_utc(R, monkeypatch):
    """The run-dir day key is LOCAL (ARCH-156), unlike the hook-log spool's UTC
    key. Asserted against telemetry.platform_day() itself, not a fixed offset,
    so this fails if someone swaps in a UTC clock here."""
    import telemetry as T
    assert R.local_today() == datetime.datetime.strptime(
        T.platform_day(), "%Y-%m-%d").date()


def test_todays_own_directory_is_never_a_retention_target(R, tmp_path):
    """A dispatch running RIGHT NOW writes into today's directory. If the
    boundary were computed from UTC while the run-dir key is local, evenings
    after 17:00 local would see this module treat today's own directory as
    outside the band and act on a live run's directory."""
    today_name = TODAY.isoformat()
    _day(tmp_path, today_name)
    assert R.should_act(today_name, TODAY, R.THIN_DAYS) is False
    assert R.should_act(today_name, TODAY, R.PRUNE_DAYS) is False


# --- test 7: a dry run changes nothing and says what it would have done -----

def test_dry_run_style_inspection_changes_nothing(R, tmp_path):
    """Mirrors the CLI's --dry-run: compute candidates without calling
    apply_thin/apply_prune, and confirm the tree is untouched by the mere act
    of asking. The failing state that matters is a SILENT dry run — one that
    changes nothing but also cannot be told apart from a dry run over an empty
    candidate set — so this also asserts the candidate lists are non-empty."""
    doomed_day = TODAY - datetime.timedelta(days=R.PRUNE_DAYS + 5)
    day_dir = _day(tmp_path, doomed_day.isoformat())
    run_dir = _run_dir(day_dir, "run-eeeeeeee", _full_run_files())

    thin_doomed = [d.name for d in R.day_dir_candidates(tmp_path)
                   if R.should_act(d.name, TODAY, R.THIN_DAYS)]
    prune_doomed = [d.name for d in R.day_dir_candidates(tmp_path)
                    if R.should_act(d.name, TODAY, R.PRUNE_DAYS)]

    assert thin_doomed == [doomed_day.isoformat()]
    assert prune_doomed == [doomed_day.isoformat()]
    assert day_dir.exists()
    assert (run_dir / "transcript.jsonl").exists()


# --- test 8: the job reports counts on every pass, including a no-op --------

def test_run_reports_counts_even_on_a_no_op_pass(R, tmp_path):
    """Silence cannot be told from a job that never ran, never loaded, or
    crashed before its first delete — so the stats dict must always carry the
    counters, zero or not."""
    stats = R.run(tmp_path, TODAY)
    assert stats["thinned"]["day_dirs"] == 0
    assert stats["thinned"]["runs_thinned"] == 0
    assert stats["pruned"]["days_deleted"] == 0


# --- independence: one mechanism failing must not stop the other ------------

def test_a_failing_thin_pass_does_not_cancel_the_prune_pass(R, tmp_path, monkeypatch):
    """THE GUARANTEE THIS PROTECTS. If apply_thin raised outward, run() would
    never reach apply_prune, and one thinning defect would silently cancel the
    only mechanism that bounds directory COUNT (and therefore sweep cost)."""
    doomed = _day(tmp_path, (TODAY - datetime.timedelta(days=R.PRUNE_DAYS + 1)).isoformat())

    def explode(root, today, *, days=R.THIN_DAYS):
        raise RuntimeError("boom")
    monkeypatch.setattr(R, "apply_thin", explode)

    stats = R.run(tmp_path, TODAY)

    assert stats["thinned"]["errors"] == 1
    assert stats["pruned"]["days_deleted"] == 1
    assert not doomed.exists()


def test_a_failing_prune_pass_does_not_cancel_the_thin_pass(R, tmp_path, monkeypatch):
    old_day = TODAY - datetime.timedelta(days=R.THIN_DAYS + 1)
    day_dir = _day(tmp_path, old_day.isoformat())
    run_dir = _run_dir(day_dir, "run-ffffffff", _full_run_files())

    def explode(root, today, *, days=R.PRUNE_DAYS):
        raise RuntimeError("boom")
    monkeypatch.setattr(R, "apply_prune", explode)

    stats = R.run(tmp_path, TODAY)

    assert stats["pruned"]["errors"] == 1
    assert not (run_dir / "transcript.jsonl").exists()
    assert (run_dir / "meta.json").exists()


def test_one_hostile_run_dir_does_not_cancel_thinning_of_its_siblings(R, tmp_path, monkeypatch):
    old_day = TODAY - datetime.timedelta(days=R.THIN_DAYS + 1)
    day_dir = _day(tmp_path, old_day.isoformat())
    _run_dir(day_dir, "run-11111111", _full_run_files())
    good = _run_dir(day_dir, "run-22222222", _full_run_files())

    real_thin = R.thin_run_dir

    def flaky(run_dir):
        if run_dir.name == "run-11111111":
            raise PermissionError("nope")
        return real_thin(run_dir)
    monkeypatch.setattr(R, "thin_run_dir", flaky)

    stats = R.apply_thin(tmp_path, TODAY, days=R.THIN_DAYS)

    assert stats["errors"] == 1
    assert not (good / "transcript.jsonl").exists()


# --- boundary is exact, not a vibe -------------------------------------------

def test_thin_boundary_is_exact(R, tmp_path):
    """The band is closed: today - 35 is the OLDEST day still inside [today-35,
    today] and stays full; today - 36 is one day past it and gets thinned."""
    at = _day(tmp_path, (TODAY - datetime.timedelta(days=R.THIN_DAYS)).isoformat())
    outside = _day(tmp_path, (TODAY - datetime.timedelta(days=R.THIN_DAYS + 1)).isoformat())
    at_run = _run_dir(at, "run-33333333", _full_run_files())
    outside_run = _run_dir(outside, "run-44444444", _full_run_files())

    R.apply_thin(tmp_path, TODAY, days=R.THIN_DAYS)

    assert (at_run / "transcript.jsonl").exists()
    assert not (outside_run / "transcript.jsonl").exists()


def test_prune_boundary_is_exact(R, tmp_path):
    """The band is closed: today - 100 stays; today - 101 is pruned."""
    at = _day(tmp_path, (TODAY - datetime.timedelta(days=R.PRUNE_DAYS)).isoformat())
    outside = _day(tmp_path, (TODAY - datetime.timedelta(days=R.PRUNE_DAYS + 1)).isoformat())

    R.apply_prune(tmp_path, TODAY, days=R.PRUNE_DAYS)

    assert at.exists()
    assert not outside.exists()


# --- symlinked entries are skipped, never followed ---------------------------

def test_a_symlinked_day_directory_is_never_a_candidate(R, tmp_path):
    real = tmp_path / "_elsewhere"
    real.mkdir()
    link = tmp_path / "2020-01-01"
    link.symlink_to(real, target_is_directory=True)

    R.apply_prune(tmp_path, TODAY, days=R.PRUNE_DAYS)

    assert link.is_symlink()
    assert real.exists()


def test_a_symlinked_run_directory_inside_a_thinned_day_is_never_followed(R, tmp_path):
    old_day = TODAY - datetime.timedelta(days=R.THIN_DAYS + 1)
    day_dir = _day(tmp_path, old_day.isoformat())
    real = tmp_path / "_elsewhere_run"
    real.mkdir()
    (real / "transcript.jsonl").write_text("secret", encoding="utf-8")
    link = day_dir / "run-55555555"
    link.symlink_to(real, target_is_directory=True)

    R.apply_thin(tmp_path, TODAY, days=R.THIN_DAYS)

    assert link.is_symlink()
    assert (real / "transcript.jsonl").read_text() == "secret"


def test_run_is_safe_on_a_missing_root(R, tmp_path):
    stats = R.run(tmp_path / "nope", TODAY)
    assert stats["pruned"]["days_deleted"] == 0
    assert stats["thinned"]["day_dirs"] == 0


# --- a poisoned day directory must not cancel thinning for later days -------

def test_a_poisoned_day_directory_does_not_cancel_thinning_of_later_days(
        R, tmp_path, capsys):
    """THE DAY-DIRECTORY-LEVEL COUNTERPART of `test_one_hostile_run_dir_...`.

    Before the fix, `for run_dir in sorted(day_dir.iterdir()):` in `apply_thin`
    sat outside every try/except. Listing an unreadable day directory raises
    PermissionError there, which propagates out of `apply_thin` entirely and
    silently cancels thinning for every day directory later in the sorted
    order — not just the poisoned one. The poisoned day is named
    "...unreadable" so it sorts BEFORE the healthy day below.

    Confirmed this fails against the pre-fix code: `PermissionError` propagated
    out of `apply_thin`, the healthy day's `transcript.jsonl` survived
    (thinning never reached it), and `stats["errors"]` was never incremented
    since the exception aborted the function before returning `stats` at all.
    After the fix: `stats["errors"] == 1`, the healthy day's `transcript.jsonl`
    is gone, and the poisoned directory's own name is printed to stderr so the
    bare count is not the only signal.
    """
    poisoned_day = TODAY - datetime.timedelta(days=R.THIN_DAYS + 2)
    healthy_day = TODAY - datetime.timedelta(days=R.THIN_DAYS + 1)
    assert poisoned_day.isoformat() < healthy_day.isoformat(), (
        "the poisoned day must sort before the healthy one")

    poisoned = _day(tmp_path, poisoned_day.isoformat())
    healthy_dir = _day(tmp_path, healthy_day.isoformat())
    healthy_run = _run_dir(healthy_dir, "run-66666666", _full_run_files())

    os.chmod(poisoned, 0o000)
    try:
        stats = R.apply_thin(tmp_path, TODAY, days=R.THIN_DAYS)
    finally:
        os.chmod(poisoned, 0o700)

    assert stats["errors"] == 1
    assert not (healthy_run / "transcript.jsonl").exists(), (
        "the healthy day must still be thinned despite the poisoned one "
        "sorting before it")
    err = capsys.readouterr().err
    assert poisoned.name in err, "the poisoned directory must be named, not just counted"


# --- a symlinked CHILD of a run directory is unlinked, never followed -------

def test_a_symlinked_child_of_a_run_directory_is_unlinked_not_followed(R, tmp_path):
    """Distinct from the day-dir and run-dir symlink tests above: this plants
    the symlink one level DEEPER, inside an otherwise-real run directory, at
    the exact level `thin_run_dir` iterates. Asserts the target's contents
    survive, not just that the link itself is gone — a bug that followed the
    symlink and deleted through it would still leave the link entry absent."""
    old_day = TODAY - datetime.timedelta(days=R.THIN_DAYS + 1)
    day_dir = _day(tmp_path, old_day.isoformat())
    run_dir = _run_dir(day_dir, "run-77777777", _full_run_files())

    target = tmp_path / "_elsewhere_prompts"
    target.mkdir()
    (target / "0.txt").write_text("do not delete me", encoding="utf-8")
    link = run_dir / "prompts"
    link.symlink_to(target, target_is_directory=True)

    R.apply_thin(tmp_path, TODAY, days=R.THIN_DAYS)

    assert not link.exists(), "the symlink entry itself must be gone"
    assert not link.is_symlink()
    assert target.exists()
    assert (target / "0.txt").read_text() == "do not delete me"


# --- the CLI itself: bin/run-retention, not the library ---------------------
#
# Every test above calls R.apply_thin / R.apply_prune / R.run directly against
# tmp_path. This one runs the real CLI, against a fake HOME, to confirm the
# script it actually is still works end to end.
#
# bin/run-retention used to also accept a --root flag, guarded by three
# rounds of containment checks (leaf name, then parent containment, then a
# system-temp-directory allowance) — and every one of those checks was itself
# only exercised by tests of the flag. The flag had no caller: the launchd
# job passes no arguments, and nothing else in the repo invokes the script
# with --root. Round 5 removed it rather than adding a fourth check, after
# the round-4 temp-directory allowance turned out to trust TMPDIR, an
# environment variable the invoking process — not this script — controls.


def _cli_env(tmp_home: Path) -> dict:
    """Env for invoking `bin/run-retention` as a subprocess against a fake HOME.

    `bin/run-retention` resolves its modules via `~/.local/lib/agent-platform`,
    the symlink farm `install.sh` lays down, absent here — so PYTHONPATH points
    at this checkout's own `lib/agent-platform` instead (the convention in
    `test_cli_contract_check._subprocess_env`). HOME is overridden so RUNS_ROOT
    (computed from `Path.home()`) lands in a scratch tree rather than the real
    `~/Convivy/agent-runs` this test must never touch.
    """
    env = dict(os.environ, HOME=str(tmp_home))
    lib = str(LIB)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{lib}{os.pathsep}{existing}" if existing else lib
    return env


def test_cli_dry_run_reports_against_the_default_root(tmp_path):
    """The failure mode that matters most for an unattended 04:40 job: the CLI
    must still run its default invocation cleanly, with no flags beyond
    --dry-run, against a HOME it computes RUNS_ROOT from itself."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    proc = subprocess.run(
        [sys.executable, str(RUN_RETENTION_BIN), "--dry-run"],
        capture_output=True, text=True, env=_cli_env(fake_home))

    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "dry run" in proc.stdout
