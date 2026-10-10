"""co restore (CQ·392): the window, the marker, the live checks, the steward rule,
the agent argument, the launch directory, the ensure-server refusal, the stagger
and the dry run.

Every tmux call here runs on a private socket (`tmux -L <unique>`) with TMUX and
TMUX_PANE removed and TMUX_TMPDIR pointed at a temp directory, so no test can reach
the operator's server. `co` and `session-agent` are stubs on PATH. The resume tests
run a real pane shell, so the stubbed `co` records what the pane was sent.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import pytest

import co_restore as CR
import tmux_sandbox

NOW = 1_800_000_000.0           # a fixed "now" for window arithmetic
WINDOW_START = NOW - 3600
KILL_TIME = NOW - 1800


# --- transcripts ---------------------------------------------------------------

def write_transcript(projects: Path, slug_dir: str, session_id: str, *, cwd: str,
                     mtime: float, entrypoint: str = "cli", title: str | None = None,
                     agent: str | None = None) -> Path:
    directory = projects / slug_dir
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"
    entries = [{"type": "user", "entrypoint": entrypoint, "cwd": cwd,
                "message": "private conversation text the restore must never read"}]
    if title is not None:
        entries.append({"type": "custom-title", "customTitle": title, "sessionId": session_id})
    if agent is not None:
        entries.append({"type": "system", "agentSetting": agent})
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    os.utime(path, (mtime, mtime))
    return path


def slug_for(path: str) -> str:
    return CR.project_slug(path)


# --- fixtures ------------------------------------------------------------------

@pytest.fixture
def tmux_sock(monkeypatch):
    """A private tmux socket with a long-lived anchor session that keeps the server up."""
    reason = tmux_sandbox.sandbox_refusal()
    if reason:
        pytest.skip(reason)
    tmp = Path(tempfile.mkdtemp(prefix="cr-tmux-"))
    socket = f"cr-test-{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp))
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    yield socket
    subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True)
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def stubs(tmp_path, tmux_sock, monkeypatch):
    """`co` and `session-agent` stubs on PATH, and a root with a steward directory."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "log"
    log.mkdir()
    co_log = log / "co.log"
    calls_log = log / "calls.log"
    (bin_dir / "co").write_text(
        "#!/bin/sh\n"
        f"{{ printf 'pwd=%s\\n' \"$PWD\"; for a in \"$@\"; do printf 'arg=%s\\n' \"$a\"; done; }} >> '{co_log}'\n")
    (bin_dir / "co").chmod(0o755)
    # Pane shells run /bin/sh, so a login shell's path_helper cannot drop the stub from PATH.
    config = tmp_path / "tmux.conf"
    config.write_text("set -g default-shell /bin/sh\n")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    anchor = subprocess.run(["tmux", "-L", tmux_sock, "-f", str(config), "new-session", "-d",
                             "-s", "anchor", "sleep 3600"], capture_output=True, text=True)
    assert anchor.returncode == 0, anchor.stderr

    class Stubs:
        pass

    s = Stubs()
    s.bin_dir = bin_dir
    s.log = log
    s.co_log = co_log
    s.calls_log = calls_log
    s.root = tmp_path / "Convivy"
    (s.root / "substrate").mkdir(parents=True)
    (s.root / "vivy").mkdir()
    (s.root / "convivy-deck").mkdir()
    s.projects = tmp_path / "projects"
    s.projects.mkdir()
    s.tmp = tmp_path

    def set_session_agent(exit_code: int = 0, message: str = "") -> None:
        # Records its call, and the number of crew sessions that existed when it ran.
        body = (
            "#!/bin/sh\n"
            f"echo ensure >> '{calls_log}'\n"
            f"tmux -L {tmux_sock} list-sessions -F '#{{session_name}}' 2>/dev/null"
            f" | grep -v '^anchor$' | wc -l >> '{log}/ensure-sessions.log'\n"
            + (f"echo '{message}' >&2\n" if message else "")
            + f"exit {exit_code}\n")
        agent = bin_dir / "session-agent"
        agent.write_text(body)
        agent.chmod(0o755)

    set_session_agent()
    s.set_session_agent = set_session_agent
    return s


def live_names(tmux_sock: str) -> list[str]:
    done = subprocess.run(["tmux", "-L", tmux_sock, "list-sessions", "-F", "#{session_name}"],
                          capture_output=True, text=True)
    if done.returncode != 0:
        return []
    return [n for n in done.stdout.split() if n != "anchor"]


