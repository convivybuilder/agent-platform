"""tools/render-staleness-context.py — a LIVE co session is told when its own
instruction render has fallen behind the knowledge base (issue_
01M4GJRCDNKCV4QREH68KSRS3W slice 2).

Drives the hook exactly as Claude Code does, the same posture
`test_usage_window_context.py` uses for its sibling hook: a JSON payload on
stdin, a fresh process per firing, `HOME` redirected per test so its
baseline/fingerprint state never touches the real machine.

`knowledge-ctx` ITSELF IS A STUB SCRIPT ON `$PATH`, not the real CLI. The
hook's own `--record` option on `bin/knowledge-ctx check` is tested directly
against the real module in `test_knowledge_ctx.py`
(`test_check_record_*`) — exit codes, the comparison, the escaping. What
this file tests is everything the HOOK does around that one subprocess
call: which workspace it resolves, when it captures a baseline and how long
that baseline survives a later re-render, which lines it trusts enough to
inject, how often, and the three skips. The stub lets every one of those be
driven directly, including a stdout line `check` would never actually
print and a `check` that hangs past the hook's own timeout — neither of
which the real CLI can be made to do without a real stale Library and a
patched clock.

The stub, at `<stub_dir>/knowledge-ctx`:
  * every call is appended to `$RSC_STUB_LOG` (one JSON line: `argv`, `cwd`)
    when that env var is set, so a test can confirm what the hook actually
    invoked, and that the SAME `--record` path is used across firings;
  * called with no `--record` (never the hook's own call — see
    `test_a_rerender_during_the_session_is_still_compared_against_the_original_baseline`,
    the one test that calls the stub this way directly, to state the
    contrast outright): always "is current", exit 0 — standing in for the
    workspace's own default, on-disk `check`;
  * called WITH `--record` (the hook's own call, always): reads
    `$RSC_STUB_CONTROL`, a JSON file a test writes with `_control()` before
    each firing (`returncode`, `stdout`, `sleep`), and reproduces exactly
    that — standing in for whatever `check --record <baseline>` would
    report that firing.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "tools" / "render-staleness-context.py"


@pytest.fixture(scope="session")
def csh():
    """`bin/co-session-health`, for its `SUB_GC_AGE_S` cutoff and its own
    `_gc_stale_caches` sweep — the mtime-refresh tests below exercise the
    real sweep this hook's sidecars must survive, not a re-implementation of
    its cutoff logic."""
    from conftest import _load_cli
    return _load_cli("co-session-health")

_STUB_SOURCE = """#!/usr/bin/env python3
import json, os, sys, time

argv = sys.argv[1:]
log_path = os.environ.get("RSC_STUB_LOG")
if log_path:
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"argv": argv, "cwd": os.getcwd()}) + "\\n")

if "--record" not in argv:
    sys.stdout.write("knowledge-ctx: stub/profile is current.\\n")
    sys.exit(0)

control = {}
control_path = os.environ.get("RSC_STUB_CONTROL")
if control_path and os.path.exists(control_path):
    with open(control_path, encoding="utf-8") as fh:
        control = json.load(fh)
