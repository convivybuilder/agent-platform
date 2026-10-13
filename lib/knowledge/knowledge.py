"""Frontmatter validator for the knowledge base.

Used by the watcher (~/.local/bin/knowledge-watcher) to gate which docs make it
into the SQLite index. Mirrors the validation rules in <root>/knowledge/SCHEMA.md.

The legacy `notion_id` / `notion_view` frontmatter fields on ~88 docs migrated
from Notion on 2026-05-18 are tolerated (the validator doesn't enforce or strip
them); after the 2026-05-28 Notion sunset (substrate ARCH-072) they are no
longer set on new docs.
"""
from __future__ import annotations

import http.client
import json
import re
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any, NamedTuple

import operator_config

# FAIL CLOSED (steward ruling, CQ·428): the audience/author marker and the
# roster crew slug come from the operator config file, read at import time so
# a missing or malformed file stops this module from validating with the
# wrong operator's identity baked in — `operator_config.get` raises
# `OperatorConfigError`, naming the missing key and the file, uncaught, so
# whatever imported this module exits loudly rather than falling back to a
# default. See `config/operator.example.json` for the key's shape.
OPERATOR_MARKER = operator_config.get("operator_marker")
ROSTER_CREW = operator_config.get("roster_crew")

ALLOWED_KINDS = {
    "agent",
    # A unit an assembler composes into a session's CLAUDE.md, with its own
    # selector fields and rules (`instruction_block_errors` below).
    "instruction-block",
    "skill",
    "architecture",
    "decision",
    "spec",
    "runbook",
    "playbook",
    "reference",
    "prd",
    "brand",
    "overview",
    "legal",
    "marketing",
    "meeting-notes",
    "feature-request",
    # curator / observability-scribe outputs (ARCH-099 crew). Were emitted but
    # never blessed → silently un-indexed until the knowledge-CI validator caught
    # the drift (2026-06-02). Keep in sync with knowledge/scripts/validate.py.
    "observation",
    "proposal",
    # Field Notes (ARCH-289) — conceptual/approach references that explain HOW WE
    # THINK about a part of the software, no code, written to be read outside the team
    # (the agentic system-design doc, Selector's taste map, …). The genre the public
    # "Convivy Lab" site is built from. Keep in sync with knowledge/scripts/validate.py.
    # Dated digests. NOT `reference`: after ARCH-426 step 5 the briefs live in
    # `platform` and `company`, scopes the activation profiles list, so a
    # `kind: reference` rule would individuate every brief ever written into
    # every co. Keep in sync with knowledge scripts/validate.py.
    "brief",
    "field-notes",
    # The bead (platform/decisions/0024) — one per project, the diagram of that
    # project's destination plus the gap to it. Its own kind for the same reason
    # `brief` has one: every project gets exactly one, at
    # `<project>/bead/<project>`, and a rule selecting beads has to be able to
    # name them. Filing them as `overview` would put a second `kind: overview`
    # document beside each project's existing one, which is the stale-overview
    # failure 0024 was designed against. This is an interim home: 0024 will put
    # the bead in the Chart House once Phase 2 gives it a schema. Keep in sync
    # with knowledge scripts/validate.py.
    "bead",
    # The build plan (CQ·139, the operator's 2026-09-17 ruling): a Library
    # document with its own kind, distinct from a scratch checklist that stays
    # out of the KB, so a profile can select one by binds_topics. Keep in sync
    # with knowledge scripts/validate.py.
    "build-plan",
}
# `shared`, `convivy`, `utility` and `engineering` are GONE (ARCH-438), and their
# absence is the end of Phase 0 step 5. Step 5 re-filed every document out of them;
# PR G could not delete them because two still held a live dispatch profile; H1
# relocated those and H2 re-pointed the workspace mappings. The paired knowledge PR
# deletes the directories, so nothing can be filed there.
#
# `engineering-practice` is a DIFFERENT scope and stays. It is cross-PRODUCT
# engineering idiom; `engineering` was a bucket whose 13 documents all moved out.
# The two were never spelled the same for exactly this moment.
ALLOWED_PROJECTS = {"constance",
                    # the constance.team landing page's own project (2026-08-25).
                    # Separate from `constance` so its narrow dispatch profile is
                    # the DEFAULT for that repo rather than an opt-in — an agent
                    # writing public marketing copy must not carry Constance's
                    # company/commercial reference material. Registered here in
                    # the same breath as the KB validator: a project the
                    # validator accepts and the indexer rejects produces
                    # documents that merge green and never index.
                    "constance-landing",
                    "vivy", "sous-chef", "agent-platform",
                    # the personal-apps suite + its platform (KB unification, ARCH-355):
                    "hollis", "selector", "convivy-app-kit", "convivy-services",
                    # non-product project with a team-plane KB slice (ARCH-358):
                    "nyh-studio",
                    # placard (2026-09-05): property instruction pages reached by a
                    # QR code posted at the property. Registered here in the same
                    # breath as knowledge/scripts/validate.py, for the reason the
                    # constance-landing note above gives — the bead merged green
                    # against the validator and then never indexed, because this
                    # copy still rejected it.
                    "placard",
                    # shipwright (2026-09-06, the operator; knowledge#356): the crew that
                    # works Convivy Builder. A slug of its own, for its runbooks
                    # and its dispatch profile — distinct from which product's
                    # BEAD it writes, which is the Builder's. Mirrored here the
                    # same evening the validator gained it, so the profile and
                    # the first runbook index instead of repeating placard's gap.
                    "shipwright",
                    # signals (2026-09-16): the Signals crew, an open internal
                    # product writing derived signal to the Archives. Registered
                    # with the knowledge validator and its daytime profile.
                    "signals",
                    # www (2026-09-12): convivy/www, the public site, Orient's
                    # repo with a narrow project of its own, for the reason the
                    # constance-landing note above gives: a worker writing public
                    # site copy must not carry company or commercial reference
                    # material (security-reviewer, agent-platform#552).
                    # Registered here with the validator, so its profile indexes.
                    "www",
                    # vivy-landing (2026-10-02, orchestrator ruling): the OwnerRez
                    # partner-page repo's narrow project, for the reason the
                    # constance-landing note above gives. Registered here with the
                    # validator, so its profile indexes.
                    "vivy-landing",
                    # the laptop GUI client for the fleet (ARCH-374):
                    "deck",
                    # the public writing guide (ARCH-440). Minted when write-better
                    # split off `platform/daytime` and needed a directory to hold its
                    # own profile. It owns no KB document yet — registered now so the
                    # first one authored is INDEXED rather than silently rejected,
                    # which is this set's failure mode when it lags.
                    "write-better",
                    # the Keel's own projects (ARCH-426). The Keel is FOUR projects —
                    # these three plus `agent-platform`, already listed above because it
                    # was a project before ARCH-426. They carry the platform documents
                    # filed under `shared` today, which re-file onto them in step 5.
                    #
                    # Naming one destination, because a guard now depends on it: the 29
                    # AGENT PROFILES go to `agent-platform`, whose agentctl dispatches
                    # them and whose name the Library's roster must carry.
                    # `kb_writes.PR_ONLY_SEGMENTS` guards that path, so a reader who
                    # concluded from here that the profiles were headed for `substrate`
                    # would be reading a contradiction (reviewer, PR #341).
                    #
                    # The "~85" figure this comment used to carry was wrong and is
                    # dropped: about 15 of the `shared` skills are cross-PRODUCT
                    # engineering practice bound for `engineering-practice`, and the
                    # overview, brand and legal documents are company-altitude.
                    "substrate", "knowledge", "convivy-orient",
                    # `keel` is the project those first two folded INTO on 2026-08-06,
                    # when the steward was retired. It owns no KB document yet and is
                    # registered ahead of one for the reason `write-better` was: the
                    # first document authored under it — the Keel's bead — must INDEX
                    # rather than be silently rejected.
                    #
                    # `substrate` and `knowledge` STAY, deliberately, though neither is
                    # a registry project any more. The KB document migration is a
                    # separate epic, so documents still sit under both slugs; dropping
                    # them here would un-index those documents rather than move them.
                    # `test_kb_slugs_are_registered_projects` asserts
                    # `kb_slugs() - ALLOWED_PROJECTS` is empty — a subset check in one
                    # direction — so a slug with no registry entry is legal here and an
                    # unregistered PROJECT is what it actually catches.
                    "keel",
                    # The Library's own KB slice, from the 2026-08-17 crew split
                    # (the operator). The Keel's two halves each took a crew, and the Library
                    # writes its documents under its own slug from that point. `keel`
                    # STAYS above for the same reason `substrate` and `knowledge` do:
                    # documents already filed there keep validating, and moving them
                    # is a re-filing pass rather than a side effect of a rename.
                    # `bosun` needs no entry — it keeps `agent-platform` as its slice,
                    # so its documents never move. `steward` writes to `platform`,
                    # already legal below as a scope.
                    "library",
                    # one project over the app kit + the backend commercial server
                    # (ARCH-426 amendment); `convivy-app-kit` and `convivy-services`
                    # stay legal until those docs re-file onto `svc`.
                    "svc",
                    # archy (2026-10-02, the operator via the steward): the small-business
                    # agentic ops product, installed by a specialist. Client data
                    # never enters it, per its bead. Registered here in the same
                    # breath as config/projects.json, for the reason the
                    # constance-landing note above gives.
                    "archy",
                    # The NON-PROJECT scopes (ARCH-426 amendments). The Library's scope
                    # set is Project union these, ONE VOCABULARY VALID IN ALL THREE
                    # VAULTS — a Reference Room document may legitimately be scoped
                    # `platform`, the way an Archives signal may be scoped `company`.
                    # `company` supersedes `convivy`, which stays legal until the
                    # step-5 re-filing pass moves its 17 documents.
                    # `engineering-practice` is cross-PRODUCT engineering idiom that
                    # binds every product and belongs to no project (operator 2026-07-31);
                    # it is NOT spelled `engineering`, a live slug whose documents all
                    # move out under the same pass.
                    "company", "platform", "commercial", "engineering-practice"}
