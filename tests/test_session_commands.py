"""`session_commands.loaded_commands` — what a session can be told to do.

Against constructed directory trees rather than the real `~/.claude`, so these
say the same thing on a machine that has no commands installed as on the operator's.
The one thing measured against reality is in the module docstring: the weight
is on skills, not commands, and a version reading only `commands/` would report
almost nothing.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parent.parent / "lib" / "agent-platform"


@pytest.fixture()
def session_commands():
    """Loaded from this tree by path, the way the other session modules are —
    the deployed farm shadows `lib/` on sys.path and a test that imported by
    name would exercise main's copy instead of the branch's."""
    spec = importlib.util.spec_from_file_location(
        "session_commands", LIB / "session_commands.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def tree(tmp_path):
    """A home and a project, each with a `.claude` to fill in."""
    home = tmp_path / "home"
    project = tmp_path / "project"
    for root in (home, project):
        (root / ".claude").mkdir(parents=True)
    return home, project


def _command(root, name, body="do the thing\n"):
    path = root / ".claude" / "commands" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def _skill(root, name, description=None, declared=None):
    path = root / ".claude" / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    front = ""
    if description is not None or declared is not None:
        front = "---\n"
        if declared is not None:
            front += f"name: {declared}\n"
        if description is not None:
            front += f"description: {description}\n"
        front += "---\n"
    path.write_text(front + f"# {name}\n")
    return path


# --- what counts as invocable ------------------------------------------------


def test_a_markdown_file_under_commands_is_a_command(session_commands, tree):
    home, project = tree
    _command(project, "ship")
    out = session_commands.loaded_commands(str(project), home=str(home))
    assert [(c["name"], c["kind"], c["scope"]) for c in out["commands"]] == [
        ("ship", "command", "project")]


def test_a_directory_with_a_skill_manifest_is_a_skill(session_commands, tree):
    home, project = tree
    _skill(project, "handoff-leave", description="write a handoff")
    out = session_commands.loaded_commands(str(project), home=str(home))
    assert [(c["name"], c["kind"]) for c in out["commands"]] == [
        ("handoff-leave", "skill")]
    assert out["commands"][0]["description"] == "write a handoff"


def test_a_directory_without_a_manifest_is_not_a_skill(session_commands, tree):
    """A stray directory under `skills/` is not invocable, and listing it would
    tell the reader a command exists that does nothing."""
    home, project = tree
    (project / ".claude" / "skills" / "notes").mkdir(parents=True)
    out = session_commands.loaded_commands(str(project), home=str(home))
    assert out["commands"] == []


def test_a_nested_command_is_namespaced_by_its_directory(session_commands, tree):
    """`commands/kb/audit.md` is `/kb:audit`; reporting `/audit` would print a
    name that does not work."""
    home, project = tree
    path = project / ".claude" / "commands" / "kb" / "audit.md"
    path.parent.mkdir(parents=True)
    path.write_text("audit\n")
    out = session_commands.loaded_commands(str(project), home=str(home))
    assert out["commands"][0]["name"] == "kb:audit"


# --- scope -------------------------------------------------------------------


def test_project_entries_come_before_the_user_wide_ones(session_commands, tree):
    """The question being asked is almost always whether THIS repo's commands
    loaded, and a machine-wide list above them is in the way of the answer."""
    home, project = tree
    _command(home, "aaa-user")
    _command(project, "zzz-project")
    out = session_commands.loaded_commands(str(project), home=str(home))
    assert [c["scope"] for c in out["commands"]] == ["project", "user"]


def test_the_user_scope_is_reported_with_no_cwd_at_all(session_commands, tree):
    """A session whose directory could not be read still has the user's own
    commands, and reporting those beats reporting nothing."""
    home, _ = tree
    _command(home, "write-better")
    out = session_commands.loaded_commands(None, home=str(home))
    assert [c["name"] for c in out["commands"]] == ["write-better"]
    assert [s["scope"] for s in out["sources"]] == ["user"]


def test_sources_say_where_it_looked_and_whether_that_existed(session_commands, tmp_path):
    """"No project commands" and "there is no project .claude" are different
    answers, and a reader chasing a missing command needs to tell them apart."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    out = session_commands.loaded_commands(str(tmp_path / "nowhere"), home=str(home))
    by_scope = {s["scope"]: s for s in out["sources"]}
    assert by_scope["user"]["exists"] is True
    assert by_scope["project"]["exists"] is False


