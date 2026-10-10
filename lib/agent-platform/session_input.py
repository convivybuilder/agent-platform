"""session_input — drive a live Claude Code session from outside its terminal.

WHY THIS EXISTS AS KEYSTROKES. There is no supported programmatic channel into a
running interactive session: the Agent SDK resumes from a transcript by spawning
a new subprocess, the IDE extensions dispatch background sessions, and
`--remote-control` is Anthropic's own web surface rather than an open protocol.
The terminal is the interface Claude Code is built to consume, so that is the
one we drive. A PTY cannot stop accepting keystrokes; what can drift is a key
binding, which is why every behaviour below was measured rather than assumed.

MEASURED 2026-07-23 AGAINST CLAUDE CODE 2.1.217, in a throwaway session. The
design that reasoning produced was wrong three times, so the corrections are
kept next to the code that depends on them. See
`deck/specs/session-pane-input-channel` in the KB for the full record.

  * A message goes in as a BRACKETED PASTE, not as typing. Typing `/rel`
    character by character opens the slash-command menu, so a message beginning
    with `/` would have its following Enter select a command rather than submit
    the message — executing something the user never asked for. `@` likewise
    fires file-mention autocomplete. Paste triggers neither. (The reason
    originally given for pasting — that multiline typing submits per line — is
    FALSE, and recording that matters: someone who believes it will "simplify"
    this back to send-keys and reintroduce the real bug.)

  * A single Enter submits a pasted block as one turn, but NOT if it arrives too
    soon after the paste. One observed submission did nothing; the identical
    Enter sent later worked.

  * `Escape` interrupts AND restores the interrupted message into the input box,
    with no user turn reaching the transcript. So the box can be non-empty when
    a caller believes it is empty, and pasting on top of that submits a
    concatenation of two messages. Clearing first is not defensive padding.

  * `Ctrl-C` clears the box in one keystroke AND INTERRUPTS THE SESSION. That
    second half is why it is not the clear key: sent to a session whose main
    agent reads `waiting` while a background subagent works behind it, it kills
    that subagent's run. This shipped twice — once cutting a live tool call, then
    again on `waiting` after the first fix narrowed the allowed states instead of
    changing the key. Do not "simplify" the clear back to a single `Ctrl-C`.

  * `Ctrl-U` kills ONE input line and interrupts nothing, which is why it is the
    clear key and why a clear is several presses rather than one. The count and
    the measurements behind it are at `CLEAR_KEYSTROKES`; read that before
    touching this path.

  * A digit both selects and confirms a permission prompt. No trailing Enter,
    which would land in whatever replaced the prompt.

RE-MEASURED 2026-07-24 AGAINST CLAUDE CODE 2.1.219, because the note that used
to sit here was wrong. It claimed a digit is ignored by the `AskUserQuestion`
class and that the fix was to walk the list with arrows — a diagnosis reached
with keystrokes that had gone to the WRONG PANE, since `tmux send-keys -t =name`
run from zsh loses its target to `=` expansion. Driven through subprocess
instead, against a live picker in a real `co` session:

  * A digit answers a SINGLE-SELECT question exactly as it answers a permission
    prompt: `session-agent choose <name> 3` selected the third option and the
    turn continued. That path was never broken.

  * A digit on a MULTISELECT question TOGGLES its checkbox and leaves the picker
    standing. This is the bug Deck presented as "clicking does nothing": one
    click ticked a box nobody could see, and the gate never resolved.

  * `Right` jumps from the question to its review tab, where digit `1` is
    "Submit answers" and `2` is "Cancel". Three single keystrokes, no walking.

  * ARROWS SENT IN QUICK SUCCESSION ARE DROPPED. Twelve `Up`s at 120ms apart
    moved the cursor two rows. This is why submission goes through `Right`
    rather than the arrow walk that was originally prescribed: a drive built on
    counted arrow keys silently lands on the wrong row.

  * `Down` clamps at the LAST row, which is "Chat about this" rather than
    "Submit" — so overshooting deliberately does not park anywhere useful.

  * The picker shows rows the gate payload does not carry ("Type something",
    "Chat about this"), appended after the real options. Option `i` in the
    payload is still digit `i`, which is what keeps the mapping honest.
"""
from __future__ import annotations

import json
import logging
import re
import sys
import time
from pathlib import Path

# ITS OWN DIRECTORY, not `~/.local/lib/agent-platform`. The farm is a per-file
# symlink into a checkout; in production that is this same tree, so siblings stay
# current together — but reaching for the FARM makes this module import whatever
# the PRIMARY checkout is on, which is a different tree whenever this one is a
# worktree. `session_state` has said so at its own insert since #329; this line
# was the one that did not.
#
# It cost a wrong-module bug that read as a code failure. `session_input` sorts
# before `session_state`, so the test harness imported it first, and the
# `import session_state` below then resolved from the farm and cached MAIN's copy
# under that name for the whole run — while `signal_client` resolved from the
# tree. The suite tested two different trees at once and the symptom was a
# credential-redaction assertion failing on a branch whose sanitiser was correct.
# Exactly the mechanism conftest documents for `telemetry`/`cost_join`, second
# instance.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmux_transcript as TT  # noqa: E402

# OPTIONAL, FOR THE REASON #309 MADE IT OPTIONAL IN `session_state`. A hard
# module-scope import of this same module took the state channel down on
# 2026-07-29: `~/.local/lib` is a per-file symlink farm, so a new module is
# unlinked until `install.sh` runs, and every entry point that imported it stopped
# working in the meantime. `send`, `choose` and `interrupt` do not need a picker
# reader; only `submit_choices` consults it, and its answer for "no reader" is the
# behaviour it had before this module existed.
try:
    import picker_render  # noqa: E402
except Exception:  # noqa: BLE001 — any failure here means "no reader"
    picker_render = None

# Top-level optional import so an ImportError surfaces at module load, not
# silently per-keystroke. Absent means "no state reader"; the degradation
# behaviour is the same as for `picker_render` above.
try:
    import session_state as _SS  # noqa: E402
except Exception:  # noqa: BLE001 — any failure here means "no state reader"
    _SS = None

JoinError = TT.JoinError


class InputError(Exception):
    """Input could not be delivered, with a reason the caller can act on."""


def _check_expectation(session_name: str,
                       expect_gate: str | None,
                       expect_question: int | None) -> None:
    """Guard a picker keystroke against a stale caller view.

    WHY THIS RUNS ON THE MINI, NOT IN THE CALLER. Deck's read of the session
    state is up to one poll interval (~2s) stale by construction. A check in
    the caller would only compare two stale reads. The check here runs
    immediately before the keystroke, so the window is the capture-to-keystroke
    gap — milliseconds, not seconds.

    WHAT THIS DOES NOT DO. It narrows the window from ~2s to that gap; it does
    not close it. A gate that advances in the microseconds between the read and
    the send would still be missed. The spec was honest about this when it built
    in the "observed-absent means send anyway" rule, and replacing that rule with
    "refuse unless proven safe" would cost the verb on every unrecognised Claude
    Code version. The residual gap is acknowledged, not papered over.

    BOTH NONE means "no expectation" — the check returns immediately and the
    verb behaves exactly as before this change. Every existing caller passes
    both as None.

    OBSERVED ABSENT means send anyway — an unrecognised Claude Code version, a
    failed capture, or a gate with no folded id all leave the field off the gate
    or render, and refusing there would cost the verb on a version bump. This is
    the same trade `answer_choice` makes on `digitBecomesText` and
    `submit_choices` makes on `multiSelectCommit`.

    ONE OBSERVATION. `questionIndex` is read from the `rendered` sub-dict that
    `session_state()` already attaches to a question-kind gate; there is no
    second `picker_render.read()` call here. Two reads milliseconds apart could
    describe screen states that never coexisted, which is the race class this
    feature exists to close.
    """
    if expect_gate is None and expect_question is None:
        return

    observed_gate_id: str | None = None
    observed_question: int | None = None

    try:
        if _SS is not None:
            # NOT `brief=True`. `session_state` attaches `rendered` only when
            # `not brief`, so briefing this call would silently drop
            # `questionIndex` and disable half the binding while the `gateId`
            # half kept working — and every test here patches `session_state`,
            # so none of them would catch it. The cost of the full read is
            # accepted for that reason.
            state = _SS.session_state(session_name)
            gate = (state.get("gate") or {})
            gid = gate.get("gateId")
            if isinstance(gid, str):
                observed_gate_id = gid
            qi = gate.get("rendered", {}).get("questionIndex")
            if isinstance(qi, int):
                observed_question = qi
    except (OSError, ValueError):
        pass

    if (expect_gate is not None
            and observed_gate_id is not None
            and expect_gate != observed_gate_id):
        raise InputError(
            f"keystroke not sent: expected gate {expect_gate!r} but the live "
            f"gate is {observed_gate_id!r}; the session moved on to a different "
            "question before this keystroke could land")

    if (expect_question is not None
            and observed_question is not None
            and expect_question != observed_question):
        raise InputError(
            f"keystroke not sent: expected question {expect_question} but the "
            f"live question index is {observed_question}; the picker advanced "
            "before this keystroke could land")


# Keys a caller may send by name. An allowlist rather than a passthrough,
# because this string reaches `tmux send-keys` as a key specification, and
# because two of these have consequences worth naming at the call site.
KEYS = {
    "enter": "Enter",
    "escape": "Escape",       # interrupts, and repopulates the input box
    "tab": "Tab",
    "backtab": "BTab",        # cycles permission mode
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "clear": "C-u",           # kills one input line; does NOT interrupt
    # NO C-c ENTRY, DELIBERATELY. It was `clear` until it was found killing
    # background agents, and a draft of that fix re-added it here as
    # `interrupt_key` — no caller, but `bin/session-agent` builds its `key`
    # choices from this dict verbatim, and `send_key` lists every known name in
    # its error text. So the capability would have survived under a new name,
    # discoverable by any agent that made a typo, and two consecutive calls of
    # it exit Claude Code. Interrupting has its own verb (`interrupt`, which
    # sends Escape); nothing needs C-c by name. Raised by security-reviewer.
    **{str(d): str(d) for d in range(1, 10)},
}

# How many `C-u` presses one clear sends.
#
# WHY C-u AND NOT C-c, WHICH IS WHAT THIS USED TO BE. `C-c` empties the box in
# one keystroke, and it also INTERRUPTS — which killed background subagents
# whenever a message was sent to a session whose main agent read `waiting` while
# a subagent was still working. The operator lost two selector build runs to this on
# 2026-07-24; an earlier round had already stopped it firing at `working`
# sessions, but `waiting` is exactly the state a session sits in while its
# background work continues.
#
# MEASURED against Claude Code 2.1.220, in a throwaway session:
#   * `C-u` on a one-line draft empties the box.
#   * `C-u` sent mid-turn, with a shell tool call in flight, did NOT interrupt —
#     the turn ran to completion and returned its result.
#   * `C-u` kills ONE line at a time, so a three-line pasted draft needed
#     several; extra presses against an empty box are inert.
#   * the footer then offers `Ctrl+Y to paste deleted text`, so even a clear the
#     user did not want is recoverable rather than destructive.
#
# 12 is a bound, not a measurement: enough for any draft a person restores with
# `Escape`, and harmless past the end. A draft longer than this keeps its head,
# which concatenates — the trade this module already names as the acceptable
# one, because a concatenated message is recoverable and a killed tool call is
# not.
CLEAR_KEYSTROKES = 12

