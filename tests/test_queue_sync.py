"""queue-sync's classification rules.

The three that matter are the ones a wrong version gets wrong QUIETLY: an in-flight
item deleted, an unverifiable item deleted, and a real prose tail truncated. Each is
pinned here against the actual queue dialects rather than a tidy fixture, because
every bug this tool has had came from a shape I did not think to write down.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "bin" / "queue-sync"


@pytest.fixture(scope="module")
def qs():
    spec = importlib.util.spec_from_loader(
        "queue_sync", loader=importlib.machinery.SourceFileLoader("queue_sync", str(SRC)))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- the tail dialects -------------------------------------------------------

def test_strips_the_constance_landed_tail(qs):
    b = ("- **2026-07-13 — A1: crash guard** — architect proposal A1. Landed as "
         "https://github.com/convivy/constance/pull/499. reviewer APPROVE; CI green.")
    assert qs.described(b) == "- **2026-07-13 — A1: crash guard** — architect proposal A1"


def test_strips_the_vivy_arrow_tail(qs):
    """vivy never writes "Landed as" — 45 entries to nil. Missing this dialect left
    every rebuilt vivy entry carrying its old PR reference and an appended new one."""
    b = ("- **2026-07-21** — **A2: pii_fields expansion** *(pii-fields)*: 13 keys added "
         "→ https://github.com/convivy/vivy/pull/951")
    got = qs.described(b)
    assert "pull/951" not in got
    assert got.endswith("13 keys added")


def test_a_bare_arrow_in_a_title_survives(qs):
    """THE TRAP IN THE OBVIOUS FIX. A bare arrow is ordinary prose in these titles, so
    stripping from the first `→` eats the title itself."""
    b = "- **2026-07-21** — **H1: igloohome logType 29 → `lock.code_changed`** *(adr)*: ADR amendment"
    assert "logType 29 → `lock.code_changed`" in qs.described(b)


def test_prose_containing_merged_is_not_truncated(qs):
    """A loose "merged … as" match ate this real vivy line mid-sentence, leaving a
    dangling paren. A tail marker has a shape: verb, then "as", then a PR link."""
    b = ("- **2026-07-21** — **F2: webhook validator** *(or-webhook)*: Already in main via "
         "PR #992 (merged 2026-07-18) which added the validator and scoped it to the web "
         "service as described")
    assert qs.described(b).endswith("as described")


# --- section splitting and bullet grouping -----------------------------------

def test_completed_section_ends_at_the_next_heading(qs):
    text = "# Q\n\n## P1\n\n- a\n\n## Completed (last 30 days)\n\n- b\n\n## Notes\n\n- c\n"
    before, body, after = qs.split_sections(text)
    assert "- b" in body and "- c" not in body
    assert after.startswith("\n## Notes")


def test_a_bullets_indented_continuation_stays_with_it(qs):
    """Sub-bullets carry follow-up notes. Splitting them off orphans the note."""
    got = qs.bullets("\n- item one\n  - a follow-up note\n- item two\n")
    assert len(got) == 2
    assert "follow-up note" in got[0]


# --- rendering ---------------------------------------------------------------

def test_render_redates_from_the_merge_not_the_authored_date(qs):
    """The authored date is the orchestrator's end-of-run belief and predates the
    merge — across constance it was out by up to six days."""
    pr = {"mergedAt": "2026-07-19T01:24:04Z", "url": "u",
          "mergeCommit": {"oid": "abcdef1234"}, "title": "t"}
    out = qs.render(pr, "- **2026-07-13 — A3: guard** — notes")
    assert "**2026-07-19 — A3: guard**" in out
    assert "2026-07-13" not in out


def test_render_falls_back_to_the_pr_title_with_no_prose(qs):
    pr = {"mergedAt": "2026-07-19T01:24:04Z", "url": "u",
          "mergeCommit": {"oid": "abcdef1234"}, "title": "fix: a thing"}
    assert "fix: a thing" in qs.render(pr, None)


# --- the classification loop: the fix this PR exists for ---------------------
# The helper tests above cover parsing. The loop that decides WHERE an entry goes
# had none, and it is the part that deleted five real vivy items.

def _run_main(qs, monkeypatch, tmp_path, states, doc_body):
    """Drive main() with resolve() stubbed, and return its stderr report + output doc."""
    queues = tmp_path / "queues"
    queues.mkdir(parents=True)
    doc = queues / "vivy.md"
    doc.write_text("# Q\n\n## P1\n\n- pending thing\n\n"
                   "## Completed (last 30 days)\n" + doc_body)
    (tmp_path / "Convivy" / "vivy").mkdir(parents=True)

    monkeypatch.setattr(qs, "QUEUE_ROOT", queues)
    monkeypatch.setenv("HOME", str(tmp_path))
    # The repo dir resolves from builder_root.root(), which ignores HOME; pinned so
    # CI, whose root is elsewhere, finds tmp_path/Convivy/vivy (PR 539's CI run).
    monkeypatch.setattr(qs.BR, "root", lambda *a, **k: tmp_path / "Convivy")
    monkeypatch.setattr(qs, "resolve", lambda repo_dir, n: dict(
        states[n], number=n, url=f"https://github.com/convivy/vivy/pull/{n}"))
    monkeypatch.setattr(qs.sys, "argv", ["queue-sync", "vivy", "--write"])
    qs.main()
    return doc.read_text()


def test_an_open_pr_item_is_kept_in_flight_not_deleted(qs, monkeypatch, tmp_path, capsys):
    """THE FIX. An OPEN PR's item was previously dropped from the doc entirely — on
    vivy that removed five real items awaiting a human merge. It must survive, and it
    must not read as completed."""
    body = ("\n- **2026-07-21 — A2: pii_fields expansion** — 13 keys "
            "→ https://github.com/convivy/vivy/pull/951\n")
    out = _run_main(qs, monkeypatch, tmp_path,
                    {951: {"state": "OPEN", "title": "expand pii_fields", "mergedAt": None}},
                    body)
    assert "pii_fields expansion" in out, "an in-flight item must not be deleted"
    assert "IN FLIGHT" in out, "and must not read as completed"


def test_a_closed_unmerged_item_is_removed_and_reported(qs, monkeypatch, tmp_path, capsys):
    """The #526 case, and the opposite call: closed-unmerged work never landed, so
    leaving it under Completed is how it disappears."""
    body = ("\n- **2026-07-20 — A1: dead code removal** — Landed as "
            "https://github.com/convivy/constance/pull/526\n")
    out = _run_main(qs, monkeypatch, tmp_path,
                    {526: {"state": "CLOSED", "title": "remove dead code", "mergedAt": None}},
                    body)
    assert "dead code removal" not in out
    assert "#526" in capsys.readouterr().err


def test_an_unresolvable_item_is_kept_verbatim(qs, monkeypatch, tmp_path, capsys):
    """An issue number rather than a PR. "Could not check" must never mean "delete"."""
    body = "\n- [ ] (triage) #424 follow-up — https://github.com/convivy/vivy/pull/424\n"
    out = _run_main(qs, monkeypatch, tmp_path, {424: {"state": "UNKNOWN"}}, body)
    assert "(triage) #424 follow-up" in out


# --- inputs that come from outside ------------------------------------------

def test_a_traversing_product_name_is_refused(qs, monkeypatch, tmp_path, capsys):
    """`product` is interpolated into a path, and with the queue repo-side (ARCH-390)
    nothing downstream re-checks it — there is no cage whose whitelist would catch a
    traversed target. This is the only gate."""
    monkeypatch.setattr(qs.sys, "argv", ["queue-sync", "../../tmp", "--write"])
    with pytest.raises(SystemExit) as e:
        qs.main()
    assert "simple identifier" in str(e.value)


def test_a_pr_title_cannot_break_out_of_its_markdown(qs):
    """A PR title is attacker-influenced — opening a PR is enough — and is written
    straight into the queue with no verifier in the path."""
    pr = {"mergedAt": "2026-07-19T01:00:00Z", "url": "u", "mergeCommit": {"oid": "abc1234"},
          "title": "foo** — [see here](https://elsewhere.example) — **bar"}
    out = qs.render(pr, None)
    assert "](https://elsewhere.example)" not in out, "no live external link"
    assert out.count("**") == 2, "the bold span must not be closed early"


# --- the --commit path -------------------------------------------------------
# Classification is well covered; the git mechanics were not, so a regression in the
# push-failure or nothing-to-commit handling would be invisible to CI (reviewer, #195).

def _commit_run(qs, monkeypatch, tmp_path, git_results):
    """Drive --commit with git stubbed. Returns (calls, exit_code)."""
    queues = tmp_path / "queues"
    queues.mkdir(parents=True)
    (queues / "vivy.md").write_text(
        "# Q\n\n## P1\n\n- pending\n\n## Completed (last 30 days)\n\n"
        "- **2026-07-21 — thing** — https://github.com/convivy/vivy/pull/951\n")
    (tmp_path / "Convivy" / "vivy").mkdir(parents=True)
    monkeypatch.setattr(qs, "QUEUE_ROOT", queues)
    monkeypatch.setenv("HOME", str(tmp_path))
    # The repo dir resolves from builder_root.root(), which ignores HOME; pinned so
    # CI, whose root is elsewhere, finds tmp_path/Convivy/vivy (PR 539's CI run).
    monkeypatch.setattr(qs.BR, "root", lambda *a, **k: tmp_path / "Convivy")
    monkeypatch.setattr(qs, "resolve", lambda repo_dir, n: {
        "number": n, "state": "MERGED", "title": "t", "mergedAt": "2026-07-21T00:00:00Z",
        "mergeCommit": {"oid": "abc1234"}, "url": f"https://github.com/convivy/vivy/pull/{n}"})

    calls = []

    class R:
        def __init__(self, rc, out="", err=""):
            self.returncode, self.stdout, self.stderr = rc, out, err

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if git_results:
            return git_results.pop(0)
        # Default to the ordinary case: the probe reports differences (`git diff
        # --quiet` exits 1 when it finds any), everything else succeeds. A blanket
        # rc=0 would make the probe say "nothing staged" and silently turn every
        # unstubbed test into a no-op run that never commits.
        return R(1) if "diff" in cmd else R(0)

    monkeypatch.setattr(qs.subprocess, "run", fake_run)
    monkeypatch.setattr(qs.sys, "argv", ["queue-sync", "vivy", "--commit"])
    try:
        return calls, qs.main()
    except SystemExit as e:
        return calls, e.code


def test_commit_adds_commits_and_pushes(qs, monkeypatch, tmp_path):
    calls, rc = _commit_run(qs, monkeypatch, tmp_path, [])
    assert rc == 0
    verbs = [c[3] for c in calls if len(c) > 3 and c[0] == "git"]
    assert verbs[:4] == ["add", "diff", "commit", "push"], verbs
    push = next(c for c in calls if len(c) > 3 and c[3] == "push")
    assert push[-2:] == ["origin", "HEAD"], \
        "the push destination must be explicit, not whatever upstream is configured"


# git says "nothing to commit" three different ways depending on what ELSE is dirty
# in the tree, and the no-op guard used to match only the first. The 06:00 backstop
# hit the third on 2026-07-23 and reported `queue-sync constance: FAILED (exit 1)`
# on a queue that was already current — a false alarm in the one log that has to stay
# trustworthy. The guard now asks git whether anything is staged instead of reading
# its prose, so these phrasings are pinned as the thing we must NOT depend on.
@pytest.mark.parametrize("phrasing", [
    "nothing to commit, working tree clean",
    "nothing added to commit but untracked files present",
    'no changes added to commit (use "git add" and/or "git commit -a")',
])
def test_an_already_current_queue_is_a_no_op_whatever_git_says(
        qs, monkeypatch, tmp_path, phrasing):
    """A re-run against an already-current queue is a no-op, not a failure — the
    orchestrator would otherwise report a spurious error every quiet night."""
    class R:
        def __init__(self, rc, out="", err=""):
            self.returncode, self.stdout, self.stderr = rc, out, err
    # add ok, staged-diff probe says "nothing staged", then the commit git would have
    # refused (reached only by a version that still tries to commit).
    calls, rc = _commit_run(qs, monkeypatch, tmp_path, [R(0), R(0), R(1, out=phrasing)])
    assert rc == 0, f"a no-op re-run must succeed; git said: {phrasing}"
    assert not any("push" in c for c in calls), "an already-current queue must not push"


def test_the_no_op_check_asks_git_rather_than_reading_its_prose(qs, monkeypatch, tmp_path):
    """Pins the mechanism, not just the outcome: git's messages are phrasing- and
    locale-dependent, so the guard has to be an exit code from a diff probe."""
    calls, rc = _commit_run(qs, monkeypatch, tmp_path, [])
    probe = next((c for c in calls if len(c) > 3 and c[3] == "diff"), None)
    assert probe is not None, "expected a staged-diff probe before the commit"
    assert "--quiet" in probe and "--cached" in probe, probe


def test_a_broken_probe_fails_loudly_rather_than_reading_as_no_op(qs, monkeypatch, tmp_path):
    """`git diff --quiet` exits 0 for "no changes" and 1 for "changes"; anything else is
    git itself failing. Treating that as "no changes" would drop a real reconciliation
    on the floor and report success."""
    class R:
        def __init__(self, rc, out="", err=""):
            self.returncode, self.stdout, self.stderr = rc, out, err
    calls, rc = _commit_run(qs, monkeypatch, tmp_path,
                            [R(0), R(128, err="fatal: not a git repository")])
    assert rc != 0
    assert not any("push" in c for c in calls)


def test_the_commit_is_scoped_to_the_queue_doc(qs, monkeypatch, tmp_path):
    """queue-sync commits one file whose path the caller cannot choose. Committing the
    whole index would sweep in anything a concurrent process happened to stage."""
    calls, _ = _commit_run(qs, monkeypatch, tmp_path, [])
    commit = next(c for c in calls if len(c) > 3 and c[3] == "commit")
    assert "--" in commit, commit
    assert commit[commit.index("--") + 1].endswith("vivy.md"), commit


def test_commit_fails_loudly_when_the_push_fails(qs, monkeypatch, tmp_path, capsys):
    """A failed push must not read as success — the commit is local and someone has
    to know."""
    class R:
        def __init__(self, rc, out="", err=""):
            self.returncode, self.stdout, self.stderr = rc, out, err
    calls, rc = _commit_run(  # add ok, probe finds changes, commit ok, push rejected
        qs, monkeypatch, tmp_path,
        [R(0), R(1), R(0), R(1, err="rejected: non-fast-forward")])
    assert rc != 0
