"""Reading a question picker off the pane — the markers, pinned to real captures.

EVERY FIXTURE HERE IS A VERBATIM LINE FROM A REAL PICKER, captured with
`tmux capture-pane -e -p` against Claude Code 2.1.220 on 2026-07-30 and pasted in
unedited. That is deliberate and it is the only thing that makes these tests worth
having: the module they cover exists to read Anthropic's rendering, so a fixture
an author invented would pin the author's belief about that rendering rather than
the rendering. Two of these markers were wrong when written from one example and
right only after being run against a second — see `test_a_multiselect_free_text_row`.

The captures are recorded in the spec `deck/specs/reading-the-rendered-picker` §7.

WHAT THESE CANNOT CATCH. A Claude Code release that changes the layout makes every
fixture stale, and a stale fixture keeps passing. That is what
`MARKERS_VERIFIED_FLOOR` and the skeleton self-test are for: the module refuses to read a version it has not
been checked against, so the failure mode of a stale fixture is Deck losing the
feature rather than Deck acting on a mis-parse.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parent.parent / "lib" / "agent-platform"


@pytest.fixture
def PR():
    spec = importlib.util.spec_from_file_location("picker_render", LIB / "picker_render.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- verbatim captures --------------------------------------------------------

# A single question, three options. No tab bar at all: a lone question has none,
# which is itself how the two forms are told apart.
SINGLE_FREE_TEXT = '  \x1b[38;5;241m4.\x1b[39m \x1b[38;5;241mType\x1b[39m \x1b[38;5;241msomething.\x1b[39m'

# Three questions, the first live. Only the SGR background says which — under
# `-p` these tabs are byte-identical apart from their checkbox glyphs.
TABS_Q0 = '← \x1b[38;5;231m\x1b[48;5;105m ☐ Colour \x1b[39m\x1b[49m ☐ Size  ☐ Speed  ✔ Submit  →'

# The same gate after answering the first: `☒ Colour`, and the chip has moved.
TABS_Q1 = '\x1b[39m←  ☒ Colour \x1b[38;5;231m\x1b[48;5;105m ☐ Size \x1b[39m\x1b[49m ☐ Speed  ✔ Submit  →'

# The free-text row of a MULTISELECT question: an option checkbox, and no
# trailing period. This is the rendering that broke the first pattern.
MULTISELECT_FREE_TEXT = '  \x1b[38;5;241m3.\x1b[39m [ ] \x1b[38;5;241mType something\x1b[39m'

# The bare button a multiselect question draws under its options, both forms,
# verbatim. `Next` came from the FIRST question of a two-question gate and
# `Submit` from the LAST, which is the entire distinction: committing off the last
# question lands on the review tab, committing off an earlier one lands on the next
# question, and `submit_choices` sends a trailing digit that is right in the first
# case and answers the wrong question in the second.
COMMIT_NEXT = '     \x1b[1mNext\x1b[0m'
COMMIT_SUBMIT = '     \x1b[1mSubmit\x1b[0m'

# THE SAME BUTTON WITH THE HIGHLIGHT ON IT. Verbatim from a live 2.1.220 capture
# on 2026-07-31, and the reason this fixture exists at all: the TUI prepends `❯`,
# the bold survives, and the first `_COMMIT_ROW` anchored on `^\s+` and therefore
# stopped matching here. `submit_choices` then read no commit word, failed its
# `== "next"` test, and sent its trailing digit into the question it had just
# advanced to — a phantom answer, driven and confirmed.
COMMIT_NEXT_HIGHLIGHTED = '\x1b[38;5;105m❯\x1b[39m    \x1b[1mNext\x1b[0m'

# The free-text row with the highlight on it, empty. Verbatim, same capture. Note
# `\x1b[2m` on the placeholder rather than the `_DIM` colour the unhighlighted row
# carries — which is why `digitBecomesText` does not lean on styling.
FREE_TEXT_HIGHLIGHTED = (
    '\x1b[38;5;105m❯\x1b[39m \x1b[38;5;241m4.\x1b[39m [ ] \x1b[2mType something\x1b[0m')

# The same row after a digit landed in it AS TEXT — the hazard itself, verbatim.
# `Type something` is gone, so anything keying on that phrase stops matching here.
FREE_TEXT_HIGHLIGHTED_WITH_CONTENT = (
    '\x1b[38;5;105m❯\x1b[39m \x1b[38;5;241m4.\x1b[39m \x1b[38;5;65m[✔] \x1b[39m1')

# `Chat about this`, unhighlighted and highlighted. The unhighlighted form is
# verbatim; the highlighted one is CONSTRUCTED by prepending the cursor run the
# other two captures carry, and it is labelled so rather than claiming provenance
# it does not have. Faithful for what the reader does here, which is a plain-text
# test after `_plain` has stripped every escape.
CHAT_ROW = '\x1b[39m  5. Chat about this'
CHAT_ROW_HIGHLIGHTED = '\x1b[38;5;105m❯\x1b[39m 5. Chat about this'

# A REAL model-supplied option row, verbatim. Distinct from `MULTISELECT_OPTION`
# above, which despite its name is a `Type something` line and therefore supplies a
# `freeTextRow` of its own — fine as filler for the commit-row tests, and fatal to
# any test asserting that a free-text row went unmatched.
OPTION_ROW = '  \x1b[38;5;241m2.\x1b[39m [ ] Anchovies'

# The multiselect option row that precedes the button, kept so a fixture can be a
# plausible picker rather than a button floating above a footer.
MULTISELECT_OPTION = '  \x1b[38;5;241m3. \x1b[39m[ ] \x1b[38;5;241mType something\x1b[39m'

# The footer every picker form ends on. Load-bearing, not decoration: it is what
# tells a picker row from a transcript line that looks like one.
FOOTER_SINGLE = '\x1b[38;5;241mEnter\x1b[39m \x1b[38;5;241mto\x1b[39m \x1b[38;5;241mselect\x1b[39m \x1b[38;5;241m·\x1b[39m \x1b[38;5;241m↑/↓\x1b[39m \x1b[38;5;241mto\x1b[39m \x1b[38;5;241mnavigate\x1b[39m \x1b[38;5;241m·\x1b[39m \x1b[38;5;241mEsc to cancel\x1b[39m'
FOOTER_MULTI = '\x1b[38;5;241mEnter\x1b[39m \x1b[38;5;241mto\x1b[39m \x1b[38;5;241mselect\x1b[39m \x1b[38;5;241m·\x1b[39m \x1b[38;5;241mTab/Arrow\x1b[39m \x1b[38;5;241mkeys\x1b[39m \x1b[38;5;241mto\x1b[39m \x1b[38;5;241mnavigate\x1b[39m \x1b[38;5;241m·\x1b[39m \x1b[38;5;241mEsc to cancel\x1b[39m'


# The same footer with the edit hint the TUI adds while the highlight sits in the
# free-text group — on the row itself, or on the commit button below it.
#
# CONSTRUCTED TO MATCH THE OBSERVED RENDERING, not pasted from a capture, and the
# distinction is worth stating because an earlier version of this comment claimed
# capture provenance it does not have. What WAS observed on a live 2.1.220 picker
# on 2026-07-30, by walking the highlight and reading the footer at each position,
# is which positions carry the hint; the SGR runs here are this fixture's own,
# built by substituting into the footer fixture beside it.
#
# The substitution is faithful for what the reader does, which is a plain-text
# substring test after `_plain` has stripped every escape — so any encoding of the
# hint in a footer that still carries `Esc to cancel` exercises the same path. It
# would NOT be faithful to a test that cared about the styling, and there is no
# such test here deliberately: the hint is read off the footer line the scan has
# already anchored, so it needs no style guard of its own.
FOOTER_MULTI_EDITING = FOOTER_MULTI.replace(
    '\x1b[38;5;241mEsc to cancel\x1b[39m',
    '\x1b[38;5;241mctrl+g\x1b[39m \x1b[38;5;241mto\x1b[39m \x1b[38;5;241medit\x1b[39m '
    '\x1b[38;5;241min\x1b[39m \x1b[38;5;241mVim\x1b[39m \x1b[38;5;241m·\x1b[39m '
    '\x1b[38;5;241mEsc to cancel\x1b[39m')


def editing_picker(*rows: str) -> str:
    """A picker whose highlight is in the free-text group, so `Right` is inert."""
    return "\n".join([*rows, FOOTER_MULTI_EDITING])


def picker(*rows: str) -> str:
    """A picker as it appears in a capture: rows, then the footer that ends one.

    The fixtures were single lines until the free-text scan gained its anchors,
    at which point they stopped representing a picker at all — a real one always
    has a footer under it. Cheaper to make the fixtures honest than to weaken the
    guard they were failing.
    """
    return "\n".join([*rows, FOOTER_MULTI])

# Every question answered, sitting on the review tab. The Submit tab carries `✔`
# rather than a checkbox, so it is not a "tab" to the checkbox scanner at all.
TABS_SUBMIT = '\x1b[39m←  ☒ Colour  ☒ Size \x1b[38;5;231m\x1b[48;5;105m ✔ Submit \x1b[38;5;241m\x1b[49m →\x1b[39m'

# The review view entire, verbatim, from the tab bar to the last row it draws.
#
# ITS POINT IS THE LINE THAT IS NOT HERE. There is no footer: `Esc to cancel`
# appears nowhere on this view, which is why the tab bar scan cannot be anchored
# on the footer the way the free-text scan is. Captured against 2.1.220 on
# 2026-07-30 by answering a three-question gate through to Submit.
REVIEW_VIEW = "\n".join([
    '\x1b[39m←  ☒ Colour  ☒ Size  ☒ Speed \x1b[38;5;231m\x1b[48;5;105m ✔ Submit \x1b[38;5;241m\x1b[49m →\x1b[39m',
    '',
    '\x1b[1m\x1b[38;5;16mReview your answers\x1b[0m',
    '',
    ' ● Colour?',
    '   \x1b[38;5;65m→ Red\x1b[39m',
    ' ● Size?',
    '   \x1b[38;5;65m→ Small\x1b[39m',
    ' ● Speed?',
    '   \x1b[38;5;65m→ Fast\x1b[39m',
    '',
    '\x1b[38;5;241mReady\x1b[39m \x1b[38;5;241mto\x1b[39m \x1b[38;5;241msubmit\x1b[39m \x1b[38;5;241myour answers?\x1b[39m',
    '',
    '\x1b[38;5;105m❯\x1b[39m \x1b[38;5;241m1.\x1b[39m \x1b[38;5;105mSubmit\x1b[39m \x1b[38;5;105manswers\x1b[39m',
    '  \x1b[38;5;241m2.\x1b[39m Cancel',
])


# --- the cursor ---------------------------------------------------------------

def test_the_live_question_is_read_from_the_highlight(PR):
    """The field Deck's cursor stops being a guess because of."""
    assert PR.parse(TABS_Q0) == {"onSubmitTab": False, "questionIndex": 0}
    assert PR.parse(TABS_Q1) == {"onSubmitTab": False, "questionIndex": 1}


