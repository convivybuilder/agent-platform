"""Pure join + aggregation logic for bin/model-scorecard (ARCH-107, Phase 1).

The risk this guards: ledger membership is decided by pr-link.json presence
alone (is_ledger_member) — not agent name, not a PR number or branch
reconstructed from the task text. A prior version gated on a hardcoded
PRODUCER_AGENTS allowlist, which both silently dropped newly named producer
agents (orient-coder, sel-coder, deck-coder, ...) and, when a coder widened it
to a task-text/branch heuristic instead, flooded the ledger with reviewer-
family runs that merely cite a PR number in their brief (measured: 88% of
reviewer-family runs in a real window do). The gh-calling IO is excluded —
these pin the pure logic.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _ledger_refusal_marker_home(model_scorecard, tmp_path, monkeypatch):
    """`report_to_ledger`'s own refusal check (`ledger.refused_figure_
    unchanged`) resolves `home` through `run_sandbox.passwd_home()` when not
    given explicitly — redirected here to this test's own `tmp_path` so no
    test in this file touches this machine's own
    `~/.local/state/agent-platform/ledger-refusals` (round 2, PR #1021
    round 1 finding)."""
    monkeypatch.setattr(model_scorecard.LG.RS, "passwd_home", lambda: tmp_path)


# --- is_close_loop -------------------------------------------------------------


def test_is_close_loop_true_for_close_loop_task(model_scorecard):
    meta = {"agent": "bug-fixer",
            "task": "CLOSE-LOOP MODE only — no code. Bug fixed in https://github.com/convivy/widget/pull/436"}
    assert model_scorecard.is_close_loop(meta) is True  # produces no PR even though it cites one


def test_is_close_loop_false_for_fix_mode(model_scorecard):
    assert model_scorecard.is_close_loop({"agent": "bug-fixer", "task": "Fix GH issue #428"}) is False
    assert model_scorecard.is_close_loop({"agent": "bug-fixer", "task": ""}) is False


# --- is_producer_shaped (skip-report bucketing only, never a ledger gate) ----


def test_is_producer_shaped_excludes_reviewer_family(model_scorecard):
    f = model_scorecard.is_producer_shaped
    assert f({"agent": "reviewer"}) is False
    assert f({"agent": "security-reviewer"}) is False
    assert f({"agent": "sel-reviewer"}) is False
    assert f({"agent": "sel-security-reviewer"}) is False


def test_is_producer_shaped_includes_any_non_reviewer_agent(model_scorecard):
    # generalizes past a hardcoded set — a never-enumerated `<product>-coder`
    # name still counts, which is the whole point of dropping PRODUCER_AGENTS
    f = model_scorecard.is_producer_shaped
    assert f({"agent": "coder"}) is True
    assert f({"agent": "orient-coder"}) is True
    assert f({"agent": "sel-coder"}) is True
    assert f({"agent": "deck-coder"}) is True
    assert f({"agent": "bug-fixer"}) is True
    assert f({"agent": "refactorer"}) is True


# --- is_ledger_member (the whole membership decision) -------------------------


# A real reviewer run (agent-runs/2026-08-24/run-0b35e027) whose task cites
# `PR #53` — the exact shape that flooded the ledger under the task-text/
# branch reconstruction this replaces. Trimmed to the fields is_ledger_member
# reads; the point is that this fixture, unlike a synthetic one, is guaranteed
# to carry a real `#N` reference.
_REAL_REVIEWER_META = {
    "run_id": "run-0b35e027",
    "agent": "reviewer",
    "task": "Review library PR #53, head 5d9a76b: `GET /ledger-entries`, the read side "
            "over the ledger store's spend entries. ... Post with `agentctl review-post "
            "--pr 53 --verdict <APPROVE|REQUEST_CHANGES> --agent <your agent name> "
            "--body-file <path>`.",
    "repo": "library",
    "worktree": "$HOME/Convivy/worktrees/"
                "library.reviewer-pr53-review-library-pr-53-head-5d9a76",
}


def test_reviewer_run_with_pr_reference_is_not_a_member(model_scorecard):
    # the regression test for the flood: a reviewer citing `#N` in its task
    # text, with no pr-link.json, must NOT qualify for the ledger
    assert model_scorecard.is_ledger_member(_REAL_REVIEWER_META, None) is False


def test_producer_shaped_agent_with_link_is_a_member_even_if_never_enumerated(model_scorecard):
    # "orient-coder" was never in the old hardcoded PRODUCER_AGENTS set, so the
    # old gate would have dropped this run outright regardless of a link;
    # the new gate cares only about the link
    meta = {"agent": "orient-coder", "task": "Fix the crawler timeout bug."}
    assert model_scorecard.is_ledger_member(meta, {"pr_number": 441}) is True
    assert model_scorecard.is_ledger_member(meta, None) is False  # no link → not a member


def test_close_loop_excluded_even_with_a_link(model_scorecard):
    # close-loop is excluded outright — unchanged from before this fix, and
    # holds even in the edge case of a stray link from an earlier cycle
    meta = {"agent": "bug-fixer", "task": "CLOSE-LOOP MODE only — no code."}
    assert model_scorecard.is_ledger_member(meta, {"pr_number": 436}) is False


# --- _validated_run_repo (round 2 security finding, PR #881) -------------------
#
# meta["repo"] is a file the dispatched run itself can write. This is the bound
# harvest() and judge_sample() call before using it in a `gh` query.


def test_validated_run_repo_refuses_a_forged_qualified_owner(model_scorecard):
    meta = {"repo": "acme-attacker/widget"}
    assert model_scorecard._validated_run_repo(meta) is None


def test_validated_run_repo_accepts_a_registered_bare_name(model_scorecard):
    meta = {"repo": "homebrew-tap"}
    assert model_scorecard._validated_run_repo(meta) == "homebrew-tap"


def test_validated_run_repo_is_none_when_absent(model_scorecard):
    assert model_scorecard._validated_run_repo({}) is None


# --- skip_counts (the coverage-gap residual) -----------------------------------


def test_skip_counts_names_producer_shaped_runs_with_no_link(model_scorecard):
    runs = [
        {"meta": {"agent": "orient-coder"}, "pr_link": None},
        {"meta": {"agent": "orient-coder"}, "pr_link": None},
        {"meta": {"agent": "coder"}, "pr_link": {"pr_number": 441}},  # linked — not skipped
        {"meta": {"agent": "reviewer"}, "pr_link": None},              # reviewer — not producer-shaped
    ]
    assert model_scorecard.skip_counts(runs) == {"orient-coder": 2}


# --- linked_pr_number (Phase 0: first-class run→PR link) ---------------------


def test_linked_pr_number_reads_recorded_link(model_scorecard):
    assert model_scorecard.linked_pr_number({"pr_number": 441}) == 441
    assert model_scorecard.linked_pr_number({"pr_number": "441"}) == 441  # tolerate str


def test_linked_pr_number_none_when_absent_or_malformed(model_scorecard):
    assert model_scorecard.linked_pr_number(None) is None
    assert model_scorecard.linked_pr_number({}) is None
    assert model_scorecard.linked_pr_number({"pr_number": None}) is None
    assert model_scorecard.linked_pr_number({"pr_number": "n/a"}) is None
    assert model_scorecard.linked_pr_number({"pr_number": True}) is None  # bool is not a PR


# --- build_row ---------------------------------------------------------------


def _run(run_id="run-1", agent="bug-fixer", model="deepseek/deepseek-v4-flash",
         usd=0.01, repo="widget", started="2026-06-02T08:00:00Z"):
    return {"run_id": run_id,
            "meta": {"agent": agent, "model": model, "repo": repo,
                     "billing_source": "provider_api:deepseek", "started_at": started},
            "cost": {"usd": usd}, "outcome": {"exit_status": "completed"}}


def test_build_row_merged_pr(model_scorecard):
    pr = {"number": 436, "state": "MERGED", "mergedAt": "2026-06-02T15:08:19Z"}
    row = model_scorecard.build_row(_run(), pr, ci_first_pass=False)
    assert row["pr_number"] == 436 and row["pr_state"] == "MERGED"
    assert row["merged"] is True and row["merged_at"] == "2026-06-02T15:08:19Z"
    assert row["ci_first_pass"] is False
    assert row["date"] == "2026-06-02" and row["cost_usd"] == 0.01
    assert row["survived"] is None  # Phase 2 fills this
    assert row["join_source"] == "linked"  # default — the only join path now
    assert row["item_id"] is None  # _run() carries no meta.issue


def test_build_row_carries_the_dispatched_work_item(model_scorecard):
    """meta.issue (E16·5) rides the row through to report_to_ledger, so the
    ledger can derive the entry's epic_id from the same item cost_join joins
    the run to."""
    run = _run()
    run["meta"]["issue"] = "issue_01M35KJZVTYG4J1R3P88P839YY"
    row = model_scorecard.build_row(run, None, ci_first_pass=None)
    assert row["item_id"] == "issue_01M35KJZVTYG4J1R3P88P839YY"


def test_build_row_respawn_flag(model_scorecard):
    """respawned mirrors ARCH-131 fallback (meta.fallback_from) — the pass^k
    reliability proxy added by the ARCH-158 brief follow-up."""
    pr = {"number": 437, "state": "MERGED", "mergedAt": "2026-06-02T15:08:19Z"}
    assert model_scorecard.build_row(_run(), pr, False)["respawned"] is False
    run = _run()
    run["meta"]["fallback_from"] = "claude-opus-4-8"  # ARCH-131 respawned this run
    assert model_scorecard.build_row(run, pr, False)["respawned"] is True


def test_build_row_unresolved(model_scorecard):
    row = model_scorecard.build_row(_run(), None, ci_first_pass=None)
    assert row["pr_state"] == "unresolved"
    assert row["merged"] is None and row["pr_number"] is None
    assert row["join_source"] == "unresolved"  # forced when no PR, even if a source was passed


def test_build_row_carries_the_raw_pr_link_for_report_to_ledgers_own_match(model_scorecard):
    """`pr_url`/`pr_number` come from the raw `pr-link.json` stamp
    (`run["pr_link"]`), not from `pr` (the gh-resolved object) — against code
    that reads them off `pr` alone, these would stay None here even though a
    real link exists, because the gh lookup (`pr`) missed (E16: `report_to_
    ledger`'s PR match needs the raw link either way)."""
    run = _run()
    run["pr_link"] = {"pr_url": "https://github.com/convivy/widget/pull/9", "pr_number": 9}
    row = model_scorecard.build_row(run, None, ci_first_pass=None)  # gh lookup missed
    assert row["pr_number"] == 9
    assert row["pr_url"] == "https://github.com/convivy/widget/pull/9"
    assert row["pr_state"] == "unresolved"  # unaffected: gh still never resolved it


# --- upsert_ledger -----------------------------------------------------------


def test_upsert_overwrites_by_run_id_and_sorts(model_scorecard):
    existing = [{"run_id": "run-a", "date": "2026-06-01", "pr_state": "OPEN", "merged": False}]
    fresh = [
        {"run_id": "run-a", "date": "2026-06-01", "pr_state": "MERGED", "merged": True},  # re-resolved
        {"run_id": "run-b", "date": "2026-06-02", "pr_state": "OPEN", "merged": False},
    ]
    out = model_scorecard.upsert_ledger(existing, fresh)
    assert [r["run_id"] for r in out] == ["run-a", "run-b"]      # sorted by date
    assert out[0]["merged"] is True                              # fresh won over stale OPEN


# --- survival_status (Phase 2) -----------------------------------------------


def _dt(model_scorecard, s):
    return model_scorecard._parse_iso(s)


