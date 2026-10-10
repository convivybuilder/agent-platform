"""The Stop hook that refuses a turn ending on a promised next action.

WHAT THESE PIN, AND WHY THE SECOND HALF MATTERS MORE. Blocking a promise is the
easy half; a check that also blocks the legitimate reasons a turn ends is one
its reader learns to ignore, and then disables. So the allow cases here are not
padding — they are the cases that decide whether the hook survives contact with
real sessions: a turn waiting on something already started, a turn handing a
decision to the operator, and a turn that simply finished.

The fail-open and never-block-twice tests pin the two properties that keep a bad
match from wedging a session, which is the failure mode that would make this
hook worse than the habit it corrects.
"""
from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "tools" / "stop-unfulfilled-next-action.py"


def _run(tmp_path, text, session=None, state_dir=None):
    """Drive the hook exactly as the harness does: a transcript path on a JSON
    stdin payload. Returns (blocked, reason)."""
    transcript = tmp_path / f"{uuid.uuid4()}.jsonl"
    transcript.write_text(json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
    ) + "\n")
    env = {"HOME": str(state_dir)} if state_dir else None
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({
            "transcript_path": str(transcript),
            "session_id": session or str(uuid.uuid4()),
        }),
        capture_output=True, text=True,
        env={**__import__("os").environ, **(env or {})},
    )
    assert proc.returncode == 0, proc.stderr
    if not proc.stdout.strip():
        return False, ""
    out = json.loads(proc.stdout)
    return out.get("decision") == "block", out.get("reason", "")


@pytest.mark.parametrize("text", [
    "Next I'll dispatch the reviewer.",
    "I'm going to open the PR.",
    "Let me run the tests.",
    "Then I'll push the branch.",
    "I will now merge main into the branch.",
])
def test_an_unconditional_promise_blocks_the_stop(tmp_path, text, monkeypatch):
    """The whole point: the promised action was one tool call away."""
    monkeypatch.setenv("HOME", str(tmp_path))
    blocked, reason = _run(tmp_path, text)
    assert blocked
    assert text.rstrip(".") in reason or text in reason


@pytest.mark.parametrize("text", [
    "I'll publish once the coder lands.",          # blocked on something started
    "Next I'll do it unless you redirect.",        # decision handed to the operator
    "Waiting on the bash job; then I'll dispatch.",# waiting on something external
    "Tell me which one and I'll open it.",         # a question put to the human
    "The merge is done and CI is green.",          # no promise at all
])
def test_a_legitimate_turn_ending_is_allowed(tmp_path, text, monkeypatch):
    """A false positive here is what trains a reader to ignore the check, so
    these matter more than the block cases."""
    monkeypatch.setenv("HOME", str(tmp_path))
    blocked, _ = _run(tmp_path, text)
    assert not blocked, f"false positive on a legitimate ending: {text!r}"


def test_a_promise_is_judged_per_sentence_not_per_message(tmp_path, monkeypatch):
    """A real promise is not excused by an unrelated conditional elsewhere in
    the same message — the sentence is the unit."""
    monkeypatch.setenv("HOME", str(tmp_path))
    blocked, reason = _run(
        tmp_path,
        "I'll open the PR once CI is green.\nNext I'll delete the old worktree.",
    )
    assert blocked
    assert "delete the old worktree" in reason


def test_the_same_message_is_never_blocked_twice(tmp_path, monkeypatch):
    """An inescapable check is one someone disables rather than obeys. A turn
    that genuinely must end while stating a next step has to be able to."""
    monkeypatch.setenv("HOME", str(tmp_path))
    session = str(uuid.uuid4())
    text = "Next I'll dispatch the reviewer."
    first, _ = _run(tmp_path, text, session=session)
    second, _ = _run(tmp_path, text, session=session)
    assert first is True
    assert second is False


def test_the_same_text_in_a_DIFFERENT_session_still_blocks(tmp_path, monkeypatch):
    """The de-duplication is per session. Otherwise one session's escape hatch
    silently disarms the check for every later session that phrases it the
    same way, which is most of them."""
    monkeypatch.setenv("HOME", str(tmp_path))
    text = "Next I'll dispatch the reviewer."
    assert _run(tmp_path, text, session="session-a")[0] is True
    assert _run(tmp_path, text, session="session-b")[0] is True


@pytest.mark.parametrize("payload", ["not json at all", "", "{}", '{"transcript_path": "/nope"}'])
def test_it_fails_open_on_anything_it_cannot_read(payload):
    """A hook that wedges a session is worse than the habit it corrects, so
    every error path must allow the stop."""
    proc = subprocess.run([sys.executable, str(HOOK)], input=payload,
                          capture_output=True, text=True)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_a_transcript_with_no_assistant_message_allows(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"type": "user", "message": {"content": []}}) + "\n")
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"transcript_path": str(transcript), "session_id": "s"}),
        capture_output=True, text=True,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""
