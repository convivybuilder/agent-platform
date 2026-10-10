"""Starting and stopping the tmux sessions Deck attaches to.

THE TESTS THAT MATTER MOST are the ones about refusing: a command is typed into
a live shell, so a config row that could carry a newline would become two
commands, and `stop --kill` destroys a session's scrollback and anything else
running in it.
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys
import types
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parent.parent / "lib" / "agent-platform"


def _load(name, *, register=False):
    """Load a module from THIS checkout.

    `register` puts it in `sys.modules` first, and that is load-bearing rather
    than tidy. Every module here opens with
    `sys.path.insert(0, ~/.local/lib/agent-platform)`, whose files symlink to the
    PRIMARY checkout — so a module loaded from this worktree would import the
    installed `tmux_transcript`, not the one beside it. A test would then exercise
    main's code while appearing to test the branch's, which is the worst way to be
    green. Registering the dependency first makes the import find this copy.
    """
    spec = importlib.util.spec_from_file_location(name, LIB / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    if register:
        sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def SL(monkeypatch, tmp_path):
    # Dependencies first, so session_lifecycle binds to this checkout's copies,
    # builder_root included: without registering it, `import builder_root`
    # resolved only because session_input happened to put its own directory
    # on sys.path first (review, PR 529).
    _load("tmux_transcript", register=True)
    _load("session_input", register=True)
    _load("claude_trust", register=True)
    _load("builder_root", register=True)
    module = _load("session_lifecycle")
    # THE MACHINE'S OWN CONFIG MUST NOT REACH THESE TESTS. CREWS_ROOT_CONFIG
    # defaults to the real ~/.config/convivy/session-agent.env, so on a box that
    # pins CREWS_ROOT there (the mini, since 2026-09-11) every test patching only
    # CONVIVY_ROOT resolved the real root and refused its tmp_path target: six
    # failures on the mini and none in CI, which has no such file. A test about
    # the config sets its own with `_write_crews_root_config`.
    monkeypatch.setattr(module, "CREWS_ROOT_CONFIG", tmp_path / "no-session-agent.env")
    monkeypatch.delitem(sys.modules, "tmux_transcript", raising=False)
    monkeypatch.delitem(sys.modules, "session_input", raising=False)
    monkeypatch.delitem(sys.modules, "claude_trust", raising=False)
    monkeypatch.delitem(sys.modules, "builder_root", raising=False)
    # No test reaches the machine's tmux server or keeper job by default. The
    # real helper is kept under a private name, and only the stub-PATH tests
    # below restore it.
    module._real_ensure_default_server = module.ensure_default_server
    monkeypatch.setattr(module, "ensure_default_server", lambda wait=None, poll=None: None)
    return module


@pytest.fixture
def tmux(SL, monkeypatch):
    """Record every tmux invocation instead of running it, and pretend no
    session exists unless a test says otherwise.

    `claude_pane` is stubbed alongside `run` rather than left alone, because
    `tmux_transcript` binds `_run = run` at import: patching `run` does not
    reach `panes()`, so an unstubbed `claude_pane` would ask the REAL tmux on
    the machine running the tests and answer from whatever sessions it found.

    `claude_trust.pre_trust_created_directory` is stubbed here too, for the
    same reason and by the same necessity: it is the one dependency of
    `start` that touches a REAL file (`~/.claude.json`) rather than a
    subprocess, and every `create_dir=True` test in this file would otherwise
    read and write the developer's actual file the moment it ran. The stub
    never touches disk; it only records `(home, directory)` onto
    `calls.trust_calls`, which is what the pre-trust-specific tests below
    assert against instead of the real function's file effects — those live
    in `tests/test_claude_trust.py`.
    """
    class Calls(list):
        """A plain list cannot carry the fixture's own state — no __dict__."""
        existing: set
        with_agent: set
        panes: dict
        trust_calls: list

    calls = Calls()
    calls.existing = set()
    # Sessions whose pane holds a live agent. `start` refuses these; everything
    # else that exists is a bare session it may adopt.
    calls.with_agent = set()
    # `list-panes` output per session, as tmux would emit it.
    calls.panes = {}
    # Every (home, directory) `start` handed to claude_trust.
    calls.trust_calls = []
    existing = calls.existing

    def fake_run(argv, stdin=None):
        calls.append(tuple(argv))
        if argv[:2] == ["tmux", "has-session"]:
            name = argv[-1].lstrip("=")
            return "" if name in existing else None
        if argv[:2] == ["tmux", "list-panes"]:
            name = argv[3].lstrip("=")
            return calls.panes.get(name, "")
        return ""

    def fake_claude_pane(name):
        if name in calls.with_agent:
            return ("%0", 1234)
        raise SL.TT.JoinError(f"no claude process found in any pane of {name!r}")

    def fake_pre_trust(home, directory):
        calls.trust_calls.append((home, directory))
        return {"trusted": True, "reason": None}

    # Where in `calls` each ensure fell, so an ordering test can compare it with
    # the tmux calls. Kept out of `calls` itself: existing tests assert on that
    # list's contents, and "nothing touched tmux" is `not calls`.
    calls.ensure_positions = []

    def fake_ensure(wait=None, poll=None):
        calls.ensure_positions.append(len(calls))

    monkeypatch.setattr(SL.TT, "run", fake_run)
    monkeypatch.setattr(SL.TT, "claude_pane", fake_claude_pane)
    monkeypatch.setattr(SL.CTR, "pre_trust_created_directory", fake_pre_trust)
    # The real helper would run `tmux` and `launchctl` from PATH, which is the
    # machine's own server and keeper job.
    monkeypatch.setattr(SL, "ensure_default_server", fake_ensure)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    return calls


def _verbs(calls):
    return [c[1] for c in calls if c[0] == "tmux"]


# MARK: - Starting


def test_a_session_is_created_detached_in_its_directory(SL, tmux, tmp_path):
    result = SL.start("nyh", directory=str(tmp_path), command="co")

    assert ("tmux", "new-session", "-d", "-s", "nyh", "-c", str(tmp_path)) in tmux
    assert result["created"] is True
    assert result["directory"] == str(tmp_path)


def test_detached_is_not_optional(SL, tmux, tmp_path):
    """Deck attaches over its own ssh connection. A session created attached
    would tie its life to a call that is meant to return."""
    SL.start("nyh", directory=str(tmp_path))
    create = next(c for c in tmux if c[1] == "new-session")
    assert "-d" in create


def test_the_command_is_typed_rather_than_being_the_pane_process(SL, tmux, tmp_path):
    """A command given to `new-session` BECOMES the pane's process, so the
    session dies when it exits — an agent that finished would take its own
    terminal away and leave nowhere to start another."""
    SL.start("nyh", directory=str(tmp_path), command="co -r")

    create = next(c for c in tmux if c[1] == "new-session")
    assert "co -r" not in create
    assert ("tmux", "send-keys", "-l", "-t", "nyh", "co -r") in tmux
    assert ("tmux", "send-keys", "-t", "nyh", "Enter") in tmux


def test_the_default_command_is_co(SL, tmux, tmp_path):
    SL.start("nyh", directory=str(tmp_path))
    assert ("tmux", "send-keys", "-l", "-t", "nyh", "co") in tmux


def test_a_home_relative_directory_is_expanded(SL, tmux, monkeypatch, tmp_path):
    """`~` reaches tmux through argv rather than a shell, so unexpanded it would
    create a directory named `~` beside the repo."""
    # HOME rather than Path.home: `expanduser` reads the environment, so
    # patching Path.home alone leaves it expanding to the real home.
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "work").mkdir()

    result = SL.start("nyh", directory="~/work")
    assert result["directory"] == str(tmp_path / "work")
    assert "~" not in result["directory"]


def test_a_missing_directory_is_refused_before_tmux_is_touched(SL, tmux):
    """`tmux new-session -c` on a missing path fails with a message about the
    session, and the reader goes looking at the wrong thing."""
    with pytest.raises(SL.LifecycleError, match="no such directory"):
        SL.start("nyh", directory="/no/such/place")
    assert not tmux


def test_a_relative_directory_is_refused(SL, tmux):
    with pytest.raises(SL.LifecycleError, match="must be absolute"):
        SL.start("nyh", directory="relative/path")


def test_create_dir_makes_a_missing_directory_and_proceeds(SL, tmux, tmp_path, monkeypatch):
    """ONLY Deck's New Project flow passes this. The directory is created
    (parents included) and the session starts in it, same as if it had already
    existed."""
    monkeypatch.setattr(SL, "CONVIVY_ROOT", tmp_path)
    target = tmp_path / "brand-new" / "project"
    assert not target.exists()

    result = SL.start("nyh", directory=str(target), create_dir=True)

    assert target.is_dir()
    assert result["directory"] == str(target)
    assert ("tmux", "new-session", "-d", "-s", "nyh", "-c", str(target)) in tmux


def test_without_create_dir_a_missing_directory_still_raises_the_same_message(
        SL, tmux, tmp_path):
    """The default stays exactly as it was: a typo in a catalog row's
    workingDirectory must keep failing loudly rather than silently
    manufacturing a directory."""
    target = tmp_path / "does-not-exist"
    with pytest.raises(SL.LifecycleError, match=f"no such directory: {target}"):
        SL.start("nyh", directory=str(target))
    assert not target.exists()
    assert not tmux


def test_create_dir_rejects_a_path_outside_the_root(SL, tmux, tmp_path, monkeypatch):
    """No caller-side convention confines `--create-dir` to Deck's New Project
    flow — this is the server-side floor. A path outside CONVIVY_ROOT must be
    refused, and nothing created."""
    monkeypatch.setattr(SL, "CONVIVY_ROOT", tmp_path / "Convivy")
    target = tmp_path / "elsewhere" / "project"

    with pytest.raises(SL.LifecycleError, match="must be under"):
        SL.start("nyh", directory=str(target), create_dir=True)
    assert not target.exists()
    assert not tmux


