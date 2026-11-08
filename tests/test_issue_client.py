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
    items = bc.list_open_issues("widget", tok="t")
    assert items == [{"id": "bug_1", "state": "open"}]
    assert "state=open" in captured["url"] and "product=widget" in captured["url"]


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


# --- deadline: the TOTAL-time bound across every page (round 1, both
# reviewers: a per-page `_TIMEOUT` alone still leaves a 100-page call
# unbounded overall). The clock is stubbed throughout — never a real sleep. --


def test_deadline_in_is_an_absolute_monotonic_timestamp(monkeypatch):
    monkeypatch.setattr(bc, "_monotonic", lambda: 100.0)
    assert bc.deadline_in(5.0) == 105.0


def test_list_issues_by_state_bounds_each_pages_timeout_by_the_time_left(monkeypatch):
    """Each request's own timeout is `min(_TIMEOUT, time left to the
    deadline)` — against code that passes `_TIMEOUT` unconditionally, this
    page's captured timeout would be 5.0, not the shorter 2.0 the deadline
    actually leaves."""
    clock = [0.0]
    monkeypatch.setattr(bc, "_monotonic", lambda: clock[0])
    captured = []

    def fake_urlopen(req, timeout=None):
        captured.append(timeout)
        return _Resp(200, {"items": [{"id": "x"}], "truncated": False})

    monkeypatch.setattr(bc.urllib.request, "urlopen", fake_urlopen)
    items = bc.list_issues_by_state("open", tok="t", deadline=bc.deadline_in(2.0))
    assert [i["id"] for i in items] == ["x"]
    assert captured == [2.0]


def test_list_issues_by_state_raises_once_the_deadline_passes_without_a_further_request(
        monkeypatch):
    """A slow-but-reachable Library (each page actually taking longer than the
    remaining budget) must stop BEFORE the next request, not after it, and
    the error must name a deadline, not a transport failure — against code
    with no deadline check at all, this would instead keep paging forever (or
    until `_MAX_PAGES`), making the extra request below."""
    clock = [0.0]
    monkeypatch.setattr(bc, "_monotonic", lambda: clock[0])
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(timeout)
        clock[0] += 10.0  # this page alone outlasts the whole deadline
        return _Resp(200, {"items": [{"id": "x"}], "truncated": True})

    monkeypatch.setattr(bc.urllib.request, "urlopen", fake_urlopen)
    deadline = bc.deadline_in(5.0)
    with pytest.raises(bc.IssueQueueUnavailable, match="deadline exceeded"):
        bc.list_issues_by_state("open", tok="t", deadline=deadline)
    assert len(calls) == 1  # the second page is never attempted


def test_list_issues_by_state_with_no_deadline_is_unbounded_as_before(monkeypatch):
    """`deadline=None` (every caller before this parameter existed —
    `bin/work-item-cost`'s and the reconciler's own calls) must keep paging
    past any budget a deadline would have imposed, each page bound only by
    `_TIMEOUT`."""
    clock = [0.0]
    monkeypatch.setattr(bc, "_monotonic", lambda: clock[0])
    pages = [
        {"items": [{"id": "i1"}], "truncated": True},
        {"items": [{"id": "i2"}], "truncated": False},
    ]
    captured = []

    def fake_urlopen(req, timeout=None):
        captured.append(timeout)
        clock[0] += 10.0
        return _Resp(200, pages[len(captured) - 1])

    monkeypatch.setattr(bc.urllib.request, "urlopen", fake_urlopen)
    items = bc.list_issues_by_state("open", tok="t")
    assert [i["id"] for i in items] == ["i1", "i2"]
    assert captured == [bc._TIMEOUT, bc._TIMEOUT]


def test_list_all_items_passes_the_same_deadline_to_every_state(monkeypatch):
    """The deadline must be threaded through UNCHANGED to every one of the
    seven states — against code that gives each state a fresh deadline off
    its own elapsed time, the states would not all see the same value."""
    seen = []

    def fake_list_issues_by_state(state, product=None, tok=None, deadline=None):
        seen.append(deadline)
        return []
    monkeypatch.setattr(bc, "list_issues_by_state", fake_list_issues_by_state)

    deadline = bc.deadline_in(5.0)
    bc.list_all_items(tok="t", deadline=deadline)
    assert seen == [deadline] * len(bc.ALL_ITEM_STATES)


