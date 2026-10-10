"""session_state — what a live Claude Code session is doing, for Deck's pane.

STATE COMES FROM HOOKS, NOT THE SCREEN. Scraping the TUI was the assumed design
and is rejected: Claude Code renders ghost suggestions in the input box that are
byte-identical to real typed input under `capture-pane -p`, so a reader built on
pane text would report user input that does not exist. The hook stream is
structured, already written per session by `session-event-log.py`, and does not
move when the TUI's layout does.

AMENDED 2026-07-30, NARROWLY, and recorded here rather than left for a reader to
discover a contradiction between this paragraph and the code below it. The gate
now carries a `rendered` block scraped from the pane by `picker_render`. The
rejection above still holds for STATE and still holds for the INPUT BOX, which is
where its failure mode lives and which nothing reads. What the scrape adds is
three facts the hook payload provably cannot carry, because the TUI invents them
at render time: which question of a multi-question gate is live, the free-text row
the picker appends, and the review tab a multi-question gate ends on. `-e` is
required for it, since the live tab is identifiable only by its SGR background —
which is a demonstration rather than an assertion, unlike the ghost-suggestion
claim above, which did not reproduce at all in 2.1.220. The full argument, the
captures, and what happens on a version change are in
`deck/specs/reading-the-rendered-picker`.

THE STATE MACHINE LIVES HERE, NOT IN SWIFT. Deck receives a state, not events to
interpret. A second implementation of these transitions in the client would drift
from this one, and this one is testable.

LIVENESS IS THREE SIGNALS, NOT A HEARTBEAT. Measured over 5,656 inter-event gaps:
silence after `UserPromptSubmit` reaches p90 495s and a maximum of 3,798s, so a
working session emits nothing for long stretches. `heartbeat_at` in the run-dir
record cannot cover that — it refreshes on `Stop`, so it is frozen during exactly
the window it appears to describe, and a reader trusting it would call every
working session stale. Instead:

  * the last hook event says WHAT the session is doing,
  * the transcript's mtime says whether it is STILL PROGRESSING (measured against
    live sessions, it ran consistently fresher than the last hook event, because
    it is appended as work happens),
  * the claude pid says whether it is ALIVE AT ALL.

A session whose event is stale, transcript is stale, and pid is gone is dead. One
whose transcript is moving is working, however quiet the hook stream is.

The long silences elsewhere need none of this: after `Stop` or a `Notification`,
"waiting for you" stays true indefinitely. The largest gaps in the whole sample
were exactly that, up to 6,253s.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Sibling modules resolve from session_state's OWN tree, not from wherever the
# deployed ~/.local/lib/agent-platform farm happens to point. The farm is a
# per-file symlink into a checkout; in production that is this same tree, so
# siblings stay current together — but a worktree-under-test or a stale farm
# (the picker_render outage) must not feed session_state a different
# signal_client than the session_state being loaded. test_optional_picker_reader
# pins this: it imports session_state from the tree under test while the farm
# still points elsewhere.
sys.path.insert(0, str(Path(__file__).resolve().parent))
# If a sibling imported signal_client before this module loaded, sys.path
# insertion cannot override the copy already in sys.modules, and the from-import
# below would take that possibly-stale farm copy and raise "cannot import name
# sanitise". Evict it so the from-import loads THIS tree's copy. Not a production
# path today — nothing in session-agent's import chain touches signal_client
# ahead of session_state — but the seam is one future module-scope import away,
# and PR #329 showed the farm pointed somewhere unexpected. Deliberately not a
# try/except that falls back to the stale copy: a silently-wrong module is worse
# than an ImportError.
if "signal_client" in sys.modules:
    del sys.modules["signal_client"]
if "private_crew" in sys.modules:
    del sys.modules["private_crew"]
# signal_client and private_crew BEFORE tmux_transcript: tmux_transcript
# re-inserts the deployed farm path during its own import, which would shadow
# this tree's copies with the farm's possibly-stale ones.
from signal_client import sanitise  # noqa: E402  (shared escape/credential sanitising)
from private_crew import private_home_for, UNKNOWN  # noqa: E402  (agent-platform#421: reader and writer must agree on private-crew routing)
# NOT evicted from sys.modules the way signal_client/private_crew are above.
# telemetry is imported by budget, cost_join, review_protocol, run_retention and
# tmux_transcript too, each binding its own `T = telemetry` at ITS import time;
# deleting the cached module here forces a second, distinct module object into
# sys.modules that only THIS module's `T` would see, leaving every sibling's `T`
# pointing at the original — the exact stale-reference failure this file's farm
# comments warn about, one level removed (a self-inflicted split instead of a
# farm-vs-tree one). `tests/conftest.py` already imports `telemetry` by name
# before anything else for the identical reason, so a plain import here always
# resolves to the one shared copy.
import telemetry as T  # noqa: E402  (guarded cost.json / cost-partial.json readers for _agent_run_detail's in-flight cost hop)
import projects  # noqa: E402  (dispatch_is_private: a dispatch INTO a black box is private wherever it was made from)
import tmux_transcript as TT  # noqa: E402
import builder_root as BR  # noqa: E402  (run records resolve through data_root, company/decisions/0008)
import rollover_policy as RP  # noqa: E402  (Slice 3: session-agent's write side reads/writes the override THIS module never touches)
import session_input as SI  # noqa: E402  (Slice 3: `rollover_command`'s composer-draft and send-message readers)
import crew_key as CK  # noqa: E402  (shared sanitiser `working_on` keys by — same one `bin/co-working-on` writes under, agent-platform CQ-357)
import slate as SLT  # noqa: E402  (platform/decisions/0091: asks_open/asks_answered, CQ-358)

# THE PICKER READER IS OPTIONAL, AND THE IMPORT HAS TO SAY SO.
#
# `picker_render`'s whole contract is that its absence costs a field and nothing
# more: a capture that failed, a version it has not been checked against, a layout
# that matched nothing all return None, and the caller behaves as though the module
# did not exist. A module that cannot be imported is that same case. A hard import
# made it the one failure mode that took the CLI down with it.
#
# It did, on 2026-07-29, within minutes of #303 merging. `~/.local/lib` is a
# per-file symlink farm built by `install.sh`, so a NEW module is unlinked until
# that runs, while the primary checkout it points into is current the moment
# someone pulls. Between the pull and the install, `import picker_render` raised at
# module scope, `session-agent` could not start, and EVERY session read `unknown`
# in Deck with "cannot reach the session" — an optional enhancement turned into a
# total outage of the state channel. Only the terminal pane still worked, which is
# the fallback earning its keep.
#
# Catching ImportError alone would be too narrow. A half-installed module can fail
# on anything its own imports raise, and the answer is identical for all of them.
#
# THE FAILURE IS SILENT TO THE CALLER AND LOUD IN THE LOG, which is the split that
# matters. Absent is a legitimate answer, so it must not raise; but a
# `picker_render` broken by a future edit would otherwise disable the feature with
# nothing anywhere saying why, and the debugging would start from "why is
# `rendered` missing" instead of from the reason. stderr rather than a logger
# because this runs before any logging is configured, and `session-agent`'s stdout
# is a JSON contract that must not be polluted. Raised in review of #309.
# SANITISATION FOR THE IMPORT-FAILURE LINE, so embedded newlines, terminal
# escape sequences, C0 controls, and credential-shaped values from a
# pathological module name or exception message do not pollute Deck's view of
# stderr or the log files. Delegates to the shared signal_client.sanitise (the
# one implementation for every alert/report body; see its docstring) with the
# length cap this line needs.
def _sanitise_error(msg: str, max_len: int = 500) -> str:
    """Collapse newlines, strip terminal escapes and C0 controls, redact
    credential-shaped values, cap *msg* — the shared sanitise with the cap."""
    return sanitise(msg, max_len=max_len)


try:
    import picker_render as PR  # noqa: E402
except Exception as e:  # noqa: BLE001 — any failure here means "no reader"
    sys.stderr.write(
        f"picker_render unavailable, continuing without it: {_sanitise_error(str(e))}\n")
    PR = None

HOOK_LOG_ROOT = BR.runs_root() / "hook-log"

# A session id becomes a filename here, so it is anchored the same way
# `tmux_transcript` anchors it before a glob.
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9_-]+")

# A session outlives a day boundary, so a span is searched rather than a path
# predicted. Note the two day-keys in this system differ and must not be
# conflated: hook-log dirs are UTC, `co-*` run dirs are LOCAL. Getting that wrong
# here read a whole evening's states off the previous day's file.
SEARCH_DAYS = 3

# Event -> state. Anything absent leaves the state unchanged, so an event we do
# not model (or one Anthropic adds) is inert rather than misleading.
#
# `Notification` is deliberately NOT here: it carries its own discrimination in
# `notification_type` and is resolved in `_state_for` below. Leaving it out of
# this table was a real bug — the `deck` session sat on "working" for six and a
# half hours after nudging its user, because a Notification left the previous
# transition standing.
_TRANSITIONS = {
    "SessionStart": "idle",
    "UserPromptSubmit": "working",
    "PreToolUse": "working",
    "SubagentStart": "working",
    "PermissionRequest": "blocked",
    "Stop": "idle",
    "StopFailure": "idle",
    "SessionEnd": "ended",
}

# `notification_type` -> state. The field says WHY attention is wanted, which is
# the part worth consuming; the notification alone is not.
#
# `waiting` is kept distinct from `idle` because the transition is real: idle
# means the turn ended, waiting means the session actively asked for its user and
# has been sitting there since. A pane that wants to nudge needs the difference.
#
# A RAIL SHOULD NOT MARK ROWS DIFFERENTLY ON IT, and this comment used to say it
# should. Deck's rail did until 2026-07-26, which put a mark on a row where
# nothing had changed — same co, same prompt, differing only in whether a minute
# had passed — and left a genuinely ready row blank for its first minute. What
# separates a co that is done from a co still working is `agents`, not this.
#
# Deciding WHETHER to alert is a different question from how to decorate a row,
# and this is still the right input for it: the notification is the session
# saying it wants someone, which a `Stop` is not.
_NOTIFICATION_STATES = {
    "idle_prompt": "waiting",
    "permission_prompt": "blocked",
}


def _state_for(name: str, payload: dict) -> str | None:
    """The state an event implies, or None to leave the state alone."""
    if name != "Notification":
        return _TRANSITIONS.get(name)
    kind = payload.get("notification_type")
    # An unknown notification_type means "it wanted attention, reason unstated".
    # `waiting` is the safe reading: it is true of every notification observed,
    # and it shows a text box rather than inventing a gate that has no payload.
    return _NOTIFICATION_STATES.get(kind, "waiting")

# A session is treated as progressing if its transcript moved this recently.
# Chosen against the measured in-flight silences: p50 106s, p90 495s.
#
# THE TRADE, STATED PLAINLY RATHER THAN FLATTERED. At 300s this sits at ~61% of
# p90, so it accepts FALSE NEGATIVES in the 300-495s band: a session in a pure
# thinking phase, writing no tool results, reads `progressing: False` while it is
# perfectly healthy. That is deliberate — the alternative, a threshold above p90,
# would call a finished session live for eight minutes, and a pane that says
# "working" about a session waiting on its user is the worse error.
#
# The pid is the backstop for the false negatives: `progressing: False` with
# `alive: True` means "quiet, possibly thinking", and only `alive: False` means
# gone. A consumer must read the three signals together; none is a verdict alone.
PROGRESS_WINDOW_SECONDS = 300

# How far the transcript must outrun a gate before the gate is treated as
# answered. See `_gate_is_stale`.
#
# The transcript is written when the question is ASKED — the assistant's
# tool_use record lands at essentially the same instant as the PermissionRequest
# — so a second or two of margin is the question itself, not an answer. Beyond
# that it is work that could only have happened after the gate cleared.
#
# Generous on purpose, and the asymmetry is deliberate: a genuinely blocked
# session writes NO MAIN-AGENT TURN, so the signal below never advances and no
# threshold in this range can clear a live gate early. The only thing a larger
# value costs is showing a resolved gate for a few more seconds.
GATE_RESOLVED_MARGIN_SECONDS = 30

# The record types that are the MAIN AGENT saying or doing something, and so the
# only writes that mean a gate was answered. A blocked agent produces none of
# them; the control-plane records and subagent work that land in the transcript
# around it are neither of these types (or are `isSidechain`).
_CONVERSATIONAL_TYPES = frozenset({"user", "assistant"})

# How much of the transcript tail to read when adjudicating a gate. Large enough
# to hold recent conversational turns past a run of control or subagent records,
# small enough that the read is cheap — and it happens only while a session is
# blocked, never on an ordinary poll.
_GATE_TAIL_BYTES = 1_000_000

# Same idea as the gate bound above but its own name and its own number, because
# it runs on every "working" poll rather than only while blocked, and because a
# queued message can itself be the largest thing in the window — the corpus holds
# one 192,068 characters long.
#
# EIGHT MEGABYTES BECAUSE THE SESSIONS THIS SERVES ARE THE LONG ONES. The window
# decides whether `fold_queue` can answer at all: a transcript larger than this
# may hold the queue's history outside the read, and the fold then refuses rather
# than guessing. At 1MB that refusal was landing constantly on exactly the
# sessions Deck's pane exists for.
#
# Measured 2026-08-10 over transcripts with queue activity, active in the last
# fortnight, split by what the session IS:
#
#   co sessions (what a human watches)   219 files, 37.4% over 1MB, median 676KB
#   dispatched workers                  3844 files,  2.6% over 1MB, median 229KB
#
# The corpus-wide figure is 4.4%, and it is the wrong number to design against —
# it is dominated by short worker runs, which nobody is reading a held message
# in. Against co sessions the cap buys: 1MB → 62.6% folding exactly, 4MB →
# 81.7%, 8MB → 99.5%, with p99 at 6.56MB and the largest seen 8.34MB.
#
# THE COST LANDS ON ONE READER, AND ONLY UP TO THE TAIL IT NEEDS.
# `session_view.render_delta` already reads the whole transcript for its own
# rewind rule, so raising this changes nothing there; `_read_held_queue` is the
# only reader that takes more, and it stops at the tail rather than the cap. Going further buys the one outlier above and is not
# worth the memory on a per-poll path.
_HELD_TAIL_BYTES = 8_000_000

# Fields that decide whether two states differ, for a follower. Ages change on
# every read and would make each poll look like a transition.
#
# `working_on` is NOT added here for exactly that reason — its object carries an
# `age` that moves every poll the way `vitals`' does. `signature()` below folds
# in just its `text`, deliberately outside this tuple; do not "complete" the
# omission by adding the key here.
#
# `ctx_pct` and `rollover` are absent for the same reason and are not folded
# into `signature()` at all, unlike `working_on`: a context percentage moves on
# nearly every poll, and `rollover`'s `since`/`calls_seeking` move the same way
# while a session keeps working through a seeking episode. Neither is a
# transition a follower should ever emit a line for.
_SIGNIFICANT = ("state", "gate", "last_event", "alive", "held")


def _hook_log(session_id: str, cwd: str | None = None, days: int = SEARCH_DAYS) -> Path | None:
    """The newest hook log for a session id, across the recent day buckets.

    KEYED BY UTC, NOT LOCAL, and the difference is not cosmetic.
    `session-event-log.py` uses two conventions in one file on purpose: `co-*`
    run dirs take the LOCAL date (line 85, `now.astimezone()`), and hook-log dirs
    take UTC (line 203, `now` unconverted). So `tmux_transcript`'s run-dir search
    is right to use `date.today()` and this one is not.

    Reading this by local date silently returned YESTERDAY'S file for the whole
    evening in a negative-offset zone, since local-today is UTC-yesterday there.
    Every session then reported a state up to a day old: `svc` showed as blocked
    on a question answered hours earlier, and none of it looked like an error —
    the file existed, parsed, and produced a confident wrong answer. Caught on
    2026-07-23 only because the operator knew what his own session was actually doing.
    """
    for p in _hook_logs(session_id, cwd=cwd, days=days):
        return p
    return None


def _utc_today():
    """The hook-log tree's day key.

    A named seam rather than an inline call, so a test can pin that the lookup
    reads UTC. A test that merely writes and reads at UTC-today cannot tell the
    two conventions apart on a UTC host — which is most CI runners — so it would
    pass just as happily against the bug it exists to prevent.
    """
    return datetime.now(timezone.utc).date()


def _hook_log_root(cwd: str | None) -> tuple[Path | None, str | None]:
    """Which hook-log root this session's records live under: `(root, None)`, or
    `(None, reason)` if that cannot be determined safely.

    Calls the SAME `private_home_for` the writer (`session-event-log.py`) calls
    to decide where to WRITE, so the two can never disagree — the gap that left
    a healthy CFO session reading `unknown` for three days after the writer
    learned to route a private crew's log to its own directory and nothing told
    this reader (agent-platform#421 / issue_01M0GVMBXCX10F0R3ZAGHGEKFZ).

    THE FAIL-DIRECTION IS THE OPPOSITE OF THE WRITER'S, deliberately — see
    `private_crew`'s module docstring for the full argument. The writer cannot
    tell if a session is private and still writes SOMEWHERE, content stripped,
    so instrumentation never silently stops (ADR-0043: a privacy control must
    not fail open). A reader has no such obligation: searching the shared root
    when privacy is `UNKNOWN` could serve a confident `unknown` for a session
    that is actually running, just in a private crew's own directory — a wrong
    answer dressed as a right one. Reporting `unknown` honestly, without
    guessing a root, is the safe answer here.
    """
    private_home = private_home_for(cwd)
    if private_home is UNKNOWN:
        return None, "cannot determine whether this session's crew is private"
    if private_home:
        return Path(private_home) / "hook-log", None
    return HOOK_LOG_ROOT, None


def _hook_logs(session_id: str, cwd: str | None = None, days: int = SEARCH_DAYS) -> list[Path]:
    """Every day-file for a session in the window, NEWEST FIRST.

    More than one, because a session outlives a day boundary and its events are
    split across files. Reading only the newest means that just after UTC
    midnight the fold sees a file with one or two events in it, none of them a
    transition, and reports `unknown` for a session that is plainly running.
    """
    # The same guard `tmux_transcript` puts on this field before a glob, for the
    # same reason: a prompt-injected session can forge the meta.json this id
    # comes from, and here it becomes a PATH COMPONENT. `../..` would walk out
    # of the day directories. Validated in one sibling and not the other is not
    # a guard, it is an accident waiting to be found.
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        return []
    root, _reason = _hook_log_root(cwd)
    if root is None:
        return []
    today = _utc_today()
    found = []
    for back in range(days):
        p = root / (today - timedelta(days=back)).isoformat() / f"{session_id}.jsonl"
        if p.is_file():
            found.append(p)
    return found


def _last_events(path: Path, keep: int = 400) -> list[dict]:
    """The tail of a session's hook log, oldest first.

    Reads the whole file and keeps the tail rather than seeking: these are one
    session's events, and the largest observed are well under a megabyte. A
    malformed line is skipped rather than fatal, per the contract's degradation
    rule — one bad record must not hide the session's state.
    """
    rows: list[dict] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict):
                    rows.append(r)
    except OSError:
        return []
    return rows[-keep:]


def _age(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        then = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds()


def _gate_from(payload: dict, gate_id: str | None = None) -> dict:
    """The blocking question, normalised across the two classes of gate.

    THE TWO DO NOT SHARE A SHAPE, and branching on `tool_name` is required rather
    than tidy: the asking class carries `questions`, the permission class carries
    the gated tool's own input. A reader expecting `questions` finds a `command`.

    Returning a normalised shape means Deck renders from one contract even though
    Anthropic emits two.

    `gate_id` is the `tool_use_id` folded forward from the paired `PreToolUse`
    event. It is emitted as `gateId` when present and omitted entirely when absent
    — not defaulted to None — so a consumer can tell "was not in the stream" from
    "came from a Claude Code build that does not stamp it".
    """
    tool = payload.get("tool_name")
    raw = payload.get("tool_input")
    tool_input = raw if isinstance(raw, dict) else {}

    if tool == "AskUserQuestion":
        questions = tool_input.get("questions")
        out: dict = {
            "kind": "question",
            "tool": tool,
            "questions": questions if isinstance(questions, list) else [],
        }
    else:
        out = {
            "kind": "permission",
            "tool": tool,
            "detail": tool_input,
            # The "don't ask again" affordance, as data. Surfaced so a pane can offer
            # it honestly rather than hiding a settings write behind a button.
            "suggestions": payload.get("permission_suggestions") or [],
        }
    if gate_id is not None:
        out["gateId"] = gate_id
    return out


# --- vitals: the terminal's status area, for a frame that is not the terminal --
# The operator, 2026-07-24: "take all the things from the status area of the TUI, and put
# them in the frame of Deck, so they are visible even in the session view" —
# context, which subscription is billed, whether remote control is on, the local
# agents in flight, and which model is live. Living in the pane meant losing the
# one line that says whether the session is about to degrade.
#
# EACH FACT COMES FROM WHOEVER OWNS IT, and they are owned in three places:
#
#   * The context numbers, the model's display name and the subscription are in
#     the status-line payload Claude Code hands `co-session-health` per turn, and
#     nowhere outside the session. That script records what it rendered; this
#     reads the recording. Deriving them again from the transcript would give a
#     second answer to a question that already has one.
#   * Remote control is in Claude Code's own per-process session file, as the
#     bridge id behind the `/rc` link in the status row.
#   * The agents in flight are in Convivy's `active.json`, which is ours and is
#     current, so they are counted here rather than read from a per-turn file.
#     Two consumers of that contract is expected; the status line keeps its own
#     reader because it runs inside every session and must not take an import it
#     can be broken by.
#
# Nothing here raises and nothing here is required. A vitals block that cannot
# be assembled comes back with the fields it could establish and `None` for the
# rest — the pane's rule for every other field, and the reason a stale install
# or a Claude Code release costs a badge rather than a pane.
CLAUDE_SESSIONS_DIR = Path.home() / ".claude" / "sessions"
VITALS_DIR = Path.home() / ".cache" / "co-session-health"
AGENT_RUNS_ROOT = BR.runs_root()
# The machine's root, where the crews live: the prefix a co's cwd is matched
# against to scope its agents (builder_root.root(), never an environment variable).
CREWS_ROOT = BR.root()


def _product_under_root(cwd, root):
    """The first path segment of `cwd` below `root`, or None when `cwd` is not
    under it. Both are resolved first, the containment idiom the session agent's
    create-dir floor uses against the same resolver, so a symlinked cwd scopes the
    same as the real directory it names (review, PR 539)."""
    if not cwd:
        return None
    try:
        rel = Path(cwd).resolve().relative_to(Path(root).resolve())
    except (TypeError, ValueError, OSError, RuntimeError):
        return None
    return rel.parts[0] if rel.parts else None


ACTIVE_JSON = AGENT_RUNS_ROOT / "active.json"

# Past this, a reading describes a session the terminal has redrawn since, so it
# is reported with its age rather than as the current state. Deliberately
# generous: an idle session's numbers are still true hours later, because nothing
# it has done has changed them.
VITALS_STALE_SECONDS = 3600


def _read_json(path) -> dict | None:
    """One JSON object from a file, or None. Never raises: every caller here has
    a real answer for "could not read it", and it is never "assume a default"."""
    try:
        with open(path) as fh:
            value = json.load(fh)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def remote_control(claude_pid: int | None) -> dict:
    """Whether this session is reachable from the phone, and under what name.

    ANTHROPIC'S SHAPE, OBSERVED RATHER THAN OWNED. `~/.claude/sessions/<pid>.json`
    is Claude Code's own per-process record, and the presence of `bridgeSessionId`
    is what puts the `/rc` link in the status row: it is the id in that link's
    URL. Measured 2026-07-24 against 2.1.219 — an interactive session launched by
    `co` carries one, and a headless `-p` run does not.

    NOT THE `--remote-control` FLAG IN ARGV, which is the reading that looks
    obvious and is wrong for exactly the case the operator cares about. `co` passes that
    flag unconditionally, so argv says "on" for every co session — including the
    ones where he has lately had to turn remote control on by hand, which is why
    he asked for this at all. A field that is always true reports nothing.

    `on` is None rather than False when the file cannot be read, because "Claude
    Code did not write a record we could parse" is not the same answer as "this
    session is not bridged", and a frame that showed the second would be lying.
    """
    if not claude_pid:
        return {"on": None, "name": None}
    record = _read_json(CLAUDE_SESSIONS_DIR / f"{int(claude_pid)}.json")
    if record is None:
        return {"on": None, "name": None}
    bridge = record.get("bridgeSessionId")
    name = record.get("name")
    return {
        "on": bool(bridge) and isinstance(bridge, str),
        "name": name if isinstance(name, str) else None,
    }


def live_agents(cwd: str | None) -> dict:
    """The dispatched runs alive right now, scoped to this session's product.

    Scoped because `active.json` is machine-wide: unscoped, every product's co
    reported another product's coder as its own (operator 2026-07-05). The Keel's co
    works across repos and keeps the fleet-wide view, labelled as such.

    Filtered on the pid being alive, so a run whose record outlived its process
    cannot be counted as in flight.
    """
    root = _read_json(ACTIVE_JSON)
    runs = (root or {}).get("runs")
    if not isinstance(runs, list):
        # `agent_runs` seeded here as well as at the end, for the reason the
        # early-return seeds in `session_state` give: every key this function
        # can return has to be returned on EVERY path, or the caller's
        # `live["agent_runs"]` raises on the one path that has no runs to
        # report. Caught by `test_a_session_with_nothing_dispatched_reports_an
        # _empty_list`, which reaches exactly this branch.
        return {"agents": [], "agent_runs": [], "fleet_wide": False}

    # The crew segment right after the machine's root, not after a fixed
    # path-name substring, so a relocated root still scopes this co's agents
    # (company/decisions/0008).
    product = _product_under_root(cwd, CREWS_ROOT)
    scoped = bool(product) and product != "substrate"

    agents = []
    runs_out = []
    for run in runs:
        if not isinstance(run, dict) or not _pid_alive(run.get("pid")):
            continue
        if scoped and str(run.get("repo")) != product:
            continue
        agents.append(str(run.get("agent") or "?"))
        runs_out.append(_agent_run_detail(run))
    return {"agents": agents, "agent_runs": runs_out, "fleet_wide": not scoped}


def _agent_run_detail(run: dict) -> dict:
    """One live run, as much of it as the registry and its meta.json carry.

    **ADDITIVE ALONGSIDE `agents`, NEVER A RETYPE OF IT.** `agents` is a list of
    bare names and Deck decodes it as one (`SessionState.liveAgents`), so
    widening that key in place would break the consumer this exists to serve —
    the `active-json` contract's rule that adding a field is additive and
    changing one is a version bump, applied to the payload that carries it.
    An older Deck reads `agents` and ignores this; a newer one reads this and
    keeps `agents` as the count it always was.

    **EVERY FIELD IS OPTIONAL AND DEGRADES ALONE.** A run whose `meta.json` is
    missing, half-written or unreadable still yields a row carrying what the
    registry knew — the registry is the thing that says this agent is alive,
    and a row dropped for a missing model would report the fleet as smaller
    than it is. That is the same per-field degrade rule Deck's own consumers
    already follow.

    **`model` IS THE ONE FIELD THAT COSTS A FILE READ**, because the registry
    does not carry it (`active-json` contract, the entry table) and `meta.json`
    does. It is a single `open()` on a path composed arithmetically — never a
    directory listing.

    **AN EARLIER VERSION OF THIS COMMENT SAID THE READ HAPPENS "only on the
    unbriefed path, since `vitals` already skips this whole function under
    `brief`". THAT WAS FALSE** and the correction is the reason this function
    composes a path instead of globbing for one. `brief` skips the DUPLICATE
    `live_agents` call inside `vitals`; the primary call in `session_state`
    runs regardless. So `session_states` — the rail's batched reader, which
    exists precisely so it does not "pay for the per-session file reads vitals
    costs" — reaches this code on every poll, for every pinned session. A glob
    here listed every date directory under `<root>/agent-runs` per live run
    per session per poll, which is the exact cost class `brief` exists to
    avoid, reintroduced through the one call `brief` does not gate (reviewer,
    PR #464).

    **A PRIVATE CREW'S `task` NEVER LEAVES THE MACHINE, AND THIS IS THE FIELD
    ADR-0043 NAMES.** `active.json` is machine-wide, so a fleet-wide read (a
    `cwd` outside a product, where `scoped` is False) walks EVERY registry
    entry — CFO's and any other `private: true` crew's included. This payload
    is then rendered by Deck on the laptop, so an unredacted `task` would carry
    a black-box crew's dispatch prompt off the machine, which is exactly the
    content ADR-0043 exists to keep inside the crew's own directory
    (security-reviewer, PR #464). The agent NAME is unchanged from what
    `agents` already exposed; only the new free text is withheld.

    **FAIL-CLOSED.** This is a DISCLOSURE, so "cannot tell" withholds: an
    unreadable crew registry, or an entry with no usable `repo`, both redact
    the task rather than emit it — see `_crew_is_shareable`.

    **The run still draws.** Dropping the row would under-report the fleet, and
    a consumer cannot tell a quiet panel from an idle one — so the entry is
    served with `task: None`, degrading exactly like a task the registry never
    carried.

    **`task` IS SANITISED even when it is not private.** It is an
    operator-supplied shell argument reaching an independent renderer this repo
    does not control, and nothing stops one carrying a pasted token or a
    terminal control sequence. `signal_client.sanitise` is the primitive this
    codebase already trusts for equivalent free text.

    **`usd` / `cost_source` / `written_at` READ THROUGH THE SAME TWO-FILE HOP
    `budget.py` ALREADY USES, NOT A THIRD COPY OF IT.** #592 gave every live run
    a `cost-partial.json` checkpoint, so "a run that is still going has none" is
    no longer true — an in-flight run now has a RECORDED figure, not merely
    `budget.py`'s pool-wide estimate. `cost.json` (final, from `_finalize_run`)
    is read first and wins when present; `cost-partial.json` (in-flight) is the
    fallback. Either way `cost_source` travels with `usd` and never without it:
    `T.read_cost_guarded`/`T.read_cost_partial` already refuse to return a
    figure with a missing or unrecognised `cost_source`, so a source-less
    partial is unreadable here exactly as it is to `budget.py`, and this
    function adds its own belt (`_finite_nonneg_usd`) rather than trust a
    forged file's `usd` blind — the reasoning `budget._partial_usd` gives for
    the identical clamp on the identical trust boundary applies unchanged: the
    run directory is writable by the run it measures. An `estimate_fallback`
    figure is still exactly that once it reaches Deck — the marking survives
    because `cost_source` rides along with the number, never displayed or
    summed without it (Deck's own commitment, recorded on
    `write_cost_partial`). A run with neither file omits all three fields
    rather than sending nulls or zeros, the same degrade-alone posture as
    every other field here.
    """
    agent = str(run.get("agent") or "?")

    # **`meta.json` IS READ BEFORE THE GATE, NOT AFTER**, because the gate itself
    # needs what only `meta.json` carries. `resolve_agent()` (bin/agentctl)
    # honours `--agents-dir` before it ever looks at `repo`, so `repo` alone
    # cannot say a dispatch reached a black box — the same bypass
    # `_dispatched_by` closes by checking `meta["agent_source"]` too
    # (agent-platform#465, round 2). This costs a file read for every run
    # carrying a `run_id`, same as the read `model` already paid for below —
    # a private run is rare, so this does not add a new cost class to the
    # rail's batched poll path (security-reviewer, round 5).
    run_id = run.get("run_id")
    started_at_raw = run.get("started_at") if isinstance(run.get("started_at"), str) else None
    meta = None
    meta_path = None
    if run_id:
        meta_path = _run_meta_path(str(run_id), started_at_raw)
        meta = _read_json(meta_path) if meta_path is not None else None

    # A run with no `run_id` has no `meta` and so no `agent_source`;
    # `_crew_is_shareable` then falls back to its repo-only half, exactly the
    # answer this function already gave that run — not an oversight, since
    # nothing here has ever been able to name that run's agent source.
    agent_source = meta.get("agent_source") if isinstance(meta, dict) else None

    # **A PRIVATE CREW'S ROW CARRIES ITS NAME AND NOTHING ELSE**, and gating the
    # task alone was not enough. `agents` already exposed a bare agent NAME on a
    # fleet-wide read before this change — a pre-existing leak, and a weak one,
    # because a name on its own binds to nothing. `repo` is the binding: it says
    # WHICH private crew is live, `started_at` says when, `model` says what it is
    # running, and `run_id` correlates them across reads. That composite is a
    # live roster row for a black-box crew, which ADR-0043 forbids in as many
    # words — and it distinguishes that from the Library's separate, deliberate
    # accounting write of model, duration and cost (security-reviewer, #464).
    #
    # Withheld together, from ONE resolution, so no future field can be added
    # past the gate by being read before it.
    if not _crew_is_shareable(run, agent_source):
        return {"run_id": None, "agent": agent, "task": None,
                "started_at": None, "repo": None, "model": None}

    detail = {
        "run_id": str(run_id) if run_id else None,
        "agent": agent,
        "task": sanitise(run["task"], max_len=_TASK_MAX)
        if isinstance(run.get("task"), str) and run["task"] else None,
        "started_at": started_at_raw,
        "repo": run.get("repo") if isinstance(run.get("repo"), str) else None,
        "model": None,
    }
    if isinstance(meta, dict):
        model = meta.get("model_resolved") or meta.get("model")
        detail["model"] = model if isinstance(model, str) else None

    # `usd` / `cost_source` / `written_at`, omitted entirely rather than sent
    # as None — a shape the other fields above deliberately don't use, because
    # unlike those, "no cost figure" and "a zero-cost figure" are different
    # facts and Deck must not conflate them (issue_01M2KVQGDRS6CA4J0DY8R50T71).
    # Reached only past the shareable-gate return above: the SAME resolution
    # that withholds task/repo/model/run_id for an unshareable crew withholds
    # these too, rather than a second check re-deriving that answer.
    if meta_path is not None:
        run_dir = meta_path.parent
        cost = _read_cost_final(run_dir)
        written_at = None
        if cost is not None:
            written_at = meta.get("ended_at") if isinstance(meta, dict) else None
        else:
            cost = _read_cost_in_flight(run_dir)
            if cost is not None:
                written_at = cost.get("written_at")
        if cost is not None:
            usd = _finite_nonneg_usd(cost.get("usd"))
            cost_source = cost.get("cost_source")
            # A figure that skipped a model the pricing table cannot price is a
            # floor, and a floor sent as `usd` reads as the run's spend. Omitting
            # it is the existing "no cost figure" shape, which Deck already
            # distinguishes from zero.
            if cost.get("unpriced_models"):
                usd = None
            if usd is not None and isinstance(cost_source, str):
                detail["usd"] = usd
                detail["cost_source"] = cost_source
                if isinstance(written_at, str):
                    detail["written_at"] = written_at
    return detail


def _read_cost_final(run_dir: Path) -> dict | None:
    """`T.read_cost_guarded(run_dir)`, wrapped the way `budget._partial_usd`
    wraps `T.read_cost_partial`: a pathological nested-bracket body inside
    `cost.json` parses inside the reader's own `json.loads` and can raise
    `RecursionError`, a `RuntimeError` outside the reader's own caught tuple —
    this file is fully controlled by the run being measured (same trust
    boundary as `cost-partial.json`), and this hop runs on every poll of every
    pinned session, so one malformed run's file must degrade only that run's
    row, not crash the batched read for every session (agent-platform#592,
    round 4, on the identical failure one file over)."""
    try:
        return T.read_cost_guarded(run_dir)
    except Exception:
        return None


def _read_cost_in_flight(run_dir: Path) -> dict | None:
    """`T.read_cost_partial(run_dir)`, wrapped for the identical
    `RecursionError` reason `_read_cost_final` is."""
    try:
        return T.read_cost_partial(run_dir)
    except Exception:
        return None


def _finite_nonneg_usd(value) -> float | None:
    """A recorded `usd` accepted only as a finite, non-negative number.

    Same clamp `budget._partial_usd` applies to the identical trust boundary
    (`cost.json`/`cost-partial.json` are writable by the run they measure): a
    forged NaN or Infinity must never reach this payload's JSON encoding,
    which cannot represent either and would either crash the encoder or hand
    Deck a string it doesn't expect."""
    try:
        usd = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(usd) or usd < 0:
        return None
    return usd


def _crew_is_shareable(run: dict, agent_source: str | None = None) -> bool:
    """Whether this run's detail may leave the machine.

    FAIL-CLOSED: every uncertainty withholds — a private crew, an unreadable
    registry, or a `repo` this cannot treat as a slug all answer False.

    **`repo` ALONE IS NOT ENOUGH**, the same bypass `_dispatched_by` closes one
    gate over. `resolve_agent()` (bin/agentctl) honours `--agents-dir` before it
    ever looks at `repo`, so `--on constance --agents-dir
    <root>/CFO/.claude/agents` writes `repo: "constance"` (a public slug)
    while loading a CFO agent — a repo-only check missed it (security-reviewer,
    round 5 of this PR). `projects.dispatch_is_private` is the SAME comparison
    `_dispatched_by` and `bin/agentctl`'s `_is_private_dispatch` both delegate
    to, checking `agent_source` (the file `resolve_agent()` actually resolved
    and read, carried on `meta.json` since agent-platform#465) against every
    private crew's `home_path()` too, so this gate cannot drift from theirs the
    way the repo-only check once did. `agent_source` is optional and
    `None`-safe: a run with no `meta.json` (no `run_id`, or one predating the
    field) gets the repo-only half — the same answer this gate always gave
    that run.

    **THE CHARSET CHECK RUNS BEFORE THE REGISTRY LOOKUP, AND IS NOT
    DECORATION.** `repo` is untrusted registry data, and a value like
    `"../CFO"` is not a registry slug, so the lookup would answer "not
    private" — while still naming the crew to anyone reading the row. Evading
    the gate and identifying the crew are not mutually exclusive, which is what
    an earlier version of this comment assumed. Anything that is not a bare
    slug withholds before `dispatch_is_private` is ever called.
    """
    repo = run.get("repo")
    if not isinstance(repo, str) or not _REPO_SLUG_RE.match(repo):
        return False
    try:
        private = projects.dispatch_is_private(repo, agent_source)
    except Exception:
        # The registry is unreadable or corrupt. Cannot tell — fail closed,
        # matching `_dispatched_by`'s posture on the identical uncertainty
        # one gate over.
        return False
    return not private


_REPO_SLUG_RE = re.compile(r"\A[A-Za-z0-9_.-]+\Z")

# What a task is capped at on the way out. Deck bounds what it DRAWS
# independently; this is the producer half of that pair, not a substitute
# for it.
_TASK_MAX = 500


def _run_meta_path(run_id: str, started_at: str | None):
    """Where a run's `meta.json` lives, composed from its start time.

    **ARITHMETIC, NOT A GLOB.** The run directory's day key is LOCAL time
    (`telemetry.platform_day`, ARCH-156) while `started_at` is UTC
    (`telemetry.now_iso`), so the day is derived by converting one to the
    other rather than by listing directories to find a match. A glob here ran
    on the rail's batched read for every pinned session on every poll; see
    `_agent_run_detail`.

    **The neighbouring days are tried too, and that is not belt-and-braces.**
    The registry's `started_at` and the directory's day key are produced by two
    different clocks-of-record — a run spawned either side of local midnight,
    or on a machine whose zone moved between the spawn and the read, lands one
    day off. Three composed paths cost three `open()` attempts at worst and
    still never list a directory.

    **`run_id` IS VALIDATED BEFORE IT REACHES A PATH.** It comes from
    `active.json`, machine-wide state this function does not own, and every
    other untrusted identifier this file turns into a path goes through the
    same shape check first (`_SESSION_ID_RE`, applied at three sites). An
    unvalidated id carrying `..` segments walks out of the runs root by plain
    path-joining, and `_read_json` would then parse whatever JSON it landed on
    and surface its `model` string into the served payload (reviewer, PR #464).
    A rejected id yields None rather than a path, and the model degrades like
    any other absent field.
    """
    if not isinstance(run_id, str) or not _SESSION_ID_RE.fullmatch(run_id):
        return None
    # RESOLVED AT CALL TIME, NOT READ OFF `AGENT_RUNS_ROOT`, and the neighbouring
    # `_meta_for_run` composing the identical path from that constant is not a
    # duplicate to collapse into this. The constant binds `Path.home()` once at
    # IMPORT; this resolves it on every call. The two are the same path in
    # production and diverge only under test, where each function's own tests
    # redirect the root by the matching mechanism — these by patching `HOME`,
    # `_meta_for_run`'s by patching the constant. Substituting the constant here
    # freezes the root before a test's `HOME` is in place and silently sends
    # four of them looking in the operator's real runs directory (caught by
    # `test_model_is_read_from_the_runs_meta_because_the_registry_has_none` and
    # three siblings, merging #465).
    root = BR.runs_root()
    days = []
    if isinstance(started_at, str):
        # THE DAY ARITHMETIC IS INSIDE THE TRY, not just the parse. An extreme
        # year parses fine and then overflows on the neighbour: `9999-12-31`
        # plus a day, or `0001-01-01` minus one, both raise OverflowError. That
        # propagates through `_agent_run_detail` and `live_agents` to
        # `session_state`, which `session_input.py` calls with no wrapper of its
        # own — so one corrupt record in machine-wide `active.json` took down a
        # whole read rather than degrading one field (security-reviewer, #464).
        try:
            when = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc).astimezone()
            days = [(when + timedelta(days=d)).strftime("%Y-%m-%d")
                    for d in (0, -1, 1)]
        except (ValueError, OverflowError):
            days = []
    if not days:
        days = [datetime.now().astimezone().strftime("%Y-%m-%d")]
    for day in days:
        candidate = root / day / run_id / "meta.json"
        if candidate.exists():
            return candidate
    return None


def _vitals_reading(session_id: str | None) -> dict | None:
    """The raw co-session-health recording for `session_id`, or None.

    THE ONE READER, TWO CALLERS. `vitals()` below builds the full status-area
    block from this file; `ctx_pct()` further down reads the same file for one
    field, at brief-mode cost. Both used to compute the sanitised-key path
    independently — a second copy of the same charset rule and the same "no
    key, no reading" answer is exactly the shape that let the hook-log root and
    its reader disagree in agent-platform#421.
    """
    key = "".join(_SESSION_ID_RE.findall(str(session_id or "")))[:64]
    if not key:
        return None
    return _read_json(VITALS_DIR / f"vitals-{key}.json")


def vitals(session_id: str | None, claude_pid: int | None,
           cwd: str | None) -> dict:
    """The status area's facts, for a frame that is not the terminal."""
    out: dict = {
        "model": None, "ctx_tokens": None, "ctx_window": None, "ctx_pct": None,
        "tier": None, "sub": None, "age": None, "stale": None, "pr": None,
    }
    out.update(live_agents(cwd))
    out["remote_control"] = remote_control(claude_pid)

    reading = _vitals_reading(session_id)
    if reading is None:
        # No reading yet is the ordinary case for a session that has not taken a
        # turn since this shipped, so it is not a `reason` — the fields are None
        # and the frame shows what it has.
        return out

    for field in ("model", "ctx_tokens", "ctx_window", "ctx_pct", "tier", "sub"):
        value = reading.get(field)
        if value is not None:
            out[field] = value
    # The PR is a small object, not a scalar; copied only when it is one, so a
    # malformed reading leaves the field None rather than half-set.
    if isinstance(reading.get("pr"), dict):
        out["pr"] = reading["pr"]
    written = reading.get("written_at")
    if isinstance(written, (int, float)):
        age = datetime.now(timezone.utc).timestamp() - written
        out["age"] = age
        out["stale"] = age > VITALS_STALE_SECONDS
    return out


# --- ctx_pct and rollover: top-level, carried in BRIEF too (ADR-0069) --------
# `session-agent states` is the rail's batched reader, polled every ~4s, and it
# has never carried a context number or a rollover zone — both lived only in
# `vitals`, which `brief` skips because it costs a per-session health file and
# a bridge-record read for a status area the rail draws none of. A context
# number and a rollover zone are exactly what the rail DOES want to draw, so
# unlike the rest of the status area, both ride at the top level regardless of
# `brief` — one small file read apiece, the same cost class `working_on`
# already pays.
#
# NEITHER BELONGS IN `_SIGNIFICANT`. A percentage moves on nearly every poll
# the way `vitals`' own `age` does, and `rollover`'s `since`/`calls_seeking`
# move the same way while the session keeps working — folding either into the
# follower's diff would make ordinary progress look like a state transition.
def ctx_pct(session_id: str | None) -> float | None:
    """The context-window percentage from the session's last status-line
    render, or None — no reading yet, an unparseable one, or a `ctx_pct` of the
    wrong shape. Unlike `rollover_policy.read_ctx_pct`, this does not treat a
    stale reading as absent: it is quoting the status line's own field, the
    same way `vitals()` does, and staleness is `vitals()`'s `age`/`stale` pair
    to report, not a second staleness rule invented here.
    """
    reading = _vitals_reading(session_id)
    if reading is None:
        return None
    pct = reading.get("ctx_pct")
    return pct if isinstance(pct, (int, float)) else None


_ROLLOVER_DEFAULT: dict = {
    "state": "none", "reason": None, "since": None, "calls_seeking": None,
    "cap": None, "policy": None,
    # Slice 3: named only while `state` is `vetoed` — see `rollover_policy._vetoed_until`.
    "vetoed_until": None,
    # Slice 5: named only while `state` is `rolling` — `bin/co-rollover`'s
    # `rollover_policy.mark_rolling`. `successor_window` is what lets Deck draw
    # a rollover as one event instead of an unexplained second window
    # (`WindowLeaf.swift:37`, epic comment 2399).
    "predecessor": None, "successor": None, "successor_window": None,
    # Slice 6: named only while `state` is `parked`. THE RAIL WAS GUESSING
    # WITHOUT THESE. Deck's hover text said "an issue is waiting on you" for
    # every parked session, because the contract promised one and this payload
    # carried nothing that could contradict it — `issue_id` was never here
    # either, so the rail could not have named the item even when filing
    # succeeded.
    #
    # `issue_status` says whether there IS an item (`created`/`duplicate`) or
    # why there is not (any `error:`-prefixed status, or `skip` with no token).
    #
    # `issue_product` is the string the producer SUBMITTED, which is not
    # necessarily the bucket the item landed in — so a consumer that needs the
    # bucket reads `product` off the item at `issue_id` rather than filtering on
    # this. The full account belongs to the field's owner,
    # `rollover_policy.DEFAULT_STATE["issue_product"]`, which `mark_parked`
    # writes; this payload only carries it through to Deck, and deliberately
    # does not explain it a second time.
    #
    # The loop below copies only keys present HERE, the same overlay
    # `rollover_policy.read_state` does against its own default — so a field
    # missing from this dict is written, persisted, read back by the policy
    # module, and still invisible to Deck. That is the third place in one
    # change where a downstream filter silently dropped a new field.
    #
    # `handoff_path` is deliberately NOT added: these three answer "is there an
    # item, and which one", which is the question the rail was answering
    # wrongly.
    # Putting a filesystem path into a UI payload is a separate judgment.
    "issue_id": None, "issue_status": None, "issue_product": None,

    # NOT ROLLOVER STATE. It is the harness's `permission_mode` for the SESSION,
    # and it sits in this dict for one reason: the overlay loop below drops
    # every key that is not here, so without it the value the rollover trigger
    # writes is invisible past this point. That is the trap the paragraph above
    # describes, and this field walked into it — `plan` on disk read back as
    # `None` with nothing failing, because an unpopulated field and a dropped
    # one are the same `None`.
    #
    # THE CONTRACT SURFACE IS THE TOP-LEVEL `permission_mode`, which
    # `session_state` lifts from here; read that one.
    #
    # Its presence inside this object is transport, and it is KEPT rather than
    # popped because Deck decodes this object's exact shape and an added key is
    # additive, where a pop would buy one surface on the poll at the cost of
    # making the poll's rollover object differ from the one every other caller
    # of `rollover()` gets. (An earlier version of this comment counted ten
    # callers, nine of them the rollover-action verb, and argued the pop would
    # leave those nine inconsistent. The count is ELEVEN — nine in the
    # rollover-action verb, `session_state` itself, and the handoff-context
    # reader — and the argument was a non-sequitur besides, since popping in
    # `session_state` changes nothing for any other caller. Reviewer on #629
    # caught both; the grep behind the old number matched a call spelling
    # rather than the function.)
    "permission_mode": None,
}

# Deck's `rollover.state` is a CLOSED set — any value outside it draws as
# "state unknown" — and this slice's evaluator speaks a vocabulary Deck does
# not know: `rollover_policy.evaluate()` calls the below-threshold zone `ok`,
# internally, because that module is not the one deciding what Deck's enum
# looks like. This is the one place that translates it to `none`, the closed
# set's name for the same zone, before the states output leaves this file.
# `seeking`, `roll_now`, `vetoed`, `rolling` (Slice 5, `bin/co-rollover`) and
# `parked` (Slice 6, a failed successor spawn — `rollover_policy.mark_parked`)
# already match the closed set and pass through unchanged; the set is
# `none`/`seeking`/`roll_now`/`vetoed`/`rolling`/`parked` as of this slice.
_STATE_TO_CLOSED_SET = {"ok": "none"}


def rollover(session_id: str | None) -> dict:
    """What `tools/rollover-trigger.py` last decided for this session, with a
    LIVE operator override resolved on top — translated to Deck's closed
    `state` set — or `none`, its honest-absence value, for a session with no
    hook firing yet (a mini that predates the hook, or the hook not yet
    registered) that has never had a `rollover-*.json` written.

    THE HOOK DECIDES THE CTX-BASED ZONE; THIS FUNCTION DECIDES WHAT'S REPORTED
    ON TOP OF IT (issue_01M2HTBMRN1W688EV40DCDVXB7). Before this fix, `veto`/
    `seek`/`clear` were invisible here until the session's own next hook
    firing folded them in — `force` looked immediate only because an idle or
    waiting session's `force` pastes `/rollover` directly
    (`rollover_command`'s "sent" outcome) rather than going through this read
    at all. The state file's `natural_state`/`natural_reason` are the zone and
    reason the hook would report with NO override — the only ctx-based
    reading this function has, since it reads no vitals of its own and must
    not invent a fresher one — and `rollover_policy.resolve_override` (the
    SAME function `evaluate()` folds an override through, live) decides what
    an override written since that reading changes about what gets reported.
    With no override at all, that resolution is a byte-for-byte pass-through
    of the hook's own `state`/`reason`, so every session that has never used
    an override reads exactly as it always has.

    A session with no `natural_state` on file (older than this fix, or a
    hand-built fixture) falls back to treating its own `state`/`reason` as
    natural — the honest assumption for a reading nothing has told this
    function to distrust.
    """
    out = dict(_ROLLOVER_DEFAULT)
    key = "".join(_SESSION_ID_RE.findall(str(session_id or "")))[:64]
    if not key:
        return out
    reading = _read_json(VITALS_DIR / f"rollover-{key}.json")
    if reading is None:
        return out
    for field in out:
        if field in reading:
            out[field] = reading[field]

    # `rolling`/`parked` ARE TERMINAL FENCES `evaluate()` never resolves an
    # override against either (see its own short-circuit) — mirrored here so
    # an override armed against a rolling or parked session is left for
    # whatever state comes after it, rather than painting over the fence this
    # read is also relied on to report.
    if reading.get("state") not in ("rolling", "parked"):
        natural_zone = reading.get("natural_state", reading.get("state", "ok"))
        natural_reason = reading.get("natural_reason", reading.get("reason"))
        policy = reading.get("policy") or RP.DEFAULT_POLICY
        override = RP.read_override(session_id, override_dir=VITALS_DIR)
        zone, reason, vetoed_until = RP.resolve_override(
            natural_zone, natural_reason, override, policy)
        if zone != reading.get("state") and override is not None:
            written_at = override.get("written_at")
            if written_at is not None:
                out["since"] = written_at
        out["state"], out["reason"], out["vetoed_until"] = zone, reason, vetoed_until

    out["state"] = _STATE_TO_CLOSED_SET.get(out["state"], out["state"])
    return out


# --- rollover_command: Deck's write side (Slice 3, ADR-0069) ----------------
#
# `session-agent rollover <name> veto|force|seek|clear` is the only writer of
# `rollover_policy`'s override file; the hook (`tools/rollover-trigger.py`) is
# the only writer of the state file `rollover()` above reads. Neither ever
# writes the other's file, so the two can never race (epic
# issue_01M1S2X8ENKYEGSDE3H0REFD3G, Slice 3 build note 1).
#
# THE BOSUN SENDS `/rollover`, DECK NEVER DOES (epic comment 2399). `force` on
# an idle or waiting session has no hook firing to carry the instruction —
# nothing calls PostToolUse/Stop while a session sits at its prompt — so this
# function sends the slash command itself, through the same `session_input`
# path `session-agent send` uses, rather than leaving Deck to paste it in.
def _held_queue_now(meta: dict) -> "dict | None":
    """The held-queue reader, independent of the reported top-level `state`.

    This file's own `held` field (further up, in `session_state`) is gated to
    `state == "working"`, because that is the only state Slice A found a
    message held in. `rollover_command`'s `force` safety check cannot lean on
    that gate: it needs the true answer for whatever state the session is
    ACTUALLY in right now, not only the one case the status area bothers to
    compute it for."""
    recorded = TT.contained_transcript(meta.get("transcript_path"))
    return _read_held_queue(recorded) if recorded is not None else None


def rollover_command(name: str, action: str) -> dict:
    """`veto`, `force`, `seek` or `clear` a session's rollover, by tmux name.

    Returns `{outcome, reason, rollover}` — `outcome` is `"sent"` (force
    actually pasted `/rollover`), `"armed"` (the override is written and will
    take effect on the session's own next hook firing, or immediately for a
    `clear`/`veto`/`seek` that only ever records state), or `"refused"` (with
    `reason` set: the session could not be resolved, or a `force` would have
    landed on top of a live composer draft or a held message). A refused
    `force` changes nothing: it is checked before anything is written, and if
    the send itself fails after the write, the override is put back to
    whatever it was before this call.

    `veto` records the CURRENT natural zone — read fresh from the hook's own
    state file, never recomputed here — so `rollover_policy.evaluate()` can
    tell, on its next firing, whether the session has since crossed past it
    (the operator's ruling (d), honoured in `evaluate` itself, not here).

    `clear` removes the override file AND, if the session is `rolling`
    (Slice 5) or `parked` (Slice 6), that state too — `rolling`'s only manual
    way out, since nothing else ever clears it
    (`rollover_policy.clear_rolling`'s docstring), and `parked`'s way back to
    normal once the operator has resolved the filed issue.
    """
    if action not in ("veto", "force", "seek", "clear"):
        raise ValueError(f"unknown rollover action: {action!r}")

    try:
        pane_id, claude_pid = TT.claude_pane(name)
    except TT.JoinError as e:
        return {"outcome": "refused", "reason": str(e), "rollover": dict(_ROLLOVER_DEFAULT)}

    meta = TT.meta_for_pid(claude_pid)
    if meta is None:
        return {"outcome": "refused",
                "reason": "no run-dir record for this session's claude process",
                "rollover": dict(_ROLLOVER_DEFAULT)}
    session_id = meta.get("session_id")

    if action == "clear":
        RP.clear_override(session_id)
        # Slice 5: `rolling` is a terminal fence with no built-in way out, so
        # `clear` is also the operator's manual escape hatch for it — see
        # `rollover_policy.clear_rolling`'s docstring.
        RP.clear_rolling(session_id)
        # Slice 6: `parked` is terminal the same way (`evaluate()` never
        # moves a session back out of it on its own) — see
        # `rollover_policy.clear_parked`'s docstring.
        RP.clear_parked(session_id)
        return {"outcome": "armed", "reason": None, "rollover": rollover(session_id)}

    if action == "seek":
        RP.write_override(session_id, "seek")
        return {"outcome": "armed", "reason": None, "rollover": rollover(session_id)}

    if action == "veto":
        zone = RP.read_state(session_id).get("natural_state", "ok")
        RP.write_override(session_id, "veto", zone=zone)
        return {"outcome": "armed", "reason": None, "rollover": rollover(session_id)}

    # action == "force"
    if session_state(name).get("state") not in ("idle", "waiting"):
        # A working/blocked/ended/unknown session gets nothing sent right now;
        # the override is armed and the session's own next hook firing picks
        # it up (`rollover_policy.evaluate` reports `roll_now` unconditionally
        # for a `force`, and the hook emits it even in `observe` mode — see
        # `tools/rollover-trigger.py`).
        RP.write_override(session_id, "force")
        return {"outcome": "armed", "reason": None, "rollover": rollover(session_id)}

    # Idle/waiting: every refusal below must write NOTHING. Deck's contract
    # is that `outcome: "refused"` means nothing changed, so the checks run
    # before the override is touched at all — and any override already on
    # disk (a veto or a seek armed earlier) is left exactly as it was.
    draft = SI._read_input_box(pane_id)
    if draft and draft.strip():
        return {"outcome": "refused", "reason": "the composer holds a draft",
                "rollover": rollover(session_id)}

    held = _held_queue_now(meta)
    if held and held.get("messages"):
        return {"outcome": "refused", "reason": "a message is queued and held",
                "rollover": rollover(session_id)}

    # The held-queue check above takes its own time reading `meta`, so the
    # draft read at the top of this branch is stale by the time we are about
    # to send. Re-read right here, immediately before the write and the
    # paste, and refuse if a draft has appeared since — this does not close
    # the window (a keystroke between this read and the paste is still
    # possible; nothing short of a lock on the pane would), but it narrows it
    # from "the whole held-queue check" down to one function call.
    draft = SI._read_input_box(pane_id)
    if draft and draft.strip():
        return {"outcome": "refused", "reason": "the composer holds a draft",
                "rollover": rollover(session_id)}

    # All refusal checks have passed. Write the override before the send —
    # not after — because the paste fires UserPromptSubmit and the hook reads
    # the override on that very firing. Remember what was here before so a
    # send failure can put it back rather than leaving a stray `force` armed.
    prior_override = RP.read_override(session_id)
    RP.write_override(session_id, "force")
    try:
        SI.send_message(name, "/rollover")
    except Exception as e:
        if prior_override is None:
            RP.clear_override(session_id)
        else:
            RP.write_override(session_id, prior_override["kind"],
                               zone=prior_override.get("zone"),
                               now=prior_override.get("written_at"))
        return {"outcome": "refused", "reason": str(e), "rollover": rollover(session_id)}

    return {"outcome": "sent", "reason": None, "rollover": rollover(session_id)}


# --- working_on: what the co says it is doing right now, for Deck's cockpit ---
# The operator, 2026-08-24: give a co a way to keep Deck told what the crew is working on.
# Deck's cockpit lists "what we are working on now, populated by the co"
# (deck/bead/deck §1) as the one panel with no data source at all. `bin/co-
# working-on` is the writer the co runs by hand; this is the reader, following
# `vitals`'s own precedent rather than inventing a second one: a fact recorded
# outside the hook stream, in the SAME cache directory and the SAME
# sanitised-charset key scheme, in a file of its own because this is not
# per-turn status-line data — nothing writes it unless the co decides to.
#
# KEYED BY THE TMUX SESSION NAME, NOT `session_id`. Vitals is keyed by
# `session_id` because `co-session-health` runs INSIDE the session and Claude
# Code hands it that id directly; a command the co runs by hand has no such
# payload to read it from, and the tmux name this function already receives as
# `name` is the identifier Deck already addresses a session by. It is also the
# more useful key here: a `/rollover` or a handoff starts a fresh Claude Code
# process — a new `session_id` — inside the SAME tmux session, and "what is
# this crew doing" is a fact about the crew's session, not about which process
# happens to be running it right now.
#
# CARRIED IN `brief` TOO, UNLIKE `vitals`. `brief` skips vitals because that
# block costs a per-session vitals-file read plus a remote-control file read,
# for facts the rail draws none of. This is ONE small file read keyed on `name`
# alone — no pid, no meta.json, no tmux round trip — so it costs nothing close
# to what `brief` exists to avoid. And "a crew row showing what its session is
# doing" is exactly the fleet-wide point the operator asked for, not a pane-only extra —
# so this does not silently inherit vitals' answer just because the two look
# alike.
#
# WHY THIS LIVES HERE AND NOT THE LIBRARY. Deck reads Chart House state from the
# Library for everything fleet-wide, and this is per-session and
# terminal-adjacent instead — wanted by whoever is watching THIS session, which
# is exactly what `vitals` already is and exactly what rides free on the poll
# Deck already makes. Moving it to the Library would mean a second poll and a
# second client stack for one line of text. That changes the day this needs to
# reach Orient or the team site, which show the fleet rather than one session —
# not before.
#
# STALENESS IS SHORTER THAN VITALS' HOUR, DELIBERATELY, and unmeasured — there
# is no corpus for this the way `VITALS_STALE_SECONDS` had one. A context
# percentage stays true for as long as nothing is typed; a sentence about what
# someone is doing goes wrong the moment the task changes, which happens well
# inside an hour. Twenty minutes is chosen as long enough that a coder mid-task,
# heads-down between tool calls, is not flagged merely for not re-announcing
# itself, and short enough that a forgotten sentence from three hours ago reads
# as stale rather than as current.
WORKING_ON_STALE_SECONDS = 1200

# The text reaches a WKWebView pane in Deck as display content from a co's own
# free-form argument, so it is re-bounded on the way OUT as well as on the way
# in — a hand-edited file or an older writer must not hand this straight
# through unsanitised. Matches `bin/co-working-on`'s own bound; kept as a
# separate constant because that script does not import this module (the same
# self-containment `co-session-health` chose, and for the same reason: this
# reader must not gain a new way to fail just because a display bound moved).
_WORKING_ON_MAX_LEN = 200


# A LEGITIMATE RECORDING IS UNDER 300 BYTES — 200 characters of text plus two
# floats — so this ceiling is generous by an order of magnitude and still far
# under anything that could cost the poll. It exists because the cache
# directory is writable by anything running as this user, including a
# prompt-injected session (the actor `meta.json`'s own comment at the top of
# this module already treats as live), so `co-working-on`'s write-side bound
# proves nothing about what is actually on disk.
#
# THE BLAST RADIUS IS WHY THIS IS A CEILING RATHER THAN A COMMENT. `working_on`
# is read under `brief` too, which is `session_states` — the rail's reader,
# looped over every pinned session on Deck's ~4s poll. An unbounded read here
# means one poisoned file stalls the poll for every session the rail watches,
# not the one whose file it is. `_GATE_TAIL_BYTES` and `_HELD_TAIL_BYTES` bound
# their reads on the same path for the same reason. Found by
# `deck-security-reviewer` on #434.
_WORKING_ON_MAX_BYTES = 4_096


def _read_working_on(path: Path) -> dict | None:
    """One recording, or None, read under a byte ceiling and a depth guard.

    NOT `_read_json`, WHICH IS UNBOUNDED. Two different failures have to be
    refused here and a size cap alone only stops one of them:

    * **Size.** A single bounded `read` of one byte past the ceiling, on a
      handle already open. Over the ceiling means refuse without parsing.

      **OPENED IN BINARY, SO THE CEILING IS THE BYTES IT SAYS IT IS.** A text
      read counts CHARACTERS, so a payload of four-byte astral-plane
      characters would pass a 4096 check having read ~16KB — a cap quietly
      four times looser than the constant promises, which is a documentation
      defect even where it is not a live one. `json.loads` takes bytes, and a
      malformed sequence still raises `UnicodeDecodeError`, a `ValueError`
      subclass the handler below already catches.
    * **Depth.** `json.loads` recurses per nesting level and raises
      `RecursionError`, which is neither `OSError` nor `ValueError` — so it
      would escape `_read_json`'s own handler and take down the `state` call
      that `working_on` is only a field of. A few kilobytes of `[[[[…` is
      thousands of levels deep, well past the interpreter's limit, so the
      ceiling above does NOT make this unreachable and the catch is load-bearing
      rather than defensive.

    ONE FILESYSTEM ACCESS, NOT TWO, AND THAT IS THE POINT OF THE SHAPE. The
    first version checked `path.stat().st_size` and then opened the path again
    — two lookups with a window between them, into a directory writable by
    anything running as this user, which is the very actor the ceiling exists
    for. A rename swapping a small placeholder for a large payload inside that
    window defeats the check entirely, and the attacker can retry as fast as it
    likes against a poll that comes round every few seconds. Reading from a
    handle already open removes the second lookup, so there is no window to
    win rather than a narrower one. Found by `deck-security-reviewer` on #434.

    `MemoryError` is caught for the same reason `RecursionError` is: this
    handler decides whether one unreadable file can take down a `state` call
    for every session the rail watches, and the exception classes an
    adversarial document can reach here have already proved non-obvious once.
    """
    try:
        with open(path, "rb") as fh:
            raw = fh.read(_WORKING_ON_MAX_BYTES + 1)
        if len(raw) > _WORKING_ON_MAX_BYTES:
            return None
        value = json.loads(raw)
    except (OSError, ValueError, RecursionError, MemoryError):
        return None
    return value if isinstance(value, dict) else None


def _working_on_path(key: str) -> Path:
    return VITALS_DIR / f"working-{key}.json"


def working_on(name: str) -> dict:
    """What the co last said it is working on, or the honest absence of that.

    Never raises: a missing, empty, malformed or unreadable file costs this
    field, never the state call. Always returns the same three keys so a
    consumer can render one shape regardless of which case it got.
    """
    out: dict = {"text": None, "age": None, "stale": None, "since": None}
    key = CK.session_key(name)
    if not key:
        return out
    reading = _read_working_on(_working_on_path(key))
    if reading is None:
        return out
    text = reading.get("text")
    if not isinstance(text, str) or not text.strip():
        return out
    out["text"] = sanitise(text, max_len=_WORKING_ON_MAX_LEN)
    now = datetime.now(timezone.utc).timestamp()
    written = reading.get("written_at")
    if isinstance(written, (int, float)):
        age = now - written
        out["age"] = age
        out["stale"] = age > WORKING_ON_STALE_SECONDS
    # `since` IS SECONDS OF DURATION, NOT A CLOCK TIME, and the difference is
    # what keeps it correct on a client in another timezone. A consumer that
    # wants "since 14:05" subtracts this from its own now and formats in the
    # reader's locale; handing over a formatted time here would bake this
    # host's timezone into a string Deck renders on a laptop that may not
    # share it. The same reason `age` is seconds rather than a timestamp.
    #
    # ABSENT rather than falling back to `written_at` when the recording
    # carries no `started_at`. A file written by an older version of the
    # writer has no start time, and inferring one from the last write would
    # report a duration of nearly zero for work that may be hours old — the
    # precise-looking wrong answer this field exists to avoid. A consumer
    # draws no duration when this is absent.
    started = reading.get("started_at")
    if isinstance(started, (int, float)):
        out["since"] = now - started
    return out


def slate_counts(name: str) -> dict:
    """`{"asks_open": n, "asks_answered": n}` for this crew's Slate
    (platform/decisions/0091, CQ-358) — keyed by the SAME tmux-session-name
    reduction `co-ask`/`slate.py` key by (`crew_key.session_key`), not the
    Claude session id `ctx_pct`/`rollover` use below, because the Slate
    survives a rollover and a Claude session id does not.

    A missing board is `slate.ask_counts`'s own 0/0, nothing special here. An
    UNREADABLE board must never fail the rail's poll (the ADR's own rule), so
    a `SlateCorruptError` is caught here and reported as `slate_error`
    instead, with the same 0/0 a crew that posted nothing would show.
    """
    key = CK.session_key(name)
    try:
        return SLT.ask_counts(key)
    except SLT.SlateCorruptError as e:
        return {"asks_open": 0, "asks_answered": 0, "slate_error": str(e)}


# --- working_on_derived: the same fact, assembled rather than typed ----------
# issue_01M15YAED1GD3X6D9QKPRFZFEK (the operator, priority confirmed 2026-08-29):
# `working_on` above goes stale because it is a separate thing for a co to
# remember, competing with the work itself, and it loses every time. This is
# the platform assembling the same fact from events it already recorded —
# `agentctl spawn --task`, a dispatch's own `--pr`, and this session's recent
# prompts — deterministically, with no model call. Same inputs, same output.
#
# NEVER OVERWRITES `working_on`. Written to its own field, `derived_from` and
# `identity_kind` naming provenance separately (see below), so an authored
# line always wins and a reader can always tell which kind it is looking at.
#
# `started_at` IS KEYED TO A DERIVED WORK IDENTITY, NOT TO THE TEXT CHANGING.
# Settled across three sessions on the parent issue (comment 1211) after the
# obvious alternative — move the clock when the rendered text differs — was
# tried and measured to fail on its own motivating case: one PR taken through
# five review rounds emits five different dispatch task strings, so that rule
# moves the clock five times across ninety minutes of continuous work on one
# thing, and the panel reads under twenty minutes. Keying on a discrete
# identity — (repo, pr), else an issue id, else the branch — makes "did the
# work change" a lookup instead of a judgement, and that session is one row,
# changing once.
#
# THE ONE-SHOT PROCESS MODEL IS WHY THIS WRITES A CACHE FILE AT ALL. Deck's
# rail and its open-pane poll each invoke `session-agent` as a short-lived
# process per read (`session-agent state <name>` — "one JSON object, then
# exit"), so nothing in this module's own memory survives between polls.
# Continuity — "did the identity change since last time" — has to live on
# disk, in the SAME cache directory `working_on`'s own file already uses, so
# this is a read-modify-write inside what is otherwise a pure reader, exactly
# the way `co-working-on`'s own file already carries `started_at` forward
# across separate invocations of that command.
WORKING_ON_DERIVED_STALE_SECONDS = WORKING_ON_STALE_SECONDS


def _derived_working_on_path(key: str) -> Path:
    return VITALS_DIR / f"working-derived-{key}.json"


def _write_derived_cache(path: Path, record: dict) -> None:
    """Best-effort atomic write, mirroring `co-working-on`'s own: a pid-unique
    temp file plus `os.replace`, so a reader never sees a half-written file.

    Best-effort because this runs inside a READER — a write failure here
    costs the NEXT poll's continuity, never this one, since the record just
    computed is returned regardless of whether it could be persisted.
    """
    try:
        VITALS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}")
        try:
            tmp.unlink()          # a same-pid crash could have left one behind
        except OSError:
            pass
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            # Cap the persisted text to what the bounded reader will accept.
            # `_read_working_on` refuses a file past its ceiling and returns
            # None, so an uncapped long `--task` would make every subsequent
            # poll read `previous` as empty — and the same-identity-keeps-one-
            # clock guarantee would break silently, on exactly the long-running
            # work it exists to measure.
            capped = dict(record)
            text = capped.get("text")
            if isinstance(text, str):
                capped["text"] = text[:_WORKING_ON_MAX_LEN]
            fh.write(json.dumps(capped))
        os.replace(tmp, path)
    except OSError:
        pass


def _crew_may_share(cwd: str | None) -> bool:
    """Whether this session's crew may have its derived line leave this
    session for the shared payload `session_state()` returns.

    FAIL CLOSED ON EVERY UNCERTAINTY (issue_01M15YAED1GD3X6D9QKPRFZFEK,
    constraint 5), matching `_hook_log_root`'s own posture on the identical
    question one field over: `UNKNOWN` is exactly as unable to tell "is this
    crew private" as a confirmed private crew, so both withhold. Gating a
    single field was already shown not to be enough one PR over (#464's
    `_crew_is_shareable`: repo + run_id + started_at + model together are a
    live roster row for a black-box crew even with task text stripped) — so
    this is the ONE gate a private crew's derived line passes through, and
    everything downstream of a `False` here is simply never computed.
    """
    home = private_home_for(cwd)
    return not (home is UNKNOWN or home)


def _git_branch(cwd: str | None) -> str | None:
    """The branch checked out at `cwd`, or None (no branch — detached HEAD,
    not a git directory, or unreadable).

    READ DIRECTLY, NOT SHELLED OUT. This runs on session-agent's poll path —
    a batched rail read touches it once per pinned session — and a plain file
    read costs less than a `git` subprocess and carries no PATH or cwd
    resolution surface to get wrong.
    HANDLES A WORKTREE'S `.git`, which is a FILE ("gitdir: <path>") rather
    than a directory — the ordinary case here, since dispatched and
    interactive work both run out of `<root>/worktrees/*`.
    A detached HEAD returns None rather than the raw sha: a sha is not a
    stable identity across polls the way a branch name is, and the third
    identity tier is literally "the branch" (comment 1211), not "a commit".
    """
    if not isinstance(cwd, str) or not cwd:
        return None
    try:
        git_path = Path(cwd) / ".git"
        if git_path.is_file():
            line = git_path.read_text(encoding="utf-8", errors="replace").strip()
            if not line.startswith("gitdir:"):
                return None
            git_dir = Path(line.split(":", 1)[1].strip())
        elif git_path.is_dir():
            git_dir = git_path
        else:
            return None
        head = (git_dir / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
        if not head.startswith("ref:"):
            return None
        ref = head.split(":", 1)[1].strip()
        return ref.rsplit("/", 1)[-1] if ref else None
    except OSError:
        return None


def _meta_for_run(run: dict) -> dict | None:
    """The `meta.json` a live `active.json` entry names, or None.

    ARITHMETIC, NOT A GLOB — the same technique #464's `_run_meta_path` uses
    for the identical reason: this runs on the rail's batched poll, once per
    live run per pinned session, and a directory listing there is the exact
    cost class `brief` exists to avoid reintroducing. The run-dir's day key is
    LOCAL time (`telemetry.platform_day`, ARCH-156) while a registry entry's
    `started_at` is UTC (`telemetry.now_iso`), so the day is computed by
    converting one clock to the other, with the neighbouring day tried either
    side in case the spawn landed on the other side of local midnight from
    where the registry's UTC stamp implies.

    `run_id` is validated against the same charset every other untrusted
    identifier in this file is checked against before becoming a path
    component (`_SESSION_ID_RE`) — this one comes out of `active.json`,
    machine-wide state this function does not own.
    """
    run_id = run.get("run_id")
    if not isinstance(run_id, str) or not _SESSION_ID_RE.fullmatch(run_id):
        return None
    started_at = run.get("started_at")
    days: list[str] = []
    if isinstance(started_at, str):
        try:
            when = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc).astimezone()
            days = [(when + timedelta(days=d)).strftime("%Y-%m-%d")
                    for d in (0, -1, 1)]
        except (ValueError, OverflowError):
            days = []
    if not days:
        days = [datetime.now().astimezone().strftime("%Y-%m-%d")]
    for day in days:
        candidate = AGENT_RUNS_ROOT / day / run_id / "meta.json"
        if candidate.is_file():
            return _read_json(candidate)
    return None


def _dispatched_by(session_id: str) -> list[dict]:
    """This co session's own live dispatches, freshest first, as `meta.json`
    records.

    `active.json`'s own entries carry no `co_session` (`registry.add_run`'s
    entry shape is `run_id, agent, task, started_at, repo, worktree, pid,
    pid_started_at, parent_run`) — only a run's OWN `meta.json` does
    (`telemetry.write_meta`), and only for a TOP-LEVEL interactive dispatch;
    a nested one carries `parent_run` instead. So recovering "this session
    dispatched run X" means opening each live run's `meta.json` and matching
    on that field — bounded by the concurrency ceiling (small), not by how
    many sessions are polling.

    Each returned dict's `run_id` is the one the `active.json` entry named
    (already charset-validated by `_meta_for_run` before it was used to build
    the path) — never `meta.json`'s own `run_id` field. A run can write its
    own `meta.json` with Bash (#457 round 2 showed exactly that for
    `pid_started_at`), and this output feeds a rollover handoff a successor
    session later runs `agentctl wait <ids>` against, so a forged `run_id`
    string there is an injection into that later command line. When the two
    disagree the row is dropped entirely and a one-line warning naming the
    trusted id goes to stderr, rather than trusting either value picked from
    a file the run itself controls.
    """
    root = _read_json(ACTIVE_JSON)
    runs = (root or {}).get("runs")
    if not isinstance(runs, list):
        return []
    out = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        meta = _meta_for_run(run)
        if not isinstance(meta, dict):
            continue
        if meta.get("co_session") != session_id:
            continue
        # A dispatch INTO a black box is that crew's work wherever it was made
        # FROM. `_crew_may_share` gates on the READING session's own cwd, which
        # answers "is this crew private" and not "is this RUN private" — and a
        # cross-repo session (the Keel is the ordinary case) sits outside every
        # private home while dispatching straight into one. Filtering here rather
        # than in `_fresh_identity`/`_fresh_text` keeps the decision single: a
        # private run never reaches the derivation machinery at all, so neither
        # its task text nor its `repo#pr` identity can contribute.
        #
        # BOTH SIGNALS `projects.dispatch_is_private` checks, not `repo` alone —
        # `resolve_agent()` (bin/agentctl) honours `--agents-dir` before it ever
        # looks at `repo`, so `--on constance --agents-dir
        # <root>/CFO/.claude/agents` loads a CFO agent while `repo` stays
        # "constance"; a repo-only check misses it (security-reviewer,
        # run-089d9dca, round 2 of this PR). `meta["agent_source"]` (written by
        # `write_meta` since agent-platform#465) is the file `resolve_agent()`
        # actually read, so it can be checked against every private crew's
        # `home_path()` here too. THE SAME COMPARISON
        # `bin/agentctl:_is_private_dispatch` uses — lifted into
        # `projects.dispatch_is_private` so the two cannot drift apart the way
        # the repo-only check drifted from it in the first place. A run
        # dispatched before this field existed carries no `agent_source`;
        # `dispatch_is_private` then falls back to the repo-only half, same as
        # this filter's first version.
        repo = meta.get("repo")
        try:
            private = projects.dispatch_is_private(repo, meta.get("agent_source"))
        except Exception:
            # The registry (`config/projects.json`) is unreadable or corrupt.
            # Cannot tell whether THIS run is a black box's, so it is excluded
            # — fail closed, matching `_crew_may_share`'s posture on the
            # identical uncertainty one gate over (security-reviewer,
            # run-089d9dca). Scoped to this one run rather than the whole
            # session: a registry outage degrades this dispatch to
            # "unobserved this poll", the same shape `_fresh_derivation`
            # already treats as nothing-to-report, never a crash and never a
            # leak.
            continue
        if private:
            continue
        located_run_id = run.get("run_id")
        if meta.get("run_id") != located_run_id:
            print(
                f"agentctl: run {located_run_id!r}'s meta.json run_id does not "
                "match the id it was located by — dropping from dispatch list",
                file=sys.stderr,
            )
            continue
        # Overwrite rather than trust the field already checked equal above:
        # every caller reading `run_id` off this dict — including a future one
        # that doesn't know about the check three lines up — gets the id this
        # function located the run by, not whatever `meta.json` said.
        out.append({**meta, "run_id": located_run_id})
    out.sort(key=lambda m: str(m.get("started_at") or ""), reverse=True)
    return out


def _recent_prompt(events: "list[dict] | None") -> str | None:
    """This session's most recently submitted prompt, or None.

    Reads the SAME `events` list `session_state()` already loaded from the
    hook log for its own state fold — no second read. A private or
    privacy-unknown crew's hook log already has this field redacted at write
    time (`session-event-log.py`'s `_strip_content`) or routed to the crew's
    own directory, so by the time `events` reaches here it is exactly as safe
    to read as anything else `session_state()` already does with it; the
    separate `_crew_may_share` gate below is what stops it leaving THIS
    session for the shared payload.
    """
    if not events:
        return None
    for r in reversed(events):
        if r.get("event") != "UserPromptSubmit":
            continue
        payload = r.get("payload")
        if not isinstance(payload, dict):
            continue
        prompt = payload.get("prompt")
        if isinstance(prompt, str) and prompt.strip():
            return prompt
    return None


def _fresh_identity(dispatches: list[dict], branch: str | None):
    """`(identity_kind, identity_key)` from the fixed precedence order
    (issue_01M15YAED1GD3X6D9QKPRFZFEK comment 1211): `(repo, pr)`, else an
    `issue_checkout`/`issue_claim` id, else the branch. `(None, None)` when
    none of them resolve.

    THE ISSUE TIER IS STRUCTURALLY HERE AND NEVER FIRES, and that is a known
    gap rather than an oversight — see the PR this shipped in. No local
    recorder exists for `issue_checkout`/`issue_claim` today: the ops MCP
    tools behind them are server-side, `issue_client.py` writes nothing
    locally, and the one PostToolUse hook already registered for those tool
    names (`tools/nudge-working-on.sh`) reads the response transiently, to
    print a nudge, and persists nothing. That script's own history is why
    this was not extended speculatively: its title-extraction alone took four
    review rounds to make safe against a hostile issue title, and adding a
    second, unverified extraction (the issue id) without a live payload to
    test it against risks the exact class of defect that history is a record
    of. The slot is left ready for whichever PR builds that recorder.
    """
    for meta in dispatches:
        repo, pr = meta.get("repo"), meta.get("pr")
        if isinstance(repo, str) and repo and isinstance(pr, int):
            return "pr", f"{repo}#{pr}"
    if branch:
        return "branch", branch
    return None, None


def _fresh_text(dispatches: list[dict], prompt: str | None):
    """`(text, derived_from)` from whichever recorded source is most
    informative right now — independent of which identity (if any) resolved,
    since text "may be reassembled on every event" while identity moves only
    when the work genuinely changes (comment 1211).

    A dispatch this session made outranks its own prompt: the prompt says
    what was ASKED, the dispatch says what the session is actually DOING
    about it, which is the more useful answer to "what is this working on".
    """
    if dispatches:
        task = dispatches[0].get("task")
        if isinstance(task, str) and task.strip():
            return task, "agentctl spawn --task"
    if isinstance(prompt, str) and prompt.strip():
        return prompt, "UserPromptSubmit"
    return None, None


def _fresh_derivation(cwd: str | None, session_id: str | None,
                       events: "list[dict] | None"):
    """One observation of `(identity_kind, identity_key, text, derived_from)`
    from currently-observable recorded events, or None when nothing at all is
    observable this poll (constraint 3: that must not move `started_at`).

    A BARE BRANCH, WITH NO TEXT BEHIND IT, DOES NOT COUNT AS AN OBSERVATION.
    `cwd` almost always sits inside SOME git checkout, so the branch tier
    resolves on nearly every poll — including the instant right after a
    dispatch this session made has exited `active.json` and nothing has
    replaced it yet. Treating that as a fresh identity would reset the clock
    onto a fact with no content behind it and, worse, blank the text a moment
    ago had real content — exactly the "session between pieces of work"
    case constraint 4 exists to protect, reached through the weakest tier
    instead of the strongest. So branch-with-no-text is folded into "nothing
    observed", the same as no signal at all; branch only counts as a real
    observation when it arrives WITH text (a dispatch that carried no `--pr`,
    or this session's own recent prompt).
    """
    dispatches = _dispatched_by(session_id) if session_id else []
    branch = _git_branch(cwd)
    prompt = _recent_prompt(events)
    kind, key = _fresh_identity(dispatches, branch)
    text, derived_from = _fresh_text(dispatches, prompt)
    if kind is None and text is None:
        return None
    if kind == "branch" and text is None:
        return None
    return kind, key, text, derived_from


def working_on_derived(name: str, cwd: "str | None" = None,
                        session_id: "str | None" = None,
                        events: "list[dict] | None" = None) -> dict:
    """The session's working-on line, reduced deterministically from recorded
    platform events — no model call, same inputs give the same output. See
    `working_on`'s own docstring for the field it never touches.

    Never raises: every field defaults to absent, matching `working_on`'s own
    contract, so a consumer reading this on a session where nothing could be
    established gets the honest "unknown" shape rather than a KeyError.

    `cwd`/`session_id`/`events` are `None` on `session_state()`'s early-return
    paths (no run-dir record yet, a dead pane) and in the hand-built
    fallbacks `session_states()`/`follow_states()` seed on a read failure.

    A `None` `cwd` specifically fails the privacy gate closed (`_crew_may_share`
    cannot tell a public session from a private one with no `cwd` to check,
    the same posture `_hook_log_root` already takes) and serves absent, full
    stop — it does not fall through to the cache. A resolvable `cwd` whose
    `session_id`/`events` are still `None` behaves differently: that is not
    "no identity has ever derived" (constraint 4), it is a session briefly
    between pieces of work, so a FRESH observation is simply skipped and
    whatever the cache already holds carries forward unchanged rather than
    being erased.
    """
    out: dict = {"text": None, "age": None, "stale": None, "since": None,
                 "derived_from": None, "identity_kind": None}
    key = CK.session_key(name)
    if not key:
        return out
    if not _crew_may_share(cwd):
        return out

    path = _derived_working_on_path(key)
    previous = _read_working_on(path)   # same bounded/safe reader `working_on` uses
    if not isinstance(previous, dict):
        previous = {}

    now = datetime.now(timezone.utc).timestamp()
    fresh = _fresh_derivation(cwd, session_id, events) if (cwd or session_id) else None

    if fresh is None:
        # NOTHING OBSERVABLE THIS POLL. The whole previous record carries
        # forward untouched — not just `started_at` but `derived_at` too, or
        # a session silent for hours would look freshly confirmed every time
        # something merely polled it (constraint 3: fail toward staleness).
        record = previous
    else:
        kind, ident_key, text, derived_from = fresh
        prev_identity = (previous.get("identity_kind"), previous.get("identity_key"))
        if ident_key is not None and (kind, ident_key) == prev_identity:
            # SAME IDENTITY: the text may be reassembled: the clock does not move.
            started_at = previous.get("started_at")
        elif ident_key is not None:
            # A DIFFERENT (or FIRST) IDENTITY: this is new work.
            started_at = now
        else:
            # No identity resolves this poll (e.g. text came from a prompt
            # with no dispatch or branch behind it). Constraint 4: a session
            # between pieces of work keeps its existing `started_at` — an
            # identity-less poll must not erase one that was already there,
            # and must not invent one that was never there either.
            started_at = previous.get("started_at")
        record = {
            "text": text,
            "derived_at": now,
            "derived_from": derived_from,
            "identity_kind": kind,
            "identity_key": ident_key,
            "started_at": started_at,
        }

    _write_derived_cache(path, record)

    text = record.get("text")
    if not isinstance(text, str) or not text.strip():
        return out
    out["text"] = sanitise(text, max_len=_WORKING_ON_MAX_LEN)
    derived_from = record.get("derived_from")
    out["derived_from"] = derived_from if isinstance(derived_from, str) else None
    identity_kind = record.get("identity_kind")
    out["identity_kind"] = identity_kind if isinstance(identity_kind, str) else None
    derived_at = record.get("derived_at")
    if isinstance(derived_at, (int, float)):
        age = now - derived_at
        out["age"] = age
        out["stale"] = age > WORKING_ON_DERIVED_STALE_SECONDS
    started_at = record.get("started_at")
    if isinstance(started_at, (int, float)):
        out["since"] = now - started_at
    return out


def state_from_events(events: list[dict]) -> dict:
    """Fold a session's hook events into a state. Pure, so it is testable.

    An unrecognised event leaves the state alone. An event with no recognisable
    shape at all is counted, so `state_unknown` can be reported rather than
    guessed at.
    """
    state = "unknown"
    gate = None
    gate_ts = None
    last_event = None
    last_ts = None
    unparsed = 0
    # The `tool_use_id` from the most recent `PreToolUse` event. Folded forward
    # onto the `PermissionRequest` that pairs with it (same `tool_name`,
    # immediately following), because `PermissionRequest` carries no id of its
    # own — measured 2026-07-31, 31 of 31 records.
    _pending_id: str | None = None
    _pending_tool: str | None = None

    for r in events:
        name = r.get("event")
        ts = r.get("ts")
        if not isinstance(name, str):
            unparsed += 1
            continue
        last_event, last_ts = name, ts or last_ts
        raw = r.get("payload")
        payload = raw if isinstance(raw, dict) else {}

        # Stash the id from PreToolUse so the paired PermissionRequest can carry
        # it. The stash is cleared on any non-blocked transition below, so a
        # PreToolUse whose PermissionRequest never arrives cannot leak onto a
        # later gate.
        if name == "PreToolUse" and isinstance(raw, dict):
            uid = raw.get("tool_use_id")
            _pending_id = uid if isinstance(uid, str) else None
            _pending_tool = raw.get("tool_name")

        nxt = _state_for(name, payload)
        if nxt is None:
            continue
        state = nxt
        # The gate is cleared by ANY later transition, so a stale question can
        # never be shown as live after the session moved on. A Notification
        # pairs with a PermissionRequest and must not clear the gate that
        # request just set, so it only carries one forward.
        #
        # `PreToolUse` is excluded from the pending-id clearing: it is the event
        # that SETS the stash, so clearing it in the same pass would erase what
        # was just written. Clearing happens on any OTHER non-blocked event that
        # falls between the PreToolUse and its PermissionRequest — that is the
        # stale-id case this guard exists to prevent.
        if nxt != "blocked":
            gate = None
            gate_ts = None
            if name != "PreToolUse":
                _pending_id = None
                _pending_tool = None
        elif name == "PermissionRequest":
            gate_ts = ts or gate_ts
            # Fold the pending id forward only when the tool names match. A
            # PreToolUse for Bash followed by a PermissionRequest for
            # AskUserQuestion is not a pair, and the wrong id is worse than
            # none — the whole point of the field is to bind a keystroke to the
            # exact gate it was aimed at.
            gate_id: str | None = None
            if (_pending_id is not None
                    and isinstance(raw, dict)
                    and payload.get("tool_name") == _pending_tool):
                gate_id = _pending_id
                _pending_id = None
                _pending_tool = None
            # A payload with nothing renderable leaves the gate None while the
            # state stays blocked. That pair is the honest signal — "stopped for
            # you, and I cannot show you what it wants" — and a pane's answer to
            # it is the raw terminal. Manufacturing an empty dialog would be the
            # lie.
            gate = _gate_from(payload, gate_id=gate_id) if isinstance(raw, dict) else None

    return {
        "state": state,
        "gate": gate,
        "gate_ts": gate_ts,
        "last_event": last_event,
        "last_event_age": _age(last_ts),
        "unparsed_records": unparsed,
    }


def session_states(names: list[str], days: int = SEARCH_DAYS) -> dict:
    """A brief state for each of several sessions, keyed by name, in one call.

    THE RAIL'S READER. Deck's rail draws every pinned session at once and needs
    each one's liveness and phase to decorate the row and grey a dead one. One
    call for all of them is one ssh round trip rather than one per row — the same
    reason `session-agent` exists at all — and each is `brief`, so it skips the
    vitals-file and remote-control reads a row does not show. The full
    `session_state` stays what the open pane polls.

    A name that fails outright still gets an entry, `state: "unknown"`, rather
    than being dropped — a missing row would read as a session the rail forgot,
    not one that is down.

    THAT ENTRY IS HAND-BUILT, WHICH IS THE TRAP. It does not come from
    `session_state`, so none of the seeding there reaches it, and a field added
    to the state object lands everywhere EXCEPT the one path a consumer is least
    likely to have tested. `agents` was absent here for exactly that reason —
    the rail is the caller of this function, so the always-present guarantee the
    contract makes was broken against its primary consumer. Anything added to
    the seed in `session_state` belongs here too.

    ONE `list-sessions` FOR THE WHOLE BATCH. `session_state` resolves a tmux
    session name via `TT.claude_pane`, which now issues its own `tmux
    list-sessions` to resolve the name to an exact id (PR #480). Left alone,
    that is N identical `list-sessions` calls for N rail rows every refresh.
    Resolving the table once here and handing it to every row removes the
    duplication without touching the exact-match fix itself.
    """
    # Outside the per-name try/except below: relies on TT.run() never raising
    # (it swallows subprocess failures), so a raise here would sink the whole
    # batch instead of degrading one name.
    table = TT.session_table()
    out = {}
    for name in names:
        try:
            out[name] = session_state(name, days=days, brief=True, table=table)
        except Exception as e:  # noqa: BLE001 — one bad name must not sink the batch
            out[name] = {"session": name, "state": "unknown", "alive": False,
                         "reason": str(e), "agents": [], "agent_runs": [], "agents_fleet_wide": False,
                         "working_on": working_on(name),
                         "working_on_derived": working_on_derived(name),
                         # No `session_id` reached this branch (the exception
                         # fired before or during resolution), so these cannot
                         # be looked up — absent-shaped defaults, matching the
                         # early seed in `session_state` rather than a KeyError.
                         "ctx_pct": None, "rollover": dict(_ROLLOVER_DEFAULT),
                         "permission_mode": None,
                         # The Slate is keyed by `name` alone (see
                         # `slate_counts`), so it resolves regardless of
                         # whatever broke the rest of this read.
                         **slate_counts(name)}
    return out


def session_state(name: str, days: int = SEARCH_DAYS, brief: bool = False,
                   table: dict | None = None) -> dict:
    """The full state of the co session in a tmux session, as one object.

    Degrades rather than lying: anything it cannot establish comes back as
    `state: "unknown"` with a reason, and Deck's answer to unknown is to offer
    the raw terminal, which cannot be wrong.

    `brief` skips the vitals block — the rail wants liveness and phase for many
    sessions at once and draws none of the status area, so it does not pay for
    the per-session file reads vitals costs. The open pane calls this without
    `brief` and gets everything.

    `table`, when given, is a pre-fetched `TT.session_table()` — `session_states()`
    resolves it once for its whole batch and passes it down here, so N sessions
    share one `tmux list-sessions` instead of each triggering its own. A direct
    caller (the open pane, polling one session) leaves it unset and `claude_pane`
    resolves its own, which is what a single lookup wants.
    """
    # `agents` seeded here rather than filled in later, and that is load-bearing:
    # every early return below would otherwise emit a state object with the key
    # ABSENT, and absent is not the same answer as empty. A consumer has to be
    # able to tell "nothing dispatched" from "this mini predates the field", and
    # a Python caller reading `state["agents"]` on an early-return session gets a
    # KeyError instead of either. Caught in review on the `meta is None` path.
    #
    # Empty is also the honest seed. The scoping that keeps one product's co from
    # reporting another product's coder as its own needs a `cwd`, and every early
    # return is a session whose `cwd` could not be established — so there is no
    # scoped answer to give, and the unscoped one is the bug that scoping fixed.
    # `working_on` is seeded here too, and for the same reason `agents` is:
    # every early return below is a real session Deck still wants a cockpit
    # line for, and it costs one small file read keyed on `name` alone — no
    # pid, no meta.json, nothing this function has not resolved yet.
    out: dict = {
        "session": name, "state": "unknown", "reason": None, "gate": None,
        "alive": None, "progressing": None, "transcript_age": None,
        "agents": [], "agent_runs": [], "agents_fleet_wide": False,
        "working_on": working_on(name),
        # `cwd` IS NOT RESOLVED YET on this early seed, so the privacy gate
        # cannot tell a public session from a private one and fails closed to
        # absent — the same posture `_hook_log_root` already takes on an
        # unresolved `cwd`. Refreshed with a real observation, and a real
        # privacy answer, once `cwd` is known, further down.
        "working_on_derived": working_on_derived(name),
        # `ctx_pct`/`rollover` are keyed by the Claude Code SESSION ID, unlike
        # `working_on` above (keyed by the tmux NAME, known immediately) — so
        # they cannot be resolved until `session_id` is, a few lines below.
        # Seeded absent here for the same reason `agents` is: every early
        # return past this point is a real session whose consumer must not get
        # a `KeyError` for a key this function does resolve on other paths.
        "ctx_pct": None,
        "rollover": dict(_ROLLOVER_DEFAULT),
        # Seeded for exactly the reason the comment above gives, and the reason
        # is sharper here: `session_states` already seeds this key on its own
        # unresolvable path, so without this line the SAME consumer reading the
        # SAME poll gets the key from one absent-shaped row and a `KeyError`
        # from the other.
        "permission_mode": None,
    }
    # `asks_open`/`asks_answered` (platform/decisions/0091, CQ-358), seeded on
    # every path for the same reason `working_on` is: the Slate is keyed by
    # the tmux NAME alone, known immediately, so every early return below is
    # a real crew whose rail badge still needs a real count rather than a
    # `KeyError`.
    out.update(slate_counts(name))

    try:
        # `table` is additive: the single-argument call stays exactly what it
        # was before batching existed, and only a resolved table changes the
        # call shape at all.
        pane_id, claude_pid = (
            TT.claude_pane(name, table) if table is not None else TT.claude_pane(name)
        )
    except TT.JoinError as e:
        out["reason"] = str(e)
        out["alive"] = False
        return out
    out["pane"] = pane_id
    out["pid"] = claude_pid
    out["alive"] = _pid_alive(claude_pid)

    meta = TT.meta_for_pid(claude_pid, days=days)
    if meta is None:
        out["reason"] = "no run-dir record for this session's claude process"
        # Remote control still resolves from the pid alone, so the frame can say
        # whether this session is reachable even when nothing else about it is
        # readable. A session with no run-dir record is exactly the one worth
        # being able to say something true about.
        if not brief:
            out["vitals"] = vitals(None, claude_pid, None)
        return out
    session_id = meta.get("session_id")
    out["session_id"] = session_id
    out["cwd"] = meta.get("cwd")

    # TOP-LEVEL AND PRESENT IN `brief` TOO — see the module comment above
    # `ctx_pct()`. Refreshed here rather than left at the early seed's `None`s
    # because this is the first point `session_id` is known; nothing before it
    # could have resolved a real answer.
    out["ctx_pct"] = ctx_pct(session_id)
    out["rollover"] = rollover(session_id)

    # TOP-LEVEL, AND LIFTED OUT OF THE ROLLOVER OBJECT ON PURPOSE. The harness's
    # `permission_mode` is stored in the rollover state file because the hook
    # that observes it is the rollover trigger and `brief` already reads that
    # file — putting it in a file of its own would add a per-session read to the
    # one poll whose whole purpose is not making them. But it is NOT rollover
    # state: it describes the session, and a consumer reading
    # `rollover.permission_mode` would fairly conclude otherwise.
    #
    # Deck reads it to stop drawing a session that is WAITING ON THE OPERATOR as
    # idle. A successor started in plan mode (ADR-0069) has presented a plan and
    # is waiting; so is any session an operator puts in plan mode. Both look
    # exactly like an idle session from every other field here.
    out["permission_mode"] = out["rollover"].get("permission_mode")

    # TOP-LEVEL AND PRESENT IN `brief`, unlike the rest of the status area.
    #
    # A co at its prompt with dispatched runs behind it is not the same session
    # as a co at its prompt with nothing running, and the rail draws exactly
    # that difference: the first is live and open to you, the second is idle.
    # `state` cannot tell them apart — both are `idle`/`waiting` — so the rail
    # needs this, and the rail is the caller that asks for `brief`.
    #
    # ONCE PER SESSION, NOT ONCE PER BATCH, stated plainly because the obvious
    # optimisation is to lift it. `session_states` loops over `session_state`, so
    # a fifteen-row rail opens `active.json` fifteen times and pays a signal-0
    # per in-flight run each time. That is accepted rather than unnoticed: the
    # file is small and hot in the page cache, and the whole batch rides one ssh
    # round trip that costs orders of magnitude more. Lifting the read to the
    # batch is a real option if this ever shows up in a profile; it is not done
    # here because it would put a cache in the middle of a function whose value
    # is that it reads the truth every time.
    #
    # Still a different order of cost from the vitals block, which reads a
    # per-session health file and a bridge record — which is why that one stays
    # behind `brief` and this one does not.
    #
    # Duplicated into `vitals` below for now, and the duplication costs a second
    # call, not just a second key: `vitals` runs `live_agents` again, so a full
    # (non-brief) read opens `active.json` twice. `vitals` is the terminal's
    # status area, quoted from the status-line payload; these come from
    # `active.json` instead, so their home is here. The copy inside `vitals` is
    # deprecated and goes when no consumer reads it — see the contract.
    live = live_agents(out["cwd"])
    out["agents"] = live["agents"]
    out["agent_runs"] = live["agent_runs"]
    out["agents_fleet_wide"] = live["fleet_wide"]

    if not brief:
        out["vitals"] = vitals(session_id, claude_pid, out["cwd"])

    logs = _hook_logs(session_id, cwd=out["cwd"], days=days) if isinstance(session_id, str) else []
    events: list[dict] = []
    if not logs:
        # Distinguish "we know where to look and it's empty" from "we don't know
        # where to look" — the latter used to be silently read as the former,
        # which is how a stale answer survives past the point it stopped being
        # true (agent-platform#421 / issue_01M0GVMBXCX10F0R3ZAGHGEKFZ).
        _, root_reason = _hook_log_root(out["cwd"])
        out["reason"] = root_reason or "no hook log for this session"
    else:
        # Oldest file first so the fold sees the session's events in order, and
        # a transition late yesterday still stands if today's file is thin.
        for p in reversed(logs):
            events.extend(_last_events(p))
        out.update(state_from_events(events))

    # REFRESHED NOW THAT `cwd`/`session_id`/`events` ARE ALL RESOLVED — the
    # early seed above could only replay the cache; this is the one call per
    # `session_state()` that can actually observe something new.
    out["working_on_derived"] = working_on_derived(name, out["cwd"], session_id, events)

    # WHAT THE PAYLOAD CANNOT SAY, read off the pane. Only for an asking-class
    # gate, and only while blocked: there is nothing to scrape otherwise, and a
    # capture costs a tmux round trip. Never raises and never blocks the state —
    # a gate Deck can already render is not worth losing to a failed scrape, so
    # every failure is one answer: the key is absent.
    #
    # NOT IN `brief`, which is the rail's mode. The rail draws eight rows and
    # needs each one's phase, never a picker's contents — so a capture per
    # blocked row would be eight tmux round trips every poll for something
    # nothing renders. The pane asks for a full read and gets it.
    gate = out.get("gate")
    if PR is not None and not brief and isinstance(gate, dict) \
            and gate.get("kind") == "question":
        # `read_detail` BY NAME, FALLING BACK TO `read`, for the same reason the
        # import above is optional at all. `~/.local/lib` is a per-file symlink
        # farm, so between a merge and an `install.sh` the module on disk can be
        # the previous one — and a hard call to a verb it does not have yet would
        # raise per poll and take the whole state channel down, which is the #309
        # outage exactly. The older module still reads pickers; it just cannot say
        # why it stopped.
        if hasattr(PR, "read_detail"):
            rendered, dark = PR.read_detail(name)
        else:
            rendered, dark = PR.read(name), None
        if rendered:
            gate["rendered"] = rendered
        elif dark:
            # WHY THE READER SAID NOTHING, WHICH IS THE HALF THAT WAS MISSING.
            # `rendered` was absent for ten days because the version constant had
            # gone stale, and the degradation was correct and silent in exactly
            # the same way it is when a session simply has no picker up. A reader
            # that refused says so here — the reason, the version the pane
            # reported, the floor it was compared against — so a consumer can draw
            # "Deck cannot read this picker" instead of drawing nothing, and a co
            # sees it in one state read.
            #
            # A KEY THAT APPEARS ONLY ON A REFUSAL. Absent means the reader either
            # worked or had no picker to read, which is the same distinction
            # `rendered`'s own absence already carries.
            gate["renderedDark"] = dark

    # Transcript freshness, the fine-grained progress signal. Independent of the
    # event stream on purpose: it moves while the hook stream is silent.
    mtime = None
    main_agent_ts = None
    recorded = None
    try:
        # Contained before it is stat'd, not merely before it is read. This
        # decides whether a gate is suppressed, so an unvalidated path here means
        # a session that forges its own meta.json can point `transcript_path` at
        # a file it controls and make a question it is actively waiting on VANISH
        # from the pane. The guard already existed one module over; it belongs on
        # every consumer of this field, not just the one that opens it.
        recorded = TT.contained_transcript(meta.get("transcript_path"))
        if recorded is not None:
            mtime = os.stat(recorded).st_mtime
            # Only while blocked, and only for the gate check below: the common
            # poll never reads the transcript body, and nothing else needs this.
            if out.get("state") == "blocked":
                main_agent_ts = newest_main_agent_ts(recorded)
            age = datetime.now(timezone.utc).timestamp() - mtime
            out["transcript_age"] = age
            out["progressing"] = age < PROGRESS_WINDOW_SECONDS
            # THE FIELD A CONSUMER CAN GATE A RE-RENDER ON. `transcript_age` is
            # relative, so it changes on every poll and is useless for deciding
            # whether anything moved; the absolute mtime holds still while the
            # conversation does and moves when it does.
            #
            # Deck needs this because `last_event` is an event TYPE NAME rather
            # than an id: two successive events of the same type compare equal,
            # so its pane concluded nothing had changed and left the reader
            # looking at a stale conversation. Observed 2026-07-23, when the operator's
            # own messages stopped appearing in the pane he had just sent them
            # from — the messages arrived, the document never redrew.
            #
            # COMPARE IT WITH `!=`, NEVER `>`. An mtime can legitimately go
            # backwards — an NTP correction, a restore, an editor rewriting the
            # file — and a `>` gate would then stall exactly the way the
            # `last_event` gate did, which is the bug this field exists to fix.
            #
            # REPORTED RAW, NOT ROUNDED. Rounding to the millisecond was the
            # first version, justified as suppressing float noise between
            # stats — and that noise does not exist: `os.stat` maps the same
            # stored nanoseconds to the same float every time (measured, 200
            # stats of an unchanged file gave one distinct value). So rounding
            # bought nothing and cost a real false negative, two appends inside
            # the same millisecond reading as no change. Caught in review.
            out["transcript_mtime"] = mtime
    except OSError:
        pass

    # A gate the main agent has outrun was answered, and the hook stream has no
    # event to say so. Without this the session reads `blocked` from the moment
    # it asks until its turn ends. Judged against the main agent's own writes,
    # not the file mtime, so a queued message or a subagent finishing cannot
    # clear a question that is still open — the bug that hid selector's picker.
    if out.get("state") == "blocked" and gate_is_stale(out.get("gate_ts"), main_agent_ts):
        out["state"] = "working"
        out["gate"] = None
        out["gate_resolved"] = True

    # A dead process outranks whatever the last event implied: a session killed
    # mid-response leaves "working" as its final transition forever.
    if not out["alive"]:
        out["state"] = "ended"
        out["reason"] = out["reason"] or "claude process is gone"

    # THE HELD FIELD, read only where a message could be waiting. `working` is
    # the only state Slice A found one held in — the box empties on Enter, so
    # there is nothing to read while idle, waiting, or blocked — and this runs
    # after both fix-ups above so it sees the FINAL state, not a `blocked` that
    # is about to flip. Key absent everywhere else: `brief` is the rail's mode
    # and draws none of this, the same reason `vitals` and `gate.rendered` stay
    # out of it too.
    #
    # THIS IS THE FIELD MOST LIKELY TO SHIP DARK. The release changes nothing
    # else in the state object, so a follower that does not treat `held` as
    # significant emits no line when a message clears — see `_SIGNIFICANT`.
    if not brief and out.get("state") == "working":
        out["held"] = _read_held_queue(recorded) if recorded is not None else None

    return out


def signature(s: dict):
    """What makes two readings 'the same' for a follower.

    `progressing` counts only while working. On an idle or waiting session its
    transcript inevitably ages past the window, which would fire an emit that
    tells the consumer nothing it did not already know — the session is idle
    either way.
    """
    sig = [json.dumps(s.get(k), sort_keys=True) for k in _SIGNIFICANT]
    if s.get("state") == "working":
        sig.append(json.dumps(s.get("progressing")))
    # Only the TEXT, never the whole `working_on` object — its `age` moves on
    # every poll, and folding that in would make every tick look like a
    # transition. Unconditional, unlike `progressing`: a new "working on" line
    # is worth a line from the follower in any state, not only `working`.
    sig.append(json.dumps((s.get("working_on") or {}).get("text")))
    # Same reasoning, same omission of everything but `text`: `working_on_derived`
    # carries `age`/`since`, which move every poll on their own.
    sig.append(json.dumps((s.get("working_on_derived") or {}).get("text")))
    return tuple(sig)


def _parent_is_gone() -> bool:
    """True once this process has been reparented to launchd (macOS's pid 1),
    which is what happens when the ssh session that started a `--follow`
    invocation disconnects: the tmux/ssh parent dies and init adopts the
    orphan. A silent session never writes, so nothing else tells the process
    its reader is gone (see `follow_states`)."""
    import os
    return os.getppid() == 1


def _reader_hung_up(stream=None) -> bool:
    """True once the far end of this process's stdout has closed.

    THE CASE THE OTHER TWO CHECKS BOTH MISS, and the one actually seen in the
    wild. Measured on the mini with Deck running: eight followers, five of them
    for one session, aged up to 1h50, ALL with a live `sshd-session` parent.
    They accumulated over two hours while Deck ran, not on quit.

      * `_parent_is_gone` needs the whole ssh connection to die. Every channel
        of a ControlMaster shares one `sshd-session`, so closing a pane closes
        a CHANNEL and leaves the parent alive. `getppid()` never becomes 1.
      * `BrokenPipeError` needs a write, and `follow_states` writes only when
        the state CHANGES. A quiet session never writes, so it never learns.

    A closed read end is observable without writing anything, but WHICH event
    reports it is platform-specific, and assuming one platform's answer is how
    this check silently did nothing on the other. Measured on macOS 2026-08-12:
    an open pipe polls back nothing and a closed read end returns `POLLHUP`
    (16). Linux reports the same condition on a pipe's write fd as `POLLERR`
    (8) — caught by CI, which runs Linux while production runs on the mini, so
    a `POLLHUP`-only check passed every macOS measurement and still failed
    there.

    Either event means the same thing here, so both count. `POLLNVAL`
    deliberately does not: it means the descriptor is not open at all, which is
    a programming error rather than a reader leaving, and `fileno()` above
    already succeeded.

    `poll()` reports `POLLHUP` and `POLLERR` whether or not they were
    requested; the registration mask is written out for the reader, not because
    it changes what comes back.
    """
    import select
    stream = stream or sys.stdout
    try:
        fd = stream.fileno()
    except (AttributeError, ValueError, OSError):
        # No real descriptor — a test double, or a closed stream. Not a
        # hangup; the other two checks still apply.
        return False
    gone = select.POLLHUP | select.POLLERR
    poller = select.poll()
    poller.register(fd, gone)
    return any(event & gone for _, event in poller.poll(0))


def follow_states(name: str, interval: float, sleep=None, reader=None,
                   ticks=None, orphaned=None, hung_up=None):
    """Yield a state on every change, starting with the first reading.

    A generator so the loop is testable: `bin/session-agent` prints what this
    yields and owns none of the logic. `ticks` bounds the loop for tests; in
    production it is None and this runs until the process is killed.

    A read failure is YIELDED as a state rather than raised. A pane whose
    follower died would freeze on its last good frame and quietly show something
    false, which is worse than showing "unknown" and offering the terminal.

    `orphaned` and `hung_up` are checked on every tick, not once at start — a
    follower goes away mid-life, when Deck quits or a pane closes hours into a
    session, not only at birth. They default to `_parent_is_gone` and
    `_reader_hung_up`, real checks of the live process tree and of stdout; tests
    inject seams instead. Between them they cover two of the three ways a reader
    leaves. The third is `bin/session-agent`'s to catch: a session that changes
    state hits `BrokenPipeError` on its next write, which this generator does not
    handle because it does not own the state stream.

    This generator writes nothing to stdout. It does write one line to stderr on
    the way out when a check fires, so a recurrence is greppable rather than
    archaeological.
    """
    import time as _time
    sleep = sleep or _time.sleep
    reader = reader or session_state
    orphaned = orphaned or _parent_is_gone
    hung_up = hung_up or _reader_hung_up

    last = None
    count = 0
    while ticks is None or count < ticks:
        # EACH PREDICATE IS CALLED ONCE PER TICK. Calling `orphaned()` again
        # below to phrase the log line made a test counting its invocations
        # see five where it expected four — and a seam that is polled twice
        # per tick is a seam whose test cannot pin when it fired.
        was_orphaned = orphaned()
        if was_orphaned or hung_up():
            # SAY SO ON THE WAY OUT. The leak this closes ran undetected for at
            # least three days and surfaced only when a Swift build on the same
            # machine timed out — 101 followers against ~8 legitimate ones, the
            # oldest nearly three days old, load average 209 on ten cores. A
            # silent exit fixes the leak and leaves the next one just as
            # invisible; a line in the log means a recurrence is greppable
            # rather than archaeological. stderr, not stdout, because stdout is
            # the state stream a consumer parses.
            why = "parent gone" if was_orphaned else "stdout closed"
            print(f"session-agent state --follow {name}: reader gone "
                  f"({why}), exiting", file=sys.stderr, flush=True)
            return
        count += 1
        try:
            current = reader(name)
        except Exception as e:  # noqa: BLE001 - reported as state, never fatal
            # The third hand-built state dict in this module, and the last.
            # `bin/session-agent` only serializes what this yields, so nothing
            # breaks today — but the contract says these fields are always
            # present, and a follower that reads `state["agents"]` on an error
            # tick would find they are not. See `session_states`.
            current = {"session": name, "state": "unknown", "reason": str(e),
                       "agents": [], "agent_runs": [], "agents_fleet_wide": False,
                       "working_on": working_on(name),
                       "working_on_derived": working_on_derived(name)}
        sig = signature(current)
        if sig != last:
            last = sig
            yield current
        sleep(interval)


def newest_main_agent_ts(transcript_path) -> "float | None":
    """The newest time the MAIN agent wrote a conversational record, or None.

    THE FILE MTIME IS NOT THIS, AND THE DIFFERENCE IS A BUG THAT HID A LIVE
    PICKER. A gate is judged answered when the transcript advances past it, but
    the FILE advances on writes the blocked agent did not make: a queued message
    (`queue-operation`), an `attachment`, a subagent's `isSidechain` records. A
    selector session waiting on an AskUserQuestion had a queue-operation written
    ~3 minutes after its gate; the raw mtime tripped the resolved-margin and the
    question vanished from the pane before the operator could answer it (2026-07-24).

    So this counts only NON-SIDECHAIN, NON-META records of a conversational type
    — the main agent's own turns, and the tool result that carries a gate's
    answer. Read newest-first from a bounded tail, returning on the first match,
    so the common case is a short read. A tail holding none returns None, which
    KEEPS the gate rather than clearing it: a gate shown after it was answered is
    recoverable — the turn's eventual `Stop` clears it — and one hidden while
    open is not.
    """
    try:
        with open(transcript_path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - _GATE_TAIL_BYTES))
            chunk = fh.read().decode("utf-8", "ignore")
    except OSError:
        return None
    # Newest-first: the first qualifying line is the answer to return on. A tail
    # boundary can split the OLDEST line, which then fails to parse and is
    # skipped — harmless, because a real match is found near the end first.
    for line in reversed(chunk.split("\n")):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("type") not in _CONVERSATIONAL_TYPES:
            continue
        if rec.get("isSidechain") or rec.get("isMeta"):
            continue
        stamp = rec.get("timestamp")
        if not isinstance(stamp, str):
            continue
        try:
            when = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when.timestamp()
    return None


