"""farm_check — detect and describe drift between the checkout and its symlink farm.

~/.local/lib/agent-platform/ is a per-file symlink farm built by install.sh phase 2
(now delegated to lib/link-farm.sh). A PR that adds a new lib module keeps that
module unlinked until link-farm.sh or install.sh runs, because the farm only links
what existed when it last ran.

This module compares the checkout directory (lib/agent-platform/) against the farm
(~/.local/lib/agent-platform/) and reports:

  * NEW modules   — a file in the checkout that has no corresponding farm entry
  * DEAD links    — a farm symlink pointing at a file that no longer exists
  * WRONG targets — a farm symlink pointing into a different checkout root

It also exposes an auto-close path: when there are new modules and no WRONG
target, it applies the new links by calling lib/link-farm.sh. A DEAD link does
not block that — it is inert, and coupling the two once held three wanted
modules out of the farm for three days. Dead links still report and still
alert; clearing one is not part of the automated job, because the unattended
tick must not delete from its own deployed library.

CLI usage:
  python3 lib/agent-platform/farm_check.py             # exit 0 on clean, 1 on drift
  python3 lib/agent-platform/farm_check.py --fix        # auto-link new modules
  python3 lib/agent-platform/farm_check.py --verbose    # print each finding

Exit code:
  0 — farm matches checkout, or --fix closed ALL of the drift
  1 — drift remains. That includes a --fix run that linked the new modules
      while a DEAD link survived: linking does not clear a dead link, so the
      farm still does not match and the exit code must keep saying so.

OVERRIDE ENVIRONMENT (used in tests):
  CHECKOUT=</path>    — repo root (default: derived from __file__)
  FARM=</path>        — farm directory (default: ~/.local/lib/agent-platform)
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


# ---- defaults ---------------------------------------------------------------

_CHECKOUT = Path(os.environ.get("CHECKOUT", Path(__file__).resolve().parent.parent.parent))
_CHECKOUT_LIB = _CHECKOUT / "lib" / "agent-platform"
_FARM = Path(os.environ.get("FARM", str(Path.home() / ".local" / "lib" / "agent-platform")))


# ---- types ------------------------------------------------------------------

@dataclass
class Drift:
    new_modules: list[str] = field(default_factory=list)
    dead_links: list[str] = field(default_factory=list)
    wrong_targets: list[tuple[str, str, str]] = field(default_factory=list)
    # wrong_targets: (link_name, expected_resolved_target, actual_resolved_target)

    @property
    def is_clean(self) -> bool:
        return not (self.new_modules or self.dead_links or self.wrong_targets)

    @property
    def has_only_new_modules(self) -> bool:
        return bool(self.new_modules) and not (self.dead_links or self.wrong_targets)

    @property
    def can_auto_link(self) -> bool:
        """New modules may be linked — a WRONG target is the only thing that stops it.

        DEAD AND WRONG ARE DIFFERENT RISKS AND MUST NOT SHARE A GATE. A wrong
        target means the farm resolves into a DIFFERENT checkout: the farm's
        provenance is in doubt, and adding more links from this checkout mixes
        two of them. That is the 2026-07-30 incident, where 38 of 40 links came
        to point at a feature-branch worktree, and refusing everything is right.

        A dead link is INERT. It resolves to nothing and an import through it
        raises ModuleNotFoundError. It records that a module was deleted from
        the checkout; it says nothing about whether linking a DIFFERENT, wanted
        module is safe.

        Coupling them cost three modules three days. `techdebt.py` was deleted
        from lib/ on 2026-09-15 and its farm symlink stayed behind, so from that
        moment the old gate was false and `crew_doctor.py`, `github_app.py` and
        `shell_families.py` could not link. The autopull logged
        `ACTION NEEDED: farm drift` 1,645 times naming the same four files, and
        a correct alert nobody can close is one that gets ignored.

        A dead link still reports and still alerts. What it no longer does is
        hold wanted modules out of the deployed library. Clearing it is not
        part of the automated job (the operator, 2026-09-17): the point is to make the
        alert actionable, not to have the machine delete from its own
        deployed library unattended.
        """
        return bool(self.new_modules) and not self.wrong_targets


# ---- helpers ----------------------------------------------------------------

def _symlink_target_exists(link: Path) -> bool:
    """True when the symlink's target exists on disk. A broken symlink returns False."""
    try:
        raw = os.readlink(str(link))
    except OSError:
        return False
    target = Path(raw)
    if not target.is_absolute():
        target = link.parent / target
    return target.exists()


def _resolved_symlink_target(link: Path) -> str | None:
    """Return the resolved absolute path a symlink points at.

    Does NOT check existence — use _symlink_target_exists for that.
    Returns None only if the link itself cannot be read.
    """
    try:
        raw = os.readlink(str(link))
    except OSError:
        return None
    target = Path(raw)
    if not target.is_absolute():
        target = link.parent / target
    try:
        return str(target.resolve())
    except (OSError, RuntimeError):
        return None


# ---- check ------------------------------------------------------------------

