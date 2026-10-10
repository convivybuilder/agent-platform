"""`issue-queue-drain` — the unattended pass that drains the fleet's issue queue.

The properties worth pinning are the ones that decide whether an unwatched run is
safe to leave alone: it reads the queue as `claimable` so a died-mid-flight run
recovers, it names everything it skipped rather than letting an unread queue look
like an empty one, and it stays bounded so a nightly pass cannot become an
open-ended spend.

The checkout guard was removed in ARCH-391 (2026-07-29): planning-orchestrator now
runs in a per-task dedicated worktree, so no dispatch path touches the primary
checkout and the guard has no surface to protect.
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
# The per-dispatch directory the drain creates; the brief names files inside it.
RUN_DIR = Path("/tmp/issue-queue-drain-run")


@pytest.fixture
def qd(issue_queue_drain):
    return issue_queue_drain


@pytest.fixture(autouse=True)
def _gate_passthrough(issue_queue_drain, monkeypatch):
    """Neutralize the in-flight gate for every test that is not about the gate.

    `_claimable` runs `issue_gate.eligible_issues` per product (ARCH-418), which
    does a real `git ls-remote` against ~/Convivy/<product> and FAILS CLOSED when
    that is not a checkout. These tests use synthetic product names, so without
    this every one of them would see an empty queue and pass or fail for a reason
    that has nothing to do with what it is asserting.

    The gate's own behaviour is covered in test_issue_gate.py, and its
    integration here is covered by the tests that opt out of this fixture below.
    """
    monkeypatch.setattr(issue_queue_drain.issue_gate, "eligible_issues",
                        lambda product, items=None, **kw: (list(items or []), []))


def _issue(iid, product="vivy", state="open"):
    return {"id": f"issue_{iid}", "product": product, "state": state,
            "title": f"thing {iid}", "type": "bug"}


# --- reading the queue -------------------------------------------------------


def test_the_queue_is_read_as_claimable_not_open(qd, monkeypatch):
    """The whole recoverability story rests on this one parameter. `open` would
    hide every issue a dead run had claimed until a human noticed."""
    seen = {}

    def fake_get(path, params):
        seen["path"], seen["params"] = path, params
        return []

    monkeypatch.setattr(qd, "_get", fake_get)
    qd._claimable()
    assert seen["path"] == "/api/issues"
    assert seen["params"]["state"] == "claimable"


def test_issues_group_by_product(qd, monkeypatch):
    monkeypatch.setattr(qd, "_get", lambda p, q: [
        _issue(1, "vivy"), _issue(2, "vivy"), _issue(3, "selector")])
    by_product, warnings = qd._claimable()
    assert {k: len(v) for k, v in by_product.items()} == {"vivy": 2, "selector": 1}
    assert warnings == []


def test_an_issue_with_no_product_is_dropped(qd, monkeypatch):
    """A product is how the drain picks a checkout to dispatch into; a row without
    one cannot be worked, and grouping it under "" would produce a spawn --on ''."""
    monkeypatch.setattr(qd, "_get", lambda p, q: [_issue(1, ""), _issue(2, "vivy")])
    by_product, _ = qd._claimable()
    assert list(by_product) == ["vivy"]


def test_a_truncated_read_warns_but_does_not_refuse(qd, monkeypatch):
    """Unlike changelog-drain's entries read, a prefix here cannot cause duplicate
    work — claims are atomic, so the remainder is simply picked up next pass."""
    monkeypatch.setattr(qd, "_get",
                        lambda p, q: [_issue(i) for i in range(qd._API_PAGE_CAP)])
    by_product, warnings = qd._claimable()
    assert by_product
    assert any("prefix" in w for w in warnings)



# --- skips are named ---------------------------------------------------------


def test_a_product_with_no_checkout_is_skipped_by_name(qd, monkeypatch, capsys):
    """A queue with no consumer looks exactly like an empty queue, so the skip
    must carry the product name rather than only a count."""
    monkeypatch.setattr(qd, "_get", lambda p, q: [_issue(1, "ghost")])
    monkeypatch.setattr(qd, "_dispatchable", lambda p: False)
    spawned = []
    monkeypatch.setattr(qd, "_spawn", lambda *a, **k: spawned.append(a) or True)
    qd.main([])
    assert spawned == []
    assert "ghost" in capsys.readouterr().err


def test_products_deferred_by_the_bound_are_named(qd, monkeypatch, capsys):
    monkeypatch.setattr(qd, "_get", lambda p, q: [
        _issue(1, "a"), _issue(2, "b"), _issue(3, "c")])
    monkeypatch.setattr(qd, "_dispatchable", lambda p: True)
    monkeypatch.setattr(qd, "_spawn", lambda *a, **k: True)
    qd.main(["--max-products", "1"])
    err = capsys.readouterr().err
    assert "deferring" in err and "b" in err and "c" in err


# --- bounds ------------------------------------------------------------------


def test_the_pass_dispatches_at_most_max_products(qd, monkeypatch):
    monkeypatch.setattr(qd, "_get", lambda p, q: [
        _issue(i, f"p{i}") for i in range(6)])
    monkeypatch.setattr(qd, "_dispatchable", lambda p: True)
    spawned = []
    monkeypatch.setattr(qd, "_spawn",
                        lambda product, task, detach, **kw: spawned.append(product) or True)
    qd.main(["--max-products", "2"])
    assert len(spawned) == 2


def test_the_deepest_queue_is_drained_first(qd, monkeypatch):
    """A bounded pass should buy the most it can, so the product with the most
    claimable issues goes first."""
    monkeypatch.setattr(qd, "_get", lambda p, q: [
        _issue(1, "shallow"), _issue(2, "deep"), _issue(3, "deep"), _issue(4, "deep")])
    monkeypatch.setattr(qd, "_dispatchable", lambda p: True)
    spawned = []
    monkeypatch.setattr(qd, "_spawn",
                        lambda product, task, detach, **kw: spawned.append(product) or True)
    qd.main(["--max-products", "1"])
    assert spawned == ["deep"]


def test_the_brief_carries_the_per_run_issue_bound(qd):
    task = qd._task_for("vivy", 9, 3, work_dir=RUN_DIR)
    assert "AT MOST 3 ISSUES" in task
    assert "state=\"claimable\"" in task
    assert "Do not raise the bound yourself" in task


def test_the_brief_forbids_mark_fresh(qd):
    """mark-fresh is issue-drain's daytime cost-shift bookkeeping: it tells the next
    overnight to skip its bug-fix phase. This drain runs ALONGSIDE the overnight, so
    suppressing that phase here would silently cost a product its overnight bug pass."""
    assert "Do NOT run `mark-fresh`" in qd._task_for("vivy", 1, 1, work_dir=RUN_DIR)


def test_the_brief_forbids_merging(qd):
    """ARCH-020 — no agent merges, and this one runs unattended overnight."""
    assert "do NOT merge" in qd._task_for("vivy", 1, 1, work_dir=RUN_DIR)


def test_the_bug_fixer_dispatches_carry_issue_and_the_reviewers_do_not(qd):
    """The bug-fixer dispatches (step 5, and the revise round in step 8) name the item
    with --issue, so the run records the item it serves (E16·4: a bug-fixer's cost joins
    to its work item through meta.issue). The reviewers do not: --issue applies the
    item's model choice to a dispatch, and a reviewer's cost joins to the item through
    its PR. Each command is asserted on its own."""
    task = qd._task_for("vivy", 1, 1, work_dir=RUN_DIR)
    for command in (
        "agentctl spawn bug-fixer --on vivy --issue <issue_id> --task",
        "agentctl spawn bug-fixer --on vivy --pr <N> --issue <issue_id> --task",
        "agentctl spawn reviewer --on vivy --pr <N>`",
        "agentctl spawn security-reviewer --on vivy --pr <N>`",
    ):
        assert command in task, command
    for command in (
        "agentctl spawn reviewer --on vivy --pr <N> --issue",
        "agentctl spawn security-reviewer --on vivy --pr <N> --issue",
    ):
        assert command not in task, command


def _brief_from_main(qd, monkeypatch, run_dir, argv=()):
    """The brief `main` hands `_spawn` for one product, with the drain's directory fixed
    to run_dir. Driving main keeps these tests on the public path, so they also run
    against code that predates the directory."""
    monkeypatch.setattr(qd, "_get", lambda p, q: [_issue(1, "vivy")])
    monkeypatch.setattr(qd, "_dispatchable", lambda p: True)
    run_dir.mkdir(parents=True)
    monkeypatch.setattr(tempfile, "mkdtemp", lambda prefix="": str(run_dir))
    briefs = []
    monkeypatch.setattr(qd, "_spawn",
                        lambda product, task, detach, **kw: briefs.append(task) or True)
    qd.main(list(argv))
    [task] = briefs
    return task


def _agentctl_exits(code):
    """A subprocess.run stand-in for agentctl that exits with `code`."""
    return lambda cmd: subprocess.CompletedProcess(
        cmd, code, "spawned run-abc123" if code == 0 else "", "" if code == 0 else "error: refused")


def _agentctl_times_out(cmd):
    raise subprocess.TimeoutExpired(cmd, 1800)


def _dispatch_from_main(qd, monkeypatch, run_dir, agentctl, argv=()):
    """Drive main through the real _spawn, with agentctl replaced by `agentctl`, so the
    directory's fate is decided by the dispatch's own outcome, as in production."""
    monkeypatch.setattr(qd, "_get", lambda p, q: [_issue(1, "vivy")])
    monkeypatch.setattr(qd, "_dispatchable", lambda p: True)
    monkeypatch.setattr(qd, "_agents_dir", lambda: None)
    monkeypatch.setattr(qd, "_wait_for_supervisor_slot", lambda: True)
    run_dir.mkdir(parents=True)
    monkeypatch.setattr(tempfile, "mkdtemp", lambda prefix="": str(run_dir))
    monkeypatch.setattr(qd.subprocess, "run", lambda cmd, **k: agentctl(cmd))
    return qd.main(list(argv))


def test_filer_and_reviewer_text_reaches_a_shell_only_through_a_file(qd, monkeypatch, tmp_path):
    """The issue's title and body, the suggested PR title and the reviewers' findings all
    derive from filer-written text, so none goes inside a single or a double shell quote.
    Each --task and --title is a command substitution over a file the orchestrator wrote."""
    task = _brief_from_main(qd, monkeypatch, tmp_path / "run")
    assert "--task '" not in task
    assert "--title '" not in task
    assert '--task "<' not in task
    assert '--title "<' not in task
    assert task.count('--task "$(cat ') == 2   # step 5, and the step 8 revise round
    assert task.count('--title "$(cat ') == 1  # step 6


def test_the_brief_writes_into_the_drain_run_directory_not_a_predictable_tmp_name(
        qd, monkeypatch, tmp_path):
    """A fixed /tmp/drain-<issue_id>-* name can be pre-placed by another local account, and
    a file from an earlier night makes the Write tool refuse. Each file goes in the
    directory the drain created for this run instead."""
    run_dir = tmp_path / "run"
    task = _brief_from_main(qd, monkeypatch, run_dir)
    assert "/tmp/drain-" not in task
    assert f"{run_dir}/<issue_id>-task.md" in task
    assert f"{run_dir}/<issue_id>-title.txt" in task


def test_each_revise_round_writes_its_own_findings_file(qd, monkeypatch, tmp_path):
    """A second revise round on one issue would overwrite a fixed findings name, and a
    refused second Write invites a shell fallback. Each round numbers its own file."""
    task = _brief_from_main(qd, monkeypatch, tmp_path / "run")
    assert f"{tmp_path / 'run'}/<issue_id>-findings-<round>.md" in task
    assert "<issue_id>-findings.md" not in task


REFUSED_WRITE_RULE = "REFUSED FILE WRITES. This rule governs steps 5, 6 and 8."


def test_the_refused_write_rule_precedes_step_5(qd, monkeypatch, tmp_path):
    """Steps 5, 6 and 8 all write filer or reviewer text, and step 5 is the first. The rule
    sits before step 5, so the orchestrator reads it before its first write, not only at
    the revise step."""
    task = _brief_from_main(qd, monkeypatch, tmp_path / "run")
    assert REFUSED_WRITE_RULE in task.split("5. Dispatch bug-fixer")[0]


def test_the_refused_write_rule_names_the_banned_shell_forms(qd, monkeypatch, tmp_path):
    """A refused Write is the likeliest path to a heredoc carrying filer text, so the rule
    names each banned form, says what to record after a refusal, and steps 5 and 6 release
    the claim. Step 8 points back to the rule rather than restating it."""
    task = _brief_from_main(qd, monkeypatch, tmp_path / "run")
    assert "the only permitted shell form is a command substitution over that file" in task
    assert "no heredoc (<<EOF)" in task
    assert "no `echo` or `printf`" in task
    assert "no literal quoted argument" in task
    assert "record the refusal with mcp__ops__issue_comment(" in task
    assert "In steps 5 and 6, also call mcp__ops__issue_release(" in task
    assert "A refused file write for this text follows the REFUSED FILE WRITES rule above." in task


def test_the_brief_quotes_the_run_directory_in_its_rm_instruction(qd, monkeypatch, tmp_path):
    """A TMPDIR with a space would make an unquoted path two arguments to rm."""
    run_dir = tmp_path / "run"
    task = _brief_from_main(qd, monkeypatch, run_dir)
    assert f'`rm -rf "{run_dir}"`' in task


def test_a_foreground_dispatch_removes_its_run_directory_once_it_returns(
        qd, monkeypatch, tmp_path):
    run_dir = tmp_path / "run"
    _dispatch_from_main(qd, monkeypatch, run_dir, _agentctl_exits(0), argv=["--foreground"])
    assert not run_dir.exists()


def test_a_detached_dispatch_keeps_its_run_directory_for_the_orchestrator(
        qd, monkeypatch, tmp_path):
    """A detached spawn returns before the orchestrator writes its files, so the drain
    must not remove the directory on return. The orchestrator removes it (brief step 9)."""
    run_dir = tmp_path / "run"
    _dispatch_from_main(qd, monkeypatch, run_dir, _agentctl_exits(0))
    assert run_dir.exists()


@pytest.mark.parametrize("argv", [[], ["--foreground"]])
def test_a_refused_dispatch_removes_its_run_directory(qd, monkeypatch, tmp_path, argv):
    """A refused spawn never started, so nothing reads the directory again."""
    run_dir = tmp_path / "run"
    _dispatch_from_main(qd, monkeypatch, run_dir, _agentctl_exits(1), argv=argv)
    assert not run_dir.exists()


@pytest.mark.parametrize("argv", [[], ["--foreground"]])
def test_a_timed_out_dispatch_keeps_its_run_directory(qd, monkeypatch, tmp_path, argv):
    """The 30-minute kill stops agentctl, and the orchestrator it started may still be
    writing its brief files. The directory stays with its filer text until the
    orchestrator's step 9 removes it."""
    run_dir = tmp_path / "run"
    _dispatch_from_main(qd, monkeypatch, run_dir, _agentctl_times_out, argv=argv)
    assert run_dir.exists()


