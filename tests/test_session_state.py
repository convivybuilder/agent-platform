"""The `held` field — what a working session is holding in its queue.

Slice B of the input-robustness plan (`convivy-deck/design/input-robustness-plan.md`).
Slice A measured live against Claude Code 2.1.226 that a message leaves the input
box on Enter and lives in the queue, so `fold_queue` folds the transcript's
`queue-operation` records rather than reading the pane. Several fixtures below are
drawn verbatim from that live measurement (a throwaway tmux probe session,
sessionId `f692ebda-0069-44a2-815f-07f2efc7e2ae`) and from the wider corpus under
`~/.claude/projects`, rather than hand-invented shapes, because `session_input`'s
own docstring records this exact reasoning getting the answer wrong three times
before it was checked against something real.

Three groups:

  * `fold_queue` — the pure fold, tested against real record sequences.
  * `_read_held_queue` — the bounded tail reader, tested against real files.
  * `session_state()` / `follow_states()` / `session_states()` — the field's wiring
    into the state object: gated on `working` and not `brief`, significant to a
    follower, and consistent on the rail's hand-built failure path.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parent.parent / "lib" / "agent-platform"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, LIB / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def SS():
    return _load("session_state")


@pytest.fixture(autouse=True)
def _isolated_slate_home(tmp_path, monkeypatch):
    """`session_state()`/`session_states()` now resolve `asks_open`/
    `asks_answered` through `slate.ask_counts` unconditionally (CQ-358,
    platform/decisions/0091), and `slate.state_root()` re-reads `Path.home()`
    on every call rather than being a patchable module-level constant.
    Without this, the `session_state()` calls in the MARK blocks below would
    create `~/.local/state/convivy/slate/<name>.json` on whatever machine
    runs this suite."""
    monkeypatch.setenv("HOME", str(tmp_path))


# MARK: - fold_queue, the pure fold


def test_a_simple_enqueue_dequeue_pair_empties_the_queue(SS):
    """Real pair, hollis session 48ad3b41-d2f8-4009-8d6c-09254f8952a7, one
    millisecond apart: the dispatch prompt landed at an idle agent and was
    consumed immediately. `[]` is "nothing held, positively known", not None —
    the queue really did go to empty."""
    records = [
        {"type": "queue-operation", "operation": "enqueue",
         "timestamp": "2026-07-27T17:51:27.045Z",
         "sessionId": "48ad3b41-d2f8-4009-8d6c-09254f8952a7",
         "content": "Task: Drain the in-box issue queue for hollis end-to-end."},
        {"type": "queue-operation", "operation": "dequeue",
         "timestamp": "2026-07-27T17:51:27.046Z",
         "sessionId": "48ad3b41-d2f8-4009-8d6c-09254f8952a7"},
    ]
    assert SS.fold_queue(records) == []


def test_the_batch_drain_pops_one_per_dequeue_record(SS):
    """THE SLICE A MEASUREMENT ITSELF. Two messages queued 15s apart produced two
    `dequeue` records at the SAME millisecond, delivered in one reply — "the
    release is one event for the whole queue, not one per message." Folding each
    `dequeue` as one pop already drains both correctly; nothing here needs to
    notice the shared timestamp to get the right answer."""
    records = [
        {"type": "queue-operation", "operation": "enqueue",
         "timestamp": "2026-08-10T00:53:19.090Z",
         "sessionId": "f692ebda-0069-44a2-815f-07f2efc7e2ae",
         "content": "FIRST-HELD-MESSAGE-alpha"},
        {"type": "queue-operation", "operation": "enqueue",
         "timestamp": "2026-08-10T00:53:34.182Z",
         "sessionId": "f692ebda-0069-44a2-815f-07f2efc7e2ae",
         "content": "SECOND-HELD-MESSAGE-bravo"},
        {"type": "queue-operation", "operation": "dequeue",
         "timestamp": "2026-08-10T00:53:40.228Z",
         "sessionId": "f692ebda-0069-44a2-815f-07f2efc7e2ae"},
        {"type": "queue-operation", "operation": "dequeue",
         "timestamp": "2026-08-10T00:53:40.228Z",
         "sessionId": "f692ebda-0069-44a2-815f-07f2efc7e2ae"},
    ]
    assert SS.fold_queue(records) == []


def test_popall_with_two_messages_empties_the_queue(SS):
    """THE SLICE A MEASUREMENT ITSELF, the other half: Up-arrow on two queued
    messages emitted one `popAll` record PER MESSAGE, each carrying its content,
    both at the same timestamp — `popAll 'DELTA-one'` and `popAll 'ECHO-two'`.
    The content is irrelevant to the fold: the messages return to the composer,
    not to `held`, so all `popAll` needs to do is clear the queue."""
    records = [
        {"type": "queue-operation", "operation": "enqueue",
         "timestamp": "2026-08-10T00:55:06.755Z",
         "sessionId": "f692ebda-0069-44a2-815f-07f2efc7e2ae", "content": "DELTA-one"},
        {"type": "queue-operation", "operation": "enqueue",
         "timestamp": "2026-08-10T00:55:08.810Z",
         "sessionId": "f692ebda-0069-44a2-815f-07f2efc7e2ae", "content": "ECHO-two"},
        {"type": "queue-operation", "operation": "popAll",
         "timestamp": "2026-08-10T00:55:16.321Z",
         "sessionId": "f692ebda-0069-44a2-815f-07f2efc7e2ae", "content": "DELTA-one"},
        {"type": "queue-operation", "operation": "popAll",
         "timestamp": "2026-08-10T00:55:16.321Z",
         "sessionId": "f692ebda-0069-44a2-815f-07f2efc7e2ae", "content": "ECHO-two"},
    ]
    assert SS.fold_queue(records) == []


def test_a_bare_remove_returns_none(SS):
    """The 2.1.202 shape: no index, no content, so which entry left is
    unknowable and the honest failure is the only answer.

    THIS DOCSTRING USED TO CLAIM 7,552 OF THESE, AND THAT WAS THE BUG. It
    counted every `remove` record and took the contentless shape for all of
    them. Re-measured 2026-08-10, split by the version that wrote each record:
    every contentless one was 2.1.202, and every `remove` from 2.1.203 through
    2.1.226 named its entry, with no exception either side. Generalising the old
    shape to the whole vocabulary made the fold refuse on the ordinary release
    path — see `test_a_remove_naming_its_entry_drops_that_entry`."""
    records = [
        {"type": "queue-operation", "operation": "enqueue",
         "timestamp": "2026-07-07T03:30:00.000Z",
         "sessionId": "d8054dbc-0352-400d-8254-cae96b47b428", "content": "keep this"},
        {"type": "queue-operation", "operation": "remove",
         "timestamp": "2026-07-07T03:33:35.203Z",
         "sessionId": "d8054dbc-0352-400d-8254-cae96b47b428"},
    ]
    assert SS.fold_queue(records) is None


def test_a_dequeue_against_an_empty_queue_means_a_mid_stream_tail(SS):
    """A bounded tail read can start after an `enqueue` that fell outside the
    window, so the first record it sees is a `dequeue` with nothing to pop. That
    is not a crash and not zero items removed — it is "the queue's true state is
    unknown from here", the same answer `remove` gives."""
    records = [
        {"type": "queue-operation", "operation": "dequeue",
         "timestamp": "2026-07-27T17:51:27.046Z",
         "sessionId": "48ad3b41-d2f8-4009-8d6c-09254f8952a7"},
    ]
    assert SS.fold_queue(records) is None


