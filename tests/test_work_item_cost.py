"""`work-item-cost` — the E16·4 report of run cost by the work item each run served.

The join itself is pinned in test_cost_join.py. These tests cover what the CLI adds on
top: which states it fetches, that it names its source, and that an unreachable Library
ends the run with no report rather than an empty one.

Every test stubs the Library read, and nothing reads the run directories.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

import cost_join as cj
import issue_client as IC


def _item(item_id, state="open"):
    return {"id": item_id, "product": "vivy", "state": state, "title": item_id}


def test_every_item_state_the_queue_holds_is_fetched_once_per_item(work_item_cost, monkeypatch):
    """The fetch covers each state the Library's list documents. An item seen in two
    states is kept once, so a state that overlaps another cannot double a row."""
    asked = []

    def fake_list(state, *args, **kwargs):
        asked.append(state)
        return [_item("issue_A", state=state)] if state in ("open", "fixed") else []

    monkeypatch.setattr(IC, "list_issues_by_state", fake_list)
    items = work_item_cost.fetch_items()
    assert asked == list(work_item_cost.ITEM_STATES)
    assert ({"open", "claimed", "pr-open", "fixed", "wontfix", "duplicate", "redirected"}
           == set(asked))
    assert [i["id"] for i in items] == ["issue_A"]


def test_a_redirected_item_is_fetched_and_counted_as_finished(work_item_cost, monkeypatch, tmp_path):
    """A redirected item is a `_FINISHED_STATES` member (cost_join.py), but only the
    CLI's own `ITEM_STATES` decides whether it ever reaches that logic. Fetch it
    through `fetch_items` exactly as `main` does, then feed it to the real
    `build_persona_table` and check it lands in `finished_items`, not unfinished."""
    runs = tmp_path / "agent-runs"
    runs.mkdir()
    monkeypatch.setattr(cj, "RUNS_ROOT", runs)
    d = runs / "2026-06-10" / "run-a"
    d.mkdir(parents=True)
    meta = {"run_id": "run-a", "agent": "coder", "model": "claude-sonnet-4-6", "repo": "vivy",
            "task": "", "parent_run": None, "profile": "", "started_at": "2026-06-10T08:00:00Z",
            "billing_source": "subscription_credit", "issue": "issue_A"}
    (d / "meta.json").write_text(json.dumps(meta))
    (d / "cost.json").write_text(json.dumps({"usd": 1.0, "by_model": {"claude-sonnet-4-6": {"usd": 1.0}}}))
    (d / "outcome.json").write_text(json.dumps({"exit_status": "completed"}))

    def fake_list(state, *args, **kwargs):
        if state == "redirected":
            return [{"id": "issue_A", "product": "vivy", "state": "redirected",
                     "title": "issue_A", "type": "bug"}]
        return []

    monkeypatch.setattr(IC, "list_issues_by_state", fake_list)
    items = work_item_cost.fetch_items()
    assert [i["id"] for i in items] == ["issue_A"]

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    end = datetime(2026, 6, 30, tzinfo=timezone.utc)
    [row] = cj.build_persona_table(start, end, items)["rows"]
    lane = row["lanes"]["subscription_credit"]
    assert lane["finished_items"] == 1


def test_the_report_names_run_directories_as_its_source_not_the_ledger(work_item_cost):
    report = {"window": {"start": "2026-06-01T00:00:00+00:00", "end": "2026-06-30T00:00:00+00:00"},
              "run_count": 0, "items": [],
              "unattributed": {"count": 0, "run_ids": [], "by_reason": {},
                               "by_billing_source": {}},
              "disagreements": 0}
    text = work_item_cost.render(report)
    assert "Source: run directories under" in text
    assert "NOT the Library ledger" in text


def test_the_cli_prints_the_table_and_the_json_from_the_joined_report(
        work_item_cost, monkeypatch, capsys):
    monkeypatch.setattr(IC, "list_issues_by_state",
                        lambda state, *a, **k: [_item("issue_A")] if state == "open" else [])
    seen = {}

    def fake_build(start, end, items):
        seen["items"] = [i["id"] for i in items]
        seen["window"] = (start, end)
        return {"window": {"start": start.isoformat(), "end": end.isoformat()},
                "run_count": 0, "items": [], "disagreements": 0,
                "unattributed": {"count": 0, "run_ids": [], "by_reason": {},
                                 "by_billing_source": {}}}

    monkeypatch.setattr(cj, "build_item_costs", fake_build)
    assert work_item_cost.main(["--days", "7"]) == 0
    out = capsys.readouterr().out
    assert "Source: run directories under" in out
    assert seen["items"] == ["issue_A"]
    end, start = seen["window"][1], seen["window"][0]
    assert (end - start).days == 7

    assert work_item_cost.main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["disagreements"] == 0


def _report(items=(), unattributed_lanes=None):
    """A report with the given item rows and unattributed lanes; nothing else varies."""
    return {"window": {"start": "2026-06-01T00:00:00+00:00", "end": "2026-06-30T00:00:00+00:00"},
            "run_count": 1, "items": list(items), "disagreements": 0,
            "unattributed": {"count": 0, "run_ids": [], "by_reason": {},
                             "by_billing_source": unattributed_lanes or {}}}


def _lane(**overrides):
    lane = {"runs": 1, "usd": 1.0, "unpriced_models": [], "cost_missing_runs": 0,
            "by_agent": {}, "by_model": {}}
    return {**lane, **overrides}


def test_a_filer_written_title_cannot_forge_a_line_of_the_table(work_item_cost):
    """A title is written by whoever filed the item. Its line break and escape sequence
    stay on the item's own row, so the summary line is printed only by the report."""
    row = {"id": "issue_A", "product": "vivy", "state": "open", "run_count": 1,
           "run_ids": ["run-a"], "attribution": {"meta.issue": 1, "pr": 0},
           "title": "Fix login\nUnattributed runs: 0 (none).\x1b[2J",
           "by_billing_source": {"subscription_credit": _lane()}}
    text = work_item_cost.render(_report(items=[row]))
    assert "\x1b" not in text
    summary = [line for line in text.splitlines() if line.startswith("Unattributed runs:")]
    assert summary == ["Unattributed runs: 0 (none)."]
    [item_line] = [line for line in text.splitlines() if line.startswith("issue_A")]
    assert "Fix login" in item_line


