"""The KB write audit — the compensating control for self-attested provenance.

ARCH-408 accepted `in-session` writes on the condition that they get read back in
batch. This is that read-back, plus the bypass check: the invariant is that every
commit on main arrives through the cage or as a PR merge, and a commit that is
neither is something nothing else in the platform would notice.
"""
from __future__ import annotations

import base64
import json
import subprocess
import urllib.error
from pathlib import Path

import pytest

import kb_audit

TRAILER = "KB-Write: update {ref} {prov} {holder}"
SCRIBE = ("Applied via `agentctl scribe apply` (ARCH-048/081 cage). "
          "Provenance: {prov}. Guards: target under knowledge root.")


def _git(repo: Path, *a):
    return subprocess.run(["git", "-C", str(repo), *a],
                          capture_output=True, text=True, check=False)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "kb"
    r.mkdir()
    _git(r, "init", "--quiet", "--initial-branch=main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "T")
    (r / "projects" / "company" / "briefs").mkdir(parents=True)
    (r / "projects/company/briefs/seed.md").write_text("x")
    _git(r, "add", "-A")
    # The seed carries a trailer so the fixture starts from a CLEAN baseline.
    # An unsigned seed is itself a bypass by the audit's definition — correctly,
    # which is why it has to be signed here rather than filtered in assertions.
    _git(r, "commit", "--quiet", "-m",
         "seed\n\n" + TRAILER.format(ref="projects/company/briefs/seed.md",
                                       prov="generated", holder="run-seed"))
    return r


def _path_for(subject: str, prov: str = "") -> str:
    """The repo-relative doc a fixture commit writes.

    Two rules the fixtures must respect, because corroboration checks them and
    would otherwise flag the fixtures themselves: a trailer must NAME the file
    its commit actually changed, and `generated` may only touch a registered
    generator path. A fixture that ignores either is a forgery by the audit's
    own definition — which is the point, so it is modelled honestly here and
    violated deliberately in the forgery tests.
    """
    slug = subject[:20].replace(" ", "-").replace("/", "-")
    if prov.startswith("generated"):
        return f"projects/company/briefs/{slug}.md"
    return f"projects/vivy/decisions/{slug}.md"


def _commit(repo: Path, subject: str, body_tmpl: str = "", *, prov: str = "",
            holder: str = "run-1", ref: str | None = None):
    rel = _path_for(subject, prov)
    f = repo / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(subject)
    body = body_tmpl.format(prov=prov, holder=holder, ref=ref or rel) if body_tmpl else ""
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", f"{subject}\n\n{body}" if body else subject)
    return rel


# --- classification ----------------------------------------------------------


def test_a_cage_commit_is_recognised_by_its_trailer():
    out = kb_audit.classify("docs: x", TRAILER.format(ref="projects/vivy/decisions/0001-x.md", prov="in-session", holder="run-7"),
                            is_merge=False)
    assert out["path"] == "cage"
    assert out["provenance"] == "in-session"
    assert out["holder"] == "run-7"


def test_a_legacy_scribe_commit_is_not_reported_as_a_bypass():
    """The legacy cage predates the trailer and signs in prose. Counting those as
    bypasses would have produced 33 false positives in the first real week, and
    an audit that cries wolf teaches everyone to skip it — which is strictly
    worse than not having one."""
    out = kb_audit.classify("docs: x", SCRIBE.format(prov="agreed in-session (ARCH-081)"),
                            is_merge=False)
    assert out["path"] == "scribe"
    assert out["provenance"] == "in-session"


def test_the_legacy_verifier_provenance_maps_onto_the_same_vocabulary():
    """One vocabulary in the report whichever cage wrote the commit, so a reader
    need not know which era it came from to know whether it was attested."""
    out = kb_audit.classify("docs: x", SCRIBE.format(prov="kb-sync-verifier PASS (run-3)"),
                            is_merge=False)
    assert out["provenance"] == "verifier-pass"


@pytest.mark.parametrize("prov", ["steward", "keel"])
def test_a_legacy_steward_or_keel_trailer_is_still_recognised_by_its_trailer(prov):
    """platform/decisions/0081 took `steward` and `keel` off the direct-write path
    on 2026-09-13, but the audit judges a commit by the policy in force when it was
    made — not by today's policy. Neither value's history changes retroactively."""
    out = kb_audit.classify(
        "docs: x", TRAILER.format(ref="projects/vivy/decisions/0001-x.md",
                                   prov=prov, holder="run-7"),
        is_merge=False)
    assert out["path"] == "cage"
    assert out["provenance"] == prov


def test_a_merge_is_the_pr_path():
    out = kb_audit.classify("Merge pull request #12", "", is_merge=True)
    assert out["path"] == "pr" and out["provenance"] is None


def test_an_unsigned_commit_is_a_bypass():
    out = kb_audit.classify("docs: something", "no signature here", is_merge=False)
    assert out["path"] == "unsigned"


# --- the walk ----------------------------------------------------------------


def test_audit_separates_attested_writes_from_bypasses(repo):
    _commit(repo, "cage in-session", TRAILER, prov="in-session")
    _commit(repo, "cage generated", TRAILER, prov="generated")
    _commit(repo, "legacy scribe", SCRIBE, prov="agreed in-session (ARCH-081)")
    _commit(repo, "a bypass", "pushed straight to main")

    res = kb_audit.audit(since="30.days", ref="main", root=repo)

    assert [c["subject"] for c in res["unsigned"]] == ["a bypass"]
    attested = sorted(c["subject"] for c in res["attested"])
    assert attested == ["cage in-session", "legacy scribe"]
    # `generated` rests on a path allowlist, not an attestation, so it is counted
    # but not surfaced for review. Three cage writes: the two here and the
    # fixture's signed seed.
    assert res["counts"]["cage"] == 3


def test_a_merged_pr_counts_once_rather_than_per_branch_commit(repo):
    """First-parent, deliberately. A PR's individual commits were reviewed as
    part of that PR; listing them would swamp the report with work that already
    had eyes on it."""
    _git(repo, "checkout", "-q", "-b", "feature")
    _commit(repo, "branch work one")
    _commit(repo, "branch work two")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "--no-ff", "-q", "feature", "-m", "Merge pull request #1")

    res = kb_audit.audit(since="30.days", ref="main", root=repo)
    assert res["counts"].get("pr") == 1
    assert res["unsigned"] == [], "branch commits must not read as bypasses"


