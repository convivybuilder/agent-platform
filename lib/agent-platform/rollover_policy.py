"""rollover_policy — the policy file and the zone evaluator for ADR-0069's hook.

ADR-0069 ("A co session rolls itself over before it runs out of context") puts
the trigger outside the session: a session past a context threshold is the one
least able to act on a standing instruction, so something external has to
notice and say so. Nothing in the Bosun computes `ctx_pct` continuously — each
session's own status line (`bin/co-session-health`) writes it once per turn to
`~/.cache/co-session-health/vitals-<session_id>.json`, and that reading stays
fresh while the session works. So the trigger evaluates HERE, inside the hook
that fires on the session's own events, rather than in a watcher process that
does not exist.

THREE ZONES, NOT A BOOLEAN. `ok` is below the seek threshold. `seeking` is past
it — the session should look for a good moment to roll over, bounded by a call
cap so an unattended session cannot look forever. `roll_now` is past the hard
floor, or past the cap while still seeking: the session rolls regardless of
whether the moment is good, because "a rough handoff beats a session that ran
out while waiting for a tidy exit" (ADR-0069).

`floor_pct` and `seek_call_cap` are `null` (inert) until Slice 2 measures the
real data and the operator approves values — a null limit never fires, so an unmeasured
platform only ever seeks, never forces.

THE POLICY FILE IS RE-READ ON EVERY CALL, ON PURPOSE. `~/.config/convivy/
rollover.json` has no cache in front of it, so an edit takes effect on the very
next hook firing across every running session — no restart, no re-deploy.

THE CALL CAP COUNTS MODEL CALLS, NOT OPERATOR TURNS. A single turn can drive
many tool calls (one Bosun session made 203 in a turn after a compact), so
`calls_seeking` increments on `PostToolUse` firings specifically — the hook
event that fires once per tool call — and nothing else. `Stop` and
`UserPromptSubmit` still re-evaluate the zone (context can have moved since the
last `PostToolUse`) but never advance the counter.

A FOURTH ZONE, `vetoed`, ARRIVES WITH SLICE 3 — Deck's write side. Where the
first three zones come only from `ctx_pct` and the policy file, `vetoed` (and
the unconditional `roll_now` from a `force`, and a `seek`-lowered threshold)
come from the override `session-agent rollover <name> veto|force|seek|clear`
writes to `~/.cache/co-session-health/rollover-override-<session_id>.json`.
That file is session-agent's alone to write and this module's alone to read
(`read_override`), mirroring the state file's own split — see `evaluate`'s
docstring for how each override kind is honoured, and `rollover-policy`'s
functions further down for the SEPARATE, stricter validation an operator's
`--set` gets that the policy file's own loader deliberately does not enforce
on itself.

`resolve_override`, further down, is what makes an override visible on a
READ before the hook fires again: `session_state.rollover` calls it against
the hook's last persisted `natural_state`/`natural_reason`, the same pair
`evaluate` computes live and hands to the identical function, so the two can
never compute a different answer for the same override
(issue_01M2HTBMRN1W688EV40DCDVXB7).

`ROLL_NOW_REISSUE_LIMIT` (below) ASSUMES THE OBSERVED SESSION COOPERATES.
The counter it bounds, `roll_now_reissues`, lives in the same per-session
state file this module's own hook writes on every firing — a file the
observed session can also write, since it runs with the same filesystem
access as everything else it does. Nothing here distinguishes a write from
the hook process from a write from the session it is watching, so a session
that rewrites or deletes its own state file resets the count this bound
reads, the same way `clear_parked` does for an operator's own `clear`. This
module guards a session that idly declines a rollover in prose
(issue_01M2HTBBHK2W4EH86RAFE0YBPY); it is not a defense against one that
tampers with its own evidence to avoid being parked — no session has been
observed doing that, and building for it is not this module's job until one
is.

SELF-CONTAINED ON PURPOSE. This module is imported by a hook that fires on
every tool call once registered, so it takes no dependency beyond the standard
library — not `session_state.py`, whose import chain pulls in `tmux_transcript`,
`builder_root`, `projects`, `signal_client` and `private_crew` for a pane and a
process this evaluator never needs. `co-session-health` and
`tools/session-event-log.py` made the identical choice for the identical
reason, and their own docstrings say so.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from pathlib import Path

# Same cache directory `co-session-health` already writes vitals and
# working-on recordings into, and the same sanitised-charset key scheme
# (`session_state._SESSION_ID_RE`) — a session id becomes a filename here too.
CACHE_DIR = Path.home() / ".cache" / "co-session-health"
POLICY_PATH = Path.home() / ".config" / "convivy" / "rollover.json"

_SESSION_ID_RE = re.compile(r"[A-Za-z0-9_-]+")

# Same staleness bound `session_state.VITALS_STALE_SECONDS` uses for the same
# file. Kept as this module's own constant rather than imported, per the
# self-containment note above: a shared constant would be one more name this
# module resolves through another file's import chain.
VITALS_STALE_SECONDS = 3600

DEFAULT_POLICY = {
    "mode": "observe",
    "seek_pct": 70,
    "floor_pct": None,
    "seek_call_cap": None,
}

DEFAULT_STATE = {
    "state": "ok",
    "reason": None,
    "since": None,
    "calls_seeking": 0,
    "cap": None,
    "policy": DEFAULT_POLICY,
    "stop_blocked": False,
    # `natural_state` is the zone this call would report with NO override in
    # effect — kept alongside the (possibly overridden) `state` so a later
    # call can tell whether the session has genuinely crossed into a worse
    # zone, rather than reading its own overridden report back as fact. See
    # `evaluate`'s override section: a veto checks the CURRENT natural zone
    # against the one it was set in (`_veto_covers`), and that check breaks
    # if "prior zone" means "what we told Deck last time" instead of "what
    # was really true".
    "natural_state": "ok",
    # Which policy threshold a `vetoed` report is waiting on — `floor_pct`,
    # `seek_call_cap`, or None. Slice 3 (Deck's write side); None whenever
    # `state` is not `vetoed`. Never `seek_pct`: a veto now holds through
    # seeking (issue_01M3WX4443DF30SJ3WM5QHA7RJ), so the floor or the call
    # cap is always the limit that actually lifts it — see `_vetoed_until`.
    "vetoed_until": None,
    # The reason that goes with `natural_state` — kept alongside it for the
    # identical reason `natural_state` is kept alongside `state`: a reader
    # resolving a FRESH override against this PERSISTED reading (`session_
    # state.rollover`, below) needs to know not just which zone was natural
    # but why, because "seeking only because an operator's `seek` lowered the
    # threshold" (`operator_seek`) and "seeking because the reading really
    # crossed it" (`threshold`) call for different answers once a veto is
    # laid on top — see `_vetoed_until`.
    "natural_reason": None,
    # `predecessor`/`successor` ({"session_id", "pane_id"}, or None) and
    # `successor_window` (the successor's tmux window index) are Slice 5's
    # fields (`bin/co-rollover`'s `mark_rolling`, below) — set only while
    # `state` is `rolling`, and None otherwise. Deck's rail draws one leaf per
    # window (`WindowLeaf.swift:37`), so without `successor_window` a rollover
    # shows up as an unexplained second window (epic comment 2399).
    # `successor["session_id"]` starts None: the successor's own Claude
    # session id is not assigned until it registers, so the predecessor's
    # initial write cannot know it. `record_successor_session_id` fills it in
    # once the successor confirms pickup and reports its own id.
    "predecessor": None,
    "successor": None,
    "successor_window": None,
    # `handoff_path`/`issue_id`/`issue_status`/`issue_product` are Slice 6's fields
    # (`bin/co-rollover`'s and `tools/rollover-trigger.py`'s `mark_parked`) — set
    # only while `state` is `parked`, and None otherwise.
    "handoff_path": None,
    # ALWAYS None. A park logs an alert signal (`signal_client.post_alert`), and a
    # signal has no id. The key stays so a parked state file written before this
    # change still reads.
    "issue_id": None,
    # WHY `issue_status` IS WHAT IT IS — the status `signal_client.post_alert`
    # returned for the park's signal: `created`, `duplicate`, `skip` when there was
    # no token, or an `error:`-prefixed status whose tail is not a closed set. It
    # belongs here and not only in `mark_parked`'s literal because `read_state`
    # overlays the persisted file onto THIS dict and iterates ITS keys: a field
    # missing here is written to disk and then invisible to every Python reader.
    "issue_status": None,
    # The `product` the park's signal was submitted under. For the hook paths that
    # is `basename(cwd)`, which is a label, not necessarily the crew's canonical slug.
    "issue_product": None,
    # How many times, THIS episode, the session has actually been told "run
    # /rollover now" with nothing acted on — counted only on a firing that
    # would really announce it (see `evaluate`'s `announced` below), so a
    # session sitting in `roll_now` under `observe` mode with no operator
    # force accrues nothing until something is actually said to it. Reset to
    # 0 whenever the zone is not `roll_now`. `tools/rollover-trigger.py` is
    # the reader: past `ROLL_NOW_REISSUE_LIMIT` it parks and logs instead of
    # re-issuing again (issue_01M2HTBBHK2W4EH86RAFE0YBPY — a session that
    # declines in prose, rather than running `/rollover`, was told again on
    # every firing with nothing to stop it). This counter lives in a file the
    # OBSERVED session can itself rewrite or delete — the same access it uses
    # for everything else it does — so this bound assumes a cooperating
    # session and can be reset by one that isn't; see the module docstring's
    # note on `ROLL_NOW_REISSUE_LIMIT` for why that's not being guarded
    # against here.
    "roll_now_reissues": 0,
    # How many times IN A ROW, with no tool call and no operator prompt in
    # between, a `Stop` firing has announced `seeking` to this session. The
    # sibling of `roll_now_reissues` for the other announcing zone, which had
    # no bound at all until issue_01M2PFRWZT27XY6ZREVWWNQ9SZ: a session that
    # ends its turn, is told to seek a rollover moment, and ends its turn
    # again was told again on every firing forever. One such loop ran 58
    # announcements in 9.5 minutes, re-reading 740k cached tokens to emit ten
    # output tokens each time — about $13/minute — and stopped only when the
    # operator typed `/exit`. `tools/rollover-trigger.py` is the reader, past
    # `SEEKING_STOP_REISSUE_LIMIT`. Reset by any firing that shows the
    # session is not looping; see `evaluate` for which and why. Same
    # cooperating-session caveat as `roll_now_reissues` above.
    "seeking_stop_reissues": 0,
    # The harness's `permission_mode` as of the last firing that carried one —
    # `plan`, `auto`, `bypassPermissions`, or None when nothing has reported yet.
    #
    # IT IS NOT ROLLOVER STATE, AND IT LIVES HERE FOR A COST REASON. Deck needs
    # it to tell a session idle in PLAN MODE from one that is simply idle: a
    # successor started in plan mode (ADR-0069) sits waiting on the operator
    # while the rail draws it as doing nothing, and so does any session an
    # operator puts in plan mode. The value arrives on hook payloads, and
    # `tools/rollover-trigger.py` is already the thing that sees those payloads
    # on `Stop` and `UserPromptSubmit` — the two events that carry it for an
    # idle session. Writing it to a file of its own would add one more per-session
    # read to the BRIEF states poll, which is the exact cost `brief` exists to
    # avoid; this file is already read there.
    #
    # So the storage is here and the CONTRACT SURFACE IS NOT: `session_state`
    # lifts it to a top-level `permission_mode`, because a consumer reading
    # `rollover.permission_mode` would reasonably conclude it describes the
    # rollover rather than the session.
    "permission_mode": None,
}

# NOT a policy key. Epic comment 2402 ruling (a) covers `floor_pct` and
# `seek_call_cap` only, both still unset pending measurement under
# ADR-0069's measure-then-operator-approves process — that ruling says nothing
# about this constant. Treating it as exempt from that process is this
# author's own call, not something ruling (a) grants: a session looping
# against a limit it can itself raise isn't bounded, so this is a circuit
# breaker on a hook that would otherwise repeat itself on a stuck session
# forever, not a tunable a crew should reach for. A session may have a good
# reason to decline — the whole point of the bound is to hand that to the
# operator rather than force the rollover (see `tools/rollover-trigger.py`'s
# park-and-file, which reads this) — so the number only bounds how many
# times the session is told before the platform stops telling it and tells a
# human instead. 3 is the exact count Deck's probe observed before manually
# clearing it (issue_01M2HTBBHK2W4EH86RAFE0YBPY).
ROLL_NOW_REISSUE_LIMIT = 3

# The same circuit breaker for `seeking`, on the same footing as the constant
# above — not a policy key, and not a tunable a crew should reach for.
#
# WHY THIS IS A SEPARATE COUNT RATHER THAN A WIDER READING OF THE ONE ABOVE:
# `roll_now_reissues` is reset to 0 on every zone that is not `roll_now`, so
# in `seeking` it is pinned at 0 and `ROLL_NOW_REISSUE_LIMIT` can never fire.
# That is exactly how a session sat in `seeking` at 72% and was told to roll
# over 58 times while the bound that exists to stop it read a counter that
# was structurally zero (issue_01M2PFRWZT27XY6ZREVWWNQ9SZ). The limit was
# working; it guards the other door.
#
# 3 matches its sibling for the same reason 3 was chosen there — enough for a
# session with something to finish, few enough that a loop costs seconds
# rather than an hour. It is deliberately NOT derived from `seek_call_cap`:
# that cap counts tool calls, and the loop this bounds makes none.
SEEKING_STOP_REISSUE_LIMIT = 3

# The event whose firing is the proxy for "a model call happened". See the
# module docstring: an operator turn can drive many of these.
_COUNTS_AS_A_CALL = "PostToolUse"


def _key(session_id) -> str:
    return "".join(_SESSION_ID_RE.findall(str(session_id or "")))[:64]


def _read_json(path) -> dict | None:
    """One JSON object from a file, or None — never raises. Mirrors
    `session_state._read_json`'s contract exactly, duplicated rather than
    imported for the reason the module docstring gives."""
    try:
        with open(path) as fh:
            value = json.load(fh)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _atomic_write(path: Path, data: dict) -> None:
    """Write `data` as JSON to `path`, atomically. A crash or a race with
    another firing of this same hook must never leave a half-written state
    file — the next call would fail to parse it and silently fall back to
    `ok`, dropping whatever seeking progress the session had made."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def load_policy(path=None) -> dict:
    """The effective policy, re-read from disk every call — never cached, so
    an edit reaches the next hook firing with no restart.

    A missing or unreadable file, or a field of the wrong shape, degrades that
    ONE field to the default rather than discarding the whole file — the same
    per-field degrade `session_state.vitals` already follows.

    `seek_pct` MUST NEVER BE NULL, NAN OR INFINITE (the operator's ruling (a)): unlike
    `floor_pct`/`seek_call_cap`, whose null means "unmeasured, so inert",
    `evaluate` always compares `effective_pct` against `seek_pct` — a null or
    non-finite value there does not fall through to inert, it breaks the
    comparison. `json.load` parses the non-standard `NaN`/`Infinity`/
    `-Infinity` tokens by default, so a hand-edited file can carry either past
    the `isinstance` check below; both are refused here exactly like a wrong
    type, falling back to `DEFAULT_POLICY["seek_pct"]`.
    """
    p = Path(path) if path is not None else POLICY_PATH
    policy = dict(DEFAULT_POLICY)
    data = _read_json(p)
    if data is None:
        return policy
    if data.get("mode") in ("observe", "enforce"):
        policy["mode"] = data["mode"]
    for field in ("seek_pct", "floor_pct", "seek_call_cap"):
        if field not in data:
            continue
        value = data[field]
        if value is None:
            if field != "seek_pct":
                policy[field] = None
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if not math.isfinite(value):
            continue
        policy[field] = value
    return policy


