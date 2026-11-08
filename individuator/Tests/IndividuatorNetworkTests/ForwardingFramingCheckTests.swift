import Foundation
import Testing
@testable import IndividuatorDaemonCore

/// `ForwardingFramingCheck` (build plan slice S5a, "framing"): the
/// ambiguous-HTTP/1-framing refusals the forwarding endpoint needs because
/// `ForwardingRequestMatcher` bounds only the first request line and
/// target on a connection.
@Suite struct ForwardingFramingCheckTests {
    private static func field(_ name: String, _ value: String) -> ForwardingHeaderField {
        ForwardingHeaderField(name: name, value: value)
    }

    @Test func acceptsAnOrdinaryRequestWithNeitherHeaderAndNoTrailingBytes() {
        let head = ForwardingRequestHead(headerFields: [])
        #expect(ForwardingFramingCheck.check(head: head, trailingBytes: []) == nil)
    }

    @Test func acceptsASingleChunkedTransferEncodingWithNoContentLength() {
        let head = ForwardingRequestHead(headerFields: [Self.field("Transfer-Encoding", "chunked")])
        #expect(ForwardingFramingCheck.check(head: head, trailingBytes: []) == nil)
    }

    @Test func refusesBothTransferEncodingAndContentLength() {
        let head = ForwardingRequestHead(headerFields: [
            Self.field("Transfer-Encoding", "chunked"),
            Self.field("Content-Length", "5"),
        ])
        #expect(ForwardingFramingCheck.check(head: head, trailingBytes: []) == .transferEncodingAndContentLength)
    }

    @Test func refusesMoreThanOneContentLengthHeader() {
        let head = ForwardingRequestHead(headerFields: [
            Self.field("Content-Length", "5"),
            Self.field("Content-Length", "5"),
        ])
        #expect(ForwardingFramingCheck.check(head: head, trailingBytes: []) == .multipleContentLengthHeaders)
    }

    @Test(arguments: [
        "gzip",
        "chunked, gzip",
        "",
        "chunked, chunked",
        "gzip, chunked",
        "chunked;q=1",
    ])
    func refusesATransferEncodingOtherThanASingleChunked(value: String) {
        let head = ForwardingRequestHead(headerFields: [Self.field("Transfer-Encoding", value)])
        #expect(ForwardingFramingCheck.check(head: head, trailingBytes: []) == .transferEncodingNotSingleChunked)
    }

    @Test func acceptsATransferEncodingValueWithMixedCaseChunked() {
        let head = ForwardingRequestHead(headerFields: [Self.field("Transfer-Encoding", "Chunked")])
        #expect(ForwardingFramingCheck.check(head: head, trailingBytes: []) == nil)
    }

    @Test func acceptsAPlainContentLengthWithNoOtherHeadersAndNoTrailingBytes() {
        let head = ForwardingRequestHead(headerFields: [Self.field("Content-Length", "5")])
        #expect(ForwardingFramingCheck.check(head: head, trailingBytes: []) == nil)
    }

    @Test(arguments: [
        "4, 6",
        "5a",
        "",
        "+5",
        "-1",
        "0x10",
        // One past Int64.max — digits only, but too large to fit a
        // non-negative 64-bit integer.
        "9223372036854775808",
    ])
    func refusesAContentLengthValueThatIsNotAnExactByteCount(value: String) {
        let head = ForwardingRequestHead(headerFields: [Self.field("Content-Length", value)])
        #expect(ForwardingFramingCheck.check(head: head, trailingBytes: []) == .contentLengthNotAnExactByteCount)
    }

    @Test func refusesTwoContentLengthHeadersDifferingOnlyInNameCase() {
        let head = ForwardingRequestHead(headerFields: [
            Self.field("Content-Length", "4"),
            Self.field("CONTENT-LENGTH", "4"),
        ])
        #expect(ForwardingFramingCheck.check(head: head, trailingBytes: []) == .multipleContentLengthHeaders)
    }

