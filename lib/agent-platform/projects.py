"""The project registry (ARCH-358, remodelled by ARCH-426) — one canonical record
of the Convivy Builder's projects, and of the commercial products they build.

A PROJECT is a unit of work: one thing being built, with a home directory, the
repos that serve it, and a dev crew. A PRODUCT is a commercial entity. They are
separate dimensions linked many-to-many, which is ARCH-426's central correction —
the old model treated them as one dimension with a `class` flavour, and that
collapse is why `commercial`, `vivy-landing` and `constance-landing` ended up in
the signals table's `product` column as if they were scopes.

The test that decides how many projects exist is the WORK test: one co, one crew,
one thing being built. Repo boundaries are NOT that test, and reaching for them is
the attractive mistake — they are visible, countable, and already carry the data,
while the real unit is invisible in the stores. `svc` is one project with two
repos (ARCH-426 amendment); repos are a service to a project.

This module loads `config/projects.json` and exposes it to the consumers that
today hand-maintain the same facts in six scattered lists (cost_join folds,
velocity BUCKETS, agent-activity-report's _PUBLIC_TASK_PRODUCTS, agentctl's
LOCAL_AGENT_REPOS, the KB validators' ALLOWED_PROJECTS). `tests/test_projects.py`
fails red when a consumer list drifts from it. Consumers read this loader as
they're next touched.

The registry keys on the project slug; the legacy `product` field names in the
control DB, telemetry, and dashboards stay as they are (renaming them would be
churn with no behavior change). This loader is where the vocabularies meet.
"""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import builder_root as BR

KB_VALUES = ("central", "local", "split")

# RESOLUTION ORDER, and the order is the whole privacy argument. This registry
# decides which projects are private, and `private_slugs()`'s docstring states
# the rule it has to satisfy: a privacy control must not fail open.
#
#   1. $PROJECTS_JSON            packaged tenant instance (§8, ARCH-358)
#   2. <this module's tree>/config/projects.json
#   3. <root>/agent-platform/config/projects.json, <root> from builder_root
#
# STEP 2 IS LIVE OR ABSENT, NEVER STALE, and that is what no deploy archiving
# `config/` buys. Reached through the ~/.local/lib SYMLINK farm, `.resolve()`
# follows the link back into the checkout, so the answer is the current file.
# Reached through a COPY — both ~/.local/deploy trees are copies — the path
# simply does not exist, because neither installer carries it. So step 2 can
# hand back a current registry or nothing; it can no longer hand back an old one.
#
# STEP 3 IS WHY A COPY DEPLOY STILL WORKS rather than merely failing safe. It
# names the operator's checkout outright, which is what `private_crew.py` has
# done for this same file since it was written, for this same reason.
#
# WHAT MADE THIS WORTH REWRITING. `run-register-api` filters `/runs` for Library
# and `/sessions` for Orient through `private_slugs()`. Under the old
# tree-relative-only default plus a deployed copy of `config/`, a project marked
# `private: true` after an install read as PUBLIC until someone reinstalled the
# daemon, with no error anywhere — a registry that parses fine is
# indistinguishable from a current one. The operator's call, 2026-09-05: it has to be read
# live.
#
# STEP 3 IS A FALLBACK, NOT THE DEFAULT, and that distinction is load-bearing. An
# earlier revision made the operator path unconditional and broke CI outright,
# where the checkout lives under the CI runner's own home directory and the
# machine root does not exist. Trying the module's own tree first is what keeps this portable — in-repo
# runs, CI, a contributor's clone and a packaged tenant all resolve their own
# file, and only a copy deploy reaches step 3.
#
# If NEITHER exists the read raises, and every caller here fails closed. That is
# the correct end of this ladder rather than an oversight.
#
# The override at step 1 is operator-level (env at process start) in the
# single-operator deployment, so it's trusted today. If the platform ever runs
# where a less-privileged process can influence the environment (multi-tenant
# containers, job-parameter-driven env), constrain the resolved path to an
# expected prefix before opening — else a crafted file could flip a data-local
# unit's task_text_public and leak its task text (security-reviewer, PR #136).
def _registry_path() -> Path:
    env = os.environ.get("PROJECTS_JSON")
    if env:
        return Path(env)
    own_tree = Path(__file__).resolve().parents[2] / "config" / "projects.json"
    if own_tree.exists():
        return own_tree
    return BR.root() / "agent-platform" / "config" / "projects.json"


PROJECTS_JSON = _registry_path()

# THE ROOT A REGISTRY HOME RESOLVES AGAINST (company/decisions/0008). A stored
# `<root>/<rest>` means <root>/<rest>, where <root> is builder_root.root(),
# unless a caller that was handed an explicit override sets ROOT_OVERRIDE: co from
# its --root-config, the session-event hook from its own, tests directly. NEVER an
# environment variable: the black-box wall matches against these homes, and the
# match must not depend on a root other than the one co resolved (the steward's
# call, 2026-09-11). The stored form is `<root>/<rest>` since slice 2.
ROOT_OVERRIDE: Path | None = None
_ROOT_PREFIX = "<root>/"
# The form every entry was stored in before 2026-09-12. READ, NEVER WRITTEN, so a
# copy of the registry older than that change still resolves to the same homes. No
# reader outside agent-platform parses it: the Library's private_repos() reads only
# project names and `repos`. tests/test_builder_root.py exempts this one line from
# the ratchet by name.
_LEGACY_ROOT_PREFIX = "~/Convivy/"


def crews_root() -> Path:
    """The root registry homes resolve against: ROOT_OVERRIDE, else builder_root.root()."""
    return Path(ROOT_OVERRIDE) if ROOT_OVERRIDE is not None else BR.root()


def _root_relative(raw: str) -> str | None:
    """What follows a root prefix, in either stored form, or None when `raw` has
    neither."""
    for prefix in (_ROOT_PREFIX, _LEGACY_ROOT_PREFIX):
        if raw.startswith(prefix):
            return raw[len(prefix):]
    return None


