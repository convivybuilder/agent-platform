"""Every git read of the knowledge repo's `origin/main` names `refs/remotes/origin/main`.

git resolves an unqualified `origin/main` through `refs/tags/` and `refs/heads/`
before `refs/remotes/`, so a tag (which the next fetch auto-follows) or a local
branch of that name steers the read and git prints only an "ambiguous" warning.
Each behavioural test here plants both decoys on a forged commit and asserts the
real remote-tracking content still wins.
"""
from __future__ import annotations

import json
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

import indexer
import kb_writes as kw

REPO = Path(__file__).resolve().parent.parent
SEEDED = "projects/platform/reference/seeded.md"

DOC = """---
id: platform/reference/seeded
kind: reference
project: platform
title: A doc
status: current
audience: [architect]
last_reviewed: 2026-07-25
authors: [operator]
---

{body}
"""


def _git(repo: Path, *a):
    return subprocess.run(["git", "-C", str(repo), *a],
                          capture_output=True, text=True, check=False)


def _commit_doc(repo: Path, body: str, msg: str) -> None:
    path = repo / SEEDED
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DOC.format(body=body))
    _git(repo, "add", "-A")
    assert _git(repo, "commit", "--quiet", "-m", msg).returncode == 0


@pytest.fixture
def repos(tmp_path, monkeypatch, agentctl, library_identity):
    """origin, a seed clone that pushes to it, and the shared checkout."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "--quiet", "--bare", "--initial-branch=main")
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "--quiet", "--initial-branch=main")
    _git(seed, "config", "user.email", "t@example.com")
    _git(seed, "config", "user.name", "T")
    _commit_doc(seed, "Original body.", "seed")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "--quiet", "-u", "origin", "main")
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "clone", "--quiet", str(origin), str(checkout)],
                   capture_output=True, text=True, check=True)
    _git(checkout, "config", "user.email", "t@example.com")
    _git(checkout, "config", "user.name", "T")

    monkeypatch.setattr(agentctl, "KNOWLEDGE_ROOT", checkout)
    monkeypatch.setattr(agentctl, "SCRIBE_WORKTREE", tmp_path / "cage-wt")
    monkeypatch.setattr(kw, "DB", tmp_path / "kb-writes.db")
    monkeypatch.setattr(agentctl, "_kb_writes", lambda: kw)
    monkeypatch.setattr(indexer, "KNOWLEDGE", checkout)
    monkeypatch.setattr(indexer, "PROJECTS", checkout / "projects")
    monkeypatch.setattr(indexer, "DB", tmp_path / "index.db")
    monkeypatch.setattr(indexer, "SCHEMA", REPO / "lib" / "knowledge" / "schema.sql")
    monkeypatch.setattr(agentctl, "_kb_indexer", lambda: indexer)
    kw.ensure_db()
    return Namespace(origin=origin, seed=seed, checkout=checkout)


def _plant_decoys(r) -> str:
    """A forged child of the checkout's HEAD, published as a tag AND a local branch
    both named `origin/main`. Returns its sha."""
    _git(r.seed, "checkout", "--quiet", "-b", "forged")
    _commit_doc(r.seed, "FORGED body.", "forged")
    _git(r.checkout, "fetch", "--quiet", str(r.seed), "forged")
    forged = _git(r.checkout, "rev-parse", "FETCH_HEAD").stdout.strip()
    _git(r.seed, "checkout", "--quiet", "main")
    assert _git(r.checkout, "tag", "origin/main", forged).returncode == 0
    assert _git(r.checkout, "branch", "origin/main", forged).returncode == 0
    return forged


def _advance_origin(r, body: str) -> str:
    _commit_doc(r.seed, body, "real")
    _git(r.seed, "push", "--quiet", "origin", "main")
    return _git(r.seed, "rev-parse", "HEAD").stdout.strip()


def test_kb_sync_fast_forwards_to_the_remote_tracking_ref(repos, agentctl):
    forged = _plant_decoys(repos)
    real = _advance_origin(repos, "Real body.")
    assert agentctl.cmd_kb_sync(Namespace(full=False, verify_only=False, no_heal=False,
                                          dry_run=False, quiet=False)) == 0
    head = _git(repos.checkout, "rev-parse", "HEAD").stdout.strip()
    assert head != forged
    assert head == real
    assert "Real body." in (repos.checkout / SEEDED).read_text()


def test_kb_apply_bases_on_the_remote_tracking_ref(repos, agentctl):
    _plant_decoys(repos)
    _git(repos.checkout, "fetch", "--quiet", "origin", "main")
    real = _git(repos.checkout, "show", f"refs/remotes/origin/main:{SEEDED}").stdout
    args = Namespace(
        doc=SEEDED, base_sha=kw.content_sha(real), provenance="in-session",
        edits='[{"old": "Original body.", "new": "Edited body."}]', content=None,
        content_file=None, edits_file=None, message=None, holder="run-test",
        allow_overwrite=False, dry_run=False, no_push=False, no_sync=True)
    assert agentctl.cmd_kb_apply(args) == 0
    _git(repos.checkout, "fetch", "--quiet", "origin", "main")
    landed = _git(repos.checkout, "show", f"refs/remotes/origin/main:{SEEDED}").stdout
    assert "Edited body." in landed
    assert "FORGED" not in landed


def test_kb_blob_at_origin_reads_the_remote_tracking_ref(repos, agentctl):
    _plant_decoys(repos)
    content, why = agentctl._kb_blob_at_origin(SEEDED)
    assert why == ""
    assert "Original body." in content
    assert "FORGED" not in content


# The display-only sites are pinned by source: (file, qualified text that must be
# present, unqualified text that must be absent).
PINS = [
    ("bin/agentctl", '"HEAD..refs/remotes/origin/main"', '"HEAD..origin/main"'),
    ("bin/agentctl", '"--abbrev-ref", "refs/remotes/origin/HEAD"',
     '"--abbrev-ref", "origin/HEAD"'),
]


@pytest.mark.parametrize("path,qualified,unqualified", PINS)
def test_call_site_names_the_ref_fully_qualified(path, qualified, unqualified):
    text = (REPO / path).read_text()
    assert qualified in text
    assert unqualified not in text


def test_kb_audit_reads_the_remote_tracking_ref(repos):
    import kb_audit
    forged = _plant_decoys(repos)
    _git(repos.checkout, "fetch", "--quiet", "origin", "main")
    out = kb_audit.audit(since="1.year", root=repos.checkout)
    assert "error" not in out
    assert out["counts"]
    shas = [c["sha"] for c in out["commits"]]
    assert forged[:9] not in shas
    assert len(shas) == 1


def _apply_args(r, **over):
    real = _git(r.checkout, "show", f"refs/remotes/origin/main:{SEEDED}").stdout
    base = dict(
        doc=SEEDED, base_sha=kw.content_sha(real), provenance="in-session",
        edits='[{"old": "Original body.", "new": "Edited body."}]', content=None,
        content_file=None, edits_file=None, message=None, holder="run-test",
        allow_overwrite=False, dry_run=False, no_push=False, no_sync=True)
    base.update(over)
    return Namespace(**base)


def _propose_args(**over):
    base = dict(doc=SEEDED, title="A proposed change", rationale=None, base_sha=None,
                edits='[{"old": "Original body.", "new": "Proposed."}]', content=None,
                content_file=None, edits_file=None, holder="run-test",
                allow_overwrite=False, dry_run=True, delete=False, also=[],
                batch=None, batch_file=None)
    base.update(over)
    return Namespace(**base)


def _real_main(r) -> str:
    return _git(r.checkout, "show-ref", "--verify", "--hash",
                "refs/remotes/origin/main").stdout.strip()


def test_kb_apply_fast_forwards_the_shared_checkout_to_the_remote_tracking_ref(repos, agentctl):
    """The post-write sync (`no_sync=False`) is the same trusted fast-forward as
    `kb sync`. Every other apply test passes no_sync=True and never reaches it."""
    forged = _plant_decoys(repos)
    assert agentctl.cmd_kb_apply(_apply_args(repos, no_sync=False)) == 0
    real = _real_main(repos)
    head = _git(repos.checkout, "rev-parse", "HEAD").stdout.strip()
    assert head == real
    assert head != forged
    body = (repos.checkout / SEEDED).read_text()
    assert "Edited body." in body
    assert "FORGED" not in body


def test_kb_propose_restores_the_cage_to_the_remote_tracking_ref(repos, agentctl):
    """`cmd_kb_propose`'s `finally` detaches the cage at main. With a tag and a
    branch named `origin/main` on a forged commit, the short name would detach it
    at the forged commit, which the next apply then reads as main."""
    forged = _plant_decoys(repos)
    assert agentctl.cmd_kb_propose(_propose_args()) == 0
    cage = agentctl.SCRIBE_WORKTREE
    assert _git(cage, "symbolic-ref", "-q", "HEAD").returncode != 0, "cage is detached"
    head = _git(cage, "rev-parse", "HEAD").stdout.strip()
    assert head == _real_main(repos)
    assert head != forged
    assert "Original body." in (cage / SEEDED).read_text()


def test_scribe_worktree_reuse_resets_to_the_remote_tracking_ref(repos, agentctl):
    """The second call finds an existing cage and takes the `reset --hard` path."""
    library = agentctl._kb_library()
    cage, err = agentctl._scribe_worktree(library)
    assert err == "" and cage is not None
    forged = _plant_decoys(repos)
    real = _advance_origin(repos, "Real body.")
    again, err = agentctl._scribe_worktree(library)
    assert err == "" and again == cage
    head = _git(cage, "rev-parse", "HEAD").stdout.strip()
    assert head == real
    assert head != forged
    assert "Real body." in (cage / SEEDED).read_text()


def test_session_view_reads_the_remote_tracking_ref(repos, monkeypatch):
    import session_view
    _plant_decoys(repos)
    monkeypatch.setattr(session_view, "_KNOWLEDGE_REPO", repos.checkout)
    content, _sha = session_view._read_js_module(SEEDED)
    assert "Original body." in content
    assert "FORGED" not in content


def test_vivy_reset_lands_on_the_remote_tracking_ref(repos):
    import vivy_main_suite
    forged = _plant_decoys(repos)
    real = _advance_origin(repos, "Real body.")
    assert vivy_main_suite.reset_to_origin_main(repos.checkout) == real
    head = _git(repos.checkout, "rev-parse", "HEAD").stdout.strip()
    assert head == real
    assert head != forged


def test_velocity_report_counts_against_the_remote_tracking_ref(repos, velocity_report, monkeypatch):
    forged = _plant_decoys(repos)
    monkeypatch.setattr(velocity_report, "repo_path", lambda repo: repos.checkout)
    ref = velocity_report.main_ref("knowledge")
    assert ref == _real_main(repos)
    assert ref != forged


# --- the real remote-tracking ref is ABSENT -------------------------------------
# With `refs/remotes/origin/main` gone (the remote renamed its default branch and
# `fetch --prune` deleted it), git's search rules read `refs/tags/refs/remotes/
# origin/main` and `refs/heads/refs/remotes/origin/main` for the same name, so a
# tag pushed to the remote is read as main. Every trusted read resolves the exact
# ref with `show-ref --verify` and refuses when it is missing.

def _absent_ref_with_decoys(r, agentctl, monkeypatch) -> str:
    """Delete the checkout's real remote-tracking ref and stand a tag and a local
    branch, both literally named `refs/remotes/origin/main`, on a forged child of
    HEAD. The fetch is stubbed to succeed and change nothing, since the real one
    would put the ref straight back from the remote's main. Returns the forged sha."""
    _git(r.seed, "checkout", "--quiet", "-b", "forged")
    _commit_doc(r.seed, "FORGED body.", "forged")
    _git(r.checkout, "fetch", "--quiet", str(r.seed), "forged")
    forged = _git(r.checkout, "rev-parse", "FETCH_HEAD").stdout.strip()
    _git(r.seed, "checkout", "--quiet", "main")
    assert _git(r.checkout, "tag", "refs/remotes/origin/main", forged).returncode == 0
    assert _git(r.checkout, "branch", "refs/remotes/origin/main", forged).returncode == 0
    assert _git(r.checkout, "update-ref", "-d", "refs/remotes/origin/main").returncode == 0
    # The decoys really do stand in for the missing ref under git's search rules.
    assert _git(r.checkout, "rev-parse", "--verify", "-q",
                "refs/remotes/origin/main").stdout.strip() == forged
    monkeypatch.setattr(agentctl._KbLibrary, "fetch_main",
                        lambda self, root: subprocess.CompletedProcess([], 0, "", ""))
    return forged


