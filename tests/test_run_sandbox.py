"""Unit tests for lib/agent-platform/run_sandbox.py: the sandbox settings every
dispatched run is launched under (Option A, Slate a-ow0q4o).

These check the settings a run receives. `tests/live/test_dispatched_run_confinement.py`
checks that Claude Code enforces them against a real run.
"""

import fnmatch
import os
import pwd
import shutil
import stat
import subprocess
import tempfile
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
    fs = RS.settings(home=tmp_path, worktree=tmp_path / "wt")["sandbox"]["filesystem"]
    assert "allowRead" not in fs
    assert str(tmp_path / ".orient-control") in fs["denyRead"]


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


def test_a_vivy_dispatch_gets_the_agent_postgres_socket_and_still_denies_local_binding(tmp_path):
    # Round-1 revise on PR #817: a vivy coder's test suite needs
    # the AGENT cluster's socket, never the operator's own cluster's. `allowLocalBinding` must
    # stay absent — opening it would open every local TCP port, the loopback Library
    # included — so only the named socket path is granted.
    net = RS.settings(home=tmp_path, worktree=tmp_path / "wt", repo="vivy")["sandbox"]["network"]
    assert net["allowUnixSockets"] == [RS.agent_postgres_socket_path(tmp_path)]
    assert "allowLocalBinding" not in net


def test_the_agent_postgres_socket_sits_under_the_account_home_not_tmp(tmp_path):
    # Round 1's reviewers both flagged that nothing shows whether Seatbelt's
    # `(remote unix-socket (subpath ...))` resolves the /tmp -> /private/tmp
    # symlink the same way the filesystem predicates do (confirmed only for
    # filesystem rules — see SCRATCH_ROOTS's comment). The fix is to never put
    # this socket under /tmp at all: it sits under the account home instead,
    # where neither this nor any ancestor component is a symlink on this
    # machine (checked by hand; see the comment above POSTGRES_SOCKET_REPOS).
    # Checked against the relative part only (not `tmp_path` itself, which in
    # this sandbox sits under /tmp for unrelated reasons and would make a
    # whole-string check pass or fail on the test fixture, not on this code).
    reldir = str(RS.AGENT_POSTGRES_RELDIR)
    assert "tmp" not in reldir.split("/")
    assert RS.agent_postgres_socket_dir(tmp_path) == tmp_path / RS.AGENT_POSTGRES_RELDIR / "sockets"


def test_a_dispatch_into_any_other_repo_is_byte_for_byte_unchanged(tmp_path):
    baseline = RS.settings_json(home=tmp_path, worktree=tmp_path / "wt")
    assert RS.settings_json(home=tmp_path, worktree=tmp_path / "wt", repo=None) == baseline
    for repo in ("constance", "agent-platform", "CFO", "sous-chef"):
        assert RS.settings_json(home=tmp_path, worktree=tmp_path / "wt", repo=repo) == baseline


def test_network_allowed_is_unchanged_for_every_repo(tmp_path):
    for repo in (None, "vivy", "constance"):
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
    which has no `allowRead` key and no eval entry in `denyRead` at all."""
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
        assert fs["allowRead"] == [str(own)]
        assert str(sibling) not in fs["allowRead"]
    finally:
        shutil.rmtree(own, ignore_errors=True)
        shutil.rmtree(sibling, ignore_errors=True)


def test_two_real_eval_dispatches_settings_cross_deny_each_others_read(
        tmp_path, monkeypatch):
    """The real `settings()` output for two separate real eval dispatches (not a
    hand-built settings dict): each one's `denyRead` glob covers the OTHER's real
    directory, and each one's `allowRead` names only its own."""
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
        assert fs_a["allowRead"] == [str(dir_a)]
        assert fs_b["allowRead"] == [str(dir_b)]
        assert str(dir_b) not in fs_a["allowRead"]
        assert str(dir_a) not in fs_b["allowRead"]
    finally:
        shutil.rmtree(dir_a, ignore_errors=True)
        shutil.rmtree(dir_b, ignore_errors=True)


def test_a_non_eval_dispatchs_settings_have_no_eval_read_rules(tmp_path):
    """Adding the read-side narrowing must not perturb an ordinary dispatch: no
    `allowRead` key at all, and no eval-prefixed glob in `denyRead` — matching the
    write side's own "byte for byte unchanged" contract
    (test_a_non_eval_dispatchs_settings_are_unchanged_by_eval_scratch_dir_support)."""
    settings = RS.settings(home=tmp_path, worktree=tmp_path / "wt", eval_scratch_dir=None)
    fs = settings["sandbox"]["filesystem"]
    assert "allowRead" not in fs
    assert not any(RS.EVAL_SCRATCH_PREFIX in p for p in fs["denyRead"])
    assert not any(RS.EVAL_SCRATCH_PREFIX in p for p in settings["permissions"]["deny"])


