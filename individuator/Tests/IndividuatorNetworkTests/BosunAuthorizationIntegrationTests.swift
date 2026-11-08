import CryptoKit
import Foundation
import NIOCore
import NIOPosix
import Testing
@testable import IndividuatorCore
@testable import IndividuatorCoreTests
@testable import IndividuatorDaemonCore
@testable import IndividuatorNetwork
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif

extension PoolBlockingIntegrationSuites {

/// Drives the real relay end to end — the root daemon's `authorize`
/// operation (`RegisterRequestHandler`), `BosunAuthorizationClient`'s real
/// mutual-TLS call, and a real `bosun_registration.RegistrationServer`
/// (`bosun_test_harness.py`) — over a real TLS 1.3 socket, the work item's
/// required integration tests (E12·2 slice S3): allow and deny for a
/// named-target call, a boundary grant for an executing call, a
/// named-target call outside a reduced ceiling, revocation, and an
/// unreachable Bosun.
///
/// THIS RELAY RELAYS THE BOSUN'S DECISION; IT ENFORCES NOTHING OF ITS OWN
/// (platform/decisions/0057) — every assertion below reads the daemon's
/// reply as exactly what the Bosun (or this client's own fail-closed
/// stand-in) answered, never as this suite's own policy.
///
/// Skips (rather than failing) under the same conditions
/// `BosunRegistrationClientIntegrationTests` does, for the same reasons —
/// see that suite's own doc comment — UNLESS
/// `INDIVIDUATOR_REQUIRE_MTLS_INTEGRATION_TEST` is set.
///
/// Nested under `PoolBlockingIntegrationSuites` (round 3, PR 1121): this
/// suite's own `.serialized` only bounds how many of ITS OWN tests can
/// block a cooperative-pool thread at once (one) — see that type's own doc
/// comment for why that is not enough on its own and every such suite in
/// this target needs to be nested there instead.
@Suite(.serialized, .timeLimit(.minutes(3)))
struct BosunAuthorizationIntegrationTests {
    private static let repoRoot: URL = {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
    }()

    private static var pythonExecutable: String {
        ProcessInfo.processInfo.environment["INDIVIDUATOR_TEST_PYTHON"] ?? "python3"
    }

    private static var requireIntegrationTest: Bool {
        ProcessInfo.processInfo.environment["INDIVIDUATOR_REQUIRE_MTLS_INTEGRATION_TEST"] != nil
    }

    private static func pythonCanRun(_ code: String) -> Bool {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        process.arguments = [pythonExecutable, "-c", code]
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        do {
            try process.run()
            process.waitUntilExit()
            return process.terminationStatus == 0
        } catch {
            return false
        }
    }

    private static var pythonHasCryptography: Bool = { pythonCanRun("import cryptography") }()

    private static var pythonCanBindLoopback: Bool = {
        pythonCanRun("""
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(('127.0.0.1', 0))
            s.close()
            """)
    }()

    private static func canRunIntegrationTest() -> Bool {
        if pythonHasCryptography && pythonCanBindLoopback {
            return true
        }
        if requireIntegrationTest {
            let message = "mTLS integration test cannot run (cryptography not importable via "
                + "\(pythonExecutable), or the sandbox refuses a loopback bind) and "
                + "INDIVIDUATOR_REQUIRE_MTLS_INTEGRATION_TEST is set, so CI requires "
                + "it to run rather than skip"
            Issue.record("\(message)")
        }
        return false
    }

