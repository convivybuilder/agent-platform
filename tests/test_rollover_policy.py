"""rollover_policy — the policy loader and zone evaluator (ADR-0069, Slice 1).

WHAT THESE PIN. The evaluator is the one place that decides whether a session
is `ok`, `seeking`, or `roll_now`, and it has to get three edges right: the
boundary of each limit (at/below/above), a `null` limit's total inertness (a
platform that has not measured a floor or a cap yet must never manufacture
one), and the call counter's scope — it counts `PostToolUse` firings while
`seeking`, never operator turns and never while `ok`. The policy file itself
is asserted to have no cache in front of it: an edit must reach the very next
call, because that is the whole point of re-reading it every time.
"""
from __future__ import annotations

import json
import math
import os
import time

import pytest

import rollover_policy as RP


def _write_vitals(tmp_path, session_id, pct, written_at=None, extra=None):
    data = {"written_at": written_at if written_at is not None else time.time(), "ctx_pct": pct}
    if extra:
        data.update(extra)
    (tmp_path / f"vitals-{session_id}.json").write_text(json.dumps(data))


def _write_policy(tmp_path, **fields):
    path = tmp_path / "rollover.json"
    path.write_text(json.dumps(fields))
    return path


def _evaluate(tmp_path, session_id, event, pct=None, written_at=None, policy=None,
              agent_running=False, now=None, permission_mode=None):
    if pct is not None:
        _write_vitals(tmp_path, session_id, pct, written_at)
    policy_path = _write_policy(tmp_path, **(policy or {"mode": "enforce"}))
    return RP.evaluate(session_id, event, agent_running=agent_running,
                       permission_mode=permission_mode,
                       policy_path=policy_path, vitals_dir=tmp_path,
                       state_dir=tmp_path, override_dir=tmp_path, now=now)


def _write_override(tmp_path, session_id, kind, zone=None):
    RP.write_override(session_id, kind, zone=zone, override_dir=tmp_path)


# --- load_policy ---------------------------------------------------------------

def test_defaults_when_the_file_does_not_exist(tmp_path):
    assert RP.load_policy(tmp_path / "nope.json") == RP.DEFAULT_POLICY


def test_defaults_when_the_file_is_not_json(tmp_path):
    p = tmp_path / "rollover.json"
    p.write_text("not json")
    assert RP.load_policy(p) == RP.DEFAULT_POLICY


def test_a_malformed_field_degrades_alone(tmp_path):
    """One bad field must not discard the rest of the file."""
    p = tmp_path / "rollover.json"
    p.write_text(json.dumps({"mode": "enforce", "seek_pct": "not a number",
                             "floor_pct": 95, "seek_call_cap": 40}))
    policy = RP.load_policy(p)
    assert policy["mode"] == "enforce"
    assert policy["seek_pct"] == RP.DEFAULT_POLICY["seek_pct"], "bad type falls back"
    assert policy["floor_pct"] == 95
    assert policy["seek_call_cap"] == 40


def test_an_unrecognised_mode_falls_back_to_observe(tmp_path):
    p = tmp_path / "rollover.json"
    p.write_text(json.dumps({"mode": "YOLO"}))
    assert RP.load_policy(p)["mode"] == "observe"


def test_null_limits_round_trip_as_null(tmp_path):
    p = tmp_path / "rollover.json"
    p.write_text(json.dumps({"floor_pct": None, "seek_call_cap": None}))
    policy = RP.load_policy(p)
    assert policy["floor_pct"] is None
    assert policy["seek_call_cap"] is None


# --- a hand-edited file can't darken the hook by putting seek_pct out of play -

def test_a_null_seek_pct_in_a_hand_edited_file_falls_back_to_default(tmp_path):
    """Unlike `floor_pct`/`seek_call_cap`, `seek_pct` is never inert — `evaluate`
    always compares against it, so a null here must degrade to the default
    rather than round-trip like the other two."""
    p = tmp_path / "rollover.json"
    p.write_text(json.dumps({"seek_pct": None}))
    assert RP.load_policy(p)["seek_pct"] == RP.DEFAULT_POLICY["seek_pct"]


def test_a_nan_seek_pct_in_a_hand_edited_file_falls_back_to_default(tmp_path):
    """`json.load` parses the non-standard `NaN` token by default, and NaN
    passes a bare `isinstance(x, (int, float))` check while making every
    `>=` comparison in `evaluate` false — silently disabling the threshold."""
    p = tmp_path / "rollover.json"
    p.write_text('{"seek_pct": NaN}')
    assert RP.load_policy(p)["seek_pct"] == RP.DEFAULT_POLICY["seek_pct"]


def test_an_infinite_seek_pct_in_a_hand_edited_file_falls_back_to_default(tmp_path):
    p = tmp_path / "rollover.json"
    p.write_text('{"seek_pct": Infinity}')
    assert RP.load_policy(p)["seek_pct"] == RP.DEFAULT_POLICY["seek_pct"]


def test_evaluate_survives_a_null_seek_pct_in_the_policy_file(tmp_path):
    """END TO END: before this fix, `evaluate` raised `TypeError` comparing
    `effective_pct >= None`, and the hook's blanket `except` swallowed it —
    rollover detection went dark with no operator-visible error. Now
    `load_policy` degrades the null before `evaluate` ever sees it, so the
    reading falls through to the DEFAULT seek threshold (70) instead."""
    result = _evaluate(tmp_path, "s1", "PostToolUse", pct=75,
                       policy={"mode": "enforce", "seek_pct": None})
    assert result["state"] == "seeking"


def test_evaluate_survives_a_nan_seek_pct_in_the_policy_file(tmp_path):
    """Same hazard, reached by `--set seek_pct=nan` (or a hand-edit) instead
    of `null`: NaN passed the old type check and made `effective_pct >=
    seek_pct` false at every reading, silently disabling the threshold."""
    result = _evaluate(tmp_path, "s1", "PostToolUse", pct=75,
                       policy={"mode": "enforce", "seek_pct": float("nan")})
    assert result["state"] == "seeking"


def test_editing_the_policy_file_changes_the_next_calls_output(tmp_path):
    """No cache: the SAME path, read twice, must answer differently after an
    edit in between — this is what "re-read on every call" means operationally."""
    p = tmp_path / "rollover.json"
    p.write_text(json.dumps({"seek_pct": 70}))
    assert RP.load_policy(p)["seek_pct"] == 70

    p.write_text(json.dumps({"seek_pct": 55}))
    assert RP.load_policy(p)["seek_pct"] == 55


# --- zone boundaries -------------------------------------------------------

@pytest.mark.parametrize("pct,expected", [(69, "ok"), (70, "seeking"), (71, "seeking")])
def test_seek_threshold_boundary(tmp_path, pct, expected):
    result = _evaluate(tmp_path, "s1", "PostToolUse", pct=pct,
                       policy={"mode": "enforce", "seek_pct": 70})
    assert result["state"] == expected


@pytest.mark.parametrize("pct,expected", [(94, "seeking"), (95, "roll_now"), (96, "roll_now")])
def test_floor_boundary(tmp_path, pct, expected):
    result = _evaluate(tmp_path, "s1", "PostToolUse", pct=pct,
                       policy={"mode": "enforce", "seek_pct": 70, "floor_pct": 95})
    assert result["state"] == expected


def test_floor_reason_is_floor(tmp_path):
    result = _evaluate(tmp_path, "s1", "PostToolUse", pct=96,
                       policy={"mode": "enforce", "seek_pct": 70, "floor_pct": 95})
    assert result["reason"] == "floor"


# --- null limits are inert --------------------------------------------------

def test_a_null_floor_never_forces_roll_now(tmp_path):
    """Even at 99.9%, with no floor configured, the zone stays `seeking`."""
    result = _evaluate(tmp_path, "s1", "PostToolUse", pct=99.9,
                       policy={"mode": "enforce", "seek_pct": 70, "floor_pct": None})
    assert result["state"] == "seeking"


def test_a_null_cap_never_forces_roll_now(tmp_path):
    result = None
    for _ in range(500):
        result = _evaluate(tmp_path, "s1", "PostToolUse", pct=80,
                           policy={"mode": "enforce", "seek_pct": 70, "seek_call_cap": None})
    assert result["state"] == "seeking"
    assert result["calls_seeking"] == 500


# --- missing or stale vitals produce no action ------------------------------

def test_missing_vitals_produce_the_ok_zone_and_write_nothing(tmp_path):
    result = RP.evaluate("ghost", "PostToolUse",
                         policy_path=_write_policy(tmp_path, mode="enforce"),
                         vitals_dir=tmp_path, state_dir=tmp_path)
    assert result["state"] == "ok"
    assert result["ctx_pct"] is None
    assert not (tmp_path / "rollover-ghost.json").exists(), "no action means no write"


def test_stale_vitals_are_treated_as_absent(tmp_path):
    now = time.time()
    _write_vitals(tmp_path, "s1", 90, written_at=now - RP.VITALS_STALE_SECONDS - 5)
    result = RP.evaluate("s1", "PostToolUse",
                         policy_path=_write_policy(tmp_path, mode="enforce", seek_pct=70),
                         vitals_dir=tmp_path, state_dir=tmp_path, now=now)
    assert result["state"] == "ok"
    assert result["ctx_pct"] is None


def test_a_fresh_reading_just_inside_the_staleness_window_is_honoured(tmp_path):
    now = time.time()
    _write_vitals(tmp_path, "s1", 90, written_at=now - RP.VITALS_STALE_SECONDS + 5)
    result = RP.evaluate("s1", "PostToolUse",
                         policy_path=_write_policy(tmp_path, mode="enforce", seek_pct=70),
                         vitals_dir=tmp_path, state_dir=tmp_path, now=now)
    assert result["state"] == "seeking"
    assert result["ctx_pct"] == 90


