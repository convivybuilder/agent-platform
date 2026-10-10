"""Drift guard for `bin/co`'s model tier and crew-cockpit list.

`bin/co` launches every co on Opus unless `_co_sonnet_crews` names its crew
(operator, 2026-09-16). It used to be the other way round: a crew reached Opus only
through one of three lists, anything else fell to a silent Sonnet `*)`, and
`placard` (2026-09-05), `shipwright` (2026-09-08) and Signals (2026-09-16) each
launched on Sonnet until the operator noticed the model. The last one was in no registry
this file reads, so a check against the registry could not have caught it; only
changing the default could.

What is left to guard is the one list. A name in `_co_sonnet_crews` that is not
an active crew's directory matches nothing and bills nothing, and it reads like a
decision somebody made, so it fails here.

These are TEXT assertions over `bin/co`, the same shape `tests/test_projects.py`
uses to hold consumer lists against the registry. `tests/bash/test_co_opus_pin.sh`
covers what actually launches, by running `co` against a fake HOME.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import projects

CO = Path(__file__).resolve().parent.parent / "bin" / "co"


@pytest.fixture(scope="module")
def co_text() -> str:
    return CO.read_text()


# --- reading the lists out of bin/co ----------------------------------------


def _zsh_array(text: str, name: str) -> list[str]:
    """The elements of the FIRST `name=( ... )` assignment, quotes stripped.

    `_co_crew_dirs` is assigned twice — the literal paths, then the `:A`-resolved
    rewrite of itself — so first-match is what reads the literals rather than the
    `("${(@)_co_crew_dirs:A}")` re-assignment.
    """
    m = re.search(rf"^{re.escape(name)}=\((.*?)\)$", text, re.MULTILINE | re.DOTALL)
    assert m, f"bin/co has no {name}=( ... ) assignment"
    return [tok.strip("\"'") for tok in m.group(1).split()]


def crew_dir_names(text: str) -> set[str]:
    """Basenames of `_co_crew_dirs`, the crew cockpits `co -r` re-individuates."""
    return {Path(p).name for p in _zsh_array(text, "_co_crew_dirs")}


def sonnet_crews(text: str) -> set[str]:
    return set(_zsh_array(text, "_co_sonnet_crews"))


def active_crew_dir_names() -> set[str]:
    """The directory basename `bin/co` would see for each active registry crew."""
    return {Path(unit["home_dir"]).name for unit in projects.load().values()
            if unit.get("lifecycle") == "active"}


# --- the guards --------------------------------------------------------------


def test_bin_co_no_longer_enumerates_the_sonnet_crews(co_text):
    """The exception list moved into the registry (crew-creation epic slice 2),
    so `bin/co` derives it and names no crew itself.

    THIS TEST EXISTS BECAUSE ITS PREDECESSOR WENT HOLLOW WHEN THE LITERAL LEFT.
    The old guard read `_co_sonnet_crews` out of this file and asserted every
    name was an active crew. With the array now assigned empty and filled at
    runtime, that subtraction is `set() - active`, which is empty, so it passed
    while checking nothing — a green that meant only that there was nothing to
    check. The registry-side guard below is what replaced its actual job; this
    one keeps a literal from quietly coming back and hollowing that one too.
    """
    assert sonnet_crews(co_text) == set(), (
        "bin/co names crews in _co_sonnet_crews again. The list belongs in "
        "config/projects.json as `model_tier: sonnet`; a literal here can "
        "disagree with the registry and nothing would notice."
    )
    assert "sonnet_crew_dirs()" in co_text, (
        "bin/co no longer derives the sonnet crews from the registry"
    )


def test_every_sonnet_crew_in_the_registry_is_an_active_crew():
    """A name nothing matches is a decision that decides nothing, and it would
    outlive a crew that was retired or renamed. This is the old guard's job,
    moved to where the names now live."""
    declared = {slug for slug, unit in projects.load().items()
                if unit.get("model_tier") == "sonnet"}
    inactive = {s for s in declared
                if (projects.get(s) or {}).get("lifecycle") != "active"}
    assert not inactive, (
        f"config/projects.json pins model_tier: sonnet on crews that are not "
        f"active: {sorted(inactive)}. Remove the field, or fix the lifecycle."
    )


def test_every_declared_model_tier_is_one_this_platform_serves():
    """A typo'd tier reads as Opus through `projects.model_tier`'s fallback,
    which bills the expensive model silently. Refused here instead."""
    bad = {slug: unit["model_tier"] for slug, unit in projects.load().items()
           if "model_tier" in unit and unit["model_tier"] not in projects.MODEL_TIERS}
    assert not bad, (
        f"unknown model_tier values in config/projects.json: {bad}. "
        f"One of {list(projects.MODEL_TIERS)}."
    )


def test_the_derived_list_is_what_bin_co_used_to_hold():
    """The derivation reproduces the literal it replaced, exactly. Written as
    the explicit four rather than as a recomputation of the same query, since a
    test that derives its expectation the same way the code does cannot tell
    whether either is right."""
    assert projects.sonnet_crew_dirs() == [
        "nyh-arch-tools", "nyh-etc", "nyh-park-villa", "write-better",
    ]


def test_opus_is_the_default_and_sonnet_is_the_listed_exception(co_text):
    """Pins the inversion by text, so a revert to a silent Sonnet fallback fails
    here as well as in the launch test."""
    block = co_text[co_text.index("_co_sonnet_crews=("):]
    block = block[:block.index("\nfi\n")]
    sonnet_arm = block.index('claude_model_args=(--model "claude-sonnet-5[1m]")')
    opus_arm = block.index('claude_model_args=(--model "claude-opus-5-5[1m]")')
    assert "_co_sonnet_crews[(Ie)" in block[:sonnet_arm]
    assert "else" in block[sonnet_arm:opus_arm]
    assert "*)" not in block, "a `case` fallthrough is back in the model block"


def test_every_crew_dir_has_a_styling_arm(co_text):
    """The crew-cockpit branch's `case ${repo_root:A}` styles each entry, and its
    own comment said it was "exhaustive only because `_co_crew_dirs` happens to
    list exactly these three. Nothing enforces that." Two entries were added on
    2026-09-08; this is the enforcement that note asked for."""
    m = re.search(r"case \$\{repo_root:A\} in\n(.*?)\n\s*esac", co_text, re.DOTALL)
    assert m, "bin/co has no `case ${repo_root:A}` styling block"
    styled = set(re.findall(r"\*/([\w.-]+)\)", m.group(1)))
    missing = crew_dir_names(co_text) - styled
    assert not missing, (
        f"_co_crew_dirs entries with no styling arm: {sorted(missing)}"
    )


def test_the_crews_that_needed_co_r_are_crew_cockpits(co_text):
    """`shipwright` and `placard` are individuated from the central KB, so
    `_co_crew_dirs` is what gives `co -r` their knowledge pull and
    re-individuate. The operator hit "cwd is not a recognized product repo" there before
    the model gap was noticed."""
    dirs = crew_dir_names(co_text)
    assert "shipwright" in dirs
    assert "placard" in dirs


# --- the registry-derived cockpits (issue_01M3YN6857H5J541G73Y9P3KA1) -------


def test_bin_co_appends_registry_derived_cockpits(co_text):
    """`_co_crew_dirs` is still a literal for the six crews above, but it now
    also consults the registry (`crew_cockpit_dirs()`), the same move
    `sonnet_crew_dirs()` already made for `model_tier` — so a crew
    `agentctl crew register` marks `crew_cockpit: true` reaches `co -r` with
    no edit to this file."""
    assert "crew_cockpit_dirs()" in co_text
    assert "_co_crew_dirs+=(" in co_text


def test_crew_cockpit_dirs_is_empty_for_the_live_registry_today():
    """No crew in the CHECKED-IN config/projects.json sets `crew_cockpit`
    yet — the six names above are still the hardcoded literal, not a
    registry field — so the derived list contributes nothing on this repo's
    own registry. This is a statement about the real file, not about the
    function (see test_crew_registration.py for that)."""
    assert projects.crew_cockpit_dirs() == []
