"""Render a Claude Code session as communication first (Deck WS1.5).

THE FRICTION THIS ANSWERS. The operator, 2026-07-19, watching a co session scroll: "how
*verbose* the session output is ... collapse every tool call and every update into
expandable one line, and only show intentional communication full-length. No
wonder I miss so much."

So: tool calls and their results collapse to one line each. What a human typed and
what the model said back render at full length. Reasoning is present but closed.

THE RULE IS MECHANICAL, AND THAT IS THE POINT. Classification falls out of the
transcript's own record types (see the KB contract
`agent-platform/contracts/claude-transcript-jsonl`), so this module carries **no
summarization vocabulary** — a collapsed line reads `Edit(session_view.py)`,
verbatim from the payload, never "editing the session view module." Paraphrase is
the editorial problem ([J3]) that makes the full conversation renderer (WS4.3)
expensive; refusing it here is what lets this ship in Phase 1 instead.

WHERE THIS LIVES. The plan has WS1.5 depending on WS1.4 for a host, since fleetd
already serves a page over the SSH forward. fleetd does not exist yet, and waiting
for it would mean building three workstreams before the operator gets the thing he asked
for. The classification is a pure function and the serving is incidental, so the
two are separated here: this module renders, `bin/session-view` serves it on
loopback today, and fleetd absorbs the same `render_session()` call unchanged when
WS1.4 lands.

DEGRADATION IS A REQUIREMENT, NOT A COURTESY. The transcript format is Anthropic's
and undocumented. An unknown record type renders as a collapsed unknown row; it is
never dropped and never raises. A reader who cannot see that something happened is
worse off than one who sees an unhelpful line.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import urllib.parse
from pathlib import Path
import builder_root as BR
import git_refs

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"

# THE QUEUE FOLD IS OPTIONAL, for the reason `session_input` already made
# `session_state` an optional import: `~/.local/lib` is a per-file symlink farm
# built by `install.sh`, so a module can be unlinked between a pull and the next
# install, and a hard import here would take the whole page renderer down for a
# feature that is allowed to just not show (#309's outage, told in full at
# `session_state`'s own import guard). No cycle exists to force the issue either
# way: this module imports nothing from `session_state` today, and `session_state`
# imports nothing from this one. `session_state` is ours, not a third-party
# module whose failure is worth a log line, so this mirrors `session_input`'s
# plain swallow rather than `session_state`'s stderr-logging variant.
try:
    import session_state as _SS  # noqa: E402
except Exception:  # noqa: BLE001 — any failure here means "no queue fold"
    _SS = None

# --- JavaScript renderer bridge -----------------------------------------------

class NodeMissingError(RuntimeError):
    """Raised when Node.js is required but cannot be found at any known path."""


# Fixed absolute paths to probe, after the glob below. PATH is not available
# over a non-interactive SSH session, so bare `node` is not reliable here.
_NODE_FIXED_CANDIDATES = [
    "/usr/local/bin/node",
    "/usr/bin/node",
]
# Set this env var to override the probed binary (useful for tests and custom installs).
_NODE_ENV_VAR = "SESSION_VIEW_NODE"


def _node_candidates() -> list[str]:
    """Absolute node locations to probe, in order.

    `/opt/*/bin/node` finds Homebrew's Apple Silicon install without naming
    "homebrew": Homebrew is the only thing this machine puts under `/opt` today,
    but a glob also matches any other `/opt/<pkg>` package manager that ships
    node there, and matches nothing at all on a machine (or Linux) with no such
    directory, rather than assuming one specific brand's layout.

    No `safe_path` filter here: this never reads PATH or any other
    agent-writable input, so there is nothing a dispatched agent's environment
    can redirect — `/opt` is root- or admin-owned on this machine today, same
    as the fixed candidates below.
    """
    opt_matches = sorted(str(p) for p in Path("/opt").glob("*/bin/node"))
    return opt_matches + _NODE_FIXED_CANDIDATES


_KNOWLEDGE_REPO = BR.root() / "knowledge"
_BRIDGE_SCRIPT = Path(__file__).parent / "md_render_bridge.js"
# A render is ~90ms on a real document, so this bounds a runaway Node rather
# than a slow one. It exists because the input is whatever file the user opened.
_BRIDGE_TIMEOUT_S = 30
# Distinct wikilink targets resolved per document. Past this the rest render as
# literal [[name]] text, which is exactly what an unresolvable target already
# does — so the degradation is one the reader has seen before, not a new mode.
# Set far above any real document: the heaviest in the KB corpus yields 7
# distinct candidates and none exceeds 100, so 500 is a runaway backstop rather
# than a limit real content can reach. That count is of raw `[[...]]` matches,
# including bash `[[ ... ]]` conditionals inside code fences — which is the
# right measure precisely because the scan below sees those too.
#
# WHAT IT BOUNDS DEPENDS ON WHETHER IT FIRES, and the common case is the one
# that is not bounded. Under the cap — every real document — the regex walks the
# whole source and tests each match against `seen`, so cost stays linear in
# document size: a 50 MB document of REPEATED links spends ~600ms here, all of it
# before the bridge starts and so outside _BRIDGE_TIMEOUT_S. Only once 500
# DISTINCT targets have been seen does the `break` below stop the scan, which is
# what removes the per-target index query measured at 11.3s for 100k distinct.
# So: the cap bounds queries always, and iteration only after it fires.
#
# Past the cap, links stop resolving SILENTLY — there is no reader-visible
# marker. That is deliberate for the unresolvable case, and slightly different
# when the cap is the cause: an author controlling a document's first 500
# targets could pad them to suppress a real link further down. It is a
# denial-of-linking rather than an escalation, and anyone able to do it already
# controls the document's content, so it is accepted rather than unnoticed.
_WIKILINK_RESOLVE_CAP = 500

# The class names FILE_VIEWER_CSS styles are emitted by render.js in the
# knowledge repo (`scripts/md-serial/render.js`), not by anything here. The
# coupling is cross-repo and silent: renaming a class there leaves this CSS
# matching nothing, and the page degrades to unstyled prose with no error.
# Named here so a change to either side can see what it is bound to:
#   md-doc, md-frontmatter, md-fm-field, md-fm-key, md-fm-value,
#   md-chip, md-html-comment, md-footnote-def, md-footnote-ref,
#   md-unsafe-link, and language-<lang> on <code>.

# Record `type` values this view HIDES. A display decision, not a structural one:
# the KB contract groups `system` and `attachment` with the conversation records
# (they carry the full envelope), and they are hidden here anyway because they are
# bookkeeping the reader never authored. Named for the decision it encodes so a
# future maintainer adding a type knows to consult display intent, not the
# contract's families. Listed explicitly so an UNKNOWN type stays distinguishable
# from a known-boring one and still surfaces.
HIDDEN_TYPES = frozenset({
    "last-prompt", "attachment", "mode", "permission-mode", "bridge-session",
    "ai-title", "pr-link", "queue-operation", "file-history-snapshot",
    "file-history-delta", "system",
})

# The argument that best identifies a call, per tool. First match wins; anything
# unlisted falls back to the first string value in the input. Verbatim, never
# paraphrased.
IDENTIFYING_ARG = (
    "file_path", "path", "pattern", "command", "url", "query", "prompt",
    "notebook_path", "skill", "subagent_type", "description",
)

FULL = "full"          # rendered at length — the human, or the model talking
COLLAPSED = "collapsed"  # one line, expandable
HIDDEN = "hidden"        # control-plane bookkeeping

MAX_ARG = 120

# Tools whose argument becomes a deck-open anchor in the rendered HTML.
# Fails closed: a name not on either list produces no anchor; the list grows later.
_PATH_TOOLS = frozenset({"Read", "Edit", "Write", "NotebookEdit"})
_KB_TOOLS = frozenset({
    # Short form (used in tests and edge cases).
    "kb_get", "kb_checkout", "kb_create", "kb_update", "kb_propose",
    # Prefixed form as the tool_use name appears in Claude Code transcripts.
    "mcp__kb__kb_get", "mcp__kb__kb_checkout", "mcp__kb__kb_create",
    "mcp__kb__kb_update", "mcp__kb__kb_propose",
})
# Checked in this order for path tools. This is NOT a prefix of IDENTIFYING_ARG:
# 'pattern', 'command', 'url', 'query', 'prompt' sit between 'path' and
# 'notebook_path' there. classify() assigns _identifying_arg's return value to
# tool_arg; for _PATH_TOOLS that equals full_ref[:MAX_ARG], for _LIST_PATH_TOOLS
# it adds a '+N' suffix on multi-file calls. Both derive from the anchor key (#346).
_PATH_TOOL_KEYS = ("file_path", "path", "notebook_path")

# Tools whose primary argument is a LIST of file paths. The value maps the tool
# name to the field that holds that list. The first string element becomes the
# anchor ref; a multi-file call links the first path only.
_LIST_PATH_TOOLS = {
    "SendUserFile": "files",
}

# The tags Claude Code wraps a slash command's own bookkeeping in. A user record
# whose content is one of these is the command SCAFFOLDING, not the sentence a
# person typed: running `/handoff-leave` records `<command-name>/handoff-leave
# </command-name>`, and the skill body it pulls in arrives as a separate `isMeta`
# record. Both the raw TUI and this renderer showed them, so running one command
# dumped a skill's entire instructions into the pane (the operator, 2026-07-24, on both
# sous-chef via Deck and hollis in the terminal). The TUI's display is
# Anthropic's; this is the half we own, and the pane is meant to read cleaner
# than the terminal, not the same.
_COMMAND_TAGS = (
    "<command-name>", "<command-message>", "<command-args>",
    "<local-command-stdout>", "<local-command-stderr>", "<local-command-caveat>",
)
_COMMAND_NAME_RE = re.compile(r"<command-name>\s*(/?[^<\s]+)")


def _slash_command(text: str) -> "str | None":
    """The command a user record is scaffolding for, or None if it is real prose.

    Recognised only when the content LEADS with a command tag, so a message that
    merely quotes `<command-name>` in the middle of a sentence is still treated
    as prose. The cost of a false positive is a collapsed row rather than a lost
    one — the body is kept and expandable — so this errs toward folding.

    Returns the command name for the chip (`/handoff-leave`), or a generic label
    when the record is command output carrying no name of its own.
    """
    stripped = text.strip()
    if not stripped.startswith("<") or not any(t in stripped for t in _COMMAND_TAGS):
        return None
    m = _COMMAND_NAME_RE.search(stripped)
    return m.group(1) if m else "command"


# A background task's completion notice, whose `<summary>` is the readable half.
_TASK_SUMMARY_RE = re.compile(r"<summary>(.*?)</summary>", re.S)

# Text the HARNESS wrote into a user-role record. Each is matched only at the
# START of the record, for the reason `_slash_command` gives: a message quoting
# one of these mid-sentence is a person talking about it, not the harness.
#
# KNOWN INCOMPLETE (agent-platform#382 security review, follow-up filed): these
# four prefixes matched 58.9% of a measured 21,133-enqueue sample; the other
# 41.1% is dominated by agentctl dispatch prompts (median 1,540 chars, max
# 192,068 — `agent-platform/contracts/claude-transcript-jsonl`), which carry no
# recognisable prefix and render as "you (queued)" the same as a typed message.
# This is reachable, not theoretical: `_index_html`
# (`bin/session-view`) lists every transcript under `CLAUDE_PROJECTS.glob("*/
# *.jsonl")`, including a dispatched agent's own worktree session, and links
# each to `/session/<sid>` → `render_session` → `held_rows_for`, so opening a
# dispatched agent's session through session-view's own loopback index renders
# its dispatch-prompt enqueues as the operator's. (Deck's tmux-pane path is narrower —
# `tmux_transcript.transcript_for_tmux_session` joins only `co-*/meta.json`,
# per `deck/specs/session-pane.md` — but the loopback index is not gated by
# that join.) Widening this past prefix-matching needs a heuristic validated
# against real `queue-operation` records, which requires pulling the corpus
# sample the review recommended; not done here since a length cutoff picked
# without that data would be exactly the unjustified guess the review warned
# against — a person can legitimately paste something long.
_HARNESS_PREFIXES = (
    "<task-notification>",
    "<system-reminder>",
    # The quote is part of the match, not decoration. The harness always names
    # the agent — `Background agent "Build B1 recording cooldown" was stopped` —
    # so requiring it keeps a person's own sentence starting "Background agent
    # stopped working, what happened?" reading as the prompt it is. Raised by
    # reviewer as the one false-positive surface here.
    'Background agent "',
    "Moving to background",
)


def _harness_event(text: str) -> "str | None":
    """A short label for a harness-written record, or None if a person wrote it.

    The label carries the useful sentence so the folded row still says what
    happened — "task finished · Dispatch reviewer on A1a PR 302" rather than a
    bare "event" the reader has to expand to understand. That is the whole
    difference between folding noise and hiding information.
    """
    stripped = text.strip()
    if not stripped.startswith(_HARNESS_PREFIXES):
        return None
    if stripped.startswith("<task-notification>"):
        m = _TASK_SUMMARY_RE.search(stripped)
        return f"task · {_one_line(m.group(1))}" if m else "task notification"
    if stripped.startswith("<system-reminder>"):
        return "system reminder"
    return _one_line(stripped)


def _one_line(text: str, limit: int = 80) -> str:
    """One tidy line for a label, truncated on a whole word where it can be."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    cut = flat[:limit].rsplit(" ", 1)[0]
    return f"{cut or flat[:limit]}…"


# A session id is a UUID. Anchoring to this alphabet is what keeps the id out of
# glob's metacharacter language below.
SESSION_ID_RE = re.compile(r"[A-Za-z0-9_-]+")


def transcript_path(session_id: str) -> Path | None:
    """The transcript for a session id, or None.

    Mirrors `cost_join._uuid_transcript`: the filename IS the session id, under a
    per-cwd project directory.

    THE INJECTION THIS BLOCKS. The id is interpolated into a glob PATTERN, not a
    path join, so `*`, `?`, and `[...]` are code rather than data: an id of `*`
    matched `*/*.jsonl` and returned somebody else's transcript. Traversal was
    never possible (the pattern is anchored under CLAUDE_PROJECTS) but reading an
    arbitrary session was. Validating here rather than only in the HTTP handler
    means every caller inherits the guard, including fleetd when it absorbs
    render_session().
    """
    if not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id):
        return None
    return next(iter(CLAUDE_PROJECTS.glob(f"*/{session_id}.jsonl")), None)


