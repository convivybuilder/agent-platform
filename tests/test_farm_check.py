"""Tests for lib/agent-platform/farm_check.py — the symlink-farm drift detector.

The farm check compares:
  - Checkout lib/agent-platform/ (what *should* be installed)
  - ~/.local/lib/agent-platform/ (the per-file symlink farm that IS installed)

Every test uses tmp_path to construct a fake checkout and a fake farm so no
real filesystem state is touched.

THREE DRIFT TYPES:
  1. NEW MODULE  — a .py file exists in the checkout but has no farm entry
  2. DEAD LINK   — a farm symlink points at a file that no longer exists
  3. WRONG TARGET — a farm symlink points at a file in a different checkout,
     including a link resolving into a Convivy/worktrees/ path — the 2026-07-30
     shape, where the deployed farm served a feature-branch worktree's code.
"""
from __future__ import annotations

import sys
from pathlib import Path

import farm_check


def _pyfile(directory: Path, name: str) -> Path:
    """Create a minimal .py file in `directory` and return its path."""
    p = directory / name
    p.write_text(f"# {name}\n")
    return p


def _symlink(farm: Path, name: str, target: Path) -> Path:
    """Create a symlink in the farm directory named `name` pointing at `target`."""
    link = farm / name
    link.symlink_to(target)
    return link


# --- clean -------------------------------------------------------------------


def test_clean_farm_returns_no_drift(tmp_path):
    checkout_lib = tmp_path / "checkout" / "lib" / "agent-platform"
    checkout_lib.mkdir(parents=True)
    farm = tmp_path / "farm"
    farm.mkdir()

    _pyfile(checkout_lib, "module_a.py")
    _pyfile(checkout_lib, "module_b.py")
    _symlink(farm, "module_a.py", checkout_lib / "module_a.py")
    _symlink(farm, "module_b.py", checkout_lib / "module_b.py")

    drift = farm_check.check(checkout_lib, farm)
    assert drift.is_clean
    assert drift.new_modules == []
    assert drift.dead_links == []
    assert drift.wrong_targets == []


def test_empty_checkout_is_trivially_clean(tmp_path):
    farm = tmp_path / "farm"
    farm.mkdir()
    drift = farm_check.check(tmp_path / "nope", farm)
    assert drift.is_clean


def test_empty_farm_with_empty_checkout_is_clean(tmp_path):
    drift = farm_check.check(tmp_path / "no-checkout", tmp_path / "no-farm")
    assert drift.is_clean


# --- new modules -------------------------------------------------------------


def test_detects_new_modules(tmp_path):
    checkout_lib = tmp_path / "checkout" / "lib" / "agent-platform"
    checkout_lib.mkdir(parents=True)
    farm = tmp_path / "farm"
    farm.mkdir()

    _pyfile(checkout_lib, "existing.py")
    _pyfile(checkout_lib, "brand_new.py")
    _symlink(farm, "existing.py", checkout_lib / "existing.py")

    drift = farm_check.check(checkout_lib, farm)
    assert drift.new_modules == ["brand_new.py"]
    assert drift.dead_links == []
    assert drift.wrong_targets == []
    assert not drift.is_clean
    assert drift.has_only_new_modules


def test_multiple_new_modules(tmp_path):
    checkout_lib = tmp_path / "checkout" / "lib" / "agent-platform"
    checkout_lib.mkdir(parents=True)
    farm = tmp_path / "farm"
    farm.mkdir()

    _pyfile(checkout_lib, "a.py")
    _pyfile(checkout_lib, "b.py")
    _pyfile(checkout_lib, "c.py")

    drift = farm_check.check(checkout_lib, farm)
    assert sorted(drift.new_modules) == ["a.py", "b.py", "c.py"]
    assert drift.has_only_new_modules


# --- dead links --------------------------------------------------------------


