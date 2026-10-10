"""Tests for decision_client — the loopback client platform jobs use to raise a
decision into the box's queue (ARCH-359). Mirrors test_signal_client's shape."""
from __future__ import annotations

import json

import decision_client as dc


class _Resp:
    def __init__(self, status, body):
        self.status = status
        self._body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def test_skip_without_token(monkeypatch):
    monkeypatch.setattr(dc, "token", lambda: None)
    assert dc.post_decision("vivy", "proposal", "x")["status"] == "skip"


def test_created_returns_id_and_posts_payload(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["auth"] = req.headers.get("Authorization")
        captured["body"] = json.loads(req.data)
        return _Resp(201, {"id": "dec_ABC", "state": "pending"})

    monkeypatch.setattr(dc.urllib.request, "urlopen", fake_urlopen)
    res = dc.post_decision("vivy", "needs-decision", "Ship it?",
                           body="context", refs={"number": 7},
                           once_key="gh-issue:convivy/vivy#7", tok="t")
    assert res == {"status": "created", "id": "dec_ABC"}
    assert captured["url"].endswith("/api/decisions")
    assert captured["auth"] == "Bearer t"
    assert captured["body"]["product"] == "vivy"
    assert captured["body"]["kind"] == "needs-decision"
    assert captured["body"]["once_key"] == "gh-issue:convivy/vivy#7"
    assert captured["body"]["refs"] == {"number": 7}


def test_duplicate_on_200(monkeypatch):
    monkeypatch.setattr(dc.urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp(200, {"id": "dec_X"}))
    res = dc.post_decision("vivy", "proposal", "x", tok="t")
    assert res == {"status": "duplicate", "id": "dec_X"}


def test_swallows_errors(monkeypatch):
    def boom(*_a, **_k):
        raise OSError("loopback down")

    monkeypatch.setattr(dc.urllib.request, "urlopen", boom)
    res = dc.post_decision("vivy", "proposal", "x", tok="t")
    assert res["status"].startswith("error:")
    assert res["id"] is None


def test_list_decisions_reads_queue_and_filters(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.method
        captured["auth"] = req.headers.get("Authorization")
        return _Resp(200, {"items": [{"id": "dec_1", "state": "pending"}]})

    monkeypatch.setattr(dc.urllib.request, "urlopen", fake_urlopen)
    res = dc.list_decisions(product="vivy", state="pending", tok="t")
    assert res["status"] == "ok" and res["items"][0]["id"] == "dec_1"
    assert captured["method"] == "GET" and captured["auth"] == "Bearer t"
    assert "product=vivy" in captured["url"] and "state=pending" in captured["url"]


def test_list_decisions_skip_without_token(monkeypatch):
    monkeypatch.setattr(dc, "token", lambda: None)
    assert dc.list_decisions()["status"] == "skip"


def test_list_decisions_swallows_errors(monkeypatch):
    def boom(*_a, **_k):
        raise OSError("loopback down")

    monkeypatch.setattr(dc.urllib.request, "urlopen", boom)
    res = dc.list_decisions(tok="t")
    assert res["status"].startswith("error:") and res["items"] == []