def test_the_highlight_and_not_the_checkbox_decides(PR):
    """`☒` means answered, not live, and the two come apart.

    In `TABS_Q1` the answered tab is `Colour` and the live one is `Size`. A reader
    that took `☒` as the cursor would report 0 where the truth is 1 — and would be
    wrong in exactly the case that matters, a user who navigated back to a tab
    they had already answered.
    """
    assert PR.parse(TABS_Q1)["questionIndex"] == 1
    assert "☒ Colour" in TABS_Q1, "the fixture no longer exercises the distinction"


def test_the_review_tab_is_recognised(PR):
    """`onSubmitTab` is what stops a multi-question gate hanging.

    A gate does NOT resolve when its last question is answered; the TUI advances
    to this tab and waits. Deck without this field draws the last question's
    options over a review screen, where `2` is Cancel.
    """
    parsed = PR.parse(TABS_SUBMIT)
    assert parsed["onSubmitTab"] is True
    assert "questionIndex" not in parsed, "no question is live on the review tab"


def test_a_plain_capture_could_not_have_told_us(PR):
    """The demonstration the whole approach rests on, as an assertion.

    Strip the styling and the two tab bars become the same string apart from a
    checkbox glyph. `-e` is required, not preferred.
    """
    assert PR._plain(TABS_Q0).replace("☐", "☒") == PR._plain(TABS_Q1).replace("☐", "☒")
    assert PR.parse(PR._plain(TABS_Q1)) is None or \
        "questionIndex" not in (PR.parse(PR._plain(TABS_Q1)) or {}), \
        "a plain capture yielded a cursor it cannot possibly know"


