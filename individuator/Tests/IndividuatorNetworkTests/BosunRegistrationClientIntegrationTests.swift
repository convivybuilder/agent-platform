import CryptoKit
import Foundation
import NIOCore
import NIOPosix
import Testing
@testable import IndividuatorCore
@testable import IndividuatorCoreTests
@testable import IndividuatorNetwork
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif

/// Runs the Swift mutual-TLS client against a REAL
/// `bosun_registration.RegistrationServer`, over a real TLS 1.3 socket, with
/// a throwaway test root — the work item's required integration test. The
/// four cases below are the work item's own matrix: success; a server
/// certificate with the wrong URI name; one without serverAuth; and one
/// chained to a foreign root. Each refusal case asserts the session count
/// at the Bosun stayed zero.
///
/// Skips (rather than failing) when the environment cannot stand up the
/// real Python half: the configured interpreter without `cryptography`
/// importable, or a sandbox that refuses a loopback TCP bind outright
/// (`EPERM` — some CI and local sandboxes disable local port binding
/// entirely; a plain, no-TLS `socket.bind(("127.0.0.1", 0))` fails
/// identically there, which is the positive control `pythonCanBindLoopback`
/// runs before blaming this test's own TLS plumbing). Either gets a
/// visible, named skip rather than a failure indistinguishable from a real
/// regression — UNLESS `INDIVIDUATOR_REQUIRE_MTLS_INTEGRATION_TEST` is set,
/// which CI's `swift` job does on its own test step: there, the same skip
/// is instead a failure, so a broken `cryptography` install (its own named
/// failure mode; see that job's comment in `ci.yml`) cannot leave this job
/// green with the mTLS client untested.
// Round 9's own occurrence (2026-10-15, run 38787155779/116602087817): a
// hang in this suite left `swift test` printing nothing until CI's
// `timeout-minutes: 20` killed it, with no test name in the log to say
// which one. `.timeLimit` makes a hang fail as that one test, under its
// own name, well before the job-level timeout — a backstop underneath the
// harness-level deadlines below, not a replacement for them.
//
// Round 10's own occurrence (2026-10-15, run 38792747476/116621291433):
// Swift Testing schedules this suite's seven tests as concurrent tasks on
// the Swift cooperative thread pool, which on the CI runner is only as
// wide as its core count. Every test here blocks its pool thread
// synchronously — `PythonBosunHarness.readLine`'s poll loop, `usleep` in
// `wait()`, `Process.waitUntilExit()`, NIO's `.wait()` on a future in
// `aPeerThatAcceptsAndGoesSilentTimesOutRatherThanHangingForever` — so with
// several in flight at once the pool is exhausted and nothing scheduled on
// it, including a test's own 2s NIO deadline firing and trying to resume,
// can make progress until another one releases its thread. That is why
// four of them hit the harness's old 15s PORT deadline at the identical
// instant and the silent-peer test's 2s client deadline took 15s to
// surface. `.serialized` runs this suite's tests one at a time, so only
// one pool thread is ever blocked by this suite's own blocking work; the
// rest of the pool, and the package's other suites, stay free. `.timeLimit`
// on a `.serialized` suite still bounds each test individually, not the
// suite's total, so it does not need to grow by the number of tests.
@Suite(.serialized, .timeLimit(.minutes(3)))
struct BosunRegistrationClientIntegrationTests {
    private static let repoRoot: URL = {
        // Tests/IndividuatorNetworkTests/<this file> -> individuator/ -> repo root.
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
    }()

    /// The interpreter to launch for both the harness subprocess and the
    /// checks below. CI's `swift` job sets this to the path
    /// `actions/setup-python` just installed `cryptography` into, rather
    /// than leaving it to resolve `python3` off PATH — see that job's
    /// comment in `ci.yml`. Unset outside CI, where the one `python3` on
    /// PATH is the right one.
    private static var pythonExecutable: String {
        ProcessInfo.processInfo.environment["INDIVIDUATOR_TEST_PYTHON"] ?? "python3"
    }

    /// Set by CI's `swift` job on its test step, in the INDIVIDUATOR_
    /// namespace: turns the skip below into a test failure, so a broken
    /// `cryptography` install cannot leave this job green with the mTLS
    /// client untested. Unset locally, where the skip is correct.
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