# --- editing the policy (Slice 3: `session-agent rollover-policy --set`) ----
#
# `load_policy` above degrades a bad field silently, on purpose: the HOOK must
# never fail a firing over a malformed config file. An operator's edit is the
# opposite case — a typo in `--set` should be refused loudly, right there on
# the command line, rather than silently falling back to a default the
# operator did not ask for. So this is a second, stricter path, not a
# relaxation of the first.
class PolicyError(ValueError):
    """Raised by `parse_policy_updates`/`apply_policy_updates` for anything
    `session-agent rollover-policy --set` should refuse rather than degrade."""


_POLICY_KEYS = ("mode", "seek_pct", "floor_pct", "seek_call_cap")


def parse_policy_updates(pairs: list[str]) -> dict:
    """`["seek_pct=65", "floor_pct=null"]` -> `{"seek_pct": 65, "floor_pct":
    None}`. Pure string parsing; `apply_policy_updates` does the semantic
    checks (mode's vocabulary, the floor/seek ordering) against the policy
    these will be merged into."""
    updates: dict = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        if not sep:
            raise PolicyError(f"malformed --set argument {pair!r}, expected key=value")
        if key not in _POLICY_KEYS:
            raise PolicyError(f"unknown policy key {key!r} (one of {', '.join(_POLICY_KEYS)})")
        if key == "mode":
            updates[key] = raw
            continue
        if raw == "null":
            updates[key] = None
            continue
        try:
            value: float = float(raw)
        except ValueError:
            raise PolicyError(f"{key} must be a number or null, got {raw!r}") from None
        updates[key] = int(value) if value.is_integer() else value
    return updates


