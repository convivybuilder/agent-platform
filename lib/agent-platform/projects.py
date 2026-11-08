"""The project registry (ARCH-358, remodelled by ARCH-426) — one canonical record
of the Convivy Builder's projects, and of the commercial products they build.

A PROJECT is a unit of work: one thing being built, with a home directory, the
repos that serve it, and a dev crew. A PRODUCT is a commercial entity. They are
separate dimensions linked many-to-many, which is ARCH-426's central correction —
the old model treated them as one dimension with a `class` flavour, and that
collapse is why `commercial` and two projects' own narrow-profile satellites
ended up in the signals table's `product` column as if they were scopes.

The test that decides how many projects exist is the WORK test: one co, one crew,
one thing being built. Repo boundaries are NOT that test, and reaching for them is
the attractive mistake — they are visible, countable, and already carry the data,
while the real unit is invisible in the stores. A project may have two repos
(ARCH-426 amendment); repos are a service to a project.

This module loads the registry — the Library's `GET /registry`, or the file
`PROJECTS_JSON` names when set — and exposes it to the consumers that
today hand-maintain the same facts in six scattered lists (cost_join folds,
velocity BUCKETS, agent-activity-report's _PUBLIC_TASK_PRODUCTS, agentctl's
LOCAL_AGENT_REPOS, the KB validators' ALLOWED_PROJECTS). `tests/test_projects.py`
fails red when a consumer list drifts from it. Consumers read this loader as
they're next touched.

The registry keys on the project slug; the legacy `product` field names in the
control DB, telemetry, and dashboards stay as they are (renaming them would be
churn with no behavior change). This loader is where the vocabularies meet.

The `_products` block's own key order is a display order a downstream consumer
reads from `products()`'s key order rather than hardcoding its own list. The
top-level `_kb_projects` block carries facts about a KB PROJECT NAME rather
than about a project unit or a `_products` entry — which KB projects are
`standalone` (shown outside the per-project product groups), in the same
key-order-is-display-order convention; see `standalone_kb_projects()` — and
which display `section` each belongs to; see `kb_project_section_for()`. A
`_products` entry may carry a `group`, a second display grouping above the
individual product; see `product_group_for()`. A project unit or a
`_products` entry may also carry `rollup`, naming which Chart House fold its
activity joins beyond the ordinary one; see `rollup_for()`/
`product_rollup_for()`. A project unit may carry `code_health`, the repos
whose CI health it is responsible for tracking; see
`code_health_repos_for()`.

A project unit may also carry `activation_lanes`, the per-unit activation-profile
lane names an external roster assigns it; `repo_display`, a per-repo
display-label override shaped like `repo_owners`; and `sandbox_repos`, repos
with no engineering signal that an external capture process excludes and this
platform's own CI mapping and code health ignore. A top-level `_release_names`
list names extra strings a release check refuses alongside every registry key
and display value. None of the four has a dedicated accessor here: the
external consumer reads the registry through its own client, and a caller in
this repo reads `load()`/`get()` directly, the way `repo_owners` is read.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from functools import lru_cache
from pathlib import Path
from typing import Any

import builder_root as BR
import vault_plane

KB_VALUES = ("central", "local", "split")


class RegistryUnavailable(RuntimeError):
    """Raised when the project registry cannot be read: `PROJECTS_JSON` is
    unset and the Library's `GET /registry` did not answer 200 with a JSON
    object, or no registry token was available to ask it at all. The message
    names the cause — the HTTP status code, or the exception class and the
    URL path — and never the token.

    This registry decides which projects are private, and `private_slugs()`'s
    own docstring states the rule it has to satisfy: a privacy control must
    not fail open. A failed read is "unknown", never "no private crews", so
    every caller here raises rather than falling back to an empty or partial
    document — `_load_raw()` and `_load_raw_live()` both raise this, and
    nothing in this module catches it."""


# THE REGISTRY SOURCE (CQ·563, Epic E37, the operator's approval 2026-10-25).
# `$PROJECTS_JSON`, when set, names a file — a packaged tenant instance (§8,
# ARCH-358), or a test's own synthetic registry — and is read exactly as
# before. Unset, the registry is served by the Library at `GET /registry`
# (the Library protocol's §6.8), the one copy an install's crew registry now
# lives in rather than a file that diverges the moment a second machine reads
# its own stale copy.
#
# NO OTHER FALLBACK REMAINS. This resolver used to also try this module's own
# tree's `config/projects.json`, then the operator's checkout at
# `<root>/agent-platform/config/projects.json` — the shape that let a copy
# deploy fall back to a live file rather than serve a stale one. An install
# now has exactly one registry, so there is nothing left for either fallback
# to answer for. `crew_registration.register` (CQ·564) sends a new crew to
# the Library's `POST /registry/crews` instead of writing a file, so this
# module's read side and that module's write side now agree on there being
# exactly one registry, with no file either of them falls back to.
#
# `PROJECTS_JSON` is resolved once, into the module attribute of the same
# name, at import time, same as before this module could read the Library at
# all; its unset state is `None`, never a path nothing reads — tests still
# monkeypatch `projects.PROJECTS_JSON` to a synthetic file.
#
# THE ENV OVERRIDE IS OPERATOR-LEVEL (env at process start) in the
# single-operator deployment, so it's trusted today. If the platform ever runs
# where a less-privileged process can influence the environment (multi-tenant
# containers, job-parameter-driven env), constrain the resolved path to an
# expected prefix before opening — else a crafted file could flip a data-local
# unit's task_text_public and leak its task text (security-reviewer, PR #136).
def _registry_path() -> Path | None:
    env = os.environ.get("PROJECTS_JSON")
    return Path(env) if env else None


PROJECTS_JSON = _registry_path()

# The one credential this module ever sends, and the one route it is good
# for: `GET /registry`. `AGENT_PLATFORM_REGISTRY_TOKEN` env, else the 0600
# file `_registry_token_file()` names — mirrors `signal_client.token()`'s
# env-then-file shape.
REGISTRY_TOKEN_ENV = "AGENT_PLATFORM_REGISTRY_TOKEN"
_REGISTRY_TOKEN_RELATIVE = Path(".orient-control") / "agent-platform-registry-reader-token"

# The Library's own route for the whole registry document (§6.8).
_REGISTRY_ROUTE = "/registry"

# Loopback-equivalent; the registry read sits on dispatch's critical path in
# places (`knowledge-ctx`, `agentctl`), so a slow or hung Library must not
# hold it forever.
_REGISTRY_TIMEOUT_S = 5


def _registry_token_file() -> Path:
    """Where the registry-reader credential lives, resolved from `Path.home()`
    fresh on every call rather than cached once at import — unlike
    `signal_client.TOKEN_FILE`, this is read once per process at most
    (`_load_raw()`'s cache) and in a daemon's whole lifetime at most once a
    minute (`_load_raw_live()`'s TTL), so there is no hot-loop cost to paying
    for a fresh `Path.home()` every time, and a test can point `Path.home()`
    at its own directory and see this resolve under it."""
    return Path.home() / _REGISTRY_TOKEN_RELATIVE


def _registry_token() -> str | None:
    """The registry-reader credential from `AGENT_PLATFORM_REGISTRY_TOKEN` or
    `_registry_token_file()`, or `None` when neither holds one. Never falls
    back to `signal_client.token()`'s general credential: that one lacks
    `registry.read` and the Library refuses it with 403."""
    tok = os.environ.get(REGISTRY_TOKEN_ENV, "").strip()
    if tok:
        return tok
    try:
        return _registry_token_file().read_text().strip() or None
    except OSError:
        return None


def _fetch_registry_from_library() -> dict[str, Any]:
    """The whole registry document, read fresh from the Library's
    `GET /registry` — raises `RegistryUnavailable` on every way that can fail:
    no token, a network error or timeout, any status other than 200, or a
    200 body that is not a JSON object (the Library's own contract: "never an
    empty document", and "treat any answer other than 200 as no registry").
    Never caught here; every caller fails closed on the raise."""
    token = _registry_token()
    if not token:
        raise RegistryUnavailable(
            f"no registry token: {REGISTRY_TOKEN_ENV} is unset and "
            f"{_registry_token_file()} could not be read")
    req = urllib.request.Request(
        vault_plane.library_url(_REGISTRY_ROUTE),
        headers=vault_plane.library_headers(token=token))
    try:
        with urllib.request.urlopen(req, timeout=_REGISTRY_TIMEOUT_S) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RegistryUnavailable(f"GET {_REGISTRY_ROUTE} returned {e.code}") from e
    except Exception as e:
        raise RegistryUnavailable(
            f"GET {_REGISTRY_ROUTE} failed: {e.__class__.__name__}") from e
    if not isinstance(body, dict):
        raise RegistryUnavailable(
            f"GET {_REGISTRY_ROUTE} returned a {type(body).__name__}, not a "
            f"JSON object")
    return body


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
# `data_local` is deliberately NOT here. `git grep` finds no reader of it in
# lib/ or bin/ today — `crew_registration.build_row` writes it
# (`spec.data_local`), and `public_task_slugs()` reads `task_text_public`,
# not this field — so requiring it would be a schema rule with nothing behind
# it to enforce.
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
KB_LEGACY_KEY = "_kb_legacy"
KB_PROJECTS_KEY = "_kb_projects"
BUSINESS_UMBRELLA_KEY = "_business_umbrella_dir"

# The only fields a `_kb_projects` entry may carry today (the steward's
# ruling, 2026-10-22, widened the same day to add `section`).
_KB_PROJECT_FIELDS = ("standalone", "section")

# The Chart House folds a unit's or a product's `rollup` may name, beyond the
# ordinary one Chart House already keys by unit name (repos) and by product
# (cost) — the steward's ruling, 2026-10-22.
ROLLUP_VALUES = ("repos", "cost")

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


def _declared_kb_targets(raw: dict[str, Any], *, via_kb_scope_exceptions: bool = True) -> set[str]:
    """Every KB project slug `_validate_kb_project_targets` treats as real for a
    `kb_project_by_repo` target: a central/split unit's own `kb_project`
    (`kb_slugs()`'s own rule, inlined against `raw` directly — `kb_slugs()`
    reads through `load()`/`_load_raw()`, which calls
    `_validate_kb_project_targets`, so calling it here would recurse), some
    `kb_scope_exceptions` entry's `kb_project` (a recorded, reviewed
    narrowing), or a `_kb_legacy` slug.

    `via_kb_scope_exceptions=False` drops the middle route, which is what a
    `kb_scope_exceptions` target itself must be checked against:
    `_validate_kb_project_targets` folding every unit's own
    `kb_scope_exceptions` value into this set and then checking a
    `kb_scope_exceptions` target against that same set is circular — such a
    target, real or fictional, is always a member of the set it just
    contributed to (agent-platform#988 round 4, security-reviewer). A
    `kb_project_by_repo` target has no such problem, since no route folds a
    `kb_project_by_repo` value into this set.

    A unit whose `kb` is not `central`/`split` contributes nothing from its own
    `kb_project` field — a `kb: local` unit may carry a stray `kb_project` with
    no KB posture behind it (agent-platform#988 round 3, security-reviewer),
    and the central/split filter is what keeps that from counting as a
    declaration. Every `kb_scope_exceptions` value must itself be a non-empty
    string to count — an empty one bypassed this set until round 3 (reviewer,
    round 2) because `isinstance("", str)` is true even though `""` names
    nothing."""
    out: set[str] = set()
    for key, unit in raw.items():
        if key.startswith("_") or not isinstance(unit, dict):
            continue
        if unit.get("kb") in ("central", "split"):
            kb_project = unit.get("kb_project", key)
            if isinstance(kb_project, str) and kb_project:
                out.add(kb_project)
        if via_kb_scope_exceptions:
            for exc in unit.get("kb_scope_exceptions", {}).values():
                if isinstance(exc, dict) and isinstance(exc.get("kb_project"), str) \
                        and exc["kb_project"]:
                    out.add(exc["kb_project"])
    legacy = raw.get(KB_LEGACY_KEY, [])
    out.update(s for s in legacy if isinstance(s, str))
    return out


def _validate_kb_project_targets(raw: dict[str, Any]) -> None:
    """Refuse a registry where a `kb_project_by_repo` or `kb_scope_exceptions`
    value names no real KB project — the audience-boundary check the
    security-reviewer asked for on agent-platform#988 round 2. Before this
    field existed, every mapping was a Python literal with a required
    per-entry comment (PRs #435, #508, #552 and #358 each caught a leak class
    exactly this shape would reintroduce silently), and a code PR through
    full review was what it took to change one; this is the same check,
    enforced at load rather than only at review time.

    A `kb_project_by_repo` target is declared when it is a central/split
    unit's own `kb_project`, some `kb_scope_exceptions` entry's `kb_project`,
    or a `_kb_legacy` slug (`_declared_kb_targets`) — nothing else, including
    a name equal to the entry's own key. Round 2 also accepted a
    `kb_project_by_repo` entry whose value equals its own key with no other
    declaration, meant for a `kb: local` unit naming its own repo's slice
    with no unit's `kb_project` naming it; the security-reviewer found that
    exception scoped to the entry, not to the declaring unit, so any unit
    could declare any name that way, including a private crew's slug (round
    3). Every such live case is declared through `_kb_legacy` instead
    (agent-platform#978), so the exception is gone outright rather than
    narrowed.

    A `kb_scope_exceptions` target is declared the same way MINUS the middle
    route: a central/split unit's own `kb_project`, or a `_kb_legacy` slug —
    never another `kb_scope_exceptions` entry, including its own. Round 3's
    `_declared_kb_targets` folded every unit's `kb_scope_exceptions` value
    into the declared set before this check ran, so a `kb_scope_exceptions`
    target was always a member of the set it had just contributed to and
    could never be refused, fictional or belonging to another crew entirely
    (round 4, security-reviewer)."""
    declared = _declared_kb_targets(raw)
    declared_without_scope_exceptions = _declared_kb_targets(raw, via_kb_scope_exceptions=False)
    for slug, unit in raw.items():
        if slug.startswith("_") or not isinstance(unit, dict):
            continue
        for name, entry in unit.get("kb_project_by_repo", {}).items():
            if not isinstance(entry, dict):
                raise ValueError(
                    f"{slug}.kb_project_by_repo[{name!r}] must be an object with "
                    f"kb_project and reason, not {entry!r}")
            kb_project = entry.get("kb_project")
            if not isinstance(kb_project, str) or not kb_project:
                raise ValueError(
                    f"{slug}.kb_project_by_repo[{name!r}] needs a non-empty kb_project")
            reason = entry.get("reason")
            if not isinstance(reason, str) or not reason:
                raise ValueError(
                    f"{slug}.kb_project_by_repo[{name!r}] needs a non-empty reason")
            if kb_project not in declared:
                raise ValueError(
                    f"{slug}.kb_project_by_repo[{name!r}] names {kb_project!r}, which "
                    f"is no central/split unit's own kb_project, no "
                    f"kb_scope_exceptions entry's kb_project, and not in "
                    f"{KB_LEGACY_KEY} — refusing to load a registry that would hand "
                    f"a dispatch an undeclared KB project")
        for repo, exc in unit.get("kb_scope_exceptions", {}).items():
            if not isinstance(exc, dict):
                raise ValueError(f"{slug}.kb_scope_exceptions[{repo!r}] must be an object")
            kb_project = exc.get("kb_project")
            if not isinstance(kb_project, str) or not kb_project:
                raise ValueError(
                    f"{slug}.kb_scope_exceptions[{repo!r}] needs a non-empty kb_project")
            if kb_project not in declared_without_scope_exceptions:
                raise ValueError(
                    f"{slug}.kb_scope_exceptions[{repo!r}] names {kb_project!r}, which "
                    f"is no central/split unit's own kb_project and not in "
                    f"{KB_LEGACY_KEY} — a kb_scope_exceptions target cannot be declared "
                    f"by another kb_scope_exceptions entry, refusing to load a registry "
                    f"that would hand a dispatch an undeclared KB project")


# A unit's own slug shape (`crew_registration._SLUG_OK`, duplicated here rather
# than imported since that module already imports this one and importing it
# back would cycle): lowercase, letters/digits/hyphens, leading letter.
_SIGNAL_PRODUCT_RE = re.compile(r"^[a-z][a-z0-9-]*$")


def signal_product(slug: str) -> str:
    """The product string `signal_client.product_for_channel` stamps a
    channel's signal with, for the unit `slug` names: that unit's own
    optional `signal_product` registry field when it sets one, else `slug`
    itself.

    `signal_product` exists only to keep a product string a signal reader
    already stores unchanged: every downstream reader of a signal's
    `product` matches the stored string exactly and none canonicalizes it
    through this registry, so a unit whose channel-matching slug would
    otherwise differ from the product string already written under it sets
    this field to keep that signal's history from splitting (reviewer, PR
    #982 round 1).

    Raises ValueError when the unit sets `signal_product` to something that
    is not a non-empty, slug-shaped string (`_SIGNAL_PRODUCT_RE`) — a
    malformed override is a registry bug to surface, not something to fall
    back from silently."""
    unit = get(slug) or {}
    override = unit.get("signal_product")
    if override is None:
        return slug
    if not isinstance(override, str) or not _SIGNAL_PRODUCT_RE.match(override):
        raise ValueError(
            f"{slug}: signal_product {override!r} must be a non-empty, "
            f"slug-shaped string")
    return override


@lru_cache(maxsize=None)
def _load_raw() -> dict[str, Any]:
    # PROJECTS_JSON set: read once at process start (a static config for the
    # run); cached so the many helper calls below don't re-parse it each
    # time. Unset: one fetch from the Library per process, cached the same
    # way — `_load_raw.cache_clear()` is what a long-running caller (bin/co,
    # bin/run-register-api) already calls to see a change; see
    # `_load_raw_live()` for the one that can't.
    if PROJECTS_JSON is not None:
        with open(PROJECTS_JSON) as f:
            raw = json.load(f)
    else:
        raw = _fetch_registry_from_library()
    _validate_kb_project_targets(raw)
    return raw


def raw_document() -> dict[str, Any]:
    """The whole registry document, unfiltered: every project entry and every
    top-level `_`-prefixed key (`_meta`/`_products`/`_scopes`/etc.), exactly
    as the file `PROJECTS_JSON` names stores it, or the Library's `GET
    /registry` answers it when unset. `load()`'s own callers want the
    per-project view with the meta keys dropped; this is for a caller that
    forwards the registry itself rather than reading through one of this
    module's accessors."""
    return _load_raw()


def load() -> dict[str, dict[str, Any]]:
    """The registry as {slug: unit-dict}, with the leading `_meta` block dropped."""
    return {k: v for k, v in raw_document().items() if not k.startswith("_")}


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


def cost_fold_map() -> dict[str, str]:
    """Every string in a unit's own `cost_fold_aliases` → that unit's
    `display`: the curated product-bucket fold `cost_join._canonical_product`
    reads instead of hand-maintaining its own copy of a few products' legacy
    and umbrella names.

    DELIBERATELY NARROWER than `fold_map()`, which folds every unit's
    `repos`/`aliases` to its `display`. Adopting that instead for cost
    reporting would fold an ordinary project's own slug to a Title-Case
    display too (`widget` -> `Widget`), which is not what `_canonical_product`
    has ever done for a project with no legacy-rename or umbrella-split
    history behind it. A unit with no `cost_fold_aliases` contributes nothing
    here, which is the conservative, unchanged default."""
    out: dict[str, str] = {}
    for u in load().values():
        for raw in u.get("cost_fold_aliases", []):
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
    what gives an adopter's crew, which is in no registry entry, its
    crew-scoped instruction blocks. A private crew never takes that path, under
    any spelling (`default_profile_refused`), so a case variant of a private
    crew's name still resolves to nothing.

    NOT `crew_for`, WHICH TAKES A PROJECT SLUG. That distinction is the whole
    reason this exists: the registry keys on project, and a project holds a LIST
    of repos, so `crew_for("agent-platform")` returns None — the project is
    `bosun`. Two reviewers independently proposed `crew_for(repo)` as the fix for
    exactly that bug and it would have returned None for every repo whose crew
    differs from its own name, turning a wrong-key lookup into a no-key lookup.

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


def repo_github_slug(repo: str, unit: dict[str, Any] | None = None) -> str:
    """`owner/repo` for *repo*, the expansion `--on` has always carried: a name
    already containing `/` is returned unchanged. Otherwise the owner is the
    repo's own entry in its unit's `repo_owners` map, else the unit's
    `github_owner`, else `"convivy"` — an unregistered name is `"convivy"`,
    matching every call site's behaviour before this function existed.

    `unit`, when given, is used in place of searching `load()` for the owning
    unit — for a caller, such as `crew_registration.py` or
    `check-ci-workflow-drift`, that already holds the unit dict from
    somewhere other than this module's `load()` (the original case was
    `issue_gate.py`'s own registry snapshot read from GitHub's contents API
    at `origin/main`, avoiding a dispatched coder's own writable checkout as
    the source of truth; that script is since deleted) and passes it straight
    in rather than asking this function to repeat a search it cannot make
    against a registry it never loaded.

    This is the ONE place that expansion lives; `bin/agentctl`'s five inline
    copies, `run_verdict._slug` and `tool_policy.repo_slug` all call through
    here now, so a repo that needs a non-`convivy` owner (homebrew-tap, under
    Builder's own `convivybuilder` account) is named in exactly one place."""
    if repo and "/" in repo:
        return repo
    if unit is None:
        for u in load().values():
            if repo in list(u.get("repos", [])) + list(u.get("aliases", [])):
                unit = u
                break
    owner = "convivy"
    if unit is not None:
        owner = (unit.get("repo_owners") or {}).get(repo) or unit.get("github_owner") or "convivy"
    return f"{owner}/{repo}"


def registered_repo_github_slug(repo: str | None) -> str | None:
    """`owner/repo` for *repo*, like `repo_github_slug`, but refusing an owner the
    registry does not itself attest to — for a caller whose `repo` string comes
    from a run-writable record (a dispatched run's own `meta.json`) rather than
    from the orchestrating session's own `--on`.

    `repo_github_slug` treats a `/`-qualified value as already resolved and
    returns it unchanged, which is correct for `--on` (the dispatcher's own
    input) but lets a run that writes its own `meta.json["repo"]` name ANY
    owner/repo, convivy or not (security-reviewer, PR #881 round 2). This
    function re-derives the slug the registry would assign the BARE repo name
    and only returns a `/`-qualified `repo` when it equals that — an owner the
    registry does not record for that bare name, named or unregistered, never
    comes back, so a forged `someone/elsewhere` resolves to None rather than
    pointing a `gh` query at an arbitrary repository.

    A bare name (no `/`) is resolved exactly as `repo_github_slug` resolves it,
    unregistered included: an unregistered bare name still returns
    `convivy/<name>`, the default every bare name has had since before this
    function existed. That default never leaves the convivy org, so it carries
    none of the risk a `/`-qualified forgery does, and refusing it here would
    drop a legitimate run on a repo the registry hasn't caught up to yet."""
    if not repo:
        return None
    if "/" not in repo:
        return repo_github_slug(repo)
    bare = repo.rsplit("/", 1)[1]
    registered = repo_github_slug(bare)
    return repo if repo == registered else None


_CI_WORKFLOW_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.ya?ml$")

DEFAULT_CI_WORKFLOWS = ["ci.yml"]


def ci_workflows_errors(unit: dict[str, Any]) -> list[str]:
    """Why `unit`'s optional `ci_workflows` field breaks its schema, or `[]` when
    it holds (absent included) — the Library's replacement for `ci_tracked.py`'s
    literal `(repo, workflow file)` pairs reads this field instead, so a bad entry
    here would silently stop a repo's CI from being tracked rather than erroring.

    Refuses: a non-dict value; a key that is not one of the unit's own `repos`
    (a unit cannot name CI for a repo it does not have); a value that is not a
    list of strings; and a file name that does not match `_CI_WORKFLOW_FILE_RE`,
    which rules out a path separator or a `..` segment.

    Shared by `crew_registration.build_row` (checked before a byte reaches the
    live registry) and `tests/test_projects.py::test_ci_workflows_wellformed`
    (checked against the committed registry), so the two cannot drift on what
    the field requires."""
    raw = unit.get("ci_workflows")
    if raw is None:
        return []
    if not isinstance(raw, dict):
        return [f"ci_workflows must be a dict, got {raw!r}"]
    repos = unit.get("repos", [])
    errors: list[str] = []
    for repo, files in raw.items():
        if repo not in repos:
            errors.append(f"ci_workflows names {repo!r}, not one of its own repos")
            continue
        if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
            errors.append(f"ci_workflows.{repo} must be a list of strings, got {files!r}")
            continue
        for name in files:
            if not _CI_WORKFLOW_FILE_RE.match(name):
                errors.append(
                    f"ci_workflows.{repo} names {name!r}, not a bare workflow file "
                    f"name (no path separator or '..' segment)")
    return errors


def ci_workflows_entry(repo: str) -> list[str] | None:
    """The unit's own `ci_workflows[repo]` list, or None when no unit names
    `repo` as a key there.

    The narrower sibling `ci_workflows` below calls: this returns None rather
    than the `ci.yml` default when the registry says nothing about `repo`,
    which lets a caller — `_repo_has_ci_workflows` in bin/agentctl among them
    — tell "the registry declares this repo has no PR CI" ([] here) apart
    from "the registry declares nothing about this repo" (None here), a
    distinction `ci_workflows`'s own default return value collapses."""
    for u in load().values():
        if repo in list(u.get("repos", [])) + list(u.get("aliases", [])):
            mapping = u.get("ci_workflows")
            if isinstance(mapping, dict) and repo in mapping:
                return list(mapping[repo])
    return None


def ci_workflows_entry_for_slug(slug: str) -> list[str] | None:
    """`ci_workflows_entry`'s `owner/repo` counterpart, for a caller that only
    has the fully-qualified slug `review-status --repo` takes (
    `_repo_has_ci_workflows` in bin/agentctl) rather than one of the
    registry's own bare repo names.

    Every row's `repos`/`aliases` entries are bare names, so matching `slug`
    against them directly never succeeds. This instead computes
    `repo_github_slug(repo, unit=u)` for each repo/alias of each unit and
    compares THAT against `slug` — the owner must match too, so a same-named
    repo registered under a different owner never picks up this row's
    `ci_workflows` entry. Returns that row's own `ci_workflows[repo]` list
    (`[]` included), or None when no row's repo resolves to `slug`."""
    for u in load().values():
        for repo in list(u.get("repos", [])) + list(u.get("aliases", [])):
            if repo_github_slug(repo, unit=u) == slug:
                mapping = u.get("ci_workflows")
                if isinstance(mapping, dict) and repo in mapping:
                    return list(mapping[repo])
                return None
    return None


def ci_workflows(repo: str) -> list[str]:
    """The CI workflow file name(s) that run on `repo`'s pull requests.

    Resolves `repo` to its unit the same way `crew_for_repo` does — a `repos` or
    `aliases` membership match — then reads that unit's own `ci_workflows[repo]`
    via `ci_workflows_entry`. `["ci.yml"]` is the default for every other case:
    a unit with no `ci_workflows` field, a unit whose `ci_workflows` does not
    mention this repo, and a repo no unit lists at all — the registry says
    nothing about an unregistered repo's CI, and `ci.yml` is what most repos
    run, so it is the answer that is right far more often than wrong. A unit
    that maps `repo` to `[]` means that repo has no pull-request CI to track,
    and that empty list is returned unchanged rather than falling through to
    the default."""
    entry = ci_workflows_entry(repo)
    if entry is not None:
        return entry
    return list(DEFAULT_CI_WORKFLOWS)


def kb_scope_exceptions() -> dict[str, dict[str, str]]:
    """Every repo across the registry with a recorded, deliberate knowledge-scope
    exception: `{repo: {"kb_project": ..., "reason": ...}}`, gathered from each
    project unit's own `kb_scope_exceptions` entry in the registry.

    A row here is the registry's record that the repo's knowledge scope is
    narrower than its project's own `kb_project` on purpose — a security review
    found the wider scope unsafe for that repo, or the project's own kb_project
    resolves to no real profile. `_explicit_project_by_name` (below) reads this
    same field directly for its narrowing rows, so `EXPLICIT_PROJECT_BY_NAME`
    cannot drift from it by construction — a unit's `kb_project_by_repo` must
    not ALSO claim a name this field narrows (tests/test_projects.py), which is
    what keeps the two from recording the same repo two different ways. That is
    the link a later change deriving scope from the registry has to consult
    before it could widen a repo back — without it, the derivation would do
    exactly that silently (issue_01M3V1J89CG19ZGD3JY4WFY7V0)."""
    out: dict[str, dict[str, str]] = {}
    for unit in load().values():
        out.update(unit.get("kb_scope_exceptions", {}))
    return out


# The KB project a DIRECTORY or REPO name resolves to when the name is not its own
# project. Read by `bin/knowledge-ctx` (`detect_project`) and `bin/agentctl`
# (`default_profile`); it replaced two copies that had to be pinned equal by a
# test, and it was itself a hand-maintained module-level table until this
# function replaced it with two per-unit registry fields — a repo, alias or
# other dispatch-time name no longer needs a code change here to resolve, only
# a registry edit (`kb_project_by_repo` is operator-only: the operator's
# install file, or the Library's guarded `--replace` load).
#
# `kb_project_by_repo` and `kb_scope_exceptions` are DISJOINT by construction
# (enforced by tests/test_projects.py), so a name resolves through at most one
# of them: `kb_scope_exceptions` is the one place a narrowing below a unit's
# own `kb_project` lives (its own docstring above states that invariant), and
# `kb_project_by_repo` is every other explicit name this registry resolves —
# most commonly a unit's own slug or repo, named explicitly here because the
# `<knowledge_root>/projects/<name>/` fallback below cannot be trusted to agree
# (an alias, a satellite repo, or a unit whose own KB posture is `local` and so
# carries no `kb_project` at all). Each entry is `{kb_project, reason}`: this
# field decides which crew's knowledge-base context a dispatch receives, so a
# wrong mapping is worse than a missing one, and `_validate_kb_project_targets`
# (above `_load_raw`) refuses to load a registry whose `kb_project` names no
# declared KB project or whose `reason` is missing or empty (security review,
# agent-platform#988 round 2).
def _explicit_project_by_name() -> dict[str, str]:
    """Every name `resolve_project` resolves explicitly, built fresh from each
    unit's own `kb_project_by_repo` and `kb_scope_exceptions` fields rather than
    read from a hand-maintained table — see the comment above this function.

    `{}` rather than a raised exception when the registry cannot be read: the
    hand-maintained table this replaces never touched the registry at all, so
    every existing caller of `resolve_project`/`EXPLICIT_PROJECT_BY_NAME`
    (`knowledge-ctx`'s `detect_project`, `agentctl`'s `default_profile`) still
    expects an unreadable registry to read as "no explicit mapping", the same
    way the directory-fallback below already fails closed on `OSError`, not as
    a crash through a path that used to need no registry at all. Since CQ·563,
    a `PROJECTS_JSON`-unset read goes to the Library's `GET /registry`, which
    raises `RegistryUnavailable` (a `RuntimeError`) rather than `OSError` on
    every way that can fail — caught here alongside `OSError` and
    `JSONDecodeError`, for the same no-explicit-mapping degrade (round 5
    review, agent-platform#988). A registry that parses but fails
    `_validate_kb_project_targets` is a different case — that raises
    `ValueError`, uncaught here, since a mapping proven to name an undeclared
    KB project must refuse loudly rather than read as "no mapping" and fall
    through to a directory check that was never the point."""
    try:
        raw_units = load()
    except (OSError, json.JSONDecodeError, RegistryUnavailable) as e:
        print(f"projects: _explicit_project_by_name() could not read the "
              f"registry ({e}); treating it as no explicit mapping this run.",
              file=sys.stderr)
        return {}
    out: dict[str, str] = {}
    for unit in raw_units.values():
        for name, entry in unit.get("kb_project_by_repo", {}).items():
            out[name] = entry["kb_project"]
        for repo, exc in unit.get("kb_scope_exceptions", {}).items():
            out[repo] = exc["kb_project"]
    return out


def __getattr__(name: str) -> Any:
    """`projects.EXPLICIT_PROJECT_BY_NAME` as a live, computed module attribute
    (PEP 562) rather than the hand-maintained dict it replaces, so every
    existing reader (`bin/agentctl`, `bin/co`, `bin/knowledge-ctx`, this
    module's own tests) keeps working against a plain dict, unchanged."""
    if name == "EXPLICIT_PROJECT_BY_NAME":
        return _explicit_project_by_name()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


_NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# A `co_*` colour field's required shape: co_style_render.py parses each as six
# hex digits and raises if they aren't, ending the render for every unit — the
# leading-"#"-only check this replaced let a value like "#zzzzzz" reach that
# parse (security-reviewer, PR #1066 round 1).
_HEX_COLOR_OK = re.compile(r"^#[0-9a-fA-F]{6}$")

# A `co_default_agent` value is joined into `.claude/agents/<name>.md` by
# bin/co's company-workspace branch — held to the bare, already-lowercase
# shape every registry agent name is written in, narrower than `_NAME_OK`
# above (no dots, no underscores, no uppercase).
_AGENT_NAME_OK = re.compile(r"^[a-z0-9-]+$")


def valid_crew(value: Any) -> bool:
    """Whether `value` is an allowed shape for a unit's `crew` or
    `tmux_session` field: `None` or a string — the rule `validate_registry`
    applies to both, extracted so a reader outside this module
    (`bin/render-co-style`) can reuse it rather than copy the check out of
    sync. `deck_key_for` in `co_style_render.py` uses a unit's `tmux_session`
    field when set, else its `crew` field, as a dict key for both Deck's
    `sessions` and `railNames` maps; a list or dict there raises
    `TypeError: unhashable type` building the key, and an int alongside
    other units' string keys raises later, sorting the keys to write
    (PR #1066 round 4; `tmux_session` checked the same way, CQ-603)."""
    return value is None or isinstance(value, str)


def valid_name(value: Any) -> bool:
    """Whether `value` is a bare name safe to join into a filesystem path: no
    `/` and no leading `.` (so a lone `..` component is refused along with
    it). The same shape `resolve_project` already requires of a repo name.

    For a caller joining a registry-sourced slug or repo name that is not
    routed through a registered unit's own validated `home_dir`
    (`home_path()` — see that function's docstring for why it doesn't apply
    to every such name: a `calls_shared_tools` entry or an `own_coverage`
    slug may name a sibling repo or a registry KEY that carries no
    `home_dir` of its own). `bin/metric-alerts` and `bin/check-cli-contracts`
    check this before joining a slug/repo name under a shared directory,
    since neither is joining a unit's own home (security-reviewer, PR #985
    round 2)."""
    return isinstance(value, str) and bool(_NAME_OK.match(value))


def resolve_project(name: str | None, knowledge_root: Path | str | None = None) -> str | None:
    """The KB project a directory or repo NAME maps to, or None.

    The registry's explicit rows (`_explicit_project_by_name`) win. Otherwise
    `<name>` maps to project `<name>` when `<knowledge_root>/projects/<name>/`
    exists, which is what lets an adopter's crews resolve without a row in the
    registry (company decision 0011). A name that is not a plain directory name
    resolves to nothing rather than reaching a path outside `projects/`."""
    if not name:
        return None
    explicit = _explicit_project_by_name().get(name)
    if explicit:
        return explicit
    if not _NAME_OK.match(name):
        return None
    root = Path(knowledge_root) if knowledge_root else BR.root() / "knowledge"
    # Exact-case match against the names on disk: a case-insensitive filesystem
    # would otherwise accept `Acme` for `projects/acme/` and hand back a slug
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
    `allowed_profiles` via `profile_allowlist()` below).

    Compared by `casefold()` because the disk is case-insensitive and a resolved
    cwd keeps whatever spelling the operator typed: a lowercase directory name
    still reaches an uppercase-named black box. An unreadable registry refuses
    as well; a check that cannot tell whether a directory is private must not
    hand it a profile."""
    folded = name.casefold()
    if folded in {r.casefold() for r in restricted_names}:
        return True
    try:
        return folded in _private_crew_names()
    except Exception:
        return True


def profile_allowlist() -> dict[str, set[str]]:
    """Every registered unit's optional `kb_profile_allowlist` field, as
    {directory name: allowed profiles} — knowledge-ctx's `allowed_profiles`/
    `check_profile_allowed` compare a DIRECTORY (`detect_product`'s own
    result) against this, never a registry slug, so each entry is keyed on
    the owning unit's `home_dir` basename, parsed directly from the stored
    field rather than through `home_path()`: a private unit's `home_path()`
    walks every path segment for a symlink (`_expand_own_home`), work this
    accessor has no need for, since all it wants is the directory name the
    field already stores.

    A unit absent from this mapping is unrestricted — most of them, so this
    costs nothing elsewhere. THIS IS A SECURITY CONTROL
    (platform/decisions/0043): it restricts which profiles a private crew's
    directory may individuate, and fails closed by construction, raising
    whatever `load()` raises on an unreadable registry rather than returning
    an empty mapping. A caller that read an empty mapping back from a failed
    fetch would mistake "couldn't ask" for "asked, and nothing restricts
    it" — exactly the failure mode a hardcoded dict literal could never
    exhibit, and the reason this is not wrapped in a broad `except` the way
    `default_profile_refused` wraps `_private_crew_names()` above.

    A field that is absent, or explicitly `null`, leaves the unit out of
    this mapping, unrestricted, the same as absent: a `null` reaches the
    registry only through a deliberate edit that removes the field's
    effect, never as a byproduct of some other change, so it carries the
    same meaning as never having set it. Any other value that is not a
    non-empty list of non-empty strings — `[]`, a bare string, a list
    holding a non-string or an empty string — maps the unit to the EMPTY
    set instead of skipping it: `check_profile_allowed` then refuses every
    profile for that unit's directory. This fails closed on a malformed
    field without raising, because `knowledge-ctx list` calls this at every
    session start and one crew's bad field must not stop every crew from
    launching; the failure stays confined to the one unit whose field is
    wrong."""
    allowlist: dict[str, set[str]] = {}
    for slug, unit in load().items():
        if "kb_profile_allowlist" not in unit:
            continue
        names = unit["kb_profile_allowlist"]
        if names is None:
            continue
        home_dir = unit.get("home_dir")
        key = Path(home_dir).name if isinstance(home_dir, str) and home_dir else slug
        valid = (
            isinstance(names, list)
            and names
            and all(isinstance(n, str) and n for n in names)
        )
        allowlist[key] = set(names) if valid else set()
    return allowlist


def postgres_socket_repos() -> list[str]:
    """Repo names granted a dedicated Postgres unix-socket in the run sandbox
    (`postgres_socket` == true), sorted — `run_sandbox.settings()`'s
    `network.allowUnixSockets` grant reads this instead of a hand-maintained
    tuple, so a project whose tests need a database gets the grant by setting
    the field on its own unit rather than by a code change here.

    Absent means no grant, the conservative default: the field opens a new
    sandbox exception, so a project a contributor registers with no
    `postgres_socket` field gets none rather than an unreviewed one."""
    return sorted(s for s, u in load().items() if u.get("postgres_socket") is True)


def explainer_donors() -> list[str]:
    """Shared-coverage products in the order `changelog-drain` tries them as the
    explainer donor — the first whose checkout carries `explainer.md` wins.

    NOT `shared_coverage_products()`'s order: that function sorts alphabetically,
    which is the right default for a set membership question but the wrong
    one here — `_agents_dir()`'s "first that exists" fallback makes the
    order ITSELF a live decision (which donor gets used when several
    checkouts exist at once, the common case), so it has to be stated
    rather than left to fall out of whichever field sort the member-list
    function happens to use. Ordered by each unit's own
    `explainer_donor_priority` (ascending; ties, and an absent priority —
    sorted last, as if infinite — broken by slug), so a registry edit to
    WHICH units are in the shared coverage does not silently reorder which one
    is tried first (reviewer, PR #985 round 2)."""
    rows = [(u.get("explainer_donor_priority", float("inf")), s)
            for s, u in load().items() if u.get("coverage") == "shared"]
    return [s for _, s in sorted(rows)]


MODEL_TIERS = ("opus", "sonnet")


def model_tier(slug: str) -> str:
    """The interactive co main-loop model tier for *slug* — `"opus"` unless the
    entry says otherwise.

    ABSENT MEANS OPUS, AND THAT DIRECTION IS THE WHOLE POINT. The operator inverted this
    default on 2026-09-16 because the old arrangement reached Opus through three
    lists and let everything else fall to a silent Sonnet, so several crews
    launched on Sonnet until he noticed the model. Forgetting to register a
    crew now costs Opus rather than judgement. A crew
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
    CREWS KEEP MISSING": two new crews worked for days on `co` alone and
    only broke on `co -r` ("cwd is not a recognized product repo"), because
    re-individuating a crew cockpit on refresh was gated on a literal nobody
    had added them to. A crew registered with
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
    lives on their own devices — several of Convivy's own consumer products are
    this. That is other people's data Convivy never holds; this is Convivy's own
    work that Convivy chooses not to put in its shared stores.
    """
    return {slug for slug, u in units().items() if u.get("private") is True}


def walled_off_slugs() -> set[str]:
    """The WALLED-OFF crew (platform/decisions/0100): the operator's own private
    use of the platform, registered so the platform knows it exists, with no row
    of its own install data carried in this repository.

    Read with `.get(...) is True`, the same direction `private_slugs()` takes, for
    the same reason — see that function's own docstring. `validate_registry`
    refuses an entry that carries both `walled_off` and `private`: a crew is one
    or the other, never both, so a reader never has to resolve a conflict between
    the two gates.

    NOT THE SAME CONTROL AS `private_slugs()`. A black box (`private: true`)
    launches with `--strict-mcp-config` and no connectors; the walled-off crew
    needs its account connectors (Gmail, Calendar, and the like), so `bin/co`
    denies the `ops`/`kb` MCP servers' own tools instead of stripping every
    server. Distinct fields keep the two launch shapes from being picked by a
    single flag that is right for one and wrong for the other.
    """
    return {slug for slug, u in units().items() if u.get("walled_off") is True}


def local_agent_repos() -> frozenset[str]:
    """Repo directory names whose agents resolve repo-locally (ARCH-177):
    `bin/agentctl`'s `resolve_agent` reads `<repo>/.claude/agents/<name>.md`
    for one of these BEFORE ever reaching the Library, so a repo-local agent's
    definition never enters the shared index. Every repo of a unit whose
    `local_agents` field is `True`.

    NOT THE SAME SET AS `private_slugs()`, despite every repo-local unit today
    also being private: a private crew with no dispatched agents at all (its
    own registry note says so) is private but carries no `local_agents`
    entry, because there is nothing for repo-local resolution to find there.
    The two fields answer different questions (does this crew's content reach
    the Library at all, versus does THIS repo's `.claude/agents/` serve a
    dispatch directly), and conflating them would have `resolve_agent` reading
    a repo-local directory that was never meant to exist.

    Read with `.get(...) is True`, the same direction `private_slugs()` takes,
    so a misspelled key leaves a unit resolving through the Library — the
    conservative failure here, since the Library path is the one every other
    crew already takes safely."""
    out: set[str] = set()
    for unit in units().values():
        if unit.get("local_agents") is True:
            out.update(unit.get("repos", []))
    return frozenset(out)


def typecheck_gates() -> dict[str, tuple[str, list[str]]]:
    """Every repo across the registry with a recorded typecheck gate for
    `agentctl publish`'s optional pre-flight check: `{repo: (dir, argv)}`,
    gathered from each unit's own `typecheck` entry in the registry.
    `dir` is the subdirectory (relative to the worktree) the command runs in;
    `argv` is a literal argument list, never a shell string, so there is
    nothing here for a shell to re-interpret. A repo absent from the result is
    simply not typecheck-gated — a table is honest about three repos not
    agreeing on one convention, where a universal `make typecheck` would
    pretend an agreement that does not exist."""
    out: dict[str, tuple[str, list[str]]] = {}
    for unit in load().values():
        for repo, gate in unit.get("typecheck", {}).items():
            out[repo] = (gate["dir"], list(gate["argv"]))
    return out


def default_profile_overrides() -> dict[str, str]:
    """Every repo across the registry whose `agentctl spawn` default profile
    name is NOT `daytime`: `{repo: profile_name}`, gathered from each unit's
    own `default_profile` entry in the registry. Read by
    `bin/agentctl`'s `default_profile()`, which otherwise assumes `daytime`."""
    out: dict[str, str] = {}
    for unit in load().values():
        out.update(unit.get("default_profile", {}))
    return out


def private_unit_repo_names(private: set[str] | None = None) -> dict[str, set[str]]:
    """Casefolded `repos` ∪ `aliases` for every BLACK BOX unit, keyed by its own
    slug — the name set `repo_github_slug` searches when it resolves a bare
    repo name to the unit that owns it, restricted to private units.

    `private`, when given, is the already-resolved private-slug set
    (`private_slugs()`, or a caller's own snapshot of it) rather than a second,
    independent read, so a caller building a snapshot for `dispatch_is_private`
    reads each private unit's own `repos`/`aliases` against the SAME registry
    generation its slug set came from. Omitted, it is read fresh via
    `private_slugs()`, raising exactly as that does on an unreadable registry."""
    slugs = private if private is not None else private_slugs()
    out: dict[str, set[str]] = {}
    for slug in slugs:
        unit = get(slug) or {}
        names = {*unit.get("repos", []), *unit.get("aliases", [])}
        out[slug] = {n.lower() for n in names if isinstance(n, str)}
    return out


def repo_is_private_unit(repo: str | None, private: set[str],
                         private_unit_repos: "dict[str, set[str]] | None" = None) -> bool:
    """Whether REPO string names a BLACK BOX unit (`platform/decisions/0043`) by
    any name it answers to: its own registry key (a member of `private`), or an
    entry of that unit's own `repos`/`aliases` — the same name set
    `repo_github_slug` searches when it resolves a bare repo name to the unit
    that owns it.

    An exact `repo.lower() in private` match alone checks only the KEY, which
    silently assumed a private unit's own `repos` entry equalled its slug; that
    assumption broke the day a private unit's repo was renamed without renaming
    its registry key (security-reviewer, PR #1015 round 1) — a dispatch against
    the renamed repo then read as public, and reached the `kb`/`ops` MCP servers
    ADR-0043 says a black box must never reach.

    `private` is the slug set the caller already resolved for the key half of
    this check — never re-read here, so this cannot straddle a different
    registry generation than the key check that produced it.
    `private_unit_repos`, when given, is a snapshot of
    `private_unit_repo_names()` taken from that SAME generation; omitted, it is
    read fresh (one `get(slug)` per private slug), which is fine for a one-shot
    live check but not for a caller judging many rows of one concurrent
    response — see `private_unit_repo_names`'s own docstring."""
    if not isinstance(repo, str):
        return False
    wanted = repo.lower()
    if wanted in private:
        return True
    repo_names = (private_unit_repos if private_unit_repos is not None
                 else private_unit_repo_names(private))
    return any(wanted in names for names in repo_names.values())


def privacy_invariant_errors(unit: dict[str, Any]) -> list[str]:
    """Why `unit` breaks the BLACK BOX contract ADR-0043 requires of a private
    crew, or `[]` when it holds. Shared by
    `test_a_private_crew_cannot_also_be_centrally_indexed` (checked at CI time,
    against the committed registry) and `crew_registration.build_row`
    (checked before a byte reaches the live registry), so the two cannot
    drift apart on what "private" requires.

    `private: true` says none of this crew's content reaches the Library and
    none of its agents reach Orient. Two other fields have to agree for that
    to be true: `kb: local` keeps it out of `kb_slugs()` and so out of the
    shared index, and `task_text_public: false` keeps its task strings off
    the public surfaces. An entry marked private with either of the two
    disagreeing would index its documents into the shared KB, or publish its
    task text, while every reader of the registry believed it was a black
    box — and nothing downstream would complain, so this has to be caught
    here rather than left to show up as a leak.
    """
    if unit.get("private") is not True:
        return []
    errors = []
    if unit.get("kb") != "local":
        errors.append(f"kb={unit.get('kb')!r}, not 'local'")
    if unit.get("task_text_public") is True:
        errors.append("task_text_public is true")
    return errors


def validate_registry(raw: dict[str, Any]) -> None:
    """Raises `ValueError` naming EVERY way `raw` — a whole registry document, in
    the shape the registry stores it (`_meta`/`_products`/`_scopes`/etc.
    included; skipped here the same way `load()` skips them) — breaks the schema,
    or returns `None` when it holds.

    Carries every check `tests/test_projects.py::test_schema_wellformed` makes
    against the registry, plus the BLACK BOX rule (`privacy_invariant_errors`,
    platform/decisions/0043) — delegated rather than re-checked inline, so a change
    to what "private" requires cannot drift between the two callers below.

    Shared by `test_schema_wellformed` itself (checked at CI time, against the
    registry a test reads) and `crew_registration.register` (checked in code, against the
    whole document it is about to write, before a byte reaches disk) — an install
    with no CI (platform/decisions/0098) has only the second caller, so the schema
    checks that used to run as a test have to also run as code.

    Collects every problem rather than raising on the first, so a caller reports
    all of them in one refusal instead of one failure at a time."""
    errors: list[str] = []
    for slug, u in raw.items():
        if not isinstance(slug, str) or slug.startswith("_"):
            continue
        if not isinstance(u, dict):
            errors.append(f"{slug} must be a JSON object, got {u!r}")
            continue
        for field in REQUIRED_FIELDS:
            if field not in u:
                errors.append(f"{slug} missing required field {field!r}")
        if u.get("class") not in CLASSES:
            errors.append(f"{slug} has bad class {u.get('class')!r}")
        if not (isinstance(u.get("display"), str) and u.get("display")):
            errors.append(f"{slug} display must be a non-empty string")
        if not (isinstance(u.get("repos"), list) and u.get("repos")):
            errors.append(f"{slug} repos must be a non-empty list")
        if not isinstance(u.get("aliases", []), list):
            errors.append(f"{slug} aliases must be a list")
        if u.get("kb") not in KB_VALUES:
            errors.append(f"{slug} has bad kb value {u.get('kb')!r}")
        elif u["kb"] in ("central", "split") and not u.get("kb_project"):
            errors.append(f"{slug} kb={u['kb']} needs kb_project")
        if not (isinstance(u.get("home_dir"), str) and u.get("home_dir")):
            errors.append(f"{slug} needs a home_dir")
        if not isinstance(u.get("dirs"), list):
            errors.append(f"{slug} dirs must be a list")
        if u.get("lifecycle") not in LIFECYCLES:
            errors.append(f"{slug} has bad lifecycle {u.get('lifecycle')!r}")
        if not isinstance(u.get("products"), list):
            errors.append(f"{slug} products must be a list")
        if not valid_crew(u.get("crew")):
            errors.append(f"{slug} crew must be a slug or null")
        if "tmux_session" in u and not valid_crew(u["tmux_session"]):
            errors.append(f"{slug} tmux_session must be a slug or null")
        if "crew_cockpit" in u and u["crew_cockpit"] is not True:
            errors.append(
                f"{slug} crew_cockpit must be true or absent, never a redundant false")
        if "local_agents" in u and u["local_agents"] is not True:
            errors.append(
                f"{slug} local_agents must be true or absent, never a redundant "
                f"false — local_agent_repos() reads the field's presence, like "
                f"crew_cockpit above")
        if "typecheck" in u:
            if not isinstance(u["typecheck"], dict):
                errors.append(f"{slug} typecheck must be a dict")
            else:
                for repo, gate in u["typecheck"].items():
                    if repo not in u.get("repos", []):
                        errors.append(
                            f"{slug} typecheck names {repo!r}, not one of its own repos")
                    if not isinstance(gate, dict):
                        errors.append(f"{slug}.{repo} typecheck entry must be a dict")
                        continue
                    if not (isinstance(gate.get("dir"), str) and gate["dir"]):
                        errors.append(f"{slug}.{repo} typecheck needs a non-empty dir")
                    argv = gate.get("argv")
                    if not (isinstance(argv, list) and argv
                            and all(isinstance(a, str) for a in argv)):
                        errors.append(
                            f"{slug}.{repo} typecheck argv must be a non-empty list of "
                            f"strings, never a shell string")
        if "default_profile" in u:
            if not isinstance(u["default_profile"], dict):
                errors.append(f"{slug} default_profile must be a dict")
            else:
                for repo, name in u["default_profile"].items():
                    if repo not in u.get("repos", []):
                        errors.append(
                            f"{slug} default_profile names {repo!r}, not one of its "
                            f"own repos")
                    if not (isinstance(name, str) and name):
                        errors.append(
                            f"{slug}.{repo} default_profile needs a non-empty profile name")
        for flag in ("co_self_contained", "co_generic_refresh", "company_workspace",
                     "co_writing_guide"):
            if flag in u and u[flag] is not True:
                errors.append(f"{slug} {flag} must be true or absent, never a redundant false")
        if "co_cockpit_gate" in u:
            if u.get("crew_cockpit") is not True:
                errors.append(
                    f"{slug} sets co_cockpit_gate but is not crew_cockpit: true")
            elif u["co_cockpit_gate"] not in CO_COCKPIT_GATES:
                errors.append(
                    f"{slug} co_cockpit_gate {u['co_cockpit_gate']!r} must be one of "
                    f"{CO_COCKPIT_GATES}")
        if "co_style_id" in u and not (
                isinstance(u["co_style_id"], str) and _NAME_OK.match(u["co_style_id"])):
            errors.append(
                f"{slug} co_style_id {u.get('co_style_id')!r} must match the "
                f"iTerm-profile/tmux-safe name rule")
        if "co_iterm_guid" in u and not (
                isinstance(u["co_iterm_guid"], str) and _NAME_OK.match(u["co_iterm_guid"])):
            errors.append(
                f"{slug} co_iterm_guid {u.get('co_iterm_guid')!r} must match the "
                f"iTerm-profile/tmux-safe name rule")
        if "co_label" in u and not (isinstance(u["co_label"], str) and u["co_label"]):
            errors.append(f"{slug} co_label must be a non-empty string when present")
        if "co_default_agent" in u and not (
                isinstance(u["co_default_agent"], str)
                and _AGENT_NAME_OK.match(u["co_default_agent"])):
            errors.append(
                f"{slug} co_default_agent {u.get('co_default_agent')!r} must be a "
                f"non-empty string matching ^[a-z0-9-]+$ — bin/co builds "
                f".claude/agents/<name>.md from it")
        for color_field in ("co_accent", "co_fg", "co_bg", "co_term_fg", "co_selection"):
            if color_field in u and not (
                    isinstance(u[color_field], str) and _HEX_COLOR_OK.match(u[color_field])):
                errors.append(
                    f"{slug} {color_field} {u.get(color_field)!r} must be a "
                    f"'#rrggbb'-shaped string")
        # co_badge (an iTerm profile's badge text) and co_deck_theme (a named Deck
        # palette theme, e.g. "indigo" — never a hex color) render alongside the
        # colour fields above (co_style_render.py, CQ-590) but are plain labels.
        for text_field in ("co_badge", "co_deck_theme"):
            if text_field in u and not (isinstance(u[text_field], str) and u[text_field]):
                errors.append(f"{slug} {text_field} must be a non-empty string when present")
        if "profiled_dirs" in u and not (
                isinstance(u["profiled_dirs"], list) and all(
                    isinstance(p, str) and p for p in u["profiled_dirs"])):
            errors.append(f"{slug} profiled_dirs must be a list of non-empty strings")
        if "agent_prefix" in u:
            raw_prefix = u["agent_prefix"]
            values = (raw_prefix,) if isinstance(raw_prefix, str) else raw_prefix
            if not (isinstance(values, (list, tuple)) and values and all(
                    isinstance(v, str) and v and v.endswith("-") for v in values)):
                errors.append(
                    f"{slug} agent_prefix must be a non-empty string ending in '-', "
                    f"or a list of such strings")
        if "postgres_socket" in u and u["postgres_socket"] is not True:
            errors.append(
                f"{slug} postgres_socket must be true or absent, never a redundant false")
        if "cost_fold_aliases" in u:
            aliases = u["cost_fold_aliases"]
            if not (isinstance(aliases, list) and aliases):
                errors.append(
                    f"{slug} cost_fold_aliases must be a non-empty list when present")
            elif not all(isinstance(a, str) and a for a in aliases):
                errors.append(f"{slug} cost_fold_aliases entries must be non-empty strings")
        if "kb_profile_allowlist" in u:
            allowed = u["kb_profile_allowlist"]
            if not (isinstance(allowed, list) and allowed):
                errors.append(
                    f"{slug} kb_profile_allowlist must be a non-empty list when present")
            elif not all(isinstance(p, str) and p for p in allowed):
                errors.append(
                    f"{slug} kb_profile_allowlist entries must be non-empty strings")
        if "created_by" in u and not valid_person_id(u["created_by"]):
            errors.append(
                f"{slug} created_by {u['created_by']!r} is not a Library person id "
                f"(person_<ULID>)")
        if "velocity_order" in u and not (
                isinstance(u["velocity_order"], int)
                and not isinstance(u["velocity_order"], bool)):
            errors.append(f"{slug} velocity_order must be an int when present")
        if "adr_kb_projects" in u:
            v = u["adr_kb_projects"]
            if not (isinstance(v, list) and all(isinstance(s, str) and s for s in v)):
                errors.append(f"{slug} adr_kb_projects must be a list of non-empty strings")
        if "github_owner_private" in u and u["github_owner_private"] is not True:
            errors.append(
                f"{slug} github_owner_private must be true or absent, never a "
                f"redundant false")
        if "walled_off" in u and u["walled_off"] is not True:
            errors.append(
                f"{slug} walled_off must be true or absent, never a redundant false")
        if u.get("walled_off") is True and u.get("private") is True:
            errors.append(
                f"{slug} carries both walled_off and private — a crew is one or "
                f"the other, never both")
        private_errors = privacy_invariant_errors(u)
        if private_errors:
            errors.append(f"{slug} is private but {'; '.join(private_errors)}")
    if errors:
        raise ValueError(f"{len(errors)} registry problem(s): " + "; ".join(errors))


def dispatch_is_private(repo: str | None, agent_source: str | None = None, *,
                        private_slugs_snapshot: "set[str] | None" = None,
                        home_paths_snapshot: "dict[str, Path] | None" = None,
                        private_unit_repos_snapshot: "dict[str, set[str]] | None" = None) -> bool:
    """Whether a dispatch reaches a black-box crew (`platform/decisions/0043`),
    checked by EITHER signal that can say so.

    `repo` ALONE IS NOT ENOUGH: `bin/agentctl`'s `resolve_agent()` honours
    `--agents-dir` before it ever looks at `repo`, so
    `--on acme --agents-dir <root>/BLACKBOX/.claude/agents` loads a black-box
    crew's agent body while `repo` stays `"acme"`, and a repo-only check misses
    it (security-reviewer, run-1031c63b — the bug that forced
    `--strict-mcp-config` to check the resolved agent's `source` too, and the
    same bug reopened the built-in-tool exfil path when it wasn't threaded
    through there — security-reviewer, run-b5dee6b0). `agent_source` is the file the caller actually resolved and
    read (`resolve_agent()` always hands it back); gate on whether it resolves
    under a private crew's `home_path()` too. Both sides go through
    `.resolve()` so a symlink or an unresolved `..` segment can't slip past
    the comparison. Optional and `None`-safe: a caller with only `repo` (or a
    recorded dispatch predating this field) gets the repo-only half.

    THE REPO HALF DELEGATES TO `repo_is_private_unit`, which checks `repo`
    against a private unit's own `repos`/`aliases` as well as its registry key
    — an exact `repo.lower() in private` match alone missed a private unit
    whose repo was renamed without renaming its key (security-reviewer, PR
    #1015 round 1). See that function's docstring.

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

    `private_slugs_snapshot`/`home_paths_snapshot`/`private_unit_repos_snapshot`
    let a caller that must judge every row of ONE response against the SAME
    registry generation pass them in, instead of this function reading
    `private_slugs()` (twice, with no snapshot of its own), `home_path()` and
    `private_unit_repo_names()` fresh per call. `bin/run-register-api`'s
    `ThreadingUnixHTTPServer` serves `/runs` and `/sessions` concurrently, so
    two calls made moments apart — even within this one function — can
    straddle another thread's cache clear-and-repopulate and land on two
    different registry generations (reviewer + security-reviewer, round 4).
    All three default to `None`, which reads live via `private_slugs()`/
    `home_path()`/`private_unit_repo_names()` exactly as before — every
    existing caller (`bin/agentctl`, `session_state._crew_is_shareable`) is
    unaffected.
    """
    private = private_slugs_snapshot if private_slugs_snapshot is not None else private_slugs()
    if repo_is_private_unit(repo, private, private_unit_repos_snapshot):
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


def private_home_for_dispatch(repo: str | None, agent_source: str | None = None, *,
                              private_slugs_snapshot: "set[str] | None" = None,
                              home_paths_snapshot: "dict[str, Path] | None" = None,
                              private_unit_repos_snapshot: "dict[str, set[str]] | None" = None
                              ) -> Path | None:
    """The home directory of the black-box crew (`platform/decisions/0043`) this
    dispatch reaches, or `None` for a public dispatch.

    ANSWERS WHICH ONE, not merely whether one matched: `dispatch_is_private`
    collapses the same two signals (`repo` against a private unit's own
    key/`repos`/`aliases`, or `agent_source` resolving under a private unit's
    `home_path()`) to a bool, which is enough to gate a transport wall but not
    enough to know WHERE a private dispatch's own run corpus belongs (ADR-0043's
    fourth surface — the run directory `bin/agentctl`'s `_private_run_root`
    routes into this crew's home instead of the shared `RUNS_ROOT`).

    THE REPO HALF DELEGATES TO `repo_is_private_unit`, called once per candidate
    slug (rather than once over the whole set, the way `dispatch_is_private`
    calls it) so the comparison itself is never re-derived here — only WHICH
    slug matched is new. Iterating this way costs nothing extra in the common
    case: a real registry carries a small, fixed number of private crews.

    Same three snapshot parameters as `dispatch_is_private`, for the same
    reason — a caller judging many rows of one concurrent response passes its
    own already-resolved `private_slugs()`/`home_path()`/
    `private_unit_repo_names()` rather than have each call re-read them fresh
    and risk straddling two registry generations."""
    private = private_slugs_snapshot if private_slugs_snapshot is not None else private_slugs()
    repo_names = (private_unit_repos_snapshot if private_unit_repos_snapshot is not None
                 else private_unit_repo_names(private))
    for slug in private:
        if repo_is_private_unit(repo, {slug}, {slug: repo_names.get(slug, set())}):
            return (home_paths_snapshot.get(slug) if home_paths_snapshot is not None
                   else home_path(slug))
    if agent_source:
        resolved_source = Path(agent_source).resolve()
        for slug in private:
            home = (home_paths_snapshot.get(slug) if home_paths_snapshot is not None
                    else home_path(slug))
            if home is not None and resolved_source.is_relative_to(home.resolve()):
                return home
    return None


def kb_slugs() -> set[str]:
    """kb_project slugs for units with a central or split KB slice — the units that
    must appear in the KB validators' ALLOWED_PROJECTS."""
    return {
        u.get("kb_project", s)
        for s, u in load().items()
        if u.get("kb") in ("central", "split")
    }


def _legal_crews_from_raw(raw: dict[str, Any]) -> set[str]:
    """The computation `legal_crews()` and `legal_crews_live()` share, against
    a raw registry dict each already read its own way (the live variant never
    touches `_load_raw`'s cache): `legal_crews()`'s rule — each unit's own
    `crew` field — inlined against `raw` directly, the same split
    `_legal_kb_projects_from_raw` uses for projects."""
    units = {k: v for k, v in raw.items() if not k.startswith("_")}
    return {crew for u in units.values()
            if isinstance(crew := u.get("crew"), str) and crew}


def legal_crews() -> set[str]:
    """Every unit's own `crew` field in the project registry — the set
    `knowledge.ALLOWED_CREWS` is derived from (CQ·586): a session's crew,
    never a project slug (`widget` works `widget-ops`) and never the name
    a crew goes by in prose (`northwind` is `acme-northwind`).

    Raises whatever `_load_raw()` raises on an unreadable or malformed
    registry — never an empty or partial set, the same reason
    `legal_kb_projects()` does: a validator that reads this set open against
    anything less than every legal crew would refuse a real `binds_crews`
    value rather than fail loudly.

    Reads through `_load_raw()`'s process-lifetime cache: a one-shot caller
    exits before a stale answer could matter. See `legal_crews_live()` for a
    long-running caller that cannot clear it."""
    return _legal_crews_from_raw(_load_raw())


def legal_crews_live() -> set[str]:
    """`legal_crews()`, read through `_load_raw_live()` rather than
    `_load_raw`'s process-lifetime cache.

    For `knowledge._current_allowed_crews()`, whose callers — like
    `_current_allowed_projects()`'s — are launchd daemons that live for days
    (`bin/knowledge-watcher`, `bin/mcp-kb-server`) and never call
    `_load_raw.cache_clear()`. Through the cached path, a crew registered, or
    removed, after either daemon's own first call into `projects.py` stayed
    wrong for the daemon's whole run — the same gap platform/decisions/0097
    closed for projects, now closed here too (reviewer + security-reviewer,
    PR #1065 round 1).

    Raises exactly as `legal_crews()` does: whatever the file read raises on
    an unreadable or malformed registry, never an empty or partial set."""
    return _legal_crews_from_raw(_load_raw_live())


CI1_HEARTBEAT_REPOS_KEY = "_ci1_heartbeat_repos"


def ci1_heartbeat_repos() -> list[str]:
    """`owner/repo` strings whose workflows use `runs-on: convivy-linux` —
    the list `bin/ci1-heartbeat` watches for a job queued past its
    15-minute threshold (CQ·586). A top-level registry key rather than a
    per-unit field: one of the repositories here is a CI scratch repo that
    belongs to no unit, the same reason this module's own docstring gives
    for why `_release_names` is a top-level key rather than a per-unit one.

    Raises whatever `_load_raw()` raises on an unreadable or malformed
    registry — never an empty list, so `bin/ci1-heartbeat` reports a failed
    read as an error rather than as "no repositories to watch"."""
    return list(raw_document().get(CI1_HEARTBEAT_REPOS_KEY, []))


# --- ARCH-426: projects, products, directories, crews ----------------------------


def products() -> dict[str, dict[str, Any]]:
    """The commercial products, {slug: product-dict}, in the registry's own key
    order — a display order, so a caller that needs one iterates this dict
    rather than sorting it. Separate from projects by design; a project links
    to these through its `products` list."""
    return dict(_load_raw().get(PRODUCTS_KEY, {}))


def get_product(slug: str) -> dict[str, Any] | None:
    return products().get(slug)


def product_group_for(slug: str) -> str | None:
    """The `group` `_products.<slug>` carries (the steward's ruling,
    2026-10-22), or None when absent or `slug` is unregistered. Raises
    ValueError when `group` is present and not a non-empty string."""
    p = get_product(slug)
    if p is None:
        return None
    value = p.get("group")
    if value is None:
        return None
    if not (isinstance(value, str) and value):
        raise ValueError(f"_products.{slug}.group must be a non-empty string, got {value!r}")
    return value


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


def _legal_kb_projects_from_raw(raw: dict[str, Any]) -> set[str]:
    """The computation `legal_kb_projects()` and `legal_kb_projects_live()`
    share, against a raw registry dict each already read its own way:
    `kb_slugs()`'s rule inlined against `raw` directly (not called, so the
    live variant never touches `_load_raw`'s cache), the non-project scopes,
    and `_kb_legacy`."""
    units = {k: v for k, v in raw.items() if not k.startswith("_")}
    kb_project_slugs = {u.get("kb_project", s) for s, u in units.items()
                        if u.get("kb") in ("central", "split")}
    scope_names = {k for k in raw.get(SCOPES_KEY, {}) if not k.startswith("_")}
    legacy = raw.get(KB_LEGACY_KEY, [])
    return kb_project_slugs | scope_names | {s for s in legacy if isinstance(s, str)}


def legal_kb_projects() -> set[str]:
    """Every KB project slug a validator must admit: `kb_slugs()` (the
    central/split units' own `kb_project`) union `scopes()` (the non-project
    scopes — `company`, `platform`, `commercial`, `engineering-practice`)
    union `_kb_legacy` (the registry's own top-level key) — KB project slugs that predate
    this registry and still carry documents but name no unit's own
    `kb_project` today, such as `substrate` and `knowledge`
    (platform/decisions/0097).

    THE REGISTRY, not its top-level keys, is the authority this returns —
    deliberately NOT `set(load()) | set(scopes()) | ...`. A private crew's
    `kb` is `local`, so its slug must stay out of this set even though it is
    a top-level key of the registry; `kb_slugs()` already excludes it by
    reading each unit's own `kb` field rather than its mere presence.

    Raises whatever `_load_raw()` raises on an unreadable or malformed
    registry — never an empty or partial set — because a validator that
    reads this set open against anything less than every legal project would
    refuse real writes rather than fail loudly.

    Reads through `_load_raw()`'s process-lifetime cache: a one-shot caller
    (bin/agentctl, bin/link-project-inboxes) exits before a stale answer could
    matter, and a long-running one clears that cache itself when it must see
    a change (bin/co, bin/run-register-api). See `legal_kb_projects_live()`
    for a caller that cannot clear it."""
    return _legal_kb_projects_from_raw(_load_raw())


_live_raw_cache: tuple[tuple[str, int, int], dict[str, Any]] | None = None

# `_load_raw_live()`'s Library-mode cache (PROJECTS_JSON unset): (fetched_at,
# document), keyed on `clock()` rather than a file stat since there is no
# file. See that function's docstring for why 60s.
_LIVE_LIBRARY_CACHE_TTL_S = 60.0
_live_library_cache: tuple[float, dict[str, Any]] | None = None


def _load_raw_live(*, clock=time.monotonic) -> dict[str, Any]:
    """`legal_kb_projects_live()`'s read, for a long-running caller that never
    clears `_load_raw()`'s own cache.

    `PROJECTS_JSON` set: opened and parsed fresh only when its (resolved
    path, `st_mtime_ns`, `st_size`) differs from the last call's, so a run of
    calls against an unchanged file costs one `stat()` each rather than a
    full re-read and re-parse — unchanged from before this module could read
    the Library.

    `PROJECTS_JSON` unset: the Library's answer is cached for at most
    `_LIVE_LIBRARY_CACHE_TTL_S` (60s), `clock()`-timed rather than file-keyed,
    since there is no file to stat. A crew registered with the Library is
    seen by a long-running reader within a minute, never longer, and a
    failed fetch is never cached as a success — the exception propagates
    before the cache is written, so the next call tries again rather than
    serving a stale answer past the window. `clock`, injectable for a test
    that must not sleep 60 real seconds to see the window turn over.

    `knowledge._name_refusal()` calls `_current_allowed_projects()` — and so
    this read — on every name it judges, and `knowledge.project_catalogue()`
    calls `_name_refusal()` once per directory under `root/projects`, once for
    `pending`, and once per retired/private name, so one document's validation
    already costs on the order of the registry's own project count in calls
    here; `bin/knowledge-watcher`'s startup scan and `agentctl kb sync` make
    that same call once per document scanned — the file-keyed branch keeps
    that cheap (reviewer, PR #978 round 2), and the TTL keeps the Library
    branch from costing dozens of HTTP requests per document validated."""
    global _live_raw_cache, _live_library_cache
    if PROJECTS_JSON is not None:
        resolved = str(Path(PROJECTS_JSON).resolve())
        st = os.stat(PROJECTS_JSON)
        key = (resolved, st.st_mtime_ns, st.st_size)
        if _live_raw_cache is not None and _live_raw_cache[0] == key:
            return _live_raw_cache[1]
        with open(PROJECTS_JSON) as f:
            raw = json.load(f)
        _live_raw_cache = (key, raw)
        return raw
    now = clock()
    if (_live_library_cache is not None
            and now - _live_library_cache[0] < _LIVE_LIBRARY_CACHE_TTL_S):
        return _live_library_cache[1]
    raw = _fetch_registry_from_library()
    _live_library_cache = (now, raw)
    return raw


def legal_kb_projects_live() -> set[str]:
    """`legal_kb_projects()`, read through `_load_raw_live()` rather than
    `_load_raw`'s process-lifetime cache.

    For `knowledge._current_allowed_projects()`, whose only callers today —
    `bin/knowledge-watcher` and `bin/mcp-kb-server` — are launchd daemons that
    live for days and, unlike `bin/co` and `bin/run-register-api`, never call
    `_load_raw.cache_clear()`. Through the cached path, a project registered
    after either daemon's own first call into `projects.py` stayed refused
    until the daemon restarted — the exact gap platform/decisions/0097 meant
    to close and the CI-fix commit this function replaces did not (reviewer +
    security-reviewer, PR #978 round 1). Every other caller of
    `legal_kb_projects()`/`kb_slugs()`/`scopes()` is unaffected: this function
    reads the file itself rather than changing what `_load_raw` hands back to
    them.

    Raises exactly as `legal_kb_projects()` does: whatever the file read
    raises on an unreadable or malformed registry, never an empty or partial
    set."""
    return _legal_kb_projects_from_raw(_load_raw_live())


def _declared_kb_project_names() -> set[str]:
    """Every KB project NAME declared somewhere in the registry: a central/
    split unit's own `kb_project` (`kb_slugs()`), a non-project scope
    (`scopes()` — matching what `legal_kb_projects()` already admits, the
    steward's ruling, 2026-10-22), a `kb_scope_exceptions` entry's
    `kb_project`, or a `_kb_legacy` entry. This is the set a `_kb_projects`
    key must be drawn from — a key naming none of these names no real KB
    project, so `_kb_projects` can carry no fact about it."""
    raw = _load_raw()
    legacy = raw.get(KB_LEGACY_KEY, [])
    declared = kb_slugs() | set(scopes()) | {s for s in legacy if isinstance(s, str)}
    for exc in kb_scope_exceptions().values():
        kb_project = exc.get("kb_project")
        if isinstance(kb_project, str):
            declared.add(kb_project)
    return declared


def kb_projects() -> dict[str, dict[str, Any]]:
    """The `_kb_projects` block, {KB project name: entry}, in the registry's
    own key order — a display order, the same convention `products()` keeps
    for `_products`. Absent reads as {}.

    Raises ValueError, at read time, on anything that would let a caller
    like `standalone_kb_projects()` read a silent guess rather than get a
    refusal: a key naming no KB project `_declared_kb_project_names()`
    recognises, a value that is not a JSON object, a value carrying a field
    other than `standalone`/`section`, a `standalone` present and not a JSON
    boolean, or a `section` present and not a non-empty string."""
    raw = _load_raw().get(KB_PROJECTS_KEY, {})
    declared = _declared_kb_project_names()
    for name, entry in raw.items():
        if name not in declared:
            raise ValueError(
                f"_kb_projects.{name} names no KB project any unit's "
                f"kb_project, kb_scope_exceptions entry or _kb_legacy entry "
                f"declares")
        if not isinstance(entry, dict):
            raise ValueError(f"_kb_projects.{name} must be a JSON object, got {entry!r}")
        unknown = set(entry) - set(_KB_PROJECT_FIELDS)
        if unknown:
            raise ValueError(
                f"_kb_projects.{name} has unknown field(s) {sorted(unknown)}; "
                f"only {_KB_PROJECT_FIELDS} is allowed")
        if "standalone" in entry and not isinstance(entry["standalone"], bool):
            raise ValueError(
                f"_kb_projects.{name}.standalone must be a JSON boolean, got "
                f"{entry['standalone']!r}")
        if "section" in entry and not (isinstance(entry["section"], str) and entry["section"]):
            raise ValueError(
                f"_kb_projects.{name}.section must be a non-empty string, got "
                f"{entry['section']!r}")
    return dict(raw)


def standalone_kb_projects() -> list[str]:
    """KB project names marked `standalone: true` in `_kb_projects`, in the
    map's own key order — Orient's `STANDALONE_PRODUCTS` reads this order as
    its display order, the way `products()`'s order is Orient's
    `PRODUCT_ORDER`. Absent reads as false: a KB project is grouped under
    the project(s) that build it unless marked otherwise."""
    return [name for name, entry in kb_projects().items() if entry.get("standalone") is True]


def kb_project_section_for(name: str) -> str | None:
    """The `section` `_kb_projects.<name>` carries (the steward's ruling,
    2026-10-22), or None when absent or `name` is not a declared KB project.
    Raises exactly as `kb_projects()` does on a malformed `_kb_projects`
    block."""
    return kb_projects().get(name, {}).get("section")


def _validate_rollup(owner_desc: str, value: Any) -> list[str]:
    """`value` as a `rollup` list, or [] when absent. Raises ValueError when
    `value` is not a list, holds a duplicate, or holds anything outside
    `ROLLUP_VALUES` — shared by `rollup_for()` and `product_rollup_for()` so
    the two schemas cannot drift."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{owner_desc} rollup must be a list, got {value!r}")
    if len(set(value)) != len(value):
        raise ValueError(f"{owner_desc} rollup has duplicate entries: {value!r}")
    bad = [v for v in value if v not in ROLLUP_VALUES]
    if bad:
        raise ValueError(
            f"{owner_desc} rollup has unknown value(s) {bad!r}; must be one "
            f"of {ROLLUP_VALUES}")
    return list(value)


def rollup_for(slug: str) -> list[str]:
    """Which Chart House fold(s), beyond the ordinary one, project `slug`'s
    own activity joins (`rollup`): `"repos"`, `"cost"`, both, or neither
    (absent, the common case). [] for an unregistered slug."""
    u = get(slug)
    if u is None:
        return []
    return _validate_rollup(slug, u.get("rollup"))


def product_rollup_for(product_slug: str) -> list[str]:
    """Like `rollup_for()`, for a `_products` entry rather than a project
    unit. [] for an unregistered product slug."""
    p = get_product(product_slug)
    if p is None:
        return []
    return _validate_rollup(f"_products.{product_slug}", p.get("rollup"))


def code_health_repos_for(slug: str) -> list[str]:
    """Repo names in project `slug`'s own `code_health` list (the steward's
    ruling, 2026-10-22) — the repos whose CI health that unit is responsible
    for. [] when absent or `slug` is unregistered.

    Raises ValueError when `code_health` is not a list, holds a duplicate, or
    names a repo `slug`'s own `repos` does not list — an orphaned entry would
    track a repo nothing else ties to this unit."""
    u = get(slug)
    if u is None:
        return []
    value = u.get("code_health")
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{slug} code_health must be a list, got {value!r}")
    if len(set(value)) != len(value):
        raise ValueError(f"{slug} code_health has duplicate entries: {value!r}")
    repos = u.get("repos", [])
    bad = [r for r in value if r not in repos]
    if bad:
        raise ValueError(
            f"{slug} code_health names repo(s) {bad!r} not in its own repos {repos!r}")
    return list(value)


def code_health_repos() -> list[str]:
    """`owner/repo` for every repo named in some registered, non-private,
    non-walled-off project's `code_health` list, each expanded by
    `repo_github_slug` — the repos a code-health capture (CI workflow runs,
    coverage) covers, read from the registry rather than hard-coded (the
    steward's correction, 2026-11-02, CQ·636, naming nine today: knowledge,
    agent-platform, convivy-orient, convivy-app-kit, constance, vivy,
    sous-chef, hollis, selector). Mirrors
    `pr-state-capture.repos_to_capture()`'s exclusion of both `private_slugs()`
    (ADR-0043's wall) and `walled_off_slugs()` (platform/decisions/0100) and
    its `repo_github_slug` expansion, so a `code_health` entry under a
    non-`convivy` owner resolves to its real slug too.

    `private_slugs()` and `walled_off_slugs()` are read in one `try`, the same
    fail-closed step `run-register-api` reads them in on every route: either
    raising means this returns [] rather than a public-only subset computed
    against a partial exclusion set — this surface posts to the Library,
    which serves it to Orient, so a failed read must drop every repo, not
    just the ones it can still vouch for."""
    try:
        excluded = private_slugs() | walled_off_slugs()
    except Exception:
        return []
    out = {repo_github_slug(r, unit)
           for slug, unit in load().items() if slug not in excluded
           for r in code_health_repos_for(slug)}
    return sorted(out)


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
    outside $HOME entirely is refused, and one product's large external data
    mirror on its own volume is the live precedent for wanting one. It is refused
    today because the field
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
    never seen. A live two-repo project is the case this happened on — an agent
    working in one of its repos wrote to that repo's inbox, and the project's co,
    watching the other repo's inbox, read nothing.

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
    the last null, on a now-retired umbrella unit, was reporting that one project
    held several crews, and splitting it into three resolved that."""
    u = get(slug)
    return u.get("crew") if u else None


def agent_prefixes(slug: str) -> tuple[str, ...]:
    """This unit's own per-app agent-name prefixes (`deck-`), as a tuple — empty
    when the unit has none (a crew that dispatches only bare roles, or an
    unregistered slug). The registry stores one string or a list of strings
    (a crew with a live prefix and a retired one it still has to authenticate
    old review stamps under); either form normalizes to a tuple here, so a
    caller never has to check which the registry used for this unit.

    Raises ValueError on a malformed entry — not a non-empty string ending in
    `-` — rather than silently dropping it: a typo'd prefix here would leave
    `all_agent_prefixes()` missing a crew from the derived set with no error
    anywhere, which is exactly the silent-drift failure this field replaces."""
    u = get(slug)
    if u is None:
        return ()
    raw = u.get("agent_prefix")
    if raw is None:
        return ()
    values = (raw,) if isinstance(raw, str) else tuple(raw)
    for v in values:
        if not isinstance(v, str) or not v or not v.endswith("-"):
            raise ValueError(
                f"{slug}: agent_prefix entry {v!r} must be a non-empty string "
                f"ending in '-'")
    return values


def all_agent_prefixes() -> tuple[str, ...]:
    """Every registered unit's agent prefix, deduplicated and sorted (ARCH-397
    Phase 2) — the ONE set `app_prefixes.internal_app_prefixes()` derives its
    answer from, in place of a literal bin/agentctl, review_protocol.py and
    app_prefixes.py each once kept their own copy of and had to hand-edit in
    lockstep. A crew registered with its own `agent_prefix` is covered the
    moment that edit lands, with no change to any of the three."""
    out: set[str] = set()
    for slug in load():
        out.update(agent_prefixes(slug))
    return tuple(sorted(out))


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


def bug_sweep_repos(slug: str) -> list[str]:
    """Repo names (bare, registry convention) whose open GitHub issues feed
    *slug*'s shared-coverage bug-sweep intake, sorted — `bin/bug-sweep`'s
    per-product repo list, read from the unit's own `bug_sweep_repos` field.

    Absent means the project's own single repo (`slug` itself) is the whole
    intake surface, the common case: most shared-coverage products are a single
    repo. A unit whose `repos` list carries a sandbox-tools or other sibling
    repo that also takes GitHub issues sets this explicitly rather than
    pulling in every entry of `repos` — a commercial workspace or landing-page
    repo in that list takes no GitHub issues and must not be swept."""
    u = get(slug) or {}
    explicit = u.get("bug_sweep_repos")
    return sorted(explicit) if explicit else [slug]


def shared_job_host() -> str | None:
    """The single active project flagged `shared_job_host: true`, or None when
    none is.

    A fleet-wide job dispatched on a `shared/daytime` profile (observability-
    scribe and curator in `bin/launch-weekly-curation`) has no product of its
    own, but every `co` session still needs SOME repo as its working
    directory — this is the registry's record of which one. At most one unit
    should carry the flag; `tests/test_projects.py` holds that."""
    for slug, u in load().items():
        if u.get("shared_job_host") is True:
            return slug
    return None


def business_umbrella_dirname() -> str | None:
    """The bare directory name bin/co's dedicated business-area-umbrella
    branch treats as the shared parent for every side-business project repo
    directly under it — the registry's own top-level `_business_umbrella_dir`
    key, read directly rather than from a per-unit field, since the branch's
    job is to recognise ANY repo under that one shared directory, registered
    or not (an unregistered new project there still launches, unstyled).

    None when the registry sets no such key, which is bin/co's signal that
    no umbrella directory is configured and the branch never fires — the
    same absent-means-off convention `company_workspace_slug()`/
    `shared_job_host()` already use for their own singleton branches.

    Raises ValueError when the key is set to anything other than a bare
    directory name (no path separator, no leading `.`) — a malformed value
    here is a registry bug to surface, not something to silently ignore."""
    raw = raw_document().get(BUSINESS_UMBRELLA_KEY)
    if raw is None:
        return None
    if not (isinstance(raw, str) and valid_name(raw)):
        raise ValueError(
            f"{BUSINESS_UMBRELLA_KEY} {raw!r} must be a bare directory name")
    return raw


def writing_guide_dirname() -> str | None:
    """Home-directory basename of the one active unit flagged
    `co_writing_guide: true` — bin/co's dedicated writing-guide branch
    resolves its own directory this way rather than by a name in its own
    source, the same move `company_workspace_slug()`/`shared_job_host()`
    already make for their own singleton branches. At most one unit should
    carry the flag. None when no unit does, or when the flagged unit's home
    does not resolve."""
    for slug, u in load().items():
        if u.get("lifecycle") == "active" and u.get("co_writing_guide") is True:
            home = home_path(slug)
            return home.name if home is not None else None
    return None


# --- bin/co: per-crew launch shape, read from the registry instead of named in
# --- bin/co's own source ("co takes its crew layout from the registry and names
# --- no crew") -------------------------------------------------------------------
#
# Every field below is optional, and absence is the conservative "nothing special"
# answer — a crew nobody has configured here launches exactly as the generic
# fallback already does, the same absent-means-off convention `crew_cockpit` and
# `model_tier` already use.

CO_COCKPIT_GATES = ("path", "basename")


def co_style(slug: str) -> tuple[str, str, str | None, str] | None:
    """(style_id, accent, label, fg) for bin/co's `_apply_co_style`, or None when
    the resolved unit carries no `co_accent` — bin/co then applies no styling,
    exactly as it does today for a crew nobody has picked a colour for.

    `slug` need not be the unit's own key. An exact match against an active or
    sunset unit's own slug wins first; failing that, the active unit whose
    `repos` lists `slug` — a caller holding only a raw git-toplevel basename
    (bin/co's `-commercial` branch, after stripping that suffix, and the
    generic daytime fallback's own styling case) may be looking at a secondary
    checkout of some OTHER unit's repo, the same join `fold_map`/
    `project_for_repo` make for a trusted repo name elsewhere (round 2,
    reviewer: on origin/main a bare `co` in a product's own secondary repo,
    listed only under that product's `repos` rather than its own slug or
    home-directory basename, got that product's own hardcoded colour
    directly; the registry cutover dropped the hardcoded case and fell
    through unstyled for that one name).

    EXCEPT `knowledge` AND `dev-setup`, which never resolve this way even
    though `knowledge` sits in `library`'s own `repos` (there for the
    `kb_scope_exceptions` question, which profile a standalone checkout
    reads, not for identity). bin/co already carries both as literal,
    nameless legacy workspaces with no registry unit of their own
    (`_co_generic_daytime_refresh`'s `case $product in knowledge|dev-setup)`)
    — resolving `knowledge` to `library` here reopens exactly the collision
    `_co_apply_product_style`'s own comment warns against, concretely:
    `_apply_co_style` calls `_co_set_name "$label"`, so a standalone
    `knowledge` checkout would relabel its session "Library" instead of
    "knowledge" (caught by test_co_session_name.sh's own pre-existing
    `knowledge` case).

    style_id defaults to the RESOLVED unit's own slug, not the name this
    function was called with, so a secondary-repo basename produces the same
    style_id a direct call with the unit's own slug would. label defaults to
    None, so `_apply_co_style` itself falls back to style_id — the same
    two-argument call bin/co's plainest styled branches already make. The two
    are independent: a unit whose iTerm/tmux key differs from its own slug
    (`convivy-deck`'s cockpit key is `deck`) can still want that key as its
    own label rather than a separately-spelled one."""
    u = get(slug)
    resolved = slug
    if u is None and slug not in ("knowledge", "dev-setup"):
        for cand_slug, cand in load().items():
            if cand.get("lifecycle") == "active" and slug in cand.get("repos", []):
                u, resolved = cand, cand_slug
                break
    if u is None:
        return None
    accent = u.get("co_accent")
    if not accent:
        return None
    style_id = u.get("co_style_id") or resolved
    label = u.get("co_label")
    fg = u.get("co_fg") or "#ffffff"
    return style_id, accent, label, fg


def co_style_by_basename(basename: str) -> tuple[str, str, str | None, str] | None:
    """Like `co_style`, keyed by the basename of an ACTIVE unit's home directory
    rather than its slug — for a caller holding only `$(basename "$repo_root")`
    (bin/co's umbrella-directory branch, which resolves a directory under a
    shared parent to no slug of its own)."""
    for slug, u in load().items():
        if u.get("lifecycle") != "active":
            continue
        home = home_path(slug)
        if home is not None and home.name == basename:
            return co_style(slug)
    return None


def self_contained_crew_dirs() -> list[str]:
    """Home-directory basenames of active crews flagged `co_self_contained: true`
    — a crew with its own hand-written `.claude/agents/`, dispatched off the
    shared KB index. bin/co launches these with no knowledge pull, no
    re-individuation: just its styling and the handoff-resume muscle memory every
    launch gets. Sorted, the same shape `crew_cockpit_dirs()`/`sonnet_crew_dirs()`
    already hand bin/co."""
    out = []
    for slug, u in load().items():
        if u.get("lifecycle") != "active" or u.get("co_self_contained") is not True:
            continue
        home = home_path(slug)
        if home is not None:
            out.append(home.name)
    return sorted(out)


def cockpit_gate(slug: str) -> str:
    """"path" (the default) or "basename" — how strictly bin/co's merged
    crew-cockpit branch gates launching a crew whose directory basename matches
    one of `crew_cockpit_dirs()`.

    "path" requires the resolved git toplevel to equal the registry's own
    `home_path(slug)` exactly before the branch fires at all — the safe default
    for a crew whose basename is generic enough that a stray same-named directory
    elsewhere on disk is a real risk (`library` is the canonical case: a scratch
    clone or a `git worktree add ../library` would otherwise collide). "basename"
    accepts the basename alone, an accepted risk recorded per-crew here for one
    idiosyncratic enough that the coincidence is considered unlikely — bin/co
    still re-checks the full path before the destructive `--force`
    re-individuation step either way."""
    u = get(slug) or {}
    raw = u.get("co_cockpit_gate")
    return raw if raw in CO_COCKPIT_GATES else "path"


def generic_refresh_names() -> set[str]:
    """Slug, alias and repo names of active units flagged `co_generic_refresh:
    true` — bin/co's `-r` validator for its least-restricted launch path (no
    dedicated branch, no crew-cockpit membership, no self-contained flag) accepts
    exactly these names, plus two legacy workspace names bin/co still hardcodes
    because no registry unit names them (`knowledge`, the KB repo itself, and
    `dev-setup`, the frozen build workshop)."""
    out: set[str] = set()
    for slug, u in load().items():
        if u.get("lifecycle") != "active" or u.get("co_generic_refresh") is not True:
            continue
        out.add(slug)
        out.update(u.get("aliases", []))
        out.update(u.get("repos", []))
    return out


def company_workspace_slug() -> str | None:
    """The slug of the one active unit flagged `company_workspace: true`, or
    None when no unit claims it. Both bin/co and Orient read this: bin/co's
    company-level commercial-workspace branch resolves its own directory
    this way rather than by a name in its own source, and Orient's 0.7.0
    de-naming reads it for the same unit."""
    for slug, u in load().items():
        if u.get("lifecycle") == "active" and u.get("company_workspace") is True:
            return slug
    return None


def co_default_agent(slug: str) -> str | None:
    """`slug`'s own `co_default_agent` field, or None when absent — bin/co's
    company-workspace branch falls back to its hardcoded `positioning-strategist`
    default in that case, the same absent-means-fallback convention
    `company_workspace_slug()` uses for its own singleton flag.

    Company decision: the company-workspace crew holds all four commercial
    roles (positioning-strategist, commercial-writer, product-analyst,
    product-manager); which one bin/co treats as that crew's default is a
    registry field rather than a name in bin/co's own source (platform
    decision 0100)."""
    u = get(slug) or {}
    raw = u.get("co_default_agent")
    return raw if isinstance(raw, str) and raw else None


def shared_coverage_products() -> list[str]:
    """Slugs the platform's shared jobs cover (`coverage == "shared"`): arch-review,
    bug-sweep, weekly curation, and the model scorecard's agentctl telemetry.
    Sorted. Excludes own-coverage units (Globex) and units with no coverage value.
    The cron-fired overnight drain itself was retired 2026-09-14 (autonomous work
    is event-triggered now); this reads a registry field that still records
    which products the shared jobs cover."""
    return sorted(s for s, u in load().items() if u.get("coverage") == "shared")


def own_coverage_products() -> list[str]:
    """Slugs registered as running their own crew and CI rather than being
    covered by the platform's shared jobs (`coverage == "own"`), sorted —
    harvested from GitHub only, so they carry their own per-product
    scorecard/ledger files alongside the shared ones."""
    return sorted(s for s, u in load().items() if u.get("coverage") == "own")


def own_ledger_paths() -> dict[str, Path]:
    """{slug: ledger path} for apps that write their own LLM-spend ledger
    directly on this machine rather than POSTing to the shared ingest
    endpoint — `bin/agent-cost-report`'s `APP_LEDGERS`.

    A unit opts in via `own_ledger: true`; its ledger lives at
    `<home_dir>/data/llm-spend.jsonl`, the one path every such app has used
    since ARCH-147. Absent is the common case — most apps run off-machine
    (Render) and ship no local disk to write to, so they POST to the ingest
    endpoint instead."""
    out: dict[str, Path] = {}
    for slug, u in load().items():
        if u.get("own_ledger") is True:
            home = home_path(slug)
            if home is not None:
                out[slug] = home / "data" / "llm-spend.jsonl"
    return out


def activity_capture_excluded_repos() -> set[str]:
    """Repo names (bare) excluded from every `github-activity-capture` pass
    beyond the private-project wall — sandbox-tool repos that mirror what
    Orient's own GitHub capture excludes, per the same specification
    (issue_01M4ZB5C5ZX0MV206064MTRAE0), named directly there rather than
    derived cross-repo.

    Read from each unit's own `activity_capture_excluded_repos` list, which
    moves the literal set into the registry without changing the decision to
    name it directly rather than derive it. Absent means the unit names
    none — the common case."""
    out: set[str] = set()
    for u in load().values():
        out.update(u.get("activity_capture_excluded_repos", []))
    return out


def live_checkout_readers() -> dict[str, dict[str, Any]]:
    """Every repo whose PRIMARY checkout is read live by something scheduled,
    {repo: {"reads": str, "adr": str | None, "jobs": tuple[str, ...]}} —
    `bin/check-primary-checkouts`' and `bin/check-serving-checkouts`'
    declared `_LIVE_CHECKOUTS`, read from each unit's own
    `live_checkout_readers` field rather than a second hardcoded copy.

    DECLARED, not discovered: only the deployment mechanism can say whether a
    branch is load-bearing (see those scripts' own module docstrings for
    why). Absent on a unit means none of its repos are declared live-read
    here — the conservative default, since an undeclared repo is simply not
    policed rather than assumed safe."""
    out: dict[str, dict[str, Any]] = {}
    for u in load().values():
        for repo, row in (u.get("live_checkout_readers") or {}).items():
            out[repo] = {"reads": row["reads"], "adr": row.get("adr"),
                         "jobs": tuple(row.get("jobs", ()))}
    return out


def primary_checkout_autoupdate_repos() -> list[str]:
    """Every repo the hourly primary-checkout fast-forward job
    (`bin/primary-checkout-autoupdate`) pulls: a unit's own
    `primary_checkout_autoupdate_repos` list field, flattened and sorted.

    Declared, not discovered — same rationale as `live_checkout_readers`:
    only a unit's own owner can say its checkout is safe to fast-forward
    unattended. Absent on every unit means nothing opts in, which is the
    conservative default; today exactly one repo does."""
    out: list[str] = []
    for u in load().values():
        out.extend(u.get("primary_checkout_autoupdate_repos") or [])
    return sorted(set(out))


def kit_repo() -> str | None:
    """The repo name of the shared app-kit library new lab apps depend on
    for their `think()` seam: the `kit_repo` field on the one unit that owns
    it, or None when no unit declares it.

    A single named value rather than a list — unlike
    `primary_checkout_autoupdate_repos`, which several units could in
    principle opt into, exactly one unit owns the kit library at all."""
    for u in load().values():
        name = u.get("kit_repo")
        if isinstance(name, str) and name:
            return name
    return None


def tool_callers(tool: str) -> list[str]:
    """Repo names whose own `scripts/` call shared bin/ tool *tool*, sorted —
    `bin/check-cli-contracts`'s declared caller map, read from each unit's own
    `calls_shared_tools` ({repo: [tool, ...]}).

    Declared, not discovered, same as the map this replaces: a repo is listed
    here because someone audited its `scripts/` directory, not because the
    registry can see into another checkout. Absent means no repo in that unit
    calls any shared tool — the CONSERVATIVE default for this check, since an
    unlisted repo is simply not checked rather than assumed clean."""
    out: list[str] = []
    for u in load().values():
        for repo, tools in (u.get("calls_shared_tools") or {}).items():
            if tool in tools:
                out.append(repo)
    return sorted(out)


def profiled_dirs() -> list[Path]:
    """Every active unit's `profiled_dirs` entries, resolved and flattened —
    bin/co's "profiled checkouts that never rendered" branch (floor slice 5): a
    directory that resolves a `daytime` KB profile but is not any unit's own
    `home_dir` (a satellite repo listed under the project's own `repos` but
    checked out on its own, outside that project's home_dir). Each entry is
    refused exactly as `_expand_own_home` refuses a `home_dir`/`dirs` entry
    outside this user's home."""
    out: list[Path] = []
    for slug, u in load().items():
        if u.get("lifecycle") != "active":
            continue
        for raw in u.get("profiled_dirs", []):
            out.append(_expand_own_home(slug, "profiled_dirs entry", raw))
    return out


def velocity_buckets() -> dict[str, list[str]]:
    """display → repos, the table `bin/velocity-report`'s BUCKETS hand-maintained
    before this read it live. Every non-private unit appears once under its own
    `display`, carrying its `repos` list unchanged; a BLACK BOX (`private: true`)
    is excluded outright, matching `tests/test_projects.py`'s
    `test_repos_match_velocity_buckets`.

    Ordered by each unit's own `velocity_order` (an int; lower sorts first) — the
    dashboard's column order, the one fact this table needed that the registry
    carried no field for before this accessor existed. A unit with no
    `velocity_order` sorts after every unit that has one, by `display`: the
    conservative placement for a newly registered unit nobody has given a
    dashboard position yet, rather than a guess at where it belongs."""
    private = private_slugs()
    rows = [
        (u.get("velocity_order"), u["display"], list(u.get("repos", [])))
        for slug, u in load().items()
        if slug not in private
    ]
    rows.sort(key=lambda r: (r[0] is None, r[0] if r[0] is not None else 0, r[1]))
    return {display: repos for _, display, repos in rows}


def kb_project_bucket_map() -> dict[str, str]:
    """KB/ADR project slug → unit display, the fold `bin/velocity-report`'s
    ADR_PROJECT_BUCKET hand-maintained before this read it live: which velocity
    bucket an ADR filed under `knowledge/projects/<slug>/decisions/` counts
    toward.

    Reads each non-private unit's `adr_kb_projects`, a list of KB slugs this
    unit's ADRs are attributed under. Usually just the unit's own `kb_project`,
    but the field is independent of it on purpose, to cover what `kb_project`
    alone cannot express: a slug that predates the unit's current `kb_project`
    and names no unit's own `kb_project` today (`substrate` → Acme Ops,
    `knowledge` → Acme Library); a slug several units once shared where only one now
    claims the ADRs (`widget-studio` → Widget Arch Tools alone, not Eastgate Circle
    or Parkview Villa); and a shared catch-all slug (`platform`) that must stay
    attributed to no single unit, so no unit lists it here. A unit naming none
    of its ADR slugs contributes nothing — the conservative default, since an
    unlisted slug falls through to velocity-report's Engineering-internal
    fallback rather than being guessed into a bucket that never asked for it."""
    private = private_slugs()
    out: dict[str, str] = {}
    for slug, u in load().items():
        if slug in private:
            continue
        for kb_slug in u.get("adr_kb_projects", []):
            out[kb_slug] = u["display"]
    return out
