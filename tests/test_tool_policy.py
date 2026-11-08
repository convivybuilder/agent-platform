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
    run = _agentctl_without_tool_policy(tmp_path, "policy", "reviewer", "--on", "cartographer")
    assert run.returncode != 0, "must not exit 0 while printing nothing real"
    assert "not installed" in run.stderr
    assert "install.sh" in run.stderr, "say how to fix it"


def test_build_agent_admits_it_is_not_enforced():
    """The honest state. A coder needs arbitrary shell (pytest, venv, make, psql), so an
    allow-list permissive enough to work equals full Bash. `enforced: True` here would be
    a control-shaped object that controls nothing."""
    p = TP.resolve("coder", TP.AUTONOMOUS, "cartographer", denied=[_DENY_SAMPLE])
    assert p.enforced is False
    # `dontAsk`, not bypassPermissions: under bypass no allow rule binds, so the file-write
    # scope in run_sandbox could not hold. `enforced` stays False: Bash is unrestricted.
    assert p.permission_mode == TP.MODE_ENFORCE == "dontAsk"
    assert p.allowed == ()
    assert p.denied == (_DENY_SAMPLE,)
    assert "arbitrary shell" in p.reason
    assert any("NOT enforced" in n for n in p.notes)


# --- a local persona's own declared `tools:` line (issue_01M6D5D0B434781TXDPMWMB2PY) --
#
# The bug: `bin/agentctl`'s `resolve_agent` resolves a `local_agents: true` crew's own
# `.claude/agents/<name>.md` and parses its `tools:` frontmatter, but that name is
# almost never a member of `ENFORCEABLE_AGENTS` (a private crew names its own
# personas), so `resolve()` sent it to the build-class branch above — `enforced:
# False`, an empty `allowed` — while `bin/agentctl`'s own `TOOL_ALLOW` default handed
# the real dispatch bare `Bash` and the worktree's `Edit(...)` grant regardless of
# what the persona declared. `local_tools` is the fix: when given, a `base not in
# ENFORCEABLE_AGENTS` agent gets ITS OWN grant as a real, enforced allow-list instead.

def test_local_persona_with_declared_tools_is_enforced_with_exactly_its_grant():
    """Fails against the unfixed resolver: without `local_tools`, this agent name
    (not a roster role) falls to the build-class branch and returns `enforced=False,
    allowed=()` — the exact bug report (`tools: Read, Glob` reporting Bash, Edit and
    Write available)."""
    p = TP.resolve("research-assistant", TP.AUTONOMOUS, "zzz-synthetic-crew", denied=[],
                   local_tools=("Read", "Glob"))
    assert p.enforced is True
    assert p.permission_mode == TP.MODE_ENFORCE == "dontAsk"
    assert set(p.allowed) == {"Read", "Glob"}
    for denied_by_omission in ("Bash", "Edit", "Write", "NotebookEdit"):
        assert denied_by_omission not in p.allowed


def test_local_persona_with_no_tools_line_keeps_the_build_class_default():
    """`local_tools=None` (the default — an agent with no `tools:` line at all, or
    any roster/Library agent) must resolve byte-identical to today's build-class
    policy; this is the regression pin that a future change to this branch must not
    silently narrow or widen."""
    without_param = TP.resolve("research-assistant", TP.AUTONOMOUS, "zzz-synthetic-crew",
                               denied=[_DENY_SAMPLE])
    with_none = TP.resolve("research-assistant", TP.AUTONOMOUS, "zzz-synthetic-crew",
                           denied=[_DENY_SAMPLE], local_tools=None)
    assert without_param == with_none
    assert with_none.enforced is False
    assert with_none.allowed == ()


def test_local_persona_declared_empty_tools_list_is_still_a_real_declaration():
    """An explicit `tools: []` (the key present, the list empty) is "this persona
    needs nothing" — a real, enforced answer of zero tools, not the build-class
    default. `bin/agentctl`'s `_local_persona_tool_rules` is what tells this case
    apart from the key's absence; this module just has to honor whatever tuple it
    is given, empty included."""
    p = TP.resolve("research-assistant", TP.AUTONOMOUS, "zzz-synthetic-crew", denied=[],
                   local_tools=())
    assert p.enforced is True
    assert p.allowed == ()


def test_local_persona_unmappable_tool_entry_is_dropped_and_named():
    """Reject or ignore what cannot be mapped, failing toward fewer tools, and say
    which one was dropped — the declared contract. `FooBarNotATool` is neither a
    recognized Claude Code builtin nor an `mcp__`-prefixed name."""
    p = TP.resolve("research-assistant", TP.AUTONOMOUS, "zzz-synthetic-crew", denied=[],
                   local_tools=("Read", "FooBarNotATool"))
    assert p.allowed == ("Read",)
    assert any("FooBarNotATool" in n and "DROPPED" in n for n in p.notes), p.notes


def test_local_persona_mcp_tool_entry_passes_through_this_policys_own_allow_list():
    """This resolver carries an `mcp__`-prefixed name through unfiltered — the
    private-dispatch wall that actually keeps it from reaching a run (ADR-0043,
    `bin/agentctl`'s own `_is_private_dispatch`) is a different layer, out of this
    module's own scope, and the policy's notes say so rather than silently
    stripping the rule and hiding the real mechanism."""
    p = TP.resolve("research-assistant", TP.AUTONOMOUS, "zzz-synthetic-crew", denied=[],
                   local_tools=("Read", "mcp__ops__issue_get"))
    assert "mcp__ops__issue_get" in p.allowed
    assert any("ADR-0043" in n for n in p.notes)