# --- the counter increments only while seeking ------------------------------

def test_the_counter_stays_at_zero_below_the_seek_threshold(tmp_path):
    for _ in range(5):
        result = _evaluate(tmp_path, "s1", "PostToolUse", pct=50,
                           policy={"mode": "enforce", "seek_pct": 70})
    assert result["calls_seeking"] == 0


def test_the_counter_advances_only_on_posttooluse(tmp_path):
    policy = {"mode": "enforce", "seek_pct": 70}
    r1 = _evaluate(tmp_path, "s1", "Stop", pct=80, policy=policy)
    assert r1["state"] == "seeking" and r1["calls_seeking"] == 0, "Stop must not advance the count"
    r2 = _evaluate(tmp_path, "s1", "UserPromptSubmit", pct=80, policy=policy)
    assert r2["calls_seeking"] == 0, "UserPromptSubmit must not advance the count either"
    r3 = _evaluate(tmp_path, "s1", "PostToolUse", pct=80, policy=policy)
    assert r3["calls_seeking"] == 1
    r4 = _evaluate(tmp_path, "s1", "PostToolUse", pct=80, policy=policy)
    assert r4["calls_seeking"] == 2


def test_dropping_below_seek_resets_the_counter(tmp_path):
    policy = {"mode": "enforce", "seek_pct": 70}
    for _ in range(3):
        result = _evaluate(tmp_path, "s1", "PostToolUse", pct=80, policy=policy)
    assert result["calls_seeking"] == 3

    result = _evaluate(tmp_path, "s1", "PostToolUse", pct=50, policy=policy)
    assert result["state"] == "ok" and result["calls_seeking"] == 0

    result = _evaluate(tmp_path, "s1", "PostToolUse", pct=80, policy=policy)
    assert result["calls_seeking"] == 1, "a fresh seeking episode starts its own count"


@pytest.mark.parametrize("cap,expected_zone", [(2, "roll_now"), (3, "seeking")])
def test_the_cap_boundary_forces_roll_now(tmp_path, cap, expected_zone):
    policy = {"mode": "enforce", "seek_pct": 70, "seek_call_cap": cap}
    result = None
    for _ in range(2):
        result = _evaluate(tmp_path, "s1", "PostToolUse", pct=80, policy=policy)
    assert result["state"] == expected_zone
    if expected_zone == "roll_now":
        assert result["reason"] == "cap"


# --- Stop-blocks-once -------------------------------------------------------

def test_stop_should_block_once_while_seeking_with_no_agent_running(tmp_path):
    policy = {"mode": "enforce", "seek_pct": 70}
    first = _evaluate(tmp_path, "s1", "Stop", pct=80, policy=policy, agent_running=False)
    assert first["should_block_stop"] is True

    second = _evaluate(tmp_path, "s1", "Stop", pct=80, policy=policy, agent_running=False)
    assert second["should_block_stop"] is False, "the same seeking episode does not block twice"


def test_stop_never_blocks_while_an_agent_is_running(tmp_path):
    policy = {"mode": "enforce", "seek_pct": 70}
    result = _evaluate(tmp_path, "s1", "Stop", pct=80, policy=policy, agent_running=True)
    assert result["should_block_stop"] is False


def test_a_new_seeking_episode_gets_a_fresh_block_chance(tmp_path):
    policy = {"mode": "enforce", "seek_pct": 70}
    first = _evaluate(tmp_path, "s1", "Stop", pct=80, policy=policy, agent_running=False)
    assert first["should_block_stop"] is True

    # drop back to ok, then rise into a new seeking episode
    _evaluate(tmp_path, "s1", "PostToolUse", pct=50, policy=policy)
    third = _evaluate(tmp_path, "s1", "Stop", pct=80, policy=policy, agent_running=False)
    assert third["should_block_stop"] is True


def test_roll_now_never_blocks_the_stop(tmp_path):
    policy = {"mode": "enforce", "seek_pct": 70, "floor_pct": 90}
    result = _evaluate(tmp_path, "s1", "Stop", pct=95, policy=policy, agent_running=False)
    assert result["state"] == "roll_now"
    assert result["should_block_stop"] is False


# --- `roll_now_reissues`: bounding a declined rollover (issue_01M2HTBBHK2W4EH86RAFE0YBPY) --

def test_roll_now_reissues_advances_on_every_firing_in_enforce_mode(tmp_path):
    policy = {"mode": "enforce", "seek_pct": 70, "floor_pct": 90}
    r1 = _evaluate(tmp_path, "s1", "PostToolUse", pct=95, policy=policy)
    assert r1["roll_now_reissues"] == 1
    r2 = _evaluate(tmp_path, "s1", "Stop", pct=95, policy=policy)
    assert r2["roll_now_reissues"] == 2, "every event type in roll_now counts, not just PostToolUse"
    r3 = _evaluate(tmp_path, "s1", "UserPromptSubmit", pct=95, policy=policy)
    assert r3["roll_now_reissues"] == 3


def test_roll_now_reissues_stays_at_zero_in_observe_mode(tmp_path):
    """Nothing is actually said to the session in `observe` mode (no `force`),
    so the counter must not accrue toward a park nobody would ever see coming
    from an announcement that never happened."""
    policy = {"mode": "observe", "seek_pct": 70, "floor_pct": 90}
    for _ in range(5):
        result = _evaluate(tmp_path, "s1", "PostToolUse", pct=95, policy=policy)
    assert result["state"] == "roll_now"
    assert result["roll_now_reissues"] == 0


def test_roll_now_reissues_advances_under_an_unconditional_force(tmp_path):
    """`force` announces regardless of mode (the one exception the hook
    itself carries), so it must count regardless of mode too."""
    _write_override(tmp_path, "s1", "force")
    policy = {"mode": "observe", "seek_pct": 70}
    r1 = _evaluate(tmp_path, "s1", "PostToolUse", policy=policy)
    assert r1["state"] == "roll_now" and r1["reason"] == "operator_force"
    assert r1["roll_now_reissues"] == 1
    r2 = _evaluate(tmp_path, "s1", "PostToolUse", policy=policy)
    assert r2["roll_now_reissues"] == 2


def test_roll_now_reissues_resets_when_the_zone_leaves_roll_now(tmp_path):
    """A veto recorded AT `roll_now` covers `roll_now` itself (`_veto_covers`'s
    table) — an operator vetoing a rollover that is already due holds it
    until manually cleared, the same as main always did. `roll_now_reissues`
    resets to 0 because nothing is announced while vetoed."""
    policy = {"mode": "enforce", "seek_pct": 70, "floor_pct": 90}
    for _ in range(3):
        result = _evaluate(tmp_path, "s1", "PostToolUse", pct=95, policy=policy)
    assert result["roll_now_reissues"] == 3

    _write_override(tmp_path, "s1", "veto", zone="roll_now")
    vetoed = _evaluate(tmp_path, "s1", "PostToolUse", pct=95, policy=policy)
    assert vetoed["state"] == "vetoed"
    assert vetoed["roll_now_reissues"] == 0, "nothing is announced while vetoed"


def test_roll_now_reissues_continues_under_a_force_laid_over_an_already_declining_session(tmp_path):
    """The merge of #587's `resolve_override` and this slice's reissue
    counter: a `force` written AFTER a session has already been announced
    `roll_now` naturally (reissues already nonzero) must fold through the
    SAME `resolve_override` call `evaluate` always uses, on top of the SAME
    prior state the counter reads — so the counter continues from where the
    natural announcements left off rather than resetting to 1 as if this
    were a fresh episode. `test_roll_now_reissues_resets_when_the_zone_
    leaves_roll_now` pins the opposite case (a veto over the same kind of
    prior state resets to 0); nothing before this merge exercised a
    non-vetoing override against a nonzero prior count."""
    policy = {"mode": "enforce", "seek_pct": 70, "floor_pct": 90}
    for _ in range(3):
        result = _evaluate(tmp_path, "s1", "PostToolUse", pct=95, policy=policy)
    assert result["roll_now_reissues"] == 3
    assert result["reason"] == "floor"

    _write_override(tmp_path, "s1", "force")
    forced = _evaluate(tmp_path, "s1", "PostToolUse", pct=95, policy=policy)
    assert forced["state"] == "roll_now" and forced["reason"] == "operator_force"
    assert forced["roll_now_reissues"] == 4, "force continues the count rather than restarting it"


def test_roll_now_reissues_starts_fresh_after_mark_parked_clears(tmp_path):
    """Mirrors `session-agent rollover <name> clear`'s effect on a `parked`
    state (`clear_parked` just unlinks the file) — the next evaluate() call
    sees no prior state at all."""
    policy = {"mode": "enforce", "seek_pct": 70, "floor_pct": 90}
    for _ in range(3):
        result = _evaluate(tmp_path, "s1", "PostToolUse", pct=95, policy=policy)
    assert result["roll_now_reissues"] == 3

    RP.mark_parked("s1", reason="declined", handoff_path="", issue_id="issue_X",
                   state_dir=tmp_path)
    assert RP.clear_parked("s1", state_dir=tmp_path) is True

    fresh = _evaluate(tmp_path, "s1", "PostToolUse", pct=95, policy=policy)
    assert fresh["state"] == "roll_now"
    assert fresh["roll_now_reissues"] == 1


# --- `since` ----------------------------------------------------------------

def test_since_is_set_on_transition_and_held_across_repeats(tmp_path):
    policy = {"mode": "enforce", "seek_pct": 70}
    now = time.time()
    first = _evaluate(tmp_path, "s1", "PostToolUse", pct=80, policy=policy, now=now)
    assert first["since"] == now

    later = _evaluate(tmp_path, "s1", "PostToolUse", pct=80, policy=policy, now=now + 30)
    assert later["since"] == now, "still the same episode, so `since` does not move"


