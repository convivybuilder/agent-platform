"""Tests for bin/metric-alerts — threshold alerts on cost/quality + code-health.

The live data is usually healthy (nothing fires), so these synthetic cases are
the only proof the thresholds + edge-trigger actually fire — and don't over-fire.
The Slack/IO is excluded; these pin the pure evaluation.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

NOW = datetime(2026, 6, 10, tzinfo=timezone.utc)


# --- code_health_alerts ------------------------------------------------------


def _repo(name="vivy", **kw):
    # ci_*_main are the alert basis (ARCH-179); ci_* (all-branch) are dashboard-only.
    base = {"repo": name, "ci_last": "success", "ci_pass_rate": 0.95,
            "ci_last_main": "success", "ci_pass_rate_main": 0.95,
            "reverts_recent": 0, "oldest_open_days": 2}
    base.update(kw)
    return base


def test_healthy_repo_no_alerts(metric_alerts):
    assert metric_alerts.code_health_alerts([_repo()]) == []


def test_ci_failure_alerts(metric_alerts):
    alerts = metric_alerts.code_health_alerts([_repo(ci_last_main="failure")])
    assert any(a["key"] == "ci-red:vivy" and a["severity"] == "high" for a in alerts)


def test_low_pass_rate_alerts(metric_alerts):
    alerts = metric_alerts.code_health_alerts([_repo(ci_pass_rate_main=0.5)])
    assert any(a["key"] == "ci-low:vivy" for a in alerts)
    # at/above the floor → no alert
    assert metric_alerts.code_health_alerts([_repo(ci_pass_rate_main=0.70)]) == []


def test_pr_branch_noise_does_not_alert(metric_alerts):
    """ARCH-179: the false-positive this fix targets. A busy night of agent PRs
    drags the all-branch rate down (ci_pass_rate=0.5) and the most recent
    all-branch run may be a PR-branch failure (ci_last="failure"), but main is
    green — no code-health alert should fire."""
    r = _repo(ci_pass_rate=0.5, ci_last="failure",
              ci_pass_rate_main=0.95, ci_last_main="success")
    assert metric_alerts.code_health_alerts([r]) == []


def test_reverts_and_stale_pr_alert(metric_alerts):
    alerts = metric_alerts.code_health_alerts([_repo(reverts_recent=2, oldest_open_days=20)])
    keys = {a["key"] for a in alerts}
    assert "reverts:vivy" in keys and "stale-pr:vivy" in keys


def test_none_metrics_dont_alert(metric_alerts):
    # a repo whose gh calls failed (None metrics) must not throw or false-alert.
    # Includes a repo with no push-to-main runs (ci_*_main None) → CI alerts silent.
    assert metric_alerts.code_health_alerts([
        _repo(ci_pass_rate=None, ci_pass_rate_main=None, reverts_recent=None,
              oldest_open_days=None, ci_last=None, ci_last_main=None)]) == []


# --- cost_quality_alerts -----------------------------------------------------


def _cq(agent, model, date="2026-06-08", **kw):
    base = {"agent": agent, "model": model, "date": date, "pr_state": "MERGED",
            "merged": True, "survived": None, "cost_usd": 0.01}
    base.update(kw)
    return base


def test_survival_floor_breach(metric_alerts):
    # 4 scored, 2 survived = 50% < 70% floor → alert
    rows = [_cq("bug-fixer", "deepseek/deepseek-v4-flash", survived=(i < 2)) for i in range(4)]
    alerts = metric_alerts.cost_quality_alerts(rows, NOW)
    assert any(a["key"] == "survival:bug-fixer:deepseek/deepseek-v4-flash" for a in alerts)


def test_survival_below_noise_floor_does_not_alert(metric_alerts):
    # only 2 scored (< SURVIVAL_MIN_N) → too few to judge, no alert despite 0% survived
    rows = [_cq("bug-fixer", "deepseek/deepseek-v4-flash", survived=False) for _ in range(2)]
    assert not any(a["key"].startswith("survival:") for a in metric_alerts.cost_quality_alerts(rows, NOW))


def test_model_gate_band_breach(metric_alerts):
    # premium (sonnet, pricier) merges 100%; cheaper (deepseek) merges 80% → >5pts
    # below. Both sides > GATE_MIN_N (25 resolved) so the gate is well-sampled.
    rows = []
    rows += [_cq("bug-fixer", "claude-sonnet-4-6", cost_usd=1.0) for _ in range(25)]  # 25/25
    rows += [_cq("bug-fixer", "deepseek/deepseek-v4-flash", cost_usd=0.01,
                 merged=(i < 20), pr_state="MERGED" if i < 20 else "CLOSED") for i in range(25)]  # 20/25
    alerts = metric_alerts.cost_quality_alerts(rows, NOW)
    gate = next((a for a in alerts if a["key"] == "gate:bug-fixer:deepseek/deepseek-v4-flash"), None)
    assert gate is not None
    # "cheaper" is quantified with observed $/merged, not asserted (operator, 2026-07-06):
    # deepseek 25*$0.01/20 = $0.01/merged; sonnet 25*$1.00/25 = $1.00/merged.
    assert "$0.01/merged" in gate["text"] and "$1.00/merged" in gate["text"]


def test_model_gate_within_band_no_alert(metric_alerts):
    rows = []
    rows += [_cq("bug-fixer", "claude-sonnet-4-6", cost_usd=1.0) for _ in range(25)]
    rows += [_cq("bug-fixer", "deepseek/deepseek-v4-flash", cost_usd=0.01) for _ in range(25)]  # both 100%
    assert not any(a["key"].startswith("gate:") for a in metric_alerts.cost_quality_alerts(rows, NOW))


def test_model_gate_ignores_tiny_n_baseline(metric_alerts):
    """The 2026-07-06 report: coder's cheaper model at 94% (187/199) versus a
    RETIRED premium frozen at 3/3 = 100%. The tiny-N baseline must drop out of the
    comparison — otherwise it anchors a gap that can never close and the alert
    re-fires daily as noise."""
    rows = []
    rows += [_cq("coder", "claude-opus-4-7", cost_usd=5.0) for _ in range(3)]  # 3/3, retired premium
    rows += [_cq("coder", "claude-sonnet-4-6", cost_usd=0.5,
                 merged=(i < 187), pr_state="MERGED" if i < 187 else "CLOSED")
             for i in range(199)]  # 187/199 = 94%
    keys = {a["key"] for a in metric_alerts.cost_quality_alerts(
        rows, NOW, {"coder": "claude-sonnet-4-6"})}
    assert "gate:coder:claude-sonnet-4-6" not in keys  # baseline below floor → no gate


def test_model_gate_ignores_tiny_n_cheaper(metric_alerts):
    """The floor is symmetric: a cheaper model with too few resolved PRs can't trip
    the gate even when its rate looks terrible — 0/3 is not evidence it lags."""
    rows = []
    rows += [_cq("bug-fixer", "claude-sonnet-4-6", cost_usd=1.0) for _ in range(25)]  # 25/25
    rows += [_cq("bug-fixer", "deepseek/deepseek-v4-flash", cost_usd=0.01,
                 merged=False, pr_state="CLOSED") for _ in range(3)]  # 0/3 — too few
    assert not any(a["key"].startswith("gate:") for a in metric_alerts.cost_quality_alerts(rows, NOW))


def test_model_gate_picks_well_sampled_baseline(metric_alerts):
    """With a tiny-N priciest model present, the baseline is the priciest model that
    is itself well-sampled — not the frozen tiny one."""
    rows = []
    rows += [_cq("coder", "claude-opus-4-7", cost_usd=9.0) for _ in range(3)]     # priciest but N=3 → excluded
    rows += [_cq("coder", "claude-opus-4-8", cost_usd=3.0) for _ in range(25)]    # 25/25, well-sampled premium
    rows += [_cq("coder", "claude-sonnet-4-6", cost_usd=0.5,
                 merged=(i < 20), pr_state="MERGED" if i < 20 else "CLOSED") for i in range(25)]  # 20/25 = 80%
    alerts = metric_alerts.cost_quality_alerts(rows, NOW, {"coder": "claude-sonnet-4-6"})
    gate = next((a for a in alerts if a["key"] == "gate:coder:claude-sonnet-4-6"), None)
    assert gate is not None
    assert "claude-opus-4-8" in gate["text"]      # baseline is the well-sampled premium
    assert "claude-opus-4-7" not in gate["text"]  # not the tiny-N frozen model


def test_cq_window_excludes_old_rows(metric_alerts):
    # a 50%-survival cell, but all rows are outside the 7d window → not aggregated
    rows = [_cq("bug-fixer", "x", date="2026-05-01", survived=(i < 2)) for i in range(4)]
    assert metric_alerts.cost_quality_alerts(rows, NOW) == []


# --- switch-aware model gating (ARCH-173) ------------------------------------


def _refactorer_mid_switch():
    """A 7d window straddling a deepseek → kimi-code switch: sonnet baseline at
    100%, both cheap models underperforming at 40% (10/25 merged). kimi is priced
    below sonnet so sonnet stays the premium baseline. All three sides clear the
    > GATE_MIN_N sample floor (25 resolved each)."""
    rows = [_cq("refactorer", "claude-sonnet-4-6", cost_usd=1.0) for _ in range(25)]  # 25/25 = 100%
    for model, usd in (("deepseek/deepseek-v4-flash", 0.01), ("moonshot/kimi-k2.7-code", 0.05)):
        rows += [_cq("refactorer", model, cost_usd=usd,
                     merged=(i < 10), pr_state="MERGED" if i < 10 else "CLOSED") for i in range(25)]  # 10/25 = 40%
    return rows


def test_model_gate_switch_aware_suppresses_retired_model(metric_alerts):
    # refactorer is configured to kimi-code now; deepseek is the retired model
    # still in the window. Only the current model gates; the retired one is silent.
    keys = {a["key"] for a in metric_alerts.cost_quality_alerts(
        _refactorer_mid_switch(), NOW, {"refactorer": "moonshot/kimi-k2.7-code"})}
    assert "gate:refactorer:moonshot/kimi-k2.7-code" in keys   # current model → gated
    assert "gate:refactorer:deepseek/deepseek-v4-flash" not in keys  # retired model → suppressed


def test_model_gate_no_configured_gates_all(metric_alerts):
    # configured=None (the default) preserves pre-ARCH-173 behavior: every priced
    # underperformer gates, retired or not — fail-open, never hides a breach.
    keys = {a["key"] for a in metric_alerts.cost_quality_alerts(_refactorer_mid_switch(), NOW)}
    assert "gate:refactorer:moonshot/kimi-k2.7-code" in keys
    assert "gate:refactorer:deepseek/deepseek-v4-flash" in keys


def test_configured_models_reads_frontmatter(metric_alerts, tmp_path):
    (tmp_path / "refactorer.md").write_text("---\nmodel: kimi-code\ntools: []\n---\nbody\n")
    (tmp_path / "coder.md").write_text('---\nmodel: "deepseek"\n---\nbody\n')  # quoted scalar → unwrapped
    (tmp_path / "explainer.md").write_text("---\nid: x\ntools: []\n---\nbody\n")  # no model: → omitted
    assert metric_alerts.configured_models(tmp_path) == {
        "refactorer": "moonshot/kimi-k2.7-code", "coder": "deepseek/deepseek-v4-flash"}


def test_configured_models_missing_dir_is_empty(metric_alerts, tmp_path):
    assert metric_alerts.configured_models(tmp_path / "does-not-exist") == {}


@pytest.mark.needs_kb
def test_configured_models_reads_the_live_roster(metric_alerts):
    """AGENTS_DIR must name the directory the roster actually occupies.

    The two tests above pass against any directory, which is why neither noticed
    when the KB re-file (ARCH-426 step 5) left AGENTS_DIR pointing at an emptied
    `projects/shared/agents/`. `configured_models` returns {} for an empty
    directory BY DESIGN — fail-open — so every model gate silently reverted to
    the pre-ARCH-173 behaviour the switch-aware gating exists to replace, and
    nothing reported the fallback.

    Asserting a non-empty result is the whole check: a KB path this module cannot
    resolve is indistinguishable from "no agent declares a model", and only a real
    checkout can tell the two apart.
    """
    assert metric_alerts.AGENTS_DIR.is_dir(), (
        f"AGENTS_DIR does not exist: {metric_alerts.AGENTS_DIR}")
    models = metric_alerts.configured_models()
    assert models, (
        f"{metric_alerts.AGENTS_DIR} yielded no agent models. Either the roster "
        f"has moved again or no agent declares `model:` — both silently ungate "
        f"every cost-quality model gate.")


# --- kb_eval_alerts (retrieval regression, ARCH-149) -------------------------


def _kb(recall10=0.98, mrr=0.81, k=10):
    return {"k": k, "overall": {f"recall@{k}": recall10, "mrr": mrr}}


def test_kb_eval_healthy_no_alert(metric_alerts):
    assert metric_alerts.kb_eval_alerts(_kb()) == []


def test_kb_eval_recall_floor_breach(metric_alerts):
    alerts = metric_alerts.kb_eval_alerts(_kb(recall10=0.60))
    assert any(a["key"] == "kb-recall" and a["severity"] == "high" for a in alerts)


def test_kb_eval_mrr_floor_breach(metric_alerts):
    alerts = metric_alerts.kb_eval_alerts(_kb(mrr=0.40))
    assert any(a["key"] == "kb-mrr" and a["severity"] == "warn" for a in alerts)


def test_kb_eval_respects_actual_k(metric_alerts):
    # reads recall@k for the summary's own k (here k=5), not a hardcoded recall@10
    alerts = metric_alerts.kb_eval_alerts({"k": 5, "overall": {"recall@5": 0.60, "mrr": 0.9}})
    assert any(a["key"] == "kb-recall" for a in alerts)
    # k=10 but only recall@5 present → no recall@10 to read → no recall alert (not a crash)
    assert not any(a["key"] == "kb-recall" for a in
                   metric_alerts.kb_eval_alerts({"k": 10, "overall": {"recall@5": 0.60, "mrr": 0.9}}))


def test_kb_eval_missing_summary_silent(metric_alerts):
    # no eval yet (missing/empty file) must not throw or false-alert
    assert metric_alerts.kb_eval_alerts({}) == []
    assert metric_alerts.kb_eval_alerts({"overall": {}}) == []


# --- edge_trigger (de-dup: alert-on-change, not every day) -------------------


def test_edge_trigger_new_sustained_recovered(metric_alerts):
    a1 = [{"key": "ci-red:vivy", "text": "x"}, {"key": "reverts:vivy", "text": "y"}]
    # first run: both are new
    new, recovered, active = metric_alerts.edge_trigger(a1, active=[])
    assert {a["key"] for a in new} == {"ci-red:vivy", "reverts:vivy"} and recovered == []
    assert set(active) == {"ci-red:vivy", "reverts:vivy"}

    # second run: same breaches, already active → nothing new, nothing recovered
    new2, recovered2, active2 = metric_alerts.edge_trigger(a1, active=active)
    assert new2 == [] and recovered2 == []

    # third run: ci-red cleared, reverts persists → ci-red recovers, nothing new
    new3, recovered3, active3 = metric_alerts.edge_trigger(
        [{"key": "reverts:vivy", "text": "y"}], active=active2)
    assert new3 == [] and recovered3 == ["ci-red:vivy"] and active3 == ["reverts:vivy"]


def test_channel_routing(metric_alerts):
    assert metric_alerts._channel_of("ci-red:vivy") == metric_alerts.CH_HEALTH
    assert metric_alerts._channel_of("stale-pr:knowledge") == metric_alerts.CH_HEALTH
    assert metric_alerts._channel_of("survival:bug-fixer:x") == metric_alerts.CH_COST
    assert metric_alerts._channel_of("gate:bug-fixer:x") == metric_alerts.CH_COST
    # KB retrieval breaches ride the cost/perf channel (ARCH-149)
    assert metric_alerts._channel_of("kb-recall") == metric_alerts.CH_COST
    assert metric_alerts._channel_of("kb-mrr") == metric_alerts.CH_COST
    # model-override alerts ride the cost/perf channel (ARCH-188)
    assert metric_alerts._channel_of("model-override:interactive") == metric_alerts.CH_COST


# --- model_override_alerts (ARCH-188) ----------------------------------------


def _ovr_log(tmp_path, rows):
    import json
    from datetime import timedelta
    p = tmp_path / "model-overrides.jsonl"
    lines = []
    for row in rows:
        hours_ago, model, origin, *rest = row
        reason = rest[0] if rest else None
        ts = (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
        record = {"ts": ts, "agent": "reviewer", "repo": "vivy",
                  "model": model, "origin": origin, "run_id": "run-x"}
        if reason is not None:
            record["reason"] = reason
        lines.append(json.dumps(record))
    p.write_text("\n".join(lines) + "\n")
    return p


def test_model_override_under_budget_no_alert(metric_alerts, tmp_path):
    log = _ovr_log(tmp_path, [(1, "opus", "interactive")] * 3)  # exactly 3 = budget
    assert metric_alerts.model_override_alerts(NOW, log) == []


def test_model_override_over_budget_alerts(metric_alerts, tmp_path):
    log = _ovr_log(tmp_path, [(1, "opus", "interactive")] * 4)  # 4 > 3
    alerts = metric_alerts.model_override_alerts(NOW, log)
    assert len(alerts) == 1 and alerts[0]["key"] == "model-override:interactive"
    assert "4 interactive" in alerts[0]["text"]


def test_model_override_excludes_old_and_scheduled(metric_alerts, tmp_path):
    log = _ovr_log(tmp_path, [
        (1, "opus", "interactive"), (2, "opus", "interactive"),   # 2 in-window interactive
        (30, "opus", "interactive"),                              # outside 24h
        (1, "opus", "scheduled"), (1, "opus", "scheduled"),       # scheduled = deliberate config
    ])
    assert metric_alerts.model_override_alerts(NOW, log) == []   # only 2 count → no alert


def test_model_override_missing_log_silent(metric_alerts, tmp_path):
    assert metric_alerts.model_override_alerts(NOW, tmp_path / "nope.jsonl") == []


def test_model_override_alert_quotes_reasons(metric_alerts, tmp_path):
    # Model-lane epic (issue_01KZH2GYGTVHFXZ055GEZVH2ZJ): the alert quotes each
    # override's reason rather than only the model breakdown, so "why" doesn't
    # need timestamp-matching to reconstruct.
    log = _ovr_log(tmp_path, [
        (1, "opus", "interactive", "needs deeper reasoning"),
        (1, "opus", "interactive", "needs deeper reasoning"),
        (1, "opus", "interactive", "needs deeper reasoning"),
        (1, "opus", "interactive", "second opinion on a tricky diff"),
    ])
    alerts = metric_alerts.model_override_alerts(NOW, log)
    assert len(alerts) == 1
    assert '"needs deeper reasoning"' in alerts[0]["text"]
    assert '"second opinion on a tricky diff"' in alerts[0]["text"]


def test_model_override_alert_handles_missing_reason(metric_alerts, tmp_path):
    # A row written before the `reason` field existed (or that lost it on a
    # best-effort log write) still counts toward the alert and quotes plainly
    # rather than crashing on a missing key.
    log = _ovr_log(tmp_path, [(1, "opus", "interactive")] * 4)
    alerts = metric_alerts.model_override_alerts(NOW, log)
    assert len(alerts) == 1
    assert "no reason recorded" in alerts[0]["text"]


def test_model_override_malformed_ts_not_counted(metric_alerts, tmp_path):
    # Rows with a missing/unparseable ts can't be confirmed in-window → dropped,
    # never counted (else the alert fires early). 3 good + 2 bad-ts → no alert.
    import json
    from datetime import timedelta
    good = (NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    p = tmp_path / "model-overrides.jsonl"
    rows = [{"ts": good, "model": "opus", "origin": "interactive"}] * 3
    rows += [{"model": "opus", "origin": "interactive"},                # no ts
             {"ts": "not-a-date", "model": "opus", "origin": "interactive"}]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    assert metric_alerts.model_override_alerts(NOW, p) == []


# --- structured alert emission (ARCH-353 Build 2c / M3) ----------------------
#
# These two channels already map to "alert" in orient's CHANNEL_CATEGORY, so
# emitting kind=alert is durability rather than a placement fix: it stops the
# Alerts page depending on a hardcoded Slack channel id, which is what lets it
# keep working after the Phase 5 Slack retirement. Review of PR #151 flagged
# that this site had no test while the increment tripwire had two.


def test_post_emits_a_structured_alert(metric_alerts, monkeypatch):
    ma = metric_alerts
    alerts = []
    monkeypatch.setattr(ma.signal_client, "post_alert",
                        lambda stream, body, **kw: alerts.append((stream, body, kw)) or "ok")

    ma._post(ma.CH_HEALTH, "*Code-health alerts*",
             [{"text": "vivy: coverage fell below 70%"}], ["constance: ci-red"])

    assert len(alerts) == 1, "_post must emit exactly one structured alert"
    stream, body, kw = alerts[0]
    assert stream == ma.CH_HEALTH
    assert "coverage fell below 70%" in body          # the finding is in the prose
    assert "recovered: constance: ci-red" in body     # recoveries too
    assert kw["meta"]["new"] == ["vivy: coverage fell below 70%"]
    assert kw["meta"]["recovered"] == ["constance: ci-red"]