def co_invocations(stubs) -> list[dict]:
    """Each stubbed `co` call: its working directory and its arguments."""
    if not stubs.co_log.exists():
        return []
    calls, current = [], None
    for line in stubs.co_log.read_text().splitlines():
        key, _, value = line.partition("=")
        if key == "pwd":
            current = {"pwd": value, "args": []}
            calls.append(current)
        elif key == "arg" and current is not None:
            current["args"].append(value)
    return calls


def wait_for_co(stubs, count: int, timeout: float = 10.0) -> list[dict]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        calls = co_invocations(stubs)
        if len(calls) >= count:
            return calls
        time.sleep(0.1)
    return co_invocations(stubs)


def restore(stubs, tmux_sock, *, names, dry_run=False, stagger=0.0, sleep=None,
            start=WINDOW_START, until=None, session_agent="session-agent") -> int:
    return CR.restore(root=str(stubs.root), projects_dir=stubs.projects, start=start,
                      until=until, dry_run=dry_run, stagger=stagger, names=names,
                      tmux_socket=tmux_sock, session_agent=session_agent,
                      sleep=sleep or (lambda seconds: None))


# --- the window and the marker -------------------------------------------------

def test_window_takes_only_interactive_transcripts_written_in_range(stubs, tmux_sock, capsys):
    vivy = str(stubs.root / "vivy")
    write_transcript(stubs.projects, slug_for(vivy), "in-window", cwd=vivy,
                     mtime=KILL_TIME)
    write_transcript(stubs.projects, slug_for(vivy), "too-old", cwd=vivy,
                     mtime=WINDOW_START - 60)
    write_transcript(stubs.projects, slug_for(vivy), "after-kill", cwd=vivy,
                     mtime=KILL_TIME + 900)
    rc = restore(stubs, tmux_sock, names={"in-window": "vivy"}, dry_run=True,
                 until=KILL_TIME + 60)
    out = capsys.readouterr().out
    assert rc == 0
    assert "in-window" in out
    assert "too-old" not in out
    assert "after-kill" not in out


def test_dispatched_runs_are_left_out_even_inside_the_window(stubs, tmux_sock, capsys):
    worktree = stubs.root / "worktrees" / "agent-platform.coder-x"
    worktree.mkdir(parents=True)
    write_transcript(stubs.projects, slug_for(str(worktree)), "dispatched",
                     cwd=str(worktree), mtime=KILL_TIME, entrypoint="sdk-cli")
    vivy = str(stubs.root / "vivy")
    write_transcript(stubs.projects, slug_for(vivy), "interactive", cwd=vivy,
                     mtime=KILL_TIME)
    restore(stubs, tmux_sock, names={"dispatched": "x", "interactive": "vivy"}, dry_run=True)
    out = capsys.readouterr().out
    # Match the session token, not the word: the pytest tmp path contains "dispatched".
    assert "session dispatched" not in out
    assert "session interactive" in out


def test_marker_is_the_entrypoint_so_an_interactive_session_in_a_worktree_is_kept(stubs):
    # The worktrees directory is not the marker: a person's co session can run in a
    # worktree, and it writes `cli` like any other interactive session.
    worktree = stubs.root / "worktrees" / "agent-platform.someone"
    worktree.mkdir(parents=True)
    write_transcript(stubs.projects, slug_for(str(worktree)), "in-worktree",
                     cwd=str(worktree), mtime=KILL_TIME, entrypoint="cli")
    found = CR.interactive_transcripts(stubs.projects, WINDOW_START, None)
    assert [t.session_id for t in found] == ["in-worktree"]


# --- the launch directory and the agent argument -------------------------------

def test_launch_directory_comes_from_the_slug_so_a_subdirectory_resumes_from_the_root(
        stubs, tmux_sock):
    sub = stubs.root / "vivy" / "src" / "deep"
    sub.mkdir(parents=True)
    write_transcript(stubs.projects, slug_for(str(stubs.root / "vivy")), "sub-session",
                     cwd=str(sub), mtime=KILL_TIME, title="vivy")
    rc = restore(stubs, tmux_sock, names={"sub-session": "vivy"})
    assert rc == 0
    calls = wait_for_co(stubs, 1)
    assert len(calls) == 1
    assert os.path.realpath(calls[0]["pwd"]) == os.path.realpath(str(stubs.root / "vivy"))


def test_a_generic_path_crew_gets_its_agent_as_the_first_argument(stubs, tmux_sock):
    vivy = str(stubs.root / "vivy")
    write_transcript(stubs.projects, slug_for(vivy), "vivy-session", cwd=vivy,
                     mtime=KILL_TIME, agent="planning-orchestrator")
    rc = restore(stubs, tmux_sock, names={"vivy-session": "vivy"})
    assert rc == 0
    calls = wait_for_co(stubs, 1)
    assert calls[0]["args"][:4] == ["planning-orchestrator", "--resume", "vivy-session",
                                    CR.RESUME_PROMPT]


