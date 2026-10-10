"""Worktree allocation + cleanup (lib/agent-platform/worktrees.py).

The risks these guard: a wrong path or a missed busy-check lets two agents
allocate the same worktree and clobber each other (the failure the in_use_map
WorktreeBusy check exists to prevent); a purpose routed to the wrong branch;
and the reaper either deleting live work (debris misclassified) or never
reaping merged work (squash-merge ahead-ness — the 2026-06-09 58-worktree
buildup).
"""
from __future__ import annotations

import os as _os
import subprocess as _sp
from pathlib import Path

import pytest
import worktrees as W


def test_branch_for():
    assert W.branch_for_purpose("overnight") == "overnight-work"
    assert W.branch_for_purpose("test-xdist") == "agent/test-xdist"
    assert W.branch_for_purpose("bug-fix-login") == "agent/bug-fix-login"


def test_parse_worktree_porcelain():
    out = (
        "worktree $HOME/Convivy/vivy\nHEAD abc123\nbranch refs/heads/main\n\n"
        "worktree $HOME/Convivy/worktrees/vivy.overnight\nHEAD def456\n"
        "branch refs/heads/overnight-work\n\n"
        "worktree $HOME/Convivy/worktrees/vivy.detached\nHEAD 999aaa\ndetached\n\n"
    )
    recs = W._parse_worktree_porcelain(out)
    assert len(recs) == 3
    assert recs[0]["worktree"].endswith("/vivy")
    assert W._branch_name(recs[0]) == "main"
    assert W._branch_name(recs[1]) == "overnight-work"
    assert W._branch_name(recs[2]) is None  # detached


@pytest.fixture
def fake_convivy(tmp_path, monkeypatch):
    """Repoint worktrees.CONVIVY_ROOT at a tmp dir with one fake primary repo."""
    monkeypatch.setattr(W, "CONVIVY_ROOT", tmp_path)
    (tmp_path / "vivy" / ".git").mkdir(parents=True)
    return tmp_path


def test_desired_path(fake_convivy):
    # primary (no purpose) is the canonical clone
    assert W.desired_path("vivy", None) == fake_convivy / "vivy"
    # purpose-ed worktrees live under ~/Convivy/worktrees/<repo>.<purpose>
    assert W.desired_path("vivy", "overnight") == fake_convivy / "worktrees" / "vivy.overnight"
    assert W.desired_path("vivy", "test-xdist") == fake_convivy / "worktrees" / "vivy.test-xdist"


def test_desired_path_unknown_repo_raises(fake_convivy):
    with pytest.raises(ValueError, match="unknown repo"):
        W.desired_path("nope", None)


def test_allocate_refuses_busy_worktree(fake_convivy, monkeypatch):
    target = W.desired_path("vivy", "overnight")
    holder = {"run_id": "run-x", "agent": "overnight-orchestrator", "worktree": str(target)}
    monkeypatch.setattr(W, "in_use_map", lambda: {target: holder})
    with pytest.raises(W.WorktreeBusy) as ei:
        W.allocate_worktree("vivy", "overnight")
    assert ei.value.holder["run_id"] == "run-x"


# --- cleanup classification ---------------------------------------------------


def test_is_debris():
    # disposable: venvs, dep dirs, caches, root pr-body.md, coverage droppings
    assert W._is_debris(".venv/")
    assert W._is_debris("frontend/node_modules/")
    assert W._is_debris("app/__pycache__/")
    assert W._is_debris(".pytest_cache/")
    assert W._is_debris("pr-body.md")
    assert W._is_debris(".coverage")
    assert W._is_debris(".coverage.mini.1234")
    assert W._is_debris(".DS_Store")
    assert W._is_debris("pkg/foo.egg-info/")
    # The DOTLESS coverage artifacts. `pytest --cov-report=json` writes coverage.json,
    # and vivy/constance do not gitignore it, so it lands as an untracked file in every
    # review worktree there. It used to be classified as real work (the `.coverage*`
    # prefix rule misses it), which under ARCH-344 would keep every one of those trees
    # forever and silently re-open the leak the reap exists to close. It is regenerable
    # CI output, not authored work.
    assert W._is_debris("coverage.json")
    assert W._is_debris("coverage.xml")
    assert W._is_debris("htmlcov/index.html")
    # .build — Swift build output dir, same class as node_modules
    assert W._is_debris(".build/")
    assert W._is_debris("frontend/.build/")
    # delivered review artifacts at the worktree root are debris, matched by pattern.
    # The platform mandates per-PR per-round reviewer filenames, so an exact-name set
    # can never track what the naming convention produces; this pattern covers all
    # observed variants and future invocations.
    for fname in [
        "explanation.md", "review.md", "review-comment.md",
        "review-round3.md", "review-round-2.md",
        "review-pr16.md", "review_pr306.md",
        "review-304.md", "review-18.md",
        "pr-523-review.md",
        "security-review-comment.md", "security-review-pr8.md",
        "security-review-561.md", "security-review-558-r2.md",
        "security-review-309.md", "security-review-307.md",
        "slack-post-pr558.md", "slack-post-pr556.md", "slack-post-pr552.md",
        "slack-post.md",
        # FOUND BY SCANNING THE LIVE POOL rather than by reading the convention.
        # An earlier draft of the anchored pattern excluded all three, which would
        # have re-opened the leak this whole change exists to close: a round suffix
        # after a PR number, and a slack-post name whose number carries no `pr`.
        "security-review-pr14-round1.md", "slack-post-546.md", "slack-post-547.md",
    ]:
        assert W._is_debris(fname), f"{fname!r} should be debris"
    # real work: source files, docs, and pr-body.md anywhere but the root
    assert not W._is_debris("app/main.py")
    assert not W._is_debris("docs/pr-body.md")
    assert not W._is_debris("notes.md")
    # the prefix rule must still not over-match a source file that merely starts with
    # the word "coverage" — that IS work.
    assert not W._is_debris("app/coverage_report.py")
    # The pattern is root-only: it applies ONLY via the "/" not in path guard in
    # _is_debris. So a nested docs/explanation.md is NOT debris (it's someone's source
    # doc), while root-level explanation.md IS (a delivered scratch artifact).
    assert not W._is_debris("docs/explanation.md")
    assert not W._is_debris("app/review-comment.md")
    assert not W._is_debris("app/security-review-comment.md")
    # Generic .md files at the root are real work, not debris.
    assert not W._is_debris("plan.md")
    assert not W._is_debris("docs.md")
    # The narrowed pattern intentionally excludes plausible-draft-name collisions:
    # a bare prefix (no compound form) matches nothing, and even review- followed by a
    # non-artifact word like "notes" or "of" is not debris.
    assert not W._is_debris("reviewers-guide.md")
    assert not W._is_debris("review-of-our-hiring-process.md")
    # A PREFIX MAY NOT CARRY ARBITRARY TEXT AFTER IT. Each branch of the pattern
    # runs to `.md$`, so a name that merely STARTS like a generated artifact is
    # real work. Every one of these matched before the anchors went in, and
    # `_is_debris` is the last check before the file is deleted for good.
    assert not W._is_debris("review-1-of-3.md")
    assert not W._is_debris("review-123-draft.md")
    assert not W._is_debris("review-comment-on-the-hiring-process.md")
    assert not W._is_debris("security-review-of-approach.md")
    assert not W._is_debris("slack-post-draft.md")
    assert not W._is_debris("slack-post-announcement.md")
    # THE ACCEPTED RESIDUAL, asserted so it is a decision rather than an oversight.
    # A four-digit run cannot be excluded without dropping real artifacts: PR
    # numbers passed 999 (convivy/vivy is on #1114), so review-1114.md is a name
    # the platform generates. review-2024.md collides with that shape.
    assert W._is_debris("review-1114.md")
    assert W._is_debris("review-2024.md"), "the residual named in worktrees.py"
    assert not W._is_debris("review-notes.md")
    # What excludes review-proposal.md (and review-prolog, etc.) is the digit requirement:
    # _NUM ends in \d+, and "proposal" has no digits — the optional pr prefix is not the guard.
    assert not W._is_debris("review-proposal.md")
    # review-round3-r2.md is a deliberate non-match: the round\d+ branch already carries
    # the round number in the word, so _ROUND is not appended to it (stacking -r2 would be
    # redundant). Pinned so the asymmetry is a decision, not an oversight.
    assert not W._is_debris("review-round3-r2.md")
    # explanation removed entirely from the pattern — only the exact name is debris.
    assert not W._is_debris("explanation-of-the-architecture.md")
    assert not W._is_debris("explanations.md")
    # _DEBRIS_DIRS (.build, node_modules, …) matches at ANY depth — same as the
    # line-172 `any(p in _DEBRIS_DIRS … for p in parts)`. So a path nested under
    # .build/ IS debris, exactly as frontend/node_modules/ is.
    assert W._is_debris("src/.build/foo.swift")


def _wt(label, *, branch="agent/x", dirty=0, real_dirty=0, ahead=0, locked=False):
    return W.Worktree(
        path=Path(f"/tmp/{label}"), primary=Path("/tmp/repo"), label=label,
        branch=branch, is_primary=False, locked=locked, dirty_files=dirty,
        real_dirty_files=real_dirty, ahead=ahead, behind=0, base_branch="main",
    )


def test_cleanup_candidates_classification(monkeypatch):
    held = _wt("held")
    wts = [
        held,
        _wt("clean"),                                    # at base, no dirt
        _wt("debris-only", dirty=2, real_dirty=0),       # venv/pr-body.md only
        _wt("real-dirty", dirty=3, real_dirty=1),        # has uncommitted work
        _wt("merged-ahead", ahead=2),                    # squash-merged PR
        _wt("live-ahead", ahead=1),                      # unmerged commits
        _wt("detached-ahead", branch=None, ahead=1),     # no branch → no PR check
        _wt("locked-merged", ahead=2, locked=True),      # session worktree — hands off
    ]
    monkeypatch.setattr(W, "all_worktrees", lambda *, base_branch, **_kw: wts)
    monkeypatch.setattr(W, "is_dispatched", lambda p: True)
    monkeypatch.setattr(W, "in_use_map", lambda: {held.path.resolve(): {"run_id": "run-x"}})
    monkeypatch.setattr(W, "pr_merged", lambda wt: wt.label in {"merged-ahead", "locked-merged"})
    # Round 8: cleanup_candidates now calls unpublished_commits for every candidate
    # it would otherwise call removable, not only an `ahead` one — so "clean" and
    # "debris-only" (never ahead of base) reach this call too, and must read 0 the
    # way a tree with nothing new authored genuinely would.
    monkeypatch.setattr(W, "unpublished_commits",
                        lambda path: 0 if path.name in ("clean", "debris-only") else 1)
    reasons = {c.worktree.label: c.reason for c in W.cleanup_candidates()}
    assert reasons == {
        "held": "skip:in_use",
        "clean": "removable",
        "debris-only": "removable",
        "real-dirty": "skip:dirty",
        "merged-ahead": "removable:pr-merged",
        "live-ahead": "skip:ahead",
        "detached-ahead": "skip:ahead",
        "locked-merged": "skip:locked",
    }


def test_cleanup_candidates_checks_unpublished_commits_even_when_not_ahead(monkeypatch):
    """Round 8 (reviewer round 7, blocking): `wt.ahead`/`wt.real_dirty_files` came
    from `all_worktrees`'s scan, which ran before this loop could import a dead
    run's leftover private store — so a HEAD that store alone could resolve reads
    as "(0, 0), not ahead" from the SAME git error `_ahead_behind`'s docstring
    documents, not from genuinely having nothing new. A plain, never-ahead
    "removable" classification must still run the fail-closed unpublished_commits
    check rather than trust `wt.ahead` as a reason to skip calling it."""
    called = []
    wt = _wt("plain")
    monkeypatch.setattr(W, "all_worktrees", lambda *, base_branch, **_kw: [wt])
    monkeypatch.setattr(W, "is_dispatched", lambda p: True)
    monkeypatch.setattr(W, "in_use_map", lambda: {})
    monkeypatch.setattr(W, "unpublished_commits", lambda path: called.append(path) or 0)
    reasons = {c.worktree.label: c.reason for c in W.cleanup_candidates()}
    assert called == [wt.path]
    assert reasons["plain"] == "removable"


