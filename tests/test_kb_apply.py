"""`agentctl kb apply` — the sha-gated, policy-gated write path (ARCH-408).

Driven against real git repositories. The property under test is that a write is
evaluated against fresh `origin/main` INSIDE the write lock, and a mock of the
git layer would assume away exactly the race this exists to close.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
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


def _git(repo: Path, *a):
    return subprocess.run(["git", "-C", str(repo), *a],
                          capture_output=True, text=True, check=False)


def _write_doc(root: Path, rel: str, *, slug: str, body: str, title: str = "A doc") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DOC.format(slug=slug, title=title, body=body))
    return path


SEEDED = "projects/platform/reference/seeded.md"
ROSTER = "projects/agent-platform/agents/reviewer.md"


@pytest.fixture
def cage(tmp_path, monkeypatch, agentctl, library_identity):
    """An origin, a shared checkout, and a cage worktree path — the real topology."""
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
    return Namespace(origin=origin, seed=seed, checkout=checkout, tmp=tmp_path)


def _args(doc, base_sha, provenance="in-session", **kw_):
    base = {"doc": doc, "base_sha": base_sha, "provenance": provenance,
            "edits": None, "content": None, "content_file": None, "edits_file": None,
            "message": None, "holder": "run-test",
            "allow_overwrite": False, "dry_run": False, "no_push": False,
            "no_sync": True}
    base.update(kw_)
    return Namespace(**base)


def _sha_on_origin(cage, rel: str) -> str:
    r = _git(cage.checkout, "fetch", "--quiet", "origin", "main")
    assert r.returncode == 0
    out = _git(cage.checkout, "show", f"origin/main:{rel}")
    return kw.content_sha(out.stdout if out.returncode == 0 else "")


def _origin_content(cage, rel: str) -> str:
    _git(cage.checkout, "fetch", "--quiet", "origin", "main")
    return _git(cage.checkout, "show", f"origin/main:{rel}").stdout


# --- the happy path ----------------------------------------------------------


def test_apply_edits_and_pushes(cage, agentctl):
    sha = _sha_on_origin(cage, SEEDED)
    rc = agentctl.cmd_kb_apply(_args(
        SEEDED, sha, edits='[{"old": "Original body.", "new": "Edited body."}]'))
    assert rc == 0
    assert "Edited body." in _origin_content(cage, SEEDED)


def test_the_commit_carries_the_audit_trailer(cage, agentctl):
    """The git log is the audit surface: any main commit without this trailer and
    without a merge parent is a write that bypassed the cage."""
    sha = _sha_on_origin(cage, SEEDED)
    agentctl.cmd_kb_apply(_args(
        SEEDED, sha, provenance="in-session",
        edits='[{"old": "Original body.", "new": "Edited."}]'))
    _git(cage.checkout, "fetch", "--quiet", "origin", "main")
    msg = _git(cage.checkout, "log", "-1", "--format=%B", "origin/main").stdout
    assert f"KB-Write: update {SEEDED} in-session run-test" in msg


def _apply_and_read_message(cage, agentctl) -> str:
    sha = _sha_on_origin(cage, SEEDED)
    rc = agentctl.cmd_kb_apply(_args(
        SEEDED, sha, edits='[{"old": "Original body.", "new": "Edited."}]'))
    assert rc == 0
    _git(cage.checkout, "fetch", "--quiet", "origin", "main")
    return _git(cage.checkout, "log", "-1", "--format=%B", "origin/main").stdout


def test_a_write_with_no_run_carries_no_coauthor_line(cage, agentctl, monkeypatch):
    """An operator's hand save must not credit Claude (knowledge commit f0a79db)."""
    monkeypatch.delenv("AGENTCTL_RUN_ID", raising=False)
    msg = _apply_and_read_message(cage, agentctl)
    assert "Co-Authored-By" not in msg
    assert "KB-Write: update" in msg


def test_a_write_from_a_run_names_the_model_that_ran(cage, agentctl, monkeypatch):
    rd = cage.tmp / "agent-runs" / "2026-09-23" / "run-abcd1234"
    rd.mkdir(parents=True)
    (rd / "meta.json").write_text(json.dumps({"model": "deepseek/deepseek-v4-flash"}))
    monkeypatch.setattr(agentctl, "RUNS_ROOT", cage.tmp / "agent-runs")
    monkeypatch.setenv("AGENTCTL_RUN_ID", "run-abcd1234")
    msg = _apply_and_read_message(cage, agentctl)
    name = agentctl.model_display_name("deepseek/deepseek-v4-flash")
    assert msg.rstrip().endswith(f"Co-Authored-By: {name} <noreply@convivy.com>")
    assert "Opus" not in msg


def test_apply_creates_a_new_doc_from_the_empty_sha(cage, agentctl):
    body = DOC.format(slug="fresh", title="Fresh", body="Brand new.")
    rc = agentctl.cmd_kb_apply(_args(
        "projects/platform/reference/fresh.md", kw.content_sha(""), content=body))
    assert rc == 0
    assert "Brand new." in _origin_content(cage, "projects/platform/reference/fresh.md")


def test_a_document_the_indexer_would_drop_is_refused_and_nothing_reaches_main(
        cage, agentctl, capsys):
    """validate.py only WARNS on an audience value outside the roster, and the
    indexer drops the row. On 2026-09-12 kb_create reported "created" for such a
    decision, it landed on main, and no agent could read it
    (issue_01M2BVA23ZC5EJHXXZN5VZB9VZ). The write must refuse with exit 6, restore
    the cage, and commit nothing."""
    rel = "projects/platform/reference/dropped.md"
    body = DOC.format(slug="dropped", title="Dropped", body="Would vanish.").replace(
        "audience: [architect]", "audience: [architect, not-a-roster-agent]")
    main_before = _git(cage.origin, "rev-parse", "main").stdout.strip()
    rc = agentctl.cmd_kb_apply(_args(rel, kw.content_sha(""), content=body))
    err = capsys.readouterr().err
    assert rc == 6, err
    assert "indexer would drop" in err and "not-a-roster-agent" in err, err
    assert _git(cage.origin, "rev-parse", "main").stdout.strip() == main_before, \
        "nothing reached origin/main"
    assert not (cage.tmp / "cage-wt" / rel).exists(), "the cage is restored"


# --- the indexer's shape rule ------------------------------------------------

def test_a_path_outside_projects_is_refused_before_any_commit(cage, agentctl, capsys):
    """The bug: a path that is the frontmatter id without the `projects/` prefix
    wrote and reported ok, while the indexer ignores it forever — retrievable by
    nobody, found only by accident. The refusal must name the corrected path,
    since the two differ by exactly that prefix."""
    content = DOC.format(slug="library-protocol", title="Library Protocol",
                         body="The protocol nobody could retrieve.")
    rc = agentctl.cmd_kb_apply(_args(
        "platform/contracts/library-protocol.md", kw.content_sha(""), content=content))
    err = capsys.readouterr().err
    assert rc == 1
    assert "platform/contracts/library-protocol.md" in err
    assert "projects/platform/contracts/library-protocol.md" in err
    assert content not in _origin_content(cage, "platform/contracts/library-protocol.md")