def test_create_dir_rejects_a_dotdot_escape(SL, tmux, tmp_path, monkeypatch):
    """The root check runs AFTER resolving `..` — a path that only looks
    contained (climbs back out via `../..`) must still be caught."""
    root = tmp_path / "Convivy"
    root.mkdir()
    monkeypatch.setattr(SL, "CONVIVY_ROOT", root)
    target = root / "sub" / ".." / ".." / "escaped" / "project"
    escaped = tmp_path / "escaped" / "project"

    with pytest.raises(SL.LifecycleError, match="must be under"):
        SL.start("nyh", directory=str(target), create_dir=True)
    assert not escaped.exists()
    assert not tmux


def test_create_dir_rejects_a_symlink_escape(SL, tmux, tmp_path, monkeypatch):
    """The root check runs AFTER resolving symlinks — an existing parent that
    only appears to sit under the root, because it is a symlink pointing
    elsewhere, must still be caught."""
    root = tmp_path / "Convivy"
    root.mkdir()
    monkeypatch.setattr(SL, "CONVIVY_ROOT", root)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "linked"
    link.symlink_to(outside)
    target = link / "project"

    with pytest.raises(SL.LifecycleError, match="must be under"):
        SL.start("nyh", directory=str(target), create_dir=True)
    assert not (outside / "project").exists()
    assert not tmux


def test_a_racing_creator_wins_the_mkdir_and_the_directory_is_adopted_not_trusted(
        SL, tmux, tmp_path, monkeypatch):
    """Between `_validated_directory`'s `is_dir()` check and this call's own
    `mkdir`, another local process can win the race and create the target
    first — with content of its own choosing. `mkdir(..., exist_ok=False)`
    turns that race into a `FileExistsError` this treats as 'not ours': the
    call still succeeds (the directory exists, which is all `start` asked
    for) but `dir_created` is `False`, so it is never offered to
    `claude_trust` — exactly the 'adopted, not created' case the module's
    docstring says must keep prompting."""
    monkeypatch.setattr(SL, "CONVIVY_ROOT", tmp_path)
    target = tmp_path / "brand-new" / "project"

    real_mkdir = SL.Path.mkdir
    raced = {}

    def racing_mkdir(self, mode=0o777, parents=False, exist_ok=False):
        # First call: simulate another process creating the directory before
        # this call's own mkdir runs, then behave exactly as the real mkdir
        # would against a directory that already exists.
        if str(self) not in raced:
            real_mkdir(self, mode=mode, parents=parents, exist_ok=True)
            raced[str(self)] = True
        if not exist_ok:
            raise FileExistsError(f"[Errno 17] File exists: '{self}'")

    monkeypatch.setattr(SL.Path, "mkdir", racing_mkdir)

    result = SL.start("nyh", directory=str(target), create_dir=True)

    assert target.is_dir()
    assert result["ok"] is True
    assert tmux.trust_calls == []
    assert result["directory_trusted"] is None
    assert result["directory_trust_reason"] is None


def test_create_dir_at_a_path_occupied_by_a_regular_file_raises_loudly(
        SL, tmux, tmp_path, monkeypatch):
    """`Path.mkdir` raises `FileExistsError` not only when another process
    wins the race with a real directory, but also when a regular file (or a
    symlink resolving to one) already sits at the target — under
    `exist_ok=True` and `exist_ok=False` alike. Treating the exception alone
    as proof of the racing-directory case would hand `start` a path that is
    not a directory at all, and tmux would be asked to use it as one. This
    must raise the same loud failure a plain file at this path always
    deserved, not silently adopt it."""
    monkeypatch.setattr(SL, "CONVIVY_ROOT", tmp_path)
    target = tmp_path / "brand-new" / "project"
    target.parent.mkdir()
    target.write_text("not a directory")

    with pytest.raises(SL.LifecycleError, match="not a directory"):
        SL.start("nyh", directory=str(target), create_dir=True)
    assert not tmux


# MARK: - The per-machine creatable root (CREWS_ROOT_CONFIG)


def _write_crews_root_config(SL, monkeypatch, tmp_path, content):
    config = tmp_path / "session-agent.env"
    config.write_text(content)
    monkeypatch.setattr(SL, "CREWS_ROOT_CONFIG", config)
    return config


def test_no_config_file_the_floor_is_convivy_root_byte_identical(
        SL, tmux, tmp_path, monkeypatch):
    """No config set anywhere: behaviour matches today's exactly."""
    monkeypatch.setattr(SL, "CREWS_ROOT_CONFIG", tmp_path / "no-such-file.env")
    monkeypatch.setattr(SL, "CONVIVY_ROOT", tmp_path / "Convivy")
    target = tmp_path / "Convivy" / "brand-new"

    result = SL.start("nyh", directory=str(target), create_dir=True)

    assert target.is_dir()
    assert result["directory"] == str(target)


def test_a_configured_root_permits_a_directory_under_it(SL, tmux, tmp_path, monkeypatch):
    root = tmp_path / "elsewhere"
    root.mkdir()
    _write_crews_root_config(SL, monkeypatch, tmp_path, f"CREWS_ROOT={root}\n")
    target = root / "brand-new" / "crew"

    result = SL.start("nyh", directory=str(target), create_dir=True)

    assert target.is_dir()
    assert result["directory"] == str(target)


def test_a_configured_root_refuses_a_directory_outside_it(SL, tmux, tmp_path, monkeypatch):
    root = tmp_path / "elsewhere"
    root.mkdir()
    _write_crews_root_config(SL, monkeypatch, tmp_path, f"CREWS_ROOT={root}\n")
    target = tmp_path / "not-under-root" / "crew"

    with pytest.raises(SL.LifecycleError, match="must be under"):
        SL.start("nyh", directory=str(target), create_dir=True)
    assert not target.exists()
    assert not tmux


def test_the_refusal_names_the_configured_root(SL, tmux, tmp_path, monkeypatch):
    """An operator who set a root sees the value actually in force, not the
    `~/Convivy` constant they would otherwise go looking for in the source."""
    root = tmp_path / "wherever-the-operator-pointed-it"
    root.mkdir()
    _write_crews_root_config(SL, monkeypatch, tmp_path, f"CREWS_ROOT={root}\n")
    target = tmp_path / "elsewhere" / "crew"

    with pytest.raises(SL.LifecycleError, match=re.escape(str(root))):
        SL.start("nyh", directory=str(target), create_dir=True)


# MARK: - `creatable_root()`, the public read the `crews-root` verb uses


def test_creatable_root_reports_the_configured_value(SL, tmp_path, monkeypatch):
    root = tmp_path / "wherever-the-operator-pointed-it"
    root.mkdir()
    _write_crews_root_config(SL, monkeypatch, tmp_path, f"CREWS_ROOT={root}\n")

    assert SL.creatable_root() == str(root)


def test_creatable_root_falls_back_to_convivy_root_when_unconfigured(
        SL, tmp_path, monkeypatch):
    monkeypatch.setattr(SL, "CREWS_ROOT_CONFIG", tmp_path / "no-such-file.env")
    monkeypatch.setattr(SL, "CONVIVY_ROOT", tmp_path / "Convivy")

    assert SL.creatable_root() == str(tmp_path / "Convivy")


def test_creatable_root_matches_what_the_refusal_message_would_name(
        SL, tmux, tmp_path, monkeypatch):
    """A client reporting one string and a refusal naming a different one
    for the same configuration would be worse than no verb at all — this is
    the guarantee the docstring promises, exercised end to end rather than
    just asserted against the private helper."""
    root = tmp_path / "wherever-the-operator-pointed-it"
    root.mkdir()
    _write_crews_root_config(SL, monkeypatch, tmp_path, f"CREWS_ROOT={root}\n")
    target = tmp_path / "elsewhere" / "crew"

    with pytest.raises(SL.LifecycleError) as excinfo:
        SL.start("nyh", directory=str(target), create_dir=True)

    assert SL.creatable_root() in str(excinfo.value)


def test_a_configured_root_still_rejects_a_dotdot_escape(SL, tmux, tmp_path, monkeypatch):
    """The property most likely to be lost in a rewrite: the escape checks
    must run against the CONFIGURED root, not just the default one.
    CONVIVY_ROOT is set to `tmp_path` itself, which DOES contain the escaped
    target — so a version that checked the default root instead of the
    configured one would wrongly accept this, and this test is what would
    catch it."""
    monkeypatch.setattr(SL, "CONVIVY_ROOT", tmp_path)
    root = tmp_path / "elsewhere"
    root.mkdir()
    _write_crews_root_config(SL, monkeypatch, tmp_path, f"CREWS_ROOT={root}\n")
    target = root / "sub" / ".." / ".." / "escaped" / "crew"
    escaped = tmp_path / "escaped" / "crew"

    with pytest.raises(SL.LifecycleError, match="must be under"):
        SL.start("nyh", directory=str(target), create_dir=True)
    assert not escaped.exists()
    assert not tmux


