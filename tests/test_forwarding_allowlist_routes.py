"""The Individuator's forwarding allowlist
(individuator/Sources/IndividuatorDaemonCore/ForwardingAllowlist.swift)
against the routes the Python Library clients actually call (E12-2 slice
S5c; platform/decisions/0092 "The Individuator carries a sessions-only
host's clients to the Keel").

`CALL_SITE_ROUTES` below is this test's source of truth on the Python side:
one entry per (file, enclosing function) that reaches the Library, mapping
it to the route(s) it reaches. `NOT_LIBRARY_CALL_SITES` is the other half of
the same census: one entry per (file, enclosing function) that dispatches an
HTTP request to somewhere ELSE, mapping it to a one-line destination.
`_discover_call_sites()` finds the SET of call sites mechanically — every
(file, function) under `lib/` or a python-shebanged `bin/` script whose body
actually puts a request on the wire — and `test_every_discovered_call_site_
is_mapped` fails when one is a key in neither dict, so a brand new call site
with no classification is caught regardless of how it builds its URL.

Round 1 built `KNOWN_LIBRARY_ROUTES`/`CALL_SITE_ROUTES` and a hand audit.
Round 2 built the first AST walk, which found real call sites round 1's
hand audit missed in `bin/agentctl`, `bin/knowledge-ctx`, `bin/ci-result-
capture` and `bin/github-activity-capture` — but that walk required a call
site to call one of `vault_plane`'s three URL builders, or a named helper
hand-listed per file, so a client that takes its base address as a
caller-supplied PARAMETER instead (`lib/agent-platform/crew_roster.py`'s
`push_roster_name`, called from `bin/agentctl`'s plain `agentctl crew
roster` subcommand) had no `vault_plane` mention in its file at all and the
walk's own `"vault_plane" not in text` gate skipped the whole file before
ever reaching it — the exact gap round 3 (below) closes.

**Round 3 rebuilt the walk around what a call site actually DOES rather
than how it builds its URL**: it finds every (file, function) that calls
`urllib.request.urlopen`, an opener's `.open(request)`, a bare or bound
callable used as an opener (`opener(request, ...)`, `self.opener(request,
...)` — `github_app.request`'s and `crew_registration._post_crew_row`'s own
shape), an `http.client.HTTPConnection`/`HTTPSConnection` construction (not
used anywhere in this repo today, but the signature this walk watches for
in case one ever is), or the forwarding transport's own entry point
(`forwarding_transport.request`, `vault_plane._forward_via_socket`'s own
call). A call to a GENERIC dispatcher (`issue_client._post`,
`github_app.request`, `bin/mcp-ops-server`'s `_call`) is reached by walking
INTO that dispatcher, not by also flagging every caller that merely passes
it a literal path — the caller never itself puts anything on the wire, so
counting it as a separate "call site" would be counting the same dispatch
twice under two names. This is strictly MORE call sites than round 2's walk
could reach (it needs no `vault_plane` mention in the file at all), and
NEVER fewer: every route round 2's hand-kept per-file helper list named is a
generic dispatcher by this definition too, so round 2's own entries for
`issue_client._post`'s callers, `bin/mcp-ops-server`'s `_call`'s per-tool
callers, `bin/knowledge-ctx`'s `_library_post`'s callers and the rest of
that shape are folded into their one shared dispatcher's own entry, mapped
to the union of the routes its callers used it for, below — the same
"a purely-generic helper maps to the union of every route its own callers
use it for" rule round 1 already stated, just no longer duplicated onto
callers this walk does not need named individually to find them.

The map from a call site to its route(s), or to its non-Library
destination, is still hand-verified, not AST-extracted: an extractor that
does not already understand what each dispatcher's path argument carries
would either miss a route or invent one.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ALLOWLIST_SWIFT = (
    REPO / "individuator" / "Sources" / "IndividuatorDaemonCore" / "ForwardingAllowlist.swift")

# (method, path template) — the template syntax mirrors the Swift side's own
# (`{name}` for one segment, `{name...}` for a trailing multi-segment
# parameter, which may itself be followed by literal segments since E12-2
# slice S5c round 2 widened `ForwardingPathSegment`), since both describe
# the same wire shape.
_Route = tuple[str, str]

# One entry per (file relative to the repo root, enclosing function name) —
# a nested function (e.g. `deck_signature.py`'s `pending`, closed inside
# `verify`; `bin/github-activity-capture`'s `counting_opener`, closed inside
# `main`) is named by its own def, not its outer function, matching what
# `_discover_call_sites` finds. Mapped to the route(s) that call site
# reaches; a purely-generic dispatcher (one whose own path/method argument
# is itself supplied by ITS caller, e.g. `issue_client._post`,
# `github_app.request`) maps to the union of every route its own callers
# use it for — this walk finds the dispatcher, never the callers that merely
# pass it a literal, so there is one entry here per dispatcher, not one per
# caller.
CALL_SITE_ROUTES: dict[tuple[str, str], frozenset[_Route]] = {
    # --- lib/agent-platform/issue_client.py -----------------------------
    # `_post` (L46) is the shared dispatcher checkout_issue, claim_issue,
    # heartbeat_issue, release_issue, comment_on_issue, link_pr_to_issue,
    # escalate_issue, resolve_issue and reconcile_pr each call with one
    # literal action path; its own entry is the union of all nine.
    ("lib/agent-platform/issue_client.py", "_post"): frozenset({
        ("POST", "/items/{id}/checkout"), ("POST", "/items/{id}/claim"),
        ("POST", "/items/{id}/heartbeat"), ("POST", "/items/{id}/release"),
        ("POST", "/items/{id}/comments"), ("POST", "/items/{id}/link-pr"),
        ("POST", "/items/{id}/escalate"), ("POST", "/items/{id}/resolve"),
        ("POST", "/items/{id}/reconcile-pr"),
    }),
    # Every other function below dispatches directly (through
    # `urllib.request.urlopen` or `pinned_opener().open`) and builds one
    # literal route of its own.
    ("lib/agent-platform/issue_client.py", "check_merge_admission"): frozenset({("POST", "/merge/admission")}),
    ("lib/agent-platform/issue_client.py", "list_open_issues"): frozenset({("GET", "/items")}),
    ("lib/agent-platform/issue_client.py", "get_item_from_default_library"): frozenset({("GET", "/items/{id}")}),
    ("lib/agent-platform/issue_client.py", "post_review_record"): frozenset({("POST", "/review-records")}),
    ("lib/agent-platform/issue_client.py", "post_ledger_entry"): frozenset({("POST", "/ledger-entries")}),
    ("lib/agent-platform/issue_client.py", "list_issues_by_state"): frozenset({("GET", "/items")}),
    ("lib/agent-platform/issue_client.py", "get_item"): frozenset({("GET", "/items/{id}")}),
    ("lib/agent-platform/issue_client.py", "get_course"): frozenset({("GET", "/courses/{id}")}),
    ("lib/agent-platform/issue_client.py", "list_courses"): frozenset({("GET", "/courses")}),
    ("lib/agent-platform/issue_client.py", "list_corrections_queues"): frozenset({("GET", "/corrections-queues")}),
    ("lib/agent-platform/issue_client.py", "list_ledger_entries"): frozenset({("GET", "/ledger-entries")}),
    ("lib/agent-platform/issue_client.py", "list_claimable_issues"): frozenset({("GET", "/items")}),
    ("lib/agent-platform/issue_client.py", "_assert_server_understands_claimable"): frozenset({("GET", "/items")}),

    # --- lib/agent-platform/decision_client.py --------------------------
    ("lib/agent-platform/decision_client.py", "post_decision"): frozenset({("POST", "/decisions")}),
    ("lib/agent-platform/decision_client.py", "get_decision"): frozenset({("GET", "/decisions/{id}")}),
    ("lib/agent-platform/decision_client.py", "list_decisions"): frozenset({("GET", "/decisions")}),

    # --- lib/agent-platform/memory_archive.py ---------------------------
    ("lib/agent-platform/memory_archive.py", "_default_poster"): frozenset({("POST", "/signals")}),
    ("lib/agent-platform/memory_archive.py", "_default_searcher"): frozenset({("GET", "/signals/search")}),
    ("lib/agent-platform/memory_archive.py", "_default_blob_getter"): frozenset({("GET", "/signals/{id}/blob")}),

    # --- lib/agent-platform/signal_client.py ----------------------------
    ("lib/agent-platform/signal_client.py", "_post_once"): frozenset({("POST", "/signals")}),

    # --- lib/agent-platform/crew_registration.py ------------------------
    # `_post_crew_row` dispatches directly (`opener(req, ...)`) — it is the
    # ONE call site in this file; `_repo_ci_workflows` calls `GA.mint_token`/
    # `GA.request` (GitHub's own generic dispatchers, below) and dispatches
    # nothing itself.
    ("lib/agent-platform/crew_registration.py", "_post_crew_row"): frozenset({("POST", "/registry/crews")}),

    # --- lib/agent-platform/crew_roster.py -------------------------------
    # `push_roster_name` has the identical shape as `crew_doctor.py`'s
    # `_served_profile` below (a caller-supplied `base_url`, no
    # `vault_plane` mention in the file at all) — reviewer round 1 found
    # this exact route missing from the allowlist BECAUSE round 2's walk
    # could not see this call site; this is the fix.
    ("lib/agent-platform/crew_roster.py", "push_roster_name"): frozenset({("PUT", "/roster/{name}")}),

    # --- lib/agent-platform/deck_signature.py ---------------------------
    ("lib/agent-platform/deck_signature.py", "registry_active"): frozenset({("GET", "/signing-certificates/{fingerprint}")}),

    # --- lib/agent-platform/projects.py ----------------------------------
    ("lib/agent-platform/projects.py", "_fetch_registry_from_library"): frozenset({("GET", "/registry")}),

    # --- lib/agent-platform/policy_client.py ----------------------------
    ("lib/agent-platform/policy_client.py", "resolve"): frozenset({("GET", "/policy/resolve")}),

    # --- lib/knowledge/knowledge.py --------------------------------------
    ("lib/knowledge/knowledge.py", "_fetch_roster"): frozenset({("GET", "/roster")}),

    # --- lib/agent-platform/crew_doctor.py -------------------------------
    # `_served_profile` builds its request from a caller-supplied `base_url`
    # string rather than a `vault_plane` URL builder — round 2's walk could
    # not see it (its file-level gate required `vault_plane` to appear in
    # the text, and this file never mentions it); round 3's walk finds it
    # directly, by the `opener.open(req, ...)`/`urlopen(req, ...)` dispatch
    # itself. Independently also reached by `bin/knowledge-ctx`'s
    # `load_profile`, the same route.
    ("lib/agent-platform/crew_doctor.py", "_served_profile"): frozenset({("GET", "/profiles/{project}/{variant}")}),

    # --- lib/agent-platform/vault_plane.py -------------------------------
    # `_forward_via_socket` is the forwarding socket's own call site (it
    # calls `forwarding_transport.request` directly) but adds no route of
    # its own: it is installed as a handler in the SAME process-wide opener
    # every client above already uses (`_ForwardingSocketHandler`), so it
    # only ever forwards a request one of THOSE clients already built.
    # Mapped programmatically, below, to the union of every route already
    # known — never hand-copied, so it cannot drift from that set.

    # --- bin/mcp-ops-server ----------------------------------------------
    # `_call` (L161) is the shared dispatcher every tool function uses; its
    # own entry is the union of all of them.
    ("bin/mcp-ops-server", "_call"): frozenset({
        ("GET", "/signals/search"), ("GET", "/signals/{id}"), ("GET", "/signals"), ("POST", "/signals"),
        ("POST", "/items"), ("GET", "/items"), ("GET", "/items/{id}"),
        ("POST", "/items/{id}/checkout"), ("POST", "/items/{id}/release"), ("POST", "/items/{id}/heartbeat"),
        ("GET", "/leases"), ("GET", "/courses"), ("GET", "/courses/{id}"), ("POST", "/courses"),
        ("PATCH", "/courses/{id}"), ("PUT", "/courses/{id}/order"), ("POST", "/courses/{id}/supersede"),
        ("POST", "/items/{id}/claim"), ("POST", "/items/{id}/comments"), ("POST", "/items/{id}/resolve"),
        ("POST", "/items/{id}/reconcile-pr"), ("POST", "/items/{id}/link-pr"), ("POST", "/items/{id}/escalate"),
        ("POST", "/items/{id}/revision"), ("POST", "/items/{id}/prerequisites/verdict"),
        ("POST", "/items/{id}/correct"), ("POST", "/items/{id}/product"),
    }),

    # --- bin/mcp-kb-server -------------------------------------------------
    # `_library_request` (L496) is `_ensure_library_checkout`,
    # `_release_library_checkout` and `_checkin_library_checkout_if_held`'s
    # shared dispatcher; its own entry is the union of all three.
    ("bin/mcp-kb-server", "_library_request"): frozenset({
        ("GET", "/reference/documents/{doc_id...}/checkout"),
        ("POST", "/reference/documents/{doc_id...}/checkout"),
        ("POST", "/reference/documents/{doc_id...}/checkin"),
    }),

    # --- bin/issues-waiting ----------------------------------------------
    ("bin/issues-waiting", "_fetch"): frozenset({("GET", "/items")}),

    # --- bin/changelog-drain -----------------------------------------------
    # `_get` is `_pending`'s shared read dispatcher.
    ("bin/changelog-drain", "_get"): frozenset({("GET", "/signals")}),

    # --- bin/agentctl --------------------------------------------------------
    # `_library_get` and `_library_post` are this file's own shared
    # dispatchers; their own entries are the union of what their callers
    # use them for.
    ("bin/agentctl", "_library_get"): frozenset({
        ("GET", "/profiles"), ("GET", "/reference/documents"),
        ("GET", "/reference/documents/{doc_id...}"), ("GET", "/leases"),
    }),
    ("bin/agentctl", "_library_post"): frozenset({
        ("POST", "/items/{id}/checkout"), ("POST", "/items/{id}/release"), ("POST", "/reference/admission"),
    }),
    ("bin/agentctl", "_read_hard_cap_signal"): frozenset({("GET", "/signals")}),
    # `_post_review_record_to` — `IC.post_review_record` aimed at a loopback
    # stand-in (`RV.RECORD_URL_ENV`), dispatching directly through
    # `IC.pinned_opener().open(req, ...)`; found only by this round's walk
    # (round 2's required a `vault_plane` URL-builder call, and this
    # function builds its URL from its own `base` parameter instead).
    ("bin/agentctl", "_post_review_record_to"): frozenset({("POST", "/review-records")}),

    # --- bin/knowledge-ctx -------------------------------------------------
    # `_library_request` builds the request and passes it straight to
    # `_library_open` — "the one place this file touches the network, so
    # tests replace exactly it" (that function's own docstring) — which
    # calls `urllib.request.urlopen` directly. Both are discovered (the
    # first by passing a request-shaped local on to the second; the second
    # by calling `urlopen` itself), and both carry the identical route set,
    # since `_library_open` has this one caller. (`_library_post` is a thin
    # caller of `_library_request`, dispatching nothing itself.)
    ("bin/knowledge-ctx", "_library_request"): frozenset({
        ("GET", "/profiles/{project}/{variant}"), ("GET", "/profiles"),
        ("GET", "/reference/documents"), ("GET", "/reference/documents/{doc_id...}"),
        ("POST", "/bundles"), ("POST", "/bundles/redeem"),
    }),
    ("bin/knowledge-ctx", "_library_open"): frozenset({
        ("GET", "/profiles/{project}/{variant}"), ("GET", "/profiles"),
        ("GET", "/reference/documents"), ("GET", "/reference/documents/{doc_id...}"),
        ("POST", "/bundles"), ("POST", "/bundles/redeem"),
    }),

    # --- bin/ci-result-capture --------------------------------------------
    ("bin/ci-result-capture", "fetch_tracked_pairs"): frozenset({("GET", "/ci/tracked")}),

    # --- bin/github-activity-capture ---------------------------------------
    ("bin/github-activity-capture", "whoami"): frozenset({("GET", "/whoami")}),
    ("bin/github-activity-capture", "_read_ingested_urls"): frozenset({("GET", "/signals")}),

    # --- bin/trial-escaped-defects -----------------------------------------
    ("bin/trial-escaped-defects", "_default_signal_search"): frozenset({("GET", "/signals/search")}),
}

# `vault_plane._forward_via_socket` reaches the Library only by forwarding a
# request one of the clients above already built — see the comment on it,
# above. Computed here, after the dict literal, so it can only ever be the
# union of what is already there; it is never hand-copied and so can never
# drift from it.
CALL_SITE_ROUTES[("lib/agent-platform/vault_plane.py", "_forward_via_socket")] = frozenset(
    route for routes in CALL_SITE_ROUTES.values() for route in routes)

KNOWN_LIBRARY_ROUTES: frozenset[_Route] = frozenset(
    route for routes in CALL_SITE_ROUTES.values() for route in routes)

# One entry per (file, enclosing function) that dispatches an HTTP request
# somewhere OTHER than the Library — bucket (b) of the census. The value is
# a one-line note naming the real destination, read by a human, never
# machine-checked against anything: there is no allowlist on the other end
# for this test to compare it with.
NOT_LIBRARY_CALL_SITES: dict[tuple[str, str], str] = {
    # --- GitHub, through github_app.py's own shared dispatcher -----------
    ("lib/agent-platform/github_app.py", "request"): (
        "GitHub's REST/GraphQL API (api.github.com) — every GitHub App call "
        "in this repo funnels through this one dispatcher"),
    # --- GitHub, bypassing that dispatcher for a raw (non-JSON) body ------
    ("lib/agent-platform/review_launch.py", "fetch_diff"): (
        "GitHub's compare API (api.github.com), read as a raw unified diff — "
        "bypasses github_app.request, which only ever returns JSON"),
    ("bin/pr-state-capture", "_get_raw"): (
        "GitHub's artifacts API, and the presigned redirect target GitHub's "
        "own 302 points the second hop at for the actual zip bytes"),
    # --- GitHub, through a locally-defined counting wrapper around urlopen
    ("bin/github-activity-capture", "counting_opener"): (
        "GitHub's REST API, through github_app.request — this is main()'s "
        "own per-pass request counter, not a route builder of its own"),
    # --- Cloudflare Access ------------------------------------------------
    ("lib/agent-platform/access_assertion.py", "_fetch_keys"): (
        "Cloudflare Access's JWKS endpoint for the configured team domain, "
        "never the Library"),
    # --- Anthropic's Claude Code release host ------------------------------
    ("bin/pin-claude", "_fetch"): (
        "Anthropic's Claude Code release host, restricted to this script's "
        "own ALLOWED_HOSTS (claude.ai / downloads.claude.ai)"),
    # --- the local model gateway (LiteLLM) ---------------------------------
    ("bin/gateway-mint-keys", "_mint"): (
        "the local LiteLLM model gateway (http://127.0.0.1:4000 by default), "
        "never the Library"),
    # --- the Keel's bosun-server, never the Library ------------------------
    ("bin/agentctl", "cmd_bosun_revoke_machine"): (
        "the Bosun's own operator-only loopback listener (bosun-server), "
        "never the Library"),
    # --- the Library's own route, but a server-side-only call -------------
    ("lib/agent-platform/bosun_registration.py", "library_machine_verify"): (
        "the Library's own GET /machine-certificates/{fingerprint} route, "
        "but ONLY from the Individuator's mTLS registration listener on "
        "the Keel's host, which always has direct network access and is never a "
        "sessions-only client — never forwarded, so deliberately absent "
        "from the allowlist census; see this file's own module docstring"),
}

_ENTRY_RE = re.compile(
    r'ForwardingRoute\(method:\s*"([A-Z]+)",\s*path:\s*"([^"]+)"\)')


def _swift_allowlist_routes() -> set[_Route]:
    text = ALLOWLIST_SWIFT.read_text()
    # Only the entries actually inside `ForwardingAllowlist.entries`'s own
    # array literal — the file's doc comment above it also mentions a
    # `{doc_id...}` shape in prose, which this regex would otherwise match as
    # if it were a real entry.
    start = text.index("public static let entries")
    body = text[start:]
    return set(_ENTRY_RE.findall(body))


def test_the_swift_allowlist_has_every_known_route():
    """A client calls a route with no allowlist entry: the Individuator
    refuses it in production. This is the test that would have caught that
    before shipping."""
    swift_routes = _swift_allowlist_routes()
    missing = KNOWN_LIBRARY_ROUTES - swift_routes
    assert not missing, (
        f"these routes are called by a Python client but have no entry in "
        f"{ALLOWLIST_SWIFT.relative_to(REPO)}:\n  " + "\n  ".join(sorted(map(str, missing))))


def test_the_swift_allowlist_has_no_route_no_client_calls():
    """An entry no call site uses is a defect (E12-2 slice S5c task):
    it widens what a sessions-only host's forwarding socket will dial for
    no client that needs it."""
    swift_routes = _swift_allowlist_routes()
    extra = swift_routes - KNOWN_LIBRARY_ROUTES
    assert not extra, (
        f"these entries in {ALLOWLIST_SWIFT.relative_to(REPO)} match no known "
        f"Python call site:\n  " + "\n  ".join(sorted(map(str, extra))))


def test_every_known_route_destination_is_library():
    """Every entry this slice adds is `.library` — see
    `ForwardingAllowlist.swift`'s own doc comment for why there is no
    `.bosun` entry yet."""
    text = ALLOWLIST_SWIFT.read_text()
    start = text.index("public static let entries")
    body = text[start:text.index("\n    ]")]
    assert ".bosun" not in body, (
        "a .bosun-destined entry appeared; confirm bosun-server actually "
        "serves that route before allowlisting it (see the doc comment)")


# ---------------------------------------------------------------------------
# AST discovery: the SET of call sites that actually DISPATCH a request,
# mechanically — never the route (or non-Library destination) each one
# reaches, which is `CALL_SITE_ROUTES`/`NOT_LIBRARY_CALL_SITES`'s job, above.
# ---------------------------------------------------------------------------

# `http.client`'s own connection classes — not used directly anywhere in
# this repo today (every client goes through `urllib.request.urlopen` or an
# opener instead), kept so a future direct connection is still caught by
# this walk rather than silently invisible to it.
_HTTP_CLIENT_CONNECTION_ATTRS = frozenset({"HTTPConnection", "HTTPSConnection"})


def _module_aliases(tree: ast.AST, module: str) -> set[str]:
    """Every local name bound to `import module [as X]` in `tree` —
    used to recognise `forwarding_transport`'s own entry point under
    whatever alias a file imported it as (`vault_plane.py`'s `_FT`)."""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == module:
                    aliases.add(alias.asname or module)
    return aliases


def _is_request_construction(node: ast.AST) -> bool:
    """`urllib.request.Request(...)` — the only class any client in this
    repo builds a request with."""
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "Request"


def _request_bound_names(func_node: ast.AST) -> set[str]:
    """Every local name, anywhere in `func_node`'s own body (a nested def's
    body included — over-including here only widens what this walk treats
    as request-like, never narrows it, so it cannot hide a real call site),
    bound directly to a `Request(...)` call: `req = urllib.request.Request(
    ...)`."""
    names: set[str] = set()
    for node in ast.walk(func_node):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and _is_request_construction(node.value):
                names.add(target.id)
    return names


def _is_dispatch_call(call: ast.Call, ft_aliases: set[str], request_names: set[str]) -> bool:
    """Whether `call` actually puts a request on the wire — never whether it
    merely builds one, or forwards a caller-supplied method/path into a
    shared dispatcher (`issue_client._post`, `github_app.request`'s own many
    callers): those are reached by walking INTO the dispatcher they
    eventually call, not by this function flagging their callers too.

    Four shapes, every one seen somewhere in this repo except the third:
      - `urllib.request.urlopen(...)`, regardless of its argument — the
        attribute name alone is unambiguous, and every call to it in this
        repo is already `urllib.request.urlopen`.
      - `<forwarding_transport alias>.request(...)` — the forwarding
        socket's own entry point (`vault_plane._forward_via_socket`'s call).
      - `http.client.HTTPConnection(...)` / `HTTPSConnection(...)` — not
        used anywhere today; kept for a future direct connection.
      - any OTHER call — `opener.open(req, ...)`, `pinned_opener().open(req,
        ...)`, a bare `opener(req, ...)`, `self.opener(req, ...)` — whose
        first positional argument is a `Request` this function built,
        inline or through a tracked local name. The request-shaped
        argument is what marks it a dispatch, never the shape of what is
        being called — this is what lets the walk find `opener(req, ...)`
        (`github_app.request`'s own body) with no special case for "a bare
        call" versus "a method call".
    """
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr == "urlopen":
        return True
    if (isinstance(func, ast.Attribute) and func.attr == "request"
            and isinstance(func.value, ast.Name) and func.value.id in ft_aliases):
        return True
    if isinstance(func, ast.Attribute) and func.attr in _HTTP_CLIENT_CONNECTION_ATTRS:
        return True
    if call.args:
        first = call.args[0]
        if _is_request_construction(first) or (isinstance(first, ast.Name) and first.id in request_names):
            return True
    return False


def _call_sites_in_file(path: Path) -> set[str]:
    """Every enclosing function name in `path` that directly dispatches an
    HTTP request."""
    tree = ast.parse(path.read_text(), filename=str(path))
    ft_aliases = _module_aliases(tree, "forwarding_transport")
    found: set[str] = set()

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str | None] = [None]
            self.request_names_stack: list[set[str]] = [set()]

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.stack.append(node.name)
            self.request_names_stack.append(_request_bound_names(node))
            self.generic_visit(node)
            self.request_names_stack.pop()
            self.stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

        def visit_Call(self, node: ast.Call) -> None:
            enclosing = self.stack[-1]
            if enclosing is not None and _is_dispatch_call(
                    node, ft_aliases, self.request_names_stack[-1]):
                found.add(enclosing)
            self.generic_visit(node)

    _Visitor().visit(tree)
    return found


