"""tools/rollover-trigger.py — the hook half of ADR-0069's self-rollover.

Drives the hook exactly as Claude Code does: a JSON payload on stdin, a fresh
process per firing. `HOME` is redirected per test so the hook's policy, vitals
and state files never touch the real machine — `rollover_policy`'s paths are
all `Path.home()`-relative and resolved at each subprocess's own import time,
so overriding `HOME` in the subprocess env is sufficient; no monkeypatch can
reach across a `subprocess.run` boundary.

THE `roll_now` PARK-AND-LOG TESTS BELOW REACH `signal_client.post_alert`, A
REAL NETWORK CLIENT — unlike everything above it in this file. `_run` clears
`AGENT_SIGNAL_TOKEN` from every subprocess by default for exactly that reason:
without it, a real token sitting in the outer test-runner's environment would
ride straight through `env = dict(os.environ)` and let a test file into the
real signal store. A cleared token makes `signal_client.token()` return None, so
`post_alert` returns `"skip"` before it ever opens a socket — safe for every
test that doesn't care what got posted. The tests that DO care point
`ORIENT_BASE_URL` at a local stub server (`_signal_stub_server`) instead, with a
fake token that server accepts unconditionally.
"""
from __future__ import annotations

import ast
import contextlib
import http.server
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import rollover_policy as RP

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "tools" / "rollover-trigger.py"
# What test_imports_under_the_system_python3 exercises transitively by actually
# importing the hook; test_the_pinned_files_parse_under_python_3_9_grammar
# checks these statically too, so a 3.10+-only construct in either is caught
# the same way the hook file itself is.
_HOOK_LOCAL_IMPORTS = (
    REPO / "lib" / "agent-platform" / "signal_client.py",
    REPO / "lib" / "agent-platform" / "rollover_policy.py",
)

# A REAL RECORDED SHAPE, REDACTED, FOLLOWING THE DOCUMENTED CONTRACT.
# `agent_id` present with no `tool_input` content is what this machine's own
# hook log showed for a `PreToolUse` fired from INSIDE a dispatched Agent-tool
# subagent's own tool call, reaching the hook stream through the parent
# session — see the PR body for the file and the (redacted) original. Every
# ordinary top-level tool call recorded on the same machine, in the same
# window, carries no `agent_id` field at all. The Claude Code hooks reference
# (https://code.claude.com/docs/en/hooks.md) confirms the same shape for
# `PostToolUse` by name, so this payload — built for `PostToolUse` — carries
# the field on the same basis the hook itself relies on.
SUBAGENT_PAYLOAD_SHAPE = {
    "agent_id": "a7ffafc777248e22e",
    "permission_mode": "bypassPermissions",
    "hook_event_name": "PostToolUse",
}


def _seed(home, session_id, *, ctx_pct=None, policy=None, override=None, rolling=None,
         state=None):
    cache = home / ".cache" / "co-session-health"
    cache.mkdir(parents=True, exist_ok=True)
    if ctx_pct is not None:
        (cache / f"vitals-{session_id}.json").write_text(
            json.dumps({"written_at": time.time(), "ctx_pct": ctx_pct}))
    if override is not None:
        (cache / f"rollover-override-{session_id}.json").write_text(
            json.dumps({"kind": override, "written_at": time.time()}))
    # `rolling` is the older parameter name (Slice 5) — `state` is the
    # general one (Slice 6 onward), for seeding any rollover state file
    # (`parked` included) rather than only the `rolling` shape.
    written_state = state if state is not None else rolling
    if written_state is not None:
        (cache / f"rollover-{session_id}.json").write_text(json.dumps(written_state))
    cfg = home / ".config" / "convivy"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "rollover.json").write_text(json.dumps(policy or {"mode": "enforce", "seek_pct": 70}))


def _crews_root(home):
    """The machine root these tests hand the hook through `--root-config`: `home/Convivy`, with an
    empty registry unless the test wrote its own first. Without it the hook would read the real
    machine's registry, which a CI runner does not have."""
    crews = home / "Convivy"
    registry = crews / "agent-platform" / "config" / "projects.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    if not registry.exists():
        registry.write_text("{}")
    cfg = home / "session-agent.env"
    cfg.write_text(f"CREWS_ROOT={crews}\n")
    return cfg


def _run(home, payload, *, tmux="/tmp/tmux-test,1,0", agentctl_run_id=None, raw_input=None,
         extra_env=None):
    env = dict(os.environ)
    env["HOME"] = str(home)
    if tmux is None:
        env.pop("TMUX", None)
    else:
        env["TMUX"] = tmux
    if agentctl_run_id is None:
        env.pop("AGENTCTL_RUN_ID", None)
    else:
        env["AGENTCTL_RUN_ID"] = agentctl_run_id
    # SAFETY (see module docstring): never let a real signal-store token ride
    # through from the outer environment into a subprocess that can now reach
    # `signal_client.post_alert`. Cleared unless a test explicitly supplies one
    # via `extra_env`, pointed at its own local stub server.
    env.pop("AGENT_SIGNAL_TOKEN", None)
    if extra_env:
        env.update(extra_env)
    # Claude Code sends `cwd` on every hook firing. The black-box gate reads it, and a firing with no
    # cwd is UNKNOWN, which withholds the park's signal. So a test firing carries a cwd outside any
    # private crew unless the test names its own.
    if isinstance(payload, dict):
        payload = {"cwd": str(home), **payload}
    stdin = raw_input if raw_input is not None else json.dumps(payload)
    # The event name comes first, as Claude Code passes it, and the test-only flag after it.
    event = payload.get("hook_event_name", "") if isinstance(payload, dict) else ""
    proc = subprocess.run([sys.executable, str(HOOK), event, "--root-config",
                           str(_crews_root(home))],
                          input=stdin, capture_output=True, text=True, env=env)
    return proc