def test_local_persona_enforcement_is_unconditional():
    """Mirrors `test_web_researchers_enforcement_caveat_says_it_is_unconditional`:
    a local persona's own grant cannot wait on a canary period either — it already
    says what it needs, so denying the rest must bind on the first dispatch."""
    p = TP.resolve("research-assistant", TP.AUTONOMOUS, "zzz-synthetic-crew", denied=[],
                   local_tools=("Read",))
    assert p.enforced is True
    assert "UNCONDITIONAL" in p.notes[-1]
    assert TP._ENFORCEMENT_GATE_CAVEAT not in p.notes


def test_local_persona_tools_are_ignored_when_the_name_collides_with_a_roster_role():
    """`local_tools` is consulted ONLY on the `base not in ENFORCEABLE_AGENTS`
    branch: a local persona whose chosen name happens to collide with a roster
    role (here, `scout`) still gets that role's own policy, unaffected by whatever
    it declared locally — `ENFORCEABLE_AGENTS` membership is keyed on name alone,
    and this module has no way to know the dispatch came from a local file at all."""
    with_local_tools = TP.resolve("scout", TP.AUTONOMOUS, "zzz-synthetic-crew", denied=[],
                                  local_tools=("Read",))
    without = TP.resolve("scout", TP.AUTONOMOUS, "zzz-synthetic-crew", denied=[])
    assert with_local_tools == without


def test_a_roster_agent_is_unaffected_by_local_tools():
    """A roster agent, and every agent on a public repository, is unchanged: this
    just confirms the parameter is a true no-op for a build-class roster name too
    when a caller never gives it a local persona's own grant (which `bin/agentctl`
    never does for a Library-resolved agent — `_local_persona_tool_rules` always
    returns `None` for one)."""
    assert TP.resolve("coder", TP.AUTONOMOUS, "cartographer", denied=[]) == TP.resolve(
        "coder", TP.AUTONOMOUS, "cartographer", denied=[], local_tools=None)


def test_review_agent_is_enforced_under_dontask():
    """dontAsk is the only mode where an allow-list binds. `--allowedTools` under
    bypassPermissions is INERT — verified empirically; a first cut of this module would
    have recorded `enforced: true` while restricting nothing."""
    p = TP.resolve("reviewer", TP.AUTONOMOUS, "cartographer", denied=[])
    assert p.enforced is True
    assert p.permission_mode == TP.MODE_ENFORCE == "dontAsk"
    assert "Read" in p.allowed and "Grep" in p.allowed
    # Write is scoped by run_sandbox's Edit rules, never granted bare here.
    assert "Write" not in p.allowed


def test_every_enforced_policy_says_its_guarantees_are_conditional():
    """The deepest version of this module's own failure mode, found by demonstration
    rather than argument (security review, PR #391 round 5).

    `reason` and the caveats state their guarantees unconditionally, but — for
    `planner`/`scout`, which still have the switch — the allow-list binds only when
    `AGENTCTL_ENFORCE_TOOL_POLICY` is set — default OFF — ANDed with `policy.enforced`.
    A reviewer proved this on its own dispatch BEFORE
    platform/specs/reviewers-run-under-their-allow-list: running under
    `bypassPermissions`, it successfully executed the very `ruff --fix` the exact-match
    grant denies. So a reader of `policy.json` who trusted `reason` and `enforced: true`
    would be wrong about the run in front of them.

    That is the "control-shaped object that controls nothing" this module's docstring
    exists to prevent, reappearing one level up in the module's own audit record. The
    caveat goes FIRST because it qualifies every other line.

    `reviewer`/`security-reviewer` moved off this gate entirely (see
    `test_the_review_roles_enforcement_caveat_says_it_is_unconditional` below) — a plain
    substring check for "CONDITIONAL" would pass by accident on their notes too, since
    "UNCONDITIONAL" contains it; this test is scoped to the two agents the gate still
    describes, and the other test pins the review roles' own, opposite wording."""
    for agent in ("planner", "scout"):
        p = TP.resolve(agent, TP.AUTONOMOUS, "cartographer", denied=[])
        if not p.enforced:
            continue
        assert p.notes, f"{agent} records an enforced policy with no notes at all"
        assert "CONDITIONAL" in p.notes[0] and "AGENTCTL_ENFORCE_TOOL_POLICY" in p.notes[0], \
            f"{agent}'s policy states guarantees without saying enforcement is gated"


def test_the_review_roles_enforcement_caveat_says_it_is_unconditional():
    """platform/specs/reviewers-run-under-their-allow-list: `reviewer` and
    `security-reviewer` get the allow-list applied to the run's own settings
    unconditionally, with no `AGENTCTL_ENFORCE_TOOL_POLICY` gate at all — the fix for
    the incident where a security-reviewer's `python3 - <<'EOF'` booted out seven live
    launchd jobs (run-4a2b213d, 2026-10-16) while that env var sat off. Their notes[0]
    must say so, not carry the OTHER agents' conditional caveat."""
    for agent in ("reviewer", "security-reviewer"):
        p = TP.resolve(agent, TP.AUTONOMOUS, "cartographer", denied=[])
        assert p.enforced is True
        assert p.notes, f"{agent} records an enforced policy with no notes at all"
        assert "UNCONDITIONAL" in p.notes[0], \
            f"{agent}'s policy does not say its enforcement is unconditional"
        assert p.notes[0] == TP._VERDICT_ENFORCEMENT_CAVEAT
        assert TP._ENFORCEMENT_GATE_CAVEAT not in p.notes, \
            f"{agent} still carries the OTHER agents' conditional-enforcement caveat"


