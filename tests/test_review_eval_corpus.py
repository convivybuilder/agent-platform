"""lib/agent-platform/review_eval_corpus.py — the review-replay eval harness's
corpus builder (agent-platform#807).

Every run record here is synthetic: no real review text or PR content, per
the harness's own test rule. Selection logic is pure and network-free, so
these drive it directly against hand-built run directories; every test of
`build_corpus` passes its own stub extractor rather than exercising the one
model call in the module (`default_finding_extractor`) — that function's own
`subprocess.run` call is stubbed directly, below, to check its env.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib" / "agent-platform"))
import review_eval_corpus as REC  # noqa: E402


def _make_run(root: Path, date: str, run_id: str, *, agent: str, repo: str,
             pr=None, review_sha="", review_verdict="", started_at="",
             review_pr=None, with_prompts=True, with_body=True,
             body_text="**Comment by Claude (reviewer):**\n\nNo findings.\n",
             tokens_in=1000, tokens_out=50, usd=0.01) -> Path:
    run_dir = root / date / run_id
    run_dir.mkdir(parents=True)
    meta = {"run_id": run_id, "agent": agent, "repo": repo, "pr": pr,
           "review_sha": review_sha, "review_verdict": review_verdict,
           "started_at": started_at or f"{date}T00:00:00Z"}
    if review_pr is not None:
        meta["review_pr"] = review_pr
    (run_dir / "meta.json").write_text(json.dumps(meta))
    (run_dir / "cost.json").write_text(json.dumps(
        {"tokens_in": tokens_in, "tokens_out": tokens_out, "usd": usd}))
    if with_prompts:
        (run_dir / "prompts").mkdir()
        (run_dir / "prompts" / "system.md").write_text("You review diffs.\n")
        (run_dir / "prompts" / "user.md").write_text(f"Review {repo}#{pr}.\n")
    if with_body:
        (run_dir / "unposted-review.md").write_text(
            "<!-- agentctl-rescued-review: not posted by its own run -->\n"
            f"# Review written by {run_id} and never posted\n"
            f"Reviewed commit: {review_sha}\n\n"
            "Written by review-post itself, before a wait that might outlive it.\n\n"
            f"{body_text}")
    return run_dir


# --- scan_review_records -----------------------------------------------------


def test_scan_review_records_filters_to_posted_review_class_runs(tmp_path):
    _make_run(tmp_path, "2026-09-21", "run-a", agent="reviewer", repo="agent-platform",
             pr="1", review_sha="a" * 40, review_verdict="APPROVE", started_at="2026-09-21T01:00:00Z")
    # wrong agent — excluded
    _make_run(tmp_path, "2026-09-21", "run-b", agent="coder", repo="agent-platform",
             pr="1", review_sha="b" * 40, review_verdict="APPROVE")
    # never posted (no review_verdict) — excluded
    _make_run(tmp_path, "2026-09-21", "run-c", agent="reviewer", repo="agent-platform",
             pr="2", review_sha="c" * 40, review_verdict="")
    # before the since_date — excluded
    _make_run(tmp_path, "2026-09-10", "run-d", agent="reviewer", repo="agent-platform",
             pr="3", review_sha="d" * 40, review_verdict="APPROVE")
    # crew-prefixed reviewer — included, role normalized to the base
    _make_run(tmp_path, "2026-09-22", "run-e", agent="hollis-reviewer", repo="hollis",
             pr="9", review_sha="e" * 40, review_verdict="REQUEST_CHANGES")

    records = REC.scan_review_records(tmp_path, since_date="2026-09-20")

    ids = {r.run_id for r in records}
    assert ids == {"run-a", "run-e"}
    by_id = {r.run_id: r for r in records}
    assert by_id["run-e"].role == "reviewer"


# --- find_confirmed_rounds ---------------------------------------------------


def test_confirmed_round_needs_a_later_approve_at_a_different_head():
    base = dict(run_dir=Path("/x"), repo="agent-platform", pr="1", role="reviewer")
    rc_fixed = REC.ReviewRecord(run_id="r1", sha="s1", verdict="REQUEST_CHANGES",
                                started_at="2026-09-21T00:00:00Z", **base)
    approve_new_head = REC.ReviewRecord(run_id="r2", sha="s2", verdict="APPROVE",
                                        started_at="2026-09-21T01:00:00Z", **base)
    confirmed = REC.find_confirmed_rounds([rc_fixed, approve_new_head])
    assert confirmed == [rc_fixed]


def test_confirmed_round_excludes_approve_at_the_same_head():
    """A REQUEST_CHANGES followed by an APPROVE on the SAME sha means nothing
    changed between the two reviews (a re-review with no new commit) — not a
    confirmed fix."""
    base = dict(run_dir=Path("/x"), repo="agent-platform", pr="1", role="reviewer")
    rc = REC.ReviewRecord(run_id="r1", sha="s1", verdict="REQUEST_CHANGES",
                          started_at="2026-09-21T00:00:00Z", **base)
    approve_same_head = REC.ReviewRecord(run_id="r2", sha="s1", verdict="APPROVE",
                                         started_at="2026-09-21T01:00:00Z", **base)
    assert REC.find_confirmed_rounds([rc, approve_same_head]) == []


def test_confirmed_round_excludes_a_request_changes_with_no_later_review():
    base = dict(run_dir=Path("/x"), repo="agent-platform", pr="1", role="reviewer")
    rc = REC.ReviewRecord(run_id="r1", sha="s1", verdict="REQUEST_CHANGES",
                          started_at="2026-09-21T00:00:00Z", **base)
    assert REC.find_confirmed_rounds([rc]) == []


def test_confirmed_round_is_scoped_to_the_same_pr_and_role():
    """A REQUEST_CHANGES on PR 1 followed by an unrelated APPROVE on PR 2 (or
    by security-reviewer, not reviewer) must not be read as confirming it."""
    rc = REC.ReviewRecord(run_id="r1", run_dir=Path("/x"), repo="agent-platform",
                          pr="1", role="reviewer", sha="s1", verdict="REQUEST_CHANGES",
                          started_at="2026-09-21T00:00:00Z")
    other_pr = REC.ReviewRecord(run_id="r2", run_dir=Path("/x"), repo="agent-platform",
                                pr="2", role="reviewer", sha="s2", verdict="APPROVE",
                                started_at="2026-09-21T01:00:00Z")
    other_role = REC.ReviewRecord(run_id="r3", run_dir=Path("/x"), repo="agent-platform",
                                  pr="1", role="security-reviewer", sha="s2", verdict="APPROVE",
                                  started_at="2026-09-21T01:00:00Z")
    assert REC.find_confirmed_rounds([rc, other_pr, other_role]) == []


# --- find_clean_rounds --------------------------------------------------------


def test_clean_round_requires_sha_at_the_merged_head():
    merged = REC.ReviewRecord(run_id="r1", run_dir=Path("/x"), repo="agent-platform",
                              pr="1", role="reviewer", sha="h" * 40, verdict="APPROVE",
                              started_at="2026-09-21T00:00:00Z")
    stale = REC.ReviewRecord(run_id="r2", run_dir=Path("/x"), repo="agent-platform",
                             pr="1", role="reviewer", sha="o" * 40, verdict="APPROVE",
                             started_at="2026-09-20T00:00:00Z")
    clean = REC.find_clean_rounds([merged, stale], merged_head=lambda repo, pr: "h" * 40)
    assert clean == [merged]


def test_clean_round_excludes_a_pr_with_no_merged_head_known():
    r = REC.ReviewRecord(run_id="r1", run_dir=Path("/x"), repo="agent-platform",
                         pr="1", role="reviewer", sha="h" * 40, verdict="APPROVE",
                         started_at="2026-09-21T00:00:00Z")
    assert REC.find_clean_rounds([r], merged_head=lambda repo, pr: None) == []


def test_clean_round_excludes_a_pr_with_a_later_request_changes():
    approve = REC.ReviewRecord(run_id="r1", run_dir=Path("/x"), repo="agent-platform",
                               pr="1", role="reviewer", sha="h" * 40, verdict="APPROVE",
                               started_at="2026-09-21T00:00:00Z")
    later_rc = REC.ReviewRecord(run_id="r2", run_dir=Path("/x"), repo="agent-platform",
                                pr="1", role="security-reviewer", sha="h" * 40,
                                verdict="REQUEST_CHANGES", started_at="2026-09-21T01:00:00Z")
    assert REC.find_clean_rounds([approve, later_rc],
                                 merged_head=lambda repo, pr: "h" * 40) == []


def test_clean_round_tolerant_of_an_earlier_request_changes_on_a_different_role():
    """An earlier REQUEST_CHANGES (e.g. security-reviewer, fixed before this
    APPROVE's commit) does not disqualify a later clean APPROVE at the merged
    head — only a LATER finding does."""
    earlier_rc = REC.ReviewRecord(run_id="r1", run_dir=Path("/x"), repo="agent-platform",
                                  pr="1", role="security-reviewer", sha="o" * 40,
                                  verdict="REQUEST_CHANGES", started_at="2026-09-20T00:00:00Z")
    approve = REC.ReviewRecord(run_id="r2", run_dir=Path("/x"), repo="agent-platform",
                               pr="1", role="reviewer", sha="h" * 40, verdict="APPROVE",
                               started_at="2026-09-21T00:00:00Z")
    assert REC.find_clean_rounds([earlier_rc, approve],
                                 merged_head=lambda repo, pr: "h" * 40) == [approve]


# --- sample_deterministic ----------------------------------------------------


def test_sample_deterministic_is_reproducible():
    items = list(range(100))
    a = REC.sample_deterministic(items, 10, seed=42)
    b = REC.sample_deterministic(items, 10, seed=42)
    c = REC.sample_deterministic(items, 10, seed=43)
    assert a == b
    assert a != c
    assert len(a) == 10


def test_sample_deterministic_returns_everything_when_k_exceeds_length():
    items = [1, 2, 3]
    assert REC.sample_deterministic(items, 10, seed=1) == items


# --- build_corpus_entry -------------------------------------------------------


def test_build_corpus_entry_skips_a_run_missing_prompts(tmp_path):
    run_dir = _make_run(tmp_path, "2026-09-21", "run-a", agent="reviewer",
                        repo="agent-platform", pr="1", review_sha="a" * 40,
                        review_verdict="APPROVE", with_prompts=False)
    record = REC.ReviewRecord(run_id="run-a", run_dir=run_dir, repo="agent-platform",
                              pr="1", role="reviewer", sha="a" * 40, verdict="APPROVE",
                              started_at="2026-09-21T00:00:00Z")
    assert REC.build_corpus_entry(record, kind="clean") is None


def test_build_corpus_entry_skips_a_run_missing_the_review_body(tmp_path):
    run_dir = _make_run(tmp_path, "2026-09-21", "run-a", agent="reviewer",
                        repo="agent-platform", pr="1", review_sha="a" * 40,
                        review_verdict="APPROVE", with_body=False)
    record = REC.ReviewRecord(run_id="run-a", run_dir=run_dir, repo="agent-platform",
                              pr="1", role="reviewer", sha="a" * 40, verdict="APPROVE",
                              started_at="2026-09-21T00:00:00Z")
    assert REC.build_corpus_entry(record, kind="clean") is None


def test_build_corpus_entry_extracts_findings_only_for_confirmed(tmp_path):
    run_dir = _make_run(tmp_path, "2026-09-21", "run-a", agent="reviewer",
                        repo="agent-platform", pr="1", review_sha="a" * 40,
                        review_verdict="REQUEST_CHANGES",
                        body_text="synthetic finding text\n", tokens_in=500,
                        tokens_out=20, usd=0.02)
    record = REC.ReviewRecord(run_id="run-a", run_dir=run_dir, repo="agent-platform",
                              pr="1", role="reviewer", sha="a" * 40,
                              verdict="REQUEST_CHANGES", started_at="2026-09-21T00:00:00Z")
    calls = []

    def stub_extractor(body):
        calls.append(body)
        return ["synthetic finding one", "synthetic finding two"]

    confirmed = REC.build_corpus_entry(record, kind="confirmed", extractor=stub_extractor)
    assert confirmed.answer_key == ["synthetic finding one", "synthetic finding two"]
    assert len(calls) == 1 and "synthetic finding text" in calls[0]
    assert confirmed.tokens_in == 500 and confirmed.usd == 0.02
    assert confirmed.entry_id == "agent-platform/1/reviewer/" + "a" * 12

    def boom(body):
        raise AssertionError("a clean entry must never call the extractor")

    clean = REC.build_corpus_entry(record, kind="clean", extractor=boom)
    assert clean.answer_key == []


def test_select_baseline_subset_is_deterministic_and_bounded():
    corpus = {"since_date": "x", "seed": 1, "sample_size": 50,
             "confirmed": list(range(50)), "clean": list(range(100, 150))}
    a = REC.select_baseline_subset(corpus, n_confirmed=15, n_clean=15, seed=7)
    b = REC.select_baseline_subset(corpus, n_confirmed=15, n_clean=15, seed=7)
    assert a == b
    assert len(a["confirmed"]) == 15 and len(a["clean"]) == 15
    assert all(c in corpus["confirmed"] for c in a["confirmed"])


# --- build_corpus end to end --------------------------------------------------


def test_build_corpus_end_to_end_with_stubs(tmp_path):
    # A confirmed round: REQUEST_CHANGES then a later APPROVE at a new head.
    _make_run(tmp_path, "2026-09-21", "run-rc", agent="reviewer", repo="agent-platform",
             pr="10", review_sha="a" * 40, review_verdict="REQUEST_CHANGES",
             started_at="2026-09-21T00:00:00Z", body_text="synthetic rc finding\n")
    _make_run(tmp_path, "2026-09-21", "run-ap", agent="reviewer", repo="agent-platform",
             pr="10", review_sha="b" * 40, review_verdict="APPROVE",
             started_at="2026-09-21T01:00:00Z")
    # A clean round: APPROVE at the (stubbed) merged head, PR 20.
    _make_run(tmp_path, "2026-09-22", "run-clean", agent="reviewer", repo="agent-platform",
             pr="20", review_sha="c" * 40, review_verdict="APPROVE",
             started_at="2026-09-22T00:00:00Z")

    def merged_head(repo, pr):
        return "c" * 40 if pr == "20" else None

    def stub_extractor(body):
        return ["synthetic rc finding, extracted"]

    corpus = REC.build_corpus(tmp_path, since_date="2026-09-20", sample_size=50,
                              seed=7, merged_head=merged_head, extractor=stub_extractor)

    assert corpus["since_date"] == "2026-09-20" and corpus["seed"] == 7
    assert [e["run_id"] for e in corpus["confirmed"]] == ["run-rc"]
    assert corpus["confirmed"][0]["answer_key"] == ["synthetic rc finding, extracted"]
    assert [e["run_id"] for e in corpus["clean"]] == ["run-clean"]
    assert corpus["clean"][0]["answer_key"] == []


def test_corpus_json_round_trip_carries_entry_id_for_plan_replays_and_scoring(tmp_path):
    """`corpus build` writes `CorpusEntry.as_dict()` to JSON; a later `replay
    run` (`review_eval_replay.plan_replays`) and `score` (bin/review-eval's
    `cmd_score`, `entries_by_id = {e["entry_id"]: e ...}`) both read
    `entry["entry_id"]` back out of that JSON. Builds a corpus through
    `build_corpus` (the same call `cmd_corpus_build` makes, model call
    stubbed), writes it to disk and reloads it exactly as `corpus build` and
    a later `replay run`/`score` invocation would, then exercises both
    readers against the reloaded entries. Fails on the pre-fix `as_dict`
    (entry_id omitted) with `KeyError: 'entry_id'` raised from
    `plan_replays`."""
    _make_run(tmp_path, "2026-09-21", "run-rc", agent="reviewer", repo="agent-platform",
             pr="10", review_sha="a" * 40, review_verdict="REQUEST_CHANGES",
             started_at="2026-09-21T00:00:00Z", body_text="synthetic rc finding\n")
    _make_run(tmp_path, "2026-09-21", "run-ap", agent="reviewer", repo="agent-platform",
             pr="10", review_sha="b" * 40, review_verdict="APPROVE",
             started_at="2026-09-21T01:00:00Z")

    corpus = REC.build_corpus(tmp_path, since_date="2026-09-20", sample_size=50, seed=7,
                              merged_head=lambda repo, pr: None,
                              extractor=lambda body: ["synthetic rc finding, extracted"])
    out_path = tmp_path / "corpus.json"
    out_path.write_text(json.dumps(corpus, indent=2) + "\n")  # same write cmd_corpus_build does
    reloaded = json.loads(out_path.read_text())

    entries = reloaded["confirmed"]
    assert len(entries) == 1 and "entry_id" in entries[0]

    import review_eval_replay as RER
    arm = RER.ModelArm(key="deepseek", model="deepseek", is_gateway=True)
    planned = RER.plan_replays(entries, [arm], existing={})
    assert len(planned) == 1

    entries_by_id = {e["entry_id"]: e for e in entries}  # bin/review-eval's cmd_score lookup
    assert entries_by_id[entries[0]["entry_id"]] is entries[0]


def test_default_finding_extractor_routes_its_env_through_the_credential_guard(monkeypatch):
    """CQ·418 round 4: this bare `claude -p` launch passed no `env=` at all, which
    carries the operator's git/gh credential helpers straight through — the same
    class `dispatch_env()` already closes for every other launch site. The test
    fails against 9ae129c0 (no `env=` kwarg reaches `subprocess.run` at all, so
    `calls[0]["env"]` is `None` and every assertion below raises)."""
    monkeypatch.setenv("GH_TOKEN", "ghp_secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append({"argv": argv, "env": kwargs.get("env")})
        return subprocess.CompletedProcess(argv, 0, stdout='{"result": "[\\"a finding\\"]"}')

    monkeypatch.setattr(REC.subprocess, "run", fake_run)

    findings = REC.default_finding_extractor("some review body")

    assert findings == ["a finding"]
    assert len(calls) == 1
    env = calls[0]["env"]
    assert env is not None
    assert os.path.isdir(env["GH_CONFIG_DIR"])
    assert list(Path(env["GH_CONFIG_DIR"]).iterdir()) == []
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["GIT_CONFIG_SYSTEM"] == "/dev/null"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "GH_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env
