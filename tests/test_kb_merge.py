"""The Library's merge of its own knowledge PRs (platform/decisions/0078).

`refusals()` is the whole control, so each condition is shown tripping on its
own against an otherwise admissible PR: a test that only checks the happy path
would pass with every condition deleted.
"""
from __future__ import annotations

import base64
import http.server
import importlib.util
import io
import json
import os
import subprocess
import sys
import threading
import urllib.error

import pytest

import agent_policy as AP
import builder_root as BR
import kb_merge as KM
import review_protocol as RP

HEAD = "a" * 40
MAIN = "m" * 40
TRAILER = "KB-Write: propose projects/platform/x.md proposal library"
# Today's Convivy values, as `library-app.json` holds them (the config fixture
# in conftest.py pins the same values for every test in this file). Local to
# this file rather than read off `kb_merge`, which holds none of Convivy's own
# any more (issue_01M3SGPX7AMVR2SKREVPWEPKN0).
REPO = "convivy/knowledge"
BOT_LOGIN = "convivy-library[bot]"
BOT_USER_ID = 328494887
BOT = {"login": BOT_LOGIN, "id": BOT_USER_ID, "type": "Bot"}
_REAL_RUN = subprocess.run  # captured before any test patches it, for the git calls


@pytest.fixture(autouse=True)
def _origin_is_github(agentctl, monkeypatch):
    """`kb merge` is GitHub-only by design, and the rest of this file exercises
    the App-identity plumbing (`_kb_library`, `_kb_library_git`, `_KbLibrary`)
    that `kb apply`/`kb propose` also reach on a non-local origin — none of
    these tests sets up its own knowledge checkout. Left unpinned, the origin
    decision (issue_01M3M67C7VB7ZVYQ0MBKHZVYBS) would read whatever
    `agentctl.KNOWLEDGE_ROOT` happens to resolve to on the machine running the
    suite: the real checkout on a dev box (which really is GitHub-backed, so
    this would pass there by accident), nothing on CI (which is not, so it
    would fail there for a reason no test here names). Pinning `_kb_origin_is_local`
    to False makes every test in this file take the App branch regardless of
    what else is on disk; `_kb_origin_is_github` is also pinned to True so a
    refusal in this file reads with the App-identity wording, not the
    unrecognised-remote wording."""
    monkeypatch.setattr(agentctl, "_kb_origin_is_local", lambda root: False)
    monkeypatch.setattr(agentctl, "_kb_origin_is_github", lambda root: True)


def _policy_text(knowledge=True, instructions=True, rounds=5, code=None):
    """A policy file in the exact form `agent_policy.load` reads. `code` adds the
    `review_rounds_code` line; without it the file has none, and the code cap is 11."""
    code_line = "" if code is None else f"review_rounds_code: {code}\n"
    return (f"policy: platform/agent-policy\nschema_version: 1\n"
            f"agents_may_merge_knowledge: {str(knowledge).lower()}\n"
            f"agents_may_merge_knowledge_instructions: {str(instructions).lower()}\n"
            f"agents_may_merge_documents: false\n"
            f"review_rounds_per_role: {rounds}\n{code_line}max_concurrent: 16\n"
            f"max_workers_per_team: 4\nmax_live_supervisors: 4\n")


def _policy(**kw):
    return AP.load(_policy_text(**kw))


def _change(path="projects/x/a.md", text="one\n"):
    """A plain addition of a regular file."""
    return KM.Change(None, None, None, path, "100644", text)


def _stamp(agent, run_id, commit=HEAD, verdict="APPROVE", pinned=None):
    """A stamp whose `reviewed_commit` text is `commit` and whose authenticated run pinned
    `pinned` (the run's full review sha). `pinned=None` pins the run to `commit`, the case a
    real stamp takes; `pinned=""` is a run that pins no sha."""
    return RP.ReviewStamp(version="1", pr="7", repo="knowledge", reviewed_commit=commit,
                          verdict=verdict, agent=agent, run_id=run_id,
                          pinned_sha=commit if pinned is None else pinned)


def _commit(sha, message):
    return {"sha": sha, "commit": {"message": message}}


def _facts(**over):
    base = dict(
        repo="convivy/knowledge", number=7, state="OPEN", head_ref="kb/docs-x-1234",
        base_ref="main", cross_repo=False, head_sha=HEAD, body="why\n",
        commits=[_commit("c" * 40, f"docs: x\n\n{TRAILER}\n")],
        stamps=[_stamp("reviewer", "run-00000001"), _stamp("security-reviewer", "run-00000002")],
        review_ok=True, gate_head=HEAD, companions={},
        author=BOT, policy=_policy(), changes=[_change()],
        base_sha=MAIN, head_contains_base=True)
    base.update(over)
    return KM.Facts(**base)


def test_an_admissible_pr_is_admitted():
    assert KM.refusals(_facts()) == []


def test_the_repository_name_is_compared_as_github_compares_it():
    """GitHub resolves `Convivy/Knowledge` to the same repository, so refusing it
    would be a spurious refusal (security-reviewer, PR #546)."""
    assert KM.refusals(_facts(repo="Convivy/Knowledge")) == []


@pytest.mark.parametrize("over, expect", [
    (dict(repo="convivy/agent-platform"), "not convivy/knowledge"),
    (dict(state="MERGED"), "not open"),
    (dict(base_ref="release"), "not 'main'"),
    (dict(cross_repo=True), "fork"),
    (dict(head_ref="fix/validate-drops-convivy-lab"), "does not start with 'kb/'"),
    (dict(commits=None), "commits could not be read"),
    (dict(commits=[_commit("d" * 40, "docs: hand-built\n")]), "without a KB-Write trailer"),
    (dict(review_ok=False), "review-status` is not PASS"),
    (dict(gate_head="b" * 40), "head moved"),
    (dict(head_contains_base=False), "does not contain 'main'"),
])
def test_each_condition_refuses_on_its_own(over, expect):
    why = KM.refusals(_facts(**over))
    assert len(why) == 1 and expect in why[0], why


def test_one_commit_without_the_trailer_refuses_even_among_stamped_ones():
    commits = [_commit("c" * 40, f"docs: x\n\n{TRAILER}\n"), _commit("e" * 40, "fixup by hand\n")]
    why = KM.refusals(_facts(commits=commits))
    assert why == ["commits without a KB-Write trailer: " + "e" * 12]


def test_a_pr_at_githubs_commit_ceiling_refuses():
    commits = [_commit(f"{i:040x}", TRAILER) for i in range(KM.MAX_PR_COMMITS)]
    assert "ceiling" in KM.refusals(_facts(commits=commits))[0]


def test_a_sixth_round_refuses_and_the_fifth_does_not():
    five = [_stamp("reviewer", f"run-0000000{i}") for i in range(1, 6)]
    six = five + [_stamp("reviewer", "run-00000009")]
    sec = [_stamp("security-reviewer", "run-000000aa")]
    assert KM.refusals(_facts(stamps=five + sec)) == []
    why = KM.refusals(_facts(stamps=six + sec))
    assert len(why) == 1 and "reviewer ran 6 rounds" in why[0]


def test_the_round_cap_is_the_policys_number_not_five():
    """At the policy's number a role passes and one round over refuses, for a
    value other than 5. A constant of 5 would pass four rounds and refuse none."""
    def rounds(n):
        return [_stamp("reviewer", f"run-r{i:07d}") for i in range(n)] + [
            _stamp("security-reviewer", "run-000000aa")]
    three = _policy(rounds=3)
    assert KM.refusals(_facts(policy=three, stamps=rounds(3))) == []
    why = KM.refusals(_facts(policy=three, stamps=rounds(4)))
    assert len(why) == 1 and "reviewer ran 4 rounds, past the cap of 3" in why[0], why
    assert KM.refusals(_facts(policy=_policy(rounds=7), stamps=rounds(7))) == []


def test_a_fallback_policy_caps_rounds_at_the_files_named_five():
    fb = AP.Fallback("the policy file is missing at origin/main")
    six = [_stamp("reviewer", f"run-r{i:07d}") for i in range(6)]
    why = KM.refusals(_facts(policy=fb, stamps=six + [_stamp("security-reviewer", "run-000000aa")]))
    assert any("past the cap of 5" in w for w in why), why


@pytest.mark.parametrize("author", [
    {"login": "operator-account", "id": 1234, "type": "User"},
    {"login": BOT_LOGIN, "id": 1234, "type": "Bot"},                    # another id
    {"login": BOT_LOGIN, "id": BOT_USER_ID, "type": "User"},           # another type
    {"login": "convivy-library", "id": BOT_USER_ID, "type": "Bot"},     # no [bot]
    {"login": "other-app[bot]", "id": 99, "type": "Bot"},
    {},
    None,
])
def test_a_pr_authored_by_any_other_account_refuses_even_on_a_kb_branch(author):
    """The default `head_ref` is `kb/...`, the prefix an agent can write freely."""
    why = KM.refusals(_facts(author=author))
    assert len(why) == 1 and "was opened by" in why[0] and BOT_LOGIN in why[0], why


def test_the_apps_own_pr_on_a_branch_without_the_prefix_is_still_refused():
    """The prefix is kept as an extra condition, so an App-authored PR that
    `kb propose` did not build does not gain admission through the author."""
    why = KM.refusals(_facts(head_ref="fix/x"))
    assert len(why) == 1 and "does not start with 'kb/'" in why[0], why


def test_the_bot_identity_is_the_one_the_libraries_commits_carry():
    assert KM.commit_name() == BOT_LOGIN
    assert KM.commit_email().startswith(f"{BOT_USER_ID}+{BOT_LOGIN}@")


def test_a_pr_with_an_unread_policy_or_unread_files_refuses():
    assert KM.refusals(_facts(policy=None)) == ["the agent policy was not read"]
    assert KM.refusals(_facts(changes=None)) == ["the PR's changed files could not be read"]
    why = KM.refusals(_facts(git_problem="could not fetch (offline)"))
    assert why == ["the knowledge checkout could not be read: could not fetch (offline)"]


def test_a_branch_behind_main_refuses_naming_both_shas():
    """knowledge#699 (issue_01M3XNHABVJ4RZJPQAQ13A46DW): a PASS recorded against a
    stale main must not admit a branch that main has since moved past."""
    why = KM.refusals(_facts(head_contains_base=False))
    assert len(why) == 1
    assert MAIN[:12] in why[0] and HEAD[:12] in why[0]
    assert "merged in" in why[0] and "re-run" in why[0]


def test_a_git_problem_reports_itself_alone_even_when_the_ancestry_would_also_refuse():
    why = KM.refusals(_facts(git_problem="could not fetch (offline)", head_contains_base=False))
    assert why == ["the knowledge checkout could not be read: could not fetch (offline)"]


def test_an_unread_ancestry_refuses_even_without_a_git_problem():
    """`refusals()` must admit only on positive evidence, like its two siblings in
    `_policy_refusals` (`policy is None`, `changes is None`): a `Facts` whose
    ancestry was never read (`head_contains_base` and `base_sha` both `None`)
    with no `git_problem` set names a state no production caller builds today,
    but the check must not read it as the ancestry having been confirmed."""
    why = KM.refusals(_facts(base_sha=None, head_contains_base=None))
    assert len(why) == 1 and "never read" in why[0], why


def test_a_re_review_at_an_unchanged_head_counts_as_a_round():
    """0078 counts distinct commits, which would call this one round. It is two."""
    stamps = [_stamp("security-reviewer", "run-00000001"),
              _stamp("security-reviewer", "run-00000002")]
    assert KM.rounds_by_role(stamps)["security-reviewer"] == 2


def test_a_stamp_with_no_run_id_makes_the_round_count_unreadable():
    stamps = [_stamp("reviewer", ""), _stamp("security-reviewer", "run-00000002")]
    why = KM.refusals(_facts(stamps=stamps))
    assert len(why) == 1 and "round count cannot be read" in why[0]


def test_an_internal_app_reviewer_counts_toward_its_role():
    """The gate counts `platform-reviewer` as the reviewer role
    (review_protocol._review_base), so the round cap must too. Otherwise a PR
    reviewed under a prefixed name passes review-status with its rounds read as
    zero, and the cap never fires."""
    six = [_stamp("platform-reviewer", f"run-0000000{i}") for i in range(1, 7)]
    why = KM.refusals(_facts(stamps=six + [_stamp("security-reviewer", "run-000000aa")]))
    assert len(why) == 1 and "reviewer ran 6 rounds" in why[0], why


def test_the_2026_09_12_companions_refuse_while_408_is_open_and_admit_once_merged():
    """knowledge#404 and #405 had to wait for #408 (validate.py drops convivy-lab)."""
    body = "The schema drops convivy-lab.\n\nMerge-after: convivy/knowledge#408\n"
    open_ = KM.refusals(_facts(body=body, companions={("convivy/knowledge", 408): "OPEN"}))
    assert open_ == ["the companion convivy/knowledge#408 is OPEN, not merged"]
    assert KM.refusals(_facts(body=body, companions={("convivy/knowledge", 408): "MERGED"})) == []


def test_a_cross_repo_companion_counts_and_an_unreadable_one_refuses():
    """knowledge#406 had to wait for library#110."""
    body = "Merge-after: convivy/library#110\n"
    assert KM.refusals(_facts(body=body, companions={("convivy/library", 110): "MERGED"})) == []
    why = KM.refusals(_facts(body=body, companions={}))
    assert why == ["the companion convivy/library#110 could not be read"]


def test_merge_after_lines_are_parsed_exactly():
    body = ("Merge-after: convivy/knowledge#408\n"
            "  Merge-after: convivy/x#1\n"          # indented: not a trailer line
            "Merge-after: convivy/library#110 \n"
            "Merge-after: convivy/knowledge#408\n"  # duplicate
            "mentions Merge-after: convivy/y#2 in prose\n")
    assert KM.merge_after(body) == [("convivy/knowledge", 408), ("convivy/library", 110)]


def test_every_failing_condition_is_reported_not_just_the_first():
    why = KM.refusals(_facts(repo="convivy/vivy", head_ref="main", review_ok=False))
    assert len(why) == 3


def test_a_non_empty_review_reason_is_reported_instead_of_the_generic_message():
    why = KM.refusals(_facts(review_ok=False, review_reasons=["reviewer latest verdict "
                                                               "is REQUEST_CHANGES"]))
    assert why == ["`agentctl review-status`: reviewer latest verdict is REQUEST_CHANGES"]


# --- carrying an earlier APPROVE onto a rebuilt head (platform/decisions/0078) --
#
# A single, non-bare, remote-less repo: `carry_blocked` only ever reads commits
# already in one checkout, so these build S and H directly rather than through
# `KnowledgeRepo`'s bare-origin-plus-push machinery (unusable in this harness's
# sandbox — confirmed on origin/main too: every `krepo`/`kb`-based test below
# fails identically there with no code of this PR's in the loop at all).


class _LocalRepo:
    """A plain git repo, no remote, for `carry_blocked`'s pure git reads."""

    def __init__(self, tmp_path):
        self.path = tmp_path / "repo"
        self.path.mkdir()
        self.git("init", "-q", "-b", "main")
        for k, v in (("user.name", "t"), ("user.email", "t@example.com"),
                     ("commit.gpgsign", "false")):
            self.git("config", k, v)

    def git(self, *a):
        r = _REAL_RUN(["git", *a], cwd=self.path, capture_output=True, text=True)
        assert r.returncode == 0, f"git {a}: {r.stderr}"
        return r.stdout.strip()

    def commit(self, files, msg="x", parent=None):
        """`files`: path -> text | None (delete) | (mode, text). `parent`, when
        given, is checked out (detached) before committing, so a commit can be
        built on top of any earlier one without disturbing `main`."""
        if parent is not None:
            self.git("checkout", "-q", "--detach", parent)
        for rel, val in files.items():
            f = self.path / rel
            if val is None:
                f.unlink()
                continue
            f.parent.mkdir(parents=True, exist_ok=True)
            mode, text = val if isinstance(val, tuple) else ("100644", val)
            f.write_text(text)
            f.chmod(0o755 if mode == "100755" else 0o644)
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", msg)
        return self.git("rev-parse", "HEAD")