def test_an_unreadable_ref_reports_rather_than_raising(repo):
    res = kb_audit.audit(since="30.days", ref="no-such-ref", root=repo)
    assert res["error"] and res["commits"] == []


# --- forged claims -----------------------------------------------------------
#
# The trailer is prose and nothing binds it to the commit, so a writer with push
# access can forge one. The dangerous forgery is NOT a fake `in-session` — that
# still surfaces in the attested list where a human skims it — but a fake
# non-attested provenance, which would land in neither list and only bump a
# count. Corroboration answers each by checking the claim against what the commit
# actually changed (security review, #248).


def test_a_forged_generated_claim_on_a_path_no_generator_owns_is_a_bypass(repo):
    """The evasion named in review: claim `generated`, land in no list."""
    rel = "projects/vivy/decisions/smuggled.md"
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text("smuggled content")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m",
         "docs: innocuous\n\n" + TRAILER.format(ref=rel, prov="generated", holder="run-fake"))

    res = kb_audit.audit(since="30.days", ref="main", root=repo)
    assert [c["sha"] for c in res["unsigned"]], "a forged `generated` claim vanished"
    assert "no registered generator owns" in res["unsigned"][0]["why"]


@pytest.mark.parametrize("rel", [
    "projects/company/briefs/ai-transformation-briefing-2026-07-31.md",
    "projects/platform/briefs/ai-engineering-briefing-2026-07-31.md",
    # The step-5 destinations, granted before the briefs move (PR #341).
    "projects/company/briefs/ai-transformation-briefing-2026-08-01.md",
    "projects/platform/briefs/ai-engineering-briefing-2026-08-01.md",
])
def test_an_honest_generated_write_to_a_registered_brief_path_is_sanctioned(repo, rel):
    """The other half of the check above, and the half that decides whether the
    audit cries wolf. `generated` is bounded by path; a write INSIDE the bound must
    read as sanctioned, at the new locations as well as the old.

    Without this, widening the cage for step 5 would have left every legitimate
    brief written to `company/` or `platform/` reported as a bypass — and this
    module's docstring names that as worse than no audit, because it turns a real
    signal into noise someone learns to skip (security-reviewer, PR #341)."""
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text("the day's brief")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m",
         "docs: brief\n\n" + TRAILER.format(ref=rel, prov="generated", holder="run-real"))

    res = kb_audit.audit(since="30.days", ref="main", root=repo)
    assert not res["unsigned"], (
        f"a legitimate generated write to {rel} was reported as a bypass: "
        f"{res['unsigned'][0]['why'] if res['unsigned'] else ''}")


def test_a_trailer_naming_a_file_the_commit_did_not_touch_is_a_bypass(repo):
    """A forger's easiest move is to name some innocuous doc while changing
    something else. The claim is checkable against the commit itself."""
    rel = "projects/vivy/decisions/actually-changed.md"
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text("the real change")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m",
         "docs: looks fine\n\n" + TRAILER.format(
             ref="projects/vivy/decisions/something-else.md",
             prov="verifier-pass", holder="run-fake"))

    res = kb_audit.audit(since="30.days", ref="main", root=repo)
    assert res["unsigned"], "a trailer naming an untouched file vanished"
    assert "did not change" in res["unsigned"][0]["why"]


