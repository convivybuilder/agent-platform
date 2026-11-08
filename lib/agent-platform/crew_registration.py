"""Register one crew with the Library — the site a new crew keeps missing
(bin/co's own comment calls it "THE SITE NEW CREWS KEEP MISSING"), so the
`new-crew` skill can file work items for a crew it just created with no hand
edit of a registry file.

issue_01M3YN6857H5J541G73Y9P3KA1, part of E19 (crew creation has one path
end to end). THE DEFECT this closes: Shipwright's single-account rehearsal
created a crew — a directory, a tmux session, a bead — with no row in the
registry, and the work-queue tools refused to file its epics because they
resolve `product` through it.

CQ·564 (the 2026-10-25 amendment to platform/decisions/0098) moved the
registry itself onto the Library: `register()` sends one row to the
Library's `POST /registry/crews` (CQ·242, library#334) and writes NO FILE
anywhere, on any outcome. The outcome is the Library's answer, never a
guess — see `register()`'s own docstring.

Every check that used to read the shared registry file to decide a
collision, an unknown product, a bad pre-existing value, or a schema-
breaking write — this module's own `claimed_names`, `_kb_project_syntax_
errors`, and a whole-document `projects.validate_registry` call — is now
the LIBRARY's job: library#334 ported `build_row` and those same checks
server-side, and reports a clash or an invalid document back as a 400 or a
409. This module keeps only the checks `build_row` can make from the spec
alone, with no registry to read.

WHAT THIS DOES NOT DO, and why it is left to a different change or a
different crew, is in this change's PR body rather than here:
lib/knowledge/knowledge.py's `ALLOWED_PROJECTS`/`ID_PATTERN` baseline (a new
crew's `knowledge/projects/<slug>/` directory, once it exists, admits the
crew through `project_catalogue`'s tree-read fallback with no edit to that
baseline at all). A crew's `agent_prefix`, once recorded here, needs no
further edit: app_prefixes.py derives bin/agentctl's and
lib/agent-platform/review_protocol.py's shared prefix tuple straight from the
registry (ARCH-397 Phase 2).
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import github_app as GA
import projects
import vault_plane

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

# knowledge.py's own KB-project slug rule (`_PROJECT_SLUG`, lib/knowledge/knowledge.py),
# duplicated rather than imported — that name is a private attribute of a sibling
# module, and the pattern is a one-line literal neither module's docstring expects
# the other to change without updating this copy. A `kb_project` this strict rule
# refuses would otherwise reach `knowledge.id_pattern_for`'s regex alternation
# unchecked: `crew_registration.build_row` validated `slug` but not `kb_project`,
# so `agentctl crew register --kb-project <value>` could write a value — an
# unbalanced paren, say — that made `re.compile()` raise at knowledge.py's
# import, taking down agentctl, the watcher, the KB server, and kb apply and kb
# sync (security-reviewer, PR #978 round 2). Checked here, before a byte reaches
# the registry, rather than only where `id_pattern_for` builds the pattern: a bad
# value refused at registration never gets the chance to break every later import.
_KB_PROJECT_SLUG_OK = re.compile(r"^[a-z][a-z-]*$")

# A GitHub account (user or org) login: letters, digits and single hyphens,
# never a leading or trailing hyphen — GitHub's own login-name rule. Checked
# here, before any request is made, since an unchecked `--github-owner`
# would otherwise reach every GitHub URL this crew's reads build (the
# `.github/workflows` read included) as an unvalidated path segment.
_GITHUB_OWNER_OK = re.compile(r"^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$")
_GITHUB_OWNER_MAX_LEN = 39


class RegistrationRefused(Exception):
    """Why `spec` was not registered: nothing is written on any of these.

    Two different sources end up here, and the message says which: a
    REFUSAL — `build_row`'s own pure checks (a bad slug, an inconsistent
    `--private` row, …), or the Library's 400/409 (a collision, an unknown
    product, a document the new row would leave schema-invalid — all now
    checked server-side, library#334) — names the field or value. A FAILURE
    — the Library unreachable, a timeout, 401, 403, 503, any other status,
    or no readable writer-token file — names the status or the path. Either
    way the caller's contract is the same: nothing was registered, and this
    is why."""


@dataclass(frozen=True)
class CrewSpec:
    """What the caller asked to register, before `build_row` turns it into a
    registry row and validates it. Every optional field left at its default
    is OMITTED from the written row (never written as a redundant `false` or
    `null`), matching the registry's own absent-means-the-conservative-answer
    convention (projects.py's REQUIRED_FIELDS comment) — with one
    deliberate exception, `task_text_public`, which every existing row in
    the registry writes explicitly even when false, so this module follows
    that convention rather than the code's own permissive one.

    CARRIES NO `ci_workflows`. That field is filled from a live GitHub read
    at registration time (comment 5947 on CQ·564) — `register()` computes
    it for every one of `repos`, and a caller cannot override it here.
    """

    slug: str
    display: str
    repos: list[str]
    crew: str | None = None
    owner: str = "convivy"
    # The GitHub account owning this crew's repo(s), when it is not convivy
    # (`projects.repo_github_slug`'s own fallback). `None` omits the field,
    # matching every other optional field's default-is-absent convention.
    github_owner: str | None = None
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


def build_row(spec: CrewSpec) -> dict[str, Any]:
    """`spec` as a registry row, with no `ci_workflows` key yet (`register`
    fills that from a live GitHub read) — or raises `RegistrationRefused` on
    anything `build_row` itself can tell is wrong with no registry to read:
    a malformed field, or a narrower rule this module enforces (a slug
    shape, a kb/kb_project mismatch, ADR-0043's private-crew invariant).
    Every check that needs the EXISTING registry to answer (a clashing
    slug/alias/repo, an unknown product, a document this row would leave
    schema-invalid) is the Library's job now — see the module docstring.
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
    if spec.created_by is not None and not projects.valid_person_id(spec.created_by):
        raise RegistrationRefused(
            f"created_by must be a Library person id (person_<ULID>), got "
            f"{spec.created_by!r}")
    # Checked before any GitHub or Library request — every GitHub read
    # `register` makes builds its slug from this value unvalidated
    # otherwise (`_repo_ci_workflows` via `projects.repo_github_slug`).
    if spec.github_owner is not None and (
            not isinstance(spec.github_owner, str)
            or len(spec.github_owner) > _GITHUB_OWNER_MAX_LEN
            or not _GITHUB_OWNER_OK.fullmatch(spec.github_owner)):
        raise RegistrationRefused(
            f"github_owner must be a GitHub account name — letters, digits "
            f"and single hyphens, at most {_GITHUB_OWNER_MAX_LEN} "
            f"characters, no leading or trailing hyphen — got "
            f"{spec.github_owner!r}")
    # Same shape `projects.agent_prefixes()` enforces on read, checked here on
    # write: a bad value refuses before the row is ever written, rather than
    # landing in the live registry and crashing every later prefix read
    # (app_prefixes.internal_app_prefixes() degrades that case to `()`, but
    # only once it exists — the write that would cause it is this one).
    if spec.agent_prefix is not None and (
            not isinstance(spec.agent_prefix, str) or not spec.agent_prefix
            or not spec.agent_prefix.endswith("-")):
        raise RegistrationRefused(
            f"agent_prefix must be a non-empty string ending in '-', got "
            f"{spec.agent_prefix!r}")

    kb_project = spec.kb_project
    if spec.kb == "local":
        if kb_project:
            raise RegistrationRefused("kb_project must not be set when kb='local'")
    else:
        kb_project = kb_project or spec.slug
    if kb_project and not _KB_PROJECT_SLUG_OK.fullmatch(kb_project):
        raise RegistrationRefused(
            f"kb_project {kb_project!r} is not a valid KB project slug — "
            f"expected a lowercase letter followed by lowercase letters or "
            f"hyphens (knowledge.py's `_PROJECT_SLUG` rule), no digits")

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
    if spec.github_owner:
        row["github_owner"] = spec.github_owner
    # Written explicitly even when false — see CrewSpec's docstring on why
    # this breaks from the rest of this row's omit-the-default shape.
    row["task_text_public"] = bool(spec.task_text_public)
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
            f"{spec.slug!r} is --private, which requires kb='local' and "
            f"task_text_public=false (ADR-0043 — a black box crew must not "
            f"be centrally indexed or publish task text), but "
            f"{'; '.join(violations)}. Pass --kb local and drop "
            f"--task-text-public to clear whichever of these this row violates.")
    return row


# --- filling ci_workflows from a live GitHub read (comment 5947 on CQ·564) --
#
# Several crews were registered with an empty ci_workflows entry though
# each repo had a ci.yml, so their pull requests were never CI-captured and
# merge admission had to stay off (CQ·569 corrected those rows). The fix is
# to stop guessing: every registration fills `ci_workflows` from what
# GitHub actually lists in each repo's `.github/workflows`, so the field a
# caller cannot even see can no longer go stale the moment it is written.
#
# THE SAME CREDENTIAL agent-platform ALREADY USES FOR A REPO-OWNER
# INSTALLATION TOKEN: the install's own GitHub App, whose JWT/installation-token
# plumbing `github_app.py` already shares, scoped per repo through `GA.mint_token`
# exactly as `bin/agentctl`'s own `_mint_run_github_token` and
# `bin/ci-result-capture`/`bin/pr-state-capture`/`bin/ci1-heartbeat` already mint
# it — all resolved through `GA.installed_app()`, never a literal, not a second
# credential path.

# A workflow file GitHub Actions recognises: directly in `.github/workflows/`
# (a subdirectory is never a workflow) with a `.yml`/`.yaml` extension —
# GitHub's own rule for what it treats as a workflow, applied here so a
# stray non-workflow file (a README, say) in that directory is never written
# into the row.
_WORKFLOW_FILE_SUFFIXES = (".yml", ".yaml")


def _repo_ci_workflows(repo: str, row: dict[str, Any], *,
                       opener=urllib.request.urlopen) -> list[str]:
    """The workflow file names GitHub lists in `repo`'s `.github/workflows`
    directory, sorted. `[]` on a 404 from GitHub's contents API — which
    reads identically for "no such directory" and "no such repo", so both
    come back as "no workflows" here, the same conflation `projects.
    ci_workflows`'s own default already treats as the safe answer for an
    absent field. Raises `RegistrationRefused`, naming `repo`, on every
    other way this can fail: the App key, the installation lookup, the
    token mint, or a GitHub status other than 200 or 404.

    `opener` defaults to the real `urllib.request.urlopen` and threads
    through to `GA.mint_token`/`GA.request` exactly as their own `opener`
    parameters do — a test passes a fake opener (`tests/test_github_mint.py`'s
    own pattern) rather than reaching the real GitHub API.
    """
    slug = projects.repo_github_slug(repo, unit=row)
    try:
        app = GA.installed_app()
        with GA.mint_token(slug, "read", key_path=app.key_path,
                            app_id=app.app_id,
                            label="the install's GitHub App", opener=opener) as token:
            code, payload = GA.request(
                "GET", f"{GA.API}/repos/{slug}/contents/.github/workflows", token, None,
                opener=opener)
    except GA.Unreadable as e:
        raise RegistrationRefused(
            f"{repo!r}'s .github/workflows could not be read: {e}") from e
    if code == 404:
        return []
    if code != 200 or not isinstance(payload, list):
        raise RegistrationRefused(
            f"{repo!r}'s .github/workflows could not be read (HTTP {code})")
    return sorted(
        item["name"] for item in payload
        if isinstance(item, dict) and item.get("type") == "file"
        and isinstance(item.get("name"), str)
        and item["name"].endswith(_WORKFLOW_FILE_SUFFIXES))


# --- sending the row to the Library (CQ·564) ---------------------------------

# The Library's own route for one crew row (CQ·242, library#334). The
# written §6.8 contract subsection is not published yet (the Library's own
# outcome note on that item); this is built to the behaviour it describes.
_REGISTRY_CREWS_ROUTE = "/registry/crews"

# The ONE capability this credential holds, per the contract: `registry.write`
# alone — it cannot read `GET /registry` or `GET /roster` (both answer 403).
# Never the reader token `projects.py` sends on `GET /registry`: that one
# lacks `registry.write` and the Library would refuse it the same way, the
# other direction.
_REGISTRY_WRITER_TOKEN_RELATIVE = Path(".orient-control") / "agent-platform-registry-writer-token"

# Loopback-equivalent; mirrors `projects._REGISTRY_TIMEOUT_S` — a slow or
# hung Library must not hold a registration open forever.
_REGISTRY_TIMEOUT_S = 5


def _registry_writer_token_file() -> Path:
    """Where the registry-writer credential lives, resolved from
    `Path.home()` fresh on every call — mirrors `projects._registry_token_
    file()`'s own resolution of the reader token, so a test can point
    `Path.home()` at its own directory and see this resolve under it. Never
    opened, printed or copied outside `_registry_writer_token` below."""
    return Path.home() / _REGISTRY_WRITER_TOKEN_RELATIVE


def _registry_writer_token() -> str | None:
    """The registry-writer credential from `_registry_writer_token_file()`,
    or `None` when it could not be read. Unlike `projects._registry_token`,
    there is no environment-variable override — the writer credential has
    exactly one source, the file."""
    try:
        return _registry_writer_token_file().read_text().strip() or None
    except OSError:
        return None


def _post_crew_row(body: dict[str, Any], *,
                   opener=urllib.request.urlopen) -> dict[str, Any]:
    """POST `body` to the Library's `POST /registry/crews` and return the
    stored row (the Library's 201 body, which adds `slug`, `updated_at` and
    `updated_by`). Raises `RegistrationRefused` on every other outcome: no
    token; 400 or 409 (named with the Library's own `error`, and `hint` when
    it gives one); any other status; or a request that could not even be
    made (unreachable, a timeout, …) — never read as success.

    `opener` defaults to the real `urllib.request.urlopen`; a test passes a
    fake one (`tests/test_github_mint.py`'s own pattern, which
    `GA.request` already relies on) rather than reaching a real socket —
    handling BOTH shapes a fake can answer with: an `HTTPError` raised the
    way the real `urlopen` raises one for a non-2xx status, or a response
    object simply carrying that status with no exception at all, the way
    `GA.request`'s own fakes already do."""
    slug = body.get("slug")
    token = _registry_writer_token()
    if not token:
        raise RegistrationRefused(
            f"no registry-writer token: {_registry_writer_token_file()} "
            f"could not be read; {slug!r} was not registered")
    req = urllib.request.Request(
        vault_plane.library_url(_REGISTRY_CREWS_ROUTE), method="POST",
        data=json.dumps(body).encode("utf-8"),
        headers=vault_plane.library_headers(token=token, json_body=True))
    try:
        with opener(req, timeout=_REGISTRY_TIMEOUT_S) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except Exception as e:
        raise RegistrationRefused(
            f"the Library could not be asked to register {slug!r}: "
            f"{e.__class__.__name__}: {e}") from e
    try:
        payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw or "{}") \
            if raw else {}
    except ValueError:
        payload = {}
    if status == 201:
        return payload
    message = payload.get("error") or f"HTTP {status}"
    hint = f" ({payload['hint']})" if payload.get("hint") else ""
    verb = "refused" if status in (400, 409) else "failed to"
    raise RegistrationRefused(
        f"the Library {verb} register {slug!r} (HTTP {status}): {message}{hint}")


