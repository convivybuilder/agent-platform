"""sanitise — unit tests for the shared escape-stripping + credential-redaction
helper (signal_client.sanitise, issue_01KYSG57XQMP3PF2W76QJ775B5).

Every alert body that used to go to a private Slack channel now renders on a
tiered-readership page, so output the platform did not author (subprocess
stderr/stdout, log lines, exception text, API response bodies, file contents)
must pass through `sanitise` before it lands in a body or meta. This is the ONE
shared implementation, consolidated here from the two prior variants
(bin/overnight-healthcheck._sanitise_launcher_line, #317, and
lib/agent-platform/session_state._sanitise_error, #315) after review flagged
their drift on both PRs.

Eight dimensions: NFC normalisation, line/paragraph-separator → space,
ANSI/terminal escape stripping (CSI, DEC private, OSC, SS3, two-byte Fe), C0
control removal, Default_Ignorable Unicode codepoint removal (category Cf —
zero-width and bidi-control codepoints, #595 round 3 — widened in round 4 to
the full Default_Ignorable_Code_Point property, which round 5 then found was
the wrong target for DISPLAY specifically: some Default_Ignorable codepoints
have no glyph of their own but control how ADJACENT codepoints render, so
removing them changes what a reader sees. Round 5 splits the property in two
— see `signal_client.is_default_ignorable` (the full property, used by
identity folding in bin/pr-state-capture) and
`signal_client._DISPLAY_KEEP_CODEPOINTS` (what this file's `sanitise` calls
keep anyway) — and replaces the round-4 name-substring heuristic with the
property's own published range table, closing the gap security-reviewer's
round-4 finding found in it (Khmer inherent-vowel marks, several reserved
ranges)), credential-prefix redaction, long-run redaction (with the 40-char
lowercase hex exemption for git shas), and the optional length cap.
"""
from __future__ import annotations

from pathlib import Path

import signal_client
from signal_client import (_DEFAULT_IGNORABLE_RANGES, is_default_ignorable, sanitise,
                           sanitise_body)


# ---------------------------------------------------------------------------
# Newline / CR / tab / VT / FF / NEL / Unicode separators → space
# ---------------------------------------------------------------------------

def test_newlines_collapsed():
    assert sanitise("line one\nline two") == "line one line two"


def test_carriage_return_collapsed():
    assert sanitise("a\rb") == "a b"


def test_tab_collapsed():
    assert sanitise("a\tb") == "a b"


def test_mixed_whitespace_collapsed():
    assert sanitise("a\n\r\tb") == "a b"


def test_multiple_consecutive_newlines_collapsed():
    assert sanitise("a\n\n\nb") == "a b"


def test_vertical_tab_collapsed():
    assert sanitise("a\vb") == "a b"


def test_form_feed_collapsed():
    assert sanitise("a\fb") == "a b"


def test_next_line_collapsed():
    """NEL (U+0085) is a C1 control treated as a line terminator."""
    assert sanitise("a\x85b") == "a b"


def test_line_separator_collapsed():
    """Unicode line separator (U+2028) collapsed like \\n."""
    assert sanitise("a b") == "a b"


def test_paragraph_separator_collapsed():
    """Unicode paragraph separator (U+2029) collapsed like \\n."""
    assert sanitise("a b") == "a b"


def test_crlf_collapsed():
    assert sanitise("line1\nline2\r\nline3") == "line1 line2 line3"


def test_trailing_whitespace_stripped():
    assert sanitise("  text  ") == "text"


def test_clean_text_passthrough():
    assert sanitise("command not found: foo") == "command not found: foo"


# ---------------------------------------------------------------------------
# CSI escape sequences stripped (original coverage, regressions)
# ---------------------------------------------------------------------------

def test_sgr_stripped():
    assert sanitise("\x1b[31mred\x1b[0m") == "red"


def test_csi_cursor_move_stripped():
    assert sanitise("before\x1b[2Kafter") == "beforeafter"


def test_csi_erase_display_stripped():
    assert sanitise("a\x1b[2Jb") == "ab"


def test_csi_without_parameters_stripped():
    assert sanitise("a\x1b[mb") == "ab"


def test_csi_and_newlines_both_sanitised():
    """Both dimensions together, as in a real pathological message."""
    nasty = "\x1b[31mModule\nerror\x1b[0m"
    assert sanitise(nasty) == "Module error"


# ---------------------------------------------------------------------------
# Broader terminal escape coverage
# ---------------------------------------------------------------------------

# -- DEC private-mode sequences (review #315, ARCH-066)
def test_dec_private_mode_hide_stripped():
    """\\x1b[?25l — DEC private-mode hide cursor."""
    assert sanitise("\x1b[?25l") == ""


def test_dec_private_mode_show_stripped():
    """\\x1b[?25h — DEC private-mode show cursor."""
    assert sanitise("\x1b[?25h") == ""


def test_dec_private_with_content_stripped():
    assert sanitise("\x1b[?25lhidden") == "hidden"


