"""Drift guard for the project registry (ARCH-358, config/projects.json).

The registry records the product/project units in one place; today the same facts
also live in scattered consumer lists. Phase 1's contract is that this test fails
red the moment a consumer disagrees with the registry, so the registry stays the
canonical record instead of decaying into a stale doc. The assertions cover only
the clean correspondences — where a consumer keys on the same unit dimension the
registry models. The deliberately-excluded seams (repo-level task-text entries,
KB organizational buckets) are noted at each check.

Marked `rich_registry` at module scope (CQ-546): every test here either reads the
committed file directly (that IS the drift guard's job) or builds its own registry
under its own `tmp_path` and monkeypatches `projects.PROJECTS_JSON` itself, in which
case the marker is inert — the per-test override wins regardless, the same way it
wins over any other default. Either way nothing here needs
`conftest.py`'s `_default_projects_registry`.
"""
import json
import pathlib
from pathlib import Path

import pytest

import app_prefixes
import cost_join
import knowledge
import operator_config
import projects
import vault_plane

pytestmark = pytest.mark.rich_registry



# --- validate_registry: the whole-document check, on a made-up registry ----------
#
# None of these touch the real file — each builds its own tiny document, the way
# `crew_registration`'s own tests do, so the validator's rules are pinned
# independent of whatever CQ-546 moves out of config/projects.json.


def _valid_doc(**overrides) -> dict:
    """A minimal, schema-valid registry document: one project, one product, one
    scope — nothing `validate_registry` should refuse. `overrides` replaces a
    top-level key (usually `"widget"`, to craft a broken unit)."""
    doc = {
        "_meta": {"doc": "test fixture"},
        "_products": {"widgets": {"display": "Widgets", "owner": "test"}},
        "_scopes": {"platform": {"display": "Platform"}},
        "widget": {
            "class": "project", "display": "Widget", "owner": "test",
            "home_dir": "<root>/widget", "dirs": [], "crew": "widget",
            "lifecycle": "active", "products": ["widgets"], "repos": ["widget"],
            "aliases": [], "kb": "local", "task_text_public": False,
            "overnight": False,
        },
    }
    doc.update(overrides)
    return doc


def test_validate_registry_admits_a_valid_document():
    assert projects.validate_registry(_valid_doc()) is None


def test_validate_registry_skips_underscore_keys():
    doc = _valid_doc(_release_names=["anything"])
    assert projects.validate_registry(doc) is None


@pytest.mark.parametrize("field", projects.REQUIRED_FIELDS)
def test_validate_registry_refuses_a_missing_required_field(field):
    unit = {k: v for k, v in _valid_doc()["widget"].items() if k != field}
    with pytest.raises(ValueError, match=f"missing required field {field!r}"):
        projects.validate_registry(_valid_doc(widget=unit))


def test_validate_registry_refuses_a_non_dict_unit():
    with pytest.raises(ValueError, match="must be a JSON object"):
        projects.validate_registry(_valid_doc(widget="not a dict"))


def test_validate_registry_refuses_a_bad_class():
    doc = _valid_doc()
    doc["widget"]["class"] = "nonsense"
    with pytest.raises(ValueError, match="bad class"):
        projects.validate_registry(doc)


def test_validate_registry_refuses_an_empty_display():
    doc = _valid_doc()
    doc["widget"]["display"] = ""
    with pytest.raises(ValueError, match="display must be a non-empty string"):
        projects.validate_registry(doc)


def test_validate_registry_refuses_an_empty_repos_list():
    doc = _valid_doc()
    doc["widget"]["repos"] = []
    with pytest.raises(ValueError, match="repos must be a non-empty list"):
        projects.validate_registry(doc)


def test_validate_registry_refuses_a_non_list_aliases():
    doc = _valid_doc()
    doc["widget"]["aliases"] = "widget-alias"
    with pytest.raises(ValueError, match="aliases must be a list"):
        projects.validate_registry(doc)


def test_validate_registry_refuses_a_bad_kb_value():
    doc = _valid_doc()
    doc["widget"]["kb"] = "everywhere"
    with pytest.raises(ValueError, match="bad kb value"):
        projects.validate_registry(doc)


def test_validate_registry_refuses_central_kb_with_no_kb_project():
    doc = _valid_doc()
    doc["widget"]["kb"] = "central"
    with pytest.raises(ValueError, match="needs kb_project"):
        projects.validate_registry(doc)


def test_validate_registry_refuses_a_missing_home_dir():
    doc = _valid_doc()
    doc["widget"]["home_dir"] = ""
    with pytest.raises(ValueError, match="needs a home_dir"):
        projects.validate_registry(doc)


def test_validate_registry_refuses_a_non_list_dirs():
    doc = _valid_doc()
    doc["widget"]["dirs"] = "<root>/widget/extra"
    with pytest.raises(ValueError, match="dirs must be a list"):
        projects.validate_registry(doc)


def test_validate_registry_refuses_a_bad_lifecycle():
    doc = _valid_doc()
    doc["widget"]["lifecycle"] = "retired"
    with pytest.raises(ValueError, match="bad lifecycle"):
        projects.validate_registry(doc)


def test_validate_registry_refuses_a_non_list_products():
    doc = _valid_doc()
    doc["widget"]["products"] = "widgets"
    with pytest.raises(ValueError, match="products must be a list"):
        projects.validate_registry(doc)


def test_validate_registry_refuses_a_non_string_crew():
    doc = _valid_doc()
    doc["widget"]["crew"] = 42
    with pytest.raises(ValueError, match="crew must be a slug or null"):
        projects.validate_registry(doc)


def test_validate_registry_refuses_a_non_string_tmux_session():
    doc = _valid_doc()
    doc["widget"]["tmux_session"] = 42
    with pytest.raises(ValueError, match="tmux_session must be a slug or null"):
        projects.validate_registry(doc)


def test_validate_registry_admits_a_string_tmux_session():
    doc = _valid_doc()
    doc["widget"]["tmux_session"] = "widget-live"
    assert projects.validate_registry(doc) is None


def test_validate_registry_admits_a_null_tmux_session():
    doc = _valid_doc()
    doc["widget"]["tmux_session"] = None
    assert projects.validate_registry(doc) is None


def test_validate_registry_refuses_a_redundant_false_crew_cockpit():
    doc = _valid_doc()
    doc["widget"]["crew_cockpit"] = False
    with pytest.raises(ValueError, match="crew_cockpit must be true or absent"):
        projects.validate_registry(doc)


def test_validate_registry_refuses_an_empty_agent_prefix():
    doc = _valid_doc()
    doc["widget"]["agent_prefix"] = ""
    with pytest.raises(ValueError, match="agent_prefix must be a non-empty string"):
        projects.validate_registry(doc)


def test_validate_registry_refuses_an_agent_prefix_without_a_trailing_dash():
    doc = _valid_doc()
    doc["widget"]["agent_prefix"] = "widget"
    with pytest.raises(ValueError, match="agent_prefix must be a non-empty string"):
        projects.validate_registry(doc)


def test_validate_registry_admits_a_list_shaped_agent_prefix():
    """A crew with a live prefix and a retired one it still has to authenticate
    old review stamps under (widget: `["widget-", "wdgt-"]`) stores both as a
    list, not a single string."""
    doc = _valid_doc()
    doc["widget"]["agent_prefix"] = ["widget-", "wdgt-"]
    assert projects.validate_registry(doc) is None


def test_validate_registry_refuses_a_list_shaped_agent_prefix_with_a_bad_entry():
    doc = _valid_doc()
    doc["widget"]["agent_prefix"] = ["widget-", "wdgt"]
    with pytest.raises(ValueError, match="agent_prefix must be a non-empty string"):
        projects.validate_registry(doc)


def test_validate_registry_refuses_a_redundant_false_postgres_socket():
    doc = _valid_doc()
    doc["widget"]["postgres_socket"] = False
    with pytest.raises(ValueError, match="postgres_socket must be true or absent"):
        projects.validate_registry(doc)


def test_validate_registry_refuses_an_empty_cost_fold_aliases():
    doc = _valid_doc()
    doc["widget"]["cost_fold_aliases"] = []
    with pytest.raises(ValueError, match="cost_fold_aliases must be a non-empty list"):
        projects.validate_registry(doc)


def test_validate_registry_refuses_a_non_string_cost_fold_alias():
    doc = _valid_doc()
    doc["widget"]["cost_fold_aliases"] = [123]
    with pytest.raises(ValueError, match="cost_fold_aliases entries must be non-empty strings"):
        projects.validate_registry(doc)


def test_validate_registry_refuses_a_malformed_created_by():
    doc = _valid_doc()
    doc["widget"]["created_by"] = "not-a-person-id"
    with pytest.raises(ValueError, match="is not a Library person id"):
        projects.validate_registry(doc)


def test_validate_registry_refuses_a_non_int_velocity_order():
    doc = _valid_doc()
    doc["widget"]["velocity_order"] = "first"
    with pytest.raises(ValueError, match="velocity_order must be an int"):
        projects.validate_registry(doc)


def test_validate_registry_refuses_a_bool_velocity_order():
    """`isinstance(True, int)` is True in Python — the bool-exclusion check
    `test_schema_wellformed` carried has to survive the move into code."""
    doc = _valid_doc()
    doc["widget"]["velocity_order"] = True
    with pytest.raises(ValueError, match="velocity_order must be an int"):
        projects.validate_registry(doc)


def test_validate_registry_refuses_a_non_string_adr_kb_project():
    doc = _valid_doc()
    doc["widget"]["adr_kb_projects"] = [123]
    with pytest.raises(ValueError, match="adr_kb_projects must be a list of non-empty strings"):
        projects.validate_registry(doc)


def test_validate_registry_refuses_a_redundant_false_github_owner_private():
    doc = _valid_doc()
    doc["widget"]["github_owner_private"] = False
    with pytest.raises(ValueError, match="github_owner_private must be true or absent"):
        projects.validate_registry(doc)


def test_validate_registry_refuses_a_redundant_false_walled_off():
    doc = _valid_doc()
    doc["widget"]["walled_off"] = False
    with pytest.raises(ValueError, match="walled_off must be true or absent"):
        projects.validate_registry(doc)


def test_validate_registry_refuses_a_unit_carrying_both_walled_off_and_private():
    """A crew is one or the other (platform/decisions/0100): a black box
    (`private: true`) launches with `--strict-mcp-config` and no connectors,
    which would break the walled-off crew's own account connectors."""
    doc = _valid_doc()
    doc["widget"]["walled_off"] = True
    doc["widget"]["private"] = True
    with pytest.raises(ValueError, match="carries both walled_off and private"):
        projects.validate_registry(doc)


def test_validate_registry_refuses_a_private_crew_indexed_centrally():
    """The black-box rule (platform/decisions/0043), delegated to
    `privacy_invariant_errors` rather than re-checked here — this pins that
    the validator actually calls it, not what it checks."""
    doc = _valid_doc()
    doc["widget"]["private"] = True
    doc["widget"]["kb"] = "central"
    doc["widget"]["kb_project"] = "widget"
    with pytest.raises(ValueError, match="is private but"):
        projects.validate_registry(doc)


def test_validate_registry_names_every_problem_not_just_the_first():
    doc = _valid_doc()
    doc["widget"]["class"] = "nonsense"
    doc["widget"]["lifecycle"] = "retired"
    with pytest.raises(ValueError) as exc_info:
        projects.validate_registry(doc)
    assert "bad class" in str(exc_info.value)
    assert "bad lifecycle" in str(exc_info.value)


@pytest.mark.parametrize(
    "flag", ("co_self_contained", "co_generic_refresh", "company_workspace",
              "co_writing_guide"))
def test_validate_registry_refuses_a_redundant_false_co_flag(flag):
    doc = _valid_doc()
    doc["widget"][flag] = False
    with pytest.raises(ValueError, match=f"{flag} must be true or absent"):
        projects.validate_registry(doc)


def test_validate_registry_refuses_a_co_cockpit_gate_without_crew_cockpit():
    doc = _valid_doc()
    doc["widget"]["co_cockpit_gate"] = "basename"
    with pytest.raises(ValueError, match="sets co_cockpit_gate but is not crew_cockpit"):
        projects.validate_registry(doc)


def test_validate_registry_refuses_a_bad_co_cockpit_gate():
    doc = _valid_doc()
    doc["widget"]["crew_cockpit"] = True
    doc["widget"]["co_cockpit_gate"] = "nonsense"
    with pytest.raises(ValueError, match="co_cockpit_gate .* must be one of"):
        projects.validate_registry(doc)


def test_validate_registry_refuses_a_bad_co_style_id():
    doc = _valid_doc()
    doc["widget"]["co_style_id"] = "not a valid style id"
    with pytest.raises(ValueError, match="co_style_id .* must match"):
        projects.validate_registry(doc)


def test_validate_registry_refuses_an_empty_co_label():
    doc = _valid_doc()
    doc["widget"]["co_label"] = ""
    with pytest.raises(ValueError, match="co_label must be a non-empty string"):
        projects.validate_registry(doc)


@pytest.mark.parametrize("bad_value", ("ops/evil", "../evil", "", "Product-Manager"))
def test_validate_registry_refuses_a_bad_co_default_agent(bad_value):
    doc = _valid_doc()
    doc["widget"]["co_default_agent"] = bad_value
    with pytest.raises(ValueError, match="co_default_agent .* must be"):
        projects.validate_registry(doc)


def test_validate_registry_admits_a_well_formed_co_default_agent():
    doc = _valid_doc()
    doc["widget"]["co_default_agent"] = "product-manager"
    assert projects.validate_registry(doc) is None


@pytest.mark.parametrize("color_field", ("co_accent", "co_fg"))
def test_validate_registry_refuses_a_bad_color_field(color_field):
    doc = _valid_doc()
    doc["widget"][color_field] = "blue"
    with pytest.raises(ValueError, match=f"{color_field} .* must be a '#rrggbb'-shaped string"):
        projects.validate_registry(doc)


def test_validate_registry_refuses_a_non_list_profiled_dirs():
    doc = _valid_doc()
    doc["widget"]["profiled_dirs"] = "<root>/widget/extra"
    with pytest.raises(ValueError, match="profiled_dirs must be a list of non-empty strings"):
        projects.validate_registry(doc)


def test_validate_registry_refuses_a_redundant_false_local_agents():
    doc = _valid_doc()
    doc["widget"]["local_agents"] = False
    with pytest.raises(ValueError, match="local_agents must be true or absent"):
        projects.validate_registry(doc)


def test_validate_registry_refuses_a_typecheck_entry_for_an_unknown_repo():
    doc = _valid_doc()
    doc["widget"]["typecheck"] = {"not-a-repo": {"dir": ".", "argv": ["make"]}}
    with pytest.raises(ValueError, match="typecheck names 'not-a-repo', not one of its own repos"):
        projects.validate_registry(doc)


def test_validate_registry_refuses_a_typecheck_argv_shell_string():
    doc = _valid_doc()
    doc["widget"]["typecheck"] = {"widget": {"dir": ".", "argv": "make typecheck"}}
    with pytest.raises(ValueError, match="argv must be a non-empty list of strings"):
        projects.validate_registry(doc)


def test_validate_registry_refuses_a_default_profile_entry_for_an_unknown_repo():
    doc = _valid_doc()
    doc["widget"]["default_profile"] = {"not-a-repo": "overnight"}
    with pytest.raises(ValueError, match="default_profile names 'not-a-repo', not one of its own repos"):
        projects.validate_registry(doc)


def test_validate_registry_refuses_an_empty_default_profile_name():
    doc = _valid_doc()
    doc["widget"]["default_profile"] = {"widget": ""}
    with pytest.raises(ValueError, match="default_profile needs a non-empty profile name"):
        projects.validate_registry(doc)


def _write_registry(tmp_path, monkeypatch, raw):
    """Point the loader at a crafted registry, the way `legal_kb_registry` does,
    and clear the load cache so the write takes effect immediately."""
    path = tmp_path / "projects.json"
    path.write_text(json.dumps(raw))
    monkeypatch.setattr(projects, "PROJECTS_JSON", path)
    projects._load_raw.cache_clear()
    return path


def _crafted_unit(repos, **fields):
    unit = {
        "class": "project", "display": "Crafted", "owner": "convivy",
        "home_dir": "<root>/crafted", "dirs": [], "crew": None,
        "lifecycle": "active", "products": [], "repos": list(repos),
        "aliases": [], "kb": "local",
    }
    unit.update(fields)
    return unit


def test_local_agent_repos_is_every_repo_of_a_unit_with_local_agents_true(
        tmp_path, monkeypatch):
    """`bin/agentctl`'s `_local_agent_repos()` reads `projects.local_agent_repos()`
    at use rather than a literal set of repos. A crafted registry with one unit
    carrying `local_agents: true` and one without proves the accessor reads the
    field rather than returning every unit's repos."""
    raw = {
        "alpha": _crafted_unit(["alpha-repo", "alpha-tools"], local_agents=True),
        "beta-studio": _crafted_unit(["beta-repo"]),
    }
    _write_registry(tmp_path, monkeypatch, raw)
    assert projects.local_agent_repos() == frozenset({"alpha-repo", "alpha-tools"})
    projects._load_raw.cache_clear()


def test_local_agent_repos_is_not_the_same_set_as_private_slugs():
    """The registry's `private` field looked like it might already answer the
    same question `local_agents` does — this pins the real, measured
    difference: a private crew with no dispatched agents at all carries no
    `local_agents` entry, so the private-repo set is strictly larger than
    `local_agent_repos()`."""
    private_repos = {repo for slug in projects.private_slugs()
                     for repo in projects.load()[slug]["repos"]}
    assert projects.local_agent_repos() < private_repos


def test_walled_off_slugs_reads_the_field_not_private(tmp_path, monkeypatch):
    """`walled_off_slugs()` mirrors `private_slugs()`'s own `.get(...) is True`
    read, but on its own field — a `private: true` unit must not appear in it."""
    raw = {
        "parlor": _crafted_unit(["parlor"], walled_off=True),
        "ridge": _crafted_unit(["ridge"], private=True),
        "finch": _crafted_unit(["finch"]),
    }
    _write_registry(tmp_path, monkeypatch, raw)
    assert projects.walled_off_slugs() == {"parlor"}
    projects._load_raw.cache_clear()


def test_typecheck_gates_is_only_the_repos_with_a_recorded_gate(tmp_path, monkeypatch):
    """`bin/agentctl`'s `_typecheck_gates()` reads `projects.typecheck_gates()` at
    use rather than a literal table of repos. A crafted registry with one unit
    carrying a `typecheck` entry and one without proves the accessor reads that
    field rather than returning every unit's repos."""
    raw = {
        "alpha": _crafted_unit(
            ["alpha-repo"],
            typecheck={"alpha-repo": {"dir": ".", "argv": ["make", "typecheck"]}}),
        "beta-studio": _crafted_unit(["beta-repo"]),
    }
    _write_registry(tmp_path, monkeypatch, raw)
    assert projects.typecheck_gates() == {"alpha-repo": (".", ["make", "typecheck"])}
    projects._load_raw.cache_clear()


def test_default_profile_overrides_is_only_the_repos_with_an_override(
        tmp_path, monkeypatch):
    """`bin/agentctl`'s `_default_profile_by_repo()` reads
    `projects.default_profile_overrides()` at use rather than a literal dict of
    repos. A crafted registry with one unit carrying a `default_profile` entry
    and one without proves the accessor reads that field rather than returning
    every unit's repos."""
    raw = {
        "example-crew": _crafted_unit(
            ["example-repo"], default_profile={"example-repo": "example-profile"}),
        "beta-studio": _crafted_unit(["beta-repo"]),
    }
    _write_registry(tmp_path, monkeypatch, raw)
    assert projects.default_profile_overrides() == {"example-repo": "example-profile"}
    projects._load_raw.cache_clear()


def test_created_by_backfill_did_not_touch_meta_products_or_scopes():
    """The hazard comment 2963 names: a backfill that iterates the file's
    top-level keys would write a creator onto `_meta`, `_products` or
    `_scopes`, corrupting the schema metadata rather than failing visibly.
    None of the three carries a `created_by` key."""
    raw = json.loads(Path(projects.PROJECTS_JSON).read_text())
    assert "created_by" not in raw["_meta"]
    for slug, product in raw["_products"].items():
        assert "created_by" not in product, f"_products.{slug} must not carry created_by"
    for slug, scope in raw["_scopes"].items():
        if slug.startswith("_"):
            continue
        assert "created_by" not in scope, f"_scopes.{slug} must not carry created_by"