def test_a_refused_bug_fixer_spawn_records_the_reason_and_releases_the_claim(qd):
    """Step 4 claims the item before step 5 dispatches it, so a refused spawn would
    otherwise leave the claim held for its TTL, and a malformed model choice would be
    re-claimed and refused every night. The brief names the refusal and both calls."""
    task = qd._task_for("vivy", 1, 1, work_dir=RUN_DIR)
    step5 = task[task.index("5. Dispatch"):task.index("6. Title, then publish")]
    assert "exits non-zero" in step5
    assert "mcp__ops__issue_comment(" in step5
    assert "mcp__ops__issue_release(" in step5
    assert step5.index("issue_comment(") < step5.index("issue_release(")


def test_a_refused_reviewer_dispatch_is_retried_once_then_recorded_as_unreviewed(qd):
    task = qd._task_for("vivy", 1, 1, work_dir=RUN_DIR)
    step8 = task[task.index("8. Dispatch"):task.index("9. Terminal state")]
    assert "re-dispatch that reviewer once" in step8
    assert "record the PR as unreviewed" in step8


def _drain_owner(run_dir):
    """The owner string `_task_for` builds for the vivy brief `_brief_from_main` returns."""
    return f"issue-queue-drain:vivy:{run_dir.name}"


def test_the_drain_checks_out_the_item_and_never_claims_it(qd, monkeypatch, tmp_path):
    """issue_claim records the claim against the shared ops token and cannot name the drain,
    so a release that names the drain would match nothing. The drain checks out under its
    own owner string instead."""
    run_dir = tmp_path / "issue-queue-drain-run"
    task = _brief_from_main(qd, monkeypatch, run_dir)
    assert f'mcp__ops__issue_checkout(issue_id, owner="{_drain_owner(run_dir)}")' in task
    assert "issue_claim" not in task