def test_survival_none_until_window_elapses(model_scorecard):
    now = _dt(model_scorecard, "2026-06-10T00:00:00Z")
    # merged 3 days ago — inside the 7-day window → pending (None)
    assert model_scorecard.survival_status(True, "2026-06-07T00:00:00Z", False, now) is None
    # not merged → None regardless
    assert model_scorecard.survival_status(False, None, None, now) is None


def test_survival_true_when_window_passed_and_no_revert(model_scorecard):
    now = _dt(model_scorecard, "2026-06-10T00:00:00Z")
    # merged 9 days ago, not reverted → survived
    assert model_scorecard.survival_status(True, "2026-06-01T00:00:00Z", False, now) is True


def test_survival_false_when_reverted(model_scorecard):
    now = _dt(model_scorecard, "2026-06-10T00:00:00Z")
    assert model_scorecard.survival_status(True, "2026-06-01T00:00:00Z", True, now) is False


def test_survival_none_when_revert_state_unknown(model_scorecard):
    now = _dt(model_scorecard, "2026-06-10T00:00:00Z")
    # window passed but gh couldn't determine revert state → don't guess
    assert model_scorecard.survival_status(True, "2026-06-01T00:00:00Z", None, now) is None


# --- survival + follow-up bug (Phase 2b) -------------------------------------


def test_survival_false_when_follow_up_bug(model_scorecard):
    now = _dt(model_scorecard, "2026-06-10T00:00:00Z")
    # not reverted, but a follow-up bug landed in-window → downgraded to False
    assert model_scorecard.survival_status(
        True, "2026-06-01T00:00:00Z", False, now, follow_up=True) is False


def test_survival_follow_up_is_downgrade_only(model_scorecard):
    now = _dt(model_scorecard, "2026-06-10T00:00:00Z")
    # unknown follow-up (None) never holds up or fails a clean survivor
    assert model_scorecard.survival_status(
        True, "2026-06-01T00:00:00Z", False, now, follow_up=None) is True
    # a follow-up bug can't rescue a reverted PR (revert wins → False)
    assert model_scorecard.survival_status(
        True, "2026-06-01T00:00:00Z", True, now, follow_up=False) is False
    # default follow_up=False keeps the revert-only behavior (back-compat)
    assert model_scorecard.survival_status(
        True, "2026-06-01T00:00:00Z", False, now) is True


# --- follow_up_bug_in_window (the under-flagging guard) -----------------------


def test_follow_up_bug_true_for_bug_referencing_pr_in_window(model_scorecard):
    matches = [
        {"number": 500, "title": "Regression: formatUSD rounds wrong",
         "body": "introduced by #441", "createdAt": "2026-06-03T00:00:00Z", "labels": []},
    ]
    assert model_scorecard.follow_up_bug_in_window(matches, 441, "2026-06-01T00:00:00Z") is True


def test_follow_up_bug_true_via_bug_label(model_scorecard):
    matches = [
        {"number": 501, "title": "totals look off on dashboard", "body": "see #441",
         "createdAt": "2026-06-04T00:00:00Z", "labels": [{"name": "bug"}]},
    ]
    assert model_scorecard.follow_up_bug_in_window(matches, 441, "2026-06-01T00:00:00Z") is True


def test_follow_up_bug_excludes_the_pr_itself(model_scorecard):
    matches = [{"number": 441, "title": "Regression in #441 area", "body": "",
                "createdAt": "2026-06-02T00:00:00Z", "labels": [{"name": "bug"}]}]
    assert model_scorecard.follow_up_bug_in_window(matches, 441, "2026-06-01T00:00:00Z") is False


def test_follow_up_bug_ignores_feature_followup(model_scorecard):
    # references the PR, created in-window, but no bug signal (no keyword / label)
    matches = [{"number": 502, "title": "follow-up: add tests for #441", "body": "nice to have",
                "createdAt": "2026-06-03T00:00:00Z", "labels": [{"name": "enhancement"}]}]
    assert model_scorecard.follow_up_bug_in_window(matches, 441, "2026-06-01T00:00:00Z") is False


def test_follow_up_bug_ignores_unrelated_bug(model_scorecard):
    # a bug created in-window that does NOT reference the PR
    matches = [{"number": 503, "title": "Regression elsewhere", "body": "unrelated #999",
                "createdAt": "2026-06-03T00:00:00Z", "labels": [{"name": "bug"}]}]
    assert model_scorecard.follow_up_bug_in_window(matches, 441, "2026-06-01T00:00:00Z") is False


def test_follow_up_bug_outside_window_does_not_count(model_scorecard):
    matches = [{"number": 504, "title": "Regression from #441", "body": "",
                "createdAt": "2026-06-10T00:00:00Z", "labels": [{"name": "bug"}]}]  # ~9d later
    assert model_scorecard.follow_up_bug_in_window(matches, 441, "2026-06-01T00:00:00Z") is False


def test_references_pr_rejects_superstring_number(model_scorecard):
    # #4410 must not match a reference to PR #441
    assert model_scorecard._references_pr("see #4410", 441) is False
    assert model_scorecard._references_pr("see #441 thanks", 441) is True
    assert model_scorecard._references_pr("convivy/widget/pull/441", 441) is True


# --- revert_in_window (the false-positive guard) -----------------------------


def test_revert_excludes_the_pr_itself(model_scorecard):
    # the `Reverts #54` search returns PR #54's own thread — must NOT count as a revert
    matches = [{"number": 54, "title": "deps: bump fastapi", "mergedAt": "2026-05-20T18:34:53Z"}]
    assert model_scorecard.revert_in_window(matches, 54, "2026-05-20T18:34:53Z") is False


def test_revert_true_for_real_revert_in_window(model_scorecard):
    matches = [
        {"number": 54, "title": "deps: bump fastapi", "mergedAt": "2026-05-20T18:34:53Z"},  # self
        {"number": 61, "title": 'Revert "deps: bump fastapi"', "mergedAt": "2026-05-22T10:00:00Z"},
    ]
    assert model_scorecard.revert_in_window(matches, 54, "2026-05-20T18:34:53Z") is True


def test_revert_ignores_mention_without_revert_title(model_scorecard):
    # a later PR that merely references #54 in discussion, not a revert
    matches = [{"number": 70, "title": "follow-up to #54", "mergedAt": "2026-05-21T10:00:00Z"}]
    assert model_scorecard.revert_in_window(matches, 54, "2026-05-20T18:34:53Z") is False


def test_revert_outside_window_does_not_count(model_scorecard):
    matches = [{"number": 61, "title": 'Revert "x"', "mergedAt": "2026-06-01T00:00:00Z"}]  # ~12d later
    assert model_scorecard.revert_in_window(matches, 54, "2026-05-20T18:34:53Z") is False


# --- summarize ---------------------------------------------------------------


def test_rates_handle_zero_denominators(model_scorecard):
    r = model_scorecard._rates({"runs": 2, "resolved": 0, "merged": 0, "scored": 0,
                                "survived": 0, "ci_runs": 0, "ci_first_pass": 0, "usd": 1.0})
    assert r["merge_pct"] is None and r["surv_pct"] is None and r["per_merged"] is None
    r2 = model_scorecard._rates({"runs": 4, "resolved": 4, "merged": 2, "scored": 2,
                                 "survived": 1, "ci_runs": 4, "ci_first_pass": 3,
                                 "respawned": 1, "usd": 8.0})
    assert r2["merge_pct"] == 50.0 and r2["surv_pct"] == 50.0 and r2["per_merged"] == 4.0
    assert r2["respawn_pct"] == 25.0          # 1 of 4 runs respawned (ARCH-131)
    # an entry missing the key (older/partial callers) defaults respawned→0
    assert r["respawn_pct"] == 0.0            # runs=2, no respawned key → 0%


# --- rollup + model gate (Phase 4) -------------------------------------------


def _lrow(agent, model, date, **kw):
    base = {"agent": agent, "model": model, "date": date, "cost_usd": kw.get("usd", 0.01),
            "pr_state": "MERGED", "merged": True, "ci_first_pass": kw.get("ci", True),
            "survived": kw.get("survived")}
    return base


def test_rollup_buckets_this_vs_prior_by_date(model_scorecard):
    from datetime import datetime, timezone
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)  # this = >= 06-07, prior = 05-31..06-07
    rows = [
        _lrow("bug-fixer", "deepseek/deepseek-v4-flash", "2026-06-10"),  # this
        _lrow("bug-fixer", "deepseek/deepseek-v4-flash", "2026-06-02"),  # prior
        _lrow("bug-fixer", "deepseek/deepseek-v4-flash", "2026-05-20"),  # older — excluded
    ]
    ru = model_scorecard.rollup(rows, now, window_days=7)
    assert ru["this"][("bug-fixer", "deepseek/deepseek-v4-flash")]["runs"] == 1
    assert ru["prior"][("bug-fixer", "deepseek/deepseek-v4-flash")]["runs"] == 1


def test_model_comparison_flags_within_band(model_scorecard):
    # bug-fixer on two models; deepseek cheaper, same merge rate → within 5pts
    this = {
        ("bug-fixer", "claude-sonnet-4-6"): model_scorecard._rates(
            {"runs": 10, "resolved": 10, "merged": 10, "scored": 0, "survived": 0,
             "ci_runs": 10, "ci_first_pass": 7, "usd": 10.0}),       # $1.00/merged
        ("bug-fixer", "deepseek/deepseek-v4-flash"): model_scorecard._rates(
            {"runs": 10, "resolved": 10, "merged": 10, "scored": 0, "survived": 0,
             "ci_runs": 10, "ci_first_pass": 6, "usd": 0.2}),        # $0.02/merged
    }
    comps = model_scorecard.model_comparison(this)
    assert len(comps) == 1
    c = comps[0]
    assert c["baseline"] == "claude-sonnet-4-6"          # priciest/merged = premium
    assert c["candidate"] == "deepseek/deepseek-v4-flash"
    assert c["within_band"] is True                      # 100% vs 100% merge


def test_model_comparison_flags_below_band(model_scorecard):
    this = {
        ("bug-fixer", "claude-sonnet-4-6"): model_scorecard._rates(
            {"runs": 10, "resolved": 10, "merged": 10, "scored": 0, "survived": 0,
             "ci_runs": 10, "ci_first_pass": 9, "usd": 10.0}),
        ("bug-fixer", "deepseek/deepseek-v4-flash"): model_scorecard._rates(
            {"runs": 10, "resolved": 10, "merged": 8, "scored": 0, "survived": 0,
             "ci_runs": 10, "ci_first_pass": 5, "usd": 0.2}),        # 80% merge → >5pts below
    }
    assert model_scorecard.model_comparison(this)[0]["within_band"] is False


def test_model_comparison_skips_single_model_agents(model_scorecard):
    this = {("coder", "claude-sonnet-4-6"): model_scorecard._rates(
        {"runs": 3, "resolved": 3, "merged": 3, "scored": 0, "survived": 0,
         "ci_runs": 3, "ci_first_pass": 2, "usd": 3.0})}
    assert model_scorecard.model_comparison(this) == []


# --- Phase 3: LLM judge (pure selection + parse) -----------------------------


def _mrow(run_id, agent, model, date, pr, repo="widget", merged=True):
    return {"run_id": run_id, "agent": agent, "model": model, "date": date,
            "pr_number": pr, "repo": repo, "merged": merged}


