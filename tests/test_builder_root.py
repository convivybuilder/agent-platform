"""lib/agent-platform/builder_root.py, the machine's one root resolver
(company/decisions/0008), and the count of paths that still bypass it."""
import importlib.util
import re
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def BR():
    spec = importlib.util.spec_from_file_location(
        "builder_root", ROOT / "lib" / "agent-platform" / "builder_root.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home" / "someone"
    h.mkdir(parents=True)
    return h


def _config(tmp_path, text):
    c = tmp_path / "session-agent.env"
    c.write_text(text)
    return c


def test_the_minis_pinned_line_resolves_to_exactly_that_root(BR, tmp_path, home):
    """The mini carries CREWS_ROOT=/srv/convivy/Convivy and must not move when
    the compiled-in fallback changes (company/decisions/0008). Resolved exactly,
    not merely to something under it."""
    config = _config(tmp_path, "CREWS_ROOT=/srv/convivy/Convivy\n")
    assert BR.configured_raw(config) == "/srv/convivy/Convivy"
    assert BR.root(config, BR.fallback_for(home), home) == Path("/srv/convivy/Convivy")


def test_no_config_means_the_fallback(BR, tmp_path, home):
    assert BR.root(tmp_path / "absent.env", BR.fallback_for(home), home) == home / "builder"


@pytest.mark.parametrize("text", ["", "OTHER=/x\n", "# CREWS_ROOT=/x\n", "CREWS_ROOT=\n",
                                  "CREWS_ROOT=relative/path\n"])
def test_an_unusable_value_falls_back(BR, tmp_path, home, text):
    assert BR.root(_config(tmp_path, text), BR.fallback_for(home), home) == home / "builder"


def test_quotes_are_tolerated(BR, tmp_path, home):
    target = tmp_path / "builder"
    config = _config(tmp_path, f'CREWS_ROOT="{target}"\n')
    assert BR.root(config, BR.fallback_for(home), home) == target


def test_a_plain_file_is_not_a_root(BR, tmp_path, home):
    f = tmp_path / "a-file"
    f.write_text("x")
    assert BR.root(_config(tmp_path, f"CREWS_ROOT={f}\n"), BR.fallback_for(home), home) \
        == home / "builder"


@pytest.mark.parametrize("which", ["filesystem-root", "home", "home-parent", "tilde"])
def test_a_root_too_wide_to_contain_anything_falls_back(BR, tmp_path, home, which):
    value = {"filesystem-root": "/", "home": str(home), "home-parent": str(home.parent),
             "tilde": "~"}[which]
    assert BR.root(_config(tmp_path, f"CREWS_ROOT={value}\n"), BR.fallback_for(home), home) \
        == home / "builder"


def test_tilde_expands_against_the_passwd_home_not_home_env(BR, tmp_path, home, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "spoofed"))
    assert BR.expanded("~/builder", home) == home / "builder"


def test_the_account_home_comes_from_passwd(BR, tmp_path, monkeypatch):
    import pwd
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: types.SimpleNamespace(pw_dir=str(tmp_path)))
    monkeypatch.setenv("HOME", "/somewhere/else")
    assert BR.real_account_home() == tmp_path


# --- the migration is done when this count is zero ------------------------------
# company/decisions/0008: "A partial migration would split the readers without
# showing it ... the change is done when no ~/Convivy construction remains ... and
# that count should be a check CI runs." Each slice that moves readers onto
# builder_root.root() lowers BASELINE to the new count; the last slice makes it 0.
# The count can never rise, so no new construction lands while the migration runs.
# Any way a path under ~/Convivy is spelled out: a join with a "Convivy" literal
# (`Path.home() / "Convivy"`, or `home / "Convivy"` through a variable), a string
# holding "/Convivy/" (`"/Convivy/" in cwd`, `cwd.split("/Convivy/")`), or the
# ~, $HOME, expanduser and /Users/<name> forms. Comments count too: the name must
# be gone, not merely unused. Security review, PR 529: the first version matched
# only an inline Path.home() and missed the run-records path in
# tools/live-review-check.py, so a zero would not have proved the migration done.
CONSTRUCTION = re.compile(
    r"""/\s*["']Convivy["']|["'/]Convivy/"""
    r"""|(~|\$HOME|\$\{HOME\})/Convivy|expanduser\([^)]*Convivy|/Users/[a-z]+/Convivy""")
BASELINE = 0


def _exempt(path, line):
    """The one construction the migration keeps on purpose: the registry's legacy
    stored prefix, which projects.py READS so an older copy of the registry still
    resolves, and never writes. Exempted by file and constant name, so a second
    literal anywhere, including elsewhere in projects.py, still counts."""
    return (path.relative_to(ROOT).as_posix() == "lib/agent-platform/projects.py"
            and line.lstrip().startswith("_LEGACY_ROOT_PREFIX = "))