def test_a_non_md_path_under_projects_is_refused(cage, agentctl, capsys):
    """A `.txt` under projects/ is as unretrievable as a path outside it — the
    indexer serves only `.md`, so the write must not land."""
    content = DOC.format(slug="notes", title="Notes", body="Not a doc.")
    rc = agentctl.cmd_kb_apply(_args(
        "projects/platform/reference/notes.txt", kw.content_sha(""), content=content))
    assert rc == 1
    assert content not in _origin_content(cage, "projects/platform/reference/notes.txt")
    # The hint a refusal gives must be a path that WOULD work. It used to APPEND
    # `.md`, suggesting `notes.txt.md` — itself unindexable, so a caller retrying
    # on it would hit a second refusal (round-2 review). The earlier test asserted
    # only that "notes.txt" and ".md" both appeared in the error, both trivially
    # true of the broken hint. Pin the corrected path on the hint builder itself:
    # the cage resolves a `.txt` argument as a document id and refuses it before
    # the shape guard runs, so the hint is not reachable through this input.
    assert agentctl._kb_indexable_hint("projects/platform/reference/notes.txt") \
        == "projects/platform/reference/notes.md"


def test_an_indexable_path_still_writes(cage, agentctl):
    """The guard refuses only what the indexer would ignore: the corrected path
    (a `.md` under projects/) is not collateral damage."""
    body = DOC.format(slug="library-protocol", title="Library Protocol",
                      body="The protocol, now retrievable.")
    rc = agentctl.cmd_kb_apply(_args(
        "projects/platform/reference/library-protocol.md", kw.content_sha(""), content=body))
    assert rc == 0
    assert "The protocol, now retrievable." in _origin_content(
        cage, "projects/platform/reference/library-protocol.md")


# --- the sha precondition ----------------------------------------------------


def test_a_stale_base_sha_is_refused_and_nothing_is_written(cage, agentctl, capsys):
    """The lost-update guard, and the reason the whole design has a sha at all.

    Two writers read the same doc. The first lands. The second must not silently
    overwrite it — git would happily serialize both, producing a clean history
    of a destroyed edit.
    """
    stale = _sha_on_origin(cage, SEEDED)

    # another writer lands first
    _write_doc(cage.seed, SEEDED, slug="seeded", body="Someone else got here first.")
    _git(cage.seed, "add", "-A")
    _git(cage.seed, "commit", "--quiet", "-m", "other writer")
    _git(cage.seed, "push", "--quiet", "origin", "main")

    rc = agentctl.cmd_kb_apply(_args(
        SEEDED, stale, edits='[{"old": "Original body.", "new": "My edit."}]'))
    assert rc == 5
    assert "stale base" in capsys.readouterr().err
    content = _origin_content(cage, SEEDED)
    assert "Someone else got here first." in content
    assert "My edit." not in content


def test_the_stale_refusal_is_journaled(cage, agentctl):
    """A refusal produces no commit, so git cannot record it — that is what the
    journal is for."""
    stale = kw.content_sha("something that was never on main")
    agentctl.cmd_kb_apply(_args(
        SEEDED, stale, edits='[{"old": "Original body.", "new": "x"}]'))
    entries = [j for j in kw.recent_journal() if j["result"] == "stale"]
    assert len(entries) == 1
    assert entries[0]["source_ref"] == SEEDED


# --- policy ------------------------------------------------------------------


@pytest.mark.parametrize("provenance", ["in-session", "verifier-pass", "generated",
                                        "keel", "steward"])
def test_a_roster_path_is_refused_under_every_provenance(cage, agentctl, provenance):
    """The bootstrapping guard, end to end: no provenance may put a roster change
    on main without review, because a bad one breaks dispatch platform-wide."""
    sha = _sha_on_origin(cage, ROSTER)
    rc = agentctl.cmd_kb_apply(_args(
        ROSTER, sha, provenance=provenance,
        edits='[{"old": "Roster doc.", "new": "Changed."}]'))
    assert rc == 4
    assert "Roster doc." in _origin_content(cage, ROSTER)


@pytest.mark.parametrize("ref", [
    "PROJECTS/SHARED/AGENTS/reviewer.md",
    "Projects/Shared/Agents/reviewer.md",
])
def test_a_case_varied_roster_path_cannot_reach_main(cage, agentctl, ref):
    """End-to-end proof of the #243 security finding and its fix.

    On a case-insensitive filesystem the uppercase path opens the same inode, so
    without the fix this wrote the REAL roster file to main under a provenance
    that should never touch it.
    """
    sha = _sha_on_origin(cage, ROSTER)
    rc = agentctl.cmd_kb_apply(_args(
        ref, sha, provenance="in-session",
        edits='[{"old": "Roster doc.", "new": "Owned."}]'))
    assert rc == 4
    assert "Roster doc." in _origin_content(cage, ROSTER)
    assert "Owned." not in _origin_content(cage, ROSTER)


@pytest.mark.parametrize("ref", [
    "projects/agent-platform/../agent-platform/agents/reviewer.md",
    "projects/agent-platform/agents/../agents/reviewer.md",
    "./projects/agent-platform/agents/reviewer.md",
    "projects//agent-platform//agents//reviewer.md",
])
def test_path_traversal_cannot_evade_the_pr_only_guard(cage, agentctl, ref):
    """A `..` segment reaches the real roster file while defeating a prefix match,
    which would make the PR-only guard decorative for any caller controlling the
    argument. Both reviewers found this from different directions on #243.

    PR G's segment guard catches every case here on the string alone — including
    the doubled separator, because `/agents/` is a substring of `//agents//`,
    matching from the second slash. Normalization stays load-bearing for the
    ESCAPE check rather than for the guard: `_kb_resolve_ref` needs the normalized
    form to reject a path that leaves the knowledge root, and keeping the guard
    independent of how it happens to be spelled is what makes the next change to
    it safe (reviewer, #345, correcting the first version of this comment)."""
    sha = _sha_on_origin(cage, ROSTER)
    rc = agentctl.cmd_kb_apply(_args(
        ref, sha, provenance="steward",
        edits='[{"old": "Roster doc.", "new": "Owned."}]'))
    assert rc == 4
    assert "Owned." not in _origin_content(cage, ROSTER)


def test_a_symlink_to_a_roster_path_is_caught_by_the_canonical_check(cage, agentctl):
    """The case only the canonical layer can see.

    Here the requested path is innocuous as a STRING — normalization and case
    folding both wave it through — and it is a symlink whose target is the
    roster. Only resolving it against the filesystem, after the cage worktree
    exists, reveals what git would actually stage. This is the test that makes
    the third layer earn its place rather than merely duplicate the first two.
    """
    link = cage.seed / "projects/platform/reference/innocent.md"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to("../../agent-platform/agents/reviewer.md")
    _git(cage.seed, "add", "-A")
    _git(cage.seed, "commit", "--quiet", "-m", "add symlink")
    _git(cage.seed, "push", "--quiet", "origin", "main")

    rc = agentctl.cmd_kb_apply(_args(
        "projects/platform/reference/innocent.md", _sha_on_origin(cage, ROSTER),
        provenance="in-session",
        edits='[{"old": "Roster doc.", "new": "Owned via symlink."}]'))
    assert rc == 4
    assert "Owned via symlink." not in _origin_content(cage, ROSTER)


