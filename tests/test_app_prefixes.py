"""app_prefixes.internal_app_prefixes() — the fail-closed empty-tuple degrade
every caller (bin/agentctl, tool_policy.py, review_protocol.py) depends on
rather than taking a crash out of `strip_app_prefix`/`internal_app_prefixes`."""
from __future__ import annotations

import app_prefixes
import projects


def test_internal_app_prefixes_degrades_to_empty_on_os_error(monkeypatch):
    def _raise():
        raise OSError("no such file")
    monkeypatch.setattr(projects, "all_agent_prefixes", _raise)
    assert app_prefixes.internal_app_prefixes() == ()


def test_internal_app_prefixes_degrades_to_empty_on_value_error(monkeypatch):
    def _raise():
        raise ValueError("malformed agent_prefix entry")
    monkeypatch.setattr(projects, "all_agent_prefixes", _raise)
    assert app_prefixes.internal_app_prefixes() == ()


def test_internal_app_prefixes_degrades_to_empty_when_the_library_is_unreachable(monkeypatch):
    """CQ·563: an unset `PROJECTS_JSON` reads the Library, which raises
    `projects.RegistryUnavailable` (a `RuntimeError`) rather than `OSError` on
    every way that read can fail. Uncaught here, that would propagate out of
    `strip_app_prefix` and take down every caller on a Library outage."""
    def _raise():
        raise projects.RegistryUnavailable("GET /registry returned 503")
    monkeypatch.setattr(projects, "all_agent_prefixes", _raise)
    assert app_prefixes.internal_app_prefixes() == ()


def test_internal_app_prefixes_returns_the_registrys_value_on_success(monkeypatch):
    monkeypatch.setattr(projects, "all_agent_prefixes", lambda: ("acme-", "globex-"))
    assert app_prefixes.internal_app_prefixes() == ("acme-", "globex-")