# This literal is the BASELINE, not the whole set. `project_catalogue` adds every
# `projects/<name>/` directory the knowledge repo being validated actually holds,
# so a project Convivy has never heard of (an adopter's) validates and indexes
# without a row here. The baseline stays for two reasons: every project below keeps
# validating when its directory is absent from a tree, and
# `check-registration.py` (the new-crew skill) reads this literal.
#
# The knowledge repo's scripts/validate.py still carries its own closed copy and
# gates PRs with it, so a new project's first PR fails THERE until the Library
# changes that script. The indexer no longer needs the matching edit.
ALLOWED_STATUSES = {"draft", "current", "deprecated", "archived"}
# Per-doc publish surface (ARCH-289). Absent = internal (fail-closed): a doc reaches
# the public Convivy Lab projection only with an explicit `visibility: public`.
ALLOWED_VISIBILITY = {"public", "team", "internal"}

# The audience roster is the Library's, served at GET /roster. Admission reads it
# through audience_roster() below, never from this literal.
#
# This literal is the loader's input and nothing else. `library/load_crew_registry.py`
# reads it with ast.literal_eval to seed `roster_agents` when that table is empty
# (`--reseed` overwrites a populated one). Keep it in step with the Library's roster
# so a fresh seed produces the same names. Agent roster per ARCH-012/017
# (+ ARCH-049 product-manager) + the operator marker (human in the loop per ARCH-020).
ALLOWED_AUDIENCE = {
    # Retired from dispatch (issue_01M2GDVPR81Z0X991Z7XD3VR0M, the operator's 2026-09-14
    # ruling): the overnight drain, knowledge-scribe and kb-sync-verifier (step
    # 5), and — per the same epic's comment 2661, also 2026-09-14 — the three
    # overnight stage agents overnight-triage / overnight-fix-drain /
    # overnight-verify. Every dispatcher is deleted: `bin/agentctl`'s
    # `scribe`/`full-monty` command families for the first four,
    # `bin/launch-overnight` (agent-platform#578, merged as adff106) for the
    # stage agents. The names stay here anyway: 100 documents on knowledge/main
    # under `projects/` (74 of them decision records) name one of the first
    # four in `audience`, and 7 more name one of the stage agents, recording
    # history that predates each removal, and the Library will not rewrite
    # that frontmatter. Revoking a name in the Library's roster fails validation
    # for every document still carrying it, within ROSTER_TTL_S of the change.
    # Same rule this file already applies to `telemetry.py`'s VALID_ORIGINS:
    # remove producers of new data, keep readers of the old.
    "overnight-orchestrator",
    "hollis-overnight-orchestrator",
    "knowledge-scribe",
    "kb-sync-verifier",
    "overnight-triage",
    "overnight-fix-drain",
    "overnight-verify",
    "planning-orchestrator",
    "planner",
    "architect",
    "coder",
    "refactorer",
    "reviewer",
    "test-writer",
    "bug-investigator",
    "bug-fixer",
    "design-critic",
    "product-analyst",
    "explainer",
    "curator",
    "observability-scribe",
    "security-reviewer",
    "security-auditor",
    "product-manager",
    # company commercial team (ARCH-124) — company-level positioning + presentation
    "positioning-strategist",
    "commercial-writer",
    # platform build crew (substrate ARCH-099) — tuned for agent-platform code
    "platform-architect",
    "platform-planner",
    "platform-coder",
    # THE PRODUCT CREWS. Every name below is dispatched against a self-contained
    # product repo, and they are listed together because the reason they were
    # separated is expiring: the content move (issue_01M092R482KSQHKKVG4XJ8X257)
    # puts each crew's definitions in the Reference Room, on the operator's directive that
    # all materials for all agents live there. Once a crew's documents are
    # indexed, this roster is what lets the indexer accept them.
    #
    # Registering a name in the Library's roster does NOT index anything. It permits
    # an `audience` value; the document still has to exist and land by PR. So
    # rostering the whole set ahead of the migration is safe, and it is the order that avoids
    # the failure described under `orient-coder` below — a document merging with
    # green CI and never indexing, because the gate lives in this file and the
    # copy a knowledge-repo reviewer can see only warns.
    #
    # deck (ARCH-382 [J6]) was the first product-prefixed crew registered here.
    # `deck-coder` was missing while its two siblings were present, which is the
    # same half-registration shape, caught before it mattered because the crew is
    # not indexed yet.
    "deck-coder",
    "deck-reviewer",
    "deck-security-reviewer",
    # hollis (ARCH-243/177).
    "hollis-architect",
    "hollis-coder",
    "hollis-planner",
    "hollis-reviewer",
    "hollis-security-reviewer",
    # selector (ARCH-243/177). Carries a design critic its siblings do not.
    "sel-architect",
    "sel-coder",
    "sel-design-critic",
    "sel-planner",
    "sel-reviewer",
    "sel-security-reviewer",
    # THE `svc-*` NAMES ARE SHARED BY TWO REPOS AND THE DEFINITIONS ARE NOT THE
    # SAME. convivy-app-kit and convivy-services each carry all five, and the
    # files differ substantively — the kit's non-dependency fence against the
    # services' two-plane cost fence and no-persist rules, not a substituted repo
    # name. They stay distinct through `project:`, since a document id is
    # `<project>/agents/<name>` and a profile include can filter on project. This
    # roster is a permit list of NAMES, so one entry per name is correct here and
    # is not a merge of the two crews.
    "svc-architect",
    "svc-coder",
    "svc-planner",
    "svc-reviewer",
    "svc-security-reviewer",
    # substrate's read-only search lane (project: platform).
    "scout",
    # write-better's prose crew (project: write-better). All FOUR have no KB
    # source and are untracked AND gitignored in their repo, so the Reference Room
    # is about to become their first durable home rather than a second copy.
    #
    # `wb-reviewer` is prefixed and the other three are not, because only it
    # collides. The repo calls it `reviewer`, and it is NOT the shared `reviewer`:
    # it checks the writing guide's internal consistency and carries only
    # Read/Grep/Glob, where the shared one reviews code diffs and posts review
    # stamps. They share a filename and nothing else, which is exactly why the
    # first pass at this migration skipped it as already-sourced. Ten-plus
    # profiles select `kind: agent` with `audience_contains: [reviewer]` and no
    # project filter, so a second document answering to `reviewer` would match all
    # of them and render to the same `reviewer.md` — whichever won would be
    # arbitrary and silent.
    "editor",
    "instructional-designer",
    "rhetorician",
    "wb-reviewer",
    # convivy-orient's crew. Registered here rather than self-contained, because
    # unlike deck/hollis/selector this crew's agents live in the SHARED index
    # (`project: convivy-orient`), so the indexer must accept the audience value
    # or the document never lands and the agent cannot be dispatched at all.
    #
    # THE ROSTER IS READ FROM THE LIBRARY PER DECISION, NOT FROM THIS SET. A roster
    # change reaches the next decision within ROSTER_TTL_S seconds, with nothing
    # restarted (see audience_roster). Before this, the long-running watcher gated
    # on a copy taken at import: an addition dropped documents until a restart, and
    # a revocation kept admitting them. While the Library cannot be read, a document
    # whose only undecided check is its audience or its binds_agents is deferred, not
    # refused: its row is kept as it was, and the first scan or event that reaches
    # the Library decides it. A document with any other refusal is refused as before.
    #
    # THIS ROSTER IS THE GATE, and it failed closed and silent on 2026-08-18.
    # `orient-coder` merged to knowledge/main, knowledge CI passed, and the
    # document never indexed — because the knowledge repo's own validator makes
    # this a WARNING (`scripts/validate.py`, warnings exit 0) while the check at
    # `validate_frontmatter` below makes it an ERROR. The lenient copy is the one
    # visible from inside the knowledge repo, so a reviewer working there is
    # structurally unable to see the strict one. Two reviewers saw the missing
    # roster entry and both under-rated it, in opposite directions.
    # The durable fix — making the two copies agree, or provable — is
    # `issue_01M09MSRMKQX7FEGRHBZW74XC7`.
    "orient-coder",
    # outside-view strategy consultant on the agentic operating model (ARCH-158)
    "consultant",
    # "team" is a special human-audience marker (like the operator marker), NOT an agent role: it
    # tags a doc for the team.convivy.com orientation site. convivy-orient/build.py
    # filters the site to team-tagged docs (substrate ARCH-089).
    "team",
    # "contractor" is the contractor-tier human-audience marker (substrate ARCH-228
    # Phase 5). A doc tagged [team, contractor] shows on both the
    # full team site and the contractor view; team-only docs are hidden from
    # contractors. convivy-orient/build.py gates the contractor render on this marker
    # (contractor_ok = "contractor" in audience) — default-deny, replacing the old
    # CONTRACTOR_HIDDEN_* blocklist.
    "contractor",
}
# OPERATOR_MARKER is a runtime value (read from operator config, not a string
# literal), so it cannot sit inside the set literal above: the Library's
# `load_crew_registry._roster_from_knowledge_py` parses this file with `ast`
# and evaluates the FIRST `ast.Assign` to `ALLOWED_AUDIENCE` it finds via
# `ast.literal_eval`, deliberately, so it never executes agent-platform code.
# A `Name` node in that literal makes `literal_eval` raise. Add the marker
# here instead, after the literal is complete.
ALLOWED_AUDIENCE = ALLOWED_AUDIENCE | {OPERATOR_MARKER}