def test_cost_fold_map_is_empty_by_default(crafted_registry):
    """A unit with no `cost_fold_aliases` contributes nothing — the
    conservative default `cost_join._canonical_product` relies on to leave an
    ordinary project's own slug passing through unchanged."""
    crafted_registry("<root>/evil")
    assert projects.cost_fold_map() == {}


def test_cost_fold_map_names_only_the_flagged_aliases(crafted_registry):
    path = crafted_registry("<root>/evil")
    data = json.loads(path.read_text())
    data["evil"]["display"] = "Evil"
    data["evil"]["cost_fold_aliases"] = ["evil", "evil-legacy-name"]
    path.write_text(json.dumps(data))
    projects._load_raw.cache_clear()
    assert projects.cost_fold_map() == {"evil": "Evil", "evil-legacy-name": "Evil"}


def test_postgres_socket_repos_is_conservative_by_default(crafted_registry):
    """A project registered with no `postgres_socket` field gets no Postgres-
    socket sandbox grant — the conservative default `run_sandbox.settings()`
    relies on, since the field opens a new sandbox exception."""
    crafted_registry("<root>/evil")
    assert projects.postgres_socket_repos() == []


def test_postgres_socket_repos_names_a_flagged_project(crafted_registry):
    path = crafted_registry("<root>/evil")
    data = json.loads(path.read_text())
    data["evil"]["postgres_socket"] = True
    path.write_text(json.dumps(data))
    projects._load_raw.cache_clear()
    assert projects.postgres_socket_repos() == ["evil"]


def test_crew_cockpit_dirs_matches_the_field(crafted_registry):
    """`crew_cockpit_dirs()` (issue_01M3YN6857H5J541G73Y9P3KA1) names exactly
    the active crews whose row sets `crew_cockpit: true`, by basename — the
    same set bin/co's own crew-cockpit branch matches per-directory through
    `projects.cockpit_gate`/`co_style`, with no list of its own to append
    this one to."""
    path = crafted_registry("<root>/evil")
    data = json.loads(path.read_text())
    data["evil"]["crew_cockpit"] = True
    data["sunset-cockpit"] = dict(data["evil"])
    data["sunset-cockpit"].update(home_dir="<root>/sunset-cockpit", lifecycle="sunset",
                                   repos=["sunset-cockpit"])
    path.write_text(json.dumps(data))
    projects._load_raw.cache_clear()
    assert projects.crew_cockpit_dirs() == ["evil"]


def test_co_style_is_none_for_a_unit_with_no_co_accent(crafted_registry):
    """Absent co_accent is the conservative answer: bin/co applies no styling at
    all, rather than guessing a default."""
    crafted_registry("<root>/evil")
    assert projects.co_style("evil") is None
    assert projects.co_style("no-such-slug") is None


def test_co_style_defaults_style_id_to_slug_and_fg_to_white(crafted_registry):
    path = crafted_registry("<root>/evil")
    data = json.loads(path.read_text())
    data["evil"]["co_accent"] = "#112233"
    path.write_text(json.dumps(data))
    projects._load_raw.cache_clear()
    assert projects.co_style("evil") == ("evil", "#112233", None, "#ffffff")


def test_co_style_honours_explicit_style_id_label_and_fg(crafted_registry):
    path = crafted_registry("<root>/evil")
    data = json.loads(path.read_text())
    data["evil"].update(co_style_id="villain", co_accent="#112233",
                         co_label="Evil Co", co_fg="#000000")
    path.write_text(json.dumps(data))
    projects._load_raw.cache_clear()
    assert projects.co_style("evil") == ("villain", "#112233", "Evil Co", "#000000")


def test_co_style_by_basename_matches_the_active_units_home_basename(crafted_registry):
    path = crafted_registry("<root>/nested/evil-home")
    data = json.loads(path.read_text())
    data["evil"]["co_accent"] = "#445566"
    path.write_text(json.dumps(data))
    projects._load_raw.cache_clear()
    assert projects.co_style_by_basename("evil-home") == ("evil", "#445566", None, "#ffffff")
    assert projects.co_style_by_basename("no-such-basename") is None


def test_co_style_resolves_a_secondary_repo_by_the_units_repos_list(crafted_registry):
    """A caller holding only a basename (bin/co's two raw-basename callers,
    `_co_apply_product_style`) may be looking at a secondary checkout of a
    unit's repo rather than that unit's own slug — a real product's own
    secondary repo on origin/main, which a hardcoded case gave that product's
    own colour explicitly before the registry cutover dropped it (round 2,
    reviewer, PR #991). style_id still defaults to the RESOLVED unit's own
    slug, not the name this was called with, matching what a direct
    `co_style("evil")` call gives."""
    path = crafted_registry("<root>/evil")
    data = json.loads(path.read_text())
    data["evil"]["co_accent"] = "#112233"
    data["evil"]["repos"] = ["evil", "evil-sandbox-tools"]
    path.write_text(json.dumps(data))
    projects._load_raw.cache_clear()
    assert projects.co_style("evil-sandbox-tools") == ("evil", "#112233", None, "#ffffff")
    assert projects.co_style("no-such-repo") is None


def test_co_style_does_not_resolve_a_sunset_units_secondary_repo(crafted_registry):
    """Matches `co_style_by_basename`'s own active-only filter: a retired
    unit's old secondary-repo name must not resurrect its styling."""
    path = crafted_registry("<root>/evil")
    data = json.loads(path.read_text())
    data["evil"].update(co_accent="#112233", repos=["evil", "evil-sandbox-tools"],
                         lifecycle="sunset")
    path.write_text(json.dumps(data))
    projects._load_raw.cache_clear()
    assert projects.co_style("evil-sandbox-tools") is None


def test_co_style_never_resolves_knowledge_or_dev_setup_through_a_repos_list(crafted_registry):
    """The real instance the general repos-list fallback above must not reopen:
    `library`'s own checked-in row lists `knowledge` under `repos` for the
    `kb_scope_exceptions` question (which profile a standalone checkout
    reads), not as a styling identity. Reproduced here on a made-up unit
    rather than asserting against the checked-in registry directly, so the
    test does not itself need to name `library`. Caught for real by
    tests/bash/test_co_session_name.sh's pre-existing `knowledge` case: a
    standalone `~/Convivy/knowledge` checkout was relabelled "Evil" instead
    of keeping its own basename as its session name."""
    path = crafted_registry("<root>/evil")
    data = json.loads(path.read_text())
    data["evil"].update(co_accent="#112233", repos=["evil", "knowledge", "dev-setup"])
    path.write_text(json.dumps(data))
    projects._load_raw.cache_clear()
    assert projects.co_style("knowledge") is None
    assert projects.co_style("dev-setup") is None


def test_self_contained_crew_dirs_matches_the_field(crafted_registry):
    path = crafted_registry("<root>/evil")
    data = json.loads(path.read_text())
    data["evil"]["co_self_contained"] = True
    data["sunset-self-contained"] = dict(data["evil"])
    data["sunset-self-contained"].update(home_dir="<root>/sunset-self-contained",
                                          lifecycle="sunset", repos=["sunset-self-contained"])
    path.write_text(json.dumps(data))
    projects._load_raw.cache_clear()
    assert projects.self_contained_crew_dirs() == ["evil"]


def test_cockpit_gate_defaults_to_path(crafted_registry):
    path = crafted_registry("<root>/evil")
    data = json.loads(path.read_text())
    data["evil"]["crew_cockpit"] = True
    path.write_text(json.dumps(data))
    projects._load_raw.cache_clear()
    assert projects.cockpit_gate("evil") == "path"
    assert projects.cockpit_gate("no-such-slug") == "path"


def test_cockpit_gate_honours_an_explicit_basename_gate(crafted_registry):
    path = crafted_registry("<root>/evil")
    data = json.loads(path.read_text())
    data["evil"].update(crew_cockpit=True, co_cockpit_gate="basename")
    path.write_text(json.dumps(data))
    projects._load_raw.cache_clear()
    assert projects.cockpit_gate("evil") == "basename"


def test_generic_refresh_names_collects_slug_aliases_and_repos(crafted_registry):
    path = crafted_registry("<root>/evil")
    data = json.loads(path.read_text())
    data["evil"].update(co_generic_refresh=True, aliases=["evil-alias"],
                         repos=["evil", "evil-repo"])
    path.write_text(json.dumps(data))
    projects._load_raw.cache_clear()
    assert projects.generic_refresh_names() == {"evil", "evil-alias", "evil-repo"}


def test_company_workspace_slug_is_none_when_unset(crafted_registry):
    crafted_registry("<root>/evil")
    assert projects.company_workspace_slug() is None


def test_at_most_one_active_unit_is_flagged_company_workspace():
    """bin/co's company-level commercial branch resolves ONE directory this way
    — a second flagged unit would make `company_workspace_slug()` silently pick
    whichever the dict happens to iterate first."""
    flagged = [s for s, u in projects.load().items()
               if u.get("lifecycle") == "active" and u.get("company_workspace") is True]
    assert len(flagged) <= 1, f"more than one unit flagged company_workspace: {flagged}"


def test_company_workspace_slug_finds_the_flagged_unit(crafted_registry):
    path = crafted_registry("<root>/evil")
    data = json.loads(path.read_text())
    data["evil"]["company_workspace"] = True
    path.write_text(json.dumps(data))
    projects._load_raw.cache_clear()
    assert projects.company_workspace_slug() == "evil"


def test_co_default_agent_is_none_when_unset(crafted_registry):
    crafted_registry("<root>/evil")
    assert projects.co_default_agent("evil") is None


def test_co_default_agent_reads_the_registry_field(crafted_registry):
    path = crafted_registry("<root>/evil")
    data = json.loads(path.read_text())
    data["evil"]["co_default_agent"] = "product-manager"
    path.write_text(json.dumps(data))
    projects._load_raw.cache_clear()
    assert projects.co_default_agent("evil") == "product-manager"


def test_profiled_dirs_resolves_every_active_units_entries(crafted_registry):
    path = crafted_registry("<root>/evil")
    data = json.loads(path.read_text())
    data["evil"]["profiled_dirs"] = ["<root>/evil-satellite"]
    path.write_text(json.dumps(data))
    projects._load_raw.cache_clear()
    root = projects.crews_root().resolve()
    assert projects.profiled_dirs() == [root / "evil-satellite"]


# --- kb_scope_exceptions: the registry's record of a deliberate narrowing --------
#
# issue_01M3V1J89CG19ZGD3JY4WFY7V0: `EXPLICIT_PROJECT_BY_NAME`
# (lib/agent-platform/projects.py) narrows eight repos below their project's own
# `kb_project`, each on a security review or a structural finding recorded in
# epic comment 4425 (issue_01M2NYBBY0FXCDHWWD8XBR9RR3) or, for harbor-updates,
# this issue's own comment 4432. Before this field existed, nothing in the
# registry said so, and a later change deriving scope from the registry would
# have silently widened each one back.


def test_kb_scope_exceptions_wellformed():
    """Schema for the optional `kb_scope_exceptions` field: a dict keyed by one of
    the unit's own `repos`, each value a `kb_project` and a one-sentence `reason`.
    `kb_project` must actually differ from the unit's own — the field exists to
    record a deliberate narrowing, so a value matching the unit's own kb_project
    is a no-op entry rather than an exception."""
    for slug, u in projects.load().items():
        exceptions = u.get("kb_scope_exceptions")
        if exceptions is None:
            continue
        assert isinstance(exceptions, dict), f"{slug} kb_scope_exceptions must be a dict"
        for repo, exc in exceptions.items():
            assert repo in u.get("repos", []), (
                f"{slug} kb_scope_exceptions names {repo!r}, not one of its own repos")
            assert isinstance(exc, dict), (
                f"{slug}.{repo} kb_scope_exceptions entry must be a dict")
            kb_project = exc.get("kb_project")
            assert isinstance(kb_project, str) and kb_project, (
                f"{slug}.{repo} kb_scope_exceptions needs a non-empty kb_project")
            reason = exc.get("reason")
            assert isinstance(reason, str) and len(reason) > 20, (
                f"{slug}.{repo} kb_scope_exceptions needs a real reason, not {reason!r}")
            assert kb_project != u.get("kb_project"), (
                f"{slug}.{repo} kb_scope_exceptions records {kb_project!r}, the same as "
                f"{slug}'s own kb_project — this field is for a deliberate narrowing, "
                f"not a no-op entry")


# --- channel_prefixes: signal_client's channel -> product mapping ---------------
#
# `signal_client.product_for_channel` used to carry its own hardcoded prefix
# tuple plus a separate special case; both are now this one optional per-unit
# field, so a project's Slack-channel prefix lives beside the rest of its
# registration rather than in a second list a reviewer has to remember to keep
# in sync.


def test_channel_prefixes_wellformed():
    """Schema for the optional `channel_prefixes` field: a non-empty list of
    non-empty, already-lowercase strings. Lowercase because
    `product_for_channel` lowercases the channel label it matches against but
    never the registry value, so an uppercase entry would silently never
    match."""
    for slug, u in projects.load().items():
        prefixes = u.get("channel_prefixes")
        if prefixes is None:
            continue
        assert isinstance(prefixes, list) and prefixes, (
            f"{slug} channel_prefixes must be a non-empty list")
        for p in prefixes:
            assert isinstance(p, str) and p and p == p.lower(), (
                f"{slug} channel_prefixes entry {p!r} must be a non-empty, "
                f"lowercase string")


def test_signal_product_wellformed():
    """Schema for the optional `signal_product` field: when a unit sets it,
    `projects.signal_product` must accept it rather than raise — this is the
    same shape check that function applies at read time, run here against
    every unit in the committed registry so a malformed override fails CI
    rather than only a caller's first read."""
    for slug, u in projects.load().items():
        if u.get("signal_product") is None:
            continue
        assert projects.signal_product(slug) == u["signal_product"]



# --- kb_project_by_repo: the per-unit field EXPLICIT_PROJECT_BY_NAME is built from ---
#
# `EXPLICIT_PROJECT_BY_NAME` (lib/agent-platform/projects.py) used to be a
# hand-maintained module-level table; `_explicit_project_by_name` now builds it
# fresh from every unit's own `kb_project_by_repo` and `kb_scope_exceptions`
# fields, so a new alias, satellite repo or restricted-profile case is a
# config/projects.json edit rather than a code PR.


def test_kb_project_by_repo_wellformed():
    """Schema for the optional `kb_project_by_repo` field: a dict of
    {name: {kb_project, reason}}, the shape `kb_scope_exceptions` already
    uses — `kb_project` a non-empty string, `reason` a real one. The `reason`
    requirement is what restores the per-entry justification the hand-maintained
    `EXPLICIT_PROJECT_BY_NAME` literal carried in comments before this field
    replaced it (security review, agent-platform#988 round 2)."""
    for slug, u in projects.load().items():
        table = u.get("kb_project_by_repo")
        if table is None:
            continue
        assert isinstance(table, dict), f"{slug} kb_project_by_repo must be a dict"
        for name, entry in table.items():
            assert isinstance(name, str) and name, (
                f"{slug} kb_project_by_repo has a non-string or empty key {name!r}")
            assert isinstance(entry, dict), (
                f"{slug}.{name} kb_project_by_repo entry must be a dict")
            kb_project = entry.get("kb_project")
            assert isinstance(kb_project, str) and kb_project, (
                f"{slug}.{name} kb_project_by_repo needs a non-empty kb_project")
            reason = entry.get("reason")
            assert isinstance(reason, str) and len(reason) > 20, (
                f"{slug}.{name} kb_project_by_repo needs a real reason, not {reason!r}")


# --- the audience-boundary check: a kb_project_by_repo/kb_scope_exceptions ------
# value must name a KB project the registry actually declares (security review,
# agent-platform#988 round 2). Before this check, a hand-edited row could widen
# a dispatch into an undeclared project's scope with nothing to catch it —
# exactly the leak class PRs #435, #508, #552 and #358 each caught by hand, under
# review, before kb_project_by_repo replaced the reviewed Python literal.
#
# Round 2 also let a kb_project_by_repo entry whose value equalled its own key
# through with no other declaration, meant for a kb: local unit naming its own
# repo's slice. Round 3 (security-reviewer, reviewer) found three gaps in that
# version of the check, all closed by tightening
# `_declared_kb_targets`/`_validate_kb_project_targets` rather than by
# widening the exception: a self-keyed entry was accepted with
# no check that the key belonged to the declaring unit at all; a `kb: local`
# unit's stray `kb_project` field counted as a declaration the way a
# central/split unit's does; and an empty `kb_scope_exceptions` target counted
# as declared because it was in the declared set by construction. The three
# tests below are the regression coverage for each.


def _minimal_unit(**overrides):
    unit = {
        "class": "project", "display": "Evil", "owner": "convivy",
        "home_dir": "<root>/evil", "dirs": [], "crew": "evil",
        "lifecycle": "active", "products": [], "repos": ["evil"],
        "aliases": [], "kb": "local",
    }
    unit.update(overrides)
    return unit


def test_an_undeclared_kb_project_by_repo_target_is_refused(monkeypatch, tmp_path):
    raw = {"evil": _minimal_unit(kb_project_by_repo={
        "other-repo": {"kb_project": "no-such-project", "reason": "made up for the test"}})}
    path = tmp_path / "projects.json"
    path.write_text(json.dumps(raw))
    monkeypatch.setattr(projects, "PROJECTS_JSON", path)
    projects._load_raw.cache_clear()
    with pytest.raises(ValueError, match="no-such-project.*no central/split unit"):
        projects.load()
    projects._load_raw.cache_clear()


def test_a_self_keyed_kb_project_by_repo_target_still_needs_a_declaration(
        monkeypatch, tmp_path):
    """Round 2's self-key exception (`kb_project == name`) was scoped to the
    entry, not to the declaring unit — a unit with no claim at all on `ghost`
    could still declare `{"ghost": {"kb_project": "ghost", ...}}` and pass,
    because the check never looked at who was declaring it. This row is
    exactly that shape: `unit-b` owns no repo or alias named `ghost`, and
    nothing else in the registry declares `ghost` a KB project either."""
    raw = {
        "unit-a": _minimal_unit(home_dir="<root>/unit-a", crew="unit-a", repos=["unit-a"]),
        "unit-b": _minimal_unit(home_dir="<root>/unit-b", crew="unit-b", repos=["unit-b"],
                                 kb_project_by_repo={
                                     "ghost": {"kb_project": "ghost",
                                               "reason": "made up for the test"}}),
    }
    path = tmp_path / "projects.json"
    path.write_text(json.dumps(raw))
    monkeypatch.setattr(projects, "PROJECTS_JSON", path)
    projects._load_raw.cache_clear()
    with pytest.raises(ValueError, match="ghost.*no central/split unit's own kb_project"):
        projects.load()
    projects._load_raw.cache_clear()


def test_a_kb_project_by_repo_target_naming_a_local_units_stray_kb_project_is_refused(
        monkeypatch, tmp_path):
    """`_declared_kb_targets` must apply `kb_slugs()`'s own `kb in ("central",
    "split")` filter before adding a unit's `kb_project` — a `kb: local` unit
    carrying a `kb_project` field (schema-valid, since that field is only
    required, not forbidden, outside central/split) must not make that value
    a legal target for some OTHER unit's `kb_project_by_repo`."""
    raw = {
        "ghost-unit": _minimal_unit(home_dir="<root>/ghost-unit", crew="ghost-unit",
                                     repos=["ghost-unit"], kb_project="ghost-kb"),
        "other-unit": _minimal_unit(home_dir="<root>/other-unit", crew="other-unit",
                                     repos=["other-unit"], kb_project_by_repo={
                                         "other-unit": {"kb_project": "ghost-kb",
                                                         "reason": "made up for the test"}}),
    }
    path = tmp_path / "projects.json"
    path.write_text(json.dumps(raw))
    monkeypatch.setattr(projects, "PROJECTS_JSON", path)
    projects._load_raw.cache_clear()
    with pytest.raises(ValueError, match="ghost-kb.*no central/split unit's own kb_project"):
        projects.load()
    projects._load_raw.cache_clear()