def test_no_queue_operations_at_all_is_positively_empty(SS):
    """The distinction the field exists to draw: nothing held is known, not
    unknown. A tail holding no queue-operation records at all is exactly that."""
    assert SS.fold_queue([]) == []


def test_an_unrecognised_operation_is_left_inert(SS):
    """Forward-compatible with an operation Anthropic adds and this vocabulary
    does not yet name — the same answer `_state_for` gives an unrecognised hook
    event, rather than treating it as an honest failure it is not."""
    records = [
        {"operation": "enqueue", "content": "kept"},
        {"operation": "somethingNew"},
    ]
    assert SS.fold_queue(records) == ["kept"]


def test_enqueue_without_usable_content_refuses(SS):
    """A skip here would be the one failure worse than refusing.

    Dropping the entry leaves the fold one short for the rest of the read, and
    nothing announces the shortfall: later `dequeue` records pop the wrong
    entries until the fold reports an empty queue over a message that is still
    genuinely held. `[]` is a positive claim that nothing is waiting, so the
    caller believes it."""
    assert SS.fold_queue([{"operation": "enqueue", "content": 12345}]) is None
    assert SS.fold_queue([{"operation": "enqueue"}]) is None


def test_a_malformed_enqueue_does_not_silently_shorten_the_queue(SS):
    """The sequence that proves it, from the review: a bad record between two
    good ones. Skipping it would fold to `[]` — nothing waiting — while `bravo`
    is still in the session's queue."""
    records = [
        {"operation": "enqueue", "content": "alpha"},
        {"operation": "enqueue", "content": None},
        {"operation": "enqueue", "content": "bravo"},
        {"operation": "dequeue"},
        {"operation": "dequeue"},
    ]
    assert SS.fold_queue(records) is None


