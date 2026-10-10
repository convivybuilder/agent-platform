"""Tests for lib/agent-platform/tool_policy.py (ARCH-346).

The bug this module exists to end: one `tools:` field meaning "enforced", "ignored", or
"enforced one level down" depending on a runtime the artifact never names. So the tests
that matter assert the policy tells the TRUTH about what binds — a policy claiming
enforcement it does not have is the failure mode, not a missing tool.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import tool_policy as TP

_DENY_SAMPLE = "Bash(gh pr review:*)"

_REPO = Path(__file__).resolve().parent.parent


def _agentctl_without_tool_policy(tmp_path: Path, *args: str):
    """Run a copy of agentctl from a checkout that is MISSING tool_policy.py —
    i.e. the window between `git pull` on main and someone remembering install.sh,
    or an old worktree checked out before the module existed.

    Hermetic on purpose: agentctl resolves its lib relative to its own `__file__`
    (ARCH-229), not via ~/.local/lib/agent-platform, so simulating "this checkout
    lacks a module" means building an actual checkout missing that file rather than
    faking a deployed dir agentctl no longer consults. HOME still points at a
    scratch dir so nothing here touches the operator's real ~/Convivy paths (the
    ARCH-343 lesson, learned on test_launch_overnight.sh: a test that reads real
    deploy state passes or fails by accident)."""
    checkout = tmp_path / "checkout"
    (checkout / "bin").mkdir(parents=True)
    shutil.copy2(_REPO / "bin" / "agentctl", checkout / "bin" / "agentctl")
    (checkout / "bin" / "agentctl").chmod(0o755)
    lib = checkout / "lib" / "agent-platform"
    lib.mkdir(parents=True)
    for src in (_REPO / "lib" / "agent-platform").glob("*.py"):
        if src.name != "tool_policy.py":  # the module the deploy forgot
            shutil.copy2(src, lib / src.name)
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home), "PYTHONPATH": ""}
    env.pop("AGENTCTL_ENFORCE_TOOL_POLICY", None)
    return subprocess.run([sys.executable, str(checkout / "bin" / "agentctl"), *args],
                          capture_output=True, text=True, env=env, timeout=60)


def test_an_unlinked_tool_policy_does_not_take_the_fleet_down(tmp_path):
    """The hazard this module's import strategy is FOR — and which the first fix re-armed.

    `~/.local/lib/agent-platform` is per-file symlinks, so main carrying an agentctl that
    imports a new module kills every spawn with ImportError until a human runs install.sh.
    Aimed squarely at the 01:00 overnight.

    The first attempt cured it with `sys.path.append(<repo>/lib)`. Security review killed
    that: it makes any file DROPPED in the repo lib dir importable, and the module in
    question decides the PERMISSION MODE — so an agent reviewing a hostile diff could
    write its own tool_policy.py and choose its own permissions. Curing an availability
    bug by widening the permission system's import surface is a bad trade at any price.

    So the import fails SOFT. And the soft import was itself theatre until this test: the
    argparse line for the `policy` subcommand read `TP.AUTONOMOUS` at parser-construction
    time — which runs on EVERY invocation — so an unlinked module still crashed `spawn`,
    and with it the overnight, from a help string. Reproduced live 2026-07-14."""
    help_run = _agentctl_without_tool_policy(tmp_path, "--help")
    assert help_run.returncode == 0, (
        "agentctl must still PARSE without tool_policy — a degraded import may degrade "
        f"one feature, never the binary.\nstderr: {help_run.stderr[-800:]}")
    assert "tool_policy unavailable" in help_run.stderr, "the degradation must be loud"


def test_the_policy_command_degrades_loudly_rather_than_lying(tmp_path):
    """The one feature that legitimately cannot work without the module. It must say so
    and exit non-zero — never print a default-looking policy that no run will honour."""
    run = _agentctl_without_tool_policy(tmp_path, "policy", "reviewer", "--on", "vivy")
    assert run.returncode != 0, "must not exit 0 while printing nothing real"
    assert "not installed" in run.stderr
    assert "install.sh" in run.stderr, "say how to fix it"


def test_build_agent_admits_it_is_not_enforced():
    """The honest state. A coder needs arbitrary shell (pytest, venv, make, psql), so an
    allow-list permissive enough to work equals full Bash. `enforced: True` here would be
    a control-shaped object that controls nothing."""
    p = TP.resolve("coder", TP.AUTONOMOUS, "vivy", denied=[_DENY_SAMPLE])
    assert p.enforced is False
    # `dontAsk`, not bypassPermissions: under bypass no allow rule binds, so the file-write
    # scope in run_sandbox could not hold. `enforced` stays False: Bash is unrestricted.
    assert p.permission_mode == TP.MODE_ENFORCE == "dontAsk"
    assert p.allowed == ()
    assert p.denied == (_DENY_SAMPLE,)
    assert "arbitrary shell" in p.reason
    assert any("NOT enforced" in n for n in p.notes)


def test_review_agent_is_enforced_under_dontask():
    """dontAsk is the only mode where an allow-list binds. `--allowedTools` under
    bypassPermissions is INERT — verified empirically; a first cut of this module would
    have recorded `enforced: true` while restricting nothing."""
    p = TP.resolve("reviewer", TP.AUTONOMOUS, "vivy", denied=[])
    assert p.enforced is True
    assert p.permission_mode == TP.MODE_ENFORCE == "dontAsk"
    assert "Read" in p.allowed and "Grep" in p.allowed
    # Write is scoped by run_sandbox's Edit rules, never granted bare here.
    assert "Write" not in p.allowed


def test_every_enforced_policy_says_its_guarantees_are_conditional():
    """The deepest version of this module's own failure mode, found by demonstration
    rather than argument (security review, PR #391 round 5).

    `reason` and the caveats state their guarantees unconditionally, but the
    allow-list binds only when `AGENTCTL_ENFORCE_TOOL_POLICY` is set — default OFF —
    ANDed with `policy.enforced`. The reviewer proved it on its own dispatch: running
    under `bypassPermissions`, it successfully executed the very `ruff --fix` the
    exact-match grant denies. So a reader of `policy.json` who trusted `reason` and
    `enforced: true` would be wrong about the run in front of them.

    That is the "control-shaped object that controls nothing" this module's docstring
    exists to prevent, reappearing one level up in the module's own audit record. The
    caveat goes FIRST because it qualifies every other line."""
    for agent in ("reviewer", "security-reviewer", "planner", "scout"):
        p = TP.resolve(agent, TP.AUTONOMOUS, "vivy", denied=[])
        if not p.enforced:
            continue
        assert p.notes, f"{agent} records an enforced policy with no notes at all"
        assert "CONDITIONAL" in p.notes[0] and "AGENTCTL_ENFORCE_TOOL_POLICY" in p.notes[0], \
            f"{agent}'s policy states guarantees without saying enforcement is gated"


def test_a_reviewer_cannot_mutate_the_tree_it_reviews():
    """The property the allow-list actually buys. Confirmed live: `git push` is refused
    by the permission layer under dontAsk, not by the model's goodwill."""
    p = TP.resolve("reviewer", TP.AUTONOMOUS, "vivy", denied=[])
    joined = " ".join(p.allowed)
    for read_verb in ("git diff", "git log", "git show"):
        assert f"Bash({read_verb}:*)" in p.allowed
    for write_verb in ("push", "commit", "checkout", "reset", "clean"):
        assert f"git {write_verb}" not in joined


def test_a_reviewer_can_lint_but_never_run_the_suite():
    """The two halves of one decision, pinned together because separating them is how
    the wrong half gets restored later.

    `ruff` PARSES the tree; `pytest` EXECUTES it, importing every `conftest.py` at
    collection time before any test runs. Granting `pytest` to an agent whose whole job
    is reading an attacker-authored diff is arbitrary code execution, and it would make
    the "no git write verbs" property asserted in
    `test_a_reviewer_cannot_mutate_the_tree_it_reviews` FALSE — code inside that process
    reaches `git` through `subprocess`, which the Bash permission engine never sees. So
    that test and this one are load-bearing for each other rather than independent.

    Approved 2026-08-12, `dec_01KZW59K9G63N0MDW3XTEYPQWF`."""
    for agent in ("reviewer", "security-reviewer"):
        p = TP.resolve(agent, TP.AUTONOMOUS, "vivy", denied=[])
        assert "Bash(ruff check --isolated .)" in p.allowed, \
            f"{agent} cannot lint, which its roster requires"
        # The BARE grant must never come back: without --isolated, a pyproject.toml in
        # the tree under review can set fix/unsafe-fixes and turn the mandated
        # `ruff check .` into a silent rewrite of that tree (round-2 review, PR #391).
        # Neither the bare grant NOR a trailing-wildcard one: the wildcard would match
        # `ruff check --isolated --fix .`, which rewrites the tree with built-in fixers
        # and needs no repo config at all (round-1 security review, knowledge PR #216).
        for reopened in ("Bash(ruff:*)", "Bash(ruff check --isolated:*)"):
            assert reopened not in p.allowed, \
                f"{agent} holds {reopened}, which lets --fix rewrite the tree it reviews"
        assert not any("pytest" in rule for rule in p.allowed), \
            f"{agent} was granted pytest, which executes conftest.py from the tree it reviews"
        # Still not a general shell, or the reason string stops being true.
        assert "Bash" not in p.allowed
        assert "Bash(:*)" not in p.allowed


def test_the_lint_grant_is_exact_so_fix_flags_cannot_be_appended():
    """Two reviewers reached opposite verdicts on this and the stricter one was right.

    agent-platform's reviewer called the appendable `--fix` acceptable, reasoning that
    unscoped `Write` on the same class is strictly more powerful, so an attacker able to
    make a reviewer type `--fix` could call `Write` directly. knowledge PR #216's
    security reviewer blocked it, and its argument beats that one: the `Write` reasoning
    assumes an ATTACKER, and the likelier trigger needs none. An agent reasoning "the
    isolated form is safe, I will tidy these lint nits while I am here" rewrites the code
    it is reviewing and then reviews its own edits, with nobody hostile anywhere.

    So the grant is an EXACT command. `--isolated` closes the config-triggered rewrite;
    exactness closes the flag-triggered one; neither closes the other. This asserts the
    property AND that `policy.json` records why, since a caveat living only in a source
    comment is the same failure one layer along."""
    for agent in ("reviewer", "security-reviewer"):
        p = TP.resolve(agent, TP.AUTONOMOUS, "vivy", denied=[])
        assert any("--fix" in n and "EXACT match" in n for n in p.notes), \
            f"{agent}'s recorded policy does not explain why the grant is exact"
        assert "EXACT command" in p.reason
        # BOTH halves in one assertion. The wildcard check alone would pass for a
        # policy that dropped ruff entirely — "no rule ends in :*" is satisfied by no
        # rule at all — so the positive is asserted here rather than relying on the
        # separate test above still existing (reviewer, PR #391 round 4).
        ruff_rules = [r for r in p.allowed if r.startswith("Bash(ruff")]
        assert ruff_rules == ["Bash(ruff check --isolated .)"], \
            f"{agent}'s ruff grant is {ruff_rules}, not exactly the one exact-match rule"
        # And the policy must record that this command is NOT the repo's CI command,
        # since --isolated discards legitimate per-file-ignores as well as hostile config.
        assert any("NOT THE REPO" in n for n in p.notes), \
            f"{agent}'s policy does not warn that --isolated output diverges from CI"


@pytest.mark.parametrize(
    "agent", ["planner", "platform-planner", "design-critic", "scout"])
def test_the_lint_grant_reaches_only_the_agents_whose_roster_asks_for_it(agent):
    """`ENFORCEABLE_AGENTS` has six members; only `reviewer` and `security-reviewer`
    are told to lint. A grant spliced into the shared `allowed` list would land on all
    six, widening the surface past any cited usage — the rule
    `test_no_write_verb_survives_without_usage_to_justify_it` already enforces for
    `Edit` and `TodoWrite`. Round-1 reviewer finding on PR #391."""
    p = TP.resolve(agent, TP.AUTONOMOUS, "vivy", denied=[])
    assert not any("ruff" in rule for rule in p.allowed), \
        f"{agent} holds the lint grant with no roster requirement for it"
    # The caveats travel with the grant and are gated by the same condition, so today
    # they cannot diverge — which is exactly the coupling the exact-grant test above
    # stopped relying on. Assert it rather than infer it (reviewer, PR #391 round 5).
    #
    # Compared by IDENTITY, not by substring. The first draft matched on "--fix", which
    # then tripped on the enforcement-gate caveat that quotes a `ruff --fix` command
    # while having nothing to do with the lint grant — a test keyed on a substring
    # fails the moment unrelated prose happens to contain it.
    for caveat in (TP._LINT_FIX_CAVEAT, TP._LINT_FIDELITY_CAVEAT):
        assert caveat not in p.notes, \
            f"{agent}'s policy carries a lint caveat for a grant it does not hold"


@pytest.mark.parametrize("unused_grant", ["Edit", "TodoWrite"])
def test_no_write_verb_survives_without_usage_to_justify_it(unused_grant):
    """Both sat in the review allow-list carrying ZERO calls across 81 review runs (the
    2026-07-14 overnight). `Edit` mutates the tree this class claims it cannot mutate;
    `TodoWrite` is a write verb in a surface named "read".

    The rule: a grant stays only if real usage needs it. A permission kept on the chance
    someone might want it is how an allow-list drifts back into being a deny-list with
    extra steps — the thing this module replaced."""
    for agent in ("reviewer", "security-reviewer"):
        p = TP.resolve(agent, TP.AUTONOMOUS, "vivy", denied=[])
        assert unused_grant not in p.allowed, \
            f"{agent} holds {unused_grant} with no usage justifying it"


def test_write_is_scoped_by_edit_rules_and_the_scope_is_stated():
    """Write is no longer granted bare. On Claude Code 2.1.208 a `Write(<path>)` rule was
    inert, so this module once granted Write unscoped and named the hole. Claude Code
    2.1.292 applies `Edit(<path>)` to Write (permissions docs, "Read and Edit"), and a
    live run confirmed a Write outside that rule was refused and one inside it succeeded.
    So the scope is run_sandbox's Edit rules, and the policy record must say so."""
    import run_sandbox as RS
    p = TP.resolve("reviewer", TP.AUTONOMOUS, "vivy", denied=[])
    assert "Write" not in p.allowed, "a bare Write grant would reopen the unscoped hole"
    assert not any(t.startswith("Write(") for t in p.allowed)
    assert any("Edit(<path>)" in n for n in p.notes), \
        "the record must name the Edit rules that scope Write"
    scope = RS.settings(home=Path("/h"), worktree=Path("/wt"))["permissions"]["allow"]
    assert "Edit(//wt/**)" in scope and "Edit(//tmp/**)" in scope
    # The reason must not claim a sandbox the policy does not deliver.
    assert "cannot mutate the tree" not in p.reason


def test_the_gh_scoping_gap_is_declared_not_faked():
    """The finding that killed the original design: Bash rules glob-match command TEXT,
    so a flag value cannot be constrained independently of its position. A rule that
    LOOKS repo-scoped but is not would be the same theater in a new costume."""
    p = TP.resolve("reviewer", TP.AUTONOMOUS, "vivy", denied=[])
    assert any("NOT scoped" in n for n in p.notes), "the scoping gap must be stated"
    assert not any("--repo convivy/vivy" in t for t in p.allowed), "must not fake a scope"


def test_app_prefixed_crews_get_their_role_policy():
    """hollis-reviewer is a reviewer (ARCH-177/290/340). Missing this leaves every
    internal-app crew silently on the unenforced path."""
    for name in ("hollis-reviewer", "sel-security-reviewer", "svc-reviewer", "pa-reviewer"):
        p = TP.resolve(name, TP.AUTONOMOUS, "hollis", denied=[])
        assert p.enforced is True, f"{name} must resolve to its role's policy"


def test_interactive_runtime_is_a_different_policy():
    """The point of ARCH-346: the same agent, resolved for a different runtime, gets a
    differently-enforced policy. One field with two meanings is the bug; two named
    policies is the fix."""
    auto = TP.resolve("coder", TP.AUTONOMOUS, "vivy", denied=[])
    inter = TP.resolve("coder", TP.INTERACTIVE, "vivy", denied=[])
    assert auto.enforced is False and inter.enforced is True
    assert auto.runtime != inter.runtime
    assert "frontmatter" in inter.reason


def test_no_repo_means_no_gh_grant_rather_than_a_wild_one():
    """Fail closed. With nothing to scope to, granting the unscoped gh surface anyway is
    how the broad grant comes back by accident."""
    rules, caveats = TP.review_tools(None)
    assert rules == ["Bash(agentctl review-post:*)"]
    assert not caveats


def test_repo_slug_normalizes():
    assert TP.repo_slug("vivy") == "convivy/vivy"
    assert TP.repo_slug("convivy/vivy") == "convivy/vivy"
    assert TP.repo_slug(None) is None


def test_policy_record_is_complete():
    """policy.json is the audit artifact — what a run COULD do, as a fact you read rather
    than a claim you trust. Without the mode, the record is unreadable."""
    p = TP.resolve("reviewer", TP.AUTONOMOUS, "vivy", denied=["X"])
    d = p.as_dict()
    for key in ("agent", "runtime", "repo", "enforced", "permission_mode",
                "reason", "allowed", "denied", "notes"):
        assert key in d, f"policy record must carry {key}"


@pytest.mark.parametrize("agent", sorted(TP.ENFORCEABLE_AGENTS))
def test_every_enforceable_agent_can_write_its_findings(agent):
    """An enforceable agent that cannot Write cannot compose its findings file, and
    `review-post --body-file` would have nothing to post. Mined usage: `reviewer` made
    670 Write + 630 `cat >` calls, so withholding Write breaks every review."""
    import run_sandbox as RS
    p = TP.resolve(agent, TP.AUTONOMOUS, "vivy", denied=[])
    assert "Read" in p.allowed and "Grep" in p.allowed
    # Findings go to /tmp, which run_sandbox allows through the Edit rules that govern Write.
    scope = RS.settings(home=Path("/h"), worktree=Path("/wt"))["permissions"]["allow"]
    assert "Edit(//tmp/**)" in scope


@pytest.mark.parametrize("agent", sorted(TP.ENFORCEABLE_AGENTS))
def test_every_enforceable_agent_can_read_task_content(agent):
    """The operator's ruling, 2026-08-30: task content (epics and work items) should be readable
    by any agent. `ENFORCEABLE_AGENTS` is where that ruling actually lands — every other
    agent runs unenforced and could already read tasks. Both grants are GET-only against
    the control plane (GET /api/issues/{id}, GET /api/issues), so they belong on the read
    surface rather than behind a write gate."""
    p = TP.resolve(agent, TP.AUTONOMOUS, "vivy", denied=[])
    assert "mcp__ops__issue_get" in p.allowed
    assert "mcp__ops__issue_list" in p.allowed
    # Course reads are a plan, not a task, and were deliberately left out of this grant.
    assert "mcp__ops__course_get" not in p.allowed
    assert "mcp__ops__course_list" not in p.allowed
    # No mutating issue verb belongs on a read surface.
    for mutating in ("issue_create", "issue_claim", "issue_resolve", "issue_correct",
                     "issue_revise", "issue_comment", "issue_escalate", "issue_link_pr",
                     "issue_release", "issue_checkout",
                     "issue_heartbeat"):
        assert f"mcp__ops__{mutating}" not in p.allowed


# --- eval mode (round-1 revise, agent-platform#813) ---------------------------


@pytest.mark.parametrize("agent", ["reviewer", "security-reviewer"])
def test_eval_mode_grants_no_gh_rule_and_no_review_post(agent):
    """The actual fix for round 1's finding: a replay must never post, so the
    allow-list itself grants no route there — not a cooperative env-var check
    the replayed model's own Bash could strip."""
    p = TP.resolve(agent, TP.AUTONOMOUS, "agent-platform", denied=[], eval_mode=True)
    assert p.enforced is True
    assert p.permission_mode == TP.MODE_ENFORCE == "dontAsk"
    assert not any(t.startswith("Bash(gh") for t in p.allowed), \
        f"{agent}'s eval policy grants a gh rule: {p.allowed}"
    assert "Bash(agentctl review-post:*)" not in p.allowed
    # knowledge#793's verdict server is another route to stamp a review; eval mode's
    # allow-list must not carry the MCP tool that posts through it either.
    assert "mcp__verdict__review_post" not in p.allowed


def test_eval_mode_still_grants_the_read_surface_and_read_only_git():
    """The replay still has to do the review — it loses only the posting
    surface, not the ability to read the diff and write its findings.

    Unlike the ordinary (non-eval) review policy, where `Write` is withheld
    as a bare allow-list entry (knowledge#793 round 6 — a `--allowedTools`
    grant does nothing for the PERMISSION layer, since that only binds
    through `dontAsk`'s own Edit rules, run_sandbox's `permissions.allow`),
    eval mode's `allowed` DOES carry a bare `Write` (round-6 revise,
    agent-platform#813): with `gh` and `review-post` both withheld, `Write`
    is the only route left to `eval-review.json`, and `#811`'s `--tools`
    narrowing reads this allow-list to decide whether `Write` is even in the
    dispatch's tool SURFACE at all — a question the ordinary policy's own
    roster grant answers instead (see `_EVAL_MODE_RESULT_WRITE_CAVEAT`). The
    sandbox-scope check below still holds either way: a bare `Write` entry
    does not reopen the unscoped hole `test_write_is_scoped_by_edit_rules_
    and_the_scope_is_stated` guards, since run_sandbox's Edit rules are what
    actually bind under `dontAsk`."""
    import run_sandbox as RS
    p = TP.resolve("reviewer", TP.AUTONOMOUS, "agent-platform", denied=[], eval_mode=True)
    assert "Read" in p.allowed and "Grep" in p.allowed
    assert "Write" in p.allowed
    scope = RS.settings(home=Path("/h"), worktree=Path("/wt"))["permissions"]["allow"]
    assert "Edit(//tmp/**)" in scope
    for read_verb in ("git diff", "git log", "git show"):
        assert f"Bash({read_verb}:*)" in p.allowed
    assert "Bash(ruff check --isolated .)" in p.allowed  # reviewer is a _LINT_AGENTS member


def test_eval_mode_states_the_write_target_is_reachable_end_to_end():
    """Round-6 revise, continued (agent-platform#813), confinement tightened
    round 8: the eval directive's write target moved off `run_dir` (denied,
    unconditionally, to every run's own write) onto a file inside this
    dispatch's own directory (`RS.create_eval_scratch_dir`), which
    `bin/agentctl`'s own dispatcher code reads back, validates, and relocates
    into `run_dir` once the run has exited. The notes must say so plainly —
    this is the complete route now, not a tool-surface fix layered on a
    target that still refuses the write (ce71b090's honest caveat, which this
    closes), and not a target shared with any other dispatch (round-7
    finding, which round 8 closes)."""
    p = TP.resolve("reviewer", TP.AUTONOMOUS, "agent-platform", denied=[], eval_mode=True)
    assert any("create_eval_scratch_dir" in n for n in p.notes), p.notes
    assert any("reachable" in n for n in p.notes), p.notes
    assert not any("refused" in n for n in p.notes), p.notes


def test_eval_mode_enforcement_is_not_declared_conditional():
    """Ordinary review policy's FIRST note says every guarantee here is
    conditional on `AGENTCTL_ENFORCE_TOOL_POLICY` (default off). That caveat
    does not apply to eval mode — `_spawn_claude` forces `dontAsk`
    unconditionally for an eval dispatch — so the eval policy must not carry
    a note that tells the reader otherwise."""
    p = TP.resolve("reviewer", TP.AUTONOMOUS, "agent-platform", denied=[], eval_mode=True)
    assert TP._ENFORCEMENT_GATE_CAVEAT not in p.notes, \
        "the eval policy must not carry the ordinary policy's conditional-enforcement caveat"
    assert any("UNCONDITIONAL" in n for n in p.notes)


def test_eval_mode_on_a_non_review_agent_is_a_no_op():
    """`agentctl spawn` only ever passes `eval_mode=True` for a review-class
    agent (validated before `resolve` is called) — if it somehow reached a
    build-class agent, there is nothing for this resolver to grant or
    restrict beyond the deny-list, so the result must be identical."""
    without = TP.resolve("coder", TP.AUTONOMOUS, "agent-platform", denied=["X"])
    with_eval = TP.resolve("coder", TP.AUTONOMOUS, "agent-platform", denied=["X"],
                           eval_mode=True)
    assert without == with_eval


def test_the_review_roles_may_post_their_verdict_and_no_other_role_may():
    # Under enforcement, a reviewer's only output is its verdict, which the verdict server
    # posts. Without this grant an enforced reviewer would be refused its own verdict.
    for role in ("reviewer", "security-reviewer"):
        assert "mcp__verdict__review_post" in TP.resolve(role, TP.AUTONOMOUS, "vivy", denied=[]).allowed
    for role in ("scout", "planner"):
        assert "mcp__verdict__review_post" not in TP.resolve(role, TP.AUTONOMOUS, "vivy", denied=[]).allowed
