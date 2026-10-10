"""`run-observer` — reporting runs that died without ever finalizing.

This sweep's whole value is precision. The raw population it draws from is huge and
almost entirely healthy: `registry`'s stale log holds 1,414 distinct run ids over
July-August 2026 while only 20 of those runs actually failed to finalize, so a
report keyed on "was reaped" would be about 98.6% noise. Most of these tests are
therefore about what it must NOT report.

The one thing it must catch is the case nothing else can see: a process that
disappeared, leaving a transcript and no `outcome.json`, so no in-process code ever
ran to classify it.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def obs(run_observer):
    return run_observer


NOW = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _run(root, run_id, *, day="2026-08-25", agent="reviewer", repo="vivy",
         pr=None, pid=999999, started=None, outcome=True, lines=3, meta=True,
         extra_meta=None):
    """A run directory shaped like a real one.

    `pid=999999` is a pid nothing owns, so the run reads as dead unless a test
    says otherwise — the ordinary case for a finished or abandoned run.
    """
    d = root / day / run_id
    d.mkdir(parents=True)
    if meta:
        body = {
            "run_id": run_id, "agent": agent, "repo": repo, "pr": pr, "pid": pid,
            "started_at": _iso(started or (NOW - timedelta(hours=6))),
        }
        body.update(extra_meta or {})
        (d / "meta.json").write_text(json.dumps(body))
    if outcome:
        (d / "outcome.json").write_text(json.dumps({"exit_status": "completed"}))
    (d / "transcript.jsonl").write_text("".join('{"type":"system"}\n' for _ in range(lines)))
    return d


def _sweep(obs, monkeypatch, tmp_path, *, days=30, dry_run=True, alerts=None):
    monkeypatch.setattr(obs, "RUNS_ROOT", tmp_path)
    if alerts is not None:
        monkeypatch.setattr(obs, "post_alert",
                            lambda stream, body, **kw: alerts.append((body, kw.get("dedup_key"))))
    argv = ["run-observer"] + ([] if days is None else ["--days", str(days)])
    monkeypatch.setattr(obs.sys, "argv", argv + (["--dry-run"] if dry_run else []))
    monkeypatch.setattr(obs, "datetime", _FrozenDatetime)
    return obs.main()


class _FrozenDatetime(datetime):
    """`now(tz)` pinned so the grace-period arithmetic is deterministic."""
    @classmethod
    def now(cls, tz=None):
        return NOW


def _age_dir(d, seconds):
    """Backdate everything in a run dir so it reads as quiet."""
    import os
    when = NOW.timestamp() - seconds
    for child in d.iterdir():
        os.utime(child, (when, when))



# --- what it must catch ---------------------------------------------------------

def test_a_run_that_never_finalized_is_reported(obs, tmp_path, monkeypatch, capsys):
    """The case nothing else can see. Built from `run-af1adcd7`: a security-reviewer
    on agent-platform#398 that did 444 transcript lines and then vanished, leaving no
    exit status, no cost record and no terminal reason."""
    _run(tmp_path, "run-af1adcd7", agent="security-reviewer", repo="agent-platform",
         pr=398, outcome=False, lines=444)
    rc = _sweep(obs, monkeypatch, tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "run-af1adcd7" in out
    assert "agent-platform#398" in out
    assert "444 transcript lines" in out, (
        "how much work it had done is what makes the finding actionable")


def test_the_finding_says_how_much_work_was_lost(obs, tmp_path, monkeypatch, capsys):
    """A run that died at startup and one that died after 400 turns are different
    facts, and a reader decides differently about them."""
    _run(tmp_path, "run-11111111", outcome=False, lines=2)
    _run(tmp_path, "run-22222222", outcome=False, lines=412)
    _sweep(obs, monkeypatch, tmp_path)
    out = capsys.readouterr().out
    assert "2 transcript lines" in out
    assert "412 transcript lines" in out


# --- what it must NOT report ----------------------------------------------------

def test_a_finalized_run_is_never_reported(obs, tmp_path, monkeypatch, capsys):
    """The 1,394 stale-log entries that finished normally. An outcome.json is the
    whole discriminator; without this the sweep is 98.6% noise."""
    _run(tmp_path, "run-33333333", outcome=True)
    _sweep(obs, monkeypatch, tmp_path)
    assert "run-33333333" not in capsys.readouterr().out


def test_a_run_still_in_flight_is_never_reported(obs, tmp_path, monkeypatch, capsys):
    """Accusing a live dispatch would be worse than missing a dead one — it would
    make the report untrustworthy for the case it exists to serve."""
    _run(tmp_path, "run-44444444", outcome=False, pid=os.getpid())
    _sweep(obs, monkeypatch, tmp_path)
    assert "run-44444444" not in capsys.readouterr().out


def test_a_run_inside_the_grace_period_is_never_reported(obs, tmp_path, monkeypatch, capsys):
    """A pid can be recycled onto an unrelated process, and a just-started run is
    the case where that misreads worst. The grace costs one sweep's delay."""
    _run(tmp_path, "run-55555555", outcome=False,
         started=NOW - timedelta(minutes=5))
    _sweep(obs, monkeypatch, tmp_path)
    assert "run-55555555" not in capsys.readouterr().out


def test_a_subagent_record_is_never_reported(obs, tmp_path, monkeypatch, capsys):
    """`sub-*` directories are Task-tool subagent records — a different shape that
    never writes an outcome.json at all. 358 of them appeared in one survey of
    "runs with no closing message", and every one was healthy. Without this filter
    the sweep would report them all, every night, forever."""
    d = tmp_path / "2026-08-25" / "sub-a2f72912bb8aa2b30"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps(
        {"agent": "general-purpose", "pid": 999999,
         "started_at": _iso(NOW - timedelta(hours=6))}))
    _sweep(obs, monkeypatch, tmp_path)
    assert "sub-a2f72912bb8aa2b30" not in capsys.readouterr().out


def test_a_run_outside_the_window_is_not_reported(obs, tmp_path, monkeypatch, capsys):
    _run(tmp_path, "run-66666666", day="2026-01-02", outcome=False,
         started=datetime(2026, 1, 2, tzinfo=timezone.utc))
    _sweep(obs, monkeypatch, tmp_path, days=7)
    assert "run-66666666" not in capsys.readouterr().out


def test_a_run_whose_meta_is_missing_entirely_is_reported(
        obs, tmp_path, monkeypatch, capsys):
    """Deleting your own meta.json must not be a way to vanish.

    This used to skip, on the reasoning that a dir with no meta was never really a
    run. But an agent can delete its own meta as easily as it can corrupt it, and
    the two must not have different outcomes — that is how the last three
    suppression holes each survived a round.
    """
    d = _run(tmp_path, "run-77777777", outcome=False, meta=False)
    _age_dir(d, 6 * 3600)
    _sweep(obs, monkeypatch, tmp_path)
    assert "run-77777777" in capsys.readouterr().out


# --- suppression lives server-side, not on this filesystem ----------------------

def test_each_finding_carries_a_stable_per_run_dedup_key(obs, tmp_path, monkeypatch):
    """Suppression is the Library's `dedup_key`, and there is deliberately no local
    seen-list.

    The obvious design — a seen-file beside the runs — is a suppression primitive
    sitting in the tree it audits. `agent-runs` is writable by every dispatched
    agent (same OS user, shell access), and any agent can list a sibling's run id,
    so any agent could append one and hide that run's abandonment forever. It would
    defeat this sweep using precisely the actor class it exists to catch.

    So the key must be per-run and STABLE across sweeps: the server returns the
    existing row on a repeat and no second alert lands, and that store is not on a
    filesystem the suppressed party can write.
    """
    _run(tmp_path, "run-88888888", outcome=False)
    first, second = [], []
    _sweep(obs, monkeypatch, tmp_path, dry_run=False, alerts=first)
    _sweep(obs, monkeypatch, tmp_path, dry_run=False, alerts=second)
    assert len(first) == 1 and len(second) == 1
    assert first[0][1] == second[0][1], (
        "the key must be identical across sweeps or the server cannot suppress it")
    assert "run-88888888" in first[0][1]


def test_two_findings_get_different_keys(obs, tmp_path, monkeypatch):
    """Per run, not per batch. A batch key would withhold a NEW finding whenever it
    happened to share a key with an older one."""
    _run(tmp_path, "run-99999999", outcome=False)
    _run(tmp_path, "run-aaaaaaaa", outcome=False)
    alerts = []
    _sweep(obs, monkeypatch, tmp_path, dry_run=False, alerts=alerts)
    keys = {a[1] for a in alerts}
    assert len(alerts) == 2 and len(keys) == 2


