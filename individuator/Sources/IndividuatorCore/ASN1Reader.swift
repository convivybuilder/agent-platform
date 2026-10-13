import Foundation

/// A minimal, read-only DER walker: splits a byte run into its top-level
/// tag/length/content triples and, for a constructed tag, recurses into
/// its content. It does not validate an encoding's correctness beyond what
/// it needs to find the next TLV boundary — callers that need a specific
/// field's shape to be well-formed (an OID, a BIT STRING) check that
/// themselves.
///
/// Built for the one job `CertificateAdoption` needs done precisely rather
/// than through `SecCertificateCopyValues`'s dictionary shape (which varies
/// across macOS releases and is awkward to pin down with the exactness a
/// security check needs): finding `extensions [3]` inside a
/// `TBSCertificate` and reading the clientAuth EKU and the URI
/// subjectAltName names out of it. Certificate *chain* validation —
/// signatures, validity dates, path building — is Security's `SecTrust`,
/// not this.
struct ASN1Node {
    let tag: UInt8
    let isConstructed: Bool
    let content: [UInt8]
}

enum ASN1 {
    struct ParseError: Error, CustomStringConvertible {
        let reason: String
        var description: String { "malformed DER: \(reason)" }
    }

    static func parseOne(_ bytes: [UInt8], at offset: Int) throws -> (node: ASN1Node, next: Int) {
        guard offset < bytes.count else { throw ParseError(reason: "truncated tag") }
        let tag = bytes[offset]
        let constructed = (tag & 0x20) != 0
        var idx = offset + 1
        guard idx < bytes.count else { throw ParseError(reason: "truncated length") }
        let first = bytes[idx]
        idx += 1

        let length: Int
        if first & 0x80 == 0 {
            length = Int(first)
        } else {
            let n = Int(first & 0x7f)
            guard n != 0 else { throw ParseError(reason: "indefinite length is not permitted in DER") }
            guard n <= 8 else { throw ParseError(reason: "long-form length uses more bytes than a 64-bit length needs") }
            guard idx + n <= bytes.count else { throw ParseError(reason: "truncated long-form length") }
            let firstLengthByte = bytes[idx]
            var value = 0
            for _ in 0..<n {
                let (shifted, shiftOverflowed) = value.multipliedReportingOverflow(by: 0x100)
                guard !shiftOverflowed else { throw ParseError(reason: "length overflow") }
                let (added, addOverflowed) = shifted.addingReportingOverflow(Int(bytes[idx]))
                guard !addOverflowed else { throw ParseError(reason: "length overflow") }
                value = added
                idx += 1
            }
            // X.690 §8.1.3.5's minimal-encoding rule for a length: a value
            // under 0x80 must use short form, and the long form's leading
            // byte, which a length's sign-free encoding never needs as
            // padding, must not be zero. Catching this here, rather than
            // only bounding `value`, is what keeps a non-minimal length
            // from decoding at all instead of decoding to a hidden second
            // reading of the same bytes.
            guard value >= 0x80 else { throw ParseError(reason: "non-minimal length: value fits in short form") }
            guard firstLengthByte != 0 else { throw ParseError(reason: "non-minimal length: leading zero byte") }
            length = value
        }

        let (end, endOverflowed) = idx.addingReportingOverflow(length)
        guard !endOverflowed else { throw ParseError(reason: "length overflow") }
        guard end <= bytes.count else { throw ParseError(reason: "content runs past end") }
        let content = Array(bytes[idx..<end])
        return (ASN1Node(tag: tag, isConstructed: constructed, content: content), end)
    }

    /// Every top-level TLV in `bytes`, in order.
    static func parseAll(_ bytes: [UInt8]) throws -> [ASN1Node] {
        var nodes: [ASN1Node] = []
        var offset = 0
        while offset < bytes.count {
            let (node, next) = try parseOne(bytes, at: offset)
            nodes.append(node)
            offset = next
        }
        return nodes
    }

