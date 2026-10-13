"""publish_scan — run Builder's content scan on the exact commit `agentctl
publish` is about to push, so a tree that carries a private person's name, a
machine's name, a credential or a network path never reaches origin.

The scan is Shipwright's own `bin/publish-engine`, which `bin/install-payload`
already runs at every release. Nothing in the ordinary PR flow runs it, so a
cleanup that passed the scan can be undone a day later by ordinary, reviewed
PRs that add a name back — this module is what closes that gap for every push
`agentctl publish` makes.

THIS MODULE NEVER RE-IMPLEMENTS A DETECTOR, A PATTERN OR THE ENGINE LIST. It
locates Shipwright's checkout, exports its `origin/main` (never whatever
branch Shipwright's own primary checkout happens to be on — company/
decisions/0008's layout puts five repositories beside each other, and nothing
pins Shipwright's own branch to that), and runs the real `bin/publish-engine`
against the commit being published. A repository `publish-engine` does not
know about is not refused: its own argparse rejects the repository name
before touching anything security-sensitive, and that rejection is read as
"not one of Builder's engine exports", not as a failure.

FAILS CLOSED. When Shipwright's checkout, `bin/publish-engine` inside its
`origin/main`, or either names file cannot be found or read, `scan_commit`
refuses — `agentctl publish` must never push a tree it could not scan.
"""
from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import operator_config as OC
import run_sandbox as RS

PROG = "publish-engine"

# Any version of the right shape satisfies publish-engine's own --version
# check; this scan is not a release, so the value itself carries no meaning.
_SCAN_VERSION = "0.0.0"

_INVALID_CHOICE = re.compile(r"invalid choice: '([^']*)'")


class PublishScanRefused(Exception):
    """`scan_commit` could not vouch for the commit: either the tree carries a
    finding, or the scan itself — Shipwright's checkout, `publish-engine`, or
    either names file — could not be reached. The caller must refuse the push
    either way; the message says which."""


def _clean_git_env() -> dict[str, str]:
    """`os.environ`, minus the vars that redirect git at a run's own private
    object store (ARCH's own convention, `run_sandbox.GIT_OBJECT_REDIRECT_VARS`).

    A dispatched run's shell carries these so ITS OWN commits land in a private
    store rather than the shared one; every git command this module runs is
    against a DIFFERENT repository (Shipwright) or against the commit already
    shared with the rest of this worktree, and inheriting the redirect makes
    git look for Shipwright's objects in a store that holds none of them —
    found by hand while building this module, against this very checkout."""
    return {k: v for k, v in os.environ.items() if k not in RS.GIT_OBJECT_REDIRECT_VARS}