def test_no_suppression_state_is_written_to_the_audited_tree(obs, tmp_path, monkeypatch):
    """Nothing this sweep writes may be usable to silence it."""
    _run(tmp_path, "run-88888888", outcome=False)
    before = {p.name for p in tmp_path.iterdir()}
    _sweep(obs, monkeypatch, tmp_path, dry_run=False, alerts=[])
    assert {p.name for p in tmp_path.iterdir()} == before


def test_dry_run_alerts_nothing(obs, tmp_path, monkeypatch):
    _run(tmp_path, "run-88888888", outcome=False)
    alerts = []
    _sweep(obs, monkeypatch, tmp_path, dry_run=True, alerts=alerts)
    assert alerts == []


# --- the tree it reads is agent-writable ----------------------------------------

def test_a_directory_name_that_is_not_the_minted_shape_is_ignored(
        obs, tmp_path, monkeypatch, capsys):
    """The name is displayed in a log a human tails, and it is attacker-chosen text:
    any dispatched agent can create a directory here. `telemetry` only ever mints
    `run-<8 lowercase hex>`, so anything else is not a run."""
    hostile = "run-\x1b[2K\x1b[1Aforged"
    d = tmp_path / "2026-08-25" / hostile
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps(
        {"agent": "reviewer", "pid": 999999,
         "started_at": _iso(NOW - timedelta(hours=6))}))
    _sweep(obs, monkeypatch, tmp_path)
    out = capsys.readouterr().out
    assert "\x1b" not in out and "forged" not in out


def test_a_symlinked_run_directory_is_not_followed(obs, tmp_path, monkeypatch, capsys):
    """A symlinked run dir would let the sweep read, and report, a file outside the
    tree it audits."""
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "meta.json").write_text(json.dumps(
        {"agent": "reviewer", "pid": 999999,
         "started_at": _iso(NOW - timedelta(hours=6))}))
    day = tmp_path / "2026-08-25"
    day.mkdir()
    (day / "run-bbbbbbbb").symlink_to(outside, target_is_directory=True)
    _sweep(obs, monkeypatch, tmp_path)
    assert "run-bbbbbbbb" not in capsys.readouterr().out


def test_a_symlinked_transcript_is_not_read(obs, tmp_path, monkeypatch, capsys):
    """A link to /dev/zero never yields a line terminator, so an unbounded read
    would hang the unattended sweep with nothing to say why. `write_owner_only`
    already treats a same-user symlink as a threat on the write side."""
    d = _run(tmp_path, "run-cccccccc", outcome=False, lines=0)
    (d / "transcript.jsonl").unlink()
    (d / "transcript.jsonl").symlink_to("/dev/zero")
    _sweep(obs, monkeypatch, tmp_path)
    out = capsys.readouterr().out
    assert "run-cccccccc" in out, "the run is still a finding"
    assert "UNREADABLE" in out, "a refused read must not be laundered into '0 lines'"


def test_an_enormous_transcript_does_not_read_forever(obs, tmp_path, monkeypatch):
    """Bounded rather than exact. The count exists to say how much work was lost,
    and a cap answers that as well as a precise figure would."""
    d = _run(tmp_path, "run-dddddddd", outcome=False, lines=0)
    monkeypatch.setattr(obs, "_MAX_TRANSCRIPT_BYTES", 200)
    (d / "transcript.jsonl").write_text("".join('{"x":1}\n' for _ in range(10_000)))
    lines = obs._transcript_lines(d)
    assert 0 < lines < 10_000, f"read was not bounded: {lines}"


def test_every_meta_field_that_is_displayed_is_sanitised(obs, tmp_path, monkeypatch, capsys):
    """meta.json sits at AGENTCTL_RUN_DIR, exported into the agent's own
    environment, so a run with shell access can write its own before it dies.

    `started_at` was interpolated raw while its neighbours were escaped — a rule
    stated and then not fully applied, which is worse than no rule because the next
    reader trusts the fields that look guarded.
    """
    d = tmp_path / "2026-08-25" / "run-eeeeeeee"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({
        "agent": "reviewer\x1b[2Kforged-agent",
        "repo": "vivy\x1b[1Aforged-repo",
        "pid": 999999,
        "started_at": "2026-08-25T00:00:00Z\x1b[2Kforged-date",
    }))
    (d / "transcript.jsonl").write_text('{"type":"system"}\n')
    _sweep(obs, monkeypatch, tmp_path)
    out = capsys.readouterr().out
    assert "run-eeeeeeee" in out, "the run is still reported"
    assert "\x1b" not in out, "an escape sequence reached the log a human tails"


def test_a_dry_run_that_lost_the_concurrency_race_is_not_reported(
        obs, agentctl, tmp_path, monkeypatch, capsys):
    """The one legitimate path that used to leave a run with no outcome.

    `agentctl spawn --dry-run` wrote meta and returned bare when it lost the
    registry race, producing a directory indistinguishable from a run whose
    process vanished. It now records `capacity_refused` like every other path
    that gives up after write_meta, so this sweep does not accuse it.
    """
    d = tmp_path / "2026-08-25" / "run-ffffffff"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps(
        {"agent": "coder", "repo": "vivy", "pid": 999999,
         "started_at": _iso(NOW - timedelta(hours=6))}))
    import telemetry as T
    T.write_outcome(d, exit_status="capacity_refused",
                    error_reason="lost the concurrency race — dry run")
    _sweep(obs, monkeypatch, tmp_path)
    assert "run-ffffffff" not in capsys.readouterr().out


def test_a_corrupt_start_time_reports_rather_than_hides_the_run(
        obs, tmp_path, monkeypatch, capsys):
    """meta.json is writable by the run itself, so skipping on an unreadable
    `started_at` would hand every agent a one-field suppression switch: corrupt
    your own start time and never be reported. Every ambiguity here resolves
    toward reporting."""
    d = tmp_path / "2026-08-25" / "run-12121212"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps(
        {"agent": "coder", "repo": "vivy", "pid": 999999, "started_at": "not-a-date"}))
    (d / "transcript.jsonl").write_text('{"type":"system"}\n')
    _sweep(obs, monkeypatch, tmp_path)
    assert "run-12121212" in capsys.readouterr().out


def test_a_future_start_time_reports_rather_than_hides_the_run(
        obs, tmp_path, monkeypatch, capsys):
    """The same suppression switch as the corrupt timestamp, one step over — and
    strictly easier to use, because nothing about it looks malformed.

    A valid ISO timestamp dated 2099 makes `now - started` NEGATIVE, which is
    `< GRACE` for any grace period, so a one-sided window skipped the run forever.
    A run cannot have started after now, so a negative age is not "just started",
    it is a lie — and the answer to a lie is to report it.
    """
    d = tmp_path / "2026-08-25" / "run-13131313"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps(
        {"agent": "reviewer", "repo": "vivy", "pid": 999999,
         "started_at": "2099-01-01T00:00:00Z"}))
    (d / "transcript.jsonl").write_text('{"type":"system"}\n')
    _sweep(obs, monkeypatch, tmp_path)
    assert "run-13131313" in capsys.readouterr().out


def test_a_transcript_with_no_newlines_is_still_bounded(obs, tmp_path, monkeypatch):
    """Iterating a file yields one line per readline(), and readline() reads to a
    newline OR EOF — so a cap checked between lines never fires on a file with no
    newline at all. A single-line multi-gigabyte transcript was read whole before
    the loop body ran once. Chunked reading bounds it regardless of content."""
    d = _run(tmp_path, "run-14141414", outcome=False, lines=0)
    monkeypatch.setattr(obs, "_MAX_TRANSCRIPT_BYTES", 1000)
    monkeypatch.setattr(obs, "_READ_CHUNK", 256)
    (d / "transcript.jsonl").write_bytes(b"x" * 5_000_000)   # not one newline
    assert obs._transcript_lines(d) == 0, "no newlines, so no lines"


def test_the_transcript_read_does_not_follow_a_symlink(obs, tmp_path, monkeypatch):
    """O_NOFOLLOW rather than stat-then-open: a check followed by an open leaves a
    window to swap the file for a link to /dev/zero, which is the hang the check
    exists to prevent. The atomic form has no window."""
    d = _run(tmp_path, "run-15151515", outcome=False, lines=0)
    (d / "transcript.jsonl").unlink()
    (d / "transcript.jsonl").symlink_to("/dev/zero")
    # None, not 0 — refusing to follow is a REFUSAL, and a symlink there is a
    # deliberate act. Reporting it as "0 lines" would launder that into a fact.
    assert obs._transcript_lines(d) is None