def test_a_configured_root_still_rejects_a_symlink_escape(SL, tmux, tmp_path, monkeypatch):
    """Same reasoning as the `..` escape above: CONVIVY_ROOT is set to
    `tmp_path`, which DOES contain the symlink's target, so this only stays
    green if the check runs against the configured root."""
    monkeypatch.setattr(SL, "CONVIVY_ROOT", tmp_path)
    root = tmp_path / "elsewhere"
    root.mkdir()
    _write_crews_root_config(SL, monkeypatch, tmp_path, f"CREWS_ROOT={root}\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "linked"
    link.symlink_to(outside)
    target = link / "crew"

    with pytest.raises(SL.LifecycleError, match="must be under"):
        SL.start("nyh", directory=str(target), create_dir=True)
    assert not (outside / "crew").exists()
    assert not tmux


def test_an_existing_directory_outside_the_configured_root_is_still_adopted(
        SL, tmux, tmp_path, monkeypatch):
    """Adoption of a folder anywhere keeps working: the floor applies only on
    the creating path."""
    root = tmp_path / "elsewhere"
    root.mkdir()
    _write_crews_root_config(SL, monkeypatch, tmp_path, f"CREWS_ROOT={root}\n")
    existing = tmp_path / "some-old-folder"
    existing.mkdir()

    result = SL.start("nyh", directory=str(existing), create_dir=True)

    assert result["directory"] == str(existing)


@pytest.mark.parametrize("bad_value", ["relative/path", ""])
def test_a_nonsense_configured_root_falls_back_to_convivy_root(
        SL, tmux, tmp_path, monkeypatch, bad_value):
    monkeypatch.setattr(SL, "CONVIVY_ROOT", tmp_path / "Convivy")
    _write_crews_root_config(SL, monkeypatch, tmp_path, f"CREWS_ROOT={bad_value}\n")
    target = tmp_path / "Convivy" / "brand-new"

    result = SL.start("nyh", directory=str(target), create_dir=True)
    assert target.is_dir()
    assert result["directory"] == str(target)

    # And it still refuses what the default would refuse.
    outside = tmp_path / "elsewhere" / "crew"
    with pytest.raises(SL.LifecycleError, match="must be under"):
        SL.start("nyh2", directory=str(outside), create_dir=True)


def test_a_root_of_the_filesystem_root_falls_back_to_convivy_root(
        SL, tmux, tmp_path, monkeypatch):
    """`CREWS_ROOT=/` is well-formed, absolute, and an existing directory —
    every earlier gate passes it — but as a floor it contains everything, so
    it must fall back the same as an unparseable value.

    Asserted on a target OUTSIDE CONVIVY_ROOT, not a create inside it: with a
    root of `/`, every absolute path is `relative_to("/")`, so a create
    inside the fallback root succeeds whether or not the fallback actually
    fired. Only a refusal that names CONVIVY_ROOT, not `/`, proves the
    fallback ran rather than `/` being honored as the root."""
    convivy_root = tmp_path / "Convivy"
    monkeypatch.setattr(SL, "CONVIVY_ROOT", convivy_root)
    _write_crews_root_config(SL, monkeypatch, tmp_path, "CREWS_ROOT=/\n")
    target = tmp_path / "not-under-convivy" / "crew"

    with pytest.raises(SL.LifecycleError,
                        match=f"must be under {re.escape(str(convivy_root))}"):
        SL.start("nyh", directory=str(target), create_dir=True)
    assert not target.exists()
    assert not tmux


def test_a_root_of_home_falls_back_to_convivy_root(SL, tmux, tmp_path, monkeypatch):
    """`CREWS_ROOT=~` resolves to the whole home directory — a floor wide
    enough to still reach `~/.ssh`.

    Asserted the same way as the `/` case, on a target outside CONVIVY_ROOT
    with the refusal naming CONVIVY_ROOT: a create INSIDE the fallback root
    would pass here for the wrong reason on macOS, where pytest's `tmp_path`
    happens not to sit under `$HOME`, so it would look contained under
    `~` too, without the fallback having fired. Naming CONVIVY_ROOT in the
    refusal is the part no incidental platform layout can fake."""
    convivy_root = tmp_path / "Convivy"
    monkeypatch.setattr(SL, "CONVIVY_ROOT", convivy_root)
    _write_crews_root_config(SL, monkeypatch, tmp_path, "CREWS_ROOT=~\n")
    target = tmp_path / "not-under-convivy" / "crew"

    with pytest.raises(SL.LifecycleError,
                        match=f"must be under {re.escape(str(convivy_root))}"):
        SL.start("nyh", directory=str(target), create_dir=True)
    assert not target.exists()
    assert not tmux


def test_a_root_of_home_spelled_absolutely_falls_back_to_convivy_root(
        SL, tmux, tmp_path, monkeypatch):
    """Same as `~`, spelled as the absolute path rather than the tilde — the
    check has to key on the resolved path, not the literal spelling."""
    convivy_root = tmp_path / "Convivy"
    monkeypatch.setattr(SL, "CONVIVY_ROOT", convivy_root)
    _write_crews_root_config(
        SL, monkeypatch, tmp_path, f"CREWS_ROOT={Path.home()}\n")
    target = tmp_path / "not-under-convivy" / "crew"

    with pytest.raises(SL.LifecycleError,
                        match=f"must be under {re.escape(str(convivy_root))}"):
        SL.start("nyh", directory=str(target), create_dir=True)
    assert not target.exists()
    assert not tmux


def test_a_root_that_is_an_ancestor_of_home_falls_back_to_convivy_root(
        SL, tmux, tmp_path, monkeypatch):
    """The directory directly containing the account's home (`/Users` on
    this platform) is no narrower a floor than `~` itself, since home sits
    under it."""
    ancestor = Path.home().resolve().parent
    convivy_root = tmp_path / "Convivy"
    monkeypatch.setattr(SL, "CONVIVY_ROOT", convivy_root)
    _write_crews_root_config(SL, monkeypatch, tmp_path, f"CREWS_ROOT={ancestor}\n")
    target = tmp_path / "not-under-convivy" / "crew"

    with pytest.raises(SL.LifecycleError,
                        match=f"must be under {re.escape(str(convivy_root))}"):
        SL.start("nyh", directory=str(target), create_dir=True)
    assert not target.exists()
    assert not tmux


def test_a_symlink_resolving_to_the_filesystem_root_falls_back_to_convivy_root(
        SL, tmux, tmp_path, monkeypatch):
    """A symlinked configured root is judged by where it RESOLVES, not by its
    literal path — a symlink to `/` is exactly as wide a floor as `/` spelled
    directly."""
    convivy_root = tmp_path / "Convivy"
    monkeypatch.setattr(SL, "CONVIVY_ROOT", convivy_root)
    link = tmp_path / "root-points-at-slash"
    link.symlink_to("/")
    _write_crews_root_config(SL, monkeypatch, tmp_path, f"CREWS_ROOT={link}\n")
    target = tmp_path / "not-under-convivy" / "crew"

    with pytest.raises(SL.LifecycleError,
                        match=f"must be under {re.escape(str(convivy_root))}"):
        SL.start("nyh", directory=str(target), create_dir=True)
    assert not target.exists()
    assert not tmux


def test_a_configured_tilde_root_expands_against_the_passwd_home(
        monkeypatch, tmp_path):
    """`CREWS_ROOT=~` must mean the PASSWD home, not `$HOME`.

    `_real_account_home` closed three environment-influenced inputs to this
    guard, and left one: `_creatable_root` expanded the configured value with
    `Path.expanduser()`, which reads `$HOME` when it is set. So `CREWS_ROOT=~`
    — an ordinary thing for an operator to write, meaning "my home" — resolved
    against whatever `$HOME` the invoking process carried, and that directory
    came back as the creatable root. The ancestor-of-home check cannot catch
    it: that check compares against the correct (passwd) home, while the
    candidate was derived from the wrong one. Security-reviewer found this on
    PR #401 round 3 and it is the last of the four.

    The assertion is the refusal of a target under the SPOOFED home. Under the
    bug that target was inside the (spoofed-home) root and would have been
    created; expanding against passwd makes the root the passwd home, which is
    then refused as too wide, so the spoofed target is unreachable either way.
    """
    passwd_home = tmp_path / "passwd-home"
    passwd_home.mkdir()
    spoofed_home = tmp_path / "spoofed-home"
    spoofed_home.mkdir()

    import pwd as real_pwd

    fake_entry = types.SimpleNamespace(pw_dir=str(passwd_home))
    monkeypatch.setattr(real_pwd, "getpwuid", lambda uid: fake_entry)
    monkeypatch.setenv("HOME", str(spoofed_home))

    _load("tmux_transcript", register=True)
    _load("session_input", register=True)
    _load("claude_trust", register=True)
    _load("builder_root", register=True)
    module = _load("session_lifecycle")
    monkeypatch.delitem(sys.modules, "tmux_transcript", raising=False)
    monkeypatch.delitem(sys.modules, "session_input", raising=False)
    monkeypatch.delitem(sys.modules, "claude_trust", raising=False)
    monkeypatch.delitem(sys.modules, "builder_root", raising=False)
    monkeypatch.setattr(module.TT, "run", lambda argv, stdin=None: "")
    monkeypatch.setattr(module, "ensure_default_server", lambda wait=None, poll=None: None)

    config = tmp_path / "session-agent.env"
    config.write_text("CREWS_ROOT=~\n")
    monkeypatch.setattr(module, "CREWS_ROOT_CONFIG", config)

    # `~` resolves to the passwd home, which is refused as too wide, so the
    # floor falls back to CONVIVY_ROOT. Under the bug it resolved to the
    # spoofed home, which is neither the passwd home nor an ancestor of it,
    # so it was accepted and this target would have been created.
    target = spoofed_home / "attacker-named-crew"
    with pytest.raises(module.LifecycleError,
                        match=f"must be under {re.escape(str(passwd_home / 'builder'))}"):
        module.start("nyh", directory=str(target), create_dir=True)
    assert not target.exists()


def test_the_trust_anchor_is_the_passwd_home_not_a_spoofed_home_env(
        monkeypatch, tmp_path):
    """`Path.home()` reads `$HOME` at call time, and CONVIVY_ROOT,
    CREWS_ROOT_CONFIG, and the ancestor-of-home comparison in
    `_creatable_root` were all computed from it. A process started with a
    different `$HOME` moved all three at once — after which `CREWS_ROOT=<the
    real account home>` came back from `_creatable_root` as a literal
    creatable root, not refused as too wide a floor. Security-reviewer
    reproduced this on PR #401 round 3.

    Both "homes" here are synthetic, under `tmp_path`, and reached by
    patching `pwd.getpwuid` directly — this never touches wherever pytest is
    actually running, and never asserts anything about the real developer
    account.
    """
    passwd_home = tmp_path / "passwd-home"
    passwd_home.mkdir()
    spoofed_home = tmp_path / "spoofed-home"
    spoofed_home.mkdir()

    import pwd as real_pwd

    fake_entry = types.SimpleNamespace(pw_dir=str(passwd_home))
    monkeypatch.setattr(real_pwd, "getpwuid", lambda uid: fake_entry)
    monkeypatch.setenv("HOME", str(spoofed_home))

    _load("tmux_transcript", register=True)
    _load("session_input", register=True)
    _load("claude_trust", register=True)
    _load("builder_root", register=True)
    module = _load("session_lifecycle")
    monkeypatch.delitem(sys.modules, "tmux_transcript", raising=False)
    monkeypatch.delitem(sys.modules, "session_input", raising=False)
    monkeypatch.delitem(sys.modules, "claude_trust", raising=False)
    monkeypatch.delitem(sys.modules, "builder_root", raising=False)
    monkeypatch.setattr(module.TT, "run", lambda argv, stdin=None: "")
    monkeypatch.setattr(module, "ensure_default_server", lambda wait=None, poll=None: None)

    # The module-level constants must derive from the passwd home, never the
    # spoofed one.
    assert module.CONVIVY_ROOT == passwd_home / "builder"
    assert (module.CREWS_ROOT_CONFIG
            == passwd_home / ".config" / "convivy" / "session-agent.env")

    # And CREWS_ROOT set to the passwd home itself must still be refused as
    # too wide a floor, exactly as it is for the real home today — this is
    # the reproduction: under the bug, `resolved in home.parents` compared
    # against the SPOOFED home, so the real home passed every check and was
    # returned as a literal creatable root.
    config = tmp_path / "session-agent.env"
    config.write_text(f"CREWS_ROOT={passwd_home}\n")
    monkeypatch.setattr(module, "CREWS_ROOT_CONFIG", config)
    target = passwd_home / "some-new-crew"

    with pytest.raises(module.LifecycleError,
                        match=f"must be under {re.escape(str(passwd_home / 'builder'))}"):
        module.start("nyh", directory=str(target), create_dir=True)
    assert not target.exists()


def test_a_configured_root_pointing_at_a_file_falls_back_to_convivy_root(
        SL, tmux, tmp_path, monkeypatch):
    """A root that 'cannot exist' as a directory — it already exists as a
    plain file — must not be trusted."""
    monkeypatch.setattr(SL, "CONVIVY_ROOT", tmp_path / "Convivy")
    not_a_dir = tmp_path / "im-a-file"
    not_a_dir.write_text("nope")
    _write_crews_root_config(SL, monkeypatch, tmp_path, f"CREWS_ROOT={not_a_dir}\n")
    target = tmp_path / "Convivy" / "brand-new"

    result = SL.start("nyh", directory=str(target), create_dir=True)

    assert target.is_dir()
    assert result["directory"] == str(target)


def test_an_unset_key_in_the_config_file_falls_back_to_convivy_root(
        SL, tmux, tmp_path, monkeypatch):
    """A config file that exists but never sets `CREWS_ROOT` is the same as
    no config file at all."""
    monkeypatch.setattr(SL, "CONVIVY_ROOT", tmp_path / "Convivy")
    _write_crews_root_config(SL, monkeypatch, tmp_path, "SOME_OTHER_KEY=nope\n")
    target = tmp_path / "Convivy" / "brand-new"

    result = SL.start("nyh", directory=str(target), create_dir=True)

    assert target.is_dir()
    assert result["directory"] == str(target)


@pytest.mark.parametrize("create_dir", [True, False])
def test_an_existing_directory_is_unaffected_either_way(SL, tmux, tmp_path, create_dir):
    result = SL.start("nyh", directory=str(tmp_path), create_dir=create_dir)
    assert result["directory"] == str(tmp_path)
    assert ("tmux", "new-session", "-d", "-s", "nyh", "-c", str(tmp_path)) in tmux


def test_starting_a_session_that_already_has_an_agent_is_refused(SL, tmux, tmp_path):
    """Two agents in one pane is not a state anything downstream can read —
    `claude_pane` would return whichever it found first."""
    tmux.existing.add("nyh")
    tmux.with_agent.add("nyh")
    with pytest.raises(SL.LifecycleError, match="already has an agent"):
        SL.start("nyh", directory=str(tmp_path))
    assert "new-session" not in _verbs(tmux)
    assert "send-keys" not in _verbs(tmux)


def test_an_existing_session_with_no_agent_is_adopted(SL, tmux, tmp_path):
    """THE DEAD END THIS CLOSES. A session whose agent exited without the
    session being killed greys on Deck's rail — correctly, it has no agent —
    and its Start button then failed forever with "already exists", which Deck
    swallows as an expected race, so the click reported nothing at all."""
    tmux.existing.add("ask")
    tmux.panes["ask"] = "%7 zsh"

    result = SL.start("ask", directory=str(tmp_path), command="co -r")

    assert "new-session" not in _verbs(tmux)
    assert ("tmux", "send-keys", "-l", "-t", "%7", "co -r") in tmux
    assert ("tmux", "send-keys", "-t", "%7", "Enter") in tmux
    assert result["created"] is False


def test_an_adopted_session_is_typed_at_the_pane_that_was_checked(SL, tmux, tmp_path):
    """The keystrokes must land in the pane whose command was read, not in
    whichever pane tmux considers active — those are different panes the moment
    a session has more than one."""
    tmux.existing.add("ask")
    tmux.panes["ask"] = "%3 vim\n%9 zsh"

    SL.start("ask", directory=str(tmp_path))

    assert ("tmux", "send-keys", "-l", "-t", "%9", "co") in tmux
    assert not any(c[:4] == ("tmux", "send-keys", "-l", "-t") and c[4] == "%3"
                   for c in tmux)


def test_an_adopted_session_reports_no_directory(SL, tmux, tmp_path):
    """It already has one, and the workdir was never applied — reporting the
    requested value would tell the caller a `cd` happened that did not."""
    tmux.existing.add("ask")
    tmux.panes["ask"] = "%7 zsh"

    result = SL.start("ask", directory=str(tmp_path))
    assert result["directory"] is None


def test_a_pane_id_that_is_not_the_tmux_form_is_not_a_send_target(SL, tmux, tmp_path):
    """The id becomes a `send-keys` target, so it is matched in full rather than
    by its leading `%` — the invariant is held here rather than depending on
    tmux's format output remaining what it is."""
    tmux.existing.add("ask")
    tmux.panes["ask"] = "%not-an-id zsh"

    with pytest.raises(SL.LifecycleError, match="shell prompt"):
        SL.start("ask", directory=str(tmp_path))
    assert "send-keys" not in _verbs(tmux)


def test_a_session_whose_pane_is_busy_is_refused(SL, tmux, tmp_path):
    """The command is keystrokes. A pane running vim or a REPL would take them
    into its buffer, which is worse than refusing: it looks like nothing
    happened while the text went somewhere."""
    tmux.existing.add("ask")
    tmux.panes["ask"] = "%3 vim"

    with pytest.raises(SL.LifecycleError, match="shell prompt"):
        SL.start("ask", directory=str(tmp_path))
    assert "send-keys" not in _verbs(tmux)


def test_an_invalid_session_name_never_reaches_tmux(SL, tmux):
    with pytest.raises(SL.TT.JoinError):
        SL.start("nyh; rm -rf /", directory="/tmp")
    assert not tmux


# MARK: - Pre-trusting a directory this call just created, for Claude Code


def test_a_freshly_created_directory_is_offered_to_claude_trust(
        SL, tmux, tmp_path, monkeypatch):
    """The one case this exists for: `--create-dir` on a directory that did
    not exist. `claude_trust.pre_trust_created_directory` must see exactly
    the resolved path the session itself starts in, so a brand-new crew is
    not born stuck on Claude Code's trust prompt."""
    monkeypatch.setattr(SL, "CONVIVY_ROOT", tmp_path)
    target = tmp_path / "brand-new" / "project"

    result = SL.start("nyh", directory=str(target), create_dir=True)

    assert tmux.trust_calls == [(SL._real_account_home(), str(target))]
    assert result["directory_trusted"] is True
    assert result["directory_trust_reason"] is None


def test_an_existing_directory_under_create_dir_is_never_offered(
        SL, tmux, tmp_path):
    """THE SCOPING TEST, mutation-guarding the gate on `dir_created` rather
    than on `create_dir` alone. `--create-dir` against a directory that
    already exists takes `_validated_directory`'s adopt branch, not its
    `mkdir` one (see that function's own docstring) — it was not created by
    this call, the user may never have looked inside it, and it must keep
    prompting. Fails if the pre-trust call is moved outside the
    `dir_created` branch and gated on `create_dir` instead."""
    result = SL.start("nyh", directory=str(tmp_path), create_dir=True)

    assert tmux.trust_calls == []
    assert result["directory_trusted"] is None
    assert result["directory_trust_reason"] is None


def test_without_create_dir_nothing_is_ever_offered(SL, tmux, tmp_path):
    """THE OTHER HALF OF THE SCOPING TEST. The ordinary path — no
    `--create-dir` at all, an existing directory — must never reach
    `claude_trust`. Fails if the pre-trust call is moved outside the
    `dir_created` branch (which is `False` unconditionally when `create_dir`
    is `False`, per `_validated_directory`)."""
    result = SL.start("nyh", directory=str(tmp_path))

    assert tmux.trust_calls == []
    assert result["directory_trusted"] is None
    assert result["directory_trust_reason"] is None


def test_a_pretrust_failure_is_reported_not_raised(SL, tmux, tmp_path, monkeypatch):
    """`start` must still succeed on a `claude_trust` failure — a session
    that starts with a trust prompt is a UX regression, not a reason to fail
    `start` outright."""
    monkeypatch.setattr(SL, "CONVIVY_ROOT", tmp_path)
    monkeypatch.setattr(SL.CTR, "pre_trust_created_directory",
                        lambda home, directory: {"trusted": False,
                                                 "reason": "could not write it"})
    target = tmp_path / "brand-new"

    result = SL.start("nyh", directory=str(target), create_dir=True)

    assert result["ok"] is True
    assert result["directory_trusted"] is False
    assert result["directory_trust_reason"] == "could not write it"
    assert target.is_dir(), "a pre-trust failure must not undo the mkdir"


def test_pretrust_home_is_the_passwd_home_not_home_env(SL, tmux, tmp_path, monkeypatch):
    """Consistency with the rest of this guard (see `_real_account_home`'s own
    docstring): the account home handed to `claude_trust` must be the PASSWD
    home, not `$HOME` — an ssh or launchd invocation carrying a different
    `$HOME` must not silently steer where the trust flag lands."""
    import pwd as real_pwd

    passwd_home = tmp_path / "passwd-home"
    passwd_home.mkdir()
    fake_entry = types.SimpleNamespace(pw_dir=str(passwd_home))
    monkeypatch.setattr(real_pwd, "getpwuid", lambda uid: fake_entry)
    monkeypatch.setenv("HOME", str(tmp_path / "spoofed-home"))
    monkeypatch.setattr(SL, "CONVIVY_ROOT", tmp_path / "Convivy")
    target = tmp_path / "Convivy" / "brand-new"

    SL.start("nyh", directory=str(target), create_dir=True)

    assert tmux.trust_calls == [(passwd_home, str(target))]


# MARK: - Reaping scratch shells


@pytest.fixture
def reaper(SL, tmux, monkeypatch):
    """`reap_scratch_shells`'s `list-sessions` (space-delimited,
    `name attached activity`) and `sessions`'s two calls both answer here.
    `sessions`'s trusted anchor call (`-F '#{session_id}\t#{session_name}'`,
    one `$<id><TAB><name>` per line) answers from `tmux.names`; its four-field
    record call, and the reaper's own call, both answer from `tmux.listing`.
    Panes from `tmux.panes`."""
    tmux.listing = ""
    tmux.names = ""
    original = SL.TT.run

    def fake_run(argv, stdin=None):
        if argv[:2] == ["tmux", "list-sessions"]:
            original(argv, stdin)          # still recorded, for call assertions
            if argv[-1] == "#{session_id}\t#{session_name}":
                return tmux.names
            return tmux.listing
        return original(argv, stdin)

    monkeypatch.setattr(SL.TT, "run", fake_run)
    return tmux


def test_a_fleet_session_is_unaddressable_by_the_reaper(SL, reaper):
    """THE GATE THAT MATTERS MOST. Everything else decides WHETHER a scratch
    shell is stale; this decides that an agent's session is not a candidate at
    all, however idle it looks."""
    reaper.listing = "vivy 0 0\nsteward 0 0\ndeck 0 0"
    result = SL.reap_scratch_shells(now=10**9)
    assert result["reaped"] == []
    assert result["spared"] == []
    assert "kill-session" not in _verbs(reaper)


def test_an_attached_shell_is_never_closed_however_idle(SL, reaper):
    """A pane open in front of the user must not close under them."""
    reaper.listing = "deck-shell-vivy 1 0"
    result = SL.reap_scratch_shells(now=10**9)
    assert result["reaped"] == []
    assert result["spared"] == [{"session": "deck-shell-vivy",
                                 "why": "a client is attached"}]
    assert "kill-session" not in _verbs(reaper)


def test_a_shell_with_work_in_it_is_spared_however_quiet(SL, reaper):
    """`session_activity` tracks OUTPUT, so a long silent command — a build, a
    sleep, a `tail -f` on a quiet log — looks exactly like an abandoned prompt
    to the clock. The pane's foreground process is what tells them apart, and it
    is why the clock alone is not the gate."""
    reaper.listing = "deck-shell-vivy 0 0"
    reaper.panes["deck-shell-vivy"] = "%4 sleep"
    result = SL.reap_scratch_shells(now=10**9)
    assert result["reaped"] == []
    assert result["spared"] == [{"session": "deck-shell-vivy",
                                 "why": "something is running in it"}]
    assert "kill-session" not in _verbs(reaper)


def test_a_split_shell_with_work_in_one_pane_is_spared(SL, reaper):
    """THE HOLE THIS CLOSES. The first draft asked `_idle_shell_pane`, which
    answers "is there a pane I can type into" and returns the FIRST bare shell
    it finds. A session split with `sleep 600` in one pane and a prompt in the
    other therefore answered yes, and would have been killed with the work still
    running — the exact property the reaper exists to guarantee.

    The suite passed with the bug because no test had two panes. Caught by
    reviewer.
    """
    reaper.listing = "deck-shell-vivy 0 0"
    reaper.panes["deck-shell-vivy"] = "%1 sleep\n%2 zsh"
    result = SL.reap_scratch_shells(idle_seconds=0, now=10**9)
    assert result["reaped"] == []
    assert result["spared"] == [{"session": "deck-shell-vivy",
                                 "why": "something is running in it"}]
    assert "kill-session" not in _verbs(reaper)


def test_a_split_shell_idle_in_every_pane_is_reaped(SL, reaper):
    """The other side of the quantifier: all panes idle, so nothing is lost."""
    reaper.listing = "deck-shell-vivy 0 0"
    reaper.panes["deck-shell-vivy"] = "%1 zsh\n%2 bash"
    result = SL.reap_scratch_shells(idle_seconds=0, now=1000)
    assert [r["session"] for r in result["reaped"]] == ["deck-shell-vivy"]


def test_an_unreadable_pane_listing_spares_the_shell(SL, reaper):
    """A reaper that cannot see what is in a pane must not close it."""
    reaper.listing = "deck-shell-vivy 0 0"
    reaper.panes["deck-shell-vivy"] = "garbage"
    result = SL.reap_scratch_shells(idle_seconds=0, now=10**9)
    assert result["reaped"] == []
    assert result["spared"][0]["why"] == "something is running in it"


def test_the_clock_gate_runs_before_the_pane_read(SL, reaper):
    """Comparing two integers is free; reading panes is a subprocess per
    session. A shell used moments ago is spared by age alone, so inspecting it
    costs a process for nothing on every run."""
    reaper.listing = "deck-shell-vivy 0 990"
    reaper.panes["deck-shell-vivy"] = "%1 zsh"
    SL.reap_scratch_shells(idle_seconds=100, now=1000)
    assert "list-panes" not in _verbs(reaper)


def test_a_recently_used_shell_is_spared(SL, reaper):
    reaper.listing = "deck-shell-vivy 0 990"
    reaper.panes["deck-shell-vivy"] = "%4 zsh"
    result = SL.reap_scratch_shells(idle_seconds=100, now=1000)
    assert result["reaped"] == []
    assert result["spared"][0]["why"] == "idle only 10s"


def test_an_idle_unattached_empty_shell_is_reaped(SL, reaper):
    reaper.listing = "deck-shell-vivy 0 0"
    reaper.panes["deck-shell-vivy"] = "%4 zsh"
    result = SL.reap_scratch_shells(idle_seconds=100, now=1000)
    assert result["reaped"] == [{"session": "deck-shell-vivy", "idle_seconds": 1000}]
    assert ("tmux", "kill-session", "-t", "=deck-shell-vivy") in reaper


def test_an_unreadable_activity_time_spares_the_shell(SL, reaper):
    """The pass is advisory. A session it cannot judge keeps its shell."""
    reaper.listing = "deck-shell-vivy 0 notanumber"
    reaper.panes["deck-shell-vivy"] = "%4 zsh"
    result = SL.reap_scratch_shells(now=10**9)
    assert result["reaped"] == []
    assert result["spared"][0]["why"] == "activity time unreadable"


def test_a_forged_line_cannot_name_another_shell(SL, reaper):
    """`list-sessions` output is split on whitespace, and tmux permits whitespace
    in a name even though `validate_session_name` does not. A session created
    outside `session-agent` and named with an embedded newline yields a synthetic
    line whose first token is whatever followed it — able to name ANOTHER scratch
    shell and kill it early against a timestamp that was never its own.

    Nothing outside the `deck-shell-` namespace was ever addressable; what the
    full-name match closes is one scratch shell forging a line about a sibling.
    Raised by security-reviewer.
    """
    reaper.listing = "deck-shell-a b c d\ndeck-shell-b! 0 0\n 0 0"
    reaper.panes["deck-shell-b!"] = "%1 zsh"
    result = SL.reap_scratch_shells(idle_seconds=0, now=10**9)
    assert result["reaped"] == []
    assert "kill-session" not in _verbs(reaper)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1])