def test_a_contentless_remove_refuses_even_with_one_entry_folded(SS):
    """Narrowing this to "one entry means unambiguous" was suggested and
    declined: it is unambiguous only if this fold's count matches the real
    queue's, and a bounded tail opening after an earlier `enqueue` sees one
    where the session holds two. That argument is about POSITION and still
    stands here, where nothing names the entry."""
    records = [
        {"operation": "enqueue", "content": "only-one-we-can-see"},
        {"operation": "remove"},
    ]
    assert SS.fold_queue(records) is None


def test_a_remove_naming_its_entry_drops_that_entry(SS):
    """The ordinary release path on 2.1.226, and the one this fold used to
    refuse. Verbatim from a live transcript, 2026-08-10."""
    records = [
        {"type": "queue-operation", "operation": "enqueue",
         "timestamp": "2026-08-10T13:45:31.836Z",
         "sessionId": "f41e1486-41e3-4507-901e-29daa7d3a9b6",
         "content": "This is the test"},
        {"type": "queue-operation", "operation": "remove",
         "timestamp": "2026-08-10T13:45:35.651Z",
         "sessionId": "f41e1486-41e3-4507-901e-29daa7d3a9b6",
         "content": "This is the test"},
    ]
    assert SS.fold_queue(records) == []


def test_a_release_drains_one_message_per_remove_not_the_whole_queue(SS):
    """Two queued at once, released one at a time — the sequence observed at
    lines 142-145 of the same transcript.

    THIS CORRECTS THE FEATURE'S FOUNDING MEASUREMENT. Slice A recorded the queue
    draining as one batch, and `_held_row_key`'s comment still describes a
    release that way. 2.1.226 emits one `remove` per message, so the queue is
    briefly one-shorter rather than empty, and the pane draws the remainder for
    that interval."""
    records = [
        {"operation": "enqueue", "content": "first"},
        {"operation": "enqueue", "content": "second"},
        {"operation": "remove", "content": "first"},
    ]
    assert SS.fold_queue(records) == ["second"]


def test_a_remove_naming_an_entry_outside_the_window_refuses(SS):
    """The mid-stream tail, reached through `remove` rather than `dequeue`. The
    entry named was enqueued before this read's window opened, so the fold
    cannot know what else it is missing and must not claim the queue is empty."""
    records = [
        {"operation": "enqueue", "content": "visible"},
        {"operation": "remove", "content": "enqueued before the window opened"},
    ]
    assert SS.fold_queue(records) is None


def test_a_truncated_window_refuses_a_content_matched_remove(SS):
    """THE CASE SECURITY REVIEW FOUND, and the reason `window_complete` exists.

    An entry queued before this window opened is still held, and a later entry
    inside the window carries identical text. The `remove` releases the older
    one, which this fold cannot see, so matching on content alone would consume
    the only copy it has and report an empty queue over a message that is
    genuinely still waiting — a confident wrong answer about emptiness, which
    the `enqueue` branch calls worse than any answer of "unknown"."""
    records = [
        {"operation": "enqueue", "content": "ok"},
        {"operation": "remove", "content": "ok"},
    ]
    assert SS.fold_queue(records, window_complete=False) is None
    # The same records read from a complete window are unambiguous: there is no
    # earlier entry for the `remove` to have meant.
    assert SS.fold_queue(records, window_complete=True) == []


