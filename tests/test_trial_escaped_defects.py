"""bin/trial-escaped-defects (CQ·527) — escaped-defect counting over a model
trial's merged pull requests.

The risk this guards: review passing is a proxy, not the thing itself, so the
count has to come from the repository's own history rather than from review
state. The fixture-repo tests build a real merge commit and real later
commits (never a stubbed `_git`) because the thing under test IS git's own
line-tracking behaviour — `git log -L` silently drops a line once something
deletes it rather than reporting the deletion (see the module docstring), so
a test against a stub of git would validate nothing about that.

Every repo, branch, person and path here is synthetic.
"""
from __future__ import annotations

import json
import os
import subprocess


# --- fixture-repo helpers -----------------------------------------------------


def _repo(tmp_path):
    path = tmp_path / "widget-repo"
    path.mkdir()

    def run(*args, when=None):
        env = None
        if when:
            env = {**os.environ, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
        subprocess.run(["git", "-C", str(path), *args], capture_output=True,
                       check=True, env=env)

    run("init", "-q", "-b", "main")
    run("config", "user.email", "widget-dev@example.com")
    run("config", "user.name", "Widget Dev")
    return path, run


def _build_pr_fixture(tmp_path, *, pr_number=42):
    """A merge commit for pull request `pr_number` that adds two lines to
    `f.py`, then three later commits on main: one within the escape window
    that changes one of the added lines, one within the window that changes
    only an unrelated line, and one past the window (day 16) that changes
    one of the added lines too. Returns the repo path."""
    path, run = _repo(tmp_path)

    (path / "f.py").write_text("base1\nbase2\nbase3\n")
    run("add", "-A")
    run("commit", "-qm", "base", when="2026-01-01T00:00:00")

    run("checkout", "-q", "-b", "feature")
    (path / "f.py").write_text("base1\nbase2\nbase3\nADDED1\nADDED2\n")
    run("add", "-A")
    run("commit", "-qm", "the trial PR's own commit", when="2026-01-02T00:00:00")

    run("checkout", "-q", "main")
    run("merge", "-q", "--no-ff", "-m", f"Merge pull request #{pr_number} from org/feature",
        "feature", when="2026-01-03T00:00:00")

    # within the 14-day window, changes ADDED1 (a line the PR added)
    (path / "f.py").write_text("base1\nbase2\nbase3\nADDED1-FIXED\nADDED2\n")
    run("add", "-A")
    run("commit", "-qm", "fix: touches a PR line", when="2026-01-05T00:00:00")

    # within the window, changes only base1 — never touches a PR line
    (path / "f.py").write_text("base1-touched\nbase2\nbase3\nADDED1-FIXED\nADDED2\n")
    run("add", "-A")
    run("commit", "-qm", "unrelated change", when="2026-01-06T00:00:00")

    # day 16 — past the 14-day window — changes ADDED2 (a PR line) too
    (path / "f.py").write_text("base1-touched\nbase2\nbase3\nADDED1-FIXED\nADDED2-LATE\n")
    run("add", "-A")
    run("commit", "-qm", "late fix: also touches a PR line", when="2026-01-19T00:00:00")

    tip = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    subprocess.run(["git", "-C", str(path), "update-ref", "refs/remotes/origin/main", tip],
                   capture_output=True, check=True)
    return path


def _branch_sha(path):
    import git_refs as GR
    return GR.exact_ref_sha(path, GR.ORIGIN_MAIN_REF)


# --- the fixture-repo gate: exactly the first fix counts ---------------------


def test_escaped_defects_for_pr_counts_exactly_the_in_window_touching_fix(
        trial_escaped_defects, tmp_path):
    path = _build_pr_fixture(tmp_path)
    result = trial_escaped_defects.escaped_defects_for_pr(
        path, 42, _branch_sha(path), days=14)
    assert result["merged"] is True
    shas = [f["short_sha"] for f in result["fixes"]]
    assert len(result["fixes"]) == 1, shas
    # the unrelated change and the day-16 fix are both excluded by name
    only_fix_subject = subprocess.run(
        ["git", "-C", str(path), "show", "-s", "--format=%s", result["fixes"][0]["sha"]],
        capture_output=True, text=True, check=True).stdout.strip()
    assert only_fix_subject == "fix: touches a PR line"


def test_escaped_defects_for_pr_handles_a_later_full_line_delete(
        trial_escaped_defects, tmp_path):
    """git log -L silently drops a line once something deletes it rather than
    reporting the deletion — this is the regression test for that gap."""
    path, run = _repo(tmp_path)
    (path / "f.py").write_text("base1\nbase2\n")
    run("add", "-A")
    run("commit", "-qm", "base", when="2026-02-01T00:00:00")
    run("checkout", "-q", "-b", "feature")
    (path / "f.py").write_text("base1\nbase2\nADDED1\n")
    run("add", "-A")
    run("commit", "-qm", "the trial PR's own commit", when="2026-02-02T00:00:00")
    run("checkout", "-q", "main")
    run("merge", "-q", "--no-ff", "-m", "Merge pull request #9 from org/feature",
        "feature", when="2026-02-03T00:00:00")
    (path / "f.py").write_text("base1\nbase2\n")   # deletes ADDED1 entirely
    run("add", "-A")
    run("commit", "-qm", "del: removes a PR line", when="2026-02-05T00:00:00")
    tip = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    subprocess.run(["git", "-C", str(path), "update-ref", "refs/remotes/origin/main", tip],
                   capture_output=True, check=True)

    result = trial_escaped_defects.escaped_defects_for_pr(path, 9, _branch_sha(path), days=14)
    assert len(result["fixes"]) == 1


def test_escaped_defects_for_pr_carries_tracking_through_a_pure_rename(
        trial_escaped_defects, tmp_path):
    """A later pure rename (`git mv`, no content change) must not drop
    tracking: a still-later edit of a PR line under the new path has to
    still count, and the rename commit itself must not be flagged (nothing
    in it was actually modified)."""
    path, run = _repo(tmp_path)
    (path / "f.py").write_text("base1\nbase2\nbase3\n")
    run("add", "-A")
    run("commit", "-qm", "base", when="2026-04-01T00:00:00")
    run("checkout", "-q", "-b", "feature")
    (path / "f.py").write_text("base1\nbase2\nbase3\nADDED1\nADDED2\n")
    run("add", "-A")
    run("commit", "-qm", "the trial PR's own commit", when="2026-04-02T00:00:00")
    run("checkout", "-q", "main")
    run("merge", "-q", "--no-ff", "-m", "Merge pull request #20 from org/feature",
        "feature", when="2026-04-03T00:00:00")
    run("mv", "f.py", "g.py")
    run("commit", "-qm", "rename: pure, no edit", when="2026-04-04T00:00:00")
    (path / "g.py").write_text("base1\nbase2\nbase3\nADDED1-FIXED\nADDED2\n")
    run("add", "-A")
    run("commit", "-qm", "fix: touches a PR line after the rename", when="2026-04-05T00:00:00")
    tip = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    subprocess.run(["git", "-C", str(path), "update-ref", "refs/remotes/origin/main", tip],
                   capture_output=True, check=True)

    result = trial_escaped_defects.escaped_defects_for_pr(path, 20, _branch_sha(path), days=14)
    assert len(result["fixes"]) == 1
    fix_subject = subprocess.run(
        ["git", "-C", str(path), "show", "-s", "--format=%s", result["fixes"][0]["sha"]],
        capture_output=True, text=True, check=True).stdout.strip()
    assert fix_subject == "fix: touches a PR line after the rename"


def test_escaped_defects_for_pr_counts_a_rename_with_edit_in_the_same_commit(
        trial_escaped_defects, tmp_path):
    """A later commit that renames a file AND edits a PR line in the same
    commit (`git diff -M`'s "rename from"/"rename to" header plus a real
    hunk below it) must flag that commit as a fix, AND must carry tracking
    to the new path so a still-later edit under it counts too. The combined
    rename+edit commit alone is not a reliable regression pin on its own:
    without rename handling, the old-path diff restricted to that one path
    still reads as a plain delete-and-flag by coincidence, so only a second,
    still-later edit under the new name distinguishes "carried the tracking
    across the rename" from "got lucky on the rename commit, then lost the
    file entirely"."""
    path, run = _repo(tmp_path)
    (path / "f.py").write_text("base1\nbase2\nbase3\n")
    run("add", "-A")
    run("commit", "-qm", "base", when="2026-04-10T00:00:00")
    run("checkout", "-q", "-b", "feature")
    (path / "f.py").write_text("base1\nbase2\nbase3\nADDED1\nADDED2\n")
    run("add", "-A")
    run("commit", "-qm", "the trial PR's own commit", when="2026-04-11T00:00:00")
    run("checkout", "-q", "main")
    run("merge", "-q", "--no-ff", "-m", "Merge pull request #21 from org/feature",
        "feature", when="2026-04-12T00:00:00")
    run("mv", "f.py", "g.py")
    (path / "g.py").write_text("base1\nbase2\nbase3\nADDED1-FIXED\nADDED2\n")
    run("add", "-A")
    run("commit", "-qm", "rename+fix: renames and touches a PR line together",
        when="2026-04-13T00:00:00")
    (path / "g.py").write_text("base1\nbase2\nbase3\nADDED1-FIXED\nADDED2-FIXED\n")
    run("add", "-A")
    run("commit", "-qm", "fix: touches the other PR line, still under the new name",
        when="2026-04-14T00:00:00")
    tip = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    subprocess.run(["git", "-C", str(path), "update-ref", "refs/remotes/origin/main", tip],
                   capture_output=True, check=True)

    result = trial_escaped_defects.escaped_defects_for_pr(path, 21, _branch_sha(path), days=14)
    assert len(result["fixes"]) == 2


def test_escaped_defects_for_pr_keeps_each_files_tracking_through_a_rename_chain(
        trial_escaped_defects, tmp_path, monkeypatch):
    """A single commit that renames two files in a chain (`b.py`->`c.py` AND
    `a.py`->`b.py` together) must not alias one file's tracked lines onto
    the other: popping/assigning `live` one rename at a time, in whatever
    order the `renames` dict happens to iterate, can overwrite the
    pre-commit `b.py` array before it is moved and then read the
    overwritten value back out for `c.py` — losing `b.py`'s own tracking
    and wrongly crediting it to `a.py`'s. A later edit under EACH new name
    must be caught against ITS OWN original PR line, not the other file's.

    Real git never actually reports this chain as two independent rename
    pairs: a rename's destination must be absent from the commit's parent
    tree, and "b.py" (freed by one rename, taken by the other in the same
    commit) can't be absent from the parent for one rename and present in
    it for the other at once (confirmed empirically — plain `git mv b.py
    c.py && git mv a.py b.py` in one commit reports `D a.py` / `M b.py` /
    `A c.py`, never a rename, under `-M`, `-C`, and `-B` alike). The repo
    below is built and committed for real throughout; only the ONE
    `git diff` call for the rename commit's own sha is intercepted, to
    return the exact "rename from"/"rename to" header shape the remap
    loop has to handle correctly regardless of dict-iteration order —
    everything else (finding the merge commit, walking later commits,
    diffing the two real fix commits that follow) runs through real git."""
    path, run = _repo(tmp_path)
    (path / "a.py").write_text("a-base1\n")
    (path / "b.py").write_text("b-base1\nb-base2\n")
    run("add", "-A")
    run("commit", "-qm", "base", when="2026-04-30T00:00:00")
    run("checkout", "-q", "-b", "feature")
    (path / "a.py").write_text("a-base1\nAADDED\n")
    (path / "b.py").write_text("b-base1\nb-base2\nBADDED\n")
    run("add", "-A")
    run("commit", "-qm", "the trial PR's own commit", when="2026-05-01T00:00:00")
    run("checkout", "-q", "main")
    run("merge", "-q", "--no-ff", "-m", "Merge pull request #22 from org/feature",
        "feature", when="2026-05-02T00:00:00")
    # real rename chain on disk: b.py->c.py frees "b.py", then a.py->b.py
    # takes it — git itself reports this as D/M/A (see docstring), not as
    # two renames, so the collision shape is injected below instead
    run("mv", "b.py", "c.py")
    run("mv", "a.py", "b.py")
    run("commit", "-qm", "rename: chains b.py->c.py and a.py->b.py together",
        when="2026-05-03T00:00:00")
    rename_sha = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                                capture_output=True, text=True, check=True).stdout.strip()
    # b.py now holds a.py's original content/PR line
    (path / "b.py").write_text("a-base1\nAADDED-FIXED\n")
    run("add", "-A")
    run("commit", "-qm", "fix: touches a.py's PR line, now under b.py",
        when="2026-05-04T00:00:00")
    # c.py now holds the original b.py's content/PR line
    (path / "c.py").write_text("b-base1\nb-base2\nBADDED-FIXED\n")
    run("add", "-A")
    run("commit", "-qm", "fix: touches b.py's PR line, now under c.py",
        when="2026-05-05T00:00:00")
    tip = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    subprocess.run(["git", "-C", str(path), "update-ref", "refs/remotes/origin/main", tip],
                   capture_output=True, check=True)

    synthetic_rename_chain_diff = (
        "diff --git a/a.py b/b.py\n"
        "similarity index 100%\n"
        "rename from a.py\n"
        "rename to b.py\n"
        "diff --git a/b.py b/c.py\n"
        "similarity index 100%\n"
        "rename from b.py\n"
        "rename to c.py\n"
    )
    real_run_git = trial_escaped_defects._run_git
    rename_diff_args = ["diff", "--no-color", "--no-ext-diff", "--no-textconv",
                        "-M", "-U0", f"{rename_sha}^1", rename_sha]

    def fake_run_git(repo_dir, args):
        if args == rename_diff_args:
            return subprocess.CompletedProcess(args, 0, stdout=synthetic_rename_chain_diff,
                                               stderr="")
        return real_run_git(repo_dir, args)

    monkeypatch.setattr(trial_escaped_defects, "_run_git", fake_run_git)

    result = trial_escaped_defects.escaped_defects_for_pr(path, 22, _branch_sha(path), days=14)
    subjects = [subprocess.run(
        ["git", "-C", str(path), "show", "-s", "--format=%s", f["sha"]],
        capture_output=True, text=True, check=True).stdout.strip()
        for f in result["fixes"]]
    assert sorted(subjects) == [
        "fix: touches a.py's PR line, now under b.py",
        "fix: touches b.py's PR line, now under c.py",
    ]