def test_kb_sync_refuses_when_the_ref_is_absent(repos, agentctl, monkeypatch, capsys):
    _absent_ref_with_decoys(repos, agentctl, monkeypatch)
    before = _git(repos.checkout, "rev-parse", "HEAD").stdout.strip()
    rc = agentctl.cmd_kb_sync(Namespace(full=False, verify_only=False, no_heal=False,
                                        dry_run=False, quiet=False))
    assert rc == 1
    assert "refs/remotes/origin/main does not exist" in capsys.readouterr().err
    assert _git(repos.checkout, "rev-parse", "HEAD").stdout.strip() == before
    assert "FORGED" not in (repos.checkout / SEEDED).read_text()


def test_kb_apply_refuses_when_the_ref_is_absent(repos, agentctl, monkeypatch, capsys):
    _absent_ref_with_decoys(repos, agentctl, monkeypatch)
    origin_before = _git(repos.origin, "rev-parse", "main").stdout.strip()
    real = _git(repos.checkout, "show", f"HEAD:{SEEDED}").stdout
    args = Namespace(
        doc=SEEDED, base_sha=kw.content_sha(real), provenance="in-session",
        edits='[{"old": "Original body.", "new": "Edited body."}]', content=None,
        content_file=None, edits_file=None, message=None, holder="run-test",
        allow_overwrite=False, dry_run=False, no_push=False, no_sync=True)
    assert agentctl.cmd_kb_apply(args) != 0
    assert "refs/remotes/origin/main does not exist" in capsys.readouterr().err
    assert _git(repos.origin, "rev-parse", "main").stdout.strip() == origin_before
    assert not (tmp_cage := agentctl.SCRIBE_WORKTREE).exists(), tmp_cage


