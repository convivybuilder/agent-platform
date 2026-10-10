"""picker_render — what a question picker is actually showing, read off the pane.

THIS READS THE SCREEN, WHICH `session_state` OPENS BY REJECTING. That rejection
is narrower than it looks and the amendment is argued in
`deck/specs/reading-the-rendered-picker` §4. In short: the rejection's failure
mode is reading the INPUT BOX, where Claude Code's ghost suggestions are
byte-identical to real typed input under `capture-pane -p`. Nothing here reads
the input box. State still comes from hooks.

WHAT IS HERE THAT THE HOOK PAYLOAD CANNOT GIVE. `AskUserQuestion`'s payload is
passed through verbatim, so it carries exactly the options the MODEL wrote. Four
things the TUI adds at render time appear nowhere in it:

  * which question of a multi-question gate is live,
  * the `Type something.` row it appends after the model's options,
  * the `✔ Submit` review tab a multi-question gate ends on,
  * whether committing a multiselect question submits the gate or advances it.

Deck needs the first three: the first so its cursor is an observation rather than
a guess, the second to offer a free-text answer at all, the third because a
multi-question gate does NOT resolve until Submit is taken, and a pane that does
not know the tab exists leaves the session waiting on it.

`submit_choices` needs the fourth, and it is the one fact that turns a documented
precondition into something the code can check. See `_COMMIT_ROW`.

A fifth is read for `answer_choice`: **whether a digit sent now would be TYPED
rather than select a row.** The picker's free-text field takes focus as soon as the
highlight reaches it, and a digit then lands in it as a character — so the general
"take an option" verb silently becomes a free-text answer nobody wrote. See
`digitBecomesText`.

THE CURSOR GLYPH IS LOAD-BEARING FOR BOTH OF THOSE, and it is worth saying once
here because it caused two separate defects. `❯` marks the highlighted row; the
TUI prepends it to whatever line the highlight is on. Reading it as decoration is
what broke `_COMMIT_ROW`'s anchor, and reading it as a position is what makes
`digitBecomesText` possible.

`-e` IS REQUIRED, NOT PREFERRED. The active tab is identifiable only by its SGR
background. Under `-p` every tab is byte-identical apart from `☐`/`☒`, so a user
who navigated back to an already-answered tab is indistinguishable from one who
did not. That is the demonstration the amendment rests on.

EVERY MARKER BELOW IS ANTHROPIC'S TO CHANGE, and the guard against that is a
check performed on every capture rather than a promise to re-read this file
periodically. The check has two halves and needs both:

  * a VERSION FLOOR. Builds older than `MARKERS_VERIFIED_FLOOR` are refused
    outright, because nothing here has been driven against them and markers move
    forward rather than back.
  * a SKELETON SELF-TEST. On a build at or above the floor the capture itself is
    asked whether the picker's bones are still where this module looks — see
    `_skeleton_reason`. A layout that moved refuses exactly as a bad version did.

**Exact version equality was the gate until 2026-08-10, and it was the wrong
one.** It went stale twice in eleven days: the constant read 2.1.220 while the
mini ran 2.1.226, so `read` returned None on every call and Deck withheld the
free-text card and the multiselect commit button for ten days; the input-box
reader one module over was re-verified against 2.1.226 on 2026-08-10 and was
dark the next day on 2.1.227. A patch release invalidated every marker whether
or not the picker had changed, so the readers spent most of their life dark and
nothing said so. The self-test asks the question the version string was standing
in for, and asks it of the screen actually in front of it.

**The refusal is not weakened, and that is the point of the self-test.** A build
whose footer, option grammar or cursor glyph has moved is refused on any version.
What changed is that a build which renders the same picker is now read.

Driven by hand against 2.1.227 on 2026-08-10 — both question views, the
multiselect commit row in its `Next` and `Submit` forms, highlighted and not, the
free-text row in both renderings, `digitBecomesText`, and the review tab. The
captures are in the spec's §7.
"""
from __future__ import annotations

import re

import tmux_transcript as TT

# The oldest Claude Code build these markers have been driven against. Anything
# below it is refused without reading the screen at all: markers are only ever
# verified forward, so a build older than the floor is one nobody has checked and
# the self-test below would be attesting to a layout this module never saw.
MARKERS_VERIFIED_FLOOR = "2.1.220"

# The newest build driven by hand, for provenance rather than for the gate.
# Nothing branches on it. It says how far the by-hand evidence reaches, so a
# reader can tell "verified against this" from "read under the self-test".
MARKERS_LAST_DRIVEN = "2.1.227"