def test_a_reviewer_cannot_mutate_the_tree_it_reviews():
    """The property the allow-list actually buys. Confirmed live: `git push` is refused
    by the permission layer under dontAsk, not by the model's goodwill."""
    p = TP.resolve("reviewer", TP.AUTONOMOUS, "cartographer", denied=[])
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
        p = TP.resolve(agent, TP.AUTONOMOUS, "cartographer", denied=[])
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
        p = TP.resolve(agent, TP.AUTONOMOUS, "cartographer", denied=[])
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
    """`ENFORCEABLE_AGENTS` has seven members; only `reviewer` and `security-reviewer`
    are told to lint (`shadow-reviewer` isn't either — `_LINT_AGENTS` stays the two
    literal names, CQ·526). A grant spliced into the shared `allowed` list would land
    on all seven, widening the surface past any cited usage — the rule
    `test_no_write_verb_survives_without_usage_to_justify_it` already enforces for
    `Edit` and `TodoWrite`. Round-1 reviewer finding on PR #391."""
    p = TP.resolve(agent, TP.AUTONOMOUS, "cartographer", denied=[])
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
        p = TP.resolve(agent, TP.AUTONOMOUS, "cartographer", denied=[])
        assert unused_grant not in p.allowed, \
            f"{agent} holds {unused_grant} with no usage justifying it"


def test_write_is_scoped_by_edit_rules_and_the_scope_is_stated():
    """Write is no longer granted bare. On Claude Code 2.1.208 a `Write(<path>)` rule was
    inert, so this module once granted Write unscoped and named the hole. Claude Code
    2.1.292 applies `Edit(<path>)` to Write (permissions docs, "Read and Edit"), and a
    live run confirmed a Write outside that rule was refused and one inside it succeeded.
    So the scope is run_sandbox's Edit rules, and the policy record must say so."""
    import run_sandbox as RS
    p = TP.resolve("reviewer", TP.AUTONOMOUS, "cartographer", denied=[])
    assert "Write" not in p.allowed, "a bare Write grant would reopen the unscoped hole"
    assert not any(t.startswith("Write(") for t in p.allowed)
    assert any("Edit(<path>)" in n for n in p.notes), \
        "the record must name the Edit rules that scope Write"
    scope = RS.settings(home=Path("/h"), worktree=Path("/wt"))["permissions"]["allow"]
    assert "Edit(//wt/**)" in scope and "Edit(//tmp/**)" in scope
    # The reason must not claim a sandbox the policy does not deliver.
    assert "cannot mutate the tree" not in p.reason


@pytest.mark.parametrize("agent", ["reviewer", "security-reviewer"])
def test_the_review_roles_get_their_own_write_scope_caveat_not_the_tmp_one(agent):
    """Round 2 of #925's security-reviewer finding: `_WRITE_SCOPE_CAVEAT` says `Write`
    and `NotebookEdit` reach the run's worktree and /tmp, but `run_sandbox.settings`'s
    `review_allowed` branch drops the /tmp grant for `reviewer`/`security-reviewer` and
    substitutes their own draft directory — so at head 33991502a that note was written,
    verbatim, into these two roles' own `policy.json`, stating a write scope the run does
    not have. `_REVIEW_DRAFT_WRITE_SCOPE_CAVEAT` must stand in for it instead, and the
    stale note must not also ride along."""
    p = TP.resolve(agent, TP.AUTONOMOUS, "cartographer", denied=[])
    assert TP._REVIEW_DRAFT_WRITE_SCOPE_CAVEAT in p.notes, \
        f"{agent}'s policy does not carry the draft-dir write-scope note"
    assert TP._WRITE_SCOPE_CAVEAT not in p.notes, \
        f"{agent}'s policy still carries the /tmp-scoped note, which is false for this class"


def test_a_non_review_enforceable_agent_keeps_the_tmp_write_scope_caveat():
    """The other four `ENFORCEABLE_AGENTS` members (`planner`, `platform-planner`,
    `design-critic`, `scout`) never take `run_sandbox.settings`'s `review_allowed`
    branch (`_is_review_agent` gates it to `reviewer`/`security-reviewer` alone in
    `bin/agentctl`) — their `allowWrite` still reaches /tmp, so `_WRITE_SCOPE_CAVEAT`
    stays true for them and must stay in their notes."""
    p = TP.resolve("scout", TP.AUTONOMOUS, "cartographer", denied=[])
    assert TP._WRITE_SCOPE_CAVEAT in p.notes
    assert TP._REVIEW_DRAFT_WRITE_SCOPE_CAVEAT not in p.notes