def test_a_symlinked_meta_is_not_read(obs, tmp_path, monkeypatch, capsys):
    """Same rule on the meta read."""
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(
        {"agent": "reviewer", "repo": "vivy", "pid": 999999,
         "started_at": _iso(NOW - timedelta(hours=6))}))
    d = tmp_path / "2026-08-25" / "run-16161616"
    d.mkdir(parents=True)
    (d / "meta.json").symlink_to(outside)
    (d / "transcript.jsonl").write_text('{"type":"system"}\n')
    _age_dir(d, 6 * 3600)
    _sweep(obs, monkeypatch, tmp_path)
    out = capsys.readouterr().out
    # Refused, so the run reports with unknown fields rather than borrowing the
    # linked file's — a symlinked meta is suspicious and must not be a way to hide.
    assert "run-16161616" in out
    assert "meta unreadable" in out


def test_an_enormous_meta_is_bounded(obs, tmp_path, monkeypatch, capsys):
    """meta.json is agent-writable too; `read_text()` would load a disk-sized one
    whole. Bounded, and an unparseable truncation simply skips the run."""
    d = tmp_path / "2026-08-25" / "run-17171717"
    d.mkdir(parents=True)
    monkeypatch.setattr(obs, "_MAX_META_BYTES", 128)
    (d / "meta.json").write_text(json.dumps(
        {"agent": "reviewer", "repo": "vivy", "pid": 999999, "pad": "z" * 50_000,
         "started_at": _iso(NOW - timedelta(hours=6))}))
    (d / "transcript.jsonl").write_text('{"type":"system"}\n')
    _age_dir(d, 6 * 3600)
    _sweep(obs, monkeypatch, tmp_path)
    out = capsys.readouterr().out
    # Bounded AND still reported: the cap must not become a third way to disappear.
    assert "run-17171717" in out
    assert "meta unreadable" in out


@pytest.mark.parametrize("stamp", ["2026-08-25T00:00:00", "2026-08-25 00:00:00",
                                   "2026-08-25"])