# The instruction-block rules below MIRROR `scripts/validate.py` in
# convivy/knowledge (knowledge#527, corrected by #529). Knowledge CI refuses a bad
# block before merge, and these copies refuse it again at index time, because a
# document can reach this checkout without passing that CI: the knowledge ruleset
# exempts org admins. Keep the two in sync.

# The four roster values naming a PERSON or a group rather than a dispatchable
# agent. `audience` says who may read a document and `binds_agents` says who must
# obey a block; the operator reads every document and obeys none, so these are valid
# audiences and never valid in `binds_agents`.
NON_AGENT_AUDIENCE = {"consultant", "team", "contractor"}
# Written as a literal plus a union to match ALLOWED_AUDIENCE above, for
# consistency: no reader outside this repo parses NON_AGENT_AUDIENCE.
NON_AGENT_AUDIENCE = NON_AGENT_AUDIENCE | {OPERATOR_MARKER}
# The derivation's weak point is the next PERSON added to the Library's roster, who
# becomes bindable unless also added above. A set-partition assertion cannot catch
# that (it holds whatever was added), so `test_every_audience_value_is_classified`
# pins the roster's size and forces the decision instead.
# `binds_roles` values: the role vocabulary of the knowledge repo's own
# `scripts/validate.py` (`ALLOWED_ROLES`), which validates the `role` field of every
# `kind: agent` document. A ROLE IS DECLARED ON THE AGENT DOCUMENT, so nothing in
# this repo holds a roster to derive it from, and a set collected from the agent
# documents would validate every typo it found. This is therefore a hand-kept
# copy, pinned by size in `tests/test_knowledge.py`. Change it only by copying
# the knowledge repo's set.
ALLOWED_ROLES = {
    "architect", "bug-fixer", "bug-investigator", "coder", "commercial-writer",
    "consultant", "curator", "design-critic", "editor", "explainer",
    "instructional-designer", "observability-scribe", "orchestrator",
    "planner", "positioning-strategist", "product-analyst", "product-manager",
    "refactorer", "reviewer", "rhetorician", "scout", "security-auditor",
    "security-reviewer", "test-writer", "writing-reviewer",
}
# `binds_crews` values: EXACTLY the `crew` fields in config/projects.json, which is
# what a session's crew is. Not project slugs (`bosun` works `agent-platform`) and
# not the name a crew goes by in prose (`orient` is `convivy-orient`). The first
# knowledge-side copy was written from memory and was wrong on eleven names, four
# of which validated and bound no session. `test_allowed_crews_is_the_registrys_
# crew_set` in tests/test_projects.py holds this equal to the registry both ways.
ALLOWED_CREWS = {
    "archy", "bosun", "cfo", "constance", "convivy-commercial", "convivy-orient",
    "deck", "ea", "hollis", ROSTER_CREW, "library", "nyh-arch-tools", "nyh-etc",
    "nyh-park-villa", "placard", "selector", "shipwright", "signals", "sous-chef",
    "steward", "svc", "vivy", "write-better",
}

