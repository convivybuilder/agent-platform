#!/usr/bin/python3
"""rollover-trigger — the hook half of ADR-0069's self-rollover.

Fires on `PostToolUse`, `Stop`, `UserPromptSubmit` and (Slice 5) `PreToolUse`.

On the first three, each firing calls `rollover_policy.evaluate()`, which
reads the session's vitals, updates and persists the per-session rollover
state, and returns the zone. This file is purely a thin formatter over that
result: in `observe` mode (the policy file's default) it writes the state and
says nothing; in `enforce` mode it turns the zone into the JSON Claude Code's
hook protocol expects.

  * `seeking` — on any of the three events, `additionalContext` telling the
    session to look for a good moment and naming what that means (no
    Agent-tool subagent still running, no multi-step edit half applied), then
    run `/rollover`. Bounded: past `rollover_policy.
    SEEKING_STOP_REISSUE_LIMIT` consecutive `Stop` announcements, with no
    tool call and no operator prompt in between, this file parks the session
    and logs one operator signal instead of saying it again — see
    `_park_stuck` (issue_01M2PFRWZT27XY6ZREVWWNQ9SZ: a session told to seek
    was told again on every stop, 58 times in 9.5 minutes, because the only
    bound in this file guarded `roll_now` and the counter it reads is pinned
    at 0 in every other zone).
  * `roll_now` — `additionalContext` saying so plainly: run `/rollover` now.
    Bounded: past `rollover_policy.ROLL_NOW_REISSUE_LIMIT` real re-issues
    with nothing rolled, this file parks the session and logs one operator
    signal instead of saying it again — see `_park_declined` below
    (issue_01M2HTBBHK2W4EH86RAFE0YBPY: a session that declines a rollover in
    prose, rather than running `/rollover`, was told again on every firing
    forever, with no bound and nothing to stop it).
  * At `Stop`, specifically, while `seeking` and no agent is running: the
    stop is BLOCKED once (`{"decision": "block", ...}`) rather than merely
    told, so a session that would otherwise exit idle gets nudged to act
    first. `rollover_policy` tracks "once" per seeking episode itself
    (`stop_blocked`), so this file does not have to.

On `PreToolUse` (Slice 5, ADR-0069's fence): a session whose rollover state is
`rolling` — set by `bin/co-rollover` once its successor has come up and been
`select-window`ed to — has every tool call DENIED, unconditionally, in both
`observe` and `enforce` mode: the predecessor's own turn is over the moment it
hands off, whatever the platform-wide enforcement switch says. This branch
never calls `rollover_policy.evaluate()` (which would recompute a zone from
`ctx_pct` and, absent the terminal-state guard `evaluate()` itself also
carries, overwrite `rolling`) — it only reads the persisted state.

SAFETY CONTRACT, same as `tools/session-event-log.py` and
`tools/stop-unfulfilled-next-action.py`:

  1. ALWAYS exit 0. A `Stop` hook that exits non-zero, or a `PreToolUse` that
     exits non-zero rather than reporting its decision as JSON, would wedge
     every session it fires in.
  2. Fail open on anything unreadable: garbage stdin, a missing vitals file,
     an unreadable policy — every one of those is a reason to do nothing,
     never a reason to block or crash.
  3. Skip a session this hook must not act on, before touching any state:
     a dispatched worker (`AGENTCTL_RUN_ID` set), a session outside tmux (an
     automatic rollover needs a tmux pane to reattach into), and a subagent's
     own tool call reaching this hook through the PARENT session's PostToolUse
     stream. That last one is real and measured: a `PreToolUse` payload
     recorded on this machine from inside a dispatched Agent-tool subagent
     carried `"agent_id": "a7ffafc777248e22e"`, while every ordinary
     top-level tool call on the same machine carries no `agent_id` field at
     all (see the PR body for the redacted example). A subagent cannot run
     `/rollover` sensibly — it has no session of its own to roll — so this
     hook must not tell it to. The `PreToolUse` fence honours the same three
     skips before it looks at rollover state at all.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                 "..", "lib", "agent-platform"))
import signal_client as SC  # noqa: E402
import rollover_policy as RP  # noqa: E402

# ADR-0043: a black box crew logs no signals. `private_home_for` answers whether a cwd is inside a
# private crew: a path, None, or UNKNOWN when it cannot tell. A park is withheld from the signal
# store for both of the first two. UNKNOWN is not None, so it fails closed, as session-event-log does.
_UNKNOWN_HOME = object()
try:
    from private_crew import private_home_for as _private_home_for
except BaseException:
    # An import failure must not break the exit-0 contract. Every session then counts as private.
    def _private_home_for(cwd):  # noqa: ARG001 — signature parity with the shared version
        return _UNKNOWN_HOME


def _honour_root_config(argv: list) -> None:
    """FOR TESTS ONLY: `--root-config FILE` after the event name points the black-box gate at another
    machine root, as session-event-log's flag does. The installed hook never passes it, and the
    registry at the machine's real root is what it reads. A config that `root()` will not take at its
    word leaves every privacy answer UNKNOWN, which fails closed."""
    global _private_home_for
    if "--root-config" not in argv[2:]:
        return
    try:
        from pathlib import Path as _P
        import builder_root as _BRC
        import projects as _PJ
        _cfg = _P(argv[argv.index("--root-config", 2) + 1])
        _unhonoured = object()
        _root = _BRC.root(config=_cfg, fallback=_unhonoured)
        if _root is _unhonoured:
            raise ValueError(f"{_cfg} names no root this machine will take")
        _PJ.ROOT_OVERRIDE = _root
    except BaseException:
        def _private_home_for(cwd):  # noqa: ARG001, F811
            return _UNKNOWN_HOME

SEEKING_TEXT = (
    "context at {pct}%, roll over at the next good moment: no Agent-tool "
    "subagent of yours still running, no multi-step edit half applied; then "
    "run /rollover."
)
ROLL_NOW_TEXT = "run /rollover now."
STOP_BLOCK_REASON = (
    "this is a good moment: context at {pct}%, no dispatched agent still "
    "running. Roll over now: run /rollover."
)
# `%N` is tmux's own pane-id spelling (`%3`, not a bare `3`) — the successor's
# `pane_id` already carries the leading `%`, so this names the exact tmux
# target the operator would type to switch there.
ROLLING_FENCE_REASON = "this session rolled over to pane {pane}; end your turn"

# `mark_parked`'s existing reasons (`bin/co-rollover`) are `spawn_error` and
# `verify_up_failed` — both for a rollover that was ATTEMPTED and failed.
# This one is for a rollover never attempted at all: told `roll_now` past the
# bound, with no `/rollover` invocation in between.
REASON_DECLINED = "declined"
# The `seeking` sibling of `declined`: not a refusal, a loop. Kept distinct so
# an operator reading a parked session's state can tell "it said no" from "it
# could not get away from us" without opening the signal.
REASON_STUCK = "seeking_loop"
# A park runs inside a hook that Claude Code waits on, so the signal gets a 5s timeout per socket
# operation rather than the delivery budget. post_structured makes up to three attempts, so a store
# that stops answering costs about 19.5 seconds at most. A store that trickles bytes can exceed that,
# since urllib applies the timeout to each read.
_PARK_SIGNAL_TIMEOUT = 5
PARK_STREAM_STUCK = "co-rollover-park-seeking-loop"
PARK_STREAM_DECLINED = "co-rollover-park-declined"
# `medium` for a decline, matching `bin/co-rollover`'s spawn-failure park: a parked session is
# recoverable and stays fully usable, so the operator needs to know, but nothing is getting worse.
_DECLINE_SEVERITY = "medium"
# `high` for the loop, and the difference from `_DECLINE_SEVERITY` is that something WAS getting
# worse: the loop this parks re-read 740k cached tokens per firing to emit ten output tokens, about
# $13/minute, bounded only by whether an operator happened to be watching. Parking stops the spend,
# so the signal is a post-mortem rather than an emergency, and the cause is a live defect that will
# do it again to the next session.
_STUCK_SEVERITY = "high"
_NO_REASON_RECORDED = "declined without a recorded reason"


def _read_payload() -> dict:
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    try:
        payload = json.loads(raw)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _agent_running(payload: dict) -> bool:
    """True iff `background_tasks` names a live agent. Claude Code decorates a
    dispatched Agent-tool subagent's entry with a truthy `agent_type`; a plain
    background shell command carries none. See the plan's Slice 1: "background
    agents appear there with an agent_type field"."""
    tasks = payload.get("background_tasks")
    if not isinstance(tasks, list):
        return False
    return any(isinstance(t, dict) and t.get("agent_type") for t in tasks)


