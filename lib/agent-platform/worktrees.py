"""Worktree management for the agent platform.

Discovery + status + allocation for the worktrees we hand out to agents.

Layout (matches 3.5 spec):
  <root>/<repo>/                              # primary
  <root>/worktrees/<repo>.<purpose>/          # additional

`knowledge` is one of the primaries under <root>/ (parallel to constance,
vivy, etc.). The `worktrees/` subdir itself is naturally skipped from primary
discovery because git worktree adds put a `.git` *file* in the extra, not a
`.git/` directory in the holder dir.
"""
from __future__ import annotations

import json
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import builder_root as BR
import git_refs
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
# which that prefix misses. Real case: a swept vivy review tree held a `coverage.json`,
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
# artifacts: PR numbers passed 999 some time ago — convivy/vivy is on #1114 — so
# `review-1114.md` is a name the platform generates today. A four-digit review
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
    label: str                  # short label for tables/registry, e.g. "constance" or "worktrees/constance.overnight"
    branch: str | None          # current branch name (None if detached)
    is_primary: bool            # True if this is the primary worktree (path == primary)
    locked: bool                # `git worktree lock` flag (e.g. Claude Code session worktrees)
    dirty_files: int            # count of changed/untracked files
    real_dirty_files: int       # dirty_files minus untracked debris (venvs, caches, pr-body.md)
    ahead: int                  # commits ahead of upstream / base (best-effort)
    behind: int                 # commits behind upstream / base
    base_branch: str            # branch used for ahead/behind comparison


# --- discovery ---------------------------------------------------------------


def discover_primaries(*, include_excluded: bool = False) -> list[Path]:
    """Depth-1 git repos under <root>/. Sorted alphabetically.

    Skips the `worktrees/` collection dir itself (its children are extras with
    `.git` *files* pointing back to their primary; only primaries have a real
    `.git/` directory). `include_excluded` also returns the DISCOVERY_EXCLUDES
    repos, for the cleanup job."""
    primaries: list[Path] = []
    if CONVIVY_ROOT.is_dir():
        for child in sorted(CONVIVY_ROOT.iterdir()):
            if not child.is_dir() or child.name == EXTRA_WT_DIRNAME:
                continue
            if child.name in DISCOVERY_EXCLUDES and not include_excluded:
                continue
            if (child / ".git").is_dir():
                primaries.append(child)
    return primaries


def _label_for(path: Path, primary: Path) -> str:
    """Short label for tables / registry. `<root>/constance` → `constance`;
    `<root>/worktrees/constance.overnight` → `worktrees/constance.overnight`."""
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
    re-read, rather than re-reading unconditionally on every worktree."""
    git_dir = RS.git_common_dir(wt)
    if git_dir is None:
        return False
    private_dir = RS.run_private_objects_dir(wt, git_dir)
    existed = private_dir.is_dir()
    RS.import_private_objects(git_dir, private_dir)
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
    reaches it only for a repo-local agent (CFO's) or a private crew's, which take
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
    """Resolve a repo name to its primary path. Raises if unknown."""
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
    return (CONVIVY_ROOT / parts[-1] / ".git").is_dir()


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


def _link_primary_venv(primary: Path, target: Path) -> None:
    """Symlink the primary's .venv into a worktree so workers resolve the repo's
    Python and pytest instead of Homebrew's bare /opt/homebrew/bin/pytest.

    Only creates the symlink when the primary has a .venv directory. Skips if the
    worktree already holds a correct symlink. Called after every worktree creation
    (git worktree add, reset --hard + clean -fdx) so a reused worktree re-linked
    after the clean removes the prior symlink.

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
        # A real directory or file that we did not create — do not touch it
        return
    target_venv.symlink_to(str(primary_venv), target_is_directory=True)


class WorktreePinError(RuntimeError):
    """A worktree could not be placed at — or asserted to be at — a pinned commit.
    Raised by the review-integrity path (ARCH-184) so a dispatch fails loudly
    rather than letting an agent review a tree that isn't the commit it claims."""