# A leading `<cross-session-message …>` opening tag, matched so its
# `hop-chain="…"` attribute can be stripped WITHOUT touching the message body
# that follows the `>`. `[^>]*` cannot itself cross into the body because the
# body's own first `>` (if any) is never reached — the match stops at the
# tag's own closing `>`, the first one in the string.
_CROSS_SESSION_TAG_RE = re.compile(r"\A<cross-session-message\b[^>]*>")
_HOP_CHAIN_ATTR_RE = re.compile(r'\s+hop-chain="[^"]*"')


def _queue_match_key(content: str) -> str:
    """*content*, normalised for matching a `remove` record against the
    `enqueue` it releases: a `hop-chain="…"` attribute (and the space before
    it) stripped from a leading `<cross-session-message …>` opening tag.

    CLAUDE CODE KEEPS THE ATTRIBUTE ON THE `enqueue` RECORD AND DROPS IT ON
    THE `remove` THAT RELEASES THE SAME MESSAGE (CQ·361, observed against
    2.1.287). Compared unstripped, the two records for one message never
    match, `queue.remove`/the equivalent search below always raises or
    falls through, and the fold returns `None` for the rest of the session
    once a hop-chain message has been queued and released — the window
    stays complete, so the unmatched `remove` record never ages out of a
    later read.

    Only the TAG is touched. A message body containing the literal text
    `hop-chain="x"` is not a `<cross-session-message>` opening tag and must
    compare unchanged, so the regex is anchored to the start of *content*
    and stops at the tag's own closing `>`.

    Returns the match key only — never what gets stored or shown. Both
    folds below keep appending the ENQUEUED text unstripped; Deck renders
    what was actually queued.
    """
    tag = _CROSS_SESSION_TAG_RE.match(content)
    if not tag:
        return content
    return _HOP_CHAIN_ATTR_RE.sub("", tag.group(0)) + content[tag.end():]


