import Foundation
import IndividuatorNetwork
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif

/// Handles exactly one already-accepted connection to the forwarding
/// socket (0092; build plan slice S5b). In order:
///
/// 1. `PeerUIDGate` against the admitted-accounts set — refused before a
///    single request byte is read.
/// 2. `ForwardingHTTPRequestReader` reads and frames the request.
/// 3. `ForwardingRequestMatcher` matches it against the fixed allowlist.
/// 4. The matched route's destination is resolved to an address from
///    configuration (never the request) and dialled through
///    `ForwardingUpstreamTransport`, with `ForwardingHeaderStripping`
///    applied first.
///
/// Every refusal answers locally and opens no upstream connection at all —
/// `upstream.forward` is called on exactly one path, the success case of
/// step 3, and nowhere else in this type.
public struct ForwardingConnectionHandler {
    public let admittedUIDs: Set<uid_t>
    public let allowlist: [ForwardingRouteEntry]
    public let libraryAddress: String?
    public let bosunAddress: String
    public let upstream: ForwardingUpstreamTransport
    public let logSink: (ForwardingLogEntry) -> Void
    /// The whole request (head and body) must arrive within this many
    /// seconds of `handle` starting to read it, or this connection is
    /// refused and closed (round 1 finding: the admission check alone does
    /// not stop one admitted account from stalling the daemon for every
    /// other one by connecting and sending nothing, or sending slowly).
    public let requestReadTimeoutSeconds: TimeInterval

    /// Resolves the connecting peer's admitted uid, the same injectable
    /// idiom `RegisterRequestHandler` uses for the identical reason: a
    /// test exercises more than one admitted uid without a real privilege
    /// change.
    private let peerUIDCheck: (Int32, Set<uid_t>) throws -> uid_t
    private let now: () -> Date

    public init(
        admittedUIDs: Set<uid_t>, allowlist: [ForwardingRouteEntry], libraryAddress: String?, bosunAddress: String,
        upstream: ForwardingUpstreamTransport,
        logSink: @escaping (ForwardingLogEntry) -> Void = { print(ForwardingLog.render($0)) },
        requestReadTimeoutSeconds: TimeInterval = ForwardingHTTPRequestReader.defaultTimeoutSeconds,
        peerUIDCheck: @escaping (Int32, Set<uid_t>) throws -> uid_t = PeerUIDGate.check,
        now: @escaping () -> Date = Date.init
    ) {
        self.admittedUIDs = admittedUIDs
        self.allowlist = allowlist
        self.libraryAddress = libraryAddress
        self.bosunAddress = bosunAddress
        self.upstream = upstream
        self.logSink = logSink
        self.requestReadTimeoutSeconds = requestReadTimeoutSeconds
        self.peerUIDCheck = peerUIDCheck
        self.now = now
    }

    public func handle(fileDescriptor: Int32) {
        defer { close(fileDescriptor) }
        SocketIO.setNoSIGPIPE(fileDescriptor: fileDescriptor)

        let peerUID: uid_t
        do {
            peerUID = try peerUIDCheck(fileDescriptor, admittedUIDs)
        } catch {
            refuseLocally(
                fileDescriptor: fileDescriptor, statusCode: 403, reasonPhrase: "Forbidden",
                detail: "the connecting account is not admitted", peerUID: nil, method: "-",
                logReason: "peer_uid_not_admitted")
            return
        }

        switch ForwardingHTTPRequestReader.read(
            fileDescriptor: fileDescriptor, timeoutSeconds: requestReadTimeoutSeconds, now: now
        ) {
        case .malformed(let method, let detail):
            refuseLocally(
                fileDescriptor: fileDescriptor, statusCode: 400, reasonPhrase: "Bad Request", detail: detail,
                peerUID: peerUID, method: method ?? "-", logReason: "malformed_request")
        case .framingRefused(let method, let reason):
            refuseLocally(
                fileDescriptor: fileDescriptor, statusCode: 400, reasonPhrase: "Bad Request",
                detail: reason.description, peerUID: peerUID, method: method, logReason: reason.description)
        case .timedOut(let detail):
            refuseLocally(
                fileDescriptor: fileDescriptor, statusCode: 408, reasonPhrase: "Request Timeout", detail: detail,
                peerUID: peerUID, method: "-", logReason: "request_read_timed_out")
        case .success(let method, let target, let head, let body):
            handleParsedRequest(
                fileDescriptor: fileDescriptor, peerUID: peerUID, method: method, target: target, head: head,
                body: body)
        }
    }

    private func handleParsedRequest(
        fileDescriptor: Int32, peerUID: uid_t, method: String, target: [UInt8], head: ForwardingRequestHead,
        body: [UInt8]
    ) {
        let routes = allowlist.map(\.route)
        switch ForwardingRequestMatcher.match(method: method, target: target, allowlist: routes) {
        case .failure(let reason):
            refuseLocally(
                fileDescriptor: fileDescriptor, statusCode: 404, reasonPhrase: "Not Found",
                detail: reason.description, peerUID: peerUID, method: method, logReason: reason.description)
        case .success(let match):
            // Every route in `allowlist` is unique (the allowlist is
            // trusted, in-code configuration, never request data), so the
            // first entry whose route matches IS the one the matcher just
            // chose.
            guard let entry = allowlist.first(where: { $0.route == match.route }) else {
                refuseLocally(
                    fileDescriptor: fileDescriptor, statusCode: 500, reasonPhrase: "Internal Server Error",
                    detail: "the matched route has no allowlist entry", peerUID: peerUID, method: method,
                    logReason: "internal_error")
                return
            }
            forward(
                fileDescriptor: fileDescriptor, peerUID: peerUID, method: method, match: match, head: head,
                body: body, destination: entry.destination)
        }
    }

