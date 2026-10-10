"""Frontmatter validator (lib/knowledge/knowledge.py).

Gates which docs enter the SQLite index. A false-accept lets a malformed doc in
(downstream KB readers crash / agents get bad context); a false-reject silently
drops a valid doc. NB: ALLOWED_PROJECTS + ID_PATTERN are exactly what Phase B
extends to add a `platform` project — these tests pin current behavior so that
change is a visible, deliberate edit.
"""
from __future__ import annotations

import pytest

import knowledge


def _ok_doc(**over):
    fm = {
        "id": "agent-platform/agents/coder",
        "kind": "agent",
        "project": "agent-platform",
        "title": "Coder",
        "status": "current",
    }
    fm.update(over)
    return fm


def test_valid_doc_has_no_errors():
    assert knowledge.validate_frontmatter(_ok_doc()) == []


def test_non_mapping_rejected():
    assert knowledge.validate_frontmatter("not a dict") == [
        "frontmatter is not a YAML mapping"
    ]


def test_each_required_field_missing_is_flagged():
    for field in knowledge.REQUIRED:
        doc = _ok_doc()
        del doc[field]
        errs = knowledge.validate_frontmatter(doc)
        assert any(field in e and "missing required field" in e for e in errs)


def test_empty_required_field_flagged():
    errs = knowledge.validate_frontmatter(_ok_doc(title=""))
    assert any("missing required field: title" in e for e in errs)


def test_invalid_enums_flagged():
    assert any("invalid kind" in e for e in knowledge.validate_frontmatter(_ok_doc(kind="bogus")))
    # `platform` used to be asserted here as NOT a project. It is now a legal
    # non-Project SCOPE (ARCH-426 amendment) — see
    # test_the_non_project_scopes_are_legal_in_the_reference_room below. Something
    # genuinely unregistered takes its place, so the check still has teeth.
    assert any("invalid project" in e for e in knowledge.validate_frontmatter(_ok_doc(project="no-such-thing")))
    assert any("invalid status" in e for e in knowledge.validate_frontmatter(_ok_doc(status="live")))


def test_agent_platform_project_accepted():
    # substrate ARCH-099: agent-platform onboarded as a first-class KB project.
    assert knowledge.validate_frontmatter(
        _ok_doc(id="agent-platform/architecture/engineering", project="agent-platform")
    ) == []


def test_the_retired_scopes_are_rejected():
    """`shared`, `convivy`, `utility` and `engineering` must no longer validate.

    ARCH-190 onboarded `engineering` and this test pinned it accepted; ARCH-438
    retires all four, so the assertion inverts rather than disappears. Deleting it
    would leave nothing saying these are gone, and a scope that quietly starts
    validating again is how a document lands in a directory nobody watches.

    The dangerous direction is REJECT-here-ACCEPT-there: a doc carrying a retired
    scope must fail in BOTH copies of the list, or it passes CI, merges, and the
    watcher silently drops it. `test_allowed_projects_matches_the_kb_validator`
    pins the pair; this pins the value.
    """
    for scope in ("shared", "convivy", "utility", "engineering"):
        errors = knowledge.validate_frontmatter(
            _ok_doc(id=f"{scope}/reference/anything", project=scope))
        assert any("invalid project" in e for e in errors), (
            f"{scope!r} still validates as a project; ARCH-438 retired it")


def test_engineering_practice_is_not_the_retired_engineering_scope():
    """The two were deliberately never spelled the same, and this is why.

    `engineering-practice` is cross-PRODUCT engineering idiom belonging to no
    project (operator, 2026-07-31). `engineering` was a bucket whose 13 documents all
    moved out under step 5. Recycling the name would have left every scoped
    document ambiguous between the old bucket and the new meaning for the length
    of the migration — so a test that the live one survives its neighbour's
    retirement is worth having.
    """
    assert knowledge.validate_frontmatter(
        _ok_doc(id="engineering-practice/skills/pytest-discipline",
                project="engineering-practice")) == []


