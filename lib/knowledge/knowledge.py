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
# Unlike ROSTER_CREW, not every install has an apps crew — a missing key
# means this install has none, never a reason to fail the import. `None`
# here is why ALLOWED_CREWS below only adds this when it's a non-empty string.
APPS_CREW = operator_config.get_optional("apps_crew")

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


def _legal_projects() -> frozenset[str]:
    """Every KB project slug a validator must admit (platform/decisions/0097):
    `projects.legal_kb_projects()` — `kb_slugs()` (every central/split unit's
    own `kb_project`) union `scopes()` (the non-project scopes: `company`,
    `platform`, `commercial`, `engineering-practice`) union `_kb_legacy`
    (the project registry) — slugs that still carry documents but name no
    unit's own `kb_project`.

    `projects` is imported here, not at module top, for the same reason
    `private_crew_names()` imports it lazily: importing this module must never
    fail for want of it. An empty frozenset comes back when the registry
    cannot be read, so every project name is then refused by `_name_refusal`
    with the "could not be read" reason — a closed catalogue, the same failure
    mode `private_crew_names` documents."""
    try:
        import projects  # noqa: PLC0415
        return frozenset(projects.legal_kb_projects())
    except Exception:
        return frozenset()


def _current_allowed_projects() -> frozenset[str]:
    """The legal project set as read RIGHT NOW, not the set this module saw at
    import. `_name_refusal` and `project_catalogue` call this rather than
    `ALLOWED_PROJECTS` so a crew `agentctl crew register`/`crew create`
    registers in THIS process — `crew create` runs the doctor's KB-slug check
    straight after registering, in the same process — is admitted without a
    restart, and so a long-running reader (`bin/knowledge-watcher`,
    `bin/mcp-kb-server`) picks up a newly registered project on its next call
    rather than needing one.

    Reads through `projects.legal_kb_projects_live()`, which sees a change to
    the project registry on the very next call with nothing to clear —
    never `legal_kb_projects()`'s cached path, which the two daemons above
    never clear, and never `ALLOWED_PROJECTS`, the set captured at import. An
    empty frozenset on a failed read is returned AS
    IS, never patched up with `ALLOWED_PROJECTS`: platform/decisions/0097,
    point 2, refuses a name outside the registry's set whether or not the
    registry is readable, and an unreadable registry at import already gives
    an empty set that refuses every name the same way (reviewer +
    security-reviewer, PR #978 round 1 — the import-time fallback this
    replaced admitted an already-known name through an outage that this
    module's own import-time behaviour would have refused)."""
    try:
        import projects  # noqa: PLC0415
        return frozenset(projects.legal_kb_projects_live())
    except Exception:
        return frozenset()


# THE IMPORT-TIME BASELINE. Nothing in this module treats it as current:
# `_name_refusal` and `project_catalogue` call `_current_allowed_projects()`,
# not this name, so a project the registry names AFTER this module's own
# import — in the same process or in a long-running one — is admitted on the
# next call rather than the next restart (platform/decisions/0097, point 2;
# the gap this closes is recorded there as the crew-create doctor misreading
# its own crew).
#
# `bin/agentctl`'s readiness probe (`hasattr(K, "ALLOWED_PROJECTS")`) and the
# tests that compare against a known baseline are the remaining readers of the
# name itself.
ALLOWED_PROJECTS = _legal_projects()
ALLOWED_STATUSES = {"draft", "current", "deprecated", "archived"}
# Per-doc publish surface (ARCH-289). Absent = internal (fail-closed): a doc reaches
# the public Convivy Lab projection only with an explicit `visibility: public`.
ALLOWED_VISIBILITY = {"public", "team", "internal"}