def test_kb_blob_at_origin_is_unreadable_when_the_ref_is_absent(repos, agentctl, monkeypatch):
    _absent_ref_with_decoys(repos, agentctl, monkeypatch)
    # Raised, not (None, why): the caller reads (None, why) as "the doc is not on
    # main" and would report exists:false about a doc that is there.
    with pytest.raises(agentctl._KbOriginUnreadable, match="refs/remotes/origin/main"):
        agentctl._kb_blob_at_origin(SEEDED)


def test_kb_audit_refuses_when_the_ref_is_absent(repos, agentctl, monkeypatch):
    import kb_audit
    _absent_ref_with_decoys(repos, agentctl, monkeypatch)
    out = kb_audit.audit(since="1.year", root=repos.checkout)
    assert "refs/remotes/origin/main does not exist" in out["error"]
    assert out["commits"] == []


def test_session_view_refuses_when_the_ref_is_absent(repos, agentctl, monkeypatch):
    import session_view
    _absent_ref_with_decoys(repos, agentctl, monkeypatch)
    monkeypatch.setattr(session_view, "_KNOWLEDGE_REPO", repos.checkout)
    with pytest.raises(RuntimeError, match="refs/remotes/origin/main does not exist"):
        session_view._read_js_module(SEEDED)


def test_vivy_reset_refuses_when_the_ref_is_absent(repos, agentctl, monkeypatch):
    import vivy_main_suite
    forged = _absent_ref_with_decoys(repos, agentctl, monkeypatch)
    before = _git(repos.checkout, "rev-parse", "HEAD").stdout.strip()
    real_run = subprocess.run
    # The real `fetch origin main` would restore the ref from the remote's main.
    monkeypatch.setattr(vivy_main_suite.subprocess, "run", lambda cmd, **kw: (
        subprocess.CompletedProcess(cmd, 0, b"", b"") if cmd[1] == "fetch"
        else real_run(cmd, **kw)))
    with pytest.raises(RuntimeError, match="refs/remotes/origin/main does not exist"):
        vivy_main_suite.reset_to_origin_main(repos.checkout)
    assert _git(repos.checkout, "rev-parse", "HEAD").stdout.strip() == before != forged