def test_a_threshold_that_cannot_be_compared_is_refused(SL, reaper, bad):
    """`idle < nan` is False, so a NaN falls THROUGH the age gate to the kill
    rather than sparing — the opposite of how every other unreadable input here
    is treated. Refused rather than clamped, so the caller learns."""
    reaper.listing = "deck-shell-vivy 0 0"
    reaper.panes["deck-shell-vivy"] = "%1 zsh"
    with pytest.raises(SL.LifecycleError, match="non-negative"):
        SL.reap_scratch_shells(idle_seconds=bad, now=10**9)
    assert "kill-session" not in _verbs(reaper)


def test_no_tmux_at_all_is_not_an_error(SL, tmux, monkeypatch):
    """This runs unattended; a missing tmux means nothing to reap."""
    monkeypatch.setattr(SL.TT, "run", lambda argv, stdin=None: None)
    assert SL.reap_scratch_shells() == {"ok": True, "reaped": [], "spared": []}


# MARK: - Listing sessions, for Deck's discovery poll


def test_two_tmux_calls_list_every_session(SL, reaper):
    """Anchoring against a trusted name listing costs one more call than the
    original single-call design, but still nothing per session — Deck polls
    this on the rail's ~4s cadence, so a round trip per session would be one
    more subprocess per row every few seconds."""
    reaper.names = "$1\tvivy\n$2\task"
    reaper.listing = "vivy\t1\t0\t/srv/convivy/vivy\n" \
                      "ask\t1\t1\t/srv/convivy/ask"
    SL.sessions()
    assert _verbs(reaper) == ["list-sessions", "list-sessions"]