# -- OSC (Operating System Command) sequences
def test_osc_8_hyperlink_neutralised():
    """OSC 8 hyperlink: introducer + BEL stripped, only text survives."""
    msg = "\x1b]8;;https://evil.example\x07deceptive text\x1b]8;;\x07"
    result = sanitise(msg)
    # Security property: no escape sequences or BELs survive
    assert "\x1b" not in result
    assert "\x07" not in result
    assert "deceptive text" in result
    # Regression tripwire: exact output. The ANSI regex consumes \x1b]8;;h
    # as one escape (h is in the f-n final-byte range), so the leading "h"
    # of "https" is eaten. Update this assertion if the consumption behaviour
    # changes; do not weaken the security-property assertions above.
    assert result == "ttps://evil.exampledeceptive text;;"


def test_osc_0_title_neutralised():
    """OSC 0 (window title): escape stripped, title text may survive."""
    msg = "\x1b]0;mytitle\x07hello"
    result = sanitise(msg)
    # Security property: no escape sequences or BELs survive
    assert "\x1b" not in result and "\x07" not in result
    # Regression tripwire: exact output. The ANSI regex consumes \x1b]0;m
    # as one escape (m is in the f-n final-byte range). Update this assertion
    # if the consumption changes; do not weaken the property assertion above.
    assert result == "ytitlehello"


def test_osc_with_st_terminator_neutralised():
    """OSC terminated by ESC \\ (string terminator) rather than BEL."""
    msg = "\x1b]8;;link\x1b\\text"
    result = sanitise(msg)
    assert "\x1b" not in result


def test_osc_introducer_and_bel_leave_inert_text():
    result = sanitise("\x1b]8;;url\x07link\x1b]8;;\x07")
    assert "\x1b" not in result
    assert "\x07" not in result
    assert "url" in result
    assert "link" in result


# -- SS3 (Single Shift 3) sequences
def test_ss3_stripped():
    """\\x1bOP — SS3 F1 (O is the SS3 final byte, consumed as the escape)."""
    result = sanitise("\x1bOP")
    assert result == "P"
    # The first alternative matches \x1bO (O is in the A-O final-byte range
    # with no prefix chars or parameters). P is a bare printable character
    # left by the substitution — it is NOT part of the escape and surviving
    # is correct.


def test_ss3_escape_bytes_consumed():
    result = sanitise("abc\x1bOAdef")
    assert "\x1b" not in result
    assert "abc" in result
    assert "def" in result


# -- Two-byte Fe sequences
def test_reverse_index_stripped():
    assert sanitise("a\x1bMb") == "ab"


def test_save_cursor_stripped():
    assert sanitise("\x1b7") == ""


def test_restore_cursor_stripped():
    assert sanitise("\x1b8") == ""


def test_alt_keypad_stripped():
    assert sanitise("\x1b=") == ""


def test_std_keypad_stripped():
    assert sanitise("\x1b>") == ""


def test_fe_escape_bytes_consumed():
    result = sanitise("before\x1bM after")
    assert "\x1b" not in result
    assert "before" in result
    assert "after" in result


# ---------------------------------------------------------------------------
# C0 control characters stripped
# ---------------------------------------------------------------------------

def test_bel_stripped():
    """BEL (\\x07) — terminates OSC but is a bare control, not a separator."""
    assert sanitise("a\x07b") == "ab"


def test_lone_bel_stripped_within_text():
    result = sanitise("text\x07more")
    assert "\x07" not in result
    assert result == "textmore"


# ---------------------------------------------------------------------------
# Unicode format characters (category Cf) stripped — #595 round 3
# ---------------------------------------------------------------------------

def test_zero_width_space_stripped():
    assert sanitise("a​b") == "ab"


def test_zero_width_non_joiner_kept():
    """Round 5 reverses round 3 here: ZWNJ forces separate letterforms in
    Persian/Urdu and suppresses conjunct ligatures in Devanagari, so removing
    it changes rendering — it stays in `_DISPLAY_KEEP_CODEPOINTS`."""
    assert sanitise("a‌b") == "a‌b"


def test_zero_width_joiner_kept():
    """Round 5 reverses round 3 here too: ZWJ joins adjacent emoji into one
    glyph (see the family-emoji test below), so blanket-stripping it broke
    real rendering — it stays in `_DISPLAY_KEEP_CODEPOINTS`."""
    assert sanitise("a‍b") == "a‍b"


def test_word_joiner_stripped():
    assert sanitise("a⁠b") == "ab"


def test_byte_order_mark_stripped():
    assert sanitise("﻿a") == "a"


def test_left_to_right_and_right_to_left_marks_stripped():
    assert sanitise("a‎‏b") == "ab"


def test_bidi_override_controls_stripped():
    """U+202A-U+202E: the LRE/RLE/PDF/LRO/RLO embedding and override family."""
    assert sanitise("a‪‫‬‭‮b") == "ab"


