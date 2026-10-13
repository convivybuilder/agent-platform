"""Drift guard for the project registry (ARCH-358, config/projects.json).

The registry records the product/project units in one place; today the same facts
also live in scattered consumer lists. Phase 1's contract is that this test fails
red the moment a consumer disagrees with the registry, so the registry stays the
canonical record instead of decaying into a stale doc. The assertions cover only
the clean correspondences — where a consumer keys on the same unit dimension the
registry models. The deliberately-excluded seams (repo-level task-text entries,
KB organizational buckets) are noted at each check.
"""
import json
import pathlib
from pathlib import Path

import pytest

import cost_join
import knowledge
import projects


def test_schema_wellformed():
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
        # ARCH-426 fields
        assert isinstance(u["home_dir"], str) and u["home_dir"], f"{slug} needs a home_dir"
        assert isinstance(u["dirs"], list), f"{slug} dirs must be a list"
        assert u["lifecycle"] in projects.LIFECYCLES, (
            f"{slug} has bad lifecycle {u['lifecycle']!r}")
        assert isinstance(u["products"], list), f"{slug} products must be a list"
        assert u["crew"] is None or isinstance(u["crew"], str), (
            f"{slug} crew must be a slug or null")
        if "crew_cockpit" in u:
            assert u["crew_cockpit"] is True, (
                f"{slug} crew_cockpit must be true or absent, never a redundant "
                f"false — bin/co's `crew_cockpit_dirs()` reads the FIELD'S "
                f"presence as the registry's own absent-means-off convention")
        if "agent_prefix" in u:
            assert isinstance(u["agent_prefix"], str) and u["agent_prefix"], (
                f"{slug} agent_prefix must be a non-empty string when present")
        if "created_by" in u:
            assert projects.valid_person_id(u["created_by"]), (
                f"{slug} created_by {u['created_by']!r} is not a Library person id "
                f"(person_<ULID>)")


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


def test_crew_cockpit_dirs_matches_the_field(crafted_registry):
    """`crew_cockpit_dirs()` (issue_01M3YN6857H5J541G73Y9P3KA1) names exactly
    the active crews whose row sets `crew_cockpit: true` — bin/co appends its
    answer to `_co_crew_dirs` with no edit to that file."""
    path = crafted_registry("<root>/evil")
    data = json.loads(path.read_text())
    data["evil"]["crew_cockpit"] = True
    data["sunset-cockpit"] = dict(data["evil"])
    data["sunset-cockpit"].update(home_dir="<root>/sunset-cockpit", lifecycle="sunset",
                                   repos=["sunset-cockpit"])
    path.write_text(json.dumps(data))
    projects._load_raw.cache_clear()
    assert projects.crew_cockpit_dirs() == ["evil"]


# --- kb_scope_exceptions: the registry's record of a deliberate narrowing --------
#
# issue_01M3V1J89CG19ZGD3JY4WFY7V0: `EXPLICIT_PROJECT_BY_NAME`
# (lib/agent-platform/projects.py) narrows eight repos below their project's own
# `kb_project`, each on a security review or a structural finding recorded in
# epic comment 4425 (issue_01M2NYBBY0FXCDHWWD8XBR9RR3) or, for constance-updates,
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


def test_kb_scope_exceptions_cover_exactly_the_recorded_crews():
    """The crew-creation epic's done-clause 1 names seven crews as recorded KEEP
    exceptions (epic comment 4425, issue_01M2NYBBY0FXCDHWWD8XBR9RR3); vivy-landing
    is the eighth, by orchestrator ruling (2026-10-02). Pinned so a new entry, or a
    missing one, surfaces here rather than only in the epic thread."""
    expected = {
        "knowledge", "convivy-app-kit", "convivy-services", "www",
        "shipwright", "constance-landing", "constance-updates", "vivy-landing",
    }
    assert set(projects.kb_scope_exceptions()) == expected


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
    """Demonstrates the guard the issue asks for: strip `constance-landing`'s
    recorded exception from a copy of the registry, and the agreement check
    catches the resulting drift — exactly the failure mode the issue describes,
    which on main at f629857a nothing caught at all because the field did not
    exist."""
    units = {k: dict(v) for k, v in projects.load().items()}
    units["constance"] = dict(units["constance"])
    units["constance"]["kb_scope_exceptions"] = {
        k: v for k, v in units["constance"]["kb_scope_exceptions"].items()
        if k != "constance-landing"
    }
    with pytest.raises(AssertionError, match=r"constance-landing.*no kb_scope_exceptions"):
        _assert_table_and_registry_agree(projects.EXPLICIT_PROJECT_BY_NAME, units)


