import CryptoKit
import Foundation
import Testing
@testable import IndividuatorCore

@Suite struct CertificateAdoptionTests {
    private func leafExtensions(clientAuth: Bool, uris: [String]) -> [[UInt8]] {
        var exts: [[UInt8]] = []
        if clientAuth {
            exts.append(TestCertificateAuthority.ekuExtension(oids: [OID.clientAuth]))
        }
        if !uris.isEmpty {
            exts.append(TestCertificateAuthority.sanExtension(uris: uris))
        }
        return exts
    }

    @Test func acceptsACorrectCertificate() throws {
        let ca = try TestCertificateAuthority.makeRoot()
        let machineKey = SoftwareMachineKey()
        let leaf = try ca.issueLeaf(
            serial: 2, subjectCN: "synthetic-mac-2", publicKey: machineKey.publicKey,
            extensions: leafExtensions(clientAuth: true, uris: ["convivy://machine/synthetic-mac-2"]))

        let adopted = try CertificateAdoption.adopt(
            certificatePEM: leaf.pem, rootPEM: ca.certificatePEM, machineKey: machineKey)

        #expect(adopted.uriName == "convivy://machine/synthetic-mac-2")
        #expect(adopted.certificatePEM == leaf.pem)
    }

    @Test func refusesAWrongKey() throws {
        let ca = try TestCertificateAuthority.makeRoot()
        let certifiedKey = SoftwareMachineKey()
        let holderKey = SoftwareMachineKey() // a different key than the certificate was issued for
        let leaf = try ca.issueLeaf(
            serial: 3, subjectCN: "synthetic-mac-3", publicKey: certifiedKey.publicKey,
            extensions: leafExtensions(clientAuth: true, uris: ["convivy://machine/synthetic-mac-3"]))

        #expect(throws: AdoptionRefusal(reason: .keyMismatch)) {
            _ = try CertificateAdoption.adopt(
                certificatePEM: leaf.pem, rootPEM: ca.certificatePEM, machineKey: holderKey)
        }
    }

    @Test func refusesAForeignRoot() throws {
        let ca = try TestCertificateAuthority.makeRoot()
        let foreignRoot = try TestCertificateAuthority.makeRoot(commonName: "a-different-root")
        let machineKey = SoftwareMachineKey()
        let leaf = try ca.issueLeaf(
            serial: 4, subjectCN: "synthetic-mac-4", publicKey: machineKey.publicKey,
            extensions: leafExtensions(clientAuth: true, uris: ["convivy://machine/synthetic-mac-4"]))

        do {
            _ = try CertificateAdoption.adopt(
                certificatePEM: leaf.pem, rootPEM: foreignRoot.certificatePEM, machineKey: machineKey)
            Issue.record("expected a refusal")
        } catch let refusal as AdoptionRefusal {
            guard case .chainInvalid = refusal.reason else {
                Issue.record("wrong reason: \(refusal.reason)")
                return
            }
        }
    }

    @Test func refusesMissingClientAuth() throws {
        let ca = try TestCertificateAuthority.makeRoot()
        let machineKey = SoftwareMachineKey()
        let leaf = try ca.issueLeaf(
            serial: 5, subjectCN: "synthetic-mac-5", publicKey: machineKey.publicKey,
            extensions: leafExtensions(clientAuth: false, uris: ["convivy://machine/synthetic-mac-5"]))

        #expect(throws: AdoptionRefusal(reason: .missingClientAuth)) {
            _ = try CertificateAdoption.adopt(
                certificatePEM: leaf.pem, rootPEM: ca.certificatePEM, machineKey: machineKey)
        }
    }

    @Test func refusesZeroURINames() throws {
        let ca = try TestCertificateAuthority.makeRoot()
        let machineKey = SoftwareMachineKey()
        let leaf = try ca.issueLeaf(
            serial: 6, subjectCN: "synthetic-mac-6", publicKey: machineKey.publicKey,
            extensions: leafExtensions(clientAuth: true, uris: []))

        #expect(throws: AdoptionRefusal(reason: .sanNotExactlyOneURI(count: 0))) {
            _ = try CertificateAdoption.adopt(
                certificatePEM: leaf.pem, rootPEM: ca.certificatePEM, machineKey: machineKey)
        }
    }

    @Test func refusesTwoURINames() throws {
        let ca = try TestCertificateAuthority.makeRoot()
        let machineKey = SoftwareMachineKey()
        let leaf = try ca.issueLeaf(
            serial: 7, subjectCN: "synthetic-mac-7", publicKey: machineKey.publicKey,
            extensions: leafExtensions(
                clientAuth: true,
                uris: ["convivy://machine/synthetic-mac-7", "convivy://machine/synthetic-mac-7-alias"]))

        #expect(throws: AdoptionRefusal(reason: .sanNotExactlyOneURI(count: 2))) {
            _ = try CertificateAdoption.adopt(
                certificatePEM: leaf.pem, rootPEM: ca.certificatePEM, machineKey: machineKey)
        }
    }

    // Manually verified (not automated — the checks below are the thing
    // under test, so disabling one to prove its test fails means editing
    // `CertificateAdoption.swift` itself, running, then reverting):
    // commenting out `checkKeyMatches` turns `refusesAWrongKey` green on a
    // cert for the wrong key; commenting out `SecTrustSetAnchorCertificatesOnly`
    // turns `refusesAForeignRoot` green; commenting out the `checkClientAuth`
    // call turns `refusesMissingClientAuth` green; and changing
    // `checkExactlyOneURISAN`'s `== 1` to `>= 1` turns `refusesTwoURINames`
    // green while leaving `refusesZeroURINames` red for the right reason.
}