time.sleep(control.get("sleep", 0))
sys.stdout.write(control.get("stdout", ""))
sys.exit(control.get("returncode", 0))
"""


def _make_stub(tmp_path) -> Path:
    stub_dir = tmp_path / "stubbin"
    stub_dir.mkdir(exist_ok=True)
    stub = stub_dir / "knowledge-ctx"
    stub.write_text(_STUB_SOURCE)
    stub.chmod(0o755)
    return stub_dir


def _workspace(tmp_path, *, name="workspace", active="agent-platform/daytime",
              manifest=None) -> Path:
    ws = tmp_path / name
    claude_dir = ws / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / ".active-profile").write_text(active + "\n")
    if manifest is None:
        manifest = {
            "documents": [{"id": "agent-platform/agents/platform-coder"}],
            "instruction_blocks": [
                {"id": "platform/instruction-blocks/floor", "body_sha256": "abc123"}],
        }
    (claude_dir / ".profile-documents.json").write_text(json.dumps(manifest))
    return ws


def _control(tmp_path, *, returncode=0, stdout="", sleep=0) -> Path:
    path = tmp_path / "stub-control.json"
    path.write_text(json.dumps({"returncode": returncode, "stdout": stdout, "sleep": sleep}))
    return path


def _baseline_path(tmp_path, session_id: str) -> Path:
    key = "".join(re.findall(r"[A-Za-z0-9_-]+", session_id))[:64]
    return tmp_path / ".cache" / "co-session-health" / f"render-staleness-{key}-baseline.json"


def _state_path(tmp_path, session_id: str) -> Path:
    key = "".join(re.findall(r"[A-Za-z0-9_-]+", session_id))[:64]
    return tmp_path / ".cache" / "co-session-health" / f"render-staleness-{key}.json"


def _payload(session_id, cwd, event="UserPromptSubmit", **extra):
    out = {"session_id": session_id, "hook_event_name": event, "cwd": str(cwd)}
    out.update(extra)
    return out


def _run(tmp_path, payload, *, stub_dir, log_path=None, tmux="/tmp/tmux-test,1,0",
        agentctl_run_id=None):
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    env["PATH"] = f"{stub_dir}{os.pathsep}{env.get('PATH', '')}"
    env["RSC_STUB_CONTROL"] = str(tmp_path / "stub-control.json")
    if log_path is not None:
        env["RSC_STUB_LOG"] = str(log_path)
    else:
        env.pop("RSC_STUB_LOG", None)
    if tmux is None:
        env.pop("TMUX", None)
    else:
        env["TMUX"] = tmux
    if agentctl_run_id is None:
        env.pop("AGENTCTL_RUN_ID", None)
    else:
        env["AGENTCTL_RUN_ID"] = agentctl_run_id
    proc = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                          capture_output=True, text=True, env=env)
    return proc


def _context(proc) -> str:
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    return out["hookSpecificOutput"]["additionalContext"]


# --- the baseline: captured once, frozen for the session's life ---------------

def test_baseline_is_captured_on_the_first_firing(tmp_path):
    stub_dir = _make_stub(tmp_path)
    ws = _workspace(tmp_path)
    _control(tmp_path, returncode=0, stdout="knowledge-ctx: agent-platform/daytime is current.\n")
    proc = _run(tmp_path, _payload("s1", ws), stub_dir=stub_dir)
    assert proc.returncode == 0
    baseline = _baseline_path(tmp_path, "s1")
    assert baseline.exists()
    assert baseline.read_text() == (ws / ".claude" / ".profile-documents.json").read_text()


def test_a_rerender_during_the_session_is_still_compared_against_the_original_baseline(tmp_path):
    """The exact occurrence slice 2 exists for: the steward's hand re-render
    (`knowledge-ctx use <profile> --force`) while a session stays live
    changes the WORKSPACE's own on-disk manifest, so a plain `check` (no
    `--record`) reads current from that moment on — the stub's own
    no-`--record` branch models that directly, standing in for the
    workspace's own default check. The session must still be told: its own
    baseline, captured at its first firing, never moves, and the hook's own
    subprocess call is always built against that one path."""
    stub_dir = _make_stub(tmp_path)
    ws = _workspace(tmp_path)
    log_path = tmp_path / "stub-log.jsonl"

    _control(tmp_path, returncode=0, stdout="knowledge-ctx: agent-platform/daytime is current.\n")
    first = _run(tmp_path, _payload("s1", ws), stub_dir=stub_dir, log_path=log_path)
    assert first.returncode == 0
    baseline = _baseline_path(tmp_path, "s1")
    original_baseline = baseline.read_text()

    # The hand re-render: the workspace's own manifest changes under the
    # live session.
    (ws / ".claude" / ".profile-documents.json").write_text(json.dumps({
        "documents": [{"id": "agent-platform/agents/reviewer"}],
        "instruction_blocks": [],
    }))
    # The workspace's own default check (no --record) now reads current —
    # the stub's no-`--record` branch always does, standing in for that.
    confirm = subprocess.run(
        [str(stub_dir / "knowledge-ctx"), "check"], capture_output=True, text=True)
    assert confirm.returncode == 0
    assert "current" in confirm.stdout

    _control(tmp_path, returncode=1,
             stdout="~ instruction-block platform/instruction-blocks/floor\n")
    second = _run(tmp_path, _payload("s1", ws), stub_dir=stub_dir, log_path=log_path)
    assert second.returncode == 0
    assert baseline.read_text() == original_baseline, (
        "the baseline must stay frozen at what this session loaded, not "
        "follow a later re-render of the workspace")

    calls = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert len(calls) == 2
    for call in calls:
        assert call["argv"] == ["check", "--record", str(baseline)]

    ctx = _context(second)
    assert "behind the knowledge base" in ctx
    assert "platform/instruction-blocks/floor" in ctx


# --- the sweep: a firing keeps this session's own sidecars reading live -------

def test_a_firing_refreshes_the_baseline_mtime_so_it_survives_the_sweep(tmp_path, csh, monkeypatch):
    """Round 2 finding (PR #839 review): the baseline is written once, at a
    session's first firing, and never rewritten by design — so without this
    hook refreshing its mtime, a session that outlives the sweep's 3-day
    cutoff (`co-session-health`'s `SUB_GC_AGE_S`), which this module's own
    docstring already calls the normal case, would lose its own baseline
    while still alive."""
    stub_dir = _make_stub(tmp_path)
    ws = _workspace(tmp_path)
    _control(tmp_path, returncode=0, stdout="knowledge-ctx: agent-platform/daytime is current.\n")
    first = _run(tmp_path, _payload("s1", ws), stub_dir=stub_dir)
    assert first.returncode == 0
    baseline = _baseline_path(tmp_path, "s1")
    original_content = baseline.read_bytes()

    old = time.time() - csh.SUB_GC_AGE_S - 1
    os.utime(baseline, (old, old))

    second = _run(tmp_path, _payload("s1", ws), stub_dir=stub_dir)
    assert second.returncode == 0

    monkeypatch.setattr(csh, "SUB_CACHE_DIR", str(tmp_path / ".cache" / "co-session-health"))
    csh._gc_stale_caches()

    assert baseline.exists(), "a session still firing must not lose its baseline to the sweep"
    assert baseline.read_bytes() == original_content, (
        "the refresh must touch mtime only — the baseline's content is what "
        "the first firing captured and must never change after that")


def test_the_fingerprint_file_follows_the_same_rule(tmp_path, csh, monkeypatch):
    """Same exposure, same fix, for `render-staleness-<key>.json` — losing it
    to the sweep while the session is still live would give a DUPLICATE
    injection (the dropped fingerprint reads as "nothing injected yet") where
    losing the baseline gives a MISS, per the finding."""
    stub_dir = _make_stub(tmp_path)
    ws = _workspace(tmp_path)
    _control(tmp_path, returncode=1,
             stdout="~ instruction-block platform/instruction-blocks/floor\n")
    first = _run(tmp_path, _payload("s1", ws), stub_dir=stub_dir)
    assert _context(first) != ""
    state = _state_path(tmp_path, "s1")
    assert state.exists()
    original_content = state.read_bytes()

    old = time.time() - csh.SUB_GC_AGE_S - 1
    os.utime(state, (old, old))

    # Same diff, so the hook itself writes no new fingerprint this firing —
    # the refresh happens regardless of what the rest of the firing decides.
    second = _run(tmp_path, _payload("s1", ws), stub_dir=stub_dir)
    assert second.returncode == 0
    assert second.stdout.strip() == "", "the same diff must still not be re-injected"

    monkeypatch.setattr(csh, "SUB_CACHE_DIR", str(tmp_path / ".cache" / "co-session-health"))
    csh._gc_stale_caches()

    assert state.exists(), "a session still firing must not lose its fingerprint to the sweep"
    assert state.read_bytes() == original_content


def test_a_session_that_stops_firing_is_still_swept(tmp_path, csh, monkeypatch):
    """The other half of the same contract: a session with nothing left to
    refresh its sidecars (it stopped firing, i.e. it ended) must still age
    out — the refresh must not turn into a sweep that never removes anything."""
    stub_dir = _make_stub(tmp_path)
    ws = _workspace(tmp_path)
    _control(tmp_path, returncode=0, stdout="knowledge-ctx: agent-platform/daytime is current.\n")
    proc = _run(tmp_path, _payload("s1", ws), stub_dir=stub_dir)
    assert proc.returncode == 0
    baseline = _baseline_path(tmp_path, "s1")
    state = _state_path(tmp_path, "s1")
    assert baseline.exists() and state.exists()

    old = time.time() - csh.SUB_GC_AGE_S - 1
    os.utime(baseline, (old, old))
    os.utime(state, (old, old))

    monkeypatch.setattr(csh, "SUB_CACHE_DIR", str(tmp_path / ".cache" / "co-session-health"))
    csh._gc_stale_caches()

    assert not baseline.exists(), "a dead session's baseline must still be swept"
    assert not state.exists(), "a dead session's fingerprint file must still be swept"


# --- once per distinct fingerprint ---------------------------------------------

def test_injects_once_per_fingerprint_then_again_on_a_new_diff(tmp_path):
    stub_dir = _make_stub(tmp_path)
    ws = _workspace(tmp_path)
    _control(tmp_path, returncode=1,
             stdout="~ instruction-block platform/instruction-blocks/floor\n")
    first = _run(tmp_path, _payload("s1", ws), stub_dir=stub_dir)
    assert _context(first) != ""

    second = _run(tmp_path, _payload("s1", ws), stub_dir=stub_dir)
    assert second.stdout.strip() == "", "the same diff must not be re-injected"

    _control(tmp_path, returncode=1, stdout=(
        "~ instruction-block platform/instruction-blocks/floor\n"
        "+ instruction-block platform/instruction-blocks/second-floor\n"))
    third = _run(tmp_path, _payload("s1", ws), stub_dir=stub_dir)
    assert third.stdout.strip() != "", "a new, distinct diff must be injected"


def test_going_back_to_current_clears_the_fingerprint_so_the_same_diff_reinjects_later(tmp_path):
    stub_dir = _make_stub(tmp_path)
    ws = _workspace(tmp_path)
    _control(tmp_path, returncode=1,
             stdout="~ instruction-block platform/instruction-blocks/floor\n")
    first = _run(tmp_path, _payload("s1", ws), stub_dir=stub_dir)
    assert _context(first) != ""

    _control(tmp_path, returncode=0, stdout="knowledge-ctx: agent-platform/daytime is current.\n")
    cleared = _run(tmp_path, _payload("s1", ws), stub_dir=stub_dir)
    assert cleared.stdout.strip() == ""

    _control(tmp_path, returncode=1,
             stdout="~ instruction-block platform/instruction-blocks/floor\n")
    again = _run(tmp_path, _payload("s1", ws), stub_dir=stub_dir)
    assert _context(again) != "", (
        "a session that went back to current must be told again if the "
        "same diff reappears")


# --- treat check's output as data ----------------------------------------------

def test_a_garbage_line_is_dropped_but_the_valid_lines_still_reach_the_message(tmp_path):
    stub_dir = _make_stub(tmp_path)
    ws = _workspace(tmp_path)
    _control(tmp_path, returncode=1, stdout=(
        "~ instruction-block platform/instruction-blocks/floor\n"
        "rm -rf / # not a shape check ever prints\n"
        "+ document agent-platform/agents/coder\n"))
    proc = _run(tmp_path, _payload("s1", ws), stub_dir=stub_dir)
    ctx = _context(proc)
    assert "platform/instruction-blocks/floor" in ctx
    assert "rm -rf" not in ctx
    assert "1 other document" in ctx


def test_a_document_id_containing_the_word_instruction_block_is_still_counted_as_a_document(tmp_path):
    """Classification is by the line's own type token (`_LINE_RE`'s `block`
    group via `_is_block_line`), never a substring test on the whole line —
    round 1's finding (PR #839 review): a document id that happens to
    contain the text "instruction-block" (a realistic id, since
    `platform/instruction-blocks/*` is an existing path family) must not be
    bucketed into the block list and printed verbatim, which is exactly what
    this module's own docstring says document ids must never do."""
    stub_dir = _make_stub(tmp_path)
    ws = _workspace(tmp_path)
    _control(tmp_path, returncode=1, stdout=(
        "+ document platform/skills/instruction-block-editor\n"
        "~ instruction-block platform/instruction-blocks/floor\n"))
    proc = _run(tmp_path, _payload("s1", ws), stub_dir=stub_dir)
    ctx = _context(proc)
    assert "platform/instruction-blocks/floor" in ctx
    assert "platform/skills/instruction-block-editor" not in ctx
    assert "1 other document" in ctx


def test_only_garbage_lines_inject_nothing(tmp_path):
    stub_dir = _make_stub(tmp_path)
    ws = _workspace(tmp_path)
    _control(tmp_path, returncode=1, stdout="not a line check ever prints\n")
    proc = _run(tmp_path, _payload("s1", ws), stub_dir=stub_dir)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_the_block_list_is_capped_with_a_remainder_count(tmp_path):
    stub_dir = _make_stub(tmp_path)
    ws = _workspace(tmp_path)
    lines = "\n".join(
        f"+ instruction-block platform/instruction-blocks/b{i}" for i in range(17)) + "\n"
    _control(tmp_path, returncode=1, stdout=lines)
    proc = _run(tmp_path, _payload("s1", ws), stub_dir=stub_dir)
    ctx = _context(proc)
    assert "platform/instruction-blocks/b0" in ctx
    assert "platform/instruction-blocks/b14" in ctx
    assert "platform/instruction-blocks/b15" not in ctx
    assert "and 2 more" in ctx


# --- a timed-out check says nothing ---------------------------------------------

def test_a_check_that_times_out_is_silent(tmp_path):
    stub_dir = _make_stub(tmp_path)
    ws = _workspace(tmp_path)
    _control(tmp_path, returncode=1, sleep=5,
             stdout="~ instruction-block platform/instruction-blocks/floor\n")
    proc = _run(tmp_path, _payload("s1", ws), stub_dir=stub_dir)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


# --- workspace resolution: a cwd with no knowledge-ctx profile -----------------

def test_a_cwd_with_no_active_profile_is_silent_and_writes_no_state(tmp_path):
    stub_dir = _make_stub(tmp_path)
    ws = tmp_path / "plain-dir"
    ws.mkdir()
    _control(tmp_path, returncode=1,
             stdout="~ instruction-block platform/instruction-blocks/floor\n")
    proc = _run(tmp_path, _payload("s1", ws), stub_dir=stub_dir)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""
    assert not (tmp_path / ".cache").exists()


def test_a_first_firing_with_no_manifest_yet_is_silent_and_writes_no_baseline(tmp_path):
    """Round 1's third ask (PR #839 review): a workspace that has reached
    `.active-profile` but not yet `.profile-documents.json` — a knowledge-ctx
    workspace whose manifest hasn't been written yet, or was removed. The
    docstring's own contract (`_ensure_baseline`) says this must be treated
    as "unreadable this firing": write nothing, and let the NEXT firing try
    again rather than locking in an empty baseline. No subprocess call
    happens either, since there is no baseline to compare against."""
    stub_dir = _make_stub(tmp_path)
    ws = tmp_path / "workspace"
    claude_dir = ws / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / ".active-profile").write_text("agent-platform/daytime\n")
    assert not (claude_dir / ".profile-documents.json").exists()
    log_path = tmp_path / "stub-log.jsonl"
    _control(tmp_path, returncode=1,
             stdout="~ instruction-block platform/instruction-blocks/floor\n")
    proc = _run(tmp_path, _payload("s1", ws), stub_dir=stub_dir, log_path=log_path)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""
    assert not _baseline_path(tmp_path, "s1").exists()
    assert not _state_path(tmp_path, "s1").exists()
    assert not log_path.exists(), "no manifest to record means no check call this firing"


