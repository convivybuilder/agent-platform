#!/usr/bin/python3
"""Append Claude Code hook events to a per-session JSONL, for instrumentation.

This is step 1 of the fleet-awareness build: a pure logger that changes no
behavior. Run it for a day, read the result, and learn which hook events
actually fire under this machine's permission mode (`bypassPermissions`) and in
what order. Step 2 reduces those events into per-session state files; writing
that reducer against measured behavior beats writing it against documentation.

It also captures two things the GUI architecture depends on and which are
currently inferred rather than verified:

  * `ppid` — whether a hook's parent process is the `claude` process, which is
    how a session row would join to a live PID for liveness.
  * `TMUX` / `TMUX_PANE` — whether a hook can see the tmux pane it runs in,
    which is how a session row would join to the pane a GUI must attach to.

Safety contract, in priority order over everything else this file does:

  1. ALWAYS exit 0. A `Stop` hook that exits 2 blocks the stop and forces the
     model to keep going, so a crash here would wedge every session on the
     machine. Every failure path is swallowed.
  2. Never block. No network, no lock acquisition. One append to a per-session
     file, so concurrent sessions never contend and a partial write cannot
     interleave with another session's line. The one subprocess is a
     `tmux display-message` at SessionStart, and only inside a tmux pane; it is
     bounded to two seconds (see `_tmux_session_name`).
  3. Never grow without bound in a single line. Oversized payloads are
     truncated with a marker rather than written whole.

Registered in `~/.claude/settings.json` on lifecycle events only, plus
`PreToolUse` narrowed by matcher to the asking-class tools. Deliberately NOT on
unfiltered `PreToolUse`/`PostToolUse`: those fire per tool call, and a ~40ms
interpreter start on each would put seconds of latency into a busy turn.

Remove by deleting this file's entries from `~/.claude/settings.json`. Nothing
else reads what it writes yet.
"""

import datetime
import glob
import json
import os
import re
import stat
import subprocess
import sys

MAX_LINE = 32_768
MAX_FIELD = 256
ENV_KEYS = ("TMUX", "TMUX_PANE", "CLAUDE_CODE_SESSION_ID", "TERM_SESSION_ID")

# The charset `tmux_transcript.SESSION_NAME_RE` uses. Repeated rather than imported:
# importing tmux_transcript loads telemetry and rewrites sys.path at hook start.
# tests/test_session_event_log.py pins the two patterns equal.
_TMUX_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")


# --- black box crews (platform/decisions/0043) -----------------------------------
#
# WHY THIS EXISTS. This hook is registered GLOBALLY, so it runs for every session
# in every directory, and `UserPromptSubmit` carries the user's full typed prompt
# while `Stop` carries `last_assistant_message`. For a black box crew that is the
# crew's private content landing in the shared run root, outside every layer
# ADR-0043 defines: its transport wall stops the session reaching the vaults, and
# none of it touches a writer that runs beside the session rather than inside it.
# Measured 2026-08-18 before the fix: 14 sessions for one black-box crew holding
# 147,005 characters of verbatim prompt and assistant text, and 27 for the
# walled-off crew.
#
# WHERE IT GOES INSTEAD, rather than being dropped: the operator's ledger-split rule says a
# black box crew's detailed record belongs inside the crew's own directory. So the
# record is written to `<crew home>/hook-log/` and the crew keeps its own history.
#
# THE DECISION IS MADE BEFORE THE WRITE, from the session's own directory. A filter
# applied after the content is in hand is not an exclusion, and this hook is the
# writer, so there is no later stage to filter at.
#
# IT CANNOT KEY ON THE FILENAME. These files are named by session id, which is a
# hash; the only signal about which crew a session belongs to is `payload.cwd`.
#
# `_private_home_for`/`_UNKNOWN` used to be defined here directly; a reader
# (`session_state.py`) needed the identical answer and had no copy at all,
# which is how a black-box crew's hook log went unreadable for three days
# (agent-platform#421 / issue_01M0GVMBXCX10F0R3ZAGHGEKFZ) — the shared root
# this reader used to write to moved and nothing told it. Both sides now import
# `lib/agent-platform/private_crew.py`, so there is one place this logic can be
# wrong instead of two that can disagree.
#
# THE IMPORT IS DEFENSIVE, not because the module is optional — every call site
# below still needs a real answer — but because an import failure here happens
# at module load, before `main()`'s own `except BaseException` on line ~450 can
# see it, and the safety contract above ("ALWAYS exit 0") does not carve out an
# exception for "the shared module could not be found." Falling back to a
# private_home_for that always answers `_UNKNOWN` keeps every downstream branch
# on the SAME fail-closed path an unreadable registry already takes.
#
# `BaseException`, not `Exception`, matching the bottom-of-file guard and for
# the identical reason stated there: `KeyboardInterrupt`/`SystemExit` derive
# only from `BaseException`, and this import runs before that guard exists to
# catch anything (security-reviewer, PR #421, run-71cd97f7) — an
# `except Exception` here would let either propagate straight through module
# load and take the exit-0 contract down with it.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib", "agent-platform"))
try:
    from private_crew import UNKNOWN as _UNKNOWN, private_home_for as _private_home_for