# --- the appended row ---------------------------------------------------------

def test_a_single_select_free_text_row(PR):
    assert PR.parse("\n".join([SINGLE_FREE_TEXT, FOOTER_SINGLE])) == {"freeTextRow": 4}


def test_a_multiselect_free_text_row(PR):
    """THE MARKER THAT WAS WRONG WHEN WRITTEN FROM ONE EXAMPLE.

    A single-select question draws `4. Type something.`; a multiselect draws
    `3. [ ] Type something` — checkbox, no period. The first pattern silently
    matched nothing here, which would have removed free-text answers from exactly
    the questions where they are most wanted.
    """
    assert PR.parse(picker(MULTISELECT_FREE_TEXT)) == {"freeTextRow": 3}


def test_the_row_number_is_the_digit_deck_will_send(PR):
    """N+1 for the model's N options, confirmed at two option counts.

    3 options -> row 4 (single-select fixture); 2 options -> row 3 (multiselect
    fixture). Deck sends that digit, so an off-by-one answers a real option.
    """
    assert PR.parse(picker(SINGLE_FREE_TEXT))["freeTextRow"] == 4
    assert PR.parse(picker(MULTISELECT_FREE_TEXT))["freeTextRow"] == 3


# --- degradation --------------------------------------------------------------

def test_nothing_recognised_is_none_rather_than_a_guess(PR):
    for text in ("", "an ordinary line of transcript", "❯ 1. Red\n  2. Blue"):
        assert PR.parse(text) is None


def test_a_full_capture_yields_every_field(PR):
    """The fields are read independently, so a capture carrying both gives both."""
    assert PR.parse(picker(TABS_Q1, "Size?", MULTISELECT_FREE_TEXT)) == {
        "onSubmitTab": False, "questionIndex": 1, "freeTextRow": 3,
    }


def test_a_tab_bar_without_submit_is_not_a_tab_bar(PR):
    """Guards against matching a transcript line that happens to hold a checkbox."""
    assert PR.parse("  ☐ Colour  ☐ Size") is None


# --- the gate: a version floor, and a self-test on the screen itself -----------
#
# Exact version equality was the gate until 2026-08-10 and went stale twice in
# eleven days, so both readers spent most of their life dark while degrading
# perfectly correctly. These tests pin the replacement from both sides: a build
# above the floor is READ, and a layout that moved is still REFUSED.

def a_live_question(version_capture: str | None = None) -> str:
    """A multiselect question as a capture holds one, end to end.

    Composed rather than pasted, from the same verbatim rows the parse tests use.
    It has to be a whole picker now: `read` asks whether the skeleton is intact
    before it parses, and a single fixture line is not a picker.
    """
    return picker(TABS_Q1, OPTION_ROW, MULTISELECT_FREE_TEXT, COMMIT_SUBMIT,
                  CHAT_ROW_HIGHLIGHTED)


def reading_at(version: str, capture: str):
    """A `TT.run` double that answers the version probe and the capture."""
    def fake_run(argv, stdin=None):
        if "display-message" in argv:
            return version
        return capture
    return fake_run


def test_read_refuses_a_build_below_the_floor(PR, monkeypatch):
    """Markers are only ever verified forward, so older is unchecked."""
    monkeypatch.setattr(PR.TT, "claude_pane", lambda name: ("%9", 1))
    monkeypatch.setattr(PR.TT, "run", reading_at("2.1.219", a_live_question()))
    rendered, dark = PR.read_detail("s")
    assert rendered is None
    assert dark["reason"] == "version-below-floor"
    assert dark["paneVersion"] == "2.1.219"


def test_read_refuses_a_version_it_cannot_parse(PR, monkeypatch):
    """`pane_current_command` says `zsh` when the pane is not running Claude.

    Refused rather than read as ancient — the two are different facts, and
    collapsing them would let a non-Claude pane compare as below the floor by
    accident rather than be recognised for what it is.
    """
    monkeypatch.setattr(PR.TT, "claude_pane", lambda name: ("%9", 1))
    monkeypatch.setattr(PR.TT, "run", reading_at("zsh", a_live_question()))
    rendered, dark = PR.read_detail("s")
    assert rendered is None
    assert dark["reason"] == "version-unparseable"