def test_bidi_isolate_controls_stripped():
    """U+2066-U+2069: the LRI/RLI/FSI/PDI isolate family."""
    assert sanitise("a⁦⁧⁨⁩b") == "ab"


def test_rtlo_spoofing_neutralised():
    """A right-to-left override (U+202E) can make rendered text read as
    something other than its own bytes — stripping it removes the spoofing
    primitive, not just the invisibility."""
    result = sanitise("safe‮txt.exe")
    assert "‮" not in result
    assert result == "safetxt.exe"


def test_invisible_character_toggle_produces_identical_output():
    """The dedup-key finding: two titles differing only by a trailing
    zero-width space must sanitise to the SAME string, or an invisible edit
    mints a new identity."""
    assert sanitise("Fix the bug") == sanitise("Fix the bug​")


def test_format_char_stripping_composes_with_ansi_and_whitespace():
    nasty = "\x1b[31mred​\ntext\x1b[0m"
    assert sanitise(nasty) == "red text"


# ---------------------------------------------------------------------------
# NFC normalisation — #595 round 3
# ---------------------------------------------------------------------------

def test_decomposed_and_composed_forms_normalise_identically():
    """"é" as one precomposed codepoint (NFC) and as "e" + a combining acute
    accent (NFD) look and read identically but are different byte sequences
    — normalising both to NFC is what gives a dedup key the same identity
    stability for combining-character sequences that Cf-stripping gives for
    invisible characters."""
    composed = "café"
    decomposed = "café"
    assert composed != decomposed  # distinct as Python strings, pre-sanitise
    assert sanitise(composed) == sanitise(decomposed) == "café"


def test_ordinary_multilingual_text_unaffected():
    assert sanitise("héllo wörld") == "héllo wörld"


# ---------------------------------------------------------------------------
# Default_Ignorable widening beyond category Cf — #595 round 4
# ---------------------------------------------------------------------------
#
# security-reviewer's round-4 finding ran the round-3 `sanitise()` against
# `base` and `base + <char>` for five codepoints outside category Cf that
# still render with no visible glyph, and confirmed each toggled the
# sanitised string. Four of the five are still stripped from display in
# round 5 (named here individually, the same shape as round 3's own
# per-character Cf tests above); VARIATION SELECTOR-16 is not — see the
# round-5 section below for why, and for the family-emoji/gitmoji cases that
# proved stripping it broke real rendering.

def test_combining_grapheme_joiner_stripped():
    """U+034F COMBINING GRAPHEME JOINER — category Mn, not Cf. Affects
    grapheme-CLUSTER boundaries (cursor/selection semantics), not what glyph
    renders, so it stays stripped in round 5's narrower display set."""
    assert sanitise("a͏b") == "ab"


def test_hangul_filler_stripped():
    """U+3164 HANGUL FILLER — category Lo, defined to render as a blank
    placeholder glyph."""
    assert sanitise("aㅤb") == "ab"


def test_halfwidth_hangul_filler_stripped():
    """U+FFA0 HALFWIDTH HANGUL FILLER — category Lo, same reason."""
    assert sanitise("aﾠb") == "ab"


def test_hangul_choseong_filler_stripped():
    """U+115F HANGUL CHOSEONG FILLER — category Lo, same reason."""
    assert sanitise("aᅟb") == "ab"


def test_default_ignorable_toggle_produces_identical_output():
    """The round-4 finding, for the four named codepoints round 5 keeps
    stripped from display: a title differing only by one of these must
    sanitise identically, the same property round 3 established for
    category Cf alone. VARIATION SELECTOR-16 is excluded from this loop —
    round 5 keeps it in display, so this equality no longer holds for it
    (see test_variation_selector_kept_for_emoji_presentation below)."""
    base = "Fix the bug"
    for ch in ("͏", "ㅤ", "ﾠ", "ᅟ"):
        assert sanitise(base) == sanitise(base + ch), ch


def test_is_default_ignorable_does_not_strip_ordinary_combining_marks():
    """The Default_Ignorable_Code_Point property is deliberately narrow: most
    category-Mn characters are ordinary combining diacritics that change what
    a word says (an Arabic fatha, a Devanagari vowel sign) and are not in
    `_DEFAULT_IGNORABLE_RANGES`, even though some of the property's members
    (the variation selectors, the combining grapheme joiner) share their Mn
    category."""
    fatha = "َ"  # ARABIC FATHA, category Mn
    assert not is_default_ignorable(fatha)
    assert sanitise("ب" + fatha) == "ب" + fatha


def test_is_default_ignorable_true_for_the_five_named_codepoints():
    """is_default_ignorable itself is unaffected by round 5's display keep
    list — all five round-4 codepoints are still formally Default_Ignorable,
    which is exactly why identity folding (bin/pr-state-capture) still
    removes VARIATION SELECTOR-16 even though display no longer does."""
    for ch in ("͏", "️", "ㅤ", "ﾠ", "ᅟ"):
        assert is_default_ignorable(ch), ch