def test_every_owner_in_the_brief_is_the_checkout_owner(qd, monkeypatch, tmp_path):
    """Each release and link must present the owner the drain checked out under. A call
    with another owner reads as someone else's lease to the Library."""
    run_dir = tmp_path / "issue-queue-drain-run"
    task = _brief_from_main(qd, monkeypatch, run_dir)
    assert "<your session identity>" not in task
    assert set(re.findall(r'owner="([^"]*)"', task)) == {_drain_owner(run_dir)}
    releases = re.findall(r"issue_release\(([^)]*)\)", task)
    assert task.count("issue_release") == len(releases) > 0
    assert all(f'owner="{_drain_owner(run_dir)}"' in call for call in releases)


def test_the_title_file_is_written_before_publish(qd, monkeypatch, tmp_path):
    """A title write refused after `agentctl publish` would leave a pushed branch with no PR
    linked, and the next pass would dispatch bug-fixer again. Writing the title first stops
    the item before anything is pushed."""
    task = _brief_from_main(qd, monkeypatch, tmp_path / "run")
    assert "6. Title, then publish" in task
    step6 = task[task.index("6. Title, then publish"):task.index("7. Record the PR")]
    assert step6.index("-title.txt with your file-writing tool") < step6.index("agentctl publish")


# --- dry run -----------------------------------------------------------------


def test_dry_run_dispatches_nothing(qd, monkeypatch):
    monkeypatch.setattr(qd, "_get", lambda p, q: [_issue(1, "vivy")])
    monkeypatch.setattr(qd, "_dispatchable", lambda p: True)
    spawned = []
    monkeypatch.setattr(qd, "_spawn", lambda *a, **k: spawned.append(a) or True)
    assert qd.main(["--dry-run"]) == 0
    assert spawned == []


# --- dispatch ----------------------------------------------------------------


def test_the_dispatch_passes_agents_dir(qd, monkeypatch, tmp_path):
    """The six repo-local crews define no planning-orchestrator, so without this the
    drain fails against them exactly as changelog-drain did (40 of 88 merges)."""
    monkeypatch.setattr(qd, "_agents_dir", lambda: tmp_path)
    cmds = []

    class _P:
        returncode, stdout, stderr = 0, "spawned run-abc123", ""

    monkeypatch.setattr(qd.subprocess, "run", lambda cmd, **k: cmds.append(cmd) or _P())
    assert qd._spawn("selector", "task", detach=True, work_dir=tmp_path) is True
    assert "--agents-dir" in cmds[0]
    assert str(tmp_path) in cmds[0]


def test_the_dispatch_carries_no_pr_flag(qd, monkeypatch, tmp_path):
    """An issue has no PR until the drain opens one, and `--pr` refuses a merged or
    absent PR outright."""
    monkeypatch.setattr(qd, "_agents_dir", lambda: tmp_path)
    cmds = []

    class _P:
        returncode, stdout, stderr = 0, "spawned run-abc123", ""

    monkeypatch.setattr(qd.subprocess, "run", lambda cmd, **k: cmds.append(cmd) or _P())
    qd._spawn("vivy", "task", detach=True, work_dir=tmp_path)
    assert "--pr" not in cmds[0]


def test_a_failed_dispatch_is_counted_not_fatal(qd, monkeypatch):
    """One stuck or failed product must not abort the loop and silently skip every
    product behind it — changelog-drain's #250 lesson."""
    monkeypatch.setattr(qd, "_get", lambda p, q: [_issue(1, "a"), _issue(2, "b")])
    monkeypatch.setattr(qd, "_dispatchable", lambda p: True)
    tried = []

    def fake_spawn(product, task, detach, **kw):
        tried.append(product)
        return product != "a"

    monkeypatch.setattr(qd, "_spawn", fake_spawn)
    rc = qd.main(["--max-products", "2"])
    assert len(tried) == 2      # b was still attempted after a failed
    assert rc == 1              # and the pass reports the failure


def test_no_donor_omits_agents_dir_and_still_dispatches(qd, monkeypatch, tmp_path):
    """When no donor checkout exists, the flag is omitted rather than passed empty.
    Against a repo-local crew that dispatch then FAILS — loudly and counted, which
    is the acceptable outcome; a silent pass would not be."""
    monkeypatch.setattr(qd, "_agents_dir", lambda: None)
    cmds = []

    class _P:
        returncode, stdout, stderr = 0, "spawned run-abc123", ""

    monkeypatch.setattr(qd.subprocess, "run", lambda cmd, **k: cmds.append(cmd) or _P())
    assert qd._spawn("vivy", "task", detach=True, work_dir=tmp_path) is True
    assert "--agents-dir" not in cmds[0]
    assert "" not in cmds[0]


