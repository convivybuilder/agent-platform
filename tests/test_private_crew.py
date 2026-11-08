"""private_crew — the single answer to "which crew home is this cwd inside", shared
by the hook writer (`session-event-log.py`) and its readers (`session_state.py`,
`tmux_transcript.py`).

The bulk of this logic's behavioral coverage already lives in
`test_session_event_log.py`, which runs it end to end through the hook via
subprocess (symlinks, case-insensitivity, subdirectories, an unreadable
registry, an unusable `cwd`). This file pins the module's OWN public contract
directly — the tri-state return (`path` / `None` / `UNKNOWN`) — so a change to
either consumer's tests can't hide a break here, and so this module has direct
coverage of its own rather than only inherited coverage from one caller.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parent.parent / "lib" / "agent-platform"


@pytest.fixture
def PC():
    spec = importlib.util.spec_from_file_location("private_crew", LIB / "private_crew.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _registry(tmp_path, PC, entries):
    cfg = tmp_path / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    path = cfg / "projects.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    PC._REGISTRY = str(path)
    return path


def test_a_private_crews_cwd_returns_its_home(tmp_path, PC):
    crew = tmp_path / "RIDGE"
    crew.mkdir()
    _registry(tmp_path, PC, {"ridge": {"home_dir": str(crew), "private": True}})
    assert PC.private_home_for(str(crew)) == str(crew)


def test_an_ordinary_crews_cwd_returns_none(tmp_path, PC):
    crew = tmp_path / "RIDGE"
    crew.mkdir()
    ordinary = tmp_path / "finch"
    ordinary.mkdir()
    _registry(tmp_path, PC, {"ridge": {"home_dir": str(crew), "private": True},
                             "finch": {"home_dir": str(ordinary)}})
    assert PC.private_home_for(str(ordinary)) is None


def test_no_private_crews_at_all_returns_none(tmp_path, PC):
    ordinary = tmp_path / "finch"
    ordinary.mkdir()
    _registry(tmp_path, PC, {"finch": {"home_dir": str(ordinary)}})
    assert PC.private_home_for(str(ordinary)) is None


def test_a_subdirectory_of_a_private_crew_still_resolves_to_its_home(tmp_path, PC):
    crew = tmp_path / "RIDGE"
    sub = crew / "statements" / "2026"
    sub.mkdir(parents=True)
    _registry(tmp_path, PC, {"ridge": {"home_dir": str(crew), "private": True}})
    assert PC.private_home_for(str(sub)) == str(crew)


def test_a_symlink_into_a_private_crew_is_still_private(tmp_path, PC):
    """Comparison is by inode, not by string — a string comparison lets a
    symlinked path through (security-reviewer, PR #409, the same bug in
    `bin/co`'s transport wall)."""
    crew = tmp_path / "RIDGE"
    crew.mkdir()
    link = tmp_path / "shortcut"
    link.symlink_to(crew)
    _registry(tmp_path, PC, {"ridge": {"home_dir": str(crew), "private": True}})
    assert PC.private_home_for(str(link)) == str(crew)


def test_an_unreadable_registry_is_unknown_not_none(tmp_path, PC):
    """`UNKNOWN` and `None` are different answers with different consequences
    for each caller — collapsing them was PR #413's leak (run-bb8a514a)."""
    PC._REGISTRY = str(tmp_path / "does-not-exist.json")
    assert PC.private_home_for(str(tmp_path)) is PC.UNKNOWN


@pytest.mark.parametrize("cwd", [None, 12345, ""])
def test_an_unusable_cwd_is_unknown_not_none(tmp_path, PC, cwd):
    _registry(tmp_path, PC, {"ridge": {"home_dir": str(tmp_path / "RIDGE"), "private": True}})
    assert PC.private_home_for(cwd) is PC.UNKNOWN


def test_a_cwd_that_does_not_exist_on_disk_is_unknown(tmp_path, PC):
    crew = tmp_path / "RIDGE"
    crew.mkdir()  # at least one real private home, so `homes` is non-empty and
    # the function reaches the `cwd` existence check rather than short-circuiting
    # on "no private crews registered at all" (test_no_private_crews_at_all_returns_none).
    _registry(tmp_path, PC, {"ridge": {"home_dir": str(crew), "private": True}})
    assert PC.private_home_for(str(tmp_path / "nowhere")) is PC.UNKNOWN


def test_private_homes_only_lists_directories_that_exist(tmp_path, PC):
    """A registry entry naming a moved or deleted crew home must not silently
    make every session look private, nor crash the caller — it just does not
    count."""
    real = tmp_path / "RIDGE"
    real.mkdir()
    _registry(tmp_path, PC, {
        "ridge": {"home_dir": str(real), "private": True},
        "ghost": {"home_dir": str(tmp_path / "gone"), "private": True},
    })
    assert PC.private_homes() == [str(real)]


def test_underscore_keys_are_metadata_not_crews(tmp_path, PC):
    real = tmp_path / "RIDGE"
    real.mkdir()
    _registry(tmp_path, PC, {
        "_meta": {"home_dir": str(tmp_path), "private": True},
        "ridge": {"home_dir": str(real), "private": True},
    })
    assert PC.private_homes() == [str(real)]


# --- the walled-off crew (platform/decisions/0100) mirrors private's own contract --


def test_a_walled_off_crews_cwd_returns_its_home(tmp_path, PC):
    crew = tmp_path / "PARLOR"
    crew.mkdir()
    _registry(tmp_path, PC, {"parlor": {"home_dir": str(crew), "walled_off": True}})
    assert PC.walled_off_home_for(str(crew)) == str(crew)


def test_a_subdirectory_of_the_walled_off_crew_still_resolves_to_its_home(tmp_path, PC):
    crew = tmp_path / "PARLOR"
    sub = crew / "handoffs"
    sub.mkdir(parents=True)
    _registry(tmp_path, PC, {"parlor": {"home_dir": str(crew), "walled_off": True}})
    assert PC.walled_off_home_for(str(sub)) == str(crew)


def test_a_sibling_of_the_walled_off_crew_returns_none(tmp_path, PC):
    crew = tmp_path / "PARLOR"
    crew.mkdir()
    sibling = tmp_path / "PARLOR-notes"
    sibling.mkdir()
    _registry(tmp_path, PC, {"parlor": {"home_dir": str(crew), "walled_off": True}})
    assert PC.walled_off_home_for(str(sibling)) is None


def test_a_private_crew_does_not_answer_the_walled_off_question(tmp_path, PC):
    """`private` and `walled_off` are separate gates (`validate_registry` refuses a
    unit carrying both) — a private crew's home must not also read as walled-off."""
    crew = tmp_path / "RIDGE"
    crew.mkdir()
    _registry(tmp_path, PC, {"ridge": {"home_dir": str(crew), "private": True}})
    assert PC.walled_off_home_for(str(crew)) is None
    assert PC.private_home_for(str(crew)) == str(crew)


def test_an_unreadable_registry_is_unknown_for_walled_off_too(tmp_path, PC):
    PC._REGISTRY = str(tmp_path / "does-not-exist.json")
    assert PC.walled_off_home_for(str(tmp_path)) is PC.UNKNOWN


def test_no_walled_off_crew_registered_returns_none(tmp_path, PC):
    ordinary = tmp_path / "finch"
    ordinary.mkdir()
    _registry(tmp_path, PC, {"finch": {"home_dir": str(ordinary)}})
    assert PC.walled_off_home_for(str(ordinary)) is None


# --- the root the registry and the homes resolve from (company/decisions/0008) ---


def test_a_root_that_cannot_be_resolved_is_unknown(tmp_path, PC, monkeypatch):
    """The steward's condition (a), 2026-09-11, for the HOME-RESOLUTION half.
    `private_home_for` has to turn a registered `home_dir` into an actual
    directory before it can compare `cwd` against it, and that step resolves
    the machine's root — so a root that cannot be resolved leaves the answer
    UNKNOWN, the fail-closed side, never None, even though the registry
    itself (via `<root>/RIDGE`'s own entry) read back without issue."""
    crew = tmp_path / "RIDGE"
    crew.mkdir()
    _registry(tmp_path, PC, {"ridge": {"home_dir": "<root>/RIDGE", "private": True}})

    def unresolvable():
        raise RuntimeError("the machine's root cannot be resolved")
    monkeypatch.setattr(PC.projects, "crews_root", unresolvable)
    assert PC.private_home_for(str(crew)) is PC.UNKNOWN


def test_is_private_slug_needs_no_root_resolution(tmp_path, PC, monkeypatch):
    """`is_private_slug` is a bare lookup against the registry document (CQ·565:
    no longer a `<root>/...`-composed path of its own), so it answers correctly
    even when the machine's root cannot be resolved at all — unlike
    `private_home_for`, which still needs the root to turn `home_dir` into a
    real directory (see the test above)."""
    _registry(tmp_path, PC, {"ridge": {"home_dir": "<root>/RIDGE", "private": True}})

    def unresolvable():
        raise RuntimeError("the machine's root cannot be resolved")
    monkeypatch.setattr(PC.projects, "crews_root", unresolvable)
    assert PC.is_private_slug("ridge") is True
    assert PC.is_private_slug("finch") is False


def test_a_home_the_registry_cannot_place_is_unknown(tmp_path, PC):
    """Neither `~/Convivy/...` nor absolute: private_crew will not guess where a
    black box lives, so it cannot tell."""
    crew = tmp_path / "RIDGE"
    crew.mkdir()
    _registry(tmp_path, PC, {"ridge": {"home_dir": "relative/RIDGE", "private": True}})
    assert PC.private_home_for(str(crew)) is PC.UNKNOWN


def test_a_stored_home_resolves_under_the_machine_root(tmp_path, PC, monkeypatch):
    """A stored `~/Convivy/<rest>` is <root>/<rest>, whatever HOME says: the root is
    moved only by the explicit override, never the environment."""
    root = tmp_path / "root"
    crew = root / "RIDGE"
    crew.mkdir(parents=True)
    monkeypatch.setattr(PC.projects, "ROOT_OVERRIDE", root)
    monkeypatch.setenv("HOME", str(tmp_path / "somewhere-else"))
    _registry(tmp_path, PC, {"ridge": {"home_dir": "~/Convivy/RIDGE", "private": True}})
    assert PC.private_home_for(str(crew)) == str(crew)