def test_field_notes_kind_and_visibility():
    # ARCH-289: the Field Notes genre (conceptual/approach docs, the public-site spine)
    # + the visibility surface that gates the public Convivy Lab projection.
    # `company`, since ARCH-438 retired `convivy`. The id and the project move
    # together — a fixture whose halves disagree is the defect PR F found three of.
    doc = _ok_doc(id="company/field-notes/taste-map", kind="field-notes",
                  project="company", status="draft", visibility="internal")
    assert knowledge.validate_frontmatter(doc) == []
    assert knowledge.validate_frontmatter({**doc, "visibility": "public"}) == []
    # visibility is fail-closed and enumerated — a typo can't silently open a doc up
    assert any("invalid visibility" in e
               for e in knowledge.validate_frontmatter({**doc, "visibility": "semi-public"}))


def test_platform_crew_audience_in_roster():
    # the 3 platform build-crew agents are valid audience values (ARCH-099)
    assert knowledge.validate_frontmatter(
        _ok_doc(audience=["platform-architect", "platform-planner", "platform-coder"])
    ) == []


def test_id_pattern_enforced():
    errs = knowledge.validate_frontmatter(_ok_doc(id="Shared/Agents/Coder"))
    assert any("invalid id format" in e for e in errs)
    assert knowledge.validate_frontmatter(_ok_doc(id="vivy/runbooks/deploy-web")) == []
    # agent-platform is a valid id prefix despite the embedded hyphen (ARCH-099)
    assert knowledge.validate_frontmatter(
        _ok_doc(id="agent-platform/runbooks/deploy", project="agent-platform")
    ) == []


def test_list_fields_must_be_lists():
    errs = knowledge.validate_frontmatter(_ok_doc(audience="coder"))
    assert any("audience must be a list" in e for e in errs)


def test_audience_roster_enforced():
    assert knowledge.validate_frontmatter(_ok_doc(audience=["coder", knowledge.OPERATOR_MARKER])) == []
    errs = knowledge.validate_frontmatter(_ok_doc(audience=["coder", "nobody"]))
    assert any("not in roster: nobody" in e for e in errs)


def test_last_reviewed_must_be_iso():
    assert any(
        "last_reviewed must be YYYY-MM-DD" in e
        for e in knowledge.validate_frontmatter(_ok_doc(last_reviewed="May 31"))
    )
    assert knowledge.validate_frontmatter(_ok_doc(last_reviewed="2026-05-31")) == []


def test_overnight_stage_agents_in_roster():
    # phase-split stage agents (ARCH-268) are valid audience values
    assert knowledge.validate_frontmatter(
        _ok_doc(audience=["overnight-triage", "overnight-fix-drain", "overnight-verify"])
    ) == []


def test_retired_agent_names_still_index_their_documents():
    """issue_01M2GDVPR81Z0X991Z7XD3VR0M step 5 (the operator's 2026-09-14 ruling) retired
    overnight-orchestrator, hollis-overnight-orchestrator, knowledge-scribe and
    kb-sync-verifier from dispatch, but 100 documents on knowledge/main (74 of
    them decision records)
    already name one of these four in `audience`, and the Library will not
    rewrite that frontmatter. A document carrying any of the four must still
    validate with no roster error, or every one of those documents drops out
    of the index on the next watcher restart. Red on b23cf33 (the commit that
    dropped all four from ALLOWED_AUDIENCE), green once they're restored."""
    for name in (
        "overnight-orchestrator",
        "hollis-overnight-orchestrator",
        "knowledge-scribe",
        "kb-sync-verifier",
    ):
        errs = knowledge.validate_frontmatter(_ok_doc(audience=[name]))
        assert errs == [], f"{name}: {errs}"