    /// The positive control for the harness's own `EPERM` on
    /// `server_bind()`: a plain socket bind, no TLS, no `bosun_registration`
    /// involved. If THIS fails, the sandbox refuses local port binding
    /// outright and the harness's identical failure proves nothing about
    /// this package's own TLS code.
    private static var pythonCanBindLoopback: Bool = {
        pythonCanRun("""
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(('127.0.0.1', 0))
            s.close()
            """)
    }()

    /// `true` when the real Python half can stand up; when it can't AND
    /// `INDIVIDUATOR_REQUIRE_MTLS_INTEGRATION_TEST` is set, records a
    /// failure instead of letting the caller's test return quietly.
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

    /// Builds a root CA plus one server leaf (under `serverRoot`, with the
    /// given EKU/SAN extensions) and one client leaf (under `clientRoot`,
    /// clientAuth + a fixed test machine name) — `serverRoot`/`clientRoot`
    /// are the same CA in every case except `refusesAForeignRoot`.
    private func writeCertificates(
        dir: URL, serverRoot: TestCertificateAuthority, clientRoot: TestCertificateAuthority,
        serverEKU: Bool, serverURIs: [String], machineKey: (any MachineKey)? = nil
    ) throws -> (trustAnchorForServer: URL, serverCert: URL, serverKey: URL, clientCertPEM: String, machineKey: any MachineKey) {
        let serverKey = P256.Signing.PrivateKey()
        var serverExtensions: [[UInt8]] = []
        if serverEKU { serverExtensions.append(TestCertificateAuthority.ekuExtension(oids: [OID.serverAuth])) }
        if !serverURIs.isEmpty { serverExtensions.append(TestCertificateAuthority.sanExtension(uris: serverURIs)) }
        let serverLeaf = try serverRoot.issueLeaf(
            serial: 100, subjectCN: "bosun-test-server", publicKey: serverKey.publicKey, extensions: serverExtensions)

        // A caller passes its own `machineKey` to drive the handshake with
        // something other than a plain working key (round 7's own masking
        // tests, below, need a key whose `signature(for:)` throws) — the
        // certificate still embeds whichever key's public half, same as a
        // real CSR would.
        let resolvedMachineKey: any MachineKey = machineKey ?? SoftwareMachineKey()
        let clientExtensions: [[UInt8]] = [
            TestCertificateAuthority.ekuExtension(oids: [OID.clientAuth]),
            TestCertificateAuthority.sanExtension(uris: ["convivy://machine/test-machine-1"]),
        ]
        let clientLeaf = try clientRoot.issueLeaf(
            serial: 101, subjectCN: "test-machine-1", publicKey: resolvedMachineKey.publicKey,
            extensions: clientExtensions)

        let trustAnchorURL = dir.appendingPathComponent("client-trust-anchor.pem")
        try clientRoot.certificatePEM.write(to: trustAnchorURL, atomically: true, encoding: .utf8)
        let serverCertURL = dir.appendingPathComponent("server.pem")
        try serverLeaf.pem.write(to: serverCertURL, atomically: true, encoding: .utf8)
        let serverKeyURL = dir.appendingPathComponent("server-key.pem")
        try serverKey.pemRepresentation.write(to: serverKeyURL, atomically: true, encoding: .utf8)

        return (trustAnchorURL, serverCertURL, serverKeyURL, clientLeaf.pem, resolvedMachineKey)
    }

    private func runScenario(
        serverEKU: Bool, serverURIs: [String], foreignServerRoot: Bool,
        expectedBosunHost: String = "bosun-test", machineKey: (any MachineKey)? = nil
    ) throws -> (outcome: RegistrationOutcome, sessionsAfter: Int, harnessStderr: () -> String) {
        let dir = try tempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }

        let clientRoot = try TestCertificateAuthority.makeRoot(commonName: "test-client-root")
        let serverRoot = foreignServerRoot
            ? try TestCertificateAuthority.makeRoot(commonName: "a-foreign-server-root") : clientRoot

        let (trustAnchor, serverCert, serverKey, clientCertPEM, resolvedMachineKey) = try writeCertificates(
            dir: dir, serverRoot: serverRoot, clientRoot: clientRoot, serverEKU: serverEKU, serverURIs: serverURIs,
            machineKey: machineKey)

        let sessionsDB = dir.appendingPathComponent("sessions.db")
        let harness = try PythonBosunHarness(
            repoRoot: Self.repoRoot, pythonExecutable: Self.pythonExecutable, trustAnchor: trustAnchor,
            serverCert: serverCert, serverKey: serverKey, sessionsDB: sessionsDB)
        // Covers every exit from here on, including a thrown error before
        // the explicit `quit()` below runs: a no-op once `quit()` has
        // already brought the process down, a kill if anything throws
        // first.
        defer { harness.terminateIfRunning() }