# How long to let a bracketed paste settle before submitting. Measured: an Enter
# sent immediately after the paste did nothing, while the same Enter later
# submitted. Generous because the cost of waiting is a moment and the cost of
# being early is a message that silently vanishes.
PASTE_SETTLE_SECONDS = 0.4

# How long to let the review tab draw before the digit that submits it. The same
# trade as the paste settle, and the same measurement behind it: keystrokes sent
# into a screen that has not finished rendering are dropped, and a dropped digit
# here leaves the session sitting on a review nobody is watching.
REVIEW_SETTLE_SECONDS = 0.6

# How long one arrow needs to land before the next is sent. Arrows in succession
# are dropped — the observation behind `submit_choices` avoiding a walk at all,
# where twelve of them moved the cursor two rows.
#
# MEASURED against Claude Code 2.1.220 on 2026-07-31, walking a multiselect
# question's highlight with a `capture-pane` read between each pair: 22
# consecutive arrows at this spacing, every one of which moved the highlight
# exactly one row. This is the spacing that makes `focus_free_text`'s walk
# possible; without it the walk is the unreliable thing that reasoning rejected.
ARROW_SETTLE_SECONDS = 0.35

# How many rows sit below the last option on a multiselect: the free-text row,
# the bare commit button, and `Chat about this`. Observed 2.1.220, and it is the
# bound on `focus_free_text`'s upward step-off rather than a measurement of any
# one walk. One of slack past the three, and then the walk gives up rather than
# pressing arrows at a picker it no longer understands.
_FREE_TEXT_GROUP_ROWS = 4

# The digit that takes "Submit answers" on the review tab. `2` is "Cancel"; they
# are named here rather than written inline so the pairing stays visible.
_SUBMIT_ANSWERS = "1"

_BUFFER = "deck-session-input"

# Control characters a message may legitimately contain. Everything else in C0
# and C1 is refused.
_ALLOWED_CONTROLS = {"\n", "\t"}


def _reject_control_characters(text: str) -> None:
    """Refuse a message carrying control bytes, ESC above all.

    THE ESCAPE THIS CLOSES. `paste-buffer -p` wraps the buffer in bracketed-paste
    markers, but tmux does not sanitise what is inside them. A message containing
    the paste-END marker `ESC[201~` therefore terminates its own paste early, and
    every byte after it reaches Claude Code as KEYSTROKES rather than as pasted
    text — which is precisely the typed-input path this module exists to avoid.
    A `/command` after the marker runs a slash command; a digit answers a
    permission prompt.

    Today the only caller is Deck, carrying text a human typed. But this module
    is built for automation to pipe into — that is what `--text -` is for — so
    the first script that forwards a webhook body or model output through it
    would hand an attacker the session. Guarding at the boundary is cheaper than
    auditing every future caller.

    Refused rather than stripped. Silently truncating a message at an injection
    attempt delivers a DIFFERENT message than the caller asked for and reports
    success, which is a worse failure than not sending it.
    """
    for ch in text:
        if ch in _ALLOWED_CONTROLS:
            continue
        code = ord(ch)
        if code < 0x20 or code == 0x7F or 0x80 <= code <= 0x9F:
            raise InputError(
                f"refusing a message containing control character {code:#04x}; "
                "it could end the bracketed paste early and become keystrokes")


def _tmux(*args: str) -> None:
    if TT.run(["tmux", *args]) is None:
        raise InputError(f"tmux rejected: {' '.join(args)}")


def _pane(session_name: str) -> str:
    pane_id, _ = TT.claude_pane(session_name)
    return pane_id


def send_key(session_name: str, key: str, pane_id: str | None = None) -> str:
    """Send one named key. Returns the pane it went to.

    Refuses an unknown name rather than passing it through: the value becomes a
    tmux key specification, and a caller inventing `"C-c C-c"` would exit the
    session rather than get an error.
    """
    spec = KEYS.get(key.lower())
    if spec is None:
        raise InputError(f"unknown key {key!r}; known: {', '.join(sorted(KEYS))}")
    pane = pane_id or _pane(session_name)
    _tmux("send-keys", "-t", pane, spec)
    return pane


def _clear_input(pane_id: str) -> None:
    """Empty a pane's input box without interrupting what it is doing.

    ONE `send-keys` CALL carrying every press, rather than one call each: the
    presses then arrive as a run with no gap for a keystroke of the user's own
    to land between them, and it is a single subprocess instead of twelve.
    """
    _tmux("send-keys", "-t", pane_id, *([KEYS["clear"]] * CLEAR_KEYSTROKES))


# The states in which the input box is cleared before a message is pasted.
#
# NO LONGER A SAFETY GATE, and the distinction is worth keeping straight: it was
# one when the clear was Ctrl-C, and it failed at that job because `waiting`
# named a session whose subagent was still working. `_clear_input` cannot
# interrupt anything now, so this set says only WHERE a clear is wanted — after
# an `Escape` restored a draft into the box, which leaves the session idle.
_CLEARABLE_STATES = frozenset({"idle", "waiting"})


def _current_state(session_name: str) -> str | None:
    """The session's state string, via `session_state`.

    Imported inside the call rather than at module scope so `session_input`
    keeps loading for callers that only send keys, and so the two modules stay
    independent — `session_state` is the one with the run-dir dependencies.
    """
    import session_state as SS
    return SS.session_state(session_name).get("state")


def _read_state(session_name: str) -> str | None:
    """The session's state, or None when it cannot be read.

    Every caller here has to decide what to do about not knowing, and they do
    not all decide the same way — so the failure comes back as a value rather
    than as an exception each of them would have to catch identically.
    """
    try:
        return _current_state(session_name)
    except Exception:
        return None


# --- verifying a paste landed and a message submitted -----------------------
#
# `send_message` used to end on a guess: sleep `PASTE_SETTLE_SECONDS`, send
# Enter, return — trusting that the paste had landed and that the Enter would
# be read. Both halves of that guess failed silently on 2026-07-22: an Enter
# sent before the paste settled submitted nothing, and `Escape` can leave text
# in the box that a caller believes is empty. `capture-pane` answers both
# questions directly, so it replaces the guess rather than padding it.
#
# THIS STAYS A VERIFICATION TOOL, NOT A STATE SOURCE. `session_state` opens by
# rejecting the screen as a source of session STATE, for the ghost-suggestion
# reason above `picker_render`'s own module docstring. What follows never asks
# "what state is the session in" — only "does the input box's own text match
# what I just sent, or is it empty" — which is the narrower question
# `deck/specs/reading-the-rendered-picker` §4 already found safe to ask, and
# for the same reason: it never reads a field state could answer.
#
# RE-VERIFIED 2026-08-09 AGAINST CLAUDE CODE 2.1.226, in a throwaway tmux
# session, the same discipline the rest of this module was built under. Three
# things settled the design:
#
#   * Ghost suggestions DID NOT REPRODUCE, exactly as `picker_render`'s own
#     2026-07-30 experiment against 2.1.220 found. Three near-identical
#     messages, then typing a prefix matching one of them, produced no dim
#     completion anywhere. So `_GHOST_SGR_PARAM` below is UNTESTED against a
#     live ghost — it is coded to the spec's SGR-2 claim, which is real
#     Anthropic styling (the welcome banner and the status line both use
#     `ESC[2m`), but no capture in this session ever carried it inside the
#     input box. `picker_render._DIM` (`38;5;241`, an unrelated 256-colour
#     grey) is what a mid-turn DISABLED input box is drawn in — its cursor and
#     the empty line both read `ESC[38;5;241m`, no `2` anywhere — and treating
#     that as a ghost would misread every message sent while a session is
#     busy. The two are kept apart by construction: `_visible` strips only
#     literal SGR parameter `2`.
#
#   * A real paste — one line, several lines, or a single line long enough to
#     soft-wrap — renders as `❯` + U+00A0 (a non-breaking space, not a
#     regular one) then the first line's text, with every further screen row
#     — an explicit newline's continuation or a wrapped one, indistinguishably
#     — indented exactly two plain spaces. An embedded blank line renders as a
#     genuinely empty row: no cursor, no indent, nothing to strip. Because
#     wrapping and an explicit `\n` render identically, this module compares
#     the box to the sent text with internal whitespace collapsed
#     (`_normalize_box_text`) rather than character-for-character — the
#     alternative would treat a message that happened to wrap as never having
#     landed.
#
#   * The box itself is delimited by a bare horizontal rule immediately above
#     and below it — a captured line that, stripped of styling, is nothing but
#     `─` — with the status footer directly beneath the lower one. Exactly two
#     such lines appeared in every capture taken this session; the welcome
#     banner draws similar rules, but always as part of a longer styled line,
#     never alone on one. `_parse_input_box` takes the BOTTOM-MOST pair for
#     the same reason `picker_render`'s scans take the bottom-most match:
#     the box is the bottom-most thing tmux draws, so anything above it that
#     looks like a rule is transcript the model wrote.
#
# DEGRADATION MATCHES THE REST OF THE MODULE. A version below the floor, a
# failed capture, or a screen with no rule-bracketed region are the same answer —
# `_read_input_box` returns `None` — and every caller below reads that as
# OBSERVED-ABSENT: fall back to the blind behaviour this replaces rather than
# block the verb on a Claude Code release.
#
# A FLOOR RATHER THAN EXACT EQUALITY, for the reason `picker_render`'s module
# docstring gives at length: this constant read 2.1.226, verified by hand on
# 2026-08-10, and the mini was on 2.1.227 the next day, so the reader was dark
# within a day of shipping. The structural check that replaces the version
# equality is already here and always was — `_parse_input_box` returns None when
# it cannot find a rule-bracketed region, which is exactly the "have the markers
# moved" question asked of the screen. The floor bounds it below, since nothing
# here has been driven against an older build.
#
# Driven by hand against 2.1.227 on 2026-08-10: a box holding typed text parsed
# back verbatim, and an emptied box parsed to `""` rather than to a ghost.
INPUT_BOX_VERIFIED_FLOOR = "2.1.220"

# How often a poll re-reads the pane, and how many reads a `settle` budget
# buys. Attempts are derived from `settle` rather than hard-coded so the two
# stay in the same relationship a caller already controls by passing `settle`
# to `send_message` — nothing here adds a second, uncoordinated knob.
INPUT_POLL_INTERVAL_SECONDS = 0.05


def _poll_attempts(settle: float) -> int:
    return max(1, round(settle / INPUT_POLL_INTERVAL_SECONDS))


# SGR parameter 2 — the standard "dim" attribute the spec records for a ghost
# suggestion. NOT `picker_render._DIM` (`38;5;241`), a 256-colour grey the
# picker draws row numbers in and the TUI also draws a mid-turn DISABLED input
# box in — conflating the two would read every message sent while a session
# is busy as a ghost. See the section note above for what could and could not
# be confirmed live.
_GHOST_SGR_PARAM = "2"

_INPUT_SGR = re.compile(r"\x1b\[[0-9;]*m")