    private func tempDirectory() throws -> URL {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    /// The fixed client machine name every scenario below registers under
    /// (its certificate's own SAN URI names it) — fixed so `revokeMachine`
    /// knows what to pass without threading the name through every call.
    private static let machineName = "test-machine-1"

    private struct Fixture {
        let harness: PythonBosunHarness
        let config: IndividuatorRootConfig
        let machineKey: any MachineKey
        let clientCertPEM: String
    }

    /// One throwaway root CA signs both the Bosun's server leaf and this
    /// machine's client leaf — this suite is testing the authorization
    /// relay, not the certificate-refusal matrix `BosunRegistrationClientIntegrationTests`
    /// already covers, so every scenario except `aForeignServerRootIsRefusedAsBosunUnreachable`
    /// uses one mutually-trusted root.
    private func makeFixture(dir: URL, serverRoot: TestCertificateAuthority? = nil) throws -> Fixture {
        let clientRoot = try TestCertificateAuthority.makeRoot(commonName: "test-client-root")
        let resolvedServerRoot = serverRoot ?? clientRoot

        let serverKey = P256.Signing.PrivateKey()
        let serverLeaf = try resolvedServerRoot.issueLeaf(
            serial: 100, subjectCN: "bosun-test-server", publicKey: serverKey.publicKey,
            extensions: [
                TestCertificateAuthority.ekuExtension(oids: [OID.serverAuth]),
                TestCertificateAuthority.sanExtension(uris: ["convivy://machine/bosun-test"]),
            ])
        let machineKey = SoftwareMachineKey()
        let clientLeaf = try clientRoot.issueLeaf(
            serial: 101, subjectCN: Self.machineName, publicKey: machineKey.publicKey,
            extensions: [
                TestCertificateAuthority.ekuExtension(oids: [OID.clientAuth]),
                TestCertificateAuthority.sanExtension(uris: ["convivy://machine/\(Self.machineName)"]),
            ])

        let trustAnchorURL = dir.appendingPathComponent("client-trust-anchor.pem")
        try clientRoot.certificatePEM.write(to: trustAnchorURL, atomically: true, encoding: .utf8)
        let serverCertURL = dir.appendingPathComponent("server.pem")
        try serverLeaf.pem.write(to: serverCertURL, atomically: true, encoding: .utf8)
        let serverKeyURL = dir.appendingPathComponent("server-key.pem")
        try serverKey.pemRepresentation.write(to: serverKeyURL, atomically: true, encoding: .utf8)

        let sessionsDB = dir.appendingPathComponent("sessions.db")
        let harness = try PythonBosunHarness(
            repoRoot: Self.repoRoot, pythonExecutable: Self.pythonExecutable, trustAnchor: trustAnchorURL,
            serverCert: serverCertURL, serverKey: serverKeyURL, sessionsDB: sessionsDB)

        let config = IndividuatorRootConfig(
            bosunHost: "bosun-test", bosunURIName: "convivy://machine/bosun-test",
            bosunAddress: "127.0.0.1:\(harness.port)", rootCAPath: trustAnchorURL.path,
            admittedAccounts: [AdmittedAccount(uid: 0, account: "operator")])

        return Fixture(harness: harness, config: config, machineKey: machineKey, clientCertPEM: clientLeaf.pem)
    }

    /// Registers directly through `BosunRegistrationClient` (never through
    /// a `SessionTable` handle at this point) with `ceiling`, returning the
    /// Bosun's own `session_token` for the scenario's own
    /// `BosunAuthorizationClient` calls or `SessionTable` entry to use.
    private func registerSession(_ fixture: Fixture, ceiling: String) throws -> String {
        let registrationClient = BosunRegistrationClient(
            config: fixture.config, machineKey: fixture.machineKey, clientCertificatePEM: fixture.clientCertPEM)
        let outcome = registrationClient.register(
            account: "test-operator", callerKind: "agent", ceiling: ceiling, protocolVersion: "1")
        guard case .registered(_, let sessionToken) = outcome else {
            Issue.record("expected a registration, got \(outcome)")
            return ""
        }
        return sessionToken
    }

    private func makeAuthorizationClient(_ fixture: Fixture) -> BosunAuthorizationClient {
        BosunAuthorizationClient(
            config: fixture.config, machineKey: fixture.machineKey, clientCertificatePEM: fixture.clientCertPEM)
    }

    // MARK: - The plan's five named scenarios

    @Test func allowAndDenyForANamedTargetCall() throws {
        guard Self.canRunIntegrationTest() else { return }
        let dir = try tempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let fixture = try makeFixture(dir: dir)
        defer { fixture.harness.terminateIfRunning() }

        let sessionToken = try registerSession(fixture, ceiling: #"["path:/work/allowed"]"#)
        let client = makeAuthorizationClient(fixture)

        let allowed = client.authorize(
            sessionToken: sessionToken, toolName: "Read", input: ["file_path": "/work/allowed/notes.txt"])
        #expect(allowed.allow)
        #expect(allowed.authorization == .object(objectRef: "path:/work/allowed/notes.txt"))

        let denied = client.authorize(
            sessionToken: sessionToken, toolName: "Read", input: ["file_path": "/etc/passwd"])
        #expect(!denied.allow)
        #expect(denied.reason.contains("outside the session's declared ceiling"))

        fixture.harness.quit()
    }

    @Test func aBoundaryGrantForAnExecutingCall() throws {
        guard Self.canRunIntegrationTest() else { return }
        let dir = try tempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let fixture = try makeFixture(dir: dir)
        defer { fixture.harness.terminateIfRunning() }

        let sessionToken = try registerSession(fixture, ceiling: #"["path:/work/allowed"]"#)
        let client = makeAuthorizationClient(fixture)

        let decision = client.authorize(sessionToken: sessionToken, toolName: "Bash", input: ["command": "ls /"])
        #expect(decision.allow)
        #expect(decision.authorization == .boundary(toolName: "Bash"))

        fixture.harness.quit()
    }

    @Test func aNamedTargetCallOutsideAReducedCeilingIsDenied() throws {
        guard Self.canRunIntegrationTest() else { return }
        let dir = try tempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let fixture = try makeFixture(dir: dir)
        defer { fixture.harness.terminateIfRunning() }

        // A reduced ceiling naming only one narrow path — never "full" —
        // so this scenario is distinct from `allowAndDenyForANamedTargetCall`'s
        // own deny case: the point here is specifically that a session's
        // own DECLARED reduction, not an unrestricted default, is what
        // excludes the object.
        let sessionToken = try registerSession(fixture, ceiling: #"["path:/work/narrow"]"#)
        let client = makeAuthorizationClient(fixture)

        let decision = client.authorize(
            sessionToken: sessionToken, toolName: "Edit", input: ["file_path": "/work/other/file.txt"])
        #expect(!decision.allow)
        #expect(decision.authorization == nil)

        fixture.harness.quit()
    }

    @Test func afterRevokeMachineTheNextCallIsRefused() throws {
        guard Self.canRunIntegrationTest() else { return }
        let dir = try tempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let fixture = try makeFixture(dir: dir)
        defer { fixture.harness.terminateIfRunning() }

        let sessionToken = try registerSession(fixture, ceiling: #"["path:/work/allowed"]"#)
        let client = makeAuthorizationClient(fixture)

        let beforeRevoke = client.authorize(
            sessionToken: sessionToken, toolName: "Read", input: ["file_path": "/work/allowed/a.txt"])
        #expect(beforeRevoke.allow)

        let revokedCount = try fixture.harness.revokeMachine(Self.machineName)
        #expect(revokedCount == 1)

        let afterRevoke = client.authorize(
            sessionToken: sessionToken, toolName: "Read", input: ["file_path": "/work/allowed/a.txt"])
        #expect(!afterRevoke.allow)
        #expect(afterRevoke.reason == "session_unknown")

        fixture.harness.quit()
    }

    @Test func withTheHarnessStoppedTheCallIsDenied() throws {
        guard Self.canRunIntegrationTest() else { return }
        let dir = try tempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let fixture = try makeFixture(dir: dir)

        let sessionToken = try registerSession(fixture, ceiling: #"["path:/work/allowed"]"#)
        let client = makeAuthorizationClient(fixture)

        fixture.harness.quit()

        let decision = client.authorize(
            sessionToken: sessionToken, toolName: "Read", input: ["file_path": "/work/allowed/a.txt"])
        #expect(decision == .bosunUnreachable)
    }

    // MARK: - The plan's named additional coverage

    /// An unknown handle and a wrong-uid handle are refused by the daemon
    /// handler itself, before `authorizationTransport.authorize` — and so
    /// before any network call — ever runs; `queryAuthorizationAuditCount`
    /// confirms the harness's own audit table saw nothing from either
    /// attempt. Drives the real `RegisterRequestHandler` over a socket
    /// pair, the same idiom `RegisterRequestHandlerTests` uses, wired to
    /// the real `BosunAuthorizationClient` rather than a fake — if either
    /// refusal path were wrong and actually reached the network, this
    /// would be the test to catch it.
    @Test func anUnknownHandleAndAWrongUIDHandleAreRefusedWithoutContactingTheHarness() throws {
        guard Self.canRunIntegrationTest() else { return }
        let dir = try tempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let fixture = try makeFixture(dir: dir)
        defer { fixture.harness.terminateIfRunning() }

        let sessionToken = try registerSession(fixture, ceiling: #"["path:/work/allowed"]"#)
        let sessions = SessionTable()
        let registeringUID = getuid()
        let handle = sessions.register(
            bosunSessionToken: sessionToken, callerKind: "agent", registeringUID: registeringUID + 1,
            account: "test-operator")

        let authorizationClient = makeAuthorizationClient(fixture)
        let handler = RegisterRequestHandler(
            admittedAccounts: [registeringUID: "operator"],
            transport: NeverCalledRegistrationTransport(), authorizationTransport: authorizationClient,
            sessions: sessions)

        let unknownResponse = sendAuthorizeRequest(
            handler: handler, handle: "not-a-real-handle", toolName: "Read",
            input: ["file_path": "/work/allowed/a.txt"])
        #expect(unknownResponse?["ok"] as? Bool == false)
        #expect(unknownResponse?["refused"] as? String == "unknown_handle")

        let mismatchResponse = sendAuthorizeRequest(
            handler: handler, handle: handle, toolName: "Read", input: ["file_path": "/work/allowed/a.txt"])
        #expect(mismatchResponse?["ok"] as? Bool == false)
        #expect(mismatchResponse?["refused"] as? String == "uid_mismatch")

        #expect(try fixture.harness.queryAuthorizationAuditCount() == 0)
        fixture.harness.quit()
    }

    @Test func aTimeoutAnswersBosunUnreachable() throws {
        guard Self.canRunIntegrationTest() else { return }

        final class SilentHandler: ChannelInboundHandler {
            typealias InboundIn = ByteBuffer
        }

        let group = MultiThreadedEventLoopGroup(numberOfThreads: 1)
        defer { try? group.syncShutdownGracefully() }
        let server = try ServerBootstrap(group: group)
            .childChannelInitializer { channel in channel.pipeline.addHandler(SilentHandler()) }
            .bind(host: "127.0.0.1", port: 0).wait()
        defer { try? server.close().wait() }
        guard let port = server.localAddress?.port else {
            Issue.record("the silent server has no bound port")
            return
        }

        let dir = try tempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let clientRoot = try TestCertificateAuthority.makeRoot(commonName: "test-client-root")
        let machineKey = SoftwareMachineKey()
        let clientLeaf = try clientRoot.issueLeaf(
            serial: 101, subjectCN: Self.machineName, publicKey: machineKey.publicKey,
            extensions: [
                TestCertificateAuthority.ekuExtension(oids: [OID.clientAuth]),
                TestCertificateAuthority.sanExtension(uris: ["convivy://machine/\(Self.machineName)"]),
            ])
        let trustAnchorURL = dir.appendingPathComponent("client-trust-anchor.pem")
        try clientRoot.certificatePEM.write(to: trustAnchorURL, atomically: true, encoding: .utf8)

        let config = IndividuatorRootConfig(
            bosunHost: "bosun-test", bosunURIName: "convivy://machine/bosun-test",
            bosunAddress: "127.0.0.1:\(port)", rootCAPath: trustAnchorURL.path,
            admittedAccounts: [AdmittedAccount(uid: 0, account: "operator")])
        let client = BosunAuthorizationClient(
            config: config, machineKey: machineKey, clientCertificatePEM: clientLeaf.pem, responseTimeoutSeconds: 2)

        let start = ContinuousClock.now
        let decision = client.authorize(sessionToken: "irrelevant", toolName: "Read", input: [:])
        let elapsed = start.duration(to: .now)

        #expect(elapsed < .seconds(15))
        #expect(decision == .bosunUnreachable)
    }

    /// The Bosun's server certificate chains to a root the client does not
    /// trust (a foreign root, the same shape
    /// `BosunRegistrationClientIntegrationTests.refusesAForeignRoot` uses)
    /// — `BosunServerVerification` refuses it, and this relay must answer
    /// `.bosunUnreachable` rather than throw or surface the refusal any
    /// other way.
    @Test func aForeignServerRootAnswersBosunUnreachable() throws {
        guard Self.canRunIntegrationTest() else { return }
        let dir = try tempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let foreignRoot = try TestCertificateAuthority.makeRoot(commonName: "a-foreign-server-root")
        let fixture = try makeFixture(dir: dir, serverRoot: foreignRoot)
        defer { fixture.harness.terminateIfRunning() }

        let client = makeAuthorizationClient(fixture)
        let decision = client.authorize(sessionToken: "irrelevant", toolName: "Read", input: [:])
        #expect(decision == .bosunUnreachable)

        fixture.harness.quit()
    }

    /// The Bosun session token's bytes appear nowhere in the daemon's
    /// encoded reply line — not under any key, and not as a substring —
    /// whether the Bosun allowed or denied the call.
    @Test func theBosunTokenNeverAppearsInAnyReply() throws {
        guard Self.canRunIntegrationTest() else { return }
        let dir = try tempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let fixture = try makeFixture(dir: dir)
        defer { fixture.harness.terminateIfRunning() }

        let sessionToken = try registerSession(fixture, ceiling: #"["path:/work/allowed"]"#)
        let sessions = SessionTable()
        let registeringUID = getuid()
        let handle = sessions.register(
            bosunSessionToken: sessionToken, callerKind: "agent", registeringUID: registeringUID,
            account: "test-operator")
        let authorizationClient = makeAuthorizationClient(fixture)
        let handler = RegisterRequestHandler(
            admittedAccounts: [registeringUID: "operator"],
            transport: NeverCalledRegistrationTransport(), authorizationTransport: authorizationClient,
            sessions: sessions)

        let (serverFD, clientFD) = makeAuthorizationSocketPair()
        let thread = Thread {
            handler.handle(fileDescriptor: serverFD)
        }
        thread.start()
        var line = try JSONSerialization.data(
            withJSONObject: ["op": "authorize", "handle": handle, "tool_name": "Read",
                              "input": ["file_path": "/work/allowed/a.txt"]])
        line.append(UInt8(ascii: "\n"))
        line.withUnsafeBytes { ptr in _ = write(clientFD, ptr.baseAddress, ptr.count) }

        var responseBytes: [UInt8] = []
        var byte: UInt8 = 0
        while true {
            let n = read(clientFD, &byte, 1)
            if n <= 0 || byte == UInt8(ascii: "\n") { break }
            responseBytes.append(byte)
        }
        close(clientFD)

        let encoded = String(decoding: responseBytes, as: UTF8.self)
        #expect(!encoded.contains(sessionToken))
        #expect(encoded.contains("\"allow\""))

        fixture.harness.quit()
    }

    // MARK: - Test-only plumbing

    private struct NeverCalledRegistrationTransport: RegistrationTransport {
        func register(account: String, callerKind: String, ceiling: String, protocolVersion: String) -> RegistrationOutcome {
            Issue.record("register should never be called from this file's authorize-only tests")
            return .transportError(detail: "unexpected call")
        }
    }

    private func makeAuthorizationSocketPair() -> (Int32, Int32) {
        var fds: [Int32] = [0, 0]
        let result = fds.withUnsafeMutableBufferPointer { socketpair(AF_UNIX, SOCK_STREAM, 0, $0.baseAddress) }
        precondition(result == 0, "socketpair() failed")
        SocketIO.setNoSIGPIPE(fileDescriptor: fds[0])
        SocketIO.setNoSIGPIPE(fileDescriptor: fds[1])
        return (fds[0], fds[1])
    }

    private func sendAuthorizeRequest(
        handler: RegisterRequestHandler, handle: String, toolName: String, input: [String: Any]
    ) -> [String: Any]? {
        let (serverFD, clientFD) = makeAuthorizationSocketPair()
        let thread = Thread { handler.handle(fileDescriptor: serverFD) }
        thread.start()

        var line = try! JSONSerialization.data(
            withJSONObject: ["op": "authorize", "handle": handle, "tool_name": toolName, "input": input])
        line.append(UInt8(ascii: "\n"))
        line.withUnsafeBytes { ptr in _ = write(clientFD, ptr.baseAddress, ptr.count) }

        var responseBytes: [UInt8] = []
        var byte: UInt8 = 0
        while true {
            let n = read(clientFD, &byte, 1)
            if n <= 0 || byte == UInt8(ascii: "\n") { break }
            responseBytes.append(byte)
        }
        close(clientFD)
        return try? JSONSerialization.jsonObject(with: Data(responseBytes)) as? [String: Any]
    }
}

} // extension PoolBlockingIntegrationSuites