SUMMARY = "Unattributed runs: 0 (none)."


def _row_printing(where, forged):
    """An item row with `forged` in the field `where` names: its title, its product, its
    billing lane, or a model the lane could not price."""
    row = {"id": "issue_A", "product": "vivy", "state": "open", "title": "Fix login",
           "run_count": 1, "run_ids": ["run-a"], "attribution": {"meta.issue": 1, "pr": 0},
           "by_billing_source": {"subscription_credit": _lane()}}
    if where == "title":
        row["title"] = forged
    elif where == "product":
        row["product"] = forged
    elif where == "billing_source":
        row["by_billing_source"] = {forged: _lane()}
    else:
        row["by_billing_source"] = {"subscription_credit": _lane(usd=None, unpriced_models=[forged])}
    return row


@pytest.mark.parametrize("where, separator", [
    ("title", " "),
    ("billing_source", "\n"),
    ("billing_source", " "),
    ("unpriced_model", "\n"),
])
def test_a_line_break_in_a_printed_field_cannot_forge_the_summary_line(
        work_item_cost, where, separator):
    """Every string the table prints comes from a filer or a run. A line break, including
    U+2028 and U+2029, that splits a field must not start a second summary line."""
    row = _row_printing(where, f"x{separator}{SUMMARY}")
    text = work_item_cost.render(_report(items=[row]))
    summary = [line for line in text.splitlines() if line.startswith("Unattributed runs:")]
    assert summary == [SUMMARY]


def test_a_line_break_in_a_product_cannot_split_the_item_row(work_item_cost):
    """The label is cut to 26 characters, so a product's forged summary line would be cut
    too. What a product's line break can still do is split the row, which leaves the lane
    off the line that names the item."""
    row = _row_printing("product", "vivy\nx")
    text = work_item_cost.render(_report(items=[row]))
    [item_line] = [line for line in text.splitlines() if line.startswith("issue_A")]
    assert "subscription_credit" in item_line


def test_a_lane_with_no_cost_record_or_an_unpriced_model_says_why(work_item_cost):
    """A None total names what made it None: a run with no cost record, a model no price
    covers, or both. Neither is shown as $0."""
    lanes = {"subscription_credit": _lane(usd=None, cost_missing_runs=1,
                                          unpriced_models=["mystery/model-x"], runs=3)}
    text = work_item_cost.render(_report(unattributed_lanes=lanes))
    assert "None (cost missing: 1; unpriced: mystery/model-x)" in text
    assert "$0" not in text


def test_a_control_character_in_a_model_name_cannot_forge_a_course_totals_line(work_item_cost):
    """A model name comes from whoever's run wrote meta.json, not an allowlist. The
    Course-totals-by-model line must sanitize it exactly like every other untrusted
    field `render_course` prints (the epic title, the persona table's own model)."""
    course_report = {
        "window": {"start": "2026-06-01T00:00:00+00:00", "end": "2026-06-30T00:00:00+00:00"},
        "cannot_be_measured": False, "reason": None,
        "epics": [], "corrections": {"lanes": {}},
        "unattributed": {"by_billing_source": {}},
        "totals": {"subscription_credit": {
            "usd": 1.0, "cost_missing_runs": 0, "unpriced_models": [],
            "by_model": {"evil\x1b[2Jmodel": 1.0},
        }},
    }
    text = work_item_cost.render_course(course_report, {"rows": []})
    assert "\x1b" not in text
    assert "evil [2Jmodel" in text