def test_list_all_items_raises_once_the_deadline_passes_across_states(monkeypatch):
    """The deadline bounds the TOTAL read across every state, not a fresh
    budget per state — against code with no deadline support, a slow
    Library here would make all seven states' worth of requests instead of
    stopping after the first."""
    clock = [0.0]
    monkeypatch.setattr(bc, "_monotonic", lambda: clock[0])
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        clock[0] += 10.0  # longer than the whole deadline, on the very first page
        return _Resp(200, {"items": [{"id": "x"}], "truncated": False})

    monkeypatch.setattr(bc.urllib.request, "urlopen", fake_urlopen)
    deadline = bc.deadline_in(5.0)
    with pytest.raises(bc.IssueQueueUnavailable, match="deadline exceeded"):
        bc.list_all_items(tok="t", deadline=deadline)
    assert len(calls) == 1  # only the first state's first page was ever attempted


def test_list_all_items_with_no_deadline_passes_none_to_every_state(monkeypatch):
    """A caller passing no deadline (every caller before this parameter
    existed) must be unchanged: every state still gets `deadline=None`."""
    seen = []

    def fake_list_issues_by_state(state, product=None, tok=None, deadline=None):
        seen.append((state, deadline))
        return [{"id": state}]
    monkeypatch.setattr(bc, "list_issues_by_state", fake_list_issues_by_state)

    items = bc.list_all_items(tok="t")
    assert seen == [(s, None) for s in bc.ALL_ITEM_STATES]
    assert {i["id"] for i in items} == set(bc.ALL_ITEM_STATES)


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


def test_list_corrections_queues_builds_url_and_returns_the_list(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        return _Resp(200, {"corrections_queues": [{"id": "cq_1", "product": "bosun"}]})

    monkeypatch.setattr(bc.urllib.request, "urlopen", fake_urlopen)
    queues = bc.list_corrections_queues("bosun", tok="t")
    assert queues == [{"id": "cq_1", "product": "bosun"}]
    assert "product=bosun" in captured["url"]


def test_list_corrections_queues_empty_list_reads_as_no_queues(monkeypatch):
    """The Library-confirmed shape: a product with no queue gets a 200 with
    an empty `corrections_queues` list, not a 404."""
    monkeypatch.setattr(bc.urllib.request, "urlopen",
                        lambda *_a, **_k: _Resp(200, {"corrections_queues": [], "count": 0}))
    assert bc.list_corrections_queues("bosun", tok="t") == []


def test_list_corrections_queues_404_reads_as_no_queues_not_an_error(monkeypatch):
    """A 404 means the route does not exist yet (the deploy has not landed),
    which is indistinguishable from "no queue exists" to a caller, since no
    item can carry a queue parent before that same deploy either."""
    def not_found(req, timeout=None):
        raise bc.urllib.error.HTTPError(req.full_url, 404, "not found", {}, None)

    monkeypatch.setattr(bc.urllib.request, "urlopen", not_found)
    assert bc.list_corrections_queues(tok="t") == []


def test_list_corrections_queues_raises_on_a_5xx_rather_than_reading_as_empty(monkeypatch):
    """Unlike a 404, a 5xx is not "no queues exist" — it is "unknown" — so a
    caller must not read it as the empty set."""
    def server_error(req, timeout=None):
        raise bc.urllib.error.HTTPError(req.full_url, 503, "Service Unavailable", {}, None)

    monkeypatch.setattr(bc.urllib.request, "urlopen", server_error)
    with pytest.raises(bc.IssueQueueUnavailable):
        bc.list_corrections_queues(tok="t")


def test_list_corrections_queues_raises_rather_than_reading_as_empty(monkeypatch):
    monkeypatch.setattr(bc.urllib.request, "urlopen",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("down")))
    with pytest.raises(bc.IssueQueueUnavailable):
        bc.list_corrections_queues(tok="t")