def _pct_str(pct) -> str:
    return f"{pct:.0f}" if isinstance(pct, (int, float)) else "?"


def _last_assistant_text(path) -> str:
    """The text of the final assistant message in the transcript at *path*, or
    ''. Same extraction `tools/stop-unfulfilled-next-action.py` already does
    for the identical `transcript_path` hook field — duplicated rather than
    imported, since that file is a script rather than a library and this
    module's own self-containment note (see `rollover_policy`'s docstring)
    applies here too. Anything unreadable (missing file, garbage JSON lines,
    no assistant message at all) returns '' rather than raising — the caller
    falls back to a fixed string, and this hook must never crash or block on
    a transcript it cannot read."""
    last = None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("type") == "assistant":
                    last = rec
    except OSError:
        return ""
    if last is None:
        return ""
    content = (last.get("message") or {}).get("content") or []
    parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
    return "\n".join(parts)


def _what_it_said(payload: dict) -> str:
    """What the session said instead of rolling — the last assistant text, or
    `_NO_REASON_RECORDED` when the payload carries no `transcript_path` or the
    transcript can't be read. Never raises."""
    path = payload.get("transcript_path")
    if not path:
        return _NO_REASON_RECORDED
    try:
        text = _last_assistant_text(path).strip()
    except Exception:
        return _NO_REASON_RECORDED
    return text or _NO_REASON_RECORDED