def test_an_empty_kb_scope_exceptions_target_is_refused(monkeypatch, tmp_path):
    """`_declared_kb_targets` must require a `kb_scope_exceptions` entry's own
    `kb_project` to be non-empty before adding it to the declared set — an
    empty string satisfied `isinstance(x, str)` and got added from the very
    entry being validated, so the membership check it fed never had a chance
    to refuse it."""
    raw = {"unit-c": _minimal_unit(home_dir="<root>/unit-c", crew="unit-c",
                                    repos=["unit-c"], kb_scope_exceptions={
                                        "unit-c": {"kb_project": "",
                                                    "reason": "made up for the test"}})}
    path = tmp_path / "projects.json"
    path.write_text(json.dumps(raw))
    monkeypatch.setattr(projects, "PROJECTS_JSON", path)
    projects._load_raw.cache_clear()
    with pytest.raises(ValueError, match="kb_scope_exceptions.*needs a non-empty kb_project"):
        projects.load()
    projects._load_raw.cache_clear()


def test_a_kb_scope_exceptions_target_declared_only_by_another_exception_is_refused(
        monkeypatch, tmp_path):
    """Round 3's `_declared_kb_targets` folded every unit's `kb_scope_exceptions`
    value into the declared set before this check ran, so a `kb_scope_exceptions`
    target was always a member of the set it had just contributed to — any name,
    real or fictional, passed. `unit-d` declares `ghost-scope` only through its own
    `kb_scope_exceptions` entry, with no central/split unit's `kb_project` and no
    `_kb_legacy` entry naming it, so it must be refused (round 4, security-reviewer)."""
    raw = {"unit-d": _minimal_unit(home_dir="<root>/unit-d", crew="unit-d",
                                    repos=["unit-d"], kb_scope_exceptions={
                                        "unit-d": {"kb_project": "ghost-scope",
                                                    "reason": "made up for the test"}})}
    path = tmp_path / "projects.json"
    path.write_text(json.dumps(raw))
    monkeypatch.setattr(projects, "PROJECTS_JSON", path)
    projects._load_raw.cache_clear()
    with pytest.raises(ValueError, match="ghost-scope.*no central/split unit's own kb_project"):
        projects.load()
    projects._load_raw.cache_clear()


def test_a_kb_scope_exceptions_target_declared_by_kb_legacy_is_admitted(
        monkeypatch, tmp_path):
    """The route that remains: a `kb_scope_exceptions` target declared through
    `_kb_legacy` (or a central/split unit's own `kb_project`) still loads,
    unlike the exception-only case above."""
    raw = {
        "_kb_legacy": ["legacy-scope"],
        "unit-e": _minimal_unit(home_dir="<root>/unit-e", crew="unit-e",
                                 repos=["unit-e"], kb_scope_exceptions={
                                     "unit-e": {"kb_project": "legacy-scope",
                                                 "reason": "made up for the test"}}),
    }
    path = tmp_path / "projects.json"
    path.write_text(json.dumps(raw))
    monkeypatch.setattr(projects, "PROJECTS_JSON", path)
    projects._load_raw.cache_clear()
    projects.load()
    projects._load_raw.cache_clear()


@pytest.mark.parametrize("bad_entry", [
    {"other-repo": "bare-string-not-an-object"},
    {"other-repo": {"kb_project": "", "reason": "fine"}},
    {"other-repo": {"kb_project": "evil", "reason": ""}},
    {"other-repo": {"kb_project": "evil"}},
])
def test_a_missing_or_empty_kb_project_by_repo_field_is_refused(monkeypatch, tmp_path, bad_entry):
    raw = {"evil": _minimal_unit(kb_project_by_repo=bad_entry)}
    path = tmp_path / "projects.json"
    path.write_text(json.dumps(raw))
    monkeypatch.setattr(projects, "PROJECTS_JSON", path)
    projects._load_raw.cache_clear()
    with pytest.raises(ValueError):
        projects.load()
    projects._load_raw.cache_clear()


def test_kb_project_by_repo_and_kb_scope_exceptions_name_disjoint_sets():
    """A name narrowed via `kb_scope_exceptions` must not also appear in some
    unit's `kb_project_by_repo` — that would record the same repo's KB project
    two different ways, with nothing to say which one `_explicit_project_by_name`
    should prefer. Disjoint by this test is what keeps the kb_scope_exceptions
    invariant (`kb_scope_exceptions`'s own docstring) true by construction rather
    than merely checked after the fact."""
    by_repo_names = set()
    exception_names = set()
    for u in projects.load().values():
        by_repo_names |= set(u.get("kb_project_by_repo", {}))
        exception_names |= set(u.get("kb_scope_exceptions", {}))
    overlap = by_repo_names & exception_names
    assert not overlap, (
        f"{overlap} appear in both kb_project_by_repo and kb_scope_exceptions; "
        f"a narrowing belongs in kb_scope_exceptions alone")


# The frozen pre-refactor table and its two equivalence checks (every name it
# held still resolves the same, both as a table and through resolve_project)
# live in tests/test_project_resolver.py's EXPECTED and
# test_the_explicit_table_is_unchanged / the both-CLIs parametrize. This file
# held an exact second copy of all three; removed here, since the resolver
# file's own coverage already exercises resolve_project(name, kb) name-by-name
# with no unique behavior left behind.


def test_explicit_project_by_name_degrades_when_the_library_is_unavailable(monkeypatch, capsys):
    """Round 5 review: `_explicit_project_by_name()` caught only `(OSError,
    json.JSONDecodeError)`, so a Library outage (CQ·563's `PROJECTS_JSON`-unset
    path raising `RegistryUnavailable`, a `RuntimeError`) propagated straight out
    of `resolve_project`'s callers instead of the "no explicit mapping" degrade
    this function's own docstring promises — new with this PR, since
    `resolve_project`'s explicit table needed no registry at all before it."""
    def _boom():
        raise projects.RegistryUnavailable("stub: the Library is down")
    monkeypatch.setattr(projects, "_load_raw", _boom)
    assert projects._explicit_project_by_name() == {}
    assert "_explicit_project_by_name() could not read the registry" in capsys.readouterr().err


def _project_for_repo_in(units, repo):
    """`project_for_repo` reimplemented over an arbitrary units dict, so the
    crafted-registry tests below don't need to monkeypatch `PROJECTS_JSON` and
    clear `projects._load_raw`'s cache just to exercise the agreement check."""
    for slug, u in units.items():
        if repo in list(u.get("repos", [])) + list(u.get("aliases", [])):
            return slug
    return None


def _assert_table_and_registry_agree(explicit_table, units):
    """The two-directions check this issue asks for, isolated as a function so
    both the real registry and a crafted one below exercise identical logic.

    Direction 1 (registry -> literal): a repo the registry narrows via
    `kb_scope_exceptions` must have `EXPLICIT_PROJECT_BY_NAME` narrow it the same
    way. This is what catches a literal entry that fell behind a registry
    correction.

    Direction 2 (literal -> registry): a repo `EXPLICIT_PROJECT_BY_NAME` narrows
    below its project's own `kb_project` must have a matching registry exception.
    This is what catches a registry exception removed — or never recorded — while
    the literal still holds the narrow value, which is this issue's bug.
    """
    exceptions = {}
    for u in units.values():
        exceptions.update(u.get("kb_scope_exceptions", {}))

    for repo, exc in exceptions.items():
        assert explicit_table.get(repo) == exc["kb_project"], (
            f"registry kb_scope_exceptions narrows {repo!r} to {exc['kb_project']!r} "
            f"but EXPLICIT_PROJECT_BY_NAME has {explicit_table.get(repo)!r}")

    for repo, project in explicit_table.items():
        owner_slug = _project_for_repo_in(units, repo)
        if owner_slug is None:
            continue
        owner = units[owner_slug]
        if owner.get("kb") not in ("central", "split"):
            continue
        if owner.get("kb_project") == project:
            continue
        assert repo in exceptions, (
            f"{repo!r} maps to {project!r} in EXPLICIT_PROJECT_BY_NAME, narrower than "
            f"its project {owner_slug!r}'s own kb_project {owner.get('kb_project')!r}, "
            f"but config/projects.json records no kb_scope_exceptions entry for it")
        assert exceptions[repo]["kb_project"] == project, (
            f"{repo!r}'s registry exception says {exceptions[repo]['kb_project']!r} "
            f"but EXPLICIT_PROJECT_BY_NAME says {project!r}")


def test_registry_exceptions_and_explicit_table_agree_both_directions():
    """The record this issue adds: config/projects.json's `kb_scope_exceptions`
    and `EXPLICIT_PROJECT_BY_NAME` must describe the same eight narrowings, or a
    later change deriving scope from one of them would widen the other back."""
    _assert_table_and_registry_agree(projects.EXPLICIT_PROJECT_BY_NAME, projects.load())


def test_removing_a_registry_exception_while_the_literal_keeps_it_fails():
    """Demonstrates the guard the issue asks for: strip `harbor-landing`'s
    recorded exception from a copy of the registry, and the agreement check
    catches the resulting drift — exactly the failure mode the issue describes,
    which on main at f629857a nothing caught at all because the field did not
    exist."""
    units = {k: dict(v) for k, v in projects.load().items()}
    units["harbor"] = dict(units["harbor"])
    units["harbor"]["kb_scope_exceptions"] = {
        k: v for k, v in units["harbor"]["kb_scope_exceptions"].items()
        if k != "harbor-landing"
    }
    with pytest.raises(AssertionError, match=r"harbor-landing.*no kb_scope_exceptions"):
        _assert_table_and_registry_agree(projects.EXPLICIT_PROJECT_BY_NAME, units)


def test_adding_a_narrowing_literal_entry_with_no_registry_exception_fails():
    """The other direction: a new `EXPLICIT_PROJECT_BY_NAME` row that narrows a
    repo below its project's `kb_project`, with nothing recorded in the registry
    to back it, is caught rather than shipped silent. `beacon` is not one of
    the eight recorded exceptions, so overriding its row to disagree with its
    project's own kb_project is exactly an unrecorded narrowing."""
    table = dict(projects.EXPLICIT_PROJECT_BY_NAME)
    table["beacon"] = "platform"
    with pytest.raises(AssertionError, match=r"beacon.*no kb_scope_exceptions"):
        _assert_table_and_registry_agree(table, projects.load())


def test_registry_and_literal_disagree_on_the_same_repos_kb_project_fails():
    """A third drift shape, distinct from the other two: both sides already
    record an exception for `harbor-landing`, so neither "removed" check
    above fires, but the literal's `kb_project` has been edited to a value the
    registry's own exception does not match. Direction 1 of the agreement
    check is what catches this — a value edited on one side without the
    other, not an entry dropped entirely."""
    table = dict(projects.EXPLICIT_PROJECT_BY_NAME)
    table["harbor-landing"] = "commercial"
    with pytest.raises(
        AssertionError,
        match=r"narrows 'harbor-landing'.*EXPLICIT_PROJECT_BY_NAME has 'commercial'",
    ):
        _assert_table_and_registry_agree(table, projects.load())


# --- ADR-0043: black box crews ----------------------------------------------------


def test_a_private_crew_cannot_also_be_centrally_indexed():
    """A BLACK BOX must not be private in name and readable in fact.

    `private: true` says none of this crew's content reaches the Library and none of
    its agents reach Orient. Two other fields have to agree for that to be true, and
    nothing but this test makes them: `kb: local` keeps it out of `kb_slugs()` and so
    out of the shared index, and `task_text_public: false` keeps its task strings off
    the public surfaces.

    Asserted rather than documented because the failure is silent in the dangerous
    direction. An entry marked private with `kb: central` would index its documents
    into the shared KB while every reader of the registry believed it was a black box,
    and nothing downstream would complain — the KB would simply do its job.
    """
    for slug in projects.private_slugs():
        u = projects.get(slug)
        errors = projects.privacy_invariant_errors(u)
        assert not errors, f"{slug} is private but {'; '.join(errors)}"


def test_private_is_spelled_exactly_that_way():
    """`private` is read with `.get(...) is True`, so a misspelling reads as PUBLIC.

    That is the one failure this design cannot detect at runtime: `privat: true` or
    `is_private: true` leaves the crew registered, readable, and believed to be a black
    box. Catching it needs a check on the KEY rather than the value, which is what this
    is. `data_local` is excluded because it is an existing, different mechanism for
    public products whose users' data stays on their devices.
    """
    suspicious = {"privat", "is_private", "isprivate", "private_crew",
                  "blackbox", "black_box", "secret", "confidential"}
    for slug, u in projects.load().items():
        found = suspicious & set(u)
        assert not found, (
            f"{slug} carries {sorted(found)}, which nothing reads. The field is "
            f"`private`; anything else leaves the crew public and silently so.")


# --- a private unit's repo may differ from its own registry key (PR #1015 round 1) --
#
# A private unit's own repos can already differ from its slug in the real registry —
# the same key/repo split this fixture exercises synthetically, so `repo_is_private_unit`
# and `dispatch_is_private` are checked against the shape the live registry already has,
# not only a hypothetical one.


@pytest.fixture
def private_mismatch_registry(tmp_path, monkeypatch):
    """A registry with one private unit whose own key differs from its only repo,
    and one ordinary public unit as a negative control — the shape a private unit's
    row takes once its `repos` is renamed to match a real GitHub repo without
    renaming the registry key alongside it."""
    units = {
        "acme-private-key": {
            "repos": ["acme-private-repo"], "aliases": [], "private": True,
        },
        "acme-public": {
            "repos": ["acme-public-repo"], "aliases": [],
        },
    }
    p = tmp_path / "projects.json"
    p.write_text(json.dumps(units))
    monkeypatch.setattr(projects, "PROJECTS_JSON", p)
    projects._load_raw.cache_clear()
    yield
    projects._load_raw.cache_clear()


def test_repo_is_private_unit_matches_a_private_units_repo_even_when_its_key_differs(
        private_mismatch_registry):
    private = projects.private_slugs()
    assert projects.repo_is_private_unit("acme-private-repo", private) is True
    # Case-insensitively, matching every other registry name comparison.
    assert projects.repo_is_private_unit("ACME-PRIVATE-REPO", private) is True
    # The registry KEY still matches too (unaffected by this fix).
    assert projects.repo_is_private_unit("acme-private-key", private) is True


def test_repo_is_private_unit_is_false_for_a_public_units_repo(private_mismatch_registry):
    private = projects.private_slugs()
    assert projects.repo_is_private_unit("acme-public-repo", private) is False
    assert projects.repo_is_private_unit("acme-public", private) is False
    assert projects.repo_is_private_unit("no-such-repo", private) is False


def test_repo_is_private_unit_snapshot_matches_the_live_answer(private_mismatch_registry):
    private = projects.private_slugs()
    snapshot = projects.private_unit_repo_names(private)
    assert projects.repo_is_private_unit(
        "acme-private-repo", private, snapshot) is True
    assert projects.repo_is_private_unit(
        "acme-public-repo", private, snapshot) is False


def test_dispatch_is_private_matches_a_private_units_repo_even_when_its_key_differs(
        private_mismatch_registry):
    """The security-reviewer's PR #1015 round 1 finding: before this fix,
    `dispatch_is_private`'s repo half was an exact `repo.lower() in
    private_slugs()` match, which answered `False` here because the renamed
    repo no longer equals the registry key."""
    assert projects.dispatch_is_private("acme-private-repo") is True
    assert projects.dispatch_is_private("acme-public-repo") is False


def test_dispatch_is_private_matches_a_private_units_repo_with_snapshot_params(
        private_mismatch_registry):
    """`private_slugs_snapshot`/`private_unit_repos_snapshot` must give the SAME
    answer as the live read — `bin/run-register-api`'s one registry generation
    per request depends on it."""
    private = projects.private_slugs()
    homes = {slug: projects.home_path(slug) for slug in private
             if projects.home_path(slug) is not None}
    repo_names = projects.private_unit_repo_names(private)
    assert projects.dispatch_is_private(
        "acme-private-repo", private_slugs_snapshot=private,
        home_paths_snapshot=homes,
        private_unit_repos_snapshot=repo_names) is True
    assert projects.dispatch_is_private(
        "acme-public-repo", private_slugs_snapshot=private,
        home_paths_snapshot=homes,
        private_unit_repos_snapshot=repo_names) is False


# --- ADR-0043's fourth surface: which crew's home a private dispatch routes into --

@pytest.fixture
def private_home_registry(tmp_path, monkeypatch):
    """A registry with one private unit whose key differs from its repo (same
    shape `private_mismatch_registry` exercises) AND carries a real,
    resolvable `home_dir`, plus a public control — for
    `private_home_for_dispatch`, which (unlike `dispatch_is_private`) needs
    an actual home directory to answer, not just a bool."""
    crews_root = tmp_path / "crews"
    crew_home = crews_root / "nightjar"
    crew_home.mkdir(parents=True)
    monkeypatch.setattr(projects, "ROOT_OVERRIDE", crews_root)
    units = {
        "nightjar-crew": {
            "repos": ["nightjar-repo"], "aliases": [], "private": True,
            "home_dir": "<root>/nightjar",
        },
        "acme-public": {
            "repos": ["acme-public-repo"], "aliases": [],
        },
        # Same shape as private_mismatch_registry's own fixture, for the
        # "a private unit with no resolvable home" edge case — no `home_dir`
        # at all, which `validate_registry` would refuse at write time but
        # `_load_raw` does not check on read.
        "homeless-private": {
            "repos": ["homeless-repo"], "aliases": [], "private": True,
        },
    }
    p = tmp_path / "projects.json"
    p.write_text(json.dumps(units))
    monkeypatch.setattr(projects, "PROJECTS_JSON", p)
    projects._load_raw.cache_clear()
    yield crew_home
    projects._load_raw.cache_clear()


def test_private_home_for_dispatch_matches_by_repo(private_home_registry):
    assert projects.private_home_for_dispatch("nightjar-repo") == private_home_registry
    # Case-insensitively, matching every other registry name comparison.
    assert projects.private_home_for_dispatch("NIGHTJAR-REPO") == private_home_registry
    # The registry KEY matches too.
    assert projects.private_home_for_dispatch("nightjar-crew") == private_home_registry


def test_private_home_for_dispatch_is_none_for_a_public_repo(private_home_registry):
    assert projects.private_home_for_dispatch("acme-public-repo") is None
    assert projects.private_home_for_dispatch("no-such-repo") is None


def test_private_home_for_dispatch_matches_by_agent_source_under_the_agents_dir_bypass(
        private_home_registry, tmp_path):
    """The `--agents-dir` bypass `dispatch_is_private` closes one gate over:
    `repo` alone can read public while the resolved agent body was actually
    read from a private crew's home."""
    source = private_home_registry / ".claude" / "agents" / "some-role.md"
    source.parent.mkdir(parents=True)
    source.write_text("body")
    assert (projects.private_home_for_dispatch("not-private-at-all", str(source))
           == private_home_registry)


def test_private_home_for_dispatch_is_none_when_the_match_has_no_resolvable_home(
        private_home_registry):
    """A private unit with no `home_dir` at all (a malformed registry
    `validate_registry` would refuse at write time, but `_load_raw` does not
    check on read) answers None rather than raising — the caller
    (`bin/agentctl`'s `_private_run_root`) must fail closed on this, not
    silently fall back to the shared run corpus."""
    assert projects.private_home_for_dispatch("homeless-repo") is None


def test_private_home_for_dispatch_agrees_with_dispatch_is_private(private_home_registry):
    """Never answers a home for a dispatch `dispatch_is_private` itself calls
    public, and never answers None for one it calls private UNLESS that
    unit's home cannot be resolved — the one documented divergence."""
    assert (projects.private_home_for_dispatch("nightjar-repo") is not None
           ) == projects.dispatch_is_private("nightjar-repo")
    assert (projects.private_home_for_dispatch("acme-public-repo") is not None
           ) == projects.dispatch_is_private("acme-public-repo")


def test_private_home_for_dispatch_matches_with_snapshot_params(private_home_registry):
    private = projects.private_slugs()
    homes = {slug: projects.home_path(slug) for slug in private
             if projects.home_path(slug) is not None}
    repo_names = projects.private_unit_repo_names(private)
    assert projects.private_home_for_dispatch(
        "nightjar-repo", private_slugs_snapshot=private,
        home_paths_snapshot=homes,
        private_unit_repos_snapshot=repo_names) == private_home_registry
    assert projects.private_home_for_dispatch(
        "acme-public-repo", private_slugs_snapshot=private,
        home_paths_snapshot=homes,
        private_unit_repos_snapshot=repo_names) is None


