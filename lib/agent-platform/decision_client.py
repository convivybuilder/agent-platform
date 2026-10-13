"""Loopback decision-plane client for platform jobs (the operating box, ARCH-359).

A thin client the platform's own launchd/bin jobs use to raise a decision into
the box's decision queue (POST /api/decisions on the mini's loopback control
plane). It mirrors signal_client.py's post shape and reuses its bearer token —
the agent-platform token holds `decision.create` (the `create` capability) but
never `decide`, so a job can raise a decision for the operator and can never decide it.

Was used by `bin/issue-queue-drain` (`get_decision`) to read back a decision
an item was escalated to; that script was deleted 2026-10-09 when the nightly
drain ended, and no caller remains in this repo. This is the ARCH-359
headless-GitHub move: the record is the in-box decision item, not a GitHub
label or a Slack reaction.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import vault_plane
from signal_client import token

_TIMEOUT = 5  # loopback; decisions aren't latency-critical like the Slack tee


def post_decision(product: str, kind: str, title: str, *, body: str = "",
                  refs: dict | None = None, once_key: str | None = None,
                  blocked: bool = False, run_id: str | None = None,
                  tok: str | None = None) -> dict[str, Any]:
    """Raise a decision item into the box. Returns
    {"status": "created"|"duplicate"|"skip"|"error:<code>", "id": <id>|None}.
    Best-effort: never raises — a sync failure must not break the caller's real
    work (the same posture as signal_client.post_signal)."""
    tok = tok or token()
    if not tok:
        return {"status": "skip", "id": None}
    payload: dict[str, Any] = {
        "product": product, "kind": kind, "title": title,
        "body": body or "", "blocked": bool(blocked),
    }
    if refs:
        payload["refs"] = refs
    if once_key:
        payload["once_key"] = once_key
    if run_id:
        payload["run_id"] = run_id
    try:
        req = urllib.request.Request(
            vault_plane.url("/api/decisions"),
            data=json.dumps(payload).encode("utf-8"),
            headers=vault_plane.headers(token=tok, json_body=True),
            method="POST")
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            item: dict[str, Any] = {}
            try:
                item = json.loads(resp.read().decode("utf-8"))
            except Exception:  # a 2xx with an unparseable body — still a success
                pass
            status = "created" if resp.status == 201 else "duplicate"
            return {"status": status, "id": item.get("id")}
    except urllib.error.HTTPError as e:
        return {"status": f"error:{e.code}", "id": None}
    except Exception as e:  # loopback down, timeout, anything — swallow
        return {"status": f"error:{type(e).__name__}", "id": None}


def get_decision(decision_id: str, *, tok: str | None = None) -> dict[str, Any]:
    """Fetch a single decision by ID (GET /api/decisions/{id}).
    Returns {"status": "ok"|"skip"|"error:<code>", "item": <dict>|None}.
    Best-effort: never raises."""
    tok = tok or token()
    if not tok:
        return {"status": "skip", "item": None}
    enc = urllib.parse.quote(decision_id, safe="")
    try:
        req = urllib.request.Request(
            vault_plane.url(f"/api/decisions/{enc}"),
            headers=vault_plane.headers(token=tok), method="GET")
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return {"status": "ok", "item": data}
    except urllib.error.HTTPError as e:
        return {"status": f"error:{e.code}", "item": None}
    except Exception as e:
        return {"status": f"error:{type(e).__name__}", "item": None}


def list_decisions(product: str | None = None, state: str = "pending",
                   *, tok: str | None = None) -> dict[str, Any]:
    """Read the box's decision queue over loopback (GET /api/decisions). Returns
    {"status": "ok"|"skip"|"error:<code>", "items": [...]}. The agent-platform
    token holds `decision.read` (the read bundle), so a platform job can see the
    queue it raises into — the digest waiting on the operator, reading the plane instead of
    Slack reactions is the first consumer (ARCH-380). Best-effort, same posture as
    post_decision: a read failure yields an empty list so the caller degrades to
    its other signals rather than raising."""
    tok = tok or token()
    if not tok:
        return {"status": "skip", "items": []}
    url = vault_plane.url("/api/decisions")
    params = {k: v for k, v in (("product", product), ("state", state)) if v}
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(
            url, headers=vault_plane.headers(token=tok), method="GET")
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return {"status": "ok", "items": list(data.get("items", []))}
    except urllib.error.HTTPError as e:
        return {"status": f"error:{e.code}", "items": []}
    except Exception as e:  # loopback down, timeout, bad JSON — swallow
        return {"status": f"error:{type(e).__name__}", "items": []}
