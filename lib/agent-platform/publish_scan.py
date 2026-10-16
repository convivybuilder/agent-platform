"""publish_scan — run Builder's content scan on the exact commit `agentctl
publish` is about to push, so a tree that carries a private person's name, a
machine's name, a credential or a network path never reaches origin.

The scan is Shipwright's own `bin/publish-engine --scan-only`, which asks the
same four detectors a release export runs, without building anything that
outlives the run. Nothing in the ordinary PR flow runs it, so a cleanup that
passed the scan can be undone a day later by ordinary, reviewed PRs that add
a name back — this module is what closes that gap for every push `agentctl
publish` makes.

THIS MODULE NEVER RE-IMPLEMENTS A DETECTOR, A PATTERN OR THE ENGINE LIST. It
locates Shipwright's checkout, exports its `origin/main` (never whatever
branch Shipwright's own primary checkout happens to be on — company/
decisions/0008's layout puts five repositories beside each other, and nothing
pins Shipwright's own branch to that), and runs the real `bin/publish-engine
--scan-only` against the commit being published. ANY exit other than 0
blocks the push — a finding, a refusal, an engine too old to know
`--scan-only`, or death by signal all read the same way to a caller that
must never push a tree it could not vouch for — with one precisely-matched
exception: `scan_commit` (below) reads publish-engine's own argparse
rejecting `repo_name` as an invalid choice as "not this gate's business",
since `agentctl publish` calls it for every repository it pushes, including
ones outside publish-engine's five scannable repositories. `scan_commits`,
run only from inside a scannable repository's own pre-push hook, treats that
same rejection as a misconfigured hook instead and blocks.

FAILS CLOSED. When Shipwright's checkout, `bin/publish-engine` inside its
`origin/main`, or either names file cannot be found or read, `scan_commit`
refuses — `agentctl publish` must never push a tree it could not scan.
"""
from __future__ import annotations

import functools
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

# argparse's own message when a positional's `choices=` rejects a value,
# e.g. "argument name: invalid choice: 'sous-chef' (choose from 'agent-platform',
# 'library', ...)" — asked of the engine's own stderr, never a copy of its
# `choices` list, so a sixth scannable repository needs no change here.
_INVALID_CHOICE = re.compile(r"invalid choice: '([^']*)'")


class PublishScanRefused(Exception):
    """`scan_commit` could not vouch for the commit: either the tree carries a
    finding, or the scan itself — Shipwright's checkout, `publish-engine`, or
    either names file — could not be reached. The caller must refuse the push
    either way; the message says which."""


@functools.cache
def _git_local_env_vars() -> tuple[str, ...]:
    """Every environment variable git itself treats as pointing a repository
    at itself — `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE` and the rest
    (`git rev-parse --local-env-vars`, which names are fixed by the installed
    git binary and needs no repository to answer). Asked of git rather than
    kept as a copy of its list, so a var this module has never heard of is
    still stripped."""
    result = subprocess.run(
        ["git", "rev-parse", "--local-env-vars"], capture_output=True, text=True, check=True,
    )
    return tuple(line for line in result.stdout.splitlines() if line)


def _clean_git_env() -> dict[str, str]:
    """`os.environ`, minus every var `_git_local_env_vars` names.

    A dispatched run's shell, or a push-time hook invoked by git itself,
    carries `GIT_DIR` (and the rest) pointed at the repository that
    triggered it; every git command this module runs is against a
    DIFFERENT repository (Shipwright) or against the commit already shared
    with the rest of this worktree, and inheriting the redirect makes git
    look for the wrong repository's objects in a store that holds none of
    them — found by hand while building this module, against this very
    checkout."""
    return {k: v for k, v in os.environ.items() if k not in _git_local_env_vars()}


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