def test_deck_shell_sessions_are_excluded(SL, reaper):
    """Deck's own scratch shells, one per fleet session, are noise in a list
    meant for grouping conversations under a project."""
    reaper.names = "$1\tvivy\n$2\tdeck-shell-vivy"
    reaper.listing = "vivy\t1\t0\t/srv/convivy/vivy\n" \
                      "deck-shell-vivy\t1\t0\t/srv/convivy/vivy"
    result = SL.sessions()
    assert [s["name"] for s in result["sessions"]] == ["vivy"]


def test_a_session_with_more_than_one_window_reports_its_count(SL, reaper):
    reaper.names = "$1\task"
    reaper.listing = "ask\t3\t1\t/srv/convivy/ask"
    result = SL.sessions()
    assert result["sessions"] == [{"name": "ask", "sessionId": "$1", "path": "/srv/convivy/ask",
                                   "windows": 3, "attached": True}]


def test_the_ordinary_case_keeps_every_session_and_its_path(SL, reaper):
    """Nothing dropped, paths correct, trusted-listing order preserved."""
    reaper.names = "$1\tvivy\n$2\task"
    reaper.listing = "vivy\t1\t0\t/srv/convivy/vivy\n" \
                      "ask\t1\t1\t/srv/convivy/ask"
    result = SL.sessions()
    assert result["sessions"] == [
        {"name": "vivy", "sessionId": "$1", "path": "/srv/convivy/vivy",
         "windows": 1, "attached": False},
        {"name": "ask", "sessionId": "$2", "path": "/srv/convivy/ask",
         "windows": 1, "attached": True},
    ]