@pytest.fixture
def lrepo(tmp_path):
    return _LocalRepo(tmp_path)


def _scenario(lrepo, *, before_main=None, before_pr=None, head_files=None, pr_files=None):
    """`M0 -> M1 (base)`, "main" moving on with an unrelated file, and
    `M0 -> S` / `M1 -> H` as the two independent reviews of the same PR, one
    before the move and one after. `before_main`/`before_pr` are extra
    `files` committed into M0 before the two branches diverge (the
    pre-existing content each side's merge base carries). `head_files`
    defaults to the same content as `pr_files`, the "identical rebuild" case;
    pass it to build a refusal. Returns `(s, h, base)`."""
    m0 = lrepo.commit(dict(before_main or {}, **(before_pr or {})), "m0")
    base = lrepo.commit({"projects/unrelated/y.md": "a different document\n"},
                        "main moves on", parent=m0)
    pr_files = pr_files or {"projects/x/new.md": "hello\n"}
    s = lrepo.commit(pr_files, "pr (first review)", parent=m0)
    h = lrepo.commit(head_files if head_files is not None else pr_files,
                     "pr (rebuilt)", parent=base)
    return s, h, base


def test_carry_blocked_admits_identical_content_onto_a_moved_main(lrepo):
    s, h, base = _scenario(lrepo)
    assert KM.carry_blocked(lrepo.path, s, h, base) is None


def test_carry_blocked_refuses_a_changed_byte_in_a_changed_file_at_h(lrepo):
    s, h, base = _scenario(lrepo, head_files={"projects/x/new.md": "hello, but different\n"})
    why = KM.carry_blocked(lrepo.path, s, h, base)
    assert why is not None and "projects/x/new.md differs" in why


def test_carry_blocked_refuses_a_changed_main_side_original(lrepo):
    """`main` itself holds a different version of the very file S and H both
    end up with — S and H agree with each other, but not with what each one's
    own review judged it against."""
    s, h, base = _scenario(
        lrepo, before_main={"projects/x/shared.md": "the original on main\n"},
        pr_files={"projects/x/shared.md": "the same new text\n"})
    # `base` (M1) still carries the ORIGINAL text — main itself never touched
    # this path — so this scenario as built would carry; mutate M1 in place to
    # give it a DIFFERENT original than M0 had, without disturbing S or H.
    lrepo.git("checkout", "-q", "--detach", base)
    newbase = lrepo.commit({"projects/x/shared.md": "a different original, "
                                                     "edited on main after S\n"},
                           "main edits the shared file too", parent=base)
    why = KM.carry_blocked(lrepo.path, s, h, newbase)
    assert why is None, why   # the control: H was built from `base`, not `newbase`
    # Re-point H at a commit built on `newbase` instead, carrying the SAME text
    # S and the original H agreed on, so only the main-side original differs.
    h2 = lrepo.commit({"projects/x/shared.md": "the same new text\n"},
                      "pr (rebuilt again)", parent=newbase)
    why = KM.carry_blocked(lrepo.path, s, h2, newbase)
    assert why is not None and "main-side original" in why and "shared.md" in why


def test_carry_blocked_refuses_an_added_path(lrepo):
    s, h, base = _scenario(
        lrepo, head_files={"projects/x/new.md": "hello\n", "projects/x/extra.md": "also new\n"})
    why = KM.carry_blocked(lrepo.path, s, h, base)
    assert why is not None and "only at" in why and "extra.md" in why


def test_carry_blocked_refuses_a_removed_path(lrepo):
    s, h, base = _scenario(
        lrepo, before_pr={"projects/x/old.md": "keep me\n"},
        pr_files={"projects/x/new.md": "hello\n", "projects/x/old.md": None},
        head_files={"projects/x/new.md": "hello\n"})   # H never deletes old.md
    why = KM.carry_blocked(lrepo.path, s, h, base)
    assert why is not None and "only at" in why and "old.md" in why


def test_carry_blocked_refuses_a_mode_change(lrepo):
    s, h, base = _scenario(
        lrepo, pr_files={"projects/x/new.md": ("100644", "hello\n")},
        head_files={"projects/x/new.md": ("100755", "hello\n")})
    why = KM.carry_blocked(lrepo.path, s, h, base)
    assert why is not None and "projects/x/new.md differs" in why


def test_carry_blocked_refuses_a_stamp_commit_absent_from_the_object_store(lrepo):
    s, h, base = _scenario(lrepo)
    ghost = "d" * 40
    with pytest.raises(KM.Unreadable, match="object store"):
        KM.carry_blocked(lrepo.path, ghost, h, base)


def test_carry_blocked_refuses_when_no_merge_base_can_be_computed(lrepo):
    s, h, base = _scenario(lrepo)
    lrepo.git("checkout", "-q", "--orphan", "unrelated")
    unrelated = lrepo.commit({"nothing/to/do/with.md": "a disjoint history\n"}, "orphan")
    with pytest.raises(KM.Unreadable, match="merge-base"):
        KM.carry_blocked(lrepo.path, s, unrelated, base)


def test_carry_for_role_never_carries_a_request_changes(lrepo):
    s, h, base = _scenario(lrepo)
    candidates = [_stamp("reviewer", "run-00000001", commit=s, verdict="REQUEST_CHANGES")]
    commit, why = KM.carry_for_role(lrepo.path, candidates, h, base)
    assert commit is None and "REQUEST_CHANGES" in why and "not APPROVE" in why


def test_carry_for_role_never_carries_an_approve_the_role_itself_superseded(lrepo):
    """Round 1 of agent-platform#840, finding 1: the reviewer approves S, then
    requests changes on a later commit; the author rebuilds back to S's exact
    bytes. The role's LATEST stamp is the REQUEST_CHANGES, so nothing carries
    — the earlier 'try every APPROVE, oldest skipped' form admitted this over
    a live REQUEST_CHANGES, which is exactly the bug this fixes."""
    s, h, base = _scenario(lrepo)
    candidates = [_stamp("reviewer", "run-00000001", commit=s),
                 _stamp("reviewer", "run-00000002", commit=base, verdict="REQUEST_CHANGES")]
    commit, why = KM.carry_for_role(lrepo.path, candidates, h, base)
    assert commit is None and "REQUEST_CHANGES" in why and "not APPROVE" in why


def test_carry_for_role_carries_the_latest_approve_over_an_earlier_request_changes(lrepo):
    """The opposite order: an earlier REQUEST_CHANGES is superseded by a later
    APPROVE at S, and S carries to H normally."""
    s, h, base = _scenario(lrepo)
    candidates = [_stamp("reviewer", "run-00000001", commit=base, verdict="REQUEST_CHANGES"),
                 _stamp("reviewer", "run-00000002", commit=s)]
    commit, why = KM.carry_for_role(lrepo.path, candidates, h, base)
    assert commit == s and why == ""


def test_carry_for_role_never_falls_back_past_the_latest_approve(lrepo):
    """An OLDER APPROVE would satisfy `carry_blocked`; it must never be tried
    once a LATER APPROVE is the role's current stamp and that one does not
    satisfy it — there is no fallback to an earlier, superseded stamp,
    approved or not."""
    s, h, base = _scenario(lrepo)
    blocked_commit = lrepo.commit({"projects/x/new.md": "a later, now-wrong approval\n"},
                                  "a later, now-wrong approval", parent=base)
    candidates = [_stamp("reviewer", "run-00000001", commit=s),
                 _stamp("reviewer", "run-00000002", commit=blocked_commit)]
    commit, why = KM.carry_for_role(lrepo.path, candidates, h, base)
    assert commit is None and "differs" in why


# --- the Library's own config, library-app.json ---------------------------------


_OMIT = object()


def _write_config(path, **over):
    """A `library-app.json` in the exact shape `load_config` reads, today's
    Convivy values unless `over` replaces a field. Passing a field as
    `_OMIT` drops it, for the missing-field refusals. Creates `path`'s parent."""
    data = {"repo": REPO, "app_id": "4923626", "bot_login": BOT_LOGIN,
            "bot_user_id": BOT_USER_ID}
    for k, v in over.items():
        if v is _OMIT:
            data.pop(k, None)
        else:
            data[k] = v
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


def test_a_missing_config_file_refuses_naming_the_file(tmp_path):
    path = tmp_path / "library-app.json"
    with pytest.raises(KM.Unreadable) as e:
        KM.load_config(path)
    assert str(e.value) == f"{path} does not exist"


def test_unreadable_json_refuses_naming_the_file(tmp_path):
    path = tmp_path / "library-app.json"
    path.write_text("{not json")
    with pytest.raises(KM.Unreadable) as e:
        KM.load_config(path)
    assert str(e.value).startswith(f"{path} is not valid JSON:")


def test_a_json_document_that_is_not_an_object_refuses_naming_the_file(tmp_path):
    path = tmp_path / "library-app.json"
    path.write_text("[1, 2, 3]")
    with pytest.raises(KM.Unreadable) as e:
        KM.load_config(path)
    assert str(e.value) == f"{path} does not hold a JSON object"


@pytest.mark.parametrize("field, bad", [
    ("repo", _OMIT), ("repo", 123), ("repo", ""), ("repo", "no-slash"),
    ("repo", "too/many/slashes"), ("repo", "/name"), ("repo", "owner/"),
    ("app_id", _OMIT), ("app_id", 4923626), ("app_id", ""),
    ("bot_login", _OMIT), ("bot_login", 328494887), ("bot_login", ""),
    ("bot_user_id", _OMIT), ("bot_user_id", "328494887"), ("bot_user_id", True),
    ("key_path", 7), ("key_path", ""),
])
def test_a_missing_or_wrongly_typed_field_refuses_naming_the_file_and_field(
        tmp_path, field, bad):
    path = _write_config(tmp_path / "library-app.json", **{field: bad})
    with pytest.raises(KM.Unreadable) as e:
        KM.load_config(path)
    assert str(e.value).startswith(str(path)) and field in str(e.value), e.value


def test_key_path_is_optional_and_defaults_beside_the_file(tmp_path):
    path = _write_config(tmp_path / "sub" / "library-app.json", key_path=_OMIT)
    cfg = KM.load_config(path)
    assert cfg.key_path == path.parent / "library-app.pem"


def test_an_explicit_key_path_is_honoured(tmp_path):
    where = tmp_path / "elsewhere" / "key.pem"
    path = _write_config(tmp_path / "library-app.json", key_path=str(where))
    assert KM.load_config(path).key_path == where


def test_owner_is_derived_from_repo_not_a_second_field(tmp_path):
    path = _write_config(tmp_path / "library-app.json", repo="acme/kb")
    cfg = KM.load_config(path)
    assert cfg.repo == "acme/kb" and cfg.owner == "acme"


def test_every_constant_the_module_used_to_hold_resolves_to_todays_convivy_value():
    """Convivy's behaviour cannot drift: the config fixture (conftest.py) holds
    exactly the values this module hardcoded before issue_01M3SGPX7AMVR2SKREVPWEPKN0,
    and every one of them still resolves to that value through `load_config`."""
    cfg = KM.load_config()
    assert cfg.repo == "convivy/knowledge"
    assert cfg.owner == "convivy"
    assert cfg.app_id == "4923626"
    assert cfg.bot_login == "convivy-library[bot]"
    assert cfg.bot_user_id == 328494887
    assert cfg.key_path.name == "library-app.pem"
    assert KM.push_url() == "https://github.com/convivy/knowledge.git"
    assert KM.commit_name() == "convivy-library[bot]"
    assert KM.commit_email() == "328494887+convivy-library[bot]@users.noreply.github.com"


def test_an_adopters_values_pass_while_convivy_knowledge_is_refused_under_them(tmp_path):
    """A different repo, app id and bot: the repository and author checks judge a
    PR against the CONFIGURED values, never a value convivy/knowledge happens to
    share by coincidence."""
    adopter = KM.LibraryConfig(repo="acme/kb", owner="acme", app_id="1",
                               bot_login="acme-library[bot]", bot_user_id=1,
                               key_path=tmp_path / "key.pem")
    acme_author = {"login": "acme-library[bot]", "id": 1, "type": "Bot"}
    admitted = _facts(repo="acme/kb", author=acme_author)
    assert KM.refusals(admitted, adopter) == []
    why = KM.refusals(_facts(repo=REPO, author=BOT), adopter)
    assert any("is not acme/kb" in w for w in why), why
    why = KM.refusals(_facts(repo="acme/kb", author=BOT), adopter)
    assert any("was opened by" in w and "acme-library[bot]" in w for w in why), why


def test_refusals_with_no_usable_config_reports_one_reason_and_still_runs_the_rest(
        tmp_path, monkeypatch):
    monkeypatch.setattr(KM, "CONFIG_PATH", tmp_path / "missing.json")
    why = KM.refusals(_facts(state="MERGED"))
    assert any("the Library config could not be read" in w for w in why), why
    assert any("not open" in w for w in why), why


# --- the credential -------------------------------------------------------------


@pytest.fixture()
def rsa_key(tmp_path):
    key = tmp_path / "app.pem"
    subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"], check=True,
                   capture_output=True)
    key.chmod(0o600)
    pub = tmp_path / "app.pub"
    subprocess.run(["openssl", "rsa", "-in", str(key), "-pubout", "-out", str(pub)],
                   check=True, capture_output=True)
    return key, pub


def _unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def test_the_app_jwt_verifies_against_the_keys_public_half(rsa_key, tmp_path):
    key, pub = rsa_key
    token = KM.app_jwt(key, app_id="4923626", now=1_000_000)
    header, claims, sig = token.split(".")
    assert json.loads(_unb64(header)) == {"alg": "RS256", "typ": "JWT"}
    assert json.loads(_unb64(claims)) == {"iat": 999_940, "exp": 1_000_540, "iss": "4923626"}
    (tmp_path / "sig").write_bytes(_unb64(sig))
    r = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(pub),
                        "-signature", str(tmp_path / "sig")],
                       input=f"{header}.{claims}".encode(), capture_output=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_a_key_others_can_read_is_refused(rsa_key):
    key, _ = rsa_key
    assert KM.key_problem(key) is None
    key.chmod(0o644)
    assert "must be 600" in KM.key_problem(key)


def test_a_missing_key_says_where_to_put_it(tmp_path):
    assert "Generate one" in KM.key_problem(tmp_path / "nope.pem")


def test_a_symlinked_key_is_refused(rsa_key, tmp_path):
    key, _ = rsa_key
    link = tmp_path / "link.pem"
    link.symlink_to(key)
    assert "not a regular file" in KM.key_problem(link)


class _Resp:
    def __init__(self, status, payload):
        self.status = status
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_the_installation_token_is_scoped_to_knowledge_and_two_permissions():
    seen = {}

    def opener(req, timeout=None):
        seen.update(url=req.full_url, method=req.get_method(),
                    auth=req.get_header("Authorization"), body=json.loads(req.data))
        return _Resp(201, {"token": "ghs_x"})

    assert KM.installation_token("jwt.x.y", "161212648", opener=opener) == "ghs_x"
    assert seen["url"].endswith("/app/installations/161212648/access_tokens")
    assert seen["method"] == "POST" and seen["auth"] == "Bearer jwt.x.y"
    assert seen["body"] == {"repositories": ["knowledge"],
                            "permissions": {"contents": "write", "pull_requests": "write"}}


def _routes(table):
    """An opener answering each (method, url fragment) with a canned (status,
    payload), recording every request as (method, url, auth, json body)."""
    seen = []

    def opener(req, timeout=None):
        seen.append((req.get_method(), req.full_url, req.get_header("Authorization"),
                     json.loads(req.data) if req.data else None))
        for (method, frag), resp in table.items():
            if req.get_method() == method and frag in req.full_url:
                return _Resp(*resp)
        raise AssertionError(f"unexpected {req.get_method()} {req.full_url}")
    return opener, seen


ONE_INSTALL = [{"id": 161212648, "account": {"login": "convivy"}}]
KNOWLEDGE_ONLY = {"total_count": 1, "repositories": [{"full_name": "convivy/knowledge"}]}


def _installation_table(installs=ONE_INSTALL, repos=KNOWLEDGE_ONLY):
    return {("GET", "/app/installations"): (200, installs),
            ("POST", "/access_tokens"): (201, {"token": "ghs_listing"}),
            ("GET", "/installation/repositories"): (200, repos),
            ("DELETE", "/installation/token"): (204, {})}


