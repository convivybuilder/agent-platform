import CryptoKit
import Foundation
import NIOCore
import NIOPosix
import NIOSSL
import Testing
@testable import IndividuatorCore
@testable import IndividuatorCoreTests
@testable import IndividuatorNetwork

extension PoolBlockingIntegrationSuites {

/// Runs `ForwardingUpstreamClient` against a REAL mutual-TLS server — a
/// plain NIOSSL `ServerBootstrap`, not a Python harness, since this is
/// pure Swift-to-Swift plumbing with no cross-language server to match
/// (unlike `BosunRegistrationClient`, which talks to the real
/// `bosun_registration.py`). It is the "test Keel server that counts
/// connections" the build plan's Handoff asks for, used here specifically
/// to pin the one check `ForwardingConnectionHandlerTests.swift`'s
/// scripted transport cannot: that `ForwardingUpstreamClient` really does
/// refuse a Keel destination whose server certificate names the wrong
/// URI, over a real TLS 1.3 handshake, via the exact
/// `BosunMutualTLS.makeSetup` + `BosunServerVerification.verify` path
/// registration and authorization already use.
///
/// Gated the same way `BosunRegistrationClientIntegrationTests` gates its
/// own real-server tests: a sandbox that refuses a loopback TCP bind
/// (`EPERM`) skips rather than fails, UNLESS
/// `INDIVIDUATOR_REQUIRE_MTLS_INTEGRATION_TEST` is set, which turns the
/// skip into a failure so CI cannot silently stop exercising this path.
///
/// Both tests call `ForwardingUpstreamClient.forward`, which blocks its
/// caller in NIO's own `.wait()`, directly on the test body — nested under
/// `PoolBlockingIntegrationSuites` (round 3, PR 1121) for the reason that
/// type's own doc comment gives: this suite's `.serialized` alone only
/// bounds blocking within itself, not against the other suites in this
/// target that also block a pool thread.
@Suite(.serialized, .timeLimit(.minutes(2)))
struct ForwardingUpstreamClientIntegrationTests {
    private static var requireIntegrationTest: Bool {
        ProcessInfo.processInfo.environment["INDIVIDUATOR_REQUIRE_MTLS_INTEGRATION_TEST"] != nil
    }

    private static func canBindLoopback() -> Bool {
        let group = MultiThreadedEventLoopGroup(numberOfThreads: 1)
        defer { try? group.syncShutdownGracefully() }
        do {
            let server = try ServerBootstrap(group: group)
                .childChannelInitializer { $0.eventLoop.makeSucceededVoidFuture() }
                .bind(host: "127.0.0.1", port: 0).wait()
            try? server.close().wait()
            return true
        } catch {
            return false
        }
    }

    private static func canRunIntegrationTest() -> Bool {
        if canBindLoopback() { return true }
        if requireIntegrationTest {
            let message = "the forwarding mTLS integration test cannot run (the sandbox refuses a loopback bind) and "
                + "INDIVIDUATOR_REQUIRE_MTLS_INTEGRATION_TEST is set, so CI requires it to run rather than skip"
            Issue.record("\(message)")
        }
        return false
    }

