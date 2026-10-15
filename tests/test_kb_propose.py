"""`agentctl kb propose` — the PR path for changes that may not write main.

The verb exists so that "you may not write this directly" has an answer other
than "so build a worktree by hand". Its correctness conditions are that it never
writes main, that it validates before opening a PR (a proposal that reddens CI
wastes a whole review cycle), and that it leaves the cage detached afterwards so
the next apply's reset is a no-op rather than a branch switch.
"""
from __future__ import annotations

import json
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

import kb_writes as kw

DOC = """---
id: platform/reference/{slug}
kind: reference
project: platform
title: {title}
status: current
audience: [architect]
last_reviewed: 2026-07-25
authors: [operator]
---

{body}
"""

SEEDED = "projects/platform/reference/seeded.md"
ROSTER = "projects/agent-platform/agents/reviewer.md"
PROFILE = "projects/shared/profiles/daytime.yaml"


def _git(repo: Path, *a):
    return subprocess.run(["git", "-C", str(repo), *a],
                          capture_output=True, text=True, check=False)


def _write_doc(root: Path, rel: str, *, slug: str, body: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DOC.format(slug=slug, title="A doc", body=body))
    return path


@pytest.fixture
def cage(tmp_path, monkeypatch, agentctl, library_identity):
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "--quiet", "--bare", "--initial-branch=main")

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "--quiet", "--initial-branch=main")
    _git(seed, "config", "user.email", "t@example.com")
    _git(seed, "config", "user.name", "T")
    _write_doc(seed, SEEDED, slug="seeded", body="Original body.")
    _write_doc(seed, ROSTER, slug="reviewer", body="Roster doc.")
    (seed / PROFILE).parent.mkdir(parents=True, exist_ok=True)
    (seed / PROFILE).write_text("name: daytime\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "--quiet", "-m", "seed")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "--quiet", "-u", "origin", "main")

    checkout = tmp_path / "knowledge"
    subprocess.run(["git", "clone", "--quiet", str(origin), str(checkout)],
                   capture_output=True, text=True, check=True)
    _git(checkout, "config", "user.email", "t@example.com")
    _git(checkout, "config", "user.name", "T")

    monkeypatch.setattr(agentctl, "KNOWLEDGE_ROOT", checkout)
    monkeypatch.setattr(agentctl, "SCRIBE_WORKTREE", tmp_path / "cage-wt")
    monkeypatch.setattr(kw, "DB", tmp_path / "kb-writes.db")
    monkeypatch.setattr(agentctl, "_kb_writes", lambda: kw)
    kw.ensure_db()
    return Namespace(origin=origin, seed=seed, checkout=checkout, wt=tmp_path / "cage-wt")


def _args(doc, **kw_):
    base = {"doc": doc, "title": "A proposed change", "rationale": None,
            "base_sha": None, "edits": None, "content": None,
            "content_file": None, "edits_file": None, "holder": "run-test",
            "allow_overwrite": False, "dry_run": False, "delete": False, "also": [],
            "batch": None, "batch_file": None}
    base.update(kw_)
    return Namespace(**base)


def _origin_content(cage, rel):
    _git(cage.checkout, "fetch", "--quiet", "origin", "main")
    return _git(cage.checkout, "show", f"origin/main:{rel}").stdout


def test_propose_never_writes_main(cage, agentctl, monkeypatch):
    """The load-bearing property. Whatever else happens, main is untouched."""
    monkeypatch.setattr(agentctl.subprocess, "run", _fake_gh(0, "https://example/pr/1"))
    before = _git(cage.checkout, "rev-parse", "origin/main").stdout.strip()
    rc = agentctl.cmd_kb_propose(_args(
        SEEDED, edits='[{"old": "Original body.", "new": "Proposed."}]'))
    assert rc == 0
    _git(cage.checkout, "fetch", "--quiet", "origin", "main")
    assert _git(cage.checkout, "rev-parse", "origin/main").stdout.strip() == before
    assert "Original body." in _origin_content(cage, SEEDED)


def test_a_foreign_lease_refuses_the_proposal(cage, agentctl, monkeypatch, capsys):
    """operator, 2026-09-11: nobody changes a doc another crew has checked out. A
    proposal is a change too, so it is refused before any branch is built."""
    monkeypatch.setattr(agentctl.subprocess, "run", _fake_gh(0, "https://example/pr/1"))
    kw.acquire(SEEDED, "someone-else")
    rc = agentctl.cmd_kb_propose(_args(
        SEEDED, edits='[{"old": "Original body.", "new": "Proposed."}]'))
    assert rc == 3
    assert "leased by someone-else" in capsys.readouterr().err
    branches = _git(cage.origin, "branch", "--list", "kb/*").stdout.strip()
    assert branches == ""


def test_kb_propose_bootstraps_before_the_lease_journals_the_refusal(
        cage, agentctl, monkeypatch):
    """Round-1 (PR 695) left `venv_bootstrap.ensure` inside `_kb_indexer` and
    `_kb_propose_refusal`, reached only once a proposal got past the lease
    check below — so a re-exec here would restart the process AFTER that
    check had already journaled its `leased` row, and a second, duplicate row
    would land once the restarted process re-ran the same check. `cmd_kb_propose`
    now calls `ensure` itself, as its first action, unconditionally — so it
    runs even on a path, like this one, that never reaches `_kb_indexer` at
    all. Proved by recording the order `venv_bootstrap.ensure` and
    `kb_writes.journal` are actually called in, through the real
    `cmd_kb_propose` and a real foreign-lease refusal."""
    order = []
    monkeypatch.setattr(agentctl.venv_bootstrap, "ensure",
                        lambda *a, **k: order.append("ensure"))
    real_journal = kw.journal

    def recording_journal(*a, **k):
        order.append("journal")
        return real_journal(*a, **k)

    monkeypatch.setattr(kw, "journal", recording_journal)
    kw.acquire(SEEDED, "someone-else")

    rc = agentctl.cmd_kb_propose(_args(
        SEEDED, edits='[{"old": "Original body.", "new": "Proposed."}]'))

    assert rc == 3
    assert order == ["ensure", "journal"], (
        f"venv_bootstrap.ensure must run before the lease refusal journals "
        f"anything; got {order}")


def _fake_gh(rc: int, out: str = "", existing: str = ""):
    """Intercept only `gh`; let every git call through to the real binary.

    `gh pr list` answers with `existing`, the URL of an open PR already on the
    branch, or nothing; every other `gh` call answers with `rc` and `out`."""
    real = subprocess.run

    def run(cmd, *a, **kw_):
        if cmd[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(cmd, 0, existing, "")
        if cmd and cmd[0] == "gh":
            return subprocess.CompletedProcess(cmd, rc, out, "" if rc == 0 else "gh failed")
        return real(cmd, *a, **kw_)
    return run


def test_propose_opens_a_pr_and_returns_its_url(cage, agentctl, monkeypatch, capsys):
    monkeypatch.setattr(agentctl.subprocess, "run", _fake_gh(0, "https://example/pr/7\n"))
    rc = agentctl.cmd_kb_propose(_args(
        SEEDED, edits='[{"old": "Original body.", "new": "Proposed."}]'))
    assert rc == 0
    assert "https://example/pr/7" in capsys.readouterr().out


def test_propose_is_the_route_for_roster_paths(cage, agentctl, monkeypatch):
    """kb apply refuses these under every provenance; propose is where they go,
    so it must NOT refuse them in turn or the rule would have no escape."""
    monkeypatch.setattr(agentctl.subprocess, "run", _fake_gh(0, "https://example/pr/8"))
    rc = agentctl.cmd_kb_propose(_args(
        ROSTER, edits='[{"old": "Roster doc.", "new": "Reviewed change."}]'))
    assert rc == 0


def test_a_non_indexable_path_is_refused_before_any_git_work(
        cage, agentctl, monkeypatch, capsys):
    """A proposal that is not a `.md` under projects/ would merge into a file the
    indexer never serves — the same trap kb apply refuses, and the PR path is no
    exception: the validator checks frontmatter, not location, and review may not
    notice a wrong path either. Refused up front, before the worktree is built,
    and the refusal names the corrected path. Roster proposals stay possible
    (test_propose_is_the_route_for_roster_paths)."""
    def no_git(*a, **kw_):
        raise AssertionError("a refused proposal must not reach any git work")
    monkeypatch.setattr(agentctl.subprocess, "run", no_git)
    rc = agentctl.cmd_kb_propose(_args(
        "platform/contracts/library-protocol.md",
        content=DOC.format(slug="library-protocol", title="T", body="Body.")))
    err = capsys.readouterr().err
    assert rc == 1
    assert "not a path the indexer will index" in err
    assert "Did you mean 'projects/platform/contracts/library-protocol.md'?" in err


def test_propose_validates_before_opening_a_pr(cage, agentctl, monkeypatch, capsys):
    """A proposal that reddens CI wastes an entire review cycle discovering what
    the validator already knew."""
    # A document the indexer accepts, so this reaches validate.py's check: the
    # indexer's own check (test_a_document_the_indexer_would_drop_is_refused)
    # would otherwise refuse a frontmatter-less body first.
    monkeypatch.setattr(agentctl, "_kb_errors",
                        lambda root: {"ERROR: broken"} if "BROKEN" in (root / SEEDED).read_text()
                        else set())
    monkeypatch.setattr(agentctl.subprocess, "run", _fake_gh(0, "https://example/pr/9"))
    rc = agentctl.cmd_kb_propose(_args(
        SEEDED, content=DOC.format(slug="seeded", title="A doc", body="BROKEN body."),
        allow_overwrite=True))
    assert rc == 6
    assert "validation errors" in capsys.readouterr().err


def test_a_document_the_indexer_would_drop_is_refused(cage, agentctl, monkeypatch, capsys):
    """A PR that passes CI and review would still merge a document the indexer
    drops, since validate.py only warns on an audience value outside the roster
    (issue_01M2BVA23ZC5EJHXXZN5VZB9VZ). Refused before any branch is pushed."""
    monkeypatch.setattr(agentctl.subprocess, "run", _fake_gh(0, "https://example/pr/11"))
    rel = "projects/platform/reference/dropped.md"
    body = DOC.format(slug="dropped", title="Dropped", body="Would vanish.").replace(
        "audience: [architect]", "audience: [architect, not-a-roster-agent]")
    rc = agentctl.cmd_kb_propose(_args(rel, content=body))
    err = capsys.readouterr().err
    assert rc == 6, err
    assert "indexer would drop" in err and "not-a-roster-agent" in err, err
    assert _git(cage.origin, "branch", "--list", "kb/*").stdout.strip() == "", \
        "no proposal branch reached the origin"


def test_propose_leaves_the_cage_detached(cage, agentctl, monkeypatch):
    """A cage left on a branch makes the NEXT apply's `reset --hard origin/main`
    a branch switch rather than a no-op, which is how one verb's leftovers
    become another verb's surprise."""
    monkeypatch.setattr(agentctl.subprocess, "run", _fake_gh(0, "https://example/pr/10"))
    agentctl.cmd_kb_propose(_args(
        SEEDED, edits='[{"old": "Original body.", "new": "Proposed."}]'))
    head = _git(cage.wt, "symbolic-ref", "-q", "HEAD")
    assert head.returncode != 0, "cage worktree is still on a branch"


def test_the_cage_is_detached_even_when_the_push_fails(cage, agentctl, monkeypatch):
    """The failure path, which is where this went wrong.

    Returning with the cage still on a branch makes the NEXT `kb apply` run its
    `reset --hard origin/main` against that branch instead of a detached HEAD —
    one verb's error path quietly changing another verb's meaning. Found by code
    review of #247; the detach is now in a `finally`.
    """
    real = subprocess.run

    def fail_push(cmd, *a, **kw_):
        if cmd and cmd[0] == "git" and "push" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "push rejected")
        if cmd and cmd[0] == "gh":
            return subprocess.CompletedProcess(cmd, 0, "https://example/pr/1", "")
        return real(cmd, *a, **kw_)

    monkeypatch.setattr(agentctl.subprocess, "run", fail_push)
    rc = agentctl.cmd_kb_propose(_args(
        SEEDED, edits='[{"old": "Original body.", "new": "Proposed."}]'))
    assert rc == 1
    head = _git(cage.wt, "symbolic-ref", "-q", "HEAD")
    assert head.returncode != 0, "cage left on a branch after a failed push"


def test_the_cage_is_detached_even_when_the_commit_fails(cage, agentctl, monkeypatch):
    real = subprocess.run

    def fail_commit(cmd, *a, **kw_):
        if cmd and cmd[0] == "git" and "commit" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "commit failed")
        if cmd and cmd[0] == "gh":
            return subprocess.CompletedProcess(cmd, 0, "https://example/pr/1", "")
        return real(cmd, *a, **kw_)

    monkeypatch.setattr(agentctl.subprocess, "run", fail_commit)
    rc = agentctl.cmd_kb_propose(_args(
        SEEDED, edits='[{"old": "Original body.", "new": "Proposed."}]'))
    assert rc == 1
    head = _git(cage.wt, "symbolic-ref", "-q", "HEAD")
    assert head.returncode != 0, "cage left on a branch after a failed commit"


