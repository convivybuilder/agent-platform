"""Driving and observing a live co session — Deck's two verbs.

WHAT THESE PIN, and why they are the tests that matter:

The input side is a sequence, not a call. `Escape` puts the interrupted message
back in the input box, so a send that does not clear first submits a
concatenation of two messages. The clear itself must never be `Ctrl-C`, which
interrupts whatever the session is doing — including a subagent working behind a
`waiting` main agent. A message must arrive as a bracketed paste, because typing a
leading `/` opens the slash menu and turns the following Enter into a command
selection. Each of those is asserted on the ORDER of tmux calls, since the bug
in every case is a correct set of calls in the wrong arrangement.

The state side folds an event stream. Its sharp edge is that the two classes of
gate carry different payload shapes, so a reader that assumes `questions` finds
a `command` instead.

Nothing here shells out. tmux, the process tree and the clock are injected.
"""
from __future__ import annotations

import datetime
import importlib.util
import json
import logging
import os
import sys
import time
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parent.parent / "lib" / "agent-platform"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, LIB / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def SI():
    return _load("session_input")


@pytest.fixture
def SS():
    return _load("session_state")


@pytest.fixture(autouse=True)
def _isolated_slate_home(tmp_path, monkeypatch):
    """`session_state()`/`session_states()` now resolve `asks_open`/
    `asks_answered` through `slate.ask_counts` unconditionally (CQ-358,
    platform/decisions/0091), and `slate.state_root()` re-reads `Path.home()`
    on every call rather than being a patchable module-level constant the way
    every other root this file's `SS` fixture touches is (`VITALS_DIR`,
    `HOOK_LOG_ROOT`, …, each overridden per test below). Without this, every
    `session_state()` call in this file — and there are dozens, none of them
    otherwise touching HOME — would create `~/.local/state/convivy/slate/
    <name>.json` on whatever machine runs this suite: on a dev box, the REAL
    per-crew Slate files Deck reads and the operator answers from.
    """
    monkeypatch.setenv("HOME", str(tmp_path))


@pytest.fixture(autouse=True)
def _no_private_crews(SS, monkeypatch):
    """Every `_hook_logs`/`HOOK_LOG_ROOT` test in this file searches the shared
    root — private-crew routing is covered further down in THIS file, under the
    "Private-crew hook-log routing" `MARK:` block, which overrides this
    fixture's stub with a real crew-aware `private_home_for`. Neutralizing
    `private_home_for` keeps `_hook_logs(session_id)` called with no `cwd`
    behaving exactly as it did before private-crew search existed, rather than
    treating an unspecified `cwd` as `UNKNOWN` (deliberately fail-closed per
    `private_crew`'s contract) and returning `[]` unconditionally. It would
    also, absent this, read the REAL `~/Convivy/agent-platform/config/projects.json`
    on a dev box where it exists — this pins every test here to the fake
    `HOOK_LOG_ROOT` alone, same as `conftest.py`'s stated hermeticity goal for
    every other RUNS_ROOT-family path.
    """
    monkeypatch.setattr(SS, "private_home_for", lambda cwd: None)
    # The tests below pass literal `/srv/convivy/<repo>` cwds, and
    # `live_agents` scopes a co by the machine's root (CREWS_ROOT, from
    # builder_root.root()). Pinned so CI, whose root is elsewhere, scopes them the
    # same as the mini does (review, PR 539).
    monkeypatch.setattr(SS, "CREWS_ROOT", SS.Path("/srv/convivy"))


@pytest.fixture
def tmux(SI, monkeypatch):
    """Record every tmux invocation instead of running it."""
    calls = []

    def fake_run(argv, stdin=None):
        calls.append((tuple(argv), stdin))
        return ""

    monkeypatch.setattr(SI.TT, "run", fake_run)
    monkeypatch.setattr(SI.TT, "claude_pane", lambda name: ("%9", 4242))
    monkeypatch.setattr(SI.time, "sleep", lambda _s: None)
    # Idle by default, so the tests written before the clear became conditional
    # still describe the case they were written for. `session_is` overrides it.
    monkeypatch.setattr(SI, "_current_state", lambda name: "idle")
    return calls


@pytest.fixture
def session_is(SI, monkeypatch):
    """Pin what `send_message` believes the session is doing."""
    def set_state(state):
        monkeypatch.setattr(SI, "_current_state", lambda name: state)
    return set_state


def _contained_transcript(tmp_path, monkeypatch, name="t.jsonl"):
    """A transcript where the containment guard will accept it.

    Real transcripts live under ~/.claude/projects and the guard requires it, so
    a test putting one in a bare tmpdir is testing a path production never
    takes."""
    home = tmp_path / "home"
    d = home / ".claude" / "projects" / "-proj"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    t = d / name
    t.write_text("{}")
    return t


def _verbs(calls):
    """The tmux subcommand of each call, in order."""
    return [c[0][1] for c in calls]


# MARK: - Sending a message


def test_a_message_is_cleared_pasted_then_submitted_in_that_order(SI, tmux):
    """`display-message` is the verification predicates' own version check —
    see `test_an_unrecognised_version_falls_back_to_the_old_sleep` — which the
    default fixture's empty version string always fails, so each predicate
    reads once and falls back rather than polling. The clear/paste/submit
    order this test is actually about is unaffected by where those reads
    land."""
    SI.send_message("deck", "hello")
    assert _verbs(tmux) == ["send-keys", "load-buffer", "paste-buffer",
                            "delete-buffer", "display-message", "send-keys",
                            "display-message"]


def test_the_clear_comes_before_the_paste(SI, tmux):
    """An interrupt leaves the interrupted message in the box. Pasting onto it
    submits both at once, which is why order is the assertion."""
    SI.send_message("deck", "hello")
    assert tmux[0][0] == ("tmux", "send-keys", "-t", "%9",
                          *(["C-u"] * SI.CLEAR_KEYSTROKES))


def test_the_clear_never_uses_ctrl_c_in_any_state(SI, tmux, session_is):
    """THE REGRESSION THIS LOCKS, and it is the second time this bug shipped.

    The clear was Ctrl-C. A first fix stopped it firing at a `working` session
    after it cut off a tool call — but kept `waiting` in the allowed set, and
    `waiting` is exactly what a main agent reads while a background subagent
    works on behind it. So every message into a session with background work
    killed that work, intermittently. The operator lost two selector build runs to it on
    2026-07-24.

    Asserted across every state rather than only the clearable ones: the bug
    both times was a state nobody thought was dangerous, so the property worth
    pinning is that NO state produces a Ctrl-C, not that a chosen few do not.
    """
    for state in ["idle", "waiting", "working", "ended", "unknown", None]:
        tmux.clear()
        session_is(state)
        SI.send_message("deck", "hello")
        assert not any("C-c" in call[0] for call in tmux), \
            f"Ctrl-C reached a session reading {state!r}"


def test_the_clear_is_one_call_carrying_every_press(SI, tmux):
    """One `send-keys`, not twelve. The presses arrive as a run with no gap for
    a keystroke of the user's own to land in the middle of, and it is a single
    subprocess rather than one per press."""
    SI.send_message("deck", "hello")
    clears = [c[0] for c in tmux if "C-u" in c[0]]
    assert len(clears) == 1
    assert clears[0].count("C-u") == SI.CLEAR_KEYSTROKES


@pytest.mark.parametrize("state", ["working", "ended", "unknown", None])
def test_a_message_never_interrupts_a_turn_it_did_not_start(SI, tmux, session_is, state):
    """THE BUG THIS CLOSES. `clear` is Ctrl-C, and Ctrl-C into a session that is
    mid-turn interrupts it — killing a tool call, silently, with nothing
    afterwards to say the sender did it.

    Typing into the same session by hand never does this: Claude Code's TUI
    holds typed input until the turn can take it. The Ctrl-C was the only
    difference between a human's message and Deck's, and it made the session
    pane unusable. Reported by the operator 2026-07-23 after it cut off a live tool call.

    Everything but `idle`/`waiting` skips the clear, including a state that
    could not be read: a concatenated message is visible and resendable, a
    killed tool call is neither. `blocked` is absent because it no longer gets
    this far — see `test_a_message_at_a_picker_is_refused_entirely`.
    """
    session_is(state)
    SI.send_message("deck", "hello")
    assert _verbs(tmux) == ["load-buffer", "paste-buffer", "delete-buffer",
                            "display-message", "send-keys", "display-message"]
    assert not any("C-c" in c[0] for c in tmux), "Ctrl-C reached a session mid-turn"


def test_a_message_at_a_picker_is_refused_entirely(SI, tmux, session_is):
    """THE BUG THIS CLOSES. A blocked session has a picker up, and a picker eats
    a paste: the text lands in its filter box and the trailing Enter takes
    whichever row is highlighted. So the message does not arrive late, it answers
    the question — with an option nobody chose.

    The operator watched this happen on 2026-07-24, when a picker came up while
    typing and a half-finished sentence went into it. Deck also draws buttons
    instead of a text box when it knows the session is blocked, but it learns
    that from a two-second poll; a picker raised inside that window still meets a
    live composer, so the guard that counts is this one.

    NOT ONE KEYSTROKE, which is the assertion that matters. Refusing after the
    paste would already have delivered the answer.
    """
    session_is("blocked")
    with pytest.raises(SI.InputError, match="waiting on a question"):
        SI.send_message("deck", "hello")
    assert tmux == [], "keystrokes reached a session that was waiting on a question"


def test_a_message_at_a_picker_is_refused_even_with_an_explicit_pane_id(
        SI, tmux, session_is):
    """CQ-420 ROUND 2 REGRESSION. The pane-agreement fix made `slate-send`
    always pass `pane_id` (resolved once via `resolve_pane`) into
    `send_message` — and `pane_id` used to skip the `blocked` gate
    entirely, since the gate only ran in the by-name-resolution branch.
    `resolve_pane`/`claude_pane` resolves to any pane with a claude
    process; it does not check whether that session is blocked on a
    picker. So a `pane_id`-given call used to reach tmux even while the
    session was waiting on a question — exactly the paste-into-a-picker
    failure the gate exists to prevent, reintroduced through the one
    caller (`slate-send`) the gate used to cover.

    This fails against 1a066bf7: `send_message` there returns `pane_id`
    straight from `if pane_id is not None: pane = pane_id`, never calling
    `_read_state`, so no `InputError` is raised and the paste keystrokes
    land in `tmux`.
    """
    session_is("blocked")
    with pytest.raises(SI.InputError, match="waiting on a question"):
        SI.send_message("deck", "hello", pane_id="%9")
    assert tmux == [], "keystrokes reached a session that was waiting on a question"


def test_an_explicit_pane_id_still_sends_when_the_session_is_not_blocked(
        SI, tmux, session_is):
    """The companion case to the regression test above: the `blocked` gate
    now runs on the `pane_id`-given path too, but it must not refuse a
    send that was never blocked in the first place — `slate-send`'s normal
    case, every time."""
    session_is("idle")
    pane = SI.send_message("deck", "hello", pane_id="%9")
    assert pane == "%9"
    assert "paste-buffer" in _verbs(tmux)


@pytest.mark.parametrize("state", ["idle", "waiting"])
def test_an_idle_session_is_still_cleared_before_the_paste(SI, tmux, session_is, state):
    """The clear is not gone, only conditional. Escape restores the interrupted
    message into the box and leaves the session idle, so pasting onto it would
    submit two messages as one — which is the case the clear exists for."""
    session_is(state)
    SI.send_message("deck", "hello")
    assert tmux[0][0] == ("tmux", "send-keys", "-t", "%9",
                          *(["C-u"] * SI.CLEAR_KEYSTROKES))


def test_an_unreadable_state_does_not_clear(SI, tmux, monkeypatch):
    """The state machinery failing must not be a reason to touch the box."""
    def boom(name):
        raise RuntimeError("no run dir")
    monkeypatch.setattr(SI, "_current_state", boom)
    SI.send_message("deck", "hello")
    assert not any("C-u" in c[0] or "C-c" in c[0] for c in tmux)


def test_the_message_is_pasted_bracketed_and_never_typed(SI, tmux):
    """`-p` is the whole point: typing a leading `/` opens the slash menu and the
    following Enter selects a command instead of submitting."""
    SI.send_message("deck", "/looks-like-a-command")
    paste = next(c[0] for c in tmux if c[0][1] == "paste-buffer")
    assert "-p" in paste
    typed = [c for c in tmux if c[0][1] == "send-keys" and "-l" in c[0]]
    assert typed == [], "message text must never be sent as keystrokes"


def test_the_text_travels_on_stdin_not_argv(SI, tmux):
    """Keeps arbitrary user text out of the process table and out of argv."""
    secret = "a message with spaces; and $metacharacters"
    SI.send_message("deck", secret)
    load = next(c for c in tmux if c[0][1] == "load-buffer")
    assert load[1] == secret
    assert all(secret not in " ".join(c[0]) for c in tmux)


def test_a_repeated_clear_cannot_exit_the_session(SI, tmux):
    """This used to pin "only ONE clear is ever sent", because the clear was
    Ctrl-C and two of those in quick succession exit Claude Code. The clear is
    now Ctrl-U, which repeats safely — that is what lets it empty a multi-line
    draft a line at a time. The property worth keeping is the consequence rather
    than the old count: however many presses go out, none of them can end the
    session."""
    SI.send_message("deck", "hello")
    keys = [k for c in tmux if c[0][1] == "send-keys" for k in c[0][4:]]
    assert set(keys) <= {"C-u", "Enter"}, keys


def test_the_submit_waits_for_the_paste_to_settle(SI, tmux, monkeypatch):
    """An Enter sent immediately after the paste did nothing; the same Enter
    later submitted."""
    slept = []
    monkeypatch.setattr(SI.time, "sleep", lambda s: slept.append(s))
    SI.send_message("deck", "hello")
    assert slept and slept[0] > 0


def test_an_empty_message_is_refused(SI, tmux):
    """A bare Enter into a non-empty box submits whatever was left there."""
    for blank in ("", "   ", "\n\t"):
        with pytest.raises(SI.InputError):
            SI.send_message("deck", blank)
    assert tmux == [], "nothing should reach tmux"


def test_the_staged_buffer_is_deleted_after_pasting(SI, tmux):
    SI.send_message("deck", "hello")
    assert "delete-buffer" in _verbs(tmux)


# MARK: - Verifying a paste landed and a message submitted
#
# RE-VERIFIED 2026-08-09 AGAINST CLAUDE CODE 2.1.226. The captures below are
# CONSTRUCTED from what that session observed — a bare, colour-246 rule
# directly above and below the box, `❯` + U+00A0 leading the first line, two
# plain spaces leading every line after it — rather than pasted verbatim the
# way `test_picker_render.py`'s fixtures are, because no capture taken this
# session ever held a ghost suggestion to paste in. `picker_render`'s own
# 2026-07-30 experiment against 2.1.220 found the same absence. The SGR-2
# stripping this module does is coded to the spec's claim and to real uses of
# `ESC[2m` elsewhere in the same TUI (the status line's icons, and
# `test_picker_render.FREE_TEXT_HIGHLIGHTED`'s own placeholder text), not to a
# live ghost this session could reproduce.


def _render_box(text):
    """The screen lines Claude Code draws for *text* sitting in the input
    box: cursor + U+00A0 on the first line, two plain spaces on every line
    after it — indistinguishable from where a soft wrap would have landed,
    which is real and observed (see the module note above `send_message`)."""
    lines = text.split("\n")
    out = [f"\x1b[39m❯\xa0{lines[0]}"]
    out.extend(f"  {line}" for line in lines[1:])
    return out


def _capture_with_box(text, ghost=None, version="2.1.226", border_fg="38;5;246"):
    """A minimal `capture-pane -e -p` reading holding *text* in the input
    box, bracketed by the bare rule `_parse_input_box` anchors on. *ghost*,
    if given, is appended to the first line under SGR 2 — CONSTRUCTED, per
    the section note above, not a captured ghost.

    *border_fg* defaults to the colour observed on 2.1.226/2.1.227
    (`38;5;246`). Pass `38;5;245` for a 2.1.292-shaped capture (CQ-422) — the
    colour the installed build moved the rule to, with its shape and
    position unchanged."""
    border = f"\x1b[{border_fg}m" + "─" * 30
    box = _render_box(text)
    if ghost is not None:
        box[0] += f"\x1b[2m{ghost}\x1b[0m"
    return "\n".join(["welcome banner", border, *box, border, "footer status"]) + "\n"


def test_parse_input_box_reads_real_pasted_text(SI):
    assert SI._parse_input_box(_capture_with_box("hello world")) == "hello world"


def test_parse_input_box_reads_an_empty_box(SI):
    assert SI._parse_input_box(_capture_with_box("")) == ""


def test_parse_input_box_reconstructs_a_multiline_paste(SI):
    """Observed: an explicit newline and a soft wrap render identically, both
    as a plain two-space indent with no marker distinguishing them. This is
    why comparisons go through `_normalize_box_text` rather than exact
    equality — but the raw join is still exactly the sent text when nothing
    wrapped."""
    text = "line one\nline two\nline three"
    assert SI._parse_input_box(_capture_with_box(text)) == text


def test_parse_input_box_strips_a_ghost_styled_suggestion(SI):
    """THE HAZARD THIS GUARDS. A ghost suggestion under `-e` carries SGR 2 —
    CONSTRUCTED here per the section note, since none reproduced live — and a
    reader that compared raw pane text against the sent message would count
    it as landed content nobody typed."""
    capture = _capture_with_box("real", ghost=" ghost-completion")
    assert SI._parse_input_box(capture) == "real"


def test_parse_input_box_does_not_confuse_the_disabled_grey_for_a_ghost(SI):
    """THE DISTINCTION THE SECTION NOTE MAKES BY CONSTRUCTION. A mid-turn
    input box is disabled and drawn in `picker_render._DIM` (256-colour grey
    241), observed live — never SGR 2. Only literal SGR parameter 2 is
    stripped, so this must survive untouched."""
    disabled = "\n".join([
        "welcome banner",
        "\x1b[38;5;246m" + "─" * 30,
        "\x1b[38;5;241m❯\xa0\x1b[39m",
        "\x1b[38;5;246m" + "─" * 30,
        "footer status",
    ]) + "\n"
    assert SI._parse_input_box(disabled) == ""


def test_parse_input_box_returns_none_without_two_borders(SI):
    assert SI._parse_input_box("no rule anywhere in this capture\n") is None
    one_border = "\x1b[38;5;246m" + "─" * 30
    assert SI._parse_input_box(f"before\n{one_border}\nafter\n") is None


def test_parse_input_box_takes_the_bottom_most_pair(SI):
    """A rule-lookalike earlier in the capture — transcript text, a banner —
    must not be mistaken for the box. The real box is always the bottom-most
    thing tmux draws."""
    border = "\x1b[38;5;246m" + "─" * 30
    decoy = "\n".join(["intro", border, "not the box", border, "more transcript"])
    capture = decoy + "\n" + _capture_with_box("real content")
    assert SI._parse_input_box(capture) == "real content"


def test_parse_input_box_returns_none_with_nothing_between_the_borders(SI):
    border = "\x1b[38;5;246m" + "─" * 30
    assert SI._parse_input_box(f"{border}\n{border}\n") is None


def test_visible_clears_dim_on_an_explicit_22_reset(SI):
    """`22` is SGR's dim/bold-off, distinct from a full reset (`0`). Real
    text following it must not still read as a ghost."""
    line = "\x1b[2mghost\x1b[22mreal"
    assert SI._visible(line) == "real"


def test_visible_keeps_a_256_colour_run_whose_palette_index_is_literally_2(SI):
    """FIX 3. `38;5;N`/`48;5;N` is a three-token COMPOUND selector — a palette
    index that happens to be `2` must not read as the standalone SGR-2 (dim)
    attribute and get the whole run dropped."""
    assert SI._visible("\x1b[38;5;2mHello\x1b[0m") == "Hello"
    assert SI._visible("\x1b[48;5;2mHello\x1b[0m") == "Hello"


def test_visible_still_strips_a_real_dim_following_a_compound_colour(SI):
    """The compound is consumed as three tokens, so the parser's index has to
    land exactly on the next INDEPENDENT parameter. Here the first `2` is a
    palette index and must be kept, while the trailing one is a genuine dim
    attribute and must still strip the run.

    WHAT THESE TWO ASSERTIONS CATCH IS OVER-CONSUMPTION, not drift in both
    directions — established by mutation-testing the parser at review rather
    than assumed. Stepping the index one too far (`i += 4`) swallows the real
    dim and fails here. Stepping one too SHORT (`i += 2`) still passes both,
    because the walk reaches the trailing dim either way and that token sets
    the attribute regardless of what the misaligned slot before it read as.

    Under-consumption is caught instead by
    `test_visible_keeps_a_256_colour_run_whose_palette_index_is_literally_2`,
    where a bare `38;5;2` has no trailing token to rescue the walk: misalign
    it and the palette index reads as dim and the run wrongly disappears. The
    pair covers both directions; neither test does it alone.
    """
    assert SI._visible("\x1b[38;5;2;2mGhost\x1b[0m") == ""
    # ...and the mirror: a compound whose index is NOT 2, followed by a real
    # dim, strips just the same. Nothing about the run's colour decides this.
    assert SI._visible("\x1b[38;5;241;2mGhost\x1b[0m") == ""


def test_read_input_box_returns_none_on_a_failed_capture(SI, monkeypatch):
    """A version match that then fails to capture is still `None` — one
    answer for every kind of unavailability, matching `picker_render.read`."""
    def fake_run(argv, stdin=None):
        if argv[1] == "display-message":
            return SI.INPUT_BOX_VERIFIED_FLOOR
        return None  # capture-pane failing
    monkeypatch.setattr(SI.TT, "run", fake_run)
    assert SI._read_input_box("%9") is None


def test_read_input_box_names_the_capture_failure_in_its_warning(SI, monkeypatch, caplog):
    """THE THIRD DEGRADATION PATH, which the warning text used to mislabel: a
    version match whose `capture-pane` then fails is neither an old pane nor
    a recoloured border, so the logged reason must say the capture itself
    failed rather than naming either of the other two causes."""
    def fake_run(argv, stdin=None):
        if argv[1] == "display-message":
            return SI.INPUT_BOX_VERIFIED_FLOOR
        return None  # capture-pane failing
    monkeypatch.setattr(SI.TT, "run", fake_run)
    with caplog.at_level(logging.WARNING, logger=SI._LOG.name):
        assert SI._read_input_box("%9") is None
    warnings = [r for r in caplog.records if r.name == SI._LOG.name]
    assert len(warnings) == 1
    assert "the pane capture itself failed" in warnings[0].getMessage()
    assert "predates" not in warnings[0].getMessage()
    assert "_BOX_BORDER_FG_CODES" not in warnings[0].getMessage()


def _run_against_capture(capture, version):
    """A `TT.run` fake reporting *version* from `display-message` and
    *capture* from `capture-pane` — the shape `_read_input_box` needs end to
    end, rather than only through `_parse_input_box`."""
    def fake_run(argv, stdin=None):
        if argv[1] == "display-message":
            return version
        if argv[1] == "capture-pane":
            return capture
        return None
    return fake_run


def test_read_input_box_reads_a_2_1_292_shaped_pane_with_the_245_border(SI, monkeypatch):
    """CQ-422. The installed build moved the composer's border to
    `38;5;245`; the reader must still find it end to end, through the
    version gate as well as the parse."""
    capture = _capture_with_box("synthetic draft text", border_fg="38;5;245")
    monkeypatch.setattr(SI.TT, "run", _run_against_capture(capture, "2.1.292"))
    assert SI._read_input_box("%9") == "synthetic draft text"


def test_read_input_box_still_reads_a_2_1_227_shaped_pane_with_the_246_border(
        SI, monkeypatch):
    """The regression this change must not cause: a build still drawing the
    border in the original `38;5;246` keeps reading, through the same set
    the 245 case above matches against."""
    capture = _capture_with_box("synthetic draft text", border_fg="38;5;246")
    monkeypatch.setattr(SI.TT, "run", _run_against_capture(capture, "2.1.227"))
    assert SI._read_input_box("%9") == "synthetic draft text"


def test_read_input_box_stays_unavailable_with_no_composer_on_screen(SI, monkeypatch):
    """A recognised version whose capture has no bare-rule pair at all — a
    picker took the composer, or the screen is pure transcript — is `None`,
    the same answer as a failed capture or an old version. Structure, not
    just colour, has to be present."""
    capture = "welcome banner\nsome transcript line\nfooter status\n"
    monkeypatch.setattr(SI.TT, "run", _run_against_capture(capture, "2.1.292"))
    assert SI._read_input_box("%9") is None


def test_read_input_box_logs_unavailable_once_per_process(SI, monkeypatch, caplog):
    """CQ-422's other half: an unreadable box must be visible without a human
    noticing a lost message first. Logged once per process, not once per
    read, so one unreadable build does not flood the log with the same line
    on every poll inside a single `send_message`."""
    monkeypatch.setattr(SI.TT, "run", lambda argv, stdin=None: None)
    with caplog.at_level(logging.WARNING, logger=SI._LOG.name):
        assert SI._read_input_box("%9") is None
        assert SI._read_input_box("%9") is None
        assert SI._read_input_box("%9") is None
    warnings = [r for r in caplog.records if r.name == SI._LOG.name]
    assert len(warnings) == 1


def test_normalize_box_text_tolerates_a_soft_wrap(SI):
    wrapped = "word0 word1 word2\nword3 word4"
    original = "word0 word1 word2 word3 word4"
    assert SI._normalize_box_text(wrapped) == SI._normalize_box_text(original)


def test_poll_attempts_is_derived_from_settle_not_hard_coded(SI):
    """The same knob a caller already controls by passing `settle` to
    `send_message` — no second, uncoordinated one."""
    assert SI._poll_attempts(SI.PASTE_SETTLE_SECONDS) == round(
        SI.PASTE_SETTLE_SECONDS / SI.INPUT_POLL_INTERVAL_SECONDS)
    assert SI._poll_attempts(0) == 1, "never zero attempts"


@pytest.fixture
def box_states(SI, tmux, monkeypatch):
    """Script successive `capture-pane` readings of the input box, so a poll
    can be tested against a box that changes underneath it rather than one
    fixed reading — the shape the real predicates poll against.

    ONE QUEUE FOR THE WHOLE CALL, not one per predicate: `send_message` polls
    the same box twice in sequence (landed, then submitted), and a test
    scripting the box's real trajectory — text arrives, then the box empties
    — reads the same way production does. The version check always matches
    `INPUT_BOX_VERIFIED_FLOOR`; only `capture-pane` is scripted. Once the
    script runs out the last state repeats, since a caller polling past a
    short script should keep seeing whatever is actually still there.
    """
    queue: list[str] = []
    index = {"n": 0}

    def script(*states):
        queue.clear()
        queue.extend(states)
        index["n"] = 0

    def fake_run(argv, stdin=None):
        tmux.append((tuple(argv), stdin))
        if len(argv) > 1 and argv[1] == "display-message":
            return SI.INPUT_BOX_VERIFIED_FLOOR
        if len(argv) > 1 and argv[1] == "capture-pane":
            state = queue[min(index["n"], len(queue) - 1)] if queue else ""
            index["n"] += 1
            return _capture_with_box(state)
        return ""

    monkeypatch.setattr(SI.TT, "run", fake_run)
    monkeypatch.setattr(SI.TT, "claude_pane", lambda name: ("%9", 4242))
    monkeypatch.setattr(SI, "_current_state", lambda name: "idle")
    return script


def test_the_paste_is_verified_by_polling_the_box(SI, tmux, box_states, monkeypatch):
    """Not landed yet, then landed — the reader, not a fixed sleep, is what
    lets `send_message` proceed."""
    slept = []
    monkeypatch.setattr(SI.time, "sleep", lambda s: slept.append(s))
    box_states("", "hello", "")
    SI.send_message("deck", "hello")
    assert slept == [SI.INPUT_POLL_INTERVAL_SECONDS], \
        "one poll interval for the miss, none for the fixed settle sleep"
    assert _keys(tmux) == ["C-u", "Enter"]


def test_a_landed_match_on_the_first_read_needs_no_polling_sleep(SI, tmux, box_states, monkeypatch):
    slept = []
    monkeypatch.setattr(SI.time, "sleep", lambda s: slept.append(s))
    box_states("hello", "")
    SI.send_message("deck", "hello")
    assert slept == [], "a first-read match must not sleep at all"


def test_an_unrecognised_version_falls_back_to_the_old_sleep(SI, tmux, monkeypatch):
    """No `box_states`: the default `tmux` fixture's empty version string
    never matches `INPUT_BOX_VERIFIED_FLOOR`, so this is OBSERVED-ABSENT
    from the very first read — the fixed sleep `send_message` always took,
    not a poll that burns the budget first and then falls back too."""
    slept = []
    monkeypatch.setattr(SI.time, "sleep", lambda s: slept.append(s))
    SI.send_message("deck", "hello")
    assert slept == [SI.PASTE_SETTLE_SECONDS]


def test_the_message_still_sends_even_if_the_paste_never_visibly_lands(SI, tmux, box_states):
    """The bound is a cap on how long to verify, never a refusal to send."""
    attempts = SI._poll_attempts(SI.PASTE_SETTLE_SECONDS)
    box_states(*(["never lands"] * attempts), "")
    SI.send_message("deck", "hello")
    assert _keys(tmux) == ["C-u", "Enter"]


def test_submission_is_confirmed_when_the_box_empties(SI, tmux, box_states):
    box_states("hello", "")
    SI.send_message("deck", "hello")
    assert _keys(tmux).count("Enter") == 1, "no retry needed when it just worked"


def test_the_half_submit_retries_enter_exactly_once(SI, tmux, box_states):
    """THE OBSERVED FAILURE THIS EXISTS FOR. An Enter sent before the paste
    settled submitted nothing; the identical Enter sent later worked. The
    text still sitting there, unchanged, through the whole first bound is
    what makes the retry safe — see the next two tests for the boundary."""
    attempts = SI._poll_attempts(SI.PASTE_SETTLE_SECONDS)
    box_states("hello", *(["hello"] * attempts), "")
    SI.send_message("deck", "hello")
    assert _keys(tmux).count("Enter") == 2


def test_a_message_that_never_submits_raises_after_the_retry(SI, tmux, box_states):
    attempts = SI._poll_attempts(SI.PASTE_SETTLE_SECONDS)
    box_states("hello", *(["hello"] * (2 * attempts)))
    with pytest.raises(SI.InputError, match="not submitted"):
        SI.send_message("deck", "hello")
    assert _keys(tmux).count("Enter") == 2, "one retry, then stop — not a retry loop"


def test_the_retry_is_not_sent_when_something_else_is_in_the_box(SI, tmux, box_states):
    """THE GATE ON THE RETRY. Only the sent text still being there, unchanged,
    proves nothing else took the box in the meantime. Anything else observed
    there is a state this module does not understand well enough to act on —
    the same refusal `answer_choice` makes on `digitBecomesText` — so this
    must raise WITHOUT the extra Enter, which could answer a picker or a
    prompt nobody chose."""
    attempts = SI._poll_attempts(SI.PASTE_SETTLE_SECONDS)
    box_states("hello", *(["something else entirely"] * attempts))
    with pytest.raises(SI.InputError, match="something other than"):
        SI.send_message("deck", "hello")
    assert _keys(tmux).count("Enter") == 1, "no keystroke should answer for the user"


def test_poll_box_content_reports_unavailable_on_a_mid_poll_none(SI, monkeypatch):
    """FIX 1. Reproduces the reviewer's finding directly against the module:
    before the fix, only the FIRST read treated `None` as unavailable — a
    LATER `None` was silently discarded and the stale, last-successfully-
    parsed content returned instead, which is exactly what a picker replacing
    the composer mid-poll produces, since `_read_input_box` returns `None`
    once `_parse_input_box` can no longer find a box-shaped region."""
    calls = {"n": 0}

    def fake_read(pane):
        calls["n"] += 1
        return "hello" if calls["n"] == 1 else None

    monkeypatch.setattr(SI, "_read_input_box", fake_read)
    monkeypatch.setattr(SI.time, "sleep", lambda s: None)
    result = SI._poll_box_content("%9", SI.PASTE_SETTLE_SECONDS)
    assert result is SI._UNAVAILABLE
    assert calls["n"] > 1, "must have polled past the first read to hit this case"