def test_a_crew_without_an_agent_gets_no_agent_argument(stubs, tmux_sock):
    deck = str(stubs.root / "convivy-deck")
    write_transcript(stubs.projects, slug_for(deck), "deck-session", cwd=deck,
                     mtime=KILL_TIME, title="Deck")
    rc = restore(stubs, tmux_sock, names={"deck-session": "deck"})
    assert rc == 0
    calls = wait_for_co(stubs, 1)
    assert calls[0]["args"][:3] == ["--resume", "deck-session", CR.RESUME_PROMPT]


def test_an_agent_setting_that_is_a_flag_is_refused_not_passed_to_co(stubs, tmux_sock, capsys):
    vivy = str(stubs.root / "vivy")
    write_transcript(stubs.projects, slug_for(vivy), "flagged", cwd=vivy,
                     mtime=KILL_TIME, agent="--model")
    restore(stubs, tmux_sock, names={"flagged": "vivy"}, dry_run=True)
    out = capsys.readouterr().out
    assert "is not an agent name" in out


# --- the live checks -----------------------------------------------------------

def test_a_crew_already_live_at_the_top_is_skipped(stubs, tmux_sock, capsys):
    vivy = str(stubs.root / "vivy")
    write_transcript(stubs.projects, slug_for(vivy), "v1", cwd=vivy, mtime=KILL_TIME)
    subprocess.run(["tmux", "-L", tmux_sock, "new-session", "-d", "-s", "vivy", "sleep 600"],
                   check=True)
    restore(stubs, tmux_sock, names={"v1": "vivy"}, dry_run=True)
    assert "tmux session vivy is already live" in capsys.readouterr().out


def test_liveness_is_checked_immediately_before_each_start_not_once_at_the_top(
        stubs, tmux_sock, capsys):
    # Two crews, both absent at the top. Between the two starts, someone starts the
    # second crew by hand (the 2026-09-27 case). The second start must notice it.
    vivy = str(stubs.root / "vivy")
    deck = str(stubs.root / "convivy-deck")
    write_transcript(stubs.projects, slug_for(vivy), "v1", cwd=vivy, mtime=KILL_TIME)
    write_transcript(stubs.projects, slug_for(deck), "d1", cwd=deck, mtime=KILL_TIME - 5)

    def someone_starts_deck(seconds: float) -> None:
        subprocess.run(["tmux", "-L", tmux_sock, "new-session", "-d", "-s", "deck",
                        "sleep 600"], check=True)

    rc = restore(stubs, tmux_sock, names={"v1": "vivy", "d1": "deck"},
                 sleep=someone_starts_deck)
    out = capsys.readouterr().out
    assert rc == 0
    assert "sent          vivy" in out
    assert "skip          deck: tmux session deck went live before its start" in out
    assert "deck" in live_names(tmux_sock)
    # Only the first crew gets a `co`: the second was skipped, so no resume was typed for it.
    wait_for_co(stubs, 1)
    time.sleep(0.5)
    calls = co_invocations(stubs)
    assert len(calls) == 1
    assert calls[0]["args"][:2] == ["--resume", "v1"]


# --- the steward rule ----------------------------------------------------------

def test_a_steward_is_not_resumed_while_a_steward_pane_is_running(stubs, tmux_sock, capsys):
    steward = str(stubs.root / "substrate")
    write_transcript(stubs.projects, slug_for(steward), "steward-1", cwd=steward,
                     mtime=KILL_TIME, title="convivy")
    subprocess.run(["tmux", "-L", tmux_sock, "new-session", "-d", "-s", "running-steward",
                    "-c", steward, "sleep 600"], check=True)
    restore(stubs, tmux_sock, names={"steward-1": "steward"}, dry_run=True)
    assert "a steward is already running" in capsys.readouterr().out


def test_only_one_steward_is_resumed_per_run(stubs, tmux_sock, capsys):
    steward = str(stubs.root / "substrate")
    write_transcript(stubs.projects, slug_for(steward), "steward-new", cwd=steward,
                     mtime=KILL_TIME)
    write_transcript(stubs.projects, slug_for(steward), "steward-old", cwd=steward,
                     mtime=KILL_TIME - 600)
    restore(stubs, tmux_sock, names={"steward-new": "steward", "steward-old": "steward-b"},
            dry_run=True)
    out = capsys.readouterr().out
    assert "resume" in out and "steward-new" in out
    assert "never resume a second steward" in out


