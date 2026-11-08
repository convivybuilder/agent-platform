"""co_style_render — render iTerm DynamicProfiles and the Deck theme seed from
the registry's `co_*` styling fields (CQ-590 / E36 slice 5).

WHY THIS EXISTS. The Builder release name scan refuses a registered crew,
product, project or organization name in a committed file. `iterm/co-*.json`
(one per crew) and `deck/theme.json`'s `sessions`/`railNames` maps named every
styled crew directly, so they are generated at install time instead, from the
registry, into a directory outside any repository — nothing in this module
writes a file; `bin/render-co-style` does that, from what these functions
return.

THE KEY A RENDERED FILE/ENTRY TAKES — two different keys, for two different
consumers. A unit's iTerm profile file name and its `SetProfile` target take
`co_style_id` when the unit sets one, its own registry slug otherwise
(`style_id_for`) — mirroring `projects.co_style()`, which `bin/co`'s
`_apply_co_style` calls to pick that same name for the escape it sends.

Deck's `sessions` and `railNames` maps take a different key: Deck matches
both against the crew's own LIVE tmux session name, which is the unit's own
`tmux_session` field when it sets one, else its `crew` field — never its
`co_style_id` — a unit `widget-secondary-repo`, for example, runs its live
tmux session as `widget-crew` (its `crew` field) while its `co_style_id` is
`widget`; no session named `widget` exists. A unit whose `crew` runs more
than one tmux session (`ssh_config_render`'s own `tmux_session` field, read
the same way there) needs `tmux_session` to tell its own entry apart from
its crewmate's. `deck_key_for` is the one place that choice is made,
falling back to the slug when neither is set.

A PROFILE'S Guid IS A SEPARATE IDENTITY FROM ITS NAME. iTerm matches a
DynamicProfiles entry across runs by `Guid`, not by `Name` or file path, so
a profile that is regenerated under a changed `Guid` reads to iTerm as a new
profile rather than an update to the old one. A unit's `co_iterm_guid`, when
set, is carried through unchanged; absent, the Guid falls back to the same
`co-<style id>` value the file name and `Name` already use.

COLOUR SOURCES. Cursor, Tab and Badge take `co_accent` directly. Background
and Selection take the optional `co_bg`/`co_selection` hex fields when a unit
has them (loaded from its committed profile, so an existing crew's rendered
profile is colour-identical to today's); otherwise each is derived from
`co_accent` by a fixed rule (the design comment on CQ-590, dated 2026-10-29):
Background keeps 5% of the accent (95% of the way toward white), Selection
keeps 20% of the accent (80% of the way toward white). Background and
Selection derive independently, so a unit that sets one still gets the other
derived.

FOREGROUND is `co_term_fg` when a unit sets it (the terminal's own
foreground, never overridden — a unit that sets it has already chosen it
for this surface); else `co_fg` when it passes a WCAG contrast check against
the profile's own Background (above); else the fixed derived `#1f1f1f`.
`co_fg` is the tmux status bar's text colour on `co_accent`, chosen for
contrast against the accent, not against this profile's near-white
Background — a unit can set an accent dark enough to want a light `co_fg`
(e.g. white) that then reads as 1.1:1 on its own near-white terminal
Background and would be unreadable there. The contrast check exists to catch
exactly that mismatch; it never runs against `co_term_fg`, which a unit sets
for this surface specifically.

BADGE TEXT falls back `co_badge` -> `co_label` -> the unit's style id (not its
own slug, for the same reason as above — a unit whose style id differs from
its slug has no committed badge text to fall back to that names its slug).
"""
from __future__ import annotations

from typing import Any

# Each fraction below is how far toward white `tint_toward_white` moves the
# accent, NOT how much accent the result keeps — a value near 1.0 keeps only
# a little accent and reads as a light background; a value near 0.0 keeps
# almost all of it and reads dark or saturated, which is unreadable under
# the fixed dark Foreground below. Background keeps 5% of the accent (moves
# 95% of the way to white); Selection keeps 20% (moves 80% of the way),
# staying stronger than Background but still legible against the derived
# foreground. Matches how every committed profile's own Background/Selection
# reads next to its Cursor color.
_BACKGROUND_TOWARD_WHITE = 0.95
_SELECTION_TOWARD_WHITE = 0.80
_DERIVED_FOREGROUND = "#1f1f1f"