# A version the pane reported that this module could not parse as a version at
# all — `pane_current_command` returning `zsh` because the pane is not running
# Claude Code, or a build numbered in some new way. Refused like a build below
# the floor, and reported separately, because the two say different things about
# what to do next.
_REASON_UNPARSEABLE = "version-unparseable"
_REASON_BELOW_FLOOR = "version-below-floor"
_REASON_NO_CAPTURE = "capture-failed"
_REASON_SKELETON = "markers-moved"
_REASON_NO_PANE = "pane-unresolved"

# Not a refusal at all: the capture holds no picker, because the question was
# answered between the hook read and the capture. Kept distinct from
# `_REASON_SKELETON` so a fast answer does not report a drift that never
# happened — see `_skeleton_reason`.
_REASON_NO_PICKER = "no-picker"

# The active tab and the active single-question header carry the same chip:
# white on the accent. Only the background distinguishes them from their
# neighbours, which is why `-p` cannot be used.
_ACTIVE_BG = "48;5;105"

# `✔ Submit` closes the tab bar of a multi-question gate. Its presence is how a
# tab bar is told from an ordinary line.
_SUBMIT_TAB = "Submit"

# `☐ Colour` unanswered, `☒ Colour` answered. The glyph is not load-bearing for
# the cursor — the highlight is — but it identifies a segment as a tab.
_TAB = re.compile(r"[☐☒]\s+([^☐☒✔]+?)\s*$")

# The row the TUI appends after the model's options.
#
# TWO RENDERINGS, and the second was found by running this parser against a real
# capture rather than by reading one. A single-select question draws
# `4. Type something.`; a MULTISELECT draws `3. [ ] Type something`, with the
# option checkbox and no trailing period. A pattern written from the first
# example silently found nothing on the second, which on a multiselect question
# is exactly where a free-text answer is most wanted.
_FREE_TEXT_ROW = re.compile(r"(\d+)\.\s+(?:\[[ xX]\]\s+)?Type something\.?\s*$")

# The picker's own footer. Every form observed in 2.1.220 ends on one, and it is
# what tells picker rows from transcript text that happens to look like them.
_FOOTER = "Esc to cancel"

# The bare button a MULTISELECT question draws under its options, and the word in
# it is the whole point.
#
# IT IS THE ONLY THING ON SCREEN THAT SAYS WHETHER COMMITTING SUBMITS. A
# multiselect's ticks are recorded as they are made; what finishes the question is
# navigating off it. Where questions follow, that lands on the next one and the
# button reads `Next`; on the last question it lands on the review tab and the
# button reads `Submit`. `submit_choices` sends a trailing digit to take
# `Submit answers` there, and that digit is wrong in the `Next` case because it
# answers the question it just arrived at.
#
# The row carries no number, so nothing can select it by digit. It is bold, and
# indented past the option column. Observed 2026-07-29 against 2.1.220; the
# captures are in the spec's §7.10.
#
# THE CURSOR GLYPH IS OPTIONAL AND THAT IS NOT COSMETIC. When the highlight is on
# the button the TUI prepends `❯`, so the plain line is `❯    Next` rather than
# `     Next`. The first version anchored on `^\s+` and therefore stopped matching
# in exactly that position — and the bold it also requires is still present there,
# so the guard was never what failed:
#
#     unhighlighted   '     \x1b[1mNext\x1b[0m'
#     highlighted     '\x1b[38;5;105m❯\x1b[39m    \x1b[1mNext\x1b[0m'
#
# THE COST OF THAT MISS WAS A PHANTOM ANSWER. `submit_choices` reads the field this
# produces to decide whether to send its trailing digit, and a missing field is not
# `next`, so it sent the digit into the question it had just advanced to. Driven on
# a live gate 2026-07-31 with one option ticked: the following question came back
# answered with its first option, which nobody had chosen. Spec §7.12.
_COMMIT_ROW = re.compile(r"^\s*❯?\s+(Next|Submit)\s*$")

# The picker's own cursor. Which ROW it sits on decides what a digit does, so this
# is read as a position rather than as decoration — see `digitBecomesText`.
_CURSOR = "❯"

# The second row the TUI appends, below the commit button. It is a real highlight
# position and the walk has to tell it apart from the free-text row above.
_CHAT_ROW = "Chat about this"

# What the observed labels mean, normalised here so no consumer compares against
# Anthropic's display string. The words are theirs and can change in any release;
# these two names are ours and the version constant guards the mapping.
_COMMIT_MEANING = {"Next": "next", "Submit": "submit"}

