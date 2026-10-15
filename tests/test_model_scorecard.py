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
from pathlib import Path


# --- is_close_loop -------------------------------------------------------------


def test_is_close_loop_true_for_close_loop_task(model_scorecard):
    meta = {"agent": "bug-fixer",
            "task": "CLOSE-LOOP MODE only — no code. Bug fixed in https://github.com/convivy/vivy/pull/436"}
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
         usd=0.01, repo="vivy", started="2026-06-02T08:00:00Z"):
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
    assert model_scorecard._references_pr("convivy/vivy/pull/441", 441) is True


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


def _mrow(run_id, agent, model, date, pr, repo="vivy", merged=True):
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
    row = {"pr_number": 441, "repo": "vivy", "agent": "bug-fixer", "model": "deepseek/deepseek-v4-flash"}
    ctx = {"task": "Fix formatUSD", "title": "fix: formatUSD", "reviews": "@bot: LGTM", "diff": "- a\n+ b"}
    pr = model_scorecard.build_judge_prompt(row, ctx)
    assert "PR #441" in pr and "Fix formatUSD" in pr and "review_thoroughness" in pr
    assert "Output ONLY a JSON object" in pr


def test_build_judge_prompt_names_the_registered_owner_for_homebrew_tap(model_scorecard):
    """Round-1 review (PR #881, comment 6150937461): this label built
    `convivy/{repo}` directly — the same pattern the PR centralizes
    everywhere else."""
    row = {"pr_number": 1, "repo": "homebrew-tap", "agent": "bug-fixer", "model": "sonnet"}
    ctx = {"task": "t", "title": "t", "reviews": "", "diff": "d"}
    pr = model_scorecard.build_judge_prompt(row, ctx)
    assert "convivybuilder/homebrew-tap" in pr


# --- gh-calling slug sites: the registered owner, not always convivy --------


def test_pr_maps_queries_the_registered_owner_for_homebrew_tap(model_scorecard, monkeypatch):
    captured = {}

    def fake_gh_json(args):
        captured["args"] = args
        return []

    monkeypatch.setattr(model_scorecard, "_gh_json", fake_gh_json)
    model_scorecard.pr_maps("homebrew-tap")

    assert captured["args"] and "--repo" in captured["args"]
    repo_arg = captured["args"][captured["args"].index("--repo") + 1]
    assert repo_arg == "convivybuilder/homebrew-tap"


def test_ci_first_pass_queries_the_registered_owner_for_homebrew_tap(model_scorecard, monkeypatch):
    monkeypatch.setitem(model_scorecard.CI_WORKFLOW, "homebrew-tap", "ci.yml")
    captured = {}

    def fake_gh_json(args):
        captured["args"] = args
        return []

    monkeypatch.setattr(model_scorecard, "_gh_json", fake_gh_json)
    model_scorecard.ci_first_pass("homebrew-tap", "a-branch")

    assert captured["args"] and "--repo" in captured["args"]
    repo_arg = captured["args"][captured["args"].index("--repo") + 1]
    assert repo_arg == "convivybuilder/homebrew-tap"


def test_was_reverted_queries_the_registered_owner_for_homebrew_tap(model_scorecard, monkeypatch):
    captured = {}

    def fake_gh_json(args):
        captured["args"] = args
        return []

    monkeypatch.setattr(model_scorecard, "_gh_json", fake_gh_json)
    model_scorecard.was_reverted("homebrew-tap", 1, "2026-05-20T18:34:53Z")

    assert captured["args"] and "--repo" in captured["args"]
    repo_arg = captured["args"][captured["args"].index("--repo") + 1]
    assert repo_arg == "convivybuilder/homebrew-tap"


def test_had_follow_up_bug_queries_the_registered_owner_for_homebrew_tap(
        model_scorecard, monkeypatch):
    captured = {}

    def fake_gh_json(args):
        captured["args"] = args
        return []

    monkeypatch.setattr(model_scorecard, "_gh_json", fake_gh_json)
    model_scorecard.had_follow_up_bug("homebrew-tap", 1, "2026-05-20T18:34:53Z")

    assert captured["args"] and "--repo" in captured["args"]
    repo_arg = captured["args"][captured["args"].index("--repo") + 1]
    assert repo_arg == "convivybuilder/homebrew-tap"