def test_a_nonzero_dispatch_is_a_counted_failure(qd, monkeypatch, tmp_path):
    """The no-donor path's failure mode: agentctl exits non-zero because it cannot
    resolve the agent, and that must register rather than read as success."""
    monkeypatch.setattr(qd, "_agents_dir", lambda: None)

    class _P:
        returncode, stdout, stderr = 1, "", "agent 'planning-orchestrator' not found"

    monkeypatch.setattr(qd.subprocess, "run", lambda cmd, **k: _P())
    assert qd._spawn("selector", "task", detach=True, work_dir=tmp_path) is False


# --- product-name validation (security review, #254) -------------------------


@pytest.mark.parametrize("bad", [
    "../sibling-repo", "..", ".", "../../etc", "vivy/../../tmp", ".hidden",
    "with space", "semi;colon", "-leading-dash",
])
def test_a_product_name_that_is_not_a_repo_directory_is_dropped(qd, monkeypatch, bad):
    """The name reaches a filesystem path in `_dispatchable` and is passed as-is to
    agentctl, BEFORE agentctl validates anything. So the drain sanitizes at ingest."""
    monkeypatch.setattr(qd, "_get", lambda p, q: [_issue(1, bad), _issue(2, "vivy")])
    by_product, warnings = qd._claimable()
    assert list(by_product) == ["vivy"]
    assert any("dropped" in w for w in warnings)


@pytest.mark.parametrize("ok", ["vivy", "sous-chef", "convivy-app-kit", "agent_platform",
                                "selector-backend", "a.b", "x1"])
def test_a_real_product_name_survives_validation(qd, monkeypatch, ok):
    monkeypatch.setattr(qd, "_get", lambda p, q: [_issue(1, ok)])
    by_product, warnings = qd._claimable()
    assert list(by_product) == [ok]
    assert warnings == []


def test_a_dropped_product_is_named_in_the_warning(qd, monkeypatch):
    """A rejected row is either an attack or a data defect, and both need a human to
    see WHICH issue carried it."""
    monkeypatch.setattr(qd, "_get", lambda p, q: [_issue("BAD", "../evil")])
    _, warnings = qd._claimable()
    assert any("issue_BAD" in w and "../evil" in w for w in warnings)


def test_a_traversing_product_never_reaches_dispatch(qd, monkeypatch):
    """End-to-end: a traversal must be gone before the plan loop runs, not merely
    before dispatch — _dispatchable is the gate, and it must run before _spawn."""
    monkeypatch.setattr(qd, "_get", lambda p, q: [_issue(1, "../sibling-repo")])
    spawned = []
    monkeypatch.setattr(qd, "_spawn", lambda *a, **k: spawned.append(a) or True)
    qd.main([])
    assert spawned == []


# --- type routing (ARCH-361's documented rule, implemented in the brief) ------


def test_the_brief_routes_on_type(qd):
    """ISSUE_AUTO_FIX_TYPES was defined in control_db and described in issue_api's
    docstring, and no code anywhere read it — so a `task` would have been auto-fixed.
    The brief is where the documented routing is implemented."""
    task = qd._task_for("vivy", 3, 3, work_dir=RUN_DIR)
    assert "ROUTE ON THE ISSUE'S `type`" in task
    assert "bug, nit, or improvement" in task
    assert "task or feature" in task
    assert "do NOT build it" in task
    assert "issue_escalate" in task


def test_the_brief_routes_a_blocked_task_or_feature_off_the_retired_decision_plane(qd):
    """The decision plane was retired 2026-08-18 (issue_01M0VP1DCE2ET0BT52SJYGW9RV):
    decision_create/list/consume are gone, and mcp__ops__issue_escalate 404s without
    a decision_id it can no longer obtain. A task/feature issue needing the operator's call
    can no longer be escalated that way, so the brief must route it through
    signal_log (a durable, searchable record) + issue_comment (visible on the
    thread) + issue_release (back to open, not stuck-claimed) instead — and must
    tell the agent NOT to call issue_escalate for this, since that call requires
    a decision_id there is no longer any way to obtain."""
    task = qd._task_for("vivy", 1, 1, work_dir=RUN_DIR)
    assert "decision_create" not in task
    assert "Do NOT call mcp__ops__issue_escalate" in task
    assert "mcp__ops__signal_log" in task
    assert "mcp__ops__issue_comment" in task
    assert "mcp__ops__issue_release" in task


def test_a_dispatch_timeout_is_one_failure(qd, monkeypatch, tmp_path):
    monkeypatch.setattr(qd, "_agents_dir", lambda: tmp_path)

    def boom(cmd, **k):
        raise qd.subprocess.TimeoutExpired(cmd, 1800)

    monkeypatch.setattr(qd.subprocess, "run", boom)
    assert qd._spawn("vivy", "task", detach=True, work_dir=tmp_path) is False


# --- unbounded by default (the operator's call, 2026-07-27) ---------------------------
#
# The bounds shipped at 3x3, which caps an unattended night at nine issues across
# three products. Against a queue that grows every time a reviewer files a finding,
# that does not converge. The operator's call is that the nightly clears the queue.

def test_every_product_is_dispatched_by_default(qd, monkeypatch):
    monkeypatch.setattr(qd, "_get", lambda p, q: [_issue(i, f"p{i}") for i in range(6)])
    monkeypatch.setattr(qd, "_dispatchable", lambda p: True)
    spawned = []
    monkeypatch.setattr(qd, "_spawn",
                        lambda product, task, detach, **kw: spawned.append(product) or True)
    qd.main([])
    assert len(spawned) == 6


def test_max_products_still_bounds_when_given(qd, monkeypatch):
    """The bound is opt-in now, not gone. A caller that wants a small pass says so."""
    monkeypatch.setattr(qd, "_get", lambda p, q: [_issue(i, f"p{i}") for i in range(6)])
    monkeypatch.setattr(qd, "_dispatchable", lambda p: True)
    spawned = []
    monkeypatch.setattr(qd, "_spawn",
                        lambda product, task, detach, **kw: spawned.append(product) or True)
    qd.main(["--max-products", "2"])
    assert len(spawned) == 2


def test_the_unbounded_brief_never_says_at_most_zero(qd):
    """An agent told to "WORK AT MOST 0 ISSUES" reasonably does nothing, and the
    pass reports success having drained nothing. The sentence must change with the
    bound, not interpolate the number."""
    task = qd._task_for("vivy", 9, 0, work_dir=RUN_DIR)
    assert "AT MOST 0" not in task
    assert "WORK EVERY CLAIMABLE ISSUE" in task
    assert "do not invent a cap" in task


def test_the_bounded_brief_is_unchanged(qd):
    task = qd._task_for("vivy", 9, 3, work_dir=RUN_DIR)
    assert "AT MOST 3 ISSUES" in task
    assert "Do not raise the bound yourself" in task


# --- review-round cap (reads the policy file, not a hardcoded number) --------
#
# The cap used to be hand-written at "5-round cap" three times in this brief
# (platform/decisions/0083 raised it 3 -> 5 and needed the same hand edit; the operator
# raised it again 5 -> 7 on 2026-10-01, sig_01M3W9600TWE8SVKQNXX6G282A). It now
# reads `review_rounds_per_role` (documents) and `review_rounds_code` (code) from
# the policy file at the knowledge repo's origin/main (`agent_policy.load`),
# falling back to `agent_policy.FALLBACK_*` when a value or the file is missing.


