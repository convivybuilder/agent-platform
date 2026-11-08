"""ssh_config_render (CQ-603 / E36 slice 6): render the per-crew ssh host
blocks and mosh launcher table from the registry's own `crew`/`home_dir`
fields, with no registered crew, product, project or organization name in
the rendering logic itself — every registry here is built in the test, with
invented names.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "lib" / "agent-platform"


@pytest.fixture
def render():
    spec = importlib.util.spec_from_file_location("ssh_config_render", LIB / "ssh_config_render.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# MARK: - relative_home_dir


def test_relative_home_dir_strips_the_root_prefix(render):
    assert render.relative_home_dir({"home_dir": "<root>/widget"}) == "widget"


def test_relative_home_dir_keeps_a_nested_path(render):
    assert render.relative_home_dir({"home_dir": "<root>/umbrella/widget"}) == "umbrella/widget"


def test_relative_home_dir_is_none_without_the_root_prefix(render):
    assert render.relative_home_dir({"home_dir": "/absolute/widget"}) is None


def test_relative_home_dir_is_none_when_home_dir_is_missing(render):
    assert render.relative_home_dir({}) is None


def test_relative_home_dir_is_none_for_a_non_string_home_dir(render):
    assert render.relative_home_dir({"home_dir": None}) is None


def test_relative_home_dir_is_none_for_a_bare_root_prefix(render):
    assert render.relative_home_dir({"home_dir": "<root>/"}) is None


# MARK: - sessions: the domain filter (lifecycle + a `crew` session name)


def test_sessions_yields_a_unit_with_a_crew_field(render):
    registry = {"widget": {"crew": "widget", "home_dir": "<root>/widget"}}
    assert list(render.sessions(registry)) == [("widget", registry["widget"])]


def test_sessions_skips_a_unit_with_no_session(render):
    registry = {
        "widget": {"crew": "widget", "home_dir": "<root>/widget"},
        "globex": {"home_dir": "<root>/globex"},
    }
    assert [slug for slug, _ in render.sessions(registry)] == ["widget"]


def test_sessions_skips_a_sunset_unit(render):
    registry = {
        "widget": {"crew": "widget", "home_dir": "<root>/widget", "lifecycle": "sunset"},
        "globex": {"crew": "globex", "home_dir": "<root>/globex", "lifecycle": "active"},
    }
    assert [slug for slug, _ in render.sessions(registry)] == ["globex"]


def test_sessions_skips_a_private_registry_key_and_a_non_dict_unit(render):
    registry = {
        "_scopes": ["whatever"],
        "widget": {"crew": "widget", "home_dir": "<root>/widget"},
    }
    assert [slug for slug, _ in render.sessions(registry)] == ["widget"]


# MARK: - host_block


def test_host_block_carries_the_setprofile_escape_before_ensure_server(render):
    block = render.host_block("widget", host="crew.example", style_id="widget",
                               relative_dir="widget")
    assert "Host widget\n" in block
    assert "HostName crew.example" in block
    setprofile_at = block.index("SetProfile=co-widget")
    ensure_at = block.index("session-agent ensure-server")
    assert setprofile_at < ensure_at
    assert "tmux attach -d -t widget || tmux new -s widget -c @MINI_ROOT@/widget" in block


def test_host_block_attaches_the_tmux_session_override_not_the_crew_name(render):
    block = render.host_block("widget", host="crew.example", style_id="widget",
                               relative_dir="widget", session="widget-live")
    assert "Host widget\n" in block
    assert "tmux attach -d -t widget-live || tmux new -s widget-live -c @MINI_ROOT@/widget" in block
    assert "-t widget ||" not in block


# MARK: - render_host_blocks


def test_render_host_blocks_names_every_crew_in_the_shared_host_line(render):
    entries = [{"crew": "widget", "dir": "widget", "style_id": "widget"},
               {"crew": "globex", "dir": "globex", "style_id": "globex"}]
    text = render.render_host_blocks(entries, crew_host="crew.example", ssh_user="op")
    shared_line = next(ln for ln in text.splitlines() if ln.startswith("Host "))
    assert shared_line.split() == ["Host", "mini", "widget", "globex", "crew.example"]


def test_render_host_blocks_writes_user_and_identity_once(render):
    entries = [{"crew": "widget", "dir": "widget", "style_id": "widget"}]
    text = render.render_host_blocks(entries, crew_host="crew.example", ssh_user="op")
    assert text.count("User op") == 1
    assert "IdentityFile ~/.ssh/id_ed25519_mini" in text


def test_render_host_blocks_writes_a_plain_mini_shell_alias(render):
    text = render.render_host_blocks([], crew_host="crew.example", ssh_user="op")
    assert "Host mini\n  HostName crew.example\n" in text


def test_render_host_blocks_omits_the_standby_alias_without_a_standby_host(render):
    text = render.render_host_blocks([], crew_host="crew.example", ssh_user="op",
                                      standby_alias="standby")
    assert "standby" not in text


def test_render_host_blocks_omits_the_standby_alias_without_a_standby_alias(render):
    text = render.render_host_blocks([], crew_host="crew.example", ssh_user="op",
                                      standby_host="standby.example")
    assert "standby" not in text


def test_render_host_blocks_writes_the_standby_alias_when_configured(render):
    text = render.render_host_blocks([], crew_host="crew.example", ssh_user="op",
                                      standby_host="standby.example", standby_alias="standby")
    assert "Host standby\n  HostName standby.example\n" in text
    shared_line = next(ln for ln in text.splitlines() if ln.startswith("Host "))
    assert "standby" in shared_line.split()
    assert "standby.example" in shared_line.split()


def test_render_host_blocks_names_a_standby_session_alias_on_the_shared_line(render):
    standby_entries = [{"crew": "widget", "dir": "widget", "style_id": "widget", "session": "widget"}]
    text = render.render_host_blocks([], crew_host="crew.example", ssh_user="op",
                                      standby_host="standby.example", standby_alias="standby",
                                      standby_entries=standby_entries)
    shared_line = next(ln for ln in text.splitlines() if ln.startswith("Host "))
    assert "standby-widget" in shared_line.split()
    # The standby session's own stanza is rendered by render_standby_session_blocks,
    # not here — only its name is reserved on the shared Host line.
    assert "Host standby-widget\n" not in text


def test_render_host_blocks_renders_one_block_per_entry(render):
    entries = [{"crew": "widget", "dir": "umbrella/widget", "style_id": "w"}]
    text = render.render_host_blocks(entries, crew_host="crew.example", ssh_user="op")
    assert "Host widget\n" in text
    assert "@MINI_ROOT@/umbrella/widget" in text
    assert "SetProfile=co-w" in text


def test_render_host_blocks_uses_the_session_name_not_the_crew_for_the_alias(render):
    entries = [{"crew": "gadget", "dir": "gadget", "style_id": "g", "session": "gadget-live"}]
    text = render.render_host_blocks(entries, crew_host="crew.example", ssh_user="op")
    assert "Host gadget-live\n" in text
    assert "Host gadget\n" not in text
    shared_line = next(ln for ln in text.splitlines() if ln.startswith("Host "))
    assert "gadget-live" in shared_line.split()
    assert "gadget" not in shared_line.split()


def test_render_host_blocks_names_a_standby_session_alias_by_session_not_crew(render):
    standby_entries = [{"crew": "gadget", "dir": "gadget", "style_id": "g", "session": "gadget-live"}]
    text = render.render_host_blocks([], crew_host="crew.example", ssh_user="op",
                                      standby_host="standby.example", standby_alias="standby",
                                      standby_entries=standby_entries)
    shared_line = next(ln for ln in text.splitlines() if ln.startswith("Host "))
    assert "standby-gadget-live" in shared_line.split()
    assert "standby-gadget" not in shared_line.split()


# MARK: - render_standby_session_blocks


def test_render_standby_session_blocks_runs_in_the_standby_root(render):
    entries = [{"crew": "widget", "dir": "widget", "style_id": "widget", "session": "widget"}]
    text = render.render_standby_session_blocks("standby", entries, "standby.example")
    assert "Host standby-widget\n" in text
    assert "HostName standby.example" in text
    assert "tmux attach -d -t widget || tmux new -s widget -c @STANDBY_ROOT@/widget" in text
    assert "@MINI_ROOT@" not in text


def test_render_standby_session_blocks_uses_the_tmux_session_override(render):
    entries = [{"crew": "widget", "dir": "widget", "style_id": "widget", "session": "widget-live"}]
    text = render.render_standby_session_blocks("standby", entries, "standby.example")
    assert "tmux attach -d -t widget-live || tmux new -s widget-live -c @STANDBY_ROOT@/widget" in text


def test_render_standby_session_blocks_names_the_host_line_by_session_not_crew(render):
    entries = [{"crew": "gadget", "dir": "gadget", "style_id": "g", "session": "gadget-live"}]
    text = render.render_standby_session_blocks("standby", entries, "standby.example")
    assert "Host standby-gadget-live\n" in text
    assert "Host standby-gadget\n" not in text


def test_render_standby_session_blocks_is_empty_without_an_alias(render):
    entries = [{"crew": "widget", "dir": "widget", "style_id": "widget", "session": "widget"}]
    assert render.render_standby_session_blocks(None, entries, "standby.example") == ""


def test_render_standby_session_blocks_is_empty_without_a_standby_host(render):
    entries = [{"crew": "widget", "dir": "widget", "style_id": "widget", "session": "widget"}]
    assert render.render_standby_session_blocks("standby", entries, None) == ""


def test_render_standby_session_blocks_is_empty_without_entries(render):
    assert render.render_standby_session_blocks("standby", [], "standby.example") == ""


# MARK: - render_mosh_sessions


def test_render_mosh_sessions_writes_one_pipe_delimited_line_per_entry(render):
    entries = [{"crew": "widget", "dir": "widget", "style_id": "widget"},
               {"crew": "globex", "dir": "umbrella/globex", "style_id": "g"}]
    text = render.render_mosh_sessions(entries)
    assert text.splitlines() == [
        "widget|co-widget|widget|@MINI_ROOT@/widget",
        "globex|co-g|globex|@MINI_ROOT@/umbrella/globex",
    ]


def test_render_mosh_sessions_uses_the_tmux_session_override(render):
    entries = [{"crew": "widget", "dir": "widget", "style_id": "widget", "session": "widget-live"}]
    text = render.render_mosh_sessions(entries)
    assert text.splitlines() == ["widget-live|co-widget|widget-live|@MINI_ROOT@/widget"]


def test_render_mosh_sessions_is_empty_for_no_entries(render):
    assert render.render_mosh_sessions([]) == ""