@pytest.mark.needs_kb
def test_allowed_projects_matches_the_kb_validator():
    """The project allowlist is duplicated across two repos and must agree.

    `knowledge.ALLOWED_PROJECTS` (here, the indexer) and the knowledge repo's
    `scripts/validate.py` (CI) gate different things and fail in different
    directions, which is what makes the drift dangerous rather than merely
    untidy:

      - a project in the VALIDATOR but not the INDEXER passes CI, merges, and
        then silently never enters the index — `validate_frontmatter` rejects it
        with "invalid project", so the doc is invisible to kb_search and to every
        agent's knowledge profile. Nothing goes red. This is exactly what
        happened to `deck` (ARCH-374, 2026-07-19): the KB PR was green and
        merged, the watcher was restarted, and the docs simply were not there.

      - a project in the INDEXER but not the VALIDATOR fails loudly at PR time,
        which is the harmless direction.

    `needs_kb` carries the skip. CI checks out agent-platform alone, so this
    never runs there; `bin/check-kb-guards` runs it daily on the machine that has
    both checkouts, which is what stops "green" from meaning "did not run". The
    marker honours KNOWLEDGE_REPO, so a paired cross-repo change is verifiable
    from either side before both halves have merged — PR C's reviewer used it for
    exactly that.

    Two related guards DO run in CI (`test_deck_project_accepted`,
    `test_id_pattern_projects_match_allowed_projects`), so specific projects are
    protected there; only the general cross-repo invariant needs the KB.
    """
    import re

    from conftest import knowledge_repo as _kb

    validator = _kb() / "scripts" / "validate.py"
    assert validator.exists(), (
        f"the knowledge checkout at {_kb()} has no scripts/validate.py — the "
        f"marker admitted this test, so the checkout is there but malformed")

    m = re.search(r"ALLOWED_PROJECTS\s*=\s*\{(.*?)\}", validator.read_text(), re.S)
    assert m, "could not locate ALLOWED_PROJECTS in the knowledge repo's validate.py"

    # Strip comments before extracting names. That file annotates its entries
    # ("# the personal-apps suite ..."), and a comment mentioning a quoted
    # project name would otherwise be read as a member. The dangerous case is a
    # removal note — `# "old-project" removed` would keep the name in the parsed
    # set and mask the removal from this assertion.
    body = "\n".join(line.split("#", 1)[0] for line in m.group(1).splitlines())
    kb_projects = set(re.findall(r'"([a-z0-9-]+)"', body))

    assert kb_projects, "parsed an empty project set — the regex probably needs updating"

    only_in_validator = kb_projects - knowledge.ALLOWED_PROJECTS
    only_in_indexer = knowledge.ALLOWED_PROJECTS - kb_projects

    assert not only_in_validator, (
        f"projects accepted by the KB validator but rejected by the indexer: "
        f"{sorted(only_in_validator)}. Docs for these validate green, merge, and "
        f"never index. Add them to knowledge.ALLOWED_PROJECTS."
    )
    assert not only_in_indexer, (
        f"projects accepted by the indexer but rejected by the KB validator: "
        f"{sorted(only_in_indexer)}. Add them to the knowledge repo's "
        f"scripts/validate.py (ALLOWED_PROJECTS *and* ID_PATTERN)."
    )


def test_deck_project_accepted():
    """ARCH-374. Pins the specific project this drift was found through, so a
    revert of the allowlist entry fails here even if the cross-repo test above
    is skipped for want of a knowledge checkout."""
    doc = {
        "id": "deck/overview/deck-overview",
        "kind": "overview",
        "project": "deck",
        "title": "Deck",
        "status": "current",
    }
    assert knowledge.validate_frontmatter(doc) == []


def test_placard_project_accepted():
    """Pins placard the way test_deck_project_accepted pins deck, and for the same
    reason: the cross-repo test above is `needs_kb`-marked and skips in this
    repo's CI, so without this a revert of the two placard lines is caught by
    nothing CI can see until the daily cron hours later.

    Placard reached this file the long way. Its bead merged to knowledge main and
    then never indexed, because registering with the KB validator
    (convivy/knowledge#347) left the indexer's own copy of the list still
    rejecting it — the failure the constance-landing note in ALLOWED_PROJECTS
    already warns about."""
    doc = {
        "id": "placard/bead/placard",
        "kind": "bead",
        "project": "placard",
        "title": "Placard — the bead: where this product is going",
        "status": "current",
    }
    assert knowledge.validate_frontmatter(doc) == []


def test_shipwright_project_accepted():
    """Pins shipwright as test_placard_project_accepted pins placard: the
    cross-repo test is `needs_kb`-marked and skips in CI, so a revert of the two
    shipwright lines would otherwise be caught by nothing CI can see.

    Shipwright's slug landed in the KB validator (convivy/knowledge#356) the
    evening of 2026-09-06; this mirror followed the same evening, before its
    first document existed, so the profile and runbooks index on arrival."""
    doc = {
        "id": "shipwright/runbooks/release-signing",
        "kind": "runbook",
        "project": "shipwright",
        "title": "Signing and notarizing a Builder release",
        "status": "current",
    }
    assert knowledge.validate_frontmatter(doc) == []