# The footer hint that appears exactly when `Right` will do nothing.
#
# NAMED FOR WHAT IT PREDICTS, NOT FOR WHAT IT SAYS. The string offers a Vim edit,
# and it is tempting to read it as "the free-text field has focus". It is broader
# than that, and the broader reading is the useful one: it is present with the
# highlight on the free-text row AND with the highlight on the commit button, and
# `Right` is inert in both. It is absent on every option row, where `Right` is the
# commit. So it predicts precisely the condition `submit_choices` has to branch
# on, which is why the field derived from it is called `commitByButton` rather
# than anything about focus or editing.
#
# Measured 2026-07-30 against 2.1.220 by walking a multiselect question's
# highlight and reading the footer at each position, including with and without
# text in the row, so "sticky once touched" could be ruled out — the hint clears
# as soon as the highlight moves back above the free-text row. Spec §7.11.
_EDIT_HINT = "ctrl+g to edit in Vim"

# Bold, which is what the commit row is drawn in and what the option rows are
# not. Load-bearing for the same reason `_DIM` is on the free-text row.
#
# AN SGR PARAMETER, LIKE THE TWO BELOW, rather than the literal `ESC[1m` it was
# until 2026-08-10. The literal was the narrower forgery surface of the two —
# it carries its own escape byte, so prose naming it does not match — but it
# missed the case `_draws_with` exists for: the TUI combining attributes into
# one sequence, `ESC[1;38;5;241m`, which `"\x1b[1m" in line` does not find. A
# missed match here is a missed `multiSelectCommit`, the one absent field this
# module's own docs call dangerous rather than limiting. Raised in round 2 of
# review on #387, where it was also the last exception to that commit's claim
# of having converted every style check.
_BOLD = "1"

# The dim grey the picker draws its row numbers and its appended rows in.
_DIM = "38;5;241"

# How far above the footer a picker's rows can be. Generous enough for nine
# options with two-line descriptions, bounded so the scan cannot walk into the
# transcript above.
_PICKER_LOOKBACK = 40

# A numbered row, with or without the highlight in front of it. This is the
# picker's basic grammar — every option, the appended free-text row and the
# review tab's `1. Submit answers` are all drawn this way — so its absence from
# a window that is supposed to hold a picker means the layout moved.
#
# Deliberately looser than `_FREE_TEXT_ROW` and `_COMMIT_ROW`, because it is
# asked a different question. Those two identify ONE row and must not match
# anything else; this one asks whether rows of this shape exist at all, and a
# skeleton test that only passed on the exact rows already parsed would attest
# to nothing the parse had not already established.
_NUMBERED_ROW = re.compile(r"^\s*❯?\s*\d+\.\s")

# A multiselect option row: the number, then the checkbox the TUI draws only on
# a question that takes several answers.
#
# THIS IS WHAT MAKES THE COMMIT ROW REQUIRED RATHER THAN OPTIONAL. A missing
# `multiSelectCommit` is the one absent field that is dangerous — `submit_choices`
# reads "not `next`" and sends its trailing digit, which on a `Next` screen
# answers the question it has just advanced to, and that phantom answer is a real
# shipped defect (spec §7.12). Absence is otherwise indistinguishable from a
# single-select question, which has no commit row at all. The checkbox tells the
# two apart, so `_skeleton_reason` can insist on the commit row exactly where one
# must exist.
#
# ANCHORED AT THE ROW'S OWN START, like `_NUMBERED_ROW` and `_COMMIT_ROW` beside
# it. Unanchored it would also match inside an option's LABEL, which is
# model-authored text passed through verbatim — a question about checkbox
# markdown could carry `3. [ ]` in its own wording and make a genuinely
# single-select picker demand a commit row it never draws. That direction is
# fail-safe, since the worst case is an over-refusal down the same degradation
# path as any other, but an over-refusal is what this whole change exists to stop
# happening for no reason. Raised in review of #387.
_CHECKBOX_ROW = re.compile(r"^\s*❯?\s*\d+\.\s+\[[ xX✔]\]")

# Strips SGR so a line can be matched on its text.
_SGR = re.compile(r"\x1b\[[0-9;]*m")

# The parameters of one SGR sequence, for asking what it does rather than what it
# looks like.
_SGR_PARAMS = re.compile(r"\x1b\[([0-9;]*)m\Z")

# Every SGR sequence on a line, for asking whether the TUI drew something in a
# colour rather than whether the line happens to CONTAIN that colour's digits.
_SGR_SEQ = re.compile(r"\x1b\[([0-9;]*)m")


# The SGR parameters that introduce an extended colour, and are therefore a
# COMPOUND rather than an attribute of their own: `38;5;N` and `48;5;N` are one
# 256-colour selector, `38;2;R;G;B` one true-colour selector. `session_input`'s
# `_visible` already had to know this; `_sgr_operations` is where this module
# learns it.
_EXTENDED_COLOUR = {"38", "48", "58"}


