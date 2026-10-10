"""Pure parse + aggregation logic for bin/warrant-scorecard (ARCH-309).

The risk these guard: the revert-marker parse reads the disseminated convention
(`Reverts: #N` + `Revert-reason: …`) and GitHub's native cross-ref, and the
aggregation turns reverts into the unwarranted-merge (spurious-work) rate. A
mis-parse or a wrong denominator would misstate the warrant axis. gh IO excluded.
"""
from __future__ import annotations


# --- _parse_revert -----------------------------------------------------------


def test_parse_revert_convention(warrant_scorecard):
    assert warrant_scorecard._parse_revert(
        'Revert "Add drift"', "Reverts: #764\nRevert-reason: unwarranted") == (764, "unwarranted")


def test_parse_revert_github_native_no_reason_is_unclassified(warrant_scorecard):
    # A detected revert with no reason line is recorded, not dropped.
    assert warrant_scorecard._parse_revert("Revert the thing", "Reverts #764") == (764, "unclassified")


def test_parse_revert_unknown_reason_falls_to_other(warrant_scorecard):
    assert warrant_scorecard._parse_revert("x", "Reverts: #99\nRevert-reason: nonsense") == (99, "other")


def test_parse_revert_bug_reason(warrant_scorecard):
    assert warrant_scorecard._parse_revert("x", "Reverts: #712\nRevert-reason: bug") == (712, "bug")


def test_parse_revert_non_revert_is_none(warrant_scorecard):
    # A plain reference to a PR ("see #764") is not a revert — needs the Reverts marker.
    assert warrant_scorecard._parse_revert("A feature", "Adds a widget. See #764.") == (None, "")
    # singular "revert" (not the "Reverts" cross-ref form) doesn't false-match
    assert warrant_scorecard._parse_revert("x", "this does not revert #764")[0] is None


# --- build_report ------------------------------------------------------------


def _reverts():
    # PR 764 (sonnet) reverted as unwarranted; PR 712 (sonnet) reverted as a bug;
    # PR 500 (deepseek) reverted, reason unclassified.
    return [
        {"repo": "vivy", "original_pr": 764, "revert_pr": 765, "reason": "unwarranted",
         "producer_model": "claude-sonnet-4-6", "revert_state": "MERGED"},
        {"repo": "vivy", "original_pr": 712, "revert_pr": 766, "reason": "bug",
         "producer_model": "claude-sonnet-4-6", "revert_state": "MERGED"},
        {"repo": "constance", "original_pr": 500, "revert_pr": 501, "reason": "unclassified",
         "producer_model": "deepseek/deepseek-chat", "revert_state": "MERGED"},
    ]


def _fake_merged(monkeypatch, warrant_scorecard):
    # 10 sonnet vivy PRs (incl. 764, 712) + 5 deepseek constance PRs (incl. 500).
    merged = {
        "vivy": {n: {"producer_model": "claude-sonnet-4-6", "merged_at": "2026-07-01T00:00:00Z",
                     "producer_agent": "coder"} for n in [764, 712] + list(range(701, 709))},
        "constance": {n: {"producer_model": "deepseek/deepseek-chat", "merged_at": "2026-07-01T00:00:00Z",
                          "producer_agent": "coder"} for n in [500] + list(range(501, 505))},
    }
    monkeypatch.setattr(warrant_scorecard, "merged_agent_prs", lambda days: merged)


def test_build_report_unwarranted_rate_is_headline(warrant_scorecard, monkeypatch):
    _fake_merged(monkeypatch, warrant_scorecard)
    rep = warrant_scorecard.build_report(_reverts(), 90)
    o = rep["overall"]
    assert o["merged_prs"] == 15                     # 10 vivy + 5 constance
    assert o["reverted"] == 3
    assert o["unwarranted"] == 1                      # only 764
    assert o["unwarranted_rate"] == round(100 * 1 / 15, 1)
    assert o["bug_reverts"] == 1                      # only 712
    assert o["by_reason"] == {"unwarranted": 1, "bug": 1, "unclassified": 1}


def test_build_report_by_model_isolates_the_offender(warrant_scorecard, monkeypatch):
    _fake_merged(monkeypatch, warrant_scorecard)
    bm = warrant_scorecard.build_report(_reverts(), 90)["by_model"]
    # sonnet: 10 merged, 1 unwarranted (764) + 1 bug (712)
    assert bm["claude-sonnet-4-6"]["merged_prs"] == 10
    assert bm["claude-sonnet-4-6"]["unwarranted"] == 1
    assert bm["claude-sonnet-4-6"]["bug_reverts"] == 1
    # deepseek: 5 merged, 0 unwarranted (its revert is unclassified)
    assert bm["deepseek/deepseek-chat"]["unwarranted"] == 0
    assert bm["deepseek/deepseek-chat"]["reverted"] == 1


def test_build_report_most_severe_reason_wins_per_pr(warrant_scorecard, monkeypatch):
    _fake_merged(monkeypatch, warrant_scorecard)
    # PR 764 reverted twice — once bug, once unwarranted; unwarranted (more severe) wins.
    reverts = [
        {"repo": "vivy", "original_pr": 764, "revert_pr": 765, "reason": "bug",
         "producer_model": "claude-sonnet-4-6", "revert_state": "MERGED"},
        {"repo": "vivy", "original_pr": 764, "revert_pr": 767, "reason": "unwarranted",
         "producer_model": "claude-sonnet-4-6", "revert_state": "MERGED"},
    ]
    rep = warrant_scorecard.build_report(reverts, 90)
    assert rep["overall"]["unwarranted"] == 1
    assert rep["overall"]["reverted"] == 1            # the same PR, counted once
    assert rep["overall"]["bug_reverts"] == 0


def test_build_report_open_revert_is_not_counted(warrant_scorecard, monkeypatch):
    _fake_merged(monkeypatch, warrant_scorecard)
    # PR 764 has an OPEN revert PR — a proposal, not yet an undo. It must not
    # register until that revert actually merges (the #773-still-open case).
    reverts = [
        {"repo": "vivy", "original_pr": 764, "revert_pr": 765, "reason": "unwarranted",
         "producer_model": "claude-sonnet-4-6", "revert_state": "OPEN"},
    ]
    rep = warrant_scorecard.build_report(reverts, 90)
    assert rep["overall"]["reverted"] == 0
    assert rep["overall"]["unwarranted"] == 0
    assert rep["overall"]["unwarranted_rate"] == 0.0


def test_build_report_empty(warrant_scorecard, monkeypatch):
    monkeypatch.setattr(warrant_scorecard, "merged_agent_prs", lambda days: {})
    rep = warrant_scorecard.build_report([], 90)
    assert rep["overall"]["merged_prs"] == 0
    assert rep["overall"]["unwarranted_rate"] == 0.0