    @Test func refusesAHeaderFieldNameWithWhitespaceBeforeWhereItsColonWas() {
        // "Content-Length :" (space before the colon) parses, on any
        // tolerant reading, into a field name of "Content-Length " — not
        // a valid token, and not equal to "Content-Length" either, so a
        // second length header carried this way must not slip past the
        // count unseen.
        let head = ForwardingRequestHead(headerFields: [
            Self.field("Content-Length", "4"),
            Self.field("Content-Length ", "6"),
        ])
        #expect(ForwardingFramingCheck.check(head: head, trailingBytes: []) == .malformedHeaderFieldName)
    }

    @Test func refusesAnObsFoldContinuationLineWithNoFieldNameOfItsOwn() {
        // An obs-fold continuation line carries no colon of its own; read
        // as its own field it has no name at all, which is just as
        // invalid a token as one with embedded whitespace.
        let head = ForwardingRequestHead(headerFields: [
            Self.field("Content-Length", "4"),
            Self.field("", " 6"),
        ])
        #expect(ForwardingFramingCheck.check(head: head, trailingBytes: []) == .malformedHeaderFieldName)
    }

    @Test func refusesTwoTransferEncodingHeadersEvenWhenBothSayChunked() {
        let head = ForwardingRequestHead(headerFields: [
            Self.field("Transfer-Encoding", "chunked"),
            Self.field("Transfer-Encoding", "chunked"),
        ])
        #expect(ForwardingFramingCheck.check(head: head, trailingBytes: []) == .transferEncodingNotSingleChunked)
    }

    @Test func refusesBytesAfterTheFirstCompleteRequestThatBeginASecondRequest() {
        let head = ForwardingRequestHead(headerFields: [Self.field("Content-Length", "0")])
        let secondRequestLine = Array("GET /v1/widgets/abc HTTP/1.1\r\n".utf8)
        #expect(ForwardingFramingCheck.check(head: head, trailingBytes: secondRequestLine) == .pipelinedSecondRequest)
    }

    /// Round 1 finding: a bare LF inside a header value — never part of a
    /// real CRLF line terminator, since the reader would have split on it
    /// as one if it were — is refused rather than forwarded verbatim.
    @Test func refusesABareLFInsideAHeaderValue() {
        let head = ForwardingRequestHead(headerFields: [Self.field("X-Session-Token", "a\nb")])
        #expect(ForwardingFramingCheck.check(head: head, trailingBytes: []) == .controlByteInHeaderValue)
    }

    @Test func refusesABareCRInsideAHeaderValue() {
        let head = ForwardingRequestHead(headerFields: [Self.field("X-Session-Token", "a\rb")])
        #expect(ForwardingFramingCheck.check(head: head, trailingBytes: []) == .controlByteInHeaderValue)
    }

    @Test func refusesANULByteInsideAHeaderValue() {
        let head = ForwardingRequestHead(headerFields: [Self.field("X-Session-Token", "a\u{0}b")])
        #expect(ForwardingFramingCheck.check(head: head, trailingBytes: []) == .controlByteInHeaderValue)
    }

    @Test func acceptsAHeaderValueCarryingAnHTAB() {
        let head = ForwardingRequestHead(headerFields: [Self.field("X-Session-Token", "a\tb")])
        #expect(ForwardingFramingCheck.check(head: head, trailingBytes: []) == nil)
    }

    @Test func refusesABareLFInsideTheRequestTarget() {
        let head = ForwardingRequestHead(headerFields: [])
        let target = Array("/v1/widgets/abc\nX-Client-Cert: smuggled".utf8)
        #expect(ForwardingFramingCheck.check(head: head, trailingBytes: [], target: target) == .controlByteInRequestTarget)
    }

    @Test func acceptsAnOrdinaryTargetWithNoTargetGiven() {
        // `target` defaults to empty for a caller (every other test in this
        // file) that has none to check — never itself read as a refusal.
        let head = ForwardingRequestHead(headerFields: [])
        #expect(ForwardingFramingCheck.check(head: head, trailingBytes: []) == nil)
    }

    @Test func refusesAnyTrailingBytesEvenWhenNotAWellFormedRequest() {
        // Regardless of what the trailing bytes are, a non-empty remainder
        // on a connection that should carry exactly one request is itself
        // the ambiguity 0092 refuses — this check never tries to parse it.
        let head = ForwardingRequestHead(headerFields: [])
        #expect(ForwardingFramingCheck.check(head: head, trailingBytes: [0x00, 0x01]) == .pipelinedSecondRequest)
    }
}