def test_adding_a_narrowing_literal_entry_with_no_registry_exception_fails():
    """The other direction: a new `EXPLICIT_PROJECT_BY_NAME` row that narrows a
    repo below its project's `kb_project`, with nothing recorded in the registry
    to back it, is caught rather than shipped silent. `selector` is not one of
    the eight recorded exceptions, so overriding its row to disagree with its
    project's own kb_project is exactly an unrecorded narrowing."""
    table = dict(projects.EXPLICIT_PROJECT_BY_NAME)
    table["selector"] = "platform"
    with pytest.raises(AssertionError, match=r"selector.*no kb_scope_exceptions"):
        _assert_table_and_registry_agree(table, projects.load())


def test_registry_and_literal_disagree_on_the_same_repos_kb_project_fails():
    """A third drift shape, distinct from the other two: both sides already
    record an exception for `constance-landing`, so neither "removed" check
    above fires, but the literal's `kb_project` has been edited to a value the
    registry's own exception does not match. Direction 1 of the agreement
    check is what catches this — a value edited on one side without the
    other, not an entry dropped entirely."""
    table = dict(projects.EXPLICIT_PROJECT_BY_NAME)
    table["constance-landing"] = "commercial"
    with pytest.raises(
        AssertionError,
        match=r"narrows 'constance-landing'.*EXPLICIT_PROJECT_BY_NAME has 'commercial'",
    ):
        _assert_table_and_registry_agree(table, projects.load())


# --- ADR-0043: black box crews ----------------------------------------------------


def test_a_private_crew_cannot_also_be_centrally_indexed():
    """A BLACK BOX must not be private in name and readable in fact.

    `private: true` says none of this crew's content reaches the Library and none of
    its agents reach Orient. Three other fields have to agree for that to be true, and
    nothing but this test makes them: `kb: local` keeps it out of `kb_slugs()` and so
    out of the shared index, `task_text_public: false` keeps its task strings off the
    public surfaces, and `overnight: false` keeps it out of the shared autonomous drain.

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
    assert projects.registry_home("~/Convivy/CFO") == projects.registry_home("<root>/CFO") \
        == tmp_path / "root" / "CFO"


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


def test_svc_is_the_live_two_directory_project():
    """The case the feature exists for: agents working in convivy-services wrote to
    that repo's inbox and the svc co, watching convivy-app-kit's, never saw them."""
    (link, target), = projects.inbox_links("svc")
    # NOT resolved: `link` is the path the symlink LIVES at, and once the link
    # exists on disk `.resolve()` follows it through to convivy-app-kit — so the
    # expectation quietly became "the link points at itself", which is the one
    # thing it must not do. Green in CI, where neither directory exists, and red
    # on the machine that actually has the link. inbox_links computes without
    # touching the filesystem, so the assertion should not either.
    assert link == projects.crews_root() / "convivy-services/inbox"
    assert (link.parent / target).resolve() == (
        projects.crews_root() / "convivy-app-kit/inbox").resolve()


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
    """The two dimensions are separate namespaces. They may share a slug (`vivy`
    the project builds `vivy` the product), so this asserts the BLOCK is separate,
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


def test_lifecycle_partitions_the_registry():
    all_slugs = set(projects.load())
    partitioned = set()
    for state in projects.LIFECYCLES:
        partitioned |= set(projects.slugs_by_lifecycle(state))
    assert partitioned == all_slugs, "every project must carry a known lifecycle"
    assert set(projects.active_slugs()) <= all_slugs


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


def test_aliases_fold_to_display():
    """Every alias must fold to the unit's display via cost_join — the fold table
    (nyh-* → NYH Studio, pa/personal-assistant → Hollis) agrees with the registry.
    Primary repos aren't asserted here: cost_join only folds the alias families and
    returns other repo strings (…-commercial, …-landing) unchanged by design."""
    for slug, u in projects.load().items():
        for alias in u.get("aliases", []):
            got = cost_join._canonical_product(alias)
            assert got == u["display"], (
                f"{slug}: cost_join folds {alias!r} to {got!r}, registry says {u['display']!r}"
            )


def test_public_task_slugs_subset_of_allowlist(agent_activity_report):
    """Units cleared for public task text must be on agent-activity-report's
    allowlist. Subset, not equality: the allowlist also carries repo-level entries
    (…-commercial, substrate, write-better) that aren't top-level registry units."""
    allow = agent_activity_report._PUBLIC_TASK_PRODUCTS
    missing = projects.public_task_slugs() - allow
    assert not missing, f"registry marks {missing} task_text_public but they're off the allowlist"