# --- an unchanged zone does not rewrite the state file ----------------------

def test_repeated_firings_in_an_unchanged_zone_do_not_rewrite_the_state_file(tmp_path):
    """This fires on every PostToolUse/Stop/UserPromptSubmit in every session
    on the machine, and most of a session's life is spent with the zone
    unchanged. `UserPromptSubmit` is used here (not `PostToolUse`) so
    `calls_seeking` genuinely holds still too, isolating the case this test
    means to pin."""
    policy = {"mode": "enforce", "seek_pct": 70}
    state_path = tmp_path / "rollover-s1.json"

    _evaluate(tmp_path, "s1", "UserPromptSubmit", pct=80, policy=policy)
    assert state_path.exists()

    # Force the mtime far into the past so a real rewrite is unmistakable
    # regardless of filesystem timestamp resolution.
    backdated = state_path.stat().st_mtime_ns - 5_000_000_000
    os.utime(state_path, ns=(backdated, backdated))

    _evaluate(tmp_path, "s1", "UserPromptSubmit", pct=80, policy=policy)
    assert state_path.stat().st_mtime_ns == backdated, \
        "an unchanged zone must not rewrite the state file"


def test_both_evaluate_branches_persist_exactly_the_default_state_keys(tmp_path):
    """`evaluate` builds its state as two hand-listed dicts — the
    `force`-override branch and the main one — and both are documented to clear
    every field of the states this function never produces. So for these two,
    unlike `mark_rolling`, the persisted key set is exactly `DEFAULT_STATE`'s,
    and asserting that catches a field added to `DEFAULT_STATE` and to only one
    literal.

    WHY THIS SHAPE AND NOT A NO-REWRITE PIN. The obvious guard is the one the
    main branch already has — fire twice, assert the file was not rewritten —
    and it CANNOT be written for the `force` branch: a force announces on every
    firing, so `roll_now_reissues` legitimately increments and the rewrite is
    correct. That was measured rather than assumed, on a clean tree with this
    change absent: two identical firings differ only in `roll_now_reissues`,
    1 then 2. A test pinning no-rewrite there would have been asserting a
    falsehood about existing behaviour.

    Nor does the round-trip sibling catch it: that check is a subset, so a
    missing key makes the file smaller and stays green. Three candidate guards,
    and only this one answers the question."""
    want = set(RP.DEFAULT_STATE)

    _write_override(tmp_path, "f-1", "force")
    _evaluate(tmp_path, "f-1", "UserPromptSubmit",
              policy={"mode": "observe", "seek_pct": 70})
    _evaluate(tmp_path, "e-1", "Stop", pct=72,
              policy={"mode": "enforce", "seek_pct": 70, "floor_pct": 90})

    for session_id, which in (("f-1", "the force-override branch"),
                              ("e-1", "the main branch")):
        got = set(json.loads((tmp_path / f"rollover-{session_id}.json").read_text()))
        assert got == want, (
            f"{which} persists a different key set than DEFAULT_STATE — "
            f"missing {sorted(want - got)}, extra {sorted(got - want)}")


@pytest.mark.parametrize("which,force,pct", [
    ("the main branch", False, 72),
    # NO `pct` FOR THE FORCE CASE, and it is load-bearing rather than tidy.
    # That branch sits inside the unreadable-vitals path ("a session this
    # evaluator cannot see"), so writing a vitals file routes the call past it
    # into the main branch below. A first version of this test passed `pct=72`
    # to both rows and exercised the main branch twice: removing the force
    # branch's own fallback broke nothing, while removing the main branch's
    # broke BOTH rows. A parametrization is not coverage until each row has
    # been shown to fail on its own target.
    ("the force-override branch", True, None),
])
def test_permission_mode_is_sticky_on_both_evaluate_branches(tmp_path, which, force, pct):
    """A firing that carries no `permission_mode` must not erase the last one
    reported. Not every hook event carries the field, so without stickiness a
    session idle in plan mode flickers back to looking ordinary on the next
    unrelated firing — and the rail would draw it as idle, which is the whole
    condition this field exists to distinguish.

    BOTH BRANCHES, PARAMETRIZED, because `evaluate` builds its state as two
    hand-written dict literals and a fallback present in one is no evidence
    about the other. The sibling key-set guard catches a field missing from a
    literal entirely; it cannot catch one PRESENT in both and sticky in only
    one, since the key set is identical either way."""
    session_id = f"pm-{'f' if force else 'm'}"
    if force:
        _write_override(tmp_path, session_id, "force")
    policy = {"mode": "observe", "seek_pct": 70}

    first = _evaluate(tmp_path, session_id, "UserPromptSubmit", pct=pct,
                      policy=policy, permission_mode="plan")
    # The discriminator is `reason`, which only the force branch ever sets to
    # `operator_force`. Asserting it is what stops this parametrization from
    # quietly collapsing into one branch again.
    assert (first["reason"] == "operator_force") is force, (
        f"{which}: this row did not reach the branch it names — "
        f"state {first['state']!r}, reason {first['reason']!r}")
    carried = _evaluate(tmp_path, session_id, "Stop", pct=pct, policy=policy)

    assert carried["permission_mode"] == "plan", (
        f"{which} erased a reported mode on a firing that carried none")
    persisted = json.loads((tmp_path / f"rollover-{session_id}.json").read_text())
    assert persisted["permission_mode"] == "plan", f"{which}, on disk"


@pytest.mark.parametrize("which", ["mark_rolling", "mark_parked"])
def test_permission_mode_survives_a_rolling_or_parked_transition(tmp_path, which):
    """Both functions write a FRESH state literal rather than reading prior, so
    every field in them resets by design. `permission_mode` is the one field in
    those literals that is not rollover state — a session's permission mode does
    not change because the session started rolling — and dropping it there does
    not reset it for the duration, it LOSES it: `evaluate`'s sticky fallback
    then restores from a `None` prior.

    IT BITES HARDER ON `parked`. That is a terminal fence `evaluate` never
    resolves past, so a parked session gets no later firing to report its mode
    again and would read `None` for as long as it sits waiting on its operator.

    Both reviewers on #629 found this independently, which is why it is fixed
    rather than documented."""
    policy = {"mode": "observe", "seek_pct": 70}
    _evaluate(tmp_path, "pm-t", "Stop", pct=72, policy=policy,
              permission_mode="plan")
    assert RP.read_state("pm-t", tmp_path)["permission_mode"] == "plan", (
        "precondition: the mode must be on disk before the transition")

    if which == "mark_rolling":
        after = RP.mark_rolling(
            "pm-t", predecessor={"session_id": "pm-t", "pane_id": "%1"},
            successor={"session_id": None, "pane_id": "%2"},
            successor_window=2, state_dir=tmp_path)
    else:
        after = RP.mark_parked(
            "pm-t", reason="spawn_error", handoff_path="", issue_id=None,
            state_dir=tmp_path)

    assert after["permission_mode"] == "plan", f"{which} returned it dropped"
    assert RP.read_state("pm-t", tmp_path)["permission_mode"] == "plan", (
        f"{which} persisted it dropped")


def test_the_unreadable_vitals_return_reports_the_mode_without_persisting_it(tmp_path):
    """`evaluate` has THREE state-shaped returns and this was the one missing the
    key — present on the force branch and the main branch, absent here, which is
    the same shape as the early-seed omission in `session_state`.

    The asymmetry that stays is deliberate: this path writes nothing at all,
    because a session whose vitals it cannot read is not one it can make claims
    about. So the mode is REPORTED to the caller and does not reach disk, and the
    states poll keeps showing the last persisted value until a firing with fresh
    vitals lands. Best-effort here, sticky on the paths that write.

    No vitals file is written, so `read_ctx_pct` finds no reading — the same
    condition an hour-old vitals file produces."""
    policy = {"mode": "observe", "seek_pct": 70}
    got = _evaluate(tmp_path, "pm-nv", "Stop", policy=policy,
                    permission_mode="plan")

    assert "permission_mode" in got, (
        "the third return omitted the key the other two carry")
    assert got["permission_mode"] == "plan", "reported to the caller"
    assert not (tmp_path / "rollover-pm-nv.json").exists(), (
        "this path must still write nothing — that is its whole discipline")


def test_a_new_permission_mode_report_replaces_the_last(tmp_path):
    """Sticky must not mean frozen — an operator leaving plan mode is exactly
    the transition the rail needs to see, and a fallback that kept the first
    value forever would pass the stickiness test above while making the field
    useless after its first firing."""
    policy = {"mode": "observe", "seek_pct": 70}
    _evaluate(tmp_path, "pm-swap", "Stop", pct=72, policy=policy,
              permission_mode="plan")
    got = _evaluate(tmp_path, "pm-swap", "Stop", pct=72, policy=policy,
                    permission_mode="auto")
    assert got["permission_mode"] == "auto"


# --- record_current_permission_mode (the PreToolUse-freshness fix) ---------


def test_record_current_permission_mode_writes_a_fresh_value(tmp_path):
    """The freshness fix itself: a `PreToolUse` reading must overwrite
    whatever `evaluate()` last persisted from a `Stop`/`UserPromptSubmit`
    firing, since that reading can be stale (a Shift+Tab mode change fires no
    hook at all)."""
    _evaluate(tmp_path, "pm-fresh", "Stop", pct=72,
              policy={"mode": "observe", "seek_pct": 70}, permission_mode="default")
    got = RP.record_current_permission_mode("pm-fresh", "plan", state_dir=tmp_path)
    assert got["permission_mode"] == "plan"
    assert RP.read_state("pm-fresh", tmp_path)["permission_mode"] == "plan"