def test_the_helper_returns_none_for_an_absent_ref_and_the_sha_for_a_present_one(repos, agentctl, monkeypatch):
    real = _git(repos.checkout, "rev-parse", "refs/remotes/origin/main").stdout.strip()
    assert agentctl._exact_origin_main(repos.checkout) == real
    _absent_ref_with_decoys(repos, agentctl, monkeypatch)
    assert agentctl._exact_origin_main(repos.checkout) is None


def test_velocity_report_does_not_count_a_tag_standing_in_for_an_absent_ref(repos, agentctl, velocity_report, monkeypatch):
    _absent_ref_with_decoys(repos, agentctl, monkeypatch)
    monkeypatch.setattr(velocity_report, "repo_path", lambda repo: repos.checkout)
    assert velocity_report.main_ref("knowledge") == "HEAD"


# --- the ref vanishes AFTER the cage is built ------------------------------------
# `_scribe_worktree` refuses first when the ref is missing at fetch time, so the
# absent-ref tests above never reach `cmd_kb_apply`'s own refusal or `cmd_kb_propose`'s
# unrestored-cage message. These delete the ref (and stand a tag and a branch of that
# literal name on a forged commit) once the cage exists.

def _lose_the_ref_once_the_cage_is_built(r, agentctl, monkeypatch) -> str:
    _git(r.seed, "checkout", "--quiet", "-b", "forged")
    _commit_doc(r.seed, "FORGED body.", "forged")
    _git(r.checkout, "fetch", "--quiet", str(r.seed), "forged")
    forged = _git(r.checkout, "rev-parse", "FETCH_HEAD").stdout.strip()
    _git(r.seed, "checkout", "--quiet", "main")
    real_scribe = agentctl._scribe_worktree

    def scribe_then_lose_the_ref(library):
        out = real_scribe(library)
        assert out[1] == ""
        assert _git(r.checkout, "tag", "refs/remotes/origin/main", forged).returncode == 0
        assert _git(r.checkout, "branch", "refs/remotes/origin/main", forged).returncode == 0
        assert _git(r.checkout, "update-ref", "-d", "refs/remotes/origin/main").returncode == 0
        return out

    monkeypatch.setattr(agentctl, "_scribe_worktree", scribe_then_lose_the_ref)
    return forged