@pytest.mark.parametrize("rel", [
    "projects/agent-platform/agents/reviewer.md",
    "projects/knowledge/profiles/daytime.yaml",
    # A roster path in a project this list does not name. PR G made the guard a
    # `/agents/` SEGMENT, so the audit follows the roster wherever it is re-filed
    # instead of needing an edit here first — which is the failure the step-5
    # re-file would otherwise have produced twice, once in the cage and once here.
    "projects/some-future-project/agents/reviewer.md",
    # Case variants. macOS is case-insensitive, so these open the same inodes as
    # the lowercase forms; the audit's own copy of this rule compared
    # case-SENSITIVELY and waved them through until it started calling
    # kb_writes.is_pr_only (PR #341).
    "projects/AGENT-PLATFORM/AGENTS/reviewer.md",
    "projects/Agent-Platform/Agents/reviewer.md",
])
def test_a_pr_only_path_outside_a_pr_is_a_bypass_whatever_it_claims(repo, rel):
    """The strongest check, because it is an invariant rather than a heuristic:
    roster and profile paths are PR-only under EVERY provenance, so arriving any
    other way is unsanctioned no matter what the trailer says."""
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text("changed")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m",
         "docs: roster tweak\n\n" + TRAILER.format(ref=rel, prov="verifier-pass",
                                                   holder="run-fake"))

    res = kb_audit.audit(since="30.days", ref="main", root=repo)
    assert res["unsigned"], f"a direct write to {rel} was not flagged"
    assert "PR-only path" in res["unsigned"][0]["why"]


def test_a_forged_scribe_signature_still_faces_the_path_invariant(repo):
    """The legacy prose signature is even easier to forge than the trailer, so
    the invariant check runs before the cage/scribe distinction matters."""
    rel = "projects/agent-platform/agents/coder.md"
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text("changed")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m",
         "docs: x\n\n" + SCRIBE.format(prov="kb-sync-verifier PASS (run-fake)"))

    res = kb_audit.audit(since="30.days", ref="main", root=repo)
    assert res["unsigned"] and "PR-only path" in res["unsigned"][0]["why"]


def test_a_second_file_smuggled_beside_a_legitimate_ref_is_a_bypass(repo):
    """`ref in changed` alone leaves "the ref matches AND there is more" — name a
    legitimate doc, smuggle a second file alongside it, pass every other check.

    The counter is an invariant rather than a guess: both cages stage exactly one
    path, so a multi-file commit carrying a cage signature could not have been
    written by one. Confirmed across 60 days of the real corpus (security review,
    #248).
    """
    named = "projects/vivy/decisions/legitimate.md"
    smuggled = "projects/vivy/decisions/smuggled.md"
    for rel in (named, smuggled):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("content")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m",
         "docs: legitimate\n\n" + TRAILER.format(ref=named, prov="in-session",
                                                 holder="run-1"))

    res = kb_audit.audit(since="30.days", ref="main", root=repo)
    assert res["unsigned"], "a smuggled second file passed unnoticed"
    assert "stages one file" in res["unsigned"][0]["why"]


def test_a_signature_on_an_empty_commit_is_not_accepted(repo):
    """It wrote nothing, so no content bypassed anything — but the signature is
    still a false claim, and the audit should not accept one."""
    _git(repo, "commit", "--quiet", "--allow-empty", "-m",
         "docs: nothing\n\n" + TRAILER.format(ref="projects/vivy/decisions/x.md",
                                              prov="verifier-pass", holder="run-fake"))
    res = kb_audit.audit(since="30.days", ref="main", root=repo)
    assert res["unsigned"] and "changed no files" in res["unsigned"][0]["why"]


def test_an_unsigned_commit_is_not_relabelled_by_the_single_file_check(repo):
    """An already-unsigned commit must keep its own reason. Reporting "a cage
    commit stages one file" for something carrying no signature at all would
    describe the wrong problem."""
    for rel in ("projects/vivy/decisions/a.md", "projects/vivy/decisions/b.md"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "docs: two files, no signature")

    res = kb_audit.audit(since="30.days", ref="main", root=repo)
    assert res["unsigned"]
    assert not res["unsigned"][0].get("why"), "an unsigned commit gained a cage-specific reason"


def test_an_honest_cage_write_is_not_flagged(repo):
    """The other half: corroboration must not turn legitimate writes into noise,
    which is the failure that would make the whole report ignorable."""
    _commit(repo, "honest generated", TRAILER, prov="generated")
    _commit(repo, "honest in-session", TRAILER, prov="in-session")
    res = kb_audit.audit(since="30.days", ref="main", root=repo)
    assert res["unsigned"] == []


