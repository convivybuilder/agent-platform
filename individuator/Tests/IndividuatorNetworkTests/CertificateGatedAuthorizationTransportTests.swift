import Foundation
import Testing
@testable import IndividuatorCore
@testable import IndividuatorNetwork

/// `CertificateGatedAuthorizationTransport` is what lets `authorize` refuse
/// without ever touching the network before a certificate has been adopted
/// — the same reason `CertificateGatedRegistrationTransportTests` exists for
/// `register`. Unlike that counterpart, a missing certificate and a real
/// network failure both answer `.bosunUnreachable` here (see
/// `AuthorizationDecision.bosunUnreachable`'s own doc comment), so these use
/// `RecordingAuthorizationTransport` via the transport's `makeClient` seam
/// rather than a non-listening address, to tell "never touched the network"
/// apart from "touched it and failed".
@Suite(.timeLimit(.minutes(1)))
struct CertificateGatedAuthorizationTransportTests {
    private final class RecordingAuthorizationTransport: AuthorizationTransport {
        private(set) var callCount = 0
        let decision: AuthorizationDecision
        init(decision: AuthorizationDecision) { self.decision = decision }
        func authorize(sessionToken: String, toolName: String, input: [String: Any]) -> AuthorizationDecision {
            callCount += 1
            return decision
        }
    }

    private func makeTempDirectory() -> URL {
        FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    }

    private func makeConfig() -> IndividuatorRootConfig {
        IndividuatorRootConfig(
            bosunHost: "bosun-test", bosunURIName: "convivy://machine/bosun-test",
            bosunAddress: "127.0.0.1:1", rootCAPath: "/dev/null", admittedAccounts: [AdmittedAccount(uid: getuid(), account: "operator")])
    }

    @Test func refusesWithBosunUnreachableWhenNoCertificateHasBeenAdopted() throws {
        let dir = makeTempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let certStore = KeyFileStore(directory: dir, fileName: "machine-cert.pem", requiredOwnerUID: getuid())
        let recorder = RecordingAuthorizationTransport(decision: AuthorizationDecision(allow: true, reason: "should never be seen"))
        let transport = CertificateGatedAuthorizationTransport(
            config: makeConfig(), machineKey: SoftwareMachineKey(), certificateStore: certStore,
            makeClient: { _ in recorder })

        let decision = transport.authorize(sessionToken: "tok", toolName: "Read", input: [:])

        #expect(decision == .bosunUnreachable)
        #expect(recorder.callCount == 0)
    }

    @Test func refusesWithBosunUnreachableWhenTheCertificateFileFailsCustodyChecks() throws {
        let dir = makeTempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let certStore = KeyFileStore(directory: dir, fileName: "machine-cert.pem", requiredOwnerUID: getuid())
        try certStore.prepareForCreate()
        try certStore.write(Data("not actually a certificate".utf8))
        try FileManager.default.setAttributes([.posixPermissions: 0o644], ofItemAtPath: certStore.fileURL.path)

        let recorder = RecordingAuthorizationTransport(decision: AuthorizationDecision(allow: true, reason: "should never be seen"))
        let transport = CertificateGatedAuthorizationTransport(
            config: makeConfig(), machineKey: SoftwareMachineKey(), certificateStore: certStore,
            makeClient: { _ in recorder })

        let decision = transport.authorize(sessionToken: "tok", toolName: "Read", input: [:])

        #expect(decision == .bosunUnreachable)
        #expect(recorder.callCount == 0)
    }

    /// Once a certificate is placed (at the right custody), the very next
    /// `authorize` call reads it — no restart, and no second instance of
    /// this transport needed — and this time `makeClient` is actually
    /// invoked and its decision passed straight through, proving the
    /// certificate is no longer treated as missing.
    @Test func readsAnAdoptedCertificateOnTheNextRequestWithNoRestart() throws {
        let dir = makeTempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let certStore = KeyFileStore(directory: dir, fileName: "machine-cert.pem", requiredOwnerUID: getuid())
        let recorder = RecordingAuthorizationTransport(decision: AuthorizationDecision(allow: true, reason: "ok"))
        let transport = CertificateGatedAuthorizationTransport(
            config: makeConfig(), machineKey: SoftwareMachineKey(), certificateStore: certStore,
            makeClient: { _ in recorder })

        let before = transport.authorize(sessionToken: "tok", toolName: "Read", input: [:])
        #expect(before == .bosunUnreachable)
        #expect(recorder.callCount == 0)

        try certStore.prepareForCreate()
        try certStore.write(Data("-----BEGIN CERTIFICATE-----\nnot real\n-----END CERTIFICATE-----\n".utf8))

        let after = transport.authorize(sessionToken: "tok", toolName: "Read", input: [:])
        #expect(after == recorder.decision)
        #expect(recorder.callCount == 1)
    }
}
