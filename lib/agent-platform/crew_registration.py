"""Register one crew in the project registry (config/projects.json) — the
site a new crew keeps missing (bin/co's own comment calls it "THE SITE NEW
CREWS KEEP MISSING"), so the `new-crew` skill can file work items for a crew
it just created with no hand edit of this file.

issue_01M3YN6857H5J541G73Y9P3KA1, part of E19 (crew creation has one path
end to end). THE DEFECT this closes: Shipwright's single-account rehearsal
created a crew — a directory, a tmux session, a bead — with no row here, and
the work-queue tools refused to file its epics because they resolve
`product` through this file (the Library's own `projects.py` reads
`agent-platform/config/projects.json` directly; see this module's
`default_registry_path`).

ONE ROW, WRITTEN ATOMICALLY, REFUSING A CLASH. `register()` never partially
writes: on any refusal the registry file is untouched, and a successful
write is a temp file written in the same directory and renamed over the
target, so a reader never observes a half-written file. A clash is checked
the way the registry's own resolution checks it — case-insensitively, against
every existing slug, alias, repo and non-project scope at once
(`claimed_names`), the same membership `projects.project_for_repo` and the
Library's `projects._resolution_map` test.

WHAT THIS DOES NOT DO, and why each is left to a different change or a
different crew, is in this change's PR body rather than here: the two
dispatch-prefix tuples in bin/agentctl and lib/agent-platform/review_protocol.py
(a crew's `agent_prefix`, once recorded, still needs a hand edit there), and
lib/knowledge/knowledge.py's `ALLOWED_PROJECTS`/`ID_PATTERN` baseline (a new
crew's `knowledge/projects/<slug>/` directory, once it exists, admits the
crew through `project_catalogue`'s tree-read fallback with no edit to that
baseline at all).
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import projects

# The stored form every `home_dir`/`dirs` entry takes (projects.py's
# `_ROOT_PREFIX`, not imported directly since it is that module's own
# internal spelling of the same literal — duplicating the four characters
# here is cheaper than coupling this module to a private name).
_ROOT_PREFIX = "<root>/"

# A registry slug: lowercase, letters/digits/hyphens, leading letter. Looser
# than knowledge.py's KB-project slug rule (`^[a-z][a-z-]*$`, no digits) on
# purpose — this is the Builder's own project slug, not necessarily its
# `kb_project`, and the two are already allowed to differ (several rows in
# `EXPLICIT_PROJECT_BY_NAME` key a repo onto a narrower `kb_project` than its
# project slug carries). A slug meant to also be its own `kb_project` should
# still avoid digits, to clear that stricter rule when it reaches the KB.
_SLUG_OK = re.compile(r"^[a-z][a-z0-9-]*$")

_VALID_OVERNIGHT = (True, False, "own-drain")


class RegistrationRefused(ValueError):
    """Why a row was not written. The registry file named in the attempt is
    untouched — this is raised before anything is opened for writing."""


@dataclass(frozen=True)
class CrewSpec:
    """What the caller asked to register, before `build_row` turns it into a
    registry row and validates it. Every optional field left at its default
    is OMITTED from the written row (never written as a redundant `false` or
    `null`), matching the registry's own absent-means-the-conservative-answer
    convention (projects.py's REQUIRED_FIELDS comment) — with two
    deliberate exceptions, `task_text_public` and `overnight`, which every
    existing row in config/projects.json writes explicitly even when false,
    so this module follows that convention rather than the code's own
    permissive one.
    """

    slug: str
    display: str
    repos: list[str]
    crew: str | None = None
    owner: str = "convivy"
    created_by: str | None = None
    cls: str = "project"
    kb: str = "central"
    kb_project: str | None = None
    aliases: list[str] = field(default_factory=list)
    products: list[str] = field(default_factory=list)
    home_dir: str | None = None
    dirs: list[str] = field(default_factory=list)
    lifecycle: str = "active"
    model_tier: str | None = None
    task_text_public: bool = False
    overnight: Any = None
    private: bool = False
    data_local: bool = False
    agent_prefix: str | None = None
    # None = auto: true iff kb == "central" (bin/co's crew-cockpit need is a
    # function of being individuated from the central KB, not a choice the
    # caller usually has an opinion about — see crew_cockpit_dirs's
    # docstring). An explicit True/False overrides the default either way,
    # for the crew that will get its own dedicated bin/co branch later.
    crew_cockpit: bool | None = None
    note: str | None = None


def default_registry_path() -> Path:
    """Where a crew is registered when no explicit path is given: calls
    `projects._registry_path()` itself — the live three-step resolver
    (`$PROJECTS_JSON`, then the module's own tree, then the operator's
    checkout) — rather than reading `projects.PROJECTS_JSON`, which is only
    that resolver's result cached at whichever moment `projects` first
    imported in this process. The two agree in the ordinary case (one
    process, one import), but calling the resolver directly is what makes
    this command honour a `PROJECTS_JSON` set after that first import too —
    the shape a test process or a long-running wrapper can hit, and the
    shared contract an adopter install's `PROJECTS_JSON` and the Library's
    own resolution (platform/decisions, 2026-10-02) both depend on landing on
    the same file this command does. Covers both shapes this repo deploys
    in: the symlink farm, where `projects.py` follows the link back to the
    checkout actually running; and a copy deploy that carries its own
    `config/`, which an adopter's packaged install preserves across updates
    as that machine's own state."""
    return projects._registry_path()


def resolved_registry_path(registry_path: Path | str | None = None) -> Path:
    """The ABSOLUTE registry file a call to `register()` acts on:
    *registry_path* if given, else `default_registry_path()`. Resolved so an
    operator reading the command's own success or refusal output sees the
    real file — not a relative spelling that only means something relative
    to whatever directory the command happened to run from, and not a
    symlinked form that reads as a different path than the one `register()`
    actually opened.

    A caller that needs to report which file was (or would have been)
    touched before `register()` itself can return it — a refusal raised
    inside `build_row()`, which never receives a path at all — calls this
    directly rather than guessing `register()`'s internal resolution over
    again.
    """
    path = Path(registry_path) if registry_path is not None else default_registry_path()
    return path.resolve()


def load_registry(path: Path) -> dict[str, Any]:
    """The registry at `path`, parsed. Refuses — never raises a bare
    exception a caller has to know to catch — on a missing or malformed
    file, since a command run against the wrong path should say so rather
    than traceback."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError as exc:
        raise RegistrationRefused(
            f"no registry at {path} — every real install ships one "
            f"(config/projects.seed.json is the template this repo carries "
            f"for a fresh one); this command will not invent one from "
            f"nothing") from exc
    except json.JSONDecodeError as exc:
        raise RegistrationRefused(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RegistrationRefused(f"{path} must hold a JSON object at its top level")
    return data


def claimed_names(data: dict[str, Any]) -> dict[str, str]:
    """Every name already spoken for in this registry, lowercased, mapped to
    what claims it — a project slug or a non-project scope — so a refusal
    can name the clash rather than just report one exists.

    The same membership `projects.project_for_repo` tests (a project's own
    slug, its `aliases` and its `repos`) UNIONED with every `_scopes` key,
    the way the Library's `projects._resolution_map` builds its table:
    ARCH-426 makes the vocabulary uniform across projects and scopes, so a
    new slug must not shadow either one.
    """
    claimed: dict[str, str] = {}
    scopes = data.get("_scopes", {})
    if isinstance(scopes, dict):
        for name in scopes:
            if isinstance(name, str) and name.strip() and not name.startswith("_"):
                claimed.setdefault(name.strip().lower(), f"the {name!r} scope")
    for slug, unit in data.items():
        if not isinstance(slug, str) or slug.startswith("_") or not slug.strip():
            continue
        names = [slug]
        if isinstance(unit, dict):
            for key in ("aliases", "repos"):
                value = unit.get(key)
                if isinstance(value, list):
                    names.extend(value)
        for name in names:
            if isinstance(name, str) and name.strip():
                claimed.setdefault(name.strip().lower(), f"project {slug!r}")
    return claimed


def build_row(spec: CrewSpec) -> dict[str, Any]:
    """`spec` as a registry row, or raises `RegistrationRefused` on anything
    that would fail `tests/test_projects.py::test_schema_wellformed` (or a
    narrower rule this module itself enforces, such as a slug shape or a
    kb/kb_project mismatch) before a single byte is written.
    """
    if not _SLUG_OK.match(spec.slug):
        raise RegistrationRefused(
            f"{spec.slug!r} is not a valid slug — expected a lowercase "
            f"letter followed by lowercase letters, digits or hyphens")
    if not isinstance(spec.display, str) or not spec.display.strip():
        raise RegistrationRefused("a non-empty --display name is required")
    repos = [r for r in spec.repos if isinstance(r, str) and r.strip()]
    if not repos:
        raise RegistrationRefused("at least one --repo is required")
    if spec.cls not in projects.CLASSES:
        raise RegistrationRefused(f"class must be one of {projects.CLASSES}, got {spec.cls!r}")
    if spec.kb not in projects.KB_VALUES:
        raise RegistrationRefused(f"kb must be one of {projects.KB_VALUES}, got {spec.kb!r}")
    if spec.lifecycle not in projects.LIFECYCLES:
        raise RegistrationRefused(
            f"lifecycle must be one of {projects.LIFECYCLES}, got {spec.lifecycle!r}")
    if spec.model_tier is not None and spec.model_tier not in projects.MODEL_TIERS:
        raise RegistrationRefused(
            f"model_tier must be one of {projects.MODEL_TIERS}, got {spec.model_tier!r}")
    if spec.overnight not in (None, *_VALID_OVERNIGHT):
        raise RegistrationRefused(
            f"overnight must be true, false or 'own-drain', got {spec.overnight!r}")
    if spec.created_by is not None and not projects.valid_person_id(spec.created_by):
        raise RegistrationRefused(
            f"created_by must be a Library person id (person_<ULID>), got "
            f"{spec.created_by!r}")

    kb_project = spec.kb_project
    if spec.kb == "local":
        if kb_project:
            raise RegistrationRefused("kb_project must not be set when kb='local'")
    else:
        kb_project = kb_project or spec.slug

    crew = spec.slug if spec.crew is None else spec.crew
    home_dir = spec.home_dir or f"{_ROOT_PREFIX}{spec.slug}"
    crew_cockpit = spec.crew_cockpit if spec.crew_cockpit is not None else (spec.kb == "central")

    row: dict[str, Any] = {
        "class": spec.cls,
        "display": spec.display,
        "owner": spec.owner,
    }
    if spec.created_by:
        row["created_by"] = spec.created_by
    row.update({
        "home_dir": home_dir,
        "dirs": list(spec.dirs),
        "repos": repos,
        "aliases": list(spec.aliases),
        "crew": crew,
        "lifecycle": spec.lifecycle,
        "products": list(spec.products),
        "kb": spec.kb,
    })
    if kb_project:
        row["kb_project"] = kb_project
    # Written explicitly even when false — see CrewSpec's docstring on why
    # these two break from the rest of this row's omit-the-default shape.
    row["task_text_public"] = bool(spec.task_text_public)
    row["overnight"] = False if spec.overnight is None else spec.overnight
    if spec.data_local:
        row["data_local"] = True
    if spec.model_tier:
        row["model_tier"] = spec.model_tier
    if spec.private:
        row["private"] = True
    if spec.agent_prefix:
        row["agent_prefix"] = spec.agent_prefix
    if crew_cockpit:
        row["crew_cockpit"] = True
    if spec.note:
        row["note"] = spec.note

    # ADR-0043: a BLACK BOX crew must come out of this function consistent,
    # not merely spelled `private: true` while another field still leaks it
    # — the exact contradiction `projects.privacy_invariant_errors` and
    # `tests/test_projects.py::test_a_private_crew_cannot_also_be_centrally_
    # indexed` both check for, shared so the two cannot drift. Refused
    # rather than defaulted: `--kb` already has its own default (`central`),
    # so a silent switch to `local` under `--private` would make the row
    # depend on whether `--kb` was typed at all, not on what it said.
    violations = projects.privacy_invariant_errors(row)
    if violations:
        raise RegistrationRefused(
            f"{spec.slug!r} is --private, which requires kb='local', "
            f"task_text_public=false and overnight=false (ADR-0043 — a "
            f"black box crew must not be centrally indexed, publish task "
            f"text or join the shared overnight drain), but {'; '.join(violations)}. "
            f"Pass --kb local, drop --task-text-public, and leave --overnight "
            f"unset or 'false' to clear whichever of these this row violates.")
    return row


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    """Write `data` as the registry's own JSON style (2-space indent, real
    UTF-8 rather than `\\uXXXX` escapes — the two together reproduce the
    checked-in file byte-for-byte when nothing changed) to a temp file in
    `path`'s own directory, then `os.replace` it over `path`. The rename is
    atomic on the same filesystem, which a same-directory temp file
    guarantees; a reader of `path` therefore never observes a partial write.
    """
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=".projects.json.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def register(spec: CrewSpec, *, registry_path: Path | str | None = None,
             dry_run: bool = False) -> tuple[Path, dict[str, Any]]:
    """Add `spec`'s row to the registry at `registry_path` (default
    `default_registry_path()`), returning `(path, row)`.

    REFUSES RATHER THAN WRITES on any of: a malformed spec (`build_row`'s
    checks), a slug/alias/repo already claimed by an existing project or
    scope (`claimed_names`), or a `--product` naming no entry in the
    registry's own `_products` block. Every refusal is raised BEFORE the
    registry is opened for writing, so the file is byte-for-byte unchanged
    on any of them — including the dry-run path, which never writes at all.

    The returned path is `resolved_registry_path(registry_path)` — absolute —
    so a caller printing it back to an operator never has to resolve it
    again itself.
    """
    path = resolved_registry_path(registry_path)
    data = load_registry(path)
    row = build_row(spec)

    claimed = claimed_names(data)
    for name in (spec.slug, *row["aliases"], *row["repos"]):
        key = name.strip().lower()
        if key in claimed:
            raise RegistrationRefused(
                f"{name!r} is already claimed by {claimed[key]}; pick a "
                f"different slug, alias or repo name")

    products_block = data.get("_products", {})
    unknown_products = [p for p in row["products"]
                        if not (isinstance(products_block, dict) and p in products_block)]
    if unknown_products:
        raise RegistrationRefused(
            f"product(s) {unknown_products} are not in the registry's "
            f"_products block")

    if dry_run:
        return path, row

    data[spec.slug] = row
    _atomic_write(path, data)
    return path, row
