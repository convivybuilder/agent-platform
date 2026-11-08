import Foundation
import Testing
@testable import IndividuatorDaemonCore

/// `ForwardingRequestMatcher` (build plan slice S5a) carries every test
/// 0092's Handoff names for the forwarding endpoint's matching rule. The
/// allowlist below is the task's own synthetic example, never the real
/// route list (slice S5c).
@Suite struct ForwardingRequestMatcherTests {
    static let allowlist = [
        ForwardingRoute(method: "GET", path: "/v1/widgets/{id}"),
        ForwardingRoute(method: "POST", path: "/v1/widgets/{id}/parts/{rest...}"),
    ]

    private static func match(method: String = "GET", _ target: String) -> Result<ForwardingMatch, ForwardingRefusalReason> {
        ForwardingRequestMatcher.match(method: method, target: Array(target.utf8), allowlist: allowlist)
    }

    @Test(arguments: [
        "/v1/widgets/abc",
        "/v1/widgets/abc?x=1&y=2",
    ])
    func allowedPathIsForwardedByteForByte(target: String) {
        switch Self.match(target) {
        case .success(let m):
            #expect(m.target == Array(target.utf8))
            #expect(m.route.method == "GET")
        case .failure(let reason):
            Issue.record("expected a match, got \(reason)")
        }
    }

    @Test(arguments: [
        ("/v1/widgets/..", ForwardingRefusalReason.dotDotSegment),
        ("/v1/widgets/.", ForwardingRefusalReason.dotSegment),
        ("/v1/widgets//abc", ForwardingRefusalReason.emptyPathSegment),
        ("/v1/widgets/abc/", ForwardingRefusalReason.emptyPathSegment),
        ("/v1/widgets/a\\b", ForwardingRefusalReason.backslashInPath),
        ("/v1/widgets/a%2fb", ForwardingRefusalReason.percentEncodedSlashInPath),
        ("/v1/widgets/a%2Fb", ForwardingRefusalReason.percentEncodedSlashInPath),
        ("/v1/widgets/a%2eb", ForwardingRefusalReason.percentEncodedDotInPath),
        ("/v1/widgets/a%2Eb", ForwardingRefusalReason.percentEncodedDotInPath),
        ("/v1/widgets/a%5cb", ForwardingRefusalReason.percentEncodedBackslashInPath),
        ("/v1/widgets/a%5Cb", ForwardingRefusalReason.percentEncodedBackslashInPath),
        ("/v1/widgets/abc#frag", ForwardingRefusalReason.fragmentPresent),
        ("http://evil.example.com/v1/widgets/abc", ForwardingRefusalReason.targetNotOriginForm),
        ("evil.example.com:443", ForwardingRefusalReason.targetNotOriginForm),
        ("*", ForwardingRefusalReason.targetNotOriginForm),
        ("/v1/unknown/abc", ForwardingRefusalReason.noMatchingRoute),
        ("/v1/widgets/a%20b", ForwardingRefusalReason.parameterCharacterClassViolation),
    ])
    func refusesEachNamedCase(target: String, reason: ForwardingRefusalReason) {
        #expect(Self.match(target) == .failure(reason))
    }

    @Test func refusesCONNECTRegardlessOfItsTarget() {
        #expect(Self.match(method: "CONNECT", "evil.example.com:443") == .failure(.connectMethod))
    }

    @Test func refusesAListedPathCalledWithAnotherMethod() {
        #expect(Self.match(method: "DELETE", "/v1/widgets/abc") == .failure(.methodNotAllowedForPath))
    }

    @Test func trailingMultiSegmentParameterAcceptsSeveralCleanSegments() {
        switch Self.match(method: "POST", "/v1/widgets/abc/parts/p1/p2/p3") {
        case .success(let m):
            #expect(m.target == Array("/v1/widgets/abc/parts/p1/p2/p3".utf8))
            #expect(m.route.method == "POST")
        case .failure(let reason):
            Issue.record("expected a match, got \(reason)")
        }
    }

