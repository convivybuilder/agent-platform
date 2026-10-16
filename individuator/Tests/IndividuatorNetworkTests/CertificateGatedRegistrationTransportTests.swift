import Foundation
import Testing
@testable import IndividuatorCore
@testable import IndividuatorNetwork

/// `CertificateGatedRegistrationTransport` is what lets the daemon start
/// and serve its socket before a certificate has been adopted
/// (2026-10-15) — these never open a real network connection: the config
/// below names an address nothing listens on, so a test that reached the
/// network by mistake would hang on a connect timeout rather than pass
/// quickly.
@Suite(.timeLimit(.minutes(1)))
struct CertificateGatedRegistrationTransportTests {
    private func makeTempDirectory() -> URL {
        FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    }

    private func makeConfig() -> IndividuatorRootConfig {
        IndividuatorRootConfig(
            bosunHost: "bosun-test", bosunURIName: "convivy://machine/bosun-test",
            bosunAddress: "127.0.0.1:1", rootCAPath: "/dev/null", operatorUID: getuid())
    }

    @Test func refusesWithCertificateMissingWhenNoCertificateHasBeenAdopted() throws {
        let dir = makeTempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let certStore = KeyFileStore(directory: dir, fileName: "machine-cert.pem", requiredOwnerUID: getuid())
        let transport = CertificateGatedRegistrationTransport(
            config: makeConfig(), machineKey: SoftwareMachineKey(), certificateStore: certStore)

        let outcome = transport.register(
            account: "operator", callerKind: "operator", ceiling: "full", protocolVersion: "1")

        guard case .refused(let reason, _) = outcome else {
            Issue.record("expected a refusal, got \(outcome)")
            return
        }
        #expect(reason == CertificateGatedRegistrationTransport.certificateMissingReason)
    }

    @Test func refusesWithCertificateMissingWhenTheCertificateFileFailsCustodyChecks() throws {
        let dir = makeTempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let certStore = KeyFileStore(directory: dir, fileName: "machine-cert.pem", requiredOwnerUID: getuid())
        try certStore.prepareForCreate()
        try certStore.write(Data("not actually a certificate".utf8))
        try FileManager.default.setAttributes([.posixPermissions: 0o644], ofItemAtPath: certStore.fileURL.path)

        let transport = CertificateGatedRegistrationTransport(
            config: makeConfig(), machineKey: SoftwareMachineKey(), certificateStore: certStore)
        let outcome = transport.register(
            account: "operator", callerKind: "operator", ceiling: "full", protocolVersion: "1")

        guard case .refused(let reason, _) = outcome else {
            Issue.record("expected a refusal, got \(outcome)")
            return
        }
        #expect(reason == CertificateGatedRegistrationTransport.certificateMissingReason)
    }

    /// Once a certificate is placed (at the right custody), the very next
    /// `register` call reads it — no restart, and no second instance of
    /// this transport needed — this one reaches the network because the
    /// certificate is no longer missing, so it fails for an unrelated
    /// transport reason instead (nothing listens on `127.0.0.1:1`), which
    /// is exactly the proof it got past the `certificate_missing` refusal.
    @Test func readsAnAdoptedCertificateOnTheNextRequestWithNoRestart() throws {
        let dir = makeTempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let certStore = KeyFileStore(directory: dir, fileName: "machine-cert.pem", requiredOwnerUID: getuid())
        let transport = CertificateGatedRegistrationTransport(
            config: makeConfig(), machineKey: SoftwareMachineKey(), certificateStore: certStore)

        let before = transport.register(
            account: "operator", callerKind: "operator", ceiling: "full", protocolVersion: "1")
        guard case .refused(let reasonBefore, _) = before else {
            Issue.record("expected a refusal before adoption, got \(before)")
            return
        }
        #expect(reasonBefore == CertificateGatedRegistrationTransport.certificateMissingReason)

        try certStore.prepareForCreate()
        try certStore.write(Data("-----BEGIN CERTIFICATE-----\nnot real\n-----END CERTIFICATE-----\n".utf8))

        let after = transport.register(
            account: "operator", callerKind: "operator", ceiling: "full", protocolVersion: "1")
        guard case .transportError = after else {
            Issue.record("expected a transport error (certificate present, network refused), got \(after)")
            return
        }
    }
}