def test_cleanup_detached_published_is_removable(monkeypatch):
    """A detached-HEAD tree whose commits all exist on a remote is reclaimable.

    The 2026-07-23 fleet had 90 of 274 trees in this state, all stuck at
    skip:ahead because pr_merged structurally cannot handle detached HEAD."""
    wt = _wt("detached-published", branch=None, ahead=1)
    monkeypatch.setattr(W, "all_worktrees", lambda *, base_branch, **_kw: [wt])
    monkeypatch.setattr(W, "is_dispatched", lambda p: True)
    monkeypatch.setattr(W, "in_use_map", lambda: {})
    monkeypatch.setattr(W, "unpublished_commits", lambda path: 0)
    reasons = {c.worktree.label: c.reason for c in W.cleanup_candidates()}
    assert reasons["detached-published"] == "removable:published"


def test_cleanup_detached_unpublished_is_skipped(monkeypatch):
    wt = _wt("detached-unpublished", branch=None, ahead=1)
    monkeypatch.setattr(W, "all_worktrees", lambda *, base_branch, **_kw: [wt])
    monkeypatch.setattr(W, "is_dispatched", lambda p: True)
    monkeypatch.setattr(W, "in_use_map", lambda: {})
    monkeypatch.setattr(W, "unpublished_commits", lambda path: 1)
    reasons = {c.worktree.label: c.reason for c in W.cleanup_candidates()}
    assert reasons["detached-unpublished"] == "skip:ahead"


def test_cleanup_unpublished_commits_error_is_skipped(monkeypatch):
    """A tree git cannot read is never removed — the raises-not-0 contract."""
    import subprocess as _sp
    wt = _wt("unreadable", ahead=1)
    monkeypatch.setattr(W, "all_worktrees", lambda *, base_branch, **_kw: [wt])
    monkeypatch.setattr(W, "is_dispatched", lambda p: True)
    monkeypatch.setattr(W, "in_use_map", lambda: {})

    def _raises(_path):
        raise _sp.CalledProcessError(128, "git")

    monkeypatch.setattr(W, "unpublished_commits", _raises)
    reasons = {c.worktree.label: c.reason for c in W.cleanup_candidates()}
    assert reasons["unreadable"] == "skip:error"


def test_cleanup_squash_merged_still_reclaimed_via_pr_merged(monkeypatch):
    """A squash-merged branch has locally unpublished commits (the originals were
    squashed into one new commit on the remote). unpublished_commits > 0, so
    the published path can't reclaim it — but pr_merged can."""
    wt = _wt("squash-merged", ahead=2)
    monkeypatch.setattr(W, "all_worktrees", lambda *, base_branch, **_kw: [wt])
    monkeypatch.setattr(W, "is_dispatched", lambda p: True)
    monkeypatch.setattr(W, "in_use_map", lambda: {})
    monkeypatch.setattr(W, "unpublished_commits", lambda path: 2)
    monkeypatch.setattr(W, "pr_merged", lambda w: True)
    reasons = {c.worktree.label: c.reason for c in W.cleanup_candidates()}
    assert reasons["squash-merged"] == "removable:pr-merged"


# --- venv symlink -----------------------------------------------------------


def test_link_primary_venv_creates_symlink(fake_convivy):
    """_link_primary_venv symlinks a primary .venv into the worktree."""
    primary = fake_convivy / "vivy"
    (primary / ".venv" / "bin").mkdir(parents=True)
    target = fake_convivy / "worktrees" / "vivy.test"
    target.mkdir(parents=True)

    W._link_primary_venv(primary, target)

    symlink = target / ".venv"
    assert symlink.is_symlink()
    assert symlink.resolve() == (primary / ".venv").resolve()


def test_link_primary_venv_skips_when_primary_lacks_venv(fake_convivy):
    """No symlink when the primary has no .venv (e.g. repos without a venv)."""
    primary = fake_convivy / "vivy"
    target = fake_convivy / "worktrees" / "vivy.test"
    target.mkdir(parents=True)

    W._link_primary_venv(primary, target)

    assert not (target / ".venv").exists()


def test_link_primary_venv_replaces_stale_symlink(fake_convivy):
    """A stale/broken symlink is replaced with a fresh one."""
    primary = fake_convivy / "vivy"
    (primary / ".venv" / "bin").mkdir(parents=True)
    stale_target = fake_convivy / "nonexistent" / ".venv"
    stale_target.parent.mkdir(parents=True)
    target = fake_convivy / "worktrees" / "vivy.test"
    target.mkdir(parents=True)
    (target / ".venv").symlink_to(str(stale_target), target_is_directory=True)
    assert not (target / ".venv").resolve().exists()  # broken

    W._link_primary_venv(primary, target)

    symlink = target / ".venv"
    assert symlink.is_symlink()
    assert symlink.resolve() == (primary / ".venv").resolve()


def test_link_primary_venv_skips_on_correct_symlink(fake_convivy):
    """Already-correct symlink is a no-op (no unnecessary unlink/re-link)."""
    primary = fake_convivy / "vivy"
    (primary / ".venv" / "bin").mkdir(parents=True)
    target = fake_convivy / "worktrees" / "vivy.test"
    target.mkdir(parents=True)
    (target / ".venv").symlink_to(str(primary / ".venv"), target_is_directory=True)

    W._link_primary_venv(primary, target)

    symlink = target / ".venv"
    assert symlink.is_symlink()
    assert symlink.resolve() == (primary / ".venv").resolve()


def test_link_primary_venv_does_not_touch_actual_directory(fake_convivy):
    """If a real .venv directory already exists in the target (should never
    happen in agentctl worktrees, but guard against it), do not remove it."""
    primary = fake_convivy / "vivy"
    (primary / ".venv" / "bin").mkdir(parents=True)
    target = fake_convivy / "worktrees" / "vivy.test"
    target.mkdir(parents=True)
    (target / ".venv").mkdir()  # real directory, not a symlink
    marker = target / ".venv" / "IM_A_REAL_DIR"
    marker.write_text("")

    W._link_primary_venv(primary, target)

    # Real directory still there, not replaced by symlink
    assert not (target / ".venv").is_symlink()
    assert marker.exists()

def test_cleanup_skip_precedence_unchanged(monkeypatch):
    """Skip checks fire in order: in_use > locked > dirty > ahead."""
    locked_ahead = _wt("locked-ahead", ahead=2, locked=True)
    held_dirty = _wt("held-dirty", real_dirty=1)
    wts = [locked_ahead, held_dirty]
    monkeypatch.setattr(W, "all_worktrees", lambda *, base_branch, **_kw: wts)
    monkeypatch.setattr(W, "is_dispatched", lambda p: True)
    monkeypatch.setattr(W, "in_use_map",
                        lambda: {held_dirty.path.resolve(): {"run_id": "x"}})
    monkeypatch.setattr(W, "unpublished_commits", lambda path: 0)
    monkeypatch.setattr(W, "pr_merged", lambda w: True)
    reasons = {c.worktree.label: c.reason for c in W.cleanup_candidates()}
    assert reasons["locked-ahead"] == "skip:locked"
    assert reasons["held-dirty"] == "skip:in_use"


def test_recheck_candidate_catches_hold(monkeypatch):
    wt = _wt("will-be-held")
    c = W.CleanupCandidate(wt, "removable")
    monkeypatch.setattr(W, "in_use_map",
                        lambda: {wt.path.resolve(): {"run_id": "new-run"}})
    assert W.recheck_candidate(c) == ("skip:in_use", 0)


def test_recheck_candidate_catches_lock(monkeypatch):
    wt = _wt("will-be-locked")
    c = W.CleanupCandidate(wt, "removable")
    locked_wt = _wt("will-be-locked", locked=True)
    monkeypatch.setattr(W, "in_use_map", lambda: {})
    monkeypatch.setattr(W, "list_worktrees", lambda primary, **kw: [locked_wt])
    assert W.recheck_candidate(c) == ("skip:locked", 0)


def test_recheck_candidate_vanished_is_skipped(monkeypatch):
    wt = _wt("vanished")
    c = W.CleanupCandidate(wt, "removable")
    monkeypatch.setattr(W, "in_use_map", lambda: {})
    monkeypatch.setattr(W, "list_worktrees", lambda primary, **kw: [])
    assert W.recheck_candidate(c) == ("skip:vanished", 0)


def test_recheck_candidate_safe_returns_none_and_fresh_dirty(monkeypatch):
    """(None, fresh_dirty_files) is the safe-to-remove contract; caller uses fresh_dirty for --force."""
    wt = _wt("safe", dirty=3, real_dirty=0)  # stale snapshot: 3 debris files
    c = W.CleanupCandidate(wt, "removable")
    fresh_wt = _wt("safe", dirty=5, real_dirty=0)  # fresh: debris grew since classification
    monkeypatch.setattr(W, "in_use_map", lambda: {})
    monkeypatch.setattr(W, "list_worktrees", lambda primary, **kw: [fresh_wt])
    monkeypatch.setattr(W, "unpublished_commits", lambda path: 0)
    skip_reason, fresh_dirty = W.recheck_candidate(c)
    assert skip_reason is None
    assert fresh_dirty == 5  # fresh count, not the stale snapshot (3)


def test_recheck_candidate_catches_real_dirty(monkeypatch):
    """A real tracked-file modification appearing after classification is caught."""
    wt = _wt("will-get-dirty")
    c = W.CleanupCandidate(wt, "removable")
    fresh_wt = _wt("will-get-dirty", dirty=1, real_dirty=1)
    monkeypatch.setattr(W, "in_use_map", lambda: {})
    monkeypatch.setattr(W, "list_worktrees", lambda primary, **kw: [fresh_wt])
    assert W.recheck_candidate(c) == ("skip:dirty", 0)


def test_recheck_candidate_catches_new_commits_on_published(monkeypatch):
    """A removable:published tree that gains local commits before removal is caught."""
    wt = _wt("published", ahead=1)
    c = W.CleanupCandidate(wt, "removable:published")
    monkeypatch.setattr(W, "in_use_map", lambda: {})
    monkeypatch.setattr(W, "list_worktrees", lambda primary, **kw: [wt])
    monkeypatch.setattr(W, "unpublished_commits", lambda path: 1)
    assert W.recheck_candidate(c) == ("skip:ahead", 0)


def test_recheck_candidate_published_commits_error_is_skipped(monkeypatch):
    """The raises-not-0 contract on unpublished_commits is preserved in recheck."""
    import subprocess as _sp
    wt = _wt("published-unreadable", ahead=1)
    c = W.CleanupCandidate(wt, "removable:published")
    monkeypatch.setattr(W, "in_use_map", lambda: {})
    monkeypatch.setattr(W, "list_worktrees", lambda primary, **kw: [wt])

    def _raises(_path):
        raise _sp.CalledProcessError(128, "git")

    monkeypatch.setattr(W, "unpublished_commits", _raises)
    assert W.recheck_candidate(c) == ("skip:error", 0)


def test_recheck_candidate_published_still_zero_is_safe(monkeypatch):
    """A removable:published tree whose unpublished count is still 0 remains removable."""
    wt = _wt("still-published", ahead=1)
    c = W.CleanupCandidate(wt, "removable:published")
    monkeypatch.setattr(W, "in_use_map", lambda: {})
    monkeypatch.setattr(W, "list_worktrees", lambda primary, **kw: [wt])
    monkeypatch.setattr(W, "unpublished_commits", lambda path: 0)
    skip_reason, fresh_dirty = W.recheck_candidate(c)
    assert skip_reason is None


def test_recheck_candidate_checks_unpublished_commits_for_plain_removable_too(monkeypatch):
    """Round 8: unpublished_commits is now re-run for EVERY reason, not only
    removable:published — the same widening cleanup_candidates makes, and for the
    same reason: a plain "removable" classification may have been reached because
    `wt.ahead` read a git error as "not ahead" rather than a genuine zero."""
    called = []
    wt = _wt("plain-removable")
    c = W.CleanupCandidate(wt, "removable")
    monkeypatch.setattr(W, "in_use_map", lambda: {})
    monkeypatch.setattr(W, "list_worktrees", lambda primary, **kw: [wt])
    monkeypatch.setattr(W, "unpublished_commits", lambda path: called.append(path) or 999)
    skip_reason, _ = W.recheck_candidate(c)
    assert called == [wt.path.resolve()]
    assert skip_reason == "skip:ahead"


def test_recheck_candidate_ignores_unpublished_commits_for_pr_merged(monkeypatch):
    """A removable:pr-merged tree is EXPECTED to have unpublished commits — a squash
    rewrites them away on the remote — so a positive count there is not a state
    change, unlike every other reason. unpublished_commits is still called (the
    fail-closed check runs either way), just not acted on the same way."""
    called = []
    wt = _wt("squash-merged", ahead=2)
    c = W.CleanupCandidate(wt, "removable:pr-merged")
    monkeypatch.setattr(W, "in_use_map", lambda: {})
    monkeypatch.setattr(W, "list_worktrees", lambda primary, **kw: [wt])
    monkeypatch.setattr(W, "unpublished_commits", lambda path: called.append(path) or 2)
    skip_reason, _ = W.recheck_candidate(c)
    assert called == [wt.path.resolve()]
    assert skip_reason is None