# --- the ensure-server refusal -------------------------------------------------

def test_ensure_server_is_called_before_any_session_and_sees_none(stubs, tmux_sock):
    vivy = str(stubs.root / "vivy")
    write_transcript(stubs.projects, slug_for(vivy), "v1", cwd=vivy, mtime=KILL_TIME)
    stubs.set_session_agent(0, "")
    rc = restore(stubs, tmux_sock, names={"v1": "vivy"})
    assert rc == 0
    assert stubs.calls_log.read_text().split() == ["ensure"]
    assert (stubs.log / "ensure-sessions.log").read_text().split() == ["0"]


def test_an_ensure_server_refusal_stops_the_run_with_its_message_and_no_session(
        stubs, tmux_sock, capsys):
    vivy = str(stubs.root / "vivy")
    write_transcript(stubs.projects, slug_for(vivy), "v1", cwd=vivy, mtime=KILL_TIME)
    stubs.set_session_agent(1, "tmux server is not GUI-born; kickstart the keeper")
    rc = restore(stubs, tmux_sock, names={"v1": "vivy"})
    captured = capsys.readouterr()
    assert rc == 1
    assert "kickstart the keeper" in captured.err
    assert live_names(tmux_sock) == []
    assert co_invocations(stubs) == []


# --- the stagger and the dry run -----------------------------------------------

def test_the_stagger_is_a_parameter_and_separates_the_starts(stubs, tmux_sock):
    vivy = str(stubs.root / "vivy")
    deck = str(stubs.root / "convivy-deck")
    write_transcript(stubs.projects, slug_for(vivy), "v1", cwd=vivy, mtime=KILL_TIME)
    write_transcript(stubs.projects, slug_for(deck), "d1", cwd=deck, mtime=KILL_TIME - 5)
    waits: list[float] = []
    rc = restore(stubs, tmux_sock, names={"v1": "vivy", "d1": "deck"}, stagger=7.5,
                 sleep=waits.append)
    assert rc == 0
    assert waits == [7.5]


def test_the_default_stagger_is_five_seconds():
    assert CR.DEFAULT_STAGGER_SECONDS == 5.0


def test_dry_run_prints_the_plan_and_starts_nothing(stubs, tmux_sock, capsys):
    vivy = str(stubs.root / "vivy")
    write_transcript(stubs.projects, slug_for(vivy), "v1", cwd=vivy, mtime=KILL_TIME)
    stubs.set_session_agent(0, "")
    rc = restore(stubs, tmux_sock, names={"v1": "vivy"}, dry_run=True)
    out = capsys.readouterr().out
    assert rc == 0
    assert "would resume" in out and "vivy" in out
    assert "dry run: nothing started" in out
    assert live_names(tmux_sock) == []
    assert not stubs.calls_log.exists()
    assert co_invocations(stubs) == []


# --- what a real run does with an unnamed crew ---------------------------------

def test_a_transcript_with_no_recorded_tmux_name_is_skipped_and_nothing_is_guessed(
        stubs, tmux_sock, capsys):
    vivy = str(stubs.root / "vivy")
    write_transcript(stubs.projects, slug_for(vivy), "v1", cwd=vivy, mtime=KILL_TIME,
                     title="vivy")
    rc = restore(stubs, tmux_sock, names={})
    assert rc == 0
    out = capsys.readouterr().out
    assert "no tmux session name on record" in out
    assert "pass --session SESSION_ID=TMUX_NAME" in out
    assert live_names(tmux_sock) == []
    assert not stubs.calls_log.exists()


def test_unknown_tmux_state_refuses_rather_than_starting(stubs, tmux_sock, monkeypatch, capsys):
    vivy = str(stubs.root / "vivy")
    write_transcript(stubs.projects, slug_for(vivy), "v1", cwd=vivy, mtime=KILL_TIME)
    real_run = subprocess.run

    def refusing(argv, **kwargs):
        if "list-panes" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "permission denied")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(CR.subprocess, "run", refusing)
    rc = restore(stubs, tmux_sock, names={"v1": "vivy"})
    assert rc == 1
    assert "cannot read tmux state: permission denied" in capsys.readouterr().err
    assert not stubs.calls_log.exists()


