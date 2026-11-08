import Foundation
import IndividuatorCore
import NIOCore
import NIOPosix
import NIOSSL

/// One forwarding call's outcome: the Keel destination's own raw response
/// (status code, parsed only for the log line, plus the complete raw
/// bytes relayed back verbatim), the Individuator's own refusal of the
/// destination's server certificate, or anything below the HTTP layer.
public enum ForwardingUpstreamOutcome: Equatable {
    case responded(statusCode: Int, responseBytes: [UInt8])
    case serverCertificateRefused(BosunServerRefusalReason)
    case transportError(detail: String)
}

/// The transport `ForwardingConnectionHandler` forwards a matched request
/// against — real networking in `ForwardingUpstreamClient`, a scripted
/// fake in its own tests.
public protocol ForwardingUpstreamTransport {
    func forward(address: String, requestBytes: [UInt8]) -> ForwardingUpstreamOutcome
}

/// The Individuator's forwarding endpoint's own mutual-TLS client (0092:
/// "The Individuator opens the mutual-TLS connection to the Keel itself,
/// presenting its machine certificate... It checks the Keel's server
/// certificate as section 6 requires."). One call is one TCP connection,
/// one TLS 1.3 handshake, one request, and one response — "Each local
/// request gets its own upstream connection, never reused" (build plan
/// slice S5b) — the identical shape `BosunRegistrationClient` already
/// uses for `POST /sessions`, generalised here to an arbitrary method,
/// target, header set and body, since this relays whatever
/// `ForwardingConnectionHandler` already matched against the allowlist
/// rather than constructing one fixed request of its own.
///
/// Reuses `BosunMutualTLS.makeSetup` completely unchanged: both Keel
/// destinations (Library and Bosun) present the same host's server
/// certificate (0092 section 6), so the identical `config.bosunURIName`
/// check that already gates registration and authorization gates every
/// forwarded request too, regardless of which of the two `address` names.
public struct ForwardingUpstreamClient: ForwardingUpstreamTransport {
    public let config: IndividuatorRootConfig
    public let machineKey: any MachineKey
    public let clientCertificatePEM: String
    public let connectTimeoutSeconds: Int64
    public let responseTimeoutSeconds: Int64

    public init(
        config: IndividuatorRootConfig, machineKey: any MachineKey, clientCertificatePEM: String,
        connectTimeoutSeconds: Int64 = 10, responseTimeoutSeconds: Int64 = 30
    ) {
        self.config = config
        self.machineKey = machineKey
        self.clientCertificatePEM = clientCertificatePEM
        self.connectTimeoutSeconds = connectTimeoutSeconds
        self.responseTimeoutSeconds = responseTimeoutSeconds
    }

    public func forward(address: String, requestBytes: [UInt8]) -> ForwardingUpstreamOutcome {
        let diagnostics = ForwardTransportDiagnostics()
        do {
            return try performForward(address: address, requestBytes: requestBytes, diagnostics: diagnostics)
        } catch let refusal as BosunServerRefusal {
            return .serverCertificateRefused(refusal.reason)
        } catch {
            return .transportError(detail: Self.transportErrorDetail(error: error, diagnostics: diagnostics))
        }
    }

    /// Round 5: a bare `"\(type(of: error)): \(error)"` is exactly what CI
    /// job 125695703568 showed was not enough to find a forward failure's
    /// real cause — nothing in it said how much of a response, if any, the
    /// client had actually seen, or named a TLS/channel error `errorCaught`
    /// reported that something else raced to claim the promise before.
    /// Appends both, so the next failure names its own cause.
    private static func transportErrorDetail(error: Error, diagnostics: ForwardTransportDiagnostics) -> String {
        var detail = "\(type(of: error)): \(error) (\(diagnostics.bytesReceived) bytes received"
        if let lastChannelError = diagnostics.lastChannelErrorDescription {
            detail += "; channel error: \(lastChannelError)"
        }
        detail += ")"
        return detail
    }