def test_no_tmux_server_is_an_empty_list_not_an_error(SL, tmux, monkeypatch):
    """Same degradation `reap_scratch_shells` gives an absent tmux: this runs
    on a poll that must not error a whole rail over it. Both calls answer
    `None`, and the first of them — the trusted-name anchor — is enough on its
    own to short-circuit to the empty list."""
    monkeypatch.setattr(SL.TT, "run", lambda argv, stdin=None: None)
    assert SL.sessions() == {"sessions": []}


def test_no_sessions_at_all_is_an_empty_list(SL, reaper):
    reaper.names = ""
    reaper.listing = ""
    assert SL.sessions() == {"sessions": []}


def test_a_session_with_no_readable_path_degrades_that_field(SL, reaper):
    """A field that could not be established is reported `null`, not dropped —
    the row still carries a name, a window count and whether it is attached."""
    reaper.names = "$1\tvivy"
    reaper.listing = "vivy\t1\t0\t"
    result = SL.sessions()
    assert result["sessions"] == [{"name": "vivy", "sessionId": "$1", "path": None,
                                   "windows": 1, "attached": False}]


def test_a_line_with_too_few_fields_before_any_record_is_dropped(SL, reaper):
    """A fragment seen before anything three-tabbed has started a record has
    nowhere to attach and is dropped; the record after it is unaffected. It
    also never reaches the trusted set, since it is not a name call one
    reported."""
    reaper.names = "$1\tvivy"
    reaper.listing = "badline-missing-fields\nvivy\t1\t0\t/srv/convivy/vivy"
    result = SL.sessions()
    assert [s["name"] for s in result["sessions"]] == ["vivy"]


def test_an_unparseable_window_count_is_emitted_with_everything_else_null(SL, reaper):
    """A record that fails to parse is no longer dropped outright — the name
    is still trusted (call one reported it), so the row survives with every
    field this parse could not vouch for set to `null` rather than the whole
    session vanishing from a poll Deck relies on for discovery."""
    reaper.names = "$1\tvivy"
    reaper.listing = "vivy\tnotanumber\t0\t/srv/convivy/vivy"
    result = SL.sessions()
    assert result["sessions"] == [{"name": "vivy", "sessionId": "$1", "path": None,
                                   "windows": None, "attached": None}]


def test_a_non_ascii_digit_in_the_window_count_degrades_rather_than_raises(SL, reaper):
    """`str.isdigit()` alone is not a conclusive gate before `int()`: it
    accepts non-ASCII decimal-look-alike characters — here U+00B2 SUPERSCRIPT
    TWO, '²' — that `int()` then rejects with `ValueError`. Reproduced live:
    `'²'.isdigit()` is `True`, `int('²')` raises. A trusted name whose genuine
    record vanished between the two calls, plus a live session forging a
    record under that now-orphaned name with this character in the window
    count, would turn a poll this function's docstring promises never fails
    into an unhandled crash. The row must degrade like any other unparseable
    window count, not propagate.
    """
    reaper.names = "$1\tvictim"
    reaper.listing = "victim\t²\t0\t/fake"
    result = SL.sessions()  # must not raise
    assert result["sessions"] == [{"name": "victim", "sessionId": "$1", "path": None,
                                   "windows": None, "attached": None}]


def test_a_raw_tab_in_the_path_is_kept_verbatim(SL, reaper):
    """`#{session_path}` is the trailing field and is parsed with `maxsplit=3`,
    so a raw tab in it (legal in a POSIX directory name; tmux does not escape
    it the way it escapes `#{session_name}`) lands in `path` rather than
    shifting the record to five fields."""
    reaper.names = "$1\tvivy"
    reaper.listing = "vivy\t1\t0\t/tmp/a\tb"
    result = SL.sessions()
    assert result["sessions"] == [{"name": "vivy", "sessionId": "$1", "path": "/tmp/a\tb",
                                   "windows": 1, "attached": False}]


def test_a_raw_newline_with_no_tabs_after_it_rejoins_the_path(SL, reaper):
    """A path holding a raw newline but nothing that reads as three more tabs
    after it has no ambiguity to resolve: the fragment after the newline never
    satisfies the `>= 3 tabs` record-start test, so it is rejoined onto the
    record it split off from and the path comes back whole, newline and all."""
    reaper.names = "$1\trealsession"
    reaper.listing = "realsession\t3\t1\t/tmp/proj\nno-tabs-here-at-all"
    result = SL.sessions()
    assert result["sessions"] == [
        {"name": "realsession", "sessionId": "$1", "path": "/tmp/proj\nno-tabs-here-at-all",
         "windows": 3, "attached": True}]


def test_a_forged_record_with_an_untrusted_name_is_dropped(SL, reaper):
    """THE FORGERY round 2 closes: a session whose path holds a raw newline
    FOLLOWED BY three more raw tabs (`mkdir -p
    $'/tmp/x\\nfake\\t9\\t1\\t/fake'`, legal POSIX) makes the record parse
    below split into two apparent records — `realsession`'s own, truncated at
    the newline, and a second, fully attacker-shaped one named `fake`. No
    re-framing of this one call can tell those apart from inside it; the fix
    is that only `realsession` is a name call one (the trusted anchor) ever
    reported, so `fake` is dropped regardless of how well-formed it looks.
    `realsession` itself survives — with a truncated path, the cost of the
    self-inflicted collision, not a forgery, since it is still the one and
    only record under its own trusted name."""
    reaper.names = "$1\trealsession"
    reaper.listing = "realsession\t1\t1\t/tmp/x\nfake\t9\t1\t/fake"
    result = SL.sessions()
    assert [s["name"] for s in result["sessions"]] == ["realsession"]
    assert result["sessions"][0]["path"] == "/tmp/x"


def test_a_forged_duplicate_of_a_real_name_is_emitted_once_with_path_null(SL, reaper):
    """A crafted path could instead forge a record under a name that is ALSO a
    real, trusted session's own name — the trusted-set check alone would let
    it through. Two parsed records sharing a trusted name means which one is
    genuine is not decidable here, so neither is guessed at: the session is
    emitted once, by name, with every field this parse could not vouch for set
    to `null`."""
    reaper.names = "$1\treal"
    reaper.listing = "real\t1\t0\t/tmp/a\nreal\t9\t1\t/fake"
    result = SL.sessions()
    assert result["sessions"] == [{"name": "real", "sessionId": "$1", "path": None,
                                   "windows": None, "attached": None}]


def test_a_trusted_name_with_no_parsed_record_is_emitted_with_path_null(SL, reaper):
    """Call one is the anchor and is trusted on its own: a name it reports that
    the record call did not (a race between the two calls, or a record call
    that failed outright) still gets a row, name-only, rather than vanishing
    from a poll Deck relies on for discovery."""
    reaper.names = "$1\tghost"
    reaper.listing = ""
    result = SL.sessions()
    assert result["sessions"] == [{"name": "ghost", "sessionId": "$1", "path": None,
                                   "windows": None, "attached": None}]


def test_malformed_listings_never_raise(SL, reaper):
    """The `maxsplit=3` parse removes the unpack hazard structurally — there is
    no field-count guard to get wrong, because a record that reaches the
    unpack always has exactly four parts. Garbage in any shape must degrade,
    never raise, on the poll this function's docstring promises never fails."""
    reaper.names = "$1\tx"
    reaper.listing = "\t\t\t\ngarbage with no tabs at all\n" \
                      "\t1\t0\t/x\tmore\ttabs\tin\tthe\tpath"
    result = SL.sessions()  # must not raise
    assert isinstance(result["sessions"], list)