# --- frontmatter -------------------------------------------------------------


def test_a_double_quoted_folded_scalar_is_read_as_yaml_means_it(session_commands, tree):
    """The shape three of agent-platform's own skills use, because their
    descriptions contain an em dash or an embedded quote. The hand-rolled
    reader this replaced returned it garbled — literal backslashes and an
    unresolved \\u2014 — which is worse in a menu than no description at all."""
    home, project = tree
    path = project / ".claude" / "skills" / "quoted" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        '---\nname: quoted\n'
        'description: "reviewing a diff \\u2014\\\n'
        '  \\ this is the \\"project rules\\" to consult"\n---\n')
    out = session_commands.loaded_commands(str(project), home=str(home))
    assert out["commands"][0]["description"] == (
        'reviewing a diff \u2014 this is the "project rules" to consult')


def test_a_symlink_out_of_the_tree_is_listed_but_not_read(session_commands, tree, tmp_path):
    """A GLOB MATCH IS A NAME. `rglob` matches a symlink, `is_file` is true
    through it, and `read_text` returns the target's bytes — so a project from
    somewhere untrusted could point a command file at a secret.

    But dropping it was the wrong fix: the operator's own two user commands are symlinks
    into `~/Convivy/write-better/`, and the first version removed them from the operator's
    menu. What leaks is the CONTENT, not the name — and Claude Code follows the
    link and loads the command, so reporting it is simply true."""
    home, project = tree
    secret = tmp_path / "id_rsa"
    secret.write_text("---\ndescription: BEGIN PRIVATE KEY\n---\n")
    commands = project / ".claude" / "commands"
    commands.mkdir(parents=True)
    (commands / "innocent.md").symlink_to(secret)

    entry = session_commands.loaded_commands(str(project), home=str(home))["commands"][0]
    assert entry["name"] == "innocent", "the name is the answer to did-it-load"
    assert entry["description"] is None, "no bytes from the target may leave"
    assert entry["content_unread"] is True, "and the menu should be able to say why"
    assert "PRIVATE" not in str(entry)


def test_a_symlink_inside_the_tree_is_still_fine(session_commands, tree):
    """Containment, not a blanket ban: a link to a sibling command is inside
    the directory it was found in and stays listed."""
    home, project = tree
    commands = project / ".claude" / "commands"
    commands.mkdir(parents=True)
    (commands / "real.md").write_text("---\ndescription: d\n---\n")
    (commands / "alias.md").symlink_to(commands / "real.md")
    entries = session_commands.loaded_commands(str(project), home=str(home))["commands"]
    assert [c["name"] for c in entries] == ["alias", "real"]
    assert all(c["description"] == "d" for c in entries), \
        "an in-tree link is read like any other file"
    assert not any("content_unread" in c for c in entries)


def test_a_description_is_sanitised_before_it_reaches_a_renderer(session_commands, tree):
    """An untrusted project's own SKILL.md can put any bytes in `description`,
    and this is the one reader in the family that hands such text straight to a
    UI. Escape sequences are the ones that render."""
    home, project = tree
    path = project / ".claude" / "skills" / "hostile" / "SKILL.md"
    path.parent.mkdir(parents=True)
    # A YAML escape in a double-quoted scalar, which is how a real ESC gets in:
    # PyYAML refuses a raw control byte outright, so the plain-scalar form never
    # reaches this code at all. See the test below for that half.
    path.write_text('---\nname: hostile\ndescription: "\\e[31mred\\e[0m and more"\n---\n')
    description = session_commands.loaded_commands(
        str(project), home=str(home))["commands"][0]["description"]
    assert "\u001b" not in description
    assert description == "red and more"


