"""Pure report assembly for bin/agent-cost-report (ARCH-136).

Guards the cost-attribution logic that makes the cost dashboards trustworthy:
the Anthropic-estimate vs gateway-actual split, the gateway-model exclusion from
the ccusage estimate (so a model isn't double-counted), and the Fable row flag.
The ccusage/gateway IO is stubbed — these pin the math.

ARCH-362 R3 retired the Slack summary post, so these assert against the plain
text and the snapshot rather than a Slack block payload. The since-last-post
delta went with the post — it diffed against the previous post, so with no post
it could never have a value. The Fable ALERT is a separate structured signal,
covered by the maybe_alert_fable tests below.
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import pytest


def _build(acr, anthropic, gateway, monkeypatch, apps=None):
    monkeypatch.setattr(acr, "ccusage_by_model", lambda day: anthropic)
    monkeypatch.setattr(acr, "gateway_by_model", lambda day: gateway)
    # PA's real ledger exists on the dev box, so the app lane must be stubbed too
    # or it would leak real spend into the pinned-math tests (ARCH-147).
    monkeypatch.setattr(acr, "apps_by_app", lambda day: apps or {})
    # build_report returns (total, plain, snapshot); the snapshot is exercised
    # in its own test below.
    total, plain, _ = acr.build_report(date(2026, 6, 11), "2026-06-11T15:30:00")
    return total, plain


def test_total_splits_anthropic_estimate_from_gateway_actual(agent_cost_report, monkeypatch):
    total, text = _build(
        agent_cost_report,
        {"claude-opus-4-8": 78.0, "claude-sonnet-4-6": 51.0},
        {"deepseek-v4-flash": 1.0, "kimi-k2.6": 0.5},
        monkeypatch,
    )
    assert total == 130.5  # 129 anthropic + 1.5 gateway
    assert "$129.00" in text and "$1.50" in text  # both subtotals surfaced


def test_fable_row_is_flagged_in_the_table(agent_cost_report, monkeypatch):
    """The inline row flag survives the retirement; the Slack alarm BLOCK does
    not. The alert itself is now maybe_alert_fable's structured signal."""
    _, text = _build(
        agent_cost_report, {"claude-fable-5": 318.17}, {}, monkeypatch
    )
    assert "FABLE" in text and "$318.17" in text


def test_no_fable_no_flag(agent_cost_report, monkeypatch):
    _, text = _build(
        agent_cost_report, {"claude-opus-4-8": 5.0}, {}, monkeypatch
    )
    assert "FABLE" not in text


def test_the_module_has_no_slack_dependency_at_all(agent_cost_report):
    """agent-platform#293 removed Slack from the substrate. This tool no longer
    imports the slack module, which is a STRONGER guarantee than the trap this
    test used to set.

    The old version stubbed `acr.slack.send` to raise, and its own docstring
    admitted the gap: a payload BUILT and discarded would still pass, which was
    the exact shape of the #160 finding. Absent the module, neither a send nor a
    build is expressible — so assert the absence directly rather than trapping one
    of the two ways to misuse something that is not there.

    This also guards the restore. #293 deleted this file outright as a "Slack-only
    alerter" when its actual deliverable was the dashboard snapshot; re-adding a
    slack import here is how that premise comes back."""
    acr = agent_cost_report
    assert not hasattr(acr, "slack"), (
        "agent-cost-report imported the slack module again; every alert it raises "
        "goes to /engineering/alerts via signal_client (agent-platform#293)")
    src = (Path(acr.__file__).read_text() if getattr(acr, "__file__", None)
           else "")
    if src:
        code = "\n".join(ln for ln in src.splitlines()
                         if not ln.lstrip().startswith("#"))
        assert "import slack" not in code
        assert "slack.send" not in code


def test_report_builds_no_discarded_payload(agent_cost_report, monkeypatch):
    """build_report returns the total, the printed text, and the snapshot — and
    nothing shaped like a notification payload.

    The data sources are stubbed on purpose: an earlier version asserted
    isinstance(total, float) without stubbing, so it passed on a dev box with real
    spend and failed in CI where the sources are empty and sum([]) is int 0. A
    test that depends on the machine having data is not pinning anything."""
    acr = agent_cost_report
    monkeypatch.setattr(acr, "ccusage_by_model", lambda day: {"claude-opus-4-8": 12.0})
    monkeypatch.setattr(acr, "gateway_by_model", lambda day: {})
    monkeypatch.setattr(acr, "apps_by_app", lambda day: {})

    total, plain, snap = acr.build_report(date(2026, 6, 11), "2026-06-11T15:30:00")
    assert total == 12.0
    assert snap["total"] == 12.0
    assert "$12.00" in plain
    assert "since last post" not in plain, "the delta died with the post it diffed against"


def test_snapshot_carries_structured_breakdown(agent_cost_report, monkeypatch):
    acr = agent_cost_report
    monkeypatch.setattr(acr, "ccusage_by_model", lambda day: {"claude-fable-5": 318.17, "claude-opus-4-8": 78.0})
    monkeypatch.setattr(acr, "gateway_by_model", lambda day: {"deepseek-v4-flash": 1.0})
    monkeypatch.setattr(acr, "apps_by_app", lambda day: {})
    _, _, snap = acr.build_report(date(2026, 6, 11), "2026-06-11T15:30:00")
    assert snap["date"] == "2026-06-11"
    assert snap["total"] == 397.17 and snap["fable"] == 318.17
    assert snap["anthropic_total"] == 396.17 and snap["gateway_total"] == 1.0
    assert snap["apps_total"] == 0.0
    # costliest first, deepseek tagged actual
    assert snap["by_model"][0]["model"] == "claude-fable-5"
    ds = next(r for r in snap["by_model"] if r["model"] == "deepseek-v4-flash")
    assert ds["kind"] == "actual"


