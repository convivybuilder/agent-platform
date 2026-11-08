"""bin/render-co-style (CQ-590 / E36 slice 5): the I/O wrapper around
co_style_render — writes the generated iTerm profiles and Deck theme seed
atomically, and regenerates both fully on every run. Invented crew names only.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def render_co_style():
    path = REPO / "bin" / "render-co-style"
    loader = importlib.machinery.SourceFileLoader("render_co_style", str(path))
    spec = importlib.util.spec_from_loader("render_co_style", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_render_writes_one_profile_per_styled_unit(render_co_style, tmp_path):
    registry = {"widget": {"co_accent": "#336699"}, "globex": {"co_label": "Globex"}}
    count, out_dir = render_co_style.render(registry, {"appearance": "system"}, tmp_path)
    assert count == 1
    assert (tmp_path / "iterm" / "co-widget.json").exists()
    assert not (tmp_path / "iterm" / "co-globex.json").exists()
    assert out_dir == tmp_path


def test_render_writes_the_deck_theme_seed(render_co_style, tmp_path):
    registry = {"widget": {"co_accent": "#336699", "co_deck_theme": "azure", "co_label": "Widget"}}
    render_co_style.render(registry, {"appearance": "system"}, tmp_path)
    theme = json.loads((tmp_path / "deck" / "theme.json").read_text())
    assert theme["appearance"] == "system"
    assert theme["sessions"] == {"widget": "azure"}
    assert theme["railNames"] == {"widget": "Widget"}


def test_a_unit_removed_from_the_registry_loses_its_stale_profile(render_co_style, tmp_path):
    first = {"widget": {"co_accent": "#336699"}, "globex": {"co_accent": "#996633"}}
    render_co_style.render(first, {"appearance": "system"}, tmp_path)
    assert (tmp_path / "iterm" / "co-widget.json").exists()
    assert (tmp_path / "iterm" / "co-globex.json").exists()

    second = {"widget": {"co_accent": "#336699"}}
    render_co_style.render(second, {"appearance": "system"}, tmp_path)
    assert (tmp_path / "iterm" / "co-widget.json").exists()
    assert not (tmp_path / "iterm" / "co-globex.json").exists()


def test_render_writes_no_leftover_temp_files(render_co_style, tmp_path):
    registry = {"widget": {"co_accent": "#336699", "co_deck_theme": "azure"}}
    render_co_style.render(registry, {"appearance": "system"}, tmp_path)
    leftovers = list(tmp_path.rglob(".*.tmp"))
    assert leftovers == []


def test_a_rerun_replaces_rather_than_appends(render_co_style, tmp_path):
    registry = {"widget": {"co_accent": "#336699", "co_badge": "first"}}
    render_co_style.render(registry, {"appearance": "system"}, tmp_path)
    registry["widget"]["co_badge"] = "second"
    render_co_style.render(registry, {"appearance": "system"}, tmp_path)
    doc = json.loads((tmp_path / "iterm" / "co-widget.json").read_text())
    assert doc["Profiles"][0]["Badge Text"] == "second"


# MARK: - A malformed colour field is skipped, warned about, and does not
# abort the rest of the render — no path that loads the live registry
# (PROJECTS_JSON file or the Library fetch) runs projects.validate_registry
# on it, so a bad value reaches here unchecked (security-reviewer, PR #1066
# round 1).


def test_a_unit_with_a_malformed_colour_is_skipped_with_a_warning(render_co_style, tmp_path, capsys):
    registry = {
        "widget": {"co_accent": "#336699"},
        "globex": {"co_accent": "#zzzzzz"},
    }
    count, _ = render_co_style.render(registry, {"appearance": "system"}, tmp_path)
    assert count == 1
    assert (tmp_path / "iterm" / "co-widget.json").exists()
    assert not (tmp_path / "iterm" / "co-globex.json").exists()
    err = capsys.readouterr().err
    assert "globex" in err
    assert "co_accent" in err


def test_a_unit_with_a_malformed_secondary_colour_is_also_skipped(render_co_style, tmp_path, capsys):
    registry = {"globex": {"co_accent": "#336699", "co_bg": "#1"}}
    count, _ = render_co_style.render(registry, {"appearance": "system"}, tmp_path)
    assert count == 0
    err = capsys.readouterr().err
    assert "globex" in err
    assert "co_bg" in err


def test_a_unit_with_a_malformed_co_fg_is_also_skipped(render_co_style, tmp_path, capsys):
    registry = {"globex": {"co_accent": "#336699", "co_fg": "#1"}}
    count, _ = render_co_style.render(registry, {"appearance": "system"}, tmp_path)
    assert count == 0
    err = capsys.readouterr().err
    assert "globex" in err
    assert "co_fg" in err


# MARK: - A malformed style id or `co_iterm_guid` is skipped the same way: a
# style id becomes the generated file name and the `SetProfile` target, a
# `co_iterm_guid` becomes the profile's `Guid` — `projects.load()` runs
# neither through `projects.validate_registry` either (security-reviewer,
# PR #1066 round 2).


def test_a_unit_whose_style_id_would_escape_the_output_dir_is_skipped(render_co_style, tmp_path, capsys):
    registry = {
        "widget": {"co_accent": "#336699"},
        "globex": {"co_accent": "#996633", "co_style_id": "../escape"},
    }
    count, _ = render_co_style.render(registry, {"appearance": "system"}, tmp_path)
    assert count == 1
    assert (tmp_path / "iterm" / "co-widget.json").exists()
    written = [p for p in tmp_path.rglob("*") if p.is_file() and p.suffix == ".json"]
    assert all(p.resolve().parent == (tmp_path / "iterm").resolve()
               or p == tmp_path / "deck" / "theme.json" for p in written)
    assert not (tmp_path.parent / "escape.json").exists()
    err = capsys.readouterr().err
    assert "globex" in err
    assert "co_style_id" in err


def test_a_slug_fallback_style_id_containing_a_slash_is_skipped(render_co_style, tmp_path, capsys):
    registry = {
        "widget": {"co_accent": "#336699"},
        "weird/slug": {"co_accent": "#996633"},
    }
    count, _ = render_co_style.render(registry, {"appearance": "system"}, tmp_path)
    assert count == 1
    assert (tmp_path / "iterm" / "co-widget.json").exists()
    err = capsys.readouterr().err
    assert "weird/slug" in err
    assert "style id" in err


def test_a_unit_with_a_malformed_iterm_guid_is_skipped(render_co_style, tmp_path, capsys):
    registry = {
        "widget": {"co_accent": "#336699"},
        "globex": {"co_accent": "#996633", "co_iterm_guid": "weird/guid"},
    }
    count, _ = render_co_style.render(registry, {"appearance": "system"}, tmp_path)
    assert count == 1
    assert (tmp_path / "iterm" / "co-widget.json").exists()
    assert not (tmp_path / "iterm" / "co-globex.json").exists()
    err = capsys.readouterr().err
    assert "globex" in err
    assert "co_iterm_guid" in err


# MARK: - A field of the wrong type (null, an int, a list) is skipped the
# same way, rather than raising out of `re.Pattern.match()` and aborting the
# render for every unit — `projects.load()` runs neither path through
# `projects.validate_registry`, so none of the three checks can assume a
# string (PR #1066 round 3).


def test_a_unit_with_a_non_string_style_id_is_skipped_with_a_warning(render_co_style, tmp_path, capsys):
    registry = {
        "widget": {"co_accent": "#336699"},
        "globex": {"co_accent": "#996633", "co_style_id": 7},
    }
    count, _ = render_co_style.render(registry, {"appearance": "system"}, tmp_path)
    assert count == 1
    assert (tmp_path / "iterm" / "co-widget.json").exists()
    assert not (tmp_path / "iterm" / "co-globex.json").exists()
    err = capsys.readouterr().err
    assert "globex" in err
    assert "co_style_id" in err


def test_a_unit_with_a_null_iterm_guid_is_skipped_with_a_warning(render_co_style, tmp_path, capsys):
    registry = {
        "widget": {"co_accent": "#336699"},
        "globex": {"co_accent": "#996633", "co_iterm_guid": None},
    }
    count, _ = render_co_style.render(registry, {"appearance": "system"}, tmp_path)
    assert count == 1
    assert (tmp_path / "iterm" / "co-widget.json").exists()
    assert not (tmp_path / "iterm" / "co-globex.json").exists()
    err = capsys.readouterr().err
    assert "globex" in err
    assert "co_iterm_guid" in err


def test_a_unit_with_a_non_string_colour_field_is_skipped_with_a_warning(render_co_style, tmp_path, capsys):
    registry = {
        "widget": {"co_accent": "#336699"},
        "globex": {"co_accent": 996633},
    }
    count, _ = render_co_style.render(registry, {"appearance": "system"}, tmp_path)
    assert count == 1
    assert (tmp_path / "iterm" / "co-widget.json").exists()
    assert not (tmp_path / "iterm" / "co-globex.json").exists()
    err = capsys.readouterr().err
    assert "globex" in err
    assert "co_accent" in err


# MARK: - A `crew` field of the wrong shape is skipped the same way, rather
# than reaching `co_style_render.deck_key_for`'s use of it as a dict key: a
# list or dict there raises `TypeError: unhashable type` building the Deck
# key, and an int alongside other units' string keys raises later, sorting
# the Deck theme's keys to write (`json.dumps(..., sort_keys=True)`) — in
# both cases nothing is written for ANY unit, not just the malformed one
# (both reviewers, PR #1066 round 4). Both outputs render from the same
# filtered registry (`render()`), so widget's iTerm profile and Deck entry
# both still appear once globex is skipped.


def test_a_unit_with_a_list_crew_is_skipped_with_a_warning(render_co_style, tmp_path, capsys):
    registry = {
        "widget": {"co_accent": "#336699", "co_deck_theme": "azure", "crew": "widget-crew"},
        "globex": {"co_accent": "#996633", "co_deck_theme": "indigo", "crew": ["not", "hashable"]},
    }
    count, _ = render_co_style.render(registry, {"appearance": "system"}, tmp_path)
    assert count == 1
    assert (tmp_path / "iterm" / "co-widget.json").exists()
    assert not (tmp_path / "iterm" / "co-globex.json").exists()
    theme = json.loads((tmp_path / "deck" / "theme.json").read_text())
    assert theme["sessions"] == {"widget-crew": "azure"}
    err = capsys.readouterr().err
    assert "globex" in err
    assert "crew" in err


def test_a_unit_with_an_int_crew_among_string_keys_is_skipped_with_a_warning(
        render_co_style, tmp_path, capsys):
    registry = {
        "widget": {"co_accent": "#336699", "co_deck_theme": "azure", "crew": "widget-crew"},
        "globex": {"co_accent": "#996633", "co_deck_theme": "indigo", "crew": 7},
    }
    count, _ = render_co_style.render(registry, {"appearance": "system"}, tmp_path)
    assert count == 1
    assert (tmp_path / "iterm" / "co-widget.json").exists()
    assert not (tmp_path / "iterm" / "co-globex.json").exists()
    theme = json.loads((tmp_path / "deck" / "theme.json").read_text())
    assert theme["sessions"] == {"widget-crew": "azure"}
    err = capsys.readouterr().err
    assert "globex" in err
    assert "crew" in err


# MARK: - A `tmux_session` field of the wrong shape is skipped the same way
# as `crew` (CQ-603 round 1): `deck_key_for` reaches it as the same dict key


def test_a_unit_with_a_list_tmux_session_is_skipped_with_a_warning(
        render_co_style, tmp_path, capsys):
    registry = {
        "widget": {"co_accent": "#336699", "co_deck_theme": "azure", "crew": "widget"},
        "globex": {"co_accent": "#996633", "co_deck_theme": "indigo", "crew": "globex",
                   "tmux_session": ["not", "hashable"]},
    }
    count, _ = render_co_style.render(registry, {"appearance": "system"}, tmp_path)
    assert count == 1
    assert (tmp_path / "iterm" / "co-widget.json").exists()
    assert not (tmp_path / "iterm" / "co-globex.json").exists()
    theme = json.loads((tmp_path / "deck" / "theme.json").read_text())
    assert theme["sessions"] == {"widget": "azure"}
    err = capsys.readouterr().err
    assert "globex" in err
    assert "tmux_session" in err


# MARK: - sessions and railNames key on tmux_session over crew, end to end
# through render() (CQ-603 round 1)


def test_render_keys_sessions_and_rail_names_by_tmux_session_over_crew(render_co_style, tmp_path):
    registry = {
        "widget": {"co_accent": "#336699", "co_deck_theme": "azure",
                   "co_label": "Widget Co", "crew": "widget"},
        "widget-secondary": {"co_accent": "#996633", "co_deck_theme": "indigo",
                              "co_label": "Widget Co Secondary", "crew": "widget",
                              "tmux_session": "widget-secondary"},
    }
    render_co_style.render(registry, {"appearance": "system"}, tmp_path)
    theme = json.loads((tmp_path / "deck" / "theme.json").read_text())
    assert theme["sessions"] == {"widget": "azure", "widget-secondary": "indigo"}
    assert theme["railNames"] == {"widget": "Widget Co", "widget-secondary": "Widget Co Secondary"}