def register(spec: CrewSpec, *, dry_run: bool = False,
             opener=urllib.request.urlopen) -> dict[str, Any]:
    """Register `spec` with the Library's `POST /registry/crews` (CQ·564).
    Returns the stored row on success. WRITES NO FILE, ANYWHERE, ON ANY
    OUTCOME.

    `build_row(spec)` runs first and raises `RegistrationRefused` on its own
    pure checks, with no registry to read. The row's `ci_workflows` is then
    filled from a live GitHub read, one call per repo in `spec.repos`
    (`_repo_ci_workflows`) — also before anything is sent, and also a
    `RegistrationRefused` on a repo whose workflows cannot be read.

    THE OUTCOME IS THE LIBRARY'S ANSWER, NEVER A GUESS. A 201 is registered.
    A 400 or a 409 is a REFUSAL, naming the field or value the Library's own
    `error` names. An unreachable Library, a timeout, 401, 403, 503, or any
    other status is a FAILURE, named by its status — never read as success.
    A missing or unreadable writer-token file fails the same way, naming the
    path, before any request is made. All three raise `RegistrationRefused`;
    see that class's docstring for how to tell them apart from the message.

    `dry_run=True` builds and fills the row — the GitHub read included — and
    returns it without ever asking the Library, so an operator previewing a
    registration sees exactly the row that would be sent.

    On a 201, `projects._load_raw`'s cache is cleared, so a caller in THIS
    SAME PROCESS that reads the registry right after registering (`agentctl
    crew create`'s next step; the doctor's KB-slug check) sees the new crew
    rather than whatever `projects.py` had already cached.

    `opener` is passed straight through to BOTH `_repo_ci_workflows`'s own
    GitHub calls and `_post_crew_row`'s POST, so one fake opener can answer
    both in a single test — the real `urllib.request.urlopen` by default,
    for production."""
    row = build_row(spec)
    row["ci_workflows"] = {repo: _repo_ci_workflows(repo, row, opener=opener)
                           for repo in row["repos"]}
    ci_errors = projects.ci_workflows_errors({"repos": row["repos"], "ci_workflows": row["ci_workflows"]})
    if ci_errors:
        raise RegistrationRefused(f"ci_workflows invalid: {'; '.join(ci_errors)}")
    if dry_run:
        return row
    stored = _post_crew_row({**row, "slug": spec.slug}, opener=opener)
    projects._load_raw.cache_clear()
    return stored