def registry_home(raw: str) -> Path:
    """A stored `<root>/<rest>` (or the legacy form) as crews_root()/<rest>,
    unresolved; an absolute path as given. Anything else raises, so a caller making
    a privacy decision fails closed on a value it cannot place rather than guessing."""
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"registry home {raw!r} is not a path")
    rest = _root_relative(raw)
    if rest is not None:
        return crews_root() / rest
    if os.path.isabs(raw):
        return Path(raw)
    raise ValueError(f"registry home {raw!r} is neither {_ROOT_PREFIX}..., "
                     f"{_LEGACY_ROOT_PREFIX}... nor absolute")

# Fields every project must carry, enforced by tests/test_projects.py.
#
# `data_local` and `overnight` are deliberately NOT here, and the omission is worth
# stating because it is not obviously safe. Both are read with a `.get()` default —
# `public_task_slugs()` tests `is True`, `overnight_products()` tests `is True` — so
# an absent field means "no" and both fail CLOSED: a project omitting them is left
# out of the public task-text allowlist and out of the shared overnight drain, which
# is the conservative answer in each case. Requiring them would turn a safe default
# into a schema error for a project that genuinely wants neither. The cost is that a
# contributor can add a project and get silently-conservative behaviour rather than a
# prompt, so check both when registering something that DOES want them
# (reviewer, PR #333).
REQUIRED_FIELDS = ("class", "display", "owner", "repos", "kb",
                   "home_dir", "dirs", "crew", "lifecycle", "products")
CLASSES = ("product", "project")

# Lifecycle. `active` is offered for work; `sunset` is winding down but still
# present; `archived` keeps its history and aliases so past attribution resolves,
# while nothing new is filed against it. A project that leaves the working set
# entirely is DELETED from the registry rather than archived — the three kit repos
# (ARCH-427) are absent, not archived, because they left <root>/ altogether.
LIFECYCLES = ("active", "sunset", "archived")

PRODUCTS_KEY = "_products"
SCOPES_KEY = "_scopes"

# A Library person id: `person_` and a 26-character ULID (Crockford base32,
# uppercase) — the same shape bin/agentctl's `_ISSUE_ID_RE` validates for issue
# ids, since both are minted by the Library's `store.new_ulid`.
PERSON_ID_RE = re.compile(r"^person_[0-9A-HJKMNP-TV-Z]{26}$")


def valid_person_id(value: Any) -> bool:
    """Whether `value` has the shape of a Library person id (`person_<ULID>`).

    A project's `created_by` field is checked against this both at write time
    (`crew_registration.build_row`) and at read time
    (`tests/test_projects.py::test_schema_wellformed`), so the two cannot
    silently accept different shapes."""
    return isinstance(value, str) and bool(PERSON_ID_RE.fullmatch(value))


@lru_cache(maxsize=None)
def _load_raw() -> dict[str, Any]:
    # PROJECTS_JSON is read once at process start (a static config for the run);
    # cached so the many helper calls below don't re-parse it each time.
    with open(PROJECTS_JSON) as f:
        return json.load(f)


def load() -> dict[str, dict[str, Any]]:
    """The registry as {slug: unit-dict}, with the leading `_meta` block dropped."""
    return {k: v for k, v in _load_raw().items() if not k.startswith("_")}


def units() -> dict[str, dict[str, Any]]:
    """Alias for load(); reads as `projects.units()` at call sites."""
    return load()


def get(slug: str) -> dict[str, Any] | None:
    """The unit dict for a slug, or None if unregistered."""
    return load().get(slug)


def slugs_by_class(cls: str) -> list[str]:
    """Registered slugs of the given class ('product' | 'project'), sorted."""
    return sorted(s for s, u in load().items() if u.get("class") == cls)


def fold_map() -> dict[str, str]:
    """Every raw repo/alias string → its unit `display`. This is the fold table
    cost_join._canonical_product and velocity's BUCKETS encode by hand; a consumer
    can adopt it to replace its own copy. A repo/alias appears under exactly one
    unit (the drift test enforces uniqueness)."""
    out: dict[str, str] = {}
    for u in load().values():
        for raw in list(u.get("repos", [])) + list(u.get("aliases", [])):
            out[raw] = u["display"]
    return out


def project_for_repo(repo: str | None) -> str | None:
    """The project slug a REPO belongs to, or None when no project claims it.

    The same answer the Library's `projects.resolve` gives a bare repo name when
    it scopes a review record, so a signal stamped with this product joins to
    that repo's review records: case-insensitive, and a project's own slug and
    aliases resolve alongside its repos. `signal_client.product_for_channel`
    answers a different question, a Slack channel's product, and put every
    repo it did not recognise under `platform`."""
    if not repo:
        return None
    wanted = repo.strip().lower()
    for slug, u in load().items():
        names = [slug, *u.get("aliases", []), *u.get("repos", [])]
        if wanted in (n.lower() for n in names if isinstance(n, str)):
            return slug
    return None


def crew_for_repo(repo: str | None, knowledge_root: Path | str | None = None) -> str | None:
    """The crew that owns a REPO, or None when neither the registry nor the
    knowledge repo names one.

    A repo the registry does not list falls through to `resolve_project`: one
    whose name resolves to project `<name>` belongs to crew `<name>`. That is
    what gives an adopter's crew, which is in no `config/projects.json`, its
    crew-scoped instruction blocks. A private crew never takes that path, under
    any spelling (`default_profile_refused`), so a case variant of a private
    crew's name still resolves to nothing.

    NOT `crew_for`, WHICH TAKES A PROJECT SLUG. That distinction is the whole
    reason this exists: the registry keys on project, and a project holds a LIST
    of repos, so `crew_for("agent-platform")` returns None — the project is
    `bosun`. Two reviewers independently proposed `crew_for(repo)` as the fix for
    exactly that bug and it would have returned None for the three repos they
    cited (agent-platform, convivy-app-kit, selector-backend), turning a
    wrong-key lookup into a no-key lookup.

    Aliases count, the same way `fold_map` treats them, so a dispatch naming an
    alias resolves like one naming the repo.
    """
    if not repo:
        return None
    for u in load().values():
        if repo in list(u.get("repos", [])) + list(u.get("aliases", [])):
            return u.get("crew")
    if default_profile_refused(repo):
        return None
    # Only a name that IS its project. An explicit row that maps a repo to a
    # different project (`dev-setup` -> `platform`) says which knowledge the repo
    # reads, not that it is that project's crew.
    project = resolve_project(repo, knowledge_root)
    return project if project == repo else None


