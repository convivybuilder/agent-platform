"""co_rollover — the successor takeover mechanics for ADR-0069 Slice 5.

`bin/co-rollover` is the thin CLI; this module holds the parts worth testing
without shelling out to a subprocess for every case: building the tmux
commands, parsing their output, and the side-channel file that lets the
successor find the predecessor it is closing out.

THE HANDOFF DIRECTORY IS THE ONE SHARED STATE THE TWO SIDES OF A ROLLOVER
BOTH KNOW. The predecessor's `co-rollover` invocation and the successor's
`co-rollover --picked-up` run in two different Claude Code sessions, so
neither can read the other's environment or process tree — the only thing
the successor is handed is the handoff PATH itself (via the
`/handoff-pickup <path>` prompt `spawn_successor` builds). So the side-channel
file this module writes alongside `handoff.md` (`co-rollover-state.json`,
`read_side_channel`/`write_side_channel` below) is what carries the
predecessor's session id and pane id, the tmux session name, and the
successor's own pane id and window index across that boundary.

THE ROLLOVER-POLICY STATE FILE IS A SEPARATE THING FROM THE SIDE CHANNEL, on
purpose. `rollover_policy.mark_rolling` writes `~/.cache/co-session-health/
rollover-<predecessor-session-id>.json` — the file `tools/rollover-trigger.py`'s
PreToolUse fence and Deck's rail both read, keyed by session id the way every
other rollover file already is. The side channel is keyed by the handoff
PATH instead, because that is the only key the successor has before it knows
anything else. Both are written by the predecessor's invocation; only the
side channel is read back by the successor's.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import rollover_policy as RP
import tmux_transcript as TT

# `session_state` and `session_input` (`forward_late_messages`'s only users,
# below) are imported LAZILY, inside that function, rather than at module
# scope here — the same reason `session_input._current_state` imports
# `session_state` inside its own function body rather than at its top:
# `tmux_transcript` (imported above) inserts the DEPLOYED
# `~/.local/lib/agent-platform` at the FRONT of `sys.path` as a side effect
# of ITS OWN `telemetry`/`private_crew` imports, so any module-scope import
# of `session_state` or `session_input` placed after this one, in a module
# a test harness loads early enough that neither is cached yet, resolves
# from that deployed farm instead of this checkout — exactly what
# conftest.py's `_repin_from_the_farm` exists to catch, and it cannot save
# an import this module already completed with the wrong module. Deferring
# both to call time means they resolve however `sys.modules` already
# settled by then, which is always correct once module loading is done.

PANE_ID_RE = re.compile(r"%\d+")
SIDE_CHANNEL_NAME = "co-rollover-state.json"

# The same banner text the `/rollover` skill has always polled for — `co -r`
# takes ~10s (git pull + node startup), and `claude --remote-control` prints
# this once it is up and registered. Checked before FAILED, so a pane that
# happens to print both (unlikely, but not impossible if a lagging knowledge
# pull warning scrolls past before the banner) is read as up.
UP_RE = re.compile(r"remote-control is active|◷ ctx|ctx [0-9]+K", re.IGNORECASE)
# The skill's own list of causes a pane shows when the spawn does not come
# up: the knowledge pull's auth preflight (ARCH-163), a stale lock, or the
# window falling back to a bare shell with the command not found. "pane is
# dead" is tmux's own banner (`capture-pane` prints it verbatim in place of
# the prompt once `remain-on-exit` — set by `spawn_successor` below — keeps a
# pane whose process has already exited from disappearing with the window):
# it carries the exit status, so a `zsh -lc "..."` that fails before writing
# anything (its own interpreter missing, the extreme case) is diagnosable
# from this text instead of an empty capture.
# `aborting` is not matched alone. knowledge-ctx's live-claude refusal ends
# "— aborting.", and bin/co falls back to the predecessor's profile and carries
# on (bin/co `_co_reindividuate`, exit-2 branch), so matching it reported a
# successor that came up as failed. Each fatal text that says "aborting" is
# followed by a text listed below. "never individuated" is the fatal branch of
# the same refusal: it exits 1 without printing any other listed text.
FAILED_RE = re.compile(
    r"pull failed|auth preflight|knowledge-ctx use failed|command not found"
    r"|never individuated|pane is dead",
    re.IGNORECASE)


class CoRolloverError(Exception):
    """Something `bin/co-rollover` cannot recover from on its own."""


def _run(argv: list[str], timeout: float = 10.0) -> str | None:
    """Command stdout, or None on any failure — never raises. Unlike
    `tmux_transcript.run`, output is NOT stripped: `verify_up` matches
    multi-line pane text where leading/trailing whitespace on inner lines is
    part of what a human reads, and stripping only the ends would give an
    inconsistent view of the same buffer.
    """
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def current_session_and_pane() -> tuple[str, str]:
    """The tmux session name and this invocation's own pane id.

    The pane id is `$TMUX_PANE` itself — tmux already sets it in every pane's
    environment, so no tmux call is needed for it. The session name takes one
    call, targeted at that exact pane so it can never answer for a different
    session sharing a pane-numbering coincidence.
    """
    pane_id = os.environ.get("TMUX_PANE")
    if not pane_id:
        raise CoRolloverError("not running inside a tmux pane (TMUX_PANE is unset)")
    out = _run(["tmux", "display-message", "-p", "-t", pane_id, "#{session_name}"])
    if not out or not out.strip():
        raise CoRolloverError(f"tmux could not resolve the session for pane {pane_id!r}")
    return out.strip(), pane_id


def pane_identity(pane_id: str) -> dict | None:
    """`{"pane_pid": int, "tmux_session_id": str}` for *pane_id*, or None if
    tmux cannot answer for it (the pane is gone, or the server is gone).

    Both fields survive a RENAME (of the session, or of nothing — panes have
    no name) but NEITHER survives a tmux SERVER RESTART: `pane_pid` is the OS
    process id of the pane's own shell, and `#{session_id}` (`$0`, `$1`, ...)
    is the server's own internal counter for the session slot, reassigned
    from zero exactly like pane ids are on every fresh server start. That is
    the point — `close_out` compares THIS reading, taken fresh right before
    it acts, against what `mark_rolling` recorded when the pane was created,
    so a `pane_id` that a restart has silently handed to an unrelated live
    pane is caught rather than trusted.
    """
    out = _run(["tmux", "display-message", "-p", "-t", pane_id, "#{pane_pid} #{session_id}"])
    if not out or not out.strip():
        return None
    parts = out.strip().split()
    if len(parts) != 2 or not parts[0].isdigit():
        return None
    return {"pane_pid": int(parts[0]), "tmux_session_id": parts[1]}


def pane_window_index(pane_id: str) -> int | None:
    """The tmux window index containing *pane_id*, or None if tmux cannot
    resolve it. Best-effort — for a parked session's issue body, "which
    window is this" (Slice 6); nothing else in this module needs it, since
    every other caller here already has its own window index from
    `spawn_successor`'s own report rather than looking one up.
    """
    out = _run(["tmux", "display-message", "-p", "-t", pane_id, "#{window_index}"])
    if not out or not out.strip().isdigit():
        return None
    return int(out.strip())


def predecessor_claude_pids(pane_pid: int) -> list[int]:
    """Every `claude` process id belonging to the predecessor's pane, given
    the pane pid `pane_identity` already recorded for it (`mark_rolling`'s
    `predecessor["pane_pid"]`).

    These are the exact pids `knowledge-ctx`'s live-process guard
    (`live_claude_processes_in`) would itself report for that pane — found by
    walking the same process tree Deck's own liveness check walks
    (`tmux_transcript.claude_pids_for_pane`), rather than by a separate
    process-matching rule that could disagree with the guard about which pids
    are "the" claude in that pane. The two checks are independent
    implementations of "is this comm a claude process"
    (`tmux_transcript._is_claude`'s exact-basename match vs.
    `knowledge-ctx.live_claude_processes_in`'s looser substring match) that
    happen to agree on every shape observed on this machine; nothing ties
    them together, so a future comm string could make them disagree.

    Returns every pid found, not just one — `tmux_transcript.claude_pids_
    under`'s own module docstring records several claude processes
    legitimately sharing one pane as an observed shape (the 2026-07-22
    computer-use-fork case), and exempting only the first would leave a
    second, equally-legitimate pid tripping the guard as a false collision.

    Empty when no claude process can be found beneath this pane (the pane's
    shell has already exited, or the tree is deeper than
    `claude_pids_under`'s bounded walk) — `spawn_successor`'s caller passes
    that straight through as `exempt_pids=[]`, which keeps the successor's
    forced re-individuate skipped rather than guessing at a pid to exempt.
    """
    return TT.claude_pids_for_pane(pane_pid)


def spawn_successor(session_name: str, cwd: str, product: str, handoff_path: str, *,
                     exempt_pids: list[int] | None = None,
                     permission_mode: str | None = None) -> dict:
    """Create the successor's window, detached (not yet selected — the caller
    verifies it came up before switching to it). Returns
    `{"window_index": int, "pane_id": str}`, read directly off tmux's own
    `-P -F` report of the window it just created, rather than a second lookup
    by name that could resolve to some other window (the same exact-target
    discipline `tmux_transcript._resolve_session_id` documents).

    `co -r "/handoff-pickup <path>"` is passed as argv, not a shell string —
    tmux execs a multi-word `new-window` command directly rather than handing
    it to a shell, so the exact path reaches `co` with no quoting boundary to
    get wrong. The `|| exec zsh` fallback the skill relies on (so a failed
    spawn leaves a diagnosable pane rather than a closed window) needs an
    actual shell, hence wrapping in `zsh -lc`.

    `CO_ROLLOVER_SUCCESSOR=1` marks this launch for `bin/co`. The predecessor's
    pane is still live in `cwd` when this window comes up — it is not this
    process's ancestor, so `knowledge-ctx`'s live-process guard cannot exempt
    it on its own, and would abort the successor's `-r` the moment the
    freshly-pulled knowledge repo differs at all from what the predecessor
    last individuated.

    `exempt_pids`, when the caller could resolve any (`predecessor_claude_pids`
    above), are the predecessor's own `claude` pids — the caller adds a SECOND
    prefix assignment, `CO_ROLLOVER_EXEMPT_PID=<pid>[,<pid>...]`, so `bin/co`
    passes one `--exempt-pid` per pid straight through to `knowledge-ctx use
    --force` instead of skipping the re-individuate outright
    (issue_01M2FDBJS8GMA16DX4XNBVH2K6).

    `permission_mode`, when the caller has one (`bin/co-rollover`'s own read of
    `rollover_policy.read_state(predecessor_session_id)["permission_mode"]`),
    is a THIRD prefix assignment, `CO_ROLLOVER_PERMISSION_MODE=<mode>` — the operator's
    ruling, 2026-09-16: "let's just leave the mode the same, it's not like
    it's worse after the rollover." `bin/co` is the one place that validates
    it against Claude Code's known modes and decides what to pass its own
    `claude --permission-mode`; this function only forwards whatever string it
    was given, or omits the assignment entirely when none was given (no state
    on record, or the predecessor's mode was never reported at all — the same
    "missing" case `bin/co` falls back to `bypassPermissions` for).

    THE FENCE DOES NOT PROTECT THIS REWRITE — an earlier version of this
    docstring said it did, backwards. `mark_rolling` (the call that arms
    `tools/rollover-trigger.py`'s PreToolUse fence) is the LAST thing
    `bin/co-rollover`'s `_cmd_spawn` does: it runs after `verify_up`,
    `select_window`, and `clear_remain_on_exit`. This window — and the
    `_co_reindividuate` call inside it that rewrites `.claude/` — starts
    running the moment `tmux new-window` returns, which is well before the
    fence exists. What actually protects the predecessor on the SUCCESS path
    is that its own Claude session is synchronously blocked inside the Bash
    tool call running `co-rollover`, which does not return until `verify_up`
    does (up to 30s) — so it has no opportunity to act on `.claude/` at all,
    regardless of whether the fence is armed yet.

    THAT PROTECTION DOES NOT COVER A FAILED SPAWN. If `verify_up` times out
    or matches a known failure signature, `_cmd_spawn` returns non-zero
    WITHOUT ever calling `mark_rolling` — the predecessor's Bash call returns
    to its own model, unfenced and live, at exactly the moment its
    `.claude/agents/` and `.claude/skills/` may already be mid-rewrite (or
    freshly wiped and not yet rewritten) by this successor's `--force`
    individuate, which `rmtree`s both directories before rewriting them file
    by file, not atomically. Nothing here kills or waits on the successor's
    window to close that gap: the torn-write window (a `verify_up` timeout
    landing while individuate is mid-rewrite, followed immediately by a
    predecessor tool call that reads an agent or skill file) has not been
    observed, and platform/decisions/0068 requires an observed occurrence
    before a guard goes in — so this is a known, unguarded limitation, not a
    defect fixed by this docstring.

    `exempt_pids=[]` (no pid could be resolved) keeps today's skip: the
    predecessor already handed off a working profile, so skipping costs
    nothing and removes the race rather than guessing at a pid.

    THE TRUST ASSUMPTION THIS RESTS ON: only this function ever sets
    `CO_ROLLOVER_SUCCESSOR` together with `CO_ROLLOVER_EXEMPT_PID` or
    `CO_ROLLOVER_PERMISSION_MODE` on a `co` invocation. `bin/co` accepts the
    pid list from the bare environment with only a comma-separated all-digits
    format check, and the permission mode with only a check against Claude
    Code's known modes — nothing ties either accepted value to any recorded
    rollover state, so that pairing is the only thing keeping either scoped
    to an actual rollover.

    `bin/co` reads all three markers into private variables and unsets all
    three environment variables at the very top of the file, before any
    launch branch can `exec claude` — not inside `_co_reindividuate` (round
    3, PR #574: most launch branches never call that helper at all, so an
    unset living only inside it left the marker to ride into the successor's
    whole `claude` process tree on every branch that doesn't). `_co_reindividuate`
    reads the already-captured private variables; it no longer touches any of
    the three environment variables itself.

    All three markers are prefix assignments on the `co` command inside
    `shell_cmd`, NOT a tmux `-e` on the window (round 2, PR #574): `-e` sets
    them in the environment of the wrapper shell (`zsh -lc '... || exec
    zsh'`) itself, so a failed launch's `|| exec zsh` fallback would inherit
    them too, and a by-hand `co -r` retry from that fallback shell would
    still carry a stale marker, exemption or permission mode with no
    predecessor left to race at all. Scoping them to the `co` command keeps
    the fallback shell's own environment clean.
    """
    prompt = f"/handoff-pickup {handoff_path}"
    prefix = "CO_ROLLOVER_SUCCESSOR=1"
    if exempt_pids:
        prefix += f" CO_ROLLOVER_EXEMPT_PID={','.join(str(p) for p in exempt_pids)}"
    if permission_mode:
        prefix += f" CO_ROLLOVER_PERMISSION_MODE={shlex.quote(permission_mode)}"
    shell_cmd = f"{prefix} co -r {shlex.quote(prompt)} || exec zsh"
    argv = [
        "tmux", "new-window", "-d", "-a", "-t", f"{session_name}:",
        "-n", product, "-c", cwd,
        "-e", f"CLAUDE_REMOTE_CONTROL_SESSION_NAME_PREFIX={product}-rollover",
        "-P", "-F", "#{window_index} #{pane_id}",
        "zsh", "-lc", shell_cmd,
    ]
    out = _run(argv)
    if not out or not out.strip():
        raise CoRolloverError("tmux new-window failed to create the successor's window")
    parts = out.strip().split()
    if len(parts) != 2 or not parts[0].isdigit() or not PANE_ID_RE.fullmatch(parts[1]):
        raise CoRolloverError(f"unexpected tmux new-window output: {out!r}")
    window_index = int(parts[0])
    # Best-effort, and deliberately AFTER window creation rather than baked
    # into `argv` above (tmux has no such flag on `new-window` itself): with
    # this unset, a pane whose process exits — including the wrapper
    # interpreter itself failing to start — takes the whole window down with
    # it within milliseconds, so `verify_up` below finds nothing to capture
    # at all (proven locally: a `new-window` execing a nonexistent binary
    # left `capture-pane` unable to even find the window 50ms later). Setting
    # it here can still lose that same race on an instant failure — which is
    # exactly the gap the real-tmux test suite skips explicitly on a host
    # with no zsh — but every failure slow enough to be worth diagnosing
    # (a hung `git pull`, the knowledge auth preflight) leaves it plenty of
    # time to land before the pane can die.
    _run(["tmux", "set-window-option", "-t", f"{session_name}:{window_index}",
          "remain-on-exit", "on"])
    return {"window_index": window_index, "pane_id": parts[1]}


def verify_up(session_name: str, window_index: int, *, timeout: float = 30.0,
              poll_interval: float = 2.0, sleep=time.sleep, clock=time.monotonic) -> tuple[bool, str]:
    """Poll the successor's window until its pane shows the remote-control
    banner (up), a known failure signature (failed), or `timeout` elapses
    (also failed). Returns `(ok, pane_text)` — `pane_text` is always the last
    capture, for the caller to report on a failure.

    Do NOT check `pane_current_command`: the skill's own history records that
    it races (still reading `zsh` for several seconds into a good launch,
    2026-06-24 live test) — the banner in the pane's rendered content is the
    reliable signal.
    """
    target = f"{session_name}:{window_index}"
    last = ""
    deadline = clock() + timeout
    while True:
        out = _run(["tmux", "capture-pane", "-t", target, "-p"])
        last = out if out is not None else last
        if UP_RE.search(last):
            return True, last
        if FAILED_RE.search(last):
            return False, last
        if clock() >= deadline:
            return False, last
        sleep(poll_interval)


def select_window(session_name: str, window_index: int) -> None:
    _run(["tmux", "select-window", "-t", f"{session_name}:{window_index}"])


def clear_remain_on_exit(session_name: str, window_index: int) -> bool:
    """Turn `remain-on-exit` back OFF on the successor's window, once the
    caller has confirmed (via `verify_up`) that the spawn succeeded.

    `spawn_successor` turns the option on so an instant exec failure stays
    diagnosable (its own comment has the race this guards). Left on past a
    successful spawn, it has no reset: the option is window-scoped and
    outlives it, so the first time this window's session ends other than
    through another rollover (which kills the pane explicitly via
    `close_out`, unaffected by `remain-on-exit`), the exited shell leaves a
    dead pane sitting in the window forever instead of tmux closing it —
    a permanent extra leaf on Deck's rail with no cleanup short of a manual
    `tmux kill-window` (round 4, agent-platform#566).

    Returns whether the call succeeded. The caller does not fail the
    rollover on `False` — by the time this runs the successor is already up
    and selected, so this is cleanup, not a precondition.
    """
    return _run(["tmux", "set-window-option", "-t", f"{session_name}:{window_index}",
                 "remain-on-exit", "off"]) is not None


def kill_pane(pane_id: str) -> None:
    _run(["tmux", "kill-pane", "-t", pane_id])


def renumber_windows(session_name: str) -> None:
    """`move-window -r`: renumber every window in the session so it runs
    1..n with no gaps. Deck's rail assumes exactly that
    (`WindowLeaf.numbers` is `2...count`), which is what lets the successor
    become window 1 once the predecessor's window is gone."""
    _run(["tmux", "move-window", "-r", "-t", session_name])


CLOSE_OUT_LOG_NAME = "co-rollover-close-out.log"


def _log_close_out_skip(log_dir, message: str) -> None:
    """Append one line, best-effort — this runs inside the detached
    close-out subprocess (stdout/stderr both `DEVNULL`), so a log line here
    is the only way the skip ever reaches anywhere an operator can find it."""
    path = Path(log_dir) / CLOSE_OUT_LOG_NAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as fh:
            fh.write(message)
    except OSError:
        pass


def close_out(session_name: str, predecessor_pane_id: str, *,
              expected_pane_pid=None, expected_tmux_session_id=None, log_dir=None) -> None:
    """Kill the predecessor's pane, then renumber so the successor becomes
    window 1. Order matters: renumbering before the kill would count the
    predecessor's own window in the new numbering.

    When both *expected_pane_pid* and *expected_tmux_session_id* are given
    (the normal path — `bin/co-rollover` always supplies both, recorded by
    `rollover_policy.mark_rolling` at spawn time), the pane's identity is
    verified fresh, right here, before anything is killed: a tmux server
    restart reassigns pane ids and session ids from zero, so `pane_id` alone
    can by then name an unrelated live pane (`pane_identity`'s docstring has
    the incident this guards). On a mismatch — or the pane no longer
    resolving at all — nothing is killed and nothing is renumbered; the
    mismatch is logged (`CLOSE_OUT_LOG_NAME`, under *log_dir* or
    `rollover_policy.CACHE_DIR`) so an operator can find out why a rollover
    never closed out.
    """
    if expected_pane_pid is not None and expected_tmux_session_id is not None:
        identity = pane_identity(predecessor_pane_id)
        if identity is None or str(identity["pane_pid"]) != str(expected_pane_pid) \
                or identity["tmux_session_id"] != expected_tmux_session_id:
            _log_close_out_skip(
                log_dir if log_dir is not None else RP.CACHE_DIR,
                f"{time.time():.0f} skip close_out: session={session_name!r} "
                f"pane={predecessor_pane_id!r} expected "
                f"pane_pid={expected_pane_pid!r} tmux_session_id={expected_tmux_session_id!r} "
                f"actual={identity!r} — pane identity mismatch (likely a tmux server "
                "restart); killing nothing\n")
            return
    kill_pane(predecessor_pane_id)
    renumber_windows(session_name)


# --- the side channel (handoff-directory-keyed, crosses the two sessions) ---


def _side_channel_path(handoff_path) -> Path:
    return Path(handoff_path).parent / SIDE_CHANNEL_NAME


def write_side_channel(handoff_path, data: dict) -> None:
    """Write the side-channel file atomically, beside `handoff.md`."""
    path = _side_channel_path(handoff_path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def read_side_channel(handoff_path) -> dict | None:
    """The side-channel file's contents, or None if it is missing or
    unreadable — the successor's `--picked-up` refuses loudly on None rather
    than guessing at a predecessor to close out."""
    try:
        return json.loads(_side_channel_path(handoff_path).read_text())
    except (OSError, ValueError):
        return None


def mark_picked_up(handoff_path, successor_session_id: str, *, now: float | None = None) -> dict:
    """Update the side channel to record pickup, and fill in the successor's
    session id on the (already `rolling`) `rollover_policy` state too —
    `record_successor_session_id` is a no-op if the predecessor's state is not
    `rolling`, which cannot happen on the intended path but must never be
    allowed to manufacture one.

    Returns the updated side-channel dict, plus one call-specific fact never
    persisted to disk: `picked_up_this_call` — whether THIS call was the one
    that flipped `picked_up`. `bin/co-rollover` uses it to skip starting a
    second, redundant close-out subprocess on a retried `--picked-up` (the
    successor re-running it, or a duplicate dispatch); idempotent regardless,
    since `picked_up_at` stays at its first value rather than resetting the
    clock either way.

    CALLED AFTER FORWARDING, NOT BEFORE (round 2, PR #588). `bin/co-rollover`
    reads `picked_up` itself, straight off `read_side_channel`, to decide
    whether to call `forward_late_messages` — and only calls this function,
    which is what actually flips the flag, once that forwarding pass has
    finished. Flipping the flag first (the original order) meant a crash
    between the flip and the forwarding loop left `picked_up` permanently
    true with the queue never read: a held message gone for good, with
    nothing to retry, because every later call sees `picked_up` already set
    and skips forwarding too. Flipping it last means the same crash instead
    leaves `picked_up` still false, so a retry re-runs forwarding against the
    queue as it currently stands — which can re-send a message an earlier,
    interrupted call already delivered. That accepted trade (a possible
    duplicate instead of a possible silent loss) is the reason this function
    no longer decides whether to forward; it only ever records that pickup
    happened.
    """
    data = read_side_channel(handoff_path)
    if data is None:
        raise CoRolloverError(f"no co-rollover state found beside {handoff_path!r}")
    picked_up_this_call = not data.get("picked_up")
    if picked_up_this_call:
        data = {**data, "picked_up": True, "picked_up_at": now if now is not None else time.time()}
        successor = dict(data.get("successor") or {})
        successor["session_id"] = successor_session_id
        data["successor"] = successor
        write_side_channel(handoff_path, data)
    predecessor = data.get("predecessor") or {}
    if predecessor.get("session_id"):
        RP.record_successor_session_id(predecessor["session_id"], successor_session_id)
    return {**data, "picked_up_this_call": picked_up_this_call}


# --- forwarding a late-arriving message to the successor (Slice 7) ---------
#
# The operator can still type into the predecessor's pane after it is
# fenced — the PreToolUse fence denies TOOL calls, not terminal input — and
# Claude Code holds whatever they send in its own queue exactly as it would
# for any busy session. `bin/co-rollover --picked-up` is the only point
# that knows both halves of "did this arrive after the handoff": the
# handoff's own write time, and the predecessor's queue as it stands right
# now. This runs there, synchronously, NOT inside the detached close-out
# below — the close-out's stdout/stderr are `DEVNULL` (see `_cmd_picked_up`
# in `bin/co-rollover`), so a failure logged only there would never reach
# anything an operator or the successor's own turn can see.


def log_forwarding_skipped(missing: "list[str]", *, log_dir=None) -> None:
    """Record that `forward_late_messages` was never called at all, because
    one or more of the identifiers it requires were missing from the side
    channel (`bin/co-rollover`'s `_cmd_picked_up`, round 2, PR #588).
    Logged to the same `CLOSE_OUT_LOG_NAME` a forwarding failure is, so this
    skip is on record even if the caller's own stderr goes unread — without
    it, a first pickup with nothing to forward and a first pickup that
    silently skipped forwarding print identically."""
    _log_close_out_skip(
        log_dir if log_dir is not None else RP.CACHE_DIR,
        f"{time.time():.0f} skip forward_late_messages: missing {', '.join(missing)} on "
        "record\n")


def _parse_queue_ts(ts) -> float | None:
    """A `queue-operation` record's own `timestamp` field, as epoch seconds,
    or None for anything that is not a parseable ISO 8601 string — the same
    "cannot tell" answer `session_state`'s own timestamp reads give a
    record from an unrecognised shape, so an unparseable timestamp is
    skipped by `forward_late_messages` rather than guessed at."""
    if not isinstance(ts, str):
        return None
    try:
        when = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.timestamp()


def forward_late_messages(handoff_path, predecessor_session_id: str, successor_pane_id: str,
                          tmux_session: str, *, log_dir=None) -> "tuple[int, list[str]]":
    """Forward every message that entered the predecessor's held queue
    AFTER `handoff.md` was written, to the successor's own pane (epic
    issue_01M1S2X8ENKYEGSDE3H0REFD3G, Slice 7). Returns `(sent, failures)`
    — `failures` is one human-readable string per message that could not
    be forwarded, for the caller to print to stderr; every failure is
    logged here too (`log_dir`, or `rollover_policy.CACHE_DIR` — the same
    `CLOSE_OUT_LOG_NAME` `close_out` itself writes to), so it is on record
    even if the caller's own stderr goes unread.

    The operator typed each of these expecting the session sitting in that
    pane to read it; by the time `--picked-up` runs, that is the successor,
    not the fenced predecessor. `handoff.md`'s own mtime is the cutoff — the
    moment `/handoff-leave` finished writing it — because anything held
    before that is already represented in the handoff text the successor
    just read, and forwarding it again would duplicate it. A message whose
    own `timestamp` cannot be parsed is skipped rather than guessed at: the
    cutoff test needs it, and "unknown" is not "after".

    NEVER THE COMPOSER DRAFT. Only the held QUEUE is read here
    (`session_state._held_messages_with_timestamps`) — a live draft in the
    predecessor's input box is a half-typed line, the operator's own unsent
    words, and the successor cannot finish it for them. `held_items`'s
    handoff section is where a draft belongs; this function never calls
    `session_input._read_input_box` at all.

    Targets `successor_pane_id` DIRECTLY, bypassing `send_message`'s normal
    by-name pane resolution: this runs inside the successor's own turn, so
    `TT.claude_pane(tmux_session)` could otherwise resolve to the
    predecessor's still-alive pane instead of the successor's — the two
    coexist in one tmux session for the brief window before `close_out`
    kills the predecessor's (see `session_input.send_message`'s `pane_id`
    parameter, added for this exact call).

    Also passes `skip_blocked_check=True`, the one caller allowed to: this
    runs inside the successor's own turn, picking up a handoff it just read,
    so a blocked picker cannot be up in that pane for the gate to protect
    against in the first place. `send_message`'s default still runs that
    check even when `pane_id` is given (CQ-420 round 2) — this call site is
    the named exception, not a general license for a future `pane_id`
    caller to assume the same.

    Idempotency is the CALLER's, and only ONE-SIDED: `_cmd_picked_up` calls
    this before the side channel's `picked_up` flag flips (round 2, PR
    #588), so a retry that follows a crash BEFORE that flip re-reads the
    predecessor's queue and can re-send a message an earlier, interrupted
    call already delivered — a possible duplicate, accepted deliberately so
    that the alternative (the flag flipping first) can never turn a crash
    into a silently, permanently dropped message. Once the flag has flipped,
    `_cmd_picked_up` never calls this again for the same handoff, so a
    retry past that point re-reads nothing.
    """
    import session_input as SI  # noqa: E402  (see the module-level note above)
    import session_state as SS  # noqa: E402  (see the module-level note above)

    try:
        cutoff = Path(handoff_path).stat().st_mtime
    except OSError as exc:
        return 0, [f"could not read handoff.md's write time, nothing forwarded: {exc}"]

    recorded = TT.transcript_for_session_id(predecessor_session_id)
    if recorded is None:
        return 0, []
    messages = SS._held_messages_with_timestamps(recorded)
    if not messages:
        return 0, []

    sent = 0
    failures: list[str] = []
    for m in messages:
        ts = m.get("queued_at")
        when = _parse_queue_ts(ts)
        if when is None or when <= cutoff:
            continue
        try:
            SI.send_message(tmux_session, m["content"], pane_id=successor_pane_id,
                            skip_blocked_check=True)
            sent += 1
        except Exception as exc:                                  # noqa: BLE001
            failure = f"could not forward a held message (queued_at={ts!r}): {exc}"
            failures.append(failure)
            _log_close_out_skip(log_dir if log_dir is not None else RP.CACHE_DIR,
                                f"{time.time():.0f} {failure}\n")
    return sent, failures


# --- the handoff directory: one per Claude session (ADR-0069 Slice 5) ---
#
# `agentctl session-dir` hands `/rollover` and `/handoff-leave` their directory,
# `bin/co-rollover` reads `handoff.md` from it, and the ops MCP `handoff_write`
# tool writes it for a session with no Write tool. Before this section, `session-dir`
# minted a new numbered directory on every call unless CONVIVY_SESSION_DIR was set,
# so no process could compute the path a session had written to.
#
# THE KEY IS THE CLAUDE SESSION ID, not the tmux session name. A rollover's successor
# shares its predecessor's tmux session, so a name-keyed directory would give both
# one handoff file. The first call for a session mints a directory and records it in
# a pointer file under `handoff_pointer_root()`; every later call returns it.

# `\Z`, not `$`: `$` also matches before a trailing newline, so "sess-a\n" would pass.
SESSION_KEY_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,127}\Z")
PRODUCT_SLUG_OK = re.compile(r"^[a-z0-9][a-z0-9._-]*\Z")


class HandoffDirError(Exception):
    """A session's handoff directory cannot be resolved. The message says why."""


def handoff_pointer_root() -> Path:
    """Where the per-session pointer files live. Re-read on every call, so a test
    can point HOME at a temporary directory."""
    return Path.home() / ".local" / "state" / "convivy" / "session-dirs"


def git_toplevel() -> str:
    """The git root of the working directory, or the directory itself outside a
    repository. The product is its basename. `bin/co-rollover` and the ops MCP
    `handoff_write` tool both call this. The product only names a new directory,
    and a recorded one is returned whatever product the caller passes."""
    try:
        done = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return os.getcwd()
    if done.returncode == 0 and done.stdout.strip():
        return done.stdout.strip()
    return os.getcwd()


def mint_handoff_dir(product: str, *, runs_root: Path, now: datetime) -> Path:
    """A new, numbered `co-<product>-<HHMM>` directory under today's bucket. `mkdir`
    is the claim, so two callers in the same minute never share one."""
    day = now.strftime("%Y-%m-%d")
    base = f"co-{product}-{now.strftime('%H%M')}"
    n = 1
    while True:
        d = runs_root / day / (base if n == 1 else f"{base}-{n}")
        try:
            d.mkdir(parents=True)
            return d
        except FileExistsError:
            n += 1


def _read_pointer(pointer: Path, *, runs_root: Path) -> Path | None:
    """The directory `pointer` records, or None when there is no pointer. A record
    that cannot be vouched for is refused, never replaced with a minted directory.
    The pointer lives where every process of this user can write it, so the named
    directory must sit under the runs root with no symlink on its final component."""
    try:
        text = pointer.read_text().strip()
    except FileNotFoundError:
        # A dangling symlink also reads as missing; it is a record that cannot be
        # followed, not an absent one, so it must not fall through to a mint.
        if pointer.is_symlink():
            raise HandoffDirError(f"the record for this session, {pointer}, is a dangling symlink")
        return None
    except OSError as exc:
        raise HandoffDirError(f"the record for this session, {pointer}, cannot be read: "
                              f"{exc}") from exc
    if not text or not Path(text).is_dir():
        raise HandoffDirError(f"the directory recorded for this session, {text or 'nothing'}, "
                              "is missing")
    named = Path(text)
    if named.is_symlink():
        raise HandoffDirError(f"the directory recorded for this session, {text}, is a symlink")
    if runs_root.resolve() not in named.resolve().parents:
        raise HandoffDirError(f"the directory recorded for this session, {text}, is outside "
                              f"the agent-runs root {runs_root}")
    return named


def _record_pointer(pointer: Path, d: Path, *, runs_root: Path) -> Path:
    """Point `pointer` at `d`, unless another caller recorded first. Returns the
    directory the pointer names once this call returns."""
    pointer.parent.mkdir(parents=True, exist_ok=True)
    # The pointer appears whole or not at all: write a temp file, then `link` it
    # into place, which fails if the pointer already exists.
    fd, tmp = tempfile.mkstemp(dir=pointer.parent, prefix=".pointer-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(f"{d}\n")
        try:
            os.link(tmp, pointer)
        except FileExistsError:
            d.rmdir()  # ours is still empty: nothing has been written into it yet
            return _read_pointer(pointer, runs_root=runs_root)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return d


def session_handoff_dir(session_id: str, product: str, *, runs_root: Path,
                        now: datetime) -> Path:
    """This Claude session's handoff directory: the one it recorded, or a new one
    minted and recorded on the first call. `now` is read only when minting."""
    if not SESSION_KEY_OK.match(session_id or ""):
        raise HandoffDirError(f"session id {session_id!r} is not a usable key")
    if not PRODUCT_SLUG_OK.match(product or ""):
        raise HandoffDirError(f"product {product!r} is not a lowercase slug")
    pointer = handoff_pointer_root() / session_id
    recorded = _read_pointer(pointer, runs_root=runs_root)
    if recorded is not None:
        return recorded
    return _record_pointer(pointer, mint_handoff_dir(product, runs_root=runs_root, now=now),
                           runs_root=runs_root)


def write_handoff(path: Path, text: str) -> None:
    """Replace `path` with `text` in one rename, so a reader sees the old file or
    the new one and never a part-written file. A symlink at `path` is replaced by
    the rename, never written through."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".handoff-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
