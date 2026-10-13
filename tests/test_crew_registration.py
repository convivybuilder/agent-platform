"""`agentctl crew register` — one crew, one row, no hand edit.

issue_01M3YN6857H5J541G73Y9P3KA1: Shipwright's single-account rehearsal
created a crew with a directory, a tmux session and a bead but no row in
config/projects.json, and the work-queue tools refused to file its epics
because they resolve `product` through that file. These tests never read or
write the real config/projects.json or any deploy area — every one builds
its own registry under `tmp_path`.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

import crew_registration as CR
import projects


def _seed(tmp_path: Path, extra: dict | None = None) -> Path:
    """A minimal, schema-valid registry: one existing project (so a
    collision has something real to collide with), the `_scopes` and
    `_products` blocks ARCH-426 gives a uniform meaning across the platform.
    """
    registry = {
        "_meta": {"doc": "test fixture"},
        "_products": {"widgets": {"display": "Widgets", "owner": "convivy"}},
        "_scopes": {"platform": {"display": "Platform"}},
        "existing": {
            "class": "project", "display": "Existing", "owner": "convivy",
            "home_dir": "<root>/existing", "dirs": [], "crew": "existing",
            "lifecycle": "active", "products": [], "repos": ["existing-repo"],
            "aliases": ["existing-alias"], "kb": "central", "kb_project": "existing",
            "task_text_public": False, "overnight": False,
        },
    }
    if extra:
        registry.update(extra)
    path = tmp_path / "projects.json"
    path.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n")
    return path


def _spec(**kw) -> CR.CrewSpec:
    base = dict(slug="recipe-units", display="Recipe Units", repos=["recipe-units"])
    base.update(kw)
    return CR.CrewSpec(**base)


# --- a new row lands and resolves through projects.py -----------------------


def test_register_writes_a_schema_valid_row(tmp_path):
    path = _seed(tmp_path)
    returned_path, row = CR.register(_spec(), registry_path=path)
    assert returned_path == path
    data = json.loads(path.read_text())
    assert data["recipe-units"] == row
    for field in projects.REQUIRED_FIELDS:
        assert field in row, f"missing required field {field!r}"
    assert row["class"] in projects.CLASSES
    assert row["kb"] in projects.KB_VALUES
    assert row["lifecycle"] in projects.LIFECYCLES
    # kb defaults to central, so kb_project and crew_cockpit both default on
    assert row["kb_project"] == "recipe-units"
    assert row["crew_cockpit"] is True
    assert row["home_dir"] == "<root>/recipe-units"
    assert row["crew"] == "recipe-units"
    # the existing row is untouched
    assert data["existing"]["repos"] == ["existing-repo"]


def test_registered_row_resolves_through_projects_py(tmp_path, monkeypatch):
    path = _seed(tmp_path)
    CR.register(_spec(repos=["recipe-units", "recipe-units-backend"],
                       aliases=["recipes"]),
                registry_path=path)
    monkeypatch.setattr(projects, "PROJECTS_JSON", path)
    projects._load_raw.cache_clear()
    try:
        assert projects.get("recipe-units")["display"] == "Recipe Units"
        assert projects.project_for_repo("recipe-units-backend") == "recipe-units"
        assert projects.project_for_repo("recipes") == "recipe-units"
        assert projects.crew_for_repo("recipe-units") == "recipe-units"
        assert "recipe-units" in projects.kb_slugs()
    finally:
        projects._load_raw.cache_clear()


def test_dry_run_returns_the_row_but_writes_nothing(tmp_path):
    path = _seed(tmp_path)
    before = path.read_bytes()
    _, row = CR.register(_spec(), registry_path=path, dry_run=True)
    assert row["class"] == "project"
    assert path.read_bytes() == before


# --- refusal: a clashing slug, alias or repo name, each leaving the file unchanged


@pytest.mark.parametrize("spec_kwargs,clash_desc", [
    ({"slug": "existing"}, "project 'existing'"),
    ({"aliases": ["existing-repo"]}, "project 'existing'"),  # alias collides with a REPO
    ({"repos": ["existing-alias"]}, "project 'existing'"),   # repo collides with an ALIAS
    ({"slug": "platform"}, "the 'platform' scope"),
])
def test_a_clashing_name_refuses_and_leaves_the_file_unchanged(
        tmp_path, spec_kwargs, clash_desc):
    path = _seed(tmp_path)
    before = path.read_bytes()
    with pytest.raises(CR.RegistrationRefused) as exc_info:
        CR.register(_spec(**spec_kwargs), registry_path=path)
    assert clash_desc in str(exc_info.value)
    assert path.read_bytes() == before, "a refused registration must not touch the file"


def test_the_match_is_case_insensitive(tmp_path):
    path = _seed(tmp_path)
    before = path.read_bytes()
    with pytest.raises(CR.RegistrationRefused):
        CR.register(_spec(slug="existing".upper().lower(), repos=["EXISTING-REPO"]),
                     registry_path=path)
    assert path.read_bytes() == before


def test_an_unknown_product_refuses(tmp_path):
    path = _seed(tmp_path)
    before = path.read_bytes()
    with pytest.raises(CR.RegistrationRefused, match="no-such-product"):
        CR.register(_spec(products=["no-such-product"]), registry_path=path)
    assert path.read_bytes() == before


def test_a_known_product_is_accepted(tmp_path):
    path = _seed(tmp_path)
    _, row = CR.register(_spec(products=["widgets"]), registry_path=path)
    assert row["products"] == ["widgets"]


def test_missing_registry_file_refuses_rather_than_creating_one(tmp_path):
    missing = tmp_path / "no-such-file.json"
    with pytest.raises(CR.RegistrationRefused):
        CR.register(_spec(), registry_path=missing)
    assert not missing.exists()


# --- write is atomic (temp file + rename in the same directory) -------------


def test_write_is_a_same_directory_rename(tmp_path, monkeypatch):
    path = _seed(tmp_path)
    seen_names = []
    import tempfile as _tempfile
    real_mkstemp = _tempfile.mkstemp

    def spy(*a, **kw):
        fd, name = real_mkstemp(*a, **kw)
        seen_names.append(name)
        return fd, name

    monkeypatch.setattr(_tempfile, "mkstemp", spy)
    CR.register(_spec(), registry_path=path)
    assert len(seen_names) == 1
    assert Path(seen_names[0]).parent == path.parent


def test_byte_identical_reproduction_when_nothing_changes(tmp_path):
    """The registry's own JSON style round-trips (2-space indent, real UTF-8
    rather than \\uXXXX escapes) — the premise `register`'s atomic write
    relies on to keep an unrelated diff out of a real registration."""
    path = _seed(tmp_path, extra={
        "em-dash": {
            "class": "project", "display": "Em—Dash", "owner": "convivy",
            "home_dir": "<root>/em-dash", "dirs": [], "crew": "em-dash",
            "lifecycle": "active", "products": [], "repos": ["em-dash"],
            "aliases": [], "kb": "local", "task_text_public": False,
            "overnight": False,
        },
    })
    original = json.loads(path.read_text())
    reproduced = json.dumps(original, indent=2, ensure_ascii=False) + "\n"
    assert reproduced == path.read_text()


# --- the deployed-path resolution --------------------------------------------


def test_default_registry_path_matches_projects_pys_own_live_resolution():
    """The command must not guess a second path: it writes exactly where
    `projects._registry_path()` — projects.py's own live resolver — answers
    right now, in-process, without a `--registry` override — the symlink-farm
    checkout and a packaged adopter's copy deploy alike.

    Equality, not identity: `default_registry_path()` calls that resolver
    fresh on every call (see the PROJECTS_JSON test below) rather than
    returning the cached `projects.PROJECTS_JSON` snapshot, so the two Path
    objects are independently constructed and merely have to agree."""
    assert CR.default_registry_path() == projects._registry_path()


def test_default_registry_path_honours_a_freshly_set_PROJECTS_JSON(tmp_path, monkeypatch):
    """`projects.PROJECTS_JSON` is only `_registry_path()`'s result cached at
    whichever moment `projects` first imported in this process — setting the
    env var afterwards never touches that cached attribute. This command must
    not inherit that staleness: Shipwright's adopter install sets
    PROJECTS_JSON once for every job's environment, and the Library is
    adopting the same variable (platform/decisions, 2026-10-02), so a value
    set before this call has to land on the same file both resolve."""
    target = tmp_path / "adopter-projects.json"
    monkeypatch.setenv("PROJECTS_JSON", str(target))
    assert CR.default_registry_path() == target


def test_resolved_registry_path_is_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PROJECTS_JSON", "relative/projects.json")
    resolved = CR.resolved_registry_path()
    assert resolved.is_absolute()
    assert resolved == (tmp_path / "relative" / "projects.json").resolve()


# --- the reader this change touches: bin/co's crew-cockpit derivation -------


def test_crew_cockpit_dirs_reads_the_new_field(tmp_path, monkeypatch):
    path = _seed(tmp_path)
    CR.register(_spec(crew_cockpit=True), registry_path=path)
    CR.register(_spec(slug="other", display="Other", repos=["other"],
                       crew_cockpit=False, kb="local"),
                registry_path=path)
    monkeypatch.setattr(projects, "PROJECTS_JSON", path)
    projects._load_raw.cache_clear()
    try:
        assert projects.crew_cockpit_dirs() == ["recipe-units"]
    finally:
        projects._load_raw.cache_clear()


def test_crew_cockpit_defaults_off_for_a_local_kb_crew(tmp_path):
    path = _seed(tmp_path)
    _, row = CR.register(_spec(kb="local"), registry_path=path)
    assert "crew_cockpit" not in row


# --- field validation ---------------------------------------------------------


@pytest.mark.parametrize("bad_slug", ["Recipe-Units", "-recipe", "recipe units", ""])
def test_bad_slug_is_refused(tmp_path, bad_slug):
    path = _seed(tmp_path)
    before = path.read_bytes()
    with pytest.raises(CR.RegistrationRefused):
        CR.register(_spec(slug=bad_slug, repos=[bad_slug or "x"]), registry_path=path)
    assert path.read_bytes() == before


def test_kb_project_is_refused_when_kb_is_local(tmp_path):
    path = _seed(tmp_path)
    with pytest.raises(CR.RegistrationRefused):
        CR.register(_spec(kb="local", kb_project="recipe-units"), registry_path=path)


def test_unknown_model_tier_is_refused(tmp_path):
    path = _seed(tmp_path)
    with pytest.raises(CR.RegistrationRefused):
        CR.register(_spec(model_tier="sonnett"), registry_path=path)


def test_task_text_public_and_overnight_are_always_written_explicitly(tmp_path):
    path = _seed(tmp_path)
    _, row = CR.register(_spec(), registry_path=path)
    assert row["task_text_public"] is False
    assert row["overnight"] is False


def test_optional_fields_are_omitted_when_not_requested(tmp_path):
    path = _seed(tmp_path)
    _, row = CR.register(_spec(), registry_path=path)
    for absent in ("private", "model_tier", "agent_prefix", "data_local", "note",
                   "created_by"):
        assert absent not in row


# --- created_by: who created the project (issue_01M2QWFR8PCBPANF8Q5P460GHY) --


_PERSON_ID = "person_01ARZ3NDEKTSV4RRFFQ69G5FAV"


def test_created_by_is_written_when_well_formed(tmp_path):
    path = _seed(tmp_path)
    _, row = CR.register(_spec(created_by=_PERSON_ID), registry_path=path)
    assert row["created_by"] == _PERSON_ID
    assert projects.valid_person_id(row["created_by"])


def test_registering_created_by_does_not_touch_meta_products_or_scopes(tmp_path):
    """The hazard comment 2963 names: a writer that iterates the file's
    top-level keys would write a creator onto `_meta`, `_products` or
    `_scopes` too. `register()` only ever sets `data[spec.slug]`, so those
    three must come out byte-identical to what `_seed` wrote."""
    path = _seed(tmp_path)
    before = json.loads(path.read_text())
    CR.register(_spec(created_by=_PERSON_ID), registry_path=path)
    after = json.loads(path.read_text())
    for block in ("_meta", "_products", "_scopes"):
        assert after[block] == before[block]


@pytest.mark.parametrize("bad", [
    "Jane Doe",              # a display name
    "convivy",               # an organisation, what `owner` holds
    "person_tooshort",
    "",
])
def test_created_by_is_refused_when_malformed(tmp_path, bad):
    path = _seed(tmp_path)
    before = path.read_bytes()
    with pytest.raises(CR.RegistrationRefused, match="created_by"):
        CR.register(_spec(created_by=bad), registry_path=path)
    assert path.read_bytes() == before


# --- ADR-0043: a private row must come out internally consistent -------------
#
# `--kb` defaults to "central" (both CrewSpec's own default and the CLI
# argparser's), so `--private` with no other flag would write exactly the
# contradiction `projects.privacy_invariant_errors` and
# `tests/test_projects.py::test_a_private_crew_cannot_also_be_centrally_indexed`
# exist to catch. These refuse before a byte is written, rather than silently
# defaulting `kb` to "local" under `--private` — seeing CrewSpec.kb's module
# docstring on why a silent default was rejected.


def test_a_private_crew_with_default_kb_is_refused(tmp_path):
    path = _seed(tmp_path)
    before = path.read_bytes()
    with pytest.raises(CR.RegistrationRefused, match="kb='central'"):
        CR.register(_spec(private=True), registry_path=path)
    assert path.read_bytes() == before


def test_a_private_crew_with_task_text_public_is_refused(tmp_path):
    path = _seed(tmp_path)
    before = path.read_bytes()
    with pytest.raises(CR.RegistrationRefused, match="task_text_public is true"):
        CR.register(_spec(private=True, kb="local", task_text_public=True),
                     registry_path=path)
    assert path.read_bytes() == before


def test_a_private_crew_with_overnight_true_is_refused(tmp_path):
    path = _seed(tmp_path)
    before = path.read_bytes()
    with pytest.raises(CR.RegistrationRefused, match="overnight is true"):
        CR.register(_spec(private=True, kb="local", overnight=True), registry_path=path)
    assert path.read_bytes() == before


def test_a_private_crew_with_consistent_fields_is_accepted(tmp_path):
    path = _seed(tmp_path)
    _, row = CR.register(_spec(private=True, kb="local"), registry_path=path)
    assert row["private"] is True
    assert row["kb"] == "local"
    assert row["task_text_public"] is False
    assert row["overnight"] is False
    assert projects.privacy_invariant_errors(row) == []


# --- the CLI wiring: `agentctl crew register` --------------------------------


def _cli_args(**overrides) -> argparse.Namespace:
    """The namespace `agentctl`'s own argparser would build for `crew
    register`, every default spelled out so this test cannot pass by
    accident of an attribute neither side set."""
    base = dict(
        slug="recipe-units", display="Recipe Units", repos=None, aliases=[],
        products=[], crew=None, owner="convivy", cls="project", kb="central",
        kb_project=None, home_dir=None, dirs=[], lifecycle="active",
        model_tier=None, task_text_public=False, overnight=None,
        private=False, data_local=False, agent_prefix=None, crew_cockpit=None,
        note=None, registry=None, dry_run=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def test_cmd_crew_register_writes_the_row(agentctl, tmp_path, capsys):
    path = _seed(tmp_path)
    rc = agentctl.cmd_crew_register(_cli_args(registry=str(path)))
    assert rc == 0
    out = capsys.readouterr().out
    assert "registered 'recipe-units'" in out
    assert "Library" in out and "next lookup" in out, (
        "the Library's projects.py re-reads the registry when the file's "
        "(st_mtime_ns, st_size, st_ino) changes, so the success output "
        "must say the new row is picked up on the next lookup")
    data = json.loads(path.read_text())
    assert data["recipe-units"]["display"] == "Recipe Units"
    assert data["recipe-units"]["repos"] == ["recipe-units"]


def test_cmd_crew_register_no_longer_tells_operator_to_restart_the_library(
        agentctl, tmp_path, capsys):
    """issue_01M4PGRW8SSE9GCHT919WKPFHB: the Library's `_snapshot()` stats
    the registry on every lookup and re-reads it when the file changes, so
    a just-registered slug resolves with no restart — the success output
    must not tell the operator to restart or reload the Library."""
    path = _seed(tmp_path)
    rc = agentctl.cmd_crew_register(_cli_args(registry=str(path)))
    assert rc == 0
    out = capsys.readouterr().out
    assert "restart" not in out
    assert "reload" not in out


def test_cmd_crew_register_dry_run_has_no_library_caveat(agentctl, tmp_path, capsys):
    """The caveat is about a row that actually reached the live file; a
    `--dry-run` writes nothing, so there is nothing for the Library to miss."""
    path = _seed(tmp_path)
    rc = agentctl.cmd_crew_register(_cli_args(registry=str(path), dry_run=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Library" not in out


def test_cmd_crew_register_refuses_a_clash_and_exits_nonzero(agentctl, tmp_path, capsys):
    path = _seed(tmp_path)
    before = path.read_bytes()
    rc = agentctl.cmd_crew_register(_cli_args(slug="existing", repos=["existing"],
                                               registry=str(path)))
    assert rc != 0
    err = capsys.readouterr().err
    assert "refused" in err
    assert path.read_bytes() == before


def test_cmd_crew_register_honours_PROJECTS_JSON_with_no_registry_override(
        agentctl, tmp_path, monkeypatch, capsys):
    """No `--registry` flag at all: the command must resolve through
    PROJECTS_JSON, the variable Shipwright's adopter install sets and the
    Library is adopting, and its success output must name the resolved
    (absolute) file it actually wrote so an operator can see it agrees with
    what the Library will read."""
    path = _seed(tmp_path)
    monkeypatch.setenv("PROJECTS_JSON", str(path))
    rc = agentctl.cmd_crew_register(_cli_args())
    assert rc == 0
    out = capsys.readouterr().out
    assert str(path.resolve()) in out
    data = json.loads(path.read_text())
    assert data["recipe-units"]["display"] == "Recipe Units"


def test_cmd_crew_register_refusal_names_the_resolved_registry_path(
        agentctl, tmp_path, monkeypatch, capsys):
    """A refusal raised inside `build_row()` (here, a malformed slug) carries
    no path in its own message — `build_row` never receives one. The command
    must still tell the operator which file was left untouched, so it does
    not have to re-derive PROJECTS_JSON / --registry / projects.py's own
    fallback order by hand to find out."""
    path = _seed(tmp_path)
    monkeypatch.setenv("PROJECTS_JSON", str(path))
    before = path.read_bytes()
    rc = agentctl.cmd_crew_register(_cli_args(slug="Not-A-Valid-Slug"))
    assert rc != 0
    err = capsys.readouterr().err
    assert "refused" in err
    assert str(path.resolve()) in err
    assert path.read_bytes() == before


# --- `created_by`, read from the operator config's `person_id` --------------


def test_cmd_crew_register_writes_created_by_from_operator_config(
        agentctl, tmp_path, monkeypatch, capsys):
    path = _seed(tmp_path)
    config_path = tmp_path / "operator.json"
    config_path.write_text(json.dumps({"person_id": _PERSON_ID}))
    monkeypatch.setenv("CONVIVY_OPERATOR_CONFIG", str(config_path))
    rc = agentctl.cmd_crew_register(_cli_args(registry=str(path)))
    assert rc == 0
    data = json.loads(path.read_text())
    assert data["recipe-units"]["created_by"] == _PERSON_ID


def test_cmd_crew_register_without_person_id_omits_created_by_and_says_why(
        agentctl, tmp_path, monkeypatch, capsys):
    """No `person_id` key at all — the fresh-install case, before the
    Library's bootstrap has created anyone. The registration still succeeds;
    it just records no creator, and says so rather than failing silently."""
    path = _seed(tmp_path)
    config_path = tmp_path / "operator.json"
    config_path.write_text(json.dumps({"roster_crew": "operator-crew"}))
    monkeypatch.setenv("CONVIVY_OPERATOR_CONFIG", str(config_path))
    rc = agentctl.cmd_crew_register(_cli_args(registry=str(path)))
    assert rc == 0
    err = capsys.readouterr().err
    assert "did not record a creator" in err
    assert "person_id" in err
    data = json.loads(path.read_text())
    assert "created_by" not in data["recipe-units"]


def test_cmd_crew_register_with_unreadable_operator_config_still_registers(
        agentctl, tmp_path, monkeypatch, capsys):
    """A missing operator config file entirely — not just a missing key — is
    the same "don't refuse" case: a fresh machine may have no
    `~/.config/convivy/operator.json` at all yet."""
    path = _seed(tmp_path)
    monkeypatch.setenv("CONVIVY_OPERATOR_CONFIG", str(tmp_path / "no-such-file.json"))
    rc = agentctl.cmd_crew_register(_cli_args(registry=str(path)))
    assert rc == 0
    err = capsys.readouterr().err
    assert "did not record a creator" in err
    data = json.loads(path.read_text())
    assert "created_by" not in data["recipe-units"]