def test_an_eval_dispatchs_permissions_deny_read_covers_every_temp_root_spelling(tmp_path):
    """Round 10 (agent-platform#813 round 9 finding, security-reviewer): round 9's
    `denyRead`/`allowRead` pair binds `sandbox.filesystem`, which the installed
    Claude Code binary's own strings confine to sandboxed commands (Bash); Read,
    Glob and Grep are governed by `permissions.deny`'s `Read(...)` rule instead, and
    round 9 never added one. `permissions.deny` must carry a `Read(...)` rule for
    every temp root spelling `eval_deny_read` already covers on the sandbox side —
    not just whichever one the platform running this test happens to resolve to.
    Fails against a4c1d44a, which has no eval `Read(...)` rule in `permissions.deny`
    at all."""
    scratch_dir = tmp_path / "scratch" / f"{RS.EVAL_SCRATCH_PREFIX}own"
    settings = RS.settings(home=tmp_path, worktree=tmp_path / "wt", eval_scratch_dir=scratch_dir)
    deny = settings["permissions"]["deny"]
    for root in (os.path.realpath(tempfile.gettempdir()), "/tmp", os.path.realpath("/tmp")):
        rule = f"Read({_rule(Path(root))}/{RS.EVAL_SCRATCH_PREFIX}*/**)"
        assert rule in deny, f"missing eval Read(...) deny rule for temp root {root}: {deny}"


def test_two_real_eval_dispatches_permissions_deny_read_of_each_others_directory(
        tmp_path, monkeypatch):
    """The real `settings()` output for two separate real eval dispatches (not a
    hand-built settings dict): each one's `permissions.deny` carries a `Read(...)`
    rule matching the OTHER's real directory. Deny wins over allow in the
    permissions layer, so the same rule also covers this dispatch's own directory —
    unlike the sandbox-layer `allowRead`, nothing here carves it back out (see
    `settings`'s own docstring for why that is acceptable). Fails against a4c1d44a."""
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
        # Deny wins over allow, so the same rule also covers each dispatch's own directory.
        assert _covering(deny_a, dir_a) == covering_b_in_a
        assert _covering(deny_b, dir_b) == covering_a_in_b
    finally:
        shutil.rmtree(dir_a, ignore_errors=True)
        shutil.rmtree(dir_b, ignore_errors=True)


def test_an_eval_dispatchs_write_allow_rule_still_stands_under_the_new_read_deny(tmp_path):
    """The round-10 `Read(...)` deny rule must not displace the `Edit(...)` allow
    rule `scratch_allow_roots` already grants this dispatch's own directory — `Write`
    is governed by `Edit(path)`, never a `Read(...)` rule (this function's own
    docstring), so the two must coexist."""
    scratch_dir = tmp_path / "scratch" / f"{RS.EVAL_SCRATCH_PREFIX}own"
    settings = RS.settings(home=tmp_path, worktree=tmp_path / "wt", eval_scratch_dir=scratch_dir)
    assert f"Edit({_rule(scratch_dir)}/**)" in settings["permissions"]["allow"]
    assert str(scratch_dir) in settings["sandbox"]["filesystem"]["allowWrite"]


def test_eval_verdict_path_is_a_fixed_filename_inside_the_scratch_dir(tmp_path):
    d = tmp_path / "scratch-dir"
    assert RS.eval_verdict_path(d) == d / RS.EVAL_VERDICT_FILENAME


def test_without_a_runs_root_no_runs_rule_is_added(tmp_path):
    fs = RS.settings(home=tmp_path, worktree=tmp_path / "wt")["sandbox"]["filesystem"]
    assert not any("agent-runs" in p for p in fs["denyWrite"])


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


def test_git_common_dir_is_the_common_git_dir_of_a_linked_worktree(tmp_path):
    primary = tmp_path / "primary"
    wt = _linked_checkout(tmp_path)

    # Checked against git itself, so a wrong reading of the worktree's files fails here.
    assert RS.git_common_dir(wt) == _git_path(wt, "--git-common-dir")
    assert RS.git_common_dir(primary) == (primary / ".git").resolve()

    common = RS.git_common_dir(wt)
    fs = RS.settings(home=tmp_path, worktree=wt, git_dir=common)["sandbox"]["filesystem"]
    assert str(common / "hooks") in fs["denyWrite"]
    assert str(common / "config") in fs["denyWrite"]


