import Foundation
import Testing
@testable import IndividuatorDaemonCore
@testable import IndividuatorNetwork
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif

private func makeSocketPair() -> (Int32, Int32) {
    var fds: [Int32] = [0, 0]
    let result = fds.withUnsafeMutableBufferPointer { socketpair(AF_UNIX, SOCK_STREAM, 0, $0.baseAddress) }
    precondition(result == 0, "socketpair() failed")
    SocketIO.setNoSIGPIPE(fileDescriptor: fds[0])
    SocketIO.setNoSIGPIPE(fileDescriptor: fds[1])
    return (fds[0], fds[1])
}

/// Records every call this test's forwarding-socket handler made to the
/// upstream transport — the "test Keel server that counts connections"
/// the build plan's Handoff asks for, as a scripted double rather than a
/// real TLS listener: a zero-call count proves "opened no upstream
/// connection" exactly as counting real TCP connections would, without
/// this file needing a real network bind (`BosunRegistrationClientIntegrationTests
/// .swift`'s own real-TLS harness covers `ForwardingUpstreamClient`'s
/// mutual-TLS plumbing separately — see `ForwardingUpstreamClientIntegrationTests.swift`).
private final class RecordingUpstreamTransport: ForwardingUpstreamTransport {
    private(set) var calls: [(address: String, requestBytes: [UInt8])] = []
    let outcome: ForwardingUpstreamOutcome
    init(outcome: ForwardingUpstreamOutcome = .responded(statusCode: 200, responseBytes: Array("HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n".utf8))) {
        self.outcome = outcome
    }
    func forward(address: String, requestBytes: [UInt8]) -> ForwardingUpstreamOutcome {
        calls.append((address, requestBytes))
        return outcome
    }
}

private final class RecordingLogSink {
    private(set) var entries: [ForwardingLogEntry] = []
    func capture(_ entry: ForwardingLogEntry) { entries.append(entry) }
}