def test_a_naive_start_time_does_not_crash_the_whole_sweep(
        obs, tmp_path, monkeypatch, capsys, stamp):
    """`fromisoformat` parses these into NAIVE datetimes, and subtracting a naive
    from an aware `now` raises TypeError — uncaught, in a loop with no per-run
    guard, so it takes the entire sweep down rather than skipping one run.

    `meta.json` is agent-writable, which makes that a one-field kill switch on the
    nightly job: worse than hiding a single run, because it hides every run at once
    and leaves a traceback nobody reads. Found by enumerating started_at values
    rather than by a review.
    """
    d = tmp_path / "2026-08-25" / "run-18181818"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps(
        {"agent": "reviewer", "repo": "vivy", "pid": 999999, "started_at": stamp}))
    (d / "transcript.jsonl").write_text('{"type":"system"}\n')
    # A healthy run alongside it, so a crash cannot masquerade as "nothing found".
    _run(tmp_path, "run-19191919", outcome=False)
    rc = _sweep(obs, monkeypatch, tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "run-18181818" in out, "an unusable start time must report, not vanish"
    assert "run-19191919" in out, "the sweep must survive to reach the next run"


def test_an_unparseable_meta_is_reported_not_silently_dropped(
        obs, tmp_path, monkeypatch, capsys):
    """The widest suppression switch of all, and skipping contradicted this file's
    own rule that every ambiguity resolves toward reporting.

    A dying agent only had to leave its own meta.json syntactically invalid — omit
    a closing brace — and it was never reported, whatever its pid said and however
    much work its transcript held.
    """
    d = tmp_path / "2026-08-25" / "run-1a1a1a1a"
    d.mkdir(parents=True)
    (d / "meta.json").write_text('{"agent": "reviewer", "pid": 999999, "started_a')
    (d / "transcript.jsonl").write_text("".join('{"t":1}\n' for _ in range(400)))
    _age_dir(d, 6 * 3600)
    _sweep(obs, monkeypatch, tmp_path)
    out = capsys.readouterr().out
    assert "run-1a1a1a1a" in out
    assert "meta unreadable" in out, "the reader must be told why the fields are absent"
    assert "400 transcript lines" in out, "the lost work is still the actionable fact"


def test_an_unparseable_meta_on_a_STILL_ACTIVE_run_is_not_reported(
        obs, tmp_path, monkeypatch, capsys):
    """`write_owner_only` is O_TRUNC then write, not atomic, so a LIVE run
    rewriting its meta has a window of partial content. Reporting on sight would
    accuse a healthy run mid-write, so staleness falls back to when the directory
    was last written — a signal that does not depend on the file's contents."""
    d = tmp_path / "2026-08-25" / "run-1b1b1b1b"
    d.mkdir(parents=True)
    (d / "meta.json").write_text('{"agent": "reviewer", "pid": 999999, "started_a')
    (d / "transcript.jsonl").write_text('{"t":1}\n')
    _age_dir(d, 60)          # written a minute ago: still going
    _sweep(obs, monkeypatch, tmp_path)
    assert "run-1b1b1b1b" not in capsys.readouterr().out


def test_a_meta_truncated_by_the_size_cap_is_still_reported(
        obs, tmp_path, monkeypatch, capsys):
    """The cap turns an over-large meta into invalid JSON, which must not become a
    third way to disappear."""
    d = tmp_path / "2026-08-25" / "run-1c1c1c1c"
    d.mkdir(parents=True)
    monkeypatch.setattr(obs, "_MAX_META_BYTES", 64)
    (d / "meta.json").write_text(json.dumps(
        {"agent": "reviewer", "repo": "vivy", "pid": 999999, "pad": "z" * 50_000,
         "started_at": _iso(NOW - timedelta(hours=6))}))
    (d / "transcript.jsonl").write_text('{"t":1}\n')
    _age_dir(d, 6 * 3600)
    _sweep(obs, monkeypatch, tmp_path)
    assert "run-1c1c1c1c" in capsys.readouterr().out


def test_a_meta_less_dir_that_never_RAN_is_not_reported(obs, tmp_path, monkeypatch, capsys):
    """Reporting a meta-less directory unconditionally added 40 findings on the real
    tree with no lost work in any of them — every one a run that never started,
    holding only `prompts/` and `started_head.txt`, sometimes nothing at all.

    A transcript with content is the evidence something happened worth recovering.
    The boundary is real rather than a loophole: an agent could delete its
    transcript too, but then the directory holds no work, nothing is recoverable,
    and a report would name a run about which nothing can be said — an agent that
    removes its transcript has thrown away the very thing it was protecting.
    """
    d = tmp_path / "2026-08-25" / "run-1d1d1d1d"
    d.mkdir(parents=True)
    (d / "prompts").mkdir()
    (d / "started_head.txt").write_text("abc123\n")
    _age_dir(d, 6 * 3600)
    _sweep(obs, monkeypatch, tmp_path)
    assert "run-1d1d1d1d" not in capsys.readouterr().out


def test_a_run_that_DID_work_and_lost_its_meta_is_still_reported(
        obs, tmp_path, monkeypatch, capsys):
    """The half that must not regress: the suppression hole stays closed for any
    run whose transcript shows it did something."""
    d = tmp_path / "2026-08-25" / "run-1e1e1e1e"
    d.mkdir(parents=True)
    (d / "transcript.jsonl").write_text("".join('{"t":1}\n' for _ in range(300)))
    _age_dir(d, 6 * 3600)
    _sweep(obs, monkeypatch, tmp_path)
    out = capsys.readouterr().out
    assert "run-1e1e1e1e" in out
    assert "300 transcript lines" in out


@pytest.mark.parametrize("offset,expect", [
    (-6 * 3600, True),      # stale — reports
    (-60, False),           # written a minute ago — still going
    (-3600, True),          # exactly GRACE — half-open, reports
    (+3600, True),          # an hour in the FUTURE — impossible, so evidence
    (+2_300_000_000, True), # year 2099
])
def test_the_mtime_fallback_is_bounded_at_both_ends(obs, tmp_path, offset, expect):
    """The fourth appearance of one bug in this file, and I wrote this one too.

    started_at unreadable, started_at in the future, meta unparseable, and now a
    future mtime: each time a comparison correct for the values I pictured and
    silently permissive for the ones I did not. A future mtime makes the age
    negative, `age >= GRACE` is False, and one `os.utime` call suppresses the run
    forever — without even needing the meta to be readable.

    A file cannot be written after now, so a negative age is evidence rather than
    absence.
    """
    import os
    d = tmp_path / "run-1f1f1f1f"
    d.mkdir()
    (d / "transcript.jsonl").write_text('{"t":1}\n')
    ts = NOW.timestamp() + offset
    os.utime(d / "transcript.jsonl", (ts, ts))
    assert obs._quiet_for_longer_than_grace(d, NOW) is expect


def test_a_chmod_000_transcript_cannot_hide_a_meta_less_run(
        obs, tmp_path, monkeypatch, capsys):
    """The fifth instance of one bug, and the cheapest yet.

    `_transcript_lines` returned 0 for BOTH "absent" and "unreadable", and the
    meta-less gate short-circuits on 0 — so `chmod 000` on its own transcript, one
    syscall, made a run vanish forever with its evidence still on disk, merely
    withheld from a sweep running as the same unprivileged user that locked it.

    Deletion is genuinely different and still is not reported: it destroys the
    evidence, so a report would name a run about which nothing can be said.
    Concealment leaves it intact. Only the first justifies silence.
    """
    import os as _os
    d = tmp_path / "2026-08-25" / "run-2a2a2a2a"
    d.mkdir(parents=True)
    (d / "meta.json").write_text('{"agent": "reviewer", "started_a')   # unparseable
    (d / "transcript.jsonl").write_text("".join('{"t":1}\n' for _ in range(444)))
    _age_dir(d, 6 * 3600)
    _os.chmod(d / "transcript.jsonl", 0o000)
    try:
        _sweep(obs, monkeypatch, tmp_path)
        out = capsys.readouterr().out
        assert "run-2a2a2a2a" in out, "a withheld transcript must not hide the run"
        assert "UNREADABLE" in out
    finally:
        _os.chmod(d / "transcript.jsonl", 0o600)


def test_an_unlistable_run_directory_reports(obs, tmp_path, monkeypatch):
    """"I could not look" is not a different state from "I looked and it was
    ambiguous" under this threat model — it is one the adversary manufactures with
    a chmod, as cheaply as any other. It gets the same answer: report."""
    import os as _os
    d = tmp_path / "2026-08-25" / "run-2b2b2b2b"
    d.mkdir(parents=True)
    (d / "transcript.jsonl").write_text('{"t":1}\n')
    _os.chmod(d, 0o000)
    try:
        assert obs._quiet_for_longer_than_grace(d, NOW) is True
    finally:
        _os.chmod(d, 0o700)


@pytest.mark.parametrize("body,reported", [
    ("", True),                                   # touch outcome.json — the bypass
    ("not json at all", True),                    # garbage
    ("{}", True),                                 # parses, no exit_status
    ('{"exit_status": ""}', True),                # present but empty
    ('{"exit_status": 7}', True),                 # present, wrong type
    ('{"exit_status": "completed"}', False),      # a real finalized run
    ('{"exit_status": "errored"}', False),
])
def test_the_outcome_file_is_parsed_not_merely_counted(
        obs, tmp_path, monkeypatch, capsys, body, reported):
    """`touch outcome.json` was a complete, one-command bypass of the whole sweep.

    Existence alone was the discriminator and the file was never read, so any run
    could mark itself finalized with an empty file and never be reported again.
    This predates every other fix here and is the sixth appearance of one shape: a
    thing being PRESENT treated as equivalent to that thing being VALID.

    `write_outcome` refuses to write without a valid `exit_status`, so anything
    lacking one is an unfinalized run with a file next to it.
    """
    d = _run(tmp_path, "run-2c2c2c2c", outcome=False)
    (d / "outcome.json").write_text(body)
    _age_dir(d, 6 * 3600)
    _sweep(obs, monkeypatch, tmp_path)
    out = capsys.readouterr().out
    assert ("run-2c2c2c2c" in out) is reported


def test_a_run_finalizing_right_now_is_not_accused(obs, tmp_path, monkeypatch, capsys):
    """`write_owner_only` is O_TRUNC-then-write and NOT atomic, so a run finalizing
    at this instant has a window where outcome.json is present and partial.

    A partial outcome must not report on sight — it falls through to the ordinary
    pid and staleness checks, which correctly identify it as still running."""
    d = _run(tmp_path, "run-2d2d2d2d", outcome=False, pid=os.getpid())
    (d / "outcome.json").write_text('{"exit_st')     # caught mid-write
    _sweep(obs, monkeypatch, tmp_path)
    assert "run-2d2d2d2d" not in capsys.readouterr().out


@pytest.mark.parametrize("pid", [0, -1, -999])
def test_a_non_positive_pid_does_not_read_as_alive(obs, pid):
    """`os.kill` gives 0 and negatives process-GROUP meaning — `kill(0, 0)` targets
    the caller's own group, `kill(-1, 0)` every process the user may signal. Both
    succeed, so both used to read as "alive" and suppress the run forever from one
    field in an agent-writable file. `int(pid)` accepted them without comment.

    A pid names one process. Anything else is not a liveness claim.
    """
    assert obs._alive(pid) is False


def test_an_enormous_pid_does_not_crash_the_sweep(obs):
    """OverflowError from os.kill was uncaught, so it took the whole sweep down
    rather than skipping one run — the same shape as the naive-datetime crash."""
    assert obs._alive(2 ** 62) is False


@pytest.mark.parametrize("pid", ["abc", None, 3.7, "", [], "1", True, 1.0])
def test_an_unusable_pid_reports(obs, pid):
    """Every ambiguity in this file resolves toward reporting.

    `3.7` is the interesting one and CI found it. `int(3.7)` is 3 — a perfectly
    plausible pid belonging to some unrelated process — so a malformed float was
    silently truncated into a liveness claim. CI's container has a pid 3 and this
    laptop does not, so the assertion's result depended on the machine: the tell
    that the value was never really being read, only coerced until it looked like
    an answer.

    `True` is an `int` subclass and would arrive as pid 1 the same way.
    """
    assert obs._alive(pid) is False


def test_a_run_parked_in_an_old_day_directory_is_still_found(
        obs, tmp_path, monkeypatch, capsys):
    """The day directory's NAME is not evidence about the run inside it.

    Nothing ties the two together and `agent-runs` is agent-writable, so moving a
    run into an old, already-past-cutoff day directory dropped it before any other
    check ran — a third independent way to vanish, needing only the directory move
    the threat model already grants. A trusted bucketing key whose bucket the
    adversary chooses is not a filter.
    """
    d = _run(tmp_path, "run-2e2e2e2e", day="2019-01-01", outcome=False,
             started=datetime(2019, 1, 1, tzinfo=timezone.utc))
    _age_dir(d, 6 * 3600)
    _sweep(obs, monkeypatch, tmp_path, days=None)
    assert "run-2e2e2e2e" in capsys.readouterr().out


def test_days_still_narrows_when_a_human_asks_for_it(obs, tmp_path, monkeypatch, capsys):
    """The flag survives for ad-hoc use — it is just no longer the default, and no
    longer load-bearing for correctness."""
    _run(tmp_path, "run-2f2f2f2f", day="2019-01-01", outcome=False,
         started=datetime(2019, 1, 1, tzinfo=timezone.utc))
    _sweep(obs, monkeypatch, tmp_path, days=7)
    assert "run-2f2f2f2f" not in capsys.readouterr().out


def test_a_symlinked_day_directory_is_not_walked(obs, tmp_path, monkeypatch, capsys):
    """The same reason the run directory refuses one, applied one level up."""
    outside = tmp_path / "elsewhere"
    (outside / "run-30303030").mkdir(parents=True)
    (outside / "run-30303030" / "meta.json").write_text(json.dumps(
        {"agent": "reviewer", "repo": "vivy", "pid": 999999,
         "started_at": _iso(NOW - timedelta(hours=6))}))
    (tmp_path / "2026-08-24").symlink_to(outside, target_is_directory=True)
    _sweep(obs, monkeypatch, tmp_path, days=None)
    assert "run-30303030" not in capsys.readouterr().out


@pytest.mark.parametrize("agent", [{"nested": "object"}, [1, 2], 7, True, 3.5])
def test_a_wrong_typed_agent_field_does_not_kill_the_sweep(
        obs, tmp_path, monkeypatch, capsys, agent):
    """`or "?"` catches FALSY values, not wrong-typed ones.

    A non-empty dict, list or number sailed through and reached `sanitise`, which
    raises TypeError on anything not str or bytes — taking the whole sweep down
    with it, and every alert for every other run found that day. The irony is
    exact: this file exists to catch agents that die unreported, and one field it
    did not coerce let a dying agent blind the catcher for everything beside it.

    A healthy run sits alongside so a crash cannot masquerade as "nothing found".
    """
    d = tmp_path / "2026-08-25" / "run-31313131"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps(
        {"agent": agent, "repo": "vivy", "pid": 999999,
         "started_at": _iso(NOW - timedelta(hours=6))}))
    (d / "transcript.jsonl").write_text('{"t":1}\n')
    _run(tmp_path, "run-32323232", outcome=False)
    rc = _sweep(obs, monkeypatch, tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "run-31313131" in out
    assert "run-32323232" in out, "the sweep must survive to reach the next run"


def test_one_unexaminable_run_costs_only_itself(obs, tmp_path, monkeypatch, capsys):
    """The structural guard, as distinct from the specific fixes.

    Every crash found in this file — a naive datetime, an overflowing pid, a
    wrong-typed agent — was one hostile field killing the whole batch. Each was
    fixed where it occurred. This makes the NEXT one, whatever it turns out to be,
    cost a single run instead of the sweep.
    """
    boom = _run(tmp_path, "run-33333334", outcome=False)
    healthy = _run(tmp_path, "run-34343434", outcome=False)
    real = obs._transcript_lines

    def explode(run_dir):
        if run_dir.name == "run-33333334":
            raise RuntimeError("something nobody predicted")
        return real(run_dir)

    monkeypatch.setattr(obs, "_transcript_lines", explode)
    rc = _sweep(obs, monkeypatch, tmp_path)
    out, err = capsys.readouterr()
    assert rc == 0
    assert "run-34343434" in out, "the healthy run beside it must still be reported"
    assert "run-33333334" in out, "the broken one is reported, not silently dropped"
    assert "could not be examined" in err
    assert boom.exists() and healthy.exists()
    # AND THE ALERT MUST NOT BORROW A CAUSE IT DOES NOT KNOW. The guard fires on
    # any exception, so rendering its record through the unreadable-transcript
    # wording would send an operator to check file modes on a transcript that may
    # be perfectly fine. The earlier version of this test asserted only the stderr
    # line, which is why that went unnoticed.
    assert "not examined (RuntimeError)" in out
    assert "UNREADABLE" not in out


def test_an_unreadable_day_directory_costs_that_day_not_the_sweep(
        obs, tmp_path, monkeypatch, capsys):
    """The per-run guard was one level shallower than the reach.

    `day_dir.iterdir()` sits outside it, and `is_dir()` returns True on a
    `chmod 000` directory — stat needs no permission on the target — so the symlink
    filter passes it and the next call raises PermissionError. Uncaught, that took
    down the whole sweep, losing every run already found in earlier days.

    Six rounds of this file's bugs landed the same way: the containment was always
    one level shallower than the reach.
    """
    import os as _os
    poisoned = tmp_path / "2026-08-20"
    poisoned.mkdir()
    (poisoned / "run-40404040").mkdir()
    healthy = _run(tmp_path, "run-41414141", day="2026-08-25", outcome=False)
    _age_dir(healthy, 6 * 3600)
    _os.chmod(poisoned, 0o000)
    try:
        rc = _sweep(obs, monkeypatch, tmp_path, days=None)
        out, err = capsys.readouterr()
        assert rc == 0
        assert "run-41414141" in out, "a later day's runs must survive the poisoned one"
        assert "2026-08-20" in err and "NOT accounted for" in err, (
            "the lost day must be named, not silently skipped")
    finally:
        _os.chmod(poisoned, 0o700)


def test_an_unreadable_day_reaches_the_alert_stream_and_not_only_stderr(
        obs, tmp_path, monkeypatch, capsys):
    """Surviving a poisoned day is not the same as reporting one.

    The day guard printed to stderr and skipped, so `abandoned_runs` returned
    normally, `main` printed a count for a sweep that had not looked at a whole
    day, and `post_alert` — the channel anyone actually watches — carried nothing.
    Any dispatched agent owns those directories, so `chmod 000` on today's hid
    every run inside it from that night's sweep and from every future one, since
    the chmod stays put and the sweep rescans all of history. An agent about to
    die abnormally could suppress the exact finding this tool exists to produce.
    """
    import os as _os
    alerts = []
    poisoned = tmp_path / "2026-08-20"
    poisoned.mkdir()
    (poisoned / "run-40404040").mkdir()
    healthy = _run(tmp_path, "run-41414141", day="2026-08-25", outcome=False)
    _age_dir(healthy, 6 * 3600)
    _os.chmod(poisoned, 0o000)
    try:
        rc = _sweep(obs, monkeypatch, tmp_path, days=None, dry_run=False,
                    alerts=alerts)
        out = capsys.readouterr().out
    finally:
        _os.chmod(poisoned, 0o700)

    assert rc == 0, "one bad day must still not fail the whole sweep"
    keys = [k for _, k in alerts]
    assert "run-observer:unreadable-day:2026-08-20:2026-08-26" in keys, (
        "the lost day must reach the alert stream, not only the log file")
    assert "run-observer:abandoned:run-41414141" in keys, (
        "and the real finding beside it must still land")
    assert "THE WHOLE DAY" in out, "and be printed where the count is printed"
    assert "FLOOR, not a total" in out, (
        "a count that omits a whole day must say so on the same stream")


def test_the_lost_day_key_carries_the_sweep_date_so_it_can_recur(
        obs, tmp_path, monkeypatch, capsys):
    """A run id is single-use; a day is recurring mutable state.

    Keyed on the day alone, an agent chmods a directory, one alert lands, an
    operator restores it, and the second chmod weeks later returns the existing
    row and posts nothing — a free second attempt at exactly the suppression this
    finding exists to catch.
    """
    import os as _os
    alerts = []
    poisoned = tmp_path / "2026-08-20"
    poisoned.mkdir()
    _os.chmod(poisoned, 0o000)
    try:
        _sweep(obs, monkeypatch, tmp_path, days=None, dry_run=False, alerts=alerts)
    finally:
        _os.chmod(poisoned, 0o700)
    keys = [k for _, k in alerts]
    assert keys == ["run-observer:unreadable-day:2026-08-20:2026-08-26"], (
        "the key must name both the day observed and the day it was observed on")


def test_a_trailing_newline_in_a_directory_name_is_not_a_day_or_a_run(obs):
    """`$` also matches just before a trailing newline, and a filename may
    legally contain one.

    Both regexes are the gate that lets `describe` skip sanitising: the run id and
    the day name are printed raw on the strength of them. A name ending in a
    newline would have split the printed line in two and minted a second dedup key
    for what a reader calls the same run or the same day.
    """
    assert obs._RUN_DIR_RE.match("run-deadbeef") is not None
    assert obs._DAY_DIR_RE.match("2026-08-26") is not None
    assert obs._RUN_DIR_RE.match("run-deadbeef\n") is None
    assert obs._DAY_DIR_RE.match("2026-08-26\n") is None


def test_an_alert_that_never_reached_the_library_fails_the_sweep(
        obs, tmp_path, monkeypatch, capsys):
    """The alert stream is the only signal, so a silent delivery failure is the
    whole sweep failing quietly.

    This file exits 0 on an unreadable day precisely BECAUSE the alert carries the
    finding. If that post never lands, launchd records a success for a night on
    which something was found and nobody was told — this file's recurring shape
    arriving at the last possible step.
    """
    healthy = _run(tmp_path, "run-41414141", day="2026-08-25", outcome=False)
    _age_dir(healthy, 6 * 3600)
    monkeypatch.setattr(obs, "post_alert",
                        lambda *a, **k: "error: connection refused")
    rc = _sweep(obs, monkeypatch, tmp_path, days=None, dry_run=False)
    err = capsys.readouterr().err
    assert rc == 1, "a finding that reached nobody is not a successful sweep"
    assert "did NOT reach the Library" in err
    assert "run-41414141" in err, "and it must name what went unreported"


def test_a_skipped_alert_is_not_a_failure(obs, tmp_path, monkeypatch, capsys):
    """`skip` is the deliberate no-op on a box with no token.

    Treating it as a delivery failure would make every such box report a broken
    sweep every night, which is how a real signal is trained into noise."""
    healthy = _run(tmp_path, "run-41414141", day="2026-08-25", outcome=False)
    _age_dir(healthy, 6 * 3600)
    monkeypatch.setattr(obs, "post_alert", lambda *a, **k: "skip")
    rc = _sweep(obs, monkeypatch, tmp_path, days=None, dry_run=False)
    assert rc == 0
    assert "did NOT reach" not in capsys.readouterr().err


def test_the_count_is_not_inflated_by_the_lost_day(obs, tmp_path, monkeypatch,
                                                   capsys):
    """The placeholder is a finding, but it is not an abandoned run.

    Folding it into the same number would trade a silent undercount for a wrong
    count, which is the same defect wearing the opposite sign."""
    import os as _os
    poisoned = tmp_path / "2026-08-20"
    poisoned.mkdir()
    healthy = _run(tmp_path, "run-41414141", day="2026-08-25", outcome=False)
    _age_dir(healthy, 6 * 3600)
    _os.chmod(poisoned, 0o000)
    try:
        _sweep(obs, monkeypatch, tmp_path, days=None)
        out = capsys.readouterr().out
    finally:
        _os.chmod(poisoned, 0o700)
    assert "1 abandoned across" in out, (
        "exactly one run was abandoned; the unreadable day is reported separately")


def test_a_clean_sweep_says_nothing_about_a_floor(obs, tmp_path, monkeypatch,
                                                  capsys):
    """Otherwise the caveat appears on every run and stops meaning anything."""
    healthy = _run(tmp_path, "run-41414141", day="2026-08-25", outcome=False)
    _age_dir(healthy, 6 * 3600)
    _sweep(obs, monkeypatch, tmp_path, days=None)
    out = capsys.readouterr().out
    assert "1 abandoned across" in out
    assert "FLOOR" not in out


def test_as_text_never_raises_even_on_deeply_nested_input(obs):
    """The docstring promised it and `repr()` can raise RecursionError on a value
    nested close to the limit — which `json.loads` will happily produce just under
    the depth at which it raises itself."""
    deep = current = {}
    for _ in range(2000):
        current["n"] = {}
        current = current["n"]
    assert isinstance(obs._as_text(deep), str)


def test_a_root_unreadable_after_the_access_check_is_not_a_clean_sweep(
        obs, tmp_path, monkeypatch, capsys):
    """The guard against an unreadable root cannot live in `os.access` alone.

    Permissions can change between asking and looking, and this file used `glob`
    to look — which swallows `PermissionError` inside pathlib's own selector and
    returns an empty listing. So the window between the check and the walk
    produced "0 abandoned" for a tree that could not be opened: a clean pass
    reported by a sweep that never looked at anything.

    The race is made deterministic here by holding `os.access` open while the
    directory is already closed, which is exactly the state the real race lands
    in. What is asserted is that the LISTING reports its own failure — so the
    call that would have to succeed for "0 abandoned" to be true is the same call
    that says it did not.
    """
    import os as _os
    day = tmp_path / "2026-08-26"
    day.mkdir()
    monkeypatch.setattr(obs.os, "access", lambda *a, **k: True)
    _os.chmod(tmp_path, 0o000)
    try:
        rc = _sweep(obs, monkeypatch, tmp_path, days=None)
        out = capsys.readouterr()
    finally:
        _os.chmod(tmp_path, 0o700)
    assert rc == 1, "a sweep that could not list its root must not exit 0"
    assert "NOT a clean pass" in out.err
    assert "abandoned across" not in out.out, (
        "a count is a claim about what was examined; nothing was")


def test_the_root_listing_is_not_glob(obs):
    """A regression guard on the MECHANISM, because the two spellings look alike
    and only one of them reports failure.

    `Path.glob` wraps its wildcard walk in `except PermissionError: return`, so
    swapping `iterdir` back for the tidier-looking `glob` would silently restore
    the bug above while every other test still passed — the failure only shows up
    on an unreadable root, which nothing else exercises. Verified against 3.11.15,
    the interpreter this file's shebang names:

        chmod 000 root -> glob yields 0 entries, raises nothing
        chmod 000 root -> iterdir raises PermissionError
    """
    import inspect
    src = inspect.getsource(obs.abandoned_runs)
    assert ".glob(" not in src, (
        "glob swallows PermissionError; the root listing must fail loudly")
    assert "RUNS_ROOT.iterdir()" in src


def test_deeply_nested_json_never_escapes_the_reader(obs, tmp_path):
    """`json.loads` raises RecursionError on 3.11, which is NOT a ValueError — so
    the narrow `except ValueError` let it through and one hostile file killed the
    sweep.

    What is asserted is that nothing escapes, deliberately NOT that the result is
    `None`: 3.14's parser walks this depth without raising and returns the list.
    Both outcomes are correct here, because what the caller needs is a dict with a
    non-empty `exit_status` and a list is not one. Pinning the return value would
    make this assert the interpreter's recursion limit rather than the reader's
    containment — it did, and it failed the day this ran on 3.14 while CI's 3.11
    stayed green.
    """
    p = tmp_path / "outcome.json"
    p.write_text("[" * 5000 + "]" * 5000)
    got = obs._read_json_nofollow(p)  # the assertion is that this line returns
    assert got is None or isinstance(got, list)
    assert obs._has_real_outcome(tmp_path) is False, (
        "however the interpreter renders it, a nested list is not an outcome")


def test_an_unreadable_runs_root_is_not_reported_as_a_clean_sweep(
        obs, tmp_path, monkeypatch, capsys):
    """"0 abandoned" must never mean "I could not look".

    `glob` on a missing, unreadable or non-directory path returns nothing rather
    than raising, so the sweep printed a clean pass. That is the same shape as
    every other bug in this file, one level up — an inability to look presented as
    a result — and worse than a crash, which would at least leave a traceback.

    Found by enumerating the levels above the day guard rather than by a review,
    testing the claim that this file's containment is always one level shallower
    than its reach. It was, again.
    """
    import os as _os
    _os.chmod(tmp_path, 0o000)
    try:
        rc = _sweep(obs, monkeypatch, tmp_path, days=None)
        err = capsys.readouterr().err
        assert rc == 1, "an unexaminable tree must not exit 0"
        assert "NOT a clean pass" in err
    finally:
        _os.chmod(tmp_path, 0o700)


def test_a_missing_runs_root_is_also_not_a_clean_sweep(obs, tmp_path, monkeypatch, capsys):
    rc = _sweep(obs, monkeypatch, tmp_path / "does-not-exist", days=None)
    assert rc == 1
    assert "NOT a clean pass" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["meta.json", "transcript.jsonl", "outcome.json"])