@pytest.mark.parametrize("stderr", [
    "error connecting to /tmp/tmux-501/default (Permission denied)\n",
    "error connecting to /tmp/tmux-501/default (Operation not permitted)\n",
])
def test_a_socket_failure_that_is_not_no_server_stops_the_run(
        stubs, tmux_sock, monkeypatch, capsys, stderr):
    vivy = str(stubs.root / "vivy")
    write_transcript(stubs.projects, slug_for(vivy), "v1", cwd=vivy, mtime=KILL_TIME)
    real_run = subprocess.run

    def refusing(argv, **kwargs):
        if "list-panes" in argv:
            return subprocess.CompletedProcess(argv, 1, "", stderr)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(CR.subprocess, "run", refusing)
    rc = restore(stubs, tmux_sock, names={"v1": "vivy"})
    assert rc == 1
    assert "cannot read tmux state" in capsys.readouterr().err
    assert live_names(tmux_sock) == []
    assert not stubs.calls_log.exists()


@pytest.mark.parametrize("stderr", [
    "no server running on /tmp/tmux-501/default\n",
    # What tmux 3.7 prints when no socket file exists (checked 2026-10-05 on a private
    # socket path that did not exist). Restore runs after a server kill, so this must
    # read as no server, or restore would refuse every time it was needed.
    "error connecting to /tmp/tmux-501/default (No such file or directory)\n",
])
def test_tmux_saying_there_is_no_server_reads_as_no_panes(monkeypatch, stderr):
    def no_server(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "", stderr)

    monkeypatch.setattr(CR.subprocess, "run", no_server)
    # The process check is stubbed: the machine running these tests may have tmux running.
    monkeypatch.setattr(CR, "tmux_processes", lambda: [])
    assert CR.list_panes(["tmux"], {}) == []


def _missing_socket(monkeypatch, processes: list[tuple[int, str]]) -> None:
    """tmux reports a missing socket file; the process check reports `processes`."""
    stderr = "error connecting to /tmp/tmux-501/default (No such file or directory)\n"
    real_run = subprocess.run

    def socket_gone(argv, **kwargs):
        # Only list-panes sees the missing socket; the test's own tmux calls go to the real server.
        if "list-panes" in argv:
            return subprocess.CompletedProcess(argv, 1, "", stderr)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(CR.subprocess, "run", socket_gone)
    monkeypatch.setattr(CR, "tmux_processes", lambda: processes)


def test_a_missing_socket_with_no_tmux_process_is_no_server(monkeypatch):
    # A kill-server leaves this state: the socket file is unlinked and no process runs.
    _missing_socket(monkeypatch, [])
    assert CR.list_panes(["tmux"], {}) == []


def test_a_missing_socket_with_a_live_tmux_process_refuses_with_the_kill_usr1_advice(monkeypatch):
    # A live server whose socket file was deleted prints the same message. Reading it as
    # no server would let the start make a second server at the same path.
    _missing_socket(monkeypatch, [(4242, "tmux new-session -d -s a.b.test")])
    with pytest.raises(CR.RestoreError) as refused:
        CR.list_panes(["tmux"], {})
    message = str(refused.value)
    assert "pid 4242: tmux new-session -d -s a.b.test" in message
    assert "Restore will not run while any tmux process of this user is alive" in message
    assert "`kill -USR1 <its pid>` recreates the socket" in message
    assert "otherwise end the processes you do not need" in message
    assert "Nothing was started" in message


def test_the_refusal_lists_every_tmux_process_with_its_command_line(monkeypatch):
    # Two processes, one a default server and one on another socket: the operator needs
    # both command lines to tell them apart.
    _missing_socket(monkeypatch, [(4242, "tmux new-session -d -s a.b.test"),
                                  (4243, "tmux -L other new-session -d -s other")])
    with pytest.raises(CR.RestoreError) as refused:
        CR.list_panes(["tmux"], {})
    message = str(refused.value)
    assert "  pid 4242: tmux new-session -d -s a.b.test\n" in message
    assert "  pid 4243: tmux -L other new-session -d -s other\n" in message


def test_a_live_tmux_process_with_a_gone_socket_stops_the_run_before_any_start(
        stubs, tmux_sock, monkeypatch, capsys):
    vivy = str(stubs.root / "vivy")
    write_transcript(stubs.projects, slug_for(vivy), "v1", cwd=vivy, mtime=KILL_TIME)
    _missing_socket(monkeypatch, [(4242, "tmux new-session -d -s a.b.test")])
    rc = restore(stubs, tmux_sock, names={"v1": "vivy"})
    assert rc == 1
    err = capsys.readouterr().err
    assert "pid 4242: tmux new-session -d -s a.b.test" in err and "Nothing was started" in err
    assert not stubs.calls_log.exists()          # ensure-server never ran
    assert live_names(tmux_sock) == []           # and no session was created


