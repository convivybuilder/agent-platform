import Foundation
import IndividuatorCore
import NIOCore
import NIOPosix
import NIOSSL

/// A registration attempt's outcome — mirrors `bosun_registration.py`'s own
/// `RegistrationOutcome`, plus the two failure shapes that are specific to
/// this side of the wire: the Individuator refusing the Bosun's OWN
/// certificate (`serverCertificateRefused`), and anything below the HTTP
/// layer (`transportError` — a connection refused, a timeout, a malformed
/// response).
public enum RegistrationOutcome: Equatable {
    case registered(sessionID: String, sessionToken: String)
    case refused(reason: String, detail: [String: String])
    case serverCertificateRefused(BosunServerRefusalReason)
    case transportError(detail: String)
}

/// The transport `RegistrationReattempt` retries against — real networking
/// in `BosunRegistrationClient`, a scripted fake in its own tests.
public protocol RegistrationTransport {
    func register(account: String, callerKind: String, ceiling: String, protocolVersion: String) -> RegistrationOutcome
}

public enum BosunRegistrationClientError: Error, CustomStringConvertible {
    case malformedAddress(String)

    public var description: String {
        switch self {
        case .malformedAddress(let address):
            return "bosun_address '\(address)' is not 'host:port'"
        }
    }
}

/// The Individuator's mutual-TLS registration client (platform/decisions/0092;
/// CQ·447 slice 3). One call is one TCP connection, one TLS 1.3 handshake,
/// one `POST /sessions`, and one response — there is no connection reuse,
/// matching the Bosun's own `_RegistrationHandler`, which closes after every
/// request.
///
/// The TLS handshake's own certificate verification is COMPLETELY replaced
/// (`certificateVerification = .noHostnameVerification` plus a custom
/// verification callback) by `BosunServerVerification.verify`, which chains
/// to the Library's root, requires serverAuth, and requires the Bosun host
/// name from `config` — 0092 Handoff: "Hostname verification is replaced by
/// this check, never simply turned off."
public struct BosunRegistrationClient: RegistrationTransport {
    public let config: IndividuatorRootConfig
    public let machineKey: any MachineKey
    public let clientCertificatePEM: String
    public let connectTimeoutSeconds: Int64
    /// Bounds the TLS handshake, the request write (which cannot complete
    /// until the handshake does, below), and the response together, as one
    /// deadline from the moment the TCP connect succeeds — not only the
    /// final wait for the Bosun's response, as a name read on its own might
    /// suggest (round 8 finding: `connectTimeoutSeconds` bounds only the
    /// initial TCP connect; a server that accepts that connection and then
    /// never completes or continues the TLS handshake left the client
    /// blocked in `channel.writeAndFlush(...).wait()` forever, with no
    /// deadline covering that step at all).
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

    public func register(account: String, callerKind: String, ceiling: String, protocolVersion: String) -> RegistrationOutcome {
        do {
            return try performRegistration(
                account: account, callerKind: callerKind, ceiling: ceiling, protocolVersion: protocolVersion)
        } catch let refusal as BosunServerRefusal {
            return .serverCertificateRefused(refusal.reason)
        } catch {
            // `"\(error)"` alone already recurses into an error's own
            // associated values (an `NIOSSLError.handshakeFailed`'s
            // `BoringSSLError.sslError` case carries a `NIOBoringSSLErrorStack`
            // whose `BoringSSLInternalError` elements ARE
            // `CustomStringConvertible`, so their text is not lost); the
            // type prefix below is for the cases that carry no payload at
            // all — `NIOSSLError.uncleanShutdown`, `ChannelError
            // .ioOnClosedChannel` — where the type name is the only extra
            // fact available, and is otherwise indistinguishable from any
            // other error whose case happens to share that name.
            return .transportError(detail: "\(type(of: error)): \(error)")
        }
    }

    static func parseAddress(_ address: String) throws -> (host: String, port: Int) {
        let parts = address.split(separator: ":", maxSplits: 1)
        guard parts.count == 2, let port = Int(parts[1]) else {
            throw BosunRegistrationClientError.malformedAddress(address)
        }
        return (String(parts[0]), port)
    }