def test_detects_dead_link(tmp_path):
    checkout_lib = tmp_path / "checkout" / "lib" / "agent-platform"
    checkout_lib.mkdir(parents=True)
    farm = tmp_path / "farm"
    farm.mkdir()

    _pyfile(checkout_lib, "still_here.py")
    gone = checkout_lib / "gone.py"
    gone.write_text("# gone")
    _symlink(farm, "still_here.py", checkout_lib / "still_here.py")
    _symlink(farm, "gone.py", gone)
    gone.unlink()

    drift = farm_check.check(checkout_lib, farm)
    assert drift.dead_links == ["gone.py"]
    assert not drift.is_clean
    assert not drift.has_only_new_modules


def test_dead_link_with_new_module_is_not_only_new(tmp_path):
    checkout_lib = tmp_path / "checkout" / "lib" / "agent-platform"
    checkout_lib.mkdir(parents=True)
    farm = tmp_path / "farm"
    farm.mkdir()

    _pyfile(checkout_lib, "brand_new.py")
    dead_target = tmp_path / "dead_target.py"
    dead_target.write_text("# dead")
    _symlink(farm, "dead_link.py", dead_target)
    dead_target.unlink()

    drift = farm_check.check(checkout_lib, farm)
    assert drift.new_modules == ["brand_new.py"]
    assert drift.dead_links == ["dead_link.py"]
    assert not drift.has_only_new_modules
    # ...BUT THE NEW MODULE IS STILL LINKABLE. This is the case that wedged the
    # real farm: techdebt.py's dead link held crew_doctor.py, github_app.py and
    # shell_families.py out of the deployed library for three days, and the
    # autopull logged it 1,645 times. A dead link is inert and says nothing
    # about whether linking a different, wanted module is safe.
    assert drift.can_auto_link


def test_a_dead_link_does_not_block_the_fix_being_applied(tmp_path, monkeypatch):
    """The gate above decides whether `fix_new_modules` actually runs, so pin the
    behaviour at the function that acts rather than only at the property.

    Without this, `can_auto_link` could be correct while `fix_new_modules` still
    consulted the old property and refused — the same shape as a guard whose
    read keys on a different field from the one it checks.
    """
    checkout = tmp_path / "checkout"
    checkout_lib = checkout / "lib" / "agent-platform"
    checkout_lib.mkdir(parents=True)
    farm = tmp_path / "lib" / "agent-platform"
    farm.mkdir(parents=True)

    _pyfile(checkout_lib, "brand_new.py")
    dead_target = tmp_path / "dead_target.py"
    dead_target.write_text("# dead")
    _symlink(farm, "dead_link.py", dead_target)
    dead_target.unlink()

    # A stub link-farm.sh, so this pins the DECISION to invoke rather than
    # re-testing the linker, which has its own suite.
    (checkout / "lib").mkdir(exist_ok=True)
    marker = tmp_path / "linker-ran"
    (checkout / "lib" / "link-farm.sh").write_text(
        f'#!/usr/bin/env bash\necho ran > "{marker}"\n')

    drift = farm_check.check(checkout_lib, farm)
    assert drift.dead_links == ["dead_link.py"]
    applied = farm_check.fix_new_modules(drift, checkout=checkout,
                                         farm_dir=str(farm.parent))
    assert applied is True
    assert marker.exists(), "link-farm.sh was never invoked"


def test_a_wrong_target_still_blocks_everything(tmp_path):
    """The half that must NOT change. A wrong target means the farm resolves
    into another checkout, so its provenance is in doubt and adding links from
    this one mixes two — the 2026-07-30 incident, 38 of 40 links pointing at a
    feature-branch worktree. Refusing everything stays right."""
    checkout_lib = tmp_path / "checkout" / "lib" / "agent-platform"
    checkout_lib.mkdir(parents=True)
    other = tmp_path / "other-checkout" / "lib" / "agent-platform"
    other.mkdir(parents=True)
    farm = tmp_path / "farm"
    farm.mkdir()

    _pyfile(checkout_lib, "brand_new.py")
    _pyfile(checkout_lib, "shared.py")
    _pyfile(other, "shared.py")
    _symlink(farm, "shared.py", other / "shared.py")

    drift = farm_check.check(checkout_lib, farm)
    assert drift.new_modules == ["brand_new.py"]
    assert len(drift.wrong_targets) == 1
    assert not drift.can_auto_link
    assert farm_check.fix_new_modules(drift, checkout=tmp_path / "checkout") is False