def test_escaped_defects_for_pr_tracks_a_shifted_line_after_an_earlier_insertion(
        trial_escaped_defects, tmp_path):
    """A later commit's length-changing insertion (one line above the whole
    PR-added range, one line inside it) shifts the tracked positions down;
    a still-later edit at the shifted position must still be caught — the
    insertion/offset half of the replay, exercised end to end through
    `escaped_defects_for_pr` rather than only in `parse_u0_diff` isolation.
    The insertion commit itself removes nothing, so it must not be
    flagged."""
    path, run = _repo(tmp_path)
    (path / "f.py").write_text("base1\nbase2\nbase3\n")
    run("add", "-A")
    run("commit", "-qm", "base", when="2026-04-20T00:00:00")
    run("checkout", "-q", "-b", "feature")
    (path / "f.py").write_text("base1\nbase2\nbase3\nADDED1\nADDED2\nADDED3\n")
    run("add", "-A")
    run("commit", "-qm", "the trial PR's own commit", when="2026-04-21T00:00:00")
    run("checkout", "-q", "main")
    run("merge", "-q", "--no-ff", "-m", "Merge pull request #30 from org/feature",
        "feature", when="2026-04-22T00:00:00")
    # one insertion above the whole added range, one inside it — nothing
    # removed, so this must not be flagged a fix
    (path / "f.py").write_text(
        "base1\nbase2\nbase3\nABOVE\nADDED1\nINSIDE\nADDED2\nADDED3\n")
    run("add", "-A")
    run("commit", "-qm", "insert: shifts the tracked range, touches nothing",
        when="2026-04-23T00:00:00")
    # edits ADDED2 at its shifted (post-insertion) position
    (path / "f.py").write_text(
        "base1\nbase2\nbase3\nABOVE\nADDED1\nINSIDE\nADDED2-FIXED\nADDED3\n")
    run("add", "-A")
    run("commit", "-qm", "fix: touches a PR line at its shifted position",
        when="2026-04-24T00:00:00")
    tip = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    subprocess.run(["git", "-C", str(path), "update-ref", "refs/remotes/origin/main", tip],
                   capture_output=True, check=True)

    result = trial_escaped_defects.escaped_defects_for_pr(path, 30, _branch_sha(path), days=14)
    assert len(result["fixes"]) == 1
    fix_subject = subprocess.run(
        ["git", "-C", str(path), "show", "-s", "--format=%s", result["fixes"][0]["sha"]],
        capture_output=True, text=True, check=True).stdout.strip()
    assert fix_subject == "fix: touches a PR line at its shifted position"


