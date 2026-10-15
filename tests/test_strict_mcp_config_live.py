"""Live check for issue_01M4APS4KRPE6ESTZAVEJVFDKC: a non-private dispatch must
never start a worktree's own committed `.mcp.json` server.

`_spawn_claude` (bin/agentctl) now passes `--strict-mcp-config` on every
dispatch, not only a black-box crew's (ADR-0043) — without it, a `--pr`
dispatch's worktree is the PR's own branch, and `claude -p` loads that
branch's committed `.mcp.json` alongside whatever `--mcp-config` names
(confirmed by live experiment, agent-platform#793 round 2: a worktree
containing a `.mcp.json` defining a `trustcheck` server, launched with an
explicit `--mcp-config` but no `--strict-mcp-config`, started BOTH servers).

This spawns the real `claude` binary twice and watches for a marker file a
stub stdio server writes the instant it starts — reading the real process
behaviour, not re-asserting the argv `test_strict_mcp_config_*` in
tests/test_agentctl.py already cover, since a flag present in argv and a flag
actually honoured by the CLI are two different claims.

The positive control (a bare `claude -p` with no `--strict-mcp-config` must
still start the project server) is what makes the main check meaningful:
without it, a broken stub and a working fix both produce "no marker" —
passing for the wrong reason forever.

Needs the `claude` binary, network, and a valid subscription auth token;
skips cleanly where either is absent. GATED ON AN EXPLICIT OPT-IN, not just
`claude` being on PATH — same reasoning as
tests/test_claude_thrifty_sonic_pin.py: a bare `pytest` on an
already-authenticated dev machine must not launch live sessions by accident.
Costs two haiku calls. Not run in CI (no `claude` binary there, and the
opt-in is unset).

Every path here is a tmp_path scratch directory; nothing under this file
touches a real worktree or the real run registry (BUILDER_ROOT is already
repointed at a tempdir by tests/conftest.py for the whole suite)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import types
from pathlib import Path

import pytest

CLAUDE_BIN = shutil.which("claude")
_LIVE_OPT_IN = os.environ.get("AGENTCTL_RUN_LIVE_CLAUDE_TESTS") == "1"

pytestmark = pytest.mark.skipif(
    CLAUDE_BIN is None or not _LIVE_OPT_IN,
    reason="claude binary not on PATH, or AGENTCTL_RUN_LIVE_CLAUDE_TESTS!=1 — "
           "this guard needs a live spawn (network + a subscription auth "
           "token) and is opt-in so a bare `pytest` on an already-"
           "authenticated dev machine doesn't launch live sessions by "
           "accident (same reasoning as PR #505); set "
           "AGENTCTL_RUN_LIVE_CLAUDE_TESTS=1 to run it deliberately",
)

_PROBE_TIMEOUT_SECONDS = 60
_MARKER_WAIT_SECONDS = 20
_PROMPT = "Reply with the single word OK. Use no tools."


def _plant_trustcheck_mcp_json(worktree: Path, marker: Path, stub_script: Path) -> None:
    """Writes `stub_script` (a stdio process that marks its own start and then
    idles reading stdin until the parent closes the pipe) and a `.mcp.json`
    naming it, into `worktree` — the shape of a PR branch that commits a
    server definition to get it started outside the sandbox the moment a
    dispatch's worktree is that branch."""
    stub_script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"open({str(marker)!r}, 'w').write('started\\n')\n"
        "for _ in sys.stdin:\n"
        "    pass\n"
    )
    stub_script.chmod(0o755)
    (worktree / ".mcp.json").write_text(json.dumps({
        "mcpServers": {
            "trustcheck": {
                "type": "stdio",
                "command": "python3",
                "args": [str(stub_script)],
            }
        }
    }))


def _wait_for_marker(marker: Path, seconds: float) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if marker.exists():
            return True
        time.sleep(0.5)
    return marker.exists()