def test_a_fifo_in_place_of_a_file_does_not_hang_the_sweep(
        obs, tmp_path, monkeypatch, capsys, name):
    """O_NOFOLLOW refuses a SYMLINK and nothing else, which is not the same as
    refusing everything that is not a file.

    `open()` on a FIFO with no writer BLOCKS AT THE SYSCALL — it never raises, so
    none of this file's seven rounds of guards ever run; it simply never returns.
    `rm meta.json && mkfifo meta.json` in an agent's own run directory hung the
    06:30 job forever, and launchd will not start a calendar job over a
    still-running instance, so every later day was silently skipped too.

    The symlink defence was aimed at the wrong question. The question was never
    "is this a symlink" but "is this a regular file".

    This test would HANG rather than fail against the previous commit, which is
    why it carries its own alarm.
    """
    import os as _os
    import signal

    d = tmp_path / "2026-08-25" / "run-42424242"
    d.mkdir(parents=True)
    for f in ("meta.json", "transcript.jsonl"):
        (d / f).write_text('{"agent": "reviewer", "pid": 999999, '
                           '"started_at": "2026-08-25T00:00:00Z"}\n')
    (d / name).unlink(missing_ok=True)
    _os.mkfifo(d / name)
    _age_dir(d, 6 * 3600)

    signal.signal(signal.SIGALRM, lambda *a: (_ for _ in ()).throw(
        AssertionError(f"the sweep hung on a FIFO at {name}")))
    signal.alarm(10)
    try:
        rc = _sweep(obs, monkeypatch, tmp_path, days=None)
    finally:
        signal.alarm(0)
    assert rc == 0
    assert "run-42424242" in capsys.readouterr().out, (
        "a run whose files are not regular files is a finding, not a skip")