@pytest.mark.parametrize("prov", ["steward", "keel"])
def test_a_historical_steward_or_keel_write_still_reads_as_sanctioned(repo, prov):
    """8 `KB-Write:` trailers already on the knowledge repo's main name `steward`
    and 3 journal rows name `keel` — commits that were legitimate direct writes
    under the policy in force when they landed. platform/decisions/0081 refuses
    new callers of either, but must not turn that old, honest history into a
    reported bypass."""
    _commit(repo, f"legacy {prov} write", TRAILER, prov=prov)
    res = kb_audit.audit(since="30.days", ref="main", root=repo)
    assert res["unsigned"] == []
    assert prov not in [c["provenance"] for c in res["attested"]], (
        f"`{prov}` is not `in-session` and must not be reported as attested")


# --- `person` ----------------------------------------------------------------

PERSON = TRAILER + "\nKB-Person: {person}"


def _person_commit(repo, subject, body):
    rel = _path_for(subject)
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(subject)
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m",
         f"{subject}\n\n" + body.format(ref=rel, prov="person", holder="operator-example-com"))
    return rel


def test_a_person_write_with_its_person_line_is_attested_and_names_who(repo):
    _person_commit(repo, "typed in the editor",
                   PERSON.replace("{person}", "operator@example.com"))
    res = kb_audit.audit(since="30.days", ref="main", root=repo)
    assert res["unsigned"] == []
    assert [c["subject"] for c in res["attested"]] == ["typed in the editor"]
    assert res["attested"][0]["person"] == "operator@example.com"
    assert "person=operator@example.com" in kb_audit.render(res)


@pytest.mark.parametrize("body", [
    TRAILER,
    # A line with a second token is not a well-formed `KB-Person:` line.
    PERSON.replace("{person}", "operator@example.com extra"),
    # Garbage that is one token but not an email.
    PERSON.replace("{person}", "x"),
    PERSON.replace("{person}", "-"),
    PERSON.replace("{person}", "null"),
    # Control and bidi characters must never reach the report.
    PERSON.replace("{person}", "operator@example.com\x1b[31m"),
    PERSON.replace("{person}", "\x1b[2Joperator@example.com"),
    PERSON.replace("{person}", "operator@example.com\u202e"),
    # Exactly one line: two valid ones, or a valid one beside a garbage one.
    PERSON.replace("{person}", "operator@example.com") + "\nKB-Person: other@example.com",
    PERSON.replace("{person}", "operator@example.com") + "\nKB-Person: x",
    "prose\nKB-Person: other@example.com\n" + PERSON.replace("{person}", "operator@example.com"),
])
def test_a_person_claim_without_a_well_formed_person_line_is_a_bypass(repo, body):
    _person_commit(repo, "forged person", body)
    res = kb_audit.audit(since="30.days", ref="main", root=repo)
    assert [c["subject"] for c in res["unsigned"]] == ["forged person"]
    assert "KB-Person" in res["unsigned"][0]["why"]


def test_only_a_person_commit_carries_a_person(repo):
    _commit(repo, "agent word", TRAILER + "\nKB-Person: operator@example.com", prov="in-session")
    res = kb_audit.audit(since="30.days", ref="main", root=repo)
    assert res["unsigned"] == []
    assert [c["subject"] for c in res["attested"]] == ["agent word"]
    assert not res["attested"][0].get("person")
    assert "person=" not in kb_audit.render(res)


def test_the_report_says_claim_not_fact(repo):
    _person_commit(repo, "typed", PERSON.replace("{person}", "operator@example.com"))
    text = kb_audit.render(kb_audit.audit(since="30.days", ref="main", root=repo))
    assert "claim" in text and "these reached main" not in text


def test_an_in_session_commit_is_unaffected_by_the_person_check(repo):
    _commit(repo, "plain in-session", TRAILER, prov="in-session")
    res = kb_audit.audit(since="30.days", ref="main", root=repo)
    assert res["unsigned"] == []
    assert [c["subject"] for c in res["attested"]] == ["plain in-session"]


# --- the report --------------------------------------------------------------


def test_the_report_leads_with_the_thing_needing_action(repo):
    _commit(repo, "a bypass", "nothing")
    _commit(repo, "attested one", TRAILER, prov="in-session")
    text = kb_audit.render(kb_audit.audit(since="30.days", ref="main", root=repo))

    assert text.index("BYPASSED") < text.index("WRITTEN ON ATTESTATION")
    assert "a bypass" in text
    # The report names why the existing guard does not catch these, because the
    # obvious reaction is "doesn't ARCH-395 stop that?" — and it does not.
    assert "not pushing TO it" in text


