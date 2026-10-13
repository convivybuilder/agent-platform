"""Loopback issue-queue client for platform jobs (the operating box, ARCH-359/361).

A thin client the platform's own tooling uses to read and change items in the
box's issue queue on the mini's loopback control plane. It mirrors decision_client
/ signal_client and reuses the agent-platform bearer token.

It does not create items. A monitor, a script or a scheduled job logs a signal
through signal_client instead, and only a co session creates an item
(platform/decisions/0073). So this module has no create call.

Used by `agentctl`, `co-rollover`, `issue-pr-reconcile`, `issue_gate.py` and the
dispatch path to claim, comment on and resolve items the crew sweep drains
(ARCH-359: the issue is an inbound cue, the box is the record).

Renamed from bug_client → issue_client (ARCH-361 Part 2). The old bug_* names
are kept as deprecated aliases for one cycle — same overlap rationale as
orient's /api/bugs path aliases — and will be removed after all callers migrate.
"""
from __future__ import annotations

import json
import os
import warnings
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import builder_root as BR
import vault_plane
import signal_client as _signal_client
from signal_client import token

_TIMEOUT = 5  # loopback; issue filing isn't latency-critical like the Slack tee

# A read sitting inline on a dispatch's critical path (agentctl's --issue
# model_choice lookup, knowledge-ctx's session_topics()) wants a tighter
# budget than general-purpose issue filing: both block useful work from
# starting, so a slow Library should read as "no override"/"no topics"
# quickly rather than hold the spawn for the full `_TIMEOUT`. One constant so
# the two callers can't drift to different numbers for the same reason.
DISPATCH_READ_TIMEOUT = 3


def _post(path: str, payload: dict, tok: str) -> dict[str, Any]:
    """One authenticated POST to the issue API → {"status", "item"|"error"}. Shared
    by the drain transitions below; never raises (a sync failure must not break the
    caller's real work)."""
    try:
        req = urllib.request.Request(
            vault_plane.url(path), data=json.dumps(payload).encode("utf-8"),
            headers=vault_plane.headers(token=tok, json_body=True),
            method="POST")
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            item: dict[str, Any] = {}
            try:
                item = json.loads(resp.read().decode("utf-8"))
            except Exception:
                pass
            return {"status": "ok", "item": item}
    except urllib.error.HTTPError as e:
        return {"status": f"error:{e.code}", "item": {}}
    except Exception as e:
        return {"status": f"error:{e.__class__.__name__}", "item": {}}


def _issue_path(issue_id: str, action: str) -> str:
    return f"/api/issues/{urllib.parse.quote(issue_id, safe='')}/{action}"


# The queue's severity set, mirroring `ITEM_SEVERITIES` in the Library's
# `store.py`. Kept here so a bad value fails at the call rather than as a 400 in
# a return value nobody reads; `test_client_and_server_agree_on_the_severities`
# reads the server's own constant so the two cannot drift apart silently.
VALID_SEVERITIES = ("high", "medium", "low")


# Item states that end an item. The Library's ITEM_RESOLUTIONS (store.py);
# `test_the_client_and_server_agree_on_the_resolutions` reads it.
RESOLVED_STATES = frozenset({"fixed", "wontfix", "duplicate", "redirected"})


def list_open_issues(product: str | None = None, tok: str | None = None) -> list[dict[str, Any]]:
    """Open issues from the box, for a drain to work through. Returns [] on any
    failure (best-effort). Pass `product` to narrow scope."""
    tok = tok or token()
    if not tok:
        return []
    url = vault_plane.url("/api/issues?state=open")
    if product:
        url += "&product=" + urllib.parse.quote(product, safe="")
    try:
        req = urllib.request.Request(url, headers=vault_plane.headers(token=tok))
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8")).get("items", [])
    except Exception:
        return []


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """Any redirect is an error: a fixed-address call's answer must come from
    the address asked."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


def pinned_opener() -> urllib.request.OpenerDirector:
    """The opener a call to `vault_plane.fixed_library_url()` must use.

    WHY not `urlopen` or the installed global opener: the fixed address exists
    so NOTHING the calling process controls may change where the request goes,
    what it trusts, or whose identity it uses. The global opener (installed by
    vault_plane) carries the stock ProxyHandler, which reads `http_proxy`,
    `HTTP_PROXY`, `all_proxy` and the rest from the environment, so a caller's
    environment could route the request and its bearer token to its own
    listener. `ProxyHandler({})` applies no proxy from any variable; redirects
    are refused.

    Shared by every call to the fixed address — `get_item_from_default_library`
    below, and `agentctl crew doctor`'s fallback-token path (security-reviewer,
    PR #708 round 2) — rather than built fresh at each site, so a hardening
    fix made once here reaches all of them."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _RefuseRedirect())


