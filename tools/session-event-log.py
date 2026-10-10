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
# Measured 2026-08-18 before the fix: 14 CFO sessions holding 147,005 characters of
# verbatim prompt and assistant text, and 27 for ask.
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
# which is how CFO's hook log went unreadable for three days
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


def _co_record(payload, event, now, runs_root, redact=False):
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
        # THE MIDNIGHT BUG THIS AVOIDS. Computing the day at Stop time points at
        # TODAY's tree, but a session that began yesterday has its record filed
        # under yesterday — so the lookup missed, returned silently, and the
        # heartbeat froze at the SessionStart timestamp. A liveness consumer would
        # then read a long-running session as one that never checked in. Sessions
        # here routinely span midnight, so this was the common case, not an edge.
        # Find the record instead of predicting where it is.
        if not os.path.exists(meta_path):
            found = sorted(glob.glob(os.path.join(runs_root, "*", f"co-{sid}", "meta.json")))
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
        os.makedirs(runs_root, exist_ok=True)
        try:
            os.chmod(runs_root, 0o700)
        except OSError:
            pass
        runs_marker = os.path.join(runs_root, ".metadata_never_index")
        if not os.path.exists(runs_marker):
            open(runs_marker, "a").close()

        os.makedirs(d, exist_ok=True)
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
        with open(meta_path, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2, default=str)
        return

    # Stop: refresh liveness without rewriting identity. A Stop for a session that
    # started before this emitter existed has no record to update, and creating a
    # partial one from a Stop payload would invent a start time it does not know.
    try:
        with open(meta_path, encoding="utf-8") as fh:
            meta = json.load(fh)
        if not isinstance(meta, dict):
            return
    except Exception:
        return
    meta["heartbeat_at"] = now.isoformat()
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, default=str)


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
    elif private_home:
        root = os.path.join(private_home, "hook-log")
    else:
        root = LOG_ROOT

    os.makedirs(root, exist_ok=True)
    # Spotlight indexes as root regardless of the 0600 file mode, which would copy
    # prompt text into an index outside the protection. Written into whichever root
    # this record landed in, since a private crew's own directory needs it at least
    # as much as the shared one.
    marker = os.path.join(root, ".metadata_never_index")
    if not os.path.exists(marker):
        open(marker, "a").close()

    log_dir = os.path.join(root, day)
    os.makedirs(log_dir, exist_ok=True)

    with open(os.path.join(log_dir, f"{session_id}.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(line + "\n")

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
            _co_record(payload, event, now, co_runs_root, redact=is_unknown)
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
