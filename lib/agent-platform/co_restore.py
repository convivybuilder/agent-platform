#!/usr/bin/env python3
"""co restore — resume the interactive crew sessions a tmux server kill took down (CQ·392).

Every crew lives in the tmux server, so a kill takes all of them at once. The
transcripts survive. Each crew resumes by name: `tmux new-session -d -s <name> -c
<launch dir>`, then `co [agent] --resume <session id> '<prompt>'` typed into that
session, so the black-box wall and the session name come back with it.

Only interactive transcripts are resumed. A dispatched run writes a transcript
too, with entrypoint `sdk-cli`; the marker is the `entrypoint` field, which every
interactive `co` session writes as `cli` (checked 2026-10-05 against the real
transcripts: no `cli` transcript sits under a worktree, and every worktree
transcript is `sdk-cli`). Of each transcript this module reads four fields:
`entrypoint`, `cwd`, `customTitle` (on the custom-title entry) and `agentSetting`.
It never keeps or prints anything else.

The window finds sessions that wrote during it, not every session that was live:
a crew idle at the kill keeps an earlier mtime and falls outside it. The dry run
prints every candidate with the reason it was picked, so the operator can name a
missed crew with `--session SESSION_ID=TMUX_NAME`. A named session's transcript is
looked up by its id wherever its last write falls, and `--until` does not apply to it.

A crew's tmux name comes from the `tmux_session` field of its session's `co-*`
identity record (written by the SessionStart hook for a session inside a pane). A
session started before that field existed has no name here, and `--session` supplies it.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

import builder_root as BR  # noqa: E402 — the run-records root the SessionStart hook writes to
import private_crew as PC  # noqa: E402 — each private crew's own agent-runs directory
import tmux_transcript as TT  # noqa: E402 — the one charset every tmux name passes

INTERACTIVE_ENTRYPOINT = "cli"
STEWARD_SUBDIR = "substrate"
DEFAULT_STAGGER_SECONDS = 5.0
DEFAULT_PROJECTS_DIR = Path.home() / ".claude" / "projects"
RESUME_PROMPT = ("Resumed by co restore after the tmux server died. "
                 "Pick up where you left off.")

# A raw line is parsed only when it mentions one of these, so a multi-megabyte
# transcript costs one JSON parse per field-bearing line rather than per line.
_FIELD_MARKERS = ('"cwd"', '"entrypoint"', '"custom-title"', '"agentSetting"')
_SESSION_ID = re.compile(r"[A-Za-z0-9-]+")
_AGENT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_DURATION = re.compile(r"(\d+)([smhd])")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
# What tmux prints when no server is running (tmux 3.7, checked 2026-10-05 on private
# sockets): "no server running on <socket>" while a socket file is left behind. Once the
# socket file is gone, tmux prints "error connecting to <socket> (No such file or
# directory)", which a live server whose socket was deleted prints too. That case asks
# whether a tmux process still runs before it reads as no server. Any other failure,
# a permission error on the socket for example, means the live state is unknown, and
# nothing may start on unknown state.
_NO_SERVER = re.compile(r"no server running on .+")
_NO_SOCKET = re.compile(r"error connecting to .+ \(No such file or directory\)")
# ORIENT_OPS_CO_TOKEN_FILE is stripped because tmux gives a server's global environment,
# and so every pane made on it, the environment of the process that starts the server.
# A restore run with the path in its environment, which only a hand export can cause,
# would otherwise hand it to every later pane, private crews included. Each restored crew
# gets it in its own ops server's MCP config, from its own co launch.
_STRIPPED_ENV = ("TMUX", "TMUX_PANE", "ORIENT_OPS_CO_TOKEN_FILE")


class RestoreError(Exception):
    """A refusal that stops the run before any session is created."""


@dataclass(frozen=True)
class Transcript:
    session_id: str
    project_dir: str          # the slug directory under projects/
    mtime: float
    entrypoint: str | None
    cwd: str | None
    title: str | None
    agent: str | None


@dataclass(frozen=True)
class Step:
    transcript: Transcript
    name: str | None
    launch: str | None
    argv: tuple[str, ...]     # the co command; empty when skipped
    skip: str | None          # why it is not resumed; None means resume


def project_slug(path: str) -> str:
    """The directory name Claude Code gives a working directory's transcripts."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def read_transcript(path: Path, mtime: float) -> Transcript:
    fields: dict[str, str | None] = {"entrypoint": None, "cwd": None,
                                     "title": None, "agent": None}
    with path.open(errors="replace") as fh:
        for line in fh:
            if not any(marker in line for marker in _FIELD_MARKERS):
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            if fields["entrypoint"] is None:
                fields["entrypoint"] = entry.get("entrypoint")
            if fields["cwd"] is None:
                fields["cwd"] = entry.get("cwd")
            if entry.get("type") == "custom-title":
                fields["title"] = entry.get("customTitle")
            if entry.get("agentSetting"):
                fields["agent"] = entry["agentSetting"]
    return Transcript(session_id=path.stem, project_dir=path.parent.name, mtime=mtime,
                      entrypoint=fields["entrypoint"], cwd=fields["cwd"],
                      title=fields["title"], agent=fields["agent"])