# `seek_pct` is the one numeric key that must never be null (the operator's ruling
# (a)): `floor_pct`/`seek_call_cap` null means "unmeasured, inert", but
# `evaluate` always compares against `seek_pct`, so a null there breaks the
# comparison instead of falling through to inert.
_NEVER_NULL_KEYS = ("seek_pct",)


def apply_policy_updates(current: dict, updates: dict) -> dict:
    """`current`, with `updates` merged in and validated as a whole — refuses
    a floor at or below the seek threshold (the field build asked for
    explicitly), checked against the RESULTING policy so `--set floor_pct=80
    --set seek_pct=85` in one call is judged on where both end up, not on an
    intermediate state that happened to be invalid for one line of the loop.

    Also refuses null for `seek_pct` (it can never be inert, unlike
    `floor_pct`/`seek_call_cap`) and refuses a non-finite number
    (`inf`/`-inf`/`nan`, reachable via `--set seek_pct=1e400` or `--set
    seek_pct=nan`) for any numeric key — both parse as valid JSON numbers and
    would otherwise pass `load_policy`'s type check while making every
    threshold comparison in `evaluate` silently false.
    """
    new = dict(current)
    for key, value in updates.items():
        if key == "mode":
            if value not in ("observe", "enforce"):
                raise PolicyError(f"mode must be 'observe' or 'enforce', got {value!r}")
        elif value is None:
            if key in _NEVER_NULL_KEYS:
                raise PolicyError(f"{key} must not be null")
        elif not isinstance(value, (int, float)) or isinstance(value, bool):
            raise PolicyError(f"{key} must be a number or null")
        elif not math.isfinite(value):
            raise PolicyError(f"{key} must be a finite number, got {value!r}")
        new[key] = value
    floor_pct, seek_pct = new.get("floor_pct"), new.get("seek_pct")
    if floor_pct is not None and seek_pct is not None and floor_pct <= seek_pct:
        raise PolicyError(f"floor_pct ({floor_pct}) must be above seek_pct ({seek_pct})")
    return new


def save_policy(policy: dict, path=None) -> None:
    """Write the whole policy file, atomically. Always the full four-field
    shape — `apply_policy_updates` builds `new` from a complete `current`, so
    there is never a partial policy to write."""
    _atomic_write(Path(path) if path is not None else POLICY_PATH, policy)


def _vitals_path(session_id, vitals_dir=None) -> Path:
    d = Path(vitals_dir) if vitals_dir is not None else CACHE_DIR
    return d / f"vitals-{_key(session_id)}.json"


def read_ctx_pct(session_id, vitals_dir=None, now: float | None = None) -> float | None:
    """The session's last-rendered context percentage, or None.

    None covers three cases the caller must not tell apart: no reading has
    ever been written, the reading is unparseable, and the reading is older
    than `VITALS_STALE_SECONDS` — a session that has not taken a turn in an
    hour has nothing current to evaluate against, and treating a stale number
    as live would let a truly idle session's LAST context reading force a
    rollover it is no longer close to needing.
    """
    key = _key(session_id)
    if not key:
        return None
    reading = _read_json(_vitals_path(session_id, vitals_dir))
    if reading is None:
        return None
    written = reading.get("written_at")
    if not isinstance(written, (int, float)):
        return None
    now_ts = now if now is not None else time.time()
    if now_ts - written > VITALS_STALE_SECONDS:
        return None
    pct = reading.get("ctx_pct")
    return pct if isinstance(pct, (int, float)) else None


def _state_path(session_id, state_dir=None) -> Path:
    d = Path(state_dir) if state_dir is not None else CACHE_DIR
    return d / f"rollover-{_key(session_id)}.json"