REQUIRED = ("id", "kind", "project", "title", "status")
LIST_FIELDS = ("topics", "audience", "applies_to", "depends_on", "authors", "tools",
               "binds_crews", "binds_agents", "binds_topics", "binds_sessions",
               "binds_roles")
# The session kinds for `binds_sessions` (instruction blocks only). A session is
# either a crew session with an operator present, or a run started by
# `agentctl spawn`. The assembler supplies which one it is; nothing here can tell.
#
# CLOSED AT TWO VALUES ON PURPOSE, mirroring the knowledge repo's own
# `scripts/validate.py`. A third — `scheduled`, say — is added when a block needs
# it and not before, because an unused selector value is a filter nobody has
# exercised that still validates.
ALLOWED_SESSION_KINDS = {"interactive", "dispatched"}
# Instruction-block scopes, widest first. THIS IS COMPOSITION ORDER ONLY: an
# assembled CLAUDE.md lists fleet blocks, then crew, agent, task. It is not an
# override order, and `fleet` is never overridden (the steward's ruling as owner
# of epic issue_01M2MBQ8Q7YX34DDF8H9YM5T58): a narrower block may add to or narrow
# a wider non-fleet block and may never contradict the floor. No check enforces
# that, since a validator cannot tell whether two paragraphs contradict; it is
# stated so nothing reads the order as permission. `fleet` binds every agent, so
# it is the one scope that carries no selector of its own.
INSTRUCTION_BLOCK_SCOPES = ("fleet", "crew", "agent", "task")
# Phrases that only make sense beside a neighbour the assembled bundle may not
# contain. Literal phrases rather than a pattern, because a false positive makes a
# rule impossible to phrase.
RELATIVE_CROSS_REFERENCES = (
    "see above", "see below",
    "as above", "as below",
    "noted above", "noted below",
    "mentioned above", "mentioned below",
    "described above", "described below",
    "the section above", "the section below",
    "the above section", "the below section",
)
# A block's body is re-read every turn. Set above the largest rule the steward's
# decomposition treats as one block (2,997 bytes) and below the sections it flags
# for splitting (5,385 bytes and up).
INSTRUCTION_BLOCK_BODY_BUDGET_BYTES = 4096
# A project slug is one directory name under `projects/` that fits the id syntax
# below: the same first-character and character rules as an id's kind segment.
# Anything else is refused by `project_dir_refusal` rather than admitted by a wider
# pattern. Anchored with `\Z` and matched with `fullmatch`: `$` also matches before
# a trailing newline, so `.match` let `foo\n` past every name refusal below, and
# the patterns are handed to callers that use `.match` (crew_doctor).
_PROJECT_SLUG = re.compile(r"^[a-z][a-z-]*\Z")
# The scopes ARCH-438 retired (comment above ALLOWED_PROJECTS). A directory with
# one of these names is refused even though it matches the slug syntax.
RETIRED_PROJECTS = frozenset({"shared", "convivy", "utility", "engineering"})


