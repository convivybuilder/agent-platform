"""Worktree management for the agent platform.

Discovery + status + allocation for the worktrees we hand out to agents.

Layout (matches 3.5 spec):
  <root>/<repo>/                              # primary
  <root>/worktrees/<repo>.<purpose>/          # additional

`knowledge` is one of the primaries under <root>/ (parallel to every product
and project repo). The `worktrees/` subdir itself is naturally skipped from primary
discovery because git worktree adds put a `.git` *file* in the extra, not a
`.git/` directory in the holder dir.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import builder_root as BR
import git_refs
import github_app as GA
import projects as PJ
import registry as R
import run_sandbox as RS
import safe_path as SP

# The machine's one root (company/decisions/0008). The name predates the
# resolver and is kept because callers and tests use it.
CONVIVY_ROOT = BR.root()
EXTRA_WT_DIRNAME = "worktrees"

# Repos under <root>/ that are NOT valid spawn targets even though they're
# git repos at depth 1. `agent-platform` is the platform's own backed-up code
# (#43); agents shouldn't be dispatched against it via the standard worktree
# allocation flow — platform edits go through `~/.local/` and sync to the repo,
# not the other way round.
DISCOVERY_EXCLUDES = frozenset({"agent-platform"})
# The exclusion scopes listing and dispatch discovery only. The cleanup job
# passes `include_excluded=True`: the platform's own dispatched worktrees pile up
# like any product's, and would otherwise sit outside every reap.

DEFAULT_BASE_BRANCH = "main"

# Untracked debris that must not block reaping: virtualenvs, dependency dirs,
# tool caches, and the platform's own pr-open artifact. Anything tracked, and
# any untracked file outside these sets, still counts as real work.
_DEBRIS_DIRS = frozenset({
    ".venv", "venv", "node_modules", "__pycache__",
    ".pytest_cache", ".ruff_cache", ".mypy_cache",
    "htmlcov", ".build",
})
# `.coverage*` is matched by prefix below; these are the DOTLESS coverage artifacts,
# which that prefix misses. Real case: a swept product-repo review tree held a `coverage.json`,
# and without this the ARCH-344 reap would classify it as work-to-preserve and keep
# every such tree forever — re-opening the leak it exists to close, silently.
_DEBRIS_FILES = frozenset({".DS_Store", "coverage.json", "coverage.xml"})
_DEBRIS_ROOT_FILES = frozenset({
    "pr-body.md",                # platform's own PR-body file — fixed name, not a review artifact
    "review.md",                 # reviewer's review body left at the root — no instruction
                                 # names this file (profile examples: /tmp/review-<pr>-<round>.md,
                                 # gitdir review-comment.md), so a plain-name catch; exact because
                                 # the review[-_] branch needs a separator
    "explanation.md",            # delivered review artifact — exact name so the pattern stays narrow
})  # root-only by design (see _is_debris: exact-path match, no basename check)

# Pattern for delivered review artifacts at the worktree root. The platform mandates
# per-PR per-round reviewer filenames (review-pr<N>.md, security-review-pr<N>.md) so
# concurrent reviewers don't overwrite each other — an exact-name set can never track a
# naming convention designed to be unique per invocation.
#
# EVERY BRANCH RUNS TO `.md$`, and that is the guard rather than the prefix list.
# `_is_debris` is the last check before `git worktree remove --force` deletes a file
# for good, so the question is not "does this name start like an artifact" but "is
# this name an artifact name". An earlier round ended each branch with `.*\.md$`,
# which let a prefix carry anything after it: `review-1-of-3.md` and
# `review-123-draft.md` were classified as debris on the strength of their first
# few characters. Nothing may follow the shape a generated name has.
#
# THE DIGIT RUN IS DELIBERATELY UNBOUNDED. Security review of #320 proposed
# `\d{1,3}` to keep a year-named `review-2024.md` out. That would drop real
# artifacts: PR numbers passed 999 some time ago — one product repo is past
# #1114 — so `review-1114.md` is a name the platform generates today. A four-digit review
# summary at a worktree root collides with a PR number and is accepted as a
# residual, named here rather than left for the next reader to rediscover.
#
# Not matched, and each was a real draft name someone could leave behind:
# review-notes.md, reviewers-guide.md, review-of-our-hiring-process.md,
# review-1-of-3.md, review-123-draft.md, slack-post-draft.md,
# slack-post-announcement.md, security-review-of-approach.md.
# A round suffix: -r2, _r2, -round1. Carried by the _NUM and pr-\d+-review branches
# only — the round\d+ branch drops it because the word "round" already names the
# number, so review-round3-r2.md is a deliberate non-match (pinned in the tests).
_ROUND = r"(?:[-_]r(?:ound)?[-_]?\d+)?"
# A PR number, which the convention writes with or without its `pr` prefix.
_NUM = r"(?:pr)?\d+"
_DEBRIS_ROOT_FILE_RE = re.compile(
    r"^(?:"
    rf"review[-_](?:comment|round[-_]?\d+|{_NUM}{_ROUND})"
    rf"|security-review[-_](?:comment|{_NUM}{_ROUND})"
    rf"|pr-\d+-review{_ROUND}"
    rf"|slack-post(?:[-_]{_NUM})?"
    r")\.md$"
)

# The manifest `knowledge-ctx use` writes into a dispatched worktree's `.claude/`,
# listing every path that individuation owns (including itself). Mirrored by
# knowledge-ctx's own _MANIFEST_NAME and pinned by a test: the two sit either side
# of the deployed-lib seam (bin/ tools import from ~/.local/lib), so a shared
# constant would break every branch until the lib half merged.
_MANIFEST_NAME = ".materialized"
# The record `knowledge-ctx use` writes beside the manifest. Its content carries a
# per-render timestamp, so it is rewritten on every render that changes anything
# and marks the end of the render's writes when the manifest was left as is.
# Mirrored the same way as _MANIFEST_NAME, and pinned by a test.
_PROFILE_DOCUMENTS_NAME = ".profile-documents.json"


@dataclass
class Worktree:
    path: Path                  # absolute path to the worktree
    primary: Path               # absolute path to the primary repo this worktree belongs to
    label: str                  # short label for tables/registry, e.g. "acme" or "worktrees/acme.overnight"
    branch: str | None          # current branch name (None if detached)
    is_primary: bool            # True if this is the primary worktree (path == primary)
    locked: bool                # `git worktree lock` flag (e.g. Claude Code session worktrees)
    dirty_files: int            # count of changed/untracked files
    real_dirty_files: int       # dirty_files minus untracked debris (venvs, caches, pr-body.md)
    ahead: int                  # commits ahead of upstream / base (best-effort)
    behind: int                 # commits behind upstream / base
    base_branch: str            # branch used for ahead/behind comparison


# --- discovery ---------------------------------------------------------------


def _registry_homes() -> list[Path]:
    """Every registered unit's home directory, when it is a real git checkout
    under CONVIVY_ROOT — the registry homes `discover_primaries`'s depth-1 scan
    cannot see, because a crew's home may be nested one level deeper
    (`<root>/area/demo-nested`, the live shape a registry home takes today).

    `projects.home_path` already refuses a `home_dir` that resolves outside this
    user's home (`_expand_own_home`); the `is_relative_to(CONVIVY_ROOT)` check
    here is a second, narrower containment test against THIS module's own root
    rather than `projects.crews_root()` — the two agree in production (both
    read `builder_root.root()`), but a test that patches only `CONVIVY_ROOT`
    must not pick up the operator's real registry homes, and a registry whose
    root has otherwise drifted from this module's must not hand back a path
    outside it either.

    Best-effort, like the depth-1 scan beside it: an unreadable or malformed
    registry, or one entry with a malformed `home_dir`, contributes nothing
    rather than raising — this is discovery, not a dispatch gate that must
    fail closed."""
    try:
        units = PJ.load()
    except (OSError, ValueError):
        return []
    root = CONVIVY_ROOT.resolve()
    out: list[Path] = []
    for slug in units:
        try:
            home = PJ.home_path(slug)
        except ValueError:
            continue
        if home is None or not home.is_relative_to(root):
            continue
        if (home / ".git").is_dir():
            out.append(home)
    return out


def _registry_primary_for(repo: str) -> Path | None:
    """A registry unit's home directory for `repo`, when that home is nested
    below CONVIVY_ROOT rather than a direct child (`_primary_for`'s plain
    `CONVIVY_ROOT / repo` join cannot see it). Matches a unit that lists `repo`
    in its own `repos`, whose `home_path` resolves to a directory basenamed
    `repo` and which is itself a real git checkout — `None` otherwise,
    including for an unreadable registry or a malformed entry (same
    best-effort contract as `_registry_homes`)."""
    try:
        units = PJ.load()
    except (OSError, ValueError):
        return None
    root = CONVIVY_ROOT.resolve()
    for slug, unit in units.items():
        if repo not in (unit.get("repos") or []):
            continue
        try:
            home = PJ.home_path(slug)
        except ValueError:
            continue
        if home is None or home.name != repo or not home.is_relative_to(root):
            continue
        if (home / ".git").is_dir():
            return home
    return None


def discover_primaries(*, include_excluded: bool = False) -> list[Path]:
    """Git repos under <root>/: every depth-1 one, plus a registered unit's
    home directory nested deeper (`_registry_homes`). Sorted alphabetically.

    Skips the `worktrees/` collection dir itself (its children are extras with
    `.git` *files* pointing back to their primary; only primaries have a real
    `.git/` directory). `include_excluded` also returns the DISCOVERY_EXCLUDES
    repos, for the cleanup job."""
    primaries: list[Path] = []
    seen: set[Path] = set()
    if CONVIVY_ROOT.is_dir():
        for child in sorted(CONVIVY_ROOT.iterdir()):
            if not child.is_dir() or child.name == EXTRA_WT_DIRNAME:
                continue
            if child.name in DISCOVERY_EXCLUDES and not include_excluded:
                continue
            if (child / ".git").is_dir():
                primaries.append(child)
                seen.add(child.resolve())
        for home in _registry_homes():
            resolved = home.resolve()
            if resolved in seen:
                continue
            if home.name in DISCOVERY_EXCLUDES and not include_excluded:
                continue
            primaries.append(home)
            seen.add(resolved)
    return sorted(primaries)


def _label_for(path: Path, primary: Path) -> str:
    """Short label for tables / registry. `<root>/acme` → `acme`;
    `<root>/worktrees/acme.overnight` → `worktrees/acme.overnight`."""
    try:
        rel = path.relative_to(CONVIVY_ROOT)
        return str(rel)
    except ValueError:
        return str(path)


# --- git plumbing ------------------------------------------------------------


def _git(primary: Path, *args: str, cwd: Path | None = None,
         check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd or primary),
        capture_output=True,
        text=True,
        check=check,
    )


def import_leftover_private_objects(wt: Path) -> bool:
    """Rejoin a dead run's own commits into the shared store before this module
    reads or resets `wt`'s HEAD.

    A dispatched run's own git writes to a private object store
    (`run_sandbox.private_objects_dir`) and the dispatcher imports it into the
    shared store once the run ends (`run_sandbox.import_private_objects`). If
    the dispatcher process itself dies first — a documented recurring
    incident, not a hypothetical (P1b, 2026-07-04) — that import never runs,
    and the run's commit is reachable only through the private store: an
    ordinary `rev-list`/`reset`/`push`/`status` against the shared store
    cannot see it at all. Every caller here that is about to read HEAD's
    ancestry, its working-tree state, or reset either away must fold that
    leftover store in first, so a dead run's commit is never discarded for
    want of an import nobody ran. A no-op when `wt` is not a git checkout, or
    carries no leftover private store.

    Returns True iff a private store existed on disk (whether or not it held
    anything to import). A caller that already read this worktree's dirty or
    ahead state BEFORE calling this — `cleanup_candidates`/`recheck_candidate`,
    whose `Worktree` snapshot was built by an earlier, separate scan — uses the
    return value to know that snapshot predates the import and must be
    re-read, rather than re-reading unconditionally on every worktree.

    Imports WITHOUT removing (`remove=False`) when `wt` is still held by a live
    run, per `in_use_map()` — the same registry-backed liveness `allocate_worktree`/
    `allocate_on_branch`/`cleanup_candidates`/`recheck_candidate` already check before
    calling this, re-read here rather than trusted from an earlier snapshot (CQ·477,
    run-421dd904, 2026-10-12): every existing call site already refuses or skips
    before reaching here when its OWN `in_use_map()` read found the worktree in use,
    but each of those reads happened earlier in the same call, and a run can start
    registering itself against `wt` in the gap between that read and this one —
    `cleanup_candidates` loops over every worktree on one `in_use_map()` snapshot
    taken before the loop starts. Deleting the private store of a run that is still
    writing to it is the incident: `shutil.rmtree`'d a store the live run's own git
    was still pointed at (`GIT_OBJECT_DIRECTORY`), breaking every git call for the
    rest of that run. `remove=False` is exactly the posture `import_private_objects`
    already defines for a live run (`_stream_transcript`'s periodic tick): fold in
    whatever is safely resolvable now, delete nothing, re-import cheaply next time."""
    git_dir = RS.git_common_dir(wt)
    if git_dir is None:
        return False
    private_dir = RS.run_private_objects_dir(wt, git_dir)
    existed = private_dir.is_dir()
    live = wt.resolve() in in_use_map()
    RS.import_private_objects(git_dir, private_dir, remove=not live)
    return existed


def _status_entries(wt: Path, *args: str, check: bool = True) -> list[str] | None:
    """`git status --porcelain -z` as `XY path` strings, names exactly as on disk.

    Plain porcelain quotes any name with a non-ASCII byte, tab, quote or backslash
    (`?? ".claude/skills/caf\\303\\251/"`), and a caller that unquotes by trimming
    the quote characters is left with escapes that name no file. `-z` prints every
    name raw. Bytes are decoded with surrogateescape so a name that is not UTF-8
    still round-trips into a pathspec instead of raising.

    A rename or copy entry carries its origin as a second NUL-separated field; the
    entry keeps the new path and the origin is dropped.

    `check=False` returns None when git cannot read the tree; `check=True` raises.
    """
    cp = subprocess.run(
        ["git", "status", "--porcelain", "-z", *args],
        cwd=str(wt), capture_output=True, check=check,
    )
    if cp.returncode != 0:
        return None
    fields = cp.stdout.decode("utf-8", "surrogateescape").split("\0")
    entries: list[str] = []
    i = 0
    while i < len(fields):
        f = fields[i]
        i += 1
        if len(f) < 4:
            continue
        entries.append(f)
        if f[0] in "RC" or f[1] in "RC":
            i += 1
    return entries


_WT_BLOCK_RE = re.compile(r"^(?P<key>\S+)(?:\s+(?P<value>.*))?$")


def _parse_worktree_porcelain(out: str) -> list[dict]:
    """Parse `git worktree list --porcelain` into records."""
    records: list[dict] = []
    cur: dict = {}
    for line in out.splitlines():
        if not line.strip():
            if cur:
                records.append(cur)
                cur = {}
            continue
        m = _WT_BLOCK_RE.match(line)
        if not m:
            continue
        key = m.group("key")
        value = m.group("value") or ""
        cur[key] = value
    if cur:
        records.append(cur)
    return records


def _branch_name(record: dict) -> str | None:
    """Extract a friendly branch name from a porcelain record. None if detached."""
    if "detached" in record:
        return None
    ref = record.get("branch", "")
    if ref.startswith("refs/heads/"):
        return ref[len("refs/heads/"):]
    return ref or None


def _is_debris(path: str) -> bool:
    """True if an untracked path is disposable build/run debris (see _DEBRIS_*)."""
    if path in _DEBRIS_ROOT_FILES:
        return True
    if "/" not in path and _DEBRIS_ROOT_FILE_RE.match(path):
        return True
    parts = [p for p in path.rstrip("/").split("/") if p]
    if not parts:
        return False
    base = parts[-1]
    if base in _DEBRIS_FILES or base.startswith(".coverage"):
        return True
    return any(p in _DEBRIS_DIRS or p.endswith(".egg-info") for p in parts)


def _claude_dir_is_only_individuated(wt: Path) -> bool:
    """True iff the untracked `.claude/` holds ONLY what the platform put there.

    `agentctl spawn` individuates the knowledge profile into every dispatched
    worktree (activate_profile → `knowledge-ctx use --force`) BEFORE the agent
    runs, so `.claude/` is the platform's own scaffolding, not agent output. It is
    untracked in every product repo, so porcelain reports a bare `?? .claude/`.

    That bare entry defeated the ARCH-344 reap on EVERY review tree from the day
    it shipped: the guard read the dispatcher's own manifest as "a change a
    reviewer should not have made" and kept the tree. In one snapshot of the fleet
    (2026-07-23) 74 of 275 trees were held by exactly this, and nothing else. The
    fleet is live — a dispatch creates trees while you measure — so treat that as
    one reading, not a constant.

    Answered from the MANIFEST knowledge-ctx writes, never from the shape of the
    names. A first draft compared only the four top-level entries, and
    security-reviewer showed why that is unsafe: an agent writing
    `.claude/agents/exfil.md` leaves the top-level set unchanged, so the tree would
    be cleared and the artifact destroyed. The rmtree that would have removed it
    runs at DISPATCH START, before the agent; the reap runs after it exits, with no
    knowledge-ctx in between. That is precisely the evidence ARCH-344 preserves.

    So: compare every path under `.claude/` against the manifest, exactly. Any
    extra file — inside `agents/`, inside `skills/`, or at the top — returns False
    and keeps the tree.

    NO MANIFEST MEANS FALSE. A tree individuated before manifests existed cannot
    prove what its `.claude/` holds, and unprovable is not safe: those keep, and a
    human sweeps them deliberately. New dispatches carry one and reap on their own.

    A LISTED FILE NEWER THAN THE RENDER ALSO MEANS FALSE (`_written_by_render`): the
    manifest names paths, and an agent that rewrote one of them changed its content
    under a name the manifest still vouches for.

    This answers only the bare `?? .claude/` shape, where the whole directory is
    untracked. A repo that tracks part of `.claude/` reports files instead, and
    `_unexplained_entries` judges those one by one against the same record.
    """
    rec = _read_render_record(wt)
    if rec is None:
        return False          # absent or unreadable manifest — cannot prove, so keep
    claude = wt / ".claude"
    try:
        actual = {str(p.relative_to(claude)) for p in claude.rglob("*") if p.is_file()}
    except OSError:
        return False          # unreadable is not clean
    return actual == rec.owned and all(
        _written_by_render(wt, rec, rel) for rel in actual)


@dataclass(frozen=True)
class _RenderRecord:
    """What the last `knowledge-ctx use` in a worktree says it wrote."""
    owned: frozenset[str]     # paths relative to `.claude/`, from the manifest
    finished_ns: int          # nothing the render wrote is newer than this


def _read_render_record(wt: Path) -> _RenderRecord | None:
    """The render's own manifest for this worktree, or None when it cannot be trusted.

    None for: no manifest, a manifest that is not a regular file, a `.claude` that
    is itself a symlink, or anything unreadable. Every caller reads None as "keep".

    `finished_ns` is the later of the manifest's and `.profile-documents.json`'s
    mtime. The render writes both after every agent, skill and CLAUDE.md file. The
    manifest is skipped when its content is unchanged, and the documents record
    carries a fresh timestamp, so it is rewritten on every render that changed
    anything. The later of the two therefore marks the end of the last render.
    """
    claude = wt / ".claude"
    try:
        if claude.is_symlink():
            return None
        manifest = claude / _MANIFEST_NAME
        st = manifest.lstat()
        if not stat.S_ISREG(st.st_mode):
            return None
        owned = frozenset(ln.strip() for ln in manifest.read_text().splitlines() if ln.strip())
    except (OSError, UnicodeDecodeError):
        return None
    finished = st.st_mtime_ns
    try:
        docs = (claude / _PROFILE_DOCUMENTS_NAME).lstat()
        if stat.S_ISREG(docs.st_mode):
            finished = max(finished, docs.st_mtime_ns)
    except OSError:
        pass
    return _RenderRecord(owned=owned, finished_ns=finished)


def _written_by_render(wt: Path, rec: _RenderRecord, rel: str) -> bool:
    """True iff `.claude/<rel>` is a file the render listed and nothing has touched since.

    The manifest holds paths and no hashes, so "unchanged since the render" is read
    from the mtime: a listed file modified after the render finished was modified by
    something else. Deliberately answered as "not newer than the render" rather than
    "equal to it", because a render that finds a file already current does not
    rewrite it. This catches an agent editing a file the render wrote. It does not
    catch a deliberate `touch -r` to hide the edit; the manifest is itself
    agent-writable, so the guard has never claimed to stop that.
    """
    if rel not in rec.owned:
        return False
    try:
        st = (wt / ".claude" / rel).lstat()
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_mtime_ns <= rec.finished_ns


def _render_cleared(wt: Path, rec: _RenderRecord, rel: str) -> bool:
    """True iff `.claude/<rel>`, deleted from the tree, is the render's own `--force` clear.

    `agentctl spawn` runs `knowledge-ctx use --force`, which empties `agents/` and
    `skills/` and rewrites only what the profile lists. In a repo that tracks files
    there, the ones the profile no longer lists show as ` D`. That is a deletion of
    committed content, so nothing is lost by dropping it, but the guard still wants
    evidence the render did it: the path is under one of the two cleared dirs, the
    manifest does not list it (a listed file was rewritten, so its absence is a later
    deletion), and the nearest surviving directory above it was not modified after
    the render finished.
    """
    if rel in rec.owned or rel.split("/", 1)[0] not in ("agents", "skills"):
        return False
    claude = wt / ".claude"
    parent = (claude / rel).parent
    while parent != claude:
        try:
            st = parent.lstat()
        except FileNotFoundError:
            parent = parent.parent
            continue
        except OSError:
            return False
        return stat.S_ISDIR(st.st_mode) and st.st_mtime_ns <= rec.finished_ns
    return False


class _RenderJudge:
    """Reads a worktree's render record once, on first use, for a loop over its status."""

    def __init__(self, wt: Path):
        self.wt = wt
        self._loaded = False
        self._rec: _RenderRecord | None = None

    @property
    def rec(self) -> _RenderRecord | None:
        if not self._loaded:
            self._rec = _read_render_record(self.wt)
            self._loaded = True
        return self._rec

    def explains(self, code: str, path: str) -> bool:
        """True iff the render wrote (or cleared) exactly this status entry."""
        prefix = ".claude/"
        rec = self.rec
        if rec is None or not path.startswith(prefix):
            return False
        rel = path[len(prefix):]
        if code in ("??", " M"):
            return _written_by_render(self.wt, rec, rel)
        if code == " D":
            return _render_cleared(self.wt, rec, rel)
        return False          # staged, renamed, type-changed, conflicted: the agent's


def _unexplained_entries(wt: Path, line: str, judge: _RenderJudge) -> list[str]:
    """The porcelain lines behind `line` that a human would miss if the tree went.

    Empty when `line` is platform scaffolding: the dispatcher's own deletion of a
    root instruction file, disposable untracked debris, or an entry under `.claude/`
    that the render's manifest accounts for. The ONE place all three are decided,
    because `unlanded_entries` and `_dirty_counts` must never disagree.

    An untracked directory under `.claude/` is opened into its files first. A repo
    that tracks part of `.claude/` reports `?? .claude/skills/x/` rather than a bare
    `?? .claude/`, and the manifest lists files.
    """
    if _is_platform_suppression(line):
        return []
    code, path = line[:2], line[3:]
    if code == "??" and _is_disposable_untracked(wt, path):
        return []
    if not path.startswith(".claude/"):
        return [line]
    if code == "??" and path.endswith("/"):
        # An untracked-directory line proves at least one untracked file is in it.
        # An expansion that finds none is a lookup that failed, and keeps the tree.
        found = _status_entries(wt, "--untracked-files=all", "--", path,
                                check=False)
        files = [e[3:] for e in found or () if e.startswith("??")]
        if not files:
            return [line]
        return [f"?? {f}" for f in files if not judge.explains("??", f)]
    return [] if judge.explains(code, path) else [line]


# The repo-root instruction files `agentctl` strips from a dispatched worktree
# before the agent starts, so a branch cannot instruct the agent working on it.
# Kept here as well as in agentctl because THIS module has to recognise the
# platform's own deletion; the two lists are asserted equal by a test.
SUPPRESSED_INSTRUCTION_FILES = ("CLAUDE.md", "AGENTS.md")


def _is_platform_suppression(line: str) -> bool:
    """True for the ` D CLAUDE.md` / ` D AGENTS.md` the DISPATCHER itself made.

    Same reasoning as `_claude_dir_is_only_individuated` one function up: a change
    the platform made to a worktree before the agent started is not the agent's
    work, and reading it as work-worth-keeping breaks the thing it protects. There
    it kept 74 of 275 trees from being reaped; here an unexempted deletion makes
    `allocate_on_branch` refuse the ARCH-347 round-fix reuse on the very next
    round after any coder dispatch.

    NARROW ON PURPOSE — only an unstaged DELETION (` D`), only those two exact
    root paths. A MODIFICATION to one of them, or a staged deletion, is the agent
    doing something and stays visible. This is the whole reason the deletion is
    left plain rather than hidden with `--skip-worktree`: that bit blinds git to
    the path permanently, so a later prompt-injected write to CLAUDE.md would
    produce no porcelain output at all and defeat the ARCH-344 guarantee this
    function exists to provide (reviewer, PR #486).
    """
    if not line.startswith(" D "):
        return False
    return line[3:] in SUPPRESSED_INSTRUCTION_FILES


def is_primary_checkout(repo: str, path: Path) -> bool:
    """True if `path` IS the repo's primary checkout rather than a worktree.

    `allocate_worktree(repo, purpose=None)` resolves to the primary. A dispatch
    reaches it only for a repo-local agent or a private crew's, which take
    no render and work on untracked files in the clone; every other dispatch gets
    its own worktree. Anything destructive in the dispatch path has to ask this
    before acting, because the primary is not disposable.
    """
    try:
        return _primary_for(repo).resolve() == Path(path).resolve()
    except Exception:
        # CANNOT TELL MEANS TREAT AS PRIMARY. Deliberately broad: `_primary_for`
        # raises ValueError for an unknown repo, not just OSError, and a narrow
        # catch let that escape and crash the dispatch. The two errors are not
        # symmetric — answering "primary" wrongly skips a suppression, while
        # answering "not primary" wrongly DELETES FILES from a human's clone.
        return True


def unlanded_entries(wt: Path) -> list[str]:
    """Every `git status --porcelain` entry that is NOT disposable build debris — that
    is, everything a human would miss if this tree were deleted.

    Counts tracked changes AND untracked non-debris files. The untracked half is the
    point (ARCH-344): `git worktree remove --force` deletes untracked files silently,
    so if a reviewer ever wrote a file — a crafted diff inducing it via prompt
    injection is the case that matters — reaping the tree would destroy the only
    evidence. Debris (venvs, caches, node_modules, coverage output, a leftover
    pr-body.md) is not evidence and does not block a reap. Neither is the `.claude/`
    the dispatcher itself individuated, when it holds nothing else, nor the
    dispatcher's own deletion of the repo-root instruction files it strips before
    the agent starts (`_is_platform_suppression`). A file under `.claude/` counts as
    the dispatcher's too when the render's manifest lists it and nothing has touched
    it since (`_unexplained_entries`), which covers the tracked ` M` a repo that
    commits its `.claude/` shows after every render.

    Raises CalledProcessError if git cannot read the tree. An unreadable tree is not a
    clean tree, and the caller must not treat it as one.
    """
    lines = _status_entries(wt, check=True)
    judge = _RenderJudge(wt)
    entries = []
    for line in lines or ():
        entries.extend(_unexplained_entries(wt, line, judge))
    return entries


def unpublished_commits(wt: Path) -> int:
    """Commits reachable from this tree's HEAD but from NO remote-tracking ref — work
    that exists only here and has no recovery path if the tree is deleted.

    `unlanded_entries` cannot see this: a worker that committed and then died before
    `publish` leaves a perfectly CLEAN working tree whose commits have never left the
    machine (the case test_allocate_on_branch_refuses_committed_but_unpublished_work
    pins). Any sweep that deletes on cleanliness alone would drop exactly that work.

    Deliberately NOT `_ahead_behind`, which compares against the local base branch and
    returns (0, 0) on any git error — best-effort is right for a status column and
    wrong for a delete gate, where an error would read as "nothing to lose". This
    raises instead: a tree git cannot read is never a tree we may remove.

    `--not --remotes` asks the question that actually matters (is it anywhere else?)
    rather than "is it ahead of main", so it is correct for a detached HEAD and for a
    branch whose upstream was deleted after merge.

    It counts a SQUASH-MERGED branch's commits as unpublished, and that is the
    intended reading rather than a defect to fix later. A squash rewrites the work
    into one new commit, so the originals are ancestors of nothing on the remote —
    the content survives upstream, the commits do not. On the 2026-07-23 fleet that
    was most of what this kept. Erring toward keeping a tree whose work is
    recoverable is cheap; erring the other way is unrecoverable, and a caller
    wanting those trees gone can confirm the merge and remove them by hand.
    """
    cp = _git(wt, "rev-list", "--count", "HEAD", "--not", "--remotes", cwd=wt, check=True)
    return int(cp.stdout.strip() or 0)


def _is_disposable_untracked(wt: Path, path: str) -> bool:
    """True if an untracked porcelain path carries nothing a human would miss.

    The ONE place that question is answered, because it is asked from two paths
    that must never disagree: `unlanded_entries` (the ARCH-344 per-run reap) and
    `_dirty_counts` (`real_dirty_files`, which `cleanup_candidates` and so
    `bin/worktrees-cleanup` gate on). They had drifted — both were blind to the
    dispatcher's own `.claude/`, so BOTH kept every review tree, one reporting a
    guarded reap and the other `skip:dirty`.
    """
    return _is_debris(path) or (
        path.rstrip("/") == ".claude" and _claude_dir_is_only_individuated(wt))


def _dirty_counts(wt: Path) -> tuple[int, int]:
    """(total, real) changed/untracked entries from `git status --porcelain`.

    `real` excludes untracked debris — a worktree whose only dirt is a venv,
    caches, a leftover pr-body.md, or the profile the dispatcher individuated —
    carries no unlanded work."""
    lines = _status_entries(wt, check=False)
    if lines is None:
        return (0, 0)
    total = real = 0
    judge = _RenderJudge(wt)
    for line in lines:
        total += 1
        real += len(_unexplained_entries(wt, line, judge))
    return (total, real)


def _dirty_counts_or_none(wt: Path) -> tuple[int, int] | None:
    """Like `_dirty_counts`, but None on a `git status` failure instead of silently
    reading it as `(0, 0)` — clean.

    For a delete gate re-reading a worktree right after
    `import_leftover_private_objects` found a leftover private store on it
    (`cleanup_candidates`, `recheck_candidate`): the snapshot `_dirty_counts` took
    before the import may have failed outright on an unresolvable HEAD and defaulted
    to "clean" the same way `_ahead_behind` is documented to, and that is exactly
    the case where defaulting to clean again on a SECOND failure would still discard
    real work. `_dirty_counts` itself stays best-effort for the status column
    (`list_worktrees`), which every other caller reads with no import ahead of it."""
    lines = _status_entries(wt, check=False)
    if lines is None:
        return None
    total = real = 0
    judge = _RenderJudge(wt)
    for line in lines:
        total += 1
        real += len(_unexplained_entries(wt, line, judge))
    return (total, real)


def _ahead_behind(wt: Path, base_branch: str) -> tuple[int, int]:
    """Commits ahead/behind the local base branch. Best-effort — returns (0,0) on error."""
    cp = _git(
        wt, "rev-list", "--left-right", "--count", f"{base_branch}...HEAD",
        cwd=wt, check=False,
    )
    if cp.returncode != 0:
        return (0, 0)
    parts = cp.stdout.strip().split()
    if len(parts) != 2:
        return (0, 0)
    try:
        behind, ahead = int(parts[0]), int(parts[1])
    except ValueError:
        return (0, 0)
    return (ahead, behind)


def list_worktrees(primary: Path, *, base_branch: str = DEFAULT_BASE_BRANCH,
                   only: Path | None = None) -> list[Worktree]:
    """All worktrees (primary + extras) attached to this primary repo.

    `only` limits the answer to that one path and skips the git status of every
    other worktree, so a re-check of one candidate does not cost a scan of the
    whole repo."""
    cp = _git(primary, "worktree", "list", "--porcelain", check=False)
    if cp.returncode != 0:
        return []
    records = _parse_worktree_porcelain(cp.stdout)
    out: list[Worktree] = []
    for rec in records:
        wt_path = Path(rec.get("worktree", "")).resolve()
        if not wt_path.exists():
            continue
        if only is not None and wt_path != Path(only).resolve():
            continue
        branch = _branch_name(rec)
        ahead, behind = _ahead_behind(wt_path, base_branch)
        dirty, real_dirty = _dirty_counts(wt_path)
        out.append(Worktree(
            path=wt_path,
            primary=primary,
            label=_label_for(wt_path, primary),
            branch=branch,
            is_primary=(wt_path == primary.resolve()),
            locked=("locked" in rec),
            dirty_files=dirty,
            real_dirty_files=real_dirty,
            ahead=ahead,
            behind=behind,
            base_branch=base_branch,
        ))
    return out


def all_worktrees(*, base_branch: str = DEFAULT_BASE_BRANCH,
                  include_excluded: bool = False) -> list[Worktree]:
    """Every worktree across every discovered primary repo, in display order."""
    out: list[Worktree] = []
    for primary in discover_primaries(include_excluded=include_excluded):
        out.extend(list_worktrees(primary, base_branch=base_branch))
    return out


def status_summary(wt: Worktree) -> str:
    """Human-readable state summary: 'clean' / 'N files changed' / 'M commits ahead'."""
    parts: list[str] = []
    if wt.real_dirty_files:
        parts.append(f"{wt.real_dirty_files} file{'s' if wt.real_dirty_files != 1 else ''} changed")
    debris = wt.dirty_files - wt.real_dirty_files
    if debris:
        parts.append(f"{debris} untracked debris")
    if wt.ahead:
        parts.append(f"{wt.ahead} commit{'s' if wt.ahead != 1 else ''} ahead of {wt.base_branch}")
    if wt.behind:
        parts.append(f"{wt.behind} behind {wt.base_branch}")
    return ", ".join(parts) if parts else "clean"


# --- registry overlay --------------------------------------------------------


def in_use_map() -> dict[Path, dict]:
    """Map of worktree absolute path → registry entry, for worktrees with an active agent."""
    out: dict[Path, dict] = {}
    for entry in R.list_runs():
        wt = entry.get("worktree")
        if not wt:
            continue
        path = Path(wt).expanduser().resolve()
        out[path] = entry
    return out


# --- allocation --------------------------------------------------------------


def _primary_for(repo: str) -> Path:
    """Resolve a repo name to its primary path. Raises if unknown.

    Checked before the plain `CONVIVY_ROOT / repo` join: a registry unit whose
    home is nested below CONVIVY_ROOT (`_registry_primary_for`) answers first,
    so a crew registered one level deeper than the depth-1 layout still
    resolves. The join is kept as the fallback for everything that layout
    already covers."""
    nested = _registry_primary_for(repo)
    if nested is not None:
        return nested
    candidate = CONVIVY_ROOT / repo
    if not (candidate / ".git").is_dir():
        known = [p.name for p in discover_primaries()]
        raise ValueError(f"unknown repo: {repo!r}; known primaries: {known}")
    return candidate


# A repo may arrive as a bare name (`knowledge`) or a full slug
# (`convivy/knowledge`); both must answer the same question. The WHOLE string is
# validated, not just the segment we end up using: basenaming first would let
# `convivy/../etc` answer for `etc`, which is neither the caller's repo nor a slug
# GitHub would accept. Nothing unmatched is ever joined onto CONVIVY_ROOT.
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")


def _is_safe_segment(seg: str) -> bool:
    return bool(_SAFE_SEGMENT.match(seg)) and seg not in {".", ".."}


def is_dispatch_target(repo: str) -> bool:
    """True when `repo` can be dispatched against, i.e. `--on <repo>` resolves.

    This is `_primary_for`'s test asked as a question instead of raised as an
    exception, so a caller can branch on dispatchability without catching
    ValueError. It deliberately does NOT filter by DISCOVERY_EXCLUDES: that set
    scopes listing and reaping, `_primary_for` ignores it, and so agent-platform
    is a valid `--on` target despite not being a discovered primary. Answering
    from DISCOVERY_EXCLUDES here would contradict what dispatch actually does.

    Accepts `name` or `owner/name`; any other shape is not a repo we can resolve.
    """
    parts = (repo or "").strip().split("/")
    if len(parts) > 2 or not all(_is_safe_segment(p) for p in parts):
        return False
    name = parts[-1]
    if (CONVIVY_ROOT / name / ".git").is_dir():
        return True
    return _registry_primary_for(name) is not None


def _extras_dir_for(_primary: Path) -> Path:
    """Where extra worktrees live. All primaries are under <root>/, so
    extras share a single collection dir at <root>/worktrees/."""
    return CONVIVY_ROOT / EXTRA_WT_DIRNAME


def desired_path(repo: str, purpose: str | None) -> Path:
    """Resolve (repo, purpose) → absolute worktree path. Doesn't check existence."""
    primary = _primary_for(repo)
    if purpose is None:
        return primary
    extras = _extras_dir_for(primary)
    return extras / f"{repo}.{purpose}"


def branch_for_purpose(purpose: str) -> str:
    """The branch a purpose-scoped worktree checks out — public because
    `agentctl` needs it too, to name the branch a fresh dispatch WOULD land on
    before allocating anything (the open-PR reuse refusal, issue
    01M3P0VPT1T33K4Y5HRFKH90JE)."""
    if purpose == "overnight":
        return "overnight-work"
    return f"agent/{purpose}"


class WorktreeVenvConflict(RuntimeError):
    """A worktree's `.venv` is a real directory or file, not the symlink
    `_link_primary_venv` makes to the primary checkout's `.venv`. Raised before
    the symlink step runs anything under that path, so a worktree carrying its
    own `.venv` is refused rather than kept as-is."""


def _link_primary_venv(primary: Path, target: Path) -> None:
    """Symlink the primary's .venv into a worktree so workers resolve the repo's
    Python and pytest instead of Homebrew's bare /opt/homebrew/bin/pytest.

    Only creates the symlink when the primary has a .venv directory. Skips if the
    worktree already holds a correct symlink. Raises WorktreeVenvConflict if the
    worktree holds a real .venv directory or file instead — the worktree's .venv
    must always be the symlink this function creates. Called after every worktree
    creation (git worktree add, reset --hard + clean -fdx) so a reused worktree
    re-linked after the clean removes the prior symlink.

    Seven repos keep .venv in their primary checkout but not in worktrees
    (agentctl cuts a worktree from git, which shares the object store but not the
    venv dir). Without this link every dispatched worker resolves bare pytest to
    /opt/homebrew/bin/pytest under Python 3.14 with older globally-installed deps,
    making collection failures look like the repo's.

    NOTE: all worktrees of a repo share ONE writable .venv (the primary
    checkout's). A worker that runs pip install (directly or via the Bash
    tool) writes into this shared venv, mutating every concurrent worker's
    environment and the operator's interactive Python for that repo. This is an
    accepted trade-off for closing the interpreter-skew bug (ARCH-027); a
    pip guard was considered but deliberately left out of scope."""
    primary_venv = primary / ".venv"
    target_venv = target / ".venv"
    if not primary_venv.is_dir():
        return  # primary has no venv; nothing to link
    # Already correctly linked — no-op for repeated allocation
    if target_venv.is_symlink() and target_venv.resolve() == primary_venv.resolve():
        return
    # Remove a stale/broken symlink (e.g. left by a prior version that had none,
    # or cleaned by git clean -fdx on reuse)
    if target_venv.is_symlink():
        target_venv.unlink()
    elif target_venv.exists():
        raise WorktreeVenvConflict(
            f"{target_venv}: refusing to use this worktree — its .venv must be "
            "the symlink agentctl creates to the primary checkout's .venv, not "
            "a real directory or file.")
    target_venv.symlink_to(str(primary_venv), target_is_directory=True)


class WorktreePinError(RuntimeError):
    """A worktree could not be placed at — or asserted to be at — a pinned commit.
    Raised by the review-integrity path (ARCH-184) so a dispatch fails loudly
    rather than letting an agent review a tree that isn't the commit it claims."""


def _ensure_commit_present(primary: Path, commit: str, *, fetch_first: bool = False) -> None:
    """Make sure `commit` is in the local object store (all worktrees of a repo
    share it), fetching origin once if it isn't. Raise if it still can't be found
    — pinning to a commit git can't see is a hard error, not a silent fallback.

    `fetch_first` (platform/specs/reviewers-run-under-their-allow-list) fetches
    origin UNCONDITIONALLY, even when `commit` is already present: a review-class
    dispatch's allow-list no longer grants `git fetch` at all (a flag-execution
    hole — `--upload-pack=<cmd>` runs a program), so this pin is the reviewer's
    only chance to see a fresh `origin/main` before its `git merge-base
    origin/main HEAD`-shaped reads run. Without it, `origin/main` is only as
    fresh as whenever THIS primary checkout last fetched for an unrelated reason
    — stale by default, not by design. `False` (every caller before this
    parameter existed, and every non-review caller today) keeps the cheap
    skip-when-present behavior."""
    if not fetch_first and _git(primary, "cat-file", "-e",
                                f"{commit}^{{commit}}", check=False).returncode == 0:
        return
    _git(primary, "fetch", "origin", check=False)
    if _git(primary, "cat-file", "-e", f"{commit}^{{commit}}", check=False).returncode != 0:
        raise WorktreePinError(f"commit {commit!r} not found in {primary} even after fetch origin")


def _assert_head(target: Path, commit: str) -> None:
    """Assert the worktree's HEAD resolves to `commit`. The tripwire that turns a
    stale checkout into an immediate, loud failure (ARCH-184)."""
    head = _git(target, "rev-parse", "HEAD", check=False).stdout.strip()
    want = _git(target, "rev-parse", f"{commit}^{{commit}}", check=False).stdout.strip()
    if not head or not want or head != want:
        raise WorktreePinError(
            f"{target}: HEAD={head[:9] or '?'} != pinned {want[:9] or commit[:9]}")


def _pin_existing_to_commit(primary: Path, target: Path, commit: str, *,
                            fetch_first: bool = False) -> None:
    """Force an already-existing (reused) worktree onto `commit`. This is the fix
    for the stale-review near-miss (ARCH-184): a reused review worktree kept its
    round-1 HEAD because allocate_worktree returned it as-is. fetch → reset --hard
    → clean → assert. Runs whenever `allocate_worktree` is given a `commit` and the
    tree already exists: a review re-dispatch, or a repeat dispatch of the same
    agent and task for a detached-main agent (scout, planner, curator, a Write-capable
    commercial-writer). `reset --hard` and `clean -fdx` discard whatever an earlier
    run left in that tree, tracked edits and untracked files alike.

    Folds in any leftover private object store first (`import_leftover_private_objects`):
    a prior run on this exact tree may have died before its own end-of-run import, and
    `reset --hard` moving HEAD off that run's commit would otherwise leave it reachable
    from nothing at all, not even the ref this function is about to overwrite.

    `fetch_first` is threaded straight to `_ensure_commit_present`; see its own
    docstring."""
    import_leftover_private_objects(target)
    _ensure_commit_present(primary, commit, fetch_first=fetch_first)
    _git(target, "reset", "--hard", commit)
    _git(target, "clean", "-fdx")
    _assert_head(target, commit)


def _pr_review_ref(number: int) -> str:
    """Where a PR's head is fetched to while `agentctl spawn --pr --review-sha`
    confirms a hand-carried commit belongs to it. Outside refs/heads and refs/tags,
    same convention as `kb_merge.pr_ref`/`docs_merge.pr_ref`, so nothing that lists
    branches or tags sees it. Not dropped afterwards — the next check against the
    same PR refetches and overwrites it, same as a re-dispatch's commit pin does."""
    return f"refs/agentctl-spawn/pr-{number}"


def commit_in_pr(repo: str, number: int, base_branch: str, commit: str, *,
                 merge_commit: str | None = None) -> "str | None":
    """Whether `commit` is one of pull request `number`'s own commits, or why not.

    Fetches the PR's head from `refs/pull/<number>/head` — the ref GitHub keeps for
    the life of the PR even once its branch is deleted, which a MERGED PR's usually
    is — rather than `origin/<headRefName>`, which a caller cannot rely on once that
    happens. `commit` must be reachable from that head AND NOT reachable from the
    base side of the PR: for an OPEN PR (`merge_commit` is None) that is
    `base_branch`'s own current tip, fetched fresh; for a MERGED PR it is
    `merge_commit`'s first parent — the base's tip the instant before the PR's own
    commit(s) joined it, which is GitHub's `mergeCommit.oid` for both a true merge
    commit and a squash merge.

    `base_branch`'s CURRENT tip is the wrong cut point once a PR has merged with a
    true merge commit: the PR's own head is then an ancestor of that tip, so
    `merge-base(head, tip)` degenerates to the head itself and every one of the
    PR's own commits — its head included — would read as "already on base"
    (agent-platform#1004, reproduced against this repo's own merge of PR #1000).

    Returns None when `commit` is confirmed in the PR. Both fetches run
    unconditionally, ambient-credentialed off `repo`'s own origin remote, same as
    `_ensure_commit_present`'s `fetch_first` path; their stderr, and a `git
    merge-base` failure's, is reported with any URL redacted (`_redact_urls`), the
    same treatment `_fresh_base_sha` gives its own `fetch origin` stderr — a
    fetch's stderr routinely echoes the remote URL back, which can itself carry
    embedded credentials. A `git merge-base --is-ancestor` exit code other than 0
    or 1 is a git failure, not a "not an ancestor" result, and is reported as its
    own distinct refusal rather than read as either outcome."""
    primary = _primary_for(repo)
    head_ref = _pr_review_ref(number)
    fetch_head = _git(primary, "fetch", "--quiet", "origin",
                      f"+refs/pull/{number}/head:{head_ref}", check=False)
    if fetch_head.returncode != 0:
        return (f"could not fetch refs/pull/{number}/head: "
                f"{_redact_urls(fetch_head.stderr.strip())}")
    fetch_base = _git(primary, "fetch", "--quiet", "origin",
                      f"+refs/heads/{base_branch}:refs/remotes/origin/{base_branch}",
                      check=False)
    if fetch_base.returncode != 0:
        return (f"could not fetch origin/{base_branch}: "
                f"{_redact_urls(fetch_base.stderr.strip())}")
    reachable = _git(primary, "merge-base", "--is-ancestor", commit, head_ref, check=False)
    if reachable.returncode == 1:
        return f"{commit} is not reachable from PR #{number}'s head"
    if reachable.returncode != 0:
        return (f"could not confirm {commit} is reachable from PR #{number}'s head "
                f"(git exited {reachable.returncode}): "
                f"{_redact_urls(reachable.stderr.strip())}")
    base_point = f"{merge_commit}^1" if merge_commit else f"origin/{base_branch}"
    on_base_already = _git(primary, "merge-base", "--is-ancestor", commit, base_point,
                           check=False)
    if on_base_already.returncode == 0:
        return f"{commit} is on {base_branch} before PR #{number} began, not one of its own commits"
    if on_base_already.returncode != 1:
        return (f"could not confirm {commit} is not already on {base_branch} "
                f"(git exited {on_base_already.returncode}): "
                f"{_redact_urls(on_base_already.stderr.strip())}")
    return None


def _redact_urls(text: str) -> str:
    """Strip anything that looks like a URL out of git error text. A fetch's
    stderr routinely echoes the remote URL back (`fatal: unable to access
    '<url>'`), and that URL can itself carry embedded credentials
    (`https://TOKEN@host/...`), so an error message built from raw stderr must
    not pass it through."""
    return re.sub(r"\S+://\S+", "<url omitted>", text)


def _fresh_base_sha(primary: Path, base_branch: str) -> str:
    """Resolve `base_branch` to an exact SHA for a BRAND-NEW branch: fetches `origin`
    first, then reads `refs/remotes/origin/<base_branch>` when `primary` has an
    `origin` remote, else the local `refs/heads/<base_branch>`.

    Without this, `git worktree add -b <branch> <target> main` resolves `main` to
    whatever the primary's own `refs/heads/main` happens to point at — which drifts
    behind `origin/main` whenever nothing has run `git pull` in that checkout for a
    while, and a dispatched coder was then cut from that stale base (CQ-245: two
    product primaries, both days to weeks behind origin, each sent a
    fresh coder dispatch off the root commit or an old tip instead of the real
    `origin/main`).

    `show-ref --verify` takes the full ref and does no search, unlike `rev-parse
    origin/main`, which would read a TAG of that name ahead of the remote-tracking
    branch (agent-platform#681 and #682 — the same reasoning `_detached_main_base`
    in bin/agentctl applies for a detached-main dispatch). Raises ValueError rather
    than falling back to local main — whether the resolved ref can't be found after
    the fetch, or the fetch itself fails (network down, credential refused, a
    GitHub outage): either way, a coder placed on some other commit, or on
    whatever `origin/<base_branch>` happened to be cached from a previous fetch,
    would build on a tree nobody chose. The fetch's stderr is reported with any
    URL redacted, since the remote URL can carry credentials. Only reads and
    fetches `primary`; never touches its checked-out branch or its local main."""
    remote = _git(primary, "remote", "get-url", "origin", check=False)
    has_origin = remote.returncode == 0
    if has_origin:
        fetch = _git(primary, "fetch", "origin", check=False)
        if fetch.returncode != 0:
            raise ValueError(
                f"{primary}: git fetch origin failed (exit {fetch.returncode}): "
                f"{_redact_urls(fetch.stderr.strip())}")
        ref = f"refs/remotes/origin/{base_branch}"
    else:
        ref = f"refs/heads/{base_branch}"
    sha = git_refs.exact_ref_sha(primary, ref)
    if sha is None:
        if has_origin:
            raise ValueError(
                f"{ref} does not exist in {primary} even after fetching origin, so "
                f"there is no remote-tracking {base_branch} to cut a new branch from.")
        raise ValueError(
            f"{primary} has no `origin` remote and no {ref}, so there is no "
            f"{base_branch} to cut a new branch from.")
    return sha


def _git_worktree_add(primary: Path, target: Path, branch: str, base_branch: str,
                      commit: str | None = None, *, fetch_first: bool = False) -> None:
    """Create a worktree at `target`. When `commit` is given, check out a detached
    HEAD pinned to that exact commit (review-class — no branch to drift, ARCH-184);
    otherwise create/reuse `branch` from base_branch. `fetch_first` is threaded
    straight to `_ensure_commit_present`; see its own docstring.

    Cutting a brand-new branch resolves `base_branch` through `_fresh_base_sha`
    rather than passing the branch name straight to `git worktree add`, which
    would resolve it against the primary's own (possibly stale) local ref."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if commit is not None:
        _ensure_commit_present(primary, commit, fetch_first=fetch_first)
        _git(primary, "worktree", "add", "--detach", str(target), commit)
        return
    # Does the branch already exist locally?
    cp = _git(primary, "rev-parse", "--verify", f"refs/heads/{branch}", check=False)
    if cp.returncode == 0:
        _git(primary, "worktree", "add", str(target), branch)
    else:
        base_sha = _fresh_base_sha(primary, base_branch)
        _git(primary, "worktree", "add", "-b", branch, str(target), base_sha)


class WorktreeBusy(RuntimeError):
    """The target worktree is currently held by another active agent."""

    def __init__(self, path: Path, holder: dict):
        super().__init__(f"worktree {path} is held by {holder.get('run_id')} ({holder.get('agent')})")
        self.path = path
        self.holder = holder


@dataclass(frozen=True)
class SwiftResolveResult:
    """What the swift-resolve dispatch step did. `ran=True` means
    `resolve_swift_package` actually invoked `swift package resolve`.
    `ran=False` covers two different cases, told apart by `skipped_reason`:
    the worktree held no `Package.swift` at all (the common case for a
    non-Swift repo — `skipped_reason` stays `None`, nothing is recorded), or
    it held one but `swift_worktree_matches_origin` or the caller's
    review-class check refused to run it (`skipped_reason` names why). Either
    way `returncode`, `duration_s` and `stderr` stay at their defaults."""
    ran: bool
    returncode: int | None = None
    duration_s: float | None = None
    stderr: str = ""
    skipped_reason: str | None = None


# PATH to find the toolchain; HOME and TMPDIR for its own caches, config and
# scratch files; LANG so its output decodes. Nothing else from the operator's
# own environment reaches a manifest process — `Package.swift` is Swift code
# that `swift package resolve` evaluates and whose named dependencies it then
# fetches, unsandboxed, with the operator's own privileges (see
# `resolve_swift_package`'s docstring), so an ambient credential (an
# `SSH_AUTH_SOCK`, a `GH_TOKEN`) must never be reachable to it
# (issue_01M5CSFQ6XGEB7JVA0ATF3EN32 round 1, security review).
_SWIFT_RESOLVE_ENV_ALLOW = ("PATH", "HOME", "TMPDIR", "LANG")


def _swift_resolve_env() -> dict[str, str]:
    """`os.environ`, cut down to `_SWIFT_RESOLVE_ENV_ALLOW`."""
    return {k: v for k, v in os.environ.items() if k in _SWIFT_RESOLVE_ENV_ALLOW}


# The one path `allocate_worktree` itself writes into a worktree between
# creation and the swift-resolve gate: `_link_primary_venv`'s `.venv`
# symlink, made for every worktree whose primary carries one — and `.venv` is
# routinely gitignored, so it can be invisible to a plain `git status`
# without `--ignored` too. Named here explicitly so a status line against
# anything else still refuses the resolve, and so a future write
# `allocate_worktree` makes before the gate runs has to be added to this set
# to pass, rather than silently widening what a reused or freshly-written
# worktree can carry through undetected (round-3 security review,
# issue_01M5CSFQ6XGEB7JVA0ATF3EN32).
_SWIFT_RESOLVE_SPAWN_WRITES = frozenset({".venv"})


def _parse_status_porcelain_paths(output: str) -> list[str]:
    """`git status --porcelain` (v1, non -z) lines → the path each one names.
    Each line is a 2-character status code, a space, then the path."""
    return [line[3:] for line in output.splitlines() if line]


def _worktree_has_hidden_index_entries(worktree: Path) -> bool:
    """Whether any index entry carries skip-worktree (an uppercase `S` in
    `git ls-files -v`) or assume-unchanged (a lowercase tag) — either hides a
    tracked file's real on-disk content from `git status` entirely, and a
    sandboxed run inside a reused worktree is not shown to be denied from
    setting either flag with its own `git update-index` (round-3 security
    review, issue_01M5CSFQ6XGEB7JVA0ATF3EN32). Fails closed: a `git ls-files`
    error means this can't be answered, so it's treated as hidden rather than
    as clean."""
    cp = _git(worktree, "ls-files", "-v", check=False)
    if cp.returncode != 0:
        return True
    for line in cp.stdout.splitlines():
        if not line:
            continue
        tag = line[0]
        if tag == "S" or tag.islower():
            return True
    return False


def swift_worktree_matches_origin(worktree: Path, *,
                                  base_branch: str = DEFAULT_BASE_BRANCH) -> tuple[bool, str | None]:
    """Whether `worktree` is safe for `resolve_swift_package` to run
    against: its HEAD is exactly `origin/<base_branch>`'s commit, its index
    carries no skip-worktree/assume-unchanged entry, and
    `git status --porcelain --untracked-files=all --ignored` reports nothing
    beyond `_SWIFT_RESOLVE_SPAWN_WRITES` — already-merged, already-reviewed
    content, never a sandboxed coder's own unreviewed branch write and never
    a reviewer's tree pinned to a PR head its own author controls.

    This checks the whole tree rather than `Package.swift`/`Package.resolved`
    alone (issue_01M5CSFQ6XGEB7JVA0ATF3EN32 round 2, security review): SwiftPM
    also reads a version-specific manifest (`Package@swift-<version>.swift` and
    friends, which take priority over `Package.swift` for a matching
    toolchain), `.swiftpm/configuration/` (`mirrors.json` redirects any
    dependency URL to an attacker-controlled host; `registries.json` the
    same for the package registry), and any `.package(path:)` local
    dependency `Package.swift` names, which resolves straight to a path
    inside the worktree with no entry in `Package.resolved` at all. A
    two-file comparison misses every one of those; comparing the whole
    worktree to `origin/<base_branch>` closes the class instead of chasing
    each file in turn. A fresh coder worktree cut straight from
    `origin/<base_branch>` matches by construction; a revise round reusing a
    PR branch an earlier coder round committed or left untracked files on
    never does, so it never resolves (round-2 fix: round 1's two-file
    comparison let exactly this case through when the two files it checked
    happened to be untouched).

    `--ignored` on the status call and the index-entry check close a round-3
    gap: plain `--untracked-files=all` omits any path `origin/<base_branch>`'s
    own `.gitignore` already excludes (the stock `.swiftpm/configuration/`
    entry SwiftPM's own `swift package init` template ships), and neither
    flag sees a tracked file hidden behind skip-worktree/assume-unchanged.
    This function alone is not what makes a reused worktree safe — the
    caller (`bin/agentctl`'s `_spawn_dispatch`) must also only call this
    against a worktree it created fresh in this exact dispatch, never a
    reused one, since a reused `commit=None` target is handed back by
    `allocate_worktree` completely unreset (round-3 security review).

    `(True, None)` only when all three hold; otherwise `(False, <reason>)`,
    including when `origin/<base_branch>` cannot be read at all, which fails
    closed rather than treating a missing ref as a match."""
    ref = f"refs/remotes/origin/{base_branch}"
    cp = _git(worktree, "rev-parse", "--verify", ref, check=False)
    if cp.returncode != 0:
        return False, f"origin/{base_branch} not found"
    origin_sha = cp.stdout.strip()
    cp = _git(worktree, "rev-parse", "--verify", "HEAD", check=False)
    if cp.returncode != 0:
        return False, "worktree HEAD could not be read"
    head_sha = cp.stdout.strip()
    if head_sha != origin_sha:
        return False, f"HEAD ({head_sha[:12]}) is not origin/{base_branch} ({origin_sha[:12]})"
    if _worktree_has_hidden_index_entries(worktree):
        return False, "worktree has a skip-worktree or assume-unchanged index entry"
    cp = _git(worktree, "status", "--porcelain", "--untracked-files=all", "--ignored", check=False)
    if cp.returncode != 0:
        return False, "git status failed"
    unexpected = [p for p in _parse_status_porcelain_paths(cp.stdout)
                 if p not in _SWIFT_RESOLVE_SPAWN_WRITES]
    if unexpected:
        return False, f"worktree has local changes relative to origin/{base_branch}"
    return True, None


def resolve_swift_package(worktree: Path) -> SwiftResolveResult:
    """Run `swift package resolve` in `worktree`, with the operator's own
    privileges, before a dispatched run's own sandboxed session ever starts
    (issue_01M5CSFQ6XGEB7JVA0ATF3EN32).

    Claude Code's Bash sandbox denies writes to any `.git/config` or git hooks
    path under the run's working directory, so a session that is itself
    sandboxed cannot let SwiftPM clone its dependencies into
    `.build/checkouts` — the clone's own `git init` needs to write that file.
    Resolving here, outside that sandbox, writes those checkouts once so the
    run never clones or fetches dependency code itself.

    A no-op (`ran=False`) when `worktree` holds no `Package.swift`: most repos
    this dispatcher serves aren't Swift packages. No timeout — no hung resolve
    has been seen, and a guard needs a named occurrence to earn one.

    This function does NOT gate on `swift_worktree_matches_origin` or on the
    dispatch's agent class — the caller (`bin/agentctl`'s `_spawn_dispatch`)
    decides whether to call this at all, and must only ever call it against a
    worktree `swift_worktree_matches_origin` has already passed, for a
    non-review-class dispatch (round-1 security review).

    The resolve runs with `--config-path`/`--security-path`/`--cache-path`
    pointed at a fresh temporary directory made for this one call and removed
    once it returns, rather than SwiftPM's own user-level defaults
    (`~/.swiftpm`, `~/Library/org.swift.swiftpm`) — so a `mirrors.json`,
    registry entry, or cached manifest any earlier resolve could have written
    there is never read back in (round-2 security review,
    issue_01M5CSFQ6XGEB7JVA0ATF3EN32)."""
    if not (worktree / "Package.swift").exists():
        return SwiftResolveResult(ran=False)
    isolation_dir = Path(tempfile.mkdtemp(prefix="swift-resolve-"))
    try:
        config_dir = isolation_dir / "config"
        security_dir = isolation_dir / "security"
        cache_dir = isolation_dir / "cache"
        for d in (config_dir, security_dir, cache_dir):
            d.mkdir()
        started = time.monotonic()
        cp = subprocess.run(
            ["swift", "package",
             "--config-path", str(config_dir),
             "--security-path", str(security_dir),
             "--cache-path", str(cache_dir),
             "resolve"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            env=_swift_resolve_env(),
        )
    finally:
        shutil.rmtree(isolation_dir, ignore_errors=True)
    return SwiftResolveResult(ran=True, returncode=cp.returncode,
                              duration_s=time.monotonic() - started,
                              stderr=cp.stderr)


def allocate_worktree(repo: str, purpose: str | None = None, *,
                      branch: str | None = None,
                      base_branch: str = DEFAULT_BASE_BRANCH,
                      commit: str | None = None,
                      fetch_origin_first: bool = False,
                      report_created: bool = False) -> "Path | tuple[Path, bool]":
    """Resolve + (if needed) create a worktree for the given (repo, purpose).

    Refuses if the target is in the active-runs registry under another agent.
    Returns the absolute worktree path. The caller (agentctl spawn) is
    responsible for registering its own run against this path afterwards.

    `commit` pins the worktree to an exact SHA (ARCH-184, review integrity): on
    create it's a detached checkout at the commit; on reuse it's force-reset to
    the commit (killing the stale-review bug where a reused worktree kept an old
    HEAD). Refused for the primary tree — pinning hard-resets, and the primary is
    the human's clone, never something to clobber.

    `fetch_origin_first` (platform/specs/reviewers-run-under-their-allow-list) is
    the caller's own answer to "is this a review dispatch?" — `True` for
    `reviewer`/`security-reviewer`, `False` for every other commit-pinned caller
    (a detached-main agent's own dispatch). Threaded straight to
    `_ensure_commit_present` via `_pin_existing_to_commit`/`_git_worktree_add`;
    see that function's own docstring for why a review dispatch needs it.

    `report_created=True` returns `(target, created)` instead of plain
    `target`: `created` is `True` only when THIS call made the worktree via
    `git worktree add` (the `target` did not already exist on entry), `False`
    for every reuse, pinned or not. A reused `commit=None` target is handed
    back completely unreset — no `git status` on it proves anything about
    what a prior run left there — so a caller deciding whether a worktree is
    safe for an unsandboxed, privileged step (the swift-resolve gate in
    `bin/agentctl`'s `_spawn_dispatch`) must use this fact explicitly rather
    than infer it from the tree's content or a timestamp (round-3 security
    review, issue_01M5CSFQ6XGEB7JVA0ATF3EN32)."""
    if commit is not None and purpose is None:
        raise ValueError(
            "commit-pinned worktree requires a dedicated purpose — refusing to "
            "hard-reset the primary worktree")
    target = desired_path(repo, purpose).resolve() if desired_path(repo, purpose).exists() else desired_path(repo, purpose)
    # in-use check (path-resolve before lookup so symlinks don't fool us)
    held_by = in_use_map().get(target.resolve() if target.exists() else target)
    if held_by:
        raise WorktreeBusy(target, held_by)

    if target.exists():
        if commit is not None:
            _pin_existing_to_commit(_primary_for(repo), target, commit,
                                    fetch_first=fetch_origin_first)
        _link_primary_venv(_primary_for(repo), target)
        return (target, False) if report_created else target

    if purpose is None:
        # primary must already exist
        raise FileNotFoundError(f"primary worktree missing: {target}")

    primary = _primary_for(repo)
    use_branch = branch or branch_for_purpose(purpose)
    _git_worktree_add(primary, target, use_branch, base_branch, commit=commit,
                      fetch_first=fetch_origin_first)
    if commit is not None:
        _assert_head(target, commit)
    _link_primary_venv(primary, target)
    return (target, True) if report_created else target


# --- PR-branch reuse (ARCH-347) ---------------------------------------------
#
# Why this exists, separately from allocate_worktree: a worktree's branch is
# `agent/<purpose>`, and `purpose` is slugged from the dispatch TASK TEXT. A round-fix
# carries a different task than the first dispatch, so it gets a different slug → a
# different branch → a branch that is NOT the PR's. `publish` then pushes that orphan
# branch and reports success while the PR head never moves (a product-crew footgun, #923).
# The fix is to stop deriving the branch from the task and instead reuse the branch the
# PR actually points at — which is what these two functions do.


def _sanitize_branch(branch: str) -> str:
    """Branch name → filesystem-safe worktree-path suffix (`agent/foo` → `agent-foo`)."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", branch).strip("-")


def find_worktree_on_branch(repo: str, branch: str) -> Path | None:
    """The worktree currently checked out on `branch`, or None if none is.

    git forbids the same branch in two worktrees, so a PR's branch lives in at most one
    — the tree a round-fix must reuse rather than fork away from. None is a NORMAL
    answer, not an error: the original tree may have been reaped between rounds (verified
    live — some open PRs keep their tree, some don't), in which case the caller creates a
    fresh one on the branch."""
    primary = _primary_for(repo)
    cp = _git(primary, "worktree", "list", "--porcelain", check=False)
    if cp.returncode != 0:
        return None
    for rec in _parse_worktree_porcelain(cp.stdout):
        if _branch_name(rec) == branch:
            wt = rec.get("worktree")
            if wt:
                return Path(wt)
    return None


def allocate_on_branch(repo: str, branch: str) -> Path:
    """Resolve a worktree checked out on an EXISTING branch — the round-fix path.

    No `base_branch`: this function always aligns to `origin/<branch>` (the PR tip), so
    there is no base to branch from — unlike `allocate_worktree`, which cuts a NEW branch
    off a base.

    Guarantees the returned worktree is on `branch` at `origin/<branch>` (the live PR
    tip), so a coder's fix lands on the PR branch and `publish` pushes the PR itself.
    Reuse hard-resets to the origin tip — the same integrity move `_pin_existing_to_commit`
    makes for reviews, so a reused tree can never carry a prior round's HEAD.

    REFUSES rather than clobbers in two cases: a tree held by another live run
    (`WorktreeBusy`), and a tree carrying real unlanded work (`WorktreePinError`) — a
    round-fix between rounds is clean, so uncommitted work means something we must not
    reset away is present. Fail loud beats losing an agent's or a human's work."""
    primary = _primary_for(repo)
    # Fetch first so origin/<branch> is the live PR tip we reuse or create against.
    _git(primary, "fetch", "origin", branch, check=False)
    # The tip is the sha the exact remote-tracking ref holds (git_refs), and every read
    # below acts on it. `origin/<branch>` as a name falls through to a same-named tag
    # or branch, and rev-parse on the qualified name does too once the real ref is gone.
    tip = git_refs.exact_ref_sha(primary, f"refs/remotes/origin/{branch}")
    if tip is None:
        raise WorktreePinError(
            f"origin/{branch!r} not found after fetch — cannot place a worktree on a "
            f"branch origin does not have (is the PR branch really {branch!r}?)")

    existing = find_worktree_on_branch(repo, branch)
    if existing is not None:
        held_by = in_use_map().get(existing.resolve() if existing.exists() else existing)
        if held_by:
            raise WorktreeBusy(existing, held_by)
        # Fold in any leftover private object store BEFORE `unlanded_entries` and the
        # ahead-check below both read this tree's HEAD: a prior run on this exact tree
        # may have died before its own end-of-run import (a documented recurring
        # incident, P1b 2026-07-04), leaving its commit reachable only through the
        # private store — `git status`, `rev-list`, and every other plain git read
        # against the shared store fail outright on a HEAD it cannot resolve.
        # Importing first makes HEAD resolvable, so both checks run instead of one
        # crashing (`unlanded_entries`'s own `git status`, `check=True`) and the other
        # being asked a question it cannot answer.
        import_leftover_private_objects(existing)
        work = unlanded_entries(existing)
        if work:
            raise WorktreePinError(
                f"{existing}: refusing to reuse for a round-fix — "
                f"{len(work)} uncommitted entr{'y' if len(work) == 1 else 'ies'} would be "
                f"lost to the reset (e.g. {work[0]!r}). Land or park that work first.")
        # unlanded_entries only sees the working tree (`git status`). It is BLIND to
        # committed-but-unpublished work: a coder that committed then crashed before
        # `publish` leaves a clean tree whose HEAD is ahead of origin. `reset --hard` would
        # destroy those commits with no recovery path. Guard it the same way the create
        # path does — fail loud, per this function's own contract.
        #
        # `check=False` and a failed `rev-list` must NOT read as "0 ahead": a git error
        # (the one the import above guards against for this tree, or any other) means
        # this question is unanswerable, not that nothing would be lost. An empty stdout
        # from a failed rev-list previously left `ahead` falsy, which would have let
        # `reset --hard` discard a dead run's commit with no error printed anywhere —
        # confirmed in isolation and, for the CREATE path below (no worktree, only a
        # lingering local ref), confirmed end-to-end: see the round-7 tests.
        cp = _git(existing, "rev-list", "--count", f"{tip}..HEAD", cwd=existing, check=False)
        if cp.returncode != 0:
            raise WorktreePinError(
                f"{existing}: could not determine whether {branch!r} has commits ahead of "
                f"origin/{branch} ({cp.stderr.strip() or 'rev-list failed'}) — refusing to "
                f"reset without knowing.")
        ahead = cp.stdout.strip()
        if ahead and ahead != "0":
            raise WorktreePinError(
                f"{existing}: {ahead} commit(s) on {branch!r} are not on origin/{branch} "
                f"(a coder that committed but never published). Refusing to reset them "
                f"away — publish or discard them first.")
        _git(existing, "reset", "--hard", tip, cwd=existing)
        _git(existing, "clean", "-fdx", cwd=existing)
        _link_primary_venv(primary, existing)
        return existing

    # No tree holds the branch — create one on it, keyed by the branch (not a task slug).
    target = _extras_dir_for(primary) / f"{repo}.{_sanitize_branch(branch)}"
    held_by = in_use_map().get(target.resolve() if target.exists() else target)
    if held_by:
        raise WorktreeBusy(target, held_by)
    if target.exists():
        raise WorktreePinError(
            f"{target}: path exists but holds no worktree on {branch!r}; resolve by hand "
            f"rather than let an auto-reuse guess at a stale directory.")

    has_local = _git(primary, "rev-parse", "--verify", f"refs/heads/{branch}",
                     check=False).returncode == 0
    if has_local:
        # A lingering local ref from the original coder (its worktree was removed, the
        # ref survived). It normally equals origin (publish verified origin==HEAD). If it
        # is AHEAD, that is unpublished work — refuse rather than -B past it.
        #
        # Same fail-closed requirement as the `existing` branch above: a failed rev-list
        # (an error, not a `0`) must refuse rather than read as nothing-to-lose.
        cp = _git(primary, "rev-list", "--count", f"{tip}..refs/heads/{branch}", check=False)
        if cp.returncode != 0:
            raise WorktreePinError(
                f"local {branch!r}: could not determine whether it has commits ahead of "
                f"origin/{branch} ({cp.stderr.strip() or 'rev-list failed'}) — refusing to "
                f"reset without knowing.")
        ahead = cp.stdout.strip()
        if ahead and ahead != "0":
            raise WorktreePinError(
                f"local {branch!r} is {ahead} commit(s) ahead of origin/{branch} — "
                f"refusing to reset a branch with unpublished commits onto the PR tip.")

    target.parent.mkdir(parents=True, exist_ok=True)
    # `-B` points the branch at the live origin tip and checks it out in one step;
    # the ahead-guard above is what makes the implicit reset safe. `--track` needs a
    # branch name as the start point and the start point here is a sha, so the
    # upstream is set afterwards by its full ref name.
    _git(primary, "worktree", "add", "--no-track", "-B", branch, str(target), tip)
    _git(target, "branch", f"--set-upstream-to=refs/remotes/origin/{branch}",
         cwd=target)
    _link_primary_venv(primary, target)
    return target


# --- cleanup ----------------------------------------------------------------


@dataclass
class CleanupCandidate:
    worktree: Worktree
    reason: str  # "removable[:published|:pr-merged]" / "skip:{dirty,ahead,in_use,locked,error,vanished,not-dispatched}"


def _gh_path() -> str | None:
    """Absolute path to gh, launchd-safe (thin PATH — ARCH-086).

    `safe_which` never reads PATH: it checks a fixed list of system
    directories, `/usr/local/bin` (the Intel Homebrew location) among them,
    so there is no separate PATH-based fallback to try after it — and no
    planted `gh` under the running account's home or a worktree for it to
    ever choose, because neither is on that fixed list
    (platform/decisions/0090's Handoff).
    """
    return SP.safe_which("gh")


# A GitHub remote URL in the HTTPS, `ssh://git@` or `git@host:` form, capturing
# its `owner/repo` — `pr_merged` takes the slug straight off the worktree's own
# `origin` rather than through the registry (company/decisions/0027: a
# worktree's primary need not be a registered project either).
_GITHUB_REMOTE_RE = re.compile(
    r"^(?:https://|ssh://git@|git@)github\.com[:/](?P<slug>[^/]+/[^/]+?)(?:\.git)?/?$",
    re.IGNORECASE)


def _origin_github_slug(path: Path) -> str | None:
    """`owner/repo` for `path`'s own `origin` remote, or None when there is
    no such remote or it isn't a GitHub HTTPS/SSH form this can parse."""
    r = subprocess.run(["git", "-C", str(path), "remote", "get-url", "origin"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None
    m = _GITHUB_REMOTE_RE.match(r.stdout.strip())
    return m.group("slug") if m else None


def pr_merged(wt: Worktree, gh_env_for) -> bool:
    """True iff a MERGED PR exists for this worktree's branch.

    Squash merges leave the branch permanently "ahead" of main, so ahead-ness
    alone can't distinguish landed work from live work — the PR state can.
    Any failure (no gh, no remote, offline, no GitHub token mintable for this
    worktree's repo — company/decisions/0027) returns False, i.e. don't reap.

    `gh_env_for` is a `GA.read_token_pool`'s own `env_for`: several worktrees
    commonly share one primary repo (two coders' worktrees off the same
    checkout), so `cleanup_candidates`'s loop mints at most once per repo
    across the whole pass, not once per worktree."""
    gh = _gh_path()
    if not gh or not wt.branch:
        return False
    repo_slug = _origin_github_slug(wt.path)
    if repo_slug is None:
        return False
    try:
        env = gh_env_for(repo_slug)
    except GA.Unreadable:
        return False
    cp = subprocess.run(
        [gh, "pr", "list", "--head", wt.branch, "--state", "merged",
         "--json", "number", "--limit", "1"],
        cwd=str(wt.path), capture_output=True, text=True, env=env,
    )
    if cp.returncode != 0:
        return False
    try:
        return bool(json.loads(cp.stdout))
    except ValueError:
        return False


def is_dispatched(wt_path: Path) -> bool:
    """True iff a render left its manifest in this worktree.

    The manifest, `.claude/.materialized`, marks that `knowledge-ctx use` rendered
    into the tree. Every `agentctl spawn` does; a tree made with
    `git worktree add` has one only if someone rendered into it (`bin/co` tells a
    person to). A path or a name says nothing about who made a tree: the co's own
    instructions prescribe `<root>/worktrees/agent-platform.<purpose>`, the same
    shape the dispatcher uses.

    A hand-made tree that was rendered into reads as dispatched, so when it is
    clean and published, or holds only render output, the cleanup can remove it.
    Every scanned tree had that exposure before the manifest check; uncommitted
    work and unpublished commits still keep it.

    A manifest that is missing, a symlink or unreadable reads as not dispatched,
    the same answer `_read_render_record` gives everywhere else."""
    return _read_render_record(wt_path) is not None


def cleanup_candidates(*, base_branch: str = DEFAULT_BASE_BRANCH) -> list[CleanupCandidate]:
    """Classify every non-primary worktree as removable or skipped, with a reason.

    Covers every DISCOVERY_EXCLUDES repo too (`include_excluded`): the platform's
    own dispatched coder worktrees are worktrees like any other. A tree is a
    candidate only when the dispatcher left its render manifest in it
    (`is_dispatched`), in every repo the job scans. Anything else was made by hand
    (`agent-platform-wt-*`, `.claude/worktrees/*`, `worktrees/agent-platform.<purpose>`)
    or predates the manifest, has no registry entry to show it is in use, and is
    kept as `skip:not-dispatched`.

    `wt.real_dirty_files`/`wt.ahead` came from `all_worktrees`'s own scan, which ran
    BEFORE this loop could import anything — so for a worktree whose HEAD a dead
    run's own private store still holds exclusively (never imported — the same
    P1b 2026-07-04 class `allocate_on_branch`'s reuse path already guards), that
    scan's `git status`/`rev-list` failed outright and the best-effort readers
    (`_dirty_counts`, `_ahead_behind`) folded the failure into "(0, 0), nothing to
    lose" — exactly what a delete gate must never read a git error as.
    `import_leftover_private_objects` makes HEAD resolvable again; a worktree whose
    import found a store gets a FRESH, strict dirty re-read rather than trusting the
    stale snapshot, and `unpublished_commits` — itself always fail-closed — is
    called for every candidate this loop would otherwise call removable, not only
    one `wt.ahead` happened to read nonzero before the import could run."""
    holds = in_use_map()
    out: list[CleanupCandidate] = []
    # One `read`-scoped token per repo across this whole pass (company/
    # decisions/0027): several worktrees commonly share one primary repo, so
    # a pool that mints at most once per repo replaces what would otherwise
    # be a mint per worktree.
    with GA.read_token_pool() as gh_env_for:
        for wt in all_worktrees(base_branch=base_branch, include_excluded=True):
            if wt.is_primary:
                continue
            if wt.path.resolve() in holds:
                out.append(CleanupCandidate(wt, "skip:in_use"))
                continue
            if not is_dispatched(wt.path):
                out.append(CleanupCandidate(wt, "skip:not-dispatched"))
                continue
            if wt.locked:
                out.append(CleanupCandidate(wt, "skip:locked"))
                continue
            real_dirty = wt.real_dirty_files
            if import_leftover_private_objects(wt.path):
                dirty = _dirty_counts_or_none(wt.path)
                if dirty is None:
                    out.append(CleanupCandidate(wt, "skip:error"))
                    continue
                real_dirty = dirty[1]
            if real_dirty:
                out.append(CleanupCandidate(wt, "skip:dirty"))
                continue
            try:
                unpub = unpublished_commits(wt.path)
            except Exception:
                out.append(CleanupCandidate(wt, "skip:error"))
                continue
            if unpub == 0:
                out.append(CleanupCandidate(wt, "removable:published" if wt.ahead else "removable"))
            elif wt.branch and pr_merged(wt, gh_env_for):
                out.append(CleanupCandidate(wt, "removable:pr-merged"))
            else:
                out.append(CleanupCandidate(wt, "skip:ahead"))
    return out


def reason_counts(cands: list[CleanupCandidate]) -> list[tuple[str, int]]:
    """(reason, count) for each reason that occurs, most frequent first.

    The cleanup job prints this last, so a run that keeps most of the fleet says so
    instead of exiting 0 over a wall of `skip:` lines."""
    counts: dict[str, int] = {}
    for c in cands:
        counts[c.reason] = counts.get(c.reason, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def unregistered_dirs() -> list[tuple[Path, str]]:
    """Directories under `<root>/worktrees/` that no scanned repo lists as a worktree.

    `cleanup_candidates` only ever sees what `git worktree list` reports, so these
    are invisible to it. None is removable on the evidence git holds, so the job
    reports them and leaves them. Kinds:

      no-git             no `.git` at all: a parked or scratch directory
      metadata-gone      a `.git` file whose gitdir no longer exists: what is left
                         after the primary forgot the worktree; its content is on disk
                         only, and nothing says whether it was ever committed
      other-repo         a worktree of a repo this job does not scan
      standalone-repo    a directory holding its own `.git/`
      unlisted           a valid worktree its primary does not list

    Plain files (`.DS_Store`) are not directories and are ignored.
    """
    extras = CONVIVY_ROOT / EXTRA_WT_DIRNAME
    if not extras.is_dir():
        return []
    scanned: dict[Path, Path] = {}
    known: set[Path] = set()
    for primary in discover_primaries(include_excluded=True):
        scanned[(primary / ".git").resolve()] = primary
        cp = _git(primary, "worktree", "list", "--porcelain", check=False)
        if cp.returncode == 0:
            known.update(Path(r["worktree"]).resolve()
                         for r in _parse_worktree_porcelain(cp.stdout) if r.get("worktree"))
    out: list[tuple[Path, str]] = []
    for child in sorted(extras.iterdir()):
        if not child.is_dir() or child.resolve() in known:
            continue
        dotgit = child / ".git"
        if dotgit.is_dir():
            out.append((child, "standalone-repo"))
        elif not dotgit.exists():
            out.append((child, "no-git"))
        else:
            try:
                gitdir = Path(dotgit.read_text().split("gitdir:", 1)[1].strip())
            except (OSError, IndexError, UnicodeDecodeError):
                out.append((child, "metadata-gone"))
                continue
            if not gitdir.is_dir():
                out.append((child, "metadata-gone"))
                continue
            common = gitdir / "commondir"
            try:
                common_git = (gitdir / common.read_text().strip()).resolve()
            except OSError:
                common_git = gitdir.resolve()
            out.append((child, "unlisted" if common_git in scanned else "other-repo"))
    return out


def remove_worktree(path: Path, *, force: bool = False) -> None:
    """`git worktree remove <path>` from its primary."""
    path = path.resolve()
    # Find the owning primary by walking up to a path with .git/ directory
    # (worktrees have a .git file, not directory).
    cp = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=str(path), capture_output=True, text=True, check=True,
    )
    common_dir = Path(cp.stdout.strip())
    if not common_dir.is_absolute():
        common_dir = (path / common_dir).resolve()
    primary = common_dir.parent  # `<primary>/.git` → `<primary>`
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(path))
    _git(primary, *args)


def recheck_candidate(c: CleanupCandidate) -> tuple[str | None, int]:
    """Re-check all safety-relevant state immediately before removal.

    Returns (skip_reason, 0) if the worktree's state changed since
    cleanup_candidates classified it, or (None, fresh_dirty_files) if it is
    still safe to remove. The caller must use fresh_dirty_files for the
    --force flag rather than the stale snapshot value.

    Checks, in order: in-use map, vanished, locked, real_dirty_files (re-read
    fresh, same as `cleanup_candidates`, if a leftover private store turns up
    here that `cleanup_candidates`'s own import did not already clear), and
    unpublished_commits for EVERY candidate, not only `removable:published` —
    the same widening `cleanup_candidates` makes, so a `removable`/
    `removable:pr-merged` candidate whose `_ahead_behind` happened to read
    "not ahead" from a git error rather than a real zero still gets the
    fail-closed check before confirming removal. A positive count is only a
    problem for a non-`removable:pr-merged` candidate: that reason is reached
    precisely because its commits are NOT reachable from any remote (a squash
    merge rewrites them away), so unpublished work there is expected, not a
    state change. The raises-not-0 contract on unpublished_commits is
    preserved: any exception returns skip:error rather than collapsing to a
    removable verdict."""
    wt = c.worktree
    wt_path = wt.path.resolve()
    if wt_path in in_use_map():
        return "skip:in_use", 0
    refreshed = {w.path.resolve(): w for w in list_worktrees(wt.primary, only=wt_path)}
    current = refreshed.get(wt_path)
    if current is None:
        return "skip:vanished", 0
    if current.locked:
        return "skip:locked", 0
    real_dirty, fresh_dirty = current.real_dirty_files, current.dirty_files
    if import_leftover_private_objects(wt_path):
        dirty = _dirty_counts_or_none(wt_path)
        if dirty is None:
            return "skip:error", 0
        fresh_dirty, real_dirty = dirty
    if real_dirty:
        return "skip:dirty", 0
    try:
        unpub = unpublished_commits(wt_path)
    except Exception:
        return "skip:error", 0
    if unpub > 0 and c.reason != "removable:pr-merged":
        return "skip:ahead", 0
    return None, fresh_dirty


def delete_branch(primary: Path, branch: str, *, force: bool = False) -> bool:
    """Best-effort local branch delete after a worktree removal. Returns True if deleted.

    force=True (-D) is for branches verified merged via PR state — `git branch -d`
    can't see a squash merge. Leaving the branch behind isn't just clutter:
    allocate_worktree reuses an existing branch by name, so a stale one would
    resurrect old work under a recycled purpose."""
    cp = _git(primary, "branch", "-D" if force else "-d", branch, check=False)
    return cp.returncode == 0