def test_pr_merged_no_gh_or_branch(monkeypatch):
    # no branch → never merged; no gh on the machine → fail safe (don't reap)
    assert W.pr_merged(_wt("detached", branch=None)) is False
    monkeypatch.setattr(W, "_gh_path", lambda: None)
    assert W.pr_merged(_wt("x", ahead=1)) is False


def test_dirty_counts_in_real_repo(tmp_path):
    import subprocess
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "tracked.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "init"], check=True)
    (tmp_path / "pr-body.md").write_text("body\n")            # debris
    (tmp_path / ".venv").mkdir()                              # debris
    (tmp_path / ".venv" / "cfg").write_text("")
    (tmp_path / "tracked.py").write_text("x = 2\n")           # real modification
    total, real = W._dirty_counts(tmp_path)
    assert total == 3
    assert real == 1


def test_new_debris_entries_in_real_repo(tmp_path):
    """Pin each new debris name: .build dirs, root scratch artifacts.

    A tree held only by .build or root scratch files is removable (real_dirty=0).
    A tree with a MODIFIED TRACKED file + these debris files is preserved.
    A nested file of a debris name does NOT make a tree removable."""
    import subprocess
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "tracked.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "init"], check=True)

    # .build dir alone → removable (real_dirty=0)
    (tmp_path / ".build").mkdir()
    (tmp_path / ".build" / "foo.o").write_text("obj\n")
    total, real = W._dirty_counts(tmp_path)
    # `git status --porcelain` collapses an entirely-untracked directory to a single
    # entry (?? .build/), so 1 file inside → total == 1, not 2.
    assert total == 1, f"expected 1 (collapsed dir entry), got {total}"
    assert real == 0, f".build alone should be debris, got real_dirty={real}"

    # .build + root scratch files alone → removable
    (tmp_path / "explanation.md").write_text("explainer output\n")
    (tmp_path / "review-comment.md").write_text("review\n")
    total, real = W._dirty_counts(tmp_path)
    assert total == 3
    assert real == 0, f"all debris (new root files + .build), got real_dirty={real}"

    # Same debris + a MODIFIED TRACKED file → preserved (real_dirty > 0)
    (tmp_path / "tracked.py").write_text("x = 2\n")
    total, real = W._dirty_counts(tmp_path)
    assert total == 4, f"expected 4 (3 untracked + 1 modified tracked), got {total}"
    assert real == 1, f"modified tracked file must preserve tree, got real_dirty={real}"

    # Replace tracked.py and add nested debris-name files → preserved
    subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qam", "restore"], check=True)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "explanation.md").write_text("source doc, not debris\n")
    total, real = W._dirty_counts(tmp_path)
    assert total == 4  # 3 old untracked (.build/, explanation.md, review-comment.md) + 1 nested
    assert real == 1, "nested explanation.md is NOT root-level — must count as real"

    # unlanded_entries must agree with _dirty_counts on the same tree
    entries = W.unlanded_entries(tmp_path)
    assert len(entries) == 1, (
        f"only the nested explanation.md should appear in unlanded_entries, "
        f"got {len(entries)}: {entries}")


# --- commit-pinned worktrees (ARCH-184, review integrity) -------------------

def _real_repo_with_two_commits(tmp_path, monkeypatch):
    """A real git primary at <root>/vivy with two commits; returns (c1, c2)."""
    import subprocess
    monkeypatch.setattr(W, "CONVIVY_ROOT", tmp_path)
    monkeypatch.setattr(W, "in_use_map", lambda: {})
    primary = tmp_path / "vivy"
    primary.mkdir()

    def git(*a):
        return subprocess.run(
            ["git", "-C", str(primary), "-c", "user.email=t@t", "-c", "user.name=t", *a],
            check=True, capture_output=True, text=True)

    git("init", "-q", "-b", "main")
    (primary / "f.py").write_text("v = 1\n")
    git("add", ".")
    git("commit", "-qm", "c1")
    c1 = git("rev-parse", "HEAD").stdout.strip()
    (primary / "f.py").write_text("v = 2\n")
    git("commit", "-qam", "c2")
    c2 = git("rev-parse", "HEAD").stdout.strip()
    return c1, c2


def _head_of(path):
    import subprocess
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()


def test_allocate_pins_new_worktree_to_commit(tmp_path, monkeypatch):
    c1, c2 = _real_repo_with_two_commits(tmp_path, monkeypatch)
    wt = W.allocate_worktree("vivy", "reviewer-x", commit=c1)
    assert _head_of(wt) == c1  # detached at the pinned (older) commit, not main's tip


def test_allocate_reuse_force_resets_to_commit(tmp_path, monkeypatch):
    # The stale-review fix: a reused review worktree must be forced to the new
    # head, not returned as-is at its round-1 HEAD.
    c1, c2 = _real_repo_with_two_commits(tmp_path, monkeypatch)
    wt1 = W.allocate_worktree("vivy", "reviewer-x", commit=c1)
    assert _head_of(wt1) == c1
    wt2 = W.allocate_worktree("vivy", "reviewer-x", commit=c2)  # same purpose → reuse
    assert wt2 == wt1 and _head_of(wt2) == c2  # force-reset to the new head


def test_allocate_reuse_pin_discards_local_dirt(tmp_path, monkeypatch):
    c1, c2 = _real_repo_with_two_commits(tmp_path, monkeypatch)
    wt = W.allocate_worktree("vivy", "reviewer-x", commit=c1)
    (wt / "f.py").write_text("tampered\n")        # a stray edit in the review tree
    (wt / "junk.txt").write_text("debris\n")
    W.allocate_worktree("vivy", "reviewer-x", commit=c2)  # re-pin
    assert _head_of(wt) == c2
    assert (wt / "f.py").read_text() == "v = 2\n"  # reset --hard restored tracked
    assert not (wt / "junk.txt").exists()          # clean -fdx removed untracked


def test_pin_refused_on_primary(tmp_path, monkeypatch):
    c1, _ = _real_repo_with_two_commits(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="primary"):
        W.allocate_worktree("vivy", None, commit=c1)  # never hard-reset the human's tree


def test_pin_bogus_commit_raises(tmp_path, monkeypatch):
    _real_repo_with_two_commits(tmp_path, monkeypatch)
    with pytest.raises(W.WorktreePinError):
        W.allocate_worktree("vivy", "reviewer-y", commit="deadbeef" * 5)


# --- PR-branch reuse (ARCH-347, the round-fix divergence fix) ----------------

def _repo_with_origin_and_pr_branch(tmp_path, monkeypatch, branch="agent/coder-fix-x"):
    """A real `vivy` primary with an `origin` remote and an existing PR branch pushed to
    it — the shape allocate_on_branch operates on. Returns (primary, branch, pr_tip).

    The local branch ref is deleted after push so the branch lives ONLY on origin, which
    is the reaped-between-rounds case that made "just reuse the original tree" insufficient
    (verified live: some open PRs keep their tree, some don't)."""
    import subprocess
    monkeypatch.setattr(W, "CONVIVY_ROOT", tmp_path)
    monkeypatch.setattr(W, "in_use_map", lambda: {})
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    primary = tmp_path / "vivy"
    subprocess.run(["git", "clone", "-q", str(origin), str(primary)], check=True)

    def g(*a):
        return subprocess.run(
            ["git", "-C", str(primary), "-c", "user.email=t@t", "-c", "user.name=t", *a],
            check=True, capture_output=True, text=True)

    (primary / "f.py").write_text("v = 1\n")
    g("add", ".")
    g("commit", "-qm", "c1")
    g("push", "-q", "origin", "main")
    g("checkout", "-q", "-b", branch)
    (primary / "f.py").write_text("v = 2\n")
    g("commit", "-qam", "pr head")
    g("push", "-q", "origin", branch)
    pr_tip = g("rev-parse", "HEAD").stdout.strip()
    g("checkout", "-q", "main")
    g("branch", "-qD", branch)            # branch now lives only on origin
    return primary, branch, pr_tip


def test_allocate_on_branch_creates_on_pr_branch_at_origin_tip(tmp_path, monkeypatch):
    primary, branch, pr_tip = _repo_with_origin_and_pr_branch(tmp_path, monkeypatch)
    assert W.find_worktree_on_branch("vivy", branch) is None  # no tree holds it yet
    wt = W.allocate_on_branch("vivy", branch)
    assert _head_of(wt) == pr_tip                              # at the live PR tip
    assert W.find_worktree_on_branch("vivy", branch) == wt     # and now findable


def test_allocate_on_branch_reuses_the_same_tree(tmp_path, monkeypatch):
    primary, branch, pr_tip = _repo_with_origin_and_pr_branch(tmp_path, monkeypatch)
    wt1 = W.allocate_on_branch("vivy", branch)
    wt2 = W.allocate_on_branch("vivy", branch)   # a round-2 fix reuses, never forks
    assert wt2 == wt1