# --- wrong targets -----------------------------------------------------------


def test_detects_wrong_target(tmp_path):
    checkout_lib = tmp_path / "checkout" / "lib" / "agent-platform"
    checkout_lib.mkdir(parents=True)
    other_checkout = tmp_path / "other-checkout" / "lib" / "agent-platform"
    other_checkout.mkdir(parents=True)
    farm = tmp_path / "farm"
    farm.mkdir()

    _pyfile(checkout_lib, "module_a.py")
    _pyfile(other_checkout, "module_a.py")
    _symlink(farm, "module_a.py", other_checkout / "module_a.py")

    drift = farm_check.check(checkout_lib, farm)
    assert len(drift.wrong_targets) == 1
    w = drift.wrong_targets[0]
    assert w[0] == "module_a.py"
    assert str(checkout_lib / "module_a.py") in w[1]
    assert str(other_checkout / "module_a.py") in w[2]
    assert not drift.is_clean
    assert not drift.has_only_new_modules


def test_worktree_target_link_is_wrong_target(tmp_path):
    """A farm link resolving into a Convivy/worktrees/ path for a module the
    primary checkout also has is a wrong target.

    The 2026-07-30 incident shape: install.sh had run inside a bug-fixer's
    worktree, so the deployed farm linked 38 modules into that still-open PR's
    checkout. The link resolves to a real, readable file — not a dead link —
    and the only thing distinguishing it from a correct link is that the target
    is a different checkout root, which is what a wrong target is."""
    checkout_lib = tmp_path / "Convivy" / "agent-platform" / "lib" / "agent-platform"
    checkout_lib.mkdir(parents=True)
    worktree = (
        tmp_path
        / "Convivy"
        / "worktrees"
        / "agent-platform.bug-link-new-lib-modules-into-the-in"
        / "lib"
        / "agent-platform"
    )
    worktree.mkdir(parents=True)
    farm = tmp_path / "farm"
    farm.mkdir()

    _pyfile(checkout_lib, "issue_client.py")
    _pyfile(worktree, "issue_client.py")
    _symlink(farm, "issue_client.py", worktree / "issue_client.py")

    drift = farm_check.check(checkout_lib, farm)
    assert len(drift.wrong_targets) == 1
    w = drift.wrong_targets[0]
    assert w[0] == "issue_client.py"
    assert str(checkout_lib / "issue_client.py") in w[1]
    assert str(worktree / "issue_client.py") in w[2]
    assert not drift.is_clean
    assert not drift.has_only_new_modules


def test_worktree_only_module_is_wrong_target(tmp_path):
    """A farm link to a module that exists only in the worktree (the primary
    checkout does not have it) is still a wrong target — it points outside the
    current checkout root."""
    checkout_lib = tmp_path / "Convivy" / "agent-platform" / "lib" / "agent-platform"
    checkout_lib.mkdir(parents=True)
    worktree = (
        tmp_path
        / "Convivy"
        / "worktrees"
        / "agent-platform.bug-something"
        / "lib"
        / "agent-platform"
    )
    worktree.mkdir(parents=True)
    farm = tmp_path / "farm"
    farm.mkdir()

    _pyfile(checkout_lib, "existing.py")
    _pyfile(worktree, "branch_only.py")
    _symlink(farm, "branch_only.py", worktree / "branch_only.py")

    drift = farm_check.check(checkout_lib, farm)
    assert len(drift.wrong_targets) == 1
    assert drift.wrong_targets[0][0] == "branch_only.py"
    assert str(worktree / "branch_only.py") in drift.wrong_targets[0][2]
    assert not drift.is_clean
    assert not drift.has_only_new_modules


