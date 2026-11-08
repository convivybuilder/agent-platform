"""co_style_render (CQ-590 / E36 slice 5): render iTerm DynamicProfiles and the
Deck theme seed from the registry's `co_*` fields, with no registered crew,
product, project or organization name in the rendering logic itself — every
registry here is built in the test, with invented names.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "lib" / "agent-platform"


@pytest.fixture
def render():
    spec = importlib.util.spec_from_file_location("co_style_render", LIB / "co_style_render.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _components(profile: dict, key: str) -> tuple[float, float, float, float]:
    c = profile["Profiles"][0][key]
    return (c["Red Component"], c["Green Component"], c["Blue Component"], c["Alpha Component"])


def _hex(r: int, g: int, b: int) -> str:
    return f"#{r:02x}{g:02x}{b:02x}"


def _to_components(hex_color: str, alpha: float = 1.0) -> tuple[float, float, float, float]:
    s = hex_color.lstrip("#")
    r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    return (r / 255, g / 255, b / 255, alpha)


# MARK: - All three colour fields set


def test_a_unit_with_all_three_colour_fields_renders_them_exactly(render):
    unit = {
        "co_accent": "#336699",
        "co_bg": "#f0f0f0",
        "co_term_fg": "#101010",
        "co_selection": "#cccccc",
    }
    profile = render.render_profile("widget", unit)
    assert _components(profile, "Background Color") == _to_components("#f0f0f0")
    assert _components(profile, "Foreground Color") == _to_components("#101010")
    assert _components(profile, "Selection Color") == _to_components("#cccccc")
    assert _components(profile, "Cursor Color") == _to_components("#336699")
    assert _components(profile, "Tab Color") == _to_components("#336699")
    assert _components(profile, "Badge Color") == _to_components("#336699", alpha=0.25)


# MARK: - Only co_accent set: derive the rest


def test_a_unit_with_only_co_accent_gets_derived_colours_computed_by_hand(render):
    unit = {"co_accent": "#336699"}
    profile = render.render_profile("globex", unit)

    # Hand-computed: tint(c, f) = round(c + (255 - c) * f), per channel.
    # accent (0x33, 0x66, 0x99) = (51, 102, 153). Background moves 95% of the
    # way to white (keeps 5% accent); Selection moves 80% (keeps 20% accent).
    bg = _hex(round(51 + (255 - 51) * 0.95), round(102 + (255 - 102) * 0.95),
              round(153 + (255 - 153) * 0.95))
    selection = _hex(round(51 + (255 - 51) * 0.80), round(102 + (255 - 102) * 0.80),
                      round(153 + (255 - 153) * 0.80))
    assert bg == "#f5f7fa"
    assert selection == "#d6e0eb"

    assert _components(profile, "Background Color") == _to_components(bg)
    assert _components(profile, "Selection Color") == _to_components(selection)
    assert _components(profile, "Foreground Color") == _to_components("#1f1f1f")


def test_each_of_the_three_colours_derives_independently(render):
    """A unit that sets one of the three still gets the other two derived —
    it is not an all-or-nothing group."""
    unit = {"co_accent": "#336699", "co_term_fg": "#eeeeee"}
    profile = render.render_profile("acme", unit)
    assert _components(profile, "Foreground Color") == _to_components("#eeeeee")
    assert _components(profile, "Background Color") == _to_components("#f5f7fa")
    assert _components(profile, "Selection Color") == _to_components("#d6e0eb")


@pytest.mark.parametrize("accent", ["#1f1f1f", "#c0392b"])
def test_derived_background_is_light_for_any_accent(render, accent):
    """The derived Background must read light regardless of how dark or
    saturated the accent is — it keeps only 5% of the accent (moves 95% of
    the way to white), so even a near-black or a saturated accent yields a
    background whose every channel is at least 0xE8.

    Hand-computed: tint(c, 0.95) = round(c + (255 - c) * 0.95), per channel.
    #1f1f1f = (31, 31, 31) -> each channel round(31 + 224 * 0.95) = 244 =
    0xf4 -> "#f4f4f4". #c0392b = (192, 57, 43) ->
    round(192 + 63 * 0.95) = 252 = 0xfc,
    round(57 + 198 * 0.95) = 245 = 0xf5,
    round(43 + 212 * 0.95) = 244 = 0xf4 -> "#fcf5f4".
    """
    expected = {"#1f1f1f": "#f4f4f4", "#c0392b": "#fcf5f4"}[accent]
    bg, _fg, _selection = render.derived_colors(accent)
    assert bg == expected
    r, g, b = int(bg[1:3], 16), int(bg[3:5], 16), int(bg[5:7], 16)
    assert min(r, g, b) >= 0xE8


# MARK: - Foreground: co_fg, gated on contrast against the profile's own
# Background; co_term_fg always wins when set and is never contrast-checked.


def test_co_fg_wins_when_co_term_fg_is_unset_and_contrast_is_sufficient(render):
    """Black on the near-white Background derived from this accent reads at
    about 19.9:1 — comfortably over the 4.5:1 floor — so `co_fg` wins."""
    unit = {"co_accent": "#4a90d9", "co_fg": "#000000"}
    bg, _fg, _selection = render.derived_colors("#4a90d9")
    assert render.contrast_ratio("#000000", bg) >= 4.5
    assert render.profile_colors(unit)[1] == "#000000"


def test_co_term_fg_wins_over_co_fg_even_when_co_fg_would_pass_contrast(render):
    """`co_term_fg` is never contrast-checked and always wins over `co_fg`
    when both are set — it is the unit's own choice for this surface."""
    unit = {"co_accent": "#4a90d9", "co_fg": "#000000", "co_term_fg": "#eeeeee"}
    assert render.profile_colors(unit)[1] == "#eeeeee"


