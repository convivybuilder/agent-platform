"""tools/outbound-guard.py — the machine-wide PreToolUse hook that stops any
session sending mail, invitations or messages to anyone but the operator.

Every case runs the real main() in-process against a tool call on stdin and
asserts the exit code the harness reads: 2 blocks, 0 passes. SATCHEL's own
suite is ported as data (tests/fixtures/outbound_guard/cases.json) with the operator's
real addresses replaced by own1..own5@example.test; the cases live in a data
file because a Bash command spelling out a blocked send would itself be refused
once the guard watches Bash. The address file and the log point at tmp_path,
so nothing here reads this machine's own config."""
import importlib.machinery
import importlib.util
import io
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GUARD = ROOT / "tools" / "outbound-guard.py"
FIX = ROOT / "tests" / "fixtures" / "outbound_guard"
OWN = ", ".join(f"own{i}@example.test" for i in range(1, 6))
CASES = json.loads((FIX / "cases.json").read_text())


def _load(name="outbound_guard"):
    loader = importlib.machinery.SourceFileLoader(name, str(GUARD))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def guard(tmp_path, monkeypatch):
    mod = _load()
    config = tmp_path / "outbound-guard.env"
    config.write_text(f"# the operator's own addresses\nOWN_ADDRESSES={OWN}\n")
    monkeypatch.setattr(mod, "OWN_CONFIG", str(config))
    monkeypatch.setattr(mod, "LOG", str(tmp_path / "state" / "outbound-guard.log"))
    return mod


def run(guard, monkeypatch, payload):
    """Exit code main() ends with, for `payload` on stdin (a str is sent raw)."""
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr("sys.stdin", io.StringIO(raw))
    with pytest.raises(SystemExit) as exc:
        guard.main()
    return exc.value.code


def _payload(case):
    if "raw" in case:
        return case["raw"]
    if "bash" in case:
        cmd = (case["bash"].replace("{FIX}", str(FIX / "scripts"))
               .replace("{GUARD}", str(GUARD)))
        return {"tool_name": "Bash", "tool_input": {"command": cmd},
                "cwd": case.get("cwd", str(FIX))}
    return {"tool_name": case["tool"], "tool_input": case["input"]}


def _id(case):
    label = case.get("raw") or case.get("bash") or f"{case['tool']} {json.dumps(case['input'])}"
    return f"{case['expect']}:{label.replace(chr(10), ' ')[:60]}"


def test_the_ported_suite_is_satchels_whole_suite():
    assert len(CASES) == 75


@pytest.mark.parametrize("case", CASES, ids=[_id(c) for c in CASES])
def test_eas_case(guard, monkeypatch, case):
    want = {"BLOCK": 2, "ALLOW": 0}[case["expect"]]
    assert run(guard, monkeypatch, _payload(case)) == want


def test_a_missing_address_file_blocks_every_recipient_and_names_the_file(
        guard, monkeypatch, tmp_path, capsys):
    """Fail closed: with no own addresses configured, even the operator is outside."""
    monkeypatch.setattr(guard, "OWN_CONFIG", str(tmp_path / "absent.env"))
    code = run(guard, monkeypatch, {"tool_name": "mcp__claude_ai_Google_Calendar__create_event",
                                    "tool_input": {"attendees": ["own1@example.test"]}})
    assert code == 2
    assert str(tmp_path / "absent.env") in capsys.readouterr().err


@pytest.mark.parametrize("text", [
    "OWN_ADDRESSES=\n",
    "OTHER_KEY=own1@example.test\n",
    "OWN_ADDRESSES=not-an-address, $(rm -rf ~)\n",
])
def test_an_address_file_with_no_usable_address_reads_as_empty(guard, tmp_path, text):
    (tmp_path / "outbound-guard.env").write_text(text)
    assert guard.own_addresses() == set()


def test_the_address_file_tolerates_quotes_and_spaces(guard, tmp_path):
    (tmp_path / "outbound-guard.env").write_text(
        "OWN_ADDRESSES=\"own1@example.test  Own2@Example.test\"\n")
    assert guard.own_addresses() == {"own1@example.test", "own2@example.test"}


def test_a_block_is_logged_under_the_state_dir(guard, monkeypatch, tmp_path):
    run(guard, monkeypatch, {"tool_name": "mcp__claude_ai_Gmail__send_message",
                             "tool_input": {"to": "x@y.test"}})
    log = (tmp_path / "state" / "outbound-guard.log").read_text()
    assert "BLOCKED mcp__claude_ai_Gmail__send_message" in log


def test_the_deployed_paths_are_agent_platform_state_and_convivy_config():
    """SATCHEL's copy logged into ~/Archive/logs, SATCHEL's black-box archive, and kept
    the operator's addresses in the source. A fresh load, since the fixture repoints both."""
    fresh = _load("og_fresh")
    assert Path(fresh.LOG).parts[-4:] == (".local", "state", "agent-platform", "outbound-guard.log")
    assert Path(fresh.OWN_CONFIG).parts[-3:] == (".config", "convivy", "outbound-guard.env")
    assert "@" not in GUARD.read_text().split('"""', 2)[1].replace("a@x, b@y", "")