except BaseException:
    _UNKNOWN = object()

    def _private_home_for(cwd):  # noqa: ARG001 — signature parity with the shared version
        return _UNKNOWN

# The run-records root, from the resolver every reader uses (builder_root.runs_root(),
# company/decisions/0008), imported defensively for the reason the block above gives.
# On failure both roots are None: a write under them raises inside main(), whose
# guard exits 0 having written nothing, so the hook still never breaks a session.
try:
    import builder_root as _BR
    RUNS_ROOT = str(_BR.runs_root())
    LOG_ROOT = os.path.join(RUNS_ROOT, "hook-log")
except BaseException:
    RUNS_ROOT = LOG_ROOT = None

# `telemetry.ensure_real_dir`/`RunRootIsSymlink`: the same lstat-before-mkdir
# check `_ensure_root`/`create_run_dir` use for a private crew's own
# `agent-runs` root, reused here rather than a second copy of the identical
# logic (ARCH-322 round 3) — `telemetry.py` makes the SAME `builder_root`
# call this module already makes directly above, so importing it is no
# riskier than that existing import, and is wrapped the same defensive way.
# On failure, `_ensure_dir_or_bail` below falls back to this module's own
# `_is_real_dir` + `os.makedirs`.
try:
    from telemetry import ensure_real_dir as _ensure_real_dir, RunRootIsSymlink as _RunRootIsSymlink
except BaseException:
    _ensure_real_dir = None
    _RunRootIsSymlink = OSError


# The payload fields that carry what a person or the model actually said. Stripped
# rather than truncated when the registry cannot be read, because a truncated
# prompt is still a prompt. `tool_input` is in this list because this hook is also
# registered on `PreToolUse` for `AskUserQuestion`/`ExitPlanMode` (~/.claude/settings.json),
# whose content lives there rather than in any of the other four fields — a
# question or a plan is exactly the kind of content this redaction exists to stop.
_CONTENT_FIELDS = ("prompt", "user_prompt", "last_assistant_message", "message", "tool_input")

# The payload fields that don't carry what anyone said but still identify which
# crew a session belongs to. `cwd` is the field `_private_home_for` itself reads
# to make that call (the module's only signal, per the comment above), and
# `transcript_path` is a live pointer at that same crew's full verbatim
# transcript — `_co_record`'s docstring already treats both as "identifying and
# content-pointing information ADR-0043 keeps out of the shared dashboard
# surface" for its own record. `_strip_content` used to leave both untouched, so
# an `_UNKNOWN` crew's identity survived in the one payload this whole mechanism
# exists to protect (reviewer, PR #413, run-72daaa55).
_IDENTITY_FIELDS = ("cwd", "transcript_path")


def _strip_content(payload):
    """Drop the content-bearing and crew-identifying fields, event shape intact.

    `_unparsed` gets the same redaction though it names no field in either list
    above: `main()` sets it to the entire raw stdin body when JSON parsing
    failed or produced something other than an object (a bare array, string, or
    other top-level value), so by construction it can hold everything
    `_CONTENT_FIELDS` and `_IDENTITY_FIELDS` exist to protect — the raw prompt
    text and the real `cwd` included, on the exact `_UNKNOWN` path this
    function runs for (reviewer, PR #413, run-a4882834).
    """
    out = dict(payload)
    for key in _CONTENT_FIELDS + _IDENTITY_FIELDS + ("_unparsed",):
        if key in out:
            out[key] = "[redacted: crew privacy could not be determined]"
    return out