def get_item_from_default_library(issue_id: str, *, tok: str | None = None,
                                  timeout: float = _TIMEOUT) -> dict[str, Any]:
    """Read ONE work item from the Library's fixed default address.

    Returns `{"status": "ok", "item": <the item>}` on a 2xx whose body is a JSON
    object, and `{"status": "skip"|"error:<code>|<ExceptionName>|bad-body",
    "item": {}}` otherwise. Never raises. **A caller that ACTS on the answer must
    treat every non-`ok` status as "the item is unknown"**, never as "the item has
    nothing on it": `agentctl spawn --issue` refuses a dispatch it cannot read the
    item for rather than dispatching on the roster default. An `{"item": ...}`
    wrapper is unwrapped.

    `agentctl spawn --issue` lets this read's result exempt a dispatch from the
    origin refusal, so NOTHING the dispatching process controls may change where
    the request goes, what it trusts, or whose identity it uses. That rules out
    `vault_plane.url()` (`LIBRARY_BASE_URL`, `VAULTS_ON_LIBRARY`,
    `ORIENT_BASE_URL`), the process-wide opener (its stock ProxyHandler honours
    `http_proxy` and friends), and `signal_client.token()` (which prefers
    `AGENT_SIGNAL_TOKEN`). The route is the Library's `/items/{id}` and the
    request carries the `Library-Protocol` header."""
    # The Bosun's token FILE, read directly: `token()` prefers the
    # AGENT_SIGNAL_TOKEN environment variable, which the caller sets.
    if tok is None:
        try:
            tok = _signal_client.TOKEN_FILE.read_text().strip() or None
        except OSError:
            tok = None
    if not tok:
        return {"status": "skip", "item": {}}
    try:
        req = urllib.request.Request(
            vault_plane.fixed_library_url(f"/items/{urllib.parse.quote(issue_id, safe='')}"),
            headers=vault_plane.library_headers(token=tok))
        with pinned_opener().open(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"status": f"error:{e.code}", "item": {}}
    except Exception as e:
        return {"status": f"error:{e.__class__.__name__}", "item": {}}
    if isinstance(body, dict) and isinstance(body.get("item"), dict) and "id" not in body:
        body = body["item"]
    if not isinstance(body, dict):
        return {"status": "error:bad-body", "item": {}}
    return {"status": "ok", "item": body}