# Allowlist entries that no registry unit claims as a slug, repo or alias. They are
# bare repo names cleared on their own merits (PR #92's security review), so the
# ownership check below cannot speak to them. Listed explicitly so a NEW one has to
# be added here on purpose rather than slipping past unnoticed.
KNOWN_UNCLAIMED = {"convivy-commercial"}


def test_no_allowlist_entry_belongs_to_a_non_public_project(agent_activity_report):
    """The REVERSE direction, and the one that actually protects anything.

    `_PUBLIC_TASK_PRODUCTS` is the enforcement gate — `_is_data_local` keys on it
    directly — while the registry's `task_text_public` is what `public_task_slugs`
    reads. The subset check above only catches a project marked public that is NOT
    on the gate, which fails CLOSED and merely under-publishes. The dangerous
    direction is the opposite: a repo still on the gate whose project is marked
    NOT public keeps publishing task text the registry says is private, and the
    subset check passes the whole time.

    Found on 2026-08-06 (PR #374, reviewer): the Keel fold set `task_text_public`
    false and left `agent-platform` and `substrate` on the allowlist, so the PR
    claimed a reduction the code did not make."""
    allow = agent_activity_report._PUBLIC_TASK_PRODUCTS
    # An allowlist entry may be a registry slug, a repo name or an alias, because
    # telemetry tags by repo basename. Resolve each to its owning unit; entries
    # that resolve to nothing are repo-level names no unit claims (…-commercial),
    # which this test has nothing to say about.
    leaking, unresolved = {}, set()
    for entry in allow:
        owners = [slug for slug, u in projects.load().items()
                  if entry in {slug, *u.get("repos", []), *u.get("aliases", [])}]
        if not owners:
            unresolved.add(entry)
            continue
        for slug in owners:
            if projects.get(slug).get("task_text_public") is not True:
                leaking[entry] = slug
    assert not leaking, (
        f"on the public task-text allowlist but owned by a project the registry "
        f"marks NOT task_text_public: {leaking}")
    # An entry claimed by no registry unit is a bare repo name, and the loop above
    # has nothing to say about it. Enumerate those rather than passing over them in
    # silence: the check is only as strong as the set it can resolve, so a NEW
    # unresolved entry should have to be added here deliberately (reviewer, round 2).
    # If a project later claims one of these repo names, the leak check above starts
    # covering it and this set is what says nobody was surprised.
    assert unresolved <= KNOWN_UNCLAIMED, (
        f"new allowlist entries belong to no registry unit, so nothing checks whether "
        f"they should be public: {unresolved - KNOWN_UNCLAIMED}")


def test_kb_slugs_are_registered_projects():
    """Every unit with a central/split KB slice must be in the KB validator's
    ALLOWED_PROJECTS (the agent-platform mirror), or its docs silently won't index."""
    missing = projects.kb_slugs() - knowledge.ALLOWED_PROJECTS
    assert not missing, f"kb slices {missing} absent from knowledge.ALLOWED_PROJECTS"


def test_allowed_crews_is_the_registrys_crew_set():
    """`binds_crews` is validated against knowledge.ALLOWED_CREWS, and a session's
    crew is its project's `crew` in this registry, so the two must be one set.
    Both directions fail differently: a registry crew missing from the set refuses
    a real block loudly, while a name in the set that is no crew validates a block
    that binds no session, which nothing reports."""
    registry = {u["crew"] for u in projects.load().values() if u.get("crew")}
    assert not registry - knowledge.ALLOWED_CREWS, (
        f"registry crews absent from knowledge.ALLOWED_CREWS, so a block binding them "
        f"is refused: {sorted(registry - knowledge.ALLOWED_CREWS)}")
    assert not knowledge.ALLOWED_CREWS - registry, (
        f"knowledge.ALLOWED_CREWS names that are no registry crew, so a block binding "
        f"them validates and binds nothing: {sorted(knowledge.ALLOWED_CREWS - registry)}")


NYH_PROJECTS = ("nyh-arch-tools", "nyh-etc", "nyh-park-villa")


def test_nyh_is_three_projects_each_with_its_own_crew():
    """ARCH-426 amendment (operator 2026-07-31). The single `nyh-studio` unit held one
    project against SEVERAL crews — bin/co gives each repo under the umbrella its
    own co session — which is the inverse of this registry's many-to-one relation.
    Its `crew: null` was the model reporting that conflict rather than missing data.

    The work test settles it: one co, one crew, one thing being built. Park Villa
    and East Texas Circle are different buildings, grouped only because they shared
    an umbrella directory."""
    assert projects.get("nyh-studio") is None, "the umbrella unit is replaced, not kept"
    for slug in NYH_PROJECTS:
        u = projects.get(slug)
        assert u is not None, f"{slug} must be registered"
        assert u["crew"] == slug, f"{slug} owns its own co session"
        assert u["repos"] == [slug]
        assert projects.home_path(slug) == (
            projects.crews_root() / "nyh-architecture" / slug).resolve()