def test_close_mode_headline_reports_final_not_so_far(agent_cost_report, monkeypatch):
    acr = agent_cost_report
    monkeypatch.setattr(acr, "ccusage_by_model", lambda day: {"claude-opus-4-8": 638.0})
    monkeypatch.setattr(acr, "gateway_by_model", lambda day: {})
    monkeypatch.setattr(acr, "apps_by_app", lambda day: {})
    # day-close run: FINAL headline + "Total:" label, no "as of HH:MM".
    # This is the reason the midnight job outlived the post it used to send.
    _, plain, _ = acr.build_report(date(2026, 6, 11), "2026-06-12T00:00:00", is_close=True)
    assert "2026-06-11 FINAL" in plain and "day close" in plain
    assert "as of" not in plain and "so far" not in plain
    assert plain.startswith("*💰 Agent cost — 2026-06-11 FINAL*")
    # the normal slot still reads "so far (as of ...)"
    _, plain2, _ = acr.build_report(date(2026, 6, 12), "2026-06-12T04:00:00")
    assert "so far" in plain2 and "as of 04:00" in plain2


def test_ccusage_by_model_allowlists_claude_only(agent_cost_report, monkeypatch):
    # The Anthropic estimate counts ONLY claude-* models (allowlist, ARCH-192):
    # gateway models that also show up in ccusage are dropped (priced from the
    # gateway spend log), AND a *novel* gateway name that the old substring
    # denylist would have missed is excluded too — that's the leak this fixes.
    acr = agent_cost_report
    rows = [{"modelBreakdowns": [
        {"modelName": "claude-opus-4-8", "cost": 78.0},
        {"modelName": "claude-fable-5", "cost": 318.17},
        {"modelName": "deepseek/deepseek-v4-flash", "cost": 4.0},   # gateway — must drop
        {"modelName": "gpt-5", "cost": 0.06},                   # gateway — must drop
        {"modelName": "qwen-3-max", "cost": 9.99},              # NOVEL gateway name — old denylist would LEAK it
    ]}]

    class _CP:
        returncode = 0
        stdout = json.dumps({"daily": rows})

    monkeypatch.setattr(acr.subprocess, "run", lambda *a, **k: _CP())
    monkeypatch.setattr(acr, "_resolve_npx", lambda: "npx")
    out = acr.ccusage_by_model(date(2026, 6, 18))
    assert out == {"claude-opus-4-8": 78.0, "claude-fable-5": 318.17}
    assert "qwen-3-max" not in out  # the allowlist closes the denylist leak