def test_every_row_carries_the_tmux_session_id(SL, reaper):
    """Deck matches a pinned child to its live session by `sessionId`, the
    field that survives a rename where a name does not. Without it a session
    renamed in tmux drew twice on the rail."""
    reaper.names = "$58\tvivy"
    reaper.listing = "vivy\t1\t0\t/srv/convivy/vivy"
    assert SL.sessions()["sessions"][0]["sessionId"] == "$58"


@pytest.mark.parametrize("raw", ["", "58", "$", "$5x", "@3", "$1234567890"])
def test_an_id_that_is_not_tmux_shape_reads_null(SL, reaper, raw):
    """Only tmux's `$<digits>` passes through, the same shape `session-agent
    crews` validates. Anything else is `null`, and the row is otherwise
    untouched."""
    reaper.names = f"{raw}\tvivy"
    reaper.listing = "vivy\t1\t0\t/x"
    assert SL.sessions()["sessions"] == [{"name": "vivy", "sessionId": None,
                                          "path": "/x", "windows": 1,
                                          "attached": False}]


# MARK: - What a command may be


@pytest.mark.parametrize("hostile,label", [
    ("co\nrm -rf /", "a newline would submit and start a second command"),
    ("co\rrm -rf /", "carriage return"),
    ("co\x1b[2J", "an escape sequence is terminal control, not text"),
    ("co\x00", "null byte"),
    ("co\x7f", "delete"),
])
def test_a_command_must_be_one_line_of_text(SL, hostile, label):
    with pytest.raises(SL.LifecycleError, match="one line"):
        SL._validated_command(hostile)


def test_a_command_is_length_capped(SL):
    with pytest.raises(SL.LifecycleError, match="longer than"):
        SL._validated_command("co " + "x" * SL.MAX_COMMAND)


def test_an_empty_command_is_refused(SL):
    for empty in ["", "   ", "\t"]:
        with pytest.raises(SL.LifecycleError):
            SL._validated_command(empty)


@pytest.mark.parametrize("allowed", ["co", "co -r", "co plan", "co --model kimi"])
def test_a_real_override_is_allowed(SL, allowed):
    """NOT an allowlist of commands, deliberately: the catalog exists so a row
    can say `co -r`, and enumerating them here would mean editing this file
    every time one is added."""
    assert SL._validated_command(allowed) == allowed


# MARK: - Stopping


def test_stopping_ends_the_agent_and_leaves_the_session(SL, tmux, monkeypatch):
    sent = []
    monkeypatch.setattr(SL.SI, "send_message", lambda n, t: sent.append((n, t)))
    tmux.existing.add("nyh")

    result = SL.stop("nyh")

    assert sent == [("nyh", "/exit")]
    assert "kill-session" not in _verbs(tmux)
    assert result["killed"] is False
    assert result["exit_sent"] is True


def test_killing_is_explicit_and_ends_the_session_too(SL, tmux, monkeypatch):
    """It destroys scrollback and anything else running there, so it never
    happens as a side effect of the gentle stop."""
    monkeypatch.setattr(SL.SI, "send_message", lambda n, t: None)
    tmux.existing.add("nyh")

    result = SL.stop("nyh", kill=True)

    assert ("tmux", "kill-session", "-t", "=nyh") in tmux
    assert result["killed"] is True


def test_stopping_a_session_with_no_agent_is_not_a_failure(SL, tmux, monkeypatch):
    """A session with nothing running in it is already where `stop` is going."""
    def no_agent(name, text):
        raise SL.TT.JoinError("no claude process found")
    monkeypatch.setattr(SL.SI, "send_message", no_agent)
    tmux.existing.add("nyh")

    result = SL.stop("nyh", kill=True)
    assert result["ok"] is True
    assert result["exit_sent"] is False
    assert result["killed"] is True


def test_stopping_a_session_that_does_not_exist_is_not_a_failure(SL, tmux):
    """The caller wanted it gone and it is gone."""
    result = SL.stop("nyh", kill=True)
    assert result == {"ok": True, "session": "nyh", "existed": False,
                      "exit_sent": False, "agent_gone": True, "killed": False}
    assert "kill-session" not in _verbs(tmux)


def test_the_kill_target_is_anchored_to_an_exact_name(SL, tmux, monkeypatch):
    """`-t nyh` is a prefix match in tmux; `-t =nyh` is the exact session.
    Without the anchor, stopping `nyh` could kill `nyh-park-villa` — and the split
    into three per-property sessions (ARCH-426 amendment) makes that likelier, not
    less, since there are now three live session names sharing the prefix."""
    monkeypatch.setattr(SL.SI, "send_message", lambda n, t: None)
    tmux.existing.add("nyh")

    SL.stop("nyh", kill=True)
    kill = next(c for c in tmux if c[1] == "kill-session")
    assert kill[-1] == "=nyh"


# MARK: - Waiting for the agent to actually go


def test_the_kill_waits_for_the_agent_to_leave(SL, tmux, monkeypatch):
    """THE BUG REVIEW FOUND. `send_message` settles the paste for 0.4s and
    returns; it does not wait for Claude Code to process `/exit`. So `--kill`
    destroyed the session while the agent was still exiting — "stop the agent,
    then kill the session" was really "signal it and pull the floor away"."""
    monkeypatch.setattr(SL.SI, "send_message", lambda n, t: None)
    tmux.existing.add("nyh")

    looks = []

    def claude_pane(name):
        looks.append(name)
        if len(looks) < 3:            # still there for two polls
            return ("%1", 4242)
        raise SL.TT.JoinError("no claude process found")

    monkeypatch.setattr(SL.TT, "claude_pane", claude_pane)
    monkeypatch.setattr(SL.time, "sleep", lambda _s: None)

    result = SL.stop("nyh", kill=True)

    assert len(looks) >= 3, "the kill did not wait for the agent"
    assert result["agent_gone"] is True
    assert result["killed"] is True


def test_an_agent_that_will_not_leave_does_not_block_the_kill(SL, tmux, monkeypatch):
    """The user asked for the session gone. A wedged agent must not be able to
    hold it open forever — the wait is bounded and the outcome is reported."""
    monkeypatch.setattr(SL.SI, "send_message", lambda n, t: None)
    monkeypatch.setattr(SL.TT, "claude_pane", lambda name: ("%1", 4242))
    monkeypatch.setattr(SL.time, "sleep", lambda _s: None)
    tmux.existing.add("nyh")

    result = SL.stop("nyh", kill=True)

    assert result["agent_gone"] is False, "it should say the agent was still there"
    assert result["killed"] is True, "and kill anyway, because that is what was asked"


def test_the_exit_wait_resolves_its_session_table_fresh_on_every_poll(SL, monkeypatch):
    """Non-blocking finding, PR #480 round 2. `_await_agent_exit` polls for the
    session to DISAPPEAR, so it must NOT share one resolved session table across
    polls the way `session_states()`'s batch deliberately does — a table fetched
    once at the top of the wait would keep answering that a gone session is still
    there for the rest of the timeout window. Bypasses the `tmux` fixture (which
    stubs `claude_pane` directly) so the real `claude_pane` -> `panes` ->
    `_resolve_session_id` chain runs and reaches `_run` for real."""
    calls = {"list_sessions": 0, "polls": 0}

    def fake_run(argv, stdin=None):
        if argv[:2] == ["tmux", "list-sessions"]:
            calls["list_sessions"] += 1
            return "$1 nyh"
        if argv[:2] == ["tmux", "list-panes"]:
            calls["polls"] += 1
            if calls["polls"] > 2:
                return None  # the third poll finds the pane gone
            return "%1 500"
        if argv[:2] == ["ps", "-o"]:
            return "claude"  # so the pane's own pid counts as the agent
        return None

    monkeypatch.setattr(SL.TT, "_run", fake_run)
    monkeypatch.setattr(SL.time, "sleep", lambda _s: None)

    gone = SL._await_agent_exit("nyh", timeout=5.0)

    assert gone is True, "the third poll's empty pane list is the agent leaving"
    assert calls["polls"] == 3
    assert calls["list_sessions"] == 3, (
        "each poll must resolve its own table rather than sharing one across the wait")


def test_every_return_carries_the_same_keys(SL, tmux, monkeypatch):
    """An asymmetric shape means a caller reading a key gets a KeyError on
    exactly the branch it was least likely to test."""
    monkeypatch.setattr(SL.SI, "send_message", lambda n, t: None)
    monkeypatch.setattr(SL.TT, "claude_pane",
                        lambda name: (_ for _ in ()).throw(SL.TT.JoinError("gone")))
    expected = {"ok", "session", "existed", "exit_sent", "agent_gone", "killed"}

    assert set(SL.stop("absent", kill=True)) == expected
    tmux.existing.add("nyh")
    assert set(SL.stop("nyh")) == expected
    tmux.existing.add("nyh2")
    assert set(SL.stop("nyh2", kill=True)) == expected


def test_send_keys_is_not_anchored_and_the_others_are(SL, tmux, tmp_path):
    """`=` is a SESSION-target modifier and send-keys takes a PANE target, so
    `send-keys -t =name` fails and the session starts with no command in it.
    The mocked run returns success for everything, so only a live run caught it
    — this pins the shape the live run proved."""
    SL.start("nyh", directory=str(tmp_path))

    send = next(c for c in tmux if c[1] == "send-keys")
    assert "=nyh" not in send, "send-keys must target the pane unanchored"

    has = next(c for c in tmux if c[1] == "has-session")
    assert has[-1] == "=nyh", "session targets stay anchored"


# MARK: - The key table