def test_list_corrections_queues_raises_without_a_token(monkeypatch):
    monkeypatch.setattr(bc, "token", lambda: None)
    with pytest.raises(bc.IssueQueueUnavailable):
        bc.list_corrections_queues()


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
    res = bc.link_pr_to_issue("bug_1", "42", url="https://github.com/convivy/widget/pull/42", tok="t")
    assert res["status"] == "ok" and res["item"]["state"] == "pr-open"
    assert captured["url"].endswith("/api/issues/bug_1/link-pr")
    assert captured["body"] == {"pr": "42", "url": "https://github.com/convivy/widget/pull/42"}


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


@pytest.mark.parametrize("outcome", ["merged", "closed", "whatever-the-caller-sends"])
def test_reconcile_pr_carries_each_outcome_unchanged(monkeypatch, outcome):
    """No client-side check on the value, valid ("merged"/"closed") or not — the
    Library decides and refuses what it does not accept."""
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

    Skips when the checkout is absent (CI has none) OR present but unreadable
    (epic E33 slice 2: a dispatched run's sandbox denies this path, which is
    never on the read allowlist — `path.exists()` alone would not catch this,
    since that is a metadata read the sandbox leaves open, while the actual
    `read_text()` below is a data read it denies). Both read as "cannot see
    the server's definition here" and skip identically; a guard that treated
    "present but unreadable" as present-and-readable would have reported a
    `PermissionError` instead of the skip this absence always produced.

    Fails LOUDLY and specifically when the file IS read but the constant is
    not in it — that is the server having moved or renamed it, which is a
    different thing from client/server drift and wants a different fix, so
    the two must not report the same way.

    A member may be a bare identifier rather than a literal — `ITEM_TYPES` names
    two of its five members via `ITEM_TYPE_TASK`/`ITEM_TYPE_EPIC` rather than
    `"task"`/`"epic"` directly. Each such identifier is resolved against its own
    top-level `NAME = "literal"` assignment elsewhere in the same file.
    """
    import re
    try:
        text = path.read_text()
    except OSError:
        pytest.skip(absent_skip)
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


def test_server_frozenset_skips_on_a_present_but_unreadable_path(tmp_path, monkeypatch):
    """Simulates the sandbox's own split (epic E33 slice 2): the file exists
    (`path.exists()` would say so) but reading it raises `PermissionError`,
    which must skip exactly like an absent checkout, never surface as an
    uncaught `PermissionError`."""
    path = tmp_path / "store.py"
    path.write_text('ITEM_COMMENT_KINDS = frozenset({"a"})\n')

    def _denied(self, *a, **k):
        raise PermissionError(1, "Operation not permitted", str(self))

    monkeypatch.setattr(pathlib.Path, "read_text", _denied)
    with pytest.raises(pytest.skip.Exception, match="unreadable"):
        _server_frozenset(path, "ITEM_COMMENT_KINDS", absent_skip="unreadable")


def test_server_frozenset_skips_on_a_missing_path(tmp_path):
    path = tmp_path / "never-created.py"
    with pytest.raises(pytest.skip.Exception, match="absent"):
        _server_frozenset(path, "ITEM_COMMENT_KINDS", absent_skip="absent")


def test_server_frozenset_raises_loudly_when_the_constant_has_moved(tmp_path):
    """A readable file missing the named constant is a DIFFERENT fact from
    either skip above — the server renamed or moved it — and must fail rather
    than skip, so the two causes are never confused."""
    path = tmp_path / "store.py"
    path.write_text("SOME_OTHER_CONSTANT = frozenset({'a'})\n")
    with pytest.raises(AssertionError, match="could not find"):
        _server_frozenset(path, "ITEM_COMMENT_KINDS", absent_skip="absent")


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


def test_the_ops_server_advertises_only_epic_which_the_library_still_accepts(
        mcp_ops_server):
    """`bin/mcp-ops-server`'s `issue_create`/`issue_list` docstrings used to name
    the Library's whole `ITEM_TYPES` set so an agent would learn every value to
    pass before filing or filtering — a type the docstring named but the
    Library rejected was a 400 discovered mid-run, the same failure mode the
    kinds/severities/resolutions guards above still catch for their own fields.

    Work items are not typed (the operator, 2026-09-24): `type` dropped to one
    settable value, `epic`, so there is no longer an enum for the two sides to
    keep in step on. This pins the two halves of what replaced that guard: no
    multi-value type list has crept back into either docstring, and `epic`
    itself still names something the Library accepts — if the Library ever
    dropped it, `issue_create`'s one remaining type value would be filing
    nothing.
    """
    import re
    server = _server_frozenset(
        LIBRARY_STORE, "ITEM_TYPES",
        absent_skip="library not checked out; cannot read the server's types")
    assert "epic" in server, (
        "the Library dropped `epic` from ITEM_TYPES; issue_create's only "
        "remaining type value no longer names anything it accepts")
    for fn in (mcp_ops_server.issue_create, mcp_ops_server.issue_list):
        assert not re.search(r"\b[a-z]+\|[a-z]+(?:\|[a-z]+)+\b", fn.__doc__), (
            f"{fn.__name__}'s docstring names a multi-value type enum again; "
            f"work items are not typed (the operator, 2026-09-24)")


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
                       "/srv/convivy/agent-runs/2026-08-05/co-widget-0930")
    bodies = []

    def fake_urlopen(req, timeout=None):
        bodies.append(json.loads(req.data))
        return _Resp(200, {"issue": {}, "lease": {}, "policy": {}})

    monkeypatch.setattr(bc.urllib.request, "urlopen", fake_urlopen)
    bc.checkout_issue("issue_a", tok="t")
    bc.heartbeat_issue("issue_a", tok="t")
    assert all(b["owner"] == "2026-08-05/co-widget-0930" for b in bodies), bodies


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
    res = bc.post_ledger_entry("widget", 0.1234, run_id="run-abc", dedup_key="run-abc",
                              lane="gateway", agent="coder", model="claude-sonnet-5", tok="t")
    assert res["status"] == "recorded"
    assert res["entry"]["id"] == "entry_1"
    assert captured["url"].endswith("/ledger-entries")
    assert captured["auth"] == "Bearer t"
    assert captured["protocol"] == "1"
    assert captured["body"] == {
        "product": "widget", "source": "agentctl", "usd": 0.1234,
        "run_id": "run-abc", "dedup_key": "run-abc", "lane": "gateway",
        "agent": "coder", "model": "claude-sonnet-5",
    }


def test_post_ledger_entry_sends_the_subscription_lane(monkeypatch):
    """Against the unfixed code (no `lane` parameter at all), this fails with a
    TypeError on the `lane=` keyword, not on a wrong body — the parameter itself
    is what E1·6 adds."""
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data)
        return _Resp(200, {"recorded": True, "id": "entry_sub"})

    monkeypatch.setattr(bc.urllib.request, "urlopen", fake_urlopen)
    bc.post_ledger_entry("widget", 0.5, run_id="run-abc", dedup_key="run-abc",
                        lane="subscription", tok="t")
    assert captured["body"]["lane"] == "subscription"


def test_post_ledger_entry_omits_agent_and_model_when_absent(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data)
        return _Resp(200, {"recorded": True, "id": "entry_2"})

    monkeypatch.setattr(bc.urllib.request, "urlopen", fake_urlopen)
    bc.post_ledger_entry("widget", 0.5, run_id="run-abc", dedup_key="run-abc",
                        lane="subscription", tok="t")
    assert "agent" not in captured["body"]
    assert "model" not in captured["body"]


def test_post_ledger_entry_sends_item_id_when_given(monkeypatch):
    """item_id (E16·5) is what lets the Library derive the entry's epic_id, which
    is what a per-epic cost reconciliation reads back."""
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data)
        return _Resp(200, {"recorded": True, "id": "entry_3"})

    monkeypatch.setattr(bc.urllib.request, "urlopen", fake_urlopen)
    bc.post_ledger_entry("widget", 0.5, run_id="run-abc", dedup_key="run-abc",
                        lane="subscription", item_id="issue_E16_5", tok="t")
    assert captured["body"]["item_id"] == "issue_E16_5"


def test_post_ledger_entry_omits_item_id_when_absent(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data)
        return _Resp(200, {"recorded": True, "id": "entry_4"})

    monkeypatch.setattr(bc.urllib.request, "urlopen", fake_urlopen)
    bc.post_ledger_entry("widget", 0.5, run_id="run-abc", dedup_key="run-abc",
                        lane="subscription", tok="t")
    assert "item_id" not in captured["body"]


def test_post_ledger_entry_sends_day_when_given(monkeypatch):
    """`day` is the caller's own bucketing of the run's finish instant
    (`telemetry.platform_day`/`platform_day_for`) — sent verbatim, this
    client has no opinion on how it was computed."""
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data)
        return _Resp(200, {"recorded": True, "id": "entry_5"})

    monkeypatch.setattr(bc.urllib.request, "urlopen", fake_urlopen)
    bc.post_ledger_entry("widget", 0.5, run_id="run-abc", dedup_key="run-abc",
                        lane="subscription", day="2026-10-20", tok="t")
    assert captured["body"]["day"] == "2026-10-20"


def test_post_ledger_entry_omits_day_when_absent(monkeypatch):
    """No `day` argument means no `day` key in the body — the Library books
    the entry to its own local date on arrival instead (its route's
    documented `day = payload.get("day") or local_day()` fallback)."""
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data)
        return _Resp(200, {"recorded": True, "id": "entry_6"})

    monkeypatch.setattr(bc.urllib.request, "urlopen", fake_urlopen)
    bc.post_ledger_entry("widget", 0.5, run_id="run-abc", dedup_key="run-abc",
                        lane="subscription", tok="t")
    assert "day" not in captured["body"]


def test_post_ledger_entry_a_rerun_sends_the_same_dedup_key(monkeypatch):
    seen = []

    def fake_urlopen(req, timeout=None):
        seen.append(json.loads(req.data)["dedup_key"])
        return _Resp(200, {"recorded": True, "id": "entry_1", "idempotent": len(seen) > 1})

    monkeypatch.setattr(bc.urllib.request, "urlopen", fake_urlopen)
    bc.post_ledger_entry("widget", 0.1, run_id="run-abc", dedup_key="run-abc",
                        lane="subscription", tok="t")
    bc.post_ledger_entry("widget", 0.1, run_id="run-abc", dedup_key="run-abc",
                        lane="subscription", tok="t")
    assert seen == ["run-abc", "run-abc"]


def test_post_ledger_entry_recorded_false_is_not_recorded_not_an_error(monkeypatch):
    monkeypatch.setattr(
        bc.urllib.request, "urlopen",
        lambda *_a, **_k: _Resp(200, {"recorded": False,
                                      "reason": "no live ledger for this scope"}))
    res = bc.post_ledger_entry("widget", 0.1, run_id="run-abc", dedup_key="run-abc",
                              lane="subscription", tok="t")
    assert res["status"] == "not-recorded"


def test_post_ledger_entry_a_disproportionate_refusal_is_reported_not_retried(monkeypatch):
    """A distinct status (E1·9), not folded into `error:422`: the caller must be
    able to tell this apart from every other refusal without parsing `error`
    text, since this is the one that must never be resubmitted unchanged."""
    def raise_422(req, timeout=None):
        body = json.dumps({"error": "this report exceeds 10x the scope's soft cap",
                           "basis": "soft_cap"}).encode()
        raise bc.urllib.error.HTTPError(req.full_url, 422, "Unprocessable", {}, io.BytesIO(body))

    monkeypatch.setattr(bc.urllib.request, "urlopen", raise_422)
    res = bc.post_ledger_entry("widget", 999.0, run_id="run-abc", dedup_key="run-abc",
                              lane="subscription", tok="t")
    assert res["status"] == "disproportionate"
    assert "soft cap" in res["error"]
    assert res["reported_usd"] == 999.0
    assert res["basis"] == "soft_cap"


def test_post_ledger_entry_a_different_422_stays_error_422(monkeypatch):
    """"day is too far from this Library's own date" is also a 422, but carries
    no `basis` field — against an implementation that branches on the status
    code alone, this would also read "disproportionate" and get silently
    marked on the run as unchanged-and-never-resubmit, which is wrong for a
    refusal that has nothing to do with the figure's size."""
    def raise_422(req, timeout=None):
        body = json.dumps({"error": "day is too far from this Library's own date",
                           "day": "2026-10-01", "library_day": "2026-10-13"}).encode()
        raise bc.urllib.error.HTTPError(req.full_url, 422, "Unprocessable", {}, io.BytesIO(body))

    monkeypatch.setattr(bc.urllib.request, "urlopen", raise_422)
    res = bc.post_ledger_entry("widget", 0.5, run_id="run-abc", dedup_key="run-abc",
                              lane="subscription", tok="t")
    assert res["status"] == "error:422"
    assert "day is too far" in res["error"]