def _write_spend(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_gateway_by_model_aggregates_per_run_logs(agent_cost_report, monkeypatch, tmp_path):
    # The squeeze-visibility fix (2026-06-12): worker gateway spend lands in each
    # run's own <run_dir>/gateway_spend.jsonl, not the central log. Reading only
    # the central log showed $0 against real deepseek spend. gateway_by_model must
    # sum central + per-run, filtered by row ts (so a cross-midnight run counts
    # under the right day), skip blank-model rows, and never double-count.
    acr = agent_cost_report
    runs = tmp_path / "agent-runs"
    monkeypatch.setattr(acr, "RUNS_ROOT", runs)
    monkeypatch.setattr(acr, "GATEWAY_SPEND", runs / "gateway-spend")
    day = "2026-06-12"
    # central log: an uncorrelated co day-lane request
    _write_spend(runs / "gateway-spend" / f"{day}.jsonl",
                 [{"ts": f"{day}T09:00:00Z", "model": "deepseek-v4-flash", "usd": 0.01}])
    # a worker run under the day's own folder (+ a blank-model row that must skip)
    _write_spend(runs / day / "run-aaa" / "gateway_spend.jsonl",
                 [{"ts": f"{day}T10:00:00Z", "model": "deepseek-v4-flash", "usd": 0.50},
                  {"ts": f"{day}T10:01:00Z", "model": "kimi-k2.6", "usd": 0.20},
                  {"ts": f"{day}T10:02:00Z", "model": "", "usd": 0.99}])
    # a run from the PRIOR day's folder that emitted a row after midnight (counts
    # for `day`), plus one genuinely on the prior day (must be excluded by ts)
    _write_spend(runs / "2026-06-11" / "run-bbb" / "gateway_spend.jsonl",
                 [{"ts": f"{day}T00:05:00Z", "model": "deepseek-v4-flash", "usd": 0.05},
                  {"ts": "2026-06-11T23:00:00Z", "model": "deepseek-v4-flash", "usd": 9.99}])
    got = acr.gateway_by_model(date(2026, 6, 12))
    assert set(got) == {"deepseek-v4-flash", "kimi-k2.6"}
    assert got["deepseek-v4-flash"] == pytest.approx(0.56)  # 0.01 central + 0.50 run + 0.05 post-midnight
    assert got["kimi-k2.6"] == pytest.approx(0.20)


def test_gateway_by_model_empty_when_no_logs(agent_cost_report, monkeypatch, tmp_path):
    acr = agent_cost_report
    runs = tmp_path / "agent-runs"
    monkeypatch.setattr(acr, "RUNS_ROOT", runs)
    monkeypatch.setattr(acr, "GATEWAY_SPEND", runs / "gateway-spend")
    assert acr.gateway_by_model(date(2026, 6, 12)) == {}


# --- ARCH-147: direct-API app spend, the third cost lane ---

def test_app_spend_is_a_third_lane_in_total_and_summary(agent_cost_report, monkeypatch):
    # PA + cookbook out-of-pocket spend adds to the total as its own lane,
    # distinct from the Anthropic estimate and the gateway actual.
    total, text = _build(
        agent_cost_report,
        {"claude-opus-4-8": 100.0},
        {"deepseek-v4-flash": 2.0},
        monkeypatch,
        apps={"personal-assistant": 0.5, "cookbook": 0.25},
    )
    assert total == 102.75  # 100 anthropic + 2 gateway + 0.75 apps
    assert "Direct-API apps (out-of-pocket): $0.75" in text
    # each app surfaces as its own row, tagged as direct-API (not est/actual)
    assert "`personal-assistant" in text and "direct API" in text


def test_app_spend_snapshot_and_history_carry_apps_total(agent_cost_report, monkeypatch, tmp_path):
    acr = agent_cost_report
    monkeypatch.setattr(acr, "ccusage_by_model", lambda day: {"claude-opus-4-8": 10.0})
    monkeypatch.setattr(acr, "gateway_by_model", lambda day: {})
    monkeypatch.setattr(acr, "apps_by_app", lambda day: {"personal-assistant": 0.40})
    monkeypatch.setattr(acr, "COST_DIR", tmp_path / "cost-report")
    _, _, snap = acr.build_report(date(2026, 6, 13), "2026-06-13T15:30:00")
    assert snap["apps_total"] == 0.40
    pa = next(r for r in snap["by_model"] if r["model"] == "personal-assistant")
    assert pa["kind"] == "api" and pa["cost"] == 0.40
    # write_snapshot persists apps_total into both latest.json and the history row
    acr.write_snapshot(snap)
    latest = json.loads((tmp_path / "cost-report" / "latest.json").read_text())
    assert latest["apps_total"] == 0.40
    hist = (tmp_path / "cost-report" / "history.jsonl").read_text().splitlines()
    assert json.loads(hist[-1])["apps_total"] == 0.40


def test_write_snapshot_is_atomic_no_temp_residue(agent_cost_report, monkeypatch, tmp_path):
    # The /dashboard cost panel reads latest.json LIVE per request (ARCH-182) and
    # the 15-min refresh writes it often (ARCH-183), so writes go via _atomic_write
    # (temp + os.replace): the live files must always be valid JSON and no stray
    # temp file may be left behind.
    acr = agent_cost_report
    monkeypatch.setattr(acr, "ccusage_by_model", lambda day: {"claude-opus-4-8": 10.0})
    monkeypatch.setattr(acr, "gateway_by_model", lambda day: {})
    monkeypatch.setattr(acr, "apps_by_app", lambda day: {})
    cost_dir = tmp_path / "cost-report"
    monkeypatch.setattr(acr, "COST_DIR", cost_dir)
    _, _, snap = acr.build_report(date(2026, 6, 13), "2026-06-13T15:30:00")
    acr.write_snapshot(snap)
    # the live file parses cleanly...
    assert json.loads((cost_dir / "latest.json").read_text())["total"] == snap["total"]
    # ...and no .tmp.<pid> residue is left in the dir
    assert not list(cost_dir.glob(".*tmp*"))


def test_apps_by_app_sums_per_app_filtered_by_day(agent_cost_report, monkeypatch, tmp_path):
    # Each app's append-only ledger is summed for the day by ts-startswith (same
    # convention as gateway); other-day rows excluded, a missing ledger = $0, and
    # an app that nets $0 today is dropped from the result.
    acr = agent_cost_report
    pa = tmp_path / "pa" / "llm-spend.jsonl"
    cookbook = tmp_path / "cookbook" / "llm-spend.jsonl"  # never created → missing
    pa.parent.mkdir(parents=True, exist_ok=True)
    pa.write_text("".join(json.dumps(r) + "\n" for r in [
        {"ts": "2026-06-13T13:30:25Z", "model": "claude-sonnet-4-6", "usd": 0.032},
        {"ts": "2026-06-13T15:00:00Z", "model": "claude-sonnet-4-6", "usd": 0.018},
        {"ts": "2026-06-12T23:59:00Z", "model": "claude-sonnet-4-6", "usd": 9.99},  # other day
        {"ts": "2026-06-13T16:00:00Z", "model": "claude-sonnet-4-6"},               # no usd → 0
    ]))
    monkeypatch.setattr(acr, "APP_LEDGERS", {"personal-assistant": pa, "cookbook": cookbook})
    monkeypatch.setattr(acr, "APP_LEDGER_DIR", tmp_path / "no-collected")
    got = acr.apps_by_app(date(2026, 6, 13))
    assert got == {"personal-assistant": pytest.approx(0.050)}  # 0.032 + 0.018; cookbook absent (missing ledger)


def test_apps_by_app_empty_when_no_ledgers(agent_cost_report, monkeypatch, tmp_path):
    acr = agent_cost_report
    monkeypatch.setattr(acr, "APP_LEDGERS", {"personal-assistant": tmp_path / "nope.jsonl"})
    monkeypatch.setattr(acr, "APP_LEDGER_DIR", tmp_path / "no-collected")
    assert acr.apps_by_app(date(2026, 6, 13)) == {}


def test_apps_by_app_merges_collected_dir_for_cloud_apps(agent_cost_report, monkeypatch, tmp_path):
    # ARCH-152: cloud apps (vivy, constance) can't write the mini's disk, so the
    # ingest endpoint drops their events in APP_LEDGER_DIR/<app>.jsonl. apps_by_app
    # scans that dir too, keying the app off the filename stem, and merges it with
    # the mini-local APP_LEDGERS sources.
    acr = agent_cost_report
    pa = tmp_path / "pa.jsonl"
    pa.write_text(json.dumps({"ts": "2026-06-13T10:00:00Z", "usd": 0.10}) + "\n")
    collected = tmp_path / "llm-spend"
    collected.mkdir()
    (collected / "vivy.jsonl").write_text(
        json.dumps({"ts": "2026-06-13T11:00:00Z", "app": "vivy", "usd": 0.30}) + "\n"
        + json.dumps({"ts": "2026-06-12T11:00:00Z", "app": "vivy", "usd": 9.99}) + "\n")  # other day
    (collected / "constance.jsonl").write_text(
        json.dumps({"ts": "2026-06-13T12:00:00Z", "app": "constance", "usd": 0.05}) + "\n")
    monkeypatch.setattr(acr, "APP_LEDGERS", {"personal-assistant": pa})
    monkeypatch.setattr(acr, "APP_LEDGER_DIR", collected)
    got = acr.apps_by_app(date(2026, 6, 13))
    assert got == {"personal-assistant": pytest.approx(0.10),
                   "vivy": pytest.approx(0.30), "constance": pytest.approx(0.05)}


def test_apps_by_app_no_collected_dir_is_fine(agent_cost_report, monkeypatch, tmp_path):
    acr = agent_cost_report
    monkeypatch.setattr(acr, "APP_LEDGERS", {})
    monkeypatch.setattr(acr, "APP_LEDGER_DIR", tmp_path / "does-not-exist")
    assert acr.apps_by_app(date(2026, 6, 13)) == {}


def test_apps_by_app_reads_cookbook_cost_field(agent_cost_report, monkeypatch, tmp_path):
    # cookbook's ledger uses `cost` (and local-time ts, no Z); PA uses `usd`.
    # apps_by_app must sum whichever dollar field is present, across providers.
    acr = agent_cost_report
    cb = tmp_path / "usage.jsonl"
    cb.write_text("".join(json.dumps(r) + "\n" for r in [
        {"ts": "2026-06-13T13:30:25", "model": "claude-sonnet", "provider": "anthropic", "cost": 0.025},
        {"ts": "2026-06-13T14:00:00", "model": "deepseek-v4-flash", "provider": "deepseek", "cost": 0.005},
        {"ts": "2026-06-12T10:00:00", "model": "claude-sonnet", "provider": "anthropic", "cost": 9.99},  # other day
    ]))
    monkeypatch.setattr(acr, "APP_LEDGERS", {"cookbook": cb})
    monkeypatch.setattr(acr, "APP_LEDGER_DIR", tmp_path / "no-collected")
    got = acr.apps_by_app(date(2026, 6, 13))
    assert got == {"cookbook": pytest.approx(0.030)}  # 0.025 + 0.005, all providers, today only


def test_upsert_history_persists_by_model(agent_cost_report, monkeypatch, tmp_path):
    # The windowed by-model panel reads each history row's by_model; upsert must
    # carry it (not just the totals the sparkline needs). Idempotent by date.
    acr = agent_cost_report
    monkeypatch.setattr(acr, "COST_DIR", tmp_path / "cost-report")
    snap = {
        "date": "2026-06-14", "total": 9.0, "anthropic_total": 8.0,
        "gateway_total": 1.0, "apps_total": 0.0, "fable": 0.0,
        "by_model": [{"model": "claude-opus-4-8", "cost": 8.0, "kind": "est"},
                     {"model": "deepseek-v4-flash", "cost": 1.0, "kind": "actual"}],
    }
    acr.upsert_history(snap)
    acr.upsert_history({**snap, "total": 9.5})  # same date → replace, not append
    rows = [json.loads(ln) for ln in (tmp_path / "cost-report" / "history.jsonl").read_text().splitlines() if ln.strip()]
    assert len(rows) == 1 and rows[0]["total"] == 9.5
    assert [m["model"] for m in rows[0]["by_model"]] == ["claude-opus-4-8", "deepseek-v4-flash"]


def test_backfill_recomputes_rows_without_touching_latest(agent_cost_report, monkeypatch, tmp_path):
    # Backfill must populate N history rows (each with by_model) and NOT overwrite
    # latest.json, which is today's live snapshot owned by the normal run.
    acr = agent_cost_report
    monkeypatch.setattr(acr, "COST_DIR", tmp_path / "cost-report")
    monkeypatch.setattr(acr, "ccusage_by_model", lambda day: {"claude-opus-4-8": 5.0})
    monkeypatch.setattr(acr, "gateway_by_model", lambda day: {})
    monkeypatch.setattr(acr, "apps_by_app", lambda day: {})
    (tmp_path / "cost-report").mkdir(parents=True)
    (tmp_path / "cost-report" / "latest.json").write_text('{"sentinel":true}')
    rc = acr.backfill_history(3, "2026-06-15T12:00:00")
    assert rc == 0
    rows = [json.loads(ln) for ln in (tmp_path / "cost-report" / "history.jsonl").read_text().splitlines() if ln.strip()]
    assert len(rows) == 3
    assert all(r["by_model"][0]["model"] == "claude-opus-4-8" for r in rows)
    assert rows == sorted(rows, key=lambda r: r["date"])  # chronological
    assert json.loads((tmp_path / "cost-report" / "latest.json").read_text()) == {"sentinel": True}


# --- subscription-spend tripwire (operator, 2026-06-23) ---------------------------

def _capture_sends(acr, monkeypatch):
    """Replace signal_client.post_alert with a capturing stub; return the calls.

    Each entry is `(alert_key, body, meta)`. This used to capture `slack.send` and
    stub `post_alert` to keep the network out of tests; agent-platform#293 removed
    Slack, so the structured alert IS the delivery and is what a test must watch.
    The name is unchanged so the assertions below keep reading the same way.

    Accepts **kw so the stub keeps matching post_alert's real signature — it takes
    meta=, and a stub that rejects a kwarg the caller passes turns a signature
    change into a confusing TypeError instead of a clear failure."""
    calls = []
    monkeypatch.setattr(
        acr.signal_client, "post_alert",
        lambda key, body, **kw: calls.append((key, body, kw.get("meta") or {})) or "stub")
    return calls


def test_subscription_increment_no_alert_under_step(agent_cost_report, monkeypatch, tmp_path):
    acr = agent_cost_report
    monkeypatch.setattr(acr, "INCREMENT_STATE_PATH", tmp_path / "sub-alert.json")
    calls = _capture_sends(acr, monkeypatch)
    assert acr.maybe_alert_subscription_increment(date(2026, 6, 23), 42.0, step=50) is None
    assert calls == []
    assert not (tmp_path / "sub-alert.json").exists()  # no state written under threshold


def test_subscription_increment_fires_once_per_increment(agent_cost_report, monkeypatch, tmp_path):
    acr = agent_cost_report
    monkeypatch.setattr(acr, "INCREMENT_STATE_PATH", tmp_path / "sub-alert.json")
    calls = _capture_sends(acr, monkeypatch)
    day = date(2026, 6, 23)
    # First crossing of $50 alerts.
    assert acr.maybe_alert_subscription_increment(day, 53.0, step=50) == 50
    # Still in the $50–$100 band on a later refresh: no re-alert.
    assert acr.maybe_alert_subscription_increment(day, 88.0, step=50) is None
    # Crossing $100 alerts again.
    assert acr.maybe_alert_subscription_increment(day, 102.0, step=50) == 100
    assert len(calls) == 2
    assert all(key == "subscription-increment" for key, _, _ in calls)
    # The alert reports the crossed level and the live total.
    assert "$50" in calls[0][1]
    assert calls[0][2]["level"] == 50


def test_subscription_increment_multi_step_jump_alerts_once_at_ceiling(agent_cost_report, monkeypatch, tmp_path):
    acr = agent_cost_report
    monkeypatch.setattr(acr, "INCREMENT_STATE_PATH", tmp_path / "sub-alert.json")
    calls = _capture_sends(acr, monkeypatch)
    # Spend leaps straight past several increments between refreshes.
    assert acr.maybe_alert_subscription_increment(date(2026, 6, 23), 173.0, step=50) == 150
    assert len(calls) == 1  # one ping at the new ceiling, not one per skipped band


def test_subscription_increment_resets_next_day(agent_cost_report, monkeypatch, tmp_path):
    acr = agent_cost_report
    monkeypatch.setattr(acr, "INCREMENT_STATE_PATH", tmp_path / "sub-alert.json")
    calls = _capture_sends(acr, monkeypatch)
    assert acr.maybe_alert_subscription_increment(date(2026, 6, 23), 60.0, step=50) == 50
    # New day, same spend level: the per-day counter resets, so it alerts again.
    assert acr.maybe_alert_subscription_increment(date(2026, 6, 24), 60.0, step=50) == 50
    assert len(calls) == 2


def test_subscription_increment_alert_body_says_which_dollars(agent_cost_report,
                                                              monkeypatch, tmp_path):
    """Pin the alert PROSE, which is now the whole payload.

    This replaces a test that pinned `slack.subscription_increment_alert`'s block
    payload. That builder went with the slack module (agent-platform#293), and the
    thing worth pinning was never the block structure — it was that the text says
    which kind of dollars crossed, so a $1000 subscription estimate is not read as
    $1000 out of pocket."""
    acr = agent_cost_report
    monkeypatch.setattr(acr, "INCREMENT_STATE_PATH", tmp_path / "sub-alert.json")
    calls = _capture_sends(acr, monkeypatch)
    assert acr.maybe_alert_subscription_increment(date(2026, 6, 23), 112.40, step=50) == 100
    key, body, meta = calls[0]
    assert "$100" in body and "$112.40" in body
    assert "not out-of-pocket cost" in body
    assert meta["step"] == 50 and meta["spent_usd"] == 112.40


# --- structured alert emission (ARCH-353 Build 2c / M3) ----------------------


def test_subscription_increment_emits_a_structured_alert(agent_cost_report, monkeypatch, tmp_path):
    """The tripwire emits kind=alert, which is what lands it on /engineering/alerts
    rather than under /memory/reports beside the daily cost digest — _category()
    honours kind before channel. Since agent-platform#293 this is also the only
    delivery path, so the emit is the alert rather than a companion to one."""
    acr = agent_cost_report
    monkeypatch.setattr(acr, "INCREMENT_STATE_PATH", tmp_path / "sub-alert.json")
    alerts = []
    monkeypatch.setattr(acr.signal_client, "post_alert",
                        lambda stream, body, **kw: alerts.append((stream, body, kw)) or "ok")

    assert acr.maybe_alert_subscription_increment(date(2026, 6, 23), 53.0, step=50) == 50
    assert len(alerts) == 1, "the tripwire must emit exactly one structured alert"
    stream, body, kw = alerts[0]
    assert stream == "subscription-increment"
    assert "50" in body                      # the crossed increment is in the prose
    assert kw["meta"]["alert"] == "subscription-increment"
    assert kw["meta"]["level"] == 50   # level is the crossed DOLLAR threshold, not a count


# test_subscription_increment_does_not_double_capture is deliberately GONE.
#
# It asserted `slack.send(..., tee=False)`, guarding against one crossing landing
# in the signal store twice: once as the structured alert, once as a generic
# notification off the Slack tee. agent-platform#293 removed the Slack post, so
# there is exactly one writer and the second path it defended against cannot
# exist. Rewriting it to assert "one store write per crossing" would duplicate
# test_subscription_increment_emits_a_structured_alert (len(alerts) == 1) and
# test_subscription_increment_fires_once_per_increment, so it is dropped rather
# than kept as a test that restates a neighbour.


# --- Fable tripwire as a structured alert (ARCH-362 R3) -----------------------


def _fable_module(agent_cost_report, tmp_path, monkeypatch):
    """Redirect the Fable state file on the SESSION module, matching how every
    increment-alert test above redirects INCREMENT_STATE_PATH.

    An earlier version reloaded bin/agent-cost-report from source instead. That
    diverged from the established pattern for no benefit and carried an unused
    `monkeypatch` argument — dead weight in a helper is how a test file starts
    disagreeing with itself.
    """
    monkeypatch.setattr(agent_cost_report, "FABLE_STATE_PATH", tmp_path / "fable-state.json")
    return agent_cost_report


def test_fable_alert_fires_once_per_day(agent_cost_report, monkeypatch, tmp_path):
    """The warning used to live only inside the 4-hourly cost summary post —
    alert-class content with no alert path. It is now a kind=alert signal, so it
    reaches /engineering/alerts and inherits Phase 4 acknowledgment and push.

    Once per day, because Fable spend is a standing CONDITION rather than an
    event: re-alerting every four hours trains the alert to be ignored, which is
    how an alert channel becomes worthless.
    """
    import datetime as dt
    m = _fable_module(agent_cost_report, tmp_path, monkeypatch)
    sent = []
    monkeypatch.setattr(m.signal_client, "post_alert",
                        lambda stream, body, **kw: sent.append((stream, body, kw)))
    day = dt.date(2026, 7, 19)
    assert m.maybe_alert_fable(day, 4.20) is True
    assert m.maybe_alert_fable(day, 4.20) is False, "re-alerted the same day"
    assert m.maybe_alert_fable(day, 9.99) is False, "re-alerted on a higher amount"
    assert len(sent) == 1
    stream, body, kw = sent[0]
    assert stream == "fable-tripwire"
    assert "4.20" in body and "ARCH-134" in body
    assert kw["meta"]["alert"] == "fable-tripwire"


def test_fable_alert_fires_again_on_a_new_day(agent_cost_report, monkeypatch, tmp_path):
    import datetime as dt
    m = _fable_module(agent_cost_report, tmp_path, monkeypatch)
    sent = []
    monkeypatch.setattr(m.signal_client, "post_alert",
                        lambda stream, body, **kw: sent.append(stream))
    assert m.maybe_alert_fable(dt.date(2026, 7, 19), 1.0) is True
    assert m.maybe_alert_fable(dt.date(2026, 7, 20), 1.0) is True
    assert len(sent) == 2


def test_no_fable_spend_emits_nothing(agent_cost_report, monkeypatch, tmp_path):
    import datetime as dt
    m = _fable_module(agent_cost_report, tmp_path, monkeypatch)
    sent = []
    monkeypatch.setattr(m.signal_client, "post_alert",
                        lambda *a, **k: sent.append(1))
    assert m.maybe_alert_fable(dt.date(2026, 7, 19), 0.0) is False
    assert m.maybe_alert_fable(dt.date(2026, 7, 19), 0.004) is False  # rounding floor
    assert sent == []


def test_fable_state_is_its_own_file_not_shared_with_the_increment_alert(
        agent_cost_report, monkeypatch, tmp_path):
    """Sharing one state file would let either tripwire reset the other, so a
    Fable alert could silently suppress a subscription-increment alert."""
    m = _fable_module(agent_cost_report, tmp_path, monkeypatch)
    assert m.FABLE_STATE_PATH != m.INCREMENT_STATE_PATH


def test_date_override_does_not_clobber_todays_live_snapshot(agent_cost_report,
                                                             monkeypatch, tmp_path):
    """`--date` is a testing override for a PAST day; latest.json is by
    definition today. Writing a historical day into it makes the cost dashboards
    report the wrong day until the next refresh — which is exactly what one
    `--date 2026-07-18` smoke run did during the R3 change. History is still
    upserted, because recomputing a past day's row is what --date is FOR."""
    acr = agent_cost_report
    cost_dir = tmp_path / "cost-report"
    cost_dir.mkdir(parents=True)
    monkeypatch.setattr(acr, "COST_DIR", cost_dir)
    monkeypatch.setattr(acr, "ccusage_by_model", lambda day: {"claude-opus-4-8": 7.0})
    monkeypatch.setattr(acr, "gateway_by_model", lambda day: {})
    monkeypatch.setattr(acr, "apps_by_app", lambda day: {})
    monkeypatch.setattr(acr, "signal_client", type("S", (), {"post_alert": staticmethod(lambda *a, **k: None)})())
    (cost_dir / "latest.json").write_text('{"date": "TODAY-SENTINEL"}')

    monkeypatch.setattr("sys.argv", ["agent-cost-report", "--date", "2026-06-11"])
    assert acr.main() == 0

    # today's live snapshot is untouched...
    assert json.loads((cost_dir / "latest.json").read_text())["date"] == "TODAY-SENTINEL"
    # ...and the historical row WAS recomputed
    rows = [json.loads(ln) for ln in (cost_dir / "history.jsonl").read_text().splitlines() if ln.strip()]
    assert [r["date"] for r in rows] == ["2026-06-11"]


# --- two spend lanes, different dollars (operator, 2026-07-19) ----------------------
#
# The old single tripwire fired at every $50 of the ANTHROPIC ESTIMATE — usage
# the Max plan mostly already covers — and went off 17 times in 14 days. That is
# ARCH-174's rule violated by the alert itself: gate on real overage, not on a
# headline number. Now there are two lanes with very different bars:
#   out-of-pocket $5   — gateway + direct-API apps, money that actually left
#   subscription $1000 — an API-rate estimate; a runaway signal, not a bill


def _capture_alerts(acr, monkeypatch):
    """Capture the structured alert as (key, message) — the signal that reaches
    /engineering/alerts and push, and since agent-platform#293 the only one."""
    alerts = []
    monkeypatch.setattr(acr.signal_client, "post_alert",
                        lambda key, msg, **kw: alerts.append((key, msg)) or "stub")
    return alerts


def test_the_defaults_are_the_ones_the_operator_asked_for(agent_cost_report):
    acr = agent_cost_report
    assert acr.OUT_OF_POCKET_WARN_STEP_USD == 5
    assert acr.SUBSCRIPTION_WARN_STEP_USD == 1000, \
        "the $50 estimate-based step is what made this alert noise"


def test_out_of_pocket_fires_at_five_dollars(agent_cost_report, monkeypatch, tmp_path):
    acr = agent_cost_report
    monkeypatch.setattr(acr, "OUT_OF_POCKET_STATE_PATH", tmp_path / "oop.json")
    alerts = _capture_alerts(acr, monkeypatch)
    day = date(2026, 7, 17)
    # the real 2026-07-17 shape: $7.18 out-of-pocket
    assert acr.maybe_alert_out_of_pocket(day, 7.18, 0.0) == 5
    assert alerts and alerts[0][0] == "out-of-pocket-spend"
    # still in the $5-$10 band on the next refresh: silent
    assert acr.maybe_alert_out_of_pocket(day, 8.90, 0.0) is None
    # crossing $10 alerts again
    assert acr.maybe_alert_out_of_pocket(day, 10.40, 0.0) == 10


def test_out_of_pocket_sums_gateway_and_apps(agent_cost_report, monkeypatch, tmp_path):
    """Both are real money; neither alone would have crossed."""
    acr = agent_cost_report
    monkeypatch.setattr(acr, "OUT_OF_POCKET_STATE_PATH", tmp_path / "oop.json")
    _capture_alerts(acr, monkeypatch)
    assert acr.maybe_alert_out_of_pocket(date(2026, 7, 17), 3.0, 2.5) == 5


def test_a_typical_day_of_subscription_spend_is_silent(agent_cost_report, monkeypatch, tmp_path):
    """The whole point of moving $50 to $1000. 2026-07-11 was $560.85 on the
    estimate with under a dollar out-of-pocket — an ordinary day that used to
    produce eleven alerts."""
    acr = agent_cost_report
    monkeypatch.setattr(acr, "INCREMENT_STATE_PATH", tmp_path / "sub.json")
    monkeypatch.setattr(acr, "OUT_OF_POCKET_STATE_PATH", tmp_path / "oop.json")
    alerts = _capture_alerts(acr, monkeypatch)
    day = date(2026, 7, 11)
    assert acr.maybe_alert_subscription_increment(day, 560.85) is None
    assert acr.maybe_alert_out_of_pocket(day, 0.94, 0.0) is None
    assert alerts == []


def test_the_lanes_keep_independent_state(agent_cost_report, monkeypatch, tmp_path):
    """Each lane has its own state file, so one firing cannot silence the other."""
    acr = agent_cost_report
    monkeypatch.setattr(acr, "INCREMENT_STATE_PATH", tmp_path / "sub.json")
    monkeypatch.setattr(acr, "OUT_OF_POCKET_STATE_PATH", tmp_path / "oop.json")
    _capture_alerts(acr, monkeypatch)
    day = date(2026, 7, 17)
    assert acr.maybe_alert_out_of_pocket(day, 7.18, 0.0) == 5
    assert acr.maybe_alert_subscription_increment(day, 1082.53) == 1000
    assert (tmp_path / "sub.json").exists() and (tmp_path / "oop.json").exists()


def test_the_message_names_the_level_actually_crossed(agent_cost_report,
                                                      monkeypatch, tmp_path):
    """A near-miss worth pinning. The first draft built the message BEFORE the
    crossed level was known, so it interpolated the STEP — an alert reading
    "crossed $1000" on a day that actually crossed $2000."""
    acr = agent_cost_report
    monkeypatch.setattr(acr, "INCREMENT_STATE_PATH", tmp_path / "sub.json")
    alerts = _capture_alerts(acr, monkeypatch)
    assert acr.maybe_alert_subscription_increment(date(2026, 7, 12), 2150.0) == 2000
    assert "$2,000" in alerts[0][1], alerts[0][1]


def test_both_alerts_say_which_kind_of_dollars(agent_cost_report, monkeypatch, tmp_path):
    """The two lanes are only useful if the reader can tell them apart at a
    glance — conflating them is what made the old alert meaningless."""
    acr = agent_cost_report
    monkeypatch.setattr(acr, "INCREMENT_STATE_PATH", tmp_path / "sub.json")
    monkeypatch.setattr(acr, "OUT_OF_POCKET_STATE_PATH", tmp_path / "oop.json")
    alerts = _capture_alerts(acr, monkeypatch)
    day = date(2026, 7, 17)
    acr.maybe_alert_out_of_pocket(day, 7.18, 0.0)
    acr.maybe_alert_subscription_increment(day, 1082.53)
    by_key = dict(alerts)
    assert "real money" in by_key["out-of-pocket-spend"]
    assert "estimate" in by_key["subscription-increment"]


def test_out_of_pocket_message_names_the_level_too(agent_cost_report, monkeypatch, tmp_path):
    """The subscription lane had this pinned and the out-of-pocket lane did not —
    and asymmetric coverage is exactly how the step-instead-of-level bug reached
    review in the first place. Both lanes now assert the same property."""
    acr = agent_cost_report
    monkeypatch.setattr(acr, "OUT_OF_POCKET_STATE_PATH", tmp_path / "oop.json")
    alerts = _capture_alerts(acr, monkeypatch)
    assert acr.maybe_alert_out_of_pocket(date(2026, 7, 17), 12.50, 0.0) == 10
    msg = alerts[0][1]
    assert "$10" in msg, msg
    assert "crossed $5" not in msg, "named the step instead of the level"


# --- the day's subscription-quota rollup (ARCH-442) -------------------------
#
# Every dollar figure in this report is an API-RATE ESTIMATE against
# subscription usage that is mostly inside the plan, so none of them answers
# "did we actually pay". These fields do, and ARCH-174 gates a model squeeze on
# exactly that distinction.

def _outcome(runs, day, name, **fields):
    d = runs / day / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "outcome.json").write_text(json.dumps({"exit_status": "completed", **fields}))
    return d


