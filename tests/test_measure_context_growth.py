"""tools/measure-context-growth.py — the rollover epic's Slice 2 (platform/
decisions/0069). Read-only: parses Claude Code transcripts into per-call
context growth so the hard floor and call cap can be set from measurement.

Covers exactly what the spec calls out: deduplication on `message.id`,
splitting at `compact_boundary`, and the jump arithmetic (growth, tool
attribution, corruption detection, command-span cost, the K-windowed rise and
the threshold-crossing call counts the floor/cap formulas are built from) —
plus this file's own population logic: the allowlist half of directory
resolution (`collect_transcripts` admits a directory only when
`TC.resolve_product` names a registered, non-private project — PR #558 round
1, security-reviewer run-c9184395) and the by-session (not by-directory)
exclusion of a dispatched run's own transcript (`dispatched_session_ids` —
PR #558 round 1, reviewer 2026-09-13 01:1x), neither of which is covered by
`bin/transcript-capture`'s own tests since both live only in this file.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TOOL_PATH = REPO / "tools" / "measure-context-growth.py"


@pytest.fixture(scope="module")
def M():
    spec = importlib.util.spec_from_file_location("measure_context_growth", TOOL_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _assistant(msg_id, input_tokens=0, cache_read=0, cache_creation=0,
               content=None, sidechain=False, model="claude-sonnet-5-20260101",
               usage=...):
    """A real assistant record by default (`model` set, `usage` a populated
    dict) — pass `model="<synthetic>"` or `usage=None` to build the
    interrupted-turn / malformed shapes `_is_real_call` must skip."""
    message = {"id": msg_id, "content": content if content is not None else []}
    if model is not None:
        message["model"] = model
    if usage is ...:
        usage = {
            "input_tokens": input_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_creation,
        }
    if usage is not None:
        message["usage"] = usage
    return {
        "type": "assistant",
        "isSidechain": sidechain,
        "message": message,
    }


def _tool_use_block(tool_id, name):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": {}}


def _text_block(text):
    return {"type": "text", "text": text}


def _tool_result(tool_use_id, sidechain=False):
    return {
        "type": "user",
        "isSidechain": sidechain,
        "toolUseResult": {},
        "message": {"content": [
            {"type": "tool_result", "tool_use_id": tool_use_id, "content": "ok"}
        ]},
    }


def _prompt(text, sidechain=False):
    return {"type": "user", "isSidechain": sidechain, "message": {"content": text}}


def _command(name, sidechain=False):
    text = f"<command-name>/{name}</command-name>\n<command-message>{name}</command-message>"
    return {"type": "user", "isSidechain": sidechain, "message": {"content": text}}


def _compact_boundary():
    return {"type": "compact_boundary"}


def _write_transcript(path, records):
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


# --- load_main_chain_records: dedup + sidechain -----------------------------


def test_dedupes_on_message_id_and_drops_sidechain(M, tmp_path):
    p = tmp_path / "s.jsonl"
    _write_transcript(p, [
        _assistant("m1", input_tokens=100),
        _assistant("m1", input_tokens=999),          # duplicate id — dropped
        _assistant("m2", input_tokens=50, sidechain=True),  # sidechain — dropped
        _assistant("m3", input_tokens=200),
    ])
    loaded = M.load_main_chain_records(p)
    ids = [r["message"]["id"] for r in loaded if r["type"] == "assistant"]
    assert ids == ["m1", "m3"]
    # the FIRST occurrence's data survives, not the duplicate's
    kept = next(r for r in loaded if r["message"]["id"] == "m1")
    assert kept["message"]["usage"]["input_tokens"] == 100


def test_malformed_json_line_is_skipped_not_fatal(M, tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text('{"not valid json\n' + json.dumps(_assistant("m1", input_tokens=10)) + "\n")
    loaded = M.load_main_chain_records(p)
    assert len(loaded) == 1


# --- split_rotations: compact_boundary --------------------------------------


def test_splits_at_compact_boundary(M):
    records = [
        _assistant("a1", input_tokens=1),
        _compact_boundary(),
        _assistant("a2", input_tokens=2),
        _assistant("a3", input_tokens=3),
    ]
    rotations = M.split_rotations(records)
    assert len(rotations) == 2
    assert [r["message"]["id"] for r in rotations[0]] == ["a1"]
    assert [r["message"]["id"] for r in rotations[1]] == ["a2", "a3"]


def test_boundary_at_start_and_consecutive_boundaries_drop_empty_segments(M):
    records = [
        _compact_boundary(),
        _assistant("a1", input_tokens=1),
        _compact_boundary(),
        _compact_boundary(),
        _assistant("a2", input_tokens=2),
    ]
    rotations = M.split_rotations(records)
    assert len(rotations) == 2
    assert [r["message"]["id"] for r in rotations[0]] == ["a1"]
    assert [r["message"]["id"] for r in rotations[1]] == ["a2"]


# --- analyze_rotation: the jump arithmetic ----------------------------------


def test_first_call_growth_is_measured_against_zero_baseline(M):
    calls, _ = M.analyze_rotation([_assistant("a1", input_tokens=66_324)])
    assert calls[0]["context"] == 66_324
    assert calls[0]["growth"] == 66_324
    assert calls[0]["growth_pct"] == pytest.approx(6.6324, abs=1e-6)


def test_growth_is_the_delta_between_consecutive_calls(M):
    records = [
        _assistant("a1", input_tokens=1000),
        _assistant("a2", input_tokens=5000),
    ]
    calls, _ = M.analyze_rotation(records)
    assert calls[0]["growth"] == 1000
    assert calls[1]["growth"] == 4000
    assert calls[1]["context"] == 5000


def test_tool_result_is_attributed_to_the_next_call(M):
    records = [
        _assistant("a1", input_tokens=1000, content=[_tool_use_block("t1", "Read")]),
        _tool_result("t1"),
        _assistant("a2", input_tokens=5000),
    ]
    calls, _ = M.analyze_rotation(records)
    assert calls[0]["tools"] == []
    assert calls[1]["tools"] == ["Read"]


def test_a_sidechain_tool_result_never_gets_matched(M):
    """A subagent's own tool call must not attribute a jump in the MAIN
    chain — its tool_result never reaches this rotation's `records` at all
    once `load_main_chain_records` has filtered isSidechain rows out, so
    pairing here only ever sees main-chain pending ids."""
    records = [
        _assistant("a1", input_tokens=1000, content=[_tool_use_block("t1", "Read")]),
        _assistant("a2", input_tokens=5000),  # no matching tool_result arrived
    ]
    calls, _ = M.analyze_rotation(records)
    assert calls[1]["tools"] == []


def test_corruption_requires_both_bare_tags_in_the_same_block(M):
    both = _assistant("a1", content=[_text_block("<invoke name=\"x\"><parameter name=\"y\">z")])
    only_invoke = _assistant("a2", content=[_text_block("<invoke name=\"x\">")])
    namespaced = _assistant("a3", content=[
        _text_block("antml:<invoke name=\"x\">antml:<parameter name=\"y\">z")
    ])
    calls, _ = M.analyze_rotation([both, only_invoke, namespaced])
    assert [c["corrupt"] for c in calls] == [True, False, False]


def test_command_span_cost_covers_calls_until_the_next_turn_boundary(M):
    records = [
        _prompt("hello"),
        _assistant("a1", input_tokens=1000),
        _command("rollover"),
        _assistant("a2", input_tokens=3000),   # made WHILE the skill runs
        _prompt("thanks"),                      # closes the span
    ]
    calls, events = M.analyze_rotation(records)
    assert len(events) == 1
    assert events[0]["command"] == "rollover"
    growth_tokens, growth_pct, num_calls = M.command_event_cost(calls, events[0])
    assert growth_tokens == 2000
    assert num_calls == 1
    assert growth_pct == pytest.approx(0.2, abs=1e-9)


def test_command_span_still_open_at_rotation_end_closes_there(M):
    records = [
        _command("handoff-leave"),
        _assistant("a1", input_tokens=4000),
    ]
    calls, events = M.analyze_rotation(records)
    assert len(events) == 1
    assert events[0]["command"] == "handoff-leave"
    assert events[0]["end_idx"] == 0
    growth_tokens, _, _ = M.command_event_cost(calls, events[0])
    assert growth_tokens == 4000  # baseline 0, since start_idx is -1


def test_a_non_handoff_command_opens_no_span(M):
    records = [_command("code-review"), _assistant("a1", input_tokens=1000)]
    _, events = M.analyze_rotation(records)
    assert events == []


# --- analyze_rotation: synthetic / malformed assistant records are skipped -


def test_synthetic_interrupted_turn_record_produces_no_jump(M):
    """PR #558 round 2, reviewer: Claude Code writes a synthetic record for
    an interrupted turn (`message.model == "<synthetic>"`, all four usage
    fields zero). Before this fix that record was counted as a real call at
    context 0, so the NEXT real call's whole context read as a one-call jump
    — exactly session `afb28de3-16c2-4617-804d-dc7af41ff787`'s reported
    838,433-token jump at call 433 (call 432 is `<synthetic>`). Fails before
    the fix: 2 calls become 3, and the second call's growth is its full
    context (520,000) rather than the 20,000 delta from the last real call."""
    records = [
        _assistant("a1", input_tokens=500_000),
        _assistant("interrupted", model="<synthetic>", usage={
            "input_tokens": 0, "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0, "output_tokens": 0,
        }),
        _assistant("a2", input_tokens=520_000),
    ]
    calls, _ = M.analyze_rotation(records)
    assert len(calls) == 2
    assert calls[0]["context"] == 500_000
    assert calls[1]["context"] == 520_000
    assert calls[1]["growth"] == 20_000


def test_assistant_record_missing_model_is_skipped(M):
    """A record with no `model` field at all is a different malformed shape
    than `<synthetic>`, but the same rule applies: it's neither a call nor a
    baseline."""
    records = [
        _assistant("a1", input_tokens=500_000),
        _assistant("no-model", model=None),
        _assistant("a2", input_tokens=520_000),
    ]
    calls, _ = M.analyze_rotation(records)
    assert len(calls) == 2
    assert calls[1]["growth"] == 20_000


def test_assistant_record_with_missing_usage_is_skipped(M):
    """Any other record shape carrying a missing `usage` block, whatever its
    `model` field says, is excluded the same way as `<synthetic>`."""
    records = [
        _assistant("a1", input_tokens=500_000),
        _assistant("no-usage", usage=None),
        _assistant("a2", input_tokens=520_000),
    ]
    calls, _ = M.analyze_rotation(records)
    assert len(calls) == 2
    assert calls[1]["growth"] == 20_000


# --- corpus-wide aggregation -------------------------------------------------


def test_corpus_excludes_a_rotations_opening_call_from_growth_stats(M):
    """PR #558 round 3, reviewer: `analyze_rotation` measures a rotation's
    FIRST call's growth against a 0 baseline (an opening payload, not growth
    inside the session — every post-compaction rotation has one), but
    `Corpus.add_rotation` fed that opening-context "growth" into
    `growth_tokens`/`growth_pct`/`max_jump` right alongside every other
    call's real delta. It inflates the global p99 the fixed cap formula
    divides by and can win "largest single-call jump" outright — exactly
    this report's previously-reported 181,548-token/18% jump, itself a
    rotation's call index 0. Fails before the fix: `growth_tokens` includes
    the opening 500_000, and `max_jump` picks it over the real 10_000 rise."""
    calls, _ = M.analyze_rotation([
        _assistant("a1", input_tokens=500_000),   # opening payload
        _assistant("a2", input_tokens=510_000),   # +10_000 real growth
        _assistant("a3", input_tokens=515_000),   # +5_000 real growth
    ])
    corpus = M.Corpus()
    corpus.add_rotation("sess-1", calls, [])
    assert corpus.growth_tokens == [10_000, 5_000]
    assert corpus.max_jump["growth_tokens"] == 10_000
    assert corpus.max_jump["call_index"] == 1


def test_corpus_still_counts_corruption_on_a_rotations_opening_call(M):
    """The opening-call exclusion is scoped to growth stats only — a corrupt
    signature on call 0 must still be counted, since corruption detection has
    nothing to do with the growth baseline."""
    calls, _ = M.analyze_rotation([
        _assistant("a1", input_tokens=100,
                   content=[_text_block("<invoke name=\"x\"><parameter name=\"y\">z")]),
    ])
    corpus = M.Corpus()
    corpus.add_rotation("sess-1", calls, [])
    assert corpus.total_corrupt_calls == 1
    assert corpus.growth_tokens == []


def _pct_calls(pcts_and_growth):
    return [{"pct": p, "growth_pct": g} for p, g in pcts_and_growth]


def test_max_rise_after_threshold_only_counts_calls_past_the_crossing(M):
    calls = _pct_calls([
        (10, 10), (40, 30), (75, 35),   # crosses 70% at index 2
        (80, 5), (95, 15), (100, 5),
    ])
    assert M.max_rise_after_threshold(calls, 70, 2) == pytest.approx(20.0)
    assert M.max_rise_after_threshold(calls, 70, 5) is None   # only 3 calls follow
    assert M.max_rise_after_threshold(calls, 70, 3) == pytest.approx(25.0)


def test_max_rise_after_threshold_none_when_threshold_never_crossed(M):
    calls = _pct_calls([(10, 10), (20, 10), (30, 10)])
    assert M.max_rise_after_threshold(calls, 70, 2) is None


def test_calls_to_thresholds_counts_from_the_crossing_call(M):
    calls = _pct_calls([
        (10, 10), (40, 30), (75, 35), (80, 5), (95, 15), (100, 5),
    ])
    result = M.calls_to_thresholds(calls, 70, (80, 90, 95, 100))
    assert result == {80: 1, 90: 2, 95: 2, 100: 3}


def test_calls_to_thresholds_marks_an_unreached_threshold_as_none(M):
    calls = _pct_calls([(75, 75), (80, 5)])
    result = M.calls_to_thresholds(calls, 70, (80, 90, 95, 100))
    # crosses 70% at index 0 (pct=75); 80% is first reached at index 1 — one
    # call after the crossing — and nothing here ever reaches 90/95/100.
    assert result == {80: 1, 90: None, 95: None, 100: None}


def test_percentile_linear_interpolation(M):
    data = [1, 2, 3, 4, 5]
    assert M.percentile(data, 0) == 1
    assert M.percentile(data, 50) == 3
    assert M.percentile(data, 100) == 5
    assert M.percentile([], 50) is None


def test_decile_clamps_to_0_through_9(M):
    assert M._decile(0) == 0
    assert M._decile(9.9) == 0
    assert M._decile(10) == 1
    assert M._decile(95) == 9
    assert M._decile(100) == 9
    assert M._decile(105) == 9


# --- population: allowlist half + by-session dispatched exclusion ----------


def _registered_dirname(M, slug: str) -> str:
    """The sanitized `~/.claude/projects/<dir>` name `slug`'s registered home
    directory produces — a REAL registry entry, not a fabricated one, since
    `TC.resolve_product` must actually resolve it (mirrors
    `tests/test_transcript_capture.py`'s own `_home_prefix` helper)."""
    return M.TC._sanitize(M.projects.home_path(slug))


def test_collect_transcripts_excludes_an_unregistered_directory_unopened(M, tmp_path):
    """PR #558 round 1, security-reviewer run-c9184395: the old denylist-only
    check fails open for any directory no crew's prefix names — exactly the
    shape a headless `claude -p` job with no `WorkingDirectory` produces
    (cwd `/`, sanitized dirname `-`). Fails before the allowlist fix: nothing
    excluded an unregistered directory, so its transcript was included."""
    projects_root = tmp_path / "claude-projects"
    unregistered_dir = projects_root / "-Users-operator-some-unregistered-place"
    unregistered_dir.mkdir(parents=True)
    _write_transcript(unregistered_dir / "s1.jsonl", [_assistant("a1", input_tokens=10)])

    runs_root = tmp_path / "agent-runs"
    found = M.collect_transcripts(projects_root, runs_root)
    assert found == []


def test_collect_transcripts_excludes_a_private_directory_even_if_the_denylist_is_bypassed(
    M, tmp_path, monkeypatch,
):
    """The allowlist is a SECOND, independent guard, not a restatement of the
    denylist (`TC.resolve_product`'s own docstring): `resolve_product` refuses
    a private slug on its own, so a private crew's directory stays excluded
    even if `black_box_prefixes` were bypassed. Fails before the allowlist
    fix: with the denylist patched out and no allowlist check, nothing
    excludes the directory."""
    monkeypatch.setattr(M.TC, "black_box_prefixes", lambda *a, **k: [])
    private_slug = sorted(M.projects.private_slugs())[0]
    projects_root = tmp_path / "claude-projects"
    private_dir = projects_root / _registered_dirname(M, private_slug)
    private_dir.mkdir(parents=True)
    _write_transcript(private_dir / "s1.jsonl", [_assistant("a1", input_tokens=10)])

    runs_root = tmp_path / "agent-runs"
    found = M.collect_transcripts(projects_root, runs_root)
    assert found == []


def test_dispatched_session_ids_reads_session_id_from_the_runs_own_transcript(M, tmp_path):
    runs_root = tmp_path / "agent-runs"
    run_dir = runs_root / "2026-09-12" / "run-abc123"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(json.dumps({"repo": "library"}))
    (run_dir / "transcript.jsonl").write_text(
        json.dumps({"type": "system", "session_id": "b964053c-dead-beef"}) + "\n"
        + json.dumps({"type": "assistant", "session_id": "b964053c-dead-beef"}) + "\n"
    )
    assert M.dispatched_session_ids(runs_root) == {"b964053c-dead-beef"}


def test_dispatched_session_ids_skips_a_transcript_with_no_session_id_line(M, tmp_path):
    runs_root = tmp_path / "agent-runs"
    run_dir = runs_root / "2026-09-12" / "run-empty"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(json.dumps({"repo": "library"}))
    (run_dir / "transcript.jsonl").write_text("not json\n")
    assert M.dispatched_session_ids(runs_root) == set()


def test_dispatched_session_ids_empty_when_runs_root_missing(M, tmp_path):
    assert M.dispatched_session_ids(tmp_path / "nope") == set()


# --- dispatched_session_ids: a private run's transcript is never opened ----


def test_a_private_runs_transcript_is_never_opened(M, tmp_path, monkeypatch):
    """PR #558 round 2, security-reviewer: before this fix, every dispatched
    run's `transcript.jsonl` was opened with no privacy check at all,
    breaking the module's own invariant of deciding privacy before reading
    content. Fails before the fix: `transcript_path` shows up in `opened`,
    and the private session id leaks into the result."""
    private_slug = sorted(M.projects.private_slugs())[0]
    runs_root = tmp_path / "agent-runs"
    run_dir = runs_root / "2026-09-12" / "run-private"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(json.dumps({"repo": private_slug}))
    transcript_path = run_dir / "transcript.jsonl"
    transcript_path.write_text(json.dumps({"type": "system", "session_id": "leak-me"}) + "\n")

    opened: list[Path] = []
    original_read_text = Path.read_text

    def spy_read_text(self, *args, **kwargs):
        opened.append(self)
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", spy_read_text)
    result = M.dispatched_session_ids(runs_root)

    assert transcript_path not in opened
    assert result == set()


def test_dispatched_session_ids_treats_unreadable_meta_as_private(M, tmp_path):
    """No `meta.json` at all (or one that fails to parse) must fail closed:
    unreadable must mean refused, never a default to public."""
    runs_root = tmp_path / "agent-runs"
    run_dir = runs_root / "2026-09-12" / "run-nometa"
    run_dir.mkdir(parents=True)
    (run_dir / "transcript.jsonl").write_text(
        json.dumps({"type": "system", "session_id": "should-not-leak"}) + "\n"
    )
    assert M.dispatched_session_ids(runs_root) == set()


# --- private dispatch into a PUBLIC worktree: window-based exclusion -------


def test_parse_iso_tolerates_z_suffix_and_fractional_seconds(M):
    assert M._parse_iso("2026-09-12T10:00:00Z") is not None
    assert M._parse_iso("2026-06-24T21:56:38.643Z") is not None
    assert M._parse_iso(None) is None
    assert M._parse_iso("not a timestamp") is None


def _existing_projects_root(M, tmp_path, slug="library"):
    """A `projects_root` with `slug`'s registered directory already created —
    the "existing transcript directory" `_private_dispatch_exclusions` checks
    a candidate `worktree` against before trusting or fully excluding it."""
    projects_root = tmp_path / "claude-projects"
    dirname = _registered_dirname(M, slug)
    (projects_root / dirname).mkdir(parents=True)
    return projects_root, dirname


def test_private_dispatch_exclusions_skips_a_public_run(M, tmp_path):
    runs_root = tmp_path / "agent-runs"
    run_dir = runs_root / "2026-09-12" / "run-public"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(json.dumps({
        "repo": "library", "worktree": str(M.projects.home_path("library")),
        "started_at": "2026-09-12T10:00:00Z", "ended_at": "2026-09-12T10:05:00Z",
    }))
    result = M._private_dispatch_exclusions(runs_root, tmp_path / "claude-projects")
    assert result.windows == {}
    assert result.full_exclude_dirs == set()
    assert result.unplaceable_count == 0


def test_private_dispatch_exclusions_applies_margin_and_falls_back_to_now(M, tmp_path):
    """A private run with no `ended_at` (still running, or abandoned) gets a
    window open-ended toward `now`, not one that silently closes at
    `started_at` — the window must stay wide enough to still exclude a
    transcript file created after this test runs but before the real run
    actually finished. Missing `ended_at` is the legitimate "still running"
    case, not implausible metadata — contrast with the present-but-garbage
    `ended_at` case below, which IS implausible.

    `started_at` is computed relative to the real `now` (an hour ago) rather
    than a fixed literal: fixed against a `now` that keeps moving, the
    literal eventually falls outside the 24-hour plausibility bound below
    and this test would start failing for a reason that has nothing to do
    with what it's testing."""
    private_slug = sorted(M.projects.private_slugs())[0]
    projects_root, dirname = _existing_projects_root(M, tmp_path)
    runs_root = tmp_path / "agent-runs"
    run_dir = runs_root / "2026-09-12" / "run-x"
    run_dir.mkdir(parents=True)
    worktree = str(M.projects.home_path("library"))
    started_at = (M.datetime.now(M.timezone.utc) - M.timedelta(hours=1)).isoformat()
    (run_dir / "meta.json").write_text(json.dumps({
        "repo": private_slug, "worktree": worktree,
        "started_at": started_at,
    }))
    result = M._private_dispatch_exclusions(runs_root, projects_root)
    assert dirname in result.windows
    assert result.full_exclude_dirs == set()
    assert result.unplaceable_count == 0
    (start, end) = result.windows[dirname][0]
    started = M._parse_iso(started_at)
    margin = M.timedelta(seconds=M.PRIVATE_DISPATCH_WINDOW_MARGIN_SECONDS)
    assert start == started - margin
    assert end > started  # fell back to "now" + margin, not started_at alone


def test_private_dispatch_exclusions_counts_a_run_with_no_worktree_as_unplaceable(M, tmp_path):
    private_slug = sorted(M.projects.private_slugs())[0]
    runs_root = tmp_path / "agent-runs"
    run_dir = runs_root / "2026-09-12" / "run-no-worktree"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(json.dumps({
        "repo": private_slug, "started_at": "2026-09-12T10:00:00Z",
    }))
    result = M._private_dispatch_exclusions(runs_root, tmp_path / "claude-projects")
    assert result.windows == {}
    assert result.full_exclude_dirs == set()
    assert result.unplaceable_count == 1


def test_private_dispatch_exclusions_counts_unresolvable_worktree_as_unplaceable(M, tmp_path):
    """A `worktree` that sanitizes to a dirname with no matching directory
    under `projects_root` at all — there is nothing to fail closed AGAINST,
    so this is "no usable directory" (unplaceable), not "implausible
    metadata" (full_exclude), even though the timestamps here are fine."""
    private_slug = sorted(M.projects.private_slugs())[0]
    runs_root = tmp_path / "agent-runs"
    run_dir = runs_root / "2026-09-12" / "run-nonexistent-dir"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(json.dumps({
        "repo": private_slug, "worktree": "/nowhere/does/not/exist",
        "started_at": "2026-09-12T10:00:00Z", "ended_at": "2026-09-12T10:05:00Z",
    }))
    result = M._private_dispatch_exclusions(runs_root, tmp_path / "claude-projects")
    assert result.windows == {}
    assert result.full_exclude_dirs == set()
    assert result.unplaceable_count == 1


def test_private_dispatch_exclusions_counts_unreadable_meta_as_unplaceable(M, tmp_path):
    """`_run_is_private` already treats unreadable `meta.json` as private
    (unreadable must mean refused); this function must not then silently
    contribute nothing for it — there's no worktree to build a window or a
    full-exclude from, so it's unplaceable, counted rather than dropped."""
    runs_root = tmp_path / "agent-runs"
    run_dir = runs_root / "2026-09-12" / "run-nometa"
    run_dir.mkdir(parents=True)
    result = M._private_dispatch_exclusions(runs_root, tmp_path / "claude-projects")
    assert result.windows == {}
    assert result.full_exclude_dirs == set()
    assert result.unplaceable_count == 1


def test_private_dispatch_exclusions_fails_closed_on_unparseable_started_at(M, tmp_path):
    """PR #558 round 5, security-reviewer: an unparseable `started_at`
    previously contributed NO window at all (`continue`), silently admitting
    this run's transcript with nothing excluding it. Now the whole candidate
    directory is excluded instead, since no window can be built."""
    private_slug = sorted(M.projects.private_slugs())[0]
    projects_root, dirname = _existing_projects_root(M, tmp_path)
    runs_root = tmp_path / "agent-runs"
    run_dir = runs_root / "2026-09-12" / "run-bad-started"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(json.dumps({
        "repo": private_slug, "worktree": str(M.projects.home_path("library")),
        "started_at": "not-a-timestamp",
    }))
    result = M._private_dispatch_exclusions(runs_root, projects_root)
    assert result.windows == {}
    assert result.full_exclude_dirs == {dirname}
    assert result.unplaceable_count == 0


def test_private_dispatch_exclusions_fails_closed_on_garbage_ended_at(M, tmp_path):
    """`ended_at` PRESENT but unparseable is a worse signal than the field
    being genuinely absent (which legitimately falls back to `now` above) —
    this must fail closed, not silently fall back the same way."""
    private_slug = sorted(M.projects.private_slugs())[0]
    projects_root, dirname = _existing_projects_root(M, tmp_path)
    runs_root = tmp_path / "agent-runs"
    run_dir = runs_root / "2026-09-12" / "run-bad-ended"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(json.dumps({
        "repo": private_slug, "worktree": str(M.projects.home_path("library")),
        "started_at": "2026-09-12T10:00:00Z", "ended_at": "not-a-timestamp",
    }))
    result = M._private_dispatch_exclusions(runs_root, projects_root)
    assert result.windows == {}
    assert result.full_exclude_dirs == {dirname}


def test_private_dispatch_exclusions_fails_closed_when_ended_precedes_started(M, tmp_path):
    private_slug = sorted(M.projects.private_slugs())[0]
    projects_root, dirname = _existing_projects_root(M, tmp_path)
    runs_root = tmp_path / "agent-runs"
    run_dir = runs_root / "2026-09-12" / "run-backwards"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(json.dumps({
        "repo": private_slug, "worktree": str(M.projects.home_path("library")),
        "started_at": "2026-09-12T10:05:00Z", "ended_at": "2026-09-12T10:00:00Z",
    }))
    result = M._private_dispatch_exclusions(runs_root, projects_root)
    assert result.windows == {}
    assert result.full_exclude_dirs == {dirname}


def test_private_dispatch_exclusions_fails_closed_when_window_exceeds_24_hours(M, tmp_path):
    private_slug = sorted(M.projects.private_slugs())[0]
    projects_root, dirname = _existing_projects_root(M, tmp_path)
    runs_root = tmp_path / "agent-runs"
    run_dir = runs_root / "2026-09-12" / "run-too-wide"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(json.dumps({
        "repo": private_slug, "worktree": str(M.projects.home_path("library")),
        "started_at": "2026-09-10T10:00:00Z", "ended_at": "2026-09-12T10:00:01Z",
    }))
    result = M._private_dispatch_exclusions(runs_root, projects_root)
    assert result.windows == {}
    assert result.full_exclude_dirs == {dirname}


def test_private_dispatch_exclusions_accepts_a_window_just_under_24_hours(M, tmp_path):
    """The 24-hour bound is a sanity check on implausible metadata, not a
    trap for a real, merely long-running dispatch — a window just under the
    limit must still be trusted as a normal time-bounded window."""
    private_slug = sorted(M.projects.private_slugs())[0]
    projects_root, dirname = _existing_projects_root(M, tmp_path)
    runs_root = tmp_path / "agent-runs"
    run_dir = runs_root / "2026-09-12" / "run-long-but-ok"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(json.dumps({
        "repo": private_slug, "worktree": str(M.projects.home_path("library")),
        "started_at": "2026-09-11T10:00:00Z", "ended_at": "2026-09-12T09:00:00Z",
    }))
    result = M._private_dispatch_exclusions(runs_root, projects_root)
    assert dirname in result.windows
    assert result.full_exclude_dirs == set()


def test_file_created_at_reads_stat_not_content(M, tmp_path):
    p = tmp_path / "f.jsonl"
    p.write_text("{}\n")
    created = M._file_created_at(p)
    assert created is not None
    assert abs((M.datetime.now(M.timezone.utc) - created).total_seconds()) < 60


def test_file_created_at_falls_back_to_mtime_when_birthtime_unavailable(M, tmp_path, monkeypatch):
    p = tmp_path / "f.jsonl"
    p.write_text("{}\n")

    class _FakeStat:
        st_mtime = 1_700_000_000.0

    monkeypatch.setattr(Path, "stat", lambda self: _FakeStat())
    created = M._file_created_at(p)
    assert created.timestamp() == pytest.approx(1_700_000_000.0)


def test_file_created_at_none_when_unstatable(M, tmp_path):
    assert M._file_created_at(tmp_path / "does-not-exist.jsonl") is None


def test_created_within_windows_treats_an_unstatable_file_as_inside(M, tmp_path):
    """Unreadable must mean refused, applied to the one other place this
    module decides privacy from something other than content: a file that
    can't even be stat'd is excluded rather than let through."""
    windows = [(M.datetime(2026, 1, 1, tzinfo=M.timezone.utc),
                M.datetime(2026, 1, 2, tzinfo=M.timezone.utc))]
    assert M._created_within_windows(tmp_path / "gone.jsonl", windows) is True


def test_private_dispatch_into_a_public_worktree_excludes_its_transcript_unopened(
    M, tmp_path, monkeypatch,
):
    """PR #558 round 3, security-reviewer: `agentctl spawn <agent> --on
    <public-repo> --agents-dir <path under a private crew's home>` resolves
    `worktree` to the public repo while `agent_source` alone makes
    `_run_is_private` correctly flag the run private. Its own `claude`
    process writes its transcript under that PUBLIC directory, passing both
    the black-box and allowlist checks, and `dispatched_session_ids` can't
    exclude it by session id — learning that id would require opening the
    very `transcript.jsonl` privacy forbids reading. Fails before the fix:
    the leaked file is admitted (and would go on to be opened later in the
    pipeline), since nothing here excluded it.

    `_file_created_at` is monkeypatched rather than backdating real file
    timestamps: `st_birthtime` on macOS/APFS reflects true filesystem
    creation and isn't settable via `os.utime`, so real-timestamp backdating
    can't deterministically place a freshly-written test file inside an
    arbitrary past window."""
    private_slug = sorted(M.projects.private_slugs())[0]
    projects_root = tmp_path / "claude-projects"
    dirname = _registered_dirname(M, "library")
    product_dir = projects_root / dirname
    product_dir.mkdir(parents=True)

    leaked_path = product_dir / "c1a2b3c4-dead-beef-0000-111122223333.jsonl"
    _write_transcript(leaked_path, [_assistant("leak", input_tokens=10)])
    kept_path = product_dir / "interactive-session.jsonl"
    _write_transcript(kept_path, [_assistant("keep", input_tokens=20)])

    runs_root = tmp_path / "agent-runs"
    run_dir = runs_root / "2026-09-12" / "run-private-on-public"
    run_dir.mkdir(parents=True)
    agent_source = str(M.projects.home_path(private_slug) / ".claude" / "agents" / "coder.md")
    (run_dir / "meta.json").write_text(json.dumps({
        "repo": "library",
        "agent_source": agent_source,
        "worktree": str(M.projects.home_path("library")),
        "started_at": "2026-09-12T10:00:00Z",
        "ended_at": "2026-09-12T10:05:00Z",
    }))
    # No transcript.jsonl for this run at all: `_run_is_private` must decide
    # privacy from meta.json alone, and this fix must never need to open one.

    def fake_created_at(path):
        if path.name == leaked_path.name:
            return M._parse_iso("2026-09-12T10:02:00Z")   # inside the window
        return M._parse_iso("2026-09-12T09:00:00Z")        # well outside it
    monkeypatch.setattr(M, "_file_created_at", fake_created_at)

    opened: list[Path] = []
    original_read_text = Path.read_text

    def spy_read_text(self, *args, **kwargs):
        opened.append(self)
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", spy_read_text)
    found = M.collect_transcripts(projects_root, runs_root)

    assert [p.name for p in found] == ["interactive-session.jsonl"]
    assert leaked_path not in opened


def test_a_private_run_dispatched_into_public_worktree_with_no_matching_window_is_kept(
    M, tmp_path, monkeypatch,
):
    """The window exclusion is bounded by TIME, not just by directory: an
    interactive transcript in the same directory as a private run's worktree,
    but created outside that run's [started_at, ended_at] window (plus
    margin), must stay in."""
    private_slug = sorted(M.projects.private_slugs())[0]
    projects_root = tmp_path / "claude-projects"
    dirname = _registered_dirname(M, "library")
    product_dir = projects_root / dirname
    product_dir.mkdir(parents=True)

    unrelated_path = product_dir / "unrelated-session.jsonl"
    _write_transcript(unrelated_path, [_assistant("a1", input_tokens=10)])

    runs_root = tmp_path / "agent-runs"
    run_dir = runs_root / "2026-09-12" / "run-private-on-public"
    run_dir.mkdir(parents=True)
    agent_source = str(M.projects.home_path(private_slug) / ".claude" / "agents" / "coder.md")
    (run_dir / "meta.json").write_text(json.dumps({
        "repo": "library",
        "agent_source": agent_source,
        "worktree": str(M.projects.home_path("library")),
        "started_at": "2026-09-12T10:00:00Z",
        "ended_at": "2026-09-12T10:05:00Z",
    }))

    monkeypatch.setattr(M, "_file_created_at",
                        lambda path: M._parse_iso("2026-09-12T04:00:00Z"))

    found = M.collect_transcripts(projects_root, runs_root)
    assert [p.name for p in found] == ["unrelated-session.jsonl"]


# --- end to end: collect + analyze + report ---------------------------------


def test_dispatched_run_excludes_only_its_own_session_not_the_whole_directory(M, tmp_path):
    """PR #558 round 1, reviewer 2026-09-13 01:1x: `kb-sync-verifier` targets
    a product's PRIMARY checkout by design, so a dispatched run's own session
    file can legitimately sit beside real interactive history in the same
    `~/.claude/projects/<dir>`. Fails before the by-session fix: excluding
    the whole directory (keyed on `worktree` alone) drops the interactive
    session too."""
    projects_root = tmp_path / "claude-projects"
    dirname = _registered_dirname(M, "library")
    product_dir = projects_root / dirname
    product_dir.mkdir(parents=True)

    dispatched_session_id = "b964053c-c995-48d6-a963-5a958e41e0e5"
    _write_transcript(product_dir / f"{dispatched_session_id}.jsonl",
                       [_assistant("a1", input_tokens=10)])
    _write_transcript(product_dir / "interactive-session.jsonl",
                       [_assistant("a2", input_tokens=20)])

    runs_root = tmp_path / "agent-runs"
    run_dir = runs_root / "2026-09-12" / "run-kbsync"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(json.dumps({"worktree": str(M.projects.home_path("library"))}))
    (run_dir / "transcript.jsonl").write_text(
        json.dumps({"type": "system", "session_id": dispatched_session_id}) + "\n"
    )

    found = M.collect_transcripts(projects_root, runs_root)
    assert [p.name for p in found] == ["interactive-session.jsonl"]


def test_build_report_reflects_the_measured_numbers(M, tmp_path):
    projects_root = tmp_path / "claude-projects"
    session_dir = projects_root / _registered_dirname(M, "library")
    session_dir.mkdir(parents=True)
    _write_transcript(session_dir / "session-a.jsonl", [
        _assistant("a1", input_tokens=100_000),
        _assistant("a2", input_tokens=900_000),
    ])
    runs_root = tmp_path / "agent-runs"

    transcripts = M.collect_transcripts(projects_root, runs_root)
    corpus = M.analyze_corpus(transcripts)
    report = M.build_report(corpus)

    assert "session-a" in report
    assert "Sessions scanned: 1" in report
    assert "800000" in report or "800,000" in report  # the second call's growth


# --- fail-closed on implausible metadata: whole-directory exclusion --------


def test_collect_transcripts_excludes_every_file_in_a_directory_with_implausible_metadata(
    M, tmp_path,
):
    """PR #558 round 5, security-reviewer: a private run with implausible
    metadata (here, an unparseable `started_at`) gets no trustworthy window,
    so `_private_dispatch_exclusions` marks its whole candidate directory for
    full exclusion — every transcript there is dropped, not just files whose
    creation time happens to fall inside a (nonexistent) window.

    PR #558 round 6, reviewer: a whole-directory drop must be visible in the
    counts as a DIRECTORY, not just as however many files happened to be in
    it — the fixture below holds two co session transcripts in the one
    implausible run's directory, and `collect_transcripts` must report
    exactly 1 excluded directory and 2 excluded files, not one combined
    number a reader can't tell apart from a few stray files."""
    private_slug = sorted(M.projects.private_slugs())[0]
    projects_root = tmp_path / "claude-projects"
    dirname = _registered_dirname(M, "vivy")
    product_dir = projects_root / dirname
    product_dir.mkdir(parents=True)
    _write_transcript(product_dir / "session-a.jsonl", [_assistant("a1", input_tokens=10)])
    _write_transcript(product_dir / "session-b.jsonl", [_assistant("a2", input_tokens=20)])

    runs_root = tmp_path / "agent-runs"
    run_dir = runs_root / "2026-09-12" / "run-implausible"
    run_dir.mkdir(parents=True)
    agent_source = str(M.projects.home_path(private_slug) / ".claude" / "agents" / "coder.md")
    (run_dir / "meta.json").write_text(json.dumps({
        "repo": "vivy", "agent_source": agent_source,
        "worktree": str(M.projects.home_path("vivy")),
        "started_at": "not-a-timestamp",
    }))

    counts = M.CollectionCounts()
    found = M.collect_transcripts(projects_root, runs_root, counts)
    assert found == []
    assert counts.implausible_metadata_dirs == 1
    assert counts.implausible_metadata == 2


def test_collect_transcripts_populates_exclusion_counts_for_each_rule(M, tmp_path, monkeypatch):
    """Every population-exclusion rule ends up as its own count, and only
    its own — the fixture below is built so each rule fires on a file (or
    run) the others can't also claim."""
    private_slug = sorted(M.projects.private_slugs())[0]
    projects_root = tmp_path / "claude-projects"

    unregistered_dir = projects_root / "-Users-operator-some-unregistered-place"
    unregistered_dir.mkdir(parents=True)
    _write_transcript(unregistered_dir / "u1.jsonl", [_assistant("u1", input_tokens=1)])
    _write_transcript(unregistered_dir / "u2.jsonl", [_assistant("u2", input_tokens=1)])

    library_dir = projects_root / _registered_dirname(M, "library")
    library_dir.mkdir(parents=True)
    dispatched_session_id = "b964053c-c995-48d6-a963-5a958e41e0e5"
    _write_transcript(library_dir / f"{dispatched_session_id}.jsonl",
                       [_assistant("d1", input_tokens=1)])
    _write_transcript(library_dir / "window-excluded.jsonl",
                       [_assistant("w1", input_tokens=1)])
    _write_transcript(library_dir / "kept.jsonl", [_assistant("k1", input_tokens=1)])

    vivy_dirname = _registered_dirname(M, "vivy")
    vivy_dir = projects_root / vivy_dirname
    vivy_dir.mkdir(parents=True)
    _write_transcript(vivy_dir / "imp1.jsonl", [_assistant("i1", input_tokens=1)])
    _write_transcript(vivy_dir / "imp2.jsonl", [_assistant("i2", input_tokens=1)])

    runs_root = tmp_path / "agent-runs"

    kbsync_run = runs_root / "2026-09-12" / "run-kbsync"
    kbsync_run.mkdir(parents=True)
    (kbsync_run / "meta.json").write_text(json.dumps({"worktree": str(M.projects.home_path("library"))}))
    (kbsync_run / "transcript.jsonl").write_text(
        json.dumps({"type": "system", "session_id": dispatched_session_id}) + "\n"
    )

    agent_source = str(M.projects.home_path(private_slug) / ".claude" / "agents" / "coder.md")
    window_run = runs_root / "2026-09-12" / "run-window"
    window_run.mkdir(parents=True)
    (window_run / "meta.json").write_text(json.dumps({
        "repo": "library", "agent_source": agent_source,
        "worktree": str(M.projects.home_path("library")),
        "started_at": "2026-09-12T10:00:00Z", "ended_at": "2026-09-12T10:05:00Z",
    }))

    implausible_run = runs_root / "2026-09-12" / "run-implausible"
    implausible_run.mkdir(parents=True)
    (implausible_run / "meta.json").write_text(json.dumps({
        "repo": "vivy", "agent_source": agent_source,
        "worktree": str(M.projects.home_path("vivy")),
        "started_at": "not-a-timestamp",
    }))

    unplaceable_run = runs_root / "2026-09-12" / "run-unplaceable"
    unplaceable_run.mkdir(parents=True)
    (unplaceable_run / "meta.json").write_text(json.dumps({
        "repo": "cfo", "agent_source": str(M.projects.home_path("cfo") / ".claude" / "agents" / "coder.md"),
    }))

    def fake_created_at(path):
        if path.name == "window-excluded.jsonl":
            return M._parse_iso("2026-09-12T10:02:00Z")   # inside the run-window window
        return M._parse_iso("2020-01-01T00:00:00Z")        # well outside it

    monkeypatch.setattr(M, "_file_created_at", fake_created_at)
    counts = M.CollectionCounts()
    found = M.collect_transcripts(projects_root, runs_root, counts)

    assert [p.name for p in found] == ["kept.jsonl"]
    assert counts.private_or_unregistered_dir == 2
    assert counts.dispatched_session == 1
    assert counts.private_dispatch_window == 1
    assert counts.implausible_metadata == 2
    assert counts.implausible_metadata_dirs == 1
    assert counts.unplaceable_private_runs == 1


def test_build_report_includes_exclusion_counts_line(M):
    corpus = M.Corpus()
    counts = M.CollectionCounts()
    counts.private_or_unregistered_dir = 3
    counts.dispatched_session = 4
    counts.private_dispatch_window = 5
    counts.implausible_metadata = 6
    counts.implausible_metadata_dirs = 2
    counts.unplaceable_private_runs = 7
    report = M.build_report(corpus, counts)
    assert "3 in a private-or-unregistered directory" in report
    assert "4 by dispatched-session id" in report
    assert "5 transcript file(s) by a private-dispatch window" in report
    assert "2 whole directories fully excluded" in report
    assert "6 transcript file(s) dropped as a result" in report
    assert "7 private run(s) had no directory" in report


def test_build_report_singularizes_one_excluded_directory(M):
    """PR #558 round 6, reviewer: the whole-directory-drop line must read as
    a distinct, prominent event even at n=1 — 'directory', not 'directories'."""
    corpus = M.Corpus()
    counts = M.CollectionCounts()
    counts.implausible_metadata_dirs = 1
    counts.implausible_metadata = 2
    report = M.build_report(corpus, counts)
    assert "1 whole directory fully excluded" in report
    assert "directories" not in report


def test_build_report_omits_the_exclusion_line_when_counts_not_supplied(M):
    """`counts` is optional — a caller that doesn't track exclusions (e.g. a
    future direct `Corpus`/`build_report` use outside `main()`) gets the
    same report shape as before, not a crash on a missing argument. This is a
    backward-compatibility guard, not a regression test for a specific bug:
    `build_report(corpus)` took no `counts` parameter at all before PR #558
    round 5, so it never printed this text either — this test passes
    unchanged against that pre-round-5 code, and exists to keep the
    zero-argument call working going forward rather than to have caught the
    round-5 bug itself."""
    corpus = M.Corpus()
    report = M.build_report(corpus)
    assert "private-or-unregistered directory" not in report


def test_report_never_names_the_session_or_directory_a_privacy_rule_excluded(M, tmp_path):
    """PR #558 round 5, security-reviewer: confirm end-to-end that excluding
    a transcript by any privacy rule never leaks its session id or directory
    into the report — only counts do. The admitted session's own id, by
    contrast, IS expected in the largest-jump line (section 2)."""
    private_slug = sorted(M.projects.private_slugs())[0]
    projects_root = tmp_path / "claude-projects"

    kept_dirname = _registered_dirname(M, "library")
    kept_dir = projects_root / kept_dirname
    kept_dir.mkdir(parents=True)
    admitted_session_id = "admitted-session-0000"
    _write_transcript(kept_dir / f"{admitted_session_id}.jsonl", [
        _assistant("a1", input_tokens=500_000),
        _assistant("a2", input_tokens=900_000),
    ])

    excluded_dirname = _registered_dirname(M, "vivy")
    excluded_dir = projects_root / excluded_dirname
    excluded_dir.mkdir(parents=True)
    excluded_session_id = "excluded-session-9999"
    _write_transcript(excluded_dir / f"{excluded_session_id}.jsonl",
                       [_assistant("e1", input_tokens=10)])

    runs_root = tmp_path / "agent-runs"
    run_dir = runs_root / "2026-09-12" / "run-implausible"
    run_dir.mkdir(parents=True)
    agent_source = str(M.projects.home_path(private_slug) / ".claude" / "agents" / "coder.md")
    (run_dir / "meta.json").write_text(json.dumps({
        "repo": "vivy", "agent_source": agent_source,
        "worktree": str(M.projects.home_path("vivy")),
        "started_at": "not-a-timestamp",
    }))

    counts = M.CollectionCounts()
    transcripts = M.collect_transcripts(projects_root, runs_root, counts)
    corpus = M.analyze_corpus(transcripts)
    report = M.build_report(corpus, counts)

    assert admitted_session_id in report
    assert excluded_session_id not in report
    assert excluded_dirname not in report
    assert counts.implausible_metadata == 1