    @Test func trailingMultiSegmentParameterRefusesWhenAnySegmentBreaksTheRule() {
        #expect(
            Self.match(method: "POST", "/v1/widgets/abc/parts/p1/p2 bad/p3")
                == .failure(.parameterCharacterClassViolation))
    }

    // --- a multi-segment parameter followed by a literal suffix (slice
    // S5c; the `{doc_id...}/checkout` shape mcp-kb-server needs) ----------

    static let suffixedAllowlist = [
        ForwardingRoute(method: "GET", path: "/reference/documents/{doc_id...}/checkout"),
    ]

    private static func matchSuffixed(
        method: String = "GET", _ target: String
    ) -> Result<ForwardingMatch, ForwardingRefusalReason> {
        ForwardingRequestMatcher.match(method: method, target: Array(target.utf8), allowlist: suffixedAllowlist)
    }

    @Test func multiSegmentParameterFollowedByLiteralSuffixMatches() {
        switch Self.matchSuffixed("/reference/documents/a/b/c/checkout") {
        case .success(let m):
            #expect(m.target == Array("/reference/documents/a/b/c/checkout".utf8))
        case .failure(let reason):
            Issue.record("expected a match, got \(reason)")
        }
    }

    @Test func multiSegmentParameterFollowedByLiteralSuffixRefusesAMissingSuffix() {
        #expect(Self.matchSuffixed("/reference/documents/a/b/c") == .failure(.noMatchingRoute))
    }

    @Test func multiSegmentParameterFollowedByLiteralSuffixRefusesAnExtraTrailingSegment() {
        #expect(Self.matchSuffixed("/reference/documents/a/b/checkout/extra") == .failure(.noMatchingRoute))
    }

    @Test func multiSegmentParameterFollowedByLiteralSuffixMatchesWhenTheSuffixTextAppearsInsideTheParameter() {
        // The doc_id segment is itself literally "checkout", followed by the
        // real suffix — the parameter consumes only one segment and the
        // final literal segment still matches.
        switch Self.matchSuffixed("/reference/documents/checkout/checkout") {
        case .success(let m):
            #expect(m.target == Array("/reference/documents/checkout/checkout".utf8))
        case .failure(let reason):
            Issue.record("expected a match, got \(reason)")
        }
    }

    @Test func multiSegmentParameterFollowedByLiteralSuffixRefusesAnEmptyParameter() {
        #expect(Self.matchSuffixed("/reference/documents/checkout") == .failure(.noMatchingRoute))
    }

    @Test(arguments: [
        ("/reference/documents/a/../checkout", ForwardingRefusalReason.dotDotSegment),
        ("/reference/documents/a/./checkout", ForwardingRefusalReason.dotSegment),
        ("/reference/documents/a//checkout", ForwardingRefusalReason.emptyPathSegment),
        ("/reference/documents/a/b/checkout/", ForwardingRefusalReason.emptyPathSegment),
        ("/reference/documents/a\\b/checkout", ForwardingRefusalReason.backslashInPath),
        ("/reference/documents/a%2fb/checkout", ForwardingRefusalReason.percentEncodedSlashInPath),
        ("/reference/documents/a%2Fb/checkout", ForwardingRefusalReason.percentEncodedSlashInPath),
        ("/reference/documents/a%2eb/checkout", ForwardingRefusalReason.percentEncodedDotInPath),
        ("/reference/documents/a%2Eb/checkout", ForwardingRefusalReason.percentEncodedDotInPath),
        ("/reference/documents/a%5cb/checkout", ForwardingRefusalReason.percentEncodedBackslashInPath),
        ("/reference/documents/a%5Cb/checkout", ForwardingRefusalReason.percentEncodedBackslashInPath),
    ])
    func multiSegmentParameterFollowedByLiteralSuffixRefusesEachTraversalForm(
        target: String, reason: ForwardingRefusalReason
    ) {
        #expect(Self.matchSuffixed(target) == .failure(reason))
    }

    @Test func queryStringIsPassedThroughUnreadAndNeverMatched() {
        // A query string holding bytes the path rule would refuse (here, a
        // '..' token) still matches, because 0092 states the query string
        // is "passed through and never matched" — only the path is read.
        let target = "/v1/widgets/abc?next=../../etc/passwd"
        switch Self.match(target) {
        case .success(let m):
            #expect(m.target == Array(target.utf8))
        case .failure(let reason):
            Issue.record("expected the query string to be passed through unmatched, got \(reason)")
        }
    }

    // --- shadowing against the REAL allowlist (security review, E12-2
    // slice S5c round 3): a bare `{doc_id...}` read and the more specific
    // `{doc_id...}/checkout` entry both shape-match a checkout path, and
    // the more specific one must win regardless of which comes first in
    // `ForwardingAllowlist.entries`, so the daemon's audit log attributes
    // the call to the entry the request actually means. ------------------

    @Test func theMostSpecificEntryWinsOverABareMultiSegmentParameterReadInTheRealAllowlist() {
        let routes = ForwardingAllowlist.entries.map(\.route)
        let result = ForwardingRequestMatcher.match(
            method: "GET", target: Array("/reference/documents/a/b/checkout".utf8), allowlist: routes)
        switch result {
        case .success(let m):
            #expect(m.route == ForwardingRoute(method: "GET", path: "/reference/documents/{doc_id...}/checkout"))
        case .failure(let reason):
            Issue.record("expected the checkout entry to match, got \(reason)")
        }
    }

    @Test func aBareMultiSegmentParameterEntryStillWinsWhenNoMoreSpecificEntryShapeMatches() {
        // The ordinary (non-checkout) document read still resolves to the
        // bare `{doc_id...}` entry — the fix above picks the MOST SPECIFIC
        // of the entries that actually shape-match, never an entry that
        // does not.
        let routes = ForwardingAllowlist.entries.map(\.route)
        let result = ForwardingRequestMatcher.match(
            method: "GET", target: Array("/reference/documents/a/b/c".utf8), allowlist: routes)
        switch result {
        case .success(let m):
            #expect(m.route == ForwardingRoute(method: "GET", path: "/reference/documents/{doc_id...}"))
        case .failure(let reason):
            Issue.record("expected the bare document-read entry to match, got \(reason)")
        }
    }

    @Test func tiedSpecificityKeepsTheFirstAllowlistEntry() {
        // Two entries of equal literal-segment specificity that both
        // shape-match: the earlier one in allowlist order wins,
        // deterministically, never whichever Swift's `max` happens to
        // visit last.
        let allowlist = [
            ForwardingRoute(method: "GET", path: "/v1/{a}/x"),
            ForwardingRoute(method: "GET", path: "/v1/y/{b}"),
        ]
        let result = ForwardingRequestMatcher.match(
            method: "GET", target: Array("/v1/y/x".utf8), allowlist: allowlist)
        switch result {
        case .success(let m):
            #expect(m.route == allowlist[0])
        case .failure(let reason):
            Issue.record("expected the first tied entry to win, got \(reason)")
        }
    }
}
