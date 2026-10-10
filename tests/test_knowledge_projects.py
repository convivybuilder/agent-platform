"""The validator's allowed projects come from the knowledge repo's `projects/`
directories (site 4 of the seeding item), and the indexer names each document it
drops. Every tree here is a tmp_path; nothing reads HOME, the live knowledge repo
or the live index."""
from __future__ import annotations

import json
import os
import types
from pathlib import Path

import pytest

import crew_doctor as CD
import indexer
import knowledge
import projects

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


def test_a_document_under_an_unlisted_project_validates_and_indexes(tree):
    assert "adopter-app" not in knowledge.ALLOWED_PROJECTS
    rel = _doc(tree, "adopter-app", "adopter-app")

    fm, err = indexer.parse_doc(tree / rel)
    assert err is None and fm["project"] == "adopter-app"

    res = indexer.full_scan_and_reconcile()
    assert (res["indexed"], res["invalid"]) == (1, [])
    assert indexer.verify_index()["ok"] is True


def test_the_catalogue_reads_the_tree_it_is_given_not_the_live_one(tree, tmp_path):
    other = tmp_path / "other"
    (other / "projects" / "elsewhere").mkdir(parents=True)

    assert "elsewhere" in knowledge.project_catalogue(other).allowed
    assert "elsewhere" not in knowledge.project_catalogue(tree).allowed
    cat = knowledge.project_catalogue(other)
    assert knowledge.validate_frontmatter(_fm("elsewhere"), cat) == []
    assert knowledge.validate_frontmatter(_fm("elsewhere")) != []


@pytest.mark.parametrize("project", sorted(knowledge.ALLOWED_PROJECTS))
def test_every_project_on_todays_list_still_validates(tree, project):
    """Also with no directory for it in the tree: the baseline stays allowed."""
    cat = knowledge.project_catalogue(tree)
    assert project in cat.allowed
    assert knowledge.validate_frontmatter(_fm(project), cat) == []
    assert cat.id_pattern.match(f"{project}/reference/x")


def test_the_derived_id_pattern_and_allowed_set_agree(tree):
    (tree / "projects" / "adopter-app").mkdir()
    cat = knowledge.project_catalogue(tree)
    assert cat.id_pattern.match("adopter-app/reference/x")
    assert not knowledge.ID_PATTERN.match("adopter-app/reference/x")
    assert not cat.id_pattern.match("nowhere/reference/x")


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
    rel = _doc(tree, "shared", "constance")
    fm, err = indexer.parse_doc(tree / rel)
    assert fm is None and "directory refused" in err and "retired scope" in err


def test_a_dot_led_directory_is_refused_with_a_reason(tree):
    rel = _doc(tree, ".hidden", "constance")
    cat = knowledge.project_catalogue(tree)
    assert ".hidden" not in cat.allowed
    assert "not a plain lowercase directory name" in cat.refused[".hidden"]
    assert indexer.full_scan_and_reconcile()["invalid"] == [
        (rel, "directory refused: " + cat.refused[".hidden"])]


def test_dotdot_and_other_bad_names_are_refused(tree):
    # A trailing newline: `$` matches before it, so `.match` admitted each of these.
    for name in ("..", ".", "a/b", "Upper", "9lives", "with_underscore", "",
                 "foo\n", "cfo\n", "shared\n"):
        assert knowledge.project_dir_refusal(tree, name), repr(name)
        assert knowledge._name_refusal(name), repr(name)
    assert knowledge.project_catalogue(tree, pending="foo\n").refused["foo\n"]


def test_an_id_or_date_with_a_trailing_newline_is_refused(tree):
    cat = knowledge.project_catalogue(tree)
    assert cat.id_pattern.fullmatch("constance/reference/x")
    for bad in ("constance/reference/x\n", "constance\n/reference/x"):
        assert not cat.id_pattern.match(bad), repr(bad)
        errs = knowledge.validate_frontmatter({**_fm("constance"), "id": bad}, cat)
        assert any(e.startswith("invalid id format") for e in errs), (bad, errs)
    errs = knowledge.validate_frontmatter(
        {**_fm("constance"), "last_reviewed": "2026-09-01\n"}, cat)
    assert any(e.startswith("last_reviewed must be") for e in errs), errs