def test_escaped_defects_for_pr_none_when_no_merge_commit_found(
        trial_escaped_defects, tmp_path):
    path, run = _repo(tmp_path)
    (path / "f.py").write_text("base1\n")
    run("add", "-A")
    run("commit", "-qm", "base", when="2026-03-01T00:00:00")
    tip = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    subprocess.run(["git", "-C", str(path), "update-ref", "refs/remotes/origin/main", tip],
                   capture_output=True, check=True)

    result = trial_escaped_defects.escaped_defects_for_pr(path, 123, _branch_sha(path), days=14)
    assert result["merged"] is False
    assert result["fixes"] == []


# --- parse_u0_diff: pure, no git needed --------------------------------------


_SAMPLE_DIFF = """diff --git a/sub/f.py b/sub/f.py
index abc..def 100644
--- a/sub/f.py
+++ b/sub/f.py
@@ -5,0 +6,2 @@ l5
+ADDED1
+ADDED2
"""


def test_parse_u0_diff_reads_an_insertion_hunk(trial_escaped_defects):
    hunks, deleted, renames = trial_escaped_defects.parse_u0_diff(_SAMPLE_DIFF)
    assert hunks == {"sub/f.py": [(5, 0, 6, 2)]}
    assert deleted == set()
    assert renames == {}


