"""config/projects.seed.json: the registry a fresh install's Library starts
with — Shipwright writes it through the Library's own guarded `--replace`
load (platform/decisions/0078), so an install begins with the one crew every
install needs (the Library itself) and nothing else.

This module checks only that the seed itself loads cleanly through
projects.py and carries the shape a fresh install's Library needs. CQ·565
deleted config/projects.json, the file this module used to diff the seed
against to prove it carried nothing identifying from the real registry —
that comparison has no subject in this repo any more (the real registry
lives in the Library now, served over the wire, not committed here), so the
leak-detection apparatus built for it is gone along with it. Reviewing the
seed's own content by hand, at authoring time, is what stands in its place.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import projects

REPO = Path(__file__).resolve().parent.parent
SEED = REPO / "config" / "projects.seed.json"


@pytest.fixture
def seed_registry(monkeypatch):
    """Point the loader at the seed file, the way an adopter's PROJECTS_JSON
    override would, and restore it afterward so later tests see whatever
    registry the suite's own default fixture set."""
    monkeypatch.setattr(projects, "PROJECTS_JSON", SEED)
    projects._load_raw.cache_clear()
    yield
    projects._load_raw.cache_clear()


def test_seed_is_valid_json():
    with open(SEED, encoding="utf-8") as f:
        json.load(f)


def test_seed_schema_matches_the_registrys_checks(seed_registry):
    """The same per-unit assertions test_projects.py's test_schema_wellformed
    applies to a committed registry, run against the seed's own units."""
    for slug, u in projects.load().items():
        for field in projects.REQUIRED_FIELDS:
            assert field in u, f"{slug} missing required field {field!r}"
        assert u["class"] in projects.CLASSES, f"{slug} has bad class {u['class']!r}"
        assert isinstance(u["display"], str) and u["display"]
        assert isinstance(u["repos"], list) and u["repos"]
        assert isinstance(u.get("aliases", []), list)
        assert u["kb"] in projects.KB_VALUES, f"{slug} has bad kb value {u['kb']!r}"
        if u["kb"] in ("central", "split"):
            assert u.get("kb_project"), f"{slug} kb={u['kb']} needs kb_project"
        assert isinstance(u["home_dir"], str) and u["home_dir"], f"{slug} needs a home_dir"
        assert isinstance(u["dirs"], list), f"{slug} dirs must be a list"
        assert u["lifecycle"] in projects.LIFECYCLES, (
            f"{slug} has bad lifecycle {u['lifecycle']!r}")
        assert isinstance(u["products"], list), f"{slug} products must be a list"
        assert u["crew"] is None or isinstance(u["crew"], str), (
            f"{slug} crew must be a slug or null")


def test_seed_has_exactly_one_project_named_library(seed_registry):
    assert set(projects.load()) == {"library"}
    u = projects.get("library")
    assert u["repos"] == ["knowledge"]
    assert u["crew"] == "library"
    assert u["kb"] == "central"
    assert u["kb_project"] == "library"
    assert u["lifecycle"] == "active"
    assert u["products"] == []


def test_seed_has_no_products_block_entries(seed_registry):
    assert projects.products() == {}


def test_seed_home_dir_is_the_root_relative_form(seed_registry):
    u = projects.get("library")
    assert u["home_dir"].startswith("<root>/")
    # registry_home() must accept the stored form without raising.
    projects.registry_home(u["home_dir"])


def test_seed_resolves_knowledge_to_library(seed_registry):
    """The same answer the Library's `POST /review-records` resolves the
    `knowledge` repo through."""
    assert projects.project_for_repo("knowledge") == "library"