def test_overage_rollup_counts_the_runs_that_hit_it(agent_cost_report, monkeypatch,
                                                    tmp_path):
    acr = agent_cost_report
    runs = tmp_path / "agent-runs"
    monkeypatch.setattr(acr, "RUNS_ROOT", runs)
    day = "2026-07-28"
    _outcome(runs, day, "run-a", overage_seen=True, rate_limit_status="rejected")
    _outcome(runs, day, "run-b", overage_seen=False, rate_limit_status="allowed",
             peak_utilization=0.42)
    _outcome(runs, day, "run-c", overage_seen=False,
             rate_limit_status="allowed_warning", peak_utilization=0.96)
    got = acr.overage_rollup(date(2026, 7, 28))
    assert got["runs_reporting"] == 3
    assert got["runs_in_overage"] == 1
    assert got["peak_utilization"] == 0.96          # max across the day
    assert got["worst_status"] == "rejected"        # worst, not last


def test_overage_rollup_is_empty_when_nothing_reported(agent_cost_report,
                                                       monkeypatch, tmp_path):
    """A day with no runs and a day whose runs never emitted the event are the
    same to us, and NEITHER is evidence of being under the cap. Returning {} is
    what lets the panel say "not reported" instead of a reassuring zero."""
    acr = agent_cost_report
    runs = tmp_path / "agent-runs"
    monkeypatch.setattr(acr, "RUNS_ROOT", runs)
    _outcome(runs, "2026-07-28", "run-old")          # a pre-ARCH-442 outcome
    assert acr.overage_rollup(date(2026, 7, 28)) == {}