        let config = IndividuatorRootConfig(
            bosunHost: expectedBosunHost, bosunURIName: "convivy://machine/\(expectedBosunHost)",
            bosunAddress: "127.0.0.1:\(harness.port)", rootCAPath: clientRoot.pemPath(in: dir), operatorUID: 0)
        let client = BosunRegistrationClient(
            config: config, machineKey: resolvedMachineKey, clientCertificatePEM: clientCertPEM)

        // A narrow JSON array of `bosun_authz.scope()` strings — the one
        // shape `bosun_authz.ceiling_entries_from_text` accepts besides the
        // literal text "full" (see that function's own doc comment) — so
        // this registers a session scoped to one path rather than an
        // unrestricted one.
        let outcome = client.register(
            account: "test-operator", callerKind: "operator",
            ceiling: #"["path:/work/allowed"]"#, protocolVersion: "1")
        let sessionsAfter = try harness.querySessionCount()
        // Stderr is drained concurrently from launch (see `PythonBosunHarness`),
        // so `quit()` no longer has to run before `capturedStderr` is
        // readable — quitting here, rather than only in the `defer` above,
        // still matters so a passing scenario leaves no process behind
        // before this function returns.
        harness.quit()
        return (outcome, sessionsAfter, { harness.capturedStderr })
    }

    /// Prints `harnessStderr()` — the Python server's own handshake/connection
    /// diagnostics (`bosun_test_harness.py`'s `_log_server_handshake_errors`)
    /// — alongside EVERY failure, unconditionally, since the Swift client's
    /// own error (`detail`, above) often names nothing more specific than
    /// `uncleanShutdown` or `ioOnClosedChannel`: the client only sees the
    /// peer close without a TLS alert, and the substantive reason, when
    /// there is one, is on the server that raised it. Printing nothing when
    /// `harnessStderr()` is empty (as this used to) reads the same as the
    /// harness's own diagnostics never having run at all — round 4's own
    /// failure of `acceptsAndRegistersAgainstARealServer` hit exactly that:
    /// the harness genuinely captured nothing, because round 3's wrapping
    /// only covered the TLS handshake and that failure's cause was a step
    /// after it, so this printed no "harness stderr" section and looked
    /// like the capture had silently broken rather than like the diagnostic
    /// coverage having a gap. A test pass never depends on this text.
    private func recordFailure(_ message: String, harnessStderr: () -> String) {
        let stderr = harnessStderr()
        Issue.record("\(message)\nharness stderr:\n\(stderr.isEmpty ? "(empty)" : stderr)")
    }

    @Test func acceptsAndRegistersAgainstARealServer() throws {
        guard Self.canRunIntegrationTest() else { return }
        let (outcome, sessionsAfter, harnessStderr) = try runScenario(
            serverEKU: true, serverURIs: ["convivy://machine/bosun-test"], foreignServerRoot: false)

        guard case .registered(let sessionID, let sessionToken) = outcome else {
            recordFailure("expected a registration, got \(outcome)", harnessStderr: harnessStderr)
            return
        }
        #expect(!sessionID.isEmpty)
        #expect(!sessionToken.isEmpty)
        #expect(sessionsAfter == 1)
    }

    @Test func refusesAWrongURIName() throws {
        guard Self.canRunIntegrationTest() else { return }
        let (outcome, sessionsAfter, harnessStderr) = try runScenario(
            serverEKU: true, serverURIs: ["convivy://machine/some-other-host"], foreignServerRoot: false)

        guard case .serverCertificateRefused(let reason) = outcome else {
            recordFailure("expected a server-certificate refusal, got \(outcome)", harnessStderr: harnessStderr)
            return
        }
        guard case .uriNameMismatch = reason else {
            recordFailure("wrong reason: \(reason)", harnessStderr: harnessStderr)
            return
        }
        #expect(sessionsAfter == 0)
    }

    @Test func refusesAMissingServerAuth() throws {
        guard Self.canRunIntegrationTest() else { return }
        let (outcome, sessionsAfter, harnessStderr) = try runScenario(
            serverEKU: false, serverURIs: ["convivy://machine/bosun-test"], foreignServerRoot: false)

        guard case .serverCertificateRefused(let reason) = outcome else {
            recordFailure("expected a server-certificate refusal, got \(outcome)", harnessStderr: harnessStderr)
            return
        }
        guard case .missingServerAuth = reason else {
            recordFailure("wrong reason: \(reason)", harnessStderr: harnessStderr)
            return
        }
        #expect(sessionsAfter == 0)
    }