def test_propose_honours_a_stale_base_sha(cage, agentctl, monkeypatch):
    monkeypatch.setattr(agentctl.subprocess, "run", _fake_gh(0, "https://example/pr/11"))
    rc = agentctl.cmd_kb_propose(_args(
        SEEDED, base_sha=kw.content_sha("never on main"),
        edits='[{"old": "Original body.", "new": "x"}]'))
    assert rc == 5


def test_a_pr_failure_after_a_successful_push_is_reported(cage, agentctl, monkeypatch, capsys):
    """The branch is on origin but there is no PR — a state a caller must know
    about, since retrying would push the same branch again."""
    monkeypatch.setattr(agentctl.subprocess, "run", _fake_gh(1))
    rc = agentctl.cmd_kb_propose(_args(
        SEEDED, edits='[{"old": "Original body.", "new": "Proposed."}]'))
    assert rc == 1
    assert "branch pushed but PR creation failed" in capsys.readouterr().err


def test_the_branch_name_distinguishes_different_docs(cage, agentctl, monkeypatch):
    """A title-only slug collides whenever two proposals are similarly named, and
    'update the runbook' is not a distinctive string. Including the doc means a
    collision implies the same doc AND the same intent — a re-proposal rather
    than one agent's commit landing on another's branch (security review, #247)."""
    monkeypatch.setattr(agentctl.subprocess, "run", _fake_gh(0, "https://example/pr/1"))
    agentctl.cmd_kb_propose(_args(SEEDED, title="Update the runbook",
                                  edits='[{"old": "Original body.", "new": "A."}]'))
    agentctl.cmd_kb_propose(_args(ROSTER, title="Update the runbook",
                                  edits='[{"old": "Roster doc.", "new": "B."}]'))
    branches = _git(cage.checkout, "ls-remote", "--heads", str(cage.origin)).stdout
    names = sorted(ln.split("refs/heads/")[-1] for ln in branches.splitlines() if "kb/" in ln)
    assert len(names) == 2, f"two docs collided on one branch: {names}"
    assert all(n.startswith("kb/update-the-runbook-") for n in names)