# WCAG 2.x minimum contrast ratio for normal text — below this, `co_fg` (sized
# for contrast against `co_accent` in the tmux status bar, not against this
# profile's own Background) is unreadable here and the derived Foreground
# wins instead.
_MIN_FG_CONTRAST = 4.5

_BADGE_ALPHA = 0.25
_SEMANTIC_HISTORY = {
    "action": "raw command",
    "text": "$HOME/bin/open-md-link \\1 \\2",
}


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    s = value.lstrip("#")
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#" + "".join(f"{c:02x}" for c in rgb)


def tint_toward_white(hex_color: str, fraction: float) -> str:
    """`hex_color` moved `fraction` of the way to white, per channel."""
    r, g, b = _hex_to_rgb(hex_color)
    return _rgb_to_hex(tuple(round(c + (255 - c) * fraction) for c in (r, g, b)))


def _linearize(channel: int) -> float:
    c = channel / 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _relative_luminance(hex_color: str) -> float:
    r, g, b = _hex_to_rgb(hex_color)
    return 0.2126 * _linearize(r) + 0.7152 * _linearize(g) + 0.0722 * _linearize(b)


def contrast_ratio(hex_a: str, hex_b: str) -> float:
    """WCAG 2.x contrast ratio between two hex colours (>= 1.0, 21.0 at most,
    for black against white)."""
    la, lb = _relative_luminance(hex_a), _relative_luminance(hex_b)
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


def _color_component(hex_color: str, *, alpha: float = 1.0) -> dict[str, Any]:
    r, g, b = _hex_to_rgb(hex_color)
    return {
        "Color Space": "sRGB",
        "Red Component": r / 255,
        "Green Component": g / 255,
        "Blue Component": b / 255,
        "Alpha Component": alpha,
    }


def style_id_for(slug: str, unit: dict[str, Any]) -> str:
    """The key a unit's profile file and iTerm `SetProfile` target take —
    `co_style_id` when the unit sets one, its own registry slug otherwise.
    Mirrors `projects.co_style()`. Not the key Deck's `sessions`/`railNames`
    entries take — see `deck_key_for`."""
    return unit.get("co_style_id") or slug


def deck_key_for(slug: str, unit: dict[str, Any]) -> str:
    """The key a unit's Deck `sessions`/`railNames` entry takes — the unit's
    own `tmux_session` field (the actual live tmux session name, when it
    differs from `crew`) when set, else its `crew` field, else its own
    registry slug. Unlike `style_id_for`, never `co_style_id`: a unit's
    `co_style_id` can name an iTerm profile with no live tmux session behind
    it."""
    return unit.get("tmux_session") or unit.get("crew") or slug


def badge_text_for(slug: str, unit: dict[str, Any]) -> str:
    return unit.get("co_badge") or unit.get("co_label") or style_id_for(slug, unit)


def derived_colors(accent: str) -> tuple[str, str, str]:
    """(background, foreground, selection) derived from `accent` alone, by the
    fixed rule a unit with no `co_bg`/`co_term_fg`/`co_selection` of its own
    falls back to."""
    return (
        tint_toward_white(accent, _BACKGROUND_TOWARD_WHITE),
        _DERIVED_FOREGROUND,
        tint_toward_white(accent, _SELECTION_TOWARD_WHITE),
    )


def profile_colors(unit: dict[str, Any]) -> tuple[str, str, str]:
    """(background, foreground, selection) hex for `unit`. Background and
    Selection: the unit's own field when set, derived from `co_accent`
    otherwise — independent of each other, not an all-or-nothing group.
    Foreground: `co_term_fg` when set (never overridden); else `co_fg` when
    it meets `_MIN_FG_CONTRAST` against this unit's own Background (`co_fg`
    is sized for contrast against `co_accent`, not against Background, so it
    can fail here even though it reads fine in the tmux status bar); else the
    derived Foreground."""
    accent = unit["co_accent"]
    derived_bg, derived_fg, derived_selection = derived_colors(accent)
    bg = unit.get("co_bg") or derived_bg
    term_fg = unit.get("co_term_fg")
    co_fg = unit.get("co_fg")
    if term_fg:
        fg = term_fg
    elif co_fg and contrast_ratio(co_fg, bg) >= _MIN_FG_CONTRAST:
        fg = co_fg
    else:
        fg = derived_fg
    return (
        bg,
        fg,
        unit.get("co_selection") or derived_selection,
    )