def test_the_non_project_scopes_are_legal_in_the_reference_room():
    """ARCH-426's amendments make the Library's scope set Project ∪ {`company`,
    `platform`, `commercial`, `engineering-practice`}, and it is ONE VOCABULARY
    VALID IN ALL THREE VAULTS. A Reference Room document scoped `platform` is
    correct, not mislabelled — which is the reading this test exists to pin, since
    the previous one asserted the opposite."""
    import projects

    for scope in projects.scopes():
        doc = {
            "id": f"{scope}/reference/example",
            "kind": "reference",
            "project": scope,
            "title": "Example",
            "status": "current",
        }
        assert knowledge.validate_frontmatter(doc) == [], (
            f"the {scope!r} scope is registered in projects.json but the indexer "
            f"rejects it — a doc scoped {scope!r} would validate nowhere")


def test_every_registered_scope_is_in_allowed_projects():
    """The registry is the source; the two validator copies mirror it. A scope
    added to `_scopes` but not to ALLOWED_PROJECTS is the silent failure — the
    scope reads as legal everywhere the registry is consulted, and every document
    carrying it is dropped by the indexer without a word."""
    import projects

    missing = set(projects.scopes()) - knowledge.ALLOWED_PROJECTS
    assert not missing, (
        f"scopes registered in projects.json but absent from "
        f"knowledge.ALLOWED_PROJECTS: {sorted(missing)}")


def test_all_scopes_is_projects_plus_the_non_project_scopes():
    """The set a `product`/`project` field should be validated against. Checking
    against `load()` alone is what returned 400 for three legal values."""
    import projects

    assert projects.all_scopes() == set(projects.load()) | set(projects.scopes())
    for scope in ("company", "platform", "commercial", "engineering-practice"):
        assert scope in projects.all_scopes()
        assert scope not in projects.load(), (
            f"{scope} is a scope, not a project — nothing is built at it, so it "
            f"must not appear as a registry unit with a home dir and a crew")


def test_id_pattern_projects_match_allowed_projects():
    """The project list appears FOUR times across two repos — ALLOWED_PROJECTS and
    ID_PATTERN, in this file and in the knowledge repo's validate.py — and all four
    must carry the same names.

    Adding a project to a set but not the matching pattern is the sharper trap of
    the two: the project check passes and then every doc fails on its `id`, which
    reads like a malformed-id bug rather than a missing registration. This was
    found by the deck bootstrap (ARCH-374) hitting it in BOTH files in sequence.
    """
    import re

    pattern_projects = set(
        re.match(r"\^\((.*?)\)/", knowledge.ID_PATTERN.pattern).group(1).split("|")
    )
    assert pattern_projects == knowledge.ALLOWED_PROJECTS, (
        "ID_PATTERN and ALLOWED_PROJECTS disagree. "
        f"only in ID_PATTERN: {sorted(pattern_projects - knowledge.ALLOWED_PROJECTS)}; "
        f"only in ALLOWED_PROJECTS: {sorted(knowledge.ALLOWED_PROJECTS - pattern_projects)}"
    )