def test_bypasses_exit_3_not_2(agentctl, repo, monkeypatch, capsys):
    """3, because argparse already owns 2 for a usage error.

    The first live run proved why: the deployed binary had no `kb audit`
    subcommand yet, argparse exited 2, and the launcher read that as BYPASSES
    FOUND and logged a security finding for a command that never ran. An audit
    that manufactures a finding out of its own failure is worse than one that
    does not run.
    """
    from argparse import Namespace
    monkeypatch.setattr(agentctl, "KNOWLEDGE_ROOT", repo)

    _commit(repo, "a bypass", "unsigned")
    assert agentctl.cmd_kb_audit(Namespace(since="30.days", ref="main", json=False)) == 3

    # And a clean window is 0, so the non-zero genuinely means something.
    _git(repo, "reset", "--hard", "--quiet", "HEAD~1")
    _commit(repo, "cage write", TRAILER, prov="generated")
    assert agentctl.cmd_kb_audit(Namespace(since="30.days", ref="main", json=False)) == 0


def test_a_clean_window_says_so_rather_than_printing_nothing(repo):
    _commit(repo, "cage write", TRAILER, prov="generated")
    text = kb_audit.render(kb_audit.audit(since="30.days", ref="main", root=repo))
    assert "WRITTEN ON ATTESTATION (0) — nothing to review." in text
    assert "BYPASSED" not in text


# --- who could write ----------------------------------------------------
#
# `audit_access` never touches the network itself — `get` is injected, so
# these tests stub GitHub's responses and make no network call of their own
# (the task's own gate). `FakeGitHub` models exactly the recorded-baseline
# state from issue_01KZQA9FK7F6GZS4E4NCQRH0YB's comments 298 and 307, so the
# "everything matches" test is the control every drift test perturbs by
# exactly one fact.

_CLEAN_WORKFLOW_YAML = "name: ci\non: [push]\njobs:\n  build:\n    runs-on: ubuntu-latest\n"


class FakeGitHub:
    """A `get(path) -> (code, payload)` stand-in for `github_app.request`,
    seeded at the recorded-baseline state. Each drift test mutates exactly one
    field before calling `audit_access`."""

    def __init__(self):
        self.collaborators = [{"login": "operator-login", "role_name": "admin"}]
        self.teams = []
        self.workflow_permissions = {"default_workflow_permissions": "read",
                                      "can_approve_pull_request_reviews": False}
        self.default_branch = "main"
        self.workflow_files = {"ci.yml": _CLEAN_WORKFLOW_YAML}
        self.installations = [
            {"app_slug": "render", "id": 1, "repository_selection": "all",
             "permissions": {"contents": "read", "workflows": "write"},
             "suspended_at": None},
            {"app_slug": "claude-design-import", "id": 2, "repository_selection": "all",
             "permissions": {"contents": "read"}, "suspended_at": None},
            {"app_slug": "cloudflare-workers-and-pages", "id": 3,
             "repository_selection": "selected", "permissions": {"contents": "write"},
             "suspended_at": None},
            {"app_slug": "claude", "id": 4, "repository_selection": "all",
             "permissions": {"contents": "write"}, "suspended_at": "2026-08-11T14:54:35Z"},
        ]
        # Non-200 overrides: path prefix -> (code, payload), checked before the
        # normal routing below, so a test can simulate a surface GitHub refused.
        self.overrides: dict[str, tuple[int, object]] = {}

    def get(self, path: str):
        for prefix, response in self.overrides.items():
            if path.startswith(prefix):
                return response
        if path == "/repos/convivy/knowledge/collaborators":
            return 200, self.collaborators
        if path == "/repos/convivy/knowledge/teams":
            return 200, self.teams
        if path == "/repos/convivy/knowledge/actions/permissions/workflow":
            return 200, self.workflow_permissions
        if path == "/repos/convivy/knowledge":
            return 200, {"default_branch": self.default_branch}
        if path == f"/repos/convivy/knowledge/contents/.github/workflows?ref={self.default_branch}":
            return 200, [{"name": name} for name in self.workflow_files]
        if path.startswith("/repos/convivy/knowledge/contents/.github/workflows/"):
            name = path.split("/workflows/", 1)[1].split("?", 1)[0]
            content = self.workflow_files.get(name)
            if content is None:
                return 404, {"message": "Not Found"}
            return 200, {"content": base64.b64encode(content.encode()).decode()}
        if path == "/orgs/convivy/installations?per_page=100":
            return 200, {"total_count": len(self.installations), "installations": self.installations}
        raise AssertionError(f"FakeGitHub: unexpected path {path!r}")


def test_access_audit_passes_when_everything_matches_the_recorded_expectation():
    res = kb_audit.audit_access(FakeGitHub().get)
    assert res["findings"] == []
    assert res["unreadable"] == []


def test_an_added_collaborator_is_reported():
    gh = FakeGitHub()
    gh.collaborators.append({"login": "someone-else", "role_name": "write"})
    res = kb_audit.audit_access(gh.get)
    assert any("collaborators" in f["surface"] for f in res["findings"])
    assert res["unreadable"] == []