extension PoolBlockingIntegrationSuites {

/// `ForwardingConnectionHandler` (build plan slice S5b; 0092 Handoff
/// "Forwarding endpoint"). The allowlist below is this slice's own
/// synthetic test route, never the real route list: slice S5c populates
/// the production `ForwardingAllowlist.entries` from the Library-calling
/// clients' own call sites, and this suite's own synthetic widget route
/// keeps exercising the matcher and the handler in isolation from that
/// list's size and contents.
///
/// `.serialized` + a time limit, the same pair the real-socket suites in
/// this target carry: every test here calls `handler.handle` against a
/// real `socketpair(2)` fd, several directly on the test body rather than
/// off a spawned thread, so under Swift Testing's default full parallelism
/// this suite's ~20 tests could hold that many cooperative-pool threads in
/// a blocking read at once; `.serialized` bounds that to one. Round 2
/// believed that was the whole CI hang fix; round 3 found it was not —
/// `.serialized` only bounds blocking WITHIN this suite, and this target
/// had enough OTHER suites making the same kind of blocking call that the
/// pool was still exhausted across suites (see
/// `PoolBlockingIntegrationSuites`'s own doc comment, which nesting this
/// suite there now fixes).
@Suite(.serialized, .timeLimit(.minutes(1))) struct ForwardingConnectionHandlerTests {
    private static let widgetRoute = ForwardingRoute(method: "GET", path: "/v1/widgets/{id}")
    private static let allowlist = [ForwardingRouteEntry(route: widgetRoute, destination: .bosun)]

    private static func makeHandler(
        admittedUIDs: Set<uid_t> = [getuid()], allowlist: [ForwardingRouteEntry] = allowlist,
        libraryAddress: String? = nil, bosunAddress: String = "127.0.0.1:9", transport: RecordingUpstreamTransport,
        log: RecordingLogSink, requestReadTimeoutSeconds: TimeInterval = ForwardingHTTPRequestReader.defaultTimeoutSeconds
    ) -> ForwardingConnectionHandler {
        ForwardingConnectionHandler(
            admittedUIDs: admittedUIDs, allowlist: allowlist, libraryAddress: libraryAddress,
            bosunAddress: bosunAddress, upstream: transport, logSink: log.capture,
            requestReadTimeoutSeconds: requestReadTimeoutSeconds)
    }

    private func sendRaw(_ fd: Int32, _ text: String) {
        sendRawBytes(fd, Array(text.utf8))
    }

    private func sendRawBytes(_ fd: Int32, _ bytes: [UInt8]) {
        bytes.withUnsafeBytes { ptr in _ = write(fd, ptr.baseAddress, ptr.count) }
    }

    /// Reads until the peer closes — `ForwardingConnectionHandler.handle`
    /// always closes its end when it returns, so this never hangs on a
    /// well-behaved handler.
    private func readAllUntilClose(_ fd: Int32) -> [UInt8] {
        var result: [UInt8] = []
        var buffer = [UInt8](repeating: 0, count: 4096)
        while true {
            let n = buffer.withUnsafeMutableBytes { ptr in read(fd, ptr.baseAddress, 4096) }
            if n <= 0 { break }
            result.append(contentsOf: buffer[0..<n])
        }
        return result
    }

    // MARK: - admission

    @Test func refusesAnUnlistedUIDBeforeAnyByteIsRead() {
        let (serverFD, clientFD) = makeSocketPair()
        defer { close(clientFD) }
        let transport = RecordingUpstreamTransport()
        let log = RecordingLogSink()
        let handler = Self.makeHandler(admittedUIDs: [getuid() + 1], transport: transport, log: log)

        // This client never writes anything at all — a handler that read
        // before checking the peer's uid would hang here forever.
        handler.handle(fileDescriptor: serverFD)

        let response = String(decoding: readAllUntilClose(clientFD), as: UTF8.self)
        #expect(response.hasPrefix("HTTP/1.1 403"))
        #expect(transport.calls.isEmpty)
        #expect(log.entries.count == 1)
        #expect(log.entries[0].result == .refused(reason: "peer_uid_not_admitted"))
    }

    /// Round 1 finding: without an overall read deadline, an admitted
    /// client that sends nothing at all stalls this connection (and, before
    /// `ForwardingSocketServer`'s own round 1 fix, every other admitted
    /// account's forwarding behind it) forever. A short
    /// `requestReadTimeoutSeconds` here is what keeps this test itself
    /// bounded; the production default is `ForwardingHTTPRequestReader
    /// .defaultTimeoutSeconds`.
    @Test func aClientThatSendsNothingIsCutOffAtTheReadDeadlineRatherThanHangingForever() {
        let (serverFD, clientFD) = makeSocketPair()
        defer { close(clientFD) }
        let transport = RecordingUpstreamTransport()
        let log = RecordingLogSink()
        let handler = Self.makeHandler(transport: transport, log: log, requestReadTimeoutSeconds: 0.2)

        handler.handle(fileDescriptor: serverFD)

        let response = String(decoding: readAllUntilClose(clientFD), as: UTF8.self)
        #expect(response.hasPrefix("HTTP/1.1 408"))
        #expect(transport.calls.isEmpty)
        #expect(log.entries.count == 1)
        #expect(log.entries[0].result == .refused(reason: "request_read_timed_out"))
    }

    /// The same deadline, but against a client that sends bytes too slowly
    /// (one byte every 50ms) rather than none at all — the deadline is
    /// counted from when `handle` started reading, not reset by each byte
    /// that arrives, so a trickle slower than the deadline still gets cut
    /// off rather than being allowed to extend it indefinitely.
    @Test func aClientThatTricklesBytesSlowerThanTheDeadlineIsStillCutOff() {
        let (serverFD, clientFD) = makeSocketPair()
        let transport = RecordingUpstreamTransport()
        let log = RecordingLogSink()
        let handler = Self.makeHandler(transport: transport, log: log, requestReadTimeoutSeconds: 0.2)

        let thread = Thread {
            for byte in Array("GET /v1/widgets/abc HTTP/1.1\r\n".utf8) {
                var byteValue = byte
                guard write(clientFD, &byteValue, 1) > 0 else { break }  // the peer closed; stop trickling
                Thread.sleep(forTimeInterval: 0.05)
            }
        }
        thread.start()
        handler.handle(fileDescriptor: serverFD)
        close(clientFD)

        #expect(transport.calls.isEmpty)
        #expect(log.entries.first?.result == .refused(reason: "request_read_timed_out"))
    }

    // MARK: - matcher refusals open no upstream connection

    @Test(arguments: [
        ("CONNECT", "/v1/widgets/abc"),
        ("GET", "http://evil.example/v1/widgets/abc"),
        ("GET", "/v1/widgets/abc#frag"),
        ("GET", "/v1/widgets/."),
        ("GET", "/v1/widgets/.."),
        ("GET", "/v1/widgets//abc"),
        ("GET", "/v1/widgets/a\\bc"),
        ("GET", "/v1/widgets/%2fabc"),
        ("GET", "/v1/widgets/%2Fabc"),
        ("GET", "/v1/widgets/%2eabc"),
        ("GET", "/v1/widgets/%5cabc"),
        ("GET", "/no/such/route"),
        ("POST", "/v1/widgets/abc"),
    ])
    func refusesAndOpensNoUpstreamConnection(method: String, target: String) {
        let (serverFD, clientFD) = makeSocketPair()
        let transport = RecordingUpstreamTransport()
        let log = RecordingLogSink()
        let handler = Self.makeHandler(transport: transport, log: log)

        let thread = Thread { handler.handle(fileDescriptor: serverFD) }
        thread.start()
        sendRaw(clientFD, "\(method) \(target) HTTP/1.1\r\nHost: test\r\n\r\n")
        let response = String(decoding: readAllUntilClose(clientFD), as: UTF8.self)
        close(clientFD)

        #expect(transport.calls.isEmpty, "method=\(method) target=\(target)")
        #expect(!response.isEmpty, "method=\(method) target=\(target)")
        #expect(!response.hasPrefix("HTTP/1.1 200"), "method=\(method) target=\(target)")
    }

    // MARK: - each framing refusal opens no upstream connection

    @Test func refusesTransferEncodingAndContentLengthTogether() {
        let (serverFD, clientFD) = makeSocketPair()
        let transport = RecordingUpstreamTransport()
        let log = RecordingLogSink()
        let handler = Self.makeHandler(transport: transport, log: log)

        let thread = Thread { handler.handle(fileDescriptor: serverFD) }
        thread.start()
        sendRaw(
            clientFD,
            "GET /v1/widgets/abc HTTP/1.1\r\nHost: test\r\nContent-Length: 0\r\nTransfer-Encoding: chunked\r\n\r\n")
        _ = readAllUntilClose(clientFD)
        close(clientFD)

        #expect(transport.calls.isEmpty)
        #expect(log.entries.first?.result == .refused(reason: "the request carries both Transfer-Encoding and Content-Length"))
    }

    @Test func refusesMultipleContentLengthHeaders() {
        let (serverFD, clientFD) = makeSocketPair()
        let transport = RecordingUpstreamTransport()
        let log = RecordingLogSink()
        let handler = Self.makeHandler(transport: transport, log: log)

        let thread = Thread { handler.handle(fileDescriptor: serverFD) }
        thread.start()
        sendRaw(clientFD, "GET /v1/widgets/abc HTTP/1.1\r\nHost: test\r\nContent-Length: 0\r\nContent-Length: 1\r\n\r\n")
        _ = readAllUntilClose(clientFD)
        close(clientFD)

        #expect(transport.calls.isEmpty)
        #expect(log.entries.first?.result == .refused(reason: "the request carries more than one Content-Length header"))
    }

    @Test func refusesAnInvalidContentLengthValue() {
        let (serverFD, clientFD) = makeSocketPair()
        let transport = RecordingUpstreamTransport()
        let log = RecordingLogSink()
        let handler = Self.makeHandler(transport: transport, log: log)

        let thread = Thread { handler.handle(fileDescriptor: serverFD) }
        thread.start()
        sendRaw(clientFD, "GET /v1/widgets/abc HTTP/1.1\r\nHost: test\r\nContent-Length: -1\r\n\r\n")
        _ = readAllUntilClose(clientFD)
        close(clientFD)

        #expect(transport.calls.isEmpty)
        #expect(log.entries.first?.result == .refused(reason: "the request's Content-Length is not a single exact non-negative byte count"))
    }

    @Test func refusesATransferEncodingThatIsNotSingleChunked() {
        let (serverFD, clientFD) = makeSocketPair()
        let transport = RecordingUpstreamTransport()
        let log = RecordingLogSink()
        let handler = Self.makeHandler(transport: transport, log: log)

        let thread = Thread { handler.handle(fileDescriptor: serverFD) }
        thread.start()
        sendRaw(clientFD, "GET /v1/widgets/abc HTTP/1.1\r\nHost: test\r\nTransfer-Encoding: gzip\r\n\r\n")
        _ = readAllUntilClose(clientFD)
        close(clientFD)

        #expect(transport.calls.isEmpty)
        #expect(log.entries.first?.result == .refused(reason: "the request's Transfer-Encoding is not a single 'chunked'"))
    }

    // MARK: - hostile chunked request bodies (round 3 review finding)
    //
    // `ForwardingHTTPRequestReader.readChunkedBody` parsed a chunk-size
    // line with a bare `Int(_:radix: 16)`, same as the response side's
    // reassembler -- the reviewers noted an unrelated `body.count + size
    // <= maxBodyBytes` check happened to keep this side safe from a
    // trapping negative index today, but it was never the thing enforcing
    // that. Every case here must come back a 400, never trap or hang.

    @Test(arguments: [
        "-100000",
        "-1",
        "+10",
        " 10",
        "0x10",
        "",
        "fffffffffffffffff",  // 17 hex digits
    ])
    func refusesAHostileChunkSizeLineInARequestBody(chunkSizeLine: String) {
        let (serverFD, clientFD) = makeSocketPair()
        let transport = RecordingUpstreamTransport()
        let log = RecordingLogSink()
        let handler = Self.makeHandler(transport: transport, log: log)

        let thread = Thread { handler.handle(fileDescriptor: serverFD) }
        thread.start()
        sendRaw(
            clientFD,
            "GET /v1/widgets/abc HTTP/1.1\r\nHost: test\r\nTransfer-Encoding: chunked\r\n\r\n\(chunkSizeLine)\r\n")
        let response = String(decoding: readAllUntilClose(clientFD), as: UTF8.self)
        close(clientFD)

        #expect(transport.calls.isEmpty, "chunkSizeLine='\(chunkSizeLine)'")
        #expect(response.hasPrefix("HTTP/1.1 400"), "chunkSizeLine='\(chunkSizeLine)'")
        #expect(
            log.entries.first?.result == .refused(reason: "malformed_request"), "chunkSizeLine='\(chunkSizeLine)'")
    }