_DELETE_FILE_DIFF = """diff --git a/gone.py b/gone.py
deleted file mode 100644
index abc..000 100644
--- a/gone.py
+++ /dev/null
@@ -1,3 +0,0 @@
-l1
-l2
-l3
"""


def test_parse_u0_diff_reports_a_whole_file_delete(trial_escaped_defects):
    hunks, deleted, renames = trial_escaped_defects.parse_u0_diff(_DELETE_FILE_DIFF)
    assert hunks == {}
    assert deleted == {"gone.py"}
    assert renames == {}


_PURE_RENAME_DIFF = """diff --git a/old.py b/new.py
similarity index 100%
rename from old.py
rename to new.py
"""


def test_parse_u0_diff_reads_a_pure_rename(trial_escaped_defects):
    hunks, deleted, renames = trial_escaped_defects.parse_u0_diff(_PURE_RENAME_DIFF)
    assert hunks == {}
    assert deleted == set()
    assert renames == {"old.py": "new.py"}


_RENAME_WITH_EDIT_DIFF = """diff --git a/old.py b/new.py
similarity index 54%
rename from old.py
rename to new.py
index abc..def 100644
--- a/old.py
+++ b/new.py
@@ -2 +2 @@ l1
-l2
+l2-EDITED
"""


def test_parse_u0_diff_reads_a_rename_with_edits(trial_escaped_defects):
    hunks, deleted, renames = trial_escaped_defects.parse_u0_diff(_RENAME_WITH_EDIT_DIFF)
    assert hunks == {"new.py": [(2, 1, 2, 1)]}
    assert deleted == set()
    assert renames == {"old.py": "new.py"}