def test_tmux_processes_reads_pgrep_exit_codes(monkeypatch):
    def pgrep_says(code: int, out: str = ""):
        def run(argv, **kwargs):
            assert argv[0] == "pgrep" and "tmux" in argv
            return subprocess.CompletedProcess(argv, code, out, "")
        return run

    monkeypatch.setattr(CR.subprocess, "run", pgrep_says(1))
    assert CR.tmux_processes() == []
    monkeypatch.setattr(CR.subprocess, "run", pgrep_says(2))
    with pytest.raises(CR.RestoreError, match="cannot check for a tmux process"):
        CR.tmux_processes()


def test_tmux_processes_pairs_each_pid_with_its_ps_command_line(monkeypatch):
    # macOS pgrep prints pids only; ps supplies the command lines. A pid ps no longer lists
    # (it exited in between) keeps a placeholder rather than dropping out of the refusal.
    def pgrep_and_ps(argv, **kwargs):
        if argv[0] == "pgrep":
            return subprocess.CompletedProcess(argv, 0, "71921\n76841\n", "")
        assert argv[0] == "ps" and argv[-1] == "71921,76841"
        return subprocess.CompletedProcess(argv, 0, "76841 tmux new-session -d -s a.b.test\n", "")

    monkeypatch.setattr(CR.subprocess, "run", pgrep_and_ps)
    processes = CR.tmux_processes()
    assert processes[0][0] == 71921 and "command line not read" in processes[0][1]
    assert processes[1] == (76841, "tmux new-session -d -s a.b.test")


# --- plan details that need no tmux --------------------------------------------

def test_clean_env_drops_the_inherited_tmux_identity_and_the_rollover_markers(monkeypatch):
    # An inherited TMUX sent a dispatched coder's kill-server to the live crews (2026-09-13).
    monkeypatch.setenv("TMUX", "/tmp/tmux-501/default,1,0")
    monkeypatch.setenv("TMUX_PANE", "%4")
    monkeypatch.setenv("CO_ROLLOVER_SUCCESSOR", "1")
    monkeypatch.setenv("KEEP_ME", "yes")
    env = CR.clean_env()
    assert "TMUX" not in env and "TMUX_PANE" not in env
    assert "CO_ROLLOVER_SUCCESSOR" not in env
    assert env["KEEP_ME"] == "yes"


def test_clean_env_drops_the_co_credential_path_so_tmux_never_inherits_it(monkeypatch):
    # tmux gives a server's global environment, and every pane made on it, the environment
    # of the process that starts the server. A restore started with the path in its
    # environment would otherwise put it in every later pane (probed 2026-10-06).
    monkeypatch.setenv("ORIENT_OPS_CO_TOKEN_FILE", "/tmp/co-credential")
    env = CR.clean_env()
    assert "ORIENT_OPS_CO_TOKEN_FILE" not in env


def test_a_transcript_whose_slug_names_no_ancestor_of_its_cwd_is_skipped():
    t = CR.Transcript(session_id="orphan", project_dir="-srv-convivy-gone",
                      mtime=KILL_TIME, entrypoint="cli", cwd="/srv/convivy/ask",
                      title="example--tennis", agent=None)
    assert CR.launch_dir(t) is None
    steps = CR.plan([t], {"orphan": "example--tennis"}, set(), "/x/substrate", False)
    assert steps[0].skip.startswith("no launch directory")


def test_the_newest_transcript_claims_a_name_and_an_older_one_for_the_same_crew_is_skipped(
        tmp_path):
    root = tmp_path / "Convivy"
    (root / "vivy").mkdir(parents=True)
    newer = CR.Transcript("new", project_dir=slug_for(str(root / "vivy")), mtime=KILL_TIME,
                          entrypoint="cli", cwd=str(root / "vivy"), title=None, agent=None)
    older = CR.Transcript("old", project_dir=slug_for(str(root / "vivy")), mtime=KILL_TIME - 9,
                          entrypoint="cli", cwd=str(root / "vivy"), title=None, agent=None)
    steps = CR.plan([newer, older], {"new": "vivy", "old": "vivy"}, set(),
                    str(root / "substrate"), False)
    assert steps[0].skip is None
    assert "a newer transcript already resumes tmux session vivy" in steps[1].skip


def test_window_arithmetic_for_since_and_from(tmp_path):
    assert CR._window_start(NOW, "2h", None) == NOW - 7200
    assert CR._window_start(NOW, "90m", None) == NOW - 5400
    assert CR._window_start(NOW, None, "2026-10-05T08:20") == \
        dt.datetime(2026, 10, 5, 8, 20).timestamp()