    @Test func refusesAMalformedHeaderFieldName() {
        let (serverFD, clientFD) = makeSocketPair()
        let transport = RecordingUpstreamTransport()
        let log = RecordingLogSink()
        let handler = Self.makeHandler(transport: transport, log: log)

        let thread = Thread { handler.handle(fileDescriptor: serverFD) }
        thread.start()
        sendRaw(clientFD, "GET /v1/widgets/abc HTTP/1.1\r\nHost: test\r\nBad Name: value\r\n\r\n")
        _ = readAllUntilClose(clientFD)
        close(clientFD)

        #expect(transport.calls.isEmpty)
        #expect(log.entries.first?.result == .refused(reason: "a header field's name is not a valid HTTP field-name token"))
    }

    /// A pipelined second request on the same connection: the first
    /// request is otherwise entirely well-formed (`Content-Length: 0`, no
    /// body), so the only thing that can refuse it is the trailing bytes
    /// of the second request arriving on this same connection before this
    /// reader ever opens an upstream one.
    @Test func refusesAPipelinedSecondRequest() {
        let (serverFD, clientFD) = makeSocketPair()
        let transport = RecordingUpstreamTransport()
        let log = RecordingLogSink()
        let handler = Self.makeHandler(transport: transport, log: log)

        let thread = Thread { handler.handle(fileDescriptor: serverFD) }
        thread.start()
        sendRaw(
            clientFD,
            "GET /v1/widgets/abc HTTP/1.1\r\nHost: test\r\nContent-Length: 0\r\n\r\n"
                + "GET /v1/widgets/def HTTP/1.1\r\nHost: test\r\n\r\n")
        _ = readAllUntilClose(clientFD)
        close(clientFD)

        #expect(transport.calls.isEmpty)
        #expect(log.entries.first?.result == .refused(reason: "bytes after the first complete request begin a second request"))
    }