def test_post_ledger_entry_an_unreachable_library_is_an_error_status(monkeypatch):
    monkeypatch.setattr(bc.urllib.request, "urlopen",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("down")))
    res = bc.post_ledger_entry("widget", 0.1, run_id="run-abc", dedup_key="run-abc",
                              lane="subscription", tok="t")
    assert res["status"].startswith("error:")


def test_post_ledger_entry_reads_the_ledger_reporter_token_file(monkeypatch, tmp_path):
    """No `tok` override: the credential comes from LEDGER_TOKEN_PATH, not the
    general `token()` — against the unfixed code (which falls back to
    `token()`), this fails because `token()` is stubbed to a different value
    than what the fake request actually carries."""
    token_file = tmp_path / "bosun-ledger-reporter.token"
    token_file.write_text("real-ledger-token\n")
    monkeypatch.setattr(bc, "LEDGER_TOKEN_PATH", token_file)
    monkeypatch.setattr(bc, "token", lambda: "wrong-general-token")
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["auth"] = req.headers.get("Authorization")
        return _Resp(200, {"recorded": True, "id": "entry_5"})

    monkeypatch.setattr(bc.urllib.request, "urlopen", fake_urlopen)
    res = bc.post_ledger_entry("widget", 0.1, run_id="run-abc", dedup_key="run-abc",
                              lane="subscription")
    assert res["status"] == "recorded"
    assert captured["auth"] == "Bearer real-ledger-token"