    private func performForward(
        address: String, requestBytes: [UInt8], diagnostics: ForwardTransportDiagnostics
    ) throws -> ForwardingUpstreamOutcome {
        let (host, port) = try BosunRegistrationClient.parseAddress(address)

        var capturedRefusal: BosunServerRefusal?
        let setup = try BosunMutualTLS.makeSetup(
            config: config, machineKey: machineKey, clientCertificatePEM: clientCertificatePEM,
            onServerCertificateRefusal: { capturedRefusal = $0 })

        let group = MultiThreadedEventLoopGroup(numberOfThreads: 1)
        defer { try? group.syncShutdownGracefully() }

        let responsePromise = group.next().makePromise(of: ByteBuffer.self)
        // Same leak-safety shape `BosunRegistrationClient.performRegistration`
        // uses — see that function's own doc comment for why this `defer`,
        // ordered after `group`'s own, exists at all.
        let promiseCompletion = PromiseCompletionClaim()
        defer {
            if promiseCompletion.claim() {
                responsePromise.fail(ForwardingAbandoned())
            }
        }
        var capturedTransportError: Error?

        let bootstrap = ClientBootstrap(group: group)
            .connectTimeout(.seconds(connectTimeoutSeconds))
            .channelInitializer { channel in
                do {
                    let sslHandler = try NIOSSLClientHandler(
                        context: setup.sslContext, serverHostname: nil, customVerificationCallback: setup.verify)
                    try channel.pipeline.syncOperations.addHandler(sslHandler)
                    try channel.pipeline.syncOperations.addHandler(
                        _ForwardingResponseCollector(
                            promise: responsePromise, completion: promiseCompletion, diagnostics: diagnostics,
                            onError: { capturedTransportError = $0 }))
                    return channel.eventLoop.makeSucceededVoidFuture()
                } catch {
                    return channel.eventLoop.makeFailedFuture(error)
                }
            }

        let channel: Channel
        do {
            channel = try bootstrap.connect(host: host, port: port).wait()
        } catch {
            throw BosunRegistrationClient.preferRealCause(
                capturedRefusal: capturedRefusal, capturedTransportError: capturedTransportError, over: error)
        }

        let timeout = ForwardingUpstreamTimeout(timeoutSeconds: responseTimeoutSeconds)
        let timeoutTask = channel.eventLoop.scheduleTask(in: .seconds(responseTimeoutSeconds)) {
            guard promiseCompletion.claim() else { return }
            capturedTransportError = capturedTransportError ?? timeout
            responsePromise.fail(timeout)
            channel.close(promise: nil)
        }

        var request = channel.allocator.buffer(capacity: requestBytes.count)
        request.writeBytes(requestBytes)
        do {
            try channel.writeAndFlush(request).wait()
        } catch {
            timeoutTask.cancel()
            throw BosunRegistrationClient.preferRealCause(
                capturedRefusal: capturedRefusal, capturedTransportError: capturedTransportError, over: error)
        }

        let responseBuffer: ByteBuffer
        do {
            responseBuffer = try responsePromise.futureResult.wait()
        } catch {
            timeoutTask.cancel()
            try? channel.close().wait()
            throw BosunRegistrationClient.preferRealCause(
                capturedRefusal: capturedRefusal, capturedTransportError: capturedTransportError, over: error)
        }
        timeoutTask.cancel()
        try? channel.close().wait()

        let bytes = Array(responseBuffer.readableBytesView)
        diagnostics.recordBytesReceived(bytes.count)
        guard let statusCode = Self.parseStatusCode(bytes) else {
            throw ForwardingUpstreamResponseUnparseable()
        }
        return .responded(statusCode: statusCode, responseBytes: bytes)
    }