def _park_declined(session_id: str, payload: dict, result: dict) -> None:
    """Reached `rollover_policy.ROLL_NOW_REISSUE_LIMIT` real re-issues with no
    `/rollover` ever invoked. The session may have a good reason to decline —
    this records that for the operator rather than forcing anything (see
    `ROLL_NOW_REISSUE_LIMIT`'s own comment in `rollover_policy.py`) — and
    reuses Slice 6's machinery (`RP.mark_parked`, `SC.post_alert`) rather than
    a second path: the only new things are the `declined` reason and what the
    session said instead of rolling.

    Logging and parking are `_park_and_file`'s, shared with `_park_stuck`;
    see it for the `dedup_key` scoping and the never-raises posture.
    """
    said = _what_it_said(payload)
    pct = _pct_str(result.get("ctx_pct"))
    body = (
        f"A session told to roll over ({RP.ROLL_NOW_REISSUE_LIMIT}x) declined "
        "instead of running /rollover; there is no automatic retry, and the "
        "session has parked.\n\n"
        f"- session id: {session_id}\n"
        f"- context at last reading: {pct}%\n"
        f"- what it said instead of rolling:\n\n{said}\n\n"
        "The session stays live and usable — nothing is fenced — but it will "
        "not be told to roll over again on its own. Once this is resolved, "
        "run `session-agent rollover <name> clear` to un-park it."
    )
    _park_and_file(
        session_id, payload, result, reason=REASON_DECLINED,
        stream=PARK_STREAM_DECLINED,
        action=("Operator action: read what the session said below, then run `session-agent "
                "rollover <name> clear` to un-park it, or leave it parked on purpose."),
        once_prefix="rollover-declined",
        title="co-rollover: session declined a rollover, session parked",
        body=body, severity=_DECLINE_SEVERITY)


def _park_stuck(session_id: str, payload: dict, result: dict) -> None:
    """Reached `rollover_policy.SEEKING_STOP_REISSUE_LIMIT` consecutive `Stop`
    announcements while `seeking`, with no tool call and no operator prompt in
    between (issue_01M2PFRWZT27XY6ZREVWWNQ9SZ).

    THIS IS A DIFFERENT FAILURE FROM `_park_declined`, WHICH IS WHY IT FILES
    ITS OWN THING. A declining session weighed the instruction and said no —
    the signal asks an operator to read its reason. A session in this state
    has not necessarily decided anything: the shape says only that it is
    ending its turn, being told to seek a rollover moment, and ending its turn
    again, which is a loop this hook is itself driving. The measured one ran
    58 announcements in 9.5 minutes at about $13/minute and ended only when
    the operator typed `/exit`, so this is the costlier of the two parks.
    """
    said = _what_it_said(payload)
    pct = _pct_str(result.get("ctx_pct"))
    body = (
        f"A session was told to seek a rollover moment on "
        f"{RP.SEEKING_STOP_REISSUE_LIMIT} consecutive turn endings with no "
        "tool call and no operator prompt in between. That shape is a loop "
        "this hook is driving: the session stops, the hook speaks, the "
        "session is re-invoked, and it stops again.\n\n"
        f"- session id: {session_id}\n"
        f"- context at last reading: {pct}%\n"
        f"- what it last said:\n\n{said}\n\n"
        "Parking is what breaks the loop, so nothing further will be said to "
        "this session about rolling over. It stays live and usable and "
        "nothing is fenced. A session in this state often cannot satisfy the "
        "instruction at all. One failure mode worth checking first, because "
        "it is cheap to rule out: a rollover that already succeeded without "
        "recording state leaves the hook asking for something already done, "
        "so look for a live successor before telling this session to roll "
        "again. That is a lead rather than a diagnosis — it is what the one "
        "measured instance turned out to be, not something this parking "
        "establishes. Once resolved, run `session-agent rollover <name> "
        "clear` to un-park it."
    )
    _park_and_file(
        session_id, payload, result, reason=REASON_STUCK,
        stream=PARK_STREAM_STUCK,
        action=("Operator action: look for a live successor session first; then run "
                "`session-agent rollover <name> clear` to un-park this session."),
        once_prefix="rollover-seeking-loop",
        title="co-rollover: session looping on the seeking instruction, session parked",
        body=body, severity=_STUCK_SEVERITY)


