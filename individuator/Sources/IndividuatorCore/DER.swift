import Foundation

/// A minimal DER (ASN.1) encoder, built because neither Security nor
/// CryptoKit exposes a public API to build a PKCS#10 certification request
/// or an X.509 `TBSCertificate` — the two structures this package needs to
/// emit itself. It encodes only the handful of universal and
/// context-specific tags the CSR and test-fixture certificate builders use;
/// it is not a general ASN.1 library and does not parse anything (reading a
/// certificate is done through `Security`'s own `SecCertificateCopyValues`
/// in `CertificateAdoption.swift`, not through this encoder).
enum DER {
    static func length(_ n: Int) -> [UInt8] {
        if n < 0x80 { return [UInt8(n)] }
        var bytes: [UInt8] = []
        var v = n
        while v > 0 {
            bytes.insert(UInt8(v & 0xff), at: 0)
            v >>= 8
        }
        return [UInt8(0x80 | bytes.count)] + bytes
    }

    static func tlv(_ tag: UInt8, _ content: [UInt8]) -> [UInt8] {
        [tag] + length(content.count) + content
    }

    static func sequence(_ parts: [[UInt8]]) -> [UInt8] {
        tlv(0x30, parts.flatMap { $0 })
    }

    static func set(_ parts: [[UInt8]]) -> [UInt8] {
        tlv(0x31, parts.flatMap { $0 })
    }

    /// INTEGER from the minimal two's-complement-as-unsigned encoding DER
    /// requires: a leading 0x00 is prepended whenever the high bit of the
    /// first byte would otherwise be read as a sign bit, and leading zero
    /// bytes beyond that one are stripped.
    static func integer(unsignedBigEndian bytes: [UInt8]) -> [UInt8] {
        var b = bytes
        while b.count > 1 && b.first == 0x00 && (b[1] & 0x80) == 0 {
            b.removeFirst()
        }
        if b.isEmpty { b = [0x00] }
        if b[0] & 0x80 != 0 { b.insert(0x00, at: 0) }
        return tlv(0x02, b)
    }

    static func integer(_ n: Int) -> [UInt8] {
        precondition(n >= 0, "only non-negative small integers are needed here")
        var b: [UInt8] = []
        var v = n
        repeat {
            b.insert(UInt8(v & 0xff), at: 0)
            v >>= 8
        } while v > 0
        return integer(unsignedBigEndian: b)
    }

    static func boolean(_ v: Bool) -> [UInt8] {
        tlv(0x01, [v ? 0xff : 0x00])
    }

    static func bitString(_ bytes: [UInt8], unusedBits: UInt8 = 0) -> [UInt8] {
        tlv(0x03, [unusedBits] + bytes)
    }

    static func octetString(_ bytes: [UInt8]) -> [UInt8] {
        tlv(0x04, bytes)
    }

    static func null() -> [UInt8] {
        tlv(0x05, [])
    }

    /// Encodes a dotted-decimal OID string ("1.2.840.10045.2.1") per X.690
    /// §8.19: the first two arcs are combined into one byte (40*arc1 +
    /// arc2), and every later arc becomes a base-128 big-endian run with the
    /// continuation bit (0x80) set on every byte but the last.
    static func oid(_ dotted: String) -> [UInt8] {
        let arcs = dotted.split(separator: ".").compactMap { UInt64($0) }
        precondition(arcs.count >= 2, "OID needs at least two arcs")
        var body: [UInt8] = []
        body.append(UInt8(arcs[0] * 40 + arcs[1]))
        for arc in arcs.dropFirst(2) {
            var digits: [UInt8] = [UInt8(arc & 0x7f)]
            var v = arc >> 7
            while v > 0 {
                digits.insert(UInt8((v & 0x7f) | 0x80), at: 0)
                v >>= 7
            }
            body.append(contentsOf: digits)
        }
        return tlv(0x06, body)
    }

    static func utf8String(_ s: String) -> [UInt8] {
        tlv(0x0c, Array(s.utf8))
    }

    static func printableString(_ s: String) -> [UInt8] {
        tlv(0x13, Array(s.utf8))
    }

    static func ia5String(_ s: String) -> [UInt8] {
        tlv(0x16, Array(s.utf8))
    }

    /// `[n]` IMPLICIT, primitive encoding (used for GeneralName's
    /// `uniformResourceIdentifier [6] IA5String` — context-specific tag,
    /// primitive form, replacing IA5String's own tag rather than wrapping it).
    static func contextPrimitive(_ n: UInt8, _ bytes: [UInt8]) -> [UInt8] {
        tlv(0x80 | n, bytes)
    }

    /// `[n]` EXPLICIT/constructed context tag (used for the CSR's
    /// `attributes [0]` and a certificate extension's `[3] Extensions`).
    static func contextConstructed(_ n: UInt8, _ parts: [[UInt8]]) -> [UInt8] {
        tlv(0xa0 | n, parts.flatMap { $0 })
    }
}
