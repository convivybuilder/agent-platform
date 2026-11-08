import Foundation
import IndividuatorCore
import NIOCore
import NIOPosix
import NIOSSL

/// One object this call named (a Read, an Edit, a document fetch — see
/// `bosun_authz.py`'s "two authorization shapes"), or the tool name a
/// boundary grant was issued for. Mirrors the Bosun's own
/// `_authorization_to_dict` exactly: `nil` is a valid, allowed decision
/// with no scoped authorization attached (never the shape a transport
/// failure takes — see `AuthorizationDecision.bosunUnreachable`).
public enum AuthorizationGrant: Equatable {
    case object(objectRef: String)
    case boundary(toolName: String)
}

/// The Bosun's answer to one `authorize` call, or this client's own
/// fail-closed stand-in for one it could not get. `reason` always carries
/// the Bosun's own text when it answered at all — refused before
/// `bosun_authz.decide()` ever ran (`session_unknown`,
/// `session_machine_mismatch`, `bad_certificate`, `malformed_request`,
/// `ceiling_unreadable`) or decided by it (`decide()`'s own `reason`) —
/// so a caller never loses why.
public struct AuthorizationDecision: Equatable {
    public let allow: Bool
    public let reason: String
    public let authorization: AuthorizationGrant?

    public init(allow: Bool, reason: String, authorization: AuthorizationGrant? = nil) {
        self.allow = allow
        self.reason = reason
        self.authorization = authorization
    }

    /// The fail-closed answer for every failure below the Bosun's own
    /// `decide()` — a connection refused, a TLS failure, a timeout, a reply
    /// this client cannot parse, or a non-2xx status the Bosun does not
    /// define as a decision. NEVER answered with `allow: true` on any such
    /// path (E12·2 slice S3): the strict availability rule is that an
    /// unreachable Bosun denies the call, with no setting that relaxes it.
    public static let bosunUnreachable = AuthorizationDecision(allow: false, reason: "bosun_unreachable")
}

/// The transport `authorize` relays against — real networking in
/// `BosunAuthorizationClient`, a scripted fake in its own tests.
public protocol AuthorizationTransport {
    func authorize(sessionToken: String, toolName: String, input: [String: Any]) -> AuthorizationDecision
}

/// The Individuator's per-call authorization relay (E12·2 slice S3;
/// E12·3's endpoint half; E12·8's per-call half): one mutual-TLS call to
/// the Bosun's `POST /authorize`, over the identical certificate setup
/// `BosunRegistrationClient` uses for `POST /sessions`
/// (`BosunMutualTLS.makeSetup`) — never a second TLS configuration.
///
/// THIS RELAYS THE BOSUN'S DECISION; IT DOES NOT ENFORCE ANYTHING ITSELF.
/// Per platform/decisions/0057, stage one is a policy and audit layer, not
/// containment or isolation, and this client adds no boundary of its own:
/// it asks the Bosun one question and reports back exactly what the Bosun
/// answered, or `.bosunUnreachable` when it could not ask at all. Exposing
/// this relay as a tool surface agents call is E12·7's concern, not this
/// one's (CQ·365: never describe this as enforcing per-agent tool policy —
/// see `bosun_registration.py`'s own `_PERMISSION_SET_NOT_WIRED`, which
/// means an allow this client reports today never reflects the Library's
/// per-agent tool policy, only the ceiling and revocation checks
/// `bosun_authz.decide()` already runs).
public struct BosunAuthorizationClient: AuthorizationTransport {
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

    /// Never throws: every failure below the Bosun's own decision —
    /// including one this type's own setup raises before a connection is
    /// even attempted, such as a malformed `bosunAddress` or an unreadable
    /// certificate file — is caught here and answered as
    /// `.bosunUnreachable`, so a caller can never mistake "this client
    /// failed to ask" for "the Bosun was asked and denied", nor, more
    /// importantly, ever read a thrown error as license to treat the call
    /// as allowed.
    public func authorize(sessionToken: String, toolName: String, input: [String: Any]) -> AuthorizationDecision {
        do {
            return try performAuthorization(sessionToken: sessionToken, toolName: toolName, input: input)
        } catch {
            return .bosunUnreachable
        }
    }