def test_select_judge_sample_skips_already_judged_and_unmerged(model_scorecard):
    from datetime import datetime, timezone
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)  # window >= 06-07
    rows = [
        _mrow("run-a", "bug-fixer", "deepseek/deepseek-v4-flash", "2026-06-10", 1),
        _mrow("run-b", "bug-fixer", "deepseek/deepseek-v4-flash", "2026-06-11", 2),
        _mrow("run-c", "coder", "claude-sonnet-4-6", "2026-06-09", 3),
        _mrow("run-d", "bug-fixer", "deepseek/deepseek-v4-flash", "2026-06-01", 4),  # out of window
        _mrow("run-e", "bug-fixer", "deepseek/deepseek-v4-flash", "2026-06-12", None),  # no PR
        _mrow("run-f", "coder", "claude-sonnet-4-6", "2026-06-12", 6, merged=False),  # unmerged
    ]
    picks = model_scorecard.select_judge_sample(rows, {"run-b"}, now, window_days=7, sample=10)
    ids = {p["run_id"] for p in picks}
    assert ids == {"run-a", "run-c"}  # b judged, d out-of-window, e no-PR, f unmerged


def test_select_judge_sample_round_robins_across_cells(model_scorecard):
    from datetime import datetime, timezone
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    rows = [
        _mrow("bf1", "bug-fixer", "deepseek/deepseek-v4-flash", "2026-06-13", 1),
        _mrow("bf2", "bug-fixer", "deepseek/deepseek-v4-flash", "2026-06-12", 2),
        _mrow("bf3", "bug-fixer", "deepseek/deepseek-v4-flash", "2026-06-11", 3),
        _mrow("co1", "coder", "claude-sonnet-4-6", "2026-06-10", 4),
    ]
    picks = model_scorecard.select_judge_sample(rows, set(), now, sample=2)
    # one from each cell before a second from the busy cell
    assert {p["run_id"] for p in picks} == {"bf1", "co1"}


def test_judge_sample_skips_a_forged_qualified_repo(model_scorecard, monkeypatch, tmp_path):
    """Round 2 security finding (PR #881, comment 6152094447): a ledger row's
    `repo` reached it from a run's own meta.json (build_row) — the same
    run-writable field harvest() guards. fetch_pr_context calls gh, so
    judge_sample must refuse the forged owner before reaching it rather than
    pass it straight through. Fails against 43f57916: fetch_pr_context was
    called with the forged owner/repo string."""
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date().isoformat()
    row = _mrow("run-x", "bug-fixer", "deepseek/deepseek-v4-flash", today, 1,
                repo="acme-attacker/widget")
    monkeypatch.setattr(model_scorecard, "_task_for_run", lambda run_id: "")
    called = []
    monkeypatch.setattr(model_scorecard, "fetch_pr_context",
                        lambda *a, **kw: called.append(a) or {"task": "", "title": "",
                                                               "reviews": "", "diff": "d"})
    # Belt-and-suspenders against the unfixed code actually reaching a live
    # `claude -p` call if the guard under test doesn't fire.
    monkeypatch.setattr(model_scorecard, "run_judge",
                        lambda *a, **kw: {"text": "{}", "cost_usd": 0.0})
    out = model_scorecard.judge_sample([row], tmp_path / "judge.jsonl",
                                       sample=3, model="m", write=False)
    assert called == []
    assert out == []


def test_judge_sample_still_resolves_a_registered_repo(model_scorecard, monkeypatch, tmp_path):
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date().isoformat()
    row = _mrow("run-y", "bug-fixer", "deepseek/deepseek-v4-flash", today, 1,
                repo="homebrew-tap")
    monkeypatch.setattr(model_scorecard, "_task_for_run", lambda run_id: "")
    monkeypatch.setattr(model_scorecard.GA, "read_token_pool", _fake_read_token_pool)
    called = []
    monkeypatch.setattr(model_scorecard, "fetch_pr_context",
                        lambda repo, *a, **kw: called.append(repo) or {
                            "task": "", "title": "", "reviews": "", "diff": "d"})
    monkeypatch.setattr(model_scorecard, "run_judge",
                        lambda prompt, model: {"text": '{"review_thoroughness": 3, "fix_quality": 3}',
                                              "cost_usd": 0.0})
    out = model_scorecard.judge_sample([row], tmp_path / "judge.jsonl",
                                       sample=3, model="m", write=False)
    assert called == ["homebrew-tap"]
    assert len(out) == 1


def test_parse_verdict_strips_fence_and_validates(model_scorecard):
    text = '```json\n{"review_thoroughness": 4, "fix_quality": 5, ' \
           '"flagged_issues": ["off-by-one risk"], "rationale": "Tight fix."}\n```'
    v = model_scorecard.parse_verdict(text)
    assert v["review_thoroughness"] == 4 and v["fix_quality"] == 5
    assert v["flagged_issues"] == ["off-by-one risk"] and v["rationale"] == "Tight fix."


def test_parse_verdict_clamps_out_of_range_and_bad_types(model_scorecard):
    # one valid score kept, the out-of-range one nulled (9 > 5)
    v = model_scorecard.parse_verdict('{"review_thoroughness": 9, "fix_quality": 3}')
    assert v["review_thoroughness"] is None and v["fix_quality"] == 3
    # a verdict with NEITHER usable score is dropped entirely
    assert model_scorecard.parse_verdict('{"review_thoroughness": 0, "fix_quality": "x"}') is None


def test_parse_verdict_none_on_garbage(model_scorecard):
    assert model_scorecard.parse_verdict("no json here") is None
    assert model_scorecard.parse_verdict("") is None


def test_run_judge_env_excludes_tmux(model_scorecard, monkeypatch):
    """run_judge spawns `claude -p` with no explicit `env=` before the fix —
    a bare inherit that carries the caller's TMUX/TMUX_PANE straight through
    (issue_01M2EG40BSYN7MXM2EWBT9VKBN). Fails against pre-fix code, where no
    `env=` kwarg was passed at all."""
    import types

    monkeypatch.setenv("TMUX", "/private/tmp/tmux-501/default,1314,3")
    monkeypatch.setenv("TMUX_PANE", "%3")
    monkeypatch.delenv("TMUX_TMPDIR", raising=False)

    captured_kwargs = {}

    def _fake_run(cmd, **kw):
        captured_kwargs.update(kw)
        return types.SimpleNamespace(returncode=0, stdout='{"result": "ok"}', stderr="")

    monkeypatch.setattr(model_scorecard.subprocess, "run", _fake_run)

    result = model_scorecard.run_judge("prompt", "model")

    assert result == {"text": "ok", "cost_usd": 0.0}
    # `env` must be passed EXPLICITLY — a bare inherit (no `env=` kwarg at
    # all) is exactly the pre-fix shape, and would otherwise carry TMUX
    # straight through with nothing here to catch it.
    assert "env" in captured_kwargs, (
        "run_judge called subprocess.run with no explicit env= — it inherits "
        "the caller's full environment, TMUX included")
    assert "TMUX" not in captured_kwargs["env"]
    assert "TMUX_PANE" not in captured_kwargs["env"]
    # Round 2 (issue_01M2EG40BSYN7MXM2EWBT9VKBN): stripping TMUX alone still
    # leaves the same-UID default socket (/tmp/tmux-<uid>/default) reachable
    # to an unqualified `tmux` in the child — TMUX_TMPDIR must point somewhere
    # private instead of being left unset.
    import os
    assert os.path.isdir(captured_kwargs["env"]["TMUX_TMPDIR"])
    assert captured_kwargs["env"]["TMUX_TMPDIR"] != "/tmp"


def test_run_judge_passes_no_tools(model_scorecard, monkeypatch):
    """run_judge's `claude -p` call is a single headless model call with no
    tool access — the boundary decision 0102 draws between a function call and
    a crew. Fails against code with no `--tools` argument at all."""
    import types

    captured_cmd = {}

    def _fake_run(cmd, **kw):
        captured_cmd["cmd"] = cmd
        return types.SimpleNamespace(returncode=0, stdout='{"result": "ok"}', stderr="")

    monkeypatch.setattr(model_scorecard.subprocess, "run", _fake_run)

    model_scorecard.run_judge("prompt", "model")

    cmd = captured_cmd["cmd"]
    assert "--tools" in cmd, "run_judge's claude -p call carries no --tools argument"
    assert cmd[cmd.index("--tools") + 1] == "", (
        "run_judge's --tools argument must be an empty string (no tools)")


def test_task_for_run_refuses_a_run_id_planted_under_two_date_dirs(model_scorecard, monkeypatch, tmp_path):
    """A run-id resolved under two date directories (a planted duplicate,
    issue_01M1JWB7M75SZ160VRJRCGARBF) must not let the newer-sorting directory's
    meta.json silently win and supply a forged task brief into the judge prompt."""
    import json as json_mod

    real = tmp_path / "2026-07-26" / "run-dup1"
    real.mkdir(parents=True)
    (real / "meta.json").write_text(json_mod.dumps({"task": "the real task"}))
    planted = tmp_path / "2099-01-01" / "run-dup1"
    planted.mkdir(parents=True)
    (planted / "meta.json").write_text(json_mod.dumps({"task": "a forged task"}))
    monkeypatch.setattr(model_scorecard, "RUNS_ROOT", tmp_path)

    assert model_scorecard._task_for_run("run-dup1") == ""


def test_build_judge_prompt_contains_context_and_json_contract(model_scorecard):
    row = {"pr_number": 441, "repo": "widget", "agent": "bug-fixer", "model": "deepseek/deepseek-v4-flash"}
    ctx = {"task": "Fix formatUSD", "title": "fix: formatUSD", "reviews": "@bot: LGTM", "diff": "- a\n+ b"}
    pr = model_scorecard.build_judge_prompt(row, ctx)
    assert "PR #441" in pr and "Fix formatUSD" in pr and "review_thoroughness" in pr
    assert "Output ONLY a JSON object" in pr


@pytest.mark.rich_registry
def test_build_judge_prompt_names_the_registered_owner_for_homebrew_tap(model_scorecard):
    """Round-1 review (PR #881, comment 6150937461): this label built
    `convivy/{repo}` directly — the same pattern the PR centralizes
    everywhere else."""
    row = {"pr_number": 1, "repo": "brewcask-tap", "agent": "bug-fixer", "model": "sonnet"}
    ctx = {"task": "t", "title": "t", "reviews": "", "diff": "d"}
    pr = model_scorecard.build_judge_prompt(row, ctx)
    assert "caskforge/brewcask-tap" in pr


# --- CI_WORKFLOW is registry-derived, not a hand-maintained product list ---


@pytest.mark.rich_registry
def test_ci_workflow_matches_the_registrys_shared_and_own_coverage_products(model_scorecard):
    """Pinned against the registry's own accessors, not a copy of the slugs —
    this fails red if CI_WORKFLOW drifts back to a hardcoded list."""
    import projects
    expected = {slug: "ci.yml"
                for slug in projects.shared_coverage_products() + projects.own_coverage_products()}
    assert model_scorecard.CI_WORKFLOW == expected


# --- gh-calling slug sites: the registered owner, not always convivy --------


@pytest.mark.rich_registry
def test_pr_maps_queries_the_registered_owner_for_homebrew_tap(model_scorecard, monkeypatch):
    captured = {}

    def fake_gh_json(args, env):
        captured["args"] = args
        return []

    monkeypatch.setattr(model_scorecard, "_gh_json", fake_gh_json)
    model_scorecard.pr_maps("brewcask-tap", {})

    assert captured["args"] and "--repo" in captured["args"]
    repo_arg = captured["args"][captured["args"].index("--repo") + 1]
    assert repo_arg == "caskforge/brewcask-tap"


@pytest.mark.rich_registry
def test_ci_first_pass_queries_the_registered_owner_for_homebrew_tap(model_scorecard, monkeypatch):
    monkeypatch.setitem(model_scorecard.CI_WORKFLOW, "brewcask-tap", "ci.yml")
    captured = {}

    def fake_gh_json(args, env):
        captured["args"] = args
        return []

    monkeypatch.setattr(model_scorecard, "_gh_json", fake_gh_json)
    model_scorecard.ci_first_pass("brewcask-tap", "a-branch", {})

    assert captured["args"] and "--repo" in captured["args"]
    repo_arg = captured["args"][captured["args"].index("--repo") + 1]
    assert repo_arg == "caskforge/brewcask-tap"