    /// Round 1 finding: the reader splits header lines only on an exact
    /// CRLF, so a bare LF inside a header value used to survive, unstripped
    /// and unrefused, into the bytes sent upstream — reintroducing exactly
    /// the `X-Client-Cert` header `ForwardingHeaderStripping` exists to
    /// remove, for any upstream parser tolerant enough to read a bare LF as
    /// its own line end. This pins that no byte of the smuggled header, or
    /// the request it rides in on, ever reaches the fake upstream at all:
    /// the whole request is refused before `upstream.forward` is called.
    @Test func refusesABareLFInAHeaderValueThatWouldSmuggleAClientCertHeader() {
        let (serverFD, clientFD) = makeSocketPair()
        let transport = RecordingUpstreamTransport()
        let log = RecordingLogSink()
        let handler = Self.makeHandler(transport: transport, log: log)

        let thread = Thread { handler.handle(fileDescriptor: serverFD) }
        thread.start()
        sendRaw(
            clientFD,
            "GET /v1/widgets/abc HTTP/1.1\r\n"
                + "Host: test\r\n"
                + "X-Session-Token: sess-123\nX-Client-Cert: smuggled-cert-bytes\r\n\r\n")
        _ = readAllUntilClose(clientFD)
        close(clientFD)

        #expect(transport.calls.isEmpty)
        #expect(log.entries.first?.result == .refused(reason: "a header value carries a disallowed control byte"))
    }