SLACK = "mcp__claude_ai_Slack__"


@pytest.mark.parametrize("tool", ["mcp__some_outlook__send_email", "mcp__some_sms__send_sms",
                                  "mcp__any__send_text", "mcp__any__schedule_mail"])
def test_a_send_named_action_on_any_server_is_blocked(guard, monkeypatch, tool):
    """The next connector's spelling is caught without anyone adding its name."""
    assert run(guard, monkeypatch, {"tool_name": tool, "tool_input": {"subject": "x"}}) == 2


@pytest.mark.parametrize("action", ["slack_send_message", "slack_schedule_message",
                                    "slack_create_conversation", "slack_create_canvas",
                                    "slack_update_canvas", "slack_send_message_draft"])
def test_slack_writes_that_reach_people_are_blocked(guard, monkeypatch, action):
    """`send_message` never matched `slack_send_message`: ALWAYS_BLOCK is exact."""
    assert run(guard, monkeypatch, {"tool_name": SLACK + action,
                                    "tool_input": {"channel_id": "C1", "text": "hi"}}) == 2


def test_a_slack_reaction_is_allowed(guard, monkeypatch):
    """Not an email, an invitation or a message, which are the rule's terms."""
    assert run(guard, monkeypatch, {"tool_name": SLACK + "slack_add_reaction",
                                    "tool_input": {"channel_id": "C1", "name": "eyes"}}) == 0


def test_every_slack_write_the_dispatcher_denies_is_decided_here():
    """A Slack write added to _SLACK_WRITE_DENY must be either blocked here or
    named as deliberately allowed, so a new send action cannot slip past."""
    import re as _re
    src = (ROOT / "bin" / "agentctl").read_text()
    blk = src[src.index("_SLACK_WRITE_DENY = ("):]
    names = set(_re.findall(r"mcp__claude_ai_Slack__(\w+)", blk[:blk.index(")")]))
    decided = set(_load("og_slack").ALWAYS_BLOCK) | {"slack_add_reaction"}
    assert names <= decided, names - decided


CAL = "mcp__claude_ai_Google_Calendar__"


@pytest.mark.parametrize("action,calendar,want", [
    ("create_event", "shared-inbox@example.test", 2),
    ("update_event", "someone@elsewhere.test", 2),
    ("create_event", "own1@example.test", 0),
    ("create_event", "primary", 0),
    ("list_events", "someone@elsewhere.test", 0),
    ("get_event", "someone@elsewhere.test", 0),
])
def test_a_calendar_write_is_checked_against_whose_calendar_it_lands_on(
        guard, monkeypatch, action, calendar, want):
    """Security review, PR 525: an event written onto the shared inbox's calendar,
    with no attendees at all, reached it exactly as the original invite did."""
    assert run(guard, monkeypatch, {"tool_name": CAL + action,
                                    "tool_input": {"summary": "x", "calendarId": calendar}}) == want


# A calendar the operator has named in OWN_CALENDARS: shared with one other
# person on purpose, who sees it but is never invited or notified.
SHARED_CAL = "shared0001@group.calendar.example"
OTHER_OWN_CAL = "shared0002@group.calendar.example"
UNKNOWN_CAL = "unknown0001@group.calendar.example"


@pytest.fixture
def guard_cal(tmp_path, monkeypatch):
    mod = _load()
    config = tmp_path / "outbound-guard.env"
    config.write_text(
        f"OWN_ADDRESSES={OWN}\nOWN_CALENDARS={SHARED_CAL}, {OTHER_OWN_CAL}\n")
    monkeypatch.setattr(mod, "OWN_CONFIG", str(config))
    monkeypatch.setattr(mod, "LOG", str(tmp_path / "state" / "outbound-guard.log"))
    return mod


def test_a_configured_shared_calendar_allows_create_event(guard_cal, monkeypatch):
    assert run(guard_cal, monkeypatch, {"tool_name": CAL + "create_event",
                                        "tool_input": {"summary": "x",
                                                       "calendarId": SHARED_CAL}}) == 0


def test_a_configured_shared_calendar_allows_update_event_case_insensitively(guard_cal, monkeypatch):
    assert run(guard_cal, monkeypatch, {"tool_name": CAL + "update_event",
                                        "tool_input": {"eventId": "e",
                                                       "calendar_id": SHARED_CAL.upper()}}) == 0


def test_a_shared_calendar_write_still_blocks_an_outside_attendee(guard_cal, monkeypatch):
    assert run(guard_cal, monkeypatch, {"tool_name": CAL + "create_event",
                                        "tool_input": {"calendarId": SHARED_CAL,
                                                       "attendees": ["someone@elsewhere.test"]}}) == 2


