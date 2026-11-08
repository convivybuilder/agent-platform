"""Pure parse + aggregation logic for bin/security-scorecard (ARCH-306).

The risk these guard: the finding-count parse reads the ARCH-033 review-comment
format (`**Blocking — substance:**` bullets), and the aggregation turns per-round
rows into the per-unit vulnerability rate the dashboard reports. A miscount, a
mis-keyed upsert, or a wrong per-KLOC divisor would misstate the security-catch
record. The gh-calling IO is excluded; these pin the pure logic.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import review_protocol as RP


# --- _count_findings ---------------------------------------------------------


def test_count_findings_blocking_and_nonblocking(security_scorecard):
    body = (
        "## Findings\n"
        "- **Blocking — substance:** SQLi at `users.py:142` — raw query.\n"
        "- **Non-blocking — substance:** verbose error leaks a path — `api.py:9`.\n"
        "- **Blocking — substance:** command injection in `run.sh`.\n"
        "**Verdict:** thumbs-down\n"
    )
    assert security_scorecard._count_findings(body) == (2, 1)


def test_count_findings_nonblocking_not_miscounted_as_blocking(security_scorecard):
    # "Non-blocking" contains "blocking" — the regex must not double-count it.
    body = "- **Non-blocking — substance:** minor risk."
    assert security_scorecard._count_findings(body) == (0, 1)


def test_count_findings_case_and_dash_insensitive(security_scorecard):
    # em-dash and lowercase severity still parse (reviewers vary the dash/casing).
    body = "- **blocking — substance:** x\n- **NON-BLOCKING - substance:** y"
    assert security_scorecard._count_findings(body) == (1, 1)


def test_count_findings_clean_review(security_scorecard):
    assert security_scorecard._count_findings("No security findings. Verdict: thumbs-up.") == (0, 0)
    assert security_scorecard._count_findings("") == (0, 0)


def test_count_findings_ignores_suggestions(security_scorecard):
    # ARCH-033 also has `**Suggestion — style:**` bullets; those are not vulns and
    # must not count toward either severity (guards a future regex edit).
    body = ("- **Suggestion — style:** rename this for clarity.\n"
            "- **Blocking — substance:** real vuln.")
    assert security_scorecard._count_findings(body) == (1, 0)


# --- _resolve_run_facts / _authentic_security_stamp --------------------------


def test_authentic_security_stamp_refuses_a_run_id_planted_under_two_date_dirs(
        security_scorecard, monkeypatch, tmp_path):
    """A run-id resolved under two date directories (a planted duplicate,
    issue_01M1JWB7M75SZ160VRJRCGARBF) must not authenticate: the real run here
    is a coder (not review-class), and a worker plants a second, newer-sorting
    directory under the SAME run-id forging a clean security-reviewer stamp. A
    first-match glob would pick the planted directory and authenticate the
    forged stamp; the harvester must instead treat the ambiguous run-id like an
    unresolved one and count the round as unverified."""
    import telemetry as T

    monkeypatch.setattr(T, "RUNS_ROOT", tmp_path)
    monkeypatch.setattr(security_scorecard, "RUNS_ROOT", tmp_path)

    head = "c" * 40
    real = T.create_run_dir("run-dupsec", date="2026-08-03")
    T.write_meta(real, run_id="run-dupsec", agent="coder", task="t",
                started_at="2026-08-03T00:00:00Z", repo="convivy/widget",
                worktree=str(tmp_path), profile="p", model="claude-opus-4-7")
    planted = T.create_run_dir("run-dupsec", date="2099-01-01")
    T.write_meta(planted, run_id="run-dupsec", agent="security-reviewer", task="t",
                started_at="2099-01-01T00:00:00Z", repo="convivy/widget",
                worktree=str(tmp_path), profile="p", model="claude-opus-4-7",
                review_sha=head, pr="7")
    T.update_meta(planted, review_verdict="APPROVE", review_pr="7")  # what review-post records

    stamp = RP.parse(RP.render_header(pr="7", repo="convivy/widget", reviewed_commit=head,
                                      verdict="APPROVE", agent="security-reviewer",
                                      run_id="run-dupsec"))

    assert security_scorecard._authentic_security_stamp(stamp, 7) is False


@pytest.mark.rich_registry
def test_authentic_security_stamp_accepts_any_internal_app_prefixed_reviewer(
        security_scorecard, monkeypatch, tmp_path):
    """`_authentic_security_stamp` must recognize an internal-app-prefixed
    security-reviewer variant generically (`RP.is_review_class`, every prefix
    in `RP.internal_app_prefixes()`), not a hardcoded set naming one crew — the
    bug class review_protocol's own `_review_base` docstring warns about:
    a crew whose prefix is missing from a hand-maintained list reads every one
    of its genuine review stamps as inauthentic."""
    import telemetry as T

    monkeypatch.setattr(T, "RUNS_ROOT", tmp_path)
    monkeypatch.setattr(security_scorecard, "RUNS_ROOT", tmp_path)

    head = "d" * 40
    agent = next(p for p in RP.internal_app_prefixes() if p != "atlas-") + "security-reviewer"
    real = T.create_run_dir("run-genericsec", date="2026-08-03")
    T.write_meta(real, run_id="run-genericsec", agent=agent, task="t",
                started_at="2026-08-03T00:00:00Z", repo="acme/acme",
                worktree=str(tmp_path), profile="p", model="claude-opus-4-7",
                review_sha=head, pr="9")
    T.update_meta(real, review_verdict="APPROVE", review_pr="9")

    stamp = RP.parse(RP.render_header(pr="9", repo="acme/acme", reviewed_commit=head,
                                      verdict="APPROVE", agent=agent,
                                      run_id="run-genericsec"))

    assert security_scorecard._authentic_security_stamp(stamp, 9) is True


# --- upsert ------------------------------------------------------------------


def test_upsert_keys_on_comment_time(security_scorecard):
    ledger = [{"repo": "widget", "pr": 1, "comment_at": "t1", "blocking": 0}]
    fresh = [
        {"repo": "widget", "pr": 1, "comment_at": "t1", "blocking": 2},  # refresh same round
        {"repo": "widget", "pr": 1, "comment_at": "t2", "blocking": 0},  # a new round
    ]
    out = security_scorecard.upsert(ledger, fresh)
    by_time = {r["comment_at"]: r for r in out}
    assert len(out) == 2
    assert by_time["t1"]["blocking"] == 2   # refreshed in place, not duplicated
    assert "t2" in by_time


def test_upsert_keeps_two_rounds_on_the_same_commit(security_scorecard):
    # Two review rounds reviewing the SAME head (a manual re-dispatch without a new
    # push) have distinct comment times — keying on comment_at keeps both, where
    # keying on reviewed_commit would have collapsed round 1 into round 2.
    fresh = [
        {"repo": "widget", "pr": 5, "reviewed_commit": "same", "comment_at": "t1", "blocking": 1},
        {"repo": "widget", "pr": 5, "reviewed_commit": "same", "comment_at": "t2", "blocking": 0},
    ]
    out = security_scorecard.upsert([], fresh)
    assert len(out) == 2   # not 1 — round 1's finding is not dropped


# --- window_rows -------------------------------------------------------------


def test_window_rows_filters_by_comment_time(security_scorecard):
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=3)).isoformat()
    old = (now - timedelta(days=40)).isoformat()
    rows = [{"comment_at": recent, "pr": 1}, {"comment_at": old, "pr": 2}, {"comment_at": "", "pr": 3}]
    kept = security_scorecard.window_rows(rows, 30)
    assert [r["pr"] for r in kept] == [1]   # old and blank-timestamp rows drop out


# --- build_report ------------------------------------------------------------


def _rows():
    # PR 1 (widget, sonnet, 1000 LOC): round 1 finds 1 blocking + 1 non-blocking.
    # PR 2 (widget, sonnet, 1000 LOC): round 1 clean, round 2 finds 1 blocking (a late/internal escape).
    # PR 3 (globex, deepseek, 100 LOC): round 1 finds 1 blocking.
    # PR 4 (widget, sonnet, 900 LOC): reviewed clean — no findings at all.
    return [
        {"repo": "widget", "pr": 1, "round": 1, "blocking": 1, "non_blocking": 1,
         "changed_loc": 1000, "producer_model": "claude-sonnet-4-6"},
        {"repo": "widget", "pr": 2, "round": 1, "blocking": 0, "non_blocking": 0,
         "changed_loc": 1000, "producer_model": "claude-sonnet-4-6"},
        {"repo": "widget", "pr": 2, "round": 2, "blocking": 1, "non_blocking": 0,
         "changed_loc": 1000, "producer_model": "claude-sonnet-4-6"},
        {"repo": "globex", "pr": 3, "round": 1, "blocking": 1, "non_blocking": 0,
         "changed_loc": 100, "producer_model": "deepseek/deepseek-chat"},
        {"repo": "widget", "pr": 4, "round": 1, "blocking": 0, "non_blocking": 0,
         "changed_loc": 900, "producer_model": "claude-sonnet-4-6"},
    ]


def test_build_report_overall_rates(security_scorecard):
    rep = security_scorecard.build_report(_rows(), window_days=30)
    o = rep["overall"]
    assert o["prs_reviewed"] == 4                 # 4 distinct PRs, not 5 rows
    assert o["changed_loc"] == 3000              # 1000 + 1000 + 100 + 900 (per-PR, not per-round)
    assert o["blocking_findings"] == 3
    assert o["non_blocking_findings"] == 1
    assert o["total_findings"] == 4
    # 3 blocking over 3.0 KLOC
    assert o["blocking_per_kloc"] == round(3 / 3.0, 2)
    # 3 of 4 PRs carried a blocking finding (PR 4 is clean)
    assert o["pct_prs_with_blocking"] == round(100 * 3 / 4, 1)
    # PR 2's blocking surfaced at round 2 → one internal escape
    assert o["internal_escapes"] == 1


def test_build_report_changed_loc_counts_pr_once(security_scorecard):
    # A PR reviewed over 2 rounds must not double-count its LOC in the denominator.
    rep = security_scorecard.build_report(_rows())
    assert rep["overall"]["changed_loc"] == 3000  # not 4000 (PR 2's 1000 counted once)


def test_build_report_by_model_split(security_scorecard):
    rep = security_scorecard.build_report(_rows())
    bm = rep["by_model"]
    assert set(bm) == {"claude-sonnet-4-6", "deepseek/deepseek-chat"}
    # deepseek: 1 blocking over 0.1 KLOC = 10.0 blk/KLOC (small-PR sensitivity is real)
    assert bm["deepseek/deepseek-chat"]["blocking_per_kloc"] == round(1 / 0.1, 2)
    assert bm["deepseek/deepseek-chat"]["prs_reviewed"] == 1
    # sonnet: 2 blocking over 2.9 KLOC (PRs 1, 2, 4)
    assert bm["claude-sonnet-4-6"]["blocking_per_kloc"] == round(2 / 2.9, 2)
    assert bm["claude-sonnet-4-6"]["prs_reviewed"] == 3


def test_internal_escape_not_counted_when_round1_already_caught_it(security_scorecard):
    # A blocking finding round 1 CAUGHT that persists (partial fix) into round 2 is
    # not an escape — round 1 didn't miss it. Only a round-1-clean-then-later-blocking
    # PR counts. This is the fix for the "sum all blocking at round >=2" overcount.
    rows = [
        {"repo": "widget", "pr": 9, "round": 1, "blocking": 1, "non_blocking": 0,
         "changed_loc": 500, "producer_model": "m"},
        {"repo": "widget", "pr": 9, "round": 2, "blocking": 1, "non_blocking": 0,
         "changed_loc": 500, "producer_model": "m"},
    ]
    assert security_scorecard.build_report(rows)["overall"]["internal_escapes"] == 0


def test_build_report_empty(security_scorecard):
    rep = security_scorecard.build_report([])
    assert rep["overall"]["prs_reviewed"] == 0
    assert rep["overall"]["blocking_per_kloc"] == 0.0
    assert rep["by_model"] == {} and rep["by_repo"] == {}


# --- agent_prs: refuse a forged repo before it reaches a gh query ------------


def test_agent_prs_excludes_a_run_with_a_forged_qualified_repo(
        security_scorecard, monkeypatch, tmp_path):
    """Round 2 security finding (PR #881, comment 6152094447): `repo` reaches
    this ledger row from a run's own meta.json, a file the dispatched run
    itself can write. Fails against 43f57916: the forged repo was included in
    agent_prs's output, so main()'s loop called harvest_pr (and its gh query)
    against it."""
    import json as json_mod
    today = datetime.now(timezone.utc).date().isoformat()
    rows = [
        {"pr_number": 1, "repo": "acme-attacker/widget", "date": today,
         "agent": "bug-fixer", "model": "m"},
        {"pr_number": 2, "repo": "homebrew-tap", "date": today,
         "agent": "bug-fixer", "model": "m"},
    ]
    ledger_path = tmp_path / "scorecard.jsonl"
    ledger_path.write_text("".join(json_mod.dumps(r) + "\n" for r in rows))
    monkeypatch.setattr(security_scorecard, "SCORECARD_LEDGER", ledger_path)

    out = security_scorecard.agent_prs(30)

    assert "acme-attacker/widget" not in out
    assert out["homebrew-tap"][2]["producer_agent"] == "bug-fixer"


# --- harvest_pr's slug: registered owner, not always convivy ----------------


@pytest.mark.rich_registry
def test_harvest_pr_queries_the_registered_owner_for_homebrew_tap(
        security_scorecard, monkeypatch):
    """Round-1 review (PR #881, comment 6150937461): `harvest_pr` built
    `f"convivy/{repo}"` directly — the same pattern the PR centralizes
    everywhere else. Pinning the `--repo` argument it passes through, not
    real `gh` IO, which this file's module docstring excludes."""
    captured = {}

    def fake_gh_json(args, env):
        captured["args"] = args
        return None  # harvest_pr returns [] on a falsy view; no further IO

    monkeypatch.setattr(security_scorecard, "_gh_json", fake_gh_json)
    security_scorecard.harvest_pr("brewcask-tap", 1, {}, {})

    assert captured["args"] and "--repo" in captured["args"]
    repo_arg = captured["args"][captured["args"].index("--repo") + 1]
    assert repo_arg == "caskforge/brewcask-tap"


# --- the GitHub credential seam (company/decisions/0027): every `gh` call
# runs on a token minted from the install's own GitHub App, never the
# operator's `gh` login ------------------------------------------------------

def test_main_threads_the_minted_tokens_env_into_every_gh_call(
        security_scorecard, monkeypatch, tmp_path):
    """Drives `main`'s real `GA.read_token_env` (only `GA.installed_app` and
    `GA.mint_token` are stubbed — `GA.gh_env` runs for real): the minted
    token lands in `GH_TOKEN`, and `GH_CONFIG_DIR` is an empty, throwaway
    directory, on every `gh` call `harvest_pr` makes for the repo."""
    from contextlib import contextmanager
    from pathlib import Path

    monkeypatch.setattr(security_scorecard, "agent_prs",
                        lambda days: {"widget": {1: {"producer_agent": "bug-fixer",
                                                      "producer_model": "m"}}})
    monkeypatch.setattr(security_scorecard, "LEDGER_PATH", tmp_path / "security.jsonl")
    monkeypatch.setattr(security_scorecard, "REPORT_PATH", tmp_path / "security-report.json")
    monkeypatch.setattr(security_scorecard.sys, "argv", ["security-scorecard", "--no-write"])
    monkeypatch.setattr(
        security_scorecard.GA, "installed_app",
        lambda: security_scorecard.GA.InstalledApp(app_id="1", key_path=Path("/dev/null"), slug=None))

    @contextmanager
    def fake_mint_token(repo_slug, verb, *, key_path, app_id,
                        label="the install's GitHub App", opener=None):
        assert repo_slug == "convivy/widget"
        assert verb == "read"
        yield "ghs_minted_token"

    monkeypatch.setattr(security_scorecard.GA, "mint_token", fake_mint_token)

    captured = []

    def fake_run(argv, **kwargs):
        env = kwargs.get("env")
        assert env["GH_TOKEN"] == "ghs_minted_token"
        config_dir = Path(env["GH_CONFIG_DIR"])
        assert config_dir.is_dir()
        assert list(config_dir.iterdir()) == []
        captured.append(env)
        return type("P", (), {"returncode": 0, "stdout": "{}", "stderr": ""})()

    monkeypatch.setattr(security_scorecard.subprocess, "run", fake_run)
    assert security_scorecard.main() == 0
    assert captured, "gh must have been called with the minted token's environment"


def test_a_mint_failure_runs_no_gh_process_for_that_repo(
        security_scorecard, monkeypatch, tmp_path, capsys):
    """A repo whose token cannot be minted is never queried: `harvest_pr`'s
    own `gh` call must not run, and the failure is named on stderr rather
    than silently dropping the repo's agent PRs from the harvest."""
    monkeypatch.setattr(security_scorecard, "agent_prs",
                        lambda days: {"widget": {1: {"producer_agent": "bug-fixer",
                                                      "producer_model": "m"}}})
    monkeypatch.setattr(security_scorecard, "LEDGER_PATH", tmp_path / "security.jsonl")
    monkeypatch.setattr(security_scorecard, "REPORT_PATH", tmp_path / "security-report.json")
    monkeypatch.setattr(security_scorecard.sys, "argv", ["security-scorecard", "--no-write"])

    class _FailingReadTokenEnv:
        def __init__(self, repo_slug):
            pass

        def __enter__(self):
            raise security_scorecard.GA.Unreadable("no App key configured at .../app.pem")

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(security_scorecard.GA, "read_token_env", _FailingReadTokenEnv)
    calls = []
    monkeypatch.setattr(security_scorecard.subprocess, "run",
                        lambda *a, **k: calls.append((a, k)))

    assert security_scorecard.main() == 0
    assert calls == [], "a mint failure must never fall back to running gh"
    err = capsys.readouterr().err
    assert "cannot mint a GitHub token for widget" in err
    assert "no App key configured" in err