def test_post_ledger_entry_skips_without_a_token_file(monkeypatch, tmp_path):
    """No fallback to the general `token()` credential (E1·6): it lacks the
    `ledger.spend.subscription` grant, so falling back to it would let a
    subscription-lane entry through on a credential the Library would refuse
    anyway — against the unfixed code (plain `token()` fallback), `token()`
    below would supply a value and the call would NOT skip."""
    monkeypatch.setattr(bc, "LEDGER_TOKEN_PATH", tmp_path / "missing.token")
    monkeypatch.setattr(bc, "token", lambda: "general-token-should-not-be-used")
    calls = []
    monkeypatch.setattr(bc.urllib.request, "urlopen",
                        lambda *a, **k: calls.append(1))
    res = bc.post_ledger_entry("widget", 0.1, run_id="run-abc", dedup_key="run-abc",
                              lane="subscription")
    assert res["status"] == "skip"
    assert calls == []


def test_post_ledger_entry_skips_on_an_empty_token_file(monkeypatch, tmp_path):
    empty = tmp_path / "bosun-ledger-reporter.token"
    empty.write_text("   \n")
    monkeypatch.setattr(bc, "LEDGER_TOKEN_PATH", empty)
    monkeypatch.setattr(bc, "token", lambda: "general-token-should-not-be-used")
    assert bc.post_ledger_entry(
        "widget", 0.1, run_id="run-abc", dedup_key="run-abc",
        lane="subscription")["status"] == "skip"