class _SignalStubHandler(http.server.BaseHTTPRequestHandler):
    """Accepts any POST, records the parsed JSON body, answers 201. Stands in for
    the real signal store (see module docstring) — nothing here is the real
    `/api/signals` route."""

    def do_POST(self):  # noqa: N802 — BaseHTTPRequestHandler's own naming
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.server.captured.append(json.loads(body))
        payload = json.dumps({"id": f"issue_STUB{len(self.server.captured)}"}).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # noqa: A003 — silence per-request logging
        pass


@contextlib.contextmanager
def _signal_stub_server():
    """A local HTTP server standing in for the signal store. `server.captured`
    accumulates every posted signal body, already JSON-decoded."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _SignalStubHandler)
    server.captured = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _stub_env(server):
    """Points `signal_client.post_alert` at *server* instead of the real signal
    store: Orient's path shape is untouched by `vault_plane.translate`
    (`VAULTS_ON_LIBRARY=0`), so `/api/signals` reaches the stub exactly as
    `signal_client` built it."""
    port = server.server_address[1]
    return {"AGENT_SIGNAL_TOKEN": "test-token", "VAULTS_ON_LIBRARY": "0",
            "ORIENT_BASE_URL": f"http://127.0.0.1:{port}"}


def _write_transcript(path, text):
    path.write_text(json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }) + "\n")


def _payload(session_id, event, **extra):
    out = {"session_id": session_id, "hook_event_name": event}
    out.update(extra)
    return out


# --- observe mode: records, emits nothing -----------------------------------

def test_observe_mode_emits_nothing_even_deep_in_roll_now(tmp_path):
    _seed(tmp_path, "s1", ctx_pct=99, policy={"mode": "observe", "seek_pct": 70, "floor_pct": 90})
    proc = _run(tmp_path, _payload("s1", "PostToolUse"))
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_observe_mode_still_writes_state(tmp_path):
    """Recording state is not "emitting" — Deck's rail and the status line both
    read the state file regardless of mode."""
    _seed(tmp_path, "s1", ctx_pct=80, policy={"mode": "observe", "seek_pct": 70})
    _run(tmp_path, _payload("s1", "PostToolUse"))
    state = json.loads((tmp_path / ".cache" / "co-session-health" / "rollover-s1.json").read_text())
    assert state["state"] == "seeking"


# --- enforce mode: the right JSON per zone and event ------------------------

def test_enforce_seeking_posttooluse_names_the_percentage(tmp_path):
    _seed(tmp_path, "s1", ctx_pct=72, policy={"mode": "enforce", "seek_pct": 70})
    proc = _run(tmp_path, _payload("s1", "PostToolUse"))
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "72%" in ctx
    assert "/rollover" in ctx
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"


def test_enforce_roll_now_says_run_rollover_now(tmp_path):
    _seed(tmp_path, "s1", ctx_pct=97, policy={"mode": "enforce", "seek_pct": 70, "floor_pct": 90})
    proc = _run(tmp_path, _payload("s1", "UserPromptSubmit"))
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["additionalContext"] == "run /rollover now."


def test_enforce_ok_emits_nothing(tmp_path):
    _seed(tmp_path, "s1", ctx_pct=40, policy={"mode": "enforce", "seek_pct": 70})
    proc = _run(tmp_path, _payload("s1", "PostToolUse"))
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


# --- a declined `roll_now` is bounded, parked and filed (issue_01M2HTBBHK2W4EH86RAFE0YBPY) --

_DECLINE_POLICY = {"mode": "enforce", "seek_pct": 70, "floor_pct": 90}


def _state_file(home, session_id="s1"):
    return home / ".cache" / "co-session-health" / f"rollover-{session_id}.json"


def test_a_declining_session_gets_the_instruction_at_most_the_bound_times(tmp_path):
    _seed(tmp_path, "s1", ctx_pct=97, policy=_DECLINE_POLICY)
    emissions = 0
    for _ in range(RP.ROLL_NOW_REISSUE_LIMIT + 3):
        proc = _run(tmp_path, _payload("s1", "PostToolUse"))
        assert proc.returncode == 0
        out = proc.stdout.strip()
        if out:
            parsed = json.loads(out)
            assert parsed["hookSpecificOutput"]["additionalContext"] == "run /rollover now."
            emissions += 1
    assert emissions == RP.ROLL_NOW_REISSUE_LIMIT, \
        "no more than the bound's worth of instructions, ever"
    state = json.loads(_state_file(tmp_path).read_text())
    assert state["state"] == "parked"
    assert state["reason"] == "declined"


def _park_in(home, cwd, *, registry_text):
    """Drive a declined park from *cwd*, with *registry_text* as the machine's registry."""
    registry = home / "Convivy" / "agent-platform" / "config" / "projects.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(registry_text)
    _seed(home, "s1", ctx_pct=97, policy=_DECLINE_POLICY)
    with _signal_stub_server() as server:
        env = _stub_env(server)
        for _ in range(RP.ROLL_NOW_REISSUE_LIMIT + 1):
            _run(home, _payload("s1", "PostToolUse", cwd=str(cwd)), extra_env=env)
    return server.captured, json.loads(_state_file(home).read_text())


