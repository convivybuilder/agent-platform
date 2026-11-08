import Testing
@testable import IndividuatorNetwork

/// `ForwardingChunkSizeParser`'s successful-parse cases (round 4 review
/// coverage finding). The malformed cases are already pinned end to end
/// through `ForwardingConnectionHandlerTests
/// .refusesAHostileChunkSizeLineInARequestBody`; this file covers the
/// shapes `parse` must accept rather than refuse.
@Suite struct ForwardingChunkSizeParserTests {
    @Test func parsesUpperCaseHexDigits() {
        #expect(ForwardingChunkSizeParser.parse(Array("1A".utf8)) == .size(0x1A))
        #expect(ForwardingChunkSizeParser.parse(Array("FF".utf8)) == .size(0xFF))
    }

    @Test func parsesASizeWithAChunkExtension() {
        #expect(ForwardingChunkSizeParser.parse(Array("a;name=value".utf8)) == .size(0xA))
    }
}