def test_stale_link_to_deleted_module_is_not_wrong(tmp_path):
    """A farm link for a file the checkout does not have, pointing at a valid
    path inside the current checkout, is not a wrong target — the file has
    been renamed or removed, the link is stale but the target is valid."""
    checkout_lib = tmp_path / "checkout" / "lib" / "agent-platform"
    checkout_lib.mkdir(parents=True)
    farm = tmp_path / "farm"
    farm.mkdir()

    _pyfile(checkout_lib, "current.py")
    stale_target = checkout_lib / "renamed.py"
    stale_target.write_text("# renamed")
    _symlink(farm, "stale.py", stale_target)
    _symlink(farm, "current.py", checkout_lib / "current.py")

    drift = farm_check.check(checkout_lib, farm)
    # stale.py has no corresponding checkout file, and it points inside the
    # current checkout — not a wrong target (could be a leftover from a
    # rename). A future sweep or full install.sh will clean it up.
    assert drift.wrong_targets == []


# --- has_only_new_modules property -------------------------------------------


def test_only_new_modules_true_when_only_new(tmp_path):
    checkout_lib = tmp_path / "checkout" / "lib" / "agent-platform"
    checkout_lib.mkdir(parents=True)
    farm = tmp_path / "farm"
    farm.mkdir()

    _pyfile(checkout_lib, "new.py")
    _pyfile(checkout_lib, "also_new.py")

    drift = farm_check.check(checkout_lib, farm)
    assert drift.has_only_new_modules is True


def test_only_new_modules_false_when_dead_link(tmp_path):
    checkout_lib = tmp_path / "checkout" / "lib" / "agent-platform"
    checkout_lib.mkdir(parents=True)
    farm = tmp_path / "farm"
    farm.mkdir()

    dead = checkout_lib / "dead.py"
    dead.write_text("# dead")
    _symlink(farm, "dead.py", dead)
    dead.unlink()

    drift = farm_check.check(checkout_lib, farm)
    assert drift.has_only_new_modules is False


def test_only_new_modules_false_when_clean(tmp_path):
    checkout_lib = tmp_path / "checkout" / "lib" / "agent-platform"
    checkout_lib.mkdir(parents=True)
    farm = tmp_path / "farm"
    farm.mkdir()

    _pyfile(checkout_lib, "m.py")
    _symlink(farm, "m.py", checkout_lib / "m.py")

    drift = farm_check.check(checkout_lib, farm)
    assert drift.is_clean
    assert drift.has_only_new_modules is False


# --- main() exit codes -------------------------------------------------------


def test_main_0_on_clean(monkeypatch):
    monkeypatch.setattr(farm_check, "_CHECKOUT_LIB", Path("/nonexistent/checkout"))
    monkeypatch.setattr(farm_check, "_FARM", Path("/nonexistent/farm"))
    assert farm_check.main() == 0


def test_main_1_on_drift(monkeypatch, tmp_path):
    checkout_lib = tmp_path / "ck" / "lib" / "agent-platform"
    checkout_lib.mkdir(parents=True)
    farm = tmp_path / "fm"
    farm.mkdir()
    _pyfile(checkout_lib, "new.py")
    monkeypatch.setattr(farm_check, "_CHECKOUT_LIB", checkout_lib)
    monkeypatch.setattr(farm_check, "_FARM", farm)
    assert farm_check.main() == 1


