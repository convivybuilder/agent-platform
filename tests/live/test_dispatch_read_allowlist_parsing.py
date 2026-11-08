"""Structural checks for `test_dispatch_read_allowlist_live.py`'s own parsing helpers,
against a stub `claude` this module runs as a real subprocess — never the network,
never a real credential, never a path under the real account home.

Four scenarios:

- an init event whose own `tools` list leaves a tool out, and a call to that tool
  answered the way a tool not in that list is answered — `_init_tools` must read the
  gap from the init event, and `_available` must drop that answer without reading it
  as a refusal or a pass;
- a Bash result holding a persisted-output note — `PERSISTED_PATH_RE` must find the
  path it carries;
- a Read call answered with Read's own byte-cap message, and a separate one answered
  with its own token-cap message — `_read_readback_failure` must report either as too
  large to read, never as a permission refusal and never as a pass.

Always runs (no `AGENTCTL_LIVE_CONFINEMENT` gate, no `claude` on PATH needed): the stub
is a shell script this module writes and invokes directly, never the real `claude`.
"""

import json
import shlex
import subprocess
from pathlib import Path

from test_dispatch_read_allowlist_live import (
    NOT_AVAILABLE_RE,
    PERSISTED_PATH_RE,
    _available,
    _calls_by_path,
    _init_tools,
    _read_readback_failure,
)
from test_dispatched_run_confinement import _transcript_calls


def _run_stub(tmp_path: Path, lines: list[str]) -> Path:
    """A `claude`-shaped stub that prints `lines` (each a JSON event) to stdout, run as
    a real subprocess, with its stdout captured into a `transcript.jsonl` exactly as
    `test_dispatch_read_allowlist_live.py`'s own live test does. Returns the run dir.

    The events go through no shell: they're written verbatim to a data file and the
    stub `exec cat`s that file, so a fixture's backticks, `$`, and `\\` reach stdout
    unchanged instead of being read as shell syntax."""
    data = tmp_path / "stub-events.jsonl"
    data.write_text("\n".join(lines) + "\n")
    stub = tmp_path / "claude-stub"
    stub.write_text(f"#!/bin/sh\nexec cat {shlex.quote(str(data))}\n")
    stub.chmod(0o755)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    proc = subprocess.run([str(stub)], capture_output=True, text=True, timeout=30)
    (run_dir / "transcript.jsonl").write_text(proc.stdout)
    return run_dir


def _tool_use(call_id: str, name: str, **input_) -> str:
    return json.dumps({"type": "assistant",
                        "message": {"content": [{"type": "tool_use", "id": call_id,
                                                   "name": name, "input": input_}]}})


def _tool_result(call_id: str, text: str, is_error: bool = False) -> str:
    return json.dumps({"type": "user",
                        "message": {"content": [{"type": "tool_result",
                                                   "tool_use_id": call_id,
                                                   "is_error": is_error,
                                                   "content": [{"type": "text", "text": text}]}]}})


def test_init_tools_without_grep_and_glob_are_not_run_and_not_a_refusal(tmp_path):
    canary = "/some/scratch/canary.txt"
    grep_error = ("<tool_use_error>Error: No such tool available: Grep. Grep is not "
                  "available in this session — search file contents with `grep` via "
                  "the Bash tool instead.</tool_use_error>")
    glob_error = ("<tool_use_error>Error: No such tool available: Glob. Glob is not "
                  "available in this session — find files with `find` via the Bash "
                  "tool instead.</tool_use_error>")
    run_dir = _run_stub(tmp_path, [
        json.dumps({"type": "system", "subtype": "init", "session_id": "s1",
                    "tools": ["Bash", "Read", "Edit", "Write", "ToolSearch"]}),
        _tool_use("t1", "Grep", pattern="x", path=canary),
        _tool_result("t1", grep_error, is_error=True),
        _tool_use("t2", "Glob", pattern="*", path=canary),
        _tool_result("t2", glob_error, is_error=True),
    ])

    offered = _init_tools(run_dir)
    assert offered is not None
    assert "Grep" not in offered and "Glob" not in offered
    assert "Read" in offered

    calls = _transcript_calls(run_dir)
    grep_calls = _calls_by_path(calls, "Grep", canary)
    glob_calls = _calls_by_path(calls, "Glob", canary)
    assert grep_calls and glob_calls, "the stub's own calls were not parsed back"
    # The harness must hand back exactly what the fixture wrote — backticks included —
    # never a shell's reading of it.
    assert grep_calls[0]["text"] == grep_error
    assert glob_calls[0]["text"] == glob_error
    # The error text matches NOT_AVAILABLE_RE, so `_available` drops it — the call
    # carries no evidence either way, never a refusal and never a pass.
    assert all(NOT_AVAILABLE_RE.search(c["text"]) for c in grep_calls)
    assert all(NOT_AVAILABLE_RE.search(c["text"]) for c in glob_calls)
    assert _available(grep_calls) == []
    assert _available(glob_calls) == []


def test_persisted_path_re_finds_the_current_wording(tmp_path):
    path = str(tmp_path / ".claude" / "projects" / "some-slug" / "tool-results" / "abc123.txt")
    note = (f"<persisted-output>\nOutput too large (1.9MB). Full output saved to: "
             f"{path}\n\nPreview (first 2KB):\nAAAA...\n</persisted-output>")
    run_dir = _run_stub(tmp_path, [
        _tool_use("b1", "Bash", command="python3 -c \"print('A' * 2000000)\""),
        _tool_result("b1", note),
    ])

    calls = _transcript_calls(run_dir)
    bash_calls = [c for c in calls if c["name"] == "Bash"]
    assert bash_calls, "the stub's own Bash call was not parsed back"
    m = PERSISTED_PATH_RE.search(bash_calls[0]["text"])
    assert m is not None, bash_calls[0]["text"]
    assert m.group(1) == path


def test_read_readback_failure_reports_the_size_limit_not_a_refusal(tmp_path):
    path = str(tmp_path / ".claude" / "projects" / "some-slug" / "tool-results" /
               "abc123.txt")
    too_large_text = ("File content (1.9MB) exceeds maximum allowed size (256KB). Use "
                       "offset and limit parameters to read specific portions of the "
                       "file, or search for specific content")
    run_dir = _run_stub(tmp_path, [
        _tool_use("r1", "Read", file_path=path, limit=5),
        _tool_result("r1", too_large_text, is_error=True),
    ])

    calls = _transcript_calls(run_dir)
    read_calls = _calls_by_path(calls, "Read", path)
    assert read_calls, "the stub's own Read call was not parsed back"

    failure = _read_readback_failure(read_calls, path)
    assert failure is not None
    assert "too large" in failure
    assert "refusal" not in failure


def test_read_readback_failure_reports_the_token_cap_not_a_refusal(tmp_path):
    path = str(tmp_path / ".claude" / "projects" / "some-slug" / "tool-results" /
               "abc123.txt")
    too_large_text = ("File content (2000008 tokens) exceeds maximum allowed tokens "
                       "(25000). Use offset and limit parameters to read specific "
                       "portions of the file, or search for specific content")
    run_dir = _run_stub(tmp_path, [
        _tool_use("r1", "Read", file_path=path, limit=5),
        _tool_result("r1", too_large_text, is_error=True),
    ])

    calls = _transcript_calls(run_dir)
    read_calls = _calls_by_path(calls, "Read", path)
    assert read_calls, "the stub's own Read call was not parsed back"

    failure = _read_readback_failure(read_calls, path)
    assert failure is not None
    assert "too large" in failure
    assert "refusal" not in failure