def test_record_current_permission_mode_is_a_no_op_with_none(tmp_path):
    """None means the payload carried no `permission_mode` at all — not every
    `PreToolUse` firing does — and must not erase the last reported value."""
    _evaluate(tmp_path, "pm-none", "Stop", pct=72,
              policy={"mode": "observe", "seek_pct": 70}, permission_mode="plan")
    got = RP.record_current_permission_mode("pm-none", None, state_dir=tmp_path)
    assert got["permission_mode"] == "plan"
    assert RP.read_state("pm-none", tmp_path)["permission_mode"] == "plan"


def test_record_current_permission_mode_writes_on_first_report_with_no_prior_state(tmp_path):
    """No state file exists yet (a session's very first `PreToolUse`) — this
    must still record the mode rather than requiring `evaluate()` to have run
    first."""
    got = RP.record_current_permission_mode("pm-first", "acceptEdits", state_dir=tmp_path)
    assert got["permission_mode"] == "acceptEdits"
    assert RP.read_state("pm-first", tmp_path)["permission_mode"] == "acceptEdits"


@pytest.mark.parametrize("state", ["rolling", "parked"])
def test_record_current_permission_mode_is_a_no_op_once_rolling_or_parked(tmp_path, state):
    """Both are terminal states whose own `mark_rolling`/`mark_parked` already
    carried `permission_mode` forward — a later `PreToolUse` firing for that
    same (fenced, or waiting-on-operator) session must not rewrite fields
    those functions own, mirroring `evaluate()`'s own terminal-state guard."""
    if state == "rolling":
        RP.mark_rolling("pm-term", predecessor={"session_id": "pm-term", "pane_id": "%1"},
                        successor={"session_id": None, "pane_id": "%2"},
                        successor_window=2, state_dir=tmp_path)
    else:
        RP.mark_parked("pm-term", reason="spawn_error", handoff_path="", issue_id=None,
                       state_dir=tmp_path)
    before = RP.read_state("pm-term", tmp_path)

    got = RP.record_current_permission_mode("pm-term", "plan", state_dir=tmp_path)

    assert got == before, "a terminal state must not be rewritten by this call"
    assert RP.read_state("pm-term", tmp_path) == before


def test_a_counted_call_while_seeking_still_writes(tmp_path):
    """The counter-advancing case the test above deliberately avoids:
    `PostToolUse` while seeking changes `calls_seeking` every time, so the
    write must still happen."""
    policy = {"mode": "enforce", "seek_pct": 70}
    state_path = tmp_path / "rollover-s1.json"

    _evaluate(tmp_path, "s1", "PostToolUse", pct=80, policy=policy)
    backdated = state_path.stat().st_mtime_ns - 5_000_000_000
    os.utime(state_path, ns=(backdated, backdated))

    _evaluate(tmp_path, "s1", "PostToolUse", pct=80, policy=policy)
    assert state_path.stat().st_mtime_ns != backdated, \
        "calls_seeking advanced, so this firing must still write"


# --- the operator override (Slice 3: `session-agent rollover`) -------------

def test_write_override_then_read_it_back(tmp_path):
    RP.write_override("s1", "seek", override_dir=tmp_path)
    got = RP.read_override("s1", override_dir=tmp_path)
    assert got["kind"] == "seek"


def test_read_override_is_none_when_nothing_was_written(tmp_path):
    assert RP.read_override("s1", override_dir=tmp_path) is None


def test_read_override_is_none_for_an_unrecognised_kind(tmp_path):
    (tmp_path / "rollover-override-s1.json").write_text(json.dumps({"kind": "nonsense"}))
    assert RP.read_override("s1", override_dir=tmp_path) is None


def test_clear_override_removes_the_file_and_says_so(tmp_path):
    RP.write_override("s1", "veto", zone="ok", override_dir=tmp_path)
    assert RP.clear_override("s1", override_dir=tmp_path) is True
    assert RP.read_override("s1", override_dir=tmp_path) is None
    assert RP.clear_override("s1", override_dir=tmp_path) is False, \
        "nothing left to clear the second time"


def test_a_stale_override_never_carries_to_a_different_session_id(tmp_path):
    """Keyed by session id, on purpose (Slice 3 build note): a successor's
    fresh session id must read no override at all, even with the
    predecessor's file still sitting in the same directory."""
    RP.write_override("predecessor", "force", override_dir=tmp_path)
    assert RP.read_override("successor", override_dir=tmp_path) is None


def test_force_reports_roll_now_with_no_vitals_at_all(tmp_path):
    """An operator's `force` needs no context reading — see `evaluate`'s
    docstring: it does not wait on one that may never come."""
    _write_override(tmp_path, "s1", "force")
    result = _evaluate(tmp_path, "s1", "PostToolUse", policy={"mode": "enforce"})
    assert result["state"] == "roll_now"
    assert result["reason"] == "operator_force"
    assert result["ctx_pct"] is None


def test_force_reports_roll_now_however_low_the_context_is(tmp_path):
    _write_override(tmp_path, "s1", "force")
    result = _evaluate(tmp_path, "s1", "PostToolUse", pct=5,
                       policy={"mode": "enforce", "seek_pct": 70})
    assert result["state"] == "roll_now"
    assert result["reason"] == "operator_force"


def test_force_never_blocks_the_stop(tmp_path):
    _write_override(tmp_path, "s1", "force")
    result = _evaluate(tmp_path, "s1", "Stop", pct=5,
                       policy={"mode": "enforce", "seek_pct": 70}, agent_running=False)
    assert result["should_block_stop"] is False


def test_seek_escalates_a_below_threshold_reading_into_seeking(tmp_path):
    _write_override(tmp_path, "s1", "seek")
    result = _evaluate(tmp_path, "s1", "PostToolUse", pct=10,
                       policy={"mode": "enforce", "seek_pct": 70})
    assert result["state"] == "seeking"
    assert result["reason"] == "operator_seek"


def test_seek_takes_effect_on_the_first_call_with_no_vitals_reading_yet(tmp_path):
    """"armed... takes effect on the next tool call" (Slice 3 build note 3) —
    the very first `PostToolUse` after `seek` is written, even before this
    session ever had a vitals reading."""
    _write_override(tmp_path, "s1", "seek")
    result = RP.evaluate("s1", "PostToolUse", policy_path=_write_policy(tmp_path, mode="enforce"),
                         vitals_dir=tmp_path, state_dir=tmp_path, override_dir=tmp_path)
    assert result["state"] == "seeking"


def test_seek_reason_is_plain_threshold_when_it_changed_nothing(tmp_path):
    """The reading already reached the real threshold on its own — the
    override was not what put it there, so it does not claim credit."""
    _write_override(tmp_path, "s1", "seek")
    result = _evaluate(tmp_path, "s1", "PostToolUse", pct=90,
                       policy={"mode": "enforce", "seek_pct": 70})
    assert result["state"] == "seeking"
    assert result["reason"] == "threshold"


def test_seek_does_not_downgrade_an_already_roll_now_zone(tmp_path):
    _write_override(tmp_path, "s1", "seek")
    result = _evaluate(tmp_path, "s1", "PostToolUse", pct=96,
                       policy={"mode": "enforce", "seek_pct": 70, "floor_pct": 95})
    assert result["state"] == "roll_now"
    assert result["reason"] == "floor"


def test_seek_still_counts_toward_the_cap(tmp_path):
    _write_override(tmp_path, "s1", "seek")
    policy = {"mode": "enforce", "seek_pct": 70, "seek_call_cap": 2}
    result = None
    for _ in range(2):
        result = _evaluate(tmp_path, "s1", "PostToolUse", pct=10, policy=policy)
    assert result["state"] == "roll_now"
    assert result["reason"] == "cap"


def test_veto_suppresses_the_zone_it_was_set_in(tmp_path):
    _write_override(tmp_path, "s1", "veto", zone="seeking")
    result = _evaluate(tmp_path, "s1", "PostToolUse", pct=80,
                       policy={"mode": "enforce", "seek_pct": 70})
    assert result["state"] == "vetoed"
    assert result["reason"] == "operator_veto"


def test_a_veto_set_at_ok_holds_through_seeking_to_the_floor(tmp_path):
    """The operator, 2026-10-01: "we need to add veto before the looking starts,
    because often the rollover window is found in a split second" — a veto
    recorded while `ok` must still suppress once the session starts
    seeking, not just the zone it was set in
    (issue_01M3WX4443DF30SJ3WM5QHA7RJ), and it does not block the `Stop`
    that seeking would otherwise block while it holds."""
    _write_override(tmp_path, "s1", "veto", zone="ok")
    policy = {"mode": "enforce", "seek_pct": 70, "floor_pct": 95}

    below = _evaluate(tmp_path, "s1", "PostToolUse", pct=50, policy=policy)
    assert below["state"] == "vetoed"
    assert below["vetoed_until"] == "floor_pct"

    seeking = _evaluate(tmp_path, "s1", "Stop", pct=80, policy=policy)
    assert seeking["state"] == "vetoed"
    assert seeking["vetoed_until"] == "floor_pct"
    assert seeking["should_block_stop"] is False

    rolling = _evaluate(tmp_path, "s1", "PostToolUse", pct=96, policy=policy)
    assert rolling["state"] == "roll_now"
    assert rolling["reason"] == "floor"