@pytest.mark.parametrize("agent", ["reviewer", "security-reviewer"])
@pytest.mark.parametrize("eval_mode", [False, True])
def test_review_write_scope_notes_name_the_env_var_not_a_launch_path_specific_path(
        agent, eval_mode):
    """Round 3 of #925 (both reviewers, round 3). Before this round, the ordinary
    (non-eval) review policy's write-scope note (`_REVIEW_DRAFT_WRITE_SCOPE_CAVEAT`)
    named a literal path — `~/.local/state/agent-platform/review-drafts/<worktree
    name>` — which is only the direct-dispatch path's own directory; the two-account
    launcher's draft directory is a different path under a different account
    (`review_launch.main`'s `run_dir / "draft"`). The eval-mode write note
    (`_EVAL_MODE_RESULT_WRITE_CAVEAT`) separately claimed the ordinary review policy
    "REACHES THE WHOLE SHARED TEMP ROOT" — false since round 2 of #925, which gave
    the two review roles their own draft directory instead of the shared root. Both
    are fixed to state scope by mechanism: no literal `/tmp` path, no claim that the
    ordinary review policy reaches a shared temp root, and (for the ordinary policy)
    naming `AGENTCTL_REVIEW_DRAFT_DIR`, the one thing both launch paths actually
    agree on — the env var that names whichever directory a given dispatch created."""
    p = TP.resolve(agent, TP.AUTONOMOUS, "cartographer", denied=[], eval_mode=eval_mode)
    blob = " ".join(p.notes)
    assert "/tmp" not in blob, f"a review-class note still names a literal /tmp path: {blob}"
    assert "whole shared temp root" not in blob.lower(), (
        f"a review-class note still claims the ordinary (non-eval) review policy "
        f"reaches a shared temp root: {blob}")
    if not eval_mode:
        assert "AGENTCTL_REVIEW_DRAFT_DIR" in blob, (
            "the ordinary review policy's write-scope note must name the env var "
            "that carries the real directory, not a hardcoded path")


def test_the_gh_scoping_gap_is_declared_not_faked():
    """The finding that killed the original design: Bash rules glob-match command TEXT,
    so a flag value cannot be constrained independently of its position. A rule that
    LOOKS repo-scoped but is not would be the same theater in a new costume."""
    p = TP.resolve("reviewer", TP.AUTONOMOUS, "cartographer", denied=[])
    assert any("NOT scoped" in n for n in p.notes), "the scoping gap must be stated"
    assert not any("--repo convivy/cartographer" in t for t in p.allowed), "must not fake a scope"


@pytest.mark.rich_registry
def test_app_prefixed_crews_get_their_role_policy():
    """atlas-reviewer is a reviewer (ARCH-177/290/340). Missing this leaves every
    internal-app crew silently on the unenforced path."""
    for name in ("atlas-reviewer", "sel-security-reviewer", "relay-reviewer", "aide-reviewer"):
        p = TP.resolve(name, TP.AUTONOMOUS, "atlas", denied=[])
        assert p.enforced is True, f"{name} must resolve to its role's policy"


@pytest.mark.rich_registry
@pytest.mark.parametrize("agent,base_role", [
    ("deck-reviewer", "reviewer"),
    ("deck-security-reviewer", "security-reviewer"),
    ("sel-reviewer", "reviewer"),
    ("sel-security-reviewer", "security-reviewer"),
    ("relay-reviewer", "reviewer"),
    ("sg-reviewer", "reviewer"),
])
def test_crew_prefixed_reviewers_get_their_base_roles_allow_list(agent, base_role):
    """ARCH-397: `strip_app_prefix` used to be its own regex
    (`^(atlas|aide|sel|relay)-`), a second list independent of `bin/agentctl`'s
    `_INTERNAL_APP_PREFIXES` that never learned `deck-`, `orient-`, `platform-`,
    `sg-`, `sig-`, or `cart-`. A crew-prefixed reviewer under one of those missing
    prefixes fell through to the unenforced build-class branch with an EMPTY
    allow-list — `deck-reviewer` run-73ad84c0 and `deck-security-reviewer`
    run-3ec54e32 (northwind-deck#378, 2026-10-20) both ended errored, having posted no
    verdict, because `gh`/`agentctl review-ci`/`mcp__verdict__review_post` were all
    denied under `dontAsk` with nothing granted. `sg-reviewer` (styleguide's own
    reviewer, `convivy/knowledge`'s `projects/styleguide/agents/sg-reviewer.md`)
    carried the exact same `sg-` gap.

    This fails against the unfixed code on the `p.allowed` assertions below: a
    crew-prefixed reviewer resolved to `()` (empty) rather than its base role's
    allow-list, and `p.reason` read "build-class agent" rather than the review-class
    reason."""
    p = TP.resolve(agent, TP.AUTONOMOUS, "finch", denied=[])
    base_p = TP.resolve(base_role, TP.AUTONOMOUS, "finch", denied=[])
    assert p.enforced is True
    assert p.allowed, f"{agent} must not resolve to an empty allow-list"
    assert p.allowed == base_p.allowed
    assert "build-class" not in p.reason
    assert p.reason == base_p.reason


@pytest.mark.rich_registry
@pytest.mark.parametrize("agent", [
    "deck-reviewer", "deck-security-reviewer", "sel-reviewer",
    "sel-security-reviewer", "relay-reviewer", "sg-reviewer",
])
def test_crew_prefixed_reviewers_settings_carry_the_review_grants(agent, tmp_path):
    """The resolved policy has to actually reach `run_sandbox.settings` and produce
    a real `permissions.allow`, not just look right in isolation."""
    import run_sandbox as RS
    p = TP.resolve(agent, TP.AUTONOMOUS, "finch", denied=[])
    allow = RS.settings(home=tmp_path / "h", worktree=tmp_path / "wt",
                        review_allowed=p.allowed)["permissions"]["allow"]
    assert allow, f"{agent}'s settings must not carry an empty allow-list"
    assert any(r.startswith("Bash(gh pr checks") for r in allow)
    assert TP._VERDICT_TOOL in allow
    assert RS.TOOL_ALLOW[0] not in allow, "must not fall back to the build-class TOOL_ALLOW grant"