def test_a_park_inside_a_black_box_crew_logs_no_signal_and_the_state_says_why(tmp_path):
    """ADR-0043: a black box crew logs no signals. This hook is registered globally, so a session in a
    private crew reaches it; its own words stay out of the shared store, and the park still happens."""
    crew = tmp_path / "Convivy" / "CFO"
    (crew / "sub").mkdir(parents=True)
    registry = json.dumps({"cfo": {"private": True, "home_dir": "<root>/CFO"}})
    captured, state = _park_in(tmp_path, crew / "sub", registry_text=registry)

    assert captured == [], "a black box session's text reaches no signal"
    assert state["state"] == "parked" and state["reason"] == "declined"
    assert state["issue_status"] == "black-box"


def test_a_park_when_the_registry_cannot_be_read_is_withheld_too(tmp_path):
    """UNKNOWN fails closed: a session that cannot be placed in a crew logs no signal either."""
    session = tmp_path / "Convivy" / "anywhere"
    session.mkdir(parents=True)
    captured, state = _park_in(tmp_path, session, registry_text="{not json")

    assert captured == []
    assert state["issue_status"] == "black-box"


def test_a_park_outside_every_private_crew_logs_its_signal(tmp_path):
    session = tmp_path / "Convivy" / "agent-platform"
    session.mkdir(parents=True)
    captured, state = _park_in(tmp_path, session, registry_text="{}")

    assert len(captured) == 1
    assert state["issue_status"] == "created"


def test_reaching_the_bound_logs_exactly_one_signal_naming_what_it_said(tmp_path):
    _seed(tmp_path, "s1", ctx_pct=97, policy=_DECLINE_POLICY)
    transcript = tmp_path / "transcript.jsonl"
    _write_transcript(transcript, "co -r refuses this directory, so a rollover would fail.")

    with _signal_stub_server() as server:
        env = _stub_env(server)
        for _ in range(RP.ROLL_NOW_REISSUE_LIMIT):
            proc = _run(tmp_path, _payload("s1", "Stop", transcript_path=str(transcript),
                                          background_tasks=[]), extra_env=env)
            out = json.loads(proc.stdout)
            assert out["hookSpecificOutput"]["additionalContext"] == "run /rollover now."
        assert server.captured == [], "must not log before the bound is reached"

        proc = _run(tmp_path, _payload("s1", "Stop", transcript_path=str(transcript),
                                       background_tasks=[]), extra_env=env)
        assert proc.returncode == 0
        assert proc.stdout.strip() == "", "the firing that parks emits nothing further"
        assert len(server.captured) == 1, "exactly one signal logged"
        posted = server.captured[0]
        assert posted["kind"] == "alert"
        assert posted["stream"] == "co-rollover-park-declined"
        assert posted["body"].splitlines()[0].startswith("Operator action: read what the session said"), \
            "the operator's next step is the first line of the signal"
        assert "co -r refuses this directory" in posted["body"]
        assert posted["meta"] == {"severity": "medium"}, "the item this replaced was filed medium"

    state = json.loads(_state_file(tmp_path).read_text())
    assert state["state"] == "parked"
    assert state["reason"] == "declined"
    assert state["issue_id"] is None, "a park logs a signal, which has no id"
    # The stub answers 201, so `post_alert` reports `created`. Recorded on this
    # path too: both of this hook's park paths discarded the status alongside
    # `bin/co-rollover`'s, and a fix to one of the two would have let the class
    # back in through the other.
    assert state["issue_status"] == "created"
    # And the product string, for the same reason the status is here: this
    # hook's product is `basename(cwd)` of whatever directory the session was
    # launched in, and what is recorded is that SUBMITTED string rather than
    # the bucket the item landed in — see
    # `rollover_policy.DEFAULT_STATE["issue_product"]`.
    assert state["issue_product"]


@pytest.mark.parametrize("transcript_kwargs", [{}, {"transcript_path": "/no/such/file.jsonl"}],
                         ids=["no-transcript-path", "nonexistent-transcript-file"])
def test_an_unreadable_or_missing_transcript_still_parks_and_logs(tmp_path, transcript_kwargs):
    _seed(tmp_path, "s1", ctx_pct=97, policy=_DECLINE_POLICY)

    with _signal_stub_server() as server:
        env = _stub_env(server)
        for _ in range(RP.ROLL_NOW_REISSUE_LIMIT):
            _run(tmp_path, _payload("s1", "PostToolUse", **transcript_kwargs), extra_env=env)
        proc = _run(tmp_path, _payload("s1", "PostToolUse", **transcript_kwargs), extra_env=env)
        assert proc.returncode == 0
        assert proc.stdout.strip() == ""
        assert len(server.captured) == 1
        assert "declined without a recorded reason" in server.captured[0]["body"]

    state = json.loads(_state_file(tmp_path).read_text())
    assert state["state"] == "parked"