def test_a_veto_set_at_ok_names_the_cap_when_the_floor_is_null(tmp_path):
    """The same fall-through `_vetoed_until` already gave a veto recorded in
    `seeking` — a veto recorded in `ok` now names the identical target once
    it is covering `seeking`, never `seek_pct`."""
    _write_override(tmp_path, "s1", "veto", zone="ok")
    result = _evaluate(tmp_path, "s1", "PostToolUse", pct=80,
                       policy={"mode": "enforce", "seek_pct": 70, "seek_call_cap": 40})
    assert result["state"] == "vetoed"
    assert result["vetoed_until"] == "seek_call_cap"


def test_a_veto_set_at_ok_names_no_limit_when_both_are_null(tmp_path):
    _write_override(tmp_path, "s1", "veto", zone="ok")
    result = _evaluate(tmp_path, "s1", "PostToolUse", pct=80,
                       policy={"mode": "enforce", "seek_pct": 70})
    assert result["state"] == "vetoed"
    assert result["vetoed_until"] is None


def test_a_veto_set_at_ok_does_not_silence_the_cap(tmp_path):
    """The operator's ruling (d) reaches a veto recorded in `ok` too, once it is
    covering `seeking`: the call cap still forces `roll_now`."""
    _write_override(tmp_path, "s1", "veto", zone="ok")
    policy = {"mode": "enforce", "seek_pct": 70, "seek_call_cap": 2}
    result = None
    for _ in range(2):
        result = _evaluate(tmp_path, "s1", "PostToolUse", pct=80, policy=policy)
    assert result["state"] == "roll_now"
    assert result["reason"] == "cap"


def test_a_veto_set_while_seeking_does_not_silence_the_floor(tmp_path):
    """The operator's ruling (d), stated directly: "a veto set while seeking doesn't
    silence the hard floor or the call cap."""
    _write_override(tmp_path, "s1", "veto", zone="seeking")
    result = _evaluate(tmp_path, "s1", "PostToolUse", pct=96,
                       policy={"mode": "enforce", "seek_pct": 70, "floor_pct": 95})
    assert result["state"] == "roll_now"
    assert result["reason"] == "floor"


def test_a_veto_set_while_seeking_does_not_silence_the_cap(tmp_path):
    _write_override(tmp_path, "s1", "veto", zone="seeking")
    policy = {"mode": "enforce", "seek_pct": 70, "seek_call_cap": 2}
    result = None
    for _ in range(2):
        result = _evaluate(tmp_path, "s1", "PostToolUse", pct=80, policy=policy)
    assert result["state"] == "roll_now"
    assert result["reason"] == "cap"


def test_a_veto_re_arms_once_the_session_drops_back_below_the_vetoed_zone(tmp_path):
    """The comparison is symmetric: a natural zone that no longer MATCHES the
    vetoed one — worse or better — stops being suppressed."""
    _write_override(tmp_path, "s1", "veto", zone="seeking")
    result = _evaluate(tmp_path, "s1", "PostToolUse", pct=40,
                       policy={"mode": "enforce", "seek_pct": 70})
    assert result["state"] == "ok"


def test_vetoed_until_names_the_floor_when_one_is_set(tmp_path):
    _write_override(tmp_path, "s1", "veto", zone="seeking")
    result = _evaluate(tmp_path, "s1", "PostToolUse", pct=80,
                       policy={"mode": "enforce", "seek_pct": 70, "floor_pct": 95})
    assert result["state"] == "vetoed"
    assert result["vetoed_until"] == "floor_pct"


def test_vetoed_until_falls_back_to_the_cap_when_the_floor_is_null(tmp_path):
    _write_override(tmp_path, "s1", "veto", zone="seeking")
    result = _evaluate(tmp_path, "s1", "PostToolUse", pct=80,
                       policy={"mode": "enforce", "seek_pct": 70, "seek_call_cap": 40})
    assert result["vetoed_until"] == "seek_call_cap"


def test_vetoed_until_is_null_when_both_limits_are_null(tmp_path):
    """The operator's ruling (a): the verbs must work when `floor_pct`/`seek_call_cap`
    stay null — a veto set while seeking then has nothing named to wait for,
    which is not an error, only the honest answer."""
    _write_override(tmp_path, "s1", "veto", zone="seeking")
    result = _evaluate(tmp_path, "s1", "PostToolUse", pct=80,
                       policy={"mode": "enforce", "seek_pct": 70})
    assert result["state"] == "vetoed"
    assert result["vetoed_until"] is None


def test_vetoed_until_is_null_once_a_veto_set_at_roll_now_has_no_rung_above_it(tmp_path):
    """A veto recorded AT `roll_now` covers `roll_now` itself (`_veto_covers`'s
    table — restored main's own behaviour): an operator vetoing a session
    already past the floor holds it until manually cleared. `vetoed_until`
    stays `None` because `roll_now` has no further limit to wait for."""
    _write_override(tmp_path, "s1", "veto", zone="roll_now")
    result = _evaluate(tmp_path, "s1", "PostToolUse", pct=96,
                       policy={"mode": "enforce", "seek_pct": 70, "floor_pct": 95})
    assert result["state"] == "vetoed"
    assert result["vetoed_until"] is None


def test_no_override_behaves_exactly_as_slice_1_did(tmp_path):
    """A regression pin, not a new property: with no override file at all,
    `natural_state`/`vetoed_until` ride along unseen and change nothing a
    Slice 1 caller already checks."""
    result = _evaluate(tmp_path, "s1", "PostToolUse", pct=80,
                       policy={"mode": "enforce", "seek_pct": 70})
    assert result["state"] == "seeking"
    assert result["reason"] == "threshold"
    assert result["vetoed_until"] is None


def test_a_persisted_seeking_zone_carries_natural_reason(tmp_path):
    """`natural_reason` rides alongside `natural_state` for the same reason
    that field exists — `session_state.rollover` resolves a fresh override
    against whatever THIS call persists, and needs to tell a genuine
    threshold crossing from one an operator's `seek` produced
    (issue_01M2HTBMRN1W688EV40DCDVXB7)."""
    result = _evaluate(tmp_path, "s1", "PostToolUse", pct=80,
                       policy={"mode": "enforce", "seek_pct": 70})
    assert result["natural_reason"] == "threshold"

    persisted = RP.read_state("s1", state_dir=tmp_path)
    assert persisted["natural_reason"] == "threshold"


def test_a_seek_promoted_zone_persists_operator_seek_as_the_natural_reason(tmp_path):
    _write_override(tmp_path, "s1", "seek")
    result = _evaluate(tmp_path, "s1", "PostToolUse", pct=10,
                       policy={"mode": "enforce", "seek_pct": 70})
    assert result["natural_reason"] == "operator_seek"


# --- resolve_override: the single function `evaluate` and `session_state.
# rollover` both fold an override through (issue_01M2HTBMRN1W688EV40DCDVXB7) -

def test_resolve_override_passes_the_natural_pair_through_with_no_override(tmp_path):
    zone, reason, vetoed_until = RP.resolve_override("seeking", "threshold", None, RP.DEFAULT_POLICY)
    assert (zone, reason, vetoed_until) == ("seeking", "threshold", None)


def test_resolve_override_force_is_unconditional(tmp_path):
    override = {"kind": "force", "zone": None, "written_at": 1}
    assert RP.resolve_override("ok", None, override, RP.DEFAULT_POLICY) == \
        ("roll_now", "operator_force", None)


def test_resolve_override_seek_promotes_ok_to_seeking(tmp_path):
    override = {"kind": "seek", "zone": None, "written_at": 1}
    assert RP.resolve_override("ok", None, override, RP.DEFAULT_POLICY) == \
        ("seeking", "operator_seek", None)


def test_resolve_override_seek_does_not_downgrade_roll_now(tmp_path):
    override = {"kind": "seek", "zone": None, "written_at": 1}
    zone, reason, vetoed_until = RP.resolve_override("roll_now", "floor", override, RP.DEFAULT_POLICY)
    assert (zone, reason) == ("roll_now", "floor")


def test_resolve_override_seek_does_not_relabel_a_genuine_seeking_zone(tmp_path):
    override = {"kind": "seek", "zone": None, "written_at": 1}
    zone, reason, vetoed_until = RP.resolve_override("seeking", "threshold", override, RP.DEFAULT_POLICY)
    assert (zone, reason) == ("seeking", "threshold")


def test_resolve_override_veto_matching_the_natural_zone_suppresses_it(tmp_path):
    override = {"kind": "veto", "zone": "seeking", "written_at": 1}
    policy = {"mode": "enforce", "seek_pct": 70, "floor_pct": 95, "seek_call_cap": None}
    zone, reason, vetoed_until = RP.resolve_override("seeking", "threshold", override, policy)
    assert (zone, reason, vetoed_until) == ("vetoed", "operator_veto", "floor_pct")


def test_resolve_override_veto_not_matching_the_natural_zone_has_no_effect(tmp_path):
    override = {"kind": "veto", "zone": "seeking", "written_at": 1}
    zone, reason, vetoed_until = RP.resolve_override("roll_now", "floor", override, RP.DEFAULT_POLICY)
    assert (zone, reason, vetoed_until) == ("roll_now", "floor", None)


def test_resolve_override_veto_set_at_ok_also_covers_seeking(tmp_path):
    """issue_01M3WX4443DF30SJ3WM5QHA7RJ: a veto recorded while `ok` must
    still suppress once the natural zone moves to `seeking`, not just the
    zone it was set in."""
    override = {"kind": "veto", "zone": "ok", "written_at": 1}
    policy = {"mode": "enforce", "seek_pct": 70, "floor_pct": 95, "seek_call_cap": None}
    zone, reason, vetoed_until = RP.resolve_override("seeking", "threshold", override, policy)
    assert (zone, reason, vetoed_until) == ("vetoed", "operator_veto", "floor_pct")