# What a `queue-operation` record's `operation` does to the queue Claude Code
# holds. Measured live 2026-08-09 against 2.1.226 (see the plan's Slice A):
# `enqueue` carries `content` and appends it; `dequeue` is a bare pop of the
# head, no content, no index; `popAll` empties the queue, carrying the whole
# former contents back to the composer (irrelevant to fold_queue, which only
# needs to know the queue is now empty); `remove` carries neither index nor
# content, so which entry left is unknowable from the stream alone.
def fold_queue(records: list[dict], window_complete: bool = True) -> "list[str] | None":
    """The queue's current contents. `_fold_queue`'s first value; see there."""
    return _fold_queue(records, window_complete)[0]


def _fold_queue(records: list[dict],
                window_complete: bool = True) -> "tuple[list[str] | None, bool]":
    """The queue's current contents, folded from its `queue-operation` records.

    RELIED ON DIRECTLY BY `session_view._held_rows`, not only through the
    public `fold_queue` above — it needs `synced` too, the second value,
    which `fold_queue` drops. Its return type (`list[str]`, content only) is
    therefore a de facto cross-module contract despite the underscore, not
    an internal-to-this-file detail: `_fold_queue_with_timestamps` below,
    which needs each entry's own `timestamp` alongside its content, is a
    SEPARATE fold rather than a change to this one's shape, precisely so
    that contract does not move under `session_view`.

    `window_complete` is whether `records` covers the session from its start,
    rather than a bounded tail that may have opened after earlier records. It
    decides whether a content-matched `remove` can be trusted; see that branch.
    Defaults True so a caller folding a complete list — every test below, and
    any future caller holding a whole file — needs no ceremony to say so.

    Pure, so it is testable against recorded records without a live session —
    mirrors the `parse`/`read` split `picker_render` uses for the same reason.
    `records` is the queue-operation records only, oldest first, the order they
    appear in the transcript.

    `remove` IS THE RELEASE PATH, and it names the entry that left. Claude Code
    2.1.226 withdraws a delivered message by content rather than draining the
    queue wholesale, so this is the record that fires on every ordinary read,
    and the entry it names is dropped by name. Only a `remove` carrying no
    content, or naming an entry outside this read's window, is unresolvable and
    returns None. A `dequeue` against an empty queue is the same honest answer
    for a different reason: it means this read's bounded tail started
    mid-stream, after some earlier `enqueue` outside the window. Both cases
    correct themselves on the NEXT poll, once the tail has moved past them —
    nothing here tries to recover within one fold.

    `popAll` is the rare one — two orders of magnitude below `remove` in every
    count taken — and needs no special grouping: several can
    share one timestamp, and emptying an already-empty queue is the same queue,
    so treating each as "clear it" is correct.

    An operation outside this vocabulary is left inert, the same
    forward-compatible answer `_state_for` gives an unrecognised hook event.
    """
    queue: list[str] = []
    # Whether this fold's view of the queue is known to be the whole of it. A
    # complete window is synced from the start; a truncated one has to earn it.
    synced = window_complete
    for r in records:
        op = r.get("operation")
        if op == "enqueue":
            content = r.get("content")
            if not isinstance(content, str):
                # REFUSING RATHER THAN SKIPPING, and the difference is the whole
                # point of this fold. Skipping leaves the queue one entry short
                # for the rest of the read, and the shortfall does not announce
                # itself: a later `dequeue` then pops the WRONG entry, and after
                # enough of them the fold reports an empty queue while a message
                # is genuinely still held. `held: []` is a positive claim that
                # nothing is waiting, so the failure is a confident wrong answer
                # rather than a missing one — worse than every case below, which
                # at least says "unknown".
                return None, synced
            queue.append(content)
        elif op == "dequeue":
            if not queue:
                return None, synced
            queue.pop(0)
        elif op == "popAll":
            queue = []
            # A `popAll` empties the REAL queue, not merely this fold's view of
            # it, so whatever the window missed before this point is gone from
            # the session too. That makes it the one record that can establish
            # sync inside a truncated window — see the `remove` branch.
            synced = True
        elif op == "remove":
            # `remove` NAMES ITS ENTRY, and that is the whole correction here.
            # This branch used to return None unconditionally, on the stated
            # premise that a `remove` "names neither the index nor the content
            # of the entry that left."
            #
            # THE PREMISE WAS STALE RATHER THAN MISMEASURED, which is the part
            # worth keeping. Re-measured 2026-08-10 across every transcript on
            # the mini, SPLIT BY THE VERSION THAT WROTE EACH RECORD: every
            # contentless `remove` was written by 2.1.202, and every `remove`
            # from 2.1.203 to 2.1.226 carries content, with not one exception
            # either side. The old corpus-wide count could not see that — a few
            # hundred records from one retired build looked like a standing
            # share of the vocabulary rather than a shape that had stopped
            # being written.
            #
            # The split is the durable claim; the totals are not. This corpus is
            # a rolling window that gains records from live sessions and loses
            # whole files to retention, and two counts an hour apart on
            # 2026-08-10 disagreed by more than a hundred.
            #
            # IT IS ALSO THE ORDINARY RELEASE PATH, not a rare edit. Claude Code
            # 2.1.226 withdraws a delivered message with `remove`, not `popAll`:
            # `remove` outnumbers `popAll` by orders of magnitude. So a
            # blanket None fired the first time ANY queued message was read, and
            # because the record then sits inside the bounded tail for the rest
            # of the session, every later fold re-read it and returned None
            # again. The queue went permanently "unknown" on first use: the
            # first held message drew for a second and vanished, and no later
            # one drew at all. Observed by hand 2026-08-10 — the defect the
            # test suite could not see, because every recorded fixture used
            # `popAll` for release.
            #
            # WITH THE CONTENT IN HAND THE AMBIGUITY IS GONE. The bounded-tail
            # worry the old comment argued — this fold's count may not be the
            # real queue's count — was an argument about POSITION, and position
            # is what a named entry does not need. An entry this fold can see is
            # dropped by name; the multiset of what remains is right regardless
            # of what sits outside the window.
            content = r.get("content")
            if not isinstance(content, str):
                # Kept for the 2.1.202 shape, where the original reasoning holds
                # in full: nothing identifies what left. Unreachable on any
                # version a live session runs today, and kept anyway — this
                # module reads a file it does not own, and a shape that existed
                # once can return.
                return None, synced
            if not synced:
                # A TRUNCATED WINDOW CANNOT TRUST A CONTENT MATCH, and the case
                # is narrow enough to be worth spelling out, because review
                # found it and the first version of this branch got it wrong.
                #
                # Take one entry queued before this window opened and still
                # held, and a LATER entry with identical text inside the window.
                # The `remove` releases the older one. This fold cannot see the
                # older one, so it matches the only copy it has and reports an
                # empty queue — over a message that is genuinely still waiting.
                # Reproduced directly:
                #
                #     fold_queue([{"operation": "enqueue", "content": "ok"},
                #                 {"operation": "remove",  "content": "ok"}],
                #                window_complete=False)
                #
                # That is a confident wrong answer about emptiness, which the
                # `enqueue` branch above calls worse than every case that at
                # least says "unknown". Duplicate short confirmations — "yes",
                # "approve", "it's merged" — are exactly the shape that repeats
                # verbatim in a session, so this is not a contrived shape.
                #
                # REFUSING HERE COSTS NOTHING AGAINST WHAT SHIPPED. Before this
                # change every `remove` returned None, so every session lost the
                # queue at its first release; a session over `_HELD_TAIL_BYTES`
                # now gets that same behaviour and no worse.
                #
                # HOW OFTEN THAT BITES WAS FIRST STATED AS ~4%, AND THAT NUMBER
                # WAS MEASURED AGAINST THE WRONG POPULATION. It is corpus-wide,
                # and the corpus is overwhelmingly short dispatched-worker runs
                # that nobody reads a held message in. Against the co sessions
                # Deck's pane actually shows, 37.4% exceeded the 1MB cap this
                # shipped with — better than one in three, not one in
                # twenty-five. Re-measured 2026-08-10; the split is on
                # `_HELD_TAIL_BYTES`, which is why it moved to 8MB and why the
                # figure lives there rather than here.
                #
                # The lever is that constant rather than a cleverer match — no
                # amount of care here can recover a record the window never saw.
                return None, synced
            match_key = _queue_match_key(content)
            for i, held in enumerate(queue):
                if _queue_match_key(held) == match_key:
                    del queue[i]  # first match; normalised text is equal content
                    break
            else:
                # Names an entry this fold never saw, so the window opened after
                # its `enqueue` — the same mid-stream case the empty-`dequeue`
                # rule catches, reached by a different record. Refusing keeps a
                # message that is genuinely still queued from being reported
                # gone.
                #
                # THIS SELF-CORRECTS ONLY WHEN THE WINDOW IS INCOMPLETE, not in
                # general (CQ·361 — the previous wording here claimed "the next
                # poll self-corrects once the tail moves past" unconditionally,
                # which is false below `_HELD_TAIL_BYTES`). A complete window
                # covers the whole transcript from its start, so this same
                # unmatched `remove` record is in every later read too, and this
                # branch returns `None` again on every subsequent poll for the
                # rest of the session. Only a window that is NOT yet complete —
                # the file still growing past the cap — can later shift this
                # record out of the tail and stop reaching this branch at all.
                return None, synced
    return queue, synced