def test_the_installation_is_derived_and_checked_to_reach_knowledge_alone():
    opener, seen = _routes(_installation_table())
    assert KM.knowledge_installation("jwt.x.y", opener=opener) == "161212648"
    paths = [(m, u.split("api.github.com")[1].split("?")[0]) for m, u, _, _ in seen]
    assert paths == [("GET", "/app/installations"),
                     ("POST", "/app/installations/161212648/access_tokens"),
                     ("GET", "/installation/repositories"),
                     ("DELETE", "/installation/token")]
    # The listing token is minted UNSCOPED, or it could only ever see knowledge
    # and would show nothing about what else the installation reaches.
    assert seen[1][3] is None
    assert seen[2][2] == "Bearer ghs_listing"
    # And it is that token, not the JWT, that is revoked, straight after listing.
    assert seen[3][2] == "Bearer ghs_listing"


def test_the_listing_token_is_revoked_even_when_the_listing_fails():
    """Every permission the installation holds, in a token nobody will use again:
    a refused or failed listing must not leave it live for the hour."""
    table = _installation_table()
    table[("GET", "/installation/repositories")] = (403, {"message": "nope"})
    opener, seen = _routes(table)
    with pytest.raises(KM.Unreadable):
        KM.knowledge_installation("jwt.x.y", opener=opener)
    assert [(m, a) for m, _, a, _ in seen][-1] == ("DELETE", "Bearer ghs_listing")


def test_a_revocation_that_fails_does_not_fail_the_check():
    """The token lives in this process's memory and expires within the hour, so
    a refused or unreachable revoke is no reason to refuse the merge or push."""
    table = _installation_table()
    table[("DELETE", "/installation/token")] = (401, {"message": "Bad credentials"})
    opener, _ = _routes(table)
    assert KM.knowledge_installation("jwt.x.y", opener=opener) == "161212648"

    def down_on_delete(req, timeout=None):
        if req.get_method() == "DELETE":
            raise urllib.error.URLError("nodename nor servname provided")
        return _routes(_installation_table())[0](req, timeout)
    assert KM.knowledge_installation("jwt.x.y", opener=down_on_delete) == "161212648"


@pytest.mark.parametrize("installs, repos, named", [
    (ONE_INSTALL * 2, KNOWLEDGE_ONLY, "count"),
    ([], KNOWLEDGE_ONLY, "count"),
    ([{"id": 1, "account": {"login": "someone-else"}}], KNOWLEDGE_ONLY, "owner"),
    (ONE_INSTALL, {"total_count": 2, "repositories": [{"full_name": "convivy/knowledge"},
                                                      {"full_name": "convivy/library"}]},
     "repo list"),
    (ONE_INSTALL, {"total_count": 1, "repositories": [{"full_name": "convivy/agent-platform"}]},
     "repo list"),
])
def test_a_widened_installation_is_refused_by_name(installs, repos, named):
    """A widened installation must read as what it is, not as a token error."""
    opener, _ = _routes(_installation_table(installs, repos))
    with pytest.raises(KM.Unreadable) as e:
        KM.knowledge_installation("jwt.x.y", opener=opener)
    assert str(e.value).startswith(named + ":"), e.value


def test_the_merge_pins_the_checked_head_and_uses_a_merge_commit():
    seen = {}

    def opener(req, timeout=None):
        seen.update(url=req.full_url, method=req.get_method(), body=json.loads(req.data))
        return _Resp(200, {"merged": True, "sha": "f" * 40})

    ok, msg = KM.merge_pr(7, HEAD, "ghs_x", opener=opener)
    assert ok and msg == "merged as " + "f" * 12
    assert seen["url"].endswith("/repos/convivy/knowledge/pulls/7/merge")
    assert seen["method"] == "PUT"
    assert seen["body"] == {"sha": HEAD, "merge_method": "merge"}


def test_a_merge_github_refuses_is_reported_as_refused():
    def opener(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 409, "Conflict", {},
                                     io.BytesIO(b'{"message": "Head branch was modified."}'))

    ok, msg = KM.merge_pr(7, HEAD, "ghs_x", opener=opener)
    assert not ok and "409" in msg and "Head branch was modified" in msg


# --- the command's own wiring -------------------------------------------------------


class KnowledgeRepo:
    """A knowledge checkout with a bare `origin`, in real git, so the policy and the
    changed files are read exactly as `cmd_kb_merge` reads them. `main` starts with
    a permissive policy and a few documents; `publish_pr` puts a PR head where
    GitHub keeps it (`refs/pull/N/head`)."""

    POLICY = AP.POLICY_PATH
    BASE = {
        POLICY: _policy_text(),
        "projects/x/a.md": "one\n",
        "projects/x/plain.md": "a plain document\n",
        "projects/x/agents/old.md": "an agent's instructions\n",
        "projects/x/skills/s.md": "a skill's instructions\n",
        "projects/x/moving.md": "text that a rename carries along unchanged\n",
        "agents/old.md": "---\nkind: agent\ntitle: t\n---\nbody\n",   # an instruction outside projects/
    }

    def __init__(self, tmp_path):
        self.bare, self.path = tmp_path / "origin.git", tmp_path / "knowledge"
        self.author_clone = tmp_path / "author"   # where PR heads are made
        self.git("init", "-q", "--bare", "-b", "main", str(self.bare), cwd=tmp_path)
        self.git("init", "-q", "-b", "main", str(self.path), cwd=tmp_path)
        self.git("remote", "add", "origin", str(self.bare))
        for k, v in (("user.name", "t"), ("user.email", "t@example.com"),
                     ("commit.gpgsign", "false")):
            self.git("config", k, v)
        self.publish_main(self.BASE)
        self.head = None

    def git(self, *a, cwd=None):
        r = _REAL_RUN(["git", *a], cwd=cwd or self.path, capture_output=True, text=True)
        assert r.returncode == 0, f"git {a}: {r.stderr}"
        return r.stdout.strip()

    def commit(self, files, msg="x", root=None):
        """`files`: path -> text | bytes | None (delete) | (mode, text) | ("120000", target)."""
        root = root or self.path
        for rel, val in files.items():
            f = root / rel
            if val is None:
                f.unlink()
                continue
            f.parent.mkdir(parents=True, exist_ok=True)
            if f.is_symlink() or f.exists():
                f.unlink()
            if isinstance(val, tuple) and val[0] == "120000":
                f.symlink_to(val[1])
                continue
            mode, body = val if isinstance(val, tuple) else ("100644", val)
            f.write_bytes(body if isinstance(body, bytes) else body.encode())
            f.chmod(0o755 if mode == "100755" else 0o644)
        self.git("add", "-A", cwd=root)
        self.git("commit", "-q", "--allow-empty", "-m", msg, cwd=root)
        return self.git("rev-parse", "HEAD", cwd=root)

    def publish_main(self, files):
        if _REAL_RUN(["git", "-C", str(self.path), "rev-parse", "-q", "--verify", "main"],
                     capture_output=True).returncode == 0:   # not the first commit
            self.git("checkout", "-q", "main")
        sha = self.commit(files, "main")
        self.git("push", "-q", "origin", "main")   # also moves refs/remotes/origin/main
        return sha

    def publish_pr(self, files, number=7):
        """A PR head off the bare origin's main, at `refs/pull/N/head` there. It is
        made in a second clone, so the checkout does not have it until it fetches."""
        return self.publish_pr_steps([files], number)[-1]

    def publish_pr_steps(self, steps, number=7):
        """One commit per entry of `steps` on a PR branch off the bare origin's main,
        the last being the head at `refs/pull/N/head`. Returns the shas in order."""
        if not self.author_clone.exists():
            self.git("clone", "-q", str(self.bare), str(self.author_clone),
                     cwd=self.author_clone.parent)
            for k, v in (("user.name", "t"), ("user.email", "t@example.com"),
                         ("commit.gpgsign", "false")):
                self.git("config", k, v, cwd=self.author_clone)
        work = self.author_clone
        self.git("fetch", "-q", "origin", "main", cwd=work)
        self.git("checkout", "-q", "-B", "pr-branch", "origin/main", cwd=work)
        shas = [self.commit(files, "pr", root=work) for files in steps]
        self.git("push", "-q", "-f", "origin", f"HEAD:refs/pull/{number}/head", cwd=work)
        self.head = shas[-1]
        return shas

    def pin(self, sha, number=7):
        """Fetch the head to where `read_git_facts` looks, as the command does."""
        self.git("fetch", "-q", "origin", f"+refs/pull/{number}/head:{KM.pr_ref(number)}")
        assert self.git("rev-parse", KM.pr_ref(number)) == sha

    def facts(self, files, policy_files=None, number=7, rounds=1, **over):
        """Publish `files` as a PR, read it as the command does and decide. `rounds`
        is the reviewer rounds stamped at the head, with one security-reviewer round."""
        if policy_files is not None:
            self.publish_main(policy_files)
        sha = self.publish_pr(files, number)
        return self.read(sha, number=number, rounds=rounds, **over)

    def read(self, sha, number=7, rounds=1, stamps=None, **over):
        """Read the PR at `sha` as the command does. Without `stamps`, the stamps are
        `rounds` reviewer rounds and one security round, all at `sha`."""
        self.pin(sha, number)
        policy, changes, base_sha, head_contains_base = KM.read_git_facts(self.path, number, sha)
        if stamps is None:
            stamps = _spread([(sha, rounds)])
        return _facts(policy=policy, changes=changes, head_sha=sha, gate_head=sha,
                      base_sha=base_sha, head_contains_base=head_contains_base, stamps=stamps,
                      stamp_changes=KM.read_stamp_changes(self.path, base_sha, stamps), **over)


@pytest.fixture
def krepo(tmp_path):
    return KnowledgeRepo(tmp_path)


@pytest.fixture
def kb(krepo, agentctl, monkeypatch, library_identity):
    """`krepo` as the command's knowledge checkout, with the App identity stubbed
    to fetch from its bare origin. A default PR (one new document) is published."""
    monkeypatch.setattr(agentctl, "KNOWLEDGE_ROOT", krepo.path)
    krepo.publish_pr({"projects/x/new.md": "hello\n"})
    krepo.identity = library_identity
    return krepo


def _wire(agentctl, monkeypatch, kb, *, head, gate_head=None, gate_ok=True, gate_reasons=(),
          stamps=None, messages=(f"docs: x\n\n{TRAILER}\n",), dry_run=True, author=BOT):
    """Drive `cmd_kb_merge` against canned GitHub reads and the real git of `kb`.
    `head` is what this command's OWN first read of the PR returns for
    headRefOid (used for the author/commits/companions reads); `gate_head`
    (default `head`) is the head the review gate's OWN, separately-stubbed
    decision reports, so a push landing between the two is one differing.
    `stamps` (default two fresh APPROVEs at `gate_head`) is what that decision
    reports as the PR's authenticated review stamps — `agentctl._review_status_decision`
    is stubbed directly rather than its own GitHub reads, since CI/draft/
    mergeability are not this command's concern and every real caller of it
    is exercised by review-status's own test file. Returns the exit code and
    the ordered calls to the review decision: `(pr, repo, required)` each."""
    import argparse
    gate_head = head if gate_head is None else gate_head
    if stamps is None:
        stamps = [_stamp("reviewer", "run-00000001", commit=gate_head),
                 _stamp("security-reviewer", "run-00000002", commit=gate_head)]
    view = {"number": 7, "state": "OPEN", "headRefName": "kb/docs-x-1234",
            "baseRefName": "main", "isCrossRepository": False, "body": "why\n"}

    def fake_pr_json(pr, fields, repo=None):
        return {**view, "headRefOid": head}

    calls = []

    def fake_run(cmd, **kw):
        if cmd[0] == "git":
            return _REAL_RUN(cmd, **kw)
        if cmd[:2] == ["gh", "api"] and "/commits" in cmd[3 if "--paginate" in cmd else 2]:
            out = "".join(json.dumps({"sha": f"{i:040x}", "commit": {"message": m}}) + "\n"
                          for i, m in enumerate(messages))
            return subprocess.CompletedProcess(cmd, 0, out, "")
        if cmd[:2] == ["gh", "api"] and cmd[2].endswith("/pulls/7"):
            if author is None:
                return subprocess.CompletedProcess(cmd, 1, "", "boom")
            return subprocess.CompletedProcess(cmd, 0, json.dumps(author), "")
        raise AssertionError(f"unexpected command {cmd}")

    def fake_review(pr_, repo_, required, **kw):
        calls.append((pr_, repo_, required))
        return agentctl.ReviewStatusResult(ok=gate_ok, head=gate_head,
                                           reasons=list(gate_reasons), stamps=stamps)

    def never_merge(*a, **k):
        raise AssertionError("merge_pr reached")

    monkeypatch.setattr(agentctl, "_gh_pr_json", fake_pr_json)
    monkeypatch.setattr(agentctl.subprocess, "run", fake_run)
    monkeypatch.setattr(agentctl, "_review_status_decision", fake_review)
    if dry_run:
        monkeypatch.setattr(KM, "merge_pr", never_merge)
    args = argparse.Namespace(pr=7, repo=None, dry_run=dry_run)
    return agentctl.cmd_kb_merge(args), calls


def test_the_command_admits_a_clean_pr_on_dry_run_and_runs_the_gate_separately(
        agentctl, monkeypatch, capsys, kb):
    rc, calls = _wire(agentctl, monkeypatch, kb, head=kb.head)
    assert rc == 0, capsys.readouterr().out
    assert "every condition holds" in capsys.readouterr().out
    assert calls == [("7", "convivy/knowledge", ["reviewer", "security-reviewer"])]


def test_the_command_refuses_when_a_push_lands_while_the_gate_runs(
        agentctl, monkeypatch, capsys, kb):
    rc, _ = _wire(agentctl, monkeypatch, kb, head=kb.head, gate_head="b" * 40)
    out = capsys.readouterr().out
    assert rc == 1
    assert "head moved" in out
    assert "not the bbbbbbbbbbbb the review gate judged" in out  # the fetched head is not that head
    assert kb.git("for-each-ref", "refs/kb-merge") == ""   # the ref goes on a refusal too


def test_the_command_refuses_a_failing_gate_and_a_hand_built_commit(
        agentctl, monkeypatch, capsys, kb):
    """Both reviewers are already fresh at the head (no carry is even
    attempted), so the gate's own unrelated reason is what refuses — a
    carried approval could never rescue this."""
    rc, _ = _wire(agentctl, monkeypatch, kb, head=kb.head, gate_ok=False,
                  gate_reasons=["required CI is not green"],
                  messages=(f"docs: x\n\n{TRAILER}\n", "fixup by hand\n"))
    out = capsys.readouterr().out
    assert rc == 1
    assert "required CI is not green" in out and "without a KB-Write trailer" in out


def test_a_stale_stamp_with_no_earlier_approve_to_carry_still_refuses(
        agentctl, monkeypatch, capsys, kb):
    """The gate's only complaint is the stale stamp itself, and there is no
    OTHER authenticated APPROVE for that role to try carrying — carry must
    not invent an admission out of nothing."""
    rc, _ = _wire(
        agentctl, monkeypatch, kb, head=kb.head, gate_ok=False,
        gate_reasons=[f"security-reviewer APPROVE is stale — reviewed {'d' * 40}, "
                      f"head is {kb.head} (re-review needed)"],
        stamps=[_stamp("reviewer", "run-00000001", commit=kb.head),
               _stamp("security-reviewer", "run-00000002", commit="d" * 40)])
    out = capsys.readouterr().out
    assert rc == 1
    # "d"*40 was never fetched into the checkout, so the carry attempt itself
    # is an unreadable git state — refused the same way any other git failure
    # in the checkout is, never read as "this one just doesn't carry".
    assert "the knowledge checkout could not be read" in out
    assert "not in" in out and "object store" in out