def test_resolve_override_veto_set_at_ok_or_seeking_never_covers_roll_now(tmp_path):
    """The operator's ruling (d): a veto recorded in `ok` or `seeking` does not
    survive a crossing into `roll_now`, which moves the natural zone PAST
    the zone(s) either one covers."""
    for veto_zone in ("ok", "seeking"):
        override = {"kind": "veto", "zone": veto_zone, "written_at": 1}
        zone, reason, vetoed_until = RP.resolve_override(
            "roll_now", "floor", override, RP.DEFAULT_POLICY)
        assert (zone, reason, vetoed_until) == ("roll_now", "floor", None), veto_zone


def test_resolve_override_veto_set_at_roll_now_covers_roll_now(tmp_path):
    """A veto recorded AT `roll_now` is a different case from the one above:
    nothing moves the natural zone past `roll_now` (it is the ceiling), so
    there is no crossing for ruling (d) to apply to, and the veto holds the
    same way main always let it — restored here after a round that read the
    ruling too broadly and dropped it."""
    override = {"kind": "veto", "zone": "roll_now", "written_at": 1}
    zone, reason, vetoed_until = RP.resolve_override(
        "roll_now", "floor", override, RP.DEFAULT_POLICY)
    assert (zone, reason, vetoed_until) == ("vetoed", "operator_veto", None)


def test_resolve_override_a_veto_cancelling_an_operator_seek_names_no_limit(tmp_path):
    """Requirement 1 of issue_01M2HTBMRN1W688EV40DCDVXB7: the zone being
    vetoed is `seeking` only because of an operator's `seek`, not a real
    threshold crossing — `vetoed_until` says `None` explicitly rather than
    naming `floor_pct`, which is set here specifically to prove this is not
    the already-covered "both limits are null" case."""
    override = {"kind": "veto", "zone": "seeking", "written_at": 1}
    policy = {"mode": "enforce", "seek_pct": 70, "floor_pct": 95, "seek_call_cap": None}
    zone, reason, vetoed_until = RP.resolve_override("seeking", "operator_seek", override, policy)
    assert zone == "vetoed"
    assert vetoed_until is None


@pytest.mark.parametrize("veto_zone", [["ok"], {"z": 1}, None, 7])
@pytest.mark.parametrize("natural_zone", ["ok", "seeking", "roll_now", {"z": 1}, None, 7])
def test_resolve_override_an_unhashable_or_atypical_zone_value_never_raises(tmp_path, veto_zone, natural_zone):
    """Round 2 finding on this PR: main's `override.get("zone") ==
    natural_zone` was total over any JSON value on either side, since `==`
    never raises. `_veto_covers` has to stay just as total rather than
    hashing either operand — a list or dict `zone` in the override file (or
    a non-string `natural_state`) would otherwise raise `TypeError`, which
    the hook's own `except BaseException` swallows, going silent on the
    session's rollover enforcement with nothing visibly wrong. None of the
    `veto_zone` values here is a real zone name, so `_veto_covers` never
    matches and the natural pair passes through unchanged — the same result
    main's `==` gave for the same inputs."""
    override = {"kind": "veto", "zone": veto_zone, "written_at": 1}
    zone, reason, vetoed_until = RP.resolve_override(
        natural_zone, "threshold", override, RP.DEFAULT_POLICY)
    assert (zone, reason, vetoed_until) == (natural_zone, "threshold", None)


# --- editing the policy (Slice 3: `session-agent rollover-policy --set`) ---

def test_parse_policy_updates_types_numbers_and_null(tmp_path):
    updates = RP.parse_policy_updates(["seek_pct=65", "floor_pct=null", "mode=enforce"])
    assert updates == {"seek_pct": 65, "floor_pct": None, "mode": "enforce"}


def test_parse_policy_updates_refuses_an_unknown_key():
    with pytest.raises(RP.PolicyError, match="unknown policy key"):
        RP.parse_policy_updates(["nonsense=1"])


def test_parse_policy_updates_refuses_a_malformed_pair():
    with pytest.raises(RP.PolicyError, match="malformed"):
        RP.parse_policy_updates(["seek_pct"])


def test_parse_policy_updates_refuses_a_non_numeric_value():
    with pytest.raises(RP.PolicyError, match="must be a number"):
        RP.parse_policy_updates(["seek_pct=soon"])


def test_apply_policy_updates_refuses_a_floor_at_or_below_seek():
    current = dict(RP.DEFAULT_POLICY)
    with pytest.raises(RP.PolicyError, match="floor_pct"):
        RP.apply_policy_updates(current, {"seek_pct": 80, "floor_pct": 80})


def test_apply_policy_updates_refuses_a_floor_below_seek_even_when_only_one_changes():
    current = dict(RP.DEFAULT_POLICY)
    current["seek_pct"] = 70
    with pytest.raises(RP.PolicyError, match="floor_pct"):
        RP.apply_policy_updates(current, {"floor_pct": 65})


def test_apply_policy_updates_accepts_a_floor_above_seek():
    current = dict(RP.DEFAULT_POLICY)
    new = RP.apply_policy_updates(current, {"seek_pct": 70, "floor_pct": 95})
    assert new["seek_pct"] == 70
    assert new["floor_pct"] == 95


def test_apply_policy_updates_refuses_an_unrecognised_mode():
    current = dict(RP.DEFAULT_POLICY)
    with pytest.raises(RP.PolicyError, match="mode"):
        RP.apply_policy_updates(current, {"mode": "YOLO"})


def test_apply_policy_updates_leaves_untouched_fields_alone():
    current = {"mode": "enforce", "seek_pct": 70, "floor_pct": 95, "seek_call_cap": 40}
    new = RP.apply_policy_updates(current, {"seek_pct": 60})
    assert new["floor_pct"] == 95
    assert new["seek_call_cap"] == 40


# --- `--set` can't silently disable a threshold (the operator's ruling (a), and the
# security-reviewer's suggestion on non-finite numbers) -----------------------

def test_apply_policy_updates_refuses_a_null_seek_pct():
    """`--set seek_pct=null`. Unlike `floor_pct`/`seek_call_cap`, `seek_pct`
    has no inert value — `evaluate` always compares against it, and a null
    there raises `TypeError` inside `evaluate` rather than doing nothing."""
    current = dict(RP.DEFAULT_POLICY)
    with pytest.raises(RP.PolicyError, match="seek_pct"):
        RP.apply_policy_updates(current, {"seek_pct": None})


def test_apply_policy_updates_still_accepts_null_floor_and_cap():
    current = dict(RP.DEFAULT_POLICY)
    new = RP.apply_policy_updates(current, {"floor_pct": None, "seek_call_cap": None})
    assert new["floor_pct"] is None
    assert new["seek_call_cap"] is None


def test_apply_policy_updates_refuses_an_infinite_seek_pct():
    """`--set seek_pct=1e400` parses to `inf` in `parse_policy_updates` — a
    valid JSON number that would otherwise pass `load_policy`'s type check."""
    current = dict(RP.DEFAULT_POLICY)
    with pytest.raises(RP.PolicyError, match="finite"):
        RP.apply_policy_updates(current, {"seek_pct": float("inf")})


def test_apply_policy_updates_refuses_a_nan_seek_pct():
    """`--set seek_pct=nan` — NaN makes every `>=` comparison in `evaluate`
    false, silently disabling the threshold rather than raising."""
    current = dict(RP.DEFAULT_POLICY)
    with pytest.raises(RP.PolicyError, match="finite"):
        RP.apply_policy_updates(current, {"seek_pct": float("nan")})


def test_apply_policy_updates_refuses_an_infinite_floor_pct():
    current = dict(RP.DEFAULT_POLICY)
    with pytest.raises(RP.PolicyError, match="finite"):
        RP.apply_policy_updates(current, {"floor_pct": float("-inf")})


def test_apply_policy_updates_refuses_a_nan_seek_call_cap():
    current = dict(RP.DEFAULT_POLICY)
    with pytest.raises(RP.PolicyError, match="finite"):
        RP.apply_policy_updates(current, {"seek_call_cap": float("nan")})


def test_set_seek_pct_null_is_refused_end_to_end():
    """The exact CLI argument from the finding: `--set seek_pct=null`."""
    updates = RP.parse_policy_updates(["seek_pct=null"])
    with pytest.raises(RP.PolicyError, match="seek_pct"):
        RP.apply_policy_updates(dict(RP.DEFAULT_POLICY), updates)


def test_set_seek_pct_1e400_is_refused_end_to_end():
    """The exact CLI argument from the finding: `--set seek_pct=1e400`."""
    updates = RP.parse_policy_updates(["seek_pct=1e400"])
    assert updates["seek_pct"] == float("inf")
    with pytest.raises(RP.PolicyError, match="finite"):
        RP.apply_policy_updates(dict(RP.DEFAULT_POLICY), updates)


def test_set_seek_pct_nan_is_refused_end_to_end():
    """The exact CLI argument from the finding: `--set seek_pct=nan`."""
    updates = RP.parse_policy_updates(["seek_pct=nan"])
    assert math.isnan(updates["seek_pct"])
    with pytest.raises(RP.PolicyError, match="finite"):
        RP.apply_policy_updates(dict(RP.DEFAULT_POLICY), updates)


def test_save_policy_then_load_policy_round_trips(tmp_path):
    path = tmp_path / "rollover.json"
    RP.save_policy({"mode": "enforce", "seek_pct": 60, "floor_pct": 90,
                    "seek_call_cap": 30}, path=path)
    assert RP.load_policy(path) == {"mode": "enforce", "seek_pct": 60,
                                    "floor_pct": 90, "seek_call_cap": 30}


# --- mark_rolling / record_successor_session_id (Slice 5, bin/co-rollover) --

_PREDECESSOR = {"session_id": "pred-1", "pane_id": "%1"}
_SUCCESSOR = {"session_id": None, "pane_id": "%2"}