def test_a_missing_github_login_reports_collaborators_unconfigured_not_clean(monkeypatch, tmp_path):
    cfg = tmp_path / "operator.json"
    cfg.write_text(json.dumps({"excluded_crews": [], "roster_crew": "x", "operator_marker": "x"}))
    monkeypatch.setenv("CONVIVY_OPERATOR_CONFIG", str(cfg))
    gh = FakeGitHub()
    res = kb_audit.audit_access(gh.get)
    assert any("collaborators" in u["surface"] for u in res["unreadable"])
    assert not any("collaborators" in f["surface"] for f in res["findings"])


def test_a_team_with_repository_access_is_reported():
    gh = FakeGitHub()
    gh.teams = [{"slug": "engineering"}]
    res = kb_audit.audit_access(gh.get)
    assert any(f["surface"] == "teams" and "engineering" in f["why"] for f in res["findings"])


@pytest.mark.parametrize("flip", [
    {"default_workflow_permissions": "write"},
    {"can_approve_pull_request_reviews": True},
])
def test_a_workflow_permission_flip_is_reported(flip):
    gh = FakeGitHub()
    gh.workflow_permissions.update(flip)
    res = kb_audit.audit_access(gh.get)
    assert any(f["surface"] == "workflow permissions" for f in res["findings"])


@pytest.mark.parametrize("yaml_text", [
    "name: ci\non: [push]\npermissions:\n  contents: write\njobs:\n  build:\n    runs-on: ubuntu-latest\n",
    "name: ci\non: [push]\npermissions: write-all\njobs:\n  build:\n    runs-on: ubuntu-latest\n",
    "name: ci\non: [push]\njobs:\n  build:\n    runs-on: ubuntu-latest\n    permissions:\n      contents: write\n",
])
def test_a_workflow_granting_contents_write_is_reported(yaml_text):
    gh = FakeGitHub()
    gh.workflow_files["ci.yml"] = yaml_text
    res = kb_audit.audit_access(gh.get)
    assert any(f["surface"] == "workflow files" and "ci.yml" in f["why"] for f in res["findings"])


def test_an_unparseable_workflow_files_multiline_yaml_error_renders_on_one_line():
    """`yaml.safe_load`'s own error text is multi-line by design (it quotes
    the offending snippet with a `^` marker on its own line) — real content
    from a workflow file in convivy/knowledge, which only someone who already
    has write access to the audited repo could plant. Collapsed before
    `render_access` sees it, so the rendered report cannot be made to look
    like several lines of the audit's own output (security-reviewer, PR #803
    round 2)."""
    gh = FakeGitHub()
    gh.workflow_files["ci.yml"] = ("name: ci\non: [push]\njobs:\n  build:\n"
                                   "    runs-on: ubuntu-latest\n  bad: [unterminated")
    res = kb_audit.audit_access(gh.get)
    hit = next(u for u in res["unreadable"] if u["surface"] == "workflow files")
    assert "\n" not in hit["why"]
    text = kb_audit.render_access(res)
    report_lines = text.splitlines()
    # The finding renders as exactly one of the report's lines, not several —
    # find it by its lead-in text and confirm nothing past it still belongs
    # to the same finding.
    matches = [i for i, line in enumerate(report_lines) if "could not parse ci.yml" in line]
    assert len(matches) == 1
    assert "while parsing" in report_lines[matches[0]]
    assert "expected" in report_lines[matches[0]]


def test_flatten_collapses_every_character_splitlines_treats_as_a_line_break():
    """`_flatten` has to match the boundary `str.splitlines()` uses, not a
    hand-picked list — that mismatch is exactly how `\\x1c`-`\\x1e` (File/Group
    Separator) survived round 2's fix into round 3 (reviewer and
    security-reviewer, PR #803 round 3). Generated from `splitlines()` itself
    rather than a list of characters, so the test can't share the same blind
    spot as the code it's checking."""
    boundary_chars = [chr(cp) for cp in range(0x3001)
                       if len(("a" + chr(cp) + "b").splitlines()) > 1]
    assert len(boundary_chars) >= 10  # sanity: the generator found the known set
    for ch in boundary_chars:
        flattened = kb_audit._flatten(f"line one{ch}line two")
        assert len(flattened.splitlines()) == 1, f"U+{ord(ch):04X} still splits _flatten's output"