# --- ARCH-426: the project/product split ------------------------------------------


def test_home_dir_is_stored_portable_and_read_expanded():
    """Stored as `<root>/<name>` so the registry survives a different machine, a
    different root or a packaged tenant instance; every caller wants it absolute,
    so home_path expands it once here rather than at each call site."""
    for slug, u in projects.load().items():
        assert u["home_dir"].startswith("<root>/"), (
            f"{slug} home_dir {u['home_dir']!r} must be root-relative, not absolute — "
            "an absolute path pins the registry to one machine")
        p = projects.home_path(slug)
        assert p is not None and p.is_absolute()
        assert "<root>" not in str(p) and "~" not in str(p)
        assert p.is_relative_to(projects.crews_root().resolve())


def test_the_legacy_stored_form_resolves_to_the_same_home(monkeypatch, tmp_path):
    """A registry written before 2026-09-12 stores `~/Convivy/<name>`; it must
    resolve exactly where the new form does, so an older copy never places a
    home, the black-box wall included, anywhere else."""
    monkeypatch.setattr(projects, "ROOT_OVERRIDE", tmp_path / "root")
    assert projects.registry_home("~/Convivy/LEDGER") == projects.registry_home("<root>/LEDGER") \
        == tmp_path / "root" / "LEDGER"


def test_home_path_and_dirs_for_are_none_and_empty_for_unregistered():
    assert projects.home_path("no-such-project") is None
    assert projects.dirs_for("no-such-project") == []


# --- the runtime refusal of cross-user paths (security-reviewer, PR #333) ---------


@pytest.fixture
def crafted_registry(tmp_path, monkeypatch):
    """Point the loader at a registry file this test writes, the way a packaged
    tenant instance does via PROJECTS_JSON. This is the path the schema test cannot
    reach: it asserts against the CHECKED-IN file, so an override never meets it."""
    def _write(home_dir, dirs=(), private=False):
        p = tmp_path / "projects.json"
        unit = {
            "class": "project", "display": "Evil", "owner": "convivy",
            "home_dir": home_dir, "dirs": list(dirs), "crew": None,
            "lifecycle": "active", "products": [],
            "repos": ["evil"], "aliases": [], "kb": "local",
        }
        if private:
            unit["private"] = True
        p.write_text(json.dumps({"evil": unit}))
        monkeypatch.setattr(projects, "PROJECTS_JSON", p)
        projects._load_raw.cache_clear()
        return p
    yield _write
    projects._load_raw.cache_clear()


def test_expanduser_really_does_escape_to_another_user(tmp_path):
    """The premise of the guard, asserted rather than assumed. Path.expanduser
    resolves ~user/... through pwd, not just $HOME — so this is a real escape and
    not a theoretical one. If a future Python changed this, the guard below would
    still be correct but this test tells us why it exists."""
    escaped = Path("~root/target").expanduser()
    assert not str(escaped).startswith(str(Path.home())), (
        "expanduser no longer escapes to another user's home; re-read the guard's "
        "rationale before relaxing it")


def test_a_crafted_registry_cannot_point_home_at_another_user(crafted_registry):
    crafted_registry("~root/target")
    with pytest.raises(ValueError, match="must start with"):
        projects.home_path("evil")
    with pytest.raises(ValueError, match="must start with"):
        projects.dirs_for("evil")


def test_a_crafted_registry_cannot_smuggle_a_cross_user_path_via_dirs(crafted_registry):
    """home_dir alone being well-formed must not buy the whole record a pass — each
    dirs entry is checked on its own."""
    crafted_registry("~/Convivy/evil", dirs=["~root/target"])
    assert projects.home_path("evil") == (projects.crews_root() / "evil").resolve()
    with pytest.raises(ValueError, match="must start with"):
        projects.dirs_for("evil")


@pytest.mark.parametrize("bad", ["/etc/passwd", "~", "~root", "../../etc", "",
                                 "Convivy/evil"])
def test_absolute_bare_and_relative_registry_paths_are_refused(crafted_registry, bad):
    crafted_registry(bad)
    if bad == "":
        assert projects.home_path("evil") is None  # falsy: reads as "no home_dir"
    else:
        with pytest.raises(ValueError, match="must start with"):
            projects.home_path("evil")


@pytest.mark.parametrize("escape", [
    "~/../../etc",            # climbs out of home entirely
    "~/../root",              # a sibling user's home
    "~/Convivy/../../../tmp",  # legitimate-looking prefix, then climbs
])
def test_a_tilde_prefix_is_not_containment(crafted_registry, escape):
    """The prefix check alone is NOT enough, and this is the case that proves it:
    every value here satisfies startswith('~/') and still lands outside home.
    `~/../../etc/passwd` resolves to /private/etc/passwd. The guard resolves and
    tests containment for exactly this reason."""
    crafted_registry(escape)
    with pytest.raises(ValueError, match="outside"):
        projects.home_path("evil")
    crafted_registry("~/Convivy/ok", dirs=[escape])
    with pytest.raises(ValueError, match="outside"):
        projects.dirs_for("evil")


def test_a_symlink_out_of_home_is_refused(crafted_registry, tmp_path, monkeypatch):
    """resolve() collapses symlinks, so the same escape dressed as a link is caught
    by the same containment test. Built against a real link rather than asserted in
    the abstract, because whether resolve() runs before or after the check is the
    whole question."""
    fake_home = tmp_path / "home"
    (fake_home / "Convivy").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (fake_home / "Convivy" / "escape").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("HOME", str(fake_home))
    # Registry homes resolve against the machine's root, which ignores HOME;
    # the explicit override is how a test moves it (company/decisions/0008).
    monkeypatch.setattr(projects, "ROOT_OVERRIDE", fake_home / "Convivy")
    crafted_registry("~/Convivy/escape")
    with pytest.raises(ValueError, match="outside"):
        projects.home_path("evil")


def test_a_symlinked_home_dir_on_a_private_project_is_refused(crafted_registry, tmp_path, monkeypatch):
    """A BLACK BOX's home_dir must not be a symlink. `home_path()` resolves through
    `.resolve()`, following the symlink to its REAL target — but a
    `~/.claude/projects` transcript dirname may preserve whichever cwd form Claude
    Code actually saw, not necessarily the resolved one, so a symlinked home_dir
    could let a private crew's sessions sanitize to a dirname that never contains
    the resolved-target fragment `bin/transcript-capture` computes, missing the
    black-box exclusion entirely (security-reviewer, PR #415, run-61a07dc7)."""
    fake_home = tmp_path / "home"
    (fake_home / "Convivy").mkdir(parents=True)
    real_target = fake_home / "Convivy" / "real-target"
    real_target.mkdir()
    (fake_home / "Convivy" / "evil-link").symlink_to(real_target, target_is_directory=True)
    monkeypatch.setenv("HOME", str(fake_home))
    # Registry homes resolve against the machine's root, which ignores HOME;
    # the explicit override is how a test moves it (company/decisions/0008).
    monkeypatch.setattr(projects, "ROOT_OVERRIDE", fake_home / "Convivy")
    crafted_registry("~/Convivy/evil-link", private=True)
    with pytest.raises(ValueError, match="symlink"):
        projects.home_path("evil")


def test_a_symlinked_dirs_entry_on_a_private_project_is_refused(crafted_registry, tmp_path, monkeypatch):
    """Each `dirs` entry is checked on its own, the same way the cross-user escape
    test above checks dirs independently of home_dir."""
    fake_home = tmp_path / "home"
    (fake_home / "Convivy").mkdir(parents=True)
    real_target = fake_home / "Convivy" / "real-target"
    real_target.mkdir()
    (fake_home / "Convivy" / "evil-link").symlink_to(real_target, target_is_directory=True)
    monkeypatch.setenv("HOME", str(fake_home))
    # Registry homes resolve against the machine's root, which ignores HOME;
    # the explicit override is how a test moves it (company/decisions/0008).
    monkeypatch.setattr(projects, "ROOT_OVERRIDE", fake_home / "Convivy")
    crafted_registry("~/Convivy/ok", dirs=["~/Convivy/evil-link"], private=True)
    with pytest.raises(ValueError, match="symlink"):
        projects.dirs_for("evil")


def test_a_symlinked_intermediate_segment_on_a_private_project_is_refused(
        crafted_registry, tmp_path, monkeypatch):
    """The leaf-only check misses this shape: `Path.is_symlink()` lstats only the
    FINAL path component, so it says False when the symlink sits one segment up —
    home_dir's parent directory, not home_dir itself — while `.resolve()` still
    silently follows it to a different real location (reviewer, PR #415,
    run-6a061c87)."""
    fake_home = tmp_path / "home"
    (fake_home / "Convivy").mkdir(parents=True)
    real_target = fake_home / "Convivy" / "real-target"
    (real_target / "actual-project").mkdir(parents=True)
    (fake_home / "Convivy" / "linked-dir").symlink_to(real_target, target_is_directory=True)
    monkeypatch.setenv("HOME", str(fake_home))
    # Registry homes resolve against the machine's root, which ignores HOME;
    # the explicit override is how a test moves it (company/decisions/0008).
    monkeypatch.setattr(projects, "ROOT_OVERRIDE", fake_home / "Convivy")
    crafted_registry("~/Convivy/linked-dir/actual-project", private=True)
    with pytest.raises(ValueError, match="symlink"):
        projects.home_path("evil")


def test_a_symlinked_home_dir_on_a_non_private_project_is_not_refused(crafted_registry, tmp_path, monkeypatch):
    """The refusal is scoped to `private: true` projects only — an ordinary public
    project's symlinked directory carries no black-box exclusion contract to
    violate, so it resolves exactly as it did before this guard existed."""
    fake_home = tmp_path / "home"
    (fake_home / "Convivy").mkdir(parents=True)
    real_target = fake_home / "Convivy" / "real-target"
    real_target.mkdir()
    (fake_home / "Convivy" / "fine-link").symlink_to(real_target, target_is_directory=True)
    monkeypatch.setenv("HOME", str(fake_home))
    # Registry homes resolve against the machine's root, which ignores HOME;
    # the explicit override is how a test moves it (company/decisions/0008).
    monkeypatch.setattr(projects, "ROOT_OVERRIDE", fake_home / "Convivy")
    crafted_registry("~/Convivy/fine-link", private=False)
    assert projects.home_path("evil") == real_target.resolve()


def test_a_legitimate_path_inside_home_still_resolves(crafted_registry):
    crafted_registry("~/Convivy/fine", dirs=["~/Convivy/also-fine"])
    assert projects.home_path("evil") == (projects.crews_root() / "fine").resolve()
    assert projects.dirs_for("evil") == [
        (projects.crews_root() / "fine").resolve(),
        (projects.crews_root() / "also-fine").resolve()]


def test_dirs_for_dedupes_home_repeated_in_dirs(crafted_registry):
    crafted_registry("~/Convivy/evil", dirs=["~/Convivy/evil", "~/Convivy/other"])
    got = projects.dirs_for("evil")
    assert got == [(projects.crews_root() / "evil").resolve(),
                   (projects.crews_root() / "other").resolve()]


def test_dirs_for_leads_with_home_then_the_associated_dirs():
    for slug, u in projects.load().items():
        got = projects.dirs_for(slug)
        assert got[0] == projects.home_path(slug), f"{slug}: home must come first"
        assert len(got) == 1 + len(u["dirs"])


# --- ARCH-426: one inbox per project, not per directory ---------------------------


def test_inbox_links_are_empty_for_a_single_directory_project():
    """Most projects have one directory, so there is nothing to unify and no link
    to make. Returning [] rather than a self-link keeps the linker from creating a
    symlink from a directory's inbox to itself."""
    for slug, u in projects.load().items():
        if not u["dirs"]:
            assert projects.inbox_links(slug) == [], f"{slug} has one dir and needs no link"


def test_inbox_links_point_every_associated_dir_at_the_home_inbox(crafted_registry):
    crafted_registry("~/Convivy/home", dirs=["~/Convivy/second", "~/Convivy/third"])
    got = projects.inbox_links("evil")
    assert [link for link, _ in got] == [
        (projects.crews_root() / "second/inbox").resolve(),
        (projects.crews_root() / "third/inbox").resolve()]
    for link, target in got:
        assert (link.parent / target).resolve() == (projects.crews_root() / "home/inbox").resolve()


def test_inbox_link_targets_are_relative_so_the_base_dir_can_move(crafted_registry):
    """ARCH-429 makes the base directory a configured setting. An absolute target
    would pin every link to today's ~/Convivy and break the whole farm on a move."""
    crafted_registry("~/Convivy/main", dirs=["~/Convivy/second"])
    (_, target), = projects.inbox_links("evil")
    assert not target.is_absolute(), f"target {target} must be relative to the link's parent"
    assert str(target) == "../main/inbox"


def test_relay_is_the_live_two_directory_project():
    """The case the feature exists for: agents working in foundry-services wrote to
    that repo's inbox and the relay co, watching foundry-kit's, never saw them."""
    (link, target), = projects.inbox_links("relay")
    # NOT resolved: `link` is the path the symlink LIVES at, and once the link
    # exists on disk `.resolve()` follows it through to foundry-kit — so the
    # expectation quietly became "the link points at itself", which is the one
    # thing it must not do. Green in CI, where neither directory exists, and red
    # on the machine that actually has the link. inbox_links computes without
    # touching the filesystem, so the assertion should not either.
    assert link == projects.crews_root() / "foundry-services/inbox"
    assert (link.parent / target).resolve() == (
        projects.crews_root() / "foundry-kit/inbox").resolve()


def test_every_product_link_resolves():
    """A project's `products` entries must be real keys in the _products block —
    a dangling link would silently drop a commercial attribution."""
    known = set(projects.products())
    for slug, u in projects.load().items():
        unknown = set(u["products"]) - known
        assert not unknown, f"{slug} links unknown products {unknown}"


def test_products_block_is_wellformed():
    for slug, p in projects.products().items():
        assert isinstance(p.get("display"), str) and p["display"], f"product {slug} needs a display"
        assert p.get("owner"), f"product {slug} needs an owner"


def test_products_are_not_projects():
    """The two dimensions are separate namespaces. They may share a slug (`finch`
    the project builds `finch` the product), so this asserts the BLOCK is separate,
    not the names — load() must never surface a product as a project."""
    assert projects.PRODUCTS_KEY not in projects.load()
    assert projects.get(projects.PRODUCTS_KEY) is None


def test_product_links_round_trip_in_both_directions():
    for slug in projects.load():
        for prod in projects.products_for(slug):
            assert slug in projects.projects_for_product(prod), (
                f"{slug} claims product {prod} but projects_for_product disagrees")


def test_projects_for_product_is_empty_for_an_unknown_product():
    assert projects.projects_for_product("no-such-product") == []


def test_crew_relation_is_many_projects_to_one_crew():
    """ARCH-426 makes the relation many-to-one, so several projects sharing a crew
    is legal and must not be treated as a collision."""
    for slug in projects.load():
        crew = projects.crew_for(slug)
        if crew is None:
            continue
        assert slug in projects.projects_by_crew(crew)
    assert projects.projects_by_crew("no-such-crew") == []


def test_no_crew_is_not_a_crew():
    """A null crew must never group. The comparison inside projects_by_crew matches
    None == None, so without an explicit guard the natural chain below returns every
    crewless project as though they shared one — the exact collapse the null value
    exists to avoid. No project carries a null crew today, so the loop below is
    vacuous; it is kept because it is what fails when the next crewless project is
    added, which is the moment the guard matters.

    Asserted through the CHAIN, not just the literal, because passing None directly
    is the call nobody makes; passing crew_for(slug) is the call everybody makes."""
    assert projects.projects_by_crew(None) == []
    for slug in projects.load():
        if projects.crew_for(slug) is None:
            assert projects.projects_by_crew(projects.crew_for(slug)) == []
    crewless = {s for s in projects.load() if projects.crew_for(s) is None}
    grouped = {s for crew in {projects.crew_for(x) for x in projects.load()} if crew
               for s in projects.projects_by_crew(crew)}
    assert not (crewless & grouped), "a crewless project appeared in a crew group"




@pytest.fixture
def agent_prefix_registry(tmp_path, monkeypatch):
    """A registry this test controls directly, for `agent_prefix` shapes
    `crafted_registry` (above) has no parameter for: `_write(**agent_prefixes)`
    maps a slug to its raw `agent_prefix` value (a string, a list, or omitted
    when the slug isn't passed at all)."""
    def _write(**agent_prefixes):
        units = {}
        for slug, raw in agent_prefixes.items():
            unit = {
                "class": "project", "display": "Evil", "owner": "convivy",
                "home_dir": "<root>/evil", "dirs": [], "crew": None,
                "lifecycle": "active", "products": [],
                "repos": [slug], "aliases": [], "kb": "local",
            }
            if raw is not None:
                unit["agent_prefix"] = raw
            units[slug] = unit
        p = tmp_path / "projects.json"
        p.write_text(json.dumps(units))
        monkeypatch.setattr(projects, "PROJECTS_JSON", p)
        projects._load_raw.cache_clear()
        return p
    yield _write
    projects._load_raw.cache_clear()


def test_agent_prefixes_normalizes_a_single_string_and_a_list_alike(agent_prefix_registry):
    agent_prefix_registry(x="x-", y=["y-", "z-"])
    assert projects.agent_prefixes("x") == ("x-",)
    assert projects.agent_prefixes("y") == ("y-", "z-")


def test_agent_prefixes_is_empty_for_an_absent_field_or_an_unregistered_slug(
        agent_prefix_registry):
    agent_prefix_registry(x=None)
    assert projects.agent_prefixes("x") == ()
    assert projects.agent_prefixes("no-such-slug") == ()


def test_agent_prefixes_raises_on_an_entry_not_ending_in_a_hyphen(agent_prefix_registry):
    agent_prefix_registry(x="x")
    with pytest.raises(ValueError):
        projects.agent_prefixes("x")


def test_all_agent_prefixes_dedupes_a_prefix_shared_across_units(agent_prefix_registry):
    agent_prefix_registry(x="shared-", y="shared-", z="solo-")
    assert projects.all_agent_prefixes() == ("shared-", "solo-")


def test_internal_app_prefixes_degrades_to_empty_on_a_malformed_entry(
        agent_prefix_registry, capsys):
    """One crew's typo'd `agent_prefix` (missing the trailing `-`) raises
    `ValueError` out of `projects.agent_prefixes()`, and `all_agent_prefixes()`
    re-raises the first bad one it finds while iterating every unit — so this
    must degrade the same way an unreadable registry does (fail CLOSED: no
    prefix recognised), not crash every other crew's prefix lookup too."""
    agent_prefix_registry(x="not-a-prefix", y="fine-")
    assert app_prefixes.internal_app_prefixes() == ()
    assert "x" in capsys.readouterr().err


def test_internal_app_prefixes_degrades_to_empty_on_broken_json(
        tmp_path, monkeypatch, capsys):
    """Broken JSON raises `json.JSONDecodeError`, a `ValueError` subclass, out
    of `_load_raw()` before `agent_prefixes()` ever runs — a different failure
    than a malformed entry, and the module's own docstring promises the same
    fail-closed empty tuple for both."""
    p = tmp_path / "projects.json"
    p.write_text("{not valid json")
    monkeypatch.setattr(projects, "PROJECTS_JSON", p)
    projects._load_raw.cache_clear()
    try:
        assert app_prefixes.internal_app_prefixes() == ()
        assert capsys.readouterr().err
    finally:
        projects._load_raw.cache_clear()


def test_lifecycle_partitions_the_registry():
    all_slugs = set(projects.load())
    partitioned = set()
    for state in projects.LIFECYCLES:
        partitioned |= set(projects.slugs_by_lifecycle(state))
    assert partitioned == all_slugs, "every project must carry a known lifecycle"
    assert set(projects.active_slugs()) <= all_slugs


