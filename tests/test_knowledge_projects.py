"""The validator's allowed projects come from the project registry
(platform/decisions/0097), not from the knowledge repo's `projects/`
directories: a tree may hold a directory the registry does not name, and the
indexer refuses a document under it rather than admitting it on the directory's
mere presence. The indexer names each document it drops. Every tree here is a
tmp_path; nothing reads HOME, the live knowledge repo or the live index.

Marked `rich_registry` at module scope (CQ-546): most tests here use a real,
already-registered slug as a convenient stand-in for "a project the registry
does name", and several assert directly against
`knowledge.ALLOWED_PROJECTS`/`cat.allowed`, which is derived from the
committed registry — so the file's own baseline has to be the real one for
those to mean what they say. The tests that build their own registry already
monkeypatch `projects.PROJECTS_JSON` themselves (`synthetic_private_registry`,
`unreadable_registry`), which overrides this marker's effect exactly as it
overrides any other default.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

import crew_doctor as CD
import indexer
import knowledge
import projects

pytestmark = pytest.mark.rich_registry

REPO = Path(__file__).resolve().parent.parent
SCHEMA = REPO / "lib" / "knowledge" / "schema.sql"

DOC = """---
id: {project}/reference/{slug}
kind: reference
project: {project}
title: A doc
status: current
---