    @Test func refusesAForeignRoot() throws {
        guard Self.canRunIntegrationTest() else { return }
        let (outcome, sessionsAfter, harnessStderr) = try runScenario(
            serverEKU: true, serverURIs: ["convivy://machine/bosun-test"], foreignServerRoot: true)

        guard case .serverCertificateRefused(let reason) = outcome else {
            recordFailure("expected a server-certificate refusal, got \(outcome)", harnessStderr: harnessStderr)
            return
        }
        guard case .chainInvalid = reason else {
            recordFailure("wrong reason: \(reason)", harnessStderr: harnessStderr)
            return
        }
        #expect(sessionsAfter == 0)
    }

    /// Round 7's own regression: the real incident (2026-10-15) was never
    /// a server-certificate refusal at all — the Individuator's own
    /// machine key failed to sign during the handshake, and that real
    /// cause was masked by `ChannelError.ioOnClosedChannel` before this
    /// fix. Run once against the unfixed code (before
    /// `BosunRegistrationClient.preferRealCause` existed) to confirm this
    /// assertion on `detail` actually fails there, rather than passing
    /// for an unrelated reason.
    @Test func aSigningFailureSurfacesItsOwnDetailRatherThanIOOnClosedChannel() throws {
        guard Self.canRunIntegrationTest() else { return }
        let brokenKey = ThrowingSignatureMachineKey()
        let (outcome, _, harnessStderr) = try runScenario(
            serverEKU: true, serverURIs: ["convivy://machine/bosun-test"], foreignServerRoot: false,
            machineKey: brokenKey)

        guard case .transportError(let detail) = outcome else {
            recordFailure("expected a transport error, got \(outcome)", harnessStderr: harnessStderr)
            return
        }
        #expect(detail.contains("machine key signing failed"))
        #expect(detail.contains(ThrowingSignatureMachineKey.failureDescription))
        let lowered = detail.lowercased()
        #expect(!lowered.contains("ioonclosedchannel"))
        #expect(!lowered.contains("i/o on closed channel"))
    }

    /// A server that refuses the client's certificate mid-handshake (here,
    /// against a DIFFERENT trust anchor than the one that actually signed
    /// it). The server's own TLS stack sends an `unknown_ca` alert for
    /// this refusal, and CI (run 38807111481) shows it reaching the
    /// client: the fixed client's `detail` is a `BoringSSLError` carrying
    /// `TLSV1_ALERT_UNKNOWN_CA`.
    ///
    /// Round 1 review finding (PR #912): this test's own name used to claim
    /// the detail names the handshake error, which this scenario's
    /// observed alert confirms. An earlier reading of swift-nio-ssl's TLS
    /// 1.3 handling — that the client sees only an alert-less
    /// `NIOSSLError.uncleanShutdown` here — is what drove this test to
    /// assert that shape instead; that reading does not match what ran.
    /// This test's own value is in pinning `detail` to the named handshake
    /// error — ruling out a wrong classification (a registration, a
    /// server-certificate refusal) and the two uninformative shapes,
    /// `ChannelError.ioOnClosedChannel` and `NIOSSLError.uncleanShutdown`
    /// — not in distinguishing this PR's fix, which the signing-failure
    /// and silent-peer tests below already do.
    @Test func aServerThatClosesMidHandshakeYieldsADetailNamingTheHandshakeError() throws {
        guard Self.canRunIntegrationTest() else { return }
        let dir = try tempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }

        let clientRoot = try TestCertificateAuthority.makeRoot(commonName: "test-client-root")
        let serverTrustedRoot = try TestCertificateAuthority.makeRoot(commonName: "a-different-client-root")

        let (trustAnchorUnused, serverCert, serverKey, clientCertPEM, machineKey) = try writeCertificates(
            dir: dir, serverRoot: clientRoot, clientRoot: clientRoot, serverEKU: true,
            serverURIs: ["convivy://machine/bosun-test"])
        _ = trustAnchorUnused

        // The harness is told to trust `serverTrustedRoot` for CLIENT
        // certificates, but `clientCertPEM` above is signed by `clientRoot`
        // — a different root — so the server's own handshake refuses it.
        let serverTrustAnchorURL = dir.appendingPathComponent("server-trust-anchor.pem")
        try serverTrustedRoot.certificatePEM.write(to: serverTrustAnchorURL, atomically: true, encoding: .utf8)