def test_allocate_on_branch_resets_reused_tree_to_new_origin_tip(tmp_path, monkeypatch):
    """A reused tree that is BEHIND (a landed round-fix advanced the PR from elsewhere)
    must land on the PR's CURRENT head, not the prior round's — the same integrity
    guarantee the review pin makes. The tree stays clean, so the clobber-guard is silent."""
    import subprocess
    primary, branch, pr_tip = _repo_with_origin_and_pr_branch(tmp_path, monkeypatch)
    wt = W.allocate_on_branch("vivy", branch)
    assert _head_of(wt) == pr_tip

    # Advance the PR branch on origin from a SEPARATE clone, leaving `wt` behind + clean.
    origin = tmp_path / "origin.git"
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", "-b", branch, str(origin), str(other)], check=True)

    def g(cwd, *a):
        return subprocess.run(
            ["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *a],
            check=True, capture_output=True, text=True)

    (other / "f.py").write_text("v = 3\n")
    g(other, "commit", "-qam", "round 2")
    g(other, "push", "-q", "origin", branch)
    new_tip = g(other, "rev-parse", "HEAD").stdout.strip()
    assert new_tip != pr_tip

    wt2 = W.allocate_on_branch("vivy", branch)       # reuse — must ff to the new tip
    assert wt2 == wt and _head_of(wt2) == new_tip
    assert (wt2 / "f.py").read_text() == "v = 3\n"


def _plant_branch_decoys(primary, branch, sha):
    """A tag AND a local branch, both literally named `origin/<branch>`. git reads a
    tag, then a local branch, before refs/remotes/origin/<branch> for that short name."""
    import subprocess
    for kind in ("tag", "branch"):
        subprocess.run(["git", "-C", str(primary), kind, f"origin/{branch}", sha],
                       check=True, capture_output=True)


def test_allocate_on_branch_creates_at_the_exact_remote_tracking_tip(tmp_path, monkeypatch):
    """The create path built the worktree from `origin/<branch>`, which a decoy tag on
    any other commit steered; it now starts from the sha the exact ref holds."""
    import subprocess
    primary, branch, pr_tip = _repo_with_origin_and_pr_branch(tmp_path, monkeypatch)
    forged = subprocess.run(["git", "-C", str(primary), "rev-parse", "main"],
                            capture_output=True, text=True, check=True).stdout.strip()
    assert forged != pr_tip
    _plant_branch_decoys(primary, branch, forged)
    wt = W.allocate_on_branch("vivy", branch)
    assert _head_of(wt) == pr_tip
    upstream = subprocess.run(
        ["git", "-C", str(wt), "for-each-ref", "--format=%(upstream)", f"refs/heads/{branch}"],
        capture_output=True, text=True).stdout.strip()
    assert upstream == f"refs/remotes/origin/{branch}", "the new branch still tracks its remote"


def test_allocate_on_branch_refuses_a_tag_standing_in_for_an_absent_remote_ref(
        tmp_path, monkeypatch):
    """origin does not have the branch, so the fetch leaves refs/remotes/origin/<branch>
    absent. `rev-parse refs/remotes/origin/<branch>` then falls through to the tag
    `refs/tags/refs/remotes/origin/<branch>` and would place a worktree on a forged tip."""
    import subprocess
    primary, branch, pr_tip = _repo_with_origin_and_pr_branch(tmp_path, monkeypatch)
    subprocess.run(["git", "-C", str(tmp_path / "origin.git"), "branch", "-qD", branch], check=True)
    subprocess.run(["git", "-C", str(primary), "update-ref", "-d",
                    f"refs/remotes/origin/{branch}"], check=True)
    for kind in ("tag", "branch"):
        subprocess.run(["git", "-C", str(primary), kind, f"refs/remotes/origin/{branch}", pr_tip],
                       check=True, capture_output=True)
    absent = subprocess.run(["git", "-C", str(primary), "show-ref", "--verify", "-q",
                             f"refs/remotes/origin/{branch}"])
    assert absent.returncode != 0
    stands_in = subprocess.run(["git", "-C", str(primary), "rev-parse",
                                f"refs/remotes/origin/{branch}"],
                               capture_output=True, text=True).stdout.strip()
    assert stands_in == pr_tip, "the fixture must make the qualified name resolve, or it tests nothing"
    with pytest.raises(W.WorktreePinError, match="not found after fetch"):
        W.allocate_on_branch("vivy", branch)
    assert W.find_worktree_on_branch("vivy", branch) is None


def test_allocate_on_branch_reset_takes_the_exact_remote_tracking_tip(tmp_path, monkeypatch):
    """The reuse path's `reset --hard` and its ahead-count read `origin/<branch>`."""
    import subprocess
    primary, branch, pr_tip = _repo_with_origin_and_pr_branch(tmp_path, monkeypatch)
    wt = W.allocate_on_branch("vivy", branch)
    origin = tmp_path / "origin.git"
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", "-b", branch, str(origin), str(other)], check=True)

    def g(cwd, *a):
        return subprocess.run(
            ["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *a],
            check=True, capture_output=True, text=True)

    (other / "f.py").write_text("v = 3\n")
    g(other, "commit", "-qam", "round 2")
    g(other, "push", "-q", "origin", branch)
    new_tip = g(other, "rev-parse", "HEAD").stdout.strip()
    _plant_branch_decoys(primary, branch, pr_tip)      # the stale tip, as a tag and a branch
    wt2 = W.allocate_on_branch("vivy", branch)
    assert wt2 == wt
    assert _head_of(wt2) == new_tip
    assert (wt2 / "f.py").read_text() == "v = 3\n"


def test_allocate_on_branch_refuses_to_clobber_unlanded_work(tmp_path, monkeypatch):
    """The guardrail: a reused tree carrying uncommitted work is NOT reset away. A
    round-fix between rounds is clean, so dirt means something we must not lose."""
    primary, branch, _ = _repo_with_origin_and_pr_branch(tmp_path, monkeypatch)
    wt = W.allocate_on_branch("vivy", branch)
    (wt / "important.py").write_text("uncommitted work\n")   # real, non-debris
    with pytest.raises(W.WorktreePinError, match="uncommitted"):
        W.allocate_on_branch("vivy", branch)
    assert (wt / "important.py").exists()                    # not clobbered


def test_allocate_on_branch_refuses_committed_but_unpublished_work(tmp_path, monkeypatch):
    """The reuse path must not `reset --hard` away commits that never reached origin — a
    coder that committed then crashed before `publish` leaves a CLEAN working tree whose
    HEAD is ahead of origin. `git status` (unlanded_entries) is blind to that; only an
    ahead-count catches it. Losing those commits has no recovery path."""
    import subprocess
    primary, branch, _ = _repo_with_origin_and_pr_branch(tmp_path, monkeypatch)
    wt = W.allocate_on_branch("vivy", branch)

    def g(*a):
        return subprocess.run(
            ["git", "-C", str(wt), "-c", "user.email=t@t", "-c", "user.name=t", *a],
            check=True, capture_output=True, text=True)

    (wt / "f.py").write_text("v = 99\n")
    g("commit", "-qam", "committed but never published")   # clean tree, HEAD ahead of origin
    saved = g("rev-parse", "HEAD").stdout.strip()
    with pytest.raises(W.WorktreePinError, match="never published"):
        W.allocate_on_branch("vivy", branch)
    assert g("rev-parse", "HEAD").stdout.strip() == saved   # the commit survives


def test_allocate_on_branch_imports_a_dead_dispatchers_commit_before_refusing_to_discard_it(
        tmp_path, monkeypatch):
    """Round 7 (reviewer round 6, blocking, reproduced): a run's own git writes new
    objects to its private store (`run_sandbox.private_objects_dir`), and only the
    DISPATCHER's own end-of-run `import_private_objects` call rejoins them into the
    shared store. If the dispatcher process itself dies first — a documented
    recurring incident, not a hypothetical (P1b, 2026-07-04) — that import never
    runs, and the commit is reachable ONLY through the private store: no plain git
    command against the shared store can even resolve HEAD.

    Verified directly against 38352c30 (no import, no fail-closed check): this
    exact scenario does not reach the reset at all — `unlanded_entries`'s own
    `git status` call (which runs first, `check=True`) raises an uncaught
    `CalledProcessError` ("fatal: bad object HEAD"), crashing the dispatch instead
    of refusing cleanly or resetting. `allocate_on_branch`'s own `rev-list` ahead
    check has the identical "empty stdout from a failed call reads as 0" shape
    confirmed in isolation (`test_allocate_on_branch_refuses_when_the_ahead_check_itself_errors`
    below) and in the CREATE path (`test_allocate_on_branch_create_path_refuses_when_the_ahead_check_itself_errors`),
    where no earlier `git status` gate exists to crash first — so the silent-discard
    shape is real for this family of checks, even though this specific reuse path
    happens to crash before reaching it today. Importing the leftover store FIRST
    (what this function now does) removes the crash and the discard risk alike: the
    commit is resolvable before anything reads it, so the existing "never
    published" refusal does its job cleanly instead of either outcome."""
    import run_sandbox as RS
    primary, branch, pr_tip = _repo_with_origin_and_pr_branch(tmp_path, monkeypatch)
    wt = W.allocate_on_branch("vivy", branch)

    git_dir = RS.git_common_dir(wt)
    private_dir = RS.run_private_objects_dir(wt, git_dir)
    private_dir.mkdir()
    env = {**_os.environ, "GIT_OBJECT_DIRECTORY": str(private_dir),
          "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(git_dir / "objects")}
    (wt / "f.py").write_text("v = 99\n")
    _sp.run(["git", "-C", str(wt), "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-qam", "committed in the run's own private store — dispatcher "
             "died before importing it"],
            check=True, capture_output=True, env=env)
    saved = _sp.run(["git", "-C", str(wt), "rev-parse", "HEAD"],
                    check=True, capture_output=True, text=True).stdout.strip()
    # Deliberately NO RS.import_private_objects call here — the dispatcher never ran it.
    # The commit is reachable only through `private_dir` at this point.

    with pytest.raises(W.WorktreePinError, match="never published"):
        W.allocate_on_branch("vivy", branch)

    # The reset never ran — HEAD is exactly where the dead run left it.
    assert _sp.run(["git", "-C", str(wt), "rev-parse", "HEAD"],
                   check=True, capture_output=True, text=True).stdout.strip() == saved
    # And the commit the refusal is ABOUT is now resolvable by plain git in the
    # SHARED store — the import ran before the refusal, so the work this test is
    # protecting is not stranded in a private directory nothing else knows to look in.
    found = _sp.run(["git", "-C", str(wt), "cat-file", "-e", f"{saved}^{{commit}}"],
                    env={k: v for k, v in _os.environ.items()
                        if k not in ("GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES")})
    assert found.returncode == 0


def test_allocate_on_branch_refuses_when_the_ahead_check_itself_errors(tmp_path, monkeypatch):
    """A git error answering "is this tree ahead of origin" must never be read as "no,
    nothing ahead" — that exact equivalence is what let the dead-dispatcher class above
    discard a commit silently. Forcing the rev-list call to fail for an UNRELATED reason
    (not the private-store case, which is covered by the import above) must still refuse
    rather than fall through to `reset --hard`."""
    primary, branch, pr_tip = _repo_with_origin_and_pr_branch(tmp_path, monkeypatch)
    W.allocate_on_branch("vivy", branch)

    real_git = W._git

    def fake_git(path, *args, **kw):
        if args[:2] == ("rev-list", "--count") and args[2:] == (f"{pr_tip}..HEAD",):
            return _sp.CompletedProcess(args, 128, stdout="", stderr="fatal: boom\n")
        return real_git(path, *args, **kw)

    monkeypatch.setattr(W, "_git", fake_git)

    with pytest.raises(W.WorktreePinError, match="could not determine"):
        W.allocate_on_branch("vivy", branch)


def test_allocate_on_branch_create_path_refuses_when_the_ahead_check_itself_errors(
        tmp_path, monkeypatch):
    """The CREATE path (no worktree holds `branch`, but a local ref lingers — e.g. a
    worktree removed by hand while the branch ref survived) has no `unlanded_entries`
    gate ahead of it at all: its own `rev-list` ahead-check is the ONLY thing standing
    between a local ref carrying unpublished commits and `git worktree add -B`, which
    force-moves the ref onto the PR tip. A failed rev-list read as "0 ahead" here is
    the cleanest reproduction of the silent-discard shape the round-6 finding
    describes: nothing upstream of this check can crash first to mask it."""
    primary, branch, pr_tip = _repo_with_origin_and_pr_branch(tmp_path, monkeypatch)
    # A dangling local ref, ahead of origin, with no worktree checked out on it —
    # built with `commit-tree` so no worktree/checkout is ever created for it.
    extra = _sp.run(["git", "-C", str(primary), "-c", "user.email=t@t", "-c", "user.name=t",
                     "commit-tree", f"{pr_tip}^{{tree}}",
                     "-p", pr_tip, "-m", "extra, never published"],
                    check=True, capture_output=True, text=True).stdout.strip()
    _sp.run(["git", "-C", str(primary), "update-ref", f"refs/heads/{branch}", extra],
            check=True, capture_output=True)
    assert W.find_worktree_on_branch("vivy", branch) is None

    real_git = W._git

    def fake_git(path, *args, **kw):
        if args[:2] == ("rev-list", "--count") and args[2:] == \
                (f"{pr_tip}..refs/heads/{branch}",):
            return _sp.CompletedProcess(args, 128, stdout="", stderr="fatal: boom\n")
        return real_git(path, *args, **kw)

    monkeypatch.setattr(W, "_git", fake_git)

    with pytest.raises(W.WorktreePinError, match="could not determine"):
        W.allocate_on_branch("vivy", branch)

    # The ref was never force-moved, and no worktree got created on the stale tip.
    assert _sp.run(["git", "-C", str(primary), "rev-parse", f"refs/heads/{branch}"],
                   check=True, capture_output=True, text=True).stdout.strip() == extra
    assert W.find_worktree_on_branch("vivy", branch) is None


def test_allocate_on_branch_missing_origin_branch_raises(tmp_path, monkeypatch):
    primary, branch, _ = _repo_with_origin_and_pr_branch(tmp_path, monkeypatch)
    with pytest.raises(W.WorktreePinError, match="not found after fetch"):
        W.allocate_on_branch("vivy", "agent/no-such-branch")


def test_find_worktree_on_branch_absent_is_none(tmp_path, monkeypatch):
    _repo_with_origin_and_pr_branch(tmp_path, monkeypatch)
    assert W.find_worktree_on_branch("vivy", "agent/nope") is None


# --- dispatchability (ARCH-392) -----------------------------------------------
# `review-post` refuses a run-id-less APPROVE on a dispatchable repo, so a wrong
# answer here is a security-relevant control failing open (a discarded stamp the
# PR thread renders as an approval).

def test_is_dispatch_target_accepts_bare_name_and_full_slug(fake_convivy):
    assert W.is_dispatch_target("vivy") is True
    assert W.is_dispatch_target("convivy/vivy") is True


def test_is_dispatch_target_rejects_unknown_repo(fake_convivy):
    assert W.is_dispatch_target("nope") is False


def test_is_dispatch_target_rejects_traversal(fake_convivy):
    """A crafted --repo must not walk the answer out to an unrelated directory.

    `..` and an absolute path both basename down to something that could exist;
    the name check rejects them before any join happens.
    """
    (fake_convivy / "etc" / ".git").mkdir(parents=True)
    for hostile in ("", "..", "../..", "../../etc", "/etc", "convivy/../etc", "."):
        assert W.is_dispatch_target(hostile) is False, hostile


def test_is_dispatch_target_ignores_discovery_excludes(fake_convivy):
    """agent-platform is excluded from DISCOVERY, but `--on agent-platform` works.

    `_primary_for` never consults DISCOVERY_EXCLUDES, so answering this question
    from that set would claim un-dispatchable a repo dispatch accepts — and would
    hand agent-platform reviews the unverified path the ARCH-392 gate exists to
    close.
    """
    (fake_convivy / "agent-platform" / ".git").mkdir(parents=True)
    assert "agent-platform" in W.DISCOVERY_EXCLUDES
    assert "agent-platform" not in [p.name for p in W.discover_primaries()]
    assert W.is_dispatch_target("agent-platform") is True


def test_is_dispatch_target_requires_a_git_directory(fake_convivy):
    """A `.git` FILE marks a linked worktree, not a primary; `_primary_for` wants a dir."""
    plain = fake_convivy / "not-a-repo"
    plain.mkdir()
    assert W.is_dispatch_target("not-a-repo") is False
    (plain / ".git").write_text("gitdir: /elsewhere\n")
    assert W.is_dispatch_target("not-a-repo") is False


# --- the individuated .claude/ that defeated the ARCH-344 reap -----------------


def _tiny_repo(tmp_path):
    """A one-commit git repo, for exercising unlanded_entries against real git."""
    import subprocess
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
    (r / "f.py").write_text("v = 1\n")
    subprocess.run(["git", "-C", str(r), "add", "."], check=True)
    subprocess.run(["git", "-C", str(r), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "c1"], check=True)
    return r


def _individuate_claude(repo):
    """What `knowledge-ctx use` leaves in a dispatched worktree, manifest included."""
    c = repo / ".claude"
    (c / "agents").mkdir(parents=True)
    (c / "agents" / "reviewer.md").write_text("# reviewer\n")
    (c / "skills" / "some-skill").mkdir(parents=True)
    (c / "skills" / "some-skill" / "SKILL.md").write_text("# skill\n")
    (c / "CLAUDE.md").write_text("extras\n")
    (c / ".active-profile").write_text("vivy/reviewer\n")
    owned = ["agents/reviewer.md", "skills/some-skill/SKILL.md",
             "CLAUDE.md", ".active-profile", W._MANIFEST_NAME]
    (c / W._MANIFEST_NAME).write_text("\n".join(sorted(owned)) + "\n")
    return c


def test_the_dispatchers_own_claude_does_not_block_a_reap(tmp_path):
    """The bug that kept every review tree since ARCH-344 shipped.

    `agentctl spawn` individuates the profile into the worktree BEFORE the agent
    runs, so `.claude/` is the dispatcher's scaffolding. Porcelain reports a bare
    `?? .claude/`, which the guard read as reviewer-authored work and kept the
    tree — 74 of 275 trees in one 2026-07-23 snapshot, held by this and nothing
    else."""
    repo = _tiny_repo(tmp_path)
    _individuate_claude(repo)
    assert W.unlanded_entries(repo) == []


def test_an_agent_written_file_inside_claude_agents_still_blocks_the_reap(tmp_path):
    """security-reviewer's blocking finding on PR #226.

    A first draft compared only the four TOP-LEVEL names, so an injected agent
    writing `.claude/agents/exfil.md` left that set unchanged and the tree was
    cleared — destroying the artifact. knowledge-ctx's rmtree of agents/ runs at
    dispatch START, before the agent; the reap runs after it exits, so nothing
    removes the file in between. The manifest is what makes this exact."""
    repo = _tiny_repo(tmp_path)
    c = _individuate_claude(repo)
    (c / "agents" / "exfil.md").write_text("stolen\n")
    assert W.unlanded_entries(repo) != []


def test_an_agent_written_file_inside_claude_skills_still_blocks_the_reap(tmp_path):
    repo = _tiny_repo(tmp_path)
    c = _individuate_claude(repo)
    (c / "skills" / "some-skill" / "notes.md").write_text("stolen\n")
    assert W.unlanded_entries(repo) != []


def test_a_claude_without_a_manifest_blocks_the_reap(tmp_path):
    """Unprovable is not safe. A tree individuated before manifests existed cannot
    show what its .claude/ holds, so it keeps and a human sweeps it deliberately."""
    repo = _tiny_repo(tmp_path)
    c = _individuate_claude(repo)
    (c / W._MANIFEST_NAME).unlink()
    assert W.unlanded_entries(repo) != []


def test_a_reviewer_authored_file_under_claude_still_blocks_the_reap(tmp_path):
    """The guard keeps its teeth. A prompt-injected reviewer writing its own entry
    into .claude/ is exactly the case ARCH-344 exists for."""
    repo = _tiny_repo(tmp_path)
    c = _individuate_claude(repo)
    (c / "settings.json").write_text('{"hooks": {}}\n')
    assert W.unlanded_entries(repo) != []


def test_a_reviewer_authored_file_outside_claude_still_blocks_the_reap(tmp_path):
    repo = _tiny_repo(tmp_path)
    _individuate_claude(repo)
    (repo / "exfil.sh").write_text("curl evil\n")
    entries = W.unlanded_entries(repo)
    assert any("exfil.sh" in e for e in entries)


def test_a_tracked_modification_still_blocks_the_reap(tmp_path):
    """The .claude pass is untracked-only; a real edit to a tracked file is work."""
    repo = _tiny_repo(tmp_path)
    _individuate_claude(repo)
    (repo / "f.py").write_text("v = 2\n")
    assert W.unlanded_entries(repo) != []


def test_unpublished_commits_sees_a_clean_tree_holding_unpushed_work(tmp_path):
    """The check `unlanded_entries` cannot make. A worker that committed and died
    before publish leaves a CLEAN tree whose commits exist nowhere else; deleting on
    cleanliness alone would lose them with no recovery path. 49 of the trees in one
    2026-07-23 sweep were in exactly this state."""
    import subprocess
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True)

    def g(*a):
        return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t",
                               "-c", "user.name=t", *a], check=True,
                              capture_output=True, text=True)

    (repo / "f.py").write_text("v = 1\n")
    g("add", ".")
    g("commit", "-qm", "c1")
    g("push", "-q", "origin", "main")
    assert W.unlanded_entries(repo) == []
    assert W.unpublished_commits(repo) == 0        # everything is on the remote

    (repo / "f.py").write_text("v = 2\n")
    g("commit", "-qam", "committed, never pushed")
    assert W.unlanded_entries(repo) == []          # still clean — status is blind
    assert W.unpublished_commits(repo) == 1        # but the commit is only here