def kb_scope_exceptions() -> dict[str, dict[str, str]]:
    """Every repo across the registry with a recorded, deliberate knowledge-scope
    exception: `{repo: {"kb_project": ..., "reason": ...}}`, gathered from each
    project unit's own `kb_scope_exceptions` entry in config/projects.json.

    A row here is the registry's record that the repo's knowledge scope is
    narrower than its project's own `kb_project` on purpose — a security review
    found the wider scope unsafe for that repo, or the project's own kb_project
    resolves to no real profile. `tests/test_projects.py` and
    `tests/test_project_resolver.py` hold these equal to the matching rows in
    `EXPLICIT_PROJECT_BY_NAME`, in both directions: a repo this function narrows
    for, EXPLICIT_PROJECT_BY_NAME must narrow the same way, and a narrowing row
    in EXPLICIT_PROJECT_BY_NAME must have a matching entry here. That is the link
    a later change deriving scope from the registry has to consult before it
    could widen a repo back — without it, the derivation would do exactly that
    silently (issue_01M3V1J89CG19ZGD3JY4WFY7V0)."""
    out: dict[str, dict[str, str]] = {}
    for unit in load().values():
        out.update(unit.get("kb_scope_exceptions", {}))
    return out


# The KB project a DIRECTORY or REPO name resolves to when the name is not its own
# project. One table, read by `bin/knowledge-ctx` (`detect_project`) and
# `bin/agentctl` (`default_profile`); it replaced two copies that had to be pinned
# equal by a test. Keys are directory names, so EA is capital. Every value must
# name a real directory under knowledge/projects/: a wrong mapping is worse than a
# missing one, since it hands the agent another product's knowledge context.
#
# Eight rows here narrow a repo below its project's own registry `kb_project`,
# each a deliberate exception rather than a stale copy: `kb_scope_exceptions()`
# above reads the same decision back out of config/projects.json, and the tests
# named there hold both readings equal.
EXPLICIT_PROJECT_BY_NAME = {
    "constance": "constance",
    "vivy": "vivy",
    "sous-chef": "sous-chef",               # B2C product (ARCH-060)
    "vivy-ownerrez-sandbox-tools": "vivy",  # vivy product family — shares vivy profile
    "vivy-commercial": "vivy",              # commercial workspace → vivy KB (ARCH-049)
    "constance-commercial": "constance",    # commercial workspace → constance KB (ARCH-049)
    # constance-updates → constance-landing (config/projects.json, #508). A
    # security review of this PR ruled out mapping to `constance`: its daytime
    # profile hands a dispatched agent the full reference set — company,
    # commercial, founder history — and constance-updates is an outward-facing
    # surface, its session recaps read by a guest co-founder who
    # works outside the engineering surfaces (no tailnet, no team-site
    # access). Same reason as constance-landing, and the same narrow
    # project: no reference docs, the recaps are composed by a co session from
    # its own conversation, and a worker dispatched here only places files.
    "constance-updates": "constance-landing",
    "convivy-commercial": "company",        # company-level commercial workspace → company KB (ARCH-124, re-pointed ARCH-438)
    # dev-setup is the frozen build workshop (build-complete 2026-05-20); its
    # README is still the authoritative build record, and that record now lives
    # under `platform`. It maps there rather than to the retired `utility`
    # (ARCH-438). Not a spawn target — a workspace context only.
    "dev-setup": "platform",
    "agent-platform": "agent-platform",     # the substrate platform repo itself (ARCH-099)
    # Added 2026-07-18 so `agentctl spawn` can dispatch reviewers against these
    # repos. Without an entry here the spawn aborts ("no project mapping for
    # worktree <x>"), which forced their reviews onto the Task tool — and a
    # Task-dispatched reviewer has no run-id, so its review stamp is
    # unverifiable and `review-status` rejects it outright (ARCH-193). The
    # missing rows were the reason those reviews were second-class, not any
    # missing capability.
    #
    # Every value here must name a real directory under knowledge/projects/ —
    # a wrong mapping is worse than a missing one, since it hands the agent
    # another product's knowledge context.
    "knowledge": "knowledge",               # the KB repo itself
    "selector": "selector",
    "hollis": "hollis",
    "convivy-app-kit": "convivy-app-kit",
    "convivy-services": "convivy-services",
    # convivy-orient → shared (the operator, 2026-07-18), answering the question the note
    # below deferred. orient has no KB project of its own, but `shared` is where
    # its knowledge already lives: team.convivy.com is documented across 7 shared
    # docs, against 2 in convivy and 1 in engineering. It also matches how the
    # `knowledge` repo itself maps, so an infrastructure repo taking `shared` has
    # precedent. orient is substrate's own read-site and gets frequent PRs, so it
    # was the repo where the unverifiable-stamp fallback cost most.
    "convivy-orient": "convivy-orient",
    "www": "www",   # its own narrow profile (#552)
    # convivy-deck → deck (ARCH-375, deck plan WS0.3). This one needed no
    # judgment call: deck has its own knowledge/projects/deck/ directory, minted
    # by the KB bootstrap, so the mapping names a real project rather than
    # borrowing one.
    "convivy-deck": "deck",
    "placard": "placard",   # the operator, 2026-09-05
    # selector-backend → selector (2026-07-27). Not a judgment call of the kind
    # orient needed: selector-backend is Selector's own backend service (the
    # MusicBrainz mirror API), so Selector's knowledge context is the correct one,
    # exactly as `vivy-ownerrez-sandbox-tools` shares vivy's. No
    # knowledge/projects/selector-backend/ exists and none should.
    "selector-backend": "selector",
    # constance-landing → its own project (2026-08-24, re-pointed 2026-08-25).
    # The constance.team apex landing page: git-connected Cloudflare Pages, plain
    # static HTML, no build step, so a merge publishes straight to the public site.
    #
    # This row first read `constance`, following the satellite-repo precedent
    # above. A security review found that hands an agent whose entire output is
    # public marketing copy the whole of Constance's daytime profile, company- and
    # commercial-scope reference documents included. The rule at the top of this
    # map — a wrong mapping is worse than a missing one, since it hands the agent
    # another product's knowledge context — applies to breadth as well as to
    # product identity. The narrow profile has to be the DEFAULT, because
    # --profile defaults to <product>/<daytime|overnight>. The operator approved the split
    # on 2026-08-25; the project is convivy/knowledge#280 and must merge first.
    #
    "constance-landing": "constance-landing",
    # vivy-landing → its own narrow project (orchestrator ruling, 2026-10-02). It
    # sits under `vivy` in the registry, whose daytime profile carries the company
    # and commercial reference set (founder history, the partner vetting narrative).
    # The repo's output is public marketing copy, so the default profile is the
    # narrow one, same as constance-landing and www. projects/vivy-landing/ is in
    # convivy/knowledge (knowledge#731, merged as 39b86b68) with its daytime profile.
    "vivy-landing": "vivy-landing",
    # substrate + write-better → shared (2026-07-30). ARCH-391 names both as
    # agentctl targets — "knowledge, write-better, substrate, hollis,
    # convivy-orient, agent-platform" — and neither was ever mapped, so every
    # dispatch against them aborted here. A recorded decision the machinery never
    # implemented, found when a reviewer dispatch on substrate PR #1 died.
    #
    # `shared` on evidence rather than by analogy. Substrate's own ADRs already
    # live in knowledge/projects/shared/decisions/ (decision 0001 on the human in the loop,
    # 0002 pr-url-review-status-labeling), and 30 of the docs mentioning
    # write-better are under shared, against none elsewhere. It also matches how
    # `knowledge` and `convivy-orient` map: an infrastructure or prose repo with
    # no project directory of its own takes shared.
    # substrate + write-better → `platform` (ARCH-438), the scope that inherits
    # `shared`'s catch-all corpus role because it inherited `shared`'s documents.
    # They shared projects/platform/profiles/daytime.yaml until ARCH-440, below.
    "substrate": "platform",
    # EA → `platform`, and it is RESTRICTED BY `_PROFILE_ALLOWLIST` BELOW to
    # `ea-daytime` alone (2026-09-08). Keyed capital-EA because `detect_product`
    # reads the directory name and the directory is capital-EA on disk, the same
    # split the CFO registry note records between slug and path.
    #
    # DO NOT REMOVE THE ALLOWLIST ENTRY AND LEAVE THIS ROW. This row is what makes
    # `platform/daytime` reachable from EA, and that profile carries the company
    # reference slice into a crew holding personal correspondence. The row and the
    # restriction landed together and are only safe together.
    "EA": "platform",
    # shipwright → its OWN project (2026-09-07), once
    # projects/shipwright/profiles/daytime.yaml merged in convivy/knowledge#358.
    # THIS TABLE IS WHAT THE `use` PATH RESOLVES A BARE PROFILE NAME
    # THROUGH, in main's fallthrough past the `list` branch, as two statements:
    #
    #     project = detect_project(cwd)
    #     profile = load_profile(project, args.profile)
    #
    # detect_project is what reads this table. That path is inline and has no
    # function of its own, so do not grep for a `cmd_use` here; the only one in
    # the tree is bin/claude-sub's, which is unrelated.
    #
    # So while this row read `platform`, `knowledge-ctx use daytime` from
    # <root>/shipwright individuated platform/daytime, the
    # profile whose claude_md_extras describe substrate, into the crew that
    # ships publicly. Dispatch was never the only consumer.
    "shipwright": "shipwright",
    # signals → its OWN project (2026-09-16). It is what makes `knowledge-ctx use daytime` from <root>/signals resolve.
    "signals": "signals",
    # write-better → its OWN project (ARCH-440).
    "write-better": "write-better",
    # library → `platform` (2026-08-07). The Library's design rationale is the
    # platform corpus — ADR-0019 through ADR-0022 and the protocol contract sit
    # under `projects/platform/`, which also carries the `daytime` profile a
    # reviewer spawn asks for. `keel` exists as a directory but holds only the
    # bead, so it would resolve and then hand over one diagram; an entry that
    # resolves to the wrong corpus is worse than the missing one it replaces.
    #
    # library → its OWN project (2026-08-17), on the condition the note beside
    # agentctl's copy set: give the Library its own project once it has documents
    # of its own. The crew split made that true — the Library is a crew with its
    # own KB slice and its own daytime profile, so a dispatch here now resolves to
    # the Library's corpus rather than borrowing the platform catch-all.
    "library": "library",
    # archy → its OWN project (2026-10-02, the operator via the steward). New Convivy
    # product, registered explicitly rather than left to the directory
    # fallback — the central knowledge/projects/archy/ directory is a separate
    # Library change, so this row is what makes resolution work regardless of
    # the order the two land in.
    "archy": "archy",
}


_NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def resolve_project(name: str | None, knowledge_root: Path | str | None = None) -> str | None:
    """The KB project a directory or repo NAME maps to, or None.

    Convivy's explicit rows win. Otherwise `<name>` maps to project `<name>` when
    `<knowledge_root>/projects/<name>/` exists, which is what lets an adopter's
    crews resolve without a row in this file (company decision 0011). A name that
    is not a plain directory name resolves to nothing rather than reaching a path
    outside `projects/`."""
    if not name:
        return None
    explicit = EXPLICIT_PROJECT_BY_NAME.get(name)
    if explicit:
        return explicit
    if not _NAME_OK.match(name):
        return None
    root = Path(knowledge_root) if knowledge_root else BR.root() / "knowledge"
    # Exact-case match against the names on disk: a case-insensitive filesystem
    # would otherwise accept `Vivy` for `projects/vivy/` and hand back a slug
    # the case-sensitive kb filters never match.
    try:
        return name if name in {d.name for d in (root / "projects").iterdir()
                                if d.is_dir()} else None
    except OSError:
        return None


DEFAULT_PROFILE_PROJECT = "platform"
DEFAULT_PROFILE_NAME = "default"


def _private_crew_names() -> set[str]:
    """Every casefolded name a private crew answers to: slug, display, crew, its
    aliases and repo names, and the basenames of its home and `dirs`. Raises when
    the registry cannot be read."""
    names: set[str] = set()
    for slug, unit in units().items():
        if unit.get("private") is not True:
            continue
        raw = [slug, unit.get("display"), unit.get("crew"),
               *unit.get("aliases", []), *unit.get("repos", [])]
        raw += [Path(p).name for p in [unit.get("home_dir"), *unit.get("dirs", [])] if p]
        names.update(n.casefold() for n in raw if isinstance(n, str) and n)
    return names