def test_a_symlink_out_of_the_repo_is_refused_and_one_inside_is_not(tree, tmp_path):
    outside = tmp_path / "outside"
    (outside / "reference").mkdir(parents=True)
    (tree / "projects" / "escapee").symlink_to(outside)
    (tree / "projects" / "real").mkdir()
    # An in-repo link is admitted on purpose and harmlessly: `rglob` does not
    # descend into a symlinked directory and git records it as one non-.md path, so
    # no document is walked or indexed twice through the alias. The only effect is
    # that `alias` becomes a valid `project:` value, as any real directory would.
    (tree / "projects" / "alias").symlink_to(tree / "projects" / "real")

    cat = knowledge.project_catalogue(tree)
    assert "escapee" not in cat.allowed
    assert "outside the knowledge repo" in cat.refused["escapee"]
    assert {"real", "alias"} <= cat.allowed


def test_a_file_under_projects_is_not_a_project(tree):
    (tree / "projects" / "readme").write_text("x")
    cat = knowledge.project_catalogue(tree)
    assert "readme" not in cat.allowed
    assert cat.refused["readme"] == "'readme' is not a directory"


def test_a_dropped_document_gets_a_line_naming_the_file_and_the_reason(tree):
    rel = _doc(tree, "constance", "constance")
    (tree / rel).write_text((tree / rel).read_text().replace("kind: reference", "kind: bogus"))

    res = indexer.full_scan_and_reconcile()
    assert res["invalid"] == [(rel, "invalid kind: bogus")]


def test_the_first_document_of_a_new_project_passes_the_write_precheck(tree):
    text = DOC.format(project="brand-new", slug="first")
    rel = "projects/brand-new/reference/first.md"
    assert not (tree / "projects" / "brand-new").exists()

    assert indexer.document_errors(text, rel) == []
    assert indexer.document_errors(text) != []  # no path, no directory to admit
    retired = DOC.format(project="shared", slug="first")
    assert any("retired scope" in e
               for e in indexer.document_errors(retired, "projects/shared/reference/first.md"))


def test_the_crew_doctor_reads_the_derived_set(tree, agentctl, monkeypatch):
    (tree / "projects" / "adopter-app").mkdir()
    monkeypatch.setattr(agentctl, "KNOWLEDGE_ROOT", tree)
    monkeypatch.setattr(agentctl.SC, "token", lambda: "t")
    seen = {}

    def diagnose(crew, **kw):
        seen.update(kw)
        return [CD.Result("s", "o", CD.PRESENT, "")]

    monkeypatch.setattr(CD, "diagnose", diagnose)
    args = types.SimpleNamespace(crew="x", all_crews=False, variant="daytime")
    assert agentctl.cmd_crew_doctor(args) == 0

    assert "adopter-app" in seen["allowed"] and "constance" in seen["allowed"]
    assert seen["id_pattern"].match("adopter-app/reference/x")


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
    assert agentctl._kb_indexer_errors(
        "projects/brand-new/reference/first.md",
        DOC.format(project="brand-new", slug="first")) == []


@pytest.mark.parametrize("project", ["private-a", "PRIVATE-B", "private-c"])
def test_a_private_crews_name_in_frontmatter_is_refused_under_any_directory(
        tree, project, synthetic_private_registry):
    rel = _doc(tree, "constance", project)
    fm, err = indexer.parse_doc(tree / rel)
    assert fm is None and f"invalid project: {project}" in err and "private crew" in err


def test_an_unreadable_registry_refuses_a_new_name_and_keeps_the_baseline(
        tree, agentctl, unreadable_registry):
    assert knowledge.private_crew_names() is None
    cat = knowledge.project_catalogue(tree)
    assert cat.allowed == knowledge.ALLOWED_PROJECTS

    (tree / "projects" / "newproj").mkdir()
    cat = knowledge.project_catalogue(tree)
    assert "newproj" not in cat.allowed and "could not be read" in cat.refused["newproj"]
    assert knowledge.project_catalogue(tree, pending="brand-new").refused["brand-new"]

    errs = agentctl._kb_indexer_errors(
        "projects/brand-new/reference/first.md",
        DOC.format(project="brand-new", slug="first"))
    assert any("could not be read" in e for e in errs), errs
    # a baseline project still validates, so an outage does not drop the corpus
    assert indexer.document_errors(
        DOC.format(project="constance", slug="first"),
        "projects/constance/reference/first.md") == []