def _park_and_file(session_id: str, payload: dict, result: dict, *, reason: str,
                   stream: str, action: str, once_prefix: str, title: str, body: str,
                   severity: str) -> None:
    """Shared by both park paths: log one alert signal, then park. The signal's first line is
    `action`, the operator's next step, so it reads first on a page that shows only that line.

    `dedup_key` is scoped to THIS episode (`session_id` plus the episode's own
    `since`, read from *result* rather than recomputed) — a fresh episode
    after `session-agent rollover <name> clear` gets its own key, so a session
    that hits the same wall again logs again rather than silently deduping
    against the first attempt forever. `once_prefix` keeps the two paths'
    keys distinct, so a session that declines and later loops is not deduped
    into one signal by a shared `since`.

    A session inside a private crew (`private_crew.private_home_for`, which
    answers UNKNOWN when it cannot tell) logs no signal, and the state records
    `issue_status: "black-box"`.

    Never raises: `SC.post_alert` already never raises (a down loopback or a
    missing token both come back as a status, not an exception), and this
    wraps it anyway so a future change there can't turn this hook's SAFETY
    CONTRACT (always exit 0) into a lie. `mark_parked` is called either way —
    a parked session with no signal is recoverable, a lost park is not.

    `handoff_path` is `""` for both: neither path reached `co-rollover`, so no
    handoff.md exists for `mark_parked` to point at.
    """
    product = os.path.basename(str(payload.get("cwd") or "")) or "unknown"
    dedup_key = f"{once_prefix}:{session_id}:{result.get('since')}"
    # THE GATE IS BEFORE ANY TEXT IS SENT. `body` quotes what the session said, and this hook is
    # registered GLOBALLY (~/.claude/settings.json), so it fires inside a black box crew's session
    # too. `payload.cwd` is the only thing that places a session in a crew. The park itself still
    # happens below; only the signal is withheld, and the state records why.
    # THE STATUS IS RECORDED. `post_alert` never raises and never prints, so a
    # wire error arrives as `error:<code>` and a missing token as `skip`. Taking
    # only success or failure would leave a failed log and a successful one
    # identical to anything reading the parked state, and this hook has no
    # stderr an operator reads — the state file is the only place the reason can
    # survive. `bin/co-rollover` keeps the same status; both paths feed one
    # `mark_parked`.
    issue_status = "unknown"
    try:
        if _private_home_for(payload.get("cwd")) is not None:
            issue_status = "black-box"
        else:
            issue_status = SC.post_alert(
                stream, SC.sanitise_body(f"{action}\n\n{title}\n\n{body}"),
                product=product, dedup_key=dedup_key, timeout=_PARK_SIGNAL_TIMEOUT,
                meta={"severity": severity})
    except Exception as exc:
        issue_status = f"error:{exc.__class__.__name__}"
    RP.mark_parked(session_id, reason=reason, handoff_path="", issue_id=None,
                   issue_status=issue_status, issue_product=product)


def _emit_context(event_name: str, text: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {"hookEventName": event_name, "additionalContext": text},
    }))


def _emit_block(reason: str) -> None:
    print(json.dumps({"decision": "block", "reason": reason}))


def _emit_deny(reason: str) -> None:
    """The `PreToolUse` deny shape (same convention as `tools/block-pr-merge.sh`
    and `tools/block-push-under-live-review.sh`): JSON on stdout, exit 0 —
    NOT `sys.exit(2)` with stderr, which this file's own outer
    `except BaseException: rc = 0` would swallow, silently turning the deny
    into a no-op."""
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
    }))