def default_profile_refused(name: str, restricted_names=()) -> bool:
    """True when directory or repo `name` must never receive the generic default
    profile: it is a private crew (`platform/decisions/0043`) or a key of a
    restricted-profile table (`restricted_names`, knowledge-ctx's
    `_PROFILE_ALLOWLIST`).

    Compared by `casefold()` because the disk is case-insensitive and a resolved
    cwd keeps whatever spelling the operator typed: `ea` reaches the `EA` black
    box. An unreadable registry refuses as well; a check that cannot tell whether
    a directory is private must not hand it a profile."""
    folded = name.casefold()
    if folded in {r.casefold() for r in restricted_names}:
        return True
    try:
        return folded in _private_crew_names()
    except Exception:
        return True


def overnight_products() -> list[str]:
    """Slugs registered for the shared overnight drain (overnight == true),
    sorted. Excludes own-drain units (Hollis) and non-overnight units. The
    cron-fired drain itself was retired 2026-09-14 (autonomous work is
    event-triggered now); this reads a registry field that still records the
    grouping."""
    return sorted(s for s, u in load().items() if u.get("overnight") is True)


MODEL_TIERS = ("opus", "sonnet")


def model_tier(slug: str) -> str:
    """The interactive co main-loop model tier for *slug* — `"opus"` unless the
    entry says otherwise.

    ABSENT MEANS OPUS, AND THAT DIRECTION IS THE WHOLE POINT. The operator inverted this
    default on 2026-09-16 because the old arrangement reached Opus through three
    lists and let everything else fall to a silent Sonnet, so `placard`,
    `shipwright` and Signals each launched on Sonnet until he noticed the model.
    Forgetting to register a crew now costs Opus rather than judgement. A crew
    registered with no `model_tier` is therefore not an oversight to guard
    against; it is the default expressing itself, which is why this returns a
    value rather than None and why nothing here refuses an absent field.

    An unrecognised value IS refused — by
    `tests/test_co_crew_registration.py`'s
    `test_every_declared_model_tier_is_one_this_platform_serves` — rather than
    silently read as Opus, because a typo'd `"sonnett"` that quietly bills Opus
    is the wrong-answer-shaped-like-an-empty-one this tier exists to avoid.
    """
    unit = get(slug) or {}
    raw = unit.get("model_tier")
    return raw if raw in MODEL_TIERS else "opus"


def sonnet_crew_dirs() -> list[str]:
    """Home-directory BASENAMES of active crews pinned to Sonnet, sorted.

    Basenames rather than slugs because that is what `bin/co` matches on: the
    basename of the git toplevel, or of `$PWD` where there is none. The two
    coincide for every crew on the list today, and deriving from the home
    directory is what keeps them coinciding rather than assuming it.
    """
    out = []
    for slug, unit in load().items():
        if unit.get("lifecycle") != "active" or model_tier(slug) != "sonnet":
            continue
        home = home_path(slug)
        if home is not None:
            out.append(home.name)
    return sorted(out)


def crew_cockpit_dirs() -> list[str]:
    """Home-directory BASENAMES of active crews whose `crew_cockpit` field is
    `True`, sorted.

    This is `_co_crew_dirs`' own set, read the registry's way rather than
    bin/co's hardcoded one — the same move `sonnet_crew_dirs()` already made
    for `model_tier`. `bin/co`'s own comment calls that array "THE SITE NEW
    CREWS KEEP MISSING": `placard` and `shipwright` worked for days on
    `co` alone and only broke on `co -r` ("cwd is not a recognized product
    repo"), because re-individuating a crew cockpit on refresh was gated on a
    literal nobody had added them to. A crew registered with
    `crew_cockpit: true` (`crew_registration.register`'s default for a
    `kb: central` crew with no dedicated bin/co branch of its own) reaches
    `co -r` with no edit to bin/co.

    Basenames, for the same reason `sonnet_crew_dirs()` gives: `bin/co`
    matches `_co_crew_dirs` against the resolved git-toplevel basename.
    `bin/co` APPENDS this list to its own six-member literal rather than
    replacing it — those six are unchanged by this function and this
    function names none of them — so a registry read failure here costs a
    registry-registered crew its `co -r` re-individuation, never one of the
    six bin/co already hardcodes.
    """
    out = []
    for slug, unit in load().items():
        if unit.get("lifecycle") != "active" or unit.get("crew_cockpit") is not True:
            continue
        home = home_path(slug)
        if home is not None:
            out.append(home.name)
    return sorted(out)


def public_task_slugs() -> set[str]:
    """Slugs cleared for public task text (task_text_public == true). A subset of
    agent-activity-report._PUBLIC_TASK_PRODUCTS, which also carries repo-level
    entries (…-commercial, substrate, write-better) that aren't top-level units."""
    return {s for s, u in load().items() if u.get("task_text_public") is True}


def private_slugs() -> set[str]:
    """The BLACK BOX crews (`platform/decisions/0043`): registered so the platform
    knows they exist, with none of their content reaching the Library and none of
    their agents visible to Orient.

    Read with `.get(...) is True`, so an absent field means "not private" and every
    existing entry keeps its behaviour. That direction is deliberate but it is the
    one worth arguing about, because it fails OPEN for a typo: `privat: true` reads
    as public and the failure is silent. The schema test in `tests/test_projects.py`
    is what closes that, by refusing an entry that is private in name and centrally
    indexed in fact — and by refusing an unknown key that looks like this one.

    THIS IS NOT THE PRIVACY CONTROL. The wall that matters is at transport, in
    `bin/co`, which launches a black box without the `ops` and `kb` MCP servers at
    all — that holds under `bypassPermissions`, which a `permissions.deny` rule
    does not. This accessor is how the rest of the platform learns to leave the
    crew alone, and ADR-0043 is explicit that a privacy control must not be the
    thing that fails open.

    Distinct from `data_local`, which describes a PUBLIC product whose users' data
    lives on their own devices (Selector, Sous Chef, Hollis). That is other
    people's data Convivy never holds; this is Convivy's own work that Convivy
    chooses not to put in its shared stores.
    """
    return {slug for slug, u in units().items() if u.get("private") is True}


