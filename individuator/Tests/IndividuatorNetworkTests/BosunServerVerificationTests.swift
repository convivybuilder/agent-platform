import Foundation
import Testing
@testable import IndividuatorCore
@testable import IndividuatorCoreTests

/// The four cases the work item's test matrix names for the Individuator's
/// own check of the Bosun's server certificate: success; a server
/// certificate with the wrong URI name; one without serverAuth; and one
/// chained to a foreign root.
@Suite struct BosunServerVerificationTests {
    private func serverExtensions(serverAuth: Bool, uris: [String]) -> [[UInt8]] {
        var exts: [[UInt8]] = []
        if serverAuth {
            exts.append(TestCertificateAuthority.ekuExtension(oids: [OID.serverAuth]))
        }
        if !uris.isEmpty {
            exts.append(TestCertificateAuthority.sanExtension(uris: uris))
        }
        return exts
    }

    @Test func acceptsACorrectServerCertificate() throws {
        let ca = try TestCertificateAuthority.makeRoot()
        let serverKey = SoftwareMachineKey()
        let leaf = try ca.issueLeaf(
            serial: 10, subjectCN: "bosun-test", publicKey: serverKey.publicKey,
            extensions: serverExtensions(serverAuth: true, uris: ["convivy://machine/bosun-test"]))

        try BosunServerVerification.verify(
            leafCertificateDER: leaf.der, rootPEM: ca.certificatePEM, expectedBosunURIName: "convivy://machine/bosun-test")
    }

    @Test func refusesAWrongURIName() throws {
        let ca = try TestCertificateAuthority.makeRoot()
        let serverKey = SoftwareMachineKey()
        let leaf = try ca.issueLeaf(
            serial: 11, subjectCN: "bosun-test", publicKey: serverKey.publicKey,
            extensions: serverExtensions(serverAuth: true, uris: ["convivy://machine/some-other-host"]))

        #expect(throws: BosunServerRefusal.self) {
            try BosunServerVerification.verify(
                leafCertificateDER: leaf.der, rootPEM: ca.certificatePEM,
                expectedBosunURIName: "convivy://machine/bosun-test")
        }
        do {
            try BosunServerVerification.verify(
                leafCertificateDER: leaf.der, rootPEM: ca.certificatePEM,
                expectedBosunURIName: "convivy://machine/bosun-test")
            Issue.record("expected a refusal")
        } catch let refusal as BosunServerRefusal {
            guard case .uriNameMismatch = refusal.reason else {
                Issue.record("wrong reason: \(refusal.reason)")
                return
            }
        }
    }

    @Test func refusesAMissingServerAuth() throws {
        let ca = try TestCertificateAuthority.makeRoot()
        let serverKey = SoftwareMachineKey()
        let leaf = try ca.issueLeaf(
            serial: 12, subjectCN: "bosun-test", publicKey: serverKey.publicKey,
            extensions: serverExtensions(serverAuth: false, uris: ["convivy://machine/bosun-test"]))

        do {
            try BosunServerVerification.verify(
                leafCertificateDER: leaf.der, rootPEM: ca.certificatePEM,
                expectedBosunURIName: "convivy://machine/bosun-test")
            Issue.record("expected a refusal")
        } catch let refusal as BosunServerRefusal {
            guard case .missingServerAuth = refusal.reason else {
                Issue.record("wrong reason: \(refusal.reason)")
                return
            }
        }
    }

    @Test func refusesAForeignRoot() throws {
        let ca = try TestCertificateAuthority.makeRoot()
        let foreignRoot = try TestCertificateAuthority.makeRoot(commonName: "a-different-root")
        let serverKey = SoftwareMachineKey()
        let leaf = try ca.issueLeaf(
            serial: 13, subjectCN: "bosun-test", publicKey: serverKey.publicKey,
            extensions: serverExtensions(serverAuth: true, uris: ["convivy://machine/bosun-test"]))

        do {
            try BosunServerVerification.verify(
                leafCertificateDER: leaf.der, rootPEM: foreignRoot.certificatePEM,
                expectedBosunURIName: "convivy://machine/bosun-test")
            Issue.record("expected a refusal")
        } catch let refusal as BosunServerRefusal {
            guard case .chainInvalid = refusal.reason else {
                Issue.record("wrong reason: \(refusal.reason)")
                return
            }
        }
    }
}