def read_state(session_id, state_dir=None) -> dict:
    """The hook's last-persisted decision for this session, degraded to the
    inert default on anything unreadable — the same "no reading yet" doctrine
    `session_state.vitals` follows for its own file."""
    reading = _read_json(_state_path(session_id, state_dir))
    if reading is None:
        return dict(DEFAULT_STATE)
    out = dict(DEFAULT_STATE)
    for field in out:
        if field in reading:
            out[field] = reading[field]
    return out


# --- rolling (Slice 5, `bin/co-rollover`) -----------------------------------
#
# `rolling` and `parked` (Slice 6, below) are TERMINAL states, unlike the
# three `evaluate()` computes on its own. Once `bin/co-rollover` sets either
# one, `evaluate()` (below) must never move a session back out of it on its
# own. For `rolling`, a session whose active window has already handed off
# to a successor has no `ctx_pct` reading worth acting on, and the PreToolUse
# fence (`tools/rollover-trigger.py`) depends on this state surviving every
# later `PostToolUse`/`Stop`/`UserPromptSubmit` firing the predecessor's own
# hooks still produce right up until its pane is killed. Ordinarily
# `bin/co-rollover`'s close-out (killing the predecessor's pane) ends it, by
# ending the session that would otherwise keep re-reading this file —
# `clear_rolling` below is the operator's manual escape hatch for every other
# case (a close-out that never fires because the successor never ran
# `--picked-up`, a `mark_rolling` call against the wrong session), since the
# fence itself has no built-in way out.
#
# For `parked`, there is no automatic retry (ADR-0069/epic comment 2402
# ruling (b)) — a session whose successor spawn failed has nothing new to
# recompute until an operator resolves the parked session, and letting
# `evaluate()` recompute a fresh zone from `ctx_pct` would tell it to seek or
# roll over again immediately, right back into the same failure. Unlike
# `rolling`, `parked` is NOT a fence: `tools/rollover-trigger.py`'s
# `PreToolUse` branch only denies on `rolling`, so a parked session's own
# tool calls are unaffected — it stays fully usable while it waits for the
# operator. `clear_parked` below is the manual way out, mirroring
# `clear_rolling`.


def mark_rolling(session_id, *, predecessor: dict, successor: dict,
                  successor_window: int, state_dir=None, now: float | None = None) -> dict:
    """Set *session_id*'s rollover state to `rolling`, recording both sides of
    the handoff. Called once, by the predecessor's own `bin/co-rollover`
    invocation, right after it confirms the successor came up and has
    `select-window`ed to it.

    `predecessor`/`successor` are `{"session_id", "pane_id"}` dicts;
    `successor["session_id"]` is None here (the successor has not registered
    yet) and is filled in later by `record_successor_session_id`.
    """
    now_ts = now if now is not None else time.time()
    new_state = {
        "state": "rolling", "reason": "rollover", "since": now_ts,
        "calls_seeking": 0, "cap": None, "policy": load_policy(),
        "stop_blocked": False, "natural_state": "rolling",
        "natural_reason": "rollover", "vetoed_until": None,
        "predecessor": predecessor, "successor": successor,
        "successor_window": successor_window, "roll_now_reissues": 0,
        "seeking_stop_reissues": 0,
        # CARRIED FORWARD WHERE EVERY OTHER FIELD HERE IS RESET, because it is
        # the one field in this literal that is not rollover state. A session's
        # permission mode does not change because the session started rolling,
        # and this function writes a fresh literal rather than reading prior —
        # so without this line the value is not reset for the duration of the
        # roll, it is LOST, and `evaluate`'s sticky fallback then has a `None`
        # prior to restore from.
        "permission_mode": read_state(session_id, state_dir)["permission_mode"],
    }
    _atomic_write(_state_path(session_id, state_dir), new_state)
    return new_state


def clear_rolling(session_id, state_dir=None) -> bool:
    """Remove a `rolling` state, if one is set — `session-agent rollover
    <name> clear`'s other half (alongside `clear_override`, below), and the
    only way a session fenced by `rolling` with no successor ever coming to
    close it out gets back to normal. Returns whether a `rolling` state was
    actually cleared: a session that is not `rolling` (or has no state file
    at all) is left untouched, so `clear` never invents a zone that was never
    there. `evaluate()`'s next firing recomputes a fresh zone from `ctx_pct`,
    same as any session with no persisted state."""
    if read_state(session_id, state_dir)["state"] != "rolling":
        return False
    try:
        _state_path(session_id, state_dir).unlink()
        return True
    except FileNotFoundError:
        return False


def mark_parked(session_id, *, reason: str, handoff_path: str, issue_id: str | None,
                issue_status: str | None = None, issue_product: str | None = None,
                state_dir=None, now: float | None = None) -> dict:
    """Set *session_id*'s rollover state to `parked` — three callers, all with
    no automatic retry (ADR-0069/epic comment 2402 ruling (b)): the
    predecessor's own `bin/co-rollover` invocation, when the successor spawn
    fails (`CoRolloverError` from `spawn_successor`, or `verify_up` timing out
    or matching a known failure signature); `tools/rollover-trigger.py`'s
    `_park_declined`, when a session told `roll_now` declines past
    `ROLL_NOW_REISSUE_LIMIT` re-issues without ever invoking `/rollover` at
    all (issue_01M2HTBBHK2W4EH86RAFE0YBPY); and that file's `_park_stuck`,
    when a `seeking` session is told to seek on
    `SEEKING_STOP_REISSUE_LIMIT` consecutive `Stop` firings with no tool call
    and no operator prompt in between, which is a loop the hook is itself
    driving (issue_01M2PFRWZT27XY6ZREVWWNQ9SZ).

    `reason` names which path fired — `bin/co-rollover`'s own
    `_REASON_SPAWN_ERROR`/`_REASON_VERIFY_UP_FAILED`, or
    `tools/rollover-trigger.py`'s `REASON_DECLINED`/`REASON_STUCK`;
    `handoff_path` and `issue_id` are what an operator needs to find the
    session and the park's signal. `handoff_path` is `""` for both of that
    file's paths — no handoff.md exists, since neither reached `co-rollover`
    at all. `issue_id` is always `None`: a park logs an alert signal, and a
    signal has no id. A parked session is recoverable and a lost park is not
    (`bin/co-rollover`'s own docstring), so the park happens whatever the
    logging returns.

    `issue_status` IS WHY THE LOGGING WENT THE WAY IT DID. `signal_client.post_alert`
    never raises: a wire error comes back as `error:<code>`, a missing token as
    `skip`. Without the status, a failed log and a successful one leave the same
    evidence in this file. Deck's rail shows `issue_id` as "an issue is waiting on
    you", which a park no longer satisfies. `None` means the caller predates this
    field, not that logging succeeded.

    Same atomic-write and never-raises posture as `mark_rolling` (`_atomic_
    write` swallows its own `OSError`) — see that function's docstring for
    why a failure here must never be the thing that leaves a session
    unparked with nothing recording why it stopped.
    """
    now_ts = now if now is not None else time.time()
    new_state = {
        "state": "parked", "reason": reason, "since": now_ts,
        "calls_seeking": 0, "cap": None, "policy": load_policy(),
        "stop_blocked": False, "natural_state": "parked",
        "natural_reason": reason, "vetoed_until": None,
        "predecessor": None, "successor": None, "successor_window": None,
        "handoff_path": str(handoff_path), "issue_id": issue_id,
        "issue_status": issue_status, "issue_product": issue_product,
        "roll_now_reissues": 0, "seeking_stop_reissues": 0,
        # Carried forward for the reason mark_rolling's twin gives, and it
        # BITES HARDER HERE: `parked` is a terminal fence `evaluate` never
        # resolves past, so a parked session gets no later firing to report its
        # mode again. Dropped here, the field would read `None` for the whole
        # time the session sits waiting on its operator — which is exactly the
        # session a rail most needs to describe correctly.
        "permission_mode": read_state(session_id, state_dir)["permission_mode"],
    }
    _atomic_write(_state_path(session_id, state_dir), new_state)
    return new_state