def test_a_finished_run_that_refused_its_recovery_is_reported(
        obs, tmp_path, monkeypatch, capsys):
    """The decision is recorded on runs that FINISHED, which is why the read had
    to move above the finalized-run short circuit.

    17 of the 103 real refusals completed, and all 68 fail-overs that happened
    completed or were killed — so a check placed after `_has_real_outcome`'s
    `continue` would have seen none of the second group and a sixth of the first.
    This run has a perfectly good outcome.json; it is not abandoned, and it is
    still a finding.
    """
    alerts = []
    _run(tmp_path, "run-51515151", day="2026-08-25", outcome=True, pr="1219",
         extra_meta={"fallback_blocked_to": "deepseek/deepseek-v4-flash",
                     "fallback_blocked_reason": "interactive origin"})
    rc = _sweep(obs, monkeypatch, tmp_path, days=None, dry_run=False, alerts=alerts)
    out = capsys.readouterr().out
    assert rc == 0
    assert "REFUSED" in out
    assert "deepseek/deepseek-v4-flash" in out, "name the target that was refused"
    assert "vivy#1219" in out, "and the PR that lost the work"
    assert "run-observer:failover-refused:run-51515151" in [k for _, k in alerts]
    assert "0 abandoned across" in out, (
        "a finished run is not abandoned; the counts are separate findings")


def test_a_finished_run_that_took_its_recovery_is_reported(
        obs, tmp_path, monkeypatch, capsys):
    """An action is reported as loudly as a refusal — the epic's words.

    64 of 68 real fail-overs completed, so the result was already published, at a
    provider the dispatcher did not choose and with nothing saying so."""
    alerts = []
    _run(tmp_path, "run-52525252", day="2026-08-25", outcome=True,
         extra_meta={"fallback_from": "claude-sonnet-5",
                     "model": "deepseek/deepseek-chat",
                     "fallback_reason": "exit_code=1"})
    _sweep(obs, monkeypatch, tmp_path, days=None, dry_run=False, alerts=alerts)
    out = capsys.readouterr().out
    assert "deepseek/deepseek-chat" in out, "name where it actually ran"
    assert "claude-sonnet-5" in out, "and where it was sent"
    assert "run-observer:failover-taken:run-52525252" in [k for _, k in alerts]


