"""The ARCH-374 push guard's decision logic.

The guard denies a `git push` while a review run is still pinned to a different
commit — because ARCH-184 pins a review to a sha and ARCH-193 then discards its
stamp once the head moves, so the review completes, posts, and is thrown away.

These test the PURE FUNCTIONS. The first version of this guard was all IO, so its
only test could exercise the pass-through and never the deny — the path that
actually matters.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
HEAD = "a" * 40
OTHER = "b" * 40


@pytest.fixture(scope="module")
def guard():
    loader = importlib.machinery.SourceFileLoader(
        "live_review_check", str(REPO / "tools" / "live-review-check.py"))
    spec = importlib.util.spec_from_loader("live_review_check", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _run(**kw):
    base = {"run_id": "run-1", "agent": "reviewer", "repo": "vivy",
            "review_sha": OTHER, "ended_at": None}
    base.update(kw)
    return base


# --- who counts as a reviewer -------------------------------------------------


@pytest.mark.parametrize("agent,want", [
    ("reviewer", True), ("security-reviewer", True),
    ("hollis-reviewer", True), ("svc-security-reviewer", True),
    ("coder", False), ("bug-fixer", False), ("design-critic", False),
])
def test_review_agent_detection(guard, agent, want):
    assert guard.is_review_agent(agent) is want


# --- repo detection, the bug the guard's own test caught ----------------------


def test_repo_comes_from_the_worktree_name_not_the_basename(guard):
    """Reviewers run IN worktrees. Using the toplevel basename would yield
    "agent-platform.reviewer-xyz" and match no run's meta, missing the exact
    case this guard exists for."""
    root = pathlib.Path("/srv/convivy/Convivy")
    wt = root / "worktrees" / "agent-platform.reviewer-round-2"
    assert guard.repo_for(wt, root) == "agent-platform"


def test_repo_from_a_primary_checkout(guard):
    root = pathlib.Path("/srv/convivy/Convivy")
    assert guard.repo_for(root / "convivy-orient", root) == "convivy-orient"


def test_a_path_outside_the_root_has_no_repo(guard):
    assert guard.repo_for(pathlib.Path("/tmp/elsewhere"), pathlib.Path("/srv/convivy/Convivy")) is None


# --- which runs are doomed ----------------------------------------------------


def test_a_review_pinned_elsewhere_is_doomed(guard):
    assert len(guard.doomed_reviews([_run()], "vivy", HEAD)) == 1


def test_a_review_pinned_to_what_we_are_pushing_is_not(guard):
    """The no-op case: pushing the very commit under review does not invalidate
    it, and denying that would be noise."""
    assert guard.doomed_reviews([_run(review_sha=HEAD)], "vivy", HEAD) == []


def test_an_ended_run_is_not_doomed(guard):
    assert guard.doomed_reviews([_run(ended_at="2026-07-19T12:00:00")], "vivy", HEAD) == []


def test_another_repos_review_is_ignored(guard):
    assert guard.doomed_reviews([_run(repo="constance")], "vivy", HEAD) == []


def test_a_non_review_agent_is_ignored(guard):
    """A coder pushing its own branch is not what this guards."""
    assert guard.doomed_reviews([_run(agent="coder")], "vivy", HEAD) == []


def test_a_run_with_no_pin_is_ignored(guard):
    """An unpinned run has no stamp to invalidate."""
    assert guard.doomed_reviews([_run(review_sha=None)], "vivy", HEAD) == []


# --- the message --------------------------------------------------------------


def test_no_doomed_runs_means_no_message(guard):
    assert guard.deny_reason([], HEAD) == ""


def test_the_message_names_the_run_and_both_ways_out(guard):
    doomed = guard.doomed_reviews([_run()], "vivy", HEAD)
    msg = guard.deny_reason(doomed, HEAD)
    assert "run-1" in msg                      # which run
    assert OTHER[:9] in msg and HEAD[:9] in msg  # both commits
    assert "agentctl kill --final run-1" in msg  # the escape hatch
    assert "wait for it" in msg                  # the other way out


def test_the_kill_line_covers_every_doomed_run(guard):
    """Two reviewers dispatched together both go stale on one push; a kill line
    naming only the first would leave the other burning."""
    runs = [_run(run_id="run-1"), _run(run_id="run-2", agent="security-reviewer")]
    msg = guard.deny_reason(guard.doomed_reviews(runs, "vivy", HEAD), HEAD)
    assert "--final run-1" in msg and "--final run-2" in msg


# --- ancestry: only a push that moves PAST the reviewed commit is a problem ---
#
# Round-2 reviewer catch, and my reasoning for the original was wrong. I argued
# "a push to any branch can move a PR head." It cannot: pushing branch B does not
# move branch A's head, so a review pinned to a commit on A survives untouched.
# Denying that push would force killing a still-valid review — precisely the
# waste this guard exists to prevent.


def test_a_review_on_another_branch_is_not_doomed(guard):
    """The false positive. A live review pinned to a commit that is NOT an
    ancestor of what we are pushing is on unrelated work."""
    runs = [_run(review_sha=OTHER)]
    assert guard.doomed_reviews(runs, "vivy", HEAD, on_this_branch=lambda sha: False) == []


def test_a_review_on_this_branch_is_still_doomed(guard):
    """The case that matters: the reviewed commit IS an ancestor, so this push
    moves the head past it and ARCH-193 will discard the stamp."""
    runs = [_run(review_sha=OTHER)]
    got = guard.doomed_reviews(runs, "vivy", HEAD, on_this_branch=lambda sha: True)
    assert len(got) == 1


def test_without_a_branch_test_it_falls_back_to_repo_only(guard):
    """If git is unavailable the filter stays broad rather than silently allowing
    the case the guard exists for."""
    runs = [_run(review_sha=OTHER)]
    assert len(guard.doomed_reviews(runs, "vivy", HEAD)) == 1


def test_the_branch_test_is_not_consulted_for_an_already_excluded_run(guard):
    """A run excluded by repo, agent, ended_at or an equal sha must not reach the
    branch check — it shells out to git, and running it for already-ruled-out
    runs is wasted work on every push."""
    called = []

    def spy(sha):
        called.append(sha)
        return True

    runs = [_run(repo="constance"), _run(agent="coder"),
            _run(ended_at="2026-07-19T00:00:00"), _run(review_sha=HEAD)]
    assert guard.doomed_reviews(runs, "vivy", HEAD, on_this_branch=spy) == []
    assert called == [], f"branch check consulted for excluded runs: {called}"


def test_a_rebased_away_commit_is_still_doomed(guard):
    """Round-3 reviewer catch: the ancestry-only test traded a false positive for
    a false NEGATIVE. After a rebase the reviewed commit is rewritten out, so it
    is no longer an ancestor of HEAD — ancestry alone reads "unrelated" and
    allows a force-push that certainly invalidates the review.

    The predicate now also asks whether the commit is on the branch's PUBLISHED
    head, which stays true after a rebase drops it locally. Modelled here as the
    real predicate would answer: not on HEAD, but on this branch.
    """
    runs = [_run(review_sha=OTHER)]
    on_branch_via_upstream = lambda sha: True   # noqa: E731 — HEAD says no, upstream says yes
    got = guard.doomed_reviews(runs, "vivy", HEAD, on_this_branch=on_branch_via_upstream)
    assert len(got) == 1, "a rebase must not smuggle a doomed review past the guard"


def test_the_two_failure_directions_are_both_covered(guard):
    """The predicate has been wrong in both directions; this pins both at once.

    too broad  — a review on another branch must NOT be doomed (repo-only bug)
    too narrow — a review on this branch MUST be doomed (ancestry-only bug)
    """
    runs = [_run(run_id="other-branch", review_sha=OTHER),
            _run(run_id="this-branch", review_sha="c" * 40)]
    doomed = guard.doomed_reviews(
        runs, "vivy", HEAD,
        on_this_branch=lambda sha: sha == "c" * 40)
    assert [d["run_id"] for d in doomed] == ["this-branch"]