        let sessionsDB = dir.appendingPathComponent("sessions.db")
        let harness = try PythonBosunHarness(
            repoRoot: Self.repoRoot, pythonExecutable: Self.pythonExecutable, trustAnchor: serverTrustAnchorURL,
            serverCert: serverCert, serverKey: serverKey, sessionsDB: sessionsDB)
        defer { harness.terminateIfRunning() }

        let clientTrustAnchorURL = dir.appendingPathComponent("client-trust-anchor-for-server-cert.pem")
        try clientRoot.certificatePEM.write(to: clientTrustAnchorURL, atomically: true, encoding: .utf8)
        let config = IndividuatorRootConfig(
            bosunHost: "bosun-test", bosunURIName: "convivy://machine/bosun-test",
            bosunAddress: "127.0.0.1:\(harness.port)", rootCAPath: clientTrustAnchorURL.path, operatorUID: 0)
        let client = BosunRegistrationClient(config: config, machineKey: machineKey, clientCertificatePEM: clientCertPEM)

        let outcome = client.register(
            account: "test-operator", callerKind: "operator", ceiling: #"["path:/work/allowed"]"#, protocolVersion: "1")
        let sessionsAfter = try harness.querySessionCount()
        harness.quit()

        guard case .transportError(let detail) = outcome else {
            recordFailure("expected a transport error, got \(outcome)", harnessStderr: { harness.capturedStderr })
            return
        }
        let lowered = detail.lowercased()
        #expect(lowered.contains("tlsv1_alert_unknown_ca"))
        #expect(!lowered.contains("ioonclosedchannel"))
        #expect(!lowered.contains("i/o on closed channel"))
        #expect(!lowered.contains("uncleanshutdown"))
        #expect(sessionsAfter == 0)
    }

    /// Round 8's own regression: a peer that accepts the TCP connection
    /// and then never speaks at all — no ServerHello, no data, no close —
    /// left `BosunRegistrationClient` blocked in `channel
    /// .writeAndFlush(...).wait()` forever before `responseTimeoutSeconds`
    /// covered this step (this run's own occurrence, 2026-10-15: CI's
    /// `swift` job hung for 59 minutes on exactly this unbounded wait).
    /// No Python harness here — `IndividuatorNetwork` already depends on
    /// NIOPosix for the client itself, so a plain `ServerBootstrap` with a
    /// handler that does nothing is this test's whole "server", and it
    /// needs the same loopback bind this suite's other tests do, hence
    /// the same `canRunIntegrationTest()` gate.
    @Test func aPeerThatAcceptsAndGoesSilentTimesOutRatherThanHangingForever() throws {
        guard Self.canRunIntegrationTest() else { return }

        final class SilentHandler: ChannelInboundHandler {
            typealias InboundIn = ByteBuffer
        }

        let group = MultiThreadedEventLoopGroup(numberOfThreads: 1)
        defer { try? group.syncShutdownGracefully() }
        let server = try ServerBootstrap(group: group)
            .childChannelInitializer { channel in
                channel.pipeline.addHandler(SilentHandler())
            }
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
            serial: 101, subjectCN: "test-machine-1", publicKey: machineKey.publicKey,
            extensions: [
                TestCertificateAuthority.ekuExtension(oids: [OID.clientAuth]),
                TestCertificateAuthority.sanExtension(uris: ["convivy://machine/test-machine-1"]),
            ])
        let trustAnchorURL = dir.appendingPathComponent("client-trust-anchor.pem")
        try clientRoot.certificatePEM.write(to: trustAnchorURL, atomically: true, encoding: .utf8)

        let config = IndividuatorRootConfig(
            bosunHost: "bosun-test", bosunURIName: "convivy://machine/bosun-test",
            bosunAddress: "127.0.0.1:\(port)", rootCAPath: trustAnchorURL.path, operatorUID: 0)
        let client = BosunRegistrationClient(
            config: config, machineKey: machineKey, clientCertificatePEM: clientLeaf.pem,
            responseTimeoutSeconds: 2)

        let start = ContinuousClock.now
        let outcome = client.register(
            account: "test-operator", callerKind: "operator", ceiling: #"["path:/work/allowed"]"#, protocolVersion: "1")
        let elapsed = start.duration(to: .now)

        // Well above the 2s deadline passed above (handshake + write +
        // response all share it), and well below anything that would
        // read as this test having silently reintroduced the hang.
        #expect(elapsed < .seconds(15))
        guard case .transportError(let detail) = outcome else {
            Issue.record("expected a transport error, got \(outcome)")
            return
        }
        #expect(detail.lowercased().contains("timed out"))
    }
}