def test_read_accepts_a_build_newer_than_any_driven_by_hand(PR, monkeypatch):
    """THE WHOLE POINT OF THE CHANGE. A patch bump no longer blinds the reader.

    Under exact equality this returned None, which is how `2.1.220` left Deck
    without its free-text card and its multiselect commit button for ten days.
    """
    monkeypatch.setattr(PR.TT, "claude_pane", lambda name: ("%9", 1))
    monkeypatch.setattr(PR.TT, "run", reading_at("2.9.400", a_live_question()))
    result = PR.read("s")
    assert result["questionIndex"] == 1
    assert result["multiSelectCommit"] == "submit"
    assert result["claudeVersion"] == "2.9.400"


def test_read_refuses_a_layout_whose_footer_moved(PR, monkeypatch):
    """The refusal the version check was standing in for, asked of the screen."""
    moved = a_live_question().replace("Esc to cancel", "Esc to dismiss")
    monkeypatch.setattr(PR.TT, "claude_pane", lambda name: ("%9", 1))
    monkeypatch.setattr(PR.TT, "run", reading_at(PR.MARKERS_LAST_DRIVEN, moved))
    rendered, dark = PR.read_detail("s")
    assert rendered is None
    assert dark["reason"] == "markers-moved"


def test_read_refuses_a_multiselect_whose_commit_row_is_gone(PR, monkeypatch):
    """The one absent field that is DANGEROUS rather than merely limiting.

    `submit_choices` reads a missing `multiSelectCommit` as "not `next`" and
    sends its trailing digit, which on a `Next` screen answers the question it
    has just advanced to — a phantom answer nobody chose, and a real shipped
    defect (spec §7.12). A checkbox row is what says a commit row must exist, so
    checkboxes without one are refused rather than parsed into that hazard.
    """
    without_commit = picker(TABS_Q1, OPTION_ROW, MULTISELECT_FREE_TEXT,
                            CHAT_ROW_HIGHLIGHTED)
    monkeypatch.setattr(PR.TT, "claude_pane", lambda name: ("%9", 1))
    monkeypatch.setattr(PR.TT, "run",
                        reading_at(PR.MARKERS_LAST_DRIVEN, without_commit))
    rendered, dark = PR.read_detail("s")
    assert rendered is None
    assert dark["reason"] == "markers-moved"


def test_a_single_select_needs_no_commit_row(PR, monkeypatch):
    """The invariant keys on the checkbox, so it must not fire without one.

    A single-select question draws no commit row at all, and demanding one would
    refuse every such picker — turning a guard against a phantom answer into a
    reader that is dark on half the gates it exists for.
    """
    single = picker(TABS_Q1, '  \x1b[38;5;241m2.\x1b[39m Anchovies',
                    SINGLE_FREE_TEXT, CHAT_ROW_HIGHLIGHTED)
    monkeypatch.setattr(PR.TT, "claude_pane", lambda name: ("%9", 1))
    monkeypatch.setattr(PR.TT, "run", reading_at(PR.MARKERS_LAST_DRIVEN, single))
    assert PR.read("s")["freeTextRow"] == 4


def test_the_review_view_passes_the_self_test_without_a_footer(PR, monkeypatch):
    """It draws none, so the self-test anchors on the tab bar there instead.

    Anchoring the skeleton on the footer alone would refuse the one view whose
    absence hangs a gate: a pane that cannot report `onSubmitTab` leaves the
    session waiting on a Submit nobody can take.
    """
    monkeypatch.setattr(PR.TT, "claude_pane", lambda name: ("%9", 1))
    monkeypatch.setattr(PR.TT, "run",
                        reading_at(PR.MARKERS_LAST_DRIVEN, REVIEW_VIEW))
    assert PR.read("s")["onSubmitTab"] is True


def test_a_screen_with_no_picker_is_not_darkness(PR, monkeypatch):
    """A gate answered between the hook read and the capture is not drift.

    Reporting it as `markers-moved` would cry wolf every time a question was
    answered quickly, which is the fastest way to make an observability signal
    worth ignoring.
    """
    monkeypatch.setattr(PR.TT, "claude_pane", lambda name: ("%9", 1))
    monkeypatch.setattr(PR.TT, "run",
                        reading_at(PR.MARKERS_LAST_DRIVEN, "an ordinary screen"))
    rendered, dark = PR.read_detail("s")
    assert rendered is None
    assert dark is None


def test_read_stamps_the_version_it_was_read_at(PR, monkeypatch):
    monkeypatch.setattr(PR.TT, "claude_pane", lambda name: ("%9", 1))
    monkeypatch.setattr(PR.TT, "run",
                        reading_at(PR.MARKERS_LAST_DRIVEN, a_live_question()))
    result = PR.read("s")
    assert result["claudeVersion"] == PR.MARKERS_LAST_DRIVEN
    assert result["questionIndex"] == 1


def test_version_tuple_refuses_a_prerelease_rather_than_truncating_it(PR):
    """`2.1.228-rc1` is a build nobody has driven, and reading it as 2.1.228
    would claim verification this module does not have."""
    assert PR.version_tuple("2.1.228") == (2, 1, 228)
    assert PR.version_tuple("2.1.228-rc1") is None
    assert PR.version_tuple("zsh") is None
    assert PR.version_tuple("") is None


def test_read_never_raises(PR, monkeypatch):
    """A scrape is not worth breaking a gate Deck can already render."""
    def boom(*a, **k):
        raise RuntimeError("no such pane")

    monkeypatch.setattr(PR.TT, "claude_pane", boom)
    assert PR.read("s") is None


# --- pane content is partly written by the session -----------------------------
#
# Raised in security review of #303, and it is the sharp edge of reading a pane at
# all. A hook payload comes from Claude Code; a pane holds whatever the model has
# printed. Deck SENDS `freeTextRow` as a digit, so a wrong one answers a real
# option instead of opening the text field.


