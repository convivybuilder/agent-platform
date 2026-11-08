import Foundation
import Testing
@testable import IndividuatorDaemonCore
@testable import IndividuatorNetwork
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif

extension PoolBlockingIntegrationSuites {

/// `ForwardingSocketServer`'s accept loop (round 1 finding on PR 1121,
/// E12·2 slice S5b): one admitted account that connects and sends nothing
/// must not stall the daemon's forwarding of every other account's
/// request. `ForwardingConnectionHandlerTests.swift` pins the per-connection
/// read deadline directly against `handler.handle`; this file pins the
/// companion half, that the accept loop itself hands each connection off to
/// its own thread rather than serving them one at a time.
///
/// Round 2 added `.serialized` + a time limit believing that was the whole
/// CI hang fix (this suite's own two tests, run with Swift Testing's
/// default full parallelism, each making a real `accept()`/`connect()`
/// syscall directly on its test body rather than off the thread that call
/// runs on — a blocking call holding a cooperative-pool thread for the
/// length of the call). Round 3 found `.serialized` only bounds that
/// within THIS suite; nesting it under `PoolBlockingIntegrationSuites`
/// (see that type's own doc comment) is what bounds it against the other
/// suites in this target that make the same kind of blocking call, which
/// is what the pool actually ran out of threads for.
///
/// Round 3 also found a second, unrelated bug this suite's own hang had
/// been masking: both tests' `server.run()` always threw
/// `DaemonSocketDirectoryError.directoryOwnerWrong` for `/`, silently
/// (`try?`), because `DaemonSocketDirectory.prepare`'s ancestor walk
/// starts at the real filesystem root, which is root-owned on every real
/// system — no non-root test process can ever satisfy that check, for any
/// directory, regardless of its path's length. `makeServer` below now
/// supplies `ForwardingSocketServer`'s test-only `prepareDirectory` seam
/// instead, which validates the synthetic directory this file builds
/// without walking the real `/`, and both tests now record whatever
/// `server.run()` actually threw rather than discarding it.
@Suite(.serialized, .timeLimit(.minutes(2))) struct ForwardingSocketServerTests {
    private static let widgetRoute = ForwardingRoute(method: "GET", path: "/v1/widgets/{id}")
    private static let allowlist = [ForwardingRouteEntry(route: widgetRoute, destination: .bosun)]

    /// Always answers `200`, scripted rather than a real network call — the
    /// same idiom `ForwardingConnectionHandlerTests.RecordingUpstreamTransport`
    /// uses, trimmed to what this file needs: nothing here inspects what
    /// was forwarded, only whether a response came back at all.
    private final class StubUpstreamTransport: ForwardingUpstreamTransport {
        func forward(address: String, requestBytes: [UInt8]) -> ForwardingUpstreamOutcome {
            .responded(statusCode: 200, responseBytes: Array("HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n".utf8))
        }
    }

    /// Binds a throwaway `AF_UNIX` socket to prove the sandbox allows it at
    /// all before any test below relies on `ForwardingSocketServer`'s own
    /// real `bind`/`listen`/`accept` — the same positive-control idiom
    /// `DaemonSocketDirectoryTests.canBindAFUnixSocket` uses, duplicated
    /// here since that one is private to its own file.
    private static let canBindAFUnixSocket: Bool = {
        let path = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString).path
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { return false }
        defer { close(fd) }
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let pathBytes = Array(path.utf8)
        withUnsafeMutableBytes(of: &addr.sun_path) { raw in
            let buffer = raw.bindMemory(to: UInt8.self)
            for (i, byte) in pathBytes.enumerated() { buffer[i] = byte }
            buffer[pathBytes.count] = 0
        }
        let size = MemoryLayout<sockaddr_un>.size
        let ok = withUnsafePointer(to: &addr) { ptr -> Bool in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, socklen_t(size)) == 0 }
        }
        if ok { unlink(path) }
        return ok
    }()

    /// Set by CI's `swift` job, the same variable `DaemonSocketDirectoryTests`
    /// and `ForwardingUpstreamClientIntegrationTests` already turn their own
    /// bind-refusal skip into a failure with: turns `canBindAFUnixSocket`
    /// being false here into a recorded failure too, so a future CI
    /// environment that refuses `AF_UNIX` bind cannot silently lose this
    /// file's coverage of the accept loop's round 1 fix while the job stays
    /// green. Unset in this sandbox, where the skip below is correct — a
    /// standalone check confirms this very sandbox's own `bind(2)` on an
    /// `AF_UNIX` path returns `EPERM`.
    private static let requireBoundSocketTests: Bool =
        ProcessInfo.processInfo.environment["INDIVIDUATOR_REQUIRE_MTLS_INTEGRATION_TEST"] != nil

    private static func canRunBoundSocketTest() -> Bool {
        if canBindAFUnixSocket { return true }
        if requireBoundSocketTests {
            let message = "ForwardingSocketServer's accept-loop tests cannot run (the sandbox refuses "
                + "AF_UNIX bind) and INDIVIDUATOR_REQUIRE_MTLS_INTEGRATION_TEST is set, so CI requires "
                + "them to run rather than skip"
            Issue.record("\(message)")
        }
        return false
    }

    /// Holds whatever `server.run()` throws on its own `Thread`, behind a
    /// lock — round 3's own regression: `try? server.run()` used to
    /// discard that error entirely, which is why `directoryOwnerWrong`
    /// (see this suite's own doc comment) surfaced only as an opaque
    /// "could not connect to the forwarding socket" rather than its real
    /// cause.
    private final class ThrownErrorBox {
        private let lock = NSLock()
        private var error: Error?
        func set(_ error: Error) { lock.lock(); self.error = error; lock.unlock() }
        var value: Error? { lock.lock(); defer { lock.unlock() }; return error }
    }

    /// Builds the server's own socket directory short enough to stay well
    /// under `sockaddr_un`'s `sun_path` limit (104 bytes on Darwin)
    /// regardless of how deep the runner's own temporary directory is —
    /// an 8-character suffix and a one-character socket name, rather than
    /// a full UUID and `forwarding.sock` — and supplies
    /// `ForwardingSocketServer`'s test-only `prepareDirectory` seam so
    /// `bindAndListen` validates this directory directly
    /// (`DaemonSocketDirectory.prepareDirectory` + `removeStaleSocketIfSafe`)
    /// instead of walking the real filesystem root, which no non-root test
    /// process can ever pass — see this suite's own doc comment.
    private func makeServer(requestReadTimeoutSeconds: TimeInterval) throws -> (server: ForwardingSocketServer, socketPath: String) {
        let suffix = UUID().uuidString.prefix(8)
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("fss-\(suffix)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        chmod(dir.path, 0o755)  // `DaemonSocketDirectory.prepareDirectory` refuses anything else.
        let socketPath = dir.appendingPathComponent("s").path

        let handler = ForwardingConnectionHandler(
            admittedUIDs: [getuid()], allowlist: Self.allowlist, libraryAddress: nil, bosunAddress: "127.0.0.1:9",
            upstream: StubUpstreamTransport(), logSink: { _ in },
            requestReadTimeoutSeconds: requestReadTimeoutSeconds)
        let server = ForwardingSocketServer(
            socketPath: socketPath, handler: handler, requiredDirectoryOwnerUID: getuid(),
            prepareDirectory: { directoryPath, socketPath, requiredOwnerUID in
                try DaemonSocketDirectory.prepareDirectory(path: directoryPath, requiredOwnerUID: requiredOwnerUID)
                try DaemonSocketDirectory.removeStaleSocketIfSafe(socketPath: socketPath, requiredOwnerUID: requiredOwnerUID)
            })
        return (server, socketPath)
    }

    /// Runs `server.run()` on its own `Thread`, capturing whatever it
    /// throws in `errorBox` rather than discarding it.
    private func startServer(_ server: ForwardingSocketServer, errorBox: ThrownErrorBox) {
        let thread = Thread {
            do { try server.run() } catch { errorBox.set(error) }
        }
        thread.start()
    }

    /// Connects to `path`, retrying for up to one second — `server.run()`
    /// binds and starts listening on its own thread, asynchronously with
    /// this test, so the first connect attempt can legitimately arrive
    /// before `listen()` has run. Bounded at one second total, so a server
    /// that never binds (including one whose thread has already thrown)
    /// fails this call rather than retrying forever.
    private func connectWithRetry(to path: String) -> Int32? {
        let deadline = Date().addingTimeInterval(1.0)
        while Date() < deadline {
            let fd = socket(AF_UNIX, SOCK_STREAM, 0)
            guard fd >= 0 else { return nil }
            var addr = sockaddr_un()
            addr.sun_family = sa_family_t(AF_UNIX)
            let pathBytes = Array(path.utf8)
            withUnsafeMutableBytes(of: &addr.sun_path) { raw in
                let buffer = raw.bindMemory(to: UInt8.self)
                for (i, byte) in pathBytes.enumerated() { buffer[i] = byte }
                buffer[pathBytes.count] = 0
            }
            let size = MemoryLayout<sockaddr_un>.size
            let result = withUnsafePointer(to: &addr) { ptr -> Int32 in
                ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { connect(fd, $0, socklen_t(size)) }
            }
            if result == 0 {
                SocketIO.setNoSIGPIPE(fileDescriptor: fd)
                return fd
            }
            close(fd)
            Thread.sleep(forTimeInterval: 0.01)
        }
        return nil
    }

    private func sendRaw(_ fd: Int32, _ text: String) {
        let data = Data(text.utf8)
        data.withUnsafeBytes { ptr in _ = write(fd, ptr.baseAddress, ptr.count) }
    }

    /// Reads until the peer closes, or `deadline` elapses — round 3's own
    /// fix: this used to block on `read(2)` with no bound at all, so a
    /// server that accepted the connection and then never closed it (a
    /// hang this file exists to catch, not assume away) would hang this
    /// test forever rather than failing it. Polls with `poll(2)` in <=1s
    /// slices, the same idiom `PythonBosunHarness.readLine` uses.
    private func readAllUntilClose(_ fd: Int32, deadline: TimeInterval = 5.0) -> [UInt8] {
        var result: [UInt8] = []
        var buffer = [UInt8](repeating: 0, count: 4096)
        let giveUpAt = Date().addingTimeInterval(deadline)
        while true {
            let remaining = giveUpAt.timeIntervalSinceNow
            guard remaining > 0 else { break }
            var fds = pollfd(fd: fd, events: Int16(POLLIN), revents: 0)
            let sliceMs = Int32(min(remaining, 1) * 1000)
            let rv = poll(&fds, 1, max(sliceMs, 0))
            if rv < 0 {
                if errno == EINTR { continue }
                break
            }
            if rv == 0 { continue }
            let n = buffer.withUnsafeMutableBytes { ptr in read(fd, ptr.baseAddress, 4096) }
            if n <= 0 { break }
            result.append(contentsOf: buffer[0..<n])
        }
        return result
    }

    /// The accept loop's own round 1 fix: a connection stalled past its own
    /// read deadline is closed with a named reason, on its own thread,
    /// without the accept loop — or any other admitted account's
    /// connection — ever waiting on it.
    @Test func aClientThatSendsNothingIsCutOffAtTheDeadlineWithoutBlockingTheAcceptLoop() throws {
        guard Self.canRunBoundSocketTest() else { return }
        let (server, socketPath) = try makeServer(requestReadTimeoutSeconds: 0.2)
        let errorBox = ThrownErrorBox()
        startServer(server, errorBox: errorBox)

        guard let silentFD = connectWithRetry(to: socketPath) else {
            let detail = errorBox.value.map { "\($0)" } ?? "(server.run() has not thrown anything yet)"
            Issue.record("could not connect to the forwarding socket: \(detail)")
            return
        }
        defer { close(silentFD) }

        let start = Date()
        let response = String(decoding: readAllUntilClose(silentFD), as: UTF8.self)
        let elapsed = Date().timeIntervalSince(start)

        #expect(response.hasPrefix("HTTP/1.1 408"))
        #expect(elapsed < 5.0, "the silent connection should be cut off at its own ~0.2s deadline, not left open")
    }

    /// The scenario finding 2 names directly: one admitted account connects
    /// and sends nothing, and a SECOND connection must still be served
    /// promptly rather than queueing behind the first one inside the accept
    /// loop.
    @Test func aSecondClientIsServedWhileTheFirstIsStalled() throws {
        guard Self.canRunBoundSocketTest() else { return }
        // Long enough that the silent connection is still open, unserved,
        // for the whole rest of this test — if the accept loop were still
        // serial, the second client below would block for this entire
        // duration waiting its turn.
        let (server, socketPath) = try makeServer(requestReadTimeoutSeconds: 30)
        let errorBox = ThrownErrorBox()
        startServer(server, errorBox: errorBox)

        guard let silentFD = connectWithRetry(to: socketPath) else {
            let detail = errorBox.value.map { "\($0)" } ?? "(server.run() has not thrown anything yet)"
            Issue.record("could not connect to the forwarding socket: \(detail)")
            return
        }
        defer { close(silentFD) }

        guard let activeFD = connectWithRetry(to: socketPath) else {
            let detail = errorBox.value.map { "\($0)" } ?? "(server.run() has not thrown anything yet)"
            Issue.record("could not open the second connection: \(detail)")
            return
        }
        sendRaw(activeFD, "GET /v1/widgets/abc HTTP/1.1\r\nHost: test\r\n\r\n")

        let start = Date()
        let response = String(decoding: readAllUntilClose(activeFD), as: UTF8.self)
        let elapsed = Date().timeIntervalSince(start)
        close(activeFD)

        #expect(response.hasPrefix("HTTP/1.1 200"))
        #expect(elapsed < 5.0, "the second client must not wait on the first connection's own 30s deadline")
    }
}

} // extension PoolBlockingIntegrationSuites