def test_a_second_episode_after_clear_gets_a_fresh_count(tmp_path):
    """Mirrors `session-agent rollover <name> clear`'s effect on a `parked`
    session: `rollover_policy.clear_parked` just unlinks the state file."""
    _seed(tmp_path, "s1", ctx_pct=97, policy=_DECLINE_POLICY)
    for _ in range(RP.ROLL_NOW_REISSUE_LIMIT + 1):
        _run(tmp_path, _payload("s1", "PostToolUse"))
    state_path = _state_file(tmp_path)
    assert json.loads(state_path.read_text())["state"] == "parked", \
        "the first episode must have parked before this test continues"

    state_path.unlink()

    proc = _run(tmp_path, _payload("s1", "PostToolUse"))
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["additionalContext"] == "run /rollover now.", \
        "a fresh episode gets its first instruction again, not an immediate park"
    state = json.loads(state_path.read_text())
    assert state["state"] == "roll_now"
    assert state["roll_now_reissues"] == 1


def test_a_session_that_rolls_over_never_reaches_the_bound_or_files(tmp_path):
    _seed(tmp_path, "s1", ctx_pct=97, policy=_DECLINE_POLICY)
    for _ in range(RP.ROLL_NOW_REISSUE_LIMIT - 1):
        proc = _run(tmp_path, _payload("s1", "PostToolUse"))
        out = json.loads(proc.stdout)
        assert out["hookSpecificOutput"]["additionalContext"] == "run /rollover now."

    # The session actually rolls over: `bin/co-rollover` marks it `rolling`.
    _seed(tmp_path, "s1", rolling=_ROLLING_STATE, policy=_DECLINE_POLICY)

    # Its own hooks keep firing right up until the predecessor's pane is
    # killed (module docstring) — none of them may resume counting toward the
    # bound or file anything.
    for _ in range(5):
        proc = _run(tmp_path, _payload("s1", "PostToolUse"))
        assert proc.returncode == 0
        assert proc.stdout.strip() == ""

    state = json.loads(_state_file(tmp_path).read_text())
    assert state["state"] == "rolling", "must never have been overwritten to parked"


# --- a seeking session looping on the instruction is bounded too ------------
# (issue_01M2PFRWZT27XY6ZREVWWNQ9SZ). The shape these reproduce is the one
# measured on 2026-09-16: a session at 72% — above `seek_pct`, well below
# `floor_pct`, so `seeking` and never `roll_now` — ending its turn, being told
# to seek a rollover moment, and ending its turn again, 58 times in 9.5
# minutes. Every `_SEEKING_POLICY` firing below is a `Stop` with no tool call
# and no operator prompt between them, because that is the loop.

_SEEKING_POLICY = {"mode": "enforce", "seek_pct": 70, "floor_pct": 90}


def _seeking_stop(home, session_id="s1", **extra):
    """One turn ending in the loop: re-seed the vitals the way a fresh hook
    firing would read them, then fire `Stop` with nothing running."""
    _seed(home, session_id, ctx_pct=72, policy=_SEEKING_POLICY)
    return _run(home, _payload(session_id, "Stop", background_tasks=[], **extra))


def test_a_looping_seeking_session_is_told_at_most_the_bound_times(tmp_path):
    """THE REGRESSION TEST. Before the fix this emitted on every firing
    forever: the bound in the hook is checked inside the `roll_now` branch,
    and `roll_now_reissues` is reset to 0 in every other zone, so a `seeking`
    session's count was structurally zero no matter how long it looped."""
    told = 0
    for _ in range(RP.SEEKING_STOP_REISSUE_LIMIT + 5):
        proc = _seeking_stop(tmp_path)
        assert proc.returncode == 0
        out = proc.stdout.strip()
        if out and "additionalContext" in out:
            told += 1
    assert told <= RP.SEEKING_STOP_REISSUE_LIMIT, \
        "a seeking session must not be told forever"
    state = json.loads(_state_file(tmp_path).read_text())
    assert state["state"] == "parked"
    assert state["reason"] == "seeking_loop"