def test_a_push_that_would_clobber_another_proposal_fails_rather_than_forcing(
        cage, agentctl, monkeypatch):
    """`--force-with-lease` protects only refs the worktree has tracking info
    for, and the cage fetches origin/main alone — so on a first push the lease
    protected nothing. The remote tip is now read explicitly."""
    monkeypatch.setattr(agentctl.subprocess, "run", _fake_gh(0, "https://example/pr/1"))
    agentctl.cmd_kb_propose(_args(SEEDED, title="Shared title",
                                  edits='[{"old": "Original body.", "new": "First."}]'))
    branch = [ln.split("refs/heads/")[-1] for ln in
              _git(cage.checkout, "ls-remote", "--heads", str(cage.origin)).stdout.splitlines()
              if "kb/" in ln][0]
    tip_before = _git(cage.checkout, "ls-remote", str(cage.origin),
                      f"refs/heads/{branch}").stdout.split()[0]

    # A second proposal for the same doc and title re-proposes: it may replace
    # the branch, but only the commit it actually looked at.
    agentctl.cmd_kb_propose(_args(SEEDED, title="Shared title",
                                  edits='[{"old": "Original body.", "new": "Second."}]'))
    tip_after = _git(cage.checkout, "ls-remote", str(cage.origin),
                     f"refs/heads/{branch}").stdout.split()[0]
    assert tip_after != tip_before, "the re-proposal should have updated its own branch"


def test_content_naming_a_real_file_is_written_literally(cage, agentctl, monkeypatch, tmp_path):
    """The cage itself no longer re-reads a `--content` value from disk.

    A boundary guard could not close this: two processes each calling is_file()
    disagree the moment a file appears between them, so the check was a race
    rather than a control. Splitting the flag removes the ambiguity for every
    caller at once, which is why the guard could then be deleted rather than
    hardened (security review, #247).
    """
    decoy = tmp_path / "someone-elses-doc.md"
    decoy.write_text("SECRET CONTENT THAT MUST NOT BE PUBLISHED")

    monkeypatch.setattr(agentctl.subprocess, "run", _fake_gh(0, "https://example/pr/1"))
    # The decoy path sits in a well-formed document's body: a bare path has no
    # frontmatter, so the indexer's own check would refuse it before any branch,
    # and the property here is literal writing, not validation.
    agentctl.cmd_kb_propose(_args("projects/platform/reference/new.md",
                                  content=DOC.format(slug="new", title="New", body=str(decoy)),
                                  allow_overwrite=True))
    written = (cage.wt / "projects/platform/reference/new.md")
    # The branch was pushed, so read what actually landed on it.
    blob = _git(cage.checkout, "show",
                f"origin/{_only_kb_branch(cage)}:projects/platform/reference/new.md").stdout
    assert "SECRET CONTENT" not in blob
    assert str(decoy) in blob
    assert not written.exists() or "SECRET CONTENT" not in written.read_text()


def _only_kb_branch(cage) -> str:
    lines = _git(cage.checkout, "ls-remote", "--heads", str(cage.origin)).stdout.splitlines()
    return [ln.split("refs/heads/")[-1] for ln in lines if "kb/" in ln][0]


def test_content_file_still_reads_from_disk_for_cli_callers(cage, agentctl, monkeypatch, tmp_path):
    """The read-from-disk case did not disappear, it moved to its own flag — the
    briefing job passes a temp file this way."""
    draft = tmp_path / "draft.md"
    draft.write_text(DOC.format(slug="new", title="New", body="From a file."))
    monkeypatch.setattr(agentctl.subprocess, "run", _fake_gh(0, "https://example/pr/2"))
    rc = agentctl.cmd_kb_propose(_args("projects/platform/reference/new.md",
                                       content_file=str(draft), allow_overwrite=True))
    assert rc == 0
    blob = _git(cage.checkout, "show",
                f"origin/{_only_kb_branch(cage)}:projects/platform/reference/new.md").stdout
    assert "From a file." in blob


def test_dry_run_opens_nothing(cage, agentctl, monkeypatch):
    real = subprocess.run          # captured BEFORE patching, or this recurses

    def no_gh(cmd, *a, **kw_):
        assert not (cmd and cmd[0] == "gh"), "dry-run must not call gh"
        return real(cmd, *a, **kw_)
    monkeypatch.setattr(agentctl.subprocess, "run", no_gh)
    rc = agentctl.cmd_kb_propose(_args(
        SEEDED, dry_run=True, edits='[{"old": "Original body.", "new": "x"}]'))
    assert rc == 0


def test_a_second_proposal_onto_an_open_pr_reports_the_update(
        cage, agentctl, monkeypatch, capsys):
    """knowledge#393: a revision force-pushed its own PR's branch and then reported
    "PR creation failed" because the PR already existed. The work happened; the
    report must say so, with the PR and the new head, and exit 0."""
    real = subprocess.run
    created = []

    def run(cmd, *a, **kw_):
        if cmd[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(cmd, 0, "https://example/pr/393\n", "")
        if cmd[:3] == ["gh", "pr", "create"]:
            created.append(cmd)
            return subprocess.CompletedProcess(cmd, 1, "", "already exists")
        if cmd[:3] == ["gh", "pr", "edit"]:
            edited.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real(cmd, *a, **kw_)
    edited = []
    monkeypatch.setattr(agentctl.subprocess, "run", run)
    rc = agentctl.cmd_kb_propose(_args(
        SEEDED, edits='[{"old": "Original body.", "new": "Revised."}]',
        rationale="Why the second revision."))
    out = capsys.readouterr().out
    assert rc == 0
    assert created == [], "an update must not try to open a second PR"
    assert "kb propose: updated https://example/pr/393 (head " in out
    # The description follows the revision (knowledge#393 needed it by hand).
    assert len(edited) == 1 and "https://example/pr/393" in edited[0]
    assert "Why the second revision." in edited[0][edited[0].index("--body") + 1]


def test_a_failed_push_still_fails_even_when_a_pr_exists(
        cage, agentctl, monkeypatch, capsys):
    """The update report is only for work that landed. A push that fails exits
    non-zero before any PR lookup, whatever is already open on the branch.

    A regression guard rather than new-behaviour coverage: it passes on the code
    before the lookup existed too. It fails if a later change moves the lookup
    ahead of the push, which would report an update that never landed."""
    real = subprocess.run
    looked_up = []

    def run(cmd, *a, **kw_):
        if cmd[:3] == ["gh", "pr", "list"]:
            looked_up.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "https://example/pr/393\n", "")
        if cmd and cmd[0] == "gh":
            return subprocess.CompletedProcess(cmd, 0, "https://example/pr/393\n", "")
        if "push" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "rejected")
        return real(cmd, *a, **kw_)
    monkeypatch.setattr(agentctl.subprocess, "run", run)
    rc = agentctl.cmd_kb_propose(_args(
        SEEDED, edits='[{"old": "Original body.", "new": "Revised."}]'))
    captured = capsys.readouterr()
    assert rc == 1
    assert "updated" not in captured.out
    assert looked_up == []


def test_a_failed_lookup_falls_through_to_creating_the_pr(
        cage, agentctl, monkeypatch, capsys):
    """If `gh pr list` itself fails, the tool cannot know whether a PR exists, so
    it does what it always did and creates one; that call's own result is then
    the report. Pins the fall-through the lookup's comment promises."""
    real = subprocess.run
    created = []

    def run(cmd, *a, **kw_):
        if cmd[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(cmd, 1, "", "gh: network unreachable")
        if cmd[:3] == ["gh", "pr", "create"]:
            created.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "https://example/pr/9\n", "")
        return real(cmd, *a, **kw_)
    monkeypatch.setattr(agentctl.subprocess, "run", run)
    rc = agentctl.cmd_kb_propose(_args(
        SEEDED, edits='[{"old": "Original body.", "new": "Proposed."}]'))
    out = capsys.readouterr().out
    assert rc == 0
    assert len(created) == 1
    assert "https://example/pr/9" in out and "updated" not in out


