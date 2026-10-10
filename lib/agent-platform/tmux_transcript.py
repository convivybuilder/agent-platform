"""tmux_transcript — which transcript belongs to the co session in a tmux pane.

THE JOIN THIS CLOSES. Deck's rail rows are tmux session names; `session_view`
renders a Claude Code transcript identified by session UUID. Nothing connected
the two, which made the collapsed session view unreachable from Deck.

THE HEURISTIC THAT LOOKS RIGHT AND IS WRONG. The obvious join is the tmux pane's
cwd, mangled into a `~/.claude/projects/` directory name, then the newest
`.jsonl` in it. Run against the live `deck` pane on 2026-07-22 it returned the
wrong session: a computer-use fork in the same directory had a newer mtime than
the pane's own session, which was merely idle for five minutes. Newest
`heartbeat_at` fails identically, and for the same reason — several claude
processes legitimately share one cwd, so recency does not identify one of them.
That failure is intermittent and silent, which is the worst shape for a bug that
decides which conversation a human is reading.

THE JOIN THAT HOLDS. Identity, not recency. The `SessionStart` hook writes a
`co-*/meta.json` per interactive session carrying `ppid`, the live `claude`
process. A tmux pane exposes its shell's pid. Walk from that shell to the
`claude` process beneath it and match on pid, and the answer is exact:
`meta.json` then hands over `session_id` and `transcript_path` directly, so the
project-directory mangling rule is never reconstructed.

WHY THIS LIVES HERE AND NOT IN DECK. The process tree and the run dirs are both
on the mini; Deck is on the laptop and would be reaching across ssh for them
either way. Keeping the join here keeps Deck out of the `run-dir-schema`
contract, and keeps it testable — a Swift implementation of this would be
untestable in Deck's suite.

WHAT THIS COUPLES TO. Three fields of the `co-*` record in
`agent-platform/contracts/run-dir-schema`, as load-bearing join keys rather than
display: `ppid` (the identity match), `transcript_path` (the answer), and
`session_id` (the fallback). This is the first code to depend on any of them, so
they are enumerated in that contract rather than only described. Renaming `ppid`
in `session-event-log.py`'s `_co_record` breaks this silently: every lookup would
find no record and raise JoinError, which reads as "no session running" rather
than as a broken join.

THE INVARIANT UNDERNEATH `ppid`. The SessionStart hook records `os.getppid()`,
which equals the `claude` pid only while Claude Code invokes the hook as a direct
subprocess. Were it to wrap the hook in a shell, `ppid` would be that shell's,
nothing would ever match, and every call would degrade to JoinError. That is the
one failure mode invisible from the record itself, which is why it is written
down here.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".local" / "lib" / "agent-platform"))
import telemetry as T  # noqa: E402
import private_crew as PC  # noqa: E402

RUNS_ROOT = T.RUNS_ROOT

# Same charset `session_view.transcript_path` anchors ids to. A tmux session name
# reaches here from Deck's config, so it is validated before it is interpolated
# into a `tmux` argument, for the reason Deck validates it on its own side too.
SESSION_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")

# How many days back to search for a `co-*` dir. A session started yesterday and
# still running today has its dir in yesterday's bucket, and the day-key is local
# rather than UTC, so a small window is searched rather than a path predicted.
# The run-dir contract makes the same point about `Stop` finding its record by
# search: predicting the path is what breaks across a day boundary.
SEARCH_DAYS = 3


class JoinError(Exception):
    """No transcript could be identified for a session name."""


def run(argv: list[str], stdin: str | None = None) -> str | None:
    """Command output, or None if the command fails or is absent.

    A missing `tmux` and a session that does not exist are the same answer here:
    no transcript. Neither is exceptional enough to raise from.

    `stdin` exists so a caller can hand tmux a message body without it ever
    becoming an argument — `load-buffer -` reads the text on stdin, which keeps
    arbitrary user text out of argv entirely. Public because `session_input`
    needs the same helper, and reaching into another module's privates to get it
    would be worse than exporting one function.
    """
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, timeout=10, input=stdin)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


# The original name, kept because every call site below reads better unprefixed
# and the module's own use of it is internal.
_run = run


def validate_session_name(session_name: str) -> str:
    """The name, or JoinError. Extracted because there are now three callers.

    Every verb that reaches tmux goes through this: the name lands in argv,
    which ssh joins and the remote shell splits again, so the charset is the
    gate rather than quoting.
    """
    if not SESSION_NAME_RE.fullmatch(session_name):
        raise JoinError(f"not a usable tmux session name: {session_name!r}")
    return session_name


def session_table() -> dict[str, str]:
    """Every live session's `#{session_id}` keyed by `#{session_name}`, from
    one `tmux list-sessions` call.

    Public so a batch caller can resolve this once and hand the same table to
    every per-session lookup, rather than each of N sessions issuing its own
    `list-sessions` for an identical table — see `_resolve_session_id`'s
    `table` parameter.
    """
    out = _run(["tmux", "list-sessions", "-F", "#{session_id} #{session_name}"])
    table: dict[str, str] = {}
    if out:
        for line in out.splitlines():
            session_id, _, name = line.partition(" ")
            table[name] = session_id
    return table


def _resolve_session_id(session_name: str, table: dict[str, str] | None = None) -> str:
    """The exact `#{session_id}` (e.g. `$26`) for a session name, or JoinError.

    THE BUG THIS CLOSES. `-t <name>` on `list-panes`/`list-windows` resolves
    `-t` as a target PATTERN, not an exact name: tmux tries the name as an
    exact session match, then as a session-name PREFIX. A target `zzprobe`
    therefore answers for a live session `zzprobe-second` even though no
    session named `zzprobe` exists — reproduced on this machine, tmux 3.6a,
    twice, independently:

        tmux new-session -d -s zzprobe-second
        tmux list-panes -t zzprobe    -F '#{session_name}'  -> zzprobe-second
        tmux list-panes -t '=zzprobe' -F '#{session_name}'  -> zzprobe-second

    `=` DOES NOT FIX THIS. It looks like the documented exact-match anchor,
    but is not honoured for this target on 3.6a, as the second line above
    shows. Do not reach for it as the fix.

    A COLON-LESS TARGET HAS NO WINDOW-NAME FALLBACK — CHECKED, NOT ASSUMED.
    It is tempting to worry that a window could stand in for a session of the
    same name: `ask--audio` has a window literally named `ask` on this
    machine today. It does not reproduce. A colon-less `-t` on `list-panes`
    resolves as a target-window string, but `man tmux`'s target
    specification is explicit that a colon-less target-window is parsed
    purely by target-SESSION rules — id, then exact name, then session-name
    prefix — with the window defaulting to the current one; there is no
    fallback to a bare cross-session window-name search. Verified live on
    this machine, tmux 3.6a, twice, independently:

        tmux new-session -d -s zzhost-xyz
        tmux rename-window -t zzhost-xyz:1 zzuniquewin
        tmux list-panes -t zzuniquewin -F 'session=#{session_name}'
          -> can't find window: zzuniquewin

    So the session-name PREFIX above is the entire exposure, not one of two.
    Targeting an id closes it: the `$`-prefixed branch of tmux's target
    resolution is an exact, unprefixable match.

    Resolving the name to an id here, in Python, via an exact dict-key
    lookup, and then targeting the id is what makes the match exact — tmux's
    own resolution never gets to run against the ambiguous name at all.

    `table`, when given, is a pre-fetched `session_table()` result: a batch
    caller resolves it once and passes it to every per-session lookup so N
    sessions share one `list-sessions` instead of issuing N identical ones.
    Left unset, this fetches its own — the right default for a single lookup,
    and also for a caller polling for a session to DISAPPEAR (`_await_agent_
    exit`), where sharing one fetched table across iterations would keep
    reporting a gone session as alive.
    """
    if table is None:
        table = session_table()
    session_id = table.get(session_name)
    if session_id is not None:
        return session_id
    raise JoinError(f"no such tmux session: {session_name!r}")


def pane_pids(session_name: str, table: dict[str, str] | None = None) -> list[int]:
    """The shell pids of every pane in a tmux session, or [].

    `-s`: without it, `list-panes -t <session>` (no `:window`) answers for the
    session's single CURRENT (active) window only — confirmed on tmux 3.6a with
    a throwaway two-window session, where the plain form listed window 1 alone
    and `-s` listed both. A session with more than one window is exactly the
    shape a rollover leaves behind (ADR-0069 Slice 5: the predecessor's window
    plus the successor's), so without `-s` this silently stopped seeing
    whichever window was not active — a claude process in it read as absent.
    """
    validate_session_name(session_name)
    try:
        session_id = _resolve_session_id(session_name, table)
    except JoinError:
        return []
    out = _run(["tmux", "list-panes", "-s", "-t", session_id, "-F", "#{pane_pid}"])
    if not out:
        return []
    return [int(line) for line in out.splitlines() if line.strip().isdigit()]


def panes(session_name: str, table: dict[str, str] | None = None) -> list[tuple[str, int]]:
    """Every pane in a session as `(pane_id, pane_pid)`, in tmux's order.

    `pane_id` (the `%N` form) rather than an index, because an index shifts when
    a window is closed and a stale index addresses somebody else's pane. The id
    is stable for the pane's lifetime.

    `-s`, for the reason `pane_pids` above gives: this must see every window of
    the session, not only whichever one happens to be active.
    """
    validate_session_name(session_name)
    try:
        session_id = _resolve_session_id(session_name, table)
    except JoinError:
        return []
    out = _run(["tmux", "list-panes", "-s", "-t", session_id, "-F", "#{pane_id} #{pane_pid}"])
    if not out:
        return []
    found = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].startswith("%") and parts[1].isdigit():
            found.append((parts[0], int(parts[1])))
    return found


def claude_pids_for_pane(pane_pid: int) -> list[int]:
    """The claude processes belonging to a pane, its OWN process included.

    THE CASE THIS ADDS. A pane started with `exec claude`, or any setup where
    claude replaces the shell rather than running under it, has claude AS the
    pane process — `pane_pid` itself. Walking only descendants finds nothing and
    the session reads as having no agent at all, which is what a scratch session
    started that way did: `state=unknown, alive=False`, while claude sat there
    plainly running.

    The fleet's own sessions take the other shape (`tmux new -s <name>` starts a
    shell, claude runs inside it), which is why this went unnoticed — every
    session tested against had claude one level down.
    """
    if _is_claude(_command_of(pane_pid)):
        return [pane_pid]
    return claude_pids_under(pane_pid)


def claude_pane(session_name: str, table: dict[str, str] | None = None) -> tuple[str, int]:
    """The `(pane_id, claude_pid)` of the pane running the agent.

    THE TARGET FOR ANY INPUT. Not "the session's active pane": that is true today
    only because every fleet session has one window, and the terminal-button work
    adds more. Not a match on the command name either — `pane_current_command`
    reports Claude Code's version string (`2.1.217`), not `claude`.

    So it is resolved the same way the transcript join resolves identity: walk
    each pane's process tree and find the claude beneath it.

    `table`, when given, is a pre-fetched `session_table()` — see
    `_resolve_session_id` for why a batch caller passes one in.
    """
    for pane_id, pane_pid in panes(session_name, table):
        for claude_pid in claude_pids_for_pane(pane_pid):
            return pane_id, claude_pid
    raise JoinError(f"no claude process found in any pane of {session_name!r}")


def _children(pid: int) -> list[int]:
    """The direct children of a pid, in `ps` order.

    NOT `pgrep -P`, AND THE REASON IS NOT STYLE. macOS pgrep excludes its own
    ancestors by default (`man pgrep`: "the current pgrep or pkill process and
    all of its ancestors are excluded"), so walking a pane's tree FROM INSIDE
    the session being walked skipped the very claude that was asking:
    `claude_pane` raised JoinError and the session read `alive=False` while it
    was plainly running. Deck never saw it, because Deck asks over ssh from
    outside every session; every on-mini caller inspecting its own session did.

    `pgrep -a` would opt back in on macOS, but the same flag means "show the
    full command line" on Linux, where it would turn each line into `pid cmd`
    and the digit filter would silently drop every child. `ps` answers the
    question the same way on both.
    """
    out = _run(["ps", "-axo", "pid=,ppid="])
    if not out:
        return []
    children = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            if int(parts[1]) == pid:
                children.append(int(parts[0]))
    return children


def _command_of(pid: int) -> str:
    return _run(["ps", "-o", "command=", "-p", str(pid)]) or ""


def claude_pids_under(pid: int, max_depth: int = 6) -> list[int]:
    """Every `claude` process in the tree beneath a pid, breadth first.

    Breadth first because the nearest `claude` to the pane's shell is the
    session the human is looking at; anything deeper is something that session
    spawned. Depth is bounded so a pathological tree cannot hang the caller.

    Descends the tree rather than scanning every `claude` on the box by cwd,
    which is the same mistake as the mtime heuristic wearing different clothes:
    a detached fork shares the cwd but is not a descendant of the pane.
    """
    found: list[int] = []
    frontier = [pid]
    seen = {pid}
    for _ in range(max_depth):
        if not frontier:
            break
        nxt = []
        for parent in frontier:
            for child in _children(parent):
                if child in seen:
                    continue
                seen.add(child)
                if _is_claude(_command_of(child)):
                    found.append(child)
                else:
                    nxt.append(child)
        if found:
            return found
        frontier = nxt
    return found


def _is_claude(command: str) -> bool:
    """Whether a `ps` command line is the claude CLI.

    Matched on the executable's basename so a path prefix, a version manager
    shim, or trailing arguments do not change the answer. `claude-something`
    must not match, which is why the boundary is checked rather than a substring.
    """
    if not command:
        return False
    argv0 = command.split()[0]
    return Path(argv0).name == "claude"


def _run_roots() -> list[Path]:
    """Every place a `co-*/meta.json` might live: the shared `RUNS_ROOT`, plus
    each registered private crew's own `agent-runs` directory.

    `session-event-log.py`'s SessionStart/Stop hook routes a private crew's
    identity record to `<crew home>/agent-runs/` instead of the shared root
    (ADR-0043), and `meta_for_pid` used to search only the shared one — CFO's
    long-running session kept resolving only because a pre-cutover record
    happened to still sit in the shared root within this pid's search window;
    the next restart would have returned None and broken `session_state`
    earlier and harder than the sibling hook-log bug this shares its root
    cause with (agent-platform#421 / issue_01M0GVMBXCX10F0R3ZAGHGEKFZ).

    Unlike `session_state`'s hook-log lookup, this cannot key off a known
    `cwd` — discovering `cwd` IS what this search is for, before any meta.json
    has been read. So every registered private crew is searched rather than
    one resolved in advance; there are few of them (one, today), and only the
    crew whose record actually matches this pid will ever produce a hit.

    An unreadable registry degrades to the shared root ALONE, matching what
    this function always did before private routing existed — no worse than
    the pre-existing behaviour, and consistent with `private_crew`'s own
    fail-closed direction being the caller's to pick.
    """
    roots = [RUNS_ROOT]
    try:
        roots.extend(Path(home) / "agent-runs" for home in PC.private_homes())
    except Exception:
        pass
    return roots


def _recent_run_dirs(days: int = SEARCH_DAYS) -> list[Path]:
    today = date.today()
    dirs = []
    for root in _run_roots():
        for back in range(days):
            day = root / (today - timedelta(days=back)).isoformat()
            if day.is_dir():
                dirs.append(day)
    return dirs


# `ps` emits English month abbreviations whatever LC_TIME says, so the table is
# fixed rather than looked up. `strptime("%a %b …")` was the first attempt and it
# reads the PROCESS's locale to interpret them: under fr_FR, de_DE, es_ES or
# ja_JP it raises, the parse degrades to the old three-day window, and the bug
# this commit fixes comes back — silently, and only for whoever runs a non-English
# locale. Caught by review; verified in all four.
_PS_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def _parse_ps_lstart(text: str) -> "date | None":
    """The date out of `ps -o lstart=`, e.g. `Mon Jul 20 22:30:05 2026`.

    Only the date is wanted, so the clock field is skipped rather than parsed.
    A single-digit day is space-padded by `ps` (`Jul  1`), which `split()`
    absorbs.
    """
    parts = text.split()
    if len(parts) != 5:
        return None
    month = _PS_MONTHS.get(parts[1])
    if month is None:
        return None
    try:
        return date(int(parts[4]), month, int(parts[2]))
    except ValueError:
        return None


def _process_start_date(pid: int) -> "date | None":
    """The local date a process started, or None if it cannot be read.

    `ps -o lstart=` rather than `-o start=`: the short form degrades to an
    elapsed-time shorthand after 24 hours, which is exactly the range that
    matters here.
    """
    out = _run(["ps", "-o", "lstart=", "-p", str(pid)])
    return _parse_ps_lstart(out.strip()) if out else None


def _process_start_timestamp(pid: int) -> "datetime | None":
    """The full local start time of a process, to the second, or None.

    Beside `_process_start_date` above, which throws the clock away because
    placing a run-dir search window only ever needed the day. `meta_for_pid`'s
    stale-record check needs to compare against a `co-*/meta.json` record's own
    `started_at` to the second, which a date alone cannot settle.
    """
    out = _run(["ps", "-o", "lstart=", "-p", str(pid)])
    return _parse_ps_lstart_full(out.strip()) if out else None


def _parse_ps_lstart_full(text: str) -> "datetime | None":
    """The full local start time out of `ps -o lstart=`, aware in the system
    timezone so it can be compared directly against a record's own
    (timezone-aware) `started_at`.

    Same fields and the same month table as `_parse_ps_lstart`, which throws
    the clock away; kept separate rather than shared because that function's
    only caller wants a day, and returning the richer type there would force
    it to throw away a timezone it never asked for.

    `naive.astimezone()`, with no argument, treats a naive datetime as already
    being in the system's local timezone and attaches that timezone rather
    than converting the wall-clock time — which is what `ps -o lstart=`
    reports in.
    """
    parts = text.split()
    if len(parts) != 5:
        return None
    month = _PS_MONTHS.get(parts[1])
    if month is None:
        return None
    time_parts = parts[3].split(":")
    if len(time_parts) != 3 or not all(p.isdigit() for p in time_parts):
        return None
    hour, minute, second = (int(p) for p in time_parts)
    try:
        naive = datetime(int(parts[4]), month, int(parts[2]), hour, minute, second)
    except ValueError:
        return None
    return naive.astimezone()


# How much a record's own `started_at` may fall short of the live process's
# own start time before it is treated as belonging to an earlier process that
# held the same pid. `ps -o lstart=`'s resolution is one second, so a record
# written the same second the process started can measure either side of it.
_STARTED_AT_SLACK = timedelta(seconds=1)


def _record_predates_process(meta: dict, process_started_at: "datetime") -> bool:
    """Whether `meta`'s own `started_at` cannot be reconciled with having been
    written by the process that started at `process_started_at` — the
    collision this closes (2026-09-30, issue_01M3TBR04B10F04Q9HGJWKS8HG):
    selector's live `co` session, pid 49951, started 2026-09-30T21:28:31Z and
    wrote its own record's `started_at` six seconds later, but a search rooted
    a day earlier also reached a record an EARLIER process that had held pid
    49951 left behind the day before, `started_at` 2026-09-29T14:43:32Z. Both
    records carried different `session_id`s for the one live pid, and
    `meta_for_pid`'s existing collision guard — built for a DIFFERENT
    ambiguity, a stale record in an unrelated private crew's directory — read
    that the same way and refused the live session its own record.

    A record whose own `started_at` predates the process by more than
    `_STARTED_AT_SLACK` was written by whatever process held this pid before
    the current one started, and is excluded here, before the collision guard
    ever sees it — rather than widening that guard to somehow rank the two.

    A record with no `started_at`, or one that fails to parse against the
    process's own (aware) timestamp, is NOT treated as proven stale — it is
    passed through unfiltered instead of skipped. A record from before this
    field existed is exactly that shape (this field is newer than sessions
    that are still running today), and skipping it here would silently lose a
    legitimate answer this function has always returned. Passing it through
    does not risk letting a genuinely stale record win regardless: it still
    has to clear the DIFFERENT-session_id collision guard below, which refuses
    the whole lookup on real ambiguity rather than picking a winner — the
    guard this function feeds, not a second one to duplicate.
    """
    raw = meta.get("started_at")
    if not isinstance(raw, str):
        return False
    try:
        recorded = datetime.fromisoformat(raw)
    except ValueError:
        return False
    try:
        return recorded < process_started_at - _STARTED_AT_SLACK
    except TypeError:
        # `recorded` and `process_started_at` disagree on aware vs. naive —
        # not evidence of staleness, so it gets the same answer as any other
        # comparison this function cannot make.
        return False


def _run_dirs_since(start: "date") -> list[Path]:
    """Every day bucket from `start` through today, newest first.

    Yesterday-and-earlier buckets are included because the day-key is local and
    a session that began just before midnight files its dir under the previous
    day.
    """
    today = date.today()
    dirs = []
    for root in _run_roots():
        day = today
        while day >= start:
            path = root / day.isoformat()
            if path.is_dir():
                dirs.append(path)
            day -= timedelta(days=1)
    return dirs


def meta_for_pid(pid: int, days: int = SEARCH_DAYS) -> dict | None:
    """The `co-*/meta.json` whose `ppid` is this process, or None.

    THE WINDOW IS MEASURED AGAINST THE PROCESS, NOT THE CLOCK. A run dir is
    filed under the day its session STARTED, and a co session runs for as long
    as it is left running — so a fixed "last N days" window loses every session
    older than N, and loses it precisely because it has been up a long time.
    That is backwards, and it was not hypothetical: on 2026-07-23 the sous-chef
    and vivy sessions, alive and healthy since 2026-07-20, both read
    `state: unknown` in Deck's pane because their records sat one day outside a
    three-day window.

    So the search runs from the process's own start date forward. That is exact
    rather than generous — the record can only be in that range — and it reads
    about one day of run dirs instead of every day since the fleet began.

    `days` remains the fallback for a process whose start date cannot be read,
    and callers that pass it explicitly still get the old bounded behaviour.

    A malformed or unreadable meta.json is skipped rather than fatal, per the
    run-dir contract's degradation rule: one bad record must not hide every
    other session.

    TWO DISTINCT RECORDS CLAIMING THE SAME PID REFUSE RATHER THAN PICK ONE.
    `_run_roots()` now searches every registered private crew's own
    `agent-runs/` alongside the shared root (agent-platform#421), which means
    a dead session's stale record inside a private crew's directory is in the
    search space too. OS pid reuse makes it possible — not likely, but real —
    for a CURRENTLY LIVE, unrelated pid to numerically match a stale `ppid`
    left behind in a private crew's `co-*/meta.json`. Picking whichever record
    the loop reaches first would, in that collision, risk handing an ordinary
    caller a black-box crew's own record — including its `transcript_path`,
    which `transcript_for_tmux_session` would then read in full, exactly the
    leak ADR-0043 exists to prevent (security-reviewer, PR #421, run-71cd97f7).
    Only one meta.json should ever exist for a given session in the first
    place, so two DIFFERENT `session_id`s both claiming one pid is itself the
    signal something is wrong — refusing is not a loss of a real answer, it is
    declining to guess at an already-broken input.

    A RECORD FROM AN EARLIER PROCESS THAT HELD THIS PID IS NOT A COLLISION —
    IT IS SKIPPED BEFORE THE COLLISION CHECK EVER SEES IT. The collision guard
    above was built for a stale record in an unrelated private crew's
    directory matching a live pid by chance; it was not built for the far more
    ordinary case of THIS pid's own prior occupant, whose record sits in the
    very same day-bucket search this function runs to find the live session's
    own record. Filed 2026-09-30 (issue_01M3TBR04B10F04Q9HGJWKS8HG): a live
    `co` session's record and a dead one from an earlier process reusing its
    pid carried different `session_id`s, the collision guard read that as the
    private-crew ambiguity, and refused the live session its own record.
    `_record_predates_process` excludes any record whose own `started_at`
    predates the live process's actual start time, so the dead occupant's
    record never reaches the guard above at all. A record with no `started_at`
    to compare, or the process's own start time being unreadable, is not
    excluded here — it still reaches the guard above unfiltered, which is
    where genuine ambiguity already refuses rather than guesses.
    """
    process_started_at = _process_start_timestamp(pid)
    start = process_started_at.date() if process_started_at else None
    # A day either side of the start: the process clock and the day-key are both
    # local, but a session that starts within a second of midnight can land on
    # either side of it.
    searched = _run_dirs_since(start - timedelta(days=1)) if start else _recent_run_dirs(days)
    found = None
    for day in searched:
        for meta_path in sorted(day.glob("co-*/meta.json")):
            try:
                meta = json.loads(meta_path.read_text())
            except (OSError, ValueError):
                continue
            if not (isinstance(meta, dict) and meta.get("ppid") == pid):
                continue
            if process_started_at is not None and _record_predates_process(meta, process_started_at):
                continue
            if found is not None and found.get("session_id") != meta.get("session_id"):
                return None
            found = meta
    return found


def transcript_for_claude_pid(claude_pid: int, days: int = SEARCH_DAYS) -> Path:
    """The transcript for one already-identified claude pid — the single-pane
    form of `transcript_for_tmux_session`, for a caller that has already
    resolved the one pane an action must stay inside (CQ-420 pane agreement).

    `transcript_for_tmux_session` walks EVERY pane of a session and returns the
    first one with a resolvable run-dir record, which is not necessarily the
    pane `claude_pane` names: during a rollover handoff two live claude panes
    share one tmux session (ADR-0069 Slice 5), and a fenced predecessor's
    process can still be running with its own run-dir record aged out of the
    search window while the successor's resolves. `send_message` goes to
    whichever pane `claude_pane` names; a caller confirming delivery for that
    exact send must read that pane's transcript, never fall through to the
    other pane's merely because it resolves.

    Raises `JoinError` when this one pid has no resolvable record — there is
    no other pane here to fall back to.
    """
    meta = meta_for_pid(claude_pid, days=days)
    if meta is not None:
        recorded = contained_transcript(meta.get("transcript_path"))
        # `transcript_path` is the recorded answer. The session_id glob is
        # the fallback for a record written before that field existed, one
        # whose file has since moved, or one that fails containment.
        if recorded is not None:
            return recorded
        found = _transcript_by_id(meta.get("session_id"))
        if found is not None:
            return found
    raise JoinError(
        f"claude pid {claude_pid} has no run-dir record in the last {days} days")


def transcript_for_tmux_session(session_name: str, days: int = SEARCH_DAYS) -> Path:
    """The transcript path for the co session running in a tmux session.

    Raises JoinError with a reason a human can act on, because every failure
    here has a different fix: no such tmux session, nothing running in it, or a
    session whose hook record never landed.
    """
    pids = pane_pids(session_name)
    if not pids:
        raise JoinError(f"no tmux session named {session_name!r}, or it has no panes")

    for pane in pids:
        for claude_pid in claude_pids_for_pane(pane):
            try:
                return transcript_for_claude_pid(claude_pid, days=days)
            except JoinError:
                continue

    raise JoinError(
        f"{session_name!r} has no claude session with a run-dir record "
        f"in the last {days} days"
    )


def contained_transcript(recorded) -> Path | None:
    """A recorded transcript path, only if it is one we are willing to read.

    THE PRIMITIVE THIS CLOSES. `meta.json` is written by the SessionStart hook,
    which made it tempting to trust. But anything that can write under
    `<root>/agent-runs/` can forge a `co-*/meta.json`, and a claude session
    has exactly that access and knows its own pid — so a prompt-injected session
    could point `transcript_path` at any file the operator can read and have the renderer
    open it. A JSON-shaped secrets file would render its contents.

    Containment rather than trust: the path must resolve inside
    `~/.claude/projects/` and be a `.jsonl` file. `resolve()` first, so a
    symlink out of the tree is caught by the same check as a `..` in the path.

    Every path to a read goes through here, including the `session_id` glob —
    a glob match is a name, and a name can be a symlink.

    KNOWN AND ACCEPTED: this canonicalises a name, it does not hold a
    descriptor, so a file swapped between this check and `read_text` would be
    followed. Closing that means reading from an fd opened inside the boundary,
    which reaches into the renderer. The window is sub-millisecond and the
    attacker already needs write access to `~/.claude/projects/`; noted so a
    future NFS or multi-process context is not surprised by it.
    """
    if not isinstance(recorded, str) or not recorded:
        return None
    projects = (Path.home() / ".claude" / "projects").resolve()
    try:
        path = Path(recorded).resolve()
        if path.suffix != ".jsonl" or not path.is_relative_to(projects):
            return None
        return path if path.is_file() else None
    except (OSError, ValueError, RuntimeError):
        return None


_contained_transcript = contained_transcript


def _transcript_by_id(session_id) -> Path | None:
    """Locate a transcript by session id, the way `session_view` does.

    Anchored on the same charset for the same reason: the id is interpolated
    into a glob pattern, where `*` would be code rather than data.

    Landing inside the glob root is not the same as landing inside the tree. A
    glob match is a name, and a name can be a symlink pointing anywhere — so the
    result goes through the same containment gate as a recorded path. Hardening
    one of two paths to the same read is not hardening it.
    """
    if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
        return None
    projects = Path.home() / ".claude" / "projects"
    found = next(iter(projects.glob(f"*/{session_id}.jsonl")), None)
    return None if found is None else contained_transcript(str(found))


def transcript_for_session_id(session_id) -> Path | None:
    """The transcript path recorded for *session_id* directly, with no tmux
    pane to walk — for a caller that already holds an exact session id
    rather than a name to resolve. `co_rollover.forward_late_messages`
    (Slice 7) is the first of these: it holds the predecessor's session id
    from the rollover side channel, not a live pane, since the predecessor
    may already be fenced or gone by the time it runs. Same glob-then-
    contain lookup `transcript_for_tmux_session` falls back to when a
    pane's own recorded path fails containment."""
    return _transcript_by_id(session_id)