def test_reaching_the_seeking_bound_logs_one_signal_for_the_costly_loop(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    _write_transcript(transcript, "*(Idle.)*")

    with _signal_stub_server() as server:
        env = _stub_env(server)
        for _ in range(RP.SEEKING_STOP_REISSUE_LIMIT):
            _seed(tmp_path, "s1", ctx_pct=72, policy=_SEEKING_POLICY)
            _run(tmp_path, _payload("s1", "Stop", transcript_path=str(transcript),
                                    background_tasks=[]), extra_env=env)
        assert server.captured == [], "must not log before the bound is reached"

        _seed(tmp_path, "s1", ctx_pct=72, policy=_SEEKING_POLICY)
        proc = _run(tmp_path, _payload("s1", "Stop", transcript_path=str(transcript),
                                       background_tasks=[]), extra_env=env)
        assert proc.returncode == 0
        assert proc.stdout.strip() == "", "the firing that parks emits nothing further"
        assert len(server.captured) == 1, "exactly one signal logged"
        posted = server.captured[0]
        assert posted["kind"] == "alert"
        assert posted["stream"] == "co-rollover-park-seeking-loop", \
            "the costly loop has its own stream, apart from a decline"
        assert posted["body"].splitlines()[0].startswith("Operator action: look for a live successor"), \
            "the operator's next step is the first line of the signal"
        assert "*(Idle.)*" in posted["body"], "records what the loop was emitting"

    state = json.loads(_state_file(tmp_path).read_text())
    assert state["state"] == "parked"
    assert state["reason"] == "seeking_loop"
    assert state["issue_id"] is None, "a park logs a signal, which has no id"
    # The stub answers 201, so `post_alert` reports `created`. Recorded on this
    # path too: both of this hook's park paths discarded the status alongside
    # `bin/co-rollover`'s, and a fix to one of the two would have let the class
    # back in through the other.
    assert state["issue_status"] == "created"
    # And the product string, for the same reason the status is here: this
    # hook's product is `basename(cwd)` of whatever directory the session was
    # launched in, and what is recorded is that SUBMITTED string rather than
    # the bucket the item landed in — see
    # `rollover_policy.DEFAULT_STATE["issue_product"]`.
    assert state["issue_product"]


def test_a_tool_call_between_turns_means_the_session_is_not_looping(tmp_path):
    """A session working normally ends turns too. What separates it from the
    loop is that it ACTS in between, so a `PostToolUse` firing resets the
    count and the bound is never approached however long it stays seeking."""
    for _ in range(RP.SEEKING_STOP_REISSUE_LIMIT * 4):
        proc = _seeking_stop(tmp_path)
        assert proc.returncode == 0
        _seed(tmp_path, "s1", ctx_pct=72, policy=_SEEKING_POLICY)
        _run(tmp_path, _payload("s1", "PostToolUse"))

    state = json.loads(_state_file(tmp_path).read_text())
    assert state["state"] == "seeking", "never parked a session that was working"
    assert state["seeking_stop_reissues"] == 0


def test_an_operator_prompt_between_turns_means_the_session_is_not_looping(tmp_path):
    """The other reset: a human typed, so the turn ended for a reason that is
    not this hook re-invoking the session."""
    for _ in range(RP.SEEKING_STOP_REISSUE_LIMIT * 4):
        proc = _seeking_stop(tmp_path)
        assert proc.returncode == 0
        _seed(tmp_path, "s1", ctx_pct=72, policy=_SEEKING_POLICY)
        _run(tmp_path, _payload("s1", "UserPromptSubmit"))

    state = json.loads(_state_file(tmp_path).read_text())
    assert state["state"] == "seeking", "never parked an attended session"
    assert state["seeking_stop_reissues"] == 0


_AGENT_RUNNING = [{"id": "x", "type": "agent", "agent_type": "reviewer", "status": "running"}]


def test_a_session_waiting_on_a_dispatched_agent_is_never_parked(tmp_path):
    """The counter is gated on `not agent_running`, mirroring
    `should_block_stop`. A session orchestrating dispatched agents ends its
    turn between them, and parking it would be the worse failure — it would
    stop being told to roll over at all, mid-orchestration, with no bug
    present. A loop rather than a single firing, because the bound is about
    accumulation and a single firing cannot reach it."""
    for _ in range(RP.SEEKING_STOP_REISSUE_LIMIT * 4):
        _seed(tmp_path, "s1", ctx_pct=72, policy=_SEEKING_POLICY)
        proc = _run(tmp_path, _payload("s1", "Stop", background_tasks=_AGENT_RUNNING))
        assert proc.returncode == 0
        assert proc.stdout.strip() != "", \
            "went silent, which is what parking looks like from here"
        assert "additionalContext" in json.loads(proc.stdout)["hookSpecificOutput"], \
            "still told to seek — held, not parked, and not silenced"

    state = json.loads(_state_file(tmp_path).read_text())
    assert state["state"] == "seeking", "never parked a session waiting on an agent"
    assert state["seeking_stop_reissues"] == 0


def test_the_measured_loop_is_still_caught_with_the_agent_gate_in_place(tmp_path):
    """The gate must not cost the catch. Every `Stop` through the real
    9.5-minute spin carried `background_tasks: []` (hook log, 2026-09-16
    18:34:54-18:35:07 MST), so the loop this bounds reaches the bound with
    the gate applied — this fires the exact payload shape that log recorded."""
    for _ in range(RP.SEEKING_STOP_REISSUE_LIMIT + 2):
        _seed(tmp_path, "s1", ctx_pct=72, policy=_SEEKING_POLICY)
        _run(tmp_path, _payload("s1", "Stop", background_tasks=[]))

    state = json.loads(_state_file(tmp_path).read_text())
    assert state["state"] == "parked"
    assert state["reason"] == "seeking_loop"


def test_observe_mode_never_parks_a_seeking_session(tmp_path):
    """Nothing is said in `observe`, so nothing can be looped on — the count
    must not accrue against a session the hook is silent toward."""
    for _ in range(RP.SEEKING_STOP_REISSUE_LIMIT + 5):
        _seed(tmp_path, "s1", ctx_pct=72,
              policy={"mode": "observe", "seek_pct": 70, "floor_pct": 90})
        proc = _run(tmp_path, _payload("s1", "Stop", background_tasks=[]))
        assert proc.returncode == 0
        assert proc.stdout.strip() == ""

    state = json.loads(_state_file(tmp_path).read_text())
    assert state["state"] == "seeking"
    assert state["seeking_stop_reissues"] == 0


# --- Stop blocks once while seeking with no agent running -------------------

def test_stop_blocks_once_then_only_adds_context(tmp_path):
    _seed(tmp_path, "s1", ctx_pct=80, policy={"mode": "enforce", "seek_pct": 70})
    first = _run(tmp_path, _payload("s1", "Stop", background_tasks=[]))
    out1 = json.loads(first.stdout)
    assert out1["decision"] == "block"
    assert "good moment" in out1["reason"]

    # Re-seed the vitals for the second firing (a fresh subprocess reads fresh
    # inputs, same as the hook would on the session's next Stop).
    _seed(tmp_path, "s1", ctx_pct=80, policy={"mode": "enforce", "seek_pct": 70})
    second = _run(tmp_path, _payload("s1", "Stop", background_tasks=[]))
    out2 = json.loads(second.stdout)
    assert "decision" not in out2, "the same seeking episode must not block twice"
    assert "additionalContext" in out2["hookSpecificOutput"]


def test_stop_never_blocks_while_an_agent_is_running(tmp_path):
    _seed(tmp_path, "s1", ctx_pct=80, policy={"mode": "enforce", "seek_pct": 70})
    proc = _run(tmp_path, _payload(
        "s1", "Stop",
        background_tasks=[{"id": "x", "type": "agent", "agent_type": "reviewer", "status": "running"}]))
    out = json.loads(proc.stdout)
    assert "decision" not in out
    assert "additionalContext" in out["hookSpecificOutput"]


# --- skip conditions ---------------------------------------------------------

def test_a_subagents_own_tool_call_is_skipped(tmp_path):
    _seed(tmp_path, "s1", ctx_pct=95, policy={"mode": "enforce", "seek_pct": 70})
    payload = _payload("s1", "PostToolUse", **SUBAGENT_PAYLOAD_SHAPE)
    proc = _run(tmp_path, payload)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_a_dispatched_worker_is_skipped(tmp_path):
    _seed(tmp_path, "s1", ctx_pct=95, policy={"mode": "enforce", "seek_pct": 70})
    proc = _run(tmp_path, _payload("s1", "PostToolUse"), agentctl_run_id="run-abc123")
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_a_session_outside_tmux_is_skipped(tmp_path):
    _seed(tmp_path, "s1", ctx_pct=95, policy={"mode": "enforce", "seek_pct": 70})
    proc = _run(tmp_path, _payload("s1", "PostToolUse"), tmux=None)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


# --- fails open ---------------------------------------------------------------

@pytest.mark.parametrize("raw", ["not json at all", "", "[1, 2, 3]", "null", '{"session_id": 123}'])
def test_garbage_input_exits_zero_and_says_nothing(tmp_path, raw):
    proc = _run(tmp_path, None, raw_input=raw)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_a_missing_session_id_exits_zero(tmp_path):
    proc = _run(tmp_path, {"hook_event_name": "PostToolUse"})
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


# --- the operator override reaches the hook (Slice 3) -----------------------

def test_a_force_override_reaches_the_session_even_in_observe_mode(tmp_path):
    """Deck's write side, epic comment 2399: "an operator's force sends in
    either mode." Everything else stays silent in `observe` — this is the one
    exception, and it is asserted directly against the mode that would
    otherwise suppress it."""
    _seed(tmp_path, "s1", policy={"mode": "observe", "seek_pct": 70}, override="force")
    proc = _run(tmp_path, _payload("s1", "PostToolUse"))
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["additionalContext"] == "run /rollover now."


def test_a_seek_override_stays_silent_in_observe_mode(tmp_path):
    """Unlike `force`: "veto, seek and clear change only recorded state in
    observe mode" (Slice 3 build note 3)."""
    _seed(tmp_path, "s1", ctx_pct=10, policy={"mode": "observe", "seek_pct": 70},
         override="seek")
    proc = _run(tmp_path, _payload("s1", "PostToolUse"))
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""
    state = json.loads((tmp_path / ".cache" / "co-session-health" / "rollover-s1.json").read_text())
    assert state["state"] == "seeking", "still recorded, just not emitted"


def test_a_seek_override_in_enforce_mode_names_the_lowered_percentage(tmp_path):
    _seed(tmp_path, "s1", ctx_pct=10, policy={"mode": "enforce", "seek_pct": 70},
         override="seek")
    proc = _run(tmp_path, _payload("s1", "PostToolUse"))
    out = json.loads(proc.stdout)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "10%" in ctx
    assert "/rollover" in ctx


def test_a_vetoed_session_emits_nothing_in_enforce_mode(tmp_path):
    cache = tmp_path / ".cache" / "co-session-health"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "rollover-override-s1.json").write_text(
        json.dumps({"kind": "veto", "zone": "seeking", "written_at": time.time()}))
    _seed(tmp_path, "s1", ctx_pct=80, policy={"mode": "enforce", "seek_pct": 70})
    proc = _run(tmp_path, _payload("s1", "PostToolUse"))
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""
    state = json.loads((tmp_path / ".cache" / "co-session-health" / "rollover-s1.json").read_text())
    assert state["state"] == "vetoed"