# --- list_ledger_entries: the read side of the Bosun's ledger writer (E16·5) --


def test_list_ledger_entries_builds_the_url_from_every_filter(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        return _Resp(200, {"entries": [], "truncated": False})

    monkeypatch.setattr(bc.urllib.request, "urlopen", fake_urlopen)
    bc.list_ledger_entries(epic_id="issue_E", item_id="issue_C", run_id="run-1",
                           product="widget", source="agentctl", day="2026-10-01", tok="t")
    url = captured["url"]
    assert "/ledger-entries?" in url
    for pair in ("epic_id=issue_E", "item_id=issue_C", "run_id=run-1",
                "product=widget", "source=agentctl", "day=2026-10-01"):
        assert pair in url


def test_list_ledger_entries_follows_pagination(monkeypatch):
    """A per-epic reconciliation that read only the first page would treat a partial
    slice of the ledger as the whole one, and a real mismatch on a later page would
    read as a clean match."""
    pages = [
        {"entries": [{"run_id": "run-1"}, {"run_id": "run-2"}], "truncated": True},
        {"entries": [{"run_id": "run-3"}], "truncated": False},
    ]
    seen = []

    def fake_urlopen(req, timeout=None):
        seen.append(req.full_url)
        return _Resp(200, pages[len(seen) - 1])

    monkeypatch.setattr(bc.urllib.request, "urlopen", fake_urlopen)
    entries = bc.list_ledger_entries(epic_id="issue_E", tok="t")
    assert [e["run_id"] for e in entries] == ["run-1", "run-2", "run-3"]
    assert "offset=0" in seen[0] and "offset=2" in seen[1]


def test_list_ledger_entries_stops_on_an_empty_truncated_page(monkeypatch):
    monkeypatch.setattr(bc.urllib.request, "urlopen",
                        lambda *_a, **_k: _Resp(200, {"entries": [], "truncated": True}))
    assert bc.list_ledger_entries(epic_id="issue_E", tok="t") == []


def test_list_ledger_entries_refuses_to_page_forever(monkeypatch):
    monkeypatch.setattr(bc, "_MAX_PAGES", 3)
    monkeypatch.setattr(bc.urllib.request, "urlopen",
                        lambda *_a, **_k: _Resp(200, {"entries": [{"run_id": "x"}],
                                                      "truncated": True}))
    with pytest.raises(bc.IssueQueueUnavailable, match="refusing to keep paging"):
        bc.list_ledger_entries(epic_id="issue_E", tok="t")


def test_list_ledger_entries_raises_rather_than_reading_as_empty(monkeypatch):
    """A reconciliation that read [] on an outage would report a clean match while
    blind — the same ARCH-311 shape list_issues_by_state guards against."""
    monkeypatch.setattr(bc.urllib.request, "urlopen",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("down")))
    with pytest.raises(bc.IssueQueueUnavailable):
        bc.list_ledger_entries(epic_id="issue_E", tok="t")