def test_an_ordinary_finished_run_produces_no_failover_finding(
        obs, tmp_path, monkeypatch, capsys):
    """Otherwise every run in the tree becomes a finding and none of them mean
    anything. This is the case that must stay silent, and it is almost all of
    them."""
    alerts = []
    _run(tmp_path, "run-53535353", day="2026-08-25", outcome=True)
    _sweep(obs, monkeypatch, tmp_path, days=None, dry_run=False, alerts=alerts)
    out = capsys.readouterr().out
    assert alerts == []
    assert "REFUSED" not in out
    assert "recovery available" not in out
    assert "provider nobody chose" not in out


def test_the_two_decisions_are_counted_apart_from_each_other_and_from_abandonment(
        obs, tmp_path, monkeypatch, capsys):
    """One total would be a number nobody could act on, and a run can carry both
    fields at once, so they are not even addable."""
    abandoned = _run(tmp_path, "run-54545454", day="2026-08-25", outcome=False)
    _age_dir(abandoned, 6 * 3600)
    _run(tmp_path, "run-55555555", day="2026-08-25", outcome=True,
         extra_meta={"fallback_blocked_to": "x", "fallback_blocked_reason": "y"})
    _run(tmp_path, "run-56565656", day="2026-08-25", outcome=True,
         extra_meta={"fallback_from": "a", "model": "b", "fallback_reason": "c"})
    _sweep(obs, monkeypatch, tmp_path, days=None)
    out = capsys.readouterr().out
    assert "1 abandoned across" in out
    assert "1 run(s) died with a recovery available and refused" in out
    assert "1 run(s) ran at a provider nobody chose" in out


def test_one_run_carrying_both_decisions_reports_both(
        obs, tmp_path, monkeypatch, capsys):
    """They are separate fields recording separate moments — a refusal of one
    target does not preclude a fail-over onto another — so reporting whichever is
    checked first would drop a real finding."""
    alerts = []
    _run(tmp_path, "run-57575757", day="2026-08-25", outcome=True,
         extra_meta={"fallback_blocked_to": "blocked-target",
                     "fallback_blocked_reason": "below floor",
                     "fallback_from": "sent-to", "model": "ran-on",
                     "fallback_reason": "exit_code=1"})
    _sweep(obs, monkeypatch, tmp_path, days=None, dry_run=False, alerts=alerts)
    keys = sorted(k for _, k in alerts)
    assert keys == ["run-observer:failover-refused:run-57575757",
                    "run-observer:failover-taken:run-57575757"], (
        "two findings about one run, keyed apart so neither suppresses the other")


def test_a_forged_refusal_reason_cannot_split_the_reported_line(
        obs, tmp_path, monkeypatch, capsys):
    """meta.json is the run's own to write, and this text is printed to a terminal
    and posted to the Library.

    A newline in the reason would forge a second run-observer line — the same
    surface that made `terminal_reason` a finding the moment it became printable.

    THE FORGED TEXT IS A SENTINEL THE SWEEP WOULD NEVER PRINT ITSELF. The first
    version of this test planted `run-observer: 0 abandoned across all of history`,
    which the sweep emits legitimately on the very tree the test builds — so a
    working `sanitise` and a broken one produced the same output and the assertion
    could not tell them apart. That is the failure this whole file is about,
    arriving inside its own test.
    """
    _run(tmp_path, "run-58585858", day="2026-08-25", outcome=True,
         extra_meta={"fallback_blocked_to": "t",
                     "fallback_blocked_reason": "ok\nrun-observer: FORGED-SENTINEL"})
    _sweep(obs, monkeypatch, tmp_path, days=None)
    out = capsys.readouterr().out
    assert "FORGED-SENTINEL" in out, "the text is reported rather than dropped"
    forged = [ln for ln in out.splitlines()
              if ln.startswith("run-observer: FORGED-SENTINEL")]
    assert forged == [], "but it must not start a line of its own"


def test_a_meta_that_is_not_an_object_yields_no_failover_finding(obs, tmp_path):
    """`_read_json_nofollow` returns whatever parsed, and valid JSON can be a list.

    Calling `.get` on that raises, and the per-run guard would then report the run
    as unexaminable — a real finding manufactured out of a type error."""
    d = tmp_path / "2026-08-25" / "run-59595959"
    d.mkdir(parents=True)
    assert obs._failover_findings(d, []) == []
    assert obs._failover_findings(d, "a string") == []
    assert obs._failover_findings(d, None) == []


def test_an_abandoned_run_that_also_refused_reports_both_findings(
        obs, tmp_path, monkeypatch, capsys):
    """THE MAJORITY CASE, and every other new test misses it.

    86 of the 103 real refusals ended `errored` rather than `completed`, so most
    refusals sit on a run that ALSO has no usable outcome. The five other
    fail-over tests all build their run with `outcome=True`, which is the
    minority. What has to hold is that the two findings coexist: distinct dedup
    keys, both counted, neither suppressing the other — a real finding buried by
    an adjacent one is the shape this file's history keeps landing on.
    """
    alerts = []
    d = _run(tmp_path, "run-60606060", day="2026-08-25", outcome=False, pr="1219",
             extra_meta={"fallback_blocked_to": "deepseek/deepseek-v4-flash",
                         "fallback_blocked_reason": "interactive origin"})
    _age_dir(d, 6 * 3600)
    _sweep(obs, monkeypatch, tmp_path, days=None, dry_run=False, alerts=alerts)
    out = capsys.readouterr().out
    keys = sorted(k for _, k in alerts)
    assert keys == ["run-observer:abandoned:run-60606060",
                    "run-observer:failover-refused:run-60606060"], (
        "one run, two independent findings, neither key suppressing the other")
    assert "1 abandoned across" in out
    assert "1 run(s) died with a recovery available and refused" in out
    assert "never finalized" in out, "the abandonment line still renders"
    assert "REFUSED" in out, "and so does the refusal line"


def test_a_raising_failover_check_does_not_mislabel_a_finished_run(
        obs, tmp_path, monkeypatch, capsys):
    """A new reachability the moved read created.

    Before it, a finalized run hit `continue` before any fallible code. Now it
    passes through `_failover_findings` inside the per-run guard, whose job is to
    report an unexaminable run as ABANDONED — so a future raise in that helper
    would turn a genuinely completed run into a false abandonment finding. That
    is worse than a missing finding: it sends someone to inspect a run that
    finished cleanly.
    """
    alerts = []
    _run(tmp_path, "run-61616161", day="2026-08-25", outcome=True)

    def _boom(run_dir, meta):
        raise RuntimeError("a future edit")
    monkeypatch.setattr(obs, "_failover_findings", _boom)
    _sweep(obs, monkeypatch, tmp_path, days=None, dry_run=False, alerts=alerts)
    out, err = capsys.readouterr()
    assert "0 abandoned across" in out, (
        "a finished run must never be reported as abandoned because a helper "
        "beside it raised")
    assert alerts == []
    assert "could not check its provider decisions" in err, (
        "and the gap must be named rather than swallowed")


def test_a_forged_flood_cannot_push_a_genuine_finding_out_of_the_sweep(
        obs, tmp_path, monkeypatch, capsys):
    """THE REASON THERE IS NO CAP, pinned as a reproduction rather than an
    argument.

    An earlier commit on this branch bounded the sweep to 50 new alerts a night,
    to stop the two fail-over kinds being manufactured at volume. A cap has to
    deliver in some order; this sweep's order is the walk order; the walk sorts
    day directories by name. So a day bucket named `1900-01-01` sorts ahead of
    every real one, costs a `mkdir`, and mints fresh never-deduped run ids every
    night — which pushed a genuine refusal on vivy#1219 to position 60 of 61 and
    past the budget, permanently.

    That is strictly worse than the flood it was fixing: a flood adds noise a
    human must sift, while the cap let the forger choose which real alert nobody
    sees, including one about its own run. Every ordering key available here is
    written by the party being reported on, so no cap is safe.
    """
    alerts = []
    real = _run(tmp_path, "run-deadbeef", day="2026-08-26", outcome=True, pr="1219",
                extra_meta={"fallback_blocked_to": "deepseek",
                            "fallback_blocked_reason": "interactive origin"})
    assert real.exists()
    for i in range(60):
        _run(tmp_path, f"run-{i:08x}", day="1900-01-01", outcome=True, lines=0,
             extra_meta={"fallback_blocked_to": "x"})
    # RETURNS "created", which is what the Library answers for a key it has not
    # seen. The shared `alerts=` recorder returns None, and the budget this test
    # exists to refute only counted a "created" — so with that recorder the cap
    # never fired and this test passed against the very commit it was written to
    # condemn. Verified after fixing: it fails against 055ad31 and passes here.
    monkeypatch.setattr(obs, "post_alert",
                        lambda stream, body, **kw: (alerts.append((body, kw.get("dedup_key"))),
                                                    "created")[1])
    _sweep(obs, monkeypatch, tmp_path, days=None, dry_run=False)
    keys = [k for _, k in alerts]
    assert "run-observer:failover-refused:run-deadbeef" in keys, (
        "the genuine finding must be delivered no matter how much sorts ahead of it")
    assert len(keys) >= 61, "and so must every other finding — nothing is deferred"


