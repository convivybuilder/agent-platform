"""A dispatched run's own sandbox denies the real account home wholesale
(epic E33 slice 2), and the two session-scoped backstops in conftest.py list a
real-home-derived root before and after the whole test session. Under that
sandbox, listing either root raises PermissionError rather than returning an
empty or missing directory, which the backstops did not expect (a dispatched
coder's own pytest run errored at setup on every test for exactly this).

These tests exercise `_listing_or_unobservable` and `_assert_no_new_entries`
directly — the helper the two fixtures now share — rather than driving a
whole session-scoped fixture, since a fixture already ran once for this very
test session.
"""
from __future__ import annotations

import warnings

import pytest

from conftest import _assert_no_new_entries, _listing_or_unobservable


def test_a_permission_denied_root_is_skipped_with_one_warning_not_a_failure(tmp_path, monkeypatch):
    """Simulates the closed root: `iterdir` raises PermissionError the way it
    does under a dispatched run's own sandbox. The helper must warn once,
    naming the root, and return None rather than letting the PermissionError
    propagate."""
    root = tmp_path / "closed-root"
    root.mkdir()

    def _denied(self):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(type(root), "iterdir", _denied)

    with pytest.warns(UserWarning, match="cannot be listed"):
        result = _listing_or_unobservable(root, "the real test root")
    assert result is None


def test_assert_no_new_entries_is_a_no_op_once_the_root_was_already_unobservable(tmp_path):
    """`_assert_no_new_entries` must neither warn again nor raise when `before`
    is None: that None already means `_listing_or_unobservable` warned once on
    the way in, and a process that could not list the root could not have
    written under it either."""
    root = tmp_path / "closed-root"
    root.mkdir()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _assert_no_new_entries(root, "the real test root", before=None)


def test_a_readable_root_with_no_new_entry_passes(tmp_path):
    root = tmp_path / "open-root"
    root.mkdir()
    before = _listing_or_unobservable(root, "the real test root")
    assert before == set()
    _assert_no_new_entries(root, "the real test root", before)


def test_a_readable_root_that_gains_an_entry_still_fails(tmp_path):
    """The backstop keeps working as it always did wherever the root IS
    readable — only the permission-denied case is new."""
    root = tmp_path / "open-root"
    root.mkdir()
    before = _listing_or_unobservable(root, "the real test root")
    (root / "new-entry").mkdir()
    with pytest.raises(AssertionError, match="new entry"):
        _assert_no_new_entries(root, "the real test root", before)


def test_a_missing_root_is_treated_as_empty_not_unobservable(tmp_path):
    """A root that does not exist at all (never created on this machine) is
    not the PermissionError case: `_listing_or_unobservable` returns an empty
    set, exactly as the backstops always treated an absent root, and warns
    nothing."""
    root = tmp_path / "never-created"
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        result = _listing_or_unobservable(root, "the real test root")
    assert result == set()