    /// The same smuggling attempt, with a bare CR in place of the bare LF.
    @Test func refusesABareCRInAHeaderValueThatWouldSmuggleAClientCertHeader() {
        let (serverFD, clientFD) = makeSocketPair()
        let transport = RecordingUpstreamTransport()
        let log = RecordingLogSink()
        let handler = Self.makeHandler(transport: transport, log: log)

        let thread = Thread { handler.handle(fileDescriptor: serverFD) }
        thread.start()
        sendRaw(
            clientFD,
            "GET /v1/widgets/abc HTTP/1.1\r\n"
                + "Host: test\r\n"
                + "X-Session-Token: sess-123\rX-Client-Cert: smuggled-cert-bytes\r\n\r\n")
        _ = readAllUntilClose(clientFD)
        close(clientFD)

        #expect(transport.calls.isEmpty)
        #expect(log.entries.first?.result == .refused(reason: "a header value carries a disallowed control byte"))
    }

    // MARK: - the forwarded request's framing always matches the body the daemon read
    // (round 4 security finding: a chunked request's body is de-chunked by
    // `ForwardingHTTPRequestReader` before it ever reaches the handler, so
    // passing the client's own `Transfer-Encoding`/`Content-Length` through
    // unchanged would let the Keel parse the de-chunked bytes as chunk
    // framing again -- an admitted local client's own chunk boundaries,
    // smuggled request included, landing inside what the Keel reads as this
    // one request's body.)

