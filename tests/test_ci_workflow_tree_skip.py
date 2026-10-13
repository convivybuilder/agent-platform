"""Structural checks on the tree-equality push skip in `.github/workflows/ci.yml`
(issue_01M36FKG4QJQ2SPQSW0VDY0A66).

WHY THIS EXISTS AS A YAML-STRUCTURE TEST RATHER THAN A LIVE-CI TEST. Nothing in
this repo can execute a GitHub Actions run, so these assertions pin the parts of
the skip that a live run cannot exercise locally: that the skip is wired at
step level (never job level — the workflow's own comment records that a
job-level `if:` reports the job SKIPPED and fails `agentctl review-status`'s
skipped-check rule, `_skipped_checks` in bin/agentctl), that the `bash` job also
swaps its runner (skipping only the steps still boots a macOS runner and bills
its ~10x-weighted one-minute minimum), and that the decision step is built to
fail open — default to NOT skipping, flip to skipping only after a positive
match. A parse-level check cannot prove the API calls resolve correctly at
runtime; it proves the shape that determines whether a real API failure fails
open or closed, and whether `review-status` reads a false SKIPPED.
"""

from __future__ import annotations

import pathlib

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
CI_YML = REPO / ".github" / "workflows" / "ci.yml"


def _load() -> dict:
    return yaml.safe_load(CI_YML.read_text())


def test_changes_job_exposes_a_tree_skip_output():
    doc = _load()
    outputs = doc["jobs"]["changes"]["outputs"]
    assert "tree_skip" in outputs


def test_decision_step_only_runs_on_push():
    doc = _load()
    steps = doc["jobs"]["changes"]["steps"]
    decision = next((s for s in steps if s.get("id") == "tree_skip"), None)
    assert decision is not None, "no step with id: tree_skip in the changes job"
    assert decision.get("if") == "github.event_name == 'push'", (
        "the tree-equality decision must be scoped to push events; deciding it "
        "on a pull_request run would apply a merge-commit optimization to a "
        "PR's own head check, which is never a merge commit"
    )


def test_decision_step_invokes_the_unit_tested_script():
    """The decision (including the fail-open default and the exclusion of a
    PR head's `skipped` draft-stage check-run) lives in
    scripts/ci/ci-tree-skip-decide, unit-tested in
    tests/test_ci_tree_skip_decide.py — not as inline shell here. This step
    should be thin I/O glue: it invokes that script and nothing more."""
    doc = _load()
    steps = doc["jobs"]["changes"]["steps"]
    decision = next((s for s in steps if s.get("id") == "tree_skip"), None)
    assert decision is not None, "no step with id: tree_skip in the changes job"
    assert "ci-tree-skip-decide" in decision.get("run", "")


def test_bash_python_and_swift_test_steps_gate_on_tree_skip_not_the_job():
    """The skip must live on the steps, never the job.

    A job-level `if:` that evaluates false reports the job SKIPPED, and
    `agentctl review-status`'s `_skipped_checks` fails any READY PR carrying a
    skipped required check — it cannot distinguish "skipped because the tree
    already passed" from "skipped because the suite never ran". `bash`,
    `python` and `swift` already carry an unrelated, pre-existing job-level
    `if:` for the ARCH-364 draft gate; the job-level half of this assertion
    is narrower than "no job-level if:" — it only forbids one that mentions
    tree_skip.

    Both halves are asserted together (rather than as two separate tests) so
    an implementation that wired the skip at job level instead of step level
    — wrong, but a way this could have been built — fails this one test on
    both counts, and so that this test, unlike a job-level-absence check
    alone, actually fails against origin/main: before this change there is no
    tree_skip anywhere, so the step-gating half below is what fails there.
    """
    doc = _load()
    expected_step_name_fragments = {
        "bash": ["bash lint", "bash tests"],
        "python": ["pytest"],
        "swift": ["swift test"],
    }
    for job_name, fragments in expected_step_name_fragments.items():
        job_if = str(doc["jobs"][job_name].get("if", ""))
        assert "tree_skip" not in job_if, (
            f"{job_name} job's job-level `if:` references tree_skip; the "
            "skip must be wired on the steps, not the job"
        )
        steps = doc["jobs"][job_name]["steps"]
        for fragment in fragments:
            matches = [s for s in steps
                       if fragment in str(s.get("name", "")).lower()]
            assert matches, f"no step named like {fragment!r} in the {job_name} job"
            for step in matches:
                assert "tree_skip" in str(step.get("if", "")), (
                    f"{job_name} job's {step.get('name')!r} step does not gate "
                    "on needs.changes.outputs.tree_skip"
                )