    /// Round 6: this used to convert `bytes` to a `String` and search it
    /// for a `Character` equal to "\r" or "\n". "\r\n" is one extended
    /// grapheme cluster in Swift, so it is a `Character` equal to neither
    /// "\r" nor "\n" on its own, and `firstIndex(where:)` never matched a
    /// real CRLF line ending — every well-formed HTTP response failed to
    /// parse, including the test server's complete, well-formed
    /// `"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"` in round 3/4/5's
    /// CI failure, which round 5's byte count (40, the server's whole
    /// response, received in full) and empty server-error list already
    /// ruled out a truncated read or a close race for. Finds the line
    /// ending on the raw bytes instead, never decoding to a `String` until
    /// a line is already isolated.
    static func parseStatusCode(_ bytes: [UInt8]) -> Int? {
        guard let lineEnd = bytes.firstIndex(where: { $0 == 0x0D || $0 == 0x0A }) else { return nil }
        let statusLine = String(decoding: bytes[bytes.startIndex..<lineEnd], as: UTF8.self)
        // `omittingEmptySubsequences: false` (round 2 finding): with the
        // default `true`, a leading space before "HTTP/1.1" is itself
        // consumed as a separator and its resulting empty piece dropped,
        // so " HTTP/1.1 200 OK" split into the same three pieces as a
        // well-formed status line and parsed as 200 regardless. `false`
        // keeps that leading empty piece as `parts[0]`, which then fails
        // the `hasPrefix` check below like any other malformed line.
        let parts = statusLine.split(separator: " ", maxSplits: 2, omittingEmptySubsequences: false)
        guard parts.count >= 2, parts[0].hasPrefix("HTTP/1."), parts[0].count == "HTTP/1.x".count else { return nil }
        guard parts[1].count == 3, parts[1].allSatisfy({ $0.isASCII && $0.isNumber }) else { return nil }
        return Int(parts[1])
    }
}

struct ForwardingUpstreamTimeout: Error, CustomStringConvertible {
    let timeoutSeconds: Int64
    var description: String {
        "timed out after \(timeoutSeconds)s waiting for the TLS handshake, request, and response to complete"
    }
}

/// `responsePromise` succeeded with bytes `ForwardingUpstreamResponseReassembler`
/// itself already called a complete response, but `parseStatusCode` still
/// could not read a status line out of them — thrown, rather than returned
/// as a `.transportError` directly, so it funnels through `forward`'s own
/// `catch` and picks up `transportErrorDetail`'s bytes-received and
/// channel-error reporting the same as every other transport failure
/// (round 5).
struct ForwardingUpstreamResponseUnparseable: Error, CustomStringConvertible {
    var description: String { "could not parse a status line from the upstream response" }
}

/// Shared, thread-safe scratch space one `forward` call's `performForward`
/// writes into as bytes arrive and as `errorCaught` reports them, and
/// `forward`'s own `catch` reads back once `performForward` has already
/// thrown — carries the two facts round 5's CI failure showed were missing
/// from `.transportError`'s own detail: how many response bytes the client
/// actually saw, and the last TLS/channel error `errorCaught` reported, even
/// one that arrived after the promise it would have failed was already
/// claimed by something else.
private final class ForwardTransportDiagnostics {
    private let lock = NSLock()
    private var bytesReceivedCount = 0
    private var channelErrorDescriptions: [String] = []

    var bytesReceived: Int {
        lock.lock()
        defer { lock.unlock() }
        return bytesReceivedCount
    }

    var lastChannelErrorDescription: String? {
        lock.lock()
        defer { lock.unlock() }
        return channelErrorDescriptions.last
    }

    func recordBytesReceived(_ count: Int) {
        lock.lock()
        defer { lock.unlock() }
        bytesReceivedCount = count
    }

    func recordChannelError(_ error: Error) {
        lock.lock()
        defer { lock.unlock() }
        channelErrorDescriptions.append("\(type(of: error)): \(error)")
    }
}

/// What `performForward`'s own closing `defer` fails `responsePromise`
/// with on the one path neither `_ForwardingResponseCollector` nor the
/// timeout task ever completed it — see `BosunRegistrationAbandoned`'s own
/// doc comment in `BosunRegistrationClient.swift` for why this exists at
/// all; never read by any caller of `forward`.
private struct ForwardingAbandoned: Error, CustomStringConvertible {
    var description: String {
        "performForward exited without completing its response promise"
    }
}