def test_the_command_mints_its_token_for_the_installation_it_derived(
        agentctl, monkeypatch, capsys, kb):
    """The installation is derived and checked before any merge token exists,
    and the token is minted for that installation, not a stored one."""
    calls = []
    monkeypatch.setattr(KM, "key_problem", lambda *a, **k: None)
    monkeypatch.setattr(KM, "app_jwt", lambda *a, **k: "jwt.x.y")
    monkeypatch.setattr(KM, "knowledge_installation",
                        lambda jwt, **k: calls.append(("derive", jwt)) or "161212648")
    monkeypatch.setattr(KM, "installation_token",
                        lambda jwt, inst, **k: calls.append(("mint", jwt, inst)) or "ghs_scoped")
    monkeypatch.setattr(KM, "merge_pr",
                        lambda n, sha, tok, **k: calls.append(("merge", n, sha, tok))
                        or (True, "merged as abc"))
    rc, _ = _wire(agentctl, monkeypatch, kb, head=kb.head, dry_run=False)
    assert rc == 0, capsys.readouterr()
    assert calls == [("derive", "jwt.x.y"), ("mint", "jwt.x.y", "161212648"),
                     ("merge", 7, kb.head, "ghs_scoped")]


def test_the_command_refuses_a_pr_another_account_opened(agentctl, monkeypatch, capsys, kb):
    other = {"login": "operator-account", "id": 42, "type": "User"}
    rc, _ = _wire(agentctl, monkeypatch, kb, head=kb.head, author=other)
    assert rc == 1 and "was opened by 'operator-account'" in capsys.readouterr().out


def test_the_command_refuses_when_the_author_cannot_be_read(agentctl, monkeypatch, capsys, kb):
    rc, _ = _wire(agentctl, monkeypatch, kb, head=kb.head, author=None)
    assert rc == 1 and "was opened by None" in capsys.readouterr().out


def test_the_command_fetches_origin_main_first_and_reads_the_fresh_policy(
        agentctl, monkeypatch, capsys, kb):
    """The checkout's `origin/main` is left at a permissive policy while the
    remote's main has moved to a restrictive one. Reading the checkout as it was
    would admit; only a fetch before the read refuses."""
    permissive = kb.git("rev-parse", "refs/remotes/origin/main")
    kb.publish_main({kb.POLICY: _policy_text(knowledge=False, instructions=False)})
    kb.git("update-ref", "refs/remotes/origin/main", permissive)
    rc, _ = _wire(agentctl, monkeypatch, kb, head=kb.head)
    assert rc == 1
    assert "agents_may_merge_knowledge to false" in capsys.readouterr().out


def test_the_command_reads_the_pr_head_it_fetched_and_drops_the_ref(agentctl, monkeypatch, kb):
    """The PR head is not in the checkout until the command fetches it, and the
    ref it fetched to does not outlive the command."""
    assert _REAL_RUN(["git", "-C", str(kb.path), "cat-file", "-e", kb.head]).returncode != 0
    rc, _ = _wire(agentctl, monkeypatch, kb, head=kb.head)
    assert rc == 0
    assert [c[3] for c in kb.identity.calls if c[0] == "fetch"] == [
        KM.MAIN_REFSPEC, "+refs/pull/7/head:refs/kb-merge/pr-7"]  # main first, then the head
    assert kb.git("for-each-ref", "refs/kb-merge") == ""


def test_a_policy_rewritten_on_the_prs_head_is_ignored(agentctl, monkeypatch, capsys, kb):
    """Main forbids instructions. The PR's head rewrites the file to permit them;
    the command still decides from main's, and the policy edit is a refusal of
    its own."""
    kb.publish_main({kb.POLICY: _policy_text(knowledge=True, instructions=False)})
    head = kb.publish_pr({kb.POLICY: _policy_text(rounds=9), "projects/x/new.md": "hi\n"})
    rc, _ = _wire(agentctl, monkeypatch, kb, head=head)
    out = capsys.readouterr().out
    assert rc == 1
    assert "agents_may_merge_knowledge_instructions to false" in out
    assert "no agent merges a change there" in out


def test_a_policy_added_only_on_the_prs_head_is_ignored(agentctl, monkeypatch, capsys, kb):
    kb.publish_main({kb.POLICY: None})
    head = kb.publish_pr({kb.POLICY: _policy_text(), "projects/x/new.md": "hi\n"})
    rc, _ = _wire(agentctl, monkeypatch, kb, head=head)
    out = capsys.readouterr().out
    assert rc == 1
    assert "the policy file is missing at origin/main" in out


@pytest.mark.parametrize("how", ["tag", "branch"])
def test_a_tag_or_local_branch_named_origin_main_does_not_supply_the_policy(
        agentctl, monkeypatch, capsys, kb, how):
    """`git show origin/main:f` would read a tag or branch of that name before the
    remote-tracking ref. The permissive commit is what that name points at."""
    permissive = kb.git("rev-parse", "main")
    kb.publish_main({kb.POLICY: _policy_text(knowledge=False, instructions=False)})
    if how == "tag":
        kb.git("tag", "origin/main", permissive)
    else:
        kb.git("branch", "origin/main", permissive)
    head = kb.publish_pr({"projects/x/new.md": "hi\n"})
    rc, _ = _wire(agentctl, monkeypatch, kb, head=head)
    assert rc == 1
    assert "agents_may_merge_knowledge to false" in capsys.readouterr().out


def test_a_checkout_that_cannot_fetch_refuses(agentctl, monkeypatch, capsys, kb):
    kb.git("remote", "set-url", "origin", str(kb.path / "nowhere.git"))
    rc, _ = _wire(agentctl, monkeypatch, kb, head=kb.head)
    assert rc == 1
    assert "the knowledge checkout could not be read: could not fetch" in capsys.readouterr().out
    assert kb.git("for-each-ref", "refs/kb-merge") == ""
    # The head fetch is not attempted once the main fetch has failed.
    assert [c[3] for c in kb.identity.calls if c[0] == "fetch"] == [KM.MAIN_REFSPEC]


def test_a_command_without_the_app_identity_refuses_before_reading_anything(
        agentctl, monkeypatch, capsys, kb):
    """Un-stubbed, the suite's fixture points the key at nothing (conftest)."""
    monkeypatch.setattr(agentctl, "_kb_library", kb.identity.real)
    rc, _ = _wire(agentctl, monkeypatch, kb, head=kb.head)
    assert rc == 2
    assert "cannot obtain the convivy-library App identity" in capsys.readouterr().err


# --- carrying an earlier APPROVE onto a rebuilt head (platform/decisions/0078) --


def test_a_rebuild_with_identical_blobs_onto_a_moved_main_admits_on_carried_stamps(
        agentctl, monkeypatch, capsys, kb):
    """The PR was approved at S; another PR then merged, moving main, and `kb
    propose` rebuilt this one onto the new main as H — same diff, new sha.
    `review-status` still says stale (unchanged, by design); the carried
    APPROVE is what admits."""
    s = kb.head
    # A prior review's dispatch (or a prior `kb merge` attempt) fetched the PR
    # while it was still at S — the only way S's commit object can still be in
    # the checkout once the rebuild below force-pushes the branch past it.
    kb.pin(s)
    kb.publish_main({"projects/unrelated/y.md": "a different document\n"})
    # Rebuild the PR branch onto the moved main, in the author's clone, the way
    # `kb propose` would (same diff content, new parent, new sha).
    work = kb.author_clone
    kb.git("fetch", "-q", "origin", "main", cwd=work)
    kb.git("checkout", "-q", "pr-branch", cwd=work)
    kb.git("rebase", "-q", "origin/main", cwd=work)
    h = kb.git("rev-parse", "HEAD", cwd=work)
    kb.git("push", "-q", "-f", "origin", "HEAD:refs/pull/7/head", cwd=work)
    kb.head = h
    rc, _ = _wire(
        agentctl, monkeypatch, kb, head=h, gate_ok=False,
        gate_reasons=[f"reviewer APPROVE is stale — reviewed {s}, head is {h} "
                      f"(re-review needed)",
                      f"security-reviewer APPROVE is stale — reviewed {s}, head is {h} "
                      f"(re-review needed)"],
        stamps=[_stamp("reviewer", "run-00000001", commit=s),
               _stamp("security-reviewer", "run-00000002", commit=s)])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert f"reviewer's APPROVE at {s[:12]} carries to {h[:12]}" in out
    assert f"security-reviewer's APPROVE at {s[:12]} carries to {h[:12]}" in out
    assert "every condition holds" in out