    @Test func forwardsADechunkedChunkedRequestWithNoTransferEncodingAndAMatchingContentLength() {
        let (serverFD, clientFD) = makeSocketPair()
        let transport = RecordingUpstreamTransport()
        let log = RecordingLogSink()
        let handler = Self.makeHandler(transport: transport, log: log)

        let thread = Thread { handler.handle(fileDescriptor: serverFD) }
        thread.start()
        sendRaw(
            clientFD,
            "GET /v1/widgets/abc HTTP/1.1\r\nHost: test\r\nTransfer-Encoding: chunked\r\n\r\n"
                + "4\r\nWiki\r\n5\r\npedia\r\n0\r\n\r\n")
        _ = readAllUntilClose(clientFD)
        close(clientFD)

        #expect(transport.calls.count == 1)
        let sentRequest = transport.calls[0].requestBytes
        let sentRequestString = String(decoding: sentRequest, as: UTF8.self)
        #expect(!sentRequestString.lowercased().contains("transfer-encoding"))
        #expect(sentRequestString.contains("Content-Length: 9\r\n"))
        #expect(sentRequest.suffix(9) == Array("Wikipedia".utf8))
    }

    /// The chunk DATA itself carries bytes that look like a terminating
    /// chunk plus a second request line. A reader that re-chunks or
    /// otherwise lets the Keel re-parse the body as chunk framing would
    /// split this into two requests; this pins that it is forwarded as the
    /// single request it is, with those bytes sitting inside the body and
    /// nowhere else.
    @Test func forwardsAChunkWhoseDataLooksLikeATerminatingChunkAndASecondRequestAsOneRequest() {
        let (serverFD, clientFD) = makeSocketPair()
        let transport = RecordingUpstreamTransport()
        let log = RecordingLogSink()
        let handler = Self.makeHandler(transport: transport, log: log)

        let smuggledBytes = Array("0\r\n\r\nGET /other HTTP/1.1\r\n".utf8)
        let chunkSizeHex = String(smuggledBytes.count, radix: 16)

        let thread = Thread { handler.handle(fileDescriptor: serverFD) }
        thread.start()
        sendRaw(clientFD, "GET /v1/widgets/abc HTTP/1.1\r\nHost: test\r\nTransfer-Encoding: chunked\r\n\r\n\(chunkSizeHex)\r\n")
        sendRawBytes(clientFD, smuggledBytes)
        sendRaw(clientFD, "\r\n0\r\n\r\n")
        _ = readAllUntilClose(clientFD)
        close(clientFD)

        #expect(transport.calls.count == 1)
        let sentRequest = transport.calls[0].requestBytes
        let sentRequestString = String(decoding: sentRequest, as: UTF8.self)
        #expect(!sentRequestString.lowercased().contains("transfer-encoding"))
        #expect(sentRequestString.contains("Content-Length: \(smuggledBytes.count)\r\n"))
        #expect(sentRequest.suffix(smuggledBytes.count) == smuggledBytes)
    }

    @Test func forwardsAContentLengthRequestWithTheDaemonsComputedLength() {
        let (serverFD, clientFD) = makeSocketPair()
        let transport = RecordingUpstreamTransport()
        let log = RecordingLogSink()
        let handler = Self.makeHandler(transport: transport, log: log)

        let thread = Thread { handler.handle(fileDescriptor: serverFD) }
        thread.start()
        sendRaw(clientFD, "GET /v1/widgets/abc HTTP/1.1\r\nHost: test\r\nContent-Length: 5\r\n\r\nhello")
        _ = readAllUntilClose(clientFD)
        close(clientFD)

        #expect(transport.calls.count == 1)
        let sentRequestString = String(decoding: transport.calls[0].requestBytes, as: UTF8.self)
        let contentLengthCount = sentRequestString.components(separatedBy: "Content-Length:").count - 1
        #expect(contentLengthCount == 1)
        #expect(sentRequestString.contains("Content-Length: 5\r\n\r\nhello"))
    }

    @Test func removesAMixedCaseTransferEncodingHeaderBeforeForwarding() {
        let (serverFD, clientFD) = makeSocketPair()
        let transport = RecordingUpstreamTransport()
        let log = RecordingLogSink()
        let handler = Self.makeHandler(transport: transport, log: log)

        let thread = Thread { handler.handle(fileDescriptor: serverFD) }
        thread.start()
        sendRaw(clientFD, "GET /v1/widgets/abc HTTP/1.1\r\nHost: test\r\ntransfer-ENCODING: chunked\r\n\r\n0\r\n\r\n")
        _ = readAllUntilClose(clientFD)
        close(clientFD)

        #expect(transport.calls.count == 1)
        let sentRequestString = String(decoding: transport.calls[0].requestBytes, as: UTF8.self)
        #expect(!sentRequestString.lowercased().contains("transfer-encoding"))
    }