@pytest.mark.rich_registry
def test_was_reverted_queries_the_registered_owner_for_homebrew_tap(model_scorecard, monkeypatch):
    captured = {}

    def fake_gh_json(args, env):
        captured["args"] = args
        return []

    monkeypatch.setattr(model_scorecard, "_gh_json", fake_gh_json)
    model_scorecard.was_reverted("brewcask-tap", 1, "2026-05-20T18:34:53Z", {})

    assert captured["args"] and "--repo" in captured["args"]
    repo_arg = captured["args"][captured["args"].index("--repo") + 1]
    assert repo_arg == "caskforge/brewcask-tap"


@pytest.mark.rich_registry
def test_had_follow_up_bug_queries_the_registered_owner_for_homebrew_tap(
        model_scorecard, monkeypatch):
    captured = {}

    def fake_gh_json(args):
        captured["args"] = args
        return []

    monkeypatch.setattr(model_scorecard, "_gh_json", fake_gh_json)
    model_scorecard.had_follow_up_bug("brewcask-tap", 1, "2026-05-20T18:34:53Z")

    assert captured["args"] and "--repo" in captured["args"]
    repo_arg = captured["args"][captured["args"].index("--repo") + 1]
    assert repo_arg == "caskforge/brewcask-tap"


@pytest.mark.rich_registry
def test_fetch_pr_context_queries_the_registered_owner_for_homebrew_tap(
        model_scorecard, monkeypatch):
    captured = []

    def fake_gh_text(args, env):
        captured.append(args)
        return "diff"

    def fake_gh_json(args, env):
        captured.append(args)
        return {}

    monkeypatch.setattr(model_scorecard, "_gh_text", fake_gh_text)
    monkeypatch.setattr(model_scorecard, "_gh_json", fake_gh_json)
    model_scorecard.fetch_pr_context("brewcask-tap", 1, "task", {})

    assert captured and all("--repo" in c for c in captured)
    for c in captured:
        assert c[c.index("--repo") + 1] == "caskforge/brewcask-tap"


def test_render_judge_report_averages_non_null_scores(model_scorecard):
    rows = [
        {"agent": "bug-fixer", "model": "deepseek/deepseek-v4-flash", "review_thoroughness": 4,
         "fix_quality": 5, "judge_cost_usd": 0.02},
        {"agent": "bug-fixer", "model": "deepseek/deepseek-v4-flash", "review_thoroughness": 2,
         "fix_quality": None, "judge_cost_usd": 0.02},  # fix_quality null → excluded from fix avg
    ]
    out = model_scorecard.render_judge_report(rows)
    assert "3.0" in out  # review avg (4+2)/2
    assert "5.0" in out  # fix avg = 5/1 (the null excluded)


def test_summarize_rates(model_scorecard):
    rows = [
        {"agent": "bug-fixer", "model": "deepseek/deepseek-v4-flash", "cost_usd": 0.01,
         "pr_state": "MERGED", "merged": True, "ci_first_pass": True},
        {"agent": "bug-fixer", "model": "deepseek/deepseek-v4-flash", "cost_usd": 0.03,
         "pr_state": "MERGED", "merged": True, "ci_first_pass": False},
        {"agent": "bug-fixer", "model": "deepseek/deepseek-v4-flash", "cost_usd": 0.02,
         "pr_state": "unresolved", "merged": None, "ci_first_pass": None},
    ]
    a = model_scorecard.summarize(rows)[("bug-fixer", "deepseek/deepseek-v4-flash")]
    assert a["runs"] == 3 and a["resolved"] == 2 and a["merged"] == 2
    assert a["ci_runs"] == 2 and a["ci_first_pass"] == 1          # 1 of 2 passed first try
    assert abs(a["usd"] - 0.06) < 1e-9


# --- a None model beside a str model (issue_01M3ABF19NHZK4KP5PQY6SFRX7) -------
# A PR without a `model:<id>` label, or a judge row missing an agent, aggregates
# under a None key beside a str one; a bare sorted() over those keys raises
# "'<' not supported between 'str' and 'NoneType'".


def test_render_judge_report_sorts_none_model_beside_str_model(model_scorecard):
    def j(model):
        return {"agent": "bug-fixer", "model": model, "review_thoroughness": 4,
                "fix_quality": 5, "judge_cost_usd": 0.01}
    rows = [j("z-model"), j(None), j("a-model")]
    out = model_scorecard.render_judge_report(rows)
    body = [ln for ln in out.splitlines() if ln.startswith("| bug-fixer")]
    assert [ln.split("|")[2].strip() for ln in body] == ["`None`", "`a-model`", "`z-model`"]
    assert model_scorecard.render_judge_report(list(reversed(rows))) == out


# --- report_to_ledger: the Bosun's first ledger writer -------------------------
# (issue_01M0TP37M3RRRY5BD1Z4B8GCB0 Part 1, POST /ledger-entries)


_LEDGER_ROW = {
    "run_id": "run-abc", "date": "2026-10-01", "agent": "coder",
    "model": "claude-sonnet-5", "repo": "widget", "cost_usd": 0.1234,
    "pr_number": 9, "pr_state": "MERGED", "merged": True,
}


# --- ledger_lane (E1·6) --------------------------------------------------------


def test_ledger_lane_gateway_for_a_gateway_model(model_scorecard):
    assert model_scorecard.ledger_lane("openai/gpt-5") == "gateway"
    assert model_scorecard.ledger_lane("deepseek/deepseek-v4-flash") == "gateway"


def test_ledger_lane_subscription_for_an_anthropic_model(model_scorecard):
    assert model_scorecard.ledger_lane("claude-sonnet-5") == "subscription"


def test_ledger_lane_none_for_no_model(model_scorecard):
    assert model_scorecard.ledger_lane(None) is None
    assert model_scorecard.ledger_lane("") is None


def test_ledger_lane_openai_subscription_for_a_codex_model(model_scorecard):
    """Codex runs bill at OpenAI's API rates (telemetry.CODEX_BILLING_SOURCE),
    neither the gateway's metered lane nor the Anthropic subscription — against
    an implementation that falls through to "subscription" for anything that
    isn't a gateway model, this would read "subscription" instead of
    "openai-subscription"."""
    assert model_scorecard.ledger_lane("codex-auto-review") == "openai-subscription"
    assert model_scorecard.ledger_lane("gpt-5.5") == "openai-subscription"


def test_report_to_ledger_posts_the_documented_fields_and_dedup_key(
        model_scorecard, monkeypatch, tmp_path):
    monkeypatch.setattr(model_scorecard, "RUNS_ROOT", tmp_path)
    captured = []

    def fake_post(product, usd, *, run_id, dedup_key, lane, agent=None, model=None,
                 tok=None, timeout=5):
        captured.append({"product": product, "usd": usd, "run_id": run_id,
                         "dedup_key": dedup_key, "lane": lane, "agent": agent, "model": model})
        return {"status": "recorded", "entry": {"id": "e1"}}

    monkeypatch.setattr(model_scorecard.issue_client, "post_ledger_entry", fake_post)
    report = model_scorecard.report_to_ledger([_LEDGER_ROW])
    assert report == {"recorded": 1, "not_recorded": 0, "errors": []}
    # _LEDGER_ROW's model ("claude-sonnet-5") is neither a gateway nor a Codex
    # model, so ledger_lane resolves it to "subscription" — against the unfixed
    # code (no `lane` kwarg on post_ledger_entry at all), this fails with a
    # TypeError before the assertion is ever reached.
    assert captured == [{"product": "widget", "usd": 0.1234, "run_id": "run-abc",
                         "dedup_key": "run-abc", "lane": "subscription",
                         "agent": "coder", "model": "claude-sonnet-5"}]


def test_report_to_ledger_sends_the_gateway_lane_for_a_gateway_model(
        model_scorecard, monkeypatch, tmp_path):
    monkeypatch.setattr(model_scorecard, "RUNS_ROOT", tmp_path)
    captured = []

    def fake_post(product, usd, *, run_id, dedup_key, lane, **kw):
        captured.append(lane)
        return {"status": "recorded", "entry": {}}

    monkeypatch.setattr(model_scorecard.issue_client, "post_ledger_entry", fake_post)
    model_scorecard.report_to_ledger([dict(_LEDGER_ROW, model="openai/gpt-5")])
    assert captured == ["gateway"]


def test_report_to_ledger_sends_the_openai_subscription_lane_for_a_codex_model(
        model_scorecard, monkeypatch, tmp_path):
    """A Codex model (is_codex_model) gets its own ledger lane, not a skip —
    against an implementation that still treats a Codex model as having no
    determinable lane, this row would never reach post_ledger_entry and
    `captured` below would stay empty. Pinned to the payload report_to_ledger
    actually builds for the POST, not to a value this test constructs."""
    monkeypatch.setattr(model_scorecard, "RUNS_ROOT", tmp_path)
    captured = []

    def fake_post(product, usd, *, run_id, dedup_key, lane, agent=None, model=None,
                 tok=None, timeout=5, **kw):
        captured.append({"product": product, "usd": usd, "run_id": run_id,
                         "dedup_key": dedup_key, "lane": lane, "agent": agent, "model": model})
        return {"status": "recorded", "entry": {}}

    monkeypatch.setattr(model_scorecard.issue_client, "post_ledger_entry", fake_post)
    row = dict(_LEDGER_ROW, run_id="run-codex", model="codex-auto-review")
    report = model_scorecard.report_to_ledger([row])
    assert report == {"recorded": 1, "not_recorded": 0, "errors": []}
    assert captured == [{"product": "widget", "usd": 0.1234, "run_id": "run-codex",
                         "dedup_key": "run-codex", "lane": "openai-subscription",
                         "agent": "coder", "model": "codex-auto-review"}]


def test_report_to_ledger_skips_a_row_with_no_model(model_scorecard, monkeypatch):
    calls = []
    monkeypatch.setattr(model_scorecard.issue_client, "post_ledger_entry",
                        lambda *a, **kw: calls.append(1) or {"status": "recorded", "entry": {}})
    row = dict(_LEDGER_ROW, run_id="run-no-model", model=None)
    report = model_scorecard.report_to_ledger([row])
    assert calls == []
    assert report["errors"] == [("run-no-model", "skip:no-lane", "no ledger lane for model None")]


def test_report_to_ledger_rejects_a_non_finite_cost(model_scorecard, monkeypatch):
    """`cost_usd` is validated finite and non-negative before any arithmetic
    or network call — the same boundary `ledger.post_run_ledger_entry`
    applies on its own side. Against code with no such check, `float('nan')`
    would flow straight into `remaining_after_live`'s subtraction."""
    calls = []
    monkeypatch.setattr(model_scorecard.issue_client, "list_ledger_entries",
                        lambda *a, **kw: calls.append("read"))
    monkeypatch.setattr(model_scorecard.issue_client, "post_ledger_entry",
                        lambda *a, **kw: calls.append("post"))
    row = dict(_LEDGER_ROW, cost_usd=float("nan"))
    report = model_scorecard.report_to_ledger([row])
    assert calls == []
    assert report["errors"] == [("run-abc", "error:invalid-cost",
                                 "non-finite or negative cost_usd nan")]