def _read_queue_records(transcript_path) -> "tuple[list[dict], bool, str | None] | None":
    """The bounded transcript-tail read `_read_held_queue` and
    `_held_messages_with_timestamps` both need: every `queue-operation`
    record in the tail (oldest first, the order the file itself is in),
    whether the tail covers the session from its start (`window_complete`),
    and the newest `claudeVersion` seen anywhere in the same read. None on
    any read failure — the "we looked and could not tell" outcome both
    callers hand upward unchanged. Split out from `_read_held_queue`, which
    used to do this inline, so Slice 7's timestamp-carrying reader does not
    duplicate the tail-read and parse.

    `claudeVersion` cannot come off a `queue-operation` record itself — it
    carries no `version`, unlike the conversational envelope (`assistant`,
    `user`) — so this takes the newest `version` seen anywhere in the same
    tail read. That is free: the tail is already open for the queue
    records, and a working session's tail holds recent conversational turns
    beside them.

    A tail boundary can split the OLDEST line, which then fails to parse
    and is skipped, same as `newest_main_agent_ts`. Harmless here for the
    same reason: the queue records that matter are near the end, well
    inside the window.
    """
    try:
        with open(transcript_path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - _HELD_TAIL_BYTES))
            chunk = fh.read().decode("utf-8", "ignore")
    except OSError:
        return None

    # A file that fits inside the cap was read whole, so the fold sees the
    # session from its start and a content-matched `remove` is exact. Read off
    # the size rather than tracked through the parse, because the seek above is
    # already the decision.
    window_complete = size <= _HELD_TAIL_BYTES

    queue_records: list[dict] = []
    version = None
    for line in chunk.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        v = rec.get("version")
        if isinstance(v, str):
            version = v  # walked oldest to newest, so the last hit is newest
        if rec.get("type") == "queue-operation":
            queue_records.append(rec)

    return queue_records, window_complete, version