def _sgr_operations(line: str):
    """Each SGR operation on *line*, as one normalised string.

    SPLITTING ON `;` IS NOT PARSING, and the difference is a real bug rather than
    pedantry. `ESC[38;5;1m` selects 256-colour index 1 and its tokens contain a
    standalone `1`, which is the parameter for BOLD — so a check that merely
    looked for the token would read a red option row as the bold commit row. This
    walker consumes the extended-colour compounds as units, so `38;5;1` is one
    operation and yields no `1`, while `ESC[1;38;5;241m` yields both `1` and
    `38;5;241`.

    A MALFORMED COMPOUND ENDS THE SEQUENCE RATHER THAN FALLING THROUGH, and that
    is a guard rather than tidiness. `38` must be followed by `5` or `2`; when it
    is not, the first draft yielded its tokens singly, so `ESC[38;1m` — which
    sets nothing a terminal agrees on — offered a `1` and read as BOLD, an
    attribute the TUI never applied. Nothing after the malformed token is
    trustworthy either, since what the remaining parameters mean depends on how
    the terminal recovered. No sequence in any real capture is malformed this
    way, so refusing them costs nothing observed.
    """
    for sequence in _SGR_SEQ.finditer(line):
        tokens = sequence.group(1).split(";")
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token in _EXTENDED_COLOUR:
                if index + 1 >= len(tokens):
                    break
                if tokens[index + 1] == "5" and index + 2 < len(tokens):
                    yield ";".join(tokens[index:index + 3])
                    index += 3
                    continue
                if tokens[index + 1] == "2" and index + 4 < len(tokens):
                    yield ";".join(tokens[index:index + 5])
                    index += 5
                    continue
                # `38` followed by neither `5` nor `2`, or a compound cut short
                # by the end of the sequence. Nothing here or after it is a
                # parameter this can name honestly.
                break
            yield token
            index += 1


def _draws_with(line: str, params: str) -> bool:
    """Whether a real SGR sequence on *line* performs the operation *params*.

    THE STYLE GUARDS WERE PLAIN SUBSTRING TESTS UNTIL 2026-08-10, AND THAT MADE
    THEM FORGEABLE WITH NO ESCAPE BYTES AT ALL. `_DIM` and `_ACTIVE_BG` are bare
    parameter strings — `38;5;241`, `48;5;105` — with no `ESC[`…`m` around them,
    so `_DIM in line` matched a line of ordinary prose that merely mentioned the
    number. Pane content is partly written by the session itself, so that is text
    a model can print: a capture built entirely from plain text passed the
    skeleton self-test, and a single planted `9. [x] a row 38;5;241` inside the
    lookback window forged the checkbox invariant's precondition and made a
    genuinely intact single-select picker refuse as `markers-moved`. Both
    reproduced against a stock checkout. Found in security review of #387.

    This is `_closes_background`'s discipline applied to the other direction:
    parse the sequence and ask what it does. Matching is against a walked
    operation rather than a token slice, so the TUI combining attributes into
    `ESC[1;38;5;241m` is found, prose outside an escape is not, and a
    256-colour index is never mistaken for the attribute its digits resemble.
    """
    return params in _sgr_operations(line)


def _closes_background(escape: str) -> bool:
    """Whether this SGR ends the chip: a background reset, or a full reset.

    PARSED RATHER THAN SUBSTRING-MATCHED, and both readings of the shortcut were
    wrong. The first version tested `"49m" in piece`, which misses a combined
    reset like `ESC[0;39m` — leaving the chip open so a LATER tab reads as active
    and `questionIndex` comes back wrong rather than absent. Raised by review of
    #303 as latent, gated behind the version constant; a wrong index is the one
    failure mode here worth spending code on, because Deck acts on it.

    The same shortcut also matched too much: `ESC[38;5;149m` contains `49m` and
    would have closed a chip that was still open. Splitting the parameters
    settles both.
    """
    hit = _SGR_PARAMS.match(escape)
    if hit is None:
        return False
    params = hit.group(1)
    if params == "":
        return True  # `ESC[m` is a full reset
    parts = params.split(";")
    return "0" in parts or "49" in parts


def _plain(line: str) -> str:
    return _SGR.sub("", line)