def test_list_ledger_entries_raises_without_a_token(monkeypatch):
    monkeypatch.setattr(bc, "token", lambda: None)
    with pytest.raises(bc.IssueQueueUnavailable):
        bc.list_ledger_entries(epic_id="issue_E")


# --- check_merge_admission: POST /merge/admission (CQ·534, library-protocol §6.7) ---
#
# Runs through pinned_opener() against the Library's FIXED default address, the
# same as get_item_from_default_library/post_review_record, and for the same
# reason: the answer AUTHORISES a merge, so nothing the caller's environment
# controls may steer it. Stubbed here at the opener, never at urllib.request.
# urlopen — conftest's `_no_live_pinned_opener_calls` guard fails loudly on an
# unpatched pinned_opener() reaching a live Library, which an urlopen-only stub
# would leave unpatched.


class _StubOpener:
    """Stands in for `issue_client.pinned_opener()`: records every request
    passed to `.open()` and answers (or raises) via `handler`."""

    def __init__(self, handler):
        self._handler = handler
        self.requests: list = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        return self._handler(req, timeout)


def test_check_merge_admission_sends_the_documented_fields(monkeypatch):
    captured = {}

    def handler(req, timeout):
        captured["url"] = req.full_url
        captured["auth"] = req.headers.get("Authorization")
        captured["protocol"] = req.headers.get("Library-protocol")
        captured["body"] = json.loads(req.data)
        return _Resp(200, {"approved": True, "reason": "", "head_sha": "a" * 40,
                           "enforced": True, "missing": []})

    monkeypatch.setattr(bc, "pinned_opener", lambda: _StubOpener(handler))
    res = bc.check_merge_admission("convivy/knowledge", 7, "a" * 40,
                                   "agentctl kb merge", tok="t")
    assert res == {"approved": True, "reason": "", "head_sha": "a" * 40,
                   "enforced": True, "missing": []}
    assert captured["url"] == bc.vault_plane.fixed_library_url("/merge/admission")
    assert captured["auth"] == "Bearer t"
    assert captured["protocol"] == "1"
    assert captured["body"] == {"repo": "convivy/knowledge", "pr": 7,
                                "head_sha": "a" * 40, "requester": "agentctl kb merge"}