def test_strict_mcp_config_positive_control_project_mcp_json_loads_without_it(tmp_path):
    """Positive control, run OUTSIDE agentctl: a bare `claude -p` with no
    `--strict-mcp-config` loads a worktree's committed `.mcp.json` (confirmed
    empirically on agent-platform#793 round 2). Without this control, "no
    marker" from the agentctl-driven check below can't be told apart from a
    broken stub server — both look identical."""
    worktree = tmp_path / "wt-control"
    worktree.mkdir()
    marker = tmp_path / "marker-control"
    stub_script = tmp_path / "trustcheck_stub_control.py"
    _plant_trustcheck_mcp_json(worktree, marker, stub_script)

    result = subprocess.run(
        [CLAUDE_BIN, "-p", _PROMPT,
         "--model", "haiku",
         "--permission-mode", "bypassPermissions",
         "--output-format", "text"],
        cwd=str(worktree), capture_output=True, text=True,
        timeout=_PROBE_TIMEOUT_SECONDS,
    )
    assert result.returncode == 0, (
        f"claude -p exited {result.returncode}; stderr:\n{result.stderr}")
    assert _wait_for_marker(marker, _MARKER_WAIT_SECONDS), (
        "the project .mcp.json's trustcheck server never started even with "
        "no --strict-mcp-config at all — either the stub is broken, or "
        "claude -p no longer loads a project .mcp.json by default; the main "
        "check below is not meaningful until this passes again")


@pytest.mark.usefixtures("_tool_session_scratch_dir_off_the_real_account_home")
def test_strict_mcp_config_blocks_a_worktrees_committed_mcp_json(
        agentctl, tmp_path, monkeypatch):
    """The regression check for issue_01M4APS4KRPE6ESTZAVEJVFDKC: a
    `.mcp.json` planted in a scratch worktree, dispatched through agentctl's
    REAL argv builder (`_run_session` → `_spawn_claude`, repo="agent-platform"
    — non-private, the shape every ordinary coder/reviewer dispatch takes)
    must never start that server. `--strict-mcp-config` (now unconditional)
    must suppress it, while `kb`/`ops` still reach the run through the
    explicit `--mcp-config` this change adds.

    This drives `_run_session` for real (the other test in this file, the
    positive control, never touches agentctl), so it applies the shared
    fixture that keeps a real dispatch's tool-session scratch directory off
    the real account's home (conftest.py, CQ·138, round 6, reviewer, PR #879,
    comment 6166526636) — otherwise live, opted-in run of this test would
    create one there."""
    run_dir = tmp_path / "run-live-mcp"
    run_dir.mkdir()
    worktree = tmp_path / "wt-live-mcp"
    worktree.mkdir()
    marker = tmp_path / "marker-main"
    stub_script = tmp_path / "trustcheck_stub_main.py"
    _plant_trustcheck_mcp_json(worktree, marker, stub_script)

    monkeypatch.setattr(agentctl, "CLAUDE_BIN", CLAUDE_BIN)
    monkeypatch.setattr(agentctl, "_gateway_virtual_key", lambda: "vk-test")
    monkeypatch.setattr(agentctl, "_gateway_url", lambda: "http://127.0.0.1:9")
    monkeypatch.setattr(agentctl, "_finalize_run", lambda **kw: None)
    monkeypatch.setattr(agentctl, "_preflight_python_worktree", lambda wt: None)
    monkeypatch.setattr(agentctl.R, "reserve_slot", lambda entry: None)
    monkeypatch.setattr(agentctl.R, "attach_pid",
                         lambda run_id, pid, pid_started_at=None, **_kw: None)
    monkeypatch.setattr(agentctl.R, "remove_run", lambda rid: True)
    monkeypatch.setattr(agentctl, "_run_origin", lambda parent=None: "scheduled")

    args = types.SimpleNamespace(agent="reviewer", task="t", parent=None,
                                 dry_run=False, label=None, model=None,
                                 review_sha=None)
    agent_def = {"body": "You are a test probe.", "model_alias": "haiku",
                 "frontmatter": {"model": "haiku"}}
    rc = agentctl._run_session(
        args=args, agent=agent_def, run_id="run-live-mcp", run_dir=run_dir,
        worktree=worktree, profile="p", repo="agent-platform",
        started_at="2026-06-11T00:00:00Z", user_prompt=_PROMPT)
    assert rc == 0, "the live dispatch itself must succeed"
    assert not marker.exists(), (
        "the worktree's committed .mcp.json started its trustcheck server "
        "through an agentctl dispatch — --strict-mcp-config did not hold")