# --- list_trial_prs: synthetic run dirs, no real agent-runs root -------------


def _write_run(runs_root, day, run_id, *, trial, repo, model, pr_number=None,
               started_at="2026-01-01T00:00:00Z"):
    run_dir = runs_root / day / run_id
    run_dir.mkdir(parents=True)
    meta = {"run_id": run_id, "trial": trial, "repo": repo, "model": model,
            "agent": "widget-coder", "started_at": started_at}
    (run_dir / "meta.json").write_text(json.dumps(meta))
    if pr_number is not None:
        (run_dir / "pr-link.json").write_text(json.dumps({"pr_number": pr_number}))
    return run_dir


def test_list_trial_prs_includes_only_the_named_trial_with_a_linked_pr(
        trial_escaped_defects, tmp_path):
    runs_root = tmp_path / "agent-runs"
    _write_run(runs_root, "2026-05-01", "run-aaa111", trial="widget-trial-1",
              repo="widget-repo", model="vendor/model-a", pr_number=5)
    _write_run(runs_root, "2026-05-01", "run-bbb222", trial="widget-trial-1",
              repo="widget-repo", model="vendor/model-a", pr_number=None)  # no link — excluded
    _write_run(runs_root, "2026-05-02", "run-ccc333", trial="other-trial",
              repo="widget-repo", model="vendor/model-b", pr_number=6)  # other trial — excluded

    rows = trial_escaped_defects.list_trial_prs("widget-trial-1", runs_root)
    assert [(r["repo"], r["pr_number"], r["model"]) for r in rows] == [
        ("widget-repo", 5, "vendor/model-a")]