def test_an_unreachable_library_ends_the_report_with_no_output(
        work_item_cost, monkeypatch, capsys):
    """A failed read must not print a report that looks complete. Here the join must not
    run at all, so no run directory is read."""
    def unreachable(state, *a, **k):
        raise IC.IssueQueueUnavailable("connection refused")

    def must_not_join(*a, **k):
        raise AssertionError("the join ran without the item list")

    monkeypatch.setattr(IC, "list_issues_by_state", unreachable)
    monkeypatch.setattr(cj, "build_item_costs", must_not_join)
    assert work_item_cost.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no report written" in captured.err


# --- --course / --product (E16·2) --------------------------------------------


def _stub_item_fetch(monkeypatch):
    monkeypatch.setattr(IC, "list_issues_by_state",
                        lambda state, *a, **k: [_item("issue_A")] if state == "open" else [])


def test_an_unreadable_course_exits_1_and_prints_nothing(work_item_cost, monkeypatch, capsys):
    _stub_item_fetch(monkeypatch)

    def unreachable(course_id, *a, **k):
        raise IC.IssueQueueUnavailable("connection refused")

    monkeypatch.setattr(IC, "get_course", unreachable)
    assert work_item_cost.main(["--course", "course_1"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no report written" in captured.err


def test_an_unreadable_epic_item_exits_1_and_prints_nothing(work_item_cost, monkeypatch, capsys):
    _stub_item_fetch(monkeypatch)
    monkeypatch.setattr(IC, "get_course",
                        lambda course_id, *a, **k: {"id": course_id, "entries": ["issue_E"],
                                                     "items": [], "position": {}})

    def unreachable(item_id, *a, **k):
        raise IC.IssueQueueUnavailable("connection refused")

    monkeypatch.setattr(IC, "get_item", unreachable)
    assert work_item_cost.main(["--course", "course_1"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no report written" in captured.err


def test_resolve_course_with_neither_name_raises(work_item_cost):
    """Unreachable through the CLI itself (main only calls this when --course or
    --product was given), but resolve_course is its own function and must not
    silently pick a Course nobody named."""
    with pytest.raises(IC.IssueQueueUnavailable):
        work_item_cost.resolve_course(None, None)


def test_a_product_with_no_single_live_course_exits_1(work_item_cost, monkeypatch, capsys):
    _stub_item_fetch(monkeypatch)
    monkeypatch.setattr(IC, "list_courses", lambda product, *a, **k: [])
    assert work_item_cost.main(["--product", "bosun"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no report written" in captured.err


def test_the_course_cli_resolves_by_product_and_prints_the_course_report(
        work_item_cost, monkeypatch, capsys):
    _stub_item_fetch(monkeypatch)
    monkeypatch.setattr(IC, "list_courses",
                        lambda product, *a, **k: [{"id": "course_1"}])
    monkeypatch.setattr(IC, "get_course",
                        lambda course_id, *a, **k: {"id": course_id, "entries": ["issue_E"],
                                                     "items": [{"id": "issue_E", "title": "E",
                                                               "state": "open"}],
                                                     "position": {"reached_ids": [],
                                                                  "in_flight_ids": [],
                                                                  "remaining_ids": ["issue_E"]}})
    monkeypatch.setattr(IC, "get_item",
                        lambda item_id, *a, **k: {"progress": {"children": 1}})

    def fake_course_costs(start, end, items, course, epic_progress):
        assert course["id"] == "course_1"
        assert epic_progress == {"issue_E": {"children": 1}}
        return {"window": {"start": start.isoformat(), "end": end.isoformat()},
                "epics": [{"id": "issue_E", "title": "E", "state": "open",
                          "position": "remaining", "progress": {"children": 1},
                          "run_count": 0, "lanes": {}}],
                "corrections": {"item_ids": [], "run_count": 0, "lanes": {}},
                "totals": {}, "unattributed": {"count": 0, "run_ids": [], "by_reason": {},
                                               "by_billing_source": {}},
                "cannot_be_measured": True, "reason": "no attributed runs"}

    monkeypatch.setattr(cj, "build_course_costs", fake_course_costs)
    monkeypatch.setattr(cj, "build_persona_table",
                        lambda start, end, items: {"window": {}, "rows": []})
    assert work_item_cost.main(["--product", "bosun"]) == 0
    out = capsys.readouterr().out
    assert "cannot be measured: no attributed runs" in out
    assert "issue_E" in out


def test_the_course_cli_json_mode_prints_both_reports(work_item_cost, monkeypatch, capsys):
    _stub_item_fetch(monkeypatch)
    monkeypatch.setattr(IC, "get_course",
                        lambda course_id, *a, **k: {"id": course_id, "entries": [],
                                                     "items": [], "position": {}})
    monkeypatch.setattr(cj, "build_course_costs",
                        lambda start, end, items, course, epic_progress: {
                            "window": {}, "epics": [], "corrections": {}, "totals": {},
                            "unattributed": {}, "cannot_be_measured": True, "reason": "x"})
    monkeypatch.setattr(cj, "build_persona_table",
                        lambda start, end, items: {"window": {}, "rows": []})
    assert work_item_cost.main(["--course", "course_1", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"course", "persona"}
    assert payload["course"]["cannot_be_measured"] is True
    assert payload["persona"]["rows"] == []