def test_a_byte_changed_at_the_rebuilt_head_is_not_carried(agentctl, monkeypatch, capsys, kb):
    s = kb.head
    kb.pin(s)
    kb.publish_main({"projects/unrelated/y.md": "a different document\n"})
    work = kb.author_clone
    kb.git("fetch", "-q", "origin", "main", cwd=work)
    kb.git("checkout", "-q", "pr-branch", cwd=work)
    kb.git("rebase", "-q", "origin/main", cwd=work)
    kb.commit({"projects/x/new.md": "hello, but different\n"}, "touch it up", root=work)
    h = kb.git("rev-parse", "HEAD", cwd=work)
    kb.git("push", "-q", "-f", "origin", "HEAD:refs/pull/7/head", cwd=work)
    rc, _ = _wire(
        agentctl, monkeypatch, kb, head=h, gate_ok=False,
        gate_reasons=[f"reviewer APPROVE is stale — reviewed {s}, head is {h} "
                      f"(re-review needed)",
                      f"security-reviewer APPROVE is stale — reviewed {s}, head is {h} "
                      f"(re-review needed)"],
        stamps=[_stamp("reviewer", "run-00000001", commit=s),
               _stamp("security-reviewer", "run-00000002", commit=s)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "no carried APPROVE from reviewer" in out
    assert "projects/x/new.md differs" in out


def test_an_unauthenticated_approve_never_carries(agentctl, monkeypatch, capsys, kb):
    """The stamp that would carry never reached `review-status`'s authenticated
    stamps at all (a forged comment, or one ARCH-193 rejected) — the gate must
    see NO candidate for that role, not a candidate it declines. `security-reviewer`
    is already fresh at the head, so only `reviewer` needs a carry attempt."""
    rc, _ = _wire(
        agentctl, monkeypatch, kb, head=kb.head, gate_ok=False,
        gate_reasons=["no review posted by reviewer"],
        stamps=[_stamp("security-reviewer", "run-00000002", commit=kb.head)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "no carried APPROVE from reviewer: no stamp from this role to carry" in out


# --- routing to `review-status`'s own decision (platform/decisions/0090) --------
#
# Round 1 of agent-platform#840, finding 2 (security-reviewer): `cmd_kb_merge`
# must decide from the exact same source `cmd_review_status` would for this
# PR — comment stamps until the launcher is installed, GitHub check-runs once
# it is — never the comment-stamp gate unconditionally just because this
# module's carry logic needs stamps to search.


def test_the_command_decides_from_checks_once_the_launcher_is_installed_and_never_carries(
        agentctl, monkeypatch, capsys, kb, tmp_path):
    """The carry (platform/decisions/0078) is defined over an APPROVE stamp,
    which the checks decision never produces (`stamps=[]`), so no carry is
    even attempted — `carry_for_role` must not run at all."""
    launcher = tmp_path / "review-launch"
    launcher.write_text("x")
    launcher.chmod(0o755)
    monkeypatch.setattr(agentctl, "_review_launch_path", lambda: launcher)

    checks_calls = []

    def fake_checks(pr_, repo_, required):
        checks_calls.append((pr_, repo_, required))
        return agentctl.ReviewStatusResult(ok=True, head=kb.head, reasons=[], stamps=[])
    monkeypatch.setattr(agentctl, "_review_status_from_checks", fake_checks)

    def _boom(*a, **k):
        raise AssertionError("carry_for_role ran: nothing to carry once review-status "
                             "decides from checks, not comment stamps")
    monkeypatch.setattr(KM, "carry_for_role", _boom)

    rc, decision_calls = _wire(agentctl, monkeypatch, kb, head=kb.head)
    out = capsys.readouterr().out
    assert rc == 0, out
    assert checks_calls == [("7", "convivy/knowledge", ["reviewer", "security-reviewer"])]
    assert decision_calls == []   # `_review_status_decision` never ran


def test_the_command_keeps_reading_comment_stamps_when_the_launcher_is_absent(
        agentctl, monkeypatch, capsys, kb):
    """The routing's other side: with no launcher installed, `kb merge` must
    keep deciding from `_review_status_decision`, never the checks-only one —
    pinning this stops a future change from flipping the default."""
    monkeypatch.setattr(agentctl, "_review_launch_path", lambda: None)

    def _boom(*a, **k):
        raise AssertionError("the checks-based decision must not run without the launcher")
    monkeypatch.setattr(agentctl, "_review_status_from_checks", _boom)

    rc, decision_calls = _wire(agentctl, monkeypatch, kb, head=kb.head)
    out = capsys.readouterr().out
    assert rc == 0, out
    assert decision_calls == [("7", "convivy/knowledge", ["reviewer", "security-reviewer"])]


# --- a copied stamp with a flipped verdict must not reach this command's own gate or
# round cap (round 8 of agent-platform#785, security-reviewer run-d414d011) ------------
#
# Unlike the tests above, `_review_status_decision` runs for real here, never stubbed:
# the point is that `cmd_kb_merge` does no authentication of its own — its round cap
# (`rounds_by_role`, `_round_cap`) and its `review_ok` both read exactly the stamps
# `_authenticated_stamps` (CQ·399, `bin/agentctl`) hands back, so the forgery is
# closed once, at that one choke point, for every reader downstream.


def test_a_copied_approve_with_a_flipped_verdict_is_not_counted_and_the_command_refuses(
        agentctl, monkeypatch, capsys, kb):
    """The attack: `reviewer`'s real run posted REQUEST_CHANGES; a commenter copies
    that stamp's header (its run-id and reviewed-commit, both visible in the thread)
    and reposts it with `verdict: APPROVE`. `_authenticated_stamps` must drop the copy
    because its verdict does not match the run's posted record — proved directly below
    — so `cmd_kb_merge`, which never re-checks a stamp's verdict itself, still reads
    `reviewer` as REQUEST_CHANGES and refuses rather than admits."""
    import argparse
    head = kb.head
    facts = {"run-rc": agentctl.RP.RunFacts(found=True, agent="reviewer", repo="convivy/knowledge",
                                            review_sha=head, posted_verdict="REQUEST_CHANGES",
                                            posted_pr="7"),
             "run-sec": agentctl.RP.RunFacts(found=True, agent="security-reviewer",
                                             repo="convivy/knowledge", review_sha=head,
                                             posted_verdict="APPROVE", posted_pr="7")}
    monkeypatch.setattr(agentctl, "_resolve_run_facts",
                        lambda rid: facts.get(rid, agentctl.RP.RunFacts(found=False)))
    genuine = {"body": agentctl.RP.render_header(
        pr="7", repo="convivy/knowledge", reviewed_commit=head, verdict="REQUEST_CHANGES",
        agent="reviewer", run_id="run-rc")}
    copied = {"body": agentctl.RP.render_header(
        pr="7", repo="convivy/knowledge", reviewed_commit=head, verdict="APPROVE",
        agent="reviewer", run_id="run-rc")}
    sec_stamp = {"body": agentctl.RP.render_header(
        pr="7", repo="convivy/knowledge", reviewed_commit=head, verdict="APPROVE",
        agent="security-reviewer", run_id="run-sec")}
    view = {"number": 7, "state": "OPEN", "headRefName": "kb/docs-x-1234",
            "baseRefName": "main", "isCrossRepository": False, "body": "why\n",
            "headRefOid": head, "comments": [genuine, copied, sec_stamp],
            "statusCheckRollup": [{"conclusion": "SUCCESS"}], "isDraft": False,
            "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN"}

    # The direct proof: the stamps `_authenticated_stamps` hands to every reader
    # (the round cap and the gate alike) hold only the genuine REQUEST_CHANGES for
    # `reviewer` — the copy never reaches that list at all.
    stamps = agentctl._authenticated_stamps(view, allow_unverified=False,
                                            repo="convivy/knowledge", pr="7")
    reviewer_stamps = [s for s in stamps if s.agent == "reviewer"]
    assert [s.verdict for s in reviewer_stamps] == ["REQUEST_CHANGES"], (
        "the copied APPROVE must not be counted alongside the genuine stamp")
    out = capsys.readouterr().out
    assert "verdict" in out  # _authenticated_stamps named why the copy was dropped

    def fake_pr_json(pr, fields, repo=None):
        return view

    def fake_run(cmd, **kw):
        if cmd[0] == "git":
            return _REAL_RUN(cmd, **kw)
        if cmd[:2] == ["gh", "api"] and "/commits" in cmd[3 if "--paginate" in cmd else 2]:
            out = json.dumps({"sha": "c" * 40, "commit": {"message": f"docs: x\n\n{TRAILER}\n"}}) + "\n"
            return subprocess.CompletedProcess(cmd, 0, out, "")
        if cmd[:2] == ["gh", "api"] and cmd[2].endswith("/pulls/7"):
            return subprocess.CompletedProcess(cmd, 0, json.dumps(BOT), "")
        raise AssertionError(f"unexpected command {cmd}")

    def never_merge(*a, **k):
        raise AssertionError("merge_pr reached: the forged stamp must not admit")

    monkeypatch.setattr(agentctl, "_gh_pr_json", fake_pr_json)
    monkeypatch.setattr(agentctl.subprocess, "run", fake_run)
    monkeypatch.setattr(agentctl, "_review_launch_path", lambda: None)
    monkeypatch.setattr(KM, "merge_pr", never_merge)

    rc = agentctl.cmd_kb_merge(argparse.Namespace(pr=7, repo=None, dry_run=True))
    out = capsys.readouterr().out
    assert rc == 1
    assert "no carried APPROVE from reviewer" in out
    assert "REQUEST_CHANGES, not APPROVE" in out


# --- the policy and the files, read from git -----------------------------------------

POLICY_PATH = "projects/platform/policy/agent-policy.yaml"


def _refused(krepo, files, **kw):
    return krepo.facts(files, **kw)


def _agent_doc(kind="agent"):
    return f"---\nkind: {kind}\ntitle: t\n---\nbody\n"


def test_a_missing_policy_refuses_with_the_reason(krepo):
    krepo.publish_main({POLICY_PATH: None})
    why = KM.refusals(krepo.facts({"projects/x/new.md": "hi\n"}))
    assert len(why) == 1 and "the policy file is missing at origin/main" in why[0], why
    assert "the agent policy fell back" in why[0]


@pytest.mark.parametrize("text, reason", [
    (_policy_text().replace("true", "yes", 1), "not exactly true or false"),
    (_policy_text() + "extra_key: 1\n", "is not a key of schema version 1"),
    (_policy_text(rounds=5).replace("review_rounds_per_role: 5\n", ""),
     "has no line for review_rounds_per_role"),
])
def test_a_malformed_policy_refuses_with_the_reason(krepo, text, reason):
    krepo.publish_main({POLICY_PATH: text})
    why = KM.refusals(krepo.facts({"projects/x/new.md": "hi\n"}))
    assert len(why) == 1 and reason in why[0], why


def test_a_new_document_passes_and_reports_nothing(krepo):
    assert KM.refusals(krepo.facts({"projects/x/new.md": "hi\n"})) == []


@pytest.mark.parametrize("files", [
    {POLICY_PATH: _policy_text(rounds=9)},                                  # an edit
    {"projects/platform/policy/extra.md": "hello\n"},                       # an addition
    {POLICY_PATH: None},                                                   # a deletion
])
def test_a_pr_touching_the_policy_directory_refuses_with_both_keys_true(krepo, files):
    why = KM.refusals(krepo.facts(files))
    assert any("no agent merges a change there" in w for w in why), why
    assert not any("agents_may_merge" in w for w in why), why


@pytest.mark.parametrize("path", ["projects/Platform/Policy/x.md", "PROJECTS/PLATFORM/POLICY/x.md",
                                  "projects/platform/policy"])
def test_the_policy_directory_is_banned_in_any_letter_case(path):
    """Given as `Change`s, since a case-insensitive disk (this machine's) files
    `projects/Platform/Policy/x.md` into the existing directory."""
    why = KM.refusals(_facts(changes=[_change(path)]))
    assert any("no agent merges a change there" in w for w in why), why
    assert not any("agents_may_merge" in w for w in why), why  # both keys are true


def test_a_look_alike_of_projects_is_not_projects():
    """U+017F folds to `s` under casefold, so `Projectſ/` would pass as `projects/`
    if the grant compared folded names; given as a `Change` because a
    case-insensitive disk files it into the real `projects/` directory."""
    why = KM.refusals(_facts(changes=[_change("Projectſ/x/a.md")]))
    assert len(why) == 1 and "not a knowledge document" in why[0], why


def test_a_rename_into_or_out_of_the_policy_directory_refuses(krepo):
    into = krepo.facts({"projects/x/moving.md": None,
                        "projects/platform/policy/moving.md": krepo.BASE["projects/x/moving.md"]})
    assert any("moving.md -> projects/platform/policy/moving.md" in w
               and "no agent merges" in w for w in KM.refusals(into))
    out = krepo.facts({POLICY_PATH: None, "projects/x/policy-copy.md": krepo.BASE[POLICY_PATH]})
    assert any("no agent merges a change there" in w for w in KM.refusals(out))


@pytest.mark.parametrize("files", [
    {"projects/x/agents/new.md": "instructions\n"},                        # an agents/ path
    {"projects/x/new.md": _agent_doc("agent")},                            # kind: agent
    {"projects/x/link.md": ("120000", "plain.md")},                        # a symlink
], ids=["agents-path", "kind-agent", "symlink"])
def test_an_instruction_needs_the_instructions_key_and_no_other(krepo, files):
    def why(**kw):
        return KM.refusals(krepo.facts(files, policy_files={POLICY_PATH: _policy_text(**kw)}))
    # The plain-knowledge key being true does not admit an instruction.
    refused = why(knowledge=True, instructions=False)
    assert len(refused) == 1 and "agents_may_merge_knowledge_instructions to false" in refused[0]
    assert "a changed file is an instruction" in refused[0]
    # The instructions key alone does.
    assert why(knowledge=False, instructions=True) == []


def test_a_plain_document_needs_the_knowledge_key_and_no_other(krepo):
    files = {"projects/x/new.md": "hi\n"}
    refused = KM.refusals(krepo.facts(
        files, policy_files={POLICY_PATH: _policy_text(knowledge=False, instructions=True)}))
    assert len(refused) == 1 and "agents_may_merge_knowledge to false" in refused[0]
    assert "instruction" not in refused[0]
    assert KM.refusals(krepo.facts(
        files, policy_files={POLICY_PATH: _policy_text(knowledge=True, instructions=False)})) == []


# The round cap by review floor (platform/decisions/0083 and the review round cap). The
# policy's numbers are distinct, 9 for documents and 13 for code, so a cap read from the
# wrong key cannot pass. Twelve rounds sit inside 13 and past 9; fourteen sit past both.

CAP_POLICY = dict(knowledge=True, instructions=True, rounds=9, code=13)
DECISION_FOR_CODER = "---\nkind: decision\naudience: [team, coder]\ntitle: t\n---\nbody\n"


def _spread(plan):
    """Reviewer rounds from `plan`: each entry is (text, count) or (text, count, pinned). Each
    round is a distinct run whose stamp reads `text` and whose run pinned `pinned` (default:
    `text`). One security-reviewer round follows, at the last entry."""
    def parts(entry):
        text, count = entry[0], entry[1]
        return text, count, (entry[2] if len(entry) > 2 else None)

    out = []
    for i, entry in enumerate(plan):
        text, count, pinned = parts(entry)
        out += [_stamp("reviewer", f"run-{i}-{j:05d}", commit=text, pinned=pinned)
                for j in range(count)]
    text, _, pinned = parts(plan[-1])
    out.append(_stamp("security-reviewer", "run-000000aa", commit=text, pinned=pinned))
    return out


def _round_refusals(krepo, files, extra, n):
    policy_files = {POLICY_PATH: _policy_text(**CAP_POLICY), **extra}
    return KM.refusals(krepo.facts(files, policy_files=policy_files, rounds=n))


@pytest.mark.parametrize("files, extra, in_floor, names", [
    ({"projects/x/new.md": _agent_doc("agent")}, {}, True,
     "projects/x/new.md has kind agent"),                                   # an added agent file
    ({"projects/x/rb.md": None}, {"projects/x/rb.md": _agent_doc("runbook")}, True,
     "projects/x/rb.md has kind runbook"),                                  # a deleted runbook
    ({"projects/x/skills/s.md": None, "projects/x/s.md": "a skill's instructions\n"}, {}, True,
     "projects/x/skills/s.md sits under an agent-instruction directory"),    # a rename out of skills/
    ({"projects/x/rb.md": _agent_doc("reference")}, {"projects/x/rb.md": _agent_doc("runbook")}, True,
     "projects/x/rb.md has kind runbook"),                                  # kind runbook -> reference
    ({"projects/platform/decisions/0001-x.md": DECISION_FOR_CODER}, {}, True,
     "projects/platform/decisions/0001-x.md is a decision whose audience names coder"),
    ({"projects/x/new.md": "hi\n"}, {}, False, "no changed file is in the review floor"),  # a plain document
    ({"projects/x/bin.md": "now text\n"}, {"projects/x/bin.md": b"\xff\xfe\n"}, False,
     "projects/x/bin.md cannot be read"),                                   # an unreadable base side
], ids=["added-agent", "deleted-runbook", "renamed-out-of-skills", "kind-runbook-to-reference",
        "decision-naming-a-role", "plain-document", "unreadable-base"])
def test_a_pr_takes_the_code_cap_only_when_the_review_floor_places_it(
        krepo, files, extra, in_floor, names):
    twelve = _round_refusals(krepo, files, extra, 12)
    if in_floor:
        assert not any("past the cap" in w for w in twelve), twelve
    else:
        assert any("past the cap of 9" in w and "review_rounds_per_role (9)" in w
                   for w in twelve), twelve
    fourteen = _round_refusals(krepo, files, extra, 14)
    cap = 13 if in_floor else 9
    capped = [w for w in fourteen if "past the cap" in w]
    assert len(capped) == 1 and f"reviewer ran 14 rounds, past the cap of {cap}" in capped[0], fourteen
    assert names in capped[0], fourteen


# The cap is the lowest at the current head and at every head a stamp names. Example Person,
# 2026-10-05 at about 23:45 MST: "fix that at doc-level numbers (7), no way to buy more rounds
# by changing scope". These tests use the shipped numbers, 7 for documents and 11 for code, so
# the round numbers they assert are the ones the rule states.

SHIPPED = dict(knowledge=True, instructions=True, rounds=7, code=11)


def test_a_documents_only_head_keeps_the_document_cap_when_a_later_head_adds_a_runbook(krepo):
    """Seven rounds at a documents-only head, then round 8 at a head that adds `kind: runbook`.
    The later head cannot buy rounds 8 to 11, so the refusal names the earlier head."""
    krepo.publish_main({POLICY_PATH: _policy_text(**SHIPPED)})
    c1, c2 = krepo.publish_pr_steps([
        {"projects/x/new.md": "hi\n"},
        {"projects/x/rb.md": _agent_doc("runbook")}])
    assert KM.refusals(krepo.read(c2, stamps=_spread([(c1, 7)]))) == []
    why = KM.refusals(krepo.read(c2, stamps=_spread([(c1, 7), (c2, 1)])))
    assert len(why) == 1 and "reviewer ran 8 rounds, past the cap of 7" in why[0], why
    assert f"at head {c1[:12]}, review_rounds_per_role (7) applies" in why[0], why
    assert f"head {c2[:12]} would raise it, because projects/x/rb.md has kind runbook" in why[0], why


def test_a_pr_that_is_in_the_floor_at_every_stamped_head_keeps_the_code_cap(krepo):
    krepo.publish_main({POLICY_PATH: _policy_text(**SHIPPED)})
    c1, c2 = krepo.publish_pr_steps([
        {"projects/x/rb.md": _agent_doc("runbook")},
        {"projects/x/other.md": "x\n"}])
    assert KM.refusals(krepo.read(c2, stamps=_spread([(c1, 4), (c2, 5)]))) == []    # nine rounds
    why = KM.refusals(krepo.read(c2, stamps=_spread([(c1, 4), (c2, 8)])))           # twelve
    assert len(why) == 1 and "reviewer ran 12 rounds, past the cap of 11" in why[0], why


def test_a_floor_pr_that_drops_its_floor_file_at_a_later_head_takes_the_document_cap(krepo):
    """Lowering the cap is allowed: the head that restores the runbook to plain text is
    documents-only, and that is the head the PR is judged at."""
    krepo.publish_main({POLICY_PATH: _policy_text(**SHIPPED), "projects/x/rb.md": "plain\n"})
    c1, c2 = krepo.publish_pr_steps([
        {"projects/x/rb.md": _agent_doc("runbook")},
        {"projects/x/rb.md": "plain\n"}])
    assert KM.refusals(krepo.read(c2, stamps=_spread([(c1, 7)]))) == []
    why = KM.refusals(krepo.read(c2, stamps=_spread([(c1, 8)])))
    assert len(why) == 1 and "reviewer ran 8 rounds, past the cap of 7" in why[0], why
    assert "no changed file is in the review floor" in why[0], why


def test_an_unreadable_stamped_head_takes_the_document_cap_even_when_the_current_head_is_in_the_floor(
        krepo):
    """A stamp at a head whose changes cannot be read is a tie-break at that head, so the PR
    takes 7 although its current head adds a runbook."""
    commit = "f" * 40
    why = KM.refusals(krepo.facts({"projects/x/rb.md": _agent_doc("runbook")},
                                  policy_files={POLICY_PATH: _policy_text(**SHIPPED)},
                                  stamps=_spread([(commit, 8)])))
    assert len(why) == 1 and "reviewer ran 8 rounds, past the cap of 7" in why[0], why
    assert f"at head {commit[:12]}" in why[0] and "could not be read" in why[0], why


# A stamp's round is placed by the commit its authenticated run pinned, never by its text: the
# text is what the stamp says, and `authenticate` accepts it only as a prefix of the pin. Each
# floor PR here is in the floor at its head, so the code cap (11) and the document cap (7)
# differ, and a read from the wrong source fails.

def _runbook_pr(krepo):
    krepo.publish_main({POLICY_PATH: _policy_text(**SHIPPED)})
    return krepo.publish_pr_steps([{"projects/x/rb.md": _agent_doc("runbook")}])[-1]


def test_a_pinned_run_keeps_the_code_cap_when_its_stamp_text_is_a_short_prefix(krepo):
    head = _runbook_pr(krepo)
    assert KM.refusals(krepo.read(head, stamps=_spread([(head[:9], 11, head)]))) == []    # 11
    why = KM.refusals(krepo.read(head, stamps=_spread([(head[:9], 12, head)])))           # 12
    assert len(why) == 1 and "reviewer ran 12 rounds, past the cap of 11" in why[0], why


def test_a_pinned_run_keeps_the_code_cap_when_its_stamp_text_is_upper_case(krepo):
    """The gate's `authenticate` drops an upper-case text before the cap reads it, so the live
    gate never passes this input. The cap is still read from the pin, and this checks that."""
    head = _runbook_pr(krepo)
    assert KM.refusals(krepo.read(head, stamps=_spread([(head.upper(), 11, head)]))) == []
    why = KM.refusals(krepo.read(head, stamps=_spread([(head.upper(), 12, head)])))
    assert len(why) == 1 and "reviewer ran 12 rounds, past the cap of 11" in why[0], why


def test_a_short_stamp_naming_an_earlier_documents_only_head_takes_the_document_cap(krepo):
    """The earlier head is a documents-only head, pinned by its full commit and named by a
    9-character prefix. The cap drops to 7 there, and the refusal names that head by its full
    commit."""
    krepo.publish_main({POLICY_PATH: _policy_text(**SHIPPED)})
    c1, c2 = krepo.publish_pr_steps([
        {"projects/x/new.md": "hi\n"},
        {"projects/x/rb.md": _agent_doc("runbook")}])
    why = KM.refusals(krepo.read(c2, stamps=_spread([(c1[:9], 7, c1), (c2, 1)])))
    assert len(why) == 1 and "reviewer ran 8 rounds, past the cap of 7" in why[0], why
    assert f"at head {c1[:12]}, review_rounds_per_role (7) applies" in why[0], why
    assert f"head {c2[:12]} would raise it, because projects/x/rb.md has kind runbook" in why[0], why


def test_a_seven_character_text_that_resolves_to_another_floor_commit_takes_the_document_cap(krepo):
    """The reviewer's case. Round 7 resolved a stamp's text in the checkout's whole object store,
    so a seven-character text that named another floor commit read as that commit and raised the
    cap to 11. Here the run's pin is absent from the checkout and starts with the seven characters
    of `c1`, a floor commit that is in it. A real attack grinds a commit to share that prefix,
    about 2^28 hashes, so this test writes the absent pin by hand. The gate reads the pin the same
    way either way, and the positive control shows the text resolves to `c1` in this checkout."""
    krepo.publish_main({POLICY_PATH: _policy_text(**SHIPPED)})
    c1, c2 = krepo.publish_pr_steps([
        {"projects/x/rb.md": _agent_doc("runbook")},
        {"projects/x/other.md": "x\n"}])
    absent = c1[:7] + "0" * 33
    krepo.pin(c2)   # fetches the head, and with it `c1`, which is its ancestor
    assert krepo.git("rev-parse", "--verify", "--quiet", f"{absent[:7]}^{{commit}}") == c1
    assert _REAL_RUN(["git", "-C", str(krepo.path), "cat-file", "-e", absent]).returncode != 0
    why = KM.refusals(krepo.read(c2, stamps=_spread([(absent[:7], 12, absent)])))
    assert len(why) == 1 and "reviewer ran 12 rounds, past the cap of 7" in why[0], why
    assert f"at head {absent[:12]}" in why[0] and "could not be read" in why[0], why


def test_a_pinned_sha_absent_from_the_checkout_takes_the_document_cap(krepo):
    """The run reviewed a commit this checkout does not hold, so its changed files cannot be
    read, and the document cap applies although the current head is in the floor."""
    head = _runbook_pr(krepo)
    absent = "e" * 40
    why = KM.refusals(krepo.read(head, stamps=_spread([(absent, 8)])))
    assert len(why) == 1 and "reviewer ran 8 rounds, past the cap of 7" in why[0], why
    assert f"at head {absent[:12]}" in why[0] and "could not be read" in why[0], why


def test_an_unpinned_stamp_takes_the_document_cap_whatever_its_text_says(krepo):
    """A run that pins no sha has no reviewed commit, so its stamp takes the document cap
    whatever its text is: the current head whole or short, a short prefix, or no hex at all."""
    head = _runbook_pr(krepo)
    for text in (head, head[:9], "deadbee", ""):
        why = KM.refusals(krepo.read(head, stamps=_spread([(text, 8, "")])))
        assert len(why) == 1 and "reviewer ran 8 rounds, past the cap of 7" in why[0], (text, why)
        assert f"reviewed-commit {text!r} names no reviewed commit" in why[0], (text, why)
        assert "could not be read" not in why[0], (text, why)


def test_a_run_whose_pin_is_a_short_sha_names_no_reviewed_commit_and_takes_the_document_cap(krepo):
    """`spawn --review-sha` records the sha as typed, so a run can pin a short sha. The gate does
    not resolve a pin, because that would be an object-store lookup again, so a short pin names
    no reviewed commit. The stamp here carries the full head and passes `authenticate`."""
    head = _runbook_pr(krepo)
    why = KM.refusals(krepo.read(head, stamps=_spread([(head, 11, head[:9])])))
    assert len(why) == 1 and "reviewer ran 11 rounds, past the cap of 7" in why[0], why
    assert f"reviewed-commit {head!r} names no reviewed commit" in why[0], why


def test_an_instruction_outside_projects_is_not_a_knowledge_document(krepo):
    why = KM.refusals(krepo.facts({"agents/new.md": "instructions\n"}))
    assert len(why) == 1 and "an instruction outside projects/" in why[0], why


@pytest.mark.parametrize("files", [
    {"agents/old.md": None, "projects/x/agents/old.md": "---\nkind: agent\ntitle: t\n---\nbody\n"},
    {"agents/old.md": None},
], ids=["renamed-into-projects", "deleted"])
def test_an_instruction_leaving_or_deleted_outside_projects_refuses(krepo, files):
    """The before side of the conjunction: the after side is under projects/ or
    absent, so only a check of where the file was refuses it. Convivy's setting
    (instructions true) is the default policy, so nothing else stands in the way."""
    why = KM.refusals(krepo.facts(files))
    assert len(why) == 1 and "an instruction outside projects/" in why[0], why
    assert "agents/old.md" in why[0]


@pytest.mark.parametrize("path", ["docs/a.md", "README.md", "scripts/x.md"])
def test_a_file_outside_projects_refuses(krepo, path):
    why = KM.refusals(krepo.facts({path: "hello\n"}))
    assert len(why) == 1 and path in why[0] and "not a knowledge document" in why[0], why


def test_deleting_an_instruction_is_judged_on_the_side_that_exists(krepo):
    """The deleted file has no after side, and its before side is an instruction."""
    files = {"projects/x/agents/old.md": None}
    why = KM.refusals(krepo.facts(
        files, policy_files={POLICY_PATH: _policy_text(knowledge=True, instructions=False)}))
    assert len(why) == 1 and "agents_may_merge_knowledge_instructions to false" in why[0]
    assert KM.refusals(krepo.facts(
        files, policy_files={POLICY_PATH: _policy_text(knowledge=False, instructions=True)})) == []


def test_deleting_a_plain_document_needs_only_the_knowledge_key(krepo):
    why = KM.refusals(krepo.facts(
        {"projects/x/plain.md": None},
        policy_files={POLICY_PATH: _policy_text(knowledge=False, instructions=True)}))
    assert len(why) == 1 and "agents_may_merge_knowledge to false" in why[0]


@pytest.mark.parametrize("src, dst", [
    ("projects/x/moving.md", "projects/x/agents/moving.md"),   # plain into an instruction dir
    ("projects/x/skills/s.md", "projects/x/s.md"),             # an instruction out of one
])
def test_a_rename_is_an_instruction_when_either_side_is(krepo, src, dst):
    body = krepo.BASE[src]
    files = {src: None, dst: body}
    why = KM.refusals(krepo.facts(
        files, policy_files={POLICY_PATH: _policy_text(knowledge=True, instructions=False)}))
    assert len(why) == 1 and "instructions to false" in why[0], why
    assert f"{src} -> {dst}" in " ".join(
        f"{c.path_before} -> {c.path_after}" for c in KM.read_git_facts(
            krepo.path, 7, krepo.head)[1])
    assert KM.refusals(krepo.facts(
        files, policy_files={POLICY_PATH: _policy_text(knowledge=False, instructions=True)})) == []


def test_a_rename_out_of_projects_refuses_naming_both_paths(krepo):
    why = KM.refusals(krepo.facts({"projects/x/moving.md": None,
                                   "docs/moving.md": krepo.BASE["projects/x/moving.md"]}))
    assert len(why) == 1 and "projects/x/moving.md -> docs/moving.md" in why[0], why


def test_a_file_that_is_not_utf8_is_unclassifiable_and_refuses(krepo):
    """`\xff\xfe` decoded lossily would be printable U+FFFD and read as a clean
    document, so the bytes are passed as None and the file cannot be classified."""
    why = KM.refusals(krepo.facts({"projects/x/bad.md": b"caf\xe9 \xff\xfe\n"}))
    assert len(why) == 1 and "projects/x/bad.md cannot be classified" in why[0], why
    assert "no readable text" in why[0]


def test_deleting_a_file_that_is_not_utf8_refuses_too(krepo):
    krepo.publish_main({"projects/x/bad.md": b"\xff\xfe\n"})
    why = KM.refusals(krepo.facts({"projects/x/bad.md": None}))
    assert len(why) == 1 and "projects/x/bad.md cannot be classified" in why[0], why


def test_every_changed_file_is_judged_and_all_the_reasons_are_reported(krepo):
    why = KM.refusals(krepo.facts({"projects/x/new.md": "ok\n", "docs/a.md": "x\n",
                                   "projects/x/bad.md": b"\xff\n", POLICY_PATH: None}))
    assert len(why) == 3, why


def test_the_changes_carry_paths_modes_and_text_from_git(krepo):
    krepo.facts({"projects/x/new.md": "hi\n", "projects/x/run.md": ("100755", "go\n"),
                 "projects/x/plain.md": None, "projects/x/a.md": "two\n",
                 "projects/x/moving.md": None,
                 "projects/x/moved.md": krepo.BASE["projects/x/moving.md"]})
    got = {(c.path_before, c.path_after): c for c in KM.read_git_facts(krepo.path, 7, krepo.head)[1]}
    assert got[(None, "projects/x/new.md")] == KM.Change(
        None, None, None, "projects/x/new.md", "100644", "hi\n")
    assert got[(None, "projects/x/run.md")].mode_after == "100755"
    assert got[("projects/x/plain.md", None)] == KM.Change(
        "projects/x/plain.md", "100644", "a plain document\n", None, None, None)
    assert got[("projects/x/a.md", "projects/x/a.md")].text_before == "one\n"
    assert got[("projects/x/a.md", "projects/x/a.md")].text_after == "two\n"
    assert ("projects/x/moving.md", "projects/x/moved.md") in got


def test_a_head_that_is_not_the_one_the_gate_judged_is_unreadable(krepo):
    sha = krepo.publish_pr({"projects/x/new.md": "hi\n"})
    krepo.pin(sha)
    with pytest.raises(KM.Unreadable, match="not the bbbbbbbbbbbb"):
        KM.read_git_facts(krepo.path, 7, "b" * 40)


def test_a_missing_origin_main_is_unreadable_and_a_lookalike_tag_is_not_it(krepo):
    """The fully qualified remote-tracking ref is required, so a tag of that
    name cannot stand in when the real ref is gone."""
    sha = krepo.publish_pr({"projects/x/new.md": "hi\n"})
    krepo.pin(sha)
    krepo.git("tag", "refs/remotes/origin/main", "main")
    krepo.git("update-ref", "-d", "refs/remotes/origin/main")
    with pytest.raises(KM.Unreadable, match="refs/remotes/origin/main does not exist"):
        KM.read_git_facts(krepo.path, 7, sha)


def test_a_branch_off_current_main_is_admitted_on_the_ancestry_condition(krepo):
    """A PR branched from main as it stands contains it, the ordinary case."""
    sha = krepo.publish_pr({"projects/x/new.md": "hi\n"})
    krepo.pin(sha)
    policy, changes, base_sha, head_contains_base = KM.read_git_facts(krepo.path, 7, sha)
    assert base_sha == krepo.git("rev-parse", "refs/remotes/origin/main")
    assert head_contains_base is True
    assert KM.refusals(_facts(policy=policy, changes=changes, head_sha=sha, gate_head=sha,
                              base_sha=base_sha, head_contains_base=head_contains_base)) == []


def test_a_branch_behind_a_main_that_moved_on_is_refused_naming_both_shas(krepo):
    """knowledge#699: the branch's required checks ran against a main that did not
    yet hold a decision added the next day, and `kb merge` must not admit it on
    that stale PASS (issue_01M3XNHABVJ4RZJPQAQ13A46DW)."""
    sha = krepo.publish_pr({"projects/x/new.md": "hi\n"})
    krepo.pin(sha)
    krepo.publish_main({"projects/x/after.md": "the commit the branch never saw\n"})
    fresh_main = krepo.git("rev-parse", "refs/remotes/origin/main")
    policy, changes, base_sha, head_contains_base = KM.read_git_facts(krepo.path, 7, sha)
    assert base_sha == fresh_main
    assert head_contains_base is False
    why = KM.refusals(_facts(policy=policy, changes=changes, head_sha=sha, gate_head=sha,
                             base_sha=base_sha, head_contains_base=head_contains_base))
    assert len(why) == 1
    assert fresh_main[:12] in why[0] and sha[:12] in why[0]
    assert "merged in" in why[0] and "re-run" in why[0]


def test_a_branch_is_admitted_again_once_main_is_merged_into_it(krepo):
    """The refusal above is fixed by updating the branch with main, not by
    waiting: merging main in restores the ancestry condition."""
    sha = krepo.publish_pr({"projects/x/new.md": "hi\n"})
    krepo.pin(sha)
    krepo.publish_main({"projects/x/after.md": "the commit the branch never saw\n"})
    assert KM.read_git_facts(krepo.path, 7, sha)[3] is False   # the control: still behind
    krepo.git("fetch", "-q", "origin", "main", cwd=krepo.author_clone)
    krepo.git("checkout", "-q", "pr-branch", cwd=krepo.author_clone)
    krepo.git("merge", "-q", "--no-edit", "origin/main", cwd=krepo.author_clone)
    merged = krepo.git("rev-parse", "HEAD", cwd=krepo.author_clone)
    krepo.git("push", "-q", "-f", "origin", "HEAD:refs/pull/7/head", cwd=krepo.author_clone)
    krepo.pin(merged)
    policy, changes, base_sha, head_contains_base = KM.read_git_facts(krepo.path, 7, merged)
    assert head_contains_base is True
    assert KM.refusals(_facts(policy=policy, changes=changes, head_sha=merged, gate_head=merged,
                              base_sha=base_sha, head_contains_base=head_contains_base)) == []


def test_a_graft_cannot_forge_ancestry_past_a_stale_head(krepo):
    """`.git/info/grafts` rewrites a commit's recorded parents the same way
    `refs/replace` does, and `--no-replace-objects` does not cover it. A graft
    that gives the stale PR head a forged parent at the fresh `origin/main`
    must not make `read_git_facts` report the branch as containing it."""
    sha = krepo.publish_pr({"projects/x/new.md": "hi\n"})
    krepo.pin(sha)
    krepo.publish_main({"projects/x/after.md": "the commit the branch never saw\n"})
    fresh_main = krepo.git("rev-parse", "refs/remotes/origin/main")
    (krepo.path / ".git" / "info" / "grafts").write_text(f"{sha} {fresh_main}\n")
    policy, changes, base_sha, head_contains_base = KM.read_git_facts(krepo.path, 7, sha)
    assert head_contains_base is False
    why = KM.refusals(_facts(policy=policy, changes=changes, head_sha=sha, gate_head=sha,
                             base_sha=base_sha, head_contains_base=head_contains_base))
    assert len(why) == 1 and "does not contain" in why[0], why


def test_a_git_failure_in_the_ancestry_check_refuses_rather_than_admitting(krepo):
    """Exit 1 from `merge-base --is-ancestor` is git's own "not an ancestor", but
    any other non-zero exit (an invalid object, here) is unreadable, and the
    module's rule is that an unreadable fact refuses rather than being read as
    either answer."""
    with pytest.raises(KM.Unreadable, match="merge-base --is-ancestor"):
        KM._is_ancestor(krepo.path, "d" * 40, "d" * 40)


def test_nothing_here_reads_home_from_the_environment(monkeypatch, tmp_path):
    """The config path is a trust anchor, so it follows the passwd home.

    `CONFIG_PATH` is fixed when the module is imported, so a test that moves
    `$HOME` and then reads the already-imported value changes nothing being
    compared. This loads a fresh copy of the module with `$HOME` moved and
    checks the path that copy computed. A `CONFIG_PATH` derived from
    `Path.home()` or `$HOME` lands under `tmp_path` and fails; so does a
    renamed config file.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    assert BR.real_account_home() != tmp_path, "the fixture home must differ from passwd's"
    spec = importlib.util.spec_from_file_location("kb_merge_fresh_home", KM.__file__)
    fresh = importlib.util.module_from_spec(spec)
    # dataclasses resolves string annotations through sys.modules[cls.__module__]
    monkeypatch.setitem(sys.modules, spec.name, fresh)
    spec.loader.exec_module(fresh)
    assert fresh.CONFIG_PATH == (
        BR.real_account_home() / ".config" / "convivy" / "library-app.json")


def test_the_key_and_app_id_are_read_at_call_time_on_every_path(tmp_path, monkeypatch):
    """`key_problem` and `app_jwt`, which `kb merge` calls with no argument,
    follow the configured key and app id as they are when called. A default
    bound at definition would hand the shared helpers the value from import,
    past conftest's repoint. The helpers are spied on so the assertion is the
    value each one receives."""
    where = tmp_path / "elsewhere.pem"
    config_path = tmp_path / "library-app.json"
    config_path.write_text(json.dumps({
        "repo": "acme/kb", "app_id": "999999", "bot_login": "acme-library[bot]",
        "bot_user_id": 1, "key_path": str(where)}))
    monkeypatch.setattr(KM, "CONFIG_PATH", config_path)
    seen = {}
    monkeypatch.setattr(KM.GA, "key_problem",
                        lambda path, **k: seen.update(problem=path))
    monkeypatch.setattr(KM.GA, "app_jwt",
                        lambda path, app_id, **k: seen.update(jwt=(path, app_id)) or "j.w.t")
    KM.key_problem()
    KM.app_jwt()
    assert seen == {"problem": where, "jwt": (where, "999999")}


# --- pushing as the App -----------------------------------------------------------
# `kb apply` pushes main and `kb propose` pushes a kb/ branch as `convivy-library`.
# Nothing below makes a network call to GitHub: the API is an injected opener, and
# git talks to a server on 127.0.0.1.


def test_the_push_token_is_scoped_to_knowledge_and_contents_alone():
    """A push needs `contents: write` and nothing else. `pull_requests` would let
    a bug that tried to open or merge a PR succeed with the push token."""
    seen = {}

    def opener(req, timeout=None):
        seen.update(url=req.full_url, auth=req.get_header("Authorization"),
                    body=json.loads(req.data))
        return _Resp(201, {"token": "ghs_push"})

    assert KM.push_token("jwt.x.y", "161212648", opener=opener) == "ghs_push"
    assert seen["url"].endswith("/app/installations/161212648/access_tokens")
    assert seen["body"] == {"repositories": ["knowledge"],
                            "permissions": {"contents": "write"}}


def test_the_merge_token_keeps_both_permissions_beside_the_narrower_push_token():
    """Narrowing the push token must not narrow the merge one, which needs both."""
    seen = []

    def spy(req, timeout=None):
        seen.append(json.loads(req.data)["permissions"])
        return _Resp(201, {"token": "ghs_x"})

    KM.installation_token("j", "1", opener=spy)
    KM.push_token("j", "1", opener=spy)
    assert seen == [{"contents": "write", "pull_requests": "write"}, {"contents": "write"}]


def test_the_read_token_is_scoped_to_knowledge_and_contents_read_alone():
    """`kb sync` runs every ten minutes and `kb checkout` on every checkout, and
    both only fetch. A bug in either that tried to push is refused by GitHub."""
    seen = []

    def opener(req, timeout=None):
        seen.append(json.loads(req.data))
        return _Resp(201, {"token": "ghs_read"})

    assert KM.read_token("jwt.x.y", "161212648", opener=opener) == "ghs_read"
    assert seen == [{"repositories": ["knowledge"], "permissions": {"contents": "read"}}]


def test_the_pr_token_is_not_the_merge_token_and_holds_no_contents_write():
    """Both reviewers of the change that introduced it asked for this narrowing.
    `contents: write` from this App can push `main` without review, and the PR
    token sits in `gh`'s environment."""
    seen = []

    def opener(req, timeout=None):
        seen.append(json.loads(req.data)["permissions"])
        return _Resp(201, {"token": "ghs_x"})

    KM.pr_token("j", "1", opener=opener)
    assert seen == [{"pull_requests": "write", "contents": "read"}]
    assert seen[0]["contents"] != "write"


def test_a_read_credential_mints_contents_read_and_no_other_scoped_token(rsa_key):
    key, _ = rsa_key
    opener, seen = _scoped_mint_routes()
    assert KM.read_credential(key, opener=opener) == "ghs_read"
    assert [b for _, _, b in seen if b] == [
        {"repositories": ["knowledge"], "permissions": {"contents": "read"}}]


def test_revoking_a_token_reports_what_github_said_and_never_raises():
    seen = {}

    def opener(req, timeout=None):
        seen.update(method=req.get_method(), url=req.full_url, auth=req.get_header("Authorization"))
        return _Resp(204, {})

    assert KM.GA.revoke_installation_token("ghs_x", opener=opener) is True
    assert seen == {"method": "DELETE", "url": "https://api.github.com/installation/token",
                    "auth": "Bearer ghs_x"}
    assert KM.GA.revoke_installation_token(
        "ghs_x", opener=lambda req, timeout=None: _Resp(401, {})) is False

    def down(req, timeout=None):
        raise urllib.error.URLError("nodename nor servname provided")
    assert KM.GA.revoke_installation_token("ghs_x", opener=down) is False


def _mint_routes(*, installs=ONE_INSTALL, repos=KNOWLEDGE_ONLY, mint=(201, {"token": "ghs_push"})):
    """The API as `push_credential` sees it. The unscoped listing token and the
    scoped push token hit the same URL; only the request body tells them apart."""
    seen = []

    def opener(req, timeout=None):
        body = json.loads(req.data) if req.data else None
        seen.append((req.get_method(), req.full_url.split("api.github.com")[1].split("?")[0], body))
        if req.get_method() == "DELETE" and req.full_url.endswith("/installation/token"):
            return _Resp(204, {})
        if "/app/installations" in req.full_url and req.get_method() == "GET":
            return _Resp(200, installs)
        if req.full_url.endswith("/access_tokens"):
            return _Resp(201, {"token": "ghs_listing"}) if body is None else _Resp(*mint)
        if "/installation/repositories" in req.full_url:
            return _Resp(200, repos)
        raise AssertionError(f"unexpected {req.get_method()} {req.full_url}")
    return opener, seen


def test_a_push_credential_is_derived_checked_and_minted_contents_only(rsa_key):
    key, _ = rsa_key
    opener, seen = _mint_routes()
    assert KM.push_credential(key, opener=opener) == "ghs_push"
    assert [(m, u) for m, u, _ in seen] == [
        ("GET", "/app/installations"),
        ("POST", "/app/installations/161212648/access_tokens"),   # the unscoped listing token
        ("GET", "/installation/repositories"),
        ("DELETE", "/installation/token"),                        # the listing token, revoked
        ("POST", "/app/installations/161212648/access_tokens")]   # the push token
    assert seen[1][2] is None
    assert seen[4][2] == {"repositories": ["knowledge"], "permissions": {"contents": "write"}}


def test_no_key_refuses_and_names_the_key(tmp_path):
    with pytest.raises(KM.Unreadable) as e:
        KM.push_credential(tmp_path / "absent.pem", opener=_no_network)
    assert str(e.value).startswith("App key:") and "absent.pem" in str(e.value)


def test_a_key_others_can_read_refuses_before_any_request(rsa_key):
    key, _ = rsa_key
    key.chmod(0o644)
    with pytest.raises(KM.Unreadable) as e:
        KM.push_credential(key, opener=_no_network)
    assert str(e.value).startswith("App key:") and "must be 600" in str(e.value)


def _scoped_mint_routes():
    """The API as `push_and_pr_credentials` sees it: the scoped mint answers with a
    token named for the scope it was asked for, so the two cannot be confused."""
    seen = []

    def opener(req, timeout=None):
        body = json.loads(req.data) if req.data else None
        seen.append((req.get_method(), req.full_url.split("api.github.com")[1].split("?")[0], body))
        if req.get_method() == "DELETE" and req.full_url.endswith("/installation/token"):
            return _Resp(204, {})
        if "/app/installations" in req.full_url and req.get_method() == "GET":
            return _Resp(200, ONE_INSTALL)
        if req.full_url.endswith("/access_tokens"):
            if body is None:
                return _Resp(201, {"token": "ghs_listing"})
            perms = body["permissions"]
            token = ("ghs_pr" if "pull_requests" in perms
                     else "ghs_read" if perms == {"contents": "read"} else "ghs_push")
            return _Resp(201, {"token": token})
        if "/installation/repositories" in req.full_url:
            return _Resp(200, KNOWLEDGE_ONLY)
        raise AssertionError(f"unexpected {req.get_method()} {req.full_url}")
    return opener, seen


def test_the_pr_token_carries_pull_requests_and_the_push_token_still_does_not(rsa_key):
    """`kb propose` opens a PR, which the push token is refused by design. The two
    are minted side by side from ONE installation check, the push token still with
    `contents: write` alone, and the pair is returned in that order.

    The PR token is NOT the merge's shape. It holds `contents: read`, because the
    three `gh` calls it authenticates write no file, and `contents: write` from
    this App can push `main` (it is on the `update` ruleset's bypass list)."""
    key, _ = rsa_key
    opener, seen = _scoped_mint_routes()
    assert KM.push_and_pr_credentials(key, opener=opener) == ("ghs_push", "ghs_pr")
    scoped = [b for _, _, b in seen if b]
    assert scoped == [
        {"repositories": ["knowledge"], "permissions": {"contents": "write"}},
        {"repositories": ["knowledge"],
         "permissions": {"pull_requests": "write", "contents": "read"}}]
    listings = [u for m, u, _ in seen if m == "GET"].count("/installation/repositories")
    assert listings == 1, "the installation was checked once per token"


def test_a_push_credential_alone_never_asks_for_pull_requests(rsa_key):
    """`kb apply` and `kb sync` open no PR, so their token must not carry the
    permission; asking for the pair is `kb propose`'s alone."""
    key, _ = rsa_key
    opener, seen = _scoped_mint_routes()
    assert KM.push_credential(key, opener=opener) == "ghs_push"
    assert [b for _, _, b in seen if b] == [
        {"repositories": ["knowledge"], "permissions": {"contents": "write"}}]


@pytest.mark.parametrize("mint", [KM.push_credential, KM.push_and_pr_credentials,
                                  KM.read_credential])
def test_a_widened_installation_refuses_before_any_scoped_token_is_minted(rsa_key, mint):
    """The PR token does not weaken the check that stops the credential reaching
    another repository."""
    key, _ = rsa_key
    wide = {"total_count": 2, "repositories": [{"full_name": "convivy/knowledge"},
                                               {"full_name": "convivy/library"}]}
    opener, seen = _mint_routes(repos=wide)
    with pytest.raises(KM.Unreadable) as e:
        mint(key, opener=opener)
    assert str(e.value).startswith("installation: repo list:")
    assert not [b for _, _, b in seen if b and "permissions" in b]


def test_a_missing_key_refuses_the_pair_and_names_the_key(tmp_path):
    with pytest.raises(KM.Unreadable) as e:
        KM.push_and_pr_credentials(tmp_path / "absent.pem", opener=_no_network)
    assert str(e.value).startswith("App key:")


def test_an_unreachable_github_is_told_apart_from_a_refusal(rsa_key):
    """`kb sync` treats an outage as offline, and a refused mint as a failure."""
    key, _ = rsa_key

    def down(req, timeout=None):
        raise urllib.error.URLError("nodename nor servname provided")

    with pytest.raises(KM.Unreachable):
        KM.push_credential(key, opener=down)
    opener, _ = _mint_routes(mint=(403, {"message": "Resource not accessible"}))
    with pytest.raises(KM.Unreadable) as e:
        KM.push_credential(key, opener=opener)
    assert not isinstance(e.value, KM.Unreachable)
    assert issubclass(KM.Unreachable, KM.Unreadable), "every caller that refuses must still catch it"


def _no_network(req, timeout=None):
    raise AssertionError(f"a request was made: {req.get_method()} {req.full_url}")


def test_a_jwt_that_cannot_be_signed_refuses_naming_the_jwt(rsa_key, monkeypatch):
    key, _ = rsa_key
    monkeypatch.setattr(KM, "app_jwt", lambda *a, **k: (_ for _ in ()).throw(
        KM.Unreadable("openssl could not sign the App JWT: bad key")))
    with pytest.raises(KM.Unreadable) as e:
        KM.push_credential(key, opener=_no_network)
    assert str(e.value).startswith("App JWT:") and "bad key" in str(e.value)


@pytest.mark.parametrize("installs, repos, named", [
    (ONE_INSTALL * 2, KNOWLEDGE_ONLY, "count"),
    ([{"id": 1, "account": {"login": "someone-else"}}], KNOWLEDGE_ONLY, "owner"),
    (ONE_INSTALL, {"total_count": 2, "repositories": [{"full_name": "convivy/knowledge"},
                                                      {"full_name": "convivy/library"}]},
     "repo list"),
])
def test_a_widened_installation_refuses_the_push_and_no_push_token_is_minted(
        rsa_key, installs, repos, named):
    """The check that stops this credential reaching another repository holds on
    the push path too, and refuses before the push token exists."""
    key, _ = rsa_key
    opener, seen = _mint_routes(installs=installs, repos=repos)
    with pytest.raises(KM.Unreadable) as e:
        KM.push_credential(key, opener=opener)
    assert str(e.value).startswith(f"installation: {named}:"), e.value
    assert not [b for _, _, b in seen if b and "permissions" in b], \
        "a scoped push token was minted for a widened installation"


def test_a_refused_mint_refuses_the_push_naming_the_mint(rsa_key):
    key, _ = rsa_key
    opener, _ = _mint_routes(mint=(403, {"message": "Resource not accessible"}))
    with pytest.raises(KM.Unreadable) as e:
        KM.push_credential(key, opener=opener)
    assert str(e.value).startswith("token mint:")
    assert "403" in str(e.value) and "Resource not accessible" in str(e.value)


def test_github_being_unreachable_refuses_rather_than_raising(rsa_key):
    key, _ = rsa_key

    def down(req, timeout=None):
        raise urllib.error.URLError("nodename nor servname provided")

    with pytest.raises(KM.Unreadable) as e:
        KM.push_credential(key, opener=down)
    assert str(e.value).startswith("GitHub could not be reached:")


def test_the_test_suite_holds_no_app_key():
    """conftest points KEY_PATH at nothing, so an un-stubbed caller refuses
    instead of minting a real token and pushing to the real knowledge repo."""
    with pytest.raises(KM.Unreadable) as e:
        KM.push_credential(opener=_no_network)
    assert str(e.value).startswith("App key: no App key at")


# The environment. Assertions are on the mechanism (where the token lands), and
# a live git process below shows git honours it.

TOKEN = "ghs_SECRETTOKENVALUE"
BASIC = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()


def test_the_token_travels_only_as_a_scoped_header_in_the_environment():
    env = KM.push_env(TOKEN, base={"PATH": "/usr/bin"})
    pairs = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
             for i in range(int(env["GIT_CONFIG_COUNT"]))}
    assert pairs[f"http.{KM.push_url()}.extraHeader"] == f"Authorization: Basic {BASIC}"
    assert len(pairs) == 2
    assert not any(TOKEN in v for v in env.values()), "the raw token is in the environment"
    assert "x-access-token" not in KM.push_url() and TOKEN not in KM.push_url()
    assert KM.push_url() == "https://github.com/convivy/knowledge.git"


def test_every_credential_helper_is_reset_and_nothing_can_be_asked():
    """This machine's gitconfig sets `credential.https://github.com.helper` to
    `gh auth git-credential`, which is the operator. The reset makes the fall-through
    impossible, not just unused."""
    env = KM.push_env(TOKEN, base={})
    pairs = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
             for i in range(int(env["GIT_CONFIG_COUNT"]))}
    assert pairs["credential.helper"] == ""
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_ASKPASS"] == "" and env["SSH_ASKPASS"] == ""
    assert env["GIT_ALLOW_PROTOCOL"] == "https"


def test_push_env_leaves_the_callers_environment_alone():
    base = {"PATH": "/usr/bin"}
    KM.push_env(TOKEN, base=base)
    assert base == {"PATH": "/usr/bin"}


def test_scrub_removes_the_token_in_both_forms():
    text = f"fatal: unable to access: {TOKEN} / Authorization: Basic {BASIC}"
    out = KM.scrub(text, TOKEN)
    assert TOKEN not in out and BASIC not in out
    assert KM.scrub("nothing to hide", TOKEN) == "nothing to hide"
    assert KM.scrub("anything", "") == "anything"


class _GitServer:
    """A local HTTP server that records every Authorization header and answers 401.

    401 is the case that matters: it is where git would consult a credential
    helper, so a helper that runs shows up as a marker file."""

    def __init__(self, status=401):
        seen = self.seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, self.headers.get("Authorization")))
                self.send_response(status)
                self.send_header("WWW-Authenticate", 'Basic realm="x"')
                self.end_headers()

            def log_message(self, *a):
                pass

        self._srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self._srv.server_port}/convivy/knowledge.git"
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    def close(self):
        self._srv.shutdown()
        self._srv.server_close()