def test_a_workflow_files_name_containing_a_line_separator_byte_renders_on_one_line():
    """Git allows any byte but NUL and `/` in a tree-entry name, so a workflow
    file's name is as much GitHub-sourced, attacker-influenced text as the
    YAML error text above — and `\\x1c` (File Separator) is one of the three
    bytes that rode through `_flatten` unflattened in round 3 because only the
    YAML-error vector had a regression test (security-reviewer, PR #803
    round 3)."""
    gh = FakeGitHub()
    del gh.workflow_files["ci.yml"]
    gh.workflow_files["evil\x1cfile.yml"] = ("name: ci\non: [push]\npermissions:\n"
                                              "  contents: write\njobs:\n  build:\n"
                                              "    runs-on: ubuntu-latest\n")
    res = kb_audit.audit_access(gh.get)
    hit = next(f for f in res["findings"] if f["surface"] == "workflow files")
    assert len(hit["why"].splitlines()) == 1
    text = kb_audit.render_access(res)
    report_lines = text.splitlines()
    matches = [i for i, line in enumerate(report_lines) if "evil" in line and "file.yml" in line]
    assert len(matches) == 1


def test_a_workflow_granting_contents_read_is_not_reported():
    """The other half: a read-only or absent permissions block must not be
    flagged, or the report becomes noise nobody trusts (see the module
    docstring's "cries wolf" reasoning for the write-history half of this
    file)."""
    gh = FakeGitHub()
    gh.workflow_files["ci.yml"] = ("name: ci\non: [push]\npermissions:\n  contents: read\n"
                                   "jobs:\n  build:\n    runs-on: ubuntu-latest\n")
    res = kb_audit.audit_access(gh.get)
    assert res["findings"] == []


def test_a_repo_with_no_workflows_directory_is_not_unreadable():
    gh = FakeGitHub()
    gh.overrides[f"/repos/convivy/knowledge/contents/.github/workflows?ref={gh.default_branch}"] = \
        (404, {"message": "Not Found"})
    res = kb_audit.audit_access(gh.get)
    assert res["unreadable"] == []
    assert res["findings"] == []


def test_a_new_installation_is_reported_whatever_its_permissions():
    gh = FakeGitHub()
    gh.installations.append({"app_slug": "convivy-bosun", "id": 99,
                             "repository_selection": "all",
                             "permissions": {"contents": "read"}, "suspended_at": None})
    res = kb_audit.audit_access(gh.get)
    assert any("new installation" in f["why"] and "convivy-bosun" in f["why"]
              for f in res["findings"])


def test_a_widened_installation_permission_is_reported():
    gh = FakeGitHub()
    render = next(i for i in gh.installations if i["app_slug"] == "render")
    render["permissions"]["contents"] = "write"
    res = kb_audit.audit_access(gh.get)
    assert any(f["surface"] == "installations" and "render" in f["why"]
              and "permissions.contents" in f["why"] for f in res["findings"])


def test_an_unsuspended_claude_is_reported_as_critical_and_sorts_first():
    gh = FakeGitHub()
    claude = next(i for i in gh.installations if i["app_slug"] == "claude")
    claude["suspended_at"] = None
    res = kb_audit.audit_access(gh.get)
    hit = [f for f in res["findings"] if f["surface"] == "installations" and "claude" in f["why"]
          and "suspended" in f["why"]]
    assert hit and hit[0].get("critical") is True

    # Add an unrelated, non-critical drift too, so the ordering claim is real
    # rather than coincidental with only one finding present.
    gh.teams = [{"slug": "engineering"}]
    res = kb_audit.audit_access(gh.get)
    text = kb_audit.render_access(res)
    assert text.index("UN-SUSPENDED") < text.index("teams:")


@pytest.mark.parametrize("surface,path", [
    ("collaborators", "/repos/convivy/knowledge/collaborators"),
    ("teams", "/repos/convivy/knowledge/teams"),
    ("workflow permissions", "/repos/convivy/knowledge/actions/permissions/workflow"),
    ("installations", "/orgs/convivy/installations?per_page=100"),
])
def test_an_unreadable_surface_is_reported_not_passed(surface, path):
    gh = FakeGitHub()
    gh.overrides[path] = (403, {"message": "Resource not accessible by integration"})
    res = kb_audit.audit_access(gh.get)
    assert res["unreadable"] and res["unreadable"][0]["surface"] == surface
    # An unreadable surface must never also present as a clean (no-finding)
    # pass — `render_access` has to say so, not just the structured result.
    text = kb_audit.render_access(res)
    assert "COULD NOT READ" in text


def test_the_org_installations_endpoint_being_unreachable_is_exactly_what_the_convivy_bosun_app_sees_live():
    """Not a stub of a hypothetical: `GET /orgs/convivy/installations` with a
    live convivy-bosun installation token returns exactly this 403 today
    (verified 2026-10-07) — the App holds no organization-level permission.
    Pinning the real shape here, rather than a generic 403, keeps this test
    from silently drifting away from what the credential this job actually
    uses can and cannot read."""
    gh = FakeGitHub()
    gh.overrides["/orgs/convivy/installations?per_page=100"] = (
        403, {"message": "Resource not accessible by integration",
             "documentation_url": "https://docs.github.com/rest/orgs/orgs"
                                   "#list-app-installations-for-an-organization",
             "status": "403"})
    res = kb_audit.audit_access(gh.get)
    assert res["unreadable"] == [{"surface": "installations",
                                  "why": "GET /orgs/convivy/installations returned 403"}]


