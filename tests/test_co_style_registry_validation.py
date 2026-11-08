"""`projects.validate_registry`'s checks on the CQ-590 / E36 slice 5 fields
this PR touches: the tightened `#rrggbb` colour shape and the new
`co_iterm_guid` field (round 1 of PR #1066). `co_style_render.py`'s own test
file (`tests/test_co_style_render.py`) covers how these fields render;
this file covers only whether `validate_registry` admits or refuses them.
Invented crew names only.
"""
from __future__ import annotations

import pytest

import projects


def _valid_doc(**overrides) -> dict:
    """A minimal, schema-valid registry document, mirroring
    `tests/test_projects.py`'s own `_valid_doc` — kept here rather than
    imported so this file's gate does not depend on that one's fixture
    shape."""
    doc = {
        "widget": {
            "class": "project", "display": "Widget", "owner": "test",
            "home_dir": "<root>/widget", "dirs": [], "crew": "widget",
            "lifecycle": "active", "products": [], "repos": ["widget"],
            "aliases": [], "kb": "local", "task_text_public": False,
            "overnight": False,
        },
    }
    doc.update(overrides)
    return doc


# --- the tightened colour shape ---------------------------------------------


@pytest.mark.parametrize("color_field", ("co_accent", "co_bg", "co_term_fg", "co_selection", "co_fg"))
def test_validate_registry_refuses_a_non_hex_colour(color_field):
    doc = _valid_doc()
    doc["widget"][color_field] = "#zzzzzz"
    with pytest.raises(ValueError, match=f"{color_field} .* must be a '#rrggbb'-shaped string"):
        projects.validate_registry(doc)


@pytest.mark.parametrize("color_field", ("co_accent", "co_bg", "co_term_fg", "co_selection", "co_fg"))
def test_validate_registry_refuses_a_too_short_colour(color_field):
    doc = _valid_doc()
    doc["widget"][color_field] = "#1"
    with pytest.raises(ValueError, match=f"{color_field} .* must be a '#rrggbb'-shaped string"):
        projects.validate_registry(doc)


@pytest.mark.parametrize("color_field", ("co_accent", "co_bg", "co_term_fg", "co_selection", "co_fg"))
def test_validate_registry_admits_a_well_formed_colour(color_field):
    doc = _valid_doc()
    doc["widget"][color_field] = "#336699"
    assert projects.validate_registry(doc) is None


# --- co_badge (non-empty string, same rule as co_label/co_deck_theme) ------


def test_validate_registry_refuses_an_empty_co_badge():
    doc = _valid_doc()
    doc["widget"]["co_badge"] = ""
    with pytest.raises(ValueError, match="co_badge must be a non-empty string"):
        projects.validate_registry(doc)


def test_validate_registry_admits_a_well_formed_co_badge():
    doc = _valid_doc()
    doc["widget"]["co_badge"] = "W"
    assert projects.validate_registry(doc) is None


# --- co_iterm_guid: the same iTerm-profile/tmux-safe name rule as co_style_id


def test_validate_registry_refuses_a_co_iterm_guid_with_a_slash():
    doc = _valid_doc()
    doc["widget"]["co_iterm_guid"] = "co-widget/2026-05-28"
    with pytest.raises(ValueError, match="co_iterm_guid .* must match"):
        projects.validate_registry(doc)


def test_validate_registry_admits_a_well_formed_co_iterm_guid():
    doc = _valid_doc()
    doc["widget"]["co_iterm_guid"] = "co-widget-2026-05-28"
    assert projects.validate_registry(doc) is None