def check(checkout_lib: Path | None = None, farm: Path | None = None) -> Drift:
    """Compare the checkout's lib/agent-platform/ against the farm.

    Args:
        checkout_lib: override checkout lib dir (default: CHECKOUT_LIB)
        farm: override farm dir (default: FARM)

    Returns:
        A Drift record. Always returns — callers check .is_clean / .can_auto_link.
    """
    if checkout_lib is None:
        checkout_lib = _CHECKOUT_LIB
    if farm is None:
        farm = _FARM

    drift = Drift()
    if not checkout_lib.is_dir():
        return drift

    checkout_root = str(checkout_lib.resolve())

    # Expected modules from the checkout (only .py files).
    expected: dict[str, Path] = {}
    for src in sorted(checkout_lib.glob("*.py")):
        expected[src.name] = src

    # Actual entries in the farm.
    actual: dict[str, Path] = {}
    if farm.is_dir():
        for entry in farm.iterdir():
            if entry.is_symlink() or entry.is_file():
                actual[entry.name] = entry

    # NEW: in checkout but absent from the farm.
    for name in expected:
        if name not in actual:
            drift.new_modules.append(name)

    # DEAD LINK + WRONG TARGET: examine each farm entry.
    for name, link in actual.items():
        if not link.is_symlink():
            continue

        exists = _symlink_target_exists(link)
        if not exists:
            drift.dead_links.append(name)
            continue

        resolved = _resolved_symlink_target(link)
        if resolved is None:
            # Link could be read (existence check passed) but resolve failed —
            # unlikely but handle gracefully as dead.
            drift.dead_links.append(name)
            continue

        if name in expected:
            expected_target = str(expected[name].resolve())
            if resolved != expected_target:
                drift.wrong_targets.append((name, expected_target, resolved))
        else:
            # The farm has a symlink for a file the checkout doesn't have.
            # Not necessarily wrong — could be from a previous checkout. Flag it
            # only if it points outside the current checkout.
            if not resolved.startswith(checkout_root + "/"):
                drift.wrong_targets.append((name, f"{checkout_root}/", resolved))

    return drift


# ---- fix --------------------------------------------------------------------

def fix_new_modules(drift: Drift, checkout: Path | None = None,
                    farm_dir: str | None = None) -> bool:
    """Auto-close new-module drift by invoking lib/link-farm.sh.

    Gated on drift.can_auto_link: new modules present and NO wrong target. A
    dead link does not block it — see that property for why the two risks are
    not the same. Does nothing when there is a wrong target, because the farm's
    provenance is then in doubt.

    Args:
        drift: the Drift from check()
        checkout: the repo root (default: CHECKOUT)
        farm_dir: the farm's parent ($HOME/.local/lib) — overrides for tests.
                  Default: derived from _FARM (FARM's parent).

    Returns:
        True if the fix was applied, False if conditions were not met.
    """
    if not drift.can_auto_link:
        return False
    if checkout is None:
        checkout = _CHECKOUT
    link_farm = checkout / "lib" / "link-farm.sh"
    if not link_farm.exists():
        return False

    if farm_dir is None:
        farm_dir = str(_FARM.parent)
    result = subprocess.run(
        ["bash", str(link_farm), str(checkout), farm_dir],
        capture_output=True, text=True,
    )
    return result.returncode == 0


# ---- main / CLI -------------------------------------------------------------

def main() -> int:
    """CLI entry point. Returns exit code 0 on clean farm, 1 on drift."""
    verbose = "--verbose" in sys.argv
    do_fix = "--fix" in sys.argv

    checkout_lib = _CHECKOUT_LIB
    farm = _FARM

    drift = check(checkout_lib, farm)

    if drift.is_clean:
        if verbose:
            print("farm_check: OK — farm matches checkout")
        return 0

    for name in drift.new_modules:
        print(f"NEW MODULE: {name}")
    for name in drift.dead_links:
        print(f"DEAD LINK: {name} → (target missing)")
    for name, expected, actual in drift.wrong_targets:
        print(f"WRONG TARGET: {name} expected {expected} got {actual}")

    # Auto-close what can be closed: --fix, and no wrong target.
    if do_fix and drift.can_auto_link:
        applied = fix_new_modules(drift, _CHECKOUT)
        if not applied:
            print("farm_check: auto-fix failed")
            return 1
        if verbose:
            print(f"farm_check: linked {len(drift.new_modules)} new module(s)")
        # EXIT 0 ONLY IF NOTHING SURVIVED THE FIX. Linking the new modules
        # does not clear a dead link — clearing one is not part of this
        # automated fix — so drift remains and the exit code must keep saying
        # so. Returning 0 here would make the documented contract false (0
        # means "farm matches checkout") and would make this CLI the one path
        # where auto-linking silences the leftover, which is exactly what the
        # autopull's own branch takes care to avoid by alerting on a
        # remaining dead link.
        if drift.dead_links:
            print("farm_check: dead link(s) remain — run install.sh from the primary checkout to clear them")
            return 1
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