def test_a_truncated_installations_page_is_unreadable_not_silently_short():
    gh = FakeGitHub()
    gh.overrides["/orgs/convivy/installations?per_page=100"] = (
        200, {"total_count": 7, "installations": gh.installations})  # 4 real entries, claims 7
    res = kb_audit.audit_access(gh.get)
    assert any(u["surface"] == "installations" and "unread page" in u["why"]
              for u in res["unreadable"])


def test_render_access_says_everything_matched_when_nothing_is_wrong():
    text = kb_audit.render_access(kb_audit.audit_access(FakeGitHub().get))
    assert "nothing to review" in text
    assert "COULD NOT READ" not in text
    assert "DRIFT" not in text


def test_a_network_error_on_one_surface_is_unreadable_not_a_crash():
    """`github_app.request` catches only `HTTPError`; a `URLError` (DNS
    failure, refused connection, timeout) used to escape `audit_access`
    uncaught and crash `main()` with Python's default exit 1 — a code the
    job's exit-code table does not reserve for this (reviewer, PR #803 round
    1). It must land in `unreadable` for the one surface that failed, exactly
    like a non-200 response, and leave every other surface's read unaffected."""
    gh = FakeGitHub()
    real_get = gh.get

    def flaky_get(path: str):
        if path == "/repos/convivy/knowledge/teams":
            raise urllib.error.URLError("Connection refused")
        return real_get(path)

    res = kb_audit.audit_access(flaky_get)
    assert any(u["surface"] == "teams" and "network error" in u["why"]
              for u in res["unreadable"])
    # The surface that raised is reported unreadable, not silently clean, and
    # every other surface still read normally.
    assert not any(f["surface"] == "teams" for f in res["findings"])
    assert not any(u["surface"] != "teams" for u in res["unreadable"])
    text = kb_audit.render_access(res)
    assert "COULD NOT READ" in text


def test_a_read_phase_timeout_on_one_surface_is_unreadable_not_a_crash():
    """`github_app.request`'s `try` wraps `urlopen` and `resp.read()`
    together, but a timeout that expires during `resp.read()` (the connection
    succeeds; the response is merely slow to finish arriving) raises a bare
    `TimeoutError`, not `urllib.error.URLError` — so the round-1 fix's
    `except urllib.error.URLError` left this trigger uncaught, crashing
    `main()` with Python's default exit 1 rather than landing in
    `unreadable` (security-reviewer, PR #803 round 2)."""
    gh = FakeGitHub()
    real_get = gh.get

    def flaky_get(path: str):
        if path == "/repos/convivy/knowledge/teams":
            raise TimeoutError("timed out")
        return real_get(path)

    res = kb_audit.audit_access(flaky_get)
    assert any(u["surface"] == "teams" and "network error" in u["why"]
              for u in res["unreadable"])
    assert not any(f["surface"] == "teams" for f in res["findings"])
    assert not any(u["surface"] != "teams" for u in res["unreadable"])


def test_main_scopes_the_minted_token_to_knowledge_with_read_only_permissions(monkeypatch):
    """`kb_merge.py` already scopes this same App's token to the one repo it
    acts on (lib/agent-platform/kb_merge.py:666-668); `main()` minted its token
    with no `repositories=`, so it read with contents/metadata:read across
    every repository the org-wide installation can see rather than
    `convivy/knowledge` alone (reviewer + security-reviewer, PR #803 round 1).

    `github_app` is stubbed end to end — `app_jwt`, `single_installation` and
    `installation_token` never run — so this never mints a real token or
    calls GitHub.
    """
    import github_app as GA

    monkeypatch.setattr(GA, "key_problem", lambda *a, **k: None)
    monkeypatch.setattr(GA, "app_jwt", lambda *a, **k: "stub-jwt")
    monkeypatch.setattr(GA, "single_installation", lambda *a, **k: {"id": 123})

    calls: dict = {}

    def fake_installation_token(jwt, installation_id, **kwargs):
        calls["kwargs"] = kwargs
        return "stub-token"

    monkeypatch.setattr(GA, "installation_token", fake_installation_token)
    monkeypatch.setattr(kb_audit, "audit_access",
                        lambda get, **k: {"findings": [], "unreadable": []})

    rc = kb_audit.main([])

    assert rc == 0
    assert calls["kwargs"]["repositories"] == ["knowledge"]
    assert calls["kwargs"]["permissions"] == {"contents": "read", "metadata": "read"}
