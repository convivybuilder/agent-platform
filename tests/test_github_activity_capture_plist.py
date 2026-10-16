"""launchd/com.convivy.github-activity-capture.plist (E25,
issue_01M4ZB5C5ZX0MV206064MTRAE0) — this job's own 180-second interval and
its `RunAtLoad` posture.

The broader fleet-wide plist invariants (deploy-pinned paths, no tunable
flags, no credential-shaped env key, the env-key allowlist) already cover
every `launchd/*.plist` by glob, including this one, in
tests/test_plists_are_deploy_pinned.py and tests/test_plist_no_tunables.py.
This file pins the two facts specific to this job's own plist, the same way
tests/test_pr_state_capture.py's own `test_plist_start_interval_is_180_seconds`
pins its sibling's.
"""
from __future__ import annotations

import plistlib
from pathlib import Path

PLIST = (Path(__file__).resolve().parent.parent / "launchd"
         / "com.convivy.github-activity-capture.plist")


def test_plist_start_interval_is_180_seconds():
    """GitHub-derived activity must land within 3 minutes, the pace the
    crews work at (the operator, 2026-10-15, relayed by the steward) — the
    same interval bin/pr-state-capture's plist states this reasoning for.
    StartInterval is seconds, not minutes, so this is 180, not 3."""
    with PLIST.open("rb") as fh:
        data = plistlib.load(fh)
    assert data["StartInterval"] == 180


def test_plist_run_at_load_is_false():
    """A launchd job that fires every 180s must not also fire the moment it
    is loaded — RunAtLoad true would double-fire on every `launchctl
    bootstrap`/machine restart, immediately re-reading whatever the previous
    pass just captured."""
    with PLIST.open("rb") as fh:
        data = plistlib.load(fh)
    assert data["RunAtLoad"] is False