def test_a_shared_calendar_write_blocks_the_operators_own_address_as_a_guest(guard_cal, monkeypatch):
    """Any guest on the shared event's list invites them, including the operator."""
    assert run(guard_cal, monkeypatch, {"tool_name": CAL + "create_event",
                                        "tool_input": {"calendarId": SHARED_CAL,
                                                       "attendees": ["own1@example.test"]}}) == 2


@pytest.mark.parametrize("field,value", [
    ("sendUpdates", "all"),
    ("send_updates", "externalOnly"),
    ("sendNotifications", True),
])
def test_a_shared_calendar_write_blocks_a_field_that_would_notify(guard_cal, monkeypatch, field, value):
    assert run(guard_cal, monkeypatch, {"tool_name": CAL + "create_event",
                                        "tool_input": {"calendarId": SHARED_CAL,
                                                       field: value}}) == 2


def test_a_shared_calendar_write_blocks_conference_data(guard_cal, monkeypatch):
    assert run(guard_cal, monkeypatch, {"tool_name": CAL + "create_event",
                                        "tool_input": {"calendarId": SHARED_CAL,
                                                       "conferenceData": {"createRequest": {}}}}) == 2


def test_a_shared_calendar_write_allows_send_updates_none(guard_cal, monkeypatch):
    assert run(guard_cal, monkeypatch, {"tool_name": CAL + "create_event",
                                        "tool_input": {"calendarId": SHARED_CAL,
                                                       "sendUpdates": "none"}}) == 0


def test_an_own_calendars_id_used_as_an_attendee_elsewhere_is_still_outside(guard_cal, monkeypatch):
    """OWN_CALENDARS governs calendarId only; under a recipient-shaped key it is
    just another address, and it is not in OWN_ADDRESSES."""
    assert run(guard_cal, monkeypatch, {"tool_name": CAL + "create_event",
                                        "tool_input": {"calendarId": "someone-else@elsewhere.test",
                                                       "attendees": [SHARED_CAL]}}) == 2


def test_an_unconfigured_calendar_still_blocks_with_own_calendars_set(guard_cal, monkeypatch):
    assert run(guard_cal, monkeypatch, {"tool_name": CAL + "create_event",
                                        "tool_input": {"summary": "x",
                                                       "calendarId": UNKNOWN_CAL}}) == 2


def test_a_call_with_two_different_approved_calendar_ids_is_allowed(guard_cal, monkeypatch):
    """Both OWN_CALENDARS ids in one call, under different spellings of the live
    parameter, are each approved -- this also exercises OTHER_OWN_CAL (round 1
    review note: the second configured id was never exercised)."""
    assert run(guard_cal, monkeypatch, {"tool_name": CAL + "create_event",
                                        "tool_input": {"calendarId": SHARED_CAL,
                                                       "calendar_id": OTHER_OWN_CAL}}) == 0


def test_an_unlisted_calendar_id_blocks_despite_a_listed_decoy_under_another_spelling(
        guard_cal, monkeypatch):
    """Security review, PR 958: an agent puts an unlisted calendar under the
    live `calendarId` key and a listed decoy under `calendar_id`; the OWN_CALENDARS
    exception must require every matching key to be approved, not just one."""
    assert run(guard_cal, monkeypatch, {"tool_name": CAL + "create_event",
                                        "tool_input": {"calendarId": UNKNOWN_CAL,
                                                       "calendar_id": SHARED_CAL}}) == 2


def test_an_unlisted_calendar_id_blocks_despite_a_listed_decoy_under_the_other_spelling(
        guard_cal, monkeypatch):
    """Same bypass attempt as above, with the live key and the decoy swapped."""
    assert run(guard_cal, monkeypatch, {"tool_name": CAL + "update_event",
                                        "tool_input": {"calendar_id": UNKNOWN_CAL,
                                                       "calendarId": SHARED_CAL}}) == 2


def test_an_unlisted_calendar_id_nested_in_the_call_blocks_too(guard_cal, monkeypatch):
    """A calendar-id-shaped key does not have to sit at the top level of the
    call to count."""
    assert run(guard_cal, monkeypatch, {"tool_name": CAL + "create_event",
                                        "tool_input": {"calendarId": SHARED_CAL,
                                                       "override": {"calendarId": UNKNOWN_CAL}}}) == 2


def test_with_own_calendars_absent_behaviour_is_unchanged(guard, monkeypatch):
    """`guard`'s config has no OWN_CALENDARS key at all."""
    assert run(guard, monkeypatch, {"tool_name": CAL + "create_event",
                                    "tool_input": {"calendarId": SHARED_CAL}}) == 2
    assert run(guard, monkeypatch, {"tool_name": CAL + "create_event",
                                    "tool_input": {"calendarId": "own1@example.test"}}) == 0