def test_overage_rollup_ignores_a_run_that_did_not_report(agent_cost_report,
                                                          monkeypatch, tmp_path):
    """`runs_reporting` must count only runs that actually carried the field, or
    the in-overage fraction is diluted by runs that never had an opinion."""
    acr = agent_cost_report
    runs = tmp_path / "agent-runs"
    monkeypatch.setattr(acr, "RUNS_ROOT", runs)
    day = "2026-07-28"
    _outcome(runs, day, "run-a", overage_seen=True)
    _outcome(runs, day, "run-silent")
    got = acr.overage_rollup(date(2026, 7, 28))
    assert got["runs_reporting"] == 1 and got["runs_in_overage"] == 1


def test_overage_rollup_survives_an_unreadable_outcome(agent_cost_report,
                                                       monkeypatch, tmp_path):
    acr = agent_cost_report
    runs = tmp_path / "agent-runs"
    monkeypatch.setattr(acr, "RUNS_ROOT", runs)
    day = "2026-07-28"
    _outcome(runs, day, "run-a", overage_seen=True)
    bad = runs / day / "run-bad"
    bad.mkdir(parents=True)
    (bad / "outcome.json").write_text("{not json")
    assert acr.overage_rollup(date(2026, 7, 28))["runs_reporting"] == 1


