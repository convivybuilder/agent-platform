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
                    try channel.pipeline.syncOperations.addHandler(_ResponseCollector(promise: responsePromise))
                    return channel.eventLoop.makeSucceededVoidFuture()
                } catch {
                    return channel.eventLoop.makeFailedFuture(error)
                }
            }

        let channel: Channel
        do {
            channel = try bootstrap.connect(host: host, port: port).wait()
        } catch {
            throw Self.preferCapturedRefusal(capturedRefusal, over: error)
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
            throw Self.preferCapturedRefusal(capturedRefusal, over: error)
        }

        let timeoutTask = channel.eventLoop.scheduleTask(in: .seconds(responseTimeoutSeconds)) {
            responsePromise.fail(BosunRegistrationTimeout())
        }
        let responseBuffer: ByteBuffer
        do {
            responseBuffer = try responsePromise.futureResult.wait()
        } catch {
            try? channel.close().wait()
            throw Self.preferCapturedRefusal(capturedRefusal, over: error)
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
    var description: String { "timed out waiting for the Bosun's response" }
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
    private var finished = false

    init(promise: EventLoopPromise<ByteBuffer>) {
        self.promise = promise
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
        promise.fail(error)
    }
}
