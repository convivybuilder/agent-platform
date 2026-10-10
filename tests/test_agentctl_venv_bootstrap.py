"""Each `kb` subcommand re-executes under its venv ONCE, as its own first
action, before anything reads stdin, takes the write lock, or journals a row.

`bin/agentctl`'s shebang is `#!/usr/bin/env python3.11`, which on a clean Linux
install resolves to the system interpreter — no PyYAML, no cryptography.
`cmd_kb_sync` and `cmd_kb_propose` need `yaml` (`_kb_indexer`); `cmd_kb_apply`
needs both (`_kb_indexer` and, for `--provenance person`, `deck_signature`).

Round 1 called `venv_bootstrap.ensure` lazily, deep inside `_kb_indexer`,
`_kb_propose_refusal` and `_kb_person_from_stdin`, wherever each subcommand's
own control flow happened to reach it first. For `kb apply --provenance
person` that place was AFTER `_kb_person_from_stdin` had already read stdin to
get the Deck signature or Access token: `ensure`'s re-exec is `os.execv`, which
restarts the process with the same stdin file descriptor at whatever position
the prior read left it. A pipe already drained does not rewind, so the
restarted process read EOF, the assertion was gone, and `kb apply --provenance
person` refused as `no-assertion` having verified nothing
(issue_01M3K7Z6ABJTDG61S2QAZR5GZC's second bug, found in round-2 review of
PR 695). The three lazy call sites are gone; each subcommand now calls
`ensure` itself, as the first statement in its body.

The structural test below reads the source directly, so it fails the moment a
future edit inserts anything — even a read-only one — ahead of that call. The
behavioural tests run the real `bin/agentctl` (loaded the way the `agentctl`
fixture in conftest.py does, via SourceFileLoader) as a subprocess under a
genuinely package-less interpreter, passing the subcommand's `args` as `None`:
if `ensure` is not truly the first thing the function does, the very next line
dereferences `None` and raises `AttributeError` instead of cleanly bootstrapping
or refusing.
"""
from __future__ import annotations

import ast
import os
import subprocess
import venv
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
AGENTCTL = REPO / "bin" / "agentctl"
LIB = str(REPO / "lib" / "agent-platform")

KB_SUBCOMMANDS = [
    ("cmd_kb_sync", ("yaml",)),
    ("cmd_kb_propose", ("yaml",)),
    ("cmd_kb_apply", ("yaml", "cryptography")),
]


# --- structural: `ensure` is the first statement, full stop -------------------