def test_main_0_with_auto_fix_and_linkfarm_sh(monkeypatch, tmp_path):
    """--fix with only new modules + link-farm.sh present should auto-fix.

    Uses a real-looking farm layout (_FARM = <local_lib>/agent-platform) so
    the farm_dir = str(_FARM.parent) derivation in fix_new_modules produces the
    correct local_lib path. Also asserts the symlink was created inside _FARM
    (not merely that main() returned 0)."""
    checkout_root = tmp_path / "ck"
    checkout_lib = checkout_root / "lib" / "agent-platform"
    checkout_lib.mkdir(parents=True)
    local_lib = tmp_path / "local_lib"
    farm = local_lib / "agent-platform"
    farm.mkdir(parents=True)
    _pyfile(checkout_lib, "new.py")

    # Create a stub link-farm.sh that actually creates the symlink.
    (checkout_root / "lib").mkdir(exist_ok=True)
    (checkout_root / "lib" / "link-farm.sh").write_text(
        "#!/bin/sh\n"
        'REPO="$1"; LOCAL_LIB="${2:-$HOME/.local/lib}"; FARM="$LOCAL_LIB/agent-platform"\n'
        'mkdir -p "$FARM"\n'
        'ln -sfn "$REPO/lib/agent-platform/new.py" "$FARM/new.py"\n'
    )

    monkeypatch.setattr(farm_check, "_CHECKOUT", checkout_root)
    monkeypatch.setattr(farm_check, "_CHECKOUT_LIB", checkout_lib)
    monkeypatch.setattr(farm_check, "_FARM", farm)
    monkeypatch.setattr(sys, "argv", ["farm_check", "--fix"])
    assert farm_check.main() == 0
    # Verify the symlink was actually created inside _FARM (ARCH-013).
    assert (farm / "new.py").is_symlink()
    assert (farm / "new.py").readlink() == Path(str(checkout_lib / "new.py"))


def test_main_1_with_drift_and_no_fix(monkeypatch, tmp_path):
    """Without --fix, drift should exit 1 even when only new modules."""
    checkout_lib = tmp_path / "ck" / "lib" / "agent-platform"
    checkout_lib.mkdir(parents=True)
    farm = tmp_path / "fm"
    farm.mkdir()
    _pyfile(checkout_lib, "new.py")
    monkeypatch.setattr(farm_check, "_CHECKOUT_LIB", checkout_lib)
    monkeypatch.setattr(farm_check, "_FARM", farm)
    monkeypatch.setattr(sys, "argv", ["farm_check"])
    assert farm_check.main() == 1


def test_main_fix_still_exits_1_when_a_dead_link_survives(monkeypatch, tmp_path):
    """--fix that links the new modules but leaves a dead link must still exit 1.

    The existing dead-link CLI test below covers a dead link with NO new module,
    where `can_auto_link` is already False for an unrelated reason — so it never
    reaches this branch and would pass whatever the branch returned. This is the
    combination that decoupling the gate newly made reachable: `can_auto_link` is
    True, the fix runs, and a dead link is still there afterwards.

    Exit 0 would make the module's own contract false — 0 means the farm matches
    the checkout, and it does not — and would make this CLI the one path where
    auto-linking silences the leftover, which the autopull's branch deliberately
    avoids by alerting on the remaining dead link.
    """
    checkout = tmp_path / "ck"
    checkout_lib = checkout / "lib" / "agent-platform"
    checkout_lib.mkdir(parents=True)
    farm = tmp_path / "lib" / "agent-platform"
    farm.mkdir(parents=True)

    _pyfile(checkout_lib, "brand_new.py")
    dead = tmp_path / "dead.py"
    dead.write_text("# dead")
    _symlink(farm, "dead.py", dead)
    dead.unlink()

    # A stub linker that succeeds, so the fix path is genuinely taken.
    (checkout / "lib" / "link-farm.sh").write_text("#!/usr/bin/env bash\nexit 0\n")

    monkeypatch.setattr(farm_check, "_CHECKOUT", checkout)
    monkeypatch.setattr(farm_check, "_CHECKOUT_LIB", checkout_lib)
    monkeypatch.setattr(farm_check, "_FARM", farm)
    monkeypatch.setattr(sys, "argv", ["farm_check", "--fix"])
    assert farm_check.main() == 1


def test_main_fix_exits_0_when_the_fix_closes_everything(monkeypatch, tmp_path):
    """The other direction, so the assertion above cannot be satisfied by always
    returning 1: a --fix with new modules and NO dead link closes all the drift
    and exits 0."""
    checkout = tmp_path / "ck"
    checkout_lib = checkout / "lib" / "agent-platform"
    checkout_lib.mkdir(parents=True)
    farm = tmp_path / "lib" / "agent-platform"
    farm.mkdir(parents=True)

    _pyfile(checkout_lib, "brand_new.py")
    (checkout / "lib" / "link-farm.sh").write_text("#!/usr/bin/env bash\nexit 0\n")

    monkeypatch.setattr(farm_check, "_CHECKOUT", checkout)
    monkeypatch.setattr(farm_check, "_CHECKOUT_LIB", checkout_lib)
    monkeypatch.setattr(farm_check, "_FARM", farm)
    monkeypatch.setattr(sys, "argv", ["farm_check", "--fix"])
    assert farm_check.main() == 0