def test_kb_apply_refuses_when_the_ref_vanishes_after_the_cage_is_built(repos, agentctl, monkeypatch, capsys):
    real = _real_main(repos)
    args = _apply_args(repos)
    _lose_the_ref_once_the_cage_is_built(repos, agentctl, monkeypatch)
    origin_before = _git(repos.origin, "rev-parse", "main").stdout.strip()
    assert agentctl.cmd_kb_apply(args) == 1
    err = capsys.readouterr().err
    assert "kb apply: refused" in err and "refs/remotes/origin/main does not exist" in err
    assert _git(repos.origin, "rev-parse", "main").stdout.strip() == origin_before == real
    assert "Original body." in (agentctl.SCRIBE_WORKTREE / SEEDED).read_text(), "edit restored"


def test_kb_propose_says_the_restore_was_skipped_when_the_ref_vanishes(repos, agentctl, monkeypatch, capsys):
    forged = _lose_the_ref_once_the_cage_is_built(repos, agentctl, monkeypatch)
    assert agentctl.cmd_kb_propose(_propose_args()) == 0
    err = capsys.readouterr().err
    assert "The restore checkout was skipped because the ref is missing" in err
    assert "Nothing was changed" not in err and "Not everything was restored" not in err
    cage = agentctl.SCRIBE_WORKTREE
    assert _git(cage, "rev-parse", "HEAD").stdout.strip() != forged, \
        "the tag standing in for the ref must not be checked out"
    # The message claims each of these, so each is asserted directly.
    assert _git(cage, "symbolic-ref", "-q", "HEAD").returncode != 0, "the cage is detached"
    assert _git(cage, "branch", "--list", "kb/*").stdout.strip() == "", \
        "the propose branch no longer exists"
    assert "Original body." in (cage / SEEDED).read_text() and "Proposed." not in (cage / SEEDED).read_text(), \
        "the edit to the tracked doc is gone"
    assert _git(cage, "status", "--porcelain").stdout.strip() == "", "the cage is clean"


