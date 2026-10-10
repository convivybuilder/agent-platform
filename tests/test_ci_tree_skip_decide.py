"""`scripts/ci/ci-tree-skip-decide` — the tree-equality push skip's decision
logic (issue_01M36FKG4QJQ2SPQSW0VDY0A66).

THE DEFECT THIS PINS. A PR head carries MORE THAN ONE job run per name:
this repo's draft-stage CI (ARCH-364) leaves a `skipped` `bash`/`python`
job on a PR's first push, then a later one concluding `success` once
review marks it ready — confirmed live on PR #643's head
(17a00d4f085d7b047de45540a186d84fbebfa3b0). An earlier version of this
decision picked the most recently STARTED run per name with no filter, which
reads whichever of the two runs happens to sort last as authoritative
— the draft-stage `skipped` entry on a repo where it sorts after the real
run, or a stale `success` as still-current on one where a rerun reorders
them. `_latest_verdict` must exclude `skipped` explicitly; the fixtures below
always include both a skipped and a real run, so a version that forgot the
exclusion fails here rather than only in a live PR.

A SECOND DEFECT THIS PINS. An earlier version matched the head's runs by
`name == "bash"` / `"python"` alone, with no check on which workflow
produced them — a same-named job from a different workflow file (or any
GitHub App able to post a same-named check-run) would satisfy the match
without ever having run this suite. `_matching_run_ids` scopes to
`WORKFLOW_PATH` before `_jobs_from_payloads` ever sees a job; the fixtures
below include a same-named job from a different workflow path, so a version
that dropped the path filter fails here rather than only on a repo that
grows a second workflow.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "ci" / "ci-tree-skip-decide"


def _load():
    loader = importlib.machinery.SourceFileLoader("ci_tree_skip_decide", str(SCRIPT))
    spec = importlib.util.spec_from_loader("ci_tree_skip_decide", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture()
def ctsd():
    return _load()


def _run(name: str, conclusion: str, started_at: str, *, url: str = "") -> dict:
    return {"name": name, "conclusion": conclusion, "started_at": started_at,
            "html_url": url or f"https://example.invalid/{name}/{started_at}"}


# --- _matching_run_ids / _jobs_from_payloads -----------------------------------


def test_matching_run_ids_keeps_only_the_workflow_path(ctsd):
    runs_payload = {
        "workflow_runs": [
            {"id": 1, "path": ".github/workflows/ci.yml"},
            {"id": 2, "path": ".github/workflows/other.yml"},
        ]
    }
    assert ctsd._matching_run_ids(runs_payload) == [1]


def test_matching_run_ids_empty_when_payload_is_not_a_dict(ctsd):
    assert ctsd._matching_run_ids(None) == []
    assert ctsd._matching_run_ids([1, 2, 3]) == []


def test_matching_run_ids_empty_when_workflow_runs_is_missing_or_not_a_list(ctsd):
    assert ctsd._matching_run_ids({}) == []
    assert ctsd._matching_run_ids({"workflow_runs": "not-a-list"}) == []


def test_jobs_from_payloads_flattens_and_skips_malformed_entries(ctsd):
    payloads = [
        {"jobs": [_run("bash", "success", "2026-09-20T01:00:00Z")]},
        {"jobs": "not-a-list"},
        None,
        {"jobs": [_run("python", "success", "2026-09-20T01:00:00Z")]},
    ]
    jobs = ctsd._jobs_from_payloads(payloads)
    assert [j["name"] for j in jobs] == ["bash", "python"]


def test_check_runs_ignores_a_same_named_job_from_a_different_workflow(ctsd):
    """The exact scenario the second defect above covers: a `bash` job that
    concluded `success`, but on a workflow other than `WORKFLOW_PATH`, must
    never reach `_latest_verdict`/`decide_tree_skip` — matching by job name
    alone would trust it as if `ci.yml` had run and passed."""
    runs_payload = {
        "workflow_runs": [
            {"id": 1, "path": ctsd.WORKFLOW_PATH},
            {"id": 2, "path": ".github/workflows/other.yml"},
        ]
    }
    jobs_by_run_id = {
        1: {"jobs": [_run("bash", "success", "2026-09-20T02:00:00Z", url="https://example.invalid/real")]},
        2: {"jobs": [_run("bash", "success", "2026-09-20T03:00:00Z", url="https://example.invalid/forged")]},
    }
    run_ids = ctsd._matching_run_ids(runs_payload)
    jobs = ctsd._jobs_from_payloads([jobs_by_run_id[rid] for rid in run_ids])
    assert len(jobs) == 1
    assert jobs[0]["html_url"] == "https://example.invalid/real"

    # And the decision table, fed only the scoped jobs, must not be swayed by
    # the forged run's later started_at — it was never in the candidate pool.
    verdict = ctsd._latest_verdict(jobs, "bash")
    assert verdict["html_url"] == "https://example.invalid/real"


# --- _latest_verdict ----------------------------------------------------------


def test_latest_verdict_skips_the_draft_stage_run_and_finds_the_real_one(ctsd):
    """The exact shape PR #643's head carries: a skipped draft-stage run
    followed by a success once review marks it ready."""
    runs = [
        _run("bash", "skipped", "2026-09-20T01:00:00Z"),
        _run("bash", "success", "2026-09-20T02:00:00Z"),
    ]
    verdict = ctsd._latest_verdict(runs, "bash")
    assert verdict["conclusion"] == "success"


def test_latest_verdict_ignores_skipped_even_when_it_sorts_last(ctsd):
    """The exclusion must be by CONCLUSION, not by taking the earliest-started
    run — a rerun can leave the skipped entry sorting after the real one."""
    runs = [
        _run("bash", "success", "2026-09-20T01:00:00Z"),
        _run("bash", "skipped", "2026-09-20T02:00:00Z"),
    ]
    verdict = ctsd._latest_verdict(runs, "bash")
    assert verdict["conclusion"] == "success"


def test_latest_verdict_none_when_only_skipped_runs_exist(ctsd):
    """A head that never actually ran `bash` for real (e.g. merged straight
    from draft with no ready-stage run) must read as missing, not as
    whatever the skipped run's conclusion happens to be."""
    runs = [_run("bash", "skipped", "2026-09-20T01:00:00Z")]
    assert ctsd._latest_verdict(runs, "bash") is None