def test_signpost_is_archived():
    """Signpost retired 2026-10-21 (the operator, relayed by the steward): nothing carries
    forward, and the steward is closing its open work items as wontfix. Its
    registry row stays — ARCH-358's rule that an archived project keeps its
    history and its repo claim, so past cost, velocity and PR rows attributed
    to `signpost` keep resolving to it."""
    unit = projects.get("signpost")
    assert unit is not None
    assert unit["lifecycle"] == "archived"
    assert "signpost" not in projects.active_slugs()
    assert "signpost" not in projects.slugs_by_lifecycle("active")
    # the repo claim survives the lifecycle flip
    assert projects.project_for_repo("signpost") == "signpost"
    assert projects.fold_map()["signpost"] == "Signpost"
    assert projects.repo_github_slug("signpost") == "convivy/signpost"


def test_the_sunsetted_kit_repos_are_absent_entirely():
    """ARCH-427 archived agentic-starter-kit, multi-agent-slack-bot-kit and
    multitenant-registry out of ~/Convivy/ altogether, so they are ABSENT from the
    registry rather than lifecycle=archived. Guards against someone re-adding them
    as projects while cleaning up the consumer lists that still name them."""
    gone = {"agentic-starter-kit", "multi-agent-slack-bot-kit", "multitenant-registry"}
    for slug, u in projects.load().items():
        # the slug itself, not only what it claims — "absent entirely" means a
        # project cannot be registered UNDER one of these names either
        assert slug not in gone, f"{slug} is a sunsetted repo, re-added as a project"
        claimed = set(u["repos"]) | set(u.get("aliases", []))
        assert not (claimed & gone), f"{slug} claims sunsetted repos {claimed & gone}"
    assert not (set(projects.products()) & gone), "a sunsetted repo re-added as a product"


def test_no_repo_or_alias_claimed_twice():
    """Each raw repo/alias string folds into exactly one unit, so fold_map() is
    unambiguous and no two dashboards double-count a repo."""
    seen: dict[str, str] = {}
    for slug, u in projects.load().items():
        for raw in list(u["repos"]) + list(u.get("aliases", [])):
            assert raw not in seen, f"{raw!r} claimed by both {seen[raw]} and {slug}"
            seen[raw] = slug


def test_home_dir_basename_names_a_claimed_repo():
    """worktrees._registry_primary_for resolves a dispatch's primary checkout by
    matching a unit's home directory basename against one of its own `repos`
    entries. For every unit where that match applies at all — a non-empty
    `repos` and a `home_dir` — it has to hold, or dispatch silently fails to
    find the checkout it just created."""
    offenders = []
    for slug, u in projects.load().items():
        repos = u.get("repos") or []
        if not repos or not u.get("home_dir"):
            continue
        if projects.home_path(slug).name not in repos:
            offenders.append(slug)
    assert not offenders, f"home_dir basename matches none of repos for: {offenders}"


def test_repos_match_velocity_buckets(velocity_report):
    """A unit's `repos` must equal velocity-report's BUCKETS[display] — the two
    are the same fact (which repos roll up into this unit's velocity)."""
    buckets = velocity_report.BUCKETS
    private = projects.private_slugs()
    for slug, u in projects.load().items():
        if slug in private:
            # A BLACK BOX must be ABSENT here, and that is the ADR-0043 consequence
            # rather than an exemption from this test: "velocity and queue metrics
            # will not see this work, and should not." Asserting the absence keeps
            # this test meaningful for private crews instead of skipping them —
            # adding a black box to the dashboards would otherwise pass silently.
            assert u["display"] not in buckets, (
                f"{slug} is a black box but appears in velocity BUCKETS as "
                f"{u['display']!r}; its work must not roll up onto a dashboard")
            continue
        assert u["display"] in buckets, f"{slug} display {u['display']!r} absent from velocity BUCKETS"
        # membership, not order — sort both sides so a reordering of either list
        # doesn't fail a test whose claim is "the same repos roll up here".
        assert sorted(u["repos"]) == sorted(buckets[u["display"]]), (
            f"{slug} repos {u['repos']} != velocity BUCKETS[{u['display']!r}] {buckets[u['display']]}"
        )


def _velocity_order_unit(display, repos, velocity_order=None, **extra):
    unit = {
        "class": "project", "display": display, "owner": "convivy",
        "home_dir": f"<root>/{repos[0]}", "dirs": [], "crew": repos[0],
        "lifecycle": "active", "products": [], "repos": repos,
        "aliases": [], "kb": "local",
    }
    if velocity_order is not None:
        unit["velocity_order"] = velocity_order
    unit.update(extra)
    return unit


@pytest.fixture
def velocity_order_registry(tmp_path, monkeypatch):
    """A crafted registry mirroring BUCKETS' shape, with made-up names: two units
    ordered by `velocity_order`, one absent, and a black box that must stay out."""
    raw = {
        "acme-later": _velocity_order_unit("Acme Later", ["acme-later"], velocity_order=5),
        "acme-first": _velocity_order_unit("Acme First", ["acme-first"], velocity_order=1),
        "acme-unordered": _velocity_order_unit("Acme Unordered", ["acme-unordered"]),
        "acme-black-box": _velocity_order_unit(
            "Acme Black Box", ["acme-black-box"], private=True),
        "_scopes": {}, "_products": {},
    }
    path = tmp_path / "projects.json"
    path.write_text(json.dumps(raw))
    monkeypatch.setattr(projects, "PROJECTS_JSON", path)
    projects._load_raw.cache_clear()
    yield raw
    projects._load_raw.cache_clear()


def test_velocity_buckets_orders_by_velocity_order_then_excludes_black_boxes(
        velocity_order_registry):
    """Pins the shape `bin/velocity-report`'s BUCKETS needs: lower `velocity_order`
    first, a unit with none sorts after every unit that has one (by `display`,
    the conservative placement for a newly registered unit), and a black box
    never appears regardless of its own `velocity_order`."""
    buckets = projects.velocity_buckets()
    assert list(buckets) == ["Acme First", "Acme Later", "Acme Unordered"]
    assert buckets["Acme First"] == ["acme-first"]
    assert "Acme Black Box" not in buckets


def test_kb_project_bucket_map_reads_adr_kb_projects_and_excludes_black_boxes(
        tmp_path, monkeypatch):
    raw = {
        "acme": _velocity_order_unit(
            "Acme", ["acme"], adr_kb_projects=["acme", "acme-legacy"]),
        "acme-quiet": _velocity_order_unit("Acme Quiet", ["acme-quiet"]),
        "acme-black-box": _velocity_order_unit(
            "Acme Black Box", ["acme-black-box"], private=True,
            adr_kb_projects=["acme-black-box"]),
        "_scopes": {}, "_products": {},
    }
    path = tmp_path / "projects.json"
    path.write_text(json.dumps(raw))
    monkeypatch.setattr(projects, "PROJECTS_JSON", path)
    projects._load_raw.cache_clear()
    try:
        m = projects.kb_project_bucket_map()
        assert m == {"acme": "Acme", "acme-legacy": "Acme"}
        # a unit naming no adr_kb_projects contributes nothing — the conservative
        # default, rather than guessing its own kb_project in
        assert "acme-quiet" not in m.values()
        # a black box's ADR slugs never reach a dashboard bucket
        assert "acme-black-box" not in m
    finally:
        projects._load_raw.cache_clear()


def test_aliases_fold_to_display():
    """Every alias must fold to the unit's display via cost_join — the fold table
    (oak-architecture → Acme Arch Tools, demo-assistant/aide → Atlas) agrees with the registry.
    Primary repos aren't asserted here: cost_join only folds the alias families and
    returns other repo strings (…-commercial, …-landing) unchanged by design."""
    for slug, u in projects.load().items():
        for alias in u.get("aliases", []):
            got = cost_join._canonical_product(alias)
            assert got == u["display"], (
                f"{slug}: cost_join folds {alias!r} to {got!r}, registry says {u['display']!r}"
            )


def test_is_data_local_reads_public_task_slugs_directly(agent_activity_report):
    """`_PUBLIC_TASK_PRODUCTS` is gone — it used to be a hand-maintained
    allowlist pinned equal to the registry by two drift tests (one for each
    direction a copy can fall out of step). `_is_data_local` now reads
    `projects.public_task_slugs()` directly, so there is no second list left
    to drift: every slug the registry marks `task_text_public: true` reads as
    public, and nothing else does, by construction rather than by a test
    pinning two copies together."""
    aar = agent_activity_report
    public = projects.public_task_slugs()
    for slug, u in projects.load().items():
        assert aar._is_data_local(slug) == (slug not in public) == (
            u.get("task_text_public") is not True)


def test_kb_slugs_are_registered_projects():
    """Every unit with a central/split KB slice must be in the KB validator's
    ALLOWED_PROJECTS (the agent-platform mirror), or its docs silently won't index."""
    missing = projects.kb_slugs() - knowledge.ALLOWED_PROJECTS
    assert not missing, f"kb slices {missing} absent from knowledge.ALLOWED_PROJECTS"


# --- legal_kb_projects: kb_slugs() ∪ scopes() ∪ _kb_legacy (platform/decisions/0097) --
#
# The fixture below is built so that an implementation deriving the set from the
# registry's own top-level keys (`set(raw)`) would disagree with the right one on
# two points at once: it would wrongly ADMIT `local-crew` (a `kb: local` crew is a
# top-level key but must stay out of the set) and it would wrongly OMIT `substrate`
# and `knowledge` (legal KB slugs that are not top-level keys at all, since they
# live under `_kb_legacy`, a list, not a project unit).


@pytest.fixture
def legal_kb_registry(tmp_path, monkeypatch):
    """A crafted registry exercising every input `legal_kb_projects()` combines:
    a `kb: local` crew (must be excluded), a `kb: central` crew (must be
    included via `kb_slugs()`), a non-project scope (must be included via
    `scopes()`), and `_kb_legacy` (must be included directly)."""
    raw = {
        "local-crew": {
            "class": "project", "display": "Local Crew", "owner": "convivy",
            "home_dir": "<root>/local-crew", "dirs": [], "crew": "local-crew",
            "lifecycle": "active", "products": [], "repos": ["local-crew"],
            "aliases": [], "kb": "local",
        },
        "shared-crew": {
            "class": "project", "display": "Shared Crew", "owner": "convivy",
            "home_dir": "<root>/shared-crew", "dirs": [], "crew": "shared-crew",
            "lifecycle": "active", "products": [], "repos": ["shared-crew"],
            "aliases": [], "kb": "central", "kb_project": "shared-crew",
        },
        "_scopes": {"_doc": "non-project scopes", "platform": {"display": "Platform"}},
        "_kb_legacy": ["substrate", "knowledge"],
        "_products": {},
    }
    path = tmp_path / "projects.json"
    path.write_text(json.dumps(raw))
    monkeypatch.setattr(projects, "PROJECTS_JSON", path)
    projects._load_raw.cache_clear()
    yield raw
    projects._load_raw.cache_clear()


def test_legal_kb_projects_excludes_a_kb_local_crew(legal_kb_registry):
    """A top-level key is not enough to admit a slug — `local-crew`'s `kb: local`
    must keep it out, the same exclusion `kb_slugs()` already makes."""
    assert "local-crew" not in projects.legal_kb_projects()


def test_legal_kb_projects_includes_both_legacy_names(legal_kb_registry):
    assert {"substrate", "knowledge"} <= projects.legal_kb_projects()


def test_legal_kb_projects_includes_a_shared_kb_project(legal_kb_registry):
    """A project registered with a shared (central/split) KB is present, via
    `kb_slugs()`."""
    assert "shared-crew" in projects.legal_kb_projects()


def test_legal_kb_projects_includes_a_non_project_scope(legal_kb_registry):
    """A non-project scope is present, via `scopes()`."""
    assert "platform" in projects.legal_kb_projects()


def test_legal_kb_projects_unchanged_by_a_new_non_project_top_level_key(legal_kb_registry):
    """Adding a top-level key that names no project (another underscore-prefixed
    metadata block, the same shape `_meta`/`_products`/`_scopes`/`_kb_legacy`
    already take) must not change the answer — the set is built from
    `kb_slugs()`/`scopes()`/`_kb_legacy` alone, never from `set(raw)`."""
    before = projects.legal_kb_projects()
    raw = dict(legal_kb_registry)
    raw["_future_meta"] = {"anything": "goes"}
    path = projects.PROJECTS_JSON
    path.write_text(json.dumps(raw))
    projects._load_raw.cache_clear()
    assert projects.legal_kb_projects() == before


def test_legal_kb_projects_raises_on_an_unreadable_registry(tmp_path, monkeypatch):
    """Later callers refuse on this error rather than treat an empty or partial
    set as the true legal set, so the function must raise, not swallow it."""
    monkeypatch.setattr(projects, "PROJECTS_JSON", tmp_path / "no-such-file.json")
    projects._load_raw.cache_clear()
    with pytest.raises(FileNotFoundError):
        projects.legal_kb_projects()
    projects._load_raw.cache_clear()


# --- legal_kb_projects_live: the uncached read (platform/decisions/0097, PR #978 round 2) --


def test_legal_kb_projects_live_sees_a_registry_change_with_no_cache_clear(
        tmp_path, monkeypatch):
    """`legal_kb_projects_live()` is the read `knowledge._current_allowed_projects()`
    calls for the long-running daemons (`bin/knowledge-watcher`,
    `bin/mcp-kb-server`) that never call `_load_raw.cache_clear()` — unlike
    `legal_kb_projects()`, whose cache this bypasses entirely, a registry
    change must reach it with no cache to clear."""
    registry = {"_kb_legacy": ["existing-scope"]}
    path = tmp_path / "projects.json"
    path.write_text(json.dumps(registry))
    monkeypatch.setattr(projects, "PROJECTS_JSON", path)
    assert "new-scope" not in projects.legal_kb_projects_live()

    registry["_kb_legacy"].append("new-scope")
    path.write_text(json.dumps(registry))
    assert "new-scope" in projects.legal_kb_projects_live()


def test_legal_kb_projects_live_agrees_with_legal_kb_projects(legal_kb_registry):
    """Same answer as `legal_kb_projects()` against the same registry — the two
    share `_legal_kb_projects_from_raw` and differ only in which read feeds
    it."""
    assert projects.legal_kb_projects_live() == projects.legal_kb_projects()


def test_legal_kb_projects_live_raises_on_an_unreadable_registry(tmp_path, monkeypatch):
    """Raises exactly as `legal_kb_projects()` does: never an empty or partial
    set on an unreadable registry."""
    monkeypatch.setattr(projects, "PROJECTS_JSON", tmp_path / "no-such-file.json")
    with pytest.raises(FileNotFoundError):
        projects.legal_kb_projects_live()


# --- _kb_projects: facts about a KB PROJECT NAME (the steward's ruling, 2026-10-22) --
#
# Keyed by KB project name rather than by a unit's own slug or a `_products` slug,
# because the set of names Orient needs (agent-platform, foundry-kit, …) spans
# all three of a unit's own `kb_project`, a `kb_scope_exceptions` entry's
# `kb_project`, and a `_kb_legacy` entry. The fixture below exercises all three
# with made-up names, never a real Convivy crew.


@pytest.fixture
def crafted_kb_projects_registry(tmp_path, monkeypatch):
    def _write(kb_projects_block):
        raw = {
            "acme": {
                "class": "project", "display": "Acme", "owner": "convivy",
                "home_dir": "<root>/acme", "dirs": [], "crew": "acme",
                "lifecycle": "active", "products": [], "repos": ["acme"],
                "aliases": [], "kb": "central", "kb_project": "acme",
                "kb_scope_exceptions": {
                    "acme-web": {
                        "kb_project": "acme-web",
                        "reason": "a made-up narrowing for this test only, not a real one",
                    },
                },
            },
            # `acme-web` is also `_kb_legacy`: `_validate_kb_project_targets`
            # refuses a `kb_scope_exceptions` value declared only by another
            # `kb_scope_exceptions` entry (agent-platform#988 round 4), so a made-up
            # narrowing target needs its own independent declaration too, the same
            # way a real one does in config/projects.json.
            "_kb_legacy": ["globex", "acme-web"],
            "_kb_projects": kb_projects_block,
        }
        path = tmp_path / "projects.json"
        path.write_text(json.dumps(raw))
        monkeypatch.setattr(projects, "PROJECTS_JSON", path)
        projects._load_raw.cache_clear()
        return path
    yield _write
    projects._load_raw.cache_clear()


def test_kb_projects_accepts_each_of_the_three_declaration_routes(crafted_kb_projects_registry):
    """`acme` (a unit's own `kb_project`), `acme-web` (a `kb_scope_exceptions`
    entry's `kb_project`) and `globex` (a `_kb_legacy` entry) are the three
    ways a name reaches `_declared_kb_project_names()`."""
    crafted_kb_projects_registry({
        "acme": {"standalone": True},
        "acme-web": {"standalone": False},
        "globex": {},
    })
    assert list(projects.kb_projects()) == ["acme", "acme-web", "globex"]
    assert projects.standalone_kb_projects() == ["acme"]


def test_kb_projects_refuses_an_undeclared_name(crafted_kb_projects_registry):
    crafted_kb_projects_registry({"nobody-declares-this": {"standalone": True}})
    with pytest.raises(ValueError, match="nobody-declares-this"):
        projects.kb_projects()


def test_kb_projects_refuses_a_non_object_value(crafted_kb_projects_registry):
    crafted_kb_projects_registry({"acme": True})
    with pytest.raises(ValueError, match="acme.*object"):
        projects.kb_projects()


def test_kb_projects_refuses_an_unknown_field(crafted_kb_projects_registry):
    crafted_kb_projects_registry({"acme": {"standalone": True, "surprise": "field"}})
    with pytest.raises(ValueError, match="acme.*unknown field"):
        projects.kb_projects()


def test_kb_projects_refuses_a_non_boolean_standalone(crafted_kb_projects_registry):
    crafted_kb_projects_registry({"acme": {"standalone": "yes"}})
    with pytest.raises(ValueError, match="acme.*standalone.*boolean"):
        projects.kb_projects()


def test_standalone_kb_projects_preserves_map_order_not_sorted(crafted_kb_projects_registry):
    """Orient reads this order as its display order, so it must be the map's
    own key order, never sorted."""
    crafted_kb_projects_registry({
        "globex": {"standalone": True},
        "acme": {"standalone": True},
        "acme-web": {"standalone": False},
    })
    assert projects.standalone_kb_projects() == ["globex", "acme"]


def test_kb_projects_is_not_read_as_a_unit(crafted_kb_projects_registry):
    crafted_kb_projects_registry({"acme": {"standalone": True}})
    assert projects.KB_PROJECTS_KEY not in projects.load()
    assert projects.get(projects.KB_PROJECTS_KEY) is None


# --- `_kb_projects.section` (the steward's ruling, 2026-10-22, widened the same day) ---


def test_declared_kb_project_names_now_admits_a_non_project_scope(tmp_path, monkeypatch):
    """Widened alongside `section`: a `_kb_projects` key may now name a
    non-project scope, matching what `legal_kb_projects()` already admits via
    `scopes()` — previously only `kb_slugs()`, `kb_scope_exceptions` and
    `_kb_legacy` could declare a name."""
    raw = {
        "_scopes": {"made-up-scope": {"display": "Made Up"}},
        "_kb_projects": {"made-up-scope": {"section": "engineering"}},
    }
    path = tmp_path / "projects.json"
    path.write_text(json.dumps(raw))
    monkeypatch.setattr(projects, "PROJECTS_JSON", path)
    projects._load_raw.cache_clear()
    try:
        assert projects.kb_project_section_for("made-up-scope") == "engineering"
    finally:
        projects._load_raw.cache_clear()


def test_kb_projects_accepts_a_section_string(crafted_kb_projects_registry):
    crafted_kb_projects_registry({"acme": {"section": "engineering"}})
    assert projects.kb_project_section_for("acme") == "engineering"


def test_kb_projects_refuses_a_non_string_section(crafted_kb_projects_registry):
    crafted_kb_projects_registry({"acme": {"section": 123}})
    with pytest.raises(ValueError, match="acme.*section.*non-empty string"):
        projects.kb_projects()


def test_kb_projects_refuses_an_empty_section(crafted_kb_projects_registry):
    crafted_kb_projects_registry({"acme": {"section": ""}})
    with pytest.raises(ValueError, match="acme.*section.*non-empty string"):
        projects.kb_projects()