def test_a_linked_worktree_writes_only_the_subpaths_a_commit_needs(tmp_path):
    # knowledge#793 condition 1 / round 2. The subpaths are what a commit and a push
    # write for THIS worktree's own branch, found by observing one
    # (tests/live/test_dispatched_run_confinement.py runs it under the sandbox).
    #
    # Round 6: `objects` is no longer in this grant at all — the run's own git writes new
    # objects to its private store instead (see RS.private_objects_dir's docstring).
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


def test_main_and_packed_refs_are_explicitly_denied(tmp_path):
    # Round 2 (reviewer + security-reviewer blocking finding), discovered live (see
    # _refs_to_protect's docstring): a path under the shared git directory that is
    # absent from BOTH allowWrite and denyWrite is still writable, unlike an ordinary
    # path elsewhere. Only an explicit denyWrite entry refuses it, so main's ref (and
    # packed-refs) must be named here, not merely left out of allowWrite.
    wt = _linked_checkout(tmp_path)
    common = RS.git_common_dir(wt)
    fs = RS.settings(home=tmp_path, worktree=wt, git_dir=common)["sandbox"]["filesystem"]
    assert str(common / "refs" / "heads" / "main") in fs["denyWrite"]
    assert str(common / "packed-refs") in fs["denyWrite"]
    assert str(common / "packed-refs.lock") in fs["denyWrite"]
    # The run's own branch is not denied.
    assert str(common / "refs" / "heads" / "b") not in fs["denyWrite"]


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


def test_every_file_under_the_shared_git_dir_is_either_this_runs_write_or_denied(tmp_path):
    """The property round 2 claimed and did not have, pinned directly rather than through
    one example at a time: enumerate every file that exists under a realistic shared git
    directory — a sibling worktree, tags, notes, a second remote, packed-refs, and the
    primary's own HEAD — and check each one against the settings `settings()` actually
    builds, not against a hand-written list of paths to check (the shape round 2 had).

    Kept under the real HOME-rooted `tmp_path` fixture (pytest's own `tmp_path` sits under
    `$TMPDIR`, same caution as the live check's docstring) is unnecessary here: this test
    checks the SETTINGS DATA this module builds, not live sandbox enforcement, so it carries
    none of the `$TMPDIR`-is-writable confound the live check has to avoid."""
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


def test_a_run_cannot_write_the_git_files_that_repoint_git(tmp_path):
    # knowledge#793 condition 1: a run that rewrote one of these could make git run a command
    # the run wrote, with core.fsmonitor or core.hooksPath, on the next git command in the worktree.
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
    assert "allowRead" not in fs


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


def test_the_credential_helper_paths_are_denied_for_every_home(tmp_path, monkeypatch):
    # Same "caller's own HOME too" shape as test_the_denial_covers_the_callers_home_too.
    caller_home = tmp_path / "caller-home"
    monkeypatch.setenv("HOME", str(caller_home))
    settings = RS.settings(home=tmp_path / "passwd-home", worktree=tmp_path / "wt")
    deny_read = settings["sandbox"]["filesystem"]["denyRead"]
    for home in (tmp_path / "passwd-home", caller_home):
        assert str(home / ".config" / "gh") in deny_read
        assert str(home / "Library" / "Keychains") in deny_read
        assert str(home / ".gitconfig") in deny_read
        assert str(home / ".config" / "convivy" / "bosun-app.pem") in deny_read
        assert str(home / ".config" / "convivy" / "bosun-ledger-reporter.token") in deny_read


def test_the_extra_deny_home_also_covers_the_credential_helper_paths(tmp_path, monkeypatch):
    extra = tmp_path / "standins"
    passwd = tmp_path / "passwd"
    monkeypatch.setenv(RS.EXTRA_DENY_HOME_ENV, str(extra))
    deny_read = RS.settings(home=passwd, worktree=tmp_path / "wt")["sandbox"]["filesystem"]["denyRead"]
    assert str(extra / ".config" / "gh") in deny_read
    assert str(extra / ".gitconfig") in deny_read
    assert str(extra / ".config" / "convivy" / "bosun-app.pem") in deny_read
    assert str(extra / ".config" / "convivy" / "bosun-ledger-reporter.token") in deny_read


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
    allow = RS.settings(home=tmp_path, worktree=tmp_path / "wt")["permissions"]["allow"]
    for tool in ("Bash", "Read", "Grep", "Glob", "WebFetch", "Agent", "ToolSearch",
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