SHIPPED_POLICY = (Path(__file__).parent / "fixtures" / "agent-policy-6fe9537.yaml").read_text(encoding="utf-8")


def _policy_text(**limits: int | str | None) -> str:
    """The shipped policy file with the named limits replaced, or appended when the
    shipped file has no line for them. A None value leaves the file as shipped."""
    text = SHIPPED_POLICY
    for key, value in limits.items():
        if value is None:
            continue
        if re.search(rf"(?m)^{key}: ", text):
            text, n = re.subn(rf"(?m)^{key}: .*$", f"{key}: {value}", text)
            assert n == 1
        else:
            text += f"{key}: {value}\n"
    return text


def _cap_sentence(code: int, doc: int, *, progress: bool = True) -> str:
    """The sentence the brief must state, built from the values the test chose. `progress`
    names the form: the progress clause is stated only when the code cap exceeds the document
    cap, so the fallback (equal caps) passes `progress=False`."""
    sentence = (f"The review-round cap is {code} rounds for a pull request that changes code or any "
                f"file an agent is instructed to act on, and {doc} rounds for one that changes only documents.")
    if progress:
        sentence += (f" A pull request that changes code or any file an agent is instructed to act on runs rounds "
                     f"past {doc} only while no finding in the latest round repeats one an earlier round raised.")
    return sentence


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
         "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True,
    )


@pytest.fixture
def knowledge_policy(qd, tmp_path, monkeypatch):
    """Build a knowledge repo whose origin/main holds the policy file (or, with
    `missing=True`, no policy file at all) and point `qd.KNOWLEDGE_REPO` at it."""

    def make(review_rounds_per_role: int | None = None, review_rounds_code: int | None = None,
             missing: bool = False) -> Path:
        repo = tmp_path / "knowledge"
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        target = repo / qd.AP.POLICY_PATH
        target.parent.mkdir(parents=True)
        if missing:
            (repo / "README.md").write_text("no policy\n")
        else:
            target.write_text(_policy_text(review_rounds_per_role=review_rounds_per_role,
                                           review_rounds_code=review_rounds_code),
                              encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "knowledge")
        _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        monkeypatch.setattr(qd, "KNOWLEDGE_REPO", repo)
        return repo

    return make


def test_the_brief_states_both_policy_caps_and_which_pull_request_takes_each(qd, knowledge_policy):
    """The cap sentence must track `review_rounds_code` and `review_rounds_per_role` as
    the policy file reads them, not numbers hand-written here. 13 (code) and 9 (document)
    are neither the shipped document default (5), the code fallback (11), nor the
    2026-10-01 document raise (7), so this cannot pass by coincidence."""
    knowledge_policy(review_rounds_per_role=9, review_rounds_code=13)
    task = qd._task_for("vivy", 9, 0, work_dir=RUN_DIR)
    assert task.count(_cap_sentence(code=13, doc=9)) == 1
    assert "11 rounds" not in task


def test_a_policy_file_without_review_rounds_code_states_the_code_fallback(qd, knowledge_policy):
    """A readable file with no `review_rounds_code` line is not broken, so the brief
    states the decided code cap (11, platform/decisions/0083) beside the file's document cap."""
    knowledge_policy(review_rounds_per_role=9)
    task = qd._task_for("vivy", 9, 0, work_dir=RUN_DIR)
    assert task.count(_cap_sentence(code=qd.AP.DECIDED_REVIEW_ROUNDS_CODE, doc=9)) == 1


def test_the_brief_falls_back_to_the_named_fallbacks_when_the_policy_is_unreadable(qd, knowledge_policy):
    """A missing policy file must not silently drop the caps the brief states: it
    falls back to `agent_policy.FALLBACK_REVIEW_ROUNDS_CODE` and
    `agent_policy.FALLBACK_REVIEW_ROUNDS_PER_ROLE`."""
    knowledge_policy(missing=True)
    task = qd._task_for("vivy", 9, 0, work_dir=RUN_DIR)
    # The fallback sentence is a prefix of the form with the progress clause, so the sentence
    # alone cannot tell them apart. Match it with the step 8 text that follows it: the progress
    # form continues "documents. A pull request", so only the fallback form is followed by "A finding".
    fallback = _cap_sentence(code=qd.AP.FALLBACK_REVIEW_ROUNDS_CODE,
                             doc=qd.AP.FALLBACK_REVIEW_ROUNDS_PER_ROLE, progress=False)
    assert task.count(fallback + " A finding that repeats one an earlier round raised") == 1
    assert "runs rounds past" not in task


def test_the_repeat_rule_in_step_eight_says_what_a_headless_run_does(qd, knowledge_policy):
    """A headless drain has no operator: a repeat in a round past the document cap stops dispatching,
    leaves the PR pr-open, and is recorded on the thread."""
    knowledge_policy(review_rounds_per_role=9, review_rounds_code=13)
    task = qd._task_for("vivy", 9, 0, work_dir=RUN_DIR)
    assert ("A finding that repeats one an earlier round raised, in a round past the document cap, ends the cycle: "
            "a headless run dispatches no further round, leaves the PR pr-open, and records the repeat on "
            "the issue thread with mcp__ops__issue_comment.") in task
    assert "No exception" not in task


def test_the_step_eight_failure_clause_takes_the_cap_for_the_prs_kind_not_the_code_cap(qd, knowledge_policy):
    """A bug fix that changes only documents is a document-only PR, so the bug-fixer's PR is not
    always a code PR: the clause names the cap for that PR's kind and the cap sentence picks it."""
    knowledge_policy(review_rounds_per_role=9, review_rounds_code=13)
    task = qd._task_for("vivy", 9, 0, work_dir=RUN_DIR)
    assert "At the step 8 cap for that PR's kind AFTER a PR opened, leave it pr-open" in task
    assert "the bug-fixer's PR is a code PR" not in task


# --- --type filter -----------------------------------------------------------


def _typed(iid, product, itype):
    return {"id": f"issue_{iid}", "product": product, "state": "open",
            "title": f"thing {iid}", "type": itype}


def test_type_filter_narrows_the_queue(qd, monkeypatch):
    monkeypatch.setattr(qd, "_get", lambda p, q: [
        _typed(1, "vivy", "bug"), _typed(2, "vivy", "improvement"),
        _typed(3, "vivy", "task")])
    by_product, _ = qd._claimable({"bug"})
    assert [i["id"] for i in by_product["vivy"]] == ["issue_1"]


def test_type_filter_reports_what_it_excluded(qd, monkeypatch):
    """A filter that silently shrinks the queue is the same failure as a queue with
    no consumer — the thing this command exists to have caught."""
    monkeypatch.setattr(qd, "_get", lambda p, q: [
        _typed(1, "vivy", "bug"), _typed(2, "vivy", "improvement")])
    _, warnings = qd._claimable({"bug"})
    assert any("excluded 1" in w for w in warnings)


def test_no_type_filter_keeps_every_type(qd, monkeypatch):
    monkeypatch.setattr(qd, "_get", lambda p, q: [
        _typed(1, "vivy", "bug"), _typed(2, "vivy", "task")])
    by_product, warnings = qd._claimable()
    assert len(by_product["vivy"]) == 2
    assert not any("excluded" in w for w in warnings)