def test_kb_project_section_for_is_none_when_the_field_is_absent(crafted_kb_projects_registry):
    crafted_kb_projects_registry({"acme": {"standalone": True}})
    assert projects.kb_project_section_for("acme") is None


def test_kb_project_section_for_is_none_when_the_entry_is_absent(crafted_kb_projects_registry):
    """`globex` is declared (via `_kb_legacy`, the fixture's own setup) but
    carries no `_kb_projects` entry at all."""
    crafted_kb_projects_registry({})
    assert projects.kb_project_section_for("globex") is None


# --- rollup: an additional Chart House fold (the steward's ruling, 2026-10-22) ---


def test_rollup_absent_gives_an_empty_list():
    assert projects.rollup_for("finch") == []
    assert projects.product_rollup_for("widget") == []


def test_rollup_is_empty_for_an_unregistered_slug():
    assert projects.rollup_for("no-such-project") == []
    assert projects.product_rollup_for("no-such-product") == []


@pytest.fixture
def crafted_rollup_registry(tmp_path, monkeypatch):
    def _write(unit_rollup, *, product_rollup=None):
        unit = {
            "class": "project", "display": "Acme", "owner": "convivy",
            "home_dir": "<root>/acme", "dirs": [], "crew": "acme",
            "lifecycle": "active", "products": [], "repos": ["acme"],
            "aliases": [], "kb": "local",
        }
        if unit_rollup is not None:
            unit["rollup"] = unit_rollup
        product = {"display": "Widget", "owner": "tbd"}
        if product_rollup is not None:
            product["rollup"] = product_rollup
        raw = {"acme": unit, "_products": {"widget": product}}
        path = tmp_path / "projects.json"
        path.write_text(json.dumps(raw))
        monkeypatch.setattr(projects, "PROJECTS_JSON", path)
        projects._load_raw.cache_clear()
        return path
    yield _write
    projects._load_raw.cache_clear()


def test_rollup_accepts_both_legal_values_with_no_duplicates(crafted_rollup_registry):
    crafted_rollup_registry(["repos", "cost"], product_rollup=["cost"])
    assert projects.rollup_for("acme") == ["repos", "cost"]
    assert projects.product_rollup_for("widget") == ["cost"]


def test_rollup_refuses_a_duplicate(crafted_rollup_registry):
    crafted_rollup_registry(["cost", "cost"])
    with pytest.raises(ValueError, match="duplicate"):
        projects.rollup_for("acme")


def test_rollup_refuses_an_unknown_value(crafted_rollup_registry):
    crafted_rollup_registry(["velocity"])
    with pytest.raises(ValueError, match="unknown value"):
        projects.rollup_for("acme")


def test_product_rollup_refuses_an_unknown_value(crafted_rollup_registry):
    crafted_rollup_registry(None, product_rollup=["velocity"])
    with pytest.raises(ValueError, match="unknown value"):
        projects.product_rollup_for("widget")


# --- `_products.group` (the steward's ruling, 2026-10-22) ---


@pytest.fixture
def crafted_product_registry(tmp_path, monkeypatch):
    def _write(group):
        product = {"display": "Widget", "owner": "tbd"}
        if group is not None:
            product["group"] = group
        raw = {"_products": {"widget": product}}
        path = tmp_path / "projects.json"
        path.write_text(json.dumps(raw))
        monkeypatch.setattr(projects, "PROJECTS_JSON", path)
        projects._load_raw.cache_clear()
        return path
    yield _write
    projects._load_raw.cache_clear()


def test_product_group_absent_gives_none(crafted_product_registry):
    crafted_product_registry(None)
    assert projects.product_group_for("widget") is None


def test_product_group_is_none_for_an_unregistered_product():
    assert projects.product_group_for("no-such-product") is None


def test_product_group_accepts_a_non_empty_string(crafted_product_registry):
    crafted_product_registry("Suite")
    assert projects.product_group_for("widget") == "Suite"


def test_product_group_refuses_a_non_string(crafted_product_registry):
    crafted_product_registry(123)
    with pytest.raises(ValueError, match="group must be a non-empty string"):
        projects.product_group_for("widget")


def test_product_group_refuses_an_empty_string(crafted_product_registry):
    crafted_product_registry("")
    with pytest.raises(ValueError, match="group must be a non-empty string"):
        projects.product_group_for("widget")


# --- `code_health` (the steward's ruling, 2026-10-22) ---


@pytest.fixture
def crafted_code_health_registry(tmp_path, monkeypatch):
    def _write(code_health, repos=("acme",)):
        unit = {
            "class": "project", "display": "Acme", "owner": "convivy",
            "home_dir": "<root>/acme", "dirs": [], "crew": "acme",
            "lifecycle": "active", "products": [], "repos": list(repos),
            "aliases": [], "kb": "local",
        }
        if code_health is not None:
            unit["code_health"] = code_health
        raw = {"acme": unit}
        path = tmp_path / "projects.json"
        path.write_text(json.dumps(raw))
        monkeypatch.setattr(projects, "PROJECTS_JSON", path)
        projects._load_raw.cache_clear()
        return path
    yield _write
    projects._load_raw.cache_clear()


def test_code_health_absent_gives_an_empty_list(crafted_code_health_registry):
    crafted_code_health_registry(None)
    assert projects.code_health_repos_for("acme") == []


def test_code_health_is_empty_for_an_unregistered_slug():
    assert projects.code_health_repos_for("no-such-project") == []


def test_code_health_accepts_a_repo_in_the_units_own_repos(crafted_code_health_registry):
    crafted_code_health_registry(["acme"], repos=["acme", "acme-web"])
    assert projects.code_health_repos_for("acme") == ["acme"]


def test_code_health_refuses_a_non_list(crafted_code_health_registry):
    crafted_code_health_registry("acme", repos=["acme"])
    with pytest.raises(ValueError, match="must be a list"):
        projects.code_health_repos_for("acme")


def test_code_health_refuses_a_duplicate(crafted_code_health_registry):
    crafted_code_health_registry(["acme", "acme"], repos=["acme"])
    with pytest.raises(ValueError, match="duplicate"):
        projects.code_health_repos_for("acme")


def test_code_health_refuses_a_repo_outside_its_own_repos(crafted_code_health_registry):
    crafted_code_health_registry(["globex"], repos=["acme"])
    with pytest.raises(ValueError, match="not in its own repos"):
        projects.code_health_repos_for("acme")


# --- `code_health_repos()`: the cross-project aggregate (CQ·636) ---------------


@pytest.fixture
def crafted_multi_unit_code_health_registry(tmp_path, monkeypatch):
    """Two public units, each naming a `code_health` repo, plus a private
    unit naming one too — the registry shape `code_health_repos()` must
    aggregate over (public) and must not leak (private, ADR-0043's wall)."""
    def _write():
        raw = {
            "acme": {
                "class": "project", "display": "Acme", "owner": "convivy",
                "home_dir": "<root>/acme", "dirs": [], "crew": "acme",
                "lifecycle": "active", "products": [], "repos": ["acme", "acme-web"],
                "aliases": [], "kb": "local", "code_health": ["acme"],
            },
            "globex": {
                "class": "project", "display": "Globex", "owner": "convivy",
                "github_owner": "someowner",
                "home_dir": "<root>/globex", "dirs": [], "crew": "globex",
                "lifecycle": "active", "products": [], "repos": ["globex"],
                "aliases": [], "kb": "local", "code_health": ["globex"],
            },
            "secret": {
                "class": "project", "display": "Secret", "owner": "convivy",
                "home_dir": "<root>/secret", "dirs": [], "crew": "secret",
                "lifecycle": "active", "products": [], "repos": ["secret"],
                "aliases": [], "kb": "local", "code_health": ["secret"],
                "private": True,
            },
        }
        path = tmp_path / "projects.json"
        path.write_text(json.dumps(raw))
        monkeypatch.setattr(projects, "PROJECTS_JSON", path)
        projects._load_raw.cache_clear()
        return path
    yield _write
    projects._load_raw.cache_clear()


def test_code_health_repos_aggregates_across_public_projects(
        crafted_multi_unit_code_health_registry):
    crafted_multi_unit_code_health_registry()
    assert projects.code_health_repos() == ["convivy/acme", "someowner/globex"]


def test_code_health_repos_excludes_a_private_projects_repo(
        crafted_multi_unit_code_health_registry):
    """Red against a version with no private exclusion: `secret` names a
    `code_health` repo too, and ADR-0043's wall says none of a black-box
    crew's content reaches a shared store — a code-health capture built on
    this list must never see it."""
    crafted_multi_unit_code_health_registry()
    repos = projects.code_health_repos()
    assert "convivy/secret" not in repos
    assert "secret" not in repos


@pytest.fixture
def crafted_multi_unit_code_health_registry_with_walled_off(tmp_path, monkeypatch):
    """Like `crafted_multi_unit_code_health_registry` above, with a walled-off
    crew (platform/decisions/0100, the operator's own private use) in place of
    the private one — the second exclusion set `code_health_repos()` must
    read alongside `private_slugs()` (security-reviewer + reviewer, round 1)."""
    def _write():
        raw = {
            "acme": {
                "class": "project", "display": "Acme", "owner": "convivy",
                "home_dir": "<root>/acme", "dirs": [], "crew": "acme",
                "lifecycle": "active", "products": [], "repos": ["acme", "acme-web"],
                "aliases": [], "kb": "local", "code_health": ["acme"],
            },
            "walled-off-x": {
                "class": "project", "display": "Walled Off X", "owner": "convivy",
                "home_dir": "<root>/walled-off-x", "dirs": [], "crew": "walled-off-x",
                "lifecycle": "active", "products": [], "repos": ["walled-off-x"],
                "aliases": [], "kb": "local", "code_health": ["walled-off-x"],
                "walled_off": True,
            },
        }
        path = tmp_path / "projects.json"
        path.write_text(json.dumps(raw))
        monkeypatch.setattr(projects, "PROJECTS_JSON", path)
        projects._load_raw.cache_clear()
        return path
    yield _write
    projects._load_raw.cache_clear()


def test_code_health_repos_excludes_a_walled_off_projects_repo(
        crafted_multi_unit_code_health_registry_with_walled_off):
    """A walled-off crew's repo (platform/decisions/0100) is excluded the
    same way a private one is (see `test_code_health_repos_excludes_a_
    private_projects_repo` above); a public crew in the same registry is
    not."""
    crafted_multi_unit_code_health_registry_with_walled_off()
    repos = projects.code_health_repos()
    assert "convivy/acme" in repos
    assert "convivy/walled-off-x" not in repos
    assert "walled-off-x" not in repos


def test_code_health_repos_returns_no_repos_when_walled_off_slugs_raises(
        crafted_multi_unit_code_health_registry, monkeypatch):
    """Red against a version that reads `private_slugs()` and
    `walled_off_slugs()` as two separate steps: when the second raises, this
    must return NO repos, including `acme`'s public one — a partial
    exclusion set computed from only the half that didn't raise would
    under-exclude."""
    crafted_multi_unit_code_health_registry()
    def _boom():
        raise RuntimeError("registry unreadable")
    monkeypatch.setattr(projects, "walled_off_slugs", _boom)
    assert projects.code_health_repos() == []


def test_code_health_repos_is_sorted_and_deduplicated(tmp_path, monkeypatch):
    raw = {
        "acme": {
            "class": "project", "display": "Acme", "owner": "convivy",
            "home_dir": "<root>/acme", "dirs": [], "crew": "acme",
            "lifecycle": "active", "products": [], "repos": ["zeta", "alpha"],
            "aliases": [], "kb": "local", "code_health": ["zeta", "alpha"],
        },
    }
    path = tmp_path / "projects.json"
    path.write_text(json.dumps(raw))
    monkeypatch.setattr(projects, "PROJECTS_JSON", path)
    projects._load_raw.cache_clear()
    try:
        assert projects.code_health_repos() == ["convivy/alpha", "convivy/zeta"]
    finally:
        projects._load_raw.cache_clear()




def test_the_renamed_private_crew_resolves_and_its_old_key_is_gone():
    """The private crew that carries the operator's own name reads its slug
    from the operator config rather than a literal, so its registry row and
    `knowledge.ALLOWED_CREWS` must still agree under whatever slug that is,
    and its previous key must no longer resolve at all."""
    crew = operator_config.get("apps_crew")
    assert crew in projects.private_slugs()
    assert crew in knowledge.ALLOWED_CREWS
    # The rename kept everything after the first hyphen, dropping only the
    # operator-name prefix this slug now carries ahead of it.
    old_slug = crew.split("-", 1)[-1]
    assert old_slug not in projects.load()


ACME_STUDIO_PROJECTS = ("oak-arch-tools", "oak-etc", "oak-park-villa")


def test_acme_studio_is_three_projects_each_with_its_own_crew():
    """ARCH-426 amendment (operator 2026-07-31). The single `oak-studio` unit held one
    project against SEVERAL crews — bin/co gives each repo under the umbrella its
    own co session — which is the inverse of this registry's many-to-one relation.
    Its `crew: null` was the model reporting that conflict rather than missing data.

    The work test settles it: one co, one crew, one thing being built. Orchard Lane
    and Cedar Ridge Circle are different buildings, grouped only because they shared
    an umbrella directory."""
    assert projects.get("oak-studio") is None, "the umbrella unit is replaced, not kept"
    for slug in ACME_STUDIO_PROJECTS:
        u = projects.get(slug)
        assert u is not None, f"{slug} must be registered"
        assert u["crew"] == slug, f"{slug} owns its own co session"
        assert u["repos"] == [slug]
        assert projects.home_path(slug) == (
            projects.crews_root() / "acme" / slug).resolve()


def test_the_acme_studio_business_area_crews_resolve_under_the_shared_area_directory():
    """oak-guest and oak-marketing (2026-10-21, relayed by the steward) sit
    beside the three physical-architecture projects under the same `<root>/acme/`
    area directory, each as its own project with no commercial product of its
    own — the owner's internal leverage, not something sold."""
    for slug in ("oak-guest", "oak-marketing"):
        u = projects.get(slug)
        assert u is not None, f"{slug} must be registered"
        assert projects.home_path(slug) == (
            projects.crews_root() / "acme" / slug).resolve()
        assert u["products"] == []


def test_the_three_acme_studio_projects_all_build_the_one_acme_studio_product():
    """The Project splits three ways; the Product does not. Acme Studio is one
    commercial entity, and many-to-many is exactly what carries that."""
    for slug in ACME_STUDIO_PROJECTS:
        assert projects.products_for(slug) == ["oak-studio"]
    assert set(projects.projects_for_product("oak-studio")) == set(ACME_STUDIO_PROJECTS)
    assert projects.get_product("oak-studio")["display"] == "Acme Studio"


def test_the_acme_studio_projects_keep_their_private_by_default_posture():
    """Splitting the unit must not relax what it was cleared for: a split KB slice,
    task text not public. All three inherit the posture the single unit carried."""
    for slug in ACME_STUDIO_PROJECTS:
        u = projects.get(slug)
        assert u["class"] == "project"
        assert u["owner"] == "acme"
        assert u["kb"] == "split"
        assert u["kb_project"] == "oak-studio", "one KB slice, shared by the three"
        assert u["data_local"] is True
        assert u["task_text_public"] is False


def test_the_env_override_still_wins(monkeypatch):
    """`$PROJECTS_JSON` still names a file, read exactly as before CQ·563."""
    import projects

    monkeypatch.setenv("PROJECTS_JSON", "/tmp/tenant/projects.json")

    assert projects._registry_path() == pathlib.Path("/tmp/tenant/projects.json")


def test_the_registry_path_is_none_when_PROJECTS_JSON_is_unset(monkeypatch):
    """CQ·563: the two file fallbacks (this module's own tree, then the
    operator's checkout) are gone. Unset means "read the Library instead",
    not "find a file some other way" — `_registry_path()` says so
    unambiguously by returning `None` rather than a path nothing reads."""
    import projects

    monkeypatch.delenv("PROJECTS_JSON", raising=False)

    assert projects._registry_path() is None


# --- CQ·563: PROJECTS_JSON unset reads the registry from the Library --------


class _RegistryStub:
    """A real local HTTP server on an ephemeral port, standing in for the
    Library's `GET /registry` — records every request's path and headers,
    and answers with a fixed status/body, same shape as `test_issue_client
    .py`'s `_Listener`."""

    def __init__(self, *, status=200, body=b"{}"):
        import http.server
        import threading
        self.requests: list[dict] = []
        self.status = status
        self.body = body
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests.append({"path": self.path, "headers": dict(self.headers)})
                self.send_response(outer.status)
                self.send_header("Content-Length", str(len(outer.body)))
                self.end_headers()
                self.wfile.write(outer.body)

            def log_message(self, *a):
                pass

        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def registry_stub(monkeypatch):
    """A factory for `_RegistryStub`s: each one made is pointed at by
    `vault_plane.LIBRARY_BASE_URL` for the rest of this test and closed at
    teardown. Also repoints `projects.PROJECTS_JSON` to `None` and clears
    `_load_raw`'s cache, so a test requesting this fixture exercises the
    Library-read branch by construction, overriding the module's own
    `rich_registry` default (`_default_projects_registry` sets
    `PROJECTS_JSON` to the committed file first; this runs after and wins,
    the same way every other per-test override in this suite does)."""
    made: list[_RegistryStub] = []

    def make(**kw):
        stub = _RegistryStub(**kw)
        made.append(stub)
        monkeypatch.setattr(vault_plane, "LIBRARY_BASE_URL", stub.url)
        return stub

    monkeypatch.setattr(projects, "PROJECTS_JSON", None)
    projects._load_raw.cache_clear()
    yield make
    for s in made:
        s.close()


_SYNTHETIC_LIBRARY_REGISTRY = {
    "_meta": {"doc": "synthetic Library-served registry (CQ·563)"},
    "widget-crew": {
        "class": "project", "display": "Widget Crew", "owner": "test-owner",
        "home_dir": "<root>/widget-crew", "dirs": [], "crew": "widget-crew",
        "lifecycle": "active", "products": [], "repos": ["widget-crew"],
        "aliases": [], "kb": "local", "task_text_public": False,
    },
}


def test_projects_json_unset_reads_the_registry_from_the_library(registry_stub, monkeypatch):
    monkeypatch.setenv("AGENT_PLATFORM_REGISTRY_TOKEN", "tok-under-test")
    stub = registry_stub(body=json.dumps(_SYNTHETIC_LIBRARY_REGISTRY).encode())

    assert projects.load() == {"widget-crew": _SYNTHETIC_LIBRARY_REGISTRY["widget-crew"]}

    assert len(stub.requests) == 1
    req = stub.requests[0]
    assert req["path"] == "/registry"
    assert req["headers"]["Library-Protocol"] == vault_plane.PROTOCOL_VERSION
    assert req["headers"]["Authorization"] == "Bearer tok-under-test"


def test_an_unreachable_library_raises_and_private_slugs_raises_too(registry_stub, monkeypatch):
    monkeypatch.setenv("AGENT_PLATFORM_REGISTRY_TOKEN", "tok")
    stub = registry_stub()
    stub.close()  # the server is gone; the port now refuses every connection

    with pytest.raises(projects.RegistryUnavailable):
        projects.load()
    with pytest.raises(projects.RegistryUnavailable):
        projects.private_slugs()


def test_a_403_from_the_library_raises(registry_stub, monkeypatch):
    monkeypatch.setenv("AGENT_PLATFORM_REGISTRY_TOKEN", "tok")
    registry_stub(status=403, body=b'{"error": "forbidden: missing registry.read"}')

    with pytest.raises(projects.RegistryUnavailable):
        projects.load()
    with pytest.raises(projects.RegistryUnavailable):
        projects.private_slugs()


def test_a_503_from_the_library_raises(registry_stub, monkeypatch):
    monkeypatch.setenv("AGENT_PLATFORM_REGISTRY_TOKEN", "tok")
    registry_stub(status=503, body=b'{"error": "empty registry"}')

    with pytest.raises(projects.RegistryUnavailable):
        projects.load()
    with pytest.raises(projects.RegistryUnavailable):
        projects.private_slugs()


def test_a_200_with_a_json_array_raises(registry_stub, monkeypatch):
    monkeypatch.setenv("AGENT_PLATFORM_REGISTRY_TOKEN", "tok")
    registry_stub(status=200, body=b"[]")

    with pytest.raises(projects.RegistryUnavailable):
        projects.load()
    with pytest.raises(projects.RegistryUnavailable):
        projects.private_slugs()


