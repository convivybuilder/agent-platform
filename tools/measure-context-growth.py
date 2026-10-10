#!/usr/bin/python3
"""measure-context-growth — the data the rollover epic's hard floor and call
cap get set from (platform/decisions/0069, Slice 2 of the rollover epic).

Read-only. Touches nothing: no policy file, no hook, no KB write. It reads
Claude Code transcripts under `~/.claude/projects/<dir>/<session>.jsonl` and
prints a markdown report of how fast in-context tokens grow across real
interactive `co` sessions, so the operator can set `floor_pct` and `seek_call_cap` from
measurement rather than guesswork.

WINDOW SIZE IS A CONSTANT, NOT A PER-RECORD FIELD. A transcript's `usage`
block carries `input_tokens`/`cache_read_input_tokens`/
`cache_creation_input_tokens` (the current call's context), but never the
window's total capacity — that number reaches Claude Code's status-line hook
in a separate payload (`context_window.context_window_size`,
`bin/co-session-health`) that is never written into the transcript itself.
Interactive `co` sessions on this machine run the Claude 5 family in its
1,000,000-token context mode (the figure `bin/co-session-health` calibrates
its own SOFT/HARD tiers against, and the one the rollover plan's own pilot
numbers reproduce: a 66,324-token first call is ~6.6% of 1M). WINDOW_TOKENS
below is that constant; a session actually running a smaller window would
have its percentages under-stated, not over-stated, by this assumption.

POPULATION. A directory is opened only when it passes BOTH of these,
matching `bin/transcript-capture`'s own `main()` exactly (its functions
reused directly here rather than re-implemented, so the two can't drift
apart):

  - The black-box denylist: skip a directory whose name matches a private
    crew's prefix (`TC.dir_matches_prefixes` against `TC.black_box_prefixes`).
  - The allowlist: admit a directory only when `TC.resolve_product` resolves
    its name to a REGISTERED, non-private project; skip it unopened
    otherwise. The denylist alone fails open — a headless `claude -p` job
    with no `WorkingDirectory` runs at cwd `/`, sanitizes to the dirname `-`,
    and matches no crew's prefix at all, which is how EA's private prompts
    leaked before `bin/transcript-capture` was fixed for the identical bug
    (its own docstring, "Capture is an allowlist"). The allowlist closes
    that: an unregistered directory has no registered slug to resolve to, so
    it is skipped rather than defaulting to included.

Dispatched (`agentctl spawn`) runs are excluded by SESSION, not by
directory. A spawned `claude` process (headless or not) writes its own
`~/.claude/projects/<dir>/<session-id>.jsonl` like any other invocation, and
`<session-id>` is also the `session_id` field carried on every event in that
run's own `<run_dir>/transcript.jsonl` copy (`_stream_transcript`'s tee) —
verified against a real run on this machine (the CLI-written project file's
name matches the first `session_id` seen in the run's own transcript
byte-for-byte). `dispatched_session_ids` reads that id from each run's own
transcript rather than sanitizing `worktree` from meta.json: a dispatched
run's `worktree` can legitimately equal a product's PRIMARY checkout by
design (`kb-sync-verifier` targets it deliberately, per `bin/agentctl`'s
`_is_worktree_class`), and that checkout also holds real interactive `co`
history. Excluding the whole directory once any run's worktree sanitized to
it dropped that entire product's interactive population; excluding only the
specific session file a dispatched run actually wrote leaves the rest of the
directory in.

`dispatched_session_ids` reads a run's OWN `meta.json` and skips a private
run's `transcript.jsonl` unopened (`_run_is_private`, via
`projects.dispatch_is_private`), the same privacy-before-content order
`collect_transcripts` keeps for interactive transcripts via the black-box/
allowlist checks above. A skipped private run's session id is simply never
matched here; its own interactive transcript is already excluded from
`collect_transcripts` by the same allowlist, so nothing leaks either way.

A private run's OWN dispatched transcript can still land inside an otherwise
PUBLIC directory: `agentctl spawn <agent> --on <public-repo> --agents-dir
<path under a private crew's home>` resolves `worktree` to the public repo
while `agent_source` alone makes `_run_is_private` correctly flag the run
private, so its `claude` process writes its transcript under that public
directory's `~/.claude/projects/<dir>`, passing both the black-box and
allowlist checks above. `dispatched_session_ids` cannot exclude it by
session id — learning that id would require opening the very
`transcript.jsonl` privacy forbids reading. `_private_dispatch_exclusions`
closes this the way `_run_is_private` does: from `meta.json`'s own
`worktree`/`started_at`/`ended_at` fields only, the transcript never opened.
Every transcript file in that run's worktree directory whose file-creation
time (`st_birthtime` on macOS, `st_mtime` where birthtime is unavailable)
falls inside `[started_at - margin, (ended_at or now) + margin]` is excluded
by `collect_transcripts` — by `stat()`, never `open()` or `read_text()`.

TRUST TRADE-OFF (PR #558 round 5, security-reviewer). This window is built
entirely from a private run's OWN `meta.json` — `worktree`, `started_at`,
`ended_at` — and that run is the only writer of that file, before and after
it finishes. `telemetry.update_meta` freezes exactly three fields against
rewrite (`origin`, `review_sha`, `model_resolved` — ARCH-156/193/378); these
three are not among them, so a run can, after the fact, rewrite its own
window to be narrower than what actually happened and admit its own
transcript into this measurement. The registry (`active.json`,
`lib/agent-platform/registry.py`) drops a run at finalize, so there is no
independent, tamper-resistant record left to check `meta.json` against once
the run has ended. Accepted, not just noted: the only party who can forge a
wider-or-narrower window for a run is that run itself, and what forging it
exposes is that run's OWN per-call token deltas and tool names in aggregate
— never another run's data, and never a session id or directory name (the
report and `CollectionCounts` below print counts only). A private run
choosing to expose itself this way is a self-inflicted outcome this module
has no way to prevent and no reason to prevent harder than it already
refuses to.

A second, distinct trade-off (PR #558 round 6, security-reviewer): `worktree`
names the directory `_private_dispatch_exclusions` excludes from, and is
self-reported with no tie to anything the run doesn't also control — nothing
here confirms the run actually ran where it claims to have. A private run
can therefore misdirect its exclusion at an unrelated PUBLIC crew's
directory, dropping that crew's own transcripts from this measurement. This
can only REMOVE measurement data, never expose it: the misdirected exclusion
skips files by `stat()` timing alone (`_created_within_windows`) or drops the
whole directory (`full_exclude_dirs`), and no content from the targeted
directory is ever read as a result. Where this shows: `CollectionCounts`'s
`implausible_metadata_dirs`/`implausible_metadata` and
`private_dispatch_window` fields, and the report's "whole directory(ies)
fully excluded" line — a directory disappearing from an otherwise-active
product's population is the visible symptom, not a silent count buried in a
combined total.

What this module DOES defend against is not forgery but IMPLAUSIBLE
metadata — a missing or malformed field, or a window shape (`ended` before
`started`, a span over 24 hours) that no real dispatch produces — because
trusting such a window's boundaries would leave real gaps: a run whose
`started_at` can't be parsed contributes no window at all under the naive
reading, which means nothing excludes its transcript. `_private_dispatch_
exclusions` fails CLOSED on implausible metadata by dropping every
transcript in that run's candidate directory outright, rather than trusting
a window shape that cannot be a real dispatch's timing.

Never prints transcript text. The report's only session id is the largest-
single-call-jump line (section 2), and that id can only ever be one this
module actually admitted into the corpus — nothing excluded by a privacy
decision (black-box, allowlist, dispatched-session, private-dispatch window,
or implausible-metadata) ever contributes a session id or directory name to
the report; each of those is surfaced as a count only (`CollectionCounts`,
below).
"""
from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import json
import math
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_DIR = REPO_ROOT / "lib" / "agent-platform"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