    private func performAuthorization(
        sessionToken: String, toolName: String, input: [String: Any]
    ) throws -> AuthorizationDecision {
        let (host, port) = try BosunRegistrationClient.parseAddress(config.bosunAddress)

        var capturedRefusal: BosunServerRefusal?
        let setup = try BosunMutualTLS.makeSetup(
            config: config, machineKey: machineKey, clientCertificatePEM: clientCertificatePEM,
            onServerCertificateRefusal: { capturedRefusal = $0 })

        let group = MultiThreadedEventLoopGroup(numberOfThreads: 1)
        defer { try? group.syncShutdownGracefully() }

        let responsePromise = group.next().makePromise(of: ByteBuffer.self)
        // Guarantees `responsePromise` is completed before `group`'s own
        // shutdown `defer` above runs: NIO's leak check raises a fatal
        // error ("leaking promise created at...") if an `EventLoopPromise`
        // is still unfulfilled when its event loop group shuts down, and
        // either throwing `.wait()` below (`connect`, `writeAndFlush`) can
        // leave this promise unfulfilled — a connect refused before any
        // handler ever sees the failure, or a write failure the pipeline
        // never reports to `_ResponseCollector`. `PromiseCompletionClaim`
        // is the single gate every path that can complete the promise
        // (`_ResponseCollector`, the timeout task below, and this `defer`)
        // goes through, so whichever gets there first wins and nothing
        // double-completes the promise — itself a separate fatal error.
        // Declared, and its `defer` ordered, after `group`'s own `defer`
        // above so Swift runs this one first (defers run in reverse
        // declaration order).
        let promiseCompletion = PromiseCompletionClaim()
        defer {
            if promiseCompletion.claim() {
                responsePromise.fail(BosunAuthorizationAbandoned())
            }
        }
        // Same non-blocking capture `BosunRegistrationClient` uses: set by
        // `_ResponseCollector.errorCaught` on the connecting event loop,
        // read from the calling thread only after one of the `.wait()`
        // calls below has already thrown.
        var capturedTransportError: Error?

        let bootstrap = ClientBootstrap(group: group)
            .connectTimeout(.seconds(connectTimeoutSeconds))
            .channelInitializer { channel in
                do {
                    let sslHandler = try NIOSSLClientHandler(
                        context: setup.sslContext, serverHostname: nil, customVerificationCallback: setup.verify)
                    try channel.pipeline.syncOperations.addHandler(sslHandler)
                    try channel.pipeline.syncOperations.addHandler(
                        _ResponseCollector(
                            promise: responsePromise, completion: promiseCompletion,
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

        // Bounds the handshake, the write below and the response together,
        // as one deadline from the moment the TCP connect succeeds — the
        // same shape `BosunRegistrationClient.responseTimeoutSeconds` uses
        // and for the same reason: a Bosun that accepts the connection and
        // then never completes the handshake or answers must still answer
        // deny within a bounded time, never hang the call indefinitely.
        let timeout = BosunAuthorizationTimeout(timeoutSeconds: responseTimeoutSeconds)
        let timeoutTask = channel.eventLoop.scheduleTask(in: .seconds(responseTimeoutSeconds)) {
            guard promiseCompletion.claim() else { return }
            capturedTransportError = capturedTransportError ?? timeout
            responsePromise.fail(timeout)
            channel.close(promise: nil)
        }

        let bodyData = try JSONSerialization.data(withJSONObject: [
            "session_token": sessionToken, "tool_name": toolName, "input": input,
        ])
        var request = channel.allocator.buffer(capacity: 256 + bodyData.count)
        request.writeString("POST /authorize HTTP/1.1\r\n")
        request.writeString("Host: \(host)\r\n")
        request.writeString("Content-Type: application/json\r\n")
        request.writeString("Content-Length: \(bodyData.count)\r\n")
        request.writeString("Connection: close\r\n\r\n")
        request.writeBytes(bodyData)
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

        return Self.parseHTTPResponse(responseBuffer)
    }

    /// Parses the Bosun's HTTP response into a decision, never throwing:
    /// anything this cannot read as either of the Bosun's two defined
    /// shapes — 200 with `{"allow", "reason"}` (`decide()`'s own answer,
    /// "always 200... even when the answer is deny" per
    /// `bosun_registration.py`), or any status with `{"refused": reason}`
    /// (a pre-`decide()` refusal, e.g. `session_unknown` after a
    /// `revoke-machine`) — answers `.bosunUnreachable` rather than guessing.
    static func parseHTTPResponse(_ buffer: ByteBuffer) -> AuthorizationDecision {
        let bytes = Array(buffer.readableBytesView)
        guard let text = String(bytes: bytes, encoding: .utf8) else {
            return .bosunUnreachable
        }
        guard let headerEnd = text.range(of: "\r\n\r\n") else {
            return .bosunUnreachable
        }
        let headLines = text[text.startIndex..<headerEnd.lowerBound].split(separator: "\r\n")
        guard let statusLine = headLines.first else {
            return .bosunUnreachable
        }
        let statusParts = statusLine.split(separator: " ", maxSplits: 2)
        guard statusParts.count >= 2, let status = Int(statusParts[1]) else {
            return .bosunUnreachable
        }
        let bodyText = String(text[headerEnd.upperBound...])
        guard let bodyData = bodyText.data(using: .utf8),
              let parsed = try? JSONSerialization.jsonObject(with: bodyData) as? [String: Any]
        else {
            return .bosunUnreachable
        }

        if status == 200, let allow = parsed["allow"] as? Bool, let reason = parsed["reason"] as? String {
            return AuthorizationDecision(allow: allow, reason: reason, authorization: Self.grant(from: parsed["authorization"]))
        }
        if let reason = parsed["refused"] as? String {
            return AuthorizationDecision(allow: false, reason: reason)
        }
        return .bosunUnreachable
    }

    private static func grant(from value: Any?) -> AuthorizationGrant? {
        guard let dict = value as? [String: Any], let kind = dict["kind"] as? String else { return nil }
        switch kind {
        case "object":
            guard let objectRef = dict["object_ref"] as? String else { return nil }
            return .object(objectRef: objectRef)
        case "boundary":
            guard let toolName = dict["tool_name"] as? String else { return nil }
            return .boundary(toolName: toolName)
        default:
            return nil
        }
    }
}

struct BosunAuthorizationTimeout: Error, CustomStringConvertible {
    let timeoutSeconds: Int64
    var description: String {
        "timed out after \(timeoutSeconds)s waiting for the TLS handshake, request, and response to complete"
    }
}

/// What `performAuthorization`'s own closing `defer` fails `responsePromise`
/// with, on the one path that reaches it without `_ResponseCollector` or the
/// timeout task ever having completed the promise themselves (a `connect` or
/// `writeAndFlush` failure neither reports). Never read by any caller of
/// `authorize`: by the time this fires, `performAuthorization` is already
/// throwing a DIFFERENT error, which is the one `authorize`'s `catch` maps to
/// `.bosunUnreachable`; this only keeps NIO's leak check from treating the
/// promise as abandoned.
private struct BosunAuthorizationAbandoned: Error, CustomStringConvertible {
    var description: String {
        "performAuthorization exited without completing its response promise"
    }
}

/// The single gate every path that can complete one `performAuthorization`
/// call's `responsePromise` goes through — `_ResponseCollector`, the timeout
/// task, and `performAuthorization`'s own closing `defer` — so whichever
/// claims it first is the one that actually calls `succeed`/`fail`, and NIO
/// never sees the same promise completed twice (itself a fatal error, same
/// family as the leaked-promise check this exists to satisfy). Duplicated
/// from `BosunRegistrationClient.swift`'s own copy rather than shared, for
/// the same reason its `_ResponseCollector` is duplicated: each file's own
/// `private` keeps the two from colliding.
private final class PromiseCompletionClaim {
    private let lock = NSLock()
    private var claimed = false

    /// Returns `true` the first time this is called, `false` every time
    /// after.
    func claim() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        if claimed { return false }
        claimed = true
        return true
    }
}

/// Collects every byte received on the connection into one buffer, and
/// succeeds its promise once the Bosun closes the connection —
/// `_RegistrationHandler.do_POST` always sets `close_connection = True`
/// (bosun_registration.py) for `/authorize` the same as `/sessions`, so a
/// closed connection is exactly "the whole response has arrived" for this
/// one-shot client too. Duplicated from `BosunRegistrationClient.swift`'s
/// own `_ResponseCollector` rather than shared: this plumbing is generic
/// byte-collection, not the TLS setup `BosunMutualTLS` exists to share, and
/// each file's own `private` keeps the two from colliding.
private final class _ResponseCollector: ChannelInboundHandler {
    typealias InboundIn = ByteBuffer

    private var buffer: ByteBuffer?
    private let promise: EventLoopPromise<ByteBuffer>
    private let completion: PromiseCompletionClaim
    private let onError: (Error) -> Void

    init(promise: EventLoopPromise<ByteBuffer>, completion: PromiseCompletionClaim, onError: @escaping (Error) -> Void) {
        self.promise = promise
        self.completion = completion
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
        guard completion.claim() else { return }
        promise.succeed(buffer ?? context.channel.allocator.buffer(capacity: 0))
    }

    func errorCaught(context: ChannelHandlerContext, error: Error) {
        guard completion.claim() else { return }
        onError(error)
        promise.fail(error)
    }
}