@pytest.mark.parametrize("agent", ["deck-coder", "sel-coder"])
def test_crew_coders_resolve_exactly_as_before(agent):
    """Widening the crew-prefix stripping must not touch a build-class crew worker:
    `deck-coder`/`sel-coder` still need arbitrary shell, and still get the honest
    unenforced policy — pinned to the exact before value, not merely "still
    unenforced", so a future change that alters the build-class text's wording has
    to touch this test too."""
    before = TP.resolve("coder", TP.AUTONOMOUS, "cartographer", denied=[])
    p = TP.resolve(agent, TP.AUTONOMOUS, "cartographer", denied=[])
    assert p.enforced is before.enforced is False
    assert p.allowed == before.allowed == ()
    assert p.permission_mode == before.permission_mode
    assert p.reason == before.reason
    assert p.notes == before.notes


@pytest.mark.rich_registry
def test_cart_planner_moves_from_build_class_to_the_enforced_planner_policy():
    """The same widening that fixed the reviewers also moves `cart-planner`
    (`convivy/knowledge`'s `projects/cartographer/agents/cart-planner.md`): `cart-` was
    not in the old `^(atlas|aide|sel|relay)-` regex, so `cart-planner` resolved
    unchanged and `"cart-planner"` is not itself a member of `ENFORCEABLE_AGENTS`
    — build-class, unenforced. `app_prefixes.internal_app_prefixes()` carries
    `cart-`, so it now strips to `planner`, which IS a member: enforced, with
    `planner`'s own allow-list."""
    planner_p = TP.resolve("planner", TP.AUTONOMOUS, "finch", denied=[])
    cart_p = TP.resolve("cart-planner", TP.AUTONOMOUS, "finch", denied=[])
    assert cart_p.enforced is True
    assert "build-class" not in cart_p.reason
    assert cart_p.allowed == planner_p.allowed
    assert cart_p.reason == planner_p.reason
    assert cart_p.notes == planner_p.notes


def test_platform_planner_resolves_the_same_as_before_this_pr():
    """`platform-planner` is its own literal entry in `ENFORCEABLE_AGENTS`
    (alongside `planner`), so before this PR it was already enforced without ever
    being stripped — the old regex did not know `platform-` either. This PR's
    widening makes `strip_app_prefix("platform-planner")` return `"planner"`
    instead of leaving it unchanged, which resolves through a different branch of
    `ENFORCEABLE_AGENTS` membership but must land on the exact same policy, since
    neither `"planner"` nor `"platform-planner"` is in `_LINT_AGENTS` or
    `_VERDICT_AGENTS` — the two sets every other branch of `resolve()` actually
    keys on. Pinned so a future narrowing of the prefix tuple cannot silently move
    `platform-planner` off of `planner`'s policy without a test noticing."""
    planner_p = TP.resolve("planner", TP.AUTONOMOUS, "cartographer", denied=[])
    platform_p = TP.resolve("platform-planner", TP.AUTONOMOUS, "cartographer", denied=[])
    assert platform_p.enforced is True
    assert platform_p.allowed == planner_p.allowed
    assert platform_p.reason == planner_p.reason
    assert platform_p.notes == planner_p.notes
    assert platform_p.permission_mode == planner_p.permission_mode


def test_interactive_runtime_is_a_different_policy():
    """The point of ARCH-346: the same agent, resolved for a different runtime, gets a
    differently-enforced policy. One field with two meanings is the bug; two named
    policies is the fix."""
    auto = TP.resolve("coder", TP.AUTONOMOUS, "cartographer", denied=[])
    inter = TP.resolve("coder", TP.INTERACTIVE, "cartographer", denied=[])
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
    assert TP.repo_slug("cartographer") == "convivy/cartographer"
    assert TP.repo_slug("convivy/cartographer") == "convivy/cartographer"
    assert TP.repo_slug(None) is None


@pytest.mark.rich_registry
def test_repo_slug_uses_the_registered_owner_for_homebrew_tap():
    """Before this repo's `repo_owners` entry existed, this read
    convivy/brewcask-tap, the wrong owner."""
    assert TP.repo_slug("brewcask-tap") == "caskforge/brewcask-tap"


def test_policy_record_is_complete():
    """policy.json is the audit artifact — what a run COULD do, as a fact you read rather
    than a claim you trust. Without the mode, the record is unreadable."""
    p = TP.resolve("reviewer", TP.AUTONOMOUS, "cartographer", denied=["X"])
    d = p.as_dict()
    for key in ("agent", "runtime", "repo", "enforced", "permission_mode",
                "reason", "allowed", "denied", "notes"):
        assert key in d, f"policy record must carry {key}"