def test_a_raw_control_byte_makes_the_whole_frontmatter_unreadable(
        session_commands, tree):
    """Fail closed, and it is the parser doing it: PyYAML rejects a raw control
    character rather than loading it. The entry still lists — the name comes
    from the directory — with no description, which is the right way round."""
    home, project = tree
    path = project / ".claude" / "skills" / "rawbyte" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nname: rawbyte\ndescription: " + chr(27) + "[31mred\n---\n")
    entry = session_commands.loaded_commands(str(project), home=str(home))["commands"][0]
    assert entry["name"] == "rawbyte"
    assert entry["description"] is None


def test_an_unreadable_skill_directory_does_not_take_the_listing_down(
        session_commands, tree):
    """The unreadable-FILE case was handled and tested; the DIRECTORY case
    raised out of `is_file()`, because that stats through the parent. One
    chmod'd skill directory lost every other entry."""
    home, project = tree
    _skill(project, "fine", description="d")
    locked = project / ".claude" / "skills" / "locked"
    (locked / "SKILL.md").parent.mkdir(parents=True, exist_ok=True)
    (locked / "SKILL.md").write_text("---\ndescription: x\n---\n")
    locked.chmod(0o000)
    try:
        names = [c["name"] for c in
                 session_commands.loaded_commands(str(project), home=str(home))["commands"]]
        assert names == ["fine"]
    finally:
        locked.chmod(0o755)


def test_a_description_wrapped_over_several_lines_is_joined(session_commands, tree):
    """Every skill this platform writes wraps its description, so a reader that
    took only the first line would truncate nearly all of them."""
    home, project = tree
    path = project / ".claude" / "skills" / "wrapped" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nname: wrapped\ndescription: one two\n  three four\n---\n")
    out = session_commands.loaded_commands(str(project), home=str(home))
    assert out["commands"][0]["description"] == "one two three four"


def test_a_file_with_no_frontmatter_has_no_description(session_commands, tree):
    home, project = tree
    _skill(project, "bare")
    assert session_commands.loaded_commands(
        str(project), home=str(home))["commands"][0]["description"] is None


def test_a_skill_whose_frontmatter_disagrees_reports_the_directory(session_commands, tree):
    """The directory name is what gets invoked, so that is what is reported —
    and the disagreement is surfaced rather than silently resolved either way."""
    home, project = tree
    _skill(project, "on-disk", description="d", declared="in-frontmatter")
    entry = session_commands.loaded_commands(str(project), home=str(home))["commands"][0]
    assert entry["name"] == "on-disk"
    assert entry["declared_name"] == "in-frontmatter"


def test_a_healthy_skill_carries_no_mismatch_field(session_commands, tree):
    home, project = tree
    _skill(project, "agrees", description="d", declared="agrees")
    entry = session_commands.loaded_commands(str(project), home=str(home))["commands"][0]
    assert "declared_name" not in entry


# --- it must not fail on a directory it cannot read --------------------------


def test_an_unreadable_file_still_reports_the_command(session_commands, tree):
    """The name is what the reader needs; a description that cannot be read is
    worth less than a listing that stops at the first bad file."""
    home, project = tree
    path = _command(project, "locked")
    path.chmod(0o000)
    try:
        out = session_commands.loaded_commands(str(project), home=str(home))
        assert [c["name"] for c in out["commands"]] == ["locked"]
        assert out["commands"][0]["description"] is None
    finally:
        path.chmod(0o644)


def test_no_claude_directory_anywhere_is_an_empty_list_not_an_error(session_commands, tmp_path):
    out = session_commands.loaded_commands(str(tmp_path / "a"), home=str(tmp_path / "b"))
    assert out["commands"] == []
    assert all(s["exists"] is False for s in out["sources"])


# --- a description too long for a menu item ----------------------------------


def test_a_long_description_is_cut_on_a_word_boundary(session_commands, tree):
    """An earlier cap chopped `endpoint-family-defense-coverage` — shipping in
    two repos today — mid-word inside "router". A menu item ending "…a rout…"
    reads as corruption rather than as elision."""
    home, project = tree
    _skill(project, "wordy", description="the quick brown fox " * 40)
    description = session_commands.loaded_commands(
        str(project), home=str(home))["commands"][0]["description"]
    assert description.endswith("…")
    assert not description.endswith("bro…")
    assert len(description) <= session_commands._MAX_DESCRIPTION + 1


def test_the_longest_description_shipping_today_is_not_cut(session_commands, tree):
    """345 characters, and the cap is 400 so it survives whole. Pinned because
    the previous cap was chosen by guess and a real file disproved it."""
    home, project = tree
    # Prose, not a 345-character run of one letter: `sanitise` redacts long
    # unbroken runs as credential-shaped, so that fixture would test the
    # redactor rather than the cap.
    prose = ("when auditing a route or reviewing a diff that touches one, "
             "so the shape of the defence is checked against the family it "
             "belongs to rather than in isolation, which is how a gap in one "
             "endpoint survives a review of its neighbour ")
    long = (prose * 3)[:345]
    _skill(project, "long", description=long)
    description = session_commands.loaded_commands(
        str(project), home=str(home))["commands"][0]["description"]
    assert description == long.strip()
    assert not description.endswith("…")


def test_text_with_no_space_in_range_still_gets_cut(session_commands):
    """A description in a language this does not space-separate has no word
    boundary to find, and returning it whole would defeat the cap."""
    out = session_commands._ellipsised("a" * 60, 40)
    assert out == "a" * 40 + "…"


def test_a_symlinked_user_command_still_lists(session_commands, tree, tmp_path):
    """The regression this closes, in the shape it actually took: both of the operator's
    user-wide commands are symlinks into a repo outside `~/.claude`, and a
    containment check that dropped them removed the only two commands the operator has.
    The menu telling the operator there are none is the failure it exists to prevent."""
    home, project = tree
    elsewhere = tmp_path / "write-better"
    elsewhere.mkdir()
    (elsewhere / "write-better.md").write_text("---\ndescription: a style pass\n---\n")
    commands = home / ".claude" / "commands"
    commands.mkdir(parents=True)
    (commands / "write-better.md").symlink_to(elsewhere / "write-better.md")

    entries = session_commands.loaded_commands(str(project), home=str(home))["commands"]
    assert [c["name"] for c in entries] == ["write-better"]
    assert entries[0]["content_unread"] is True


def test_a_symlinked_skill_directory_out_of_tree_lists_without_reading(
        session_commands, tree, tmp_path):
    """The DIRECTORY case, which the command-file tests above do not reach.
    `_readable` checks the manifest path, and `resolve()` canonicalises every
    component — so a symlinked `skills/<name>` is caught by the same gate. That
    held only by inspection until here; reviewer noted the PR claimed it was
    verified when no test said so."""
    home, project = tree
    outside = tmp_path / "elsewhere" / "hostile"
    outside.mkdir(parents=True)
    (outside / "SKILL.md").write_text("---\ndescription: BEGIN PRIVATE KEY\n---\n")
    skills = project / ".claude" / "skills"
    skills.mkdir(parents=True)
    (skills / "innocent").symlink_to(outside)

    entry = session_commands.loaded_commands(str(project), home=str(home))["commands"][0]
    assert entry["name"] == "innocent"
    assert entry["kind"] == "skill"
    assert entry["description"] is None
    assert entry["content_unread"] is True
    assert "PRIVATE" not in str(entry)


def test_a_symlinked_skill_directory_inside_the_tree_is_read(session_commands, tree):
    """And the legitimate half: a skill directory linked to a sibling stays
    inside the tree, so it is read like any other."""
    home, project = tree
    _skill(project, "real", description="a real one")
    skills = project / ".claude" / "skills"
    (skills / "alias").symlink_to(skills / "real")

    entries = session_commands.loaded_commands(str(project), home=str(home))["commands"]
    assert [c["name"] for c in entries] == ["alias", "real"]
    assert all(c["description"] == "a real one" for c in entries)
    assert not any("content_unread" in c for c in entries)


# --- Builder's own deploy area, a skill's second trusted root ----------------


def test_a_skill_linked_into_builders_deploy_area_is_read(
        session_commands, tree, monkeypatch):
    """A Builder install places every skill as a link OUT of `~/.claude/skills`
    into Shipwright's deploy area on purpose — `~/.claude/skills/new-crew` ->
    `~/.local/deploy/builder-payload/skills/new-crew` — so the containment
    check must admit that one target as well as the tree itself, or every
    skill on such a machine reports `content_unread` and Deck's menu has no
    descriptions at all (the bug this closes)."""
    home, project = tree
    monkeypatch.setattr(session_commands.builder_root, "real_account_home",
                         lambda: home)
    deployed = home / ".local" / "deploy" / "builder-payload" / "skills" / "new-crew"
    deployed.mkdir(parents=True)
    (deployed / "SKILL.md").write_text("---\ndescription: create a new crew\n---\n")
    skills = project / ".claude" / "skills"
    skills.mkdir(parents=True)
    (skills / "new-crew").symlink_to(deployed)

    entry = session_commands.loaded_commands(str(project), home=str(home))["commands"][0]
    assert entry["name"] == "new-crew"
    assert entry["description"] == "create a new crew"
    assert "content_unread" not in entry


def test_a_skill_linked_outside_both_base_and_the_deploy_area_stays_unread(
        session_commands, tree, monkeypatch):
    """The trusted root is `builder-payload/skills` specifically, not the whole
    `~/.local/deploy/` tree every installer shares, and not even the whole
    `builder-payload` payload — a link one directory over stays unread just
    like one anywhere else on the machine."""
    home, project = tree
    monkeypatch.setattr(session_commands.builder_root, "real_account_home",
                         lambda: home)
    sibling = session_commands._builder_deploy_skills_root().parent / "not-skills" / "new-crew"
    sibling.mkdir(parents=True)
    (sibling / "SKILL.md").write_text("---\ndescription: BEGIN PRIVATE KEY\n---\n")
    skills = project / ".claude" / "skills"
    skills.mkdir(parents=True)
    (skills / "innocent").symlink_to(sibling)

    entry = session_commands.loaded_commands(str(project), home=str(home))["commands"][0]
    assert entry["name"] == "innocent"
    assert entry["description"] is None
    assert entry["content_unread"] is True
    assert "PRIVATE" not in str(entry)


def test_a_skill_linked_into_write_betters_deploy_area_is_read(
        session_commands, tree, monkeypatch):
    """A second Shipwright installer, `bin/link-commands`, links skills such as
    `write-better` and `talk-better` out of `~/.claude/skills` into its own
    deploy area — `~/.local/deploy/write-better/skills/write-better` — rather
    than `builder-payload`'s. That target must be admitted too, or every skill
    that installer places reports `content_unread` on a real Builder install
    (PR #697 fixed only the `builder-payload` root)."""
    home, project = tree
    monkeypatch.setattr(session_commands.builder_root, "real_account_home",
                         lambda: home)
    deployed = home / ".local" / "deploy" / "write-better" / "skills" / "write-better"
    deployed.mkdir(parents=True)
    (deployed / "SKILL.md").write_text("---\ndescription: tighten prose\n---\n")
    skills = project / ".claude" / "skills"
    skills.mkdir(parents=True)
    (skills / "write-better").symlink_to(deployed)

    entry = session_commands.loaded_commands(str(project), home=str(home))["commands"][0]
    assert entry["name"] == "write-better"
    assert entry["description"] == "tighten prose"
    assert "content_unread" not in entry


def test_a_skill_linked_outside_both_deploy_areas_stays_unread(
        session_commands, tree, monkeypatch):
    """The trusted roots are `builder-payload/skills` and `write-better/skills`
    specifically, not the whole `~/.local/deploy/` tree every installer
    shares — a link into a sibling such as `<deploy>/other/skills` is still
    listed without being read."""
    home, project = tree
    monkeypatch.setattr(session_commands.builder_root, "real_account_home",
                         lambda: home)
    sibling = home / ".local" / "deploy" / "other" / "skills" / "new-crew"
    sibling.mkdir(parents=True)
    (sibling / "SKILL.md").write_text("---\ndescription: BEGIN PRIVATE KEY\n---\n")
    skills = project / ".claude" / "skills"
    skills.mkdir(parents=True)
    (skills / "innocent").symlink_to(sibling)

    entry = session_commands.loaded_commands(str(project), home=str(home))["commands"][0]
    assert entry["name"] == "innocent"
    assert entry["description"] is None
    assert entry["content_unread"] is True
    assert "PRIVATE" not in str(entry)


def test_a_deploy_area_named_through_an_environment_variable_is_not_trusted(
        session_commands, tree, monkeypatch):
    """`install-payload` itself honours `BUILDER_DEPLOY_DIR` to redirect where
    IT writes, for verifying a branch before it ships. This containment check
    must not follow that override — the passwd home is the only anchor —
    or anything able to set the variable for session-agent's own process
    could widen what gets read."""
    home, project = tree
    monkeypatch.setattr(session_commands.builder_root, "real_account_home",
                         lambda: home)
    redirected = home / "elsewhere-via-env" / "skills" / "new-crew"
    redirected.mkdir(parents=True)
    (redirected / "SKILL.md").write_text("---\ndescription: create a new crew\n---\n")
    monkeypatch.setenv("BUILDER_DEPLOY_DIR", str(redirected.parent.parent))
    skills = project / ".claude" / "skills"
    skills.mkdir(parents=True)
    (skills / "new-crew").symlink_to(redirected)

    entry = session_commands.loaded_commands(str(project), home=str(home))["commands"][0]
    assert entry["name"] == "new-crew"
    assert entry["description"] is None
    assert entry["content_unread"] is True