def id_pattern_for(projects) -> re.Pattern:
    """The id pattern whose project alternatives are exactly `projects`. Every
    member must already satisfy `_PROJECT_SLUG`, so nothing is escaped."""
    return re.compile(
        r"^(" + "|".join(sorted(projects)) + r")/[a-z][a-z-]*/[a-z0-9][a-z0-9-]*\Z")


# Module-level, for readers with no tree in hand: the baseline set's pattern.
ID_PATTERN = id_pattern_for(ALLOWED_PROJECTS)


class ProjectCatalogue(NamedTuple):
    """The projects one knowledge tree admits. `allowed` is the baseline plus the
    tree's valid `projects/` directories; `id_pattern` is built from `allowed`;
    `refused` maps each directory entry that was NOT admitted to why."""
    allowed: frozenset
    id_pattern: re.Pattern
    refused: dict


def private_crew_names() -> frozenset | None:
    """Every casefolded name a private (black-box) crew answers to, read from the
    registry through `projects._private_crew_names`, the set
    `projects.default_profile_refused` compares against: slug, display name, crew,
    aliases, repos and home-directory basenames. `None` when the registry cannot
    be read, which `_name_refusal` treats as refusing every name outside the
    baseline.

    `projects` is imported here, not at module top, so importing this module never
    fails for want of it. Every consumer that validates (`bin/agentctl`,
    `bin/knowledge-watcher`, `bin/mcp-kb-server`, `bin/knowledge-ctx`) puts
    lib/agent-platform on `sys.path` first, as the watcher does for `venv_bootstrap`;
    a caller that has not gets `None`, and so a closed catalogue rather than an open
    one."""
    try:
        import projects  # noqa: PLC0415
        return frozenset(projects._private_crew_names())
    except Exception:
        return None