# ---------------------------------------------------------------------------
# DISPLAY keeps ZWJ, variation selectors, ZWNJ and soft hyphen — #595 round 5
# ---------------------------------------------------------------------------
#
# Round 4 stripped the full Default_Ignorable_Code_Point property from
# display and broke real rendering: a codepoint can have no glyph of its own
# while still controlling how ADJACENT codepoints render. Round 5 carves out
# `_DISPLAY_KEEP_CODEPOINTS` for exactly the codepoints where that's true, and
# replaces `is_default_ignorable`'s name-substring heuristic with the
# property's own published range table (`_DEFAULT_IGNORABLE_RANGES`),
# transcribed from Unicode 14.0.0's DerivedCoreProperties.txt.

def test_zwj_joined_family_emoji_kept_intact():
    """reviewer's round-4 finding, verified directly: stripping ZWJ split a
    joined family into four separate person emoji."""
    family = "👨‍👩‍👧‍👦"
    assert sanitise(family) == family
    assert "‍" in sanitise(family)


def test_variation_selector_kept_for_emoji_presentation():
    """reviewer's round-4 finding, verified directly: stripping VS16 lost
    the forced emoji presentation a gitmoji-style "recycle" title depends
    on, rendering as a plain glyph."""
    recycle = "♻️"
    assert sanitise(recycle) == recycle
    assert "️" in sanitise(recycle)


def test_zero_width_non_joiner_preserves_persian_letterforms():
    """A ZWNJ between two Arabic-script letters keeps them in their
    isolated/final forms instead of joining; stripping it would join glyphs
    the author deliberately kept apart — same class of harm as stripping
    ZWJ."""
    persian = "می‌خواهم"  # "mikhāham" — ZWNJ keeps the prefix unjoined
    assert sanitise(persian) == persian


def test_soft_hyphen_kept():
    """Invisible everywhere except exactly at a line-break position, where a
    wrapping renderer shows it as a hyphen — its absence is a conditional but
    real visible difference, so round 5 keeps it rather than strip it blind."""
    assert sanitise("compre­hensive") == "compre­hensive"


def test_zero_width_space_still_stripped():
    """Contrast with the kept set above: ZWSP has no glyph and no joining or
    selection function, so removing it cannot change what a reader sees."""
    assert sanitise("a​b") == "ab"


def test_word_joiner_still_stripped():
    assert sanitise("a⁠b") == "ab"


def test_bom_still_stripped():
    assert sanitise("﻿a") == "a"


def test_display_keep_codepoints_are_still_default_ignorable():
    """The keep list doesn't change what `is_default_ignorable` reports —
    only what `sanitise` deletes. Identity folding relies on this: it calls
    `is_default_ignorable` directly, so it still removes every one of these."""
    for ch in ("‌", "‍", "­", "️", "\U000e0100"):
        assert is_default_ignorable(ch), ch


# ---------------------------------------------------------------------------
# The authoritative range table itself — #595 round 5
# ---------------------------------------------------------------------------

def test_default_ignorable_ranges_total_4174_codepoints():
    """Cross-check against Unicode 14.0.0 DerivedCoreProperties.txt's own
    "# Total code points: 4174" line for this property."""
    total = sum(hi - lo + 1 for lo, hi in _DEFAULT_IGNORABLE_RANGES)
    assert total == 4174


def test_default_ignorable_ranges_are_sorted_and_non_overlapping():
    """is_default_ignorable's early-exit lookup depends on this."""
    for (lo1, hi1), (lo2, hi2) in zip(_DEFAULT_IGNORABLE_RANGES, _DEFAULT_IGNORABLE_RANGES[1:]):
        assert lo1 <= hi1 < lo2 <= hi2


def test_every_range_boundary_is_default_ignorable():
    """Both endpoints of every declared range, interior to the property."""
    for lo, hi in _DEFAULT_IGNORABLE_RANGES:
        assert is_default_ignorable(chr(lo)), hex(lo)
        assert is_default_ignorable(chr(hi)), hex(hi)


def test_codepoints_just_outside_a_real_gap_are_not_default_ignorable():
    """Where two declared ranges are NOT adjacent, the codepoint immediately
    before the next range and immediately after the previous one must both
    fall outside the property — the boundary the range table exists to
    enforce precisely."""
    ranges = _DEFAULT_IGNORABLE_RANGES
    for i, (lo, hi) in enumerate(ranges):
        if i > 0:
            prev_hi = ranges[i - 1][1]
            if lo > prev_hi + 1:
                assert not is_default_ignorable(chr(lo - 1)), hex(lo - 1)
        if i < len(ranges) - 1:
            next_lo = ranges[i + 1][0]
            if next_lo > hi + 1:
                assert not is_default_ignorable(chr(hi + 1)), hex(hi + 1)