def clear_parked(session_id, state_dir=None) -> bool:
    """Remove a `parked` state, if one is set — `session-agent rollover
    <name> clear`'s other half alongside `clear_rolling`, and the operator's
    way to un-park a session once the parked session's problem is resolved. Same contract
    as `clear_rolling`: returns whether a `parked` state was actually
    cleared, and leaves any other state untouched."""
    if read_state(session_id, state_dir)["state"] != "parked":
        return False
    try:
        _state_path(session_id, state_dir).unlink()
        return True
    except FileNotFoundError:
        return False


def record_current_permission_mode(session_id, permission_mode: str | None, *,
                                    state_dir=None) -> dict:
    """Persist *permission_mode* as this session's CURRENT mode, called from
    `tools/rollover-trigger.py`'s `PreToolUse` branch — the one hook event
    Claude Code fires immediately before the tool call it describes actually
    runs, which is what makes it the freshest source available. `Stop` and
    `UserPromptSubmit` (what `evaluate` above already records the field
    from) can go stale the moment an operator toggles Shift+Tab, since that
    fires no hook at all; a `PreToolUse` reading taken at the instant
    `bin/co-rollover` itself is about to run is never behind that toggle
    (ADR-0069's 2026-09-18 correction, "the mechanism trap").

    A no-op, returning `prior` unchanged, when *permission_mode* is None (the
    payload carried none — not every `PreToolUse` firing does) or when the
    session is already `rolling`/`parked`: both are terminal states
    `mark_rolling`/`mark_parked` already carry `permission_mode` forward
    into from `read_state` at the moment they are called, and a later
    `PreToolUse` firing for that same (now fenced, or waiting-on-operator)
    session has nothing to add and must not rewrite the fields those
    functions own.
    """
    if permission_mode is None:
        return read_state(session_id, state_dir)
    prior = read_state(session_id, state_dir)
    if prior["state"] in ("rolling", "parked") or prior["permission_mode"] == permission_mode:
        return prior
    new_state = {**prior, "permission_mode": permission_mode}
    _atomic_write(_state_path(session_id, state_dir), new_state)
    return new_state


def record_successor_session_id(session_id, successor_session_id: str, *,
                                 state_dir=None) -> dict:
    """Fill in the successor's own session id on an already-`rolling` state,
    once it has registered and confirmed pickup (`bin/co-rollover
    --picked-up`). A no-op returning the state unchanged if *session_id* is not
    (or is no longer) `rolling` — the successor's own report must never
    manufacture a rolling state that `mark_rolling` never set."""
    state = read_state(session_id, state_dir)
    if state["state"] != "rolling":
        return state
    successor = dict(state.get("successor") or {})
    successor["session_id"] = successor_session_id
    state = {**state, "successor": successor}
    _atomic_write(_state_path(session_id, state_dir), state)
    return state


# --- the operator override (Slice 3, ADR-0069's Deck write side) -----------
#
# `session-agent rollover <name> veto|force|seek|clear` is the only writer of
# this file; `evaluate` below is the only reader. Keyed by session id, same as
# the vitals and state files, so an override never carries over to a rollover's
# successor — a fresh `co -r` gets a fresh session id and reads no override at
# all until Deck sets one against IT.
OVERRIDE_KINDS = ("veto", "force", "seek")


def _override_path(session_id, override_dir=None) -> Path:
    d = Path(override_dir) if override_dir is not None else CACHE_DIR
    return d / f"rollover-override-{_key(session_id)}.json"


def read_override(session_id, override_dir=None) -> dict | None:
    """The live override, or None — nothing written, an unreadable file, or a
    `kind` outside the closed set. Re-read fresh every call, same as
    `load_policy` and `read_state`: `session-agent` is the only writer, so a
    veto/force/seek/clear reaches the very next hook firing with no restart."""
    key = _key(session_id)
    if not key:
        return None
    reading = _read_json(_override_path(session_id, override_dir))
    if reading is None or reading.get("kind") not in OVERRIDE_KINDS:
        return None
    return reading


def write_override(session_id, kind: str, *, zone: str | None = None,
                    override_dir=None, now: float | None = None) -> dict:
    """Write the override, atomically. `zone` is the natural zone a `veto` is
    suppressing — the caller's job to supply, since only it knows the
    session's current reading (`session_state.rollover_command` reads it from
    `read_state` before calling this) — and is ignored for every other kind."""
    if kind not in OVERRIDE_KINDS:
        raise ValueError(f"unknown override kind: {kind!r}")
    data = {"kind": kind, "zone": zone if kind == "veto" else None,
             "written_at": now if now is not None else time.time()}
    _atomic_write(_override_path(session_id, override_dir), data)
    return data


def clear_override(session_id, override_dir=None) -> bool:
    """Remove the override file, if one exists. Returns whether one was
    actually removed, so `clear` on a session with no override can say so."""
    try:
        _override_path(session_id, override_dir).unlink()
        return True
    except FileNotFoundError:
        return False


# Which zones a veto recorded in `veto_zone` still covers — a direct table,
# read rather than derived. `ok` covers `ok` and `seeking` (the operator, 2026-10-01:
# "we need to add veto before the looking starts, because often the rollover
# window is found in a split second", so a veto recorded before the seek
# threshold is crossed still applies once seeking starts —
# issue_01M3WX4443DF30SJ3WM5QHA7RJ). `seeking` covers only `seeking`, exactly
# as before. `roll_now` covers `roll_now` itself — restored here after a
# round that read the operator's ruling (d) too broadly and dropped it: an operator
# vetoing a rollover that is already due holds it until manually cleared,
# since `roll_now` is the top of the ladder `evaluate` climbs and there is no
# further limit past it to wait for (main's original behaviour).
#
# Ruling (d) ("a veto holds until the session crosses its next limit",
# platform/decisions/0069) is what this table enforces for `ok` and
# `seeking`: a veto recorded in either does not survive a crossing that moves
# the natural zone PAST the zone(s) it covers, and reaching `roll_now` from
# `ok` or `seeking` is exactly that crossing — so neither row lists
# `roll_now`. A veto recorded AT `roll_now` is a different case: nothing
# moves the natural zone past `roll_now` (it is the ceiling), so there is no
# crossing for the ruling to apply to, and the veto holds the same way main
# always let it.
#
# TUPLES OF TUPLES, NOT A DICT OF FROZENSETS, AND THE LOOKUP BELOW USES `==`
# ONLY (never `in` against a dict or a set) — a dict lookup and a frozenset
# membership test both hash their operand, and `veto_zone`/`natural_zone` can
# be whatever JSON value sits in the override or vitals file, including an
# unhashable list or dict (round 2 finding on this PR: no code path writes
# one today, but `TypeError: unhashable type` here is uncaught by the hook's
# `except BaseException` catch-all, and the session's rollover enforcement
# goes silent). Main's own comparison (`override.get("zone") ==
# natural_zone`) was total over every JSON value because `==` never raises;
# this table keeps that totality rather than reintroducing a way to lose it.
_VETO_COVERS = (
    ("roll_now", ("roll_now",)),
    ("ok", ("ok", "seeking")),
    ("seeking", ("seeking",)),
)