def test_list_trial_prs_dedupes_two_runs_linked_to_the_same_pr(
        trial_escaped_defects, tmp_path):
    runs_root = tmp_path / "agent-runs"
    _write_run(runs_root, "2026-05-01", "run-aaa111", trial="widget-trial-1",
              repo="widget-repo", model="vendor/model-a", pr_number=5,
              started_at="2026-05-01T00:00:00Z")
    _write_run(runs_root, "2026-05-01", "run-aaa222", trial="widget-trial-1",
              repo="widget-repo", model="vendor/model-a", pr_number=5,
              started_at="2026-05-01T01:00:00Z")  # revise round, same PR

    rows = trial_escaped_defects.list_trial_prs("widget-trial-1", runs_root)
    assert len(rows) == 1
    assert rows[0]["run_id"] == "run-aaa111"  # the earliest-started one


def test_list_trial_prs_sanitises_the_model_and_agent_fields(
        trial_escaped_defects, tmp_path):
    """`meta.json["model"]`/`["agent"]` are written by the run itself, so an
    escape sequence or an embedded markdown table row in either must not
    survive into the row `render_report` later reads unsanitised."""
    runs_root = tmp_path / "agent-runs"
    nasty_model = "vendor/model-a\n| injected | row |\x1b[31mRED\x1b[0m"
    run_dir = _write_run(runs_root, "2026-05-01", "run-aaa111", trial="widget-trial-1",
                         repo="widget-repo", model=nasty_model, pr_number=5)
    meta = json.loads((run_dir / "meta.json").read_text())
    meta["agent"] = "widget-coder\x1b[31m"
    (run_dir / "meta.json").write_text(json.dumps(meta))

    rows = trial_escaped_defects.list_trial_prs("widget-trial-1", runs_root)
    assert len(rows) == 1
    assert "\x1b" not in rows[0]["model"]
    assert "\n" not in rows[0]["model"]
    assert "\x1b" not in rows[0]["agent"]

    out = trial_escaped_defects.render_report(
        [{**rows[0], "fixes": [], "signals": None}], [])
    assert "\x1b" not in out
    # the injected newline must not start a new markdown table row
    assert "\n| injected | row |" not in out


def test_render_report_escapes_a_pipe_in_the_model_field(trial_escaped_defects):
    """`sanitise()` strips control sequences and newlines but leaves `|`
    untouched, so a run-record field such as `model` containing `| x | y`
    would otherwise split its own row into extra cells and push the
    escaped-defect count past the table's last column, where GFM drops it.
    `_table_cells` is `session_view`'s own markdown-table-cell reader (a
    backslash before `|` is how a cell carries a literal pipe) — used here
    to parse the rendered row the same way a human-facing reader would."""
    import session_view

    rows = [{"repo": "widget-repo", "pr_number": 5, "model": "m | injected | row",
             "fixes": [{"sha": "a" * 40, "short_sha": "aaaaaaaaaa", "committer_ts": 0}],
             "signals": [{"id": "sig_1"}]}]
    out = trial_escaped_defects.render_report(rows, [])
    data_row = next(line for line in out.splitlines() if line.startswith("| widget-repo"))
    cells = session_view._table_cells(data_row)

    assert len(cells) == 6
    assert cells[2] == "m | injected | row"   # the raw text, cell-escaping undone
    assert cells[3] == "2"                    # 1 fix + 1 signal, in its own column