@pytest.fixture
def git_server():
    srv = _GitServer()
    yield srv
    srv.close()


def _git_push_as_app(tmp_path, url, gitconfig_body=""):
    """A real `git push` to `url` under `push_env`, with a gitconfig standing in
    for this machine's (a marker helper, and optionally more)."""
    marker = tmp_path / "helper-ran"
    cfg = tmp_path / "gitconfig"
    cfg.write_text(
        '[credential "http://127.0.0.1"]\n'
        f"\thelper = !touch {marker}; echo username=helper; echo password=helper\n"
        f"[credential]\n\thelper = !touch {marker}\n" + gitconfig_body)
    repo = tmp_path / "repo"
    repo.mkdir()
    base = dict(os.environ, GIT_CONFIG_GLOBAL=str(cfg), GIT_CONFIG_NOSYSTEM="1")
    run = lambda *a, env=None: subprocess.run(   # noqa: E731
        ["git", "-C", str(repo), *a], capture_output=True, text=True, env=env or base)
    run("init", "-q", "--initial-branch=main")
    run("-c", "user.email=t@e", "-c", "user.name=T", "commit", "-q", "--allow-empty", "-m", "x")
    r = run("push", "-q", url, "HEAD:main", env=KM.push_env(TOKEN, base=base, url=url))
    return r, marker


