#!/usr/bin/python3
"""render-staleness-context — a LIVE co session is told when its own
instruction render has fallen behind the knowledge base (issue_
01M4GJRCDNKCV4QREH68KSRS3W slice 2).

Slice 1 (`bin/knowledge-ctx check`, `bin/co`'s `_co_refresh_if_stale`) fixes a
session at LAUNCH: a plain `co` now re-renders when the on-disk profile is
behind. That does nothing for a session already running — it keeps the
`CLAUDE.md` it loaded at start for its whole life, which on this platform is
often days. Two named occurrences (the issue body): vivy-commercial's live
session (started 2026-10-06) cited a review-round rule a 2026-10-05 decision
had replaced, from a render dated 2026-09-27; and the steward re-rendered
four workspaces by hand with `knowledge-ctx use <profile> --force` while
their sessions stayed live, so the ON-DISK render went current while each
running session still held the old text — exactly the case the workspace's
own `.profile-documents.json` can never catch, because re-reading it just
reads the new render.

THE BASELINE IS WHAT THIS SESSION LOADED, NOT WHAT IS ON DISK. On this
hook's first firing for a session, it copies the workspace's
`.claude/.profile-documents.json` into this session's own state (a launch
renders before Claude starts, so the hook's first firing is the earliest
this process can read what that render was). Every later firing runs
`knowledge-ctx check --record <that copy>`, which diffs the CURRENT
Library/profile resolution against the baseline — never against the
workspace's own, possibly since-updated, manifest. That is the one change
`bin/knowledge-ctx check` needed for this (the `--record` option): with no
`--record`, its behaviour and slice 1's tests are unchanged.

WORKSPACE RESOLUTION is the payload's `cwd`. A `cwd` with no
`.claude/.active-profile` is not a `knowledge-ctx` workspace: this says
nothing and writes no state, the same as a crew session `bin/co` never
individuated.

`knowledge-ctx` IS RUN BY BARE NAME ON `$PATH`, with a 2s timeout, no shell
— the same way `bin/co`'s own `_co_refresh_if_stale` calls it
(`knowledge-ctx check && rc=0 || rc=$?`). Not a path resolved relative to
this file: this hook is deployed into `~/.local/tools/` the same way
`bin/knowledge-ctx` is deployed into `~/.local/bin/`
(`install.sh`), and `~/.local/bin` is already on every interactive
session's `$PATH` — the same assumption `bin/co` makes.

OUTPUT IS TREATED AS DATA, NEVER AS TEXT TO FORWARD VERBATIM. `check`'s
stdout lines are kept only when they match one of the exact shapes it
prints for a changed document or instruction block (`_LINE_RE` below); a
line matching neither — truncated output, a future format change, anything
else — is dropped rather than injected. The ids in a kept line come from
the knowledge repo and reach a model's context from here, same posture as
`knowledge-ctx check`'s own `_escape_for_check_output` escaping its control
characters before they ever reach this far.

Injects only the CHANGED INSTRUCTION BLOCKS by id (capped; see
`_MAX_BLOCK_LINES`) plus a COUNT of other documents added or removed — never
the document ids themselves, which this slice has no occurrence motivating
(an agent/skill joining or leaving a profile is a renaming/housekeeping
change a session does not need to read live; a REPLACED instruction block,
the vivy-commercial occurrence, is the one a session could act on wrongly).

INJECTS ONCE PER DISTINCT DIFFERENCE. A fingerprint of the kept lines is
stored per session (same cache dir `co-session-health` already uses for
vitals and rollover state); a firing whose fingerprint matches the one last
injected says nothing, so a long-lived session is not told the same thing
on every turn. A firing that reads current (`check` exit 0) clears the
stored fingerprint, so a later re-render that goes stale again is reported
again rather than staying suppressed by a fingerprint from before it was
fixed.

THE BASELINE AND FINGERPRINT FILES STAY FRESH FOR AS LONG AS THE SESSION
FIRES (round 2 finding, PR #839 review). `co-session-health`'s
`_gc_stale_caches` sweeps `render-staleness-*` by mtime on a 3-day cutoff —
the same shape as the vitals sidecar it was built for, where a LIVE session
rewrites its own file every render so only a dead one goes stale. This
hook's baseline is written once, at the session's first firing, and never
rewritten (by design: THE BASELINE IS WHAT THIS SESSION LOADED, above) —
so without something else keeping its mtime current, a session that runs
longer than the GC cutoff, which this docstring already calls the normal
case, loses its own baseline while still alive. Its next firing then
re-baselines against whatever is on disk at that moment and silently stops
reporting the drift this whole hook exists to report. Every firing that
reaches past the three skips and the workspace check therefore refreshes
(`os.utime`, never a rewrite of the content) both the baseline and the
fingerprint-state file, before either is read or `_ensure_baseline` runs —
so a session alive long enough to fire again always counts as "live" to the
sweep, and only a session that has stopped firing altogether goes stale.

SAFETY CONTRACT, same as `tools/rollover-trigger.py` and
`tools/usage-window-context.py`:
  1. ALWAYS exit 0.
  2. Fail open on anything unreadable: no `cwd` in the payload, no active
     profile, an unreadable manifest, a `knowledge-ctx` that times out, is
     missing from `$PATH`, or exits with the distinct "cannot tell" code —
     every one of those is a reason to say nothing, never a reason to block
     or crash. `check`'s own exit codes already distinguish "current" (0)
     from "behind" (1) from "cannot tell" (its own `CHECK_CANNOT_TELL`,
     currently 3); only "behind" is ever worth injecting, and this hook
     treats any OTHER code, including a future one, the same as "cannot
     tell" — silence, not a guess.
  3. Skip a session this hook must not act on, before touching any state: a
     dispatched worker (`AGENTCTL_RUN_ID` set), a session outside tmux, and
     a subagent's own tool call reaching this hook through the parent
     session's stream (`agent_id` present) — the same three skips
     `tools/rollover-trigger.py` and `tools/usage-window-context.py` apply,
     for the same reasons (see either one's own docstring).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Matches `rollover_policy.CACHE_DIR` / `usage-window-context.py`'s own
# `CACHE_DIR` — the same cache directory co-session-health already writes
# vitals and rollover state into.
CACHE_DIR = Path.home() / ".cache" / "co-session-health"

# `bin/knowledge-ctx`'s own `_PROFILE_DOCUMENTS_NAME` — duplicated as a
# literal rather than imported, since this hook must never depend on
# `knowledge-ctx` being importable (it is a hyphenated `bin/` CLI, not a
# library module) to satisfy its SAFETY CONTRACT.
_PROFILE_DOCUMENTS_NAME = ".profile-documents.json"

# Bound via `timeout=` on the `subprocess.run` call below, never a signal
# sent after the fact — a `check` that runs long says nothing this firing
# rather than blocking the prompt it is attached to.
CHECK_TIMEOUT_SECONDS = 2.0

# Cap on the changed-instruction-block lines shown; beyond this, "and N
# more" rather than a context block that grows without bound.
_MAX_BLOCK_LINES = 15

_SESSION_ID_RE = re.compile(r"[A-Za-z0-9_-]+")

# The exact two shapes `cmd_check` in `bin/knowledge-ctx` prints (module
# comment there, above `CHECK_CANNOT_TELL`): a document only ever "+"/"-"
# (id set membership alone), an instruction block "+"/"-"/"~" (membership or
# a changed `body_hash`). The protection is THIS REGEX'S OWN anchoring and id
# charset (`[A-Za-z0-9/_.-]+`): a line whose id carries a space or any other
# character outside that charset fails the match and is dropped, regardless
# of what `_escape_for_check_output` does or doesn't escape — that function
# only escapes control characters, not a plain space, so it is not what
# keeps the literal " document "/" instruction-block " tokens below from
# being mistaken for part of an id.
_LINE_RE = re.compile(
    r"^(?:[+-] (?P<doc>document) [A-Za-z0-9/_.-]+"
    r"|[+\-~] (?P<block>instruction-block) [A-Za-z0-9/_.-]+)$"
)


def _is_block_line(line: str) -> bool:
    """`True` for a kept `instruction-block` line, `False` for a kept
    `document` line — by the matched TYPE TOKEN (`_LINE_RE`'s own `block`
    group), never by a substring test on the line, which would also match a
    `document` line whose id happens to contain the text `instruction-block`."""
    match = _LINE_RE.match(line)
    return bool(match and match.group("block"))


def _read_payload() -> dict:
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    try:
        payload = json.loads(raw)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _session_key(session_id: str) -> str:
    return "".join(_SESSION_ID_RE.findall(session_id))[:64]


def _baseline_path(key: str) -> Path:
    return CACHE_DIR / f"render-staleness-{key}-baseline.json"


def _state_path(key: str) -> Path:
    return CACHE_DIR / f"render-staleness-{key}.json"


def _read_state(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(path: Path, state: dict) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(state))
        os.replace(tmp, path)
    except OSError:
        pass


def _ensure_baseline(baseline_path: Path, manifest_path: Path) -> bool:
    """`True` once *baseline_path* holds a copy of *manifest_path* — either
    already did, or was just captured. `False` on anything unreadable;
    captures nothing in that case, so the NEXT firing tries again rather
    than this one locking in an empty or partial baseline."""
    if baseline_path.exists():
        return True
    try:
        data = manifest_path.read_bytes()
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = baseline_path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, baseline_path)
    except OSError:
        return False
    return True


def _touch(path: Path) -> None:
    """Refresh *path*'s mtime to now, content untouched. A no-op if *path*
    does not exist yet — this only keeps an already-written file from
    reading as dead to `_gc_stale_caches`'s sweep; it never creates one."""
    try:
        os.utime(path, None)
    except OSError:
        pass