@pytest.mark.parametrize("agent", sorted(TP.ENFORCEABLE_AGENTS - {"web-researcher"}))
def test_every_enforceable_agent_can_write_its_findings(agent):
    """An enforceable agent that cannot Write cannot compose its findings file, and
    `review-post --body-file` would have nothing to post. Mined usage: `reviewer` made
    670 Write + 630 `cat >` calls, so withholding Write breaks every review.

    `web-researcher` is excluded (`test_web_researcher_has_no_file_tool_grant` covers
    it instead): its own roster says "READ-ONLY, with no file, shell or repository
    tools" and its final text IS its output, never a findings file it writes and a
    later step reads back."""
    import run_sandbox as RS
    p = TP.resolve(agent, TP.AUTONOMOUS, "cartographer", denied=[])
    assert "Read" in p.allowed and "Grep" in p.allowed
    # Findings go to /tmp, which run_sandbox allows through the Edit rules that govern Write.
    scope = RS.settings(home=Path("/h"), worktree=Path("/wt"))["permissions"]["allow"]
    assert "Edit(//tmp/**)" in scope


@pytest.mark.parametrize("agent", sorted(TP.ENFORCEABLE_AGENTS - {"web-researcher"}))
def test_every_enforceable_agent_can_read_task_content(agent):
    """The operator's ruling, 2026-08-30: task content (epics and work items) should be readable
    by any agent. `ENFORCEABLE_AGENTS` is where that ruling actually lands — every other
    agent runs unenforced and could already read tasks. Both grants are GET-only against
    the control plane (GET /api/issues/{id}, GET /api/issues), so they belong on the read
    surface rather than behind a write gate.

    `web-researcher` is excluded: its own grant (`TP._WEB_RESEARCH_SURFACE`) is its
    roster's four tools alone, never the shared `_READ_SURFACE` every other member
    gets — see `test_web_researcher_has_no_file_tool_grant`."""
    p = TP.resolve(agent, TP.AUTONOMOUS, "cartographer", denied=[])
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


# --- web-researcher runs under an enforced allow-list of exactly its own grant ----
#
# Before this fix, `web-researcher` was not a member of `ENFORCEABLE_AGENTS`, so
# `resolve()` fell into the `base not in ENFORCEABLE_AGENTS` branch: `enforced=False`,
# the deny-list-only state every build-class agent gets, though the agent's whole job
# is reading untrusted web pages and needs no shell at all.


def test_web_researcher_is_enforced_with_exactly_its_roster_grant():
    """Fails against the unfixed code: `web-researcher` was not in
    `ENFORCEABLE_AGENTS`, so this resolved `enforced=False` with an empty
    `allowed` tuple (the honest "deny-list only" build-class state) rather than
    the enforced policy asserted here."""
    p = TP.resolve("web-researcher", TP.AUTONOMOUS, "knowledge", denied=[])
    assert p.enforced is True
    assert p.permission_mode == TP.MODE_ENFORCE == "dontAsk"
    assert set(p.allowed) == {"WebSearch", "WebFetch", "mcp__kb__kb_search", "mcp__kb__kb_get"}


def test_web_researcher_has_no_file_tool_grant():
    """Its own roster (platform/agents/web-researcher) says "READ-ONLY, with no
    file, shell or repository tools" — unlike every other `ENFORCEABLE_AGENTS`
    member, it gets none of `_READ_SURFACE`, `_GIT_READ`, or `review_tools`'s
    `gh` grant."""
    p = TP.resolve("web-researcher", TP.AUTONOMOUS, "knowledge", denied=[])
    for builtin in ("Read", "Grep", "Glob", "Write", "Edit", "Bash"):
        assert builtin not in p.allowed, f"web-researcher must not hold {builtin}"
    assert not any(t.startswith("Bash(") for t in p.allowed)
    assert "mcp__ops__issue_get" not in p.allowed
    assert "mcp__ops__issue_list" not in p.allowed


def test_web_researcher_is_not_review_or_lint_class():
    """It posts no verdict and runs no lint command — it is enforced for the same
    reason `scout`/`planner` are (no shell needed), not because it joined the
    review class."""
    p = TP.resolve("web-researcher", TP.AUTONOMOUS, "knowledge", denied=[])
    assert "web-researcher" not in TP._VERDICT_AGENTS
    assert "web-researcher" not in TP._LINT_AGENTS
    assert TP._VERDICT_TOOL not in p.allowed
    assert not any("ruff" in rule for rule in p.allowed)


def test_web_researcher_is_the_one_always_enforced_agent():
    """Security review of PR #1044 round 1: `enforced: true` on the `Policy` this
    resolver returns means only that the policy CLASS is enforceable
    (`_ENFORCEMENT_GATE_CAVEAT`) — whether a given run actually applies it is a
    separate question `bin/agentctl`'s `enforcing`/`_enforcement_active` answer, and
    both were still gated by `AGENTCTL_ENFORCE_TOOL_POLICY` for `web-researcher` same
    as `scout`/`planner`, though its one job is reading content an attacker controls.
    `ALWAYS_ENFORCED_AGENTS` is the set `bin/agentctl` now reads instead, to drop that
    gate for exactly this agent and no other — it must not pick up `scout` or
    `planner`, and it must stay disjoint from `_VERDICT_AGENTS` (`web-researcher`
    posts no verdict and must never gain the verdict tool or the review draft-write
    scope that set's own members carry)."""
    assert TP.ALWAYS_ENFORCED_AGENTS == frozenset({"web-researcher"})
    assert not TP.ALWAYS_ENFORCED_AGENTS & TP._VERDICT_AGENTS