def _tabs(line: str) -> list[tuple[str, bool]]:
    """The tab bar's `(label, is_active)` pairs, in render order.

    Walks the SGR runs rather than splitting on text, because the ONLY thing that
    marks the live tab is the background it is drawn on. A split on `☐`/`☒` would
    find every tab and know nothing about which one is being answered.
    """
    tabs: list[tuple[str, bool]] = []
    active = False
    for piece in re.split(r"(\x1b\[[0-9;]*m)", line):
        if piece.startswith("\x1b["):
            if _draws_with(piece, _ACTIVE_BG):
                active = True
            elif _closes_background(piece):
                active = False
            continue
        if not piece.strip():
            continue
        # A run can hold several tabs when none of them is highlighted.
        for segment in re.split(r"(?=[☐☒])", piece):
            hit = _TAB.search(segment)
            if hit:
                tabs.append((hit.group(1).strip(), active))
    return tabs


def parse(capture: str) -> dict | None:
    """What the picker is showing, from an `-e` capture. None when nothing matched.

    Pure, so the markers can be tested against recorded captures rather than
    against a live session. A live session is how they were FOUND (see the spec's
    §7); it is a poor way to keep them honest afterwards.

    Absent keys mean "not established", never "not offered" — the caller must not
    read a missing `freeTextRow` as "this picker has no free-text row".
    """
    lines = capture.splitlines()
    out: dict = {}

    # --- the tab bar, and with it the cursor -----------------------------------
    #
    # TWO GUARDS, AND DELIBERATELY NOT THE FOOTER. This scan had the same defect
    # the free-text scan below was fixed for: it walked the capture top-down and
    # took the first line carrying `✔`, `Submit` and a checkbox, so a model that
    # DESCRIBED a picker in an earlier turn — `☐ Colour ☒ Size ✔ Submit` — supplied
    # the tab bar instead of the real one. A wrong `questionIndex` puts Deck's
    # cursor on the wrong question; a wrong `onSubmitTab` leaves the session
    # blocked with no action left to take. Raised in review of #303.
    #
    # THE FOOTER ANCHOR CANNOT BE REUSED HERE, and that is observed rather than
    # reasoned: the review tab draws no footer at all. Its last line is
    # `2. Cancel`, and `Esc to cancel` appears nowhere in a capture of it.
    # Anchoring on the footer would have dropped `onSubmitTab` on the one view it
    # exists to report — a worse failure than the one being fixed, because a gate
    # the pane cannot submit hangs.
    #
    # The two guards that hold on both views:
    #
    #   1. The active chip must be in the raw line. A multi-question gate always
    #      has its cursor on the bar, on a question tab or on Submit, and in a
    #      full capture of the review view `_ACTIVE_BG` appeared on exactly one
    #      line: the tab bar. This is the load-bearing-styling guard `_DIM` gives
    #      the free-text row, and unstyled transcript text fails it.
    #   2. Bottom-most wins, because the picker is the bottom-most thing drawn, so
    #      anything the model printed about a picker sits above the real one.
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index]
        if not _draws_with(line, _ACTIVE_BG):
            continue
        plain = _plain(line)
        if _SUBMIT_TAB not in plain or "✔" not in plain:
            continue
        tabs = _tabs(line)
        if not tabs:
            continue
        # The Submit tab is not a question, so it is counted separately.
        questions = [t for t in tabs if t[0] != _SUBMIT_TAB]
        # `_tabs` cannot see the Submit tab, which carries `✔` rather than a
        # checkbox, so an active Submit is read off the chip's own run. The chip is
        # known present — the loop guard above required it.
        submit_active = any(
            active and label == _SUBMIT_TAB for label, active in tabs
        ) or "✔" in _active_run(line)
        out["onSubmitTab"] = bool(submit_active)
        for index, (_, active) in enumerate(questions):
            if active:
                out["questionIndex"] = index
                break
        break

    # --- the appended free-text row -------------------------------------------
    #
    # THREE GUARDS, and the reason is worth stating because the first version had
    # none of them. It scanned the whole capture for the phrase with styling
    # stripped, so any transcript line still visible above the picker reading
    # `2. Type something.` was returned — and Deck SENDS that number as a digit,
    # which would answer option 2 instead of opening the text field. Raised in
    # security review of agent-platform #303, and it is the sharp edge of reading
    # a pane at all: unlike a hook payload, pane content is partly written by the
    # session itself, so a model can put text where this parser looks.
    #
    #   1. Only below the picker's own footer marker, so the scan is inside a
    #      picker rather than anywhere on screen.
    #   2. Only within `_PICKER_LOOKBACK` lines above it, so it cannot walk up
    #      into the transcript.
    #   3. Only a line carrying the dim attribute the TUI draws these rows in.
    #      Styling is load-bearing here for the same reason it is for the tab bar,
    #      and this is the guard `questionIndex` always had and this did not.
    #
    # Nearest the footer wins, because the picker is the bottom-most thing drawn.
    footer = None
    for index in range(len(lines) - 1, -1, -1):
        if _FOOTER in _plain(lines[index]):
            footer = index
            break
    if footer is not None:
        start = max(0, footer - _PICKER_LOOKBACK)
        for index in range(footer - 1, start - 1, -1):
            line = lines[index]
            if not _draws_with(line, _DIM):
                continue
            hit = _FREE_TEXT_ROW.search(_plain(line))
            if hit:
                out["freeTextRow"] = int(hit.group(1))
                break

    # --- the multiselect commit row -------------------------------------------
    #
    # Guarded like the free-text row above, and for the same reason: this scan
    # matches a short, ordinary English word, so it is the easiest thing in this
    # module for transcript text to imitate. Footer anchor, the same bounded
    # window, and bold rather than dim because bold is what the TUI draws it in.
    #
    # Nearest the footer wins. `_COMMIT_ROW` is anchored at both ends, so a line
    # of prose containing the word does not match; only a line that IS the word.
    if footer is not None:
        start = max(0, footer - _PICKER_LOOKBACK)
        for index in range(footer - 1, start - 1, -1):
            line = lines[index]
            if not _draws_with(line, _BOLD):
                continue
            hit = _COMMIT_ROW.match(_plain(line))
            if hit:
                out["multiSelectCommit"] = _COMMIT_MEANING[hit.group(1)]
                break

    # --- whether committing has to go through the button ----------------------
    #
    # Read off the footer line itself rather than scanned for, because that line
    # is already located and the hint is part of it. Absent means `Right` is the
    # commit, which is the behaviour every caller had before this field existed —
    # so an unrecognised version dropping `rendered` leaves the old drive intact.
    if footer is not None and _EDIT_HINT in _plain(lines[footer]):
        out["commitByButton"] = True

    # --- whether a digit would be TYPED rather than select a row ---------------
    #
    # NAMED FOR WHAT IT PREDICTS, which is the correction `commitByButton` already
    # carries and this field needs for the same reason. With the highlight on the
    # free-text row the field has focus, and `answer_choice`'s digit lands in it as
    # a character: sending `1` to tick the first option produced `4. [✔] 1`. A
    # caller expecting a tick gets a free-text answer nobody typed. Observed
    # 2026-07-31 against 2.1.220; spec §7.11 recorded the behaviour and §7.12 the
    # marker.
    #
    # ARRIVING ON THE ROW IS FOCUSING IT. There is no separate focus state to
    # detect — the digit landed as text with nothing yet typed — so this is a
    # position question, and `commitByButton` narrows it to three: the free-text
    # row, the commit button, and `Chat about this`. Only the first is dangerous,
    # and that field cannot tell them apart, which is why this one exists.
    #
    # BY ELIMINATION, NOT BY MATCHING THE ROW, and that is the whole subtlety.
    # Testing the cursor line against `_FREE_TEXT_ROW` reads as the obvious rule and
    # FAILS EXACTLY WHEN THE HAZARD IS LIVE: that pattern keys on the literal
    # `Type something` placeholder, which the TUI replaces the moment the field has
    # content. A marker built that way goes quiet as soon as anything is typed,
    # which is the state a caller is most likely to be in. The other two positions
    # are identifiable by their own text in every state, so the row falls out of
    # ruling them out.
    if out.get("commitByButton"):
        cursor = _cursor_line(lines, footer)
        if cursor is not None:
            plain = _plain(cursor)
            on_button = _COMMIT_ROW.match(plain) is not None
            on_chat = _CHAT_ROW in plain
            if not on_button and not on_chat:
                out["digitBecomesText"] = True

    return out or None