/// A `MachineKey` whose `signature(for:)` always fails — the handshake's
/// own certificate still carries a real, valid public key (`inner`'s), so
/// what fails is specifically the signing step a TLS 1.3 CertificateVerify
/// needs, not certificate parsing or verification.
private struct ThrowingSignatureMachineKey: MachineKey {
    static let failureDescription = "deliberately broken for round 7's own masking test"

    struct SigningFailure: Error, CustomStringConvertible {
        var description: String { ThrowingSignatureMachineKey.failureDescription }
    }

    private let inner = SoftwareMachineKey()

    var publicKey: P256.Signing.PublicKey { inner.publicKey }

    func signature<D: DataProtocol>(for data: D) throws -> P256.Signing.ECDSASignature {
        throw SigningFailure()
    }
}

private extension TestCertificateAuthority {
    /// Writes this CA's own certificate PEM to `dir` once, so
    /// `BosunRegistrationClient`'s `rootCAPath` (a file path, not a PEM
    /// string) has a real file to read when the client's own trust root is
    /// the same CA already written as `client-trust-anchor.pem`.
    func pemPath(in dir: URL) -> String {
        dir.appendingPathComponent("client-trust-anchor.pem").path
    }
}

/// Drives one `bosun_test_harness.py` subprocess (see that file's own doc
/// comment for the stdin/stdout protocol).
///
/// Round 9's own occurrence (2026-10-15): nothing below used to bound how
/// long it would wait on the subprocess — the `PORT` read in `init`, the
/// `SESSIONS` read in `querySessionCount`, and `waitUntilExit()` in `quit()`
/// all blocked however long their underlying `read()`/`waitpid()` took, with
/// no deadline of their own. `readLine` below now polls with a deadline
/// instead (`portTimeout` for `PORT`, `readTimeout` for `SESSIONS` and
/// `quit()`'s own wait), and `quit()` escalates through terminate then kill
/// rather than waiting forever.
private final class PythonBosunHarness {
    enum HarnessError: Error, CustomStringConvertible {
        case didNotStart(String)
        case timedOut(String)
        var description: String {
            switch self {
            case .didNotStart(let detail): return "the Python test harness did not start: \(detail)"
            case .timedOut(let detail): return "the Python test harness timed out: \(detail)"
            }
        }
    }

    /// How long `querySessionCount`'s `SESSIONS` read, and `quit()`'s wait
    /// for the process to exit on its own, may block before giving up.
    /// Comfortably above the server's own worst-case per-connection
    /// timeouts (`bosun_registration.py`'s `TLS_HANDSHAKE_TIMEOUT_S` +
    /// `TLS_REQUEST_TIMEOUT_S` + `TLS_RESPONSE_TIMEOUT_S`, 50s today),
    /// comfortably below the suite's own `.timeLimit(.minutes(3))` backstop
    /// so a genuine hang here fails under this class's own named reason
    /// first.
    private static let readTimeout: TimeInterval = 15

    /// How long `init` may wait for the harness's `PORT` line specifically.
    /// Round 10's own occurrence (2026-10-15, run 38792747476/116621291433)
    /// showed main's own `acceptsAndRegistersAgainstARealServer`, run
    /// alone, taking 35.839s end to end on the CI runner — this suite's own
    /// `.serialized` (above) should remove the thread-pool starvation that
    /// pushed the harness's startup past the old 15s deadline, but 90s
    /// stays comfortably above that single-test figure as the deadline for
    /// just the PORT wait, rather than retuning it to a number this run
    /// cannot itself confirm is still enough once the real cause is fixed.
    private static let portTimeout: TimeInterval = 90

    /// Holds the harness's captured stderr behind a lock, as its own object
    /// rather than a pair of properties on `PythonBosunHarness` itself —
    /// `readabilityHandler` (below) needs to start draining before `init`
    /// has set every one of `PythonBosunHarness`'s own stored properties
    /// (`port` isn't known yet), and a closure that captures `self` and
    /// escapes into another object's property, which assigning to
    /// `readabilityHandler` does, cannot run until `self` is fully
    /// initialized. Capturing this object instead, rather than `self`, has
    /// no such restriction.
    private final class StderrCapture {
        private let lock = NSLock()
        private var buffer = Data()
        func append(_ data: Data) {
            lock.lock()
            buffer.append(data)
            lock.unlock()
        }
        var text: String {
            lock.lock()
            defer { lock.unlock() }
            return String(decoding: buffer, as: UTF8.self)
        }
    }