# Imported here, ahead of `_load_bin_module`, so `sys.modules` already holds
# THIS checkout's copies before transcript-capture's own `sys.path.insert`
# (the deployed `~/.local/lib/agent-platform` farm) runs — matching
# tests/conftest.py's own defensive re-pinning, for the identical reason: a
# worktree under test must not silently measure against the primary
# checkout's code.
import builder_root as BR  # noqa: E402
import projects  # noqa: E402
import session_view  # noqa: E402
import vault_plane  # noqa: E402,F401 — imported only to pin sys.modules ahead of
# transcript-capture's own `import vault_plane`, so that import returns THIS
# checkout's copy rather than reloading from the deployed farm; never called here


def _load_bin_module(filename: str):
    """Import a hyphenated, extension-less `bin/` script as a module.

    Matches tests/conftest.py's `_load_cli`: `spec_from_file_location` can't
    infer a loader for a file with no extension, so an explicit
    `SourceFileLoader` is handed in. `bin/transcript-capture`'s own top-level
    `sys.path.insert` + imports run as a side effect of `exec_module`, but by
    then `projects`/`session_view`/`vault_plane`/`builder_root` are already
    in `sys.modules`, so its `import projects` (etc.) return the exact
    objects imported above rather than reloading from the deployed farm.
    """
    modname = filename.replace("-", "_")
    path = REPO_ROOT / "bin" / filename
    loader = importlib.machinery.SourceFileLoader(modname, str(path))
    spec = importlib.util.spec_from_loader(modname, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


TC = _load_bin_module("transcript-capture")

CLAUDE_PROJECTS = session_view.CLAUDE_PROJECTS
WINDOW_TOKENS = 1_000_000
SEEK_PCT = 70.0
K_VALUES = (10, 20, 40, 80)
CALL_THRESHOLDS = (80, 90, 95, 100)

# Preflight (profile activation/individuation, git snapshots — see
# `activate_profile` in bin/agentctl) runs between a dispatched run's
# `started_at` being recorded and its `claude` process actually starting and
# creating its transcript file; the process's own exit and the transcript's
# last flush can likewise trail `ended_at` by some seconds. Wide on purpose,
# the same reasoning `GATE_RESOLVED_MARGIN_SECONDS` documents
# (lib/agent-platform/session_state.py): the only thing a larger value costs
# here is excluding a few more seconds from ONE private run's own worktree
# window, never a whole session's worth.
PRIVATE_DISPATCH_WINDOW_MARGIN_SECONDS = 120

# Mirrors bin/co-session-health's own un-namespaced tool-call signature
# (ARCH-088) exactly, so this tool's decile counts and that guard's live
# detection can never disagree about what counts as corruption.
_BARE_INVOKE = re.compile(r'(?<!antml:)<invoke\s+name=')
_BARE_PARAM = re.compile(r'(?<!antml:)<parameter\s+name=')

# A slash command's transcript scaffolding (session_view.py's `_slash_command`
# recognizes the same tag family; duplicated narrowly here rather than
# imported, since only the command name — not the fuller row-classification
# machinery — is needed).
_COMMAND_NAME_RE = re.compile(r"<command-name>\s*/?([^<\s]+)")
_HANDOFF_COMMANDS = {"rollover", "handoff-leave"}


# --- population: which transcripts count -----------------------------------


def _run_is_private(run_dir: Path) -> bool:
    """Whether `run_dir`'s dispatch reached a black-box crew, read from its
    OWN `meta.json` before `transcript.jsonl` is ever opened — this
    function's caller must decide privacy without reading a private run's
    dispatched transcript, the same requirement `collect_transcripts` meets
    for interactive transcripts via the black-box/allowlist checks below.
    `projects.dispatch_is_private` is the single comparison
    `bin/agentctl`'s own dispatch-privacy gate uses, given the `repo` and
    `agent_source` `meta.json` already records for every run.

    An unreadable or malformed `meta.json`, or a failed privacy check, is
    treated as private — unreadable must mean refused, never a default to
    public that opens the transcript anyway."""
    try:
        meta = json.loads((run_dir / "meta.json").read_text())
    except (OSError, ValueError):
        return True
    if not isinstance(meta, dict):
        return True
    try:
        return projects.dispatch_is_private(meta.get("repo"), meta.get("agent_source"))
    except Exception:
        return True


def dispatched_session_ids(runs_root: Path) -> set[str]:
    """Every dispatched run's own session id, read from the first line of its
    `<run_dir>/transcript.jsonl` that carries a `session_id` field. This is
    the exact `~/.claude/projects/<dir>/<session_id>.jsonl` the run's own
    `claude` process wrote — see the module docstring for why matching by
    session id, rather than by the directory `worktree` sanitizes to, is
    required (a dispatched run's worktree can legitimately be a product's
    primary checkout, which also holds real interactive history).

    A private run's `transcript.jsonl` is never opened (`_run_is_private`,
    checked against `meta.json` first): this function would otherwise open
    every dispatched run's transcript with no privacy check at all, breaking
    the invariant every other transcript read in this module honours. A
    skipped private run's session id is simply absent from the result, which
    means its own interactive transcript (already excluded from
    `collect_transcripts` by the same allowlist) can't be matched against
    here either — nothing leaks either way."""
    out: set[str] = set()
    if not runs_root.is_dir():
        return out
    for day_dir in sorted(p for p in runs_root.iterdir() if p.is_dir()):
        for run_dir in sorted(p for p in day_dir.iterdir() if p.is_dir()):
            if _run_is_private(run_dir):
                continue
            transcript_path = run_dir / "transcript.jsonl"
            try:
                raw_lines = transcript_path.read_text().splitlines()
            except OSError:
                continue
            for raw in raw_lines:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                sid = event.get("session_id") if isinstance(event, dict) else None
                if isinstance(sid, str) and sid:
                    out.add(sid)
                    break
    return out


def _parse_iso(s) -> "datetime | None":
    """Tolerant ISO-8601 parse (trailing `Z`, optional fractional seconds) —
    the same two `meta.json` timestamp flavours `agentctl`'s `T.now_iso()`
    and the SubagentStop hook produce. Reimplemented narrowly here rather
    than importing `lib/agent-platform/cost_join.py`'s own `parse_iso`,
    whose top-level `sys.path.insert(0, ~/.local/lib/agent-platform)` would
    risk shadowing this checkout's own `lib/` modules with the deployed
    farm's copies — exactly the drift the module docstring's import-pinning
    discussion (`import vault_plane`, above) exists to prevent."""
    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


MAX_PLAUSIBLE_DISPATCH_WINDOW = timedelta(hours=24)


class PrivateDispatchExclusions:
    """The result of scanning every private dispatched run's `meta.json` for
    a candidate worktree directory to exclude from — never its
    `transcript.jsonl`, which privacy forbids opening (see `_run_is_private`).

    `windows`: `{sanitized worktree dirname: [(window_start, window_end)]}`
    for a run whose metadata is PLAUSIBLE — every file in that directory
    created inside one of its windows is excluded, everything else stays.

    `full_exclude_dirs`: sanitized worktree dirnames for a run whose
    metadata is IMPLAUSIBLE but still names a real, existing directory —
    every file in that directory is excluded outright, since no window
    shape built from that metadata can be trusted (see `_is_implausible`).

    `unplaceable_count`: private runs with no usable directory at all
    (unreadable `meta.json`, no `worktree` field, or a `worktree` that
    doesn't sanitize to a directory that exists under `projects_root`) —
    nothing to exclude from, so nothing IS excluded; the count alone is
    what the report surfaces (PR #558 round 5, security-reviewer)."""

    def __init__(self) -> None:
        self.windows: dict[str, list[tuple[datetime, datetime]]] = {}
        self.full_exclude_dirs: set[str] = set()
        self.unplaceable_count = 0


def _is_implausible_window(started: "datetime | None", ended_field_present: bool,
                            ended: "datetime | None", now: "datetime") -> bool:
    """Whether a private run's `started_at`/`ended_at` describe a window no
    real dispatch could produce, per PR #558 round 5 (security-reviewer):

    - `started` unparseable/missing — there is no fallback for it (unlike
      `ended_at` missing, which legitimately means "still running" and
      falls back to `now`, extending the window forward rather than losing
      it — the safe direction, not implausible).
    - `ended_at` present in the metadata but fails to parse — a garbage
      value is a different, worse signal than the field being genuinely
      absent, so it is NOT given the same "still running" benefit.
    - the resolved end precedes `started` (a run ending before it began).
    - the resolved window exceeds 24 hours — no `agentctl spawn` run runs
      that long; trusting a window that wide risks trusting corrupted or
      adversarially-widened metadata that was never a real dispatch."""
    if started is None:
        return True
    if ended_field_present and ended is None:
        return True
    window_end = ended or now
    if window_end < started:
        return True
    return (window_end - started) > MAX_PLAUSIBLE_DISPATCH_WINDOW


def _private_dispatch_exclusions(runs_root: Path, projects_root: Path) -> PrivateDispatchExclusions:
    """Scan every PRIVATE dispatched run (`_run_is_private`) for a candidate
    worktree directory to exclude from — the gap `dispatched_session_ids`
    cannot close (see the module docstring): a private run dispatched into a
    PUBLIC repo's worktree writes its own transcript into that public
    directory, and its session id is never learned since `_run_is_private`
    correctly refuses to open `transcript.jsonl` to find it.

    Read from each run's OWN `meta.json` only — `worktree`/`started_at`/
    `ended_at` are exactly the fields `_run_is_private` and
    `dispatched_session_ids` already read from the same file.
    `transcript.jsonl` is never opened here. See the module docstring's
    "TRUST TRADE-OFF" section for what trusting this file, and failing
    closed on an implausible shape of it, does and doesn't defend against."""
    result = PrivateDispatchExclusions()
    if not runs_root.is_dir():
        return result
    margin = timedelta(seconds=PRIVATE_DISPATCH_WINDOW_MARGIN_SECONDS)
    now = datetime.now(timezone.utc)
    for day_dir in sorted(p for p in runs_root.iterdir() if p.is_dir()):
        for run_dir in sorted(p for p in day_dir.iterdir() if p.is_dir()):
            if not _run_is_private(run_dir):
                continue
            try:
                meta = json.loads((run_dir / "meta.json").read_text())
            except (OSError, ValueError):
                meta = None
            if not isinstance(meta, dict):
                result.unplaceable_count += 1
                continue
            worktree = meta.get("worktree")
            if not isinstance(worktree, str) or not worktree:
                result.unplaceable_count += 1
                continue
            dirname = TC._sanitize(Path(worktree))
            if not (projects_root / dirname).is_dir():
                result.unplaceable_count += 1
                continue

            started = _parse_iso(meta.get("started_at"))
            ended_field = meta.get("ended_at")
            ended = _parse_iso(ended_field) if ended_field is not None else None
            if _is_implausible_window(started, ended_field is not None, ended, now):
                result.full_exclude_dirs.add(dirname)
                continue

            window_end = ended or now
            result.windows.setdefault(dirname, []).append((started - margin, window_end + margin))
    return result


def _file_created_at(path: Path) -> "datetime | None":
    """`path`'s file-creation time, read via `stat()` only — never the
    file's own content. `st_birthtime` (macOS/APFS, and BSD generally) is
    the true creation time; where it's unavailable (most Linux
    filesystems), `st_mtime` is the closest available signal. `None` when
    `path` can't be stat'd at all."""
    try:
        st = path.stat()
    except OSError:
        return None
    ts = getattr(st, "st_birthtime", None)
    if ts is None:
        ts = st.st_mtime
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _created_within_windows(path: Path, windows: list[tuple[datetime, datetime]]) -> bool:
    """Whether `path`'s file-creation time falls inside any of `windows`.
    An unstat-able file is treated as INSIDE (excluded) rather than outside
    — the same unreadable-must-mean-refused posture `_run_is_private` takes
    on its own `meta.json` read, applied here to the one other place this
    module decides privacy from something other than content."""
    created = _file_created_at(path)
    if created is None:
        return True
    return any(start <= created <= end for start, end in windows)


class CollectionCounts:
    """How many transcripts `collect_transcripts` excluded, by rule — never a
    session id or directory name, only counts (PR #558 round 5, reviewer:
    a window-based exclusion can silently drop a legitimate transcript with
    nothing in the report showing it happened). Passed in by the caller so
    `main()` can read the tallies back after the call; tests that don't care
    about counts can omit it and let `collect_transcripts` discard its own.

    `implausible_metadata` is a FILE count and `implausible_metadata_dirs` is
    a DIRECTORY count (PR #558 round 6): implausible metadata on a single
    private run drops every co session transcript in its candidate directory
    at once (see `PrivateDispatchExclusions.full_exclude_dirs`), so a reader
    needs the directory count to tell a few stray files apart from a whole
    product's interactive history disappearing."""

    def __init__(self) -> None:
        self.private_or_unregistered_dir = 0
        self.dispatched_session = 0
        self.private_dispatch_window = 0
        self.implausible_metadata = 0
        self.implausible_metadata_dirs = 0
        self.unplaceable_private_runs = 0


def collect_transcripts(projects_root: Path, runs_root: Path,
                         counts: "CollectionCounts | None" = None) -> list[Path]:
    """Every transcript file in the interactive-`co`-sessions population:
    every `~/.claude/projects/<dir>/*.jsonl` whose directory clears both the
    black-box denylist and the registered-project allowlist (see the module
    docstring; both checks reused directly from `bin/transcript-capture`),
    minus the specific session files a dispatched (`agentctl spawn`) run's
    own `claude` process wrote, minus any file a private run's dispatch
    exclusion covers — either by time window, for a run with plausible
    metadata, or wholesale, for a run whose metadata is implausible enough
    that no window built from it can be trusted (`_private_dispatch_
    exclusions` — see the module docstring's "TRUST TRADE-OFF" section)."""
    if counts is None:
        counts = CollectionCounts()
    if not projects_root.is_dir():
        return []
    project_index = TC.all_project_prefixes()
    private = projects.private_slugs()
    black_box = TC.black_box_prefixes(project_index, private)
    dispatched_ids = dispatched_session_ids(runs_root)
    exclusions = _private_dispatch_exclusions(runs_root, projects_root)
    counts.unplaceable_private_runs += exclusions.unplaceable_count
    out: list[Path] = []
    for dirpath in sorted(p for p in projects_root.iterdir() if p.is_dir()):
        if (TC.dir_matches_prefixes(dirpath.name, black_box)
                or TC.resolve_product(dirpath.name, project_index, private) is None):
            counts.private_or_unregistered_dir += sum(1 for _ in dirpath.glob("*.jsonl"))
            continue
        full_exclude = dirpath.name in exclusions.full_exclude_dirs
        if full_exclude:
            counts.implausible_metadata_dirs += 1
        windows = exclusions.windows.get(dirpath.name)
        for f in sorted(dirpath.glob("*.jsonl")):
            if f.stem in dispatched_ids:
                counts.dispatched_session += 1
                continue
            if full_exclude:
                counts.implausible_metadata += 1
                continue
            if windows and _created_within_windows(f, windows):
                counts.private_dispatch_window += 1
                continue
            out.append(f)
    return out


# --- one transcript file: parse, dedupe, split ------------------------------


def load_main_chain_records(path: Path) -> list[dict]:
    """One transcript file's records, main-chain only, assistant records
    deduplicated on `message.id` (first occurrence wins — the plan's own
    pilot saw the same message written twice)."""
    try:
        raw_lines = path.read_text().splitlines()
    except OSError:
        return []
    seen_ids: set[str] = set()
    out: list[dict] = []
    for raw in raw_lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(rec, dict):
            continue
        if rec.get("isSidechain") is True:
            continue
        if rec.get("type") == "assistant":
            message = rec.get("message")
            mid = message.get("id") if isinstance(message, dict) else None
            if isinstance(mid, str):
                if mid in seen_ids:
                    continue
                seen_ids.add(mid)
        out.append(rec)
    return out


def split_rotations(records: list[dict]) -> list[list[dict]]:
    """Split at every `compact_boundary` record. The boundary record itself
    is dropped from both sides; an empty leading/trailing segment (a boundary
    at the very start or end, or two in a row) is dropped too."""
    rotations: list[list[dict]] = [[]]
    for rec in records:
        if rec.get("type") == "compact_boundary":
            rotations.append([])
            continue
        rotations[-1].append(rec)
    return [r for r in rotations if r]


# --- one rotation: per-call context, jumps, tool attribution, commands -----


def _context_tokens(usage: dict) -> int:
    return (
        int(usage.get("input_tokens", 0) or 0)
        + int(usage.get("cache_read_input_tokens", 0) or 0)
        + int(usage.get("cache_creation_input_tokens", 0) or 0)
    )


def _is_real_call(message: dict) -> bool:
    """Whether an assistant record is an actual API call rather than
    scaffolding with no context of its own.

    Claude Code writes a synthetic record for an interrupted turn:
    `message.model == "<synthetic>"`, all four usage fields zero, and a
    `message.id` not prefixed `msg_`. Counted as a call, it reports context 0,
    so the NEXT real call's whole context reads as a one-call jump — session
    `afb28de3-16c2-4617-804d-dc7af41ff787`'s reported 838,433-token jump at
    call 433 is exactly this: call 432 is `<synthetic>`. Same marker
    `lib/agent-platform/cost_join.py:337` already skips on for the identical
    reason (`if not model or model.startswith("<"): continue`), matched here
    rather than re-derived so the two can't drift apart.

    A record whose `usage` block is missing or empty entirely (some other,
    unnamed malformed shape) is excluded the same way: it is neither a call
    nor a valid baseline for the next one, whatever its `model` field says."""
    model = message.get("model")
    if not isinstance(model, str) or not model or model.startswith("<"):
        return False
    usage = message.get("usage")
    return isinstance(usage, dict) and bool(usage)


def _has_corruption(content) -> bool:
    """The malformed tool-call signature, checked the same way
    `co-session-health._corruption_in_tail` checks it: both the bare
    `<invoke>` and the bare `<parameter>` tag inside the SAME text block."""
    if not isinstance(content, list):
        return False
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text", "")
        if isinstance(text, str) and _BARE_INVOKE.search(text) and _BARE_PARAM.search(text):
            return True
    return False


def _command_name(rec: dict) -> str | None:
    """The slash command a `user` record's scaffolding names, or None."""
    if rec.get("type") != "user":
        return None
    message = rec.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        text = "".join(
            block.get("text", "") for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    elif isinstance(content, str):
        text = content
    else:
        return None
    stripped = text.strip()
    if not stripped.startswith("<command-name>"):
        return None
    m = _COMMAND_NAME_RE.search(stripped)
    return m.group(1) if m else None


def _is_turn_boundary(rec: dict) -> bool:
    """A genuine new conversational turn: a `user` record that is neither a
    tool-result echo (`toolUseResult` present) nor harness-injected context
    (`isMeta`). Used only to close an open command span — see
    `analyze_rotation`."""
    if rec.get("type") != "user":
        return False
    if "toolUseResult" in rec:
        return False
    if rec.get("isMeta") is True:
        return False
    return True


def analyze_rotation(records: list[dict]) -> tuple[list[dict], list[dict]]:
    """One rotation's per-call context data and command-span events.

    Returns `(calls, command_events)`.

    `calls` is one entry per REAL assistant record (`_is_real_call`), in
    order — a synthetic or zero/missing-usage record is skipped entirely: not
    counted as a call, and never used as the baseline the next real call's
    growth is measured against:
    `{context, growth, growth_pct, pct, tools, corrupt}` — `context` is the
    call's total in-context tokens, `growth` its rise over the PREVIOUS real
    call (the first real call's growth is measured against a baseline of 0,
    matching the rollover plan's own pilot, which counted the first call
    after a compact as a real data point), `pct`/`growth_pct` are both as a
    percentage of `WINDOW_TOKENS`, `tools` is the sorted tool name(s) whose
    result arrived since the previous call (attributing the jump), and
    `corrupt` is whether this call's own text carries the malformed
    tool-call signature.

    `command_events` is one entry per `/rollover` or `/handoff-leave`
    invocation found: `{"command", "start_idx", "end_idx"}`, `start_idx`/
    `end_idx` indexing into `calls` (`start_idx` is -1 when the command was
    the rotation's very first turn). The span runs from the command's
    invocation to the next genuine turn boundary (or rotation end).
    """
    calls: list[dict] = []
    pending_tool_names: dict[str, str] = {}
    current_tool_batch: list[str] = []
    command_events: list[dict] = []
    open_command: dict | None = None

    for rec in records:
        rtype = rec.get("type")
        if rtype == "assistant":
            message = rec.get("message") or {}
            if not _is_real_call(message):
                continue
            usage = message.get("usage") or {}
            context = _context_tokens(usage)
            prev_context = calls[-1]["context"] if calls else 0
            growth = context - prev_context
            calls.append({
                "context": context,
                "growth": growth,
                "growth_pct": 100.0 * growth / WINDOW_TOKENS,
                "pct": 100.0 * context / WINDOW_TOKENS,
                "tools": sorted(current_tool_batch),
                "corrupt": _has_corruption(message.get("content")),
            })
            current_tool_batch = []
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_id = block.get("id")
                        name = block.get("name")
                        if isinstance(tool_id, str) and isinstance(name, str):
                            pending_tool_names[tool_id] = name
        elif rtype == "user":
            message = rec.get("message") or {}
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        name = pending_tool_names.pop(block.get("tool_use_id"), None)
                        if name:
                            current_tool_batch.append(name)
            if _is_turn_boundary(rec):
                if open_command is not None:
                    command_events.append({
                        "command": open_command["command"],
                        "start_idx": open_command["start_idx"],
                        "end_idx": len(calls) - 1,
                    })
                    open_command = None
                name = _command_name(rec)
                if name is not None and name.lstrip("/") in _HANDOFF_COMMANDS:
                    open_command = {"command": name.lstrip("/"), "start_idx": len(calls) - 1}

    if open_command is not None:
        command_events.append({
            "command": open_command["command"],
            "start_idx": open_command["start_idx"],
            "end_idx": len(calls) - 1,
        })
    return calls, command_events


def command_event_cost(calls: list[dict], event: dict) -> tuple[int, float, int]:
    """`(growth_tokens, growth_pct, num_calls)` a command span consumed."""
    start_idx, end_idx = event["start_idx"], event["end_idx"]
    if end_idx < start_idx:
        return 0, 0.0, 0
    baseline = calls[start_idx]["context"] if start_idx >= 0 else 0
    end_context = calls[end_idx]["context"] if end_idx >= 0 else baseline
    growth = end_context - baseline
    return growth, 100.0 * growth / WINDOW_TOKENS, end_idx - start_idx


# --- corpus-wide aggregation -------------------------------------------------


def max_rise_after_threshold(calls: list[dict], threshold_pct: float, k: int) -> float | None:
    """The largest sum of `growth_pct` over any `k` CONSECUTIVE calls, among
    calls strictly after the first one that reached `threshold_pct`. None
    when fewer than `k` calls follow the crossing."""
    cross = next((i for i, c in enumerate(calls) if c["pct"] >= threshold_pct), None)
    if cross is None:
        return None
    tail = [c["growth_pct"] for c in calls[cross + 1:]]
    if len(tail) < k:
        return None
    window_sum = sum(tail[:k])
    best = window_sum
    for i in range(k, len(tail)):
        window_sum += tail[i] - tail[i - k]
        best = max(best, window_sum)
    return best


def calls_to_thresholds(calls: list[dict], seek_pct: float,
                         thresholds: tuple[int, ...]) -> dict[int, int | None]:
    """From the first call reaching `seek_pct`, how many further calls it
    took to reach each of `thresholds`. None for a threshold never reached in
    this rotation. Empty dict if `seek_pct` itself is never reached."""
    cross = next((i for i, c in enumerate(calls) if c["pct"] >= seek_pct), None)
    if cross is None:
        return {}
    out: dict[int, int | None] = {}
    for t in thresholds:
        idx = next((i for i in range(cross, len(calls)) if calls[i]["pct"] >= t), None)
        out[t] = (idx - cross) if idx is not None else None
    return out


def percentile(data: list[float], p: float) -> float | None:
    """Linear-interpolation percentile — no third-party dependency, and
    matches the conventional p50/p90/p99 definition closely enough for a
    proposal a human reviews before it becomes policy."""
    if not data:
        return None
    s = sorted(data)
    if p >= 100:
        return s[-1]
    k = (len(s) - 1) * (p / 100.0)
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] + (s[c] - s[f]) * (k - f)


def _decile(pct: float) -> int:
    return max(0, min(9, int(pct // 10)))


class Corpus:
    """Accumulates every measurement across every rotation in the corpus."""

    def __init__(self) -> None:
        self.growth_tokens: list[int] = []
        self.growth_pct: list[float] = []
        self.growth_pct_after_70: list[float] = []
        self.max_jump: dict | None = None
        self.rise_after_70: dict[int, list[float]] = {k: [] for k in K_VALUES}
        self.calls_to: dict[int, list[int]] = {t: [] for t in CALL_THRESHOLDS}
        self.sessions_reaching_70 = 0
        self.sessions_total = 0
        self.corruption_by_decile: dict[int, int] = {d: 0 for d in range(10)}
        self.total_corrupt_calls = 0
        self.command_costs: dict[str, list[float]] = {c: [] for c in _HANDOFF_COMMANDS}
        self.command_sessions: dict[str, list[str]] = {c: [] for c in _HANDOFF_COMMANDS}
        self.rotation_count = 0
        self.session_count = 0

    def add_rotation(self, session_id: str, calls: list[dict], command_events: list[dict]) -> None:
        if not calls:
            return
        self.rotation_count += 1
        self.sessions_total += 1
        for i, c in enumerate(calls):
            # Call 0's "growth" is its whole opening context measured against
            # analyze_rotation's 0 baseline — an opening payload, not growth
            # inside this session, and every post-compaction rotation has
            # one. Counted here it inflates the global p99 the fixed cap
            # formula divides by, and can win "largest single-call jump"
            # outright (PR #558 round 3, reviewer: the 181,548-token/18%
            # jump this report showed before this fix was exactly a
            # rotation's first call). `growth_pct_after_70`/`rise_after_70`
            # need no matching change: both already start from `cross + 1`,
            # which is never index 0 (a rotation's own SEEK_PCT crossing, if
            # any, is itself the earliest possible `cross`).
            if i > 0:
                self.growth_tokens.append(c["growth"])
                self.growth_pct.append(c["growth_pct"])
                if self.max_jump is None or c["growth"] > self.max_jump["growth_tokens"]:
                    self.max_jump = {
                        "growth_tokens": c["growth"],
                        "growth_pct": c["growth_pct"],
                        "session_id": session_id,
                        "call_index": i,
                        "tools": c["tools"],
                    }
            if c["corrupt"]:
                self.total_corrupt_calls += 1
                self.corruption_by_decile[_decile(c["pct"])] += 1

        cross = next((i for i, c in enumerate(calls) if c["pct"] >= SEEK_PCT), None)
        if cross is not None:
            self.sessions_reaching_70 += 1
            self.growth_pct_after_70.extend(c["growth_pct"] for c in calls[cross + 1:])
        for k in K_VALUES:
            rise = max_rise_after_threshold(calls, SEEK_PCT, k)
            if rise is not None:
                self.rise_after_70[k].append(rise)
        for t, n in calls_to_thresholds(calls, SEEK_PCT, CALL_THRESHOLDS).items():
            if n is not None:
                self.calls_to[t].append(n)

        for event in command_events:
            cmd = event["command"]
            if cmd not in _HANDOFF_COMMANDS:
                continue
            _, growth_pct, _ = command_event_cost(calls, event)
            self.command_costs[cmd].append(growth_pct)
            self.command_sessions[cmd].append(session_id)


def analyze_corpus(transcripts: list[Path]) -> Corpus:
    corpus = Corpus()
    for path in transcripts:
        session_id = path.stem
        records = load_main_chain_records(path)
        if not records:
            continue
        corpus.session_count += 1
        for rotation in split_rotations(records):
            calls, command_events = analyze_rotation(rotation)
            corpus.add_rotation(session_id, calls, command_events)
    return corpus


# --- report -------------------------------------------------------------------


def _fmt(v, digits=1):
    return "n/a" if v is None else f"{v:.{digits}f}"


def build_report(corpus: Corpus, counts: "CollectionCounts | None" = None) -> str:
    lines: list[str] = []
    a = lines.append
    a("# Context-growth measurement (rollover epic, Slice 2)")
    a("")
    a(f"Window assumed: {WINDOW_TOKENS:,} tokens (see module docstring). "
      f"Seek threshold: {SEEK_PCT:.0f}%.")
    a(f"Sessions scanned: {corpus.session_count}. Rotations analyzed: "
      f"{corpus.rotation_count}. Rotations that ever reached {SEEK_PCT:.0f}%: "
      f"{corpus.sessions_reaching_70}.")
    if counts is not None:
        a(f"Excluded before scanning: {counts.private_or_unregistered_dir} in a "
          "private-or-unregistered directory, "
          f"{counts.dispatched_session} by dispatched-session id, "
          f"{counts.private_dispatch_window} transcript file(s) by a private-dispatch window.")
        dir_word = "directory" if counts.implausible_metadata_dirs == 1 else "directories"
        a(f"**{counts.implausible_metadata_dirs} whole {dir_word} fully excluded** for "
          "implausible private-run metadata — every co session transcript under an "
          "excluded directory is gone from this measurement, not just the offending "
          f"run's own file ({counts.implausible_metadata} transcript file(s) dropped "
          "as a result).")
        a(f"{counts.unplaceable_private_runs} private run(s) had no directory to "
          "exclude from and are counted here only, not excluded from anywhere.")
    a("")

    a("## 1. Per-call context growth")
    a("")
    a("| stat | tokens | % of window |")
    a("|---|---|---|")
    for label, p in (("p50", 50), ("p90", 90), ("p99", 99)):
        tok = percentile([float(g) for g in corpus.growth_tokens], p)
        pct = percentile(corpus.growth_pct, p)
        a(f"| {label} | {_fmt(tok, 0)} | {_fmt(pct, 2)}% |")
    max_tok = max(corpus.growth_tokens) if corpus.growth_tokens else None
    max_pct = max(corpus.growth_pct) if corpus.growth_pct else None
    a(f"| max | {_fmt(max_tok, 0)} | {_fmt(max_pct, 2)}% |")
    a(f"n = {len(corpus.growth_tokens)} calls")
    a("")
    p99_growth_pct = percentile(corpus.growth_pct, 99)
    p99_after_70 = percentile(corpus.growth_pct_after_70, 99)
    n_after_70 = len(corpus.growth_pct_after_70)
    a(f"p99 restricted to calls after crossing {SEEK_PCT:.0f}%: "
      f"{_fmt(p99_after_70, 4)}% (n = {n_after_70} calls, drawn from "
      f"{corpus.sessions_reaching_70} of {corpus.rotation_count} rotations that "
      f"ever crossed {SEEK_PCT:.0f}%).")
    if p99_growth_pct is None or p99_after_70 is None:
        a("Direction vs. the global p99 above: unknowable — one of the two "
          "series is empty.")
    elif min(len(corpus.growth_pct), n_after_70) < min(K_VALUES):
        a(f"Direction vs. the global p99 above: unknowable at this sample "
          f"size (n = {n_after_70} calls after crossing, below the "
          f"{min(K_VALUES)}-call floor this report treats as trustworthy).")
    else:
        cmp_word = ("higher than" if p99_growth_pct > p99_after_70
                    else "lower than" if p99_growth_pct < p99_after_70
                    else "equal to")
        a(f"Direction vs. the global p99 above: the global per-call p99 "
          f"({_fmt(p99_growth_pct, 4)}%, the fixed cap formula's divisor) "
          f"runs {cmp_word} the p99 over calls after crossing {SEEK_PCT:.0f}% "
          f"({_fmt(p99_after_70, 4)}%).")
    a("")

    a("## 2. Largest single-call jump")
    a("")
    if corpus.max_jump is None:
        a("No calls observed.")
    else:
        mj = corpus.max_jump
        tools = "+".join(mj["tools"]) if mj["tools"] else "none recorded"
        a(f"{mj['growth_tokens']:,} tokens ({mj['growth_pct']:.2f}% of window), "
          f"session `{mj['session_id']}`, call index {mj['call_index']}, "
          f"tool(s): {tools}.")
    a("")

    a("## 3. Largest rise over K consecutive calls after crossing "
      f"{SEEK_PCT:.0f}%")
    a("")
    a("| K | max rise (% of window) | n rotations |")
    a("|---|---|---|")
    for k in K_VALUES:
        vals = corpus.rise_after_70[k]
        best = max(vals) if vals else None
        a(f"| {k} | {_fmt(best, 2)}% | {len(vals)} |")
    a("")

    a("## 4. Calls from 70% to each threshold, per session")
    a("")
    a("| threshold | p50 calls | p90 calls | max calls | n sessions reaching it |")
    a("|---|---|---|---|---|")
    for t in CALL_THRESHOLDS:
        vals = [float(n) for n in corpus.calls_to[t]]
        a(f"| {t}% | {_fmt(percentile(vals, 50), 1)} | {_fmt(percentile(vals, 90), 1)} | "
          f"{_fmt(max(vals) if vals else None, 0)} | {len(vals)} |")
    a("")

    a("## 5. Context consumed by past /rollover and /handoff-leave runs")
    a("")
    a("| command | n occurrences | p50 % of window | max % of window |")
    a("|---|---|---|---|")
    for cmd in sorted(_HANDOFF_COMMANDS):
        vals = corpus.command_costs[cmd]
        a(f"| /{cmd} | {len(vals)} | {_fmt(percentile(vals, 50), 2)}% | "
          f"{_fmt(max(vals) if vals else None, 2)}% |")
    all_handoff_vals = [v for vals in corpus.command_costs.values() for v in vals]
    handoff_cost_pct = max(all_handoff_vals) if all_handoff_vals else 0.0
    if not all_handoff_vals:
        a("No /rollover or /handoff-leave invocation was found in this corpus; "
          "handoff_cost_pct defaults to 0.0 below, which pushes floor_pct(K) "
          "HIGHER and cap(K) LARGER than a measured value would — the "
          "optimistic direction, since an unmeasured handoff is assumed to "
          "cost nothing. Revisit once occurrences exist.")
    a("")

    a("## 6. Malformed tool-call signature by context decile")
    a("")
    a("| decile | pct range | corrupt calls |")
    a("|---|---|---|")
    for d in range(10):
        lo, hi = d * 10, (d + 1) * 10
        hi_label = "100" if d == 9 else str(hi)
        a(f"| {d} | {lo}-{hi_label}% | {corpus.corruption_by_decile[d]} |")
    a(f"Total corrupt calls: {corpus.total_corrupt_calls}")
    a("")

    a("## Proposed floor and cap")
    a("")
    a("floor_pct(K) = 100 - (largest rise over K calls after "
      f"{SEEK_PCT:.0f}% + handoff_cost_pct)")
    a("cap(K) = ceil((floor_pct(K) - seek_pct) / p99_growth_pct_per_call), "
      "the fixed formula, using the GLOBAL per-call p99 (all calls, any pct)")
    a("cap_after70(K) = the same formula using the p99 restricted to calls "
      f"after crossing {SEEK_PCT:.0f}% instead — see section 1 for which one "
      "runs higher on this corpus")
    a("")
    a(f"handoff_cost_pct = {handoff_cost_pct:.2f} (max observed /rollover or "
      f"/handoff-leave cost, or 0.0 if none observed; n = {len(all_handoff_vals)} "
      "occurrences)")
    a(f"p99_growth_pct_per_call (global) = {_fmt(p99_growth_pct, 4)}% "
      f"(n = {len(corpus.growth_pct)} calls)")
    a(f"p99_growth_pct_per_call (after crossing {SEEK_PCT:.0f}%) = "
      f"{_fmt(p99_after_70, 4)}% (n = {n_after_70} calls)")
    a(f"Rotations crossing {SEEK_PCT:.0f}%: {corpus.sessions_reaching_70} of "
      f"{corpus.rotation_count} total — the denominator every row below draws "
      "its own K-specific n from (n rotations column), which is at most this "
      "count.")
    a("")
    a("| K | n rotations reaching K calls after crossing | floor_pct(K) | "
      "cap(K) [global p99] | cap_after70(K) [after-70 p99] |")
    a("|---|---|---|---|---|")
    for k in K_VALUES:
        vals = corpus.rise_after_70[k]
        rise = max(vals) if vals else None
        n_k = len(vals)
        if rise is None:
            a(f"| {k} | {n_k} | n/a (no rotation had {k} calls after crossing "
              f"{SEEK_PCT:.0f}%) | n/a | n/a |")
            continue
        floor_pct = 100.0 - (rise + handoff_cost_pct)
        if p99_growth_pct and p99_growth_pct > 0:
            cap = math.ceil((floor_pct - SEEK_PCT) / p99_growth_pct)
        else:
            cap = None
        if p99_after_70 and p99_after_70 > 0:
            cap_after70 = math.ceil((floor_pct - SEEK_PCT) / p99_after_70)
        else:
            cap_after70 = None
        a(f"| {k} | {n_k} | {floor_pct:.2f}% | "
          f"{cap if cap is not None else 'n/a'} | "
          f"{cap_after70 if cap_after70 is not None else 'n/a'} |")
    a("")
    a("The values are the operator's to approve; the formulas above are fixed.")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("output", nargs="?", type=Path,
                    help="optional path to also write the report to")
    ap.add_argument("--projects-root", type=Path, default=CLAUDE_PROJECTS,
                    help="override ~/.claude/projects (for testing)")
    ap.add_argument("--runs-root", type=Path, default=None,
                    help="override the agent-runs root (for testing)")
    args = ap.parse_args()

    runs_root = args.runs_root if args.runs_root is not None else BR.runs_root()
    counts = CollectionCounts()
    transcripts = collect_transcripts(args.projects_root, runs_root, counts)
    corpus = analyze_corpus(transcripts)
    report = build_report(corpus, counts)

    sys.stdout.write(report)
    if args.output is not None:
        args.output.write_text(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