def test_unpublished_commits_raises_on_an_unreadable_tree(tmp_path):
    """A tree git cannot read is never a tree we may delete. Best-effort (0 on error)
    would read as 'nothing to lose' — the wrong default for a delete gate."""
    import subprocess
    notrepo = tmp_path / "notrepo"
    notrepo.mkdir()
    with pytest.raises(subprocess.CalledProcessError):
        W.unpublished_commits(notrepo)


def test_unpublished_commits_sees_a_commit_rejoined_from_a_runs_private_object_store(tmp_path):
    """The worktree pin check (knowledge#793 round 6): `allocate_on_branch`'s refusal to
    reset away unpublished work, and `unpublished_commits` itself, both walk history with
    `git rev-list` — which needs the commit's actual objects present, not just its ref.
    A dispatched run's own commit lands in its private object store, never the shared one
    (`run_sandbox.private_objects_dir`); `import_private_objects` is what makes it visible
    to a plain `rev-list` run OUTSIDE the sandbox, the same as every caller in this module."""
    import run_sandbox as RS
    origin = tmp_path / "origin.git"
    _sp.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    repo = tmp_path / "repo"
    _sp.run(["git", "clone", "-q", str(origin), str(repo)], check=True)

    def g(*a, env=None):
        return _sp.run(["git", "-C", str(repo), "-c", "user.email=t@t",
                        "-c", "user.name=t", *a], check=True,
                       capture_output=True, text=True, env=env)

    (repo / "f.py").write_text("v = 1\n")
    g("add", ".")
    g("commit", "-qm", "c1")
    g("push", "-q", "origin", "main")
    assert W.unpublished_commits(repo) == 0

    git_dir = RS.git_common_dir(repo)
    private_dir = RS.run_private_objects_dir(repo, git_dir)
    private_dir.mkdir()
    env = {**_os.environ, "GIT_OBJECT_DIRECTORY": str(private_dir),
          "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(git_dir / "objects")}
    (repo / "f.py").write_text("v = 2\n")
    g("commit", "-qam", "committed in the run's own private store", env=env)

    # Still true the moment the commit lands — the object isn't in the shared store yet,
    # so an unsandboxed `rev-list` could not even walk to it to notice it's unpublished.
    RS.import_private_objects(git_dir, private_dir)

    assert W.unpublished_commits(repo) == 1


def test_manifest_name_matches_knowledge_ctx(knowledge_ctx):
    """The two constants sit either side of the deployed-lib seam (bin/ tools import
    from ~/.local/lib), so they are literals bound by this test rather than a shared
    import — the same shape as agentctl/review_protocol's prefix lists, and for the
    same reason: a cross-module reference passes CI and breaks every branch until
    the lib half merges. A drift here silently stops every reap."""
    assert W._MANIFEST_NAME == knowledge_ctx._MANIFEST_NAME


# ---------------------------------------------------------------------------
# The dispatcher's own suppression of repo-root instruction files.
# ---------------------------------------------------------------------------

def _repo_with_tracked_instructions(tmp_path):
    import subprocess as sp
    wt = tmp_path / "repo"
    wt.mkdir()
    for c in (["init", "-q", "."], ["config", "user.email", "t@t"],
              ["config", "user.name", "t"]):
        sp.run(["git", "-C", str(wt), *c], check=True, capture_output=True)
    (wt / "CLAUDE.md").write_text("instructions\n")
    (wt / "AGENTS.md").write_text("instructions\n")
    (wt / "main.py").write_text("x = 1\n")
    sp.run(["git", "-C", str(wt), "add", "-A"], check=True, capture_output=True)
    sp.run(["git", "-C", str(wt), "commit", "-qm", "init"], check=True, capture_output=True)
    return wt


def test_the_dispatchers_own_suppression_is_not_unlanded_work(tmp_path):
    """`allocate_on_branch` refuses reuse when `unlanded_entries` is non-empty,
    and it asks BEFORE its reset. The dispatcher deletes CLAUDE.md/AGENTS.md
    before every run, so without this exemption the ARCH-347 round-fix reuse is
    refused on the very next round after any coder dispatch — on vivy, hollis,
    sous-chef and convivy-orient, which is the first revision of any PR there.

    Same reasoning as the `.claude/` exemption above it: a change the PLATFORM
    made before the agent started is not the agent's work.
    """
    wt = _repo_with_tracked_instructions(tmp_path)
    (wt / "CLAUDE.md").unlink()
    (wt / "AGENTS.md").unlink()
    assert W.unlanded_entries(wt) == []


def test_a_MODIFIED_instruction_file_is_still_unlanded_work(tmp_path):
    """The exemption is a DELETION only. An agent that writes new content to
    CLAUDE.md — the prompt-injection case ARCH-344 exists for — must still show
    up, or the reap destroys the evidence."""
    wt = _repo_with_tracked_instructions(tmp_path)
    (wt / "CLAUDE.md").unlink()
    (wt / "AGENTS.md").unlink()
    (wt / "CLAUDE.md").write_text("injected\n")
    entries = W.unlanded_entries(wt)
    assert any("CLAUDE.md" in e for e in entries), entries


def test_the_exemption_does_not_cover_other_deletions(tmp_path):
    """Only those two exact root paths. A deleted source file is real work lost."""
    wt = _repo_with_tracked_instructions(tmp_path)
    (wt / "main.py").unlink()
    entries = W.unlanded_entries(wt)
    assert any("main.py" in e for e in entries), entries


def test_the_PRIMARY_checkout_is_recognised(tmp_path, monkeypatch):
    """The guard that stops the dispatcher deleting instruction files off the
    human's own clone.

    `derive_purpose` returns None for planner/curator BY DESIGN,
    and `allocate_worktree(repo, purpose=None)` resolves to the primary — so
    anything destructive in the dispatch path must ask this first. Missing it
    meant a routine `agentctl spawn planner` deleted CLAUDE.md off the real repo
    (reviewer, PR #486).
    """
    primary = tmp_path / "Convivy" / "demo"
    primary.mkdir(parents=True)
    monkeypatch.setattr(W, "_primary_for", lambda repo: primary)
    assert W.is_primary_checkout("demo", primary) is True
    assert W.is_primary_checkout("demo", tmp_path / "worktrees" / "demo.coder-x") is False


def test_an_unresolvable_path_is_treated_as_PRIMARY(tmp_path, monkeypatch):
    """Cannot tell must mean the safe answer, and here the safe answer is
    'primary' — suppression is skipped rather than performed on an unknown tree."""
    # ValueError, not OSError, and that distinction is the whole test:
    # `_primary_for` raises ValueError for an unknown repo, and a narrow
    # `except OSError` let it escape and crash the dispatch. A version of this
    # test that raised OSError passed against the broken catch.
    def _boom(repo):
        raise ValueError(f"unknown repo: {repo!r}")
    monkeypatch.setattr(W, "_primary_for", _boom)
    assert W.is_primary_checkout("demo", tmp_path) is True

    # And the real function, unstubbed, on a name that genuinely is not a repo.
    assert W.is_primary_checkout("definitely-not-a-repo-xyz", tmp_path) is True


def test_the_suppression_is_also_forgiven_by_dirty_counts(tmp_path):
    """`unlanded_entries` and `_dirty_counts` must not disagree about the same
    tree. They did: `cleanup_candidates` would have marked every affected
    worktree `skip:dirty` forever, since suppression re-runs on every dispatch
    and nothing un-dirties it. That is the exact drift the `.claude/` version of
    this bug caused once already, 74 of 275 trees stuck (security review)."""
    wt = _repo_with_tracked_instructions(tmp_path)
    (wt / "CLAUDE.md").unlink()
    (wt / "AGENTS.md").unlink()
    assert W.unlanded_entries(wt) == []
    # `total` legitimately counts every porcelain line; `real` is the one
    # `status_summary` and the cleanup decision key on, and it must be 0.
    total, real = W._dirty_counts(wt)
    assert real == 0, (
        f"the two consumers disagree about the platform's own deletion "
        f"(total={total}, real={real})")


def test_a_NESTED_same_named_deletion_is_not_exempted(tmp_path):
    """The path check is against the bare ROOT name, not merely a different
    filename. An agent deleting docs/CLAUDE.md is destroying project content and
    must stay visible."""
    import subprocess as sp
    wt = _repo_with_tracked_instructions(tmp_path)
    (wt / "docs").mkdir()
    (wt / "docs" / "CLAUDE.md").write_text("documentation example\n")
    sp.run(["git", "-C", str(wt), "add", "-A"], check=True, capture_output=True)
    sp.run(["git", "-C", str(wt), "commit", "-qm", "docs"], check=True, capture_output=True)
    (wt / "docs" / "CLAUDE.md").unlink()
    entries = W.unlanded_entries(wt)
    assert any("docs/CLAUDE.md" in e for e in entries), entries


def test_the_two_suppression_lists_agree():
    """agentctl now REFERENCES worktrees' tuple rather than restating it, so this
    asserts identity — if someone reintroduces a second literal, `is` fails."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "bin" / "agentctl").read_text()
    assert "REPO_INSTRUCTION_FILES = W.SUPPRESSED_INSTRUCTION_FILES" in src, (
        "agentctl must reference the single definition, not declare its own")

# ---------------------------------------------------------------------------
# Render output is debris: why `worktrees-cleanup` left ~580 of ~600 worktrees.
#
# `knowledge-ctx use --force` renders agents, skills and CLAUDE.md into every
# dispatched worktree's `.claude/`. In a repo that TRACKS any of that, git reports
# tracked modifications, tracked deletions and per-file untracked paths, not the
# bare `?? .claude/` the ARCH-344 check understood, so every finished dispatch read
# as uncommitted work forever (579 of 598 candidates on 2026-09-26).
# ---------------------------------------------------------------------------

_T0 = 1_700_000_000_000_000_000        # ns: when the fake render finished
_LATER = _T0 + 60_000_000_000          # ns: a minute after it


def _git_in(repo, *args):
    return _sp.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                    *args], check=True, capture_output=True, text=True)


def _stamp(path, ns):
    _os.utime(path, ns=(ns, ns), follow_symlinks=False)


def _demo_root(tmp_path, monkeypatch, *, tracked_claude=True, repo="demo"):
    """A tmp `<root>/<repo>` primary and no registry. Returns (root, primary).

    With `tracked_claude` the primary commits part of `.claude/` (an agent the
    profile still lists, one it no longer lists, a skill, a settings file), which is
    the shape substrate, convivy-deck and the other affected repos have.

    `main` is pushed to an `origin` remote (every real scanned repo has one), at the
    SAME bare path `_publish` reuses (`tmp_path / "origin.git"`) so a test that calls
    `_publish` afterwards adds no second remote. Round 8: `cleanup_candidates` now
    calls `unpublished_commits` for every candidate it would otherwise call
    removable, not only an `ahead` one, so a worktree created off `main` with no new
    commits of its own must have somewhere to BE published to — else the one commit
    everyone starts from reads as perpetually unpublished and the whole render-only
    test suite below would see `skip:ahead` instead of `removable`."""
    root = tmp_path / "Convivy"
    primary = root / repo
    primary.mkdir(parents=True)
    _sp.run(["git", "init", "-q", "-b", "main", str(primary)], check=True)
    (primary / "f.py").write_text("v = 1\n")
    if tracked_claude:
        c = primary / ".claude"
        (c / "agents").mkdir(parents=True)
        (c / "agents" / "reviewer.md").write_text("committed reviewer\n")
        (c / "agents" / "old.md").write_text("committed, no longer listed\n")
        (c / "skills" / "some-skill").mkdir(parents=True)
        (c / "skills" / "some-skill" / "SKILL.md").write_text("committed skill\n")
        (c / "settings.json").write_text("{}\n")
        (c / "commands").mkdir()
        (c / "commands" / "a.md").write_text("a\n")
        (c / "commands" / "b.md").write_text("b\n")
    _git_in(primary, "add", "-A")
    _git_in(primary, "commit", "-qm", "c1")
    bare = tmp_path / "origin.git"
    if not bare.exists():
        _sp.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        _git_in(primary, "remote", "add", "origin", str(bare))
    _git_in(primary, "push", "-q", "origin", "main")
    monkeypatch.setattr(W, "CONVIVY_ROOT", root)
    monkeypatch.setattr(W, "in_use_map", lambda: {})
    monkeypatch.setattr(W, "pr_merged", lambda wt: False)
    return root, primary


def _add_worktree(primary, root, name="x", repo=None):
    wt = root / "worktrees" / f"{repo or primary.name}.{name}"
    wt.parent.mkdir(exist_ok=True)
    _git_in(primary, "worktree", "add", "-q", "-b", f"agent/{name}", str(wt), "main")
    return wt


def _render(wt, *, at=_T0):
    """What `knowledge-ctx use --force` leaves in `wt/.claude`, all stamped `at`.

    Overwrites the tracked reviewer.md and SKILL.md, clears old.md, adds an untracked
    skill, and writes the three records, manifest last, as the real render does."""
    c = wt / ".claude"
    (c / "agents").mkdir(parents=True, exist_ok=True)
    (c / "agents" / "old.md").unlink(missing_ok=True)
    (c / "agents" / "reviewer.md").write_text("rendered reviewer\n")
    (c / "skills" / "some-skill").mkdir(parents=True, exist_ok=True)
    (c / "skills" / "some-skill" / "SKILL.md").write_text("rendered skill\n")
    (c / "skills" / "new-skill").mkdir(parents=True, exist_ok=True)
    (c / "skills" / "new-skill" / "SKILL.md").write_text("rendered new skill\n")
    (c / ".active-profile").write_text("demo/reviewer\n")
    (c / W._PROFILE_DOCUMENTS_NAME).write_text("{}\n")
    owned = ["agents/reviewer.md", "skills/some-skill/SKILL.md", "skills/new-skill/SKILL.md",
             ".active-profile", W._PROFILE_DOCUMENTS_NAME, W._MANIFEST_NAME]
    (c / W._MANIFEST_NAME).write_text("\n".join(sorted(owned)) + "\n")
    for p in [*c.rglob("*")]:
        _stamp(p, at)
    _stamp(c, at)


def _reason(wt):
    got = {c.worktree.path.resolve(): c.reason for c in W.cleanup_candidates()}
    return got[wt.resolve()]


def test_a_finished_worktree_whose_only_changes_are_render_output_is_removable(
        tmp_path, monkeypatch):
    root, primary = _demo_root(tmp_path, monkeypatch)
    wt = _add_worktree(primary, root)
    _render(wt)
    # Guard against a vacuous pass: the tree really is dirty as far as git can tell.
    porcelain = _git_in(wt, "status", "--porcelain").stdout
    assert " M .claude/agents/reviewer.md" in porcelain
    assert " D .claude/agents/old.md" in porcelain
    assert "?? .claude/skills/new-skill/" in porcelain
    assert _reason(wt) == "removable"
    # The two consumers agree: allocate_on_branch's reuse check and the reap gate.
    assert W.unlanded_entries(wt) == []
    assert W._dirty_counts(wt)[1] == 0


def test_a_render_only_worktree_stays_removable_through_recheck(tmp_path, monkeypatch):
    root, primary = _demo_root(tmp_path, monkeypatch)
    wt = _add_worktree(primary, root)
    _render(wt)
    (cand,) = [c for c in W.cleanup_candidates() if c.worktree.path.resolve() == wt.resolve()]
    skip, fresh_dirty = W.recheck_candidate(cand)
    assert skip is None and fresh_dirty > 0


@pytest.mark.parametrize("edited", [
    ".claude/agents/reviewer.md",                 # tracked, render overwrote it
    ".claude/skills/new-skill/SKILL.md",          # untracked, listed
    ".claude/.active-profile",                    # untracked record file
])
def test_a_render_written_file_edited_afterwards_keeps_the_worktree(
        tmp_path, monkeypatch, edited):
    root, primary = _demo_root(tmp_path, monkeypatch)
    wt = _add_worktree(primary, root)
    _render(wt)
    assert _reason(wt) == "removable"
    f = wt / edited
    f.write_text("agent rewrote this\n")
    _stamp(f, _LATER)
    assert _reason(wt) == "skip:dirty"
    assert any(edited in e for e in W.unlanded_entries(wt))


def test_an_edited_render_file_keeps_a_repo_that_tracks_nothing_in_claude(
        tmp_path, monkeypatch):
    """The bare `?? .claude/` shape. It reaches the same question through the older
    set-equality check, which had no way to see a listed file's content change."""
    root, primary = _demo_root(tmp_path, monkeypatch, tracked_claude=False)
    wt = _add_worktree(primary, root)
    _render(wt)
    assert "?? .claude/\n" in _git_in(wt, "status", "--porcelain").stdout
    assert _reason(wt) == "removable"
    f = wt / ".claude" / "agents" / "reviewer.md"
    f.write_text("agent rewrote this\n")
    _stamp(f, _LATER)
    assert _reason(wt) == "skip:dirty"


def test_a_render_that_found_files_current_and_left_the_manifest_alone_still_reaps(
        tmp_path, monkeypatch):
    """A reused worktree re-rendered after a KB change: an agent file is rewritten
    after the manifest, and only `.profile-documents.json` (fresh timestamp inside)
    is rewritten with it. Its mtime is what marks the end of that render."""
    root, primary = _demo_root(tmp_path, monkeypatch)
    wt = _add_worktree(primary, root)
    _render(wt)
    for rel in ("agents/reviewer.md", W._PROFILE_DOCUMENTS_NAME):
        f = wt / ".claude" / rel
        f.write_text("re-rendered\n")
        _stamp(f, _LATER)
    assert _reason(wt) == "removable"


@pytest.mark.parametrize("how", [
    "tracked-unlisted-modified",       # .claude/settings.json: not the render's
    "untracked-unlisted-in-skill",     # a file beside a rendered skill
    "untracked-unlisted-agent",        # the exfil.md case from ARCH-344
    "unlisted-deleted-outside-cleared",  # ` D .claude/commands/a.md`: not a dir the render clears
    "listed-file-deleted",             # the render wrote it; something removed it
    "staged-render-file",              # `git add`: an agent chose to keep it
    "cleared-dir-touched-after",       # agents/ gained or lost an entry post-render
    "listed-file-now-a-symlink",
])
def test_a_change_the_render_did_not_write_keeps_the_worktree(tmp_path, monkeypatch, how):
    root, primary = _demo_root(tmp_path, monkeypatch)
    wt = _add_worktree(primary, root)
    _render(wt)
    assert _reason(wt) == "removable"
    c = wt / ".claude"
    # Every change below is stamped BEFORE the render finished (_T0), so the mtime
    # rule cannot be what keeps the tree: only the manifest or the entry's shape
    # can. A fixture whose new files carry the wall clock would let the mtime rule
    # answer for all of them and leave the manifest check untested.
    if how == "tracked-unlisted-modified":
        (c / "settings.json").write_text('{"hooks": {}}\n')
        _stamp(c / "settings.json", _T0)
    elif how == "untracked-unlisted-in-skill":
        (c / "skills" / "new-skill" / "notes.md").write_text("stolen\n")
        _stamp(c / "skills" / "new-skill" / "notes.md", _T0)
    elif how == "untracked-unlisted-agent":
        (c / "agents" / "exfil.md").write_text("stolen\n")
        _stamp(c / "agents" / "exfil.md", _T0)
        _stamp(c / "agents", _T0)
    elif how == "unlisted-deleted-outside-cleared":
        (c / "commands" / "a.md").unlink()
        _stamp(c / "commands", _T0)
    elif how == "listed-file-deleted":
        (c / "skills" / "some-skill" / "SKILL.md").unlink()
        _stamp(c / "skills" / "some-skill", _T0)
    elif how == "staged-render-file":
        _git_in(wt, "add", ".claude/agents/reviewer.md")
    elif how == "cleared-dir-touched-after":
        _stamp(c / "agents", _LATER)
    elif how == "listed-file-now-a-symlink":
        f = c / "skills" / "new-skill" / "SKILL.md"
        f.unlink()
        f.symlink_to(wt / "f.py")
        _stamp(f, _T0)
    assert _reason(wt) == "skip:dirty", how


def test_a_worktree_without_a_manifest_keeps_its_render_output(tmp_path, monkeypatch):
    """Unprovable is not safe: no record of what the render wrote, so nothing in
    `.claude/` can be called the render's."""
    root, primary = _demo_root(tmp_path, monkeypatch)
    wt = _add_worktree(primary, root)
    _render(wt)
    (wt / ".claude" / W._MANIFEST_NAME).unlink()
    assert W._dirty_counts(wt)[1] > 0
    assert W.unlanded_entries(wt)
    # The reap never gets that far: no manifest also means the dispatcher was not here.
    assert _reason(wt) == "skip:not-dispatched"


def test_a_real_change_outside_claude_keeps_a_render_only_worktree(tmp_path, monkeypatch):
    root, primary = _demo_root(tmp_path, monkeypatch)
    wt = _add_worktree(primary, root)
    _render(wt)
    (wt / "f.py").write_text("v = 2\n")
    assert _reason(wt) == "skip:dirty"
    assert [e for e in W.unlanded_entries(wt) if ".claude" not in e] == [" M f.py"]


def test_a_live_runs_worktree_is_kept_even_when_only_render_output(tmp_path, monkeypatch):
    root, primary = _demo_root(tmp_path, monkeypatch)
    wt = _add_worktree(primary, root)
    _render(wt)
    monkeypatch.setattr(W, "in_use_map", lambda: {wt.resolve(): {"run_id": "run-x"}})
    assert _reason(wt) == "skip:in_use"


def test_a_locked_worktree_is_kept_even_when_only_render_output(tmp_path, monkeypatch):
    root, primary = _demo_root(tmp_path, monkeypatch)
    wt = _add_worktree(primary, root)
    _render(wt)
    _git_in(primary, "worktree", "lock", str(wt))
    assert _reason(wt) == "skip:locked"


def test_render_only_worktree_with_unpublished_commits_is_still_kept(tmp_path, monkeypatch):
    root, primary = _demo_root(tmp_path, monkeypatch)
    wt = _add_worktree(primary, root)
    (wt / "f.py").write_text("v = 3\n")
    _git_in(wt, "commit", "-qam", "agent work")
    _render(wt)
    assert _reason(wt) == "skip:ahead"


def test_profile_documents_name_matches_knowledge_ctx(knowledge_ctx):
    assert W._PROFILE_DOCUMENTS_NAME == knowledge_ctx._PROFILE_DOCUMENTS_NAME


# --- names git quotes ---------------------------------------------------------


@pytest.mark.parametrize("name", [
    "caf\u00e9",             # non-ASCII: porcelain prints caf\303\251
    "a\tb",                  # tab
    "a\\b",                 # backslash
    'a"b',                   # double quote
    "a[b]",                  # glob characters in the name
    "a*b",
])
def test_an_untracked_skill_directory_git_quotes_keeps_the_worktree(
        tmp_path, monkeypatch, name):
    """The collapsed line for an untracked directory is quoted when its name has an
    unusual byte. Trimming the quotes left escapes that named no file, the expansion
    found nothing, and the tree read clean with real files in it."""
    root, primary = _demo_root(tmp_path, monkeypatch)
    wt = _add_worktree(primary, root)
    _render(wt)
    assert _reason(wt) == "removable"
    d = wt / ".claude" / "skills" / name
    d.mkdir()
    (d / "notes.md").write_text("an agent wrote this\n")
    _stamp(d / "notes.md", _T0)
    _stamp(d, _T0)
    # Guard against a vacuous pass: git collapses the directory to one line, and for
    # the non-ASCII name that line is the quoted, escaped form.
    collapsed = [ln for ln in _git_in(wt, "status", "--porcelain").stdout.splitlines()
                 if ln.startswith("?? ") and ln.rstrip('"').endswith("/")
                 and "new-skill" not in ln]
    assert len(collapsed) == 1, collapsed
    if name == "caf\u00e9":
        assert collapsed[0] == '?? ".claude/skills/caf\\303\\251/"'
    assert _reason(wt) == "skip:dirty"
    assert W.unlanded_entries(wt) == [f"?? .claude/skills/{name}/notes.md"]
    assert W._dirty_counts(wt)[1] == 1


def test_an_untracked_directory_line_whose_expansion_finds_nothing_keeps_the_tree(
        tmp_path, monkeypatch):
    """An untracked-directory line proves a file exists, so a lookup that returns none
    failed and must not read as clean."""
    root, primary = _demo_root(tmp_path, monkeypatch)
    wt = _add_worktree(primary, root)
    _render(wt)
    assert _reason(wt) == "removable"
    real = W._status_entries
    monkeypatch.setattr(W, "_status_entries", lambda wt, *a, **kw: (
        [] if "--untracked-files=all" in a else real(wt, *a, **kw)))
    assert W.unlanded_entries(wt) == ["?? .claude/skills/new-skill/"]
    assert _reason(wt) == "skip:dirty"


def test_a_failed_expansion_keeps_the_tree(tmp_path, monkeypatch):
    root, primary = _demo_root(tmp_path, monkeypatch)
    wt = _add_worktree(primary, root)
    _render(wt)
    real = W._status_entries
    monkeypatch.setattr(W, "_status_entries", lambda wt, *a, **kw: (
        None if "--untracked-files=all" in a else real(wt, *a, **kw)))
    assert W.unlanded_entries(wt) == ["?? .claude/skills/new-skill/"]


def test_a_status_entry_for_a_rename_names_the_new_path_once(tmp_path, monkeypatch):
    root, primary = _demo_root(tmp_path, monkeypatch)
    wt = _add_worktree(primary, root)
    _git_in(wt, "mv", "f.py", "g h.py")
    (wt / "new.txt").write_text("x\n")
    assert W._status_entries(wt) == ["R  g h.py", "?? new.txt"]
    assert W.unlanded_entries(wt) == ["R  g h.py", "?? new.txt"]


def test_recheck_flips_a_listed_candidate_to_dirty_when_a_render_file_is_edited_after(
        tmp_path, monkeypatch):
    """Nothing stubbed: the recheck reads the tree again and applies the render rule."""
    root, primary = _demo_root(tmp_path, monkeypatch)
    wt = _add_worktree(primary, root)
    _render(wt)
    (cand,) = [c for c in W.cleanup_candidates() if c.worktree.path.resolve() == wt.resolve()]
    assert cand.reason == "removable"
    f = wt / ".claude" / "agents" / "reviewer.md"
    f.write_text("agent rewrote this\n")
    _stamp(f, _LATER)
    assert W.recheck_candidate(cand) == ("skip:dirty", 0)


# --- the nightly reap job's own delete gate vs. a dead dispatcher's leftover
# private object store (round 8, reviewer round 7, blocking, new) --------------
#
# `cleanup_candidates`/`recheck_candidate` are a fourth "decide whether work would
# be lost" check, alongside the three `allocate_on_branch`/`_pin_existing_to_commit`/
# `cmd_publish`/`cmd_pr_open` sites round 7 already fixed — and the nightly
# `launchd/com.convivy.worktrees-cleanup.plist` job runs this one unattended, with
# `--commit`, with nobody watching it run. A dispatcher that dies after a run
# commits but before importing leaves a worktree whose HEAD only the run's own
# private object store can resolve; plain `git status`/`rev-list` against the
# shared store fail outright, and the best-effort readers these two functions
# used to trust (`_ahead_behind`, `_dirty_counts`) folded that failure into
# "(0, 0), nothing to lose" — exactly backwards for a delete gate.
#
# Checked directly (below) rather than assumed: for THIS exact shape — nothing
# else dirty, so `recheck_candidate`'s own `dirty_files` is ALSO masked to 0 —
# the unfixed code computes `force=False` for the remove, and plain
# `git worktree remove` (unforced) itself refuses with `fatal: bad object HEAD`
# rather than silently deleting; `--force` is what skips that refusal and does
# delete silently (confirmed empirically, not asserted below — not the path this
# exact repro reaches). So the unfixed behavior proven below is an opaque crash
# (exit 128, logged to a file nobody reads daily) and a worktree stuck forever
# with its commit permanently stranded in a private store nothing will ever
# import — not a literal `--force` deletion in this minimal repro. Either
# outcome is the same root cause this round fixes: a classification reading a
# git error as "nothing to lose" rather than "cannot tell."


def _commit_into_private_store(wt, *, msg):
    """Commit a change to a TRACKED file in `wt`, redirected to the run's own
    private object store (`run_sandbox.private_objects_dir`), with NO
    `import_private_objects` call — the shape a dispatcher leaves behind when it
    dies before its own end-of-run import runs (P1b, 2026-07-04). Returns the new
    commit's sha, resolvable only through the private store at this point."""
    import run_sandbox as RS
    git_dir = RS.git_common_dir(wt)
    private_dir = RS.run_private_objects_dir(wt, git_dir)
    private_dir.mkdir(exist_ok=True)
    env = {**_os.environ, "GIT_OBJECT_DIRECTORY": str(private_dir),
          "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(git_dir / "objects")}
    (wt / "f.py").write_text("v = 99\n")
    _sp.run(["git", "-C", str(wt), "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-qam", msg], check=True, capture_output=True, env=env)
    return _sp.run(["git", "-C", str(wt), "rev-parse", "HEAD"],
                   check=True, capture_output=True, text=True).stdout.strip()


def _resolves_in_shared_store(wt, sha):
    found = _sp.run(
        ["git", "-C", str(wt), "cat-file", "-e", f"{sha}^{{commit}}"],
        env={k: v for k, v in _os.environ.items()
            if k not in ("GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES")})
    return found.returncode == 0


def test_cleanup_candidates_does_not_classify_a_dead_dispatchers_unimported_commit_as_removable(
        tmp_path, monkeypatch):
    """The exact scenario the round-7 reviewer reproduced through the reap path.
    Before this round's fix: plain `git status` against the shared store fails
    outright on this HEAD (confirmed below, a positive control against the
    unfixed shape), `_dirty_counts`/`_ahead_behind` read that failure as
    `(0, 0)`, and `cleanup_candidates` called the tree `removable` with the
    commit reachable from nowhere `git worktree remove` would not also delete."""
    root, primary = _demo_root(tmp_path, monkeypatch)
    wt = _add_worktree(primary, root)
    _render(wt)
    saved = _commit_into_private_store(
        wt, msg="committed in the run's own private store — dispatcher died "
                "before importing it")

    # Positive control: confirm the masking condition is real before trusting the
    # fix for it. Plain git against the shared store cannot resolve this HEAD.
    unmasked = _sp.run(["git", "-C", str(wt), "status", "--porcelain"], capture_output=True)
    assert unmasked.returncode != 0

    (cand,) = [c for c in W.cleanup_candidates() if c.worktree.path.resolve() == wt.resolve()]
    assert not cand.reason.startswith("removable"), cand.reason

    # The classification's own import rejoined the commit into the shared store —
    # it is no longer stranded, whatever the final verdict on the worktree is.
    assert _resolves_in_shared_store(wt, saved)


def test_recheck_candidate_does_not_confirm_a_dead_dispatchers_unimported_commit(
        tmp_path, monkeypatch):
    """`recheck_candidate`'s own guard, independent of `cleanup_candidates` —
    exercised directly, as round 7's own `allocate_on_branch` tests exercise each
    call site rather than relying on one end-to-end pass to cover every site."""
    root, primary = _demo_root(tmp_path, monkeypatch)
    wt = _add_worktree(primary, root)
    _render(wt)
    base_wt = W.list_worktrees(primary, only=wt)[0]
    saved = _commit_into_private_store(
        wt, msg="committed in the run's own private store — dispatcher died "
                "before importing it")

    cand = W.CleanupCandidate(base_wt, "removable")
    skip_reason, _ = W.recheck_candidate(cand)
    assert skip_reason is not None

    assert _resolves_in_shared_store(wt, saved)


def test_worktrees_cleanup_commit_does_not_discard_the_worktree_or_the_commit(
        tmp_path, monkeypatch):
    """End-to-end through the actual CLI entry point the launchd job runs. On the
    unfixed code this exits 1 (`cleanup_candidates` calls it "removable", the
    unforced `git worktree remove` it then tries crashes with "fatal: bad object
    HEAD" rather than being skipped with a clear reason) rather than silently
    removing it — but the worktree is left stuck in that broken state forever,
    and its commit stranded. The fix must exit 0 and leave the worktree AND its
    HEAD commit intact, correctly classified rather than crashed into."""
    from conftest import _load_cli
    root, primary = _demo_root(tmp_path, monkeypatch)
    wt = _add_worktree(primary, root)
    _render(wt)
    saved = _commit_into_private_store(
        wt, msg="committed in the run's own private store — dispatcher died "
                "before importing it")

    assert _load_cli("worktrees-cleanup").main(["--commit"]) == 0

    assert wt.exists()
    head = _sp.run(["git", "-C", str(wt), "rev-parse", "HEAD"], env={
        k: v for k, v in _os.environ.items()
        if k not in ("GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES")},
        capture_output=True, text=True).stdout.strip()
    assert head == saved
    assert _resolves_in_shared_store(wt, saved)


# --- the directories that were never candidates --------------------------------


def test_the_platform_repos_own_worktrees_are_scanned_by_the_cleanup(tmp_path, monkeypatch):
    """115 of the 700 directories were worktrees of agent-platform, which
    DISCOVERY_EXCLUDES keeps out of listing and dispatch discovery. The cleanup
    passes `include_excluded`, and only it does. A dispatched tree carries the
    render manifest; the same shape without one is a person's (next test)."""
    root, primary = _demo_root(tmp_path, monkeypatch, repo="agent-platform")
    wt = _add_worktree(primary, root)
    _render(wt)
    assert "agent-platform" not in [p.name for p in W.discover_primaries()]
    assert "agent-platform" in [p.name for p in W.discover_primaries(include_excluded=True)]
    assert wt.resolve() not in [w.path.resolve() for w in W.all_worktrees()]
    assert _reason(wt) == "removable"
    # ...and the primary checkout itself is never a candidate.
    assert primary.resolve() not in [
        c.worktree.path.resolve() for c in W.cleanup_candidates()]


def test_hand_made_worktrees_of_the_platform_repo_are_kept(tmp_path, monkeypatch):
    """`agent-platform-wt-*` and `.claude/worktrees/*` are a person's or a session's.
    Nothing in the registry says whether one is in use, so the job leaves them even
    when they are clean and fully published."""
    root, primary = _demo_root(tmp_path, monkeypatch, repo="agent-platform")
    beside = root / "agent-platform-wt-keel"
    inside = primary / ".claude" / "worktrees" / "keel-merge-flow"
    _git_in(primary, "worktree", "add", "-q", "-b", "keel/x", str(beside), "main")
    _git_in(primary, "worktree", "add", "-q", "-b", "keel/y", str(inside), "main")
    got = {c.worktree.path.resolve(): c.reason for c in W.cleanup_candidates()}
    assert got[beside.resolve()] == "skip:not-dispatched"
    assert got[inside.resolve()] == "skip:not-dispatched"


def _publish(wt, primary, tmp_path):
    """Give `wt`'s branch a commit that exists on a remote, so it reads `removable:published`."""
    bare = tmp_path / "origin.git"
    if not bare.exists():
        _sp.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        _git_in(primary, "remote", "add", "origin", str(bare))
    (wt / "f.py").write_text("v = 5\n")
    _git_in(wt, "commit", "-qam", "work")
    _git_in(wt, "push", "-q", "origin", "HEAD")
    _git_in(wt, "fetch", "-q", "origin")


@pytest.mark.parametrize("repo", ["agent-platform", "demo"])
@pytest.mark.parametrize("published", [False, True])
def test_a_hand_made_tree_where_the_dispatcher_puts_its_own_is_kept(
        tmp_path, monkeypatch, repo, published):
    """`worktrees/<repo>.<purpose>` is the path the co's instructions tell a session to
    make by hand, and the one the dispatcher uses. Where a tree sits does not say who
    made it. A session that pushed and is waiting on review is clean and published, so
    only a record the dispatcher leaves can tell it from a finished dispatch."""
    root, primary = _demo_root(tmp_path, monkeypatch, repo=repo)
    wt = _add_worktree(primary, root, "tmux-target")
    if published:
        _publish(wt, primary, tmp_path)
        assert W.unpublished_commits(wt) == 0
    assert _reason(wt) == "skip:not-dispatched"
    # The same tree, once the dispatcher has rendered into it, is reaped as before.
    _render(wt)
    assert _reason(wt) == ("removable:published" if published else "removable")


def test_a_manifest_that_is_not_a_regular_file_does_not_make_a_tree_dispatched(
        tmp_path, monkeypatch):
    root, primary = _demo_root(tmp_path, monkeypatch)
    wt = _add_worktree(primary, root)
    (wt / ".claude").mkdir(exist_ok=True)
    (wt / ".claude" / W._MANIFEST_NAME).symlink_to(wt / "f.py")
    assert _reason(wt) == "skip:not-dispatched"


def test_a_live_run_in_an_unrendered_tree_reads_in_use(tmp_path, monkeypatch):
    root, primary = _demo_root(tmp_path, monkeypatch)
    wt = _add_worktree(primary, root)
    monkeypatch.setattr(W, "in_use_map", lambda: {wt.resolve(): {"run_id": "run-x"}})
    assert _reason(wt) == "skip:in_use"


def test_the_platform_repos_dirty_worktree_is_kept(tmp_path, monkeypatch):
    """The uncommitted bin/lib changes waiting on the operator in two coder worktrees."""
    root, primary = _demo_root(tmp_path, monkeypatch, repo="agent-platform")
    wt = _add_worktree(primary, root)
    _render(wt)
    (wt / "f.py").write_text("v = 9\n")
    (wt / "new_test.py").write_text("x\n")
    assert _reason(wt) == "skip:dirty"


def test_directories_no_scanned_repo_owns_are_reported_and_left(tmp_path, monkeypatch):
    root, primary = _demo_root(tmp_path, monkeypatch)
    registered = _add_worktree(primary, root)
    extras = root / "worktrees"

    (extras / ".parked-claude-1").mkdir()                      # no .git
    (extras / ".parked-claude-1" / ".claude").mkdir()

    orphan = extras / "demo.orphan"                            # gitdir gone
    orphan.mkdir()
    (orphan / ".git").write_text(f"gitdir: {tmp_path}/no/such/gitdir\n")

    elsewhere = tmp_path / "elsewhere"                         # a repo the job does not scan
    elsewhere.mkdir()
    _sp.run(["git", "init", "-q", "-b", "main", str(elsewhere)], check=True)
    (elsewhere / "a").write_text("a\n")
    _git_in(elsewhere, "add", "-A")
    _git_in(elsewhere, "commit", "-qm", "c")
    other = extras / "elsewhere.x"
    _git_in(elsewhere, "worktree", "add", "-q", "-b", "x", str(other), "main")

    standalone = extras / "clone"                              # its own .git/
    standalone.mkdir()
    _sp.run(["git", "init", "-q", str(standalone)], check=True)

    (extras / ".DS_Store").write_text("")                      # a file, ignored

    got = {p.name: k for p, k in W.unregistered_dirs()}
    assert got == {
        ".parked-claude-1": "no-git",
        "demo.orphan": "metadata-gone",
        "elsewhere.x": "other-repo",
        "clone": "standalone-repo",
    }
    assert registered.name not in got
    # Reported, not candidates: the reap has nothing to remove for any of them.
    labels = {c.worktree.path.resolve() for c in W.cleanup_candidates()}
    assert not labels & {p.resolve() for p in (orphan, other, standalone)}


def test_reason_counts_are_most_frequent_first():
    cands = [W.CleanupCandidate(_wt(f"w{i}"), r) for i, r in enumerate(
        ["skip:dirty", "removable", "skip:dirty", "skip:ahead", "skip:dirty", "skip:ahead"])]
    assert W.reason_counts(cands) == [("skip:dirty", 3), ("skip:ahead", 2), ("removable", 1)]


def test_the_job_says_why_each_worktree_was_kept(tmp_path, monkeypatch, capsys):
    """A run that keeps most of the fleet must not read as a quiet exit 0."""
    from conftest import _load_cli
    root, primary = _demo_root(tmp_path, monkeypatch)
    done = _add_worktree(primary, root, "done")
    _render(done)
    mid = _add_worktree(primary, root, "mid")
    _render(mid)
    (mid / "f.py").write_text("v = 2\n")
    busy = _add_worktree(primary, root, "busy")
    (root / "worktrees" / ".parked-claude-1").mkdir()
    monkeypatch.setattr(W, "in_use_map", lambda: {busy.resolve(): {"run_id": "run-x"}})

    assert _load_cli("worktrees-cleanup").main([]) == 0
    out = capsys.readouterr().out
    tail = out[out.index("1 removable,"):]
    assert "1 removable, 2 kept, 1 left alone, by reason:" in tail
    assert "1  removable\n" in tail
    assert "1  skip:dirty\n" in tail
    assert "1  skip:in_use\n" in tail
    assert "1  not-a-worktree:no-git\n" in tail
    assert done.exists() and mid.exists()        # a dry run removes nothing


def test_recheck_reads_one_worktree_not_the_whole_repo(tmp_path, monkeypatch):
    """`list_worktrees(only=...)` skips the git status of every other worktree, so
    re-checking N removable candidates is N status calls rather than N x repo size."""
    root, primary = _demo_root(tmp_path, monkeypatch)
    a, b = _add_worktree(primary, root, "a"), _add_worktree(primary, root, "b")
    seen = []
    real = W._dirty_counts
    monkeypatch.setattr(W, "_dirty_counts", lambda p: (seen.append(p.resolve()), real(p))[1])
    got = W.list_worktrees(primary, only=a)
    assert [w.path.resolve() for w in got] == [a.resolve()]
    assert seen == [a.resolve()] and b.resolve() not in seen


def test_gh_path_prefers_safe_which(monkeypatch):
    """`safe_which` resolves `gh` from a fixed list of system directories
    (platform/decisions/0090); `_gh_path` returns exactly what it finds."""
    monkeypatch.setattr(W.SP, "safe_which", lambda name: "/opt/homebrew/bin/gh")
    assert W._gh_path() == "/opt/homebrew/bin/gh"


def test_gh_path_is_none_when_nothing_is_found(monkeypatch):
    monkeypatch.setattr(W.SP, "safe_which", lambda name: None)
    assert W._gh_path() is None


def test_gh_path_a_planted_gh_under_a_fake_home_is_not_chosen(monkeypatch, tmp_path):
    """The real end-to-end exercise of `safe_which`'s plant protection, with
    no mock: a planted `gh` under a fake HOME, ahead on PATH, must not become
    what `_gh_path` returns — `safe_which` never reads PATH or HOME, so the
    real gh on this machine must be the one resolved regardless
    (platform/decisions/0090's Handoff). No literal path: this machine's real
    answer is computed before HOME/PATH are poisoned, not hardcoded — the
    literal `/opt/homebrew/bin/gh` this test used before round 13 is wrong on
    the GitHub Actions macOS runner, which puts `gh` somewhere else."""
    baseline = W.SP.safe_which("gh")
    if baseline is None:
        pytest.skip("no real gh on this machine's fixed system list")

    fake_home = tmp_path / "fake-home"
    planted_dir = fake_home / ".local" / "bin"
    planted_dir.mkdir(parents=True)
    (planted_dir / "gh").write_text("#!/bin/sh\n")
    (planted_dir / "gh").chmod(0o755)

    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("PATH", str(planted_dir))
    got = W._gh_path()
    assert got is not None and not got.startswith(str(fake_home)), \
        f"the planted gh under the fake HOME was chosen: {got}"
    assert got == baseline