# The audience roster is the Library's, served at GET /roster. Admission reads it
# through audience_roster() below, never from this literal.
#
# This literal is the FRESH-INSTALL SEED for the Library's roster and nothing else:
# `library/load_crew_registry.py roster <path>` reads it with ast.literal_eval to
# populate `roster_agents` once, when that table is empty, and Shipwright's
# jobs/library.toml runs that loader on a fresh install. A crew's own agents reach
# the live roster afterward through PUT /roster/{name}, never by editing this file.
ALLOWED_AUDIENCE = {
    # Builder's four starter agents, shipped at
    # payload/knowledge-seed/projects/platform/agents in convivy/shipwright, and
    # named in payload/knowledge-seed/projects/platform/profiles/default.yaml's
    # `audience_contains` list.
    "planning-orchestrator",
    "coder",
    "reviewer",
    "security-reviewer",
    # shadow-reviewer is a literal in bin/agentctl's own dispatch logic
    # (`_SHADOW_REVIEW_AGENTS`, `_is_review_agent`), not only a crew's roster
    # entry, so the engine recognizes it on a fresh install before any crew
    # defines it.
    "shadow-reviewer",
    # scout is a literal in lib/agent-platform/tool_policy.py's
    # `ENFORCEABLE_AGENTS`, the same engine-level recognition shadow-reviewer
    # gets above: the tool-policy enforcement an agent named "scout" receives
    # does not wait on a crew's roster entry.
    "scout",
    # consultant is a dispatchable agent (agent-platform/agents/consultant, the
    # outside strategy consultant), valid in both audience and binds_agents. team
    # and contractor are the groups NON_AGENT_AUDIENCE (below) needs: the
    # team-wide marker and the contractor-tier marker.
    "consultant",
    "team",
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

# The roster values naming a group rather than a dispatchable agent. `audience`
# says who may read a document and `binds_agents` says who must obey a block; the
# operator reads every document and obeys none, so these are valid audiences and
# never valid in `binds_agents`.
NON_AGENT_AUDIENCE = {"team", "contractor"}
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
# `binds_crews` values: EXACTLY the `crew` fields of the project registry, which
# is what a session's crew is. Not project slugs (`widget` works `widget-ops`) and
# not the name a crew goes by in prose (`northwind` is `acme-northwind`).
#
# CQ·586: DERIVED from the registry (`projects.legal_crews()`) rather than a
# hand-kept literal. The first knowledge-side copy was written from memory and
# was wrong on eleven names, four of which validated and bound no session — a
# derived set cannot drift from the registry it is drawn from the way a
# hand-kept copy could.


def _legal_crews() -> frozenset[str]:
    """Every unit's own `crew` field in the project registry, as of this
    module's own import.

    `projects` is imported here, not at module top, for the same reason
    `_legal_projects()` imports it lazily: importing this module must never
    fail for want of it. An empty frozenset comes back when the registry
    cannot be read, so every crew name is then refused by
    `instruction_block_errors` — a closed catalogue, never "every crew
    allowed" (the same failure mode `_legal_projects()` documents for
    projects)."""
    try:
        import projects  # noqa: PLC0415
        return frozenset(projects.legal_crews())
    except Exception:
        return frozenset()


def _current_allowed_crews() -> frozenset[str]:
    """The legal crew set as read RIGHT NOW, not the set this module saw at
    import. `instruction_block_errors` calls this rather than
    `ALLOWED_CREWS` so a crew the registry gains or loses after this
    module's import is seen on the very next call rather than needing a
    restart — a long-running reader (`bin/knowledge-watcher`,
    `bin/mcp-kb-server`) that started while the registry was unreadable
    would otherwise refuse every registered crew's `binds_crews` documents
    for its whole run (reviewer + security-reviewer, PR #1065 round 1).

    Reads through `projects.legal_crews_live()`, which sees a change to the
    project registry on the very next call with nothing to clear — never
    `legal_crews()`'s cached path, which the two daemons above never clear,
    and never `ALLOWED_CREWS`, the set captured at import. An unreadable
    registry at call time yields an empty registry-derived set, exactly as
    `_current_allowed_projects()` does on its own failed read (returned AS
    IS, never patched up with a baseline): platform/decisions/0097, point 2,
    refuses a name outside the live set whether or not the registry is
    readable. `ROSTER_CREW` and `APPS_CREW` are not registry-derived — they
    come from this install's own operator config — so they are added after,
    unconditionally, exactly as `ALLOWED_CREWS` adds them below."""
    try:
        import projects  # noqa: PLC0415
        crews = frozenset(projects.legal_crews_live())
    except Exception:
        crews = frozenset()
    crews = crews | {ROSTER_CREW}
    if isinstance(APPS_CREW, str) and APPS_CREW:
        crews = crews | {APPS_CREW}
    return crews


# THE IMPORT-TIME BASELINE. Nothing in this module treats it as current:
# `instruction_block_errors` calls `_current_allowed_crews()`, not this name,
# so a crew the registry gains or loses AFTER this module's own import — in
# the same process or in a long-running one — is seen on the next call
# rather than the next restart (platform/decisions/0097, point 2, applied
# here the way it already is for `ALLOWED_PROJECTS`/`_current_allowed_projects()`).
#
# `bin/agentctl`'s readiness probe and the tests that compare against a
# known baseline are the remaining readers of the name itself.
ALLOWED_CREWS = _legal_crews() | {ROSTER_CREW}
# APPS_CREW is None on an install with no apps crew (see above) — added only
# when it names one, so the set itself never holds None.
if isinstance(APPS_CREW, str) and APPS_CREW:
    ALLOWED_CREWS = ALLOWED_CREWS | {APPS_CREW}

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
# `shared`, `convivy`, `utility` and `engineering` are GONE (ARCH-438), and their
# absence is the end of Phase 0 step 5. Step 5 re-filed every document out of them;
# PR G could not delete them because two still held a live dispatch profile; H1
# relocated those and H2 re-pointed the workspace mappings. The paired knowledge PR
# deletes the directories, so nothing can be filed there. A directory with one of
# these names is refused even though it matches the slug syntax above.
#
# `engineering-practice` is a DIFFERENT scope and stays. It is cross-PRODUCT
# engineering idiom; `engineering` was a bucket whose 13 documents all moved out.
# The two were never spelled the same for exactly this moment.
RETIRED_PROJECTS = frozenset({"shared", "convivy", "utility", "engineering"})


def id_pattern_for(projects) -> re.Pattern:
    """The id pattern whose project alternatives are the members of `projects`
    that satisfy `_PROJECT_SLUG`, each `re.escape`d.

    `projects` is registry-controlled (a unit's own `kb_project`, among other
    sources — see `_legal_projects()`), and `crew_registration.build_row`
    validated `slug` but not `kb_project` until PR #978 round 2, so a value
    that is not valid regex syntax (an unbalanced paren, say) could reach
    here. Escaping closes the `re.compile()` crash that value caused at this
    module's own import, which took down agentctl, the watcher, the KB
    server, and kb apply and kb sync (security-reviewer, PR #978 round 2). A
    member that fails `_PROJECT_SLUG` is left out of the alternation
    entirely, rather than escaped in: it is refused by `_name_refusal`'s own
    slug check regardless, so admitting it here would only widen the pattern
    for no gain."""
    valid = [p for p in projects if _PROJECT_SLUG.fullmatch(p)]
    return re.compile(
        r"^(" + "|".join(re.escape(p) for p in sorted(valid)) + r")/"
        r"[a-z][a-z-]*/[a-z0-9][a-z0-9-]*\Z")


# Module-level, for readers with no tree in hand: the baseline set's pattern.
ID_PATTERN = id_pattern_for(ALLOWED_PROJECTS)


class ProjectCatalogue(NamedTuple):
    """The projects one knowledge tree admits: always exactly the CURRENT legal
    set, `_current_allowed_projects()` as of this call (platform/decisions/0097
    closes the set to what the registry names, so no tree ever adds to it).
    `id_pattern` is built from `allowed`; `refused` maps each directory entry —
    and the `pending` name of a write — the registry does not name, or whose
    own entry fails a filesystem check, to why."""
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
    # Before the slug rule, so a private crew's own-case slug names the privacy wall and not the casing.
    private = private_crew_names()
    allowed = _current_allowed_projects()
    if private is None:
        if name not in allowed:
            return (f"'{name}' cannot be admitted: the project registry could not "
                    f"be read, so private crews cannot be told apart "
                    f"(platform/decisions/0043)")
    elif name.casefold() in private:
        return (f"'{name}' is a private crew (platform/decisions/0043); none of "
                f"its content may reach the Library")
    if not _PROJECT_SLUG.fullmatch(name):
        return (f"'{name}' is not a plain lowercase directory name "
                f"(expected [a-z][a-z-]*, the id syntax's project segment)")
    if name not in allowed:
        return (f"'{name}' is not a project the registry names "
                f"(platform/decisions/0097)")
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
    """`allowed` is always the CURRENT legal set (`_current_allowed_projects()`,
    a live read): `root`'s own `projects/` directories name nothing this
    registry does not already admit (platform/decisions/0097). What `root`
    decides is `refused` — the reason recorded against each directory actually
    on disk, and against `pending` (a write's not-yet-created directory name),
    that the registry excludes or whose own on-disk entry fails a filesystem
    check — for a caller reporting why a document under it was dropped."""
    allowed = set(_current_allowed_projects())
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
        # `_name_refusal` as well, so a private crew's own-case slug gets the
        # privacy reason though only the casefolded name is in `refused`.
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
        allowed_crews = _current_allowed_crews()
        for val in crews:
            if not _is_one_of(val, allowed_crews):
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