def _veto_covers(veto_zone: str | None, natural_zone: str) -> bool:
    return any(veto_zone == zone and natural_zone in covered
               for zone, covered in _VETO_COVERS)


def _vetoed_until(veto_zone: str | None, natural_reason: str | None, policy: dict) -> str | None:
    """Which policy key names the limit a veto recorded in `veto_zone` holds
    until — for display only; `evaluate`'s own re-arming (below) compares
    zones directly, through `_veto_covers`, and does not call this.

    A veto recorded in `ok` covers `seeking` too (`_veto_covers`), so
    `seek_pct` is never the answer: crossing it does not lift the veto
    anymore, the floor or the call cap does — the same target a veto
    recorded in `seeking` already named. A null `floor_pct` falls through to
    `seek_call_cap`, and a null cap too leaves this None: "the verbs must
    work when they're null" (the operator's ruling (a)) means a veto held with
    neither limit measured yet has nothing named to wait for — exactly as
    inert as the limits themselves.

    A veto recorded in `roll_now` falls through the `veto_zone not in ("ok",
    "seeking")` check below to the same bare `None` — there is no further
    limit for a veto recorded at the ceiling to wait for, the identical
    answer main gave for this case (its `_NEXT_LIMIT_FOR_ZONE` had no entry
    for `roll_now` either). This is not the `operator_seek` short-circuit
    below; it falls through on the zone check alone.

    `natural_reason == "operator_seek"` is the other source of a bare `None`:
    that reason means the zone being vetoed is only "seeking" because an
    operator's `seek` lowered the threshold, not because the reading crossed
    it — no limit was crossed to get here, so there is none to name as what
    lifts the veto. Naming `floor_pct` anyway would promise the veto holds
    until a real ctx crossing that has nothing to do with why this veto was
    set (issue_01M2HTBMRN1W688EV40DCDVXB7)."""
    if natural_reason == "operator_seek":
        return None
    if veto_zone not in ("ok", "seeking"):
        return None
    if policy.get("floor_pct") is not None:
        return "floor_pct"
    if policy.get("seek_call_cap") is not None:
        return "seek_call_cap"
    return None


def resolve_override(natural_zone: str, natural_reason: str | None,
                      override: dict | None, policy: dict) -> tuple[str, str | None, str | None]:
    """`(zone, reason, vetoed_until)` after folding *override* onto the
    natural zone/reason — the SINGLE place that applies an override, so
    `evaluate` (which computes `natural_zone` live, from a fresh `ctx_pct`)
    and `session_state.rollover` (which has no fresher context than the
    hook's last persisted `natural_state`/`natural_reason`, and must never
    invent one — an override changes what the session is TOLD, not the
    context reading it was read against) can never disagree about what an
    override does once it is written.

    `force` is unconditional, from any `natural_zone`. `veto` fires while
    `_veto_covers` says its recorded zone still covers `natural_zone`, by the
    table there: a veto recorded in `ok` covers `ok` and `seeking` (the operator,
    2026-10-01: the window to veto is often found in the split second before
    seeking starts, so a veto that stopped applying the instant seeking began
    would arrive too late), a veto recorded in `seeking` covers only
    `seeking`, as before, and a veto recorded in `roll_now` covers `roll_now`
    itself — an operator vetoing a rollover that is already due holds it
    until manually cleared, the same as main always did. Ruling (d) ("a veto
    holds until the session crosses its next limit") is what keeps a veto
    recorded in `ok` or `seeking` from also covering `roll_now`: reaching it
    from either zone moves the natural zone PAST what the veto covers, and
    that crossing is the limit the ruling means. It says nothing about a veto
    recorded AT `roll_now`, where there is no further crossing to move past.
    `seek` promotes `ok` to `seeking` (mirroring what
    `evaluate` computes about the reading it holds, so a caller with no
    reading at all still reports the same thing `evaluate` would); it neither
    downgrades `roll_now` nor invents a reason where the zone is already
    `seeking` on its own. `None` (no override, or one that does not apply)
    returns the natural pair unchanged, byte-for-byte — the pass-through this
    function's callers relied on before overrides existed at all.
    """
    if override is None:
        return natural_zone, natural_reason, None
    kind = override.get("kind")
    if kind == "force":
        return "roll_now", "operator_force", None
    if kind == "veto" and _veto_covers(override.get("zone"), natural_zone):
        return "vetoed", "operator_veto", _vetoed_until(override.get("zone"), natural_reason, policy)
    if kind == "seek" and natural_zone == "ok":
        return "seeking", "operator_seek", None
    return natural_zone, natural_reason, None