# --- the three fast-exit skips (shared with the sibling hooks) -----------------

def test_a_dispatched_run_is_skipped(tmp_path):
    stub_dir = _make_stub(tmp_path)
    ws = _workspace(tmp_path)
    _control(tmp_path, returncode=1,
             stdout="~ instruction-block platform/instruction-blocks/floor\n")
    proc = _run(tmp_path, _payload("s1", ws), stub_dir=stub_dir, agentctl_run_id="run-x")
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""
    assert not _baseline_path(tmp_path, "s1").exists(), "a skipped firing must write no state"


def test_outside_tmux_is_skipped(tmp_path):
    stub_dir = _make_stub(tmp_path)
    ws = _workspace(tmp_path)
    _control(tmp_path, returncode=1,
             stdout="~ instruction-block platform/instruction-blocks/floor\n")
    proc = _run(tmp_path, _payload("s1", ws), stub_dir=stub_dir, tmux=None)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""
    assert not _baseline_path(tmp_path, "s1").exists()


def test_a_subagents_own_tool_call_is_skipped(tmp_path):
    stub_dir = _make_stub(tmp_path)
    ws = _workspace(tmp_path)
    _control(tmp_path, returncode=1,
             stdout="~ instruction-block platform/instruction-blocks/floor\n")
    proc = _run(tmp_path, _payload("s1", ws, agent_id="a7ffafc777248e22e"), stub_dir=stub_dir)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""
    assert not _baseline_path(tmp_path, "s1").exists()