    private func forward(
        fileDescriptor: Int32, peerUID: uid_t, method: String, match: ForwardingMatch, head: ForwardingRequestHead,
        body: [UInt8], destination: ForwardingDestination
    ) {
        let address: String
        switch destination {
        case .library:
            guard let libraryAddress else {
                refuseLocally(
                    fileDescriptor: fileDescriptor, statusCode: 502, reasonPhrase: "Bad Gateway",
                    detail: "no library destination is configured", peerUID: peerUID, method: method,
                    destination: destination, logReason: nil)
                return
            }
            address = libraryAddress
        case .bosun:
            address = bosunAddress
        }

        let strippedHeaders = ForwardingHeaderStripping.strip(head.headerFields)
        let reframedHeaders = Self.reframeHeaders(strippedHeaders, bodyCount: body.count)
        let requestBytes = Self.buildRequestBytes(
            method: method, target: match.target, headers: reframedHeaders, body: body)

        switch upstream.forward(address: address, requestBytes: requestBytes) {
        case .responded(let statusCode, let responseBytes):
            ForwardingLocalResponse.writeUpstreamResponse(fileDescriptor: fileDescriptor, rawResponseBytes: responseBytes)
            logSink(ForwardingLogEntry(
                time: now(), peerUID: peerUID, method: method,
                result: .forwarded(destination: destination, upstreamStatus: statusCode)))
        case .serverCertificateRefused, .transportError:
            ForwardingLocalResponse.writeRefusal(
                fileDescriptor: fileDescriptor, statusCode: 502, reasonPhrase: "Bad Gateway",
                detail: "the Keel destination could not be reached")
            logSink(ForwardingLogEntry(
                time: now(), peerUID: peerUID, method: method, result: .upstreamUnreachable(destination: destination)))
        }
    }

    private func refuseLocally(
        fileDescriptor: Int32, statusCode: Int, reasonPhrase: String, detail: String, peerUID: uid_t?,
        method: String, destination: ForwardingDestination? = nil, logReason: String?
    ) {
        ForwardingLocalResponse.writeRefusal(
            fileDescriptor: fileDescriptor, statusCode: statusCode, reasonPhrase: reasonPhrase, detail: detail)
        let result: ForwardingLogEntry.Result
        if let destination {
            result = .upstreamUnreachable(destination: destination)
        } else {
            result = .refused(reason: logReason ?? detail)
        }
        logSink(ForwardingLogEntry(time: now(), peerUID: peerUID ?? 0, method: method, result: result))
    }

    /// Replaces whatever framing the client's own headers named with
    /// framing that matches `body`, the bytes this struct is actually
    /// about to write (round 4 security finding): `readChunkedBody` has
    /// already de-chunked the body by the time it reaches here, so passing
    /// the client's `Transfer-Encoding: chunked` through unchanged would
    /// have the Keel parse `body`'s raw bytes as chunk framing again —
    /// letting an admitted local client plant its own chunk boundaries (and
    /// a second, smuggled request) inside what the Keel reads as the body
    /// of this one. Removing `Transfer-Encoding`, `Trailer`, and the
    /// client's own `Content-Length` and replacing them with a single
    /// `Content-Length` computed from `body.count` makes this true for a
    /// Content-Length request too: the forwarded length is always the one
    /// this reader verified by actually reading that many bytes, never the
    /// client's header value passed through unexamined.
    private static let framingHeaderNamesToRemove: Set<String> = ["transfer-encoding", "trailer", "content-length"]

    private static func reframeHeaders(_ headers: [ForwardingHeaderField], bodyCount: Int) -> [ForwardingHeaderField] {
        var result = headers.filter { !Self.framingHeaderNamesToRemove.contains($0.name.lowercased()) }
        result.append(ForwardingHeaderField(name: "Content-Length", value: String(bodyCount)))
        return result
    }

    /// Builds the exact bytes sent to the Keel destination: `match.target`
    /// — the request target forwarded byte for byte, exactly as it
    /// matched (0092) — never rebuilt from a parsed path, plus the
    /// stripped header set, plus the body unchanged.
    private static func buildRequestBytes(
        method: String, target: [UInt8], headers: [ForwardingHeaderField], body: [UInt8]
    ) -> [UInt8] {
        var bytes = Array("\(method) ".utf8)
        bytes.append(contentsOf: target)
        bytes.append(contentsOf: Array(" HTTP/1.1\r\n".utf8))
        for field in headers {
            bytes.append(contentsOf: Array("\(field.name): \(field.value)\r\n".utf8))
        }
        bytes.append(contentsOf: Array("\r\n".utf8))
        bytes.append(contentsOf: body)
        return bytes
    }
}