def test_mark_rolling_writes_state_and_both_sides_of_the_handoff(tmp_path):
    got = RP.mark_rolling("pred-1", predecessor=_PREDECESSOR, successor=_SUCCESSOR,
                          successor_window=2, state_dir=tmp_path, now=1000.0)
    assert got["state"] == "rolling"
    assert got["predecessor"] == _PREDECESSOR
    assert got["successor"] == _SUCCESSOR
    assert got["successor_window"] == 2
    assert RP.read_state("pred-1", tmp_path)["state"] == "rolling"


def test_every_state_writer_carries_both_reissue_counters(tmp_path):
    """This module has FOUR places that build a state dict as a hand-listed
    literal rather than from `DEFAULT_STATE` — `mark_rolling`, `mark_parked`,
    `evaluate`'s `force`-override branch, and `evaluate`'s main branch — so a
    new field has to be added in five places counting `DEFAULT_STATE`, and
    two were missed in turn: `mark_rolling`/`mark_parked` first, then the
    `force` branch, each caught a review round apart.

    So this covers every writer rather than the two a finding named, which is
    what stops the next field from costing a third round. `read_state`
    overlays onto `DEFAULT_STATE` and hides the gap from every in-repo Python
    reader; this asserts on the PERSISTED JSON, which is what anything
    outside that overlay sees.

    Scoped to the two counters rather than the whole field set: the writers
    genuinely differ elsewhere by design (`mark_rolling` carries no
    `handoff_path`/`issue_id`), and asserting a shape the module never held
    would be inventing an invariant rather than pinning one."""
    RP.mark_rolling("r-1", predecessor=_PREDECESSOR, successor=_SUCCESSOR,
                    successor_window=2, state_dir=tmp_path, now=1000.0)
    RP.mark_parked("p-1", reason="declined", handoff_path="", issue_id=None,
                   state_dir=tmp_path, now=1000.0)
    # `evaluate`'s `force` branch: reached with an override and no vitals, so
    # there is no `ctx_pct` reading and the threshold branches never run.
    _write_override(tmp_path, "f-1", "force")
    _evaluate(tmp_path, "f-1", "PostToolUse", policy={"mode": "observe", "seek_pct": 70})
    # `evaluate`'s main branch.
    _evaluate(tmp_path, "e-1", "Stop", pct=72,
              policy={"mode": "enforce", "seek_pct": 70, "floor_pct": 90})

    for session_id in ("r-1", "p-1", "f-1", "e-1"):
        written = json.loads((tmp_path / f"rollover-{session_id}.json").read_text())
        assert "roll_now_reissues" in written, f"{session_id}: no roll_now counter"
        assert "seeking_stop_reissues" in written, \
            f"{session_id}'s writer forgot the seeking counter"


def test_record_successor_session_id_fills_in_the_id(tmp_path):
    RP.mark_rolling("pred-1", predecessor=_PREDECESSOR, successor=_SUCCESSOR,
                    successor_window=2, state_dir=tmp_path)
    got = RP.record_successor_session_id("pred-1", "succ-1", state_dir=tmp_path)
    assert got["successor"] == {"session_id": "succ-1", "pane_id": "%2"}
    assert RP.read_state("pred-1", tmp_path)["successor"]["session_id"] == "succ-1"


def test_record_successor_session_id_is_a_noop_when_not_rolling(tmp_path):
    """The successor's own report must never manufacture a rolling state that
    `mark_rolling` never set."""
    got = RP.record_successor_session_id("never-rolled", "succ-1", state_dir=tmp_path)
    assert got["state"] == "ok"
    assert (tmp_path / "rollover-never-rolled.json").exists() is False


# --- clear_rolling (Slice 5 round 2: the operator's manual escape hatch) ----


def test_clear_rolling_removes_a_rolling_state_and_says_so(tmp_path):
    RP.mark_rolling("pred-1", predecessor=_PREDECESSOR, successor=_SUCCESSOR,
                    successor_window=2, state_dir=tmp_path)
    assert RP.clear_rolling("pred-1", state_dir=tmp_path) is True
    assert RP.read_state("pred-1", tmp_path)["state"] == "ok", \
        "no state file left behind, so the next read is the honest default"
    assert (tmp_path / "rollover-pred-1.json").exists() is False


def test_clear_rolling_is_a_noop_with_no_state_file_at_all(tmp_path):
    assert RP.clear_rolling("never-rolled", state_dir=tmp_path) is False


def test_clear_rolling_leaves_a_non_rolling_state_untouched(tmp_path):
    """`clear` must never invent a zone that was never there — a session that
    is `seeking`, not `rolling`, keeps its state file exactly as it was."""
    _write_vitals(tmp_path, "s1", 80)
    policy_path = _write_policy(tmp_path, mode="enforce", seek_pct=70)
    RP.evaluate("s1", "PostToolUse", policy_path=policy_path, vitals_dir=tmp_path,
               state_dir=tmp_path, override_dir=tmp_path, now=1000.0)
    before = RP.read_state("s1", tmp_path)
    assert before["state"] == "seeking"

    assert RP.clear_rolling("s1", state_dir=tmp_path) is False
    assert RP.read_state("s1", tmp_path) == before


# --- rolling is terminal to evaluate() (Slice 5) ----------------------------


def test_evaluate_never_moves_a_rolling_session_back_out(tmp_path):
    """The predecessor's own PostToolUse/Stop/UserPromptSubmit hooks keep
    firing up to the moment its pane is killed. Without this, the very next
    one would recompute a zone from ctx_pct and silently un-fence the session
    the PreToolUse guard depends on staying fenced."""
    RP.mark_rolling("pred-1", predecessor=_PREDECESSOR, successor=_SUCCESSOR,
                    successor_window=2, state_dir=tmp_path)
    _write_vitals(tmp_path, "pred-1", 10)  # would read as `ok` if recomputed
    policy_path = _write_policy(tmp_path, mode="enforce", seek_pct=70)

    result = RP.evaluate("pred-1", "Stop", policy_path=policy_path, vitals_dir=tmp_path,
                         state_dir=tmp_path, override_dir=tmp_path)

    assert result["state"] == "rolling"
    assert result["successor_window"] == 2
    assert result["should_block_stop"] is False
    assert RP.read_state("pred-1", tmp_path)["state"] == "rolling", \
        "must not have been overwritten"


def test_evaluate_on_a_rolling_session_ignores_a_force_override_too(tmp_path):
    """An operator's override is honoured only through the ordinary evaluation
    path, which `rolling` deliberately never reaches."""
    RP.mark_rolling("pred-1", predecessor=_PREDECESSOR, successor=_SUCCESSOR,
                    successor_window=2, state_dir=tmp_path)
    RP.write_override("pred-1", "force", override_dir=tmp_path)
    policy_path = _write_policy(tmp_path, mode="enforce", seek_pct=70)

    result = RP.evaluate("pred-1", "PostToolUse", policy_path=policy_path,
                         vitals_dir=tmp_path, state_dir=tmp_path, override_dir=tmp_path)

    assert result["state"] == "rolling"


def test_evaluate_on_a_rolling_session_ignores_a_veto_override_too(tmp_path):
    """Round-4 finding on PR #584: only `force` was pinned against a rolling
    session at this layer. `veto` never reaches `resolve_override` either —
    the terminal short-circuit returns `prior` before `read_override` is even
    consulted — so a veto recorded with the exact zone `mark_rolling` sets
    (`natural_state == "rolling"`) still leaves the read `rolling`."""
    RP.mark_rolling("pred-1", predecessor=_PREDECESSOR, successor=_SUCCESSOR,
                    successor_window=2, state_dir=tmp_path)
    RP.write_override("pred-1", "veto", zone="rolling", override_dir=tmp_path)
    policy_path = _write_policy(tmp_path, mode="enforce", seek_pct=70)

    result = RP.evaluate("pred-1", "PostToolUse", policy_path=policy_path,
                         vitals_dir=tmp_path, state_dir=tmp_path, override_dir=tmp_path)

    assert result["state"] == "rolling"


def test_evaluate_on_a_rolling_session_ignores_a_seek_override_too(tmp_path):
    """Same matrix cell as the veto/force tests above, for `seek`."""
    RP.mark_rolling("pred-1", predecessor=_PREDECESSOR, successor=_SUCCESSOR,
                    successor_window=2, state_dir=tmp_path)
    RP.write_override("pred-1", "seek", override_dir=tmp_path)
    policy_path = _write_policy(tmp_path, mode="enforce", seek_pct=70)

    result = RP.evaluate("pred-1", "PostToolUse", policy_path=policy_path,
                         vitals_dir=tmp_path, state_dir=tmp_path, override_dir=tmp_path)

    assert result["state"] == "rolling"


def test_evaluate_recomputes_a_fresh_zone_after_clear_rolling(tmp_path):
    """Mirrors `test_roll_now_reissues_starts_fresh_after_mark_parked_clears`
    for the other terminal state: `clear_rolling` just unlinks the file, so
    `clear` is the only way an override-shaped write ever changes what a
    rolling session reads — not by being honoured through `resolve_override`,
    but by removing the state that made `evaluate()` short-circuit at all.
    The next call sees no prior state and computes a fresh zone from
    `ctx_pct`."""
    RP.mark_rolling("pred-1", predecessor=_PREDECESSOR, successor=_SUCCESSOR,
                    successor_window=2, state_dir=tmp_path)
    assert RP.clear_rolling("pred-1", state_dir=tmp_path) is True

    _write_vitals(tmp_path, "pred-1", 95)
    policy_path = _write_policy(tmp_path, mode="enforce", seek_pct=70, floor_pct=90)

    result = RP.evaluate("pred-1", "PostToolUse", policy_path=policy_path, vitals_dir=tmp_path,
                         state_dir=tmp_path, override_dir=tmp_path)

    assert result["state"] == "roll_now"