@pytest.mark.needs_kb
def test_schema_constants_agree_with_the_kb_validator():
    """`knowledge.py` and the KB's `scripts/validate.py` must hold the same schema.

    The watcher validates with this module and CI validates with that script, so a
    value legal in one and not the other splits the two readings of "is this
    document valid" — and the failure is silent in the dangerous direction: a doc
    that passes CI, merges, and is then skipped by the watcher exists in git and
    not in the index, which is a document that has quietly left the KB.

    Both files carry a comment asking the next author to keep them in sync, and
    nothing enforced it. That is exactly the shape that let the project maps
    disagree three times before `test_product_maps_agree` was written (`knowledge`
    in July, then substrate and write-better, then convivy-lab). Step 5 edits both
    lists, so it adds the assertion rather than a fourth comment.

    `needs_kb` carries the skip and honours KNOWLEDGE_REPO, so a paired
    cross-repo change is verifiable from either side and `bin/check-kb-guards`
    runs this daily where both checkouts exist.
    """
    import re

    from conftest import knowledge_repo

    kb = knowledge_repo()
    src = kb / "scripts" / "validate.py"
    assert src.is_file(), (
        f"the knowledge checkout at {kb} has no scripts/validate.py — the marker "
        f"admitted this test, so the checkout is there but malformed")
    text = src.read_text(encoding="utf-8")

    def literal_set(name: str) -> set[str]:
        # Brace-MATCHED rather than regex-terminated. These constants do not all
        # close on a line of their own — ALLOWED_PROJECTS ends mid-line — so a
        # `^\}` terminator runs on into the next constant and silently unions two
        # unrelated sets. That is not a hypothetical: the first version of this
        # test reported the whole audience roster as missing projects.
        start = text.index(f"{name} = {{") + len(f"{name} = ")
        depth, end = 0, start
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        else:
            raise AssertionError(f"unbalanced braces reading {name} from {src}")
        # Quoted entries only, so a `#`-comment mentioning a value cannot smuggle
        # one in — several of these lists carry long explanatory comments.
        body = re.sub(r"#.*", "", text[start:end])
        return set(re.findall(r'"([A-Za-z0-9._-]+)"', body))

    # ALLOWED_AUDIENCE is pinned too, and its asymmetry makes it the WORST of the
    # four rather than the most forgiving. validate.py treats an unknown audience as
    # a WARNING and knowledge.py treats it as an ERROR, so the natural way to hit
    # the gap is to add a new agent slug to validate.py because CI is complaining,
    # and stop there: every document carrying that audience then passes CI, merges,
    # and is silently dropped by the watcher. The deliberately-lenient one is the
    # one most likely to be edited alone (reviewer, #343).
    # NON_AGENT_AUDIENCE is the other half of the roster the agent policy reads: ALLOWED_AGENTS
    # is ALLOWED_AUDIENCE minus it, so a person added to one list alone changes which names may
    # be bound. Both files hold it, and a test pins it like the rest.
    #
    # The operator marker is configured per-operator here (CQ·428, steward ruling) and
    # still hardcoded in the KB validator — "the knowledge repo's frontmatter keeps its
    # current value, so the Library's contract is untouched". It is a member of BOTH
    # ALLOWED_AUDIENCE and NON_AGENT_AUDIENCE on each side, so it is excluded from both
    # sides of both comparisons, DERIVED rather than named (their marker is whatever
    # their own NON_AGENT_AUDIENCE carries beyond the three fixed human-group markers),
    # so a synthetic test-session marker never reads as the drift this check exists to
    # catch.
    their_marker = literal_set("NON_AGENT_AUDIENCE") - {"consultant", "team", "contractor"}
    for name, ours in (("ALLOWED_KINDS", knowledge.ALLOWED_KINDS),
                       ("ALLOWED_PROJECTS", knowledge.ALLOWED_PROJECTS),
                       ("ALLOWED_STATUSES", knowledge.ALLOWED_STATUSES),
                       ("ALLOWED_AUDIENCE", knowledge.ALLOWED_AUDIENCE),
                       ("NON_AGENT_AUDIENCE", knowledge.NON_AGENT_AUDIENCE)):
        theirs = literal_set(name)
        ours_set = set(ours)
        if name in ("ALLOWED_AUDIENCE", "NON_AGENT_AUDIENCE"):
            ours_set -= {knowledge.OPERATOR_MARKER}
            theirs -= their_marker
        only_here = sorted(ours_set - theirs)
        only_there = sorted(theirs - ours_set)
        assert not only_here, (
            f"{name}: in knowledge.py but not the KB validator: {only_here} — "
            f"the watcher would index a document CI rejects")
        assert not only_there, (
            f"{name}: in the KB validator but not knowledge.py: {only_there} — "
            f"a document passes CI, merges, and the watcher then skips it, so it "
            f"exists in git and not in the index")


# --- kind: instruction-block (mirrors convivy/knowledge scripts/validate.py) ------
#
# Ported from knowledge's scripts/test_validate_instruction_block.py so both copies
# of the rules are held to the same cases. Every rule gets a case that must fail
# and one that must pass: a check that only ever sees valid blocks cannot be told
# apart from one that is not wired up.