def _python_bin_scripts() -> list[Path]:
    scripts = []
    for p in (REPO / "bin").iterdir():
        if not p.is_file():
            continue
        try:
            with p.open() as f:
                first_line = f.readline()
        except OSError:
            continue
        if "python" in first_line:
            scripts.append(p)
    return scripts


def _discover_call_sites() -> set[tuple[str, str]]:
    """Every (file, enclosing function), under `lib/` or a python-shebanged
    `bin/` script, that directly dispatches an HTTP request — the file set
    is found by walking the tree, never by trusting a hand-kept list of
    files."""
    candidates = list((REPO / "lib").rglob("*.py")) + _python_bin_scripts()
    found: set[tuple[str, str]] = set()
    for path in candidates:
        rel = str(path.relative_to(REPO))
        for fn in _call_sites_in_file(path):
            found.add((rel, fn))
    return found


def test_every_discovered_call_site_is_mapped():
    """A client gains a new HTTP call with no classification in either
    `CALL_SITE_ROUTES` or `NOT_LIBRARY_CALL_SITES`: this is the test that
    catches it, regardless of which file the new call site lands in or how
    it builds its URL. Proven in E12-2 slice S5c round 3 by planting a
    function in a scratch module that calls `urlopen` on a URL built from a
    `base_url` parameter (the exact shape this round closes) and observing
    this test fail on it, and by re-running the census against round 1's
    version of `crew_roster.py`'s call site with its classification
    removed, observing the same failure the reviewer found by hand (both
    failure texts are in the slice's pull request body); both plants were
    removed before this change landed."""
    found = _discover_call_sites()
    classified = set(CALL_SITE_ROUTES) | set(NOT_LIBRARY_CALL_SITES)
    unmapped = found - classified
    assert not unmapped, (
        "these call sites dispatch an HTTP request with no classification "
        "in CALL_SITE_ROUTES or NOT_LIBRARY_CALL_SITES:\n  "
        + "\n  ".join(sorted(map(str, unmapped))))
