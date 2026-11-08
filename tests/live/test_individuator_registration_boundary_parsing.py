"""Unit check for `test_individuator_registration_boundary_live.py`'s own
refusal classifiers — the helpers that decide whether a connection failure
or an HTTP response is a genuine refusal or something else (a down Bosun, a
reset, a timeout, an unrelated refusal reason) that must never be read as
one.

Always runs (no `AGENTCTL_LIVE_INDIVIDUATOR` gate): in-memory exceptions and
response bodies only, never a real socket or TLS handshake.
"""

import json
import sqlite3
import ssl

import pytest

from test_individuator_registration_boundary_live import (
    _ATTEMPT_1_ALERTS,
    _ATTEMPT_4_REFUSAL_REASONS,
    _attempt_4_exc_mismatch,
    _attempt_4_http_mismatch,
    _refusal_mismatch,
    _safe_body_summary,
    _safe_http_summary,
    _session_count,
)


def _ssl_error(reason: str) -> ssl.SSLError:
    exc = ssl.SSLError(f"[SSL] test exception for reason {reason}")
    exc.reason = reason
    return exc


def test_certificate_required_reason_passes_attempt_1():
    exc = _ssl_error("TLSV13_ALERT_CERTIFICATE_REQUIRED")
    assert _refusal_mismatch(exc, _ATTEMPT_1_ALERTS) is None


def test_connection_refused_is_not_a_refusal():
    exc = ConnectionRefusedError("[Errno 61] Connection refused")
    mismatch = _refusal_mismatch(exc, _ATTEMPT_1_ALERTS)
    assert mismatch is not None
    assert "ConnectionRefusedError" in mismatch


def test_ssl_error_with_unrelated_reason_is_not_a_refusal():
    exc = _ssl_error("TLSV1_ALERT_UNKNOWN_CA")
    mismatch = _refusal_mismatch(exc, _ATTEMPT_1_ALERTS)
    assert mismatch is not None
    assert "TLSV1_ALERT_UNKNOWN_CA" in mismatch


# --- attempt 4's classifier: named HTTP refusal reasons and TLS alerts ----


def test_each_named_http_refusal_reason_passes_attempt_4():
    for reason in _ATTEMPT_4_REFUSAL_REASONS:
        assert _attempt_4_http_mismatch(403, {"refused": reason}) is None, reason


def test_each_expected_tls_alert_passes_attempt_4():
    for reason in ("TLSV1_ALERT_UNKNOWN_CA", "SSLV3_ALERT_BAD_CERTIFICATE"):
        assert _attempt_4_exc_mismatch(_ssl_error(reason)) is None, reason


def test_a_2xx_fails_attempt_4():
    mismatch = _attempt_4_http_mismatch(200, {"session_id": "s", "session_token": "t"})
    assert mismatch is not None
    assert "200" in mismatch


def test_an_http_refusal_with_an_unrelated_reason_fails_attempt_4():
    mismatch = _attempt_4_http_mismatch(400, {"refused": "malformed_body"})
    assert mismatch is not None
    assert "malformed_body" in mismatch


def test_a_connection_reset_fails_attempt_4():
    exc = ConnectionResetError("[Errno 54] Connection reset by peer")
    mismatch = _attempt_4_exc_mismatch(exc)
    assert mismatch is not None
    assert "ConnectionResetError" in mismatch


def test_an_ssl_error_with_an_unrelated_reason_fails_attempt_4():
    exc = _ssl_error("TLSV13_ALERT_CERTIFICATE_REQUIRED")
    mismatch = _attempt_4_exc_mismatch(exc)
    assert mismatch is not None
    assert "TLSV13_ALERT_CERTIFICATE_REQUIRED" in mismatch


# --- the safe response-summary helper: never the raw body --------------


_SYNTHETIC_TOKEN = "synthetic-test-token-should-never-be-printed"


def test_safe_body_summary_omits_the_token_value():
    body = {"ok": True, "session_id": "synthetic-session-id",
            "session_token": _SYNTHETIC_TOKEN}
    summary = _safe_body_summary(body)
    assert str(len(json.dumps(body))) in summary
    assert _SYNTHETIC_TOKEN not in summary


def test_safe_http_summary_includes_status_and_length_but_not_the_token_value():
    body = {"session_id": "synthetic-session-id", "session_token": _SYNTHETIC_TOKEN}
    summary = _safe_http_summary(200, body)
    assert "200" in summary
    assert str(len(json.dumps(body))) in summary
    assert _SYNTHETIC_TOKEN not in summary


# --- the sessions-database count helper: CQ·601 ---------------------------
#
# These three were each run by hand against origin/main's own `_session_count`
# (the version before this change, reproduced in this comment because it no
# longer exists in this file to import: `if not BRG.SESSIONS_DB.exists():
# return 0` ... `except sqlite3.OperationalError: return 0`). A missing
# database file and a missing `sessions` table both returned 0 there instead
# of raising, so the first two below fail against it; the third does not,
# since counting an existing table's rows was never the broken part — see
# this change's PR body for the per-assertion report.


def test_session_count_fails_on_a_missing_database_file(tmp_path):
    missing = tmp_path / "no-such-sessions.db"
    with pytest.raises(RuntimeError) as excinfo:
        _session_count(missing)
    assert str(missing) in str(excinfo.value)


def test_session_count_fails_on_a_database_with_no_sessions_table(tmp_path):
    db_path = tmp_path / "empty.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE unrelated (id TEXT)")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError) as excinfo:
        _session_count(db_path)
    assert str(db_path) in str(excinfo.value)


def test_session_count_counts_rows_in_a_synthetic_database(tmp_path):
    db_path = tmp_path / "sessions.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
    conn.executemany("INSERT INTO sessions (id) VALUES (?)",
                      [("session-a",), ("session-b",), ("session-c",)])
    conn.commit()
    conn.close()
    assert _session_count(db_path) == 3
