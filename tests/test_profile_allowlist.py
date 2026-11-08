"""profile_allowlist() (CQ-587: moved off knowledge-ctx's own hardcoded
`_PROFILE_ALLOWLIST` dict, onto each unit's own `kb_profile_allowlist` field).

Split out of `tests/test_projects.py` rather than appended there: another
open PR also appends tests at that file's end, and two PRs each adding
lines at the same spot in one file conflict on merge. Every test here either
monkeypatches `projects.load`/`projects._load_raw` directly or builds its own
registry document, so none of it needs `conftest.py`'s
`_default_projects_registry` fixture or the `rich_registry` marker.
"""
from __future__ import annotations

import pytest

import projects
from test_projects import _valid_doc


def test_profile_allowlist_reads_a_units_kb_profile_allowlist_field(monkeypatch):
    """A unit carrying `kb_profile_allowlist` restricts its own directory (keyed
    on `home_dir`'s basename, not the slug); a unit without the field is absent
    from the mapping, which `knowledge-ctx.allowed_profiles` reads as
    unrestricted. Break this by making the loop skip a falsy-but-present
    value differently (e.g. `if names is None`) and a unit with
    `kb_profile_allowlist: []` would wrongly appear restricted-to-nothing
    instead of absent."""
    monkeypatch.setattr(projects, "load", lambda: {
        "widget-crew": {
            "home_dir": "<root>/widget-crew",
            "kb_profile_allowlist": ["widget-daytime"],
        },
        "globex": {
            "home_dir": "<root>/globex",
        },
    })
    allowlist = projects.profile_allowlist()
    assert allowlist == {"widget-crew": {"widget-daytime"}}
    assert "globex" not in allowlist


def test_profile_allowlist_keys_on_the_home_dir_basename_not_the_slug(monkeypatch):
    """The allowlist is compared against `detect_product`'s own DIRECTORY
    result, never the registry slug — the same split `default_profile_refused`
    and `_private_crew_names` already make for a private crew's name set.
    Guards a key built from `slug` instead of `home_dir`'s basename."""
    monkeypatch.setattr(projects, "load", lambda: {
        "widget-crew-slug": {
            "home_dir": "<root>/Widget-Crew",
            "kb_profile_allowlist": ["widget-daytime"],
        },
    })
    allowlist = projects.profile_allowlist()
    assert allowlist == {"Widget-Crew": {"widget-daytime"}}
    assert "widget-crew-slug" not in allowlist


def test_profile_allowlist_raises_on_an_unreadable_registry(monkeypatch):
    """A profile allowlist is a security control (platform/decisions/0043):
    an unreadable registry must refuse, never report every directory
    unrestricted. A hardcoded dict literal — what this replaced — could
    never exhibit this failure at all; moving the data into the registry
    introduces exactly the failure mode this test guards against. Break it
    by wrapping the loop in `try/except Exception: return {}` and this
    test fails because an empty mapping reads as "no unit is restricted"
    rather than propagating the outage."""
    def _boom():
        raise projects.RegistryUnavailable("stub: the Library is down")
    monkeypatch.setattr(projects, "_load_raw", _boom)
    with pytest.raises(projects.RegistryUnavailable):
        projects.profile_allowlist()


def test_validate_registry_refuses_an_empty_kb_profile_allowlist():
    """Schema check: a non-empty list of non-empty strings, or absent — the
    same shape `cost_fold_aliases` enforces just above it. Break the `if`
    branch's `isinstance(allowed, list) and allowed` into `isinstance(allowed,
    list)` alone and an empty list stops being reported."""
    doc = _valid_doc()
    doc["widget"]["kb_profile_allowlist"] = []
    with pytest.raises(ValueError, match="kb_profile_allowlist must be a non-empty list"):
        projects.validate_registry(doc)


def test_validate_registry_refuses_a_non_string_kb_profile_allowlist_entry():
    doc = _valid_doc()
    doc["widget"]["kb_profile_allowlist"] = ["widget-daytime", ""]
    with pytest.raises(ValueError, match="kb_profile_allowlist entries must be non-empty"):
        projects.validate_registry(doc)


def test_validate_registry_admits_a_well_formed_kb_profile_allowlist():
    doc = _valid_doc()
    doc["widget"]["kb_profile_allowlist"] = ["widget-daytime"]
    assert projects.validate_registry(doc) is None


@pytest.mark.parametrize("malformed", [
    [],
    "widget-daytime",
    [""],
    [7],
], ids=["empty-list", "bare-string", "empty-string-entry", "non-string-entry"])
def test_profile_allowlist_fails_closed_on_a_malformed_field(monkeypatch, malformed):
    """A field that is present but not a non-empty list of non-empty strings
    maps the unit to the EMPTY set rather than leaving it out of the
    mapping: `projects.load()` never runs `validate_registry` (that check
    only gates a hand-authored edit's shape, not a value the Library writes
    later), so a malformed field must fail closed here rather than reopen
    the directory it was meant to confine. Break the `valid = ...` check
    back into `if not names: continue` and this test fails, because the
    unit then vanishes from the mapping (unrestricted) instead of landing
    in it mapped to `set()` (refused)."""
    monkeypatch.setattr(projects, "load", lambda: {
        "widget": {
            "home_dir": "<root>/widget",
            "kb_profile_allowlist": malformed,
        },
    })
    assert projects.profile_allowlist() == {"widget": set()}


def test_profile_allowlist_leaves_an_absent_field_out_of_the_mapping(monkeypatch):
    monkeypatch.setattr(projects, "load", lambda: {
        "widget": {"home_dir": "<root>/widget"},
    })
    assert "widget" not in projects.profile_allowlist()


def test_profile_allowlist_leaves_a_null_field_out_of_the_mapping(monkeypatch):
    """`null` means the same as absent — a deliberate removal of the field's
    effect — never the empty-set refusal a malformed value gets below."""
    monkeypatch.setattr(projects, "load", lambda: {
        "widget": {"home_dir": "<root>/widget", "kb_profile_allowlist": None},
    })
    assert "widget" not in projects.profile_allowlist()


def test_check_profile_allowed_refuses_everything_for_an_empty_allowlist(
        knowledge_ctx, monkeypatch):
    """An empty set (what the malformed-field fix above produces) must refuse
    EVERY profile for that directory, including the profile the crew would
    otherwise be entitled to — `check_profile_allowed` cannot tell a
    deliberate lockout from a malformed field, so both fail closed the same
    way."""
    monkeypatch.setattr(knowledge_ctx.projects, "load", lambda: {
        "widget": {"home_dir": "<root>/widget", "kb_profile_allowlist": []},
    })
    with pytest.raises(SystemExit) as exc:
        knowledge_ctx.check_profile_allowed("widget", "some-other-profile")
    assert exc.value.code == knowledge_ctx.PROFILE_NOT_ALLOWED
    with pytest.raises(SystemExit) as exc2:
        knowledge_ctx.check_profile_allowed("widget", "widget-daytime")
    assert exc2.value.code == knowledge_ctx.PROFILE_NOT_ALLOWED