def test_main_refuses_a_malformed_duration_without_starting_anything(capsys, tmp_path):
    with pytest.raises(SystemExit) as refused:
        CR.main(["--since", "2x", "--root", str(tmp_path)])
    assert refused.value.code == 2
    assert "--since takes a duration" in capsys.readouterr().err


# --- where the tmux name comes from: co-* records and --session (CQ·392) -------

WINDOW_START_ISO = dt.datetime.fromtimestamp(WINDOW_START).isoformat()


def write_record(runs: Path, session_id: str, tmux_session: str | None = None,
                 day: str = "2026-10-05") -> None:
    """A co-* identity record as the SessionStart hook writes it."""
    directory = runs / day / f"co-{session_id}"
    directory.mkdir(parents=True, exist_ok=True)
    meta = {"kind": "co-session", "session_id": session_id, "cwd": ""}
    if tmux_session is not None:
        meta["tmux_session"] = tmux_session
    (directory / "meta.json").write_text(json.dumps(meta))


def run_main(stubs, tmux_sock: str, runs: Path, *extra: str) -> int:
    """`co restore` through the CLI entry point, with the test seams for transcripts, runs and tmux."""
    return CR.main(["--from", WINDOW_START_ISO, "--root", str(stubs.root),
                    "--projects-dir", str(stubs.projects), "--runs-root", str(runs),
                    "--tmux-socket", tmux_sock, *extra])


def test_the_tmux_name_comes_from_the_co_record_of_the_same_session(stubs, tmux_sock, tmp_path, capsys):
    vivy = str(stubs.root / "vivy")
    write_transcript(stubs.projects, slug_for(vivy), "v1", cwd=vivy, mtime=KILL_TIME)
    runs = tmp_path / "runs"
    write_record(runs, "v1", "vivy")
    write_record(runs, "other", "decoy")
    rc = run_main(stubs, tmux_sock, runs, "--dry-run")
    out = capsys.readouterr().out
    assert rc == 0
    assert any(line.startswith("would resume  vivy  ") and "name from co-* record" in line
               for line in out.splitlines())
    assert "decoy" not in out, "a record for a session with no transcript names nothing"


def test_a_record_without_a_tmux_session_leaves_the_crew_skipped_with_the_reason(
        stubs, tmux_sock, tmp_path, capsys):
    vivy = str(stubs.root / "vivy")
    write_transcript(stubs.projects, slug_for(vivy), "v1", cwd=vivy, mtime=KILL_TIME)
    runs = tmp_path / "runs"
    write_record(runs, "v1")  # pre-dates the field, or the session was not in a pane
    rc = run_main(stubs, tmux_sock, runs, "--dry-run")
    out = capsys.readouterr().out
    assert rc == 0
    assert "skip          session v1: no tmux session name on record" in out
    assert "pass --session SESSION_ID=TMUX_NAME" in out
    assert "would resume" not in out


def test_a_session_flag_overrides_the_record_name(stubs, tmux_sock, tmp_path, capsys):
    vivy = str(stubs.root / "vivy")
    write_transcript(stubs.projects, slug_for(vivy), "v1", cwd=vivy, mtime=KILL_TIME)
    runs = tmp_path / "runs"
    write_record(runs, "v1", "vivy")
    rc = run_main(stubs, tmux_sock, runs, "--dry-run", "--session", "v1=crewx")
    out = capsys.readouterr().out
    assert rc == 0
    lines = out.splitlines()
    assert any(line.startswith("would resume  crewx  ") and "name from --session" in line
               for line in lines)
    assert not any(line.startswith("would resume  vivy") for line in lines)


def test_a_session_flag_adds_a_session_the_window_missed(stubs, tmux_sock, tmp_path, capsys):
    vivy = str(stubs.root / "vivy")
    write_transcript(stubs.projects, slug_for(vivy), "v1", cwd=vivy,
                     mtime=WINDOW_START - 600)  # written before the window opens
    runs = tmp_path / "runs"
    assert run_main(stubs, tmux_sock, runs, "--dry-run") == 0
    assert "would resume" not in capsys.readouterr().out
    assert run_main(stubs, tmux_sock, runs, "--dry-run", "--session", "v1=vivy") == 0
    out = capsys.readouterr().out
    assert any(line.startswith("would resume  vivy  ") and "picked by --session" in line
               for line in out.splitlines())