def test_bash_job_runner_swaps_to_linux_on_tree_skip():
    """Step-level gating alone is not enough for the bash job: a docs-only PR
    whose steps all skip still bills a macOS runner's ~10x-weighted one-minute
    minimum just to check out and stop (the job's own comment on this file
    explains the pre-existing docs-only case). Confirm the tree-equality skip
    reuses that same runner-swap mechanism rather than only gating the steps.
    """
    doc = _load()
    runs_on = str(doc["jobs"]["bash"]["runs-on"])
    assert "tree_skip" in runs_on


def test_linux_jobs_run_on_the_ci1_scale_set():
    """`changes`, `lint` and `python` run on the self-hosted `convivy-linux`
    scale set (platform/runbooks/standing-up-the-linux-ci-box, step 10). GARM
    gives that scale set no label list, so the value must be exactly the scale
    set's name. The bash job's Linux fallback uses the same name, and its macOS
    branch runs on the self-hosted runner on the Mac Studio (label
    `convivy-macos-bash`, CQ-417), requested by that single label alone rather than GitHub's hosted
    `macos-latest`.

    Only the one label: `gh api repos/convivy/agent-platform/actions/runners`
    lists the self-hosted runner on the Mac Studio's labels as `convivy-macos-bash:custom` and nothing
    else, no default `self-hosted` or `macOS` label, so a `runs-on` that also
    asks for `self-hosted` or `macOS` never matches this runner and the job
    queues forever (CQ-417 revise: queued since 2026-10-07 20:10 UTC on commit
    e6ef9777 against this exact online, idle runner).
    """
    doc = _load()
    for job_name in ("changes", "lint", "python"):
        assert doc["jobs"][job_name]["runs-on"] == "convivy-linux", job_name
    bash_runs_on = str(doc["jobs"]["bash"]["runs-on"])
    assert "convivy-linux" in bash_runs_on
    assert "convivy-macos-bash" in bash_runs_on
    assert "self-hosted" not in bash_runs_on
    assert "macOS" not in bash_runs_on
    assert "macos-latest" not in bash_runs_on
    assert "ubuntu-latest" not in bash_runs_on


def test_python_job_sets_up_node_before_pytest():
    """The `convivy-linux` image has no `node`, and tests/test_session_view.py's
    `@needs_node` tests skip silently without one. The `python` job must install
    Node through `actions/setup-node` before pytest runs, so those tests execute
    on ci1 as they do on GitHub's hosted image.
    """
    doc = _load()
    steps = doc["jobs"]["python"]["steps"]
    setup_node = [i for i, s in enumerate(steps)
                  if str(s.get("uses", "")).startswith("actions/setup-node@")]
    assert setup_node, "python job has no actions/setup-node step"
    pytest_steps = [i for i, s in enumerate(steps)
                    if "pytest" in str(s.get("name", "")).lower()]
    assert pytest_steps, "python job has no pytest step"
    assert setup_node[0] < pytest_steps[0], (
        "actions/setup-node must run before the pytest step"
    )
    assert str(steps[setup_node[0]]["with"]["node-version"]) == "22"


def test_python_job_links_setup_node_npx_onto_the_fixed_list():
    """setup-node's npx sits in the runner tool cache, which safe_path's fixed
    system list does not reach. Without a link at /usr/local/bin/npx, the two
    npx resolution tests skip on the `convivy-linux` runner. The link step must
    run after setup-node and before pytest.
    """
    doc = _load()
    steps = doc["jobs"]["python"]["steps"]
    setup_node = [i for i, s in enumerate(steps)
                  if str(s.get("uses", "")).startswith("actions/setup-node@")]
    assert setup_node, "python job has no actions/setup-node step"
    link = [i for i, s in enumerate(steps)
            if "/usr/local/bin/npx" in str(s.get("run", ""))]
    assert link, "python job has no step linking npx into /usr/local/bin"
    pytest_steps = [i for i, s in enumerate(steps)
                    if "pytest" in str(s.get("name", "")).lower()]
    assert pytest_steps, "python job has no pytest step"
    assert setup_node[0] < link[0] < pytest_steps[0], (
        "the npx link must run after actions/setup-node and before pytest"
    )


def test_python_pytest_step_reports_skip_reasons():
    """`-rs` makes pytest print one `SKIPPED [file:line] reason` line per skip.
    CI's skip set is compared with a local run's, and without this flag a skip
    shows only as a count, so a skip cannot be named from the log.
    """
    doc = _load()
    steps = doc["jobs"]["python"]["steps"]
    pytest_runs = [str(s.get("run", "")) for s in steps
                   if "pytest" in str(s.get("name", "")).lower()]
    assert pytest_runs, "python job has no pytest step"
    assert "-rs" in pytest_runs[0].split(), (
        "the pytest step must pass -rs so every skip prints its reason"
    )