def privacy_invariant_errors(unit: dict[str, Any]) -> list[str]:
    """Why `unit` breaks the BLACK BOX contract ADR-0043 requires of a private
    crew, or `[]` when it holds. Shared by
    `test_a_private_crew_cannot_also_be_centrally_indexed` (checked at CI time,
    against the committed registry) and `crew_registration.build_row`
    (checked before a byte reaches the live registry), so the two cannot
    drift apart on what "private" requires.

    `private: true` says none of this crew's content reaches the Library and
    none of its agents reach Orient. Three other fields have to agree for
    that to be true: `kb: local` keeps it out of `kb_slugs()` and so out of
    the shared index, `task_text_public: false` keeps its task strings off
    the public surfaces, and `overnight: false` keeps it out of the shared
    autonomous drain. An entry marked private with any of the three
    disagreeing would index its documents into the shared KB, or publish its
    task text, or join the shared drain, while every reader of the registry
    believed it was a black box — and nothing downstream would complain, so
    this has to be caught here rather than left to show up as a leak.
    """
    if unit.get("private") is not True:
        return []
    errors = []
    if unit.get("kb") != "local":
        errors.append(f"kb={unit.get('kb')!r}, not 'local'")
    if unit.get("task_text_public") is True:
        errors.append("task_text_public is true")
    if unit.get("overnight") is True:
        errors.append("overnight is true")
    return errors


def dispatch_is_private(repo: str | None, agent_source: str | None = None, *,
                        private_slugs_snapshot: "set[str] | None" = None,
                        home_paths_snapshot: "dict[str, Path] | None" = None) -> bool:
    """Whether a dispatch reaches a black-box crew (`platform/decisions/0043`),
    checked by EITHER signal that can say so.

    `repo` ALONE IS NOT ENOUGH: `bin/agentctl`'s `resolve_agent()` honours
    `--agents-dir` before it ever looks at `repo`, so
    `--on constance --agents-dir <root>/CFO/.claude/agents` loads a CFO
    agent's body while `repo` stays `"constance"`, and a repo-only check misses
    it (security-reviewer, run-1031c63b — the bug that forced
    `--strict-mcp-config` to check the resolved agent's `source` too, and the
    same bug reopened the built-in-tool exfil path when it wasn't threaded
    through there — security-reviewer, run-b5dee6b0). `agent_source` is the file the caller actually resolved and
    read (`resolve_agent()` always hands it back); gate on whether it resolves
    under a private crew's `home_path()` too. Both sides go through
    `.resolve()` so a symlink or an unresolved `..` segment can't slip past
    the comparison. Optional and `None`-safe: a caller with only `repo` (or a
    recorded dispatch predating this field) gets the repo-only half.

    THE SINGLE COMPARISON, shared by every caller that needs "is this dispatch
    a black box's" — `bin/agentctl`'s `_is_private_dispatch` (the
    `_disallowed_tools`/`--strict-mcp-config` gate) and
    `session_state._dispatched_by` (the derived-working-on-line filter,
    agent-platform#465, round 2: `repo` alone let a public session's dispatch
    into a private crew via `--agents-dir` leak that crew's task text) both
    delegate here instead of re-deriving the comparison, so the two cannot
    drift the way the repo-only and agent-source-aware checks once did within
    `bin/agentctl` itself.

    Raises whatever the registry read raises (`FileNotFoundError`,
    `json.JSONDecodeError`, via `private_slugs()`/`home_path()`) rather than
    swallowing it — a caller that must fail closed on an unreadable registry
    catches at its own boundary, matching `private_home_for`'s posture in
    `private_crew.py` one module over.

    `private_slugs_snapshot`/`home_paths_snapshot` let a caller that must judge
    every row of ONE response against the SAME registry generation pass one in,
    instead of this function reading `private_slugs()` (twice, with no snapshot
    of its own) and `home_path()` fresh per call. `bin/run-register-api`'s
    `ThreadingUnixHTTPServer` serves `/runs` and `/sessions` concurrently, so
    two calls made moments apart — even within this one function — can
    straddle another thread's cache clear-and-repopulate and land on two
    different registry generations (reviewer + security-reviewer, round 4).
    Both default to `None`, which reads live via `private_slugs()`/
    `home_path()` exactly as before — every existing caller (`bin/agentctl`,
    `session_state._crew_is_shareable`) is unaffected.
    """
    private = private_slugs_snapshot if private_slugs_snapshot is not None else private_slugs()
    if isinstance(repo, str) and repo.lower() in private:
        return True
    if not agent_source:
        return False
    resolved_source = Path(agent_source).resolve()
    for slug in private:
        home = (home_paths_snapshot.get(slug) if home_paths_snapshot is not None
                else home_path(slug))
        if home is not None and resolved_source.is_relative_to(home.resolve()):
            return True
    return False


def kb_slugs() -> set[str]:
    """kb_project slugs for units with a central or split KB slice — the units that
    must appear in the KB validators' ALLOWED_PROJECTS."""
    return {
        u.get("kb_project", s)
        for s, u in load().items()
        if u.get("kb") in ("central", "split")
    }


# --- ARCH-426: projects, products, directories, crews ----------------------------


def products() -> dict[str, dict[str, Any]]:
    """The commercial products, {slug: product-dict}. Separate from projects by
    design; a project links to these through its `products` list."""
    return dict(_load_raw().get(PRODUCTS_KEY, {}))


def get_product(slug: str) -> dict[str, Any] | None:
    return products().get(slug)


def products_for(slug: str) -> list[str]:
    """Product slugs this project builds, sorted. Empty is normal — most of the
    platform builds no commercial product."""
    u = get(slug)
    return sorted(u.get("products", [])) if u else []


def projects_for_product(product_slug: str) -> list[str]:
    """Project slugs that build this product, sorted. The other direction of the
    many-to-many, which is the one a commercial question asks."""
    return sorted(s for s, u in load().items()
                  if product_slug in u.get("products", []))