    private func performRegistration(
        account: String, callerKind: String, ceiling: String, protocolVersion: String
    ) throws -> RegistrationOutcome {
        let (host, port) = try Self.parseAddress(config.bosunAddress)
        let rootPEM = try String(contentsOfFile: config.rootCAPath, encoding: .utf8)
        let rootCert = try NIOSSLCertificate(bytes: Array(try derFromPEM(rootPEM)), format: .der)

        let clientCertDER = try derFromPEM(clientCertificatePEM)
        let clientCert = try NIOSSLCertificate(bytes: Array(clientCertDER), format: .der)
        let privateKey = NIOSSLPrivateKey(customPrivateKey: MachineKeyNIOPrivateKey(key: machineKey))

        var tlsConfig = TLSConfiguration.makeClientConfiguration()
        tlsConfig.minimumTLSVersion = .tlsv13
        tlsConfig.maximumTLSVersion = .tlsv13
        tlsConfig.certificateChain = [.certificate(clientCert)]
        tlsConfig.privateKey = .privateKey(privateKey)
        // Hostname verification is replaced, never turned off outright
        // (0092 Handoff): `.noHostnameVerification` disables only the
        // SNI/CN-matching step BoringSSL would otherwise do; chain
        // validation to `trustRoots` below still runs, and the custom
        // verification callback below runs IN ADDITION, doing the
        // serverAuth/URI checks `BosunServerVerification` implements.
        tlsConfig.certificateVerification = .noHostnameVerification
        tlsConfig.trustRoots = .certificates([rootCert])

        let sslContext = try NIOSSLContext(configuration: tlsConfig)
        let bosunURIName = config.bosunURIName

        let group = MultiThreadedEventLoopGroup(numberOfThreads: 1)
        defer { try? group.syncShutdownGracefully() }

        let responsePromise = group.next().makePromise(of: ByteBuffer.self)
        var capturedRefusal: BosunServerRefusal?
        // Set by `_ResponseCollector.errorCaught` the moment NIO reports
        // ANY error on this channel — not only a server-certificate
        // refusal — before `responsePromise` itself is failed with it.
        // Reading this plain `var` from the calling thread after one of
        // the three `.wait()` calls below throws is the same non-blocking
        // idiom `capturedRefusal` already uses (round 4): both closures
        // run on `channel.eventLoop`, and by the time any of connect,
        // write or the response wait can observe `ChannelError
        // .ioOnClosedChannel`/`NIOSSLError.uncleanShutdown` on THIS
        // channel, the close that produced it was already preceded, on
        // that same event loop, by whichever `fireErrorCaught` call
        // reported the real cause (`NIOSSLHandler` fires the private-key
        // signing error, when there is one, before the generic handshake
        // failure — see `MachineKeyNIOPrivateKey.sign`'s own doc comment).
        var capturedTransportError: Error?

        let bootstrap = ClientBootstrap(group: group)
            .connectTimeout(.seconds(connectTimeoutSeconds))
            .channelInitializer { channel in
                let verify: NIOSSLCustomVerificationCallback = { certs, promise in
                    guard let leaf = certs.first else {
                        promise.succeed(.failed)
                        return
                    }
                    do {
                        try BosunServerVerification.verify(
                            leafCertificateDER: Data(leaf.toDERBytes()), rootPEM: rootPEM,
                            expectedBosunURIName: bosunURIName)
                        promise.succeed(.certificateVerified)
                    } catch let refusal as BosunServerRefusal {
                        capturedRefusal = refusal
                        promise.succeed(.failed)
                    } catch {
                        promise.succeed(.failed)
                    }
                }
                do {
                    let sslHandler = try NIOSSLClientHandler(
                        context: sslContext, serverHostname: nil, customVerificationCallback: verify)
                    try channel.pipeline.syncOperations.addHandler(sslHandler)
                    try channel.pipeline.syncOperations.addHandler(
                        _ResponseCollector(promise: responsePromise, onError: { capturedTransportError = $0 }))
                    return channel.eventLoop.makeSucceededVoidFuture()
                } catch {
                    return channel.eventLoop.makeFailedFuture(error)
                }
            }

        let channel: Channel
        do {
            channel = try bootstrap.connect(host: host, port: port).wait()
        } catch {
            throw Self.preferRealCause(
                capturedRefusal: capturedRefusal, capturedTransportError: capturedTransportError, over: error)
        }

        // Covers the handshake, the write below (which cannot complete
        // until the handshake does) and the response in one deadline,
        // scheduled now rather than after the write — see
        // `responseTimeoutSeconds`'s own doc comment (round 8). Firing
        // closes the channel, which is what turns the write's or the
        // response wait's own `.wait()` — whichever is in flight at the
        // time — into a failure at all; without this, a peer that accepts
        // the TCP connection and goes silent leaves either one blocked
        // forever. `capturedTransportError` is set here, on this same
        // event loop, exactly like `_ResponseCollector.errorCaught` already
        // sets it, so whichever `.wait()` throws next prefers this named
        // timeout over the generic `ChannelError.ioOnClosedChannel` the
        // forced close itself produces (`preferRealCause`, below).
        let timeout = BosunRegistrationTimeout(timeoutSeconds: responseTimeoutSeconds)
        let timeoutTask = channel.eventLoop.scheduleTask(in: .seconds(responseTimeoutSeconds)) {
            capturedTransportError = capturedTransportError ?? timeout
            responsePromise.fail(timeout)
            channel.close(promise: nil)
        }

        let bodyData = try JSONSerialization.data(withJSONObject: [
            "account": account, "caller_kind": callerKind, "ceiling": ceiling, "protocol_version": protocolVersion,
        ])
        var request = channel.allocator.buffer(capacity: 256 + bodyData.count)
        request.writeString("POST /sessions HTTP/1.1\r\n")
        request.writeString("Host: \(host)\r\n")
        request.writeString("Content-Type: application/json\r\n")
        request.writeString("Content-Length: \(bodyData.count)\r\n")
        request.writeString("Connection: close\r\n\r\n")
        request.writeBytes(bodyData)
        do {
            try channel.writeAndFlush(request).wait()
        } catch {
            timeoutTask.cancel()
            throw Self.preferRealCause(
                capturedRefusal: capturedRefusal, capturedTransportError: capturedTransportError, over: error)
        }

        let responseBuffer: ByteBuffer
        do {
            responseBuffer = try responsePromise.futureResult.wait()
        } catch {
            timeoutTask.cancel()
            try? channel.close().wait()
            throw Self.preferRealCause(
                capturedRefusal: capturedRefusal, capturedTransportError: capturedTransportError, over: error)
        }
        timeoutTask.cancel()
        try? channel.close().wait()

        return try Self.parseHTTPResponse(responseBuffer)
    }

