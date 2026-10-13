import CryptoKit
import Foundation
import Testing
@testable import IndividuatorCore

/// The finding's own scenario, reproduced inside an actual certificate
/// rather than a bare TLV: a leaf certificate whose extended-key-usage
/// extension's `extnValue` OCTET STRING carries the sign-bit long-form
/// length from `ASN1ReaderHostileInputTests.refusesASignBitLongFormLength`.
/// `TestCertificateAuthority.issueLeaf` takes already-encoded `Extension`
/// SEQUENCEs directly, so the malicious bytes go in as one of those,
/// leaving every structural layer above it (`Extensions`, `TBSCertificate`,
/// `Certificate`) counted correctly by `DER.sequence`'s own byte-count
/// arithmetic — the malformed length is reachable only once something
/// tries to read what it claims to bound, which is exactly what
/// `CertificateExtensions.extensions(ofCertificateDER:)`'s walk into the
/// extension's own fields does.
@Suite struct CertificateExtensionsHostileInputTests {
    private static let maliciousExtnValueTLV: [UInt8] =
        [0x04, 0x88, 0x80, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]

    private func issueLeafWithMaliciousEKU(
        ca: TestCertificateAuthority, key: SoftwareMachineKey, serial: Int
    ) throws -> (der: Data, pem: String) {
        let maliciousEKU = DER.sequence([DER.oid(OID.extendedKeyUsage), Self.maliciousExtnValueTLV])
        return try ca.issueLeaf(
            serial: serial, subjectCN: "synthetic-mac-hostile", publicKey: key.publicKey,
            extensions: [maliciousEKU, TestCertificateAuthority.sanExtension(uris: ["convivy://machine/x"])])
    }

    @Test func extensionsRefusesRatherThanCrashingOnAMaliciousEKULength() throws {
        let ca = try TestCertificateAuthority.makeRoot()
        let leaf = try issueLeafWithMaliciousEKU(ca: ca, key: SoftwareMachineKey(), serial: 100)
        #expect(throws: ASN1.ParseError.self) {
            _ = try CertificateExtensions.extensions(ofCertificateDER: Array(leaf.der))
        }
    }

    /// `individuator status` reads a stored certificate straight off disk
    /// through this call, with no `SecCertificateCreateWithData` check in
    /// front of it (see `CertificateInspection.swift`) — the one path in
    /// this package where a malformed certificate file reaches
    /// `ASN1Reader` with no other parser's framing check ahead of it.
    @Test func inspectionRefusesRatherThanCrashingOnAMaliciousEKULength() throws {
        let ca = try TestCertificateAuthority.makeRoot()
        let leaf = try issueLeafWithMaliciousEKU(ca: ca, key: SoftwareMachineKey(), serial: 101)
        #expect(throws: (any Error).self) {
            _ = try CertificateInspection.uriNames(ofCertificatePEM: leaf.pem)
        }
    }

    /// `adopt-cert`'s own call, end to end. In practice `SecCertificateCreateWithData`
    /// rejects this particular malformed structure outright, before this
    /// package's own reader is reached at all (confirmed by running this
    /// test against the pre-fix reader: it already passed, unlike the two
    /// tests above) — a defense-in-depth property of `adopt`'s own
    /// ordering, not of this fix. Kept here because that ordering is not
    /// guaranteed for every malformed shape `SecCertificateCreateWithData`
    /// might accept, and the end-to-end call must stay crash-free either
    /// way.
    @Test func adoptionRefusesRatherThanCrashingOnAMaliciousEKULength() throws {
        let ca = try TestCertificateAuthority.makeRoot()
        let key = SoftwareMachineKey()
        let leaf = try issueLeafWithMaliciousEKU(ca: ca, key: key, serial: 102)

        #expect(throws: AdoptionRefusal.self) {
            _ = try CertificateAdoption.adopt(certificatePEM: leaf.pem, rootPEM: ca.certificatePEM, machineKey: key)
        }
    }
}
