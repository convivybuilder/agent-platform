"""term_safe — strip control bytes from untrusted text before it reaches a terminal."""
from __future__ import annotations

# C0 control bytes (0x00–0x1F) plus DEL (0x7F), stripped from any externally-authored
# string before it is printed to a terminal. A terminal EXECUTES escape sequences rather
# than displaying them: a title carrying "\x1b[H\x1b[2Jco: session healthy, no issues
# waiting" clears the screen and plants text the reader attributes to the tool (security
# review, PR #255). Newline and tab go too; callers use fixed one-line-per-field layouts,
# and a value carrying either would break the alignment that makes the output readable,
# which is its own small forgery.
#
# First introduced in bin/issues-waiting, where forged output lands during co startup
# and inherits co's credibility. Lifted here when bin/triage-queue became the second
# caller (issue_01KYGPTQ5VPSQ8GG34KEJFEQR6).
#
# C1 control bytes (0x80–0x9F): 8-bit equivalents of C0 sequences — U+009B is the C1
# CSI (Control Sequence Introducer), U+009D is OSC. xterm and VTE-based terminals in
# 8-bit mode interpret these as control sequences; stripped because term_safe.py is the
# project-wide sanitization boundary (security review round 2, PR #262).
#
# Bidi override characters (U+202A–U+202E, U+2066–U+2069): Unicode directional controls
# that can make displayed text render right-to-left while the underlying bytes read
# left-to-right — the Trojan Source technique. An operator reading a [dry-run] line
# with U+202E embedded sees a different string than was filed. U+200B (ZERO WIDTH SPACE)
# and U+FEFF (BOM/ZWNBSP) are invisible-character hiding; stripped for the same reason.
#
# NOT stripped: U+200C (ZWNJ) and U+200D (ZWJ). ZWNJ is required for correct rendering
# of Arabic, Persian, and Indic scripts; ZWJ is required for emoji ZWJ sequences (e.g.
# 👨‍💻) and Indic script rendering. They carry no bidi-attack capability — Trojan Source
# depends on directional overrides, not joiners. Stripping them would corrupt legitimate
# non-Latin titles and break emoji family sequences.
#
# U+061C (ARABIC LETTER MARK), U+200E (LEFT-TO-RIGHT MARK), and U+200F (RIGHT-TO-LEFT MARK)
# are stripped — unlike ZWNJ/ZWJ, which influence how glyphs join, these marks carry
# directional effect and set directionality for adjacent runs in the Unicode bidi algorithm.
# Most terminal emulators do not implement Unicode bidi, so the practical impact on terminal
# output is minimal; stripped here as defence in depth for non-terminal rendering contexts
# (browsers, editors, log viewers) that do implement the bidi algorithm.
_CONTROL = {c: None for c in range(0x20)}
_CONTROL[0x7F] = None
# C1 controls
_CONTROL.update({c: None for c in range(0x80, 0xa0)})
# Bidi overrides and invisible-character hiding
_CONTROL.update({c: None for c in (
    0x200B,                                        # ZERO WIDTH SPACE
    0x061C, 0x200E, 0x200F,                        # ARABIC LETTER MARK, LEFT-TO-RIGHT MARK, RIGHT-TO-LEFT MARK
    0x202A, 0x202B, 0x202C, 0x202D, 0x202E,       # LRE, RLE, PDF, LRO, RLO
    0x2066, 0x2067, 0x2068, 0x2069,                # LRI, RLI, FSI, PDI
    0xFEFF,                                        # BOM / ZERO WIDTH NO-BREAK SPACE
)})


def safe(value) -> str:
    """Externally-authored text, made safe to print to a terminal."""
    return str(value or "").translate(_CONTROL)