def _first_statement(funcname: str) -> ast.stmt:
    tree = ast.parse(AGENTCTL.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == funcname:
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(
                    body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                body = body[1:]  # the docstring
            if not body:
                raise AssertionError(f"{funcname} has no body past its docstring")
            return body[0]
    raise AssertionError(f"no function named {funcname} in {AGENTCTL}")


@pytest.mark.parametrize("funcname,requires", KB_SUBCOMMANDS)
def test_kb_subcommand_bootstraps_as_its_first_statement(funcname, requires):
    stmt = _first_statement(funcname)
    assert isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call), (
        f"{funcname}'s first statement after its docstring must be a bare "
        f"`venv_bootstrap.ensure(...)` call, found {ast.dump(stmt)}")
    call = stmt.value
    assert ast.unparse(call.func) == "venv_bootstrap.ensure", (
        f"{funcname}'s first statement calls {ast.unparse(call.func)}, not "
        f"venv_bootstrap.ensure — a read, a lock or a journal row could run "
        f"ahead of the bootstrap")
    (first_arg,) = call.args
    assert ast.unparse(first_arg) == "__file__"
    got_requires = None
    for kw in call.keywords:
        if kw.arg == "requires":
            got_requires = ast.literal_eval(kw.value)
    assert got_requires is not None, f"{funcname}'s ensure() call names no requires="
    assert set(got_requires) == set(requires), (
        f"{funcname} needs {sorted(requires)}, its ensure() call names "
        f"{sorted(got_requires)}")


def test_kb_indexer_and_kb_person_from_stdin_no_longer_bootstrap_themselves():
    """The three lazy call sites this fix removed must stay removed: a helper
    re-added here would run its `ensure` wherever ITS caller happens to reach
    it, exactly the bug this file exists to catch."""
    tree = ast.parse(AGENTCTL.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in (
                "_kb_indexer", "_kb_propose_refusal", "_kb_person_from_stdin"):
            for inner in ast.walk(node):
                if (isinstance(inner, ast.Call)
                        and ast.unparse(inner.func) == "venv_bootstrap.ensure"):
                    raise AssertionError(
                        f"{node.name} calls venv_bootstrap.ensure itself; that "
                        f"call belongs in its caller's subcommand function")


# --- behavioural: a real re-exec, forced by a genuinely package-less venv ----

HARNESS = '''
import importlib.machinery, importlib.util, sys
sys.path.insert(0, {libdir!r})
import venv_bootstrap
venv_bootstrap._passwd_home = lambda: None  # the real user's venv must not leak in
loader = importlib.machinery.SourceFileLoader("agentctl_under_test", {agentctl!r})
spec = importlib.util.spec_from_loader("agentctl_under_test", loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)
{call}
print("prefix=" + sys.prefix)
'''


def _make_bare_venv(path: Path) -> Path:
    """A fresh, isolated venv: no pip, no system site-packages, so it has
    neither `yaml` nor `cryptography` regardless of what runs pytest."""
    venv.EnvBuilder(with_pip=False, symlinks=True).create(path)
    return path


def _make_serving_venv(path: Path) -> Path:
    """A venv that inherits whatever runs pytest's own site-packages — real
    `yaml`/`cryptography`, since agentctl's own tests already need both."""
    venv.EnvBuilder(with_pip=False, symlinks=True, system_site_packages=True).create(path)
    return path


def _make_github_origin_repo(path: Path) -> Path:
    """A git repo this test owns, whose `origin` resolves to a GitHub URL.

    `_kb_origin_is_local`/`_kb_origin_is_github` read `git remote get-url`,
    pure git-config, no network — so pointing `KNOWLEDGE_ROOT` at this repo
    makes `_kb_refuse_if_not_github` fall through without ever contacting
    github.com or the machine's real, shared knowledge checkout."""
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin",
                    "https://github.com/convivy/knowledge.git"], check=True)
    return path


def _run(call: str, home: Path, python: Path, tmp_path: Path, stdin: str | None = None):
    script = tmp_path / "harness.py"
    script.write_text(HARNESS.format(agentctl=str(AGENTCTL), libdir=LIB, call=call))
    env = {"PATH": os.environ["PATH"], "HOME": str(home)}
    return subprocess.run([str(python), str(script)], env=env, input=stdin,
                          capture_output=True, text=True, timeout=60)


@pytest.fixture
def bare_python(tmp_path):
    v = _make_bare_venv(tmp_path / "bare")
    return v / "bin" / "python3"


@pytest.mark.parametrize("funcname,requires", KB_SUBCOMMANDS)
def test_kb_subcommand_refuses_clearly_with_no_venv(funcname, requires, tmp_path, bare_python):
    """`args=None`: if anything ran before `ensure`, it would dereference
    `None` and crash with `AttributeError`, not this clean refusal."""
    home = tmp_path / "home"
    home.mkdir()
    r = _run(f"mod.{funcname}(None)", home, bare_python, tmp_path)
    assert r.returncode == 1
    assert "Traceback" not in r.stderr
    for pkg in requires:
        assert pkg in r.stderr
    assert "no virtualenv was found" in r.stderr