def _block(**overrides):
    fm = {
        "id": "platform/instruction-blocks/example-rule",
        "kind": "instruction-block",
        "project": "platform",
        "title": "Example rule",
        "status": "current",
        "scope": "crew",
        "binds_crews": ["library"],
    }
    fm.update(overrides)
    return fm


def test_the_first_real_blocks_frontmatter_validates():
    """The frontmatter the Library passed to this function and got
    `['invalid kind: instruction-block']` back (issue_01M2N9YV3G52KBYNR614WAC5NY)."""
    fm = {"id": "platform/instruction-blocks/no-agent-merges-a-pull-request",
          "kind": "instruction-block", "project": "platform",
          "title": "No agent merges a pull request", "status": "current",
          "scope": "fleet", "audience": ["team"], "topics": ["merge-safety"],
          "last_reviewed": "2026-09-16", "authors": ["library"]}
    assert knowledge.validate_frontmatter(fm) == []
    assert knowledge.instruction_block_errors(fm, "No agent merges a pull request.") == []


def test_binds_fields_must_be_lists():
    for field in ("binds_crews", "binds_agents", "binds_topics", "binds_sessions"):
        errs = knowledge.validate_frontmatter(_block(**{field: "library"}))
        assert f"{field} must be a list" in errs, field


# --- binds_sessions (issue_01M2PKAP28KPP44P64DGXS8CG8) ----------------------
#
# The selector merged into convivy/knowledge as knowledge#549 and this copy of
# the schema never gained it, so a block selecting only on session kind was
# refused here as "a block matching nothing binds nothing" — a correct-sounding
# refusal of a document that was in fact well formed. Two sessions confirmed the
# field had shipped by reading the merge; only calling `kb propose` showed
# neither could use it.


def test_a_block_binding_only_a_session_kind_is_a_real_selector():
    """THE REGRESSION. Before the fix `has_selector` ignored `binds_sessions`
    entirely, so this block was rejected for binding nothing."""
    fm = _block(binds_sessions=["interactive"])
    del fm["binds_crews"]
    assert knowledge.instruction_block_errors(fm, "A rule.") == []


def test_an_unknown_session_kind_fails_and_a_known_one_passes():
    errs = knowledge.instruction_block_errors(_block(binds_sessions=["scheduled"]), "A rule.")
    assert any("unknown session kind" in e for e in errs)
    for kind in ("interactive", "dispatched"):
        assert knowledge.instruction_block_errors(
            _block(binds_sessions=[kind]), "A rule.") == [], kind


def test_a_blank_session_entry_fails_on_the_membership_test():
    """`binds_sessions` gets no separate blank-entry floor because its
    vocabulary is closed — a blank fails membership on the same line. This
    pins that, so removing the floor's absence is a deliberate act."""
    for junk in ("", "   ", None):
        errs = knowledge.instruction_block_errors(_block(binds_sessions=[junk]), "A rule.")
        assert any("unknown session kind" in e for e in errs), repr(junk)


def test_fleet_must_not_carry_a_session_selector():
    fm = _block(scope="fleet", binds_sessions=["interactive"])
    del fm["binds_crews"]
    errs = knowledge.instruction_block_errors(fm, "A rule.")
    assert any("fleet is the floor" in e for e in errs)


def test_unknown_crew_fails_and_known_crew_passes():
    errs = knowledge.instruction_block_errors(_block(binds_crews=["not-a-real-crew"]), "A rule.")
    assert any("unknown crew" in e for e in errs)
    assert knowledge.instruction_block_errors(_block(binds_crews=["library"]), "A rule.") == []


def test_names_that_are_not_registry_crews_fail():
    """Four names the first knowledge-side set accepted, each binding no session."""
    for name in ("orient", "www", "constance-landing", "nyh-studio"):
        errs = knowledge.instruction_block_errors(_block(binds_crews=[name]), "A rule.")
        assert any("unknown crew" in e for e in errs), name


