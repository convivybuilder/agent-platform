"""bin/review-eval — the review-replay eval harness's CLI wiring
(agent-platform#807).

Exercises argument parsing and subcommand wiring end to end against an EMPTY
or synthetic corpus, so nothing here reaches `agentctl`, `gh`, `claude`, or
the gateway — the underlying dispatch/scoring logic (real subprocess calls
injected as stubs) is covered in tests/test_review_eval_replay.py and
tests/test_review_eval_scoring.py instead. The one exception is
`_anthropic_judge_call` itself (below): it lives in this module, so its own
`subprocess.run` call is stubbed here rather than in one of those files.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
         "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True,
    )


def test_corpus_build_on_an_empty_runs_root_writes_an_empty_corpus(
        review_eval_cli, tmp_path):
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    out = tmp_path / "corpus.json"
    rc = review_eval_cli.main(["corpus", "build", "--runs-root", str(runs_root),
                               "--seed", "1", "--out", str(out)])
    assert rc == 0
    data = json.loads(out.read_text())
    assert data["confirmed"] == [] and data["clean"] == []


def test_estimate_reports_no_pinned_pricing_for_an_unknown_model(
        review_eval_cli, tmp_path, capsys):
    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps({"confirmed": [], "clean": []}))
    rc = review_eval_cli.main(["estimate", "--corpus", str(corpus),
                               "--models", "deepseek", "--runs-root", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "deepseek" in out and "cache miss" in out


def test_estimate_prices_from_the_real_gateway_config(review_eval_cli, tmp_path, capsys):
    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps(
        {"confirmed": [{"tokens_in": 10_000_000, "tokens_out": 500_000}], "clean": []}))
    rc = review_eval_cli.main(["estimate", "--corpus", str(corpus),
                               "--models", "deepseek", "--runs-root", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "$0.00" not in out.splitlines()[0]  # non-trivial corpus prices non-zero


def test_replay_run_on_an_empty_corpus_dispatches_nothing(review_eval_cli, tmp_path, capsys):
    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps({"confirmed": [], "clean": [], "seed": 0}))
    results_dir = tmp_path / "results"
    rc = review_eval_cli.main(["replay", "run", "--corpus", str(corpus),
                               "--models", "deepseek", "--results-dir", str(results_dir)])
    assert rc == 0
    assert list(results_dir.glob("*.json")) == []


def test_replay_run_rejects_an_unknown_model_arm(review_eval_cli, tmp_path, capsys):
    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps({"confirmed": [], "clean": [], "seed": 0}))
    rc = review_eval_cli.main(["replay", "run", "--corpus", str(corpus),
                               "--models", "not-a-real-model",
                               "--results-dir", str(tmp_path / "results")])
    assert rc == 2
    assert "unknown model arm" in capsys.readouterr().err


def test_score_combines_confirmed_and_clean_into_a_total(
        review_eval_cli, tmp_path, capsys, monkeypatch):
    """A non-empty corpus with one confirmed and one clean entry, both
    replayed by the same model — the judge/match/diff calls are stubbed, so
    this never reaches `claude`/`git`, but it exercises the real
    confirmed/clean/total aggregation `cmd_score` does around them."""
    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps({
        "confirmed": [{"entry_id": "c1", "repo": "agent-platform", "pr": "1",
                       "role": "reviewer", "sha": "a" * 40,
                       "answer_key": ["synthetic key finding"]}],
        "clean": [{"entry_id": "e1", "repo": "agent-platform", "pr": "2",
                  "role": "reviewer", "sha": "b" * 40, "answer_key": []}],
    }))
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    (results_dir / "c1__deepseek.json").write_text(json.dumps(
        {"entry_id": "c1", "model_key": "deepseek", "verdict": "REQUEST_CHANGES",
         "body": "synthetic review body", "usd": 0.01, "tokens_in": 10, "tokens_out": 5}))
    (results_dir / "e1__deepseek.json").write_text(json.dumps(
        {"entry_id": "e1", "model_key": "deepseek", "verdict": "APPROVE",
         "body": "synthetic review body", "usd": 0.02, "tokens_in": 10, "tokens_out": 5}))

    monkeypatch.setattr(review_eval_cli, "_diff_for_entry", lambda entry, repo_root: "synthetic diff")
    monkeypatch.setattr("review_eval_corpus.default_finding_extractor",
                        lambda body: ["synthetic key finding"])
    monkeypatch.setattr(review_eval_cli, "_match_fn",
                        lambda candidate, key: (0, "stub match") if key else (None, "no key"))
    monkeypatch.setattr(review_eval_cli, "_judge_fn",
                        lambda finding, diff: (True, "stub judged real"))

    out = tmp_path / "report.json"
    rc = review_eval_cli.main(["score", "--corpus", str(corpus), "--results-dir",
                               str(results_dir), "--out", str(out)])
    assert rc == 0
    payload = json.loads(out.read_text())

    confirmed_model = payload["confirmed"]["models"][0]
    assert confirmed_model["recall_confirmed"] == 1.0  # the one key item matched
    assert confirmed_model["verdict_agreement_rate"] == 1.0  # REQUEST_CHANGES == REQUEST_CHANGES

    clean_model = payload["clean"]["models"][0]
    assert clean_model["recall_confirmed"] is None  # no key on a clean round
    assert clean_model["judged_real_extra"] == 1  # the clean round's candidate finding, judged
    assert clean_model["verdict_agreement_rate"] == 1.0  # APPROVE == APPROVE

    total_model = payload["total"]["models"][0]
    assert total_model["runs_total"] == 2
    assert total_model["judged_real_extra"] == 1


def test_diff_for_entry_covers_every_commit_since_the_prs_base(review_eval_cli, tmp_path):
    """Round-1 review (agent-platform#813): `git show <sha>` shows only that one
    commit. A PR with two commits by the review round under test must still
    surface BOTH in the diff a judge reads — not only the second."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")

    _git(repo, "checkout", "-b", "feature")
    (repo / "first.txt").write_text("first commit's file\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "first commit")
    (repo / "second.txt").write_text("second commit's file\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "second commit")
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                        capture_output=True, text=True, check=True).stdout.strip()

    diff = review_eval_cli._diff_for_entry({"sha": sha}, repo_root=repo)

    assert "first.txt" in diff, "the diff must include the earlier commit's change"
    assert "second.txt" in diff, "the diff must include the entry's own commit's change"
    assert "base.txt" not in diff, "the diff must not reach past the PR's base"


def test_diff_for_entry_prefers_origin_main_when_present(review_eval_cli, tmp_path):
    """A full clone (what `replay score`'s `--repo-root-base` points at)
    carries a remote-tracking `origin/main`, not just a local `main` — the
    primary path, exercised here with an actual clone rather than the
    no-origin fallback the previous test exercises."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-b", "main")
    (origin / "base.txt").write_text("base\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-m", "base")

    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", str(origin), str(repo)],
                   check=True, capture_output=True)
    _git(repo, "checkout", "-b", "feature")
    (repo / "change.txt").write_text("change\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "change")
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                        capture_output=True, text=True, check=True).stdout.strip()

    diff = review_eval_cli._diff_for_entry({"sha": sha}, repo_root=repo)

    assert "change.txt" in diff
    assert "base.txt" not in diff


def test_diff_for_entry_on_an_unresolvable_sha_returns_empty(review_eval_cli, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")

    diff = review_eval_cli._diff_for_entry({"sha": "f" * 40}, repo_root=repo)

    assert diff == ""


def test_score_on_empty_results_writes_an_empty_report(review_eval_cli, tmp_path, capsys):
    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps({"confirmed": [], "clean": []}))
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    out = tmp_path / "report.json"
    rc = review_eval_cli.main(["score", "--corpus", str(corpus),
                               "--results-dir", str(results_dir), "--out", str(out)])
    assert rc == 0
    assert json.loads(out.read_text()) == {
        "confirmed": {"models": []}, "clean": {"models": []}, "total": {"models": []}}
    audit = tmp_path / "report-audit-sample.json"
    assert json.loads(audit.read_text()) == {"judged_real": [], "judged_false": []}


def test_anthropic_judge_call_routes_its_env_through_the_credential_guard(
        review_eval_cli, monkeypatch):
    """CQ·418 round 4: this bare `claude -p` launch passed no `env=` at all, which
    carries the operator's git/gh credential helpers straight through — the same
    class `dispatch_env()` already closes for every other launch site. The test
    fails against 9ae129c0 (no `env=` kwarg reaches `subprocess.run` at all, so
    `calls[0]["env"]` is `None` and every assertion below raises)."""
    import subprocess

    monkeypatch.setenv("GH_TOKEN", "ghp_secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append({"argv": argv, "env": kwargs.get("env")})
        return subprocess.CompletedProcess(argv, 0, stdout='{"result": "{\\"ok\\": true}"}')

    monkeypatch.setattr(review_eval_cli.subprocess, "run", fake_run)

    result = review_eval_cli._anthropic_judge_call("prompt", model="sonnet")

    assert result == {"ok": True}
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
