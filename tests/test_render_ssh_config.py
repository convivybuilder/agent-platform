"""bin/render-ssh-config (CQ-603 / E36 slice 6): the I/O wrapper around
ssh_config_render — validates each unit's crew/home_dir/style id shape, then
writes the generated ssh host blocks and mosh launcher table atomically, and
regenerates both fully on every run. Invented crew names only.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def render_ssh_config():
    path = REPO / "bin" / "render-ssh-config"
    loader = importlib.machinery.SourceFileLoader("render_ssh_config", str(path))
    spec = importlib.util.spec_from_loader("render_ssh_config", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


# MARK: - the happy path: one block per unit with a session


def test_render_writes_one_host_block_per_unit_with_a_session(render_ssh_config, tmp_path):
    registry = {
        "widget": {"crew": "widget", "home_dir": "<root>/widget"},
        "globex": {"home_dir": "<root>/globex"},  # no crew: no session
    }
    out = render_ssh_config.render(registry, "crew.example", "op", None, tmp_path)
    hosts = (out / "hosts.conf").read_text()
    assert "Host widget\n" in hosts
    assert "globex" not in hosts
    mosh = (out / "mosh-sessions.conf").read_text()
    assert mosh.splitlines() == ["widget|co-widget|widget|@MINI_ROOT@/widget"]


def test_render_uses_crew_not_slug_for_the_alias(render_ssh_config, tmp_path):
    registry = {"widget-secondary-repo": {"crew": "widget-crew", "home_dir": "<root>/widget"}}
    out = render_ssh_config.render(registry, "crew.example", "op", None, tmp_path)
    hosts = (out / "hosts.conf").read_text()
    assert "Host widget-crew\n" in hosts
    assert "Host widget-secondary-repo\n" not in hosts


def test_render_uses_the_style_id_over_the_slug(render_ssh_config, tmp_path):
    registry = {"widget-secondary-repo": {"crew": "widget-crew", "home_dir": "<root>/widget",
                                           "co_style_id": "widget"}}
    out = render_ssh_config.render(registry, "crew.example", "op", None, tmp_path)
    hosts = (out / "hosts.conf").read_text()
    assert "SetProfile=co-widget\\a" in hosts


def test_render_uses_tmux_session_over_crew_for_the_alias_and_attach_target(render_ssh_config, tmp_path):
    registry = {"widget": {"crew": "widget", "home_dir": "<root>/widget",
                            "tmux_session": "widget-live"}}
    out = render_ssh_config.render(registry, "crew.example", "op", None, tmp_path)
    hosts = (out / "hosts.conf").read_text()
    assert "Host widget-live\n" in hosts
    assert "Host widget\n" not in hosts
    assert "tmux attach -d -t widget-live || tmux new -s widget-live" in hosts
    assert "-t widget ||" not in hosts
    mosh = (out / "mosh-sessions.conf").read_text()
    assert mosh.splitlines() == ["widget-live|co-widget|widget-live|@MINI_ROOT@/widget"]


def test_a_unit_with_a_malformed_tmux_session_is_skipped(render_ssh_config, tmp_path, capsys):
    registry = {
        "widget": {"crew": "widget", "home_dir": "<root>/widget"},
        "globex": {"crew": "globex", "home_dir": "<root>/globex", "tmux_session": "not a valid name"},
    }
    out = render_ssh_config.render(registry, "crew.example", "op", None, tmp_path)
    hosts = (out / "hosts.conf").read_text()
    assert "Host widget\n" in hosts
    assert "globex" not in hosts
    err = capsys.readouterr().err
    assert "globex" in err
    assert "tmux_session" in err


def test_render_writes_no_leftover_temp_files(render_ssh_config, tmp_path):
    registry = {"widget": {"crew": "widget", "home_dir": "<root>/widget"}}
    render_ssh_config.render(registry, "crew.example", "op", None, tmp_path)
    assert list(tmp_path.rglob(".*.tmp")) == []


def test_a_rerun_replaces_rather_than_appends(render_ssh_config, tmp_path):
    first = {"widget": {"crew": "widget", "home_dir": "<root>/widget"},
             "globex": {"crew": "globex", "home_dir": "<root>/globex"}}
    render_ssh_config.render(first, "crew.example", "op", None, tmp_path)
    second = {"widget": {"crew": "widget", "home_dir": "<root>/widget"}}
    out = render_ssh_config.render(second, "crew.example", "op", None, tmp_path)
    hosts = (out / "hosts.conf").read_text()
    assert "Host widget\n" in hosts
    assert "globex" not in hosts


# MARK: - a missing host value skips the dependent block, named on stderr


def test_a_missing_standby_host_skips_only_the_standby_alias(render_ssh_config, tmp_path, capsys):
    registry = {"widget": {"crew": "widget", "home_dir": "<root>/widget"}}
    out = render_ssh_config.render(registry, "crew.example", "op", None, tmp_path)
    hosts = (out / "hosts.conf").read_text()
    assert "Host widget\n" in hosts
    assert "standby" not in hosts
    err = capsys.readouterr().err
    assert "standby_tailnet_host" in err


def test_a_missing_crew_host_skips_the_whole_render(render_ssh_config, tmp_path, capsys):
    registry = {"widget": {"crew": "widget", "home_dir": "<root>/widget"}}
    out = render_ssh_config.render(registry, None, "op", None, tmp_path)
    assert (out / "hosts.conf").read_text() == ""
    assert (out / "mosh-sessions.conf").read_text() == ""
    err = capsys.readouterr().err
    assert "tailnet_host" in err


def test_a_missing_ssh_user_skips_the_whole_render(render_ssh_config, tmp_path, capsys):
    registry = {"widget": {"crew": "widget", "home_dir": "<root>/widget"}}
    out = render_ssh_config.render(registry, "crew.example", None, None, tmp_path)
    assert (out / "hosts.conf").read_text() == ""
    err = capsys.readouterr().err
    assert "ssh_user" in err


def test_a_configured_standby_host_alone_skips_the_standby_block(render_ssh_config, tmp_path, capsys):
    registry = {"widget": {"crew": "widget", "home_dir": "<root>/widget"}}
    out = render_ssh_config.render(registry, "crew.example", "op",
                                    "standby.example", tmp_path)
    hosts = (out / "hosts.conf").read_text()
    assert "standby" not in hosts
    err = capsys.readouterr().err
    assert "standby_alias" in err


def test_a_configured_standby_host_and_alias_renders_the_standby_plain_shell(render_ssh_config, tmp_path):
    registry = {"widget": {"crew": "widget", "home_dir": "<root>/widget"}}
    out = render_ssh_config.render(registry, "crew.example", "op",
                                    "standby.example", tmp_path, standby_alias="standby")
    hosts = (out / "hosts.conf").read_text()
    assert "Host standby\n  HostName standby.example" in hosts
    assert (out / "standby-sessions.conf").read_text() == ""
    assert (out / "standby-host").read_text() == ""


def test_an_absent_standby_alias_skips_the_standby_block_with_a_message(render_ssh_config, tmp_path, capsys):
    registry = {"widget": {"crew": "widget", "home_dir": "<root>/widget"}}
    out = render_ssh_config.render(registry, "crew.example", "op",
                                    "standby.example", tmp_path, standby_sessions=["widget"])
    hosts = (out / "hosts.conf").read_text()
    assert "standby" not in hosts
    assert (out / "standby-sessions.conf").read_text() == ""
    err = capsys.readouterr().err
    assert "standby_alias" in err


def test_a_standby_session_renders_its_own_block_and_the_standby_host_file(render_ssh_config, tmp_path):
    registry = {"widget": {"crew": "widget", "home_dir": "<root>/widget"}}
    out = render_ssh_config.render(registry, "crew.example", "op",
                                    "standby.example", tmp_path,
                                    standby_alias="standby", standby_sessions=["widget"])
    hosts = (out / "hosts.conf").read_text()
    shared_line = next(ln for ln in hosts.splitlines() if ln.startswith("Host "))
    assert "standby-widget" in shared_line.split()
    standby_sessions_text = (out / "standby-sessions.conf").read_text()
    assert "Host standby-widget\n  HostName standby.example" in standby_sessions_text
    assert "@STANDBY_ROOT@/widget" in standby_sessions_text
    assert (out / "standby-host").read_text() == "standby.example\n"


def test_a_standby_session_naming_no_such_crew_is_skipped_with_a_message(render_ssh_config, tmp_path, capsys):
    registry = {"widget": {"crew": "widget", "home_dir": "<root>/widget"}}
    out = render_ssh_config.render(registry, "crew.example", "op",
                                    "standby.example", tmp_path,
                                    standby_alias="standby", standby_sessions=["no-such-crew"])
    assert (out / "standby-sessions.conf").read_text() == ""
    assert (out / "standby-host").read_text() == ""
    err = capsys.readouterr().err
    assert "no-such-crew" in err


# MARK: - a malformed crew, home_dir or style id is skipped, warned about,
# and does not abort the rest of the render


def test_a_unit_with_a_malformed_crew_is_skipped_with_a_warning(render_ssh_config, tmp_path, capsys):
    registry = {
        "widget": {"crew": "widget", "home_dir": "<root>/widget"},
        "globex": {"crew": "not a valid name", "home_dir": "<root>/globex"},
    }
    out = render_ssh_config.render(registry, "crew.example", "op", None, tmp_path)
    hosts = (out / "hosts.conf").read_text()
    assert "Host widget\n" in hosts
    assert "globex" not in hosts
    err = capsys.readouterr().err
    assert "globex" in err
    assert "crew" in err


def test_a_unit_with_a_home_dir_escaping_the_root_is_skipped(render_ssh_config, tmp_path, capsys):
    registry = {
        "widget": {"crew": "widget", "home_dir": "<root>/widget"},
        "globex": {"crew": "globex", "home_dir": "<root>/../escape"},
    }
    out = render_ssh_config.render(registry, "crew.example", "op", None, tmp_path)
    hosts = (out / "hosts.conf").read_text()
    assert "Host widget\n" in hosts
    assert "globex" not in hosts
    err = capsys.readouterr().err
    assert "globex" in err
    assert "home_dir" in err


def test_a_unit_with_a_home_dir_not_in_root_relative_form_is_skipped(render_ssh_config, tmp_path, capsys):
    registry = {
        "widget": {"crew": "widget", "home_dir": "<root>/widget"},
        "globex": {"crew": "globex", "home_dir": "/absolute/elsewhere"},
    }
    out = render_ssh_config.render(registry, "crew.example", "op", None, tmp_path)
    hosts = (out / "hosts.conf").read_text()
    assert "globex" not in hosts
    err = capsys.readouterr().err
    assert "globex" in err
    assert "home_dir" in err


def test_a_unit_with_a_malformed_style_id_is_skipped(render_ssh_config, tmp_path, capsys):
    registry = {
        "widget": {"crew": "widget", "home_dir": "<root>/widget"},
        "globex": {"crew": "globex", "home_dir": "<root>/globex", "co_style_id": "../escape"},
    }
    out = render_ssh_config.render(registry, "crew.example", "op", None, tmp_path)
    hosts = (out / "hosts.conf").read_text()
    assert "globex" not in hosts
    err = capsys.readouterr().err
    assert "globex" in err
    assert "style id" in err


def test_two_units_sharing_a_crew_name_keep_only_the_first(render_ssh_config, tmp_path, capsys):
    registry = {
        "widget": {"crew": "shared", "home_dir": "<root>/widget"},
        "globex": {"crew": "shared", "home_dir": "<root>/globex"},
    }
    out = render_ssh_config.render(registry, "crew.example", "op", None, tmp_path)
    hosts = (out / "hosts.conf").read_text()
    assert hosts.count("Host shared\n") == 1
    assert "@MINI_ROOT@/widget" in hosts
    assert "@MINI_ROOT@/globex" not in hosts
    err = capsys.readouterr().err
    assert "globex" in err


# MARK: - dedup keys on the SESSION name (tmux_session, else crew), not crew
# alone — two units CAN share a crew, as long as their sessions differ


def test_two_units_sharing_a_crew_with_different_tmux_sessions_render_two_aliases(
        render_ssh_config, tmp_path):
    registry = {
        "widget": {"crew": "widget", "home_dir": "<root>/widget"},
        "widget-secondary": {"crew": "widget", "home_dir": "<root>/widget-secondary",
                              "tmux_session": "widget-secondary"},
    }
    out = render_ssh_config.render(registry, "crew.example", "op", None, tmp_path)
    hosts = (out / "hosts.conf").read_text()
    # Both units share `crew: widget`, but the second names its own
    # `tmux_session`, so the two must render as two DISTINCT `Host` lines —
    # not two `Host widget` blocks, where the second would shadow the first
    # and the laptop would lose the `widget-secondary` alias entirely.
    assert hosts.count("Host widget\n") == 1
    assert hosts.count("Host widget-secondary\n") == 1
    shared_line = next(ln for ln in hosts.splitlines() if ln.startswith("Host "))
    assert "widget" in shared_line.split()
    assert "widget-secondary" in shared_line.split()
    assert "tmux attach -d -t widget || tmux new -s widget -c @MINI_ROOT@/widget" in hosts
    assert ("tmux attach -d -t widget-secondary || tmux new -s widget-secondary "
            "-c @MINI_ROOT@/widget-secondary") in hosts
    mosh = (out / "mosh-sessions.conf").read_text()
    assert mosh.splitlines() == [
        "widget|co-widget|widget|@MINI_ROOT@/widget",
        "widget-secondary|co-widget-secondary|widget-secondary|@MINI_ROOT@/widget-secondary",
    ]


def test_two_units_whose_session_names_collide_still_skip_the_second(
        render_ssh_config, tmp_path, capsys):
    registry = {
        "widget": {"crew": "widget", "home_dir": "<root>/widget"},
        "globex": {"crew": "globex", "home_dir": "<root>/globex", "tmux_session": "widget"},
    }
    out = render_ssh_config.render(registry, "crew.example", "op", None, tmp_path)
    hosts = (out / "hosts.conf").read_text()
    assert hosts.count("Host widget\n") == 1
    assert "globex" not in hosts
    err = capsys.readouterr().err
    assert "globex" in err
    assert "session" in err
    assert "widget" in err


def test_a_standby_sessions_entry_naming_a_session_that_differs_from_its_crew_renders(
        render_ssh_config, tmp_path):
    registry = {"widget": {"crew": "widget", "home_dir": "<root>/widget",
                            "tmux_session": "widget-live"}}
    out = render_ssh_config.render(registry, "crew.example", "op", "standby.example", tmp_path,
                                    standby_alias="standby", standby_sessions=["widget-live"])
    hosts = (out / "hosts.conf").read_text()
    shared_line = next(ln for ln in hosts.splitlines() if ln.startswith("Host "))
    assert "standby-widget-live" in shared_line.split()
    assert "standby-widget" not in shared_line.split()
    standby_text = (out / "standby-sessions.conf").read_text()
    assert "Host standby-widget-live\n" in standby_text
    assert "Host standby-widget\n" not in standby_text
    assert ("tmux attach -d -t widget-live || tmux new -s widget-live "
            "-c @STANDBY_ROOT@/widget") in standby_text