def interactive_transcripts(projects_dir: Path, start: float,
                            until: float | None) -> list[Transcript]:
    """Interactive transcripts whose last write falls in [start, until], newest first."""
    found = []
    for path in projects_dir.glob("*/*.jsonl"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime < start or (until is not None and mtime > until):
            continue
        try:
            transcript = read_transcript(path, mtime)
        except OSError:
            continue
        if transcript.entrypoint == INTERACTIVE_ENTRYPOINT:
            found.append(transcript)
    return sorted(found, key=lambda t: t.mtime, reverse=True)


def explicit_transcripts(projects_dir: Path, session_ids: Sequence[str]
                         ) -> tuple[list[Transcript], list[str]]:
    """The interactive transcript of each named session, and the ids with none.

    The window does not apply: a named session is one the window missed.
    """
    found, missing = [], []
    for sid in session_ids:
        transcript = None
        for path in sorted(projects_dir.glob(f"*/{sid}.jsonl")):
            try:
                candidate = read_transcript(path, path.stat().st_mtime)
            except OSError:
                continue
            if candidate.entrypoint == INTERACTIVE_ENTRYPOINT:
                transcript = candidate
                break
        if transcript is None:
            missing.append(sid)
        else:
            found.append(transcript)
    return found, missing


def default_run_roots() -> list[Path]:
    """The shared run root and each private crew's `agent-runs`, where the hook writes records.

    An unreadable registry leaves out the private crews and says so on stderr. Their
    sessions then show as unnamed, and `--session` names them. `tmux_transcript._run_roots`
    takes the same unreadable-registry branch.
    """
    roots = [Path(BR.runs_root())]
    try:
        roots.extend(Path(home) / "agent-runs" for home in PC.private_homes())
    except Exception as exc:  # noqa: BLE001 — registry failures have no narrower type
        print(f"co restore: private crew records not read ({exc}); "
              "name those sessions with --session", file=sys.stderr)
    return roots


def read_names(run_roots: Sequence[Path]) -> dict[str, str]:
    """Session id -> tmux session name, from the `co-*` records under the run roots.

    A record carries `tmux_session` only when its session ran in a pane. A redacted
    or malformed value fails the charset and is not used. A later day bucket wins
    when one session id has more than one record.
    """
    names: dict[str, str] = {}
    for root in run_roots:
        for path in sorted(root.glob("*/co-*/meta.json")):
            try:
                meta = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(meta, dict):
                continue
            sid, name = meta.get("session_id"), meta.get("tmux_session")
            if isinstance(sid, str) and isinstance(name, str) and TT.SESSION_NAME_RE.fullmatch(name):
                names[sid] = name
    return names


def launch_dir(transcript: Transcript) -> str | None:
    """The ancestor of the cwd whose slug is the transcript's project directory.

    The slug cannot be decoded back to a path (every non-alphanumeric becomes
    `-`), so the cwd is walked upward until one of its ancestors encodes to the
    same slug. A session that cd'd into a subdirectory therefore resumes from the
    crew's root. No match returns None, and the crew is skipped.
    """
    path = transcript.cwd
    while path:
        if project_slug(path) == transcript.project_dir:
            return path
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return None


def co_command(transcript: Transcript) -> tuple[str, ...]:
    """`co`, the agent when the crew has one, then the resume arguments."""
    agent = (transcript.agent,) if transcript.agent else ()
    return ("co", *agent, "--resume", transcript.session_id, RESUME_PROMPT)


def plan(transcripts: Sequence[Transcript], names: Mapping[str, str], live: set[str],
         steward_dir: str, steward_running: bool) -> list[Step]:
    """Decide each transcript's fate from the snapshot. Newest first.

    The newest transcript claims a tmux name, and an older transcript for the same
    crew is skipped. Only one steward is resumed per run, and none while a steward
    pane is running. Live names are re-checked before each start in `restore`, so
    this snapshot only explains what will happen.
    """
    steps: list[Step] = []
    claimed: set[str] = set()
    steward_claimed = False
    for t in transcripts:
        launch = launch_dir(t)
        name = names.get(t.session_id)
        skip = None
        if t.agent is not None and not _AGENT_NAME.fullmatch(t.agent):
            skip = f"agentSetting {t.agent!r} is not an agent name"
        elif not _SESSION_ID.fullmatch(t.session_id):
            skip = "transcript name is not a session id"
        elif launch is None:
            skip = "no launch directory: the project slug names no directory above its cwd"
        elif not os.path.isdir(launch):
            skip = f"launch directory {launch} is missing"
        elif name is None:
            skip = ("no tmux session name on record for this transcript; "
                    "pass --session SESSION_ID=TMUX_NAME")
        else:
            try:
                TT.validate_session_name(name)
            except TT.JoinError as exc:
                skip = f"tmux session name {name!r} is not valid: {exc}"
        if skip is None and name in claimed:
            skip = f"a newer transcript already resumes tmux session {name}"
        if skip is None and name in live:
            skip = f"tmux session {name} is already live"
        if skip is None and os.path.realpath(launch) == steward_dir:
            if steward_running or steward_claimed:
                skip = "a steward is already running; never resume a second steward"
            else:
                steward_claimed = True
        if skip is None:
            claimed.add(name)
            steps.append(Step(t, name, launch, co_command(t), None))
        else:
            steps.append(Step(t, name, launch, (), skip))
    return steps


def tmux_command(socket: str | None) -> list[str]:
    return ["tmux", *(["-L", socket] if socket else [])]


def clean_env() -> dict[str, str]:
    """The environment for every tmux and session-agent call.

    TMUX and TMUX_PANE go, because tmux resolves its socket from TMUX ahead of
    TMUX_TMPDIR. An inherited TMUX sent a dispatched coder's kill-server to the
    live crews on 2026-09-13.
    """
    return {k: v for k, v in os.environ.items()
            if k not in _STRIPPED_ENV and not k.startswith("CO_ROLLOVER_")}


def tmux_processes() -> list[tuple[int, str]]:
    """(pid, command line) of every tmux process this user runs, servers and clients alike.

    pgrep exits 1 when none runs. Any other failure raises, so the caller refuses. On
    macOS pgrep prints pids only, so the command lines come from ps.
    """
    try:
        done = subprocess.run(["pgrep", "-a", "-u", str(os.getuid()), "-x", "tmux"],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RestoreError(f"cannot check for a tmux process: {exc}") from exc
    if done.returncode == 1:
        return []
    if done.returncode != 0:
        raise RestoreError(f"cannot check for a tmux process: {done.stderr.strip()}")
    pids = [int(line.split()[0]) for line in done.stdout.splitlines() if line.strip()]
    commands = _command_lines(pids)
    return [(pid, commands.get(pid, "(command line not read; it may have exited)"))
            for pid in pids]


def _command_lines(pids: list[int]) -> dict[int, str]:
    """The command line ps can still read for each pid. A failed ps yields none."""
    if not pids:
        return {}
    try:
        done = subprocess.run(["ps", "-o", "pid=,args=", "-p", ",".join(map(str, pids))],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return {}
    commands = {}
    for line in done.stdout.splitlines():
        pid, _, args = line.strip().partition(" ")
        if pid.isdigit():
            commands[int(pid)] = args
    return commands


def list_panes(tmux: list[str], env: Mapping[str, str]) -> list[tuple[str, str]]:
    """(session name, pane current path) for every pane on the server.

    No server is an empty list. A missing socket file is no server only when no tmux
    process runs: a live server whose socket was deleted cannot be listed, and a start
    then makes a second server at the same path. Any other failure raises, because a
    start decided on unknown live state could duplicate a crew.
    """
    done = subprocess.run(
        [*tmux, "list-panes", "-a", "-F", "#{session_name}\t#{pane_current_path}"],
        capture_output=True, text=True, env=dict(env), timeout=10)
    if done.returncode != 0:
        stderr = done.stderr.strip()
        if _NO_SERVER.fullmatch(stderr):
            return []
        if _NO_SOCKET.fullmatch(stderr):
            processes = tmux_processes()
            if not processes:
                return []
            listed = "\n".join(f"  pid {pid}: {command}" for pid, command in processes)
            raise RestoreError(
                f"cannot read tmux state: {stderr}. tmux processes of this user are running "
                f"while that socket file is missing:\n{listed}\n"
                "Restore will not run while any tmux process of this user is alive, because it "
                "cannot tell from here whether one is a default server whose socket file was "
                "removed. If one of these is the default server and its socket was removed, "
                "`kill -USR1 <its pid>` recreates the socket; otherwise end the processes you do "
                "not need. Nothing was started.")
        raise RestoreError(f"cannot read tmux state: {stderr}")
    rows = []
    for line in done.stdout.splitlines():
        name, _, path = line.partition("\t")
        rows.append((name, path))
    return rows


def ensure_server(env: Mapping[str, str], session_agent: str = "session-agent") -> None:
    """Start the default tmux server through the GUI-session keeper, or refuse.

    Runs before any session is created. A server born over SSH cannot read the
    login keychain, and every crew under it gets gh 401s. The refusal message is
    session-agent's own stderr, passed through unchanged.
    """
    try:
        done = subprocess.run([session_agent, "ensure-server"], capture_output=True,
                              text=True, env=dict(env), timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RestoreError(f"cannot run session-agent ensure-server: {exc}") from exc
    if done.returncode != 0:
        raise RestoreError(done.stderr.strip()
                           or "session-agent ensure-server could not start the tmux server")


def _provenance(transcript: Transcript, name: str | None, explicit: Mapping[str, str]) -> str:
    """Why this transcript is a candidate, and where its tmux name came from."""
    sid = transcript.session_id
    written = dt.datetime.fromtimestamp(transcript.mtime).isoformat(timespec="minutes")
    picked = "picked by --session" if sid in explicit else "picked by the window"
    named = "" if name is None else (
        "; name from --session" if sid in explicit else "; name from co-* record")
    return f"{picked}, last written {written}{named}"


def restore(*, root: str, projects_dir: Path, start: float, until: float | None,
            dry_run: bool, stagger: float, names: Mapping[str, str],
            explicit: Mapping[str, str] | None = None,
            tmux_socket: str | None = None, session_agent: str = "session-agent",
            sleep: Callable[[float], None] = time.sleep) -> int:
    """Plan the restore, print it, and start the crews unless dry_run. Returns the exit code.

    `explicit` holds the `--session` pairs. Each one also adds its session's transcript
    to the candidates when the window missed it, and it overrides the name in `names`.
    """
    explicit = explicit or {}
    env = clean_env()
    tmux = tmux_command(tmux_socket)
    steward_dir = os.path.realpath(os.path.join(root, STEWARD_SUBDIR))
    try:
        panes = list_panes(tmux, env)
    except RestoreError as exc:
        print(f"co restore: {exc}", file=sys.stderr)
        return 1
    live = {name for name, _ in panes}
    steward_running = any(os.path.realpath(path) == steward_dir for _, path in panes if path)
    windowed = interactive_transcripts(projects_dir, start, until)
    named_found, missing = explicit_transcripts(projects_dir, sorted(explicit))
    in_window = {t.session_id for t in windowed}
    transcripts = sorted(windowed + [t for t in named_found if t.session_id not in in_window],
                         key=lambda t: t.mtime, reverse=True)
    for sid in missing:
        print(f"skip          session {sid}: no interactive transcript under {projects_dir}")
    steps = plan(transcripts, {**names, **explicit}, live, steward_dir, steward_running)

    verb = "would resume" if dry_run else "resume"
    for step in steps:
        sid = step.transcript.session_id
        why = _provenance(step.transcript, step.name, explicit)
        if step.skip is None:
            print(f"{verb:<13} {step.name}  {step.launch}  session {sid}  [{why}]")
        else:
            label = step.name or f"session {sid}"
            print(f"skip          {label}: {step.skip}  [{why}]")
    if dry_run:
        print("dry run: nothing started")
        return 0
    resumes = [step for step in steps if step.skip is None]
    if not resumes:
        return 0
    try:
        ensure_server(env, session_agent)
    except RestoreError as exc:
        print(f"co restore: {exc}", file=sys.stderr)
        return 1

    failures = 0
    for index, step in enumerate(resumes):
        if index:
            sleep(stagger)
        # Checked again here, not once above: a crew can be started by hand in the
        # gap, and two cos on one handoff is the failure this guards against.
        try:
            panes = list_panes(tmux, env)
        except RestoreError as exc:
            print(f"co restore: {exc}; stopping before the next start", file=sys.stderr)
            return 1
        if step.name in {name for name, _ in panes}:
            print(f"skip          {step.name}: tmux session {step.name} went live before its start")
            continue
        steward_now = any(os.path.realpath(path) == steward_dir for _, path in panes if path)
        if steward_now and os.path.realpath(step.launch) == steward_dir:
            print(f"skip          {step.name}: a steward went live before its start")
            continue
        created = subprocess.run(
            [*tmux, "new-session", "-d", "-s", step.name, "-c", step.launch],
            capture_output=True, text=True, env=dict(env), timeout=30)
        if created.returncode != 0:
            failures += 1
            print(f"fail          {step.name}: new-session: {created.stderr.strip()}")
            continue
        # `-l` sends the command as literal text, so a word in it is never read as a key name.
        typed = [*tmux, "send-keys", "-t", f"={step.name}:", "-l", shlex.join(step.argv)]
        sent = subprocess.run(typed, capture_output=True, text=True, env=dict(env), timeout=30)
        if sent.returncode == 0:
            sent = subprocess.run([*tmux, "send-keys", "-t", f"={step.name}:", "Enter"],
                                  capture_output=True, text=True, env=dict(env), timeout=30)
        if sent.returncode != 0:
            failures += 1
            print(f"fail          {step.name}: created, but the resume command was not sent: "
                  f"{sent.stderr.strip()}")
            continue
        print(f"sent          {step.name}  {step.launch}  session {step.transcript.session_id}")
    return 1 if failures else 0


def _window_start(now: float, since: str | None, start: str | None) -> float:
    if since is not None:
        found = _DURATION.fullmatch(since)
        if not found:
            raise argparse.ArgumentTypeError(f"--since takes a duration like 90m, 2h or 1d, not {since!r}")
        return now - int(found.group(1)) * _UNIT_SECONDS[found.group(2)]
    return _parse_time(start)


def _parse_time(text: str) -> float:
    try:
        # A naive time reads as local time, which is the time the operator quotes.
        return dt.datetime.fromisoformat(text).timestamp()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an ISO 8601 time: {text!r} ({exc})") from exc


def _session_pair(text: str) -> tuple[str, str]:
    sid, sep, name = text.partition("=")
    if not sep or not _SESSION_ID.fullmatch(sid):
        raise argparse.ArgumentTypeError(f"--session takes SESSION_ID=TMUX_NAME, not {text!r}")
    try:
        TT.validate_session_name(name)
    except TT.JoinError as exc:
        raise argparse.ArgumentTypeError(f"--session {text!r}: {exc}") from exc
    return sid, name


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="co restore",
        description="Resume every interactive crew a tmux server kill took down. The window "
                    "finds sessions that wrote during it, not every session that was live, so a "
                    "crew idle at the kill is missed. The dry run lists every candidate and why it "
                    "was picked; name a missed crew with --session SESSION_ID=TMUX_NAME.")
    window = parser.add_mutually_exclusive_group(required=True)
    window.add_argument("--since", metavar="DURATION",
                        help="transcripts last written within this long ago, e.g. 90m, 2h, 1d")
    window.add_argument("--from", dest="start", metavar="TIME", type=str,
                        help="transcripts last written at or after this ISO 8601 time")
    parser.add_argument("--until", metavar="TIME", type=_parse_time,
                        help="transcripts last written at or before this ISO 8601 time; "
                             "pass the kill time so sessions closed afterwards are left out")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan and start nothing")
    parser.add_argument("--stagger", metavar="SECONDS", type=float,
                        default=DEFAULT_STAGGER_SECONDS,
                        help="delay between starts; twelve at once timed out the kb and ops servers")
    parser.add_argument("--root", required=True,
                        help="the machine's root; the steward's directory is <root>/substrate")
    parser.add_argument("--session", metavar="SESSION_ID=TMUX_NAME", action="append",
                        default=[], type=_session_pair,
                        help="the tmux name for one session, repeatable; it overrides the session's "
                             "co-* record and adds the session when the window missed it")
    # Test seams. Production uses the default transcript directory, run roots and tmux socket.
    parser.add_argument("--projects-dir", type=Path, default=DEFAULT_PROJECTS_DIR,
                        help=argparse.SUPPRESS)
    parser.add_argument("--runs-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--tmux-socket", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    now = time.time()
    try:
        start = _window_start(now, args.since, args.start)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
    if args.until is not None and args.until < start:
        parser.error("--until is before the start of the window")
    run_roots = [args.runs_root] if args.runs_root else default_run_roots()
    explicit = dict(args.session)
    return restore(root=args.root, projects_dir=args.projects_dir, start=start,
                   until=args.until, dry_run=args.dry_run, stagger=args.stagger,
                   names=read_names(run_roots), explicit=explicit,
                   tmux_socket=args.tmux_socket)


if __name__ == "__main__":
    raise SystemExit(main())