# --- the PreToolUse fence on a `rolling` session (Slice 5) ------------------

_ROLLING_STATE = {
    "state": "rolling", "reason": "rollover", "since": 1_000_000_000,
    "predecessor": {"session_id": "s1", "pane_id": "%1"},
    "successor": {"session_id": None, "pane_id": "%7"},
    "successor_window": 2,
}


def test_pretooluse_denies_a_rolling_session(tmp_path):
    _seed(tmp_path, "s1", policy={"mode": "observe"}, rolling=_ROLLING_STATE)
    proc = _run(tmp_path, _payload("s1", "PreToolUse"))
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    hook_out = out["hookSpecificOutput"]
    assert hook_out["hookEventName"] == "PreToolUse"
    assert hook_out["permissionDecision"] == "deny"
    assert "%7" in hook_out["permissionDecisionReason"]
    assert "end your turn" in hook_out["permissionDecisionReason"]


def test_pretooluse_fences_unconditionally_even_in_observe_mode(tmp_path):
    """Unlike every ordinary zone, which `observe` mode suppresses: the
    predecessor's own turn is over regardless of the platform-wide switch."""
    _seed(tmp_path, "s1", policy={"mode": "observe"}, rolling=_ROLLING_STATE)
    proc = _run(tmp_path, _payload("s1", "PreToolUse"))
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_pretooluse_allows_a_non_rolling_session(tmp_path):
    _seed(tmp_path, "s1", ctx_pct=95, policy={"mode": "enforce", "seek_pct": 70})
    proc = _run(tmp_path, _payload("s1", "PreToolUse"))
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_pretooluse_allows_a_session_with_no_rollover_state_at_all(tmp_path):
    proc = _run(tmp_path, _payload("s1", "PreToolUse"))
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_pretooluse_never_calls_evaluate_so_rolling_state_is_untouched(tmp_path):
    """The fence must read state, never recompute it — recomputing would hit
    the same overwrite `evaluate()`'s own terminal-state guard exists to
    prevent, from the one caller that guard cannot see (PreToolUse isn't one
    of the three events `evaluate()` is ever invoked for elsewhere)."""
    _seed(tmp_path, "s1", ctx_pct=10, policy={"mode": "enforce", "seek_pct": 70},
         rolling=_ROLLING_STATE)
    _run(tmp_path, _payload("s1", "PreToolUse"))
    state = json.loads((tmp_path / ".cache" / "co-session-health" / "rollover-s1.json").read_text())
    assert state == _ROLLING_STATE