    // MARK: - a matched request forwards correctly

    @Test func forwardsAMatchedRequestWithItsQueryStringUnchanged() {
        let (serverFD, clientFD) = makeSocketPair()
        let transport = RecordingUpstreamTransport()
        let log = RecordingLogSink()
        let handler = Self.makeHandler(bosunAddress: "127.0.0.1:9999", transport: transport, log: log)

        let thread = Thread { handler.handle(fileDescriptor: serverFD) }
        thread.start()
        sendRaw(clientFD, "GET /v1/widgets/abc?x=1&y=two HTTP/1.1\r\nHost: test\r\n\r\n")
        let response = String(decoding: readAllUntilClose(clientFD), as: UTF8.self)
        close(clientFD)

        #expect(transport.calls.count == 1)
        #expect(transport.calls[0].address == "127.0.0.1:9999")
        let sentRequest = String(decoding: transport.calls[0].requestBytes, as: UTF8.self)
        #expect(sentRequest.hasPrefix("GET /v1/widgets/abc?x=1&y=two HTTP/1.1\r\n"))
        #expect(response.hasPrefix("HTTP/1.1 200"))
        #expect(log.entries.first?.result == .forwarded(destination: .bosun, upstreamStatus: 200))
    }

    @Test func stripsClientIdentityHeadersAndKeepsAuthorization() {
        let (serverFD, clientFD) = makeSocketPair()
        let transport = RecordingUpstreamTransport()
        let log = RecordingLogSink()
        let handler = Self.makeHandler(transport: transport, log: log)

        let thread = Thread { handler.handle(fileDescriptor: serverFD) }
        thread.start()
        sendRaw(
            clientFD,
            "GET /v1/widgets/abc HTTP/1.1\r\n"
                + "Host: test\r\n"
                + "X-Client-Cert: deadbeefcertbytes\r\n"
                + "X-Forwarded-For: 10.1.2.3\r\n"
                + "Authorization: Bearer the-real-token\r\n\r\n")
        _ = readAllUntilClose(clientFD)
        close(clientFD)

        #expect(transport.calls.count == 1)
        let sentRequest = String(decoding: transport.calls[0].requestBytes, as: UTF8.self)
        #expect(!sentRequest.lowercased().contains("x-client-cert"))
        #expect(!sentRequest.lowercased().contains("x-forwarded-for"))
        #expect(!sentRequest.contains("deadbeefcertbytes"))
        #expect(!sentRequest.contains("10.1.2.3"))
        #expect(sentRequest.contains("Authorization: Bearer the-real-token"))
    }

    /// The upstream destination's own response, byte for byte, is what
    /// the local caller receives back.
    @Test func relaysTheUpstreamResponseBackToTheLocalCallerVerbatim() {
        let (serverFD, clientFD) = makeSocketPair()
        let responseBytes = Array("HTTP/1.1 201 Created\r\nContent-Length: 5\r\n\r\nhello".utf8)
        let transport = RecordingUpstreamTransport(outcome: .responded(statusCode: 201, responseBytes: responseBytes))
        let log = RecordingLogSink()
        let handler = Self.makeHandler(transport: transport, log: log)

        let thread = Thread { handler.handle(fileDescriptor: serverFD) }
        thread.start()
        sendRaw(clientFD, "GET /v1/widgets/abc HTTP/1.1\r\nHost: test\r\n\r\n")
        let response = readAllUntilClose(clientFD)
        close(clientFD)

        #expect(response == responseBytes)
        #expect(log.entries.first?.result == .forwarded(destination: .bosun, upstreamStatus: 201))
    }

    // MARK: - log line redaction

