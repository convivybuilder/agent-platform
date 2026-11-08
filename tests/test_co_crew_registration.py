"""Drift guard for `bin/co`'s model tier and crew-cockpit list.

`bin/co` launches every co on Opus unless `_co_sonnet_crews` names its crew
(operator, 2026-09-16). It used to be the other way round: a crew reached Opus only
through one of three lists, anything else fell to a silent Sonnet `*)`, and
`signpost` (2026-09-05), `shipwright` (2026-09-08) and Signals (2026-09-16) each
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

import fnmatch
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

    `_co_sonnet_crews` is assigned twice: `()` empty by default, then
    rewritten from a registry read with no crew name written in by hand.
    First-match reads that empty literal, which is exactly what the guard
    below needs — a hardcoded name belongs there, not in the registry-driven
    rewrite.
    """
    m = re.search(rf"^{re.escape(name)}=\((.*?)\)$", text, re.MULTILINE | re.DOTALL)
    assert m, f"bin/co has no {name}=( ... ) assignment"
    return [tok.strip("\"'") for tok in m.group(1).split()]


def sonnet_crews(text: str) -> set[str]:
    return set(_zsh_array(text, "_co_sonnet_crews"))


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


@pytest.mark.rich_registry
def test_the_derived_list_is_what_bin_co_used_to_hold():
    """The derivation reproduces the literal it replaced, exactly. Written as
    the explicit four rather than as a recomputation of the same query, since a
    test that derives its expectation the same way the code does cannot tell
    whether either is right."""
    assert projects.sonnet_crew_dirs() == [
        "northwind-concierge", "oak-arch-tools", "oak-etc", "oak-guest",
        "oak-marketing", "oak-park-villa", "oak-properties", "styleguide",
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


# --- the crew-cockpit branch, fully registry-driven (no bin/co literal left) -


def test_bin_co_no_longer_hardcodes_a_crew_cockpit_list_or_case(co_text):
    """`co takes its crew layout from the registry and names no crew`: the old
    `_co_crew_dirs=(...)` six-member literal and its `case ${repo_root:A}`
    styling block are gone. What is left to guard is that nothing like them
    comes back, and that the branch reads its gate, style and label through
    `projects.cockpit_gate`/`projects.co_style` rather than re-inventing either."""
    assert "_co_crew_dirs" not in co_text
    assert "crew_cockpit_dirs()" not in co_text, (
        "bin/co should query the registry per-directory (cockpit_gate/co_style) "
        "rather than build a list of every crew-cockpit home up front"
    )
    assert "cockpit_gate(" in co_text
    assert "co_style(" in co_text


def test_every_active_central_kb_crew_has_a_bin_co_launch_shape():
    """A crew individuated from the central KB needs ONE of the four launch-shape
    flags bin/co reads (`crew_cockpit`, `co_self_contained`, `co_generic_refresh`,
    `company_workspace`), or `co -r` dies there with "cwd is not a recognized
    product repo" — the gap `crew_cockpit` exists to close, generalised: a crew
    missing all four is a crew nobody told bin/co how to launch at all, the same
    way an unregistered one used to be missed before any of these fields existed.
    Checked against the real, checked-in registry, by the invariant rather than
    by naming which crew holds which flag — a retired crew leaving no row, or a
    new one added with a flag already set, both pass with no edit here."""
    flags = ("crew_cockpit", "co_self_contained", "co_generic_refresh",
             "company_workspace")
    for slug, unit in projects.load().items():
        if unit.get("lifecycle") != "active" or unit.get("kb") != "central":
            continue
        assert any(unit.get(f) is True for f in flags), (
            f"{slug} is an active, kb=central crew with none of {flags} set — "
            f"bin/co has no dedicated branch, crew-cockpit entry or self-contained "
            f"flag to launch it with")


def test_every_active_crew_cockpit_unit_has_a_real_home_directory_entry():
    """`projects.cockpit_gate`/`co_style`, which the branch calls per-directory,
    both resolve through `home_path(slug)` — a crew_cockpit unit with no (or a
    malformed) `home_dir` would raise there instead of failing closed to "not a
    cockpit", so the schema test's `home_dir` check is this guard's real
    backstop; this one just states the invariant where this file's own readers
    will see it."""
    for slug, unit in projects.load().items():
        if unit.get("crew_cockpit") is not True:
            continue
        assert projects.home_path(slug) is not None, (
            f"{slug} is crew_cockpit but home_path() returned None")


# CQ·565: `test_crew_cockpit_dirs_count_matches_the_live_registry_today`, which
# used to live here, pinned the real, checked-in registry's current
# crew-cockpit COUNT (10), so a row gaining or losing the flag was a change a
# reviewer had to look at. That subject is gone: the committed registry this
# repo once counted no longer exists, and the `rich_registry` fixture's own
# crew_cockpit rows are invented, with no live count for this test to track.
# `test_crew_cockpit_dirs_matches_the_live_registry_today` below is
# unaffected — it derives its own expectation from whichever registry is
# active rather than pinning a literal.


@pytest.mark.rich_registry
def test_crew_cockpit_dirs_matches_the_live_registry_today():
    """The derived list is exactly the home-directory basenames of the
    CHECKED-IN config/projects.json's own active, `crew_cockpit: true`
    crews. This is a statement about the real file, not about the function
    (see test_crew_registration.py for that), and it must find at least
    one: a registry with none would mean the one path `agentctl crew
    register` now defaults onto has gone untested here."""
    expected = sorted(
        Path(unit["home_dir"]).name
        for unit in projects.load().values()
        if unit.get("lifecycle") == "active" and unit.get("crew_cockpit") is True
    )
    assert expected, "no active crew in config/projects.json sets crew_cockpit: true"
    assert projects.crew_cockpit_dirs() == expected


# --- every active, central-kb crew must actually reach `co -r` -------------


def _dedicated_branch_names(text: str) -> tuple[set[str], set[str]]:
    """The literal and glob-pattern basenames `bin/co` recognizes through its
    own per-crew `if [[ $(basename "$repo_root") == ... ]]` branches, each
    elsewhere in this file than the registry-driven cockpit/self-contained/
    generic-refresh/company-workspace matches below. A crew matched here
    needs none of those registry flags: it already has a dedicated branch
    of its own."""
    literal = set(re.findall(r'\(basename "\$repo_root"\) == ([A-Za-z][\w-]*)\b', text))
    glob = set(re.findall(r'\(basename "\$repo_root"\) == (\*[\w-]+)', text))
    return literal, glob


def _product_case_allowlist(text: str) -> set[str]:
    """The literal product-repo names `_co_generic_daytime_refresh`'s own
    `case $product in` lets straight through with no adopter-crew check —
    today just the two legacy workspaces with no registry unit of their
    own (`knowledge`, `dev-setup`); every registered crew on this path
    names itself via `co_generic_refresh: true` instead."""
    m = re.search(r"case \$product in\n(?:\s*#.*\n)*\s*([\w|-]+)\)", text)
    assert m, "bin/co's generic-tail case $product allowlist moved or was renamed"
    return set(m.group(1).split("|"))


def _flag_home_basenames(flag: str) -> set[str]:
    """Home-directory basenames of active units with `flag` set True — the
    same shape `projects.crew_cockpit_dirs()`/`self_contained_crew_dirs()`
    already return, computed here for the two flags (`co_generic_refresh`,
    `company_workspace`) with no basename accessor of their own, since
    `generic_refresh_names()` answers a different question (the slugs,
    aliases and repo names bin/co's CLI-arg validator matches, not
    directory basenames) and `company_workspace_slug()` returns one slug
    rather than a basename."""
    out = set()
    for slug, unit in projects.load().items():
        if unit.get("lifecycle") != "active" or unit.get(flag) is not True:
            continue
        home = projects.home_path(slug)
        if home is not None:
            out.add(home.name)
    return out


def test_every_active_central_kb_crew_reaches_co_dash_r(co_text):
    """`co -r` from a central-kb crew's home directory must resolve through
    one of: a registry `crew_cockpit`/`co_self_contained`/
    `co_generic_refresh`/`company_workspace` flag, a crew's own dedicated
    branch elsewhere in `bin/co`, or the generic tail's legacy-workspace
    allowlist. A crew in none of these dies with "cwd is not a recognized
    product repo" — the gap two earlier crews each hit before
    `crew_cockpit` existed (`projects.crew_cockpit_dirs`'s own docstring has
    the history). No `_co_crew_dirs` literal is read any more: `co takes
    its crew layout from the registry and names no crew` removed it, and
    every one of its six crews now carries `crew_cockpit: true` in the
    checked-in registry instead."""
    literal_branches, glob_branches = _dedicated_branch_names(co_text)
    product_allowlist = _product_case_allowlist(co_text)
    covered = (
        set(projects.crew_cockpit_dirs())
        | set(projects.self_contained_crew_dirs())
        | _flag_home_basenames("co_generic_refresh")
        | _flag_home_basenames("company_workspace")
    )

    uncovered = []
    for slug, unit in projects.load().items():
        if unit.get("lifecycle") != "active" or unit.get("kb") != "central":
            continue
        name = Path(unit["home_dir"]).name
        if (name in covered or name in literal_branches
                or name in product_allowlist
                or any(fnmatch.fnmatch(name, pat) for pat in glob_branches)):
            continue
        uncovered.append(slug)
    assert not uncovered, (
        f"co -r cannot resolve these active, central-kb crews: {uncovered}. "
        f"Each needs crew_cockpit, co_self_contained, co_generic_refresh or "
        f"company_workspace set true (if it has no dedicated bin/co branch "
        f"of its own), or a fix to whichever branch/allowlist should cover "
        f"it instead."
    )