def test_a_symlink_to_a_non_indexable_path_is_caught_by_the_canonical_shape_check(
        cage, agentctl, capsys):
    """The indexer-shape rule has the same string-vs-filesystem gap the canonical
    policy check closes: it ran on the caller's `projects/*.md`-looking string,
    and a symlink committed in the checkout can make that path resolve to a real
    file the indexer will not serve. Only the resolved path is authoritative, so
    the shape is re-checked on it (the #243 pattern, applied to this PR's rule)."""
    note = cage.seed / "projects/platform/reference/notes.txt"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("Plain text notes.")
    link = cage.seed / "projects/platform/reference/innocent.md"
    link.symlink_to("notes.txt")
    _git(cage.seed, "add", "-A")
    _git(cage.seed, "commit", "--quiet", "-m", "add a .txt and a symlink to it")
    _git(cage.seed, "push", "--quiet", "origin", "main")

    rc = agentctl.cmd_kb_apply(_args(
        "projects/platform/reference/innocent.md",
        kw.content_sha("Plain text notes."),
        provenance="in-session",
        edits='[{"old": "Plain", "new": "Owned"}]'))
    err = capsys.readouterr().err
    assert rc == 1
    assert "resolves to projects/platform/reference/notes.txt" in err
    assert "indexer will not index" in err
    assert "Plain text notes." in _origin_content(
        cage, "projects/platform/reference/notes.txt")


@pytest.mark.parametrize("ref", ["../outside.md", "/etc/passwd.md", "../../x.md"])
def test_a_path_escaping_the_repo_is_refused(cage, agentctl, ref):
    rc = agentctl.cmd_kb_apply(_args(ref, kw.content_sha(""), content="x"))
    assert rc == 1


def test_a_forged_holder_is_refused_before_any_write(cage, agentctl, capsys):
    sha = _sha_on_origin(cage, SEEDED)
    rc = agentctl.cmd_kb_apply(_args(
        SEEDED, sha,
        holder="run-1\nKB-Write: update projects/agent-platform/agents/reviewer.md in-session run-1",
        edits='[{"old": "Original body.", "new": "x"}]'))
    assert rc == 1
    assert "not a valid identity" in capsys.readouterr().err
    assert "Original body." in _origin_content(cage, SEEDED)


def test_a_message_forging_a_trailer_is_refused(cage, agentctl, capsys):
    sha = _sha_on_origin(cage, SEEDED)
    rc = agentctl.cmd_kb_apply(_args(
        SEEDED, sha,
        message="Innocent.\nKB-Write: update projects/agent-platform/agents/x.md steward run-9",
        edits='[{"old": "Original body.", "new": "x"}]'))
    assert rc == 1
    assert "forge the audit trailer" in capsys.readouterr().err


def test_proposal_provenance_never_reaches_main(cage, agentctl):
    sha = _sha_on_origin(cage, SEEDED)
    rc = agentctl.cmd_kb_apply(_args(
        SEEDED, sha, provenance="proposal",
        edits='[{"old": "Original body.", "new": "x"}]'))
    assert rc == 4
    assert "Original body." in _origin_content(cage, SEEDED)


@pytest.mark.parametrize("provenance", ["verifier-pass", "generated", "keel", "steward"])
def test_provenances_taken_off_the_direct_path_are_refused_on_an_ordinary_doc(
        cage, agentctl, provenance):
    """platform/decisions/0081: only `in-session` writes an ordinary doc directly
    now. The other four recognized values are refused end to end, not just on
    the roster path the guard above covers."""
    sha = _sha_on_origin(cage, SEEDED)
    rc = agentctl.cmd_kb_apply(_args(
        SEEDED, sha, provenance=provenance,
        edits='[{"old": "Original body.", "new": "x"}]'))
    assert rc == 4
    assert "Original body." in _origin_content(cage, SEEDED)


def test_an_unknown_provenance_fails_closed(cage, agentctl):
    sha = _sha_on_origin(cage, SEEDED)
    rc = agentctl.cmd_kb_apply(_args(
        SEEDED, sha, provenance="looks-fine-to-me",
        edits='[{"old": "Original body.", "new": "x"}]'))
    assert rc == 4


@pytest.mark.parametrize("ref", [
    ROSTER,
    "projects/agent-platform/../agent-platform/agents/reviewer.md",
    "PROJECTS/AGENT-PLATFORM/AGENTS/reviewer.md",
])
def test_policy_is_checked_before_any_git_work(cage, agentctl, monkeypatch, ref):
    """A refusal must not depend on the repo being reachable — same answer online
    or off, and no cage worktree built for a write that was never going to land.

    The traversal and case variants ISOLATE the early layer. `_scribe_worktree`
    is patched to throw, so the canonical re-check that normally also catches
    them is unreachable here: if early normalization or case-folding were
    removed, the refusal would not happen, the throw would fire, and this test
    would fail. Without these two parameters, either layer could be deleted with
    every test still green — which is exactly what my first mutation run found,
    and what round-2 review named.
    """
    monkeypatch.setattr(agentctl, "_scribe_worktree",
                        lambda *a: (_ for _ in ()).throw(AssertionError("must not be called")))
    rc = agentctl.cmd_kb_apply(_args(
        ref, "whatever", provenance="steward",
        edits='[{"old": "a", "new": "b"}]'))
    assert rc == 4


# --- the write lock -----------------------------------------------------------


def test_a_held_lock_refuses_naming_kb_not_scribe_apply(cage, agentctl, monkeypatch, capsys):
    """`#578` removed `agentctl scribe apply` and `scribe verify`; the only
    callers of this lock now are `kb apply` and `kb propose`. The refusal text
    used to say `another scribe apply has held ...`, pointing whoever hits a
    wedged lock at a command that no longer exists."""
    monkeypatch.setattr(agentctl, "SCRIBE_LOCK_TIMEOUT", 0.3)
    lock_path = agentctl.SCRIBE_WORKTREE.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    marker = cage.tmp / "holder-ready"
    holder_script = (
        "import fcntl, time\n"
        f"fh = open({str(lock_path)!r}, 'w')\n"
        "fcntl.flock(fh.fileno(), fcntl.LOCK_EX)\n"
        f"open({str(marker)!r}, 'w').close()\n"
        "time.sleep(10)\n"
    )
    holder = subprocess.Popen([sys.executable, "-c", holder_script])
    try:
        deadline = time.monotonic() + 5
        while not marker.exists():
            assert time.monotonic() < deadline, "holder process never took the lock"
            time.sleep(0.05)

        sha = _sha_on_origin(cage, SEEDED)
        rc = agentctl.cmd_kb_apply(_args(
            SEEDED, sha, edits='[{"old": "Original body.", "new": "x"}]'))
    finally:
        holder.terminate()
        holder.wait(timeout=5)

    err = capsys.readouterr().err
    assert rc == 1
    assert "kb apply or kb propose" in err, err
    assert "scribe apply" not in err, err


# --- content guards ----------------------------------------------------------


def test_an_anchor_that_does_not_match_uniquely_is_refused(cage, agentctl, capsys):
    sha = _sha_on_origin(cage, SEEDED)
    rc = agentctl.cmd_kb_apply(_args(
        SEEDED, sha, edits='[{"old": "not present anywhere", "new": "x"}]'))
    assert rc == 1
    assert "matches 0x" in capsys.readouterr().err
    assert "Original body." in _origin_content(cage, SEEDED)


def test_content_will_not_silently_overwrite_an_existing_doc(cage, agentctl):
    sha = _sha_on_origin(cage, SEEDED)
    rc = agentctl.cmd_kb_apply(_args(
        SEEDED, sha, content=DOC.format(slug="seeded", title="T", body="Replaced.")))
    assert rc == 1
    assert "Original body." in _origin_content(cage, SEEDED)


def test_an_identical_write_is_a_no_op_not_an_empty_commit(cage, agentctl):
    sha = _sha_on_origin(cage, SEEDED)
    before = _git(cage.checkout, "rev-parse", "origin/main").stdout.strip()
    rc = agentctl.cmd_kb_apply(_args(
        SEEDED, sha, edits='[{"old": "Original body.", "new": "Original body."}]'))
    assert rc == 0
    _git(cage.checkout, "fetch", "--quiet", "origin", "main")
    assert _git(cage.checkout, "rev-parse", "origin/main").stdout.strip() == before


