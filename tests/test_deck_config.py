"""Seeding Deck's config without discarding the user's.

THE BUG THESE PIN. Seeding only when the FILE was absent meant one Cmd-minus in
Deck created the file and locked the user out of Convivy's mapping forever —
observed as the operator's Hollis rendering blue where iTerm renders pink. The unit of
"already present" is a key, not a file, so the tests that matter are the ones
where a partial config exists: the user's entries survive and only the gaps fill.

The other half is drift. The rail's working directories are derived from the ssh
aliases rather than restated, and a test asserts they agree, because two copies
of the same list is how the rail ends up pointing somewhere the alias does not.
"""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "lib" / "agent-platform"
# A synthetic rendering, not the installer script itself (excluded from the
# Builder export): deck_config parses a rendered ssh config, so the fixture
# carries the same `tmux new -s <session> -c <workdir>` shape, with its own
# placeholders rendered the same way the installer renders them at install time.
SSH_SCRIPT = REPO / "tests" / "fixtures" / "deck_mini_tmux.conf"
FAKE_MINI_ROOT = "/opt/fakeroot/builder"
FAKE_STANDBY_ROOT = "/opt/fakeroot/macmini-builder"


def _script_text() -> str:
    return (SSH_SCRIPT.read_text()
            .replace("@MINI_ROOT@", FAKE_MINI_ROOT)
            .replace("@STANDBY_ROOT@", FAKE_STANDBY_ROOT))
THEME_JSON = REPO / "deck" / "theme.json"