def test_a_popall_establishes_sync_inside_a_truncated_window(SS):
    """`popAll` empties the real queue, not merely this fold's view of it, so
    whatever the window missed is gone from the session too. It is the one
    record that earns a truncated window the right to trust a later match."""
    records = [
        {"operation": "enqueue", "content": "from before we could see clearly"},
        {"operation": "popAll", "content": "from before we could see clearly"},
        {"operation": "enqueue", "content": "alpha"},
        {"operation": "enqueue", "content": "bravo"},
        {"operation": "remove", "content": "alpha"},
    ]
    assert SS.fold_queue(records, window_complete=False) == ["bravo"]


def test_window_complete_defaults_true_so_a_whole_file_needs_no_ceremony(SS):
    """Every caller holding a complete record list gets the exact fold without
    saying so; only the bounded-tail readers have to opt out."""
    records = [
        {"operation": "enqueue", "content": "x"},
        {"operation": "remove", "content": "x"},
    ]
    assert SS.fold_queue(records) == []


def test_one_release_does_not_blind_the_fold_for_the_rest_of_the_session(SS):
    """The regression this whole change exists for, and the shape of the defect
    the operator hit by hand on 2026-08-10.

    A blanket refusal on `remove` was not a one-poll miss. The record stays
    inside the bounded tail for the rest of the session, so every later fold
    re-read it and returned None again: the first held message drew for a second
    and vanished, and no later one drew at all. What proves the fix is the
    SECOND message being visible after the first has come and gone."""
    records = [
        {"operation": "enqueue", "content": "first message"},
        {"operation": "remove", "content": "first message"},
        {"operation": "enqueue", "content": "second message"},
    ]
    assert SS.fold_queue(records) == ["second message"]


# `hop-chain="…"` is a relay session (CQ·361): Claude Code keeps it on a
# cross-session `enqueue` and drops it on the `remove` that releases the same
# message, so a straight content comparison between the two can never match.
_ENQUEUED_WITH_HOP_CHAIN = (
    '<cross-session-message from="uds:/tmp/cc-socks/36714.sock" '
    'hop-chain="bbe9b1b67f53afd2802fd396" from-name="convivy" '
    'from-mode="bypass">\nSteward, for CQ·359 …'
)
_RELEASED_WITHOUT_HOP_CHAIN = (
    '<cross-session-message from="uds:/tmp/cc-socks/36714.sock" '
    'from-name="convivy" from-mode="bypass">\nSteward, for CQ·359 …'
)
_LATER_ENQUEUED_WITH_HOP_CHAIN = (
    '<cross-session-message from="uds:/tmp/cc-socks/36714.sock" '
    'hop-chain="deadbeefdeadbeefdeadbeef" from-name="convivy" '
    'from-mode="bypass">\nSteward, a later message.'
)


def test_a_remove_without_hop_chain_matches_the_enqueue_that_had_one(SS):
    """CQ·361 regression, from Bosun's own transcript
    72ff834d-7836-4fd2-993f-e8dd5306cf2f.jsonl, lines 153/156
    (2026-10-01T22:09:20.734Z enqueue, 22:11:10.376Z remove): the `remove`
    record is the same message with `hop-chain="…"` stripped from its opening
    tag by Claude Code itself, not a record this fold failed to see. Matched
    on raw content the two never agree, and because the window stays complete
    the unmatched `remove` sits inside every later read — `held` went null for
    the rest of the session, not just one poll."""
    records = [
        {"operation": "enqueue", "content": _ENQUEUED_WITH_HOP_CHAIN},
        {"operation": "remove", "content": _RELEASED_WITHOUT_HOP_CHAIN},
    ]
    assert SS.fold_queue(records) == []
    # A later message, also carrying its own hop-chain, is held and returned
    # EXACTLY as enqueued — this match never rewrites what Deck draws.
    records.append({"operation": "enqueue", "content": _LATER_ENQUEUED_WITH_HOP_CHAIN})
    assert SS.fold_queue(records) == [_LATER_ENQUEUED_WITH_HOP_CHAIN]