def test_web_researchers_enforcement_caveat_says_it_is_unconditional():
    """Mirrors `test_the_review_roles_enforcement_caveat_says_it_is_unconditional`
    for the other unconditional class: `web-researcher`'s notes[0] must say
    enforcement cannot wait on `AGENTCTL_ENFORCE_TOOL_POLICY`, not carry the
    conditional caveat every other non-`_VERDICT_AGENTS` enforceable agent gets."""
    p = TP.resolve("web-researcher", TP.AUTONOMOUS, "knowledge", denied=[])
    assert p.enforced is True
    assert p.notes, "web-researcher records an enforced policy with no notes at all"
    assert "UNCONDITIONAL" in p.notes[0], \
        "web-researcher's policy does not say its enforcement is unconditional"
    assert p.notes[0] == TP._ALWAYS_ENFORCED_CAVEAT
    assert TP._ENFORCEMENT_GATE_CAVEAT not in p.notes, \
        "web-researcher still carries the OTHER agents' conditional-enforcement caveat"


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
    # `shadow-reviewer` posts one too (CQ·526, non-gating) — same mechanism, same grant.
    for role in ("reviewer", "security-reviewer", "shadow-reviewer"):
        assert "mcp__verdict__review_post" in TP.resolve(role, TP.AUTONOMOUS, "cartographer", denied=[]).allowed
    for role in ("scout", "planner"):
        assert "mcp__verdict__review_post" not in TP.resolve(role, TP.AUTONOMOUS, "cartographer", denied=[]).allowed


# --- platform/specs/reviewers-run-under-their-allow-list ----------------------


def test_the_review_roles_get_the_new_ci_git_and_kb_grants():
    """The measured additions: CI state of the reviewed commit through the new
    wrapper, more read-only git, and the two read-only `kb` tools — all scoped to
    `reviewer`/`security-reviewer` alone, the same "only if real usage needs it"
    rule `_LINT_AGENTS` already follows."""
    for role in ("reviewer", "security-reviewer"):
        allowed = TP.resolve(role, TP.AUTONOMOUS, "cartographer", denied=[]).allowed
        assert "Bash(agentctl review-ci)" in allowed
        assert "mcp__kb__kb_get" in allowed
        assert "mcp__kb__kb_search" in allowed
        for verb in ("merge-base", "ls-tree", "ls-files", "cat-file"):
            assert f"Bash(git {verb}:*)" in allowed
        assert "Bash(git remote -v)" in allowed
        # Exact, not a prefix: `git remote` has mutating subcommands.
        assert "Bash(git remote -v:*)" not in allowed
        assert "Bash(agentctl review-ci:*)" not in allowed, \
            "the CI wrapper takes no arguments and must not be a prefix grant"


def test_the_review_roles_get_only_exact_argument_free_branch_reads():
    """Round 1 of #925, security-reviewer: `Bash(git branch:*)` grants every
    `git branch` form, including `-f <name> <sha>`, `-D`, `-m` and `-c`, which
    write refs — a live permission-layer check (2026-10-17) found `git branch
    -f calib-probe HEAD` and `git branch -D calib-probe` ALLOWED under that
    grant. The review worktree shares its ref store with the primary checkout
    and every other worktree, so this could move or delete a branch other
    checkouts read. The resolved policy must carry none of the prefix grant
    and exactly the four argument-free read forms, each an exact match with
    no `:*` for a write verb to be appended to."""
    for role in ("reviewer", "security-reviewer"):
        allowed = TP.resolve(role, TP.AUTONOMOUS, "cartographer", denied=[]).allowed
        assert "Bash(git branch:*)" not in allowed
        for form in ("Bash(git branch)", "Bash(git branch -a)", "Bash(git branch -r)",
                    "Bash(git branch --show-current)"):
            assert form in allowed


@pytest.mark.parametrize("agent", ["planner", "platform-planner", "design-critic", "scout"])
def test_the_new_review_grants_do_not_reach_the_other_enforceable_agents(agent):
    """The measurement behind `Bash(agentctl review-ci)`, the extra read-only git,
    and the two `kb` reads is 470 reviewer/security-reviewer runs, not these four
    agents' — splicing them into the shared `allowed` list would widen the surface
    past any cited usage, the same drift `test_the_lint_grant_reaches_only_the_
    agents_whose_roster_asks_for_it` already guards against for `ruff`."""
    allowed = TP.resolve(agent, TP.AUTONOMOUS, "cartographer", denied=[]).allowed
    assert "Bash(agentctl review-ci)" not in allowed
    assert "mcp__kb__kb_get" not in allowed
    assert "mcp__kb__kb_search" not in allowed
    assert "Bash(git merge-base:*)" not in allowed
    assert "Bash(git remote -v)" not in allowed


# --- CQ·526 round 1 revise: shadow-reviewer classified the way reviewer is -----
#
# The round-1 reviewer finding on PR #960: `bin/agentctl`'s `_is_review_agent`
# widening sends a `shadow-reviewer` dispatch down `_run_session`'s review-class
# sandboxing path (`review_allowed = tuple(policy.allowed) if
# _is_review_agent(args.agent) else None`), but at head b11556ed5 this module's
# `ENFORCEABLE_AGENTS`/`_VERDICT_AGENTS` did not know the role existed, so
# `resolve("shadow-reviewer", ...)` fell into the `base not in ENFORCEABLE_AGENTS`
# branch and returned `enforced=False, allowed=()`. `review_allowed` then became
# `()` rather than `None`, which trips `run_sandbox.settings()`'s `review_allowed
# is not None` branch and sets the run's entire `permissions.allow` to
# `["ToolSearch"]` — every other tool denied, including `Read`/`Grep`/`Glob`, so a
# shadow-reviewer dispatched on a Claude model (a plain `--model` override; nothing
# refuses one) could not read the diff it was dispatched to review.

