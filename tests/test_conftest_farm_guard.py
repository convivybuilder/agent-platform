"""issue_01M3P6AQS6EFX2MCP8WMKEH82P: `sys.path` itself must refuse the deployed
~/.local/lib/agent-platform farm for the whole process, not just at the moments
conftest.py's own pin loop happens to unshadow it.

Unlike the failure this guards (`test_session_crews.py::
test_an_unreadable_registry_serves_nothing`, which only shows red on a machine
where the farm exists at all — never in CI), these tests exercise `_NoFarmPath`
directly by string equality, so they catch a regression to a plain `sys.path`
on every machine, farm or no farm.
"""
from __future__ import annotations

import sys

from conftest import _DEPLOYED, _NoFarmPath


def test_inserting_the_deployed_path_is_a_no_op():
    guarded = _NoFarmPath(["/already/here"])
    guarded.insert(0, _DEPLOYED)
    assert _DEPLOYED not in guarded
    assert guarded == ["/already/here"]


def test_inserting_any_other_path_still_works():
    guarded = _NoFarmPath(["/already/here"])
    guarded.insert(0, "/some/other/lib")
    assert guarded == ["/some/other/lib", "/already/here"]


def test_the_live_sys_path_is_the_guarded_type():
    """Proves the object protecting the real suite is this class, not a
    same-shaped stand-in — a future refactor that reassigns `sys.path` back to
    a plain `list` before all nested imports have run would fail this."""
    assert isinstance(sys.path, _NoFarmPath)


def test_a_nested_import_mid_suite_still_cannot_reinsert_the_farm():
    """The whole point (per conftest.py's own comment) is that this holds for
    the REST OF THE PROCESS, not just during the pin loop — so re-check it
    here, well after collection, against the real `sys.path`."""
    before = list(sys.path)
    sys.path.insert(0, _DEPLOYED)
    assert _DEPLOYED not in sys.path
    assert sys.path == before
