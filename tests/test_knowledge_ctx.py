"""cwd/worktree → KB-project resolution (bin/knowledge-ctx detect_project).

The risk this guards: a silent mapping miss makes `knowledge-ctx use <profile>`
individuate the WRONG product's KB into a worktree (or abort), so an agent runs
without its context. Phase B (ARCH-099) added `agent-platform` as a mapped
target — these tests pin the map, including the platform repo's self-mapping and
the hyphen-surviving worktree split.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import pathlib
import sqlite3
import re
import shutil
import subprocess
import sys
import tempfile

import inspect
import io
import urllib.error
import urllib.parse
import urllib.request

import pytest

import issue_client

_KX_PATH = pathlib.Path(__file__).resolve().parent.parent / "bin" / "knowledge-ctx"


def test_co_generic_tail_matches_no_project_mapping_code(knowledge_ctx):
    """`bin/co`'s generic tail is zsh and can't import NO_PROJECT_MAPPING, so it
    carries the value as a literal (`_co_kx_status == 42`). Pin the two together:
    if this constant ever changes, `bin/co` must change with it, or the
    "inconclusive falls through to the safe branch" invariant silently breaks —
    the generic tail would stop firing for real unmapped directories (co's
    `_co_kx_status` would never equal the new value) or, worse, start firing on
    the WRONG signal if someone reused the old value elsewhere in knowledge-ctx.
    """
    co_src = (pathlib.Path(knowledge_ctx.__file__).resolve().parent / "co").read_text()
    m = re.search(r"_co_kx_status == (\d+)", co_src)
    assert m, "could not find bin/co's generic-tail exit-code check"
    assert int(m.group(1)) == knowledge_ctx.NO_PROJECT_MAPPING, (
        "bin/co's generic tail matches a literal exit code that no longer equals "
        "knowledge_ctx.NO_PROJECT_MAPPING — update the literal in bin/co."
    )


def test_detect_project_canonical_clone(knowledge_ctx, tmp_path, monkeypatch):
    monkeypatch.setattr(knowledge_ctx, "CONVIVY", tmp_path)
    dp = knowledge_ctx.detect_project
    assert dp(tmp_path / "vivy" / "app") == "vivy"
    assert dp(tmp_path / "constance") == "constance"
    assert dp(tmp_path / "sous-chef" / "x") == "sous-chef"
    # the platform repo maps to its own KB project (ARCH-099)
    assert dp(tmp_path / "agent-platform") == "agent-platform"
    assert dp(tmp_path / "agent-platform" / "bin") == "agent-platform"
    # family + commercial aliases collapse to the parent product's KB
    assert dp(tmp_path / "vivy-commercial") == "vivy"
    assert dp(tmp_path / "constance-commercial") == "constance"
    assert dp(tmp_path / "vivy-ownerrez-sandbox-tools") == "vivy"


def test_detect_project_worktree_shape(knowledge_ctx, tmp_path, monkeypatch):
    monkeypatch.setattr(knowledge_ctx, "CONVIVY", tmp_path)
    dp = knowledge_ctx.detect_project
    # worktrees/<product>.<purpose> → product (prefix before the first dot)
    assert dp(tmp_path / "worktrees" / "constance.overnight" / "app") == "constance"
    assert dp(tmp_path / "worktrees" / "sous-chef.test-xdist") == "sous-chef"
    # agent-platform worktree → agent-platform (split on the FIRST dot, so the
    # hyphenated product name survives; ARCH-099)
    assert dp(tmp_path / "worktrees" / "agent-platform.ci-fix") == "agent-platform"


def test_detect_project_unmapped_exits(knowledge_ctx, tmp_path, monkeypatch):
    monkeypatch.setattr(knowledge_ctx, "CONVIVY", tmp_path)
    dp = knowledge_ctx.detect_project
    # a genuinely-unmapped dir still die()s → exit (agent-platform is now mapped,
    # covered by the canonical-clone case above; ARCH-099). Every one of these
    # carries NO_PROJECT_MAPPING specifically — `bin/co`'s generic tail matches
    # on that exact code as its only affirmative "no project" signal, so a
    # future die() call here reverting to the default code (1, indistinguishable
    # from a crash) would silently break that invariant.
    with pytest.raises(SystemExit) as exc1:
        dp(tmp_path / "totally-unknown-repo")
    assert exc1.value.code == knowledge_ctx.NO_PROJECT_MAPPING
    with pytest.raises(SystemExit) as exc2:
        dp(tmp_path / "worktrees" / "nope.x")
    assert exc2.value.code == knowledge_ctx.NO_PROJECT_MAPPING
    with pytest.raises(SystemExit) as exc3:
        dp(tmp_path.parent / "not-under-convivy-at-all")
    assert exc3.value.code == knowledge_ctx.NO_PROJECT_MAPPING


_EMPTY_KB = pathlib.Path(tempfile.gettempdir()) / "no-such-knowledge-checkout"


def test_every_centrally_indexed_crew_repo_is_dispatchable():
    """An active crew whose context comes from the central KB must have each repo in
    projects.EXPLICIT_PROJECT_BY_NAME or a `projects/<repo>/` directory in the
    knowledge repo, or `agentctl spawn --on <repo>` aborts and
    `knowledge-ctx use` cannot resolve the crew's profile (signals, 2026-09-16)."""
    import projects
    missing = sorted(
        (slug, repo) for slug, unit in projects.load().items()
        if unit.get("lifecycle") == "active" and unit.get("kb") == "central"
        for repo in unit.get("repos", [])
        # an explicit empty root: the table alone, as CI sees it, never a local checkout
        if projects.resolve_project(repo, _EMPTY_KB) is None)
    assert not missing, (
        f"active central-KB crews whose repos projects.resolve_project does not map: "
        f"{missing}. Add each to projects.EXPLICIT_PROJECT_BY_NAME, or create "
        f"projects/<repo>/ in the knowledge repo.")


def test_claude_md_not_delivered_marker_matches_agentctls_regex(agentctl, knowledge_ctx):
    """knowledge-ctx and agentctl are separate executables with no shared module
    (ARCH-023's per-file symlinks; see this file's own header), so the marker
    that carries the not-delivered outcome between them — knowledge-ctx emits
    it, agentctl's `_CLAUDE_MD_NOT_DELIVERED_RE` matches it — is written out
    twice with nothing tying the copies together. Renaming the marker in one
    file and missing the other would leave every other test in this suite
    green while `activate_profile` silently stops detecting a blocked
    `CLAUDE.md` write (reviewer, PR #635 round 4) — the same failure mode
    this PR exists to close, one layer down. Both sides are read from the
    real modules so this test cannot itself become a third stale copy.
    """
    marker = knowledge_ctx._CLAUDE_MD_NOT_DELIVERED_MARKER
    assert marker in agentctl._CLAUDE_MD_NOT_DELIVERED_RE.pattern, (
        f"knowledge_ctx._CLAUDE_MD_NOT_DELIVERED_MARKER ({marker!r}) does not "
        f"appear in agentctl._CLAUDE_MD_NOT_DELIVERED_RE.pattern "
        f"({agentctl._CLAUDE_MD_NOT_DELIVERED_RE.pattern!r}) — agentctl's regex "
        "must be updated to match knowledge-ctx's marker."
    )


# --- P2a: the live-process guard exempts the caller's own session (2026-07-04) ---

def test_live_claude_processes_exempt_pid(knowledge_ctx, tmp_path, monkeypatch):
    target = tmp_path / "repo"
    target.mkdir()

    # Fake ps → one live `claude` (pid 4242); fake lsof → its cwd IS the target
    # (the self-collision shape: an orchestrator dispatching into its own worktree).
    def fake_run(cmd, *a, **k):
        class _R:
            returncode = 0
            stdout = ""
        r = _R()
        if cmd[0] == "ps":
            r.stdout = "4242 claude\n"
        elif cmd[0] == "lsof":
            r.stdout = f"p4242\nn{target}\n"
        return r
    monkeypatch.setattr(knowledge_ctx.subprocess, "run", fake_run)

    # Not exempt → reported as a collision (the guard would abort).
    hits = knowledge_ctx.live_claude_processes_in(target)
    assert [pid for pid, _ in hits] == [4242]

    # Exempt → skipped, so the caller's own session no longer blocks its spawn.
    assert knowledge_ctx.live_claude_processes_in(
        target, exempt_pids=frozenset({4242})) == []


def test_individuate_refuses_a_live_other_session_in_the_primary(knowledge_ctx, tmp_path,
                                                                 monkeypatch):
    """Pins ARCH-080's live-process guard on the exact path this repo needs it
    for: a real force-individuation (`agentctl.activate_profile` → `knowledge-ctx
    use <name> --force`) landing in a repo's PRIMARY checkout.

    convivy-commercial has no origin remote, so it can never get a dedicated
    worktree (unlike coder/reviewer/etc — _DEDICATED_WORKTREE_WORKERS), which
    means every commercial-writer dispatch individuates straight into the live
    workspace. The question this pins is whether the EXISTING guard already
    refuses that, with no new guard needed: a DIFFERENT live `claude` (not the
    invoker's own ancestor chain) in the target must abort the write untouched;
    the SAME pid, exempted (the self-dispatch shape, P2a), must let it through.

    Faked `ps`/`lsof` rather than a real process, per this PR's instruction never
    to run knowledge-ctx against a real workspace.
    """
    target = tmp_path / "convivy-commercial"
    target.mkdir()

    def fake_run(cmd, *a, **k):
        class _R:
            returncode = 0
            stdout = ""
        r = _R()
        if cmd[0] == "ps":
            r.stdout = "4242 claude\n"
        elif cmd[0] == "lsof":
            r.stdout = f"p4242\nn{target}\n"
        return r
    monkeypatch.setattr(knowledge_ctx.subprocess, "run", fake_run)

    kwargs = dict(
        project="company", profile_name="commercial", profile={},
        target_repo=target, rows=[], force=True, force_anyway=False, dry_run=False,
    )

    # A different live session holds it (not exempted) — refused, nothing written.
    rc = knowledge_ctx.individuate(**kwargs, exempt_pids=frozenset())
    assert rc == 2
    assert not (target / ".claude").exists()

    # Only the invoker (its own ancestor chain) holds it — the write proceeds.
    rc = knowledge_ctx.individuate(**kwargs, exempt_pids=frozenset({4242}))
    assert rc == 0
    assert (target / ".claude" / ".active-profile").read_text() == "company/commercial\n"


def test_individuate_exempts_one_live_claude_but_still_refuses_a_second(
        knowledge_ctx, tmp_path, monkeypatch):
    """The rollover fix (issue_01M2FDBJS8GMA16DX4XNBVH2K6) exempts exactly the
    predecessor's own claude pid, never every live claude in the target: a
    SECOND, genuinely different live claude session in the same directory
    must still refuse the write.
    """
    target = tmp_path / "convivy-commercial"
    target.mkdir()

    def fake_run(cmd, *a, **k):
        class _R:
            returncode = 0
            stdout = ""
        r = _R()
        if cmd[0] == "ps":
            r.stdout = "4242 claude\n5555 claude\n"
        elif cmd[0] == "lsof":
            pid = cmd[-1]
            r.stdout = f"p{pid}\nn{target}\n"
        return r
    monkeypatch.setattr(knowledge_ctx.subprocess, "run", fake_run)

    kwargs = dict(
        project="company", profile_name="commercial", profile={},
        target_repo=target, rows=[], force=True, force_anyway=False, dry_run=False,
    )

    # Only 4242 (the predecessor) is exempted — 5555 is a different live
    # session and still refuses the write.
    rc = knowledge_ctx.individuate(**kwargs, exempt_pids=frozenset({4242}))
    assert rc == 2
    assert not (target / ".claude").exists()

    # Both exempted (no other live session) — the write proceeds.
    rc = knowledge_ctx.individuate(**kwargs, exempt_pids=frozenset({4242, 5555}))
    assert rc == 0
    assert (target / ".claude" / ".active-profile").read_text() == "company/commercial\n"


def test_individuate_refuses_a_live_non_ancestor_claude_when_the_plan_is_stale(
        knowledge_ctx, tmp_path, monkeypatch):
    """Pins the ARCH-023 precondition that makes a rollover successor's
    re-individuation refusable (agent-platform issue_01M2F7ZVFYH2WXFC6BX8GCW2ZN):
    a live `claude` in the target that is not this process's ancestor, combined
    with a stale on-disk plan, is refused rather than overwritten.

    `co_rollover.spawn_successor` opens the successor in a sibling tmux window
    in the SAME cwd the predecessor is still running in — the predecessor's
    `claude` pid is not this process's ancestor, and none of `bin/co`'s `-r`
    branches pass `--exempt-pid` (only `agentctl` does, for its own dispatch).
    This test only exercises `knowledge_ctx.individuate()` directly, with a
    faked `ps`/`lsof`; it does not go through `bin/co` or the
    `CO_ROLLOVER_SUCCESSOR` marker path — that is
    `tests/bash/test_co_rollover_successor.sh`'s job.

    This target's `.claude` already exists — modelling what the predecessor
    individuated before handing off — but holds STALE content relative to
    the plan this call computes, the ordinary result of a knowledge-repo
    change landing since the predecessor's own last activation (exactly what
    `_co_pull_knowledge` picks up before this runs). That is enough to make
    individuation a real write, not a no-op, so the live-process guard is
    reached and the predecessor's unexempted pid refuses it.
    """
    target = tmp_path / "constance-commercial"
    claude_dir = target / ".claude"
    claude_dir.mkdir(parents=True)
    claude_dir.joinpath(".active-profile").write_text("company/commercial-stale\n")

    def fake_run(cmd, *a, **k):
        class _R:
            returncode = 0
            stdout = ""
        r = _R()
        if cmd[0] == "ps":
            r.stdout = "4242 claude\n"
        elif cmd[0] == "lsof":
            r.stdout = f"p4242\nn{target}\n"
        return r
    monkeypatch.setattr(knowledge_ctx.subprocess, "run", fake_run)

    rc = knowledge_ctx.individuate(
        project="company", profile_name="commercial", profile={},
        target_repo=target, rows=[], force=True, force_anyway=False, dry_run=False,
        exempt_pids=frozenset(),
    )
    assert rc == 2
    assert claude_dir.joinpath(".active-profile").read_text() == "company/commercial-stale\n"


@pytest.mark.needs_kb
def test_every_mapping_resolves_to_a_real_profile(agentctl, knowledge_ctx):
    """A workspace mapping must name a project that actually has a profile.

    This is the third condition a spawn needs and the only one nothing checked.
    `test_every_centrally_indexed_crew_repo_is_dispatchable` pins that every crew
    repo resolves, and `test_the_project_table_maps_only_to_known_projects` (in
    test_agentctl.py) pins that the table names a real project — but a real
    directory with no `profiles/` in it still aborts
    the spawn, with "profile not found", naming a path rather than the mapping
    that produced it.

    ARCH-438 re-points seven mappings onto projects whose profiles were created in
    the same pass, so this is exactly the pass where the invariant is worth
    pinning. Verified by hand once; a hand check is not a guard.

    LOCAL_AGENT_REPOS are exempt and that is not a loophole: `resolve_agent`
    returns before touching the index for those, and `cmd_spawn` stores
    `<repo>/none` rather than resolving a profile, so they legitimately have none.
    """
    from conftest import knowledge_repo

    projects = knowledge_repo() / "projects"
    missing = []
    import projects as projects_registry
    for repo, project in sorted(projects_registry.EXPLICIT_PROJECT_BY_NAME.items()):
        if repo in agentctl.LOCAL_AGENT_REPOS:
            continue
        if not any((projects / project / "profiles").glob("*.yaml")):
            missing.append((repo, project))
    assert not missing, (
        f"mappings whose project has no profile, so `agentctl spawn --on <repo>` "
        f"aborts: {missing}. Add a profile under "
        f"knowledge/projects/<project>/profiles/, or map the repo elsewhere.")


# --- the EA profile allowlist (2026-09-08) ------------------------------------
# EA is a black box holding the operator's personal correspondence. Mapping it to
# `platform` is what makes its own narrow profile reachable, and the same row
# makes `platform/daytime` — which carries the company reference slice — equally
# reachable. `_PROFILE_ALLOWLIST` is what closes that, and it must fail closed.

def test_ea_may_individuate_only_its_own_profile(knowledge_ctx, tmp_path, monkeypatch):
    """THE OCCURRENCE IS A HARDCODED LITERAL, not a typo. Every crew's `-r` branch
    in bin/co runs `knowledge-ctx use daytime --force`; an EA branch written by
    analogy with its siblings would resolve the wide platform profile, and
    `--force` rmtrees agents/ and skills/ before installing it."""
    monkeypatch.setattr(knowledge_ctx, "CONVIVY", tmp_path)
    check = knowledge_ctx.check_profile_allowed
    product, _ = knowledge_ctx.detect_product(tmp_path / "EA")
    assert product == "EA", "the allowlist keys on the directory name, capital-EA"

    # The permitted one is permitted — without this the test below would pass
    # against a function that refused everything.
    check("EA", "ea-daytime")

    with pytest.raises(SystemExit) as exc:
        check("EA", "daytime")
    # A DISTINCT CODE FROM `NO_PROJECT_MAPPING`, whose own comment forbids
    # reusing it. "no mapping at all" tells `bin/co` to fall back safely;
    # "mapped fine, not allowed that profile" means a fallback is exactly
    # wrong, so the two must stay tellable apart (reviewer, PR #513).
    assert exc.value.code == knowledge_ctx.PROFILE_NOT_ALLOWED
    assert knowledge_ctx.PROFILE_NOT_ALLOWED != knowledge_ctx.NO_PROJECT_MAPPING
    with pytest.raises(SystemExit):
        check("EA", "overnight")


def test_the_allowlist_restricts_the_directory_not_the_project(knowledge_ctx, tmp_path,
                                                               monkeypatch):
    """EA maps to `platform`, and so does substrate. Keying the
    restriction on the resolved PROJECT would forbid `platform/daytime` in
    substrate, where it is substrate's own profile and must keep working."""
    monkeypatch.setattr(knowledge_ctx, "CONVIVY", tmp_path)
    assert knowledge_ctx.detect_project(tmp_path / "EA") == "platform"
    assert knowledge_ctx.detect_project(tmp_path / "substrate") == "platform"

    # Same project, opposite answers — this is the whole point of the split.
    knowledge_ctx.check_profile_allowed("substrate", "daytime")
    with pytest.raises(SystemExit):
        knowledge_ctx.check_profile_allowed("EA", "daytime")


def test_an_unlisted_directory_is_unrestricted(knowledge_ctx):
    """Absent from the table means unrestricted, so this costs nothing anywhere
    else. A guard that refused by default would break every other crew."""
    assert knowledge_ctx.allowed_profiles("vivy") is None
    knowledge_ctx.check_profile_allowed("vivy", "daytime")
    knowledge_ctx.check_profile_allowed("vivy", "anything-at-all")


def test_the_ea_row_and_its_restriction_are_present_together(knowledge_ctx):
    """The row and the restriction are only safe together: the row is what makes
    the wide profile reachable from EA. Whichever is removed, this fails."""
    import projects
    assert projects.EXPLICIT_PROJECT_BY_NAME.get("EA") == "platform", (
        "EA must map to platform or its own profile is unreachable")
    assert knowledge_ctx.allowed_profiles("EA") == {"ea-daytime"}, (
        "the EA row without the allowlist entry reopens platform/daytime to a "
        "crew holding personal correspondence")


def test_the_use_path_actually_calls_the_guard(knowledge_ctx, tmp_path, monkeypatch):
    """THROUGH `main()`, NOT THE HELPER. The four tests above call
    `check_profile_allowed` directly, so all of them would stay green if the call
    site in the `use` path were deleted — which is the defect, since the call
    site is the only thing standing between `co -r` and the wide profile.

    The refusal must land BEFORE `load_profile`, so this needs no index and no
    profile on disk: reaching either means the guard did not fire first."""
    monkeypatch.setattr(knowledge_ctx, "CONVIVY", tmp_path)
    ea = tmp_path / "EA"
    ea.mkdir()
    monkeypatch.chdir(ea)

    monkeypatch.setattr("sys.argv", ["knowledge-ctx", "use", "daytime"])
    with pytest.raises(SystemExit) as exc:
        knowledge_ctx.main()
    assert exc.value.code == knowledge_ctx.PROFILE_NOT_ALLOWED, (
        "`use daytime` from EA must be refused by the guard, not fall through "
        "to load_profile — and with the code that says WHY it was refused")


def test_list_from_ea_does_not_advertise_the_wide_profile(knowledge_ctx, tmp_path,
                                                          monkeypatch, capsys):
    """Listing a profile the directory may not use is an invitation to type it.
    `list` runs the same filter, so the two cannot disagree."""
    monkeypatch.setattr(knowledge_ctx, "CONVIVY", tmp_path)
    ea = tmp_path / "EA"
    ea.mkdir()
    monkeypatch.chdir(ea)
    monkeypatch.setattr(knowledge_ctx, "list_profiles",
                        lambda project: ["daytime", "ea-daytime", "overnight"])

    monkeypatch.setattr("sys.argv", ["knowledge-ctx", "list"])
    assert knowledge_ctx.main() == 0
    out = capsys.readouterr().out
    assert "ea-daytime" in out
    assert "\n  daytime" not in out, "the wide profile must not be offered in EA"
    assert "overnight" not in out


def test_a_dispatch_on_ea_never_reaches_a_profile_at_all(agentctl):
    """EA IS LOCAL-RESOLVED, SO DISPATCH SKIPS INDIVIDUATION ENTIRELY, and an
    earlier revision of this PR did not know that.

    It added a per-repo default so a spawn would ask for `ea-daytime` rather
    than `platform/daytime`. That code was unreachable: `resolve_agent` returns
    `local: True` for every repo in LOCAL_AGENT_REPOS, `_spawn_dispatch` then
    uses the sentinel `<repo>/none` instead of calling `default_profile`, and
    gates `activate_profile` on `not is_local`. So no dispatch on EA resolves a
    profile or consults the allowlist (reviewer, PR #513 round 2).

    This pins the property the removed code misunderstood, so the next reader
    does not re-add it. Reads `_spawn_dispatch` (the function `cmd_spawn`
    delegates to as of the model-lane epic's slice 1,
    issue_01M35KQ8WTW8R3WSKXS81TV2DZ) rather than `cmd_spawn` itself, which is
    now a thin wrapper carrying none of this logic."""
    assert "EA" in agentctl.LOCAL_AGENT_REPOS
    src = inspect.getsource(agentctl._spawn_dispatch)
    assert 'f"{repo}/none" if is_local else default_profile' in src, (
        "_spawn_dispatch must take the sentinel for a local-resolved repo rather "
        "than computing a profile; if this changed, EA dispatch now resolves one "
        "and the allowlist becomes reachable from it")
    assert "if not is_local:\n        claude_md_blocked = activate_profile" in src, (
        "individuation must stay gated on not is_local")


def test_the_default_profile_is_daytime_for_every_repo(agentctl, monkeypatch):
    """No per-repo override exists for THESE repos, and none should be
    re-added for EA — see the test above for why it would be dead code there
    (EA is local-resolved and never reaches `default_profile` at all).

    convivy-commercial is the one repo that DOES override the default (its
    product, `company`, has no `daytime` profile — see
    test_default_profile_resolves_convivy_commercial_to_its_named_profile in
    test_agentctl.py); that repo is a real dispatch target, unlike EA, so the
    override is load-bearing rather than dead code."""
    from conftest import stub_profile_listing
    stub_profile_listing(agentctl, monkeypatch)
    assert agentctl.default_profile("vivy", "reviewer") == "vivy/daytime"
    assert agentctl.default_profile("substrate", "reviewer") == "platform/daytime"
    assert agentctl.default_profile("EA", "reviewer") == "platform/daytime"


# --- the refusal must be the LAST thing a blocked run prints (2026-09-09) ---


def _run_piped(body: str) -> tuple[str, int]:
    """Run `body` against bin/knowledge-ctx with stdout AND stderr on ONE pipe.

    The pipe is the whole point and an in-process test cannot substitute for it.
    Python line-buffers stdout to a terminal and BLOCK-buffers it to a pipe, so
    the ordering defect exists only when the output is captured — which is the
    case every caller that reads this programmatically is in, and the one a
    developer watching a terminal never sees.

    THE PROGRAM GOES IN A FILE, NOT IN `-c`, AND THAT IS NOT A STYLE CHOICE.
    This helper inlines the WHOLE of `bin/knowledge-ctx` into the child program,
    so an `-c` argument grows with that script. Linux caps a SINGLE argument at
    `MAX_ARG_STRLEN`, 131,072 bytes, independently of the much larger total
    `ARG_MAX`; past it `execve` returns `E2BIG` and Python raises
    `OSError: [Errno 7] Argument list too long` from `subprocess`.

    Measured when this bit: the repr'd source was 126,731 bytes on `main` —
    inside the cap by 4,341 bytes, about 3% headroom — and 140,869 on the branch
    that added ~300 lines to the script, which is 9,797 over. So this test was a
    landmine primed to kill the next few KB added to `bin/knowledge-ctx` by
    anyone, and it named the growth rather than the mechanism when it went off.

    **macOS has no equivalent per-argument cap**, so the whole suite passes
    locally and only Linux CI fails — which is why it reached CI green-looking.
    A temp file has no size limit of this kind and changes nothing else: the
    child still runs as its own process with one pipe, which is the behaviour
    under test.
    """
    src = pathlib.Path(_KX_PATH).read_text()
    prog = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_loader('kx', loader=None)\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        # The script finds lib/ from its own path, as every bin/ script does, so
        # the exec'd copy needs the real one (builder_root, company/decisions/0008).
        f"mod.__file__ = {str(_KX_PATH)!r}\n"
        f"exec(compile({src!r}, 'kx', 'exec'), mod.__dict__)\n"
        + body
    )
    with tempfile.TemporaryDirectory() as td:
        runner = pathlib.Path(td) / "run_kx.py"
        runner.write_text(prog)
        p = subprocess.run([sys.executable, str(runner)],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return p.stdout, p.returncode


def test_a_refused_individuation_says_so_last(knowledge_ctx):
    """The defect: a blocked `use` printed its plan to buffered stdout and its
    refusal to unbuffered stderr, so the refusal came out FIRST and
    `plan: N files` landed LAST. Reading the tail of a blocked run showed a
    completed plan and nothing else — a refusal and a success produced the same
    evidence at the end of the output, which is the part a caller reads. Orient
    took a blocked run for a completed one on 2026-09-09 and caught it only by
    checking the individuated file's mtime.

    The exit code was correct throughout and is not what this fixes.
    """
    out, code = _run_piped(
        "print('profile: placard/daytime')\n"
        "print('plan:    32 files')\n"
        "sys.exit(mod.abort('\\nX live Claude Code processes detected - aborting.\\n"
        "NOTHING WAS WRITTEN - the plan above was not applied.\\n', 2))\n"
    )
    assert code == 2
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert "NOTHING WAS WRITTEN" in lines[-1], (
        f"the last line of a refused run must say it refused; got {lines[-1]!r}\n"
        f"full output:\n{out}"
    )
    # And the plan must still be there, above it — the fix is ORDER, not silence.
    assert any("plan:" in ln for ln in lines)
    assert lines.index([ln for ln in lines if "plan:" in ln][0]) < len(lines) - 1


def test_every_return_in_individuate_is_success_or_an_abort(knowledge_ctx):
    """`abort` is only load-bearing if every refusal goes through it. Both
    pre-flights did their own `sys.stderr.write` before this change, and a third
    one added later would reintroduce the defect with NO runtime signal, because
    the exit code stays correct either way.

    Stated as a PARTITION — every return is `return 0` or `return abort(...)`.

    READ BY AST RATHER THAN BY REGEX, because the regex version was too narrow
    twice in two review rounds and each time it read as coverage:

    1. It matched `return <positive integer>`, so it caught `return 9` and missed
       `return code` or `return RC_CONFLICT`. Naming or computing an exit code is
       the ordinary way someone writes a refusal path.
    2. It required a space after `return`, so a bare `return` was invisible — it
       matched nothing and did not trip the emptiness guard either, since the
       other returns kept the list non-empty. `check_profile_allowed` in this same
       file already uses a bare `return` as an early exit, and `main()` propagates
       one to `sys.exit(None)`, which is exit 0. A lazily-written third refusal —
       print to stderr, `return` — would have been success-shaped AND wrongly
       ordered, silently.

    A third shape was then identified and deliberately NOT patched: `if cond:
    return 5` on one line evades any `\\n +return` anchor, and this codebase has
    zero occurrences of it. Fixing narrow patterns one named shape at a time is
    how the first two got through, so the pattern matching goes instead. The AST
    sees every return in the function however it is written, and nested functions
    are excluded structurally rather than by hoping the slice ends in the right
    place.
    """
    tree = ast.parse(pathlib.Path(_KX_PATH).read_text())
    fn = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == "individuate"), None)
    assert fn is not None, "individuate() not found — has it been renamed?"

    # Returns belonging to individuate ITSELF: walk into every node except a
    # nested def, whose returns are that function's business and not this one's.
    def _own_returns(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(child, ast.Return):
                yield child
            yield from _own_returns(child)

    returns = list(_own_returns(fn))
    assert returns, "found no returns in individuate() — the walk is wrong"

    def _describe(node):
        if node.value is None:
            return "<bare return>"
        return ast.unparse(node.value)

    def _ok(node):
        v = node.value
        # `type(v.value) is int` rather than `v.value == 0` alone, because
        # Python's bool subclasses int and `False == 0`. Without it a refusal
        # written `sys.stderr.write(reason); return False` classified as a
        # SUCCESS return and passed — the exact failure class this test exists to
        # catch, going through the check silently. Both reviewers found it
        # independently in round 3 and both called it non-blocking, since
        # individuate() has no boolean returns and every refusal path in this
        # codebase signals with an integer code. Excluded anyway: the
        # coincidence is invisible at the call site, so a future author would get
        # no signal, and that is what makes it different from the shapes with no
        # occurrence that were deliberately left alone.
        if isinstance(v, ast.Constant) and type(v.value) is int and v.value == 0:
            return True
        return (isinstance(v, ast.Call) and isinstance(v.func, ast.Name)
                and v.func.id == "abort")

    bad = [f"line {n.lineno}: return {_describe(n)}" for n in returns if not _ok(n)]
    assert not bad, (
        "every return in individuate() must be `return 0` or `return abort(...)`; "
        f"found {bad}. A refusal that returns without going through abort() prints "
        "its reason before the buffered plan summary and lands success-shaped last."
    )


def test_abort_flushes_stdout_before_writing_stderr(knowledge_ctx):
    """Pins the mechanism rather than the symptom. The flush is the entire fix,
    and it is one line that a future edit could drop while every other assertion
    here still passed on a terminal."""
    src = inspect.getsource(knowledge_ctx.abort)
    flush = src.index("sys.stdout.flush()")
    write = src.index("sys.stderr.write(")
    assert flush < write, "abort() must flush stdout BEFORE writing to stderr"


# --- `.profile-documents.json` (agent-platform issue_01M0Y6NVCECKQMDS74R1KZ1WT4) --
#
# Deck's cockpit lists the documents a checkout's profile brought
# (`session-agent documents`, `lib/agent-platform/session_documents.py`), read
# from this record rather than recomputed. These tests pin what individuate()
# writes into it; the read side is tested against constructed trees in
# `test_session_documents.py`.


def _stub_source(knowledge_root: pathlib.Path, rel: str, body: str = "Body.\n") -> None:
    path = knowledge_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def _served_rows(knowledge_ctx, knowledge_root: pathlib.Path, rows: list[dict]) -> list[dict]:
    """Rows as the Library serves them: each row's frontmatter and body come from
    its stubbed file, read HERE, in the test, as the Library's document would
    supply them. `individuate` itself reads nothing under `knowledge_root`."""
    out = []
    for row in rows:
        text = (knowledge_root / row["source_ref"]).read_text()
        fm, body = knowledge_ctx.split_frontmatter(text)
        out.append({**row, "project": row["id"].split("/")[0], "body": body,
                    "_bundle": {"fm": fm, "body": body.rstrip() + "\n",
                                "body_sha256": hashlib.sha256(body.encode()).hexdigest()}})
    return out


def test_individuate_writes_profile_documents_json_across_every_kind(
        knowledge_ctx, tmp_path, monkeypatch):
    """One `{id, kind, body_sha256}` per resolved row, including a kind
    (`decision`) that is never rendered into `.claude/` — and `.materialized`
    lists the record. The agent's hash is of the RENDERED file (frontmatter
    plus body), the decision's of the body the Library served (frontmatter
    stripped): each is a hash of what this individuation actually resolved for
    that row, not a shared formula."""
    knowledge_root = tmp_path / "knowledge"
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", knowledge_root)
    _stub_source(knowledge_root, "shared/agents/coder.md",
                 "---\ndescription: does the coding\n---\n\nBody.\n")
    _stub_source(knowledge_root, "shared/decisions/0001-thing.md",
                 "---\ntitle: a decision\n---\n\nBody.\n")

    rows = [
        {"id": "shared/agents/coder", "kind": "agent",
         "source_ref": "shared/agents/coder.md", "title": "coder"},
        {"id": "shared/decisions/0001-thing", "kind": "decision",
         "source_ref": "shared/decisions/0001-thing.md", "title": "a decision"},
    ]
    rows = _served_rows(knowledge_ctx, knowledge_root, rows)
    target = tmp_path / "repo"
    target.mkdir()

    rc = knowledge_ctx.individuate(
        project="shared", profile_name="daytime", profile={},
        target_repo=target, rows=rows, force=False, force_anyway=False,
        dry_run=False,
    )
    assert rc == 0

    record = json.loads((target / ".claude" / ".profile-documents.json").read_text())
    assert record["profile"] == "shared/daytime"
    # Decision 0088: the timestamp is written under the new key beside the old.
    assert "materialized_at" in record and "individuated_at" in record
    assert record["individuated_at"] == record["materialized_at"]

    agent_hash = hashlib.sha256(
        (target / ".claude" / "agents" / "coder.md").read_text().encode()).hexdigest()
    decision_hash = hashlib.sha256(b"Body.\n").hexdigest()
    assert record["documents"] == [
        {"id": "shared/agents/coder", "kind": "agent", "body_sha256": agent_hash},
        {"id": "shared/decisions/0001-thing", "kind": "decision",
         "body_sha256": decision_hash},
    ]

    manifest = (target / ".claude" / ".materialized").read_text().splitlines()
    assert ".profile-documents.json" in manifest


def test_individuate_renders_a_library_document_with_no_file_in_the_checkout(
        knowledge_ctx, tmp_path, monkeypatch):
    """A row carries its own body, so its `source_ref` need not exist on disk.
    Before the Library served bodies, a row whose file was missing was skipped
    and left out of the record; now nothing is skipped for want of a file, and
    the record names every row the render brought."""
    knowledge_root = tmp_path / "knowledge"
    knowledge_root.mkdir()
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", knowledge_root)
    body = "Body.\n"
    sha = hashlib.sha256(body.encode()).hexdigest()
    rows = [
        {"id": "shared/agents/coder", "kind": "agent", "project": "shared",
         "source_ref": "shared/agents/coder.md", "title": "coder", "body": body,
         "_bundle": {"fm": {}, "body": body, "body_sha256": sha}},
        {"id": "shared/decisions/gone", "kind": "decision", "project": "shared",
         "source_ref": "shared/decisions/gone.md", "title": "vanished", "body": body,
         "_bundle": {"fm": {}, "body": body, "body_sha256": sha}},
    ]
    target = tmp_path / "repo"
    target.mkdir()

    rc = knowledge_ctx.individuate(
        project="shared", profile_name="daytime", profile={},
        target_repo=target, rows=rows, force=False, force_anyway=False,
        dry_run=False,
    )
    assert rc == 0

    record = json.loads((target / ".claude" / ".profile-documents.json").read_text())
    agent_hash = hashlib.sha256(
        (target / ".claude" / "agents" / "coder.md").read_text().encode()).hexdigest()
    assert record["documents"] == [
        {"id": "shared/agents/coder", "kind": "agent", "body_sha256": agent_hash},
        {"id": "shared/decisions/gone", "kind": "decision",
         "body_sha256": hashlib.sha256(body.encode()).hexdigest()}]


def test_individuate_never_decodes_a_non_utf8_checkout_file_for_a_library_row(
        knowledge_ctx, tmp_path, monkeypatch):
    """A stray non-UTF-8 byte in the checkout copy of a document must not reach
    the render: the row's body is the Library's, and that is what is hashed. The
    checkout file is never opened (reviewer, PR #740 round 1, which this replaces
    with the Library body as the only source)."""
    knowledge_root = tmp_path / "knowledge"
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", knowledge_root)
    path = knowledge_root / "shared/decisions/0001-thing.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"Memory pressure \xff\xfereaper is off.\n")
    body = "Memory pressure reaper is off.\n"
    raw = body.encode()
    raw_sha = hashlib.sha256(raw).hexdigest()

    rows = [{"id": "shared/decisions/0001-thing", "kind": "decision", "project": "shared",
             "source_ref": "shared/decisions/0001-thing.md", "title": "a decision",
             "body": body, "_bundle": {"fm": {}, "body": body, "body_sha256": raw_sha}}]
    target = tmp_path / "repo"
    target.mkdir()

    rc = knowledge_ctx.individuate(
        project="shared", profile_name="daytime", profile={},
        target_repo=target, rows=rows, force=False, force_anyway=False,
        dry_run=False,
    )
    assert rc == 0

    record = json.loads((target / ".claude" / ".profile-documents.json").read_text())
    assert record["documents"] == [
        {"id": "shared/decisions/0001-thing", "kind": "decision",
         "body_sha256": hashlib.sha256(raw).hexdigest()}]


def test_individuate_never_opens_a_fifo_at_a_library_rows_checkout_path(knowledge_ctx, tmp_path):
    """A FIFO dropped at a row's `source_ref` must not hang `individuate()`: the
    render never opens the checkout for a Library row, so the FIFO is never
    touched and the row hashes its served body. Run in a subprocess with a hard
    stdlib timeout: a regression here would otherwise hang the whole test run
    (mirrors `test_frontmatter_from_disk_does_not_block_on_a_fifo` above)."""
    knowledge_root = tmp_path / "knowledge"
    source_rel = "shared/decisions/0001-thing.md"
    source_path = knowledge_root / source_rel
    source_path.parent.mkdir(parents=True)
    os.mkfifo(source_path)
    target = tmp_path / "repo"
    target.mkdir()
    body = "Body.\n"
    sha = hashlib.sha256(body.encode()).hexdigest()

    kx_path = pathlib.Path(knowledge_ctx.__file__).resolve()
    script = (
        "import sys, importlib.util, importlib.machinery, pathlib\n"
        f"loader = importlib.machinery.SourceFileLoader('kx', {str(kx_path)!r})\n"
        "spec = importlib.util.spec_from_loader('kx', loader)\n"
        "kx = importlib.util.module_from_spec(spec)\n"
        "loader.exec_module(kx)\n"
        f"kx.KNOWLEDGE = pathlib.Path({str(knowledge_root)!r})\n"
        "rows = [{'id': 'shared/decisions/0001-thing', 'kind': 'decision', "
        f"'source_ref': {source_rel!r}, 'title': 'a decision', 'project': 'shared', "
        f"'body': {body!r}, '_bundle': {{'fm': {{}}, 'body': {body!r}, "
        f"'body_sha256': {sha!r}}}}}]\n"
        "rc = kx.individuate(project='shared', profile_name='daytime', profile={}, "
        f"target_repo=pathlib.Path({str(target)!r}), rows=rows, force=False, "
        "force_anyway=False, dry_run=False)\n"
        "print('DONE', rc)\n"
    )
    proc = subprocess.run([sys.executable, "-c", script],
                          capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, proc.stderr
    assert "DONE 0" in proc.stdout

    record = json.loads((target / ".claude" / ".profile-documents.json").read_text())
    assert record["documents"] == [
        {"id": "shared/decisions/0001-thing", "kind": "decision",
         "body_sha256": hashlib.sha256(body.encode()).hexdigest()}]


def test_profile_documents_match_ignores_only_the_timestamp(knowledge_ctx):
    """The comparison `plan_is_current` uses: same `profile` and `documents`
    but a different `materialized_at` still matches, since that field is the
    wall-clock time of the last real write and differs on every call by
    design. A real difference in `documents` must still not match."""
    a = json.dumps({"profile": "shared/daytime", "materialized_at": "T1",
                    "documents": [{"id": "x", "kind": "agent"}]})
    b = json.dumps({"profile": "shared/daytime", "materialized_at": "T2",
                    "documents": [{"id": "x", "kind": "agent"}]})
    assert knowledge_ctx._profile_documents_match(a, b) is True

    c = json.dumps({"profile": "shared/daytime", "materialized_at": "T2",
                    "documents": [{"id": "y", "kind": "agent"}]})
    assert knowledge_ctx._profile_documents_match(a, c) is False
    assert knowledge_ctx._profile_documents_match(a, "{not json") is False


def test_profile_documents_match_treats_a_non_object_as_a_mismatch(knowledge_ctx):
    """Valid JSON that isn't an object — `null`, `[]`, a bare string — must
    read as a mismatch like any other malformed input, not raise
    `AttributeError` out of the `.get()` calls; a docstring claiming
    fail-closed coverage for "malformed JSON on either side" must hold for
    this case too, on either argument."""
    valid = json.dumps({"profile": "shared/daytime", "materialized_at": "T1",
                        "documents": [{"id": "x", "kind": "agent"}]})
    for non_object in ("null", "[]", '"x"'):
        assert knowledge_ctx._profile_documents_match(non_object, valid) is False
        assert knowledge_ctx._profile_documents_match(valid, non_object) is False


def test_plan_is_current_refuses_a_fifo_target_without_blocking(
        knowledge_ctx, tmp_path):
    """Security review, PR #740 round 5: `plan_is_current`'s read decides
    whether `individuate()` ever signals `.profile-documents.json` current
    on the dedicated pipe, and every `target` it reads lives under
    `agents_dir`/`skills_dir`/`claude_dir` — exactly where a prior,
    compromised dispatch into a reused worktree could plant a FIFO.
    `open(2)` on a FIFO with no writer blocks forever; `_guarded_existing_
    text` goes through `_guarded_read_bytes`'s `O_NONBLOCK` guard instead of
    a plain `read_text()` (the same guard `test_individuate_does_not_block_
    on_a_fifo_other_kind_source` already proves non-blocking at the syscall
    level, so this test calls `plan_is_current` directly rather than via a
    subprocess+timeout of its own)."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    skills_dir = tmp_path / "skills"
    target = agents_dir / "coder.md"
    os.mkfifo(target)
    plan = [(target, "---\ndescription: does the coding\n---\n\nBody.\n")]

    assert knowledge_ctx.plan_is_current(plan, agents_dir, skills_dir, force=False) is False


def test_reused_individuation_stays_a_true_no_op_despite_a_fresh_timestamp(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """`.profile-documents.json`'s `materialized_at` changes on every call by
    design, so `plan_is_current` must ignore it — otherwise EVERY
    re-individuation of an unchanged profile would look like a real change
    and trip the ARCH-080 live-process guard, which is exactly the deadlock
    `agentctl`'s kb-sync-verifier self-dispatch relies on this no-op path to
    avoid (see `plan_is_current`'s docstring)."""
    knowledge_root = tmp_path / "knowledge"
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", knowledge_root)
    _stub_source(knowledge_root, "shared/agents/coder.md",
                 "---\ndescription: does the coding\n---\n\nBody.\n")
    rows = [{"id": "shared/agents/coder", "kind": "agent",
             "source_ref": "shared/agents/coder.md", "title": "coder"}]
    rows = _served_rows(knowledge_ctx, knowledge_root, rows)
    target = tmp_path / "repo"
    target.mkdir()
    kwargs = dict(project="shared", profile_name="daytime", profile={},
                  target_repo=target, rows=rows, force=False,
                  force_anyway=False, dry_run=False)

    rc1 = knowledge_ctx.individuate(**kwargs)
    assert rc1 == 0
    record_path = target / ".claude" / ".profile-documents.json"
    first_written = record_path.read_text()

    capsys.readouterr()
    rc2 = knowledge_ctx.individuate(**kwargs)
    assert rc2 == 0
    assert "already current" in capsys.readouterr().out
    assert record_path.read_text() == first_written, (
        "a true no-op must not rewrite the record — its timestamp would "
        "otherwise move on every call that changes nothing")


def _individuate_with_signal(knowledge_ctx, **kwargs) -> tuple[int, bool]:
    """Call `individuate()` with a fresh pipe wired up as
    `--profile-documents-current-fd` would be by `agentctl`, and return
    `(returncode, got_current_signal)`. Drains and closes the read end itself
    (mirroring `agentctl.activate_profile`), so each call gets an independent
    pipe rather than reusing fds across calls."""
    read_fd, write_fd = os.pipe()
    try:
        rc = knowledge_ctx.individuate(profile_documents_current_fd=write_fd, **kwargs)
    finally:
        os.close(write_fd)
    try:
        signal = b""
        while True:
            chunk = os.read(read_fd, 4096)
            if not chunk:
                break
            signal += chunk
    finally:
        os.close(read_fd)
    return rc, signal == knowledge_ctx._PROFILE_DOCUMENTS_CURRENT_PAYLOAD


def test_profile_documents_current_marker_on_write_noop_and_dry_run(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """Security review, PR #740 round 4: `agentctl`'s document-set capture
    must key on whether `.profile-documents.json` is CURRENT for this call,
    not on "did a render run" — `--dry-run` runs but returns before writing
    or checking anything, so it must never signal current the way a real
    write or a verified no-op both do. Round 5: the signal travels on a
    dedicated pipe fd, not a stderr marker, so these assertions read the pipe
    rather than captured output."""
    knowledge_root = tmp_path / "knowledge"
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", knowledge_root)
    _stub_source(knowledge_root, "shared/agents/coder.md",
                 "---\ndescription: does the coding\n---\n\nBody.\n")
    rows = [{"id": "shared/agents/coder", "kind": "agent",
             "source_ref": "shared/agents/coder.md", "title": "coder"}]
    rows = _served_rows(knowledge_ctx, knowledge_root, rows)
    target = tmp_path / "repo"
    target.mkdir()
    kwargs = dict(project="shared", profile_name="daytime", profile={},
                  target_repo=target, rows=rows, force=False, force_anyway=False)

    rc, current = _individuate_with_signal(knowledge_ctx, dry_run=True, **kwargs)
    assert rc == 0
    assert not current, (
        "a dry run must never signal .profile-documents.json current — "
        "it never writes or checks it")
    assert not (target / ".claude" / ".profile-documents.json").exists()

    rc, current = _individuate_with_signal(knowledge_ctx, dry_run=False, **kwargs)
    assert rc == 0
    assert current, "a real write must signal current"

    rc, current = _individuate_with_signal(knowledge_ctx, dry_run=False, **kwargs)
    assert rc == 0
    assert current, (
        "the no-op short-circuit proved the on-disk file matches this call's "
        "plan byte-for-byte, so it is current too")


# --- orphans: a rendered file with no source document anywhere ------------
#
# issue_01M2HT8TM0M85WCQ594ADPMEFT: `start-overnight`'s source
# (projects/platform/skills/start-overnight.md) was deleted from the KB in
# convivy/knowledge@0639498 (2026-09-14 13:38 local), but the rendered
# `.claude/skills/start-overnight/SKILL.md` it had produced was still sitting,
# untouched and unreported, in 13 separate workspaces the next day — because
# no workspace had re-individuated since. The investigation ruled out a second
# copy source and a broken rmtree (both a temp-dir `force=True` repro and a
# direct read of every live workspace's `.claude/skills` confirmed there is
# exactly one code path that writes these files, and it removes a stray
# correctly when it runs); the finding was that nothing had run it since the
# deletion, in ANY workspace, force or not — see the PR body for the full
# evidence chain. These tests pin the fix: an orphan is now reported by name
# and removed the next time anything DOES run, in either mode, rather than
# only on the next `--force`.


def test_all_agent_and_skill_ids_reads_the_full_corpus_any_status(
        knowledge_ctx, tmp_path, monkeypatch):
    """Unlike `collect_matches`, this must NOT be scoped to one profile's
    `include:` rules — `_find_orphans` uses it to tell "excluded by this
    profile" (still correctable) from "gone from the whole corpus"
    (unattributable), so it has to see everything, including a `decision` row
    it will simply ignore (only agent/skill kinds matter to a render). Read
    from the Library's `/reference/documents` listing (CQ·371,
    issue_01M43AE2QD6H3JZMKM2RR2NK2W), never a local index: this must still
    answer on a machine with no `~/.knowledge-index/index.db` at all."""
    _Library(knowledge_ctx, monkeypatch, tmp_path, docs=[
        _bdoc("shared/agents/coder", "agent", ""),
        _bdoc("constance/agents/coder", "agent", ""),
        _bdoc("shared/skills/triage-corrections-queue", "skill", ""),
        _bdoc("shared/decisions/0001-thing", "decision", ""),
    ])
    result = knowledge_ctx.all_agent_and_skill_ids()
    assert result["agent"] == {"shared/agents/coder", "constance/agents/coder"}
    assert result["skill"] == {"shared/skills/triage-corrections-queue"}


def test_all_agent_and_skill_ids_pages_to_the_end(knowledge_ctx, tmp_path, monkeypatch):
    """A corpus larger than one page must still be read whole, the same way
    `bin/agentctl`'s `_library_agent` pages `/reference/documents`."""
    monkeypatch.setattr(knowledge_ctx, "_LIBRARY_DOCS_PAGE", 1)
    lib = _Library(knowledge_ctx, monkeypatch, tmp_path, docs=[
        _bdoc(f"shared/agents/a{i}", "agent", "") for i in range(3)])
    result = knowledge_ctx.all_agent_and_skill_ids()
    assert result["agent"] == {"shared/agents/a0", "shared/agents/a1", "shared/agents/a2"}
    # 3 agent pages (one row each) + 1 empty skill page, at page size 1.
    assert sum(1 for c in lib.calls if c[0] == "/reference/documents") == 4


def test_an_unreachable_library_refuses_all_agent_and_skill_ids_by_name(
        knowledge_ctx, tmp_path, monkeypatch):
    _Library(knowledge_ctx, monkeypatch, tmp_path)

    def down(req):
        raise urllib.error.URLError(ConnectionRefusedError())
    monkeypatch.setattr(knowledge_ctx, "_library_open", down)
    with pytest.raises(SystemExit) as exc:
        knowledge_ctx.all_agent_and_skill_ids()
    assert exc.value.code == knowledge_ctx.LIBRARY_REFUSED


# --- round 2: an orphan is only removed when it clears a per-file gate ----
#
# Round 1 (above) made the render delete a file with no source document
# ANYWHERE in the index, unconditionally. Both round-1 reviews confirmed the
# mechanism but found it was not limited to files the render can PROVE it
# owns: an empty-both-sets index (not-yet-populated, mid-reindex, misrooted,
# or a test KB_INDEX_DB) makes every rendered file look orphaned; a directory
# symlink under `.claude/skills/<name>` makes the glob traverse outside the
# workspace; and a git-tracked file with no KB doc (e.g. a repo-native agent
# definition that was never authored as a KB document) would be unlinked on
# the next render. These tests pin the fix: a candidate is REMOVED only when
# it is listed in the previous `.materialized` manifest, is a plain
# symlink-free file contained under `agents_dir`/`skills_dir`, is not
# git-tracked in the target repo, and the index itself is trustworthy (both
# known sets non-empty, DB_PATH is the configured default) — otherwise it is
# reported ("orphan, not removed: <reason>") and left alone, in both force
# and non-force mode.


def _make_trusted(knowledge_ctx, monkeypatch) -> None:
    """Make the module's index-trust check pass regardless of whatever
    KB_INDEX_DB this pytest run happens to be launched under — the gate for
    this PR deliberately points KB_INDEX_DB at a scratch path for the whole
    suite, which would otherwise make every "trusted index" test below look
    like an override."""
    monkeypatch.setattr(knowledge_ctx, "_DEFAULT_DB_PATH", knowledge_ctx.DB_PATH)


def _make_disk_trusted(knowledge_root: pathlib.Path) -> None:
    """An existing, readable (if empty) `projects/` tree under `knowledge_root`
    — the disk-side half of round 3's trust gate (`_disk_trust_problem`).
    Every orphan test below that expects `_find_orphans` to actually run
    (whether the outcome is removal or blocking) needs this: absence from the
    index alone is no longer enough to trust a candidate as gone everywhere,
    and a `knowledge_root` this helper never touches at all (as every one of
    these tests did before round 3) reads as "the knowledge checkout could
    not be read", which skips orphan detection outright rather than running
    it — see test_a_missing_knowledge_root_on_disk_refuses_deletion_and_says_why
    for that refusal pinned on its own."""
    (knowledge_root / "projects").mkdir(parents=True, exist_ok=True)


def _git_init(path: pathlib.Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"],
                   cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)


def _git_add_commit(repo: pathlib.Path, rel: str) -> None:
    subprocess.run(["git", "add", rel], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add " + rel], cwd=repo, check=True)


def _write_manifest(knowledge_ctx, claude_dir: pathlib.Path, entries: list[str]) -> None:
    """The `.materialized` manifest a PREVIOUS run would have left — read by
    `_read_previous_manifest` before this run's own plan (and its own fresh
    copy of this same file) is computed."""
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / knowledge_ctx._MANIFEST_NAME).write_text(
        "\n".join(sorted(entries)) + "\n")


# A populated, trustworthy known_ids that never matches the orphan slugs these
# tests plant, so the trust gate passes without accidentally un-orphaning the
# very file under test.
_TRUSTED_KNOWN_IDS = {"agent": {"shared/agents/other"}, "skill": {"shared/skills/other"}}


def test_individuate_force_deletes_and_names_an_orphan_with_no_source_anywhere(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """The literal repro from the issue, now with the round-2 preconditions
    (a git repo, both orphans listed in the previous manifest, untracked, and
    a trustworthy non-empty index): both must be gone from disk and named on
    stderr — not just silently swept up by `--force`'s blanket rmtree, which
    is what `bin/knowledge-ctx` did before round 1 (confirmed by a `force=True`
    repro against a stray file: it vanished, but nothing on stderr ever said
    so, which is the "left behind silently" half of the bug the epic calls out
    even where the file WAS actually removed). `--force` no longer rmtrees
    agents_dir/skills_dir wholesale — `_clear_dir_preserving` does the same
    clear while still able to protect a BLOCKED orphan, exercised by the
    force-mode symlink/git-tracked tests below."""
    _make_trusted(knowledge_ctx, monkeypatch)
    knowledge_root = tmp_path / "knowledge"
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", knowledge_root)
    _make_disk_trusted(knowledge_root)
    target = tmp_path / "repo"
    target.mkdir()
    _git_init(target)

    orphan_skill = target / ".claude" / "skills" / "start-overnight" / "SKILL.md"
    orphan_skill.parent.mkdir(parents=True)
    orphan_skill.write_text("stale rule nobody can correct\n")
    orphan_agent = target / ".claude" / "agents" / "ghost-agent.md"
    orphan_agent.parent.mkdir(parents=True)
    orphan_agent.write_text("---\nname: ghost-agent\n---\n\nold.\n")
    _write_manifest(knowledge_ctx, target / ".claude", [
        "skills/start-overnight/SKILL.md", "agents/ghost-agent.md",
    ])

    rc = knowledge_ctx.individuate(
        project="constance", profile_name="commercial", profile={},
        target_repo=target, rows=[], force=True, force_anyway=False,
        dry_run=False, known_ids=_TRUSTED_KNOWN_IDS,
    )
    assert rc == 0
    assert not orphan_skill.exists()
    assert not orphan_skill.parent.exists(), "the emptied skill directory must go too"
    assert not orphan_agent.exists()

    stderr = capsys.readouterr().err
    assert str(orphan_skill) in stderr, "the orphan skill must be named on stderr"
    assert str(orphan_agent) in stderr, "the orphan agent must be named on stderr"


def test_individuate_non_force_blocks_an_orphan_absent_from_any_previous_manifest(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """`bin/co`'s own error hints (lines around "Activate the commercial
    profile first:") tell an operator to type `knowledge-ctx use <profile>`
    BY HAND — no `--force` — which is exactly the first-ever individuation
    of a workspace, before any `.materialized` manifest exists. Round 1 made
    this path unconditionally delete a stray file it found there; round 2's
    "if no previous manifest exists, delete nothing" means the very same
    setup must now BLOCK it instead — reported, not removed. This is also
    the one orphan scenario non-force mode can reach at all: once a real
    `.materialized` exists, listing an orphan `use` should delete makes that
    file's fresh content differ from what's on disk, which `find_conflicts`
    treats as an ordinary conflict requiring `--force` regardless of orphans
    — see the force-mode tests below for every other per-file gate."""
    _make_trusted(knowledge_ctx, monkeypatch)
    knowledge_root = tmp_path / "knowledge"
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", knowledge_root)
    _make_disk_trusted(knowledge_root)
    target = tmp_path / "repo"
    target.mkdir()
    _git_init(target)
    orphan = target / ".claude" / "skills" / "start-overnight" / "SKILL.md"
    orphan.parent.mkdir(parents=True)
    orphan.write_text("stale rule nobody can correct\n")
    # deliberately no `.materialized` written — first-ever individuation

    rc = knowledge_ctx.individuate(
        project="constance", profile_name="commercial", profile={},
        target_repo=target, rows=[], force=False, force_anyway=False,
        dry_run=False, known_ids=_TRUSTED_KNOWN_IDS,
    )
    assert rc == 0
    out = capsys.readouterr()
    assert orphan.exists()
    assert str(orphan) in out.err
    assert "not listed in the previous individuation manifest" in out.err


def test_individuate_leaves_alone_a_file_whose_document_still_exists_elsewhere(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """The other half of the attribution check: a file this PROFILE'S rules
    simply exclude, whose document is still live somewhere in the corpus, is
    NOT an orphan candidate at all — it's correctable through the KB, so it
    must be left for `--force`'s ordinary clear+rewrite rather than examined
    against the per-file gate (it never even reaches `_consider`'s manifest/
    containment/git checks, since `has_document` short-circuits first)."""
    _make_trusted(knowledge_ctx, monkeypatch)
    knowledge_root = tmp_path / "knowledge"
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", knowledge_root)
    _make_disk_trusted(knowledge_root)
    target = tmp_path / "repo"
    target.mkdir()
    still_live = target / ".claude" / "skills" / "triage-corrections-queue" / "SKILL.md"
    still_live.parent.mkdir(parents=True)
    still_live.write_text("this profile doesn't include it, but it still exists\n")

    known_ids = {"agent": {"shared/agents/other"},
                "skill": {"shared/skills/triage-corrections-queue"}}
    rc = knowledge_ctx.individuate(
        project="constance", profile_name="commercial", profile={},
        target_repo=target, rows=[], force=False, force_anyway=False,
        dry_run=False, known_ids=known_ids,
    )
    assert rc == 0
    assert still_live.exists()
    assert str(still_live) not in capsys.readouterr().err


def test_individuate_skips_orphan_detection_when_known_ids_is_none(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """`known_ids=None` (the default) is what every pre-existing test in this
    file relies on implicitly — individuate() must not go looking for an
    index that was never supplied. Pins that default explicitly rather than
    leaving it to accident."""
    knowledge_root = tmp_path / "knowledge"
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", knowledge_root)
    target = tmp_path / "repo"
    target.mkdir()
    stray = target / ".claude" / "skills" / "start-overnight" / "SKILL.md"
    stray.parent.mkdir(parents=True)
    stray.write_text("untouched without known_ids\n")

    rc = knowledge_ctx.individuate(
        project="constance", profile_name="commercial", profile={},
        target_repo=target, rows=[], force=False, force_anyway=False,
        dry_run=False,
    )
    assert rc == 0
    assert stray.exists()
    # A clean render emits no stderr at all: the current-individuation signal
    # travels on a dedicated pipe fd since round 5, not a stderr marker, so
    # nothing remains on stderr for this test to rule the orphan-detection
    # warnings out against except silence itself.
    assert capsys.readouterr().err == ""


def test_an_empty_index_refuses_all_deletion_and_says_why(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """An index whose docs table has zero agent and zero skill rows —
    not-yet-populated, mid-reindex, misrooted, or a test KB_INDEX_DB — is
    indistinguishable, from here, from every rendered file having lost its
    document. Reproduced against the round-1 code: an all-empty `known_ids`
    there deletes every candidate; here it must delete nothing and say why."""
    _make_trusted(knowledge_ctx, monkeypatch)
    knowledge_root = tmp_path / "knowledge"
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", knowledge_root)
    target = tmp_path / "repo"
    target.mkdir()
    orphan = target / ".claude" / "agents" / "ghost.md"
    orphan.parent.mkdir(parents=True)
    orphan.write_text("---\nname: ghost\n---\n\nold.\n")
    # Stays non-force and deliberately does NOT list `orphan` in the planted
    # manifest — matching the fresh (rows=[]) plan's own manifest content
    # exactly (see test_an_orphan_absent_from_the_previous_manifest_survives
    # for why that's what keeps `find_conflicts` from tripping on an
    # unrelated divergence). `--force`'s blanket clear-and-rewrite is an
    # orthogonal, pre-existing behavior this test isn't about: force always
    # removes anything not part of the current plan, orphan or not, and an
    # untrustworthy index answers `rows` too, so what force would do with a
    # genuinely empty `rows` is out of scope here — this test is only about
    # whether the ORPHAN mechanism itself deletes on an untrustworthy read.
    _write_manifest(knowledge_ctx, target / ".claude",
                    [".active-profile", ".materialized", ".profile-documents.json"])

    rc = knowledge_ctx.individuate(
        project="constance", profile_name="commercial", profile={},
        target_repo=target, rows=[], force=False, force_anyway=False,
        dry_run=False, known_ids={"agent": set(), "skill": set()},
    )
    assert rc == 0
    assert orphan.exists()
    err = capsys.readouterr().err
    assert "orphan detection skipped" in err
    assert "known-agent set is empty" in err
    assert str(orphan) not in err, "nothing was even examined per-file"


def test_force_mode_leaves_a_symlinked_skill_and_its_target_alone(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """`skills_dir.glob('*/SKILL.md')` traverses a directory symlink without
    complaint, and `_clear_dir_preserving` must unlink only the symlinks it
    decides to remove, never recurse through one it decides to preserve — so
    a symlink planted at `.claude/skills/<name>` pointing outside the
    workspace must survive force's clear along with the file it leads to.
    Reproduced: without the containment check, the outside file is deleted."""
    _make_trusted(knowledge_ctx, monkeypatch)
    knowledge_root = tmp_path / "knowledge"
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", knowledge_root)
    _make_disk_trusted(knowledge_root)
    target = tmp_path / "repo"
    target.mkdir()
    _git_init(target)
    skills_dir = target / ".claude" / "skills"
    skills_dir.mkdir(parents=True)

    outside = tmp_path / "outside-skill"
    outside.mkdir()
    (outside / "SKILL.md").write_text("do not touch\n")
    link = skills_dir / "evil-link"
    link.symlink_to(outside, target_is_directory=True)

    _write_manifest(knowledge_ctx, target / ".claude", ["skills/evil-link/SKILL.md"])

    rc = knowledge_ctx.individuate(
        project="constance", profile_name="commercial", profile={},
        target_repo=target, rows=[], force=True, force_anyway=False,
        dry_run=False, known_ids=_TRUSTED_KNOWN_IDS,
    )
    assert rc == 0
    assert link.is_symlink(), "the blocked symlink itself must survive force's clear"
    assert (outside / "SKILL.md").read_text() == "do not touch\n"


def test_a_git_tracked_orphan_survives_and_is_reported(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """A rendered agent/skill file with no KB document can still be a real,
    git-tracked file in the target repo (a repo-native definition that was
    never authored as a KB document, or a file the operator added by hand) —
    the render must never delete it, even though "no document anywhere"
    would otherwise call it an orphan."""
    _make_trusted(knowledge_ctx, monkeypatch)
    knowledge_root = tmp_path / "knowledge"
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", knowledge_root)
    _make_disk_trusted(knowledge_root)
    target = tmp_path / "repo"
    target.mkdir()
    _git_init(target)
    agent = target / ".claude" / "agents" / "tracked-agent.md"
    agent.parent.mkdir(parents=True)
    agent.write_text("---\nname: tracked-agent\n---\n\nkeep.\n")
    _git_add_commit(target, ".claude/agents/tracked-agent.md")
    _write_manifest(knowledge_ctx, target / ".claude", ["agents/tracked-agent.md"])

    # force=True — see test_individuate_non_force_blocks_an_orphan_absent_
    # from_any_previous_manifest for why a manifest listing an orphan the
    # fresh plan doesn't own always conflicts in non-force mode regardless.
    rc = knowledge_ctx.individuate(
        project="constance", profile_name="commercial", profile={},
        target_repo=target, rows=[], force=True, force_anyway=False,
        dry_run=False, known_ids=_TRUSTED_KNOWN_IDS,
    )
    assert rc == 0
    assert agent.exists()
    err = capsys.readouterr().err
    assert str(agent) in err
    assert "tracked by git" in err


def test_an_orphan_absent_from_the_previous_manifest_survives(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """A rendered file this workspace's LAST individuation never produced
    (the manifest exists but doesn't list it) is left alone — the manifest
    check exists precisely so a render only ever considers deleting something
    it can prove it once wrote itself.

    Stays non-force deliberately: the planted manifest lists exactly the
    fixed set `rows=[]` would itself produce (`.active-profile`,
    `.materialized`, `.profile-documents.json` — nothing else, since this
    orphan candidate is never part of it either way), so it matches the
    fresh plan byte for byte and `find_conflicts` has nothing to flag. This
    is the one shape that lets a manifest precondition and non-force mode
    coexist; every other per-file-gate test below needs `--force`, since a
    manifest that DOES list the orphan under test necessarily diverges from
    the fresh plan and would conflict for an unrelated reason first."""
    _make_trusted(knowledge_ctx, monkeypatch)
    knowledge_root = tmp_path / "knowledge"
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", knowledge_root)
    _make_disk_trusted(knowledge_root)
    target = tmp_path / "repo"
    target.mkdir()
    _git_init(target)
    orphan = target / ".claude" / "agents" / "ghost.md"
    orphan.parent.mkdir(parents=True)
    orphan.write_text("---\nname: ghost\n---\n\nold.\n")
    _write_manifest(knowledge_ctx, target / ".claude",
                    [".active-profile", ".materialized", ".profile-documents.json"])

    rc = knowledge_ctx.individuate(
        project="constance", profile_name="commercial", profile={},
        target_repo=target, rows=[], force=False, force_anyway=False,
        dry_run=False, known_ids=_TRUSTED_KNOWN_IDS,
    )
    assert rc == 0
    assert orphan.exists()
    err = capsys.readouterr().err
    assert str(orphan) in err
    assert "not listed in the previous individuation manifest" in err


def test_an_orphan_that_clears_every_gate_is_removed_and_named(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """The positive case: listed in the previous manifest, a plain untracked
    file fully contained under skills_dir, with a trustworthy populated
    index — every condition holds, so it IS removed, same as round 1's
    unconditional behaviour for this one shape. force=True, since a manifest
    listing the orphan necessarily diverges from the fresh (orphan-excluding)
    plan's own manifest content, which non-force mode treats as an ordinary
    `find_conflicts` conflict regardless of orphans."""
    _make_trusted(knowledge_ctx, monkeypatch)
    knowledge_root = tmp_path / "knowledge"
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", knowledge_root)
    _make_disk_trusted(knowledge_root)
    target = tmp_path / "repo"
    target.mkdir()
    _git_init(target)
    orphan = target / ".claude" / "skills" / "start-overnight" / "SKILL.md"
    orphan.parent.mkdir(parents=True)
    orphan.write_text("stale rule nobody can correct\n")
    _write_manifest(knowledge_ctx, target / ".claude", ["skills/start-overnight/SKILL.md"])
    # deliberately not `git add`ed — untracked in a real git repo

    rc = knowledge_ctx.individuate(
        project="constance", profile_name="commercial", profile={},
        target_repo=target, rows=[], force=True, force_anyway=False,
        dry_run=False, known_ids=_TRUSTED_KNOWN_IDS,
    )
    assert rc == 0
    assert not orphan.exists()
    assert not orphan.parent.exists()
    err = capsys.readouterr().err
    assert str(orphan) in err
    assert "orphan (no source document anywhere)" in err


def test_a_dry_run_with_orphan_detection_escapes_a_newline_in_a_planted_filename(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """Security review, PR #740 round 5: round 4's capture gate matched a
    stable marker out of knowledge-ctx's combined stdout+stderr. That stream
    also carries text derived from the worktree — the blocked-orphan echo
    runs before the `--dry-run` early return and prints a candidate's path
    VERBATIM — and a Unix filename may contain a literal newline, which a
    prior, compromised dispatch into a reused worktree could plant. A
    filename shaped `<junk>\\n<the old marker text>\\n<junk>.md` split that
    echo into an extra "line" matching the marker exactly, forging it on a
    `--dry-run` call that never wrote or checked anything.

    `known_ids` must be populated here (unlike most tests in this file) —
    orphan detection, and therefore the vulnerable echo, is skipped entirely
    when it is `None`, which is why round 4's own dry-run test never
    exercised this path.

    Pins both halves of the round 5 fix: `_escape_for_log` keeps the
    filename's embedded newlines from ever producing an isolated line on
    stderr, and the current-individuation signal travels on the dedicated
    pipe fd alone — a `--dry-run` call never writes to it, regardless of
    what the planted filename could make stderr say."""
    _make_trusted(knowledge_ctx, monkeypatch)
    knowledge_root = tmp_path / "knowledge"
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", knowledge_root)
    _make_disk_trusted(knowledge_root)
    target = tmp_path / "repo"
    target.mkdir()
    _git_init(target)

    marker = "KNOWLEDGE_CTX_PROFILE_DOCUMENTS_CURRENT"
    agents_dir = target / ".claude" / "agents"
    agents_dir.mkdir(parents=True)
    crafted_name = f"evil\n{marker}\ntail.md"
    orphan = agents_dir / crafted_name
    orphan.write_bytes(b"planted by a prior, compromised dispatch\n")
    # No previous manifest is written, so this candidate fails gate 2
    # ("not listed in the previous individuation manifest") and is BLOCKED,
    # not removed — the simplest shape that still reaches the vulnerable
    # echo, and the one the round 5 finding named.

    rc, current = _individuate_with_signal(
        knowledge_ctx, project="constance", profile_name="commercial",
        profile={}, target_repo=target, rows=[], force=False,
        force_anyway=False, dry_run=True, known_ids=_TRUSTED_KNOWN_IDS,
    )
    assert rc == 0
    assert not current, (
        "a dry run must never signal .profile-documents.json current, "
        "regardless of what a planted filename could make stderr say")

    err = capsys.readouterr().err
    assert marker not in err.splitlines(), (
        "the marker text must never appear as its own line on stderr — "
        "the embedded newlines in the filename must be escaped")
    assert "\\n" + marker + "\\n" in err, (
        "the escaped filename should still be visible, with its embedded "
        "newlines spelled out rather than silently dropped")
    assert "not listed in the previous individuation manifest" in err, (
        "the orphan's own reason must still reach stderr, just safely")


def test_slug_from_id_rejects_unsafe_slugs(knowledge_ctx):
    """The write plan `individuate` builds joins a slug onto `agents_dir`/
    `skills_dir` — '' (a trailing slash in the doc
    id), '.', '..', and a NUL are all refused before a caller can build an
    unsafe target path from them. A normal id still resolves."""
    for bad_id in ("shared/agents/", "shared/agents/.", "shared/agents/..",
                  "shared/agents/evil\x00name"):
        with pytest.raises(SystemExit):
            knowledge_ctx.slug_from_id(bad_id)
    assert knowledge_ctx.slug_from_id("shared/agents/planner") == "planner"


def test_inventory_matches_by_slug_and_lists_unmatched(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """The Step 3 inventory: every rendered skill/agent across every workspace,
    matched to its source id, with unmatched ones called out — the read-only
    tool the epic asks for, run by hand against this machine for the PR body."""
    convivy = tmp_path / "Convivy"
    (convivy / "constance-commercial" / ".claude" / "agents").mkdir(parents=True)
    (convivy / "constance-commercial" / ".claude" / "agents" / "coder.md").write_text("x")
    (convivy / "constance-commercial" / ".claude" / "skills" / "start-overnight").mkdir(
        parents=True)
    (convivy / "constance-commercial" / ".claude" / "skills" / "start-overnight" /
     "SKILL.md").write_text("orphan")
    (convivy / "worktrees" / "vivy.overnight" / ".claude" / "skills" /
     "rollover").mkdir(parents=True)
    (convivy / "worktrees" / "vivy.overnight" / ".claude" / "skills" / "rollover" /
     "SKILL.md").write_text("matched")

    monkeypatch.setattr(knowledge_ctx, "CONVIVY", convivy)
    _Library(knowledge_ctx, monkeypatch, tmp_path, docs=[
        _bdoc("shared/agents/coder", "agent", ""),
        _bdoc("shared/skills/rollover", "skill", ""),
    ])

    rc = knowledge_ctx.inventory()
    assert rc == 0
    out = capsys.readouterr().out
    assert "constance-commercial\tagent\tcoder\tshared/agents/coder" in out
    assert "vivy.overnight\tskill\trollover\tshared/skills/rollover" in out
    assert "constance-commercial\tskill\tstart-overnight\tUNMATCHED" in out
    assert "1 unmatched" in out
    assert str(convivy / "constance-commercial" / ".claude" / "skills" /
               "start-overnight" / "SKILL.md") in out


def test_inventory_marks_unmatched_files_with_tracked_symlink_manifest_flags(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """The three per-file conditions `use`'s orphan deletion gates on
    (`_find_orphans`) are what an operator needs to see per unmatched file to
    tell "a render would actually remove this" from "a render would report
    and leave it" — a bare UNMATCHED tag can't distinguish those."""
    convivy = tmp_path / "Convivy"
    ws = convivy / "constance-commercial"
    claude_dir = ws / ".claude"
    (claude_dir / "agents").mkdir(parents=True)
    tracked = claude_dir / "agents" / "tracked.md"
    tracked.write_text("x")
    unmanifested = claude_dir / "agents" / "unmanifested.md"
    unmanifested.write_text("y")
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=ws, check=True)
    subprocess.run(["git", "add", ".claude/agents/tracked.md"], cwd=ws, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add"], cwd=ws, check=True)
    _write_manifest(knowledge_ctx, claude_dir, ["agents/tracked.md"])

    monkeypatch.setattr(knowledge_ctx, "CONVIVY", convivy)
    _Library(knowledge_ctx, monkeypatch, tmp_path)

    rc = knowledge_ctx.inventory()
    assert rc == 0
    out = capsys.readouterr().out
    tracked_line = next(ln for ln in out.splitlines() if "\ttracked\t" in ln)
    assert "tracked=yes" in tracked_line
    assert "contained=yes" in tracked_line
    assert "manifest=yes" in tracked_line
    unmanifested_line = next(ln for ln in out.splitlines() if "\tunmanifested\t" in ln)
    assert "tracked=no" in unmanifested_line
    assert "manifest=no" in unmanifested_line


# --- round 3: absence from the index alone never proves a source document --
# is gone (issue_01M2HT8TM0M85WCQ594ADPMEFT round 3)
#
# `full_scan_and_reconcile` (lib/knowledge/indexer.py) upserts one document
# per commit, so a render's index read can land mid-reconcile: both known-
# agent/known-skill sets non-empty, yet still missing the one id that
# matters. Round 2's per-file gate never checked the knowledge checkout on
# disk at all — `_known_ids_from_disk` and `_disk_trust_problem` are the
# second, independent read `_find_orphans` now also requires before treating
# a candidate as undeclared anywhere.


def test_a_source_still_on_disk_survives_a_mid_reconcile_index_gap(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """Reproduced against the round-2 code: an index with both known-agent
    and known-skill sets non-empty, but missing this one id, deleted a
    current, manifest-listed, untracked file whose source .md was still
    sitting on disk — exactly the shape a mid-`full_scan_and_reconcile` read
    produces. The disk read must survive a gap like this and report why."""
    _make_trusted(knowledge_ctx, monkeypatch)
    knowledge_root = tmp_path / "knowledge"
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", knowledge_root)
    source = knowledge_root / "projects" / "platform" / "skills" / "start-overnight.md"
    source.parent.mkdir(parents=True)
    source.write_text(
        "---\nid: platform/skills/start-overnight\nkind: skill\n"
        "project: platform\ntitle: start overnight\nstatus: current\n---\n\nBody.\n"
    )
    target = tmp_path / "repo"
    target.mkdir()
    _git_init(target)
    candidate = target / ".claude" / "skills" / "start-overnight" / "SKILL.md"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("still current — the index just hasn't caught up\n")
    _write_manifest(knowledge_ctx, target / ".claude", ["skills/start-overnight/SKILL.md"])

    # `known_ids` (the index) has no entry for start-overnight at all — the
    # reconcile-gap shape — while the source .md is still on disk.
    rc = knowledge_ctx.individuate(
        project="constance", profile_name="commercial", profile={},
        target_repo=target, rows=[], force=True, force_anyway=False,
        dry_run=False, known_ids=_TRUSTED_KNOWN_IDS,
    )
    assert rc == 0
    assert candidate.exists(), "a source still on disk must survive even though the index lacks it"
    err = capsys.readouterr().err
    assert str(candidate) in err
    assert "not yet reflected in the index" in err


def test_an_orphan_gone_from_disk_and_index_is_still_removed(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """The other half: once the source .md is ALSO gone from the knowledge
    checkout on disk — not merely excluded from the index by a reconcile gap
    — the file is a genuine orphan and is removed, confirmed through BOTH
    reads rather than just the index."""
    _make_trusted(knowledge_ctx, monkeypatch)
    knowledge_root = tmp_path / "knowledge"
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", knowledge_root)
    _make_disk_trusted(knowledge_root)  # projects/ exists and is empty
    target = tmp_path / "repo"
    target.mkdir()
    _git_init(target)
    orphan = target / ".claude" / "skills" / "start-overnight" / "SKILL.md"
    orphan.parent.mkdir(parents=True)
    orphan.write_text("stale rule nobody can correct\n")
    _write_manifest(knowledge_ctx, target / ".claude", ["skills/start-overnight/SKILL.md"])

    rc = knowledge_ctx.individuate(
        project="constance", profile_name="commercial", profile={},
        target_repo=target, rows=[], force=True, force_anyway=False,
        dry_run=False, known_ids=_TRUSTED_KNOWN_IDS,
    )
    assert rc == 0
    assert not orphan.exists()
    err = capsys.readouterr().err
    assert str(orphan) in err
    assert "orphan (no source document anywhere)" in err


def test_a_missing_knowledge_root_on_disk_refuses_deletion_and_says_why(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """The disk read is the second, independent leg of the trust check — if
    the knowledge checkout itself can't be found (unset, missing, or
    unreadable), a candidate absent from the index can no longer be trusted
    as absent everywhere, so deletion must refuse and say why, exactly like
    an untrustworthy index does."""
    _make_trusted(knowledge_ctx, monkeypatch)
    knowledge_root = tmp_path / "knowledge"  # deliberately never created
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", knowledge_root)
    target = tmp_path / "repo"
    target.mkdir()
    orphan = target / ".claude" / "agents" / "ghost.md"
    orphan.parent.mkdir(parents=True)
    orphan.write_text("---\nname: ghost\n---\n\nold.\n")
    # Non-force, with a planted manifest matching the fresh (rows=[]) plan's
    # own content exactly — see test_an_empty_index_refuses_all_deletion_and_
    # says_why for why that's what isolates this trust gate from force's
    # orthogonal, pre-existing blanket-clear behavior (force sweeps anything
    # not explicitly preserved, and orphan detection being skipped here means
    # nothing gets added to that preserve set).
    _write_manifest(knowledge_ctx, target / ".claude",
                    [".active-profile", ".materialized", ".profile-documents.json"])

    rc = knowledge_ctx.individuate(
        project="constance", profile_name="commercial", profile={},
        target_repo=target, rows=[], force=False, force_anyway=False,
        dry_run=False, known_ids=_TRUSTED_KNOWN_IDS,
    )
    assert rc == 0
    assert orphan.exists()
    err = capsys.readouterr().err
    assert "orphan detection skipped" in err
    assert "knowledge checkout on disk could not be read" in err


def test_force_still_clears_a_stray_whose_document_exists_elsewhere(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """A rendered file this PROFILE excludes but whose document is still
    live elsewhere in the corpus is not a blocked orphan at all —
    `_find_orphans`'s has_document short-circuit means it never reaches
    `blocked`, so `--force`'s ordinary clear+rewrite must still sweep it
    exactly as it always did before the orphan mechanism existed.

    Paired, in the SAME render, with a second file that DOES reach round
    3's on-disk branch (round 4, replacing this test's previous
    hand-verified, uncommitted red-check with a scenario a reader can
    reproduce): `on_disk_blocked` is unknown to the index but its source is
    genuinely present in the knowledge checkout on disk, so `_find_orphans`
    puts it in `blocked` with the "not yet reflected in the index" reason —
    force's clear (`_clear_dir_preserving_fd`) must preserve it by THAT
    reason, the same as it already must for a manifest-absent or
    git-tracked one (round 2's tests). What this would fail against, stated
    concretely rather than left to a reverted hand-edit: narrowing the
    preserve-set `individuate()` builds for the force branch (today,
    unconditionally every name in `blocked`) to exclude entries whose
    reason names the index gap would make `_clear_dir_preserving_fd` treat
    `on_disk_blocked` as unprotected and sweep it — confirmed against a
    scratch copy of `individuate()` with exactly that narrowing, not
    committed here.

    `still_live` alone cannot be made red against 8d23aa5 (round 2): that
    code already gets its scenario right, since the file was never a
    candidate at all. `on_disk_blocked` is what makes this test exercise a
    path round 2 could not even reach.
    """
    _make_trusted(knowledge_ctx, monkeypatch)
    knowledge_root = tmp_path / "knowledge"
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", knowledge_root)
    source = knowledge_root / "projects" / "platform" / "skills" / "start-overnight.md"
    source.parent.mkdir(parents=True)
    source.write_text(
        "---\nid: platform/skills/start-overnight\nkind: skill\n"
        "project: platform\ntitle: start overnight\nstatus: current\n---\n\nBody.\n"
    )
    target = tmp_path / "repo"
    target.mkdir()
    _git_init(target)
    still_live = target / ".claude" / "skills" / "triage-corrections-queue" / "SKILL.md"
    still_live.parent.mkdir(parents=True)
    still_live.write_text("excluded by this profile but still a real doc\n")
    on_disk_blocked = target / ".claude" / "skills" / "start-overnight" / "SKILL.md"
    on_disk_blocked.parent.mkdir(parents=True)
    on_disk_blocked.write_text("still current — the index just hasn't caught up\n")
    _write_manifest(knowledge_ctx, target / ".claude", [
        "skills/triage-corrections-queue/SKILL.md", "skills/start-overnight/SKILL.md",
    ])

    known_ids = {"agent": {"shared/agents/other"},
                "skill": {"shared/skills/triage-corrections-queue"}}
    rc = knowledge_ctx.individuate(
        project="constance", profile_name="commercial", profile={},
        target_repo=target, rows=[], force=True, force_anyway=False,
        dry_run=False, known_ids=known_ids,
    )
    assert rc == 0
    assert not still_live.exists(), (
        "force must still clear a file whose document is merely excluded by "
        "this profile, not treat it as a protected orphan")
    err = capsys.readouterr().err
    assert on_disk_blocked.exists(), (
        "force must preserve a candidate blocked for round 3's on-disk reason")
    assert str(on_disk_blocked) in err
    assert "not yet reflected in the index" in err


# --- round 3: `.claude/agents` or `.claude/skills` ITSELF being a symlink --

def test_individuate_aborts_before_writing_if_agents_dir_is_a_symlink(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """The old `shutil.rmtree(agents_dir)` raised on this shape; round 2's
    `_clear_dir_preserving` turned that into a warn-and-continue that still
    went on to write THROUGH the symlink afterwards (round 3, reproduced). A
    render must refuse outright, before touching anything, whichever mode."""
    knowledge_root = tmp_path / "knowledge"
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", knowledge_root)
    _stub_source(knowledge_root, "shared/agents/coder.md",
                 "---\ndescription: does the coding\n---\n\nBody.\n")
    rows = [{"id": "shared/agents/coder", "kind": "agent",
             "source_ref": "shared/agents/coder.md", "title": "coder"}]
    rows = _served_rows(knowledge_ctx, knowledge_root, rows)
    target = tmp_path / "repo"
    claude_dir = target / ".claude"
    claude_dir.mkdir(parents=True)
    outside = tmp_path / "outside-agents"
    outside.mkdir()
    (claude_dir / "agents").symlink_to(outside, target_is_directory=True)

    rc = knowledge_ctx.individuate(
        project="shared", profile_name="daytime", profile={},
        target_repo=target, rows=rows, force=False, force_anyway=False,
        dry_run=False,
    )
    assert rc == 5
    assert not (outside / "coder.md").exists(), "nothing must be written through the symlink"
    err = capsys.readouterr().err
    assert str(claude_dir / "agents") in err
    assert "is a symlink" in err
    assert "NOTHING WAS WRITTEN" in err


def test_individuate_aborts_before_writing_if_skills_dir_is_a_symlink_even_under_force(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """Same refusal under `--force`, the mode most likely to reach a
    symlinked container in practice (`co -r` always passes `--force`)."""
    knowledge_root = tmp_path / "knowledge"
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", knowledge_root)
    target = tmp_path / "repo"
    claude_dir = target / ".claude"
    claude_dir.mkdir(parents=True)
    outside = tmp_path / "outside-skills"
    outside.mkdir()
    (outside / "SKILL.md").write_text("do not touch\n")
    (claude_dir / "skills").symlink_to(outside, target_is_directory=True)

    rc = knowledge_ctx.individuate(
        project="shared", profile_name="daytime", profile={},
        target_repo=target, rows=[], force=True, force_anyway=False,
        dry_run=False,
    )
    assert rc == 5
    assert (outside / "SKILL.md").read_text() == "do not touch\n"
    assert (claude_dir / "skills").is_symlink(), "the container symlink itself must be untouched"
    err = capsys.readouterr().err
    assert str(claude_dir / "skills") in err
    assert "is a symlink" in err


# --- round 3: `find_conflicts` must ignore `.profile-documents.json`'s -----
# ever-changing `materialized_at`, the same way `plan_is_current` does
# (security-reviewer, round 2 outside-the-diff note on PR #594)


def test_find_conflicts_ignores_profile_documents_json_timestamp_only(
        knowledge_ctx, tmp_path):
    """`find_conflicts` compared `.profile-documents.json` literally even
    though that field changes on every call by design
    (`_profile_documents_match`'s own docstring) — it must ignore it via the
    SAME comparison `plan_is_current` already uses, by calling it rather than
    reimplementing. A real difference in `documents` (not just the
    timestamp) must still be reported — the exception is narrow, not a
    blanket skip of this file."""
    target = tmp_path / "repo"
    claude_dir = target / ".claude"
    claude_dir.mkdir(parents=True)
    doc_path = claude_dir / knowledge_ctx._PROFILE_DOCUMENTS_NAME
    doc_path.write_text(json.dumps({
        "profile": "shared/daytime", "materialized_at": "T1",
        "documents": [{"id": "shared/agents/coder", "kind": "agent"}],
    }))

    fresh_same = json.dumps({
        "profile": "shared/daytime", "materialized_at": "T2",
        "documents": [{"id": "shared/agents/coder", "kind": "agent"}],
    })
    assert knowledge_ctx.find_conflicts([(doc_path, fresh_same)]) == []

    fresh_different = json.dumps({
        "profile": "shared/daytime", "materialized_at": "T3",
        "documents": [{"id": "shared/agents/other", "kind": "agent"}],
    })
    assert knowledge_ctx.find_conflicts([(doc_path, fresh_different)]) == [doc_path]


def test_non_force_conflict_report_excludes_a_spurious_profile_documents_timestamp_entry(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """End-to-end: a real change elsewhere (CLAUDE.md, from an edited
    `claude_md_extras`) still correctly aborts non-force individuation, but
    the reported conflicts must no longer ALSO list `.profile-documents.json`
    purely because its timestamp moved — that entry names nothing an
    operator can resolve, and clutters the one reason `--force` is actually
    needed for. Reproduced against the pre-fix code: the conflicts list
    carried BOTH `CLAUDE.md` and `.profile-documents.json` here, even
    though `documents` itself never changed between the two calls.

    A genuinely removable orphan cannot be used to exercise this end-to-end,
    despite the security reviewer's finding being framed around one:
    removability requires the orphan to be listed in the PREVIOUS
    `.materialized` manifest, and the manifest this run computes fresh never
    lists an orphan (it renders no file for one) — so `.materialized` itself
    always differs for a real, non-spurious reason whenever an orphan is
    genuinely removable, and `find_conflicts` correctly still requires
    `--force` for that regardless of this fix (see
    test_an_orphan_absent_from_the_previous_manifest_survives's docstring,
    which already established this in round 2 for the identical reason).
    This test isolates the part of the finding that IS fixable without also
    tolerating a real manifest difference: a change that touches
    `.profile-documents.json`'s timestamp alone must not appear in the
    reported conflict list next to a genuine one.
    """
    knowledge_root = tmp_path / "knowledge"
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", knowledge_root)
    _stub_source(knowledge_root, "shared/agents/coder.md",
                 "---\ndescription: does the coding\n---\n\nBody.\n")
    rows = [{"id": "shared/agents/coder", "kind": "agent",
             "source_ref": "shared/agents/coder.md", "title": "coder"}]
    rows = _served_rows(knowledge_ctx, knowledge_root, rows)
    target = tmp_path / "repo"
    target.mkdir()
    # Git-initialized so the write-side ownership gate's git-tracked check
    # (`_is_git_tracked`, shared with the deletion gate) reads this render's
    # own untracked `CLAUDE.md` correctly, rather than failing closed to
    # "tracked" the way it does for a target that is not a git repository at
    # all (see test_a_git_tracked_claude_md_is_never_deleted) — which would
    # block this test's own second-render rewrite before it ever reaches
    # `find_conflicts`, the thing this test actually exercises.
    _git_init(target)

    rc1 = knowledge_ctx.individuate(
        project="shared", profile_name="daytime",
        profile={"claude_md_extras": ["first version"]},
        target_repo=target, rows=rows, force=False, force_anyway=False, dry_run=False,
    )
    assert rc1 == 0

    capsys.readouterr()
    rc2 = knowledge_ctx.individuate(
        project="shared", profile_name="daytime",
        profile={"claude_md_extras": ["second version"]},
        target_repo=target, rows=rows, force=False, force_anyway=False, dry_run=False,
    )
    assert rc2 == 3
    err = capsys.readouterr().err
    assert "CLAUDE.md" in err
    assert knowledge_ctx._PROFILE_DOCUMENTS_NAME not in err


# --- round 4: the disk-corroboration read must be bounded, cannot block or --
# raise, and never mistakes "could not look" for "looked and found nothing"
# (issue_01M2HT8TM0M85WCQ594ADPMEFT round 4)
#
# Round 3's `_known_ids_from_disk` read every candidate `.md` under the
# knowledge checkout with plain `path.read_text()`. Both round-3 reviews
# found this unsafe against a tree neither review is exclusive to write into:
# a FIFO named `*.md` anywhere in it blocks `open(2)` forever (nothing else
# in the render can run once that happens — every `knowledge-ctx use` on the
# machine hangs), and a single non-UTF-8 byte anywhere in any `.md` raises
# `UnicodeDecodeError` — a `ValueError` subclass the surrounding `except
# OSError` never caught — crashing the render outright.


def test_frontmatter_from_disk_does_not_block_on_a_fifo(knowledge_ctx, tmp_path):
    """`open(2)` on a FIFO with no writer blocks at the syscall until one
    appears — the exact mechanism that let a FIFO dropped into the
    knowledge checkout hang every render on the machine before this fix.
    Run in a subprocess with a hard stdlib `timeout`: an actual regression
    here would otherwise hang this whole test run, not just this test."""
    fifo = tmp_path / "a-fifo.md"
    os.mkfifo(fifo)
    kx_path = pathlib.Path(knowledge_ctx.__file__).resolve()
    script = (
        "import sys, importlib.util, importlib.machinery, pathlib\n"
        f"loader = importlib.machinery.SourceFileLoader('kx', {str(kx_path)!r})\n"
        "spec = importlib.util.spec_from_loader('kx', loader)\n"
        "kx = importlib.util.module_from_spec(spec)\n"
        "loader.exec_module(kx)\n"
        f"fm, ok = kx._frontmatter_from_disk(pathlib.Path({str(fifo)!r}), None)\n"
        "print('DONE', fm, ok)\n"
    )
    proc = subprocess.run([sys.executable, "-c", script],
                          capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, proc.stderr
    assert "DONE None True" in proc.stdout, (
        "a FIFO is proven not-a-document by lstat alone — ok=True, "
        "frontmatter=None — without ever calling open(2) on it")


def test_frontmatter_from_disk_survives_a_non_utf8_byte(knowledge_ctx, tmp_path):
    """`path.read_text()`'s default strict decoding raised
    `UnicodeDecodeError` on a stray non-UTF-8 byte anywhere in the file —
    reproduced against round 3's code, which caught only `OSError` around
    that read. `errors='replace'` here must never raise, regardless of
    where in the file the bad byte sits."""
    bad = tmp_path / "bad-bytes.md"
    bad.write_bytes(b"---\nid: platform/skills/bad\nkind: skill\n---\n\n\xff\xfeBody.\n")
    fm, ok = knowledge_ctx._frontmatter_from_disk(bad, knowledge_ctx._kb_indexer())
    assert ok is True
    assert fm is not None
    assert fm.get("id") == "platform/skills/bad"


def test_frontmatter_from_disk_reports_not_ok_for_an_unreadable_file(
        knowledge_ctx, tmp_path):
    """A file this read genuinely cannot open (unlike a FIFO, which `lstat`
    alone proves is not a document without ever attempting `open(2)`) must
    report `ok=False` — the caller must never treat "couldn't look" the
    same as "looked and found nothing.\""""
    locked = tmp_path / "locked.md"
    locked.write_text("---\nid: platform/skills/locked\nkind: skill\n---\n\nBody.\n")
    locked.chmod(0o000)
    try:
        fm, ok = knowledge_ctx._frontmatter_from_disk(locked, knowledge_ctx._kb_indexer())
    finally:
        locked.chmod(0o644)  # so tmp_path cleanup can remove it
    assert ok is False
    assert fm is None


def test_known_ids_from_disk_returns_none_when_any_file_is_unreadable(
        knowledge_ctx, tmp_path):
    """A single unreadable file anywhere in the tree makes the WHOLE scan
    report itself untrustworthy (`None`), not just skip that one file —
    this scan cannot rule out that the unreadable file's id is exactly the
    one an orphan candidate elsewhere needs, so it cannot vouch for any
    candidate's absence either (round 4, issue_01M2HT8TM0M85WCQ594ADPMEFT:
    "a file that cannot be read is treated as PRESENT — blocks deletion —
    never as absent"). A FIFO or a non-frontmatter file does NOT trigger
    this — both are confirmed, not merely unreadable — see the two tests
    above."""
    projects = tmp_path / "projects"
    skills = projects / "platform" / "skills"
    skills.mkdir(parents=True)
    (skills / "good.md").write_text(
        "---\nid: platform/skills/good\nkind: skill\n---\n\nBody.\n")
    locked = skills / "locked.md"
    locked.write_text("---\nid: platform/skills/locked\nkind: skill\n---\n\nBody.\n")
    locked.chmod(0o000)
    try:
        result = knowledge_ctx._known_ids_from_disk(projects)
    finally:
        locked.chmod(0o644)
    assert result is None


def test_known_ids_from_disk_survives_a_fifo_and_a_bad_byte_together(
        knowledge_ctx, tmp_path):
    """End-to-end version of the two syscall-level tests above: a FIFO and a
    non-UTF-8 document sitting in the SAME tree as a normal one — the scan
    must complete (not hang, not raise) and still find the normal
    document."""
    projects = tmp_path / "projects"
    skills = projects / "platform" / "skills"
    skills.mkdir(parents=True)
    os.mkfifo(skills / "a-fifo.md")
    (skills / "bad-bytes.md").write_bytes(
        b"---\nid: platform/skills/bad\nkind: skill\n---\n\n\xff\xfeBody.\n")
    (skills / "good.md").write_text(
        "---\nid: platform/skills/good\nkind: skill\n---\n\nBody.\n")

    result = knowledge_ctx._known_ids_from_disk(projects)
    assert result is not None
    assert "platform/skills/good" in result["skill"]
    assert "platform/skills/bad" in result["skill"]


# --- round 4: the disk walk only runs when it can change an outcome -------
# (issue_01M2HT8TM0M85WCQ594ADPMEFT round 4)
#
# Round 3's `individuate()` ran `_known_ids_from_disk` (a walk of the WHOLE
# knowledge checkout, ~0.5s against this machine's corpus) on every render
# with a trustworthy index, regardless of whether anything rendered was
# actually unattributed. `_has_disk_check_candidate` is a cheap pre-check
# (globs only the target repo's own small `.claude/` tree) that answers
# whether the walk could possibly change this render's outcome at all.


def test_has_disk_check_candidate_false_when_nothing_is_unattributed(
        knowledge_ctx, tmp_path):
    """Every rendered file is either part of the plan or already known to
    the index — the disk walk could not change either conclusion, so there
    is nothing for it to check."""
    agents_dir = tmp_path / ".claude" / "agents"
    skills_dir = tmp_path / ".claude" / "skills"
    agents_dir.mkdir(parents=True)
    skills_dir.mkdir(parents=True)
    known = (agents_dir / "coder.md").resolve()
    known.write_text("---\nname: coder\n---\n\nkeep.\n")
    assert knowledge_ctx._has_disk_check_candidate(
        agents_dir, skills_dir, {known}, {"agent": set(), "skill": set()}) is False

    excluded_but_known = agents_dir / "planner.md"
    excluded_but_known.write_text("---\nname: planner\n---\n\nkeep.\n")
    assert knowledge_ctx._has_disk_check_candidate(
        agents_dir, skills_dir, {known},
        {"agent": {"shared/agents/planner"}, "skill": set()}) is False


def test_has_disk_check_candidate_true_for_an_unattributed_file(knowledge_ctx, tmp_path):
    """A rendered file that is neither planned nor index-known is exactly
    the shape the disk walk exists to resolve."""
    agents_dir = tmp_path / ".claude" / "agents"
    skills_dir = tmp_path / ".claude" / "skills"
    agents_dir.mkdir(parents=True)
    skills_dir.mkdir(parents=True)
    (agents_dir / "ghost.md").write_text("---\nname: ghost\n---\n\nold.\n")
    assert knowledge_ctx._has_disk_check_candidate(
        agents_dir, skills_dir, set(), {"agent": set(), "skill": set()}) is True


def test_individuate_skips_the_disk_walk_when_nothing_is_unattributed(
        knowledge_ctx, tmp_path, monkeypatch):
    """The common case — a clean re-render, or a profile with no strays at
    all — must never pay the knowledge-tree walk's cost. `KNOWLEDGE` is
    pointed at a path that does not exist; if `_known_ids_from_disk` were
    called at all, it would report a trust problem (and this test would see
    "orphan detection skipped" on stderr) rather than silently proceed —
    the absence of that line is what proves the walk was never reached, not
    just that it returned quickly."""
    _make_trusted(knowledge_ctx, monkeypatch)
    knowledge_root = tmp_path / "knowledge"  # deliberately never created
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", knowledge_root)
    target = tmp_path / "repo"
    target.mkdir()
    # rows=[] and an empty target: nothing rendered, nothing on disk, so
    # `_has_disk_check_candidate` finds nothing unattributed and the walk is
    # skipped entirely — the no-op short-circuit is reached with `removable`
    # correctly empty either way, so this also confirms the skip doesn't
    # itself cause a false orphan report.
    rc = knowledge_ctx.individuate(
        project="constance", profile_name="commercial", profile={},
        target_repo=target, rows=[], force=False, force_anyway=False,
        dry_run=False, known_ids=_TRUSTED_KNOWN_IDS,
    )
    assert rc == 0


def test_individuate_still_walks_disk_when_a_candidate_exists(
        knowledge_ctx, tmp_path, monkeypatch):
    """The converse: a genuine orphan candidate present is exactly the case
    `_has_disk_check_candidate` must NOT skip — spy on `_known_ids_from_disk`
    to confirm it is actually invoked when there is something for it to
    resolve.

    Two calls, not one, since round 5: `_find_orphans`'s first pass finds
    the candidate, and `_reverify_removable`'s second pass re-walks the disk
    one more time, immediately before the actual delete, precisely because a
    render with a genuine candidate is exactly the render that reverify
    exists to protect (issue_01M2HT8TM0M85WCQ594ADPMEFT round 5) — this test
    pinned "exactly once" before that second, deliberate walk existed."""
    _make_trusted(knowledge_ctx, monkeypatch)
    knowledge_root = tmp_path / "knowledge"
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", knowledge_root)
    _make_disk_trusted(knowledge_root)
    target = tmp_path / "repo"
    target.mkdir()
    _git_init(target)
    orphan = target / ".claude" / "agents" / "ghost.md"
    orphan.parent.mkdir(parents=True)
    orphan.write_text("---\nname: ghost\n---\n\nold.\n")
    _write_manifest(knowledge_ctx, target / ".claude", ["agents/ghost.md"])

    calls = []
    real_walk = knowledge_ctx._known_ids_from_disk

    def spy(projects_root):
        calls.append(projects_root)
        return real_walk(projects_root)
    monkeypatch.setattr(knowledge_ctx, "_known_ids_from_disk", spy)

    rc = knowledge_ctx.individuate(
        project="constance", profile_name="commercial", profile={},
        target_repo=target, rows=[], force=True, force_anyway=False,
        dry_run=False, known_ids=_TRUSTED_KNOWN_IDS,
    )
    assert rc == 0
    assert len(calls) == 2, (
        "the disk walk must run once to find the candidate and once more "
        "to re-verify it immediately before the delete")
    assert not orphan.exists()


# --- round 4: the container-symlink guard must bind to what is actually ---
# written, not to a path checked once earlier (issue_01M2HT8TM0M85WCQ594ADPMEFT
# round 4)
#
# Round 3's `is_symlink()` check at function entry only proves the shape at
# THAT instant. Both round-3 reviews found the gap: a render that takes any
# real time between the check and the actual write — the disk-corroboration
# walk chief among them — gives an attacker a window to swap the container
# for a symlink in between. The security reviewer reproduced this by parking
# a render on a blocking FIFO read, swapping `.claude/agents` for a symlink,
# then releasing it — the rendered file landed outside the workspace.
# `_open_container_fd` closes this by opening the container `O_NOFOLLOW`
# immediately before the write phase and binding every write, unlink, and
# clear in that phase to the returned fd rather than to the path again.


def test_write_loop_is_bound_to_the_fd_opened_before_a_mid_run_container_swap(
        knowledge_ctx, tmp_path, monkeypatch):
    """The security reviewer's exact sequence, reproduced without a real
    FIFO: `find_conflicts` is the last thing that runs, in non-force mode,
    between this render's container fd being opened and its write loop —
    swap the container there. Confirmed red against the pre-round-4 code
    (which re-opens `agents_dir` by PATH inside the write loop): that
    version writes `coder.md` into `outside/`. The fd-bound version here
    must not."""
    knowledge_root = tmp_path / "knowledge"
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", knowledge_root)
    _stub_source(knowledge_root, "shared/agents/coder.md",
                 "---\ndescription: does the coding\n---\n\nBody.\n")
    rows = [{"id": "shared/agents/coder", "kind": "agent",
             "source_ref": "shared/agents/coder.md", "title": "coder"}]
    rows = _served_rows(knowledge_ctx, knowledge_root, rows)
    target = tmp_path / "repo"
    claude_dir = target / ".claude"
    agents_dir = claude_dir / "agents"
    agents_dir.mkdir(parents=True)

    outside = tmp_path / "outside-agents"
    outside.mkdir()

    real_find_conflicts = knowledge_ctx.find_conflicts

    def swap_then_check(plan):
        shutil.rmtree(agents_dir)
        agents_dir.symlink_to(outside, target_is_directory=True)
        return real_find_conflicts(plan)

    monkeypatch.setattr(knowledge_ctx, "find_conflicts", swap_then_check)

    rc = knowledge_ctx.individuate(
        project="shared", profile_name="daytime", profile={},
        target_repo=target, rows=rows, force=False, force_anyway=False,
        dry_run=False,
    )
    assert not (outside / "coder.md").exists(), (
        "nothing must be written through the swapped symlink")
    assert rc == 5, "a mid-run container swap must abort the render, not silently partial-write"


def test_force_clear_is_bound_to_the_fd_opened_before_a_mid_run_container_swap(
        knowledge_ctx, tmp_path, monkeypatch):
    """Same sequence for `--force` — the mode `co -r` always uses, and the
    one the security reviewer named as most likely to reach this shape in
    practice. The swap happens inside `_clear_dir_preserving_fd`, the FIRST
    thing that runs on the fd opened for this render, so if the fix only
    bound the LATER write loop and not this call, the swap would still
    succeed here."""
    knowledge_root = tmp_path / "knowledge"
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", knowledge_root)
    _stub_source(knowledge_root, "shared/agents/coder.md",
                 "---\ndescription: does the coding\n---\n\nBody.\n")
    rows = [{"id": "shared/agents/coder", "kind": "agent",
             "source_ref": "shared/agents/coder.md", "title": "coder"}]
    rows = _served_rows(knowledge_ctx, knowledge_root, rows)
    target = tmp_path / "repo"
    claude_dir = target / ".claude"
    agents_dir = claude_dir / "agents"
    agents_dir.mkdir(parents=True)

    outside = tmp_path / "outside-agents"
    outside.mkdir()

    real_clear = knowledge_ctx._clear_dir_preserving_fd
    swapped = []

    def swap_then_clear(dir_fd, preserve_children):
        if not swapped and dir_fd is not None:
            swapped.append(True)
            shutil.rmtree(agents_dir)
            agents_dir.symlink_to(outside, target_is_directory=True)
        return real_clear(dir_fd, preserve_children)

    monkeypatch.setattr(knowledge_ctx, "_clear_dir_preserving_fd", swap_then_clear)

    rc = knowledge_ctx.individuate(
        project="shared", profile_name="daytime", profile={},
        target_repo=target, rows=rows, force=True, force_anyway=False,
        dry_run=False,
    )
    assert not (outside / "coder.md").exists(), (
        "nothing must be written through the swapped symlink")
    assert rc == 5
    assert swapped, "the swap must actually have happened for this test to mean anything"


# --- round 5: the fd chain must bind `.claude` itself, not just `agents`/ ---
# `skills` (issue_01M2HT8TM0M85WCQ594ADPMEFT round 5)
#
# Round 4's `_open_container_fd` opened `<target_repo>/.claude/agents` BY
# PATH. `O_NOFOLLOW` on that open only refuses when the FINAL path component
# (`agents`) is itself a symlink — it says nothing about `.claude`, an
# ancestor component in the same path string. Swapping `.claude` for a
# symlink therefore defeated every fd-based protection on `agents`/`skills`
# with NO race needed at all: the open just followed the swapped parent and
# returned a valid descriptor for whatever an attacker pointed it at
# (security-reviewer, round 5). The fix opens `target_repo` itself first
# (`_open_root_fd`) and every directory after it — `.claude`, then
# `agents`/`skills` — relative to the previously-opened descriptor
# (`dir_fd=`), never by re-resolving a path from the filesystem root.


def test_a_claude_dir_symlink_defeats_no_fd_based_protection_without_a_race(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """`.claude` itself, swapped for a symlink to an attacker-controlled
    directory BEFORE the render even starts — no race, no timing window,
    just the plain shape the round-5 finding names. Reproduced against the
    pre-round-5 code: `agents_dir.is_symlink()` follows the swapped `.claude`
    to resolve `agents`, sees a perfectly ordinary (nonexistent) path, and
    the fd-open that follows opens straight through it, writing `coder.md`
    into the attacker's directory. The fix here must refuse before writing
    or deleting anything under it."""
    knowledge_root = tmp_path / "knowledge"
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", knowledge_root)
    _stub_source(knowledge_root, "shared/agents/coder.md",
                 "---\ndescription: does the coding\n---\n\nBody.\n")
    rows = [{"id": "shared/agents/coder", "kind": "agent",
             "source_ref": "shared/agents/coder.md", "title": "coder"}]
    rows = _served_rows(knowledge_ctx, knowledge_root, rows)
    target = tmp_path / "repo"
    target.mkdir()
    outside = tmp_path / "attacker-controlled"
    outside.mkdir()
    (target / ".claude").symlink_to(outside, target_is_directory=True)

    rc = knowledge_ctx.individuate(
        project="shared", profile_name="daytime", profile={},
        target_repo=target, rows=rows, force=True, force_anyway=False,
        dry_run=False,
    )
    assert rc == 5
    assert list(outside.iterdir()) == [], (
        "nothing must be written or deleted through the swapped parent")
    assert (target / ".claude").is_symlink(), "the symlink itself must be untouched"
    err = capsys.readouterr().err
    assert "NOTHING WAS WRITTEN" in err


# --- round 5: a candidate must be re-verified immediately before it is -----
# actually deleted, not trusted off a snapshot taken earlier in the render
# (issue_01M2HT8TM0M85WCQ594ADPMEFT round 5)
#
# `_find_orphans` runs once, early in `individuate()` — before the dry-run
# print, the live-process pre-flight, and the fd chain open, all of which
# take real time. A source document for a candidate's slug appearing on
# disk in that window was never re-checked before the pre-round-5 code
# unlinked it anyway.


def test_a_source_appearing_mid_render_survives_the_reverify(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """Plants a source document for `ghost`'s slug as a side effect of
    `live_claude_processes_in` — the last thing `individuate()` calls before
    committing to write — reproducing a source document landing after
    `_find_orphans`'s snapshot but before the actual delete. Reproduced
    against the pre-round-5 code: `ghost.md` is still unlinked, because
    nothing re-examines `removable` between the snapshot and the delete."""
    _make_trusted(knowledge_ctx, monkeypatch)
    knowledge_root = tmp_path / "knowledge"
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", knowledge_root)
    _make_disk_trusted(knowledge_root)
    target = tmp_path / "repo"
    target.mkdir()
    _git_init(target)
    orphan = target / ".claude" / "agents" / "ghost.md"
    orphan.parent.mkdir(parents=True)
    orphan.write_text("---\nname: ghost\n---\n\nold.\n")
    _write_manifest(knowledge_ctx, target / ".claude", ["agents/ghost.md"])

    new_doc = knowledge_root / "projects" / "shared" / "agents" / "ghost.md"
    real_live_check = knowledge_ctx.live_claude_processes_in

    def plant_then_check(target_root, exempt_pids=frozenset()):
        new_doc.parent.mkdir(parents=True, exist_ok=True)
        new_doc.write_text(
            "---\nid: shared/agents/ghost\nkind: agent\n---\n\nnewly proposed.\n")
        return real_live_check(target_root, exempt_pids=exempt_pids)

    monkeypatch.setattr(knowledge_ctx, "live_claude_processes_in", plant_then_check)

    rc = knowledge_ctx.individuate(
        project="constance", profile_name="commercial", profile={},
        target_repo=target, rows=[], force=True, force_anyway=False,
        dry_run=False, known_ids=_TRUSTED_KNOWN_IDS,
    )
    assert rc == 0
    assert orphan.exists(), (
        "a source document that appeared after the snapshot but before the "
        "actual delete must save the candidate it now belongs to")
    err = capsys.readouterr().err
    assert "no longer clears the gate at delete time" in err


# --- round 5: unreadable must never be confused with absent, even at the ---
# frontmatter-size cap (issue_01M2HT8TM0M85WCQ594ADPMEFT round 5)
#
# `_frontmatter_from_disk` reads at most `_MAX_FRONTMATTER_BYTES`. Before
# round 5, a document whose real frontmatter's closing `---` sat PAST that
# cap parsed as "malformed" from inside the truncated window and read back
# `ok=True, frontmatter=None` — "confirmed nothing to declare" — which is
# indistinguishable, to a caller, from a document that genuinely has no
# frontmatter. That is exactly the ambiguity every other branch of this
# function exists to refuse.


def test_frontmatter_from_disk_treats_a_frontmatter_past_the_cap_as_unreadable(
        knowledge_ctx, tmp_path):
    """A real, valid frontmatter block whose closing `---` sits past
    `_MAX_FRONTMATTER_BYTES` must read back `ok=False` — reproduced against
    the pre-round-5 code, which returned `ok=True, frontmatter=None` for
    this exact file."""
    big = "x" * (knowledge_ctx._MAX_FRONTMATTER_BYTES + 5000)
    path = tmp_path / "big.md"
    path.write_text(
        f"---\nid: platform/skills/big\nkind: skill\nnotes: {big}\n---\n\nBody.\n")
    fm, ok = knowledge_ctx._frontmatter_from_disk(path, knowledge_ctx._kb_indexer())
    assert ok is False
    assert fm is None


def test_an_oversized_frontmatter_on_disk_blocks_deletion_rather_than_hiding_it(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """End-to-end: an oversized-frontmatter document sitting anywhere in the
    knowledge checkout must poison `_known_ids_from_disk`'s trust the same
    way any other unreadable file does, which must in turn block deletion
    of an unrelated candidate rather than let the disk walk complete and
    silently confirm the candidate's slug absent. Non-force, matching the
    fresh (rows=[]) plan's own manifest exactly — see
    test_an_empty_index_refuses_all_deletion_and_says_why for why that
    isolates this trust gate from force's orthogonal blanket-clear."""
    _make_trusted(knowledge_ctx, monkeypatch)
    knowledge_root = tmp_path / "knowledge"
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", knowledge_root)
    big = "x" * (knowledge_ctx._MAX_FRONTMATTER_BYTES + 5000)
    oversized = knowledge_root / "projects" / "platform" / "skills" / "oversized.md"
    oversized.parent.mkdir(parents=True)
    oversized.write_text(f"---\nid: platform/skills/oversized\nnotes: {big}\n---\n\nBody.\n")

    target = tmp_path / "repo"
    target.mkdir()
    orphan = target / ".claude" / "agents" / "ghost.md"
    orphan.parent.mkdir(parents=True)
    orphan.write_text("---\nname: ghost\n---\n\nold.\n")
    _write_manifest(knowledge_ctx, target / ".claude",
                    [".active-profile", ".materialized", ".profile-documents.json"])

    rc = knowledge_ctx.individuate(
        project="constance", profile_name="commercial", profile={},
        target_repo=target, rows=[], force=False, force_anyway=False,
        dry_run=False, known_ids=_TRUSTED_KNOWN_IDS,
    )
    assert rc == 0
    assert orphan.exists(), (
        "an unreadable oversized-frontmatter file elsewhere must block "
        "deletion, not hide it as a confirmed absence")
    err = capsys.readouterr().err
    assert "orphan detection skipped" in err
    assert "knowledge checkout on disk could not be read" in err


# --- round 6: the fd-chain root itself must be pinned by identity, not -----
# just by `O_NOFOLLOW` on its own final path component
# (issue_01M2HT8TM0M85WCQ594ADPMEFT round 6)
#
# `_open_root_fd` opens `target_repo` BY PATH. `O_NOFOLLOW` there only
# refuses `target_repo` itself being a symlink — it says nothing about an
# ANCESTOR of `target_repo`, swapped for a symlink in the real time this
# render has already spent (the disk-corroboration walk, the live-process
# check) before this open runs: the final path component still names a
# genuine, non-symlink directory either way, so `O_NOFOLLOW` has nothing to
# refuse. `expected_root_stat` — `target_repo`'s `(st_dev, st_ino)`, captured
# by `main()` the instant `Path.cwd()` resolved it — closes that: the
# descriptor this open actually returns must still identify the same real
# directory, or it is refused just as if `target_repo` itself had been
# swapped.


def test_open_root_fd_refuses_when_an_ancestor_was_swapped_for_a_symlink(
        knowledge_ctx, tmp_path):
    """The literal round-6 finding, reproduced directly against
    `_open_root_fd`: an ancestor of `target_repo` is swapped for a symlink to
    a directory that also happens to have a same-named child, so the final
    path component genuinely is a real, non-symlink directory and
    `O_NOFOLLOW` alone has nothing to refuse. Confirmed red against the
    pre-round-6 code (`_open_root_fd(root)`, no identity argument at all):
    the open succeeds and returns a descriptor for the attacker's directory."""
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    target_repo = real_parent / "repo"
    target_repo.mkdir()
    st = os.stat(target_repo)
    expected = (st.st_dev, st.st_ino)

    attacker = tmp_path / "attacker"
    (attacker / "repo").mkdir(parents=True)

    shutil.rmtree(real_parent)
    real_parent.symlink_to(attacker, target_is_directory=True)

    fd, err = knowledge_ctx._open_root_fd(target_repo, expected)
    assert fd is None
    assert err is not None
    assert "no longer identifies" in err


def test_open_root_fd_accepts_a_matching_root_with_no_ancestor_swap(
        knowledge_ctx, tmp_path):
    """The ordinary case — nothing swapped between the stat and the open —
    must still succeed and match."""
    target_repo = tmp_path / "repo"
    target_repo.mkdir()
    st = os.stat(target_repo)
    expected = (st.st_dev, st.st_ino)
    fd, err = knowledge_ctx._open_root_fd(target_repo, expected)
    try:
        assert err is None
        assert fd is not None
    finally:
        if fd is not None:
            os.close(fd)


def test_individuate_refuses_when_an_ancestor_of_target_repo_is_swapped(
        knowledge_ctx, tmp_path, monkeypatch):
    """End-to-end: `individuate()` given `expected_root_stat` for the
    ORIGINAL `target_repo`, with an ancestor swapped for a symlink before the
    fd chain opens, must abort and write nothing into the attacker's
    directory. Confirmed red against the pre-round-6 code (`_open_root_fd`
    took no identity argument), which opens straight through the swapped
    ancestor and writes `coder.md` into the attacker's tree."""
    knowledge_root = tmp_path / "knowledge"
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", knowledge_root)
    _stub_source(knowledge_root, "shared/agents/coder.md",
                 "---\ndescription: does the coding\n---\n\nBody.\n")
    rows = [{"id": "shared/agents/coder", "kind": "agent",
             "source_ref": "shared/agents/coder.md", "title": "coder"}]
    rows = _served_rows(knowledge_ctx, knowledge_root, rows)

    real_parent = tmp_path / "real"
    real_parent.mkdir()
    target = real_parent / "repo"
    target.mkdir()
    st = os.stat(target)
    expected = (st.st_dev, st.st_ino)

    attacker = tmp_path / "attacker"
    (attacker / "repo").mkdir(parents=True)

    shutil.rmtree(real_parent)
    real_parent.symlink_to(attacker, target_is_directory=True)

    rc = knowledge_ctx.individuate(
        project="shared", profile_name="daytime", profile={},
        target_repo=target, rows=rows, force=True, force_anyway=False,
        dry_run=False, expected_root_stat=expected,
    )
    assert rc == 5
    assert not (attacker / "repo" / ".claude").exists(), (
        "nothing must be written into the attacker's directory through the "
        "swapped ancestor")


# --- round 6: the disk-corroboration read must not disagree with the -------
# indexer's own frontmatter parse on newline style
# (issue_01M2HT8TM0M85WCQ594ADPMEFT round 6)
#
# `_frontmatter_from_disk` reads bytes and decodes them directly.
# `indexer_mod.parse_doc` — the function this scan calls
# (`indexer_mod.split_frontmatter`) exists to stay consistent with — reads
# every document through `Path.read_text()`, whose text-mode open
# universal-newline-translates CRLF and a lone CR to LF before
# `split_frontmatter` ever sees it. Without matching that translation here, a
# CRLF (or lone-CR) document's real frontmatter — visible to the indexer —
# read back here as "no frontmatter": the exact divergence the disk check
# exists to prevent, since it is the second, independent source a
# candidate's absence is confirmed against.


def test_a_crlf_document_on_disk_is_seen_as_declaring_its_frontmatter(
        knowledge_ctx, tmp_path):
    """Reproduced against the pre-round-6 code: a CRLF-encoded document with
    real, valid frontmatter read back `ok=True, frontmatter=None` — read and
    confirmed to declare nothing — even though the same bytes, read through
    `Path.read_text()` the way the indexer reads them, parse a real id and
    kind."""
    path = tmp_path / "crlf.md"
    path.write_bytes(
        b"---\r\nid: platform/skills/crlf-doc\r\nkind: skill\r\n---\r\n\r\nBody.\r\n")
    fm, ok = knowledge_ctx._frontmatter_from_disk(path, knowledge_ctx._kb_indexer())
    assert ok is True
    assert fm is not None
    assert fm.get("id") == "platform/skills/crlf-doc"
    assert fm.get("kind") == "skill"


def test_a_lone_cr_document_on_disk_is_seen_as_declaring_its_frontmatter(
        knowledge_ctx, tmp_path):
    """Same divergence, old Mac-style lone-CR line endings."""
    path = tmp_path / "cr-only.md"
    path.write_bytes(
        b"---\rid: platform/skills/cr-doc\rkind: skill\r---\r\rBody.\r")
    fm, ok = knowledge_ctx._frontmatter_from_disk(path, knowledge_ctx._kb_indexer())
    assert ok is True
    assert fm is not None
    assert fm.get("id") == "platform/skills/cr-doc"
    assert fm.get("kind") == "skill"


def test_a_bom_document_on_disk_agrees_with_the_indexer_seeing_no_frontmatter(
        knowledge_ctx, tmp_path):
    """A leading UTF-8 BOM is not stripped by either side — `Path.read_text()`
    with no explicit `utf-8-sig` encoding leaves it in place too — so both
    the indexer and this disk scan already agree the document declares
    nothing (`ok=True, frontmatter=None`). Pinned so a future change to
    either side's decoding can't introduce a divergence silently."""
    path = tmp_path / "bom.md"
    path.write_bytes(
        b"\xef\xbb\xbf---\nid: platform/skills/bom-doc\nkind: skill\n---\n\nBody.\n")
    fm, ok = knowledge_ctx._frontmatter_from_disk(path, knowledge_ctx._kb_indexer())
    assert ok is True
    assert fm is None

    indexer_mod = knowledge_ctx._kb_indexer()
    text = path.read_text()
    _fm, _body, err = indexer_mod.split_frontmatter(text)
    assert err == "no frontmatter delimiter", (
        "the indexer's own read must agree this declares nothing too — "
        "otherwise this is a divergence, not a shared, correct outcome")


def test_individuate_treats_a_crlf_source_as_present_and_blocks_the_candidate(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """End-to-end version of the mid-reconcile-gap shape
    (test_a_source_still_on_disk_survives_a_mid_reconcile_index_gap), with
    the source document written CRLF: the disk-corroboration read must still
    see its id and block the candidate. Confirmed red against the
    pre-round-6 code, which read this exact document as declaring nothing
    and let the candidate through as a genuine orphan."""
    _make_trusted(knowledge_ctx, monkeypatch)
    knowledge_root = tmp_path / "knowledge"
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", knowledge_root)
    source = knowledge_root / "projects" / "platform" / "skills" / "start-overnight.md"
    source.parent.mkdir(parents=True)
    source.write_bytes(
        b"---\r\nid: platform/skills/start-overnight\r\nkind: skill\r\n"
        b"project: platform\r\ntitle: start overnight\r\nstatus: current\r\n"
        b"---\r\n\r\nBody.\r\n"
    )
    target = tmp_path / "repo"
    target.mkdir()
    _git_init(target)
    candidate = target / ".claude" / "skills" / "start-overnight" / "SKILL.md"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("still current — the index just hasn't caught up\n")
    _write_manifest(knowledge_ctx, target / ".claude", ["skills/start-overnight/SKILL.md"])

    rc = knowledge_ctx.individuate(
        project="constance", profile_name="commercial", profile={},
        target_repo=target, rows=[], force=True, force_anyway=False,
        dry_run=False, known_ids=_TRUSTED_KNOWN_IDS,
    )
    assert rc == 0
    assert candidate.exists(), (
        "a CRLF source document on disk must be seen the same way the "
        "indexer would see it, and must survive deletion")
    err = capsys.readouterr().err
    assert str(candidate) in err
    assert "not yet reflected in the index" in err


# --- instruction-block assembly (issue_01M2PXHKZ3SE1AQPS8MWWT691X) ---


def _load_knowledge_schema_module():
    """`lib/knowledge/knowledge.py`, loaded by explicit file path rather than
    `import knowledge` — that bare name resolves via `sys.path` search order,
    and the `knowledge_ctx` fixture's own module-level code inserts the
    DEPLOYED `~/.local/lib/knowledge` at position 0 when it loads (the same
    reason `_kb_indexer()` in `bin/knowledge-ctx` hand-rolls its own
    repo-preferring import instead of a bare one). A plain `import knowledge`
    run after that fixture has already loaded could silently resolve the
    deployed copy instead of this checkout's; loading by explicit path
    sidesteps the search order entirely."""
    path = pathlib.Path(__file__).resolve().parent.parent / "lib" / "knowledge" / "knowledge.py"
    spec = importlib.util.spec_from_file_location("_test_knowledge_schema", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_instruction_block_scopes_matches_the_knowledge_module(knowledge_ctx):
    """`bin/knowledge-ctx`'s copy of `INSTRUCTION_BLOCK_SCOPES` mirrors
    `lib/knowledge/knowledge.py`'s on the same either-side-of-the-deployed-
    lib-seam basis this file already uses for `_MANIFEST_NAME`/
    `_PROFILE_DOCUMENTS_NAME` — pinned equal here so the two copies cannot
    silently drift apart."""
    schema = _load_knowledge_schema_module()
    assert knowledge_ctx.INSTRUCTION_BLOCK_SCOPES == schema.INSTRUCTION_BLOCK_SCOPES


def _ib_row(doc_id: str, *, status: str = "current", body: str = "Rule text.\n",
            scope: str | None = None, **selectors) -> dict:
    """A synthetic `fetch_instruction_blocks()` row — the same shape the real
    query returns (id, project, status, body, body_hash, frontmatter_extra) —
    so selector and fail-closed behaviour can be tested without a real
    sqlite index."""
    fm: dict = {}
    if scope is not None:
        fm["scope"] = scope
    fm.update(selectors)
    return {
        "id": doc_id,
        "project": doc_id.split("/", 1)[0],
        "status": status,
        "body": body,
        "body_hash": hashlib.sha256(body.encode()).hexdigest(),
        "frontmatter_extra": json.dumps(fm),
    }


# THE DECLARED ROLE, NOT THE NAME. `agent_roles.role_for` reads the `role` an
# agent's document declares, from the index, so a test that resolves an agent
# needs a document declaring one. `write-better/agents/wb-reviewer` declares
# `writing-reviewer` on the real corpus; `deck-coder` and `platform-coder`
# declare `coder`.
_DECLARED_ROLES = {
    "agent-platform/agents/reviewer": "reviewer",
    "agent-platform/agents/security-reviewer": "security-reviewer",
    "agent-platform/agents/coder": "coder",
    "agent-platform/agents/platform-coder": "coder",
    "deck/agents/deck-coder": "coder",
    "selector/agents/sel-reviewer": "reviewer",
    "write-better/agents/wb-reviewer": "writing-reviewer",
}


def _declare_agent_roles(db: pathlib.Path, roles: dict[str, str]) -> None:
    """Add one `kind: agent` row per `{doc_id: role}` to an index that already
    has the schema, with the role in `frontmatter_extra` where the indexer
    puts it. A row already present has its role set instead of being added."""
    conn = sqlite3.connect(db)
    for doc_id, role in roles.items():
        fm = json.dumps({"role": role})
        if conn.execute("SELECT 1 FROM docs WHERE id = ?", (doc_id,)).fetchone():
            conn.execute("UPDATE docs SET frontmatter_extra = ? WHERE id = ?", (fm, doc_id))
            continue
        conn.execute(
            "INSERT INTO docs (id, source, source_ref, kind, project, title, status, "
            "body, body_hash, updated_at, frontmatter_extra) VALUES "
            "(?, 'files', ?, 'agent', ?, 't', 'current', 'b', 'h', '2026-09-24', ?)",
            (doc_id, f"{doc_id}.md", doc_id.split("/", 1)[0], fm))
    conn.commit()
    conn.close()


@pytest.fixture(autouse=True)
def _index_declaring_roles(knowledge_ctx, tmp_path_factory, monkeypatch):
    """Every test in this file resolves agent roles against a private index,
    never the live `~/.knowledge-index`. A test that builds its own index
    repoints `DB_PATH` after this runs, and that later setattr wins."""
    db = tmp_path_factory.mktemp("roles-index") / "index.db"
    schema = (pathlib.Path(__file__).resolve().parent.parent
              / "lib" / "knowledge" / "schema.sql").read_text()
    conn = sqlite3.connect(db)
    conn.executescript(schema)
    conn.commit()
    conn.close()
    _declare_agent_roles(db, _DECLARED_ROLES)
    monkeypatch.setattr(knowledge_ctx, "DB_PATH", db)


def test_fleet_scope_binds_every_session_with_no_selectors(knowledge_ctx):
    fm = {"scope": "fleet"}
    for crew, agent, kind in [
        (None, None, "interactive"),
        ("bosun", "reviewer", "dispatched"),
        ("selector", "wb-reviewer", "dispatched"),
    ]:
        assert knowledge_ctx._instruction_block_binds(
            fm, crew=crew, agent=agent, session_kind=kind)


def test_selector_match_is_and_across_fields_not_or(knowledge_ctx):
    """The trap this build must not fall into: a block binds only when EVERY
    selector field it carries matches. Read as OR across fields, a block
    carrying `binds_agents: [reviewer]` and `binds_sessions: [interactive]`
    would bind every reviewer AND every interactive session instead of their
    intersection — wrong on both counts, and invisible to a test that varies
    only one selector at a time (which is why this test varies both)."""
    fm = {"scope": "agent", "binds_agents": ["reviewer"],
          "binds_sessions": ["interactive"]}
    # Agent matches, session kind does not — an OR implementation matches here.
    assert not knowledge_ctx._instruction_block_binds(
        fm, crew=None, agent="reviewer", session_kind="dispatched")
    # Session kind matches, agent does not — an OR implementation matches here too.
    assert not knowledge_ctx._instruction_block_binds(
        fm, crew=None, agent=None, session_kind="interactive")
    # Both match: this is the only case that should bind.
    assert knowledge_ctx._instruction_block_binds(
        fm, crew=None, agent="reviewer", session_kind="interactive")


def test_selector_match_is_or_within_one_field(knowledge_ctx):
    fm = {"scope": "agent", "binds_agents": ["reviewer", "security-reviewer"]}
    assert knowledge_ctx._instruction_block_binds(
        fm, crew=None, agent="security-reviewer", session_kind="dispatched")
    assert knowledge_ctx._instruction_block_binds(
        fm, crew=None, agent="reviewer", session_kind="dispatched")
    assert not knowledge_ctx._instruction_block_binds(
        fm, crew=None, agent="coder", session_kind="dispatched")


def test_binds_roles_resolves_through_role_for_a_crew_prefixed_reviewer(knowledge_ctx):
    """`binds_roles: [reviewer]` must reach a crew's own reviewer, e.g.
    `sel-reviewer`, resolved through `agent_roles.role_for` rather than a
    literal-name comparison, since a crew-prefixed instance id is never
    itself a role."""
    fm = {"scope": "agent", "binds_roles": ["reviewer"]}
    assert knowledge_ctx._instruction_block_binds(
        fm, crew=None, agent="sel-reviewer", session_kind="dispatched")


def test_wb_reviewer_does_not_match_binds_roles_reviewer(knowledge_ctx):
    """`wb-reviewer` checks the writing guide's internal consistency, not a
    pull request — its document declares `writing-reviewer`, so a
    `binds_roles: [reviewer]` block must not reach it even though its name
    ends the same way every real reviewer's does. A block selecting
    `writing-reviewer` does reach it."""
    fm = {"scope": "agent", "binds_roles": ["reviewer"]}
    assert not knowledge_ctx._instruction_block_binds(
        fm, crew=None, agent="wb-reviewer", session_kind="dispatched")
    assert knowledge_ctx._instruction_block_binds(
        {"scope": "agent", "binds_roles": ["writing-reviewer"]},
        crew=None, agent="wb-reviewer", session_kind="dispatched")


def test_an_agent_with_no_document_binds_no_binds_agents_block(knowledge_ctx):
    """No document means `role_for` is None. `binds_agents` compares the exact
    name only, so a role word matches nothing, and the name is never turned
    into a role."""
    fm = {"scope": "agent", "binds_agents": ["reviewer"]}
    assert not knowledge_ctx._instruction_block_binds(
        fm, crew=None, agent="svc-reviewer", session_kind="dispatched")


def test_a_bare_name_whose_documents_disagree_fails_the_render(knowledge_ctx, capsys):
    """`role_for` raises on it. None would read as "no declared role" and drop
    every `binds_roles` block for the agent with the render still looking
    successful, so the render dies the way it does for a non-current block."""
    _declare_agent_roles(knowledge_ctx.DB_PATH, {"other/agents/sel-reviewer": "coder"})
    fm = {"scope": "agent", "binds_roles": ["reviewer"]}
    with pytest.raises(SystemExit) as e:
        knowledge_ctx._instruction_block_binds(
            fm, crew=None, agent="sel-reviewer", session_kind="dispatched")
    assert e.value.code != 0
    assert "other/agents/sel-reviewer" in capsys.readouterr().err


def test_resolve_instruction_blocks_orders_fleet_before_narrower_scopes(knowledge_ctx):
    rows = [
        _ib_row("platform/instruction-blocks/agent-only", scope="agent",
                binds_agents=["reviewer"]),
        _ib_row("platform/instruction-blocks/floor", scope="fleet"),
    ]
    resolved = knowledge_ctx.resolve_instruction_blocks(
        rows, crew=None, agent="reviewer", session_kind="dispatched")
    assert [r["id"] for r in resolved] == [
        "platform/instruction-blocks/floor",
        "platform/instruction-blocks/agent-only",
    ]


def test_resolve_instruction_blocks_matches_the_reviewer_dod_fixture(knowledge_ctx):
    """The real corpus's shape, reproduced: a dispatched reviewer receives
    the floor plus its own agent-scoped block and neither interactive-only
    block; an interactive session receives the interactive-only blocks and
    not the reviewer one; `wb-reviewer` receives neither reviewer block."""
    rows = [
        _ib_row("platform/instruction-blocks/act-rather-than-ask", scope="fleet"),
        _ib_row("platform/instruction-blocks/verify-before-you-report", scope="fleet"),
        _ib_row("platform/instruction-blocks/a-guard-needs-a-seen-occurrence", scope="fleet"),
        _ib_row("platform/instruction-blocks/no-agent-merges-a-pull-request", scope="fleet"),
        _ib_row("platform/instruction-blocks/review-a-pull-request", scope="agent",
                binds_agents=["reviewer", "security-reviewer"]),
        _ib_row("platform/instruction-blocks/open-a-pull-request", scope="crew",
                binds_sessions=["interactive"]),
        _ib_row("platform/instruction-blocks/working-with-the-operator", scope="crew",
                binds_sessions=["interactive"]),
    ]
    fleet_ids = {r["id"] for r in rows
                 if json.loads(r["frontmatter_extra"])["scope"] == "fleet"}
    assert len(fleet_ids) == 4

    reviewer = knowledge_ctx.resolve_instruction_blocks(
        rows, crew="bosun", agent="reviewer", session_kind="dispatched")
    reviewer_ids = {r["id"] for r in reviewer}
    assert "platform/instruction-blocks/review-a-pull-request" in reviewer_ids
    assert "platform/instruction-blocks/open-a-pull-request" not in reviewer_ids
    assert "platform/instruction-blocks/working-with-the-operator" not in reviewer_ids
    assert fleet_ids <= reviewer_ids

    interactive = knowledge_ctx.resolve_instruction_blocks(
        rows, crew="bosun", agent=None, session_kind="interactive")
    interactive_ids = {r["id"] for r in interactive}
    assert "platform/instruction-blocks/open-a-pull-request" in interactive_ids
    assert "platform/instruction-blocks/working-with-the-operator" in interactive_ids
    assert "platform/instruction-blocks/review-a-pull-request" not in interactive_ids
    assert fleet_ids <= interactive_ids

    wb = knowledge_ctx.resolve_instruction_blocks(
        rows, crew="write-better", agent="wb-reviewer", session_kind="dispatched")
    wb_ids = {r["id"] for r in wb}
    assert "platform/instruction-blocks/review-a-pull-request" not in wb_ids
    assert fleet_ids <= wb_ids


def test_resolve_instruction_blocks_fails_closed_on_non_current_status(knowledge_ctx):
    """A block that binds this session but is not `status: current` must
    stop the render rather than being silently left out — the
    `start-overnight` shape (issue_01M2HT8TM0M85WCQ594ADPMEFT), reproduced
    here for instruction blocks."""
    rows = [_ib_row("platform/instruction-blocks/floor", scope="fleet",
                    status="deprecated")]
    with pytest.raises(SystemExit):
        knowledge_ctx.resolve_instruction_blocks(
            rows, crew=None, agent=None, session_kind="interactive")


def test_resolve_instruction_blocks_ignores_non_current_status_when_excluded_anyway(
        knowledge_ctx):
    """A block that does NOT bind this session is excluded on selector
    grounds alone, whatever its status — only a block this session was
    actually going to receive triggers the fail-closed abort."""
    rows = [_ib_row("selector/instruction-blocks/other-crew-only", scope="crew",
                    binds_crews=["selector"], status="deprecated")]
    resolved = knowledge_ctx.resolve_instruction_blocks(
        rows, crew="bosun", agent=None, session_kind="interactive")
    assert resolved == []


def test_resolve_instruction_blocks_fails_closed_on_unreadable_frontmatter(knowledge_ctx):
    row = _ib_row("platform/instruction-blocks/floor", scope="fleet")
    row["frontmatter_extra"] = "{not json"
    with pytest.raises(SystemExit):
        knowledge_ctx.resolve_instruction_blocks(
            [row], crew=None, agent=None, session_kind="interactive")


def test_individuate_writes_instruction_blocks_named_with_id_and_hash(
        knowledge_ctx, tmp_path, monkeypatch):
    """The assembled file names each composed block by id and `body_sha256`
    (the index's `body_hash` column), so a past run's instructions
    reconstruct exactly.

    Writes to `.claude/CLAUDE.md` — the path Claude Code actually loads
    (measured on 2.1.280) — and never to `.claude/CLAUDE.local.md`, which
    this tool composed into before this fix but Claude Code never read."""
    knowledge_root = tmp_path / "knowledge"
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", knowledge_root)
    target = tmp_path / "repo"
    target.mkdir()
    row = _ib_row("platform/instruction-blocks/floor", scope="fleet",
                  body="Always verify before you report.\n")
    resolved = knowledge_ctx.resolve_instruction_blocks(
        [row], crew=None, agent=None, session_kind="interactive")

    rc = knowledge_ctx.individuate(
        project="platform", profile_name="daytime", profile={},
        target_repo=target, rows=[], force=False, force_anyway=False,
        dry_run=False, instruction_blocks=resolved,
    )
    assert rc == 0
    claude_md = (target / ".claude" / "CLAUDE.md").read_text()
    assert "platform/instruction-blocks/floor" in claude_md
    assert row["body_hash"] in claude_md
    assert "Always verify before you report." in claude_md
    assert not (target / ".claude" / "CLAUDE.local.md").exists()


def test_individuate_removes_a_claude_md_it_no_longer_composes(
        knowledge_ctx, tmp_path, monkeypatch):
    """A render composing NO `CLAUDE.md` content must delete the file a
    previous render left, rather than leaving it to be read as current.

    This is not hypothetical and was not argued into existence: run
    `individuate` on the `--force` path `agentctl.activate_profile` uses, once
    with `claude_md_extras` and once with them removed, and before the fix the
    withdrawn rule was still on disk afterwards while the render reported exit
    0 and `wrote N files.` Inclusion-in-plan is the only thing that rewrites
    this file, so the content-goes-empty transition was invisible.

    It is the mirror of `resolve_instruction_blocks`'s fail-closed rule: there
    a rule that should apply must not go missing quietly, here a rule that
    should no longer apply must not linger quietly.
    """
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", tmp_path / "knowledge")
    target = tmp_path / "repo"
    target.mkdir()
    _git_init(target)
    claude_md = target / ".claude" / "CLAUDE.md"

    rc = knowledge_ctx.individuate(
        project="platform", profile_name="daytime",
        profile={"claude_md_extras": ["NEVER force-push to main."]},
        target_repo=target, rows=[], force=True, force_anyway=True, dry_run=False,
    )
    assert rc == 0
    assert "NEVER force-push to main." in claude_md.read_text()

    # The same worktree, rendered again with nothing to compose.
    rc = knowledge_ctx.individuate(
        project="platform", profile_name="daytime", profile={},
        target_repo=target, rows=[], force=True, force_anyway=True, dry_run=False,
    )
    assert rc == 0
    assert not claude_md.exists()


def test_a_withdrawn_claude_md_does_not_survive_behind_the_no_op_path(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """The idempotency short-circuit must not report `already current` and
    return while a withdrawn `CLAUDE.md` is still on disk.

    It does not, and for a reason worth pinning rather than restating: a file
    this render may delete is by definition listed in the previous manifest,
    and `.materialized` is itself a planned target whose content is the sorted
    list of what the render owns — so the render that stops composing the file
    also drops it from that list, and `plan_is_current` already fails on the
    `.materialized` comparison. This test is what would catch that coupling
    being broken by a later change to how the manifest is built.
    """
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", tmp_path / "knowledge")
    target = tmp_path / "repo"
    target.mkdir()
    _git_init(target)
    claude_md = target / ".claude" / "CLAUDE.md"

    # Render once WITH content, so the file is genuinely this tool's — listed
    # in the manifest the next render reads. Planting it by hand would be
    # blocked by the ownership gate, which is the next test's subject.
    knowledge_ctx.individuate(
        project="platform", profile_name="daytime",
        profile={"claude_md_extras": ["A rule this profile will stop composing."]},
        target_repo=target, rows=[], force=False, force_anyway=True, dry_run=False,
    )
    # Render again with nothing to compose.
    rc = knowledge_ctx.individuate(
        project="platform", profile_name="daytime", profile={},
        target_repo=target, rows=[], force=True, force_anyway=True, dry_run=False,
    )
    assert rc == 0
    assert "already current" not in capsys.readouterr().out
    assert not claude_md.exists()


def test_a_hand_written_claude_md_is_never_deleted(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """`.claude/CLAUDE.md` is Claude Code's generic path for local repo
    instructions (measured on 2.1.280: Claude Code loads it), not a name this
    tool owns by virtue of writing it sometimes.

    An operator who hand-places notes there, in a repo whose profile composes
    nothing this render, must get them back — and `knowledge-ctx use` typed at
    a terminal is a named occurrence, not a weaker hypothetical one. So the
    removal clears the same ownership gate every other file this tool deletes
    must clear: listed in the PREVIOUS manifest, which a hand-written file
    never is.

    The render here SWITCHES PROFILE, which is what makes this a real test of
    the gate rather than of the message it prints. Re-rendering the same
    profile leaves every planned target identical, so the idempotency
    short-circuit returns before the deletion is reached and the file survives
    whether or not the gate exists. Switching profiles changes
    `.active-profile`, the plan is no longer current, and the deletion path is
    genuinely entered — with the gate removed, this file is destroyed.
    """
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", tmp_path / "knowledge")
    target = tmp_path / "repo"
    target.mkdir()
    _git_init(target)

    knowledge_ctx.individuate(
        project="platform", profile_name="daytime", profile={},
        target_repo=target, rows=[], force=False, force_anyway=True, dry_run=False,
    )
    claude_md = target / ".claude" / "CLAUDE.md"
    claude_md.write_text("Notes I wrote by hand.\n")

    rc = knowledge_ctx.individuate(
        project="platform", profile_name="overnight", profile={},
        target_repo=target, rows=[], force=True, force_anyway=True, dry_run=False,
    )
    assert rc == 0
    assert claude_md.read_text() == "Notes I wrote by hand.\n"
    assert "not listed in the previous individuation manifest" in capsys.readouterr().out


def test_a_git_tracked_claude_md_is_never_deleted(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """Committing the file makes it the repo's rather than this render's, and
    the gate fails closed on anything git does not POSITIVELY report as
    untracked — so a target that is not a git repository at all is also
    treated as tracked. Both readings protect the same thing: a deletion this
    check cannot clear never proceeds."""
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", tmp_path / "knowledge")
    target = tmp_path / "repo"
    target.mkdir()
    _git_init(target)

    knowledge_ctx.individuate(
        project="platform", profile_name="daytime",
        profile={"claude_md_extras": ["Composed once."]},
        target_repo=target, rows=[], force=False, force_anyway=True, dry_run=False,
    )
    claude_md = target / ".claude" / "CLAUDE.md"
    subprocess.run(["git", "add", "-f", ".claude/CLAUDE.md"],
                   cwd=target, check=True)

    rc = knowledge_ctx.individuate(
        project="platform", profile_name="daytime", profile={},
        target_repo=target, rows=[], force=True, force_anyway=True, dry_run=False,
    )
    assert rc == 0
    assert claude_md.exists()
    assert "tracked by git in this repo" in capsys.readouterr().out


def test_individuate_removes_a_legacy_claude_local_md_owned_by_the_previous_manifest(
        knowledge_ctx, tmp_path, monkeypatch):
    """A `.claude/CLAUDE.local.md` this tool composed into before this fix —
    listed in the manifest a previous render left — is cleaned up on the next
    render, whatever that render composes at the live `CLAUDE.md` path.

    This pins the migration behaviour: the retired path is never written
    again, so its removal cannot be conditioned on "this render composes
    nothing" the way `CLAUDE.md`'s own stale-file check is. Planted directly
    (not via a real pre-fix render, which no longer exists in this codebase)
    with a hand-built previous manifest that lists it, exactly as a real
    pre-fix `.materialized` would.
    """
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", tmp_path / "knowledge")
    target = tmp_path / "repo"
    target.mkdir()
    _git_init(target)
    claude_dir = target / ".claude"
    _write_manifest(knowledge_ctx, claude_dir, ["CLAUDE.local.md", knowledge_ctx._MANIFEST_NAME])
    legacy_md = claude_dir / "CLAUDE.local.md"
    legacy_md.write_text("A rule composed before this tool moved off this path.\n")

    rc = knowledge_ctx.individuate(
        project="platform", profile_name="daytime",
        profile={"claude_md_extras": ["The live rule."]},
        target_repo=target, rows=[], force=True, force_anyway=True, dry_run=False,
    )
    assert rc == 0
    assert not legacy_md.exists()
    assert "The live rule." in (claude_dir / "CLAUDE.md").read_text()


def test_a_legacy_claude_local_md_not_in_the_manifest_survives_a_render(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """A `.claude/CLAUDE.local.md` an operator hand-placed — never listed in a
    previous manifest, since this tool no longer writes there — survives a
    render exactly as a hand-placed `.claude/CLAUDE.md` does. Same ownership
    gate, same reasoning, the retired path included."""
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", tmp_path / "knowledge")
    target = tmp_path / "repo"
    target.mkdir()
    _git_init(target)

    knowledge_ctx.individuate(
        project="platform", profile_name="daytime", profile={},
        target_repo=target, rows=[], force=False, force_anyway=True, dry_run=False,
    )
    legacy_md = target / ".claude" / "CLAUDE.local.md"
    legacy_md.write_text("Notes I wrote by hand.\n")

    rc = knowledge_ctx.individuate(
        project="platform", profile_name="overnight", profile={},
        target_repo=target, rows=[], force=True, force_anyway=True, dry_run=False,
    )
    assert rc == 0
    assert legacy_md.read_text() == "Notes I wrote by hand.\n"
    assert "not listed in the previous individuation manifest" in capsys.readouterr().out


def test_a_hand_written_claude_md_survives_a_force_render_that_composes_content(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """The write side of the same ownership question
    `test_a_hand_written_claude_md_is_never_deleted` settles for deletion:
    a `.claude/CLAUDE.md` never listed in a previous individuation
    manifest is not this tool's file, so a render that DOES compose content
    must not truncate it through `_write_regular_relative` either. Under
    `--force`, `find_conflicts` never runs, so nothing else would stop that
    write — the render must report loudly that composed instructions were
    not delivered rather than silently keeping the file.

    The second render switches PROFILE NAME (daytime -> overnight), which is
    what makes this a real test of the write-side gate rather than of the
    idempotency short-circuit: without a real change elsewhere in the plan,
    a render that skips only the blocked CLAUDE.md write would look
    unchanged from the previous one and return before ever reaching the
    write loop.

    Also pins the two surfaces a dispatched run actually reaches
    (security-reviewer, PR #635 round 3): `bin/co` never captures this
    tool's stdout, so the human line must reach stderr too, and
    `activate_profile` only inspects captured output on a zero exit by
    matching a stable marker — not this human prose — so that marker must
    carry the same path/reason. The telemetry record individuation already
    emits must carry the same reason, for the same reason the CI dashboard
    reads outcome from telemetry rather than from stdout.
    """
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", tmp_path / "knowledge")
    import types
    telemetry_records = []
    monkeypatch.setattr(knowledge_ctx, "kb_telemetry", types.SimpleNamespace(
        INDIVIDUATE_LOG="fake-individuate-log",
        append=lambda log_path, record: telemetry_records.append(record),
    ))
    target = tmp_path / "repo"
    target.mkdir()
    _git_init(target)

    # Baseline render with nothing to compose, just to create `.claude/`.
    knowledge_ctx.individuate(
        project="platform", profile_name="daytime", profile={},
        target_repo=target, rows=[], force=False, force_anyway=True, dry_run=False,
    )
    claude_md = target / ".claude" / "CLAUDE.md"
    claude_md.write_text("Notes I wrote by hand.\n")

    rc = knowledge_ctx.individuate(
        project="platform", profile_name="overnight",
        profile={"claude_md_extras": ["A rule this render would compose."]},
        target_repo=target, rows=[], force=True, force_anyway=True, dry_run=False,
    )
    assert rc == 0
    assert claude_md.read_text() == "Notes I wrote by hand.\n"
    captured = capsys.readouterr()
    assert "not listed in the previous individuation manifest" in captured.out
    assert "NOT delivered" in captured.out
    assert "NOT delivered" in captured.err
    assert (f"KNOWLEDGE_CTX_CLAUDE_MD_NOT_DELIVERED path={claude_md} "
            "reason=not listed in the previous individuation manifest") in captured.err
    # The rest of the render still proceeded — this render is not aborted
    # wholesale over one file that is not the tool's to touch.
    assert (target / ".claude" / ".active-profile").read_text() == "platform/overnight\n"

    assert telemetry_records[-1]["claude_md_blocked"] == {
        "path": str(claude_md),
        "reason": "not listed in the previous individuation manifest",
    }


def test_a_hand_written_claude_md_survives_a_non_force_render_that_composes_content(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """Same as the `--force` case above, but on the default path: since the
    blocked `CLAUDE.md` write is dropped from the plan before `find_conflicts`
    ever runs, there is nothing left in the plan for the hand-written file to
    conflict with — a non-force render neither overwrites it nor aborts over
    it.

    Same profile both renders (unlike the `--force` test above, which
    switches profile name to prove the write path is genuinely exercised):
    here nothing else in the plan changes either, so with the blocked
    `CLAUDE.md` excluded the two plans are identical and this comes out a
    true no-op — which is itself the point on the non-force path.
    """
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", tmp_path / "knowledge")
    target = tmp_path / "repo"
    target.mkdir()
    _git_init(target)

    knowledge_ctx.individuate(
        project="platform", profile_name="daytime", profile={},
        target_repo=target, rows=[], force=False, force_anyway=True, dry_run=False,
    )
    claude_md = target / ".claude" / "CLAUDE.md"
    claude_md.write_text("Notes I wrote by hand.\n")

    rc = knowledge_ctx.individuate(
        project="platform", profile_name="daytime",
        profile={"claude_md_extras": ["A rule this render would compose."]},
        target_repo=target, rows=[], force=False, force_anyway=True, dry_run=False,
    )
    assert rc == 0
    assert claude_md.read_text() == "Notes I wrote by hand.\n"
    captured = capsys.readouterr()
    assert "not listed in the previous individuation manifest" in captured.out
    assert "NOT delivered" in captured.out
    assert "NOT delivered" in captured.err


def test_a_manifest_owned_claude_md_is_overwritten_normally_on_force(
        knowledge_ctx, tmp_path, monkeypatch):
    """The ownership gate must not catch the ordinary case: a `CLAUDE.md`
    THIS TOOL composed on a previous render — listed in that render's
    manifest — is this tool's file, and a later render with different
    content overwrites it exactly as before this fix."""
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", tmp_path / "knowledge")
    target = tmp_path / "repo"
    target.mkdir()
    _git_init(target)

    knowledge_ctx.individuate(
        project="platform", profile_name="daytime",
        profile={"claude_md_extras": ["Rule A."]},
        target_repo=target, rows=[], force=False, force_anyway=True, dry_run=False,
    )
    claude_md = target / ".claude" / "CLAUDE.md"
    assert "Rule A." in claude_md.read_text()

    rc = knowledge_ctx.individuate(
        project="platform", profile_name="daytime",
        profile={"claude_md_extras": ["Rule B."]},
        target_repo=target, rows=[], force=True, force_anyway=True, dry_run=False,
    )
    assert rc == 0
    content = claude_md.read_text()
    assert "Rule B." in content
    assert "Rule A." not in content


def test_first_render_after_migration_writes_claude_md_from_a_legacy_manifest(
        knowledge_ctx, tmp_path, monkeypatch):
    """The migration scenario the new write-side ownership gate must not
    block: every individuated repo's PREVIOUS manifest lists `CLAUDE.local.md`
    and not `CLAUDE.md` (this tool composed at the legacy path before the
    move to the live one), and no crew directory has a `.claude/CLAUDE.md` on
    disk yet. On the first render after the move, `claude_md_path.exists()`
    is False, so the ownership gate never triggers and the file is simply
    written."""
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", tmp_path / "knowledge")
    target = tmp_path / "repo"
    target.mkdir()
    _git_init(target)
    claude_dir = target / ".claude"
    _write_manifest(knowledge_ctx, claude_dir, ["CLAUDE.local.md", knowledge_ctx._MANIFEST_NAME])
    claude_md = claude_dir / "CLAUDE.md"
    assert not claude_md.exists()

    rc = knowledge_ctx.individuate(
        project="platform", profile_name="daytime",
        profile={"claude_md_extras": ["The composed rule."]},
        target_repo=target, rows=[], force=True, force_anyway=True, dry_run=False,
    )
    assert rc == 0
    assert "The composed rule." in claude_md.read_text()


def test_resolve_instruction_blocks_fails_closed_on_a_missing_scope(knowledge_ctx):
    """A block carrying no `scope` places no selector constraint either, so it
    binds every session exactly as `scope: fleet` would — and is caught only by
    the composition-order check downstream. Pinned explicitly because that is
    the whole distance between "malformed document" and "a rule nobody merged
    as fleet reaching every agent on the machine"."""
    row = _ib_row("platform/instruction-blocks/no-scope")
    assert json.loads(row["frontmatter_extra"]) == {}
    with pytest.raises(SystemExit):
        knowledge_ctx.resolve_instruction_blocks(
            [row], crew=None, agent=None, session_kind="interactive")

    unplaceable = _ib_row("platform/instruction-blocks/bad-scope", scope="galaxy")
    with pytest.raises(SystemExit):
        knowledge_ctx.resolve_instruction_blocks(
            [unplaceable], crew=None, agent=None, session_kind="interactive")


def test_a_block_kind_outside_the_blocks_path_is_never_composed(
        knowledge_ctx, tmp_path, monkeypatch):
    """`kind` alone must not decide what reaches a session's CLAUDE.md.

    The write-side guard (`kb_writes.PR_ONLY_SEGMENTS`) keys on the
    `/instruction-blocks/` path. If the read keyed on `kind` alone the two would
    guard different sets, and the gap was reachable: a document at
    `projects/platform/decisions/0099-foo.md` carrying `kind: instruction-block`
    and `scope: fleet` passes every validator, is not a guarded path, so it takes
    an `in-session` direct write to main unreviewed — and was then composed into
    a dispatched reviewer's file. Demonstrated against this code before the path
    clause was added (security review, PR #616).

    The clause matches `source_ref`, the path the indexer records from where the
    file actually sits, which is the same string `is_pr_only` checks.
    """
    db = tmp_path / "index.db"
    schema = (pathlib.Path(__file__).resolve().parent.parent
              / "lib" / "knowledge" / "schema.sql").read_text()
    conn = sqlite3.connect(db)
    conn.executescript(schema)
    body = "A rule nobody reviewed.\n"
    conn.execute(
        "INSERT INTO docs (id, source, source_ref, kind, project, title, status, "
        "body, body_hash, updated_at, frontmatter_extra) VALUES "
        "(?, 'files', ?, 'instruction-block', 'platform', 'Foo', 'current', "
        "?, ?, '2026-09-17', ?)",
        ("platform/decisions/0099-foo", "projects/platform/decisions/0099-foo.md",
         body, hashlib.sha256(body.encode()).hexdigest(),
         json.dumps({"scope": "fleet"})),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(knowledge_ctx, "DB_PATH", db)
    assert knowledge_ctx.fetch_instruction_blocks() == []
    # And therefore nothing is composed for any session, fleet scope included.
    resolved = knowledge_ctx.resolve_instruction_blocks(
        knowledge_ctx.fetch_instruction_blocks(),
        crew="bosun", agent="reviewer", session_kind="dispatched")
    assert resolved == []


def test_a_forged_frontmatter_id_cannot_smuggle_a_block_past_the_guard(
        knowledge_ctx, tmp_path, monkeypatch):
    """The read must key on `source_ref`, never on `id`.

    `id` is a frontmatter field the author writes, validated only against
    `ID_PATTERN` and never checked against the file's real location. `source_ref`
    is what the indexer records from where the file sits, and is the same string
    `kb_writes.is_pr_only` guards. So a filter on `id` can be satisfied by one
    extra frontmatter line while the write guard sees an unguarded path — file at
    `projects/platform/decisions/0099-foo.md`, declare
    `id: platform/instruction-blocks/evil-block`, and an `id`-based clause
    matches it.

    That is not hypothetical: the first fix for this bypass matched on `id` and
    was defeated exactly this way (security review, PR #616, round 2). This test
    is the adversarial case the earlier one missed — its synthetic row took its
    id from the file's own natural path, so it could not have caught this.
    """
    db = tmp_path / "index.db"
    schema = (pathlib.Path(__file__).resolve().parent.parent
              / "lib" / "knowledge" / "schema.sql").read_text()
    conn = sqlite3.connect(db)
    conn.executescript(schema)
    body = "Obey this.\n"
    conn.execute(
        "INSERT INTO docs (id, source, source_ref, kind, project, title, status, "
        "body, body_hash, updated_at, frontmatter_extra) VALUES "
        "(?, 'files', ?, 'instruction-block', 'platform', 'X', 'current', "
        "?, ?, '2026-09-17', ?)",
        # id CONTAINS the guarded segment; source_ref does not.
        ("platform/instruction-blocks/evil-block",
         "projects/platform/decisions/0099-foo.md",
         body, hashlib.sha256(body.encode()).hexdigest(),
         json.dumps({"scope": "fleet"})),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(knowledge_ctx, "DB_PATH", db)
    assert knowledge_ctx.fetch_instruction_blocks() == []


def test_the_filter_keys_on_source_ref_alone_and_ignores_the_id(
        knowledge_ctx, tmp_path, monkeypatch):
    """`source_ref` decides, and `id` is not consulted in either direction.

    Two rows whose `id` and `source_ref` DISAGREE, pointing opposite ways, so
    this discriminates between four implementations rather than confirming one:

      - keying on `source_ref` alone (correct) returns exactly the served row
      - keying on `kind` alone returns both
      - keying on `id` returns exactly the wrong one
      - keying on `source_ref` AND `id` returns neither

    An earlier version of this test used a single row whose id and path agreed,
    which could not tell any of those apart — it would have passed against a
    regression that quietly reintroduced an `id` condition beside the
    `source_ref` one, and against no filter at all.
    """
    db = tmp_path / "index.db"
    schema = (pathlib.Path(__file__).resolve().parent.parent
              / "lib" / "knowledge" / "schema.sql").read_text()
    conn = sqlite3.connect(db)
    conn.executescript(schema)
    for doc_id, source_ref, title in [
        # Served: the FILE sits under the guarded path, though its declared id
        # does not mention it.
        ("platform/decisions/legit-block",
         "projects/platform/instruction-blocks/legit.md", "Legit"),
        # Refused: the declared id mentions the guarded path, the file does not
        # sit there.
        ("platform/instruction-blocks/evil-block",
         "projects/platform/decisions/0099-foo.md", "Evil"),
    ]:
        body = f"{title}.\n"
        conn.execute(
            "INSERT INTO docs (id, source, source_ref, kind, project, title, "
            "status, body, body_hash, updated_at, frontmatter_extra) VALUES "
            "(?, 'files', ?, 'instruction-block', 'platform', ?, 'current', "
            "?, ?, '2026-09-17', ?)",
            (doc_id, source_ref, title, body,
             hashlib.sha256(body.encode()).hexdigest(),
             json.dumps({"scope": "fleet"})),
        )
    conn.commit()
    conn.close()

    monkeypatch.setattr(knowledge_ctx, "DB_PATH", db)
    assert [r["id"] for r in knowledge_ctx.fetch_instruction_blocks()] == [
        "platform/decisions/legit-block"]


def test_fetch_instruction_blocks_reads_the_real_docs_table(
        knowledge_ctx, tmp_path, monkeypatch):
    """Every other instruction-block test builds synthetic rows and skips the
    SQL, so the column names in `fetch_instruction_blocks` are pinned by
    nothing. A rename in `lib/knowledge/schema.sql` would otherwise surface
    only when a real render failed. Builds the table from that schema file
    rather than a hand-written CREATE, so the test moves with it."""
    db = tmp_path / "index.db"
    schema = (pathlib.Path(__file__).resolve().parent.parent
              / "lib" / "knowledge" / "schema.sql").read_text()
    conn = sqlite3.connect(db)
    conn.executescript(schema)
    conn.execute(
        "INSERT INTO docs (id, source, source_ref, kind, project, title, status, "
        "body, body_hash, updated_at, frontmatter_extra) VALUES "
        "(?, 'files', 'projects/platform/instruction-blocks/floor.md', "
        "'instruction-block', 'platform', 'Floor', 'current', "
        "?, ?, '2026-09-17', ?)",
        ("platform/instruction-blocks/floor", "Rule text.\n", "abc123",
         json.dumps({"scope": "fleet"})),
    )
    conn.execute(
        "INSERT INTO docs (id, source, source_ref, kind, project, title, status, "
        "body, body_hash, updated_at, frontmatter_extra) VALUES "
        "(?, 'files', 'projects/platform/reference/other.md', "
        "'reference', 'platform', 'Not a block', 'current', "
        "'other', 'def456', '2026-09-17', '{}')",
        ("platform/reference/other",),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(knowledge_ctx, "DB_PATH", db)
    rows = knowledge_ctx.fetch_instruction_blocks()
    assert [r["id"] for r in rows] == ["platform/instruction-blocks/floor"]
    assert rows[0]["body_hash"] == "abc123"
    assert json.loads(rows[0]["frontmatter_extra"]) == {"scope": "fleet"}


def _index_with_instruction_blocks(db: pathlib.Path) -> None:
    """A real index carrying the shape the live corpus has: fleet blocks that
    bind everyone, one `binds_agents` block, one `binds_sessions: [interactive]`
    block."""
    schema = (pathlib.Path(__file__).resolve().parent.parent
              / "lib" / "knowledge" / "schema.sql").read_text()
    conn = sqlite3.connect(db)
    conn.executescript(schema)
    for doc_id, body, fm in [
        ("platform/instruction-blocks/verify-before-you-report",
         "Say what you ran.\n", {"scope": "fleet"}),
        ("platform/instruction-blocks/review-a-pull-request",
         "Review the whole diff.\n",
         {"scope": "agent", "binds_agents": ["reviewer", "security-reviewer"]}),
        ("platform/instruction-blocks/working-with-the-operator",
         "End on one concrete proposal.\n",
         {"scope": "crew", "binds_sessions": ["interactive"]}),
        # The product `convivy-deck` resolves to crew `deck`; its PROJECT,
        # `deck`, resolves to no crew at all. So this block binds only if
        # `main()` passes the PRODUCT to `crew_for_repo`, which is what makes
        # the product/project distinction observable at all.
        ("platform/instruction-blocks/deck-only",
         "A rule for this crew.\n",
         {"scope": "crew", "binds_crews": ["deck"]}),
    ]:
        conn.execute(
            "INSERT INTO docs (id, source, source_ref, kind, project, title, "
            "status, body, body_hash, updated_at, frontmatter_extra) VALUES "
            "(?, 'files', ?, 'instruction-block', 'platform', ?, 'current', "
            "?, ?, '2026-09-17', ?)",
            (doc_id, f"{doc_id}.md", doc_id.rsplit("/", 1)[-1], body,
             hashlib.sha256(body.encode()).hexdigest(), json.dumps(fm)),
        )
    conn.commit()
    conn.close()
    _declare_agent_roles(db, _DECLARED_ROLES)


@pytest.fixture(autouse=True)
def _sandbox_home(tmp_path, monkeypatch):
    """`use` reads `~/.claude/.floor-manifest.json` to decide whether to compose
    fleet blocks, so no test in this file may see the real HOME. Starts with no
    manifest (floor not cut over); a test opts in with `_cut_over_floor`."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def _no_unfaked_library(monkeypatch):
    """Every Library call ends in `urllib.request.urlopen`. A test that reaches it
    without a `_Library` fake (which replaces `_library_open`) fails here, loudly,
    rather than calling the live Library from a test run."""
    def unfaked(req, timeout=None):
        raise AssertionError(
            f"a test reached the Library unfaked: {req.get_method()} {req.full_url}")
    monkeypatch.setattr("urllib.request.urlopen", unfaked)


def _cut_over_floor(home, output=None):
    """Write `~/.claude/.floor-manifest.json` under `home`, naming `output`
    (default `home/.claude/CLAUDE.md`) as the file the floor render owns."""
    claude = home / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    (claude / ".floor-manifest.json").write_text(json.dumps(
        {"output": str(output or claude / "CLAUDE.md"), "blocks": []}))


def _run_use_through_main(knowledge_ctx, tmp_path, monkeypatch, db, argv,
                          cut_over=True):
    """Drive `knowledge-ctx use` through `main()` in a tree it can individuate,
    and return the rendered CLAUDE.md text.

    HOME is a sandbox under `tmp_path`, never the real one: `use` reads
    `~/.claude/.floor-manifest.json` to decide whether to compose fleet blocks.
    `cut_over=True` (the default, so the tests written for the cut-over render
    keep their meaning) writes a manifest naming the sandbox's CLAUDE.md;
    `cut_over=False` leaves the sandbox without one.

    COUPLED TO THE REAL `config/projects.json`, deliberately and not
    incidentally: `crew_for_repo` is not monkeypatched, so the `deck` crew
    that `convivy-deck` resolves to is the registry's own. That coupling is
    the fixture's whole point — a stubbed crew map would make the
    product/project distinction unobservable again — but it is worth stating,
    because renaming the `deck` crew or moving `convivy-deck` out of its
    `repos` list will fail these tests from another file with nothing linking
    the two. The registry is only READ here; nothing outside `tmp_path` is
    written.
    """
    monkeypatch.setattr(knowledge_ctx, "CONVIVY", tmp_path)
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", tmp_path / "knowledge")
    monkeypatch.setattr(knowledge_ctx, "DB_PATH", db)
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    manifest = home / ".claude" / ".floor-manifest.json"
    if cut_over:
        _cut_over_floor(home)
    elif manifest.exists():
        manifest.unlink()

    # The product is `convivy-deck` and its project is `deck` — DELIBERATELY a
    # pair that disagrees, so the render depends on which of the two reaches
    # `crew_for_repo`. Some `projects.EXPLICIT_PROJECT_BY_NAME` rows diverge this
    # way; a fixture drawn from the rows that do not cannot tell them apart.
    (tmp_path / "knowledge" / "projects" / "deck").mkdir(parents=True, exist_ok=True)
    _Library(knowledge_ctx, monkeypatch, tmp_path)

    # Callable twice in one test (a dispatched render, then an interactive one).
    product_dir = tmp_path / "convivy-deck"
    if not product_dir.exists():
        product_dir.mkdir()
        _git_init(product_dir)
    monkeypatch.chdir(product_dir)

    monkeypatch.setattr("sys.argv", argv)
    assert knowledge_ctx.main() == 0
    return (product_dir / ".claude" / "CLAUDE.md").read_text()


def test_use_with_an_agent_renders_a_dispatched_session_through_main(
        knowledge_ctx, tmp_path, monkeypatch):
    """THROUGH `main()`, NOT `resolve_instruction_blocks`. Every other
    instruction-block test passes `crew=`/`agent=`/`session_kind=` by hand, so
    all of them stay green if `main()`'s wiring is wrong — and that wiring is
    the whole feature.

    Four regressions would each misclassify a dispatched render, and each is
    planted and caught here: inverting the `session_kind` ternary, dropping
    `args.agent` from the `resolve_instruction_blocks` call, passing no crew,
    and passing `project` where `crew_for_repo` wants `product`. The last is
    why this renders `convivy-deck` rather than a repo whose product and
    project share a name — most `projects.EXPLICIT_PROJECT_BY_NAME` rows resolve
    to the same crew either way, so a fixture drawn from those cannot tell the two
    arguments apart, and a test written on one would pass whichever was
    passed.

    The failure being pinned is the one `activate_profile`'s docstring
    describes: a worker receiving `working-with-the-operator` while
    `review-a-pull-request` reaches no reviewer. Nothing else in this suite or
    in CI would notice it.
    """
    db = tmp_path / "index.db"
    _index_with_instruction_blocks(db)
    text = _run_use_through_main(
        knowledge_ctx, tmp_path, monkeypatch, db,
        ["knowledge-ctx", "use", "daytime", "--agent", "reviewer",
         "--force", "--force-anyway"])

    # A dispatched render composes fleet blocks itself (its own claude never
    # reads the floor that owns them for an interactive session).
    assert "platform/instruction-blocks/verify-before-you-report" in text
    assert "platform/instruction-blocks/review-a-pull-request" in text
    assert "platform/instruction-blocks/deck-only" in text
    assert "platform/instruction-blocks/working-with-the-operator" not in text


def test_use_without_an_agent_renders_an_interactive_session_through_main(
        knowledge_ctx, tmp_path, monkeypatch):
    """The other half of the same ternary: no `--agent` is a crew session, so
    it receives the `binds_sessions: [interactive]` block and not the
    reviewer's."""
    db = tmp_path / "index.db"
    _index_with_instruction_blocks(db)
    text = _run_use_through_main(
        knowledge_ctx, tmp_path, monkeypatch, db,
        ["knowledge-ctx", "use", "daytime", "--force", "--force-anyway"])

    assert "platform/instruction-blocks/verify-before-you-report" not in text
    assert "platform/instruction-blocks/working-with-the-operator" in text
    assert "platform/instruction-blocks/deck-only" in text
    assert "platform/instruction-blocks/review-a-pull-request" not in text


def test_individuate_with_no_instruction_blocks_is_unaffected(
        knowledge_ctx, tmp_path, monkeypatch):
    """Every pre-existing caller passes no `instruction_blocks` at all — the
    default must behave exactly as before this feature existed, i.e. no
    `CLAUDE.md` when the profile also carries no `claude_md_extras`."""
    knowledge_root = tmp_path / "knowledge"
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", knowledge_root)
    target = tmp_path / "repo"
    target.mkdir()

    rc = knowledge_ctx.individuate(
        project="platform", profile_name="daytime", profile={},
        target_repo=target, rows=[], force=False, force_anyway=False,
        dry_run=False,
    )
    assert rc == 0
    assert not (target / ".claude" / "CLAUDE.md").exists()


# --- per-agent instruction blocks on a crew-session render -----------------
# (issue_01M35B8Q926KNB53X4050T9V5F)
#
# A crew-session render (no `--agent`) resolves its `CLAUDE.md` with
# `agent=None`, so a `binds_agents` block can never land there — the gap this
# section closes is that `.claude/agents/*.md` is the only file such a block
# can reach for an agent invoked as an Agent-tool subagent within that
# session.


def test_resolve_agent_bound_instruction_blocks_excludes_non_agent_selectors(
        knowledge_ctx):
    """A fleet block and a `binds_crews`-only crew block both match
    `agent="reviewer"` under `resolve_instruction_blocks` — neither carries
    `binds_agents`, so their match does not depend on which agent this is.
    Both are already correctly resolved (or excluded) by the crew-session's
    own `agent=None` call and belong in `CLAUDE.md` once; repeating them
    per-agent would duplicate that section into every individuated
    definition. Only the block that actually names `reviewer` belongs
    here."""
    rows = [
        _ib_row("platform/instruction-blocks/floor", scope="fleet"),
        _ib_row("platform/instruction-blocks/crew-only", scope="crew",
                binds_crews=["bosun"]),
        _ib_row("platform/instruction-blocks/reviewer-only", scope="agent",
                binds_agents=["reviewer"]),
    ]
    resolved = knowledge_ctx.resolve_agent_bound_instruction_blocks(
        rows, crew="bosun", agent="reviewer", session_kind="interactive")
    assert [r["id"] for r in resolved] == [
        "platform/instruction-blocks/reviewer-only"]


def test_resolve_agent_bound_instruction_blocks_fails_closed_on_non_current_status(
        knowledge_ctx):
    """Reuses `resolve_instruction_blocks`'s fail-closed behaviour: a
    `binds_agents` block that matches this agent but is not `status:
    current` must stop the render rather than being silently left off the
    agent's own definition (the `start-overnight` shape,
    issue_01M2HT8TM0M85WCQ594ADPMEFT, reproduced for a per-agent block)."""
    row = _ib_row("platform/instruction-blocks/review-a-pull-request",
                  scope="agent", binds_agents=["reviewer"], status="deprecated")
    with pytest.raises(SystemExit):
        knowledge_ctx.resolve_agent_bound_instruction_blocks(
            [row], crew=None, agent="reviewer", session_kind="dispatched")


def _insert_instruction_block_row(db_path, doc_id, body, fm) -> None:
    """Insert one `kind: instruction-block` row into an already-schema'd
    index db — the same shape `_index_with_instruction_blocks` builds a
    whole fixture around, factored out so a test can add rows onto an index
    another helper (`_seed_crew_agent_render_env`, `_seed_render_env`)
    already created."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO docs (id, source, source_ref, kind, project, title, "
        "status, body, body_hash, updated_at, frontmatter_extra) VALUES "
        "(?, 'files', ?, 'instruction-block', 'platform', ?, 'current', "
        "?, ?, '2026-09-22', ?)",
        (doc_id, f"{doc_id}.md", doc_id.rsplit("/", 1)[-1], body,
         hashlib.sha256(body.encode()).hexdigest(), json.dumps(fm)))
    conn.commit()
    conn.close()


def _seed_crew_agent_render_env(knowledge_ctx, tmp_path, monkeypatch, agent_bodies):
    """A CREW-session (no `--agent`) render environment: the Library resolves
    `agent-platform/daytime` to one `kind: agent` document per entry of
    `agent_bodies`, and the index holds the same rows for the roles and blocks
    that read it. No source file is written: a render never reads one.

    `agent_bodies` is `{slug: body_text}`. Returns the product directory
    `main()` should already be `chdir`'d into (done here) so a test only
    needs to set `sys.argv` and call `main()`.
    """
    knowledge_root = tmp_path / "knowledge"
    (knowledge_root / "projects" / "agent-platform").mkdir(parents=True)
    _Library(knowledge_ctx, monkeypatch, tmp_path, profiles={
        ("agent-platform", "daytime"): [
            _bdoc(f"agent-platform/agents/{slug}", "agent", body)
            for slug, body in agent_bodies.items()]})

    db = tmp_path / "index.db"
    schema = (pathlib.Path(__file__).resolve().parent.parent
              / "lib" / "knowledge" / "schema.sql").read_text()
    conn = sqlite3.connect(db)
    conn.executescript(schema)
    for slug, body in agent_bodies.items():
        doc_id = f"agent-platform/agents/{slug}"
        source_ref = f"{doc_id}.md"
        conn.execute(
            "INSERT INTO docs (id, source, source_ref, kind, project, title, "
            "status, body, body_hash, updated_at) VALUES (?, 'files', ?, "
            "'agent', 'agent-platform', ?, 'current', ?, ?, '2026-09-22')",
            (doc_id, source_ref, slug, body, hashlib.sha256(body.encode()).hexdigest()))
    conn.commit()
    conn.close()
    _declare_agent_roles(db, {f"agent-platform/agents/{slug}": _DECLARED_ROLES[
        f"agent-platform/agents/{slug}"] for slug in agent_bodies})

    product_dir = tmp_path / "agent-platform"
    product_dir.mkdir()
    _git_init(product_dir)
    monkeypatch.setattr(knowledge_ctx, "CONVIVY", tmp_path)
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", knowledge_root)
    monkeypatch.setattr(knowledge_ctx, "DB_PATH", db)
    monkeypatch.setattr(knowledge_ctx, "_DEFAULT_DB_PATH", db)
    monkeypatch.chdir(product_dir)
    return product_dir


def test_use_without_an_agent_appends_bound_blocks_to_the_bound_agents_definition_only(
        knowledge_ctx, tmp_path, monkeypatch):
    """THE GAP THIS FIX CLOSES: a crew-session render composes `CLAUDE.md`
    with `agent=None`, so a `binds_agents` block (e.g. a reviewer's trust
    boundary) can never land there — the only file left for it is the
    bound agent's own individuated definition, which this pins landing on
    `reviewer.md` alone: not on the unrelated `platform-coder.md`, and not
    repeated into `CLAUDE.md` either.
    `test_use_without_an_agent_renders_an_interactive_session_through_main`
    already pins that `CLAUDE.md` itself is unaffected; this is the other
    half."""
    product_dir = _seed_crew_agent_render_env(
        knowledge_ctx, tmp_path, monkeypatch,
        {"reviewer": "Review the diff.\n", "platform-coder": "Write the code.\n"})
    _insert_instruction_block_row(
        tmp_path / "index.db", "platform/instruction-blocks/verify-before-you-report",
        "Say what you ran.\n", {"scope": "fleet"})
    _insert_instruction_block_row(
        tmp_path / "index.db", "platform/instruction-blocks/review-a-pull-request",
        "Do not run the test suite.\n",
        {"scope": "agent", "binds_agents": ["reviewer"]})

    _cut_over_floor(tmp_path / "home")
    monkeypatch.setattr("sys.argv", ["knowledge-ctx", "use", "daytime",
                                     "--force", "--force-anyway"])
    assert knowledge_ctx.main() == 0

    claude_dir = product_dir / ".claude"
    reviewer_md = (claude_dir / "agents" / "reviewer.md").read_text()
    coder_md = (claude_dir / "agents" / "platform-coder.md").read_text()
    # Nothing composes into CLAUDE.md here: the only block that binds is
    # agent-bound, and the fleet block belongs to the floor render.
    claude_md_path = claude_dir / "CLAUDE.md"
    claude_md = claude_md_path.read_text() if claude_md_path.exists() else ""

    block_id = "platform/instruction-blocks/review-a-pull-request"
    assert block_id in reviewer_md
    assert "Do not run the test suite." in reviewer_md
    assert block_id not in coder_md
    assert block_id not in claude_md
    fleet_id = "platform/instruction-blocks/verify-before-you-report"
    assert fleet_id not in claude_md
    assert fleet_id not in reviewer_md
    assert fleet_id not in coder_md


def test_agent_with_no_bound_block_keeps_its_definition_byte_identical(
        knowledge_ctx, tmp_path, monkeypatch):
    """`platform-coder` binds to no block in this fixture; its individuated
    definition must come out byte-identical to a render of the same source
    taken before any instruction block existed in the index at all — the
    per-agent section is added only for an agent something actually binds,
    never as an empty marker or a trailing-whitespace change."""
    product_dir = _seed_crew_agent_render_env(
        knowledge_ctx, tmp_path, monkeypatch, {"platform-coder": "Write the code.\n"})
    argv = ["knowledge-ctx", "use", "daytime", "--force", "--force-anyway"]
    monkeypatch.setattr("sys.argv", argv)
    assert knowledge_ctx.main() == 0
    unbound = (product_dir / ".claude" / "agents" / "platform-coder.md").read_text()

    # An instruction block now exists in the index, binding a DIFFERENT
    # agent — proving its mere presence changes nothing for one it does not
    # bind.
    _insert_instruction_block_row(
        tmp_path / "index.db", "platform/instruction-blocks/review-a-pull-request",
        "Do not run the test suite.\n",
        {"scope": "agent", "binds_agents": ["reviewer"]})
    monkeypatch.setattr("sys.argv", argv)
    assert knowledge_ctx.main() == 0
    still_unbound = (product_dir / ".claude" / "agents" / "platform-coder.md").read_text()
    assert still_unbound == unbound


def test_use_without_an_agent_rerender_is_byte_identical(
        knowledge_ctx, tmp_path, monkeypatch):
    """Re-individuating an unchanged workspace must produce the exact same
    bytes and hit `plan_is_current`'s idempotency short-circuit, or a crew
    session's `.claude` churns on every render that touches it."""
    product_dir = _seed_crew_agent_render_env(
        knowledge_ctx, tmp_path, monkeypatch, {"reviewer": "Review the diff.\n"})
    _insert_instruction_block_row(
        tmp_path / "index.db", "platform/instruction-blocks/review-a-pull-request",
        "Do not run the test suite.\n",
        {"scope": "agent", "binds_agents": ["reviewer"]})

    monkeypatch.setattr("sys.argv", ["knowledge-ctx", "use", "daytime",
                                     "--force", "--force-anyway"])
    assert knowledge_ctx.main() == 0
    reviewer_md_path = product_dir / ".claude" / "agents" / "reviewer.md"
    first = reviewer_md_path.read_text()

    # No --force this time: a genuine no-op must hit plan_is_current.
    monkeypatch.setattr("sys.argv", ["knowledge-ctx", "use", "daytime"])
    assert knowledge_ctx.main() == 0
    second = reviewer_md_path.read_text()
    assert second == first


# --- `check` (issue_01M4GJRCDNKCV4QREH68KSRS3W) -----------------------------
#
# `check` must resolve the active profile through the SAME calls `use`
# makes, so every test below renders for real through `main()` first — never
# hand-builds `.profile-documents.json` — then mutates the index and runs
# `check` through `main()` again, in the same workspace `use` just rendered.


def _render_for_check(knowledge_ctx, tmp_path, monkeypatch, *, agent_bodies=None,
                      instruction_blocks=()):
    """An interactive crew-session render (`agent-platform/daytime`, no
    `--agent`) for `check`'s tests. `instruction_blocks` is `(doc_id, body,
    fm)` triples inserted into the index before the render, so `check`'s
    baseline sees exactly what `use` wrote. Returns `(product_dir, db_path)`;
    cwd is already chdir'd to `product_dir`, as `use` left it."""
    if agent_bodies is None:
        agent_bodies = {"platform-coder": "Write the code.\n"}
    product_dir = _seed_crew_agent_render_env(knowledge_ctx, tmp_path, monkeypatch, agent_bodies)
    db = tmp_path / "index.db"
    for doc_id, body, fm in instruction_blocks:
        _insert_instruction_block_row(db, doc_id, body, fm)
    # `--force-anyway` skips the live-process ABORT, not the `ps` scan
    # itself (`individuate`'s pre-flight 1 always runs it) — irrelevant to
    # what `check` is being tested against here, and `ps` is unreachable
    # from a sandboxed test runner (no such sandbox restriction applies to
    # the real `co`/`agentctl` callers this guard protects).
    monkeypatch.setattr(knowledge_ctx, "live_claude_processes_in",
                        lambda target_root, exempt_pids=frozenset(): [])
    monkeypatch.setattr("sys.argv", ["knowledge-ctx", "use", "daytime",
                                     "--force", "--force-anyway"])
    assert knowledge_ctx.main() == 0
    return product_dir, db


def _run_check(knowledge_ctx, monkeypatch, capsys) -> tuple[int, str]:
    monkeypatch.setattr("sys.argv", ["knowledge-ctx", "check"])
    rc = knowledge_ctx.main()
    return rc, capsys.readouterr().out


def test_check_exits_0_when_nothing_changed_since_the_render(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    _render_for_check(knowledge_ctx, tmp_path, monkeypatch, instruction_blocks=[
        ("platform/instruction-blocks/floor", "Say what you ran.\n", {"scope": "fleet"}),
    ])
    rc, out = _run_check(knowledge_ctx, monkeypatch, capsys)
    assert rc == 0
    assert "agent-platform/daytime" in out


def test_check_exits_1_naming_a_blocks_changed_body(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    block_id = "platform/instruction-blocks/floor"
    _, db = _render_for_check(knowledge_ctx, tmp_path, monkeypatch, instruction_blocks=[
        (block_id, "Say what you ran.\n", {"scope": "fleet"}),
    ])
    # The rule was replaced (the vivy-commercial occurrence this fixes:
    # platform/decisions/0083 replaced a review-round rule, in place, at the
    # same id) — not added or removed, so the comparison must say "changed",
    # not "+"/"-".
    new_body = "Say what you ran, and how.\n"
    conn = sqlite3.connect(db)
    conn.execute("UPDATE docs SET body = ?, body_hash = ? WHERE id = ?",
                 (new_body, hashlib.sha256(new_body.encode()).hexdigest(), block_id))
    conn.commit()
    conn.close()

    capsys.readouterr()  # discard the render's own plan-summary output above
    rc, out = _run_check(knowledge_ctx, monkeypatch, capsys)
    assert rc == 1
    assert out.strip().splitlines() == [f"~ instruction-block {block_id}"]


def test_check_exits_1_naming_an_added_and_a_removed_block(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    removed_id = "platform/instruction-blocks/floor"
    added_id = "platform/instruction-blocks/second-floor"
    _, db = _render_for_check(knowledge_ctx, tmp_path, monkeypatch, instruction_blocks=[
        (removed_id, "Say what you ran.\n", {"scope": "fleet"}),
    ])
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM docs WHERE id = ?", (removed_id,))
    conn.commit()
    conn.close()
    _insert_instruction_block_row(db, added_id, "A new fleet rule.\n", {"scope": "fleet"})

    rc, out = _run_check(knowledge_ctx, monkeypatch, capsys)
    assert rc == 1
    lines = out.splitlines()
    assert f"+ instruction-block {added_id}" in lines
    assert f"- instruction-block {removed_id}" in lines


def test_check_exits_1_naming_an_added_and_a_removed_document(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """The other half of the comparison this command makes: the profile's
    document id set, independent of the instruction-block set above."""
    _render_for_check(
        knowledge_ctx, tmp_path, monkeypatch,
        agent_bodies={"platform-coder": "Write the code.\n", "reviewer": "Review it.\n"})
    # Swap the Library's served profile out from under the render: `reviewer`
    # left the profile, `coder` joined it — same shape as `collect_matches`'
    # own refusal test, but via the `/profiles/...` route `load_profile` reads.
    _Library(knowledge_ctx, monkeypatch, tmp_path, profiles={
        ("agent-platform", "daytime"): [
            _bdoc("agent-platform/agents/platform-coder", "agent", "Write the code.\n"),
            _bdoc("agent-platform/agents/coder", "agent", "Code too.\n")]})

    rc, out = _run_check(knowledge_ctx, monkeypatch, capsys)
    assert rc == 1
    lines = out.splitlines()
    assert "+ document agent-platform/agents/coder" in lines
    assert "- document agent-platform/agents/reviewer" in lines


@pytest.mark.parametrize("corrupt", ["missing", "malformed"])
def test_check_exits_3_cannot_tell_on_a_missing_or_malformed_manifest(
        knowledge_ctx, tmp_path, monkeypatch, capsys, corrupt):
    product_dir, _ = _render_for_check(knowledge_ctx, tmp_path, monkeypatch)
    manifest = product_dir / ".claude" / ".profile-documents.json"
    if corrupt == "missing":
        manifest.unlink()
    else:
        manifest.write_text("{not valid json")

    rc, _ = _run_check(knowledge_ctx, monkeypatch, capsys)
    assert rc == knowledge_ctx.CHECK_CANNOT_TELL


def test_check_exits_3_with_no_active_profile(knowledge_ctx, tmp_path, monkeypatch, capsys):
    product_dir, _ = _render_for_check(knowledge_ctx, tmp_path, monkeypatch)
    (product_dir / ".claude" / ".active-profile").unlink()

    rc, _ = _run_check(knowledge_ctx, monkeypatch, capsys)
    assert rc == knowledge_ctx.CHECK_CANNOT_TELL


def test_check_exits_3_when_the_profile_no_longer_resolves(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """A resolve failure (the profile the manifest names is gone from the
    Library, say) is "cannot tell", not a crash and not "behind" — `check`
    never claims to know the current state when it could not read it."""
    _render_for_check(knowledge_ctx, tmp_path, monkeypatch)
    _Library(knowledge_ctx, monkeypatch, tmp_path, profiles={})

    rc, _ = _run_check(knowledge_ctx, monkeypatch, capsys)
    assert rc == knowledge_ctx.CHECK_CANNOT_TELL


def test_check_refuses_a_disallowed_profile_before_resolving_it(
        knowledge_ctx, tmp_path, monkeypatch):
    """EA may only ever individuate `ea-daytime` (the allowlist tests above).
    `cmd_use` enforces that through `check_profile_allowed` before
    `load_profile`; `check` must refuse the same way from the same
    workspace-writable `.active-profile` marker, rather than silently
    resolving and diffing a profile this directory is forbidden to use
    (security-reviewer, PR #835 round 1)."""
    monkeypatch.setattr(knowledge_ctx, "CONVIVY", tmp_path)
    ea = tmp_path / "EA"
    (ea / ".claude").mkdir(parents=True)
    (ea / ".claude" / ".active-profile").write_text("platform/daytime\n")
    (ea / ".claude" / ".profile-documents.json").write_text("{}")
    monkeypatch.chdir(ea)

    load_profile_calls = []
    monkeypatch.setattr(knowledge_ctx, "load_profile",
                        lambda *a, **k: load_profile_calls.append((a, k)))

    monkeypatch.setattr("sys.argv", ["knowledge-ctx", "check"])
    assert knowledge_ctx.main() == knowledge_ctx.CHECK_CANNOT_TELL
    assert load_profile_calls == [], (
        "check must refuse via check_profile_allowed before calling "
        "load_profile, not after resolving the disallowed profile")


def test_check_refuses_a_marker_naming_ea_from_an_unrestricted_workspace(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """The reverse of the test above: there the CALLING directory was EA and
    the marker named a wider profile; here the calling directory
    (`agent-platform`) is unrestricted, so `check_profile_allowed` is a
    no-op for whatever `profile_name` the marker names, and the only thing
    standing between an unrestricted workspace and EA's own profile is
    `project` itself. Rewriting `.active-profile` to name EA — the
    platform's black box (platform/decisions/0043) — must not make `check`
    fetch and print EA's profile; `cmd_use` can never be pointed at a
    project the marker names, because it only ever resolves `project` from
    cwd, and `check` must refuse the same way before any `load_profile` call
    (security-reviewer, PR #835 round 3)."""
    product_dir, _ = _render_for_check(knowledge_ctx, tmp_path, monkeypatch)
    (product_dir / ".claude" / ".active-profile").write_text("EA/ea-daytime\n")

    load_profile_calls = []
    monkeypatch.setattr(knowledge_ctx, "load_profile",
                        lambda *a, **k: load_profile_calls.append((a, k)))

    rc, _ = _run_check(knowledge_ctx, monkeypatch, capsys)
    assert rc == knowledge_ctx.CHECK_CANNOT_TELL
    assert load_profile_calls == [], (
        "check must refuse a marker naming a different project than cwd "
        "resolves to before calling load_profile")


def test_check_refuses_a_marker_naming_any_other_project_than_cwd_resolves_to(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """Same gap as the test above, generalized past the EA literal: the
    mismatch check must compare `project` against what cwd itself resolves
    to, not special-case EA's name, since any directory could have its
    marker rewritten to name any OTHER project it has no claim to."""
    product_dir, _ = _render_for_check(knowledge_ctx, tmp_path, monkeypatch)
    (product_dir / ".claude" / ".active-profile").write_text(
        "some-unrelated-project/daytime\n")

    load_profile_calls = []
    monkeypatch.setattr(knowledge_ctx, "load_profile",
                        lambda *a, **k: load_profile_calls.append((a, k)))

    rc, _ = _run_check(knowledge_ctx, monkeypatch, capsys)
    assert rc == knowledge_ctx.CHECK_CANNOT_TELL
    assert load_profile_calls == []


def test_check_still_resolves_when_the_marker_names_the_project_cwd_resolves_to(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """The new project/cwd comparison must not false-positive on the
    ordinary case: a marker left exactly as `use` wrote it, naming the same
    project cwd resolves to. `check` still reaches the diff and reports it,
    same as before the marker was ever compared against cwd."""
    block_id = "platform/instruction-blocks/floor"
    _, db = _render_for_check(knowledge_ctx, tmp_path, monkeypatch, instruction_blocks=[
        (block_id, "Say what you ran.\n", {"scope": "fleet"}),
    ])
    new_body = "Say what you ran, and how.\n"
    conn = sqlite3.connect(db)
    conn.execute("UPDATE docs SET body = ?, body_hash = ? WHERE id = ?",
                 (new_body, hashlib.sha256(new_body.encode()).hexdigest(), block_id))
    conn.commit()
    conn.close()

    capsys.readouterr()  # discard the render's own plan-summary output above
    rc, out = _run_check(knowledge_ctx, monkeypatch, capsys)
    assert rc == 1
    assert out.strip().splitlines() == [f"~ instruction-block {block_id}"]


def test_check_escapes_control_characters_in_a_manifest_id_before_printing(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """`.profile-documents.json` is written by the workspace's own render but
    stays writable by anything already running there — the crew session
    itself, or anything it spawns — and `check` now prints lines built from
    it on every plain `co` launch, not just an explicit `-r`. A crafted id
    must not reach the operator's real terminal as a raw escape sequence
    (security-reviewer, PR #835 round 1)."""
    block_id = "platform/instruction-blocks/floor"
    malicious_id = "platform/instruction-blocks/\x1b]0;title\x07evil\x1b[2J"
    product_dir, _ = _render_for_check(knowledge_ctx, tmp_path, monkeypatch, instruction_blocks=[
        (block_id, "Say what you ran.\n", {"scope": "fleet"}),
    ])
    manifest_path = product_dir / ".claude" / ".profile-documents.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["instruction_blocks"].append(
        {"id": malicious_id, "body_sha256": "deadbeef"})
    manifest_path.write_text(json.dumps(manifest))

    rc, out = _run_check(knowledge_ctx, monkeypatch, capsys)
    assert rc == 1
    assert "\x1b" not in out, "a raw ESC byte reached stdout"
    assert "\x07" not in out, "a raw BEL byte reached stdout"
    assert "\\x1b" in out, "the ESC byte must still be visible, escaped"
    assert "evil" in out


# --- `check --record` (issue_01M4GJRCDNKCV4QREH68KSRS3W slice 2) ------------
#
# A live session's own render-staleness hook keeps a copy of what IT loaded
# at launch, taken once, and must diff the CURRENT resolution against that
# copy rather than the workspace's on-disk `.profile-documents.json` — which
# a re-render during the session's life (the steward's `use --force`, or a
# fresh `co -r` in the same directory) updates out from under it. `--record`
# is the option that lets it: same comparison, same output, same exit codes,
# only the "previous" side moves.


def _run_check_record(knowledge_ctx, monkeypatch, capsys, record_path):
    monkeypatch.setattr("sys.argv", ["knowledge-ctx", "check", "--record", str(record_path)])
    rc = knowledge_ctx.main()
    return rc, capsys.readouterr().out


def test_check_record_reports_behind_against_a_stale_baseline_even_though_the_workspace_is_current(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """The exact re-render-under-a-live-session scenario slice 2 exists for:
    a baseline is taken (what the hook would copy at the session's first
    firing), the Library changes, and the workspace is re-rendered to match
    — so the DEFAULT check (against the now-current on-disk manifest) reads
    current, while `--record` against the old baseline still reports
    behind."""
    block_id = "platform/instruction-blocks/floor"
    product_dir, db = _render_for_check(knowledge_ctx, tmp_path, monkeypatch, instruction_blocks=[
        (block_id, "Say what you ran.\n", {"scope": "fleet"}),
    ])
    manifest_path = product_dir / ".claude" / ".profile-documents.json"
    record_path = tmp_path / "session-baseline.json"
    record_path.write_text(manifest_path.read_text())

    new_body = "Say what you ran, and how.\n"
    conn = sqlite3.connect(db)
    conn.execute("UPDATE docs SET body = ?, body_hash = ? WHERE id = ?",
                 (new_body, hashlib.sha256(new_body.encode()).hexdigest(), block_id))
    conn.commit()
    conn.close()

    # Re-render for real (the steward's hand re-render, or a fresh `co -r`
    # in the same directory) — the on-disk manifest now matches the Library.
    monkeypatch.setattr(knowledge_ctx, "live_claude_processes_in",
                        lambda target_root, exempt_pids=frozenset(): [])
    monkeypatch.setattr("sys.argv", ["knowledge-ctx", "use", "daytime",
                                     "--force", "--force-anyway"])
    assert knowledge_ctx.main() == 0

    rc, _ = _run_check(knowledge_ctx, monkeypatch, capsys)
    assert rc == 0, "the workspace's own manifest is current after the re-render"

    rc, out = _run_check_record(knowledge_ctx, monkeypatch, capsys, record_path)
    assert rc == 1, "the session's own baseline is still behind"
    assert out.strip().splitlines() == [f"~ instruction-block {block_id}"]


def test_check_record_exits_0_when_the_record_matches_current(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    product_dir, _ = _render_for_check(knowledge_ctx, tmp_path, monkeypatch)
    manifest_path = product_dir / ".claude" / ".profile-documents.json"
    record_path = tmp_path / "session-baseline.json"
    record_path.write_text(manifest_path.read_text())

    rc, out = _run_check_record(knowledge_ctx, monkeypatch, capsys, record_path)
    assert rc == 0
    assert "is current" in out


def test_check_record_exits_cannot_tell_on_a_missing_record_file(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    _render_for_check(knowledge_ctx, tmp_path, monkeypatch)
    rc, _ = _run_check_record(knowledge_ctx, monkeypatch, capsys, tmp_path / "no-such-file.json")
    assert rc == knowledge_ctx.CHECK_CANNOT_TELL


def test_check_without_record_still_reads_the_workspace_manifest(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """Without `--record`, behaviour is unchanged from slice 1: the default
    path is still cwd's own `.claude/.profile-documents.json`."""
    _render_for_check(knowledge_ctx, tmp_path, monkeypatch)
    rc, out = _run_check(knowledge_ctx, monkeypatch, capsys)
    assert rc == 0
    assert "agent-platform/daytime" in out


# --- a dispatched agent renders from the Library's bundle ------------------
# (issue_01M2MAP3X0Q1BQW06AV8204CFW, platform/decisions/0085)
#
# The Library is faked at `_library_open`, the one place the file touches the
# network, so no test here can reach the live service.

_BUNDLE_TOKEN = "test-bosun-token-not-real"


def _bdoc(doc_id: str, kind: str, body: str, **extra) -> dict:
    return {"id": doc_id, "source_ref": f"projects/{doc_id}.md", "kind": kind,
            "project": doc_id.split("/")[0], "title": doc_id.rsplit("/", 1)[-1],
            "body": body, "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
            "status": "current", **extra}


class _Library:
    """Scripted Library. `issue` answers `POST /bundles`; `profiles` maps
    `(project, variant)` to what `GET /profiles/{project}/{variant}` returns; `docs`
    holds the documents a bundle redeems and `GET /reference/documents/{id}` serves.

    `profile` is the document list the Library's selection rule resolves the
    default `deck/daytime` profile to. The bodies are served from the same
    documents, so a profile and its bodies can never disagree in a test by accident.

    `reference_only` adds documents to the `/reference/documents` listing
    (`all_agent_and_skill_ids`, CQ·371) and to the single-document GET,
    WITHOUT adding them to `self.docs` — a bundle redeem must never see them,
    since `fetch_bundle`'s own guard refuses a bundle carrying any document
    beyond the one agent it was issued for.

    Every request is appended to `calls` as (path, body, headers); a GET has no body."""

    def __init__(self, knowledge_ctx, monkeypatch, tmp_path, docs=None, issue=None,
                 profile=None, profiles=None, reference_only=None):
        self.calls: list[tuple[str, dict | None, dict]] = []
        self.docs = docs if docs is not None else []
        self.issue = issue or (lambda body: (201, {"token_id": "tok-1"}))
        resolved = profile if profile is not None else []
        self.profiles = profiles if profiles is not None else {("deck", "daytime"): resolved}
        self.bodies = {d["id"]: d for d in [*self.docs, *(reference_only or []),
                                            *(d for ds in self.profiles.values()
                                              for d in ds)]}
        tokens = tmp_path / "client-tokens.json"
        tokens.write_text(json.dumps({_BUNDLE_TOKEN: {"name": "bosun", "products": ["*"]},
                                      "other-token": {"name": "deck", "products": ["*"]}}))
        monkeypatch.setattr(knowledge_ctx, "LIBRARY_CLIENT_TOKENS", tokens)
        monkeypatch.setattr(knowledge_ctx, "_library_open", self._open)

    def _open(self, req):
        parsed = urllib.parse.urlparse(req.full_url)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        body = json.loads(req.data) if req.data is not None else None
        self.calls.append((path, body, dict(req.header_items())))
        status, payload = self._answer(req.get_method(), path, body, query)
        raw = json.dumps(payload).encode()
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "err", {}, io.BytesIO(raw))
        return _Response(raw)

    def _answer(self, method, path, body, query=None):
        query = query or {}
        if method == "GET" and path == "/profiles":
            return 200, {"profiles": [{"project": p, "variant": v} for p, v in self.profiles],
                         "count": len(self.profiles)}
        if method == "GET" and path.startswith("/profiles/"):
            _, _, project, variant = path.split("/", 3)
            if (project, variant) not in self.profiles:
                return 404, {"error": "not found"}
            return 200, _profile_payload(project, variant, self.profiles[(project, variant)])
        if method == "GET" and path == "/reference/documents":
            # `all_agent_and_skill_ids`'s listing (CQ·371): filtered by `kind`,
            # paginated the same `limit`/`offset`/`truncated` shape the real
            # route serves, over every document this instance knows (bundle
            # docs and profile docs alike — the fake Library is one corpus).
            kind = query.get("kind", [None])[0]
            rows = sorted((d for d in self.bodies.values()
                          if kind is None or d.get("kind") == kind),
                         key=lambda d: d["id"])
            limit = int(query.get("limit", [str(len(rows) or 1)])[0])
            offset = int(query.get("offset", ["0"])[0])
            page = rows[offset:offset + limit]
            listing = [{k: v for k, v in d.items() if k != "body"} for d in page]
            return 200, {"documents": listing, "count": len(listing), "total": len(rows),
                        "offset": offset, "truncated": offset + len(page) < len(rows)}
        if method == "GET" and path.startswith("/reference/documents/"):
            doc_id = urllib.parse.unquote(path[len("/reference/documents/"):])
            if doc_id not in self.bodies:
                return 404, {"error": "not found"}
            return 200, self.bodies[doc_id]
        if path == "/bundles":
            return self.issue(body)
        return 200, {"token_id": body["token_id"], "agent": "x",
                     "scope": {"kind": "repo"}, "documents": self.docs,
                     "count": len(self.docs)}


def _profile_payload(project, variant, docs):
    """`GET /profiles/{project}/{variant}`'s shape: the documents WITHOUT bodies,
    one match count per rule, and the profile's CLAUDE.md extras."""
    listing = [{k: v for k, v in d.items() if k != "body"} for d in docs]
    return {"profile": {"project": project, "variant": variant,
                        "updated_at": "2026-10-04", "updated_by": "test"},
            "documents": listing, "count": len(listing),
            "rules": [{"position": 0, "matches": len(listing)}],
            "claude_md_extras": []}


class _Response:
    def __init__(self, raw):
        self._raw = raw

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _agent_doc(agent="platform-coder", body="Do the work.\n"):
    return _bdoc(f"agent-platform/agents/{agent}", "agent", body,
                 model="sonnet", tools=["Read", "Edit"], trigger="use for coding")


def _skill_doc(name="pytest-discipline", body="# Tests\n\nWrite them.\n"):
    return _bdoc(f"engineering-practice/skills/{name}", "skill", body)


def test_fetch_bundle_repo_scope_speaks_the_protocol_and_redeems_the_issued_token(
        knowledge_ctx, tmp_path, monkeypatch):
    lib = _Library(knowledge_ctx, monkeypatch, tmp_path, docs=[_agent_doc()])
    docs, scope, note = knowledge_ctx.fetch_bundle(
        "platform-coder", repo="agent-platform", variant="daytime")
    assert [c[0] for c in lib.calls] == ["/bundles", "/bundles/redeem"]
    assert lib.calls[0][1] == {"agent": "platform-coder", "repo": "agent-platform",
                               "variant": "daytime"}
    assert lib.calls[1][1] == {"token_id": "tok-1"}
    for _, _, headers in lib.calls:
        assert headers["Library-protocol"] == "1"
        # the credential is the `bosun` entry's KEY, not another client's
        assert headers["Authorization"] == f"Bearer {_BUNDLE_TOKEN}"
    assert [d["id"] for d in docs] == ["agent-platform/agents/platform-coder"]
    assert "agent+repo" in note


def test_fetch_bundle_scopes_to_the_item_when_the_dispatch_names_one(
        knowledge_ctx, tmp_path, monkeypatch):
    lib = _Library(knowledge_ctx, monkeypatch, tmp_path, docs=[_agent_doc()])
    _, _, note = knowledge_ctx.fetch_bundle(
        "platform-coder", repo="agent-platform", variant="daytime", item_id="issue_X")
    issue_calls = [c for c in lib.calls if c[0] == "/bundles"]
    assert len(issue_calls) == 1
    assert issue_calls[0][1] == {"agent": "platform-coder", "item_id": "issue_X",
                                 "variant": "daytime"}
    assert "agent+item (issue_X)" in note


def test_an_item_whose_product_has_no_profile_falls_back_to_the_repo_scope(
        knowledge_ctx, tmp_path, monkeypatch):
    """Measured against the live Library: `item_id` for a `bosun` item is a 422
    `no profile bosun/daytime`. Without this fall-through every dispatch for a
    bosun item would be refused."""
    def issue(body):
        if "item_id" in body:
            return 422, {"error": "no profile bosun/daytime"}
        return 201, {"token_id": "tok-2"}
    lib = _Library(knowledge_ctx, monkeypatch, tmp_path, docs=[_agent_doc()], issue=issue)
    _, _, note = knowledge_ctx.fetch_bundle(
        "platform-coder", repo="agent-platform", variant="daytime", item_id="issue_X")
    issue_calls = [c[1] for c in lib.calls if c[0] == "/bundles"]
    assert issue_calls == [
        {"agent": "platform-coder", "item_id": "issue_X", "variant": "daytime"},
        {"agent": "platform-coder", "repo": "agent-platform", "variant": "daytime"}]
    assert "agent+repo" in note and "no profile bosun/daytime" in note


@pytest.mark.parametrize("status,error", [
    (409, "no profile bosun/daytime"),      # right words, wrong status
    (422, "agent 'ghost' is not on the roster"),   # right status, wrong words
])
def test_only_the_no_profile_422_falls_back_to_the_repo_scope(
        knowledge_ctx, tmp_path, monkeypatch, status, error):
    lib = _Library(knowledge_ctx, monkeypatch, tmp_path,
                   issue=lambda body: (status, {"error": error}))
    with pytest.raises(knowledge_ctx.BundleRefused) as ei:
        knowledge_ctx.fetch_bundle("ghost", repo="agent-platform", variant="daytime",
                                   item_id="issue_X")
    assert ei.value.status == status
    assert len([c for c in lib.calls if c[0] == "/bundles"]) == 1


def test_an_unreachable_library_refuses_and_names_it(knowledge_ctx, tmp_path, monkeypatch):
    _Library(knowledge_ctx, monkeypatch, tmp_path)

    def down(req):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(knowledge_ctx, "_library_open", down)
    with pytest.raises(knowledge_ctx.BundleRefused) as ei:
        knowledge_ctx.fetch_bundle("platform-coder", repo="agent-platform", variant="daytime")
    assert "the Library" in str(ei.value) and "unreachable" in str(ei.value)
    assert _BUNDLE_TOKEN not in str(ei.value)


# --- what a refusal says (issue_01M35DC5TJY5ZXM8MRZKPAWJDD) ---
#
# Two dispatches on 2026-09-22 hit a 15s client timeout while the Library was
# up and healthy but busy rendering another bundle; the refusal blamed a
# restart it never checked for, and its "retry in a minute" advice did not
# clear the contention. These pin that a timeout is reported as a timeout (not
# a guessed cause), that urlopen's two timeout shapes both read the same way,
# that a refused connection reads differently from a timeout, and that the
# client actually waits the full 60s before giving up.

@pytest.mark.parametrize("timeout_exc", [
    TimeoutError("timed out"),
    urllib.error.URLError(TimeoutError("timed out")),
], ids=["bare-TimeoutError", "URLError-wrapping-TimeoutError"])
def test_a_timeout_reports_the_wait_not_a_guessed_restart(
        knowledge_ctx, tmp_path, monkeypatch, timeout_exc):
    _Library(knowledge_ctx, monkeypatch, tmp_path)

    def slow(req):
        raise timeout_exc
    monkeypatch.setattr(knowledge_ctx, "_library_open", slow)
    with pytest.raises(knowledge_ctx.BundleRefused) as ei:
        knowledge_ctx.fetch_bundle("platform-coder", repo="agent-platform", variant="daytime")
    msg = str(ei.value)
    assert "did not answer POST /bundles within 60s" in msg
    assert "restart" not in msg
    assert "retry in a minute" not in msg


@pytest.mark.parametrize("refused_exc", [
    ConnectionRefusedError(61, "Connection refused"),
    urllib.error.URLError(ConnectionRefusedError(61, "Connection refused")),
], ids=["bare-ConnectionRefusedError", "URLError-wrapping-ConnectionRefusedError"])
def test_a_refused_connection_says_nothing_is_listening_not_a_timeout(
        knowledge_ctx, tmp_path, monkeypatch, refused_exc):
    _Library(knowledge_ctx, monkeypatch, tmp_path)

    def down(req):
        raise refused_exc
    monkeypatch.setattr(knowledge_ctx, "_library_open", down)
    with pytest.raises(knowledge_ctx.BundleRefused) as ei:
        knowledge_ctx.fetch_bundle("platform-coder", repo="agent-platform", variant="daytime")
    msg = str(ei.value)
    assert "nothing is listening at the Library's address" in msg
    assert "restart" not in msg
    assert "did not answer" not in msg


def test_library_open_waits_the_full_60s_before_giving_up(knowledge_ctx, monkeypatch):
    """Pins the timeout constant itself and that `_library_open` actually
    passes it to `urlopen` — the two can drift apart if one is edited and not
    the other, and the refusal message above hardcodes 60s from the constant,
    not from what urlopen was actually told."""
    assert knowledge_ctx.LIBRARY_TIMEOUT_SECONDS == 60
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["timeout"] = timeout
        return _Response(b"{}")
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    req = urllib.request.Request("http://127.0.0.1:8100/bundles", data=b"{}", method="POST")
    knowledge_ctx._library_open(req)
    assert seen["timeout"] == 60


def test_a_missing_credential_refuses_without_printing_any_token(
        knowledge_ctx, tmp_path, monkeypatch):
    lib = _Library(knowledge_ctx, monkeypatch, tmp_path)
    knowledge_ctx.LIBRARY_CLIENT_TOKENS.write_text(
        json.dumps({"other-token": {"name": "deck", "products": ["*"]}}))
    with pytest.raises(knowledge_ctx.BundleRefused) as ei:
        knowledge_ctx.fetch_bundle("platform-coder", repo="agent-platform", variant="daytime")
    assert "bosun" in str(ei.value) and "other-token" not in str(ei.value)
    assert lib.calls == []


def test_the_bundle_request_goes_to_the_librarys_own_address(
        knowledge_ctx, tmp_path, monkeypatch):
    """Addressed through `vault_plane.library_url` (the sweep in
    test_vault_plane forbids composing a base URL anywhere else), so the request
    lands on the Library's `/bundles` route on the configured Library address."""
    seen = []
    lib = _Library(knowledge_ctx, monkeypatch, tmp_path, docs=[_agent_doc()])
    real_open = lib._open
    monkeypatch.setattr(knowledge_ctx, "_library_open",
                        lambda req: seen.append(req.full_url) or real_open(req))
    knowledge_ctx.fetch_bundle("platform-coder", repo="agent-platform", variant="daytime")
    import vault_plane
    assert seen == [vault_plane.library_url("/bundles"),
                    vault_plane.library_url("/bundles/redeem")]


def test_bundle_rows_refuses_a_body_that_does_not_match_its_hash(knowledge_ctx):
    doc = _agent_doc()
    doc["body"] = "tampered\n"
    with pytest.raises(knowledge_ctx.BundleRefused, match="body_sha256"):
        knowledge_ctx.bundle_rows([doc], "platform-coder")


def test_bundle_rows_refuses_another_agents_definition(knowledge_ctx):
    with pytest.raises(knowledge_ctx.BundleRefused, match="another agent"):
        knowledge_ctx.bundle_rows(
            [_agent_doc(), _agent_doc("reviewer")], "platform-coder")


def test_bundle_rows_refuses_a_bundle_without_the_agents_own_definition(knowledge_ctx):
    with pytest.raises(knowledge_ctx.BundleRefused, match="0 agent definitions"):
        knowledge_ctx.bundle_rows([_skill_doc()], "platform-coder")


def _index_with_skill_trigger(db, skill_id, body, trigger, hash_of=None):
    schema = (pathlib.Path(__file__).resolve().parent.parent
              / "lib" / "knowledge" / "schema.sql").read_text()
    conn = sqlite3.connect(db)
    conn.executescript(schema)
    conn.execute(
        "INSERT INTO docs (id, source, source_ref, kind, project, title, status, "
        "body, body_hash, updated_at, frontmatter_extra) VALUES "
        "(?, 'files', 'x.md', 'skill', 'engineering-practice', 't', 'current', "
        "?, ?, '2026-09-20', ?)",
        (skill_id, body, hashlib.sha256((hash_of or body).encode()).hexdigest(),
         json.dumps({"trigger": trigger})))
    conn.commit()
    conn.close()


def test_a_skills_trigger_comes_from_the_library_document(knowledge_ctx, tmp_path, monkeypatch):
    """The Library serves `trigger` on a skill document, and that is the only source:
    the local index is not read for it, so an absent index changes nothing. The
    trigger is the `description:` Claude Code fires a skill on."""
    skill = _bdoc("engineering-practice/skills/pytest-discipline", "skill",
                  "# Tests\n\nWrite them.\n", trigger="when writing tests")
    monkeypatch.setattr(knowledge_ctx, "DB_PATH", tmp_path / "absent.db")
    rows, untriggered = knowledge_ctx.bundle_rows([_agent_doc(), skill], "platform-coder")
    assert untriggered == []
    row = next(r for r in rows if r["kind"] == "skill")
    assert row["_bundle"]["fm"] == {"trigger": "when writing tests"}
    assert "description: when writing tests" in knowledge_ctx._skill_md(
        row, row["_bundle"]["fm"], row["_bundle"]["body"])


@pytest.mark.parametrize("how", ["missing", "unopenable"])
def test_an_unusable_index_degrades_rather_than_burning_the_redeemed_token(
        knowledge_ctx, tmp_path, monkeypatch, how):
    """This function runs AFTER the Library was called and its single-use token
    redeemed, so anything that raises here spends the token and exits with the
    generic crash code instead of a named refusal — and the token cannot be
    redeemed again.

    TWO WAYS TO FAIL AND THE FIRST FIX CAUGHT ONE. `_conn` raises `SystemExit`
    via `die()` when the index is absent, which was handled; it then calls
    `sqlite3.connect`, which raises `OperationalError` for an index that EXISTS
    and cannot be opened — unreadable, corrupt, or lock-refused — and that
    escaped (reviewer, #630, round 2). The `unopenable` row is that case, and it
    fails against the narrow catch.

    A skill with no trigger degrades to a body-derived description, which is
    what this function already returns for a row it declines.
    """
    db = tmp_path / "index.db"
    if how == "unopenable":
        db.write_text("this is not a database")
        os.chmod(db, 0o000)
    monkeypatch.setattr(knowledge_ctx, "DB_PATH", db)
    skill = _skill_doc()
    rows, untriggered = knowledge_ctx.bundle_rows([_agent_doc(), skill], "platform-coder")
    row = next(r for r in rows if r["kind"] == "skill")
    assert row["_bundle"]["fm"] == {"trigger": None}
    assert untriggered == [skill["id"]], "and the degrade is still recorded"


def test_a_local_index_trigger_is_never_read_for_a_bundle_skill(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """The index holds a trigger for this skill with a matching body hash. A bundle
    skill that the Library left without one must NOT pick it up: the index is no
    longer a source of triggers, so the skill is recorded as untriggered."""
    skill = _skill_doc()
    db = tmp_path / "index.db"
    _index_with_skill_trigger(db, skill["id"], skill["body"], "when writing tests")
    monkeypatch.setattr(knowledge_ctx, "DB_PATH", db)
    rows, untriggered = knowledge_ctx.bundle_rows([_agent_doc(), skill], "platform-coder")
    row = next(r for r in rows if r["kind"] == "skill")
    assert row["_bundle"]["fm"] == {"trigger": None}
    assert "no trigger for skill" in capsys.readouterr().err
    # The stderr line above is discarded by `activate_profile` on the success
    # path, so the RETURNED list is the only thing that makes this observable.
    assert untriggered == [skill["id"]]


# A trigger the Library serves is display text that becomes a `description:`
# every session loads, so bundle_rows bounds it the same way the index path
# does. Each case names the shape it bounds; the expected value is written out
# rather than computed from `_bounded_trigger`, so the test cannot agree with a
# broken bound.

def _bundle_doc_with_trigger(kind, trigger):
    if kind == "agent":
        return [_bdoc("agent-platform/agents/platform-coder", "agent", "Do the work.\n",
                      trigger=trigger)]
    return [_agent_doc(), _bdoc("engineering-practice/skills/pytest-discipline", "skill",
                                "# Tests\n\nWrite them.\n", trigger=trigger)]


@pytest.mark.parametrize("kind", ["skill", "agent"])
@pytest.mark.parametrize("raw,expected", [
    ("when writing\ntests", "when writing tests"),
    ("when writing\x07tests", "when writingtests"),
    ("x" * 600, "x" * 500),
], ids=["newline", "control-char", "over-500-chars"])
def test_a_bundle_trigger_is_bounded_before_it_renders(
        knowledge_ctx, tmp_path, monkeypatch, kind, raw, expected):
    monkeypatch.setattr(knowledge_ctx, "DB_PATH", tmp_path / "absent.db")
    rows, _ = knowledge_ctx.bundle_rows(_bundle_doc_with_trigger(kind, raw), "platform-coder")
    row = next(r for r in rows if r["kind"] == kind)
    assert row["_bundle"]["fm"]["trigger"] == expected
    render = knowledge_ctx._skill_md if kind == "skill" else knowledge_ctx._agent_md
    assert f"description: {expected}\n" in render(
        row, row["_bundle"]["fm"], row["_bundle"]["body"])


@pytest.mark.parametrize("kind", ["skill", "agent"])
def test_a_clean_bundle_trigger_renders_unchanged(knowledge_ctx, tmp_path, monkeypatch, kind):
    monkeypatch.setattr(knowledge_ctx, "DB_PATH", tmp_path / "absent.db")
    rows, _ = knowledge_ctx.bundle_rows(
        _bundle_doc_with_trigger(kind, "when writing tests"), "platform-coder")
    row = next(r for r in rows if r["kind"] == kind)
    assert row["_bundle"]["fm"]["trigger"] == "when writing tests"
    render = knowledge_ctx._skill_md if kind == "skill" else knowledge_ctx._agent_md
    assert "description: when writing tests\n" in render(
        row, row["_bundle"]["fm"], row["_bundle"]["body"])


def test_a_bundle_trigger_that_bounds_to_nothing_is_recorded_as_untriggered(
        knowledge_ctx, tmp_path, monkeypatch):
    """A Library trigger of control characters only bounds to nothing. The skill
    renders from its body and is recorded as untriggered; the index, which holds a
    trigger, is not consulted."""
    skill = _bdoc("engineering-practice/skills/pytest-discipline", "skill",
                  "# Tests\n\nWrite them.\n", trigger="\x07\n")
    db = tmp_path / "index.db"
    _index_with_skill_trigger(db, skill["id"], skill["body"], "when writing tests")
    monkeypatch.setattr(knowledge_ctx, "DB_PATH", db)
    rows, untriggered = knowledge_ctx.bundle_rows([_agent_doc(), skill], "platform-coder")
    row = next(r for r in rows if r["kind"] == "skill")
    assert row["_bundle"]["fm"] == {"trigger": None}
    assert untriggered == [skill["id"]]


# With no trigger, `description:` is derived from the body, and derive_description
# returns the title when the body has no prose line. Every source of the
# description passes the bound, so a headings-only body cannot carry a raw title.

def _headings_only_bundle(kind, title):
    if kind == "agent":
        return [_bdoc("agent-platform/agents/platform-coder", "agent", "# Role\n\n# Rules\n",
                      title=title)]
    return [_agent_doc(), _bdoc("engineering-practice/skills/pytest-discipline", "skill",
                                "# Tests\n\n# Rules\n", title=title)]


@pytest.mark.parametrize("kind", ["skill", "agent"])
@pytest.mark.parametrize("title,expected", [
    ("Pytest\ndiscipline rules", "Pytest discipline rules"),
    ("Pytest\x07 rules", "Pytest rules"),
], ids=["newline", "control-char"])
def test_a_headings_only_title_is_bounded_before_it_renders(
        knowledge_ctx, tmp_path, monkeypatch, kind, title, expected):
    monkeypatch.setattr(knowledge_ctx, "DB_PATH", tmp_path / "absent.db")
    rows, _ = knowledge_ctx.bundle_rows(_headings_only_bundle(kind, title), "platform-coder")
    row = next(r for r in rows if r["kind"] == kind)
    render = knowledge_ctx._skill_md if kind == "skill" else knowledge_ctx._agent_md
    fm, _ = knowledge_ctx.split_frontmatter(
        render(row, row["_bundle"]["fm"], row["_bundle"]["body"]))
    assert fm["description"] == expected


@pytest.mark.parametrize("kind,slug", [
    ("skill", "pytest-discipline"),
    ("agent", "platform-coder"),
])
def test_a_title_that_bounds_to_nothing_renders_the_slug_as_its_description(
        knowledge_ctx, tmp_path, monkeypatch, kind, slug):
    monkeypatch.setattr(knowledge_ctx, "DB_PATH", tmp_path / "absent.db")
    rows, _ = knowledge_ctx.bundle_rows(_headings_only_bundle(kind, "\x07\n"), "platform-coder")
    row = next(r for r in rows if r["kind"] == kind)
    render = knowledge_ctx._skill_md if kind == "skill" else knowledge_ctx._agent_md
    fm, _ = knowledge_ctx.split_frontmatter(
        render(row, row["_bundle"]["fm"], row["_bundle"]["body"]))
    assert fm["description"] == slug


def test_an_agent_with_no_bundle_trigger_renders_its_body_derived_first_sentence(
        knowledge_ctx, tmp_path, monkeypatch):
    monkeypatch.setattr(knowledge_ctx, "DB_PATH", tmp_path / "absent.db")
    doc = _bdoc("agent-platform/agents/platform-coder", "agent",
                "# Role\n\nYou do the work. Then you stop.\n")
    rows, _ = knowledge_ctx.bundle_rows([doc], "platform-coder")
    fm, _ = knowledge_ctx.split_frontmatter(knowledge_ctx._agent_md(
        rows[0], rows[0]["_bundle"]["fm"], rows[0]["_bundle"]["body"]))
    assert fm["description"] == "You do the work."


# ---- through main() --------------------------------------------------------


def _linked_worktree(tmp_path) -> pathlib.Path:
    """`convivy-deck` as a LINKED worktree — the tree `agentctl spawn` owns,
    where `.git` is a file — beside a primary checkout with a real commit."""
    primary = tmp_path / "convivy-deck"
    primary.mkdir()
    _git_init(primary)
    (primary / "README").write_text("x\n")
    subprocess.run(["git", "add", "README"], cwd=primary, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=primary, check=True)
    wt = tmp_path / "worktrees" / "convivy-deck.spawn"
    wt.parent.mkdir()
    subprocess.run(["git", "worktree", "add", "-q", "--detach", str(wt)],
                   cwd=primary, check=True)
    assert (wt / ".git").is_file()
    return wt


def _seed_render_env(knowledge_ctx, tmp_path, monkeypatch, index_rows):
    """Profile YAML + a real index holding `index_rows` (id, kind) agent/skill
    documents, and the trust switches an orphan-detecting render needs."""
    (tmp_path / "knowledge" / "projects" / "deck").mkdir(parents=True)
    db = tmp_path / "index.db"
    schema = (pathlib.Path(__file__).resolve().parent.parent
              / "lib" / "knowledge" / "schema.sql").read_text()
    conn = sqlite3.connect(db)
    conn.executescript(schema)
    for doc_id, kind in index_rows:
        conn.execute(
            "INSERT INTO docs (id, source, source_ref, kind, project, title, status, "
            "body, body_hash, updated_at) VALUES (?, 'files', ?, ?, 'deck', 't', "
            "'current', 'b', 'h', '2026-09-20')", (doc_id, f"{doc_id}.md", kind))
    conn.commit()
    conn.close()
    _declare_agent_roles(db, {i: _DECLARED_ROLES[i] for i, k in index_rows
                              if k == "agent" and i in _DECLARED_ROLES})
    monkeypatch.setattr(knowledge_ctx, "CONVIVY", tmp_path)
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", tmp_path / "knowledge")
    monkeypatch.setattr(knowledge_ctx, "DB_PATH", db)
    monkeypatch.setattr(knowledge_ctx, "_DEFAULT_DB_PATH", db)


_ELEVEN_OTHERS = [(f"deck/agents/other-{i}", "agent") for i in range(3)] + \
                 [(f"engineering-practice/skills/wide-{i}", "skill") for i in range(8)]


def _prior_profile_render(wt, ids_kinds):
    """The wide render a previous profile-based individuation left."""
    claude = wt / ".claude"
    written = []
    for doc_id, kind in ids_kinds:
        slug = doc_id.rsplit("/", 1)[-1]
        rel = f"agents/{slug}.md" if kind == "agent" else f"skills/{slug}/SKILL.md"
        p = claude / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("---\nname: %s\n---\n\nold wide render\n" % slug)
        written.append(rel)
    (claude / ".materialized").write_text("\n".join(sorted(written)) + "\n")
    return [claude / r for r in written]


def _run_main(knowledge_ctx, monkeypatch, cwd, argv):
    monkeypatch.chdir(cwd)
    monkeypatch.setattr("sys.argv", ["knowledge-ctx", "use", "daytime", *argv])
    try:
        return knowledge_ctx.main()
    except SystemExit as e:
        return e.code


def _own_docs():
    return [_agent_doc("deck-coder"), _skill_doc("skill-0"), _skill_doc("skill-1")]


def test_an_agent_render_writes_only_the_bundle_and_never_reads_the_checkout(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    wt = _linked_worktree(tmp_path)
    _seed_render_env(knowledge_ctx, tmp_path, monkeypatch, _ELEVEN_OTHERS + [
        ("agent-platform/agents/deck-coder", "agent")])
    lib = _Library(knowledge_ctx, monkeypatch, tmp_path, docs=_own_docs())
    rc = _run_main(knowledge_ctx, monkeypatch, wt,
                   ["--agent", "deck-coder", "--force", "--force-anyway"])
    assert rc == 0
    rendered = sorted(str(p.relative_to(wt / ".claude"))
                      for p in (wt / ".claude").rglob("*") if p.is_file())
    assert "agents/deck-coder.md" in rendered
    assert "skills/skill-0/SKILL.md" in rendered and "skills/skill-1/SKILL.md" in rendered
    assert not [r for r in rendered if r.startswith("agents/other")]
    assert not (wt / ".claude" / "skills" / "wide-2").exists()
    # the agent definition is rendered from the BUNDLE's fields
    agent_md = (wt / ".claude" / "agents" / "deck-coder.md").read_text()
    assert "model: sonnet" in agent_md and "description: use for coding" in agent_md
    # `source_ref` files do not exist under the tmp KNOWLEDGE: a disk read would warn
    assert "source missing" not in capsys.readouterr().err
    record = json.loads((wt / ".claude" / ".profile-documents.json").read_text())
    assert [d["id"] for d in record["documents"]] == sorted(d["id"] for d in _own_docs())
    assert len([c for c in lib.calls if c[0] == "/bundles"]) == 1


def test_use_with_an_agent_does_not_double_deliver_a_bound_block(
        knowledge_ctx, tmp_path, monkeypatch):
    """The `--agent` dispatch path already composes a `binds_roles` block
    into its OWN `CLAUDE.md` through the top-level `resolve_instruction_blocks`
    call (verified after PR #635). This fix must never ALSO append that
    same block to the dispatched agent's own individuated definition file —
    that would deliver the same rule twice into the same run. Only a
    crew-session render (no `--agent`) gets the new per-agent section
    (issue_01M35B8Q926KNB53X4050T9V5F, constraint 1)."""
    wt = _linked_worktree(tmp_path)
    _seed_render_env(knowledge_ctx, tmp_path, monkeypatch, _ELEVEN_OTHERS + [
        ("deck/agents/deck-coder", "agent")])
    # `deck-coder`'s document declares `role: coder`, so
    # `binds_roles: ["coder"]` is what reaches it.
    _insert_instruction_block_row(
        tmp_path / "index.db", "platform/instruction-blocks/review-a-pull-request",
        "Do not run the test suite.\n", {"scope": "agent", "binds_roles": ["coder"]})
    _Library(knowledge_ctx, monkeypatch, tmp_path, docs=_own_docs())

    rc = _run_main(knowledge_ctx, monkeypatch, wt,
                   ["--agent", "deck-coder", "--force", "--force-anyway"])
    assert rc == 0
    block_id = "platform/instruction-blocks/review-a-pull-request"
    claude_md = (wt / ".claude" / "CLAUDE.md").read_text()
    assert block_id in claude_md
    agent_md = (wt / ".claude" / "agents" / "deck-coder.md").read_text()
    assert block_id not in agent_md
    assert "instruction blocks bound to this agent" not in agent_md


def test_a_narrowed_force_render_removes_the_wide_renders_and_is_not_refused_by_the_orphan_gate(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """THE HAZARD, worked through. The 11 files a narrowed render stops writing
    all have documents in the Library's listing, so `_find_orphans` does not
    treat them as orphan candidates (it asks whether a document exists
    ANYWHERE, not whether this render selected it) and `known_ids` staying
    the whole corpus (`all_agent_and_skill_ids`, read from the Library since
    CQ·371) is what keeps it that way. So the orphan gate neither refuses the
    narrowed render nor names them. `--force` then clears them as ordinary
    rewrite, which is what a spawn worktree needs (another agent's
    definition must not survive into this run). A file with NO document
    anywhere still goes through the orphan machinery in the same render."""
    wt = _linked_worktree(tmp_path)
    _seed_render_env(knowledge_ctx, tmp_path, monkeypatch, _ELEVEN_OTHERS + [
        ("agent-platform/agents/deck-coder", "agent"),
        ("engineering-practice/skills/skill-0", "skill"),
        ("engineering-practice/skills/skill-1", "skill")])
    (tmp_path / "knowledge" / "projects").mkdir(exist_ok=True)
    wide = _prior_profile_render(wt, _ELEVEN_OTHERS)
    ghost = wt / ".claude" / "skills" / "start-overnight" / "SKILL.md"
    ghost.parent.mkdir(parents=True)
    ghost.write_text("retired\n")
    manifest = wt / ".claude" / ".materialized"
    manifest.write_text(manifest.read_text() + "skills/start-overnight/SKILL.md\n")
    _Library(knowledge_ctx, monkeypatch, tmp_path, docs=_own_docs(), reference_only=[
        _bdoc(doc_id, kind, "") for doc_id, kind in _ELEVEN_OTHERS])

    rc = _run_main(knowledge_ctx, monkeypatch, wt,
                   ["--agent", "deck-coder", "--force", "--force-anyway"])
    err = capsys.readouterr().err
    assert rc == 0
    assert not any(p.exists() for p in wide)
    # the 11 are not reported as orphans: they have documents
    assert not any(str(p) in err for p in wide)
    # the one with none still is, and is removed
    assert str(ghost) in err and "orphan (no source document anywhere)" in err
    assert not ghost.exists()


def test_a_narrowed_render_without_force_refuses_over_a_wide_one_and_deletes_nothing(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """Without `--force` (a human at a terminal, not spawn) the manifest a wide
    render left differs from the narrowed plan's, which is the ordinary
    conflict abort. Nothing is removed."""
    wt = _linked_worktree(tmp_path)
    _seed_render_env(knowledge_ctx, tmp_path, monkeypatch, _ELEVEN_OTHERS)
    wide = _prior_profile_render(wt, _ELEVEN_OTHERS)
    _Library(knowledge_ctx, monkeypatch, tmp_path, docs=_own_docs())
    rc = _run_main(knowledge_ctx, monkeypatch, wt, ["--agent", "deck-coder", "--force-anyway"])
    assert rc == 3
    assert all(p.exists() for p in wide)
    assert "conflicting files" in capsys.readouterr().err


def test_a_refused_bundle_writes_nothing_and_does_not_fall_back_to_the_index(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    wt = _linked_worktree(tmp_path)
    _seed_render_env(knowledge_ctx, tmp_path, monkeypatch, _ELEVEN_OTHERS)
    wide = _prior_profile_render(wt, _ELEVEN_OTHERS)
    _Library(knowledge_ctx, monkeypatch, tmp_path)

    def down(req):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(knowledge_ctx, "_library_open", down)

    def no_fallback(profile):
        raise AssertionError("fell back to the local index")
    monkeypatch.setattr(knowledge_ctx, "collect_matches", no_fallback)

    rc = _run_main(knowledge_ctx, monkeypatch, wt,
                   ["--agent", "deck-coder", "--force", "--force-anyway"])
    err = capsys.readouterr().err
    assert rc == knowledge_ctx.LIBRARY_REFUSED
    assert "the Library" in err and "NOTHING WAS WRITTEN" in err
    assert all(p.exists() for p in wide)
    assert not (wt / ".claude" / ".active-profile").exists()


def test_a_crew_session_renders_the_library_profile_and_never_asks_for_a_bundle(
        knowledge_ctx, tmp_path, monkeypatch):
    """A crew session has no `--agent`, so it renders the whole profile. The profile
    comes from the Library, and no bundle is issued for it."""
    wt = _linked_worktree(tmp_path)
    _seed_render_env(knowledge_ctx, tmp_path, monkeypatch, [])
    lib = _Library(knowledge_ctx, monkeypatch, tmp_path)
    used = []
    real = knowledge_ctx.collect_matches
    monkeypatch.setattr(knowledge_ctx, "collect_matches",
                        lambda p: used.append(1) or real(p))
    assert _run_main(knowledge_ctx, monkeypatch, wt, ["--force", "--force-anyway"]) == 0
    assert used == [1]
    assert ("/profiles/deck/daytime" in [c[0] for c in lib.calls])
    assert [c for c in lib.calls if c[0] == "/bundles"] == []


def test_an_agent_render_into_a_primary_checkout_keeps_the_profile_render(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """`planner`/`curator` resolve into the operator's own clone. A narrowed
    `--force` there would delete the sibling agents and skills from under the co
    session working in it. The profile comes from the Library; no bundle is issued."""
    _linked_worktree(tmp_path)
    primary = tmp_path / "convivy-deck"
    assert (primary / ".git").is_dir()
    _seed_render_env(knowledge_ctx, tmp_path, monkeypatch, [])
    lib = _Library(knowledge_ctx, monkeypatch, tmp_path)
    used = []
    real = knowledge_ctx.collect_matches
    monkeypatch.setattr(knowledge_ctx, "collect_matches",
                        lambda p: used.append(1) or real(p))
    rc = _run_main(knowledge_ctx, monkeypatch, primary,
                   ["--agent", "planner", "--force", "--force-anyway"])
    assert rc == 0
    assert used == [1]
    assert [c for c in lib.calls if c[0] == "/bundles"] == []
    assert "not a linked worktree" in capsys.readouterr().err


@pytest.mark.parametrize("what", ["absent", "directory", "not-a-gitdir-file"])
def test_a_tree_that_is_not_provably_a_linked_worktree_keeps_the_profile_render(
        knowledge_ctx, tmp_path, monkeypatch, capsys, what):
    """CANNOT TELL MUST FALL TO THE PROFILE RENDER, and the first version of the
    guard fell the other way.

    It asked `.git`.is_dir() and took the narrowing `--force` bundle render
    whenever that was false — so a tree with NO `.git` at all (a subdirectory of
    a checkout, a plain directory) read as "worktree spawn owns" and had its
    `.claude/agents` and `.claude/skills` cleared. The two errors are not
    symmetric: rendering the wide profile into a worktree wastes a few documents,
    while narrowing into someone's clone deletes their working files.
    `worktrees.is_primary_checkout` states that asymmetry for this same dispatch
    path, and this mirrors it (security-reviewer, #630).

    `not-a-gitdir-file` is the submodule and `--separate-git-dir` shape: a `.git`
    FILE that does not point at a worktree.
    """
    # The PRIMARY that `_linked_worktree` builds, so the render environment is
    # the same one the sibling tests use; only its `.git` varies.
    _linked_worktree(tmp_path)
    tree = tmp_path / "convivy-deck"
    if what == "absent":
        shutil.rmtree(tree / ".git")
    elif what == "not-a-gitdir-file":
        shutil.rmtree(tree / ".git")
        # A SUBMODULE'S REAL POINTER, not invented text. The first version wrote
        # "this is not a gitdir pointer", which no real tree contains, so the row
        # passed while the shape it named stayed accepted (both reviewers, #630).
        (tree / ".git").write_text("gitdir: ../.git/modules/convivy-deck\n")
    # "directory" is the primary as git left it.

    _seed_render_env(knowledge_ctx, tmp_path, monkeypatch, [])
    lib = _Library(knowledge_ctx, monkeypatch, tmp_path)
    used = []
    real = knowledge_ctx.collect_matches
    monkeypatch.setattr(knowledge_ctx, "collect_matches",
                        lambda p: used.append(1) or real(p))

    rc = _run_main(knowledge_ctx, monkeypatch, tree,
                   ["--agent", "planner", "--force", "--force-anyway"])
    assert rc == 0, f"{what}: the profile render must still succeed"
    assert [c for c in lib.calls if c[0] == "/bundles"] == [], f"{what}: no bundle is issued"
    assert used == [1], f"{what}: the profile render must be the one used"
    assert "not a linked worktree" in capsys.readouterr().err


def test_the_worktree_probe_accepts_only_a_linked_worktree(knowledge_ctx, tmp_path):
    """The probe itself, at the unit level, since the cases above go through
    `main` and a future refactor could route around it.

    THE POINTER SHAPES ARE TAKEN FROM REAL GIT, NOT INVENTED. A first version of
    this test wrote "not a pointer" for its negative row, which no real tree
    contains — so it passed while a submodule and a `--separate-git-dir` clone,
    the two shapes the probe's docstring named, were still accepted. Both write
    a `.git` file starting with `gitdir:`; the prefix is git's pointer format
    rather than a worktree marker. Pinning the counterexample I happened to
    write proved nothing about the property (both reviewers, #630).

    Verified against git on 2026-09-21:

        linked worktree      gitdir: /path/repo/.git/worktrees/<name>
        submodule            gitdir: ../.git/modules/<name>
        --separate-git-dir   gitdir: /path/to/anywhere
    """
    wt = tmp_path / "wt"
    wt.mkdir()
    assert knowledge_ctx._is_spawn_owned_worktree(wt) is False       # absent
    (wt / ".git").mkdir()
    assert knowledge_ctx._is_spawn_owned_worktree(wt) is False       # directory
    shutil.rmtree(wt / ".git")

    for label, content in (
        ("not a pointer at all", "not a pointer\n"),
        ("a submodule", "gitdir: ../.git/modules/sub\n"),
        ("a --separate-git-dir clone", "gitdir: /elsewhere/sepdir\n"),
        ("an empty pointer", "gitdir:\n"),
        # A SUBMODULE WHOSE PATH BEGINS `worktrees/`. Built with real git to
        # confirm the pointer shape: its parent directory is ALSO named
        # `worktrees`, so a parent-only test answered True for it — which is
        # why the check requires `.git/worktrees/` (security-reviewer, #630
        # round 3).
        ("a submodule at worktrees/<name>",
         "gitdir: ../../.git/modules/worktrees/foo\n"),
        # The safe-direction miss, pinned so a later change notices it: a linked
        # worktree OF a submodule is rejected. Wrong answer, wide render.
        ("a linked worktree of a submodule",
         "gitdir: /r/.git/modules/sub/worktrees/w\n"),
    ):
        (wt / ".git").write_text(content)
        assert knowledge_ctx._is_spawn_owned_worktree(wt) is False, label

    (wt / ".git").write_bytes(b"\xff\xfe not utf-8\n")
    assert knowledge_ctx._is_spawn_owned_worktree(wt) is False       # undecodable

    (wt / ".git").write_text("gitdir: /somewhere/repo/.git/worktrees/wt\n")
    assert knowledge_ctx._is_spawn_owned_worktree(wt) is True        # the real thing


def test_the_worktree_probe_against_trees_git_itself_built(knowledge_ctx, tmp_path):
    """The shapes above, created by git rather than described by me. This is the
    check that cannot drift from what git actually writes — the previous test
    would still pass if git changed its pointer format, and this one would not.
    """
    primary = tmp_path / "primary"
    primary.mkdir()
    _git_init(primary)
    (primary / "R").write_text("x\n")
    subprocess.run(["git", "add", "R"], cwd=primary, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "i"], cwd=primary, check=True)

    linked = tmp_path / "linked"
    subprocess.run(["git", "worktree", "add", "-q", "--detach", str(linked)],
                   cwd=primary, check=True)
    assert knowledge_ctx._is_spawn_owned_worktree(linked) is True, (
        "a real linked worktree must take the bundle path")
    assert knowledge_ctx._is_spawn_owned_worktree(primary) is False, (
        "the primary must take the whole-profile path")

    sep = tmp_path / "sepclone"
    subprocess.run(["git", "init", "-q", f"--separate-git-dir={tmp_path / 'sepdir'}",
                    str(sep)], check=True)
    assert (sep / ".git").is_file(), "precondition: this shape writes a .git FILE"
    assert (sep / ".git").read_text().startswith("gitdir:"), (
        "precondition: and it starts with the same prefix a worktree does")
    assert knowledge_ctx._is_spawn_owned_worktree(sep) is False, (
        "a --separate-git-dir clone is not spawn-owned; narrowing into one "
        "would clear a human's .claude/")

    # THE SHAPE THAT BEAT THE PARENT-ONLY TEST: a submodule whose PATH begins
    # `worktrees/` lands at `.git/modules/worktrees/<name>`, whose parent
    # directory is also `worktrees`. Built here rather than asserted, because
    # the last two versions of this probe were wrong about content I wrote from
    # memory (security-reviewer, #630 round 3).
    child = tmp_path / "child"
    child.mkdir()
    _git_init(child)
    (child / "S").write_text("y\n")
    subprocess.run(["git", "add", "S"], cwd=child, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "i"], cwd=child, check=True)
    added = subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "submodule", "add", "-q",
         str(child), "worktrees/foo"],
        cwd=primary, capture_output=True, text=True)
    if added.returncode == 0:
        sub = primary / "worktrees" / "foo"
        assert (sub / ".git").is_file()
        assert "worktrees" in (sub / ".git").read_text().rsplit("/", 2)[-2], (
            "precondition: this submodule's pointer parent really is `worktrees`")
        assert knowledge_ctx._is_spawn_owned_worktree(sub) is False, (
            "a submodule at worktrees/<name> is not a linked worktree; the "
            "parent-directory test accepted it and `.git/worktrees/` does not")
    elif os.environ.get("CI", "").strip().lower() not in ("", "0", "false"):
        # ON CI A SKIP IS GREEN, which would silently unpin the one regression
        # this case exists for. A local run may legitimately lack whatever git
        # needs for a file-protocol submodule; CI is a controlled environment,
        # so there the refusal is a broken test rather than an environment fact
        # (reviewer, #630). The CI check treats "", "0" and "false" as not-CI,
        # matching `bin/agentctl`'s own idiom, because a bare truthy read takes
        # CI=false for CI.
        raise AssertionError(
            f"git refused the submodule add on CI, so the "
            f"submodule-at-worktrees/<name> regression went unchecked: "
            f"{added.stderr.strip()[:200]}")
    else:
        pytest.skip(f"git refused the submodule add: {added.stderr.strip()[:120]}")


def test_issue_without_an_agent_is_refused(knowledge_ctx, tmp_path, monkeypatch):
    wt = _linked_worktree(tmp_path)
    _seed_render_env(knowledge_ctx, tmp_path, monkeypatch, [])
    _Library(knowledge_ctx, monkeypatch, tmp_path)
    assert _run_main(knowledge_ctx, monkeypatch, wt, ["--issue", "issue_X"]) == 1


def test_an_agent_render_scopes_the_bundle_to_the_issue_it_was_given(
        knowledge_ctx, tmp_path, monkeypatch):
    wt = _linked_worktree(tmp_path)
    _seed_render_env(knowledge_ctx, tmp_path, monkeypatch, [])
    lib = _Library(knowledge_ctx, monkeypatch, tmp_path, docs=_own_docs())
    # `--issue` also makes `main()` read the item's topics (`session_topics`),
    # a SECOND request through `issue_client.get_item_from_default_library` —
    # a different client than `_Library` above stubs, and unstubbed it reaches
    # the real ops plane (CQ·416). This test's own assertions are about the
    # bundle scope, not binds_topics, so an item with no topics is enough.
    _patch_item_topics(monkeypatch, item={"id": "issue_X"})
    rc = _run_main(knowledge_ctx, monkeypatch, wt,
                   ["--agent", "deck-coder", "--issue", "issue_X", "--force", "--force-anyway"])
    assert rc == 0
    [issued] = [body for path, body, _ in lib.calls if path == "/bundles"]
    assert issued["item_id"] == "issue_X"
    # the project (`deck`) and profile name are what a repo-scope retry would use
    assert issued["variant"] == "daytime"


# --- `knowledge-ctx floor`: the machine-wide floor from fleet blocks ---------

def _real_claude_md_state():
    """(lstat identity, stat identity, content) of the operator's REAL
    ~/.claude/CLAUDE.md, read from the passwd entry so a HOME a test repoints
    cannot redirect it."""
    import pwd
    real = pathlib.Path(pwd.getpwuid(os.getuid()).pw_dir) / ".claude" / "CLAUDE.md"
    try:
        lst = os.lstat(real)
        st = os.stat(real)
        content = real.read_bytes()
    except OSError:
        return None
    return ((lst.st_dev, lst.st_ino), (st.st_dev, st.st_ino), content)


@pytest.fixture
def floor_env(knowledge_ctx, tmp_path, monkeypatch):
    """A sandboxed HOME plus an index of fleet and non-fleet blocks. Every
    floor test runs under it, and its teardown asserts the real
    ~/.claude/CLAUDE.md kept its inode and bytes: the hand-written file must
    survive until the steward's manual cutover, and a test that wrote it would
    drop its rules from every new session."""
    before = _real_claude_md_state()
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    db = tmp_path / "index.db"
    _index_with_instruction_blocks(db)
    monkeypatch.setattr(knowledge_ctx, "DB_PATH", db)
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", tmp_path / "knowledge")
    yield home / ".claude", db
    assert _real_claude_md_state() == before, (
        "a floor test changed the real ~/.claude/CLAUDE.md")


def _run_floor(knowledge_ctx, monkeypatch, *args):
    monkeypatch.setattr("sys.argv", ["knowledge-ctx", "floor", *args])
    return knowledge_ctx.main()


def _add_fleet_block(db, doc_id, body, status="current"):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO docs (id, source, source_ref, kind, project, title, "
        "status, body, body_hash, updated_at, frontmatter_extra) VALUES "
        "(?, 'files', ?, 'instruction-block', 'platform', ?, ?, ?, ?, "
        "'2026-09-24', ?)",
        (doc_id, f"{doc_id}.md", doc_id.rsplit("/", 1)[-1], status, body,
         hashlib.sha256(body.encode()).hexdigest(),
         json.dumps({"scope": "fleet"})))
    conn.commit()
    conn.close()


def test_floor_render_selects_only_fleet_blocks(knowledge_ctx, floor_env, monkeypatch):
    claude, db = floor_env
    _add_fleet_block(db, "platform/instruction-blocks/a-second-fleet-rule",
                     "Another rule.\n")
    out = claude / "CLAUDE.md"
    assert _run_floor(knowledge_ctx, monkeypatch, "--output", str(out)) == 0
    text = out.read_text()
    assert "platform/instruction-blocks/verify-before-you-report" in text
    assert "platform/instruction-blocks/a-second-fleet-rule" in text
    # the three non-fleet blocks, one per selector shape, are all absent
    assert "review-a-pull-request" not in text
    assert "working-with-the-operator" not in text
    assert "deck-only" not in text


def test_floor_render_refuses_a_non_current_fleet_block(
        knowledge_ctx, floor_env, monkeypatch, capsys):
    claude, db = floor_env
    _add_fleet_block(db, "platform/instruction-blocks/retired-rule",
                     "Gone.\n", status="retired")
    out = claude / "CLAUDE.md"
    out.write_text("previous floor\n")
    with pytest.raises(SystemExit) as e:
        _run_floor(knowledge_ctx, monkeypatch, "--output", str(out))
    # die()'s code 1 and its own message: an argparse rejection of an unknown
    # subcommand also exits nonzero and would pass a bare `!= 0`.
    assert e.value.code == 1
    err = capsys.readouterr().err
    assert "retired-rule" in err and "not current" in err
    assert out.read_text() == "previous floor\n"
    assert not (claude / ".floor-manifest.json").exists()


def test_floor_render_refuses_an_index_with_no_fleet_block(
        knowledge_ctx, floor_env, monkeypatch, capsys):
    claude, db = floor_env
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM docs WHERE frontmatter_extra LIKE '%fleet%'")
    conn.commit()
    conn.close()
    out = claude / "CLAUDE.md"
    out.write_text("previous floor\n")
    with pytest.raises(SystemExit) as e:
        _run_floor(knowledge_ctx, monkeypatch, "--output", str(out))
    assert e.value.code == 1
    assert "no `scope: fleet` instruction block" in capsys.readouterr().err
    assert out.read_text() == "previous floor\n"


def test_floor_render_replaces_a_symlink_with_a_regular_file(
        knowledge_ctx, floor_env, monkeypatch, tmp_path):
    claude, _ = floor_env
    target = tmp_path / "substrate" / "machine-global-CLAUDE.md"
    target.parent.mkdir()
    target.write_text("hand-written rules\n")
    st = os.stat(target)
    out = claude / "CLAUDE.md"
    out.symlink_to(target)
    assert _run_floor(knowledge_ctx, monkeypatch, "--output", str(out)) == 0
    assert not out.is_symlink()
    assert "verify-before-you-report" in out.read_text()
    # the file the link pointed at is untouched: it was replaced, never written through
    assert target.read_text() == "hand-written rules\n"
    assert os.stat(target).st_ino == st.st_ino


def test_floor_render_leaves_the_previous_file_whole_when_the_write_fails(
        knowledge_ctx, floor_env, monkeypatch):
    claude, _ = floor_env
    out = claude / "CLAUDE.md"
    previous = "the full previous floor\n" * 50
    out.write_text(previous)

    def boom(src, dst):
        raise OSError("simulated failure between the temp write and the replace")

    monkeypatch.setattr(knowledge_ctx.os, "replace", boom)
    with pytest.raises(OSError):
        _run_floor(knowledge_ctx, monkeypatch, "--output", str(out))
    assert out.read_text() == previous
    assert sorted(p.name for p in claude.iterdir()) == ["CLAUDE.md"], (
        "the temp file must not be left behind")


def test_floor_render_does_not_touch_the_substrate_repo_copy(
        knowledge_ctx, floor_env, monkeypatch, tmp_path):
    claude, _ = floor_env
    repo_copy = tmp_path / "substrate" / "machine-global-CLAUDE.md"
    repo_copy.parent.mkdir()
    repo_copy.write_text("tracked copy\n")
    before = (repo_copy.read_bytes(), os.stat(repo_copy).st_mtime_ns,
              sorted(p.name for p in repo_copy.parent.iterdir()))
    (claude / "CLAUDE.md").symlink_to(repo_copy)
    assert _run_floor(knowledge_ctx, monkeypatch,
                      "--output", str(claude / "CLAUDE.md")) == 0
    assert (repo_copy.read_bytes(), os.stat(repo_copy).st_mtime_ns,
            sorted(p.name for p in repo_copy.parent.iterdir())) == before


def test_floor_manifest_lists_every_composed_block_with_its_body_sha256(
        knowledge_ctx, floor_env, monkeypatch, tmp_path):
    claude, db = floor_env
    _add_fleet_block(db, "platform/instruction-blocks/a-second-fleet-rule",
                     "Another rule.\n")
    kb = tmp_path / "knowledge"
    kb.mkdir()
    _git_init(kb)
    subprocess.run(["git", "-C", str(kb), "-c", "user.email=t@t", "-c",
                    "user.name=t", "commit", "--allow-empty", "-qm", "seed"],
                   check=True)
    head = subprocess.run(["git", "-C", str(kb), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    out = claude / "CLAUDE.md"
    assert _run_floor(knowledge_ctx, monkeypatch, "--output", str(out)) == 0
    manifest = json.loads((claude / ".floor-manifest.json").read_text())
    assert manifest["output"] == str(out)
    assert manifest["knowledge_commit"] == head
    assert manifest["rendered_at"]
    assert manifest["blocks"] == [
        {"id": "platform/instruction-blocks/a-second-fleet-rule", "scope": "fleet",
         "body_sha256": hashlib.sha256(b"Another rule.\n").hexdigest()},
        {"id": "platform/instruction-blocks/verify-before-you-report", "scope": "fleet",
         "body_sha256": hashlib.sha256(b"Say what you ran.\n").hexdigest()},
    ]
    # each hash the manifest carries is the one stamped in the served file
    for b in manifest["blocks"]:
        assert f"{b['id']} scope:fleet body_sha256:{b['body_sha256']}" in out.read_text()


def test_floor_dry_run_writes_nothing(knowledge_ctx, floor_env, monkeypatch, capsys):
    claude, _ = floor_env
    out = claude / "CLAUDE.md"
    assert _run_floor(knowledge_ctx, monkeypatch, "--output", str(out), "--dry-run") == 0
    assert "verify-before-you-report" in capsys.readouterr().out
    assert list(claude.iterdir()) == []


def test_floor_requires_an_explicit_output(
        knowledge_ctx, floor_env, monkeypatch, capsys):
    """No default path: an invocation that omits `--output` must not be able to
    cut ~/.claude/CLAUDE.md over."""
    claude, _ = floor_env
    with pytest.raises(SystemExit) as e:
        _run_floor(knowledge_ctx, monkeypatch)
    assert e.value.code == 2
    assert "--output" in capsys.readouterr().err
    assert list(claude.iterdir()) == []


# --- floor slice 2: the targeted render drops fleet blocks --------------------
# (issue_01M3ABW3HN0J8N154YZMM8Y6G5)

def test_use_render_composes_no_fleet_block_for_an_interactive_session(
        knowledge_ctx, tmp_path, monkeypatch):
    """Red on origin/main: `_instruction_block_binds` returned True on
    `scope: fleet` before any selector check, so `use` composed every fleet
    block. The non-fleet control proves the render composed something at all."""
    db = tmp_path / "index.db"
    _index_with_instruction_blocks(db)
    _add_fleet_block(db, "platform/instruction-blocks/a-second-fleet-rule", "Another.\n")
    text = _run_use_through_main(
        knowledge_ctx, tmp_path, monkeypatch, db,
        ["knowledge-ctx", "use", "daytime", "--force", "--force-anyway"])
    assert "instruction-blocks/deck-only" in text
    assert "verify-before-you-report" not in text
    assert "a-second-fleet-rule" not in text


@pytest.mark.parametrize("agent", ["reviewer", "coder"])
def test_use_render_composes_every_fleet_block_for_a_dispatched_run(
        knowledge_ctx, tmp_path, monkeypatch, agent):
    """A dispatched run's own claude never reads the floor once
    `claudeMdExcludes` (`run_sandbox.settings`) excludes it, so its own
    targeted render must carry every fleet block itself — cut over or not
    (context trim phase 2, issue_01M4D32KA2F99QGKABZ8HV2RG5). Red before that
    render passed `include_fleet=True` for a dispatched session kind.
    Parametrized over both product roles (`agentctl spawn`'s coder and
    reviewer), not just one: nothing in `main()`'s wiring singles either out."""
    db = tmp_path / "index.db"
    _index_with_instruction_blocks(db)
    _add_fleet_block(db, "platform/instruction-blocks/a-second-fleet-rule", "Another.\n")
    text = _run_use_through_main(
        knowledge_ctx, tmp_path, monkeypatch, db,
        ["knowledge-ctx", "use", "daytime", "--agent", agent,
         "--force", "--force-anyway"])
    assert "instruction-blocks/deck-only" in text
    assert "verify-before-you-report" in text
    assert "a-second-fleet-rule" in text
    assert _read_profile_record(tmp_path)["fleet_blocks"] == {
        "composed": True, "reason": "a dispatched run does not read the floor"}


def _fleet_ids_in(text):
    return [i for i in ("verify-before-you-report", "a-second-fleet-rule")
            if f"instruction-blocks/{i}" in text]


def test_targeted_render_composes_fleet_blocks_before_the_cutover(
        knowledge_ctx, tmp_path, monkeypatch):
    """No manifest: `~/.claude/CLAUDE.md` is still hand-written and lacks two
    fleet rules (a merge guard and the single-quote `--task` rule), so an
    interactive `use` composes fleet blocks exactly as on origin/main. A
    dispatched `use` composes the same blocks, but the record gives the
    different, now-primary reason: it never reads the floor at all,
    regardless of the floor's own cutover state."""
    db = tmp_path / "index.db"
    _index_with_instruction_blocks(db)
    _add_fleet_block(db, "platform/instruction-blocks/a-second-fleet-rule", "Another.\n")

    text = _run_use_through_main(
        knowledge_ctx, tmp_path, monkeypatch, db,
        ["knowledge-ctx", "use", "daytime", "--force", "--force-anyway"],
        cut_over=False)
    assert _fleet_ids_in(text) == ["verify-before-you-report", "a-second-fleet-rule"]
    assert "instruction-blocks/deck-only" in text
    assert _read_profile_record(tmp_path)["fleet_blocks"] == {
        "composed": True, "reason": "floor not cut over"}

    text = _run_use_through_main(
        knowledge_ctx, tmp_path, monkeypatch, db,
        ["knowledge-ctx", "use", "daytime", "--agent", "reviewer",
         "--force", "--force-anyway"],
        cut_over=False)
    assert _fleet_ids_in(text) == ["verify-before-you-report", "a-second-fleet-rule"]
    assert "instruction-blocks/deck-only" in text
    assert _read_profile_record(tmp_path)["fleet_blocks"] == {
        "composed": True, "reason": "a dispatched run does not read the floor"}


def test_targeted_render_drops_fleet_blocks_once_the_manifest_names_the_floor(
        knowledge_ctx, tmp_path, monkeypatch):
    db = tmp_path / "index.db"
    _index_with_instruction_blocks(db)
    text = _run_use_through_main(
        knowledge_ctx, tmp_path, monkeypatch, db,
        ["knowledge-ctx", "use", "daytime", "--force", "--force-anyway"])
    assert _fleet_ids_in(text) == []
    assert "instruction-blocks/deck-only" in text
    assert _read_profile_record(tmp_path)["fleet_blocks"] == {
        "composed": False, "reason": "floor cut over"}


def test_a_manifest_naming_another_path_is_not_a_cutover(
        knowledge_ctx, tmp_path, monkeypatch):
    """The manifest must name `~/.claude/CLAUDE.md` itself. One for a floor
    rendered elsewhere says nothing about the file sessions read."""
    db = tmp_path / "index.db"
    _index_with_instruction_blocks(db)
    argv = ["knowledge-ctx", "use", "daytime", "--force", "--force-anyway"]
    _run_use_through_main(knowledge_ctx, tmp_path, monkeypatch, db, argv,
                          cut_over=False)
    _cut_over_floor(tmp_path / "home", output=tmp_path / "elsewhere" / "CLAUDE.md")
    monkeypatch.setattr("sys.argv", argv)
    assert knowledge_ctx.main() == 0
    text = (tmp_path / "convivy-deck" / ".claude" / "CLAUDE.md").read_text()
    assert _fleet_ids_in(text) == ["verify-before-you-report"]
    assert _read_profile_record(tmp_path)["fleet_blocks"]["composed"] is True


def test_an_unreadable_manifest_is_not_a_cutover(knowledge_ctx, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude").mkdir()
    assert knowledge_ctx.floor_cut_over() is False
    (tmp_path / ".claude" / ".floor-manifest.json").write_text("not json")
    assert knowledge_ctx.floor_cut_over() is False
    (tmp_path / ".claude" / ".floor-manifest.json").write_text("[]")
    assert knowledge_ctx.floor_cut_over() is False


def test_a_retired_fleet_block_does_not_fail_a_targeted_render(
        knowledge_ctx, tmp_path, monkeypatch):
    """Red on origin/main: `resolve_instruction_blocks` died on `status !=
    current` for any block that bound, a fleet block included."""
    db = tmp_path / "index.db"
    _index_with_instruction_blocks(db)
    _add_fleet_block(db, "platform/instruction-blocks/retired-rule", "Gone.\n",
                     status="retired")
    text = _run_use_through_main(
        knowledge_ctx, tmp_path, monkeypatch, db,
        ["knowledge-ctx", "use", "daytime", "--force", "--force-anyway"])
    assert "instruction-blocks/deck-only" in text
    assert "retired-rule" not in text


def test_floor_render_still_fails_closed_on_a_retired_fleet_block(
        knowledge_ctx, floor_env, tmp_path, monkeypatch, capsys):
    """The pair to the test above. The safety property moved rather than went
    away: the same retired fleet block that a targeted render now ignores
    still stops the floor render. Red until `include_fleet` exists to tell the
    two renders apart (either one alone passes against a resolver that
    always skips, or one that never does)."""
    claude, db = floor_env
    _add_fleet_block(db, "platform/instruction-blocks/retired-rule", "Gone.\n",
                     status="retired")
    _run_use_through_main(
        knowledge_ctx, tmp_path, monkeypatch, db,
        ["knowledge-ctx", "use", "daytime", "--force", "--force-anyway"])
    capsys.readouterr()
    out = claude / "CLAUDE.md"
    with pytest.raises(SystemExit) as e:
        _run_floor(knowledge_ctx, monkeypatch, "--output", str(out))
    assert e.value.code == 1
    assert "retired-rule" in capsys.readouterr().err
    assert not out.exists()


def test_resolve_instruction_blocks_keeps_fleet_by_default(knowledge_ctx):
    """`include_fleet` defaults True so the floor path, which passes nothing,
    keeps every fleet block."""
    rows = [_ib_row("platform/instruction-blocks/floor", scope="fleet")]
    got = knowledge_ctx.resolve_instruction_blocks(
        rows, crew=None, agent=None, session_kind="interactive")
    assert [r["id"] for r in got] == ["platform/instruction-blocks/floor"]
    assert knowledge_ctx.resolve_instruction_blocks(
        rows, crew=None, agent=None, session_kind="interactive",
        include_fleet=False) == []


def _read_profile_record(tmp_path):
    return json.loads(
        (tmp_path / "convivy-deck" / ".claude" / ".profile-documents.json").read_text())


def test_profile_documents_records_each_composed_block_and_hash(
        knowledge_ctx, tmp_path, monkeypatch):
    """Red on origin/main: `documents` was built from profile rows only and
    listed no block."""
    db = tmp_path / "index.db"
    _index_with_instruction_blocks(db)
    text = _run_use_through_main(
        knowledge_ctx, tmp_path, monkeypatch, db,
        ["knowledge-ctx", "use", "daytime", "--force", "--force-anyway"])
    record = _read_profile_record(tmp_path)
    assert record["instruction_blocks"] == [
        {"id": "platform/instruction-blocks/deck-only", "scope": "crew",
         "body_sha256": hashlib.sha256(b"A rule for this crew.\n").hexdigest()},
        {"id": "platform/instruction-blocks/working-with-the-operator",
         "scope": "crew",
         "body_sha256": hashlib.sha256(b"End on one concrete proposal.\n").hexdigest()},
    ]
    for b in record["instruction_blocks"]:
        assert f"{b['id']} scope:crew body_sha256:{b['body_sha256']}" in text


def _import_line(db):
    """An `@` line at a real file under `<CONVIVY>/write-better`, the root
    every render inlines from (`_run_use_through_main` puts CONVIVY at the
    index's directory). A path outside the roots is dropped, so the composed
    fixtures cannot use a dummy path."""
    wb = db.parent / "write-better"
    wb.mkdir(exist_ok=True)
    (wb / "essentials.md").write_text("STANDARD TEXT\n")
    return f"@{wb / 'essentials.md'}\n"


def _add_writing_standard_blocks(db):
    for doc_id, fm in [
        ("platform/instruction-blocks/write-better-import",
         {"scope": "crew", "binds_sessions": ["interactive"]}),
        ("platform/instruction-blocks/write-better-import-dispatched",
         {"scope": "agent", "binds_roles": ["architect"],
          "binds_sessions": ["dispatched"]}),
        ("platform/instruction-blocks/talk-better-import",
         {"scope": "crew", "binds_sessions": ["interactive"]}),
    ]:
        _insert_instruction_block_row(db, doc_id, _import_line(db), fm)


def test_profile_documents_states_why_a_writing_standard_was_not_composed(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """Red on origin/main: no `writing_standards` field. A dispatched coder is
    the case that drops both standards, each for a different reason; the same
    index rendered for an interactive session composes both, so a selector
    that matched nobody could not produce the coder's record."""
    db = tmp_path / "index.db"
    _index_with_instruction_blocks(db)
    _add_writing_standard_blocks(db)

    _run_use_through_main(
        knowledge_ctx, tmp_path, monkeypatch, db,
        ["knowledge-ctx", "use", "daytime", "--agent", "coder",
         "--force", "--force-anyway"])
    ws = _read_profile_record(tmp_path)["writing_standards"]
    assert ws["write-better"]["composed"] is False
    assert ws["write-better"]["reason"] == (
        "dispatched; write-better-import: the block binds interactive sessions "
        "only, this one is dispatched; write-better-import-dispatched: "
        "role 'coder' is not in the block's binds_roles")
    assert ws["talk-better"] == {
        "composed": False,
        "reason": "dispatched; talk-better-import: the block binds interactive "
                  "sessions only, this one is dispatched"}
    err = capsys.readouterr().err
    assert "writing standard write-better: NOT composed" in err
    assert "writing standard talk-better: NOT composed" in err

    _run_use_through_main(
        knowledge_ctx, tmp_path, monkeypatch, db,
        ["knowledge-ctx", "use", "daytime", "--force", "--force-anyway"])
    ws = _read_profile_record(tmp_path)["writing_standards"]
    assert ws["write-better"] == {
        "composed": True, "blocks": ["platform/instruction-blocks/write-better-import"]}
    assert ws["talk-better"] == {
        "composed": True, "blocks": ["platform/instruction-blocks/talk-better-import"]}
    assert "writing standard write-better: composed via" in capsys.readouterr().err


def test_a_fleet_writing_standard_says_the_floor_delivers_it(
        knowledge_ctx, tmp_path, monkeypatch):
    """A candidate authored `scope: fleet` is dropped by a cut-over render; the
    record says the floor delivers it, not "bound but not composed". Before the
    cutover the same block is composed and the record says so. `binds_topics`
    on a candidate is judged with the session's topics, as the render does."""
    db = tmp_path / "index.db"
    _index_with_instruction_blocks(db)
    _insert_instruction_block_row(
        db, "platform/instruction-blocks/talk-better-import", _import_line(db),
        {"scope": "fleet"})
    argv = ["knowledge-ctx", "use", "daytime", "--force", "--force-anyway"]
    _run_use_through_main(knowledge_ctx, tmp_path, monkeypatch, db, argv)
    assert _read_profile_record(tmp_path)["writing_standards"]["talk-better"] == {
        "composed": False,
        "reason": "interactive; talk-better-import: delivered by the floor"}
    _run_use_through_main(knowledge_ctx, tmp_path, monkeypatch, db, argv,
                          cut_over=False)
    assert _read_profile_record(tmp_path)["writing_standards"]["talk-better"] == {
        "composed": True, "blocks": ["platform/instruction-blocks/talk-better-import"]}


def test_writing_standards_record_judges_binds_topics_with_the_sessions_topics(
        knowledge_ctx):
    row = _ib_row("platform/instruction-blocks/talk-better-import",
                  scope="crew", binds_topics=["writing"])
    rec = knowledge_ctx.writing_standards_record(
        [row], [], crew="deck", agent=None, session_kind="interactive",
        topics=frozenset({"writing"}))
    assert "binds_topics" not in rec["talk-better"]["reason"]
    rec = knowledge_ctx.writing_standards_record(
        [row], [], crew="deck", agent=None, session_kind="interactive")
    assert "binds_topics" in rec["talk-better"]["reason"]


# --- `main()` passing the dispatched work item's topics through
# (issue_01M3V1J3TCHC7CWCZ9534WD6KG) ----------------------------------------
#
# Before this, `main()` called `resolve_instruction_blocks` and
# `writing_standards_record` with no `topics=` at all, so `binds_topics`
# selected nothing for any real dispatch — `--issue` reached `render_source`
# (for the Library bundle) but never the instruction-block resolution beside
# it. These tests drive `main()` end to end, the same way
# `test_use_with_an_agent_renders_a_dispatched_session_through_main` does,
# rather than calling `resolve_instruction_blocks`/`writing_standards_record`
# directly: every test above this one would stay green if `main()` itself
# never threaded `topics` through, because none of them go through `main()`.


def _patch_item_topics(monkeypatch, *, status="ok", item=None):
    """Make `issue_client.get_item_from_default_library` answer as the
    Library would for one `GET /items/{id}`, with no network call. `status`
    other than `"ok"` is a failed lookup — `knowledge_ctx.session_topics`'s
    other branch."""
    def fake(issue_id, *, tok=None, timeout=5):
        if status != "ok":
            return {"status": status, "item": {}}
        return {"status": "ok", "item": item or {}}
    monkeypatch.setattr(issue_client, "get_item_from_default_library", fake)


def test_session_topics_is_empty_with_no_issue_named(knowledge_ctx):
    assert knowledge_ctx.session_topics(None) == (frozenset(), None)


def test_session_topics_reads_the_items_topics(knowledge_ctx, monkeypatch):
    _patch_item_topics(monkeypatch, item={"id": "issue_X",
                                          "topics": ["dependency-bump", "ci"]})
    assert knowledge_ctx.session_topics("issue_X") == (
        frozenset({"dependency-bump", "ci"}), None)


def test_session_topics_uses_the_dispatch_critical_path_timeout(
        knowledge_ctx, monkeypatch):
    """Reviewer, non-blocking: this read sits inline on a dispatch the same way
    `agentctl`'s own `--issue` read does, so it must use the same 3s
    `IC.DISPATCH_READ_TIMEOUT` budget rather than `issue_client`'s
    general-purpose 5s default. Red on 4ff3abee, which called
    `get_item_from_default_library` with no `timeout=` at all."""
    seen = {}

    def fake(issue_id, *, tok=None, timeout=5):
        seen["timeout"] = timeout
        return {"status": "ok", "item": {}}
    monkeypatch.setattr(issue_client, "get_item_from_default_library", fake)
    knowledge_ctx.session_topics("issue_X")
    assert seen["timeout"] == issue_client.DISPATCH_READ_TIMEOUT


def test_session_topics_an_item_with_no_topics_is_not_a_failure(
        knowledge_ctx, monkeypatch):
    _patch_item_topics(monkeypatch, item={"id": "issue_X"})
    assert knowledge_ctx.session_topics("issue_X") == (frozenset(), None)


def test_session_topics_a_failed_lookup_is_visibly_distinct_from_no_topics(
        knowledge_ctx, monkeypatch):
    """A failed read and a real item with no topics both reduce to an empty
    `frozenset`, so the second element of the pair — `None` only on success —
    is the one thing that tells them apart."""
    _patch_item_topics(monkeypatch, status="error:404")
    assert knowledge_ctx.session_topics("issue_X") == (frozenset(), "error:404")


def test_use_with_a_matching_item_composes_a_topic_bound_block_through_main(
        knowledge_ctx, tmp_path, monkeypatch):
    """Red on origin/main: `main()` passed no `topics=` at all, so this block
    was never composed for any dispatched session regardless of the item."""
    db = tmp_path / "index.db"
    _index_with_instruction_blocks(db)
    _insert_instruction_block_row(
        db, "platform/instruction-blocks/dependency-bump-only",
        "Pin exact versions.\n",
        {"scope": "task", "binds_topics": ["dependency-bump"]})
    _patch_item_topics(monkeypatch,
                       item={"id": "issue_X", "topics": ["dependency-bump"]})
    text = _run_use_through_main(
        knowledge_ctx, tmp_path, monkeypatch, db,
        ["knowledge-ctx", "use", "daytime", "--agent", "reviewer",
         "--issue", "issue_X", "--force", "--force-anyway"])
    assert "platform/instruction-blocks/dependency-bump-only" in text


def test_use_with_a_non_matching_item_omits_the_topic_bound_block_through_main(
        knowledge_ctx, tmp_path, monkeypatch):
    """The same agent, the same block — only the item's topics differ (a UI
    change rather than a dependency bump), which is the operator's test case for the
    instruction-block assembly epic.

    Reviewer, non-blocking: this guards against a FALSE MATCH and passes on
    4ff3abee too, before `main()` threaded `topics` through at all — with no
    topics passed, `binds_topics` matches nothing, so the block is omitted
    either way. `test_use_with_a_matching_item_composes_a_topic_bound_block_
    through_main` is the one that is red on 4ff3abee and so the one that
    guards the wiring."""
    db = tmp_path / "index.db"
    _index_with_instruction_blocks(db)
    _insert_instruction_block_row(
        db, "platform/instruction-blocks/dependency-bump-only",
        "Pin exact versions.\n",
        {"scope": "task", "binds_topics": ["dependency-bump"]})
    _patch_item_topics(monkeypatch, item={"id": "issue_X", "topics": ["ui"]})
    text = _run_use_through_main(
        knowledge_ctx, tmp_path, monkeypatch, db,
        ["knowledge-ctx", "use", "daytime", "--agent", "reviewer",
         "--issue", "issue_X", "--force", "--force-anyway"])
    assert "platform/instruction-blocks/dependency-bump-only" not in text


def test_use_with_an_issue_writing_standards_record_sees_the_same_topics(
        knowledge_ctx, tmp_path, monkeypatch):
    """`writing_standards_record` is a second call beside `resolve_instruction_
    blocks` in `main()`; it must be judging the SAME topic set, not an empty
    default of its own, or the two could disagree about whether a topic-bound
    writing standard was composed."""
    db = tmp_path / "index.db"
    _index_with_instruction_blocks(db)
    _insert_instruction_block_row(
        db, "platform/instruction-blocks/talk-better-import", _import_line(db),
        {"scope": "task", "binds_topics": ["dependency-bump"]})
    _patch_item_topics(monkeypatch,
                       item={"id": "issue_X", "topics": ["dependency-bump"]})
    text = _run_use_through_main(
        knowledge_ctx, tmp_path, monkeypatch, db,
        ["knowledge-ctx", "use", "daytime", "--agent", "reviewer",
         "--issue", "issue_X", "--force", "--force-anyway"])
    assert "platform/instruction-blocks/talk-better-import" in text
    assert _read_profile_record(tmp_path)["writing_standards"]["talk-better"] == {
        "composed": True, "blocks": ["platform/instruction-blocks/talk-better-import"]}


def test_use_with_a_failed_topic_lookup_is_visible_on_stderr(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """A failed lookup must not render identically to a real item with no
    topics: the block is left out either way, but only a failure says so on
    stderr, naming the status `session_topics` returned."""
    db = tmp_path / "index.db"
    _index_with_instruction_blocks(db)
    _insert_instruction_block_row(
        db, "platform/instruction-blocks/dependency-bump-only",
        "Pin exact versions.\n",
        {"scope": "task", "binds_topics": ["dependency-bump"]})
    _patch_item_topics(monkeypatch, status="error:404")
    text = _run_use_through_main(
        knowledge_ctx, tmp_path, monkeypatch, db,
        ["knowledge-ctx", "use", "daytime", "--agent", "reviewer",
         "--issue", "issue_X", "--force", "--force-anyway"])
    assert "platform/instruction-blocks/dependency-bump-only" not in text
    err = capsys.readouterr().err
    assert "could not read issue_X's topics" in err
    assert "error:404" in err


def test_profile_documents_names_a_writing_standard_the_index_does_not_serve(
        knowledge_ctx, tmp_path, monkeypatch):
    """Today's corpus: the three blocks do not exist yet. The record says so
    rather than leaving an absence to be read as a decision."""
    db = tmp_path / "index.db"
    _index_with_instruction_blocks(db)
    _run_use_through_main(
        knowledge_ctx, tmp_path, monkeypatch, db,
        ["knowledge-ctx", "use", "daytime", "--force", "--force-anyway"])
    ws = _read_profile_record(tmp_path)["writing_standards"]
    assert ws["write-better"] == {
        "composed": False,
        "reason": "the index serves no instruction block named "
                  "write-better-import or write-better-import-dispatched"}


def test_profile_documents_match_compares_blocks_and_standards(knowledge_ctx):
    """A change to the composed blocks or a standard's reason must rewrite the
    record, or `plan_is_current` would call a stale record current."""
    base = {"profile": "p/d", "documents": [], "instruction_blocks": [],
            "writing_standards": {}}
    same = json.dumps({**base, "materialized_at": "t1"})
    assert knowledge_ctx._profile_documents_match(
        same, json.dumps({**base, "materialized_at": "t2"})) is True
    for changed in ({"instruction_blocks": [{"id": "x"}]},
                    {"writing_standards": {"write-better": {"composed": True}}},
                    {"fleet_blocks": {"composed": False, "reason": "floor cut over"}}):
        assert knowledge_ctx._profile_documents_match(
            same, json.dumps({**base, **changed})) is False


# --- selectors: binds_roles by declared role, binds_agents by exact name ------
# (the operator's ruling, issue_01M3ABW3HN0J8N154YZMM8Y6G5 comment 4099)

def _binds(knowledge_ctx, fm, agent):
    return knowledge_ctx._instruction_block_binds(
        {"scope": "agent", **fm}, crew=None, agent=agent, session_kind="dispatched")


@pytest.mark.parametrize("agent,expected", [
    ("sel-reviewer", True),        # declares reviewer
    ("wb-reviewer", False),        # declares writing-reviewer
    ("platform-coder", False),     # declares coder
])
def test_binds_roles_binds_an_agent_whose_document_declares_the_role(
        knowledge_ctx, agent, expected):
    """Red on origin/main for the False rows: nothing read `binds_roles`, so
    such a block bound every session. The True row is green there only by that
    same accident, and is what a fail-closed fix must not break."""
    assert _binds(knowledge_ctx, {"binds_roles": ["reviewer"]}, agent) is expected


@pytest.mark.parametrize("agent,expected", [
    ("platform-coder", True),
    ("deck-coder", False),   # same role, other name: an exact-name match cannot reach it
    ("sel-reviewer", False),
])
def test_binds_agents_binds_the_exact_agent_name_only(knowledge_ctx, agent, expected):
    """Red on origin/main for `platform-coder`: `binds_agents` was compared with
    the agent's ROLE (`coder`), so an instance id matched nothing."""
    assert _binds(knowledge_ctx, {"binds_agents": ["platform-coder"]}, agent) is expected


@pytest.mark.parametrize("agent", ["sel-reviewer", "platform-coder"])
def test_binds_agents_carrying_a_role_word_does_not_bind_by_role(
        knowledge_ctx, agent):
    """The transitional fallback is gone (issue_01M3ABW3HN0J8N154YZMM8Y6G5,
    step 3). Red on origin/main for `sel-reviewer`, which declares `reviewer`
    and was matched by role through `binds_agents`."""
    assert _binds(knowledge_ctx, {"binds_agents": ["reviewer"]}, agent) is False


def _agent_doc_without_a_role(db, doc_id):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO docs (id, source, source_ref, kind, project, title, status, "
        "body, body_hash, updated_at, frontmatter_extra) VALUES "
        "(?, 'files', ?, 'agent', 'x', 't', 'current', 'b', 'h', '2026-09-24', '{}')",
        (doc_id, f"{doc_id}.md"))
    conn.commit()
    conn.close()


@pytest.mark.parametrize("agent", ["roleless", "svc-reviewer", None])
def test_an_agent_with_no_declared_role_matches_no_binds_roles_block(
        knowledge_ctx, agent):
    """`roleless` has a document declaring no role; `svc-reviewer` has no
    document; None is a crew render. All fail closed. Red on origin/main, where
    the block bound everyone."""
    _agent_doc_without_a_role(knowledge_ctx.DB_PATH, "x/agents/roleless")
    assert _binds(knowledge_ctx, {"binds_roles": ["reviewer"]}, agent) is False


def test_an_agent_with_no_declared_role_still_binds_by_exact_name(knowledge_ctx):
    _agent_doc_without_a_role(knowledge_ctx.DB_PATH, "x/agents/roleless")
    assert _binds(knowledge_ctx, {"binds_agents": ["roleless"]}, "roleless") is True
    assert _binds(knowledge_ctx, {"binds_agents": ["roleless"]}, "svc-reviewer") is False


def test_binds_roles_and_binds_agents_combine_with_and(knowledge_ctx):
    fm = {"binds_roles": ["coder"], "binds_agents": ["platform-coder"]}
    assert _binds(knowledge_ctx, fm, "platform-coder") is True
    assert _binds(knowledge_ctx, fm, "deck-coder") is False
    assert _binds(knowledge_ctx, {"binds_roles": ["reviewer"],
                                  "binds_agents": ["platform-coder"]},
                  "platform-coder") is False


def test_a_role_selected_block_reaches_a_crew_render_through_the_agents_definition(
        knowledge_ctx):
    """`resolve_agent_bound_instruction_blocks` filtered to `binds_agents`, so
    a `binds_roles` block would have reached no file at all in a crew render."""
    rows = [_ib_row("platform/instruction-blocks/by-role", scope="agent",
                    binds_roles=["reviewer"])]
    got = knowledge_ctx.resolve_agent_bound_instruction_blocks(
        rows, crew=None, agent="sel-reviewer", session_kind="interactive")
    assert [r["id"] for r in got] == ["platform/instruction-blocks/by-role"]
    assert knowledge_ctx.resolve_agent_bound_instruction_blocks(
        rows, crew=None, agent="platform-coder", session_kind="interactive") == []


# --- `knowledge-ctx bundle --generic` and its SessionStart hook --------------

_GENERIC_BUNDLE_HOOK = (pathlib.Path(__file__).resolve().parent.parent
                        / "tools" / "generic-bundle-context.sh")


def _add_block(db, doc_id, body, fm, status="current"):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO docs (id, source, source_ref, kind, project, title, "
        "status, body, body_hash, updated_at, frontmatter_extra) VALUES "
        "(?, 'files', ?, 'instruction-block', 'platform', ?, ?, ?, ?, "
        "'2026-09-24', ?)",
        (doc_id, f"{doc_id}.md", doc_id.rsplit("/", 1)[-1], status, body,
         hashlib.sha256(body.encode()).hexdigest(), json.dumps(fm)))
    conn.commit()
    conn.close()


@pytest.fixture
def bundle_env(knowledge_ctx, floor_env):
    """The floor fixture's index plus the two writing-standard imports, and a
    dispatched-only block the bundle must not carry."""
    claude, db = floor_env
    _add_block(db, "platform/instruction-blocks/write-better-import",
               "Import write-better.\n",
               {"scope": "crew", "binds_sessions": ["interactive"]})
    _add_block(db, "platform/instruction-blocks/talk-better-import",
               "Import talk-better.\n",
               {"scope": "crew", "binds_sessions": ["interactive"]})
    _add_block(db, "platform/instruction-blocks/write-better-import-dispatched",
               "Dispatched import.\n",
               {"scope": "agent", "binds_agents": ["architect"],
                "binds_sessions": ["dispatched"]})
    return claude, db


def _generic_bundle(knowledge_ctx, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["knowledge-ctx", "bundle", "--generic"])
    assert knowledge_ctx.main() == 0
    return capsys.readouterr().out


def test_generic_bundle_holds_the_interactive_blocks_and_both_imports(
        knowledge_ctx, bundle_env, monkeypatch, capsys):
    out = _generic_bundle(knowledge_ctx, monkeypatch, capsys)
    stamped = re.findall(r"<!-- instruction-block: (\S+) scope:\S+ "
                         r"body_sha256:([0-9a-f]{64}) -->", out)
    assert [i for i, _ in stamped] == [
        "platform/instruction-blocks/talk-better-import",
        "platform/instruction-blocks/working-with-the-operator",
        "platform/instruction-blocks/write-better-import",
    ]
    assert dict(stamped)["platform/instruction-blocks/write-better-import"] == \
        hashlib.sha256(b"Import write-better.\n").hexdigest()
    assert "Import write-better." in out and "Import talk-better." in out


def test_generic_bundle_holds_no_fleet_block(
        knowledge_ctx, bundle_env, monkeypatch, capsys):
    out = _generic_bundle(knowledge_ctx, monkeypatch, capsys)
    assert "scope:fleet" not in out
    assert "verify-before-you-report" not in out
    # neither the agent-bound nor the dispatched-only block reaches it
    assert "review-a-pull-request" not in out
    assert "write-better-import-dispatched" not in out


def test_generic_bundle_refuses_a_retired_block_that_binds(
        knowledge_ctx, bundle_env, monkeypatch, capsys):
    _, db = bundle_env
    conn = sqlite3.connect(db)
    conn.execute("UPDATE docs SET status='retired' WHERE id LIKE '%/talk-better-import'")
    conn.commit()
    conn.close()
    monkeypatch.setattr("sys.argv", ["knowledge-ctx", "bundle", "--generic"])
    with pytest.raises(SystemExit) as e:
        knowledge_ctx.main()
    assert e.value.code != 0
    assert capsys.readouterr().out == ""


def _set_import_body(db, name, body):
    conn = sqlite3.connect(db)
    conn.execute("UPDATE docs SET body=? WHERE id LIKE ?", (body, f"%/{name}"))
    conn.commit()
    conn.close()


@pytest.fixture
def import_env(knowledge_ctx, bundle_env, tmp_path, monkeypatch):
    """CONVIVY under tmp_path, with a write-better repo holding one essentials
    file; the write-better-import block is that file's bare `@` line."""
    monkeypatch.setattr(knowledge_ctx, "CONVIVY", tmp_path)
    monkeypatch.delenv("KNOWLEDGE_CTX_INLINE_ROOTS", raising=False)
    wb = tmp_path / "write-better"
    wb.mkdir()
    essentials = wb / "essentials.md"
    essentials.write_bytes("# Write Better\nsay it once\n".encode())
    _, db = bundle_env
    _set_import_body(db, "write-better-import", f"@{essentials}\n")
    return db, wb, essentials


def test_generic_bundle_inlines_an_allowlisted_import_with_its_hash(
        knowledge_ctx, import_env, monkeypatch, capsys):
    _, _, essentials = import_env
    out = _generic_bundle(knowledge_ctx, monkeypatch, capsys)
    digest = hashlib.sha256(essentials.read_bytes()).hexdigest()
    assert f"<!-- inlined: {essentials.resolve()} sha256:{digest} -->" in out
    assert "# Write Better\nsay it once" in out
    assert f"@{essentials}" not in out
    # the block keeps its own stamp
    assert "<!-- instruction-block: platform/instruction-blocks/write-better-import" in out


def test_generic_bundle_refuses_a_path_outside_the_allowlist(
        knowledge_ctx, import_env, tmp_path, monkeypatch, capsys):
    db, wb, _ = import_env
    secret = tmp_path / "secret.txt"
    secret.write_text("TOKEN-VALUE")
    link = wb / "link.md"
    link.symlink_to(secret)
    for target in (secret, link):
        _set_import_body(db, "write-better-import", f"@{target}\n")
        out = _generic_bundle(knowledge_ctx, monkeypatch, capsys)
        assert "TOKEN-VALUE" not in out
        assert not re.search(r"^\s*@/", out, re.M)
        assert "<!-- instruction-block: platform/instruction-blocks/write-better-import" in out


def test_generic_bundle_names_a_refused_import_on_stderr(
        knowledge_ctx, import_env, tmp_path, monkeypatch, capsys):
    db, _, _ = import_env
    secret = tmp_path / "secret.txt"
    secret.write_text("TOKEN-VALUE")
    _set_import_body(db, "write-better-import", f"@{secret}\n")
    monkeypatch.setattr("sys.argv", ["knowledge-ctx", "bundle", "--generic"])
    assert knowledge_ctx.main() == 0
    assert str(secret) in capsys.readouterr().err


def test_generic_bundle_extra_root_is_explicit_configuration(
        knowledge_ctx, import_env, tmp_path, monkeypatch, capsys):
    db, _, _ = import_env
    extra = tmp_path / "extra"
    extra.mkdir()
    (extra / "x.md").write_text("EXTRA-CONTENT\n")
    _set_import_body(db, "write-better-import", f"@{extra / 'x.md'}\n")
    assert "EXTRA-CONTENT" not in _generic_bundle(knowledge_ctx, monkeypatch, capsys)
    monkeypatch.setenv("KNOWLEDGE_CTX_INLINE_ROOTS", str(extra))
    assert "EXTRA-CONTENT" in _generic_bundle(knowledge_ctx, monkeypatch, capsys)


def test_generic_bundle_leaves_out_a_missing_import_and_still_emits(
        knowledge_ctx, import_env, tmp_path, monkeypatch, capsys):
    db, wb, _ = import_env
    missing = wb / "gone.md"
    _set_import_body(db, "write-better-import", f"@{missing}\n")
    monkeypatch.setattr("sys.argv", ["knowledge-ctx", "bundle", "--generic"])
    assert knowledge_ctx.main() == 0
    cap = capsys.readouterr()
    assert str(missing) in cap.err
    assert not re.search(r"^\s*@/", cap.out, re.M)
    assert "Import talk-better." in cap.out


def test_generic_bundle_bounds_the_inlined_size(
        knowledge_ctx, import_env, monkeypatch, capsys):
    _, wb, _ = import_env
    big = wb / "big.md"
    big.write_text("x" * (64 * 1024 + 1))
    db = import_env[0]
    _set_import_body(db, "write-better-import", f"@{big}\n")
    monkeypatch.setattr("sys.argv", ["knowledge-ctx", "bundle", "--generic"])
    assert knowledge_ctx.main() == 0
    cap = capsys.readouterr()
    assert "xxxx" not in cap.out and str(big) in cap.err
    assert not re.search(r"^\s*@/", cap.out, re.M)


def _generic_part(knowledge_ctx, monkeypatch, capsys, part, cap=None):
    monkeypatch.setattr("sys.argv", ["knowledge-ctx", "bundle", "--generic",
                                     "--part", part])
    if cap is not None:
        monkeypatch.setenv("KNOWLEDGE_CTX_PART_MAX_BYTES", str(cap))
    code = knowledge_ctx.main()
    return code, capsys.readouterr()


def test_generic_bundle_parts_each_hold_their_content_and_not_the_others(
        knowledge_ctx, import_env, monkeypatch, capsys):
    _, _, essentials = import_env
    digest = hashlib.sha256(essentials.read_bytes()).hexdigest()
    blocks = _generic_part(knowledge_ctx, monkeypatch, capsys, "blocks")[1].out
    wb = _generic_part(knowledge_ctx, monkeypatch, capsys, "write-better")[1].out
    tb = _generic_part(knowledge_ctx, monkeypatch, capsys, "talk-better")[1].out
    assert "working-with-the-operator" in blocks
    assert "write-better-import" not in blocks and "talk-better-import" not in blocks
    assert "inlined:" not in blocks
    # the write-better part keeps its stamp and its inlined marker with the hash
    assert "instruction-block: platform/instruction-blocks/write-better-import" in wb
    assert f"<!-- inlined: {essentials.resolve()} sha256:{digest} -->" in wb
    assert "say it once" in wb
    assert "working-with-the-operator" not in wb and "talk-better-import" not in wb
    assert "instruction-block: platform/instruction-blocks/talk-better-import" in tb
    assert "Import talk-better." in tb
    assert "write-better-import" not in tb and "say it once" not in tb


def test_generic_bundle_parts_together_carry_the_whole_bundle(
        knowledge_ctx, import_env, monkeypatch, capsys):
    whole = _generic_bundle(knowledge_ctx, monkeypatch, capsys)
    parts = "\n".join(
        _generic_part(knowledge_ctx, monkeypatch, capsys, p)[1].out
        for p in ("blocks", "write-better", "talk-better"))
    # the whole bundle is the same blocks in one order, so the parts reorder
    # them and lose nothing: compare the block sections as sets
    def sections(text):
        return sorted(s.strip() for s in re.split(
            r"(?=^<!-- instruction-block: )", text, flags=re.M) if s.strip())
    assert sections(parts) == sections(whole)


def test_generic_bundle_without_a_part_is_unchanged_by_the_part_option(
        knowledge_ctx, import_env, monkeypatch, capsys):
    """Every block of the whole bundle stays in one stream in composition
    order, ending with the write-better import, exactly as before --part."""
    whole = _generic_bundle(knowledge_ctx, monkeypatch, capsys)
    ids = re.findall(r"<!-- instruction-block: (\S+) ", whole)
    assert ids == [
        "platform/instruction-blocks/talk-better-import",
        "platform/instruction-blocks/working-with-the-operator",
        "platform/instruction-blocks/write-better-import",
    ]
    assert "inlined:" in whole and "Import talk-better." in whole


def test_generic_bundle_part_over_the_cap_is_refused_whole(
        knowledge_ctx, import_env, monkeypatch, capsys):
    with pytest.raises(SystemExit) as e:
        _generic_part(knowledge_ctx, monkeypatch, capsys, "write-better", cap=10)
    assert e.value.code == 1
    cap = capsys.readouterr()
    assert cap.out == ""
    lines = [ln for ln in cap.err.splitlines() if "write-better" in ln and "cap" in ln]
    assert len(lines) == 1
    assert re.search(r"'write-better' is \d+ bytes, over the 10-byte cap", lines[0])


def test_generic_bundle_cap_counts_utf8_bytes_not_characters(
        knowledge_ctx, import_env, monkeypatch, capsys):
    _, wb, essentials = import_env
    # 3000 chars, 6000 bytes: the part is ~3.4k characters and ~6.4k bytes, so
    # a cap between them refuses only a byte count.
    essentials.write_text("é" * 3000, encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        _generic_part(knowledge_ctx, monkeypatch, capsys, "write-better", cap=4500)
    assert e.value.code == 1


def test_generic_bundle_unknown_part_exits_1_not_2(
        knowledge_ctx, import_env, monkeypatch, capsys):
    with pytest.raises(SystemExit) as e:
        _generic_part(knowledge_ctx, monkeypatch, capsys, "nonsense")
    assert e.value.code == 1
    cap = capsys.readouterr()
    assert cap.out == "" and "nonsense" in cap.err


def test_generic_bundle_hook_passes_the_part_through_and_exits_1_on_size(
        tmp_path, bundle_env, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    _, db = bundle_env
    wrapper = tmp_path / "kctx-wrapper"
    wrapper.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" '
        f'"{pathlib.Path(__file__).resolve().parent.parent / "bin" / "knowledge-ctx"}" "$@"\n')
    wrapper.chmod(0o755)
    env = {**os.environ, "HOME": str(tmp_path / "home"),
           "KB_INDEX_DB": str(db), "KNOWLEDGE_CTX": str(wrapper)}

    def run(part, **extra):
        return subprocess.run(["/bin/bash", str(_GENERIC_BUNDLE_HOOK), part],
                              cwd=ws, env={**env, **extra}, capture_output=True,
                              text=True, timeout=60)
    ok = run("talk-better")
    assert ok.returncode == 0, ok.stderr
    ctx = json.loads(ok.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "Import talk-better." in ctx and "Import write-better." not in ctx
    big = run("talk-better", KNOWLEDGE_CTX_PART_MAX_BYTES="10")
    assert big.returncode == 1 and big.stdout == ""
    assert len([ln for ln in big.stderr.splitlines() if ln]) == 1
    assert "-byte cap" in big.stderr
    unknown = run("nonsense")
    assert unknown.returncode == 0 and unknown.stdout == ""


def test_targeted_render_keeps_the_bare_import_line(knowledge_ctx):
    """`use` and the floor render through render_instruction_blocks_md, which
    the inlining does not touch: `@` resolves in a CLAUDE.md."""
    row = {"id": "platform/instruction-blocks/write-better-import",
           "_fm": {"scope": "crew"}, "body_hash": "0" * 64,
           "body": "@/srv/write-better/essentials.md\n"}
    assert "\n@/srv/write-better/essentials.md\n" in \
        knowledge_ctx.render_instruction_blocks_md([row])


def _run_bundle_hook(tmp_path, bundle_env, cwd):
    """Run the real hook script against the fixture index, HOME sandboxed.
    `knowledge-ctx` is a wrapper running THIS checkout's file under the test
    interpreter, since the file's shebang is a machine-local venv."""
    _, db = bundle_env
    wrapper = tmp_path / "kctx-wrapper"
    wrapper.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" '
        f'"{pathlib.Path(__file__).resolve().parent.parent / "bin" / "knowledge-ctx"}" "$@"\n')
    wrapper.chmod(0o755)
    env = {**os.environ, "HOME": str(tmp_path / "home"),
           "KB_INDEX_DB": str(db), "KNOWLEDGE_CTX": str(wrapper)}
    return subprocess.run(["/bin/bash", str(_GENERIC_BUNDLE_HOOK)], cwd=cwd,
                          env=env, capture_output=True, text=True, timeout=60)


def _hook_beside_a_knowledge_ctx(tmp_path, bundle_env, symlink):
    """A repo-shaped tree, tools/hook + bin/knowledge-ctx, run with
    KNOWLEDGE_CTX unset and a PATH that holds no knowledge-ctx."""
    _, db = bundle_env
    repo = tmp_path / "repo"
    (repo / "tools").mkdir(parents=True)
    (repo / "bin").mkdir()
    kctx = repo / "bin" / "knowledge-ctx"
    kctx.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" '
        f'"{pathlib.Path(__file__).resolve().parent.parent / "bin" / "knowledge-ctx"}" "$@"\n')
    kctx.chmod(0o755)
    hook = repo / "tools" / "hook.sh"
    hook.write_text(_GENERIC_BUNDLE_HOOK.read_text())
    entry = hook
    if symlink:
        (tmp_path / "elsewhere").mkdir()
        entry = tmp_path / "elsewhere" / "hook-link.sh"
        entry.symlink_to(hook)
    ws = tmp_path / "ws"
    ws.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "KNOWLEDGE_CTX"}
    env.update(HOME=str(tmp_path / "home"), KB_INDEX_DB=str(db),
               PATH="/usr/bin:/bin")
    return subprocess.run(["/bin/bash", str(entry)], cwd=ws, env=env,
                          capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("symlink", [False, True])
def test_generic_bundle_hook_finds_knowledge_ctx_beside_the_script(
        tmp_path, bundle_env, symlink):
    r = _hook_beside_a_knowledge_ctx(tmp_path, bundle_env, symlink)
    assert r.returncode == 0, r.stderr
    assert "Import write-better." in json.loads(r.stdout)[
        "hookSpecificOutput"]["additionalContext"]
    assert r.stderr == ""


def test_generic_bundle_hook_emits_where_no_render_exists(tmp_path, bundle_env):
    ws = tmp_path / "ws"
    ws.mkdir()
    r = _run_bundle_hook(tmp_path, bundle_env, ws)
    assert r.returncode == 0, r.stderr
    payload = json.loads(r.stdout)
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "Import write-better." in ctx and "Import talk-better." in ctx
    assert "scope:fleet" not in ctx


def test_generic_bundle_hook_is_silent_in_a_rendered_workspace(tmp_path, bundle_env):
    for manifest in (".active-profile", ".profile-documents.json"):
        ws = tmp_path / f"ws{manifest}"
        (ws / ".claude").mkdir(parents=True)
        (ws / ".claude" / manifest).write_text("x\n")
        r = _run_bundle_hook(tmp_path, bundle_env, ws)
        assert (r.returncode, r.stdout) == (0, ""), manifest


def test_generic_bundle_hook_finds_the_manifest_at_the_git_toplevel(tmp_path, bundle_env):
    ws = tmp_path / "repo"
    (ws / ".claude").mkdir(parents=True)
    (ws / ".claude" / ".active-profile").write_text("daytime\n")
    (ws / "sub" / "dir").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(ws)], check=True)
    r = _run_bundle_hook(tmp_path, bundle_env, ws / "sub" / "dir")
    assert (r.returncode, r.stdout) == (0, "")


def test_a_hand_written_claude_tree_with_no_manifest_still_gets_the_bundle(
        tmp_path, bundle_env):
    ws = tmp_path / "hollis"
    (ws / ".claude" / "agents").mkdir(parents=True)
    (ws / ".claude" / "agents" / "scout.md").write_text("hand-written\n")
    r = _run_bundle_hook(tmp_path, bundle_env, ws)
    assert r.returncode == 0, r.stderr
    assert "Import write-better." in json.loads(
        r.stdout)["hookSpecificOutput"]["additionalContext"]


# --- the crew and dispatched renders inline `@` imports
# (issue_01M3DAAXJMT5X1FE2RF0JG28SB). Claude Code prompts once per project
# before an external `@` import in a CLAUDE.md, and under `claude -p` drops it
# silently, so the file text has to be in the render itself.

_WB_BLOCK = "platform/instruction-blocks/write-better-import"
_WB_DISPATCHED_BLOCK = "platform/instruction-blocks/write-better-import-dispatched"


def _render_with_imports(knowledge_ctx, tmp_path, monkeypatch, capsys, argv,
                         body_for):
    """Render through `main()` with the write-better blocks holding
    `body_for(essentials_path)`; return (CLAUDE.md text, record, stderr)."""
    monkeypatch.delenv("KNOWLEDGE_CTX_INLINE_ROOTS", raising=False)
    db = tmp_path / "index.db"
    _index_with_instruction_blocks(db)
    wb = tmp_path / "write-better"
    wb.mkdir()
    essentials = wb / "essentials.md"
    essentials.write_text("# Write Better\nsay it once\n")
    body = body_for(essentials)
    for doc_id, fm in [
        (_WB_BLOCK, {"scope": "crew", "binds_sessions": ["interactive"]}),
        (_WB_DISPATCHED_BLOCK, {"scope": "agent", "binds_roles": ["coder"],
                                "binds_sessions": ["dispatched"]}),
    ]:
        _insert_instruction_block_row(db, doc_id, body, fm)
    text = _run_use_through_main(knowledge_ctx, tmp_path, monkeypatch, db, argv)
    return (text, _read_profile_record(tmp_path)["writing_standards"],
            capsys.readouterr().err)


@pytest.mark.parametrize("argv", [
    ["knowledge-ctx", "use", "daytime", "--force", "--force-anyway"],
    ["knowledge-ctx", "use", "daytime", "--agent", "coder", "--force",
     "--force-anyway"],
], ids=["crew", "dispatched"])
def test_render_inlines_an_allowlisted_import(
        knowledge_ctx, tmp_path, monkeypatch, capsys, argv):
    text, ws, _ = _render_with_imports(
        knowledge_ctx, tmp_path, monkeypatch, capsys, argv,
        lambda p: f"@{p}\n")
    essentials = tmp_path / "write-better" / "essentials.md"
    digest = hashlib.sha256(essentials.read_bytes()).hexdigest()
    assert f"<!-- inlined: {essentials.resolve()} sha256:{digest} -->" in text
    assert "# Write Better\nsay it once" in text
    assert not re.search(r"^\s*@/", text, re.M)
    assert ws["write-better"]["composed"] is True


@pytest.mark.parametrize("argv", [
    ["knowledge-ctx", "use", "daytime", "--force", "--force-anyway"],
    ["knowledge-ctx", "use", "daytime", "--agent", "coder", "--force",
     "--force-anyway"],
], ids=["crew", "dispatched"])
def test_render_drops_an_import_outside_the_roots_and_says_not_composed(
        knowledge_ctx, tmp_path, monkeypatch, capsys, argv):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOKEN-VALUE\n")
    text, ws, err = _render_with_imports(
        knowledge_ctx, tmp_path, monkeypatch, capsys, argv,
        lambda p: f"@{secret}\n")
    assert "TOKEN-VALUE" not in text
    assert "inlined:" not in text
    assert not re.search(r"^\s*@/", text, re.M)
    assert str(secret) in err
    assert ws["write-better"]["composed"] is False
    assert str(secret) in ws["write-better"]["reason"]


def test_render_inlines_an_import_in_an_agent_bound_block(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """A crew render appends `binds_agents` blocks to each individuated agent's
    own file; that path inlines too."""
    monkeypatch.setattr(knowledge_ctx, "CONVIVY", tmp_path)
    monkeypatch.delenv("KNOWLEDGE_CTX_INLINE_ROOTS", raising=False)
    wb = tmp_path / "write-better"
    wb.mkdir()
    (wb / "essentials.md").write_text("AGENT-BOUND-STANDARD\n")
    bound = knowledge_ctx.inline_block_imports(
        [_ib_row("platform/instruction-blocks/x", scope="agent",
                 body=f"@{wb / 'essentials.md'}\n")])
    assert "AGENT-BOUND-STANDARD" in bound[0]["body"]
    assert not re.search(r"^\s*@/", bound[0]["body"], re.M)


# --- materialization from the Library, with no knowledge directory ----------
# (issue_01M34SV3BDRHBTXZPZ8C4AH9HD, epic E9)
#
# The profile and each document body come from the Library on every path, so each
# test below runs with `KNOWLEDGE` naming a directory that is never created: no
# `projects/` and no `profiles/` on disk. The index is NOT absent. The autouse
# `_index_declaring_roles` supplies one, and `individuate` still reads it (`_conn()`),
# as do the instruction-block and orphan-id reads. So these tests do not show that a
# machine with no index renders; CQ·371 (issue_01M43AE2QD6H3JZMKM2RR2NK2W) covers that.


def _no_checkout(knowledge_ctx, tmp_path, monkeypatch, docs):
    """The Library serves `deck/daytime` as `docs`, and the knowledge directory is never created."""
    monkeypatch.setattr(knowledge_ctx, "CONVIVY", tmp_path)
    knowledge = tmp_path / "knowledge"
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", knowledge)
    _cut_over_floor(tmp_path / "home")
    return _Library(knowledge_ctx, monkeypatch, tmp_path, docs=docs, profile=docs), knowledge


def test_a_crew_session_renders_its_profile_with_no_profiles_directory_and_an_index_present(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    product_dir = tmp_path / "convivy-deck"
    product_dir.mkdir()
    _git_init(product_dir)
    lib, knowledge = _no_checkout(knowledge_ctx, tmp_path, monkeypatch, _own_docs())
    rc = _run_main(knowledge_ctx, monkeypatch, product_dir, ["--force", "--force-anyway"])
    assert rc == 0, capsys.readouterr().err
    assert (product_dir / ".claude" / "agents" / "deck-coder.md").is_file()
    assert (product_dir / ".claude" / "skills" / "skill-0" / "SKILL.md").is_file()
    assert any(c[0] == "/profiles/deck/daytime" for c in lib.calls)
    assert not knowledge.exists()


def test_a_primary_checkout_dispatch_renders_its_profile_with_no_profiles_directory_and_an_index_present(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    _linked_worktree(tmp_path)   # builds the primary `convivy-deck`, with a `.git` directory
    primary = tmp_path / "convivy-deck"
    assert (primary / ".git").is_dir()
    lib, knowledge = _no_checkout(knowledge_ctx, tmp_path, monkeypatch, _own_docs())
    rc = _run_main(knowledge_ctx, monkeypatch, primary,
                   ["--agent", "deck-coder", "--force", "--force-anyway"])
    assert rc == 0, capsys.readouterr().err
    assert (primary / ".claude" / "agents" / "deck-coder.md").is_file()
    assert [c for c in lib.calls if c[0] == "/bundles"] == []
    assert not knowledge.exists()


def test_a_bundle_dispatch_renders_its_profile_with_no_profiles_directory_and_an_index_present(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    wt = _linked_worktree(tmp_path)
    lib, knowledge = _no_checkout(knowledge_ctx, tmp_path, monkeypatch, _own_docs())
    rc = _run_main(knowledge_ctx, monkeypatch, wt,
                   ["--agent", "deck-coder", "--force", "--force-anyway"])
    assert rc == 0, capsys.readouterr().err
    assert (wt / ".claude" / "agents" / "deck-coder.md").is_file()
    paths = [c[0] for c in lib.calls]
    assert "/profiles/deck/daytime" in paths and "/bundles" in paths
    assert not knowledge.exists()


@pytest.mark.parametrize("argv", [
    ["--force", "--force-anyway"],                              # crew session
    ["--agent", "deck-coder", "--force", "--force-anyway"],     # primary-checkout dispatch
], ids=["crew-session", "primary-checkout"])
def test_an_unreachable_library_fails_a_profile_render_by_naming_it(
        knowledge_ctx, tmp_path, monkeypatch, capsys, argv):
    product_dir = tmp_path / "convivy-deck"
    product_dir.mkdir()
    _git_init(product_dir)
    _no_checkout(knowledge_ctx, tmp_path, monkeypatch, _own_docs())

    def down(req):
        raise urllib.error.URLError(ConnectionRefusedError())
    monkeypatch.setattr(knowledge_ctx, "_library_open", down)
    rc = _run_main(knowledge_ctx, monkeypatch, product_dir, argv)
    err = capsys.readouterr().err
    assert rc == knowledge_ctx.LIBRARY_REFUSED
    assert "the Library" in err and "NOTHING WAS WRITTEN" in err
    assert not (product_dir / ".claude").exists()


def test_a_profile_route_the_library_cannot_answer_fails_the_render_by_naming_it(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """The list answers and the profile route does not: the render still stops, and
    the message names the profile it could not load from the Library."""
    product_dir = tmp_path / "convivy-deck"
    product_dir.mkdir()
    _git_init(product_dir)
    lib, _ = _no_checkout(knowledge_ctx, tmp_path, monkeypatch, _own_docs())
    answer = lib._open

    def profile_route_down(req):
        if urllib.parse.urlparse(req.full_url).path.startswith("/profiles/"):
            raise urllib.error.URLError(ConnectionRefusedError())
        return answer(req)
    monkeypatch.setattr(knowledge_ctx, "_library_open", profile_route_down)
    rc = _run_main(knowledge_ctx, monkeypatch, product_dir, ["--force", "--force-anyway"])
    err = capsys.readouterr().err
    assert rc == knowledge_ctx.LIBRARY_REFUSED
    assert "cannot load profile deck/daytime from the Library" in err
    assert "NOTHING WAS WRITTEN" in err
    assert not (product_dir / ".claude").exists()


def test_an_unreachable_library_fails_a_bundle_dispatch_by_naming_it(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    wt = _linked_worktree(tmp_path)
    _no_checkout(knowledge_ctx, tmp_path, monkeypatch, _own_docs())

    def down(req):
        raise urllib.error.URLError(ConnectionRefusedError())
    monkeypatch.setattr(knowledge_ctx, "_library_open", down)
    rc = _run_main(knowledge_ctx, monkeypatch, wt,
                   ["--agent", "deck-coder", "--force", "--force-anyway"])
    err = capsys.readouterr().err
    assert rc == knowledge_ctx.LIBRARY_REFUSED
    assert "the Library" in err and "NOTHING WAS WRITTEN" in err
    assert not (wt / ".claude").exists()


def test_an_absent_profile_is_a_not_found_that_names_the_library(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    product_dir = tmp_path / "convivy-deck"
    product_dir.mkdir()
    _git_init(product_dir)
    _no_checkout(knowledge_ctx, tmp_path, monkeypatch, _own_docs())
    monkeypatch.chdir(product_dir)
    monkeypatch.setattr("sys.argv", ["knowledge-ctx", "use", "ghost", "--force"])
    with pytest.raises(SystemExit) as exc:
        knowledge_ctx.main()
    assert exc.value.code == 1
    assert "profile not found in the Library: deck/ghost" in capsys.readouterr().err


def test_a_profile_name_from_the_command_line_is_quoted_into_its_route(
        knowledge_ctx, tmp_path, monkeypatch):
    """A `use` argument may hold `/`, `?` or `..`; quoted, it names one profile and
    cannot reshape the request path."""
    lib = _Library(knowledge_ctx, monkeypatch, tmp_path)
    with pytest.raises(SystemExit):
        knowledge_ctx.load_profile("deck", "a/b?c")
    assert lib.calls[0][0] == "/profiles/deck/a%2Fb%3Fc"


def test_the_profile_route_rule_counts_are_what_the_render_reports(
        knowledge_ctx, tmp_path, monkeypatch):
    """`collect_matches` returns the Library's per-rule counts as they came, keyed by
    position, rather than counting anything itself."""
    docs = [_agent_doc()]
    lib = _Library(knowledge_ctx, monkeypatch, tmp_path, docs=docs)
    profile = {"documents": [{k: v for k, v in d.items() if k != "body"} for d in docs],
               "rules": [{"position": 0, "matches": 1}, {"position": 1, "matches": 0}]}
    rows, rule_stats = knowledge_ctx.collect_matches(profile)
    assert [r["id"] for r in rows] == [docs[0]["id"]]
    assert rule_stats == [{"position": 0, "matches": 1}, {"position": 1, "matches": 0}]
    assert lib.calls[0][0] == "/reference/documents/" + docs[0]["id"]


@pytest.mark.parametrize("kind", ["skill", "agent"])
@pytest.mark.parametrize("raw,expected", [
    ("when writing\ntests", "when writing tests"),
    ("when writing\x07tests", "when writingtests"),
    ("x" * 600, "x" * 500),
], ids=["newline", "control-char", "over-500-chars"])
def test_a_trigger_from_the_profile_route_is_bounded_before_it_renders(
        knowledge_ctx, tmp_path, monkeypatch, capsys, kind, raw, expected):
    """The profile route serves each skill's and agent's `trigger` unbounded
    (convivy/library#211). The render must bound it, as a bundle's is bounded."""
    doc = (_bdoc("agent-platform/agents/deck-coder", "agent", "Do the work.\n", trigger=raw)
           if kind == "agent" else
           _bdoc("engineering-practice/skills/skill-0", "skill", "# Tests\n\nWrite them.\n",
                 trigger=raw))
    product_dir = tmp_path / "convivy-deck"
    product_dir.mkdir()
    _git_init(product_dir)
    _no_checkout(knowledge_ctx, tmp_path, monkeypatch, [doc])
    assert _run_main(knowledge_ctx, monkeypatch, product_dir,
                     ["--force", "--force-anyway"]) == 0, capsys.readouterr().err
    rendered = (product_dir / ".claude" / ("agents/deck-coder.md" if kind == "agent"
                                           else "skills/skill-0/SKILL.md")).read_text()
    assert f"description: {expected}\n" in rendered
    assert "\x07" not in rendered


@pytest.mark.parametrize("raw,expected", [
    ("when writing\ntests", "when writing tests"),
    ("when writing\x07tests", "when writingtests"),
    ("x" * 600, "x" * 500),
], ids=["newline", "control-char", "over-500-chars"])
def test_a_profile_route_trigger_is_bounded_on_the_row(
        knowledge_ctx, tmp_path, monkeypatch, raw, expected):
    """The row a profile resolves to carries the bounded trigger, so the untriggered
    decision and the description both see the bounded value."""
    doc = _bdoc("engineering-practice/skills/skill-0", "skill", "# Tests\n\nWrite them.\n",
                trigger=raw)
    _Library(knowledge_ctx, monkeypatch, tmp_path, docs=[doc])
    profile = {"documents": [{k: v for k, v in doc.items() if k != "body"}],
               "rules": [{"position": 0, "matches": 1}]}
    rows, _ = knowledge_ctx.collect_matches(profile)
    assert rows[0]["_bundle"]["fm"] == {"trigger": expected}


def test_no_profile_is_read_from_the_checkout_or_the_local_index_for_selection():
    """The epic's grep, pinned: `bin/knowledge-ctx` selects a profile only through
    the Library. Each of these reads a profile file or a `docs` row, and none may
    appear in the selection path."""
    src = (pathlib.Path(__file__).resolve().parent.parent / "bin" / "knowledge-ctx").read_text()
    for forbidden in ('/ "profiles"', "projects.has_profiles(",
                      "projects.default_profile_path(", "query_rule("):
        assert forbidden not in src, f"knowledge-ctx still reads {forbidden!r}"
    tree = ast.parse(src)
    selection = {"load_profile", "load_profile_or_default", "list_profiles", "_has_profiles",
                 "_default_profile_available", "_library_profile_variants", "collect_matches",
                 "_library_profile_rows", "library_rows", "bundle_rows"}
    checked = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in selection:
            checked.add(node.name)
            body = ast.get_source_segment(src, node)
            for name in (r"\bKNOWLEDGE\b", r"\bDB_PATH\b", r"_conn\("):
                assert not re.search(name, body), f"{node.name} reads {name}"
    assert checked == selection, selection - checked


def test_the_full_corpus_is_read_from_the_library_not_the_local_index():
    """CQ·371 (issue_01M43AE2QD6H3JZMKM2RR2NK2W): `all_agent_and_skill_ids` —
    the full-corpus read `_find_orphans` uses, called on every `use` and by
    `inventory` — dies when `~/.knowledge-index/index.db` is missing on
    origin/main, through `_conn()`. Pinned the same way the selection grep
    above is: the function's own body may never open the local index."""
    src = (pathlib.Path(__file__).resolve().parent.parent / "bin" / "knowledge-ctx").read_text()
    tree = ast.parse(src)
    checked = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "all_agent_and_skill_ids":
            checked.add(node.name)
            body = ast.get_source_segment(src, node)
            for name in (r"\bDB_PATH\b", r"_conn\("):
                assert not re.search(name, body), f"{node.name} reads {name}"
    assert checked == {"all_agent_and_skill_ids"}