def _cursor_line(lines: list[str], footer: int | None) -> str | None:
    """The picker's highlighted row, or None when it cannot be located.

    Bounded to the picker's own window for the reason every scan here is: the
    transcript above can contain a `❯`, and this module's whole hazard is reading
    something the session itself wrote. The bottom-most match wins, because the
    picker is the bottom-most thing drawn.

    None means "not established", and every caller must read it that way — the
    safe response to an unlocatable cursor is to claim nothing about where it is.
    """
    if footer is None:
        return None
    start = max(0, footer - _PICKER_LOOKBACK)
    for index in range(footer - 1, start - 1, -1):
        if _CURSOR in _plain(lines[index]):
            return lines[index]
    return None


def _active_run(line: str) -> str:
    """The text drawn inside the active chip, or "" when there is none.

    Separate because the Submit tab carries `✔` rather than a checkbox glyph, so
    `_tabs` does not see it and cannot report it active.
    """
    after = line.split(f"\x1b[{_ACTIVE_BG}m", 1)
    if len(after) != 2:
        return ""
    return _SGR.split(after[1])[0]


def version_tuple(version: str) -> tuple[int, ...] | None:
    """A dotted version as integers, or None when it is not one.

    None rather than a zero tuple, because the two answers lead to different
    behaviour and collapsing them is how a non-Claude pane would sail past the
    floor: `pane_current_command` reports `zsh` for a pane that is not running
    Claude Code at all, and `("zsh",) -> (0,)` would compare as ancient rather
    than as unreadable.

    Trailing non-numeric segments are refused rather than truncated. A build
    numbered `2.1.228-rc1` is one nobody has driven, and reading it as 2.1.228
    would claim verification this module does not have.
    """
    parts = version.split(".")
    if not parts or any(not p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


# What a reported pane version may look like on its way into the state object.
# Deliberately wider than a version, since the point of reporting it is to say
# what the pane claimed when that was NOT a version, and deliberately bounded.
_REPORTABLE_VERSION = re.compile(r"\A[A-Za-z0-9._+-]{1,32}\Z")
_UNREPORTABLE = "<unreadable>"


def _reportable(version: str) -> str:
    """*version* if it is safe to put on the wire, else a fixed placeholder.

    `pane_current_command` reports whatever the pane's foreground process calls
    itself, which is Claude Code's version only because Claude Code names itself
    that. It reads `zsh` during a startup race, and a process that chooses its own
    argv0 chooses this string. `renderedDark` exists to be DRAWN — Deck renders
    its panes as HTML — so the field is bounded here rather than trusted to be
    escaped correctly at every future consumer. Raised in security review of #387.

    Refused rather than truncated, because a clipped string is a claim about what
    the pane reported and a placeholder is not. The reason field beside it already
    says the version could not be parsed, so nothing diagnostic is lost.
    """
    return version if _REPORTABLE_VERSION.match(version) else _UNREPORTABLE


def _skeleton_reason(capture: str) -> str | None:
    """Why this capture cannot be trusted, or None when it can.

    THIS IS THE HALF OF THE GATE THAT REPLACED EXACT VERSION EQUALITY, and it is
    asked of the screen rather than of a version string. The version said "a build
    nobody checked"; this asks the question that was standing behind it — has the
    layout moved? — of the capture actually in hand.

    A picker has to satisfy one of the two views, and each is anchored the way
    `parse` anchors its own scans:

      * A QUESTION VIEW is anchored on the footer. Within the picker's own window
        above it there must be at least one dim numbered row and the cursor glyph.
      * THE REVIEW VIEW DRAWS NO FOOTER, which is observed rather than reasoned —
        its last line is `2. Cancel` and `Esc to cancel` appears nowhere in a
        capture of it. It is anchored instead on the tab bar's active chip, with
        the same numbered row and cursor required below it.

    Plus one invariant that is not about finding the picker but about trusting a
    field: **a question drawing checkbox rows must also yield a commit row.** That
    is the only absent field that is dangerous rather than merely limiting, and
    the checkbox is what distinguishes a multiselect question — where the row must
    exist — from a single-select one, where it never does. Without this the
    self-test would happily pass a build whose commit row had been reworded, and
    `submit_choices` would send a digit into the next question.

    **NEITHER ANCHOR PRESENT IS NOT A MOVED LAYOUT**, and separating the two is
    what keeps this from crying wolf. A gate that resolved between the hook read
    and the capture leaves an ordinary screen behind, with no footer and no tab
    bar, and calling that "the markers moved" would report a drift that never
    happened every time a question was answered quickly. It is reported as no
    picker instead, which is the truth and is not darkness.

    Styling is load-bearing throughout, the same way it is for every scan in
    `parse`: transcript text the model itself wrote can imitate any of these
    shapes, and only the TUI draws them dim.
    """
    lines = capture.splitlines()

    footer = None
    for index in range(len(lines) - 1, -1, -1):
        if _FOOTER in _plain(lines[index]):
            footer = index
            break

    # The tab bar, found exactly as `parse` finds it — the bottom-most line
    # carrying the active chip, `✔` and `Submit`.
    bar = None
    for index in range(len(lines) - 1, -1, -1):
        plain = _plain(lines[index])
        if _draws_with(lines[index], _ACTIVE_BG) \
                and _SUBMIT_TAB in plain and "✔" in plain:
            bar = index
            break

    if footer is not None:
        window = range(max(0, footer - _PICKER_LOOKBACK), footer)
    elif bar is not None and _submit_tab_active(lines[bar]):
        # The review view, the one view that draws no footer.
        window = range(bar + 1, min(len(lines), bar + 1 + _PICKER_LOOKBACK))
    elif bar is not None:
        # A QUESTION TAB IS LIVE AND THERE IS NO FOOTER, which is drift rather
        # than a view. Falling through to the review branch here was a real bug
        # in the first draft of this function: a reworded footer left the tab bar
        # standing, the scan read the question view as a review view and passed
        # it, and `parse` then returned `questionIndex` alone — the partial parse
        # this whole gate exists to refuse. Whether the Submit tab is the ACTIVE
        # one is what tells the two views apart; the bar's mere presence does not.
        return _REASON_SKELETON
    else:
        return _REASON_NO_PICKER

    numbered = [i for i in window
                if _draws_with(lines[i], _DIM)
                and _NUMBERED_ROW.match(_plain(lines[i]))]
    if not numbered:
        return _REASON_SKELETON
    if not any(_CURSOR in _plain(lines[i]) for i in window):
        return _REASON_SKELETON

    # The checkbox invariant. Bounded to the same window and to the same dim
    # styling as every other scan here, so a transcript line the model wrote
    # cannot manufacture a requirement the real picker then fails.
    if any(_CHECKBOX_ROW.match(_plain(lines[i])) for i in numbered):
        if not any(_draws_with(lines[i], _BOLD)
                   and _COMMIT_ROW.match(_plain(lines[i]))
                   for i in window):
            return _REASON_SKELETON

    return None


def _submit_tab_active(line: str) -> bool:
    """Whether the review tab is the live one on this tab bar.

    The same two readings `parse` uses, for the same reason: `_tabs` cannot see
    the Submit tab, which carries `✔` rather than a checkbox, so an active Submit
    has to be read off the chip's own run as well.
    """
    tabs = _tabs(line)
    return any(active and label == _SUBMIT_TAB for label, active in tabs) \
        or "✔" in _active_run(line)


def read(session_name: str) -> dict | None:
    """Capture the pane and report what the picker shows. None when unavailable.

    Never raises. A capture that fails, a version below the floor, a layout whose
    bones have moved, a screen that matched nothing — all of them are the same
    answer, because the caller's fallback is identical in every case: behave as
    though this module did not exist. A blocked session with a gate Deck can
    already render is not worth breaking over a scrape.

    `read_detail` is the same read with the refusal's reason attached, for a
    caller that reports a dark reader rather than only degrading around one.
    """
    return read_detail(session_name)[0]


def read_detail(session_name: str) -> tuple[dict | None, dict | None]:
    """`(rendered, dark)` — the read, and why it refused when it did.

    At most one of the two is non-None, and both None is the ordinary answer for
    a screen with no picker on it. `dark` carries the reason, the version the pane
    reported and the floor it was compared against, so a consumer can say which of
    "too old", "not Claude Code" and "the layout moved" happened rather than
    showing the same silence for all three.

    THIS EXISTS BECAUSE SILENT CORRECT DEGRADATION IS HOW THIS MODULE SAT DARK FOR
    TEN DAYS. The refusal was working exactly as designed and nothing announced
    it, so a shipped feature was missing from the pane and the only way to notice
    was to go looking. A reason on the wire is what turns that into something a
    consumer can draw.
    """
    try:
        pane_id, _ = TT.claude_pane(session_name)
    except Exception:
        return None, {"reason": _REASON_NO_PANE, "floor": MARKERS_VERIFIED_FLOOR}

    version = TT.run(
        ["tmux", "display-message", "-p", "-t", pane_id, "#{pane_current_command}"]
    )
    version = (version or "").strip()
    # THE RE-CHECK, PERFORMED RATHER THAN PROMISED. `pane_current_command` reports
    # the version string, which is the same quirk that forces pane resolution by
    # pid — so the guard needs no new source.
    dark = {"paneVersion": _reportable(version), "floor": MARKERS_VERIFIED_FLOOR}
    parsed = version_tuple(version)
    if parsed is None:
        return None, {**dark, "reason": _REASON_UNPARSEABLE}
    if parsed < version_tuple(MARKERS_VERIFIED_FLOOR):
        return None, {**dark, "reason": _REASON_BELOW_FLOOR}

    capture = TT.run(["tmux", "capture-pane", "-e", "-p", "-t", pane_id])
    if not capture:
        return None, {**dark, "reason": _REASON_NO_CAPTURE}
    reason = _skeleton_reason(capture)
    if reason == _REASON_SKELETON:
        return None, {**dark, "reason": reason}
    result = parse(capture)
    if result is not None:
        result["claudeVersion"] = version
    return result, None