def test_an_unknown_type_is_rejected_loudly(qd, monkeypatch, capsys):
    """A typo that matched nothing would drain zero and report a clean pass."""
    monkeypatch.setattr(qd, "_get", lambda p, q: [_typed(1, "vivy", "bug")])
    rc = qd.main(["--type", "bugs"])          # note the plural typo
    assert rc == 2
    assert "unknown --type bugs" in capsys.readouterr().err


def test_the_scoped_brief_tells_the_agent_not_to_claim_other_types(qd):
    task = qd._task_for("vivy", 9, 0, {"bug", "nit"}, work_dir=RUN_DIR)
    assert "SCOPED TO TYPE bug/nit" in task
    assert "do NOT claim them" in task


# --- the in-flight gate (ARCH-418) -------------------------------------------
#
# `claimable` answers only the CLAIM question — open, plus claims whose TTL
# lapsed. It knows nothing about branches or PRs, so an issue whose claim expired
# while its PR stayed open reads as free. Dispatching that is how the overnight
# opened convivy/vivy #971 for work a daytime co already had on #944. The gate
# moved here when the drain did; these assert it actually runs.


def test_an_issue_whose_pr_is_already_open_is_held_back(qd, monkeypatch):
    """The must-not-happen direction, and the whole reason the gate came across."""
    monkeypatch.setattr(qd.issue_gate, "eligible_issues",
                        lambda product, items=None, **kw: (
                            [i for i in (items or []) if not (i.get("refs") or {}).get("pr")],
                            [(i, f"a PR is already linked ({(i.get('refs') or {})['pr']})")
                             for i in (items or []) if (i.get("refs") or {}).get("pr")]))
    free = _issue(1, "vivy")
    taken = _issue(2, "vivy")
    taken["refs"] = {"pr": 1069}
    monkeypatch.setattr(qd, "_get", lambda p, q: [free, taken])
    by_product, warnings = qd._claimable()
    assert [i["id"] for i in by_product["vivy"]] == ["issue_1"]
    assert any("issue_2" in w and "1069" in w for w in warnings), \
        "a held-back issue must be NAMED; a gate that drops work silently is " \
        "indistinguishable from an empty queue"


def test_a_product_whose_every_issue_is_held_back_drops_out_entirely(qd, monkeypatch):
    """No empty-list entry: a product with nothing eligible must not be dispatched
    against, or the drain spawns a worker with no work to do."""
    monkeypatch.setattr(qd.issue_gate, "eligible_issues",
                        lambda product, items=None, **kw: ([], [(i, "held") for i in (items or [])]))
    monkeypatch.setattr(qd, "_get", lambda p, q: [_issue(1, "vivy"), _issue(2, "sous-chef")])
    by_product, warnings = qd._claimable()
    assert by_product == {}
    assert len(warnings) == 2


def test_a_gate_failure_is_loud_rather_than_a_quiet_empty_queue(qd, monkeypatch):
    """eligible_issues fails CLOSED on an unreachable remote, returning everything
    as skipped. That is the right call — a duplicate PR costs more than a quiet
    night — but it must SURFACE, or a product silently drops out of the drain and
    reads exactly like having nothing to do."""
    monkeypatch.setattr(qd.issue_gate, "eligible_issues",
                        lambda product, items=None, **kw: (
                            [], [(i, "branch scan unavailable (ls-remote failed)")
                                 for i in (items or [])]))
    monkeypatch.setattr(qd, "_get", lambda p, q: [_issue(1, "vivy")])
    by_product, warnings = qd._claimable()
    assert by_product == {}
    assert any("branch scan unavailable" in w for w in warnings)


def test_the_gate_is_applied_per_product(qd, monkeypatch):
    """The branch scan is a per-repo ls-remote, so the gate must be called once
    per product with only that product's issues — not once with the whole queue."""
    seen = []

    def fake_gate(product, items=None, **kw):
        seen.append((product, [i["id"] for i in (items or [])]))
        return list(items or []), []

    monkeypatch.setattr(qd.issue_gate, "eligible_issues", fake_gate)
    monkeypatch.setattr(qd, "_get", lambda p, q: [
        _issue(1, "vivy"), _issue(2, "sous-chef"), _issue(3, "vivy")])
    qd._claimable()
    assert sorted(seen) == [("sous-chef", ["issue_2"]), ("vivy", ["issue_1", "issue_3"])]


# --- the feature-authorization gate (ARCH-403 Tier 1.2, carried by ARCH-418) ---
#
# A feature the operator files themself IS the authorization — the plane's successor to their
# 🚀 — recorded as created_by_kind == "human". An agent-filed feature is a
# proposal that must reach them as a feature-gate decision. That rule lived only in
# overnight-triage's prompt, which is no longer in the loop; without it here, the
# drain's "work every claimable issue" brief would auto-build agent proposals.


def _feature(iid, product="vivy", created_by_kind="agent"):
    it = _issue(iid, product)
    it["type"] = "feature"
    it["created_by_kind"] = created_by_kind
    return it


def test_an_agent_filed_feature_is_never_dispatched(qd, monkeypatch):
    """The must-not-happen direction: the authorization gate failing open,
    unattended, is worse than any duplicate-work case."""
    monkeypatch.setattr(qd, "_get", lambda p, q: [_feature(1, "vivy", "agent")])
    by_product, warnings = qd._claimable()
    assert by_product == {}
    assert any("issue_1" in w and "feature-gate" in w for w in warnings)


def test_a_human_filed_feature_is_dispatched(qd, monkeypatch):
    """The must-still-work direction. The operator filing it is the authorization, so
    gating on type alone would silently stop features shipping."""
    monkeypatch.setattr(qd, "_get", lambda p, q: [_feature(1, "vivy", "human")])
    by_product, _ = qd._claimable()
    assert [i["id"] for i in by_product["vivy"]] == ["issue_1"]


def test_a_feature_with_no_created_by_kind_is_treated_as_agent_filed(qd, monkeypatch):
    """Absent must read as NOT-authorized, matching the store's own default. This
    and the API have to agree, or a deploy skew flips the gate open."""
    it = _issue(1, "vivy")
    it["type"] = "feature"
    monkeypatch.setattr(qd, "_get", lambda p, q: [it])
    by_product, _ = qd._claimable()
    assert by_product == {}


def test_non_feature_types_are_untouched_by_the_authorization_gate(qd, monkeypatch):
    """Bugs and tasks carry no such gate — filtering them on created_by_kind would
    stop the drain doing its actual job, since nearly every bug is agent-filed."""
    monkeypatch.setattr(qd, "_get", lambda p, q: [_issue(1, "vivy"), _issue(2, "vivy")])
    by_product, _ = qd._claimable()
    assert len(by_product["vivy"]) == 2


def _feature_with_decision(iid, decision_id, product="vivy", *, top_level=False):
    """An agent-filed feature carrying a feature-gate decision ref.

    Default: decision_id nested under refs (the real API shape). The API nests this
    under `refs.decision_id` — a top-level read always returned None, which is the
    bug this file exists to guard against (issue_01KYRB2J6SHDC7CSGQV6QJQQG6). Pass top_level=True
    for explicit fallback-path coverage when the API shape changes."""
    it = _feature(iid, product, created_by_kind="agent")
    if top_level:
        it["decision_id"] = decision_id
    else:
        it["refs"] = {"decision_id": decision_id}
    return it