def test_main_1_on_dead_link_no_autofix(monkeypatch, tmp_path):
    """Dead links should exit 1 even with --fix."""
    checkout_lib = tmp_path / "ck" / "lib" / "agent-platform"
    checkout_lib.mkdir(parents=True)
    farm = tmp_path / "fm"
    farm.mkdir()

    dead = tmp_path / "dead.py"
    dead.write_text("# dead")
    _symlink(farm, "dead.py", dead)
    dead.unlink()

    monkeypatch.setattr(farm_check, "_CHECKOUT_LIB", checkout_lib)
    monkeypatch.setattr(farm_check, "_FARM", farm)
    monkeypatch.setattr(sys, "argv", ["farm_check", "--fix"])
    assert farm_check.main() == 1


# --- fix_new_modules ---------------------------------------------------------


def test_fix_skips_when_not_only_new(tmp_path, monkeypatch):
    checkout_lib = tmp_path / "ck" / "lib" / "agent-platform"
    checkout_lib.mkdir(parents=True)
    farm = tmp_path / "fm"
    farm.mkdir()

    dead_target = tmp_path / "dead.py"
    dead_target.write_text("# dead")
    _symlink(farm, "dead.py", dead_target)
    dead_target.unlink()

    drift = farm_check.check(checkout_lib, farm)
    assert not farm_check.fix_new_modules(drift, checkout=tmp_path / "ck")


def test_fix_links_new_modules(tmp_path, monkeypatch):
    """fix_new_modules should create symlinks for new modules via link-farm.sh."""
    checkout_root = tmp_path / "ck"
    checkout_lib = checkout_root / "lib" / "agent-platform"
    checkout_lib.mkdir(parents=True)
    # The farm dir matches link-farm.sh's convention: <local_lib>/agent-platform.
    local_lib = tmp_path / "local"
    farm = local_lib / "agent-platform"
    farm.mkdir(parents=True)

    _pyfile(checkout_lib, "existing.py")
    _pyfile(checkout_lib, "new_module.py")
    _symlink(farm, "existing.py", checkout_lib / "existing.py")

    # Create a stub link-farm.sh that actually creates the symlink.
    (checkout_root / "lib").mkdir(exist_ok=True)
    (checkout_root / "lib" / "link-farm.sh").write_text(
        "#!/bin/sh\nREPO=\"$1\"; LOCAL_LIB=\"${2:-$HOME/.local/lib}\"; FARM=\"$LOCAL_LIB/agent-platform\"; mkdir -p \"$FARM\"; ln -sfn \"$REPO/lib/agent-platform/new_module.py\" \"$FARM/new_module.py\"\n")

    drift = farm_check.check(checkout_lib, farm)
    assert drift.has_only_new_modules
    # Pass LOCAL_LIB (not the farm itself) — matches link-farm.sh's second arg.
    assert farm_check.fix_new_modules(drift, checkout_root, str(local_lib))

    # Verify the symlink was created.
    assert (farm / "new_module.py").is_symlink()
    assert (farm / "new_module.py").readlink() == Path(str(checkout_lib / "new_module.py"))


def test_fix_noop_when_script_missing(tmp_path):
    checkout_root = tmp_path / "ck"
    checkout_lib = checkout_root / "lib" / "agent-platform"
    checkout_lib.mkdir(parents=True)
    farm = tmp_path / "fm"
    farm.mkdir()

    _pyfile(checkout_lib, "new_module.py")

    drift = farm_check.check(checkout_lib, farm)
    assert drift.has_only_new_modules
    # No link-farm.sh exists — fix should be a no-op.
    assert not farm_check.fix_new_modules(drift, checkout_root, str(farm.parent))