def test_report_to_ledger_sends_item_id_only_when_the_row_carries_one(
        model_scorecard, monkeypatch, tmp_path):
    """`post_ledger_entry`'s `item_id` is omitted entirely for a row with none
    (the pre-E16·5 shape, and any run with no dispatched work item) rather
    than sent as `None` — against the unfixed code (which always passed
    `item_id=r.get("item_id")`), the first captured call below would be
    `{"item_id": None}`, not `{}`."""
    monkeypatch.setattr(model_scorecard, "RUNS_ROOT", tmp_path)
    captured = []

    def fake_post(product, usd, *, run_id, dedup_key, lane=None, agent=None, model=None,
                 tok=None, timeout=5, **kw):
        captured.append(kw)
        return {"status": "recorded", "entry": {}}

    monkeypatch.setattr(model_scorecard.issue_client, "post_ledger_entry", fake_post)
    model_scorecard.report_to_ledger([_LEDGER_ROW])
    assert captured == [{}]
    model_scorecard.report_to_ledger([dict(_LEDGER_ROW, item_id="issue_E16_5")])
    assert captured == [{}, {"item_id": "issue_E16_5"}]


# --- report_to_ledger resolves a missing item_id from the row's own PR (E16) -


def test_report_to_ledger_resolves_the_item_from_the_rows_pr_when_it_carries_none(
        model_scorecard, monkeypatch, tmp_path):
    """A row with no `item_id` but a PR (`_LEDGER_ROW`'s own `pr_number`, no
    `pr_url`) still attaches a work item — resolved through the same PR-match
    rule `cost_join.build_item_costs` applies on the run-cost side, via
    `ledger.resolve_item_id`'s own `cost_join.item_for_pr`. Against code that
    sends `item_id` only from the row's own field, `captured` below would
    carry no `item_id` key at all."""
    monkeypatch.setattr(model_scorecard, "RUNS_ROOT", tmp_path)
    _stub_no_live_reported_entries(model_scorecard, monkeypatch)
    monkeypatch.setattr(
        model_scorecard.issue_client, "list_all_items",
        lambda *a, **kw: [{"id": "issue_PR9", "pr_repo": "convivy/widget",
                           "pr": 9, "pr_url": None}])
    captured = []

    def fake_post(product, usd, *, run_id, dedup_key, lane=None, agent=None, model=None,
                 tok=None, timeout=5, **kw):
        captured.append(kw)
        return {"status": "recorded", "entry": {}}
    monkeypatch.setattr(model_scorecard.issue_client, "post_ledger_entry", fake_post)

    model_scorecard.report_to_ledger([_LEDGER_ROW])

    assert captured == [{"item_id": "issue_PR9"}]


def test_report_to_ledger_does_not_fetch_items_when_the_row_already_names_one(
        model_scorecard, monkeypatch, tmp_path):
    """A row that already carries `item_id` never reaches the item-corpus
    fetch at all — `list_all_items` is left unstubbed to raise `AssertionError`
    rather than quietly succeed, so a regression that always fetches fails here."""
    monkeypatch.setattr(model_scorecard, "RUNS_ROOT", tmp_path)
    _stub_no_live_reported_entries(model_scorecard, monkeypatch)

    def _boom(*a, **kw):
        raise AssertionError("must not fetch the item corpus when item_id is already given")
    monkeypatch.setattr(model_scorecard.issue_client, "list_all_items", _boom)
    captured = []

    def fake_post(product, usd, *, run_id, dedup_key, lane=None, agent=None, model=None,
                 tok=None, timeout=5, **kw):
        captured.append(kw)
        return {"status": "recorded", "entry": {}}
    monkeypatch.setattr(model_scorecard.issue_client, "post_ledger_entry", fake_post)

    model_scorecard.report_to_ledger([dict(_LEDGER_ROW, item_id="issue_E16_5")])

    assert captured == [{"item_id": "issue_E16_5"}]


def test_report_to_ledger_posts_with_no_item_when_the_row_has_neither_issue_nor_pr(
        model_scorecard, monkeypatch, tmp_path):
    monkeypatch.setattr(model_scorecard, "RUNS_ROOT", tmp_path)
    _stub_no_live_reported_entries(model_scorecard, monkeypatch)
    captured = []

    def fake_post(product, usd, *, run_id, dedup_key, lane=None, agent=None, model=None,
                 tok=None, timeout=5, **kw):
        captured.append(kw)
        return {"status": "recorded", "entry": {}}
    monkeypatch.setattr(model_scorecard.issue_client, "post_ledger_entry", fake_post)

    row = dict(_LEDGER_ROW, pr_number=None)
    model_scorecard.report_to_ledger([row])

    assert captured == [{}]


def test_report_to_ledger_posts_with_no_item_and_records_why_on_a_failed_lookup(
        model_scorecard, monkeypatch, tmp_path):
    """A failed item-corpus fetch (the Library unreachable) must never block or
    fail the ledger post: this posts with no `item_id`, and the reason is
    recorded on the run's own meta.json beside `ledger_post_status`."""
    monkeypatch.setattr(model_scorecard, "RUNS_ROOT", tmp_path)
    _stub_no_live_reported_entries(model_scorecard, monkeypatch)
    run_dir = _make_run_dir(tmp_path, "run-abc")

    def _boom(*a, **kw):
        raise model_scorecard.issue_client.IssueQueueUnavailable("library is down")
    monkeypatch.setattr(model_scorecard.issue_client, "list_all_items", _boom)
    captured = []

    def fake_post(product, usd, *, run_id, dedup_key, lane=None, agent=None, model=None,
                 tok=None, timeout=5, **kw):
        captured.append(kw)
        return {"status": "recorded", "entry": {}}
    monkeypatch.setattr(model_scorecard.issue_client, "post_ledger_entry", fake_post)

    model_scorecard.report_to_ledger([_LEDGER_ROW])

    assert captured == [{}]
    meta = json.loads((run_dir / "meta.json").read_text())
    assert "IssueQueueUnavailable" in meta[model_scorecard.LG.ITEM_LOOKUP_ERROR_FIELD]


def test_report_to_ledger_fetches_the_item_corpus_at_most_once_per_pass(
        model_scorecard, monkeypatch, tmp_path):
    """Two rows that both need a PR match in one `report_to_ledger` call share
    ONE item-corpus fetch — against a per-row fetch, `fetch_calls` below would
    read `[1, 1]`, not `[1]`."""
    monkeypatch.setattr(model_scorecard, "RUNS_ROOT", tmp_path)
    _stub_no_live_reported_entries(model_scorecard, monkeypatch)
    fetch_calls = []

    def _list_all_items(*a, **kw):
        fetch_calls.append(1)
        return [{"id": "issue_PR9", "pr_repo": "convivy/widget", "pr": 9, "pr_url": None}]
    monkeypatch.setattr(model_scorecard.issue_client, "list_all_items", _list_all_items)
    monkeypatch.setattr(model_scorecard.issue_client, "post_ledger_entry",
                        lambda *a, **kw: {"status": "recorded", "entry": {}})

    model_scorecard.report_to_ledger(
        [dict(_LEDGER_ROW, run_id="run-abc"), dict(_LEDGER_ROW, run_id="run-xyz")])

    assert fetch_calls == [1]


def test_report_to_ledger_a_rerun_sends_the_same_dedup_key(model_scorecard, monkeypatch, tmp_path):
    """A later harvest pass re-resolves the same run (e.g. its PR is still open) and
    upserts the same row again; the dedup_key sent must not change, since that is
    what makes the resend idempotent at the Library rather than double-counted."""
    monkeypatch.setattr(model_scorecard, "RUNS_ROOT", tmp_path)
    seen = []
    monkeypatch.setattr(
        model_scorecard.issue_client, "post_ledger_entry",
        lambda product, usd, *, run_id, dedup_key, **kw:
            seen.append(dedup_key) or {"status": "recorded", "entry": {}})
    model_scorecard.report_to_ledger([_LEDGER_ROW])
    model_scorecard.report_to_ledger([_LEDGER_ROW])
    assert seen == ["run-abc", "run-abc"]


def test_report_to_ledger_counts_not_recorded_without_retrying(model_scorecard, monkeypatch, tmp_path):
    monkeypatch.setattr(model_scorecard, "RUNS_ROOT", tmp_path)
    calls = []
    monkeypatch.setattr(
        model_scorecard.issue_client, "post_ledger_entry",
        lambda *a, **kw: calls.append(1) or {"status": "not-recorded", "entry": {}})
    report = model_scorecard.report_to_ledger([_LEDGER_ROW])
    assert report == {"recorded": 0, "not_recorded": 1, "errors": []}
    assert len(calls) == 1


def test_report_to_ledger_counts_errors_and_keeps_going_over_the_rest(
        model_scorecard, monkeypatch, tmp_path):
    monkeypatch.setattr(model_scorecard, "RUNS_ROOT", tmp_path)
    rows = [dict(_LEDGER_ROW, run_id="run-a"), dict(_LEDGER_ROW, run_id="run-b")]
    monkeypatch.setattr(
        model_scorecard.issue_client, "post_ledger_entry",
        lambda product, usd, *, run_id, dedup_key, **kw:
            {"status": "error:OSError", "error": "down", "entry": {}})
    report = model_scorecard.report_to_ledger(rows)
    assert report["recorded"] == 0 and report["not_recorded"] == 0
    assert report["errors"] == [("run-a", "error:OSError", "down"),
                                ("run-b", "error:OSError", "down")]


# --- report_to_ledger: a disproportionate refusal (E1·9, budget-enforcement
# epic Clause 6) is recorded on the run and never resubmitted unchanged -------


def _make_run_dir(root: Path, run_id: str, *, meta_extra=None) -> Path:
    rd = root / "2026-10-13" / run_id
    rd.mkdir(parents=True)
    meta = {"run_id": run_id, "agent": "coder", "task": "t", "repo": "widget",
            "worktree": "/wt", "profile": "p", "model": "claude-sonnet-5",
            "started_at": "2026-10-13T00:00:00Z", "pid": None}
    meta.update(meta_extra or {})
    (rd / "meta.json").write_text(json.dumps(meta))
    return rd


def _stub_no_live_reported_entries(model_scorecard, monkeypatch):
    """Every `report_to_ledger` test below whose row's run dir resolves now
    makes a Library read (`ledger.read_live_ledger_state`) before its own
    post — a test that doesn't stub it would otherwise hit the real network
    client and get back `IssueQueueUnavailable`, which `report_to_ledger`
    correctly counts as an error rather than posting; stubbing an empty
    ledger here reproduces the pre-CQ·446-round-2 case of a run with
    nothing live-reported yet."""
    monkeypatch.setattr(model_scorecard.issue_client, "list_ledger_entries",
                        lambda *a, **kw: [])


def test_report_to_ledger_marks_a_disproportionate_refusal_on_the_run(
        model_scorecard, monkeypatch, tmp_path):
    """Against the unfixed code (no write back to the run at all), the run dir's
    meta.json never gains `ledger_report_refused_usd`/`_reason`, so the second
    assertion below fails. The poster/dedup_key fields name the sweep as the
    poster and `run_id` as the key it posted (and was refused) under."""
    monkeypatch.setattr(model_scorecard, "RUNS_ROOT", tmp_path)
    _stub_no_live_reported_entries(model_scorecard, monkeypatch)
    run_dir = _make_run_dir(tmp_path, "run-abc")
    monkeypatch.setattr(
        model_scorecard.issue_client, "post_ledger_entry",
        lambda *a, **kw: {"status": "disproportionate",
                          "error": "this report exceeds 10x the scope's soft cap",
                          "reported_usd": 0.1234, "basis": "soft_cap", "entry": {}})
    report = model_scorecard.report_to_ledger([_LEDGER_ROW])
    assert report["errors"] == [("run-abc", "disproportionate",
                                 "this report exceeds 10x the scope's soft cap")]
    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["ledger_report_refused_usd"] == 0.1234
    assert meta["ledger_report_refused_reason"] == "this report exceeds 10x the scope's soft cap"
    assert meta["ledger_report_refused_poster"] == "sweep"
    assert meta["ledger_report_refused_dedup_key"] == "run-abc"