def test_transcript_text_above_a_picker_cannot_supply_the_row(PR):
    """THE FINDING. Model output reading `2. Type something.` used to win.

    The first version scanned the whole capture with styling stripped, so this
    exact shape — a numbered list item in prior output — was returned as the row
    to click. Deck would have sent `2`, answering option 2.
    """
    hostile = "\n".join([
        "  2. Type something.",              # unstyled model output, still visible
        "",
        SINGLE_FREE_TEXT,                     # the picker's real row, dim-styled
        FOOTER_SINGLE,
    ])
    assert PR.parse(hostile) == {"freeTextRow": 4}


def test_a_matching_line_below_the_footer_is_ignored(PR):
    """The scan runs above the footer, because the picker is drawn above it."""
    assert PR.parse("\n".join([FOOTER_SINGLE, "  9. Type something."])) is None


def test_an_unstyled_row_is_not_a_picker_row(PR):
    """Styling is load-bearing here for the reason it is for the tab bar."""
    assert PR.parse("\n".join(["  4. Type something.", FOOTER_SINGLE])) is None


def test_a_match_far_above_the_footer_is_out_of_reach(PR):
    """Bounded lookback, so the scan cannot walk up into the transcript."""
    far = "\n".join([SINGLE_FREE_TEXT] + ["filler"] * 60 + [FOOTER_SINGLE])
    assert PR.parse(far) is None


def test_the_row_nearest_the_footer_wins(PR):
    """Two styled candidates: the picker is the bottom-most thing drawn."""
    stale = SINGLE_FREE_TEXT.replace("4.", "7.")
    assert PR.parse("\n".join([stale, SINGLE_FREE_TEXT, FOOTER_SINGLE])) == {
        "freeTextRow": 4
    }


# --- the multiselect commit row ------------------------------------------------
#
# The field that lets `submit_choices` check its own precondition instead of
# documenting it. The old one said single-question gates only; the real constraint
# was the last question, which is a different set and one the screen can report.


def test_the_commit_row_says_committing_advances(PR):
    """`Next` means the commit lands on another question, so send no digit."""
    assert PR.parse(picker(MULTISELECT_OPTION, COMMIT_NEXT)) == {
        "freeTextRow": 3, "multiSelectCommit": "next"
    }


def test_the_commit_row_says_committing_submits(PR):
    """`Submit` means the commit lands on the review tab, where `1` is correct."""
    assert PR.parse(picker(MULTISELECT_OPTION, COMMIT_SUBMIT)) == {
        "freeTextRow": 3, "multiSelectCommit": "submit"
    }


def test_a_single_select_question_has_no_commit_row(PR):
    """Absent means the question does not offer one, which single-select does not.

    The button belongs to a multiselect; a single-select digit selects and
    confirms, so there is nothing to commit.
    """
    assert "multiSelectCommit" not in PR.parse(picker(SINGLE_FREE_TEXT))


def test_anthropics_word_does_not_reach_the_caller(PR):
    """Normalised at the boundary that observes it.

    `Next` and `Submit` are Anthropic's display strings and can change in any
    release. A caller comparing against them would carry a marker it has no way to
    re-verify, so the reader maps them to names this side owns.
    """
    for fixture in (COMMIT_NEXT, COMMIT_SUBMIT):
        got = PR.parse(picker(MULTISELECT_OPTION, fixture))["multiSelectCommit"]
        assert got in ("next", "submit")
        assert got == got.lower(), "the raw label leaked through"


def test_the_commit_goes_through_the_button_while_the_hint_is_up(PR):
    """`Right` is inert while the highlight sits in the free-text group.

    Observed 2026-07-30 against 2.1.220: with the highlight on the free-text row
    `Right` moves the caret inside the text, and with it on the commit button
    `Right` does nothing at all. `submit_choices` sent `Right` and returned having
    changed nothing, so Deck's commit button was a silent no-op — which is how
    this was found, on a live gate rather than here.
    """
    parsed = PR.parse(editing_picker(MULTISELECT_OPTION, COMMIT_NEXT))
    assert parsed["commitByButton"] is True
    assert parsed["multiSelectCommit"] == "next", "the button's word is still read"


def test_the_commit_row_is_still_read_with_the_highlight_on_it(PR):
    """THE PHANTOM ANSWER THIS CLOSES, and the glyph is the whole of it.

    With the highlight on the button the TUI prepends `❯`, so the plain line is
    `❯    Next` and the old `^\\s+` anchor did not match. The bold is still there —
    the guard was never what failed.

    The cost was not a missing field. `submit_choices` reads it to decide whether
    to send its trailing digit; absent is not `next`, so it sent the digit into the
    question it had just advanced to. Driven on a live 2.1.220 gate 2026-07-31 with
    one option ticked, the FOLLOWING question came back answered with its first
    option, which nobody had chosen. Spec §7.12.
    """
    parsed = PR.parse(editing_picker(MULTISELECT_OPTION, COMMIT_NEXT_HIGHLIGHTED))
    assert parsed["multiSelectCommit"] == "next", "the digit would go to the next question"


def test_the_highlighted_and_plain_buttons_read_the_same(PR):
    """The glyph is a position marker, not a different button. Both renderings are
    the same commit and must produce the same word."""
    for row, word in ((COMMIT_NEXT, "next"), (COMMIT_NEXT_HIGHLIGHTED, "next")):
        assert PR.parse(editing_picker(MULTISELECT_OPTION, row))["multiSelectCommit"] == word


def test_a_digit_would_be_typed_with_the_highlight_on_the_free_text_row(PR):
    """THE HAZARD `answer_choice` NOW REFUSES ON.

    The field takes focus as soon as the highlight arrives — no typing needed — and
    a digit then lands in it as a character. Sending `1` to tick the first option
    produced `4. [✔] 1` on a live 2.1.220 gate, so a caller asking for option 1
    would record a free-text answer nobody wrote.
    """
    parsed = PR.parse(editing_picker(
        OPTION_ROW, FREE_TEXT_HIGHLIGHTED, COMMIT_NEXT, CHAT_ROW))
    assert parsed["digitBecomesText"] is True


