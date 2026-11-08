import Testing
@testable import IndividuatorNetwork

/// `ForwardingUpstreamClient.parseStatusCode` (round 6): on 403065f45 this
/// converted the response bytes to a `String` and searched for a
/// `Character` equal to "\r" or "\n". "\r\n" is one extended grapheme
/// cluster in Swift, a `Character` equal to neither on its own, so
/// `firstIndex(where:)` never matched a real CRLF line ending and every
/// well-formed HTTP response failed to parse — including CI's own
/// "HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok", received whole (40
/// bytes, the server's entire response). These feed the function the same
/// bytes directly, since reaching it through a socket only reintroduces
/// the test environment's own loopback-bind restriction.
@Suite struct ForwardingUpstreamClientParseStatusCodeTests {
    @Test func parsesARealCRLFResponse() {
        let bytes = Array("HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok".utf8)
        #expect(ForwardingUpstreamClient.parseStatusCode(bytes) == 200)
    }

    @Test func parsesAnLFOnlyResponse() {
        let bytes = Array("HTTP/1.1 404 Not Found\nContent-Length: 0\n\n".utf8)
        #expect(ForwardingUpstreamClient.parseStatusCode(bytes) == 404)
    }

    @Test(arguments: [
        Array("not an HTTP response at all\r\n".utf8),
        Array("HTTP/1.1\r\n".utf8),
        Array("HTTP/1.1 OK\r\n".utf8),
        Array("HTTP/1.1 20 OK\r\n".utf8),
        Array("HTTP/1.1 2000 OK\r\n".utf8),
        Array("HTTP/1.1 2a0 OK\r\n".utf8),
        Array("HTTP/2 200 OK\r\n".utf8),
        Array("ok".utf8),
        [],
    ])
    func refusesAMalformedStatusLine(_ bytes: [UInt8]) {
        #expect(ForwardingUpstreamClient.parseStatusCode(bytes) == nil)
    }

    // MARK: - hostile input (round 2 finding: these cases were untested)

    @Test func refusesNonASCIIBytesInTheStatusCode() {
        // The middle status-code digit replaced with a raw non-ASCII byte
        // that is not even valid UTF-8 on its own, decoding lossily rather
        // than crashing — still three `Character`s wide, so only the
        // `isASCII` check below is what refuses it.
        let bytes = Array("HTTP/1.1 2".utf8) + [0xFF] + Array("0 OK\r\n".utf8)
        #expect(ForwardingUpstreamClient.parseStatusCode(bytes) == nil)
    }

    @Test(arguments: [
        Array("HTTP/1.1 +200 OK\r\n".utf8),
        Array("HTTP/1.1 -200 OK\r\n".utf8),
    ])
    func refusesASignedStatusCode(_ bytes: [UInt8]) {
        #expect(ForwardingUpstreamClient.parseStatusCode(bytes) == nil)
    }

    @Test func refusesLeadingWhitespaceBeforeTheStatusLine() {
        // Round 2 finding: the previous `split(separator:maxSplits:)` used
        // its default `omittingEmptySubsequences: true`, which consumed a
        // leading space as a separator and dropped the empty piece it
        // left behind, so this parsed as a clean 200 rather than refusing.
        let bytes = Array(" HTTP/1.1 200 OK\r\n".utf8)
        #expect(ForwardingUpstreamClient.parseStatusCode(bytes) == nil)
    }

    @Test func refusesAStatusLineMissingTheSpaceBeforeTheCode() {
        let bytes = Array("HTTP/1.1200 OK\r\n".utf8)
        #expect(ForwardingUpstreamClient.parseStatusCode(bytes) == nil)
    }
}
