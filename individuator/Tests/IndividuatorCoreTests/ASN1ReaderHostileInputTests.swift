import Foundation
import Testing
@testable import IndividuatorCore

/// `adopt-cert` and `individuator status` hand this reader whatever bytes
/// are in a file on disk — a certificate the Library issued, or (until
/// adoption succeeds) whatever is sitting at that path. Every case here is
/// a byte sequence the pre-fix reader either trapped on or decoded wrongly;
/// `refusesASignBitLongFormLength` below is the crash from the finding
/// itself (a `fatalError`-style Swift trap building a `Range` with its
/// upper bound below its lower bound, from a long-form length whose bytes
/// read back as a negative `Int`), reproduced directly against
/// `ASN1.parseOne` with no certificate structure around it.
@Suite struct ASN1ReaderHostileInputTests {
    private func assertRefusesWithoutCrashing(_ bytes: [UInt8]) {
        #expect(throws: ASN1.ParseError.self) {
            _ = try ASN1.parseOne(bytes, at: 0)
        }
    }

    @Test func refusesASignBitLongFormLength() {
        // tag OCTET STRING, long-form length with 8 bytes whose leading
        // byte is 0x80: as a plain 64-bit load that is `Int.min`'s bit
        // pattern, which the pre-fix reader added to `idx` and handed
        // straight to `bytes[idx..<(idx + length)]`.
        assertRefusesWithoutCrashing([0x04, 0x88, 0x80, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
    }

    @Test func refusesALengthLongerThanTheInput() {
        assertRefusesWithoutCrashing([0x04, 0x7f, 0x01, 0x02]) // declares 127 bytes, has 2
    }

    @Test func refusesEveryTruncationOfAValidLongFormTLV() {
        // 200 bytes of content forces a long-form (2-byte) length, so this
        // truncation table walks the long-form length-byte loop as well as
        // the content-bounds check.
        let full = DER.tlv(0x04, Array(repeating: UInt8(0x41), count: 200))
        for end in 0..<full.count {
            let truncated = Array(full[0..<end])
            #expect(throws: ASN1.ParseError.self) {
                _ = try ASN1.parseOne(truncated, at: 0)
            }
        }
    }

    @Test func refusesIndefiniteLength() {
        // 0x80 alone, in the length position, is BER's indefinite-length
        // marker. DER forbids it; the pre-fix reader read `n = 0`,
        // skipped the length loop, and silently decoded it as length 0.
        assertRefusesWithoutCrashing([0x30, 0x80, 0x01, 0x02, 0x00, 0x00])
    }

    @Test func refusesMoreLengthBytesThanAnIntNeeds() {
        assertRefusesWithoutCrashing([0x04, 0x89] + Array(repeating: UInt8(0x01), count: 9)) // n = 9
    }

    @Test func refusesANonMinimalShortFormableLength() {
        // 5 fits in a single short-form byte; DER forbids spending a
        // long-form length on it.
        assertRefusesWithoutCrashing([0x04, 0x81, 0x05, 0x00, 0x00, 0x00, 0x00, 0x00])
    }

    @Test func refusesALeadingZeroLongFormLength() {
        assertRefusesWithoutCrashing([0x04, 0x82, 0x00, 0xff] + Array(repeating: UInt8(0), count: 255))
    }

    @Test func refusesALengthThatOverflowsAcrossSeveralShifts() {
        // Every byte is non-zero, so no single byte is individually
        // damning the way the sign-bit case above is; the overflow lands
        // partway through the multiply/add loop instead of on the first
        // iteration.
        assertRefusesWithoutCrashing([0x04, 0x88, 0x7f, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff])
    }

    @Test func deeplyNestedWrappingDoesNotCrashTheTopLevelParse() throws {
        // `ASN1.parseOne` itself never recurses into a constructed tag's
        // content (only an explicit `children(of:)` call does that, and
        // `CertificateExtensions` calls it a fixed number of times, not
        // once per nesting level) — so this is a size/length stress on the
        // outermost TLV, not a stack-depth one, and is expected to parse
        // cleanly rather than refuse.
        var bytes: [UInt8] = []
        for _ in 0..<2000 {
            bytes = DER.tlv(0x30, bytes)
        }
        let node = try ASN1.parseOne(bytes, at: 0).node
        #expect(node.isConstructed)
    }
}
