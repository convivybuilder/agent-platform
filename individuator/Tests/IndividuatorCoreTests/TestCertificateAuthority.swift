import CryptoKit
import Foundation
@testable import IndividuatorCore

/// A throwaway X.509 certificate authority, built only for this test
/// target's fixtures. It composes `TBSCertificate` and its extensions
/// itself, from `IndividuatorCore`'s internal `DER` TLV primitives
/// (`@testable import`) — it does NOT call `CertificateSigningRequest` or
/// `CertificateAdoption`, so a test built from it is exercising
/// `CertificateAdoption`'s reading of a certificate an independent party
/// produced, not round-tripping the one encoder against itself.
struct TestCertificateAuthority {
    let key: P256.Signing.PrivateKey
    let commonName: String
    let certificateDER: Data

    var certificatePEM: String { pem(der: certificateDER, label: "CERTIFICATE") }

    private static func name(commonName: String) -> [UInt8] {
        let atv = DER.sequence([DER.oid(OID.commonName), DER.utf8String(commonName)])
        return DER.sequence([DER.set([atv])])
    }

    private static func utcTime(_ date: Date) -> [UInt8] {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyMMddHHmmss'Z'"
        formatter.timeZone = TimeZone(identifier: "UTC")
        formatter.locale = Locale(identifier: "en_US_POSIX")
        return DER.tlv(0x17, Array(formatter.string(from: date).utf8))
    }

    private static func subjectPublicKeyInfo(_ publicKey: P256.Signing.PublicKey) -> [UInt8] {
        let algorithm = DER.sequence([DER.oid(OID.ecPublicKey), DER.oid(OID.prime256v1)])
        return DER.sequence([algorithm, DER.bitString(Array(publicKey.x963Representation))])
    }

    private static func extensionEntry(oid: String, critical: Bool, value: [UInt8]) -> [UInt8] {
        var parts = [DER.oid(oid)]
        if critical { parts.append(DER.boolean(true)) }
        parts.append(DER.octetString(value))
        return DER.sequence(parts)
    }

    static func basicConstraintsCA() -> [UInt8] {
        extensionEntry(oid: "2.5.29.19", critical: true, value: DER.sequence([DER.boolean(true)]))
    }

    static func ekuExtension(oids: [String]) -> [UInt8] {
        extensionEntry(oid: OID.extendedKeyUsage, critical: false, value: DER.sequence(oids.map { DER.oid($0) }))
    }

    static func sanExtension(uris: [String]) -> [UInt8] {
        let value = DER.sequence(uris.map { DER.contextPrimitive(6, Array($0.utf8)) })
        return extensionEntry(oid: OID.subjectAltName, critical: false, value: value)
    }

    private static func tbsCertificate(
        serial: Int, issuerCN: String, subjectCN: String, publicKey: P256.Signing.PublicKey,
        notBefore: Date, notAfter: Date, extensions: [[UInt8]]
    ) -> [UInt8] {
        var fields: [[UInt8]] = [
            DER.contextConstructed(0, [DER.integer(2)]), // version v3
            DER.integer(serial),
            DER.sequence([DER.oid(OID.ecdsaWithSHA256)]),
            name(commonName: issuerCN),
            DER.sequence([utcTime(notBefore), utcTime(notAfter)]),
            name(commonName: subjectCN),
            subjectPublicKeyInfo(publicKey),
        ]
        if !extensions.isEmpty {
            fields.append(DER.contextConstructed(3, [DER.sequence(extensions)]))
        }
        return DER.sequence(fields)
    }

    private static func sign(tbs: [UInt8], with key: P256.Signing.PrivateKey) throws -> Data {
        let signature = try key.signature(for: tbs)
        return Data(DER.sequence([tbs, DER.sequence([DER.oid(OID.ecdsaWithSHA256)]),
                                   DER.bitString(Array(signature.derRepresentation))]))
    }

    /// A self-signed root with CA:true, basicConstraints critical.
    static func makeRoot(commonName: String = "test-root-ca") throws -> TestCertificateAuthority {
        let key = P256.Signing.PrivateKey()
        let now = Date()
        let tbs = tbsCertificate(
            serial: 1, issuerCN: commonName, subjectCN: commonName, publicKey: key.publicKey,
            notBefore: now.addingTimeInterval(-3600), notAfter: now.addingTimeInterval(3600 * 24 * 3650),
            extensions: [basicConstraintsCA()])
        let der = try sign(tbs: tbs, with: key)
        return TestCertificateAuthority(key: key, commonName: commonName, certificateDER: der)
    }

    /// Issues a leaf certificate under this root, with whatever extensions
    /// the caller passes directly — callers build exactly the extension
    /// set each refusal test needs (zero URI SANs, two URI SANs, no EKU at
    /// all) rather than this helper offering named shortcuts for each.
    func issueLeaf(
        serial: Int, subjectCN: String, publicKey: P256.Signing.PublicKey, extensions: [[UInt8]]
    ) throws -> (der: Data, pem: String) {
        let now = Date()
        let tbs = Self.tbsCertificate(
            serial: serial, issuerCN: commonName, subjectCN: subjectCN, publicKey: publicKey,
            notBefore: now.addingTimeInterval(-3600), notAfter: now.addingTimeInterval(3600 * 24 * 30),
            extensions: extensions)
        let der = try Self.sign(tbs: tbs, with: key)
        return (der: der, pem: pem(der: der, label: "CERTIFICATE"))
    }
}