def test_khmer_inherent_vowel_marks_are_default_ignorable():
    """security-reviewer's round-4 gap #1: assigned Khmer marks (category
    Mn) matching neither "VARIATION SELECTOR" nor "COMBINING GRAPHEME
    JOINER" in their Unicode name, so the round-4 name heuristic missed
    them. The range table has no such gap."""
    assert is_default_ignorable("឴")  # KHMER VOWEL INHERENT AQ
    assert is_default_ignorable("឵")  # KHMER VOWEL INHERENT AA


def test_reserved_default_ignorable_codepoints_are_covered():
    """security-reviewer's round-4 gap #2: codepoints Unicode reserves for
    future Default_Ignorable assignment (category Cn), invisible to any
    name lookup since `unicodedata.name()` returns "" for an unassigned
    codepoint."""
    assert is_default_ignorable("⁥")
    assert is_default_ignorable("￴")
    assert is_default_ignorable("\U000e0002")


def test_interior_tag_and_variation_supplement_codepoints():
    """A handful of interior (non-boundary) spot checks: a tag character
    from an emoji flag-subdivision sequence (e.g. Scotland's 🏴󠁧󠁢󠁳󠁣󠁴󠁿) and a
    variation selector from the supplementary block."""
    assert is_default_ignorable("\U000e0067")  # TAG LATIN SMALL LETTER G
    assert is_default_ignorable("\U000e0150")  # VARIATION SELECTOR-81


def test_ordinary_ascii_and_cjk_are_not_default_ignorable():
    for ch in "Aa0 café修复":
        assert not is_default_ignorable(ch), ch


# ---------------------------------------------------------------------------
# Credential redaction
# ---------------------------------------------------------------------------

def test_creds_prefix_sk():
    assert sanitise("key=sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789") == "key=[redacted]"


def test_creds_standalone_sk():
    result = sanitise("sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz01234567890123")
    assert "sk-" in result
    assert "[redacted]" in result
    assert "AbCdEfGhIjKlMnOpQrStUvWxYz01234567890123" not in result


def test_creds_prefix_ghp():
    assert sanitise("token=ghp_abcdef1234567890abcdef1234567890abcd") == "token=[redacted]"


def test_creds_standalone_ghp():
    result = sanitise("ghp_abcdef1234567890abcdef1234567890abcd")
    assert "ghp_" in result
    assert "[redacted]" in result
    assert "abcdef1234567890abcdef1234567890abcd" not in result


def test_creds_prefix_github_pat():
    result = sanitise("using github_pat_abcdef1234567890abcdef1234567890abcdef")
    assert "github_pat_" in result
    assert "[redacted]" in result
    assert "abcdef1234567890abcdef1234567890abcdef" not in result