def _text_of(content) -> str:
    """Concatenated text of a message's content, whether str or block list."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    out = []
    for b in content:
        if isinstance(b, dict) and b.get("type") == "text":
            t = b.get("text")
            if isinstance(t, str):
                out.append(t)
    return "\n".join(out)


def _result_text(rec, msg) -> str:
    """The text of a tool result.

    THE BUG THIS FIXES. `_text_of` only collects blocks of type `text`, but a
    result arrives as a `tool_result` block — so every expanded result rendered
    EMPTY. The content of a tool_result is itself either a string or a list of
    text blocks, and `toolUseResult` carries the structured form (stdout/stderr)
    as a fallback for the shapes that have no block content.
    """
    parts = []
    content = msg.get("content")
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                inner = b.get("content")
                if isinstance(inner, str):
                    parts.append(inner)
                elif isinstance(inner, list):
                    for ib in inner:
                        # Non-empty, or an empty text block would count as
                        # "found content" and suppress the stdout fallback below,
                        # rendering blank when stdout had the answer.
                        if isinstance(ib, dict) and (ib.get("text") or "").strip():
                            parts.append(ib["text"])
    if not parts:
        tur = rec.get("toolUseResult")
        if isinstance(tur, str):
            parts.append(tur)
        elif isinstance(tur, dict):
            for key in ("stdout", "stderr"):
                v = tur.get(key)
                if isinstance(v, str) and v.strip():
                    parts.append(v)
    return "\n".join(p for p in parts if p).strip()


def _result_ids(msg) -> list[str]:
    """The tool_use_id(s) a result answers, for folding it into its call."""
    out = []
    content = msg.get("content")
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                tid = b.get("tool_use_id")
                if isinstance(tid, str):
                    out.append(tid)
    return out


def _is_error(msg) -> bool:
    content = msg.get("content")
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error"):
                return True
    return False


def _first_str(lst) -> "str | None":
    """The first non-empty string in a list, or None.

    Called when a tool argument is a list rather than a scalar. An empty list,
    a list of non-strings, and a list of dicts all return None without raising.
    """
    if not isinstance(lst, list):
        return None
    for item in lst:
        if isinstance(item, str) and item.strip():
            return item.strip()
    return None


def _identifying_arg(name: str, tool_input) -> str:
    """The most identifying argument of a tool call, verbatim and truncated.

    Deliberately not a summary. The reader is scanning for "which file", not for a
    description of intent.

    When the tool has a deck-open anchor, the label derives from that anchor's
    value — the same source as the href — so text and link always agree (#346).
    List-path tools with multiple files get a '+N' count suffix on the label;
    the anchor ref is never decorated.

    Tools without an anchor fall through to IDENTIFYING_ARG key order, then the
    generic insertion-order scan. A list-valued field yields its first string
    element rather than falling through to an unrelated scalar.

    dict-in-list (e.g. AskUserQuestion's 'questions': [{...}]) is out of scope:
    there is no standard field name that identifies a dict element across tools,
    so deciding which sub-field to surface requires per-tool knowledge this
    function deliberately does not encode. A future fix would need to extend
    _LIST_PATH_TOOLS (or a parallel table) with a sub-key selector per tool.
    """
    if not isinstance(tool_input, dict):
        return ""
    # Anchor-first: label and href must derive from ONE value (#346). Ask
    # _tool_anchor for the canonical ref; if it finds one, use it and stop.
    _, full_ref = _tool_anchor(name, tool_input)
    if full_ref is not None:
        label = full_ref[:MAX_ARG]
        if name in _LIST_PATH_TOOLS:
            lst = tool_input.get(_LIST_PATH_TOOLS[name])
            if isinstance(lst, list) and len(lst) > 1:
                label = f"{label} +{len(lst) - 1}"
        return label
    for key in IDENTIFYING_ARG:
        v = tool_input.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()[:MAX_ARG]
        if isinstance(v, list):
            first = _first_str(v)
            if first is not None:
                return first[:MAX_ARG]
    for v in tool_input.values():
        if isinstance(v, str) and v.strip():
            return v.strip()[:MAX_ARG]
        if isinstance(v, list):
            first = _first_str(v)
            if first is not None:
                return first[:MAX_ARG]
    return ""


def _tool_anchor(name: str, tool_input) -> "tuple[str, str] | tuple[None, None]":
    """The (origin, full_ref) for a deck-open anchor, or (None, None) if not eligible.

    Fails closed: an unrecognised name, a non-dict input, or a missing or
    non-string value each return (None, None). The ref is the full untruncated
    value — the visible label may truncate at MAX_ARG, but the href must carry
    the complete path so Deck can locate the file unambiguously.

    For tools in _LIST_PATH_TOOLS the primary argument is a list of paths; the
    first string element becomes the ref. A multi-file call links the first path.
    """
    if not isinstance(tool_input, dict):
        return None, None
    if name in _PATH_TOOLS:
        for key in _PATH_TOOL_KEYS:
            v = tool_input.get(key)
            if isinstance(v, str) and v.strip():
                return "path", v.strip()
    elif name in _LIST_PATH_TOOLS:
        key = _LIST_PATH_TOOLS[name]
        first = _first_str(tool_input.get(key))
        if first is not None:
            return "path", first
    elif name in _KB_TOOLS:
        v = tool_input.get("id")
        if isinstance(v, str) and v.strip():
            return "kb", v.strip()
    return None, None


def _queued_command_row(rec) -> "dict | None":
    """The turn a released queued message becomes, or None if this is not one.

    None means "not a human's queued message" and lets the caller fall through
    to the ordinary `attachment` handling, which hides it. That fall-through is
    the common case by a wide margin: most `queued_command` attachments are
    `task-notification`, the harness telling the agent a background task
    finished, and at 07:30 MST on 2026-08-10 that read 4,121 against 1,011
    human ones. Treat those as scale, not as a figure to reconcile — the corpus
    is a rolling window that gains and loses records continuously, and two
    counts an hour apart that day differed by more than a hundred. Drawing a
    task-notification as a turn labelled "you" is the exact misattribution
    `_harness_event` exists to prevent for ordinary user records, and it would
    land on a surface the operator reads as his own words.

    THREE GUARDS, AND THEY ARE DELIBERATELY NOT ALL REQUIRED IN THE SAME WAY.
    `commandMode` and `origin` agree exactly — every `prompt` record carries
    `origin.kind == "human"` and no other record does, with no exception in
    either direction — so either alone would do today and the choice is about
    which failure survives Anthropic changing the shape. That agreement is the
    durable observation; the counts above are not, which is why this states it
    as a correspondence rather than as a number:

      * `commandMode == "prompt"` is REQUIRED. It is the field that names what
        the record is, and a mode this code has never seen is not a mode it
        should attribute to a person.
      * `origin.kind` is required to be `"human"` only WHEN PRESENT. Demanding
        it outright would re-hide every human message the day the field is
        dropped — silent degradation, which is the failure mode this codebase
        has already shipped four times. An explicitly non-human origin is still
        refused.
      * `_harness_event` runs on the text regardless, the same backstop
        `held_rows_for` applies to a queued message on the way in. A guard on
        the envelope and a guard on the content fail independently.

    The row is an ordinary `you` prompt, with no mark of having been queued. It
    IS the plain turn the held row hands off to: the held row carries the dashed
    rule and the "you (queued)" label, and its whole promise is that it settles
    into something indistinguishable from a message typed when the session was
    idle. A badge here would keep a distinction the reader stopped needing the
    moment the agent read it.
    """
    att = rec.get("attachment")
    if not isinstance(att, dict) or att.get("type") != "queued_command":
        return None
    if att.get("commandMode") != "prompt":
        return None
    origin = att.get("origin")
    if origin is not None and not (
            isinstance(origin, dict) and origin.get("kind") == "human"):
        # PRESENT-BUT-UNREADABLE IS A CLAIM, NOT AN ABSENCE. An earlier version
        # refused only a dict whose `kind` was wrong, so an `origin` that was a
        # string or a list read as "field not there" and passed. Absent is a
        # shape this code has seen and reasoned about; a shape it has not seen
        # is exactly when it should not be attributing text to a person. This
        # now fails closed the way `commandMode` above does, which is what
        # review asked for: the two guards sit together deciding one question
        # and should read as equally strict.
        return None
    text = att.get("prompt")
    if not isinstance(text, str) or not text.strip():
        return None
    if _harness_event(text) is not None:
        return None
    return {"kind": "prompt", "display": FULL, "label": "you", "body": text}


def classify(rec) -> list[dict]:
    """One transcript record to zero or more rows. Never raises.

    Returns rows shaped {kind, display, label, body}. `kind` is one of the module
    constants; `display` is FULL/COLLAPSED/HIDDEN.
    """
    if not isinstance(rec, dict):
        return [{"kind": "unknown", "display": COLLAPSED,
                 "label": "unreadable record", "body": ""}]

    rtype = rec.get("type")

    # A RELEASED QUEUED MESSAGE ARRIVES BY ONE OF TWO PATHS, AND THIS IS THE ONE
    # THAT WAS BEING DROPPED. Checked ahead of HIDDEN_TYPES, which holds
    # `attachment` and discarded it with everything else, so the page withdrew
    # the held row on release with nothing to draw in its place and the message
    # vanished from the conversation rather than settling into a turn.
    #
    # The other path is an ordinary `user` record carrying
    # `promptSource: "queued"`, which the branch below already renders. The two
    # are mutually exclusive per message — 985 texts took this path and 451 took
    # that one across the corpus, with ZERO taking both, including in the 97
    # transcripts holding some of each — so handling both draws no duplicates.
    #
    # An earlier version of this comment said the attachment was the only copy.
    # That came from one 2.1.226 session where every message happened to take
    # this path, which made the other one invisible. Both are live within a
    # single session; this is not a version split.
    if rtype == "attachment":
        row = _queued_command_row(rec)
        if row is not None:
            return [row]

    if rtype in HIDDEN_TYPES:
        return [{"kind": "control", "display": HIDDEN, "label": str(rtype), "body": ""}]

    msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}

    if rtype == "user":
        # A user record carrying toolUseResult is a tool RESULT echoed back, not a
        # person speaking. That distinction is the whole feature: without it every
        # tool result would render as if the operator had typed it.
        # Membership, not `is not None`: an explicit toolUseResult: null would
        # otherwise misclassify a tool result as something a human typed.
        if "toolUseResult" in rec:
            return [{"kind": "tool_result", "display": COLLAPSED,
                     "label": "result", "body": _result_text(rec, msg),
                     "answers": _result_ids(msg), "error": _is_error(msg)}]
        # `isMeta` is Claude Code's own flag for a message it injected rather than
        # one the person typed — the body of a slash command's skill, a
        # local-command caveat. The TUI does not show these as turns; folding
        # them here is what stops `/handoff-leave` from printing a skill's whole
        # instructions as if the operator had written them. COLLAPSED, not HIDDEN: the
        # file's rule is to surface and fold, never to drop, so the content stays
        # one expand away.
        if rec.get("isMeta") is True:
            text = _text_of(msg.get("content"))
            return [{"kind": "meta", "display": COLLAPSED, "label": "context",
                     "body": text}] if text.strip() else []
        text = _text_of(msg.get("content"))
        if not text.strip():
            return []
        # A slash command's invocation echo folds to a chip naming the command,
        # rather than rendering its `<command-name>…` scaffolding as prose.
        command = _slash_command(text)
        if command is not None:
            return [{"kind": "command", "display": COLLAPSED,
                     "label": command, "body": text}]
        # THE HARNESS SPEAKS IN THE USER'S VOICE, and nothing above catches it.
        # A task notification and a stopped-agent line are ordinary user records:
        # no `toolUseResult`, no `isMeta`. So they reached the row below and were
        # drawn as full-width turns labelled "you" — the operator reading his own pane and
        # finding himself apparently writing XML. Measured on a live selector
        # transcript: 19 such records against 5 real prompts, so the surface was
        # mostly attributing the harness to him.
        event = _harness_event(text)
        if event is not None:
            return [{"kind": "event", "display": COLLAPSED,
                     "label": event, "body": text}]
        return [{"kind": "prompt", "display": FULL, "label": "you", "body": text}]

    if rtype == "assistant":
        content = msg.get("content")
        if isinstance(content, str):
            return ([{"kind": "speech", "display": FULL, "label": "claude",
                      "body": content}] if content.strip() else [])
        if not isinstance(content, list):
            return []
        rows = []
        for b in content:
            if not isinstance(b, dict):
                rows.append({"kind": "unknown", "display": COLLAPSED,
                             "label": "unreadable block", "body": ""})
                continue
            btype = b.get("type")
            if btype == "text":
                t = b.get("text")
                if isinstance(t, str) and t.strip():
                    rows.append({"kind": "speech", "display": FULL,
                                 "label": "claude", "body": t})
            elif btype == "tool_use":
                name = b.get("name") if isinstance(b.get("name"), str) else "tool"
                tool_input = b.get("input")
                origin, full_ref = _tool_anchor(name, tool_input)
                arg = _identifying_arg(name, tool_input)
                row = {"kind": "tool", "display": COLLAPSED,
                       "label": f"{name}({arg})" if arg else name,
                       "id": b.get("id") if isinstance(b.get("id"), str) else None,
                       "body": json.dumps(tool_input, indent=2, default=str)[:4000]}
                if origin is not None:
                    row["tool_name"] = name
                    row["tool_arg"] = arg
                    row["tool_origin"] = origin
                    row["tool_ref"] = full_ref
                rows.append(row)
            elif btype == "thinking":
                # Present but closed (gate G10, operator 2026-07-19). Reasoning is
                # available on demand without competing with what was said.
                rows.append({"kind": "thinking", "display": COLLAPSED,
                             "label": "thinking", "body": str(b.get("thinking") or "")})
            else:
                rows.append({"kind": "unknown", "display": COLLAPSED,
                             "label": f"block: {btype}", "body": ""})
        return rows

    # An UNKNOWN top-level type. Surfaced, never dropped: the format is
    # Anthropic's, so a new type is expected, and a reader who cannot tell that
    # something happened is worse off than one who sees an unhelpful line.
    return [{"kind": "unknown", "display": COLLAPSED,
             "label": f"record: {rtype}", "body": ""}]


# A row key has to survive interpolation into an HTML attribute and into the
# JavaScript Deck evaluates against the page, so its charset is an allowlist
# rather than an escape. `uuid` comes from the transcript, which embeds tool
# output — the field itself is Claude Code's, but the file is not ours and the
# cost of being wrong about that is injection into a host-evaluated script. A
# uuid outside this shape falls back to the content hash below, which is hex by
# construction.
#
# THIS IS THE UUID'S SHAPE, NOT THE EMITTED KEY'S. A key built from it also
# carries a `.` before the block index and, on a repeat, a `~n` suffix from
# `stamp` below. So the full emitted charset is `[A-Za-z0-9._~-]`, which is what
# a consumer validates against — `~` is not special in HTML, in a JavaScript
# string, or in a quoted CSS attribute selector, so it costs nothing here and it
# would be a real gap in a consumer that allowlisted only the characters above.
_ROW_KEY_OK = re.compile(r"\A[A-Za-z0-9._-]{1,100}\Z")


def _row_key(rec: "dict | None", index: int, row: dict) -> str:
    """A stable identifier for one row, for a reader's open/closed state.

    Deck reloads the whole document whenever the conversation changes, so a
    `<details>` the reader opened re-collapses unless something survives the
    reload. Position cannot be that something: a new turn inserted above shifts
    every index below it, and the reader finds an unrelated row expanded — a
    visible defect traded for a silent one.

    The record's `uuid` is the identity to use where it exists. A record is
    written once and appended to a file, so its uuid is fixed for the life of
    the transcript, and one record's blocks are distinguished by their index
    WITHIN it, which is stable for the same reason.

    Where there is no usable uuid — an unparseable line, a record shaped in a
    way this does not expect — the fallback is a hash of what the row says.
    That is stable while the row's content is, which is the most a keyless row
    can offer, and it degrades to the current behaviour rather than to a wrong
    row opening.
    """
    if isinstance(rec, dict):
        uid = rec.get("uuid")
        if isinstance(uid, str) and _ROW_KEY_OK.match(uid):
            return f"{uid}.{index}"
    seed = "\x00".join((
        str(row.get("kind", "")), str(row.get("label", "")), str(row.get("body", "")),
    ))
    return "h" + hashlib.sha1(seed.encode("utf-8", "replace")).hexdigest()[:16]


def fold_results(rows: list[dict]) -> list[dict]:
    """Merge each tool result into the call it answers.

    The operator, 2026-07-19, on the first version: the results "would be better ... in the
    collapsed part of the call anyway." He is right, and it halves the row count:
    a call and its result are one thing that happened, so they read as one line
    you expand to see what came back.

    Correlation is exact, not positional — a `tool_result` block names the
    `tool_use_id` it answers, so concurrent calls fold correctly. A result whose
    call is missing (a truncated transcript, or a call outside the window) stays
    its own row rather than being dropped.
    """
    # First occurrence wins. Ids are expected unique, but the format is
    # undocumented, and a dict comprehension would silently keep the LAST — which
    # attaches the result to the wrong call and leaves the first empty. If the
    # assumption ever breaks, failing toward the earlier call is at least stable.
    by_id: dict[str, dict] = {}
    for r in rows:
        if r.get("kind") == "tool" and r.get("id"):
            by_id.setdefault(r["id"], r)
    out = []
    for r in rows:
        if r.get("kind") != "tool_result":
            out.append(r)
            continue
        target = next((by_id[i] for i in r.get("answers") or [] if i in by_id), None)
        if target is None:
            out.append(r)      # orphan: surface it rather than lose it
            continue
        target["result"] = r.get("body", "")
        if r.get("error"):
            target["error"] = True
        # _result_line drives render_delta's rewind rule: a tool_use whose result
        # arrived after the cursor must be re-sent even though the call itself is
        # older. Conditional on _line being present so a caller that built rows by
        # hand without stamping _line never writes _result_line: None into a row.
        if "_line" in r:
            target["_result_line"] = r["_line"]
    return out


def _queue_operation_records(lines) -> list[dict]:
    """Every `queue-operation` record among `lines`, oldest first.

    A second linear pass over the same lines `rows_for` parses, kept separate
    rather than folded into that loop: `classify` puts `queue-operation` in
    `HIDDEN_TYPES` and discards the record itself once it has classified it, and
    reaching into that pipeline for a second, unrelated return value would tie
    the row-classification contract (one record in, rows out) to a feature that
    has nothing to do with classification. The extra `json.loads` per line is
    small next to the markdown rendering and regex work `rows_for` already does
    over the same transcript.
    """
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:  # noqa: BLE001 — an unparseable line has no operation
            continue
        if isinstance(rec, dict) and rec.get("type") == "queue-operation":
            out.append(rec)
    return out


def _tail_lines(lines: list, byte_cap: int) -> list:
    """The suffix of `lines` whose encoded bytes fit within the last `byte_cap`.

    Mirrors `session_state._read_held_queue`'s file-tail read — that reader
    seeks to `size - _HELD_TAIL_BYTES` in the raw file and folds only what is
    after it — so the page's fold and the state field's fold share one
    recovery window. Without this, `held_rows_for` folded every line ever
    written: once an ambiguous `remove` occurs anywhere in a session, it is
    present on every future full-file read this function does, and the page's
    held rows go dark for the rest of the session even after the state field
    has recovered because that same record scrolled out of its bounded tail.

    Kept to whole lines rather than an exact byte offset into the file — this
    operates on an already-split list, and a partial line here would either
    fail to parse (matching the state reader's own tolerance for a split
    boundary) or, worse, silently truncate a `content` field mid-string.

    A LINE THAT STRADDLES THE BOUNDARY IS DROPPED, NOT ROUNDED UP, and that is
    what makes "one recovery window" true rather than merely nominal. The state
    reader keeps the last `_HELD_TAIL_BYTES` of the raw file, so a record whose
    start falls before that offset arrives truncated, fails to parse, and is
    skipped. Rounding the same record up to whole-line-included here would make
    the two readers disagree at the boundary about queue CONTENT — review
    reproduced it with a 2000-byte `enqueue` followed by a 50-byte one under a
    1000-byte cap, where the state field saw one message and the page saw both
    — and, worse, about whether the queue is ambiguous at all, since an
    ambiguous `remove` visible to one reader and dropped by the other has one
    of them answering "unknown" while the other answers confidently.
    """
    total = 0
    cut = len(lines)
    for i in range(len(lines) - 1, -1, -1):
        total += len(lines[i].encode("utf-8", "replace")) + 1  # +1 for the newline
        if total > byte_cap:
            # This line's start lies before the byte offset the state reader
            # seeks to, so that reader gets it truncated and drops it. Drop it
            # here too rather than including it whole.
            break
        cut = i
    return lines[cut:]


# A held row's key must never collide with a `_row_key` output, including one
# built from an adversarial transcript — the file is Anthropic's, not ours (see
# `_ROW_KEY_OK`'s own comment). `_row_key` only ever emits three shapes: an
# emitted key always STARTS with either a `uuid` field that passed
# `_ROW_KEY_OK` (charset `[A-Za-z0-9._-]`, no `~`) or the literal `"h"` fallback
# prefix, and gains a `~n` disambiguator only as a SUFFIX appended to one of
# those two, never as a prefix. So no string `_row_key` can produce starts with
# `~`, which makes a leading `~` a safe, collision-proof marker for this key
# space — independent of whatever an attacker puts in a `uuid` field, since
# `~` itself is outside the charset that field is validated against.
#
# The index is the message's position in the CURRENT fold and the suffix is a
# hash of its own text, so the key is stable across re-renders while the same
# message sits at that position, and changes the moment it does not. A release
# or a `remove` shifting who is where are both real state changes, and a
# changed key for them is correct, not a bug.
#
# WHAT A RELEASE LOOKS LIKE IS DELIBERATELY NOT CLAIMED HERE. This comment
# used to say the whole queue drains as one batch; that reading is superseded
# by `agent-platform/contracts/claude-transcript-jsonl`, which measured one
# `remove` per message and traced the batch story back to two `dequeue`
# records sharing a millisecond. The contract also records that both
# operations are live across every version from 2.1.202 on and that the
# distinction between them is NOT YET ESTABLISHED. Nothing here depends on
# the answer — the key changes when the fold's composition changes, whether
# one message leaves or all of them do — so the safe thing is to say that and
# let the contract own the open question.
#
# The charset used — `~`, letters, digits, `.` — stays inside
# `[A-Za-z0-9._~-]`, the emitted-key allowlist `platform/contracts/
# session-page-markup` documents and Deck's `rowKeyPattern` enforces, so this
# needs no change on the consumer side.
def _held_row_key(index: int, text: str) -> str:
    digest = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]
    return f"~held.{index}.{digest}"


def _held_rows(lines) -> "tuple[list[dict] | None, bool]":
    """`held_rows_for`'s rows, plus whether ABSENCE from them is authoritative.

    The second value is the window-completeness `fold_queue` is given, surfaced
    because one caller needs it and the difference is not cosmetic. A complete
    window saw the session from its start, so a message missing from the rows is
    a message that genuinely is not queued. A truncated window can miss an
    `enqueue` that scrolled out with no later operation on it — the fold never
    sees that entry at all, so it is not a mismatch that trips an error, and the
    rows come back a positive-looking answer that is simply short.

    That is why `render_delta` may not read absence as departure. Reported by
    security review with a working repro: a message queued early and still held,
    with more than `_HELD_TAIL_BYTES` of transcript accumulating after it, drops
    out of a fold over the whole file while remaining in a fold over the
    client's shorter prefix — and the difference between the two is exactly what
    drives the removal list.
    """
    if _SS is None:
        return None, False
    tail = _tail_lines(lines, _SS._HELD_TAIL_BYTES)
    # The tail kept every line it was given, so this fold sees the session from
    # its start and a content-matched `remove` is exact. `_tail_lines` drops a
    # line that straddles the byte boundary, so a shorter list is precisely the
    # "opened mid-stream" case `fold_queue` must not trust.
    complete = len(tail) == len(lines)
    # SYNC, NOT MERELY A COMPLETE WINDOW, is what makes absence authoritative,
    # and the difference is the sharper signal review asked for. `complete`
    # answers "did this read see the whole session"; `synced` answers "is this
    # fold's account of the queue exact from here on", which a `popAll` makes
    # true even inside a truncated window because it empties the real queue.
    #
    # Gating on the coarse flag stranded a row: a message released by `popAll`
    # in an over-cap transcript resolved cleanly, so the fold dropped it, but the
    # window was still truncated so no removal was emitted and the client kept
    # showing a "you (queued)" ghost beside the delivered turn — the same
    # message twice. `fold_queue` already computed the right answer internally
    # and only the wrapper hid it.
    messages, synced = _SS._fold_queue(_queue_operation_records(tail),
                                       window_complete=complete)
    if messages is None:
        return None, synced
    rows = []
    for i, text in enumerate(messages):
        if not isinstance(text, str) or not text.strip():
            continue
        if _harness_event(text) is not None:
            continue
        rows.append({
            "kind": "prompt", "display": FULL, "label": "you (queued)",
            "body": text, "held": True, "key": _held_row_key(i, text),
        })
    return rows, synced


def held_rows_for(lines) -> "list[dict] | None":
    """Row dicts for the session's currently-queued messages, oldest first.

    Folds a bounded tail of `lines` with `session_state.fold_queue` — the SAME
    fold Slice B built for the `held` state field, over the SAME `_HELD_TAIL_
    BYTES` window (`_tail_lines`) — via the optional import above, so this page
    and the live bar agree on what is held rather than each guessing
    separately, and recover from an ambiguous record on the same read as each
    other rather than the state field alone.

    THREE-WAY RETURN, not two. `None` means unknown — `session_state` is
    unavailable, or the fold could not resolve the queue's current contents
    (an ambiguous `remove`, or a read that opened mid-stream) — and is
    distinct from `[]`, which is the positive claim that the queue is
    genuinely empty. Collapsing the two was the bug: a caller with nothing to
    reconcile against (a full render) can safely treat `None` as `[]` — there
    is no earlier held set on the page for it to leave stranded — but
    `render_delta`'s diff cannot, because there `[]` drives the removal list,
    and an unknown queue state must never be read as "queue just emptied."
    Every full-render caller collapses explicitly (`held_rows_for(...) or
    []`); `render_delta` checks for `None` instead and takes its full-page
    escape hatch, which is unreconcilable-safe by construction since it
    replaces the whole document rather than patching it.

    Filters non-user enqueues with `_harness_event`, the SAME check `classify`
    already runs on ordinary "user" records and for the identical reason: the
    harness writes into this stream in the user's voice too — a
    `<task-notification>` blob from a background task is the hazard the plan
    names outright — and drawing that text as "your held message" would be the
    exact misattribution `classify` exists to prevent for read turns.

    Each row carries `held: True`, which `_render_row` reads to add the `held`
    class alongside `you` — the released row a queued message eventually
    becomes is an ordinary transcript record, classified and keyed the normal
    way; this function's rows never persist past the poll that produced them.

    A thin wrapper over `_held_rows`, which carries the same rows plus whether
    absence from them is authoritative. Callers that only draw rows want this
    one; only `render_delta`, which reads absence as departure, needs the other.
    """
    return _held_rows(lines)[0]


def rows_for(lines) -> list[dict]:
    """Classify an iterable of raw transcript lines. Never raises.

    Every row leaves here carrying a `key` (see `_row_key`) and `_line` (its
    0-based source-line index). Both are stamped here rather than inside
    `classify` because `classify` returns from a dozen places and an attribute
    added at only eleven of them is the kind of gap that shows up as one row
    that will not stay open. `_line` lets `fold_results` stamp `_result_line`
    on a tool row when its result is folded in, which render_delta uses for the
    rewind rule — see `_rows_for_lines`.
    """
    rows = []
    # Keys are expected unique, and a duplicate would mean two rows open and
    # close together — the wrong-row failure this exists to avoid, arriving by
    # another door. Counting occurrences makes uniqueness a property of the
    # output rather than an assumption about the input.
    #
    # `seen` counts every row stamped, including the `tool_result` rows that
    # `fold_results` merges away afterwards. So on a transcript where two
    # records share a uuid, a surviving row can carry a `~n` suffix earned by a
    # row that is no longer in the output. That is stable across renders, which
    # is the only property a consumer relies on, and the alternative — stamping
    # after the fold — would key each row by whether its result had arrived yet.
    seen: dict[str, int] = {}

    def stamp(new_rows: list[dict], rec: "dict | None", line_idx: int) -> None:
        for i, row in enumerate(new_rows):
            key = _row_key(rec, i, row)
            n = seen.get(key, 0)
            seen[key] = n + 1
            row["key"] = key if n == 0 else f"{key}~{n}"
            row["_line"] = line_idx
        rows.extend(new_rows)

    for line_idx, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            stamp([{"kind": "unknown", "display": COLLAPSED,
                    "label": "unparseable line", "body": ""}], None, line_idx)
            continue
        stamp(classify(rec), rec, line_idx)
    return fold_results(rows)


def read_rows(path: Path) -> list[dict]:
    """The transcript's rows, plus any currently-queued message as a held row.

    Held rows are appended LAST — they are chronologically the newest content,
    typed after everything already in the transcript, and `render_html` relies
    on row order to put the newest turn at the bottom of the page.

    `held_rows_for`'s `None` (queue state unknown) collapses to `[]` here: a
    full render has no earlier held set on the page to reconcile against, so
    "unknown" and "draw nothing extra" are the same outcome for this caller —
    unlike `render_delta`, which must keep them distinct (see that
    function's docstring).
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = text.splitlines()
    return rows_for(lines) + (held_rows_for(lines) or [])


def _rows_for_lines(raw_lines: "list[str]") -> "tuple[int, list[dict]]":
    """Parse an explicit list of transcript lines, returning (total_line_count, rows).

    rows_for stamps _line on every row; fold_results stamps _result_line on a
    tool row when its result is folded in. render_delta uses both for the rewind
    rule: a tool_use whose result arrived after the cursor is re-sent even though
    the call itself is older.
    """
    return len(raw_lines), rows_for(raw_lines)


# --- rendering ---------------------------------------------------------------

# THE APPEARANCE RULE THIS OBEYS. Deck's shell is the single appearance
# authority: it resolves light or dark once and stamps `data-theme` into every
# pane in the same frame, so a pane cannot decide for itself and end up light
# beside a dark rail. This page used `Canvas`/`CanvasText`, which is exactly
# that forbidden self-decision — it followed the OS, agreed with Deck by luck,
# and would have diverged the moment the operator pinned an appearance.
#
# So: an explicit `data-theme` always wins, and only in its ABSENCE does the
# page follow the system. That keeps it correct in a plain browser, where
# nobody stamps anything, without letting the browser case override a stamp.
#
# The variables are named after orient's Atelier set (`--bg`, `--ink`,
# `--surface`) and keyed on the same `[data-theme="dark"]` selector orient
# already uses. The design-token pipeline (WS2.5/4.2) does not exist yet; when
# it does, this becomes a value swap rather than a restructure, which is what
# the WS1.5 plan meant by "retrofitted when Deck's panes do".
PAGE_CSS = """
:root {
  color-scheme: light;
  --bg: #fdfdfc; --ink: #1c1c1a; --err: #b3261e;
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --bg: #16171a; --ink: #e6e5e1; --err: #e8776b;
}
/* No stamp: follow the system. Scoped to :not([data-theme]) so a stamped
   appearance is never overridden by the OS setting disagreeing with it. */
@media (prefers-color-scheme: dark) {
  :root:not([data-theme]) {
    color-scheme: dark;
    --bg: #16171a; --ink: #e6e5e1; --err: #e8776b;
  }
}
/* `.9375rem` is 15px at the default root size, written relative so the whole
   page answers to `--scale`. As an absolute 15px it did not, which left the
   empty-state line stuck while everything around it resized — and made the
   claim "every size here is relative" false for whoever edits this next. */
/* THE TURN LIST IS THE SCROLLER, not the body, and that is what makes the
   bottom-anchoring work: a `column-reverse` flex container starts its scroll at
   the flow's beginning, which here is the newest turn. Anchoring on the body
   leaves the page at the top instead — measured. */
html { height: 100%; }
body { font: .9375rem/1.6 -apple-system, BlinkMacSystemFont, sans-serif;
       height: 100%; margin: 0; padding: 0 1rem; box-sizing: border-box;
       display: flex; flex-direction: column; align-items: center;
       background: var(--bg); color: var(--ink); }
/* WHAT IS SERIF AND WHAT IS NOT. Only the prose a human reads at length: what
   was said, by the person and by the model. Tool calls, their arguments and
   their results stay monospace, because they are code and output rather than
   writing, and because the eye uses the change of face to tell the two apart
   without a label.
   `ui-serif` resolves to New York on Apple platforms and to the reader's serif
   elsewhere; the rest of the stack is the fallback for a machine with neither. */
.body { font-family: ui-serif, "New York", Georgia, "Times New Roman", serif;
        font-size: 1.05rem; line-height: var(--body-line-height, 1.65); }
/* The exceptions inside prose. A code span or a fence is still code when it
   sits in a sentence, and inheriting the serif would undo the distinction the
   rule above draws. */
.body code, .body pre, .body pre code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
h1 { font-size: 1rem; font-weight: 600; opacity: .6;
     width: 100%; max-width: 46rem; flex: none; margin: 1.4rem 0 .6rem; }
.turn { margin: 1.1rem 0; }
.who { font-size: .72rem; text-transform: uppercase; letter-spacing: .06em;
       opacity: .5; margin-bottom: .2rem; }
.you .body { border-left: 3px solid currentColor; padding-left: .8rem; opacity: .95; }
/* HELD: a message sent to the queue and not yet read. NO NEW COLOUR — the rule
   and the text both stay currentColor, only thinner, dashed and dimmer than the
   read treatment above. Presence, position and the .who label already say
   "this is yours and it is waiting"; hue would add nothing to something typed
   two seconds ago at the spot the reader's eye already is. There is no release
   transition to style here: a held row simply stops being emitted once its
   message is read, replaced in the next render by the ordinary "you" turn the
   record becomes — so "the rule goes solid and the ink goes full" is just this
   selector no longer matching, not an animation. */
.you.held .body { border-left: 1px dashed currentColor; padding-left: .8rem; opacity: .55; }
/* `white-space: pre-wrap` was here from before this view rendered markdown, and
   it double-counts now that blocks are real: the newline between two `<p>` tags
   became a visible break on top of the margin they already carry. `<pre>` keeps
   its own whitespace regardless, so fences and tool output are unaffected. */
.body { overflow-wrap: anywhere; }
details { margin: .15rem 0; }
summary { cursor: pointer; font-size: .82rem; opacity: .55;
          font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
          list-style: none; padding: .1rem 0; }
summary::-webkit-details-marker { display: none; }
summary:before { content: "▸ "; }
details[open] > summary:before { content: "▾ "; }
summary:hover { opacity: .9; }
details pre { font-size: .78rem; opacity: .75; overflow-x: auto;
              padding: .5rem .8rem; margin: .2rem 0 .6rem;
              border-left: 2px solid currentColor; }
.empty { opacity: .5; font-style: italic; }
/* BOTTOM-ANCHORED WITHOUT FOCUS. The page opens at its newest turn because the
   turns are laid out in a reversed flex column and emitted in reverse order, so
   the flow starts at the bottom and the visual order still reads oldest-first.
   No script, and nothing is focused.
   The previous version used an `autofocus` anchor, and focusing an element in
   the pane pulled first responder AWAY from the composer: the operator's keystrokes
   stopped landing and macOS beeped at every render. A surface that steals focus
   from the box you are typing in is worse than one that opens at the top.

   WHAT THIS COSTS, so nobody rediscovers it as a bug: the clipboard follows DOM
   order, not visual order, so a drag selection spanning SEVERAL turns copies
   them newest-first. A selection inside one turn is unaffected, which is the
   overwhelmingly common case. Accepted deliberately — reversed paste order is a
   nuisance a reader can see and fix, where a keyboard that stops working
   mid-sentence is not. */
.turns { width: 100%; max-width: 46rem; flex: 1; min-height: 0;
         overflow-y: auto; display: flex; flex-direction: column-reverse; }
.body p { margin: .5rem 0; }
.body h3, .body h4, .body h5, .body h6 { font-size: 1rem; margin: 1rem 0 .3rem; }
.body ul { margin: .4rem 0; padding-left: 1.2rem; }
.body li { margin: .15rem 0; }
.body code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
             font-size: .88em; padding: .05rem .25rem; border-radius: 3px;
             background: color-mix(in srgb, var(--ink) 9%, transparent); }
.body pre { padding: .6rem .8rem; overflow-x: auto; border-radius: 4px;
            background: color-mix(in srgb, var(--ink) 6%, transparent); }
.body pre code { background: none; padding: 0; }
/* Tables. `overflow-x: auto` on a wrapper is the usual answer to a wide table,
   but there is no wrapper here and adding one would mean the renderer emitting
   layout around content; `table-layout: auto` with a max width lets long cells
   wrap instead, which suits a pane that is already narrow. */
.body table { border-collapse: collapse; margin: .7rem 0; font-size: .92rem;
              display: block; overflow-x: auto; max-width: 100%; }
.body th, .body td { padding: .3rem .6rem; text-align: left;
                     border-bottom: 1px solid color-mix(in srgb, var(--ink) 15%, transparent); }
.body th { font-weight: 600; opacity: .75; }
.body tbody tr:last-child td { border-bottom: none; }
.body blockquote { margin: .5rem 0; padding-left: .8rem; opacity: .8;
                   border-left: 2px solid currentColor; }
/* Full opacity, not the .55 other summaries carry: an error is
   the one collapsed line that should not be dimmed, and .85 over
   the light --bg fell under WCAG AA. Both --err values clear AA at
   full strength. */
details.err > summary { color: var(--err); opacity: 1; }
details pre.res { border-left-color: currentColor; }
details pre.arg { opacity: .5; }
"""


FILE_VIEWER_CSS = """
:root {
  color-scheme: light;
  --bg: #fdfdfc; --ink: #1c1c1a; --surface: #f5f4f0; --err: #b3261e;
}
[data-theme="dark"] {
  color-scheme: dark;
  --bg: #16171a; --ink: #e6e5e1; --surface: #1e2026; --err: #e8776b;
}
/* The shell always stamps data-theme on the file viewer pane; prefers-color-scheme
   is deliberately absent so a pinned appearance is never overridden by the OS. */
html { min-height: 100%; }
body {
  font: .9375rem/1.6 -apple-system, BlinkMacSystemFont, sans-serif;
  margin: 0; padding: 1.5rem 2rem; box-sizing: border-box;
  max-width: 52rem; background: var(--bg); color: var(--ink);
}
h1 {
  font-size: 1.05rem; font-weight: 600; opacity: .6;
  margin: 0 0 1.2rem; padding-bottom: .5rem;
  border-bottom: 1px solid color-mix(in srgb, var(--ink) 12%, transparent);
}
/* md-frontmatter: compact typed header above the body — visually distinct from prose */
.md-frontmatter {
  display: grid; grid-template-columns: auto 1fr;
  gap: .15rem .75rem; margin: 0 0 1.5rem;
  padding: .55rem .75rem; border-radius: 4px;
  background: var(--surface);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: .8rem; line-height: 1.5;
}
.md-fm-field { display: contents; }
.md-fm-key { opacity: .5; }
.md-fm-value { opacity: .9; }
/* Inert locked chips — HTML comments, footnote defs, deeply-nested content */
.md-chip {
  display: inline-block; opacity: .35; cursor: default;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: .8em; padding: .05rem .3rem; border-radius: 3px;
  background: color-mix(in srgb, var(--ink) 8%, transparent);
  user-select: none; vertical-align: middle;
}
.md-footnote-def {
  display: block; margin: .3rem 0;
}
.md-unsafe-link {
  text-decoration: line-through; opacity: .45;
}
/* Document body — headings */
.md-doc h1 { font-size: 1.55rem; margin: 1.4rem 0 .5rem; opacity: 1; font-weight: 700; border: none; padding: 0; }
.md-doc h2 { font-size: 1.2rem; margin: 1.25rem 0 .4rem; font-weight: 600; }
.md-doc h3 { font-size: 1.05rem; margin: 1.1rem 0 .3rem; font-weight: 600; }
.md-doc h4, .md-doc h5, .md-doc h6 { font-size: 1rem; margin: 1rem 0 .25rem; font-weight: 600; }
/* Prose */
.md-doc p { margin: .5rem 0; line-height: 1.7; overflow-wrap: anywhere; }
.md-doc a { color: inherit; }
.md-doc blockquote {
  margin: .5rem 0; padding-left: .8rem; opacity: .8;
  border-left: 2px solid currentColor;
}
.md-doc hr {
  border: none;
  border-top: 1px solid color-mix(in srgb, var(--ink) 15%, transparent);
  margin: 1.5rem 0;
}
/* Lists */
.md-doc ul, .md-doc ol { margin: .4rem 0; padding-left: 1.5rem; }
.md-doc li { margin: .15rem 0; }
/* Inline code */
.md-doc code {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: .88em; padding: .05rem .25rem; border-radius: 3px;
  background: color-mix(in srgb, var(--ink) 9%, transparent);
}
/* Fenced code blocks */
.md-doc pre {
  padding: .6rem .8rem; overflow-x: auto; border-radius: 4px;
  background: color-mix(in srgb, var(--ink) 6%, transparent);
  margin: .6rem 0; font-size: .88rem;
}
.md-doc pre code { background: none; padding: 0; }
/* Tables */
.md-doc table {
  border-collapse: collapse; margin: .7rem 0; font-size: .92rem;
  display: block; overflow-x: auto; max-width: 100%;
}
.md-doc th, .md-doc td {
  padding: .3rem .6rem; text-align: left;
  border-bottom: 1px solid color-mix(in srgb, var(--ink) 15%, transparent);
}
.md-doc th { font-weight: 600; opacity: .75; }
.md-doc tbody tr:last-child td { border-bottom: none; }
/* KB doc metadata line (kind · project · status) */
.md-kb-meta {
  opacity: .55; font-size: .85rem; margin: 0 0 1rem;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
"""

# Additional CSS for the WYSIWYG editor page.  FILE_VIEWER_CSS is always emitted
# first; this extends it with the toolbar and editable-surface rules.
EDITOR_CSS = """
#editor-toolbar {
  position: sticky; top: 0; z-index: 10;
  background: var(--bg);
  padding: .4rem 0; margin-bottom: 1rem;
  border-bottom: 1px solid color-mix(in srgb, var(--ink) 12%, transparent);
  display: flex; align-items: center; gap: .75rem;
}
#save-btn {
  font: inherit; font-size: .85rem;
  padding: .25rem .75rem; border-radius: 4px;
  background: color-mix(in srgb, var(--ink) 10%, transparent);
  color: var(--ink); border: 1px solid color-mix(in srgb, var(--ink) 20%, transparent);
  cursor: pointer;
}
#save-btn:hover { background: color-mix(in srgb, var(--ink) 18%, transparent); }
#save-unavailable { font-size: .85rem; color: var(--err); }
#editor-root { outline: none; }
"""

# These run over ALREADY-ESCAPED text, so a quote inside a URL arrives as the
# entity `&#x27;` / `&quot;` — no literal quote character survives to be excluded
# by a character class. The parser decodes those entities INSIDE the attribute
# value, which closes the href early, so the check has to be on the entity form.
_ESCAPED_QUOTE = re.compile(r"&(#x27|#39|quot);", re.I)


def _link_sub(m: "re.Match") -> str:
    """A link, or the original text when the URL cannot be safely quoted."""
    label, url = m.group(1), m.group(2)
    if _ESCAPED_QUOTE.search(url):
        return m.group(0)
    return f"<a href='{url}'>{label}</a>"


# A URL sitting in prose on its own.
#
# `&amp;` is matched as an atom because a query string is the common case for a
# bare URL and `?a=1&b=2` arrives escaped. Stopping at the bare `&` instead would
# silently link the first half of the query — a link that goes somewhere the
# author did not write, which is worse than no link at all.
_BARE_URL = re.compile(r"https?://(?:&amp;|[^\s<>&])*")

# Punctuation that ends a sentence rather than a URL. Trimmed from the href and
# put back as text, so `See https://x/y.` links `https://x/y` and keeps its stop.
#
# `;` IS DELIBERATELY ABSENT, and leaving it in was a bug: a URL ending in `&`
# arrives as `&amp;`, and trimming the `;` splits the entity — producing a
# malformed href and a stray semicolon in the prose. That is exactly the
# query-string case the `&amp;` atom exists to serve, so the two rules
# contradicted each other. A semicolon genuinely ending a URL is rarer than a
# semicolon ending an entity.
_SENTENCE_TAIL = ".,:!?)]}"


def _bare_link_sub(m: "re.Match") -> str:
    """A link, or the original text when the URL cannot be linked honestly.

    Declining when the URL runs into a quote entity is the same judgement the
    markdown-link rule makes, and for the same reason: the match had to stop
    early, so linking it would point at a DIFFERENT, truncated URL. A wrong
    link is worse than plain text, which is at least readable and copyable.
    """
    url = m.group(0)
    if _ESCAPED_QUOTE.match(m.string, m.end()):
        return url
    trimmed = url.rstrip(_SENTENCE_TAIL)
    if len(trimmed) <= len("https://"):
        return url
    return f"<a href='{trimmed}'>{trimmed}</a>{url[len(trimmed):]}"

# Where linkifying would be wrong, and both cases are reachable from one
# session. Inside `<a>` the URL is already a link, and substituting there would
# nest an anchor inside an anchor. Inside `<code>` a URL is being shown as text,
# which is the whole point of the span.
_NO_LINKIFY = ("a", "code")

# Tags, so the walk below can tell markup from text. `_inline_md` only ever
# emits the small closed set of tags above it, so this stays a splitter rather
# than growing into an HTML parser.
_TAG = re.compile(r"<(/?)([a-z]+)[^>]*>")


def _linkify_bare(marked_up: str) -> str:
    """Linkify bare URLs in the text of `marked_up`, never inside its markup.

    A regex with a lookbehind was the first attempt and it cannot do this: it
    sees `href='https://…` and the text of `<code>https://…</code>` as the same
    shape, so it either linkifies both or neither. Walking the tags is what
    tells them apart, and the case that made it necessary is a link the model
    wrote in markdown — already an `<a href>` by the time this runs.
    """
    # One counter PER element, not one shared. A single counter lets a `</a>`
    # inside a code span close the code span's guard, exposing the rest of it to
    # linkification. `_inline_md` emits balanced tags so that input is
    # unreachable today — which is the reason to write the invariant down here
    # rather than rely on a caller staying well-behaved forever.
    depth = dict.fromkeys(_NO_LINKIFY, 0)
    out, pos = [], 0

    def inside() -> bool:
        return any(depth.values())

    for tag in _TAG.finditer(marked_up):
        segment = marked_up[pos:tag.start()]
        out.append(segment if inside() else _BARE_URL.sub(_bare_link_sub, segment))
        name = tag.group(2)
        if name in depth:
            # Floored at zero so a stray close tag cannot drive the count
            # negative, which would re-enable linkification inside a span that
            # is still open.
            depth[name] = max(0, depth[name] - 1) if tag.group(1) else depth[name] + 1
        out.append(tag.group(0))
        pos = tag.end()
    tail = marked_up[pos:]
    out.append(tail if inside() else _BARE_URL.sub(_bare_link_sub, tail))
    return "".join(out)


# Match path-shaped tokens in already-HTML-escaped prose text.
# `&`, `<`, `>`, `"` won't appear literally in text content after html.escape, so
# stopping at `&` catches the entity boundary; stopping at the others is redundant
# but keeps the character class self-documenting.  `'` CAN appear (html.escape
# doesn't encode it in text nodes), so stopping there avoids a single-quote
# breaking a `href='...'` attribute.
#
# Three shapes:
#   1. Tilde paths:   ~/some/path
#   2. Absolute paths: /word[chars]*  (first char after / must be word, dot, or tilde)
#   3. Relative with extension: dir/file.ext  (needs a known shape to avoid prose-slash FPs)
#
# The lookbehind (?<![/\w~]) prevents matching in the middle of a URL already
# present in the text (e.g. the `/path` suffix of `https://example.com/path`).
_PATH_RE = re.compile(
    r"(?<![/\w~])"
    r"("
    r"~/[^\s&<>'\"]*"                                              # tilde path
    r"|/[A-Za-z0-9_.~][^\s&<>'\"]*"                               # absolute path
    r"|[A-Za-z0-9_][A-Za-z0-9_-]*/[A-Za-z0-9_.][^\s&<>'\"]*\.[A-Za-z0-9]{1,10}"  # relative w/ ext
    r")"
)

# Punctuation that ends a sentence but cannot end a path.  Stripped from the
# match before building the href, then appended as plain text after the anchor —
# the same trade _bare_link_sub makes for trailing URL punctuation.
_PATH_SENTENCE_TAIL = ").,;:!?]}"

# Tags whose text content must NOT receive path anchors.  Two exclusions remain:
# `a`, because an anchor inside an anchor is invalid HTML; and `pre`, because a
# fenced code block or tool-output `<pre>` is showing code verbatim, not naming a
# file for the reader to open.  `code` (inline backtick spans) is intentionally
# absent: backticks are how prose names a path, so a path in an inline `code`
# span should be clickable just as it would be without the backticks.
_NO_PATH_LINKIFY = frozenset({"a", "pre"})

# THREE SKIP-SETS NOW EXIST AND THEY DIFFER ON PURPOSE. Do not reconcile them
# without deciding the question each one answers:
#
#   _NO_LINKIFY      ("a", "code")          bare URLs
#   _NO_WIKILINK     ("a", "code", "pre")   [[wikilinks]]
#   _NO_PATH_LINKIFY ("a", "pre")           file paths — this one
#
# Only paths link inside an inline `code` span, because a backticked path is
# how prose NAMES a file, whereas a backticked [[wikilink]] is usually someone
# showing the syntax rather than pointing at a document — linkifying that would
# rewrite an example into a link. `pre` and `a` are excluded everywhere: a
# fenced block is code shown as code, and an anchor inside an anchor is invalid.


def _linkify_file_paths(marked_up: str, exist_cache: "dict[str, bool]",
                        cwd: "str | None") -> str:
    """Add deck-open anchors to path-shaped tokens in already-rendered HTML prose.

    Walks the tag structure (skipping the tags in _NO_PATH_LINKIFY) and anchors
    any path-shaped token whose resolved absolute path passes os.path.lexists().
    lexists rather than exists so a broken symlink still links instead of
    vanishing — the spec requirement.

    `exist_cache` is shared across the whole render so a path that appears twenty
    times is checked once.  `cwd` is the session working directory, used to
    resolve relative-path candidates; None disables relative-path linking.
    """
    depth = {t: 0 for t in _NO_PATH_LINKIFY}
    out: "list[str]" = []
    pos = 0

    def inside() -> bool:
        return any(v > 0 for v in depth.values())

    def _sub(m: "re.Match") -> str:
        raw = m.group(0)
        stripped = raw.rstrip(_PATH_SENTENCE_TAIL)
        tail = raw[len(stripped):]

        if stripped.startswith("~/") or stripped == "~":
            expanded = os.path.expanduser(stripped)
        else:
            expanded = stripped

        if not expanded.startswith("/"):
            if not cwd:
                return raw
            expanded = os.path.normpath(os.path.join(cwd, expanded))
        else:
            expanded = os.path.normpath(expanded)

        if expanded not in exist_cache:
            exist_cache[expanded] = os.path.lexists(expanded)
        if not exist_cache[expanded]:
            return raw

        ref_encoded = urllib.parse.quote(expanded, safe="")
        href = f"deck-open:?origin=path&ref={ref_encoded}"
        return f"<a href='{html.escape(href)}'>{stripped}</a>{tail}"

    for tag in _TAG.finditer(marked_up):
        segment = marked_up[pos:tag.start()]
        out.append(segment if inside() else _PATH_RE.sub(_sub, segment))
        name = tag.group(2)
        if name in depth:
            depth[name] = max(0, depth[name] - 1) if tag.group(1) else depth[name] + 1
        out.append(tag.group(0))
        pos = tag.end()
    tail_text = marked_up[pos:]
    out.append(tail_text if inside() else _PATH_RE.sub(_sub, tail_text))
    return "".join(out)


_INLINE = (
    (re.compile(r"`([^`\n]+)`"), r"<code>\1</code>"),
    (re.compile(r"\*\*([^*\n]+)\*\*"), r"<strong>\1</strong>"),
    (re.compile(r"(?<![*\w])\*([^*\n]+)\*(?!\w)"), r"<em>\1</em>"),
    (re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)"), _link_sub),
)


def _inline_md(escaped: str) -> str:
    """Inline markdown over ALREADY-ESCAPED text.

    Order matters: escaping first means any markup in the source is inert, and
    these patterns only ever introduce tags we chose. Applying markdown to raw
    text and escaping after would either undo the tags or reopen XSS.
    """
    for pat, rep in _INLINE:
        escaped = pat.sub(rep, escaped)
    # Last, because it needs the other rules' tags in place to know what is
    # already a link and what is code.
    return _linkify_bare(escaped)



# A markdown table: a header row of pipe-separated cells, then a delimiter row
# of dashes, then body rows. Matched on the DELIMITER, because a lone line
# containing pipes is far more often prose than a table — `a | b` in a sentence,
# or a command line with a pipe in it.
# THE PIPE IS REQUIRED, and leaving it optional was a bug: without it a bare
# `---` matched, so any prose line containing a pipe followed by a horizontal
# rule became a one-row table with an empty body, and the rule was swallowed.
# Every real GFM delimiter has a pipe — `|---|`, `---|---`, `|:---|---:|`. The
# pipe-less single-column form is given up deliberately: it is ambiguous with a
# setext heading, and losing it costs less than turning prose into tables.
_TABLE_DELIM = re.compile(
    r"^\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)+\|?$|^\|\s*:?-{1,}:?\s*\|$")


def _table_cells(line: str) -> list[str]:
    """The cells of one row, with the outer pipes stripped.

    A trailing backslash escapes a pipe inside a cell, which is how a table
    carries a literal `|`.
    """
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    cells, current, escaped = [], [], False
    for ch in line:
        if escaped:
            current.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    cells.append("".join(current).strip())
    return cells


def _alignments(delim: str) -> list[str]:
    """Per-column alignment from the delimiter row's colons."""
    out = []
    for cell in _table_cells(delim):
        left, right = cell.startswith(":"), cell.endswith(":")
        out.append("center" if left and right else
                   "right" if right else
                   "left" if left else "")
    return out


def render_markdown(text: str) -> str:
    """Enough markdown to read prose: fences, headings, lists, quotes, inline.

    Deliberately small and dependency-free. The assistant writes markdown and the
    first version rendered it raw, so `**bold**` and bullet dashes showed as
    literal punctuation — which is precisely the noise this view exists to cut.
    """
    lines = text.split("\n")
    out, in_fence, in_list = [], False, False
    para: list[str] = []
    skip_until = -1

    def flush_para() -> None:
        """Emit the buffered prose lines as ONE paragraph.

        Every line used to get its own `<p>`, so a wrapped paragraph became four
        paragraphs, each carrying the stylesheet's paragraph margin. Stacked
        with `.body`'s `white-space: pre-wrap` — which turned the newline
        BETWEEN those tags into a visible break as well — that is where the
        oversized gaps in the pane came from. Joining with a space also lets the
        text reflow to the pane's width instead of keeping the model's own line
        breaks, which were measured for a terminal.
        """
        if para:
            out.append(f"<p>{_inline_md(' '.join(para))}</p>")
            para.clear()

    for index, raw in enumerate(lines):
        if index < skip_until:
            continue
        if raw.strip().startswith("```"):
            if in_fence:
                out.append("</code></pre>")
            else:
                flush_para()
                if in_list:
                    out.append("</ul>"); in_list = False
                out.append("<pre><code>")
            in_fence = not in_fence
            continue
        if in_fence:
            out.append(html.escape(raw))
            continue

        esc = html.escape(raw)
        stripped = esc.strip()

        m = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        bullet = re.match(r"^[-*+]\s+(.*)$", stripped)
        numbered = re.match(r"^\d+\.\s+(.*)$", stripped)

        # A TABLE IS RECOGNISED BY THE ROW UNDER ITS HEADER, not by pipes.
        # A line with pipes in it is usually prose — `a | b` in a sentence, a
        # shell pipeline in a code span — so the delimiter row is what says a
        # table was meant. Before this, a table fell through to the paragraph
        # buffer and its rows were joined with spaces into one line: "just a
        # glob", as the operator put it, which the terminal rendered fine.
        if (index + 1 < len(lines) and "|" in stripped
                and _TABLE_DELIM.match(html.unescape(lines[index + 1].strip()))):
            flush_para()
            if in_list:
                out.append("</ul>"); in_list = False
            aligns = _alignments(html.unescape(lines[index + 1].strip()))

            def cell(tag: str, value: str, column: int) -> str:
                align = aligns[column] if column < len(aligns) else ""
                style = f" style='text-align:{align}'" if align else ""
                return f"<{tag}{style}>{_inline_md(value)}</{tag}>"

            head = _table_cells(esc)
            rows = ["<tr>" + "".join(cell("th", c, i) for i, c in enumerate(head)) + "</tr>"]
            body = index + 2
            while body < len(lines) and "|" in lines[body] and lines[body].strip():
                cells = _table_cells(html.escape(lines[body]))
                rows.append("<tr>" + "".join(cell("td", c, i)
                                             for i, c in enumerate(cells)) + "</tr>")
                body += 1
            out.append("<table><thead>" + rows[0] + "</thead><tbody>"
                       + "".join(rows[1:]) + "</tbody></table>")
            skip_until = body
            continue

        if bullet or numbered:
            flush_para()
            if not in_list:
                out.append("<ul>"); in_list = True
            out.append(f"<li>{_inline_md((bullet or numbered).group(1))}</li>")
            continue
        if in_list:
            out.append("</ul>"); in_list = False

        if m:
            flush_para()
            lvl = min(len(m.group(1)) + 2, 6)
            out.append(f"<h{lvl}>{_inline_md(m.group(2))}</h{lvl}>")
        elif stripped.startswith("&gt; "):
            flush_para()
            out.append(f"<blockquote>{_inline_md(stripped[5:])}</blockquote>")
        elif not stripped:
            # A blank line is the paragraph break, and now it is the ONLY one.
            flush_para()
        else:
            para.append(stripped)
    flush_para()
    if in_list:
        out.append("</ul>")
    if in_fence:
        out.append("</code></pre>")
    return _linkify_wikilinks("\n".join(out))


# A CSS hex colour and nothing else. The value lands inside a `<style>` block,
# where an unvalidated string could close the block and open a script tag, so
# this is an allowlist of the one shape the caller is allowed to send.
_HEX_COLOUR = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})\Z")

# How far the caller may scale the page's text. Bounded rather than trusted: the
# value lands in a CSS declaration, and a caller that computes it from a font
# size could send something absurd on a bad config. Half to triple covers every
# useful reading size and refuses the rest.
MIN_SCALE, MAX_SCALE = 0.5, 3.0

# How far the caller may open up the prose's leading. Bounded for the same
# reason the scale is, and narrower: line-height is a ratio whose useful range
# for running text is small. Below ~1.2 descenders collide with the next line's
# ascenders; past ~2.4 a paragraph stops reading as one block.
MIN_LINE_HEIGHT, MAX_LINE_HEIGHT = 1.2, 2.4


def _validated_line_height(value: "float | None") -> "float | None":
    """A prose line-height ratio, or None to leave the shipped leading alone."""
    if value is None:
        return None
    try:
        height = float(value)
    except (TypeError, ValueError):
        return None
    # NaN fails both comparisons, so this rejects it without naming it — the
    # same shape `_validated_scale` uses one function up.
    if not (MIN_LINE_HEIGHT <= height <= MAX_LINE_HEIGHT):
        return None
    return height


def _validated_scale(scale: "float | None") -> "float | None":
    """A text-scale multiplier, or None to leave the shipped size alone."""
    if scale is None:
        return None
    try:
        value = float(scale)
    except (TypeError, ValueError):
        return None
    # NaN fails both comparisons, so this rejects it without naming it.
    if not (MIN_SCALE <= value <= MAX_SCALE):
        return None
    return value


def _validated_transcript_lines(value: "int | None") -> "int | None":
    """A non-negative row count for the transcript-lines meta, or None to omit it.

    Same posture as `_validated_scale`/`_validated_line_height` above: the value
    is interpolated into `content="..."` unescaped, so anything other than a
    non-negative int is refused rather than emitted.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _summary_label(row: dict) -> str:
    """HTML for the summary text of a collapsed row.

    For tools on the anchor allowlist, wraps the argument in a deck-open anchor
    so Deck can intercept the click and open the referenced file or document.
    The visible text is unchanged; only the argument portion gains an `<a href>`.

    For all other rows the label is HTML-escaped verbatim, matching today's
    behaviour. Degrades silently on any missing or unexpected field.
    """
    origin = row.get("tool_origin")
    full_ref = row.get("tool_ref")
    if not origin or not isinstance(full_ref, str) or not full_ref:
        return html.escape(row.get("label", ""))
    tool_name = row.get("tool_name", "")
    tool_arg = row.get("tool_arg", "")
    if not tool_name or not tool_arg:
        return html.escape(row.get("label", ""))
    ref_encoded = urllib.parse.quote(full_ref, safe="")
    href = f"deck-open:?origin={origin}&ref={ref_encoded}"
    return (f"{html.escape(tool_name)}(<a href='{html.escape(href)}'>"
            f"{html.escape(tool_arg)}</a>)")


def _render_row(r: dict, exist_cache: "dict[str, bool]", cwd: "str | None") -> str:
    """The HTML fragment for one visible row, as render_html emits it.

    Extracted so render_delta can render individual rows to the same markup
    without duplicating the logic.  `data-k` appears on both FULL and COLLAPSED
    rows: FULL rows need it so the append bridge can find and replace them by key.
    """
    key = r.get("key")
    k_attr = f' data-k="{html.escape(str(key), quote=True)}"' if key else ""
    if r["display"] == FULL:
        cls = "you" if r["kind"] == "prompt" else "said"
        # `held` is an EXTRA class, never a substitute for `you` — the contract's
        # own rule ("a consumer must treat an unknown class on a turn as
        # cosmetic") is what lets this ship without a Deck-side change: an old
        # client renders a held row exactly like an ordinary "you" turn, and
        # only a client that knows the class dims it.
        if r.get("held"):
            cls += " held"
        body_html = _linkify_file_paths(render_markdown(r["body"]), exist_cache, cwd)
        return (f"<div class='turn {cls}'{k_attr}>"
                f"<div class=who>{html.escape(r['label'])}</div>"
                f"<div class=body>{body_html}</div></div>")
    # The result leads: expanding a call is nearly always asking what came back,
    # not what was sent.
    inner = ""
    result = (r.get("result") or "").strip()
    if result:
        inner += f"<pre class=res>{html.escape(result[:20000])}</pre>"
    body = (r.get("body") or "").strip()
    if body:
        inner += f"<pre class=arg>{html.escape(body)}</pre>"
    mark = " class=err" if r.get("error") else ""
    return (f"<details{mark}{k_attr}><summary>{_summary_label(r)}"
            f"{' ⚠' if r.get('error') else ''}</summary>{inner}</details>")


def render_html(rows: list[dict], title: str, theme: str | None = None,
                background: str | None = None, scale: "float | None" = None,
                line_height: "float | None" = None,
                cwd: "str | None" = None,
                transcript_lines: "int | None" = None,
                heading: bool = True) -> str:
    """Rows to a self-contained page. No external assets, no build step.

    `heading=False` leaves out the `<h1>` title. Deck's conversation pane sits
    under a status bar that already names the session, so the heading repeated
    it (the operator, 2026-10-06); `_render_tmux` and `render_delta`, Deck's two entry
    points, pass it. The `<title>` stays either way.

    `theme` is the appearance the caller has already resolved, `light` or
    `dark`. It is written onto `<html>` so the stamp arrives in the same
    document as the content it styles — Deck's rule is that the shell decides
    once and every surface is told in the same frame, and a page that had to be
    corrected by a later script would flash the wrong appearance first.

    Omitting it emits no `<html>` tag at all, which is the pre-existing
    behaviour and leaves the page following the system. That is right for a
    browser, where nobody is the authority.

    Anything other than the two known values is ignored rather than written,
    since the attribute would otherwise be an unvalidated string on the root
    element.

    `background` is the page's canvas colour, and Deck sends it for the same
    reason it sends `theme`: the shell owns the appearance, and only the shell
    knows what the terminal beside this pane is painted. The renderer cannot
    derive it — it is per-session, user-remappable, and lives in Deck's theme
    catalog. Ignored unless it is a CSS hex colour, because it is interpolated
    into a `<style>` block.

    `transcript_lines`, when given, is stamped as `<meta name=transcript-lines
    content=N>` so Deck's append-bridge consumer can learn its starting delta
    cursor from a full render instead of fetching `--delta-since 0` — which
    returns every row just to read this same number off it. It goes in a
    `<meta>` rather than on `.turns` because `.turns` is only emitted when
    there is at least one visible row, and an empty conversation still needs a
    cursor. Omitted, no meta is emitted — every existing caller's output is
    unchanged. The caller MUST derive this from the same count render_delta
    reports for the same transcript (see `_rows_for_lines`); a value from
    anywhere else can disagree with the delta cursor and cause Deck to either
    re-apply rows it already has or silently skip rows it never got.

    Ignored unless it is a non-negative int, because it is interpolated into a
    `<meta ... content="...">` attribute unescaped.
    """
    # Quoted, though the closed set below contains no character that needs it.
    # The quotes make the attribute safe independent of that gate, so adding a
    # future theme value cannot turn into markup here by being forgotten.
    stamp = f'<html data-theme="{theme}">' if theme in ("light", "dark") else ""
    # `!important`, and it is load-bearing rather than lazy. The appearance
    # blocks in PAGE_CSS are `:root[data-theme="dark"]` and
    # `:root:not([data-theme])`, both specificity (0,2,0); a bare `:root` is
    # (0,1,0) and LOSES to them however late it appears, because source order
    # only decides ties. Without this the canvas applied in light mode and was
    # silently discarded in dark — the mode Deck actually runs in. Caught in
    # review, by reading the cascade rather than the test, which asserted source
    # order and passed.
    canvas = (f"<style>:root{{--bg:{background}!important;}}</style>"
              if background and _HEX_COLOUR.match(background) else "")
    # Scaling the ROOT font size, not each rule: every size on the page is
    # written relative to it (rem) or to its parent (em) — including `body`
    # itself, which was an absolute 15px until this landed — so one number moves
    # the whole document coherently and nothing has to be enumerated. The
    # collapsed rows scale with the prose, which is right — a reader who needs
    # bigger text needs it everywhere, not only in the dialogue.
    zoom = (f"<style>:root{{font-size:{_validated_scale(scale) * 100:.0f}%;}}</style>"
            if _validated_scale(scale) is not None else "")
    # A CUSTOM PROPERTY RATHER THAN A RULE, so the shipped 1.65 stays the
    # fallback written beside the declaration it belongs to, and an absent or
    # refused value changes nothing. The same shape `--bg` uses above.
    #
    # SEPARATE FROM `--scale` ON PURPOSE. Scale moves the whole document, prose
    # and tool output together; this opens the leading of running prose only,
    # because that is the surface a reader reads and code wants its own tighter
    # setting. Folding them into one control would mean choosing which of the
    # two a single number ruins.
    leading = (f"<style>:root{{--body-line-height:{_validated_line_height(line_height):.2f};}}</style>"
               if _validated_line_height(line_height) is not None else "")
    _lines = _validated_transcript_lines(transcript_lines)
    lines_meta = (f'<meta name="transcript-lines" content="{_lines}">'
                  if _lines is not None else "")
    parts = [
        f"<!doctype html>{stamp}<meta charset=utf-8>{lines_meta}",
        f"<title>{html.escape(title)}</title>",
        "<meta name=viewport content='width=device-width,initial-scale=1'>",
        f"<style>{PAGE_CSS}</style>{canvas}{zoom}{leading}",
        f"<h1>{html.escape(title)}</h1>" if heading else "",
    ]
    visible = [r for r in rows if r["display"] != HIDDEN]
    if not visible:
        parts.append("<p class=empty>nothing to show yet.</p>")
    # The turns go in their own container, collected separately so they can be
    # emitted in REVERSE. Paired with `flex-direction: column-reverse` that puts
    # the newest turn at the start of the flow — which is where a fresh document
    # opens — while the visual order still reads oldest-first.
    exist_cache: "dict[str, bool]" = {}
    turns: list[str] = []
    for r in visible:
        turns.append(_render_row(r, exist_cache, cwd))
    # THE PAGE OPENS AT THE NEWEST TURN, not the oldest. Deck reloads this whole
    # document whenever the conversation changes, and a fresh document starts at
    # the top — so sending a message scrolled the reader back to the beginning.
    #
    # Done with layout rather than focus. The turns are emitted in reverse inside
    # a `column-reverse` flex container: the flow therefore starts at the newest
    # turn, which is where the document opens, and the visual order is unchanged
    # because the reversal cancels out.
    #
    # It was an `autofocus` anchor, and that stole the composer's first
    # responder on every render — keystrokes stopped landing in the message box
    # and macOS beeped at each one. Reported by the operator while typing into a live
    # session. No element is focused now.
    if turns:
        parts.append("<div class=turns>" + "".join(reversed(turns)) + "</div>")
    return "\n".join(parts)


def render_delta(path: Path, cursor: int, title: str,
                 theme: "str | None" = None, background: "str | None" = None,
                 scale: "float | None" = None, line_height: "float | None" = None,
                 cwd: "str | None" = None) -> dict:
    """The incremental update since cursor, for the session pane append bridge.

    Returns one of:
      {"lines": N, "full": False, "rows": [...], "removed": ["key", ...]}
      {"full": True, "page": "<the whole page>"}

    `cursor` is the number of transcript lines the client has already rendered.
    `rows` is in transcript order (oldest first) with held rows last; each entry
    carries the same markup _render_row emits for the full page. A held row also
    carries `newest: True`, meaning "place this at the newest end wherever it
    currently sits" — see the flag's own comment below. Absent on every other
    row, where the transcript already fixes the position. `removed` is
    every held row key the client is currently showing that release or a queue
    edit has invalidated — always present, `[]` when nothing needs removing.

    THE REWIND RULE: "every row after the cursor, plus every earlier row a fold
    mutated where the mutating tool_result arrived after the cursor."  fold_results
    merges a tool_result into the tool_use it answers, so a call that is already
    on the client's page changes when its result arrives on a later line.  A naive
    suffix (lines >= cursor only) would leave that call permanently stale.
    _rows_for_lines computes this exactly via _result_line, so the delta is precise
    rather than a fixed-window approximation whose failure mode is a silent stale row.

    The escape hatch (full: True) fires when cursor > total lines — a sign the
    transcript was rotated or truncated since the last poll.  Deck treats it the
    same as today's full render, making the worst case current behaviour.

    HELD ROWS RIDE ALONG, because Deck's steady state is THIS path, not full
    render. `SessionPaneModel` applies a delta whenever a cursor already exists
    — which is whenever a reader has the pane open — so a queued message that
    only ever reached the page through `held_rows_for`'s call in `read_rows` was
    invisible in exactly the case the feature exists for: a working session,
    the entire use case. That was Slice D's scoping, not an oversight in it —
    the append bridge could only replace or insert a row, never remove one, and
    a held row must vanish on release. Now that Deck's bridge can remove a row
    by key, that gap closes here.

    THE REMOVAL LIST IS COMPUTED, NOT GUESSED. The cursor is a count of
    transcript lines, and says nothing on its own about which held keys are on
    the client's page — a queue can drain (a `dequeue`/`popAll` record) without
    changing what conversational rows exist, so line position alone cannot tell
    "was held" from "still held." But the transcript is append-only JSONL and
    `held_rows_for` is a pure function of the lines it is given, and every
    caller of this function — full render and delta alike — stamps `cursor`
    (the `lines` field here, `transcript_lines` in `render_html`'s meta) as the
    exact total line count read at THAT call. So `raw_lines[:cursor]` is not an
    approximation of what the client last saw; it IS the file this same
    function read to build the response the client is currently showing.
    Running `held_rows_for` over that identical prefix reconstructs the
    client's current held rows exactly, and the keys present there but absent
    from the fold over the WHOLE file now are what changed underneath the
    client — those go in `removed`.

    THE RECONSTRUCTION ASSUMES EACH ALREADY-COUNTED LINE IS BYTE-IDENTICAL ON
    A LATER READ — true only if Claude Code appends each transcript line
    atomically. Nothing here asserts that; the pre-existing rewind rule above
    only ever compares line INDICES against `cursor`, never re-reads and diffs
    content below it, so this is a new reliance the held-row reconstruction
    introduces. A read that raced a torn write on the final line before the
    cursor could make one poll's `removed` list wrong; harmless in isolation,
    since the next poll re-reads the now-complete line and self-corrects
    rather than accumulating the error.

    THE CURRENT HELD SET IS ALWAYS RESENT IN FULL, in `rows`, rather than
    diffed down to only what is new. A `~held.<index>.<digest>` key encodes
    both content and queue position, so an earlier entry draining renumbers
    every key after it even though the message it names is unchanged — a
    same-key optimisation would still have to resend most of the set on any
    queue edit, and Slice A measured a working queue at one or two messages, so
    resending the rest costs nothing a diff would meaningfully save.

    WHEN THE QUEUE STATE IS UNKNOWN ON EITHER SIDE OF THE RECONSTRUCTION, this
    takes the full-page escape hatch rather than diffing. `held_rows_for`
    returns `None` — not `[]` — when it cannot resolve the queue (`session_
    state` unavailable, or an ambiguous fold), and `[]` is not a safe stand-in
    here the way it is for a full render: `[]` drives `removed`, so treating
    "unknown" as "empty" would tell the client to remove held rows it should
    leave alone, or fail to remove ones it should — reaching the "worse than
    showing nothing" state the plan warns about, just via `_SS` flipping
    between polls rather than the queue-drain race the escape hatch already
    guards. Routing to the full page is correct rather than merely convenient:
    it replaces the whole document, so it clears or redraws every held row by
    construction and needs no reconciliation of its own.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        # No lines were read, so 0 is the honest count — same as the
        # cursor-past-EOF branch below, which must also stamp the meta so a
        # pane that lands here still gets a cursor instead of going back to
        # --delta-since 0.
        return {"full": True, "page": render_html(
            [], title, theme=theme, background=background,
            scale=scale, line_height=line_height, cwd=cwd,
            transcript_lines=0, heading=False)}

    raw_lines = text.splitlines()
    total = len(raw_lines)
    # Deck only echoes back a cursor the mini gave it, so a negative value is
    # latent rather than live — but clamp it here so the invariant holds even
    # if a caller constructs one by hand.
    cursor = max(0, cursor)

    if cursor > total:
        # Same convention as read_rows: held rows are appended last, since
        # they are chronologically the newest content on the page. `or []`:
        # nothing on this page to reconcile against, so an unknown queue state
        # draws no held rows rather than failing the whole render.
        rows = rows_for(raw_lines) + (held_rows_for(raw_lines) or [])
        return {"full": True, "page": render_html(
            rows, title, theme=theme, background=background,
            scale=scale, line_height=line_height, cwd=cwd,
            transcript_lines=total, heading=False)}

    line_count, all_rows = _rows_for_lines(raw_lines)

    # See the docstring: raw_lines[:cursor] is exactly what the client's
    # current held rows were built from, so this reconstructs them rather than
    # guessing at them. Checked BEFORE building the delta rows below: `None`
    # on either side means the reconstruction cannot be trusted, and the
    # full-page escape hatch is the honest answer, not a diff over a guess.
    held_before, _ = _held_rows(raw_lines[:cursor])
    held_now, now_authoritative = _held_rows(raw_lines)
    if held_before is None or held_now is None:
        rows = all_rows + (held_now or [])
        return {"full": True, "page": render_html(
            rows, title, theme=theme, background=background,
            scale=scale, line_height=line_height, cwd=cwd,
            transcript_lines=total, heading=False)}

    exist_cache: "dict[str, bool]" = {}
    rendered = []
    for r in all_rows:
        if r["display"] == HIDDEN:
            continue
        src = r.get("_line", 0)
        res_src = r.get("_result_line")
        if src >= cursor or (res_src is not None and res_src >= cursor):
            rendered.append({"key": r["key"],
                             "html": _render_row(r, exist_cache, cwd)})

    # ABSENCE IS ONLY DEPARTURE WHEN THE WINDOW SAW THE WHOLE SESSION, and this
    # guard is the second half of the `window_complete` work. `fold_queue`'s own
    # refusal covers a record it cannot resolve; it cannot cover a record it
    # never saw. An `enqueue` that scrolls past `_HELD_TAIL_BYTES` with no later
    # operation on it leaves the fold short by one, with nothing to trip on, and
    # the fold over the client's shorter prefix still holds that message — so
    # differencing the two would name a still-queued message as removed and tell
    # the client to erase it. Reported by security review with a repro.
    #
    # SUPPRESSING THE REMOVAL IS RIGHT RATHER THAN MERELY SAFE, and the three
    # cases are worth walking because "leave the row alone" sounds like it just
    # trades one wrong answer for another:
    #
    #   * The message aged out and is STILL QUEUED — the case above. Keeping the
    #     row is the correct answer, not a lesser evil.
    #   * The message was RELEASED and the fold resolved that cleanly. It is
    #     then synced — either the window was complete, or the `popAll` that
    #     released it established sync — so the diff runs and the row goes.
    #   * The message was RELEASED by a `remove` the fold cannot resolve. That
    #     returns None and takes the full-page hatch above, which clears the row.
    #
    # AN EARLIER VERSION GATED ON THE WINDOW RATHER THAN ON SYNC, and stranded a
    # row in the second case: a `popAll` release in an over-cap transcript resolved
    # cleanly, but the coarse flag still said "truncated", so no removal was
    # emitted and the client kept a "you (queued)" ghost beside the delivered
    # turn. Both reviewers found it, and the comment here claimed the case did
    # not exist. `synced` is the signal that distinguishes them.
    #
    # ONE SUB-CASE IS NOT RESOLVED HERE, and it is worth being exact about what
    # that does and does not mean, because the previous two versions of this
    # comment were each too confident in a different direction.
    #
    # When the queue's entire history scrolls past `_HELD_TAIL_BYTES` — the
    # `enqueue` and its release both — this fold sees no queue records, so
    # "released while out of window" and "still queued, aged out" read the same
    # TO THE FOLD. Keeping the row is right in the second world and leaves a
    # ghost in the first; erasing inverts which world it gets wrong, and erasing
    # a live message is the worse of the two.
    #
    # THE FOLD IS NOT THE ONLY THING THAT KNOWS, WHICH THE LAST VERSION OF THIS
    # COMMENT DENIED. A row can only be a removal candidate if `held_before`
    # holds it, so it was still queued as of `cursor`, so any release of it
    # happened after `cursor` — and `raw_lines[cursor:]`, which this function
    # already holds UNCAPPED for the rewind rule, therefore contains that
    # release record however far it has scrolled from the current end of file.
    # Reading it there would settle most of this case. That is
    # `issue_01KZP746HJJY3VAS6BJRMSMGYD`, deliberately not done here: it wants
    # its own care around two held rows sharing text, and this change is already
    # four review cycles deep.
    #
    # Until then the ghost needs more than `_HELD_TAIL_BYTES` of transcript
    # between two polls to appear, which at 8MB is a great deal.
    #
    # A FULL-PAGE RENDER IS NOT THE SAFETY NET IT LOOKS LIKE, and saying it was
    # is the third time a comment here has claimed more than it could. Security
    # review checked it: with the queue's history outside the tail, a full page
    # draws NO held rows, because `read_rows` collapses the unknown to `[]`. In
    # the released world that clears the ghost, and in the still-queued world it
    # erases a live message — the outcome this same comment calls the worse of
    # the two, arriving by the path offered as the remedy. Pre-existing in
    # `held_rows_for`'s truncation rather than introduced here, and filed.
    held_now_keys = {r["key"] for r in held_now}
    removed = ([r["key"] for r in held_before if r["key"] not in held_now_keys]
               if now_authoritative else [])
    for r in held_now:
        # `newest: True` — THIS ROW BELONGS AT THE BOTTOM, EVERY TIME.
        #
        # A held row is the one row whose position is not fixed by the
        # transcript. Every other row sits where its line put it, so a client
        # that replaces it in place is right; a held row is not in the
        # transcript at all and must stay below all of it, because it is the
        # thing that has not happened yet.
        #
        # Without this the client had no way to tell: it replaces a row it
        # already has by key, in the DOM slot it already occupies, while new
        # transcript rows insert at the newest end — so each new row from the
        # agent slid in underneath the held message and pushed it one position
        # further up. One long assistant turn carried it off the top of the
        # viewport, which reads as the message disappearing. Observed by hand
        # 2026-08-10.
        #
        # A flag rather than the client keying off the `~held.` prefix: the key
        # shape is this module's private business, and `platform/contracts/
        # session-page-markup` promises only a charset for it, not a grammar.
        rendered.append({"key": r["key"], "html": _render_row(r, exist_cache, cwd),
                         "newest": True})

    return {"lines": line_count, "full": False, "rows": rendered, "removed": removed}


def _find_node() -> str:
    """Return the absolute path to a usable node binary.

    Probes a short list of absolute candidates because session-view is invoked
    over a non-interactive SSH session where PATH does not carry Homebrew's bin.
    Raises NodeMissingError (a named, visible error) rather than returning None
    or falling back silently — a silent fallback would reinstate the rendering
    divergence this bridge exists to remove.
    """
    override = os.environ.get(_NODE_ENV_VAR, "").strip()
    if override and os.path.isfile(override) and os.access(override, os.X_OK):
        return override
    candidates = _node_candidates()
    for candidate in candidates:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    checked = ", ".join(candidates)
    raise NodeMissingError(
        f"Node.js not found; checked: {checked}. "
        f"Install Node.js or set {_NODE_ENV_VAR} to its absolute path."
    )


def _read_js_module(path_in_repo: str) -> "tuple[str, str]":
    """Read a JS module from origin/main in the knowledge repo.

    Returns (content, blob_sha). The sha travels into the rendered page as an
    HTML comment so a mis-rendered document can be traced to a renderer version.
    Reads from the committed ref — never the working tree — matching the same
    invariant kb_checkout uses.

    Raises RuntimeError when the knowledge repo or the file cannot be read.
    """
    # Resolved to the exact remote-tracking ref's sha (git_refs): a same-named tag
    # or branch, or a tag standing in for an absent ref, would otherwise put its
    # JS into the rendered page.
    main_sha = git_refs.origin_main_sha(_KNOWLEDGE_REPO)
    if main_sha is None:
        raise RuntimeError(f"{git_refs.ORIGIN_MAIN_REF} does not exist in {_KNOWLEDGE_REPO}")
    ref = f"{main_sha}:{path_in_repo}"
    try:
        content = subprocess.check_output(
            ["git", "-C", str(_KNOWLEDGE_REPO), "show", ref],
            stderr=subprocess.PIPE,
        ).decode("utf-8")
        sha = subprocess.check_output(
            ["git", "-C", str(_KNOWLEDGE_REPO), "rev-parse", ref],
            stderr=subprocess.PIPE,
        ).decode("utf-8").strip()
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace").strip() if exc.stderr else ""
        raise RuntimeError(
            f"Failed to read origin/main:{path_in_repo} from {_KNOWLEDGE_REPO}: {stderr}"
        ) from exc
    return content, sha


def _wikilink_map(source: str) -> "dict[str, str]":
    """Pre-resolve all [[target]] targets in source to KB doc ids.

    Scans the raw Markdown source for [[...]] targets and resolves each
    through the existing _resolve_wikilink_target, which queries the KB index.
    Returns a {name: docId} dict. Unresolvable targets are absent so the
    renderer falls back to literal [[name]] text — the same behaviour
    _linkify_wikilinks produces.

    Scanning raw source may over-include targets inside code spans or fences,
    but extra map entries are harmless: the renderer only consults the map for
    parsed wikilink nodes, which code spans never produce.
    """
    resolved: "dict[str, str]" = {}
    # Every DISTINCT name costs one index query, so both of the following are
    # load-bearing under a hostile document, and neither substitutes for the
    # other. `seen` covers repetition — only successes used to be remembered, so
    # the same broken link a thousand times was a thousand queries. The cap
    # covers variety, which repetition-memoing cannot: measured at 11.3s for
    # 100k distinct targets, and this phase runs BEFORE the bridge, so the
    # subprocess timeout does not bound it.
    seen: "set[str]" = set()
    for m in _WIKILINK_RE.finditer(source):
        name = m.group(1).strip()
        if name in seen:
            continue
        if len(seen) >= _WIKILINK_RESOLVE_CAP:
            break
        seen.add(name)
        doc_id = _resolve_wikilink_target(name)
        if doc_id is not None:
            resolved[name] = doc_id
    return resolved


def _render_via_bridge(source: str, wikilink_map: "dict[str, str]") -> str:
    """Render a Markdown document through the Node/md-serial bridge.

    Writes the JS modules to a temp directory (as .mjs files so Node recognises
    them as ES modules regardless of the surrounding package.json), passes the
    job as JSON on STDIN — never on the command line — and returns the rendered
    HTML body including a leading HTML comment with the blob shas.

    Raises NodeMissingError when Node cannot be found.
    Raises RuntimeError when the knowledge repo is unreachable or the bridge fails.
    """
    node = _find_node()
    md_serial_content, md_serial_sha = _read_js_module("scripts/md-serial/md-serial.js")
    render_content, render_sha = _read_js_module("scripts/md-serial/render.js")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        md_serial_path = tmpdir_path / "md-serial.mjs"
        render_path = tmpdir_path / "render.mjs"
        md_serial_path.write_text(md_serial_content, encoding="utf-8")
        render_path.write_text(render_content, encoding="utf-8")

        job = json.dumps({
            "source": source,
            "wikilinkMap": wikilink_map,
            "blobShas": {"mdSerial": md_serial_sha, "render": render_sha},
        })

        try:
            result = subprocess.run(
                [node, str(_BRIDGE_SCRIPT), str(md_serial_path), str(render_path)],
                input=job.encode("utf-8"),
                capture_output=True,
                timeout=_BRIDGE_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as exc:
            # TimeoutExpired derives from SubprocessError, NOT from RuntimeError
            # or OSError, so it would sail past both CLI handlers and reach the
            # viewer as a traceback. Re-raise as the RuntimeError those handlers
            # already expect.
            raise RuntimeError(
                f"md_render_bridge timed out after {_BRIDGE_TIMEOUT_S}s"
            ) from exc

    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"md_render_bridge failed (exit {result.returncode}): {stderr}")

    return result.stdout.decode("utf-8")


# --- editor page builder ------------------------------------------------------

def _escape_for_script(value) -> str:
    """JSON-encode value and replace every < with \\u003c.

    json.dumps with ensure_ascii=True (the default) already escapes U+2028 and
    U+2029. Replacing < removes the only character that lets document-controlled
    content break out of a <script> block as </script> or <!-- sequences.

    The property a caller should assert: the result contains no < character.
    """
    return json.dumps(value, ensure_ascii=True).replace('<', '\\u003c')


class BundleError(RuntimeError):
    """Raised when the JS module bundle fails a safety guard.

    Emitting a broken page is worse than raising, so both guards in
    _flatten_js_modules use this rather than logging a warning.
    """


# Matches top-level JS declarations at column 0 (unindented) in a concatenated
# ES module bundle.  Top-level declarations in real ES modules are always at
# column 0; nested ones are indented, so the ^ anchor in MULTILINE mode is an
# accurate discriminator for the files this function handles.
_TOP_LEVEL_DECL_RE = re.compile(
    r'^(?:async\s+)?(?:function\s*\*?\s*|class\s+|const\s+|let\s+|var\s+)'
    r'([A-Za-z_$][A-Za-z0-9_$]*)',
    re.MULTILINE,
)

# Guard 1 uses this to catch any surviving import/export at a word boundary —
# 'export{a,b}' and 'import{foo}' have no space after the keyword, so a
# startswith(' ') check misses them.
_SURVIVING_MODULE_KW_RE = re.compile(r'^(import|export)\b')


def _flatten_js_modules(modules: "list[str]") -> str:
    """Flatten ES modules into a single script block in dependency order.

    Each module has its import lines dropped and its 'export ' prefix stripped
    from declarations.  The three md-serial modules (md-serial.js, render.js,
    editor.js) use this pattern exclusively: editor.js imports from the other
    two, all exports are named function declarations, and no re-export or
    namespace-export syntax appears.

    Three guards run on the concatenated result:
    1. No line starts with 'import' or 'export' at a word boundary — stripping
       or dropping every such line is what makes the bundle a valid non-module
       script.  The word-boundary check catches no-space forms ('export{a,b}',
       'import{foo}') that a trailing-space prefix misses.
    2. No top-level name is declared twice — the bundle is flattened into one
       scope, so a duplicate silently shadows and breaks the earlier definition.
    3. No '</script' anywhere in the bundle — that string closes the inline
       script block prematurely even when it appears inside a JS comment.

    Raises BundleError on guard failure rather than emitting a broken page.
    """
    parts = []
    for content in modules:
        lines = content.splitlines(keepends=True)
        out = []
        for line in lines:
            if line.startswith('import '):
                continue
            # Word boundary, not a trailing space. 'export default{...}' and a
            # tab-separated 'export default\t...' both skip a space-suffixed
            # test, fall through to the 'export ' strip below, and emit
            # 'default{...}' — broken output from the branch whose whole job is
            # refusing to emit broken output. This is the same gap the strip and
            # Guard 1 close for named imports and exports, so it closes the same
            # way. '\b' still declines to match 'export defaultThing', which is
            # an ordinary named export of a binding called defaultThing.
            if re.match(r'export default\b', line):
                # Refuse rather than strip. Flattening puts every module in one
                # scope, where a default export has no meaning to preserve, and
                # only ONE of its three forms survives the strip intact:
                # 'export default function foo()' becomes a valid declaration,
                # but 'export default function()' becomes an anonymous function
                # declaration, which is a SyntaxError at statement position, and
                # 'export default 42' becomes a bare expression that silently
                # drops the export. Stripping therefore emits a broken page for
                # two forms out of three, which is the outcome this function
                # exists to prevent.
                raise BundleError(
                    "flattening cannot express a default export: give it a name "
                    "and export it normally"
                )
            if line.startswith('export '):
                line = line[len('export '):]
            out.append(line)
        parts.append(''.join(out))

    bundle = '\n'.join(parts)

    # Guard 1: no line-initial import or export at a word boundary survived the
    # strip pass.  Word boundary catches 'export{a,b}' and 'import{foo}' which
    # a trailing-space prefix misses.
    for i, line in enumerate(bundle.splitlines(), 1):
        m = _SURVIVING_MODULE_KW_RE.match(line)
        if m:
            kw = m.group(1)
            raise BundleError(
                f"unexpected {kw!r} at line {i} after flattening — "
                f"update _flatten_js_modules to handle this pattern"
            )

    # Guard 2: no top-level name declared twice.
    seen: "set[str]" = set()
    for m in _TOP_LEVEL_DECL_RE.finditer(bundle):
        name = m.group(1)
        if name in seen:
            raise BundleError(
                f"duplicate top-level declaration '{name}' in flattened bundle — "
                f"a name added to one module now conflicts with another"
            )
        seen.add(name)

    # Guard 3: no script close tag anywhere in the bundle.  '</script' inside a
    # JS comment (e.g. '// handle the </script> edge case') closes the inline
    # script block early — the rest of the bundle and the bootstrap then render
    # as visible page text.  _escape_for_script already removes '<' from the
    # document content; this guard protects the module code itself.
    if re.search(r'</script', bundle, re.IGNORECASE):
        raise BundleError(
            "bundle contains '</script' which would close the inline script block "
            "prematurely — remove or escape the close tag in the module source"
        )

    return bundle


def _file_viewer_page(body_html: str, title: str, theme: str | None = None) -> str:
    """A self-contained HTML page for the Deck file viewer.

    Uses FILE_VIEWER_CSS which has no prefers-color-scheme fallback — the shell
    always stamps data-theme on this pane, so the page should never self-decide.
    """
    stamp = f'<html data-theme="{theme}">' if theme in ("light", "dark") else ""
    return "\n".join([
        f"<!doctype html>{stamp}<meta charset=utf-8>",
        f"<title>{html.escape(title)}</title>",
        "<meta name=viewport content='width=device-width,initial-scale=1'>",
        f"<style>{FILE_VIEWER_CSS}</style>",
        f"<h1>{html.escape(title)}</h1>",
        body_html,
    ])


def render_file_page(content: str, name: str, theme: str | None = None,
                     cwd: "str | None" = None) -> str:
    """A self-contained HTML page rendering a Markdown document for the Deck file viewer.

    Raises NodeMissingError when Node.js is unavailable.

    TAKES CONTENT RATHER THAN A PATH so a document that reached Deck without a
    second filesystem open — read once through session-agent's contained
    read-file verb and piped in on stdin — can be rendered without this
    command opening the path a second time itself.

    `cwd` resolves relative-path candidates in the prose for linkification;
    None (the stdin case, where no path is known) disables relative-path
    linking, matching `render_editor_page`'s stdin case.
    """
    wikilinks = _wikilink_map(content)
    body_html = _render_via_bridge(content, wikilinks)
    exist_cache: "dict[str, bool]" = {}
    body_html = _linkify_file_paths(body_html, exist_cache, cwd)
    return _file_viewer_page(body_html, name, theme=theme)


def render_file_html(path: Path, theme: str | None = None) -> str:
    """A self-contained HTML page rendering a Markdown file for the Deck file viewer.

    Raises OSError if the path is missing or unreadable.
    Raises NodeMissingError when Node.js is unavailable.

    Renders through the md-serial JavaScript renderer so one implementation
    handles all documents. The file's parent directory is the cwd for
    relative-path resolution.
    """
    content = path.read_text(encoding="utf-8", errors="replace")
    return render_file_page(content, path.name, theme=theme, cwd=str(path.parent))


KB_INDEX_PATH = Path.home() / ".knowledge-index" / "index.db"

# Viewing a doc reads from the index directly: open READ-ONLY (the sqlite
# URI `mode=ro`), SELECT by bound parameter, done. This is how mcp-kb-server's
# kb_get works and the posture to match.
#
# kb_checkout ACQUIRES A LEASE with a 4-hour TTL. Using it here would lock a
# doc against the next writer every time someone glances at it. Checkout stays
# for phase 4 (editing), which needs content_sha. Viewing and editing are
# deliberately different operations with different costs.


def _kb_connect() -> sqlite3.Connection:
    """Read-only connection to the knowledge index. URI mode enforces mode=ro."""
    uri = f"file:{KB_INDEX_PATH}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _kb_fetch(doc_id: str) -> "dict | None":
    """The title and body for a KB doc id, or None if not found.

    Reads from the index via a bound parameter — never string interpolation —
    matching the read-only posture mcp-kb-server uses for kb_get.
    """
    try:
        conn = _kb_connect()
    except sqlite3.OperationalError:
        return None
    try:
        row = conn.execute(
            "SELECT title, kind, project, status, body FROM docs WHERE id = ?",
            (doc_id,),
        ).fetchone()
    except sqlite3.DatabaseError:
        return None
    finally:
        conn.close()
    if row is None:
        return None
    return {
        "title": row["title"] or doc_id,
        "kind": row["kind"],
        "project": row["project"],
        "status": row["status"],
        "body": row["body"] or "",
    }


def _kb_slug_lookup(slug: str) -> "str | None":
    """The KB id whose basename matches slug, or None if not found or ambiguous.

    Queries for docs whose id ends in '/<slug>'. Returns None when zero or more
    than one document matches — ambiguity is left unlinked rather than guessed.
    """
    try:
        conn = _kb_connect()
    except sqlite3.OperationalError:
        return None
    try:
        # Escape LIKE wildcards so a slug containing % or _ matches literally.
        esc = slug.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = conn.execute(
            "SELECT id FROM docs WHERE id LIKE ? ESCAPE '\\'",
            (f"%/{esc}",),
        ).fetchall()
    except sqlite3.DatabaseError:
        return None
    finally:
        conn.close()
    return rows[0][0] if len(rows) == 1 else None


def _resolve_wikilink_target(target: str) -> "str | None":
    """The KB id a [[target]] resolves to, or None.

    A target containing '/' is a full KB id — verified to exist before linking.
    A bare target is a slug (the document's filename basename) resolved via
    _kb_slug_lookup. Returns None when the doc is not found, the slug is
    ambiguous, or the KB index is unavailable.
    """
    if not target:
        return None
    if "/" in target:
        return target if _kb_fetch(target) is not None else None
    return _kb_slug_lookup(target)


_WIKILINK_RE = re.compile(r"\[\[([^\[\]\n]+)\]\]")
_NO_WIKILINK = frozenset({"a", "code", "pre"})


def _linkify_wikilinks(marked_up: str) -> str:
    """Replace [[target]] with deck-open anchors in already-rendered HTML.

    Walks the tag structure to skip <a>, <code>, and <pre> — the same approach
    _linkify_bare uses. Fenced blocks and inline code spans are therefore immune.
    Unresolvable and ambiguous targets remain as literal [[target]] text.
    """
    depth = {t: 0 for t in _NO_WIKILINK}
    out, pos = [], 0

    def inside() -> bool:
        return any(v > 0 for v in depth.values())

    def _sub(m: "re.Match") -> str:
        # The match comes from already-escaped HTML; unescape before KB lookup
        # so a target like 'my &amp; doc' is looked up as 'my & doc'.
        target_esc = m.group(1).strip()
        target_raw = html.unescape(target_esc)
        doc_id = _resolve_wikilink_target(target_raw)
        if doc_id is None:
            return m.group(0)
        ref_encoded = urllib.parse.quote(doc_id, safe="")
        href = f"deck-open:?origin=kb&ref={ref_encoded}"
        return f"<a href='{html.escape(href)}'>{target_esc}</a>"

    for tag in _TAG.finditer(marked_up):
        segment = marked_up[pos:tag.start()]
        out.append(segment if inside() else _WIKILINK_RE.sub(_sub, segment))
        name = tag.group(2)
        if name in depth:
            depth[name] = max(0, depth[name] - 1) if tag.group(1) else depth[name] + 1
        out.append(tag.group(0))
        pos = tag.end()
    tail = marked_up[pos:]
    out.append(tail if inside() else _WIKILINK_RE.sub(_sub, tail))
    return "".join(out)


def render_kb_html(doc_id: str, theme: str | None = None) -> "str | None":
    """A self-contained HTML page rendering a KB document for the Deck viewer.

    Returns None when the doc is not found in the index or the index is
    unreadable; exits without emitting a page so the caller can surface a
    clean error rather than a blank one.

    The body is already frontmatter-free in the index — the indexer strips
    frontmatter before storing. The metadata (kind, project, status) is emitted
    as a compact HTML line above the body, then the body is rendered through the
    md-serial JavaScript renderer.

    Raises NodeMissingError when Node.js is unavailable.
    """
    doc = _kb_fetch(doc_id)
    if doc is None:
        return None

    meta_parts = []
    if doc.get("kind"):
        meta_parts.append(html.escape(doc["kind"]))
    if doc.get("project"):
        meta_parts.append(html.escape(doc["project"]))
    if doc.get("status"):
        meta_parts.append(html.escape(doc["status"]))
    meta_html = (f"<p class='md-kb-meta'>{' · '.join(meta_parts)}</p>"
                 if meta_parts else "")

    body = doc["body"]
    wikilinks = _wikilink_map(body)
    body_html = _render_via_bridge(body, wikilinks)
    return _file_viewer_page(meta_html + body_html, doc["title"], theme=theme)


def _save_click_handler_js() -> str:
    """The Save button's click handler, as JS text, for the editor page.

    Split out from render_editor_page's bootstrap so it can be executed and
    pinned in isolation: test_session_view.py stubs `editor`, `document` and
    `window.webkit` under Node and inspects what gets posted for every shape
    `saveChecked()` can return. It runs after a `const editor = mountEditor(...)`
    binding is already in scope.

    Deck refuses to sign whenever the posted `roundTrip.ok` is not `true`, so a
    missing or malformed verdict must normalise to `ok: false` rather than be
    dropped or passed through — see normaliseRoundTrip's strict `=== true`
    check below, and the two throw sites that post `ok: false` rather than
    leave a click unanswered. normaliseRoundTrip reads `ok` and `failures` as
    `roundTrip`'s own properties through the one `ownProp` helper (also used by
    resolveWikilink's WIKILINKS lookup in render_editor_page's bootstrap, which
    this text is appended to), not through the prototype chain, so a polluted
    `Object.prototype.ok` can't stand in for a verdict that was never
    computed. The same `ownProp` helper reads `source` and `roundTrip` off
    `saveChecked()`'s own return value, so a polluted `Object.prototype.source`
    or `Object.prototype.roundTrip` can't stand in for a value saveChecked()
    never returned. postSaveResult is the one place `ok: true` can reach
    Deck: it forces `ok: false` whenever `source` isn't a string, even if the
    verdict it was handed says `ok: true` — a saveChecked() bug that returns a
    non-string source must not post that source's absence as a pass.
    """
    return "\n".join([
        "function ownProp(obj, key) {",
        "  return (obj && typeof obj === 'object' &&",
        "      Object.prototype.hasOwnProperty.call(obj, key))",
        "    ? obj[key]",
        "    : undefined;",
        "}",
        "",
        "function normaliseRoundTrip(roundTrip, missingReason) {",
        "  const ok = ownProp(roundTrip, 'ok');",
        "  const failures = ownProp(roundTrip, 'failures');",
        "  if (ok === true) {",
        "    const cleanFailures = Array.isArray(failures) &&",
        "        failures.every((f) => typeof f === 'string')",
        "      ? failures",
        "      : [];",
        "    return { ok: true, failures: cleanFailures };",
        "  }",
        "  if (ok === false && Array.isArray(failures) &&",
        "      failures.every((f) => typeof f === 'string')) {",
        "    return { ok: false, failures: failures };",
        "  }",
        "  return { ok: false, failures: [missingReason] };",
        "}",
        "",
        "function postSaveResult(source, roundTrip) {",
        "  const verdict = typeof source === 'string'",
        "    ? roundTrip",
        "    : { ok: false, failures: ['saveChecked() returned a non-string source'] };",
        "  window.webkit.messageHandlers.deckEditor.postMessage({",
        "    action: 'save',",
        "    source: typeof source === 'string' ? source : '',",
        "    roundTrip: verdict,",
        "  });",
        "}",
        "",
        "document.getElementById('save-btn').addEventListener('click', function() {",
        "  if (!(window.webkit && window.webkit.messageHandlers &&",
        "      window.webkit.messageHandlers.deckEditor)) {",
        "    document.getElementById('save-unavailable').style.display = 'block';",
        "    return;",
        "  }",
        "  if (typeof editor.saveChecked === 'function') {",
        "    try {",
        "      const result = editor.saveChecked();",
        "      postSaveResult(ownProp(result, 'source'), normaliseRoundTrip(",
        "        ownProp(result, 'roundTrip'), 'saveChecked() returned no round-trip verdict'));",
        "    } catch (err) {",
        "      postSaveResult('', { ok: false, failures: [String(err && err.message || err)] });",
        "    }",
        "    return;",
        "  }",
        "  try {",
        "    const source = editor.save();",
        "    postSaveResult(source, { ok: false, failures: ['round-trip check did not run'] });",
        "  } catch (err) {",
        "    postSaveResult('', { ok: false, failures: [String(err && err.message || err)] });",
        "  }",
        "});",
    ])


def render_editor_html(path: Path, theme: str | None = None) -> str:
    """A self-contained HTML page for the Deck WYSIWYG editor, from a file.

    Raises OSError if the path is missing or unreadable; see
    `render_editor_page` for everything else this can raise.
    """
    return render_editor_page(
        path.read_text(encoding="utf-8", errors="replace"),
        name=path.name,
        theme=theme,
    )


def render_editor_page(content: str, name: str, theme: str | None = None) -> str:
    """A self-contained HTML page for the Deck WYSIWYG editor, from content.

    Reads md-serial.js, render.js, and editor.js from origin/main in the
    knowledge repo, flattens them into one inline ES module, inlines the
    document source and wikilink map as JSON literals, and emits a bootstrap
    that calls mountEditor and wires a Save control.

    The source and wikilink map are JSON-encoded with every < replaced by
    \\u003c so no document content can break out of the script block.

    TAKES CONTENT RATHER THAN A PATH BECAUSE A KB DOCUMENT HAS NEITHER. It
    lives in the knowledge repository and reaches Deck through
    `agentctl kb checkout`, which returns the content and the sha to submit
    against and deliberately writes nothing to disk. Deck holds those bytes;
    building the page from the same bytes is what guarantees the page edits
    the version the sha guards. A `--render-editor-kb <doc-id>` verb that
    re-read the document here would be a second read, and a second read can
    disagree with the first.

    `name` is the document's display name, used for the title and heading.

    Does NOT invoke Node — all rendering is deferred to the browser.  The only
    subprocesses are git reads via _read_js_module.

    Raises BundleError if the flattened modules fail their safety checks.
    Raises RuntimeError if the knowledge repo or a JS module cannot be read.
    """
    wikilinks = _wikilink_map(content)

    md_serial_content, md_serial_sha = _read_js_module("scripts/md-serial/md-serial.js")
    render_content, render_sha = _read_js_module("scripts/md-serial/render.js")
    editor_content, editor_sha = _read_js_module("scripts/md-serial/editor.js")

    bundle = _flatten_js_modules([md_serial_content, render_content, editor_content])

    source_literal = _escape_for_script(content)
    wikilinks_literal = _escape_for_script(wikilinks)

    stamp = f'<html data-theme="{theme}">' if theme in ("light", "dark") else ""
    sha_comment = (
        f"<!-- md-serial:{md_serial_sha} render:{render_sha} editor:{editor_sha} -->"
    )

    bootstrap = "\n".join([
        f"const SOURCE = {source_literal};",
        f"const WIKILINKS = {wikilinks_literal};",
        "",
        "const resolveWikilink = (name) => {",
        "  const value = ownProp(WIKILINKS, name);",
        "  return value !== undefined ? value : null;",
        "};",
        "",
        "const editorRoot = document.getElementById('editor-root');",
        "const editor = mountEditor(editorRoot, SOURCE, { resolveWikilink });",
        "",
        "if (!(window.webkit && window.webkit.messageHandlers &&",
        "    window.webkit.messageHandlers.deckEditor)) {",
        "  document.getElementById('save-unavailable').style.display = 'block';",
        "}",
        "",
        _save_click_handler_js(),
    ])

    return "\n".join([
        f"<!doctype html>{stamp}<meta charset=utf-8>",
        f"<title>{html.escape(name)}</title>",
        "<meta name=viewport content='width=device-width,initial-scale=1'>",
        f"<style>{FILE_VIEWER_CSS}{EDITOR_CSS}</style>",
        sha_comment,
        f"<h1>{html.escape(name)}</h1>",
        '<div id="editor-toolbar">',
        '  <button id="save-btn">Save</button>',
        (
            '  <span id="save-unavailable" style="display:none">'
            'saving is unavailable — open in Deck to save</span>'
        ),
        '</div>',
        '<div id="editor-root" class="md-doc"></div>',
        '<script type="module">',
        bundle,
        bootstrap,
        '</script>',
    ])


def render_session(session_id: str, theme: str | None = None,
                   background: str | None = None,
                   scale: "float | None" = None,
                   line_height: "float | None" = None) -> str | None:
    """The whole job, for a caller that has a session id. None if not found.

    The WS1.4 seam, at the id level. fleetd may well absorb one layer lower —
    it manages sessions and will already hold the transcript path, making this
    function's glob redundant — in which case it calls
    `render_html(read_rows(path), title)` directly. Both levels are public for
    exactly that reason; neither assumes the standalone server.
    """
    path = transcript_path(session_id)
    if path is None:
        return None
    return render_html(read_rows(path), session_id[:8], theme=theme,
                       background=background, scale=scale,
                       line_height=line_height)