def _scan_one(repo_name: str, wt: Path, commit: str, *, skip_unknown_repo: bool) -> None:
    """The shared body of `scan_commit` and `scan_commits`: run Builder's
    content scan (`publish-engine --scan-only`) on `commit` in `wt`, scoped
    to `repo_name`.

    Returns when the scan exits 0 — clean, including when it printed a
    "pass `--ref HEAD`" notice, which is not a finding. Raises
    `PublishScanRefused` on every other exit EXCEPT one: publish-engine's own
    argparse rejecting `repo_name` itself as an invalid choice (matched
    precisely against the value it names, never any other argument's
    rejection). `skip_unknown_repo` decides what that one case means —
    `scan_commit` passes `True` (not this gate's business), `scan_commits`
    passes `False` (a misconfigured hook). Every other non-zero exit — a
    finding (1), a refusal (2: a names file, the purity checker, the ref, or
    a usage error naming some other argument), any other positive exit, and
    a negative exit (killed by a signal) — always raises; exit 1 means
    either a finding or an error before the scan started, publish-engine's
    `--scan-only` contract does not distinguish the two on exit code alone,
    so the message names both possibilities rather than asserting a finding.
    Every other exit keeps "could not run". The message carries the engine's
    own stdout and stderr unchanged — never reparsed or re-worded, since the
    engine already withholds every matched value itself.
    """
    checkout = shipwright_checkout(wt)
    if not (checkout / ".git").exists():
        raise PublishScanRefused(
            f"no Shipwright checkout at {checkout}; cannot run Builder's content scan"
        )

    export_dir: Path | None = None
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
            [sys.executable, "-I", str(engine), repo_name,
             "--scan-only",
             "--from", str(wt),
             "--ref", commit],
            capture_output=True, text=True, env=env,
        )
        if result.returncode == 0:
            return

        stderr = result.stderr or ""
        match = _INVALID_CHOICE.search(stderr)
        if match is not None and match.group(1) == repo_name:
            if skip_unknown_repo:
                return  # not one of publish-engine's scannable repositories
            raise PublishScanRefused(
                f"publish-engine does not know how to scan {repo_name!r}, but "
                f"this hook is only installed in a clone of a repository it "
                f"scans; the hook, not the push, is misconfigured"
            )

        reason = (
            "found something to refuse, or failed before the scan started"
            if result.returncode == 1 else "could not run"
        )
        output = (result.stdout or "") + stderr
        raise PublishScanRefused(
            f"publish-engine {reason} (exit {result.returncode}) scanning "
            f"{repo_name} at {commit}:\n{output}"
        )
    finally:
        if export_dir is not None:
            shutil.rmtree(export_dir, ignore_errors=True)


def scan_commit(repo_name: str, wt: Path, commit: str) -> None:
    """`_scan_one`, for `agentctl publish`: returns without raising when
    publish-engine's own argparse rejects `repo_name` as an invalid choice —
    `agentctl publish` runs this against every repository it pushes, not
    only the five publish-engine knows how to scan, and a repository outside
    that five is not this gate's business. Every other non-zero exit still
    raises `PublishScanRefused`; see `_scan_one`.
    """
    _scan_one(repo_name, wt, commit, skip_unknown_repo=True)


def scan_commits(repo_name: str, wt: Path, shas: list[str]) -> None:
    """`_scan_one`, run once per commit in `shas`, in order, stopping at the
    first refusal — for the pre-push hook, which must scan every new commit
    a push carries, not only the one at its tip, and which is only ever
    installed in a clone of a repository publish-engine scans: unlike
    `scan_commit`, publish-engine rejecting `repo_name` itself here raises
    (the hook is misconfigured, not the push).

    Raises `PublishScanRefused` on the first refused commit, its message
    prefixed with `repo_name`, that commit's short sha, and its position as
    "k of n" among `shas`, so a push of several commits says which one needs
    fixing. Returns when every commit passed.
    """
    total = len(shas)
    for index, sha in enumerate(shas, start=1):
        try:
            _scan_one(repo_name, wt, sha, skip_unknown_repo=False)
        except PublishScanRefused as exc:
            raise PublishScanRefused(
                f"{repo_name} at {sha[:9]} ({index} of {total}): {exc}"
            ) from exc
