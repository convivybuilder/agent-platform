"""lib/agent-platform/safe_path.py — the shared tool-resolution guard
(platform/decisions/0090's Handoff) every credentialed or guard-deciding
lookup this PR touches goes through: `gh`, `git`, `jq`, `npx` resolution
checks a fixed, ordered list of system directories and never reads `$PATH`
or `$HOME`. See tests/bash/test_safe_path.sh for the bash sibling and
tests/test_shared_safe_path_dirs.py for the cross-language list-equality
check.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

import safe_path as SP


def _plant(dir_path, name):
    dir_path.mkdir(parents=True, exist_ok=True)
    f = dir_path / name
    f.write_text("#!/bin/sh\necho planted\n")
    f.chmod(0o755)
    return f


# --- the fixed list, on this machine today -----------------------------------
#
# No literal path here: a Homebrew install sits at /opt/homebrew/bin on
# Apple Silicon, /usr/local/bin on Intel, and the GitHub Actions macOS
# runner puts `gh` and `npx` somewhere else again (round 13 — the macOS
# runner first ran this suite once the PR left draft, and both literals
# were wrong there). The expectation is computed the same way
# `safe_which` resolves: the first `_system_dirs()` entry holding an
# executable regular file of that name.

@pytest.mark.parametrize("name", ["git", "jq", "gh", "npx"])
def test_the_fixed_list_resolves_each_tool_on_this_machine_today(name):
    expected = None
    for d in SP._system_dirs():
        candidate = os.path.join(d, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            expected = candidate
            break
    if expected is None:
        # ON CI A SKIP IS GREEN. The fixed-list npx case is the only check that
        # setup-node's npx is on the list (ci.yml's npx link step); the check
        # covers every name here, and CI runs all four today.
        if os.environ.get("CI", "").strip().lower() not in ("", "0", "false"):
            pytest.fail(f"{name} is not on any _system_dirs() entry under CI, "
                        "so its fixed-list resolution went unchecked")
        pytest.skip(f"{name} is not on any _system_dirs() entry on this machine")
    assert SP.safe_which(name) == expected


def test_a_name_nothing_provides_resolves_to_none():
    assert SP.safe_which("definitely-not-a-real-binary-xyz") is None


# --- the /opt glob keeps only directories -------------------------------------

def test_system_dirs_opt_glob_excludes_a_regular_file(monkeypatch, tmp_path):
    """`Path("/opt").glob("*/bin")` matches a pattern's final literal component
    by existence only, not directory-ness — a `*/bin` entry that is a regular
    file (not a directory) would still be included in the raw glob results.
    `lib/safe-path.sh`'s loop requires `[ -d "$opt_dir" ]`; `_system_dirs()`
    must apply the same `is_dir()` filter for the two to agree."""
    opt = tmp_path / "opt"
    (opt / "realpkg" / "bin").mkdir(parents=True)
    (opt / "filepkg").mkdir(parents=True)
    (opt / "filepkg" / "bin").write_text("not a directory")
    real_glob = SP.Path.glob  # captured before patching: the `else` branch below
                              # must fall through to the REAL glob, not recurse
                              # into the lambda that's about to replace it.
    monkeypatch.setattr(
        SP.Path, "glob",
        lambda self, pattern: (opt / "realpkg" / "bin", opt / "filepkg" / "bin")
        if str(self) == "/opt" else real_glob(self, pattern))
    dirs = SP._system_dirs()
    assert str(opt / "realpkg" / "bin") in dirs
    assert str(opt / "filepkg" / "bin") not in dirs


# --- the plant: four shapes an agent's own environment fully controls --------
#
# THE PLANT: a dispatched agent can create a directory under /tmp or
# /var/tmp (world-writable, sticky), a directory literally named `worktrees`,
# a directory under its own $HOME, or point a relative PATH entry at its own
# worktree cwd — and set PATH/HOME/cwd to put any of them ahead of a real
# tool. None of PATH, HOME or cwd is ever read by `safe_which`'s default
# (no-override) resolution, so planting in any of them must leave the answer
# unchanged — the real tool if one exists on the fixed list, None otherwise —
# never the plant.

def _dangerous_dir(shape, tmp_path):
    if shape == "tmp":
        return Path(f"/tmp/safe-path-test-plant-{os.getpid()}")
    if shape == "bare_worktrees_dir":
        return tmp_path / "elsewhere" / "worktrees"
    if shape == "under_home":
        return tmp_path / "fake-home" / ".local" / "bin"
    if shape == "relative":
        return tmp_path / "relwork" / "x"
    raise AssertionError(shape)


@pytest.mark.parametrize("shape", ["tmp", "bare_worktrees_dir", "under_home", "relative"])
def test_a_plant_in_a_dangerous_location_is_not_chosen_over_nothing(monkeypatch, tmp_path, shape):
    plant_dir = _dangerous_dir(shape, tmp_path)
    _plant(plant_dir, "definitely-not-a-real-binary-xyz")
    try:
        monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
        if shape == "relative":
            monkeypatch.chdir(plant_dir.parent)
            monkeypatch.setenv("PATH", "x" + os.pathsep + str(plant_dir))
        else:
            monkeypatch.setenv("PATH", str(plant_dir) + os.pathsep + os.environ.get("PATH", ""))
        got = SP.safe_which("definitely-not-a-real-binary-xyz")
        assert got is None, f"a plant in {shape!r} was chosen: {got}"
    finally:
        shutil.rmtree(plant_dir, ignore_errors=True)


@pytest.mark.parametrize("shape", ["tmp", "bare_worktrees_dir", "under_home", "relative"])
def test_a_plant_in_a_dangerous_location_does_not_shadow_the_real_jq(monkeypatch, tmp_path, shape):
    plant_dir = _dangerous_dir(shape, tmp_path)
    _plant(plant_dir, "jq")
    try:
        monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
        if shape == "relative":
            monkeypatch.chdir(plant_dir.parent)
            monkeypatch.setenv("PATH", "x" + os.pathsep + str(plant_dir))
        else:
            monkeypatch.setenv("PATH", str(plant_dir) + os.pathsep + os.environ.get("PATH", ""))
        got = SP.safe_which("jq")
        assert got == "/usr/bin/jq", f"a plant in {shape!r} shadowed the real jq: {got}"
    finally:
        shutil.rmtree(plant_dir, ignore_errors=True)


# --- search_dirs: the test-only override --------------------------------------

def test_search_dirs_override_is_honoured_in_order(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    _plant(first, "gh")
    _plant(second, "gh")
    assert SP.safe_which("gh", search_dirs=[str(first), str(second)]) == str(first / "gh")
    assert SP.safe_which("gh", search_dirs=[str(second), str(first)]) == str(second / "gh")


def test_search_dirs_override_returns_none_when_nothing_matches(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert SP.safe_which("gh", search_dirs=[str(empty)]) is None


def test_the_default_resolution_ignores_a_search_dirs_fixture_left_on_disk(tmp_path):
    """The override is read only when a caller passes it — a planted `gh`
    sitting in an arbitrary tmp_path directory must not leak into the
    production (no-override) call just because the directory exists."""
    plant_dir = tmp_path / "plant"
    _plant(plant_dir, "gh")
    assert SP.safe_which("gh") != str(plant_dir / "gh")