def _ok_decision(state, directive=""):
    return {"status": "ok", "item": {"state": state, "directive": directive}}


# --- feature-gate decision path (the missing third branch) -------------------


def test_an_agent_filed_feature_with_an_approved_decision_drains(qd, monkeypatch):
    """The live repro: issue_01KYMFBZG6N9K9RNM8ENA15JZ0 carried dec_01KYMVYH3J41...
    (state=approved) and was still held back on every pass."""
    monkeypatch.setattr(qd.DC, "get_decision",
                        lambda decision_id, **kw: _ok_decision("approved"))
    monkeypatch.setattr(qd, "_get",
                        lambda p, q: [_feature_with_decision(1, "dec_ABC")])
    by_product, warnings = qd._claimable()
    assert [i["id"] for i in by_product["vivy"]] == ["issue_1"]
    assert warnings == []


def test_an_agent_filed_feature_with_a_redirected_decision_drains(qd, monkeypatch):
    """A redirect is the operator choosing a different shape — still an authorization."""
    monkeypatch.setattr(qd.DC, "get_decision",
                        lambda decision_id, **kw: _ok_decision("redirected",
                                                                "use approach X"))
    monkeypatch.setattr(qd, "_get",
                        lambda p, q: [_feature_with_decision(1, "dec_ABC")])
    by_product, warnings = qd._claimable()
    assert [i["id"] for i in by_product["vivy"]] == ["issue_1"]
    assert warnings == []


def test_the_directive_reaches_the_brief(qd, monkeypatch):
    """The operator's redirect directive must appear verbatim in the orchestrator brief.
    If it does not, the worker builds what the operator redirected AWAY from."""
    it = _feature_with_decision(1, "dec_ABC")
    monkeypatch.setattr(qd.DC, "get_decision",
                        lambda decision_id, **kw: _ok_decision("redirected",
                                                                "focus on the export path only"))
    monkeypatch.setattr(qd, "_get", lambda p, q: [it])
    by_product, _ = qd._claimable()
    issues = by_product.get("vivy", [])
    assert issues, "redirected feature should drain"
    task = qd._task_for("vivy", 1, 1, None, issues, work_dir=RUN_DIR)
    assert "focus on the export path only" in task
    assert "AUTHORIZED FEATURES" in task


def test_an_agent_filed_feature_with_a_pending_decision_is_held(qd, monkeypatch):
    """A pending decision is not yet an authorization — the issue must wait."""
    monkeypatch.setattr(qd.DC, "get_decision",
                        lambda decision_id, **kw: _ok_decision("pending"))
    monkeypatch.setattr(qd, "_get",
                        lambda p, q: [_feature_with_decision(1, "dec_ABC")])
    by_product, warnings = qd._claimable()
    assert by_product == {}
    assert any("issue_1" in w for w in warnings)


def test_an_agent_filed_feature_with_an_unresolvable_decision_is_held(qd, monkeypatch):
    """A lookup failure must not open the gate — fail closed."""
    monkeypatch.setattr(qd.DC, "get_decision",
                        lambda decision_id, **kw: {"status": "error:503", "item": None})
    monkeypatch.setattr(qd, "_get",
                        lambda p, q: [_feature_with_decision(1, "dec_ABC")])
    by_product, warnings = qd._claimable()
    assert by_product == {}
    assert any("issue_1" in w and "could not be resolved" in w for w in warnings)


def test_an_agent_filed_feature_with_a_missing_decision_id_is_held(qd, monkeypatch):
    """No decision_id at all — the issue still needs a feature-gate decision."""
    monkeypatch.setattr(qd, "_get", lambda p, q: [_feature(1, "vivy", "agent")])
    by_product, warnings = qd._claimable()
    assert by_product == {}
    assert any("issue_1" in w and "feature-gate" in w for w in warnings)


# --- regression: decision_id lives under refs, not at top level (issue_01KYRB2J6SHDC7CSGQV6QJQQG6) --
#
# The real /api/issues endpoint nests decision_id inside refs. The MCP tools hoist
# it to top level, which is why the bug was invisible when testing through MCP.
# The drain reads the raw HTTP response, so refs is the shape that must work.


def test_an_approved_decision_read_from_refs_clears_the_issue(qd, monkeypatch):
    """The real API shape: decision_id nested under refs. This is the regression
    for the bug — the approved branch was dead code because every decision_id read
    returned None."""
    monkeypatch.setattr(qd.DC, "get_decision",
                        lambda decision_id, **kw: _ok_decision("approved"))
    monkeypatch.setattr(qd, "_get",
                        lambda p, q: [_feature_with_decision(1, "dec_ABC")])
    by_product, warnings = qd._claimable()
    assert [i["id"] for i in by_product["vivy"]] == ["issue_1"]
    assert warnings == []


def test_a_pending_decision_read_from_refs_holds_it_back(qd, monkeypatch):
    """Same real API shape: a pending decision gate read from refs must still
    hold the issue, just as it would from a top-level field."""
    monkeypatch.setattr(qd.DC, "get_decision",
                        lambda decision_id, **kw: _ok_decision("pending"))
    monkeypatch.setattr(qd, "_get",
                        lambda p, q: [_feature_with_decision(1, "dec_ABC")])
    by_product, warnings = qd._claimable()
    assert by_product == {}
    assert any("issue_1" in w for w in warnings)


def test_top_level_decision_id_fallback_still_works(qd, monkeypatch):
    """Explicit fallback-path coverage: if a future API shape promotes decision_id
    to top level, the drain handles it without needing a second fix."""
    monkeypatch.setattr(qd.DC, "get_decision",
                        lambda decision_id, **kw: _ok_decision("approved"))
    monkeypatch.setattr(qd, "_get",
                        lambda p, q: [_feature_with_decision(1, "dec_ABC", top_level=True)])
    by_product, warnings = qd._claimable()
    assert [i["id"] for i in by_product["vivy"]] == ["issue_1"]
    assert warnings == []


def test_approved_feature_does_not_appear_in_redirect_section_of_brief(qd, monkeypatch):
    """An approved (non-redirected) feature has no directive to thread through;
    the AUTHORIZED FEATURES section must still name it but without a directive."""
    it = _feature_with_decision(1, "dec_ABC")
    monkeypatch.setattr(qd.DC, "get_decision",
                        lambda decision_id, **kw: _ok_decision("approved"))
    monkeypatch.setattr(qd, "_get", lambda p, q: [it])
    by_product, _ = qd._claimable()
    issues = by_product.get("vivy", [])
    assert issues
    task = qd._task_for("vivy", 1, 1, None, issues, work_dir=RUN_DIR)
    assert "AUTHORIZED FEATURES" in task
    assert "approved — build the original proposal" in task


# --- the operator watch ------------------------------------------------------
#
# `needs=operator` issues are excluded from `claimable` by design, so nothing
# revisits them. That exclusion is the point of the field and also its failure
# mode: issue_01KYGK70A6Y9GFHE8P0DJZ3KJA sat undrainable for two days after being
# declined as needing "the Render dashboard" — true of the dashboard, false of the
# Render API an agent already had a key for. The watch runs here rather than as its
# own job because this command already runs nightly with the queue in hand.


