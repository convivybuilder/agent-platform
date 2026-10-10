"""session_state._sanitise_error — the picker_render import-failure line.

The picker_render import-failure line writes its exception text to stderr,
where Deck and the logs consume it. Since #315 this was its own sanitiser;
since the consolidation (issue_01KYSG57XQMP3PF2W76QJ775B5) it delegates to the
shared signal_client.sanitise with the 500-char cap. The full escape/
whitespace/redaction coverage now lives in test_sanitise.py; this file pins
the wrapper's distinctive contract: the default cap, the strengthened
credential redaction, and the clean pass-through.
"""
from __future__ import annotations

from session_state import _sanitise_error


def test_delegates_to_shared_sanitise_for_escapes():
    assert _sanitise_error("\x1b[31mred\x1b[0m") == "red"


def test_delegates_to_shared_sanitise_for_whitespace():
    assert _sanitise_error("line one\nline two") == "line one line two"


def test_redacts_credentials():
    """Redaction is inherited from the shared helper — an exception message
    that happens to carry a credential-shaped value loses it before stderr."""
    result = _sanitise_error("token=ghp_abcdef1234567890abcdef1234567890abcd")
    assert result == "token=[redacted]"


def test_short_message_unchanged():
    assert _sanitise_error("no such module named foo") == "no such module named foo"


def test_plain_message_unchanged():
    assert _sanitise_error("no module named 'picker_render'") == "no module named 'picker_render'"


# ---------------------------------------------------------------------------
# Length cap (the wrapper's own contract; the filler stays under the redactor's
# 32-char run threshold so the cap, not the redactor, does the truncation)
# ---------------------------------------------------------------------------

def test_capped_at_default_limit():
    msg = "ab " * 200  # 600 chars before collapse, 599 after
    result = _sanitise_error(msg)
    assert len(result) == 503  # 500 chars + "..."
    assert result.endswith("...")


def test_at_limit_passes_through():
    msg = "ab " * 167  # collapses to exactly 500 chars
    assert _sanitise_error(msg) == "ab " * 166 + "ab"


def test_one_past_limit_is_capped():
    msg = "ab " * 168  # collapses to 503 chars
    result = _sanitise_error(msg)
    assert len(result) == 503
    assert result == ("ab " * 166 + "ab") + "..."