def scopes() -> dict[str, dict[str, Any]]:
    """The NON-PROJECT scopes, {slug: scope-dict} — `company`, `platform`,
    `commercial`, `engineering-practice` (ARCH-426 amendments).

    Nothing is BUILT at these, so they have no home directory and no crew and are
    not projects. They are a third dimension in the same file, the way `_products`
    holds the commercial one. The `_doc` key is dropped, like `_meta` elsewhere."""
    raw = _load_raw().get(SCOPES_KEY, {})
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def all_scopes() -> set[str]:
    """Every legal scope: Project ∪ the non-Project scopes.

    This is the set anything validating a `product`/`project` field should check
    against, and getting it from here is the point. ARCH-426 makes the vocabulary
    UNIFORM ACROSS ALL THREE VAULTS — a Workshop issue may be scoped `commercial`,
    an Archives signal `company`, a Reference Room document `platform` — so a
    consumer that validates against `load()` alone rejects three legal values.
    That was the live gap: the issue API resolved `product` against registered
    projects only, so filing anything at a non-Project scope returned 400."""
    return set(load()) | set(scopes())


def _expand_own_home(slug: str, field: str, raw: str) -> Path:
    """Expand a registry path, REFUSING any form that resolves outside this user's
    home. Raises ValueError on anything that is neither root-relative (`<root>/` or
    the legacy prefix) nor `~/`.

    This is a runtime invariant, not a style rule, and the distinction is the whole
    point. `Path.expanduser()` resolves `~someone/path` through `pwd.getpwnam`, not
    just $HOME, so a registry value of `~root/target` expands to `/var/root/target` —
    a different user's tree. The schema test asserts the `~/` prefix, but a test runs
    at CI time against the CHECKED-IN file, and `PROJECTS_JSON` exists precisely to
    point the loader at a different one (a packaged tenant instance). A crafted file
    supplied that way never meets the test, so the constraint has to hold here too.

    Nothing calls this outside tests yet, which is why it is cheap to fix now: the
    next step migrates agentctl's `product_dir` derivation onto `dirs_for()`, and
    that is a filesystem consumer — the point at which a redirected path stops being
    a string and starts being an `os.listdir` somewhere it should not be
    (security-reviewer, PR #333).

    A bare `~` is refused along with `~user/…`: it is almost certainly a mistake, and
    a project whose home is the whole home directory is not a case worth supporting
    silently.

    THE PREFIX CHECK IS NOT THE WHOLE GUARD, and assuming it was is the mistake this
    docstring exists to stop someone repeating. `~/../../etc/passwd` satisfies
    `startswith("~/")` and still resolves outside the home directory entirely; so does
    `~/../root`. So the value is resolved and then tested for containment
    under the real home directory. `resolve()` also collapses symlinks, which closes
    the same escape dressed as a link.

    Containment is deliberately absolute: a registry path may not leave the user's
    home AT ALL. That rules out an external volume — a `dirs` entry naming a mount
    outside $HOME entirely is refused, and Selector's MusicBrainz mirror on an external
    NVMe is the live precedent for wanting one. It is refused today because the field
    is documented `~`-relative and no caller needs otherwise; if a project genuinely
    needs an out-of-home directory, widen this deliberately with its own field rather
    than by loosening the check.

    A BLACK BOX's (`private: true`) `home_dir`/`dirs` entry is refused outright if it,
    or any path segment between it and $HOME, is a symlink. `home_path()`/`dirs_for()`
    resolve through `.resolve()`, which follows a symlink to its REAL target — but a
    `~/.claude/projects` transcript dirname may preserve whichever cwd form Claude
    Code actually saw, which is not guaranteed to be the resolved form. A private
    crew's sessions run through a symlinked home directory could then sanitize to a
    dirname that never contains the resolved-target fragment `bin/transcript-capture`
    computes, missing its black-box exclusion silently (security-reviewer, PR #415,
    run-61a07dc7). Not live today — no registered `private: true` project's
    `home_dir`/`dirs` is a symlink — but nothing else in the schema stops the next
    one from registering one, and ADR-0043 says more crews will follow. Scoped to
    `private: true` only: an ordinary public project's symlinked directory carries no
    such exclusion contract to violate.

    Checking only the leaf is not enough: `Path.is_symlink()` lstats the FINAL path
    component only, so it does not see a symlink one or more segments up — e.g.
    `home_dir: "<root>/linked-dir/actual-project"` where `linked-dir`, not
    `actual-project`, is the symlink. `.resolve()` still silently follows it to a
    different real directory, so that shape sailed through the leaf-only check while
    exhibiting the exact exclusion bypass this guard exists to close (reviewer, PR
    #415, run-6a061c87). So every segment between the expanded path and $HOME is
    walked; the walk stops at $HOME rather than climbing into it or above, so a
    machine-wide symlink outside the user's home tree (unrelated to this project's
    registration) is never inspected and never fails the check."""
    rest = _root_relative(raw) if isinstance(raw, str) else None
    if rest is None and (not isinstance(raw, str) or not raw.startswith("~/")):
        raise ValueError(
            f"{slug}: {field} {raw!r} must start with '{_ROOT_PREFIX}' or '~/' — a "
            f"registry path that resolves outside this user's home is refused "
            f"(expanduser resolves ~user/… through pwd, so it would escape the "
            f"intended tree)")
    # A root-relative value (`<root>/<rest>`, or the legacy form) lives under the
    # machine's root, so containment and the symlink walk below are measured
    # against the root; any other `~/` value keeps its home-relative meaning.
    if rest is not None:
        home_literal = crews_root()
        expanded = home_literal / rest
    else:
        home_literal = Path.home()
        expanded = Path(raw).expanduser()
    home = home_literal.resolve()
    unit = get(slug)
    if unit is not None and unit.get("private") is True:
        symlinked_at = expanded if expanded.is_symlink() else None
        if symlinked_at is None:
            for parent in expanded.parents:
                if parent == home_literal:
                    break
                if parent.is_symlink():
                    symlinked_at = parent
                    break
        if symlinked_at is not None:
            where = ("" if symlinked_at == expanded
                     else f" at intermediate segment {symlinked_at}")
            raise ValueError(
                f"{slug}: {field} {raw!r} ({expanded}) is a symlink{where} — a "
                f"private project's home_dir/dirs entry must be a real directory, "
                f"with no symlinked path segment, since the transcript-capture "
                f"black-box exclusion may not see the same resolved form this "
                f"registry would hand back")
    resolved = expanded.resolve()
    if not resolved.is_relative_to(home):
        raise ValueError(
            f"{slug}: {field} {raw!r} resolves to {resolved}, outside {home} — a "
            f"'~/' prefix is not containment, since '..' segments and symlinks "
            f"both escape it")
    # Return the RESOLVED path, so what the caller gets is the value that was
    # checked. Returning the merely-expanded form would hand back a path only
    # equivalent to the validated one, and the difference is the whole bug: an
    # unresolved `~/../../etc` satisfies is_relative_to(home) because that compares
    # path PARTS without resolving, while the OS call the caller eventually makes
    # does resolve, and lands in /private/etc (security-reviewer, PR #333).
    return resolved