def test_report_to_ledger_does_not_resend_an_unchanged_refused_figure(
        model_scorecard, monkeypatch, tmp_path):
    """Clause 6: a refused report is surfaced against its run and not resubmitted
    unchanged. Against the unfixed code (no pre-post check against the run's own
    mark), the stub below would receive a second call for the same $0.1234 figure
    — `calls` would be 2, not 1. The marker comes from the state-tree file
    (round 2, PR #1021: never meta.json, which a dispatched run can write
    for its whole execution window)."""
    monkeypatch.setattr(model_scorecard, "RUNS_ROOT", tmp_path)
    _stub_no_live_reported_entries(model_scorecard, monkeypatch)
    _make_run_dir(tmp_path, "run-abc")
    model_scorecard.LG.write_refusal_marker(
        "run-abc", usd=0.1234,
        reason="this report exceeds 10x the scope's soft cap",
        poster="sweep", dedup_key="run-abc")
    calls = []
    monkeypatch.setattr(
        model_scorecard.issue_client, "post_ledger_entry",
        lambda *a, **kw: calls.append(1) or {"status": "disproportionate",
                                             "error": "x", "entry": {}})
    report = model_scorecard.report_to_ledger([_LEDGER_ROW])
    assert calls == []
    assert report["errors"] == [("run-abc", "skip:refused-unchanged", "")]


def test_report_to_ledger_posts_despite_a_meta_json_only_refused_figure(
        model_scorecard, monkeypatch, tmp_path):
    """Round 2 (security review of PR #1021 round 1, BLOCKING finding): a
    run's own meta.json carrying `ledger_report_refused_usd` equal to the
    row's `cost_usd`, with NO marker file, must not be skipped — a
    dispatched run can forge that field onto its own meta.json.

    AGAINST THE ROUND-1 CODE (a4e1bc1e3's ledger.py, which read
    `REFUSED_USD_FIELD` straight off the run's meta.json), this test fails:
    `calls` comes back `[]` and the row is counted `"skip:refused-unchanged"`
    instead of `"recorded"`."""
    monkeypatch.setattr(model_scorecard, "RUNS_ROOT", tmp_path)
    _stub_no_live_reported_entries(model_scorecard, monkeypatch)
    _make_run_dir(tmp_path, "run-abc", meta_extra={
        "ledger_report_refused_usd": 0.1234,
        "ledger_report_refused_reason": "forged by the run itself",
    })
    calls = []
    monkeypatch.setattr(
        model_scorecard.issue_client, "post_ledger_entry",
        lambda *a, **kw: calls.append(1) or {"status": "recorded", "entry": {}})
    report = model_scorecard.report_to_ledger([_LEDGER_ROW])
    assert calls == [1]
    assert report == {"recorded": 1, "not_recorded": 0, "errors": []}


def test_report_to_ledger_reposts_a_corrected_figure(model_scorecard, monkeypatch, tmp_path):
    """A later pass with a DIFFERENT figure for the same run (its cost was
    corrected) posts again — against an implementation that keys the skip on
    run_id alone rather than the figure, the stub below would never be called
    and `calls` would stay empty."""
    monkeypatch.setattr(model_scorecard, "RUNS_ROOT", tmp_path)
    _stub_no_live_reported_entries(model_scorecard, monkeypatch)
    _make_run_dir(tmp_path, "run-abc")
    model_scorecard.LG.write_refusal_marker(
        "run-abc", usd=999.0,
        reason="this report exceeds 10x the scope's soft cap",
        poster="sweep", dedup_key="run-abc")
    calls = []
    monkeypatch.setattr(
        model_scorecard.issue_client, "post_ledger_entry",
        lambda *a, **kw: calls.append(1) or {"status": "recorded", "entry": {}})
    report = model_scorecard.report_to_ledger([_LEDGER_ROW])  # cost_usd 0.1234, not 999.0
    assert calls == [1]
    assert report == {"recorded": 1, "not_recorded": 0, "errors": []}


def test_report_to_ledger_posts_unconditionally_with_no_resolvable_run_dir(
        model_scorecard, monkeypatch, tmp_path):
    """A synthetic row (or a run that has aged off disk) has no run dir to check
    or mark — it posts the same as it always did, rather than erroring."""
    monkeypatch.setattr(model_scorecard, "RUNS_ROOT", tmp_path)  # empty: no run dirs exist
    calls = []
    monkeypatch.setattr(
        model_scorecard.issue_client, "post_ledger_entry",
        lambda *a, **kw: calls.append(1) or {"status": "disproportionate",
                                             "error": "x", "entry": {}})
    report = model_scorecard.report_to_ledger([_LEDGER_ROW])
    assert calls == [1]
    assert report["errors"] == [("run-abc", "disproportionate", "x")]


# --- report_to_ledger: a run already `ledger_post_status: "recorded"` is --
# skipped (either agentctl's own at-finish post, or an earlier sweep pass) ---


def test_report_to_ledger_skips_a_run_already_recorded(model_scorecard, monkeypatch, tmp_path):
    """Against the unfixed code (no such check), the stub below would be
    called and `calls` would be `[1]`, not `[]`."""
    monkeypatch.setattr(model_scorecard, "RUNS_ROOT", tmp_path)
    _make_run_dir(tmp_path, "run-abc", meta_extra={"ledger_post_status": "recorded"})
    calls = []
    monkeypatch.setattr(
        model_scorecard.issue_client, "post_ledger_entry",
        lambda *a, **kw: calls.append(1) or {"status": "recorded", "entry": {}})
    report = model_scorecard.report_to_ledger([_LEDGER_ROW])
    assert calls == []
    assert report["errors"] == [("run-abc", "skip:already-recorded", "")]


def test_report_to_ledger_posts_a_run_not_yet_recorded_and_marks_it(
        model_scorecard, monkeypatch, tmp_path):
    """A run with no prior `ledger_post_status` mark posts normally, and this
    pass's own status is what gets written back — the mark a later pass (or
    `agentctl`'s own at-finish post, for a different run) reads to skip it."""
    monkeypatch.setattr(model_scorecard, "RUNS_ROOT", tmp_path)
    _stub_no_live_reported_entries(model_scorecard, monkeypatch)
    run_dir = _make_run_dir(tmp_path, "run-abc")
    monkeypatch.setattr(
        model_scorecard.issue_client, "post_ledger_entry",
        lambda *a, **kw: {"status": "recorded", "entry": {}})
    report = model_scorecard.report_to_ledger([_LEDGER_ROW])
    assert report == {"recorded": 1, "not_recorded": 0, "errors": []}
    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["ledger_post_status"] == "recorded"


def test_report_to_ledger_sends_the_rows_day(model_scorecard, monkeypatch, tmp_path):
    """A row carrying `day` (the sweep's own finish-day bucket) sends it
    through to `post_ledger_entry`; `_LEDGER_ROW` carries none, so this pins
    the field is forwarded rather than silently dropped."""
    monkeypatch.setattr(model_scorecard, "RUNS_ROOT", tmp_path)
    _stub_no_live_reported_entries(model_scorecard, monkeypatch)
    _make_run_dir(tmp_path, "run-abc")
    captured = {}

    def fake_post(product, usd, *, run_id, dedup_key, lane=None, **kw):
        captured.update(kw)
        return {"status": "recorded", "entry": {}}
    monkeypatch.setattr(model_scorecard.issue_client, "post_ledger_entry", fake_post)
    model_scorecard.report_to_ledger([dict(_LEDGER_ROW, day="2026-10-20")])
    assert captured["day"] == "2026-10-20"


# --- report_to_ledger: the backstop subtracts what's already posted for this
# lane, read FRESH from the Library's own ledger (CQ·446 round 2), so a run
# whose at-finish post failed outright is not double-counted between the
# live deltas and this sweep's own entry -----------------------------------


def test_report_to_ledger_posts_only_the_remainder_after_live_reporting(
        model_scorecard, monkeypatch, tmp_path):
    """Against the unfixed code (no subtraction at all), `_LEDGER_ROW`'s full
    $0.1234 would be posted again here, double-counting the $0.10 the live
    reporter already got through while the run was still going."""
    monkeypatch.setattr(model_scorecard, "RUNS_ROOT", tmp_path)
    _make_run_dir(tmp_path, "run-abc")
    monkeypatch.setattr(
        model_scorecard.issue_client, "list_ledger_entries",
        lambda *a, **kw: [{"usd": 0.10, "lane": "subscription", "voided_at": None}])
    captured = {}

    def fake_post(product, usd, *, run_id, dedup_key, lane, **kw):
        captured["usd"] = usd
        return {"status": "recorded", "entry": {}}

    monkeypatch.setattr(model_scorecard.issue_client, "post_ledger_entry", fake_post)
    report = model_scorecard.report_to_ledger([_LEDGER_ROW])
    assert report == {"recorded": 1, "not_recorded": 0, "errors": []}
    assert captured["usd"] == pytest.approx(0.0234)


def test_report_to_ledger_skips_a_run_the_live_reporter_already_fully_covered(
        model_scorecard, monkeypatch, tmp_path):
    """The run's own at-finish post failed (meta carries no
    `ledger_post_status: recorded`), but its live reporter already posted the
    whole $0.1234 before the run finished — the backstop sweep must post
    nothing, not the full figure again."""
    monkeypatch.setattr(model_scorecard, "RUNS_ROOT", tmp_path)
    _make_run_dir(tmp_path, "run-abc")
    monkeypatch.setattr(
        model_scorecard.issue_client, "list_ledger_entries",
        lambda *a, **kw: [{"usd": 0.1234, "lane": "subscription", "voided_at": None}])
    calls = []
    monkeypatch.setattr(
        model_scorecard.issue_client, "post_ledger_entry",
        lambda *a, **kw: calls.append(1) or {"status": "recorded", "entry": {}})
    report = model_scorecard.report_to_ledger([_LEDGER_ROW])
    assert calls == []
    assert report["errors"] == [("run-abc", "skip:already-reported-live", "")]


def test_report_to_ledger_a_disproportionate_refusal_marks_the_post_live_remainder(
        model_scorecard, monkeypatch, tmp_path):
    """`ledger_report_refused_usd` must record the figure THIS sweep actually
    sent (the post-live-reduction remainder), not the row's raw `cost_usd` —
    otherwise a later pass's own (identically-reduced) figure would never
    compare equal to it, and `skip:refused-unchanged` would never fire."""
    monkeypatch.setattr(model_scorecard, "RUNS_ROOT", tmp_path)
    run_dir = _make_run_dir(tmp_path, "run-abc")
    monkeypatch.setattr(
        model_scorecard.issue_client, "list_ledger_entries",
        lambda *a, **kw: [{"usd": 0.10, "lane": "subscription", "voided_at": None}])
    monkeypatch.setattr(
        model_scorecard.issue_client, "post_ledger_entry",
        lambda *a, **kw: {"status": "disproportionate", "error": "x",
                          "reported_usd": 0.0234, "basis": "soft_cap", "entry": {}})
    report = model_scorecard.report_to_ledger([_LEDGER_ROW])
    assert report["errors"] == [("run-abc", "disproportionate", "x")]
    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["ledger_report_refused_usd"] == pytest.approx(0.0234)

    # A second pass, same row, same (still-fixed) live-reported figure at the
    # Library: the reduced remainder reproduces 0.0234 again, matches the
    # refused mark, and is held back rather than resent.
    calls = []
    monkeypatch.setattr(
        model_scorecard.issue_client, "post_ledger_entry",
        lambda *a, **kw: calls.append(1))
    report2 = model_scorecard.report_to_ledger([_LEDGER_ROW])
    assert calls == []
    assert report2["errors"] == [("run-abc", "skip:refused-unchanged", "x")]