# A captured line that is nothing but the box-drawing rule, once styling is
# stripped. The welcome banner uses the same glyph but never alone on a line;
# the input box's own top and bottom rules do, every time observed.
_BOX_BORDER = re.compile(r"^─+$")

# The colours the TUI draws those two rules in, and checking one is a GUARD
# rather than decoration. The shape alone is forgeable: pane content is partly
# written by the session itself, so two plain lines of `─` around chosen text
# were read back verbatim as the live box's contents — no escape bytes
# anywhere, verified in the security review of #387. Requiring the colour,
# through a parsed SGR check rather than a substring test, is what makes the
# border a thing the TUI drew.
#
# A SET, NOT ONE CODE. The shape alone (`_BOX_BORDER`'s bare rule, at the
# bottom-most pair `_parse_input_box` takes) is identical across every build
# this module has seen and cannot by itself tell a recoloured border from a
# forged one, so colour has to stay load-bearing — but pinning it to one exact
# code is what let CQ-422 happen: `_read_input_box` went dark on every capture
# from the day 2.1.292 installed until CQ-420's coder reproduced it by hand
# (run-21177f7d, agent-platform#808), because the floor-version check passed
# and the border-colour check then failed silently. Matching a set found on
# builds this module has actually been driven against keeps the guard without
# repeating that outage on the next recolour.
#
#   * `38;5;246` — observed on 2.1.226 (2026-08-09) and 2.1.227 (2026-08-10),
#     both captures holding a message and emptied.
#   * `38;5;245` — the installed build's colour by 2.1.292, found reproducing
#     CQ-422: every capture from that session carried this code where
#     `38;5;246` had been, with the rule's shape and position unchanged.
#
# A build that recolours the rule to something outside this set still
# refuses — the same degradation every other marker here already has. This
# widens which builds match; it does not drop the guard.
_BOX_BORDER_FG_CODES = frozenset({"38;5;246", "38;5;245"})

# Every SGR sequence on a line. Local rather than borrowed from `picker_render`
# for the reason `_version_tuple` below is local: that import is optional here.
_SGR_SEQ = re.compile(r"\x1b\[([0-9;]*)m")


def _drawn_with(line: str, params: str) -> bool:
    """Whether a real SGR sequence on *line* sets *params*.

    The parameters must be consecutive tokens inside one genuine `ESC[`…`m`, so a
    line that merely mentions the digits does not qualify. Mirrors
    `picker_render._draws_with`, and see that docstring for what forging one
    bought before the check was parsed.
    """
    for sequence in _SGR_SEQ.finditer(line):
        tokens = sequence.group(1).split(";")
        index = 0
        while index < len(tokens):
            token = tokens[index]
            # The extended-colour compounds, consumed as units for the reason
            # `_visible` above already gives: `38;5;N` is one selector, and
            # walking its tokens singly reads its digits as attributes.
            if token in ("38", "48", "58"):
                if index + 1 < len(tokens):
                    if tokens[index + 1] == "5" and index + 2 < len(tokens):
                        if ";".join(tokens[index:index + 3]) == params:
                            return True
                        index += 3
                        continue
                    if tokens[index + 1] == "2" and index + 4 < len(tokens):
                        if ";".join(tokens[index:index + 5]) == params:
                            return True
                        index += 5
                        continue
                # A malformed compound names nothing honestly, and neither does
                # anything after it — see `picker_render._sgr_operations`.
                break
            if token == params:
                return True
            index += 1
    return False


def _drawn_with_any(line: str, candidates) -> bool:
    """Whether *line* carries a real SGR sequence matching any of *candidates*.

    The border match needs a set rather than one code because the TUI's own
    choice of grey has already moved under this module once without the
    rule's shape or position changing at all — see `_BOX_BORDER_FG_CODES`.
    """
    return any(_drawn_with(line, params) for params in candidates)

# The box's own cursor. Real content on the first row reads `❯` + U+00A0; an
# empty box reads the same with nothing after it.
_CURSOR = "❯"  # ❯
_NBSP = "\xa0"