def _name_refusal(name: str) -> str | None:
    """Why `name` cannot be a project slug whatever is on disk, or None."""
    if name in RETIRED_PROJECTS:
        return (f"'{name}' is a retired scope (ARCH-438); nothing may be filed "
                f"under it")
    # Before the slug rule, so `EA` names the privacy wall and not the casing.
    private = private_crew_names()
    if private is None:
        if name not in ALLOWED_PROJECTS:
            return (f"'{name}' cannot be admitted: the project registry could not "
                    f"be read, so private crews cannot be told apart "
                    f"(platform/decisions/0043)")
    elif name.casefold() in private:
        return (f"'{name}' is a private crew (platform/decisions/0043); none of "
                f"its content may reach the Library")
    if not _PROJECT_SLUG.fullmatch(name):
        return (f"'{name}' is not a plain lowercase directory name "
                f"(expected [a-z][a-z-]*, the id syntax's project segment)")
    return None


def project_dir_refusal(root: Path, name: str) -> str | None:
    """Why `<root>/projects/<name>` is not a project directory, or None when it is.
    `root` is the knowledge repo being validated, never assumed to be the live one."""
    why = _name_refusal(name)
    if why:
        return why
    path = Path(root) / "projects" / name
    if not path.is_dir():
        return f"'{name}' is not a directory"
    try:
        path.resolve().relative_to(Path(root).resolve())
    except ValueError:
        return f"'{name}' is a symlink pointing outside the knowledge repo"
    return None


def project_catalogue(root: Path, pending: str | None = None) -> ProjectCatalogue:
    """Read the projects the knowledge repo at `root` holds. A missing or unreadable
    `projects/` admits only the baseline. `pending` names a directory a write is
    about to create; when it is not on disk it is admitted on its name alone."""
    allowed = set(ALLOWED_PROJECTS)
    refused: dict[str, str] = {}
    try:
        names = sorted(p.name for p in (Path(root) / "projects").iterdir())
    except OSError:
        names = []
    for name in names:
        why = project_dir_refusal(root, name)
        if why is None:
            allowed.add(name)
        else:
            refused[name] = why
    if pending and pending not in names:
        why = _name_refusal(pending)
        if why is None:
            allowed.add(pending)
        else:
            refused[pending] = why
    for name in RETIRED_PROJECTS | (private_crew_names() or frozenset()):
        refused.setdefault(name, project_dir_refusal(root, name))
    return ProjectCatalogue(frozenset(allowed), id_pattern_for(allowed), refused)


def project_dir_errors(catalogue: ProjectCatalogue, rel: str) -> list[str]:
    """The error for a document at repo-relative `rel` whose top `projects/`
    directory the catalogue refused; `[]` otherwise."""
    parts = rel.split("/")
    if len(parts) > 2 and parts[0] == "projects" and parts[1] in catalogue.refused:
        return [f"directory refused: {catalogue.refused[parts[1]]}"]
    return []


ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}\Z")


# The roster the Library serves at GET /roster (`roster.read`, granted to the agent
# and system roles). Admission reads it here, at most once per ROSTER_TTL_S, so a change in
# the Library reaches the next read on every machine with nothing pulled or restarted.
# Both a success and a failed read are cached for ROSTER_TTL_S seconds. A pass keeps the
# read it starts with (`RosterRead`), so the bound is per pass, not per document: a pass
# that outlasts it, as a full scan can while it waits on the writer lock, decides its later
# documents on a read older than this. One full scan validates every document, so a request
# per document would hit the Library hundreds of times for one answer, and during an outage
# each request would wait out ROSTER_TIMEOUT_S before it failed.
ROSTER_TTL_S = 30
ROSTER_TIMEOUT_S = 6
# (read at, the names or None for a failed read, why the read failed).
_roster_cache: tuple[float, frozenset[str] | None, str] | None = None


class RosterUnavailable(RuntimeError):
    """The Library's roster could not be read, or was not a usable answer.

    `errors` holds the refusals the document had before the roster was needed. When
    it is non-empty the document is refused on them, and the roster cannot change
    that. When it is empty the caller defers the document: its row is left as it is,
    and nothing falls back to a copy of the roster."""

    def __init__(self, message: str, errors: list[str] | None = None):
        super().__init__(message)
        self.errors = list(errors or [])


def _fetch_roster() -> frozenset[str]:
    """One GET /roster, on the same credential `policy_client` uses.

    An empty list is a failed read, not an empty roster. The Library's `roster_agents`
    is empty until `load_crew_registry.py` seeds it, and admitting against that would
    refuse every document that names an agent and prune its row."""
    try:
        import signal_client
        import vault_plane
    except Exception as exc:  # noqa: BLE001 - its refusals are RuntimeError/SystemExit
        raise RosterUnavailable(f"cannot address the Library: {exc}") from exc
    tok = signal_client.token()
    if not tok:
        raise RosterUnavailable("no Library credential")
    req = urllib.request.Request(
        vault_plane.library_url("/roster"), headers=vault_plane.library_headers(tok))
    try:
        with urllib.request.urlopen(req, timeout=ROSTER_TIMEOUT_S) as resp:
            body = json.loads(resp.read())
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
        # HTTPException is not an OSError: a body cut off mid-read raises IncompleteRead.
        raise RosterUnavailable(f"GET /roster failed: {exc}") from exc
    rows = body.get("agents") if isinstance(body, dict) else None
    if not isinstance(rows, list) or not rows:
        raise RosterUnavailable("GET /roster answered without any agents")
    names = set()
    for row in rows:
        name = row.get("name") if isinstance(row, dict) else None
        if not isinstance(name, str) or not name.strip():
            raise RosterUnavailable("GET /roster answered a row without a usable name")
        names.add(name)
    return frozenset(names)