def test_a_session_flag_naming_no_interactive_transcript_is_reported(stubs, tmux_sock, tmp_path, capsys):
    vivy = str(stubs.root / "vivy")
    write_transcript(stubs.projects, slug_for(vivy), "bot", cwd=vivy, mtime=KILL_TIME,
                     entrypoint="sdk-cli")
    rc = run_main(stubs, tmux_sock, tmp_path / "runs", "--dry-run",
                  "--session", "bot=vivy", "--session", "ghost=vivy")
    out = capsys.readouterr().out
    assert rc == 0
    assert "skip          session bot: no interactive transcript" in out
    assert "skip          session ghost: no interactive transcript" in out
    assert "would resume" not in out


@pytest.mark.parametrize("flag", ["v1", "bad name=vivy", "v1=bad name"])
def test_a_malformed_session_flag_is_refused_before_anything_runs(flag, tmp_path, capsys):
    with pytest.raises(SystemExit) as refused:
        CR.main(["--from", WINDOW_START_ISO, "--root", str(tmp_path),
                 "--session", flag])
    assert refused.value.code == 2
    assert "--session" in capsys.readouterr().err


def test_a_real_start_resumes_the_crew_under_its_recorded_name(stubs, tmux_sock, tmp_path):
    vivy = str(stubs.root / "vivy")
    write_transcript(stubs.projects, slug_for(vivy), "v1", cwd=vivy, mtime=KILL_TIME)
    runs = tmp_path / "runs"
    write_record(runs, "v1", "vivy")
    stubs.set_session_agent(0, "")
    assert run_main(stubs, tmux_sock, runs) == 0
    assert "vivy" in live_names(tmux_sock)
    calls = wait_for_co(stubs, 1)
    assert len(calls) == 1
    assert calls[0]["pwd"].endswith("vivy")
    assert calls[0]["args"][:2] == ["--resume", "v1"]


def test_read_names_keeps_only_values_that_are_tmux_names(tmp_path):
    runs = tmp_path / "runs"
    write_record(runs, "redacted", "[redacted: crew privacy could not be determined]")
    write_record(runs, "spaced", "bad name")
    write_record(runs, "good", "crew_1.x-2")
    assert CR.read_names([runs]) == {"good": "crew_1.x-2"}


def test_read_names_takes_the_later_day_when_a_session_has_two_records(tmp_path):
    runs = tmp_path / "runs"
    write_record(runs, "s", "old-name", day="2026-10-04")
    write_record(runs, "s", "new-name", day="2026-10-05")
    assert CR.read_names([runs]) == {"s": "new-name"}


def test_help_says_the_window_misses_sessions_that_were_idle(capsys):
    with pytest.raises(SystemExit) as done:
        CR.main(["--help"])
    assert done.value.code == 0
    assert "not every session that was live" in " ".join(capsys.readouterr().out.split())


# --- the validators refuse a trailing newline ----------------------------------
# `$` matches before a final newline, so a `.match` against `^...$` accepted one.
# Each validator must fullmatch, so every value a newline would reach is refused.

def test_a_session_flag_with_a_trailing_newline_in_its_session_id_is_refused():
    with pytest.raises(argparse.ArgumentTypeError):
        CR._session_pair("0a1b\n=vivy")


def test_a_session_flag_with_a_trailing_newline_in_its_tmux_name_is_refused():
    with pytest.raises(argparse.ArgumentTypeError):
        CR._session_pair("0a1b=vivy\n")


def test_a_since_duration_with_a_trailing_newline_is_refused():
    with pytest.raises(argparse.ArgumentTypeError):
        CR._window_start(NOW, "90m\n", None)


def test_an_agent_setting_with_a_trailing_newline_is_skipped_not_resumed(tmp_path):
    vivy = tmp_path / "vivy"
    vivy.mkdir()
    t = CR.Transcript(session_id="v1", project_dir=CR.project_slug(str(vivy)), mtime=KILL_TIME,
                      entrypoint="cli", cwd=str(vivy), title=None, agent="coder\n")
    (step,) = CR.plan([t], {"v1": "vivy"}, set(), str(tmp_path / "substrate"), False)
    assert step.argv == ()
    assert "is not an agent name" in step.skip


def test_a_session_id_with_a_trailing_newline_is_skipped_not_resumed(tmp_path):
    vivy = tmp_path / "vivy"
    vivy.mkdir()
    t = CR.Transcript(session_id="v1\n", project_dir=CR.project_slug(str(vivy)), mtime=KILL_TIME,
                      entrypoint="cli", cwd=str(vivy), title=None, agent=None)
    (step,) = CR.plan([t], {"v1\n": "vivy"}, set(), str(tmp_path / "substrate"), False)
    assert step.argv == ()
    assert "transcript name is not a session id" in step.skip