/// Duplicated from `BosunRegistrationClient.swift`'s own copy, for the
/// same reason that file's is duplicated from `BosunAuthorizationClient
/// .swift`'s: each file's own `private` keeps the two from colliding.
private final class PromiseCompletionClaim {
    private let lock = NSLock()
    private var claimed = false

    func claim() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        if claimed { return false }
        claimed = true
        return true
    }
}

/// Feeds every byte received on the connection to a
/// `ForwardingUpstreamResponseReassembler`, and succeeds its promise once
/// that reassembler calls the response whole — by its own declared
/// `Content-Length` or chunked body, or (lacking either) by the Keel
/// destination closing the connection, never by the close alone regardless
/// of what framing the head declared (round 4: the previous shape, "whole
/// the moment the connection closes", read a response's completeness off
/// the wrong signal on principle, independent of the round 3/4 CI failure
/// — round 6 found that failure's real cause was `parseStatusCode`
/// comparing `Character`s to "\r"/"\n", which a CRLF line ending, one
/// `Character` under Swift's grapheme clustering, never matches).
private final class _ForwardingResponseCollector: ChannelInboundHandler {
    typealias InboundIn = ByteBuffer

    private var reassembler = ForwardingUpstreamResponseReassembler()
    private let promise: EventLoopPromise<ByteBuffer>
    private let completion: PromiseCompletionClaim
    private let diagnostics: ForwardTransportDiagnostics
    private let onError: (Error) -> Void

    init(
        promise: EventLoopPromise<ByteBuffer>, completion: PromiseCompletionClaim,
        diagnostics: ForwardTransportDiagnostics, onError: @escaping (Error) -> Void
    ) {
        self.promise = promise
        self.completion = completion
        self.diagnostics = diagnostics
        self.onError = onError
    }

    func channelRead(context: ChannelHandlerContext, data: NIOAny) {
        let incoming = unwrapInboundIn(data)
        let outcome = reassembler.feed(Array(incoming.readableBytesView))
        diagnostics.recordBytesReceived(reassembler.bytesReceived)
        switch outcome {
        case .needsMoreBytes:
            return
        case .complete(let responseBytes):
            complete(context: context, responseBytes: responseBytes)
        case .malformed(let detail):
            fail(error: ForwardingUpstreamResponseMalformed(detail: detail))
        }
    }

    func channelInactive(context: ChannelHandlerContext) {
        switch reassembler.connectionClosed() {
        case .complete(let responseBytes):
            complete(context: context, responseBytes: responseBytes)
        case .malformed(let detail):
            fail(error: ForwardingUpstreamResponseMalformed(detail: detail))
        }
    }

    // Recorded unconditionally — including after `completion` has already
    // been claimed by something else — so a TLS/channel error that arrives
    // after the promise is already settled is still visible in
    // `transportErrorDetail` rather than silently dropped by `fail`'s own
    // claim guard (round 5 finding: round 4's `onError` only ever saw
    // whichever error happened to win that race).
    func errorCaught(context: ChannelHandlerContext, error: Error) {
        diagnostics.recordChannelError(error)
        fail(error: error)
    }

    private func complete(context: ChannelHandlerContext, responseBytes: [UInt8]) {
        guard completion.claim() else { return }
        var buffer = context.channel.allocator.buffer(capacity: responseBytes.count)
        buffer.writeBytes(responseBytes)
        promise.succeed(buffer)
    }

    private func fail(error: Error) {
        guard completion.claim() else { return }
        onError(error)
        promise.fail(error)
    }
}

/// A response `ForwardingUpstreamResponseReassembler` could not call whole —
/// surfaced as a `.transportError` by `ForwardingUpstreamClient.forward`,
/// same as any other error on this path.
struct ForwardingUpstreamResponseMalformed: Error, CustomStringConvertible {
    let detail: String
    var description: String { detail }
}