Body.
"""


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """An empty knowledge tree with its own index, the indexer pointed at both."""
    root = tmp_path / "knowledge"
    (root / "projects").mkdir(parents=True)
    monkeypatch.setattr(indexer, "KNOWLEDGE", root)
    monkeypatch.setattr(indexer, "PROJECTS", root / "projects")
    monkeypatch.setattr(indexer, "DB", tmp_path / "index.db")
    monkeypatch.setattr(indexer, "SCHEMA", SCHEMA)
    indexer.ensure_db()
    return root


def _doc(root: Path, directory: str, project: str, slug: str = "first") -> str:
    path = root / "projects" / directory / "reference" / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DOC.format(project=project, slug=slug))
    return f"projects/{directory}/reference/{slug}.md"


def _fm(project: str) -> dict:
    return {"id": f"{project}/reference/first", "kind": "reference",
            "project": project, "title": "A doc", "status": "current"}


def test_a_document_under_an_unregistered_project_is_refused_and_never_indexes(tree):
    """platform/decisions/0097 closes the catalogue to the registry: a directory
    the registry does not name is refused even when it is a syntactically valid
    slug and already holds a document. On origin/main this asserts the opposite
    (the document validated and indexed) and fails."""
    assert "adopter-app" not in knowledge.ALLOWED_PROJECTS
    rel = _doc(tree, "adopter-app", "adopter-app")

    fm, err = indexer.parse_doc(tree / rel)
    assert fm is None and "not a project the registry names" in err

    res = indexer.full_scan_and_reconcile()
    assert res["indexed"] == 0
    assert [r for r, _ in res["invalid"]] == [rel]
    assert "not a project the registry names" in res["invalid"][0][1]


def test_a_pending_name_outside_the_registry_is_refused_in_the_catalogue(tree):
    """`kb propose`/`kb create` name a `projects/<new>/` directory that does not
    exist yet, through `pending`; platform/decisions/0097 refuses it there
    exactly as it refuses a directory already on disk."""
    cat = knowledge.project_catalogue(tree, pending="adopter-app")
    assert "adopter-app" not in cat.allowed
    assert "not a project the registry names" in cat.refused["adopter-app"]


def test_a_registered_project_is_admitted_on_disk_pending_and_by_document_errors(tree):
    """The counterpart to the refusals above: a project the registry does name
    is admitted whether its directory already exists, is only a pending write,
    or is checked through `indexer.document_errors`."""
    rel = _doc(tree, "oak-marketing", "oak-marketing")
    cat = knowledge.project_catalogue(tree)
    assert "oak-marketing" in cat.allowed and "oak-marketing" not in cat.refused

    pending_cat = knowledge.project_catalogue(tree, pending="telemetry")
    assert "telemetry" in pending_cat.allowed and "telemetry" not in pending_cat.refused

    fm, err = indexer.parse_doc(tree / rel)
    assert err is None and fm["project"] == "oak-marketing"
    assert indexer.document_errors(
        DOC.format(project="telemetry", slug="first"),
        "projects/telemetry/reference/first.md") == []


def test_the_catalogue_reads_the_tree_it_is_given_not_the_live_one(tree, tmp_path):
    """The registry closes `allowed` to the same set regardless of tree, but the
    `refused` bookkeeping — read by `indexer.document_errors` to report why a
    document under a given directory was dropped — is still read from the root
    this call was given, not from some other tree or a cached default."""
    other = tmp_path / "other"
    (other / "projects" / "elsewhere").mkdir(parents=True)

    assert "elsewhere" not in knowledge.project_catalogue(other).allowed
    assert "elsewhere" in knowledge.project_catalogue(other).refused
    assert "elsewhere" not in knowledge.project_catalogue(tree).refused
    cat = knowledge.project_catalogue(other)
    assert knowledge.validate_frontmatter(_fm("elsewhere"), cat) != []


@pytest.mark.parametrize("project", sorted(knowledge.ALLOWED_PROJECTS))
def test_every_project_on_todays_list_still_validates(tree, project):
    """Also with no directory for it in the tree: the baseline stays allowed."""
    cat = knowledge.project_catalogue(tree)
    assert project in cat.allowed
    assert knowledge.validate_frontmatter(_fm(project), cat) == []
    assert cat.id_pattern.match(f"{project}/reference/x")


def test_the_derived_id_pattern_and_allowed_set_agree(tree):
    """A directory on disk no longer grows `allowed` beyond the registry's set
    (platform/decisions/0097), so `cat.id_pattern` and the module baseline
    `ID_PATTERN` agree on every tree. On origin/main the first assertion here
    (`cat.id_pattern` matching `adopter-app`) held and this one did not; now it
    is the reverse."""
    (tree / "projects" / "adopter-app").mkdir()
    cat = knowledge.project_catalogue(tree)
    assert not cat.id_pattern.match("adopter-app/reference/x")
    assert not knowledge.ID_PATTERN.match("adopter-app/reference/x")
    assert not cat.id_pattern.match("nowhere/reference/x")
    assert cat.allowed == knowledge.ALLOWED_PROJECTS
    assert cat.id_pattern.pattern == knowledge.ID_PATTERN.pattern


@pytest.mark.parametrize("scope", ["shared", "convivy", "utility", "engineering"])
def test_a_retired_scope_is_still_refused_with_its_reason(tree, scope):
    rel = _doc(tree, scope, scope)
    cat = knowledge.project_catalogue(tree)

    assert scope not in cat.allowed
    assert "retired scope" in cat.refused[scope]
    errs = knowledge.validate_frontmatter(_fm(scope), cat)
    assert any(e.startswith(f"invalid project: {scope}") and "retired scope" in e
               for e in errs), errs
    fm, err = indexer.parse_doc(tree / rel)
    assert fm is None and "retired scope" in err
    # a retired name with no directory at all is refused the same way
    assert scope in knowledge.project_catalogue(tree.parent).refused


def test_a_retired_scope_directory_drops_a_document_whose_project_is_valid(tree):
    rel = _doc(tree, "shared", "harbor")
    fm, err = indexer.parse_doc(tree / rel)
    assert fm is None and "directory refused" in err and "retired scope" in err


def test_a_dot_led_directory_is_refused_with_a_reason(tree):
    rel = _doc(tree, ".hidden", "harbor")
    cat = knowledge.project_catalogue(tree)
    assert ".hidden" not in cat.allowed
    assert "not a plain lowercase directory name" in cat.refused[".hidden"]
    assert indexer.full_scan_and_reconcile()["invalid"] == [
        (rel, "directory refused: " + cat.refused[".hidden"])]


def test_dotdot_and_other_bad_names_are_refused(tree):
    # A trailing newline: `$` matches before it, so `.match` admitted each of these.
    for name in ("..", ".", "a/b", "Upper", "9lives", "with_underscore", "",
                 "foo\n", "ledger\n", "shared\n"):
        assert knowledge.project_dir_refusal(tree, name), repr(name)
        assert knowledge._name_refusal(name), repr(name)
    assert knowledge.project_catalogue(tree, pending="foo\n").refused["foo\n"]


def test_an_id_or_date_with_a_trailing_newline_is_refused(tree):
    cat = knowledge.project_catalogue(tree)
    assert cat.id_pattern.fullmatch("harbor/reference/x")
    for bad in ("harbor/reference/x\n", "harbor\n/reference/x"):
        assert not cat.id_pattern.match(bad), repr(bad)
        errs = knowledge.validate_frontmatter({**_fm("harbor"), "id": bad}, cat)
        assert any(e.startswith("invalid id format") for e in errs), (bad, errs)
    errs = knowledge.validate_frontmatter(
        {**_fm("harbor"), "last_reviewed": "2026-09-01\n"}, cat)
    assert any(e.startswith("last_reviewed must be") for e in errs), errs


def test_a_symlink_out_of_the_repo_is_refused_and_one_inside_is_not(tree, tmp_path):
    """Both directory names used here are registered projects (platform/decisions/
    0097 would otherwise refuse an unregistered name before either filesystem
    check ran, which is what made `escapee`/`real`/`alias` — none of them
    registered — the wrong fixtures once the catalogue closed): `harbor`'s
    `projects/` entry is a symlink escaping the repo and is refused for that,
    `northwind-orient`'s is an in-repo symlink to another registered project's
    directory and is admitted the same as any real one."""
    outside = tmp_path / "outside"
    (outside / "reference").mkdir(parents=True)
    (tree / "projects" / "harbor").symlink_to(outside)
    (tree / "projects" / "beacon").mkdir()
    # An in-repo link is admitted on purpose and harmlessly: `rglob` does not
    # descend into a symlinked directory and git records it as one non-.md path, so
    # no document is walked or indexed twice through the alias.
    (tree / "projects" / "northwind-orient").symlink_to(tree / "projects" / "beacon")

    cat = knowledge.project_catalogue(tree)
    assert "outside the knowledge repo" in cat.refused["harbor"]
    assert "northwind-orient" not in cat.refused
    errs = knowledge.project_dir_errors(cat, "projects/harbor/reference/x.md")
    assert errs and "outside the knowledge repo" in errs[0]
    assert knowledge.project_dir_errors(cat, "projects/northwind-orient/reference/x.md") == []


def test_a_file_under_projects_is_not_a_project(tree):
    """`harbor`, a registered project: on origin/main an unregistered name
    (`readme`) demonstrated this fine, but under the closed catalogue an
    unregistered name is refused before this check ever runs, so the fixture
    has to be a name the registry does name. `harbor` stays in `cat.allowed`
    regardless — that set is the registry's baseline, not a per-directory
    health check — so the assertion that matters is on `refused` and on the
    error a document under the broken directory actually gets."""
    (tree / "projects" / "harbor").write_text("x")
    cat = knowledge.project_catalogue(tree)
    assert cat.refused["harbor"] == "'harbor' is not a directory"
    assert knowledge.project_dir_errors(cat, "projects/harbor/reference/x.md") == [
        "directory refused: 'harbor' is not a directory"]


def test_a_dropped_document_gets_a_line_naming_the_file_and_the_reason(tree):
    rel = _doc(tree, "harbor", "harbor")
    (tree / rel).write_text((tree / rel).read_text().replace("kind: reference", "kind: bogus"))

    res = indexer.full_scan_and_reconcile()
    assert res["invalid"] == [(rel, "invalid kind: bogus")]


def test_the_first_document_of_an_unregistered_project_is_refused(tree):
    """platform/decisions/0097: `kb_create`/`kb_update` commit through
    `agentctl kb apply` with no pull request, so this precheck is the only gate
    on that path. On origin/main the first assertion here passed with no
    errors — a project absent from the registry could create its own
    directory through a write alone — and this one fails there."""
    text = DOC.format(project="brand-new", slug="first")
    rel = "projects/brand-new/reference/first.md"
    assert not (tree / "projects" / "brand-new").exists()

    errs = indexer.document_errors(text, rel)
    assert any("not a project the registry names" in e for e in errs), errs
    assert indexer.document_errors(text) != []  # no path, no directory to admit
    retired = DOC.format(project="shared", slug="first")
    assert any("retired scope" in e
               for e in indexer.document_errors(retired, "projects/shared/reference/first.md"))


def test_the_first_document_of_a_registered_project_passes_the_write_precheck(tree):
    """The same precheck admits a project the registry does name, with no
    directory for it yet, the same as before this decision."""
    text = DOC.format(project="oak-guest", slug="first")
    rel = "projects/oak-guest/reference/first.md"
    assert not (tree / "projects" / "oak-guest").exists()
    assert indexer.document_errors(text, rel) == []


def test_the_crew_doctor_reads_the_derived_set(tree, agentctl, monkeypatch):
    monkeypatch.setattr(agentctl, "KNOWLEDGE_ROOT", tree)
    monkeypatch.setattr(agentctl.SC, "token", lambda: "t")
    seen = {}

    def diagnose(crew, **kw):
        seen.update(kw)
        return [CD.Result("s", "o", CD.PRESENT, "")]

    monkeypatch.setattr(CD, "diagnose", diagnose)
    args = types.SimpleNamespace(crew="x", all_crews=False, variant="daytime")
    assert agentctl.cmd_crew_doctor(args) == 0

    assert seen["allowed"] == knowledge.project_catalogue(tree).allowed
    assert "harbor" in seen["allowed"]
    assert seen["id_pattern"].match("harbor/reference/x")


# ---- the private (black-box) crews, platform/decisions/0043 ----------------------
#
# Private crews were kept out of the index only because their slugs were absent
# from the closed ALLOWED_PROJECTS. Deriving the catalogue from the tree removed
# that absence, so the refusal has to be stated.

PRIVATE_DIRS = ["private-a", "PRIVATE-A", "private-b", "PRIVATE-B", "private-c"]


@pytest.fixture
def unreadable_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(projects, "PROJECTS_JSON", tmp_path / "no-such-registry.json")
    projects._load_raw.cache_clear()
    yield
    projects._load_raw.cache_clear()


@pytest.fixture
def synthetic_private_registry(tmp_path, monkeypatch):
    """Point the loader at a registry naming three synthetic black-box crews, the
    way a packaged tenant instance does via PROJECTS_JSON (test_projects.py's
    `crafted_registry` fixture does the same for the cross-user-path tests) — so
    the directory-refusal mechanism below is exercised without the operator's
    own crew slugs."""
    def _unit(slug):
        return {"class": "project", "display": slug, "owner": "convivy",
                "home_dir": f"<root>/{slug}", "dirs": [], "crew": slug,
                "lifecycle": "active", "products": [], "repos": [slug],
                "aliases": [], "kb": "local", "private": True}
    registry = {slug: _unit(slug) for slug in ("private-a", "private-b", "private-c")}
    p = tmp_path / "synthetic-projects.json"
    p.write_text(json.dumps(registry))
    monkeypatch.setattr(projects, "PROJECTS_JSON", p)
    projects._load_raw.cache_clear()
    yield
    projects._load_raw.cache_clear()


def test_the_refusal_set_is_the_registrys_private_crews(tree):
    """The mechanism: `knowledge.private_crew_names()` is exactly the registry's
    own private slugs, disjoint from every public one and from the baseline, and
    every one of them is refused as a directory. Against the live registry — not
    `synthetic_private_registry` — because it is the live set this reads, not a
    fixed one; see test_the_operators_private_crews_match_the_expected_list
    below for the one assertion that is a fact about that live set rather than
    a mechanism."""
    private = knowledge.private_crew_names()
    assert private == frozenset(projects._private_crew_names())
    assert {s.casefold() for s in projects.private_slugs()} <= private
    # and no public crew's slug is among them
    assert not (private & {s for s in projects.load() if s not in projects.private_slugs()})
    assert not (private & knowledge.ALLOWED_PROJECTS)
    # every private name is refused as a directory even when none is on disk
    cat = knowledge.project_catalogue(tree)
    for name in private:
        assert "private crew" in cat.refused[name], name


EXPECTED_PRIVATE_CREWS_FILE = os.environ.get("AGENT_PLATFORM_EXPECTED_PRIVATE_CREWS_FILE")


def test_the_operators_private_crews_match_the_expected_list():
    """A fact about the operator's own registry, not a mechanism: which slugs
    are (and must stay) registered private. The expected set is read from a
    file named by AGENT_PLATFORM_EXPECTED_PRIVATE_CREWS_FILE rather than a
    literal here, so the names never enter this file or its diff history;
    unset on CI and in the Builder export (which excludes
    config/projects.json entirely), where there is nothing to compare
    against — see tests/fixtures/expected_private_crews.example.txt for the
    file's shape. Never derive the expected set from the registry's own
    `private` flag: that is exactly the mechanism this test exists to catch
    drifting, and reading it back out of itself would make the assertion
    vacuous."""
    if not EXPECTED_PRIVATE_CREWS_FILE:
        pytest.skip("AGENT_PLATFORM_EXPECTED_PRIVATE_CREWS_FILE is unset")
    expected = {ln.strip() for ln in Path(EXPECTED_PRIVATE_CREWS_FILE).read_text().splitlines()
                if ln.strip() and not ln.startswith("#")}
    assert expected <= knowledge.private_crew_names()


@pytest.mark.parametrize("directory", PRIVATE_DIRS)
def test_a_document_under_a_private_crews_directory_is_refused(
        tree, directory, synthetic_private_registry):
    rel = _doc(tree, directory, directory.lower())
    cat = knowledge.project_catalogue(tree)
    assert directory.casefold() not in cat.allowed and directory not in cat.allowed
    assert "private crew" in cat.refused[directory]
    assert "0043" in cat.refused[directory]
    assert "retired" not in cat.refused[directory]

    fm, err = indexer.parse_doc(tree / rel)
    assert fm is None and "directory refused" in err and "private crew" in err

    res = indexer.full_scan_and_reconcile()
    assert res["indexed"] == 0
    assert [r for r, _ in res["invalid"]] == [rel]
    assert "private crew" in res["invalid"][0][1]
    # served to nobody
    assert indexer.verify_index()["indexed"] == 0


@pytest.mark.parametrize("directory", PRIVATE_DIRS)
def test_a_private_crews_first_document_fails_the_write_precheck(
        tree, agentctl, directory, synthetic_private_registry):
    """`kb propose` writes `projects/<name>/...`, a directory the checkout does not
    hold yet, so the name is admitted through `pending`."""
    text = DOC.format(project=directory.lower(), slug="first")
    rel = f"projects/{directory}/reference/first.md"
    assert not (tree / "projects" / directory).exists()

    for errs in (indexer.document_errors(text, rel), agentctl._kb_indexer_errors(rel, text)):
        assert any("private crew" in e and "0043" in e for e in errs), errs
    # An ordinary unregistered name is refused too (platform/decisions/0097), but
    # on 0097's reason rather than 0043's — the two gates are independent, and a
    # private crew's refusal must still name the privacy wall specifically.
    brand_new_errs = agentctl._kb_indexer_errors(
        "projects/brand-new/reference/first.md",
        DOC.format(project="brand-new", slug="first"))
    assert any("0097" in e for e in brand_new_errs), brand_new_errs
    assert not any("0043" in e for e in brand_new_errs), brand_new_errs


@pytest.mark.parametrize("project", ["private-a", "PRIVATE-B", "private-c"])
def test_a_private_crews_name_in_frontmatter_is_refused_under_any_directory(
        tree, project, synthetic_private_registry):
    rel = _doc(tree, "harbor", project)
    fm, err = indexer.parse_doc(tree / rel)
    assert fm is None and f"invalid project: {project}" in err and "private crew" in err


def test_an_unreadable_registry_refuses_every_name_including_the_baseline(
        tree, agentctl, unreadable_registry):
    """Decision 0097, point 2: a name outside the registry's set is refused
    whether or not the registry is readable. A live-read failure must refuse
    every name exactly as an unreadable registry at import does — never fall
    back to `ALLOWED_PROJECTS`, the set captured at import (reviewer +
    security-reviewer, PR #978 round 2 — see
    test_a_registry_that_turns_unreadable_mid_process_refuses_every_name_on_the_next_call
    below for the narrower, single-process version of this)."""
    assert knowledge.private_crew_names() is None
    cat = knowledge.project_catalogue(tree)
    assert cat.allowed == frozenset()

    (tree / "projects" / "newproj").mkdir()
    cat = knowledge.project_catalogue(tree)
    assert "newproj" not in cat.allowed and "could not be read" in cat.refused["newproj"]
    assert knowledge.project_catalogue(tree, pending="brand-new").refused["brand-new"]

    errs = agentctl._kb_indexer_errors(
        "projects/brand-new/reference/first.md",
        DOC.format(project="brand-new", slug="first"))
    assert any("could not be read" in e for e in errs), errs
    # a baseline project is refused too — an outage must not keep admitting it
    harbor_errs = indexer.document_errors(
        DOC.format(project="harbor", slug="first"),
        "projects/harbor/reference/first.md")
    assert any("could not be read" in e for e in harbor_errs), harbor_errs


def test_a_registry_that_turns_unreadable_mid_process_refuses_every_name_on_the_next_call(
        tmp_path, monkeypatch):
    """`_current_allowed_projects()` reads live every call (platform/decisions/0097)
    rather than caching import's answer, so a registry readable on the first
    call and gone on the second refuses every name on the second — including a
    name the first call just admitted, with no fallback to the import-time
    baseline (finding 3, PR #978 round 2). Asserted on `_current_allowed_projects()`
    itself rather than `_name_refusal`'s message: `_name_refusal` also consults
    `private_crew_names()`, a separate, still-cached read this round does not
    touch, so its wording here would depend on that cache's own state rather
    than on the fix under test."""
    registry = {
        "widget-crew": {
            "class": "project", "display": "Widget Crew", "owner": "convivy",
            "home_dir": "<root>/widget-crew", "dirs": [], "crew": "widget-crew",
            "lifecycle": "active", "products": [], "repos": ["widget-crew"],
            "aliases": [], "kb": "central", "kb_project": "widget-crew",
        },
    }
    path = tmp_path / "projects.json"
    path.write_text(json.dumps(registry))
    monkeypatch.setattr(projects, "PROJECTS_JSON", path)
    assert "widget-crew" in knowledge._current_allowed_projects()

    path.unlink()
    assert knowledge._current_allowed_projects() == frozenset()


def test_an_unreadable_registry_at_import_makes_import_succeed_and_refuses_every_name(
        tmp_path):
    """`ALLOWED_PROJECTS` is computed once, at import time, from
    `projects.legal_kb_projects()` (platform/decisions/0097). Importing
    `knowledge` must never raise for a registry it cannot read — it must come
    up with an empty `ALLOWED_PROJECTS` instead, a closed catalogue that
    refuses every name with the same reason `_name_refusal` already gives a
    registry that goes unreadable after import (see the fixture above)."""
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(REPO / 'lib' / 'agent-platform')!r})\n"
        f"sys.path.insert(0, {str(REPO / 'lib' / 'knowledge')!r})\n"
        "import knowledge\n"
        "assert knowledge.ALLOWED_PROJECTS == frozenset(), knowledge.ALLOWED_PROJECTS\n"
        "for name in ('harbor', 'agent-platform', 'platform', 'anything-at-all'):\n"
        "    why = knowledge._name_refusal(name)\n"
        "    assert why and 'could not be read' in why, (name, why)\n"
        "print('OK')\n"
    )
    env = dict(os.environ)
    env["PROJECTS_JSON"] = str(tmp_path / "no-such-registry.json")
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                          env=env, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout


def test_current_allowed_projects_admits_a_project_registered_mid_process_with_no_cache_clear(
        tmp_path, monkeypatch):
    """The whole point of the CI-fix commit (platform/decisions/0097): a
    long-running reader (`bin/knowledge-watcher`, `bin/mcp-kb-server`) admits a
    project the registry names after the process started, with nothing cleared
    and nothing restarted. `projects._load_raw`'s unbounded `lru_cache` made the
    live read not actually live (finding 1, PR #978 round 2) — this asserts the
    second call sees a changed registry with no
    `projects._load_raw.cache_clear()` call anywhere in this test. Fails against
    the round-1 head: the second assertion sees the stale (refusing) answer the
    first call cached.

    Asserted on `_current_allowed_projects()` directly rather than
    `_name_refusal`'s refusal — `_name_refusal` also consults
    `private_crew_names()`, which DOES read through `_load_raw`'s cache, and
    calling it here would leave that cache holding this test's throwaway
    registry for every later test in the same process (round 2's own first
    attempt at this test did exactly that and broke three unrelated test
    files)."""
    registry = {"_kb_legacy": ["existing-scope"]}
    path = tmp_path / "projects.json"
    path.write_text(json.dumps(registry))
    monkeypatch.setattr(projects, "PROJECTS_JSON", path)

    assert "new-scope" not in knowledge._current_allowed_projects()

    registry["_kb_legacy"].append("new-scope")
    path.write_text(json.dumps(registry))
    assert "new-scope" in knowledge._current_allowed_projects()


def test_project_catalogue_parses_the_registry_once_for_many_documents(
        tree, tmp_path, monkeypatch):
    """`project_catalogue()` calls `_current_allowed_projects()` — and so
    `projects.legal_kb_projects_live()` — once directly, once per directory
    under `root/projects`, once for `pending`, and once per retired/private
    name, and `indexer.parse_doc`/`document_errors` call `project_catalogue()`
    once per document validated (reviewer, PR #978 round 2): without a cache
    on `legal_kb_projects_live()`'s own read, validating many documents against
    an unchanged registry multiplies a single open-and-parse of
    config/projects.json by all of that. Fails against the current head: the
    spy below sees far more than one call, because every one of those calls
    reopens and re-parses the file."""
    registry = {
        "alpha": {
            "class": "project", "display": "Alpha", "owner": "convivy",
            "home_dir": "<root>/alpha", "dirs": [], "crew": "alpha",
            "lifecycle": "active", "products": [], "repos": ["alpha"],
            "aliases": [], "kb": "central", "kb_project": "alpha",
        },
    }
    path = tmp_path / "registry-projects.json"
    path.write_text(json.dumps(registry))
    monkeypatch.setattr(projects, "PROJECTS_JSON", path)
    projects._load_raw.cache_clear()
    try:
        # Warms `_load_raw`'s own, separately process-lifetime-cached read, so
        # the spy installed below counts only `legal_kb_projects_live`'s reads.
        projects._private_crew_names()

        parses = []
        real_load = json.load

        def spy(f):
            parses.append(1)
            return real_load(f)

        monkeypatch.setattr(projects.json, "load", spy)

        for i in range(20):
            rel = _doc(tree, "alpha", "alpha", slug=f"doc-{i}")
            fm, err = indexer.parse_doc(tree / rel)
            assert err is None, err

        assert len(parses) == 1, len(parses)
    finally:
        projects._load_raw.cache_clear()


def test_a_regex_breaking_kb_project_does_not_break_import_and_is_refused(tmp_path):
    """`id_pattern_for` builds `ID_PATTERN` from every legal project name,
    including a unit's own registry-controlled `kb_project` — unvalidated
    against regex syntax until `crew_registration.build_row` also checked it
    (finding 2, PR #978 round 2). A registry holding a `kb_project` that is
    not valid regex syntax must not raise at `re.compile()` when `knowledge`
    imports, and the bad name itself must still be refused."""
    registry = {
        "widget-crew": {
            "class": "project", "display": "Widget Crew", "owner": "convivy",
            "home_dir": "<root>/widget-crew", "dirs": [], "crew": "widget-crew",
            "lifecycle": "active", "products": [], "repos": ["widget-crew"],
            "aliases": [], "kb": "central", "kb_project": "widget(crew",
        },
    }
    path = tmp_path / "projects.json"
    path.write_text(json.dumps(registry))
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(REPO / 'lib' / 'agent-platform')!r})\n"
        f"sys.path.insert(0, {str(REPO / 'lib' / 'knowledge')!r})\n"
        "import knowledge\n"
        "why = knowledge._name_refusal('widget(crew')\n"
        "assert why, 'expected the regex-breaking kb_project to be refused'\n"
        "print('OK')\n"
    )
    env = dict(os.environ)
    env["PROJECTS_JSON"] = str(path)
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                          env=env, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout


# CQ·565: `OLD_LITERAL_PROJECTS` and `test_every_slug_in_the_old_literal_is_
# still_admitted`, which used to live here, pinned the 30 names `ALLOWED_
# PROJECTS` carried as a hand-kept literal before platform/decisions/0097 —
# written as data rather than read back from `knowledge.ALLOWED_PROJECTS`
# itself, as a regression guard that the registry-derived replacement still
# admitted every one of the original 30 real names. That subject is gone:
# the committed registry those 30 names came from no longer exists in this
# repo, and the `rich_registry` fixture's own slugs are invented, with
# nothing left for this test to check the migration against.