    /// Once the TLS verification callback has already refused the Bosun's
    /// server certificate, that refusal — not whatever NIO raises next
    /// closing the connection out from under the still-failing handshake
    /// (`ChannelError.ioOnClosedChannel`, `NIOSSLError.uncleanShutdown`) —
    /// is the registration's real outcome (round 4 finding: the connect,
    /// write and response-wait steps below can each be where that later,
    /// uninformative error actually surfaces, depending on exactly when
    /// NIO tears the channel down relative to which call is in flight, so
    /// every one of them must prefer the refusal the same way).
    static func preferCapturedRefusal(_ capturedRefusal: BosunServerRefusal?, over error: Error) -> Error {
        capturedRefusal ?? error
    }

    /// Round 7: once `_ResponseCollector.errorCaught` has already recorded
    /// the real cause behind this connection's failure — our own machine
    /// key's signing failure, surfacing via `MachineKeyNIOPrivateKey.sign`
    /// during the handshake, is exactly such a cause — prefer that
    /// recorded cause over whatever NIO raises next tearing down the
    /// still-failing channel (`ChannelError.ioOnClosedChannel`,
    /// `NIOSSLError.uncleanShutdown`), at every one of connect, write and
    /// response-wait. Same precedence shape `preferCapturedRefusal` already
    /// uses: a captured server-certificate refusal still wins over
    /// everything, including this.
    static func preferRealCause(
        capturedRefusal: BosunServerRefusal?, capturedTransportError: Error?, over error: Error
    ) -> Error {
        if let capturedRefusal {
            return capturedRefusal
        }
        if let capturedTransportError, Self.isUninformativeTeardown(error) {
            return capturedTransportError
        }
        return error
    }