def test_latest_verdict_none_when_the_name_is_absent(ctsd):
    assert ctsd._latest_verdict([_run("python", "success", "2026-09-20T01:00:00Z")], "bash") is None


def test_latest_verdict_picks_the_most_recently_started_among_reruns(ctsd):
    runs = [
        _run("bash", "failure", "2026-09-20T01:00:00Z"),
        _run("bash", "success", "2026-09-20T03:00:00Z"),
        _run("bash", "success", "2026-09-20T02:00:00Z"),
    ]
    verdict = ctsd._latest_verdict(runs, "bash")
    assert verdict["started_at"] == "2026-09-20T03:00:00Z"


# --- decide_tree_skip ----------------------------------------------------------


_BOTH_GREEN = [
    _run("bash", "skipped", "2026-09-20T01:00:00Z"),
    _run("bash", "success", "2026-09-20T02:00:00Z", url="https://example.invalid/bash-run"),
    _run("python", "skipped", "2026-09-20T01:00:00Z"),
    _run("python", "success", "2026-09-20T02:00:00Z"),
]


def test_skips_when_trees_match_and_both_checks_are_green_past_the_draft_skip(ctsd):
    result = ctsd.decide_tree_skip(
        pr_number=643, head_sha="17a00d4f085d7b047de45540a186d84fbebfa3b0",
        merge_tree="deadbeef", head_tree="deadbeef", check_runs=_BOTH_GREEN)
    assert result == {
        "tree_skip": True,
        "skip_pr": "643",
        "skip_head": "17a00d4f085d7b047de45540a186d84fbebfa3b0",
        "skip_run_url": "https://example.invalid/bash-run",
    }


def test_does_not_skip_when_trees_differ(ctsd):
    result = ctsd.decide_tree_skip(
        pr_number=643, head_sha="abc123",
        merge_tree="deadbeef", head_tree="somethingelse", check_runs=_BOTH_GREEN)
    assert result["tree_skip"] is False


@pytest.mark.parametrize("missing_tree", ["merge_tree", "head_tree"])
def test_does_not_skip_when_a_tree_could_not_be_read(ctsd, missing_tree):
    trees = {"merge_tree": "deadbeef", "head_tree": "deadbeef"}
    trees[missing_tree] = None
    result = ctsd.decide_tree_skip(
        pr_number=643, head_sha="abc123", check_runs=_BOTH_GREEN, **trees)
    assert result["tree_skip"] is False


def test_does_not_skip_when_no_pr_was_found(ctsd):
    result = ctsd.decide_tree_skip(
        pr_number=None, head_sha="", merge_tree="deadbeef", head_tree="deadbeef",
        check_runs=_BOTH_GREEN)
    assert result["tree_skip"] is False


def test_does_not_skip_when_the_head_ran_but_failed(ctsd):
    runs = [
        _run("bash", "success", "2026-09-20T02:00:00Z"),
        _run("python", "failure", "2026-09-20T02:00:00Z"),
    ]
    result = ctsd.decide_tree_skip(
        pr_number=643, head_sha="abc123", merge_tree="deadbeef", head_tree="deadbeef",
        check_runs=runs)
    assert result["tree_skip"] is False


def test_does_not_skip_when_the_head_has_no_real_run_of_one_name(ctsd):
    """Only a skipped `python` run on the head — e.g. merged straight from a
    draft with no ready-stage run — must read as missing, not as passing."""
    runs = [
        _run("bash", "success", "2026-09-20T02:00:00Z"),
        _run("python", "skipped", "2026-09-20T01:00:00Z"),
    ]
    result = ctsd.decide_tree_skip(
        pr_number=643, head_sha="abc123", merge_tree="deadbeef", head_tree="deadbeef",
        check_runs=runs)
    assert result["tree_skip"] is False


def test_does_not_skip_when_check_runs_is_empty(ctsd):
    result = ctsd.decide_tree_skip(
        pr_number=643, head_sha="abc123", merge_tree="deadbeef", head_tree="deadbeef",
        check_runs=[])
    assert result["tree_skip"] is False