def render_profile(slug: str, unit: dict[str, Any]) -> dict[str, Any] | None:
    """The DynamicProfiles document for one unit, or None when it carries no
    `co_accent` (nothing to style with). Carries every key a committed
    `iterm/co-*.json` profile carries today, so a unit whose colour fields are
    loaded from its old file renders the identical profile. Foreground comes
    from `profile_colors`: `co_term_fg` when set, else `co_fg` when it is
    readable (WCAG contrast >= `_MIN_FG_CONTRAST`) against this unit's own
    Background, else the derived Foreground — `co_fg` is picked for contrast
    against `co_accent` in the tmux status bar, not against this profile's
    near-white terminal Background, so the check exists to catch a `co_fg`
    that is unreadable there even though it is fine in tmux."""
    accent = unit.get("co_accent")
    if not accent:
        return None
    style_id = style_id_for(slug, unit)
    bg, fg, selection = profile_colors(unit)
    name = f"co-{style_id}"
    guid = unit.get("co_iterm_guid") or name
    return {
        "Profiles": [
            {
                "Name": name,
                "Guid": guid,
                "Dynamic Profile Parent Name": "Default",
                "Semantic History": dict(_SEMANTIC_HISTORY),
                "Background Color": _color_component(bg),
                "Foreground Color": _color_component(fg),
                "Cursor Color": _color_component(accent),
                "Selection Color": _color_component(selection),
                "Tab Color": _color_component(accent),
                "Use Tab Color": True,
                "Badge Color": _color_component(accent, alpha=_BADGE_ALPHA),
                "Badge Text": badge_text_for(slug, unit),
                "Badge Max Width": 0.2,
                "Badge Max Height": 0.04,
            }
        ]
    }


def _styled_units(registry: dict[str, Any]):
    for slug, unit in registry.items():
        if not isinstance(slug, str) or slug.startswith("_") or not isinstance(unit, dict):
            continue
        if unit.get("lifecycle") not in (None, "active"):
            continue
        yield slug, unit


def render_profiles(registry: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """{style_id: profile-document} for every active unit carrying `co_accent`.
    `style_id` is the bare key (e.g. `deck`, not `co-deck`) — `bin/render-co-
    style` prefixes it to name the file."""
    out: dict[str, dict[str, Any]] = {}
    for slug, unit in _styled_units(registry):
        profile = render_profile(slug, unit)
        if profile is not None:
            out[style_id_for(slug, unit)] = profile
    return out


def render_deck_theme(registry: dict[str, dict[str, Any]],
                       committed: dict[str, Any]) -> dict[str, Any]:
    """`committed` (today's `deck/theme.json`, carrying only non-per-crew keys
    such as `appearance`) plus `sessions` and `railNames` rendered from the
    registry: `sessions[deck_key] = co_deck_theme`, `railNames[deck_key] =
    co_label`, for every active unit that sets the field — `deck_key` the
    unit's own `tmux_session` field when set, else its `crew` field
    (`deck_key_for`), never its `co_style_id`, since Deck matches both maps
    against the live tmux session name. A unit with
    neither gets no entry in either map, the same as it has none in the
    committed file until an operator or the Library sets one."""
    sessions: dict[str, str] = {}
    rail_names: dict[str, str] = {}
    for slug, unit in _styled_units(registry):
        deck_key = deck_key_for(slug, unit)
        deck_theme = unit.get("co_deck_theme")
        if deck_theme:
            sessions[deck_key] = deck_theme
        label = unit.get("co_label")
        if label:
            rail_names[deck_key] = label
    seed = dict(committed)
    seed["sessions"] = sessions
    seed["railNames"] = rail_names
    return seed
