"""Tests for issue_client — the loopback client that reads and changes items in the box's
queue (ARCH-359). It creates no item: a monitor logs a signal instead (platform/decisions/0073).
Mirrors test_decision_client's shape."""
from __future__ import annotations

import io
import json
import pathlib
import sys

import pytest

import issue_client as bc


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


def test_list_open_issues_builds_url_and_returns_items(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        return _Resp(200, {"items": [{"id": "bug_1", "state": "open"}]})

    monkeypatch.setattr(bc.urllib.request, "urlopen", fake_urlopen)
    items = bc.list_open_issues("vivy", tok="t")
    assert items == [{"id": "bug_1", "state": "open"}]
    assert "state=open" in captured["url"] and "product=vivy" in captured["url"]


def test_list_open_issues_empty_on_error(monkeypatch):
    monkeypatch.setattr(bc.urllib.request, "urlopen",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("down")))
    assert bc.list_open_issues(tok="t") == []


def test_list_issues_by_state_follows_pagination(monkeypatch):
    """The server caps a page at 200 and reports `truncated`. A reconciler reading only
    the first page would leave the tail permanently unreconciled, growing as the queue
    does — the exact drift it exists to stop."""
    pages = [
        {"items": [{"id": "i1"}, {"id": "i2"}], "truncated": True},
        {"items": [{"id": "i3"}], "truncated": False},
    ]
    seen = []

    def fake_urlopen(req, timeout=None):
        seen.append(req.full_url)
        return _Resp(200, pages[len(seen) - 1])

    monkeypatch.setattr(bc.urllib.request, "urlopen", fake_urlopen)
    items = bc.list_issues_by_state("pr-open", tok="t")
    assert [i["id"] for i in items] == ["i1", "i2", "i3"]
    assert "offset=0" in seen[0] and "offset=2" in seen[1]
    assert "state=pr-open" in seen[0]


def test_list_issues_by_state_stops_on_an_empty_truncated_page(monkeypatch):
    """A server that reports `truncated` while returning no items would otherwise spin
    forever, hammering the API from an unattended job."""
    monkeypatch.setattr(bc.urllib.request, "urlopen",
                        lambda *_a, **_k: _Resp(200, {"items": [], "truncated": True}))
    assert bc.list_issues_by_state("pr-open", tok="t") == []


def test_list_issues_by_state_refuses_to_page_forever(monkeypatch):
    """Runs unattended from launchd, where an unbounded loop against a misbehaving
    server hammers an API all night with nobody watching. The ceiling turns that into
    one loud failure."""
    monkeypatch.setattr(bc, "_MAX_PAGES", 3)
    monkeypatch.setattr(bc.urllib.request, "urlopen",
                        lambda *_a, **_k: _Resp(200, {"items": [{"id": "x"}],
                                                      "truncated": True}))
    with pytest.raises(bc.IssueQueueUnavailable, match="refusing to keep paging"):
        bc.list_issues_by_state("pr-open", tok="t")


def test_list_issues_by_state_raises_rather_than_reading_as_empty(monkeypatch):
    """Returning [] here would make an outage indistinguishable from a reconciled
    queue, and the sweep would report a clean pass while blind (ARCH-311)."""
    monkeypatch.setattr(bc.urllib.request, "urlopen",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("down")))
    with pytest.raises(bc.IssueQueueUnavailable):
        bc.list_issues_by_state("pr-open", tok="t")


def test_list_issues_by_state_raises_without_a_token(monkeypatch):
    monkeypatch.setattr(bc, "token", lambda: None)
    with pytest.raises(bc.IssueQueueUnavailable):
        bc.list_issues_by_state("pr-open")