@pytest.fixture
def DC():
    spec = importlib.util.spec_from_file_location("deck_config", LIB / "deck_config.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# MARK: - The merge


def test_an_absent_key_is_filled(DC):
    merged, filled = DC.merge({}, {"appearance": "system"})
    assert merged["appearance"] == "system"
    assert filled == ["appearance"]


def test_a_key_the_user_set_is_never_touched(DC):
    merged, filled = DC.merge({"appearance": "dark"}, {"appearance": "system"})
    assert merged["appearance"] == "dark"
    assert filled == []


def test_an_empty_value_is_a_choice_and_survives(DC):
    """`rail: []` is a user saying they want no fleet rows. Refilling it would
    override a deliberate choice with a default."""
    merged, filled = DC.merge({"rail": []}, {"rail": [{"session": "deck"}]})
    assert merged["rail"] == []
    assert filled == []


def test_session_themes_merge_one_level_down(DC):
    """The whole reason this module exists: a user with three themes picked
    keeps those three and gains the rest."""
    merged, filled = DC.merge(
        {"sessions": {"vivy": "rose"}},
        {"sessions": {"vivy": "azure", "deck": "gold", "hollis": "rose"}},
    )
    assert merged["sessions"]["vivy"] == "rose", "the user's own pick must win"
    assert merged["sessions"]["deck"] == "gold"
    assert filled == ["sessions.deck", "sessions.hollis"]


def test_the_reported_bug_a_file_with_no_sessions_block(DC):
    """The operator's actual file: created by Deck on a font-size change, carrying
    typography and nothing else. It must gain the mapping."""
    existing = {"terminal": {"fontSize": 13.0}, "appearance": "system"}
    merged, filled = DC.merge(existing, {"sessions": {"hollis": "rose"}, "appearance": "light"})
    assert merged["sessions"] == {"hollis": "rose"}
    assert merged["terminal"] == {"fontSize": 13.0}, "typography is untouched"
    assert merged["appearance"] == "system", "their appearance wins"
    assert filled == ["sessions"]


def test_merging_is_idempotent(DC):
    seed = {"sessions": {"deck": "gold"}, "appearance": "system"}
    once, _ = DC.merge({}, seed)
    twice, filled = DC.merge(once, seed)
    assert twice == once
    assert filled == []


# MARK: - The rail, derived from the aliases


def test_rail_is_derived_from_the_tmux_create_clause(DC):
    text = """
    RemoteCommand printf 'x'; tmux attach -d -t vivy || tmux new -s vivy -c /Users/example/Convivy/vivy
    RemoteCommand printf 'x'; tmux attach -d -t deck || tmux new -s deck -c /Users/example/Convivy/convivy-deck
    """
    rail = DC.rail_from_ssh_config(text)
    assert [r["session"] for r in rail] == ["vivy", "deck"]
    assert rail[0]["workdir"] == "/Users/example/Convivy/vivy"


def test_rail_keeps_the_workdir_when_the_create_is_grouped_behind_the_server_check(DC):
    """The aliases run `session-agent ensure-server && (attach || new -c DIR)`.
    The `)` closing that group is not part of the workdir: read into it, the
    workdir fails `_SAFE_WORKDIR`, and the rail silently loses the directory."""
    text = ("  RemoteCommand printf 'x'; ~/.local/bin/session-agent ensure-server && "
            "(tmux attach -d -t vivy || tmux new -s vivy -c /Users/example/Convivy/vivy)\n")
    rail = DC.rail_from_ssh_config(text)
    assert rail == [{"session": "vivy", "workdir": "/Users/example/Convivy/vivy"}]


def test_rail_keeps_ssh_config_order(DC):
    """Order is load-bearing — the rail sorts stably so muscle memory survives."""
    text = "tmux new -s c -c /c\ntmux new -s a -c /a\ntmux new -s b -c /b"
    assert [r["session"] for r in DC.rail_from_ssh_config(text)] == ["c", "a", "b"]


def test_a_repeated_session_keeps_its_first_position(DC):
    text = "tmux new -s a -c /a\ntmux new -s b -c /b\ntmux new -s a -c /other"
    rail = DC.rail_from_ssh_config(text)
    assert [r["session"] for r in rail] == ["a", "b"]
    assert rail[0]["workdir"] == "/a"


def test_a_display_name_equal_to_the_session_is_omitted(DC):
    """Deck falls back to the session name, so writing it twice is noise."""
    rail = DC.rail_from_ssh_config("tmux new -s deck -c /d", {"deck": "deck"})
    assert "name" not in rail[0]
    rail = DC.rail_from_ssh_config("tmux new -s svc -c /s", {"svc": "Convivy Kit"})
    assert rail[0]["name"] == "Convivy Kit"


# MARK: - No drift between the rail and the aliases


def test_every_alias_reaches_the_rail():
    """Two copies of one list is how the rail ends up pointing somewhere the
    alias does not. The derivation is the guard; this asserts it holds against
    the real script."""
    # The rail is built from the whole file, as the installer builds it, so every
    # block is checked, including any appended after macmini. Deck keeps a session's
    # first row, so the expected rows are each session's first alias. macmini-steward
    # repeats the crew's `steward` session and is correctly not a row.
    text = _script_text()
    # Same workdir class as deck_config._TMUX_NEW: stops before the `)` that
    # closes the ensure-server group.
    first_alias = {}
    for session, workdir in re.findall(r"tmux\s+new\s+-s\s+([A-Za-z0-9._-]+)\s+-c\s+([^\s)]+)", text):
        first_alias.setdefault(session, workdir)
    assert first_alias, "the ssh config should declare sessions with working directories"

    spec = importlib.util.spec_from_file_location("deck_config", LIB / "deck_config.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rail = mod.rail_from_ssh_config(text)
    # r.get: a row whose workdir the rail rejected has none, and must not match.
    assert {(r["session"], r.get("workdir")) for r in rail} == set(first_alias.items())


def test_every_rail_session_is_a_valid_tmux_name():
    """A rail row that cannot become valid ssh argv is a bug, not an edge case.
    Deck drops such a row on parse, so it would vanish silently."""
    spec = importlib.util.spec_from_file_location("deck_config", LIB / "deck_config.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rail = mod.rail_from_ssh_config(_script_text())
    for row in rail:
        assert re.fullmatch(r"[A-Za-z0-9._-]{1,64}", row["session"]), row
        assert row["workdir"].startswith("/"), row
        assert ".." not in row["workdir"], row


def test_shipped_theme_json_parses_and_carries_rail_names():
    doc = json.loads(THEME_JSON.read_text())
    assert isinstance(doc.get("sessions"), dict)
    assert isinstance(doc.get("railNames"), dict)


# MARK: - The installer's entry point


def test_seed_document_folds_the_rail_in_and_drops_rail_names(DC):
    """railNames labels rail rows and would be meaningless in the installed
    file, so it is consumed rather than passed through."""
    seed = DC.seed_document(THEME_JSON.read_text(), _script_text())
    assert "railNames" not in seed
    assert seed["rail"], "the shipped config should seed a rail"
    assert any(r.get("name") == "Convivy Kit" for r in seed["rail"])


def test_merge_file_round_trips_and_sorts(DC):
    out, filled = DC.merge_file(None, THEME_JSON.read_text(), _script_text())
    doc = json.loads(out)
    assert doc["rail"] and doc["sessions"]
    assert out == json.dumps(doc, indent=2, sort_keys=True) + "\n"
    assert "rail" in filled and "sessions" in filled


def test_an_unparseable_existing_file_is_treated_as_absent(DC):
    """Refusing to install because the user hand-edited their config into
    invalid JSON helps nobody, and Deck itself degrades a malformed config to
    defaults rather than failing."""
    out, filled = DC.merge_file("{not json", THEME_JSON.read_text(), _script_text())
    assert json.loads(out)["sessions"]
    assert "sessions" in filled


def test_a_non_object_existing_file_is_treated_as_absent(DC):
    out, _ = DC.merge_file("[1,2,3]", THEME_JSON.read_text(), _script_text())
    assert isinstance(json.loads(out), dict)


# MARK: - Not mutating the caller's config


def test_merge_does_not_mutate_its_input(DC):
    """A shallow dict() copy leaves the inner dict aliased, so the nested merge
    would quietly populate the caller's own config. merge_file happens to let
    `existing` go out of scope, which is what would hide this."""
    existing = {"sessions": {"vivy": "rose"}}
    before = json.dumps(existing, sort_keys=True)
    DC.merge(existing, {"sessions": {"deck": "gold"}})
    assert json.dumps(existing, sort_keys=True) == before


def test_merge_does_not_mutate_its_seed(DC):
    seed = {"sessions": {"deck": "gold"}}
    before = json.dumps(seed, sort_keys=True)
    DC.merge({"sessions": {"vivy": "rose"}}, seed)
    assert json.dumps(seed, sort_keys=True) == before


# MARK: - What a re-install does, and does not, do


def test_a_second_install_leaves_an_existing_rail_alone(DC):
    """The limitation, asserted rather than only described. A rail is ordered and
    curated, and once the key exists nothing here can tell "never customised"
    from "deliberately removed rows". Re-adding a deleted row is the worse
    error."""
    theme, ssh = THEME_JSON.read_text(), _script_text()
    first, _ = DC.merge_file(None, theme, ssh)
    expanded = ssh + "\n  RemoteCommand tmux attach -d -t brand-new || tmux new -s brand-new -c /Users/example/new\n"

    second, filled = DC.merge_file(first, theme, expanded)
    sessions_on_rail = {r["session"] for r in json.loads(second)["rail"]}
    assert "brand-new" not in sessions_on_rail
    assert filled == [], "nothing should be filled on a clean re-install"


def test_deleting_the_rail_key_is_the_documented_refresh(DC):
    """The escape hatch the docs promise has to actually work."""
    theme, ssh = THEME_JSON.read_text(), _script_text()
    first, _ = DC.merge_file(None, theme, ssh)
    expanded = ssh + "\n  RemoteCommand tmux attach -d -t brand-new || tmux new -s brand-new -c /Users/example/new\n"

    stripped = json.loads(first)
    del stripped["rail"]
    refreshed, filled = DC.merge_file(json.dumps(stripped), theme, expanded)
    assert "brand-new" in {r["session"] for r in json.loads(refreshed)["rail"]}
    assert filled == ["rail"]


def test_a_new_session_theme_DOES_arrive_on_a_re_install(DC):
    """`sessions` is nested-merged, so unlike the rail it does track additions.
    The two keys behave differently on purpose; this pins the difference."""
    theme, ssh = THEME_JSON.read_text(), _script_text()
    first, _ = DC.merge_file(None, theme, ssh)
    grown = json.loads(theme)
    grown["sessions"]["brand-new"] = "teal"

    second, filled = DC.merge_file(first, json.dumps(grown), ssh)
    assert json.loads(second)["sessions"]["brand-new"] == "teal"
    assert filled == ["sessions.brand-new"]


# MARK: - The workdir guard, at extraction


def test_a_workdir_with_shell_metacharacters_is_dropped(DC):
    """The workdir is interpolated into a remote shell command by Deck. The
    guard lives here rather than in the caller, since the function is public and
    a future caller may point it at a user-editable ssh config."""
    for bad in ("/tmp;rm", "/$(whoami)", "/a`id`", "/a|b", "/a&b", "relative/path", "/a/../etc"):
        rail = DC.rail_from_ssh_config(f"tmux new -s x -c {bad}")
        assert rail[0]["session"] == "x", "the row survives"
        assert "workdir" not in rail[0], f"{bad} should not reach the config"


def test_a_legitimate_workdir_survives_the_guard(DC):
    for good in ("/Users/example/Convivy/vivy", "~/work/x", "/Users/example/nyh-architecture/nyh-etc"):
        rail = DC.rail_from_ssh_config(f"tmux new -s x -c {good}")
        assert rail[0]["workdir"] == good


def test_the_guard_mirrors_decks_own_validator(DC):
    """A value Deck would reject must never be written, or the rail silently
    loses directories on read. Every shipped workdir must clear it."""
    rail = DC.rail_from_ssh_config(_script_text())
    assert all("workdir" in r for r in rail), "no shipped alias should fail the guard"