def _version_tuple(version: str) -> tuple[int, ...] | None:
    """A dotted version as integers, or None when it is not one.

    DUPLICATED FROM `picker_render.version_tuple` RATHER THAN IMPORTED, and that
    is the point of it. `picker_render` is an OPTIONAL import here for the reason
    #309 made it optional in `session_state` — `~/.local/lib` is a per-file
    symlink farm, so a module can be unlinked between a merge and an install —
    and borrowing this helper would take the input-box reader down with it,
    re-creating the outage that motivated the guard. Eight lines is the cheaper
    half of that trade.
    """
    parts = version.split(".")
    if not parts or any(not p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def _input_box_plain(line: str) -> str:
    return _INPUT_SGR.sub("", line)


def _visible(line: str) -> str:
    """*line*'s text, dropping any run drawn with SGR parameter 2.

    Walks SGR runs left to right rather than substring-matching, the same
    discipline `picker_render._closes_background` uses and for the same
    reason: a combined escape like `ESC[0;2m` or `ESC[22m` has to be parsed as
    parameters, not grepped for a substring that could appear inside an
    unrelated code (`ESC[38;5;2xxm` does not set SGR 2). Reset (`0` or a bare
    `ESC[m`) and the dim-specific reset (`22`) both clear it; nothing here
    tracks any other attribute, because nothing else here needs one.

    `38;5;N`/`48;5;N` — a 256-colour foreground/background selector — is a
    THREE-TOKEN COMPOUND, consumed as one unit rather than checked
    token-by-token: a palette index that happens to be literally `2`
    (`38;5;2`) is not SGR 2. Checking tokens independently, as
    `picker_render._closes_background` still does for its own narrower
    check, misreads that index as the standalone dim attribute and drops the
    run.
    """
    dim = False
    out: list[str] = []
    for piece in re.split(r"(\x1b\[[0-9;]*m)", line):
        if piece.startswith("\x1b["):
            params = piece[2:-1].split(";")
            i = 0
            while i < len(params):
                p = params[i]
                if p in ("38", "48") and params[i + 1:i + 2] == ["5"]:
                    i += 3  # the selector and its palette index, together
                    continue
                if p in ("", "0"):
                    dim = False
                elif p == _GHOST_SGR_PARAM:
                    dim = True
                elif p == "22":
                    dim = False
                i += 1
            continue
        if piece and not dim:
            out.append(piece)
    return "".join(out)


def _parse_input_box(capture: str) -> str | None:
    """The live input box's own non-ghost text, or `None` if none was found.

    Pure, so the parsing can be tested against a recorded capture rather than
    a live session — the same reason `picker_render.parse` is split from
    `picker_render.read`.

    Absent means "no box-shaped region was found", never "the box is empty" —
    an empty box parses to `""`, a real value every caller below can compare
    against.
    """
    lines = capture.splitlines()
    borders = [i for i, ln in enumerate(lines)
               if _BOX_BORDER.match(_input_box_plain(ln).rstrip())
               and _drawn_with_any(ln, _BOX_BORDER_FG_CODES)]
    if len(borders) < 2:
        return None
    top, bottom = borders[-2], borders[-1]
    content = lines[top + 1:bottom]
    if not content:
        return None
    out = []
    for i, raw in enumerate(content):
        text = _visible(raw)
        if i == 0:
            if text.startswith(_CURSOR):
                text = text[1:]
            if text.startswith(_NBSP):
                text = text[1:]
        elif text.startswith("  "):
            text = text[2:]
        out.append(text)
    return "\n".join(out)


# --- making an unreadable box visible ---------------------------------------
#
# Degrading to `None` is silent by design everywhere a caller of this function
# sits — OBSERVED-ABSENT means fall back to the blind behaviour that existed
# before a reader did, which is what keeps a Claude Code version bump from
# costing the verb. That silence is also exactly how CQ-422 went unnoticed:
# `_BOX_BORDER_FG` (now `_BOX_BORDER_FG_CODES`) was measured against 2.1.227,
# the installed build moved to 2.1.292, and every caller kept quietly taking
# its fallback until CQ-420's coder reproduced the dead reader by hand. This
# warns once the day it starts happening, so the next recolour is a line
# someone can grep for rather than a lost Slate message.
_LOG = logging.getLogger(__name__)
_box_unavailable_logged = False


def _log_box_unavailable_once(reason: str) -> None:
    """Warn that the input box could not be read, because *reason*. Fires
    once per process.

    *reason* is always a static clause naming which of `_read_input_box`'s
    three failure paths called this — never captured pane text, which would
    put untrusted session output into the log line.

    ONCE PER PROCESS, NOT PER POLL. `_await_paste_landed` and
    `_poll_box_content` each call `_read_input_box` several times inside one
    `send_message` — logging every one of those reads would turn a single
    unreadable build into dozens of identical lines per message sent, with
    the dozenth telling a reader nothing the first did not. A fresh process
    warns again: `bin/session-agent` and `bin/limit-resume` are both one-shot
    invocations, the latter run every 180 seconds by
    `launchd/com.convivy.limit-resume.plist`, whose plist already captures its
    own stderr to a file — so the signal still surfaces the day a build
    ships, not only on this module's first import.
    """
    global _box_unavailable_logged
    if _box_unavailable_logged:
        return
    _box_unavailable_logged = True
    _LOG.warning(
        "session_input: the Claude Code input box could not be read; every "
        f"verb that checks it is taking its unavailable-box fallback: {reason}.")


def _read_input_box(pane_id: str) -> str | None:
    """The live input box's non-ghost text, or `None` when unavailable.

    Mirrors `picker_render.read`'s shape and its degradation: a version this
    was not checked against, a failed capture, and a screen with no
    box-shaped region all come back `None`, and every caller treats that one
    value as "cannot verify" rather than as three different failures. Every
    one of those three also logs via `_log_box_unavailable_once` — see the
    section note above this function for why that is once per process rather
    than once per read.
    """
    version = TT.run(
        ["tmux", "display-message", "-p", "-t", pane_id, "#{pane_current_command}"])
    version = (version or "").strip()
    parsed = _version_tuple(version)
    if parsed is None or parsed < _version_tuple(INPUT_BOX_VERIFIED_FLOOR):
        _log_box_unavailable_once(
            f"this pane predates the {INPUT_BOX_VERIFIED_FLOOR} floor")
        return None
    capture = TT.run(["tmux", "capture-pane", "-e", "-p", "-t", pane_id])
    if not capture:
        _log_box_unavailable_once("the pane capture itself failed")
        return None
    result = _parse_input_box(capture)
    if result is None:
        _log_box_unavailable_once(
            "its composer border has moved to a colour outside _BOX_BORDER_FG_CODES")
    return result


def _normalize_box_text(text: str) -> str:
    """Collapse whitespace so a soft-wrapped box reads the same as the
    unwrapped text that produced it — see the section note above."""
    return " ".join(text.split())


def _await_paste_landed(pane: str, text: str, settle: float) -> None:
    """Block until the box shows *text* landed, or fall back to a fixed sleep.

    THE FIRST READ DECIDES WHETHER THIS CAN VERIFY AT ALL. A version this has
    not been checked against does not start matching mid-poll, so a `None` on
    attempt one goes straight to the same fixed sleep `send_message` always
    took — spending the poll interval on those reads first would silently
    double the wait on exactly the sessions this cannot help. A `None` on any
    LATER attempt is read as "not yet", not "give up" — a transient capture
    miss must not cost the verb.
    """
    target = _normalize_box_text(text)
    content = _read_input_box(pane)
    if content is None:
        # OBSERVED-ABSENT: an unrecognised Claude Code version or a failed
        # capture is not going to change mid-poll, so there is nothing to
        # gain from spending the budget on more reads of it.
        time.sleep(settle)
        return
    if _normalize_box_text(content) == target:
        return
    for _ in range(_poll_attempts(settle) - 1):
        time.sleep(INPUT_POLL_INTERVAL_SECONDS)
        content = _read_input_box(pane)
        if content is not None and _normalize_box_text(content) == target:
            return
    # Bound exhausted without a match. Proceed to Enter regardless — the
    # worst case this replaces never verified anything either.


_UNAVAILABLE = object()  # sentinel: the box could not be read even once


def _poll_box_content(pane: str, settle: float):
    """The box's content after up to `_poll_attempts(settle)` reads, stopping
    the moment it reads empty. `_UNAVAILABLE` the instant ANY read cannot
    establish a box — not only the first.

    A LATER `None` IS NOT "NO CHANGE". `_read_input_box` returns `None` for
    an unrecognised version, a failed capture, OR `_parse_input_box` finding
    no box-shaped region at all — and a picker replacing the composer mid-poll
    produces exactly that third case, since `picker_render` never draws the
    bare rule pair `_parse_input_box` anchors on. Carrying the last
    successfully parsed content forward past a `None` would hand
    `_await_submitted` stale, pre-picker text as if it were still current —
    which is the whole gate this function exists to protect."""
    content = _read_input_box(pane)
    if content is None:
        return _UNAVAILABLE
    last = content
    if last == "":
        return last
    for _ in range(_poll_attempts(settle) - 1):
        time.sleep(INPUT_POLL_INTERVAL_SECONDS)
        content = _read_input_box(pane)
        if content is None:
            return _UNAVAILABLE
        last = content
        if last == "":
            return last
    return last


def _await_submitted(session_name: str, pane: str, text: str, settle: float) -> None:
    """Block until the box empties, retrying Enter once on the observed
    half-submit, then raise if the message still has not gone.

    THE RETRY IS GATED ON THE TEXT STILL BEING THERE, NEVER ON A BARE
    TIMEOUT. That is what proves nothing else — a picker, another sender —
    took the box in the meantime; retrying into an unknown state could answer
    a question nobody asked, which is the exact hazard `answer_choice` and
    `submit_choices` already refuse to risk elsewhere in this module.

    UNAVAILABLE MEANS PROCEED, both before the first Enter and after the
    retry: `send_message` sent no verification at all before this existed, so
    "cannot verify" costs nothing extra here, unlike `_await_paste_landed`,
    which preserves the fixed sleep that already existed.

    THE RETRY RE-CHECKS STATE IMMEDIATELY BEFORE ITS OWN KEYSTROKE, mirroring
    `_check_expectation` and all four of its callers (`focus_free_text`,
    `type_text`, `answer_choice`, `submit_choices`) — every other sender in
    this module re-reads right before a keystroke that can land on a live
    gate. Confirmed against a real
    Claude Code 2.1.226 gate on 2026-08-09: a bare `Enter` DOES accept the
    default-highlighted option there, the same as a digit does on a
    permission prompt — see `deck/specs/session-pane-input-channel` — so this
    re-check is closing a real hazard, not a theoretical one.

    WHAT REMAINS OPEN, NAMED RATHER THAN SUMMARISED AS "narrowed". One gap
    survives: the capture-to-keystroke race, microseconds between the state
    read and the keystroke landing, which is the same residue
    `_check_expectation` documents and cannot be closed from here. The
    `session_state`-unavailable window is NOT a second such gap, because the
    check below fails closed on an unreadable state; that is deliberate and
    its reasoning is at the call site.
    """
    # `_UNAVAILABLE` IS TREATED IDENTICALLY TO "" HERE, DELIBERATELY — the same
    # OBSERVED-ABSENT trade the rest of this module makes. It means a mid-
    # session version bump or a transient capture failure makes this report
    # success having verified nothing; that is accepted so the verb is not
    # lost on every unrecognised Claude Code release, not a claim that
    # `_UNAVAILABLE` is as strong a signal as a confirmed empty box.
    content = _poll_box_content(pane, settle)
    if content is _UNAVAILABLE or content == "":
        return
    if _normalize_box_text(content) != _normalize_box_text(text):
        raise InputError(
            "could not confirm the message submitted; the input box holds "
            "something other than what was sent, so retrying Enter could "
            "answer whatever else is there")
    # THE OBSERVED HALF-SUBMIT: the text is still there, unchanged, which is
    # the one condition safe to retry into.
    #
    # THIS CHECK FAILS CLOSED ON AN UNREADABLE STATE, and the shape of that is
    # particular. `_read_state` returns None for every failure — a
    # `session_state` import that is not there, a raising call, an unparseable
    # answer — and `None == "blocked"` is False, so reading None as "not
    # blocked" would send the keystroke exactly as if the state had been
    # confirmed safe. That is not hypothetical: the 2026-07-29 symlink-farm
    # incident left a module unlinked and unreadable for as long as it went
    # unnoticed, which is a far wider window than the capture-to-keystroke race.
    #
    # IT IS NOT THE ONLY CHECK HERE THAT REFUSES ON None — `focus_free_text`
    # and `type_text` both demand `state == "blocked"` to proceed, so None
    # fails their equality too. The distinction is narrower than "this one
    # fails closed": those two require ONE specific state and refuse every
    # other, None included, which costs them nothing extra. This is the only
    # check that refuses on None while letting a sibling discrete state — any
    # readable, non-blocked value — through, so failing closed here is a
    # deliberate choice rather than a side effect of demanding one state.
    #
    # The choice is worth stating because the file's default runs the other
    # way. OBSERVED-ABSENT MEANS SEND ANYWAY for a keystroke the caller ASKED
    # for, where refusing on an unreadable state would cost the verb on every
    # degraded environment. This guards a keystroke NOBODY asked for — an extra
    # Enter this module decided to send — so an unsent retry costs the caller
    # an error it can act on, while a wrongly-sent one approves whatever gate
    # is on screen. Confirmed live: a bare Enter takes the default-highlighted
    # option, and on the gate observed that option was "use auto mode", which
    # approves everything after it rather than one action.
    #
    # Refusing here is also honest rather than merely cautious: reaching this
    # line means the box still holds the message, so it demonstrably did NOT
    # submit. The caller is told the truth and can resend.
    #
    # THIS PARAGRAPH HAS A TWIN, and correcting one alone is how it last went
    # wrong: `test_the_retry_refuses_when_the_state_cannot_be_read_at_all` in
    # `tests/test_session_verbs.py` states the same fact, and a round of
    # review shipped with the two contradicting each other because the fix
    # reached only the copy the editor happened to be looking at. Edit both.
    state = _read_state(session_name)
    if state is None or state == "blocked":
        raise InputError(
            "could not confirm it is safe to retry Enter; the session reads "
            f"{state or 'unreadable'} and a retry could answer a gate instead "
            "of submitting. The message is still in the input box, unsent")
    send_key(session_name, "enter", pane_id=pane)
    # `_UNAVAILABLE` reads as "" here too; see the note on the first check.
    content = _poll_box_content(pane, settle)
    if content is _UNAVAILABLE or content == "":
        return
    raise InputError(
        "the message was not submitted, even after a retried Enter; the "
        "input box still holds it")


def send_message(session_name: str, text: str, settle: float = PASTE_SETTLE_SECONDS,
                 pane_id: str | None = None, *, skip_blocked_check: bool = False) -> str:
    """Clear, paste, settle, submit. Returns the pane it went to.

    Empty text is refused rather than sent, since a bare Enter into an empty box
    is a no-op the caller almost certainly did not mean, and into a NON-empty one
    submits whatever was left there — the `Escape` case above. A message aimed at
    a session that is `blocked` is refused too, because there it would be read as
    an answer rather than as a message; see the guard below.

    `pane_id`, given explicitly, addresses that exact pane and skips the
    by-name PANE resolution below. `_pane(session_name)` can resolve to ANY
    claude pane inside *session_name* — ordinarily there is only one, but
    during a rollover's handoff window two exist there at once: the
    predecessor's, fenced but still alive until `co_rollover.close_out`
    kills it, and the successor's.

    `pane_id` does NOT skip the `blocked` gate below. That gate reads
    *session_name*'s state, not the pane, so knowing which pane to address
    says nothing about whether a picker is up in front of it — CQ-420 round
    2 shipped exactly that gap, by having `pane_id` bypass both checks at
    once. `skip_blocked_check=True` is the one explicit opt-out, and the
    only caller that sets it is `co_rollover.forward_late_messages`, which
    runs inside the successor's own turn, where a blocked picker cannot be
    up for the gate to protect against in the first place — see that
    function's docstring for why that call site is the exception.
    """
    if not text.strip():
        raise InputError("refusing to send an empty message")
    # Before anything reaches tmux, and before the pane is even resolved, so a
    # rejected message costs no keystrokes at all.
    _reject_control_characters(text)

    # A BLOCKED SESSION HAS A PICKER UP, AND A PICKER EATS A PASTE. The keys
    # that submit a message are the keys that answer a question: the paste lands
    # in the picker's own filter box and the trailing Enter takes whatever row is
    # highlighted. So a message sent at a question does not arrive late, it
    # answers something instead — and the answer it gives is whichever option
    # happened to be under the cursor.
    #
    # The operator watched exactly this on 2026-07-24: a picker came up while he was
    # typing, and his half-written sentence about remote control went into it.
    # Deck guards the same case by drawing buttons instead of a text box, but it
    # learns the state from a two-second poll, so a picker raised inside that
    # window still meets a live composer. This is the guard that does not race.
    #
    # Refused rather than queued. Holding the message until the gate clears
    # would mean sending it at a moment the user is no longer watching, into a
    # session whose turn has moved on; handing it back with a reason lets them
    # answer the question and resend the sentence.
    #
    # KEYED ON `session_name`, NOT ON `pane_id`: `_read_state` never looks at
    # a pane, so this runs the same whether or not the caller resolved one by
    # hand, and a future caller that passes `pane_id` for CQ-420's
    # pane-agreement reason still gets it. `skip_blocked_check` is the only
    # way around it; see the docstring above.
    state = None if skip_blocked_check else _read_state(session_name)
    if state == "blocked":
        raise InputError(
            "this session is waiting on a question; answer it before sending a "
            "message, or the message becomes the answer")

    pane = pane_id if pane_id is not None else _pane(session_name)

    # THE CLEAR NO LONGER INTERRUPTS, AND THAT IS THE POINT OF `CLEAR_KEYSTROKES`.
    #
    # It used to be Ctrl-C, which empties the box and also kills whatever the
    # session is doing. A first round stopped it firing at a `working` session,
    # after it cut off a tool call — but the allowed set kept `waiting`, and
    # `waiting` is exactly what a main agent reads while a background subagent
    # works on behind it. So every message sent to a session with background work
    # killed that work, intermittently, with nothing to show the caller had done
    # it. The operator lost two selector build runs this way on 2026-07-24.
    #
    # `C-u` kills the input line without interrupting anything — measured, see
    # CLEAR_KEYSTROKES. The state gate stays because it still describes WHEN a
    # clear is wanted rather than when it is safe: the box holds unexpected text
    # exactly when `Escape` restored an interrupted message into it, and Escape
    # leaves the session idle. It is no longer load-bearing for safety, which is
    # the difference that matters — a future state that slips through this set
    # now costs a concatenated message rather than somebody's half-finished run.
    # `pane_id` given means no clear runs regardless of `state` (`state` may
    # still have been read above, for the `blocked` gate) — a stray draft to
    # clear away is not this call's concern either way, since the only
    # callers that pass it (post-pickup forwarding, and `slate-send`'s
    # resolve-once pane) target a pane that was just spawned, is mid-turn, or
    # was just proven not `blocked`, not one an `Escape` could have left
    # dirty.
    if pane_id is None and state in _CLEARABLE_STATES:
        _clear_input(pane)

    # `load-buffer -` takes the text on stdin, so it never becomes an argument
    # and never reaches a shell. A named buffer so a concurrent paste elsewhere
    # cannot swap out what is about to be pasted here.
    if TT.run(["tmux", "load-buffer", "-b", _BUFFER, "-"], stdin=text) is None:
        raise InputError("could not stage the message in a tmux buffer")

    # `-p` requests bracketed paste, which is the whole point: the block arrives
    # as one unit and Claude Code's typing handlers never see it.
    _tmux("paste-buffer", "-p", "-b", _BUFFER, "-t", pane)
    _tmux("delete-buffer", "-b", _BUFFER)

    # THE TWO VERIFICATION STEPS THE SPEC LEFT OPEN. Neither is defensive
    # padding — both observed failures were silent: an Enter sent before the
    # paste settled submitted nothing, and a half-submitted message left
    # sitting in the box reads as sent to a caller that never checked. See the
    # section above this function for what was re-verified and against which
    # Claude Code build.
    _await_paste_landed(pane, text, settle)
    send_key(session_name, "enter", pane_id=pane)
    _await_submitted(session_name, pane, text, settle)
    return pane


def focus_free_text(session_name: str, settle: float = ARROW_SETTLE_SECONDS,
                    expect_gate: str | None = None,
                    expect_question: int | None = None) -> str:
    """Put the highlight on a MULTISELECT question's free-text row. Returns the pane.

    WHY A WALK AND NOT A DIGIT. On a single-select question the free-text row's
    digit focuses the field, which is what `type_text`'s docstring describes and
    what Deck drives there. On a MULTISELECT the same digit only TICKS the row,
    the highlight does not move, and a paste into an unfocused field is silently
    swallowed. Navigating the highlight onto the row is the only way in, and
    convivy-deck #60 withdrew the free-text card entirely for want of it.

    THE LIST WRAPS, WHICH RULES OUT COUNTING ARROWS. The obvious walk — send
    `Up` enough times to pin the highlight at the top, then `Down` to a computed
    row — assumes an end to clamp against. There is none: measured against 2.1.220
    on 2026-07-31, `Up` from the first option lands on the FREE-TEXT ROW rather
    than stopping, so a counted walk lands wherever the modular arithmetic happens
    to put it. No count is safe, and this is the observation that decided the
    design.

    SO THE WALK READS THE SCREEN BETWEEN ARROWS AND STOPS ON WHAT IT SEES.
    `picker_render` already publishes the one positional fact this needs:
    `commitByButton` is true with the highlight on the free-text row, on the
    commit button, and on `Chat about this`, and false on every option row. It
    was derived for `submit_choices` to know whether `Right` would be inert; the
    same field read repeatedly is a position sensor, and it needs no new marker.

    Hence two phases, and neither counts anything:

      1. While the sensor reads true, send `Up`. That steps off the free-text
         group onto the last option row, from wherever in the group the highlight
         started — the row itself, the button, or `Chat about this`.
      2. Send `Down` until the sensor reads true again. The first true going down
         IS the free-text row, because the row sits above both the button and
         `Chat about this`.

    Entering already on an option row skips phase 1 and phase 2 walks the whole
    way down, so the same two phases cover every starting position.

    NO READER MEANS NO WALK, and that refusal is the honest one. Without
    `commitByButton` there is no way to know where the highlight is, and the only
    alternative is the counted walk the wrap above rules out — which would tick or
    submit a row nobody chose. `submit_choices` can fall back to its old blind
    drive because that drive was correct before the reader existed; this verb has
    no such past to fall back to, so it raises and Deck keeps the card withheld.

    ONLY WHEN THE SESSION IS BLOCKED, for `type_text`'s reason: arrows into an
    ordinary input box move a cursor through a draft nobody meant to edit.

    Observed end to end against a live 2.1.220 two-question gate on 2026-07-31:
    walk, paste, then `submit_choices`, which takes its `commitByButton` branch
    from the row this leaves the highlight on and commits with `Tab`, `Enter`.
    The review tab showed the typed text as the whole answer, with no option
    ticked alongside it.
    """
    if picker_render is None:
        raise InputError(
            "no picker reader, so the free-text row cannot be found; the walk "
            "needs `commitByButton` to know where the highlight is")

    state = _read_state(session_name)
    if state != "blocked":
        raise InputError(
            f"this session is {state}, not waiting on a question; arrows would "
            "move a cursor through whatever is in its input box")

    _check_expectation(session_name, expect_gate, expect_question)
    pane = _pane(session_name)

    def sensor() -> dict:
        """What the picker is showing right now. Never None, so a failed capture
        reads as "nothing established" rather than raising mid-walk."""
        return picker_render.read(session_name) or {}

    rendered = sensor()
    free_text_row = rendered.get("freeTextRow")
    if free_text_row is None:
        # Absent means "not established", never "no free-text row" — an
        # unrecognised version drops every field. Either way there is nothing to
        # walk to, and pressing arrows at a picker this module cannot read is
        # exactly what the version check exists to prevent.
        raise InputError(
            "the picker reports no free-text row, so there is nothing to focus")

    # Phase 1 — step off the free-text group, upward.
    for _ in range(_FREE_TEXT_GROUP_ROWS):
        if not rendered.get("commitByButton"):
            break
        send_key(session_name, "up", pane_id=pane)
        time.sleep(settle)
        rendered = sensor()
    else:
        raise InputError(
            "the highlight never left the free-text group walking up; the "
            "picker's layout is not the one this was measured against")

    # Phase 2 — walk down to the first row the sensor claims. `freeTextRow` is
    # the row's own number, so it bounds the distance from any option row above
    # it, and the loop asserts arrival rather than assuming it.
    for _ in range(free_text_row):
        send_key(session_name, "down", pane_id=pane)
        time.sleep(settle)
        rendered = sensor()
        if rendered.get("commitByButton"):
            return pane

    raise InputError(
        "the free-text row was not reached walking down; the highlight is "
        "somewhere this module did not put it deliberately")


def type_text(session_name: str, text: str,
              expect_gate: str | None = None,
              expect_question: int | None = None) -> str:
    """Put text into a live picker WITHOUT answering it. Returns the pane.

    THIS IS `send_message` WITH THE TWO DANGEROUS PARTS REMOVED, and the removals
    are the whole verb: no clear, and no trailing `Enter`. What is left is a
    bracketed paste into whatever field the picker has focused.

    WHY IT EXISTS. Claude Code's question picker appends a `Type something.` row
    after the model's options. Selecting that row focuses a text field, and until
    now nothing could fill it: `send_message` refuses at a `blocked` session
    precisely because its clear-paste-Enter would answer the picker rather than
    type into it, and `send_key` carries no literal text. So a free-text answer
    was reachable from a terminal and from nowhere else. convivy-deck #54 tried
    it with `send_message`, failed on every attempt, and was reverted.

    ONLY WHEN THE SESSION IS BLOCKED, which is the exact inverse of
    `send_message`'s guard, and the inversion is the point. A paste with no
    `Enter` into an ordinary input box leaves a half-written message sitting there
    for the next keystroke to submit — worse than useless. Inside a picker it is
    the only safe shape. Narrow on purpose: widening later is cheap, and
    narrowing once callers depend on it is not.

    THE CALLER STILL OWNS THE SEQUENCE, AND THERE ARE TWO OF THEM. This is the
    middle step of both; what differs is how the field is focused before it and
    how the answer is committed after. Verified against Claude Code 2.1.220.

    On a SINGLE-SELECT question:

        answer_choice(n)   # the `Type something.` row: FOCUSES, does not confirm
        type_text(...)     # here
        send_key("enter")  # commits it as the answer

    That first step is the asymmetry to remember: a digit on a model-supplied
    option selects and confirms, and a digit on the free-text row does not.

    On a MULTISELECT question every one of those steps is wrong:

        focus_free_text()  # the digit would TICK the row; the highlight walks
        type_text(...)     # here
        submit_choices()   # `Enter` here would tick whatever row is highlighted

    The `Enter` is the dangerous one. With the field unfocused it ticks the row
    the highlight still sits on, producing an answer nobody chose — which is what
    convivy-deck #60 withdrew the free-text card over, after this sequence was
    driven unscoped against both question kinds. Spec §7.11.

    Bracketed paste rather than `send-keys -l` for the reason
    `deck/specs/session-pane-input-channel` records: typed `/` and `@` fire
    autocomplete, so a note beginning with `/` would leave a slash menu open for
    the next keystroke to select.
    """
    if not text.strip():
        raise InputError("refusing to type an empty string")
    # Before the pane is resolved, so a rejected string costs no keystrokes.
    _reject_control_characters(text)

    state = _read_state(session_name)
    if state != "blocked":
        raise InputError(
            f"this session is {state}, not waiting on a question; typing without "
            "a trailing Enter would leave text in its input box for the next "
            "keystroke to submit")

    _check_expectation(session_name, expect_gate, expect_question)
    pane = _pane(session_name)

    # Same staging as `send_message`: `load-buffer -` takes the text on stdin so
    # it never becomes an argument, and a named buffer stops a concurrent paste
    # swapping out what is about to land.
    if TT.run(["tmux", "load-buffer", "-b", _BUFFER, "-"], stdin=text) is None:
        raise InputError("could not stage the text in a tmux buffer")
    _tmux("paste-buffer", "-p", "-b", _BUFFER, "-t", pane)
    _tmux("delete-buffer", "-b", _BUFFER)
    return pane


def answer_choice(session_name: str, choice: int,
                  expect_gate: str | None = None,
                  expect_question: int | None = None) -> str:
    """Take option `choice` on a numbered prompt.

    WHAT ONE DIGIT DOES DEPENDS ON THE PROMPT, and the caller has to know which
    it is holding. On a permission prompt and on a single-select question the
    digit selects AND confirms, so this call is the whole answer. On a
    multiselect question it only ticks a box: the picker stays up until
    `submit_choices` takes the review tab. A caller that stops here on a
    multiselect leaves the session blocked on a question it has already been
    given the answer to.

    Deliberately not `send_message(str(choice))`: that would clear, paste and
    submit, and a prompt is not a text box.

    REFUSES WHERE THE DIGIT WOULD BE TYPED INSTEAD OF SELECTING. The picker's
    free-text field takes focus as soon as the highlight reaches it, and a digit
    then lands in it as a character: sending `1` to tick the first option produced
    `4. [✔] 1`. So a caller asking for option 1 would get a free-text answer nobody
    wrote, attributed to the user, on a gate that can be submitted before anyone
    reads the review tab. `picker_render` reports that position as
    `digitBecomesText` and this refuses on it.

    REFUSING RATHER THAN MOVING THE HIGHLIGHT FIRST, deliberately. The machinery to
    walk it off exists now (`focus_free_text`), so a "clear the way, then send"
    version is buildable — and it would make this verb move a cursor the caller
    never asked it to move, in a picker where every extra keystroke has cost
    someone an answer. The caller asked to tick option N; the honest answer from
    this state is that it cannot, with a message saying why.

    THE REFUSAL IS NARROW, and the blind case is deliberately not it. An
    unrecognised Claude Code version, a failed capture, or an unlocatable cursor
    all leave the field absent, and this sends the digit exactly as it always has.
    Widening it to "refuse unless proven safe" would take the verb away on a
    version bump — the same trade `submit_choices` makes, and for the same reason.

    GATE BINDING via `expect_gate` and `expect_question`. The digit answers
    whatever prompt is live at the instant it lands, and Deck's read of which
    prompt is live can be up to one poll interval (~2s) stale. Passing the caller's
    observed `gateId` and `questionIndex` narrows the window to the
    capture-to-keystroke gap on the mini. The check runs immediately before the
    keystroke, not in the caller. If either field is absent on the mini — an
    unrecognised Claude Code version, a failed capture — the digit goes out as
    before; the residual gap is documented in `_check_expectation`.
    """
    if not 1 <= choice <= 9:
        raise InputError(f"choice must be 1-9, got {choice}")
    # Checked here rather than in `send_key`, which has callers that legitimately
    # want a literal key into a focused field. This verb's contract is "take
    # option N", and that is what cannot be honoured from this position.
    rendered = picker_render.read(session_name) if picker_render is not None else None
    if (rendered or {}).get("digitBecomesText"):
        raise InputError(
            "the picker's free-text field has the highlight, so this digit would "
            "be typed into it rather than take an option; move off that row before "
            "choosing")
    _check_expectation(session_name, expect_gate, expect_question)
    return send_key(session_name, str(choice))


def submit_choices(session_name: str, settle: float = REVIEW_SETTLE_SECONDS,
                   expect_gate: str | None = None,
                   expect_question: int | None = None) -> str:
    """Finish a multiselect question whose boxes are already ticked.

    A MULTISELECT'S TICKS ARE ALREADY RECORDED; what this does is leave the
    question. `Right` off it is the whole commit, which is why there is no verb
    for the ticks themselves — `choose` makes them one at a time and the tab flips
    to `☒` on the first one. Observed 2.1.220: ticking, tabbing away, and coming
    back to the review tab showed the ticked option as the answer.

    `Right` rather than the counted arrow walk down to the button the list layout
    invites: arrows sent in succession are dropped (twelve moved the cursor two
    rows), so a walk lands somewhere nobody chose, and `Down` clamps on "Chat
    about this". The button carries no digit either, so it cannot be taken
    directly.

    EXCEPT WHERE `Right` IS INERT, which is the one place that reasoning fails.
    With the highlight on the free-text row it moves the caret inside the text,
    and with the highlight on the button it does nothing — so from either position
    this function used to return having changed nothing, and the caller had no way
    to tell. `picker_render` reports that condition as `commitByButton`, read off
    the footer hint that is present in exactly those two positions and absent on
    every option row, and the commit there is `Tab` (a no-op once on the button)
    then `Enter`. Observed 2026-07-30 against 2.1.220; spec §7.11.

    WHERE `Right` LANDS IS THE WHOLE QUESTION, AND THE SCREEN ANSWERS IT. Off the
    LAST question it lands on the review tab, where `1` takes "Submit answers".
    Off any earlier question it lands on the NEXT QUESTION, where that same `1`
    answers a question nobody chose — with its first option, silently.

    So the trailing digit is sent only when the picker says the commit submits.
    `picker_render` reports `multiSelectCommit` from the TUI's own button, which
    reads `Submit` on the last question and `Next` before it. This used to be a
    precondition the caller kept ("single-question gates only"), which was both
    over-broad and unenforceable: the real constraint was never about how many
    questions the gate has, only about whether this is the last one.

    When the reader cannot tell — a Claude Code version it has not been checked
    against, a capture that failed — the OLD precondition applies unchanged and
    the digit is sent. That keeps a blind mini behaving exactly as it does today
    rather than losing the verb on a version bump, and Deck offers this on a
    multi-question gate only where `rendered` is present to begin with.

    Returns the pane id.
    """
    pane = _pane(session_name)
    # `picker_render is None` when the module could not be imported, which the
    # guard above makes a supported state rather than a crash. Checking it here is
    # what makes that guard mean anything: without it this line raises
    # `AttributeError` and the documented fall-back to the old drive never happens.
    # Raised in review of #312 — the import was made optional and then called
    # unconditionally, which is the guard's own failure mode.
    rendered = picker_render.read(session_name) if picker_render is not None else None
    _check_expectation(session_name, expect_gate, expect_question)
    commit = (rendered or {}).get("multiSelectCommit")
    if (rendered or {}).get("commitByButton"):
        # `Right` IS INERT HERE, so sending it would be a silent no-op. With the
        # highlight on the free-text row it moves the caret inside the text; with
        # the highlight already on the button it does nothing at all. Deck pressed
        # its commit button and the gate did not move, which is how this was found.
        #
        # `Tab` reaches the button from the free-text row and is a NO-OP once on
        # it, both observed, so this needs no idea of which of the two positions it
        # is in. `Enter` then presses the button, which is the same commit `Right`
        # performs from an option row — it lands on the next question or on the
        # review tab exactly as the button's own word says.
        send_key(session_name, "tab", pane_id=pane)
        # THIS SETTLE GUARDS A DIFFERENT THING from the one below it, and shares a
        # constant with it only because both measured to the same value. Below,
        # the wait is for the review tab to finish DRAWING before a digit lands on
        # it. Here it is for the HIGHLIGHT to reach the button before `Enter`
        # confirms — and an `Enter` that arrives early lands on the free-text row,
        # which is the state this whole branch exists to get off. Measured at 0.6s
        # against a live 2.1.220 gate. Anyone tuning `REVIEW_SETTLE_SECONDS` down
        # on a faster review-tab reading has to re-verify this path too.
        time.sleep(settle)
        send_key(session_name, "enter", pane_id=pane)
    else:
        send_key(session_name, "right", pane_id=pane)
    if commit == "next":
        # Arrived at the next question. Answering it is precisely the bug.
        return pane
    time.sleep(settle)
    send_key(session_name, _SUBMIT_ANSWERS, pane_id=pane)
    return pane


def interrupt(session_name: str) -> str:
    """Stop an in-flight response.

    Named rather than left as `send_key(..., "escape")` because of the effect
    that is easy to forget: the interrupted message returns to the input box.

    THE WINDOW A CALLER MUST NOT RACE. `send_message` clears that box, but only
    once the session reads `idle` — and the `Stop` hook that makes it read idle
    lands some milliseconds AFTER this returns. A caller that interrupts and
    immediately pastes can therefore hit a session still reading `working`,
    skip the clear, and submit the restored message concatenated with the new
    one. Poll `session_state` until it settles rather than sending straight
    after this.

    Nothing enforces that here on purpose: blocking until the state settles
    would put a state-poll round trip inside every interrupt, to close a window
    narrower than the time it takes a human to type the next message. Named so
    a future caller that IS fast enough to lose the race knows what happened.
    """
    return send_key(session_name, "escape")


# --- unqueue: pulling Claude Code's own held queue back out -----------------
#
# Deck could already send `key up`, the keystroke Claude Code's own TUI
# advertises ("Press up to edit queued messages"). MEASURED ON THE MINI
# AGAINST CLAUDE CODE 2.1.228, in a throwaway tmux session, that keystroke is
# unsafe to drive blind:
#
#   * `Up` on an EMPTY queue recalls a previously-SENT message from HISTORY
#     into the box, headed `─── History n/n ───`. No `popAll` record is
#     written, so nothing distinguishes this from a genuine pop except the
#     transcript's silence.
#   * The race that produces that is not contrived: two queued messages
#     drained as one `dequeue` pair at the SAME millisecond when a turn
#     ended, and an `Up` ~2s later hit the now-empty queue and recalled
#     history. Deck's own poll cannot close this — `held` moving from
#     populated to `[]` looks identical whether the queue popped or drained
#     naturally.
#   * `Up` on a LIVE queue emits one `popAll` record PER MESSAGE, each
#     carrying its own `content`, in queue order — and `popAll` REMOVES THEM
#     PERMANENTLY. Text not captured at the press is gone; there is no
#     second read.
#   * `Up` against a NON-EMPTY box DESTROYS the draft, replacing it with
#     history outright.
#
# Every discriminator above is read off the MINI — Deck cannot tell a popped
# queue from a natural drain, or a live draft from an empty box, without a
# round trip it does not have time for. So this does the whole
# read-verify-act sequence in one call, the same argument
# `focus_free_text`'s docstring makes for walking the picker highlight here
# rather than from Deck. See `deck/specs/session-pane-input-channel` for the
# full measurement record (M1-M5) this is built from.

# How long to keep looking for the `popAll` records after `Up`, and how often
# to re-read the tail while waiting.
#
# MEASURED, on the mini against Claude Code 2.1.228, five trials in a throwaway
# tmux session, polling from the instant `Up` was sent: 130, 124, 116, 117 and
# 140 ms to the whole batch appearing, at two and three queued messages. The
# budget is roughly fourteen times the slowest of those, which is generous on
# purpose — the cost of waiting is a moment, and the cost of giving up early is
# a queue this verb then destroys (see `unqueue`'s no-records branch).
#
# A DEADLINE THAT POLLS, NOT A FLAT SLEEP. A sleep sized to the measurements
# would return no faster in the common case and would still be a guess in the
# tail; polling returns in about 120ms when the write is prompt and spends the
# whole budget only when it is not.
POPALL_DEADLINE_SECONDS = 2.0
POPALL_POLL_INTERVAL_SECONDS = 0.02

# How many tail reads pass between checks for the history divider, which ends
# the wait early. Ten of them is about 200ms — comfortably longer than the
# render, and cheap enough that the extra `capture-pane` does not dominate a
# loop whose other read is a seek.
_RECALL_CHECK_EVERY = 10


def _popall_messages(tail: str) -> list[str]:
    """The `content` of every `popAll` `queue-operation` record in *tail*, in
    the order they appear — which is queue order: `agent-platform/contracts/
    claude-transcript-jsonl` measured one `popAll` record per message, each
    carrying its own content, all at the same timestamp.

    Pure, so it is testable against constructed JSONL without a live
    transcript — mirrors the `parse`/read split the rest of this module
    uses. A line that fails to parse, is not an object, is not a
    `queue-operation`, is not a `popAll`, or carries no string `content` is
    skipped rather than fatal — the same tolerance `session_state.fold_queue`
    gives an operation outside its vocabulary. Skipping a malformed entry
    here costs one message from the returned list rather than the whole
    read, which is the right side to fail on: the caller can still read the
    ones that did parse, on a record shape Claude Code has never been
    observed to write.
    """
    out: list[str] = []
    for line in tail.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("type") != "queue-operation":
            continue
        if rec.get("operation") != "popAll":
            continue
        content = rec.get("content")
        if isinstance(content, str):
            out.append(content)
    return out


def _history_recall_drawn(capture: str) -> bool:
    """Whether the pane is showing Claude Code's history recall.

    MEASURED, mini, Claude Code 2.1.228: `Up` against an empty queue replaces
    the input box's TOP rule with a labelled divider, plain form
    `─── History 1/1 ────────…`, and leaves the bottom rule bare. A genuine
    pop (M3) draws both rules bare. So the divider is present in exactly one
    of the two cases this has to tell apart.

    Its first consequence is upstream of this function and worth naming here,
    because it is what makes the check necessary rather than decorative: the
    labelled line is not a bare rule, so `_parse_input_box` finds one border
    where it needs two and `_read_input_box` returns `None` for the whole
    recall. Absence of the box is therefore the SYMPTOM of a recall, and this
    is the positive confirmation of it.

    Pure, and checked POSITIVELY on purpose. `unqueue` clears the box when
    this returns True, so the failure that matters is a marker that has moved
    in a later Claude Code release: this then returns False, `unqueue`
    refuses, and the box is left alone. Inferring the recall from the missing
    box instead would fail the other way, clearing on every reason the box
    cannot be read — including a picker that took the composer, which
    `_poll_box_content`'s docstring records as a real cause of exactly that
    `None`.

    TWO GUARDS, BOTH FOR THE REASON `_BOX_BORDER_FG_CODES` EXISTS, and
    neither is optional. The shape alone is forgeable: pane content is partly
    written by the session itself, which is what let two plain `─` lines
    around chosen text read back as the live box in #387's security review.

      * The divider must be DRAWN in one of the border colours, checked
        through the same parsed-SGR `_drawn_with_any` the box rules use, so a
        line that merely contains the characters does not qualify.
      * The scan is scoped to the box's OWN upper rule — walk up from the
        bottom-most drawn rule and take the first rule-shaped line above it —
        rather than run over the whole capture. A capture is mostly
        transcript, and the transcript is the session talking.

    Both matter here more than anywhere else in this module, because the
    literal string `─── History 1/1 ───` appears verbatim in this file's own
    comments and in the KB spec behind it. A `co` session working on THIS
    code can put that text on its own screen, and an unguarded shape check
    would read it as a recall and clear a box holding a real popped queue.
    Caught by deck-reviewer.
    """
    lines = capture.splitlines()
    drawn_rules = [
        i for i, ln in enumerate(lines)
        if _BOX_BORDER.match(_input_box_plain(ln).rstrip())
        and _drawn_with_any(ln, _BOX_BORDER_FG_CODES)
    ]
    if not drawn_rules:
        return False
    # Upward from the box's lower rule to whatever brackets it above. The
    # recalled text sits between them and can run to several lines.
    for i in range(drawn_rules[-1] - 1, -1, -1):
        plain = _input_box_plain(lines[i]).strip()
        if not plain:
            continue
        if _BOX_BORDER.match(plain):
            # A bare rule above the content: an ordinary box, not a recall.
            return False
        if plain.startswith("───") and plain.endswith("─") and "History" in plain:
            return _drawn_with_any(lines[i], _BOX_BORDER_FG_CODES)
    return False


# The most this will read back after a press. Nothing legitimate comes close:
# what is being read is whatever the transcript gained inside a two-second
# deadline, and a `popAll` batch is a handful of lines. The cap exists because
# the read is unbounded BY CONSTRUCTION otherwise — a busy subagent writing
# tool output into the same file during the poll window is the realistic way
# it grows — and an oversized tail fails closed as "could not be read" rather
# than being pulled into memory. Raised by security-reviewer.
_TAIL_MAX_BYTES = 4 * 1024 * 1024


def _read_transcript_tail(path, offset: int) -> "str | None":
    """The transcript's bytes from *offset* to EOF, or `None` on a read
    failure or a tail past `_TAIL_MAX_BYTES`. Split from the parse above for
    the reason `_read_input_box` is split from `_parse_input_box`: the I/O is
    what a live session can fail at, and the parse is what a test can pin
    without one.

    `None` FOR TOO-LARGE IS THE SAME ANSWER AS `None` FOR UNREADABLE, and
    deliberately so: `unqueue` already treats an unreadable tail as "refuse
    and leave the box alone", which is the right response to both.
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            # One byte past the cap, so a tail exactly at the limit is kept
            # and anything beyond it is detected without reading it all.
            data = fh.read(_TAIL_MAX_BYTES + 1)
    except OSError:
        return None
    if len(data) > _TAIL_MAX_BYTES:
        return None
    return data.decode("utf-8", "ignore")


# --- slate-send's receipt: confirming delivery in the transcript ------------
#
# CQ-420 / issue_01M4BNS7KKGDWBPF1XFPV8XMPV. `_await_submitted` answers "is the
# input box empty or unreadable", and reports success on both — a trade this
# module accepts for `send_message`'s own callers (see that function's
# docstring) so the verb survives a version bump. `slate-send` cannot accept
# the same trade: it is the one caller whose "success" writes a Slate card to
# `answered`/`sent`, which stops the operator from ever being asked again. A
# live reproduction against Claude Code 2.1.292 found the input box reading
# `None` (unreadable) on EVERY capture, busy or idle alike — the border-colour
# guard (then `_BOX_BORDER_FG`, now `_BOX_BORDER_FG_CODES`, fixed under
# CQ-422) was measured against 2.1.227 and the border was drawn in a different
# grey on this build — so `_await_submitted` reported every slate-send
# "submitted" without ever having verified anything. The same reproduction
# showed the paste landing correctly regardless: a plain `user` record when
# the session was idle, and a `queue-operation` (`enqueue`, later `remove`
# with `reason: "absorbed_mid_turn"`) when it was mid-tool-call. Confirming
# against one of those two record shapes, rather than the screen, is the
# receipt that does not go dark on a drawing change.
SLATE_DELIVERY_DEADLINE_SECONDS = 5.0
SLATE_DELIVERY_POLL_INTERVAL_SECONDS = 0.1


def _record_first_line(rec: dict) -> "str | None":
    """The first line of *rec*'s delivered text, or `None` if *rec* is
    neither shape a slate delivery can land as.

    A plain `user` record (`message.content`, a string or a block list) is
    what Claude Code writes when the session is idle; a `queue-operation`
    record (its own `content`, always a string — see `_popall_messages`) is
    what it writes when the session is mid-turn, immediately on enqueue
    rather than only once the queue later drains. Both measured live against
    a real Claude Code 2.1.292 session; see the section note above.
    """
    rtype = rec.get("type")
    if rtype == "queue-operation":
        content = rec.get("content")
    elif rtype == "user":
        msg = rec.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
    else:
        return None
    if isinstance(content, list):
        content = "\n".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text")
    if not isinstance(content, str) or not content:
        return None
    return content.split("\n", 1)[0]


def resolve_pane(session_name: str) -> tuple[str, int]:
    """The `(pane_id, claude_pid)` driving *session_name*, resolved ONCE.

    CQ-420 PANE AGREEMENT. `transcript_offset` and `send_message` used to
    each resolve the target pane independently — `transcript_offset` via
    `TT.transcript_for_tmux_session`'s own walk, `send_message` via
    `_pane`/`TT.claude_pane`. During a rollover handoff two live claude panes
    share one tmux session: the predecessor's, fenced but still alive until
    `co_rollover.close_out` kills it, and the successor's. The two walks are
    not the same algorithm — `transcript_for_tmux_session` skips a pane whose
    run-dir record cannot be found and tries the next one, while
    `claude_pane` returns the first pane with a claude process regardless of
    its record — so a predecessor whose record has aged out of the search
    window sends `claude_pane` there while `transcript_for_tmux_session` falls
    through to the successor. A caller confirming delivery would then watch
    the WRONG pane's transcript and report "answered" without the message
    ever having reached the pane it was supposedly confirming.

    A `slate-send` call resolves this once, before anything else, and passes
    it to `transcript_offset` (as `pane=`) and `send_message` (as `pane_id=`)
    so both act on the one pane `claude_pane` named.
    """
    return TT.claude_pane(session_name)


def transcript_offset(session_name: str,
                      pane: tuple[str, int] | None = None) -> tuple[Path, int]:
    """The transcript of the claude session running in *session_name*, and
    its current size in bytes — resolved and read together, before a send,
    so `await_delivery` can look only at what the transcript gains AFTER
    this call rather than matching a line already there.

    `pane`, given explicitly as `(pane_id, claude_pid)` from `resolve_pane`,
    resolves the transcript for THAT pane alone via
    `TT.transcript_for_claude_pid`, rather than independently re-walking the
    session's panes. See `resolve_pane`'s docstring for the rollover hazard
    this closes.

    Raises `JoinError` (from `transcript_for_tmux_session`/
    `transcript_for_claude_pid`) or `InputError` (on a failed `stat`) rather
    than returning a sentinel: both are refusals a slate-send caller must
    treat as "could not confirm", not as "the transcript is empty".
    """
    if pane is not None:
        _, claude_pid = pane
        transcript = TT.transcript_for_claude_pid(claude_pid)
    else:
        transcript = TT.transcript_for_tmux_session(session_name)
    try:
        return transcript, transcript.stat().st_size
    except OSError as e:
        raise InputError(
            f"could not read the transcript for {session_name!r} to "
            f"confirm delivery: {e}") from e


def await_delivery(session_name: str, text: str, transcript: Path, since: int,
                   deadline: float = SLATE_DELIVERY_DEADLINE_SECONDS) -> None:
    """Block until *transcript* gains a `user` or `queue-operation` record
    whose text starts with *text*'s first line, written after byte offset
    *since*. Returns on confirmation; raises `InputError` if *deadline*
    passes with none — the caller's cue to leave its own record unchanged
    and exit non-zero, per CQ-420.

    `since` is a byte offset a caller resolved before sending, via
    `transcript_offset` — never re-derived here — so a line already in the
    transcript before the send (a coincidentally matching first line from an
    earlier turn) cannot be mistaken for this one's receipt.

    THE TIMEOUT MESSAGE NEVER CARRIES *text* OR ITS FIRST LINE. For a
    slate-send ask, *text* is `[slate <id>] Your question: ...` — an
    operator's question, which can quote private data — and the success path
    already reports confirmation without echoing it back. An earlier version
    of this message embedded `first_line`, so the one caller that logs this
    exception verbatim (`bin/session-agent`'s `slate-send`, CQ-420) wrote the
    question into `~/.local/state/convivy/session-agent.log` on every timeout.
    Keeping the text out of the message here, rather than trusting every
    future logger of `str(e)` to redact it, is what makes that true for all of
    them at once.
    """
    first_line = text.split("\n", 1)[0]
    poll_deadline = time.monotonic() + deadline
    while True:
        tail = _read_transcript_tail(transcript, since)
        if tail is not None:
            for line in tail.split("\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and _record_first_line(rec) == first_line:
                    return
        if time.monotonic() >= poll_deadline:
            break
        time.sleep(SLATE_DELIVERY_POLL_INTERVAL_SECONDS)
    raise InputError(
        f"could not confirm delivery to {session_name!r}: no new transcript "
        f"entry matched the sent text within {deadline}s")


def _clear_unless_a_picker_took_the_box(session_name: str, pane: str) -> bool:
    """Clear the input box, unless a question has taken it. True if cleared.

    THE STATE IS RE-READ IMMEDIATELY BEFORE THE KEYSTROKES, which is this
    module's standing discipline for anything that can land on a live gate —
    `_check_expectation`'s callers and `_await_submitted`'s retry both do it,
    and both docstrings say why. `unqueue` confirms what it is looking at and
    then acts on that confirmation a moment later, and a background subagent
    can raise a permission prompt or a question inside that moment, replacing
    the composer with a picker. `_poll_box_content`'s docstring records that
    exact substitution as a live cause of an unreadable box.

    What twelve `C-u` do to a picker is UNTESTED AND UNDOCUMENTED, and this
    module has already been bitten once by a key whose side effect nobody had
    characterised: `C-c` sat in `KEYS` as `clear` until it was found killing
    background agents, and it was removed rather than re-scoped. Not firing
    into an interface nobody has measured is the cheaper side of that lesson.
    Raised by security-reviewer.

    ONLY A POSITIVELY OBSERVED `blocked` WITHHOLDS THE CLEAR. `_read_state`
    hands back `None` for every way it can fail to know, and treating not
    knowing as "a picker is up" would withhold the clear on any session whose
    state cannot be read — leaving a dirty box far more often than it would
    avoid a keystroke, for a substitution that is narrow to begin with. The
    keystroke this withholds is also not destructive on its own: `C-u` sends
    no Enter, so the cost of being wrong here is an unmeasured interaction
    rather than an answered question.
    """
    if _read_state(session_name) == "blocked":
        return False
    _clear_input(pane)
    return True


def unqueue(session_name: str) -> dict:
    """Pull Claude Code's queued messages back out of the queue, and hand
    them back to the caller. Read-verify-act in one call; see the section
    note above for what `Up` does unguarded and why none of it is safe to
    drive blind from Deck's side of an ssh hop.

    Returns `{"messages": [...]}`, oldest first. An EMPTY list is a real,
    positive answer — the queue had already drained, naturally or via a
    history recall this undid — not a partial one; a caller does not need a
    fourth shape to tell "nothing was held" from "something was held and
    lost", because the latter cannot happen here (see the refusal below).

    RAISES `InputError`, matching every other refusal in this module, when
    the box was not observably empty before anything was pressed, or when
    what the press did could not be confirmed afterward:

      * the box could not be read at all — `_read_input_box` returned
        `None`. THIS IS A DELIBERATE DEPARTURE FROM EVERY OTHER CALLER OF
        THAT FUNCTION IN THIS MODULE, where absence means "fall back to the
        blind behaviour this replaces" (see `_await_paste_landed` and
        `_await_submitted`). There is no blind behaviour to fall back to
        here: the blind behaviour IS `Up`, and unguarded it either destroys
        a draft or recalls history into one (M5). A caller that cannot
        verify the box is empty must not press the key that can wipe it.
      * the box was read and is not empty — the M5 case above, and the
        message in the box is the caller's, not this verb's to replace.
      * `Up` was pressed but the transcript could not be read afterward.
        THIS ONE FIRES AFTER THE KEYSTROKE ALREADY WENT OUT, which is why it
        is not treated as "no popAll" and folded into an empty `messages`
        list: on the module's usual OBSERVED-ABSENT doctrine that would
        clear the box on a guess, and if a popAll DID land (M3) that guess
        destroys the only copy of what it returned. The doctrine covers a
        keystroke the CALLER asked for degrading gracefully — the analysis
        `_await_submitted`'s retry-refusal gives for why IT fails closed
        applies here for the same reason: clearing the box is a cleanup this
        verb decided to do on its own, not one anybody asked for, so an
        unconfirmed cleanup is refused rather than risked. The box is left
        exactly as `Up` last drew it, and the caller can still read it
        directly.
      * no `popAll` arrived within the deadline, and what the pane is drawing
        is neither an empty box nor a confirmed history recall. That leaves a
        pop whose records never came, and clearing would destroy the only
        copy of what it returned, so this refuses and leaves the box as `Up`
        drew it. A refusal costs the reader a box they can see and fix; the
        wrong guess costs them messages nothing can recover.

    THE COMMON RACE IS NOT A REFUSAL, and getting that right is most of the
    point of the branch below. `held` can drain between the caller's last
    poll and this keystroke — measured happening on the first attempt, M2 —
    and `Up` then recalls history instead of popping (M1). Refusing there
    would be worse than useless: the recalled message would be LEFT in the
    box, and `send_message` skips its clear while a session is `working`, so
    the next message sent would go out with an already-sent one concatenated
    in front of it. `_await_paste_landed` does not stop that; it gives up on
    the mismatch and presses Enter anyway. So a confirmed recall is cleared,
    which puts the box back exactly where it started, and answered with an
    empty list — the honest report, since the queue really is empty and its
    messages really were read.
    """
    transcript = TT.transcript_for_tmux_session(session_name)
    pane = _pane(session_name)

    box = _read_input_box(pane)
    if box is None:
        raise InputError(
            "the input box could not be read, so pressing Up cannot be "
            "confirmed safe; nothing was sent")
    # NORMALIZED, NOT COMPARED RAW, AND THE PLACEHOLDER IS WHY. When a queue
    # exists Claude Code draws `Press up to edit queued messages` in the box,
    # dim — so `_visible` drops the words as it drops a ghost suggestion, but
    # the SPACES BETWEEN THEM are not dim and survive, and the box parses to
    # "     " rather than "". A raw comparison reads that as a live draft and
    # refuses, which is a refusal in exactly the state this verb exists for:
    # a queue is present precisely when the placeholder is. Found by running
    # the verb against a real session; every unit test passed, because a
    # constructed empty box has no placeholder in it.
    if _normalize_box_text(box) != "":
        raise InputError(
            "the input box is not empty; Up would replace it with history "
            "rather than the queue, so nothing was sent")

    # Caught rather than allowed to escape: a transcript rotated or deleted in
    # the window between resolving the path and this stat is not one of the
    # exception types `bin/session-agent` handles, so it would exit on a raw
    # traceback instead of the `{"ok": false, "error": …}` a consumer parses.
    # No data is at risk here — this is still before the keystroke.
    try:
        offset = transcript.stat().st_size
    except OSError:
        raise InputError(
            "the transcript could not be read, so a pop could not be "
            "confirmed afterward; nothing was sent") from None

    send_key(session_name, "up", pane_id=pane)

    # Poll to the deadline, stopping at the first batch. `popAll` is written
    # per message but the whole batch shares one timestamp (M3), so a read
    # that sees any of it sees all of it — there is no partial batch to wait
    # out once records start appearing.
    #
    # A CONFIRMED RECALL ALSO ENDS THE WAIT, and that is what keeps the common
    # race quick. `Up` either pops or recalls; a recall means no `popAll` is
    # ever coming, so spending the rest of the budget on it would make the
    # most frequent outcome (M2) the slowest one — two seconds of nothing
    # before the reader is told nothing was waiting. Checked on an interval
    # rather than every pass, since it costs a `capture-pane` round trip where
    # re-reading the tail costs a seek.
    deadline = time.monotonic() + POPALL_DEADLINE_SECONDS
    messages: list[str] = []
    recalled = False
    passes = 0
    while True:
        tail = _read_transcript_tail(transcript, offset)
        if tail is None:
            raise InputError(
                "Up was pressed, but the transcript could not be read "
                "afterward; the box was left untouched rather than cleared "
                "on a guess, since a popped queue would be unrecoverable")
        messages = _popall_messages(tail)
        if messages:
            # ONE MORE READ BEFORE COMMITTING, because the batch is N separate
            # JSONL records rather than one atomic write — `session_state`'s
            # `_fold_queue` notes several sharing a timestamp, which is not the
            # same as being flushed together. A poll landing between records
            # would see M of N, and the clear that follows would destroy the
            # rest permanently. Cheap insurance: re-read once and take the
            # larger count, repeating while it is still growing.
            while time.monotonic() < deadline:
                time.sleep(POPALL_POLL_INTERVAL_SECONDS)
                again = _read_transcript_tail(transcript, offset)
                if again is None:
                    break
                grown = _popall_messages(again)
                if len(grown) <= len(messages):
                    break
                messages = grown
            break
        if time.monotonic() >= deadline:
            break
        passes += 1
        if passes % _RECALL_CHECK_EVERY == 0 and _history_recall_drawn(
                TT.run(["tmux", "capture-pane", "-e", "-p", "-t", pane]) or ""):
            recalled = True
            break
        time.sleep(POPALL_POLL_INTERVAL_SECONDS)

    if not messages:
        if recalled:
            # Confirmed above, inside the loop, so it is not re-read here.
            if not _clear_unless_a_picker_took_the_box(session_name, pane):
                raise InputError(
                    "Up recalled history, but a question came up before the "
                    "box could be cleared; the recall was left on screen "
                    "rather than typed into whatever now has focus")
            return {"messages": []}
        # Nothing was popped, as far as the transcript is willing to say. What
        # the pane is drawing decides whether that is safe to act on, and the
        # two benign answers are told apart by different evidence: an empty
        # box parses, a history recall does not parse at all and is confirmed
        # by its own divider instead.
        after = _read_input_box(pane)
        if after is not None and _normalize_box_text(after) == "":
            # The keystroke changed nothing, so there is nothing to clear and
            # nothing was held. Positively known, not inferred. Normalized for
            # the same reason the pre-press check is: a box holding only the
            # placeholder's undimmed spaces is an empty box.
            return {"messages": []}
        # The same check the loop makes, run once more because the loop's
        # last one can predate the divider: a recall that renders between the
        # final interval check and the deadline would otherwise be refused as
        # unexplained, and refusing leaves the recalled text in the box.
        if after is None and _history_recall_drawn(
                TT.run(["tmux", "capture-pane", "-e", "-p", "-t", pane]) or ""):
            if not _clear_unless_a_picker_took_the_box(session_name, pane):
                raise InputError(
                    "Up recalled history, but a question came up before the "
                    "box could be cleared; the recall was left on screen "
                    "rather than typed into whatever now has focus")
            return {"messages": []}
        raise InputError(
            "Up was pressed and no popAll record arrived, but the pane is "
            "showing neither an empty box nor a history recall; the box was "
            "left as Up drew it rather than cleared on a guess, since a "
            "popped queue would be unrecoverable")

    # THE MESSAGES ARE RETURNED WHETHER OR NOT THE CLEAR HAPPENS, and that
    # asymmetry against the two branches above is the point. There, a skipped
    # clear costs nothing, because there is nothing to hand back. Here the
    # messages are already out of the queue and this is their only copy —
    # refusing would destroy them to avoid a dirty box, which is the trade
    # this whole verb exists to refuse.
    _clear_unless_a_picker_took_the_box(session_name, pane)
    return {"messages": messages}