def test_unknown_agent_fails_and_known_agent_passes():
    errs = knowledge.instruction_block_errors(
        _block(binds_crews=[], binds_agents=["platform-astronaut"]), "A rule.")
    assert any("unknown agent" in e for e in errs)
    assert knowledge.instruction_block_errors(
        _block(scope="agent", binds_crews=[], binds_agents=["platform-coder"]), "A rule.") == []


def test_binds_agents_refuses_a_person_or_a_group():
    for who in sorted(knowledge.NON_AGENT_AUDIENCE):
        errs = knowledge.instruction_block_errors(
            _block(scope="agent", binds_crews=[], binds_agents=[who]), "A rule.")
        assert any("person or a group" in e for e in errs), who


def test_unknown_or_missing_scope_fails():
    assert any("scope" in e for e in knowledge.instruction_block_errors(_block(scope="galaxy"), "A rule."))
    fm = _block()
    del fm["scope"]
    assert any("scope" in e for e in knowledge.instruction_block_errors(fm, "A rule."))


def test_every_scope_passes_with_the_selector_it_needs():
    for scope in knowledge.INSTRUCTION_BLOCK_SCOPES:
        crews = [] if scope == "fleet" else ["library"]
        assert knowledge.instruction_block_errors(_block(scope=scope, binds_crews=crews), "A rule.") == [], scope


def test_fleet_with_a_selector_fails_and_non_fleet_without_one_fails():
    errs = knowledge.instruction_block_errors(_block(scope="fleet", binds_crews=["library"]), "A rule.")
    assert any("fleet" in e for e in errs)
    errs = knowledge.instruction_block_errors(
        _block(scope="crew", binds_crews=[], binds_agents=[], binds_topics=[]), "A rule.")
    assert any("matching nothing" in e for e in errs)


def test_binds_topics_refuses_an_unusable_entry_and_it_is_not_a_selector():
    for junk in ("", "   ", None, 42, ["nested"]):
        errs = knowledge.instruction_block_errors(
            _block(scope="task", binds_crews=[], binds_topics=[junk]), "A rule.")
        assert any("not a usable topic" in e for e in errs), junk
        assert any("needs at least one of" in e for e in errs), junk
    assert knowledge.instruction_block_errors(
        _block(scope="task", binds_crews=[], binds_topics=["dependencies"]), "A rule.") == []


def test_a_fleet_block_with_only_junk_selectors_is_refused_for_the_entry_not_the_floor():
    errs = knowledge.instruction_block_errors(
        _block(scope="fleet", binds_crews=[], binds_topics=[""]), "A rule.")
    assert not any("must not also carry" in e for e in errs)
    assert any("not a usable topic" in e for e in errs)


# --- binds_roles: a selector by declared role (mirrors knowledge scripts/validate.py) ---


def test_a_non_fleet_block_whose_only_selector_is_binds_roles_is_usable():
    """Red on origin/main: `binds_roles` was not counted, so the block was dropped
    with "needs at least one of binds_crews, ..."."""
    assert knowledge.instruction_block_errors(
        _block(scope="agent", binds_crews=[], binds_roles=["reviewer"]), "A rule.") == []


def test_binds_roles_refuses_an_unknown_role_naming_it():
    errs = knowledge.instruction_block_errors(
        _block(scope="agent", binds_crews=[], binds_roles=["reviwer"]), "A rule.")
    assert any("binds_roles names an unknown role: reviwer" in e for e in errs), errs


def test_binds_roles_refuses_an_unusable_entry_and_it_is_not_a_selector():
    for junk in ("", "   ", None, 42, ["nested"], {"a": 1}):
        errs = knowledge.instruction_block_errors(
            _block(scope="agent", binds_crews=[], binds_roles=[junk]), "A rule.")
        assert any("not a usable role" in e for e in errs), junk
        assert any("needs at least one of" in e for e in errs), junk


def test_a_fleet_block_carrying_binds_roles_is_refused_and_the_message_names_it():
    errs = knowledge.instruction_block_errors(
        _block(scope="fleet", binds_crews=[], binds_roles=["coder"]), "A rule.")
    assert any("must not also carry" in e and "binds_roles" in e for e in errs), errs


def test_the_non_fleet_no_selector_message_names_binds_roles():
    errs = knowledge.instruction_block_errors(
        _block(scope="agent", binds_crews=[]), "A rule.")
    assert any("needs at least one of" in e and "binds_roles" in e for e in errs), errs