def test_fetch_pr_context_queries_the_registered_owner_for_homebrew_tap(
        model_scorecard, monkeypatch):
    captured = []

    def fake_gh_text(args):
        captured.append(args)
        return "diff"

    def fake_gh_json(args):
        captured.append(args)
        return {}

    monkeypatch.setattr(model_scorecard, "_gh_text", fake_gh_text)
    monkeypatch.setattr(model_scorecard, "_gh_json", fake_gh_json)
    model_scorecard.fetch_pr_context("homebrew-tap", 1, "task")

    assert captured and all("--repo" in c for c in captured)
    for c in captured:
        assert c[c.index("--repo") + 1] == "convivybuilder/homebrew-tap"


def test_harvest_repo_prs_queries_the_registered_owner_for_homebrew_tap(
        model_scorecard, monkeypatch):
    captured = {}

    def fake_gh_json(args):
        captured["args"] = args
        return []

    monkeypatch.setattr(model_scorecard, "_gh_json", fake_gh_json)
    model_scorecard.harvest_repo_prs("homebrew-tap", 30, "shipwright")

    assert captured["args"] and "--repo" in captured["args"]
    repo_arg = captured["args"][captured["args"].index("--repo") + 1]
    assert repo_arg == "convivybuilder/homebrew-tap"


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


# --- GitHub-only repo scorecard (telemetry-less crews, e.g. PA) --------------


def test_build_repo_row_synthetic_run_id_and_no_cost(model_scorecard):
    pr = {"number": 36, "state": "MERGED", "mergedAt": "2026-06-02T10:00:00Z",
          "createdAt": "2026-06-01T09:00:00Z", "title": "Add ledger",
          "author": {"login": "convivy-bot"}}
    row = model_scorecard.build_repo_row(pr, "personal-assistant", "personal-assistant",
                                         ci_first_pass=True, survived=None)
    assert row["run_id"] == "personal-assistant#36"  # synthetic, for upsert keying
    assert row["agent"] == "personal-assistant" and row["model"] is None
    assert row["cost_usd"] is None and row["source"] == "github-pr"
    assert row["merged"] is True and row["date"] == "2026-06-01"
    assert row["ci_first_pass"] is True and row["author"] == "convivy-bot"


def test_build_repo_row_reads_model_label(model_scorecard):
    # A telemetry-less repo stamps the producing model as a `model:` PR label (ARCH-169).
    pr = {"number": 149, "state": "MERGED", "mergedAt": "2026-06-15T10:00:00Z",
          "createdAt": "2026-06-15T09:00:00Z", "title": "feat",
          "labels": [{"name": "enhancement"}, {"name": "model:claude-sonnet-4-6"}]}
    row = model_scorecard.build_repo_row(pr, "personal-assistant", "personal-assistant",
                                         ci_first_pass=True, survived=None)
    assert row["model"] == "claude-sonnet-4-6"


def test_model_from_labels_variants(model_scorecard):
    f = model_scorecard._model_from_labels
    assert f({"labels": [{"name": "model:claude-opus-4-8"}]}) == "claude-opus-4-8"
    assert f({"labels": [{"name": "bug"}]}) is None
    assert f({}) is None
    assert f({"labels": []}) is None
    # more than one model: label → deterministic first-sorted pick
    assert f({"labels": [{"name": "model:gpt-5"}, {"name": "model:claude-opus-4-8"}]}) \
        == "claude-opus-4-8"


def test_build_repo_row_closed_unmerged(model_scorecard):
    pr = {"number": 5, "state": "CLOSED", "mergedAt": None,
          "createdAt": "2026-06-01T09:00:00Z", "title": "Abandoned"}
    row = model_scorecard.build_repo_row(pr, "personal-assistant", "pa-crew", None, None)
    assert row["merged"] is False and row["pr_state"] == "CLOSED"