def evaluate(session_id, event_name: str, *, agent_running: bool = False,
             permission_mode: str | None = None,
             policy_path=None, vitals_dir=None, state_dir=None,
             override_dir=None, now: float | None = None) -> dict:
    """The single evaluator: reads the session's vitals and its own prior
    state, decides the zone, persists it, and returns it.

    `event_name` is one of `PostToolUse`, `Stop`, `UserPromptSubmit` — anything
    else is treated like the other two (evaluate and persist, never count).

    `agent_running` is the caller's answer to "does this Stop payload's
    `background_tasks` show a live agent" (an `agent_type` field on any entry).
    It matters only for the `Stop`-blocks-once decision below; PostToolUse and
    UserPromptSubmit ignore it.

    Returns a dict shaped like the persisted state plus two call-specific
    facts that are never persisted: `ctx_pct` (what this call read) and
    `should_block_stop` (whether THIS call is the one that should block).

    HONOURING THE OVERRIDE (Slice 3). `session-agent rollover` is the only
    writer of the override file this reads (`read_override`); this function
    and `session_state.rollover` (which resolves a FRESH override against
    THIS function's last persisted `natural_state`/`natural_reason`, so an
    operator's veto/seek/clear is visible before the session's own next hook
    firing — issue_01M2HTBMRN1W688EV40DCDVXB7) are the only readers, and both
    fold an override in through the same `resolve_override`, below. `force`
    is unconditional and reported regardless of `ctx_pct` — an operator's
    "roll over now" does not wait on a context reading that may never come.
    `seek` lowers the effective seek threshold to whatever this call reads
    (or, absent any reading at all, to the policy's own `seek_pct`, so the
    very first tool call after it is armed still lands in the seeking
    branch), which is what "armed... takes effect on the next tool call"
    means operationally: no second copy of the threshold logic, one number
    nudged down. `veto` is applied AFTER the ordinary computation below, by
    checking (`_veto_covers`) its recorded zone against `natural_state` — the
    zone this call would report with NO override at all. A veto recorded in
    `ok` also covers `seeking`, so a veto set before the seek threshold is
    crossed still suppresses once the session starts seeking
    (issue_01M3WX4443DF30SJ3WM5QHA7RJ: "we need to add veto before the
    looking starts, because often the rollover window is found in a split
    second"). A veto recorded in `roll_now` covers `roll_now` itself: an
    operator vetoing a rollover that is already due holds it until manually
    cleared, the same as main always did. A veto recorded in `ok` or
    `seeking` stops suppressing the instant the floor or the cap would
    independently push the natural zone PAST it to `roll_now` (the operator's ruling
    (d): a veto holds until the session crosses its next limit, and that
    crossing is the limit) — it does not stop suppressing a `roll_now` it
    was itself recorded in, since nothing moves the natural zone past
    `roll_now`.
    """
    now_ts = now if now is not None else time.time()
    policy = load_policy(policy_path)
    prior = read_state(session_id, state_dir)

    # `rolling` AND `parked` ARE TERMINAL (Slices 5 and 6) — see the section
    # above `mark_rolling`. Reached on every later PostToolUse/Stop/
    # UserPromptSubmit the session's own hooks still fire; without this, the
    # very next one would recompute a zone from `ctx_pct` and overwrite
    # either state (and, for `rolling`, the successor/predecessor pane info
    # with it) — silently un-fencing a `rolling` session, or telling a
    # `parked` one to seek or roll over again with no operator having acted.
    # Nothing is written; `prior` is returned exactly as stored.
    if prior["state"] in ("rolling", "parked"):
        return {**prior, "ctx_pct": read_ctx_pct(session_id, vitals_dir, now_ts),
                "should_block_stop": False}

    ctx_pct = read_ctx_pct(session_id, vitals_dir, now_ts)
    override = read_override(session_id, override_dir)
    override_kind = override.get("kind") if override else None

    effective_pct = ctx_pct
    if effective_pct is None and override_kind == "seek":
        effective_pct = policy["seek_pct"]

    if effective_pct is None:
        # NO ACTION ON THE ORDINARY PATH: nothing is written, and the reported
        # zone is `ok` — a session this evaluator cannot see is not one it can
        # call "seeking" without guessing. `calls_seeking` is still reported so
        # a caller mid-episode can display it, but it is not touched.
        #
        # `force` IS THE ONE EXCEPTION, and it still needs no reading: an
        # operator's override is reported and persisted exactly as it would be
        # below, just without a live `ctx_pct` to fold into the threshold
        # branches that never run here.
        if override_kind == "force":
            since = prior.get("since")
            if prior["state"] != "roll_now" or since is None:
                since = now_ts
            # `force` always announces (the one exception the hook's mode
            # gate carries too), so every firing here is a real re-issue.
            new_state = {
                "state": "roll_now", "reason": "operator_force", "since": since,
                "calls_seeking": prior["calls_seeking"], "cap": policy["seek_call_cap"],
                "policy": policy, "stop_blocked": False,
                "natural_state": prior.get("natural_state", prior["state"]),
                "natural_reason": prior.get("natural_reason", prior["reason"]),
                "vetoed_until": None,
                "roll_now_reissues": prior.get("roll_now_reissues", 0) + 1,
                # 0 for the same reason the twin below zeroes it outside
                # `seeking`: this zone is `roll_now`, and the seeking counter
                # only ever counts within a seeking episode.
                "seeking_stop_reissues": 0,
                # Never rolling or parked here — see `new_state`'s twin below.
                "predecessor": None, "successor": None, "successor_window": None,
                "handoff_path": None, "issue_id": None, "issue_status": None,
                "issue_product": None,
                # Sticky, exactly as in the twin below: a firing carrying no
                # `permission_mode` must not erase the last one reported.
                "permission_mode": (permission_mode if permission_mode is not None
                                    else prior.get("permission_mode")),
            }
            if new_state != prior:
                _atomic_write(_state_path(session_id, state_dir), new_state)
            return {**new_state, "ctx_pct": None, "should_block_stop": False}
        return {
            "state": "ok", "reason": None, "since": None,
            "calls_seeking": prior["calls_seeking"], "cap": policy["seek_call_cap"],
            "policy": policy, "ctx_pct": None, "should_block_stop": False,
            # REPORTED BUT NOT PERSISTED, and the asymmetry is this path's own
            # discipline rather than an oversight: it writes nothing, because a
            # session whose vitals this evaluator cannot read is not one it can
            # make claims about. Reporting costs nothing and keeps all three of
            # `evaluate`'s returns the same shape — the key was absent here
            # while present on the other two, which is the same trap the early
            # seed in `session_state` had.
            #
            # The consequence to know: under vitals older than
            # `VITALS_STALE_SECONDS`, a mode reported on this firing reaches the
            # caller and does NOT reach disk, so the states poll still shows the
            # previously persisted value until a firing with fresh vitals lands.
            # The field lags by one firing there. Best-effort, not sticky, here.
            "permission_mode": (permission_mode if permission_mode is not None
                                else prior.get("permission_mode")),
        }

    seek_pct = policy["seek_pct"]
    if override_kind == "seek":
        # Never ABOVE the reading, so the branch below always takes it — this
        # is the whole mechanism, not a special case inside it.
        seek_pct = min(seek_pct, effective_pct)
    floor_pct = policy["floor_pct"]
    cap = policy["seek_call_cap"]
    # `natural_state`, NOT `state`: the counter's "was I already seeking"
    # check has to ask what was REALLY true last call, not what an active veto
    # told Deck last call — see the docstring above and `DEFAULT_STATE`'s own
    # comment on this field.
    prior_natural = prior.get("natural_state", prior["state"])
    should_block_stop = False

    if floor_pct is not None and effective_pct >= floor_pct:
        natural_zone, natural_reason = "roll_now", "floor"
        calls_seeking = prior["calls_seeking"]
        stop_blocked = False  # roll_now never blocks; a future seeking episode gets a fresh chance
    elif effective_pct >= seek_pct:
        calls_seeking = prior["calls_seeking"] if prior_natural in ("seeking", "roll_now") else 0
        if event_name == _COUNTS_AS_A_CALL:
            calls_seeking += 1
        if cap is not None and calls_seeking >= cap:
            natural_zone, natural_reason = "roll_now", "cap"
            stop_blocked = False
        else:
            natural_zone, natural_reason = "seeking", "threshold"
            if override_kind == "seek" and (ctx_pct is None or ctx_pct < policy["seek_pct"]):
                # The REAL reading would not have reached the REAL threshold
                # without the override — say so, rather than "threshold",
                # which would otherwise be true, in this call's local frame,
                # by construction.
                natural_reason = "operator_seek"
            if event_name == "Stop" and not agent_running and not prior.get("stop_blocked"):
                should_block_stop = True
                stop_blocked = True
            else:
                stop_blocked = bool(prior.get("stop_blocked"))
    else:
        natural_zone, natural_reason = "ok", None
        calls_seeking = 0
        stop_blocked = False

    # `resolve_override` is the SAME function `session_state.rollover` calls
    # at read time against a PERSISTED `natural_state`/`natural_reason` — see
    # its docstring for why one function has to decide this for both callers.
    # An override_kind == "veto" whose recorded zone `_veto_covers` no longer
    # covers has been crossed past — the session reached `roll_now` from `ok`
    # or `seeking` since the veto was set (those two cover `seeking` but not
    # `roll_now` — `_veto_covers`'s table), or (rarer) dropped back to a
    # better zone than the one covered — and `resolve_override` returns the
    # natural pair unchanged in that case. A veto recorded IN `roll_now`
    # keeps covering it, since nothing moves the natural zone past `roll_now`
    # to cross past. The override file itself is left alone; `read_override`
    # will keep answering the same stale veto until `session-agent rollover
    # ... clear` removes it, which is harmless because this comparison
    # already ignores it.
    zone, reason, vetoed_until = resolve_override(natural_zone, natural_reason, override, policy)
    if override_kind == "force":
        stop_blocked = False
        should_block_stop = False
    elif override_kind == "veto" and zone == "vetoed":
        stop_blocked = False
        should_block_stop = False

    since = prior.get("since")
    if prior["state"] != zone or since is None:
        since = now_ts

    # `roll_now_reissues` counts REAL announcements only — the same
    # condition `tools/rollover-trigger.py` uses to decide whether to emit at
    # all (`mode == "enforce"`, or a `force` regardless of mode), so a
    # session sitting in `roll_now` under `observe` mode accrues nothing
    # until something is actually said to it. Anything else (a different
    # zone, or `roll_now` with nothing announced) resets to 0.
    announced = zone == "roll_now" and (policy["mode"] == "enforce" or reason == "operator_force")
    roll_now_reissues = (prior.get("roll_now_reissues", 0) + 1) if announced else 0

    # `seeking_stop_reissues` counts CONSECUTIVE `Stop` announcements while
    # seeking — the one signature a session looping on the hook's own output
    # still produces. `calls_seeking` cannot see that loop: it advances on
    # `_COUNTS_AS_A_CALL` firings alone, and a session emitting text with no
    # tool call never fires one, so `seek_call_cap` will not catch this shape
    # even once measurement sets it (issue_01M2PFRWZT27XY6ZREVWWNQ9SZ).
    #
    # A `PostToolUse` firing resets it (the session ACTED) and so does a
    # `UserPromptSubmit` (a human TYPED, so the turn ended for a reason that
    # is not this hook). What is left — `Stop` after `Stop` with neither in
    # between — is only reachable when something re-invokes the session the
    # moment it stops, which is what this hook does. Measured on the loop
    # this bounds: its hook stream is `Stop, SubagentStop, Stop,
    # SubagentStop, ...` ~1.7s apart with no `UserPromptSubmit` and no
    # `PostToolUse` between consecutive `Stop`s.
    #
    # `not agent_running` GATES THE INCREMENT, mirroring `should_block_stop`
    # above rather than diverging from it. A session waiting on a dispatched
    # agent is not the idle loop this bounds, and parking it would be the
    # worse failure — it would stop being told to roll over at all, mid
    # orchestration, with no bug present. This costs nothing on the measured
    # case: every `Stop` through that 9.5-minute spin carried
    # `background_tasks: []`, so the loop is still caught. A `Stop` while an
    # agent runs HOLDS the count rather than resetting it — the agent's own
    # completion wakes the session as a `UserPromptSubmit`, which resets.
    #
    # THE HOLD IS UNBOUNDED, AND THAT IS A STATED RESIDUAL RATHER THAN AN
    # OVERSIGHT. The same Stop-to-hook-text-to-Stop mechanism that produced
    # 58 re-issues is not structurally tied to an empty `background_tasks`:
    # if it ever coincides with a genuinely long-running agent, or with a
    # stale entry that never clears, the count sits frozen and the spend is
    # unbounded again. Nobody has observed that combination. It is left alone
    # deliberately, on two grounds: this branch is strictly better than what
    # it replaced, where `seeking` had no bound in ANY case; and the only fix
    # for it is to park a session waiting on a dispatched agent, which is the
    # exact harm the gate above exists to prevent, so buying it would cost a
    # worse failure than it prevents. A second, higher ceiling on the hold
    # was considered and rejected as a threshold nobody measured.
    #
    # WHAT WOULD CHANGE THE ANSWER: one observed session looping here with a
    # live `background_tasks` entry. That is a cheap thing to see, because
    # `_park_stuck`'s logged signal names the session and the hook log records
    # the payload — so the evidence arrives on its own if the case is real.
    if zone != "seeking" or event_name in (_COUNTS_AS_A_CALL, "UserPromptSubmit"):
        seeking_stop_reissues = 0
    elif event_name == "Stop" and policy["mode"] == "enforce" and not agent_running:
        seeking_stop_reissues = prior.get("seeking_stop_reissues", 0) + 1
    else:
        seeking_stop_reissues = prior.get("seeking_stop_reissues", 0)

    new_state = {
        "state": zone, "reason": reason, "since": since,
        "calls_seeking": calls_seeking, "cap": cap, "policy": policy,
        "stop_blocked": stop_blocked, "natural_state": natural_zone,
        "natural_reason": natural_reason, "vetoed_until": vetoed_until,
        # STICKY: a firing that carries no `permission_mode` (not every event
        # does) must not erase the last one reported, or an idle session in
        # plan mode would flicker back to looking ordinary on the next
        # unrelated firing.
        "permission_mode": (permission_mode if permission_mode is not None
                            else prior.get("permission_mode")),
        "roll_now_reissues": roll_now_reissues,
        "seeking_stop_reissues": seeking_stop_reissues,
        # `evaluate()` never produces `rolling` or `parked` itself (only
        # `mark_rolling`/`mark_parked` do, and the short-circuit above is what
        # protects either once set), so every zone this function computes
        # clears both states' own fields rather than carrying stale ones
        # forward.
        "predecessor": None, "successor": None, "successor_window": None,
        "handoff_path": None, "issue_id": None, "issue_status": None,
        "issue_product": None,
    }
    # This fires on every PostToolUse/Stop/UserPromptSubmit in every session on
    # the machine, and most of a session's life is spent with the zone
    # unchanged — so skip the write when nothing in the persisted state
    # actually moved. `calls_seeking` is part of this comparison, so a call
    # that advances the counter while seeking still writes.
    if new_state != prior:
        _atomic_write(_state_path(session_id, state_dir), new_state)

    return {**new_state, "ctx_pct": ctx_pct, "should_block_stop": should_block_stop}