    private func tempDirectory() throws -> URL {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    /// A one-shot TLS server: accepts exactly the connections it is sent,
    /// counts them, and answers a fixed HTTP response to anything a
    /// handshake completes for, then closes. Also records (round 5,
    /// following the orchestrator's reading of CI job 125695703568) whether
    /// `RespondingHandler.channelRead` ever ran and any `errorCaught` the
    /// server's own child channel saw — a client-certificate verification
    /// failure from `NIOSSLServerHandler` surfaces exactly that way, never
    /// through `channelRead` at all — so a test failure can say which side
    /// of the handshake the client's own `.transportError` is describing.
    private final class CountingTLSServer {
        let port: Int
        private let group: MultiThreadedEventLoopGroup
        private let channel: Channel
        private let counter = ConnectionCounter()
        private let observation = RequestObservation()

        final class ConnectionCounter {
            private let lock = NSLock()
            private var count = 0
            func increment() { lock.lock(); count += 1; lock.unlock() }
            var value: Int { lock.lock(); defer { lock.unlock() }; return count }
        }

        final class RequestObservation {
            private let lock = NSLock()
            private var sawRequest = false
            private var errorDescriptions: [String] = []

            func recordRequestSeen() {
                lock.lock(); defer { lock.unlock() }
                sawRequest = true
            }

            func recordError(_ error: Error) {
                lock.lock(); defer { lock.unlock() }
                errorDescriptions.append("\(type(of: error)): \(error)")
            }

            var requestWasSeen: Bool {
                lock.lock(); defer { lock.unlock() }
                return sawRequest
            }

            var errors: [String] {
                lock.lock(); defer { lock.unlock() }
                return errorDescriptions
            }
        }

        private final class RespondingHandler: ChannelInboundHandler {
            typealias InboundIn = ByteBuffer
            let counter: ConnectionCounter
            let observation: RequestObservation
            init(counter: ConnectionCounter, observation: RequestObservation) {
                self.counter = counter
                self.observation = observation
            }
            func channelActive(context: ChannelHandlerContext) { counter.increment() }
            func channelRead(context: ChannelHandlerContext, data: NIOAny) {
                observation.recordRequestSeen()
                var buffer = context.channel.allocator.buffer(capacity: 64)
                buffer.writeString("HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                context.writeAndFlush(NIOAny(buffer)).whenComplete { _ in context.close(promise: nil) }
            }
        }

        /// Added after `RespondingHandler` so any `errorCaught` an earlier
        /// handler (most importantly `NIOSSLServerHandler` itself, on a
        /// rejected client certificate) fires still reaches this one as it
        /// propagates down the inbound pipeline.
        private final class ServerErrorObserver: ChannelInboundHandler {
            typealias InboundIn = ByteBuffer
            let observation: RequestObservation
            init(observation: RequestObservation) { self.observation = observation }
            func errorCaught(context: ChannelHandlerContext, error: Error) {
                observation.recordError(error)
                context.fireErrorCaught(error)
            }
        }

        init(serverCertificateDER: Data, serverKeyPEM: String, clientTrustRoot: NIOSSLCertificate) throws {
            let serverCert = try NIOSSLCertificate(bytes: Array(serverCertificateDER), format: .der)
            let serverKey = try NIOSSLPrivateKey(bytes: Array(derFromPEM(serverKeyPEM)), format: .der)
            var tlsConfig = TLSConfiguration.makeServerConfigurationWithMTLS(
                certificateChain: [.certificate(serverCert)], privateKey: .privateKey(serverKey),
                trustRoots: .certificates([clientTrustRoot]))
            tlsConfig.minimumTLSVersion = .tlsv13
            tlsConfig.maximumTLSVersion = .tlsv13
            let sslContext = try NIOSSLContext(configuration: tlsConfig)

            let counter = self.counter
            let observation = self.observation
            let group = MultiThreadedEventLoopGroup(numberOfThreads: 1)
            let channel = try ServerBootstrap(group: group)
                .childChannelInitializer { channel in
                    do {
                        try channel.pipeline.syncOperations.addHandler(NIOSSLServerHandler(context: sslContext))
                        try channel.pipeline.syncOperations.addHandler(
                            RespondingHandler(counter: counter, observation: observation))
                        try channel.pipeline.syncOperations.addHandler(ServerErrorObserver(observation: observation))
                        return channel.eventLoop.makeSucceededVoidFuture()
                    } catch {
                        return channel.eventLoop.makeFailedFuture(error)
                    }
                }
                .bind(host: "127.0.0.1", port: 0).wait()
            self.group = group
            self.channel = channel
            self.port = channel.localAddress?.port ?? 0
        }

        var connectionCount: Int { counter.value }
        var requestWasSeen: Bool { observation.requestWasSeen }
        var serverErrors: [String] { observation.errors }

        func shutdown() {
            try? channel.close().wait()
            try? group.syncShutdownGracefully()
        }
    }

    @Test func acceptsAndRespondsOverARealMutualTLSHandshake() throws {
        guard Self.canRunIntegrationTest() else { return }
        let dir = try tempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }

        let root = try TestCertificateAuthority.makeRoot(commonName: "test-forwarding-root")
        let serverKey = P256.Signing.PrivateKey()
        let serverLeaf = try root.issueLeaf(
            serial: 200, subjectCN: "keel-test-server", publicKey: serverKey.publicKey,
            extensions: [
                TestCertificateAuthority.ekuExtension(oids: [OID.serverAuth]),
                TestCertificateAuthority.sanExtension(uris: ["convivy://machine/keel-test"]),
            ])

        let machineKey = SoftwareMachineKey()
        let clientLeaf = try root.issueLeaf(
            serial: 201, subjectCN: "test-machine-1", publicKey: machineKey.publicKey,
            extensions: [
                TestCertificateAuthority.ekuExtension(oids: [OID.clientAuth]),
                TestCertificateAuthority.sanExtension(uris: ["convivy://machine/test-machine-1"]),
            ])

        let server = try CountingTLSServer(
            serverCertificateDER: serverLeaf.der, serverKeyPEM: serverKey.pemRepresentation,
            clientTrustRoot: try NIOSSLCertificate(bytes: Array(root.certificateDER), format: .der))
        defer { server.shutdown() }

        let trustAnchorURL = dir.appendingPathComponent("root.pem")
        try root.certificatePEM.write(to: trustAnchorURL, atomically: true, encoding: .utf8)
        let config = IndividuatorRootConfig(
            bosunHost: "keel-test", bosunURIName: "convivy://machine/keel-test",
            bosunAddress: "127.0.0.1:1", rootCAPath: trustAnchorURL.path,
            admittedAccounts: [AdmittedAccount(uid: 0, account: "operator")])
        let client = ForwardingUpstreamClient(config: config, machineKey: machineKey, clientCertificatePEM: clientLeaf.pem)

        let outcome = client.forward(
            address: "127.0.0.1:\(server.port)", requestBytes: Array("GET /x HTTP/1.1\r\nHost: t\r\n\r\n".utf8))

        guard case .responded(let statusCode, let responseBytes) = outcome else {
            let message: String =
                "expected a response, got \(outcome); server saw a request: \(server.requestWasSeen); "
                + "server errors: \(server.serverErrors)"
            Issue.record("\(message)")
            return
        }
        #expect(statusCode == 200)
        #expect(String(decoding: responseBytes, as: UTF8.self).hasSuffix("ok"))
        #expect(server.connectionCount == 1)
    }

    @Test func refusesAServerCertificateWhoseURINameDoesNotMatch() throws {
        guard Self.canRunIntegrationTest() else { return }
        let dir = try tempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }

        let root = try TestCertificateAuthority.makeRoot(commonName: "test-forwarding-root-2")
        let serverKey = P256.Signing.PrivateKey()
        let serverLeaf = try root.issueLeaf(
            serial: 210, subjectCN: "keel-test-server", publicKey: serverKey.publicKey,
            extensions: [
                TestCertificateAuthority.ekuExtension(oids: [OID.serverAuth]),
                TestCertificateAuthority.sanExtension(uris: ["convivy://machine/some-other-host"]),
            ])

        let machineKey = SoftwareMachineKey()
        let clientLeaf = try root.issueLeaf(
            serial: 211, subjectCN: "test-machine-1", publicKey: machineKey.publicKey,
            extensions: [
                TestCertificateAuthority.ekuExtension(oids: [OID.clientAuth]),
                TestCertificateAuthority.sanExtension(uris: ["convivy://machine/test-machine-1"]),
            ])

        let server = try CountingTLSServer(
            serverCertificateDER: serverLeaf.der, serverKeyPEM: serverKey.pemRepresentation,
            clientTrustRoot: try NIOSSLCertificate(bytes: Array(root.certificateDER), format: .der))
        defer { server.shutdown() }

        let trustAnchorURL = dir.appendingPathComponent("root.pem")
        try root.certificatePEM.write(to: trustAnchorURL, atomically: true, encoding: .utf8)
        let config = IndividuatorRootConfig(
            bosunHost: "keel-test", bosunURIName: "convivy://machine/keel-test",
            bosunAddress: "127.0.0.1:1", rootCAPath: trustAnchorURL.path,
            admittedAccounts: [AdmittedAccount(uid: 0, account: "operator")])
        let client = ForwardingUpstreamClient(config: config, machineKey: machineKey, clientCertificatePEM: clientLeaf.pem)

        let outcome = client.forward(
            address: "127.0.0.1:\(server.port)", requestBytes: Array("GET /x HTTP/1.1\r\nHost: t\r\n\r\n".utf8))

        guard case .serverCertificateRefused(let reason) = outcome else {
            let message: String =
                "expected a server-certificate refusal, got \(outcome); server saw a request: "
                + "\(server.requestWasSeen); server errors: \(server.serverErrors)"
            Issue.record("\(message)")
            return
        }
        guard case .uriNameMismatch = reason else {
            let message: String =
                "wrong reason: \(reason); server saw a request: \(server.requestWasSeen); "
                + "server errors: \(server.serverErrors)"
            Issue.record("\(message)")
            return
        }
    }
}

} // extension PoolBlockingIntegrationSuites