def shipwright_checkout(wt: Path) -> Path:
    """Where Shipwright's own checkout is: the operator config's
    `shipwright_checkout` key when set, else the sibling of `wt`'s primary
    checkout — the same convention `publish-payload`'s own `siblings_dir()`
    resolves to (company/decisions/0008's layout: the platform's repositories
    sit beside each other, one directory per repository)."""
    try:
        return Path(OC.get("shipwright_checkout")).expanduser()
    except OC.OperatorConfigError:
        pass
    common = subprocess.run(
        ["git", "-C", str(wt), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True, text=True, env=_clean_git_env(),
    )
    base = Path(common.stdout.strip()) if common.returncode == 0 and common.stdout.strip() \
        else wt / ".git"
    return base.resolve().parent.parent / "shipwright"


def private_names_file() -> Path:
    """The file naming the operator's private people, for `BUILDER_PRIVATE_NAMES`:
    the operator config's `builder_private_names_file` key when set, else
    `~/.config/convivy/private-names` — beside `operator_config`'s own file, and
    never inside any repository."""
    try:
        return Path(OC.get("builder_private_names_file")).expanduser()
    except OC.OperatorConfigError:
        return Path.home() / ".config" / "convivy" / "private-names"


def machine_names_file() -> Path:
    """The file naming the operator's machines, for `BUILDER_MACHINE_NAMES`:
    the operator config's `builder_machine_names_file` key when set, else
    `~/.config/convivy/machine-names`."""
    try:
        return Path(OC.get("builder_machine_names_file")).expanduser()
    except OC.OperatorConfigError:
        return Path.home() / ".config" / "convivy" / "machine-names"


def _export_shipwright_tree(checkout: Path) -> Path:
    """Shipwright's `origin/main`, extracted into a fresh temp directory.

    `git archive`, never `git checkout`: this never touches `checkout`'s own
    working tree or HEAD, so it is safe to call against the operator's primary
    Shipwright checkout even while that checkout sits on some other branch.
    """
    dest = Path(tempfile.mkdtemp(prefix="publish-engine-src-"))
    archive = subprocess.run(
        ["git", "-C", str(checkout), "archive", "origin/main"],
        capture_output=True, env=_clean_git_env(),
    )
    if archive.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        raise PublishScanRefused(
            f"could not export {checkout} at origin/main: "
            f"{archive.stderr.decode(errors='replace').strip()}"
        )
    try:
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
            tar.extractall(dest, filter="data")
    except tarfile.TarError as exc:
        shutil.rmtree(dest, ignore_errors=True)
        raise PublishScanRefused(
            f"could not extract {checkout}'s origin/main export: {exc}"
        ) from exc
    return dest


def scan_commit(repo_name: str, wt: Path, commit: str) -> None:
    """Run Builder's content scan on `commit` in `wt`, scoped to `repo_name`.

    Returns when the scan passed, or when `repo_name` is not one of
    `publish-engine`'s known engine exports — asked of the tool itself, via
    its own argument validation, never a copy of its list. Raises
    `PublishScanRefused` on a refused tree (findings, or a names file
    `publish-engine` itself could not read) and when the scan could not be
    run at all (Shipwright's checkout or `bin/publish-engine` missing, or an
    unexpected failure) — `agentctl publish` must refuse the push either way.
    """
    checkout = shipwright_checkout(wt)
    if not (checkout / ".git").exists():
        raise PublishScanRefused(
            f"no Shipwright checkout at {checkout}; cannot run Builder's content scan"
        )

    export_dir: Path | None = None
    out_dir = Path(tempfile.mkdtemp(prefix="publish-engine-out-"))
    try:
        export_dir = _export_shipwright_tree(checkout)
        engine = export_dir / "bin" / "publish-engine"
        if not engine.is_file():
            raise PublishScanRefused(
                f"{checkout} at origin/main has no bin/publish-engine; "
                f"cannot run Builder's content scan"
            )

        env = _clean_git_env()
        env["BUILDER_PRIVATE_NAMES"] = str(private_names_file())
        env["BUILDER_MACHINE_NAMES"] = str(machine_names_file())

        result = subprocess.run(
            [sys.executable, str(engine), repo_name,
             "--version", _SCAN_VERSION,
             "--out", str(out_dir),
             "--from", str(wt),
             "--ref", commit],
            capture_output=True, text=True, env=env,
        )
        if result.returncode == 0:
            return

        stderr = result.stderr or ""
        if "usage: publish-engine" in stderr:
            if _INVALID_CHOICE.search(stderr):
                return  # not one of publish-engine's known engine exports
            raise PublishScanRefused(
                f"publish-engine rejected its own arguments: {stderr.strip()}"
            )
        if stderr.startswith(f"{PROG}: "):
            raise PublishScanRefused(stderr[len(f"{PROG}: "):].strip())
        raise PublishScanRefused(
            f"publish-engine exited {result.returncode} without its usual message: "
            f"{stderr.strip()}"
        )
    finally:
        if export_dir is not None:
            shutil.rmtree(export_dir, ignore_errors=True)
        shutil.rmtree(out_dir, ignore_errors=True)