def test_an_anomalous_volume_is_alerted_on_alongside_everything_else(
        obs, tmp_path, monkeypatch, capsys):
    """Reported rather than throttled: the flood is named without the flood
    getting to decide what is named."""
    alerts = []
    for i in range(obs._ANOMALOUS_FINDING_COUNT + 3):
        _run(tmp_path, f"run-{i:08x}", day="2026-08-25", outcome=True,
             extra_meta={"fallback_blocked_to": "x", "fallback_blocked_reason": "y"})
    _sweep(obs, monkeypatch, tmp_path, days=None, dry_run=False, alerts=alerts)
    out = capsys.readouterr().out
    volume = [k for _, k in alerts if k.startswith("run-observer:finding-volume:")]
    findings = [k for _, k in alerts if k.startswith("run-observer:failover-refused:")]
    assert len(findings) == obs._ANOMALOUS_FINDING_COUNT + 3, "all of them posted"
    assert volume == ["run-observer:finding-volume:2026-08-26"], (
        "plus one alert about the count, keyed to the sweep date")
    assert "all posted" in out


def test_an_ordinary_sweep_says_nothing_about_volume(
        obs, tmp_path, monkeypatch, capsys):
    """Almost every night, at roughly three findings a day. A volume notice on a
    sweep nowhere near the threshold is the noise it exists to flag."""
    alerts = []
    _run(tmp_path, "run-62626262", day="2026-08-25", outcome=True,
         extra_meta={"fallback_blocked_to": "x", "fallback_blocked_reason": "y"})
    _sweep(obs, monkeypatch, tmp_path, days=None, dry_run=False, alerts=alerts)
    assert [k for _, k in alerts if "finding-volume" in k] == []
    assert "all posted" not in capsys.readouterr().out


def test_alerts_are_paced_without_deferring_any_of_them(
        obs, tmp_path, monkeypatch, capsys):
    """Pacing is not a cap, and the difference is what this asserts.

    Removing the budget left one outbound post per finding with no spacing, so a
    forger minting N directories forces N near-simultaneous calls at the Library.
    A delay bounds that rate without choosing what to drop — which is the whole
    reason the cap had to go — so what has to hold is that every finding is still
    posted in THIS sweep, and that the sleep happens between them rather than
    before the first.
    """
    slept = []
    monkeypatch.setattr(obs.time, "sleep", lambda s: slept.append(s))
    alerts = []
    for i in range(5):
        _run(tmp_path, f"run-{i:08x}", day="2026-08-25", outcome=True,
             extra_meta={"fallback_blocked_to": "x", "fallback_blocked_reason": "y"})
    _sweep(obs, monkeypatch, tmp_path, days=None, dry_run=False, alerts=alerts)
    assert len(alerts) == 5, "every finding is posted in this sweep, none deferred"
    assert slept == [obs._ALERT_SPACING_S] * 4, (
        "spaced between posts, not before the first")


def test_the_volume_line_is_printed_even_when_its_alert_is_refused(
        obs, tmp_path, monkeypatch, capsys):
    """The second channel, and the reason it is worth having.

    The volume alert's dedup key is the calendar date and nothing else, so an
    agent about to flood can pre-claim it and the Library will answer the real
    sweep with the existing row. The stdout line does not go through the Library
    and is printed whatever the post returns, so the log still records the volume.
    A weak channel — nothing tails it — but the difference between weak and none.
    """
    for i in range(obs._ANOMALOUS_FINDING_COUNT + 1):
        _run(tmp_path, f"run-{i:08x}", day="2026-08-25", outcome=True,
             extra_meta={"fallback_blocked_to": "x", "fallback_blocked_reason": "y"})
    # Every post answers "duplicate", which is what a pre-claimed key looks like.
    monkeypatch.setattr(obs, "post_alert", lambda *a, **k: "duplicate")
    monkeypatch.setattr(obs.time, "sleep", lambda s: None)
    rc = _sweep(obs, monkeypatch, tmp_path, days=None, dry_run=False)
    out = capsys.readouterr().out
    assert rc == 0, "a duplicate is not a delivery failure"
    assert "all posted, and the volume is itself alerted on" in out, (
        "the log records the volume even when the alert was pre-claimed away")


def test_a_dry_run_names_the_extra_volume_alert_it_would_post(
        obs, tmp_path, monkeypatch, capsys):
    """The dry-run branch had no test.

    Two covered the budget-era message and were deleted with the budget; nothing
    replaced them, and every other default dry-run test builds one or two run
    directories, so this branch was unreached. A preview whose extra line nothing
    exercises is a line that can rot silently — which matters here precisely
    because the volume alert is the one thing the cap removal left behind.
    """
    for i in range(obs._ANOMALOUS_FINDING_COUNT + 2):
        _run(tmp_path, f"run-{i:08x}", day="2026-08-25", outcome=True,
             extra_meta={"fallback_blocked_to": "x", "fallback_blocked_reason": "y"})
    _sweep(obs, monkeypatch, tmp_path, days=None)
    out = capsys.readouterr().out
    assert f"would post all {obs._ANOMALOUS_FINDING_COUNT + 2} of these" in out
    assert "one more alert about the volume itself" in out


def test_an_ordinary_dry_run_says_nothing_about_volume(
        obs, tmp_path, monkeypatch, capsys):
    """The other direction, which is almost every night. A volume note on a sweep
    nowhere near the threshold is the noise the threshold exists to flag."""
    _run(tmp_path, "run-63636363", day="2026-08-25", outcome=True,
         extra_meta={"fallback_blocked_to": "x", "fallback_blocked_reason": "y"})
    _sweep(obs, monkeypatch, tmp_path, days=None)
    out = capsys.readouterr().out
    assert "would post all" not in out
    assert "volume itself" not in out


def test_the_pacing_budget_is_spent_and_then_abandoned_not_the_posts(
        obs, tmp_path, monkeypatch, capsys):
    """The delay is bounded; the posting is not.

    Unbounded, the 50ms spacing was a denial of the sweep: nothing limits how many
    findings a sweep produces, manufacturing one costs a `mkdir` and a line, and
    launchd will not start a calendar job over a still-running instance — so an
    inflated sweep silently skips every later day, nightly, until someone cleans
    the tree.

    What must hold is that the BUDGET runs out rather than the reporting. Every
    finding still posts in this sweep; only the spacing between them stops.
    """
    slept = []
    monkeypatch.setattr(obs.time, "sleep", lambda s: slept.append(s))
    alerts = []
    n = int(obs._MAX_TOTAL_PACING_S / obs._ALERT_SPACING_S) + 20
    for i in range(n):
        _run(tmp_path, f"run-{i:08x}", day="2026-08-25", outcome=True,
             extra_meta={"fallback_blocked_to": "x", "fallback_blocked_reason": "y"})
    _sweep(obs, monkeypatch, tmp_path, days=None, dry_run=False, alerts=alerts)
    err = capsys.readouterr().err

    # n findings plus the volume alert; every one of them posted.
    assert len(alerts) == n + 1, "the budget bounds the delay, never the posting"
    assert sum(slept) <= obs._MAX_TOTAL_PACING_S + 1e-9, (
        "and the total delay is bounded no matter how many findings there are")
    assert len(slept) < n - 1, "so the later findings post back to back"
    assert "posting the rest back to back" in err, (
        "and abandoning the pacing is said out loud, since a sweep this size is "
        "itself the finding")


def test_an_ordinary_sweep_never_reaches_the_pacing_budget(
        obs, tmp_path, monkeypatch, capsys):
    """Roughly three findings a day. The budget must be invisible on the nights
    that matter, or its message becomes the noise it exists to flag."""
    slept = []
    monkeypatch.setattr(obs.time, "sleep", lambda s: slept.append(s))
    alerts = []
    for i in range(3):
        _run(tmp_path, f"run-{i:08x}", day="2026-08-25", outcome=True,
             extra_meta={"fallback_blocked_to": "x", "fallback_blocked_reason": "y"})
    _sweep(obs, monkeypatch, tmp_path, days=None, dry_run=False, alerts=alerts)
    assert sum(slept) < obs._MAX_TOTAL_PACING_S
    assert "back to back" not in capsys.readouterr().err