def test_a_white_co_fg_on_a_near_white_background_falls_back_to_derived(render):
    """Hand example from the spec: accent #2e7d5b derives a near-white
    Background, so white `co_fg` (sized for contrast against the accent in
    tmux, not against this Background) reads at about 1.1:1 here — under the
    4.5:1 floor — and the derived Foreground wins instead."""
    unit = {"co_accent": "#2e7d5b", "co_fg": "#ffffff"}
    bg, derived_fg, _selection = render.derived_colors("#2e7d5b")
    assert render.contrast_ratio("#ffffff", bg) < 4.5
    assert render.profile_colors(unit)[1] == derived_fg == "#1f1f1f"


def test_no_co_fg_falls_back_to_derived_as_today(render):
    unit = {"co_accent": "#996633"}
    assert render.profile_colors(unit)[1] == "#1f1f1f"


def test_contrast_is_measured_against_an_explicit_co_bg_not_the_derived_background(render):
    """With no `co_bg`, white `co_fg` fails contrast against the derived
    (near-white) Background and falls back to derived. Set an explicit dark
    `co_bg` on the SAME accent/co_fg and the answer flips: white now passes
    against that Background, so `co_fg` wins — proving the check runs
    against the unit's own final Background, not always the derived one."""
    accent, co_fg, explicit_bg = "#4a90d9", "#ffffff", "#202020"
    derived_bg, _derived_fg, _selection = render.derived_colors(accent)
    assert render.contrast_ratio(co_fg, derived_bg) < 4.5
    assert render.contrast_ratio(co_fg, explicit_bg) >= 4.5

    without_co_bg = {"co_accent": accent, "co_fg": co_fg}
    assert render.profile_colors(without_co_bg)[1] != co_fg

    with_co_bg = {"co_accent": accent, "co_bg": explicit_bg, "co_fg": co_fg}
    assert render.profile_colors(with_co_bg)[1] == co_fg


# MARK: - Badge text fallback chain


def test_co_badge_wins_when_set(render):
    unit = {"co_accent": "#336699", "co_badge": "W", "co_label": "Widget Co", "co_style_id": "w"}
    assert render.render_profile("widget", unit)["Profiles"][0]["Badge Text"] == "W"