    private let process: Process
    private let stdinHandle: FileHandle
    private let stdoutHandle: FileHandle
    private let stderrHandle: FileHandle
    private var pending = Data()
    let port: Int

    /// The harness's own stderr, drained from the moment the process
    /// launches (`readabilityHandler` below), not only once `init` has
    /// read the `PORT` line — round 10's own occurrence (2026-10-15) found
    /// this capture starting too late to say anything about a harness that
    /// fails or stalls before printing `PORT`, which is exactly the
    /// scenario a PORT timeout needs it for (see the `didNotStart`/
    /// `timedOut` detail below). `quit()` needing to wait for the process
    /// before it could read this, which an earlier round fixed, is a
    /// separate concern from when draining starts.
    private let stderrCapture: StderrCapture
    var capturedStderr: String { stderrCapture.text }

    init(repoRoot: URL, pythonExecutable: String, trustAnchor: URL, serverCert: URL, serverKey: URL, sessionsDB: URL) throws {
        let script = repoRoot.appendingPathComponent("individuator/Tests/support/bosun_test_harness.py")
        let libDir = repoRoot.appendingPathComponent("lib/agent-platform")

        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        process.arguments = [
            pythonExecutable, script.path,
            "--lib-dir", libDir.path,
            "--trust-anchor", trustAnchor.path,
            "--server-cert", serverCert.path,
            "--server-key", serverKey.path,
            "--sessions-db", sessionsDB.path,
        ]
        let stdinPipe = Pipe()
        let stdoutPipe = Pipe()
        let stderrPipe = Pipe()
        process.standardInput = stdinPipe
        process.standardOutput = stdoutPipe
        process.standardError = stderrPipe

        let stderrCapture = StderrCapture()
        self.process = process
        self.stdinHandle = stdinPipe.fileHandleForWriting
        self.stdoutHandle = stdoutPipe.fileHandleForReading
        self.stderrHandle = stderrPipe.fileHandleForReading
        self.stderrCapture = stderrCapture

        // Attached before `process.run()`, capturing `stderrCapture` rather
        // than `self`, so stderr is drained from the very first byte the
        // harness writes — including anything it writes while failing or
        // stalling before the `PORT` line below.
        stderrHandle.readabilityHandler = { handle in
            let chunk = handle.availableData
            if chunk.isEmpty {
                handle.readabilityHandler = nil
                return
            }
            stderrCapture.append(chunk)
        }

        try process.run()
        let startedAt = Date()

        do {
            guard let line = try Self.readLine(
                from: stdoutHandle, pending: &pending, timeout: Self.portTimeout, what: "PORT"),
                  line.hasPrefix("PORT ") else {
                throw HarnessError.didNotStart("no PORT line within \(Self.portTimeout)s of startup")
            }
            guard let port = Int(line.dropFirst("PORT ".count)) else {
                throw HarnessError.didNotStart("malformed PORT line: \(line)")
            }
            self.port = port
            // Visible in CI's log (the `swift` job's `script -q -e
            // /dev/null` gives `swift test` a pty, so this streams live
            // rather than waiting for the process to exit) — round 10's own
            // occurrence had no record of how long the harness actually
            // took to print `PORT`, only that four tests hit the same old
            // 15s deadline at once, which this makes checkable directly
            // against `.serialized`'s own fix rather than inferred from it.
            print("PythonBosunHarness: PORT line read after \(String(format: "%.3f", Date().timeIntervalSince(startedAt)))s")
        } catch {
            // `process.run()` above has already launched the subprocess —
            // a throw from here out of `init` means the caller never gets
            // a `PythonBosunHarness` to call `terminateIfRunning()` on, so
            // this is the only place left that can stop it from leaking.
            // Still running at this point means `error` is the PORT
            // timeout/failure itself; captured here before `terminate`
            // below can turn it into an exit the enriched detail would
            // otherwise report as "exited".
            let stillRunning = process.isRunning
            Self.terminate(process)
            let exitStatus = process.terminationStatus
            let stderrText = stderrCapture.text
            if let harnessError = error as? HarnessError {
                throw Self.enriched(
                    harnessError, processExited: !stillRunning, exitStatus: exitStatus, stderr: stderrText)
            }
            throw error
        }
    }