    static func children(of node: ASN1Node) throws -> [ASN1Node] {
        guard node.isConstructed else { throw ParseError(reason: "expected a constructed tag") }
        return try parseAll(node.content)
    }

    /// Dotted-decimal string from an OID's raw content bytes (tag and
    /// length already stripped), the inverse of `DER.oid`.
    static func oidString(_ content: [UInt8]) -> String {
        guard !content.isEmpty else { return "" }
        var arcs: [UInt64] = [UInt64(content[0] / 40), UInt64(content[0] % 40)]
        var value: UInt64 = 0
        for byte in content.dropFirst() {
            value = (value << 7) | UInt64(byte & 0x7f)
            if byte & 0x80 == 0 {
                arcs.append(value)
                value = 0
            }
        }
        return arcs.map(String.init).joined(separator: ".")
    }
}

enum CertificateExtensions {
    /// `extensions [3] EXPLICIT Extensions` (`SEQUENCE OF Extension`) out
    /// of a DER certificate's `TBSCertificate`, keyed by extension OID.
    /// Each value is the extension's raw `extnValue` OCTET STRING content
    /// (the DER of whatever that extension's own syntax is — a CSR builds
    /// the identical bytes in `ekuExtensionValue`/`sanExtensionValue`).
    static func extensions(ofCertificateDER der: [UInt8]) throws -> [String: [UInt8]] {
        let certificate = try ASN1.parseOne(der, at: 0).node
        let tbs = try ASN1.children(of: certificate).first
        guard let tbs else { throw ASN1.ParseError(reason: "empty certificate") }
        let tbsFields = try ASN1.children(of: tbs)
        // `extensions [3]` is EXPLICIT, so its tag is the context-constructed
        // tag 3 (0xa3) regardless of what it wraps; nothing else in
        // TBSCertificate carries that tag, so a tag match alone finds it
        // without needing to count past the optional fields ahead of it.
        guard let extField = tbsFields.first(where: { $0.tag == 0xa3 }) else {
            return [:]
        }
        let extensionsSeq = try ASN1.children(of: extField)
        guard let extensionsSeqNode = extensionsSeq.first else { return [:] }
        var result: [String: [UInt8]] = [:]
        for extensionNode in try ASN1.children(of: extensionsSeqNode) {
            let fields = try ASN1.children(of: extensionNode)
            guard let oidNode = fields.first, oidNode.tag == 0x06 else { continue }
            // fields[1] is the optional BOOLEAN `critical`; the extnValue
            // OCTET STRING is whichever of the remaining fields has tag 0x04.
            guard let valueNode = fields.dropFirst().first(where: { $0.tag == 0x04 }) else { continue }
            result[ASN1.oidString(oidNode.content)] = valueNode.content
        }
        return result
    }

    /// `ExtKeyUsageSyntax ::= SEQUENCE OF KeyPurposeId` from an EKU
    /// extension's `extnValue` content, as dotted-decimal OID strings.
    static func keyPurposeOIDs(ekuExtnValue: [UInt8]) throws -> [String] {
        let seq = try ASN1.parseOne(ekuExtnValue, at: 0).node
        return try ASN1.children(of: seq).filter { $0.tag == 0x06 }.map { ASN1.oidString($0.content) }
    }

    /// The `uniformResourceIdentifier [6]` `GeneralName` entries out of a
    /// `subjectAltName` extension's `extnValue` content (`GeneralNames ::=
    /// SEQUENCE OF GeneralName`). Every other `GeneralName` choice is
    /// ignored — the adoption check counts URI names specifically, the same
    /// field the Bosun's own `_SAN_URI_RE` reads.
    static func uriNames(sanExtnValue: [UInt8]) throws -> [String] {
        let seq = try ASN1.parseOne(sanExtnValue, at: 0).node
        return try ASN1.children(of: seq)
            .filter { $0.tag == 0x86 } // context-specific, primitive, tag 6
            .map { String(decoding: $0.content, as: UTF8.self) }
    }
}