def test_a_cross_session_message_without_hop_chain_on_either_side_still_matches(SS):
    """Unchanged behaviour: a relay message released by a Claude Code build
    that never stamped `hop-chain` at all still matches on content exactly as
    it did before this fix — the normalisation is a no-op when there is
    nothing to strip."""
    content = ('<cross-session-message from="uds:/tmp/cc-socks/1.sock" '
               'from-name="convivy" from-mode="bypass">\nordinary release, no hop-chain')
    records = [
        {"operation": "enqueue", "content": content},
        {"operation": "remove", "content": content},
    ]
    assert SS.fold_queue(records) == []


def test_queue_match_key_strips_hop_chain_from_the_opening_tag_only(SS):
    """The normalisation `_fold_queue` and `_fold_queue_with_timestamps` both
    route through, pinned directly: stripping `hop-chain="…"` (and the space
    before it) from the enqueued form must produce exactly the released
    form."""
    assert (SS._queue_match_key(_ENQUEUED_WITH_HOP_CHAIN)
            == _RELEASED_WITHOUT_HOP_CHAIN
            == SS._queue_match_key(_RELEASED_WITHOUT_HOP_CHAIN))


def test_a_literal_hop_chain_looking_string_in_the_message_body_is_untouched(SS):
    """The strip is scoped to the opening `<cross-session-message …>` tag,
    never the body that follows it. A body that happens to contain the exact
    text `hop-chain="…"` — someone quoting this very attribute back, say —
    must compare unchanged, or two genuinely different messages could be
    folded as though they were the same one."""
    content = ('<cross-session-message from="x">\n'
               'Keep this exact text: hop-chain="should-not-be-touched" verbatim.')
    assert SS._queue_match_key(content) == content


# MARK: - _fold_queue_with_timestamps, _fold_queue's timestamped mirror


def test_the_hop_chain_mismatch_also_matches_through_the_timestamped_fold(SS):
    """Same CQ·361 regression as `test_a_remove_without_hop_chain_matches_the_
    enqueue_that_had_one`, through the SEPARATE fold `held_items` and
    `co_rollover.forward_late_messages` depend on — the two folds share the
    same normalisation helper and must agree."""
    records = [
        {"operation": "enqueue", "timestamp": "2026-10-01T22:09:20.734Z",
         "content": _ENQUEUED_WITH_HOP_CHAIN},
        {"operation": "remove", "timestamp": "2026-10-01T22:11:10.376Z",
         "content": _RELEASED_WITHOUT_HOP_CHAIN},
        {"operation": "enqueue", "timestamp": "2026-10-01T23:00:00.000Z",
         "content": _LATER_ENQUEUED_WITH_HOP_CHAIN},
    ]
    assert SS._fold_queue_with_timestamps(records) == [
        ("2026-10-01T23:00:00.000Z", _LATER_ENQUEUED_WITH_HOP_CHAIN),
    ]


# MARK: - _read_held_queue, the bounded tail reader


def _transcript(tmp_path, *records):
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + ("\n" if records else ""))
    return str(p)


def test_read_held_queue_wraps_the_fold_in_the_field_shape(SS, tmp_path):
    path = _transcript(
        tmp_path,
        {"type": "assistant", "version": "2.1.226", "timestamp": "2026-08-09T00:00:00Z",
         "message": {"content": [{"type": "text", "text": "hi"}]}},
        {"type": "queue-operation", "operation": "enqueue",
         "timestamp": "2026-08-09T00:01:00Z", "content": "also check the timeout branch"},
    )
    got = SS._read_held_queue(path)
    assert got == {
        "messages": ["also check the timeout branch"],
        "source": "queue",
        "claudeVersion": "2.1.226",
    }


def test_read_held_queue_takes_the_newest_version_in_the_tail(SS, tmp_path):
    """`queue-operation` records carry no `version` of their own — only the
    conversational envelope does — so this reads it off whatever else is in the
    same bounded tail. Two versions in the window means the session was upgraded
    mid-transcript; the newest one describes the session as it is now."""
    path = _transcript(
        tmp_path,
        {"type": "assistant", "version": "2.1.220", "timestamp": "2026-08-01T00:00:00Z",
         "message": {"content": []}},
        {"type": "assistant", "version": "2.1.226", "timestamp": "2026-08-09T00:00:00Z",
         "message": {"content": []}},
        {"type": "queue-operation", "operation": "enqueue",
         "timestamp": "2026-08-09T00:01:00Z", "content": "x"},
    )
    got = SS._read_held_queue(path)
    assert got["claudeVersion"] == "2.1.226"