def test_repo_rows_upsert_and_summarize_for_merge_rate(model_scorecard):
    # 3 decided PRs (2 merged, 1 closed) → merge% 67% over decided; reuses summarize()
    rows = [
        model_scorecard.build_repo_row(
            {"number": n, "state": s, "mergedAt": ("2026-06-02T10:00:00Z" if s == "MERGED" else None),
             "createdAt": "2026-06-01T09:00:00Z", "title": "t"},
            "personal-assistant", "personal-assistant", ci, None)
        for n, s, ci in [(1, "MERGED", True), (2, "MERGED", False), (3, "CLOSED", None)]
    ]
    a = model_scorecard.summarize(rows)[("personal-assistant", None)]
    assert a["resolved"] == 3 and a["merged"] == 2     # merge% = 2/3
    assert a["ci_runs"] == 2 and a["ci_first_pass"] == 1
    out = model_scorecard.render_repo_report(rows)
    assert "67%" in out and "| personal-assistant |" in out


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
# A PR without a `model:<id>` label aggregates under (agent, None) beside
# (agent, "<model>"); a bare sorted() over those keys raised
# "'<' not supported between 'str' and 'NoneType'" and killed the daily
# com.convivy.model-scorecard-hollis run.


def _mixed_model_repo_rows(model_scorecard):
    def row(n, model):
        r = model_scorecard.build_repo_row(
            {"number": n, "state": "MERGED", "mergedAt": "2026-06-02T10:00:00Z",
             "createdAt": "2026-06-01T09:00:00Z", "title": "t"},
            "hollis", "hollis", True, None)
        r["model"] = model
        return r
    return [row(1, "deepseek/deepseek-v4-flash"), row(2, None), row(3, "anthropic/claude-sonnet-5")]


def test_render_repo_report_sorts_none_model_beside_str_model(model_scorecard):
    rows = _mixed_model_repo_rows(model_scorecard)
    out = model_scorecard.render_repo_report(rows)
    assert out.count("| hollis |") == 3
    assert model_scorecard.render_repo_report(list(reversed(rows))) == out  # order stable


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
    "model": "claude-sonnet-5", "repo": "vivy", "cost_usd": 0.1234,
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