# --- mark_parked / clear_parked (Slice 6: a failed successor spawn) --------


def test_mark_parked_writes_state_reason_handoff_and_issue_id(tmp_path):
    got = RP.mark_parked("pred-1", reason="spawn_error", handoff_path="/tmp/h/handoff.md",
                         issue_id="issue_ABC", state_dir=tmp_path, now=1000.0)
    assert got["state"] == "parked"
    assert got["reason"] == "spawn_error"
    assert got["since"] == 1000.0
    assert got["handoff_path"] == "/tmp/h/handoff.md"
    assert got["issue_id"] == "issue_ABC"
    assert RP.read_state("pred-1", tmp_path)["state"] == "parked"


def test_mark_parked_issue_id_can_be_none(tmp_path):
    """Logging can fail (`signal_client.post_alert` never raises but can
    return no id) — the park must still be recorded, with the honest
    absence rather than a placeholder."""
    got = RP.mark_parked("pred-1", reason="verify_up_failed", handoff_path="/tmp/h/handoff.md",
                         issue_id=None, state_dir=tmp_path)
    assert got["state"] == "parked"
    assert got["issue_id"] is None
    assert RP.read_state("pred-1", tmp_path)["issue_id"] is None


def test_mark_parked_records_why_logging_failed(tmp_path):
    """`issue_id: None` alone cannot distinguish a park that logged nothing from
    one whose item is sitting in the queue, and that ambiguity is what let a
    real parked state sit for four days with nobody able to say which it was.
    `issue_status` carries `post_alert`'s own answer — `error:<code>`, `skip`
    with no token, or `created`."""
    got = RP.mark_parked("pred-1", reason="verify_up_failed", handoff_path="/tmp/h/handoff.md",
                         issue_id=None, issue_status="error:400", state_dir=tmp_path)
    assert got["issue_id"] is None
    assert got["issue_status"] == "error:400"
    # THROUGH `read_state`, not just the returned dict — see the test below.
    assert RP.read_state("pred-1", tmp_path)["issue_status"] == "error:400"


def test_a_caller_that_omits_the_status_still_parks(tmp_path):
    """`issue_status` defaults rather than being required, because
    `tools/rollover-trigger.py`'s two paths and any future caller must never
    fail to park over a missing keyword. `None` therefore means "this caller
    did not say", never "logging succeeded"."""
    got = RP.mark_parked("pred-2", reason="declined", handoff_path="",
                         issue_id=None, state_dir=tmp_path)
    assert got["state"] == "parked"
    assert got["issue_status"] is None


def test_every_key_a_state_writer_persists_is_visible_through_read_state(tmp_path):
    """`read_state` overlays the persisted file onto `DEFAULT_STATE` and
    iterates DEFAULT_STATE's OWN keys, so a field a writer persists but
    `DEFAULT_STATE` does not name is written to disk and then invisible to
    every Python reader — worse than not writing it, because the file looks
    right to anyone inspecting it by hand while the code sees nothing.

    That is not hypothetical: `issue_status` was added to `mark_parked`'s
    literal first and behaved exactly this way until `DEFAULT_STATE` named it
    too. The sibling test above covers the two reissue counters across all
    four writers; this covers the whole persisted key set for the two writers
    that produce terminal states, which is the check that would have caught it
    without anyone thinking to look."""
    RP.mark_parked("p-1", reason="verify_up_failed", handoff_path="/tmp/h.md",
                   issue_id=None, issue_status="error:500", state_dir=tmp_path)
    RP.mark_rolling("r-1", predecessor=_PREDECESSOR, successor=_SUCCESSOR,
                    successor_window=2, state_dir=tmp_path)
    # `evaluate`'s two hand-listed literals as well, so a key one of them writes
    # that `DEFAULT_STATE` lacks is caught here.
    #
    # THIS DOES NOT CATCH A KEY MISSING FROM ONE OF THOSE LITERALS, and an
    # earlier version of this comment claimed it did. The check is a SUBSET —
    # persisted keys must be visible — so an omission makes the file smaller
    # and stays green. Confirmed by planting the omission rather than reasoning
    # about it: it passes here.
    # `test_both_evaluate_branches_persist_exactly_the_default_state_keys` is
    # what catches that, by asserting the key set instead.
    _write_override(tmp_path, "f-1", "force")
    _evaluate(tmp_path, "f-1", "PostToolUse", policy={"mode": "observe", "seek_pct": 70})
    _evaluate(tmp_path, "e-1", "Stop", pct=72,
              policy={"mode": "enforce", "seek_pct": 70, "floor_pct": 90})

    for session_id in ("p-1", "r-1", "f-1", "e-1"):
        persisted = set(json.loads((tmp_path / f"rollover-{session_id}.json").read_text()))
        visible = set(RP.read_state(session_id, tmp_path))
        assert not (persisted - visible), (
            f"{session_id}: {sorted(persisted - visible)} written to disk but dropped by "
            "read_state — add them to DEFAULT_STATE")


def test_clear_parked_removes_the_state_and_says_so(tmp_path):
    RP.mark_parked("pred-1", reason="spawn_error", handoff_path="/tmp/h/handoff.md",
                   issue_id="issue_ABC", state_dir=tmp_path)
    assert RP.clear_parked("pred-1", state_dir=tmp_path) is True
    assert RP.read_state("pred-1", tmp_path)["state"] == "ok"
    assert (tmp_path / "rollover-pred-1.json").exists() is False


def test_clear_parked_is_a_noop_with_no_state_file_at_all(tmp_path):
    assert RP.clear_parked("never-parked", state_dir=tmp_path) is False


def test_clear_parked_leaves_a_non_parked_state_untouched(tmp_path):
    RP.mark_rolling("pred-1", predecessor=_PREDECESSOR, successor=_SUCCESSOR,
                    successor_window=2, state_dir=tmp_path)
    before = RP.read_state("pred-1", tmp_path)

    assert RP.clear_parked("pred-1", state_dir=tmp_path) is False
    assert RP.read_state("pred-1", tmp_path) == before


# --- parked is terminal to evaluate() (Slice 6) -----------------------------


def test_evaluate_never_moves_a_parked_session_back_out(tmp_path):
    """Without this, the predecessor's own hooks — still firing while it sits
    parked and usable — would recompute a fresh zone from `ctx_pct` and tell
    it to seek or roll over again with no operator having acted."""
    RP.mark_parked("pred-1", reason="spawn_error", handoff_path="/tmp/h/handoff.md",
                   issue_id="issue_ABC", state_dir=tmp_path)
    _write_vitals(tmp_path, "pred-1", 95)  # would read as roll_now if recomputed
    policy_path = _write_policy(tmp_path, mode="enforce", seek_pct=70, floor_pct=90)

    result = RP.evaluate("pred-1", "PostToolUse", policy_path=policy_path, vitals_dir=tmp_path,
                         state_dir=tmp_path, override_dir=tmp_path)

    assert result["state"] == "parked"
    assert result["issue_id"] == "issue_ABC"
    assert result["should_block_stop"] is False
    assert RP.read_state("pred-1", tmp_path)["state"] == "parked", \
        "must not have been overwritten"


def test_evaluate_on_a_parked_session_ignores_a_force_override_too(tmp_path):
    RP.mark_parked("pred-1", reason="spawn_error", handoff_path="/tmp/h/handoff.md",
                   issue_id=None, state_dir=tmp_path)
    RP.write_override("pred-1", "force", override_dir=tmp_path)
    policy_path = _write_policy(tmp_path, mode="enforce", seek_pct=70)

    result = RP.evaluate("pred-1", "PostToolUse", policy_path=policy_path,
                         vitals_dir=tmp_path, state_dir=tmp_path, override_dir=tmp_path)

    assert result["state"] == "parked"


def test_evaluate_on_a_parked_session_ignores_a_veto_override_too(tmp_path):
    """Round-4 finding on PR #584: only `force` was pinned against a parked
    session at this layer. `veto` never reaches `resolve_override` either —
    the terminal short-circuit at the top of `evaluate()` returns `prior`
    before `read_override` is even consulted — so a veto recorded with the
    exact zone `mark_parked` sets (`natural_state == "parked"`) still leaves
    the read `parked`, with its original `reason`."""
    RP.mark_parked("pred-1", reason="spawn_error", handoff_path="/tmp/h/handoff.md",
                   issue_id=None, state_dir=tmp_path)
    RP.write_override("pred-1", "veto", zone="parked", override_dir=tmp_path)
    policy_path = _write_policy(tmp_path, mode="enforce", seek_pct=70)

    result = RP.evaluate("pred-1", "PostToolUse", policy_path=policy_path,
                         vitals_dir=tmp_path, state_dir=tmp_path, override_dir=tmp_path)

    assert result["state"] == "parked"
    assert result["reason"] == "spawn_error"


def test_evaluate_on_a_parked_session_ignores_a_seek_override_too(tmp_path):
    """Same matrix cell as the veto/force tests above, for `seek`."""
    RP.mark_parked("pred-1", reason="spawn_error", handoff_path="/tmp/h/handoff.md",
                   issue_id=None, state_dir=tmp_path)
    RP.write_override("pred-1", "seek", override_dir=tmp_path)
    policy_path = _write_policy(tmp_path, mode="enforce", seek_pct=70)

    result = RP.evaluate("pred-1", "PostToolUse", policy_path=policy_path,
                         vitals_dir=tmp_path, state_dir=tmp_path, override_dir=tmp_path)

    assert result["state"] == "parked"
    assert result["reason"] == "spawn_error"