def _kept_lines(stdout: str) -> list[str]:
    return [line for line in stdout.splitlines() if _LINE_RE.match(line)]


def _compose_text(block_lines: list[str], doc_count: int) -> str:
    lines = [
        "The instructions this session loaded at launch are behind the "
        "knowledge base.",
    ]
    if block_lines:
        lines.append("Changed instruction blocks:")
        shown = block_lines[:_MAX_BLOCK_LINES]
        lines.extend(f"  {line}" for line in shown)
        remaining = len(block_lines) - len(shown)
        if remaining > 0:
            lines.append(f"  and {remaining} more")
    if doc_count:
        plural = "" if doc_count == 1 else "s"
        lines.append(f"{doc_count} other document{plural} added or removed.")
    lines.append(
        "Before acting on a rule one of these blocks covers, read its "
        "current text with kb_get rather than trusting what this session "
        "loaded. Tell the operator that a /rollover would load the "
        "current render.")
    return "\n".join(lines)


def _emit_context(event_name: str, text: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {"hookEventName": event_name, "additionalContext": text},
    }))


def main() -> int:
    payload = _read_payload()
    event = payload.get("hook_event_name")
    if not isinstance(event, str):
        event = sys.argv[1] if len(sys.argv) > 1 else ""
    if event != "UserPromptSubmit":
        return 0

    if os.environ.get("AGENTCTL_RUN_ID"):
        return 0
    if not os.environ.get("TMUX"):
        return 0
    if payload.get("agent_id"):
        return 0

    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return 0
    key = _session_key(session_id)
    if not key:
        return 0

    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return 0
    workspace = Path(cwd)
    claude_dir = workspace / ".claude"
    if not (claude_dir / ".active-profile").is_file():
        return 0  # not a knowledge-ctx workspace

    baseline_path = _baseline_path(key)
    state_path = _state_path(key)
    # Keep this session's own sidecars reading as live to the sweep for as
    # long as it keeps firing — content untouched (see the docstring's own
    # THE BASELINE AND FINGERPRINT FILES STAY FRESH section above).
    _touch(baseline_path)
    _touch(state_path)

    manifest_path = claude_dir / _PROFILE_DOCUMENTS_NAME
    if not _ensure_baseline(baseline_path, manifest_path):
        return 0

    try:
        proc = subprocess.run(
            ["knowledge-ctx", "check", "--record", str(baseline_path)],
            cwd=str(workspace), capture_output=True, text=True,
            timeout=CHECK_TIMEOUT_SECONDS,
        )
    except Exception:
        return 0

    if proc.returncode == 0:
        # Current again: clear the stored fingerprint so a LATER re-render
        # that goes stale again is reported again, not suppressed by a
        # fingerprint left over from before this one was fixed.
        _write_state(state_path, {})
        return 0
    if proc.returncode != 1:
        return 0  # cannot tell: say nothing, and leave any prior state alone

    kept = _kept_lines(proc.stdout)
    if not kept:
        return 0  # nothing safe to report, even though `check` said "behind"

    fingerprint = hashlib.sha256("\n".join(kept).encode()).hexdigest()
    state = _read_state(state_path)
    if state.get("last_fingerprint") == fingerprint:
        return 0

    block_lines = [line for line in kept if _is_block_line(line)]
    doc_count = len(kept) - len(block_lines)

    _write_state(state_path, {"last_fingerprint": fingerprint})
    _emit_context(event, _compose_text(block_lines, doc_count))
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except BaseException:
        rc = 0
    sys.exit(rc or 0)