@pytest.mark.parametrize("funcname,requires", [
    ("cmd_kb_apply", ("yaml", "cryptography")),
    ("cmd_kb_propose", ("yaml",)),
])
def test_kb_subcommand_reexecs_before_touching_its_args(funcname, requires, tmp_path, bare_python):
    """`args=None`, but now a serving venv exists to re-exec into. Both of
    these subcommands touch `args` in their very next line (`_kb_holder
    (args.holder)`), so the only way to reach that `None`-shaped
    `AttributeError` — rather than a `ModuleNotFoundError` three frames into a
    lazy `import yaml`/`import cryptography` — is for `ensure` to have already
    re-exec'd into a fit interpreter first. `cmd_kb_sync` is covered separately
    below: it does not touch `args` until well after the point this test
    checks, so `args=None` alone would not distinguish the two orderings for
    it.

    `cmd_kb_propose` also has a statement between `ensure()` and its first
    `args` read: `_kb_refuse_if_not_github`, which reads `KNOWLEDGE_ROOT`'s
    origin. Pointed at a plain local repo, that refusal returns before `args`
    is ever touched and this test would see the refusal's `2`, not the
    `AttributeError` it exists to catch — so `KNOWLEDGE_ROOT` is pointed at a
    repo whose origin resolves to GitHub (`_make_github_origin_repo`), read
    from git config alone and never fetched, for both subcommands (harmless
    for `cmd_kb_apply`, which never reads `KNOWLEDGE_ROOT` before its own
    `args` access)."""
    home = tmp_path / "home"
    home.mkdir()
    good = _make_serving_venv(home / ".local" / "lib" / "knowledge" / ".venv")
    origin_repo = _make_github_origin_repo(tmp_path / "knowledge-origin")
    call = f'''
import pathlib
mod.KNOWLEDGE_ROOT = pathlib.Path({str(origin_repo)!r})
try:
    mod.{funcname}(None)
except AttributeError as e:
    print("ATTRERR=" + str(e))
'''
    r = _run(call, home, bare_python, tmp_path)
    assert r.returncode == 0, r.stderr
    assert "ModuleNotFoundError" not in r.stdout and "ModuleNotFoundError" not in r.stderr
    assert f"prefix={good}" in r.stdout
    assert "ATTRERR=" in r.stdout and "'NoneType' object has no attribute" in r.stdout


def test_kb_sync_reexecs_before_touching_the_knowledge_checkout(tmp_path, bare_python):
    """`cmd_kb_sync`'s first `args` access is well past its bootstrap, so this
    pins the interpreter down a different way: point `KNOWLEDGE_ROOT` at a
    directory this test owns (never the machine's real, shared knowledge
    checkout — touching that from a test would be its own bug) with no `.git`
    in it, and check that the resulting refusal is the git-shaped one
    `cmd_kb_sync` itself prints, not a `ModuleNotFoundError` for `yaml`."""
    home = tmp_path / "home"
    home.mkdir()
    good = _make_serving_venv(home / ".local" / "lib" / "knowledge" / ".venv")
    fake_root = tmp_path / "not-a-git-repo"
    fake_root.mkdir()
    # `cmd_kb_sync` reaches far enough to import `knowledge`, which reads the
    # operator config at import time — give this subprocess's faked $HOME one,
    # at operator_config's own default path, same as the real install would.
    operator_cfg = home / ".config" / "convivy"
    operator_cfg.mkdir(parents=True)
    (operator_cfg / "operator.json").write_text(
        '{"excluded_crews": [], "operator_marker": "operator", "roster_crew": "operator-crew"}')
    call = f'''
import pathlib
mod.KNOWLEDGE_ROOT = pathlib.Path({str(fake_root)!r})
mod.cmd_kb_sync(None)
'''
    r = _run(call, home, bare_python, tmp_path)
    assert r.returncode == 0, r.stderr
    assert "ModuleNotFoundError" not in r.stdout and "ModuleNotFoundError" not in r.stderr
    assert f"prefix={good}" in r.stdout
    assert f"kb sync: {fake_root} is not a git repo" in r.stderr


# The behavioural proof that `_kb_person_from_stdin` itself sees the full,
# untruncated stdin payload once its caller bootstraps first — the actual
# mechanism of the bug this PR fixes — is `test_kb_apply.py`'s
# `test_kb_apply_bootstraps_before_reading_stdin_for_person_provenance`. It
# drives the real `cmd_kb_apply`, with a real Deck signature, and asserts on
# call ORDER rather than on a forced re-exec: a subprocess test that
# bootstraps the interpreter itself before calling `_kb_person_from_stdin`
# would pass on `agentctl` before this fix too, since by the time it called
# in, the interpreter would already be fit — it would not be testing what
# `cmd_kb_apply` itself does, only that the mechanism works when a caller
# gets the order right.