NEW_REL = "projects/platform/reference/brand-new.md"
NEW_DOC = """---
id: platform/reference/brand-new
kind: reference
project: platform
title: Brand new
status: current
audience: [architect]
last_reviewed: 2026-07-25
authors: [operator]
---

Fresh.
"""


def _fake_gh(rc: int, out: str = ""):
    """Intercept only `gh`; every git call passes through to the real binary."""
    real = subprocess.run

    def run(cmd, *a, **kw_):
        if cmd and cmd[0] == "gh":
            return subprocess.CompletedProcess(cmd, rc, out, "" if rc == 0 else "gh failed")
        return real(cmd, *a, **kw_)
    return run


def test_kb_propose_discards_a_committed_change_when_the_ref_vanishes(
        repos, agentctl, monkeypatch, capsys):
    """Round 4 finding: by cleanup time on a non-dry-run propose, the cage's own
    HEAD IS the proposal commit, whatever the later push or PR step did. Detaching
    at HEAD and resetting to it (the old cleanup) kept exactly the edit and the
    created file the message claimed to discard. Recording the cage's
    pre-proposal commit before any write, and detaching cleanup back to THAT sha,
    is what makes the claim true whether or not the proposal committed."""
    _lose_the_ref_once_the_cage_is_built(repos, agentctl, monkeypatch)
    pre_sha = _git(repos.checkout, "rev-parse", "HEAD").stdout.strip()
    monkeypatch.setattr(agentctl.subprocess, "run", _fake_gh(0, "https://example/pr/1\n"))
    kw.acquire(SEEDED, "run-test")
    kw.acquire(NEW_REL, "run-test")
    batch = json.dumps([
        {"id": SEEDED, "edits": [{"old": "Original body.", "new": "Proposed."}]},
        {"id": NEW_REL, "content": NEW_DOC},
    ])
    assert agentctl.cmd_kb_propose(_propose_args(dry_run=False, batch=batch, edits=None)) == 0
    err = capsys.readouterr().err
    assert "the cage was detached at its pre-proposal commit" in err
    assert "Not everything was restored" not in err
    cage = agentctl.SCRIBE_WORKTREE
    # The message claims each of these, so each is asserted directly against the
    # cage rather than trusted from the printed text.
    assert _git(cage, "symbolic-ref", "-q", "HEAD").returncode != 0, "the cage is detached"
    assert _git(cage, "rev-parse", "HEAD").stdout.strip() == pre_sha, \
        "detached at the commit before this proposal, not at HEAD (the proposal commit)"
    assert _git(cage, "branch", "--list", "kb/*").stdout.strip() == "", \
        "the propose branch no longer exists"
    assert "Original body." in (cage / SEEDED).read_text() and \
        "Proposed." not in (cage / SEEDED).read_text(), \
        "the edit to the existing tracked doc is gone"
    assert not (cage / NEW_REL).exists(), "the created document is gone"
    assert _git(cage, "status", "--porcelain").stdout.strip() == "", "the cage is clean"