def post_review_record(fields: dict[str, Any], *, tok: str | None = None,
                       timeout: float = _TIMEOUT) -> dict[str, Any]:
    """Write one review record to the Library's fixed default address
    (`POST /review-records`, ARCH-193 phase 2 — `agentctl review-post`'s
    call, before it ever touches the pull request).

    Returns `{"status": "ok", "record": <the created/superseded row>}` on a
    2xx whose body is a JSON object, and `{"status": "error:<code>|
    <ExceptionName>", "error": <str>, "record": {}}` otherwise. Never raises.
    `error` is the Library's own `error` field on an HTTPError body when one
    parses, else the exception's string form — `review-post` names it
    verbatim rather than just the status, so a 503 `"run facts unavailable"`
    reads differently from a 400 naming a bad field.

    Runs through the SAME proxy-free, redirect-refusing opener and the SAME
    direct token-file read as `get_item_from_default_library` (agent-
    platform#648), for the same reason: `review-post` runs inside a
    dispatched reviewer's OWN process, so nothing that process's environment
    controls — `LIBRARY_BASE_URL`, `VAULTS_ON_LIBRARY`, `http_proxy` and
    friends, or `AGENT_SIGNAL_TOKEN` — may change where this write goes or
    whose bearer token rides on it."""
    if tok is None:
        try:
            tok = _signal_client.TOKEN_FILE.read_text().strip() or None
        except OSError:
            tok = None
    if not tok:
        return {"status": "skip", "error": "no Library token available", "record": {}}
    try:
        req = urllib.request.Request(
            vault_plane.fixed_library_url("/review-records"),
            data=json.dumps(fields).encode("utf-8"),
            headers=vault_plane.library_headers(token=tok, json_body=True),
            method="POST")
        with pinned_opener().open(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return {"status": "ok", "record": body}
    except urllib.error.HTTPError as e:
        err_text = str(e)
        try:
            err_body = json.loads(e.read().decode("utf-8"))
            if isinstance(err_body, dict) and isinstance(err_body.get("error"), str):
                err_text = err_body["error"]
        except Exception:
            pass
        return {"status": f"error:{e.code}", "error": err_text, "record": {}}
    except Exception as e:
        return {"status": f"error:{e.__class__.__name__}", "error": str(e), "record": {}}


# The source this client's writer reports as. `agentctl` is one of the two
# values the Library's `store.LEDGER_SOURCES` accepts (the other, "app", is a
# product backend reporting its own runtime spend); a closed set so a value
# no filter recognises never reaches the ledger as a surprise.
LEDGER_SOURCE = "agentctl"

# The Bosun's dedicated ledger-reporting credential (E1·6), minted by the
# Library as `bosun-ledger-reporter` holding the `ledger.spend.subscription`
# grant the general `token()` credential lacks. Resolved under the real
# account home (`BR.real_account_home()`), the same way `bin/agentctl`
# resolves `bosun-app.pem`, so a dispatched run's sandboxed `$HOME` can never
# redirect this read — and `run_sandbox.DENIED_HOME_FILES` refuses a
# dispatched run this path outright regardless of where it resolves.
LEDGER_TOKEN_PATH = BR.real_account_home() / ".config" / "convivy" / "bosun-ledger-reporter.token"


def _ledger_reporter_token() -> str | None:
    """The `bosun-ledger-reporter` credential from LEDGER_TOKEN_PATH, or None
    when the file is absent or empty."""
    try:
        return LEDGER_TOKEN_PATH.read_text().strip() or None
    except OSError:
        return None


def post_ledger_entry(product: str, usd: float, *, run_id: str, dedup_key: str,
                      lane: str,
                      agent: str | None = None, model: str | None = None,
                      item_id: str | None = None,
                      tok: str | None = None, timeout: float = _TIMEOUT) -> dict[str, Any]:
    """Report one run's cost to the Library's ledger (`POST /ledger-entries`,
    the Bosun's first writer — issue_01M0TP37M3RRRY5BD1Z4B8GCB0 Part 1).

    `lane` ("gateway" or "subscription") is required and sent on every entry
    (E1·6): the Library stores it and refuses to sum a product's gateway and
    subscription spend together. The caller decides which lane a row belongs
    to; this client has no opinion on what a model's lane is.

    `item_id`, when given, is the work item the run served (E16·5). The
    Library derives the entry's `epic_id` from it server-side — the item's
    own id when it is an epic, else its parent — so sending it is what lets
    a per-epic cost reconcile to the ledger's own total for that epic at all.
    Sent only when present: an entry with no `item_id` gets `epic_id: null`
    and reads as unattributed to the Library the same way it does in
    `cost_join`'s run-record report.

    Returns `{"status": "recorded"|"not-recorded"|"skip"|"error:<code>|
    <ExceptionName>|bad-body", "entry": <the route's body, or {}>}`, plus
    `"error": <str>` on an `error:` status. Never raises.

    `"not-recorded"` is the route's documented `{"recorded": false}` answer
    for a product with no live ledger — NOT an error, and the caller must
    not retry it, the same as every `error:` status here: a disproportionate-
    spend refusal, a malformed call, or an unreachable Library are each a
    fault in this report, not a transient condition to resend.

    `dedup_key` collides at the Library with any earlier entry carrying the
    same key, answering a resend with the existing row rather than double-
    counting — this client sends whatever key the caller passes rather than
    deriving one, so a re-resolved row on a later harvest pass reports the
    same key and stays idempotent there. This is a plain collision, not a
    reconciliation: the FIRST amount posted under a `dedup_key` is the one
    that stands forever, so a caller whose own figure for a run can still
    change (e.g. `model-scorecard`'s cost before the run has finished) must
    not post under that run's `dedup_key` until the figure is final — see
    `model-scorecard`'s `is_cost_final`.

    Uses `vault_plane.library_url` (the Library's own address, since
    `/ledger-entries` has no Orient counterpart to translate from) and the
    `bosun-ledger-reporter` credential (`_ledger_reporter_token`), NOT the
    general `token()` this module's other calls use: the Library refuses a
    `lane: "subscription"` entry with 403 unless the posting credential holds
    the `ledger.spend.subscription` grant, which only this dedicated token
    carries. `tok`, when given, overrides it — tests inject a token this way
    rather than pointing `LEDGER_TOKEN_PATH` at a real file. An absent or
    empty credential file is a `"skip"`, the same status an absent `token()`
    used to produce here: there is no fallback to `token()`, because the
    general credential lacks the subscription grant and a gateway-only
    credential posting a subscription entry would still be refused."""
    tok = tok or _ledger_reporter_token()
    if not tok:
        return {"status": "skip", "entry": {},
               "error": f"no bosun-ledger-reporter credential at {LEDGER_TOKEN_PATH}"}
    payload: dict[str, Any] = {
        "product": product, "source": LEDGER_SOURCE, "usd": usd,
        "run_id": run_id, "dedup_key": dedup_key, "lane": lane,
    }
    if agent:
        payload["agent"] = agent
    if model:
        payload["model"] = model
    if item_id:
        payload["item_id"] = item_id
    try:
        req = urllib.request.Request(
            vault_plane.library_url("/ledger-entries"),
            data=json.dumps(payload).encode("utf-8"),
            headers=vault_plane.library_headers(token=tok, json_body=True),
            method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_text = str(e)
        try:
            err_body = json.loads(e.read().decode("utf-8"))
            if isinstance(err_body, dict) and isinstance(err_body.get("error"), str):
                err_text = err_body["error"]
        except Exception:
            pass
        return {"status": f"error:{e.code}", "error": err_text, "entry": {}}
    except Exception as e:
        return {"status": f"error:{e.__class__.__name__}", "error": str(e), "entry": {}}
    if not isinstance(body, dict):
        return {"status": "error:bad-body", "error": "not a JSON object", "entry": {}}
    if body.get("recorded") is False:
        return {"status": "not-recorded", "entry": body}
    return {"status": "recorded", "entry": body}


class IssueQueueUnavailable(RuntimeError):
    """The queue could not be read, so its contents are UNKNOWN — not empty."""


# 200 items per page, so this is a 20,000-item ceiling — orders of magnitude above
# any real queue, which is what makes hitting it a server fault rather than growth.
_MAX_PAGES = 100


def list_issues_by_state(state: str, product: str | None = None,
                         tok: str | None = None) -> list[dict[str, Any]]:
    """Every issue in `state`, following pagination to the end.

    RAISES IssueQueueUnavailable rather than returning [] on failure, for the same
    reason list_claimable_issues does: a caller that acts on "nothing to do" cannot
    tell an empty state from an unreachable API, and the reconciler built on this
    would report a clean sweep while blind.

    Pagination is followed here rather than left to the caller. The server caps a
    page at 200 and reports `truncated`; a reconciler that read only the first page
    would leave the tail permanently unreconciled, growing exactly as the queue does.

    Bounded at _MAX_PAGES. The empty-page guard already stops the one spin a real
    server could produce (`truncated` true with no items), but this runs unattended
    from launchd, where an unbounded loop against a misbehaving server is a machine
    hammering an API all night with nobody watching. A ceiling turns that into one
    loud failure.
    """
    tok = tok or token()
    if not tok:
        raise IssueQueueUnavailable("no ops API token available")
    out: list[dict[str, Any]] = []
    offset = 0
    for _ in range(_MAX_PAGES):
        url = vault_plane.url(
            f"/api/issues?state={urllib.parse.quote(state, safe='')}&offset={offset}")
        if product:
            url += "&product=" + urllib.parse.quote(product, safe="")
        try:
            req = urllib.request.Request(url, headers=vault_plane.headers(token=tok))
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            raise IssueQueueUnavailable(f"could not read the issue queue: {e}") from e
        items = payload.get("items")
        if not isinstance(items, list):
            raise IssueQueueUnavailable(f"issue queue returned no items list: {payload!r:.200}")
        out.extend(items)
        if not payload.get("truncated") or not items:
            return out
        offset += len(items)
    raise IssueQueueUnavailable(
        f"issue queue still reports more after {_MAX_PAGES} pages ({len(out)} items); "
        "refusing to keep paging")


def get_item(item_id: str, tok: str | None = None) -> dict[str, Any]:
    """One work item by id, full shape (`GET /items/{id}`): body, comments, events,
    and for an epic its `progress`.

    RAISES IssueQueueUnavailable on any failure, the same posture as
    `list_issues_by_state` and for the same reason: a caller building a cost report
    from this cannot tell a missing item from an unreachable Library, and reporting
    a partial figure while blind is worse than refusing to report at all."""
    tok = tok or token()
    if not tok:
        raise IssueQueueUnavailable("no ops API token available")
    url = vault_plane.url(f"/api/items/{urllib.parse.quote(item_id, safe='')}")
    try:
        req = urllib.request.Request(url, headers=vault_plane.headers(token=tok))
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        raise IssueQueueUnavailable(f"could not read item {item_id}: {e}") from e
    if not isinstance(payload, dict):
        raise IssueQueueUnavailable(
            f"item {item_id} returned a non-object body: {payload!r:.200}")
    return payload


def get_course(course_id: str, tok: str | None = None) -> dict[str, Any]:
    """One Course by id, with its resolved items and its `position`
    (`GET /courses/{id}`). RAISES IssueQueueUnavailable on any failure, the same
    posture as `get_item`."""
    tok = tok or token()
    if not tok:
        raise IssueQueueUnavailable("no ops API token available")
    url = vault_plane.url(f"/api/courses/{urllib.parse.quote(course_id, safe='')}")
    try:
        req = urllib.request.Request(url, headers=vault_plane.headers(token=tok))
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        raise IssueQueueUnavailable(f"could not read Course {course_id}: {e}") from e
    if not isinstance(payload, dict):
        raise IssueQueueUnavailable(
            f"Course {course_id} returned a non-object body: {payload!r:.200}")
    return payload


def list_courses(product: str, tok: str | None = None) -> list[dict[str, Any]]:
    """Every live Course for `product` (`GET /courses?product=`), following
    pagination to the end, the same way `list_issues_by_state` does and for the
    same reason: a caller reading only the first page would treat a partial list
    as the whole one. RAISES IssueQueueUnavailable on any failure."""
    tok = tok or token()
    if not tok:
        raise IssueQueueUnavailable("no ops API token available")
    out: list[dict[str, Any]] = []
    offset = 0
    for _ in range(_MAX_PAGES):
        url = vault_plane.url(
            f"/api/courses?product={urllib.parse.quote(product, safe='')}&offset={offset}")
        try:
            req = urllib.request.Request(url, headers=vault_plane.headers(token=tok))
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            raise IssueQueueUnavailable(f"could not read Courses for {product}: {e}") from e
        courses = payload.get("courses")
        if not isinstance(courses, list):
            raise IssueQueueUnavailable(
                f"Course list returned no courses list: {payload!r:.200}")
        out.extend(courses)
        if not payload.get("truncated") or not courses:
            return out
        offset += len(courses)
    raise IssueQueueUnavailable(
        f"Course list for {product} still reports more after {_MAX_PAGES} pages "
        f"({len(out)} courses); refusing to keep paging")


def list_corrections_queues(product: str | None = None,
                            tok: str | None = None) -> list[dict[str, Any]]:
    """Every corrections queue (`GET /corrections-queues`, optionally
    `?product=`), scoped by product the way `get_item` is (one-home-per-work-
    item, library/specs/one-home-per-work-item).

    The Library has confirmed the route's shape (2026-10-09): a 200 response
    is `{"corrections_queues": [...], "count": n}`, with an empty list (not a
    404) when the product has no queue. No response carries a `"queues"`
    key. The route still answers 404 only before the deploy that adds it
    (Library PR 1) has landed — not for "no queue matches" after that, since
    a 200 with an empty list covers that case.

    A 404 means the route does not exist yet — not that no queue matches.
    Since no item carries a queue parent before that deploy either, the two
    happen to read the same way to a caller: returns [] on a 404 rather than
    raising.

    RAISES IssueQueueUnavailable on any other failure (a 5xx, a parse
    failure, no token, or a response without a `"corrections_queues"` list)
    rather than returning [] — a caller that cannot tell those apart from
    "no queues exist" would, after the deploy, start bucketing
    queue-parented spend as if the queue id were an unlisted epic instead of
    reporting that it could not tell."""
    tok = tok or token()
    if not tok:
        raise IssueQueueUnavailable("no ops API token available")
    url = vault_plane.url("/api/corrections-queues")
    if product:
        url += "?product=" + urllib.parse.quote(product, safe="")
    try:
        req = urllib.request.Request(url, headers=vault_plane.headers(token=tok))
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return []
        raise IssueQueueUnavailable(f"could not read corrections queues: {e}") from e
    except Exception as e:
        raise IssueQueueUnavailable(f"could not read corrections queues: {e}") from e
    queues = payload.get("corrections_queues")
    if not isinstance(queues, list):
        raise IssueQueueUnavailable(
            f"corrections queue list returned no queues list: {payload!r:.200}")
    return queues


def list_ledger_entries(*, epic_id: str | None = None, item_id: str | None = None,
                        run_id: str | None = None, product: str | None = None,
                        source: str | None = None, day: str | None = None,
                        tok: str | None = None) -> list[dict[str, Any]]:
    """Every ledger entry matching the given filters (`GET /ledger-entries`,
    `library/contracts/library-protocol` §6.4 — the read side of the Bosun's
    first ledger writer, `post_ledger_entry`), following pagination to the
    end the same way `list_issues_by_state` does and for the same reason: a
    per-epic cost reconciliation that read only the first page would treat a
    partial slice of the ledger as the whole one, and a real mismatch sitting
    on a later page would read as a clean match.

    Each entry carries `run_id`, `usd`, `lane` (`gateway`, `subscription` or
    null — never a `billing_source` string; see `cost_join.reconcile_epic_cost`
    for why the two are compared without trying to share a lane vocabulary),
    `model`, `agent`, `item_id`, `epic_id` and `voided_at`.

    Uses `vault_plane.library_url`/`library_headers`, the same as
    `post_ledger_entry`: `/ledger-entries` has no Orient counterpart to
    translate from.

    RAISES IssueQueueUnavailable on any failure — the same posture as
    `get_item`: a reconciler cannot tell an empty ledger from an unreachable
    Library, and reporting a clean match while blind is worse than refusing
    to report at all."""
    tok = tok or token()
    if not tok:
        raise IssueQueueUnavailable("no ops API token available")
    params: dict[str, str] = {}
    if epic_id:
        params["epic_id"] = epic_id
    if item_id:
        params["item_id"] = item_id
    if run_id:
        params["run_id"] = run_id
    if product:
        params["product"] = product
    if source:
        params["source"] = source
    if day:
        params["day"] = day
    out: list[dict[str, Any]] = []
    offset = 0
    for _ in range(_MAX_PAGES):
        qs = urllib.parse.urlencode({**params, "offset": offset})
        url = vault_plane.library_url(f"/ledger-entries?{qs}")
        try:
            req = urllib.request.Request(url, headers=vault_plane.library_headers(token=tok))
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            raise IssueQueueUnavailable(f"could not read ledger entries: {e}") from e
        entries = payload.get("entries")
        if not isinstance(entries, list):
            raise IssueQueueUnavailable(
                f"ledger entries returned no entries list: {payload!r:.200}")
        out.extend(entries)
        if not payload.get("truncated") or not entries:
            return out
        offset += len(entries)
    raise IssueQueueUnavailable(
        f"ledger entries still report more after {_MAX_PAGES} pages ({len(out)} entries); "
        "refusing to keep paging")


def list_claimable_issues(product: str | None = None,
                          tok: str | None = None) -> list[dict[str, Any]]:
    """Issues a drain may take: open, plus those whose claim has expired.

    The expiry half matters — a co session that died holding a claim would
    otherwise strand its issue out of the queue forever. The server computes the
    predicate (state=claimable) so the TTL lives in one place; filtering
    state=open here instead is exactly how that stranding happens.

    RAISES rather than returning [] on failure, unlike the best-effort helpers
    around it. An empty list here means "the queue is empty", and a caller acts
    on that by doing nothing and reporting a quiet night. If an unreachable API
    produced the same value, an outage would be indistinguishable from an empty
    queue and the drain would report success while blind — the ARCH-311 silent
    no-op, one layer down.
    """
    tok = tok or token()
    if not tok:
        raise IssueQueueUnavailable("no ops API token available")
    url = vault_plane.url("/api/issues?state=claimable")
    if product:
        url += "&product=" + urllib.parse.quote(product, safe="")
    try:
        req = urllib.request.Request(url, headers=vault_plane.headers(token=tok))
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        raise IssueQueueUnavailable(f"could not read the issue queue: {e}") from e
    items = payload.get("items")
    if not isinstance(items, list):
        raise IssueQueueUnavailable(f"issue queue returned no items list: {payload!r:.200}")
    if not items:
        _assert_server_understands_claimable(product, tok)
    return items


def _assert_server_understands_claimable(product: str | None, tok: str) -> None:
    """Distinguish "the queue is empty" from "this server predates state=claimable".

    An orient that has not deployed the claim spine treats `claimable` as a plain
    state string, matches no row, and answers 200 with an empty list — identical
    to a genuinely empty queue. Verified against the live API on 2026-07-18: it
    returned 0 claimable while 13 issues were open.

    That is the deploy-order trap. It fails closed, so no work is duplicated, but
    it fails SILENTLY: the drain reports a clean night while the entire queue is
    invisible to it. So when the result is empty, ask a question the old server
    answers differently — if `state=open` finds rows that `claimable` did not,
    the server does not implement the filter and we must say so out loud.

    Costs one extra request, only ever on the empty path.
    """
    try:
        req = urllib.request.Request(
            vault_plane.url("/api/issues?state=open") + (
                "&product=" + urllib.parse.quote(product, safe="") if product else ""),
            headers=vault_plane.headers(token=tok))
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            open_items = json.loads(resp.read().decode("utf-8")).get("items") or []
    except Exception:
        return  # the probe is best-effort; an empty claimable result stands
    if open_items:
        raise IssueQueueUnavailable(
            f"server returned 0 claimable issues but {len(open_items)} open — it "
            "predates state=claimable (ARCH-362). Deploy the orient claim spine "
            "before relying on this gate; until then the queue is invisible here.")


def session_owner() -> str | None:
    """This session's claim owner — who to record as holding an issue.

    Every caller presents the same ops-mcp bearer token, so the server cannot
    tell one session from another; the caller names itself. CONVIVY_SESSION_DIR
    is set once per co session (agentctl session-dir), and its date-qualified
    name is stable for the session's whole life. None when unset, in which case
    the server falls back to the token identity.
    """
    d = os.environ.get("CONVIVY_SESSION_DIR", "").strip()
    if not d:
        return None
    path = d.rstrip("/")
    parent, _, name = path.rpartition("/")
    return f"{parent.rpartition('/')[2]}/{name}" if parent else name


def checkout_issue(issue_id: str, owner: str | None = None, *,
                   run_id: str | None = None,
                   tok: str | None = None) -> dict[str, Any]:
    """Check out an issue: claim it AND take the lease on the epic it sits in
    (Keel Phase 2, ARCH-424/428).

    Returns {"status": "ok"|"error:<code>", "item": {"issue", "lease", "policy"}}.

    A 409 means the subtree is held, and the error names the holder and the
    expiry — enough to route to other work and come back, rather than retry. A
    drain meeting one should move to the next item, not spin.

    `owner` names WHICH session holds it and defaults to this session's identity.
    `run_id` is provenance, so a reaper can cross-check whether the run behind a
    lease is still alive.

    **PREFER THIS OVER `claim_issue`.** Both take the lease — the old verb is not
    a way around the fence — but only this one returns it, so only this one lets
    a caller see what it holds and when that hold lapses."""
    tok = tok or token()
    if not tok:
        return {"status": "skip", "item": {}}
    payload: dict[str, Any] = {}
    owner = owner or session_owner()
    if owner:
        payload["owner"] = owner
    if run_id:
        payload["run_id"] = run_id
    return _post(_issue_path(issue_id, "checkout"), payload, tok)


def claim_issue(issue_id: str, owner: str | None = None,
                tok: str | None = None) -> dict[str, Any]:
    """Claim an issue (open → claimed) so two workers don't drain the same one.
    Returns {"status": "ok"|"error:<code>", "item": <issue>}; status error:409 means
    it is held by a live claim OR by an epic lease — skip it either way.

    `owner` names WHICH session holds it; defaults to this session's identity.

    Superseded by `checkout_issue`, and kept working rather than removed. It goes
    through the same server-side path and takes the same epic lease; what it
    cannot do is tell the caller what it now holds."""
    tok = tok or token()
    if not tok:
        return {"status": "skip", "item": {}}
    owner = owner or session_owner()
    return _post(_issue_path(issue_id, "claim"), {"owner": owner} if owner else {}, tok)


def heartbeat_issue(issue_id: str, owner: str | None = None,
                    tok: str | None = None) -> dict[str, Any]:
    """Record a sign of life on a lease this session already holds.

    Every queue action a holder takes already counts, so this is only for a long
    stretch with no queue writes. `error:404` means this session holds no live
    lease covering the item; it never takes one, because a heartbeat that could
    would be a checkout that skipped the fence."""
    tok = tok or token()
    if not tok:
        return {"status": "skip", "item": {}}
    owner = owner or session_owner()
    return _post(_issue_path(issue_id, "heartbeat"), {"owner": owner} if owner else {}, tok)


def release_issue(issue_id: str, owner: str | None = None, *, force: bool = False,
                  tok: str | None = None) -> dict[str, Any]:
    """Hand a claim back (claimed → open) at session end.

    Without this the 24h TTL is the only way out of `claimed`, so every clean
    exit would hide an issue from the drain for a day."""
    tok = tok or token()
    if not tok:
        return {"status": "skip", "item": {}}
    payload: dict[str, Any] = {}
    owner = owner or session_owner()
    if owner:
        payload["owner"] = owner
    if force:
        payload["force"] = True
    return _post(_issue_path(issue_id, "release"), payload, tok)


ISSUE_COMMENT_KINDS = ("note", "diagnosis", "question", "decline")


def comment_on_issue(issue_id: str, body: str, *, kind: str = "note",
                     run_id: str | None = None,
                     tok: str | None = None) -> dict[str, Any]:
    """Append to an issue's thread (ARCH-373).

    `kind="diagnosis"` is the one that matters operationally: it is the contract
    bug-fixer consumes, and it is a distinct kind so that contract is FINDABLE
    rather than parsed out of prose — a decline note reads a lot like a diagnosis
    to a regex.

    Append-only server-side; there is no edit or delete, because a handoff
    contract that can change under the agent already acting on it is not one.

    Best-effort like the rest of this module: a sync failure must not break the
    caller's real work.

    `{"status": "error:409"}` means the thread has hit its 200-comment cap and is
    PERMANENTLY full — not a transient failure. A caller that retries on
    `status != "ok"` would loop forever against it; open a new issue instead.
    """
    tok = tok or token()
    if not tok:
        return {"status": "skip", "item": {}}
    payload: dict[str, Any] = {"body": body, "kind": kind}
    if run_id:
        payload["run_id"] = run_id
    return _post(_issue_path(issue_id, "comments"), payload, tok)


def link_pr_to_issue(issue_id: str, pr: str, url: str | None = None,
                     tok: str | None = None) -> dict[str, Any]:
    """Record a fix PR on an issue (→ pr-open). Non-terminal — resolve it fixed when
    the PR merges."""
    tok = tok or token()
    if not tok:
        return {"status": "skip", "item": {}}
    payload: dict[str, Any] = {"pr": pr}
    if url:
        payload["url"] = url
    return _post(_issue_path(issue_id, "link-pr"), payload, tok)


def escalate_issue(issue_id: str, decision_id: str, url: str | None = None,
                   tok: str | None = None) -> dict[str, Any]:
    """Record that an issue was escalated to the decision queue (→ awaiting-decision),
    when the fix needs a human call. File the decision first, then escalate."""
    tok = tok or token()
    if not tok:
        return {"status": "skip", "item": {}}
    payload: dict[str, Any] = {"decision_id": decision_id}
    if url:
        payload["url"] = url
    return _post(_issue_path(issue_id, "escalate"), payload, tok)


def resolve_issue(issue_id: str, resolution: str, outcome_note: str | None = None,
                  fix_pr: str | None = None, tok: str | None = None) -> dict[str, Any]:
    """Resolve an issue (→ fixed | wontfix | duplicate). `outcome_note` records what
    was done; `fix_pr` links the fix PR."""
    tok = tok or token()
    if not tok:
        return {"status": "skip", "item": {}}
    payload: dict[str, Any] = {"resolution": resolution}
    if outcome_note:
        payload["outcome_note"] = outcome_note
    if fix_pr:
        payload["refs"] = {"fix_pr": fix_pr}
    return _post(_issue_path(issue_id, "resolve"), payload, tok)


def reconcile_pr(issue_id: str, outcome: str, tok: str | None = None) -> dict[str, Any]:
    """End a `pr-open` item's wait on its own linked PR, once the PR itself has
    reached an outcome `link_pr_to_issue` had no way back from (convivy/library#116).
    `outcome` is `merged` | `closed` | `abandoned` — the CALLER decides which
    applies, by polling the linked PR; this client only carries the call.

    `merged` and `closed` both return the item to `open`, keeping its `refs`
    (`pr`/`url`) as history: a merged PR that leaves work in the item's body (an
    epic's slice, say) and a closed-unmerged PR both mean nothing is still open on
    it. Neither is a resolution — the item is not `fixed`, `wontfix` or
    `duplicate`, only unstuck from `pr-open` so it is claimable again.

    `abandoned` is for a PR that is STILL OPEN. It moves nothing — an open PR
    looks exactly like one still being worked, and freeing the item would put a
    second worker on a live branch — and instead raises the item's `needs` to
    `operator` so a human sees it."""
    tok = tok or token()
    if not tok:
        return {"status": "skip", "item": {}}
    return _post(_issue_path(issue_id, "reconcile-pr"), {"outcome": outcome}, tok)


# ---------------------------------------------------------------------------
# Deprecated aliases — kept for one cycle (ARCH-361). Remove once all callers
# migrate to the issue_* names above. Each delegates; behavior cannot drift.
#
# Delegate by KEYWORD, never positionally. claim_bug originally passed `tok`
# positionally, and when claim_issue later gained an `owner` second parameter the
# token silently began arriving as the owner — a defect neither PR could see on
# its own, since one wrote the alias and the other changed the signature.
# test_deprecated_aliases_delegate_by_keyword pins this.
# ---------------------------------------------------------------------------

def list_open_bugs(product: str | None = None, tok: str | None = None) -> list[dict[str, Any]]:
    """Deprecated. Use list_open_issues."""
    warnings.warn("list_open_bugs is deprecated; use list_open_issues",
                  DeprecationWarning, stacklevel=2)
    return list_open_issues(product, tok=tok)


def claim_bug(issue_id: str, tok: str | None = None) -> dict[str, Any]:
    """Deprecated. Use claim_issue."""
    warnings.warn("claim_bug is deprecated; use claim_issue", DeprecationWarning, stacklevel=2)
    return claim_issue(issue_id, tok=tok)


def link_pr(issue_id: str, pr: str, url: str | None = None,
            tok: str | None = None) -> dict[str, Any]:
    """Deprecated. Use link_pr_to_issue."""
    warnings.warn("link_pr is deprecated; use link_pr_to_issue",
                  DeprecationWarning, stacklevel=2)
    return link_pr_to_issue(issue_id, pr, url, tok=tok)


def escalate_bug(issue_id: str, decision_id: str, url: str | None = None,
                 tok: str | None = None) -> dict[str, Any]:
    """Deprecated. Use escalate_issue."""
    warnings.warn("escalate_bug is deprecated; use escalate_issue",
                  DeprecationWarning, stacklevel=2)
    return escalate_issue(issue_id, decision_id, url, tok=tok)


def resolve_bug(issue_id: str, resolution: str, outcome_note: str | None = None,
                fix_pr: str | None = None, tok: str | None = None) -> dict[str, Any]:
    """Deprecated. Use resolve_issue."""
    warnings.warn("resolve_bug is deprecated; use resolve_issue",
                  DeprecationWarning, stacklevel=2)
    return resolve_issue(issue_id, resolution, outcome_note, fix_pr, tok=tok)