def test_pretooluse_a_subagents_own_tool_call_is_skipped_before_the_fence(tmp_path):
    _seed(tmp_path, "s1", policy={"mode": "observe"}, rolling=_ROLLING_STATE)
    # Same `agent_id`-carrying shape as SUBAGENT_PAYLOAD_SHAPE, but for a real
    # PreToolUse firing — that constant's own `hook_event_name` is fixed to
    # PostToolUse (see its docstring), so it is not reused verbatim here.
    payload = _payload("s1", "PreToolUse", agent_id="a7ffafc777248e22e",
                       permission_mode="bypassPermissions")
    proc = _run(tmp_path, payload)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_pretooluse_a_dispatched_worker_is_skipped_before_the_fence(tmp_path):
    _seed(tmp_path, "s1", policy={"mode": "observe"}, rolling=_ROLLING_STATE)
    proc = _run(tmp_path, _payload("s1", "PreToolUse"), agentctl_run_id="run-abc123")
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


# --- PreToolUse records the current permission_mode (the freshness fix) ----
#
# `Stop`/`UserPromptSubmit` (what `RP.evaluate` already records the field
# from) can go stale the moment an operator toggles Shift+Tab, since that
# fires no hook at all. `PreToolUse` fires immediately before the tool call it
# describes runs — including the Bash call running `bin/co-rollover`
# itself — which is what makes it the freshest source available.


def test_pretooluse_records_the_current_permission_mode_for_a_non_rolling_session(tmp_path):
    _seed(tmp_path, "s1", policy={"mode": "enforce", "seek_pct": 70})
    proc = _run(tmp_path, _payload("s1", "PreToolUse", permission_mode="plan"))
    assert proc.returncode == 0
    assert proc.stdout.strip() == "", "recording the mode must not itself say anything to the session"
    state = json.loads((tmp_path / ".cache" / "co-session-health" / "rollover-s1.json").read_text())
    assert state["permission_mode"] == "plan"


def test_pretooluse_overwrites_a_stale_persisted_permission_mode(tmp_path):
    """The freshness fix itself: a prior `Stop`/`UserPromptSubmit` firing
    persisted `default`, and this `PreToolUse` firing — carrying the mode as
    of right now — must replace it rather than leave the stale value sitting
    on disk for a rollover to read."""
    _seed(tmp_path, "s1", policy={"mode": "enforce", "seek_pct": 70},
         state={**RP.DEFAULT_STATE, "permission_mode": "default"})
    proc = _run(tmp_path, _payload("s1", "PreToolUse", permission_mode="plan"))
    assert proc.returncode == 0
    state = json.loads((tmp_path / ".cache" / "co-session-health" / "rollover-s1.json").read_text())
    assert state["permission_mode"] == "plan"


def test_pretooluse_with_no_permission_mode_in_the_payload_leaves_state_untouched(tmp_path):
    """Not every `PreToolUse` payload carries the field — a firing that omits
    it must not erase whatever was last recorded."""
    _seed(tmp_path, "s1", policy={"mode": "enforce", "seek_pct": 70},
         state={**RP.DEFAULT_STATE, "permission_mode": "plan"})
    proc = _run(tmp_path, _payload("s1", "PreToolUse"))
    assert proc.returncode == 0
    state = json.loads((tmp_path / ".cache" / "co-session-health" / "rollover-s1.json").read_text())
    assert state["permission_mode"] == "plan"