def test_a_failed_description_edit_warns_and_still_reports_the_update(
        cage, agentctl, monkeypatch, capsys):
    """The branch has landed by the time the description is refreshed, so a
    failed `gh pr edit` must not turn the revision into a reported failure: it
    warns on stderr, still reports the update and exits 0."""
    real = subprocess.run

    def run(cmd, *a, **kw_):
        if cmd[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(cmd, 0, "https://example/pr/393\n", "")
        if cmd[:3] == ["gh", "pr", "edit"]:
            return subprocess.CompletedProcess(cmd, 1, "", "HTTP 403: forbidden")
        if cmd[:3] == ["gh", "pr", "create"]:
            raise AssertionError("an update must not try to open a second PR")
        return real(cmd, *a, **kw_)
    monkeypatch.setattr(agentctl.subprocess, "run", run)
    rc = agentctl.cmd_kb_propose(_args(
        SEEDED, edits='[{"old": "Original body.", "new": "Revised."}]'))
    captured = capsys.readouterr()
    assert rc == 0
    assert "kb propose: updated https://example/pr/393 (head " in captured.out
    assert "could not be refreshed" in captured.err and "HTTP 403" in captured.err


# --- the cage is left as it was found ------------------------------------------


def _no_gh(monkeypatch, agentctl):
    real = subprocess.run

    def run(cmd, *a, **kw_):
        assert not (cmd and cmd[0] == "gh"), "a dry run must not call gh"
        return real(cmd, *a, **kw_)
    monkeypatch.setattr(agentctl.subprocess, "run", run)


def test_a_dry_run_edit_leaves_the_cage_clean(cage, agentctl, monkeypatch):
    """Seen 2026-09-11: a dry run after the #531 deploy left its edit on disk and
    its kb/ branch in the cage, because the dry run returned inside the try and
    the `finally` only detached."""
    _no_gh(monkeypatch, agentctl)
    rc = agentctl.cmd_kb_propose(_args(
        SEEDED, dry_run=True, edits='[{"old": "Original body.", "new": "x"}]'))
    assert rc == 0
    assert _git(cage.wt, "status", "--porcelain").stdout == ""
    assert _git(cage.wt, "branch", "--list", "kb/*").stdout.strip() == ""


def test_a_dry_run_create_leaves_no_file_behind(cage, agentctl, monkeypatch):
    """A new doc is untracked until committed, and `checkout -f` leaves
    untracked files alone, so the cleanup removes it by hand."""
    _no_gh(monkeypatch, agentctl)
    new = "projects/platform/reference/new.md"
    rc = agentctl.cmd_kb_propose(_args(
        new, dry_run=True, content=DOC.format(slug="new", title="New", body="B.")))
    assert rc == 0
    assert not (cage.wt / new).exists()


def test_a_refused_edit_leaves_the_cage_clean(cage, agentctl, monkeypatch):
    """The validation refusal returns from the same try as the dry run did."""
    # A document the indexer accepts, with the trigger in its body, so the
    # validate.py refusal is what fires: a bare "BROKEN" has no frontmatter and
    # would be refused earlier by the indexer's check, leaving this stub dead
    # (reviewer, #554).
    monkeypatch.setattr(agentctl, "_kb_errors",
                        lambda root: {"ERROR: broken"} if "BROKEN" in (root / SEEDED).read_text()
                        else set())
    _no_gh(monkeypatch, agentctl)
    rc = agentctl.cmd_kb_propose(_args(
        SEEDED, content=DOC.format(slug="seeded", title="A doc", body="BROKEN body."),
        allow_overwrite=True))
    assert rc == 6
    assert _git(cage.wt, "status", "--porcelain").stdout == ""


# --- --delete ------------------------------------------------------------------


def _no_git(monkeypatch, agentctl):
    def run(*a, **kw_):
        raise AssertionError("a refused delete must not reach any git work")
    monkeypatch.setattr(agentctl.subprocess, "run", run)


def test_a_delete_needs_the_callers_own_checkout(cage, agentctl, monkeypatch, capsys):
    """operator, 2026-09-11: only a checked-out item changes. An edit carries the base
    sha `kb checkout` returned; a delete has no content to carry one, so the
    caller's own lease is the evidence, and its absence refuses."""
    _no_git(monkeypatch, agentctl)
    rc = agentctl.cmd_kb_propose(_args(SEEDED, delete=True))
    assert rc == 1
    assert f"{SEEDED} is not checked out" in capsys.readouterr().err


def test_a_foreign_lease_refuses_a_delete(cage, agentctl, monkeypatch, capsys):
    _no_git(monkeypatch, agentctl)
    kw.acquire(SEEDED, "someone-else")
    rc = agentctl.cmd_kb_propose(_args(SEEDED, delete=True))
    assert rc == 3
    assert "leased by someone-else" in capsys.readouterr().err


def test_every_path_of_a_delete_needs_its_own_checkout(cage, agentctl, monkeypatch, capsys):
    _no_git(monkeypatch, agentctl)
    kw.acquire(SEEDED, "run-test")
    rc = agentctl.cmd_kb_propose(_args(SEEDED, also=[PROFILE], delete=True))
    assert rc == 1
    assert f"{PROFILE} is not checked out" in capsys.readouterr().err


def test_a_delete_opens_one_pr_removing_every_named_path(cage, agentctl, monkeypatch, capsys):
    """Several paths go in one PR, so a retirement is one review cycle. A .yaml
    profile is deletable, and main is untouched until the PR merges."""
    kw.acquire(SEEDED, "run-test")
    kw.acquire(PROFILE, "run-test")
    monkeypatch.setattr(agentctl.subprocess, "run", _fake_gh(0, "https://example/pr/12\n"))
    rc = agentctl.cmd_kb_propose(_args(SEEDED, also=[PROFILE], delete=True,
                                       title="Retire the seeded doc"))
    assert rc == 0
    assert "https://example/pr/12" in capsys.readouterr().out
    branch = _only_kb_branch(cage)
    tree = _git(cage.checkout, "ls-tree", "-r", "--name-only", f"origin/{branch}").stdout.split()
    assert SEEDED not in tree and PROFILE not in tree
    assert ROSTER in tree
    message = _git(cage.checkout, "log", "-1", "--format=%B", f"origin/{branch}").stdout
    assert f"KB-Write: propose {SEEDED} proposal run-test" in message
    assert f"KB-Write: propose {PROFILE} proposal run-test" in message
    assert "Original body." in _origin_content(cage, SEEDED)
    assert _git(cage.wt, "status", "--porcelain").stdout == ""


def test_a_delete_takes_no_content(cage, agentctl, monkeypatch, capsys):
    _no_git(monkeypatch, agentctl)
    kw.acquire(SEEDED, "run-test")
    rc = agentctl.cmd_kb_propose(_args(SEEDED, delete=True, content="x"))
    assert rc == 1
    assert "takes no edits or content" in capsys.readouterr().err


def test_several_paths_are_for_a_delete_only(cage, agentctl, monkeypatch, capsys):
    _no_git(monkeypatch, agentctl)
    rc = agentctl.cmd_kb_propose(_args(
        SEEDED, also=[ROSTER], edits='[{"old": "Original body.", "new": "x"}]'))
    assert rc == 1
    assert "only --delete takes several paths" in capsys.readouterr().err


def test_a_delete_outside_projects_is_refused(cage, agentctl, monkeypatch, capsys):
    _no_git(monkeypatch, agentctl)
    kw.acquire("scripts/notes.md", "run-test")
    rc = agentctl.cmd_kb_propose(_args("scripts/notes.md", delete=True))
    assert rc == 1
    assert "not under projects/" in capsys.readouterr().err


def test_a_delete_of_a_file_not_on_main_is_refused(cage, agentctl, monkeypatch, capsys):
    missing = "projects/platform/reference/never-existed.md"
    kw.acquire(missing, "run-test")
    monkeypatch.setattr(agentctl.subprocess, "run", _fake_gh(0, "https://example/pr/1"))
    rc = agentctl.cmd_kb_propose(_args(missing, delete=True))
    assert rc == 1
    assert "nothing to delete" in capsys.readouterr().err


def test_a_delete_that_breaks_validation_is_refused(cage, agentctl, monkeypatch, capsys):
    """Removing a document another one depends on is a validation error, and a
    PR that lands red wastes its review cycle."""
    monkeypatch.setattr(agentctl, "_kb_errors",
                        lambda root: set() if (root / SEEDED).exists()
                        else {"ERROR: depends_on names a missing document"})
    kw.acquire(SEEDED, "run-test")
    monkeypatch.setattr(agentctl.subprocess, "run", _fake_gh(0, "https://example/pr/1"))
    rc = agentctl.cmd_kb_propose(_args(SEEDED, delete=True))
    assert rc == 6
    assert "validation errors" in capsys.readouterr().err
    assert (cage.wt / SEEDED).is_file()
    assert _git(cage.wt, "status", "--porcelain").stdout == ""


def test_a_dry_run_delete_leaves_the_file_in_place(cage, agentctl, monkeypatch):
    kw.acquire(SEEDED, "run-test")
    _no_gh(monkeypatch, agentctl)
    rc = agentctl.cmd_kb_propose(_args(SEEDED, delete=True, dry_run=True))
    assert rc == 0
    assert (cage.wt / SEEDED).is_file()
    assert _git(cage.wt, "status", "--porcelain").stdout == ""


# --- profiles resolve as paths -------------------------------------------------


def test_a_profile_path_resolves_as_a_path(agentctl):
    """The index holds only .md, so a .yaml profile can only resolve as a path,
    and it is normalized and escape-checked like any other."""
    assert agentctl._kb_resolve_ref(PROFILE) == (PROFILE, "")
    ref, err = agentctl._kb_resolve_ref("projects/../../outside.yaml")
    assert ref is None and "escapes the knowledge root" in err


def test_a_profile_can_be_checked_out(cage, agentctl):
    """A delete needs the caller's own lease, so a profile has to be leasable."""
    rc = agentctl.cmd_kb_checkout(Namespace(doc=PROFILE, holder="run-test", note=None, sha_only=False))
    assert rc == 0
    assert kw.lease_for(PROFILE)["holder"] == "run-test"


def test_a_profile_edit_opens_a_pr(cage, agentctl, monkeypatch, capsys):
    """Profiles are PR-only under every provenance, so a proposal is the only
    route that can change one; refusing them left every profile uneditable once
    the knowledge repo required a KB-Write trailer (knowledge#399 met it first)."""
    monkeypatch.setattr(agentctl.subprocess, "run", _fake_gh(0, "https://example/pr/21\n"))
    rc = agentctl.cmd_kb_propose(_args(
        PROFILE, content="name: daytime\ninclude: []\n", allow_overwrite=True))
    assert rc == 0
    assert "https://example/pr/21" in capsys.readouterr().out
    blob = _git(cage.checkout, "show", f"origin/{_only_kb_branch(cage)}:{PROFILE}").stdout
    assert blob == "name: daytime\ninclude: []\n"
    assert "name: daytime\n" == _origin_content(cage, PROFILE)   # main is untouched


@pytest.mark.parametrize("content, why", [
    ("name: [unclosed\n", "is not valid YAML"),
    ("- a list\n- not a mapping\n", "must be a YAML mapping"),
])
def test_a_profile_knowledge_ctx_could_not_load_is_refused(
        cage, agentctl, monkeypatch, capsys, content, why):
    """The KB validator reads only .md, so it says nothing about a profile. The
    edit is parsed the way knowledge-ctx's load_profile reads it, and refused
    before any branch exists."""
    monkeypatch.setattr(agentctl.subprocess, "run", _fake_gh(0, "https://example/pr/1"))
    rc = agentctl.cmd_kb_propose(_args(PROFILE, content=content, allow_overwrite=True))
    assert rc == 6
    assert why in capsys.readouterr().err
    branches = _git(cage.checkout, "ls-remote", "--heads", str(cage.origin)).stdout
    assert "kb/" not in branches
    assert _git(cage.wt, "status", "--porcelain").stdout == ""


def test_a_yml_profile_is_refused_because_nothing_would_load_it(
        cage, agentctl, monkeypatch, capsys):
    """knowledge-ctx loads `<name>.yaml` and only that, so a .yml profile would
    merge and never be read (review, PR 540). Refused before any git work."""
    _no_git(monkeypatch, agentctl)
    rc = agentctl.cmd_kb_propose(_args("projects/shared/profiles/nightly.yml",
                                       content="name: nightly\n", allow_overwrite=True))
    assert rc == 1
    assert "not a path the indexer will index" in capsys.readouterr().err


def test_a_yaml_outside_profiles_is_still_refused(cage, agentctl, monkeypatch, capsys):
    """Only profiles are let past the .md rule. Any other YAML under projects/
    still fails the indexer-shape rule before any git work."""
    _no_git(monkeypatch, agentctl)
    rc = agentctl.cmd_kb_propose(_args("projects/platform/reference/notes.yaml",
                                       content="a: 1\n", allow_overwrite=True))
    assert rc == 1
    assert "not a path the indexer will index" in capsys.readouterr().err


POLICY = "projects/platform/policy/agent-policy.yaml"


def test_the_agent_policy_file_proposal_opens_a_pr(cage, agentctl, monkeypatch, capsys):
    """The policy file is a non-.md path a proposal may edit, so kb propose is
    the only route that can create it."""
    monkeypatch.setattr(agentctl.subprocess, "run", _fake_gh(0, "https://example/pr/22\n"))
    content = "policy: platform/agent-policy\nmerge_permission: none\n"
    rc = agentctl.cmd_kb_propose(_args(POLICY, content=content))
    assert rc == 0
    assert "https://example/pr/22" in capsys.readouterr().out
    blob = _git(cage.checkout, "show", f"origin/{_only_kb_branch(cage)}:{POLICY}").stdout
    assert blob == content


@pytest.mark.parametrize("ref", [
    "projects/platform/policy/other.yaml",
    "projects/constance/policy/agent-policy.yaml",
    "projects/platform/policy/agent-policy.yml",
    "projects/platform/agent-policy.yaml",
])
def test_no_other_policy_path_is_proposable(cage, agentctl, monkeypatch, capsys, ref):
    _no_git(monkeypatch, agentctl)
    rc = agentctl.cmd_kb_propose(_args(ref, content="a: 1\n", allow_overwrite=True))
    assert rc == 1
    assert "not a path the indexer will index" in capsys.readouterr().err


# --- --batch: several documents, one PR ------------------------------------------


def _batch(*entries):
    import json
    return json.dumps(list(entries))


EDIT_SEEDED = {"id": SEEDED, "edits": [{"old": "Original body.", "new": "Batched seeded."}]}
EDIT_ROSTER = {"id": ROSTER, "edits": [{"old": "Roster doc.", "new": "Batched roster."}]}


def test_a_batch_opens_one_pr_changing_every_document(cage, agentctl, monkeypatch, capsys):
    """A field added across a roster is one PR and one review cycle, not one per
    document; main is untouched until it merges."""
    kw.acquire(SEEDED, "run-test")
    kw.acquire(ROSTER, "run-test")
    monkeypatch.setattr(agentctl.subprocess, "run", _fake_gh(0, "https://example/pr/21\n"))
    rc = agentctl.cmd_kb_propose(_args(SEEDED, batch=_batch(EDIT_SEEDED, EDIT_ROSTER),
                                       title="Batch two documents"))
    assert rc == 0
    assert "https://example/pr/21" in capsys.readouterr().out
    branch = _only_kb_branch(cage)
    assert "Batched seeded." in _git(cage.checkout, "show", f"origin/{branch}:{SEEDED}").stdout
    assert "Batched roster." in _git(cage.checkout, "show", f"origin/{branch}:{ROSTER}").stdout
    commits = _git(cage.checkout, "rev-list", "--count", f"origin/main..origin/{branch}").stdout
    assert commits.strip() == "1"
    message = _git(cage.checkout, "log", "-1", "--format=%B", f"origin/{branch}").stdout
    assert f"KB-Write: propose {SEEDED} proposal run-test" in message
    assert f"KB-Write: propose {ROSTER} proposal run-test" in message
    assert "Original body." in _origin_content(cage, SEEDED)
    assert _git(cage.wt, "status", "--porcelain").stdout == ""


def test_every_document_in_a_batch_needs_its_own_checkout(cage, agentctl, monkeypatch, capsys):
    """Only a checked-out item changes, and a batch is where a missed checkout
    would be easiest to overlook."""
    _no_git(monkeypatch, agentctl)
    kw.acquire(SEEDED, "run-test")
    rc = agentctl.cmd_kb_propose(_args(SEEDED, batch=_batch(EDIT_SEEDED, EDIT_ROSTER)))
    assert rc == 1
    assert f"{ROSTER} is not checked out" in capsys.readouterr().err


def test_a_foreign_lease_on_one_document_refuses_the_batch(cage, agentctl, monkeypatch, capsys):
    _no_git(monkeypatch, agentctl)
    kw.acquire(SEEDED, "run-test")
    kw.acquire(ROSTER, "someone-else")
    rc = agentctl.cmd_kb_propose(_args(SEEDED, batch=_batch(EDIT_SEEDED, EDIT_ROSTER)))
    assert rc == 3
    assert "leased by someone-else" in capsys.readouterr().err


def test_a_stale_base_on_one_document_refuses_the_batch_before_any_branch(
        cage, agentctl, monkeypatch, capsys):
    kw.acquire(SEEDED, "run-test")
    kw.acquire(ROSTER, "run-test")
    monkeypatch.setattr(agentctl.subprocess, "run", _fake_gh(0, "https://example/pr/22\n"))
    stale = dict(EDIT_ROSTER, base_sha="0" * 64)
    rc = agentctl.cmd_kb_propose(_args(SEEDED, batch=_batch(EDIT_SEEDED, stale)))
    assert rc == 5
    assert f"stale base for {ROSTER}" in capsys.readouterr().err
    assert _git(cage.origin, "branch", "--list", "kb/*").stdout.strip() == ""


def test_one_document_the_indexer_would_drop_refuses_the_batch(cage, agentctl, monkeypatch, capsys):
    kw.acquire(SEEDED, "run-test")
    kw.acquire(ROSTER, "run-test")
    monkeypatch.setattr(agentctl.subprocess, "run", _fake_gh(0, "https://example/pr/23\n"))
    broken = {"id": ROSTER, "edits": [{"old": "status: current", "new": "status: bogus"}]}
    rc = agentctl.cmd_kb_propose(_args(SEEDED, batch=_batch(EDIT_SEEDED, broken)))
    assert rc == 6
    assert f"the knowledge indexer would drop {ROSTER}" in capsys.readouterr().err
    assert _git(cage.origin, "branch", "--list", "kb/*").stdout.strip() == ""


def test_a_document_already_current_is_left_out_of_the_batch_pr(cage, agentctl, monkeypatch, capsys):
    kw.acquire(SEEDED, "run-test")
    kw.acquire(ROSTER, "run-test")
    monkeypatch.setattr(agentctl.subprocess, "run", _fake_gh(0, "https://example/pr/24\n"))
    current = _origin_content(cage, ROSTER)
    rc = agentctl.cmd_kb_propose(_args(SEEDED, batch=_batch(
        EDIT_SEEDED, {"id": ROSTER, "content": current, "allow_overwrite": True})))
    assert rc == 0
    assert f"already current, not in the PR: {ROSTER}" in capsys.readouterr().err
    branch = _only_kb_branch(cage)
    changed = _git(cage.checkout, "diff", "--name-only", f"origin/main..origin/{branch}").stdout.split()
    assert changed == [SEEDED]


@pytest.mark.parametrize("batch,extra,message", [
    (_batch(EDIT_SEEDED, EDIT_SEEDED), {}, "appears twice in the batch"),
    (_batch(EDIT_ROSTER), {}, "is not one of the batch's documents"),
    (_batch(EDIT_SEEDED), {"edits": '[{"old": "a", "new": "b"}]'}, "--batch carries each document"),
    (_batch(EDIT_SEEDED), {"delete": True}, "--batch carries each document"),
    (_batch({"id": SEEDED}), {}, "needs exactly one of"),
    ("[]", {}, "must be a non-empty JSON list"),
    ("not json", {}, "is not valid JSON"),
])
def test_a_malformed_batch_is_refused_before_any_git_work(
        cage, agentctl, monkeypatch, capsys, batch, extra, message):
    _no_git(monkeypatch, agentctl)
    kw.acquire(SEEDED, "run-test")
    kw.acquire(ROSTER, "run-test")
    rc = agentctl.cmd_kb_propose(_args(SEEDED, batch=batch, **extra))
    assert rc == 1
    assert message in capsys.readouterr().err


def test_a_content_entry_replacing_an_existing_document_needs_allow_overwrite(
        cage, agentctl, monkeypatch, capsys):
    """The single-document rule: --content over an existing file needs
    --allow-overwrite. A batch entry is held to the same (reviewer, #605)."""
    kw.acquire(SEEDED, "run-test")
    replacement = _origin_content(cage, SEEDED).replace("Original body.", "Replaced.")
    monkeypatch.setattr(agentctl.subprocess, "run", _fake_gh(0, "https://example/pr/27\n"))
    rc = agentctl.cmd_kb_propose(_args(SEEDED, batch=_batch(
        {"id": SEEDED, "content": replacement})))
    assert rc == 1
    assert SEEDED in capsys.readouterr().err
    assert _git(cage.origin, "branch", "--list", "kb/*").stdout.strip() == ""


def test_a_content_entry_with_allow_overwrite_replaces_the_document(
        cage, agentctl, monkeypatch, capsys):
    kw.acquire(SEEDED, "run-test")
    monkeypatch.setattr(agentctl.subprocess, "run", _fake_gh(0, "https://example/pr/25\n"))
    replacement = _origin_content(cage, SEEDED).replace("Original body.", "Replaced.")
    rc = agentctl.cmd_kb_propose(_args(SEEDED, batch=_batch(
        {"id": SEEDED, "content": replacement, "allow_overwrite": True})))
    assert rc == 0
    branch = _only_kb_branch(cage)
    assert "Replaced." in _git(cage.checkout, "show", f"origin/{branch}:{SEEDED}").stdout


def test_a_re_proposal_whose_changed_set_shrank_lands_on_the_same_branch(
        cage, agentctl, monkeypatch, capsys):
    """The branch tag hashes the batch as submitted, so a second call where one
    document is already current still revises the first PR's branch rather than
    opening a second PR beside it (reviewer, #605)."""
    kw.acquire(SEEDED, "run-test")
    kw.acquire(ROSTER, "run-test")
    monkeypatch.setattr(agentctl.subprocess, "run", _fake_gh(0, "https://example/pr/26\n"))
    assert agentctl.cmd_kb_propose(_args(
        SEEDED, batch=_batch(EDIT_SEEDED, EDIT_ROSTER), title="Batch revise")) == 0
    first = _only_kb_branch(cage)
    current_roster = _origin_content(cage, ROSTER)
    revised = {"id": SEEDED, "edits": [{"old": "Original body.", "new": "Revised batch."}]}
    assert agentctl.cmd_kb_propose(_args(
        SEEDED, batch=_batch(revised, {"id": ROSTER, "content": current_roster,
                                       "allow_overwrite": True}),
        title="Batch revise")) == 0
    heads = [ln for ln in _git(cage.checkout, "ls-remote", "--heads", str(cage.origin)).stdout.splitlines()
             if "kb/" in ln]
    assert len(heads) == 1 and heads[0].endswith(first)
    assert "Revised batch." in _git(cage.checkout, "show", f"origin/{first}:{SEEDED}").stdout


def test_a_batch_that_creates_a_document_leaves_no_file_behind(cage, agentctl, monkeypatch):
    """`checkout -f` restores tracked files but leaves an uncommitted new one, so
    the cleanup removes what the attempt created, for every batch entry."""
    new_rel = "projects/platform/reference/brand-new.md"
    kw.acquire(SEEDED, "run-test")
    kw.acquire(new_rel, "run-test")
    monkeypatch.setattr(agentctl.subprocess, "run", _fake_gh(0, "https://example/pr/28\n"))
    new_doc = DOC.format(slug="brand-new", title="New", body="Fresh.")
    rc = agentctl.cmd_kb_propose(_args(SEEDED, dry_run=True, batch=_batch(
        EDIT_SEEDED, {"id": new_rel, "content": new_doc})))
    assert rc == 0
    assert not (cage.wt / new_rel).exists()
    assert (cage.wt / SEEDED).is_file()
    assert _git(cage.wt, "status", "--porcelain").stdout == ""


# --- every write is made as convivy-library, and refuses without that identity --------


def _gh_calls(monkeypatch, agentctl, existing: str = ""):
    """Every `gh` call as `(argv, env, cwd)`; git passes through to the real binary.
    `gh pr list` answers with `existing`, the URL of a PR already open, or nothing."""
    real = subprocess.run
    seen = []

    def run(cmd, *a, **kw_):
        if cmd and cmd[0] == "gh":
            seen.append((cmd, kw_.get("env"), kw_.get("cwd")))
            out = existing if cmd[:3] == ["gh", "pr", "list"] else "https://example/pr/50\n"
            return subprocess.CompletedProcess(cmd, 0, out, "")
        return real(cmd, *a, **kw_)
    monkeypatch.setattr(agentctl.subprocess, "run", run)
    return seen


def test_the_fetch_the_tip_read_and_the_branch_push_run_as_the_app_against_the_https_url(
        cage, agentctl, library_identity, monkeypatch):
    """None of them names `origin`, whose ssh remote is the operator's personal key (the
    fixture fails a call that does). The fetch is the first call made as the App,
    and it updates `origin/main`. A first push is plain; a revision leases the exact
    tip it read."""
    import kb_merge as KM
    monkeypatch.setattr(agentctl.subprocess, "run", _fake_gh(0, "https://example/pr/40\n"))
    kw.acquire(SEEDED, "run-test")
    assert agentctl.cmd_kb_propose(_args(
        SEEDED, edits='[{"old": "Original body.", "new": "One."}]')) == 0
    branch = _only_kb_branch(cage)
    tip = _git(cage.origin, "rev-parse", branch).stdout.strip()
    assert library_identity.calls == [
        ("fetch", "--quiet", KM.push_url(), KM.MAIN_REFSPEC),
        ("ls-remote", KM.push_url(), f"refs/heads/{branch}"),
        ("push", "-q", KM.push_url(), branch)]

    library_identity.calls.clear()
    assert agentctl.cmd_kb_propose(_args(
        SEEDED, edits='[{"old": "Original body.", "new": "One."}]', rationale="again")) == 0
    revisions = [c for c in library_identity.calls if c[0] == "push"]
    assert revisions == [("push", "-q", f"--force-with-lease={branch}:{tip}", KM.push_url(), branch)]


def test_creating_the_pr_runs_gh_as_the_app_with_the_pr_token(
        cage, agentctl, library_identity, monkeypatch):
    """`gh pr list` and `gh pr create` each get `GH_TOKEN` in their own environment,
    the PR token and not the push token, replacing one inherited from the caller.
    The identity was asked for with `pull_requests`, once, before anything ran."""
    monkeypatch.setenv("GH_TOKEN", "ghp_the_callers_own")
    seen = _gh_calls(monkeypatch, agentctl)
    kw.acquire(SEEDED, "run-test")
    assert agentctl.cmd_kb_propose(_args(
        SEEDED, edits='[{"old": "Original body.", "new": "One."}]')) == 0
    assert [c[:3] for c, _, _ in seen] == [["gh", "pr", "list"], ["gh", "pr", "create"]]
    for cmd, env, _ in seen:
        assert env["GH_TOKEN"] == "ghs_pr_test", cmd
        assert env["GH_PROMPT_DISABLED"] == "1"
        assert "ghs_push_test" not in "".join(env.values()), "the push token reached gh"
    assert library_identity.mints == ["push+pr"]


def test_updating_the_pr_runs_gh_as_the_app_with_the_pr_token(
        cage, agentctl, library_identity, monkeypatch):
    """The revision path: `gh pr list` finds the open PR and `gh pr edit` refreshes
    its description. Both run as the App."""
    seen = _gh_calls(monkeypatch, agentctl, existing="https://example/pr/393\n")
    kw.acquire(SEEDED, "run-test")
    assert agentctl.cmd_kb_propose(_args(
        SEEDED, edits='[{"old": "Original body.", "new": "One."}]')) == 0
    assert [c[:3] for c, _, _ in seen] == [["gh", "pr", "list"], ["gh", "pr", "edit"]]
    assert all(env["GH_TOKEN"] == "ghs_pr_test" for _, env, _ in seen)


def test_the_proposal_is_authored_and_committed_as_the_app_and_by_no_person(
        cage, agentctl, library_identity, monkeypatch):
    """Author AND committer. The cage's repo config names a person and so does the
    environment; neither may reach the commit."""
    import kb_merge as KM
    for var in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(var, "The Operator")
    for var in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(var, "operator@example.com")
    monkeypatch.setattr(agentctl.subprocess, "run", _fake_gh(0, "https://example/pr/51\n"))
    kw.acquire(SEEDED, "run-test")
    assert agentctl.cmd_kb_propose(_args(
        SEEDED, edits='[{"old": "Original body.", "new": "One."}]')) == 0
    branch = _only_kb_branch(cage)
    bot = f"{KM.commit_name()} <{KM.commit_email()}>"
    got = _git(cage.origin, "log", "-1", "--format=%an <%ae>%n%cn <%ce>%n%b", branch).stdout
    assert got.splitlines()[:2] == [bot, bot], got
    # The requesting session survives as the trailer's holder.
    assert f"KB-Write: propose {SEEDED} proposal run-test" in got


def test_without_the_app_identity_the_proposal_is_refused_and_nothing_is_left_behind(
        cage, agentctl, library_identity, monkeypatch, capsys):
    """The suite holds no App key (conftest), so the real identity refuses. The
    proposal must NOT fall back to the ssh remote or `gh`'s stored login.

    The identity is obtained before the lock and the fetch, so a refusal leaves
    nothing at all: `_scribe_worktree` is never reached, and neither is `gh`."""
    monkeypatch.setattr(agentctl, "_kb_library", library_identity.real)
    monkeypatch.setattr(agentctl, "_scribe_worktree",
                        lambda *a: (_ for _ in ()).throw(AssertionError("lock and fetch reached")))
    seen = _gh_calls(monkeypatch, agentctl)
    kw.acquire(SEEDED, "run-test")
    rc = agentctl.cmd_kb_propose(_args(
        SEEDED, edits='[{"old": "Original body.", "new": "One."}]'))
    err = capsys.readouterr().err
    assert rc == 2
    assert "cannot obtain the convivy-library App identity" in err and "App key:" in err
    assert "does not fall back to a personal credential" in err
    assert _git(cage.origin, "branch", "--list", "kb/*").stdout.strip() == ""
    assert [j for j in kw.recent_journal() if j["result"] == "no-identity"]
    assert library_identity.calls == [] and seen == []
    assert not cage.wt.exists(), "a refusal built the cage"


def test_a_dry_run_proposal_still_needs_the_identity_for_its_fetch(
        cage, agentctl, library_identity, monkeypatch):
    """A dry run makes no write, but the fetch that opens the cage is made as the
    App, so it refuses without one. It used to fetch over ssh as the operator."""
    monkeypatch.setattr(agentctl, "_kb_library", library_identity.real)
    monkeypatch.setattr(agentctl, "_scribe_worktree",
                        lambda *a: (_ for _ in ()).throw(AssertionError("lock and fetch reached")))
    kw.acquire(SEEDED, "run-test")
    assert agentctl.cmd_kb_propose(_args(
        SEEDED, dry_run=True, edits='[{"old": "Original body.", "new": "One."}]')) == 2


def test_reading_main_without_the_identity_raises_rather_than_reporting_a_missing_doc(
        cage, agentctl, library_identity, monkeypatch, capsys):
    """`kb checkout` reads main through the same fetch. Without the App identity it
    reports the refusal rather than falling back to the ssh remote, and it says
    "could not read", which is not the answer "the doc is not there"."""
    monkeypatch.setattr(agentctl, "_kb_library", library_identity.real)
    with pytest.raises(agentctl._KbOriginUnreadable) as e:
        agentctl._kb_blob_at_origin(SEEDED)
    assert "App key:" in str(e.value)
    assert "kb checkout: refused" in capsys.readouterr().err
    assert library_identity.calls == []


def _checkout(agentctl, doc=SEEDED, holder="run-test"):
    return agentctl.cmd_kb_checkout(Namespace(doc=doc, holder=holder, note=None, sha_only=False))


def _unreadable_journal():
    return [j for j in kw.recent_journal() if j["result"] == "unreadable"]


def test_a_checkout_that_could_not_read_main_holds_no_lease_and_fails(
        cage, agentctl, library_identity, monkeypatch, capsys):
    """The swallow this replaces: a refused identity became `exists: false`, empty
    content and exit 0, after a four-hour lease was taken, so every other session
    was blocked from a document that the caller had read nothing of. Now exit 1,
    no payload, no lease, and the refusal on stderr."""
    monkeypatch.setattr(agentctl, "_kb_library", library_identity.real)
    assert _checkout(agentctl) == 1
    out, err = capsys.readouterr()
    assert out == "", "a payload was printed for a read that did not happen"
    assert "App key:" in err and "nothing was checked out and no lease is held" in err
    assert kw.lease_for(SEEDED) is None
    assert len(_unreadable_journal()) == 1
    # A leftover lease would make this a 3 (`leased`) instead of the same read failure.
    assert _checkout(agentctl, holder="another-session") == 1
    assert kw.lease_for(SEEDED) is None


def test_a_checkout_whose_fetch_failed_holds_no_lease_and_fails(
        cage, agentctl, monkeypatch, capsys):
    """The other way to not read main: the identity is fine and the fetch fails."""
    class Refused(agentctl._KbLibrary):
        def __call__(self, root, *a):
            return subprocess.CompletedProcess(a, 128, "", "fatal: HTTP 403 Forbidden")
    monkeypatch.setattr(agentctl, "_kb_library", lambda **kw_: Refused("ghs_read_test"))
    assert _checkout(agentctl) == 1
    out, err = capsys.readouterr()
    assert out == "" and "could not fetch origin/main" in err and "HTTP 403 Forbidden" in err
    assert kw.lease_for(SEEDED) is None
    assert len(_unreadable_journal()) == 1


def test_a_failed_recheckout_leaves_the_lease_the_holder_already_had(
        cage, agentctl, library_identity, monkeypatch, capsys):
    """A same-session re-checkout is allowed and refreshes the lease. When that
    one cannot read main, the earlier checkout is still the holder's, with edits
    possibly in flight, so releasing it would let another session take the doc."""
    assert _checkout(agentctl) == 0
    capsys.readouterr()
    monkeypatch.setattr(agentctl, "_kb_library", library_identity.real)
    assert _checkout(agentctl) == 1
    assert kw.lease_for(SEEDED)["holder"] == "run-test"
    assert "no lease is held" not in capsys.readouterr().err


def test_a_checkout_reads_main_with_the_read_only_token(cage, agentctl, library_identity, capsys):
    """A document on main comes back with its content; one that is not on main is
    still a legitimate checkout (`kb_create` reserves its path this way) and
    reports `exists: false`. Both fetched with the read-only identity."""
    assert _checkout(agentctl) == 0
    got = json.loads(capsys.readouterr().out)
    assert got["exists"] is True and "Original body." in got["content"]
    new = "projects/platform/reference/not-yet.md"
    assert _checkout(agentctl, doc=new) == 0
    got = json.loads(capsys.readouterr().out)
    assert got["exists"] is False and got["content"] == ""
    assert kw.lease_for(new)["holder"] == "run-test"
    assert library_identity.mints == ["read", "read"]
    assert [c[0] for c in library_identity.calls] == ["fetch", "fetch"]


def test_a_proposal_from_a_run_ends_on_the_coauthor_line_after_the_kb_write(
        cage, agentctl, monkeypatch, tmp_path):
    """Pins placement at the propose site, which the source regex in
    test_kb_coauthor_trailer.py cannot: a present but mis-placed call passes it."""
    rd = tmp_path / "agent-runs" / "2026-09-23" / "run-abcd1234"
    rd.mkdir(parents=True)
    (rd / "meta.json").write_text(json.dumps({"model": "deepseek/deepseek-v4-flash"}))
    monkeypatch.setattr(agentctl, "RUNS_ROOT", tmp_path / "agent-runs")
    monkeypatch.setenv("AGENTCTL_RUN_ID", "run-abcd1234")
    monkeypatch.setattr(agentctl.subprocess, "run", _fake_gh(0, "https://example/pr/3"))
    rc = agentctl.cmd_kb_propose(_args(
        SEEDED, edits='[{"old": "Original body.", "new": "Proposed."}]'))
    assert rc == 0
    msg = _git(cage.checkout, "log", "-1", "--format=%B",
               f"origin/{_only_kb_branch(cage)}").stdout.rstrip().splitlines()
    name = agentctl.model_display_name("deepseek/deepseek-v4-flash")
    assert msg[-1] == f"Co-Authored-By: {name} <noreply@convivy.com>"
    assert msg[-2].startswith("KB-Write: propose ")