def test_badge_falls_back_to_co_label_when_co_badge_is_absent(render):
    unit = {"co_accent": "#336699", "co_label": "Widget Co"}
    assert render.render_profile("widget", unit)["Profiles"][0]["Badge Text"] == "Widget Co"


def test_badge_falls_back_to_the_style_id_when_neither_is_set(render):
    unit = {"co_accent": "#336699", "co_style_id": "w"}
    assert render.render_profile("widget", unit)["Profiles"][0]["Badge Text"] == "w"


def test_badge_falls_back_to_the_units_own_slug_with_no_style_id_either(render):
    unit = {"co_accent": "#336699"}
    assert render.render_profile("widget", unit)["Profiles"][0]["Badge Text"] == "widget"


# MARK: - No co_accent: no profile


def test_a_unit_without_co_accent_gets_no_profile(render):
    assert render.render_profile("widget", {"co_label": "Widget Co"}) is None


def test_render_profiles_skips_units_with_no_co_accent(render):
    registry = {
        "widget": {"co_accent": "#336699"},
        "globex": {"co_label": "Globex"},
    }
    profiles = render.render_profiles(registry)
    assert set(profiles) == {"widget"}


def test_render_profiles_skips_sunset_units(render):
    registry = {
        "widget": {"co_accent": "#336699", "lifecycle": "sunset"},
        "globex": {"co_accent": "#996633", "lifecycle": "active"},
    }
    assert set(render.render_profiles(registry)) == {"globex"}


# MARK: - The rendered key is the style id, not the unit's own slug


def test_a_unit_with_a_style_id_renders_under_that_key(render):
    registry = {"widget-secondary-repo": {"co_accent": "#336699", "co_style_id": "widget"}}
    profiles = render.render_profiles(registry)
    assert set(profiles) == {"widget"}
    assert profiles["widget"]["Profiles"][0]["Name"] == "co-widget"
    assert profiles["widget"]["Profiles"][0]["Guid"] == "co-widget"


# MARK: - Guid: the unit's own co_iterm_guid when set, co-<style id> otherwise


def test_guid_falls_back_to_co_style_id_when_co_iterm_guid_is_absent(render):
    unit = {"co_accent": "#336699", "co_style_id": "widget"}
    profile = render.render_profile("widget-secondary-repo", unit)
    assert profile["Profiles"][0]["Guid"] == "co-widget"


def test_co_iterm_guid_wins_when_set(render):
    unit = {"co_accent": "#336699", "co_style_id": "widget", "co_iterm_guid": "co-widget-2026-01-01"}
    profile = render.render_profile("widget-secondary-repo", unit)
    assert profile["Profiles"][0]["Guid"] == "co-widget-2026-01-01"
    assert profile["Profiles"][0]["Name"] == "co-widget"


# MARK: - The Deck theme seed


def test_deck_theme_seed_follows_the_registry_and_leaves_appearance_untouched(render):
    registry = {
        "widget": {"co_accent": "#336699", "co_deck_theme": "azure", "co_label": "Widget Co"},
        "globex": {"co_accent": "#996633", "co_style_id": "g", "co_label": "Globex"},
        "acme": {"co_accent": "#663399"},  # no co_deck_theme, no co_label
    }
    committed = {"appearance": "system"}
    seed = render.render_deck_theme(registry, committed)
    assert seed["appearance"] == "system"
    assert seed["sessions"] == {"widget": "azure"}
    # "globex", not "g": with no `crew` field, the Deck key falls back to the
    # unit's own slug, not its (profile-only) `co_style_id`.
    assert seed["railNames"] == {"widget": "Widget Co", "globex": "Globex"}


# MARK: - The Deck key is the unit's `crew` field, not its `co_style_id`