def test_get_item_builds_url_and_returns_the_body(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        return _Resp(200, {"id": "issue_A", "type": "epic", "progress": {"children": 2}})

    monkeypatch.setattr(bc.urllib.request, "urlopen", fake_urlopen)
    item = bc.get_item("issue_A", tok="t")
    assert item == {"id": "issue_A", "type": "epic", "progress": {"children": 2}}
    assert captured["url"].endswith("/items/issue_A")


def test_get_item_raises_on_a_404(monkeypatch):
    def not_found(req, timeout=None):
        raise bc.urllib.error.HTTPError(req.full_url, 404, "not found", {}, None)

    monkeypatch.setattr(bc.urllib.request, "urlopen", not_found)
    with pytest.raises(bc.IssueQueueUnavailable):
        bc.get_item("issue_missing", tok="t")


def test_get_item_raises_without_a_token(monkeypatch):
    monkeypatch.setattr(bc, "token", lambda: None)
    with pytest.raises(bc.IssueQueueUnavailable):
        bc.get_item("issue_A")


def test_get_course_builds_url_and_returns_the_body(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        return _Resp(200, {"id": "course_1", "entries": ["issue_A"], "position": {}})

    monkeypatch.setattr(bc.urllib.request, "urlopen", fake_urlopen)
    course = bc.get_course("course_1", tok="t")
    assert course == {"id": "course_1", "entries": ["issue_A"], "position": {}}
    assert captured["url"].endswith("/courses/course_1")


def test_get_course_raises_on_an_unreachable_library(monkeypatch):
    monkeypatch.setattr(bc.urllib.request, "urlopen",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("down")))
    with pytest.raises(bc.IssueQueueUnavailable):
        bc.get_course("course_1", tok="t")


def test_list_courses_builds_url_and_returns_the_list(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        return _Resp(200, {"courses": [{"id": "course_1"}], "truncated": False})

    monkeypatch.setattr(bc.urllib.request, "urlopen", fake_urlopen)
    courses = bc.list_courses("bosun", tok="t")
    assert courses == [{"id": "course_1"}]
    assert "product=bosun" in captured["url"]


def test_list_courses_follows_pagination(monkeypatch):
    pages = [
        {"courses": [{"id": "c1"}], "truncated": True},
        {"courses": [{"id": "c2"}], "truncated": False},
    ]
    seen = []

    def fake_urlopen(req, timeout=None):
        seen.append(req.full_url)
        return _Resp(200, pages[len(seen) - 1])

    monkeypatch.setattr(bc.urllib.request, "urlopen", fake_urlopen)
    courses = bc.list_courses("bosun", tok="t")
    assert [c["id"] for c in courses] == ["c1", "c2"]
    assert "offset=0" in seen[0] and "offset=1" in seen[1]


def test_list_courses_raises_rather_than_reading_as_empty(monkeypatch):
    monkeypatch.setattr(bc.urllib.request, "urlopen",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("down")))
    with pytest.raises(bc.IssueQueueUnavailable):
        bc.list_courses("bosun", tok="t")


def test_list_courses_raises_without_a_token(monkeypatch):
    monkeypatch.setattr(bc, "token", lambda: None)
    with pytest.raises(bc.IssueQueueUnavailable):
        bc.list_courses("bosun")


def test_claim_issue_posts_and_returns_item(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        return _Resp(200, {"id": "bug_1", "state": "claimed"})

    monkeypatch.setattr(bc.urllib.request, "urlopen", fake_urlopen)
    res = bc.claim_issue("bug_1", tok="t")
    assert res["status"] == "ok" and res["item"]["state"] == "claimed"
    assert captured["url"].endswith("/api/issues/bug_1/claim")


def test_claim_issue_409_is_error_status(monkeypatch):
    def raise_409(req, timeout=None):
        raise bc.urllib.error.HTTPError(req.full_url, 409, "Conflict", {}, None)

    monkeypatch.setattr(bc.urllib.request, "urlopen", raise_409)
    assert bc.claim_issue("bug_1", tok="t")["status"] == "error:409"


def test_link_pr_posts_pr_and_url(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        return _Resp(200, {"id": "bug_1", "state": "pr-open"})

    monkeypatch.setattr(bc.urllib.request, "urlopen", fake_urlopen)
    res = bc.link_pr_to_issue("bug_1", "42", url="https://github.com/convivy/vivy/pull/42", tok="t")
    assert res["status"] == "ok" and res["item"]["state"] == "pr-open"
    assert captured["url"].endswith("/api/issues/bug_1/link-pr")
    assert captured["body"] == {"pr": "42", "url": "https://github.com/convivy/vivy/pull/42"}


def test_escalate_issue_posts_decision_id(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        return _Resp(200, {"id": "bug_1", "state": "awaiting-decision"})

    monkeypatch.setattr(bc.urllib.request, "urlopen", fake_urlopen)
    res = bc.escalate_issue("bug_1", "dec_ABC", tok="t")
    assert res["item"]["state"] == "awaiting-decision"
    assert captured["url"].endswith("/api/issues/bug_1/escalate")
    assert captured["body"] == {"decision_id": "dec_ABC"}


def test_resolve_issue_posts_resolution_and_fix_pr(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data)
        return _Resp(200, {"id": "bug_1", "state": "fixed"})

    monkeypatch.setattr(bc.urllib.request, "urlopen", fake_urlopen)
    res = bc.resolve_issue("bug_1", "fixed", outcome_note="shipped", fix_pr="PR#42", tok="t")
    assert res["item"]["state"] == "fixed"
    assert captured["body"]["resolution"] == "fixed"
    assert captured["body"]["outcome_note"] == "shipped"
    assert captured["body"]["refs"] == {"fix_pr": "PR#42"}


def test_reconcile_pr_posts_the_outcome(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        return _Resp(200, {"id": "issue_1", "state": "open"})

    monkeypatch.setattr(bc.urllib.request, "urlopen", fake_urlopen)
    res = bc.reconcile_pr("issue_1", "merged", tok="t")
    assert res["status"] == "ok" and res["item"]["state"] == "open"
    assert captured["url"].endswith("/api/issues/issue_1/reconcile-pr")
    assert captured["body"] == {"outcome": "merged"}


@pytest.mark.parametrize("outcome", ["merged", "closed", "abandoned"])
def test_reconcile_pr_carries_each_outcome_unchanged(monkeypatch, outcome):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data)
        return _Resp(200, {"id": "issue_1"})

    monkeypatch.setattr(bc.urllib.request, "urlopen", fake_urlopen)
    bc.reconcile_pr("issue_1", outcome, tok="t")
    assert captured["body"] == {"outcome": outcome}


def test_reconcile_pr_409_is_error_status(monkeypatch):
    """A non-2xx must be reported to the caller, never swallowed — the sweep
    that calls this needs to know a write did not land."""
    def raise_409(req, timeout=None):
        raise bc.urllib.error.HTTPError(req.full_url, 409, "Conflict", {}, None)

    monkeypatch.setattr(bc.urllib.request, "urlopen", raise_409)
    assert bc.reconcile_pr("issue_1", "merged", tok="t")["status"] == "error:409"


def test_reconcile_pr_skips_without_a_token(monkeypatch):
    def explode(req, timeout=None):
        raise AssertionError("no request should be made without a token")

    monkeypatch.setattr(bc.urllib.request, "urlopen", explode)
    monkeypatch.setattr(bc, "token", lambda: None)
    assert bc.reconcile_pr("issue_1", "merged")["status"] == "skip"


def test_drain_transitions_skip_without_token(monkeypatch):
    monkeypatch.setattr(bc, "token", lambda: None)
    for call in (lambda: bc.claim_issue("b"), lambda: bc.link_pr_to_issue("b", "1"),
                 lambda: bc.escalate_issue("b", "d"), lambda: bc.resolve_issue("b", "fixed"),
                 lambda: bc.reconcile_pr("b", "merged")):
        assert call()["status"] == "skip"
    assert bc.list_open_issues() == []


# --- the deprecated-alias contract --------------------------------------------


def test_deprecated_aliases_delegate_by_keyword_not_position():
    """A positional hand-off silently re-aims when the target gains a parameter.

    That is not hypothetical: claim_bug passed `tok` positionally, and when
    claim_issue later gained an `owner` second parameter, the token began
    arriving as the owner instead. Neither PR could see it — one wrote the alias,
    the other changed the signature — so it took an integration review to catch.

    This pins the whole class: NO function here may pass `tok` positionally, so
    the next signature change cannot quietly re-point it.

    Scoped by behaviour, not by name. An earlier cut of this test only inspected
    functions ending in `_bug`, and `link_pr` — a deprecated alias that does not
    carry that suffix — sailed straight through it while carrying the identical
    defect. A guard whose reach is narrower than the rule it enforces gives false
    assurance, which is worse than no guard.

    The line drawn here is PUBLIC callee, not "any positional tok". Passing tok
    positionally to a private helper like `_post` is fine: its signature sits a
    few lines away and changes in the same edit. The fragile case is delegating
    positionally to a PUBLIC sibling, whose signature is a contract someone
    changes in a different PR — which is precisely how claim_bug broke.
    """
    import ast
    import pathlib
    src = pathlib.Path(bc.__file__).read_text()
    tree = ast.parse(src)
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for call in (n for n in ast.walk(node) if isinstance(n, ast.Call)):
            # A bare name (`post_issue(...)`) or an attribute call
            # (`vault_plane.headers(...)`). The attribute form used to resolve to
            # "" and be reported as `()`, which named no callee a reader could
            # act on — and a call into ANOTHER module is the strongest case the
            # rule covers, since its signature is a contract that changes in a
            # different repo's PR, let alone a different one here.
            callee = getattr(call.func, "id", None) or getattr(call.func, "attr", "")
            if callee.startswith("_"):
                continue   # private helper: signature changes in the same edit
            names = {a.id for a in call.args if isinstance(a, ast.Name)}
            if "tok" in names:
                offenders.append(f"{node.name}() passes tok positionally to {callee}()")
    assert not offenders, offenders


def test_every_deprecated_alias_routes_its_token_to_the_token_parameter(monkeypatch):
    """The behavioural half of the check above: an explicitly-passed token must
    reach the request as a token, not land in some other parameter."""
    seen = {}

    def fake_post(path, payload, tok):
        seen["path"], seen["payload"], seen["tok"] = path, payload, tok
        return {"status": "ok", "item": {}}

    monkeypatch.setattr(bc, "_post", fake_post)
    monkeypatch.setattr(bc, "session_owner", lambda: None)

    bc.claim_bug("issue_01A", tok="SENTINEL")
    assert seen["tok"] == "SENTINEL", seen
    # and the sentinel must not have leaked into the claim owner
    assert seen["payload"].get("owner") != "SENTINEL", seen["payload"]


# --- the issue thread (ARCH-373) ----------------------------------------------


def test_comment_on_issue_posts_body_and_kind(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        return _Resp(201, {"id": "issue_1", "comments": [{"kind": "diagnosis"}]})

    monkeypatch.setattr(bc.urllib.request, "urlopen", fake_urlopen)
    res = bc.comment_on_issue("issue_1", "root cause: stale fixture",
                              kind="diagnosis", run_id="run-9", tok="t")
    assert res["status"] == "ok"
    assert captured["url"].endswith("/api/issues/issue_1/comments")
    assert captured["body"] == {"body": "root cause: stale fixture",
                                "kind": "diagnosis", "run_id": "run-9"}


def test_comment_defaults_to_note(monkeypatch):
    captured = {}
    monkeypatch.setattr(bc.urllib.request, "urlopen",
                        lambda req, timeout=None: captured.update(
                            {"body": json.loads(req.data)}) or _Resp(201, {}))
    bc.comment_on_issue("issue_1", "just a note", tok="t")
    assert captured["body"]["kind"] == "note"
    assert "run_id" not in captured["body"], "an absent run_id must not be sent as null"


def test_comment_without_a_token_skips(monkeypatch):
    """Best-effort like the rest of the module: no token means no call, not a
    raise — a sync failure must not break the caller's real work."""
    monkeypatch.setattr(bc, "token", lambda: None)
    monkeypatch.setattr(bc.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("must not call the API without a token")))
    assert bc.comment_on_issue("issue_1", "x")["status"] == "skip"


# --- the client/server constant guards ---------------------------------------
#
# Each of these reads the SERVER's own constant rather than a literal. A test
# asserting a hard-coded set against itself passes unchanged through exactly the
# schema change it exists to catch — which is not hypothetical: the severities
# guard below was written after `ITEM_SEVERITIES` tightened under a client that
# defaulted to "unset", and `post_bug` filed nothing for twenty days.
#
# THE SERVER MOVED, AND THE KINDS GUARD SAID SO. Until 2026-08-25 it read
# `ISSUE_COMMENT_KINDS` from convivy-orient's `control_db.py`; that constant is
# now `ITEM_COMMENT_KINDS` in the Library's `store.py`. The guard went red with
# "could not find ..." — correct behaviour and a clear message — and the red was
# left standing. The defect was never in the check; it was that a failing test
# nobody chases is worth no more than one that passes vacuously.
#
# So they share the extraction: when the next constant moves, the fix is one path.


def _server_frozenset(path: pathlib.Path, name: str, *, absent_skip: str) -> set[str]:
    """The named `frozenset({...})` of string literals, read out of the server.

    Skips when the checkout is absent (CI has none). Fails LOUDLY and specifically
    when the file is there but the constant is not — that is the server having
    moved or renamed it, which is a different thing from client/server drift and
    wants a different fix, so the two must not report the same way.

    A member may be a bare identifier rather than a literal — `ITEM_TYPES` names
    two of its five members via `ITEM_TYPE_TASK`/`ITEM_TYPE_EPIC` rather than
    `"task"`/`"epic"` directly. Each such identifier is resolved against its own
    top-level `NAME = "literal"` assignment elsewhere in the same file.
    """
    import re
    if not path.exists():
        pytest.skip(absent_skip)
    text = path.read_text()
    m = re.search(rf"{name}\s*=\s*frozenset\(\{{([^}}]*)\}}\)", text)
    assert m, (
        f"could not find {name} in {path} — the constant has moved or been renamed. "
        f"This is NOT client/server drift: find where the server defines it now and "
        f"repoint this guard, rather than editing the client to match a set you "
        f"cannot read.")
    body = m.group(1)
    values = set(re.findall(r'"([^"]+)"', body))
    for ident in re.findall(r"\b([A-Z][A-Z0-9_]*)\b", body):
        ref = re.search(rf'^{ident}\s*=\s*"([^"]+)"', text, re.MULTILINE)
        if ref:
            values.add(ref.group(1))
    return values


LIBRARY_STORE = pathlib.Path.home() / "Convivy" / "library" / "store.py"


def test_the_client_and_server_agree_on_the_kinds():
    """A kind the server rejects is a 400 the agent discovers mid-run."""
    server = _server_frozenset(
        LIBRARY_STORE, "ITEM_COMMENT_KINDS",
        absent_skip="library not checked out; cannot read the server's kinds")
    assert set(bc.ISSUE_COMMENT_KINDS) == server, (
        f"client {sorted(bc.ISSUE_COMMENT_KINDS)} != server {sorted(server)}")


def test_the_client_and_server_agree_on_the_severities():
    """The drift this test exists to catch already happened, and cost twenty days.

    `post_issue` defaulted `severity` to "unset" while the Library refuses anything
    outside `ITEM_SEVERITIES` with a 400. Every call that omitted a severity filed
    nothing. It stayed invisible because the only caller that omitted one was
    `bin/post_bug` — the one agents use — so the scheduled checkers, which all pass
    a severity, kept the endpoint looking healthy.

    Reads the SERVER's constant rather than a literal, for the same reason the kinds
    guard beside it does: a test asserting a hard-coded set against itself would
    have passed unchanged through the tightening that broke this.
    """
    server = _server_frozenset(
        LIBRARY_STORE, "ITEM_SEVERITIES",
        absent_skip="library not checked out; cannot read the server's severities")
    assert set(bc.VALID_SEVERITIES) == server, (
        f"client {sorted(bc.VALID_SEVERITIES)} != server {sorted(server)}")


def test_the_client_and_server_agree_on_the_resolutions():
    """RESOLVED_STATES names the states that end an item. A resolution the
    server has and the client lacks answers `duplicate` of a closed item, so a recurrence files
    nothing: the redirected case that muted its own outage (issue_01M457WD9HD9WDB92CYTJ7TP97).
    """
    server = _server_frozenset(
        LIBRARY_STORE, "ITEM_RESOLUTIONS",
        absent_skip="library not checked out; cannot read the server's resolutions")
    assert set(bc.RESOLVED_STATES) == server, (
        f"client {sorted(bc.RESOLVED_STATES)} != server {sorted(server)}")


def test_the_ops_server_advertises_the_types_the_library_accepts(mcp_ops_server):
    """`bin/mcp-ops-server`'s `issue_create`/`issue_list` docstrings are the only
    place an agent learns what `type` accepts before filing or filtering — a type
    the docstring names but the Library rejects is a 400 the agent discovers
    mid-run, the same failure mode the kinds/severities guards above exist to
    catch. Reads the SERVER's constant rather than a literal, for the same
    reason those two do.
    """
    import re
    server = _server_frozenset(
        LIBRARY_STORE, "ITEM_TYPES",
        absent_skip="library not checked out; cannot read the server's types")
    for fn in (mcp_ops_server.issue_create, mcp_ops_server.issue_list):
        m = re.search(r"\b([a-z]+\|[a-z]+(?:\|[a-z]+)+)\b", fn.__doc__)
        assert m, f"{fn.__name__}'s docstring names no pipe-separated type list"
        advertised = set(m.group(1).split("|"))
        assert advertised == server, (
            f"{fn.__name__} advertises {sorted(advertised)} but the Library "
            f"accepts {sorted(server)}")


def test_checkout_posts_owner_and_run_id_and_returns_the_lease(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        return _Resp(200, {"issue": {"id": "issue_a", "state": "claimed"},
                           "lease": {"id": "lease_1", "holder": "co-a",
                                     "expires_at": "2026-08-05T14:00:00Z"},
                           "policy": {"grace_seconds": 14400}})

    monkeypatch.setattr(bc.urllib.request, "urlopen", fake_urlopen)
    res = bc.checkout_issue("issue_a", owner="co-a", run_id="run-7f3a91", tok="t")
    assert captured["url"].endswith("/api/issues/issue_a/checkout")
    assert captured["body"] == {"owner": "co-a", "run_id": "run-7f3a91"}
    assert res["status"] == "ok"
    assert res["item"]["lease"]["expires_at"] == "2026-08-05T14:00:00Z"


def test_checkout_omits_an_absent_run_id(monkeypatch):
    """An interactive co has no run id, and the API rejects a blank one."""
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data)
        return _Resp(200, {"issue": {}, "lease": {}, "policy": {}})

    monkeypatch.setattr(bc.urllib.request, "urlopen", fake_urlopen)
    bc.checkout_issue("issue_a", owner="co-a", tok="t")
    assert "run_id" not in captured["body"]


def test_checkout_409_is_an_error_status_a_drain_can_skip_on(monkeypatch):
    """The subtree is held. A drain must move to the next item rather than
    retrying — which is only possible if the refusal is legible as a status."""
    def raise_409(req, timeout=None):
        raise bc.urllib.error.HTTPError(req.full_url, 409, "Conflict", {}, None)

    monkeypatch.setattr(bc.urllib.request, "urlopen", raise_409)
    assert bc.checkout_issue("issue_a", tok="t")["status"] == "error:409"


def test_heartbeat_posts_the_owner(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        return _Resp(200, {"lease": {"id": "lease_1"}, "policy": {}})

    monkeypatch.setattr(bc.urllib.request, "urlopen", fake_urlopen)
    assert bc.heartbeat_issue("issue_a", owner="co-a", tok="t")["status"] == "ok"
    assert captured["url"].endswith("/api/issues/issue_a/heartbeat")
    assert captured["body"] == {"owner": "co-a"}


def test_checkout_and_heartbeat_default_the_owner_to_the_session_identity(monkeypatch):
    """The same defaulting `claim_issue` does. A caller that names no owner would
    otherwise take a lease attributable only to the shared ops token, which is
    the attribution gap `session_owner` exists to close."""
    monkeypatch.setenv("CONVIVY_SESSION_DIR",
                       "/srv/convivy/agent-runs/2026-08-05/co-vivy-0930")
    bodies = []

    def fake_urlopen(req, timeout=None):
        bodies.append(json.loads(req.data))
        return _Resp(200, {"issue": {}, "lease": {}, "policy": {}})

    monkeypatch.setattr(bc.urllib.request, "urlopen", fake_urlopen)
    bc.checkout_issue("issue_a", tok="t")
    bc.heartbeat_issue("issue_a", tok="t")
    assert all(b["owner"] == "2026-08-05/co-vivy-0930" for b in bodies), bodies


def test_checkout_without_a_token_skips_rather_than_calling(monkeypatch):
    """Same contract as every other write here: no token means skip, not an
    exception and not an unauthenticated request."""
    def explode(req, timeout=None):
        raise AssertionError("no request should be made without a token")

    monkeypatch.setattr(bc.urllib.request, "urlopen", explode)
    monkeypatch.setattr(bc, "token", lambda: None)
    assert bc.checkout_issue("issue_a")["status"] == "skip"
    assert bc.heartbeat_issue("issue_a")["status"] == "skip"


# --- get_item_from_default_library: the read whose result exempts a dispatch ---
# from the origin refusal, so the caller's environment must not steer it.

class _Listener:
    """A real local HTTP server on an ephemeral port that records what reaches it."""

    def __init__(self, reply=None):
        import http.server
        import threading
        self.requests = []
        outer = self
        reply = reply or (200, {}, b'{"id": "issue_X"}')

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests.append({"path": self.path, "headers": dict(self.headers)})
                code, hdrs, body = reply
                self.send_response(code)
                for k, v in hdrs.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def listeners():
    made = []

    def make(reply=None):
        made.append(_Listener(reply))
        return made[-1]
    yield make
    for x in made:
        x.close()


# The probe runs the REAL read in a fresh process whose environment is set before
# any import (vault_plane reads LIBRARY_BASE_URL at import). Only the fixed
# address and the token file are pointed at the test listener.
_FIXED_READ_PROBE = """
import sys, json, pathlib
sys.path.insert(0, {lib!r})
import vault_plane, signal_client
import issue_client as ic
vault_plane.DEFAULT_LIBRARY_URL = {target!r}
signal_client.TOKEN_FILE = pathlib.Path({tokfile!r})
print(json.dumps(ic.get_item_from_default_library("issue_X", timeout=5)))
"""


def _fixed_read(env_extra, target, tmp_path):
    import os
    import subprocess
    lib = str(pathlib.Path(__file__).resolve().parents[1] / "lib" / "agent-platform")
    tokfile = tmp_path / "token"
    tokfile.write_text("tok-from-file\n")
    drop = ("LIBRARY_BASE_URL", "VAULTS_ON_LIBRARY", "ORIENT_BASE_URL", "AGENT_SIGNAL_TOKEN",
            "no_proxy", "NO_PROXY")
    env = {k: v for k, v in os.environ.items()
           if k not in drop and "proxy" not in k.lower()}
    env.update(env_extra)
    out = subprocess.run(
        [sys.executable, "-c",
         _FIXED_READ_PROBE.format(lib=lib, target=target, tokfile=str(tokfile))],
        env=env, capture_output=True, text=True, check=True, timeout=60).stdout
    return json.loads(out)


@pytest.mark.parametrize("var", ["http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"])
def test_the_item_read_ignores_every_proxy_variable(var, listeners, tmp_path):
    target, attacker = listeners(), listeners()
    got = _fixed_read({var: attacker.url}, target.url, tmp_path)
    assert got == {"status": "ok", "item": {"id": "issue_X"}}
    assert [r["path"] for r in target.requests] == ["/items/issue_X"]
    assert target.requests[0]["headers"].get("Library-Protocol") == "1"
    assert attacker.requests == [], "the request, and the bearer token, went to the proxy"


@pytest.mark.parametrize("env_extra_fn", [
    lambda a: {"LIBRARY_BASE_URL": a},
    lambda a: {"VAULTS_ON_LIBRARY": "0", "ORIENT_BASE_URL": a},
])
def test_the_item_read_ignores_the_callers_base_urls(env_extra_fn, listeners, tmp_path):
    target, attacker = listeners(), listeners()
    got = _fixed_read(env_extra_fn(attacker.url), target.url, tmp_path)
    assert got["status"] == "ok"
    assert len(target.requests) == 1
    assert attacker.requests == []


def test_the_item_read_uses_the_token_file_not_the_environment(listeners, tmp_path):
    target = listeners()
    got = _fixed_read({"AGENT_SIGNAL_TOKEN": "tok-from-env"}, target.url, tmp_path)
    assert got["status"] == "ok"
    assert target.requests[0]["headers"].get("Authorization") == "Bearer tok-from-file"


def test_the_item_read_refuses_a_redirect(listeners, tmp_path):
    elsewhere = listeners()
    target = listeners((302, {"Location": elsewhere.url + "/items/issue_X"}, b""))
    got = _fixed_read({}, target.url, tmp_path)
    assert got == {"status": "error:302", "item": {}}
    assert len(target.requests) == 1
    assert elsewhere.requests == []


# --- post_ledger_entry: the Bosun's first ledger writer -----------------------
# (issue_01M0TP37M3RRRY5BD1Z4B8GCB0 Part 1, POST /ledger-entries)


def test_post_ledger_entry_sends_the_documented_fields_and_dedup_key(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["auth"] = req.headers.get("Authorization")
        captured["protocol"] = req.headers.get("Library-protocol")
        captured["body"] = json.loads(req.data)
        return _Resp(200, {"recorded": True, "id": "entry_1", "idempotent": False})

    monkeypatch.setattr(bc.urllib.request, "urlopen", fake_urlopen)
    res = bc.post_ledger_entry("vivy", 0.1234, run_id="run-abc", dedup_key="run-abc",
                              agent="coder", model="claude-sonnet-5", tok="t")
    assert res["status"] == "recorded"
    assert res["entry"]["id"] == "entry_1"
    assert captured["url"].endswith("/ledger-entries")
    assert captured["auth"] == "Bearer t"
    assert captured["protocol"] == "1"
    assert captured["body"] == {
        "product": "vivy", "source": "agentctl", "usd": 0.1234,
        "run_id": "run-abc", "dedup_key": "run-abc",
        "agent": "coder", "model": "claude-sonnet-5",
    }


def test_post_ledger_entry_omits_agent_and_model_when_absent(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data)
        return _Resp(200, {"recorded": True, "id": "entry_2"})

    monkeypatch.setattr(bc.urllib.request, "urlopen", fake_urlopen)
    bc.post_ledger_entry("vivy", 0.5, run_id="run-abc", dedup_key="run-abc", tok="t")
    assert "agent" not in captured["body"]
    assert "model" not in captured["body"]


def test_post_ledger_entry_a_rerun_sends_the_same_dedup_key(monkeypatch):
    seen = []

    def fake_urlopen(req, timeout=None):
        seen.append(json.loads(req.data)["dedup_key"])
        return _Resp(200, {"recorded": True, "id": "entry_1", "idempotent": len(seen) > 1})

    monkeypatch.setattr(bc.urllib.request, "urlopen", fake_urlopen)
    bc.post_ledger_entry("vivy", 0.1, run_id="run-abc", dedup_key="run-abc", tok="t")
    bc.post_ledger_entry("vivy", 0.1, run_id="run-abc", dedup_key="run-abc", tok="t")
    assert seen == ["run-abc", "run-abc"]


def test_post_ledger_entry_recorded_false_is_not_recorded_not_an_error(monkeypatch):
    monkeypatch.setattr(
        bc.urllib.request, "urlopen",
        lambda *_a, **_k: _Resp(200, {"recorded": False,
                                      "reason": "no live ledger for this scope"}))
    res = bc.post_ledger_entry("vivy", 0.1, run_id="run-abc", dedup_key="run-abc", tok="t")
    assert res["status"] == "not-recorded"


def test_post_ledger_entry_a_disproportionate_refusal_is_reported_not_retried(monkeypatch):
    def raise_422(req, timeout=None):
        body = json.dumps({"error": "this report exceeds 10x the scope's soft cap",
                           "basis": "soft_cap"}).encode()
        raise bc.urllib.error.HTTPError(req.full_url, 422, "Unprocessable", {}, io.BytesIO(body))

    monkeypatch.setattr(bc.urllib.request, "urlopen", raise_422)
    res = bc.post_ledger_entry("vivy", 999.0, run_id="run-abc", dedup_key="run-abc", tok="t")
    assert res["status"] == "error:422"
    assert "soft cap" in res["error"]


def test_post_ledger_entry_an_unreachable_library_is_an_error_status(monkeypatch):
    monkeypatch.setattr(bc.urllib.request, "urlopen",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("down")))
    res = bc.post_ledger_entry("vivy", 0.1, run_id="run-abc", dedup_key="run-abc", tok="t")
    assert res["status"].startswith("error:")


def test_post_ledger_entry_skips_without_a_token(monkeypatch):
    monkeypatch.setattr(bc, "token", lambda: None)
    assert bc.post_ledger_entry("vivy", 0.1, run_id="run-abc", dedup_key="run-abc")["status"] == "skip"