def _read_held_queue(transcript_path) -> "dict | None":
    """The `held` field's reader: a bounded transcript tail, folded.

    Mirrors `picker_render.read`'s split from `parse` — this hands the
    records `_read_queue_records` already parsed to the pure fold above,
    then wraps the result in the field's own shape. None propagates from
    `fold_queue` unchanged: "we looked and could not tell" per the
    contract, not a crash.
    """
    read = _read_queue_records(transcript_path)
    if read is None:
        return None
    queue_records, window_complete, version = read
    messages = fold_queue(queue_records, window_complete=window_complete)
    if messages is None:
        return None
    return {"messages": messages, "source": "queue", "claudeVersion": version}


def _fold_queue_with_timestamps(
        records: list[dict], window_complete: bool = True) -> "list[tuple[str | None, str]] | None":
    """`_fold_queue`'s own fold, kept as `(timestamp, content)` pairs instead
    of content alone — a SEPARATE function rather than a change to
    `_fold_queue`'s shape, because `session_view._held_rows` already relies
    on `_fold_queue` returning `list[str]` directly (not only through the
    public `fold_queue`, for the `synced` flag `fold_queue` drops), so that
    return type is a de facto cross-module contract this file does not own
    alone. This mirrors `_fold_queue`'s logic line for line — same
    operations, same refusal conditions, same "first match" `remove` rule —
    and the two must be changed together if Claude Code's queue vocabulary
    ever does (see `_fold_queue`'s own docstring for what each operation
    means and why each refusal exists; not re-explained here to avoid the
    two drifting into disagreeing explanations of one fold).

    Built for epic issue_01M1S2X8ENKYEGSDE3H0REFD3G's Slice 7:
    `_held_messages_with_timestamps` below is this function's only caller,
    feeding the `held_items` verb and `co_rollover.forward_late_messages`,
    both of which need a held message's own timestamp — `fold_queue`
    itself has no such caller and stays content-only.
    """
    queue: list[tuple[str | None, str]] = []
    synced = window_complete
    for r in records:
        op = r.get("operation")
        if op == "enqueue":
            content = r.get("content")
            if not isinstance(content, str):
                return None
            queue.append((r.get("timestamp"), content))
        elif op == "dequeue":
            if not queue:
                return None
            queue.pop(0)
        elif op == "popAll":
            queue = []
            synced = True
        elif op == "remove":
            content = r.get("content")
            if not isinstance(content, str):
                return None
            if not synced:
                return None
            match_key = _queue_match_key(content)
            for i, (_, held) in enumerate(queue):
                if _queue_match_key(held) == match_key:
                    del queue[i]
                    break
            else:
                return None
    return queue