def test_report_to_ledger_posts_nothing_when_the_ledger_read_fails_and_a_later_pass_posts_the_remainder(
        model_scorecard, monkeypatch, tmp_path):
    """Against 3adf80d9b, there is no ledger read at all here — this failure
    mode doesn't exist, and the row posts (or is refused) on its raw
    `cost_usd` regardless. Against the fixed code, a Library read that fails
    must post NOTHING for this row (never falling back to the raw,
    un-reduced figure), and a LATER pass whose read succeeds — finding
    nothing yet reported, since the first attempt posted nothing — posts
    exactly the row's own remainder."""
    monkeypatch.setattr(model_scorecard, "RUNS_ROOT", tmp_path)
    _make_run_dir(tmp_path, "run-abc")

    def _boom(*a, **kw):
        raise model_scorecard.issue_client.IssueQueueUnavailable("library is down")
    monkeypatch.setattr(model_scorecard.issue_client, "list_ledger_entries", _boom)
    calls = []
    monkeypatch.setattr(
        model_scorecard.issue_client, "post_ledger_entry",
        lambda *a, **kw: calls.append(1) or {"status": "recorded", "entry": {}})

    report = model_scorecard.report_to_ledger([_LEDGER_ROW])
    assert calls == []
    assert report["errors"] == [("run-abc", "error:ledger-read-IssueQueueUnavailable",
                                 "library is down")]

    monkeypatch.setattr(model_scorecard.issue_client, "list_ledger_entries",
                        lambda *a, **kw: [])
    report2 = model_scorecard.report_to_ledger([_LEDGER_ROW])
    assert calls == [1]
    assert report2 == {"recorded": 1, "not_recorded": 0, "errors": []}


# --- cross-poster: a refusal recorded by one poster is read by the next (E1·9,
# Clause 6 — the refusal record is the ONE module (ledger.py) all three
# posters share, so this is provoked across the real module boundary rather
# than mocked away) -----------------------------------------------------------


def test_a_refused_finish_post_then_a_sweep_pass_sends_nothing(
        model_scorecard, monkeypatch, tmp_path):
    """agentctl's at-finish post (`ledger.post_run_ledger_entry`) is refused;
    `model-scorecard`'s sweep then sees the SAME remainder for the same run
    and must not resend it. Against code where the two posters' refusal
    records don't share a module, or don't compare the same figure, the stub
    would receive the $0.1234 figure a second time."""
    monkeypatch.setattr(model_scorecard, "RUNS_ROOT", tmp_path)
    run_dir = _make_run_dir(tmp_path, "run-abc")
    monkeypatch.setattr(model_scorecard.issue_client, "list_ledger_entries",
                        lambda *a, **kw: [])
    calls = []
    monkeypatch.setattr(
        model_scorecard.issue_client, "post_ledger_entry",
        lambda *a, **kw: calls.append(1) or {"status": "disproportionate",
                                             "error": "too large", "entry": {}})

    finish_status = model_scorecard.LG.post_run_ledger_entry(
        run_dir, run_id="run-abc", repo="example-product", model="claude-sonnet-5",
        agent="coder", cost_usd=0.1234, is_gateway_model=model_scorecard.is_gateway_model,
        is_codex_model=model_scorecard.is_codex_model)
    assert finish_status == "disproportionate"
    assert calls == [1]
    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["ledger_report_refused_poster"] == "finish"

    report = model_scorecard.report_to_ledger([_LEDGER_ROW])  # same $0.1234
    assert calls == [1]  # unchanged — the sweep sent nothing
    assert report["errors"] == [("run-abc", "skip:refused-unchanged", "too large")]


# --- ledger_post_rows: the Library post widens beyond ledger membership -----
# (is_ledger_member stays link-only; every OTHER finished run in the window
# still reaches the Library, closing the reviewer/scout/planner/architect gap)


def test_ledger_post_rows_includes_a_non_member_finished_run(model_scorecard, monkeypatch, tmp_path):
    """A reviewer-family run with no pr-link.json is not a ledger member
    (`is_ledger_member` stays link-only), but `ledger_post_rows` still names
    it — the coverage gap `report_to_ledger`'s old `fresh`/`finished`-only
    call site never closed."""
    from datetime import datetime, timezone
    monkeypatch.setattr(model_scorecard, "RUNS_ROOT", tmp_path)
    today = datetime.now(timezone.utc)
    run_dir = tmp_path / today.date().isoformat() / "run-reviewer-1"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(json.dumps({
        "run_id": "run-reviewer-1", "agent": "reviewer", "model": "claude-sonnet-5",
        "repo": "widget", "started_at": today.isoformat().replace("+00:00", "Z"),
        "task": "review PR #42",
    }))
    (run_dir / "cost.json").write_text(json.dumps({"usd": 2.5}))

    rows = model_scorecard.ledger_post_rows(14, fresh=[], finished=set())
    assert len(rows) == 1
    assert rows[0]["run_id"] == "run-reviewer-1"
    assert rows[0]["agent"] == "reviewer"
    assert rows[0]["model"] == "claude-sonnet-5"
    assert rows[0]["cost_usd"] == 2.5


def test_ledger_post_rows_carries_the_non_members_own_pr_link(
        model_scorecard, monkeypatch, tmp_path):
    """A non-member row (no `meta.issue`) still carries its own `pr-link.json`
    fields through to `report_to_ledger`'s PR match (E16) — against code that
    only copies `item_id`, these would be absent here."""
    from datetime import datetime, timezone
    monkeypatch.setattr(model_scorecard, "RUNS_ROOT", tmp_path)
    today = datetime.now(timezone.utc)
    run_dir = tmp_path / today.date().isoformat() / "run-reviewer-2"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(json.dumps({
        "run_id": "run-reviewer-2", "agent": "reviewer", "model": "claude-sonnet-5",
        "repo": "widget", "started_at": today.isoformat().replace("+00:00", "Z"),
        "task": "review PR #42",
    }))
    (run_dir / "cost.json").write_text(json.dumps({"usd": 2.5}))
    (run_dir / "pr-link.json").write_text(json.dumps(
        {"pr_url": "https://github.com/convivy/widget/pull/42", "pr_number": 42}))

    rows = model_scorecard.ledger_post_rows(14, fresh=[], finished=set())
    assert len(rows) == 1
    assert rows[0]["pr_number"] == 42
    assert rows[0]["pr_url"] == "https://github.com/convivy/widget/pull/42"


def test_ledger_post_rows_excludes_a_run_still_in_flight(model_scorecard, monkeypatch, tmp_path):
    """No cost.json yet (still running) — never posted, same `is_cost_final`
    contract the ledger-member path already holds."""
    from datetime import datetime, timezone
    monkeypatch.setattr(model_scorecard, "RUNS_ROOT", tmp_path)
    today = datetime.now(timezone.utc)
    run_dir = tmp_path / today.date().isoformat() / "run-in-flight"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(json.dumps({
        "run_id": "run-in-flight", "agent": "reviewer", "model": "claude-sonnet-5",
        "repo": "widget", "started_at": today.isoformat().replace("+00:00", "Z"),
        "task": "review PR #42",
    }))
    rows = model_scorecard.ledger_post_rows(14, fresh=[], finished=set())
    assert rows == []


def test_ledger_post_rows_does_not_duplicate_a_ledger_member(model_scorecard, monkeypatch, tmp_path):
    """A run already covered by `fresh`/`finished` (a ledger member) is not
    also picked up a second time by the widened scan."""
    from datetime import datetime, timezone
    monkeypatch.setattr(model_scorecard, "RUNS_ROOT", tmp_path)
    today = datetime.now(timezone.utc)
    run_dir = tmp_path / today.date().isoformat() / "run-abc"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(json.dumps({
        "run_id": "run-abc", "agent": "coder", "model": "claude-sonnet-5",
        "repo": "widget", "started_at": today.isoformat().replace("+00:00", "Z"),
        "task": "fix a bug",
    }))
    (run_dir / "cost.json").write_text(json.dumps({"usd": 0.1234}))
    rows = model_scorecard.ledger_post_rows(14, fresh=[_LEDGER_ROW], finished={"run-abc"})
    assert [r["run_id"] for r in rows] == ["run-abc"]


def test_main_still_writes_the_local_ledger_when_the_library_is_unreachable(
        model_scorecard, monkeypatch, tmp_path, capsys):
    """The Library write is a side effect of the harvest pass, not a precondition
    for it — an unreachable Library must not stop the local upsert that was
    already decided, and the failure is reported rather than swallowed."""
    monkeypatch.setattr(model_scorecard, "RUNS_ROOT", tmp_path / "agent-runs")
    monkeypatch.setattr(model_scorecard, "harvest", lambda days: ([_LEDGER_ROW], {}, {"run-abc"}))
    monkeypatch.setattr(
        model_scorecard.issue_client, "post_ledger_entry",
        lambda *a, **kw: {"status": "error:OSError", "error": "Library down", "entry": {}})
    ledger_path = tmp_path / "scorecard.jsonl"
    rc = model_scorecard.main(["--ledger", str(ledger_path)])
    assert rc == 0
    rows = model_scorecard.load_ledger(ledger_path)
    assert [r["run_id"] for r in rows] == ["run-abc"]
    err = capsys.readouterr().err
    assert "failed to report" in err and "run-abc" in err and "error:OSError" in err


def test_main_skips_the_ledger_report_on_no_write(model_scorecard, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(model_scorecard, "harvest", lambda days: ([_LEDGER_ROW], {}, {"run-abc"}))
    calls = []
    monkeypatch.setattr(
        model_scorecard.issue_client, "post_ledger_entry",
        lambda *a, **kw: calls.append(1) or {"status": "recorded", "entry": {}})
    ledger_path = tmp_path / "scorecard.jsonl"
    rc = model_scorecard.main(["--ledger", str(ledger_path), "--no-write"])
    assert rc == 0
    assert calls == []
    assert not ledger_path.exists()


# --- is_cost_final + the in-flight ledger-post guard (round 2 security finding) -
# `agentctl spawn --pr N` stamps pr-link.json (and so is_ledger_member) at DISPATCH
# time, before the run has done any work, so a run still in flight is already a
# ledger member with a $0.00 cost_usd placeholder. Posting that under
# dedup_key=run_id would have the real cost, once the run finishes and a later
# pass re-resolves the row, answered by the Library with the existing (wrong)
# entry instead of recorded.


def test_is_cost_final_true_once_cost_json_exists(model_scorecard, tmp_path):
    (tmp_path / "cost.json").write_text('{"usd": 1.23}')
    assert model_scorecard.is_cost_final({"dir": tmp_path}) is True


def test_is_cost_final_false_while_the_run_is_in_flight(model_scorecard, tmp_path):
    (tmp_path / "meta.json").write_text("{}")  # the run exists but hasn't finished
    assert model_scorecard.is_cost_final({"dir": tmp_path}) is False


def test_harvest_prices_a_row_from_the_same_read_that_decided_finality(
        model_scorecard, monkeypatch, tmp_path):
    """Round 2's TOCTOU (round-3 security finding): `list_candidate_runs` used to
    snapshot `run["cost"]` before `harvest`'s loop started, and `is_cost_final`
    later did its own, separately-timed `.exists()` check. A run whose cost.json
    is written (the run finishes) in the window between the two was posted final
    under the stale pre-loop figure. Here the run's cost.json does not exist when
    `list_candidate_runs` scans it, and is written only once `harvest` reaches
    `pr_maps` — after the scan, before this run's row is built — simulating
    exactly that window. Fails against 885d31d7: the posted (and only) row carries
    the $0.00 placeholder instead of the real $4.50, because the pre-fix code
    priced from the pre-loop snapshot rather than re-reading cost.json at the
    point each member is processed."""
    import json as json_mod
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc)
    day_dir = tmp_path / today.date().isoformat()
    run_dir = day_dir / "run-toctou"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(json_mod.dumps({
        "started_at": today.isoformat().replace("+00:00", "Z"),
        "agent": "bug-fixer",
        "model": "claude-sonnet-5",
        "billing_source": "api",
        "repo": "widget",
        "task": "Fix GH issue #99",
    }))
    (run_dir / "pr-link.json").write_text(json_mod.dumps({"pr_number": 99}))
    # No cost.json yet: list_candidate_runs scans this run while it's still in flight.
    monkeypatch.setattr(model_scorecard, "RUNS_ROOT", tmp_path)

    def _pr_maps_writes_cost_mid_harvest(repo, env):
        # The run finishes (cost.json lands) after list_candidate_runs's scan,
        # in the window harvest's loop reaches while resolving this repo's PRs —
        # before it reaches this run's own build_row/is_cost_final step.
        (run_dir / "cost.json").write_text(json_mod.dumps({"usd": 4.50}))
        return {99: {"number": 99, "state": "OPEN", "headRefName": "fix-99", "mergedAt": None}}

    monkeypatch.setattr(model_scorecard, "pr_maps", _pr_maps_writes_cost_mid_harvest)
    monkeypatch.setattr(model_scorecard, "ci_first_pass", lambda repo, branch, env: None)
    monkeypatch.setattr(model_scorecard.GA, "read_token_pool", _fake_read_token_pool)

    rows, skipped, finished = model_scorecard.harvest(days=3)

    assert [r["run_id"] for r in rows] == ["run-toctou"]
    assert finished == {"run-toctou"}  # the run that finished mid-pass is posted
    assert rows[0]["cost_usd"] == 4.50  # priced from cost.json, not the stale $0.00 snapshot