def test_list_trial_prs_refuses_a_traversal_shaped_repo_name(
        trial_escaped_defects, tmp_path):
    """meta.json["repo"] is written by the run itself; ".." must never reach
    `repo_checkout_dir`'s `root / repo` build."""
    runs_root = tmp_path / "agent-runs"
    _write_run(runs_root, "2026-05-01", "run-aaa111", trial="widget-trial-1",
              repo="..", model="vendor/model-a", pr_number=5)
    assert trial_escaped_defects.list_trial_prs("widget-trial-1", runs_root) == []


# --- repo_checkout_dir ---------------------------------------------------------


def test_repo_checkout_dir_refuses_traversal_and_missing_dirs(
        trial_escaped_defects, tmp_path):
    assert trial_escaped_defects.repo_checkout_dir("..", tmp_path) is None
    assert trial_escaped_defects.repo_checkout_dir("not-a-real-repo", tmp_path) is None


def test_repo_checkout_dir_finds_a_real_checkout(trial_escaped_defects, tmp_path):
    (tmp_path / "widget-repo").mkdir()
    assert trial_escaped_defects.repo_checkout_dir(
        "widget-repo", tmp_path) == tmp_path / "widget-repo"


# --- _references_pr / escaped_count / render_report (pure) -------------------


def test_references_pr_matches_repo_hash_number_and_pull_url(trial_escaped_defects):
    f = trial_escaped_defects._references_pr
    assert f("widget-repo#5 escaped review", "widget-repo", 5) is True
    assert f("https://github.com/example/widget-repo/pull/5", "widget-repo", 5) is True
    assert f("widget-repo#50 escaped review", "widget-repo", 5) is False
    assert f("no reference here", "widget-repo", 5) is False


def test_references_pr_scopes_pull_and_issues_urls_to_the_named_repo(trial_escaped_defects):
    """An unscoped `pull/`/`issues/` alternative would match any repo's
    PR/issue link sharing the number — this trial spans several repos with
    small, overlapping PR numbers (cart#15 vs shipwright#15, concretely)."""
    f = trial_escaped_defects._references_pr
    other_repos_pull_url = "https://github.com/org/shipwright/pull/15"
    assert f(other_repos_pull_url, "cart", 15) is False
    assert f(other_repos_pull_url, "shipwright", 15) is True
    other_repos_issue_url = "https://github.com/org/shipwright/issues/15"
    assert f(other_repos_issue_url, "cart", 15) is False
    assert f(other_repos_issue_url, "shipwright", 15) is True
    # a number that is a prefix of another, through the URL form too
    assert f("https://github.com/org/widget-repo/pull/50", "widget-repo", 5) is False
    assert f("https://github.com/org/widget-repo/pull/5", "widget-repo", 50) is False


def test_escaped_count_adds_git_fixes_and_signal_matches(trial_escaped_defects):
    row = {"fixes": [{"sha": "a"}], "signals": [{"id": "sig_1"}, {"id": "sig_2"}]}
    assert trial_escaped_defects.escaped_count(row) == 3


def test_escaped_count_treats_unavailable_signals_as_zero_not_a_crash(trial_escaped_defects):
    row = {"fixes": [{"sha": "a"}], "signals": None}
    assert trial_escaped_defects.escaped_count(row) == 1


def test_render_report_shows_unavailable_for_a_none_signal_search(trial_escaped_defects):
    rows = [{"repo": "widget-repo", "pr_number": 5, "model": "vendor/model-a",
             "fixes": [], "signals": None}]
    out = trial_escaped_defects.render_report(rows, [])
    assert "unavailable" in out
    assert "widget-repo" in out and "#5" in out


def test_render_report_lists_skipped_prs_with_their_reason(trial_escaped_defects):
    out = trial_escaped_defects.render_report(
        [], [("widget-repo", 7, "no local primary checkout")])
    assert "widget-repo#7: no local primary checkout" in out