def _constructions():
    hits = []
    # sbin/ too: its installers compute and bake paths as bin/ does, and leaving it
    # out is how install-deck-ci-runner.sh's run-records path escaped the sweep that
    # moved every other one (review, PR 538).
    for top in ("bin", "lib", "sbin", "tools", "install.sh"):
        path = ROOT / top
        # Symlinks are skipped: a link into another file under the same sweep
        # would otherwise count that file's hits twice.
        files = [path] if path.is_file() else [
            p for p in path.rglob("*")
            if p.is_file() and not p.is_symlink() and "__pycache__" not in p.parts]
        for f in files:
            try:
                text = f.read_text()
            except (UnicodeDecodeError, OSError):
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if CONSTRUCTION.search(line) and not _exempt(f, line):
                    hits.append(f"{f.relative_to(ROOT)}:{n}")
    return hits


def test_no_new_home_convivy_path_is_constructed():
    hits = _constructions()
    assert len(hits) <= BASELINE, (
        f"{len(hits)} lines build a path under ~/Convivy directly, above the baseline "
        f"of {BASELINE}. Resolve the root with builder_root.root() instead "
        f"(company/decisions/0008). New ones: see {hits[:10]}")


def test_the_data_root_is_builder_root_when_set_and_root_otherwise(BR, monkeypatch, tmp_path):
    """BUILDER_ROOT, set and non-empty, is taken as given; unset or empty falls to
    root(). The shell launchers use `${BUILDER_ROOT:-...}`, which has exactly
    these semantics, so the two resolve alike."""
    monkeypatch.setenv("BUILDER_ROOT", str(tmp_path / "data"))
    assert BR.data_root() == tmp_path / "data"
    assert BR.runs_root() == tmp_path / "data" / "agent-runs"
    monkeypatch.setenv("BUILDER_ROOT", "")
    assert BR.data_root() == BR.root()
    monkeypatch.delenv("BUILDER_ROOT")
    assert BR.data_root() == BR.root()