    /// Adds the harness's exit status (when it has one) and its captured
    /// stderr to `error`'s own detail, keeping `error`'s case — a PORT
    /// timeout stays a `.timedOut`, a malformed line stays a
    /// `.didNotStart` — so the caller's own description still names which
    /// one happened, with the added detail telling why.
    private static func enriched(
        _ error: HarnessError, processExited: Bool, exitStatus: Int32, stderr: String
    ) -> HarnessError {
        var detail: String
        switch error {
        case .didNotStart(let d): detail = d
        case .timedOut(let d): detail = d
        }
        if processExited {
            detail += " (process had already exited with status \(exitStatus))"
        }
        detail += "\nharness stderr:\n\(stderr.isEmpty ? "(empty)" : stderr)"
        switch error {
        case .didNotStart: return .didNotStart(detail)
        case .timedOut: return .timedOut(detail)
        }
    }

    /// Throws `HarnessError.timedOut` (naming the `SESSIONS` read) rather
    /// than returning a sentinel on a hang, so a caller that does not
    /// bother to check a `-1` still fails loudly instead of asserting on a
    /// value a timeout fabricated.
    func querySessionCount() throws -> Int {
        send("QUERY_SESSIONS")
        guard let line = try Self.readLine(
            from: stdoutHandle, pending: &pending, timeout: Self.readTimeout, what: "SESSIONS"),
              line.hasPrefix("SESSIONS ") else {
            return -1
        }
        return Int(line.dropFirst("SESSIONS ".count)) ?? -1
    }

    /// Sends `QUIT` and waits for the process to exit on its own; escalates
    /// to `terminate()` (SIGTERM) and then `SIGKILL` rather than waiting
    /// forever if it does not. Every path through this still ends with the
    /// process gone, so a caller's own `defer { harness.terminateIfRunning() }`
    /// has nothing left to do afterward.
    func quit() {
        send("QUIT")
        if Self.wait(process, upTo: Self.readTimeout) { return }
        process.terminate()
        if Self.wait(process, upTo: 5) { return }
        kill(process.processIdentifier, SIGKILL)
        _ = Self.wait(process, upTo: 5)
    }

    /// Kills the process if it is still running — the `defer`-safe
    /// counterpart to `quit()`, for every exit path (including a thrown
    /// error) that reaches here without having sent `QUIT` first.
    func terminateIfRunning() {
        if process.isRunning {
            Self.terminate(process)
        }
    }

    private static func terminate(_ process: Process) {
        guard process.isRunning else { return }
        process.terminate()
        if wait(process, upTo: 5) { return }
        kill(process.processIdentifier, SIGKILL)
        _ = wait(process, upTo: 5)
    }

    private static func wait(_ process: Process, upTo timeout: TimeInterval) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while process.isRunning {
            if Date() >= deadline { return false }
            usleep(20_000)
        }
        return true
    }

    private func send(_ line: String) {
        stdinHandle.write((line + "\n").data(using: .utf8)!)
    }

    /// Reads one line from `handle`, polling with `poll(2)` rather than
    /// blocking on `availableData` outright, so a peer that never writes
    /// the line throws `HarnessError.timedOut` instead of hanging this
    /// thread — and, with it, `swift test` — indefinitely.
    private static func readLine(
        from handle: FileHandle, pending: inout Data, timeout: TimeInterval, what: String
    ) throws -> String? {
        let deadline = Date().addingTimeInterval(timeout)
        while true {
            if let idx = pending.firstIndex(of: UInt8(ascii: "\n")) {
                let line = String(decoding: pending[..<idx], as: UTF8.self)
                pending.removeSubrange(...idx)
                return line
            }
            let remaining = deadline.timeIntervalSinceNow
            guard remaining > 0 else {
                throw HarnessError.timedOut("no \(what) line within \(timeout)s")
            }
            // Polled in <=1s slices (rather than the full remaining time in
            // one `poll` call) so a deadline that elapses between `poll`
            // calls is still caught promptly.
            var fds = pollfd(fd: handle.fileDescriptor, events: Int16(POLLIN), revents: 0)
            let sliceMs = Int32(min(remaining, 1) * 1000)
            let rv = poll(&fds, 1, max(sliceMs, 0))
            if rv < 0 {
                if errno == EINTR { continue }
                throw HarnessError.timedOut("poll failed: \(String(cString: strerror(errno)))")
            }
            if rv == 0 { continue }
            let chunk = handle.availableData
            if chunk.isEmpty { return nil }
            pending.append(chunk)
        }
    }
}