def test_the_severity_order_matches_telemetry(agent_cost_report):
    """This script does not import the platform libs, so it carries its own copy
    of the ordering. Two copies of a comparison is how one comes to rank
    `rejected` below `allowed`, silently."""
    import telemetry as T
    assert agent_cost_report.RATE_LIMIT_SEVERITY == T.RATE_LIMIT_SEVERITY


def test_resolve_npx_prefers_safe_which(agent_cost_report, monkeypatch):
    """`safe_which` resolves `npx` from a fixed list of system directories
    (platform/decisions/0090); `_resolve_npx` returns exactly what it finds."""
    acr = agent_cost_report
    monkeypatch.setattr("safe_path.safe_which", lambda name: "/opt/homebrew/bin/npx")
    assert acr._resolve_npx() == "/opt/homebrew/bin/npx"


def test_resolve_npx_raises_when_nothing_is_found(agent_cost_report, monkeypatch):
    acr = agent_cost_report
    monkeypatch.setattr("safe_path.safe_which", lambda name: None)
    with pytest.raises(SystemExit):
        acr._resolve_npx()


def test_resolve_npx_a_planted_npx_under_a_fake_home_is_not_chosen(agent_cost_report, monkeypatch, tmp_path):
    """The real end-to-end exercise of `safe_which`'s plant protection, with
    no mock: a planted `npx` under a fake HOME, ahead on PATH, must not be
    what `_resolve_npx` returns — `safe_which` never reads PATH or HOME, so
    the real npx on this machine must be the one resolved regardless
    (platform/decisions/0090's Handoff). No literal path: this machine's
    real answer is computed before HOME/PATH are poisoned, not hardcoded —
    the literal `/opt/homebrew/bin/npx` this test used before round 13 is
    wrong on the GitHub Actions macOS runner, which puts `npx` somewhere
    else."""
    import safe_path as SP
    acr = agent_cost_report
    baseline = SP.safe_which("npx")
    if baseline is None:
        # ON CI A SKIP IS GREEN, which would pass the gate with the planted-npx
        # regression unchecked. The CI check matches tests/test_knowledge_ctx.py's.
        if os.environ.get("CI", "").strip().lower() not in ("", "0", "false"):
            pytest.fail("npx is not on the fixed system list under CI, so the "
                        "planted-npx regression went unchecked")
        pytest.skip("no real npx on this machine's fixed system list")

    fake_home = tmp_path / "fake-home"
    planted_dir = fake_home / ".local" / "bin"
    planted_dir.mkdir(parents=True)
    (planted_dir / "npx").write_text("#!/bin/sh\n")
    (planted_dir / "npx").chmod(0o755)

    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("PATH", str(planted_dir))
    got = acr._resolve_npx()
    assert got is not None and not got.startswith(str(fake_home)), \
        f"the planted npx under the fake HOME was chosen: {got}"
    assert got == baseline