def test_deck_entries_key_by_crew_while_the_profile_keys_by_style_id(render):
    """A unit whose `crew` (its live tmux session name) differs from its
    `co_style_id` (its iTerm profile name) must render the profile under the
    style id and both Deck entries under `crew`."""
    registry = {
        "widget-secondary-repo": {
            "co_accent": "#336699",
            "co_style_id": "widget",
            "crew": "widget-crew",
            "co_deck_theme": "azure",
            "co_label": "Widget Co",
        },
    }
    profiles = render.render_profiles(registry)
    assert set(profiles) == {"widget"}

    seed = render.render_deck_theme(registry, {"appearance": "system"})
    assert seed["sessions"] == {"widget-crew": "azure"}
    assert seed["railNames"] == {"widget-crew": "Widget Co"}


def test_deck_theme_seed_does_not_mutate_the_committed_document(render):
    registry = {"widget": {"co_accent": "#336699", "co_deck_theme": "azure"}}
    committed = {"appearance": "system"}
    render.render_deck_theme(registry, committed)
    assert committed == {"appearance": "system"}


# MARK: - deck_key_for: tmux_session over crew, for a crew running more than
# one live tmux session (CQ-603 round 1)


def test_deck_key_for_prefers_tmux_session_over_crew(render):
    unit = {"crew": "widget", "tmux_session": "widget-secondary"}
    assert render.deck_key_for("widget-secondary-repo", unit) == "widget-secondary"


def test_deck_key_for_falls_back_to_crew_without_tmux_session(render):
    unit = {"crew": "widget"}
    assert render.deck_key_for("widget-repo", unit) == "widget"


def test_deck_entries_key_by_tmux_session_when_it_differs_from_crew(render):
    """Two units sharing one `crew` but each naming its own `tmux_session`
    (CQ-603) must land under two distinct Deck keys, not collide into one."""
    registry = {
        "widget": {"co_accent": "#336699", "co_deck_theme": "azure",
                    "co_label": "Widget Co", "crew": "widget"},
        "widget-secondary": {"co_accent": "#996633", "co_deck_theme": "indigo",
                              "co_label": "Widget Co Secondary", "crew": "widget",
                              "tmux_session": "widget-secondary"},
    }
    seed = render.render_deck_theme(registry, {"appearance": "system"})
    assert seed["sessions"] == {"widget": "azure", "widget-secondary": "indigo"}
    assert seed["railNames"] == {"widget": "Widget Co", "widget-secondary": "Widget Co Secondary"}


# MARK: - Reproducing a committed profile exactly (synthetic crew)


def test_rendering_reproduces_a_committed_profiles_colours_exactly(render):
    """Fields copied from a hand-written profile (inlined here, for an invented
    crew — never a real one) must render the identical colours, and the
    identical Guid: iTerm identifies a profile by Guid, not Name, so a unit's
    own stable `co_iterm_guid` has to survive the render unchanged."""
    unit = {
        "co_accent": "#4f46e5",
        "co_bg": "#f5f5fd",
        "co_term_fg": "#1f2e3d",
        "co_selection": "#d9d6f7",
        "co_badge": "widget",
        "co_iterm_guid": "co-widget-2026-05-28",
    }
    profile = render.render_profile("widget", unit)["Profiles"][0]
    assert profile["Background Color"] == {
        "Color Space": "sRGB", "Red Component": 0xf5 / 255, "Green Component": 0xf5 / 255,
        "Blue Component": 0xfd / 255, "Alpha Component": 1.0,
    }
    assert profile["Cursor Color"]["Red Component"] == 0x4f / 255
    assert profile["Cursor Color"]["Green Component"] == 0x46 / 255
    assert profile["Cursor Color"]["Blue Component"] == 0xe5 / 255
    assert profile["Selection Color"]["Red Component"] == 0xd9 / 255
    assert profile["Foreground Color"]["Red Component"] == 0x1f / 255
    assert profile["Badge Text"] == "widget"
    assert profile["Guid"] == "co-widget-2026-05-28"
    assert profile["Use Tab Color"] is True
    assert profile["Badge Max Width"] == 0.2
    assert profile["Badge Max Height"] == 0.04