def test_read_held_queue_propagates_a_fold_failure_as_none(SS, tmp_path):
    path = _transcript(
        tmp_path,
        {"type": "queue-operation", "operation": "remove",
         "timestamp": "2026-08-09T00:00:00Z"},
    )
    assert SS._read_held_queue(path) is None


def test_read_held_queue_on_a_missing_file_is_none_not_a_crash(SS, tmp_path):
    assert SS._read_held_queue(str(tmp_path / "does-not-exist.jsonl")) is None


def test_read_held_queue_skips_a_malformed_line(SS, tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(
        "not json at all\n"
        + json.dumps({"type": "queue-operation", "operation": "enqueue", "content": "kept"})
        + "\n"
    )
    got = SS._read_held_queue(str(p))
    assert got["messages"] == ["kept"]


def test_read_held_queue_ignores_non_queue_records(SS, tmp_path):
    """`<task-notification>` blobs and ordinary turns share the file; only
    `type == "queue-operation"` feeds the fold."""
    path = _transcript(
        tmp_path,
        {"type": "user", "timestamp": "2026-08-09T00:00:00Z",
         "message": {"content": "not a queue record"}},
        {"type": "queue-operation", "operation": "enqueue",
         "timestamp": "2026-08-09T00:01:00Z", "content": "kept"},
    )
    assert SS._read_held_queue(path)["messages"] == ["kept"]


# MARK: - session_state(): the field's wiring


def _contained_transcript(tmp_path, monkeypatch, *records):
    """A transcript the containment guard will accept, with real content.

    Mirrors `test_session_verbs._contained_transcript` + `_transcript` combined:
    real transcripts live under ~/.claude/projects and the guard requires it, so
    Path.home is patched to a fake home rather than testing a path production
    never takes.
    """
    home = tmp_path / "home"
    d = home / ".claude" / "projects" / "-proj"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    t = d / "t.jsonl"
    t.write_text("\n".join(json.dumps(r) for r in records) + ("\n" if records else ""))
    return t


def _working_session(SS, monkeypatch, transcript_path, gate=None):
    """The world stubbed down to one working session with a real transcript.

    `state_from_events` is stubbed rather than driven off a real hook log, the
    same seam `test_optional_picker_reader._blocked_on_a_question` uses — the
    hook log's own parsing has its own tests, and this field does not touch it.
    """
    monkeypatch.setattr(SS.TT, "claude_pane", lambda n: ("%1", 4242))
    monkeypatch.setattr(SS.TT, "meta_for_pid", lambda pid, days=3: {
        "session_id": "aaaa", "cwd": "/x", "transcript_path": str(transcript_path)})
    monkeypatch.setattr(SS, "_pid_alive", lambda pid: True)
    log = transcript_path.parent / "hooks.jsonl"
    log.write_text('{"event": "UserPromptSubmit"}\n', encoding="utf-8")
    monkeypatch.setattr(SS, "_hook_logs", lambda sid, cwd=None, days=3: [log])
    monkeypatch.setattr(SS, "state_from_events", lambda events: {
        "state": "working", "gate": gate, "gate_ts": None,
        "last_event": "PreToolUse", "last_event_age": 0.1, "unparsed_records": 0,
    })


def test_held_present_and_populated_on_a_full_read_while_working(SS, monkeypatch, tmp_path):
    t = _contained_transcript(
        tmp_path, monkeypatch,
        {"type": "assistant", "version": "2.1.226", "timestamp": "2026-08-09T00:00:00Z",
         "message": {"content": []}},
        {"type": "queue-operation", "operation": "enqueue",
         "timestamp": "2026-08-09T00:01:00Z", "content": "also check the timeout branch"},
    )
    _working_session(SS, monkeypatch, t)

    got = SS.session_state("deck")
    assert got["state"] == "working"
    assert got["held"] == {
        "messages": ["also check the timeout branch"],
        "source": "queue",
        "claudeVersion": "2.1.226",
    }


def test_held_is_positively_empty_when_nothing_is_queued(SS, monkeypatch, tmp_path):
    t = _contained_transcript(
        tmp_path, monkeypatch,
        {"type": "assistant", "version": "2.1.226", "timestamp": "2026-08-09T00:00:00Z",
         "message": {"content": []}},
    )
    _working_session(SS, monkeypatch, t)

    got = SS.session_state("deck")
    assert got["held"]["messages"] == []


def test_held_is_null_when_the_fold_could_not_tell(SS, monkeypatch, tmp_path):
    t = _contained_transcript(
        tmp_path, monkeypatch,
        {"type": "queue-operation", "operation": "remove",
         "timestamp": "2026-08-09T00:00:00Z"},
    )
    _working_session(SS, monkeypatch, t)

    got = SS.session_state("deck")
    assert "held" in got
    assert got["held"] is None


def test_held_is_absent_on_a_brief_read(SS, monkeypatch, tmp_path):
    """The rail's mode. It draws no held row for any session, so it must not pay
    for the bounded read on every one of its rows every 0.75s."""
    t = _contained_transcript(
        tmp_path, monkeypatch,
        {"type": "queue-operation", "operation": "enqueue",
         "timestamp": "2026-08-09T00:01:00Z", "content": "x"},
    )
    _working_session(SS, monkeypatch, t)

    got = SS.session_state("deck", brief=True)
    assert "held" not in got


@pytest.mark.parametrize("state", ["idle", "waiting", "blocked", "unknown"])
def test_held_is_absent_off_the_working_state(SS, monkeypatch, tmp_path, state):
    """Slice A: the input box empties on Enter and the message goes straight to
    the queue, so a message can only be held while the session is `working` —
    every other state has nothing to read."""
    t = _contained_transcript(
        tmp_path, monkeypatch,
        {"type": "queue-operation", "operation": "enqueue",
         "timestamp": "2026-08-09T00:01:00Z", "content": "x"},
    )
    monkeypatch.setattr(SS.TT, "claude_pane", lambda n: ("%1", 4242))
    monkeypatch.setattr(SS.TT, "meta_for_pid", lambda pid, days=3: {
        "session_id": "aaaa", "cwd": "/x", "transcript_path": str(t)})
    monkeypatch.setattr(SS, "_pid_alive", lambda pid: True)
    log = t.parent / "hooks.jsonl"
    log.write_text('{"event": "UserPromptSubmit"}\n', encoding="utf-8")
    monkeypatch.setattr(SS, "_hook_logs", lambda sid, cwd=None, days=3: [log])
    gate = {"kind": "permission", "tool": "Bash", "detail": {}} if state == "blocked" else None
    monkeypatch.setattr(SS, "state_from_events", lambda events: {
        "state": state, "gate": gate, "gate_ts": None,
        "last_event": "PreToolUse", "last_event_age": 0.1, "unparsed_records": 0,
    })

    got = SS.session_state("deck")
    assert got["state"] == state
    assert "held" not in got


def test_held_is_null_when_the_transcript_cannot_be_resolved(SS, monkeypatch, tmp_path):
    """`working`, but no readable transcript — the field is `null`, NOT absent,
    and the distinction is the contract's: absent means nobody looked, null
    means we looked and could not establish it. A consumer draws different
    things for each, so a test conflating them in its name is worse than a
    naming nit."""
    monkeypatch.setattr(SS.TT, "claude_pane", lambda n: ("%1", 4242))
    monkeypatch.setattr(SS.TT, "meta_for_pid", lambda pid, days=3: {
        "session_id": "aaaa", "cwd": "/x", "transcript_path": "/nonexistent/x.jsonl"})
    monkeypatch.setattr(SS, "_pid_alive", lambda pid: True)
    log = tmp_path / "hooks.jsonl"
    log.write_text('{"event": "UserPromptSubmit"}\n', encoding="utf-8")
    monkeypatch.setattr(SS, "_hook_logs", lambda sid, cwd=None, days=3: [log])
    monkeypatch.setattr(SS, "state_from_events", lambda events: {
        "state": "working", "gate": None, "gate_ts": None,
        "last_event": "PreToolUse", "last_event_age": 0.1, "unparsed_records": 0,
    })

    got = SS.session_state("deck")
    assert got["state"] == "working"
    assert got["held"] is None


# MARK: - significance: the field the release must not go dark on


def test_signature_changes_when_only_held_changes(SS):
    """THE SINGLE MOST LIKELY WAY THIS FEATURE SHIPS DEAD. A release clears
    `held` and touches nothing else in the state object, so a follower that does
    not treat this field as significant would emit no line at all."""
    base = {"state": "working", "gate": None, "last_event": "PreToolUse", "alive": True}
    held = {**base, "held": {"messages": ["x"], "source": "queue", "claudeVersion": "2.1.226"}}
    released = {**base, "held": {"messages": [], "source": "queue", "claudeVersion": "2.1.226"}}
    assert SS.signature(held) != SS.signature(released)


def test_follow_states_emits_a_line_on_a_held_only_change(SS):
    """Driven through the `reader` and `ticks` seams, per the plan's test list —
    everything but `held` stays fixed across three ticks; the unchanged middle
    tick must not re-emit, and the held-only change on the third must."""
    readings = iter([
        {"session": "deck", "state": "working", "gate": None, "last_event": "PreToolUse",
         "alive": True, "held": {"messages": ["x"], "source": "queue",
                                  "claudeVersion": "2.1.226"}},
        {"session": "deck", "state": "working", "gate": None, "last_event": "PreToolUse",
         "alive": True, "held": {"messages": ["x"], "source": "queue",
                                  "claudeVersion": "2.1.226"}},
        {"session": "deck", "state": "working", "gate": None, "last_event": "PreToolUse",
         "alive": True, "held": {"messages": [], "source": "queue",
                                  "claudeVersion": "2.1.226"}},
    ])
    seen = list(SS.follow_states(
        "deck", 0, sleep=lambda s: None, reader=lambda name: next(readings), ticks=3))
    assert len(seen) == 2, "the unchanged middle tick must not re-emit"
    assert seen[0]["held"]["messages"] == ["x"]
    assert seen[1]["held"]["messages"] == [], "the held-only change must still emit"


# MARK: - the rail's batch, and its hand-built failure entry


def test_session_states_batch_never_carries_held(SS, monkeypatch):
    """`session_states` always asks for `brief`, so `held` is never read on that
    path — and the hand-built failure entry has to agree, the same trap that once
    left `agents` absent from exactly this path (see its own docstring)."""
    def fake(name, days=3, brief=False, table=None):
        assert brief is True
        if name == "boom":
            raise RuntimeError("no such session")
        return {"session": name, "state": "working", "alive": True}
    monkeypatch.setattr(SS, "session_state", fake)
    monkeypatch.setattr(SS.TT, "session_table", lambda: {})

    got = SS.session_states(["ok", "boom"])
    assert "held" not in got["ok"], "brief already omits it upstream"
    assert "held" not in got["boom"], "the hand-built failure entry must match"


def test_session_states_batch_shares_one_list_sessions_call(SS, monkeypatch):
    """Non-blocking finding, PR #480 round 2. `TT.claude_pane`'s exact-match fix
    resolves a session name via `tmux list-sessions`, and `session_state` reaches
    it unconditionally — so before batching this, a rail refresh over N pinned
    sessions issued N identical `list-sessions` calls, one per row, all listing
    the same table. `session_states` must resolve that table once and hand it to
    every row instead."""
    calls = {"list-sessions": 0}

    def fake_run(argv, stdin=None):
        if argv[:2] == ["tmux", "list-sessions"]:
            calls["list-sessions"] += 1
            return "$1 alpha\n$2 beta"
        return None  # no panes for either: both entries settle on JoinError

    monkeypatch.setattr(SS.TT, "_run", fake_run)

    got = SS.session_states(["alpha", "beta"])

    assert set(got) == {"alpha", "beta"}
    assert calls["list-sessions"] == 1, (
        "one `list-sessions` for the whole batch, not one per session")
