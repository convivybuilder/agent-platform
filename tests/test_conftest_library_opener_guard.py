"""PR #801's CI run (37586286188): ~20 `default_profile` tests reached the
real Library through `issue_client.pinned_opener()`, which never passes
through `urllib.request.urlopen` — so conftest.py's `_no_live_ops_writes`
guard, which patches only `urlopen`, never saw the call. On the hosted
runner (no real credential) that surfaced as a loud `FileNotFoundError`; on a
machine with the real `~/.orient-control/agent-platform-token` it would have
been a silent read of that credential and a real request to the real
Library.

These tests exercise `conftest._guard_pinned_opener` directly, the way
`test_conftest_farm_guard.py` exercises `_NoFarmPath`, rather than only
through the `_no_live_pinned_opener_calls` fixture every other test already
runs under.
"""
from __future__ import annotations

import urllib.request

import pytest

from conftest import _guard_pinned_opener


class _FakeOpener:
    """Stands in for the real `OpenerDirector` `pinned_opener()` returns."""

    def __init__(self):
        self.opened: list[str] = []

    def open(self, req, timeout=None):
        self.opened.append(req.full_url)
        return "reached"


def test_a_call_to_the_librarys_fixed_address_is_refused_not_sent():
    real = _FakeOpener()
    attempted: list[str] = []
    guarded = _guard_pinned_opener(lambda: real, attempted)()
    req = urllib.request.Request("http://127.0.0.1:8100/profiles")

    with pytest.raises(RuntimeError, match="LIVE Library"):
        guarded.open(req)

    assert attempted == ["http://127.0.0.1:8100/profiles"]
    assert real.opened == [], "the real opener must never see the refused request"


def test_a_call_that_swallows_exceptions_still_lands_in_attempted():
    """`get_item_from_default_library`'s own docstring: "Never raises" — it
    catches bare `Exception`, which would absorb the guard's RuntimeError into
    an `"error:RuntimeError"` result with nothing failing the test. `attempted`
    is what the fixture checks at teardown instead of trusting the raise to
    propagate."""
    real = _FakeOpener()
    attempted: list[str] = []
    guarded = _guard_pinned_opener(lambda: real, attempted)()
    req = urllib.request.Request("http://127.0.0.1:8100/items/issue_X")

    try:
        guarded.open(req)
    except Exception:
        pass

    assert attempted == ["http://127.0.0.1:8100/items/issue_X"]


def test_a_local_test_listener_on_a_different_port_still_reaches_the_real_opener():
    """An ephemeral-port local HTTP server (`tests/test_crew_doctor.py`'s and
    `tests/test_issue_client.py`'s `listeners` fixtures) is not the Library's
    fixed address, and must pass through unguarded."""
    real = _FakeOpener()
    guarded = _guard_pinned_opener(lambda: real, [])()
    req = urllib.request.Request("http://127.0.0.1:54321/profiles")

    assert guarded.open(req) == "reached"
    assert real.opened == ["http://127.0.0.1:54321/profiles"]