@contextmanager
def _fake_read_token_pool(**kw):
    """Stands in for `GA.read_token_pool` in a `harvest()`/`judge_sample()`
    test that stubs the gh-calling functions it wraps: yields an `env_for`
    that answers every repo with a plain dict rather than reaching the
    install's GitHub App."""
    yield lambda repo_slug: {}


def _write_candidate_run(tmp_path, run_id: str, repo: str, pr_number: int = 1) -> None:
    import json as json_mod
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc)
    day_dir = tmp_path / today.date().isoformat()
    run_dir = day_dir / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(json_mod.dumps({
        "started_at": today.isoformat().replace("+00:00", "Z"),
        "agent": "bug-fixer",
        "model": "claude-sonnet-5",
        "repo": repo,
        "task": "t",
    }))
    (run_dir / "pr-link.json").write_text(json_mod.dumps({"pr_number": pr_number}))
    (run_dir / "cost.json").write_text(json_mod.dumps({"usd": 1.0}))


def test_harvest_refuses_a_forged_qualified_repo(model_scorecard, monkeypatch, tmp_path):
    """Round 2 security finding (PR #881, comment 6152094447): meta.json's
    `repo` is run-writable, and harvest's gh joins built `convivy/<repo>` (or,
    after round 1, repo_github_slug(repo)) unconditionally. Fails against
    43f57916: pr_maps was called with the forged owner/repo string."""
    _write_candidate_run(tmp_path, "run-forged", "acme-attacker/widget")
    monkeypatch.setattr(model_scorecard, "RUNS_ROOT", tmp_path)

    calls = []
    monkeypatch.setattr(model_scorecard, "pr_maps", lambda repo: calls.append(repo) or {})

    rows, skipped, finished = model_scorecard.harvest(days=3)

    assert calls == []  # the forged repo never reached a gh query
    assert [r["run_id"] for r in rows] == ["run-forged"]
    assert rows[0]["pr_state"] == "unresolved"  # degrades like a missing repo


def test_harvest_still_resolves_a_registered_repo_like_homebrew_tap(
        model_scorecard, monkeypatch, tmp_path):
    _write_candidate_run(tmp_path, "run-real", "homebrew-tap")
    monkeypatch.setattr(model_scorecard, "RUNS_ROOT", tmp_path)

    calls = []

    def fake_pr_maps(repo, env):
        calls.append(repo)
        return {1: {"number": 1, "state": "OPEN", "headRefName": "b", "mergedAt": None}}

    monkeypatch.setattr(model_scorecard, "pr_maps", fake_pr_maps)
    monkeypatch.setattr(model_scorecard, "ci_first_pass", lambda repo, branch, env: None)
    monkeypatch.setattr(model_scorecard.GA, "read_token_pool", _fake_read_token_pool)

    rows, skipped, finished = model_scorecard.harvest(days=3)

    assert calls == ["homebrew-tap"]
    assert rows[0]["pr_number"] == 1


def test_main_does_not_post_a_run_still_in_flight(model_scorecard, monkeypatch, tmp_path):
    # ledger_post_rows also scans RUNS_ROOT for every other finished run in the
    # window (the reviewer/scout/planner/architect widening) — redirect it here
    # too, or this test is at the mercy of whatever real run dirs this
    # machine's (or another test's) actual runs root happens to hold.
    monkeypatch.setattr(model_scorecard, "RUNS_ROOT", tmp_path)
    row = dict(_LEDGER_ROW, run_id="run-inflight", cost_usd=0.0)
    monkeypatch.setattr(model_scorecard, "harvest", lambda days: ([row], {}, set()))
    posted = []
    monkeypatch.setattr(
        model_scorecard.issue_client, "post_ledger_entry",
        lambda *a, **kw: posted.append(1) or {"status": "recorded", "entry": {}})
    ledger_path = tmp_path / "scorecard.jsonl"
    rc = model_scorecard.main(["--ledger", str(ledger_path)])
    assert rc == 0
    assert posted == []  # never posted while the run's cost isn't final
    rows = model_scorecard.load_ledger(ledger_path)
    assert [r["run_id"] for r in rows] == ["run-inflight"]  # the local ledger still upserts it


def test_main_does_not_post_a_scan_found_run_still_in_flight(model_scorecard, monkeypatch, tmp_path):
    """The widened scan (`list_candidate_runs`, not `harvest`) finds a
    reviewer-family run with no pr-link.json. Its cost.json is the only field
    that marks a run's cost final (telemetry.write_cost_partial's docstring:
    cost.json's mere presence means "this run is done" to every reader) — a
    run `main` finds this way, with no cost.json yet, must never be posted,
    the same `is_cost_final` contract `ledger_post_rows`'s harvest-sourced
    rows already hold."""
    from datetime import datetime, timezone

    monkeypatch.setattr(model_scorecard, "RUNS_ROOT", tmp_path)
    monkeypatch.setattr(model_scorecard, "harvest", lambda days: ([], {}, set()))
    today = datetime.now(timezone.utc)
    run_dir = tmp_path / today.date().isoformat() / "run-scanned-inflight"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(json.dumps({
        "run_id": "run-scanned-inflight", "agent": "reviewer", "model": "claude-sonnet-5",
        "repo": "widget", "started_at": today.isoformat().replace("+00:00", "Z"),
        "task": "review PR #42",
    }))
    posted = []
    monkeypatch.setattr(
        model_scorecard.issue_client, "post_ledger_entry",
        lambda *a, **kw: posted.append(1) or {"status": "recorded", "entry": {}})
    ledger_path = tmp_path / "scorecard.jsonl"
    rc = model_scorecard.main(["--ledger", str(ledger_path)])
    assert rc == 0
    assert posted == []  # no cost.json yet: never posted


def test_main_posts_a_run_once_it_finishes_with_its_real_cost(model_scorecard, monkeypatch, tmp_path):
    monkeypatch.setattr(model_scorecard, "RUNS_ROOT", tmp_path / "agent-runs")
    row = dict(_LEDGER_ROW, run_id="run-abc", cost_usd=1.23)
    monkeypatch.setattr(model_scorecard, "harvest", lambda days: ([row], {}, {"run-abc"}))
    captured = []
    monkeypatch.setattr(
        model_scorecard.issue_client, "post_ledger_entry",
        lambda product, usd, *, run_id, dedup_key, **kw:
            captured.append((run_id, usd)) or {"status": "recorded", "entry": {}})
    ledger_path = tmp_path / "scorecard.jsonl"
    rc = model_scorecard.main(["--ledger", str(ledger_path)])
    assert rc == 0
    assert captured == [("run-abc", 1.23)]


# --- the GitHub credential seam (company/decisions/0027): every `gh` call
# runs on a token minted from the install's own GitHub App, never the
# operator's `gh` login ------------------------------------------------------

def test_harvest_threads_the_minted_tokens_env_into_pr_maps_and_ci_first_pass(
        model_scorecard, monkeypatch, tmp_path):
    """Drives `harvest`'s real `GA.read_token_pool` (only `GA.installed_app`
    and `GA.mint_token` are stubbed — `GA.gh_env` runs for real): the minted
    token lands in `GH_TOKEN`, and `GH_CONFIG_DIR` is an empty, throwaway
    directory, on every `gh` call `pr_maps`/`ci_first_pass` make for the repo."""
    from pathlib import Path as P

    _write_candidate_run(tmp_path, "run-real", "widget")
    monkeypatch.setattr(model_scorecard, "RUNS_ROOT", tmp_path)
    monkeypatch.setitem(model_scorecard.CI_WORKFLOW, "widget", "ci.yml")
    monkeypatch.setattr(
        model_scorecard.GA, "installed_app",
        lambda: model_scorecard.GA.InstalledApp(app_id="1", key_path=P("/dev/null"), slug=None))

    @contextmanager
    def fake_mint_token(repo_slug, verb, *, key_path, app_id,
                        label="the install's GitHub App", opener=None):
        assert repo_slug == "convivy/widget"
        assert verb == "read"
        yield "ghs_minted_token"

    monkeypatch.setattr(model_scorecard.GA, "mint_token", fake_mint_token)

    captured = []

    def fake_run(argv, **kwargs):
        env = kwargs.get("env")
        assert env["GH_TOKEN"] == "ghs_minted_token"
        config_dir = P(env["GH_CONFIG_DIR"])
        assert config_dir.is_dir()
        assert list(config_dir.iterdir()) == []
        captured.append(env)
        return type("P", (), {"returncode": 0, "stdout": "[]", "stderr": ""})()

    monkeypatch.setattr(model_scorecard.subprocess, "run", fake_run)
    rows, skipped, finished = model_scorecard.harvest(days=3)
    assert len(rows) == 1
    assert captured, "gh must have been called with the minted token's environment"


def test_a_mint_failure_runs_no_gh_process_for_that_repo(
        model_scorecard, monkeypatch, tmp_path, capsys):
    """A repo whose token cannot be minted is never queried: `pr_maps`'s own
    `gh` call must not run, and the failure is named on stderr rather than
    silently resolving to an unlinked PR."""
    _write_candidate_run(tmp_path, "run-real", "widget")
    monkeypatch.setattr(model_scorecard, "RUNS_ROOT", tmp_path)

    class _FailingReadTokenPool:
        def __enter__(self):
            def env_for(repo_slug):
                raise model_scorecard.GA.Unreadable("no App key configured at .../app.pem")
            return env_for

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(model_scorecard.GA, "read_token_pool",
                        lambda **kw: _FailingReadTokenPool())
    calls = []
    monkeypatch.setattr(model_scorecard.subprocess, "run",
                        lambda *a, **k: calls.append((a, k)))

    rows, skipped, finished = model_scorecard.harvest(days=3)
    assert calls == [], "a mint failure must never fall back to running gh"
    assert rows[0]["pr_state"] == "unresolved"
    err = capsys.readouterr().err
    assert "cannot mint a GitHub token for widget" in err