def test_a_live_git_push_sends_the_token_as_a_header_and_prints_it_nowhere(
        tmp_path, git_server):
    r, _ = _git_push_as_app(tmp_path, git_server.url)
    assert git_server.seen, "git never reached the server"
    assert {auth for _, auth in git_server.seen} == {f"Basic {BASIC}"}
    assert r.returncode != 0
    assert TOKEN not in r.stdout + r.stderr and BASIC not in r.stdout + r.stderr


def test_a_rejected_token_never_falls_through_to_a_credential_helper(tmp_path, git_server):
    """The server rejects the token (401), which is where git asks a helper for
    another credential. With this machine's `gh auth git-credential` that would be
    the operator. The marker helper must not run and git must fail instead."""
    r, marker = _git_push_as_app(tmp_path, git_server.url)
    assert r.returncode != 0
    assert "terminal prompts disabled" in r.stderr
    assert not marker.exists(), "a credential helper ran"


def test_the_marker_helper_does_run_without_the_env(tmp_path, git_server):
    """The control for the test above: with a plain environment the same
    gitconfig DOES run the helper, so its absence there means something."""
    marker = tmp_path / "helper-ran"
    cfg = tmp_path / "gitconfig"
    cfg.write_text(f"[credential]\n\thelper = !touch {marker}\n")
    env = dict(os.environ, GIT_CONFIG_GLOBAL=str(cfg), GIT_CONFIG_NOSYSTEM="1",
               GIT_TERMINAL_PROMPT="0")
    subprocess.run(["git", "ls-remote", git_server.url], capture_output=True, text=True,
                   env=env, cwd=tmp_path)
    assert marker.exists()