def test_dry_run_validates_but_writes_nothing(cage, agentctl):
    sha = _sha_on_origin(cage, SEEDED)
    before = _git(cage.checkout, "rev-parse", "origin/main").stdout.strip()
    rc = agentctl.cmd_kb_apply(_args(
        SEEDED, sha, dry_run=True,
        edits='[{"old": "Original body.", "new": "Edited."}]'))
    assert rc == 0
    _git(cage.checkout, "fetch", "--quiet", "origin", "main")
    assert _git(cage.checkout, "rev-parse", "origin/main").stdout.strip() == before
    assert "Original body." in _origin_content(cage, SEEDED)


# --- another holder's live lease refuses the write ----------------------------
#
# operator, 2026-09-11: "The Library shouldn't go around changing docs other crews
# have checked out to work on." This was a warning until then.


def test_a_foreign_lease_refuses_the_write(cage, agentctl, capsys):
    kw.acquire(SEEDED, "someone-else")
    sha = _sha_on_origin(cage, SEEDED)
    rc = agentctl.cmd_kb_apply(_args(
        SEEDED, sha, edits='[{"old": "Original body.", "new": "Edited."}]'))
    assert rc == 3
    assert "leased by someone-else" in capsys.readouterr().err
    assert "Original body." in _origin_content(cage, SEEDED)