def test_the_three_nyh_projects_all_build_the_one_nyh_studio_product():
    """The Project splits three ways; the Product does not. NYH Studio is one
    commercial entity, and many-to-many is exactly what carries that."""
    for slug in NYH_PROJECTS:
        assert projects.products_for(slug) == ["nyh-studio"]
    assert set(projects.projects_for_product("nyh-studio")) == set(NYH_PROJECTS)
    assert projects.get_product("nyh-studio")["display"] == "NYH Studio"


def test_the_nyh_projects_keep_their_private_by_default_posture():
    """Splitting the unit must not relax what it was cleared for: a split KB slice,
    task text not public, no shared overnight drain. All three inherit the posture
    the single unit carried."""
    for slug in NYH_PROJECTS:
        u = projects.get(slug)
        assert u["class"] == "project"
        assert u["owner"] == "nyh"
        assert u["kb"] == "split"
        assert u["kb_project"] == "nyh-studio", "one KB slice, shared by the three"
        assert u["data_local"] is True
        assert u["task_text_public"] is False
        assert u["overnight"] is False


def _fake_module_at(tmp_path, *, with_config: bool):
    """A stand-in projects.py three levels below a tree root, with or without a
    `config/projects.json` beside it — the two shapes that matter: a checkout
    (or symlink-resolved checkout) has one, a copy deploy does not."""
    mod = tmp_path / "lib" / "agent-platform" / "projects.py"
    mod.parent.mkdir(parents=True)
    mod.write_text("")
    if with_config:
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "projects.json").write_text("{}")
    return mod


def test_the_modules_own_tree_wins_when_it_holds_a_registry(tmp_path, monkeypatch):
    """Step 2, and the case that keeps this portable. In-repo runs, CI (where
    the checkout is at $HOME/work/...), and a contributor's clone all
    resolve their OWN file. An earlier revision hardcoded the operator's path
    unconditionally and broke CI outright."""
    import projects

    mod = _fake_module_at(tmp_path, with_config=True)
    monkeypatch.delenv("PROJECTS_JSON", raising=False)
    monkeypatch.setattr(projects, "__file__", str(mod))

    assert projects._registry_path() == tmp_path / "config" / "projects.json"


def test_a_copy_deploy_falls_back_to_the_checkout_rather_than_reading_a_pinned_copy(
        tmp_path, monkeypatch):
    """Step 3, and the whole privacy argument.

    A copy deploy has no `config/` — neither installer archives it, deliberately
    — so the tree-relative lookup finds nothing and the operator's checkout
    answers instead. What must never happen is the middle case this replaced: a
    deployed COPY of the registry, which parses fine and is indistinguishable
    from a current one while being as old as the last install.

    `run-register-api` filters `/runs` for Library and `/sessions` for Orient
    through `private_slugs()`, so a stale answer here serves a just-marked-private
    project as PUBLIC. `private_slugs()`'s docstring: a privacy control must not
    fail open.
    """
    import projects

    mod = _fake_module_at(tmp_path, with_config=False)
    monkeypatch.delenv("PROJECTS_JSON", raising=False)
    monkeypatch.setattr(projects, "__file__", str(mod))

    import builder_root

    resolved = projects._registry_path()
    # Step 3 is the machine's root (company/decisions/0008), which the resolver
    # reads from the passwd home and CREWS_ROOT rather than from $HOME.
    assert resolved == builder_root.root() / "agent-platform" / "config" / "projects.json"
    assert tmp_path not in resolved.parents, (
        "a deploy tree must never answer this; that is the stale read")


def test_the_env_override_still_wins(tmp_path, monkeypatch):
    """Step 1, for a packaged tenant instance."""
    import projects

    mod = _fake_module_at(tmp_path, with_config=True)
    monkeypatch.setattr(projects, "__file__", str(mod))
    monkeypatch.setenv("PROJECTS_JSON", "/tmp/tenant/projects.json")

    assert projects._registry_path() == pathlib.Path("/tmp/tenant/projects.json")


def test_project_for_repo_returns_the_project_that_lists_the_repo():
    for slug, unit in projects.load().items():
        for repo in unit.get("repos", []):
            assert projects.project_for_repo(repo) == slug, repo


def test_project_for_repo_resolves_case_insensitively_and_by_slug_or_alias():
    assert projects.project_for_repo("Convivy-Deck") == "convivy-deck"
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