@pytest.mark.parametrize("keyname", ["C-d", "C-c", "Enter", "Escape", "Tab", "Up", "F1"])
def test_a_command_that_is_a_tmux_key_name_is_typed_not_pressed(SL, tmux, tmp_path, keyname):
    """THE VULNERABILITY. `send-keys` matches each argument against its key
    table first, so an argument that IS a key name is dispatched as that key
    rather than typed. A catalog row of `C-d` sent Ctrl-D and exited the shell,
    leaving a session with no agent — the state the command gate was meant to
    prevent, reached while looking like nothing went wrong.

    Measured against real tmux: without `-l` the session was dead a second
    later; with it the shell printed `C-d: command not found` and stayed up.
    """
    SL.start("nyh", directory=str(tmp_path), command=keyname)

    literal = [c for c in tmux if c[1] == "send-keys" and "-l" in c]
    assert literal, f"{keyname} was sent without -l and would be pressed, not typed"
    assert keyname in literal[0]
    # The submit is a real key press, and the only one.
    presses = [c for c in tmux if c[1] == "send-keys" and "-l" not in c]
    assert presses == [("tmux", "send-keys", "-t", "nyh", "Enter")]


@pytest.mark.parametrize("separator", ["\u2028", "\u2029"])
def test_unicode_line_separators_are_refused(SL, separator):
    """Not exploitable at a shell, which reads only ASCII control bytes as line
    breaks — but Deck is the caller, JavaScript treats these as real line breaks
    in a string literal, and a step there that re-serialised the command would
    turn one into a newline past this gate."""
    with pytest.raises(SL.LifecycleError, match="one line"):
        SL._validated_command(f"co{separator}rm -rf /")


# ---------------------------------------------------------------------------
# The default tmux server is started from the GUI session, never forked here.
#
# Every test below runs `tmux` and `launchctl` from a PATH holding only stub
# scripts, so no test reaches the machine's real server or keeper job. The stubs
# come first on PATH, and each test writes both, so a missing stub is a failing
# test rather than a call to the real binary.

_TMUX_STUB = """#!/bin/sh
echo "tmux $*" >> "$STUB_LOG"
echo "tmux-env TMUX=${TMUX+set} TMUX_PANE=${TMUX_PANE+set}" >> "$STUB_LOG"
case "$1" in
  list-sessions)
    if [ -f "$STUB_STATE/sessions" ]; then cat "$STUB_STATE/sessions"; exit 0; fi
    echo "no server running on /tmp/tmux-0/default" >&2
    exit 1 ;;
esac
exit 0
"""

_LAUNCHCTL_STUB = """#!/bin/sh
echo "launchctl $*" >> "$STUB_LOG"
if [ "$1" = kickstart ]; then
  if [ -n "$STUB_KICKSTART_FAIL" ]; then echo "Could not find service" >&2; exit 113; fi
  if [ -n "$STUB_KEEPER_SESSIONS" ]; then printf '%s\\n' $STUB_KEEPER_SESSIONS > "$STUB_STATE/sessions"; fi
fi
exit 0
"""


@pytest.fixture
def stubs(SL, tmp_path, monkeypatch):
    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir()
    state = tmp_path / "stub-state"
    state.mkdir()
    log = tmp_path / "stub.log"
    log.write_text("")
    for name, body in (("tmux", _TMUX_STUB), ("launchctl", _LAUNCHCTL_STUB)):
        script = bin_dir / name
        script.write_text(body)
        script.chmod(0o755)
    monkeypatch.setattr(SL, "ensure_default_server", SL._real_ensure_default_server)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("STUB_LOG", str(log))
    monkeypatch.setenv("STUB_STATE", str(state))
    monkeypatch.delenv("STUB_KICKSTART_FAIL", raising=False)
    monkeypatch.delenv("STUB_KEEPER_SESSIONS", raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)

    class Stubs:
        def lines(self):
            return log.read_text().splitlines()

        def sessions(self, *names):
            (state / "sessions").write_text("".join(f"{n}\n" for n in names))

    return Stubs()


KICKSTART = f"launchctl kickstart gui/{os.getuid()}/com.convivy.tmux-keeper"


def test_a_running_server_leaves_launchctl_uncalled(SL, stubs, monkeypatch):
    stubs.sessions("deck-x")
    # Were the server treated as down, a kickstart would bring the keeper up,
    # so the helper returns cleanly and only the launchctl assertion can fail.
    monkeypatch.setenv("STUB_KEEPER_SESSIONS", "keeper")

    SL.ensure_default_server(wait=0.3, poll=0.05)

    # Positive control: the stub was consulted, so "no launchctl line" is a
    # real absence and not a stub that never ran.
    assert "tmux list-sessions -F #{session_name}" in stubs.lines()
    assert not [line for line in stubs.lines() if line.startswith("launchctl")]


def test_a_missing_server_is_started_by_kickstarting_the_keeper_without_k(SL, stubs, monkeypatch):
    monkeypatch.setenv("STUB_KEEPER_SESSIONS", "keeper")

    SL.ensure_default_server(wait=2, poll=0.01)

    kicks = [line for line in stubs.lines() if line.startswith("launchctl")]
    # Exact argv: `-k` would kill a keeper that is already running.
    assert kicks == [f"launchctl kickstart gui/{os.getuid()}/com.convivy.tmux-keeper"]
    # The server is the keeper's to fork; this helper forks none.
    assert not [line for line in stubs.lines() if "new-session" in line or "start-server" in line]


def test_a_failed_kickstart_refuses_with_the_command_and_forks_nothing(SL, stubs, monkeypatch):
    monkeypatch.setenv("STUB_KICKSTART_FAIL", "1")

    # The text only the kickstart branch produces; the timeout branch also names
    # the command, so matching on the command alone would not tell them apart.
    with pytest.raises(SL.TmuxServerError, match=r"launchctl kickstart exited 113") as refused:
        SL.ensure_default_server(wait=0.3, poll=0.05)
    # The message tells the operator what to run, verbatim.
    assert KICKSTART in str(refused.value)

    assert [line for line in stubs.lines() if line.startswith("launchctl")]
    assert not [line for line in stubs.lines() if "new-session" in line or "start-server" in line]


def test_a_keeper_that_never_appears_times_out_and_refuses(SL, stubs):
    # The kickstart succeeds but no keeper session ever comes up.
    with pytest.raises(SL.TmuxServerError, match="did not appear within 0.3 seconds"):
        SL.ensure_default_server(wait=0.3, poll=0.05)

    assert not [line for line in stubs.lines() if "new-session" in line or "start-server" in line]


def test_a_session_named_like_the_keeper_does_not_count_as_the_keeper(SL, stubs, monkeypatch):
    # `has-session -t keeper` would prefix-match `keepers`; the list is matched exactly.
    monkeypatch.setenv("STUB_KEEPER_SESSIONS", "keepers")

    with pytest.raises(SL.TmuxServerError, match="did not appear"):
        SL.ensure_default_server(wait=0.3, poll=0.05)


def test_the_helper_strips_tmux_identity_so_tmux_sees_neither_variable(SL, stubs, monkeypatch):
    # A caller inside a pane has both set. Left in, tmux would take its socket
    # from TMUX and check the caller's server rather than the default one.
    stubs.sessions("deck-x")
    monkeypatch.setenv("TMUX", "/tmp/tmux-caller/default,1234,0")
    monkeypatch.setenv("TMUX_PANE", "%7")

    SL.ensure_default_server(wait=0.3, poll=0.05)

    # Positive control: the stub ran and logged, so the env line is a real read.
    assert "tmux list-sessions -F #{session_name}" in stubs.lines()
    assert [line for line in stubs.lines() if line.startswith("tmux-env ")] == [
        "tmux-env TMUX= TMUX_PANE="]


def test_a_timeout_names_the_tmux_fault_it_last_saw(SL, stubs):
    # No sessions file, so the stub answers as tmux does with no server running.
    # The keeper never appears, and the refusal says what tmux reported.
    with pytest.raises(SL.TmuxServerError,
                       match="last tmux check: no server running on /tmp/tmux-0/default"):
        SL.ensure_default_server(wait=0.3, poll=0.05)


def test_a_timeout_names_the_os_error_when_tmux_cannot_run(SL, stubs, tmp_path, monkeypatch):
    # PATH holds only the launchctl stub, so tmux cannot be run at all. That is
    # a different fault from a server that is not running, and the refusal must
    # not present it as one.
    (tmp_path / "stub-bin" / "tmux").unlink()
    monkeypatch.setenv("PATH", str(tmp_path / "stub-bin"))

    with pytest.raises(SL.TmuxServerError, match="last tmux check: .*No such file or directory"):
        SL.ensure_default_server(wait=0.3, poll=0.05)


def test_the_default_server_is_ensured_before_the_session_is_created(SL, tmux, tmp_path):
    SL.start("nyh", directory=str(tmp_path))

    # Positions are counted in `tmux` (the recorded calls), so an ensure that
    # ran before the create sits at an index no greater than the create's.
    assert tmux.ensure_positions, "start() never asked for the default server"
    create = next(i for i, c in enumerate(tmux) if c[0] == "tmux" and c[1] == "new-session")
    assert tmux.ensure_positions[0] <= create


def test_a_refused_server_creates_no_session_and_no_directory(SL, tmux, tmp_path, monkeypatch):
    def refused(wait=None, poll=None):
        raise SL.TmuxServerError("the tmux server must be started from the GUI session")

    monkeypatch.setattr(SL, "ensure_default_server", refused)
    monkeypatch.setattr(SL, "CONVIVY_ROOT", tmp_path)
    target = tmp_path / "not-yet"

    with pytest.raises(SL.LifecycleError, match="GUI session"):
        SL.start("nyh", directory=str(target), create_dir=True)

    assert not target.exists()
    assert "new-session" not in _verbs(tmux)