def test_check_merge_admission_returns_a_refusal_body_rather_than_raising(monkeypatch):
    """`approved: false` is a well-formed 200 answer, not a failure of the ask
    itself — the CALLER decides whether to merge; this client only carries
    the answer."""
    monkeypatch.setattr(
        bc, "pinned_opener",
        lambda: _StubOpener(lambda req, timeout: _Resp(200, {
            "approved": False, "reason": "round cap exceeded", "head_sha": "a" * 40,
            "enforced": True,
            "missing": [{"kind": "stamp", "role": "security-reviewer", "workflow": None,
                        "detail": "no APPROVE at this head", "retryable": True}]})))
    res = bc.check_merge_admission("convivy/knowledge", 7, "a" * 40,
                                   "agentctl kb merge", tok="t")
    assert res["approved"] is False
    assert res["reason"] == "round cap exceeded"
    assert res["missing"][0]["role"] == "security-reviewer"


@pytest.mark.parametrize("code, error_text", [
    (401, "no identity resolves"),
    (403, "this identity lacks 'merge.admit'"),
    (404, "convivy/knowledge is not a registered project"),
    (500, "internal error"),
])
def test_check_merge_admission_raises_on_a_refusal_status(monkeypatch, code, error_text):
    def handler(req, timeout):
        body = json.dumps({"error": error_text}).encode()
        raise bc.urllib.error.HTTPError(req.full_url, code, "refused", {}, io.BytesIO(body))

    monkeypatch.setattr(bc, "pinned_opener", lambda: _StubOpener(handler))
    with pytest.raises(bc.MergeAdmissionError) as excinfo:
        bc.check_merge_admission("convivy/knowledge", 7, "a" * 40, "agentctl kb merge", tok="t")
    assert excinfo.value.status == str(code)
    assert excinfo.value.text == error_text


def test_check_merge_admission_raises_on_a_timeout(monkeypatch):
    def handler(req, timeout):
        raise TimeoutError("timed out")

    monkeypatch.setattr(bc, "pinned_opener", lambda: _StubOpener(handler))
    with pytest.raises(bc.MergeAdmissionError) as excinfo:
        bc.check_merge_admission("convivy/knowledge", 7, "a" * 40, "agentctl kb merge", tok="t")
    assert excinfo.value.status == "transport:TimeoutError"


def test_check_merge_admission_raises_on_a_malformed_body(monkeypatch):
    """A 200 whose body has no `approved` field is not a well-formed answer —
    there is no reading it as either approved or refused."""
    monkeypatch.setattr(
        bc, "pinned_opener",
        lambda: _StubOpener(lambda req, timeout: _Resp(200, {"reason": "no approved field at all"})))
    with pytest.raises(bc.MergeAdmissionError) as excinfo:
        bc.check_merge_admission("convivy/knowledge", 7, "a" * 40, "agentctl kb merge", tok="t")
    assert excinfo.value.status == "bad-body"


def test_check_merge_admission_raises_without_a_token(monkeypatch):
    monkeypatch.setattr(bc, "token", lambda: None)
    calls = []
    monkeypatch.setattr(bc, "pinned_opener", lambda: calls.append(1))
    with pytest.raises(bc.MergeAdmissionError) as excinfo:
        bc.check_merge_admission("convivy/knowledge", 7, "a" * 40, "agentctl kb merge")
    assert excinfo.value.status == "no-token"
    assert calls == []