def home_path(slug: str) -> Path | None:
    """The project's home directory as an absolute Path, or None if unregistered.

    `home_dir` is STORED as `<root>/<name>` (the legacy form, _LEGACY_ROOT_PREFIX,
    still reads) so the registry stays portable across machines and packaged tenant
    instances; every caller wants it expanded, so the expansion lives here rather
    than at each call site. Returns a Path even when the
    directory does not exist — existence is the caller's question, not the
    registry's.

    None means UNREGISTERED. A registered project whose `home_dir` is malformed
    raises instead, because returning None for both would let a crafted or corrupt
    registry read as "no such project" and be skipped silently."""
    u = get(slug)
    if u is None or not u.get("home_dir"):
        return None
    return _expand_own_home(slug, "home_dir", u["home_dir"])


def dirs_for(slug: str) -> list[Path]:
    """Every directory belonging to this project — home first, then the associated
    directories that are NOT subdirectories of it (ARCH-426). Absolute, expanded, and
    de-duplicated while preserving order, so a `dirs` entry repeating home_dir does
    not hand a caller the same directory twice.

    The non-subdirectory case is the whole reason this is a list rather than a
    prefix: `agentctl` assumes a project lives at <root>/<repo> (bin/agentctl,
    the product_dir derivation), and that assumption is what this field exists to
    replace.

    Every path goes through the same refusal as home_path — see _expand_own_home."""
    u = get(slug)
    if u is None:
        return []
    out: list[Path] = []
    if u.get("home_dir"):
        out.append(_expand_own_home(slug, "home_dir", u["home_dir"]))
    for d in u.get("dirs", []):
        out.append(_expand_own_home(slug, "dirs entry", d))
    seen: set[Path] = set()
    deduped = []
    for p in out:
        if p not in seen:
            seen.add(p)
            deduped.append(p)
    return deduped


def inbox_links(slug: str) -> list[tuple[Path, Path]]:
    """The `(link, target)` pairs making this project's inboxes ONE inbox: every
    associated directory's `inbox/` points at the home directory's (ARCH-426).
    Empty for a project with no associated directories, which is most of them.

    The problem it solves is observed rather than theoretical. A project with two
    directories has two `inbox/` directories and one reader: the co watches the home
    directory's, so notes an agent drops while standing in the other directory are
    never seen. `svc` is the live case — an agent working in `convivy-services`
    writes to that repo's inbox and the svc co, watching `convivy-app-kit/inbox`,
    reads nothing.

    A symlink is what makes "the project has ONE inbox" true at the FILESYSTEM
    level, so it holds for any caller that opens `<dir>/inbox` without knowing the
    registry exists — including an agent that writes to `./inbox` in whatever
    directory it happens to be standing in. A registry-driven sweep over every
    directory's inbox would fix the same symptom for callers that consult the
    registry, and leave the trap in place for everything else.

    The target is returned RELATIVE to the link's parent, so the farm survives the
    base directory moving (ARCH-429 makes the base a configured setting). An
    absolute target would pin every link to today's `<root>`.

    This computes; it does not touch the filesystem. `bin/link-project-inboxes`
    applies the pairs, and it is the thing that refuses to clobber real content."""
    dirs = dirs_for(slug)
    if len(dirs) < 2:
        return []
    home, *associated = dirs
    target = home / "inbox"
    return [(d / "inbox", Path(os.path.relpath(target, d))) for d in associated]


def crew_for(slug: str) -> str | None:
    """The dev crew / co session working this project, or None when it is not a
    single one. None is a real answer rather than missing data: it says the model
    found no single crew that fits. Every registered project carries one today —
    the last null, on the old nyh-studio unit, was reporting that one project held
    several crews, and splitting it into three resolved that."""
    u = get(slug)
    return u.get("crew") if u else None


def creator_of(slug: str) -> str | None:
    """The Library person id (`person_<ULID>`) that created project `slug`, or
    None when unrecorded.

    Unrecorded covers two cases the Library's per-person project-visibility
    default (issue_01M2QWFR8PCBPANF8Q5P460GHY) treats alike: `slug` is not
    registered at all, and `slug` is registered but carries no `created_by` —
    every project predating the field (backfilled 2026-09-18 to the operator's
    own person id, the ruling in that issue's comment 3014), or one `agentctl crew
    register` wrote before the operator config carried a `person_id`. A real
    id, once recorded, is never confused with either: this returns the string
    the registry holds, not a sentinel a caller could mistake for a name."""
    u = get(slug)
    return u.get("created_by") if u else None


def slugs_created_by(person_id: str) -> list[str]:
    """Project slugs whose `created_by` names `person_id`, sorted — the other
    direction of `creator_of`, for a visibility default computed per person
    rather than per project."""
    return sorted(s for s, u in load().items() if u.get("created_by") == person_id)


def projects_by_crew(crew: str | None) -> list[str]:
    """Project slugs worked by this crew, sorted. ARCH-426 makes the relation
    many-to-one, so several projects may legitimately share one crew.

    `None` returns [] rather than every crewless project. Without this guard the
    comparison below matches `None == None`, so the natural chain
    `projects_by_crew(crew_for(slug))` would group the crewless projects into a
    single phantom crew — the exact grouping the null crew exists to AVOID. No
    project carries a null crew today, so the guard protects the next one to be
    added rather than a live case; keep it, because the collapse it prevents is
    silent (reviewer, PR #333)."""
    if crew is None:
        return []
    return sorted(s for s, u in load().items() if u.get("crew") == crew)


def slugs_by_lifecycle(state: str) -> list[str]:
    """Registered project slugs in the given lifecycle state, sorted."""
    return sorted(s for s, u in load().items() if u.get("lifecycle") == state)


def active_slugs() -> list[str]:
    """The projects offered for work. What a picker or a drain should enumerate."""
    return slugs_by_lifecycle("active")