def test_binds_roles_is_a_list_field():
    errs = knowledge.validate_frontmatter(_ok_doc(binds_roles="reviewer"))
    assert "binds_roles must be a list" in errs


# Bump this WHEN YOU COPY A CHANGE TO ALLOWED_ROLES from the knowledge repo's
# `scripts/validate.py`. The set is hand-kept: no roster here can derive it.
EXPECTED_ROLE_VOCABULARY_SIZE = 25


def test_the_role_vocabulary_is_pinned():
    assert len(knowledge.ALLOWED_ROLES) == EXPECTED_ROLE_VOCABULARY_SIZE, (
        "ALLOWED_ROLES changed. Copy the knowledge repo's scripts/validate.py set "
        "exactly, then update EXPECTED_ROLE_VOCABULARY_SIZE.")


def test_relative_cross_reference_fails():
    errs = knowledge.instruction_block_errors(_block(), "As noted above, do the thing.")
    assert any("relative cross-reference" in e for e in errs)
    assert knowledge.instruction_block_errors(_block(), "Do the thing, always.") == []


def test_body_budget_is_enforced_at_the_boundary():
    limit = knowledge.INSTRUCTION_BLOCK_BODY_BUDGET_BYTES
    assert any("budget" in e for e in knowledge.instruction_block_errors(_block(), "x" * (limit + 1)))
    assert knowledge.instruction_block_errors(_block(), "x" * limit) == []


# Bump this WHEN THE ROSTER GROWS, and in the same edit decide whether each new name
# is a person. If it is, add it to NON_AGENT_AUDIENCE too; otherwise it becomes a
# valid `binds_agents` value, a defect for a human. The roster lives in the Library,
# so this pins the seed the stub serves here, which is the same set the loader reads.
EXPECTED_AUDIENCE_ROSTER_SIZE = 58


def test_every_audience_value_is_classified():
    """allowed_agents() is the roster minus NON_AGENT_AUDIENCE, so a person added to
    the roster alone becomes bindable. A partition assertion holds whatever was
    added, so the roster's size is the forcing function."""
    roster = knowledge.audience_roster()
    assert knowledge.NON_AGENT_AUDIENCE <= roster
    assert knowledge.allowed_agents().isdisjoint(knowledge.NON_AGENT_AUDIENCE)
    assert len(roster) == EXPECTED_AUDIENCE_ROSTER_SIZE, (
        "The roster changed. Decide whether each new name is a person "
        "(add it to NON_AGENT_AUDIENCE) or an agent (nothing to do), then update "
        "EXPECTED_AUDIENCE_ROSTER_SIZE.")


# --- a malformed value is refused, never raised ------------------------------------
#
# The indexer's whole-tree scan calls these with no try/except, so a raise on one
# document stops every other document from indexing (security-reviewer, #601).


@pytest.mark.parametrize("field", ["kind", "project", "status", "visibility", "id", "title"])
@pytest.mark.parametrize("bad", [["x"], {"a": 1}, 42])
def test_a_non_string_scalar_field_is_an_error_not_an_exception(field, bad):
    errs = knowledge.validate_frontmatter(_ok_doc(**{field: bad}))
    assert errs, (field, bad)


@pytest.mark.parametrize("field", ["binds_crews", "binds_agents"])
def test_an_unhashable_binds_entry_is_an_error_not_an_exception(field):
    errs = knowledge.instruction_block_errors(
        _block(**{"scope": "agent", "binds_crews": [], field: [["coder"], {"a": 1}]}), "A rule.")
    assert sum("unknown" in e for e in errs) == 2, errs


@pytest.mark.parametrize("field", knowledge.LIST_FIELDS)
@pytest.mark.parametrize("bad", [__import__("datetime").date(2026, 8, 11), ["nested"], {"a": 1}, 7])
def test_a_non_string_list_entry_is_an_error(field, bad):
    """An unquoted date in YAML list is a `date`; json.dumps then raised in upsert."""
    errs = knowledge.validate_frontmatter(_ok_doc(**{field: [bad]}))
    assert f"{field} entries must be strings: {bad!r}" in errs