    /// The two error shapes that name nothing beyond "the channel was
    /// already closing/closed" — the peer having already gone away without
    /// a TLS alert, as far as the Swift client can see — never anything
    /// more specific about WHY, which is exactly what a captured cause
    /// above should take the place of when one was recorded.
    private static func isUninformativeTeardown(_ error: Error) -> Bool {
        if let channelError = error as? ChannelError, channelError == .ioOnClosedChannel {
            return true
        }
        if let sslError = error as? NIOSSLError, sslError == .uncleanShutdown {
            return true
        }
        return false
    }

    static func parseHTTPResponse(_ buffer: ByteBuffer) throws -> RegistrationOutcome {
        let bytes = Array(buffer.readableBytesView)
        guard let text = String(bytes: bytes, encoding: .utf8) else {
            return .transportError(detail: "response was not valid UTF-8")
        }
        guard let headerEnd = text.range(of: "\r\n\r\n") else {
            return .transportError(detail: "no header/body boundary in response")
        }
        let headLines = text[text.startIndex..<headerEnd.lowerBound].split(separator: "\r\n")
        guard let statusLine = headLines.first else {
            return .transportError(detail: "empty status line")
        }
        let statusParts = statusLine.split(separator: " ", maxSplits: 2)
        guard statusParts.count >= 2, let status = Int(statusParts[1]) else {
            return .transportError(detail: "malformed status line: \(statusLine)")
        }
        let bodyText = String(text[headerEnd.upperBound...])
        guard let bodyData = bodyText.data(using: .utf8),
              let parsed = try? JSONSerialization.jsonObject(with: bodyData) as? [String: Any]
        else {
            return .transportError(detail: "response body is not a JSON object: \(bodyText)")
        }

        if status == 200, let sessionID = parsed["session_id"] as? String,
           let sessionToken = parsed["session_token"] as? String {
            return .registered(sessionID: sessionID, sessionToken: sessionToken)
        }
        guard let reason = parsed["refused"] as? String else {
            return .transportError(detail: "response neither registered nor named a refusal: \(bodyText)")
        }
        var detail: [String: String] = [:]
        for (key, value) in parsed where key != "refused" {
            detail[key] = "\(value)"
        }
        return .refused(reason: reason, detail: detail)
    }
}

struct BosunRegistrationTimeout: Error, CustomStringConvertible {
    let timeoutSeconds: Int64
    var description: String {
        "timed out after \(timeoutSeconds)s waiting for the TLS handshake, request, and response to complete"
    }
}

/// Collects every byte received on the connection into one buffer, and
/// succeeds its promise once the Bosun closes the connection —
/// `_RegistrationHandler.do_POST` always sets `close_connection = True`
/// (bosun_registration.py), so a closed connection is exactly "the whole
/// response has arrived" for this one-shot client.
private final class _ResponseCollector: ChannelInboundHandler {
    typealias InboundIn = ByteBuffer

    private var buffer: ByteBuffer?
    private let promise: EventLoopPromise<ByteBuffer>
    private let onError: (Error) -> Void
    private var finished = false

    init(promise: EventLoopPromise<ByteBuffer>, onError: @escaping (Error) -> Void) {
        self.promise = promise
        self.onError = onError
    }

    func channelRead(context: ChannelHandlerContext, data: NIOAny) {
        var incoming = unwrapInboundIn(data)
        if buffer == nil {
            buffer = context.channel.allocator.buffer(capacity: incoming.readableBytes)
        }
        buffer!.writeBuffer(&incoming)
    }

    func channelInactive(context: ChannelHandlerContext) {
        guard !finished else { return }
        finished = true
        promise.succeed(buffer ?? context.channel.allocator.buffer(capacity: 0))
    }

    func errorCaught(context: ChannelHandlerContext, error: Error) {
        guard !finished else { return }
        finished = true
        onError(error)
        promise.fail(error)
    }
}
