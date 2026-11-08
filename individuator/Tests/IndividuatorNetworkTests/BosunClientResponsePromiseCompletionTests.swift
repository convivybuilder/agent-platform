import CryptoKit
import Foundation
import Testing
@testable import IndividuatorCore
@testable import IndividuatorCoreTests
@testable import IndividuatorNetwork

/// Unit-level coverage (no Python harness, no bound listener) for the gap
/// CI's `swift` job hit against 43918fabd: `BosunAuthorizationClient
/// .performAuthorization` created its `responsePromise` and deferred
/// `group.syncShutdownGracefully()` without ever guaranteeing the promise
/// got completed first. A `connect` that fails before `_ResponseCollector`
/// or the timeout task ever touches the promise — a connection refused
/// being the real occurrence, a malformed address never even reaching
/// `connect` — left it unfulfilled, and NIO's own leak check turned that
/// into "Fatal error: leaking promise created at ..." tearing down the
/// whole `swift test` process rather than a normal test failure.
/// `BosunRegistrationClient.performRegistration` is built from the
/// identical shape and had the identical gap, unexercised by any existing
/// test (every `BosunRegistrationClientIntegrationTests` scenario either
/// registers against a running harness or hits a handshake failure after
/// `connect` already succeeded) — covered here too, for the same reason.
///
/// Connecting to a loopback address nothing listens on needs no bind of
/// this process's own: `127.0.0.1:1` is a reserved, sub-1024 port no
/// unprivileged test process can be the one bound to, so the connect
/// itself (not a prior bind this suite would need to tear down) is what
/// gets refused.
@Suite struct BosunClientResponsePromiseCompletionTests {
    private static let nothingListening = "127.0.0.1:1"

    private func tempDirectory() throws -> URL {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    /// A root CA plus one client leaf under it — enough for
    /// `BosunMutualTLS.makeSetup` to build a real `NIOSSLContext` and
    /// reach the `connect` call; no server leaf, because a connect that
    /// never completes never reaches the handshake that would need one.
    private func makeClientFixture(dir: URL) throws -> (config: IndividuatorRootConfig, machineKey: any MachineKey, clientCertPEM: String) {
        let root = try TestCertificateAuthority.makeRoot(commonName: "test-client-root")
        let machineKey = SoftwareMachineKey()
        let clientLeaf = try root.issueLeaf(
            serial: 101, subjectCN: "test-machine-1", publicKey: machineKey.publicKey,
            extensions: [
                TestCertificateAuthority.ekuExtension(oids: [OID.clientAuth]),
                TestCertificateAuthority.sanExtension(uris: ["convivy://machine/test-machine-1"]),
            ])
        let trustAnchorURL = dir.appendingPathComponent("client-trust-anchor.pem")
        try root.certificatePEM.write(to: trustAnchorURL, atomically: true, encoding: .utf8)

        let config = IndividuatorRootConfig(
            bosunHost: "bosun-test", bosunURIName: "convivy://machine/bosun-test",
            bosunAddress: Self.nothingListening, rootCAPath: trustAnchorURL.path,
            admittedAccounts: [AdmittedAccount(uid: 0, account: "operator")])
        return (config, machineKey, clientLeaf.pem)
    }

    @Test func authorizeAgainstAConnectionRefusedAddressAnswersBosunUnreachable() throws {
        let dir = try tempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let (config, machineKey, clientCertPEM) = try makeClientFixture(dir: dir)
        let client = BosunAuthorizationClient(config: config, machineKey: machineKey, clientCertificatePEM: clientCertPEM)

        let decision = client.authorize(sessionToken: "irrelevant", toolName: "Read", input: [:])

        #expect(decision == .bosunUnreachable)
    }

    @Test func authorizeAgainstAMalformedAddressAnswersBosunUnreachable() throws {
        let config = IndividuatorRootConfig(
            bosunHost: "bosun-test", bosunURIName: "convivy://machine/bosun-test",
            bosunAddress: "not-an-address", rootCAPath: "/nonexistent",
            admittedAccounts: [AdmittedAccount(uid: 0, account: "operator")])
        let client = BosunAuthorizationClient(
            config: config, machineKey: SoftwareMachineKey(), clientCertificatePEM: "")

        let decision = client.authorize(sessionToken: "irrelevant", toolName: "Read", input: [:])

        #expect(decision == .bosunUnreachable)
    }

    @Test func registerAgainstAConnectionRefusedAddressAnswersTransportError() throws {
        let dir = try tempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let (config, machineKey, clientCertPEM) = try makeClientFixture(dir: dir)
        let client = BosunRegistrationClient(config: config, machineKey: machineKey, clientCertificatePEM: clientCertPEM)

        let outcome = client.register(
            account: "test-operator", callerKind: "operator", ceiling: #"["path:/work/allowed"]"#, protocolVersion: "1")

        guard case .transportError = outcome else {
            Issue.record("expected a transport error, got \(outcome)")
            return
        }
    }

    @Test func registerAgainstAMalformedAddressAnswersTransportError() throws {
        let config = IndividuatorRootConfig(
            bosunHost: "bosun-test", bosunURIName: "convivy://machine/bosun-test",
            bosunAddress: "not-an-address", rootCAPath: "/nonexistent",
            admittedAccounts: [AdmittedAccount(uid: 0, account: "operator")])
        let client = BosunRegistrationClient(
            config: config, machineKey: SoftwareMachineKey(), clientCertificatePEM: "")

        let outcome = client.register(
            account: "test-operator", callerKind: "operator", ceiling: #"["path:/work/allowed"]"#, protocolVersion: "1")

        guard case .transportError = outcome else {
            Issue.record("expected a transport error, got \(outcome)")
            return
        }
    }
}