def main() -> int:
    _honour_root_config(sys.argv)
    payload = _read_payload()
    event = payload.get("hook_event_name")
    if not isinstance(event, str):
        event = sys.argv[1] if len(sys.argv) > 1 else ""

    if os.environ.get("AGENTCTL_RUN_ID"):
        return 0
    if not os.environ.get("TMUX"):
        return 0
    # The hooks reference (https://code.claude.com/docs/en/hooks.md) states this by
    # name for PostToolUse: "tool events such as PreToolUse and PostToolUse fire
    # the same configured hooks as in the main conversation, and the input
    # carries the agent_id and agent_type common input fields that identify the
    # subagent." Its Agent-Specific Fields table further notes agent_type is
    # present only "with --agent or inside a subagent" — not guaranteed
    # alongside agent_id — so this skip deliberately keys on agent_id alone.
    if payload.get("agent_id"):
        return 0

    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return 0

    # Slice 5's fence: a `rolling` session's own turn is over, unconditionally
    # — in both `observe` and `enforce` mode (unlike every zone below, which
    # `observe` mode suppresses). This reads `read_state` directly rather than
    # `RP.evaluate()`: `PreToolUse` is not one of the three events the zone
    # evaluator recomputes a zone from, and calling it here would do exactly
    # that, defeating the terminal-state guard `evaluate()` itself carries.
    if event == "PreToolUse":
        state = RP.read_state(session_id)
        if state.get("state") == "rolling":
            successor = state.get("successor") or {}
            pane = successor.get("pane_id") or "?"
            _emit_deny(ROLLING_FENCE_REASON.format(pane=pane))
            return 0
        # Recorded here, not via `RP.evaluate()` (which this branch must never
        # call — see the comment above the fence): a `PreToolUse` firing is
        # the freshest source `permission_mode` has, since it fires immediately
        # before the tool call it describes runs. When that tool call is
        # `bin/co-rollover` itself, this is what lets it read the predecessor's
        # CURRENT mode rather than whatever `Stop`/`UserPromptSubmit` last
        # reported — see `rollover_policy.record_current_permission_mode`.
        RP.record_current_permission_mode(session_id, payload.get("permission_mode"))
        return 0

    # `permission_mode` rides on `Stop` and `UserPromptSubmit` (and some others);
    # `evaluate` keeps the last non-None report, so a firing without one does not
    # erase it. Deck reads it to tell a session idle IN PLAN MODE from an idle one.
    result = RP.evaluate(session_id, event, agent_running=_agent_running(payload),
                         permission_mode=payload.get("permission_mode"))
    policy = result.get("policy") or {}
    # `force` is an operator's direct override (Slice 3), not the passive
    # threshold notification `observe` mode exists to suppress — Deck's
    # comment on the epic is explicit that "an operator's force sends in
    # either mode", so a forced `roll_now` still reaches the session here even
    # while the platform-wide switch is off. `veto`/`seek`/`clear` carry no
    # such exception and stay silent in `observe`, same as an unforced zone.
    if policy.get("mode") != "enforce" and result.get("reason") != "operator_force":
        return 0

    zone = result.get("state")
    if zone == "ok":
        return 0

    pct = _pct_str(result.get("ctx_pct"))

    if zone == "roll_now":
        if result.get("roll_now_reissues", 0) > RP.ROLL_NOW_REISSUE_LIMIT:
            _park_declined(session_id, payload, result)
            return 0
        if event in ("PostToolUse", "Stop", "UserPromptSubmit"):
            _emit_context(event, ROLL_NOW_TEXT)
        return 0

    if zone == "seeking":
        # Checked before anything is said, and safe to check on any event:
        # `evaluate` resets the counter to 0 on a `PostToolUse` or a
        # `UserPromptSubmit`, so a non-`Stop` firing can never arrive here
        # holding a count that survived the session doing something.
        if result.get("seeking_stop_reissues", 0) > RP.SEEKING_STOP_REISSUE_LIMIT:
            _park_stuck(session_id, payload, result)
            return 0
        if event == "Stop" and result.get("should_block_stop"):
            _emit_block(STOP_BLOCK_REASON.format(pct=pct))
            return 0
        if event in ("PostToolUse", "Stop", "UserPromptSubmit"):
            _emit_context(event, SEEKING_TEXT.format(pct=pct))
        return 0

    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except BaseException:
        # BaseException, not Exception: a `Stop` hook that lets SystemExit or
        # KeyboardInterrupt propagate can still take the whole firing down in
        # a way the harness reads as a crash rather than a no-op.
        rc = 0
    sys.exit(rc or 0)