def _held_messages_with_timestamps(transcript_path) -> "list[dict] | None":
    """Every currently-held queued message, oldest first, as
    `{"content": str, "queued_at": str | None}` — `_fold_queue_with_
    timestamps` wrapped in the same tail-read `_read_held_queue` uses via
    `_read_queue_records`, so this and the `held` field agree on what is
    queued.

    Built for epic issue_01M1S2X8ENKYEGSDE3H0REFD3G's Slice 7: the
    `held_items` verb below reports each held message's own timestamp, and
    `co_rollover.forward_late_messages` needs it to tell whether a message
    arrived after a handoff was written.

    None on the same two conditions `_read_held_queue` returns None for:
    the transcript could not be read, or the fold hit an unresolvable
    operation.
    """
    read = _read_queue_records(transcript_path)
    if read is None:
        return None
    queue_records, window_complete, _version = read
    pairs = _fold_queue_with_timestamps(queue_records, window_complete=window_complete)
    if pairs is None:
        return None
    return [{"content": content, "queued_at": ts} for ts, content in pairs]


def held_items(name: str) -> dict:
    """What *name* is holding for the operator right now (epic
    issue_01M1S2X8ENKYEGSDE3H0REFD3G, Slice 7): its composer's current
    draft, every message held in its queue (each with whatever `timestamp`
    the queue's own `enqueue` record carried), and its rollover state.

    Returns `{"outcome": "ok", "draft": str | None, "held_messages":
    list[dict] | None, "rollover": {...}}` on success, or `{"outcome":
    "refused", "reason": str}` when the session itself cannot be resolved —
    the same two-shape contract `rollover_command` uses, and for the same
    reason: a caller (the `held-items` verb, and `handoff-leave` through
    it) needs to tell "nothing is held" from "couldn't check" from "no such
    session", not conflate them into one falsy value.

    `held_messages` is `None` when the fold could not resolve the queue
    (the same "we looked and could not tell" `_held_queue_now` returns),
    `[]` when it positively found nothing held, and the list itself
    otherwise. The caller renders that three-way split as an explicit
    "nothing held" line rather than a silently empty section — see
    `bin/session-agent`'s `_held_items_markdown`, which is what
    `handoff-leave` actually pastes.
    """
    try:
        pane_id, claude_pid = TT.claude_pane(name)
    except TT.JoinError as e:
        return {"outcome": "refused", "reason": str(e)}

    meta = TT.meta_for_pid(claude_pid)
    if meta is None:
        return {"outcome": "refused",
                "reason": "no run-dir record for this session's claude process"}

    draft = SI._read_input_box(pane_id)
    draft = draft if draft and draft.strip() else None

    recorded = TT.contained_transcript(meta.get("transcript_path"))
    held_messages = _held_messages_with_timestamps(recorded) if recorded is not None else None

    return {
        "outcome": "ok",
        "draft": draft,
        "held_messages": held_messages,
        "rollover": rollover(meta.get("session_id")),
    }


