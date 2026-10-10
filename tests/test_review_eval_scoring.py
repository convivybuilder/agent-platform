"""lib/agent-platform/review_eval_scoring.py — the review-replay eval
harness's scoring (agent-platform#807). The judge is always stubbed here;
every finding/diff string is synthetic.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib" / "agent-platform"))
import review_eval_scoring as RSC  # noqa: E402


def _exact_match_fn(candidate, key):
    for i, k in enumerate(key):
        if k == candidate:
            return i, "exact text match"
    return None, "no key item matches"


def _always_real_judge(finding, diff_text):
    return True, "stub: always real"


def _always_false_judge(finding, diff_text):
    return False, "stub: always false"


# --- match_findings / judge_unmatched -----------------------------------------


def test_match_findings_records_a_reason_for_every_decision():
    matches = RSC.match_findings(
        ["synthetic finding A", "synthetic finding B — unmatched"],
        ["synthetic finding A"], match_fn=_exact_match_fn)
    assert matches[0].matched_key_index == 0 and matches[0].reason == "exact text match"
    assert matches[1].matched_key_index is None


def test_judge_unmatched_classifies_every_finding():
    judged = RSC.judge_unmatched(["synthetic extra finding"], "synthetic diff",
                                 judge_fn=_always_real_judge)
    assert len(judged) == 1 and judged[0].real is True
    assert judged[0].reason == "stub: always real"


# --- EntryScore / score_entry --------------------------------------------------


def test_score_entry_recall_counts_unique_key_items_matched():
    # Two candidate findings both matching the SAME key item must not inflate
    # recall past 1/1.
    def match_fn(candidate, key):
        return (0, "matches key 0") if "A" in candidate else (None, "no match")

    score = RSC.score_entry("e1", ["finding A v1", "finding A v2", "unrelated"],
                            ["synthetic key finding A"], "synthetic diff",
                            match_fn=match_fn, judge_fn=_always_false_judge)
    assert score.recall == 1.0
    assert score.unmatched_findings == ["unrelated"]
    assert score.judged_false_count == 1 and score.judged_real_count == 0


def test_score_entry_recall_is_none_for_an_empty_answer_key():
    score = RSC.score_entry("e1", ["an extra finding"], [], "synthetic diff",
                            match_fn=_exact_match_fn, judge_fn=_always_real_judge)
    assert score.recall is None
    assert score.judged_real_count == 1


def test_score_entry_recall_zero_when_nothing_matches():
    score = RSC.score_entry("e1", [], ["synthetic key finding"], "synthetic diff",
                            match_fn=_exact_match_fn, judge_fn=_always_real_judge)
    assert score.recall == 0.0


# --- aggregate_recall ----------------------------------------------------------


def test_aggregate_recall_skips_entries_with_no_defined_recall():
    scores = [
        RSC.EntryScore(entry_id="e1", answer_key_size=2,
                      matches=[RSC.MatchResult("f", 0, "r"), RSC.MatchResult("f", 1, "r")]),
        RSC.EntryScore(entry_id="e2", answer_key_size=0),  # clean round: no key
    ]
    assert RSC.aggregate_recall(scores) == 1.0


def test_aggregate_recall_none_when_every_entry_has_no_key():
    scores = [RSC.EntryScore(entry_id="e1", answer_key_size=0)]
    assert RSC.aggregate_recall(scores) is None


# --- noise floor band ----------------------------------------------------------


def test_noise_floor_band_spans_the_given_recalls():
    band = RSC.noise_floor_band([0.6, 0.8, 0.7])
    assert band["low"] == 0.6 and band["high"] == 0.8
    assert abs(band["mid"] - 0.7) < 1e-9


def test_within_band_respects_low_and_high_with_no_slack():
    band = {"low": 0.6, "high": 0.8, "mid": 0.7}
    assert RSC.within_band(0.6, band) is True
    assert RSC.within_band(0.8, band) is True
    assert RSC.within_band(0.59, band) is False
    assert RSC.within_band(0.81, band) is False


def test_within_band_slack_widens_both_sides():
    band = {"low": 0.6, "high": 0.8, "mid": 0.7}
    assert RSC.within_band(0.55, band, slack=0.05) is True
    assert RSC.within_band(0.85, band, slack=0.05) is True
    assert RSC.within_band(0.54, band, slack=0.05) is False


# --- verdict_agreement ----------------------------------------------------------


def test_verdict_agreement_true_when_equal():
    assert RSC.verdict_agreement("APPROVE", "APPROVE") is True


def test_verdict_agreement_false_when_different():
    assert RSC.verdict_agreement("APPROVE", "REQUEST_CHANGES") is False


def test_verdict_agreement_none_when_candidate_produced_no_verdict():
    assert RSC.verdict_agreement(None, "APPROVE") is None


# --- build_model_report / report rendering -------------------------------------


def test_build_model_report_counts_a_failed_replay_as_zero_recall_but_still_costs():
    # One scored entry at recall 0 (the candidate found nothing), one replay
    # result dict that errored but still carries real cost.
    scores = [RSC.EntryScore(entry_id="e1", answer_key_size=1, matches=[])]
    replay_results = [
        {"usd": 0.10, "tokens_in": 1000, "tokens_out": 50, "error": "wait exit 4"},
    ]
    report = RSC.build_model_report("deepseek", scores, [None], replay_results)
    assert report.recall_confirmed == 0.0
    assert report.usd_total == 0.10
    assert report.runs_errored == 1
    assert report.runs_not_run == 0


def test_build_model_report_not_run_entries_are_not_counted_as_errors():
    scores = []
    replay_results = [{"usd": 0.0, "tokens_in": 0, "tokens_out": 0, "not_run": True,
                       "reason": "deepseek reached its spend cap"}]
    report = RSC.build_model_report("deepseek", scores, [], replay_results)
    assert report.runs_not_run == 1 and report.runs_errored == 0


def test_sample_for_audit_splits_real_and_false_and_bounds_each():
    judged = []
    for i in range(15):
        judged.append(("deepseek", f"e{i}", RSC.JudgedFinding(f"real {i}", True, "r"),
                       f"diff {i}"))
    for i in range(3):
        judged.append(("kimi", f"f{i}", RSC.JudgedFinding(f"false {i}", False, "r"),
                       f"diff {i}"))

    sample = RSC.sample_for_audit(judged, n=10, seed=5)

    assert len(sample["judged_real"]) == 10  # bounded even though 15 are real
    assert len(sample["judged_false"]) == 3  # all of them, since there are only 3
    assert all(r["reason"] == "r" and "diff_hunk" in r for r in sample["judged_real"])

    # deterministic under the same seed
    again = RSC.sample_for_audit(judged, n=10, seed=5)
    assert sample == again


def test_render_markdown_table_has_a_row_per_model_and_handles_undefined_recall():
    reports = [
        RSC.ModelReport(model_key="sonnet", recall_confirmed=0.8, judged_real_extra=1,
                        judged_false_extra=2, verdict_agreement_rate=0.9, usd_total=1.5,
                        tokens_in_total=1000, tokens_out_total=100, runs_total=10,
                        runs_errored=0, runs_not_run=0),
        RSC.ModelReport(model_key="deepseek", recall_confirmed=None, judged_real_extra=0,
                        judged_false_extra=0, verdict_agreement_rate=None, usd_total=0.0,
                        tokens_in_total=0, tokens_out_total=0, runs_total=0,
                        runs_errored=0, runs_not_run=5),
    ]
    markdown, payload = RSC.build_report(reports)
    assert "sonnet" in markdown and "deepseek" in markdown
    assert "80%" in markdown
    assert "—" in markdown  # undefined recall/agreement render as a dash, not "None"
    assert payload["models"][0]["model"] == "sonnet"
    assert payload["models"][1]["runs_not_run"] == 5