def test_shadow_reviewer_resolves_the_same_allow_list_as_reviewer_minus_the_lint_grant():
    """Fails at head b11556ed5: `shadow-reviewer` was not in `ENFORCEABLE_AGENTS`,
    so this returned `enforced=False, allowed=()` instead of the review-class
    surface below. The lint grant (`ruff`) is the one deliberate difference:
    `_LINT_AGENTS` stays the two literal names, since nothing measured
    `shadow-reviewer`'s own usage against it."""
    reviewer = TP.resolve("reviewer", TP.AUTONOMOUS, "cartographer", denied=[])
    shadow = TP.resolve("shadow-reviewer", TP.AUTONOMOUS, "cartographer", denied=[])
    assert shadow.enforced is True
    assert shadow.permission_mode == reviewer.permission_mode == TP.MODE_ENFORCE
    assert set(reviewer.allowed) - set(shadow.allowed) == {"Bash(ruff check --isolated .)"}
    assert set(shadow.allowed) - set(reviewer.allowed) == set()
    assert "mcp__verdict__review_post" in shadow.allowed


def test_shadow_reviewer_gets_the_draft_write_scope_caveat_not_the_tmp_one():
    """Same treatment as `reviewer`/`security-reviewer` (round 2 of #925): the
    unscoped `/tmp` caveat would be false for a role whose `allowWrite` is its
    own draft directory, not the shared temp root."""
    p = TP.resolve("shadow-reviewer", TP.AUTONOMOUS, "cartographer", denied=[])
    assert TP._REVIEW_DRAFT_WRITE_SCOPE_CAVEAT in p.notes
    assert TP._WRITE_SCOPE_CAVEAT not in p.notes


def test_a_shadow_reviewer_run_on_a_claude_model_gets_the_same_tool_surface_a_reviewer_run_gets():
    """The exact mechanism the round-1 finding named: `_run_session` builds
    `review_allowed` from `tool_policy.resolve(...).allowed` and feeds it straight
    into `run_sandbox.settings()`. Fails at head b11556ed5, where `shadow-reviewer`'s
    `allowed` was `()`, so `review_allowed is not None` was true but empty, and
    `base_allow` collapsed to `["ToolSearch"]` alone — Read/Grep/Glob and every
    `gh pr` rule denied, so the dispatched run could not read the diff it was meant
    to review."""
    import run_sandbox as RS
    policy = TP.resolve("shadow-reviewer", TP.AUTONOMOUS, "cartographer", denied=[])
    review_allowed = tuple(policy.allowed)
    got = RS.settings(home=Path("/h"), worktree=Path("/wt"),
                      review_allowed=review_allowed)["permissions"]["allow"]
    want = RS.settings(
        home=Path("/h"), worktree=Path("/wt"),
        review_allowed=tuple(TP.resolve("reviewer", TP.AUTONOMOUS, "cartographer",
                                        denied=[]).allowed))["permissions"]["allow"]
    assert got != ["ToolSearch"], \
        "shadow-reviewer's run would be denied everything but ToolSearch"
    assert "Read" in got and "Grep" in got and "Glob" in got
    assert "Bash(gh pr view:*)" in got
    # Same surface a reviewer dispatch gets, modulo the lint grant reviewer alone holds.
    assert set(want) - set(got) == {"Bash(ruff check --isolated .)"}


def test_tool_policys_review_class_set_agrees_with_agentctls_is_review_agent(agentctl):
    """`_VERDICT_AGENTS` is this module's review-class set — the one `resolve()`
    branch keys both the verdict grant and the review-class sandboxing caveats on.
    It must agree, name for name, with `bin/agentctl`'s own `_is_review_agent`
    (`_REVIEW_AGENTS + _SHADOW_REVIEW_AGENTS`), or the two classifications drift
    the way this round's finding showed: agentctl sends a dispatch down the
    review-class sandboxing path while this module still treats it as build-class
    (or the reverse, which would grant a non-review-class agent the verdict tool
    and review-only grants)."""
    assert TP._VERDICT_AGENTS == frozenset(agentctl._REVIEW_AGENTS) | frozenset(
        agentctl._SHADOW_REVIEW_AGENTS)
    for name in frozenset(agentctl._REVIEW_AGENTS) | frozenset(agentctl._SHADOW_REVIEW_AGENTS):
        assert agentctl._is_review_agent(name), name
        assert name in TP._VERDICT_AGENTS, name
    # And no crew-prefixed variant disagrees either — the same seam
    # `test_app_prefixed_crews_get_their_role_policy` covers for ENFORCEABLE_AGENTS.
    for prefix in agentctl._internal_app_prefixes():
        for base in agentctl._REVIEW_AGENTS + agentctl._SHADOW_REVIEW_AGENTS:
            name = f"{prefix}{base}"
            assert agentctl._is_review_agent(name) == (
                TP.strip_app_prefix(name) in TP._VERDICT_AGENTS), name
