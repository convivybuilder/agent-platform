"""publish_scan — run Builder's content scan on the exact commits `agentctl
publish` is about to push, so a tree that carries a private person's name, a
machine's name, a credential or a network path never reaches origin.

The scan is Shipwright's own `bin/publish-engine --scan-only`, which asks the
same five detectors a release export runs, without building anything that
outlives the run. Nothing in the ordinary PR flow runs it, so a cleanup that
passed the scan can be undone a day later by ordinary, reviewed PRs that add
a name back — this module is what closes that gap for every push `agentctl
publish` makes.

THIS GATE REFUSES ONLY WHAT A PUSH ADDS. `publish-engine --scan-only` itself
answers "does this tree have any finding at all", and agent-platform's own
`origin/main` already carries thousands of them — nothing in the ordinary PR
flow has ever run this scan before now, so a push that cannot add a single
one of them could never pass a whole-tree check. `scan_commits` (below) runs
the engine once per commit being pushed, and once more per parent of each of
those commits, and refuses only a finding that exists at a pushed commit and
did not exist at EVERY one of its parents (a root commit's one parent is the
empty tree). Judging a commit against only one of its parents — its first,
or a single merge-base with `origin/main` shared by the whole push — let a
merge of `origin/main` into a branch already on GitHub read every name
`origin/main` gained since the branch's fork as newly added, since neither
rule asks whether the OTHER parent (the thing already on `origin/main`,
reachable from `origin/main` itself) already carried it. The whole-tree
check stays the release gate's own job, in Shipwright's `tools/release`;
nothing here changes `publish-engine` itself.

THIS MODULE NEVER RE-IMPLEMENTS A DETECTOR, A PATTERN OR THE ENGINE LIST. It
locates Shipwright's checkout, exports its `origin/main` (never whatever
branch Shipwright's own primary checkout happens to be on — company/
decisions/0008's layout puts five repositories beside each other, and nothing
pins Shipwright's own branch to that), and runs the real `bin/publish-engine
--scan-only` against the commits being compared.

ANY EXIT OTHER THAN 0 OR 1 BLOCKS THE PUSH — a refusal, an engine too old to
know `--scan-only`, or death by signal all read the same way to a caller
that must never push a tree it could not vouch for — with one
precisely-matched exception: `scan_commits(..., skip_unknown_repo=True)`
(`agentctl publish`'s own call) reads publish-engine's own argparse
rejecting `repo_name` as an invalid choice as "not this gate's business",
since `agentctl publish` calls it for every repository it pushes, including
ones outside publish-engine's five scannable repositories.
`scan_commits(..., skip_unknown_repo=False)`, the default, is run only from
inside a scannable repository's own pre-push hook and treats that same
rejection as a misconfigured hook instead, blocking the push.

FAILS CLOSED, WITH ONE CARVE-OUT FOR A MISSING CHECKOUT. When a fetch of
Shipwright's origin's main, `bin/publish-engine` inside its `origin/main`,
either names file, or a parent commit to compare against cannot be found,
read, fetched, or parsed, `scan_commits` refuses — neither `agentctl
publish` nor a push through the pre-push hook may ever push a tree it could
not scan. A MISSING CHECKOUT ITSELF is different: an adopter instance has no
Shipwright at all, and `agentctl publish` runs this gate against every
repository it pushes, including the adopter's own, which is never one of
publish-engine's five. Without a checkout, publish-engine's own argparse can
never be asked whether `repo_name` is one of those five, so
`_LOCALLY_KNOWN_SCANNABLE_REPOS` (below) answers that one question in its
place: a repository not in it skips, same as the argparse carve-out above
would once a checkout existed; a repository in it still refuses, since that
is exactly the scan this gate exists to run. The moment a checkout exists,
this stand-in is never consulted again — publish-engine's own argparse is
asked, as it always was.

`repo_name` ALONE IS NOT TRUSTED FOR THIS. It is read from the checkout's own
directory name (`agentctl`'s `_publish_repo_name`), so a renamed or
relocated checkout of one of the five reads as an unfamiliar name, and
skipping on that name alone would turn "no Shipwright checkout" into a
silent, unscanned push for exactly the repository this gate exists to
scan. `push_remote` (the literal URL the push is going to, or the
worktree's own `origin` remote when nothing is actually being pushed yet)
is the independent check: a repository the directory name does not
recognize still refuses when that remote names one of the five anyway
(`_names_a_locally_known_scannable_repo`, below), case-insensitively. A
remote this module cannot resolve or parse counts as one of the five too —
unresolvable fails closed, exactly like every other "could not tell" case
in this module.
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
from urllib.parse import unquote

import operator_config as OC

# argparse's own message when a positional's `choices=` rejects a value,
# e.g. "argument name: invalid choice: 'acme-web' (choose from 'agent-platform',
# 'library', ...)" — asked of the engine's own stderr, never a copy of its
# `choices` list, so a sixth scannable repository needs no change here.
_INVALID_CHOICE = re.compile(r"invalid choice: '([^']*)'")

# The repository names `bin/install-push-gate` installs this gate's pre-push
# hook into (that script's own `CLONES`) — used ONLY by `_scan_added_findings`
# when `shipwright_checkout` names no checkout at all, so publish-engine's own
# argparse can never be asked whether `repo_name` is one of its five scannable
# repositories. A name NOT in this set skips without a checkout, the answer
# argparse would give once a checkout existed; a name IN this set still
# refuses, since it is exactly the repository this gate exists to scan. The
# moment a checkout exists, this set is never consulted — publish-engine's own
# argparse decides, as it always did — so a sixth repository publish-engine
# starts scanning needs an update here only to keep the no-checkout skip
# accurate for it, never to keep the gate itself correct.
#
# Compared case-insensitively (`_names_a_locally_known_scannable_repo`,
# below), and never the only check that function makes: `repo_name` is a
# checkout's own directory name, which a rename or relocation can make say
# anything, so a name missing from this set is not on its own proof that the
# checkout is an adopter's — see that function for the independent, remote-
# based check that still catches one of these five under any directory name.
_LOCALLY_KNOWN_SCANNABLE_REPOS = frozenset({
    "agent-platform", "library", "convivy-orient", "convivy-deck", "write-better",
})

# Every one of `_LOCALLY_KNOWN_SCANNABLE_REPOS`, as the `owner/repo` GitHub
# slug its push target names today — all five are Convivy's own, company/
# decisions/0008's layout (confirmed against `dispatch_env.py`, `issue_client.py`,
# `kb_merge.py`, `vault_plane.py` and `bin/agentctl`'s own literal `convivy/<name>`
# strings for these same five). Lower-cased once here so the case-insensitive
# compare in `_names_a_locally_known_scannable_repo` is a plain membership
# test, never a per-call re-lowering of this set.
_LOCALLY_KNOWN_SCANNABLE_SLUGS = frozenset(
    f"convivy/{name}" for name in _LOCALLY_KNOWN_SCANNABLE_REPOS
)

# A GitHub remote URL in any of the HTTPS, `ssh://git@` or `git@host:` forms,
# capturing its `owner/repo` — the same shape `bin/agentctl`'s own
# `_GITHUB_REMOTE_RE` recognizes, copied rather than imported (`bin/agentctl`
# is a script, not an importable module, and push-content-gate's own
# `_repo_slug` is more general — any host — which this module deliberately
# is not: a remote on some other host never names one of Convivy's five
# repositories, so treating it as unresolved, below, and failing closed is
# the only sound reading of it here).
_GITHUB_REMOTE_RE = re.compile(
    r"^(?:https://|ssh://git@|git@)github\.com[:/](?P<slug>[^/]+/[^/]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)

# A decoded `owner/repo` slug's own two segments — exactly the characters
# GitHub allows in either (`_decode_github_slug`, below). Checked AFTER
# percent-decoding, so a literal `/` or `%` smuggled in through encoding
# (a further path segment, or a double-encoded `%25xx`) fails this and
# reads as unresolved rather than as a slug to compare.
_SLUG_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _decode_github_slug(raw_slug: str) -> str | None:
    """Percent-decode a slug captured by `_GITHUB_REMOTE_RE` and return it
    normalized, or `None` when the decoded text is not a plain `owner/repo`
    — a remote such as `.../convivy/agent%2Dplatform.git` names the exact
    same GitHub repository on the wire as `.../convivy/agent-platform.git`
    (percent-encoding an unreserved character is wire-transparent, RFC 3986
    §2.3), so comparing the raw, still-encoded text against
    `_LOCALLY_KNOWN_SCANNABLE_SLUGS` would miss it. Decoding can also turn
    one segment into something that is no longer a single, plain slug — a
    further `/` (a smuggled extra path segment), a leftover `%` (a
    double-encoded or malformed escape that `unquote` could not resolve),
    or any character outside what GitHub allows in an owner or repository
    name — and `None` for any of those is read as unresolved by the caller,
    the same fail-closed answer as a remote this module cannot parse at
    all."""
    decoded = unquote(raw_slug)
    owner, sep, repo = decoded.partition("/")
    if not sep or "/" in repo:
        return None
    if not (_SLUG_SEGMENT_RE.match(owner) and _SLUG_SEGMENT_RE.match(repo)):
        return None
    return f"{owner}/{repo}"


def _names_a_locally_known_scannable_repo(
    repo_name: str, push_remote: str | None,
) -> bool:
    """Whether `_scan_added_findings`'s no-checkout carve-out must refuse
    rather than skip — `True` for one of publish-engine's five scannable
    repositories, by EITHER of two independent reads, each compared
    case-insensitively: `repo_name` itself (the checkout's own directory
    name, `_LOCALLY_KNOWN_SCANNABLE_REPOS`), or `push_remote` (the literal
    URL the push is going to, or the worktree's own `origin` remote when
    nothing is actually being pushed yet) naming one of the five under its
    real GitHub slug (`_LOCALLY_KNOWN_SCANNABLE_SLUGS`).

    The second read is what a renamed or relocated checkout of one of the
    five still has right, even once its directory name no longer does:
    `repo_name` comes from `agentctl`'s `_publish_repo_name`, which reads
    the checkout's OWN directory name, so a `mv` of that directory changes
    `repo_name` but never the checkout's `.git/config` — `push_remote`
    reads the latter, not the former.

    `push_remote` is `None`, any string `_GITHUB_REMOTE_RE` does not match,
    or a match whose captured slug `_decode_github_slug` cannot read as a
    plain, decoded `owner/repo` — read as UNRESOLVED rather than as "not
    one of the five" — a missing `origin`, an unreadable git config, a
    remote on some host other than `github.com`, or a captured slug that
    is still percent-encoded into something other than a single owner and
    a single repo segment, are all "cannot tell", and this gate fails
    closed on "cannot tell" exactly as it does on every other question it
    cannot answer, never open. The decode matters because percent-encoding
    is wire-transparent: `.../convivy/agent%2Dplatform.git` reaches the
    exact same repository as `.../convivy/agent-platform.git`, so comparing
    the still-encoded capture would let an encoded form of one of the five
    read as unfamiliar."""
    if repo_name.lower() in _LOCALLY_KNOWN_SCANNABLE_REPOS:
        return True
    if push_remote is None:
        return True
    match = _GITHUB_REMOTE_RE.match(push_remote.strip())
    if match is None:
        return True
    slug = _decode_github_slug(match.group("slug"))
    if slug is None:
        return True
    return slug.lower() in _LOCALLY_KNOWN_SCANNABLE_SLUGS


# The empty tree's well-known sha (`git hash-object -t tree /dev/null`), used
# as a commit's "base" when it has no parent — a repository's root commit,
# pushed for the first time. Every one of its own lines then reads as added,
# which is the correct, fail-closed answer for a tree with nothing before it.
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

# A finding item, once `finding_lines()`'s own 4-space indent is stripped, in
# either of the two shapes the five detectors print it: with a location
# (`path:line[ (byte N)]: rest`) or without one (`path: rest`). The location
# is parsed out and discarded rather than kept in the comparison key, because
# a line number moves between the base and the head commit for reasons that
# have nothing to do with what changed — a comment added three lines above an
# untouched finding would otherwise read as a brand new one.
_FINDING_WITH_LOCATION = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+)(?: \(byte \d+\))?: (?P<rest>.+)$"
)
_FINDING_WITHOUT_LOCATION = re.compile(r"^(?P<path>.+?): (?P<rest>.+)$")

# `finding_lines()`'s own section-title shape: `"{title} ({count}):"`, printed
# with a 2-space indent by `scan_only()` — one level shallower than an item's
# 4-space indent, which is what tells the two apart.
_FINDING_TITLE = re.compile(r"^  \S.*\(\d+\):$")


class PublishScanRefused(Exception):
    """`scan_commits` could not vouch for a commit: either the push adds a
    finding, or the scan itself — Shipwright's checkout, `publish-engine`,
    either names file, or a parent commit to compare against — could not be
    reached. The caller must refuse the push either way; the message says
    which."""


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


def no_replace_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """`base` (a copy of `os.environ` when `None`), with `GIT_NO_REPLACE_OBJECTS`
    and `GIT_GRAFT_FILE` forced, so every git invocation using this env reads a
    commit's REAL parents and tree, never a `refs/replace/<oid>` substitute or
    a `.git/info/grafts` entry.

    Both are local-only rewrites `git push`'s own transport never honours — it
    always sends the real object — so without this, this gate's own git calls
    (and `publish-engine`'s, when `_scan_added_findings` hands it this same
    env) could read a dirty commit that is genuinely about to reach `origin`
    as a clean stand-in instead. Confirmed by hand: with a `refs/replace/<oid>`
    entry on a commit this gate was about to treat as a trusted boundary, or a
    `.git/info/grafts` line declaring it a different parent, `git rev-list
    <head> --not <oid> <other-trusted-shas>` — the shape
    `commits_new_to_remote`'s own call below still has — dropped a commit
    genuinely new to the remote from its result; with these two set, it
    did not. `run_sandbox.py`'s write
    protection for a dispatched run's own linked worktree is built by
    enumerating paths that exist when the sandbox is set up, so a NEW
    `refs/replace/<oid>` or a NEW `.git/info/grafts` (created after that
    enumeration) is not covered by it either — this is what closes that gap,
    not a write-permission fix."""
    env = dict(os.environ if base is None else base)
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    env["GIT_GRAFT_FILE"] = os.devnull
    return env


def _clean_git_env() -> dict[str, str]:
    """`os.environ`, minus every var `_git_local_env_vars` names, with
    `no_replace_env`'s two overrides forced on top.

    A dispatched run's shell, or a push-time hook invoked by git itself,
    carries `GIT_DIR` (and the rest) pointed at the repository that
    triggered it; every git command this module runs is against a
    DIFFERENT repository (Shipwright) or against the commit already shared
    with the rest of this worktree, and inheriting the redirect makes git
    look for the wrong repository's objects in a store that holds none of
    them — found by hand while building this module, against this very
    checkout."""
    return no_replace_env({k: v for k, v in os.environ.items() if k not in _git_local_env_vars()})


def _peel(ref: str) -> str:
    """`<ref>^{commit}`: the commit `ref` names, resolving an annotated tag
    object to the commit it points at. A no-op for a ref that already names
    a commit, so a caller can use this unconditionally rather than
    special-casing a tag ref by name."""
    return f"{ref}^{{commit}}"


def _rev_list(wt: Path, args: list[str]) -> list[str]:
    """`git rev-list --reverse <args>` in `wt`, oldest first, as a list of
    shas — raising `PublishScanRefused` on a git failure rather than letting
    a caller mistake an empty result for "nothing new" when git itself could
    not answer."""
    result = subprocess.run(
        ["git", "-C", str(wt), "rev-list", "--reverse", *args],
        capture_output=True, text=True, env=_clean_git_env(),
    )
    if result.returncode != 0:
        raise PublishScanRefused(
            f"git rev-list {' '.join(args)} failed in {wt}: {result.stderr.strip()}"
        )
    return [line for line in result.stdout.splitlines() if line]


def _trusted_tracking_shas(wt: Path, remote_name: str, dest: str | None) -> list[str]:
    """Every commit this function will let `commits_new_to_remote` treat as
    "already on `remote_name`" via a local tracking ref, under
    `refs/remotes/<remote_name>/*`: every ref there EXCEPT `dest`'s own
    (`refs/remotes/<remote_name>/<dest>`, when `dest` is given), a ref
    literally named `<remote_name>/HEAD`, and any ref that is SYMBOLIC,
    under whatever name.

    The symbolic exclusion is the general fix (security review of PR #1002,
    round 4): `refresh_remote_tracking_refs`'s fetch refspec is
    `+refs/heads/*:refs/remotes/<remote_name>/*`, and `--prune`'s rule
    (confirmed by hand against a real fetch) removes any ref under that
    destination namespace with no matching source on the remote — with no
    exception keyed on the ref's own name, so a PLAIN ref a dispatched run
    plants at ANY name there, `refs/remotes/<remote_name>/zzz-decoy`
    included, is gone by the very next refresh. A SYMBOLIC ref there is
    different: `--prune` leaves every one of those untouched regardless of
    name or target (confirmed by hand the same way), so one planted at ANY
    name under this namespace — not only `HEAD` — survives any number of
    refreshes, pointing at whatever commit (a local tag, say) it names.
    `run_sandbox.py`'s enumeration-based write protection does not cover a
    ref path absent when a run's sandbox was built, and a brand-new
    remote's entire `refs/remotes/<name>/` subtree is absent from the
    start, so a run can plant such a ref under a name of its own choosing.
    Reading refs by TYPE (`git for-each-ref`'s own `%(symref)`, empty for a
    plain ref) rather than excluding one name at a time closes the whole
    class rather than the one name (`HEAD`) round 4 tested.

    The separate, literal `<remote_name>/HEAD` exclusion stays, on top of
    the type-based one: it is the one case this function must refuse even
    when the ref is a PLAIN one (round 2's own gap) — a plain ref there
    does get pruned by the time any real caller reaches this function
    (every one of them refreshes first), but this function is also tested,
    and trusted, on its own contract, independent of that ordering."""
    result = subprocess.run(
        ["git", "-C", str(wt), "for-each-ref",
         "--format=%(objectname) %(symref) %(refname)", f"refs/remotes/{remote_name}/"],
        capture_output=True, text=True, env=_clean_git_env(),
    )
    if result.returncode != 0:
        raise PublishScanRefused(
            f"git for-each-ref refs/remotes/{remote_name}/ failed in {wt}: "
            f"{result.stderr.strip()}"
        )
    excluded_refs = {f"refs/remotes/{remote_name}/HEAD"}
    if dest is not None:
        excluded_refs.add(f"refs/remotes/{remote_name}/{dest}")
    shas = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        objectname, symref, refname = line.split(" ", 2)
        if symref or refname in excluded_refs:
            continue
        shas.append(objectname)
    return shas


def commits_new_to_remote(
    wt: Path, head: str, remote_sha: str | None, remote_name: str, dest: str | None,
) -> list[str]:
    """The commits reachable from `head` that `remote_name` does not already
    have, oldest first — the one computation both the pre-push hook
    (`bin/push-content-gate`, scanning every ref line a `pre-push` stdin
    describes) and `agentctl publish` (scanning the one branch it is about
    to push) need, so a commit the hook would scan and a commit `publish`
    would scan are always the same set.

    "Already have" means reachable from `remote_sha` (when given and already
    a commit in `wt` — a force-push, or a remote this worktree has never
    fetched, passes `None` instead) or from one of `_trusted_tracking_shas`'s
    own shas (every ref under `refs/remotes/<remote_name>/*`, other than
    `dest`'s own, `<remote_name>/HEAD`, and any symbolic ref under any name
    — see that function's docstring for why each is excluded). `dest`'s own
    tracking ref, `refs/remotes/<remote_name>/<dest>`, is excluded rather
    than trusted: it is the one tracking ref a dispatched run can write
    directly (`run_sandbox.py`'s `WORKTREE_OWN_FILES`), and `agentctl
    publish` is about to overwrite it with the real answer anyway, rather
    than relying on whatever happens to be there already. `dest=None` excludes
    nothing extra, for a destination outside `refs/heads/`."""
    args = [_peel(head), "--not"]
    if remote_sha:
        args.append(_peel(remote_sha))
    args.extend(_trusted_tracking_shas(wt, remote_name, dest))
    return _rev_list(wt, args)


def refresh_remote_tracking_refs(
    wt: Path, url: str, remote_name: str, *, env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """`git fetch --prune` `url`'s own `refs/heads/*`, straight into
    `refs/remotes/<remote_name>/*`, so `commits_new_to_remote`'s own
    `_trusted_tracking_shas` sweep (above) reads `url`'s own CURRENT answer,
    never whatever a dispatched run wrote directly into a NEW
    `refs/remotes/<remote_name>/<anything>` ref on its own — `run_sandbox.py`'s
    write protection for a dispatched run's own linked worktree is built by
    enumerating paths that exist when the sandbox is set up, so a ref path
    that did not exist yet is not covered by it (security review of PR #1002:
    `agentctl publish`'s "already in sync" branch, and `bin/push-content-gate`'s
    pre-push logic, both trusted local tracking refs with no refresh of their
    own). `--prune` also deletes any such planted ref `url` does not actually
    have.

    Every one of `commits_new_to_remote`'s three callers must call this, over
    the exact `url` they are about to trust, immediately before computing the
    commit set; skipping it at any one call site reopens the gap there — this
    is the one call `agentctl publish`'s own push already made (PR #932 round
    2), now shared rather than reimplemented at the other two.

    `env`, when given, is the credentials to fetch with (`agentctl publish`'s
    own App-token push environment, reused here rather than minted twice);
    `None`, the default, fetches under this process's own ambient
    environment — correct for `bin/push-content-gate`'s pre-push hook, already
    running mid-`git push` under whatever credentials authorize `url` already.
    Either way, `no_replace_env` is layered on top, for the same reason every
    other git call in this module carries it."""
    return subprocess.run(
        ["git", "-C", str(wt), "fetch", "--prune", url,
         f"+refs/heads/*:refs/remotes/{remote_name}/*"],
        capture_output=True, text=True, env=no_replace_env(env),
    )


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


# How long `_fetch_shipwright_main`'s own `git fetch` may run before this gate gives up
# and refuses, rather than letting a stalled network call hang every push indefinitely.
_SHIPWRIGHT_FETCH_TIMEOUT_S = 30


def _fetch_shipwright_main(checkout: Path) -> None:
    """`git fetch --no-tags origin refs/heads/main:refs/remotes/origin/main` in
    `checkout`, so `_export_shipwright_tree`'s own `git archive origin/main` (called
    right after this, in every caller) reads Shipwright's CURRENT main rather than
    whatever this checkout's `origin/main` happened to point at the last time someone
    fetched it by hand. On 2026-10-31, after a Shipwright merge at 15:43:07Z, every
    `agentctl publish` kept refusing for hours on a finding that merge had already
    fixed, because nothing in this module ever fetched this checkout — someone had to
    run `git fetch origin` in it by hand before publishing could resume.

    Exactly that one refspec, `--no-tags` so nothing beyond it is written, into the
    plain remote-tracking ref `refs/remotes/origin/main` — never `checkout`'s own HEAD,
    index, or whatever local branch it happens to be on; `git archive origin/main`
    reads a ref, not a checkout, so this never needs one and must never create one.
    `GIT_TERMINAL_PROMPT=0` stops a credential prompt from hanging the push, and the
    bounded timeout stops a stalled network call from doing the same; either failure
    raises `PublishScanRefused`, matching this module's fail-closed stance, rather than
    letting `_export_shipwright_tree` archive a tree that might not be current.

    `_clean_git_env` strips only git's own local-repo variables; its other callers
    never fetch, so a config or transport override reaching them never matters. This
    call does fetch, so on top of `_clean_git_env` it also forces
    `GIT_CONFIG_NOSYSTEM=1` and `GIT_CONFIG_GLOBAL=os.devnull` — the same pin
    `primary_guard.py`'s `_SAFE_GIT_ENV` and `dispatch_env.py`'s
    `git_credential_guard_env` already force, for the same reason — and drops
    `GIT_SSH_COMMAND`, `GIT_SSH`, `GIT_PROXY_COMMAND` and `GIT_ALLOW_PROTOCOL` when the
    caller's own environment set any of them, so none of this process's own config or
    transport restriction can point `origin` at anything other than Shipwright's real
    remote, or block the protocol that remote needs. `checkout`'s own `.git/config`
    still names `origin`'s URL, and ssh reads its own configuration, so authentication
    is unaffected; only an EXTRA override from outside the checkout is closed off.

    Dropping `GIT_ALLOW_PROTOCOL` matters because `github_app.py`'s `push_env` sets it
    to `https` on `agentctl publish`'s own push, scoped to that push's own HTTPS
    remote; `bin/push-content-gate` runs this function from inside that same push's
    pre-push hook and inherits the restriction as plain environment, with nothing left
    to scope it back down. Shipwright's own `origin` is SSH, so that inherited
    `https`-only value made this fetch fail with "fatal: transport 'ssh' not allowed",
    refusing `agentctl publish` from a co session on any repository with the pre-push
    gate hook. A repository with no hook never reaches this fetch under the push.
    Dropping the variable rather than setting it to some other value leaves this
    fetch under git's own default protocol policy, the same policy this fetch has
    when the hook runs under an operator's plain `git push`.

    Skips the fetch entirely when `AGENTCTL_RUN_ID` is set: that means this process is
    itself a dispatched run, and `bin/push-content-gate` runs this module from inside
    such a run's own `git push` (`run_sandbox.py`'s write protection around a
    dispatched run's linked worktree denies a write into the Shipwright checkout from
    inside the run). The fetch below could only fail against that denial, so skipping
    it — rather than unsetting `AGENTCTL_RUN_ID` or otherwise working around the
    sandbox — leaves `_export_shipwright_tree` to archive `origin/main` exactly as this
    checkout already has it, the same as before this fetch existed."""
    if os.environ.get("AGENTCTL_RUN_ID"):
        return
    env = _clean_git_env()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    for var in ("GIT_SSH_COMMAND", "GIT_SSH", "GIT_PROXY_COMMAND", "GIT_ALLOW_PROTOCOL"):
        env.pop(var, None)
    try:
        result = subprocess.run(
            ["git", "-C", str(checkout), "fetch", "--no-tags", "origin",
             "refs/heads/main:refs/remotes/origin/main"],
            capture_output=True, text=True, env=env, timeout=_SHIPWRIGHT_FETCH_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise PublishScanRefused(
            f"fetching {checkout}'s origin main timed out after "
            f"{_SHIPWRIGHT_FETCH_TIMEOUT_S}s; cannot run Builder's content scan on a "
            f"tree that might not be current"
        ) from exc
    if result.returncode != 0:
        raise PublishScanRefused(
            f"could not fetch {checkout}'s origin main: {result.stderr.strip()}"
        )


def _refuse_unsafe_tar_member(checkout: Path, dest: Path, member: tarfile.TarInfo) -> None:
    """By-hand stand-in for `tarfile.data_filter` (PEP 706), for Python 3.11.2 — the
    minimum this gate supports, shipped before the `filter` keyword `extractall` needs
    to use that stdlib filter arrived in 3.11.4. Raises `PublishScanRefused` on every
    shape `data_filter` itself refuses — a path (after resolving any `..` segment)
    landing outside `dest`; a device or FIFO; a member of any type OTHER than a regular
    file, a directory, a symlink or a hardlink (`data_filter`'s own `SpecialFileError`,
    CPython's `_get_filtered_attrs`); and a symlink or hardlink whose own target —
    resolved the same way — lands outside `dest` — PLUS one shape `data_filter` does
    NOT refuse, so this is stricter there rather than an exact match: an absolute
    member NAME. `data_filter` strips a leading `/` and re-checks the now-relative
    name for containment instead of refusing it outright; this function refuses it
    outright, straight off `os.path.isabs`, never stripping and re-checking — strictly
    safer, and `git archive` (the only producer this function ever feeds) never emits
    an absolute member name regardless. Does not reproduce `data_filter`'s mode/
    ownership normalization, which guards a tree meant to outlive this call;
    `_export_shipwright_tree`'s own `dest` is a throwaway temp directory this module
    alone reads and then deletes."""
    name = member.name
    if os.path.isabs(name):
        raise PublishScanRefused(
            f"{checkout}'s origin/main export carries an absolute member name, "
            f"{member.name!r}; refusing to extract it"
        )
    dest_real = os.path.realpath(dest)
    target_real = os.path.realpath(os.path.join(dest_real, name))
    if os.path.commonpath([target_real, dest_real]) != dest_real:
        raise PublishScanRefused(
            f"{checkout}'s origin/main export carries {member.name!r}, which "
            f"resolves outside the extraction directory; refusing to extract it"
        )
    if member.isdev():
        raise PublishScanRefused(
            f"{checkout}'s origin/main export carries a device or FIFO member, "
            f"{member.name!r}; refusing to extract it"
        )
    if not (member.isreg() or member.isdir() or member.issym() or member.islnk()):
        raise PublishScanRefused(
            f"{checkout}'s origin/main export carries a member of a type "
            f"data_filter refuses ({member.type!r}), {member.name!r}; refusing "
            f"to extract it"
        )
    if member.issym() or member.islnk():
        if os.path.isabs(member.linkname):
            raise PublishScanRefused(
                f"{checkout}'s origin/main export carries a link with an absolute "
                f"target, {member.name!r} -> {member.linkname!r}; refusing to "
                f"extract it"
            )
        if member.issym():
            link_target = os.path.join(dest_real, os.path.dirname(name), member.linkname)
        else:
            link_target = os.path.join(dest_real, member.linkname)
        link_target_real = os.path.realpath(link_target)
        if os.path.commonpath([link_target_real, dest_real]) != dest_real:
            raise PublishScanRefused(
                f"{checkout}'s origin/main export carries a link leaving the "
                f"extraction directory, {member.name!r} -> {member.linkname!r}; "
                f"refusing to extract it"
            )


def _export_shipwright_tree(checkout: Path) -> Path:
    """Shipwright's `origin/main`, fetched fresh then extracted into a fresh temp
    directory.

    `_fetch_shipwright_main` runs first, so the archive below reads `origin/main` as it
    stands on Shipwright's own remote right now, not a snapshot from whenever this
    checkout was last fetched by hand. `git archive`, never `git checkout`: neither this
    nor the fetch above ever touches `checkout`'s own working tree or HEAD, so both are
    safe to call against the operator's primary Shipwright checkout even while that
    checkout sits on some other branch.

    Extracts with `tarfile`'s own `data_filter` (PEP 706) when this Python has it;
    `extractall`'s `filter` keyword, needed to request it, arrived only in 3.11.4, so on
    3.11.2 — Debian 12's own packaged Python, and the minimum this gate supports —
    passing it at all raises `TypeError` before extraction even starts. Without it,
    `_refuse_unsafe_tar_member` checks every member by hand first, so this stays
    fail-closed on a `..` escape, a device/FIFO, an unsupported member type, or a link
    leaving `dest`, on every Python this gate runs on — matching what `data_filter`
    itself refuses on every one of those, and refusing one shape MORE than
    `data_filter` does (an absolute member name; see that function's own docstring).
    """
    _fetch_shipwright_main(checkout)
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
            if hasattr(tarfile, "data_filter"):
                tar.extractall(dest, filter="data")
            else:
                members = tar.getmembers()
                for member in members:
                    _refuse_unsafe_tar_member(checkout, dest, member)
                tar.extractall(dest, members=members)
    except tarfile.TarError as exc:
        shutil.rmtree(dest, ignore_errors=True)
        raise PublishScanRefused(
            f"could not extract {checkout}'s origin/main export: {exc}"
        ) from exc
    except PublishScanRefused:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return dest


def _parents(wt: Path, commit: str) -> list[str]:
    """Every parent of `commit`, so `scan_commits` judges each pushed commit
    against ALL of them, never one alone: a merge commit's SECOND parent
    (the branch `origin/main` was merged from, say) can carry a name the
    merge's first parent never had, and comparing against only one parent
    would read that name as added by the merge even though it already sits
    on `origin/main`, reachable through the other parent. A name added by
    one commit and removed by a later commit in the same push must still be
    refused, since the finding reached a ref on origin the instant the
    first commit did, and comparing each commit to its own parents is what
    keeps catching the one that added it regardless of what a later commit
    in the same push does.

    `_EMPTY_TREE` stands in for a root commit's one, absent parent, so its
    own content reads as entirely new rather than refusing for want of
    one — `commit^@` (git-rev-parse(1)'s "all parents of commit" syntax)
    resolves to nothing at all, with no error, for a root commit, which is
    exactly the case this falls back on."""
    result = subprocess.run(
        ["git", "-C", str(wt), "rev-parse", f"{commit}^@"],
        capture_output=True, text=True, env=_clean_git_env(),
    )
    if result.returncode != 0:
        raise PublishScanRefused(
            f"could not resolve the parents of {commit[:9]} in {wt}: "
            f"{result.stderr.strip()}"
        )
    parents = [line for line in result.stdout.splitlines() if line]
    return parents if parents else [_EMPTY_TREE]


def _finding_key(item: str) -> tuple[str, str] | None:
    """`item` (one finding, `finding_lines()`'s 4-space indent already
    stripped) as `(path, rest)`, `rest` being everything after the path and
    any `:line[ (byte N)]` location — the key two occurrences of the same
    finding share even when the location moved. `None` when `item` matches
    neither shape `finding_lines()` prints today."""
    match = _FINDING_WITH_LOCATION.match(item)
    if match is not None:
        return match.group("path"), match.group("rest")
    match = _FINDING_WITHOUT_LOCATION.match(item)
    if match is not None:
        return match.group("path"), match.group("rest")
    return None


def _leading_report_lines(stdout: str) -> list[str]:
    """Every leading line of `stdout` that is one of publish-engine's own
    report lines, i.e. starts with "publish-engine: ": `scan_only`'s own
    "would be refused by a release" header, and, when `--ref` names a
    commit other than the checkout's HEAD, the "pass --ref HEAD" notice
    printed before that header. Matched by the fixed "publish-engine: "
    prefix rather than by wording, so either line is recognised without this
    module parroting its text; stops at the first line that does not start
    that way, so a "publish-engine: "-shaped line appearing later, inside
    the findings body, is never swept in here."""
    lines = stdout.splitlines()
    end = 0
    while end < len(lines) and lines[end].startswith("publish-engine: "):
        end += 1
    return lines[:end]


def _parse_findings(
    stdout: str,
) -> tuple[dict[tuple[str, str], int], dict[tuple[str, str], list[str]]]:
    """`scan_only`'s stdout on a findings exit (1), as `(counts, lines)`:
    `counts[key]` is how many times `key` (see `_finding_key`) occurs, and
    `lines[key]` holds the exact finding line(s) `publish-engine` printed for
    it — reused verbatim in a refusal, never reworded, since the engine
    already withholds every matched value itself.

    The leading run of lines starting with "publish-engine: "
    (`_leading_report_lines`) is the engine's own report: `scan_only`'s
    header, and, when `--ref` named a commit other than the checkout's HEAD,
    the "pass --ref HEAD" notice printed ahead of it. That run is skipped,
    never pattern-matched against its wording beyond the fixed prefix. Empty
    `stdout` (no lines at all — `--scan-only`'s exit of 1 does not promise a
    findings block on every such exit) returns empty, leaving the caller's
    own ambiguity check between a finding and a pre-scan error to raise;
    NON-empty `stdout` whose first line is not shaped like a report line
    FAILS CLOSED here instead, since that is not an ambiguity this function
    can resolve silently. Every line after the leading run is either a
    section title (`finding_lines`'s 2-space indent) or an item (4-space
    indent); anything else — an unparseable item, a "publish-engine: " line
    appearing after the body has started, or a line at neither indent —
    also FAILS CLOSED, raising `PublishScanRefused`, rather than being
    silently dropped from the comparison.
    """
    counts: dict[tuple[str, str], int] = {}
    lines_by_key: dict[tuple[str, str], list[str]] = {}
    lines = stdout.splitlines()
    preamble = _leading_report_lines(stdout)
    if not preamble and lines:
        raise PublishScanRefused(
            f"publish-engine's findings output did not begin with its own "
            f"report line (a line starting \"publish-engine: \"), so this "
            f"gate cannot tell a header from a finding: {stdout!r}"
        )
    body = lines[len(preamble):]
    for raw_line in body:
        if not raw_line.strip():
            continue
        if raw_line.startswith("    "):
            item = raw_line[4:]
            key = _finding_key(item)
            if key is None:
                raise PublishScanRefused(
                    f"publish-engine printed a finding this gate cannot parse "
                    f"into a file and a name, so it cannot tell whether the "
                    f"push adds it: {item!r}"
                )
            counts[key] = counts.get(key, 0) + 1
            lines_by_key.setdefault(key, []).append(item)
            continue
        if raw_line.startswith("  ") and _FINDING_TITLE.match(raw_line):
            continue
        raise PublishScanRefused(
            f"publish-engine printed a line this gate does not recognise as "
            f"a finding or a section title, so it cannot tell what is new: "
            f"{raw_line!r}"
        )
    return counts, lines_by_key


def _could_not_run_message(
    repo_name: str, which: str, ref: str, result: subprocess.CompletedProcess,
) -> str:
    """The same wording `_scan_one` always used for a scan that could not run
    or whose exit (1) does not distinguish a finding from a pre-scan error —
    `which` says whether `ref` is the commit being pushed or one of its
    parents, so a caller reading the message knows which scan failed."""
    reason = (
        "found something to refuse, or failed before the scan started"
        if result.returncode == 1 else "could not run"
    )
    output = (result.stdout or "") + (result.stderr or "")
    return (
        f"publish-engine {reason} (exit {result.returncode}) scanning "
        f"{repo_name} at its {which}, {ref}:\n{output}"
    )


def _scan_added_findings(
    repo_name: str, wt: Path, head: str, bases_fn, *, skip_unknown_repo: bool,
    push_remote: str | None = None,
) -> None:
    """The shared body of `scan_commits`: run Builder's content scan
    (`publish-engine --scan-only`) on `head` in `wt`, scoped to `repo_name`,
    and refuse only a finding present at `head` MORE OFTEN than at every one
    of `bases_fn()`'s commits — `head`'s own parents, each one a commit the
    caller already considers on origin. `bases_fn` is called at most once,
    and only when `head` has findings at all, so a clean `head` never needs
    a parent resolved. A key present at `head` but absent from one parent is
    still not refused when another parent already carries it at least as
    many times — a merge brings nothing new just because one of its two
    sides had not seen it yet.

    Returns when `head` scans clean (exit 0) — a "pass `--ref HEAD`" notice
    is not a finding. Raises `PublishScanRefused` when `head` finds
    something none of `bases_fn()`'s commits do as often, or when any scan
    could not run at all. One exit is read differently depending on
    `skip_unknown_repo`: publish-engine's own argparse rejecting `repo_name`
    itself as an invalid choice (matched precisely against the value it
    names, never any other argument's rejection) means "not this gate's
    business" for `scan_commits(..., skip_unknown_repo=True)`
    (`agentctl publish`'s own call) and "this hook is misconfigured" for
    `skip_unknown_repo=False` (the pre-push hook's call). Every other
    non-zero exit on `head` — a refusal (2: a names file, the purity
    checker, the ref, or a usage error naming some other argument), any
    other positive exit, and a negative exit (killed by a signal) — always
    raises.

    A MISSING CHECKOUT is decided before any of that, by
    `_names_a_locally_known_scannable_repo` rather than by publish-engine's
    own argparse, since there is no engine to ask without one: a `repo_name`
    AND `push_remote` that neither one names as one of the five returns,
    regardless of `skip_unknown_repo`, the same "not this gate's business"
    answer argparse would give once a checkout existed; either one naming a
    scannable repository still raises, unconditionally — this is the
    repository the scan exists for. See that function's own docstring for
    why `repo_name` alone (the checkout's own directory name) is not enough.
    """
    checkout = shipwright_checkout(wt)
    if not (checkout / ".git").exists():
        if not _names_a_locally_known_scannable_repo(repo_name, push_remote):
            return
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

        def run(ref: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                [sys.executable, "-I", str(engine), repo_name,
                 "--scan-only",
                 "--from", str(wt),
                 "--ref", ref],
                capture_output=True, text=True, env=env,
            )

        head_result = run(head)
        if head_result.returncode == 0:
            return

        stderr = head_result.stderr or ""
        match = _INVALID_CHOICE.search(stderr)
        if match is not None and match.group(1) == repo_name:
            if skip_unknown_repo:
                return  # not one of publish-engine's scannable repositories
            raise PublishScanRefused(
                f"publish-engine does not know how to scan {repo_name!r}, but "
                f"this hook is only installed in a clone of a repository it "
                f"scans; the hook, not the push, is misconfigured"
            )

        if head_result.returncode != 1:
            raise PublishScanRefused(
                _could_not_run_message(repo_name, "push", head, head_result)
            )

        head_counts, head_lines = _parse_findings(head_result.stdout or "")
        if not head_counts:
            # Exit 1 with nothing this gate could parse as a finding: the
            # same ambiguity `_scan_one` always refused on, since `--scan-only`
            # does not distinguish a finding from a pre-scan error by exit
            # code alone.
            raise PublishScanRefused(
                _could_not_run_message(repo_name, "push", head, head_result)
            )

        bases = bases_fn()
        max_base_counts: dict[tuple[str, str], int] = {}
        for base in bases:
            base_result = run(base)
            if base_result.returncode == 0:
                base_counts: dict[tuple[str, str], int] = {}
            elif base_result.returncode == 1:
                base_counts, _ = _parse_findings(base_result.stdout or "")
            else:
                raise PublishScanRefused(
                    _could_not_run_message(repo_name, "parent", base, base_result)
                )
            for key, count in base_counts.items():
                if count > max_base_counts.get(key, 0):
                    max_base_counts[key] = count

        refused = [
            (key, head_count, max_base_counts.get(key, 0))
            for key, head_count in head_counts.items()
            if head_count > max_base_counts.get(key, 0)
        ]
        if refused:
            # The head scan's own leading "publish-engine: " report line(s)
            # are carried along first, unchanged — `push-gate-verify` greps a
            # refusal for publish-engine's own "publish-engine: ..." report
            # line specifically, and dropping it here would be exactly the
            # truncated, context-free paste that tool exists to prevent.
            #
            # Each refused key is printed with its own head/base counts,
            # then only the HEAD's own lines for that key — never a base's
            # lines, and never every one of the head's occurrences lumped
            # under one unlabelled count, both of which would read as the
            # gate counting content the push does not actually add.
            head_preamble = _leading_report_lines(head_result.stdout or "")
            base_desc = ", ".join(base[:9] for base in bases)
            body: list[str] = []
            for key, head_count, base_count in refused:
                path, rest = key
                body.append(
                    f"{path}: {rest}: {head_count} at {head[:9]}, "
                    f"{base_count} at {base_desc}"
                )
                body.extend(f"  {line}" for line in head_lines[key])
            raise PublishScanRefused("\n".join(head_preamble + body))
    finally:
        if export_dir is not None:
            shutil.rmtree(export_dir, ignore_errors=True)


def scan_commits(
    repo_name: str, wt: Path, shas: list[str], *, skip_unknown_repo: bool = False,
    push_remote: str | None = None,
) -> None:
    """`_scan_added_findings`, run once per commit in `shas`, in order,
    stopping at the first refusal. Backs both the pre-push hook, which must
    scan every new commit a push carries, not only the one at its tip, and
    `agentctl publish`, which runs this against every repository it pushes,
    not only the five publish-engine knows how to scan.

    Each commit is compared against ALL of its own parents (`_parents`), not
    a single base shared by the whole push and not its first parent alone:
    a name added by one commit and removed by a later commit in the same
    push must still be refused, since the finding reached a ref on origin
    the instant the first commit did; and a merge commit's non-first parent
    can carry a name the merge's first parent never had, so judging a merge
    against every parent is what keeps a merge of `origin/main` into an
    existing branch from reading main's own history as newly added.

    A file renamed between a commit's parent and the commit itself is NOT
    recognised as the same file: a finding keyed by its old path at the
    parent and its new path at the commit reads as the old one disappearing
    and a new one appearing, and the new one is refused. This gate does not
    correlate file identity across a rename, deliberately — a renamed file
    that also picked up a finding along the way must not be waved through by
    the rename alone, and failing toward a refusal here costs the same
    one-line fix a genuinely new name would.

    `skip_unknown_repo` decides how publish-engine's own argparse rejecting
    `repo_name` as an invalid choice reads: `True` (`agentctl publish`'s own
    call) means "not this gate's business", since `publish` pushes
    repositories outside publish-engine's five scannable ones; `False`, the
    default (the pre-push hook's call, only ever installed in a clone of a
    repository publish-engine scans), means the hook itself is
    misconfigured and raises. With no Shipwright checkout at all —
    `_names_a_locally_known_scannable_repo` standing in for argparse, since
    there is no engine to ask — a `repo_name`/`push_remote` pair that names
    neither one of the five returns regardless of `skip_unknown_repo`, and
    either one naming a scannable repository still raises regardless of
    `skip_unknown_repo`: an adopter instance's own pre-push hook, were one
    ever installed into a repository sharing one of the five names without
    Shipwright behind it, fails closed exactly as `agentctl publish` does.

    `push_remote` is consulted ONLY inside that no-checkout carve-out, as the
    independent, directory-name-proof check described on
    `_names_a_locally_known_scannable_repo`: the literal URL this push is
    going to (the pre-push hook's own `$2`), or the worktree's own `origin`
    remote when `agentctl publish` is calling before any push has
    happened — `None` (the default) when a caller has neither, read as
    unresolved, same as a remote `_names_a_locally_known_scannable_repo`
    cannot parse.

    Raises `PublishScanRefused` on the first refused commit, its message
    prefixed with `repo_name`, that commit's short sha, and its position as
    "k of n" among `shas`, so a push of several commits says which one needs
    fixing. Returns when every commit passed.
    """
    total = len(shas)
    for index, sha in enumerate(shas, start=1):
        try:
            _scan_added_findings(
                repo_name, wt, sha,
                lambda sha=sha: _parents(wt, sha),
                skip_unknown_repo=skip_unknown_repo,
                push_remote=push_remote,
            )
        except PublishScanRefused as exc:
            raise PublishScanRefused(
                f"{repo_name} at {sha[:9]} ({index} of {total}): {exc}"
            ) from exc