def test_an_insteadof_rewrite_to_ssh_is_refused_not_followed(tmp_path):
    """A gitconfig that rewrites https://github.com/ to ssh would silently send the
    push over the operator's personal key. GIT_ALLOW_PROTOCOL turns that into a refusal."""
    cfg = tmp_path / "gitconfig"
    cfg.write_text('[url "ssh://git@127.0.0.1:1/"]\n\tinsteadOf = https://github.com/\n')
    repo = tmp_path / "r"
    repo.mkdir()
    base = dict(os.environ, GIT_CONFIG_GLOBAL=str(cfg), GIT_CONFIG_NOSYSTEM="1")
    control = subprocess.run(["git", "ls-remote", "--get-url", KM.push_url()],
                             capture_output=True, text=True, env=base, cwd=repo)
    assert control.stdout.startswith("ssh://"), "the rewrite must be real for this test to mean anything"
    r = subprocess.run(["git", "ls-remote", KM.push_url()], capture_output=True, text=True,
                       env=KM.push_env(TOKEN, base=base), cwd=repo, timeout=30)
    assert r.returncode != 0 and "transport 'ssh' not allowed" in r.stderr


def test_the_git_runner_keeps_the_token_out_of_argv_and_out_of_what_it_returns(
        agentctl, monkeypatch, tmp_path):
    """`_kb_library_git` is what both commands push through. The token must not be
    in argv (visible in `ps` to every user), and a failure's stderr, which the
    callers print, must come back scrubbed even if git somehow echoed it."""
    monkeypatch.setattr(KM, "push_credential", lambda *a, **k: TOKEN)
    ran = {}

    def fake_run(cmd, **kw):
        ran.update(cmd=cmd, env=kw["env"])
        return subprocess.CompletedProcess(
            cmd, 1, "", f"fatal: rejected {TOKEN} and Authorization: Basic {BASIC}")

    git, why = agentctl._kb_library_git("kb test")
    assert why is None
    monkeypatch.setattr(agentctl.subprocess, "run", fake_run)
    r = git(tmp_path, "push", "-q", KM.push_url(), "kb/x")
    assert ran["cmd"] == ["git", "-C", str(tmp_path), "push", "-q", KM.push_url(), "kb/x"]
    assert not any(TOKEN in a or BASIC in a for a in ran["cmd"])
    assert ran["env"]["GIT_CONFIG_VALUE_1"] == f"Authorization: Basic {BASIC}"
    assert TOKEN not in r.stderr and BASIC not in r.stderr and "fatal: rejected" in r.stderr


def test_the_git_runner_refuses_naming_the_stage_and_never_returns_a_runner(
        agentctl, monkeypatch, capsys):
    monkeypatch.setattr(KM, "push_credential", lambda *a, **k: (_ for _ in ()).throw(
        KM.Unreadable("installation: repo list: the installation reaches ['a/b', "
                      "'convivy/knowledge'] (total 2), not exactly ['convivy/knowledge']")))
    git, why = agentctl._kb_library_git("kb apply")
    err = capsys.readouterr().err
    assert git is None and "installation: repo list:" in why
    assert err.startswith("kb apply: refused") and "installation: repo list:" in err
    assert "does not fall back to a personal credential" in err


# `gh`, the commit, and the fetch. As above, no network: `gh` and the environments
# are checked at the boundary, and git runs against local repositories.


def test_gh_env_sets_the_app_token_over_whatever_the_caller_inherited():
    base = {"PATH": "/usr/bin", "GH_TOKEN": "ghp_the_operators", "GITHUB_TOKEN": "ghp_other"}
    env = KM.gh_env(TOKEN, base=base)
    assert env["GH_TOKEN"] == TOKEN and env["GH_PROMPT_DISABLED"] == "1"
    assert env["PATH"] == "/usr/bin"
    assert base["GH_TOKEN"] == "ghp_the_operators", "the caller's environment was edited"


def _commit_as(repo, env):
    repo.mkdir()
    run = lambda *a, env=env: subprocess.run(   # noqa: E731
        ["git", "-C", str(repo), *a], capture_output=True, text=True, env=env)
    run("init", "-q", "--initial-branch=main")
    r = run("commit", "-q", "--allow-empty", "-m", "x")
    assert r.returncode == 0, r.stderr
    return run("log", "-1", "--format=%an|%ae|%cn|%ce").stdout.strip()


def test_a_commit_made_under_commit_env_names_the_app_as_author_and_committer(tmp_path):
    """Against a gitconfig that names a person AND an environment that does. The
    control shows both are live; without `commit_env` the person is the author or
    the committer, and with it neither is. The gitconfig itself is not edited."""
    cfg = tmp_path / "gitconfig"
    cfg.write_text("[user]\n\tname = The Operator\n\temail = operator@example.com\n")
    before = cfg.read_text()
    base = dict(os.environ, GIT_CONFIG_GLOBAL=str(cfg), GIT_CONFIG_NOSYSTEM="1",
                GIT_AUTHOR_NAME="Someone Else", GIT_COMMITTER_EMAIL="else@example.com")
    assert _commit_as(tmp_path / "control", base) == \
        "Someone Else|operator@example.com|The Operator|else@example.com"
    bot = "convivy-library[bot]|328494887+convivy-library[bot]@users.noreply.github.com"
    assert _commit_as(tmp_path / "app", KM.commit_env(base)) == f"{bot}|{bot}"
    assert cfg.read_text() == before


def test_commit_env_leaves_the_callers_environment_alone():
    base = {"PATH": "/usr/bin", "GIT_AUTHOR_NAME": "The Operator"}
    KM.commit_env(base)
    assert base == {"PATH": "/usr/bin", "GIT_AUTHOR_NAME": "The Operator"}


def test_a_fetch_by_url_updates_origin_main_only_when_the_refspec_names_it(tmp_path):
    """`git fetch <url> main` leaves `origin/main` where it was, and the cage reads
    that ref, so the refspec is what makes a fetch over PUSH_URL a fetch. It forces,
    as the default `origin` refspec does, so a rewritten main is followed."""
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir(), dst.mkdir()
    g = lambda repo, *a: subprocess.run(   # noqa: E731
        ["git", "-C", str(repo), "-c", "user.email=t@e", "-c", "user.name=T", *a],
        capture_output=True, text=True)
    g(src, "init", "-q", "--initial-branch=main")
    g(src, "commit", "-q", "--allow-empty", "-m", "one")
    g(dst, "init", "-q")
    ref = "refs/remotes/origin/main"
    g(dst, "fetch", "--quiet", str(src), "main")
    assert g(dst, "rev-parse", "--verify", "-q", ref).returncode != 0, \
        "the control: fetching by URL alone must not have created the ref"
    assert g(dst, "fetch", "--quiet", str(src), KM.MAIN_REFSPEC).returncode == 0
    first = g(src, "rev-parse", "HEAD").stdout
    assert g(dst, "rev-parse", ref).stdout == first
    g(src, "commit", "-q", "--amend", "--allow-empty", "-m", "rewritten")
    assert g(dst, "fetch", "--quiet", str(src), KM.MAIN_REFSPEC).returncode == 0
    assert g(dst, "rev-parse", ref).stdout == g(src, "rev-parse", "HEAD").stdout != first


def _fake_runner(agentctl, monkeypatch, stderr=""):
    ran = []

    def fake_run(cmd, **kw):
        ran.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, 1 if stderr else 0, "", stderr)
    monkeypatch.setattr(agentctl.subprocess, "run", fake_run)
    return ran


def test_git_and_gh_each_get_only_their_own_token_in_their_own_environment(
        agentctl, monkeypatch, tmp_path):
    """The push token authenticates git and the PR token authenticates gh, and
    neither process can see the other's: a `gh` that read the push token would
    have `contents` and no more, and a git that carried the PR token would hand
    every remote it named a credential that can merge."""
    monkeypatch.delenv("GH_TOKEN", raising=False)
    ran = _fake_runner(agentctl, monkeypatch)
    lib = agentctl._KbLibrary("ghs_PUSH", "ghs_PR")
    lib(tmp_path, "push", "-q", KM.push_url(), "kb/x")
    lib.fetch_main(tmp_path)
    lib.gh(tmp_path, "pr", "list")
    (push, pushkw), (fetch, fetchkw), (gh, ghkw) = ran
    assert push == ["git", "-C", str(tmp_path), "push", "-q", KM.push_url(), "kb/x"]
    assert fetch == ["git", "-C", str(tmp_path), "fetch", "--quiet", KM.push_url(), KM.MAIN_REFSPEC]
    assert gh == ["gh", "pr", "list"] and ghkw["cwd"] == str(tmp_path)
    push_basic = base64.b64encode(b"x-access-token:ghs_PUSH").decode()
    for kw in (pushkw, fetchkw):
        assert kw["env"]["GIT_CONFIG_VALUE_1"] == f"Authorization: Basic {push_basic}"
        assert "GH_TOKEN" not in kw["env"]
        assert "ghs_PR" not in "".join(kw["env"].values())
    assert ghkw["env"]["GH_TOKEN"] == "ghs_PR"
    assert "ghs_PUSH" not in "".join(ghkw["env"].values()) and push_basic not in "".join(ghkw["env"].values())


def test_the_gh_runner_scrubs_the_pr_token_from_what_it_returns(agentctl, monkeypatch, tmp_path):
    """`kb propose` prints and journals gh's stderr on failure."""
    _fake_runner(agentctl, monkeypatch, stderr=f"HTTP 422 with token {TOKEN}")
    r = agentctl._KbLibrary("ghs_PUSH", TOKEN).gh(tmp_path, "pr", "create")
    assert TOKEN not in r.stderr and "HTTP 422" in r.stderr


def test_an_identity_obtained_without_a_pr_token_cannot_run_gh(agentctl, monkeypatch, tmp_path):
    """`kb apply` and `kb sync` ask for the push token alone, so nothing they hold
    can open or edit a PR, even by a bug."""
    ran = _fake_runner(agentctl, monkeypatch)
    with pytest.raises(RuntimeError):
        agentctl._KbLibrary("ghs_PUSH").gh(tmp_path, "pr", "list")
    assert ran == []


def test_the_pr_token_is_requested_only_when_the_caller_asks_for_it(agentctl, monkeypatch, tmp_path):
    asked = []
    monkeypatch.setattr(KM, "push_credential", lambda *a, **k: asked.append("push") or "ghs_PUSH")
    monkeypatch.setattr(KM, "read_credential", lambda *a, **k: asked.append("read") or "ghs_READ")
    monkeypatch.setattr(KM, "push_and_pr_credentials",
                        lambda *a, **k: asked.append("pair") or ("ghs_PUSH", "ghs_PR"))
    ran = _fake_runner(agentctl, monkeypatch)
    with pytest.raises(RuntimeError):
        agentctl._kb_library().gh(tmp_path, "pr", "list")
    agentctl._kb_library(pull_requests=True).gh(tmp_path, "pr", "list")
    assert asked == ["push", "pair"] and len(ran) == 1


def test_a_read_only_identity_asks_for_the_read_token_and_can_fetch_but_not_run_gh(
        agentctl, monkeypatch, tmp_path):
    """`kb sync` and `kb checkout` only fetch, so what they hold is `contents:
    read` and nothing that opens a PR. The fetch authenticates with that token."""
    asked = []
    monkeypatch.setattr(KM, "push_credential", lambda *a, **k: asked.append("push") or "ghs_PUSH")
    monkeypatch.setattr(KM, "read_credential", lambda *a, **k: asked.append("read") or "ghs_READ")
    ran = _fake_runner(agentctl, monkeypatch)
    lib = agentctl._kb_library(read_only=True)
    lib.fetch_main(tmp_path)
    read_basic = base64.b64encode(b"x-access-token:ghs_READ").decode()
    assert asked == ["read"]
    assert ran[0][1]["env"]["GIT_CONFIG_VALUE_1"] == f"Authorization: Basic {read_basic}"
    with pytest.raises(RuntimeError):
        lib.gh(tmp_path, "pr", "list")
    with pytest.raises(ValueError):
        agentctl._kb_library(pull_requests=True, read_only=True)


def test_a_read_only_identity_scrubs_its_token_from_what_git_returns(
        agentctl, monkeypatch, tmp_path):
    monkeypatch.setattr(KM, "read_credential", lambda *a, **k: TOKEN)
    _fake_runner(agentctl, monkeypatch, stderr=f"fatal: HTTP 403 {TOKEN} Basic {BASIC}")
    r = agentctl._kb_library(read_only=True).fetch_main(tmp_path)
    assert TOKEN not in r.stderr and BASIC not in r.stderr and "HTTP 403" in r.stderr


def test_the_suite_cannot_obtain_either_identity(agentctl):
    """conftest points KEY_PATH at nothing, so an un-stubbed caller refuses."""
    for kwargs in ({}, {"pull_requests": True}, {"read_only": True}):
        with pytest.raises(KM.Unreadable) as e:
            agentctl._kb_library(**kwargs)
        assert str(e.value).startswith("App key: no App key at")