def _op_issue(iid, title="a thing", created="2026-07-01T00:00:00Z"):
    return {"id": f"issue_{iid}", "product": "vivy", "state": "open",
            "needs": "operator", "title": title, "created_at": created}


def test_operator_watch_asks_the_store_for_operator_issues(qd, monkeypatch):
    """The whole watch depends on one query the rest of this command never makes."""
    seen = {}

    def fake_get(path, params):
        seen.update(params)
        return []

    monkeypatch.setattr(qd, "_get", fake_get)
    qd._operator_watch()
    assert seen.get("needs") == "operator"
    assert seen.get("state") == "open"


def test_operator_watch_is_silent_when_nothing_waits(qd, monkeypatch):
    """No operator issues must produce NO output — a nightly job that prints a
    reassurance every time trains the reader to skip it."""
    monkeypatch.setattr(qd, "_get", lambda p, q: [])
    assert qd._operator_watch() == []


def test_operator_watch_names_every_issue_oldest_first(qd, monkeypatch):
    """The watch names every issue rather than counting them. A count hides WHICH
    item is stuck, and the stuck one is the only thing worth acting on."""
    monkeypatch.setattr(qd, "_get", lambda p, q: [
        _op_issue(1, "newer", created="2026-07-20T00:00:00Z"),
        _op_issue(2, "older", created="2026-01-01T00:00:00Z"),
    ])
    lines = qd._operator_watch()
    body = "\n".join(lines)
    assert "issue_1" in body and "issue_2" in body
    assert body.index("issue_2") < body.index("issue_1"), "oldest must sort first"


def test_operator_watch_survives_an_unreadable_created_at(qd, monkeypatch):
    """Age is cosmetic; a malformed timestamp must not cost the visibility, which
    is the part that matters."""
    monkeypatch.setattr(qd, "_get", lambda p, q: [_op_issue(1, created="not-a-date")])
    body = "\n".join(qd._operator_watch())
    assert "issue_1" in body


def test_operator_watch_survives_a_naive_timestamp(qd, monkeypatch):
    """A timestamp with no timezone designator is the dangerous one, and the first
    version of this test missed it.

    "not-a-date" fails inside fromisoformat and raises ValueError. But
    "2026-07-01T00:00:00" PARSES FINE and yields a naive datetime; the raise comes
    one line later, from subtracting it from an aware now(), as TypeError. Catching
    only ValueError let that escape through items.sort() and take down the drain —
    breaking the invariant this watch is built around. Caught by review.
    """
    monkeypatch.setattr(qd, "_get", lambda p, q: [
        _op_issue(1, created="2026-07-01T00:00:00"),   # naive: no Z, no offset
    ])
    body = "\n".join(qd._operator_watch())
    assert "issue_1" in body


def test_operator_watch_never_fails_the_drain(qd, monkeypatch):
    """A watch that can take down the pass it rides on is a worse bug than the one
    it reports. A store error degrades to a warning, and the drain continues."""
    def boom(path, params):
        raise RuntimeError("store down")

    monkeypatch.setattr(qd, "_get", boom)
    lines = qd._operator_watch()
    assert lines and "could not read the operator queue" in lines[0]


def test_operator_watch_does_not_guess_at_misclassification(qd, monkeypatch):
    """Deliberately NOT a classifier, and this pins that.

    A lexical version was written and tested against the live queue: flag a body
    with venue words ("dashboard") and no capability words ("credential", "no
    agent"). It did not flag the one item known to be misfiled —
    issue_01KYQ44ZYB4DA5B7YYNJWGEPTT reads "requires the Render dashboard, which no
    agent can reach", where "no agent" scans as a capability claim while being
    false. Telling a true claim from a false one is semantic, so the heuristic
    passed exactly the case it was written for.

    A check that misses its motivating example while looking like a check turns an
    unknown into a false all-clear. If someone re-adds one, this fails.
    """
    monkeypatch.setattr(qd, "_get", lambda p, q: [
        _op_issue(1, "needs the Render dashboard, which no agent can reach"),
    ])
    body = "\n".join(qd._operator_watch())
    assert "issue_1" in body
    assert "SUSPECT" not in body.upper(), (
        "the watch must not label items as probably-misclassified — a lexical "
        "heuristic missed the known-bad case and gave a false all-clear")


# --- origin (2026-09-03 incident: 61 runs, $89.02, uncapped) ------------------
#
# No shell launcher fronts this program (unlike launch-overnight et al., which
# `export AGENT_RUN_ORIGIN=scheduled` before ever calling `agentctl spawn`), so
# it must set its own default at import time or every run it dispatches lands
# in budget.CAPPED_ORIGINS' blind spot. The `qd` fixture is session-scoped and
# already execed by the time these run, so a fresh exec is what lets a test
# observe the default actually being applied rather than reading a module the
# rest of the suite already imported.


def _reload_fresh():
    path = REPO / "bin" / "issue-queue-drain"
    spec = importlib.util.spec_from_loader("issue_queue_drain_fresh", None)
    mod = importlib.util.module_from_spec(spec)
    mod.__dict__["__file__"] = str(path)
    exec(compile(path.read_text(), str(path), "exec"), mod.__dict__)  # noqa: S102
    return mod


def test_bin_resolves_scheduled_when_unset(monkeypatch):
    monkeypatch.delenv("AGENT_RUN_ORIGIN", raising=False)
    _reload_fresh()
    assert os.environ["AGENT_RUN_ORIGIN"] == "scheduled"


def test_bin_OVERRIDES_a_poisoned_inherited_origin(monkeypatch):
    """An unconditional assignment, not setdefault, and this is the case that
    decides which one is right.

    AGENT_RUN_ORIGIN is self-asserted and unauthenticated (ADR 0058), and
    `launchctl setenv AGENT_RUN_ORIGIN interactive` — runnable by any process
    under this account, an autonomous agent holding Bash included — persists
    into every launchd-started process until it is unset or the machine
    reboots. Under setdefault that value survives into the 03:30 run, which
    then stamps `interactive`, writes an ordinary meta.json, and is silently
    uncapped: the exact 2026-09-03 incident, reproduced without anyone
    touching this file (security review, round 1).

    `interactive` is the value that matters here because it is the one that
    escapes the cap; the parametrisation covers the other origins only to pin
    that NOTHING inherited wins.
    """
    for poisoned in ("interactive", "curation", "full_monty", "nonsense"):
        monkeypatch.setenv("AGENT_RUN_ORIGIN", poisoned)
        _reload_fresh()
        assert os.environ["AGENT_RUN_ORIGIN"] == "scheduled", (
            f"an inherited AGENT_RUN_ORIGIN={poisoned!r} survived into the run")


def test_the_origin_this_bin_sets_is_actually_capped():
    """The assignment is only worth anything if the value it writes is one the
    budget gate caps. A test that pinned the literal string 'scheduled' without
    checking it against budget.CAPPED_ORIGINS would keep passing if the capped
    set were ever changed out from under this job."""
    import budget
    assert "scheduled" in budget.CAPPED_ORIGINS
    assert "interactive" not in budget.CAPPED_ORIGINS