def test_no_token_anywhere_raises_and_makes_no_request(tmp_path, monkeypatch):
    """Neither `AGENT_PLATFORM_REGISTRY_TOKEN` nor the token file (pointed, via
    `Path.home()`, at a directory that holds no such file) supplies a
    credential — refuses before any request is made, rather than asking the
    Library with no token at all."""
    monkeypatch.setattr(projects, "PROJECTS_JSON", None)
    projects._load_raw.cache_clear()
    monkeypatch.delenv("AGENT_PLATFORM_REGISTRY_TOKEN", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def _explode(*a, **k):
        pytest.fail("no request should be made without a token")
    monkeypatch.setattr(projects.urllib.request, "urlopen", _explode)

    with pytest.raises(projects.RegistryUnavailable):
        projects.private_slugs()


# --- CQ·563: _load_raw_live's Library-mode cache -----------------------------


def test_load_raw_live_caches_the_librarys_answer_for_60_seconds_then_refetches(
        registry_stub, monkeypatch):
    """The cache is `clock()`-timed, never slept past in a test: within the
    60s window a second call costs no request, and past it a third call
    refetches — never serving a stale answer past the window, and never
    caching nothing-fetched-yet as a hit."""
    monkeypatch.setenv("AGENT_PLATFORM_REGISTRY_TOKEN", "tok")
    stub = registry_stub(body=json.dumps(_SYNTHETIC_LIBRARY_REGISTRY).encode())
    projects._live_library_cache = None
    clock = {"t": 1_000.0}

    first = projects._load_raw_live(clock=lambda: clock["t"])
    assert first == _SYNTHETIC_LIBRARY_REGISTRY
    assert len(stub.requests) == 1

    clock["t"] += 30
    second = projects._load_raw_live(clock=lambda: clock["t"])
    assert second == _SYNTHETIC_LIBRARY_REGISTRY
    assert len(stub.requests) == 1, "within the 60s window, no refetch"

    clock["t"] += 31  # 61s past the first fetch
    third = projects._load_raw_live(clock=lambda: clock["t"])
    assert third == _SYNTHETIC_LIBRARY_REGISTRY
    assert len(stub.requests) == 2, "past the window, a call refetches"


def test_project_for_repo_returns_the_project_that_lists_the_repo():
    for slug, unit in projects.load().items():
        for repo in unit.get("repos", []):
            assert projects.project_for_repo(repo) == slug, repo


def test_project_for_repo_resolves_case_insensitively_and_by_slug_or_alias():
    assert projects.project_for_repo("Northwind-Deck") == "northwind-deck"
    assert projects.project_for_repo("bosun") == "bosun"
    aliased = next((s, u["aliases"][0]) for s, u in projects.load().items() if u.get("aliases"))
    assert projects.project_for_repo(aliased[1]) == aliased[0]


def test_project_for_repo_is_none_for_an_unregistered_or_empty_name():
    assert projects.project_for_repo("no-such-repo-anywhere") is None
    assert projects.project_for_repo("") is None
    assert projects.project_for_repo(None) is None


def test_project_for_repo_lowercases_the_registry_side_too(monkeypatch):
    """The Library's resolve lowercases both the name asked for and every
    registry name, so a mixed-case entry must match here as it does there."""
    monkeypatch.setattr(projects, "load",
                        lambda: {"demo": {"repos": ["Demo-Repo"], "aliases": ["DemoAlias"]}})
    assert projects.project_for_repo("demo-repo") == "demo"
    assert projects.project_for_repo("demoalias") == "demo"


# --- repo_owners / repo_github_slug: a unit whose repos don't share one owner ----
#
# brewcask-tap (2026-10-13): Builder's public Homebrew formula, the first repo a
# crew owns outside the convivy GitHub organization. `outfitter`'s own repo stays
# under convivy; `brewcask-tap` lives under `caskforge`.


def test_crew_for_repo_resolves_homebrew_tap_to_shipwright():
    """On the unfixed registry (brewcask-tap absent from outfitter's `repos`)
    this returned None, not `outfitter`."""
    assert projects.crew_for_repo("brewcask-tap") == "outfitter"


def test_resolve_project_resolves_homebrew_tap_to_shipwright():
    """`resolve_project`, not `project_for_repo`, is what `default_profile` calls —
    `EXPLICIT_PROJECT_BY_NAME` needs its own row for brewcask-tap even once the
    registry lists it under outfitter's `repos`, because this function never
    consults `repos`/`aliases` at all. On the unfixed table this returned None."""
    assert projects.resolve_project("brewcask-tap") == "outfitter"


def test_repo_github_slug_uses_the_per_repo_owner_when_registered():
    """The behaviour this function exists for: a repo owner, else the function
    did not exist at all on the unfixed module (AttributeError)."""
    assert projects.repo_github_slug("brewcask-tap") == "caskforge/brewcask-tap"


def test_repo_github_slug_keeps_convivy_for_a_repo_with_no_override():
    """outfitter's own repo carries no `repo_owners` entry, so it still resolves
    under convivy — behaviour for an already-registered repo must not change."""
    assert projects.repo_github_slug("outfitter") == "convivy/outfitter"


def test_repo_github_slug_uses_the_unit_github_owner_for_acme_studio_repos():
    """Round-1 review (PR #881, comment 6150937461): the three Acme Studio units each
    set the unit's github_owner, but before every call site routed through
    this function, all seven inline `convivy/<repo>` builds ignored that field
    and read `convivy/<repo>` — the wrong, 404ing owner. Each Acme Studio unit's
    `repos` holds exactly one bare name, matching the unit's own key."""
    for repo in ACME_STUDIO_PROJECTS:
        owner = projects.get(repo)["github_owner"]
        assert owner and owner != "convivy"
        assert projects.repo_github_slug(repo) == f"{owner}/{repo}"


def test_repo_github_slug_defaults_unregistered_repos_to_convivy():
    assert projects.repo_github_slug("no-such-repo-anywhere") == "convivy/no-such-repo-anywhere"


def test_repo_github_slug_returns_an_already_qualified_slug_unchanged():
    assert projects.repo_github_slug("someowner/somerepo") == "someowner/somerepo"


def test_repo_github_slug_accepts_a_caller_supplied_unit():
    """A caller that already holds the unit dict from somewhere other than
    `load()` — originally `issue_gate.py`, which resolved its own unit from a
    registry snapshot fetched over the network, since deleted; today's live
    callers are `crew_registration.py` and `check-ci-workflow-drift` —
    passing that unit in must skip the `load()` search entirely and still
    find the per-repo owner."""
    unit = {"repos": ["homebrew-tap"], "repo_owners": {"homebrew-tap": "someoneelse"}}
    assert projects.repo_github_slug("homebrew-tap", unit=unit) == "someoneelse/homebrew-tap"


def test_repo_github_slug_and_project_for_repo_on_a_two_owner_unit(monkeypatch):
    """A unit whose second repo overrides the owner: `repo_owners` wins for that
    repo, the unit's own `github_owner` still answers for its first repo, and
    `project_for_repo` maps either bare name back to the unit."""
    monkeypatch.setattr(projects, "load", lambda: {
        "synthetic-unit": {
            "github_owner": "owner-a",
            "repos": ["first-repo", "second-repo"],
            "repo_owners": {"second-repo": "owner-b"},
        },
    })
    assert projects.repo_github_slug("second-repo") == "owner-b/second-repo"
    assert projects.repo_github_slug("first-repo") == "owner-a/first-repo"
    assert projects.project_for_repo("second-repo") == "synthetic-unit"


# --- registered_repo_github_slug: the strict form for a run-writable `repo` ----
#
# PR #881 round 2 (security-reviewer, comment 6152094447): the three scorecards'
# `gh` joins read `repo` from a run's own meta.json, which the dispatched run can
# write, and built `convivy/<repo>` (or, after round 1, `repo_github_slug(repo)`)
# unconditionally — a `/`-qualified forgery pointed their queries at any
# repository. This is the bound those sites call instead.


def test_registered_repo_github_slug_resolves_a_registered_bare_name():
    assert projects.registered_repo_github_slug("brewcask-tap") == "caskforge/brewcask-tap"


def test_registered_repo_github_slug_accepts_its_own_matching_qualified_slug():
    assert (projects.registered_repo_github_slug("caskforge/brewcask-tap")
            == "caskforge/brewcask-tap")


def test_registered_repo_github_slug_refuses_a_mismatched_owner():
    """brewcask-tap's registered owner is caskforge — a `/`-qualified value
    naming a different owner for the same bare repo must come back None rather
    than pass through unchanged."""
    assert projects.registered_repo_github_slug("acme-attacker/brewcask-tap") is None
    assert projects.registered_repo_github_slug("convivy/brewcask-tap") is None


def test_registered_repo_github_slug_refuses_an_unregistered_qualified_owner():
    """An unregistered bare name still defaults to convivy (matching
    repo_github_slug, and carrying none of that function's risk — it never
    leaves the convivy org), but a forged `/`-qualified value naming some OTHER
    owner for that same unregistered name is refused."""
    assert (projects.registered_repo_github_slug("no-such-repo-anywhere")
            == "convivy/no-such-repo-anywhere")
    assert projects.registered_repo_github_slug("acme-attacker/no-such-repo-anywhere") is None


def test_registered_repo_github_slug_refuses_empty_and_none():
    assert projects.registered_repo_github_slug(None) is None
    assert projects.registered_repo_github_slug("") is None


def test_repo_owners_wellformed():
    """Schema for the optional `repo_owners` field: a dict keyed by one of the
    unit's own `repos`, each value a non-empty owner string."""
    for slug, u in projects.load().items():
        owners = u.get("repo_owners")
        if owners is None:
            continue
        assert isinstance(owners, dict), f"{slug} repo_owners must be a dict"
        for repo, owner in owners.items():
            assert repo in u.get("repos", []), (
                f"{slug} repo_owners names {repo!r}, not one of its own repos")
            assert isinstance(owner, str) and owner, (
                f"{slug}.{repo} repo_owners needs a non-empty owner string")


def test_no_repo_claims_two_different_owners_via_two_units():
    """`test_no_repo_or_alias_claimed_twice` already refuses the same repo name
    under two units at all; this confirms a `repo_owners` entry can't sidestep
    that by being the only disagreement between two hypothetical claims — a repo
    appears in exactly one unit's `repos`, so it can carry exactly one owner."""
    seen: dict[str, str] = {}
    for slug, u in projects.load().items():
        owners = u.get("repo_owners") or {}
        for repo in owners:
            assert repo not in seen, f"{repo!r} owner claimed by both {seen[repo]} and {slug}"
            seen[repo] = slug


# --- ci_workflows: which CI workflow file(s) run on a repo's pull requests ----
#
# The Library's ci_tracked.py holds a literal list of (repo, workflow file)
# pairs; this field is what it reads instead, so a bad entry here would
# silently stop a repo's CI from being tracked rather than erroring loudly.


def test_ci_workflows_wellformed():
    """Schema for the optional `ci_workflows` field, checked against the
    COMMITTED registry — the one test here that reads the real file rather
    than a temporary one."""
    for slug, u in projects.load().items():
        errors = projects.ci_workflows_errors(u)
        assert not errors, f"{slug}: {'; '.join(errors)}"


def _unit(repos, ci_workflows=None, aliases=()):
    unit = {"repos": list(repos), "aliases": list(aliases)}
    if ci_workflows is not None:
        unit["ci_workflows"] = ci_workflows
    return unit


def test_ci_workflows_errors_accepts_an_absent_field():
    assert projects.ci_workflows_errors(_unit(["acme-web"])) == []


def test_ci_workflows_errors_accepts_a_wellformed_mapping():
    unit = _unit(["acme-web", "acme-api"],
                 ci_workflows={"acme-web": ["ci.yml", "e2e.yml"], "acme-api": []})
    assert projects.ci_workflows_errors(unit) == []


def test_ci_workflows_errors_refuses_a_non_dict_value():
    unit = _unit(["acme-web"], ci_workflows=["ci.yml"])
    errors = projects.ci_workflows_errors(unit)
    assert errors and "must be a dict" in errors[0]


def test_ci_workflows_errors_refuses_a_key_not_in_repos():
    unit = _unit(["acme-web"], ci_workflows={"acme-other": ["ci.yml"]})
    errors = projects.ci_workflows_errors(unit)
    assert errors and "not one of its own repos" in errors[0]


def test_ci_workflows_errors_refuses_a_non_list_value():
    unit = _unit(["acme-web"], ci_workflows={"acme-web": "ci.yml"})
    errors = projects.ci_workflows_errors(unit)
    assert errors and "must be a list of strings" in errors[0]


def test_ci_workflows_errors_refuses_a_non_string_list_item():
    unit = _unit(["acme-web"], ci_workflows={"acme-web": ["ci.yml", 3]})
    errors = projects.ci_workflows_errors(unit)
    assert errors and "must be a list of strings" in errors[0]


@pytest.mark.parametrize("bad_name", [
    "../evil.yml", "sub/dir.yml", "ci", "ci.json", ".yml", "", "-ci.yml",
])
def test_ci_workflows_errors_refuses_a_bad_file_name(bad_name):
    unit = _unit(["acme-web"], ci_workflows={"acme-web": [bad_name]})
    errors = projects.ci_workflows_errors(unit)
    assert errors and "not a bare workflow file name" in errors[0]


@pytest.fixture
def temp_registry(tmp_path, monkeypatch):
    """Point the loader at a registry this test writes with made-up units, the
    way `crafted_registry` does — `ci_workflows()` reads through `load()`, so
    exercising its resolution needs a real (if temporary) registry file."""
    def _write(units: dict) -> Path:
        p = tmp_path / "projects.json"
        p.write_text(json.dumps(units))
        monkeypatch.setattr(projects, "PROJECTS_JSON", p)
        projects._load_raw.cache_clear()
        return p
    yield _write
    projects._load_raw.cache_clear()


def _acme_unit(repos, ci_workflows=None, aliases=(), repo_owners=None, github_owner=None):
    unit = {
        "class": "project", "display": "Acme", "owner": "convivy",
        "home_dir": "<root>/acme", "dirs": [], "crew": "acme",
        "lifecycle": "active", "products": [], "repos": list(repos),
        "aliases": list(aliases), "kb": "local",
    }
    if ci_workflows is not None:
        unit["ci_workflows"] = ci_workflows
    if repo_owners is not None:
        unit["repo_owners"] = repo_owners
    if github_owner is not None:
        unit["github_owner"] = github_owner
    return unit


def test_ci_workflows_defaults_to_ci_yml_when_the_field_is_absent(temp_registry):
    temp_registry({"acme": _acme_unit(["acme-web"])})
    assert projects.ci_workflows("acme-web") == ["ci.yml"]


def test_ci_workflows_defaults_to_ci_yml_when_the_repo_is_absent_from_the_map(temp_registry):
    temp_registry({"acme": _acme_unit(["acme-web", "acme-api"],
                                       ci_workflows={"acme-api": []})})
    assert projects.ci_workflows("acme-web") == ["ci.yml"]


def test_ci_workflows_empty_list_is_returned_unchanged(temp_registry):
    temp_registry({"acme": _acme_unit(["acme-web"], ci_workflows={"acme-web": []})})
    assert projects.ci_workflows("acme-web") == []


def test_ci_workflows_returns_two_workflows_for_one_repo(temp_registry):
    temp_registry({"acme": _acme_unit(
        ["acme-web"], ci_workflows={"acme-web": ["ci.yml", "e2e.yml"]})})
    assert projects.ci_workflows("acme-web") == ["ci.yml", "e2e.yml"]


def test_ci_workflows_defaults_to_ci_yml_for_a_repo_no_unit_lists(temp_registry):
    """No unit claims this repo at all — the registry says nothing about its
    CI, and `ci.yml` is what most repos run, so it is the default here too,
    exactly as it is for a repo a unit lists but says nothing about."""
    temp_registry({"acme": _acme_unit(["acme-web"])})
    assert projects.ci_workflows("no-such-repo-anywhere") == ["ci.yml"]


# --- ci_workflows_entry: the narrower read that tells "declared empty" apart
# from "not named" (`ci_workflows` collapses both to the `ci.yml` default) ---

def test_ci_workflows_entry_returns_the_declared_empty_list(temp_registry):
    temp_registry({"acme": _acme_unit(["acme-web"], ci_workflows={"acme-web": []})})
    assert projects.ci_workflows_entry("acme-web") == []


def test_ci_workflows_entry_returns_the_declared_non_empty_list(temp_registry):
    temp_registry({"acme": _acme_unit(["acme-web"], ci_workflows={"acme-web": ["lint.yml"]})})
    assert projects.ci_workflows_entry("acme-web") == ["lint.yml"]


def test_ci_workflows_entry_is_none_when_the_repo_is_absent_from_the_map(temp_registry):
    temp_registry({"acme": _acme_unit(["acme-web", "acme-api"],
                                       ci_workflows={"acme-api": []})})
    assert projects.ci_workflows_entry("acme-web") is None


def test_ci_workflows_entry_is_none_for_a_repo_no_unit_lists(temp_registry):
    temp_registry({"acme": _acme_unit(["acme-web"])})
    assert projects.ci_workflows_entry("no-such-repo-anywhere") is None


# --- ci_workflows_entry_for_slug: the owner/repo-qualified counterpart of
# ci_workflows_entry, for a caller (`_repo_has_ci_workflows` in bin/agentctl)
# that only has the slug `review-status --repo` takes rather than a bare name.

def test_ci_workflows_entry_for_slug_matches_the_resolved_owner_and_repo(temp_registry):
    temp_registry({"acme": _acme_unit(
        ["acme-web"], repo_owners={"acme-web": "acme-owner"},
        ci_workflows={"acme-web": []})})
    assert projects.ci_workflows_entry_for_slug("acme-owner/acme-web") == []


def test_ci_workflows_entry_for_slug_returns_the_declared_non_empty_list(temp_registry):
    temp_registry({"acme": _acme_unit(
        ["acme-web"], repo_owners={"acme-web": "acme-owner"},
        ci_workflows={"acme-web": ["lint.yml"]})})
    assert projects.ci_workflows_entry_for_slug("acme-owner/acme-web") == ["lint.yml"]


def test_ci_workflows_entry_for_slug_is_none_for_a_mismatched_owner(temp_registry):
    """The same bare repo name, registered under `acme-owner` — a slug naming
    any OTHER owner must not match this row, even though the bare name is
    identical."""
    temp_registry({"acme": _acme_unit(
        ["acme-web"], repo_owners={"acme-web": "acme-owner"},
        ci_workflows={"acme-web": ["lint.yml"]})})
    assert projects.ci_workflows_entry_for_slug("other-owner/acme-web") is None


def test_ci_workflows_entry_for_slug_is_none_when_no_row_resolves_to_it(temp_registry):
    temp_registry({"acme": _acme_unit(["acme-web"])})
    assert projects.ci_workflows_entry_for_slug("convivy/no-such-repo-anywhere") is None


def test_ci_workflows_entry_for_slug_uses_the_default_convivy_owner(temp_registry):
    """A repo with no `repo_owners` override resolves under `convivy`, the
    same default `repo_github_slug` uses for every other call site."""
    temp_registry({"acme": _acme_unit(["acme-web"], ci_workflows={"acme-web": ["ci.yml"]})})
    assert projects.ci_workflows_entry_for_slug("convivy/acme-web") == ["ci.yml"]


# --- created_by: who made a project, for the per-person visibility default --
# (issue_01M2QWFR8PCBPANF8Q5P460GHY) ------------------------------------------

_PERSON_A = "person_01ARZ3NDEKTSV4RRFFQ69G5FAV"
_PERSON_B = "person_01BX5ZZKBKACTAV9WEVGEMMVRZ"


@pytest.mark.parametrize("value,ok", [
    (_PERSON_A, True),
    (_PERSON_B, True),
    ("Jane Doe", False),            # a display name, never recorded here
    ("convivy", False),             # an organisation, what `owner` holds
    ("", False),
    ("person_short", False),        # not a 26-character ULID
    ("person_01arz3ndektsv4rrffq69g5fav", False),  # lowercase: not Crockford
    (None, False),
])
def test_valid_person_id(value, ok):
    assert projects.valid_person_id(value) is ok


def test_creator_of_a_recorded_project(monkeypatch):
    monkeypatch.setattr(projects, "load", lambda: {"demo": {"created_by": _PERSON_A}})
    assert projects.creator_of("demo") == _PERSON_A


def test_creator_of_a_registered_project_with_no_created_by(monkeypatch):
    """Unrecorded because the project predates the field — every project
    registered before the 2026-09-18 backfill ruling is exactly this case."""
    monkeypatch.setattr(projects, "load", lambda: {"demo": {"display": "Demo"}})
    assert projects.creator_of("demo") is None


def test_creator_of_an_unknown_slug(monkeypatch):
    monkeypatch.setattr(projects, "load", lambda: {})
    assert projects.creator_of("no-such-project") is None


def test_slugs_created_by_finds_every_match_sorted(monkeypatch):
    units = {
        "zeta": {"created_by": _PERSON_A},
        "alpha": {"created_by": _PERSON_A},
        "other": {"created_by": _PERSON_B},
        "unrecorded": {"display": "Unrecorded"},
    }
    monkeypatch.setattr(projects, "load", lambda: units)
    assert projects.slugs_created_by(_PERSON_A) == ["alpha", "zeta"]
    assert projects.slugs_created_by(_PERSON_B) == ["other"]


def test_slugs_created_by_is_empty_for_a_person_nobody_created_anything_for(monkeypatch):
    monkeypatch.setattr(projects, "load", lambda: {"demo": {"created_by": _PERSON_A}})
    assert projects.slugs_created_by(_PERSON_B) == []


def test_every_recorded_created_by_in_the_real_registry_is_well_formed():
    """The live registry's own `created_by` values, read through the accessor
    rather than the raw dict — the check this field's writer and the
    Library's reader both need to hold."""
    for slug in projects.load():
        creator = projects.creator_of(slug)
        if creator is not None:
            assert projects.valid_person_id(creator), f"{slug}: {creator!r}"


# --- made-up-crew accessors: bug_sweep_repos, shared_job_host, tool_callers, ----
# --- own_coverage_products, shared_coverage_products, own_ledger_paths,      ----
# --- activity_capture_excluded_repos, live_checkout_readers,                ----
# --- explainer_donors, valid_name (ARCH-0099 Convivy-name removal pass) ---------


def test_explainer_donors_orders_by_the_priority_field(monkeypatch):
    """Pinned against the shape of the OLD hardcoded tuple this accessor
    replaced — made-up names stand in for the three real donors, in the
    same relative order."""
    monkeypatch.setattr(projects, "load", lambda: {
        "acme": {"display": "Acme", "coverage": "shared", "explainer_donor_priority": 2},
        "globex": {"display": "Globex", "coverage": "shared", "explainer_donor_priority": 1},
        "initech": {"display": "Initech", "coverage": "shared", "explainer_donor_priority": 3},
    })
    assert projects.explainer_donors() == ["globex", "acme", "initech"]


def test_explainer_donors_sorts_an_absent_priority_last_by_slug(monkeypatch):
    monkeypatch.setattr(projects, "load", lambda: {
        "globex": {"display": "Globex", "coverage": "shared"},
        "acme": {"display": "Acme", "coverage": "shared", "explainer_donor_priority": 1},
        "initech": {"display": "Initech", "explainer_donor_priority": 1},
    })
    assert projects.explainer_donors() == ["acme", "globex"]


def test_valid_name_accepts_an_ordinary_bare_name():
    assert projects.valid_name("acme-backend")
    assert projects.valid_name("acme.jsonl")


def test_valid_name_refuses_a_slash_or_a_traversal_component():
    assert not projects.valid_name("../../etc/passwd")
    assert not projects.valid_name("acme/../../etc")
    assert not projects.valid_name("..")
    assert not projects.valid_name("")
    assert not projects.valid_name(None)


def test_bug_sweep_repos_defaults_to_the_slug_itself(monkeypatch):
    monkeypatch.setattr(projects, "load", lambda: {"acme": {"display": "Acme"}})
    assert projects.bug_sweep_repos("acme") == ["acme"]


def test_bug_sweep_repos_reads_the_explicit_list_sorted(monkeypatch):
    monkeypatch.setattr(projects, "load", lambda: {
        "acme": {"display": "Acme", "bug_sweep_repos": ["acme-tools", "acme"]}})
    assert projects.bug_sweep_repos("acme") == ["acme", "acme-tools"]


def test_bug_sweep_repos_for_an_unregistered_slug(monkeypatch):
    monkeypatch.setattr(projects, "load", lambda: {})
    assert projects.bug_sweep_repos("no-such-project") == ["no-such-project"]


def test_shared_job_host_is_none_when_no_unit_carries_the_flag(monkeypatch):
    monkeypatch.setattr(projects, "load", lambda: {"acme": {"display": "Acme"}})
    assert projects.shared_job_host() is None


def test_shared_job_host_returns_the_flagged_slug(monkeypatch):
    monkeypatch.setattr(projects, "load", lambda: {
        "acme": {"display": "Acme"},
        "globex": {"display": "Globex", "shared_job_host": True}})
    assert projects.shared_job_host() == "globex"


def test_tool_callers_defaults_to_empty_the_conservative_direction(monkeypatch):
    monkeypatch.setattr(projects, "load", lambda: {"acme": {"display": "Acme"}})
    assert projects.tool_callers("post-changelog") == []


def test_tool_callers_reads_each_units_declared_map_sorted(monkeypatch):
    monkeypatch.setattr(projects, "load", lambda: {
        "acme": {"display": "Acme",
                 "calls_shared_tools": {"acme": ["post-changelog"]}},
        "globex": {"display": "Globex",
                   "calls_shared_tools": {"globex-backend": ["post-changelog"],
                                           "globex": ["post-release-note"]}},
    })
    assert projects.tool_callers("post-changelog") == ["acme", "globex-backend"]
    assert projects.tool_callers("post-release-note") == ["globex"]
    assert projects.tool_callers("no-such-tool") == []


def test_tool_callers_raises_when_the_registry_is_unreadable(monkeypatch):
    """Fails closed by raising, same as every other accessor here — never an
    empty caller list indistinguishable from "nobody calls this tool"."""
    def boom():
        raise OSError("registry unreadable")
    monkeypatch.setattr(projects, "load", boom)
    with pytest.raises(OSError):
        projects.tool_callers("post-changelog")


def test_live_checkout_readers_defaults_to_empty_the_conservative_direction(monkeypatch):
    monkeypatch.setattr(projects, "load", lambda: {"acme": {"display": "Acme"}})
    assert projects.live_checkout_readers() == {}


def test_live_checkout_readers_merges_every_units_declared_repos(monkeypatch):
    monkeypatch.setattr(projects, "load", lambda: {
        "acme": {"display": "Acme", "live_checkout_readers": {
            "acme": {"reads": "served live", "adr": "ADR-0001", "jobs": ["com.acme.job"]}}},
        "globex": {"display": "Globex", "live_checkout_readers": {
            "globex": {"reads": "indexed live"}}},
    })
    assert projects.live_checkout_readers() == {
        "acme": {"reads": "served live", "adr": "ADR-0001", "jobs": ("com.acme.job",)},
        "globex": {"reads": "indexed live", "adr": None, "jobs": ()},
    }


def test_live_checkout_readers_raises_when_the_registry_is_unreadable(monkeypatch):
    def boom():
        raise OSError("registry unreadable")
    monkeypatch.setattr(projects, "load", boom)
    with pytest.raises(OSError):
        projects.live_checkout_readers()


def test_own_coverage_products_matches_only_the_own_value(monkeypatch):
    monkeypatch.setattr(projects, "load", lambda: {
        "acme": {"display": "Acme", "coverage": "shared"},
        "globex": {"display": "Globex", "coverage": "own"},
        "initech": {"display": "Initech"},
    })
    assert projects.own_coverage_products() == ["globex"]


def test_shared_coverage_products_matches_only_the_shared_value(monkeypatch):
    monkeypatch.setattr(projects, "load", lambda: {
        "acme": {"display": "Acme", "coverage": "shared"},
        "globex": {"display": "Globex", "coverage": "own"},
        "initech": {"display": "Initech"},
    })
    assert projects.shared_coverage_products() == ["acme"]


def test_own_ledger_paths_defaults_to_empty(monkeypatch):
    monkeypatch.setattr(projects, "load", lambda: {"acme": {"display": "Acme"}})
    assert projects.own_ledger_paths() == {}


def test_own_ledger_paths_reads_the_flagged_units_home(monkeypatch, tmp_path):
    monkeypatch.setattr(projects, "ROOT_OVERRIDE", tmp_path)
    monkeypatch.setattr(projects, "load", lambda: {
        "globex": {"display": "Globex", "home_dir": "<root>/globex", "own_ledger": True},
        "acme": {"display": "Acme", "home_dir": "<root>/acme"},
    })
    assert projects.own_ledger_paths() == {
        "globex": tmp_path / "globex" / "data" / "llm-spend.jsonl"}


def test_activity_capture_excluded_repos_defaults_to_empty(monkeypatch):
    monkeypatch.setattr(projects, "load", lambda: {"acme": {"display": "Acme"}})
    assert projects.activity_capture_excluded_repos() == set()


def test_activity_capture_excluded_repos_unions_every_unit(monkeypatch):
    monkeypatch.setattr(projects, "load", lambda: {
        "acme": {"display": "Acme",
                 "activity_capture_excluded_repos": ["acme-sandbox-tools"]},
        "globex": {"display": "Globex",
                   "activity_capture_excluded_repos": ["globex-sandbox-tools"]},
    })
    assert projects.activity_capture_excluded_repos() == {
        "acme-sandbox-tools", "globex-sandbox-tools"}


def test_schema_wellformed_accepts_every_new_optional_field_on_the_real_registry():
    """The real registry validates against every accessor added for this pass,
    without naming a crew in the assertion itself."""
    for slug, u in projects.load().items():
        if "coverage" in u:
            assert u["coverage"] in ("shared", "own"), f"{slug}: coverage={u['coverage']!r}"
        if "bug_sweep_repos" in u:
            assert isinstance(u["bug_sweep_repos"], list) and u["bug_sweep_repos"]
        if "shared_job_host" in u:
            assert u["shared_job_host"] is True
        if "calls_shared_tools" in u:
            assert isinstance(u["calls_shared_tools"], dict)
            for repo, tools in u["calls_shared_tools"].items():
                assert repo in u["repos"], f"{slug}: {repo!r} not in its own repos"
                assert isinstance(tools, list) and tools
        if "own_ledger" in u:
            assert u["own_ledger"] is True
        if "activity_capture_excluded_repos" in u:
            assert isinstance(u["activity_capture_excluded_repos"], list)
        if "live_checkout_readers" in u:
            assert isinstance(u["live_checkout_readers"], dict)
            for repo, row in u["live_checkout_readers"].items():
                assert isinstance(row, dict) and "reads" in row
        if "explainer_donor_priority" in u:
            assert isinstance(u["explainer_donor_priority"], int)
            assert u.get("coverage") == "shared", (
                f"{slug}: explainer_donor_priority on a non-shared-coverage unit")
    assert [s for s, u in projects.load().items() if u.get("shared_job_host") is True].__len__() <= 1


# --- activation_lanes / repo_display / sandbox_repos / _release_names -------
# (Builder 0.7.0's de-naming, an external roster's lane assignment, display
# override and no-engineering-signal marker). None of the four has a
# dedicated accessor: a caller here reads the registry directly, the way
# `repo_owners` is read. Checked by shape only, against whichever test
# registry is live — CQ·565 dropped the exact-count pin these checks used to
# carry against the real, now-deleted config/projects.json, since a fixture's
# count says nothing about production.


def test_activation_lanes_wellformed():
    """Schema for the optional `activation_lanes` field: a list of
    non-empty strings."""
    carrying = 0
    for slug, u in projects.load().items():
        lanes = u.get("activation_lanes")
        if lanes is None:
            continue
        carrying += 1
        assert isinstance(lanes, list), f"{slug} activation_lanes must be a list"
        for lane in lanes:
            assert isinstance(lane, str) and lane, (
                f"{slug} activation_lanes entries must be non-empty strings")
    assert carrying >= 1, "fixture carries no activation_lanes entry to exercise this check"


def test_repo_display_wellformed():
    """Schema for the optional `repo_display` field: a dict keyed by one of
    the unit's own `repos`, each value a non-empty display string — the same
    shape `repo_owners` already holds to."""
    carrying = 0
    for slug, u in projects.load().items():
        labels = u.get("repo_display")
        if labels is None:
            continue
        carrying += 1
        assert isinstance(labels, dict), f"{slug} repo_display must be a dict"
        for repo, label in labels.items():
            assert repo in u.get("repos", []), (
                f"{slug} repo_display names {repo!r}, not one of its own repos")
            assert isinstance(label, str) and label, (
                f"{slug}.{repo} repo_display needs a non-empty display string")
    assert carrying >= 1, "fixture carries no repo_display entry to exercise this check"


def test_sandbox_repos_wellformed():
    """Schema for the optional `sandbox_repos` field: a list of non-empty
    strings, each of which may also appear in the unit's own `repos` — the
    marker does not remove a repo from it."""
    carrying = 0
    for slug, u in projects.load().items():
        sandboxed = u.get("sandbox_repos")
        if sandboxed is None:
            continue
        carrying += 1
        assert isinstance(sandboxed, list), f"{slug} sandbox_repos must be a list"
        for repo in sandboxed:
            assert isinstance(repo, str) and repo, (
                f"{slug} sandbox_repos entries must be non-empty strings")
    assert carrying >= 1, "fixture carries no sandbox_repos entry to exercise this check"


def test_release_names_is_a_list_of_non_empty_strings():
    raw = json.loads(Path(projects.PROJECTS_JSON).read_text())
    names = raw.get("_release_names", [])
    assert isinstance(names, list)
    for name in names:
        assert isinstance(name, str) and name


def test_release_names_is_skipped_as_metadata_like_other_underscore_keys():
    """`load()` drops every top-level key starting with `_` — `_meta`,
    `_products`, `_scopes`, `_kb_legacy` and `_release_names` alike. The raw
    read is the positive control: the key really is present before `load()`
    drops it."""
    raw = json.loads(Path(projects.PROJECTS_JSON).read_text())
    assert "_release_names" in raw
    assert "_release_names" not in projects.load()


@pytest.fixture
def crafted_de_naming_registry(tmp_path, monkeypatch):
    """A registry carrying no real crew, company or person name — this
    module's own tests may not name one (config/projects.json is the one
    file that may)."""
    def _write(unit_extra=None, *, release_names=None):
        unit = {
            "class": "project", "display": "Alpha", "owner": "convivy",
            "home_dir": "<root>/alpha", "dirs": [], "crew": "alpha",
            "lifecycle": "active", "products": [],
            "repos": ["alpha", "beta-studio"], "aliases": [], "kb": "local",
        }
        if unit_extra:
            unit.update(unit_extra)
        raw = {"alpha": unit, "example-crew": dict(unit, repos=["example-crew"])}
        if release_names is not None:
            raw["_release_names"] = release_names
        path = tmp_path / "projects.json"
        path.write_text(json.dumps(raw))
        monkeypatch.setattr(projects, "PROJECTS_JSON", path)
        projects._load_raw.cache_clear()
        return path
    yield _write
    projects._load_raw.cache_clear()


def test_a_unit_lacking_the_fields_carries_none_of_them(crafted_de_naming_registry):
    crafted_de_naming_registry()
    unit = projects.load()["example-crew"]
    assert "activation_lanes" not in unit
    assert "repo_display" not in unit
    assert "sandbox_repos" not in unit


def test_a_crafted_units_fields_round_trip(crafted_de_naming_registry):
    crafted_de_naming_registry({
        "activation_lanes": ["daytime"],
        "repo_display": {"beta-studio": "Beta Studio"},
        "sandbox_repos": ["beta-studio"],
    })
    unit = projects.load()["alpha"]
    assert unit["activation_lanes"] == ["daytime"]
    assert unit["repo_display"] == {"beta-studio": "Beta Studio"}
    assert unit["sandbox_repos"] == ["beta-studio"]
    assert "beta-studio" in unit["repos"], "a sandbox_repos entry may stay in repos"


def test_a_crafted_release_names_round_trips_and_is_dropped_by_load(crafted_de_naming_registry):
    crafted_de_naming_registry(release_names=["EXAMPLE-CO"])
    raw = json.loads(Path(projects.PROJECTS_JSON).read_text())
    assert raw["_release_names"] == ["EXAMPLE-CO"]
    assert "_release_names" not in projects.load()


# --- business_umbrella_dirname / writing_guide_dirname (CQ-588: bin/co's -------
# --- umbrella and writing-guide branches, resolved by registry field rather ----
# --- than by name) ---------------------------------------------------------


@pytest.fixture
def crafted_umbrella_registry(tmp_path, monkeypatch):
    """A registry carrying only the top-level `_business_umbrella_dir` key
    `business_umbrella_dirname()` reads directly — no per-unit shape
    required, the same minimal-document approach `_release_names`'s own
    tests above use for a top-level key with no owning unit."""
    def _write(**doc):
        path = tmp_path / "projects.json"
        path.write_text(json.dumps(doc))
        monkeypatch.setattr(projects, "PROJECTS_JSON", path)
        projects._load_raw.cache_clear()
        return path
    yield _write
    projects._load_raw.cache_clear()


def test_business_umbrella_dirname_is_none_when_the_key_is_absent(crafted_umbrella_registry):
    crafted_umbrella_registry()
    assert projects.business_umbrella_dirname() is None


def test_business_umbrella_dirname_returns_a_bare_directory_name(crafted_umbrella_registry):
    crafted_umbrella_registry(_business_umbrella_dir="umbrella-dir")
    assert projects.business_umbrella_dirname() == "umbrella-dir"


@pytest.mark.parametrize("bad", (
    "umbrella/dir",
    ".umbrella-dir",
    "",
    7,
), ids=("slash", "leading-dot", "empty", "non-string"))
def test_business_umbrella_dirname_refuses_a_malformed_value(crafted_umbrella_registry, bad):
    crafted_umbrella_registry(_business_umbrella_dir=bad)
    with pytest.raises(ValueError, match="must be a bare directory name"):
        projects.business_umbrella_dirname()


def test_writing_guide_dirname_is_none_when_no_unit_carries_the_flag(crafted_registry):
    crafted_registry("<root>/widget-home")
    assert projects.writing_guide_dirname() is None


def test_writing_guide_dirname_returns_the_flagged_units_home_basename(crafted_registry):
    path = crafted_registry("<root>/widget-home")
    data = json.loads(path.read_text())
    data["evil"]["co_writing_guide"] = True
    path.write_text(json.dumps(data))
    projects._load_raw.cache_clear()
    assert projects.writing_guide_dirname() == "widget-home"


def test_writing_guide_dirname_is_none_when_the_flagged_unit_is_not_active(crafted_registry):
    path = crafted_registry("<root>/widget-home")
    data = json.loads(path.read_text())
    data["evil"].update(co_writing_guide=True, lifecycle="sunset")
    path.write_text(json.dumps(data))
    projects._load_raw.cache_clear()
    assert projects.writing_guide_dirname() is None


def test_writing_guide_dirname_selects_the_flagged_unit_among_several(tmp_path, monkeypatch):
    """Two active units, both with a valid home_dir: the unflagged one
    ("widget") sorts first in the registry's key order, the flagged one
    ("gadget") second. An accessor that returned the first active unit's
    home regardless of the flag would return "widget-home" here; only a
    real flag check returns the flagged unit's "gadget-home"."""
    def _unit(home_dir, **extra):
        return {
            "class": "project", "display": home_dir, "owner": "convivy",
            "home_dir": home_dir, "dirs": [], "crew": None,
            "lifecycle": "active", "products": [],
            "repos": [], "aliases": [], "kb": "local",
            **extra,
        }
    path = tmp_path / "projects.json"
    path.write_text(json.dumps({
        "widget": _unit("<root>/widget-home"),
        "gadget": _unit("<root>/gadget-home", co_writing_guide=True),
    }))
    monkeypatch.setattr(projects, "PROJECTS_JSON", path)
    projects._load_raw.cache_clear()
    assert projects.writing_guide_dirname() == "gadget-home"