def test_the_hazard_is_still_reported_once_the_field_has_content(PR):
    """THE SUBTLETY THE OBVIOUS RULE GETS WRONG, and it fails exactly when the
    hazard is live.

    Testing the cursor line against `_FREE_TEXT_ROW` reads as the natural way to
    identify this position. That pattern keys on the literal `Type something`
    placeholder, which the TUI replaces the moment the field has content — so a
    marker built that way goes quiet as soon as anything is typed, which is the
    state a caller is most likely to be in. Hence identification by ELIMINATING the
    other two positions, both of which keep their own text in every state.
    """
    parsed = PR.parse(editing_picker(
        OPTION_ROW, FREE_TEXT_HIGHLIGHTED_WITH_CONTENT, COMMIT_NEXT, CHAT_ROW))
    assert "freeTextRow" not in parsed, "the fixture no longer makes the point"
    assert parsed["digitBecomesText"] is True


def test_the_highlight_on_the_commit_button_is_not_the_hazard(PR):
    """`commitByButton` is true in three positions and only one of them is
    dangerous. A digit on the button does not reach a text field, so refusing here
    would take the verb away for nothing."""
    parsed = PR.parse(editing_picker(
        OPTION_ROW, FREE_TEXT_HIGHLIGHTED.replace('\x1b[38;5;105m❯\x1b[39m', ' '),
        COMMIT_NEXT_HIGHLIGHTED, CHAT_ROW))
    assert "digitBecomesText" not in parsed


def test_the_highlight_on_chat_about_this_is_not_the_hazard(PR):
    """The third position in the group, and the one §7.10's captures omit
    entirely — they stop at the button, which reads as though it is the last row."""
    parsed = PR.parse(editing_picker(
        OPTION_ROW, FREE_TEXT_HIGHLIGHTED.replace('\x1b[38;5;105m❯\x1b[39m', ' '),
        COMMIT_NEXT, CHAT_ROW_HIGHLIGHTED))
    assert "digitBecomesText" not in parsed


def test_no_hazard_is_claimed_where_the_highlight_is_on_an_option(PR):
    """Absent `commitByButton` means the highlight is on an option row, where a
    digit does what it has always done. The narrowing is what keeps this from
    refusing on every picker."""
    parsed = PR.parse(picker(OPTION_ROW, COMMIT_NEXT, CHAT_ROW))
    assert "digitBecomesText" not in parsed


def test_an_ordinary_option_row_commits_with_right(PR):
    """Absent is the whole degradation, and it is also the common case.

    The hint appears in exactly two positions and nowhere else, so its absence
    means `Right` is the commit — which is what every caller did before this field
    existed. An unrecognised version that drops `rendered` lands here too.
    """
    assert "commitByButton" not in PR.parse(picker(MULTISELECT_OPTION, COMMIT_NEXT))


def test_the_hint_is_read_off_the_footer_rather_than_scanned_for(PR):
    """It is part of the footer line, not a row, so transcript text cannot supply
    it — the footer is already anchored and only that line is consulted."""
    body = "\x1b[1m     ctrl+g to edit in Vim\x1b[0m"
    assert "commitByButton" not in PR.parse(picker(MULTISELECT_OPTION, body))


def test_the_tab_bars_submit_is_not_the_commit_row(PR):
    """Two different `Submit`s on one screen, and confusing them submits a gate.

    The tab bar's `✔ Submit` is the review TAB; the commit row is a bare word
    under the options. A parser that matched the tab bar here would report
    `submit` on every multi-question gate, including one whose commit advances.
    """
    parsed = PR.parse(picker(TABS_Q0, "Colour?", MULTISELECT_OPTION, COMMIT_NEXT))
    assert parsed["multiSelectCommit"] == "next"
    assert parsed["questionIndex"] == 0, "the tab bar should still be read"


def test_prose_containing_the_word_is_not_the_commit_row(PR):
    """Anchored at both ends, because the marker is one ordinary English word.

    This is the easiest marker in the module for transcript text to imitate, so it
    carries the free-text row's guards: inside the footer window, and bold.
    """
    hostile = "\n".join([
        "  Next, tick the boxes you want.",       # prose, unstyled
        "     Submit",                             # the right shape, no bold
        FOOTER_MULTI,
    ])
    assert PR.parse(hostile) is None


def test_a_commit_row_far_above_the_footer_is_out_of_reach(PR):
    """Bounded lookback, the same one the free-text row scans in."""
    far = "\n".join([COMMIT_SUBMIT] + ["filler"] * 60 + [FOOTER_MULTI])
    assert PR.parse(far) is None


# --- the same class of finding, on the tab bar ---------------------------------
#
# Raised in review of #303 on the round that fixed the free-text row: the tab bar
# scan had the identical defect and none of the guards. Deck acts on both fields —
# a wrong `questionIndex` moves its cursor to the wrong question, and a wrong
# `onSubmitTab` leaves a gate that cannot be submitted from the pane.


def test_transcript_text_above_a_picker_cannot_supply_the_tab_bar(PR):
    """THE FINDING. A model DESCRIBING a picker used to win, because it is higher.

    The scan ran top-down and required nothing of the styling, so a prior turn's
    prose about a picker was read as the bar. Here it would answer `questionIndex`
    0 — `Colour` — where the truth is 1.
    """
    hostile = "\n".join([
        "I would render it as: ☐ Colour ☒ Size ✔ Submit",   # unstyled model output
        "",
        TABS_Q1,                                             # the real bar, chipped
        FOOTER_MULTI,
    ])
    assert PR.parse(hostile) == {"onSubmitTab": False, "questionIndex": 1}