    /// The log line for a forwarded request carrying `Authorization` and
    /// a session token holds neither value — trivially true by
    /// `ForwardingLogEntry`'s own shape (it carries no header at all), but
    /// pinned here against the actual rendered text, end to end from a
    /// real request.
    @Test func theLogLineForAForwardedRequestHoldsNeitherAuthorizationNorASessionToken() {
        let (serverFD, clientFD) = makeSocketPair()
        let transport = RecordingUpstreamTransport()
        let log = RecordingLogSink()
        let handler = Self.makeHandler(transport: transport, log: log)

        let thread = Thread { handler.handle(fileDescriptor: serverFD) }
        thread.start()
        sendRaw(
            clientFD,
            "GET /v1/widgets/abc HTTP/1.1\r\n"
                + "Host: test\r\n"
                + "Authorization: Bearer the-real-token\r\n"
                + "X-Session-Token: the-session-token-value\r\n\r\n")
        _ = readAllUntilClose(clientFD)
        close(clientFD)

        #expect(log.entries.count == 1)
        let rendered = ForwardingLog.render(log.entries[0])
        #expect(!rendered.contains("the-real-token"))
        #expect(!rendered.contains("the-session-token-value"))
    }

    // MARK: - destination resolution

    @Test func resolvesALibraryDestinationToTheConfiguredLibraryAddress() {
        let (serverFD, clientFD) = makeSocketPair()
        let entry = ForwardingRouteEntry(route: Self.widgetRoute, destination: .library)
        let transport = RecordingUpstreamTransport()
        let log = RecordingLogSink()
        let handler = Self.makeHandler(
            allowlist: [entry], libraryAddress: "127.0.0.1:7777", bosunAddress: "127.0.0.1:9999",
            transport: transport, log: log)

        let thread = Thread { handler.handle(fileDescriptor: serverFD) }
        thread.start()
        sendRaw(clientFD, "GET /v1/widgets/abc HTTP/1.1\r\nHost: test\r\n\r\n")
        _ = readAllUntilClose(clientFD)
        close(clientFD)

        #expect(transport.calls.count == 1)
        #expect(transport.calls[0].address == "127.0.0.1:7777")
    }

    /// A route destined for the Library when no `libraryAddress` is
    /// configured is refused rather than dialled anywhere — this is the
    /// handler-level half of "an install without it serves no forwarding
    /// socket" (the daemon-level half, not starting the socket at all, is
    /// `ForwardingServingDecisionTests`).
    @Test func refusesALibraryDestinationWhenNoLibraryAddressIsConfigured() {
        let (serverFD, clientFD) = makeSocketPair()
        let entry = ForwardingRouteEntry(route: Self.widgetRoute, destination: .library)
        let transport = RecordingUpstreamTransport()
        let log = RecordingLogSink()
        let handler = Self.makeHandler(allowlist: [entry], libraryAddress: nil, transport: transport, log: log)

        let thread = Thread { handler.handle(fileDescriptor: serverFD) }
        thread.start()
        sendRaw(clientFD, "GET /v1/widgets/abc HTTP/1.1\r\nHost: test\r\n\r\n")
        let response = String(decoding: readAllUntilClose(clientFD), as: UTF8.self)
        close(clientFD)

        #expect(transport.calls.isEmpty)
        #expect(response.hasPrefix("HTTP/1.1 502"))
    }

    @Test func answersUpstreamUnreachableWithA502AndLogsItWithoutACrash() {
        let (serverFD, clientFD) = makeSocketPair()
        let transport = RecordingUpstreamTransport(outcome: .transportError(detail: "connection refused"))
        let log = RecordingLogSink()
        let handler = Self.makeHandler(transport: transport, log: log)

        let thread = Thread { handler.handle(fileDescriptor: serverFD) }
        thread.start()
        sendRaw(clientFD, "GET /v1/widgets/abc HTTP/1.1\r\nHost: test\r\n\r\n")
        let response = String(decoding: readAllUntilClose(clientFD), as: UTF8.self)
        close(clientFD)

        #expect(transport.calls.count == 1)
        #expect(response.hasPrefix("HTTP/1.1 502"))
        #expect(log.entries.first?.result == .upstreamUnreachable(destination: .bosun))
    }
}

} // extension PoolBlockingIntegrationSuites