def gate_is_stale(gate_ts: str | None, main_agent_ts: float | None) -> bool:
    """Whether a gate has been answered, judged by the MAIN AGENT outrunning it.

    THE GAP THIS COVERS, AND WHY IT NEEDS COVERING. Answering an
    `AskUserQuestion` emits NO hook event. The stream carries the
    `PermissionRequest` and its paired `Notification` and then falls silent
    while the session works on, so a reader folding events alone reports
    `blocked` from the moment a question is asked until the turn eventually
    ends — which can be an hour, and looks exactly like a session that is
    genuinely waiting.

    Observed 2026-07-23: `steward` and `svc` both reported blocked on questions
    already answered, their transcripts 19 and 68 minutes past their gates.

    The main agent's own next write closes it, because a blocked agent writes no
    turn of its own. `main_agent_ts` is `newest_main_agent_ts`, NOT the file
    mtime — the mtime moves on control records and subagent work the blocked
    agent did not do, which is what cleared a live gate too early (see that
    function). A conversational record after the gate could only have been
    written once the gate cleared.
    """
    if gate_ts is None or main_agent_ts is None:
        return False
    try:
        then = datetime.fromisoformat(gate_ts)
    except ValueError:
        return False
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return main_agent_ts - then.timestamp() > GATE_RESOLVED_MARGIN_SECONDS


def _pid_alive(pid) -> bool:
    """Whether a pid names a live process. False for anything that is not a pid.

    The type guard is not decoration since `live_agents` started calling this:
    the pane's own pid comes from tmux and is an int, but a pid out of
    `active.json` is whatever was written there, and `os.kill(None, 0)` raises
    TypeError. Uncaught, one malformed run record would take down the whole state
    read — so the pane would go blank over a field it does not even display.
    """
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists and belongs to someone else. Alive for our purposes; the join
        # already established it is the pane's own process.
        return True
    except (TypeError, ValueError, OSError):
        return False
    return True
