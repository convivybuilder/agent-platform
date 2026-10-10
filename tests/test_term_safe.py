"""term_safe — unit tests for the project-wide terminal sanitization boundary.

Covers all character classes stripped by safe(): C0+DEL (PR #255), C1 controls,
bidi override characters, and invisible-character hiding (PR #262). Also verifies
that legitimate non-Latin text (Arabic, Persian, emoji ZWJ sequences) survives
intact — stripping ZWJ/ZWNJ would corrupt those, so they are deliberately excluded
from the translate table.
"""
from __future__ import annotations

from term_safe import safe


# ---------------------------------------------------------------------------
# C0 + DEL (existing coverage, quick sanity check)
# ---------------------------------------------------------------------------

def test_c0_stripped():
    assert safe("a\x00b\x07c\x1fd") == "abcd"


def test_del_stripped():
    assert safe("a\x7fb") == "ab"


def test_escape_stripped():
    assert "\x1b" not in safe("\x1b[2Jcleared")


# ---------------------------------------------------------------------------
# C1 controls (0x80–0x9F) — security-reviewer finding, PR #262
# ---------------------------------------------------------------------------

def test_c1_csi_stripped():
    """U+009B is the C1 CSI — 8-bit equivalent of ESC [. Terminals in 8-bit mode
    interpret it as the start of a control sequence."""
    assert "\x9b" not in safe("before\x9b2Jafter")


def test_c1_osc_stripped():
    """U+009D is the C1 OSC — 8-bit equivalent of ESC ]."""
    assert "\x9d" not in safe("before\x9d0;title\x9cafter")


def test_c1_range_all_stripped():
    payload = "".join(chr(c) for c in range(0x80, 0xa0))
    assert safe(payload) == ""


# ---------------------------------------------------------------------------
# Bidi override characters — Trojan Source class (PR #262)
# ---------------------------------------------------------------------------

def test_rlo_stripped():
    """U+202E RIGHT-TO-LEFT OVERRIDE: makes subsequent text appear reversed."""
    title = "safe prefix ‮ esuf etaf"
    assert "‮" not in safe(title)
    assert "safe prefix" in safe(title)


def test_bidi_embeddings_stripped():
    """U+202A–U+202D: LRE, RLE, PDF, LRO."""
    for cp in (0x202A, 0x202B, 0x202C, 0x202D):
        assert chr(cp) not in safe(f"a{chr(cp)}b"), f"U+{cp:04X} not stripped"


def test_bidi_isolates_stripped():
    """U+2066–U+2069: LRI, RLI, FSI, PDI."""
    for cp in (0x2066, 0x2067, 0x2068, 0x2069):
        assert chr(cp) not in safe(f"a{chr(cp)}b"), f"U+{cp:04X} not stripped"


def test_zero_width_space_stripped():
    """U+200B ZERO WIDTH SPACE: invisible character that can hide content."""
    assert "​" not in safe("hid​den")
    assert "hidden" in safe("hid​den")


def test_bom_stripped():
    """U+FEFF BOM / ZERO WIDTH NO-BREAK SPACE."""
    assert "﻿" not in safe("﻿real title")
    assert "real title" in safe("﻿real title")


# ---------------------------------------------------------------------------
# Directional marks (U+200E, U+200F) — defence in depth
# ---------------------------------------------------------------------------

def test_lrm_stripped():
    """U+200E LEFT-TO-RIGHT MARK: invisible directional mark; stripped for
    defence in depth in non-terminal rendering contexts."""
    lrm = "‎"
    assert lrm not in safe(f"be{lrm}fore")
    assert "before" in safe(f"be{lrm}fore")


def test_rlm_stripped():
    """U+200F RIGHT-TO-LEFT MARK: invisible directional mark; makes following
    text inherit RTL directionality in bidi-aware renderers."""
    rlm = "‏"
    assert rlm not in safe(f"he{rlm}llo")
    assert "hello" in safe(f"he{rlm}llo")


def test_alm_stripped():
    """U+061C ARABIC LETTER MARK: invisible directional mark added in Unicode 6.3;
    sets directionality for adjacent runs in the bidi algorithm."""
    alm = "؜"
    assert alm not in safe(f"he{alm}llo")
    assert "hello" in safe(f"he{alm}llo")


# ---------------------------------------------------------------------------
# NOT stripped: ZWNJ (U+200C) and ZWJ (U+200D)
# ---------------------------------------------------------------------------

def test_zwnj_preserved():
    """U+200C ZERO WIDTH NON-JOINER is required for correct Arabic and Persian
    rendering (e.g. preventing ligature between ی and ر in می‌روم). Stripping it
    would corrupt legitimate non-Latin titles."""
    persian = "می‌روم"  # "mi-ravam" with ZWNJ preventing ligature
    assert safe(persian) == persian


def test_zwj_preserved():
    """U+200D ZERO WIDTH JOINER is required for emoji ZWJ sequences and Indic
    script rendering. Stripping it breaks emoji family glyphs and Devanagari."""
    emoji = "👨‍💻"  # man + ZWJ + laptop → 👨‍💻
    assert safe(emoji) == emoji


def test_mixed_latin_arabic_survives():
    """A title that mixes scripts (legitimate in bilingual issue trackers) passes
    through with no corruption."""
    title = "Fix: رفع اشکال در می‌روم (Persian)"
    result = safe(title)
    assert "رفع اشکال" in result
    assert "‌" in result


def test_plain_ascii_unchanged():
    assert safe("fix: add null check in auth handler") == "fix: add null check in auth handler"


def test_none_input():
    assert safe(None) == ""