def _safe_name(value, fallback):
    """Reduce an untrusted payload field to something safe as a filename."""
    if not isinstance(value, str):
        return fallback
    keep = [c for c in value if c.isalnum() or c in "-_"]
    return "".join(keep)[:64] or fallback


# --- symlink-safe write primitives (ARCH-322 round 3: a private crew's own
# sandboxed dispatch can replace a predictable path under its own home —
# `hook-log`, a day directory, `agent-runs` — with a symlink to anywhere the
# real OS user can write, and this hook runs UNSANDBOXED (registered in
# `~/.claude/settings.json`; see `run_sandbox.py`'s "Edit and Write tools run
# outside the sandbox" comment, which applies equally to a hook process).
# `os.makedirs(..., exist_ok=True)` and a plain `open(path, "w")` both follow
# such a symlink silently, laundering a write the crew's own sandboxed Bash
# tool could not make directly. These two primitives are the fix: a
# directory check that refuses a symlink outright, and file opens that
# refuse to follow one at the final path component. ----------------------


def _is_real_dir(path):
    """True iff `path` exists and is a directory that is NOT a symlink.

    `os.lstat`, never `os.stat`: `stat` follows a symlink to check what it
    points at, which is exactly the question this function must answer NO
    to for a symlink pointing at a real directory elsewhere."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode)


def _is_safe_co_meta_path(meta_path):
    """True iff both the day directory and the `co-<sid>` directory naming
    `meta_path` are real, non-symlink directories — the same pair of
    predictable, plantable path components SessionStart checks (each via
    `_ensure_dir_or_bail`) before creating anything under them. Stop does
    not create either directory, but must refuse to read or write through
    one that was replaced with a symlink exactly as SessionStart refuses to
    create under one (ARCH-322 round 3)."""
    co_dir = os.path.dirname(meta_path)
    day_dir = os.path.dirname(co_dir)
    return _is_real_dir(day_dir) and _is_real_dir(co_dir)


def _ensure_dir_or_bail(path):
    """Make `path` a real, non-symlink directory, creating it if absent;
    True on success, False when it must be refused (something else already
    sits there, or creation failed).

    Uses the shared `telemetry.ensure_real_dir` when it imported cleanly —
    the SAME lstat-before-mkdir check `_ensure_root`/`create_run_dir` use
    for this exact family of path (ARCH-322 round 3), rather than a second
    copy of it here — translating its raised `RunRootIsSymlink`/`OSError`
    into this module's own never-raise idiom. Falls back to this module's
    own `_is_real_dir` + `os.makedirs` only if that import failed."""
    if _ensure_real_dir is not None:
        try:
            _ensure_real_dir(path)
        except (_RunRootIsSymlink, OSError):
            return False
        return True
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        return False
    return _is_real_dir(path)


def _open_create_only(path, mode=0o644):
    """Create `path` and return its fd, or None if it already exists (file,
    directory, or symlink — dangling or not) or cannot be created.

    `O_EXCL` with `O_CREAT` refuses to follow a symlink at all: the open
    fails if anything already sits at `path`, even a broken link, so this
    never writes through one. Used for a file that is written once, only
    when absent (`.gitignore`, the Spotlight marker)."""
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW
    try:
        return os.open(path, flags, mode)
    except OSError:
        return None


def _open_write_nofollow(path, mode=0o600, *, append=False):
    """Open `path` for writing (creating it if absent) and return its fd, or
    None. Refuses to follow a symlink at the final path component.

    Unlike `_open_create_only`, this overwrites (or appends to) an existing
    REAL file — the day's `.jsonl`, a co-session's `meta.json` — which a
    plain `O_EXCL` cannot do since the file is meant to exist across calls."""
    flags = os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW | (os.O_APPEND if append else os.O_TRUNC)
    try:
        return os.open(path, flags, mode)
    except OSError:
        return None


def _write_json_record(path, record, *, is_private):
    """Write `record` to `path` as indented JSON, overwriting any prior
    content — `meta.json`'s own shape, used at both SessionStart (fresh) and
    Stop (heartbeat refresh).

    `is_private` picks `_open_write_nofollow` over a plain `open(path, "w")`:
    same O_NOFOLLOW reasoning as `_ensure_hook_log_gitignore`, for the one
    write this module makes under a private crew's own `agent-runs` tree
    rather than `hook-log`."""
    data = json.dumps(record, indent=2, default=str).encode("utf-8")
    if not is_private:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(data.decode("utf-8"))
        return
    fd = _open_write_nofollow(path)
    if fd is None:
        return
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def _read_json_record(path, *, is_private):
    """Read `path` as a JSON object, or None on any failure (missing,
    unreadable, not JSON, not an object) — `meta.json`'s own shape, read
    back by Stop to refresh the heartbeat without rewriting identity.

    `is_private` opens with `O_NOFOLLOW`: the symlink this guards against is
    the same one `_write_json_record`'s own write-back already refuses to
    follow at this exact path, so a read with no such guard would still let
    the file's CONTENT (not just the write) be read through a symlink
    planted at the final component."""
    try:
        if is_private:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            fh = os.fdopen(fd, "r", encoding="utf-8")
        else:
            fh = open(path, encoding="utf-8")
        with fh:
            data = json.load(fh)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _ensure_hook_log_gitignore(path):
    """Write `<path>/.gitignore` containing `*`, so a black-box crew's own
    `git add -A` cannot stage the hook log `main()` is about to write into
    (the same gap telemetry._ensure_root closes for `<crew home>/agent-runs`,
    one directory over). Runs on every hook event, so it has to be cheap
    once the file exists (a single failing `O_EXCL` open attempt) and must
    never raise: a failed write here must not fail the hook (see the
    module's "ALWAYS exit 0" contract above).

    Written unconditionally rather than only at `os.makedirs`'s creation,
    since `hook-log/` can already exist from before this fix shipped.

    `_open_create_only`, not `exists()` + `open(path, "w")`: the caller has
    already confirmed `path` itself is a real directory (`_ensure_dir_or_bail`),
    but `.gitignore` under it could itself be a symlink the crew's own
    sandboxed dispatch planted — `O_EXCL|O_NOFOLLOW` refuses to follow it,
    dangling or not, rather than writing `*\\n` through it."""
    fd = _open_create_only(os.path.join(path, ".gitignore"))
    if fd is None:
        return
    try:
        os.write(fd, b"*\n")
    finally:
        os.close(fd)


def _tmux_session_name(pane):
    """The tmux session that owns `pane`, or None when it cannot be named.

    `co restore` needs this to resume a crew under its own name. A name outside
    `_TMUX_NAME_RE` is refused, so the field is omitted rather than written.
    """
    try:
        done = subprocess.run(["tmux", "display-message", "-p", "-t", pane, "#S"],
                              capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return None
    name = done.stdout.strip() if done.returncode == 0 else ""
    return name if _TMUX_NAME_RE.fullmatch(name) else None


def _co_record(payload, event, now, runs_root, redact=False, is_private=False):
    """Write a co-session identity record (Deck WS1.2).

    `<runs_root>/<local-day>/co-<session-uuid>/meta.json` — cwd, product, model,
    pid, transcript path, tmux session — written at SessionStart, heartbeat
    refreshed on Stop. `tmux_session` is set only when the session runs in a tmux
    pane; outside one the key is absent.
    This closes the identity half of the gap `cost_join`'s docstring names: an
    interactive co session has no first-class record, only a transcript.

    `runs_root` is `RUNS_ROOT` for an ordinary session, or a black box crew's own
    directory when `main()` has determined this session belongs to one — this
    record carries `cwd`, `product`, and `transcript_path`, which is exactly the
    identifying and content-pointing information ADR-0043 keeps out of the shared
    dashboard surface, so it follows the same redirect as the primary write
    rather than a separate rule (security-reviewer, PR #413, run-73e19889).

    `redact` is `True` only when `main()` sent this record to `RUNS_ROOT` because
    `private_home` was `_UNKNOWN` — cannot tell, not "confirmed not private". On
    that path the directory redirect alone is not enough: `_strip_content` already
    blanks the primary write's content fields on the identical `_UNKNOWN` case, and
    `cwd`/`product`/`transcript_path` here are exactly as identifying, so they get
    the same redaction rather than landing verbatim in the shared root (reviewer,
    PR #413, run-c7ea4a4b). `redact` must NOT fire when `private_home` is `None`
    (registry read fine, genuinely not a private crew) — that record's identity
    fields are legitimately shared-root content.

    `is_private` is `True` only on that same redirected-to-the-crew's-own-directory
    path — i.e. `runs_root` is `<crew home>/agent-runs`, a name that crew's own
    sandboxed dispatch could already have replaced with a symlink before this
    unsandboxed hook ever ran (same reasoning as `main()`'s hook-log write,
    above). On that path every directory this function touches is confirmed a
    real, non-symlink directory before anything is written under it OR read
    through it: SessionStart creates-or-refuses `runs_root`, the day
    directory and `d` (`_ensure_dir_or_bail`); Stop creates neither, but
    refuses to use the day/`co-<sid>` directories naming its own meta.json
    guess, or any glob-fallback candidate, when either is a symlink rather
    than a real directory (`_is_safe_co_meta_path`) — the day directory is
    exactly as predictable as `runs_root` itself, and the midnight-bug glob
    below has no filter of its own. `meta.json` itself is both read and
    written with `O_NOFOLLOW` (`_read_json_record`/`_open_write_nofollow`)
    rather than a plain `open(path, ...)`. A failed check skips the write
    (or the read); it never falls back to `RUNS_ROOT` (ARCH-322 round 3).

    IDENTITY AND LIVENESS ONLY. Cost stays computed on demand from the transcript
    by `cost_join.co_main_loop_node()`. Two representations of one session's spend
    would be a reconciliation problem nobody asked for.

    **A `co-*` dir is NEVER a corpus node.** `cost_join.index_nodes()` loads only
    `run-`/`sub-` prefixed dirs, and that NAME FILTER is the whole enforcement.
    Until this WS, `load_node()`'s docstring credited the skip to meta-absence —
    which this record makes false, since the dir now carries a meta.json and
    `load_node()` called on it directly WOULD return a node. Relaxing the prefix
    filter would double-count every interactive session.

    The day key is LOCAL, matching the run-dir convention (ARCH-156) rather than
    the hook-log's UTC — this dir lives in the run-dir tree, so it follows the
    run-dir clock.
    """
    sid = _safe_name(payload.get("session_id"), "")
    if not sid:
        return
    day = now.astimezone().strftime("%Y-%m-%d")
    d = os.path.join(runs_root, day, f"co-{sid}")
    meta_path = os.path.join(d, "meta.json")

    if event != "SessionStart":
        # A Stop whose own home has already been replaced with a symlink (the
        # same crew's own earlier, sandboxed dispatch) must not search or read
        # through it — same posture as SessionStart's own check below.
        if is_private and not _is_real_dir(runs_root):
            return
        # The direct guess's day directory is exactly as predictable as
        # `runs_root` itself (today's local date) — SessionStart already
        # refuses to create under a symlinked one, so Stop refuses to read
        # or write through one too, rather than silently falling through to
        # the glob below (ARCH-322 round 3).
        if is_private and os.path.exists(meta_path) and not _is_safe_co_meta_path(meta_path):
            return
        # THE MIDNIGHT BUG THIS AVOIDS. Computing the day at Stop time points at
        # TODAY's tree, but a session that began yesterday has its record filed
        # under yesterday — so the lookup missed, returned silently, and the
        # heartbeat froze at the SessionStart timestamp. A liveness consumer would
        # then read a long-running session as one that never checked in. Sessions
        # here routinely span midnight, so this was the common case, not an edge.
        # Find the record instead of predicting where it is.
        if not os.path.exists(meta_path):
            found = sorted(glob.glob(os.path.join(runs_root, "*", f"co-{sid}", "meta.json")))
            if is_private:
                # The glob has no idea which matched day segment is real and
                # which is a symlink a crew's own earlier dispatch planted —
                # filter exactly as the direct-path check above refuses one.
                found = [p for p in found if _is_safe_co_meta_path(p)]
            if not found:
                return
            meta_path = found[-1]

    if event == "SessionStart":
        # The runs tree holds prompts/user.md, transcript.jsonl, and now this
        # record's transcript_path. The hook-log tree already suppresses Spotlight
        # for exactly this reason — the indexer runs as root and copies content
        # into an index outside the file mode's protection — and the same threat
        # applies here. Also pin 0700 explicitly: the tree is owner-only today
        # only because this script happened to create it under the umask, which
        # makes the invariant depend on creation order rather than intent.
        if is_private:
            if not _ensure_dir_or_bail(runs_root):
                return
        else:
            try:
                os.makedirs(runs_root, exist_ok=True)
            except OSError:
                return
        try:
            os.chmod(runs_root, 0o700)
        except OSError:
            pass
        runs_marker = os.path.join(runs_root, ".metadata_never_index")
        if not os.path.exists(runs_marker):
            if is_private:
                marker_fd = _open_create_only(runs_marker)
                if marker_fd is not None:
                    os.close(marker_fd)
            else:
                open(runs_marker, "a").close()

        if is_private:
            # `d` is `runs_root/day/co-sid`; the day component is as
            # predictable and plantable as `runs_root` itself, so it gets
            # the same real-directory check before `d` is created under it.
            day_dir = os.path.join(runs_root, day)
            if not _ensure_dir_or_bail(day_dir):
                return
            if not _ensure_dir_or_bail(d):
                return
        else:
            try:
                os.makedirs(d, exist_ok=True)
            except OSError:
                return
        if redact:
            cwd_val = "[redacted: crew privacy could not be determined]"
            product_val = "[redacted: crew privacy could not be determined]"
            transcript_val = "[redacted: crew privacy could not be determined]"
        else:
            cwd_val = str(payload.get("cwd") or "")[:MAX_FIELD]
            product_val = os.path.basename(str(payload.get("cwd") or ""))[:MAX_FIELD]
            transcript_val = str(payload.get("transcript_path") or "")[:MAX_FIELD]
        meta = {
            "kind": "co-session",
            "session_id": sid,
            "started_at": now.isoformat(),
            "heartbeat_at": now.isoformat(),
            "cwd": cwd_val,
            "product": product_val,
            "model": str(payload.get("model") or "")[:MAX_FIELD],
            "transcript_path": transcript_val,
            "ppid": os.getppid(),
        }
        pane = os.environ.get("TMUX_PANE")
        if pane:
            # Redacted like cwd on the unknown-crew path. The tmux call is skipped there.
            if redact:
                meta["tmux_session"] = "[redacted: crew privacy could not be determined]"
            else:
                tmux_val = _tmux_session_name(pane)
                if tmux_val is not None:
                    meta["tmux_session"] = tmux_val
        _write_json_record(meta_path, meta, is_private=is_private)
        return

    # Stop: refresh liveness without rewriting identity. A Stop for a session that
    # started before this emitter existed has no record to update, and creating a
    # partial one from a Stop payload would invent a start time it does not know.
    meta = _read_json_record(meta_path, is_private=is_private)
    if meta is None:
        return
    meta["heartbeat_at"] = now.isoformat()
    _write_json_record(meta_path, meta, is_private=is_private)


def _write_hook_log(root, is_private, day, session_id, line):
    """Create `<root>/<day>/<session_id>.jsonl` and append `line`, writing
    `<root>/.gitignore` and the Spotlight-exclusion marker along the way.

    For a private crew's own hook-log (`is_private`), `root` and the day
    directory are each confirmed real, non-symlink directories
    (`_ensure_dir_or_bail`) before anything is written under them, and the
    `.gitignore`/jsonl writes refuse to follow a symlink at the final path
    component (`_open_create_only`/`_open_write_nofollow`). This hook runs
    UNSANDBOXED (module docstring, "black box crews"), so a symlink a
    dispatched run of the SAME crew planted — within its own sandboxed
    write grant, since this tree lives inside that crew's own worktree —
    at either directory, or at one of these files, would otherwise be
    followed, landing this crew's content wherever the symlink points. A
    failed check skips the write outright: it never falls back to the
    shared log root, which would land a black-box crew's content in
    exactly the place ADR-0043 keeps it out of.

    The shared-root path (`is_private=False`) is unchanged from before this
    hardening: `LOG_ROOT` sits outside every tree a dispatch can write to,
    so nothing here can plant a symlink under it."""
    if is_private:
        if not _ensure_dir_or_bail(root):
            return
        _ensure_hook_log_gitignore(root)
    else:
        try:
            os.makedirs(root, exist_ok=True)
        except OSError:
            return

    # Spotlight indexes as root regardless of the 0600 file mode, which would copy
    # prompt text into an index outside the protection. Written into whichever root
    # this record landed in, since a private crew's own directory needs it at least
    # as much as the shared one.
    marker = os.path.join(root, ".metadata_never_index")
    if not os.path.exists(marker):
        if is_private:
            marker_fd = _open_create_only(marker)
            if marker_fd is not None:
                os.close(marker_fd)
        else:
            open(marker, "a").close()

    log_dir = os.path.join(root, day)
    if is_private:
        if not _ensure_dir_or_bail(log_dir):
            return
    else:
        try:
            os.makedirs(log_dir, exist_ok=True)
        except OSError:
            return

    jsonl_path = os.path.join(log_dir, f"{session_id}.jsonl")
    if is_private:
        fd = _open_write_nofollow(jsonl_path, append=True)
        if fd is None:
            return
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    else:
        with open(jsonl_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def main():
    # `UserPromptSubmit` payloads carry the user's full typed prompt, and tool
    # inputs can carry anything a command line can. Create the tree at 0700 and
    # files at 0600 rather than inheriting the 022 umask, so the log's
    # confidentiality does not rest solely on the parent `agent-runs/` mode.
    os.umask(0o077)

    # FOR TESTS ONLY: `--root-config FILE` after the event name points the
    # black-box routing at another machine root, as co's own flag does. An
    # explicit argument, never an environment variable (company/decisions/0008);
    # the harness never passes it. If it is given and cannot be honoured, every
    # privacy answer is UNKNOWN, so content is stripped rather than misrouted.
    # "Cannot be honoured" means any config root() would not take at its word.
    # root() never raises: a missing file, a missing or empty CREWS_ROOT, a
    # relative value, one that is not a directory, and one too wide to be a root
    # all come back as its `fallback`, which defaults to the machine's real root.
    # So a sentinel is passed as the fallback and refused by identity, which
    # covers every one of those paths at once rather than one named case at a
    # time (security-reviewer and reviewer, #545 rounds 1 and 2).
    global _private_home_for
    if "--root-config" in sys.argv[2:]:
        try:
            from pathlib import Path as _P
            import builder_root as _BRC
            import projects as _PJ
            _cfg = _P(sys.argv[sys.argv.index("--root-config", 2) + 1])
            _unhonoured = object()
            _root = _BRC.root(config=_cfg, fallback=_unhonoured)
            if _root is _unhonoured:
                raise ValueError(f"{_cfg} names no root this machine will take")
            _PJ.ROOT_OVERRIDE = _root
        except BaseException:
            def _private_home_for(cwd):  # noqa: ARG001, F811
                return _UNKNOWN

    raw = sys.stdin.read()

    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            payload = {"_unparsed": raw}
    except Exception:
        payload = {"_unparsed": raw}

    session_id = _safe_name(payload.get("session_id"), "unknown-session")
    event = payload.get("hook_event_name")
    if not isinstance(event, str):
        # Fall back to the event name passed as argv[1] by the settings.json
        # registration, so a payload missing the field still lands labeled.
        event = sys.argv[1] if len(sys.argv) > 1 else "unknown-event"

    now = datetime.datetime.now(datetime.timezone.utc)
    record = {
        "ts": now.isoformat(),
        # Capped here rather than in the truncation branch below, because the
        # branch only rebuilds `payload`. Any field living outside `payload` has
        # to be bounded at construction or it escapes the ceiling entirely.
        "event": event[:MAX_FIELD],
        "ppid": os.getppid(),
        "env": {k: os.environ[k][:MAX_FIELD] for k in ENV_KEYS if k in os.environ},
        "payload": payload,
    }

    line = json.dumps(record, ensure_ascii=False, default=str)
    if len(line) > MAX_LINE:
        # Every field carried into the replacement is itself capped. Copying a
        # field through unbounded would leave the ceiling nominal: a payload
        # with a 200KB `cwd` would produce a 200KB "truncated" line.
        record["payload"] = {
            "_truncated": True,
            "_original_bytes": len(line),
            "session_id": str(payload.get("session_id"))[:MAX_FIELD],
            "cwd": str(payload.get("cwd"))[:MAX_FIELD],
            "tool_name": str(payload.get("tool_name"))[:MAX_FIELD],
        }
        line = json.dumps(record, ensure_ascii=False, default=str)

    # UTC for the directory as well as the timestamp. Mixing local and UTC would
    # file a record logged just after local midnight under tomorrow's directory
    # while its `ts` still read yesterday.
    day = now.strftime("%Y-%m-%d")

    # Spotlight's indexer runs as root and content-indexes this tree regardless
    # of the 0600 file mode, which would copy prompt text into an index outside
    # the protection. The marker file opts the subtree out.
    # WHERE THIS RECORD GOES is decided here, before anything is written, from the
    # session's own directory. A black box crew's record is written inside that
    # crew rather than into the shared root (ADR-0043, and the operator's ledger-split rule:
    # the detailed record belongs to the crew, the accounting fact to the platform).
    private_home = _private_home_for(payload.get("cwd"))
    if private_home is _UNKNOWN:
        # THE REGISTRY COULD NOT BE READ, so this session's crew is unknown, and an
        # unknown crew must not be assumed public. ADR-0043 is explicit that a
        # privacy control must not be the thing that fails open.
        #
        # Failing closed by writing NOTHING would silently stop instrumentation for
        # the whole fleet on an unreadable registry, which trades a privacy failure
        # for an availability one. So the event still lands and its CONTENT does
        # not: the shape of the session survives, the words do not.
        record["payload"] = _strip_content(record.get("payload") or {})
        record["_privacy"] = "registry-unreadable: content stripped"
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        if len(line) > MAX_LINE:
            line = line[:MAX_LINE] + '"}'
        root = LOG_ROOT
        is_private_hook_log = False
    elif private_home:
        root = os.path.join(private_home, "hook-log")
        is_private_hook_log = True
    else:
        root = LOG_ROOT
        is_private_hook_log = False

    _write_hook_log(root, is_private_hook_log, day, session_id, line)

    # Last, and guarded separately: the spool append is the emitter's primary job
    # and must not be lost to a failure in the identity record.
    #
    # SAME REDIRECT AS THE PRIMARY WRITE, reusing `private_home` rather than
    # recomputing it: this record's cwd/product/transcript_path are exactly the
    # identifying, content-pointing fields ADR-0043 keeps off the shared dashboard
    # surface, so a black box crew's identity record is written under the crew's
    # own directory instead of the shared `RUNS_ROOT`. `_UNKNOWN` takes the same
    # DIRECTORY as "not private" here, exactly as it does for the primary write's
    # `root` choice above — but unlike the primary write, whose CONTENT only
    # branches on `_UNKNOWN` because `_strip_content` runs before `root` is even
    # chosen, this record's identity fields are written by `_co_record` itself, so
    # the `_UNKNOWN`-vs-`None` distinction has to be threaded through explicitly
    # rather than falling out of which directory was picked (reviewer, PR #413,
    # run-c7ea4a4b).
    if event in ("SessionStart", "Stop"):
        is_unknown = private_home is _UNKNOWN
        co_runs_root = (
            os.path.join(private_home, "agent-runs")
            if private_home and not is_unknown
            else RUNS_ROOT
        )
        try:
            _co_record(payload, event, now, co_runs_root, redact=is_unknown,
                      is_private=bool(private_home and not is_unknown))
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        # BaseException, not Exception: KeyboardInterrupt and SystemExit derive
        # only from BaseException, so catching Exception would let a SIGINT
        # delivered to the foreground process group exit 130 — which from a Stop
        # hook is exactly the wedge this contract exists to prevent.
        pass
    sys.exit(0)
