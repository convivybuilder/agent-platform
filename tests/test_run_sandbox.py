"""Unit tests for lib/agent-platform/run_sandbox.py: the sandbox settings every
dispatched run is launched under (Option A, Slate a-ow0q4o).

These check the settings a run receives. `tests/live/test_dispatched_run_confinement.py`
checks that Claude Code enforces them against a real run.
"""

import fnmatch
import functools
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

import run_sandbox as RS

# The Convivy root's place under the passwd home: the Mac's `~/Convivy`, and `~/builder`, the
# root the CI runner falls back to (no CREWS_ROOT config there).
ROOT_LAYOUTS = ("Convivy", "builder")

# Captured before conftest's autouse seam stands in for it, so the ancestry tests test the real check.
_inside_dispatched_run = RS.inside_dispatched_run


def _rule(path: Path) -> str:
    return "//" + str(path).lstrip("/")


def _read_rules(deny: list) -> list:
    return [r for r in deny if r.startswith("Read(")]


def test_the_sandbox_is_on_and_fails_closed(tmp_path):
    s = RS.settings(home=tmp_path, worktree=tmp_path / "wt")["sandbox"]
    assert s["enabled"] is True
    # A run whose sandbox cannot start is refused, not run unsandboxed.
    assert s["failIfUnavailable"] is True
    # The model's `dangerouslyDisableSandbox` parameter is ignored.
    assert s["allowUnsandboxedCommands"] is False


def test_the_grant_directory_is_denied_for_read_and_edit(tmp_path):
    settings = RS.settings(home=tmp_path, worktree=tmp_path / "wt")
    oc = tmp_path / ".orient-control"
    assert str(oc) in settings["sandbox"]["filesystem"]["denyRead"]
    assert f"Read({_rule(oc)}/**)" in settings["permissions"]["deny"]
    assert f"Edit({_rule(oc)}/**)" in settings["permissions"]["deny"]


def test_nothing_is_reopened_inside_the_grant_directory(tmp_path):
    # agent-platform-token carries review_record.create. A run that could read it could
    # post an APPROVE the merge gate reads (knowledge#793), so no file under the grant
    # directory is re-opened for a run's Bash, and the verdict goes through the server.
    # Epic E33 slice 2: `allowRead` now always carries the base read-allowlist (the
    # worktree, caches, installed tools...), but none of it may be the grant
    # directory or anything under it.
    fs = RS.settings(home=tmp_path, worktree=tmp_path / "wt")["sandbox"]["filesystem"]
    oc = tmp_path / ".orient-control"
    assert str(oc) not in fs["allowRead"]
    assert not any(Path(p) == oc or oc in Path(p).parents for p in fs["allowRead"])
    assert str(oc) in fs["denyRead"]


def test_the_denial_covers_the_callers_home_too(tmp_path, monkeypatch):
    # A nested spawn can run with a HOME it chose. The passwd-home grant directory
    # must stay denied whatever HOME the caller had, and so must the caller's own.
    caller_home = tmp_path / "caller-home"
    monkeypatch.setenv("HOME", str(caller_home))
    settings = RS.settings(home=tmp_path / "passwd-home", worktree=tmp_path / "wt")
    assert str(tmp_path / "passwd-home" / ".orient-control") in settings["sandbox"]["filesystem"]["denyRead"]
    assert str(caller_home / ".orient-control") in settings["sandbox"]["filesystem"]["denyRead"]
    deny = settings["permissions"]["deny"]
    assert f"Read({_rule(caller_home / '.orient-control')}/**)" in deny


def test_claude_md_excludes_is_empty_by_default(tmp_path):
    # Round 1 (security-reviewer, PR #823): the exclusion is only safe for a dispatch
    # whose fleet-carrying render actually ran, so settings() must default to NOT
    # excluding — a caller that passes nothing (review_launch.py's call, which never
    # runs that render) gets exactly main's pre-#823 behaviour.
    settings = RS.settings(home=tmp_path, worktree=tmp_path / "wt")
    assert settings["claudeMdExcludes"] == []


def test_claude_md_excludes_names_the_user_scope_file_under_home(tmp_path):
    # Context trim phase 2 (issue_01M4D32KA2F99QGKABZ8HV2RG5): a dispatched run must
    # skip ONLY the operator's user-scope CLAUDE.md, never a project's own — when the
    # caller can show the fleet-carrying render ran (`exclude_user_claude_md=True`).
    settings = RS.settings(home=tmp_path, worktree=tmp_path / "wt",
                           exclude_user_claude_md=True)
    assert str(tmp_path / ".claude" / "CLAUDE.md") in settings["claudeMdExcludes"]
    # Everything else is untouched: hooks, env and permissions are a different
    # settings layer entirely, and this key sits beside "sandbox"/"permissions",
    # never inside either.
    assert "claudeMdExcludes" not in settings["sandbox"]
    assert "claudeMdExcludes" not in settings["permissions"]


def test_claude_md_excludes_covers_the_callers_home_too(tmp_path, monkeypatch):
    # Same two-homes rule as the grant-directory denial above: a nested spawn's
    # own HOME must lose its user CLAUDE.md as well as the passwd home's.
    caller_home = tmp_path / "caller-home"
    monkeypatch.setenv("HOME", str(caller_home))
    settings = RS.settings(home=tmp_path / "passwd-home", worktree=tmp_path / "wt",
                           exclude_user_claude_md=True)
    excludes = settings["claudeMdExcludes"]
    assert str(tmp_path / "passwd-home" / ".claude" / "CLAUDE.md") in excludes
    assert str(caller_home / ".claude" / "CLAUDE.md") in excludes


def test_claude_md_excludes_never_names_a_worktrees_own_claude_md(tmp_path):
    # The project's own CLAUDE.md, and the render `knowledge-ctx use` writes to
    # the worktree's `.claude/CLAUDE.md`, must keep loading — a different
    # absolute path from the excluded user-scope file, never swept in by the
    # same name under a different parent.
    wt = tmp_path / "wt"
    excludes = RS.settings(home=tmp_path, worktree=wt,
                           exclude_user_claude_md=True)["claudeMdExcludes"]
    assert str(wt / ".claude" / "CLAUDE.md") not in excludes


def test_worktree_settings_and_mcp_files_cannot_be_written(tmp_path):
    wt = tmp_path / "wt"
    settings = RS.settings(home=tmp_path, worktree=wt)
    deny_write = settings["sandbox"]["filesystem"]["denyWrite"]
    for name in (".claude/settings.json", ".claude/settings.local.json", ".mcp.json"):
        assert str(wt / name) in deny_write
        # The Edit and Write tools run outside the sandbox, so they need the rule too.
        assert f"Edit({_rule(wt / name)})" in settings["permissions"]["deny"]


def test_home_settings_hooks_and_launch_agents_cannot_be_edited(tmp_path):
    deny = RS.settings(home=tmp_path, worktree=tmp_path / "wt")["permissions"]["deny"]
    assert f"Edit({_rule(tmp_path / '.claude/settings.json')})" in deny
    assert f"Edit({_rule(tmp_path / '.claude/settings.local.json')})" in deny
    assert f"Edit({_rule(tmp_path / '.claude.json')})" in deny
    assert f"Edit({_rule(tmp_path / 'Library/LaunchAgents')}/**)" in deny


def test_the_network_is_a_strict_allowlist(tmp_path):
    net = RS.settings(home=tmp_path, worktree=tmp_path / "wt")["sandbox"]["network"]
    assert net["strictAllowlist"] is True
    assert "github.com" in net["allowedDomains"]
    assert net["allowMachLookup"] == ["com.apple.trustd.agent"]


def test_port_22_is_denied_for_a_default_coder_dispatch(tmp_path):
    # The proxy admits github.com on any port, which is what let run-c36c0559
    # tunnel an SSH push to github.com:22 through it on 2026-10-19 (CQ·506 step S8).
    net = RS.settings(home=tmp_path, worktree=tmp_path / "wt")["sandbox"]["network"]
    assert net["deniedDomains"] == ["*:22"]


def test_port_22_is_denied_for_a_review_class_dispatch(tmp_path):
    net = RS.settings(home=tmp_path, worktree=tmp_path / "wt",
                      review_allowed=("Read",))["sandbox"]["network"]
    assert net["deniedDomains"] == ["*:22"]


def test_port_22_is_denied_for_a_postgres_socket_repo_dispatch(tmp_path, monkeypatch):
    monkeypatch.setattr(RS, "postgres_socket_repos", lambda: ("acme",))
    net = RS.settings(home=tmp_path, worktree=tmp_path / "wt",
                      repo="acme")["sandbox"]["network"]
    assert net["deniedDomains"] == ["*:22"]


def test_a_flagged_repo_dispatch_gets_the_agent_postgres_socket_and_still_denies_local_binding(
        tmp_path, monkeypatch):
    # Round-1 revise on PR #817: a product coder's test suite needs
    # the AGENT cluster's socket, never the operator's own cluster's. `allowLocalBinding` must
    # stay absent — opening it would open every local TCP port, the loopback Library
    # included — so only the named socket path is granted.
    monkeypatch.setattr(RS, "postgres_socket_repos", lambda: ("acme",))
    net = RS.settings(home=tmp_path, worktree=tmp_path / "wt", repo="acme")["sandbox"]["network"]
    assert net["allowUnixSockets"] == [RS.agent_postgres_socket_path(tmp_path)]
    assert "allowLocalBinding" not in net


def test_importing_run_sandbox_survives_an_unreadable_registry(tmp_path):
    """`postgres_socket_repos()`'s registry read must happen when `settings()`
    calls it, never at module load: an import-time read turns a missing or
    unreadable config/projects.json into an ImportError for every caller of
    this module, which is exactly what broke
    tests/test_agentctl_module_resolution.py's per-checkout copies (no
    sibling config/ at all) when `POSTGRES_SOCKET_REPOS` used to be computed
    as a module-level constant. `HOME` is pointed at an empty directory so
    `cost_join.py`'s own `sys.path.insert` (and anything else keyed off
    `Path.home()`) cannot reach this machine's real ~/.local farm instead of
    the registry this test controls."""
    empty_home = tmp_path / "empty_home"
    empty_home.mkdir()
    env = dict(os.environ)
    env["HOME"] = str(empty_home)
    env["PROJECTS_JSON"] = str(tmp_path / "no-such-file.json")
    lib_dir = Path(__file__).resolve().parent.parent / "lib" / "agent-platform"
    result = subprocess.run(
        [sys.executable, "-c", f"import sys; sys.path.insert(0, {str(lib_dir)!r}); "
                                "import run_sandbox"],
        capture_output=True, text=True, timeout=30, env=env,
    )
    assert result.returncode == 0, result.stderr


def test_the_agent_postgres_socket_sits_under_the_account_home_not_tmp(tmp_path):
    # Round 1's reviewers both flagged that nothing shows whether Seatbelt's
    # `(remote unix-socket (subpath ...))` resolves the /tmp -> /private/tmp
    # symlink the same way the filesystem predicates do (confirmed only for
    # filesystem rules — see SCRATCH_ROOTS's comment). The fix is to never put
    # this socket under /tmp at all: it sits under the account home instead,
    # where neither this nor any ancestor component is a symlink on this
    # machine (checked by hand; see the comment above AGENT_POSTGRES_RELDIR).
    # Checked against the relative part only (not `tmp_path` itself, which in
    # this sandbox sits under /tmp for unrelated reasons and would make a
    # whole-string check pass or fail on the test fixture, not on this code).
    reldir = str(RS.AGENT_POSTGRES_RELDIR)
    assert "tmp" not in reldir.split("/")
    assert RS.agent_postgres_socket_dir(tmp_path) == tmp_path / RS.AGENT_POSTGRES_RELDIR / "sockets"


def test_a_dispatch_into_any_other_repo_is_byte_for_byte_unchanged(tmp_path):
    baseline = RS.settings_json(home=tmp_path, worktree=tmp_path / "wt")
    assert RS.settings_json(home=tmp_path, worktree=tmp_path / "wt", repo=None) == baseline
    for repo in ("globex", "agent-platform", "LEDGER", "initech"):
        assert RS.settings_json(home=tmp_path, worktree=tmp_path / "wt", repo=repo) == baseline


def test_trial_turns_off_the_harness_commit_and_pr_attribution(tmp_path):
    """Blind model trial (operator approval 2026-10-18): `trial=True` adds the
    two keys that turn off the Claude Code harness's own commit/PR attribution
    on the installed 2.1.302 release — the newer `attribution` key (empty
    string disables each surface) and the older `includeCoAuthoredBy` boolean
    the same release still reads."""
    settings = RS.settings(home=tmp_path, worktree=tmp_path / "wt", trial=True)
    assert settings["attribution"] == {"commit": "", "pr": ""}
    assert settings["includeCoAuthoredBy"] is False


def test_trial_false_is_byte_for_byte_unchanged_from_before_the_parameter_existed(tmp_path):
    """`trial` defaults to False, and when False the two keys above are absent
    altogether — not `null` — so `settings_json`'s output for every dispatch
    that never passes `trial` stays byte-identical to before this parameter
    was added."""
    baseline = RS.settings_json(home=tmp_path, worktree=tmp_path / "wt")
    assert RS.settings_json(home=tmp_path, worktree=tmp_path / "wt", trial=False) == baseline
    settings = RS.settings(home=tmp_path, worktree=tmp_path / "wt")
    assert "attribution" not in settings
    assert "includeCoAuthoredBy" not in settings


def test_trial_denies_reading_the_whole_of_runs_root(tmp_path):
    """agent-platform#934 round 2 (reviewer finding): round 1's enumeration of
    four named files under `run_dir` missed two more sources on the same
    default dispatch path that name a trial run's true model —
    `<run_dir>/detach.out.log`/`detach.err.log` (the detached dispatch's own
    live transcript copy) and `MODEL_OVERRIDES_LOG`
    (`runs_root/model-overrides.jsonl`, which sits directly under `runs_root`,
    never under any one run's `run_dir`) — and an enumeration keyed on
    `run_dir` alone can never close either. Denying the WHOLE of `runs_root`
    for reading covers all three by PLACE, in both layers this module pairs
    for every other credential path. Checked against the real rendered rules
    with `fnmatch`, never against a hand-built rule string alone — a literal
    `/**`-suffixed string could pass this assertion while still failing to
    cover a real path one directory deeper."""
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "2026-10-18" / "run-trial-1"
    settings = RS.settings(home=tmp_path, worktree=tmp_path / "wt",
                           runs_root=runs_root, trial=True)
    deny_read = settings["sandbox"]["filesystem"]["denyRead"]
    perm_deny = settings["permissions"]["deny"]
    read_rules = [r[len("Read("):-1] for r in perm_deny
                 if r.startswith("Read(") and r.endswith(")")]
    for candidate in (run_dir / "detach.out.log", run_dir / "detach.err.log",
                     runs_root / "model-overrides.jsonl"):
        # `denied_dirs` entries (this one included) are bare directory paths,
        # never a glob, the same shape `test_the_grant_directory_is_denied_
        # for_read_and_edit` already checks — the sandbox's own `(subpath
        # ...)` semantics are what make a bare directory entry cover every
        # path beneath it, so the real check is "is a subpath of", not a
        # literal glob match.
        assert any(candidate.is_relative_to(Path(p)) for p in deny_read), \
            f"no sandbox denyRead entry covers {candidate}: {deny_read}"
        assert any(fnmatch.fnmatch(RS._rule_path(candidate), r) for r in read_rules), \
            f"no permissions.deny Read(...) rule covers {candidate}: {perm_deny}"


def test_trial_without_runs_root_adds_no_whole_root_deny_rule(tmp_path):
    """A `--trial` dispatch given no `runs_root` gets no whole-root deny rule —
    there is nothing to point it at, the same shape every other
    `runs_root`-gated rule in this module already takes. Compared against a
    non-trial baseline, not asserted empty outright: `denyRead`/`deny` already
    carry the home-credential rules (`DENIED_DIRS`/`DENIED_HOME_FILES`)
    regardless of `trial`, and this must add nothing on top of those."""
    baseline = RS.settings(home=tmp_path, worktree=tmp_path / "wt")
    settings = RS.settings(home=tmp_path, worktree=tmp_path / "wt", trial=True)
    assert (settings["sandbox"]["filesystem"]["denyRead"]
           == baseline["sandbox"]["filesystem"]["denyRead"])
    assert (_read_rules(settings["permissions"]["deny"])
           == _read_rules(baseline["permissions"]["deny"]))


def test_a_non_trial_dispatch_with_runs_root_adds_no_whole_root_read_deny(tmp_path):
    """A non-trial dispatch's `denyRead`/`permissions.deny` must carry no
    whole-`runs_root` Read rule even when `runs_root` is given — `trial=False`
    never reads the new behavior at all, the same contract every other
    `trial`-gated addition to this function already keeps. Compared against
    the same dispatch with `runs_root` omitted, since the home-credential
    Read rules stay present either way and must not be mistaken for this
    one."""
    runs_root = tmp_path / "runs"
    without_root = RS.settings(home=tmp_path, worktree=tmp_path / "wt")
    settings = RS.settings(home=tmp_path, worktree=tmp_path / "wt", runs_root=runs_root)
    assert (settings["sandbox"]["filesystem"]["denyRead"]
           == without_root["sandbox"]["filesystem"]["denyRead"])
    assert (_read_rules(settings["permissions"]["deny"])
           == _read_rules(without_root["permissions"]["deny"]))
    assert (RS.settings(home=tmp_path, worktree=tmp_path / "wt", runs_root=runs_root, trial=False)
           == settings)


def test_network_allowed_is_unchanged_for_every_repo(tmp_path):
    for repo in (None, "widget", "globex"):
        net = RS.settings(home=tmp_path, worktree=tmp_path / "wt", repo=repo)["sandbox"]["network"]
        assert net["allowedDomains"] == list(RS.NETWORK_ALLOWED)
    assert RS.NETWORK_ALLOWED == ("github.com", "api.github.com", "pypi.org", "files.pythonhosted.org")


def test_the_agent_runs_root_is_denied_for_writing_by_a_run(tmp_path):
    # A run's meta.json is the record its verdict is checked against. A run that could
    # write under the runs root could rewrite it, so the root is denied for Bash and for
    # the Edit tool, and it is not in the write allow list.
    runs = tmp_path / "agent-runs"
    settings = RS.settings(home=tmp_path, worktree=tmp_path / "wt", runs_root=runs)
    fs = settings["sandbox"]["filesystem"]
    assert str(runs) in fs["denyWrite"]
    assert f"Edit({_rule(runs)}/**)" in settings["permissions"]["deny"]
    assert str(runs) not in fs["allowWrite"]
    # tmux's socket directory lives under /tmp, which resolves to /private/tmp on macOS and
    # to /tmp on Linux, so the expected spelling is the platform's own.
    assert "/tmp" in fs["allowWrite"]
    assert os.path.realpath("/tmp") in fs["allowWrite"]
    # The grant directory is denied for reading whatever the writable roots are.
    assert str(tmp_path / ".orient-control") in fs["denyRead"]


def test_the_tmux_socket_directory_is_denied_under_every_temp_root(tmp_path, monkeypatch):
    # knowledge#793 condition 4: a run that rebound or renamed a socket onto the operator's
    # default one would get the next connection a co's tooling makes.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "scratch-tmp"))
    fs = RS.settings(home=tmp_path, worktree=tmp_path / "wt")["sandbox"]["filesystem"]
    deny = RS.settings(home=tmp_path, worktree=tmp_path / "wt")["permissions"]["deny"]
    uid = os.getuid()
    roots = {os.path.realpath(tempfile.gettempdir()), "/tmp", os.path.realpath("/tmp")}
    assert len(roots) > 1, "the test needs at least two distinct roots to show each is covered"
    for root in roots:
        tmux_dir = Path(root) / f"tmux-{uid}"
        assert str(tmux_dir) in fs["denyWrite"], root
        assert f"Edit({_rule(tmux_dir)}/**)" in deny, root
        # Still inside an allowed root, so the deny rule is the only thing stopping a write.
        assert str(root) in fs["allowWrite"]


def test_create_eval_scratch_dir_makes_an_owner_only_directory(tmp_path, monkeypatch):
    # agent-platform#813 round 8 (round-7 finding): mode 0700 is mkdtemp's own
    # default, kept rather than loosened, and the directory sits under the
    # resolved system temp root like the old shared path did.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    d = RS.create_eval_scratch_dir()
    try:
        assert d.is_dir()
        assert d.is_relative_to(tmp_path)
        assert stat.S_IMODE(d.stat().st_mode) == 0o700
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_two_eval_scratch_dirs_are_distinct_and_unguessable_from_each_other(tmp_path, monkeypatch):
    # Round-7 finding: several eval arms run against the same corpus entry at once, so
    # two dispatches must never land on the same directory, and neither name may be
    # derivable from the other (both are mkdtemp's own random suffix, not a counter).
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    a = RS.create_eval_scratch_dir()
    b = RS.create_eval_scratch_dir()
    try:
        assert a != b
        assert a.name != b.name
    finally:
        shutil.rmtree(a, ignore_errors=True)
        shutil.rmtree(b, ignore_errors=True)


