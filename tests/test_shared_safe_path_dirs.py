"""The bash and Python `safe_which` helpers (lib/safe-path.sh,
lib/agent-platform/safe_path.py) each keep their own literal copy of the
fixed system-directory list, on purpose (platform/decisions/0090's Handoff:
no runtime dependency between the two languages). Nothing links them, so
nothing stops the two from drifting except this test, run against the same
machine both helpers actually run on.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import safe_path as SP

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_bash_and_python_system_dirs_agree():
    python_dirs = list(SP._system_dirs())
    out = subprocess.run(
        ["/bin/bash", "-c",
         f'. "{REPO_ROOT}/lib/safe-path.sh" && _safe_path_system_dirs'],
        capture_output=True, text=True, check=True,
    )
    bash_dirs = out.stdout.strip().split(":")
    assert bash_dirs == python_dirs, (
        f"bash and Python safe_which disagree on the system directory list: "
        f"bash={bash_dirs!r} python={python_dirs!r}"
    )


# `/opt` holds exactly one entry on this machine today, so the test above
# never exercises more than a one-element sort, where order is moot. This
# calls the bash sort helper directly with names whose order differs between
# C and en_US.UTF-8 collation (round 5's finding: a bare `sort` follows
# LC_ALL/LC_COLLATE, while Python's `sorted()` is always ordinal), under a
# non-C locale, and confirms it still agrees with Python's `sorted()` on the
# same strings.
_LOCALE_ORDER_NAMES = ["/opt/Zebra/bin", "/opt/apple/bin", "/opt/_under/bin"]


def test_bash_sort_helper_matches_python_sorted_under_a_non_C_locale():
    # Mirrors the bash sibling's own guard (tests/bash/test_safe_path.sh): an
    # ungenerated en_US.UTF-8 makes `sort` silently fall back to the C locale
    # with no error, so this assertion would hold even if
    # `_safe_path_sort_colon_list` had never pinned LC_ALL=C — false
    # confidence, not a check, on a runner where the locale isn't installed.
    have_locale = subprocess.run(
        ["/bin/bash", "-c", "locale -a 2>/dev/null | grep -qi '^en_US\\.UTF-8$'"],
    ).returncode == 0
    if not have_locale:
        pytest.skip("en_US.UTF-8 not installed on this machine")
    python_order = sorted(_LOCALE_ORDER_NAMES)
    colon_list = ":".join(_LOCALE_ORDER_NAMES)
    out = subprocess.run(
        ["/bin/bash", "-c",
         f'. "{REPO_ROOT}/lib/safe-path.sh" && _safe_path_sort_colon_list "$1"',
         "_", colon_list],
        capture_output=True, text=True, check=True,
        env={**os.environ, "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"},
    )
    bash_order = out.stdout.strip(":").split(":")
    assert bash_order == python_order, (
        f"under a non-C locale, bash's sort helper disagrees with Python's "
        f"sorted(): bash={bash_order!r} python={python_order!r}"
    )