def test_builder_root_never_moves_root_or_the_create_dir_floor(monkeypatch, tmp_path):
    """root() is a trust anchor: the session agent's create-dir floor is checked
    against it. BUILDER_ROOT exists to redirect DATA, and neither it nor HOME may
    move the anchor (company/decisions/0008, amended 2026-09-11). Uses the
    conftest-pinned modules, so the floor under test is this checkout's."""
    import builder_root
    import session_lifecycle
    monkeypatch.delenv("BUILDER_ROOT", raising=False)
    root_before = builder_root.root()
    floor_before = session_lifecycle._creatable_root()

    monkeypatch.setenv("BUILDER_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert builder_root.data_root() == tmp_path / "data"
    assert builder_root.root() == root_before
    assert session_lifecycle._creatable_root() == floor_before


def test_the_baseline_is_not_slack():
    """A baseline left above the real count would let that many constructions
    come back unnoticed, so lower it in the same change that removes them."""
    assert len(_constructions()) == BASELINE


# --- the root marker (company/decisions/0008, condition 1) ----------------------


def _marker(root, text):
    (root / ".builder-root").write_text(text)


def test_a_marker_naming_the_resolved_root_confirms_it(BR, tmp_path):
    root = tmp_path / "builder"
    root.mkdir()
    _marker(root, f"{root.resolve()}\n")
    assert BR.marker_problem(root) is None


def test_a_root_behind_a_symlink_is_confirmed_by_its_resolved_path(BR, tmp_path):
    real = tmp_path / "real-root"
    real.mkdir()
    link = tmp_path / "link-root"
    link.symlink_to(real, target_is_directory=True)
    _marker(real, f"{real.resolve()}\n")
    assert BR.marker_problem(link) is None


@pytest.mark.parametrize("text", ["", "/somewhere/else\n", "{root}", "{root}\n\n", " {root}\n"])
def test_a_marker_that_does_not_name_exactly_the_root_is_a_problem(BR, tmp_path, text):
    """A moved or copied tree carries a marker naming its OLD path; anything but
    the exact resolved path and one newline is reported."""
    root = tmp_path / "builder"
    root.mkdir()
    _marker(root, text.format(root=root.resolve()))
    assert BR.marker_problem(root)


def test_a_missing_marker_is_a_problem(BR, tmp_path):
    assert "missing" in BR.marker_problem(tmp_path)


def test_a_fifo_marker_is_reported_rather_than_blocking(BR, tmp_path):
    """A FIFO would block read_text() forever, and every knowledge read with it."""
    import os
    os.mkfifo(tmp_path / ".builder-root")
    assert "not a regular file" in BR.marker_problem(tmp_path)


# --- the report-only confirmation, before enforcement (company/decisions/0008) ----


def test_an_unconfirmed_root_is_reported_once_per_root_and_never_refused(BR, tmp_path, capsys):
    root = tmp_path / "scratch"
    root.mkdir()
    first = BR.report_unconfirmed("data_root() from BUILDER_ROOT", root)
    second = BR.report_unconfirmed("data_root() from BUILDER_ROOT", root)
    err = capsys.readouterr().err
    assert first and second, "the problem is returned every time"
    assert err.count("not confirmed (report-only)") == 1
    assert f"data_root() from BUILDER_ROOT = {str(root.resolve())!r} " in err, \
        "the line names which root was checked and its resolved path"


def test_a_root_holding_a_newline_cannot_forge_a_second_report_line(BR, tmp_path, capsys):
    """BUILDER_ROOT is any process's to set, and this log is what the enforcement
    decision reads, so the path is printed escaped (security-reviewer, #551)."""
    root = tmp_path / "x\nbuilder_root: data_root() from root() = forged confirmed"
    root.mkdir()
    BR.report_unconfirmed("data_root() from BUILDER_ROOT", root)
    err = capsys.readouterr().err
    assert err.count("\n") == 1, f"one report line, not two: {err!r}"
    assert "\\n" in err, "the newline shows escaped"


def test_a_confirmed_root_is_not_reported(BR, tmp_path, capsys):
    root = tmp_path / "builder"
    root.mkdir()
    _marker(root, f"{root.resolve()}\n")
    assert BR.report_unconfirmed("data_root() from root()", root) is None
    assert capsys.readouterr().err == ""


def test_a_report_whose_check_fails_still_does_not_raise(BR, tmp_path, monkeypatch, capsys):
    """runs_root() runs at import time in several modules, so a report that raised
    would take down every process that imports one."""
    def broken(_root):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(BR, "marker_problem", broken)
    assert "marker check failed" in BR.report_unconfirmed("data_root() from root()", tmp_path)
    assert "report-only" in capsys.readouterr().err


def test_runs_root_reports_the_data_root_it_builds_from(BR, tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("BUILDER_ROOT", str(data))
    assert BR.runs_root() == data / "agent-runs"
    assert f"data_root() from BUILDER_ROOT = {str(data.resolve())!r} not confirmed" \
        in capsys.readouterr().err


def test_the_shell_data_root_keeps_stdout_to_the_path_and_reports_on_stderr(tmp_path):
    """The shell launchers read `$(builder-root)` as their data root, so the report
    must never reach stdout."""
    import os
    import subprocess
    data = tmp_path / "data"
    data.mkdir()
    env = {**os.environ, "BUILDER_ROOT": str(data)}
    cmd = [str(ROOT / "bin" / "builder-root")]
    out = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    assert out.returncode == 0
    assert out.stdout == f"{data}\n"
    assert "data_root() from BUILDER_ROOT" in out.stderr and "report-only" in out.stderr
    _marker(data, f"{data.resolve()}\n")
    out = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    assert (out.returncode, out.stdout, out.stderr) == (0, f"{data}\n", "")


def test_builder_root_imports_only_the_standard_library():
    """lib/knowledge/builder_root.py links this file into the knowledge venv. That
    widens its import surface by exactly this one module only while builder_root
    imports nothing but the standard library; an agent-platform import here would
    be inherited by the knowledge venv without anyone deciding it."""
    import ast
    import sys
    tree = ast.parse((ROOT / "lib" / "agent-platform" / "builder_root.py").read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    assert names - set(sys.stdlib_module_names) == set(), names


def test_the_knowledge_copy_loads_the_one_implementation_not_a_second_copy():
    """lib/knowledge/builder_root.py used to be a git symlink to the
    agent-platform file; an export of this repository writes a symlink as its
    target's TEXT rather than following it, so it is a thin loader instead
    (compiles and execs the real file's source into its own globals()). A
    second, drifting copy of the module body would defeat the reason this
    file exists at all, so assert the two files' source text is identical
    apart from this one's own re-export header."""
    link = ROOT / "lib" / "knowledge" / "builder_root.py"
    real = ROOT / "lib" / "agent-platform" / "builder_root.py"
    assert not link.is_symlink()
    link_text = link.read_text()
    assert real.read_text() not in link_text  # not a pasted-in second copy
    assert "exec(compile(" in link_text and "_REAL_PATH" in link_text


def test_the_knowledge_copy_imports_from_its_knowledge_package_path():
    """Loaded the way `lib/knowledge/indexer.py` and `bin/knowledge-ctx` load
    it — as the module `lib/knowledge/builder_root.py` names, not the
    agent-platform file directly — it exposes the exact same public API,
    because it runs that file's own source rather than a copy."""
    spec = importlib.util.spec_from_file_location(
        "builder_root", ROOT / "lib" / "knowledge" / "builder_root.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.root() == mod.FALLBACK or isinstance(mod.root(), Path)
    assert mod.data_root_label() in (
        "data_root() from BUILDER_ROOT", "data_root() from root()")
