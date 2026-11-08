import Foundation

/// The forwarding endpoint's fixed, in-code allowlist (0092: "It forwards a
/// fixed set of routes, and nothing else... It is a list in the
/// Individuator's code, so adding a route is a reviewed code change.").
///
/// E12·2 slice S5c: every entry below is a Library protocol route a client
/// on a sessions-only host actually calls through `vault_plane`'s
/// `url()` / `library_url()` / `fixed_library_url()`, enumerated from those
/// clients' own call sites (file and line listed in the slice's pull
/// request body). Every entry's destination is `.library` — see "What is
/// NOT here" below for why there is no `.bosun` entry.
///
/// Round 2 widened the search beyond the files round 1's hand audit named
/// — `tests/test_forwarding_allowlist_routes.py`'s own AST walk, built the
/// same round — and found real call sites round 1 missed entirely:
/// `bin/agentctl`'s own `_library_get`/`_library_post` helpers (profiles,
/// reference documents, admission), `bin/knowledge-ctx`'s bundle issue/
/// redeem and profile/document reads, `bin/ci-result-capture`'s tracked-CI
/// pairs, and `bin/github-activity-capture`'s identity check.
///
/// Round 3 rebuilt that same test's walk to find every call site that
/// actually DISPATCHES a request (`urlopen`, an opener's `.open`, the
/// forwarding transport's own entry point), rather than one that calls a
/// `vault_plane` URL builder — the walk round 2 built could not see a
/// client that takes its base address as a parameter instead, which is
/// exactly how round 2 missed `lib/agent-platform/crew_roster.py`'s
/// `push_roster_name` (added below) and `crew_doctor.py`'s own
/// `_served_profile` (already covered, by the same route `load_profile`
/// contributes). Every route below is now backed by
/// `KNOWN_LIBRARY_ROUTES`/`CALL_SITE_ROUTES` in that test file, which fails
/// when a call site and an entry here drift apart.
///
/// `GET`/`POST` routes under `/items` and `/courses` are called from BOTH
/// `lib/agent-platform/issue_client.py` (agentctl's own calls) and
/// `bin/mcp-ops-server` (the ops tool server) — one allowlist entry per
/// route covers both call sites, since the allowlist matches the request,
/// never the calling process.
///
/// **What is NOT here.** Anything under `/sessions` or `/authorize` — those are
/// the Individuator's OWN registration and per-call-authorization traffic
/// (0092 slices S1–S3), sent directly by the daemon's `BosunRegistrationClient`
/// / `BosunAuthorizationClient`, never through this forwarding socket. And
/// nothing destined `.bosun`: 0092's and 0099's "Bosun's run-register and
/// session-view paths" describe a route surface `bosun_registration.py`
/// does not yet serve — its mTLS listener answers only `POST /sessions` and
/// `POST /authorize` — and no client in this repo calls one. Adding a
/// `.bosun` entry for a route nothing serves would claim coverage this
/// slice cannot verify. Nor is `GET /machine-certificates/{fingerprint}`:
/// `lib/agent-platform/bosun_registration.py`'s `library_machine_verify`
/// calls it, but only from the Individuator's own mTLS registration
/// listener on the Keel's host, which always has direct network access
/// and is never a sessions-only client — see that module's own docstring.
public enum ForwardingAllowlist {
    public static let entries: [ForwardingRouteEntry] = [
        // --- work items ("/items"; Orient-shaped "/api/issues" on the wire
        // before translation) -----------------------------------------------
        ForwardingRouteEntry(route: ForwardingRoute(method: "GET", path: "/items"), destination: .library),
        ForwardingRouteEntry(route: ForwardingRoute(method: "POST", path: "/items"), destination: .library),
        ForwardingRouteEntry(route: ForwardingRoute(method: "GET", path: "/items/{id}"), destination: .library),
        ForwardingRouteEntry(route: ForwardingRoute(method: "POST", path: "/items/{id}/checkout"), destination: .library),
        ForwardingRouteEntry(route: ForwardingRoute(method: "POST", path: "/items/{id}/claim"), destination: .library),
        ForwardingRouteEntry(route: ForwardingRoute(method: "POST", path: "/items/{id}/heartbeat"), destination: .library),
        ForwardingRouteEntry(route: ForwardingRoute(method: "POST", path: "/items/{id}/release"), destination: .library),
        ForwardingRouteEntry(route: ForwardingRoute(method: "POST", path: "/items/{id}/comments"), destination: .library),
        ForwardingRouteEntry(route: ForwardingRoute(method: "POST", path: "/items/{id}/link-pr"), destination: .library),
        ForwardingRouteEntry(route: ForwardingRoute(method: "POST", path: "/items/{id}/escalate"), destination: .library),
        ForwardingRouteEntry(route: ForwardingRoute(method: "POST", path: "/items/{id}/resolve"), destination: .library),
        ForwardingRouteEntry(route: ForwardingRoute(method: "POST", path: "/items/{id}/reconcile-pr"), destination: .library),
        ForwardingRouteEntry(route: ForwardingRoute(method: "POST", path: "/items/{id}/revision"), destination: .library),
        ForwardingRouteEntry(route: ForwardingRoute(method: "POST", path: "/items/{id}/prerequisites/verdict"), destination: .library),
        ForwardingRouteEntry(route: ForwardingRoute(method: "POST", path: "/items/{id}/correct"), destination: .library),
        ForwardingRouteEntry(route: ForwardingRoute(method: "POST", path: "/items/{id}/product"), destination: .library),

        // --- Courses -----------------------------------------------------
        ForwardingRouteEntry(route: ForwardingRoute(method: "GET", path: "/courses"), destination: .library),
        ForwardingRouteEntry(route: ForwardingRoute(method: "POST", path: "/courses"), destination: .library),
        ForwardingRouteEntry(route: ForwardingRoute(method: "GET", path: "/courses/{id}"), destination: .library),
        ForwardingRouteEntry(route: ForwardingRoute(method: "PATCH", path: "/courses/{id}"), destination: .library),
        ForwardingRouteEntry(route: ForwardingRoute(method: "PUT", path: "/courses/{id}/order"), destination: .library),
        ForwardingRouteEntry(route: ForwardingRoute(method: "POST", path: "/courses/{id}/supersede"), destination: .library),

        // --- corrections queues, leases, the registry and roster ---------
        ForwardingRouteEntry(route: ForwardingRoute(method: "GET", path: "/corrections-queues"), destination: .library),
        ForwardingRouteEntry(route: ForwardingRoute(method: "GET", path: "/leases"), destination: .library),
        ForwardingRouteEntry(route: ForwardingRoute(method: "GET", path: "/registry"), destination: .library),
        ForwardingRouteEntry(route: ForwardingRoute(method: "POST", path: "/registry/crews"), destination: .library),
        ForwardingRouteEntry(route: ForwardingRoute(method: "GET", path: "/roster"), destination: .library),
        ForwardingRouteEntry(route: ForwardingRoute(method: "PUT", path: "/roster/{name}"), destination: .library),

        // --- signals -------------------------------------------------------
        ForwardingRouteEntry(route: ForwardingRoute(method: "GET", path: "/signals"), destination: .library),
        ForwardingRouteEntry(route: ForwardingRoute(method: "POST", path: "/signals"), destination: .library),
        ForwardingRouteEntry(route: ForwardingRoute(method: "GET", path: "/signals/search"), destination: .library),
        ForwardingRouteEntry(route: ForwardingRoute(method: "GET", path: "/signals/{id}"), destination: .library),
        ForwardingRouteEntry(route: ForwardingRoute(method: "GET", path: "/signals/{id}/blob"), destination: .library),

        // --- decisions -----------------------------------------------------
        ForwardingRouteEntry(route: ForwardingRoute(method: "GET", path: "/decisions"), destination: .library),
        ForwardingRouteEntry(route: ForwardingRoute(method: "POST", path: "/decisions"), destination: .library),
        ForwardingRouteEntry(route: ForwardingRoute(method: "GET", path: "/decisions/{id}"), destination: .library),

        // --- the ledger ------------------------------------------------------
        ForwardingRouteEntry(route: ForwardingRoute(method: "GET", path: "/ledger-entries"), destination: .library),
        ForwardingRouteEntry(route: ForwardingRoute(method: "POST", path: "/ledger-entries"), destination: .library),

        // --- fixed-address routes (vault_plane.fixed_library_url, through
        // issue_client.pinned_opener) ----------------------------------------
        ForwardingRouteEntry(route: ForwardingRoute(method: "POST", path: "/merge/admission"), destination: .library),
        ForwardingRouteEntry(route: ForwardingRoute(method: "POST", path: "/review-records"), destination: .library),
        ForwardingRouteEntry(route: ForwardingRoute(method: "GET", path: "/policy/resolve"), destination: .library),

        // --- machine and signing certificates --------------------------------
        ForwardingRouteEntry(route: ForwardingRoute(method: "GET", path: "/signing-certificates/{fingerprint}"), destination: .library),

        // --- the Reference Room: listing, reading one document, and the
        // checkout/checkin clause (bin/agentctl's `_library_get`/
        // `_verify_grant_document`/`_library_agent`, bin/knowledge-ctx's
        // `_library_reference_rows`/`collect_matches`, bin/mcp-kb-server's
        // `_library_request` through `_ensure_library_checkout`/
        // `_release_library_checkout`) — `doc_id` is itself multiple path
        // segments (quoted with `safe="/"`; a real id looks like
        // `agent-platform/architecture/e12-2-individuator-build-plan`), so
        // the checkout/checkin shape (a literal suffix after the id) needed
        // `ForwardingPathSegment.trailingMultiSegmentParameter` widened
        // (E12-2 slice S5c round 2) to express; the bare read below needed
        // no widening, only an entry ---------------------------------------
        ForwardingRouteEntry(route: ForwardingRoute(method: "GET", path: "/reference/documents"), destination: .library),
        ForwardingRouteEntry(route: ForwardingRoute(method: "GET", path: "/reference/documents/{doc_id...}"), destination: .library),
        ForwardingRouteEntry(route: ForwardingRoute(method: "GET", path: "/reference/documents/{doc_id...}/checkout"), destination: .library),
        ForwardingRouteEntry(route: ForwardingRoute(method: "POST", path: "/reference/documents/{doc_id...}/checkout"), destination: .library),
        ForwardingRouteEntry(route: ForwardingRoute(method: "POST", path: "/reference/documents/{doc_id...}/checkin"), destination: .library),
        ForwardingRouteEntry(route: ForwardingRoute(method: "POST", path: "/reference/admission"), destination: .library),

        // --- profiles (bin/agentctl's `_library_profile_variants`,
        // bin/knowledge-ctx's `load_profile`/`_library_profile_variants`,
        // and `crew_doctor.py`'s own `_served_profile`, which builds its
        // request from a caller-supplied `base_url` rather than a
        // `vault_plane` URL builder) ------------------------------------
        ForwardingRouteEntry(route: ForwardingRoute(method: "GET", path: "/profiles"), destination: .library),
        ForwardingRouteEntry(route: ForwardingRoute(method: "GET", path: "/profiles/{project}/{variant}"), destination: .library),

        // --- bundles (bin/knowledge-ctx's `fetch_bundle`) -------------------
        ForwardingRouteEntry(route: ForwardingRoute(method: "POST", path: "/bundles"), destination: .library),
        ForwardingRouteEntry(route: ForwardingRoute(method: "POST", path: "/bundles/redeem"), destination: .library),

        // --- CI tracking and identity (bin/ci-result-capture's
        // `fetch_tracked_pairs`, bin/github-activity-capture's `whoami`) ----
        ForwardingRouteEntry(route: ForwardingRoute(method: "GET", path: "/ci/tracked"), destination: .library),
        ForwardingRouteEntry(route: ForwardingRoute(method: "GET", path: "/whoami"), destination: .library),
    ]
}