def test_an_unstyled_tab_bar_is_not_a_tab_bar(PR):
    """The chip is load-bearing here for the reason the dim attribute is below.

    Strip the styling from a real bar and it becomes indistinguishable from prose
    about one, so the only honest answer is silence.
    """
    assert PR.parse("\n".join([PR._plain(TABS_Q1), FOOTER_MULTI])) is None


def test_the_bottom_most_tab_bar_wins(PR):
    """Two chipped candidates: the picker is the bottom-most thing drawn."""
    assert PR.parse("\n".join([TABS_Q0, TABS_Q1, FOOTER_MULTI]))["questionIndex"] == 1


def test_the_review_tab_is_read_although_it_draws_no_footer(PR):
    """WHY THE TAB BAR IS NOT FOOTER-ANCHORED, pinned to the capture that says so.

    The obvious fix for the finding above was the free-text row's own first guard:
    find the footer, scan only above it. The review view has no footer — this
    fixture is the whole view and `Esc to cancel` is not in it — so that fix would
    have returned None here and cost `onSubmitTab` exactly where it is needed. A
    gate whose Submit tab the pane cannot see hangs, which is worse than the
    mis-parse being fixed.
    """
    assert "Esc to cancel" not in REVIEW_VIEW, "the fixture no longer makes the point"
    assert PR.parse(REVIEW_VIEW)["onSubmitTab"] is True


# --- how the chip closes ------------------------------------------------------
#
# Raised by review of #303 as latent: the chip was closed on a substring test, so
# a combined reset left it open and a LATER tab read as active. A wrong
# `questionIndex` is the failure mode worth code, because Deck acts on it — an
# absent one only costs the feature.


@pytest.mark.parametrize("closer", [
    "\x1b[49m",          # what 2.1.220 actually emits
    "\x1b[0m",           # a bare full reset
    "\x1b[m",            # the parameterless full reset
    "\x1b[0;39m",        # combined — the case the substring test missed
    "\x1b[39;49m",       # combined the other way round
])
def test_every_reset_form_closes_the_chip(PR, closer):
    line = f"← \x1b[38;5;231m\x1b[48;5;105m ☐ Colour {closer} ☐ Size  ✔ Submit  →"
    assert PR.parse(line) == {"onSubmitTab": False, "questionIndex": 0}, (
        f"{closer!r} left the chip open, so a later tab read as live"
    )


def test_a_colour_containing_49_does_not_close_the_chip(PR):
    """The substring test also matched too much: `38;5;149m` contains `49m`."""
    line = ("← \x1b[38;5;231m\x1b[48;5;105m ☐ Colour \x1b[38;5;149m ☒ Size "
            "\x1b[49m ✔ Submit  →")
    assert PR.parse(line)["questionIndex"] == 0


def test_a_checkbox_shaped_option_label_does_not_force_a_commit_row(PR, monkeypatch):
    """The invariant keys on the ROW's checkbox, never on one inside a label.

    Option text is model-authored and passed through verbatim, so a question
    about checkbox markdown can carry `3. [ ]` in its own wording. Unanchored,
    that made a genuinely single-select picker demand a commit row it never
    draws — an over-refusal, fail-safe but pointless, and exactly the needless
    darkness this change exists to end. Raised in review of #387.
    """
    label = '  \x1b[38;5;241m2.\x1b[39m how do I write 3. [ ] in markdown?'
    single = picker(TABS_Q1, label, SINGLE_FREE_TEXT, CHAT_ROW_HIGHLIGHTED)
    monkeypatch.setattr(PR.TT, "claude_pane", lambda name: ("%9", 1))
    monkeypatch.setattr(PR.TT, "run", reading_at(PR.MARKERS_LAST_DRIVEN, single))
    rendered, dark = PR.read_detail("s")
    assert dark is None, "a label that mentions a checkbox is not a multiselect"
    assert rendered["freeTextRow"] == 4


# --- styling is load-bearing, and it has to be PARSED to be load-bearing -------
#
# `_DIM` and `_ACTIVE_BG` are bare SGR parameter strings — `38;5;241`, `48;5;105`
# — with no `ESC[`…`m` around them, and every guard using them was a plain `in`
# test until 2026-08-10. Pane content is partly written by the session itself, so
# a line of prose mentioning the number satisfied a check meant to establish that
# the TUI drew it. Both directions are pinned below. Found in security review of
# #387.

def test_a_capture_with_no_escapes_at_all_cannot_forge_a_picker(PR, monkeypatch):
    """The false pass: plain text passed the self-test outright."""
    forged = "\n".join([
        "  the colour 38;5;241 is a nice grey",
        "  1. an option row 38;5;241",
        "❯ 2. another 38;5;241",
        "Enter to select · Esc to cancel",
    ])
    assert "\x1b" not in forged
    monkeypatch.setattr(PR.TT, "claude_pane", lambda name: ("%9", 1))
    monkeypatch.setattr(PR.TT, "run", reading_at(PR.MARKERS_LAST_DRIVEN, forged))
    rendered, dark = PR.read_detail("s")
    assert rendered is None
    assert dark["reason"] == "markers-moved"


def test_a_planted_row_cannot_force_a_real_picker_to_refuse(PR, monkeypatch):
    """The forced refusal, which is the same hole pointing the other way.

    One plain-text line inside the lookback window, naming the dim colour and
    shaped like a checkbox row, manufactured the checkbox invariant's
    precondition — so a genuinely intact single-select picker, which correctly
    draws no commit row, was refused as `markers-moved`. A denial of service on
    a picker that never moved.
    """
    planted = '  9. [x] some fake row 38;5;241'
    assert "\x1b" not in planted
    single = picker(TABS_Q1, planted, '  \x1b[38;5;241m2.\x1b[39m Anchovies',
                    SINGLE_FREE_TEXT, CHAT_ROW_HIGHLIGHTED)
    monkeypatch.setattr(PR.TT, "claude_pane", lambda name: ("%9", 1))
    monkeypatch.setattr(PR.TT, "run", reading_at(PR.MARKERS_LAST_DRIVEN, single))
    rendered, dark = PR.read_detail("s")
    assert dark is None, "a planted plain-text row is not a checkbox row"
    assert rendered["freeTextRow"] == 4