def test_creds_prefix_bearer():
    assert sanitise(
        "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ) == "Authorization: Bearer [redacted]"


def test_creds_prefix_token_eq():
    assert sanitise(
        "token=sk-ant-api03-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    ) == "token=[redacted]"


def test_creds_prefix_key_eq():
    assert sanitise("key=supersecretapikey1234567890") == "key=[redacted]"


def test_creds_prefix_password():
    assert sanitise(
        "password=MyP@ssw0rd!ThisIsVerySecret12345"
    ) == "password=[redacted]"


def test_redacts_long_base64():
    b64 = "dGhpcyBpcyBhIHRlc3Qgc3RyaW5nIHRoYXQgaXMgdmVyeSBsb25nIGFuZCBib3Jpbmc="
    assert len(b64) >= 32
    assert sanitise(b64) == "[redacted]"


def test_preserves_git_sha():
    """A 40-char lowercase git sha is NOT redacted: diagnostic, not secret."""
    sha = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"
    assert len(sha) == 40
    result = sanitise(f"commit {sha}")
    assert sha in result
    assert "[redacted]" not in result


def test_hex_exemption_deliberate():
    """40-char lowercase-hex exemption is deliberate — a bare 40-hex legacy
    PAT passes unredacted (indistinguishable from a git sha)."""
    legacy_token = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"
    assert sanitise(legacy_token) == legacy_token  # not redacted
    # Same value with a known prefix IS caught by _CRED_PREFIX_RE
    assert sanitise(f"key={legacy_token}") == "key=[redacted]"


def test_preserves_file_path():
    """A long file path is not redacted (no 32-char alphanumeric run)."""
    path = f"{Path.home()}/Convivy/agent-runs/2026-07-30/overnight-launcher.err.log"
    assert sanitise(path) == path


# ---------------------------------------------------------------------------
# Long-run redaction survives a _DISPLAY_KEEP_CODEPOINTS splice — #595 round 6
# ---------------------------------------------------------------------------
#
# security-reviewer's round-6 blocking finding: round 5 kept ZWJ, variation
# selectors, ZWNJ and soft hyphen in DISPLAY output (the section above), but
# `sanitise` runs that strip BEFORE the long-run redaction below it — a
# secret with one of those four kept codepoints spliced in survives the
# strip and reaches `_LONG_RUN_RE` intact, where the old flat
# `[A-Za-z0-9+/=]{32,}` class didn't span the splice: each side fell under
# the 32-char minimum and the whole secret was emitted verbatim. Regressing
# from "redacted" (round 5 and earlier, for any of these four codepoints —
# they weren't kept until round 5) to "emitted in full" is worse than the
# rendering bug round 5 fixed. `_LONG_RUN_RE` now tolerates any run of
# `_DISPLAY_KEEP_RANGES` codepoints between consecutive base64/hex
# characters, so the whole spliced span — including the kept codepoints —
# still redacts.

_B64_RUN = "dGhpcyBpcyBhIHRlc3Qgc3RyaW5nIHRoYXQgaXMgdmVyeSBsb25nIGFuZCBib3Jpbmc="


def _splice_between_every_pair(s: str, ch: str) -> str:
    """`ch` inserted after every character of `s` except the last — "between
    every pair of characters", the round-6 fix's hardest case."""
    return "".join(c + (ch if i != len(s) - 1 else "") for i, c in enumerate(s))


def test_long_run_redacted_with_zwj_spliced_in():
    spliced = _B64_RUN[:20] + "‍" + _B64_RUN[20:]
    result = sanitise(spliced)
    assert result == "[redacted]"
    assert _B64_RUN not in result


def test_long_run_redacted_with_variation_selector_spliced_in():
    spliced = _B64_RUN[:20] + "️" + _B64_RUN[20:]
    result = sanitise(spliced)
    assert result == "[redacted]"
    assert _B64_RUN not in result


def test_long_run_redacted_with_soft_hyphen_spliced_in():
    spliced = _B64_RUN[:20] + "­" + _B64_RUN[20:]
    result = sanitise(spliced)
    assert result == "[redacted]"
    assert _B64_RUN not in result


def test_long_run_redacted_with_zwnj_spliced_in():
    spliced = _B64_RUN[:20] + "‌" + _B64_RUN[20:]
    result = sanitise(spliced)
    assert result == "[redacted]"
    assert _B64_RUN not in result


def test_long_run_redacted_with_keep_codepoint_between_every_pair():
    spliced = _splice_between_every_pair(_B64_RUN, "‍")
    assert "‍" in spliced  # the splice helper actually spliced
    assert sanitise(spliced) == "[redacted]"


def test_long_run_not_yet_32_real_characters_is_left_alone():
    """A splice cannot manufacture a redaction out of fewer than 32 real
    base64/hex characters — the tolerant class only spans keep-listed
    codepoints between REAL characters, it doesn't count them. The ZWJs
    themselves stay, unredacted, since round 5 keeps them in display."""
    short = "AbCdEfGhIjKlMnOp0123456789+/=="  # 30 real chars
    assert len(short) < 32
    spliced = _splice_between_every_pair(short, "‍")
    assert sanitise(spliced) == spliced


def test_zwj_joined_family_emoji_still_intact_after_round_6_fix():
    """The redaction fix must not re-break round 5's rendering fix."""
    family = "👨‍👩‍👧‍👦"
    assert sanitise(family) == family
    assert "‍" in sanitise(family)


def test_gitmoji_recycle_still_intact_after_round_6_fix():
    recycle = "♻️"
    assert sanitise(recycle) == recycle
    assert "️" in sanitise(recycle)


# ---------------------------------------------------------------------------
# Round 7's isalpha() carve-out withdrawn — #595 round 8
# ---------------------------------------------------------------------------
#
# Round 7 (below, superseded) narrowed `_redact_long_run` so a
# tolerance-formed match with no digit or symbol in its real characters
# ("core.isalpha()") was returned unredacted, on the theory that a real
# secret always carries a digit or symbol. security-reviewer's round-7
# finding showed that's false: a BIP-39 recovery phrase is drawn from a
# fixed, lowercase-a-z-only 2048-word list, so a phrase with one soft hyphen
# spliced in cleared round 7's carve-out and was emitted byte-for-byte whole
# — a full wallet credential surviving. Any alphabetic secret plus one
# spliced ignorable character hits the same gap. No replacement syntactic
# heuristic is used: the reviewer's own analysis is that a BIP-39 phrase and
# a fabricated compound word are the same shape, so entropy, case and length
# tests all fail to separate them.
#
# The tests below (`test_soft_hyphen_joined_compound_word_now_redacted` and
# the BIP-39/alphabetic-secret group) replace the round-7 tests that
# asserted the carve-out's behaviour; the round-6 splice tests above this
# section (ZWJ/variation-selector/soft-hyphen/ZWNJ against `_B64_RUN` and
# `_MIXED_40`) were never exercising the carve-out (their cores hold digits)
# and are unaffected by its removal.

_MIXED_40 = "oHBvRPOIvGrv5iFlbCBFNOgmBjMtpsiaOclRz3Aw"  # 40 chars, mixed case + digits, no '+/='
_ALPHA_40 = "abcdefghij" * 4  # 40 chars, pure alphabetic, no digit or symbol
# First 12 words of the BIP-39 English wordlist (a fixed, published,
# lowercase-a-z-only 2048-word list) concatenated with no separator, as they
# would appear in a leaked log line with whitespace already collapsed —
# security-reviewer's round-7 counterexample. This is the standard public
# wordlist, not anyone's actual recovery phrase.
_BIP39_RUN = (
    "abandon" "ability" "able" "about" "above" "absent"
    "absorb" "abstract" "absurd" "abuse" "access" "accident"
)


def test_soft_hyphen_joined_compound_word_now_redacted():
    """security-reviewer's round-6 verified case (a real compound word using
    soft hyphens as legitimate syllable/line-break hints, not a credential)
    now redacts. This is the accepted cost of withdrawing round 7's
    carve-out, stated deliberately: round 6's security-reviewer already
    raised this over-redaction as NON-BLOCKING, and a cosmetic false
    positive on an ordinary word is preferable to the credential leak the
    carve-out reopened (round-7 finding, see the section comment above)."""
    word = "super­cali­fragilistic­expialidocious­wordthing"
    assert sanitise(word) == "[redacted]"


def test_bip39_phrase_spliced_with_soft_hyphen_is_redacted():
    """security-reviewer's round-7 counterexample: round 7's isalpha()
    carve-out let this exact shape of input — an alphabetic secret with one
    soft hyphen spliced in — through byte-for-byte. It must now redact."""
    assert len(_BIP39_RUN) >= 32
    spliced = _BIP39_RUN[:20] + "­" + _BIP39_RUN[20:]
    result = sanitise(spliced)
    assert result == "[redacted]"
    assert _BIP39_RUN not in result


def test_alphabetic_run_spliced_with_soft_hyphen_is_redacted():
    spliced = _ALPHA_40[:20] + "­" + _ALPHA_40[20:]
    result = sanitise(spliced)
    assert result == "[redacted]"
    assert _ALPHA_40 not in result


def test_alphabetic_run_spliced_with_zwj_is_redacted():
    spliced = _ALPHA_40[:20] + "‍" + _ALPHA_40[20:]
    result = sanitise(spliced)
    assert result == "[redacted]"
    assert _ALPHA_40 not in result


def test_alphabetic_run_spliced_with_variation_selector_is_redacted():
    spliced = _ALPHA_40[:20] + "️" + _ALPHA_40[20:]
    result = sanitise(spliced)
    assert result == "[redacted]"
    assert _ALPHA_40 not in result


def test_mixed_secret_spliced_with_soft_hyphens_still_redacted():
    """`_MIXED_40` has digits and mixed case, so it never depended on round
    7's carve-out; unaffected by the carve-out's removal."""
    assert len(_MIXED_40) == 40
    spliced = _splice_between_every_pair(_MIXED_40, "­")
    assert "­" in spliced
    result = sanitise(spliced)
    assert result == "[redacted]"
    assert _MIXED_40 not in result


def test_mixed_secret_spliced_with_zwj_still_redacted():
    spliced = _splice_between_every_pair(_MIXED_40, "‍")
    result = sanitise(spliced)
    assert result == "[redacted]"
    assert _MIXED_40 not in result


def test_mixed_secret_spliced_with_variation_selector_still_redacted():
    spliced = _splice_between_every_pair(_MIXED_40, "️")
    result = sanitise(spliced)
    assert result == "[redacted]"
    assert _MIXED_40 not in result


def test_mixed_secret_spliced_with_zwnj_still_redacted():
    spliced = _splice_between_every_pair(_MIXED_40, "‌")
    result = sanitise(spliced)
    assert result == "[redacted]"
    assert _MIXED_40 not in result


def test_long_run_without_any_keep_codepoint_still_always_redacted():
    """A plain solid run (no tolerance ever exercised, `s == core` in
    `_redact_long_run`) redacts regardless of content — unaffected by
    round 7's carve-out or its round-8 removal either way."""
    assert sanitise("x" * 600) == "[redacted]"
    # "z" rather than a hex digit — deliberately outside 0-9a-f so this run
    # doesn't accidentally clear the pre-existing git-sha exemption instead.
    plain_long_word = "z" * 40  # solid, no soft hyphens
    assert sanitise(plain_long_word) == "[redacted]"


# ---------------------------------------------------------------------------
# unicodedata.unidata_version drift check — #595 round 6
# ---------------------------------------------------------------------------
#
# security-reviewer's non-blocking round-6 finding: `_DEFAULT_IGNORABLE_RANGES`
# is a point-in-time transcription pinned to Unicode 14.0.0, with nothing that
# notices when the deployed interpreter's `unicodedata` disagrees. Chosen
# behaviour on a mismatch: warn to stderr, keep stripping the pinned table
# exactly as before — see `_check_unidata_version`'s docstring for why
# "refuse to strip" would reopen the round-2 RTLO-spoof hole this module
# exists to close, which is worse than an outdated table.

def test_unidata_version_match_is_silent(capsys):
    assert signal_client.unicodedata.unidata_version == signal_client._PINNED_UNIDATA_VERSION
    signal_client._check_unidata_version()
    assert capsys.readouterr().err == ""


def test_unidata_version_mismatch_warns_to_stderr(monkeypatch, capsys):
    monkeypatch.setattr(signal_client.unicodedata, "unidata_version", "99.0.0")
    signal_client._check_unidata_version()
    err = capsys.readouterr().err
    assert "99.0.0" in err
    assert signal_client._PINNED_UNIDATA_VERSION in err


def test_unidata_version_mismatch_does_not_change_stripping_behaviour(monkeypatch):
    """The chosen behaviour is warn-and-continue, not warn-and-refuse: a
    mismatch must not silently widen or narrow what `sanitise` strips."""
    monkeypatch.setattr(signal_client.unicodedata, "unidata_version", "99.0.0")
    assert sanitise("a​b") == "ab"  # ZWSP still stripped
    assert sanitise("a‍b") == "a‍b"  # ZWJ still kept
    spliced = _B64_RUN[:20] + "‍" + _B64_RUN[20:]
    assert sanitise(spliced) == "[redacted]"  # long-run redaction still tolerant


def test_pinned_unidata_version_matches_running_interpreter():
    """ROUND 7 (#595, security-reviewer non-blocking finding): the warning
    above prints to a launchd job's stderr log that nothing in this codebase
    reads, so a real drift — the running interpreter learning about
    Default_Ignorable codepoints `_DEFAULT_IGNORABLE_RANGES` has never heard
    of, since Unicode only ever adds to the property — could sit unnoticed
    indefinitely while silently reopening the exact splice/spoof bypass
    rounds 2-6 closed. This assertion is the real gate: unlike the print, a
    failure here is a red, blocking CI run, forcing `_DEFAULT_IGNORABLE_RANGES`
    and `_PINNED_UNIDATA_VERSION` to be re-transcribed from the new version's
    DerivedCoreProperties.txt together, deliberately, before anything ships
    against the stale table. This is NOT a monkeypatched test — it asserts
    the actual running interpreter, so it is meant to go red the day the pin
    genuinely drifts."""
    assert signal_client.unicodedata.unidata_version == signal_client._PINNED_UNIDATA_VERSION, (
        f"unicodedata.unidata_version is {signal_client.unicodedata.unidata_version!r}, not the "
        f"{signal_client._PINNED_UNIDATA_VERSION!r} _DEFAULT_IGNORABLE_RANGES was transcribed "
        "from — re-transcribe _DEFAULT_IGNORABLE_RANGES from this version's "
        "DerivedCoreProperties.txt and bump _PINNED_UNIDATA_VERSION together."
    )


# ---------------------------------------------------------------------------
# Length cap
# ---------------------------------------------------------------------------

def test_short_message_unchanged():
    assert sanitise("no such module named foo") == "no such module named foo"


def test_capped_at_given_limit():
    # "ab " repeated stays under the 32-char run threshold, so the cap (not
    # the redactor) does the truncation.
    msg = "ab " * 200
    result = sanitise(msg, max_len=100)
    assert len(result) == 103  # 100 chars + "..."
    assert result.endswith("...")


def test_at_limit_passes_through():
    # "ab " * 167 collapses to exactly 500 chars ("ab " * 166 + "ab"), so the
    # cap at 500 is a pass-through, not a truncation.
    msg = "ab " * 167
    result = sanitise(msg, max_len=500)
    assert result == ("ab " * 166 + "ab")
    assert len(result) == 500


def test_one_past_limit_is_capped():
    msg = "ab " * 168
    result = sanitise(msg, max_len=500)
    assert len(result) == 503
    assert result == ("ab " * 166 + "ab") + "..."


def test_no_cap_leaves_long_text_alone():
    assert sanitise("ab " * 200) == "ab " * 199 + "ab"


def test_cap_applies_after_redaction():
    """A long alphanumeric run is redacted before the cap sees it — the
    redactor is the stronger guarantee and must win."""
    assert sanitise("x" * 600, max_len=500) == "[redacted]"


# ---------------------------------------------------------------------------
# Clean message unchanged
# ---------------------------------------------------------------------------

def test_plain_message_unchanged():
    assert sanitise("no module named 'picker_render'") == "no module named 'picker_render'"


# --- sanitise_body: a multi-line body, each line sanitised on its own ---------

def test_sanitise_body_keeps_line_breaks_that_sanitise_would_flatten():
    """A body is multi-line, and `sanitise` collapses newlines, so the body helper must not."""
    assert sanitise_body("title\n\nfirst line\nsecond line") == "title\n\nfirst line\nsecond line"


def test_sanitise_body_sanitises_every_line_not_only_the_first():
    body = "ok\n\x1b[31mred\x1b[0m line\nkey token=abcdefghijklmnop"
    out = sanitise_body(body)
    assert "\x1b" not in out
    assert out.splitlines()[1] == "red line"
    assert out.splitlines()[2] == "key token=[redacted]"


def test_sanitise_body_of_nothing_is_nothing():
    assert sanitise_body("") == ""