def test_a_picker_replacing_the_box_mid_poll_does_not_retry_on_stale_text(
        SI, tmux, monkeypatch):
    """FIX 1, END TO END. `_read_input_box` returns `None` once a picker takes
    the box, because `picker_render` never draws the bare rule pair
    `_parse_input_box` anchors on. Before the fix, `_poll_box_content` kept
    returning the stale pre-picker text through this gap and `_await_submitted`
    read that as "unchanged, safe to retry" — firing a retried Enter into the
    picker's highlighted row. After the fix, the mid-poll `None` reports
    `_UNAVAILABLE` and the retry is skipped."""
    monkeypatch.setattr(SI.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def fake_run(argv, stdin=None):
        tmux.append((tuple(argv), stdin))
        if len(argv) > 1 and argv[1] == "display-message":
            return SI.INPUT_BOX_VERIFIED_FLOOR
        if len(argv) > 1 and argv[1] == "capture-pane":
            calls["n"] += 1
            if calls["n"] <= 2:
                return _capture_with_box("hello")
            # A picker has taken the box: no rule-bracketed region at all.
            return "picker footer\nsome other picker row\n"
        return ""

    monkeypatch.setattr(SI.TT, "run", fake_run)
    SI.send_message("deck", "hello")
    assert _keys(tmux).count("Enter") == 1, "must not retry into a picker"


def test_the_retry_re_checks_state_and_refuses_a_now_blocked_session(
        SI, tmux, box_states, monkeypatch):
    """FIX 2. The retry used to trust the box's OWN content as proof nothing
    else took it — but a picker can take the box in the capture-to-keystroke
    gap between that read and the retry's own keystroke landing. Every other
    sender in this module re-checks state immediately before a keystroke that
    can land on a live gate (`_check_expectation`); the retry now does too."""
    attempts = SI._poll_attempts(SI.PASTE_SETTLE_SECONDS)
    box_states("hello", *(["hello"] * attempts), "")
    calls = {"n": 0}

    def state(name):
        calls["n"] += 1
        # The first call is `send_message`'s own leading guard; only the
        # SECOND call is the retry's re-check, which is what this test pins.
        return "blocked" if calls["n"] > 1 else "idle"

    monkeypatch.setattr(SI, "_current_state", state)
    with pytest.raises(SI.InputError, match="safe to retry"):
        SI.send_message("deck", "hello")
    assert _keys(tmux).count("Enter") == 1, \
        "the retry must not fire into a session that now reads blocked"


def test_the_retry_refuses_when_the_state_cannot_be_read_at_all(
        SI, tmux, box_states, monkeypatch):
    """THE RETRY REFUSES ON AN UNREADABLE STATE, which is a choice here rather
    than a consequence of the check's shape.

    `_read_state` answers None for every failure, and `None == "blocked"` is
    False — so reading None as "not blocked" would send the keystroke exactly
    as if the state had been confirmed safe. The 2026-07-29 symlink-farm
    incident left a module unreadable for as long as it went unnoticed, which
    is a far wider window than the capture-to-keystroke race.

    `focus_free_text` and `type_text` refuse on None too, but only because
    they demand `state == "blocked"` and reject everything else; refusing
    costs them nothing. This check is the only one that names None while
    letting any other readable value through, so what it protects is an extra
    Enter NOBODY asked for — and a bare Enter is confirmed to take a gate's
    default option, which on the gate observed was "use auto mode".

    See the comment above the `state is None or state == "blocked"` guard in
    `_await_submitted`; this docstring and that comment state the same thing
    and must be corrected together. Naming the guard rather than saying "the
    guard" is deliberate — `session_input.py` calls several other checks
    guards, and this pointer has to survive a reader who does not already
    know which one is meant.
    """
    attempts = SI._poll_attempts(SI.PASTE_SETTLE_SECONDS)
    box_states("hello", *(["hello"] * attempts), "")
    calls = {"n": 0}

    def state(name):
        calls["n"] += 1
        # The leading guard in `send_message` reads a healthy session; the
        # state source only becomes unreadable by the time the retry asks.
        if calls["n"] > 1:
            raise RuntimeError("session_state unavailable")
        return "idle"

    monkeypatch.setattr(SI, "_current_state", state)
    with pytest.raises(SI.InputError, match="unreadable"):
        SI.send_message("deck", "hello")
    assert _keys(tmux).count("Enter") == 1, \
        "an unreadable state must not fall through to the retry keystroke"


# MARK: - Keys


def test_an_unknown_key_is_refused_rather_than_passed_through(SI, tmux):
    """The value becomes a tmux key spec; a caller inventing "C-c C-c" would
    exit the session rather than get an error."""
    for bad in ("C-c C-c", "kill", "q", ""):
        with pytest.raises(SI.InputError):
            SI.send_key("deck", bad)
    assert tmux == []


def test_a_digit_answers_a_prompt_with_no_trailing_enter(SI, tmux):
    """A digit selects and confirms. A following Enter would land in whatever
    replaced the prompt.

    KEYSTROKES RATHER THAN EVERY TMUX CALL, which this asserted until the verb
    gained its `digitBecomesText` guard. That guard reads the pane first, so the
    call list now carries a capture round trip ahead of the digit — a real cost,
    one read per digit, accepted for the same reason `submit_choices` pays it: the
    alternative is sending a keystroke that could be typed into a text field. What
    this test is about is unchanged, and it is the keystrokes.
    """
    SI.answer_choice("deck", 2)
    assert _keys(tmux) == ["2"]


def test_a_choice_outside_the_menu_is_refused(SI, tmux):
    for bad in (0, 10, -1):
        with pytest.raises(SI.InputError):
            SI.answer_choice("deck", bad)
    assert tmux == []


def test_a_digit_is_refused_where_it_would_be_typed_instead_of_selecting(
        SI, tmux, monkeypatch):
    """THE HAZARD THIS CLOSES, and it is the widest of the three in this picker.

    The free-text field takes focus as soon as the highlight reaches it, and a
    digit then lands in it as a character: sending `1` to tick the first option
    produced `4. [✔] 1` on a live 2.1.220 gate. `answer_choice` is the general
    "take an option" verb, so a caller asking for option 1 would record a free-text
    answer nobody wrote, attributed to the user — and unlike the other two hazards
    here, every consumer of `choose` can reach it.

    The keystroke must not go out. A refusal costs a trip to the terminal; an
    answer recorded as the user's cannot be undone from the pane.
    """
    monkeypatch.setattr(SI.picker_render, "read",
                        lambda name: {"digitBecomesText": True})
    with pytest.raises(SI.InputError):
        SI.answer_choice("deck", 1)
    assert _keys(tmux) == [], "the digit was typed into the free-text field"


def test_a_digit_goes_out_where_the_picker_says_it_selects(SI, tmux, monkeypatch):
    """The narrowing, without which this verb would refuse on every picker.

    `commitByButton` is true in three positions and only one is dangerous, so the
    reader reports this one specifically. On an option row — the common case — the
    digit does what it has always done.
    """
    monkeypatch.setattr(SI.picker_render, "read",
                        lambda name: {"commitByButton": True, "multiSelectCommit": "next"})
    SI.answer_choice("deck", 2)
    assert _keys(tmux) == ["2"]


def test_a_blind_reader_still_sends_the_digit(SI, tmux, monkeypatch):
    """THE DEGRADATION, and its direction is the same trade `submit_choices` makes.

    An unrecognised Claude Code version, a failed capture, or a cursor this module
    could not locate all leave the field absent. Refusing there would take the verb
    away on a version bump, which is a worse failure than the one being guarded —
    so absent means send, exactly as every caller did before this field existed.
    """
    for rendered in (None, {}, {"commitByButton": True}):
        tmux.clear()
        monkeypatch.setattr(SI.picker_render, "read", lambda name, r=rendered: r)
        SI.answer_choice("deck", 3)
        assert _keys(tmux) == ["3"]


def test_the_choice_survives_the_reader_module_being_absent(SI, tmux, monkeypatch):
    """The optional import's own failure mode, which `submit_choices` shipped once:
    a module made optional and then called unconditionally raises `AttributeError`
    and the documented fall-back never runs."""
    monkeypatch.setattr(SI, "picker_render", None)
    SI.answer_choice("deck", 4)
    assert _keys(tmux) == ["4"]


def test_an_out_of_range_choice_is_refused_before_the_picker_is_read(
        SI, tmux, monkeypatch):
    """Argument validation costs no round trip to the pane. A caller passing 0 gets
    the same error whether or not a session is up."""
    def boom(name):
        raise AssertionError("the picker was read for a choice that cannot be valid")
    monkeypatch.setattr(SI.picker_render, "read", boom)
    with pytest.raises(SI.InputError):
        SI.answer_choice("deck", 0)


@pytest.fixture
def commit_says(SI, monkeypatch):
    """Pin what the picker reader says the multiselect commit will do.

    `None` is the blind reader — a Claude Code version it has not been checked
    against, or a capture that failed — which is the case every caller had before
    the reader existed.
    """
    def set_commit(value, by_button=False):
        rendered = None if value is None else {"multiSelectCommit": value}
        if by_button:
            rendered = {**(rendered or {}), "commitByButton": True}
        monkeypatch.setattr(SI.picker_render, "read", lambda name: rendered)
    return set_commit


def _keys(calls):
    """Just the keystrokes, dropping the reader's own capture round trip."""
    return [c[0][-1] for c in calls if c[0][:2] == ("tmux", "send-keys")]


def test_submitting_a_multiselect_goes_right_then_takes_the_first_row(
        SI, tmux, monkeypatch, commit_says):
    """THE BUG THIS CLOSES. On a multiselect question a digit only TICKS a box;
    the picker stays up until its review tab is taken. Deck sent one digit and
    waited for a gate that was never going to clear, which is what "the buttons
    do nothing" was.

    Measured 2026-07-24 against Claude Code 2.1.219: `Right` moves from the
    question to its review tab, where `1` is "Submit answers" and `2` is
    "Cancel".

    NOT AN ARROW WALK DOWN TO THE SUBMIT ROW, which the layout invites and which
    does not survive contact: arrows sent in succession are dropped (twelve moved
    the cursor two rows), and `Down` clamps on "Chat about this" rather than on
    Submit. Two keystrokes with a settle between them is the whole drive.

    THE READER BEING BLIND IS THIS CASE. With nothing observed, the old drive and
    the old precondition both stand unchanged, which is what keeps a mini on an
    unrecognised Claude Code version working exactly as it does today.
    """
    monkeypatch.setattr(SI.time, "sleep", lambda s: None)
    commit_says(None)
    SI.submit_choices("deck")
    assert _keys(tmux) == ["Right", "1"]


def test_the_commit_reads_the_picker_before_it_moves_the_cursor(
        SI, tmux, monkeypatch, commit_says):
    """Order, not merely presence. The capture has to happen while the question is
    still on screen: after `Right` the pane is showing something else, and the
    reader would answer about the wrong view.
    """
    monkeypatch.setattr(SI.time, "sleep", lambda s: None)
    reads = []
    monkeypatch.setattr(SI.picker_render, "read",
                        lambda name: reads.append(len(tmux)) or {"multiSelectCommit": "submit"})
    SI.submit_choices("deck")
    assert reads == [0], "the picker was read after a keystroke had already moved it"


def test_a_commit_that_advances_sends_no_digit(SI, tmux, monkeypatch, commit_says):
    """THE FINDING THIS FIXES, and the digit is the whole of it.

    Off any question but the last, `Right` lands on the NEXT QUESTION. The trailing
    `1` then answers that question with its first option, silently — an answer
    nobody chose, attributed to the user. Observed 2026-07-29 against 2.1.220,
    where the TUI's own button reads `Next` in this case and `Submit` in the other.
    """
    monkeypatch.setattr(SI.time, "sleep", lambda s: None)
    commit_says("next")
    SI.submit_choices("deck")
    assert _keys(tmux) == ["Right"], "a digit reached the next question"


def test_the_commit_survives_the_reader_module_being_absent(SI, tmux, monkeypatch):
    """THE GUARD'S OWN FAILURE MODE, raised in review of #312.

    The import is optional so that an unlinked module costs a field rather than the
    verb — and the first version then called `picker_render.read` unconditionally,
    so `None` raised `AttributeError` and the documented fall-back never ran. An
    optional import that is not checked at its call site is not optional.
    """
    monkeypatch.setattr(SI.time, "sleep", lambda s: None)
    monkeypatch.setattr(SI, "picker_render", None)
    SI.submit_choices("deck")
    assert _keys(tmux) == ["Right", "1"], "no reader at all should drive as before"


def test_a_commit_that_submits_still_takes_the_review_row(
        SI, tmux, monkeypatch, commit_says):
    """Off the LAST question `Right` lands on the review tab, where `1` is correct.

    This is the case the old precondition described as "single-question gates
    only". It was over-broad: the constraint was never the number of questions,
    only whether this is the last one, which is why a multi-question gate can now
    be finished from the pane at all.
    """
    monkeypatch.setattr(SI.time, "sleep", lambda s: None)
    commit_says("submit")
    SI.submit_choices("deck")
    assert _keys(tmux) == ["Right", "1"]


def test_a_commit_takes_the_button_where_right_is_inert(
        SI, tmux, monkeypatch, commit_says):
    """THE SILENT NO-OP, found on a live gate rather than here.

    `Right` is inert while the highlight sits in the free-text group: on the row
    it moves the caret inside the text, on the commit button it does nothing at
    all. So this function used to send `Right`, change nothing, and return as
    though it had committed — and Deck's button, shipped in convivy-deck #59,
    appeared broken with no error to explain it.

    `Tab` reaches the button from the row and is a no-op once on it, so one
    keystroke covers both positions without knowing which it is in. Observed
    2026-07-30 against 2.1.220; spec §7.11.
    """
    monkeypatch.setattr(SI.time, "sleep", lambda s: None)
    commit_says("next", by_button=True)
    SI.submit_choices("deck")
    assert _keys(tmux) == ["Tab", "Enter"], "Right would have done nothing here"


def test_the_button_commit_still_takes_the_review_row_on_the_last_question(
        SI, tmux, monkeypatch, commit_says):
    """The button's word governs the trailing digit exactly as `Right`'s landing
    does. `Submit` means `Enter` lands on the review tab, where `1` is correct."""
    monkeypatch.setattr(SI.time, "sleep", lambda s: None)
    commit_says("submit", by_button=True)
    SI.submit_choices("deck")
    assert _keys(tmux) == ["Tab", "Enter", "1"]


def test_the_button_commit_settles_between_the_tab_and_the_enter(
        SI, tmux, monkeypatch, commit_says):
    """An `Enter` sent before the highlight has landed on the button presses
    whatever is still under it, which on the free-text row is the hazard this
    whole path exists to avoid."""
    slept = []
    monkeypatch.setattr(SI.time, "sleep", lambda s: slept.append(s))
    commit_says("next", by_button=True)
    SI.submit_choices("deck")
    assert slept and slept[0] > 0


def test_an_absent_hint_drives_with_right_as_before(
        SI, tmux, monkeypatch, commit_says):
    """The degradation, and the direction matters: absent means `Right`, which is
    what every caller did before this field existed. A blind reader lands here."""
    monkeypatch.setattr(SI.time, "sleep", lambda s: None)
    commit_says("next")
    SI.submit_choices("deck")
    assert _keys(tmux) == ["Right"]


def test_the_submit_settles_before_the_digit(SI, tmux, monkeypatch):
    """A digit sent into a review tab that has not finished drawing is dropped,
    and a dropped digit here leaves the session sitting on a review nobody is
    watching."""
    slept = []
    monkeypatch.setattr(SI.time, "sleep", lambda s: slept.append(s))
    SI.submit_choices("deck")
    assert slept and slept[0] > 0


def test_interrupt_sends_escape(SI, tmux):
    SI.interrupt("deck")
    assert [c[0] for c in tmux] == [("tmux", "send-keys", "-t", "%9", "Escape")]


# MARK: - Walking the highlight onto a multiselect's free-text row


# The rows a multiselect question draws, in screen order. `Chat about this` sits
# BELOW the commit button and is as real a position as any other — a fact the
# spec's §7.10 and §7.11 captures both omit, and one that matters here because
# the walk has to step off it.
_GROUP = ("free_text", "button", "chat")


@pytest.fixture
def picker_at(SI, monkeypatch, tmux, session_is):
    """A picker that MOVES, so the walk is tested against a layout rather than a
    fixed reading.

    A scripted list of `read` results cannot test this verb. The walk is a closed
    loop — it reads, decides, presses, reads again — so a fixture that does not
    respond to the keystrokes would pass any implementation that pressed the right
    NUMBER of arrows, including the counted walk this design exists to avoid.

    So this models the rows and replays the recorded arrows onto them. The moves
    are the ones measured against 2.1.220 on 2026-07-31, and the wrap is the
    load-bearing one: `Up` from the FIRST option lands on the free-text row rather
    than clamping, which is why nothing here may count.
    """
    def place(start, options=3, free_text_row=4, commit="next"):
        session_is("blocked")
        rows = [f"option{i}" for i in range(1, options + 1)] + list(_GROUP)

        def moved():
            at = rows.index(start)
            for call in tmux:
                if call[0][:2] != ("tmux", "send-keys"):
                    continue
                key = call[0][-1]
                row = rows[at]
                if key == "Up":
                    # The wrap. Off the top option the highlight does not stop; it
                    # appears on the free-text row.
                    at = rows.index("free_text") if row == "option1" else at - 1
                elif key == "Down":
                    # Clamped at the bottom. Unmeasured past `chat`, and the walk
                    # never goes there — it stops at the first row of the group.
                    at = min(at + 1, len(rows) - 1)
            return rows[at]

        def read(_name):
            out = {"freeTextRow": free_text_row, "multiSelectCommit": commit}
            if free_text_row is None:
                del out["freeTextRow"]
            if moved() in _GROUP:
                out["commitByButton"] = True
            return out

        monkeypatch.setattr(SI.picker_render, "read", read)
        return moved
    return place


@pytest.mark.parametrize(
    "start", ["option1", "option2", "option3", "free_text", "button", "chat"])
def test_the_walk_lands_on_the_free_text_row_from_anywhere(
        SI, tmux, monkeypatch, picker_at, start):
    """THE WHOLE POINT, and it is stated per starting position because the
    highlight is wherever the last driver left it. Deck does not get to choose
    where this begins."""
    monkeypatch.setattr(SI.time, "sleep", lambda s: None)
    where = picker_at(start)
    SI.focus_free_text("deck")
    assert where() == "free_text"


def test_the_walk_never_sends_a_digit_or_an_enter(
        SI, tmux, monkeypatch, picker_at):
    """THE SAFETY PROPERTY, and it is the reason this verb exists at all.

    On a multiselect a digit TICKS the row it names and `Enter` ticks whatever the
    highlight sits on. Either one recorded against this question is an answer
    nobody chose, which cannot be undone from the pane — the failure convivy-deck
    #60 withdrew the free-text card over. Arrows only.
    """
    monkeypatch.setattr(SI.time, "sleep", lambda s: None)
    picker_at("option2")
    SI.focus_free_text("deck")
    assert set(_keys(tmux)) <= {"Up", "Down"}, "a keystroke here could tick a row"


def test_the_walk_steps_off_the_group_before_walking_down(
        SI, tmux, monkeypatch, picker_at):
    """Starting ON the free-text row still costs two arrows, and that is correct
    rather than wasteful.

    `commitByButton` is true on the row, the button and `Chat about this` alike,
    so it cannot tell which of the three the highlight is on. Stepping up until it
    reads false and coming back down one resolves the ambiguity by moving through
    it. Returning early on a true reading would leave the highlight on the button
    about a third of the time, where a paste goes nowhere.
    """
    monkeypatch.setattr(SI.time, "sleep", lambda s: None)
    where = picker_at("free_text")
    SI.focus_free_text("deck")
    assert _keys(tmux) == ["Up", "Down"]
    assert where() == "free_text"


def test_the_walk_settles_between_every_pair_of_arrows(
        SI, tmux, monkeypatch, picker_at):
    """Arrows in succession are dropped — twelve moved the cursor two rows, which
    is why `submit_choices` refuses to walk at all. The settle is what makes this
    verb possible, so its absence is a test rather than a comment."""
    slept = []
    monkeypatch.setattr(SI.time, "sleep", lambda s: slept.append(s))
    picker_at("chat")
    SI.focus_free_text("deck")
    assert len(slept) == len(_keys(tmux)), "an arrow went out without a settle"
    assert all(s > 0 for s in slept)


def test_the_walk_refuses_without_a_picker_reader(SI, tmux, monkeypatch, session_is):
    """NO FALLBACK HERE, DELIBERATELY, and this is where it differs from
    `submit_choices`.

    That verb falls back to its blind drive because the blind drive was correct
    before the reader existed. This one has no such past: without `commitByButton`
    there is no way to know where the highlight is, and the only blind alternative
    is the counted walk the list's wrap rules out. Refusing leaves Deck withholding
    the card, which is the state it is in today.
    """
    session_is("blocked")
    monkeypatch.setattr(SI, "picker_render", None)
    with pytest.raises(SI.InputError):
        SI.focus_free_text("deck")
    assert _keys(tmux) == [], "arrows went out with nothing watching where they landed"


def test_the_walk_refuses_when_no_free_text_row_is_reported(
        SI, tmux, monkeypatch, picker_at):
    """An absent `freeTextRow` means "not established", never "no such row" — an
    unrecognised Claude Code version drops every field at once. Either reading
    leaves nothing to walk to."""
    monkeypatch.setattr(SI.time, "sleep", lambda s: None)
    picker_at("option1", free_text_row=None)
    with pytest.raises(SI.InputError):
        SI.focus_free_text("deck")
    assert _keys(tmux) == []


def test_the_walk_refuses_on_a_session_that_is_not_waiting(
        SI, tmux, monkeypatch, session_is):
    """`type_text`'s guard, for `type_text`'s reason: with no picker up, arrows
    move a cursor through whatever draft is in the input box."""
    for state in ("idle", "working", "waiting", None):
        session_is(state)
        with pytest.raises(SI.InputError):
            SI.focus_free_text("deck")
    assert _keys(tmux) == []


def test_the_walk_gives_up_rather_than_pressing_arrows_at_a_picker_it_cannot_read(
        SI, tmux, monkeypatch, session_is):
    """A sensor stuck true is a layout this was not measured against. Pressing
    arrows into it until something moves is how a walk ticks a row nobody chose,
    so the bound is small and the failure is loud."""
    monkeypatch.setattr(SI.time, "sleep", lambda s: None)
    session_is("blocked")
    monkeypatch.setattr(SI.picker_render, "read",
                        lambda name: {"freeTextRow": 4, "commitByButton": True})
    with pytest.raises(SI.InputError):
        SI.focus_free_text("deck")
    assert len(_keys(tmux)) <= SI._FREE_TEXT_GROUP_ROWS


# MARK: - The state machine


def _ev(name, ts="2026-07-23T00:00:00+00:00", **payload):
    return {"event": name, "ts": ts, "payload": payload}


def test_events_fold_into_the_expected_states(SS):
    cases = [
        ("SessionStart", "idle"),
        ("UserPromptSubmit", "working"),
        ("PreToolUse", "working"),
        ("Stop", "idle"),
        ("SessionEnd", "ended"),
    ]
    for event, expected in cases:
        assert SS.state_from_events([_ev(event)])["state"] == expected


def test_the_latest_transition_wins(SS):
    got = SS.state_from_events([_ev("UserPromptSubmit"), _ev("Stop")])
    assert got["state"] == "idle"


def test_an_unmodelled_event_leaves_the_state_alone(SS):
    """An event Anthropic adds must be inert, not misleading."""
    got = SS.state_from_events([_ev("Stop"), _ev("SomethingNewInTheNextRelease")])
    assert got["state"] == "idle"


def test_a_permission_request_blocks_and_carries_the_question(SS):
    got = SS.state_from_events([_ev(
        "UserPromptSubmit"),
        _ev("PermissionRequest", tool_name="AskUserQuestion",
            tool_input={"questions": [{"question": "Which?", "header": "Pick"}]}),
    ])
    assert got["state"] == "blocked"
    assert got["gate"]["kind"] == "question"
    assert got["gate"]["questions"][0]["header"] == "Pick"


def test_the_two_gate_classes_do_not_share_a_shape(SS):
    """The sharp edge. A reader that assumes `questions` finds a `command`."""
    got = SS.state_from_events([_ev(
        "PermissionRequest", tool_name="Bash",
        tool_input={"command": "sw_vers", "description": "version"},
        permission_suggestions=[{"type": "addRules"}])])
    gate = got["gate"]
    assert gate["kind"] == "permission"
    assert gate["tool"] == "Bash"
    assert gate["detail"]["command"] == "sw_vers"
    assert gate["suggestions"] == [{"type": "addRules"}]


def test_a_gate_is_cleared_once_the_session_moves_on(SS):
    """A stale question shown as live is worse than no question."""
    got = SS.state_from_events([
        _ev("PermissionRequest", tool_name="AskUserQuestion", tool_input={"questions": []}),
        _ev("Stop"),
    ])
    assert got["state"] == "idle"
    assert got["gate"] is None


def test_a_subagent_finishing_does_not_clear_an_open_gate(SS):
    """Pins the reading issue_01M1X3B448VSEHC06AXDVS5RQQ feared and the measurement
    refuted: `SubagentStop` is not in `_TRANSITIONS`, so the fold skips it and a
    question the main agent is still asking stays blocked with its gate. Deck
    draws the gate as the highest-value state it has; a subagent finishing while
    the operator reads a prompt must not turn that row into `working`."""
    got = SS.state_from_events([
        _ev("PreToolUse", tool_name="Bash", tool_use_id="t1"),
        _ev("PermissionRequest", tool_name="Bash", tool_input={"command": "rm -rf build"}),
        _ev("SubagentStop"),
    ])
    assert got["state"] == "blocked"
    assert got["gate"] is not None
    assert got["last_event"] == "SubagentStop"


def test_a_gate_asked_after_a_stop_and_subagent_stop_is_live(SS):
    """The recorded shape from 2026-09-05/06 (hook-log c39cc46d, b793e58e): a
    `Stop`/`SubagentStop` pair, then the prompt that raises a gate. The two
    `gate: null` reads happened before the prompt's own events had been written;
    once they are in the log the fold reports the gate."""
    got = SS.state_from_events([
        _ev("Stop"),
        _ev("SubagentStop"),
        _ev("UserPromptSubmit"),
        _ev("PermissionRequest", tool_name="Write", tool_input={"file_path": "/tmp/x"}),
    ])
    assert got["state"] == "blocked"
    assert got["gate"]["kind"] == "permission"


def test_a_malformed_record_is_counted_not_fatal(SS):
    """One bad record must not hide the session's state."""
    got = SS.state_from_events([{"nonsense": True}, _ev("Stop")])
    assert got["state"] == "idle"
    assert got["unparsed_records"] == 1


def test_no_events_is_unknown_rather_than_a_guess(SS):
    assert SS.state_from_events([])["state"] == "unknown"


def test_a_gate_payload_that_is_not_a_dict_does_not_raise(SS):
    got = SS.state_from_events([{"event": "PermissionRequest", "ts": None, "payload": "x"}])
    assert got["state"] == "blocked"
    assert got["gate"] is None


# MARK: - Liveness


def test_a_dead_process_outranks_the_last_event(SS, monkeypatch, tmp_path):
    """A session killed mid-response leaves "working" as its final transition
    forever, so the pid has the last word."""
    monkeypatch.setattr(SS.TT, "claude_pane", lambda n: ("%1", 4242))
    monkeypatch.setattr(SS.TT, "meta_for_pid", lambda pid, days=3: {
        "session_id": "aaaa", "cwd": "/x", "transcript_path": str(tmp_path/"t.jsonl")})
    monkeypatch.setattr(SS, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(SS, "_hook_logs", lambda sid, cwd=None, days=3: [])

    got = SS.session_state("deck")
    assert got["state"] == "ended"
    assert got["alive"] is False


def test_a_session_with_no_claude_reports_why(SS, monkeypatch):
    def boom(name):
        raise SS.TT.JoinError("no claude process found in any pane of 'deck'")
    monkeypatch.setattr(SS.TT, "claude_pane", boom)

    got = SS.session_state("deck")
    assert got["state"] == "unknown"
    assert "no claude" in got["reason"]
    assert got["alive"] is False


def test_a_moving_transcript_means_progressing(SS, monkeypatch, tmp_path):
    """The fine-grained signal: it moves while the hook stream is silent."""
    t = _contained_transcript(tmp_path, monkeypatch)
    monkeypatch.setattr(SS.TT, "claude_pane", lambda n: ("%1", 4242))
    monkeypatch.setattr(SS.TT, "meta_for_pid", lambda pid, days=3: {
        "session_id": "aaaa", "cwd": "/x", "transcript_path": str(t)})
    monkeypatch.setattr(SS, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(SS, "_hook_logs", lambda sid, cwd=None, days=3: [])

    got = SS.session_state("deck")
    assert got["progressing"] is True
    assert got["transcript_age"] < SS.PROGRESS_WINDOW_SECONDS


def test_the_absolute_transcript_mtime_is_reported(SS, monkeypatch, tmp_path):
    """THE FIELD A RE-RENDER CAN BE GATED ON.

    `transcript_age` is relative, so it changes on every poll and cannot answer
    "has anything moved". Deck gated on `last_event` instead — an event TYPE
    NAME, not an id — so a whole turn of `PreToolUse` events compared equal and
    the pane never redrew. Observed 2026-07-23: the operator's own messages did not
    appear in the pane they were sent from.
    """
    t = _contained_transcript(tmp_path, monkeypatch)
    monkeypatch.setattr(SS.TT, "claude_pane", lambda n: ("%1", 4242))
    monkeypatch.setattr(SS.TT, "meta_for_pid", lambda pid, days=3: {
        "session_id": "aaaa", "cwd": "/x", "transcript_path": str(t)})
    monkeypatch.setattr(SS, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(SS, "_hook_logs", lambda sid, cwd=None, days=3: [])

    got = SS.session_state("deck")
    assert got["transcript_mtime"] == os.stat(t).st_mtime


def test_the_mtime_is_stable_while_the_transcript_is(SS, monkeypatch, tmp_path):
    """Stable across polls is the whole point: a value that moved every call
    would make the consumer re-render constantly, which is the bug in the other
    direction — it throws away scroll position and expanded rows."""
    t = _contained_transcript(tmp_path, monkeypatch)
    monkeypatch.setattr(SS.TT, "claude_pane", lambda n: ("%1", 4242))
    monkeypatch.setattr(SS.TT, "meta_for_pid", lambda pid, days=3: {
        "session_id": "aaaa", "cwd": "/x", "transcript_path": str(t)})
    monkeypatch.setattr(SS, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(SS, "_hook_logs", lambda sid, cwd=None, days=3: [])

    first = SS.session_state("deck")["transcript_mtime"]
    assert SS.session_state("deck")["transcript_mtime"] == first

    # ...and moves when the conversation does, which is the signal.
    os.utime(t, (first + 5, first + 5))
    assert SS.session_state("deck")["transcript_mtime"] != first


def test_two_appends_in_the_same_millisecond_are_not_one(SS, monkeypatch, tmp_path):
    """The first version rounded to the millisecond, justified as suppressing
    float noise between stats. That noise does not exist — os.stat maps the same
    stored nanoseconds to the same float every time — so the rounding bought
    nothing and cost this: two writes inside a millisecond reading as no change,
    which is the stale-pane bug the field exists to fix."""
    t = _contained_transcript(tmp_path, monkeypatch)
    monkeypatch.setattr(SS.TT, "claude_pane", lambda n: ("%1", 4242))
    monkeypatch.setattr(SS.TT, "meta_for_pid", lambda pid, days=3: {
        "session_id": "aaaa", "cwd": "/x", "transcript_path": str(t)})
    monkeypatch.setattr(SS, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(SS, "_hook_logs", lambda sid, cwd=None, days=3: [])

    base = os.stat(t).st_mtime
    os.utime(t, (base, base))
    first = SS.session_state("deck")["transcript_mtime"]
    os.utime(t, (base + 0.0002, base + 0.0002))     # 200µs later

    assert SS.session_state("deck")["transcript_mtime"] != first


def test_an_mtime_that_moves_backwards_still_reads_as_a_change(SS, monkeypatch, tmp_path):
    """A clock correction or a restore can lower it. A consumer comparing with
    `>` would stall exactly the way the last_event gate did, so the value must
    change — the contract tells consumers to compare with `!=`."""
    t = _contained_transcript(tmp_path, monkeypatch)
    monkeypatch.setattr(SS.TT, "claude_pane", lambda n: ("%1", 4242))
    monkeypatch.setattr(SS.TT, "meta_for_pid", lambda pid, days=3: {
        "session_id": "aaaa", "cwd": "/x", "transcript_path": str(t)})
    monkeypatch.setattr(SS, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(SS, "_hook_logs", lambda sid, cwd=None, days=3: [])

    first = SS.session_state("deck")["transcript_mtime"]
    os.utime(t, (first - 60, first - 60))

    assert SS.session_state("deck")["transcript_mtime"] != first


def test_a_missing_transcript_does_not_raise(SS, monkeypatch):
    monkeypatch.setattr(SS.TT, "claude_pane", lambda n: ("%1", 4242))
    monkeypatch.setattr(SS.TT, "meta_for_pid", lambda pid, days=3: {
        "session_id": "aaaa", "transcript_path": "/nonexistent/x.jsonl"})
    monkeypatch.setattr(SS, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(SS, "_hook_logs", lambda sid, cwd=None, days=3: [])

    got = SS.session_state("deck")
    assert got["progressing"] is None


def test_the_state_object_is_json_serialisable(SS, monkeypatch):
    """It crosses an ssh boundary as JSON; a stray Path would break the pane."""
    monkeypatch.setattr(SS.TT, "claude_pane", lambda n: ("%1", 4242))
    monkeypatch.setattr(SS.TT, "meta_for_pid", lambda pid, days=3: None)
    monkeypatch.setattr(SS, "_pid_alive", lambda pid: True)
    json.dumps(SS.session_state("deck"))


# MARK: - Notification, the bug found by running it against live sessions


def test_an_idle_notification_stops_the_session_reading_as_working(SS):
    """THE BUG. Notification was absent from the transition table, so it left the
    previous transition standing — the live `deck` session reported "working"
    for six and a half hours after nudging its user."""
    got = SS.state_from_events([
        _ev("UserPromptSubmit"),
        _ev("Notification", notification_type="idle_prompt", message="waiting"),
    ])
    assert got["state"] == "waiting"


def test_waiting_is_distinct_from_idle(SS):
    """Both draw a text box, but idle means the turn ended and waiting means the
    session asked for its user and has been sitting there since."""
    assert SS.state_from_events([_ev("Stop")])["state"] == "idle"
    assert SS.state_from_events([
        _ev("Notification", notification_type="idle_prompt")])["state"] == "waiting"


def test_a_permission_notification_blocks(SS):
    got = SS.state_from_events([
        _ev("Notification", notification_type="permission_prompt")])
    assert got["state"] == "blocked"


def test_a_notification_does_not_clear_the_gate_it_pairs_with(SS):
    """A PermissionRequest is followed by its own Notification. Clearing the gate
    on that notification would blank the question the pane is meant to render."""
    got = SS.state_from_events([
        _ev("PermissionRequest", tool_name="Bash", tool_input={"command": "x"}),
        _ev("Notification", notification_type="permission_prompt"),
    ])
    assert got["state"] == "blocked"
    assert got["gate"] is not None and got["gate"]["tool"] == "Bash"


def test_an_unknown_notification_type_reads_as_waiting(SS):
    """True of every notification observed, and it shows a text box rather than
    inventing a gate with no payload behind it."""
    got = SS.state_from_events([
        _ev("UserPromptSubmit"),
        _ev("Notification", notification_type="something_new"),
    ])
    assert got["state"] == "waiting"


# MARK: - The follow loop


def test_the_first_reading_is_always_emitted(SS):
    """Guaranteed only because `last` starts as None rather than a precomputed
    signature. A pane with nothing to draw until the session changes would sit
    blank on a quiet session — which is most of them."""
    got = list(SS.follow_states(
        "deck", 0, sleep=lambda _s: None,
        reader=lambda n: {"state": "idle"}, ticks=3))
    assert len(got) == 1, "one emit up front, none for the unchanged repeats"


def test_only_changes_are_emitted(SS):
    seq = iter([{"state": "idle"}, {"state": "idle"},
                {"state": "working"}, {"state": "working"},
                {"state": "blocked"}])
    got = list(SS.follow_states(
        "deck", 0, sleep=lambda _s: None, reader=lambda n: next(seq), ticks=5))
    assert [g["state"] for g in got] == ["idle", "working", "blocked"]


def test_a_read_failure_becomes_a_state_and_the_follower_survives(SS):
    """A pane whose follower died would freeze on its last good frame and show
    something false. Unknown plus a reason is the honest frame."""
    calls = {"n": 0}

    def flaky(name):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("tmux went away")
        return {"state": "idle"}

    got = list(SS.follow_states(
        "deck", 0, sleep=lambda _s: None, reader=flaky, ticks=3))
    assert [g["state"] for g in got] == ["idle", "unknown", "idle"]
    assert "tmux went away" in got[1]["reason"]
    # The error frame is hand-built too, so it needs the same seeding the other
    # two paths do. Nothing breaks today — `bin/session-agent` only serializes
    # what this yields — but the contract promises these fields are always
    # present, and a follower reading them on an error tick would find they are
    # not. Third instance of the same shape problem, found by the reviewer
    # asking whether the class of bug survived rather than the instance.
    assert got[1]["agents"] == []
    assert got[1]["agents_fleet_wide"] is False


def test_ages_alone_do_not_count_as_a_change(SS):
    """Every field that ticks with the clock would make each poll look like a
    transition."""
    seq = iter([
        {"state": "idle", "last_event_age": 1, "transcript_age": 1},
        {"state": "idle", "last_event_age": 2, "transcript_age": 2},
    ])
    got = list(SS.follow_states(
        "deck", 0, sleep=lambda _s: None, reader=lambda n: next(seq), ticks=2))
    assert len(got) == 1


def test_progressing_only_counts_while_working(SS):
    """An idle session's transcript inevitably ages past the window. Emitting on
    that tells the consumer nothing it did not know — the session is idle either
    way."""
    idle = [{"state": "idle", "progressing": True}, {"state": "idle", "progressing": False}]
    seq = iter(idle)
    got = list(SS.follow_states(
        "deck", 0, sleep=lambda _s: None, reader=lambda n: next(seq), ticks=2))
    assert len(got) == 1, "no emit for an idle session going quiet"

    work = [{"state": "working", "progressing": True}, {"state": "working", "progressing": False}]
    seq = iter(work)
    got = list(SS.follow_states(
        "deck", 0, sleep=lambda _s: None, reader=lambda n: next(seq), ticks=2))
    assert len(got) == 2, "a working session going quiet IS worth reporting"


def test_a_gate_change_is_a_change(SS):
    """Two different questions must not read as the same blocked state."""
    seq = iter([
        {"state": "blocked", "gate": {"kind": "question", "tool": "AskUserQuestion"}},
        {"state": "blocked", "gate": {"kind": "permission", "tool": "Bash"}},
    ])
    got = list(SS.follow_states(
        "deck", 0, sleep=lambda _s: None, reader=lambda n: next(seq), ticks=2))
    assert len(got) == 2


# MARK: - Orphan exit (ARCH: 101 orphaned `--follow` processes found 2026-08-12)


def test_the_loop_runs_to_the_tick_budget_while_never_orphaned(SS):
    seq = iter([{"state": "idle"}, {"state": "working"}, {"state": "blocked"}])
    got = list(SS.follow_states(
        "deck", 0, sleep=lambda _s: None, reader=lambda n: next(seq),
        orphaned=lambda: False, ticks=3))
    assert [g["state"] for g in got] == ["idle", "working", "blocked"]


def test_the_loop_exits_immediately_once_already_orphaned(SS):
    """No read happens at all — an orphan discovered before the first tick
    must not touch the session on its way out."""
    calls = {"n": 0}

    def reader(name):
        calls["n"] += 1
        return {"state": "idle"}

    got = list(SS.follow_states(
        "deck", 0, sleep=lambda _s: None, reader=reader,
        orphaned=lambda: True, ticks=5))

    assert got == []
    assert calls["n"] == 0


def test_the_orphan_check_runs_every_tick_not_once_at_start(SS):
    """A follower is orphaned mid-life — Deck quits hours into a quiet session —
    not at birth. A check made once up front would never catch that."""
    orphan_calls = {"n": 0}
    flips_true_on_call = 4

    def orphaned():
        orphan_calls["n"] += 1
        return orphan_calls["n"] >= flips_true_on_call

    seq = iter([{"state": "idle"}, {"state": "working"}, {"state": "blocked"},
                {"state": "waiting"}, {"state": "idle"}])
    got = list(SS.follow_states(
        "deck", 0, sleep=lambda _s: None, reader=lambda n: next(seq),
        orphaned=orphaned, ticks=10))

    assert [g["state"] for g in got] == ["idle", "working", "blocked"]
    assert orphan_calls["n"] == flips_true_on_call, \
        "stopped as soon as the check flipped, not after the full ticks budget"


def test_omitting_orphaned_falls_back_to_the_real_process_check(SS, monkeypatch):
    """The default seam is a real check of this process's parent, proven by
    making that check say True and watching the loop exit before reading."""
    monkeypatch.setattr(os, "getppid", lambda: 1)
    calls = {"n": 0}

    def reader(name):
        calls["n"] += 1
        return {"state": "idle"}

    got = list(SS.follow_states("deck", 0, sleep=lambda _s: None, reader=reader, ticks=5))

    assert got == []
    assert calls["n"] == 0


# MARK: - Bracketed-paste escape


def test_a_paste_end_marker_in_the_message_is_refused(SI, tmux):
    """THE ATTACK. `paste-buffer -p` wraps the buffer in bracketed-paste markers
    but does not sanitise inside them, so an embedded ESC[201~ ends the paste
    early and everything after it arrives as KEYSTROKES — a slash command runs,
    a digit answers a permission prompt."""
    payload = "looks harmless\x1b[201~/release-notes\n"
    with pytest.raises(SI.InputError, match="control character"):
        SI.send_message("deck", payload)
    assert tmux == [], "not one keystroke should reach the session"


def test_every_control_character_is_refused(SI, tmux):
    """ESC is the exploitable one, but the guard is the whole C0/C1 range: a
    lone CR can rewrite a line, and NUL truncates."""
    for bad in ("\x1b", "\x00", "\x07", "\x1f", "\x7f", "\x9b", "\r"):
        with pytest.raises(SI.InputError):
            SI.send_message("deck", f"before{bad}after")
    assert tmux == []


def test_newlines_and_tabs_still_pass(SI, tmux):
    """A multiline message is the normal case and must survive the guard."""
    SI.send_message("deck", "first line\n\tindented second line\n")
    load = next(c for c in tmux if c[0][1] == "load-buffer")
    assert load[1] == "first line\n\tindented second line\n"


def test_the_guard_runs_before_the_pane_is_resolved(SI, monkeypatch):
    """A rejected message should cost no tmux calls at all, not even the lookup
    that finds the pane."""
    called = []
    monkeypatch.setattr(SI.TT, "claude_pane", lambda n: called.append(n) or ("%9", 1))
    monkeypatch.setattr(SI.TT, "run", lambda *a, **k: called.append(a) or "")
    with pytest.raises(SI.InputError):
        SI.send_message("deck", "x\x1b[201~y")
    assert called == []


# MARK: - The UTC day-key, and reading across a boundary


def _write_log(root, day, sid, events):
    d = root / day
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sid}.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")


def test_the_hook_log_is_found_by_utc_not_local_date(SS, monkeypatch, tmp_path):
    """THE BUG. hook-log dirs are UTC-keyed (session-event-log.py line 203) while
    co-* run dirs are LOCAL (line 85). Searching this tree by local date returned
    yesterday's file all evening in a negative-offset zone, and every session
    reported a state up to a day old — confidently, with no error anywhere."""
    import datetime as dt
    monkeypatch.setattr(SS, "HOOK_LOG_ROOT", tmp_path)
    utc_today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    _write_log(tmp_path, utc_today, "aaa", [{"event": "Stop", "ts": None}])

    assert SS._hook_logs("aaa"), "the UTC-dated file must be found"
    assert SS._hook_logs("aaa")[0].parent.name == utc_today


def test_events_are_read_across_the_day_boundary(SS, monkeypatch, tmp_path):
    """Just after UTC midnight today's file holds a couple of events, none of
    them a transition. Reading only the newest reported `unknown` for a session
    that was plainly running."""
    import datetime as dt
    monkeypatch.setattr(SS, "HOOK_LOG_ROOT", tmp_path)
    today = dt.datetime.now(dt.timezone.utc).date()
    yesterday = (today - dt.timedelta(days=1)).isoformat()

    _write_log(tmp_path, yesterday, "aaa", [{"event": "UserPromptSubmit", "ts": None}])
    # Today's file carries only a non-transition event.
    _write_log(tmp_path, today.isoformat(), "aaa", [{"event": "SubagentStop", "ts": None}])

    logs = SS._hook_logs("aaa")
    assert len(logs) == 2
    events = []
    for p in reversed(logs):
        events.extend(SS._last_events(p))
    assert SS.state_from_events(events)["state"] == "working", \
        "yesterday's transition must still stand when today's file is thin"


def test_the_newest_day_wins_for_ordering(SS, monkeypatch, tmp_path):
    """Oldest first, so a later transition overrides an earlier one."""
    import datetime as dt
    monkeypatch.setattr(SS, "HOOK_LOG_ROOT", tmp_path)
    today = dt.datetime.now(dt.timezone.utc).date()
    yesterday = (today - dt.timedelta(days=1)).isoformat()

    _write_log(tmp_path, yesterday, "aaa", [
        {"event": "PermissionRequest", "ts": None,
         "payload": {"tool_name": "AskUserQuestion", "tool_input": {"questions": []}}}])
    _write_log(tmp_path, today.isoformat(), "aaa", [{"event": "Stop", "ts": None}])

    events = []
    for p in reversed(SS._hook_logs("aaa")):
        events.extend(SS._last_events(p))
    got = SS.state_from_events(events)
    assert got["state"] == "idle", "a stale gate must not survive today's Stop"
    assert got["gate"] is None


def test_the_lookup_reads_the_utc_day_key_on_any_host(SS, monkeypatch, tmp_path):
    """The revert-proof version of the UTC test.

    Writing and reading at UTC-today cannot discriminate on a UTC host, where
    local and UTC dates are equal — which is most CI runners, so the original
    test passed just as happily against the bug. Pinning the seam instead: if
    the lookup goes back to `date.today()` this patch stops affecting it and the
    sentinel directory is never searched, on every host."""
    import datetime as dt
    monkeypatch.setattr(SS, "HOOK_LOG_ROOT", tmp_path)
    sentinel = dt.date(2030, 5, 17)          # today by no clock, anywhere
    monkeypatch.setattr(SS, "_utc_today", lambda: sentinel)
    _write_log(tmp_path, sentinel.isoformat(), "aaa", [{"event": "Stop", "ts": None}])

    found = SS._hook_logs("aaa")
    assert found and found[0].parent.name == sentinel.isoformat()


def test_a_dead_process_still_outranks_a_working_event(SS, monkeypatch, tmp_path):
    """Restores the invariant the dead monkeypatch had silently stopped
    exercising: a session killed mid-response leaves `working` as its last
    event, and the pid must override it."""
    import datetime as dt
    monkeypatch.setattr(SS, "HOOK_LOG_ROOT", tmp_path)
    utc_today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    _write_log(tmp_path, utc_today, "aaa", [{"event": "UserPromptSubmit", "ts": None}])

    monkeypatch.setattr(SS.TT, "claude_pane", lambda n: ("%1", 4242))
    monkeypatch.setattr(SS.TT, "meta_for_pid", lambda pid, days=3: {
        "session_id": "aaa", "transcript_path": str(tmp_path / "gone.jsonl")})
    monkeypatch.setattr(SS, "_pid_alive", lambda pid: False)

    got = SS.session_state("deck")
    assert got["last_event"] == "UserPromptSubmit", "the event really was working"
    assert got["state"] == "ended", "and the dead pid overrides it"


# MARK: - Private-crew hook-log routing (agent-platform#421 / issue_01M0GVMBXCX10F0R3ZAGHGEKFZ)
#
# THE BUG, reproduced end to end. `session-event-log.py` routes a private
# crew's hook log to `<crew home>/hook-log/`, not the shared `HOOK_LOG_ROOT`.
# `session_state()` kept searching only the shared root, so a healthy CFO
# session read `state: unknown` — not because nothing was running, but because
# the reader was looking in a directory the writer had stopped using three
# days earlier. Deck's answer to `unknown` is a disabled composer.
#
# These tests override this file's `_no_private_crews` autouse fixture with a
# real crew-aware `private_home_for`, since that is exactly the thing under
# test here.


def _private_crew_aware(monkeypatch, SS, crew_home):
    """Route `cwd` under `crew_home` as private, everything else as not — the
    same shape `private_crew.private_home_for` gives the real writer, without
    touching the real machine's registry."""
    def fake(cwd):
        if not isinstance(cwd, str) or not cwd:
            return SS.UNKNOWN
        try:
            return crew_home if Path(cwd).resolve().is_relative_to(crew_home.resolve()) else None
        except (OSError, ValueError):
            return SS.UNKNOWN
    monkeypatch.setattr(SS, "private_home_for", fake)


def test_a_private_crews_hook_log_is_found_via_its_own_home(SS, monkeypatch, tmp_path):
    """THE FIX, pinned directly: a session whose `cwd` is inside a private
    crew's home must read that crew's OWN hook-log root, not the shared one."""
    import datetime as dt
    shared = tmp_path / "shared-hook-log"
    crew_home = tmp_path / "CFO"
    crew_home.mkdir()
    monkeypatch.setattr(SS, "HOOK_LOG_ROOT", shared)
    _private_crew_aware(monkeypatch, SS, crew_home)

    utc_today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    _write_log(crew_home / "hook-log", utc_today, "aaa", [{"event": "UserPromptSubmit", "ts": None}])

    monkeypatch.setattr(SS.TT, "claude_pane", lambda n: ("%1", 4242))
    monkeypatch.setattr(SS.TT, "meta_for_pid", lambda pid, days=3: {
        "session_id": "aaa", "cwd": str(crew_home), "transcript_path": None})
    monkeypatch.setattr(SS, "_pid_alive", lambda pid: True)

    got = SS.session_state("cfo")
    assert got["last_event"] == "UserPromptSubmit", \
        "the private crew's own hook-log root must be searched"
    assert got["reason"] is None


def test_a_private_crews_session_does_not_fall_back_to_the_shared_root(SS, monkeypatch, tmp_path):
    """Precision, not just presence: a stray same-named file in the SHARED root
    must not be read for a session that belongs to a private crew — the two
    roots are alternatives, not a search order."""
    import datetime as dt
    shared = tmp_path / "shared-hook-log"
    crew_home = tmp_path / "CFO"
    crew_home.mkdir()
    monkeypatch.setattr(SS, "HOOK_LOG_ROOT", shared)
    _private_crew_aware(monkeypatch, SS, crew_home)

    utc_today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    # A decoy in the shared root, for the SAME session id, with a DIFFERENT event.
    _write_log(shared, utc_today, "aaa", [{"event": "Stop", "ts": None}])

    monkeypatch.setattr(SS.TT, "claude_pane", lambda n: ("%1", 4242))
    monkeypatch.setattr(SS.TT, "meta_for_pid", lambda pid, days=3: {
        "session_id": "aaa", "cwd": str(crew_home), "transcript_path": None})
    monkeypatch.setattr(SS, "_pid_alive", lambda pid: True)

    got = SS.session_state("cfo")
    assert got["reason"] == "no hook log for this session", \
        "the crew's own root has nothing, and the shared decoy must not be read instead"


def test_an_ordinary_crew_still_reads_the_shared_root(SS, monkeypatch, tmp_path):
    """Registering a private crew must not stop an ordinary session's reads —
    the routing adds a destination, it does not remove the default one."""
    import datetime as dt
    shared = tmp_path / "shared-hook-log"
    crew_home = tmp_path / "CFO"
    crew_home.mkdir()
    monkeypatch.setattr(SS, "HOOK_LOG_ROOT", shared)
    _private_crew_aware(monkeypatch, SS, crew_home)

    utc_today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    _write_log(shared, utc_today, "aaa", [{"event": "UserPromptSubmit", "ts": None}])

    monkeypatch.setattr(SS.TT, "claude_pane", lambda n: ("%1", 4242))
    monkeypatch.setattr(SS.TT, "meta_for_pid", lambda pid, days=3: {
        "session_id": "aaa", "cwd": str(tmp_path / "vivy"), "transcript_path": None})
    monkeypatch.setattr(SS, "_pid_alive", lambda pid: True)

    got = SS.session_state("vivy")
    assert got["last_event"] == "UserPromptSubmit"


def test_undeterminable_privacy_reports_unknown_reason_rather_than_guessing(SS, monkeypatch, tmp_path):
    """When privacy cannot be determined, the reader must say so distinctly —
    not silently read the shared root and report a generic 'no hook log',
    which is indistinguishable from a session that genuinely has none. This is
    the reader's fail-closed direction, the opposite of the writer's: the
    writer still writes SOMEWHERE on `UNKNOWN` (ADR-0043), the reader refuses
    to guess a root instead."""
    shared = tmp_path / "shared-hook-log"
    monkeypatch.setattr(SS, "HOOK_LOG_ROOT", shared)
    monkeypatch.setattr(SS, "private_home_for", lambda cwd: SS.UNKNOWN)

    monkeypatch.setattr(SS.TT, "claude_pane", lambda n: ("%1", 4242))
    monkeypatch.setattr(SS.TT, "meta_for_pid", lambda pid, days=3: {
        "session_id": "aaa", "cwd": "/some/path", "transcript_path": None})
    monkeypatch.setattr(SS, "_pid_alive", lambda pid: True)

    got = SS.session_state("mystery")
    assert got["state"] == "unknown"
    assert got["reason"] == "cannot determine whether this session's crew is private"


# MARK: - The rail's batched brief state


def test_states_returns_a_brief_entry_per_name_keyed(SS, monkeypatch):
    """The rail draws every pinned session and needs each one's liveness in one
    ssh round trip. Each entry is brief — no vitals — because a row draws none of
    the status area, and a name that fails still gets an entry rather than
    vanishing from the rail."""
    def fake(name, days=3, brief=False, table=None):
        assert brief is True, "the rail's batch must ask for brief state"
        if name == "boom":
            raise RuntimeError("no such session")
        return {"session": name, "state": "working", "alive": True}
    monkeypatch.setattr(SS, "session_state", fake)
    monkeypatch.setattr(SS.TT, "session_table", lambda: {})

    got = SS.session_states(["vivy", "boom", "deck"])
    assert set(got) == {"vivy", "boom", "deck"}
    assert got["vivy"]["state"] == "working" and got["vivy"]["alive"] is True
    assert "vitals" not in got["vivy"]
    assert got["boom"]["state"] == "unknown" and got["boom"]["alive"] is False


# The two `if not brief` guards session_state carries: the early return when a
# session has no run-dir record, and the normal path when it does. Both must be
# covered, or deleting one would let a brief read pay for vitals unnoticed —
# raised by reviewer, whose first version tested only the early return.
@pytest.mark.parametrize("has_meta", [False, True], ids=["no-run-dir", "normal-path"])
def test_brief_state_skips_the_vitals_block(SS, monkeypatch, has_meta):
    """A brief read is for the rail, which shows no vitals, so it must not pay the
    per-session file reads vitals costs. The full pane read still carries them —
    on both the early-return and the normal path."""
    monkeypatch.setattr(SS.TT, "claude_pane", lambda n: ("%1", 4242))
    monkeypatch.setattr(SS.TT, "meta_for_pid",
                        lambda pid, days=3: {"session_id": "abc"} if has_meta else None)
    monkeypatch.setattr(SS.TT, "contained_transcript", lambda p: None)
    monkeypatch.setattr(SS, "_hook_logs", lambda sid, cwd=None, days=3: [])
    monkeypatch.setattr(SS, "_pid_alive", lambda pid: True)
    called = {"vitals": 0}
    real = SS.vitals
    monkeypatch.setattr(SS, "vitals", lambda *a, **k: called.__setitem__("vitals", called["vitals"] + 1) or real(*a, **k))

    SS.session_state("deck", brief=True)
    assert called["vitals"] == 0, "brief must not assemble vitals"
    SS.session_state("deck", brief=False)
    assert called["vitals"] == 1, "the full read still does"


# MARK: - A gate the transcript has outrun


def test_a_gate_is_resolved_once_the_transcript_outruns_it(SS):
    """Answering an AskUserQuestion emits NO hook event, so the stream reports
    blocked from the moment a question is asked until the turn ends — an hour,
    sometimes. Observed live: steward and svc both read blocked on questions
    already answered, transcripts 19 and 68 minutes past their gates."""
    import datetime as dt
    gate = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=20)
    later = (gate + dt.timedelta(minutes=19)).timestamp()
    assert SS.gate_is_stale(gate.isoformat(), later) is True


def test_a_live_gate_is_not_cleared_by_the_ask_itself(SS):
    """The transcript is written when the question is ASKED, so a second or two
    of margin is the question landing, not an answer. A blocked session writes
    nothing further, so its transcript cannot outrun its gate."""
    import datetime as dt
    gate = dt.datetime.now(dt.timezone.utc)
    assert SS.gate_is_stale(gate.isoformat(), gate.timestamp() + 2) is False
    assert SS.gate_is_stale(gate.isoformat(), gate.timestamp()) is False


def test_gate_staleness_degrades_rather_than_raising(SS):
    assert SS.gate_is_stale(None, 123.0) is False
    assert SS.gate_is_stale("not-a-timestamp", 123.0) is False
    assert SS.gate_is_stale("2026-07-23T00:00:00+00:00", None) is False


# MARK: - A gate is judged by the MAIN agent's writes, not the file mtime


def _transcript(tmp_path, *records):
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return str(p)


def _ask(ts):
    return {"type": "assistant", "isSidechain": False, "timestamp": ts,
            "message": {"content": [{"type": "tool_use", "name": "AskUserQuestion"}]}}


def test_newest_main_agent_ts_ignores_control_and_subagent_writes(SS, tmp_path):
    """THE BUG THIS CLOSES. A blocked session's transcript still advances — a
    queued message, an attachment, a subagent finishing — and none of that is the
    main agent answering. selector's picker vanished from the pane because the
    FILE mtime moved on exactly these records. Only a non-sidechain, non-meta
    conversational record counts, so the newest one here is the question itself."""
    import datetime as dt
    path = _transcript(
        tmp_path,
        _ask("2026-07-24T19:32:06+00:00"),
        {"type": "queue-operation", "timestamp": "2026-07-24T19:34:57+00:00"},
        {"type": "attachment", "timestamp": "2026-07-24T19:34:57+00:00"},
        {"type": "assistant", "isSidechain": True, "timestamp": "2026-07-24T19:40:00+00:00",
         "message": {"content": [{"type": "text", "text": "subagent work"}]}},
        {"type": "user", "isMeta": True, "timestamp": "2026-07-24T19:45:00+00:00",
         "message": {"content": [{"type": "text", "text": "injected context"}]}},
    )
    ts = SS.newest_main_agent_ts(path)
    assert dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%H:%M:%S") == "19:32:06"


def test_a_gate_stays_open_while_only_control_and_subagent_records_follow(SS, tmp_path):
    """The selector incident, 2026-07-24: the gate at 19:32 was not answered until
    19:57, and everything written in between was control-plane or subagent. Judged
    by the file mtime the picker vanished at 19:35; judged by the main agent's own
    writes it stays open the whole 25 minutes."""
    gate = "2026-07-24T19:32:06+00:00"
    path = _transcript(
        tmp_path,
        _ask(gate),
        {"type": "queue-operation", "timestamp": "2026-07-24T19:34:57+00:00"},
        {"type": "assistant", "isSidechain": True, "timestamp": "2026-07-24T19:50:00+00:00",
         "message": {"content": [{"type": "text", "text": "sub"}]}},
    )
    assert SS.gate_is_stale(gate, SS.newest_main_agent_ts(path)) is False


def test_a_gate_clears_once_the_main_agent_writes_the_answer(SS, tmp_path):
    """The answer is the main agent's own tool_result, written after the gate.
    Past the margin, that is unambiguous progress and the gate resolves — which is
    what this whole mechanism exists to do."""
    gate = "2026-07-24T19:32:06+00:00"
    path = _transcript(
        tmp_path,
        _ask(gate),
        {"type": "user", "toolUseResult": {}, "timestamp": "2026-07-24T19:57:05+00:00",
         "message": {"content": [{"type": "tool_result"}]}},
    )
    assert SS.gate_is_stale(gate, SS.newest_main_agent_ts(path)) is True


def test_a_tail_with_no_main_agent_record_keeps_the_gate(SS, tmp_path):
    """None is 'could not tell', and the safe reading is to keep the gate: a
    picker shown after it was answered self-heals on the turn's Stop, one hidden
    while open hangs the session. Also the degradation path for an unreadable
    file."""
    path = _transcript(
        tmp_path,
        {"type": "queue-operation", "timestamp": "2026-07-24T19:34:57+00:00"},
        {"type": "attachment", "timestamp": "2026-07-24T19:35:00+00:00"},
    )
    assert SS.newest_main_agent_ts(path) is None
    assert SS.newest_main_agent_ts(str(tmp_path / "does-not-exist.jsonl")) is None
    assert SS.gate_is_stale("2026-07-24T19:32:06+00:00", None) is False


def test_a_resolved_gate_flips_the_session_back_to_working(SS, monkeypatch, tmp_path):
    """End to end: the fold says blocked, the MAIN AGENT'S OWN WRITE says otherwise.

    The answer is a conversational record after the gate, not just a fresher file
    mtime — a bare mtime bump is a queued message or a subagent, and must not
    resolve the gate. That distinction is the selector fix; see the sibling below.
    """
    import datetime as dt
    utc_today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    gate_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=20)
    monkeypatch.setattr(SS, "HOOK_LOG_ROOT", tmp_path)
    _write_log(tmp_path, utc_today, "aaa", [{
        "event": "PermissionRequest", "ts": gate_at.isoformat(),
        "payload": {"tool_name": "AskUserQuestion", "tool_input": {"questions": []}}}])

    transcript = _contained_transcript(tmp_path, monkeypatch)
    answered_at = (gate_at + dt.timedelta(minutes=19)).isoformat()
    transcript.write_text(json.dumps({
        "type": "user", "toolUseResult": {}, "timestamp": answered_at,
        "message": {"content": [{"type": "tool_result"}]}}) + "\n")
    monkeypatch.setattr(SS.TT, "claude_pane", lambda n: ("%1", 4242))
    monkeypatch.setattr(SS.TT, "meta_for_pid", lambda pid, days=3: {
        "session_id": "aaa", "transcript_path": str(transcript)})
    monkeypatch.setattr(SS, "_pid_alive", lambda pid: True)

    got = SS.session_state("deck")
    assert got["state"] == "working"
    assert got["gate"] is None
    assert got["gate_resolved"] is True


def test_a_live_gate_survives_a_transcript_that_only_control_records_touch(SS, monkeypatch, tmp_path):
    """THE SELECTOR INCIDENT, END TO END (2026-07-24). A session blocked on an
    AskUserQuestion had control-plane records — a queued message, an attachment —
    written minutes after its gate. The file mtime moved, so the old logic cleared
    the gate and the picker vanished from the pane before the operator could answer it. The
    gate must survive, because the MAIN AGENT wrote nothing after it."""
    import datetime as dt
    utc_today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    gate_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=20)
    monkeypatch.setattr(SS, "HOOK_LOG_ROOT", tmp_path)
    _write_log(tmp_path, utc_today, "aaa", [{
        "event": "PermissionRequest", "ts": gate_at.isoformat(),
        "payload": {"tool_name": "AskUserQuestion",
                    "tool_input": {"questions": [{"question": "which?", "options": []}]}}}])

    transcript = _contained_transcript(tmp_path, monkeypatch)
    # The gate itself, then only bookkeeping after it — never a main-agent turn.
    touched_at = (gate_at + dt.timedelta(minutes=3)).isoformat()
    transcript.write_text("\n".join(json.dumps(r) for r in [
        {"type": "assistant", "timestamp": gate_at.isoformat(),
         "message": {"content": [{"type": "tool_use", "name": "AskUserQuestion"}]}},
        {"type": "queue-operation", "timestamp": touched_at},
        {"type": "attachment", "timestamp": touched_at},
    ]) + "\n")
    monkeypatch.setattr(SS.TT, "claude_pane", lambda n: ("%1", 4242))
    monkeypatch.setattr(SS.TT, "meta_for_pid", lambda pid, days=3: {
        "session_id": "aaa", "transcript_path": str(transcript)})
    monkeypatch.setattr(SS, "_pid_alive", lambda pid: True)

    got = SS.session_state("deck")
    assert got["state"] == "blocked", "a live picker must not vanish on a bookkeeping write"
    assert got["gate"] is not None
    assert got.get("gate_resolved") is not True


# MARK: - The guards this module owes its sibling


def test_a_forged_session_id_cannot_walk_out_of_the_day_dirs(SS, monkeypatch, tmp_path):
    """The id becomes a path component here. tmux_transcript guards the same
    field before a glob; validated in one sibling and not the other is not a
    guard."""
    monkeypatch.setattr(SS, "HOOK_LOG_ROOT", tmp_path)
    for bad in ("../../etc/passwd", "..", "a/b", "*", "", None, 42):
        assert SS._hook_logs(bad) == []


def test_a_forged_transcript_path_cannot_suppress_a_gate(SS, monkeypatch, tmp_path):
    """THE ATTACK. A session that forges its own meta.json could point
    transcript_path at a file whose mtime it controls, and a question it is
    actively waiting on would vanish from the pane. Containment is why it
    cannot."""
    import datetime as dt
    utc_today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    gate_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=20)
    monkeypatch.setattr(SS, "HOOK_LOG_ROOT", tmp_path)
    _write_log(tmp_path, utc_today, "aaa", [{
        "event": "PermissionRequest", "ts": gate_at.isoformat(),
        "payload": {"tool_name": "AskUserQuestion", "tool_input": {"questions": []}}}])

    # Freshly written, outside ~/.claude/projects — exactly the forgery.
    planted = tmp_path / "attacker.jsonl"
    planted.write_text("{}")
    monkeypatch.setattr(SS.TT, "claude_pane", lambda n: ("%1", 4242))
    monkeypatch.setattr(SS.TT, "meta_for_pid", lambda pid, days=3: {
        "session_id": "aaa", "transcript_path": str(planted)})
    monkeypatch.setattr(SS, "_pid_alive", lambda pid: True)

    got = SS.session_state("deck")
    assert got["state"] == "blocked", "the gate must survive an uncontained path"
    assert got.get("gate_resolved") is not True
    assert got["transcript_age"] is None, "and it must not be stat'd at all"



# MARK: - Vitals: the terminal's status area, for a frame that is not the terminal


def test_remote_control_is_read_from_the_bridge_id_not_argv(SS, monkeypatch, tmp_path):
    """WHY NOT THE FLAG. `co` passes `--remote-control` unconditionally, so argv
    says "on" for every co session — including the ones where the operator has lately had
    to turn it on by hand, which is the case the operator asked to see. The bridge id in
    Claude Code's own per-pid record is the same id the `/rc` link in the status
    row points at, so it moves when the state moves.

    Measured 2026-07-24 against 2.1.219: an interactive session launched by `co`
    carries `bridgeSessionId`, a headless `-p` run does not.
    """
    monkeypatch.setattr(SS, "CLAUDE_SESSIONS_DIR", tmp_path)
    (tmp_path / "111.json").write_text(json.dumps(
        {"pid": 111, "bridgeSessionId": "session_01ABC", "name": "convivy-deck-e2"}))
    (tmp_path / "222.json").write_text(json.dumps({"pid": 222, "name": "headless"}))

    assert SS.remote_control(111) == {"on": True, "name": "convivy-deck-e2"}
    assert SS.remote_control(222) == {"on": False, "name": "headless"}


def test_unreadable_remote_control_is_unknown_rather_than_off(SS, monkeypatch, tmp_path):
    """"Claude Code wrote no record we could parse" is not the same answer as
    "this session is not bridged", and a frame that showed the second would be
    lying. One release can take the file away; it must cost a badge, not the
    truth."""
    monkeypatch.setattr(SS, "CLAUDE_SESSIONS_DIR", tmp_path)
    (tmp_path / "333.json").write_text("{not json")

    for pid in (333, 999, None, 0):
        assert SS.remote_control(pid)["on"] is None, pid


def test_agents_in_flight_are_scoped_to_this_products_repo(SS, monkeypatch, tmp_path):
    """Unscoped, every product's co reported another product's coder as its own —
    constance reading "coder in flight" when the coder was sous-chef's (the operator
    2026-07-05). The steward works across repos and keeps the fleet-wide view."""
    active = tmp_path / "active.json"
    active.write_text(json.dumps({"runs": [
        {"pid": 1, "repo": "convivy-deck", "agent": "deck-reviewer"},
        {"pid": 2, "repo": "sous-chef", "agent": "coder"},
    ]}))
    monkeypatch.setattr(SS, "ACTIVE_JSON", active)
    monkeypatch.setattr(SS, "_pid_alive", lambda pid: True)

    mine = SS.live_agents("/srv/convivy/convivy-deck")
    # Asserted key by key rather than as whole-dict equality, because this
    # payload is additive by contract: a new field is allowed and an equality
    # assertion turns every legitimate addition into a failing test about
    # something the test does not care about. `agent_runs` has its own tests.
    assert mine["agents"] == ["deck-reviewer"]
    assert mine["fleet_wide"] is False

    steward = SS.live_agents("/srv/convivy/substrate")
    assert steward["fleet_wide"] is True
    assert sorted(steward["agents"]) == ["coder", "deck-reviewer"]


def test_a_run_whose_process_is_gone_is_not_in_flight(SS, monkeypatch, tmp_path):
    """A record can outlive its process, and a count that included those would
    report a review wave that finished hours ago."""
    active = tmp_path / "active.json"
    active.write_text(json.dumps({"runs": [
        {"pid": 1, "repo": "convivy-deck", "agent": "alive"},
        {"pid": 2, "repo": "convivy-deck", "agent": "dead"},
    ]}))
    monkeypatch.setattr(SS, "ACTIVE_JSON", active)
    monkeypatch.setattr(SS, "_pid_alive", lambda pid: int(pid) == 1)

    assert SS.live_agents("/srv/convivy/convivy-deck")["agents"] == ["alive"]


def test_a_malformed_run_record_cannot_take_down_the_state_read(SS, monkeypatch, tmp_path):
    """`os.kill(None, 0)` raises TypeError, and this reader is fed whatever was
    written to active.json. Uncaught, one bad record would blank the pane over a
    field it does not even display."""
    active = tmp_path / "active.json"
    active.write_text(json.dumps({"runs": [
        {"repo": "convivy-deck", "agent": "no-pid"},
        {"pid": "nonsense", "repo": "convivy-deck", "agent": "bad-pid"},
        "not-even-a-dict",
    ]}))
    monkeypatch.setattr(SS, "ACTIVE_JSON", active)

    assert SS.live_agents("/srv/convivy/convivy-deck")["agents"] == []


def test_brief_carries_live_agents_but_still_skips_vitals(SS, monkeypatch, tmp_path):
    """THE FIELD THE RAIL NEEDS, AND THE ONE IT DOES NOT.

    A co at its prompt with a reviewer running behind it and a co at its prompt
    with nothing running are both `idle`, and the rail draws them differently:
    one is live and open to you, the other is done. Nothing else in the brief
    state separates them.

    The vitals block stays behind `brief` in the same breath, because that is
    the cost the rail refuses — a per-session health file and a bridge record,
    for a status area a row does not draw.
    """
    active = tmp_path / "active.json"
    active.write_text(json.dumps({"runs": [
        {"pid": 1, "repo": "convivy-deck", "agent": "deck-reviewer"},
    ]}))
    monkeypatch.setattr(SS, "ACTIVE_JSON", active)
    monkeypatch.setattr(SS.TT, "claude_pane", lambda n: ("%1", 4242))
    monkeypatch.setattr(SS.TT, "meta_for_pid", lambda pid, days=3: {
        "session_id": "aaaa", "cwd": "/srv/convivy/convivy-deck",
        "transcript_path": str(tmp_path / "t.jsonl")})
    monkeypatch.setattr(SS, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(SS, "_hook_logs", lambda sid, cwd=None, days=3: [])

    brief = SS.session_state("deck", brief=True)
    assert brief["agents"] == ["deck-reviewer"]
    assert brief["agents_fleet_wide"] is False
    assert "vitals" not in brief

    full = SS.session_state("deck")
    assert full["agents"] == ["deck-reviewer"], "the full state answers the same"
    assert "vitals" in full


def test_a_session_with_nothing_dispatched_reports_an_empty_list(SS, monkeypatch, tmp_path):
    """Empty rather than absent, so a consumer can tell "nothing running" from
    "this mini predates the field" — the second degrades to a quiet row, the
    first IS the quiet row."""
    monkeypatch.setattr(SS, "ACTIVE_JSON", tmp_path / "none.json")
    monkeypatch.setattr(SS.TT, "claude_pane", lambda n: ("%1", 4242))
    monkeypatch.setattr(SS.TT, "meta_for_pid", lambda pid, days=3: {
        "session_id": "aaaa", "cwd": "/srv/convivy/convivy-deck",
        "transcript_path": str(tmp_path / "t.jsonl")})
    monkeypatch.setattr(SS, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(SS, "_hook_logs", lambda sid, cwd=None, days=3: [])

    assert SS.session_state("deck", brief=True)["agents"] == []


@pytest.mark.parametrize("brief", [True, False])
def test_every_early_return_still_carries_an_agents_key(SS, monkeypatch, tmp_path, brief):
    """ABSENT IS NOT EMPTY, and every path out of here has to know it.

    The field was filled in after the `meta is None` guard, so a session with no
    run-dir record emitted a state object with no `agents` key at all — and a
    consumer reading `state["agents"]` on it gets a KeyError rather than either
    answer. Caught in review.

    Empty rather than fleet-wide on these paths, deliberately: scoping needs a
    `cwd` and these are exactly the sessions whose `cwd` could not be
    established, so the unscoped answer would be the bug scoping fixed —
    one product's co reporting another product's coder as its own.
    """
    active = tmp_path / "active.json"
    active.write_text(json.dumps({"runs": [
        {"pid": 1, "repo": "sous-chef", "agent": "coder"},
    ]}))
    monkeypatch.setattr(SS, "ACTIVE_JSON", active)
    monkeypatch.setattr(SS, "_pid_alive", lambda pid: True)

    # No run-dir record for the pid.
    monkeypatch.setattr(SS.TT, "claude_pane", lambda n: ("%1", 4242))
    monkeypatch.setattr(SS.TT, "meta_for_pid", lambda pid, days=3: None)
    no_meta = SS.session_state("deck", brief=brief)
    assert no_meta["agents"] == []
    assert no_meta["agents_fleet_wide"] is False

    # No claude process at all — the earliest return of the lot.
    def boom(name):
        raise SS.TT.JoinError("no claude process found in any pane of 'deck'")
    monkeypatch.setattr(SS.TT, "claude_pane", boom)
    no_session = SS.session_state("deck", brief=brief)
    assert no_session["agents"] == []
    assert no_session["agents_fleet_wide"] is False


def test_the_batchs_hand_built_failure_entry_carries_agents_too(SS, monkeypatch):
    """THE PATH THE SEEDING DOES NOT REACH, and the rail is its caller.

    `session_states` builds the failure entry itself rather than getting one
    from `session_state`, so a field added to that function's seed lands
    everywhere except here. `agents` was absent on this path — against the rail,
    the one consumer the always-present guarantee exists for.
    """
    def boom(name, days=3, brief=False, table=None):
        raise RuntimeError("tmux is not running")
    monkeypatch.setattr(SS, "session_state", boom)
    monkeypatch.setattr(SS.TT, "session_table", lambda: {})

    entry = SS.session_states(["deck"])["deck"]
    assert entry["state"] == "unknown"
    assert entry["reason"] == "tmux is not running"
    assert entry["agents"] == []
    assert entry["agents_fleet_wide"] is False


def test_the_status_lines_reading_is_quoted_rather_than_recomputed(SS, monkeypatch, tmp_path):
    """The context numbers and the model name come from the payload Claude Code
    hands the status-line command INSIDE the session, and nowhere else. Deriving
    them again here would give a second answer to a question that already has
    one, and the first disagreement would make both suspect."""
    monkeypatch.setattr(SS, "VITALS_DIR", tmp_path)
    monkeypatch.setattr(SS, "CLAUDE_SESSIONS_DIR", tmp_path / "none")
    monkeypatch.setattr(SS, "ACTIVE_JSON", tmp_path / "none.json")
    (tmp_path / "vitals-abc.json").write_text(json.dumps({
        "written_at": 1_000_000_000, "model": "Opus 4.8 (1M context)",
        "ctx_tokens": 481_000, "ctx_window": 1_000_000, "ctx_pct": 48.1,
        "tier": "soft", "sub": "convivy (operator)"}))

    got = SS.vitals("abc", None, None)
    assert got["model"] == "Opus 4.8 (1M context)"
    assert (got["ctx_tokens"], got["ctx_window"], got["tier"]) == (481_000, 1_000_000, "soft")
    assert got["sub"] == "convivy (operator)"
    assert got["age"] > 0 and got["stale"] is True, "a reading that old is not now"


def test_the_worked_on_pr_passes_through_the_reader(SS, monkeypatch, tmp_path):
    """The PR is recorded by the status line from Claude Code's own payload and
    read back here, the same quoted-not-recomputed path as the context numbers.
    Copied only when it is an object, so a malformed reading leaves it None."""
    monkeypatch.setattr(SS, "VITALS_DIR", tmp_path)
    monkeypatch.setattr(SS, "CLAUDE_SESSIONS_DIR", tmp_path / "none")
    monkeypatch.setattr(SS, "ACTIVE_JSON", tmp_path / "none.json")
    (tmp_path / "vitals-abc.json").write_text(json.dumps({
        "written_at": 1_000_000_000,
        "pr": {"number": 33, "url": "http://x/33", "review_state": "approved"}}))
    assert SS.vitals("abc", None, None)["pr"] == {
        "number": 33, "url": "http://x/33", "review_state": "approved"}

    (tmp_path / "vitals-bad.json").write_text(json.dumps({
        "written_at": 1_000_000_000, "pr": "not an object"}))
    assert SS.vitals("bad", None, None)["pr"] is None


def test_no_reading_yet_is_empty_rather_than_zero(SS, monkeypatch, tmp_path):
    """A session that has not taken a turn since this shipped has no reading. A
    pane showing `ctx 0/? (0%)` would be reporting an empty context rather than
    an absent measurement."""
    monkeypatch.setattr(SS, "VITALS_DIR", tmp_path)
    monkeypatch.setattr(SS, "CLAUDE_SESSIONS_DIR", tmp_path / "none")
    monkeypatch.setattr(SS, "ACTIVE_JSON", tmp_path / "none.json")

    got = SS.vitals("never-ran", None, None)
    assert got["ctx_tokens"] is None and got["ctx_pct"] is None
    assert got["model"] is None and got["tier"] is None
    # The two that resolve without a reading still answer.
    assert got["agents"] == [] and "remote_control" in got


def test_a_session_id_cannot_escape_the_vitals_directory(SS, monkeypatch, tmp_path):
    """The id reaches this from a run-dir record, and it becomes a filename."""
    monkeypatch.setattr(SS, "VITALS_DIR", tmp_path / "cache")
    monkeypatch.setattr(SS, "CLAUDE_SESSIONS_DIR", tmp_path / "none")
    monkeypatch.setattr(SS, "ACTIVE_JSON", tmp_path / "none.json")
    (tmp_path / "cache").mkdir()
    (tmp_path / "secret.json").write_text(json.dumps({"model": "leaked"}))

    for hostile in ("../secret", "../../etc/passwd", "/absolute", ""):
        assert SS.vitals(hostile, None, None)["model"] is None, hostile


# --- `ctx_pct` and `rollover`: top-level, carried in brief too (ADR-0069) -----

def test_ctx_pct_quotes_the_same_reading_vitals_uses(SS, monkeypatch, tmp_path):
    monkeypatch.setattr(SS, "VITALS_DIR", tmp_path)
    (tmp_path / "vitals-abc.json").write_text(json.dumps({
        "written_at": 1_000_000_000, "ctx_pct": 48.1}))
    assert SS.ctx_pct("abc") == 48.1


def test_ctx_pct_is_none_with_no_reading_yet(SS, monkeypatch, tmp_path):
    monkeypatch.setattr(SS, "VITALS_DIR", tmp_path)
    assert SS.ctx_pct("never-ran") is None


def test_ctx_pct_does_not_reinvent_staleness(SS, monkeypatch, tmp_path):
    """Unlike `rollover_policy.read_ctx_pct`, this quotes the field
    unconditionally, the same way `vitals()` does for its own `ctx_pct` — an
    old reading is still the honest last-known number, and staleness is what
    `vitals()`'s own `age`/`stale` pair exists to report."""
    monkeypatch.setattr(SS, "VITALS_DIR", tmp_path)
    (tmp_path / "vitals-old.json").write_text(json.dumps({
        "written_at": 1_000_000_000, "ctx_pct": 91.0}))
    assert SS.ctx_pct("old") == 91.0


def test_rollover_reads_the_hooks_state_file(SS, monkeypatch, tmp_path):
    monkeypatch.setattr(SS, "VITALS_DIR", tmp_path)
    (tmp_path / "rollover-abc.json").write_text(json.dumps({
        "state": "seeking", "reason": "threshold", "since": 1_000_000_000,
        "calls_seeking": 4, "cap": 40,
        "policy": {"mode": "enforce", "seek_pct": 70, "floor_pct": None, "seek_call_cap": 40}}))
    got = SS.rollover("abc")
    assert got["state"] == "seeking"
    assert got["calls_seeking"] == 4
    assert got["cap"] == 40
    assert got["policy"]["seek_pct"] == 70


def test_rollover_is_the_honest_absence_with_no_hook_firing_yet(SS, monkeypatch, tmp_path):
    monkeypatch.setattr(SS, "VITALS_DIR", tmp_path)
    got = SS.rollover("never-fired")
    assert got == {"state": "none", "reason": None, "since": None,
                  "calls_seeking": None, "cap": None, "policy": None,
                  "vetoed_until": None,
                  "predecessor": None, "successor": None, "successor_window": None,
                  # Slice 6's park fields, None outside a park. Spelled out here
                  # rather than loosened to a subset check: this literal is the
                  # payload contract Deck decodes, and it caught this very change
                  # adding three keys to it. A test that stopped pinning the exact
                  # shape would stop reporting the next one.
                  "issue_id": None, "issue_status": None, "issue_product": None,
                  # And it reported the next one, as promised: `permission_mode`
                  # is transport here, not part of what this payload MEANS — the
                  # contract surface is `session_state`'s top-level field of the
                  # same name. It sits in the overlay default because the copy
                  # loop drops anything absent from it.
                  "permission_mode": None}


def test_rollover_translates_the_evaluators_ok_to_the_closed_sets_none(SS, monkeypatch, tmp_path):
    """Deck's `rollover.state` is a closed set that does not include `ok` —
    `rollover_policy.evaluate()`'s own name for the below-threshold zone — so
    this is the one place that has to rename it before it reaches the states
    output. `seeking` and `roll_now` already match the closed set and must
    pass through unchanged."""
    monkeypatch.setattr(SS, "VITALS_DIR", tmp_path)
    (tmp_path / "rollover-abc.json").write_text(json.dumps({
        "state": "ok", "reason": None, "since": None,
        "calls_seeking": 0, "cap": None, "policy": {"mode": "observe"}}))
    assert SS.rollover("abc")["state"] == "none"

    (tmp_path / "rollover-def.json").write_text(json.dumps({"state": "seeking"}))
    assert SS.rollover("def")["state"] == "seeking"

    (tmp_path / "rollover-ghi.json").write_text(json.dumps({"state": "roll_now"}))
    assert SS.rollover("ghi")["state"] == "roll_now"


def test_rollover_carries_the_rolling_handoff_fields(SS, monkeypatch, tmp_path):
    """`rolling` already matches Deck's closed set and passes through
    unchanged, like `seeking`/`roll_now`. `successor_window` is what lets Deck
    draw a rollover as one event rather than an unexplained second window
    (epic comment 2399)."""
    monkeypatch.setattr(SS, "VITALS_DIR", tmp_path)
    (tmp_path / "rollover-abc.json").write_text(json.dumps({
        "state": "rolling", "reason": "rollover", "since": 1_000_000_000,
        "predecessor": {"session_id": "abc", "pane_id": "%1"},
        "successor": {"session_id": None, "pane_id": "%2"},
        "successor_window": 2}))
    got = SS.rollover("abc")
    assert got["state"] == "rolling"
    assert got["successor_window"] == 2
    assert got["successor"] == {"session_id": None, "pane_id": "%2"}
    assert got["predecessor"] == {"session_id": "abc", "pane_id": "%1"}


def test_rollover_carries_the_parked_issue_fields(SS, monkeypatch, tmp_path):
    """DECK'S RAIL WAS GUESSING WITHOUT THESE. It said "an issue is waiting on
    you" for every parked session, because the contract promised one and this
    payload carried nothing that could contradict it — `issue_id` was never in
    `_ROLLOVER_DEFAULT` either, so the rail could not have named the item even
    when filing succeeded.

    `rollover` copies only keys present in `_ROLLOVER_DEFAULT`, the same
    overlay `rollover_policy.read_state` does against its own default. So a
    field can be written by `mark_parked`, persisted, read back correctly by
    the policy module, and still be invisible here. This asserts the whole
    triple crosses that boundary."""
    monkeypatch.setattr(SS, "VITALS_DIR", tmp_path)
    (tmp_path / "rollover-abc.json").write_text(json.dumps({
        "state": "parked", "reason": "verify_up_failed", "since": 1_000_000_000,
        "issue_id": None, "issue_status": "error:400", "issue_product": "deck-rollover-probe"}))
    got = SS.rollover("abc")
    assert got["state"] == "parked"
    # Each is load-bearing on its own: the status is the only thing that says
    # whether an item exists, and `issue_product` is the only thing that says
    # what string the producer submitted when filing — not necessarily the
    # bucket the item landed in, for which see
    # `rollover_policy.DEFAULT_STATE["issue_product"]`. An id alone answers
    # neither.
    assert got["issue_status"] == "error:400"
    assert got["issue_product"] == "deck-rollover-probe"
    assert got["issue_id"] is None


def test_rollover_carries_a_successful_filings_issue_fields_too(SS, monkeypatch, tmp_path):
    """The success case crosses the same boundary, so a rail can render the
    filing's id and status rather than only the failure. A payload that carried
    the fields only when filing failed would leave a successful park looking
    exactly like an old one."""
    monkeypatch.setattr(SS, "VITALS_DIR", tmp_path)
    (tmp_path / "rollover-xyz.json").write_text(json.dumps({
        "state": "parked", "reason": "spawn_error", "since": 1_000_000_000,
        "issue_id": "issue_ABC", "issue_status": "created",
        "issue_product": "agent-platform"}))
    got = SS.rollover("xyz")
    assert got["issue_id"] == "issue_ABC"
    assert got["issue_status"] == "created"
    assert got["issue_product"] == "agent-platform"


# Every key the evaluator persists that `_ROLLOVER_DEFAULT` deliberately does
# NOT carry to Deck, with the reason it is withheld. This is the allowlist half
# of the parity guard below: a key belongs here only when someone decided it
# should not cross, never because the overlay happens not to have it.
_DELIBERATELY_NOT_IN_THE_PAYLOAD = {
    # Internal to the evaluator's own arithmetic — Deck renders the zone, not
    # the counters that produced it.
    "roll_now_reissues": "re-issue counter, evaluator-internal",
    "seeking_stop_reissues": "re-issue counter, evaluator-internal",
    # What was REALLY true before a veto painted over it. `rollover.state` is
    # what Deck draws; the natural pair exists so the evaluator can ask what it
    # decided last call.
    "natural_state": "pre-veto truth, evaluator-internal",
    "natural_reason": "pre-veto truth, evaluator-internal",
    # A hook-side decision about whether to block `Stop`, not session state.
    "stop_blocked": "hook control flow, not a rail field",
    # Withheld on a judgment `_ROLLOVER_DEFAULT`'s own comment records: putting
    # a filesystem path into a UI payload is a separate decision from carrying
    # the park's issue fields, which ARE carried.
    "handoff_path": "filesystem path, deliberately not in a UI payload",
}


def test_every_evaluator_field_is_carried_or_deliberately_withheld(SS):
    """THE PARITY GUARD, and it exists because pinning one key was not enough.

    `rollover()` copies only keys present in `_ROLLOVER_DEFAULT`, so a field
    added to `rollover_policy.DEFAULT_STATE` and not here is written to disk,
    persisted, read back by the policy module, and invisible to Deck — with
    nothing raising and nothing going red. `_ROLLOVER_DEFAULT`'s own comment
    counted three instances of that before `permission_mode` became the fourth
    (PR #629), so this is four seen occurrences rather than a hazard somebody
    imagined.

    The sibling test pins `permission_mode` specifically. It cannot catch the
    FIFTH field, which is the one this catches: a new key must be either in the
    payload or in the allowlist above, and a `None` entry in the allowlist is
    not accepted — the author has to write down why it is withheld.

    Reviewer on #629 asked for exactly this, arguing the earlier fix closed the
    instance and not the class. It does not fix the overlay's silent-intersection
    shape, which is the structural problem; it makes the next omission fail here
    instead of reaching Deck."""
    persisted = set(SS.RP.DEFAULT_STATE)
    carried = set(SS._ROLLOVER_DEFAULT)
    withheld = set(_DELIBERATELY_NOT_IN_THE_PAYLOAD)

    unexplained = persisted - carried - withheld
    assert not unexplained, (
        "these keys are persisted by `rollover_policy.evaluate` and silently "
        f"dropped by `_ROLLOVER_DEFAULT`'s copy loop: {sorted(unexplained)}. "
        "Add each to `_ROLLOVER_DEFAULT` so Deck receives it, or to "
        "`_DELIBERATELY_NOT_IN_THE_PAYLOAD` with the reason it is withheld.")

    # The allowlist must not outlive what it describes: a key carried to Deck,
    # or dropped from the evaluator entirely, has to leave the list, or the list
    # slowly becomes a record of decisions nobody holds any more.
    stale = withheld - persisted
    assert not stale, (
        f"allowlisted but no longer persisted by the evaluator: {sorted(stale)}")
    contradictory = withheld & carried
    assert not contradictory, (
        f"allowlisted as withheld while actually carried: {sorted(contradictory)}")
    assert all(_DELIBERATELY_NOT_IN_THE_PAYLOAD.values()), (
        "every withheld key needs a written reason, not an empty one")


def test_rollover_carries_permission_mode_through_the_overlay(SS, monkeypatch, tmp_path):
    """THE FIELD IS DROPPED UNLESS IT IS IN `_ROLLOVER_DEFAULT`, and this test
    exists because that is exactly what happened: the trigger wrote
    `permission_mode`, the policy module persisted it, `session_state` lifted
    it to the top level, and Deck read `None` forever.

    WHY IT SURVIVED EVERY OTHER CHECK. Nothing raised, nothing went red, and
    the lifted field WAS present at the top level — it was simply always
    `None`. An unpopulated field and a dropped one produce identical evidence,
    so a test asserting presence, or a live read of a session no hook had yet
    reported for, passes in both worlds. Only a NON-DEFAULT value written to
    disk and read back out separates them, which is why this test writes
    `plan` rather than checking the key exists.

    `_ROLLOVER_DEFAULT`'s own comment describes this trap and counts three
    prior instances of it; this was the fourth."""
    monkeypatch.setattr(SS, "VITALS_DIR", tmp_path)
    (tmp_path / "rollover-abc.json").write_text(json.dumps({
        "state": "ok", "permission_mode": "plan"}))
    assert SS.rollover("abc")["permission_mode"] == "plan"


def test_brief_lifts_permission_mode_to_the_top_level(SS, monkeypatch, tmp_path):
    """The states poll is the rail's mode, and this field exists for the rail —
    a session idle IN PLAN MODE is waiting on its operator while every other
    field here says it is idle. So it rides at the top level under `brief`,
    and the value must be the one on disk rather than the default.

    The top level is the contract surface deliberately: a consumer reading
    `rollover.permission_mode` would fairly conclude it described the rollover
    rather than the session."""
    monkeypatch.setattr(SS, "VITALS_DIR", tmp_path)
    monkeypatch.setattr(SS, "ACTIVE_JSON", tmp_path / "none.json")
    monkeypatch.setattr(SS.TT, "claude_pane", lambda n: ("%1", 4242))
    monkeypatch.setattr(SS.TT, "meta_for_pid", lambda pid, days=3: {
        "session_id": "abc", "cwd": "/srv/convivy/convivy-deck",
        "transcript_path": str(tmp_path / "t.jsonl")})
    monkeypatch.setattr(SS, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(SS, "_hook_logs", lambda sid, cwd=None, days=3: [])
    (tmp_path / "rollover-abc.json").write_text(json.dumps({
        "state": "ok", "permission_mode": "plan"}))

    for brief in (True, False):
        got = SS.session_state("deck", brief=brief)
        assert got["permission_mode"] == "plan", f"brief={brief}"


def test_a_session_id_cannot_escape_the_rollover_directory(SS, monkeypatch, tmp_path):
    monkeypatch.setattr(SS, "VITALS_DIR", tmp_path / "cache")
    (tmp_path / "cache").mkdir()
    (tmp_path / "secret.json").write_text(json.dumps({"state": "leaked"}))
    for hostile in ("../secret", "../../etc/passwd", "/absolute", ""):
        assert SS.rollover(hostile)["state"] == "none", hostile


def test_brief_carries_ctx_pct_and_rollover(SS, monkeypatch, tmp_path):
    """The rail wants a context number and a rollover zone, so both ride at the
    top level regardless of `brief` — unlike the rest of the status area."""
    monkeypatch.setattr(SS, "VITALS_DIR", tmp_path)
    monkeypatch.setattr(SS, "ACTIVE_JSON", tmp_path / "none.json")
    monkeypatch.setattr(SS.TT, "claude_pane", lambda n: ("%1", 4242))
    monkeypatch.setattr(SS.TT, "meta_for_pid", lambda pid, days=3: {
        "session_id": "abc", "cwd": "/srv/convivy/convivy-deck",
        "transcript_path": str(tmp_path / "t.jsonl")})
    monkeypatch.setattr(SS, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(SS, "_hook_logs", lambda sid, cwd=None, days=3: [])
    (tmp_path / "vitals-abc.json").write_text(json.dumps({
        "written_at": 1_000_000_000, "ctx_pct": 73.0}))
    (tmp_path / "rollover-abc.json").write_text(json.dumps({
        "state": "seeking", "calls_seeking": 2, "cap": None,
        "policy": {"mode": "enforce"}}))

    brief = SS.session_state("deck", brief=True)
    assert brief["ctx_pct"] == 73.0
    assert brief["rollover"]["state"] == "seeking"
    assert "vitals" not in brief, "the rest of the status area still stays behind brief"

    full = SS.session_state("deck")
    assert full["ctx_pct"] == 73.0
    assert full["rollover"]["state"] == "seeking"


@pytest.mark.parametrize("brief", [True, False])
def test_every_early_return_still_carries_ctx_pct_and_rollover(SS, monkeypatch, tmp_path, brief):
    monkeypatch.setattr(SS, "VITALS_DIR", tmp_path)
    monkeypatch.setattr(SS.TT, "claude_pane", lambda n: ("%1", 4242))
    monkeypatch.setattr(SS.TT, "meta_for_pid", lambda pid, days=3: None)
    no_meta = SS.session_state("deck", brief=brief)
    assert no_meta["ctx_pct"] is None
    assert no_meta["rollover"] == {"state": "none", "reason": None, "since": None,
                                   "calls_seeking": None, "cap": None, "policy": None,
                                   "vetoed_until": None,
                                   "predecessor": None, "successor": None,
                                   "successor_window": None,
                                   "issue_id": None, "issue_status": None,
                                   "issue_product": None,
                                   "permission_mode": None}
    # SEPARATE FROM THE DICT ABOVE, because the top-level field is the contract
    # surface and its presence inside `rollover` is only transport. A consumer
    # reads THIS one, and it is missing rather than `None` when the seed above
    # `session_state`'s early returns omits it.
    assert "permission_mode" in no_meta, (
        "the no-run-dir early return drops the key `session_states` seeds on "
        "its own unresolvable path — same poll, same consumer, KeyError")
    assert no_meta["permission_mode"] is None

    def boom(name):
        raise SS.TT.JoinError("no claude process found in any pane of 'deck'")
    monkeypatch.setattr(SS.TT, "claude_pane", boom)
    no_session = SS.session_state("deck", brief=brief)
    assert no_session["ctx_pct"] is None
    assert no_session["rollover"]["state"] == "none"
    assert "permission_mode" in no_session, "the join-failure early return too"
    assert no_session["permission_mode"] is None


def test_the_batchs_hand_built_failure_entry_carries_ctx_pct_and_rollover_too(SS, monkeypatch):
    """The same trap `agents` and `working_on` were once caught in:
    `session_states` hand-builds its failure entry rather than getting one
    from `session_state`, so a field added to the latter's seed reaches every
    path except this one unless it is added here too."""
    def boom(name, days=3, brief=False, table=None):
        raise RuntimeError("tmux is not running")
    monkeypatch.setattr(SS, "session_state", boom)
    monkeypatch.setattr(SS.TT, "session_table", lambda: {})

    entry = SS.session_states(["deck"])["deck"]
    assert entry["ctx_pct"] is None
    assert entry["rollover"]["state"] == "none"
    assert "permission_mode" in entry
    assert entry["permission_mode"] is None


# --- `asks_open`/`asks_answered` (CQ-358, platform/decisions/0091) ----------
#
# The counts are keyed by the tmux NAME alone (`crew_key.session_key`), the
# same split `working_on` draws, tested directly against `SS.slate_counts`
# here and then for its wiring into `session_state`/`session_states` below —
# this file's own `_isolated_slate_home` autouse fixture (top of file) keeps
# every one of these off the real `~/.local/state/convivy/slate/`.


def test_slate_counts_is_zero_and_zero_with_nothing_posted(SS):
    assert SS.slate_counts("deck") == {"asks_open": 0, "asks_answered": 0}


def test_slate_counts_splits_open_from_answered(SS):
    open_id = SS.SLT.post_ask("deck", "open?")
    answered_id = SS.SLT.post_ask("deck", "answered?")
    SS.SLT.answer_ask("deck", answered_id, "yes")
    resolved_id = SS.SLT.post_ask("deck", "resolved?")
    SS.SLT.resolve_ask("deck", resolved_id, "done")

    assert SS.slate_counts("deck") == {"asks_open": 1, "asks_answered": 1}
    assert open_id  # the ids themselves are not what's under test here


def test_slate_counts_on_a_corrupt_board_reports_slate_error_rather_than_raising(SS):
    """The rail's poll must never fail on a bad board — `ask_counts`'s own
    `SlateCorruptError` is caught here and turned into 0/0 plus `slate_error`."""
    path = SS.SLT.board_path("deck")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"{ not json at all")

    got = SS.slate_counts("deck")

    assert got["asks_open"] == 0
    assert got["asks_answered"] == 0
    assert "slate_error" in got


def _board_as_a_directory(path):
    path.parent.mkdir(parents=True)
    path.mkdir()


def _board_permission_denied(path):
    path.parent.mkdir(parents=True)
    path.write_bytes(b"{}")
    os.chmod(path, 0o000)


def _board_over_the_byte_ceiling(path):
    """`tests/test_slate.py` pins the exact ceiling (`SLT._BOARD_MAX_BYTES`)
    against `slate.ask_counts` directly; this one only needs a board bigger
    than any ceiling that module could reasonably hold, so it does not need
    the loaded module the `SS` fixture hands to the test function."""
    path.parent.mkdir(parents=True)
    oversized = json.dumps({"version": 1, "session": "deck",
                             "cards": [{"pad": "x" * 2_000_000}]})
    path.write_bytes(oversized.encode())


def _board_a_nesting_bomb_under_the_ceiling(path):
    path.parent.mkdir(parents=True)
    path.write_bytes(b"[" * 5_000)


def _board_a_fifo_with_no_writer(path):
    path.parent.mkdir(parents=True)
    os.mkfifo(path)


@pytest.mark.parametrize("label, make_board", [
    ("a directory", _board_as_a_directory),
    ("permission denied", _board_permission_denied),
    ("over the byte ceiling", _board_over_the_byte_ceiling),
    ("a nesting bomb under the ceiling", _board_a_nesting_bomb_under_the_ceiling),
    ("a FIFO with no writer", _board_a_fifo_with_no_writer),
])
def test_an_unreadable_board_never_fails_the_rail_poll(SS, monkeypatch, label, make_board):
    """`platform/decisions/0091`'s "the rail poll never fails because of a bad
    board" covers more than malformed JSON — `security-reviewer`'s finding on
    #752 named a board path that is a directory, a permission-denied file and
    a JSON-nesting bomb as three more ways a board can be unreadable without
    being malformed JSON, none of which the pre-existing `SlateCorruptError`
    catch alone stopped from reaching `session_state`/`session_states` as a
    raw traceback. Round 3 added a fourth: a FIFO with no writer, which does
    not raise at all on the unhardened reader — it blocks inside `open()`
    forever, which would freeze `session_states()`'s whole poll of every
    crew it batches in one call, not just this one. The SIGALRM below is a
    hang detector for that case, not the pass condition; it is a no-op for
    the other three. Every one of the five must settle the same way the one
    malformed-JSON case above does: 0/0 plus `slate_error`, from `slate_counts`
    directly and from both `session_state`'s early-return path and
    `session_states`' hand-built failure entry, neither of which call
    `slate_counts` through that same guarded spot by accident."""
    import signal
    if label == "permission denied" and os.getuid() == 0:
        pytest.skip("root ignores a file's permission bits")
    path = SS.SLT.board_path("deck")
    make_board(path)
    if label == "a FIFO with no writer":
        signal.signal(signal.SIGALRM, lambda *a: (_ for _ in ()).throw(
            AssertionError("the rail poll hung on a writerless FIFO board")))
        signal.alarm(10)
    try:
        got = SS.slate_counts("deck")
        assert got["asks_open"] == 0
        assert got["asks_answered"] == 0
        assert "slate_error" in got

        def no_pane(name):
            raise SS.TT.JoinError("no claude process found in any pane of 'deck'")
        monkeypatch.setattr(SS.TT, "claude_pane", no_pane)
        no_session = SS.session_state("deck")
        assert no_session["asks_open"] == 0
        assert no_session["asks_answered"] == 0

        def no_state(name, days=3, brief=False, table=None):
            raise RuntimeError("tmux is not running")
        monkeypatch.setattr(SS, "session_state", no_state)
        monkeypatch.setattr(SS.TT, "session_table", lambda: {})
        entry = SS.session_states(["deck"])["deck"]
        assert entry["asks_open"] == 0
        assert entry["asks_answered"] == 0
    finally:
        if label == "a FIFO with no writer":
            signal.alarm(0)
        if label == "permission denied":
            os.chmod(path, 0o600)


@pytest.mark.parametrize("brief", [True, False])
def test_every_early_return_still_carries_asks_open_and_asks_answered(
        SS, monkeypatch, brief):
    SS.SLT.post_ask("deck", "still open?")
    answered_id = SS.SLT.post_ask("deck", "answered?")
    SS.SLT.answer_ask("deck", answered_id, "yes")

    def boom(name):
        raise SS.TT.JoinError("no claude process found in any pane of 'deck'")
    monkeypatch.setattr(SS.TT, "claude_pane", boom)

    no_session = SS.session_state("deck", brief=brief)

    assert no_session["asks_open"] == 1
    assert no_session["asks_answered"] == 1


def test_the_batchs_hand_built_failure_entry_carries_the_ask_counts_too(SS, monkeypatch):
    """The same trap `ctx_pct`/`rollover` were once caught in, for this
    field: `session_states` hand-builds its failure entry rather than
    getting one from `session_state`."""
    SS.SLT.post_ask("deck", "still open?")

    def boom(name, days=3, brief=False, table=None):
        raise RuntimeError("tmux is not running")
    monkeypatch.setattr(SS, "session_state", boom)
    monkeypatch.setattr(SS.TT, "session_table", lambda: {})

    entry = SS.session_states(["deck"])["deck"]
    assert entry["asks_open"] == 1
    assert entry["asks_answered"] == 0



# --- `rollover_command`: Deck's write side (Slice 3, ADR-0069) --------------
#
# `SS.RP.CACHE_DIR` is monkeypatched alongside `SS.VITALS_DIR` in every test
# below that writes or reads an override — the two constants point at the
# same real directory in production (`~/.cache/co-session-health`) but are
# separate module globals, so a test redirecting only one would still touch
# the other's real path.

@pytest.fixture
def rollover_session(SS, monkeypatch, tmp_path):
    """A resolvable session (`claude_pane`/`meta_for_pid` both answer) with its
    override and state files redirected into `tmp_path`. Returns the session
    id `rollover_command` will resolve `name` to."""
    monkeypatch.setattr(SS, "VITALS_DIR", tmp_path)
    monkeypatch.setattr(SS.RP, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(SS.TT, "claude_pane", lambda n: ("%9", 4242))
    monkeypatch.setattr(SS.TT, "meta_for_pid", lambda pid, days=3: {
        "session_id": "sess1", "cwd": "/tmp", "transcript_path": "/tmp/t.jsonl"})
    return "sess1"


def test_veto_writes_the_override_naming_the_current_natural_zone(
        SS, monkeypatch, rollover_session, tmp_path):
    (tmp_path / "rollover-sess1.json").write_text(json.dumps({
        "state": "seeking", "natural_state": "seeking", "reason": "threshold",
        "since": None, "calls_seeking": 3, "cap": None,
        "policy": SS.RP.DEFAULT_POLICY, "stop_blocked": False, "vetoed_until": None}))
    result = SS.rollover_command("deck", "veto")
    assert result["outcome"] == "armed"
    override = SS.RP.read_override(rollover_session, override_dir=tmp_path)
    assert override == {"kind": "veto", "zone": "seeking",
                        "written_at": override["written_at"]}


def test_seek_writes_the_override(SS, rollover_session, tmp_path):
    result = SS.rollover_command("deck", "seek")
    assert result["outcome"] == "armed"
    assert SS.RP.read_override(rollover_session, override_dir=tmp_path)["kind"] == "seek"


def test_clear_removes_a_previously_written_override(SS, rollover_session, tmp_path):
    SS.RP.write_override(rollover_session, "seek", override_dir=tmp_path)
    result = SS.rollover_command("deck", "clear")
    assert result["outcome"] == "armed"
    assert SS.RP.read_override(rollover_session, override_dir=tmp_path) is None


def test_clear_also_clears_a_rolling_state(SS, rollover_session, tmp_path):
    """Slice 5 round 2: `rolling` is a terminal fence with no other way out,
    so `clear` is also its manual escape hatch — see
    `rollover_policy.clear_rolling`'s docstring."""
    SS.RP.mark_rolling(rollover_session,
                       predecessor={"session_id": rollover_session, "pane_id": "%1"},
                       successor={"session_id": None, "pane_id": "%2"}, successor_window=2)
    assert SS.RP.read_state(rollover_session)["state"] == "rolling"

    result = SS.rollover_command("deck", "clear")

    assert result["outcome"] == "armed"
    assert SS.RP.read_state(rollover_session)["state"] == "ok"
    assert result["rollover"]["state"] == "none"


def test_clear_also_clears_a_parked_state(SS, rollover_session, tmp_path):
    """Slice 6: `parked` is terminal to `evaluate()` the same way `rolling`
    is, with no other way back to normal — see
    `rollover_policy.clear_parked`'s docstring."""
    SS.RP.mark_parked(rollover_session, reason="spawn_error",
                      handoff_path="/tmp/h/handoff.md", issue_id="issue_ABC")
    assert SS.RP.read_state(rollover_session)["state"] == "parked"

    result = SS.rollover_command("deck", "clear")

    assert result["outcome"] == "armed"
    assert SS.RP.read_state(rollover_session)["state"] == "ok"
    assert result["rollover"]["state"] == "none"


# --- an override written against a terminal (rolling/parked) session is
# inert at THIS read layer too (round-4 finding on PR #584): `rollover()`
# mirrors `evaluate()`'s own short-circuit (session_state.py:974) so
# `RP.resolve_override` is never even called while `rolling`/`parked` — only
# `clear` (tested above, for both states) has any effect. `force` was
# already pinned at the `evaluate()` layer in test_rollover_policy.py but
# never at this one, which is the read `session-agent state`/Deck actually
# uses; `veto`/`seek` were pinned at neither layer.

def test_a_veto_on_a_parked_session_leaves_the_read_parked(SS, rollover_session, tmp_path):
    SS.RP.mark_parked(rollover_session, reason="spawn_error",
                      handoff_path="/tmp/h/handoff.md", issue_id="issue_ABC")
    SS.RP.write_override(rollover_session, "veto", zone="parked", override_dir=tmp_path)

    result = SS.rollover(rollover_session)

    assert result["state"] == "parked"
    assert result["reason"] == "spawn_error"


def test_a_seek_on_a_parked_session_leaves_the_read_parked(SS, rollover_session, tmp_path):
    SS.RP.mark_parked(rollover_session, reason="spawn_error",
                      handoff_path="/tmp/h/handoff.md", issue_id="issue_ABC")
    SS.RP.write_override(rollover_session, "seek", override_dir=tmp_path)

    result = SS.rollover(rollover_session)

    assert result["state"] == "parked"
    assert result["reason"] == "spawn_error"


def test_a_force_on_a_parked_session_leaves_the_read_parked(SS, rollover_session, tmp_path):
    SS.RP.mark_parked(rollover_session, reason="spawn_error",
                      handoff_path="/tmp/h/handoff.md", issue_id="issue_ABC")
    SS.RP.write_override(rollover_session, "force", override_dir=tmp_path)

    result = SS.rollover(rollover_session)

    assert result["state"] == "parked"
    assert result["reason"] == "spawn_error"


def test_a_veto_on_a_rolling_session_leaves_the_read_rolling(SS, rollover_session, tmp_path):
    SS.RP.mark_rolling(rollover_session,
                       predecessor={"session_id": rollover_session, "pane_id": "%1"},
                       successor={"session_id": None, "pane_id": "%2"}, successor_window=2)
    SS.RP.write_override(rollover_session, "veto", zone="rolling", override_dir=tmp_path)

    result = SS.rollover(rollover_session)

    assert result["state"] == "rolling"


def test_a_seek_on_a_rolling_session_leaves_the_read_rolling(SS, rollover_session, tmp_path):
    SS.RP.mark_rolling(rollover_session,
                       predecessor={"session_id": rollover_session, "pane_id": "%1"},
                       successor={"session_id": None, "pane_id": "%2"}, successor_window=2)
    SS.RP.write_override(rollover_session, "seek", override_dir=tmp_path)

    result = SS.rollover(rollover_session)

    assert result["state"] == "rolling"


def test_a_force_on_a_rolling_session_leaves_the_read_rolling(SS, rollover_session, tmp_path):
    SS.RP.mark_rolling(rollover_session,
                       predecessor={"session_id": rollover_session, "pane_id": "%1"},
                       successor={"session_id": None, "pane_id": "%2"}, successor_window=2)
    SS.RP.write_override(rollover_session, "force", override_dir=tmp_path)

    result = SS.rollover(rollover_session)

    assert result["state"] == "rolling"


def test_the_next_hook_call_obeys_the_override_the_verb_just_wrote(
        SS, rollover_session, tmp_path):
    """THE PROPERTY THE BUILD ASKED FOR DIRECTLY: "each verb writes the
    override and the next hook call obeys it." Written through
    `rollover_command`; read back through `rollover_policy.evaluate` at the
    same paths, exactly as the hook itself would on this session's next
    `PostToolUse`."""
    SS.rollover_command("deck", "force")
    result = SS.RP.evaluate(rollover_session, "PostToolUse",
                            policy_path=tmp_path / "nonexistent-uses-default.json",
                            vitals_dir=tmp_path, state_dir=tmp_path, override_dir=tmp_path)
    assert result["state"] == "roll_now"
    assert result["reason"] == "operator_force"


def test_vetoed_is_produced_by_a_matching_veto_and_stays_in_the_closed_set(
        SS, rollover_session, tmp_path):
    """Before this fix nothing ever produced `vetoed` — Deck drew a state no
    real veto could reach (issue_01M2HTBMRN1W688EV40DCDVXB7). A veto whose
    recorded zone matches the hook's last `natural_state` is what produces
    it; `vetoed` already matches Deck's closed set, so this also pins that it
    is not remapped the way `ok` is."""
    (tmp_path / "rollover-sess1.json").write_text(json.dumps({
        "state": "seeking", "natural_state": "seeking",
        "reason": "threshold", "natural_reason": "threshold",
        "since": None, "calls_seeking": 1, "cap": None,
        "policy": {"mode": "enforce", "seek_pct": 70, "floor_pct": 95, "seek_call_cap": None},
        "stop_blocked": False, "vetoed_until": None}))
    SS.RP.write_override(rollover_session, "veto", zone="seeking", override_dir=tmp_path)

    result = SS.rollover(rollover_session)

    assert result["state"] == "vetoed"
    assert result["vetoed_until"] == "floor_pct"


# --- an operator override resolves against the hook's last reading, on every
# read — no hook firing required in between (issue_01M2HTBMRN1W688EV40DCDVXB7,
# the operator's ruling: "I think veto should change the state immediately.") --------

def test_seek_changes_the_read_immediately_with_no_hook_firing(SS, rollover_session, tmp_path):
    (tmp_path / "rollover-sess1.json").write_text(json.dumps({
        "state": "ok", "natural_state": "ok", "reason": None, "natural_reason": None,
        "since": None, "calls_seeking": 0, "cap": None,
        "policy": SS.RP.DEFAULT_POLICY, "stop_blocked": False, "vetoed_until": None}))

    result = SS.rollover_command("deck", "seek")

    assert result["rollover"]["state"] == "seeking"
    assert result["rollover"]["reason"] == "operator_seek"


def test_veto_changes_the_read_immediately_with_no_hook_firing(SS, rollover_session, tmp_path):
    (tmp_path / "rollover-sess1.json").write_text(json.dumps({
        "state": "seeking", "natural_state": "seeking",
        "reason": "threshold", "natural_reason": "threshold",
        "since": None, "calls_seeking": 3, "cap": None,
        "policy": SS.RP.DEFAULT_POLICY, "stop_blocked": False, "vetoed_until": None}))

    result = SS.rollover_command("deck", "veto")

    assert result["rollover"]["state"] == "vetoed"


def test_clear_changes_the_read_immediately_with_no_hook_firing(SS, rollover_session, tmp_path):
    """A veto with no corroborating override left on disk has to stop being
    reported the instant it is cleared, not merely once the session's own
    next hook firing recomputes a fresh zone from `ctx_pct` — the same
    property `test_clear_also_clears_a_rolling_state` pins for the terminal
    states, here for the ordinary `vetoed` -> natural transition."""
    (tmp_path / "rollover-sess1.json").write_text(json.dumps({
        "state": "vetoed", "natural_state": "seeking",
        "reason": "operator_veto", "natural_reason": "threshold",
        "since": None, "calls_seeking": 3, "cap": None,
        "policy": SS.RP.DEFAULT_POLICY, "stop_blocked": False, "vetoed_until": None}))
    SS.RP.write_override(rollover_session, "veto", zone="seeking", override_dir=tmp_path)

    result = SS.rollover_command("deck", "clear")

    assert result["rollover"]["state"] == "seeking"
    assert result["rollover"]["reason"] == "threshold"


def test_a_veto_that_cancels_an_operator_seek_still_shows_vetoed(
        SS, rollover_session, tmp_path):
    """Requirement 1 of issue_01M2HTBMRN1W688EV40DCDVXB7: a veto that cancels
    an operator's own `seek` still shows `vetoed`, even though the session
    never crossed a real threshold to get into the zone being vetoed —
    `natural_reason` says the "seeking" here is `operator_seek`, not
    `threshold`. `vetoed_until` is `None` rather than naming `floor_pct`:
    naming it would promise the veto lifts once a real ctx crossing happens,
    when nothing about why this veto was set has anything to do with the
    floor."""
    (tmp_path / "rollover-sess1.json").write_text(json.dumps({
        "state": "seeking", "natural_state": "seeking",
        "reason": "operator_seek", "natural_reason": "operator_seek",
        "since": None, "calls_seeking": 1, "cap": None,
        "policy": {"mode": "enforce", "seek_pct": 70, "floor_pct": 95, "seek_call_cap": None},
        "stop_blocked": False, "vetoed_until": None}))
    SS.RP.write_override(rollover_session, "seek", override_dir=tmp_path)

    result = SS.rollover_command("deck", "veto")

    assert result["rollover"]["state"] == "vetoed"
    assert result["rollover"]["vetoed_until"] is None


def test_a_veto_set_at_ok_holds_through_seeking_on_the_read_path(
        SS, rollover_session, tmp_path):
    """The read-time counterpart of `test_a_veto_set_at_ok_holds_through_
    seeking_to_the_floor` in `test_rollover_policy.py` — `session_state.
    rollover` must resolve the same override the same way `evaluate` does,
    with no hook firing required in between
    (issue_01M3WX4443DF30SJ3WM5QHA7RJ, issue_01M2HTBMRN1W688EV40DCDVXB7)."""
    policy = {"mode": "enforce", "seek_pct": 70, "floor_pct": 95, "seek_call_cap": None}
    SS.RP.write_override(rollover_session, "veto", zone="ok", override_dir=tmp_path)

    (tmp_path / "rollover-sess1.json").write_text(json.dumps({
        "state": "ok", "natural_state": "ok", "reason": None, "natural_reason": None,
        "since": None, "calls_seeking": 0, "cap": None,
        "policy": policy, "stop_blocked": False, "vetoed_until": None}))
    below = SS.rollover(rollover_session)
    assert below["state"] == "vetoed"
    assert below["vetoed_until"] == "floor_pct"

    (tmp_path / "rollover-sess1.json").write_text(json.dumps({
        "state": "seeking", "natural_state": "seeking",
        "reason": "threshold", "natural_reason": "threshold",
        "since": None, "calls_seeking": 1, "cap": None,
        "policy": policy, "stop_blocked": False, "vetoed_until": None}))
    seeking = SS.rollover(rollover_session)
    assert seeking["state"] == "vetoed"
    assert seeking["vetoed_until"] == "floor_pct"

    (tmp_path / "rollover-sess1.json").write_text(json.dumps({
        "state": "roll_now", "natural_state": "roll_now",
        "reason": "floor", "natural_reason": "floor",
        "since": None, "calls_seeking": 1, "cap": None,
        "policy": policy, "stop_blocked": False, "vetoed_until": None}))
    rolling = SS.rollover(rollover_session)
    assert rolling["state"] == "roll_now"


def test_a_veto_set_at_roll_now_holds_on_the_read_path(
        SS, rollover_session, tmp_path):
    """The read-time counterpart of
    `test_vetoed_until_is_null_once_a_veto_set_at_roll_now_has_no_rung_above_
    it` in `test_rollover_policy.py` — a veto recorded AT `roll_now` covers
    `roll_now` itself (`_veto_covers`'s table), so `session_state.rollover`
    reports `vetoed` off a persisted `natural_state` of `roll_now`, with no
    hook firing required in between."""
    policy = {"mode": "enforce", "seek_pct": 70, "floor_pct": 95, "seek_call_cap": None}
    SS.RP.write_override(rollover_session, "veto", zone="roll_now", override_dir=tmp_path)

    state_path = tmp_path / "rollover-sess1.json"
    state_path.write_text(json.dumps({
        "state": "roll_now", "natural_state": "roll_now",
        "reason": "floor", "natural_reason": "floor",
        "since": None, "calls_seeking": 1, "cap": None,
        "policy": policy, "stop_blocked": False, "vetoed_until": None}))
    result = SS.rollover(rollover_session)
    assert result["state"] == "vetoed"
    assert result["vetoed_until"] is None
    # The hook's own persisted reading is untouched by this read — the state
    # file still says the natural zone is `roll_now`, confirming this read
    # resolved the veto against that zone rather than the `vetoed` it is
    # about to report.
    assert json.loads(state_path.read_text())["natural_state"] == "roll_now"


def test_seek_does_not_fabricate_a_context_reading_with_no_hook_firing_ever(
        SS, rollover_session, tmp_path):
    """THE HOOK'S OWN DECISION STILL WINS FOR THE ZONE: no
    `rollover-sess1.json` has ever been written, so the hook has never taken
    a `ctx_pct` reading for this session at all. `seek` changes what a
    reading would be TOLD, not whether one exists — with nothing to promote,
    the read stays the honest absence, `none`."""
    result = SS.rollover_command("deck", "seek")
    assert result["rollover"]["state"] == "none"


def test_states_and_state_agree_on_a_resolved_override(SS, monkeypatch, tmp_path):
    """`session-agent states` (the rail's batched, brief reader) and
    `session-agent state` (the single full reader) both resolve an override
    through the same `rollover()` — pinned directly, since a divergence here
    would mean the rail and the open pane could disagree about the same
    session."""
    monkeypatch.setattr(SS, "VITALS_DIR", tmp_path)
    monkeypatch.setattr(SS, "ACTIVE_JSON", tmp_path / "none.json")
    monkeypatch.setattr(SS.TT, "claude_pane", lambda n, table=None: ("%1", 4242))
    monkeypatch.setattr(SS.TT, "session_table", lambda: {})
    monkeypatch.setattr(SS.TT, "meta_for_pid", lambda pid, days=3: {
        "session_id": "abc", "cwd": "/srv/convivy/convivy-deck",
        "transcript_path": str(tmp_path / "t.jsonl")})
    monkeypatch.setattr(SS, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(SS, "_hook_logs", lambda sid, cwd=None, days=3: [])
    (tmp_path / "rollover-abc.json").write_text(json.dumps({
        "state": "ok", "natural_state": "ok", "reason": None, "natural_reason": None,
        "since": None, "calls_seeking": 0, "cap": None,
        "policy": SS.RP.DEFAULT_POLICY, "stop_blocked": False, "vetoed_until": None}))
    SS.RP.write_override("abc", "seek", override_dir=tmp_path)

    brief = SS.session_states(["deck"])["deck"]
    full = SS.session_state("deck")

    assert brief["rollover"]["state"] == "seeking"
    assert full["rollover"]["state"] == "seeking"
    assert brief["rollover"] == full["rollover"]


def test_rollover_command_refuses_when_the_session_cannot_be_resolved(SS, monkeypatch):
    def boom(name):
        raise SS.TT.JoinError("no claude process found in any pane of 'deck'")
    monkeypatch.setattr(SS.TT, "claude_pane", boom)
    result = SS.rollover_command("deck", "veto")
    assert result["outcome"] == "refused"
    assert result["rollover"]["state"] == "none"


def test_force_on_an_idle_session_sends_rollover(SS, monkeypatch, rollover_session):
    monkeypatch.setattr(SS, "session_state", lambda name: {"state": "idle"})
    monkeypatch.setattr(SS.SI, "_read_input_box", lambda pane_id: "")
    monkeypatch.setattr(SS, "_held_queue_now", lambda meta: None)
    sent = []
    monkeypatch.setattr(SS.SI, "send_message", lambda name, text: sent.append((name, text)))

    result = SS.rollover_command("deck", "force")
    assert result["outcome"] == "sent"
    assert sent == [("deck", "/rollover")]


def test_force_on_a_working_session_only_arms_it(SS, monkeypatch, rollover_session, tmp_path):
    """No hook fires while idle — that is the case the direct send exists
    for. A working session has its own PostToolUse/Stop coming; the verb must
    not paste on top of whatever it is doing. Regression pin: this path
    writes the override and returns `armed` unconditionally, exactly as
    before the refused-force fix below.

    Also pins `force still reads immediately, as it does today`
    (issue_01M2HTBMRN1W688EV40DCDVXB7): with a state file already on disk
    (this session has taken at least one turn), the same override-file `arm`
    path veto/seek use also reports `roll_now` before any hook fires again."""
    (tmp_path / "rollover-sess1.json").write_text(json.dumps({
        "state": "ok", "natural_state": "ok", "reason": None, "natural_reason": None,
        "since": None, "calls_seeking": 0, "cap": None,
        "policy": SS.RP.DEFAULT_POLICY, "stop_blocked": False, "vetoed_until": None}))
    monkeypatch.setattr(SS, "session_state", lambda name: {"state": "working"})
    sent = []
    monkeypatch.setattr(SS.SI, "send_message", lambda name, text: sent.append((name, text)))

    result = SS.rollover_command("deck", "force")
    assert result["outcome"] == "armed"
    assert sent == []
    assert result["rollover"]["state"] == "roll_now"
    assert result["rollover"]["reason"] == "operator_force"
    assert SS.RP.read_override(rollover_session, override_dir=tmp_path)["kind"] == "force"


def test_force_refuses_when_the_composer_holds_a_draft(SS, monkeypatch, rollover_session, tmp_path):
    """THE HAZARD THIS GUARDS (epic comment 2399): sending `/rollover`
    unconditionally would clear-then-paste over a live human draft — the same
    clear `send_message` already performs for an idle session, which is safe
    only because it assumes any box content is a restored, interruption echo
    rather than someone typing right now. A `refused` outcome must leave no
    trace: nothing writes the override before this check passes, so the
    session's next hook firing sees no `force` at all (Deck's contract:
    `outcome: "refused"` means nothing changed)."""
    monkeypatch.setattr(SS, "session_state", lambda name: {"state": "idle"})
    monkeypatch.setattr(SS.SI, "_read_input_box", lambda pane_id: "half a sent")
    sent = []
    monkeypatch.setattr(SS.SI, "send_message", lambda name, text: sent.append((name, text)))

    result = SS.rollover_command("deck", "force")
    assert result["outcome"] == "refused"
    assert "draft" in result["reason"]
    assert sent == []
    assert SS.RP.read_override(rollover_session, override_dir=tmp_path) is None


def test_force_refuses_when_a_draft_appears_between_the_checks_and_the_send(
        SS, monkeypatch, rollover_session, tmp_path):
    """Narrows the window a `reviewer` finding named: the composer read at the
    top of the branch was empty, the held-queue check ran (taking its own
    time), and a draft landed before the send. The re-read immediately before
    the write and the paste catches it, and refusing here must still leave no
    override on disk."""
    monkeypatch.setattr(SS, "session_state", lambda name: {"state": "idle"})
    reads = iter(["", "typed while the held-queue check was running"])
    monkeypatch.setattr(SS.SI, "_read_input_box", lambda pane_id: next(reads))
    monkeypatch.setattr(SS, "_held_queue_now", lambda meta: None)
    sent = []
    monkeypatch.setattr(SS.SI, "send_message", lambda name, text: sent.append((name, text)))

    result = SS.rollover_command("deck", "force")
    assert result["outcome"] == "refused"
    assert "draft" in result["reason"]
    assert sent == []
    assert SS.RP.read_override(rollover_session, override_dir=tmp_path) is None


def test_force_refuses_when_the_held_queue_is_non_empty(SS, monkeypatch, rollover_session, tmp_path):
    monkeypatch.setattr(SS, "session_state", lambda name: {"state": "idle"})
    monkeypatch.setattr(SS.SI, "_read_input_box", lambda pane_id: "")
    monkeypatch.setattr(SS, "_held_queue_now", lambda meta: {"messages": ["still waiting"]})
    sent = []
    monkeypatch.setattr(SS.SI, "send_message", lambda name, text: sent.append((name, text)))

    result = SS.rollover_command("deck", "force")
    assert result["outcome"] == "refused"
    assert "queued" in result["reason"]
    assert sent == []
    assert SS.RP.read_override(rollover_session, override_dir=tmp_path) is None


def test_force_refuses_and_restores_no_override_when_send_fails(
        SS, monkeypatch, rollover_session, tmp_path):
    """A `force` that clears every draft/held-queue check still has to send
    the paste, and the send can fail (pane gone, tmux error). The override
    was written just before the send so the paste's own UserPromptSubmit
    firing would see it — a send failure must put that write back exactly as
    it found it, here: no override existed before the call, so none exists
    after."""
    monkeypatch.setattr(SS, "session_state", lambda name: {"state": "idle"})
    monkeypatch.setattr(SS.SI, "_read_input_box", lambda pane_id: "")
    monkeypatch.setattr(SS, "_held_queue_now", lambda meta: None)

    def boom(name, text):
        raise SS.SI.InputError("pane vanished mid-send")
    monkeypatch.setattr(SS.SI, "send_message", boom)

    result = SS.rollover_command("deck", "force")
    assert result["outcome"] == "refused"
    assert "pane vanished" in result["reason"]
    assert SS.RP.read_override(rollover_session, override_dir=tmp_path) is None


def test_force_refuses_and_restores_the_prior_veto_when_send_fails(
        SS, monkeypatch, rollover_session, tmp_path):
    """Same send failure, but this time a veto was already armed before the
    `force` call. Restoring must put back the exact prior override — kind,
    zone and `written_at` — not merely clear the file."""
    prior = SS.RP.write_override(rollover_session, "veto", zone="seeking", override_dir=tmp_path)
    monkeypatch.setattr(SS, "session_state", lambda name: {"state": "idle"})
    monkeypatch.setattr(SS.SI, "_read_input_box", lambda pane_id: "")
    monkeypatch.setattr(SS, "_held_queue_now", lambda meta: None)

    def boom(name, text):
        raise SS.SI.InputError("pane vanished mid-send")
    monkeypatch.setattr(SS.SI, "send_message", boom)

    result = SS.rollover_command("deck", "force")
    assert result["outcome"] == "refused"
    restored = SS.RP.read_override(rollover_session, override_dir=tmp_path)
    assert restored == prior


def test_force_refuses_and_restores_no_override_when_the_send_raises_join_error(
        SS, monkeypatch, rollover_session, tmp_path):
    """`send_message` re-resolves the pane through `TT.claude_pane` partway
    through, and raises `TT.JoinError` — not `SI.InputError` — if the session
    has gone in the meantime. The restore has to cover that exception too, or
    the `force` written just before the send is left stray with no `refused`
    outcome at all (reviewer finding on PR #570 round 1)."""
    monkeypatch.setattr(SS, "session_state", lambda name: {"state": "idle"})
    monkeypatch.setattr(SS.SI, "_read_input_box", lambda pane_id: "")
    monkeypatch.setattr(SS, "_held_queue_now", lambda meta: None)

    def boom(name, text):
        raise SS.TT.JoinError("no claude process found in any pane of 'deck'")
    monkeypatch.setattr(SS.SI, "send_message", boom)

    result = SS.rollover_command("deck", "force")
    assert result["outcome"] == "refused"
    assert "no claude process found" in result["reason"]
    assert SS.RP.read_override(rollover_session, override_dir=tmp_path) is None


def test_force_refuses_and_restores_the_prior_veto_when_the_send_raises_join_error(
        SS, monkeypatch, rollover_session, tmp_path):
    """Same as above, but a veto was already armed before the `force` call —
    the restore must put back the exact prior override, not merely clear the
    file."""
    prior = SS.RP.write_override(rollover_session, "veto", zone="seeking", override_dir=tmp_path)
    monkeypatch.setattr(SS, "session_state", lambda name: {"state": "idle"})
    monkeypatch.setattr(SS.SI, "_read_input_box", lambda pane_id: "")
    monkeypatch.setattr(SS, "_held_queue_now", lambda meta: None)

    def boom(name, text):
        raise SS.TT.JoinError("no claude process found in any pane of 'deck'")
    monkeypatch.setattr(SS.SI, "send_message", boom)

    result = SS.rollover_command("deck", "force")
    assert result["outcome"] == "refused"
    restored = SS.RP.read_override(rollover_session, override_dir=tmp_path)
    assert restored == prior


def test_a_policy_edit_shows_in_the_next_rollover_read(SS, monkeypatch, tmp_path):
    """The policy embedded in the state file is a snapshot from the call that
    wrote it — an edit does not retroactively change an already-persisted
    read, but it reaches the very NEXT `evaluate()`, which is what
    `session-agent rollover-policy --set` followed by the session's own next
    hook firing looks like end to end."""
    monkeypatch.setattr(SS, "VITALS_DIR", tmp_path)
    (tmp_path / "vitals-s1.json").write_text(json.dumps(
        {"written_at": time.time(), "ctx_pct": 80}))
    policy_path = tmp_path / "rollover.json"
    policy_path.write_text(json.dumps(
        {"mode": "enforce", "seek_pct": 70, "floor_pct": None, "seek_call_cap": None}))

    SS.RP.evaluate("s1", "PostToolUse", policy_path=policy_path,
                   vitals_dir=tmp_path, state_dir=tmp_path, override_dir=tmp_path)
    assert SS.rollover("s1")["policy"]["seek_pct"] == 70

    policy = SS.RP.apply_policy_updates(SS.RP.load_policy(policy_path), {"seek_pct": 55})
    SS.RP.save_policy(policy, path=policy_path)

    SS.RP.evaluate("s1", "PostToolUse", policy_path=policy_path,
                   vitals_dir=tmp_path, state_dir=tmp_path, override_dir=tmp_path)
    assert SS.rollover("s1")["policy"]["seek_pct"] == 55


def test_signature_ignores_ctx_pct_and_rollover_entirely(SS):
    """Neither belongs in `_SIGNIFICANT`: a context percentage and a
    `calls_seeking` count both move on nearly every poll while a session keeps
    working, and folding either into a follower's diff would make ordinary
    progress look like a state transition."""
    base = {"state": "idle", "gate": None, "last_event": "Stop", "alive": True,
            "held": None, "ctx_pct": 40.0,
            "rollover": {"state": "ok", "since": None, "calls_seeking": 0}}
    changed = {**base, "ctx_pct": 91.5,
              "rollover": {"state": "seeking", "since": 123, "calls_seeking": 9}}
    assert SS.signature(base) == SS.signature(changed)
    assert "ctx_pct" not in SS._SIGNIFICANT
    assert "rollover" not in SS._SIGNIFICANT


# --- `held_items`: what this session holds for the operator (Slice 7, ADR-0069) --
#
# Epic issue_01M1S2X8ENKYEGSDE3H0REFD3G, Slice 7: a rollover's handoff must
# carry a composer draft, a held queued message, and the rollover state
# explicitly — never a silently omitted section, which would be
# indistinguishable from a handoff that never checked at all.


@pytest.fixture
def held_items_session(SS, monkeypatch, tmp_path):
    """A resolvable session (`claude_pane`/`meta_for_pid` both answer), with
    a real transcript file `held_items` can read the queue from. Mirrors
    `rollover_session` above, one file section over."""
    monkeypatch.setattr(SS, "VITALS_DIR", tmp_path)
    monkeypatch.setattr(SS.RP, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(SS.TT, "claude_pane", lambda n: ("%9", 4242))
    t = _contained_transcript(tmp_path, monkeypatch)
    monkeypatch.setattr(SS.TT, "meta_for_pid", lambda pid, days=3: {
        "session_id": "sess1", "cwd": "/tmp", "transcript_path": str(t)})
    return t


def test_held_items_reports_a_draft_a_held_message_and_the_rollover_state(
        SS, monkeypatch, held_items_session, tmp_path):
    held_items_session.write_text(
        '{"type": "queue-operation", "operation": "enqueue", '
        '"timestamp": "2026-09-15T05:12:00.000Z", "content": "please check the PR"}\n')
    monkeypatch.setattr(SS.SI, "_read_input_box", lambda pane_id: "half-typed line")
    (tmp_path / "rollover-sess1.json").write_text(json.dumps({
        "state": "seeking", "natural_state": "seeking", "reason": "threshold",
        "since": None, "calls_seeking": 3, "cap": None,
        "policy": SS.RP.DEFAULT_POLICY, "stop_blocked": False, "vetoed_until": None}))

    result = SS.held_items("deck")

    assert result["outcome"] == "ok"
    assert result["draft"] == "half-typed line"
    assert result["held_messages"] == [
        {"content": "please check the PR", "queued_at": "2026-09-15T05:12:00.000Z"}]
    assert result["rollover"]["state"] == "seeking"


def test_held_items_says_nothing_held_explicitly_when_empty(
        SS, monkeypatch, held_items_session):
    """An empty draft and an empty queue are POSITIVE answers (`None`,
    `[]`), not an omission — the caller (`bin/session-agent`'s
    `_held_items_markdown`) is what turns this into an explicit line rather
    than a silently missing section, but the data itself must already tell
    the two apart from "could not check"."""
    held_items_session.write_text("")
    monkeypatch.setattr(SS.SI, "_read_input_box", lambda pane_id: "")

    result = SS.held_items("deck")

    assert result["outcome"] == "ok"
    assert result["draft"] is None
    assert result["held_messages"] == []
    assert result["rollover"]["state"] == "none"


def test_held_items_held_messages_is_none_when_the_fold_could_not_tell(
        SS, monkeypatch, held_items_session):
    """A malformed `enqueue` (no usable content) makes the fold refuse
    rather than under-report — `None`, not `[]`, so a caller cannot mistake
    "could not check" for "positively nothing held"."""
    held_items_session.write_text(
        '{"type": "queue-operation", "operation": "enqueue"}\n')
    monkeypatch.setattr(SS.SI, "_read_input_box", lambda pane_id: "")

    result = SS.held_items("deck")

    assert result["outcome"] == "ok"
    assert result["held_messages"] is None


def test_held_items_refuses_when_the_session_cannot_be_resolved(SS, monkeypatch):
    def boom(name):
        raise SS.TT.JoinError("no claude process found in any pane of 'deck'")
    monkeypatch.setattr(SS.TT, "claude_pane", boom)

    result = SS.held_items("deck")

    assert result["outcome"] == "refused"
    assert "no claude process" in result["reason"]


# --- `working_on`: what the co says it is doing, for Deck's cockpit -----------
#
# `bin/co-working-on` is the writer; `working_on()` is the reader these tests
# pin. Unlike `vitals` it is keyed by the tmux session NAME (what `session_state`
# already takes as `name`), not by a Claude Code session id, and it is carried
# in `brief` as well as the full read — see the module comment for why.


def test_working_on_carries_a_well_formed_recording(SS, monkeypatch, tmp_path):
    monkeypatch.setattr(SS, "VITALS_DIR", tmp_path)
    (tmp_path / "working-deck.json").write_text(json.dumps({
        "text": "chasing the pagination bug", "written_at": 1_000_000_000}))

    got = SS.working_on("deck")
    assert got["text"] == "chasing the pagination bug"
    assert got["age"] > 0
    assert got["stale"] is True, "a recording that old from a fixed epoch is stale"


def test_since_is_a_duration_and_is_absent_without_a_start(SS, monkeypatch, tmp_path):
    """`since` answers "how long has this been the work", which is a different
    question from `age`'s "how long since anyone confirmed it".

    A recording written by an older version of the writer carries no
    `started_at`, and this must report NO duration rather than inferring one
    from the last write — that would say work begun hours ago started seconds
    ago, which is the precise-looking wrong answer the field exists to avoid.
    """
    monkeypatch.setattr(SS, "VITALS_DIR", tmp_path)
    key = "sess"
    (tmp_path / f"working-{key}.json").write_text(json.dumps(
        {"text": "chasing the pagination bug", "written_at": time.time() - 30}))

    got = SS.working_on(key)

    assert got["text"] == "chasing the pagination bug"
    assert got["age"] is not None, "it was still confirmed 30s ago"
    assert got["since"] is None, "but nothing says when the work began"


def test_since_outlives_a_re_announcement(SS, monkeypatch, tmp_path):
    """The reader's half of the writer's two-timestamp rule: a line confirmed
    seconds ago can still have been the work for an hour."""
    monkeypatch.setattr(SS, "VITALS_DIR", tmp_path)
    key = "sess"
    now = time.time()
    (tmp_path / f"working-{key}.json").write_text(json.dumps(
        {"text": "chasing the pagination bug",
         "written_at": now - 5, "started_at": now - 3600}))

    got = SS.working_on(key)

    assert got["age"] < 60, "freshly confirmed"
    assert got["stale"] is False
    assert got["since"] > 3000, "and been the work for about an hour"


def test_working_on_with_no_file_is_the_honest_absence(SS, monkeypatch, tmp_path):
    """A session that has never called the writer has nothing to report — not an
    error, and not zero, exactly the `vitals` "no reading yet" doctrine."""
    monkeypatch.setattr(SS, "VITALS_DIR", tmp_path)
    got = SS.working_on("never-called-it")
    assert got == {"text": None, "age": None, "stale": None, "since": None}


@pytest.mark.parametrize("payload", [
    "not a json object",
    {"text": 12345, "written_at": 1_000_000_000},
    {"text": "   ", "written_at": 1_000_000_000},
    {"written_at": 1_000_000_000},
])
def test_a_malformed_recording_is_the_same_honest_absence(SS, monkeypatch, tmp_path, payload):
    """A missing, empty or malformed file must cost this field, never the state
    call — the reader's job is to degrade, not to raise."""
    monkeypatch.setattr(SS, "VITALS_DIR", tmp_path)
    (tmp_path / "working-deck.json").write_text(
        payload if isinstance(payload, str) else json.dumps(payload))

    got = SS.working_on("deck")
    assert got["text"] is None


def test_an_unusable_written_at_still_carries_the_text(SS, monkeypatch, tmp_path):
    """`text` and `written_at` degrade independently: a corrupted timestamp is no
    reason to also drop a perfectly good piece of text, it just means age and
    staleness cannot be established either."""
    monkeypatch.setattr(SS, "VITALS_DIR", tmp_path)
    (tmp_path / "working-deck.json").write_text(json.dumps({
        "text": "fine", "written_at": "not a number"}))

    got = SS.working_on("deck")
    assert got["text"] == "fine"
    assert got["age"] is None and got["stale"] is None


def test_working_on_staleness_flips_at_its_threshold(SS, monkeypatch, tmp_path):
    """Twenty minutes, deliberately shorter than vitals' hour — see the module
    comment for why a working-on sentence goes stale faster than a context
    number does."""
    monkeypatch.setattr(SS, "VITALS_DIR", tmp_path)
    now = SS.datetime.now(SS.timezone.utc).timestamp()

    (tmp_path / "working-fresh.json").write_text(json.dumps({
        "text": "still on it", "written_at": now - (SS.WORKING_ON_STALE_SECONDS - 5)}))
    assert SS.working_on("fresh")["stale"] is False

    (tmp_path / "working-old.json").write_text(json.dumps({
        "text": "still on it", "written_at": now - (SS.WORKING_ON_STALE_SECONDS + 5)}))
    assert SS.working_on("old")["stale"] is True


def test_working_on_re_bounds_the_text_on_read(SS, monkeypatch, tmp_path):
    """The text reaches a WKWebView pane as display content. The writer already
    bounds it, but this reader must not trust that — a hand-edited file or an
    older writer must not hand an unbounded string straight through."""
    monkeypatch.setattr(SS, "VITALS_DIR", tmp_path)
    # Space-separated words, not one long run — a 32+ char alphanumeric run
    # would trip `sanitise`'s credential redaction and test the wrong thing.
    long_text = "word " * ((SS._WORKING_ON_MAX_LEN + 50) // len("word "))
    (tmp_path / "working-deck.json").write_text(json.dumps({
        "text": long_text, "written_at": 1_000_000_000}))

    got = SS.working_on("deck")["text"]
    assert len(got) <= SS._WORKING_ON_MAX_LEN + len("...")
    assert got.endswith("...")


def test_a_session_name_cannot_escape_the_working_on_directory(SS, monkeypatch, tmp_path):
    monkeypatch.setattr(SS, "VITALS_DIR", tmp_path / "cache")
    (tmp_path / "cache").mkdir()
    (tmp_path / "secret.json").write_text(json.dumps({"text": "leaked"}))

    for hostile in ("../secret", "../../etc/passwd", "/absolute", ""):
        assert SS.working_on(hostile)["text"] is None, hostile


def test_working_on_is_carried_in_brief_too_unlike_vitals(SS, monkeypatch, tmp_path):
    """`vitals` stays behind `brief` because it costs a per-session health-file
    read plus a remote-control read for a status area the rail never draws.
    `working_on` is a single small file read keyed on `name` alone — no pid, no
    meta.json — and the rail is exactly the view this is for: a crew row
    showing what its session is doing. So it does not inherit vitals' answer."""
    monkeypatch.setattr(SS, "VITALS_DIR", tmp_path)
    monkeypatch.setattr(SS, "ACTIVE_JSON", tmp_path / "none.json")
    monkeypatch.setattr(SS.TT, "claude_pane", lambda n: ("%1", 4242))
    monkeypatch.setattr(SS.TT, "meta_for_pid", lambda pid, days=3: {
        "session_id": "aaaa", "cwd": "/srv/convivy/convivy-deck",
        "transcript_path": str(tmp_path / "t.jsonl")})
    monkeypatch.setattr(SS, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(SS, "_hook_logs", lambda sid, cwd=None, days=3: [])
    (tmp_path / "working-deck.json").write_text(json.dumps({
        "text": "reviewing the cockpit PR", "written_at": 1_000_000_000}))

    brief = SS.session_state("deck", brief=True)
    assert brief["working_on"]["text"] == "reviewing the cockpit PR"

    full = SS.session_state("deck")
    assert full["working_on"]["text"] == "reviewing the cockpit PR"


@pytest.mark.parametrize("brief", [True, False])
def test_every_early_return_still_carries_a_working_on_key(SS, monkeypatch, tmp_path, brief):
    """Seeded at the top of `session_state`, like `agents`, so a session with no
    run-dir record — or no claude process at all — still answers rather than
    raising `KeyError` on a consumer that reads `state["working_on"]`."""
    monkeypatch.setattr(SS, "VITALS_DIR", tmp_path)
    (tmp_path / "working-deck.json").write_text(json.dumps({
        "text": "still here", "written_at": 1_000_000_000}))

    monkeypatch.setattr(SS.TT, "claude_pane", lambda n: ("%1", 4242))
    monkeypatch.setattr(SS.TT, "meta_for_pid", lambda pid, days=3: None)
    no_meta = SS.session_state("deck", brief=brief)
    assert no_meta["working_on"]["text"] == "still here"

    def boom(name):
        raise SS.TT.JoinError("no claude process found in any pane of 'deck'")
    monkeypatch.setattr(SS.TT, "claude_pane", boom)
    no_session = SS.session_state("deck", brief=brief)
    assert no_session["working_on"]["text"] == "still here"


def test_the_batchs_hand_built_failure_entry_carries_working_on_too(SS, monkeypatch, tmp_path):
    """The same trap `agents` was once caught in: `session_states` hand-builds
    its failure entry rather than getting one from `session_state`, so a field
    added to the latter's seed reaches every path except this one unless it is
    added here too."""
    monkeypatch.setattr(SS, "VITALS_DIR", tmp_path)
    (tmp_path / "working-deck.json").write_text(json.dumps({
        "text": "still here", "written_at": 1_000_000_000}))

    def boom(name, days=3, brief=False, table=None):
        raise RuntimeError("tmux is not running")
    monkeypatch.setattr(SS, "session_state", boom)
    monkeypatch.setattr(SS.TT, "session_table", lambda: {})

    entry = SS.session_states(["deck"])["deck"]
    assert entry["reason"] == "tmux is not running"
    assert entry["working_on"]["text"] == "still here"


def test_signature_ignores_working_on_age_but_reacts_to_its_text(SS):
    """`age` moves every poll, the same trap `_SIGNIFICANT`'s own docstring
    names; folding the whole `working_on` object into a follower's diff would
    make every tick look like a transition. Only the text may fire an emit."""
    base = {"state": "idle", "gate": None, "last_event": "Stop", "alive": True,
            "held": None, "working_on": {"text": "same thing", "age": 10, "stale": False}}
    aged = {**base, "working_on": {"text": "same thing", "age": 999, "stale": True}}
    assert SS.signature(base) == SS.signature(aged), "age alone must not fire"

    changed = {**base, "working_on": {"text": "something else", "age": 10, "stale": False}}
    assert SS.signature(base) != SS.signature(changed)


# --- `working_on_derived`: the same fact, assembled rather than typed --------
# issue_01M15YAED1GD3X6D9QKPRFZFEK. `working_on` above is a co's own words;
# this is the platform's, reduced from `agentctl spawn`'s own records with no
# model call. The sharp edge is `started_at`: it is keyed to a derived work
# IDENTITY, not to the rendered text changing, and comment 1211 on the parent
# issue names the failure mode a naive "text changed" rule falls into — one
# PR through five review rounds emits five different dispatch strings, so
# that rule moves the clock five times across ninety minutes of continuous
# work on one thing.


def _write_active_json(path, runs):
    path.write_text(json.dumps({"runs": runs}))


def _write_run_meta(runs_root, run_id, day, **fields):
    d = runs_root / day / run_id
    d.mkdir(parents=True, exist_ok=True)
    payload = {"run_id": run_id, "agent": "reviewer", "task": "review",
               "started_at": "2026-08-29T10:00:00Z", "repo": "agent-platform",
               "worktree": "/tmp/wt", "profile": "default", "model": "x",
               "pr": None, "co_session": None}
    payload.update(fields)
    (d / "meta.json").write_text(json.dumps(payload))


def _setup_derived_env(SS, monkeypatch, tmp_path):
    """Every test below points the module's on-disk roots at `tmp_path`, so
    none of them can read this machine's real `active.json` or cache dir."""
    monkeypatch.setattr(SS, "VITALS_DIR", tmp_path / "cache")
    monkeypatch.setattr(SS, "ACTIVE_JSON", tmp_path / "active.json")
    monkeypatch.setattr(SS, "AGENT_RUNS_ROOT", tmp_path / "runs")
    (tmp_path / "runs").mkdir(exist_ok=True)
    _write_active_json(tmp_path / "active.json", [])


def test_five_dispatches_to_the_same_pr_anchor_one_started_at(SS, monkeypatch, tmp_path):
    """THE MOTIVATING CASE, measured on the parent issue (comment 1211): one PR
    taken through five review rounds must read as one row with one start time,
    not five, or ninety minutes of continuous work reads as under twenty."""
    _setup_derived_env(SS, monkeypatch, tmp_path)
    runs_root = tmp_path / "runs"
    day = "2026-08-29"
    cwd = "/srv/convivy/agent-platform"
    cache_path = tmp_path / "cache" / "working-derived-deck.json"

    started_ats = []
    for round_n in range(5):
        run_id = f"run-round{round_n}"
        task = f"review round {round_n}"
        _write_run_meta(runs_root, run_id, day, task=task, repo="agent-platform",
                         pr=123, co_session="sess-abc")
        _write_active_json(tmp_path / "active.json", [
            {"run_id": run_id, "agent": "reviewer", "task": task,
             "started_at": "2026-08-29T10:00:00Z", "repo": "agent-platform",
             "worktree": "/tmp/wt", "pid": 111}])

        got = SS.working_on_derived("deck", cwd=cwd, session_id="sess-abc")
        assert got["text"] == task
        assert got["identity_kind"] == "pr"
        started_ats.append(json.loads(cache_path.read_text())["started_at"])

    # THE ACTUAL PIN: all five rounds, despite five different run ids and five
    # different task strings, share the exact same on-disk `started_at` — not
    # merely "the last one differs from a later, genuinely different PR" (true
    # under wall-clock time regardless of whether this mechanism exists at
    # all), which is why this is asserted round-by-round rather than only at
    # the end.
    assert len(set(started_ats)) == 1, \
        f"five rounds on the same PR must anchor one started_at, got {started_ats}"
    first_started = started_ats[0]

    # A DIFFERENT pr is genuinely new work, and only this must move the clock.
    _write_run_meta(runs_root, "run-newpr", day, task="coder on a fresh PR",
                     repo="agent-platform", pr=456, co_session="sess-abc")
    _write_active_json(tmp_path / "active.json", [
        {"run_id": "run-newpr", "agent": "coder", "task": "coder on a fresh PR",
         "started_at": "2026-08-29T10:00:00Z", "repo": "agent-platform",
         "worktree": "/tmp/wt", "pid": 222}])
    SS.working_on_derived("deck", cwd=cwd, session_id="sess-abc")

    second_started = json.loads(cache_path.read_text())["started_at"]
    assert second_started != first_started, "a different PR is new work"


def test_no_identity_ever_derived_serves_text_but_no_duration(SS, monkeypatch, tmp_path):
    """Constraint 4: text may exist (a dispatch says what is happening) while
    no discrete identity ever resolved for it (no `--pr`, no branch). That
    must report NO duration — never zero, never a stale timestamp, both of
    which are worse than an honest absence."""
    _setup_derived_env(SS, monkeypatch, tmp_path)
    runs_root = tmp_path / "runs"
    day = "2026-08-29"
    _write_run_meta(runs_root, "run-x", day, task="poking at something",
                     repo="agent-platform", pr=None, co_session="sess-abc")
    _write_active_json(tmp_path / "active.json", [
        {"run_id": "run-x", "agent": "coder", "task": "poking at something",
         "started_at": "2026-08-29T10:00:00Z", "repo": "agent-platform",
         "worktree": "/tmp/wt", "pid": 111}])

    got = SS.working_on_derived("deck", cwd=str(tmp_path / "not-a-git-checkout"),
                                 session_id="sess-abc")
    assert got["text"] == "poking at something"
    assert got["identity_kind"] is None
    assert got["since"] is None, "no identity ever resolved, so no duration"


def test_a_session_between_dispatches_keeps_its_started_at(SS, monkeypatch, tmp_path):
    """Constraint 4's other half: once a dispatched run exits, `active.json`
    drops it — that must read as 'between pieces of work', not as blank."""
    _setup_derived_env(SS, monkeypatch, tmp_path)
    runs_root = tmp_path / "runs"
    day = "2026-08-29"
    cwd = "/srv/convivy/agent-platform"
    _write_run_meta(runs_root, "run-x", day, task="reviewing PR 42",
                     repo="agent-platform", pr=42, co_session="sess-abc")
    _write_active_json(tmp_path / "active.json", [
        {"run_id": "run-x", "agent": "reviewer", "task": "reviewing PR 42",
         "started_at": "2026-08-29T10:00:00Z", "repo": "agent-platform",
         "worktree": "/tmp/wt", "pid": 111}])
    first = SS.working_on_derived("deck", cwd=cwd, session_id="sess-abc")
    assert first["identity_kind"] == "pr"
    assert first["since"] is not None

    # The run finished — active.json no longer lists it — and this cwd has no
    # real git checkout, so nothing is observable this poll.
    _write_active_json(tmp_path / "active.json", [])
    second = SS.working_on_derived("deck", cwd=cwd, session_id="sess-abc")
    assert second["text"] == "reviewing PR 42"
    assert second["identity_kind"] == "pr"
    assert second["since"] >= first["since"], "the clock kept running, not reset"


def test_private_crew_serves_the_derived_line_absent_not_partial(SS, monkeypatch, tmp_path):
    """Constraint 5. Overrides this file's autouse `_no_private_crews` stub —
    the one case in this block that needs a REAL positive answer."""
    _setup_derived_env(SS, monkeypatch, tmp_path)
    runs_root = tmp_path / "runs"
    day = "2026-08-29"
    _write_run_meta(runs_root, "run-x", day, task="private crew work",
                     repo="cfo", pr=99, co_session="sess-priv")
    _write_active_json(tmp_path / "active.json", [
        {"run_id": "run-x", "agent": "coder", "task": "private crew work",
         "started_at": "2026-08-29T10:00:00Z", "repo": "cfo",
         "worktree": "/tmp/wt", "pid": 111}])
    monkeypatch.setattr(SS, "private_home_for",
                         lambda cwd: "/srv/convivy/CFO")

    got = SS.working_on_derived("cfo-session", cwd="/srv/convivy/CFO",
                                 session_id="sess-priv")
    assert got == {"text": None, "age": None, "stale": None, "since": None,
                    "derived_from": None, "identity_kind": None}


def test_a_public_session_dispatching_into_a_black_box_leaks_neither_text_nor_identity(
    SS, monkeypatch, tmp_path
):
    """The gate `_crew_may_share` asks "is the READING crew private", which a
    cross-repo session (the Keel is the ordinary case) always passes — it sits
    outside every private home while dispatching straight into one. So the run
    itself must be filtered on its own `repo`, or a black box's task text and
    `repo#pr` reach the fleet-wide payload through a public session's line.

    Asserted as a DELTA against an otherwise identical public dispatch, because
    "the private line is absent" alone passes against an implementation that
    derives nothing at all.
    """
    _setup_derived_env(SS, monkeypatch, tmp_path)
    runs_root = tmp_path / "runs"
    day = "2026-08-29"
    # A PUBLIC session: outside every private home, so the crew gate passes.
    keel_cwd = "/srv/convivy/agent-platform"
    monkeypatch.setattr(SS, "private_home_for", lambda cwd: None)
    monkeypatch.setattr(SS.projects, "private_slugs", lambda: {"cfo"})

    def _dispatch(run_id, repo, task, pr):
        _write_run_meta(runs_root, run_id, day, task=task, repo=repo, pr=pr,
                         co_session="sess-keel")
        _write_active_json(tmp_path / "active.json", [
            {"run_id": run_id, "agent": "coder", "task": task,
             "started_at": "2026-08-29T10:00:00Z", "repo": repo,
             "worktree": "/tmp/wt", "pid": 111}])
        return SS.working_on_derived("keel", cwd=keel_cwd, session_id="sess-keel")

    # The half that must still work — otherwise the assertion below is vacuous.
    public = _dispatch("run-pub", "agent-platform", "public crew work", 123)
    assert public["text"] == "public crew work"
    assert public["identity_kind"] == "pr"

    private = _dispatch("run-priv", "cfo", "SENSITIVE private crew work", 99)

    # The filtered run contributes NOTHING, so the line carries forward the last
    # public work rather than going blank — that is `_fresh_derivation`'s ordinary
    # between-dispatches behaviour, and it is the right shape here: the session IS
    # still working, just not on anything it may say. The property under test is
    # that the black box's text and slug never appear, not that the line empties.
    assert private["text"] == "public crew work", \
        "the private dispatch must not become this session's line"
    assert "SENSITIVE" not in json.dumps(private), \
        "a black box's task text must not reach the shared payload"
    assert "cfo" not in json.dumps(private).lower(), \
        "nor its repo slug, which is a roster row on its own (#464)"


def test_agents_dir_bypass_leaks_neither_text_nor_identity(SS, monkeypatch, tmp_path):
    """The bypass security-reviewer found in round 2 of this PR (run-089d9dca):
    `repo` alone can't say a dispatch reached a black box, because
    `resolve_agent()` (bin/agentctl) honours `--agents-dir` before it ever
    looks at `repo` — so `--on constance --agents-dir
    ~/Convivy/CFO/.claude/agents` writes `repo: "constance"` (a public slug)
    into `meta.json` while loading a CFO agent. `meta["agent_source"]` (the
    file `resolve_agent()` actually read) is the second signal that catches
    it, checked against every private crew's `home_path()`.

    Asserted as a DELTA against an otherwise identical fully-public dispatch,
    same as the sibling `repo`-string test above — "the private line is
    absent" alone passes against an implementation that derives nothing at
    all. `repo` is "constance" (non-private) for BOTH dispatches here, so a
    repo-only check cannot distinguish them; only `agent_source` can.
    """
    _setup_derived_env(SS, monkeypatch, tmp_path)
    runs_root = tmp_path / "runs"
    day = "2026-08-29"
    keel_cwd = "/srv/convivy/agent-platform"
    monkeypatch.setattr(SS, "private_home_for", lambda cwd: None)
    monkeypatch.setattr(SS.projects, "private_slugs", lambda: {"private-crew"})

    private_home = tmp_path / "private-crew-home"
    private_agents_dir = private_home / ".claude" / "agents"
    private_agents_dir.mkdir(parents=True)
    monkeypatch.setattr(SS.projects, "home_path",
                         lambda slug: private_home if slug == "private-crew" else None)

    # The PUBLIC dispatch's agent_source lives OUTSIDE the private home — the
    # shape a shared-knowledge-index resolution actually produces.
    public_agents_dir = tmp_path / "public-agents"
    public_agents_dir.mkdir()

    def _dispatch(run_id, task, pr, agent_source):
        _write_run_meta(runs_root, run_id, day, task=task, repo="constance", pr=pr,
                         co_session="sess-keel", agent_source=agent_source)
        _write_active_json(tmp_path / "active.json", [
            {"run_id": run_id, "agent": "coder", "task": task,
             "started_at": "2026-08-29T10:00:00Z", "repo": "constance",
             "worktree": "/tmp/wt", "pid": 111}])
        return SS.working_on_derived("keel", cwd=keel_cwd, session_id="sess-keel")

    # The half that must still work — otherwise the assertion below is vacuous.
    public = _dispatch("run-pub2", "public crew work via constance", 321,
                        str(public_agents_dir / "coder.md"))
    assert public["text"] == "public crew work via constance"
    assert public["identity_kind"] == "pr"

    private = _dispatch("run-priv2", "SENSITIVE constance-labelled work", 99,
                         str(private_agents_dir / "cfo-classify.md"))

    assert private["text"] == "public crew work via constance", \
        "the --agents-dir dispatch must not become this session's line"
    assert "SENSITIVE" not in json.dumps(private), \
        "a black box's task text must not reach the shared payload via --agents-dir"


def test_unreadable_registry_withholds_a_dispatch_rather_than_leaking_or_crashing(
    SS, monkeypatch, tmp_path
):
    """`projects.private_slugs()` (`units()` → `load()` → `_load_raw()`) can
    raise `FileNotFoundError`/`json.JSONDecodeError` straight through
    `_dispatched_by` if nothing there catches it. That must fail closed on
    the ONE run it can no longer vouch for — never crash `working_on_derived`,
    and never fall through to treating an unverifiable dispatch as public.
    """
    _setup_derived_env(SS, monkeypatch, tmp_path)
    runs_root = tmp_path / "runs"
    day = "2026-08-29"
    cwd = "/srv/convivy/agent-platform"
    monkeypatch.setattr(SS, "private_home_for", lambda cwd: None)

    def _boom():
        raise FileNotFoundError("config/projects.json")
    monkeypatch.setattr(SS.projects, "private_slugs", _boom)

    _write_run_meta(runs_root, "run-x", day, task="could be anything",
                     repo="agent-platform", pr=123, co_session="sess-abc")
    _write_active_json(tmp_path / "active.json", [
        {"run_id": "run-x", "agent": "coder", "task": "could be anything",
         "started_at": "2026-08-29T10:00:00Z", "repo": "agent-platform",
         "worktree": "/tmp/wt", "pid": 111}])

    # Must not raise — an unreadable registry degrades this poll, it does not
    # crash the caller.
    got = SS.working_on_derived("deck", cwd=cwd, session_id="sess-abc")
    assert got["text"] is None, \
        "a run this poll cannot vouch for must not become the session's line"
    assert got["identity_kind"] is None


def test_an_unresolvable_cwd_also_fails_the_privacy_gate_closed(SS, monkeypatch, tmp_path):
    """`_crew_may_share` cannot tell a public session from a private one with
    no `cwd` at all — the same posture `_hook_log_root` already takes — so it
    must withhold rather than guess "not private"."""
    _setup_derived_env(SS, monkeypatch, tmp_path)
    monkeypatch.setattr(SS, "private_home_for", lambda cwd: SS.UNKNOWN)
    got = SS.working_on_derived("deck", cwd=None, session_id="sess-abc")
    assert got["text"] is None


def test_working_on_derived_is_additive_alongside_the_authored_field(SS, monkeypatch, tmp_path):
    """Deck already decodes `working_on`; this ships on a NEW key, never a
    retype of the existing one — the same rule agent-platform#464 follows for
    `agents`/`agent_runs`, so an older decoder keeps working unmodified."""
    _setup_derived_env(SS, monkeypatch, tmp_path)
    monkeypatch.setattr(SS.TT, "claude_pane", lambda n: ("%1", 4242))
    monkeypatch.setattr(SS.TT, "meta_for_pid", lambda pid, days=3: {
        "session_id": "aaaa", "cwd": "/srv/convivy/convivy-deck",
        "transcript_path": str(tmp_path / "t.jsonl")})
    monkeypatch.setattr(SS, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(SS, "_hook_logs", lambda sid, cwd=None, days=3: [])
    (tmp_path / "cache" / "working-deck.json").parent.mkdir(exist_ok=True)
    (tmp_path / "cache" / "working-deck.json").write_text(json.dumps({
        "text": "reviewing the cockpit PR", "written_at": 1_000_000_000}))

    full = SS.session_state("deck")
    assert full["working_on"]["text"] == "reviewing the cockpit PR", \
        "the authored field must be untouched"
    assert set(full["working_on_derived"]) == {
        "text", "age", "stale", "since", "derived_from", "identity_kind"}


# --- `type`: text into a live picker, without answering it --------------------
#
# The verb exists because `send_message` refuses at a blocked session and
# `send_key` carries no literal text, so the picker's `Type something.` row was
# fillable from a terminal and from nowhere else. convivy-deck #54 tried it with
# `send_message`, failed every time, and was reverted.


def test_type_pastes_without_submitting(SI, tmux, session_is):
    """THE WHOLE POINT: no clear, and no trailing Enter.

    Asserted on the call sequence rather than on the outcome, like every other
    test here, because the defect in this verb would be a correct set of calls
    with an `Enter` on the end.
    """
    session_is("blocked")
    assert SI.type_text("s", "fast but only on the flat bits") == "%9"

    verbs = [argv[1] for argv, _ in tmux if argv[0] == "tmux"]
    assert "paste-buffer" in verbs, "the text never reached the pane"
    assert "send-keys" not in verbs, "a keystroke was sent; this verb must not"
    assert "load-buffer" in verbs

    paste = next(argv for argv, _ in tmux if argv[1] == "paste-buffer")
    assert "-p" in paste, "not a bracketed paste, so `/` would open the slash menu"


def test_type_stages_the_text_on_stdin(SI, tmux, session_is):
    """Never on argv, so it is not visible in the process table."""
    session_is("blocked")
    SI.type_text("s", "a note with a $dollar and a `backtick`")
    load = next((argv, stdin) for argv, stdin in tmux if argv[1] == "load-buffer")
    assert load[1] == "a note with a $dollar and a `backtick`"
    assert "a note" not in " ".join(load[0])


@pytest.mark.parametrize("state", ["idle", "working", "waiting", "ended"])
def test_type_is_refused_unless_the_session_is_blocked(SI, tmux, session_is, state):
    """The inverse of `send_message`'s guard, and the inversion is the point.

    A paste with no Enter into an ordinary input box leaves a half-written
    message sitting there for the next keystroke to submit.
    """
    session_is(state)
    with pytest.raises(SI.InputError) as e:
        SI.type_text("s", "text")
    assert state in str(e.value)
    assert not tmux, "keystrokes were spent before the state was checked"


def test_type_refuses_an_empty_string(SI, tmux, session_is):
    session_is("blocked")
    for text in ("", "   ", "\n\t "):
        with pytest.raises(SI.InputError):
            SI.type_text("s", text)
    assert not tmux


def test_type_refuses_control_characters(SI, tmux, session_is):
    """Same gate `send_message` applies, and before the pane is resolved."""
    session_is("blocked")
    with pytest.raises(SI.InputError):
        SI.type_text("s", "text\x03with an interrupt in it")
    assert not tmux


# MARK: - tool_use_id fold: PreToolUse -> PermissionRequest


def test_pre_tool_use_id_is_folded_into_the_gate(SS):
    """THE WHOLE POINT. `PermissionRequest` carries no `tool_use_id`, but the
    `PreToolUse` that precedes it does. Folding it forward gives Deck a stable
    identity to bind a keystroke against. The code pairs on `tool_name` with no
    time window, so the measured gap (min 24ms, median 35ms, max 535ms) does not
    matter — no threshold could go wrong."""
    got = SS.state_from_events([
        _ev("PreToolUse", tool_name="AskUserQuestion",
            tool_use_id="toolu_01ABC", tool_input={"questions": []}),
        _ev("PermissionRequest", tool_name="AskUserQuestion",
            tool_input={"questions": [{"question": "ok?", "header": "H"}]}),
    ])
    assert got["state"] == "blocked"
    assert got["gate"]["gateId"] == "toolu_01ABC"


def test_tool_name_mismatch_does_not_fold(SS):
    """A PreToolUse for Bash followed by a PermissionRequest for AskUserQuestion
    is not a pair. Folding the wrong id would bind a keystroke to a different
    tool's identity, which is worse than no id at all."""
    got = SS.state_from_events([
        _ev("PreToolUse", tool_name="Bash",
            tool_use_id="toolu_01BASH", tool_input={"command": "ls"}),
        _ev("PermissionRequest", tool_name="AskUserQuestion",
            tool_input={"questions": []}),
    ])
    assert got["state"] == "blocked"
    assert "gateId" not in got["gate"]


def test_non_blocked_transition_clears_the_pending_id(SS):
    """A PreToolUse whose PermissionRequest never arrives because the session moved
    on must not leak onto a later gate. The id is cleared on any non-blocked
    transition alongside the existing gate-clearing."""
    got = SS.state_from_events([
        _ev("PreToolUse", tool_name="AskUserQuestion",
            tool_use_id="toolu_stale", tool_input={}),
        _ev("Stop"),   # non-blocked transition — clears the pending id
        _ev("PermissionRequest", tool_name="AskUserQuestion",
            tool_input={"questions": []}),
    ])
    assert got["state"] == "blocked"
    assert "gateId" not in got["gate"]


def test_absent_tool_use_id_produces_no_gate_id_key(SS):
    """A payload with no `tool_use_id` leaves the field off the gate entirely.
    The absence of the key is the contract, not a None value — a key set to None
    would read as "was in the stream, came back empty"."""
    got = SS.state_from_events([
        _ev("PreToolUse", tool_name="AskUserQuestion",
            tool_input={"questions": []}),   # no tool_use_id
        _ev("PermissionRequest", tool_name="AskUserQuestion",
            tool_input={"questions": []}),
    ])
    assert got["state"] == "blocked"
    assert "gateId" not in got["gate"]


def test_gate_id_is_stamped_on_both_gate_kinds(SS):
    """The fold mechanism works for any gate kind, exercised here with a permission
    gate. In a real event stream, permission-class gates always degrade permissively
    for keystroke binding: the PreToolUse hook matcher is narrowed to
    AskUserQuestion|ExitPlanMode, so a Bash PermissionRequest has no preceding
    PreToolUse in the stream and carries no gateId. This test uses a synthetic pair
    to verify the fold itself, not a guarantee about real streams."""
    got = SS.state_from_events([
        _ev("PreToolUse", tool_name="Bash",
            tool_use_id="toolu_01PERM", tool_input={"command": "ls"}),
        _ev("PermissionRequest", tool_name="Bash",
            tool_input={"command": "ls", "description": "list"},
            permission_suggestions=[]),
    ])
    assert got["state"] == "blocked"
    assert got["gate"]["kind"] == "permission"
    assert got["gate"]["gateId"] == "toolu_01PERM"


# MARK: - expectation check: binding keystrokes to the gate they were aimed at


def _fake_ss(monkeypatch, SI, gate_id=None, rendered=None):
    """Patch _SS.session_state so _check_expectation reads a controlled gate.

    gate_id=None means the gateId key is absent.  rendered, when supplied, is
    set as gate["rendered"] so questionIndex is reachable from the fetched state
    rather than from a second picker_render.read() call.
    """
    gate = {}
    if gate_id is not None:
        gate["gateId"] = gate_id
    if rendered is not None:
        gate["rendered"] = rendered
    monkeypatch.setattr(SI._SS, "session_state", lambda name: {"gate": gate})


def test_both_none_is_a_no_op_and_no_state_is_read(SI, tmux, session_is, monkeypatch):
    """Both None means "no expectation". The check must be a no-op so every
    existing caller works unchanged with no added latency."""
    session_is("blocked")

    def boom(name):
        raise AssertionError("session_state.session_state was called for a "
                             "no-expectation check; the early return is broken")

    monkeypatch.setattr(SI._SS, "session_state", boom)

    # picker_render.read IS expected here — answer_choice reads it for its own
    # digitBecomesText guard, which predates this change. Only the session_state
    # read inside _check_expectation must not fire.
    monkeypatch.setattr(SI.picker_render, "read", lambda name: {})

    SI.answer_choice("deck", 1)   # no expect_gate, no expect_question
    assert _keys(tmux) == ["1"]


def test_matching_gate_id_sends(SI, tmux, session_is, monkeypatch):
    """Observed == expected: the keystroke goes out."""
    session_is("blocked")
    _fake_ss(monkeypatch, SI, gate_id="toolu_01MATCH")
    monkeypatch.setattr(SI.picker_render, "read", lambda name: {})
    SI.answer_choice("deck", 2, expect_gate="toolu_01MATCH")
    assert _keys(tmux) == ["2"]


def test_mismatched_gate_id_raises_before_the_keystroke(SI, tmux, session_is, monkeypatch):
    """THE GUARD. The caller observed gate A; the mini is showing gate B. The
    keystroke would answer the wrong question."""
    session_is("blocked")
    _fake_ss(monkeypatch, SI, gate_id="toolu_01OTHER")
    monkeypatch.setattr(SI.picker_render, "read", lambda name: {})
    with pytest.raises(SI.InputError, match="toolu_01EXPECTED"):
        SI.answer_choice("deck", 1, expect_gate="toolu_01EXPECTED")
    assert _keys(tmux) == []


def test_mismatched_question_index_raises_before_the_keystroke(SI, tmux, session_is,
                                                                monkeypatch):
    """The picker advanced to a different question before the keystroke landed.
    questionIndex is read from gate["rendered"], not from a second
    picker_render.read() call — one observation, both fields."""
    session_is("blocked")
    _fake_ss(monkeypatch, SI, rendered={"questionIndex": 2})
    monkeypatch.setattr(SI.picker_render, "read", lambda name: {})
    with pytest.raises(SI.InputError, match="question"):
        SI.answer_choice("deck", 3, expect_question=1)
    assert _keys(tmux) == []


def test_absent_gate_id_on_mini_sends_anyway(SI, tmux, session_is, monkeypatch):
    """The gate carries no gateId — an unrecognised Claude Code version, a failed
    capture, or a build that predates this change. The verb goes out as before.
    Refusing here would cost it on a version bump."""
    session_is("blocked")
    _fake_ss(monkeypatch, SI, gate_id=None)   # gateId key is absent
    monkeypatch.setattr(SI.picker_render, "read", lambda name: {})
    SI.answer_choice("deck", 4, expect_gate="toolu_01EXPECTED")
    assert _keys(tmux) == ["4"]


def test_absent_question_index_on_mini_sends_anyway(SI, tmux, session_is, monkeypatch):
    """No rendered field in the gate means questionIndex is absent. Same trade
    as absent gateId — the verb goes out rather than refusing on uncertainty."""
    session_is("blocked")
    _fake_ss(monkeypatch, SI, gate_id="x")   # no rendered key, so no questionIndex
    monkeypatch.setattr(SI.picker_render, "read", lambda name: {})
    SI.answer_choice("deck", 5, expect_question=0)
    assert _keys(tmux) == ["5"]


def test_check_runs_on_submit_type_and_focus_free_text(SI, tmux, session_is, monkeypatch):
    """The guard covers all four verbs, not just `answer_choice`.

    `submit_choices`, `type_text`, and `focus_free_text` all route through the
    same helper, and none of them must send a keystroke before the check fires.
    """
    _fake_ss(monkeypatch, SI, gate_id="toolu_01OTHER")
    monkeypatch.setattr(SI.picker_render, "read", lambda name: {"freeTextRow": 4})
    monkeypatch.setattr(SI.time, "sleep", lambda _s: None)
    session_is("blocked")

    verbs = [
        lambda: SI.type_text("deck", "text", expect_gate="toolu_01EXPECTED"),
        lambda: SI.submit_choices("deck", expect_gate="toolu_01EXPECTED"),
        lambda: SI.focus_free_text("deck", expect_gate="toolu_01EXPECTED"),
    ]
    for verb in verbs:
        tmux.clear()
        with pytest.raises(SI.InputError, match="toolu_01EXPECTED"):
            verb()
        assert _keys(tmux) == [], "a keystroke went out before the mismatch was caught"


def test_check_expectation_reads_picker_render_exactly_once(SI, tmux, session_is,
                                                             monkeypatch):
    """_check_expectation must not call picker_render.read() a second time.

    `questionIndex` is read from gate["rendered"], which session_state() already
    attaches to a question-kind gate. Two picker_render.read() calls milliseconds
    apart can describe screen states that never coexisted — that is the race class
    this feature exists to close. One call (the pre-existing digitBecomesText guard
    in answer_choice) is correct; two is the pre-fix bug."""
    session_is("blocked")
    _fake_ss(monkeypatch, SI, gate_id="g1", rendered={"questionIndex": 0})
    read_calls = []
    monkeypatch.setattr(SI.picker_render, "read",
                        lambda name: (read_calls.append(1) or {}))
    SI.answer_choice("deck", 1, expect_gate="g1", expect_question=0)
    assert _keys(tmux) == ["1"]
    assert len(read_calls) == 1, (
        f"picker_render.read called {len(read_calls)} time(s); expected exactly 1 "
        "(the digitBecomesText guard in answer_choice, zero times inside "
        "_check_expectation)"
    )


def test_a_plain_text_box_shape_is_not_the_input_box(SI):
    """Two lines of `─` around chosen text are not a box the TUI drew.

    The shape alone was the whole check until 2026-08-10, and pane content is
    partly written by the session itself — so a capture with no escape bytes
    anywhere was read back verbatim as the live box's contents. The border's own
    colour, tested by parsing the SGR rather than by a substring, is what makes
    it evidence. Found in security review of agent-platform #387.
    """
    forged = "\n".join(["─" * 40, "❯\xa0a message nobody typed", "─" * 40])
    assert "\x1b" not in forged
    assert SI._parse_input_box(forged) is None


def test_a_real_bordered_box_still_parses(SI):
    """The same shape drawn in the colour observed on 2.1.227, both captures."""
    rule = "\x1b[38;5;246m" + "─" * 40 + "\x1b[39m"
    real = "\n".join([rule, "❯\xa0a message someone typed", rule])
    assert SI._parse_input_box(real) == "a message someone typed"


def test_drawn_with_wants_a_real_sequence(SI):
    assert SI._drawn_with("\x1b[38;5;246m─\x1b[39m", "38;5;246")
    assert SI._drawn_with("\x1b[2;38;5;246m─\x1b[0m", "38;5;246")
    assert not SI._drawn_with("a rule coloured 38;5;246 here", "38;5;246")


def test_drawn_with_consumes_the_extended_colour_compounds(SI):
    """The same walk `picker_render._sgr_operations` does, and the same reasons.

    No caller here searches for a bare digit today — every member of
    `_BOX_BORDER_FG_CODES` is itself a 3-token compound, which a leaked token
    can never equal — so the misread this
    guards against has no trigger in this module yet. It is tested anyway,
    because the asymmetry is what makes the day someone adds a single-token
    parameter search here dangerous rather than merely new.
    """
    # A 256-colour index is not the attribute its digits resemble.
    assert not SI._drawn_with("\x1b[38;5;1mred\x1b[0m", "1")
    assert SI._drawn_with("\x1b[38;5;1mred\x1b[0m", "38;5;1")
    # The walk does not swallow a real attribute following a complete compound.
    assert SI._drawn_with("\x1b[38;5;246;1m─\x1b[0m", "1")
    assert SI._drawn_with("\x1b[38;5;246;1m─\x1b[0m", "38;5;246")


def test_a_truncated_compound_cannot_forge_an_attribute_here_either(SI):
    """`ESC[38;2;1m` claims true colour and supplies one of three components.

    An extended-colour lead token without enough tokens to complete the compound
    is an INCONCLUSIVE read of what the sequence sets, not "no compound, so treat
    the pieces as ordinary attributes" — so the walk stops rather than yielding
    them. Found in security review of agent-platform #387, where the same
    fallthrough in `picker_render` forged the bold that identifies the commit
    row.
    """
    assert not SI._drawn_with("\x1b[38;2;1m─\x1b[0m", "1")
    assert not SI._drawn_with("\x1b[38;1m─\x1b[0m", "1")
    assert not SI._drawn_with("\x1b[38;5m─\x1b[0m", "5")


# MARK: - unqueue: pulling Claude Code's own held queue back out


def _qop(operation, content=None):
    rec = {"type": "queue-operation", "operation": operation}
    if content is not None:
        rec["content"] = content
    return rec


def test_popall_messages_collects_content_in_order(SI):
    tail = "\n".join(json.dumps(r) for r in [
        _qop("enqueue", "a"),
        _qop("popAll", "first"),
        _qop("dequeue"),
        _qop("popAll", "second"),
    ])
    assert SI._popall_messages(tail) == ["first", "second"]


def test_popall_messages_on_an_empty_tail(SI):
    assert SI._popall_messages("") == []
    assert SI._popall_messages("\n\n  \n") == []


def test_popall_messages_skips_malformed_records_rather_than_raising(SI):
    tail = "\n".join([
        "not json at all {{{",
        json.dumps(["queue-operation", "popAll"]),      # not an object
        json.dumps(_qop("popAll")),                       # no content at all
        json.dumps({**_qop("popAll"), "content": 7}),     # content not a string
        json.dumps({"type": "other", "operation": "popAll", "content": "wrong type"}),
        json.dumps(_qop("popAll", "kept")),
    ])
    assert SI._popall_messages(tail) == ["kept"]


def test_read_transcript_tail_ignores_bytes_before_the_offset(SI, tmp_path):
    """The offset is what stops an earlier gesture's `popAll` — or anything
    else already in the file — from being read as this press's answer."""
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps(_qop("popAll", "stale")) + "\n")
    offset = t.stat().st_size
    with open(t, "a") as fh:
        fh.write(json.dumps(_qop("popAll", "fresh")) + "\n")
    tail = SI._read_transcript_tail(t, offset)
    assert SI._popall_messages(tail) == ["fresh"]


def test_read_transcript_tail_returns_none_on_a_missing_file(SI, tmp_path):
    assert SI._read_transcript_tail(tmp_path / "nope.jsonl", 0) is None


# MARK: - slate-send's receipt: confirming delivery in the transcript (CQ-420)


def _user_rec(content):
    return {"type": "user", "message": {"content": content}}


def test_record_first_line_reads_a_plain_user_record(SI):
    assert SI._record_first_line(_user_rec("hello\nmore")) == "hello"


def test_record_first_line_reads_a_user_record_with_block_list_content(SI):
    rec = _user_rec([{"type": "text", "text": "hello\nmore"},
                     {"type": "text", "text": "ignored second block"}])
    assert SI._record_first_line(rec) == "hello"


def test_record_first_line_reads_a_queue_operation_record(SI):
    assert SI._record_first_line(_qop("enqueue", "hello\nmore")) == "hello"


def test_record_first_line_is_none_for_other_shapes(SI):
    assert SI._record_first_line({"type": "assistant", "message": {"content": "x"}}) is None
    assert SI._record_first_line({"type": "attachment"}) is None
    assert SI._record_first_line(_user_rec(None)) is None
    assert SI._record_first_line(_qop("enqueue")) is None  # no content key at all


def test_await_delivery_confirms_once_a_matching_record_is_written_after_since(
        SI, tmp_path):
    """The common case: `since` is the offset before the send, and the
    matching record lands after it on the very first read — no polling
    needed, so this needs no clock control at all."""
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps(_user_rec("unrelated earlier turn")) + "\n")
    since = t.stat().st_size
    with open(t, "a") as fh:
        fh.write(json.dumps(_qop("enqueue", "[slate a-1] hello\nmy answer")) + "\n")
    SI.await_delivery("deck", "[slate a-1] hello\nmy answer", t, since)  # no raise


def test_await_delivery_ignores_a_matching_record_that_predates_since(
        SI, tmp_path, monkeypatch):
    """A record already in the transcript before the send must not count as
    this send's receipt — the exact guard `_read_transcript_tail`'s offset
    gives `unqueue` against a stale `popAll`, applied here against a stale
    `user`/`queue-operation` record with a coincidentally matching first
    line."""
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps(_qop("enqueue", "[slate a-1] hello\nmy answer")) + "\n")
    since = t.stat().st_size
    monkeypatch.setattr(SI.time, "sleep", lambda _s: None)
    ticks = iter([0.0] + [SI.SLATE_DELIVERY_DEADLINE_SECONDS + 1.0] * 50)
    monkeypatch.setattr(SI.time, "monotonic", lambda: next(ticks))
    with pytest.raises(SI.InputError, match="could not confirm delivery"):
        SI.await_delivery("deck", "[slate a-1] hello\nmy answer", t, since)


def test_await_delivery_raises_when_the_deadline_passes_with_no_match(
        SI, tmp_path, monkeypatch):
    t = tmp_path / "t.jsonl"
    t.write_text("")
    since = t.stat().st_size
    monkeypatch.setattr(SI.time, "sleep", lambda _s: None)
    ticks = iter([0.0] + [SI.SLATE_DELIVERY_DEADLINE_SECONDS + 1.0] * 50)
    monkeypatch.setattr(SI.time, "monotonic", lambda: next(ticks))
    with pytest.raises(SI.InputError, match="could not confirm delivery"):
        SI.await_delivery("deck", "[slate a-1] hello", t, since)


def test_await_delivery_sees_a_record_that_lands_on_a_later_poll(SI, tmp_path, monkeypatch):
    """The box empties, the write lands a beat later — the same race
    `test_unqueue_waits_out_a_slow_transcript_write_instead_of_giving_up`
    pins for `unqueue`, applied here to the receipt this function confirms
    instead of a `popAll` batch."""
    t = tmp_path / "t.jsonl"
    t.write_text("")
    since = t.stat().st_size
    monkeypatch.setattr(SI.time, "sleep", lambda _s: None)
    reads = {"n": 0}
    real_tail = SI._read_transcript_tail

    def slow_tail(path, offset):
        reads["n"] += 1
        if reads["n"] >= 3:
            with open(t, "a") as fh:
                fh.write(json.dumps(_user_rec("[slate a-1] hello")) + "\n")
        return real_tail(path, offset)

    monkeypatch.setattr(SI, "_read_transcript_tail", slow_tail)
    SI.await_delivery("deck", "[slate a-1] hello", t, since)
    assert reads["n"] >= 3, "the tail must be re-read, not read once and given up on"


def test_transcript_offset_resolves_via_transcript_for_tmux_session(SI, tmp_path, monkeypatch):
    t = tmp_path / "t.jsonl"
    t.write_text("stale")
    monkeypatch.setattr(SI.TT, "transcript_for_tmux_session", lambda name: t)
    transcript, since = SI.transcript_offset("deck")
    assert transcript == t
    assert since == len("stale")


def test_transcript_offset_raises_input_error_when_the_transcript_cannot_be_stat_ed(
        SI, tmp_path, monkeypatch):
    missing = tmp_path / "gone.jsonl"
    monkeypatch.setattr(SI.TT, "transcript_for_tmux_session", lambda name: missing)
    with pytest.raises(SI.InputError, match="could not read the transcript"):
        SI.transcript_offset("deck")


# MARK: - pane agreement (CQ-420): one resolution, not two independent ones
#
# `transcript_offset` and `send_message` used to each resolve the target pane
# on their own. During a rollover handoff two live claude panes share one
# tmux session, and the two resolutions are not the same algorithm:
# `transcript_for_tmux_session` skips a pane whose run-dir record cannot be
# found and falls through to the next one, while `claude_pane` (what
# `send_message`/`resolve_pane` use) returns the first pane with a claude
# process regardless of its record. A fenced predecessor whose record has
# aged out of the search window is exactly that split: `claude_pane` still
# names it, `transcript_for_tmux_session` does not. `resolve_pane` closes
# this by resolving once; `transcript_offset(pane=...)` then reads that one
# pane's transcript rather than independently re-walking the session.


def test_resolve_pane_is_claude_pane(SI, monkeypatch):
    monkeypatch.setattr(SI.TT, "claude_pane", lambda name: ("%9", 4242))
    assert SI.resolve_pane("deck") == ("%9", 4242)


def test_transcript_offset_given_a_pane_reads_that_pane_alone(SI, tmp_path, monkeypatch):
    t = tmp_path / "t.jsonl"
    t.write_text("stale")
    calls = []

    def single_pane(claude_pid, days=SI.TT.SEARCH_DAYS):
        calls.append(claude_pid)
        return t

    monkeypatch.setattr(SI.TT, "transcript_for_claude_pid", single_pane)
    monkeypatch.setattr(SI.TT, "transcript_for_tmux_session",
                        lambda name: pytest.fail("must not independently re-walk the session"))
    transcript, since = SI.transcript_offset("deck", pane=("%9", 4242))
    assert transcript == t
    assert since == len("stale")
    assert calls == [4242]


def test_resolving_the_pane_once_confirms_against_the_pane_actually_sent_to(
        SI, tmp_path, monkeypatch):
    """Two candidate panes, each with its own transcript — the rollover
    shape (ADR-0069 Slice 5): `claude_pane` (what `send_message` and
    `resolve_pane` use) names pane A's pid, while an INDEPENDENT walk of the
    session's panes (`transcript_for_tmux_session`, the old pane-less
    `transcript_offset(name)` call `slate-send` used to make) lands on pane
    B's transcript instead — pane A's own run-dir record no longer resolving
    is exactly what leaves a fenced predecessor's pane findable by
    `claude_pane` but skipped by the independent walk.

    Resolving the pane ONCE via `resolve_pane` and passing that identity to
    `transcript_offset` (the shape `bin/session-agent`'s `slate-send` now
    uses) must make the receipt read pane A's transcript, the one
    `send_message` was actually given — never pane B's, which is what an
    unpatched, pane-less `transcript_offset("deck")` call would read here."""
    sent_to = tmp_path / "pane-a.jsonl"
    sent_to.write_text("")
    other_pane = tmp_path / "pane-b.jsonl"
    other_pane.write_text("")
    monkeypatch.setattr(SI.TT, "claude_pane", lambda name: ("%1", 100))
    monkeypatch.setattr(SI.TT, "transcript_for_claude_pid",
                        lambda pid, days=SI.TT.SEARCH_DAYS: sent_to if pid == 100 else other_pane)
    # The independent walk is still here, STILL pointing at the other pane —
    # proving the fix never reaches it rather than merely removing the decoy.
    monkeypatch.setattr(SI.TT, "transcript_for_tmux_session", lambda name: other_pane)

    pane = SI.resolve_pane("deck")
    transcript, _since = SI.transcript_offset("deck", pane=pane)

    assert pane[0] == "%1"
    assert transcript == sent_to


@pytest.fixture
def unqueue_env(SI, monkeypatch, tmp_path):
    """Wire `unqueue`'s collaborators for a controlled read-verify-act.

    The transcript is a REAL file, not a mock: `unqueue` reads it by byte
    offset, so the only faithful way to test the offset behaviour is an
    actual file that grows between the note and the read. `on_up["lines"]`
    is what a real Claude Code session would append to the transcript in
    response to the `Up` keystroke — written here the instant that
    keystroke is seen, mirroring the production race this verb exists to
    close (the write happens after the press, never before).
    """
    calls = []
    box = {"text": ""}
    on_up = {"lines": []}
    t = tmp_path / "t.jsonl"
    t.write_text("")

    def fake_run(argv, stdin=None):
        calls.append((tuple(argv), stdin))
        if len(argv) > 1 and argv[1] == "display-message":
            return SI.INPUT_BOX_VERIFIED_FLOOR
        if len(argv) > 1 and argv[1] == "capture-pane":
            return _capture_with_box(box["text"])
        if len(argv) > 1 and argv[1] == "send-keys" and argv[-1] == "Up":
            with open(t, "a") as fh:
                for rec in on_up["lines"]:
                    fh.write(json.dumps(rec) + "\n")
        return ""

    monkeypatch.setattr(SI.TT, "run", fake_run)
    monkeypatch.setattr(SI.TT, "claude_pane", lambda name: ("%9", 4242))
    monkeypatch.setattr(SI.TT, "transcript_for_tmux_session", lambda name: t)
    monkeypatch.setattr(SI.time, "sleep", lambda _s: None)
    return {"calls": calls, "box": box, "on_up": on_up, "transcript": t}


def test_unqueue_pops_messages_in_order_and_clears_the_box(SI, unqueue_env):
    unqueue_env["on_up"]["lines"] = [
        _qop("popAll", "first"), _qop("popAll", "second")]
    result = SI.unqueue("deck")
    assert result == {"messages": ["first", "second"]}
    assert _keys(unqueue_env["calls"]) == \
        ["Up", SI.KEYS["clear"]], (
            "the clear is ONE tmux call carrying every press (see "
            "test_the_clear_is_one_call_carrying_every_press), so `_keys` "
            "sees its trailing key once, not CLEAR_KEYSTROKES times")


def test_unqueue_reports_an_empty_list_and_clears_nothing_when_up_did_nothing(
        SI, unqueue_env, monkeypatch):
    """No `popAll`, and the box still parses as empty: the keystroke changed
    nothing. Positively known, so the answer is an empty list — and there is
    nothing to clear, which is asserted rather than assumed. A clear here
    would be twelve keystrokes into a box that never needed them.

    The clock is driven rather than slept through (see
    test_unqueue_stops_polling_once_the_deadline_passes), so this pins the
    outcome without spending the real `POPALL_DEADLINE_SECONDS`."""
    unqueue_env["on_up"]["lines"] = []
    ticks = iter([0.0, 0.0] + [SI.POPALL_DEADLINE_SECONDS + 1.0] * 50)
    monkeypatch.setattr(SI.time, "monotonic", lambda: next(ticks))
    result = SI.unqueue("deck")
    assert result == {"messages": []}
    assert _keys(unqueue_env["calls"]) == ["Up"]


def test_unqueue_ignores_a_popall_that_predates_the_press(SI, unqueue_env, monkeypatch):
    """The clock is driven rather than slept through (see
    test_unqueue_stops_polling_once_the_deadline_passes), so this pins the
    outcome without spending the real `POPALL_DEADLINE_SECONDS`."""
    with open(unqueue_env["transcript"], "a") as fh:
        fh.write(json.dumps(_qop("popAll", "stale")) + "\n")
    unqueue_env["on_up"]["lines"] = []
    ticks = iter([0.0, 0.0] + [SI.POPALL_DEADLINE_SECONDS + 1.0] * 50)
    monkeypatch.setattr(SI.time, "monotonic", lambda: next(ticks))
    result = SI.unqueue("deck")
    assert result == {"messages": []}


def test_unqueue_refuses_a_non_empty_box_and_sends_nothing(SI, unqueue_env):
    """M5: `Up` against a live draft destroys it. The draft is the caller's,
    not this verb's to replace, so nothing is pressed at all."""
    unqueue_env["box"]["text"] = "a draft nobody asked to lose"
    with pytest.raises(SI.InputError, match="not empty"):
        SI.unqueue("deck")
    assert _keys(unqueue_env["calls"]) == []


def test_unqueue_refuses_when_the_box_cannot_be_read(SI, unqueue_env, monkeypatch):
    """`_read_input_box` returning `None` means refuse HERE — the deliberate
    departure from every other caller of it in this module, which falls back
    to the blind keystroke instead. There is no blind fallback for `Up`:
    unguarded it either destroys a draft or recalls history into one.
    """
    monkeypatch.setattr(SI, "_read_input_box", lambda pane: None)
    with pytest.raises(SI.InputError, match="could not be read"):
        SI.unqueue("deck")
    assert _keys(unqueue_env["calls"]) == []


def test_unqueue_refuses_rather_than_clear_when_the_transcript_goes_dark_after_the_press(
        SI, unqueue_env, monkeypatch):
    """The one place this verb fails closed on its OWN cleanup rather than on
    the caller's keystroke. `Up` already went out — the module's usual
    OBSERVED-ABSENT doctrine does not reach back and unsend it — but clearing
    the box on a guess could destroy a real `popAll` (M3) that merely could
    not be read back, so the clear is what is withheld, not the keystroke.
    """
    monkeypatch.setattr(SI, "_read_transcript_tail", lambda path, offset: None)
    with pytest.raises(SI.InputError, match="transcript could not be read"):
        SI.unqueue("deck")
    assert _keys(unqueue_env["calls"]) == ["Up"], (
        "Up should have gone out before the transcript read failed, and no "
        "clear should follow it")


# The history-recall pane, which is what `Up` draws when the queue has
# already drained (M1). CONSTRUCTED from a real capture taken on the mini
# against Claude Code 2.1.228, in the style of `_capture_with_box` above: the
# TOP rule is replaced by a labelled divider and the bottom rule stays bare,
# which is precisely why `_parse_input_box` cannot find its border pair here.
#
# *border_fg* defaults to `38;5;246` for the same reason `_capture_with_box`'s
# does; pass `38;5;245` for a 2.1.292-shaped capture (CQ-422), the colour
# `_history_recall_drawn` must also match through `_BOX_BORDER_FG_CODES`.
def _capture_with_history_recall(text, label="History 1/1", border_fg="38;5;246"):
    bare = f"\x1b[{border_fg}m" + "─" * 30
    labelled = f"\x1b[{border_fg}m───\x1b[39m \x1b[2m{label}\x1b[0m \x1b[{border_fg}m" + "─" * 26
    return "\n".join(
        ["welcome banner", labelled, *_render_box(text), bare, "footer status"]) + "\n"


@pytest.mark.parametrize("border_fg", ["38;5;246", "38;5;245"])
def test_a_history_recall_is_drawn_with_a_labelled_divider(SI, border_fg):
    assert SI._history_recall_drawn(
        _capture_with_history_recall("an old message", border_fg=border_fg))
    assert SI._history_recall_drawn(
        _capture_with_history_recall("x", "History 3/8", border_fg=border_fg))


@pytest.mark.parametrize("border_fg", ["38;5;246", "38;5;245"])
def test_a_plain_box_is_not_read_as_a_history_recall(SI, border_fg):
    """The pop case (M3) draws both rules bare. Nothing about it may look
    like a recall, or `unqueue` would clear a box holding a popped queue it
    then reports as empty."""
    assert not SI._history_recall_drawn(_capture_with_box("GOLF-one alpha", border_fg=border_fg))
    assert not SI._history_recall_drawn(_capture_with_box("", border_fg=border_fg))
    assert not SI._history_recall_drawn("")


@pytest.mark.parametrize("border_fg", ["38;5;246", "38;5;245"])
def test_a_history_recall_defeats_the_box_parser(SI, border_fg):
    """MEASURED, mini, 2.1.228: `_read_input_box` returns `None` through a
    recall, because the labelled divider is not the bare rule the parser
    anchors on. This is why the absent box is the SYMPTOM the branch keys on
    and the divider is its confirmation — pinned here so a parser change that
    started returning the recalled text would fail loudly rather than quietly
    turn an already-sent message into a 'held' one."""
    assert SI._parse_input_box(
        _capture_with_history_recall("an old message", border_fg=border_fg)) is None


def test_unqueue_clears_a_confirmed_history_recall_and_reports_an_empty_queue(
        SI, unqueue_env, monkeypatch):
    """THE COMMON RACE (M2), and it must not be a refusal. The queue drained
    between the caller's poll and this keystroke, so `Up` recalled history
    instead of popping. Leaving that in the box would be actively harmful:
    `send_message` skips its clear while a session is `working`, so the next
    message would go out with an already-sent one concatenated in front."""
    unqueue_env["on_up"]["lines"] = []
    box = unqueue_env["box"]

    pressed = {"up": False}
    real_run = SI.TT.run

    def run_with_recall_after_up(argv, stdin=None):
        if tuple(argv[:2]) == ("tmux", "capture-pane") and pressed["up"]:
            return _capture_with_history_recall("an already-sent message")
        out = real_run(argv, stdin=stdin)
        if tuple(argv[:2]) == ("tmux", "send-keys") and argv[-1] == "Up":
            pressed["up"] = True
        if tuple(argv[:2]) == ("tmux", "send-keys") and argv[-1] == SI.KEYS["clear"]:
            pressed["up"] = False          # the clear restores the plain box
            box["text"] = ""
        return out

    monkeypatch.setattr(SI.TT, "run", run_with_recall_after_up)
    assert SI.unqueue("deck") == {"messages": []}
    assert _keys(unqueue_env["calls"]) == ["Up", SI.KEYS["clear"]]


def test_unqueue_refuses_when_no_popall_arrived_and_the_pane_is_unexplained(
        SI, unqueue_env, monkeypatch):
    """No `popAll`, no empty box, and no history divider either. That leaves
    a pop whose records never came, and clearing would destroy the only copy
    of what it returned — so the box is left exactly as `Up` drew it.

    The clock is driven rather than slept through (see
    test_unqueue_stops_polling_once_the_deadline_passes), so this pins the
    outcome without spending the real `POPALL_DEADLINE_SECONDS`."""
    unqueue_env["on_up"]["lines"] = []
    ticks = iter([0.0, 0.0] + [SI.POPALL_DEADLINE_SECONDS + 1.0] * 50)
    monkeypatch.setattr(SI.time, "monotonic", lambda: next(ticks))
    pressed = {"up": False}
    real_run = SI.TT.run

    def run_with_unexplained_box_after_up(argv, stdin=None):
        if tuple(argv[:2]) == ("tmux", "capture-pane") and pressed["up"]:
            # Parses fine, holds text, carries no divider.
            return _capture_with_box("text from nowhere")
        out = real_run(argv, stdin=stdin)
        if tuple(argv[:2]) == ("tmux", "send-keys") and argv[-1] == "Up":
            pressed["up"] = True
        return out

    monkeypatch.setattr(SI.TT, "run", run_with_unexplained_box_after_up)
    with pytest.raises(SI.InputError, match="neither an empty box nor a history recall"):
        SI.unqueue("deck")
    assert _keys(unqueue_env["calls"]) == ["Up"], \
        "the box must be left as Up drew it — no clear on an unexplained pane"


def test_unqueue_waits_out_a_slow_transcript_write_instead_of_giving_up(
        SI, unqueue_env, monkeypatch):
    """THE DEFECT THE FIXED SLEEP CARRIED. `popAll` lands 116-140ms after the
    press (measured, mini, 2.1.228, five trials), but a sleep sized to that
    reads the tail exactly once and clears the box on whatever it found. A
    write that lands late then reads as 'nothing popped', and the clear
    destroys the queue. Polling to a deadline sees it on a later read."""
    reads = {"n": 0}
    late = json.dumps(_qop("popAll", "arrived late"))
    real_tail = SI._read_transcript_tail

    def slow_tail(path, offset):
        reads["n"] += 1
        return late if reads["n"] >= 3 else real_tail(path, offset)

    monkeypatch.setattr(SI, "_read_transcript_tail", slow_tail)
    assert SI.unqueue("deck") == {"messages": ["arrived late"]}
    assert reads["n"] >= 3, "the tail must be re-read, not read once after a sleep"
    assert _keys(unqueue_env["calls"]) == ["Up", SI.KEYS["clear"]]


def test_unqueue_stops_polling_once_the_deadline_passes(SI, unqueue_env, monkeypatch):
    """The deadline is what stops a genuinely empty result from spinning. The
    clock is driven rather than slept through, so this pins the bound without
    spending it."""
    unqueue_env["on_up"]["lines"] = []
    ticks = iter([0.0] + [SI.POPALL_DEADLINE_SECONDS + 1.0] * 50)
    monkeypatch.setattr(SI.time, "monotonic", lambda: next(ticks))
    assert SI.unqueue("deck") == {"messages": []}


# The box as Claude Code draws it while a queue exists: the placeholder
# `Press up to edit queued messages`, each WORD dim and the spaces between
# them not. Transcribed from a real `capture-pane -e` on the mini against
# 2.1.228 — the spacing is the point of the fixture, so it is copied rather
# than approximated.
_QUEUE_PLACEHOLDER = (
    "\x1b[38;5;241m❯\xa0\x1b[2m\x1b[39mPress\x1b[0m \x1b[2mup\x1b[0m \x1b[2mto\x1b[0m "
    "\x1b[2medit\x1b[0m \x1b[2mqueued\x1b[0m \x1b[2mmessages\x1b[0m")


def _capture_with_queue_placeholder():
    bare = "\x1b[38;5;246m" + "─" * 30
    return "\n".join(
        ["welcome banner", bare, _QUEUE_PLACEHOLDER, bare, "footer status"]) + "\n"


def test_the_queue_placeholder_leaves_only_its_undimmed_spaces_behind(SI):
    """The dim words go the way a ghost suggestion goes. The spaces BETWEEN
    them are not dim, so they survive — which is the whole trap below."""
    assert SI._parse_input_box(_capture_with_queue_placeholder()) == "     "


def test_unqueue_treats_the_queue_placeholder_as_an_empty_box(SI, unqueue_env, monkeypatch):
    """THE BUG THE UNIT TESTS COULD NOT SEE, found by running the verb against
    a real session. The placeholder is drawn exactly when a queue exists, so
    comparing the parsed box to "" raw made `unqueue` refuse in the one state
    it exists to serve — every constructed test passed, because a constructed
    empty box has no placeholder in it."""
    unqueue_env["on_up"]["lines"] = [_qop("popAll", "ROMEO-one alpha")]
    real_run = SI.TT.run

    def run_with_placeholder(argv, stdin=None):
        if tuple(argv[:2]) == ("tmux", "capture-pane"):
            real_run(argv, stdin=stdin)          # keep the call recorded
            return _capture_with_queue_placeholder()
        return real_run(argv, stdin=stdin)

    monkeypatch.setattr(SI.TT, "run", run_with_placeholder)
    assert SI.unqueue("deck") == {"messages": ["ROMEO-one alpha"]}


def test_unqueue_stops_waiting_as_soon_as_a_recall_is_confirmed(
        SI, unqueue_env, monkeypatch):
    """The common race must not also be the slowest outcome. `Up` either pops
    or recalls, so a confirmed divider means no `popAll` is coming and there
    is nothing left to wait for. Driven on the real clock with a real poll
    interval, so a regression to 'burn the whole deadline' shows up as a
    wall-clock failure rather than a subtle one."""
    unqueue_env["on_up"]["lines"] = []
    monkeypatch.setattr(SI.time, "sleep", time.sleep)      # undo the fixture's no-op
    real_run = SI.TT.run
    pressed = {"up": False}

    def run_with_recall_after_up(argv, stdin=None):
        if tuple(argv[:2]) == ("tmux", "capture-pane") and pressed["up"]:
            return _capture_with_history_recall("an already-sent message")
        out = real_run(argv, stdin=stdin)
        if tuple(argv[:2]) == ("tmux", "send-keys") and argv[-1] == "Up":
            pressed["up"] = True
        return out

    monkeypatch.setattr(SI.TT, "run", run_with_recall_after_up)
    started = time.monotonic()
    assert SI.unqueue("deck") == {"messages": []}
    elapsed = time.monotonic() - started
    assert elapsed < SI.POPALL_DEADLINE_SECONDS / 2, (
        f"took {elapsed:.2f}s — the recall was confirmed but the wait ran on")


def test_unqueue_still_catches_a_recall_that_renders_after_the_deadline(
        SI, unqueue_env, monkeypatch):
    """The backstop after the loop. The in-loop check runs on an interval, so
    a divider that appears between the last one and the deadline would be
    refused as unexplained — and a refusal leaves the recalled text sitting in
    the box, which is the harm the whole branch exists to avoid."""
    unqueue_env["on_up"]["lines"] = []
    seen = {"captures": 0}
    real_run = SI.TT.run

    def run_with_a_late_recall(argv, stdin=None):
        if tuple(argv[:2]) == ("tmux", "capture-pane"):
            seen["captures"] += 1
            # Capture 1 is the pre-press box read, which must see a plain
            # empty box or nothing is pressed at all. Everything after it is
            # post-deadline, where the divider has finally rendered.
            if seen["captures"] > 1:
                return _capture_with_history_recall("an already-sent message")
        return real_run(argv, stdin=stdin)

    # One pass, then straight past the deadline: the loop never reaches its
    # interval check, so only the backstop can catch this.
    ticks = iter([0.0, 0.0] + [SI.POPALL_DEADLINE_SECONDS + 1.0] * 50)
    monkeypatch.setattr(SI.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(SI.TT, "run", run_with_a_late_recall)
    assert SI.unqueue("deck") == {"messages": []}
    assert _keys(unqueue_env["calls"]) == ["Up", SI.KEYS["clear"]], \
        "the late-rendered recall must still be cleared, not refused"


def test_an_undrawn_history_divider_is_not_a_recall(SI):
    """THE FORGERY THE REVIEW CAUGHT. Pane content is partly written by the
    session itself, and the literal `─── History 1/1 ───` appears verbatim in
    this module's own comments and in the KB spec behind it — so a co session
    working on this very code can put that exact text on screen. Shape alone
    must not confirm a recall, or `unqueue` clears a box holding a real popped
    queue. Same guard `_parse_input_box` already demands of its borders."""
    bare = "\x1b[38;5;246m" + "─" * 30
    forged = "─── History 1/1 " + "─" * 20          # no SGR: the session's own text
    capture = "\n".join(
        ["welcome banner", forged, bare, *_render_box("real popped text"), bare,
         "footer status"]) + "\n"
    assert not SI._history_recall_drawn(capture)


def test_a_history_divider_elsewhere_on_screen_is_not_a_recall(SI):
    """Even DRAWN in the border colour, the divider only counts as the box's
    own upper rule. The scan walks up from the bottom-most drawn rule and
    stops at the first rule-shaped line, so a divider further up the
    transcript cannot reach in."""
    bare = "\x1b[38;5;246m" + "─" * 30
    drawn_divider = ("\x1b[38;5;246m───\x1b[39m \x1b[2mHistory 1/1\x1b[0m "
                     "\x1b[38;5;246m" + "─" * 20)
    capture = "\n".join(
        ["welcome banner", drawn_divider, "some transcript in between",
         bare, *_render_box("real popped text"), bare, "footer status"]) + "\n"
    assert not SI._history_recall_drawn(capture)


def test_unqueue_waits_for_a_batch_that_arrives_one_record_at_a_time(
        SI, unqueue_env, monkeypatch):
    """A `popAll` batch is N separate JSONL records, not one atomic write.
    Accepting the first non-empty read would clear the box on M of N and
    destroy the rest, so the count has to stop growing before it commits."""
    reads = {"n": 0}
    recs = [_qop("popAll", "first"), _qop("popAll", "second"), _qop("popAll", "third")]

    def growing_tail(path, offset):
        reads["n"] += 1
        return "\n".join(json.dumps(r) for r in recs[:min(reads["n"], 3)])

    monkeypatch.setattr(SI, "_read_transcript_tail", growing_tail)
    assert SI.unqueue("deck") == {"messages": ["first", "second", "third"]}


def test_unqueue_leaves_the_box_alone_when_a_question_took_it(
        SI, unqueue_env, monkeypatch):
    """Twelve `C-u` into a live picker is an unmeasured interaction, and this
    module removed `C-c` from KEYS entirely over exactly that class of
    surprise. A blocked session withholds the clear."""
    unqueue_env["on_up"]["lines"] = [_qop("popAll", "first")]
    monkeypatch.setattr(SI, "_read_state", lambda name: "blocked")
    # The messages still come back: they are already out of the queue and this
    # is their only copy, so withholding them to avoid a dirty box would be
    # the trade this verb exists to refuse.
    assert SI.unqueue("deck") == {"messages": ["first"]}
    assert _keys(unqueue_env["calls"]) == ["Up"], "no clear may go out at a picker"


def test_only_an_observed_block_withholds_the_clear(SI, unqueue_env, monkeypatch):
    """`_read_state` returns None for every way it can fail to know. Treating
    that as 'a picker is up' would leave dirty boxes on any session whose
    state cannot be read, which is far more common than the substitution it
    guards against."""
    unqueue_env["on_up"]["lines"] = [_qop("popAll", "first")]
    monkeypatch.setattr(SI, "_read_state", lambda name: None)
    assert SI.unqueue("deck") == {"messages": ["first"]}
    assert _keys(unqueue_env["calls"]) == ["Up", SI.KEYS["clear"]]


def test_read_transcript_tail_refuses_a_tail_past_the_cap(SI, tmp_path):
    """An oversized tail is 'could not be read', which routes to the same
    refusal a missing file does — `unqueue` leaves the box alone either way."""
    t = tmp_path / "t.jsonl"
    t.write_bytes(b"x" * (SI._TAIL_MAX_BYTES + 2))
    assert SI._read_transcript_tail(t, 0) is None
    assert SI._read_transcript_tail(t, 2) is not None


def test_unqueue_reports_a_vanished_transcript_cleanly_instead_of_crashing(
        SI, unqueue_env, monkeypatch):
    """A transcript rotated between resolving the path and stat'ing it raises
    OSError, which `bin/session-agent` does not handle — the process would
    exit on a traceback rather than the `{"ok": false, ...}` a consumer
    parses. Nothing is at risk yet; this is still before the keystroke."""
    unqueue_env["transcript"].unlink()
    with pytest.raises(SI.InputError, match="nothing was sent"):
        SI.unqueue("deck")
    assert _keys(unqueue_env["calls"]) == []


def test_an_undrawn_divider_in_the_boxs_own_position_is_not_a_recall(SI):
    """THE CASE THE OTHER TWO FORGERY TESTS MISS, found by mutation: both of
    them put their forged divider OUTSIDE the box's bracket, so the walk's
    scoping rejects them before the SGR check on the History branch is ever
    reached — delete `_drawn_with` from that branch and they both still pass.
    This one puts a shape-correct, UNDRAWN divider exactly where the real one
    sits, which is the only position that reaches the guard."""
    undrawn = "─── History 1/1 " + "─" * 20          # same shape, no SGR at all
    capture = "\n".join(
        ["welcome banner", undrawn, *_render_box("a real popped queue"),
         "\x1b[38;5;246m" + "─" * 30, "footer status"]) + "\n"
    assert not SI._history_recall_drawn(capture)
    # And the same construction WITH the colour is a recall, so the test is
    # pinning the guard rather than some unrelated difference.
    assert SI._history_recall_drawn(_capture_with_history_recall("a real popped queue"))


def test_a_recall_that_cannot_be_cleared_refuses_rather_than_reporting_success(
        SI, unqueue_env, monkeypatch):
    """The other half of the asymmetry. On the pop path a withheld clear still
    returns the messages, because they are already out of the queue. Here there
    is nothing to hand back, so reporting `{"messages": []}` over a box still
    holding recalled text would be reporting success over a dirty box."""
    unqueue_env["on_up"]["lines"] = []
    monkeypatch.setattr(SI, "_read_state", lambda name: "blocked")
    real_run = SI.TT.run
    pressed = {"up": False}

    def run_with_recall_after_up(argv, stdin=None):
        if tuple(argv[:2]) == ("tmux", "capture-pane") and pressed["up"]:
            return _capture_with_history_recall("an already-sent message")
        out = real_run(argv, stdin=stdin)
        if tuple(argv[:2]) == ("tmux", "send-keys") and argv[-1] == "Up":
            pressed["up"] = True
        return out

    monkeypatch.setattr(SI.TT, "run", run_with_recall_after_up)
    with pytest.raises(SI.InputError, match="before the box could be cleared"):
        SI.unqueue("deck")
    assert _keys(unqueue_env["calls"]) == ["Up"], "no clear may go out at a picker"


# MARK: - the third exit condition: a closed channel on a live connection


def test_a_live_reader_is_not_a_hangup(SS):
    """The write end of an open pipe polls `POLLOUT`, never `POLLHUP`."""
    import os
    r, w = os.pipe()
    try:
        with os.fdopen(w, "w") as stream:
            assert not SS._reader_hung_up(stream)
    finally:
        os.close(r)


def test_a_closed_reader_is_a_hangup(SS):
    """THE CASE THE OTHER TWO CHECKS MISS: no write attempted and no change to
    the parent process, yet the reader is provably gone.

    Which event says so is platform-specific — macOS reports POLLHUP (16) on
    the write end, Linux reports POLLERR (8) for the same closed read end — so
    this asserts the condition, not the number. A POLLHUP-only check passes
    this on a developer's mac and fails it in CI, which is how the gap was
    found."""
    import os
    r, w = os.pipe()
    os.close(r)
    with os.fdopen(w, "w") as stream:
        assert SS._reader_hung_up(stream)


def test_a_stream_with_no_descriptor_is_not_a_hangup(SS):
    """A test double or an in-memory buffer is not evidence the reader left;
    the other two conditions still apply."""
    import io
    assert not SS._reader_hung_up(io.StringIO())


def test_follow_exits_on_a_hangup_with_a_live_parent(SS):
    """The whole point: the parent is alive, the session is silent, and the
    loop must still stop. This is the shape seen on the mini — five followers
    for one session, all with a live sshd parent, none of them ever writing."""
    ticks = {"n": 0}

    def reader(_name):
        ticks["n"] += 1
        return {"session": "s", "state": "idle", "agents": [], "agents_fleet_wide": False}

    list(SS.follow_states(
        "s", 0, sleep=lambda _s: None, reader=reader,
        orphaned=lambda: False,                 # parent alive, as measured
        hung_up=lambda: ticks["n"] >= 2))       # channel closes on the 2nd tick
    assert ticks["n"] <= 3, "the loop must stop at the hangup, not run on"


def test_follow_keeps_running_while_the_reader_is_present(SS):
    out = list(SS.follow_states(
        "s", 0, sleep=lambda _s: None,
        reader=lambda _n: {"session": "s", "state": "idle",
                           "agents": [], "agents_fleet_wide": False},
        orphaned=lambda: False, hung_up=lambda: False, ticks=3))
    assert len(out) == 1, "one yield for the first state, then silence while stable"


def test_an_oversized_working_on_file_is_refused_without_reading_it(
        SS, monkeypatch, tmp_path):
    """The rail's blast radius, not this session's.

    `working_on` is read under `brief` too, which is the rail's reader looped
    over every pinned session on Deck's ~4s poll. The cache directory is
    writable by anything running as this user, so `co-working-on`'s write-side
    bound proves nothing about what is on disk — one poisoned file must not
    stall the poll for every session the rail watches.
    """
    monkeypatch.setattr(SS, "VITALS_DIR", tmp_path)
    key = "sess"
    (tmp_path / f"working-{key}.json").write_text(
        json.dumps({"text": "x" * (SS._WORKING_ON_MAX_BYTES + 1),
                    "written_at": time.time()}))

    got = SS.working_on(key)

    assert got == {"text": None, "age": None, "stale": None, "since": None}


def test_a_deeply_nested_working_on_file_does_not_take_down_the_state_call(
        SS, monkeypatch, tmp_path):
    """A size cap alone does not close this.

    A few kilobytes of `[[[[…` is thousands of levels deep — well past the
    interpreter's recursion limit and well under any generous byte ceiling.
    `json.load` raises `RecursionError`, which is neither `OSError` nor
    `ValueError`, so it would escape the ordinary handler and take down the
    whole `state` call that this is one field of.
    """
    monkeypatch.setattr(SS, "VITALS_DIR", tmp_path)
    key = "sess"
    depth = 2_000
    (tmp_path / f"working-{key}.json").write_text("[" * depth + "]" * depth)

    got = SS.working_on(key)

    assert got == {"text": None, "age": None, "stale": None, "since": None}


def test_a_working_on_file_exactly_at_the_ceiling_is_still_read(
        SS, monkeypatch, tmp_path):
    """The boundary, on the side that must keep working.

    The ceiling is generous against a ~300-byte legitimate record, so nothing
    real approaches it — but an off-by-one that refused at the limit would
    silently drop the panel for a record the design considers fine, and no
    other test would notice.
    """
    monkeypatch.setattr(SS, "VITALS_DIR", tmp_path)
    key = "sess"
    path = tmp_path / f"working-{key}.json"

    # ONE `written_at`, USED FOR BOTH THE MEASUREMENT AND THE WRITE. Two
    # separate `time.time()` calls stringify to different lengths often enough
    # that the padding arithmetic was wrong about half the time, and the
    # trailing slice that "fixed" it cut through the timestamp and produced
    # invalid JSON — so this test failed about one run in four, on the exact
    # assertion meant to prove the ceiling does not clip a valid record. Found
    # and measured by `deck-security-reviewer` on #434.
    written = time.time()
    filler = "x" * (SS._WORKING_ON_MAX_BYTES
                    - len(json.dumps({"text": "", "written_at": written}).encode()))
    body = json.dumps({"text": filler, "written_at": written}).encode()
    assert len(body) == SS._WORKING_ON_MAX_BYTES, "the test must sit exactly on the boundary"
    path.write_bytes(body)

    got = SS.working_on(key)

    assert got["text"] is not None, "a record at exactly the ceiling still reads"


def test_one_byte_over_the_ceiling_is_refused(SS, monkeypatch, tmp_path):
    """The other side of the same boundary, so the pair pins where the line is
    rather than only that a line exists."""
    monkeypatch.setattr(SS, "VITALS_DIR", tmp_path)
    key = "sess"
    written = time.time()
    filler = "x" * (1 + SS._WORKING_ON_MAX_BYTES
                    - len(json.dumps({"text": "", "written_at": written}).encode()))
    body = json.dumps({"text": filler, "written_at": written}).encode()
    assert len(body) == SS._WORKING_ON_MAX_BYTES + 1

    (tmp_path / f"working-{key}.json").write_bytes(body)

    assert SS.working_on(key)["text"] is None


def test_the_ceiling_counts_bytes_rather_than_characters(SS, monkeypatch, tmp_path):
    """The one payload that tells a byte read apart from a character read.

    `ensure_ascii=False` IS THE WHOLE TEST, and the first version of it lacked
    that argument and therefore proved nothing. `json.dumps` escapes non-ASCII
    to `\\uXXXX` by default, so an astral-plane filler reaches disk as pure
    ASCII where one character is one byte — a payload both a byte read and a
    character read refuse identically, for its size alone. Confirmed by running
    both implementations against it: same answer from each.

    This body carries real multi-byte UTF-8 on disk instead: SHORT in
    characters and OVER the ceiling in bytes, which is exactly the shape a
    text-mode `read(n)` would wrongly admit. A regression to text mode makes
    this test fail rather than pass quietly. Found by `reviewer` on #434, which
    built the distinguishing payload rather than only naming the gap.
    """
    monkeypatch.setattr(SS, "VITALS_DIR", tmp_path)
    key = "sess"
    written = time.time()
    overhead = len(json.dumps(
        {"text": "", "written_at": written}, ensure_ascii=False).encode())
    filler = "\U0001F600" * ((SS._WORKING_ON_MAX_BYTES - overhead) // 4 + 2)
    body = json.dumps(
        {"text": filler, "written_at": written}, ensure_ascii=False).encode()

    # The two assertions that make this test the one it claims to be.
    assert len(body) > SS._WORKING_ON_MAX_BYTES, "over the ceiling in bytes"
    assert len(body.decode()) <= SS._WORKING_ON_MAX_BYTES, \
        "but under it in characters — a character read would admit this"

    (tmp_path / f"working-{key}.json").write_bytes(body)

    assert SS.working_on(key)["text"] is None


def test_agent_runs_carries_the_registry_fields_agents_throws_away(SS, monkeypatch, tmp_path):
    """`live_agents` filtered and scoped every live run and then kept only its
    name, so Deck could say an agent existed and nothing about it. The registry
    already held the task, the start time and the repo; this is that, served."""
    active = tmp_path / "active.json"
    active.write_text(json.dumps({"runs": [
        {"pid": 1, "repo": "convivy-deck", "agent": "deck-coder",
         "run_id": "run-abc", "task": "redraw the panel",
         "started_at": "2026-08-28T10:00:00Z"},
    ]}))
    monkeypatch.setattr(SS, "ACTIVE_JSON", active)
    monkeypatch.setattr(SS, "_pid_alive", lambda pid: True)
    _stub_crew_registry(tmp_path, monkeypatch, SS)

    out = SS.live_agents("/srv/convivy/convivy-deck")
    assert out["agents"] == ["deck-coder"]          # unchanged for old readers
    (run,) = out["agent_runs"]
    assert run["run_id"] == "run-abc"
    assert run["agent"] == "deck-coder"
    assert run["task"] == "redraw the panel"
    assert run["started_at"] == "2026-08-28T10:00:00Z"
    assert run["repo"] == "convivy-deck"


def _private_crew(SS):
    """The `private_crew` module `session_state` imported, from `sys.modules`.

    Reached this way rather than through a handle on `session_state` itself: a
    module attribute existing only so a test can patch it is unused production
    code, and ruff says so (F401). The import already put it here.
    """
    return sys.modules["private_crew"]


def _stub_crew_registry(tmp_path, monkeypatch, SS):
    """Point the crew registry at a fixture, for EVERY test touching this path.

    The privacy gate reads `~/Convivy/agent-platform/config/projects.json` to
    answer "is this crew private". A test that does not stub it reads the
    OPERATOR'S REAL CONFIG: locally that file exists and says `convivy-deck` is
    not private, so such tests pass; on CI it does not exist, the gate fails
    closed, and every field is withheld. Green locally and red on CI for a
    reason unrelated to what the test asserts — which is how three of these
    reached CI.

    A test that reads the machine's configuration is not testing this code.
    Callers that care about privacy override `_REGISTRY` (or the `projects`
    stubs below) again afterwards.

    **STUBS TWO SEPARATE MODULES WITH THE SAME FACTS.** `_crew_is_shareable`
    (the gate `agent_runs` sits behind) answers through
    `projects.dispatch_is_private`, a different registry reader from
    `private_crew.is_private_slug` — the two modules exist for different
    reasons and neither reads the other's stub. `private_crew`'s `_REGISTRY`
    is kept here too because other functions in this file (`private_home_for`)
    still read it.
    """
    registry = tmp_path / "projects-default.json"
    registry.write_text(json.dumps({
        "_meta": {"note": "underscore keys are skipped"},
        "convivy-deck": {"private": False},
        "cfo": {"private": True, "home_dir": "~/Convivy/CFO"},
    }))
    monkeypatch.setattr(_private_crew(SS), "_REGISTRY", str(registry))
    monkeypatch.setattr(SS.projects, "private_slugs", lambda: {"cfo"})
    monkeypatch.setattr(
        SS.projects, "home_path",
        lambda slug: (tmp_path / "CFO") if slug == "cfo" else None)


def _active_with(tmp_path, monkeypatch, SS, **entry):
    """One live run in a registry the test owns, with `Path.home()` pointed at
    tmp_path so `_run_meta_path` composes real paths under it.

    **`_run_meta_path` IS NOT STUBBED IN ANY TEST BELOW**, deliberately. Its
    first version globbed for the run directory and the cost of that was
    invisible to a suite that mocked it away — the regression these tests exist
    to hold reached review because nothing exercised the real lookup.
    """
    active = tmp_path / "active.json"
    base = {"pid": 1, "repo": "convivy-deck", "agent": "deck-coder"}
    base.update(entry)
    active.write_text(json.dumps({"runs": [base]}))
    monkeypatch.setattr(SS, "ACTIVE_JSON", active)
    monkeypatch.setattr(SS, "_pid_alive", lambda pid: pid == 1)
    monkeypatch.setattr(SS.Path, "home", staticmethod(lambda: tmp_path))
    # Run records resolve through builder_root.runs_root(), which never reads
    # Path.home(), so the redirect has to name the data root too.
    monkeypatch.setenv("BUILDER_ROOT", str(tmp_path / "Convivy"))

    # THE CREW REGISTRY IS STUBBED FOR EVERY TEST, NOT ONLY THE PRIVACY ONES.
    # The gate reads `~/Convivy/agent-platform/config/projects.json` to answer
    # "is this crew private", so a test that does not stub it depends on the
    # operator's real config. Locally that file exists and says `convivy-deck`
    # is not private, so these passed; on CI it does not exist,
    # `is_private_slug` answers UNKNOWN, the gate fails closed, and every field
    # is withheld. Green locally and red on CI for a reason unrelated to what
    # the tests assert. A test that reads the machine's configuration is not
    # testing this code.
    _stub_crew_registry(tmp_path, monkeypatch, SS)
    return active


def _write_meta(tmp_path, day, run_id, payload):
    d = tmp_path / "Convivy" / "agent-runs" / day / run_id
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps(payload))


def _write_cost_partial(tmp_path, day, run_id, **fields):
    """A `cost-partial.json` in the same run dir `_write_meta` composes —
    `exist_ok=True` since a test writes `meta.json` into this same directory
    first, in either order."""
    d = tmp_path / "Convivy" / "agent-runs" / day / run_id
    d.mkdir(parents=True, exist_ok=True)
    payload = {"usd": 1.0, "tokens_in": 0, "tokens_out": 0,
               "cost_source": "estimate_fallback", "by_model": {},
               "written_at": "2026-08-28T19:40:00Z"}
    payload.update(fields)
    (d / "cost-partial.json").write_text(json.dumps(payload))


def _write_cost_final(tmp_path, day, run_id, **fields):
    """A finished run's `cost.json` in the same run dir `_write_meta` composes."""
    d = tmp_path / "Convivy" / "agent-runs" / day / run_id
    d.mkdir(parents=True, exist_ok=True)
    payload = {"usd": 2.0, "tokens_in": 0, "tokens_out": 0,
               "cost_source": "anthropic_cli", "by_model": {}}
    payload.update(fields)
    (d / "cost.json").write_text(json.dumps(payload))


def test_model_is_read_from_the_runs_meta_because_the_registry_has_none(
        SS, monkeypatch, tmp_path):
    """The `active-json` entry table carries no model; `meta.json` does.
    `model_resolved` wins over `model`: one is what ran, the other is what was
    asked for and can be an alias."""
    _active_with(tmp_path, monkeypatch, SS,
                 run_id="run-xyz", started_at="2026-08-28T19:36:03Z")
    local_day = datetime.datetime(2026, 8, 28, 19, 36, 3,
                                  tzinfo=datetime.timezone.utc).astimezone().strftime("%Y-%m-%d")
    _write_meta(tmp_path, local_day, "run-xyz",
                {"model": "sonnet", "model_resolved": "claude-sonnet-5"})

    (run,) = SS.live_agents("/srv/convivy/convivy-deck")["agent_runs"]
    assert run["model"] == "claude-sonnet-5"


def test_the_meta_path_is_composed_from_started_at_never_globbed(
        SS, monkeypatch, tmp_path):
    """THE REGRESSION THIS FILE EXISTS TO HOLD. The first version globbed
    `*/<run_id>/meta.json`, listing every date directory under agent-runs — on
    the rail's batched read, for every pinned session, on every poll, because
    `brief` gates the duplicate `live_agents` call inside `vitals` and not the
    primary one. Decoy directories here would each be listed by a glob and are
    never opened by a composed path."""
    _active_with(tmp_path, monkeypatch, SS,
                 run_id="run-target", started_at="2026-08-28T19:36:03Z")
    local_day = datetime.datetime(2026, 8, 28, 19, 36, 3,
                                  tzinfo=datetime.timezone.utc).astimezone().strftime("%Y-%m-%d")
    _write_meta(tmp_path, local_day, "run-target", {"model_resolved": "claude-opus-5"})
    for decoy in ("2026-01-01", "2026-02-02", "2026-03-03"):
        _write_meta(tmp_path, decoy, "run-target", {"model_resolved": "WRONG"})

    (run,) = SS.live_agents("/srv/convivy/convivy-deck")["agent_runs"]
    assert run["model"] == "claude-opus-5", "took a decoy, so it is still searching"

    # ASSERT THE COST, NOT THE METHOD. An earlier version instrumented
    # `Path.glob` alone, so a refactor to `iterdir`, `os.listdir` or
    # `os.scandir` would have reintroduced the same per-poll directory listing
    # and still passed — the test named the mechanism it happened to replace
    # rather than the property it guards (reviewer and security-reviewer, #464).
    listings = []
    for owner, name in ((SS.Path, "glob"), (SS.Path, "iterdir"),
                        (SS.os, "listdir"), (SS.os, "scandir")):
        real = getattr(owner, name)
        monkeypatch.setattr(
            owner, name,
            lambda *a, _n=name, _r=real, **k: (listings.append(_n), _r(*a, **k))[1])
    SS.live_agents("/srv/convivy/convivy-deck")
    assert listings == [], f"a directory listing happened: {listings}"


def test_a_run_id_that_would_walk_out_of_the_runs_root_is_refused(
        SS, monkeypatch, tmp_path):
    """`run_id` comes from machine-wide `active.json`. A `..` segment is a
    literal glob component and a literal path component alike, so without a
    shape check it joins straight out of the root and `_read_json` parses
    whatever JSON it lands on, surfacing that file's `model` into the payload.
    Every other untrusted id in this file passes a charset check first."""
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "meta.json").write_text(json.dumps({"model_resolved": "ESCAPED"}))

    # THE RUNS ROOT MUST EXIST FOR THIS TEST TO TEST ANYTHING. `..` is resolved
    # by traversal, not lexically, so a missing intermediate directory makes the
    # escape fail with ENOENT — and the test then passes against vulnerable code
    # for a reason that has nothing to do with the guard. Verified: without this
    # mkdir the test passes against the original globbing version, which is the
    # version it exists to catch.
    (tmp_path / "Convivy" / "agent-runs" / "2026-08-28").mkdir(parents=True)

    # THE DOT-COUNT IS DERIVED FROM THE REAL COMPOSITION, never hardcoded. This
    # fixture has now been wrong twice for the same reason: a literal count is
    # pinned to a path shape, and when the shape moved the escape landed on a
    # directory that does not exist, so the test passed against unguarded code
    # for an ENOENT reason rather than because of the guard. Asking the function
    # where it would put a benign run makes the fixture move with the shape.
    monkeypatch.setattr(SS.Path, "home", staticmethod(lambda: tmp_path))
    # Run records resolve through builder_root.runs_root(), which never reads
    # Path.home(), so the redirect has to name the data root too.
    monkeypatch.setenv("BUILDER_ROOT", str(tmp_path / "Convivy"))
    benign = SS._run_meta_path("run-benign", "2026-08-28T19:36:03Z")
    if benign is None:
        benign = (tmp_path / "Convivy" / "agent-runs" / "2026-08-28"
                  / "run-benign" / "meta.json")
    # `.parent.parent` is the directory the run-id segment SITS IN, not the
    # run-id directory itself. Counting one level too deep climbs past tmp_path
    # and the escape lands somewhere that does not exist — inert again, in the
    # same way and for the third time. Verified in the failing direction.
    depth = len(benign.parent.parent.relative_to(tmp_path).parts)
    payload = "/".join([".."] * depth) + "/elsewhere"

    _active_with(tmp_path, monkeypatch, SS,
                 run_id=payload, started_at="2026-08-28T19:36:03Z")

    (run,) = SS.live_agents("/srv/convivy/convivy-deck")["agent_runs"]
    assert run["model"] is None
    assert run["agent"] == "deck-coder", "the row still draws; only the field degrades"


def test_a_run_started_the_other_side_of_local_midnight_is_still_found(
        SS, monkeypatch, tmp_path):
    """The registry stamps `started_at` in UTC and the run directory's day key
    is LOCAL (ARCH-156), so the two disagree either side of midnight. The
    neighbouring days are tried rather than assumed away."""
    _active_with(tmp_path, monkeypatch, SS,
                 run_id="run-edge", started_at="2026-08-28T19:36:03Z")
    local = datetime.datetime(2026, 8, 28, 19, 36, 3,
                              tzinfo=datetime.timezone.utc).astimezone()
    neighbour = (local - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    _write_meta(tmp_path, neighbour, "run-edge", {"model_resolved": "claude-sonnet-5"})

    (run,) = SS.live_agents("/srv/convivy/convivy-deck")["agent_runs"]
    assert run["model"] == "claude-sonnet-5"


def test_a_run_with_no_readable_meta_still_draws(SS, monkeypatch, tmp_path):
    """The registry is what says this agent is alive. Dropping its row because
    a model could not be resolved would report the fleet as smaller than it is."""
    _active_with(tmp_path, monkeypatch, SS, run_id="gone")

    (run,) = SS.live_agents("/srv/convivy/convivy-deck")["agent_runs"]
    assert run["model"] is None
    assert run["agent"] == "deck-coder"


def test_a_run_with_no_cost_file_of_either_kind_omits_the_fields(SS, monkeypatch, tmp_path):
    """#592 gave every live run a `cost-partial.json` checkpoint, so a run that
    is still going is no longer guaranteed to have NO cost figure — but one too
    young to have written either file yet must still omit `usd`/`cost_source`/
    `written_at` rather than send nulls or zeros (issue_01M2KVQGDRS6CA4J0DY8R50T71).
    `meta.json` exists here (unlike the old version of this test), so the
    omission is specifically about the two cost files, not about `meta_path`
    resolving to nothing."""
    _active_with(tmp_path, monkeypatch, SS, run_id="r", started_at="2026-08-28T19:36:03Z")
    local_day = datetime.datetime(2026, 8, 28, 19, 36, 3,
                                  tzinfo=datetime.timezone.utc).astimezone().strftime("%Y-%m-%d")
    _write_meta(tmp_path, local_day, "r", {"model_resolved": "claude-sonnet-5"})

    (run,) = SS.live_agents("/srv/convivy/convivy-deck")["agent_runs"]
    assert "usd" not in run
    assert "cost_source" not in run
    assert "written_at" not in run


def test_a_live_run_with_a_partial_carries_the_in_flight_cost(SS, monkeypatch, tmp_path):
    """#592's `cost-partial.json` checkpoint reaching a consumer for the first
    time (issue_01M2KVQGDRS6CA4J0DY8R50T71): no `cost.json` yet, so the
    in-flight figure is what the row carries."""
    _active_with(tmp_path, monkeypatch, SS,
                 run_id="run-inflight", started_at="2026-08-28T19:36:03Z")
    local_day = datetime.datetime(2026, 8, 28, 19, 36, 3,
                                  tzinfo=datetime.timezone.utc).astimezone().strftime("%Y-%m-%d")
    _write_meta(tmp_path, local_day, "run-inflight", {"model_resolved": "claude-sonnet-5"})
    _write_cost_partial(tmp_path, local_day, "run-inflight",
                        usd=1.23, cost_source="estimate_fallback",
                        written_at="2026-08-28T19:40:00Z")

    (run,) = SS.live_agents("/srv/convivy/convivy-deck")["agent_runs"]
    assert run["usd"] == pytest.approx(1.23)
    assert run["cost_source"] == "estimate_fallback"
    assert run["written_at"] == "2026-08-28T19:40:00Z"


def test_a_partial_that_skipped_an_unpriced_model_omits_usd(SS, monkeypatch, tmp_path):
    """The partial's `usd` is a floor when a model could not be priced; sent as
    `usd` it would read as the run's spend. Omitted is the existing no-figure shape."""
    _active_with(tmp_path, monkeypatch, SS,
                 run_id="run-unpriced", started_at="2026-08-28T19:36:03Z")
    local_day = datetime.datetime(2026, 8, 28, 19, 36, 3,
                                  tzinfo=datetime.timezone.utc).astimezone().strftime("%Y-%m-%d")
    _write_meta(tmp_path, local_day, "run-unpriced", {"model_resolved": "mystery/model-x"})
    _write_cost_partial(tmp_path, local_day, "run-unpriced", usd=0.0,
                        cost_source="estimate_fallback",
                        unpriced_models=["mystery/model-x"])

    (run,) = SS.live_agents("/srv/convivy/convivy-deck")["agent_runs"]
    assert "usd" not in run
    assert "cost_source" not in run


def test_a_finished_run_with_cost_json_carries_the_final_cost(SS, monkeypatch, tmp_path):
    """`cost.json` carries no `written_at` of its own — this is `meta.json`'s
    `ended_at`, already loaded for this run, standing in for it rather than a
    second file read."""
    _active_with(tmp_path, monkeypatch, SS,
                 run_id="run-done", started_at="2026-08-28T19:36:03Z")
    local_day = datetime.datetime(2026, 8, 28, 19, 36, 3,
                                  tzinfo=datetime.timezone.utc).astimezone().strftime("%Y-%m-%d")
    _write_meta(tmp_path, local_day, "run-done",
                {"model_resolved": "claude-sonnet-5", "ended_at": "2026-08-28T19:50:00Z"})
    _write_cost_final(tmp_path, local_day, "run-done", usd=4.56, cost_source="anthropic_cli")

    (run,) = SS.live_agents("/srv/convivy/convivy-deck")["agent_runs"]
    assert run["usd"] == pytest.approx(4.56)
    assert run["cost_source"] == "anthropic_cli"
    assert run["written_at"] == "2026-08-28T19:50:00Z"


def test_cost_json_wins_over_a_stale_partial(SS, monkeypatch, tmp_path):
    """`_finalize_run` unlinks `cost-partial.json` once `cost.json` lands, but a
    reader must not depend on that cleanup having already happened — `cost.json`
    is authoritative the instant it exists (write_cost_partial's docstring)."""
    _active_with(tmp_path, monkeypatch, SS,
                 run_id="run-both", started_at="2026-08-28T19:36:03Z")
    local_day = datetime.datetime(2026, 8, 28, 19, 36, 3,
                                  tzinfo=datetime.timezone.utc).astimezone().strftime("%Y-%m-%d")
    _write_meta(tmp_path, local_day, "run-both", {"ended_at": "2026-08-28T19:50:00Z"})
    _write_cost_partial(tmp_path, local_day, "run-both",
                        usd=1.11, cost_source="estimate_fallback")
    _write_cost_final(tmp_path, local_day, "run-both", usd=9.99, cost_source="anthropic_cli")

    (run,) = SS.live_agents("/srv/convivy/convivy-deck")["agent_runs"]
    assert run["usd"] == pytest.approx(9.99)
    assert run["cost_source"] == "anthropic_cli"


def test_a_partial_with_an_unrecognised_cost_source_is_omitted(SS, monkeypatch, tmp_path):
    """`T.read_cost_partial` already refuses a source-less or unknown-sourced
    partial (agent-platform#592); this is that refusal reaching
    `_agent_run_detail` as an omission, not a raise."""
    _active_with(tmp_path, monkeypatch, SS,
                 run_id="run-bad-source", started_at="2026-08-28T19:36:03Z")
    local_day = datetime.datetime(2026, 8, 28, 19, 36, 3,
                                  tzinfo=datetime.timezone.utc).astimezone().strftime("%Y-%m-%d")
    _write_meta(tmp_path, local_day, "run-bad-source", {})
    _write_cost_partial(tmp_path, local_day, "run-bad-source",
                        usd=1.0, cost_source="not_a_real_source")

    (run,) = SS.live_agents("/srv/convivy/convivy-deck")["agent_runs"]
    assert "usd" not in run
    assert "cost_source" not in run
    assert "written_at" not in run


def test_a_non_regular_cost_partial_file_is_omitted(SS, monkeypatch, tmp_path):
    """A FIFO planted at `cost-partial.json`'s own name must not hang this poll
    — `T.read_cost_partial`'s `os.lstat` guard refuses it before ever opening,
    and this hop must degrade to an omission the same way (agent-platform#592,
    round 3, on the identical file this function now also reads)."""
    _active_with(tmp_path, monkeypatch, SS,
                 run_id="run-fifo", started_at="2026-08-28T19:36:03Z")
    local_day = datetime.datetime(2026, 8, 28, 19, 36, 3,
                                  tzinfo=datetime.timezone.utc).astimezone().strftime("%Y-%m-%d")
    _write_meta(tmp_path, local_day, "run-fifo", {})
    run_dir = tmp_path / "Convivy" / "agent-runs" / local_day / "run-fifo"
    os.mkfifo(run_dir / "cost-partial.json")

    (run,) = SS.live_agents("/srv/convivy/convivy-deck")["agent_runs"]
    assert "usd" not in run
    assert "cost_source" not in run
    assert "written_at" not in run


def test_a_malformed_run_record_cannot_take_down_agent_runs_either(
        SS, monkeypatch, tmp_path):
    """The detail path is fed the same untrusted records the name path is and
    does strictly more with them."""
    active = tmp_path / "active.json"
    active.write_text(json.dumps({"runs": [
        {"repo": "convivy-deck", "agent": "no-pid"},
        {"pid": "nonsense", "repo": "convivy-deck", "agent": "bad-pid"},
        {"pid": 1, "repo": "convivy-deck", "agent": "ok",
         "run_id": 12345, "task": ["not", "a", "string"], "started_at": 7},
        "not-even-a-dict",
    ]}))
    monkeypatch.setattr(SS, "ACTIVE_JSON", active)
    monkeypatch.setattr(SS, "_pid_alive", lambda pid: pid == 1)
    monkeypatch.setattr(SS.Path, "home", staticmethod(lambda: tmp_path))
    # Run records resolve through builder_root.runs_root(), which never reads
    # Path.home(), so the redirect has to name the data root too.
    monkeypatch.setenv("BUILDER_ROOT", str(tmp_path / "Convivy"))
    _stub_crew_registry(tmp_path, monkeypatch, SS)

    (run,) = SS.live_agents("/srv/convivy/convivy-deck")["agent_runs"]
    assert run["agent"] == "ok"
    assert run["run_id"] == "12345"
    assert run["task"] is None
    assert run["started_at"] is None
    assert run["model"] is None


def test_a_private_crews_task_never_reaches_the_payload(SS, monkeypatch, tmp_path):
    """ADR-0043's black box: a private crew's dispatch prompt must not leave the
    crew's own directory, and `active.json` is machine-wide — so a fleet-wide
    read walks CFO's entries too, and this payload is rendered by Deck on the
    laptop. The agent NAME is unchanged from what `agents` already exposed;
    only the new free text is withheld."""
    # THE REAL LOOKUP, NOT A MOCKED GATE. Mocking `private_home_for` meant a
    # regression in how `repo` becomes a privacy answer would not be caught —
    # and that construction is exactly what changed twice (security-reviewer,
    # #464). This stubs the REGISTRY instead, one layer further out, so the
    # slug matching and the fail-closed policy both run for real.
    _active_with(tmp_path, monkeypatch, SS,
                 repo="CFO", agent="cfo-analyst", run_id="run-priv",
                 task="reconcile the September ledger against the bank export")
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({
        "_meta": {"note": "underscore keys are skipped"},
        "cfo": {"private": True, "home_dir": "~/Convivy/CFO"},
        "convivy-deck": {"private": False},
    }))
    monkeypatch.setattr(_private_crew(SS), "_REGISTRY", str(registry))
    # A REAL, VALID cost-partial.json — not merely an absent one — so this test
    # proves the shareable-gate withholds a genuine in-flight figure, not just
    # that a private crew's run happens to have none
    # (issue_01M2KVQGDRS6CA4J0DY8R50T71: "the same resolution", not a second
    # check re-deriving the answer).
    today = datetime.datetime.now().astimezone().strftime("%Y-%m-%d")
    _write_meta(tmp_path, today, "run-priv", {"repo": "CFO"})
    _write_cost_partial(tmp_path, today, "run-priv",
                        usd=3.5, cost_source="estimate_fallback")

    out = SS.live_agents("/srv/convivy/substrate")   # fleet-wide
    assert out["fleet_wide"] is True
    (run,) = out["agent_runs"]
    assert run["agent"] == "cfo-analyst", "the run still draws; the fleet is not under-reported"
    # THE WHOLE ROW, not just the task. `agents` already leaked a bare NAME
    # before this feature and a name binds to nothing; `repo` says which private
    # crew is live, `started_at` when, `model` what it runs, and `run_id`
    # correlates them across reads. That composite is the live roster row
    # ADR-0043 forbids (security-reviewer, #464).
    for field in ("task", "repo", "run_id", "started_at", "model"):
        assert run[field] is None, f"{field} crossed for a private crew"
    # A black-box crew's live spend is a roster row by another name
    # (issue_01M2KVQGDRS6CA4J0DY8R50T71) — omitted, like the fields above,
    # rather than sent as None, despite a genuine cost-partial.json existing.
    for field in ("usd", "cost_source", "written_at"):
        assert field not in run, f"{field} crossed for a private crew"


def test_agents_dir_bypass_is_closed_for_agent_runs_too(SS, monkeypatch, tmp_path):
    """THE SAME BYPASS `test_agents_dir_bypass_leaks_neither_text_nor_identity`
    closed for `_dispatched_by`, still open here: `_crew_is_shareable` read only
    `repo`, but `resolve_agent()` (bin/agentctl) honours `--agents-dir` before it
    ever looks at `repo` — so `--on constance --agents-dir
    ~/Convivy/CFO/.claude/agents` writes `repo: "constance"` (a public slug)
    into `active.json` while the run's `meta.json` carries an `agent_source`
    pointing inside CFO's home. A repo-only check answers "not private" and the
    row leaks task, started_at, run_id, repo and model to Deck — the ADR-0043
    black box this function's own docstring claims to close.

    Asserted as a DELTA against an otherwise identical public dispatch sharing
    the same `repo` slug, so a repo-only check cannot pass by coincidence.
    """
    active = tmp_path / "active.json"
    monkeypatch.setattr(SS, "ACTIVE_JSON", active)
    monkeypatch.setattr(SS, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(SS.Path, "home", staticmethod(lambda: tmp_path))
    # Run records resolve through builder_root.runs_root(), which never reads
    # Path.home(), so the redirect has to name the data root too.
    monkeypatch.setenv("BUILDER_ROOT", str(tmp_path / "Convivy"))
    monkeypatch.setattr(SS.projects, "private_slugs", lambda: {"private-crew"})
    # `constance` must read as non-private on the LEGACY registry too, so a
    # pre-fix run isolates the bypass itself rather than failing merely because
    # an unreadable registry (this test's `HOME` has none) redacts both rows
    # for an unrelated reason.
    legacy_registry = tmp_path / "legacy-projects.json"
    legacy_registry.write_text(json.dumps({"constance": {"private": False}}))
    monkeypatch.setattr(_private_crew(SS), "_REGISTRY", str(legacy_registry))

    private_home = tmp_path / "private-crew-home"
    private_agents_dir = private_home / ".claude" / "agents"
    private_agents_dir.mkdir(parents=True)
    monkeypatch.setattr(SS.projects, "home_path",
                         lambda slug: private_home if slug == "private-crew" else None)
    public_agents_dir = tmp_path / "public-agents"
    public_agents_dir.mkdir()

    started_at = "2026-08-29T10:00:00Z"
    local_day = datetime.datetime(2026, 8, 29, 10, 0, 0,
                                  tzinfo=datetime.timezone.utc).astimezone().strftime("%Y-%m-%d")
    active.write_text(json.dumps({"runs": [
        {"pid": 1, "repo": "constance", "agent": "coder", "run_id": "run-pub",
         "task": "public crew work via constance", "started_at": started_at},
        {"pid": 1, "repo": "constance", "agent": "cfo-classify", "run_id": "run-priv",
         "task": "SENSITIVE constance-labelled work", "started_at": started_at},
    ]}))
    _write_meta(tmp_path, local_day, "run-pub",
                {"agent_source": str(public_agents_dir / "coder.md")})
    _write_meta(tmp_path, local_day, "run-priv",
                {"agent_source": str(private_agents_dir / "cfo-classify.md")})

    public, private = SS.live_agents(
        "/srv/convivy/substrate")["agent_runs"]

    # The half that must still work — otherwise the assertion below is vacuous.
    assert public["task"] == "public crew work via constance"
    assert public["run_id"] == "run-pub"
    assert public["repo"] == "constance"

    assert private["agent"] == "cfo-classify"
    assert "SENSITIVE" not in json.dumps(private)
    for field in ("task", "repo", "run_id", "started_at", "model"):
        assert private[field] is None, f"{field} crossed the agents-dir bypass"


def test_a_task_is_withheld_when_privacy_cannot_be_determined(SS, monkeypatch, tmp_path):
    """`_crew_is_shareable` answers through `projects.dispatch_is_private`,
    which raises straight through when its registry read fails. This side is a
    DISCLOSURE, so "cannot tell" withholds rather than emits."""
    _active_with(tmp_path, monkeypatch, SS, run_id="run-x", task="something")
    # `projects.private_slugs()` (`units()` -> `load()` -> `_load_raw()`) raising
    # is what an unreadable or unparseable registry looks like from this call's
    # side — the gate must treat it as withhold, not crash or leak.
    def _boom():
        raise json.JSONDecodeError("bad", "doc", 0)
    monkeypatch.setattr(SS.projects, "private_slugs", _boom)

    (run,) = SS.live_agents("/srv/convivy/convivy-deck")["agent_runs"]
    for field in ("task", "repo", "run_id", "started_at", "model"):
        assert run[field] is None, field
    assert run["agent"] == "deck-coder"


def test_a_non_private_task_is_sanitised_and_bounded(SS, monkeypatch, tmp_path):
    """`task` is an operator-supplied shell argument reaching a renderer this
    repo does not control. Nothing stops one carrying a terminal control
    sequence or a pasted credential."""
    _active_with(tmp_path, monkeypatch, SS, run_id="run-y",
                 task="\x1b[31mred\x1b[0m " + "x" * 4000)
    monkeypatch.setattr(SS, "private_home_for", lambda cwd: None)

    (run,) = SS.live_agents("/srv/convivy/convivy-deck")["agent_runs"]
    assert "\x1b" not in run["task"], "escape sequences survived"
    assert len(run["task"]) <= 520, f"unbounded: {len(run['task'])}"
    assert "red" in run["task"]


def test_a_run_with_no_repo_withholds_its_task(SS, monkeypatch, tmp_path):
    """Without a repo the run cannot be placed, so it cannot be cleared."""
    active = tmp_path / "active.json"
    active.write_text(json.dumps({"runs": [
        {"pid": 1, "agent": "orphan", "run_id": "run-z", "task": "secret-ish"},
    ]}))
    monkeypatch.setattr(SS, "ACTIVE_JSON", active)
    monkeypatch.setattr(SS, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(SS.Path, "home", staticmethod(lambda: tmp_path))
    # Run records resolve through builder_root.runs_root(), which never reads
    # Path.home(), so the redirect has to name the data root too.
    monkeypatch.setenv("BUILDER_ROOT", str(tmp_path / "Convivy"))

    (run,) = SS.live_agents("/srv/convivy/substrate")["agent_runs"]
    # ALL FIVE, not just the task. The gate withholds the whole row, and a test
    # asserting one field is the "gate covers less than it looks like" shape
    # this PR hit twice already (reviewer, #464).
    for field in ("task", "repo", "run_id", "started_at", "model"):
        assert run[field] is None, f"{field} crossed for an unplaceable run"


def test_the_other_side_of_local_midnight_is_covered_too(SS, monkeypatch, tmp_path):
    """The `-1` neighbour had a test and `+1` did not, so half the branch was
    asserted and half was assumed."""
    _active_with(tmp_path, monkeypatch, SS,
                 run_id="run-fwd", started_at="2026-08-28T19:36:03Z")
    local = datetime.datetime(2026, 8, 28, 19, 36, 3,
                              tzinfo=datetime.timezone.utc).astimezone()
    ahead = (local + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    _write_meta(tmp_path, ahead, "run-fwd", {"model_resolved": "claude-opus-5"})

    (run,) = SS.live_agents("/srv/convivy/convivy-deck")["agent_runs"]
    assert run["model"] == "claude-opus-5"


def test_an_extreme_started_at_degrades_one_field_not_the_whole_read(
        SS, monkeypatch, tmp_path):
    """`started_at` comes from the same untrusted registry `run_id` does. An
    extreme year PARSES and then overflows on the neighbouring-day arithmetic,
    and that raised outside the try — propagating through `live_agents` to
    `session_state`, which `session_input.py` calls with no wrapper of its own.
    One corrupt record took down a whole read rather than degrading a field."""
    for stamp in ("9999-12-31T23:59:59Z", "0001-01-01T00:00:01Z"):
        assert SS._run_meta_path("run-xyz", stamp) is None, stamp

    _active_with(tmp_path, monkeypatch, SS,
                 run_id="run-boom", started_at="9999-12-31T23:59:59Z")
    (run,) = SS.live_agents("/srv/convivy/convivy-deck")["agent_runs"]
    assert run["model"] is None
    assert run["agent"] == "deck-coder"


def test_a_repo_that_is_not_a_slug_withholds_even_though_it_names_the_crew(
        SS, monkeypatch, tmp_path):
    """`"../CFO"` is not a registry slug, so the lookup answers "not private" —
    while still naming CFO to anyone reading the row. Evading the gate and
    identifying the crew are not mutually exclusive, which an earlier version
    of this gate's reasoning assumed. The charset check is what closes it."""
    _active_with(tmp_path, monkeypatch, SS,
                 repo="../CFO", agent="cfo-analyst", run_id="run-sneak",
                 task="ledger work")
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"cfo": {"private": True}}))
    monkeypatch.setattr(_private_crew(SS), "_REGISTRY", str(registry))

    (run,) = SS.live_agents("/srv/convivy/substrate")["agent_runs"]
    for field in ("task", "repo", "run_id", "started_at", "model"):
        assert run[field] is None, f"{field} crossed for a slug-shaped evasion"


def test_a_private_crew_is_matched_case_insensitively(SS, monkeypatch, tmp_path):
    """The registry's slug is `cfo` and its home is `CFO`. A privacy question
    matched case-sensitively would answer "not private" for one spelling of the
    same crew, and matching more broadly withholds more."""
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"cfo": {"private": True}}))
    for spelling in ("cfo", "CFO", "Cfo"):
        _active_with(tmp_path, monkeypatch, SS, repo=spelling,
                     agent="a", run_id="r", task="t")
        monkeypatch.setattr(_private_crew(SS), "_REGISTRY", str(registry))
        (run,) = SS.live_agents("/srv/convivy/substrate")["agent_runs"]
        assert run["repo"] is None, spelling


def test_an_unreadable_crew_registry_withholds(SS, monkeypatch, tmp_path):
    """`dispatch_is_private` reports "cannot tell" by raising rather than
    guessing. This side is a disclosure, so cannot-tell withholds."""
    _active_with(tmp_path, monkeypatch, SS, repo="convivy-deck",
                 agent="a", run_id="r", task="t")

    def _boom():
        raise FileNotFoundError("config/projects.json")
    monkeypatch.setattr(SS.projects, "private_slugs", _boom)

    (run,) = SS.live_agents("/srv/convivy/convivy-deck")["agent_runs"]
    assert run["task"] is None
    assert run["agent"] == "a", "the row still draws"