def test_a_forged_free_text_row_is_not_read(PR):
    """The same root cause in `parse`'s own scan, which predates this gate.

    It was reachable only on one exact pinned version before; a floor widens it
    to every build from 2.1.220 on, which is why it is fixed here rather than
    filed.
    """
    forged = picker('  38;5;241 and then 2. Type something.', FOOTER_MULTI)
    assert "freeTextRow" not in (PR.parse(forged) or {})


def test_draws_with_wants_the_parameters_inside_a_real_sequence(PR):
    """Consecutive tokens in one genuine `ESC[`…`m`, and nothing looser."""
    assert PR._draws_with("\x1b[38;5;241mgrey\x1b[39m", "38;5;241")
    # The TUI combining attributes into one sequence still counts.
    assert PR._draws_with("\x1b[1;38;5;241mbold grey\x1b[0m", "38;5;241")
    # Prose naming the colour does not.
    assert not PR._draws_with("the colour 38;5;241 is grey", "38;5;241")
    # Nor a near-miss that only shares a prefix.
    assert not PR._draws_with("\x1b[38;5;24m\x1b[1m", "38;5;241")


def test_a_pane_version_that_is_not_a_token_is_not_put_on_the_wire(PR, monkeypatch):
    """`renderedDark` is meant to be DRAWN, and Deck renders panes as HTML.

    `pane_current_command` reports whatever the foreground process calls itself,
    so the string is bounded here rather than trusted to be escaped correctly at
    every future consumer. Refused rather than clipped: a truncated string is a
    claim about what the pane reported, and the `reason` beside it already says
    the version could not be parsed.
    """
    hostile = "<img src=x onerror=alert(1)>"
    monkeypatch.setattr(PR.TT, "claude_pane", lambda name: ("%9", 1))
    monkeypatch.setattr(PR.TT, "run", reading_at(hostile, a_live_question()))
    rendered, dark = PR.read_detail("s")
    assert rendered is None
    assert dark["reason"] == "version-unparseable"
    assert dark["paneVersion"] == "<unreadable>"


def test_an_ordinary_non_claude_command_is_still_reported_as_itself(PR, monkeypatch):
    """`zsh` is the common case and is diagnostic, so it stays readable."""
    monkeypatch.setattr(PR.TT, "claude_pane", lambda name: ("%9", 1))
    monkeypatch.setattr(PR.TT, "run", reading_at("zsh", a_live_question()))
    _, dark = PR.read_detail("s")
    assert dark["paneVersion"] == "zsh"


# --- splitting on `;` is not parsing ------------------------------------------

COMMIT_SUBMIT_COMBINED = '     \x1b[1;38;5;241mSubmit\x1b[0m'


def test_a_256_colour_index_is_not_the_attribute_its_digits_resemble(PR):
    """`ESC[38;5;1m` is red, and its tokens contain a standalone `1` — bold.

    The obvious conversion of the bold check, `_draws_with(line, "1")` over a
    token slice, read every red-drawn row as the commit row. `_sgr_operations`
    consumes the extended-colour compounds as units, so the digits inside one
    are never offered as an attribute.
    """
    assert not PR._draws_with("\x1b[38;5;1mred\x1b[0m", "1")
    assert PR._draws_with("\x1b[38;5;1mred\x1b[0m", "38;5;1")
    assert list(PR._sgr_operations("\x1b[1;38;5;241mx\x1b[0m")) == ["1", "38;5;241", "0"]


def test_a_commit_row_drawn_with_combined_attributes_is_still_read(PR):
    """The behavioural half, and the reason this was not left as a suggestion.

    `_BOLD` was the literal `ESC[1m` and tested with `in`, so a TUI that drew the
    button as `ESC[1;38;5;241m` — one sequence, bold combined with the grey —
    matched nothing. A missing `multiSelectCommit` is the field `submit_choices`
    reads as "not `next`" before sending a trailing digit into the question it
    just advanced to. Nothing observed draws it combined today, which makes this
    latent rather than live, and latent in the one field the module's own docs
    call dangerous rather than limiting.
    """
    combined = picker(TABS_Q1, OPTION_ROW, MULTISELECT_FREE_TEXT,
                      COMMIT_SUBMIT_COMBINED, CHAT_ROW_HIGHLIGHTED)
    assert PR.parse(combined)["multiSelectCommit"] == "submit"
    assert PR._skeleton_reason(combined) is None


def test_a_malformed_compound_cannot_synthesise_an_attribute(PR):
    """`38` must be followed by `5` or `2`, and when it is not, nothing after it
    is a parameter this can name honestly.

    The first draft of the walk fell through to yielding tokens singly, so
    `ESC[38;1m` — which sets nothing a terminal agrees on — offered a `1` and
    read as BOLD, an attribute the TUI never applied. No sequence in any real
    capture is malformed this way, so refusing them costs nothing observed.
    """
    assert list(PR._sgr_operations("\x1b[38;1m")) == []
    assert list(PR._sgr_operations("\x1b[38;5m")) == []
    assert list(PR._sgr_operations("\x1b[38;2;1;2m")) == []
    assert not PR._draws_with("\x1b[38;1m", "1")
    # A well-formed compound still lets a following attribute through.
    assert list(PR._sgr_operations("\x1b[38;5;241;1m")) == ["38;5;241", "1"]