def audience_roster() -> frozenset[str]:
    """The roster as the Library serves it, at most ROSTER_TTL_S seconds old.
    Raises RosterUnavailable when the Library could not answer, whether that failure
    was read now or within the bound."""
    global _roster_cache
    now = time.monotonic()
    if _roster_cache is None or now - _roster_cache[0] >= ROSTER_TTL_S:
        try:
            _roster_cache = (now, _fetch_roster(), "")
        except RosterUnavailable as exc:
            _roster_cache = (now, None, str(exc))
    _, names, why = _roster_cache
    if names is None:
        raise RosterUnavailable(why)
    return names


class RosterRead:
    """The roster as one pass sees it. The first call to `names()` or `unreadable()`
    reads it, and every later call in the same pass gets that answer, so a Library that
    goes down partway through the pass cannot defer a document the pass started with a
    readable roster for. A pass calls `unreadable()` once, at its start, to decide its
    mode, and passes this object to every document it checks."""

    def __init__(self) -> None:
        self._read = False
        self._names: frozenset[str] | None = None
        self._why = ""

    def names(self) -> frozenset[str]:
        if not self._read:
            self._read = True
            try:
                self._names = audience_roster()
            except RosterUnavailable as exc:
                self._why = str(exc)
        if self._names is None:
            raise RosterUnavailable(self._why)
        return self._names

    def unreadable(self) -> bool:
        try:
            self.names()
        except RosterUnavailable:
            return True
        return False


def allowed_agents(roster: RosterRead | None = None) -> frozenset[str]:
    """The roster minus the people and groups: the names `binds_agents` may name."""
    read = RosterRead() if roster is None else roster
    return read.names() - NON_AGENT_AUDIENCE


def _is_one_of(value: Any, allowed: set[str]) -> bool:
    """Membership that refuses rather than raises on a non-string value."""
    return isinstance(value, str) and value in allowed


def validate_frontmatter(fm: Any, catalogue: ProjectCatalogue | None = None,
                         roster: RosterRead | None = None) -> list[str]:
    """Return a list of error strings; empty list means the doc is valid.

    `catalogue` is the project set of the tree the document lives in
    (`project_catalogue`); without one the baseline `ALLOWED_PROJECTS` applies.
    `roster` is the pass's read (`RosterRead`); without one the check reads the roster
    itself. Raises RosterUnavailable when an `audience` must be checked and the roster
    cannot be read."""
    if not isinstance(fm, dict):
        return ["frontmatter is not a YAML mapping"]

    errs: list[str] = []

    for field in REQUIRED:
        if field not in fm or fm[field] in (None, ""):
            errs.append(f"missing required field: {field}")
        elif not isinstance(fm[field], str):
            # Each is bound to a text column. A list or mapping passed every other
            # check and raised in sqlite3 at upsert, mid-scan.
            errs.append(f"{field} must be a string: {fm[field]!r}")

    # Every enum test checks for a string first. A YAML list or mapping is
    # unhashable, and testing one against a set raises TypeError, which takes down
    # the indexer's whole-tree scan instead of refusing one document
    # (security-reviewer, #601).
    if "kind" in fm and not _is_one_of(fm["kind"], ALLOWED_KINDS):
        errs.append(f"invalid kind: {fm['kind']}")
    allowed = catalogue.allowed if catalogue else ALLOWED_PROJECTS
    id_pattern = catalogue.id_pattern if catalogue else ID_PATTERN
    if "project" in fm and not _is_one_of(fm["project"], allowed):
        # `_name_refusal` as well, so `project: EA` gets the privacy reason though
        # only the casefolded name is in `refused`.
        why = (catalogue.refused.get(fm["project"]) or _name_refusal(fm["project"])
               if catalogue and isinstance(fm["project"], str) else None)
        errs.append(f"invalid project: {fm['project']}" + (f" ({why})" if why else ""))
    if "status" in fm and not _is_one_of(fm["status"], ALLOWED_STATUSES):
        errs.append(f"invalid status: {fm['status']}")
    if "visibility" in fm and not _is_one_of(fm["visibility"], ALLOWED_VISIBILITY):
        errs.append(f"invalid visibility: {fm['visibility']}")

    doc_id = fm.get("id")
    if isinstance(doc_id, str) and not id_pattern.fullmatch(doc_id):
        errs.append(
            f"invalid id format (expected <project>/<kind-plural>/<slug>): {doc_id}"
        )

    for field in LIST_FIELDS:
        if field in fm and not isinstance(fm[field], list):
            errs.append(f"{field} must be a list")
        elif field in fm:
            # An unquoted date in YAML is a `date`, and a stray `-` nests a list;
            # neither is a usable entry. None of the 805 documents on 2026-09-16
            # held a non-string entry (security-reviewer, #601 round 2).
            for item in fm[field]:
                if not isinstance(item, str):
                    errs.append(f"{field} entries must be strings: {item!r}")

    lr = fm.get("last_reviewed")
    if lr is not None and not isinstance(lr, date):
        if not (isinstance(lr, str) and ISO_DATE.fullmatch(lr)):
            errs.append(f"last_reviewed must be YYYY-MM-DD: {lr!r}")

    # Last, so every refusal above is in `errs` when a RosterUnavailable carries it out.
    audience = fm.get("audience")
    if isinstance(audience, list) and any(isinstance(v, str) for v in audience):
        read = RosterRead() if roster is None else roster
        try:
            names = read.names()
        except RosterUnavailable as exc:
            raise RosterUnavailable(str(exc), errs) from exc
        for value in audience:
            if isinstance(value, str) and value not in names:
                errs.append(f"audience value not in roster: {value}")

    return errs


