import CryptoKit
import Foundation
import Testing
@testable import IndividuatorCore
@testable import IndividuatorCoreTests
@testable import IndividuatorNetwork

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
@Suite struct BosunRegistrationClientIntegrationTests {
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
        serverEKU: Bool, serverURIs: [String]
    ) throws -> (trustAnchorForServer: URL, serverCert: URL, serverKey: URL, clientCertPEM: String, machineKey: SoftwareMachineKey) {
        let serverKey = P256.Signing.PrivateKey()
        var serverExtensions: [[UInt8]] = []
        if serverEKU { serverExtensions.append(TestCertificateAuthority.ekuExtension(oids: [OID.serverAuth])) }
        if !serverURIs.isEmpty { serverExtensions.append(TestCertificateAuthority.sanExtension(uris: serverURIs)) }
        let serverLeaf = try serverRoot.issueLeaf(
            serial: 100, subjectCN: "bosun-test-server", publicKey: serverKey.publicKey, extensions: serverExtensions)

        let machineKey = SoftwareMachineKey()
        let clientExtensions: [[UInt8]] = [
            TestCertificateAuthority.ekuExtension(oids: [OID.clientAuth]),
            TestCertificateAuthority.sanExtension(uris: ["convivy://machine/test-machine-1"]),
        ]
        let clientLeaf = try clientRoot.issueLeaf(
            serial: 101, subjectCN: "test-machine-1", publicKey: machineKey.publicKey, extensions: clientExtensions)

        let trustAnchorURL = dir.appendingPathComponent("client-trust-anchor.pem")
        try clientRoot.certificatePEM.write(to: trustAnchorURL, atomically: true, encoding: .utf8)
        let serverCertURL = dir.appendingPathComponent("server.pem")
        try serverLeaf.pem.write(to: serverCertURL, atomically: true, encoding: .utf8)
        let serverKeyURL = dir.appendingPathComponent("server-key.pem")
        try serverKey.pemRepresentation.write(to: serverKeyURL, atomically: true, encoding: .utf8)

        return (trustAnchorURL, serverCertURL, serverKeyURL, clientLeaf.pem, machineKey)
    }

    private func runScenario(
        serverEKU: Bool, serverURIs: [String], foreignServerRoot: Bool,
        expectedBosunHost: String = "bosun-test"
    ) throws -> (outcome: RegistrationOutcome, sessionsAfter: Int, harnessStderr: () -> String) {
        let dir = try tempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }

        let clientRoot = try TestCertificateAuthority.makeRoot(commonName: "test-client-root")
        let serverRoot = foreignServerRoot
            ? try TestCertificateAuthority.makeRoot(commonName: "a-foreign-server-root") : clientRoot

        let (trustAnchor, serverCert, serverKey, clientCertPEM, machineKey) = try writeCertificates(
            dir: dir, serverRoot: serverRoot, clientRoot: clientRoot, serverEKU: serverEKU, serverURIs: serverURIs)

        let sessionsDB = dir.appendingPathComponent("sessions.db")
        let harness = try PythonBosunHarness(
            repoRoot: Self.repoRoot, pythonExecutable: Self.pythonExecutable, trustAnchor: trustAnchor,
            serverCert: serverCert, serverKey: serverKey, sessionsDB: sessionsDB)

        let config = IndividuatorRootConfig(
            bosunHost: expectedBosunHost, bosunURIName: "convivy://machine/\(expectedBosunHost)",
            bosunAddress: "127.0.0.1:\(harness.port)", rootCAPath: clientRoot.pemPath(in: dir), operatorUID: 0)
        let client = BosunRegistrationClient(config: config, machineKey: machineKey, clientCertificatePEM: clientCertPEM)

        // A narrow JSON array of `bosun_authz.scope()` strings — the one
        // shape `bosun_authz.ceiling_entries_from_text` accepts besides the
        // literal text "full" (see that function's own doc comment) — so
        // this registers a session scoped to one path rather than an
        // unrestricted one.
        let outcome = client.register(
            account: "test-operator", callerKind: "operator",
            ceiling: #"["path:/work/allowed"]"#, protocolVersion: "1")
        let sessionsAfter = harness.querySessionCount()
        // `quit()` must run before the stderr it captures is read — this
        // scenario's own TLS/registration round-trip is over by this point,
        // so quitting here (rather than in a `defer`) does not race the
        // calls above, and gives every caller a closure it can read from
        // after the fact rather than a string it must already hold.
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
private final class PythonBosunHarness {
    enum HarnessError: Error, CustomStringConvertible {
        case didNotStart(String)
        var description: String {
            switch self {
            case .didNotStart(let detail): return "the Python test harness did not start: \(detail)"
            }
        }
    }

    private let process: Process
    private let stdinHandle: FileHandle
    private let stdoutHandle: FileHandle
    private let stderrHandle: FileHandle
    private var pending = Data()
    let port: Int

    /// The harness's own stderr — empty until `quit()` has read it. Read
    /// only after the process has exited (`readDataToEndOfFile()` in
    /// `quit()`), so this never blocks waiting on a still-running process;
    /// `bosun_test_harness.py`'s own diagnostics are a handful of short
    /// lines per connection attempt, well under one pipe buffer, so nothing
    /// here drains the pipe concurrently while the process runs.
    private(set) var capturedStderr: String = ""

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

        self.process = process
        self.stdinHandle = stdinPipe.fileHandleForWriting
        self.stdoutHandle = stdoutPipe.fileHandleForReading
        self.stderrHandle = stderrPipe.fileHandleForReading

        try process.run()

        guard let line = Self.readLine(from: stdoutHandle, pending: &pending), line.hasPrefix("PORT ") else {
            throw HarnessError.didNotStart("no PORT line on startup")
        }
        guard let port = Int(line.dropFirst("PORT ".count)) else {
            throw HarnessError.didNotStart("malformed PORT line: \(line)")
        }
        self.port = port
    }

    func querySessionCount() -> Int {
        send("QUERY_SESSIONS")
        guard let line = Self.readLine(from: stdoutHandle, pending: &pending), line.hasPrefix("SESSIONS ") else {
            return -1
        }
        return Int(line.dropFirst("SESSIONS ".count)) ?? -1
    }

    func quit() {
        send("QUIT")
        process.waitUntilExit()
        let data = stderrHandle.readDataToEndOfFile()
        capturedStderr = String(decoding: data, as: UTF8.self)
    }

    private func send(_ line: String) {
        stdinHandle.write((line + "\n").data(using: .utf8)!)
    }

    private static func readLine(from handle: FileHandle, pending: inout Data) -> String? {
        while true {
            if let idx = pending.firstIndex(of: UInt8(ascii: "\n")) {
                let line = String(decoding: pending[..<idx], as: UTF8.self)
                pending.removeSubrange(...idx)
                return line
            }
            let chunk = handle.availableData
            if chunk.isEmpty { return nil }
            pending.append(chunk)
        }
    }
}
