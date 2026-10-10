"""notify — best-effort phone-push egress (ARCH-269).

Guards: the push never raises into a caller, degrades silently with no
credential, keeps `fields` to counts/dollars only, targets the configured
topic, the dry-run guard stops a test from firing a real push, and a failed
send never logs the push credential. `message` itself is caller-controlled
free text sent verbatim — `tcc_prompt_watch.push_alert` is a deliberate
exception to the counts-and-dollars convention (see test_tcc_prompt_watch.py),
covered there rather than here.
"""
from __future__ import annotations

import logging
import sys
import types

import notify


def _fake_requests(capture: dict, status: int = 200):
    mod = types.ModuleType("requests")

    class _Resp:
        status_code = status

    def _post(url, **kwargs):
        capture["url"] = url
        capture.update(kwargs)
        return _Resp()

    mod.post = _post
    return mod


def test_dry_run_returns_true_without_resolving_cred(monkeypatch):
    monkeypatch.setenv("AGENT_PLATFORM_PUSH_DRY_RUN", "1")

    def _boom():
        raise AssertionError("cred must not be resolved in dry-run")

    monkeypatch.setattr(notify, "_resolve_cred", _boom)
    assert notify.push("budget-hard-cap", "test", spent_usd=160.0) is True


def test_no_credential_degrades_to_false(monkeypatch):
    monkeypatch.delenv("AGENT_PLATFORM_PUSH_DRY_RUN", raising=False)
    monkeypatch.setattr(notify, "_resolve_cred", lambda: None)
    assert notify.push("budget-hard-cap", "test") is False


def test_push_never_raises_on_post_failure(monkeypatch):
    monkeypatch.delenv("AGENT_PLATFORM_PUSH_DRY_RUN", raising=False)
    monkeypatch.setattr(notify, "_resolve_cred", lambda: "ntfy:platform-xyz")
    mod = types.ModuleType("requests")

    def _boom(*a, **k):
        raise RuntimeError("network down")

    mod.post = _boom
    monkeypatch.setitem(sys.modules, "requests", mod)
    assert notify.push("budget-hard-cap", "test") is False  # swallowed, no raise


def test_push_failure_does_not_log_the_credential(monkeypatch, caplog):
    """A ConnectionError from requests/urllib3 embeds the request URL — for
    ntfy that URL is https://ntfy.sh/<topic>, and the topic is the push
    credential. The failure log must not reproduce it."""
    monkeypatch.delenv("AGENT_PLATFORM_PUSH_DRY_RUN", raising=False)
    monkeypatch.setattr(notify, "_resolve_cred", lambda: "ntfy:secret-topic-xyz")
    mod = types.ModuleType("requests")

    class ConnectionError(Exception):  # noqa: A001 — mirrors requests.ConnectionError
        pass

    def _boom(*a, **k):
        raise ConnectionError(
            "HTTPSConnectionPool(host='ntfy.sh', port=443): Max retries exceeded "
            "with url: /secret-topic-xyz (Caused by NewConnectionError(...))"
        )

    mod.post = _boom
    monkeypatch.setitem(sys.modules, "requests", mod)
    with caplog.at_level(logging.WARNING):
        assert notify.push("tcc-prompt", "Documents access requested by tmux") is False
    assert "secret-topic-xyz" not in caplog.text


def test_payload_carries_counts_and_dollars_only():
    payload = notify._build_payload(
        "budget-hard-cap", "msg",
        {"spent_usd": 160.0, "cap_usd": 160.0, "subject": None},
    )
    assert payload == {
        "kind": "budget-hard-cap", "source": "agent-platform",
        "message": "msg", "spent_usd": 160.0, "cap_usd": 160.0,
    }
    forbidden = {"body", "text", "title", "sender", "email", "content", "subject"}
    assert not (set(payload) & forbidden)


def test_ntfy_post_sends_plain_text_body_and_headers(monkeypatch):
    """ntfy shows the body of a POST to a topic URL verbatim — a JSON `payload`
    there reads as raw JSON on the phone, not text. `message` must go over the
    wire as the plain-text body, with the rest carried in headers."""
    monkeypatch.delenv("AGENT_PLATFORM_PUSH_DRY_RUN", raising=False)
    monkeypatch.setattr(notify, "_resolve_cred", lambda: "ntfy:platform-abc123")
    capture: dict = {}
    monkeypatch.setitem(sys.modules, "requests", _fake_requests(capture))
    assert notify.push("tcc-prompt", "Documents access requested by tmux") is True
    assert capture["url"] == "https://ntfy.sh/platform-abc123"
    assert capture["data"] == b"Documents access requested by tmux"
    assert capture["headers"]["Title"] == "tcc-prompt"
    assert "json" not in capture


def test_ntfy_non_2xx_response_returns_false(monkeypatch):
    monkeypatch.delenv("AGENT_PLATFORM_PUSH_DRY_RUN", raising=False)
    monkeypatch.setattr(notify, "_resolve_cred", lambda: "ntfy:platform-abc123")
    monkeypatch.setitem(sys.modules, "requests", _fake_requests({}, status=429))
    assert notify.push("budget-hard-cap", "msg") is False


def test_pushover_post_still_works(monkeypatch):
    monkeypatch.delenv("AGENT_PLATFORM_PUSH_DRY_RUN", raising=False)
    monkeypatch.setattr(notify, "_resolve_cred", lambda: "pushover:app-tok/user-tok")
    capture: dict = {}
    monkeypatch.setitem(sys.modules, "requests", _fake_requests(capture))
    assert notify.push("budget-hard-cap", "spend crossed $160") is True
    assert capture["url"] == "https://api.pushover.net/1/messages.json"
    assert capture["json"] == {
        "token": "app-tok", "user": "user-tok", "message": "spend crossed $160",
    }


def test_pushover_non_2xx_response_returns_false(monkeypatch):
    monkeypatch.delenv("AGENT_PLATFORM_PUSH_DRY_RUN", raising=False)
    monkeypatch.setattr(notify, "_resolve_cred", lambda: "pushover:app-tok/user-tok")
    monkeypatch.setitem(sys.modules, "requests", _fake_requests({}, status=500))
    assert notify.push("budget-hard-cap", "msg") is False


def test_unknown_cred_format_returns_false(monkeypatch):
    monkeypatch.delenv("AGENT_PLATFORM_PUSH_DRY_RUN", raising=False)
    monkeypatch.setattr(notify, "_resolve_cred", lambda: "slack:whatever")
    monkeypatch.setitem(sys.modules, "requests", _fake_requests({}))
    assert notify.push("budget-hard-cap", "msg") is False