def _ensure_commit_present(primary: Path, commit: str) -> None:
    """Make sure `commit` is in the local object store (all worktrees of a repo
    share it), fetching origin once if it isn't. Raise if it still can't be found
    — pinning to a commit git can't see is a hard error, not a silent fallback."""
    if _git(primary, "cat-file", "-e", f"{commit}^{{commit}}", check=False).returncode == 0:
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


def _pin_existing_to_commit(primary: Path, target: Path, commit: str) -> None:
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
    from nothing at all, not even the ref this function is about to overwrite."""
    import_leftover_private_objects(target)
    _ensure_commit_present(primary, commit)
    _git(target, "reset", "--hard", commit)
    _git(target, "clean", "-fdx")
    _assert_head(target, commit)


def _git_worktree_add(primary: Path, target: Path, branch: str, base_branch: str,
                      commit: str | None = None) -> None:
    """Create a worktree at `target`. When `commit` is given, check out a detached
    HEAD pinned to that exact commit (review-class — no branch to drift, ARCH-184);
    otherwise create/reuse `branch` from base_branch."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if commit is not None:
        _ensure_commit_present(primary, commit)
        _git(primary, "worktree", "add", "--detach", str(target), commit)
        return
    # Does the branch already exist locally?
    cp = _git(primary, "rev-parse", "--verify", f"refs/heads/{branch}", check=False)
    if cp.returncode == 0:
        _git(primary, "worktree", "add", str(target), branch)
    else:
        _git(primary, "worktree", "add", "-b", branch, str(target), base_branch)


class WorktreeBusy(RuntimeError):
    """The target worktree is currently held by another active agent."""

    def __init__(self, path: Path, holder: dict):
        super().__init__(f"worktree {path} is held by {holder.get('run_id')} ({holder.get('agent')})")
        self.path = path
        self.holder = holder


def allocate_worktree(repo: str, purpose: str | None = None, *,
                      branch: str | None = None,
                      base_branch: str = DEFAULT_BASE_BRANCH,
                      commit: str | None = None) -> Path:
    """Resolve + (if needed) create a worktree for the given (repo, purpose).

    Refuses if the target is in the active-runs registry under another agent.
    Returns the absolute worktree path. The caller (agentctl spawn) is
    responsible for registering its own run against this path afterwards.

    `commit` pins the worktree to an exact SHA (ARCH-184, review integrity): on
    create it's a detached checkout at the commit; on reuse it's force-reset to
    the commit (killing the stale-review bug where a reused worktree kept an old
    HEAD). Refused for the primary tree — pinning hard-resets, and the primary is
    the human's clone, never something to clobber.
    """
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
            _pin_existing_to_commit(_primary_for(repo), target, commit)
        _link_primary_venv(_primary_for(repo), target)
        return target

    if purpose is None:
        # primary must already exist
        raise FileNotFoundError(f"primary worktree missing: {target}")

    primary = _primary_for(repo)
    use_branch = branch or branch_for_purpose(purpose)
    _git_worktree_add(primary, target, use_branch, base_branch, commit=commit)
    if commit is not None:
        _assert_head(target, commit)
    _link_primary_venv(primary, target)
    return target


# --- PR-branch reuse (ARCH-347) ---------------------------------------------
#
# Why this exists, separately from allocate_worktree: a worktree's branch is
# `agent/<purpose>`, and `purpose` is slugged from the dispatch TASK TEXT. A round-fix
# carries a different task than the first dispatch, so it gets a different slug → a
# different branch → a branch that is NOT the PR's. `publish` then pushes that orphan
# branch and reports success while the PR head never moves (the vivy-co footgun, #923).
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


def pr_merged(wt: Worktree) -> bool:
    """True iff a MERGED PR exists for this worktree's branch.

    Squash merges leave the branch permanently "ahead" of main, so ahead-ness
    alone can't distinguish landed work from live work — the PR state can.
    Any failure (no gh, no remote, offline) returns False, i.e. don't reap."""
    gh = _gh_path()
    if not gh or not wt.branch:
        return False
    cp = subprocess.run(
        [gh, "pr", "list", "--head", wt.branch, "--state", "merged",
         "--json", "number", "--limit", "1"],
        cwd=str(wt.path), capture_output=True, text=True,
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
        elif wt.branch and pr_merged(wt):
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