def test_pretooluse_never_records_a_permission_mode_for_a_rolling_session(tmp_path):
    """`test_pretooluse_never_calls_evaluate_so_rolling_state_is_untouched`
    above already pins that a rolling session's state survives a PreToolUse
    firing byte-for-byte with no `permission_mode` in the payload; this pins
    the same thing when the payload DOES carry one, since the fence returns
    before this recording call and must never reach it."""
    _seed(tmp_path, "s1", ctx_pct=10, policy={"mode": "enforce", "seek_pct": 70},
         rolling=_ROLLING_STATE)
    _run(tmp_path, _payload("s1", "PreToolUse", permission_mode="plan"))
    state = json.loads((tmp_path / ".cache" / "co-session-health" / "rollover-s1.json").read_text())
    assert state == _ROLLING_STATE


# --- `parked` (Slice 6): live and usable, but not told to roll again -------

_PARKED_STATE = {
    "state": "parked", "reason": "spawn_error", "since": 1_000_000_000,
    "handoff_path": "/tmp/h/handoff.md", "issue_id": "issue_ABC",
}


def test_pretooluse_allows_a_parked_sessions_tool_call(tmp_path):
    """`parked`, unlike `rolling`, is not a fence — the operator works in a
    parked session, so its own tool calls must go through unconditionally."""
    _seed(tmp_path, "s1", policy={"mode": "enforce"}, state=_PARKED_STATE)
    proc = _run(tmp_path, _payload("s1", "PreToolUse"))
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_a_parked_session_is_not_told_to_seek_even_deep_in_context(tmp_path):
    """Without `rollover_policy.evaluate()` treating `parked` as terminal,
    this firing would recompute a fresh zone from `ctx_pct` and tell the
    session to roll over again — exactly the automatic retry Slice 6 exists
    to not have."""
    _seed(tmp_path, "s1", ctx_pct=97, policy={"mode": "enforce", "seek_pct": 70, "floor_pct": 90},
         state=_PARKED_STATE)
    proc = _run(tmp_path, _payload("s1", "PostToolUse"))
    assert proc.returncode == 0
    assert proc.stdout.strip() == "", "a parked session must not be told to seek or roll again"
    state = json.loads((tmp_path / ".cache" / "co-session-health" / "rollover-s1.json").read_text())
    assert state["state"] == "parked", "must not have been overwritten"


# --- runs under the system interpreter the hooks config actually invokes ----

def test_imports_under_the_system_python3(tmp_path):
    """This hook is registered in the hooks config with no interpreter named
    (`platform/decisions/0090`'s Handoff), so Claude Code execs the file by
    its own shebang, `#!/usr/bin/python3` — macOS and Linux's fixed,
    root-owned system interpreter, 3.9 on the mini that actually runs these
    hooks. Run directly under that binary rather than `sys.executable` (which
    every other test in this file uses, and which is this repo's newer venv
    interpreter): a 3.10+-only construct in this file, or in anything it
    imports, would pass every other test here and still crash at import
    under the interpreter actually used.

    This alone does not prove the property everywhere this suite runs:
    `/usr/bin/python3` is whatever version the HOST ships, which on CI's
    `convivy-linux` runner is 3.10+ (not verified on ci1), not 3.9 — see
    `test_the_pinned_files_parse_under_python_3_9_grammar` below for the
    check that holds regardless of which interpreter actually runs it."""
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    env.pop("TMUX", None)
    env.pop("AGENTCTL_RUN_ID", None)
    env.pop("AGENT_SIGNAL_TOKEN", None)
    proc = subprocess.run(["/usr/bin/python3", str(HOOK)], input="{}",
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr


def test_the_pinned_files_parse_under_python_3_9_grammar():
    """The check above runs under whatever `/usr/bin/python3` is on the HOST,
    which is 3.9 only on the mini — CI's `convivy-linux` runner ships 3.10+ (not verified on ci1), so a
    reintroduced 3.10-only construct (a `match` statement, `except*`, ...)
    would pass every test in this file there and still crash on the mini.
    `ast.parse`'s `feature_version` gates the grammar the parser accepts, not
    which interpreter is running it, so this holds the same on every host —
    confirmed by planting a `match` statement here and watching it fail under
    both this repo's venv interpreter and a real 3.9 one.

    Checks the hook itself and every first-party module it imports
    (`_HOOK_LOCAL_IMPORTS`), the same breadth the real-import test above has
    when it actually runs under 3.9 — a construct in `rollover_policy` or
    `signal_client` crashes this hook at import exactly as one in the hook
    file itself would.

    This does not catch every possible 3.10-only construct — a runtime-only
    difference such as a bare `int | None` evaluated outside a deferred
    annotation is semantics, not grammar, and `ast.parse` cannot see it — but
    it is the mechanism that actually fails in CI, which a fixed-interpreter
    subprocess call does not."""
    for path in (HOOK, *_HOOK_LOCAL_IMPORTS):
        ast.parse(path.read_text(), filename=str(path), feature_version=(3, 9))
