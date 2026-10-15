"""Tests for bin/dependabot-review.

The classifier decides whether a bump costs a security review, so it is the part
worth testing hard: a false 'changelog' silently skips the security pass on
something that terminates HTTP, and a false 'full' burns a review on a test
runner. Both failures are quiet.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

BIN = pathlib.Path(__file__).resolve().parent.parent / "bin" / "dependabot-review"


def _load():
    spec = importlib.util.spec_from_loader("dependabot_review", None)
    mod = importlib.util.module_from_spec(spec)
    mod.__dict__["__file__"] = str(BIN)
    exec(compile(BIN.read_text(), str(BIN), "exec"), mod.__dict__)  # noqa: S102
    return mod


dr = _load()


# --- parsing --------------------------------------------------------------

def test_parses_a_grouped_body():
    body = ("Bumps the routine group with 3 updates:\n\n"
            "Updates `uvicorn` from 0.51.0 to 0.52.0\n"
            "Updates `pytest` from 8.1.0 to 8.1.1\n"
            "Updates `ruff` from 0.5.0 to 0.5.1\n")
    got = dr.parse_updates("deps: bump the routine group", body)
    assert {p.lower() for p, _, _ in got} == {"uvicorn", "pytest", "ruff"}


def test_falls_back_to_the_title_for_a_single_bump():
    got = dr.parse_updates("Bump uvicorn from 0.51.0 to 0.52.0", "")
    assert got == [("uvicorn", "0.51.0", "0.52.0")]


def test_body_wins_over_title_and_does_not_duplicate():
    got = dr.parse_updates("Bump uvicorn from 0.51.0 to 0.52.0",
                           "Updates `uvicorn` from 0.51.0 to 0.52.0")
    assert len(got) == 1


def test_unparseable_body_yields_nothing_rather_than_guessing():
    assert dr.parse_updates("something entirely different", "no versions here") == []


# --- major detection ------------------------------------------------------

@pytest.mark.parametrize("lo,hi,want", [
    ("1.2.3", "2.0.0", True),
    ("0.51.0", "0.52.0", False),
    ("1.2.3", "1.3.0", False),
    ("v1.0.0", "v2.0.0", True),
    ("9.9.9", "10.0.0", True),
    ("2.0.0", "2.0.1", False),
])
def test_major_jump(lo, hi, want):
    assert dr._major_jump(lo, hi) is want


def test_unparseable_version_is_not_treated_as_major():
    # Guessing 'major' here would spend a security review on noise, and the PR
    # still reaches a human either way.
    assert dr._major_jump("abc", "def") is False
    assert dr._major_jump("", "1.0.0") is False


# --- classification -------------------------------------------------------

def test_routine_patch_trips_neither_axis():
    sec, brk = dr.classify("deps: bump the routine group",
                           "Updates `pytest` from 8.1.0 to 8.1.1", [])
    assert sec == [] and brk == []


def test_request_path_package_is_a_security_reason():
    sec, brk = dr.classify("deps: bump uvicorn",
                           "Updates `uvicorn` from 0.51.0 to 0.52.0", [])
    assert any("request path" in r for r in sec)
    assert brk == []


def test_a_major_bump_is_BREAKING_not_security():
    # The whole point of splitting the axes. A major bump of a test matcher or an
    # icon library is a breaking-change risk, and sending a security-reviewer at
    # it spends a security review on a risk that is not a security risk.
    sec, brk = dr.classify("deps: bump lucide-react",
                           "Updates `lucide-react` from 0.441.0 to 1.27.0", [])
    assert sec == []
    assert any("MAJOR" in r for r in brk)


def test_a_major_bump_of_a_sensitive_package_trips_both():
    sec, brk = dr.classify("deps: bump cryptography",
                           "Updates `cryptography` from 42.0.0 to 43.0.0", [])
    assert any("cryptography" in r for r in sec)
    assert any("MAJOR" in r for r in brk)


def test_a_security_label_is_a_security_reason():
    sec, _ = dr.classify("deps: bump pytest",
                         "Updates `pytest` from 8.1.0 to 8.1.1", ["security"])
    assert any("advisory" in r for r in sec)


def test_one_sensitive_package_in_a_large_group_pulls_the_whole_group_up():
    # The machine-global rule's case: review a group by its contents, not as one
    # unit. A group is not safe because it is routine.
    body = ("Updates `pytest` from 8.1.0 to 8.1.1\n"
            "Updates `ruff` from 0.5.0 to 0.5.1\n"
            "Updates `cryptography` from 42.0.0 to 42.0.1\n"
            "Updates `rich` from 13.0.0 to 13.0.1\n")
    sec, _ = dr.classify("deps: bump the routine group", body, [])
    assert any("cryptography" in r for r in sec)


def test_matching_is_case_insensitive():
    sec, _ = dr.classify("x", "Updates `Jinja2` from 3.1.0 to 3.1.1", [])
    assert sec


def test_a_near_miss_name_does_not_match_a_sensitive_package():
    # `starlette-extras` is not `starlette`; catching it would be a false security.
    sec, _ = dr.classify("x", "Updates `starlette-extras` from 1.0.0 to 1.0.1", [])
    assert sec == []


def test_an_unknown_package_trips_nothing():
    sec, brk = dr.classify("x", "Updates `some-internal-lib` from 1.0.0 to 1.0.1", [])
    assert sec == [] and brk == []


# --- task strings ---------------------------------------------------------

def _pr(n=1, sha="a" * 40):
    return {"number": n, "headRefOid": sha, "title": "t", "body": "b"}


def test_changelog_task_tells_the_reviewer_to_be_brief_and_not_merge():
    task = dr._task("vivy", _pr(), [], [],
                    [("pytest", "8.1.0", "8.1.1")], security=False)
    assert "CHANGELOG PASS" in task
    assert "do not merge" in task.lower()
    assert "Do not pad this" in task


def test_security_task_names_the_surface_and_forbids_fast_bail():
    task = dr._task("vivy", _pr(),
                    ["uvicorn terminates HTTP on the request path"], [],
                    [("uvicorn", "0.51.0", "0.52.0")], security=True)
    assert "terminates HTTP on the request path" in task
    assert "fast-bail" in task
    assert "do not merge" in task.lower()


def test_breaking_task_asks_for_migration_notes_and_says_no_security_pass_ran():
    # A reviewer sent at a major must know a security-reviewer did NOT run, so it
    # can flag a surface the classifier missed rather than assuming it is covered.
    task = dr._task("vivy", _pr(), [],
                    ["lucide-react is a MAJOR bump (0.441.0 to 1.27.0)"],
                    [("lucide-react", "0.441.0", "1.27.0")], security=False)
    assert "BREAKING CHANGES" in task
    assert "no security-reviewer was dispatched" in task
    assert "call sites" in task


def test_the_reviewer_on_a_security_pr_still_gets_the_breaking_note_if_both_apply():
    task = dr._task("vivy", _pr(),
                    ["cryptography implements the crypto primitives"],
                    ["cryptography is a MAJOR bump (42.0.0 to 43.0.0)"],
                    [("cryptography", "42.0.0", "43.0.0")], security=True)
    assert "crypto primitives" in task


def test_every_task_lists_the_packages_it_covers():
    task = dr._task("vivy", _pr(), [], [],
                    [("pytest", "8.1.0", "8.1.1"), ("ruff", "0.5.0", "0.5.1")],
                    security=False)
    assert "pytest: 8.1.0 -> 8.1.1" in task
    assert "ruff: 0.5.0 -> 0.5.1" in task


# --- the sensitive list itself -------------------------------------------

def test_sensitive_entries_all_carry_a_reason():
    # The reason travels into the reviewer's prompt, so an empty one would
    # produce a review told to look at nothing in particular.
    for pkg, (_category, why) in dr.SENSITIVE.items():
        assert why and why[0].islower(), pkg
        assert pkg == pkg.lower(), pkg


# --- the CI gate ----------------------------------------------------------

def test_pending_ci_is_reported_as_pending(monkeypatch):
    monkeypatch.setattr(dr, "_gh", lambda *a, **k: json.dumps(
        {"statusCheckRollup": [
            {"status": "COMPLETED", "conclusion": "SUCCESS"},
            {"status": "IN_PROGRESS", "conclusion": None},
        ]}))
    assert dr.ci_state("vivy", 1) == "pending"


def test_a_failing_check_is_red(monkeypatch):
    monkeypatch.setattr(dr, "_gh", lambda *a, **k: json.dumps(
        {"statusCheckRollup": [
            {"status": "COMPLETED", "conclusion": "SUCCESS"},
            {"status": "COMPLETED", "conclusion": "FAILURE"},
        ]}))
    assert dr.ci_state("vivy", 1) == "red"


def test_all_complete_and_successful_is_green(monkeypatch):
    monkeypatch.setattr(dr, "_gh", lambda *a, **k: json.dumps(
        {"statusCheckRollup": [
            {"status": "COMPLETED", "conclusion": "SUCCESS"},
            {"status": "COMPLETED", "conclusion": "SKIPPED"},
        ]}))
    assert dr.ci_state("vivy", 1) == "green"


def test_no_checks_at_all_is_unknown_not_green(monkeypatch):
    # "unknown" rather than "green" on purpose: a repo whose checks have not
    # been created yet must not read as a pass.
    monkeypatch.setattr(dr, "_gh", lambda *a, **k: json.dumps(
        {"statusCheckRollup": []}))
    assert dr.ci_state("vivy", 1) == "unknown"


def test_a_gh_failure_is_unknown_and_does_not_raise(monkeypatch):
    monkeypatch.setattr(dr, "_gh", lambda *a, **k: "")
    assert dr.ci_state("vivy", 1) == "unknown"


def test_red_ci_makes_diagnosis_the_reviewer_s_job():
    # The night this shipped, the one red PR produced the most useful review of
    # the lot (lucide-react 1.x adding aria-hidden broke a nav test). Red earns a
    # review; pending does not.
    task = dr._task("vivy", _pr(), [], ["x is a MAJOR bump (0.4 to 1.2)"],
                    [("x", "0.4", "1.2")], security=False, ci="red")
    assert "CI IS RED" in task
    assert "Do not simply" in task


def test_green_ci_adds_no_red_preamble():
    task = dr._task("vivy", _pr(), [], [], [("x", "1.0", "1.1")],
                    security=False, ci="green")
    assert "CI IS RED" not in task


# --- failing closed -------------------------------------------------------

def test_an_unparseable_pr_is_routed_to_a_FULL_review_not_a_cheap_one():
    # The security review's finding: if the parse yields nothing, no sensitive
    # package can match and a uvicorn bump reads as routine. "I cannot tell what
    # this changes" must not resolve to "it is fine".
    sec, brk = dr.classify("something dependabot has never written",
                           "a body in a format this tool does not know", [])
    assert sec, "an unreadable PR must earn a security review"
    assert any("could not be parsed" in r for r in sec)


def test_an_empty_pr_does_not_trip_the_unparseable_rule():
    # No title and no body is a degenerate input, not a format change; treating
    # it as unparseable would dispatch a security review at nothing.
    sec, brk = dr.classify("", "", [])
    assert sec == [] and brk == []


def test_gh_failure_returns_None_and_is_not_an_empty_result(monkeypatch):
    monkeypatch.setattr(dr.subprocess, "run",
                        lambda *a, **k: type("P", (), {"returncode": 1,
                                                       "stdout": "",
                                                       "stderr": "bad credentials"})())
    assert dr._gh(["pr", "list"], "vivy") is None


def test_a_failed_listing_is_None_rather_than_zero_prs(monkeypatch):
    # The difference that matters: None means "unreviewed and unknown", [] means
    # "genuinely nothing to do". Conflating them hid a fleet-wide auth failure.
    monkeypatch.setattr(dr, "_gh", lambda *a, **k: None)
    assert dr.open_prs("vivy") is None


def test_an_empty_listing_is_an_empty_list(monkeypatch):
    monkeypatch.setattr(dr, "_gh", lambda *a, **k: "[]")
    assert dr.open_prs("vivy") == []


def test_unparseable_listing_json_is_None(monkeypatch):
    monkeypatch.setattr(dr, "_gh", lambda *a, **k: "not json")
    assert dr.open_prs("vivy") is None


# --- rollup shapes --------------------------------------------------------

def test_a_pending_StatusContext_is_pending_not_green(monkeypatch):
    # A rollup mixes CheckRun (status/conclusion) with StatusContext (state).
    # Reading only the CheckRun fields treats a pending external status as green,
    # which is the wasted-round bug by another route.
    monkeypatch.setattr(dr, "_gh", lambda *a, **k: json.dumps(
        {"statusCheckRollup": [
            {"__typename": "CheckRun", "status": "COMPLETED",
             "conclusion": "SUCCESS", "state": None},
            {"__typename": "StatusContext", "status": None,
             "conclusion": None, "state": "PENDING"},
        ]}))
    assert dr.ci_state("vivy", 1) == "pending"


def test_a_failed_StatusContext_is_red(monkeypatch):
    monkeypatch.setattr(dr, "_gh", lambda *a, **k: json.dumps(
        {"statusCheckRollup": [
            {"__typename": "StatusContext", "status": None,
             "conclusion": None, "state": "FAILURE"},
        ]}))
    assert dr.ci_state("vivy", 1) == "red"


def test_a_successful_StatusContext_is_green(monkeypatch):
    monkeypatch.setattr(dr, "_gh", lambda *a, **k: json.dumps(
        {"statusCheckRollup": [
            {"__typename": "StatusContext", "status": None,
             "conclusion": None, "state": "SUCCESS"},
        ]}))
    assert dr.ci_state("vivy", 1) == "green"


@pytest.mark.parametrize("pkg", [
    "python-multipart", "aiohttp", "stripe", "twilio",
])
def test_packages_added_in_review_are_security_reasons(pkg):
    sec, _ = dr.classify("x", f"Updates `{pkg}` from 1.0.0 to 1.0.1", [])
    assert sec, f"{pkg} should earn a security review"


def test_alembic_is_not_a_security_trigger():
    # It applies developer-authored migrations, so no untrusted input reaches it
    # and it matches none of the rule's four categories. Its real risk is data
    # integrity, which neither axis covers; see the comment where it was removed.
    sec, _ = dr.classify("x", "Updates `alembic` from 1.13.0 to 1.13.1", [])
    assert sec == []


def test_every_sensitive_package_declares_a_category_the_rule_states():
    """Every package must declare a category from the CLOSED RULE_CATEGORIES set.

    The previous version of this guard matched reasons by SUBSTRING against a
    hand-maintained list living here, in the test. Widening that list was easier
    than amending the rule, so it happened: five categories sat in this file that
    the rule never named, and the guard ratified the drift it existed to catch.

    A closed set moves the decision into the tool, where adding a category is a
    visible one-line diff a reviewer sees, rather than a substring appended to a
    tuple in a test nobody re-reads. Amend the rule first; RULE_CATEGORIES second.
    """
    for pkg, entry in dr.SENSITIVE.items():
        assert isinstance(entry, tuple) and len(entry) == 2, pkg
        category, why = entry
        assert category in dr.RULE_CATEGORIES, (
            f"{pkg} declares category {category!r}, which the rule does not state. "
            f"Amend machine-global-CLAUDE.md first, then RULE_CATEGORIES.")


def test_a_package_with_an_unstated_category_fails_the_guard(monkeypatch):
    # The guard must actually guard. Without this, a green suite proves nothing
    # about whether the closed set is enforced.
    monkeypatch.setitem(dr.SENSITIVE, "some-pkg", ("invented-category", "does a thing"))
    with pytest.raises(AssertionError, match="which the rule does not state"):
        test_every_sensitive_package_declares_a_category_the_rule_states()


def test_every_stated_category_is_used_by_at_least_one_package():
    # A category in the rule that no package uses is drift the other way: the
    # rule promises coverage the tool does not deliver.
    used = {c for c, _ in dr.SENSITIVE.values()}
    unused = set(dr.RULE_CATEGORIES) - used
    assert not unused, f"rule states categories no package uses: {sorted(unused)}"


# --- the printed merge gate -----------------------------------------------

def test_the_printed_gate_names_the_reviewers_dispatched_for_each_tier(monkeypatch, capsys):
    # `agentctl review-status` defaults to requiring BOTH reviewers, so a
    # changelog- or breaking-tier PR (which only ever dispatches a reviewer)
    # reads FAIL on the merge gate while carrying a clean APPROVE. The printed
    # command must pass --required for exactly the reviewers dispatched, so the
    # operator checks the gate the tier actually asked for.
    monkeypatch.setattr(dr, "is_dispatch_target", lambda r: True)
    monkeypatch.setattr(dr, "open_prs", lambda repo: [
        {"number": 1109, "title": "deps: bump the routine group",
         "body": "Updates `pytest` from 8.1.0 to 8.1.1",
         "headRefOid": "a" * 40, "labels": [], "isDraft": False},
        {"number": 1110, "title": "deps: bump lucide-react",
         "body": "Updates `lucide-react` from 0.441.0 to 1.27.0",
         "headRefOid": "b" * 40, "labels": [], "isDraft": False},
        {"number": 1111, "title": "deps: bump uvicorn",
         "body": "Updates `uvicorn` from 0.51.0 to 0.52.0",
         "headRefOid": "c" * 40, "labels": [], "isDraft": False},
    ])
    monkeypatch.setattr(dr, "reviewed_at_head", lambda *a, **k: False)
    monkeypatch.setattr(dr, "ci_state", lambda *a, **k: "green")
    assert dr.main(["--repo", "vivy", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert ("would-be gate: agentctl review-status --pr 1109 "
            "--repo convivy/vivy --required reviewer") in out
    assert ("would-be gate: agentctl review-status --pr 1110 "
            "--repo convivy/vivy --required reviewer") in out
    assert ("would-be gate: agentctl review-status --pr 1111 "
            "--repo convivy/vivy --required reviewer --required security-reviewer") in out


def test_a_failed_security_reviewer_dispatch_drops_it_from_the_gate(monkeypatch, capsys):
    # The gate is derived from the reviewers ACTUALLY dispatched, not from the
    # tier. If the security-reviewer spawn fails (a slot never frees), the
    # printed command must not claim it, or the operator's check would PASS on
    # a security review that never happened.
    monkeypatch.setattr(dr, "is_dispatch_target", lambda r: True)
    monkeypatch.setattr(dr, "open_prs", lambda repo: [
        {"number": 1111, "title": "deps: bump uvicorn",
         "body": "Updates `uvicorn` from 0.51.0 to 0.52.0",
         "headRefOid": "c" * 40, "labels": [], "isDraft": False},
    ])
    monkeypatch.setattr(dr, "reviewed_at_head", lambda *a, **k: False)
    monkeypatch.setattr(dr, "ci_state", lambda *a, **k: "green")
    attempted: list[str] = []

    def fake_dispatch(repo, agent, pr, task, dry):
        attempted.append(agent)
        return agent == "reviewer"  # the security-reviewer spawn fails

    monkeypatch.setattr(dr, "dispatch", fake_dispatch)
    assert dr.main(["--repo", "vivy"]) == 0
    out = capsys.readouterr().out
    assert attempted == ["reviewer", "security-reviewer"]
    assert ("gate: agentctl review-status --pr 1111 --repo convivy/vivy "
            "--required reviewer") in out


# --- homebrew-tap: the registered owner is convivybuilder, not convivy ---

def test_task_names_the_registered_owner_for_homebrew_tap():
    """Round-1 review (PR #881, comment 6150937461): `_task` built
    `convivy/{repo}` directly — the same pattern the PR centralizes
    everywhere else."""
    task = dr._task("homebrew-tap", _pr(), [], [],
                    [("pytest", "8.1.0", "8.1.1")], security=False)
    assert "convivybuilder/homebrew-tap" in task


def test_gate_command_names_the_registered_owner_for_homebrew_tap():
    assert dr._gate_command("homebrew-tap", 1, ["reviewer"]) == (
        "agentctl review-status --pr 1 --repo convivybuilder/homebrew-tap "
        "--required reviewer")


def test_gh_queries_the_registered_owner_for_homebrew_tap(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return type("P", (), {"returncode": 0, "stdout": "[]", "stderr": ""})()

    monkeypatch.setattr(dr.subprocess, "run", fake_run)
    dr._gh(["pr", "list"], "homebrew-tap")

    assert calls and "--repo" in calls[0]
    repo_arg = calls[0][calls[0].index("--repo") + 1]
    assert repo_arg == "convivybuilder/homebrew-tap"