def test_always_exits_zero_on_unreadable_stdin(tmp_path):
    stub_dir = _make_stub(tmp_path)
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    env["PATH"] = f"{stub_dir}{os.pathsep}{env.get('PATH', '')}"
    env["TMUX"] = "/tmp/tmux-test,1,0"
    proc = subprocess.run([sys.executable, str(HOOK)], input="not json at all",
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_a_non_userpromptsubmit_event_is_ignored(tmp_path):
    stub_dir = _make_stub(tmp_path)
    ws = _workspace(tmp_path)
    _control(tmp_path, returncode=1,
             stdout="~ instruction-block platform/instruction-blocks/floor\n")
    proc = _run(tmp_path, _payload("s1", ws, event="PostToolUse"), stub_dir=stub_dir)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


# --- runs under the system interpreter the hooks config actually invokes ------

def test_imports_under_the_system_python3(tmp_path):
    """Registered with no interpreter named, so Claude Code execs the file
    by its own shebang, `#!/usr/bin/python3` — see
    `test_rollover_trigger.py::test_imports_under_the_system_python3` for
    why this is checked directly rather than trusted from the venv run."""
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    env.pop("TMUX", None)
    env.pop("AGENTCTL_RUN_ID", None)
    proc = subprocess.run(["/usr/bin/python3", str(HOOK)], input="{}",
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr


def test_the_hook_parses_under_python_3_9_grammar():
    """Same guard as `test_rollover_trigger.py` and
    `test_usage_window_context.py`'s own: `ast.parse`'s `feature_version`
    gates the grammar accepted regardless of which interpreter runs this
    check, so it holds even where `/usr/bin/python3` above happens to be
    3.10+ (CI's Linux runner, not verified on ci1)."""
    import ast
    ast.parse(HOOK.read_text(), filename=str(HOOK), feature_version=(3, 9))