def test_ledger_lane_none_for_a_codex_model(model_scorecard):
    """Codex runs bill at OpenAI's API rates (telemetry.CODEX_BILLING_SOURCE),
    neither the gateway's metered lane nor the Anthropic subscription — against
    an implementation that falls through to "subscription" for anything that
    isn't a gateway model, this would read "subscription" instead of None."""
    assert model_scorecard.ledger_lane("codex-auto-review") is None
    assert model_scorecard.ledger_lane("gpt-5.5") is None


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
    assert captured == [{"product": "vivy", "usd": 0.1234, "run_id": "run-abc",
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


def test_report_to_ledger_skips_a_row_with_no_determinable_lane(model_scorecard, monkeypatch):
    """A Codex model (is_codex_model) has no ledger lane — it is billed at
    OpenAI's API rates, neither the gateway's metered lane nor the Anthropic
    subscription — so the row is never posted. Against the unfixed code
    (which always calls post_ledger_entry), `calls` below would be non-empty."""
    calls = []
    monkeypatch.setattr(model_scorecard.issue_client, "post_ledger_entry",
                        lambda *a, **kw: calls.append(1) or {"status": "recorded", "entry": {}})
    row = dict(_LEDGER_ROW, run_id="run-codex", model="codex-auto-review")
    report = model_scorecard.report_to_ledger([row])
    assert calls == []
    assert report["recorded"] == 0
    assert report["errors"] == [("run-codex", "skip:no-lane",
                                 "no ledger lane for model 'codex-auto-review'")]


def test_report_to_ledger_skips_a_row_with_no_model(model_scorecard, monkeypatch):
    calls = []
    monkeypatch.setattr(model_scorecard.issue_client, "post_ledger_entry",
                        lambda *a, **kw: calls.append(1) or {"status": "recorded", "entry": {}})
    row = dict(_LEDGER_ROW, run_id="run-no-model", model=None)
    report = model_scorecard.report_to_ledger([row])
    assert calls == []
    assert report["errors"] == [("run-no-model", "skip:no-lane", "no ledger lane for model None")]


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
    meta = {"run_id": run_id, "agent": "coder", "task": "t", "repo": "vivy",
            "worktree": "/wt", "profile": "p", "model": "claude-sonnet-5",
            "started_at": "2026-10-13T00:00:00Z", "pid": None}
    meta.update(meta_extra or {})
    (rd / "meta.json").write_text(json.dumps(meta))
    return rd


def test_report_to_ledger_marks_a_disproportionate_refusal_on_the_run(
        model_scorecard, monkeypatch, tmp_path):
    """Against the unfixed code (no write back to the run at all), the run dir's
    meta.json never gains `ledger_report_refused_usd`/`_reason`, so the second
    assertion below fails."""
    monkeypatch.setattr(model_scorecard, "RUNS_ROOT", tmp_path)
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


def test_report_to_ledger_does_not_resend_an_unchanged_refused_figure(
        model_scorecard, monkeypatch, tmp_path):
    """Clause 6: a refused report is surfaced against its run and not resubmitted
    unchanged. Against the unfixed code (no pre-post check against the run's own
    mark), the stub below would receive a second call for the same $0.1234 figure
    — `calls` would be 2, not 1."""
    monkeypatch.setattr(model_scorecard, "RUNS_ROOT", tmp_path)
    _make_run_dir(tmp_path, "run-abc", meta_extra={
        "ledger_report_refused_usd": 0.1234,
        "ledger_report_refused_reason": "this report exceeds 10x the scope's soft cap",
    })
    calls = []
    monkeypatch.setattr(
        model_scorecard.issue_client, "post_ledger_entry",
        lambda *a, **kw: calls.append(1) or {"status": "disproportionate",
                                             "error": "x", "entry": {}})
    report = model_scorecard.report_to_ledger([_LEDGER_ROW])
    assert calls == []
    assert report["errors"] == [("run-abc", "skip:refused-unchanged",
                                 "this report exceeds 10x the scope's soft cap")]


def test_report_to_ledger_reposts_a_corrected_figure(model_scorecard, monkeypatch, tmp_path):
    """A later pass with a DIFFERENT figure for the same run (its cost was
    corrected) posts again — against an implementation that keys the skip on
    run_id alone rather than the figure, the stub below would never be called
    and `calls` would stay empty."""
    monkeypatch.setattr(model_scorecard, "RUNS_ROOT", tmp_path)
    _make_run_dir(tmp_path, "run-abc", meta_extra={
        "ledger_report_refused_usd": 999.0,
        "ledger_report_refused_reason": "this report exceeds 10x the scope's soft cap",
    })
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
        "repo": "vivy",
        "task": "Fix GH issue #99",
    }))
    (run_dir / "pr-link.json").write_text(json_mod.dumps({"pr_number": 99}))
    # No cost.json yet: list_candidate_runs scans this run while it's still in flight.
    monkeypatch.setattr(model_scorecard, "RUNS_ROOT", tmp_path)

    def _pr_maps_writes_cost_mid_harvest(repo):
        # The run finishes (cost.json lands) after list_candidate_runs's scan,
        # in the window harvest's loop reaches while resolving this repo's PRs —
        # before it reaches this run's own build_row/is_cost_final step.
        (run_dir / "cost.json").write_text(json_mod.dumps({"usd": 4.50}))
        return {99: {"number": 99, "state": "OPEN", "headRefName": "fix-99", "mergedAt": None}}

    monkeypatch.setattr(model_scorecard, "pr_maps", _pr_maps_writes_cost_mid_harvest)
    monkeypatch.setattr(model_scorecard, "ci_first_pass", lambda repo, branch: None)

    rows, skipped, finished = model_scorecard.harvest(days=3)

    assert [r["run_id"] for r in rows] == ["run-toctou"]
    assert finished == {"run-toctou"}  # the run that finished mid-pass is posted
    assert rows[0]["cost_usd"] == 4.50  # priced from cost.json, not the stale $0.00 snapshot


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

    def fake_pr_maps(repo):
        calls.append(repo)
        return {1: {"number": 1, "state": "OPEN", "headRefName": "b", "mergedAt": None}}

    monkeypatch.setattr(model_scorecard, "pr_maps", fake_pr_maps)
    monkeypatch.setattr(model_scorecard, "ci_first_pass", lambda repo, branch: None)

    rows, skipped, finished = model_scorecard.harvest(days=3)

    assert calls == ["homebrew-tap"]
    assert rows[0]["pr_number"] == 1


def test_main_does_not_post_a_run_still_in_flight(model_scorecard, monkeypatch, tmp_path):
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


def test_main_posts_a_run_once_it_finishes_with_its_real_cost(model_scorecard, monkeypatch, tmp_path):
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