def test_create_trial_zdotdir_base_makes_an_owner_only_directory_outside_runs_root(
        tmp_path, monkeypatch):
    """agent-platform#934 round 2 (reviewer finding): a `--trial` dispatch's own
    ZDOTDIR base must sit outside `runs_root` (its own `settings()` now denies
    reading all of it) and outside the worktree, mode 0700 — the same shape
    `create_eval_scratch_dir` already uses for the same reason."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    runs_root = tmp_path / "runs"
    worktree = tmp_path / "wt"
    d = RS.create_trial_zdotdir_base()
    try:
        assert d.is_dir()
        assert d.is_relative_to(tmp_path)
        assert not d.is_relative_to(runs_root)
        assert not d.is_relative_to(worktree)
        assert stat.S_IMODE(d.stat().st_mode) == 0o700
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_an_eval_dispatchs_settings_grant_only_its_own_directory(tmp_path):
    # agent-platform#813 round 8 (round-7 finding): an eval dispatch must not get the
    # shared temp root at all — only its own directory — so a sibling eval arm sharing
    # that root cannot plant or overwrite this dispatch's verdict. runs_root stays
    # denied exactly as it is for every other dispatch.
    runs = tmp_path / "agent-runs"
    scratch_dir = tmp_path / "scratch-dir" / "agentctl-eval-xyz"
    scratch_dir.mkdir(parents=True)
    settings = RS.settings(home=tmp_path, worktree=tmp_path / "wt", runs_root=runs,
                           eval_scratch_dir=scratch_dir)
    fs = settings["sandbox"]["filesystem"]
    assert str(scratch_dir) in fs["allowWrite"]
    assert f"Edit({_rule(scratch_dir)}/**)" in settings["permissions"]["allow"]
    assert "/tmp" not in fs["allowWrite"]
    assert os.path.realpath("/tmp") not in fs["allowWrite"]
    assert str(runs) in fs["denyWrite"]
    assert str(runs) not in fs["allowWrite"]


def test_a_non_eval_dispatchs_settings_are_unchanged_by_eval_scratch_dir_support(tmp_path):
    # Adding the `eval_scratch_dir` parameter must not perturb an ordinary dispatch's
    # settings at all: omitting it (the default) must produce byte-identical output to
    # before this parameter existed.
    runs = tmp_path / "agent-runs"
    with_param = RS.settings(home=tmp_path, worktree=tmp_path / "wt", runs_root=runs,
                             eval_scratch_dir=None)
    without_param = RS.settings(home=tmp_path, worktree=tmp_path / "wt", runs_root=runs)
    assert with_param == without_param
    fs = with_param["sandbox"]["filesystem"]
    assert "/tmp" in fs["allowWrite"]
    assert os.path.realpath("/tmp") in fs["allowWrite"]


def test_two_eval_dispatches_settings_do_not_cover_each_others_directory(tmp_path):
    # Neither dispatch's allow-list may reach the other's directory — the whole point
    # of confining each arm to its own scratch directory rather than the shared root.
    dir_a = tmp_path / "scratch" / "agentctl-eval-a"
    dir_b = tmp_path / "scratch" / "agentctl-eval-b"
    dir_a.mkdir(parents=True)
    dir_b.mkdir(parents=True)
    settings_a = RS.settings(home=tmp_path, worktree=tmp_path / "wt", eval_scratch_dir=dir_a)
    settings_b = RS.settings(home=tmp_path, worktree=tmp_path / "wt", eval_scratch_dir=dir_b)
    assert str(dir_b) not in settings_a["sandbox"]["filesystem"]["allowWrite"]
    assert str(dir_a) not in settings_b["sandbox"]["filesystem"]["allowWrite"]
    assert f"Edit({_rule(dir_b)}/**)" not in settings_a["permissions"]["allow"]
    assert f"Edit({_rule(dir_a)}/**)" not in settings_b["permissions"]["allow"]


def test_an_eval_dispatchs_settings_deny_read_of_every_sibling_eval_directory(
        tmp_path, monkeypatch):
    """Round 9 (agent-platform#813 round 8 finding, security-reviewer): round 8
    confined the WRITE but not the READ — several eval arms run against the same
    corpus entry at once, and nothing stopped a sibling arm's sandboxed Bash or its
    Read/Glob/Grep tools from reaching into another arm's `create_eval_scratch_dir`
    directory. This dispatch's own `denyRead` must cover a sibling's real directory
    (not just a hand-built path sharing the prefix by assertion), and `allowRead`
    must carve its own back out and name no one else's. Fails against 65f84957,
    which has no `allowRead` key and no eval entry in `denyRead` at all.

    Epic E33 slice 2: `allowRead` now always carries the base read-allowlist too, so
    this checks containment (`own` is in it, `sibling` is not) rather than exact
    equality."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    root = os.path.realpath(tempfile.gettempdir())
    own = RS.create_eval_scratch_dir()
    sibling = RS.create_eval_scratch_dir()
    try:
        settings = RS.settings(home=tmp_path, worktree=tmp_path / "wt", eval_scratch_dir=own)
        fs = settings["sandbox"]["filesystem"]
        assert f"{root}/{RS.EVAL_SCRATCH_PREFIX}*" in fs["denyRead"]
        covering = [p for p in fs["denyRead"] if fnmatch.fnmatch(str(sibling), p)]
        assert covering, f"no denyRead entry covers the sibling's real directory: {fs['denyRead']}"
        assert fnmatch.fnmatch(str(own), covering[0]), \
            "the same glob must cover this dispatch's own directory too — allowRead is what carves it back out"
        assert str(own) in fs["allowRead"]
        assert str(sibling) not in fs["allowRead"]
    finally:
        shutil.rmtree(own, ignore_errors=True)
        shutil.rmtree(sibling, ignore_errors=True)


def test_two_real_eval_dispatches_settings_cross_deny_each_others_read(
        tmp_path, monkeypatch):
    """The real `settings()` output for two separate real eval dispatches (not a
    hand-built settings dict): each one's `denyRead` glob covers the OTHER's real
    directory, and each one's `allowRead` names its own but never the other's."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    dir_a = RS.create_eval_scratch_dir()
    dir_b = RS.create_eval_scratch_dir()
    try:
        settings_a = RS.settings(home=tmp_path, worktree=tmp_path / "wt", eval_scratch_dir=dir_a)
        settings_b = RS.settings(home=tmp_path, worktree=tmp_path / "wt", eval_scratch_dir=dir_b)
        fs_a = settings_a["sandbox"]["filesystem"]
        fs_b = settings_b["sandbox"]["filesystem"]
        assert any(fnmatch.fnmatch(str(dir_b), p) for p in fs_a["denyRead"]), \
            f"A's denyRead does not cover B's real directory: {fs_a['denyRead']}"
        assert any(fnmatch.fnmatch(str(dir_a), p) for p in fs_b["denyRead"]), \
            f"B's denyRead does not cover A's real directory: {fs_b['denyRead']}"
        assert str(dir_a) in fs_a["allowRead"]
        assert str(dir_b) in fs_b["allowRead"]
        assert str(dir_b) not in fs_a["allowRead"]
        assert str(dir_a) not in fs_b["allowRead"]
    finally:
        shutil.rmtree(dir_a, ignore_errors=True)
        shutil.rmtree(dir_b, ignore_errors=True)


def test_a_non_eval_dispatchs_settings_have_no_eval_read_rules(tmp_path):
    """Adding the read-side narrowing must not perturb an ordinary dispatch's
    EVAL-specific rules: no eval-prefixed glob in `denyRead`, matching the write
    side's own "byte for byte unchanged" contract
    (test_a_non_eval_dispatchs_settings_are_unchanged_by_eval_scratch_dir_support).
    `allowRead` itself is populated for every dispatch since epic E33 slice 2 (see
    test_an_ordinary_dispatch_gets_the_base_read_allowlist_on_darwin), so this no longer
    checks for its absence."""
    settings = RS.settings(home=tmp_path, worktree=tmp_path / "wt", eval_scratch_dir=None)
    fs = settings["sandbox"]["filesystem"]
    assert not any(RS.EVAL_SCRATCH_PREFIX in p for p in fs["denyRead"])
    assert not any(RS.EVAL_SCRATCH_PREFIX in p for p in settings["permissions"]["deny"])


# --- epic E33 slice 2: dispatched runs read through one allowlist --------------

def test_an_ordinary_dispatch_gets_the_base_read_allowlist_on_darwin(tmp_path, monkeypatch):
    """Pinned against the current `READ_ALLOWLIST_RELDIRS`/`READ_ALLOWLIST_RELFILES`
    on macOS, byte for byte, whatever host this suite itself runs on
    (`_dispatch_host_platform` pinned to `"darwin"` here rather than left to the
    real `sys.platform` — CI's own pytest job runs on Linux). See
    test_an_ordinary_dispatch_on_linux_omits_the_claude_codes_own_reopened_dirs for
    the Linux-only exception this pins as absent here."""
    monkeypatch.setattr(RS.BR, "real_account_home", lambda: tmp_path)
    monkeypatch.setattr(RS.BR, "root", lambda: tmp_path / "convivy")
    monkeypatch.setattr(RS, "_dispatch_host_platform", lambda: "darwin")
    home = tmp_path / "home"
    worktree = tmp_path / "wt"
    s = RS.settings(home=home, worktree=worktree)
    fs = s["sandbox"]["filesystem"]
    allow_read = fs["allowRead"]

    assert str(worktree) in allow_read
    for d in RS.READ_ALLOWLIST_RELDIRS:
        assert str(home / d) in allow_read
    for f in RS.READ_ALLOWLIST_RELFILES:
        assert str(home / f) in allow_read
    for p in RS._primary_checkout_read_dirs():
        assert str(p) in allow_read
    # An ordinary dispatch's file tools treat the shared temp roots as working
    # directories too, the same reach its allowWrite already has.
    for root in RS.SCRATCH_ROOTS:
        assert str(Path(root)) in allow_read

    # Both layers render the exact same list (`settings()`'s own contract).
    assert s["permissions"]["additionalDirectories"] == allow_read
    assert s["permissions"]["blockReadsOutsideWorkingDirectories"] is True


def test_an_ordinary_dispatch_on_linux_omits_the_claude_codes_own_reopened_dirs(
        tmp_path, monkeypatch):
    """On Linux, `.claude/session-env` and `.claude/shell-snapshots` are dropped
    from the render — the installed binary re-opens both itself under its own
    read-only bwrap bind (`LINUX_SELF_REOPENED_RELDIRS`'s own comment has the
    measurement), and this module asking bwrap to open them too is what crashed
    every Bash call on the adopter's Debian 12 install. Every other
    `READ_ALLOWLIST_RELDIRS`/`READ_ALLOWLIST_RELFILES` entry, and the worktree
    and primary-checkout dirs, are unaffected — this is a two-entry omission,
    not a narrower allowlist overall."""
    monkeypatch.setattr(RS.BR, "real_account_home", lambda: tmp_path)
    monkeypatch.setattr(RS.BR, "root", lambda: tmp_path / "convivy")
    monkeypatch.setattr(RS, "_dispatch_host_platform", lambda: "linux")
    home = tmp_path / "home"
    worktree = tmp_path / "wt"
    s = RS.settings(home=home, worktree=worktree)
    fs = s["sandbox"]["filesystem"]
    allow_read = fs["allowRead"]

    assert str(worktree) in allow_read
    for d in RS.LINUX_SELF_REOPENED_RELDIRS:
        assert str(home / d) not in allow_read
    for d in RS.READ_ALLOWLIST_RELDIRS:
        if d in RS.LINUX_SELF_REOPENED_RELDIRS:
            continue
        assert str(home / d) in allow_read
    for f in RS.READ_ALLOWLIST_RELFILES:
        assert str(home / f) in allow_read
    for p in RS._primary_checkout_read_dirs():
        assert str(p) in allow_read

    # Both layers still render the exact same (shorter) list.
    assert s["permissions"]["additionalDirectories"] == allow_read


def test_darwin_and_linux_read_allowlists_differ_only_by_the_two_reopened_dirs(
        tmp_path, monkeypatch):
    """Direct comparison of the two platforms' own renders, rather than two
    separate pinned lists that could each drift independently: the Linux list is
    exactly the darwin list with `LINUX_SELF_REOPENED_RELDIRS` removed, nothing
    added and nothing else dropped."""
    monkeypatch.setattr(RS.BR, "real_account_home", lambda: tmp_path)
    monkeypatch.setattr(RS.BR, "root", lambda: tmp_path / "convivy")
    home = tmp_path / "home"
    worktree = tmp_path / "wt"

    monkeypatch.setattr(RS, "_dispatch_host_platform", lambda: "darwin")
    darwin_allow_read = RS.read_allowlist(home=home, worktree=worktree)
    monkeypatch.setattr(RS, "_dispatch_host_platform", lambda: "linux")
    linux_allow_read = RS.read_allowlist(home=home, worktree=worktree)

    removed = {home / d for d in RS.LINUX_SELF_REOPENED_RELDIRS}
    assert set(darwin_allow_read) - set(linux_allow_read) == removed
    assert set(linux_allow_read) - set(darwin_allow_read) == set()


def test_the_claude_projects_entry_is_keyed_on_the_resolved_worktree(tmp_path, monkeypatch):
    """`read_allowlist` adds `<home>/.claude/projects/<slug>` so a run can read back
    Claude Code's own spilled tool output for its project — but Claude Code computes
    that slug from the RESOLVED working directory, so a `worktree` reached through a
    symlink must be resolved before the slug is taken, or the entry names the wrong
    directory. A symlinked worktree makes the resolved and unresolved paths (and so
    their slugs) differ, which catches either `.resolve()` being dropped or the
    `"projects"` path segment being misspelt."""
    monkeypatch.setattr(RS.BR, "real_account_home", lambda: tmp_path)
    monkeypatch.setattr(RS.BR, "root", lambda: tmp_path / "convivy")
    home = tmp_path / "home"
    real_worktree = tmp_path / "real_worktree"
    real_worktree.mkdir()
    linked_worktree = tmp_path / "linked_worktree"
    linked_worktree.symlink_to(real_worktree)

    resolved_slug = RS._project_slug(str(real_worktree.resolve()))
    unresolved_slug = RS._project_slug(str(linked_worktree))
    assert resolved_slug != unresolved_slug

    s = RS.settings(home=home, worktree=linked_worktree)
    allow_read = s["sandbox"]["filesystem"]["allowRead"]
    projects_root = home / ".claude" / "projects"

    entry = projects_root / resolved_slug
    assert str(entry) in allow_read
    assert entry.parent == projects_root
    assert str(projects_root / unresolved_slug) not in allow_read


def test_the_builder_root_marker_file_is_allowed_but_not_its_parent_directory(tmp_path, monkeypatch):
    """An in-run `agentctl` call's own `BR.report_unconfirmed` reads
    `BR.root() / BR.MARKER` on every `runs_root()`/`data_root()` call. Only the
    marker FILE is granted, the same shape as `tool_session_marker`: a run has
    no reason to list everything else `BR.root()` holds."""
    monkeypatch.setattr(RS.BR, "real_account_home", lambda: tmp_path)
    monkeypatch.setattr(RS.BR, "root", lambda: tmp_path / "convivy")
    s = RS.settings(home=tmp_path / "home", worktree=tmp_path / "wt")
    allow_read = s["sandbox"]["filesystem"]["allowRead"]
    marker = RS.BR.root() / RS.BR.MARKER
    assert str(marker) in allow_read
    assert str(marker.parent) not in allow_read


def test_the_home_directory_itself_is_denied_for_every_dispatch_class(tmp_path, monkeypatch):
    monkeypatch.setattr(RS.BR, "real_account_home", lambda: tmp_path)
    monkeypatch.setattr(RS.BR, "root", lambda: tmp_path / "convivy")
    home = tmp_path / "home"
    worktree = tmp_path / "wt"
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "2026-10-25" / "run-x"
    run_dir.mkdir(parents=True)
    eval_dir = tmp_path / "eval-scratch"
    eval_dir.mkdir()

    ordinary = RS.settings(home=home, worktree=worktree)
    ev = RS.settings(home=home, worktree=worktree, eval_scratch_dir=eval_dir)
    review = RS.settings(home=home, worktree=worktree, review_allowed=("Read",),
                         draft_dir=tmp_path / "draft")
    trial = RS.settings(home=home, worktree=worktree, trial=True, runs_root=runs_root,
                        run_dir=run_dir)
    for dispatch in (ordinary, ev, review, trial):
        assert str(home) in dispatch["sandbox"]["filesystem"]["denyRead"]


def test_an_eval_dispatch_gets_only_its_own_scratch_dir_for_the_file_tools_too(
        tmp_path, monkeypatch):
    monkeypatch.setattr(RS.BR, "real_account_home", lambda: tmp_path)
    monkeypatch.setattr(RS.BR, "root", lambda: tmp_path / "convivy")
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    own = RS.create_eval_scratch_dir()
    sibling = RS.create_eval_scratch_dir()
    try:
        s = RS.settings(home=tmp_path, worktree=tmp_path / "wt", eval_scratch_dir=own)
        allow_read = s["sandbox"]["filesystem"]["allowRead"]
        assert str(own) in allow_read
        assert str(sibling) not in allow_read
        # Confined to its own directory, never the shared temp roots an ordinary
        # dispatch's file tools get — the read side mirrors the write side's own
        # `eval_scratch_dir` narrowing.
        for root in RS.SCRATCH_ROOTS:
            assert str(Path(root)) not in allow_read
    finally:
        shutil.rmtree(own, ignore_errors=True)
        shutil.rmtree(sibling, ignore_errors=True)


def test_a_review_dispatch_gets_its_draft_dir_not_the_shared_scratch_roots(tmp_path, monkeypatch):
    monkeypatch.setattr(RS.BR, "real_account_home", lambda: tmp_path)
    monkeypatch.setattr(RS.BR, "root", lambda: tmp_path / "convivy")
    draft = tmp_path / "draft"
    s = RS.settings(home=tmp_path, worktree=tmp_path / "wt",
                    review_allowed=("Read", "Grep"), draft_dir=draft)
    allow_read = s["sandbox"]["filesystem"]["allowRead"]
    assert str(draft) in allow_read
    for root in RS.SCRATCH_ROOTS:
        assert str(Path(root)) not in allow_read


def test_an_ordinary_dispatch_reopens_its_own_run_dir_never_the_runs_root(tmp_path, monkeypatch):
    monkeypatch.setattr(RS.BR, "real_account_home", lambda: tmp_path)
    monkeypatch.setattr(RS.BR, "root", lambda: tmp_path / "convivy")
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "2026-10-25" / "run-x"
    run_dir.mkdir(parents=True)
    s = RS.settings(home=tmp_path, worktree=tmp_path / "wt", runs_root=runs_root, run_dir=run_dir)
    allow_read = s["sandbox"]["filesystem"]["allowRead"]
    assert str(run_dir) in allow_read
    assert str(runs_root) not in allow_read


def test_a_trial_dispatch_never_reopens_its_own_run_dir(tmp_path, monkeypatch):
    # "trial runs stay as they are" (epic E33 slice 2's own plan): a trial
    # dispatch's run directory is dropped from the allowlist even when the caller
    # passes one, since its own `.zshenv` already lives outside `runs_root`
    # (`create_trial_zdotdir_base`) and has no read this dispatch's tool shells need.
    monkeypatch.setattr(RS.BR, "real_account_home", lambda: tmp_path)
    monkeypatch.setattr(RS.BR, "root", lambda: tmp_path / "convivy")
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "2026-10-25" / "run-x"
    run_dir.mkdir(parents=True)
    s = RS.settings(home=tmp_path, worktree=tmp_path / "wt", trial=True,
                    runs_root=runs_root, run_dir=run_dir)
    allow_read = s["sandbox"]["filesystem"]["allowRead"]
    assert str(run_dir) not in allow_read


def test_no_read_allowlist_entry_contains_or_equals_a_denied_path(tmp_path, monkeypatch):
    """No entry `read_allowlist` returns may equal, or be an ancestor of (so as to
    re-open), a path this module denies outright: `DENIED_DIRS`, `DENIED_HOME_FILES`,
    the run-records root, `<home>/.claude` itself (which holds the sign-in file), or
    `<home>/.config/convivy` itself (which holds the two private keys
    `DENIED_HOME_FILES` names) — checked after resolving symlinks, against a scratch
    home that symlinks one of its own subdirectories elsewhere, the way a real
    account home can."""
    monkeypatch.setattr(RS.BR, "real_account_home", lambda: tmp_path)
    monkeypatch.setattr(RS.BR, "root", lambda: tmp_path / "convivy")
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    home = tmp_path / "home"
    home.symlink_to(real_home)
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "2026-10-25" / "run-x"
    run_dir.mkdir(parents=True)

    allow_read = RS.read_allowlist(
        home=home, worktree=tmp_path / "wt", git_dir=tmp_path / "gitdir",
        run_dir=run_dir, tool_session_marker=tmp_path / "marker",
        scratch_roots=[tmp_path / "draft"])

    denied_points = ([home / d for d in RS.DENIED_DIRS]
                     + [home / f for f in RS.DENIED_HOME_FILES]
                     + [runs_root, home / ".claude", home / ".config" / "convivy"])

    for entry in allow_read:
        entry_resolved = Path(entry).resolve()
        for denied in denied_points:
            denied_resolved = Path(denied).resolve()
            assert entry_resolved != denied_resolved, \
                f"{entry} equals a denied path: {denied}"
            assert not denied_resolved.is_relative_to(entry_resolved), \
                f"{entry} contains (and so re-opens) a denied path: {denied}"


def test_an_eval_dispatchs_permissions_deny_read_is_never_a_wildcard(tmp_path):
    """Round 11 (agent-platform#835): `permissions.deny`'s `Read(...)` rule for an
    eval dispatch must never be a wildcard glob over `EVAL_SCRATCH_PREFIX*` — that
    shape (round 10's own, `eval_deny_read`) matches this dispatch's own directory
    exactly as it matches a sibling's, and the permissions layer has no confirmed
    way to carve an exception back out of a matching deny (this module's own
    `settings()` docstring on `return`). `sandbox.filesystem`'s `denyRead` keeps the
    wildcard — only `permissions.deny` must not. Fails against 70795aca2, whose
    `permissions.deny` carries `Read(//.../agentctl-eval-*/**)` for this exact
    dispatch's own (never-created, so enumeration finds nothing at all) directory."""
    scratch_dir = tmp_path / "scratch" / f"{RS.EVAL_SCRATCH_PREFIX}own"
    settings = RS.settings(home=tmp_path, worktree=tmp_path / "wt", eval_scratch_dir=scratch_dir)
    deny = settings["permissions"]["deny"]
    assert not any("*" in r for r in deny if r.startswith("Read(") and RS.EVAL_SCRATCH_PREFIX in r), (
        f"a wildcard eval Read(...) deny rule survived in permissions.deny: {deny}")
    fs = settings["sandbox"]["filesystem"]
    for root in (os.path.realpath(tempfile.gettempdir()), "/tmp", os.path.realpath("/tmp")):
        rule = f"{root}/{RS.EVAL_SCRATCH_PREFIX}*"
        assert rule in fs["denyRead"], f"sandbox-layer wildcard missing for {root}: {fs['denyRead']}"


def test_two_real_eval_dispatches_permissions_deny_read_of_each_others_directory(
        tmp_path, monkeypatch):
    """The real `settings()` output for two separate real eval dispatches (not a
    hand-built settings dict): each one's `permissions.deny` carries a `Read(...)`
    rule matching the OTHER's real directory, by its exact path — never this
    dispatch's own (round 11, agent-platform#835: a `Read(...)` deny matching this
    dispatch's own directory also refuses its own `Write`, measured against a real
    replay, run-18436a29). Fails against 70795aca2, where `deny_a` ALSO covers
    `dir_a` (round 10's wildcard, "deny wins over allow" by design)."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    dir_a = RS.create_eval_scratch_dir()
    dir_b = RS.create_eval_scratch_dir()
    try:
        settings_a = RS.settings(home=tmp_path, worktree=tmp_path / "wt", eval_scratch_dir=dir_a)
        settings_b = RS.settings(home=tmp_path, worktree=tmp_path / "wt", eval_scratch_dir=dir_b)
        deny_a = settings_a["permissions"]["deny"]
        deny_b = settings_b["permissions"]["deny"]

        def _covering(deny, path):
            candidate = _rule(path) + "/verdict.json"
            return [r for r in deny
                    if r.startswith("Read(") and fnmatch.fnmatch(candidate, r[len("Read("):-1])]

        covering_b_in_a = _covering(deny_a, dir_b)
        assert covering_b_in_a, f"A's permissions.deny does not cover B's real directory: {deny_a}"
        covering_a_in_b = _covering(deny_b, dir_a)
        assert covering_a_in_b, f"B's permissions.deny does not cover A's real directory: {deny_b}"
        # Each dispatch's own directory must be absent from its OWN deny list — a
        # Read(...) deny on it would also refuse this dispatch's own Write.
        assert _covering(deny_a, dir_a) == []
        assert _covering(deny_b, dir_b) == []
        # The rule naming a sibling is the sibling's exact directory (plus the
        # trailing `/**` every such rule gets, for its contents) — never a wildcard
        # over the directory NAME itself, the shape round 10 used.
        for rule in covering_b_in_a:
            assert f"{RS.EVAL_SCRATCH_PREFIX}*" not in rule, (
                f"sibling deny rule names a wildcard directory, not {dir_b}: {rule}")
        for rule in covering_a_in_b:
            assert f"{RS.EVAL_SCRATCH_PREFIX}*" not in rule, (
                f"sibling deny rule names a wildcard directory, not {dir_a}: {rule}")
    finally:
        shutil.rmtree(dir_a, ignore_errors=True)
        shutil.rmtree(dir_b, ignore_errors=True)


def test_an_eval_dispatchs_three_siblings_are_all_denied_by_exact_path(tmp_path, monkeypatch):
    """`_existing_eval_sibling_dirs` must find every sibling already on disk, not
    just the first one — enumeration bugs tend to hide behind a two-item fixture.
    Fails against 70795aca2's shape just as the pairwise test above does, but this
    one would also catch an enumeration that stops after the first match."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    dirs = [RS.create_eval_scratch_dir() for _ in range(3)]
    try:
        own = dirs[0]
        settings = RS.settings(home=tmp_path, worktree=tmp_path / "wt", eval_scratch_dir=own)
        deny = settings["permissions"]["deny"]

        def _covering(path):
            candidate = _rule(path) + "/verdict.json"
            return [r for r in deny
                    if r.startswith("Read(") and fnmatch.fnmatch(candidate, r[len("Read("):-1])]

        assert _covering(own) == []
        for sibling in dirs[1:]:
            assert _covering(sibling), f"sibling {sibling} not denied: {deny}"
    finally:
        for d in dirs:
            shutil.rmtree(d, ignore_errors=True)


def test_an_eval_dispatchs_write_allow_rule_still_stands_under_the_new_read_deny(
        tmp_path, monkeypatch):
    """The `Read(...)` deny rule covering a sibling's directory must not displace
    the `Edit(...)` allow rule `scratch_allow_roots` already grants THIS dispatch's
    own directory, and — round 11, agent-platform#835 — this dispatch's own
    directory must not appear in the `Read(...)` deny list at all: measured against
    a real replay (run-18436a29), a `Write` call refuses when its target matches a
    `Read(...)` deny rule, whatever `Edit(...)` allows for the same path.

    `RS.create_eval_scratch_dir()`'s REAL directory is used here, not a hand-built
    path under `tmp_path`: round 10's wildcard (`eval_deny_read`) is built from the
    real `tmp_roots`, so a hand-built directory elsewhere never actually matched it
    either — a hand-built path would pass even against 70795aca2 for the wrong
    reason. Fails against 70795aca2, whose `permissions.deny` carries a
    wildcard `Read(...)` rule that DOES match this real directory."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    scratch_dir = RS.create_eval_scratch_dir()
    try:
        settings = RS.settings(home=tmp_path, worktree=tmp_path / "wt",
                               eval_scratch_dir=scratch_dir)
        assert f"Edit({_rule(scratch_dir)}/**)" in settings["permissions"]["allow"]
        assert str(scratch_dir) in settings["sandbox"]["filesystem"]["allowWrite"]
        verdict_rule_spelling = _rule(RS.eval_verdict_path(scratch_dir))
        for rule in settings["permissions"]["deny"]:
            if not rule.startswith("Read("):
                continue
            pattern = rule[len("Read("):-1]
            assert not fnmatch.fnmatch(verdict_rule_spelling, pattern), (
                f"{rule} covers this dispatch's own verdict path")
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)


def test_eval_verdict_path_is_a_fixed_filename_inside_the_scratch_dir(tmp_path):
    d = tmp_path / "scratch-dir"
    assert RS.eval_verdict_path(d) == d / RS.EVAL_VERDICT_FILENAME


def test_without_a_runs_root_no_runs_rule_is_added(tmp_path):
    fs = RS.settings(home=tmp_path, worktree=tmp_path / "wt")["sandbox"]["filesystem"]
    assert not any("agent-runs" in p for p in fs["denyWrite"])


# --- CQ·138: a dispatch's tool-session scratch directory must live outside
# `runs_root`, which is denied for writing by every dispatched run's Bash,
# and outside every root any dispatch's `allowWrite` holds. Only the MARKER
# FILE inside that directory is ever granted by `settings()`'s
# `tool_session_marker` parameter — never the directory itself. Measured
# directly against a real dispatch: a run can append to and delete its own
# marker, but cannot create a file in its own scratch directory, rename that
# directory, or reach a SIBLING dispatch's marker or directory at all — see
# `settings()`'s own `tool_session_marker` docstring for the full reasoning,
# and `RS.tool_session_scratch_root`'s own docstring for why the directory
# lives under a per-account state path rather than the shared system temp
# root. ---

def test_create_tool_session_scratch_dir_makes_an_owner_only_directory(tmp_path):
    """Mirrors `test_create_eval_scratch_dir_makes_an_owner_only_directory`: mode
    0700 is `mkdtemp`'s own default, and the directory sits under this
    account's own `tool_session_scratch_root`, not the shared system temp
    root."""
    d = RS.create_tool_session_scratch_dir(tmp_path)
    try:
        assert d.is_dir()
        assert d.is_relative_to(RS.tool_session_scratch_root(tmp_path))
        assert stat.S_IMODE(d.stat().st_mode) == 0o700
        assert d.name.startswith(RS.TOOL_SESSION_SCRATCH_PREFIX)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_two_tool_session_scratch_dirs_are_distinct_and_unguessable_from_each_other(
        tmp_path):
    """Mirrors `test_two_eval_scratch_dirs_are_distinct_and_unguessable_from_each_other`:
    two concurrent dispatches must never land on the same directory."""
    a = RS.create_tool_session_scratch_dir(tmp_path)
    b = RS.create_tool_session_scratch_dir(tmp_path)
    try:
        assert a != b
        assert a.name != b.name
    finally:
        shutil.rmtree(a, ignore_errors=True)
        shutil.rmtree(b, ignore_errors=True)


def _fake_account_home() -> Path:
    """A directory for one test's own synthetic account home, created under
    the current working directory (the worktree) rather than under
    `tmp_path`: in THIS test environment `tmp_path` itself sits under the
    system temp root (`tempfile.gettempdir()`), which would make a check that
    something sits OUTSIDE the temp roots pass or fail on the fixture, not on
    this code — the same caveat
    `test_the_agent_postgres_socket_sits_under_the_account_home_not_tmp`
    already notes for `tmp_path`. The caller must remove it."""
    return Path(tempfile.mkdtemp(prefix="cq138-fakehome-", dir="."))


def test_create_tool_session_scratch_dir_lives_outside_the_shared_temp_roots():
    """CQ·138: the scratch directory's parent must not be the shared system
    temp root — that root sits inside an ordinary dispatch's own
    `allowWrite`, which would let one dispatch reach a sibling's scratch
    directory through it (see this module's own `TOOL_SESSION_SCRATCH_RELDIR`
    comment). Fails against 84c9aa93, whose
    `create_tool_session_scratch_dir` takes no `home` argument at all and
    creates its directory directly under `tempfile.gettempdir()`."""
    home = _fake_account_home()
    try:
        d = RS.create_tool_session_scratch_dir(home)
        root = RS.tool_session_scratch_root(home)
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        tmp_roots = [os.path.realpath(tempfile.gettempdir()), "/tmp", os.path.realpath("/tmp")]
        for t in tmp_roots:
            assert not root.is_relative_to(t), f"the scratch root must not sit under {t}"
            assert not d.is_relative_to(t), f"the scratch directory must not sit under {t}"
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_create_tool_session_marker_makes_an_owner_only_file_inside_the_scratch_dir(
        tmp_path):
    """The marker is a FILE, created directly inside its own
    `create_tool_session_scratch_dir` directory, mode 0600 (owner read/write,
    nobody else — tighter than the directory's own 0700, since this is the
    one path a run is actually granted write access to)."""
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir(mode=0o700)
    marker = RS.create_tool_session_marker(scratch_dir)
    try:
        assert marker.parent == scratch_dir
        assert marker.is_file()
        assert stat.S_IMODE(marker.stat().st_mode) == 0o600
        assert marker.name == RS.TOOL_SESSION_MARKER_NAME
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)


def test_a_dispatchs_settings_grant_its_tool_session_marker_on_top_of_the_shared_root(
        tmp_path):
    """An ordinary (non-eval) dispatch's `allowWrite` must gain exactly this
    dispatch's own marker FILE — on TOP of the shared temp root it already had,
    never in place of it (contrast `eval_scratch_dir`, which replaces that
    root) — while `runs_root` stays denied exactly as it is for every other
    dispatch, AND the marker's own parent directory is never granted by THIS
    parameter as a separate entry: the directory itself lives outside the
    shared root so nothing else covers it either — see `settings()`'s own
    `tool_session_marker` docstring. Fails against b362c5d2: the parameter
    still takes a directory, not this file, so the directory itself would
    appear in `allowWrite` instead of being absent from it."""
    runs = tmp_path / "agent-runs"
    scratch_dir = tmp_path / "scratch-dir" / f"{RS.TOOL_SESSION_SCRATCH_PREFIX}xyz"
    scratch_dir.mkdir(parents=True)
    marker = scratch_dir / RS.TOOL_SESSION_MARKER_NAME
    marker.touch()
    settings = RS.settings(home=tmp_path, worktree=tmp_path / "wt", runs_root=runs,
                           tool_session_marker=marker)
    fs = settings["sandbox"]["filesystem"]
    assert str(marker) in fs["allowWrite"]
    assert str(scratch_dir) not in fs["allowWrite"], \
        "the directory must never be granted — only the marker file inside it"
    assert "/tmp" in fs["allowWrite"]
    assert os.path.realpath("/tmp") in fs["allowWrite"]
    assert str(runs) in fs["denyWrite"]
    assert str(runs) not in fs["allowWrite"]


def test_two_tool_session_dispatches_settings_do_not_cover_each_others_marker(
        tmp_path):
    """Neither dispatch's allow-list may reach the other's marker — the same
    isolation `test_two_eval_dispatches_settings_do_not_cover_each_others_directory`
    requires of `eval_scratch_dir`."""
    dir_a = tmp_path / "scratch" / f"{RS.TOOL_SESSION_SCRATCH_PREFIX}a"
    dir_b = tmp_path / "scratch" / f"{RS.TOOL_SESSION_SCRATCH_PREFIX}b"
    dir_a.mkdir(parents=True)
    dir_b.mkdir(parents=True)
    marker_a = dir_a / RS.TOOL_SESSION_MARKER_NAME
    marker_b = dir_b / RS.TOOL_SESSION_MARKER_NAME
    marker_a.touch()
    marker_b.touch()
    settings_a = RS.settings(home=tmp_path, worktree=tmp_path / "wt",
                             tool_session_marker=marker_a)
    settings_b = RS.settings(home=tmp_path, worktree=tmp_path / "wt",
                             tool_session_marker=marker_b)
    assert str(marker_b) not in settings_a["sandbox"]["filesystem"]["allowWrite"]
    assert str(marker_a) not in settings_b["sandbox"]["filesystem"]["allowWrite"]


def _assert_allow_write_never_covers(allow_write: list[str], target: Path) -> None:
    """`target` (a directory) must not be reachable through any entry in
    `allow_write`: no entry may equal it, nor be an ancestor of it, since
    `allowWrite` on a directory grants write on everything inside it,
    recursively."""
    target_str = str(target)
    for p in allow_write:
        assert p != target_str, f"{p!r} is exactly the scratch root/directory"
        assert not target_str.startswith(p.rstrip("/") + os.sep), \
            f"{p!r} is an ancestor of {target_str!r} and so covers it"


def test_an_ordinary_dispatchs_settings_never_grant_the_tool_session_scratch_root(
        tmp_path):
    """CQ·138: an ORDINARY dispatch's `allowWrite` holds the shared temp
    roots, so a scratch directory created directly under one of them would be
    reachable by every OTHER dispatch too — `allowWrite` on a directory grants
    removing or renaming anything inside it, so any dispatch could reach ANY
    sibling's scratch directory this way, not only its own. This asserts the
    actual fix: neither the
    scratch root (`RS.tool_session_scratch_root`) nor the one dispatch's own
    scratch directory inside it is ever covered by any `allowWrite` entry —
    only the marker file is. `home` is a synthetic account home under the
    current working directory, not `tmp_path` — see `_fake_account_home`'s
    own docstring for why. Fails against 84c9aa93, whose `allowWrite`
    includes `tempfile.gettempdir()`, the literal parent every scratch
    directory was created under."""
    home = _fake_account_home()
    try:
        scratch_root = RS.tool_session_scratch_root(home)
        scratch_dir = scratch_root / f"{RS.TOOL_SESSION_SCRATCH_PREFIX}xyz"
        scratch_dir.mkdir(parents=True)
        marker = scratch_dir / RS.TOOL_SESSION_MARKER_NAME
        marker.touch()
        settings = RS.settings(home=home, worktree=tmp_path / "wt",
                               tool_session_marker=marker)
        allow_write = settings["sandbox"]["filesystem"]["allowWrite"]
        assert str(marker) in allow_write
        _assert_allow_write_never_covers(allow_write, scratch_root)
        _assert_allow_write_never_covers(allow_write, scratch_dir)
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_an_eval_dispatchs_settings_never_grant_the_tool_session_scratch_root(
        tmp_path):
    """The same check as
    `test_an_ordinary_dispatchs_settings_never_grant_the_tool_session_scratch_root`,
    for an `--eval-mode` dispatch: its `allowWrite` is narrowed to
    `eval_scratch_dir` alone, which must not happen to cover the tool-session
    scratch root either."""
    home = _fake_account_home()
    try:
        scratch_root = RS.tool_session_scratch_root(home)
        scratch_dir = scratch_root / f"{RS.TOOL_SESSION_SCRATCH_PREFIX}xyz"
        scratch_dir.mkdir(parents=True)
        marker = scratch_dir / RS.TOOL_SESSION_MARKER_NAME
        marker.touch()
        eval_dir = tmp_path / "eval-scratch" / f"{RS.EVAL_SCRATCH_PREFIX}e"
        eval_dir.mkdir(parents=True)
        settings = RS.settings(home=home, worktree=tmp_path / "wt",
                               eval_scratch_dir=eval_dir, tool_session_marker=marker)
        allow_write = settings["sandbox"]["filesystem"]["allowWrite"]
        assert str(marker) in allow_write
        _assert_allow_write_never_covers(allow_write, scratch_root)
        _assert_allow_write_never_covers(allow_write, scratch_dir)
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_an_eval_dispatch_still_gets_its_tool_session_marker_added_on_top(
        tmp_path):
    """The blocking finding's own edge case: an `--eval-mode` dispatch's
    `allowWrite` is narrowed to `eval_scratch_dir` ALONE (no shared root) —
    without this parameter's explicit, unconditional addition, that narrowing
    would silently drop the tool-session mechanism for every eval dispatch,
    the same way omitting `runs_root` drops it for every dispatch today. Both
    paths must be present, and the shared root must still be absent."""
    eval_dir = tmp_path / "eval-scratch" / f"{RS.EVAL_SCRATCH_PREFIX}e"
    tool_dir = tmp_path / "tool-scratch" / f"{RS.TOOL_SESSION_SCRATCH_PREFIX}t"
    eval_dir.mkdir(parents=True)
    tool_dir.mkdir(parents=True)
    marker = tool_dir / RS.TOOL_SESSION_MARKER_NAME
    marker.touch()
    settings = RS.settings(home=tmp_path, worktree=tmp_path / "wt",
                           eval_scratch_dir=eval_dir,
                           tool_session_marker=marker)
    fs = settings["sandbox"]["filesystem"]
    assert str(eval_dir) in fs["allowWrite"]
    assert str(marker) in fs["allowWrite"]
    assert str(tool_dir) not in fs["allowWrite"]
    assert "/tmp" not in fs["allowWrite"]
    assert os.path.realpath("/tmp") not in fs["allowWrite"]


def test_without_tool_session_marker_settings_are_unchanged(tmp_path):
    """Adding the `tool_session_marker` parameter must not perturb a dispatch
    that omits it: byte-identical to before this parameter existed (the same
    contract `test_a_non_eval_dispatchs_settings_are_unchanged_by_eval_scratch_dir_support`
    holds for `eval_scratch_dir`)."""
    runs = tmp_path / "agent-runs"
    with_param = RS.settings(home=tmp_path, worktree=tmp_path / "wt", runs_root=runs,
                             tool_session_marker=None)
    without_param = RS.settings(home=tmp_path, worktree=tmp_path / "wt", runs_root=runs)
    assert with_param == without_param


def test_the_agent_runs_root_still_denies_writing_with_a_tool_session_marker_set(
        tmp_path):
    """The blocking finding's own guarantee, restated against the real pinning
    test's own assertions (`test_the_agent_runs_root_is_denied_for_writing_by_a_run`):
    granting this dispatch's own marker must never loosen `runs_root`'s deny
    rule one inch — the fix adds a path OUTSIDE `runs_root`, it does not touch
    the rule that denies `runs_root` itself."""
    runs = tmp_path / "agent-runs"
    scratch_dir = tmp_path / "scratch-dir" / f"{RS.TOOL_SESSION_SCRATCH_PREFIX}xyz"
    scratch_dir.mkdir(parents=True)
    marker = scratch_dir / RS.TOOL_SESSION_MARKER_NAME
    marker.touch()
    settings = RS.settings(home=tmp_path, worktree=tmp_path / "wt", runs_root=runs,
                           tool_session_marker=marker)
    fs = settings["sandbox"]["filesystem"]
    assert str(runs) in fs["denyWrite"]
    assert f"Edit({_rule(runs)}/**)" in settings["permissions"]["deny"]
    assert str(runs) not in fs["allowWrite"]


def test_settings_json_includes_the_tool_session_marker(tmp_path):
    """`settings_json` must thread the new parameter through to `settings`,
    not just accept and drop it."""
    import json as _json

    scratch_dir = tmp_path / "scratch-dir" / f"{RS.TOOL_SESSION_SCRATCH_PREFIX}xyz"
    scratch_dir.mkdir(parents=True)
    marker = scratch_dir / RS.TOOL_SESSION_MARKER_NAME
    marker.touch()
    rendered = RS.settings_json(home=tmp_path, worktree=tmp_path / "wt",
                                tool_session_marker=marker)
    assert str(marker) in _json.loads(rendered)["sandbox"]["filesystem"]["allowWrite"]


def _linked_checkout(tmp_path: Path) -> Path:
    """A primary repository with one commit, and a linked worktree of it. Returns the worktree."""
    primary = tmp_path / "primary"
    primary.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(primary)], check=True)
    subprocess.run(["git", "-C", str(primary), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    wt = tmp_path / "wt"
    subprocess.run(["git", "-C", str(primary), "worktree", "add", "-q", "-b", "b", str(wt)], check=True)
    return wt


def _git_path(worktree: Path, flag: str) -> Path:
    """A path git itself reports for `worktree`, such as `--git-dir` or `--git-common-dir`."""
    out = subprocess.run(["git", "-C", str(worktree), "rev-parse", "--path-format=absolute", flag],
                         capture_output=True, text=True, check=True)
    return Path(out.stdout.strip()).resolve()


def test_git_common_dir_is_the_common_git_dir_of_a_linked_worktree(tmp_path, monkeypatch):
    # Pinned to darwin: `_linked_checkout`'s own branch ("b") has no namespace segment,
    # so `_branch_write_dirs` now grants it no write at all on Linux (see its own
    # docstring) — this test is about git_common_dir's resolution, not that grant, so
    # it keeps the exact-file platform the real host here (darwin) already uses, same
    # reason test_a_linked_worktree_writes_only_the_subpaths_a_commit_needs pins it.
    monkeypatch.setattr(RS, "_dispatch_host_platform", lambda: "darwin")
    primary = tmp_path / "primary"
    wt = _linked_checkout(tmp_path)

    # Checked against git itself, so a wrong reading of the worktree's files fails here.
    assert RS.git_common_dir(wt) == _git_path(wt, "--git-common-dir")
    assert RS.git_common_dir(primary) == (primary / ".git").resolve()

    common = RS.git_common_dir(wt)
    fs = RS.settings(home=tmp_path, worktree=wt, git_dir=common)["sandbox"]["filesystem"]
    assert str(common / "hooks") in fs["denyWrite"]
    assert str(common / "config") in fs["denyWrite"]


def test_a_linked_worktree_writes_only_the_subpaths_a_commit_needs(tmp_path, monkeypatch):
    # knowledge#793 condition 1 / round 2. The subpaths are what a commit and a push
    # write for THIS worktree's own branch, found by observing one
    # (tests/live/test_dispatched_run_confinement.py runs it under the sandbox).
    #
    # Round 6: `objects` is no longer in this grant at all — the run's own git writes new
    # objects to its private store instead (see RS.private_objects_dir's docstring).
    #
    # Pinned to darwin (CI's own pytest job runs on Linux — see
    # test_an_ordinary_dispatch_gets_the_base_read_allowlist_on_darwin's own comment):
    # this is the exact-file render `_branch_write_paths` still produces unchanged on
    # macOS. test_linux_branch_write_grant_is_directories_not_files pins the Linux one,
    # which grants the branch's own directories instead — see `_branch_write_dirs`'s
    # own docstring for why.
    monkeypatch.setattr(RS, "_dispatch_host_platform", lambda: "darwin")
    wt = _linked_checkout(tmp_path)
    common = RS.git_common_dir(wt)
    fs = RS.settings(home=tmp_path, worktree=wt, git_dir=common)["sandbox"]["filesystem"]
    linked = RS._linked_gitdir(wt)
    assert str(RS.private_objects_dir(common, linked)) in fs["allowWrite"]
    for rel in ("refs/heads/b", "refs/heads/b.lock", "refs/remotes/origin/b",
                "logs/refs/heads/b", "logs/refs/remotes/origin/b"):
        assert str(common / rel) in fs["allowWrite"], rel
    assert str(_git_path(wt, "--git-dir")) in fs["allowWrite"]
    # The shared directory itself is not writable, so a new file at its top level is refused.
    assert str(common) not in fs["allowWrite"]
    # Round 6: `objects` is denied wholesale now, not merely absent from allowWrite — see
    # test_protect_rest_of_git_dir_denies_objects_wholesale.
    assert str(common / "objects") not in fs["allowWrite"]
    # The shared refs/logs trees themselves are not in this grant — see
    # test_main_and_packed_refs_are_explicitly_denied for what actually refuses a
    # write to another branch's ref (this allowWrite list alone does not).
    assert str(common / "refs") not in fs["allowWrite"]
    assert str(common / "logs") not in fs["allowWrite"]
    assert str(common / "packed-refs") not in fs["allowWrite"]


def _linked_checkout_with_branch(tmp_path: Path, branch: str) -> "tuple[Path, Path]":
    """The same fixture `_linked_checkout` builds, except on a caller-named branch —
    `agent/<synthetic>`-shaped, matching a real dispatched branch's own nesting, rather
    than `_linked_checkout`'s flat `"b"`. Returns (worktree, primary)."""
    primary = tmp_path / "primary"
    primary.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(primary)], check=True)
    subprocess.run(["git", "-C", str(primary), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    wt = tmp_path / "wt"
    subprocess.run(["git", "-C", str(primary), "worktree", "add", "-q", "-b", branch, str(wt)],
                   check=True)
    return wt, primary


def test_linux_branch_write_grant_is_directories_not_files(tmp_path, monkeypatch):
    """Occurrence (Debian 12.15, bwrap, agent-platform 17d979579): the installed binary
    bind-mounts each `allowWrite` entry on its own, and a rename onto or out of a
    bind-mount point fails — exactly what a commit's lock-then-rename onto its own ref
    does. `_branch_write_dirs` grants the branch's own directories instead of its exact
    files on Linux, so the ref and its lock file share one mount (see its own docstring).
    This pins the shape of that grant; test_linux_bwrap_sandbox_commits_without_a_stale_lock
    confirms it actually works under a real sandbox."""
    monkeypatch.setattr(RS, "_dispatch_host_platform", lambda: "linux")
    branch = "agent/x"
    wt, _primary = _linked_checkout_with_branch(tmp_path, branch)
    common = RS.git_common_dir(wt)
    fs = RS.settings(home=tmp_path, worktree=wt, git_dir=common)["sandbox"]["filesystem"]
    for rel in ("refs/heads/agent", "refs/remotes/origin/agent",
               "logs/refs/heads/agent", "logs/refs/remotes/origin/agent"):
        assert str(common / rel) in fs["allowWrite"], rel
    # Never the leaf files themselves on Linux — binding those individually is the
    # defect this grant replaces.
    for rel in ("refs/heads/agent/x", "refs/heads/agent/x.lock", "refs/remotes/origin/agent/x",
               "logs/refs/heads/agent/x", "logs/refs/remotes/origin/agent/x"):
        assert str(common / rel) not in fs["allowWrite"], rel


def test_linux_branch_write_grant_still_denies_a_sibling(tmp_path, monkeypatch):
    """The directory `test_linux_branch_write_grant_is_directories_not_files` grants is
    shared with every other dispatched run's own branch under the same `refs/heads/agent/`
    prefix — `_protect_rest_of_git_dir`'s own enumeration is what narrows it back down,
    unchanged by which of `_branch_write_paths`/`_branch_write_dirs` fed the allow side
    (see `_branch_write_dirs`'s own docstring on the one gap enumeration cannot close: a
    sibling branch created after this render)."""
    monkeypatch.setattr(RS, "_dispatch_host_platform", lambda: "linux")
    branch = "agent/x"
    wt, primary = _linked_checkout_with_branch(tmp_path, branch)
    subprocess.run(["git", "-C", str(primary), "branch", "agent/sibling"], check=True)
    common = RS.git_common_dir(wt)
    fs = RS.settings(home=tmp_path, worktree=wt, git_dir=common)["sandbox"]["filesystem"]
    assert str(common / "refs" / "heads" / "agent" / "sibling") in fs["denyWrite"]
    assert str(common / "refs" / "heads" / "agent" / "x") not in fs["denyWrite"]


def test_darwin_and_linux_branch_write_grants_differ_only_in_shape(tmp_path, monkeypatch):
    """Direct comparison, the same way test_darwin_and_linux_read_allowlists_differ_only_by_the_two_reopened_dirs
    pins the read side: darwin's own branch-write entries are exactly `_branch_write_paths`,
    Linux's own are exactly `_branch_write_dirs` — each present in its platform's
    `allowWrite`, neither the other's shape leaking into the wrong platform's render."""
    branch = "agent/x"
    wt, _primary = _linked_checkout_with_branch(tmp_path, branch)
    common = RS.git_common_dir(wt)
    files = {str(p) for p in RS._branch_write_paths(common, branch)}
    dirs = {str(p) for p in RS._branch_write_dirs(common, branch)}
    assert files.isdisjoint(dirs)

    monkeypatch.setattr(RS, "_dispatch_host_platform", lambda: "darwin")
    darwin_allow = set(RS.settings(home=tmp_path, worktree=wt, git_dir=common)
                       ["sandbox"]["filesystem"]["allowWrite"])
    assert files <= darwin_allow
    assert dirs.isdisjoint(darwin_allow)

    monkeypatch.setattr(RS, "_dispatch_host_platform", lambda: "linux")
    linux_allow = set(RS.settings(home=tmp_path, worktree=wt, git_dir=common)
                      ["sandbox"]["filesystem"]["allowWrite"])
    assert dirs <= linux_allow
    assert files.isdisjoint(linux_allow)


def _denied(common: Path, rel: str, fs: dict) -> bool:
    """True when `rel` (relative to `common`) is itself, or sits under, one of
    `fs["denyWrite"]`'s own entries — a directory denied wholesale (`_protect_rest_of_git_dir`'s
    own enumeration) covers every path under it, not just the ones named."""
    target = common / rel
    for raw in fs["denyWrite"]:
        p = Path(raw)
        if target == p or p in target.parents:
            return True
    return False


def _assert_branch_write_grant_is_empty(common: Path, branch: str, fs: dict) -> None:
    """What round 2's "grant nothing" fix (PR #1111 round 2 security review) means for
    one Linux render: none of the branch's own ref/lock/remote-tracking-ref/reflog paths,
    and none of the shared roots those paths' enclosing directories could instead have
    named, appear in `allowWrite` — and the deny side actually covers each of those paths
    (directly, or via an ancestor denied wholesale), so a commit attempting any of them
    fails rather than merely finding nothing granted."""
    for rel in (f"refs/heads/{branch}", f"refs/heads/{branch}.lock",
               f"refs/remotes/origin/{branch}", f"logs/refs/heads/{branch}",
               f"logs/refs/remotes/origin/{branch}"):
        assert str(common / rel) not in fs["allowWrite"], rel
        assert _denied(common, rel, fs), rel
    for root in ("refs/heads", "refs/remotes/origin", "logs/refs/heads",
                "logs/refs/remotes/origin"):
        assert str(common / root) not in fs["allowWrite"], root


def test_linux_branch_write_grant_is_empty_for_the_primary_checkouts_own_main(tmp_path, monkeypatch):
    """Occurrence (CI, this PR's own round 1): the real-worktree fixture
    test_run_session_subprocess_env_wires_the_private_object_store_for_a_real_worktree
    builds a plain `git init -b main` checkout with no linked worktree at all — exactly
    the shape `current_branch()` reaches `main` with, and exactly what failed CI's Linux
    `python` job under round 1's refusal ("branch 'main' has no namespace segment of its
    own"). Round 2 grants it no write at all instead of refusing the whole dispatch —
    unlike a hand-opened PR branch (test_linux_branch_write_grant_is_empty_for_an_unnamespaced_pr_branch,
    below), `main` here is the REPOSITORY's own default branch, reached by an ordinary
    dispatch with no `--pr` round-fix involved, so this is the common case the refusal
    used to block, not only the hand-opened-branch edge case."""
    monkeypatch.setattr(RS, "_dispatch_host_platform", lambda: "linux")
    primary = tmp_path / "primary"
    primary.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(primary)], check=True)
    subprocess.run(["git", "-C", str(primary), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    common = RS.git_common_dir(primary)
    fs = RS.settings(home=tmp_path, worktree=primary, git_dir=common)["sandbox"]["filesystem"]
    _assert_branch_write_grant_is_empty(common, "main", fs)


def test_linux_branch_write_grant_is_empty_for_an_unnamespaced_pr_branch(tmp_path, monkeypatch):
    """Security review (PR #1111 round 1): `current_branch()` returns whatever `HEAD`
    actually names, and a `--pr N` round-fix checks out the PR's own `headRefName` as
    given (`bin/agentctl`), with no `agent/`-prefix normalization — a hand-opened PR
    branch with no `/` (`fix-ci`, say) reaches `_branch_write_dirs` with a ref that sits
    directly in `refs/heads` itself. Round 1 refused the whole dispatch over this, which
    blocked every Linux run on such a branch, including a read-only one that never
    commits (round 2 revise). Round 2 grants the branch no write at all instead:
    `_assert_branch_write_grant_is_empty` is the same check
    test_linux_branch_write_grant_is_empty_for_the_primary_checkouts_own_main makes for
    `main`."""
    monkeypatch.setattr(RS, "_dispatch_host_platform", lambda: "linux")
    branch = "fix-ci"
    wt, _primary = _linked_checkout_with_branch(tmp_path, branch)
    common = RS.git_common_dir(wt)
    fs = RS.settings(home=tmp_path, worktree=wt, git_dir=common)["sandbox"]["filesystem"]
    _assert_branch_write_grant_is_empty(common, branch, fs)


def test_linux_branch_write_note_names_the_reason_only_for_the_unnamespaced_case(tmp_path, monkeypatch):
    """`linux_branch_write_note` is `bin/agentctl`'s own source for the reason it logs
    to the run's stderr.log and policy.json (`settings()` itself never calls it — see
    `_branch_write_dirs`'s own docstring). None on darwin regardless of branch shape;
    None on Linux for a namespaced branch or a detached HEAD (`branch=None`); a message
    naming the branch otherwise."""
    branch = "fix-ci"
    wt, _primary = _linked_checkout_with_branch(tmp_path, branch)
    common = RS.git_common_dir(wt)
    monkeypatch.setattr(RS, "_dispatch_host_platform", lambda: "darwin")
    assert RS.linux_branch_write_note(common, branch) is None
    monkeypatch.setattr(RS, "_dispatch_host_platform", lambda: "linux")
    assert RS.linux_branch_write_note(common, None) is None
    assert RS.linux_branch_write_note(common, "agent/x") is None
    note = RS.linux_branch_write_note(common, branch)
    assert note is not None
    assert repr(branch) in note


def test_linux_branch_write_grant_still_namespaced_for_a_nested_branch(tmp_path, monkeypatch):
    """The refusal above is specific to a branch with NO namespace segment of its own — one
    with a deeper one (`team/sub/x`, not just the one-level `agent/x`
    test_linux_branch_write_grant_is_directories_not_files already pins) still yields its
    own namespace directory, never the shared `refs/heads` root itself."""
    monkeypatch.setattr(RS, "_dispatch_host_platform", lambda: "linux")
    branch = "team/sub/fix-ci"
    wt, _primary = _linked_checkout_with_branch(tmp_path, branch)
    common = RS.git_common_dir(wt)
    fs = RS.settings(home=tmp_path, worktree=wt, git_dir=common)["sandbox"]["filesystem"]
    assert str(common / "refs" / "heads" / "team" / "sub") in fs["allowWrite"]
    assert str(common / "refs" / "heads") not in fs["allowWrite"]
    assert str(common / "refs" / "remotes" / "origin" / "team" / "sub") in fs["allowWrite"]
    assert str(common / "refs" / "remotes" / "origin") not in fs["allowWrite"]


@pytest.mark.parametrize("branch", ["main", "fix-ci", "agent/x"])
def test_darwin_render_is_unchanged_regardless_of_namespace_shape(tmp_path, monkeypatch, branch):
    """The "grant nothing" fix (PR #1111 round 2) is Linux-only: darwin's own grant is
    `_branch_write_paths`'s exact files, which never covers a shared root to begin with,
    so this renders identically on darwin whether `branch` has no namespace segment at
    all (`main`, `fix-ci`) or the ordinary one (`agent/x`) — same shape
    test_a_linked_worktree_writes_only_the_subpaths_a_commit_needs already pins for
    branch "b"."""
    monkeypatch.setattr(RS, "_dispatch_host_platform", lambda: "darwin")
    if branch == "main":
        # The primary checkout's own default branch, no linked worktree — the shape
        # test_linux_branch_write_grant_is_empty_for_the_primary_checkouts_own_main
        # pins on Linux.
        wt = tmp_path / "primary"
        wt.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(wt)], check=True)
        subprocess.run(["git", "-C", str(wt), "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    else:
        wt, _primary = _linked_checkout_with_branch(tmp_path, branch)
    common = RS.git_common_dir(wt)
    fs = RS.settings(home=tmp_path, worktree=wt, git_dir=common)["sandbox"]["filesystem"]
    for rel in (f"refs/heads/{branch}", f"refs/heads/{branch}.lock",
               f"refs/remotes/origin/{branch}", f"logs/refs/heads/{branch}",
               f"logs/refs/remotes/origin/{branch}"):
        assert str(common / rel) in fs["allowWrite"], rel
    assert str(common / "refs" / "heads") not in fs["allowWrite"]


# --- Linux's own bwrap sandbox, built from the rendered settings, exercises a real commit
# and push against the fix above: skipped with a stated reason wherever bwrap is absent or
# this host refuses unprivileged user namespaces (every macOS dev sandbox, and any Linux
# host without bwrap installed or with user namespaces locked down) — UNLESS
# AGENTCTL_REQUIRE_BWRAP is set (CI's own `python` job, after the workflow's "install
# bubblewrap" step), in which case that same condition is a FAILURE instead of a skip: the
# one real-sandbox confirmation this fix has must actually run in CI, not silently skip
# there the way it always has (reviewer, PR #1111 round 1). `pytest -rs`, already the flag
# this repo's own CI `python` job runs with, prints the stated reason either way rather
# than a silent pass. -----------------------------------------------------------------------

# What these tests exercise is filesystem grants (`allowWrite`/`denyWrite` bind
# mounts), not networking, so this never unshares the network namespace — neither
# `--unshare-net` directly nor the whole-net-included `--unshare-all` (CI occurrence,
# PR #1111 round 2: bwrap's own loopback setup inside a freshly unshared net
# namespace failed, "RTM_NEWADDR: Operation not permitted," on a runner that refused
# it, which failed both the probe below and every real-bwrap test before either ever
# reached its own filesystem assertions). `--unshare-user` is kept whenever bwrap is
# NOT setuid-root: the probe's own stated reason ("unprivileged user namespaces likely
# refused") is what an unprivileged bwrap build needs to create the mount namespace
# these binds live in at all. A setuid-root bwrap (PR #1111 round 3's own CI
# occurrence: a `convivy-linux` runner with unprivileged user namespaces refused and
# remediated by making the installed `bwrap` setuid-root) unshares the user namespace
# itself before dropping privileges and refuses the option outright — "Specifying
# --unshare-user is not allowed for setuid-root bwrap" — so it is dropped there as
# already implied. The rest of what `--unshare-all` used to add (`ipc`, `pid`, `uts`)
# is harmless isolation this suite does not depend on either way.


def _bwrap_unshare_flags(bwrap_path: "str | None") -> tuple:
    base = ("--unshare-ipc", "--unshare-pid", "--unshare-uts")
    if bwrap_path and os.stat(bwrap_path).st_mode & stat.S_ISUID:
        return base
    return ("--unshare-user", *base)


_BWRAP_PATH = shutil.which("bwrap")
_BWRAP_UNSHARE_FLAGS = _bwrap_unshare_flags(_BWRAP_PATH)


def _bwrap_unavailable_reason() -> "str | None":
    """None when a real bwrap sandbox can be built on this host, else why not — including
    bwrap's own stderr when it ran but refused (unprivileged user namespaces disabled), not
    just "absent from PATH", so a CI failure under AGENTCTL_REQUIRE_BWRAP names the real
    cause rather than a bare skip reason. Probed once at collection, not inside the test, so
    every test below shares one answer and one subprocess call rather than re-probing per
    test."""
    bwrap = _BWRAP_PATH
    if bwrap is None:
        return "bwrap is not on PATH"
    try:
        probe = subprocess.run(
            [bwrap, *_BWRAP_UNSHARE_FLAGS, "--die-with-parent", "--ro-bind", "/", "/", "true"],
            capture_output=True, text=True, timeout=30)
    except OSError as exc:
        return f"bwrap could not run: {exc}"
    if probe.returncode != 0:
        return f"bwrap sandbox unavailable (unprivileged user namespaces likely refused): {probe.stderr.strip()}"
    return None


_BWRAP_UNAVAILABLE = _bwrap_unavailable_reason()


def requires_bwrap(test_func):
    """Skips a real-bwrap test with `_BWRAP_UNAVAILABLE`'s stated reason, same as a plain
    `pytest.mark.skipif` would — UNLESS `AGENTCTL_REQUIRE_BWRAP` is set in the environment,
    in which case a missing or unusable bwrap FAILS the test with that same reason instead
    of skipping it (see this section's own header comment for why). A macOS dev run never
    sets that variable, so it keeps skipping exactly as before this existed."""
    if _BWRAP_UNAVAILABLE is None:
        return test_func
    if os.environ.get("AGENTCTL_REQUIRE_BWRAP"):
        reason = _BWRAP_UNAVAILABLE

        @functools.wraps(test_func)
        def _fail(*args, **kwargs):
            pytest.fail(reason)
        return _fail
    return pytest.mark.skip(reason=_BWRAP_UNAVAILABLE)(test_func)


def _bwrap_commit_env(common: Path, wt: Path) -> dict:
    """The env a real dispatch's own `dispatch_env` wires for this worktree's git
    (knowledge#793 round 6): `GIT_OBJECT_DIRECTORY` at its private store, created here
    the same way `bin/agentctl` creates it before spawn, and
    `GIT_ALTERNATE_OBJECT_DIRECTORIES` at the shared store so the same git still reads
    what is already there. `_protect_rest_of_git_dir` denies `common / "objects"`
    wholesale (round 6; see its own docstring) whether or not this run's branch is
    namespaced, so a bwrap-sandboxed `git commit` with no object-store redirect fails
    outright on the object write itself — measured directly (chmod 555 on a plain
    `.git/objects`): "insufficient permission for adding an object to repository
    database", exit 128 — a different failure from the ref/lock question this test
    suite exists to check, and the redirect is what every commit-performing bwrap test
    below needs regardless of branch shape."""
    private_dir = RS.run_private_objects_dir(wt, common)
    private_dir.mkdir(parents=True, exist_ok=True)
    return {**os.environ, "GIT_OBJECT_DIRECTORY": str(private_dir),
           "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(common / "objects")}


def _fabricate_missing_deny_write_stand_in(path: str) -> bool:
    """Stands in for the installed Claude Code binary's own measured translation of a
    `denyWrite` entry whose path does not exist (`_ensure_packed_refs_exists`'s own
    docstring, run-c409dcfe): a stand-in node appears there anyway, unreadable by this
    run's own user — not a bare refusal to write nothing. `bwrap`'s `--ro-bind-try`
    does the opposite when its source is missing: it skips the bind entirely (that is
    the documented meaning of "try"), leaving the path governed by whatever broader
    grant covers its parent — in this suite, the ambient `/tmp` `allowWrite` every
    non-eval, non-review dispatch gets, since `tmp_path` lives under `/tmp` on Linux CI.
    Left alone, `_bwrap_argv` cannot reproduce the incident this module's fix addresses:
    a missing `denyWrite` path simply stays writable here, where the real binary leaves
    it unreadable.

    An empty, mode-0 regular file reproduces the functional effect without needing the
    real binary's own character-device mechanism (unavailable outside it, and not what
    this suite checks): the owning user's own permission bits are enforced even for that
    same user, confirmed directly against plain git with no bwrap involved at all —
    `chmod 000` on an empty `packed-refs` makes `git commit` fail with the exact text
    run-c409dcfe reported, "fatal: couldn't read .git/packed-refs: Permission denied".
    `O_CREAT | O_EXCL | O_NOFOLLOW`, same as `_ensure_packed_refs_exists`, so this never
    follows or overwrites whatever might already be there.

    Returns whether it created the file, so `_bwrap_argv` can hand the caller exactly
    what it fabricated, for `_remove_fabricated_stand_ins` to remove once the sandboxed
    commands that needed it on disk have run."""
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o000)
    except OSError:
        return False
    os.close(fd)
    return True


def _remove_fabricated_stand_ins(paths: list) -> None:
    """Removes every path `_bwrap_argv` fabricated, once the bwrap commands that needed
    it on disk have all run. Nothing in this module's own task description or in
    `_ensure_packed_refs_exists`'s docstring claims the real binary's own stand-in
    survives past the run that triggered it, and a test that left this module's own
    fabricated file behind would fail the very "nothing was left behind" assertions
    (`test_linux_bwrap_sandbox_commits_when_packed_refs_absent_at_spawn` and its
    present-at-spawn sibling) for a reason that has nothing to do with the sandbox —
    this module's own test-infrastructure artifact, not anything git or bwrap did."""
    for p in paths:
        os.remove(p)


def _bwrap_argv(fs: dict) -> "tuple[list, list]":
    """A literal bwrap invocation built from one dispatch's own rendered
    `sandbox.filesystem` — the minimal translation this test needs to exercise the same
    allowWrite/denyWrite narrowing the installed Claude Code binary applies on Linux,
    without depending on that binary (unavailable to this suite). Bind the whole host
    read-only, then layer `allowWrite` read-write and `denyWrite` back to read-only, in
    that order: bwrap applies binds in argument order and the last one for a given
    destination wins, so a `denyWrite` entry nested inside an `allowWrite` directory
    still narrows it, the same precedence `_protect_rest_of_git_dir`'s own docstring
    already relies on for the installed binary's real behavior. `_BWRAP_UNSHARE_FLAGS`
    (not `--unshare-all`) is this suite's one namespace list — see its own comment for
    why it never unshares the network namespace.

    Each `denyWrite` path missing on the host is fabricated first
    (`_fabricate_missing_deny_write_stand_in`), so the `--ro-bind-try` below always
    binds a real, unreadable node rather than silently skipping — see that function's
    own docstring for why a bare `--ro-bind-try` cannot stand in for the real binary's
    translation of a missing path. Returns `(argv, fabricated)`: the second list is
    every path this call fabricated, for the caller to remove
    (`_remove_fabricated_stand_ins`) once it no longer needs them on disk — a caller
    that does not check for them leaves this module's own test-infrastructure artifact
    sitting where the incident's own `denyWrite` path was, not anything the sandbox or
    git put there."""
    argv = ["bwrap", *_BWRAP_UNSHARE_FLAGS, "--die-with-parent",
           "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
    fabricated = []
    for p in fs["allowWrite"]:
        argv += ["--bind-try", p, p]
    for p in fs["denyWrite"]:
        if not os.path.lexists(p) and _fabricate_missing_deny_write_stand_in(p):
            fabricated.append(p)
        argv += ["--ro-bind-try", p, p]
    return argv, fabricated


@requires_bwrap
def test_linux_bwrap_sandbox_commits_without_a_stale_lock(tmp_path, monkeypatch):
    """Exercises the fix against a real bwrap sandbox, built from this run's own rendered
    `sandbox.filesystem` (`_bwrap_argv` — the minimal translation this suite needs to
    stand in for the installed Claude Code binary's own, unavailable here).

    Not run against the pre-fix code on any bwrap host as part of writing this change:
    this worktree's own sandbox (macOS) has no bwrap, so `@requires_bwrap` skips it here
    (see the skip reason this module prints). Reverting `settings()`'s Linux branch to
    `_branch_write_paths` and running this test on a Linux host with bwrap is the
    confirmation this module's own task description asks for; expect the commit below to
    fail there with "fatal: couldn't set 'refs/heads/<branch>'" and a stale
    `refs/heads/<branch>.lock` left behind, matching the adopter's own occurrence (Debian
    12.15, bwrap, agent-platform 17d979579). With the fix, the commit and the push both
    succeed, the branch ref actually moves, and no `.lock` file is left under the git
    directory's refs."""
    monkeypatch.setattr(RS, "_dispatch_host_platform", lambda: "linux")
    branch = f"agent/sandbox-check-{uuid.uuid4().hex[:8]}"
    wt, _primary = _linked_checkout_with_branch(tmp_path, branch)
    common = RS.git_common_dir(wt)
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run(["git", "-C", str(wt), "remote", "add", "origin", str(bare)], check=True)

    fs = RS.settings(home=tmp_path, worktree=wt, git_dir=common)["sandbox"]["filesystem"]
    argv, fabricated = _bwrap_argv(fs)
    env = _bwrap_commit_env(common, wt)

    commit = subprocess.run(
        argv + ["git", "-C", str(wt), "-c", "user.name=t", "-c", "user.email=t@t",
               "commit", "-q", "--allow-empty", "-m", "sandboxed commit"],
        capture_output=True, text=True, timeout=60, env=env)
    assert commit.returncode == 0, commit.stderr

    push = subprocess.run(
        argv + ["git", "-C", str(wt), "push", "-q", "origin", f"HEAD:{branch}"],
        capture_output=True, text=True, timeout=60, env=env)
    assert push.returncode == 0, push.stderr
    _remove_fabricated_stand_ins(fabricated)

    head = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    moved = subprocess.run(["git", "-C", str(wt), "rev-parse", branch],
                           capture_output=True, text=True, check=True).stdout.strip()
    assert moved == head
    assert list(common.glob("refs/**/*.lock")) == [], "a stale .lock was left under refs"


@requires_bwrap
def test_linux_bwrap_sandbox_refuses_a_siblings_branch_ref(tmp_path, monkeypatch):
    """Under the same real sandbox as test_linux_bwrap_sandbox_commits_without_a_stale_lock,
    a sibling branch already in `refs/heads/agent/` — another dispatched run's own branch —
    stays unwritable even though this run's own `allowWrite` now covers the whole
    directory: `_protect_rest_of_git_dir`'s enumeration-based `denyWrite` is what narrows
    it, confirmed here against the real sandbox rather than only the rendered settings."""
    monkeypatch.setattr(RS, "_dispatch_host_platform", lambda: "linux")
    branch = f"agent/sandbox-check-{uuid.uuid4().hex[:8]}"
    sibling = f"agent/sandbox-check-{uuid.uuid4().hex[:8]}"
    wt, primary = _linked_checkout_with_branch(tmp_path, branch)
    subprocess.run(["git", "-C", str(primary), "branch", sibling], check=True)
    common = RS.git_common_dir(wt)
    sibling_ref = common / "refs" / "heads" / sibling
    assert sibling_ref.is_file()
    before = sibling_ref.read_bytes()

    fs = RS.settings(home=tmp_path, worktree=wt, git_dir=common)["sandbox"]["filesystem"]
    argv, fabricated = _bwrap_argv(fs)

    tamper = subprocess.run(argv + ["tee", "-a", str(sibling_ref)],
                            input="tampered\n", text=True, capture_output=True, timeout=60)
    assert tamper.returncode != 0, "a sibling branch's ref must not be writable"
    assert sibling_ref.read_bytes() == before
    _remove_fabricated_stand_ins(fabricated)


@requires_bwrap
def test_linux_bwrap_sandbox_refuses_a_commit_on_an_unnamespaced_branch(tmp_path, monkeypatch):
    """Round 2's own confirmation, against the same real sandbox
    test_linux_bwrap_sandbox_commits_without_a_stale_lock exercises: a branch with no
    namespace segment of its own (`fix-ci`, the hand-opened-PR-branch shape security
    review named) gets no write grant at all, so `refs` is denied wholesale the same
    way a detached HEAD's already is — git's own commit cannot even create the ref's
    `.lock` file, let alone move the ref, and leaves none behind."""
    monkeypatch.setattr(RS, "_dispatch_host_platform", lambda: "linux")
    branch = "fix-ci"
    wt, _primary = _linked_checkout_with_branch(tmp_path, branch)
    common = RS.git_common_dir(wt)

    fs = RS.settings(home=tmp_path, worktree=wt, git_dir=common)["sandbox"]["filesystem"]
    argv, fabricated = _bwrap_argv(fs)
    env = _bwrap_commit_env(common, wt)

    commit = subprocess.run(
        argv + ["git", "-C", str(wt), "-c", "user.name=t", "-c", "user.email=t@t",
               "commit", "-q", "--allow-empty", "-m", "sandboxed commit"],
        capture_output=True, text=True, timeout=60, env=env)
    assert commit.returncode != 0, \
        "a commit on an unnamespaced branch must fail, not succeed against a widened grant"
    _remove_fabricated_stand_ins(fabricated)
    assert list(common.glob("refs/**/*.lock")) == [], "no stale .lock may be left under refs"


def _packed_refs_write_refusals(argv: list, common: Path, wt: Path) -> dict:
    """Runs the three write attempts every packed-refs case must refuse, under the
    given real sandbox: a plain write, a rename of another file onto it, and
    creating its lock file. Returns each `subprocess.CompletedProcess`, keyed by name,
    for the caller to assert against."""
    packed_refs = common / "packed-refs"
    source = wt / "tamper-source.txt"
    source.write_text("tampered content\n")
    return {
        "write": subprocess.run(argv + ["tee", "-a", str(packed_refs)],
                                input="tampered\n", text=True, capture_output=True, timeout=60),
        "rename": subprocess.run(argv + ["mv", str(source), str(packed_refs)],
                                 capture_output=True, text=True, timeout=60),
        "lock": subprocess.run(argv + ["touch", str(common / "packed-refs.lock")],
                               capture_output=True, text=True, timeout=60),
    }


@requires_bwrap
def test_linux_bwrap_sandbox_reads_packed_refs_present_at_spawn(tmp_path, monkeypatch):
    """run-c409dcfe's control (run-14ffa02b) packed refs before committing; this is
    the present-at-spawn half of the incident this module's fix addresses. With
    `packed-refs` already holding the branch's own ref (and no loose ref file left
    for it — `git pack-refs --all` removes it), reading it under the real sandbox
    must work: `git rev-parse`, `git log` and `git commit` all read it to find the
    branch's current tip. Writing it, renaming another file onto it, and creating
    its `.lock` file all stay refused."""
    monkeypatch.setattr(RS, "_dispatch_host_platform", lambda: "linux")
    branch = f"agent/sandbox-check-{uuid.uuid4().hex[:8]}"
    wt, _primary = _linked_checkout_with_branch(tmp_path, branch)
    common = RS.git_common_dir(wt)
    subprocess.run(["git", "-C", str(wt), "pack-refs", "--all"], check=True)
    assert not (common / "refs" / "heads" / branch).exists(), \
        "the branch's ref should be packed only, with no loose file left"
    packed_refs_before = (common / "packed-refs").read_bytes()

    fs = RS.settings(home=tmp_path, worktree=wt, git_dir=common)["sandbox"]["filesystem"]
    argv, fabricated = _bwrap_argv(fs)
    env = _bwrap_commit_env(common, wt)

    rev_parse = subprocess.run(argv + ["git", "-C", str(wt), "rev-parse", branch],
                               capture_output=True, text=True, timeout=60, env=env)
    assert rev_parse.returncode == 0, rev_parse.stderr
    log = subprocess.run(argv + ["git", "-C", str(wt), "log", "--oneline", "-1"],
                         capture_output=True, text=True, timeout=60, env=env)
    assert log.returncode == 0, log.stderr

    commit = subprocess.run(
        argv + ["git", "-C", str(wt), "-c", "user.name=t", "-c", "user.email=t@t",
               "commit", "-q", "--allow-empty", "-m", "sandboxed commit"],
        capture_output=True, text=True, timeout=60, env=env)
    assert commit.returncode == 0, commit.stderr

    refusals = _packed_refs_write_refusals(argv, common, wt)
    assert refusals["write"].returncode != 0, "packed-refs must not be writable"
    assert refusals["rename"].returncode != 0, "nothing may be renamed onto packed-refs"
    assert refusals["lock"].returncode != 0, "packed-refs.lock must not be creatable"
    assert (common / "packed-refs").read_bytes() == packed_refs_before
    _remove_fabricated_stand_ins(fabricated)
    assert not (common / "packed-refs.lock").exists()


@requires_bwrap
def test_linux_bwrap_sandbox_commits_when_packed_refs_absent_at_spawn(tmp_path, monkeypatch):
    """run-c409dcfe itself: a repository made with `git init` has no `packed-refs`
    until something packs refs. `settings()`'s own fix
    (`_ensure_packed_refs_exists`) must create an empty one before this sandbox's
    `denyWrite` entry is added, so the commit below — which a fresh `git init`
    repository with no packed refs always needs — still succeeds, and writing,
    renaming onto, or locking `packed-refs` all stay refused."""
    monkeypatch.setattr(RS, "_dispatch_host_platform", lambda: "linux")
    branch = f"agent/sandbox-check-{uuid.uuid4().hex[:8]}"
    wt, _primary = _linked_checkout_with_branch(tmp_path, branch)
    common = RS.git_common_dir(wt)
    assert not (common / "packed-refs").exists(), \
        "the fixture should not have created this file itself"

    fs = RS.settings(home=tmp_path, worktree=wt, git_dir=common)["sandbox"]["filesystem"]
    assert (common / "packed-refs").exists(), "settings() should have created an empty one"
    argv, fabricated = _bwrap_argv(fs)
    env = _bwrap_commit_env(common, wt)

    commit = subprocess.run(
        argv + ["git", "-C", str(wt), "-c", "user.name=t", "-c", "user.email=t@t",
               "commit", "-q", "--allow-empty", "-m", "sandboxed commit"],
        capture_output=True, text=True, timeout=60, env=env)
    assert commit.returncode == 0, commit.stderr
    log = subprocess.run(argv + ["git", "-C", str(wt), "log", "--oneline", "-1"],
                         capture_output=True, text=True, timeout=60, env=env)
    assert log.returncode == 0, log.stderr

    refusals = _packed_refs_write_refusals(argv, common, wt)
    assert refusals["write"].returncode != 0, "packed-refs must not be writable"
    assert refusals["rename"].returncode != 0, "nothing may be renamed onto packed-refs"
    assert refusals["lock"].returncode != 0, "packed-refs.lock must not be creatable"
    assert (common / "packed-refs").read_bytes() == b""
    _remove_fabricated_stand_ins(fabricated)
    assert not (common / "packed-refs.lock").exists()


@requires_bwrap
def test_linux_bwrap_sandbox_reproduces_the_unreadable_packed_refs_incident(tmp_path, monkeypatch):
    """Proves the harness itself, not just the fix: with `_ensure_packed_refs_exists`
    disabled, `settings()`'s `denyWrite` entry for `packed-refs` names a path that does
    not exist, and run-c409dcfe is exactly this — a `git commit` against a fresh
    `git init` repository that then fails reading it. `_bwrap_argv`'s own fabrication of
    a stand-in for a missing `denyWrite` path (see its docstring) is what makes this
    reproducible on a real sandbox without the installed binary: revert that
    fabrication to a bare `--ro-bind-try` and this test fails instead, since the path
    then falls through to the ambient `/tmp` grant every non-eval, non-review dispatch
    gets and stays writable and readable — proof the un-fabricated harness could not
    have shown this fix's own bug in the first place, so the two tests either side of
    this one (present/absent at spawn) were not actually confirming the fix against a
    real sandbox until `_bwrap_argv` fabricated a missing path the way the real binary
    does."""
    monkeypatch.setattr(RS, "_dispatch_host_platform", lambda: "linux")
    monkeypatch.setattr(RS, "_ensure_packed_refs_exists", lambda git_dir: None)
    branch = f"agent/sandbox-check-{uuid.uuid4().hex[:8]}"
    wt, _primary = _linked_checkout_with_branch(tmp_path, branch)
    common = RS.git_common_dir(wt)
    assert not (common / "packed-refs").exists(), \
        "the fixture should not have created this file itself"

    fs = RS.settings(home=tmp_path, worktree=wt, git_dir=common)["sandbox"]["filesystem"]
    assert not (common / "packed-refs").exists(), \
        "the fix is disabled for this test; settings() must not have created one"
    argv, fabricated = _bwrap_argv(fs)
    env = _bwrap_commit_env(common, wt)

    commit = subprocess.run(
        argv + ["git", "-C", str(wt), "-c", "user.name=t", "-c", "user.email=t@t",
               "commit", "-q", "--allow-empty", "-m", "sandboxed commit"],
        capture_output=True, text=True, timeout=60, env=env)
    _remove_fabricated_stand_ins(fabricated)
    assert commit.returncode != 0, \
        "run-c409dcfe: a commit against a missing, unfixed packed-refs must fail"
    assert "packed-refs" in commit.stderr, commit.stderr


def test_main_and_packed_refs_are_explicitly_denied(tmp_path, monkeypatch):
    # Round 2 (reviewer + security-reviewer blocking finding), discovered live (see
    # _refs_to_protect's docstring): a path under the shared git directory that is
    # absent from BOTH allowWrite and denyWrite is still writable, unlike an ordinary
    # path elsewhere. Only an explicit denyWrite entry refuses it, so main's ref (and
    # packed-refs) must be named here, not merely left out of allowWrite.
    #
    # Pinned to darwin: `_linked_checkout`'s own branch ("b") has no namespace segment,
    # so `_branch_write_dirs` now grants it no write at all on Linux — unrelated to
    # what this test checks (denyWrite's own entries), so it keeps the exact-file
    # platform.
    monkeypatch.setattr(RS, "_dispatch_host_platform", lambda: "darwin")
    wt = _linked_checkout(tmp_path)
    common = RS.git_common_dir(wt)
    fs = RS.settings(home=tmp_path, worktree=wt, git_dir=common)["sandbox"]["filesystem"]
    assert str(common / "refs" / "heads" / "main") in fs["denyWrite"]
    assert str(common / "packed-refs") in fs["denyWrite"]
    assert str(common / "packed-refs.lock") in fs["denyWrite"]
    # The run's own branch is not denied.
    assert str(common / "refs" / "heads" / "b") not in fs["denyWrite"]


def test_packed_refs_is_created_empty_when_absent_at_spawn(tmp_path, monkeypatch):
    """run-c409dcfe: a fresh `git init` repository has no `packed-refs` until
    something packs refs. `settings()` must create an empty one before adding its
    unconditional `denyWrite` entry — see `_ensure_packed_refs_exists`'s own
    docstring for why a `denyWrite` entry on an absent path broke reads on Linux."""
    monkeypatch.setattr(RS, "_dispatch_host_platform", lambda: "darwin")
    wt = _linked_checkout(tmp_path)
    common = RS.git_common_dir(wt)
    packed_refs = common / "packed-refs"
    assert not packed_refs.exists(), "the fixture should not have created this file itself"

    fs = RS.settings(home=tmp_path, worktree=wt, git_dir=common)["sandbox"]["filesystem"]

    assert packed_refs.exists() and packed_refs.is_file()
    assert packed_refs.read_bytes() == b""
    assert str(packed_refs) in fs["denyWrite"]
    assert str(common / "packed-refs.lock") in fs["denyWrite"]


def test_packed_refs_is_left_untouched_when_present_at_spawn(tmp_path, monkeypatch):
    """A repository that already has a `packed-refs` — any repo cloned from GitHub,
    or one where something already ran `git pack-refs` — keeps its own content
    untouched; `settings()` only fills the gap, it never truncates or rewrites an
    existing file."""
    monkeypatch.setattr(RS, "_dispatch_host_platform", lambda: "darwin")
    wt = _linked_checkout(tmp_path)
    common = RS.git_common_dir(wt)
    subprocess.run(["git", "-C", str(wt), "pack-refs", "--all"], check=True)
    packed_refs = common / "packed-refs"
    before = packed_refs.read_bytes()
    assert before, "the fixture's own pack-refs should have written real content"

    fs = RS.settings(home=tmp_path, worktree=wt, git_dir=common)["sandbox"]["filesystem"]

    assert packed_refs.read_bytes() == before
    assert str(packed_refs) in fs["denyWrite"]


def test_packed_refs_symlink_is_not_followed_or_clobbered(tmp_path, monkeypatch):
    """A `packed-refs` that is already a symlink — planted or pre-existing — is
    neither followed nor replaced: `_ensure_packed_refs_exists`'s `O_EXCL` means
    the symlink's own presence at that path is enough to stop it, before
    `O_NOFOLLOW` would even matter."""
    monkeypatch.setattr(RS, "_dispatch_host_platform", lambda: "darwin")
    wt = _linked_checkout(tmp_path)
    common = RS.git_common_dir(wt)
    target = tmp_path / "elsewhere.txt"
    target.write_text("not a real packed-refs\n")
    packed_refs = common / "packed-refs"
    packed_refs.symlink_to(target)

    fs = RS.settings(home=tmp_path, worktree=wt, git_dir=common)["sandbox"]["filesystem"]

    assert packed_refs.is_symlink()
    assert packed_refs.resolve() == target.resolve()
    assert target.read_text() == "not a real packed-refs\n"
    assert str(packed_refs) in fs["denyWrite"]


def test_protect_rest_of_git_dir_excludes_only_the_named_branch(tmp_path):
    wt = _linked_checkout(tmp_path)
    primary = tmp_path / "primary"
    subprocess.run(["git", "-C", str(primary), "branch", "sibling"], check=True)
    common = RS.git_common_dir(wt)
    linked = RS._linked_gitdir(wt)
    private_dir = RS.private_objects_dir(common, linked)
    rels = {p.relative_to(common).as_posix()
            for p in RS._protect_rest_of_git_dir(common, "b", linked, private_dir)}
    assert "refs/heads/main" in rels
    assert "refs/heads/sibling" in rels
    assert "logs/refs/heads/main" in rels
    assert "refs/heads/b" not in rels
    assert "logs/refs/heads/b" not in rels


def _covered(path: Path, protect: list[Path]) -> bool:
    """True when `path` itself, or an ancestor of it, is denied — a whole directory
    denied as one path covers every path under it, not just the ones enumerated."""
    return any(p == path or p in path.parents for p in protect)


def test_protect_rest_of_git_dir_with_no_branch_protects_every_ref(tmp_path):
    # A reviewer dispatched with --pr is pinned to a commit (detached HEAD), with no
    # branch of its own to exempt. Nothing under refs/ is exempt, so the whole refs
    # directory is denied as one path rather than file by file.
    wt = _linked_checkout(tmp_path)
    common = RS.git_common_dir(wt)
    linked = RS._linked_gitdir(wt)
    protect = RS._protect_rest_of_git_dir(common, None, linked, RS.private_objects_dir(common, linked))
    assert _covered(common / "refs" / "heads" / "main", protect)
    assert _covered(common / "refs" / "heads" / "b", protect)


def test_protect_rest_of_git_dir_covers_tags_notes_and_a_sibling_worktree(tmp_path):
    # Round 2's reviewer + security-reviewer blocking finding: the round-2 version of
    # this function enumerated only refs/heads and refs/remotes, leaving refs/tags,
    # refs/notes, and every sibling worktree's own per-worktree directory unnamed and so
    # (per the measured property this module documents) still writable.
    wt = _linked_checkout(tmp_path)
    primary = tmp_path / "primary"
    subprocess.run(["git", "-C", str(primary), "tag", "a-tag"], check=True)
    subprocess.run(["git", "-C", str(primary), "-c", "user.name=t", "-c", "user.email=t@t",
                    "notes", "add", "-m", "a note", "HEAD"], check=True)
    sibling = tmp_path / "sibling"
    subprocess.run(["git", "-C", str(primary), "worktree", "add", "-q", "-b", "sibling-branch",
                    str(sibling)], check=True)
    common = RS.git_common_dir(wt)
    linked = RS._linked_gitdir(wt)
    protect = RS._protect_rest_of_git_dir(common, "b", linked, RS.private_objects_dir(common, linked))
    assert _covered(common / "refs" / "tags" / "a-tag", protect)
    assert _covered(common / "refs" / "notes" / "commits", protect)
    # The sibling's whole per-worktree directory is denied as one path, not file by
    # file: a file created under it after this call (its own next commit) still refuses.
    sibling_dir = common / "worktrees" / "sibling"
    assert sibling_dir in protect
    assert _covered(sibling_dir / "config.worktree", protect)
    # This run's own linked gitdir is not swept into the deny list at all (its own
    # commit writes index/HEAD/ORIG_HEAD there; GIT_POINTER_FILES are added back by
    # settings(), not by this function).
    assert not _covered(linked / "index", protect)


def test_protect_rest_of_git_dir_with_no_linked_gitdir_exempts_this_worktrees_own_files(tmp_path):
    # The worktree IS git_dir's own main checkout (no linked gitdir) — a repo-local
    # (keeps_primary) dispatch. Its own index/HEAD/ORIG_HEAD/COMMIT_EDITMSG/logs/HEAD sit
    # directly at the top of git_dir, so they must be exempt there instead of under a
    # `linked` subtree — the one case `_branch_write_paths` does not cover, and the one
    # case the private objects dir must be named explicitly for too (round 6): with no
    # `linked` to fall under, it would otherwise be swept into the wholesale deny below
    # the same way a brand-new name anywhere else under git_dir would be.
    primary = tmp_path / "primary"
    primary.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(primary)], check=True)
    subprocess.run(["git", "-C", str(primary), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    common = RS.git_common_dir(primary)
    private_dir = RS.private_objects_dir(common, None)
    private_dir.mkdir()
    rels = {p.relative_to(common).as_posix()
            for p in RS._protect_rest_of_git_dir(common, "main", None, private_dir)}
    for own in ("HEAD", "index", "ORIG_HEAD", "COMMIT_EDITMSG", "FETCH_HEAD", "logs/HEAD",
                RS.PRIVATE_OBJECTS_DIRNAME):
        assert own not in rels, own
    assert "config" in rels
    assert "hooks" in rels


def test_protect_rest_of_git_dir_denies_objects_wholesale(tmp_path):
    # Round 3 denied `objects/info` and an existing pack; round 4 swept every pack/index
    # file present at spawn; round 5 denied `info` and `pack` as whole directories so a
    # name invented after spawn (`multi-pack-index`) was covered too. Each round's
    # reviewer + security-reviewer still found the structural gap underneath all of them:
    # an EXISTING LOOSE OBJECT could be deleted and recreated under the same hash-named
    # path (`rm` only needs write permission on its fanout directory, not the 0444 file),
    # and no ordinary git read ever catches the substitution. Round 6 denies `objects`
    # wholesale — not just `info`/`pack` — so a loose object is covered the same way a
    # pack file or an invented name is. Repack so a real pack/idx pair exists, and write a
    # loose object directly, before `_protect_rest_of_git_dir` runs, to confirm both
    # shapes are covered, not just one.
    wt = _linked_checkout(tmp_path)
    primary = tmp_path / "primary"
    subprocess.run(["git", "-C", str(primary), "repack", "-ad", "-q"], check=True)
    loose_hash = subprocess.run(
        ["git", "-C", str(primary), "hash-object", "-w", "--stdin"],
        input="a loose blob\n", text=True, capture_output=True, check=True).stdout.strip()
    common = RS.git_common_dir(wt)
    linked = RS._linked_gitdir(wt)
    packs = sorted((common / "objects" / "pack").iterdir())
    assert packs, "the repack above should have produced at least one pack/index file"
    loose_object = common / "objects" / loose_hash[:2] / loose_hash[2:]
    assert loose_object.is_file(), "the hash-object above should have written a loose object"
    private_dir = RS.private_objects_dir(common, linked)
    protect = RS._protect_rest_of_git_dir(common, "b", linked, private_dir)
    assert _covered(common / "objects" / "info", protect)
    assert _covered(common / "objects" / "info" / "alternates", protect)
    for p in packs:
        assert _covered(p, protect), p
    # A name no git fetch would ever write, and that this fixture never created — the
    # round-4 gap — is covered by the same wholesale directory deny, not by name.
    assert _covered(common / "objects" / "pack" / "multi-pack-index", protect)
    # A name a REAL future fetch would write is covered too: `objects/pack` is closed
    # outright, not narrowed to "what already exists."
    assert _covered(common / "objects" / "pack" / "pack-not-yet-fetched.pack", protect)
    # Round 6's own gap: an EXISTING loose object, deletable and recreatable under its own
    # name, is now covered too — `objects` is denied as one path, not narrowed to info/pack.
    assert _covered(loose_object, protect)
    # And so is a loose object this fixture never wrote — the wholesale deny covers the
    # whole fanout, not just what already exists.
    assert _covered(common / "objects" / "ab" / "cdef0123456789", protect)
    # This run's own private object store is exempt from the wholesale deny — it is where
    # the run's own git writes instead.
    assert not _covered(private_dir, protect)


def test_every_file_under_the_shared_git_dir_is_either_this_runs_write_or_denied(tmp_path, monkeypatch):
    """The property round 2 claimed and did not have, pinned directly rather than through
    one example at a time: enumerate every file that exists under a realistic shared git
    directory — a sibling worktree, tags, notes, a second remote, packed-refs, and the
    primary's own HEAD — and check each one against the settings `settings()` actually
    builds, not against a hand-written list of paths to check (the shape round 2 had).

    Kept under the real HOME-rooted `tmp_path` fixture (pytest's own `tmp_path` sits under
    `$TMPDIR`, same caution as the live check's docstring) is unnecessary here: this test
    checks the SETTINGS DATA this module builds, not live sandbox enforcement, so it carries
    none of the `$TMPDIR`-is-writable confound the live check has to avoid.

    Pinned to darwin: the fixture's own branch ("b") has no namespace segment, so
    `_branch_write_dirs` now grants it no write at all on Linux — unrelated to the
    allow/deny coverage this test checks, so it keeps the exact-file platform."""
    monkeypatch.setattr(RS, "_dispatch_host_platform", lambda: "darwin")
    primary = tmp_path / "primary"
    wt = tmp_path / "linked"
    sibling = tmp_path / "sibling"
    bare = tmp_path / "origin.git"
    backup = tmp_path / "backup.git"
    subprocess.run(["git", "init", "-q", "-b", "main", str(primary)], check=True)
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run(["git", "init", "-q", "--bare", str(backup)], check=True)
    subprocess.run(["git", "-C", str(primary), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    subprocess.run(["git", "-C", str(primary), "remote", "add", "origin", str(bare)], check=True)
    subprocess.run(["git", "-C", str(primary), "remote", "add", "backup", str(backup)], check=True)
    subprocess.run(["git", "-C", str(primary), "push", "-q", "origin", "main"], check=True)
    subprocess.run(["git", "-C", str(primary), "push", "-q", "backup", "main"], check=True)
    subprocess.run(["git", "-C", str(primary), "tag", "a-tag"], check=True)
    subprocess.run(["git", "-C", str(primary), "-c", "user.name=t", "-c", "user.email=t@t",
                    "notes", "add", "-m", "a note", "HEAD"], check=True)
    subprocess.run(["git", "-C", str(primary), "worktree", "add", "-q", "-b", "b", str(wt)], check=True)
    subprocess.run(["git", "-C", str(primary), "worktree", "add", "-q", "-b", "sibling-branch",
                    str(sibling)], check=True)
    subprocess.run(["git", "-C", str(wt), "push", "-q", "origin", "b"], check=True)
    subprocess.run(["git", "-C", str(primary), "pack-refs", "--all"], check=True)
    # Round 3's miss (reviewer + security-reviewer blocking finding): `objects/info` and
    # `objects/pack` are not content-addressed the way a loose object is. `alternates`
    # always exists as a directory by the time a repo has one commit; writing a file there
    # is enough to exercise the gap without it, but a real repack is what populates `pack`.
    (RS.git_common_dir(primary) / "objects" / "info" / "alternates").write_text(
        "/tmp/not-a-real-alternate\n")
    subprocess.run(["git", "-C", str(primary), "repack", "-ad", "-q"], check=True)
    # A loose object created AFTER the repack above, so a hash-prefix directory under
    # `objects` exists distinctly from `info` and `pack` for the loose-object assertion below.
    subprocess.run(["git", "-C", str(primary), "hash-object", "-w", "--stdin"],
                    input="a loose blob\n", text=True, check=True)

    common = RS.git_common_dir(wt)
    fs = RS.settings(home=tmp_path, worktree=wt, git_dir=common)["sandbox"]["filesystem"]
    allow = [Path(p) for p in fs["allowWrite"]]
    deny = [Path(p) for p in fs["denyWrite"]]

    # Round 4's miss (reviewer + security-reviewer blocking finding), reproduced in the real
    # order: a name invented AFTER `settings()` ran — nothing has enumerated it yet to deny
    # it by name — the same shape a run's own write would take. Written only now, after `fs`
    # above was already computed, so a per-spawn enumeration (round 4's `_protect_objects`)
    # could not have swept it in; a wholesale directory deny (round 5's) still covers it.
    multi_pack_index = common / "objects" / "pack" / "multi-pack-index"
    multi_pack_index.write_bytes(b"fake")

    files = [f for f in common.rglob("*") if f.is_file()]
    assert len(files) > 15, "the fixture should produce a realistic spread of git-dir files"
    packs = [p for p in (common / "objects" / "pack").iterdir() if p != multi_pack_index]
    assert packs, "the repack above should have produced at least one pack/index file"
    uncovered = [f for f in files if not _covered(f, allow) and not _covered(f, deny)]
    assert not uncovered, f"writable by neither allow nor deny: {uncovered}"

    # The two round-2 misses, confirmed denied rather than merely "not uncovered" above.
    assert _covered(common / "HEAD", deny)
    assert _covered(common / "worktrees" / "sibling" / "config.worktree", deny)
    assert _covered(common / "refs" / "tags" / "a-tag", deny)
    assert _covered(common / "refs" / "notes" / "commits", deny)
    assert _covered(common / "packed-refs", deny)
    # Round 3's miss: `objects/info` (any file under it, including one this fixture never
    # wrote) and every pack/index file already on disk are denied, not merely "not uncovered".
    assert _covered(common / "objects" / "info" / "alternates", deny)
    assert _covered(common / "objects" / "info" / "packs", deny)
    for p in packs:
        assert _covered(p, deny), p
    # Round 4's miss: a name invented after spawn (`multi-pack-index`, written above) is
    # covered by the same wholesale `objects/pack` deny, not because it was enumerated.
    assert _covered(common / "objects" / "pack" / "multi-pack-index", deny)
    # The run's own commit-and-push write stays allowed.
    assert _covered(common / "refs" / "heads" / "b", allow)
    assert _covered(common / "refs" / "remotes" / "origin" / "b", allow)
    # Round 6: `objects` is denied wholesale now, including every loose object already on
    # disk — the structural gap rounds 3-5 left (an existing loose object deletable and
    # recreatable under its own name) is closed by covering the whole directory, not by
    # narrowing it to info/pack the way round 5 did.
    assert _covered(common / "objects", deny)
    loose = next(d for d in (common / "objects").iterdir() if d.name not in ("info", "pack"))
    # Not also checked against `allow` here: this fixture's `tmp_path` sits under
    # `$TMPDIR`, one of the sandbox's own scratch roots, so EVERY path in it reads as
    # allow-covered for that unrelated reason (see this test's own docstring on why that
    # confound is harmless for the settings-DATA checks this test makes, and why the live
    # check roots its own scratch state under the real HOME instead to avoid it).
    assert _covered(loose, deny)
    # The run's own private object store is exempt from the wholesale deny — it is where
    # the run's own git writes instead.
    linked = RS._linked_gitdir(wt)
    private_dir = RS.private_objects_dir(common, linked)
    assert not _covered(private_dir, deny)


def test_current_branch_reads_the_worktrees_own_head_file(tmp_path):
    wt = _linked_checkout(tmp_path)
    assert RS.current_branch(wt) == "b"
    primary = tmp_path / "primary"
    assert RS.current_branch(primary) == "main"


def test_current_branch_is_none_for_a_detached_head(tmp_path):
    # A reviewer dispatched with --pr is pinned to a commit, not a branch.
    wt = _linked_checkout(tmp_path)
    subprocess.run(["git", "-C", str(wt), "checkout", "-q", "--detach"], check=True)
    assert RS.current_branch(wt) is None


# --- import_private_objects: a finished run's objects rejoin the shared store through
# git's own verified transfer, never a file copy (knowledge#793 round 6) ------------------

def _loose_object_path(objects_dir: Path, oid: str) -> Path:
    return objects_dir / oid[:2] / oid[2:]


def _hash_object(store: Path, content: str) -> str:
    """Write `content` as a loose blob directly into `store` (a bare object directory —
    NOT necessarily the repo's own), and return its hash."""
    env = {**os.environ, "GIT_DIR": str(store.parent), "GIT_OBJECT_DIRECTORY": str(store)}
    return subprocess.run(["git", "hash-object", "-w", "--stdin"], input=content,
                          env=env, text=True, capture_output=True, check=True).stdout.strip()


def test_run_private_objects_dir_resolves_linked_and_main_checkout_cases(tmp_path):
    wt = _linked_checkout(tmp_path)
    common = RS.git_common_dir(wt)
    linked = RS._linked_gitdir(wt)
    assert RS.run_private_objects_dir(wt, common) == linked / RS.PRIVATE_OBJECTS_DIRNAME

    primary = tmp_path / "primary"
    assert RS.run_private_objects_dir(primary, common) == common / RS.PRIVATE_OBJECTS_DIRNAME


def test_import_private_objects_is_a_no_op_without_a_private_dir(tmp_path):
    wt = _linked_checkout(tmp_path)
    common = RS.git_common_dir(wt)
    # Must not raise just because the directory a dry run (or a non-git dispatch) never
    # created is absent.
    RS.import_private_objects(common, common / "never-created")


def test_import_private_objects_moves_a_commit_into_the_shared_store(tmp_path):
    """The ordinary case: a run committed with `GIT_OBJECT_DIRECTORY` pointed at its own
    private store, and `import_private_objects` is what makes that commit resolvable by
    PLAIN git afterward — no env override, exactly how `agentctl publish`/`pr-open`/a
    revise coder re-spawned on the same worktree/a reviewer worktree all read it."""
    wt = _linked_checkout(tmp_path)
    common = RS.git_common_dir(wt)
    private_dir = RS.run_private_objects_dir(wt, common)
    private_dir.mkdir()

    env = {**os.environ, "GIT_OBJECT_DIRECTORY": str(private_dir),
          "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(common / "objects")}
    subprocess.run(["git", "-C", str(wt), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "--allow-empty", "-m", "private-store commit"],
                   env=env, check=True)
    new_head = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
    # It genuinely landed in the private store, not the shared one — the premise the rest
    # of this test depends on.
    assert _loose_object_path(private_dir, new_head).is_file()
    assert not _loose_object_path(common / "objects", new_head).is_file()

    RS.import_private_objects(common, private_dir)

    # Plain git, no override at all: the commit is readable from the shared store alone.
    shown = subprocess.run(["git", "-C", str(wt), "show", "-s", "--format=%s", new_head],
                           capture_output=True, text=True, check=True).stdout.strip()
    assert shown == "private-store commit"
    assert _loose_object_path(common / "objects", new_head).is_file() or any(
        (common / "objects" / "pack").glob("*.pack"))
    # Emptied once its contents are safely in the shared store.
    assert not private_dir.exists()


def test_import_private_objects_never_lets_a_tampered_name_reach_the_shared_store(tmp_path):
    """index-pack verifies by RECOMPUTING each object's hash from its content, rather
    than trusting the name it was handed — so a loose object file whose path-derived name
    doesn't match its own content (the exact shape of the round-3-6 shared-store defect
    this PR closes, reproduced here inside the run's OWN private store rather than the
    shared one) can never make it into the shared store under the wrong name."""
    wt = _linked_checkout(tmp_path)
    common = RS.git_common_dir(wt)
    private_dir = RS.run_private_objects_dir(wt, common)
    private_dir.mkdir(parents=True)

    real_a = _hash_object(private_dir, "AAAA\n")
    real_b = _hash_object(private_dir, "BBBB\n")
    # Clobber A's path with B's content, the same `rm` + recreate the reviews describe:
    # a loose object's file is mode 0444, so unlinking (needs only the DIRECTORY's write
    # permission) and recreating is how the substitution happens, not an in-place write.
    a_path = _loose_object_path(private_dir, real_a)
    a_path.unlink()
    shutil.copyfile(_loose_object_path(private_dir, real_b), a_path)

    RS.import_private_objects(common, private_dir)

    shared_env = {**os.environ, "GIT_DIR": str(common)}
    shared_env.pop("GIT_ALTERNATE_OBJECT_DIRECTORIES", None)
    b_content = subprocess.run(["git", "cat-file", "-p", real_b], env=shared_env,
                               capture_output=True, text=True, check=True).stdout
    assert b_content == "BBBB\n"
    # Real A's content never existed anywhere by the time of the import (clobbered above),
    # so nothing in the transferred pack ever computes to A's hash — it was never written.
    a_present = subprocess.run(["git", "cat-file", "-e", real_a], env=shared_env,
                               capture_output=True, text=True).returncode
    assert a_present != 0, "a tampered name reached the shared store under its claimed hash"


def test_import_private_objects_leaves_an_already_present_object_untouched(tmp_path):
    wt = _linked_checkout(tmp_path)
    common = RS.git_common_dir(wt)
    shared_objects = common / "objects"
    shared_oid = _hash_object(shared_objects, "already here\n")
    existing_path = _loose_object_path(shared_objects, shared_oid)
    before = existing_path.read_bytes()

    private_dir = RS.run_private_objects_dir(wt, common)
    private_dir.mkdir()
    # The same content, written independently into the private store — the only way a
    # name could ever collide, since a name IS its content's hash.
    _hash_object(private_dir, "already here\n")

    RS.import_private_objects(common, private_dir)

    assert existing_path.read_bytes() == before


# --- import_private_objects(remove=False): catching a LIVE run's objects up into the
# shared store periodically, WITHOUT deleting the private store a still-running run
# could still write into (the successor to knowledge#793's own window) ------------------


def test_a_live_runs_unimported_commit_breaks_fetch_in_any_other_checkout(tmp_path):
    """The 2026-10-07 incident (two merge coders running): while a run's branch ref, in
    the SHARED ref store every worktree of this repo reads, points at a commit that
    exists only in that run's private store, `git fetch` in ANY OTHER checkout sharing
    these objects fails outright — not a read of the run's own worktree, a read of
    anything sharing its object database. Reproduced here exactly as measured: a plain
    `git fetch origin`, no `--all`, no `rev-list`, in a SEPARATE primary checkout that
    pushed this same branch earlier and has nothing new to fetch."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "trunk", str(origin)], check=True)
    primary = tmp_path / "primary"
    subprocess.run(["git", "init", "-q", "-b", "trunk", str(primary)], check=True)

    def g(*a, cwd=primary, env=None, check=True):
        return subprocess.run(
            ["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *a],
            capture_output=True, text=True, env=env, check=check)

    g("commit", "-q", "--allow-empty", "-m", "base")
    g("remote", "add", "origin", str(origin))
    g("push", "-q", "origin", "trunk")

    g("worktree", "add", "-q", "-b", "agent/foo", str(tmp_path / "wt"))
    wt = tmp_path / "wt"
    git_dir = RS.git_common_dir(wt)
    private_dir = RS.run_private_objects_dir(wt, git_dir)
    private_dir.mkdir()
    env = {**os.environ, "GIT_OBJECT_DIRECTORY": str(private_dir),
          "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(git_dir / "objects")}
    g("commit", "-q", "--allow-empty", "-m", "private, unimported", cwd=wt, env=env)

    # The bug: while the commit sits only in the private store, the branch ref it
    # advanced (shared, read by every worktree of THIS repo) breaks a fetch run from
    # the PRIMARY — a checkout that never touched the run's worktree at all.
    broken = g("fetch", "origin", cwd=primary, check=False)
    assert broken.returncode != 0, "expected the unimported commit to break fetch"
    assert "bad object" in broken.stderr

    # The fix: import while the run is still alive (remove=False) — the private store
    # stays in place (a live run could still write into it), but the branch's commit is
    # now ALSO safely in the shared store, so the dangling-object problem is gone.
    RS.import_private_objects(git_dir, private_dir, remove=False)
    assert private_dir.exists(), "remove=False must not delete a live run's private store"

    fixed = g("fetch", "origin", cwd=primary, check=False)
    assert fixed.returncode == 0, fixed.stderr


def test_import_private_objects_remove_false_skips_already_imported_objects(tmp_path):
    """A long-running coder's dispatcher calls this every tick while the run is alive
    (`bin/agentctl`'s `_stream_transcript`). Tick N+1 must not re-pack what tick N
    already moved into the shared store — repacking a GROWING set every ~20s for an
    hours-long run would otherwise bloat the shared store with near-duplicate packs."""
    wt = _linked_checkout(tmp_path)
    common = RS.git_common_dir(wt)
    private_dir = RS.run_private_objects_dir(wt, common)
    private_dir.mkdir()
    env = {**os.environ, "GIT_OBJECT_DIRECTORY": str(private_dir),
          "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(common / "objects")}
    subprocess.run(["git", "-C", str(wt), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "--allow-empty", "-m", "tick 1"],
                   env=env, check=True)

    RS.import_private_objects(common, private_dir, remove=False)
    packs_after_first = set((common / "objects" / "pack").glob("*.pack"))
    assert packs_after_first, "the first tick must have packed something"

    # A second tick with nothing new written since: every object it finds in the
    # private store is already in the shared store, so no new pack is created.
    RS.import_private_objects(common, private_dir, remove=False)
    packs_after_second = set((common / "objects" / "pack").glob("*.pack"))
    assert packs_after_second == packs_after_first
    assert private_dir.exists()


def test_import_private_objects_remove_false_still_catches_a_second_commit(tmp_path):
    """Two ticks, two commits in between: the second tick must still pick up what the
    first left behind, not just skip everything because SOME of it is already there."""
    wt = _linked_checkout(tmp_path)
    common = RS.git_common_dir(wt)
    private_dir = RS.run_private_objects_dir(wt, common)
    private_dir.mkdir()
    env = {**os.environ, "GIT_OBJECT_DIRECTORY": str(private_dir),
          "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(common / "objects")}

    def commit(msg):
        subprocess.run(["git", "-C", str(wt), "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-q", "--allow-empty", "-m", msg], env=env, check=True)

    commit("tick 1")
    RS.import_private_objects(common, private_dir, remove=False)
    commit("tick 2")
    second_head = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"],
                                 capture_output=True, text=True, check=True).stdout.strip()
    RS.import_private_objects(common, private_dir, remove=False)

    shared_env = {**os.environ, "GIT_DIR": str(common)}
    shared_env.pop("GIT_ALTERNATE_OBJECT_DIRECTORIES", None)
    present = subprocess.run(["git", "cat-file", "-e", second_head], env=shared_env).returncode
    assert present == 0, "tick 2's commit never reached the shared store"


def test_import_private_objects_remove_true_still_cleans_up_after_a_remove_false_tick(tmp_path):
    """The run's own end still does the one `remove=True` call (the existing call
    sites, unchanged) — it must still remove the directory even though earlier,
    live-run ticks already imported most of its contents."""
    wt = _linked_checkout(tmp_path)
    common = RS.git_common_dir(wt)
    private_dir = RS.run_private_objects_dir(wt, common)
    private_dir.mkdir()
    env = {**os.environ, "GIT_OBJECT_DIRECTORY": str(private_dir),
          "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(common / "objects")}
    subprocess.run(["git", "-C", str(wt), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "--allow-empty", "-m", "final commit"],
                   env=env, check=True)

    RS.import_private_objects(common, private_dir, remove=False)
    assert private_dir.exists()
    RS.import_private_objects(common, private_dir)
    assert not private_dir.exists()


def test_a_revise_round_on_the_same_worktree_reads_the_first_rounds_commit(tmp_path):
    """Caller 3 of knowledge#793 round 6's item 3: `agentctl spawn --pr` re-dispatches a
    revise coder onto the SAME worktree a prior round already committed in. That second
    run gets its OWN fresh private dir (`dispatch_env` always gives one) and never needs
    the first round's — it only needs to READ the earlier commit, which plain git (no
    override at all) already can, because `import_private_objects` rejoined it into the
    shared store before this round's dispatch ever started."""
    wt = _linked_checkout(tmp_path)
    common = RS.git_common_dir(wt)

    round1_dir = RS.run_private_objects_dir(wt, common)
    round1_dir.mkdir()
    env = {**os.environ, "GIT_OBJECT_DIRECTORY": str(round1_dir),
          "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(common / "objects")}
    subprocess.run(["git", "-C", str(wt), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "--allow-empty", "-m", "round 1"],
                   env=env, check=True)
    RS.import_private_objects(common, round1_dir)

    # Round 2's own (fresh, now-empty since round 1's was removed by the import above)
    # private dir — a distinct instance, never reused across rounds.
    round2_dir = RS.run_private_objects_dir(wt, common)
    assert not round2_dir.exists()
    round2_dir.mkdir()
    log = subprocess.run(["git", "-C", str(wt), "log", "--oneline", "-1"],
                         capture_output=True, text=True, check=True).stdout
    assert "round 1" in log


def test_a_reviewer_worktree_fetching_the_pushed_branch_gets_the_full_commit(tmp_path):
    """Caller 4 of knowledge#793 round 6's item 3: a reviewer's worktree is a SEPARATE
    checkout that fetches the already-pushed branch from origin — it never touches this
    run's private store at all. It only works because the push (through
    `import_private_objects` + `agentctl publish`) put the REAL objects on origin, not
    just a ref."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    primary = tmp_path / "primary"
    subprocess.run(["git", "init", "-q", "-b", "main", str(primary)], check=True)

    def g(*a, cwd=primary, env=None):
        return subprocess.run(
            ["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *a],
            check=True, capture_output=True, text=True, env=env)

    g("commit", "-q", "--allow-empty", "-m", "base")
    g("remote", "add", "origin", str(origin))
    g("push", "-q", "origin", "main")
    wt = tmp_path / "wt"
    g("worktree", "add", "-q", "-b", "agent/for-review", str(wt))

    git_dir = RS.git_common_dir(wt)
    private_dir = RS.run_private_objects_dir(wt, git_dir)
    private_dir.mkdir()
    env = {**os.environ, "GIT_OBJECT_DIRECTORY": str(private_dir),
          "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(git_dir / "objects")}
    (wt / "f.py").write_text("for the reviewer\n")
    g("add", ".", cwd=wt, env=env)
    g("commit", "-qm", "work for review", cwd=wt, env=env)
    RS.import_private_objects(git_dir, private_dir)
    g("push", "-q", "origin", "agent/for-review", cwd=wt)

    reviewer_wt = tmp_path / "reviewer"
    subprocess.run(["git", "clone", "-q", str(origin), str(reviewer_wt)], check=True)
    subprocess.run(["git", "-C", str(reviewer_wt), "checkout", "-q", "agent/for-review"], check=True)
    assert (reviewer_wt / "f.py").read_text() == "for the reviewer\n"


def test_a_run_cannot_write_the_git_files_that_repoint_git(tmp_path, monkeypatch):
    # knowledge#793 condition 1: a run that rewrote one of these could make git run a command
    # the run wrote, with core.fsmonitor or core.hooksPath, on the next git command in the worktree.
    #
    # Pinned to darwin: `_linked_checkout`'s own branch ("b") has no namespace segment,
    # so `_branch_write_dirs` now grants it no write at all on Linux — unrelated to
    # what this test checks (the git-pointer-file denials), so it keeps the exact-file
    # platform.
    monkeypatch.setattr(RS, "_dispatch_host_platform", lambda: "darwin")
    wt = _linked_checkout(tmp_path)
    common = RS.git_common_dir(wt)
    gitdir = _git_path(wt, "--git-dir")
    settings = RS.settings(home=tmp_path, worktree=wt, git_dir=common)
    deny_write = settings["sandbox"]["filesystem"]["denyWrite"]
    for path in (wt / ".git", gitdir / "commondir", gitdir / "gitdir", gitdir / "config.worktree",
                 common / "hooks", common / "config"):
        assert str(path) in deny_write, path
    # The Edit and Write tools run outside the sandbox. Edit(worktree/**) allows the worktree's
    # `.git` file, so it needs its own Edit rule.
    assert f"Edit({_rule(wt / '.git')})" in settings["permissions"]["deny"]


def test_git_common_dir_is_none_outside_a_checkout(tmp_path):
    assert RS.git_common_dir(tmp_path) is None


def test_passwd_home_ignores_the_environment(monkeypatch, tmp_path):
    before = RS.passwd_home()
    monkeypatch.setenv("HOME", str(tmp_path / "elsewhere"))
    assert RS.passwd_home() == before
    assert before == Path(pwd.getpwuid(os.getuid()).pw_dir)


def test_settings_json_round_trips(tmp_path):
    import json
    text = RS.settings_json(home=tmp_path, worktree=tmp_path / "wt")
    assert "\n" not in text
    assert json.loads(text) == RS.settings(home=tmp_path, worktree=tmp_path / "wt")


@pytest.mark.parametrize("layout", ROOT_LAYOUTS)
def test_the_library_directory_and_the_orient_api_token_file_are_denied(tmp_path, monkeypatch, layout):
    monkeypatch.setattr(RS.BR, "root", lambda: RS.BR.real_account_home() / layout)
    settings = RS.settings(home=tmp_path, worktree=tmp_path / "wt")
    lib = tmp_path / ".library"
    api_tokens = tmp_path / layout / "convivy-orient" / "api-tokens.json"
    deny = settings["permissions"]["deny"]
    # The whole directory: client-tokens.json, a saved person login, ci-fetch-token,
    # the Library's databases and tls/server.key all sit in it.
    assert f"Read({_rule(lib)}/**)" in deny
    assert f"Edit({_rule(lib)}/**)" in deny
    # One file. Its directory holds ordinary checkout files, which stay readable.
    assert f"Read({_rule(api_tokens)})" in deny
    assert f"Edit({_rule(api_tokens)})" in deny
    assert f"Read({_rule(api_tokens.parent)}/**)" not in deny
    fs = settings["sandbox"]["filesystem"]
    assert str(lib) in fs["denyRead"]
    assert str(api_tokens) in fs["denyRead"]
    # Spawn passes the passwd home. The real file is denied there, whatever layout the root has.
    real = RS.BR.real_account_home() / layout / "convivy-orient" / "api-tokens.json"
    real_deny = RS.settings(home=RS.passwd_home(), worktree=tmp_path / "wt")["permissions"]["deny"]
    assert f"Read({_rule(real)})" in real_deny


@pytest.mark.parametrize("layout", ROOT_LAYOUTS)
def test_the_extra_deny_home_adds_denials_and_removes_none(tmp_path, monkeypatch, layout):
    monkeypatch.setattr(RS.BR, "root", lambda: RS.BR.real_account_home() / layout)
    # The confinement test points this at stand-in credentials while spawn keeps its real
    # HOME. It must add to the denial, never replace the passwd-home rules.
    extra = tmp_path / "standins"
    passwd = tmp_path / "passwd"
    monkeypatch.setenv(RS.EXTRA_DENY_HOME_ENV, str(extra))
    s = RS.settings(home=passwd, worktree=tmp_path / "wt")
    deny = s["permissions"]["deny"]
    assert f"Read({_rule(extra / '.orient-control')}/**)" in deny
    assert f"Read({_rule(extra / layout / 'convivy-orient' / 'api-tokens.json')})" in deny
    assert f"Read({_rule(passwd / '.orient-control')}/**)" in deny
    fs = s["sandbox"]["filesystem"]
    assert str(extra / ".library") in fs["denyRead"]
    assert str(passwd / ".orient-control") in fs["denyRead"]
    # Epic E33 slice 2: `extra` is denied (never re-opened) even though it is not
    # the dispatch's own `home` or worktree — nothing in the base read-allowlist is
    # keyed off it.
    assert str(extra) not in fs["allowRead"]
    assert not any(Path(p) == extra or extra in Path(p).parents for p in fs["allowRead"])


def test_the_deck_token_is_denied_by_the_orient_control_directory(tmp_path):
    # deck-library-token sits inside .orient-control, so the directory rule covers it.
    # The confinement test tries it by name, so the rule it relies on is pinned here.
    settings = RS.settings(home=tmp_path, worktree=tmp_path / "wt")
    assert str(tmp_path / ".orient-control") in settings["sandbox"]["filesystem"]["denyRead"]
    assert f"Read({_rule(tmp_path / '.orient-control')}/**)" in settings["permissions"]["deny"]


# --- the operator's git/gh credential, and the App key that signs this run's OWN
# minted token (CQ·418) -------------------------------------------------------------

def test_the_gh_config_directory_is_denied(tmp_path):
    # hosts.yml, where gh stores the operator's token when it isn't in the keychain.
    settings = RS.settings(home=tmp_path, worktree=tmp_path / "wt")
    gh_dir = tmp_path / ".config" / "gh"
    deny = settings["permissions"]["deny"]
    assert f"Read({_rule(gh_dir)}/**)" in deny
    assert f"Edit({_rule(gh_dir)}/**)" in deny
    assert str(gh_dir) in settings["sandbox"]["filesystem"]["denyRead"]


def test_the_keychains_directory_is_denied(tmp_path):
    settings = RS.settings(home=tmp_path, worktree=tmp_path / "wt")
    keychains = tmp_path / "Library" / "Keychains"
    deny = settings["permissions"]["deny"]
    assert f"Read({_rule(keychains)}/**)" in deny
    assert f"Edit({_rule(keychains)}/**)" in deny
    assert str(keychains) in settings["sandbox"]["filesystem"]["denyRead"]


def test_the_ssh_directory_is_denied(tmp_path):
    # run-c36c0559 read ~/.ssh/github_ed25519 and pushed as the operator on
    # 2026-10-19 (CQ·506 step S8).
    settings = RS.settings(home=tmp_path, worktree=tmp_path / "wt")
    ssh_dir = tmp_path / ".ssh"
    deny = settings["permissions"]["deny"]
    assert f"Read({_rule(ssh_dir)}/**)" in deny
    assert f"Edit({_rule(ssh_dir)}/**)" in deny
    assert str(ssh_dir) in settings["sandbox"]["filesystem"]["denyRead"]


def test_the_gitconfig_file_is_denied(tmp_path):
    # Where credential.https://github.com.helper points at gh.
    settings = RS.settings(home=tmp_path, worktree=tmp_path / "wt")
    gitconfig = tmp_path / ".gitconfig"
    deny = settings["permissions"]["deny"]
    assert f"Read({_rule(gitconfig)})" in deny
    assert f"Edit({_rule(gitconfig)})" in deny
    assert str(gitconfig) in settings["sandbox"]["filesystem"]["denyRead"]
    # Only the file, not its directory — the home itself stays otherwise readable.
    assert f"Read({_rule(tmp_path)}/**)" not in deny


def test_the_bosun_app_key_is_denied(tmp_path):
    # The key that signs the JWT minting THIS run's own installation token
    # (bin/agentctl's _mint_run_github_token) — read by the dispatcher, never by a run.
    settings = RS.settings(home=tmp_path, worktree=tmp_path / "wt")
    key = tmp_path / ".config" / "convivy" / "bosun-app.pem"
    deny = settings["permissions"]["deny"]
    assert f"Read({_rule(key)})" in deny
    assert f"Edit({_rule(key)})" in deny
    assert str(key) in settings["sandbox"]["filesystem"]["denyRead"]


def test_the_installed_app_key_is_denied(tmp_path):
    # The install's own GitHub App key, as `github_app.installed_app()` resolves it
    # (issue_01M69GD1WEN67MZPSBQZD02CDN) — a dispatched run must be denied this path
    # regardless of which of the two names the install's key file is reached by.
    settings = RS.settings(home=tmp_path, worktree=tmp_path / "wt")
    key = tmp_path / ".config" / "convivy" / "github-app" / "app.pem"
    deny = settings["permissions"]["deny"]
    assert f"Read({_rule(key)})" in deny
    assert f"Edit({_rule(key)})" in deny
    assert str(key) in settings["sandbox"]["filesystem"]["denyRead"]


def test_a_symlinked_installed_app_key_is_denied_through_its_target(tmp_path):
    # an install that kept the older key file: `.config/convivy/github-app/app.pem` is a symlink to
    # `.config/convivy/bosun-app.pem`. `path_denied` must deny the symlink's target
    # path too, not only the literal `github-app/app.pem` name — `Path.resolve()`
    # is what makes that true (round 1 of #732's own symlinked-key posture).
    home = tmp_path / "home"
    convivy_dir = home / ".config" / "convivy"
    convivy_dir.mkdir(parents=True)
    github_app_dir = convivy_dir / "github-app"
    github_app_dir.mkdir()
    target = convivy_dir / "bosun-app.pem"
    target.write_text("placeholder")
    (github_app_dir / "app.pem").symlink_to(target)

    assert RS.path_denied(github_app_dir / "app.pem", [home]) is True
    assert RS.path_denied(target, [home]) is True


def test_the_bosun_ledger_reporter_token_is_denied(tmp_path):
    # The credential the Library will mint for agentctl's ledger client to POST spend
    # entries (including subscription-lane ones) — read by the dispatcher, never by a
    # run. Denied ahead of the Library minting it, per the steward's request.
    settings = RS.settings(home=tmp_path, worktree=tmp_path / "wt")
    token = tmp_path / ".config" / "convivy" / "bosun-ledger-reporter.token"
    deny = settings["permissions"]["deny"]
    assert f"Read({_rule(token)})" in deny
    assert f"Edit({_rule(token)})" in deny
    assert str(token) in settings["sandbox"]["filesystem"]["denyRead"]


def test_the_codex_login_credential_is_denied(tmp_path):
    # The Codex CLI's own login credential (holds the operator's ChatGPT subscription
    # grant since the CLI logged in on this machine, 2026-10-17). Codex is launched by
    # the dispatcher, outside a run's sandbox, so a run never needs to read it.
    settings = RS.settings(home=tmp_path, worktree=tmp_path / "wt")
    auth = tmp_path / ".codex" / "auth.json"
    deny = settings["permissions"]["deny"]
    assert f"Read({_rule(auth)})" in deny
    assert f"Edit({_rule(auth)})" in deny
    assert str(auth) in settings["sandbox"]["filesystem"]["denyRead"]
    # Only the file, not its directory — a later Codex runner may still need to
    # read the directory's own configuration.
    assert f"Read({_rule(tmp_path / '.codex')}/**)" not in deny


def test_the_claude_sign_in_credential_is_denied(tmp_path):
    # Claude Code's own sign-in file. A dispatched coder read it into its run log on
    # 2026-10-07. Claude Code is launched by the dispatcher, outside a run's sandbox,
    # so a run never needs to read it.
    settings = RS.settings(home=tmp_path, worktree=tmp_path / "wt")
    creds = tmp_path / ".claude" / ".credentials.json"
    deny = settings["permissions"]["deny"]
    assert f"Read({_rule(creds)})" in deny
    assert f"Edit({_rule(creds)})" in deny
    assert str(creds) in settings["sandbox"]["filesystem"]["denyRead"]
    # Only the file, not its directory — a run's own CLAUDE.md render lives under it.
    assert f"Read({_rule(tmp_path / '.claude')}/**)" not in deny


def test_the_credential_helper_paths_are_denied_for_every_home(tmp_path, monkeypatch):
    # Same "caller's own HOME too" shape as test_the_denial_covers_the_callers_home_too.
    caller_home = tmp_path / "caller-home"
    monkeypatch.setenv("HOME", str(caller_home))
    settings = RS.settings(home=tmp_path / "passwd-home", worktree=tmp_path / "wt")
    deny_read = settings["sandbox"]["filesystem"]["denyRead"]
    for home in (tmp_path / "passwd-home", caller_home):
        assert str(home / ".config" / "gh") in deny_read
        assert str(home / "Library" / "Keychains") in deny_read
        assert str(home / ".ssh") in deny_read
        assert str(home / ".gitconfig") in deny_read
        assert str(home / ".config" / "convivy" / "bosun-app.pem") in deny_read
        assert str(home / ".config" / "convivy" / "github-app" / "app.pem") in deny_read
        assert str(home / ".config" / "convivy" / "bosun-ledger-reporter.token") in deny_read
        assert str(home / ".codex" / "auth.json") in deny_read
        assert str(home / ".claude" / ".credentials.json") in deny_read


def test_the_extra_deny_home_also_covers_the_credential_helper_paths(tmp_path, monkeypatch):
    extra = tmp_path / "standins"
    passwd = tmp_path / "passwd"
    monkeypatch.setenv(RS.EXTRA_DENY_HOME_ENV, str(extra))
    deny_read = RS.settings(home=passwd, worktree=tmp_path / "wt")["sandbox"]["filesystem"]["denyRead"]
    assert str(extra / ".config" / "gh") in deny_read
    assert str(extra / ".ssh") in deny_read
    assert str(extra / ".gitconfig") in deny_read
    assert str(extra / ".config" / "convivy" / "bosun-app.pem") in deny_read
    assert str(extra / ".config" / "convivy" / "github-app" / "app.pem") in deny_read
    assert str(extra / ".config" / "convivy" / "bosun-ledger-reporter.token") in deny_read
    assert str(extra / ".codex" / "auth.json") in deny_read
    assert str(extra / ".claude" / ".credentials.json") in deny_read


# --- closing the dispatched-run write channel into the orchestrator's own
# auto-memory (every worktree of this repo resolves to the same project slug as
# the primary checkout, so a run in any of them can otherwise reach the memory
# the orchestrator's own session loads as trusted notes) -------------------------

def test_the_auto_memory_directory_is_denied_for_writing_in_both_layers(tmp_path):
    settings = RS.settings(home=tmp_path, worktree=tmp_path / "wt")
    deny_write = settings["sandbox"]["filesystem"]["denyWrite"]
    deny = settings["permissions"]["deny"]
    glob = str(tmp_path / ".claude" / "projects" / "*" / "memory")
    assert glob in deny_write
    assert f"Edit({_rule(Path(glob))}/**)" in deny
    # Pull the entries straight back out of settings()'s own output, rather than
    # re-deriving the pattern to test against, so a wrong rendering (missing the
    # wildcard segment, wrong depth, no Edit() counterpart) fails here instead of
    # a self-built pattern passing regardless of what settings() produced.
    rendered_write_glob = next(e for e in deny_write if e.endswith("/memory"))
    rendered_edit_rule = next(d for d in deny if d.startswith("Edit(") and d.endswith("/memory/**)"))
    edit_pattern = rendered_edit_rule[len("Edit("):-1]
    # The glob must cover ANY project slug, not only the run's own repository's —
    # a run can Edit or Write to any absolute path it names — and a FILE nested
    # under memory/, not only the directory itself. Live-confirmed 2026-10-18: under
    # a sandbox denying writes to a bare directory glob like this one, a Bash write
    # anywhere under that directory fails, so a trailing `/**` on the rendered glob
    # is the right shape to test "falls under it", and the mid-path `*` is a real
    # wildcard there, not a literal character.
    for slug in ("agent-platform", "-Users-example-src-other-repo-worktree"):
        for rel in ("MEMORY.md", "sub/file.md"):
            memory_file = tmp_path / ".claude" / "projects" / slug / "memory" / rel
            assert fnmatch.fnmatch(str(memory_file), f"{rendered_write_glob}/**"), \
                f"sandbox.filesystem.denyWrite does not cover {slug}/{rel}: {rendered_write_glob}"
            assert fnmatch.fnmatch(_rule(memory_file), edit_pattern), \
                f"permissions.deny does not cover {slug}/{rel}: {edit_pattern}"


def test_the_auto_memory_deny_covers_the_callers_home_too(tmp_path, monkeypatch):
    # Same "this home and the caller's own" shape as test_the_denial_covers_the_callers_home_too:
    # a nested spawn can run with a HOME it chose, and both must lose the write.
    caller_home = tmp_path / "caller-home"
    monkeypatch.setenv("HOME", str(caller_home))
    settings = RS.settings(home=tmp_path / "passwd-home", worktree=tmp_path / "wt")
    deny_write = settings["sandbox"]["filesystem"]["denyWrite"]
    assert str(tmp_path / "passwd-home" / ".claude" / "projects" / "*" / "memory") in deny_write
    assert str(caller_home / ".claude" / "projects" / "*" / "memory") in deny_write
    deny = settings["permissions"]["deny"]
    assert f"Edit({_rule(caller_home / '.claude' / 'projects' / '*' / 'memory')}/**)" in deny


def test_the_auto_memory_deny_does_not_cover_the_session_transcript_file(tmp_path):
    # The harness's own transcript write, <slug>/<session>.jsonl, sits as a SIBLING of
    # the memory/ subdirectory this change denies, never inside it — so it must stay
    # unmatched by either layer's deny rule.
    settings = RS.settings(home=tmp_path, worktree=tmp_path / "wt")
    transcript = tmp_path / ".claude" / "projects" / "some-slug" / "some-session-id.jsonl"
    assert not any(fnmatch.fnmatch(str(transcript), p)
                   for p in settings["sandbox"]["filesystem"]["denyWrite"])
    edit_denies = [p[len("Edit("):-1] for p in settings["permissions"]["deny"] if p.startswith("Edit(")]
    assert not any(fnmatch.fnmatch(_rule(transcript), p) for p in edit_denies)


def test_auto_memory_is_disabled_for_every_dispatch(tmp_path):
    # Every dispatch kind sets `autoMemoryEnabled: false`, so the harness neither loads
    # MEMORY.md into the run nor writes it back; that is separate from the write deny
    # above and, like it, leaves a run's own Bash/Read calls to these files untouched.
    # Checked for an ordinary dispatch, an eval dispatch and a review-class dispatch
    # alike — none of this module's branches may omit it.
    ordinary = RS.settings(home=tmp_path, worktree=tmp_path / "wt")
    eval_dispatch = RS.settings(home=tmp_path, worktree=tmp_path / "wt",
                                eval_scratch_dir=tmp_path / "scratch")
    review = RS.settings(home=tmp_path, worktree=tmp_path / "wt", review_allowed=("Read",))
    for s in (ordinary, eval_dispatch, review):
        assert s["autoMemoryEnabled"] is False


def test_settings_json_includes_auto_memory_disabled(tmp_path):
    import json

    rendered = json.loads(RS.settings_json(home=tmp_path, worktree=tmp_path / "wt"))
    assert rendered["autoMemoryEnabled"] is False


def test_mach_lookup_names_only_trustd_so_the_keychains_service_is_denied_by_omission():
    """CQ·418: the keychain's own mach service is never named here, and the list is an
    allowlist under Seatbelt's default-deny — so it is refused by omission, not by an
    explicit entry. This pins that nothing was added to the allowlist in this change's
    name; see run_sandbox.py's MACH_LOOKUP comment and the PR body for what the co's own
    live check (outside this suite) found."""
    assert RS.MACH_LOOKUP == ("com.apple.trustd.agent",)


def test_file_tools_are_allowed_only_in_the_worktree_and_scratch(tmp_path):
    wt = tmp_path / "wt"
    allow = RS.settings(home=tmp_path, worktree=wt)["permissions"]["allow"]
    edits = [r for r in allow if r.startswith("Edit(")]
    assert sorted(edits) == sorted([f"Edit({_rule(wt)}/**)", "Edit(//tmp/**)", "Edit(//private/tmp/**)"])
    # Write and NotebookEdit are governed by Edit rules, so no bare Write or Edit may appear.
    assert "Write" not in allow and "Edit" not in allow
    assert not any(r.startswith("Write(") for r in allow)


def _edit_rule_covers(rule: str, path: Path) -> bool:
    """True when the Edit allow rule `rule` covers `path`, under the same prefix
    match dontAsk applies to an `Edit(//a/b/**)` rule. A bare `Edit`, or a pattern
    with no path prefix (`Edit(**)`, `Edit(//**)`), covers every target, and so
    does any other `Edit(...)` shape this does not recognise: the regression this
    guards is exactly an allow entry broader than intended, so an unrecognised
    shape must read as covering everything, never as safely narrow."""
    if rule == "Edit" or rule in ("Edit(**)", "Edit(//**)"):
        return True
    if not (rule.startswith("Edit(//") and rule.endswith("/**)")):
        return True
    root = "/" + rule[len("Edit(//"):-len("/**)")]
    return str(path) == root or str(path).startswith(root + "/")


def test_no_primary_checkout_falls_under_any_edit_allow_rule(tmp_path):
    """CQ·435. The confinement probe Read-then-Edit-ed bin/co, bin/mcp-ops-server and
    tools/rollover-trigger.py in the primary agent-platform checkout from a dispatched
    run, each with an anchor absent from the file, and each came back "String to
    replace not found in file." -- the same result the probe's own positive control
    (the run's worktree README, also missing its anchor) got. That result alone
    cannot tell a permission refusal from Edit's own anchor check running first. A
    follow-up probe used a REAL anchor on a harmless file outside every primary
    checkout and outside the worktree/scratch allow list, run from inside a
    dispatched run (this PR's own), and Claude Code refused it at the permission
    layer ("Claude Code is running in don't ask mode") -- settling the three "not
    found" results as the validation-order reading, not a real hole.

    This pins the reason that refusal holds at the settings level: for a worktree
    laid out the way a real dispatch is (`<root>/worktrees/<repo>.<purpose>`), no
    path under any primary checkout (agent-platform, knowledge, library) is covered
    by any Edit allow rule. `test_file_tools_are_allowed_only_in_the_worktree_and_scratch`
    already pins the allow list to exactly the worktree and the two scratch roots;
    this confirms those three roots never reach a primary checkout, for the specific
    paths the probe named.

    `root` is a synthetic absolute path, not `tmp_path`: pytest's own `tmp_path`
    sits under `$TMPDIR`, which on this machine resolves under `/private/tmp` --
    one of `SCRATCH_ROOTS` itself (see `test_every_file_under_the_shared_git_dir_is_either_this_runs_write_or_denied`'s
    docstring on the same confound) -- so every path under it would read as
    allow-covered for that unrelated reason. `settings()` builds the Edit allow
    list from `worktree` and the literal `SCRATCH_ROOTS` strings alone, with no
    filesystem access, so a synthetic root exercises the real logic without it.
    `home` is built under the same synthetic root, beside the primary checkouts,
    the way it sits on the real machine; `settings()` never folds `home` into
    the Edit allow list, so a target under it must read as uncovered too.
    """
    root = Path("/synthetic-convivy-root-for-cq435")
    worktree = root / "worktrees" / "agent-platform.some-run"
    home = root / "home"
    allow = RS.settings(home=home, worktree=worktree)["permissions"]["allow"]
    edit_rules = [r for r in allow if r == "Edit" or r.startswith("Edit(")]
    targets = [
        root / "agent-platform" / "bin" / "co",
        root / "agent-platform" / "bin" / "mcp-ops-server",
        root / "agent-platform" / "tools" / "rollover-trigger.py",
        root / "knowledge" / "projects" / "some-doc.md",
        root / "library" / "some_module.py",
        home / ".claude" / "settings.json",
    ]
    for target in targets:
        assert not any(_edit_rule_covers(r, target) for r in edit_rules), target


def test_the_run_keeps_the_tools_its_recorded_calls_use(tmp_path):
    # WebFetch/WebSearch are excluded here on purpose (epic E33 slice 3): they are
    # granted per agent through `web_tools`, never unconditionally — see
    # test_web_tools_default_grants_neither_tool and its siblings below.
    allow = RS.settings(home=tmp_path, worktree=tmp_path / "wt")["permissions"]["allow"]
    for tool in ("Bash", "Read", "Grep", "Glob", "Agent", "ToolSearch",
                 "mcp__ops", "mcp__kb", "mcp__verdict"):
        assert tool in allow, tool


def test_a_dispatched_run_is_named_so_its_processes_can_find_it():
    assert RS.run_name("run-abc") == "agentctl-dispatched-run:run-abc"


def _ancestry(monkeypatch, table, start=100):
    """Stand in for `ps -o ppid=,command= -p <pid>` over `table` (pid -> (ppid, argv))."""
    class Done:
        def __init__(self, stdout, returncode=0):
            self.stdout, self.returncode = stdout, returncode

    def fake_run(argv, **kw):
        pid = int(argv[argv.index("-p") + 1])
        if pid not in table:
            return Done("", 1)
        ppid, command = table[pid]
        return Done(f"{ppid} {command}\n")

    monkeypatch.setattr(RS.os, "getppid", lambda: start)
    monkeypatch.setattr(RS.subprocess, "run", fake_run)


def test_a_process_under_a_dispatched_run_sees_the_marker(monkeypatch):
    _ancestry(monkeypatch, {
        100: (90, "/bin/zsh -c agentctl spawn reviewer"),
        90: (80, "claude -p --name agentctl-dispatched-run:run-abc --settings {}"),
        80: (1, "/sbin/launchd"),
    })
    assert _inside_dispatched_run() is True


def test_a_co_session_is_not_under_a_dispatched_run(monkeypatch):
    _ancestry(monkeypatch, {
        100: (90, "/bin/zsh -c agentctl spawn reviewer"),
        90: (80, "claude --remote-control --name co-agent-platform"),
        80: (1, "/sbin/launchd"),
    })
    assert _inside_dispatched_run() is False


def test_an_unreadable_process_table_is_treated_as_inside(monkeypatch):
    # Fail closed: the one caller refuses dispatch on True.
    _ancestry(monkeypatch, {})
    assert _inside_dispatched_run() is True


def test_the_marker_is_read_from_the_ancestor_argv_not_the_environment(monkeypatch):
    # A run's shell clears an environment variable with `env -u`. The check reads the
    # argv of the claude ancestor, which the run cannot rewrite.
    _ancestry(monkeypatch, {
        100: (90, "agentctl spawn reviewer"),
        90: (80, "claude -p --name agentctl-dispatched-run:run-abc"),
        80: (1, "launchd"),
    })
    assert _inside_dispatched_run() is True


def test_sandbox_denies_read_is_true_on_a_permission_error(tmp_path):
    probe = tmp_path / "denied"
    probe.write_text("x")
    probe.chmod(0o000)
    try:
        assert RS._sandbox_denies_read(probe) is True
    finally:
        probe.chmod(0o600)


def test_sandbox_denies_read_is_false_when_readable_or_absent(tmp_path):
    readable = tmp_path / "readable"
    readable.write_text("x")
    assert RS._sandbox_denies_read(readable) is False
    assert RS._sandbox_denies_read(tmp_path / "missing") is False


def test_the_sandbox_denial_signal_is_checked_before_the_ancestor_walk(monkeypatch):
    # Round 2 (reviewer + security-reviewer): a double-forked orphan reparented to pid 1
    # still cannot shed the kernel's own sandbox confinement, unlike ps ancestry — this is
    # the primary signal, and ps must never even run once it has already answered True.
    monkeypatch.setattr(RS, "_sandbox_denies_read", lambda path: True)

    def boom(*a, **k):
        raise AssertionError("ps must not run once the sandbox denial already answered True")

    monkeypatch.setattr(RS.subprocess, "run", boom)
    assert _inside_dispatched_run() is True


def test_a_chain_longer_than_the_ancestor_limit_fails_closed(monkeypatch):
    # Round 2 (reviewer + security-reviewer blocking finding): on 678a9caf, a table shaped
    # like this made the old code return False — a run's Bash can interpose more than
    # _ANCESTOR_LIMIT processes (nested subshells) before calling `agentctl spawn`, hiding
    # the marker from a walk that gave up and answered "not inside" instead of failing
    # closed. The sandbox-denial signal is forced off here so this test exercises only the
    # ancestor walk's own exhaustion branch.
    monkeypatch.setattr(RS, "_sandbox_denies_read", lambda path: False)
    depth = RS._ANCESTOR_LIMIT + 10
    table = {10_000 - i: (10_000 - i - 1, "/bin/sh -c sleep 1") for i in range(depth)}
    _ancestry(monkeypatch, table, start=10_000)
    assert _inside_dispatched_run() is True


# --- platform/specs/reviewers-run-under-their-allow-list ----------------------


def test_review_draft_dir_is_keyed_on_the_worktrees_own_name(tmp_path):
    home = tmp_path / "home"
    wt = tmp_path / "extras" / "widget.reviewer-pr123-review-it"
    assert RS.review_draft_dir(home, wt) == home / RS.REVIEW_DRAFT_RELDIR / wt.name


def test_create_review_draft_dir_makes_an_owner_only_directory_and_is_idempotent(tmp_path):
    home = tmp_path / "home"
    wt = tmp_path / "wt"
    d = RS.create_review_draft_dir(home, wt)
    assert d.is_dir()
    assert d == RS.review_draft_dir(home, wt)
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    # A re-dispatch onto the same worktree must find the SAME directory, not a fresh one.
    (d / "draft.md").write_text("in progress")
    again = RS.create_review_draft_dir(home, wt)
    assert again == d
    assert (d / "draft.md").read_text() == "in progress"


def test_two_worktrees_get_distinct_draft_directories(tmp_path):
    home = tmp_path / "home"
    a = RS.create_review_draft_dir(home, tmp_path / "wt-a")
    b = RS.create_review_draft_dir(home, tmp_path / "wt-b")
    assert a != b


def test_without_review_allowed_settings_are_byte_identical_to_before(tmp_path):
    """coder/scout/planner/etc. never pass `review_allowed` or `draft_dir` — adding
    both parameters must not change one bit of their settings. Fails if either
    parameter's branch is taken when it should not be."""
    runs = tmp_path / "agent-runs"
    without_params = RS.settings_json(home=tmp_path, worktree=tmp_path / "wt", runs_root=runs)
    with_params_explicitly_none = RS.settings_json(
        home=tmp_path, worktree=tmp_path / "wt", runs_root=runs,
        review_allowed=None, draft_dir=None)
    assert without_params == with_params_explicitly_none
    allow = RS.settings(home=tmp_path, worktree=tmp_path / "wt")["permissions"]["allow"]
    # The exact base this module shipped with — see TOOL_ALLOW's own comment on why
    # a review-class dispatch no longer gets it, and why every other agent still does.
    assert allow[:len(RS.TOOL_ALLOW)] == list(RS.TOOL_ALLOW)
    sandbox = RS.settings(home=tmp_path, worktree=tmp_path / "wt")["sandbox"]
    fs = sandbox["filesystem"]
    assert "/tmp" in fs["allowWrite"]
    assert os.path.realpath("/tmp") in fs["allowWrite"]
    # Calibration (2026-10-17, platform/decisions/0926 proposed): this key is added
    # only on the `review_allowed is not None` branch — every other caller's sandbox
    # block keeps today's shape, with no `autoAllowBashIfSandboxed` key at all.
    assert "autoAllowBashIfSandboxed" not in sandbox


def test_review_allowed_replaces_tool_allow_entirely(tmp_path):
    # The incident fix itself: no bare Bash, no whole-server mcp__ops/mcp__kb grant —
    # only the resolved policy's own rules, plus ToolSearch.
    policy_rules = ("Read", "Grep", "Bash(git diff:*)", "mcp__kb__kb_get",
                    "mcp__verdict__review_post")
    settings = RS.settings(home=tmp_path, worktree=tmp_path / "wt",
                           review_allowed=policy_rules)
    allow = settings["permissions"]["allow"]
    assert "Bash" not in allow
    assert "mcp__ops" not in allow
    assert "mcp__kb" not in allow
    assert "mcp__verdict" not in allow
    assert "WebFetch" not in allow
    assert "WebSearch" not in allow
    assert "Agent" not in allow
    assert "Skill" not in allow
    for rule in policy_rules:
        assert rule in allow
    assert "ToolSearch" in allow
    # Calibration (2026-10-17, platform/decisions/0926 proposed): the allow-list
    # above is not what refuses a Bash command the sandbox can otherwise run — left
    # unset, `sandbox.autoAllowBashIfSandboxed` auto-allows it regardless of
    # `permissions`. A review-class dispatch must also carry this key set to `false`.
    assert settings["sandbox"]["autoAllowBashIfSandboxed"] is False


def test_review_allowed_with_no_draft_dir_grants_no_scratch_write_at_all(tmp_path):
    # /tmp is dropped outright for a review-class dispatch (TOOL_ALLOW's own comment);
    # with no draft_dir given, there is nothing to grant in its place.
    settings = RS.settings(home=tmp_path, worktree=tmp_path / "wt", review_allowed=("Read",))
    fs = settings["sandbox"]["filesystem"]
    assert "/tmp" not in fs["allowWrite"]
    assert os.path.realpath("/tmp") not in fs["allowWrite"]
    allow = settings["permissions"]["allow"]
    assert "Edit(//tmp/**)" not in allow
    assert "Edit(//private/tmp/**)" not in allow
    # The worktree itself is still writable (a review's `Write` tool reaches it).
    assert f"Edit({_rule(tmp_path / 'wt')}/**)" in allow


def test_review_allowed_with_a_draft_dir_grants_exactly_that_directory(tmp_path):
    draft = tmp_path / "home" / RS.REVIEW_DRAFT_RELDIR / "wt"
    settings = RS.settings(home=tmp_path, worktree=tmp_path / "wt",
                           review_allowed=("Read",), draft_dir=draft)
    fs = settings["sandbox"]["filesystem"]
    assert str(draft) in fs["allowWrite"]
    assert "/tmp" not in fs["allowWrite"]
    allow = settings["permissions"]["allow"]
    assert f"Edit({_rule(draft)}/**)" in allow
    assert "Edit(//tmp/**)" not in allow


def test_review_allowed_never_perturbs_runs_root_denial(tmp_path):
    # The review carve-out changes the WRITE allow-list, never the runs_root deny — a
    # review-class dispatch must not be able to write under runs_root any more than
    # any other dispatch can.
    runs = tmp_path / "agent-runs"
    settings = RS.settings(home=tmp_path, worktree=tmp_path / "wt", runs_root=runs,
                           review_allowed=("Read",))
    fs = settings["sandbox"]["filesystem"]
    assert str(runs) in fs["denyWrite"]
    assert str(runs) not in fs["allowWrite"]
    assert f"Edit({_rule(runs)}/**)" in settings["permissions"]["deny"]


def test_review_allowed_does_not_duplicate_toolsearch_if_the_policy_already_names_it(tmp_path):
    allow = RS.settings(home=tmp_path, worktree=tmp_path / "wt",
                        review_allowed=("Read", "ToolSearch"))["permissions"]["allow"]
    assert allow.count("ToolSearch") == 1


def test_settings_json_threads_review_allowed_and_draft_dir(tmp_path):
    import json as _json

    draft = tmp_path / "draft"
    rendered = _json.loads(RS.settings_json(
        home=tmp_path, worktree=tmp_path / "wt", review_allowed=("Read",), draft_dir=draft))
    assert "Bash" not in rendered["permissions"]["allow"]
    assert str(draft) in rendered["sandbox"]["filesystem"]["allowWrite"]


# --- the shared deny list and the Codex renderer (platform/specs/codex-coder-lane,
# slice 1: read confinement) ----------------------------------------------------

def test_denied_read_paths_matches_every_entry_in_the_shared_lists(tmp_path):
    """denied_read_paths is the one list settings() and codex_permission_overrides
    both render from — this pins it to exactly DENIED_DIRS, DENIED_HOME_FILES and
    each home's own Orient token file, the same set settings() built inline before
    this refactor."""
    home = tmp_path / "home"
    other = tmp_path / "other-home"
    dirs, files = RS.denied_read_paths([home, other])
    assert set(dirs) == {h / d for h in (home, other) for d in RS.DENIED_DIRS}
    assert set(files) == ({h / f for h in (home, other) for f in RS.DENIED_HOME_FILES}
                          | {RS._orient_token_file(h) for h in (home, other)})


def test_path_denied_is_true_for_a_denied_dir_and_a_path_under_it(tmp_path):
    home = tmp_path / "home"
    dirs, _ = RS.denied_read_paths([home])
    denied_dir = dirs[0]
    assert RS.path_denied(denied_dir, [home]) is True
    assert RS.path_denied(denied_dir / "nested" / "file.txt", [home]) is True


def test_path_denied_is_true_for_a_denied_file(tmp_path):
    home = tmp_path / "home"
    _, files = RS.denied_read_paths([home])
    assert RS.path_denied(files[0], [home]) is True


def test_path_denied_is_false_for_an_ordinary_path(tmp_path):
    home = tmp_path / "home"
    ordinary = tmp_path / "work" / "probe-canary.txt"
    assert RS.path_denied(ordinary, [home]) is False


def test_path_denied_is_true_for_an_unreadable_path(tmp_path, monkeypatch):
    """Fail closed: a path `Path.resolve()` cannot walk (a permission error) denies,
    the same posture a missing confinement stamp already takes elsewhere."""
    home = tmp_path / "home"

    def _raise(self, strict=False):
        raise OSError("denied")

    monkeypatch.setattr(Path, "resolve", _raise)
    assert RS.path_denied(tmp_path / "anything", [home]) is True


def test_settings_denied_read_and_edit_rules_come_from_denied_read_paths(tmp_path):
    """settings()'s own denyRead/Read(...)/Edit(...) rules must still carry exactly
    what denied_read_paths names for this home and the caller's own, on top of each
    home itself (epic E33 slice 2's own wholesale home denial) — not a second,
    possibly-diverging enumeration (the refactor this slice makes)."""
    home = tmp_path / "home"
    dirs, files = RS.denied_read_paths([home, Path.home()])
    settings = RS.settings(home=home, worktree=tmp_path / "wt")
    fs = settings["sandbox"]["filesystem"]
    deny = settings["permissions"]["deny"]
    assert set(fs["denyRead"]) == {str(home), str(Path.home())} | {str(p) for p in [*dirs, *files]}
    for d in dirs:
        assert f"Read({_rule(d)}/**)" in deny
        assert f"Edit({_rule(d)}/**)" in deny
    for f in files:
        assert f"Read({_rule(f)})" in deny
        assert f"Edit({_rule(f)})" in deny


def test_codex_permission_overrides_denies_the_whole_home(tmp_path, monkeypatch):
    """Epic E33 slice 4: the hand-probe syntax (platform/specs/codex-coder-lane,
    2026-10-19) still applies — one `-c` flag, one TOML inline table — but the
    renderer now denies `home` WHOLESALE rather than only the shared credential
    list, the same home-wide deny a Claude dispatch's own `denyRead` already
    carries (`settings()`'s own comment on that)."""
    monkeypatch.setattr(RS.BR, "real_account_home", lambda: tmp_path)
    monkeypatch.setattr(RS.BR, "root", lambda: tmp_path / "convivy")
    home = tmp_path / "home"
    worktree = tmp_path / "wt"
    flag, value = RS.codex_permission_overrides("agentctl_read_only", home, worktree=worktree)
    assert flag == "-c"
    assert value.startswith("permissions.agentctl_read_only.filesystem={")
    assert value.endswith("}")
    assert '":root"="read"' in value.replace(" ", "")
    assert f'{json.dumps(str(home))}={json.dumps("deny")}' in value


def test_codex_permission_overrides_reopens_exactly_the_read_allowlist(tmp_path, monkeypatch):
    """Every path `read_allowlist` names for this home and worktree is reopened
    "read", and no other key (besides `:root` and `home` itself) appears — the
    renderer adds nothing of its own beyond what `read_allowlist` already computes
    for a Claude dispatch's own `allowRead`."""
    monkeypatch.setattr(RS.BR, "real_account_home", lambda: tmp_path)
    monkeypatch.setattr(RS.BR, "root", lambda: tmp_path / "convivy")
    home = tmp_path / "home"
    worktree = tmp_path / "wt"
    _, value = RS.codex_permission_overrides("agentctl_read_only", home, worktree=worktree)
    allowed = RS.read_allowlist(home=home, worktree=worktree)
    for p in allowed:
        assert f'{json.dumps(str(p))}={json.dumps("read")}' in value
    table = value.split("={", 1)[1].rstrip("}")
    keys = set(re.findall(r'"((?:[^"\\]|\\.)*)"=', table))
    assert keys == {":root", str(home)} | {str(p) for p in allowed}


def test_codex_permission_overrides_re_denies_a_shared_list_entry_nested_in_an_allowlist_dir(
        tmp_path, monkeypatch):
    """Task 3's own guard: a path `denied_read_paths` names, found nested inside a
    directory `read_allowlist` reopens, is re-added as its own "deny" entry on top
    of that directory's "read" — the more specific entry (the hand probe's own
    confirmed precedence) must be the credential's deny, not the directory's read.

    `read_allowlist` itself holds no such overlap today
    (test_no_read_allowlist_entry_contains_or_equals_a_denied_path pins that for
    every entry it returns) — this constructs one by hand, through
    `READ_ALLOWLIST_RELDIRS`, so the renderer's own belt-and-suspenders check is
    pinned now rather than left untested because the overlap it guards against
    does not exist yet."""
    monkeypatch.setattr(RS.BR, "real_account_home", lambda: tmp_path)
    monkeypatch.setattr(RS.BR, "root", lambda: tmp_path / "convivy")
    monkeypatch.setattr(RS, "READ_ALLOWLIST_RELDIRS", (Path("."),))
    monkeypatch.setattr(RS, "READ_ALLOWLIST_RELFILES", ())
    home = tmp_path / "home"
    worktree = tmp_path / "wt"
    _, value = RS.codex_permission_overrides("agentctl_read_only", home, worktree=worktree)
    assert f'{json.dumps(str(home))}={json.dumps("read")}' in value
    dirs, files = RS.denied_read_paths([home])
    for p in [*dirs, *files]:
        assert f'{json.dumps(str(p))}={json.dumps("deny")}' in value


def test_codex_permission_overrides_denies_each_extra_deny_home_wholesale(tmp_path, monkeypatch):
    """`extra_deny_homes` (codex_probe's own scratch canary home) denies each one
    wholesale too, with nothing reopened inside it — never a `read_allowlist` of
    its own, since it is a throwaway directory of placeholder files, not a real
    account home."""
    monkeypatch.setattr(RS.BR, "real_account_home", lambda: tmp_path)
    monkeypatch.setattr(RS.BR, "root", lambda: tmp_path / "convivy")
    home = tmp_path / "home"
    canary_home = tmp_path / "canary-home"
    worktree = tmp_path / "wt"
    _, value = RS.codex_permission_overrides("agentctl_read_only", home, worktree=worktree,
                                             extra_deny_homes=(canary_home,))
    assert f'{json.dumps(str(canary_home))}={json.dumps("deny")}' in value
    for d in RS.READ_ALLOWLIST_RELDIRS:
        assert str(canary_home / d) not in value
    for f in RS.READ_ALLOWLIST_RELFILES:
        assert str(canary_home / f) not in value


def test_codex_permission_overrides_threads_git_dir_to_the_read_allowlist(tmp_path, monkeypatch):
    """`git_dir` reaches `read_allowlist` the same way `worktree` does — reopened
    "read" on top of the home-wide deny, exactly as a Claude dispatch's own
    `allowRead` already carries it."""
    monkeypatch.setattr(RS.BR, "real_account_home", lambda: tmp_path)
    monkeypatch.setattr(RS.BR, "root", lambda: tmp_path / "convivy")
    home = tmp_path / "home"
    worktree = tmp_path / "wt"
    git_dir = tmp_path / "gitdir"
    _, value = RS.codex_permission_overrides("agentctl_read_only", home, worktree=worktree,
                                             git_dir=git_dir)
    assert f'{json.dumps(str(git_dir))}={json.dumps("read")}' in value


def test_codex_permission_overrides_uses_only_this_homes_copy(tmp_path, monkeypatch):
    """Unlike settings(), which also denies the CALLER's own home (a nested spawn's
    HOME), the renderer takes exactly the one home it is given — Codex itself runs
    outside the sandbox and is launched directly by the dispatcher with no nested
    spawn of its own to worry about."""
    monkeypatch.setattr(RS.BR, "real_account_home", lambda: tmp_path)
    monkeypatch.setattr(RS.BR, "root", lambda: tmp_path / "convivy")
    home = tmp_path / "home"
    worktree = tmp_path / "wt"
    _, value = RS.codex_permission_overrides("agentctl_read_only", home, worktree=worktree)
    assert str(Path.home()) not in value


# --- epic E33 slice 3: web tools are granted per agent, through `web_tools` -------

def test_tool_allow_holds_neither_web_tool():
    assert "WebFetch" not in RS.TOOL_ALLOW
    assert "WebSearch" not in RS.TOOL_ALLOW


def test_web_tools_default_grants_neither_tool(tmp_path):
    allow = RS.settings(home=tmp_path, worktree=tmp_path / "wt")["permissions"]["allow"]
    assert "WebFetch" not in allow
    assert "WebSearch" not in allow


def test_web_tools_grants_exactly_what_is_passed(tmp_path):
    allow = RS.settings(home=tmp_path, worktree=tmp_path / "wt",
                        web_tools=("WebFetch", "WebSearch"))["permissions"]["allow"]
    assert "WebFetch" in allow
    assert "WebSearch" in allow


def test_web_tools_grants_only_the_one_tool_named(tmp_path):
    allow = RS.settings(home=tmp_path, worktree=tmp_path / "wt",
                        web_tools=("WebSearch",))["permissions"]["allow"]
    assert "WebSearch" in allow
    assert "WebFetch" not in allow


def test_web_tools_does_not_duplicate_a_name_tool_allow_already_carried(tmp_path, monkeypatch):
    # Neither name is in TOOL_ALLOW today, but the dedupe (`_unique`) must hold even
    # if that ever changes — never two copies of the same rule in `permissions.allow`.
    monkeypatch.setattr(RS, "TOOL_ALLOW", RS.TOOL_ALLOW + ("WebFetch",))
    allow = RS.settings(home=tmp_path, worktree=tmp_path / "wt",
                        web_tools=("WebFetch",))["permissions"]["allow"]
    assert allow.count("WebFetch") == 1


def test_settings_json_threads_web_tools(tmp_path):
    rendered = json.loads(RS.settings_json(
        home=tmp_path, worktree=tmp_path / "wt", web_tools=("WebFetch",)))
    allow = rendered["permissions"]["allow"]
    assert "WebFetch" in allow
    assert "WebSearch" not in allow