def test_the_foreign_lease_refusal_is_journaled(cage, agentctl):
    kw.acquire(SEEDED, "someone-else")
    agentctl.cmd_kb_apply(_args(
        SEEDED, _sha_on_origin(cage, SEEDED),
        edits='[{"old": "Original body.", "new": "Edited."}]'))
    conn = kw._connect()
    try:
        row = conn.execute("SELECT result, detail FROM journal WHERE verb = 'apply' "
                           "ORDER BY rowid DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    assert tuple(row) == ("leased", "someone-else")


def test_the_holders_own_lease_does_not_refuse(cage, agentctl):
    kw.acquire(SEEDED, "run-test")
    rc = agentctl.cmd_kb_apply(_args(
        SEEDED, _sha_on_origin(cage, SEEDED),
        edits='[{"old": "Original body.", "new": "Edited."}]'))
    assert rc == 0
    assert "Edited." in _origin_content(cage, SEEDED)


def test_an_expired_foreign_lease_does_not_refuse(cage, agentctl):
    """A dead session holds a doc for its lease's TTL and no longer."""
    from datetime import datetime, timedelta, timezone
    kw.acquire(SEEDED, "someone-else",
               now=datetime.now(timezone.utc) - timedelta(days=1))
    rc = agentctl.cmd_kb_apply(_args(
        SEEDED, _sha_on_origin(cage, SEEDED),
        edits='[{"old": "Original body.", "new": "Edited."}]'))
    assert rc == 0


# --- every write is made as convivy-library, and refuses without that identity --------


def test_the_direct_write_pushes_main_as_the_app_to_the_https_url(cage, agentctl, library_identity):
    """The push names the https URL, so an App token can authenticate it. It never
    names `origin`, whose ssh remote is the operator's personal key."""
    import kb_merge as KM
    sha = _sha_on_origin(cage, SEEDED)
    assert agentctl.cmd_kb_apply(_args(
        SEEDED, sha, edits='[{"old": "Original body.", "new": "Edited body."}]')) == 0
    pushes = [c for c in library_identity.calls if c[0] == "push"]
    assert pushes == [("push", "--quiet", KM.push_url(), "HEAD:main")]


def test_the_apply_asks_for_a_push_token_and_no_pr_token(cage, agentctl, library_identity):
    """A direct write opens no PR, so it has no use for `pull_requests`."""
    sha = _sha_on_origin(cage, SEEDED)
    assert agentctl.cmd_kb_apply(_args(
        SEEDED, sha, edits='[{"old": "Original body.", "new": "Edited body."}]')) == 0
    assert library_identity.mints == ["push"]


class _RecordingIndexer:
    """The real indexer's predicates, with the two calls that write the live index
    recorded instead of made."""

    def __init__(self, real, deferred=(), refused=()):
        self._real, self.indexed = real, []
        self.deferred, self.refused = set(deferred), set(refused)

    def __getattr__(self, name):
        return getattr(self._real, name)

    def ensure_db(self):
        pass

    def index_paths(self, paths):
        self.indexed.append(list(paths))
        return {"deferred": [(p, "roster unreadable") for p in paths if p in self.deferred],
                "refused": [(p, "invalid status: bogus") for p in paths if p in self.refused]}


def test_both_fetches_of_a_direct_write_run_as_the_app_over_the_https_url(
        cage, agentctl, library_identity, monkeypatch):
    """The fetch that opens the cage and the one after the push both name the https
    URL and the refspec that updates `origin/main`; neither names `origin`, whose
    ssh remote is the operator's personal key (the fixture fails a call that does). The
    second one is what lets the shared checkout fast-forward and the index catch up."""
    import kb_merge as KM
    idx = _RecordingIndexer(agentctl._kb_indexer())
    monkeypatch.setattr(agentctl, "_kb_indexer", lambda: idx)
    sha = _sha_on_origin(cage, SEEDED)
    assert agentctl.cmd_kb_apply(_args(
        SEEDED, sha, no_sync=False,
        edits='[{"old": "Original body.", "new": "Edited body."}]')) == 0
    fetch = ("fetch", "--quiet", KM.push_url(), KM.MAIN_REFSPEC)
    assert [c for c in library_identity.calls if c[0] == "fetch"] == [fetch, fetch]
    assert library_identity.calls[0] == fetch, "the fetch is the first call made as the App"
    assert idx.indexed == [[SEEDED]]
    assert "Edited body." in (cage.checkout / SEEDED).read_text(), \
        "the shared checkout did not fast-forward to the pushed commit"


def test_a_pushed_write_the_index_defers_is_named_as_not_indexed(
        cage, agentctl, monkeypatch, capsys):
    """The index reports the write deferred (the indexer test covers how). The push
    stands, so the exit is 0, but the write is not in the index until a later pass
    reads the roster. That is named, not left for a caller to infer."""
    idx = _RecordingIndexer(agentctl._kb_indexer(), deferred=[SEEDED])
    monkeypatch.setattr(agentctl, "_kb_indexer", lambda: idx)
    sha = _sha_on_origin(cage, SEEDED)
    assert agentctl.cmd_kb_apply(_args(
        SEEDED, sha, no_sync=False,
        edits='[{"old": "Original body.", "new": "Edited body."}]')) == 0
    assert "Edited body." in _origin_content(cage, SEEDED)
    err = capsys.readouterr().err
    assert f"kb apply: {SEEDED} is pushed but not indexed" in err, err


def test_a_pushed_write_the_index_refuses_is_named_refused_not_deferred(
        cage, agentctl, monkeypatch, capsys):
    """A refusal the index holds on its own grounds is named REFUSED, and no retry is promised
    for it. The push still stands, so exit 0. This cage never indexed the document, so there
    is no row to keep and the line says nothing about one."""
    idx = _RecordingIndexer(agentctl._kb_indexer(), refused=[SEEDED])
    monkeypatch.setattr(agentctl, "_kb_indexer", lambda: idx)
    sha = _sha_on_origin(cage, SEEDED)
    assert agentctl.cmd_kb_apply(_args(
        SEEDED, sha, no_sync=False,
        edits='[{"old": "Original body.", "new": "Edited body."}]')) == 0
    assert "Edited body." in _origin_content(cage, SEEDED)
    err = capsys.readouterr().err
    assert (f"kb apply: REFUSED {SEEDED}: invalid status: bogus. The write is "
            f"pushed but not indexed.") in err, err
    assert f"{SEEDED} is pushed but not indexed" not in err, err


def test_the_direct_write_is_authored_and_committed_as_the_app_and_by_no_person(
        cage, agentctl, library_identity, monkeypatch):
    """Author AND committer. The cage's repo config names a person and so does the
    environment; neither may reach the commit, and `~/.gitconfig` is never edited."""
    import kb_merge as KM
    monkeypatch.setenv("GIT_AUTHOR_NAME", "The Operator")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "operator@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "The Operator")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "operator@example.com")
    sha = _sha_on_origin(cage, SEEDED)
    assert agentctl.cmd_kb_apply(_args(
        SEEDED, sha, edits='[{"old": "Original body.", "new": "Edited body."}]')) == 0
    bot = f"{KM.commit_name()} <{KM.commit_email()}>"
    got = _git(cage.origin, "log", "-1", "--format=%an <%ae>%n%cn <%ce>%n%b", "main").stdout
    assert got.splitlines()[:2] == [bot, bot], got
    assert "The Operator" not in got.splitlines()[0] + got.splitlines()[1]
    # The requesting session survives as the trailer's holder.
    assert f"KB-Write: update {SEEDED} in-session run-test" in got


def test_without_the_app_identity_the_write_is_refused_and_nothing_is_left_behind(
        cage, agentctl, library_identity, monkeypatch, capsys):
    """The suite holds no App key (conftest), so the real identity refuses. The
    write must NOT fall back to the ssh remote, which would push as the operator.

    The identity is now obtained before the lock and the fetch, so a refusal
    leaves nothing at all: `_scribe_worktree`, which takes the lock, fetches and
    builds the cage, is never reached."""
    monkeypatch.setattr(agentctl, "_kb_library", library_identity.real)
    monkeypatch.setattr(agentctl, "_scribe_worktree",
                        lambda *a: (_ for _ in ()).throw(AssertionError("lock and fetch reached")))
    main_before = _git(cage.origin, "rev-parse", "main").stdout.strip()
    checkout_before = _git(cage.checkout, "rev-parse", "origin/main").stdout.strip()
    sha = _sha_on_origin(cage, SEEDED)
    rc = agentctl.cmd_kb_apply(_args(
        SEEDED, sha, edits='[{"old": "Original body.", "new": "Edited body."}]'))
    err = capsys.readouterr().err
    assert rc == 2
    assert "cannot obtain the convivy-library App identity" in err
    assert "App key:" in err, "the refusal must say which stage failed"
    assert "does not fall back to a personal credential" in err
    assert _git(cage.origin, "rev-parse", "main").stdout.strip() == main_before
    assert _git(cage.checkout, "rev-parse", "origin/main").stdout.strip() == checkout_before
    assert library_identity.calls == []
    assert not agentctl.SCRIBE_WORKTREE.exists(), "a refusal built the cage"


def test_the_missing_identity_refusal_is_journaled(cage, agentctl, library_identity, monkeypatch):
    monkeypatch.setattr(agentctl, "_kb_library", library_identity.real)
    sha = _sha_on_origin(cage, SEEDED)
    agentctl.cmd_kb_apply(_args(
        SEEDED, sha, edits='[{"old": "Original body.", "new": "Edited body."}]'))
    entries = [j for j in kw.recent_journal() if j["result"] == "no-identity"]
    assert len(entries) == 1 and "App key" in entries[0]["detail"]


@pytest.mark.parametrize("flag", ["no_push", "dry_run"])
def test_a_write_that_does_not_push_still_needs_the_identity_for_its_fetch(
        cage, agentctl, library_identity, monkeypatch, flag):
    """`--no-push` and `--dry-run` never push, but the fetch that opens the cage is
    made as the App now, so they refuse without it, before the lock is taken. They
    used to need none because they never called out as the App; they did fetch over
    ssh as the operator, which is the dependency this removes."""
    monkeypatch.setattr(agentctl, "_kb_library", library_identity.real)
    monkeypatch.setattr(agentctl, "_scribe_worktree",
                        lambda *a: (_ for _ in ()).throw(AssertionError("lock and fetch reached")))
    sha = _sha_on_origin(cage, SEEDED)
    assert agentctl.cmd_kb_apply(_args(
        SEEDED, sha, edits='[{"old": "Original body.", "new": "Edited body."}]',
        **{flag: True})) == 2


# --- provenance `person`: the cage, and what it refuses ----------------------
#
# A person's write needs a Cloudflare Access assertion on stdin that verifies.
# Real RS256 tokens, signed the way tests/test_access_assertion.py signs them; no
# network (the JWKS fetch is replaced) and config and cache live under tmp_path.

import io  # noqa: E402
import urllib.request  # noqa: E402

import kb_audit  # noqa: E402
from test_access_assertion import AUD, KID, TEAM, FakeResp, _openssl, b64u, make_token  # noqa: E402

PERSON = "operator@example.com"
EDIT = '[{"old": "Original body.", "new": "Edited by a person."}]'


@pytest.fixture(scope="module")
def person_key(tmp_path_factory):
    pem = tmp_path_factory.mktemp("person-rsa") / "key.pem"
    _openssl("genrsa", "-out", str(pem), "2048")
    mod = _openssl("rsa", "-in", str(pem), "-noout", "-modulus").decode().strip()
    return pem, int(mod.split("=", 1)[1], 16)


@pytest.fixture
def person(cage, monkeypatch, person_key):
    pem, n = person_key
    cfg = cage.tmp / "kb-person.json"
    cfg.write_text(json.dumps({"team_domain": TEAM, "aud": AUD, "persons": [PERSON]}))
    monkeypatch.setenv("KB_PERSON_CONFIG", str(cfg))
    monkeypatch.setenv("KB_PERSON_JWKS_CACHE", str(cage.tmp / "jwks-cache" / "access-jwks.json"))
    jwks = json.dumps({"keys": [{"kid": KID, "kty": "RSA", "n": b64u(n.to_bytes(256, "big")),
                                 "e": b64u((65537).to_bytes(3, "big"))}]}).encode()
    state = {"fail": False}

    def fake_urlopen(url, *a, **k):
        if state["fail"]:
            raise OSError("unreachable")
        return FakeResp(jwks)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.delenv("AGENTCTL_RUN_ID", raising=False)

    def present(token):
        monkeypatch.setattr(sys, "stdin", io.StringIO(token))

    return Namespace(pem=pem, state=state, present=present,
                     token=lambda **kw_: make_token(pem, **kw_))


def _person_args(sha, ref=SEEDED, **kw_):
    kw_.setdefault("edits", EDIT)
    return _args(ref, sha, provenance="person", **kw_)


def _history(cage):
    """Every commit reachable from origin, and from the cage worktree if one was
    made. An exit code cannot tell 'refused' from 'refused, then wrote something
    under another provenance'; the log can."""
    out = {"origin": _git(cage.origin, "log", "--all", "--format=%H").stdout.split()}
    wt = cage.tmp / "cage-wt"
    if (wt / ".git").exists():
        out["cage"] = _git(wt, "log", "--all", "--format=%H").stdout.split()
    return out


def test_a_person_write_with_a_valid_assertion_records_the_verified_email(cage, agentctl, person):
    person.present(person.token())
    rc = agentctl.cmd_kb_apply(_person_args(_sha_on_origin(cage, SEEDED)))
    assert rc == 0
    assert "Edited by a person." in _origin_content(cage, SEEDED)
    _git(cage.checkout, "fetch", "--quiet", "origin", "main")
    msg = _git(cage.checkout, "log", "-1", "--format=%B", "origin/main").stdout
    # The token's email is `operator@Example.com`: what is recorded is lowercased.
    assert f"KB-Write: update {SEEDED} person run-test" in msg
    assert f"KB-Person: {PERSON}" in msg
    # The audit's own parsers read the commit the cage wrote.
    info = kb_audit.classify("docs: update", msg, is_merge=False)
    assert (info["path"], info["provenance"], info["person"]) == ("cage", "person", PERSON)
    assert kb_audit.PERSON_RE.match(kb_audit.PERSON_LINE_RE.findall(msg)[0])


def test_a_person_write_carries_no_coauthor_line_even_from_a_run(cage, agentctl, person,
                                                                   monkeypatch):
    monkeypatch.setenv("AGENTCTL_RUN_ID", "run-abcd1234")
    person.present(person.token())
    assert agentctl.cmd_kb_apply(_person_args(_sha_on_origin(cage, SEEDED))) == 0
    _git(cage.checkout, "fetch", "--quiet", "origin", "main")
    msg = _git(cage.checkout, "log", "-1", "--format=%B", "origin/main").stdout
    assert "Co-Authored-By" not in msg


def _refused(cage, agentctl, capsys, rc_expected=8, **kw_):
    before = _history(cage)
    rc = agentctl.cmd_kb_apply(_person_args(_sha_on_origin(cage, SEEDED), **kw_))
    assert rc == rc_expected
    assert _history(cage) == before, "a refused person write left a commit behind"
    assert "Original body." in _origin_content(cage, SEEDED)
    return capsys.readouterr().err


def _rows():
    import sqlite3
    c = sqlite3.connect(kw.DB)
    try:
        return c.execute("SELECT provenance, result, detail FROM journal").fetchall()
    finally:
        c.close()


def _author_rows():
    import sqlite3
    c = sqlite3.connect(kw.DB)
    try:
        return c.execute(
            "SELECT provenance, result, author_kind, person FROM journal").fetchall()
    finally:
        c.close()


def test_no_assertion_on_stdin_exits_8(cage, agentctl, person, capsys):
    person.present("")
    err = _refused(cage, agentctl, capsys)
    assert "no-assertion" in err
    assert ("person", "refused-assertion", "no-assertion") in _rows()


def test_an_alg_with_control_text_reaches_neither_stderr_nor_the_journal(
        cage, agentctl, person, capsys):
    evil = "RS256\n\x1b[31mINJECTED\x1b[0m"
    h, p, s = person.token().split(".")
    hdr = b64u(json.dumps({"alg": evil, "kid": KID}).encode())
    person.present(f"{hdr}.{p}.{s}")
    err = _refused(cage, agentctl, capsys)
    assert "(bad-alg)" in err
    assert "INJECTED" not in err and "\x1b" not in err
    rows = _rows()
    assert ("person", "refused-assertion", "bad-alg") in rows
    assert not [r for r in rows if "INJECTED" in str(r) or "\x1b" in str(r)]


def test_an_expired_assertion_exits_8_and_writes_nothing(cage, agentctl, person, capsys):
    person.present(person.token(claims={"exp": int(time.time()) - 3600}))
    assert "expired" in _refused(cage, agentctl, capsys)


def test_a_forged_signature_exits_8(cage, agentctl, person, capsys):
    tok = person.token()
    h, p, s = tok.split(".")
    person.present(f"{h}.{p}.{s[:-4]}AAAA")
    assert "bad-signature" in _refused(cage, agentctl, capsys)


def test_an_email_outside_the_persons_allowlist_exits_8(cage, agentctl, person, capsys):
    person.present(person.token(claims={"email": "eve@example.com"}))
    err = _refused(cage, agentctl, capsys)
    assert "not-in-persons" in err
    assert "eve@example.com" not in err   # the claim is never echoed


@pytest.mark.parametrize("email", ["operator@example.com extra", "j" * 250 + "@example.com", 7])
def test_a_whitespace_or_overlong_or_non_string_email_exits_8(cage, agentctl, person, capsys,
                                                                 email):
    person.present(person.token(claims={"email": email}))
    err = _refused(cage, agentctl, capsys)
    assert any(r in err for r in ("bad-email", "no-email"))


def test_no_fallback_when_the_jwks_cannot_be_fetched_and_nothing_is_cached(
        cage, agentctl, person, capsys):
    """The no-fallback test. A failed verification must not quietly become an
    `in-session` write: exit 8, the journal records a refusal and no write, and
    neither origin nor the cage worktree gained a commit."""
    person.state["fail"] = True
    person.present(person.token())
    err = _refused(cage, agentctl, capsys)
    assert "jwks-unavailable" in err
    rows = _rows()
    assert ("person", "refused-assertion", "jwks-unavailable") in rows
    assert not [r for r in rows if r[1] == "ok"]
    assert not [r for r in rows if r[0] == "in-session"]


def test_a_person_refusal_writes_nothing_under_any_other_provenance(
        cage, agentctl, person, capsys):
    """Run the refusal, then read the history: `in-session` and every other
    provenance must be absent from it, so 'refused' cannot hide 'refused and
    wrote something else'."""
    person.present("")
    _refused(cage, agentctl, capsys)
    logs = _git(cage.origin, "log", "--all", "--format=%B").stdout
    assert "KB-Write:" not in logs


def test_person_on_a_roster_path_is_refused_by_policy_before_stdin_is_read(
        cage, agentctl, person, monkeypatch, capsys):
    class Tripwire:
        def isatty(self):
            return False

        def read(self, *a):
            raise AssertionError("stdin was read for a write policy refuses")

    monkeypatch.setattr(sys, "stdin", Tripwire())
    rc = agentctl.cmd_kb_apply(_person_args(_sha_on_origin(cage, ROSTER), ref=ROSTER))
    assert rc == 4
    assert "roster path" in capsys.readouterr().err


def test_a_message_planting_a_person_line_is_refused_at_exit_1(cage, agentctl, person, capsys):
    person.present(person.token())
    rc = agentctl.cmd_kb_apply(_person_args(
        _sha_on_origin(cage, SEEDED), message="Fine.\nKB-Person: eve@example.com"))
    assert rc == 1
    assert "KB-Person" in capsys.readouterr().err
    assert "Original body." in _origin_content(cage, SEEDED)


def test_in_session_reads_nothing_from_stdin_and_records_no_person(cage, agentctl, monkeypatch):
    class Tripwire:
        def isatty(self):
            raise AssertionError("stdin was consulted for an in-session write")

        def read(self, *a):
            raise AssertionError("stdin was read for an in-session write")

    monkeypatch.setattr(sys, "stdin", Tripwire())
    sha = _sha_on_origin(cage, SEEDED)
    rc = agentctl.cmd_kb_apply(_args(SEEDED, sha, edits=EDIT))
    assert rc == 0
    _git(cage.checkout, "fetch", "--quiet", "origin", "main")
    msg = _git(cage.checkout, "log", "-1", "--format=%B", "origin/main").stdout
    assert "KB-Write: update" in msg and "KB-Person" not in msg


# --- provenance `person`: the journal's typed author_kind ---------------------


def test_a_verified_person_write_is_journaled_with_the_typed_kind(cage, agentctl, person):
    person.present(person.token())
    rc = agentctl.cmd_kb_apply(_person_args(_sha_on_origin(cage, SEEDED)))
    assert rc == 0
    assert ("person", "ok", "person", PERSON) in _author_rows()


def test_an_unverified_person_attempt_is_journaled_as_agent_not_person(
        cage, agentctl, person, capsys):
    """A requested-but-unverified `person` write must not be recorded as one:
    the typed column is set only once the Access assertion actually verifies,
    never from the caller's `--provenance person` string alone."""
    person.present("")
    _refused(cage, agentctl, capsys)
    assert ("person", "refused-assertion", "agent", None) in _author_rows()


def test_an_in_session_write_is_journaled_as_agent(cage, agentctl):
    sha = _sha_on_origin(cage, SEEDED)
    rc = agentctl.cmd_kb_apply(_args(SEEDED, sha, edits=EDIT))
    assert rc == 0
    assert ("in-session", "ok", "agent", None) in _author_rows()


# --- provenance `person`: a Deck signature, the second proof on stdin --------
#
# Deck's editor signs a save with a Touch-ID-gated Secure Enclave key; the
# Library's root certifies the certificate and its registry answers whether it
# is active (library-protocol.md §2.2). No network: `deck_signature`'s registry
# call and root-CA file are both stubbed per test. `tests/test_deck_signature.py`
# owns the exhaustive certificate/window edge cases; these drive the same
# checks through the real `cmd_kb_apply`.

import datetime as _dt  # noqa: E402

import deck_signature as ds  # noqa: E402
from test_deck_signature import (  # noqa: E402
    make_root, make_signing_cert, make_machine_cert, pem, sign, signed_at_str, stdin_object,
)

DECK_PERSON = "avery@example.com"


class _FakeRegistryResp:
    def __init__(self, active: bool):
        self.status = 200
        self._body = json.dumps(
            {"fingerprint_sha256": "x", "status": "active" if active else "revoked",
             "active": active}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _deck_content(body: str) -> str:
    """A whole valid document. A Deck signature carries the whole file, never
    an anchor edit (--edits is refused), so every test body needs the real
    frontmatter the indexer requires — a bare string would refuse at exit 6
    (`no frontmatter delimiter`) before ever reaching a Deck check."""
    return DOC.format(slug="seeded", title="A doc", body=body)


@pytest.fixture
def deck(cage, monkeypatch):
    root_key, root_cert = make_root()
    ca_path = cage.tmp / "library-ca.crt"
    ca_path.write_text(pem(root_cert))
    monkeypatch.setattr(ds, "ROOT_CA_PATH", ca_path)
    key, cert = make_signing_cert(root_key, root_cert, email=DECK_PERSON)
    monkeypatch.setattr(ds.signal_client, "token", lambda: "test-agent-token")
    monkeypatch.delenv("AGENTCTL_RUN_ID", raising=False)
    state = {"active": True, "raise": None}

    def fake_urlopen(req, timeout=None):
        if state["raise"] is not None:
            raise state["raise"]
        return _FakeRegistryResp(state["active"])

    monkeypatch.setattr(ds.urllib.request, "urlopen", fake_urlopen)

    def present(document, base_sha, body, *, signing_key=None, signing_cert=None,
                signed_at=None):
        signing_key = signing_key or key
        signing_cert = signing_cert or cert
        sat = signed_at or signed_at_str(_dt.datetime.now(_dt.timezone.utc))
        # `_deck_content` already ends in "\n" (the DOC template does), so this
        # is exactly the byte string kb apply will hash and write — sign what
        # will actually land, exactly as kb apply itself always does.
        envelope = ds.build_envelope(document, base_sha, kw.content_sha(_deck_content(body)), sat)
        raw = stdin_object(signing_cert, signed_at=sat, signature=sign(signing_key, envelope))
        monkeypatch.setattr(sys, "stdin", io.StringIO(raw))

    def present_raw(raw: str):
        monkeypatch.setattr(sys, "stdin", io.StringIO(raw))

    return Namespace(root_key=root_key, root_cert=root_cert, key=key, cert=cert,
                     state=state, present=present, present_raw=present_raw)


def _assert_no_stray_cage_commit(cage):
    """A Deck signature's phase 2 needs the cage worktree open before it can
    even check the signature, so — unlike an Access refusal, which never opens
    it — the worktree may exist after a refused write. What must not exist is
    a commit IN it that `origin` does not already have; `_history`'s dict
    equality would flag the worktree merely coming into existence as if it
    were one."""
    hist = _history(cage)
    stray = set(hist.get("cage", [])) - set(hist["origin"])
    assert not stray, f"a refused Deck-signed write left a commit behind: {stray}"


def _deck_args(doc, base_sha, body, **kw_):
    kw_.setdefault("content", _deck_content(body))
    kw_.setdefault("allow_overwrite", True)
    return _args(doc, base_sha, provenance="person", **kw_)


def test_a_deck_signed_write_records_the_certificates_person(cage, agentctl, deck):
    sha = _sha_on_origin(cage, SEEDED)
    deck.present(SEEDED, sha, "Signed by Deck.")
    rc = agentctl.cmd_kb_apply(_deck_args(SEEDED, sha, "Signed by Deck."))
    assert rc == 0
    assert "Signed by Deck." in _origin_content(cage, SEEDED)
    _git(cage.checkout, "fetch", "--quiet", "origin", "main")
    msg = _git(cage.checkout, "log", "-1", "--format=%B", "origin/main").stdout
    assert f"KB-Write: update {SEEDED} person run-test" in msg
    assert f"KB-Person: {DECK_PERSON}" in msg
    assert ("person", "ok", "person", DECK_PERSON) in _author_rows()


def test_a_deck_signature_with_edits_is_refused(cage, agentctl, deck, capsys):
    sha = _sha_on_origin(cage, SEEDED)
    deck.present(SEEDED, sha, "Signed by Deck.")
    before = _history(cage)
    rc = agentctl.cmd_kb_apply(_args(SEEDED, sha, provenance="person", edits=EDIT))
    assert rc == 8
    assert "edits-not-allowed" in capsys.readouterr().err
    assert _history(cage) == before


def test_a_forged_deck_signature_is_refused(cage, agentctl, deck, capsys):
    sha = _sha_on_origin(cage, SEEDED)
    deck.present(SEEDED, sha, "Signed by Deck.")
    tampered = json.loads(sys.stdin.read())
    sig = tampered["signature"]
    tampered["signature"] = sig[:-4] + ("AAAA" if sig[-4:] != "AAAA" else "BBBB")
    deck.present_raw(json.dumps(tampered))
    rc = agentctl.cmd_kb_apply(_deck_args(SEEDED, sha, "Signed by Deck."))
    assert rc == 8
    assert "bad-signature" in capsys.readouterr().err
    _assert_no_stray_cage_commit(cage)
    assert "Original body." in _origin_content(cage, SEEDED)


def test_a_deck_signature_for_a_different_document_is_refused(cage, agentctl, deck, capsys):
    sha = _sha_on_origin(cage, SEEDED)
    other_sha = _sha_on_origin(cage, ROSTER)
    deck.present(ROSTER, other_sha, "Signed by Deck.")  # signed for a DIFFERENT doc
    rc = agentctl.cmd_kb_apply(_deck_args(SEEDED, sha, "Signed by Deck."))
    assert rc == 8
    assert "bad-signature" in capsys.readouterr().err
    _assert_no_stray_cage_commit(cage)


def test_a_deck_signature_over_a_stale_base_is_refused_at_the_generic_precondition(
        cage, agentctl, deck, capsys):
    """The base-sha precondition is generic — checked well before any Deck
    check runs — so a signature cannot make a stale write land."""
    stale = _sha_on_origin(cage, SEEDED)
    deck.present(SEEDED, stale, "Signed by Deck.")

    _write_doc(cage.seed, SEEDED, slug="seeded", body="Someone else got here first.")
    _git(cage.seed, "add", "-A")
    _git(cage.seed, "commit", "--quiet", "-m", "other writer")
    _git(cage.seed, "push", "--quiet", "origin", "main")

    rc = agentctl.cmd_kb_apply(_deck_args(SEEDED, stale, "Signed by Deck."))
    assert rc == 5
    assert "stale base" in capsys.readouterr().err
    content = _origin_content(cage, SEEDED)
    assert "Someone else got here first." in content
    assert "Signed by Deck." not in content


def test_a_deck_signature_over_bytes_that_differ_from_the_hash_is_refused(
        cage, agentctl, deck, capsys):
    sha = _sha_on_origin(cage, SEEDED)
    deck.present(SEEDED, sha, "What was signed.")  # signs THIS content's hash
    rc = agentctl.cmd_kb_apply(_deck_args(SEEDED, sha, "Different bytes entirely."))
    assert rc == 8
    assert "bad-signature" in capsys.readouterr().err
    _assert_no_stray_cage_commit(cage)


def test_a_signing_certificate_chained_to_another_root_is_refused(cage, agentctl, deck, capsys):
    other_key, other_cert = make_root()
    key, cert = make_signing_cert(other_key, other_cert, email=DECK_PERSON)
    sha = _sha_on_origin(cage, SEEDED)
    deck.present(SEEDED, sha, "Signed by Deck.", signing_key=key, signing_cert=cert)
    before = _history(cage)
    rc = agentctl.cmd_kb_apply(_deck_args(SEEDED, sha, "Signed by Deck."))
    assert rc == 8
    assert "chain-untrusted" in capsys.readouterr().err
    assert _history(cage) == before


def test_a_machine_certificate_presented_as_signing_is_refused(cage, agentctl, deck, capsys):
    key, cert = make_machine_cert(deck.root_key, deck.root_cert)
    sha = _sha_on_origin(cage, SEEDED)
    deck.present(SEEDED, sha, "Signed by Deck.", signing_key=key, signing_cert=cert)
    before = _history(cage)
    rc = agentctl.cmd_kb_apply(_deck_args(SEEDED, sha, "Signed by Deck."))
    assert rc == 8
    assert "wrong-purpose" in capsys.readouterr().err
    assert _history(cage) == before


def test_a_revoked_certificate_is_refused(cage, agentctl, deck, capsys):
    deck.state["active"] = False
    sha = _sha_on_origin(cage, SEEDED)
    deck.present(SEEDED, sha, "Signed by Deck.")
    rc = agentctl.cmd_kb_apply(_deck_args(SEEDED, sha, "Signed by Deck."))
    assert rc == 8
    assert "not-active" in capsys.readouterr().err
    _assert_no_stray_cage_commit(cage)


def test_an_unreachable_library_is_refused(cage, agentctl, deck, capsys):
    deck.state["raise"] = OSError("connection refused")
    sha = _sha_on_origin(cage, SEEDED)
    deck.present(SEEDED, sha, "Signed by Deck.")
    rc = agentctl.cmd_kb_apply(_deck_args(SEEDED, sha, "Signed by Deck."))
    assert rc == 8
    assert "library-unreachable" in capsys.readouterr().err
    _assert_no_stray_cage_commit(cage)


def test_a_signed_at_outside_the_window_is_refused(cage, agentctl, deck, capsys):
    sha = _sha_on_origin(cage, SEEDED)
    too_old = signed_at_str(_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=301))
    deck.present(SEEDED, sha, "Signed by Deck.", signed_at=too_old)
    before = _history(cage)
    rc = agentctl.cmd_kb_apply(_deck_args(SEEDED, sha, "Signed by Deck."))
    assert rc == 8
    assert "signed-at-window" in capsys.readouterr().err
    assert _history(cage) == before


def test_a_path_that_resolves_off_its_canonical_form_is_refused(cage, agentctl, deck, capsys):
    """A signed save names its file by its canonical path (library-protocol.md
    §2.2): a symlink committed in the checkout must not carry it to another
    file — this is the property the Access path has no envelope field to
    check at all."""
    linked = "projects/platform/reference/linked.md"
    (cage.seed / linked).symlink_to("seeded.md")
    _git(cage.seed, "add", "-A")
    _git(cage.seed, "commit", "--quiet", "-m", "add a symlink")
    _git(cage.seed, "push", "--quiet", "origin", "main")

    sha = _sha_on_origin(cage, SEEDED)
    deck.present(linked, sha, "Signed by Deck.")
    rc = agentctl.cmd_kb_apply(_deck_args(linked, sha, "Signed by Deck."))
    assert rc == 8
    err = capsys.readouterr().err
    assert linked in err and SEEDED in err and "symlink" in err
    assert ("person", "refused-assertion", f"path-not-canonical; requested as {linked}") in _rows()
    _assert_no_stray_cage_commit(cage)
    assert "Original body." in _origin_content(cage, SEEDED)


def test_an_unverified_deck_attempt_is_journaled_as_agent_not_person(
        cage, agentctl, deck, capsys):
    deck.state["active"] = False
    sha = _sha_on_origin(cage, SEEDED)
    deck.present(SEEDED, sha, "Signed by Deck.")
    agentctl.cmd_kb_apply(_deck_args(SEEDED, sha, "Signed by Deck."))
    assert ("person", "refused-assertion", "agent", None) in _author_rows()


def test_kb_apply_bootstraps_before_reading_stdin_for_person_provenance(
        cage, agentctl, deck, monkeypatch):
    """Round-1 (PR 695) called `venv_bootstrap.ensure` only from inside
    `_kb_person_from_stdin`, AFTER it had already read stdin — so the re-exec
    that call could trigger (`os.execv`, which keeps the same stdin file
    descriptor at whatever position the read left it) would restart the
    process onto an already-drained pipe: the Deck signature or Access token
    was gone, and `kb apply --provenance person` refused as `no-assertion`
    having verified nothing. `cmd_kb_apply` now calls `ensure` itself, as its
    first action, before anything — including stdin — is touched.

    Proved here by recording the ORDER `venv_bootstrap.ensure` and
    `sys.stdin.read` are actually called in, through the real `cmd_kb_apply`
    and a real Deck signature (no forced re-exec needed: the ordering bug is
    in what `cmd_kb_apply` calls first, not in whether a venv swap is needed
    on this machine) — and by checking the write itself lands with the right
    person recorded, the substance a dropped signature would have refused,
    not only the exit code."""
    order = []
    monkeypatch.setattr(agentctl.venv_bootstrap, "ensure",
                        lambda *a, **k: order.append("ensure"))

    sha = _sha_on_origin(cage, SEEDED)
    deck.present(SEEDED, sha, "Signed by Deck.")
    stdin, real_read = sys.stdin, sys.stdin.read

    def recording_read(*a, **k):
        order.append("stdin-read")
        return real_read(*a, **k)

    monkeypatch.setattr(stdin, "read", recording_read, raising=False)

    rc = agentctl.cmd_kb_apply(_deck_args(SEEDED, sha, "Signed by Deck."))

    assert order and order[0] == "ensure", (
        f"venv_bootstrap.ensure must run before anything reads stdin; got {order}")
    assert "stdin-read" in order
    assert rc == 0
    assert "Signed by Deck." in _origin_content(cage, SEEDED)
    assert ("person", "ok", "person", DECK_PERSON) in _author_rows()
