"""Tests for `agentctl verify-init` (ARCH-254).

verify-init polls a just-spawned worker for its `init` system event and reports
a pre-init death (the silent pre-first-call death class behind the ARCH-253
overnight worker losses) so the orchestrator can re-dispatch instead of waiting
for the 05:00 healthcheck. These pin the one-shot classifier + its helpers; the
poll loop is a thin wrapper over them.
"""
from __future__ import annotations

import json
import os


def _write_transcript(run_dir, *events):
    with (run_dir / "transcript.jsonl").open("w") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def test_pid_alive(agentctl):
    assert agentctl._pid_alive(os.getpid()) is True
    assert agentctl._pid_alive(None) is False
    assert agentctl._pid_alive("not-a-pid") is False
    # PID 0 is special (process-group signal) — must read as not-a-worker.
    assert agentctl._pid_alive(0) is False
    # A very high pid is almost certainly free.
    assert agentctl._pid_alive(4_000_000_000) is False


def test_transcript_has_init_true(agentctl, tmp_path):
    _write_transcript(
        tmp_path,
        {"type": "system", "subtype": "hook_started"},
        {"type": "system", "subtype": "hook_response"},
        {"type": "system", "subtype": "init", "cwd": "/x"},
    )
    assert agentctl._transcript_has_init(tmp_path) is True


def test_transcript_has_init_false_when_only_hooks(agentctl, tmp_path):
    # The exact pre-init-death shape: hooks ran, then nothing.
    _write_transcript(
        tmp_path,
        {"type": "system", "subtype": "hook_started"},
        {"type": "system", "subtype": "hook_response"},
    )
    assert agentctl._transcript_has_init(tmp_path) is False


def test_transcript_has_init_missing_file(agentctl, tmp_path):
    assert agentctl._transcript_has_init(tmp_path) is False


def test_transcript_has_init_ignores_init_substring_in_other_fields(agentctl, tmp_path):
    # A non-system line that merely contains the word "init" must not count.
    _write_transcript(
        tmp_path,
        {"type": "assistant", "message": {"content": "let me init the repo"}},
    )
    assert agentctl._transcript_has_init(tmp_path) is False


def test_verify_init_state_init(agentctl, tmp_path):
    _write_transcript(tmp_path, {"type": "system", "subtype": "init"})
    assert agentctl._verify_init_state(tmp_path, os.getpid()) == "init"


def test_verify_init_state_finalized_counts_as_init(agentctl, tmp_path):
    # A run that finalized (outcome.json) before we caught init still lived.
    _write_transcript(tmp_path, {"type": "system", "subtype": "hook_started"})
    (tmp_path / "outcome.json").write_text("{}")
    assert agentctl._verify_init_state(tmp_path, 4_000_000_000) == "init"


def test_verify_init_state_predeath(agentctl, tmp_path):
    # Hooks ran, no init, no outcome, and the pid is dead → pre-init death.
    _write_transcript(
        tmp_path,
        {"type": "system", "subtype": "hook_started"},
        {"type": "system", "subtype": "hook_response"},
    )
    assert agentctl._verify_init_state(tmp_path, 4_000_000_000) == "predeath"


def test_verify_init_state_pending_when_alive_no_init(agentctl, tmp_path):
    _write_transcript(tmp_path, {"type": "system", "subtype": "hook_started"})
    assert agentctl._verify_init_state(tmp_path, os.getpid()) == "pending"


def test_verify_init_state_pending_when_pid_unknown(agentctl, tmp_path):
    # Before the detached grandchild writes its pid: no verdict, keep polling.
    _write_transcript(tmp_path, {"type": "system", "subtype": "hook_started"})
    assert agentctl._verify_init_state(tmp_path, None) == "pending"


def test_safe_meta_pid_missing_file(agentctl, tmp_path):
    assert agentctl._safe_meta_pid(tmp_path) is None


def test_safe_meta_pid_empty_or_partial_file(agentctl, tmp_path):
    # update_meta truncates-then-writes; a poll can catch meta.json mid-write.
    # An empty / partial file must read as None (pid unknown), never crash.
    (tmp_path / "meta.json").write_text("")
    assert agentctl._safe_meta_pid(tmp_path) is None
    (tmp_path / "meta.json").write_text('{"run_id": "run-x", "pi')  # truncated mid-write
    assert agentctl._safe_meta_pid(tmp_path) is None


def test_safe_meta_pid_reads_pid(agentctl, tmp_path):
    (tmp_path / "meta.json").write_text(json.dumps({"run_id": "run-x", "pid": 4242}))
    assert agentctl._safe_meta_pid(tmp_path) == 4242


def test_verify_init_exit_codes_distinct(agentctl):
    codes = {agentctl.VERIFY_INIT_OK,
             agentctl.VERIFY_INIT_TIMEOUT,
             agentctl.VERIFY_INIT_PREDEATH}
    assert codes == {0, 2, 3}