def instruction_block_errors(fm: Any, body: str,
                             roster: RosterRead | None = None) -> list[str]:
    """The rules unique to `kind: instruction-block`: who it binds, its scope,
    that it stands alone, and that it stays small. `validate_frontmatter` covers
    the generic shape. Empty list means the block is usable. Every rule is an
    error, because a block that binds nobody, or drags a dangling reference into
    an assembled CLAUDE.md, looks valid while doing nothing. `roster` is the pass's
    read (`RosterRead`), as for `validate_frontmatter`."""
    if not isinstance(fm, dict):
        return []
    errs: list[str] = []

    scope = fm.get("scope")
    if scope not in INSTRUCTION_BLOCK_SCOPES:
        errs.append(f"instruction-block requires scope, one of "
                    f"{INSTRUCTION_BLOCK_SCOPES}: {scope!r}")
        scope = None

    crews = fm.get("binds_crews") or []
    agents = fm.get("binds_agents") or []
    topics = fm.get("binds_topics") or []
    sessions = fm.get("binds_sessions") or []
    roles = fm.get("binds_roles") or []

    if isinstance(crews, list):
        for val in crews:
            if not _is_one_of(val, ALLOWED_CREWS):
                errs.append(f"binds_crews names an unknown crew: {val}")
    # Names only the roster can decide. A person is refused here, and a non-string
    # is refused here, so neither needs the roster.
    roster_agents: list[str] = []
    if isinstance(agents, list):
        for val in agents:
            if _is_one_of(val, NON_AGENT_AUDIENCE):
                errs.append(f"binds_agents names a person or a group, not an "
                            f"agent: {val} — a block binds what obeys it, and "
                            f"a person is an audience rather than an agent")
            elif isinstance(val, str):
                roster_agents.append(val)
            else:
                errs.append(f"binds_agents names an unknown agent: {val}")
    # Topics have no roster, so a blank entry (a bare `-` in YAML parses to None)
    # would otherwise count as a selector while matching no work item.
    if isinstance(topics, list):
        for val in topics:
            if not isinstance(val, str) or not val.strip():
                errs.append(f"binds_topics entry is not a usable topic: "
                            f"{val!r} — it would count as a selector while "
                            f"matching no work item")
    # `binds_sessions` IS CHECKED AGAINST A ROSTER LIKE CREWS AND AGENTS, not
    # given the blank-entry floor topics get, because its vocabulary is closed:
    # a blank or unknown entry fails the membership test on the same line.
    if isinstance(sessions, list):
        for val in sessions:
            if not _is_one_of(val, ALLOWED_SESSION_KINDS):
                errs.append(f"binds_sessions names an unknown session kind: "
                            f"{val!r} — one of "
                            f"{sorted(ALLOWED_SESSION_KINDS)}")
    # `binds_roles` is checked against the closed ALLOWED_ROLES. The blank-entry
    # floor is stated apart so the message names the real slip (a bare `-` parses
    # to None) rather than "unknown role: None".
    if isinstance(roles, list):
        for val in roles:
            if not isinstance(val, str) or not val.strip():
                errs.append(f"binds_roles entry is not a usable role: {val!r} — "
                            f"it would count as a selector while matching no "
                            f"agent")
            elif val not in ALLOWED_ROLES:
                errs.append(f"binds_roles names an unknown role: {val} — one "
                            f"of {sorted(ALLOWED_ROLES)}")

    # Counted from entries that could select something, not from list length.
    def _usable(values: Any) -> bool:
        return isinstance(values, list) and any(
            isinstance(v, str) and v.strip() for v in values)

    has_selector = (_usable(crews) or _usable(agents) or _usable(topics)
                    or _usable(sessions) or _usable(roles))
    if scope == "fleet":
        if has_selector:
            errs.append("scope: fleet is the floor and binds every agent; it "
                        "must not also carry binds_crews, binds_agents, "
                        "binds_topics, binds_sessions or binds_roles")
    elif scope is not None and not has_selector:
        errs.append("a non-fleet instruction-block needs at least one of "
                    "binds_crews, binds_agents, binds_topics, binds_sessions, "
                    "binds_roles — a block matching nothing binds nothing")

    lower_body = (body or "").lower()
    for phrase in RELATIVE_CROSS_REFERENCES:
        if phrase in lower_body:
            errs.append(f"body contains a relative cross-reference "
                        f"({phrase!r}) — an instruction block must stand "
                        f"alone since its neighbour may not be in the "
                        f"assembled bundle")

    size = len((body or "").strip().encode("utf-8"))
    if size > INSTRUCTION_BLOCK_BODY_BUDGET_BYTES:
        errs.append(f"body is {size} bytes, over the "
                    f"{INSTRUCTION_BLOCK_BODY_BUDGET_BYTES}-byte "
                    f"instruction-block budget — split it")

    # The roster is read last, so every refusal above is in `errs` when a
    # RosterUnavailable carries it out.
    if roster_agents:
        try:
            known_agents = allowed_agents(roster)
        except RosterUnavailable as exc:
            raise RosterUnavailable(str(exc), errs) from exc
        errs += [f"binds_agents names an unknown agent: {val}"
                 for val in roster_agents if val not in known_agents]

    return errs
