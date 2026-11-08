import Foundation

/// What `ForwardingUpstreamResponseReassembler.feed` returns after each
/// chunk of bytes NIO hands `_ForwardingResponseCollector`: the response is
/// not yet whole, it is now whole (carrying the exact raw bytes received,
/// head and body together, verbatim), or the bytes seen so far make it
/// impossible to call whole at all.
enum ForwardingUpstreamResponseReassemblyOutcome: Equatable {
    case needsMoreBytes
    case complete(responseBytes: [UInt8])
    case malformed(detail: String)
}

/// What `ForwardingUpstreamResponseReassembler.connectionClosed` returns:
/// the connection's own close is this response's framing and the response
/// is therefore whole, or it closed before the response it had already
/// started declaring was.
enum ForwardingUpstreamResponseCloseOutcome: Equatable {
    case complete(responseBytes: [UInt8])
    case malformed(detail: String)
}

/// Reassembles one upstream HTTP/1 response out of however many pieces NIO
/// happens to deliver it in -- one TLS record's worth, several, or a single
/// `channelRead` split mid status line, mid header, or exactly at the
/// head/body boundary. Reads a response's completeness off its own framing
/// rather than off the connection's close, which is right on principle even
/// though round 6 found that neither it nor the close it replaced was the
/// cause of round 3/4's CI failure — see `parseStatusCode`'s own doc
/// comment for that cause. Pure: fed bytes, and told when the connection
/// closes; opens no socket and reads nothing itself.
///
/// Three ways a response is framed, read from its own head the same way
/// `ForwardingHTTPRequestReader` reads them for a request on the other side
/// of this daemon: a single valid `Content-Length`, a single
/// `Transfer-Encoding: chunked`, or -- carrying neither -- the connection's
/// own close. Carrying both headers, or `Content-Length` more than once, is
/// never resolved by guessing which one governs; it is read as a malformed
/// response instead.
struct ForwardingUpstreamResponseReassembler {
    private var buffer: [UInt8] = []
    private var framing: Framing?

    private enum Framing {
        case contentLength(headEnd: Int, bodyLength: Int)
        case chunked(headEnd: Int)
        case untilClose
    }

    /// How many bytes this reassembler has been fed in total, whole response
    /// or not — what `_ForwardingResponseCollector` reports back to
    /// `ForwardingUpstreamClient` as "what the client saw" when a forward
    /// fails for any reason (round 5).
    var bytesReceived: Int { buffer.count }

    mutating func feed(_ bytes: [UInt8]) -> ForwardingUpstreamResponseReassemblyOutcome {
        buffer.append(contentsOf: bytes)
        return evaluate()
    }

    /// An empty buffer and one that has not yet reassembled a complete
    /// response head are the same fact: no response has started arriving
    /// (or finished starting), so the connection's close is not that
    /// response's framing — it is the absence of one (round 5 finding: the
    /// previous "could not parse a status line" message this case used to
    /// surface as, downstream, did not say that zero or a handful of bytes
    /// had arrived, which is exactly what a client-certificate rejection
    /// closing the handshake with no application data looks like on the
    /// wire).
    func connectionClosed() -> ForwardingUpstreamResponseCloseOutcome {
        switch framing {
        case .untilClose:
            return .complete(responseBytes: buffer)
        case .none:
            return .malformed(
                detail: "upstream closed the connection before sending a response (\(buffer.count) bytes received)")
        case .contentLength, .chunked:
            return .malformed(detail: "the connection closed before the declared response body arrived")
        }
    }

    /// `ForwardingSizeLimits.maxHeadBytes` plus `.maxBodyBytes`: the most
    /// this reassembler ever lets `buffer` grow to before calling a
    /// response malformed, checked on every `feed` regardless of framing
    /// (round 2 finding). Nothing else bounds a response framed by the
    /// connection's own close — `.untilClose` has no declared length to
    /// check against a cap the way `Content-Length` and a chunked body's
    /// running total do below — and nothing bounds the head itself before
    /// `headEndIndex` finds its terminating blank line, so this single
    /// check covers both.
    private static let maxBufferedBytes = ForwardingSizeLimits.maxHeadBytes + ForwardingSizeLimits.maxBodyBytes

    private mutating func evaluate() -> ForwardingUpstreamResponseReassemblyOutcome {
        guard buffer.count <= Self.maxBufferedBytes else {
            return .malformed(
                detail: "the response exceeded \(Self.maxBufferedBytes) bytes (the head limit plus the body cap) without completing")
        }
        if framing == nil {
            guard let headEnd = Self.headEndIndex(buffer) else { return .needsMoreBytes }
            switch Self.parseFraming(buffer, headEnd: headEnd) {
            case .failure(let detail):
                return .malformed(detail: detail)
            case .success(let parsedFraming):
                framing = parsedFraming
            }
        }
        switch framing! {
        case .contentLength(let headEnd, let bodyLength):
            // Both operands are already bounded (`bodyLength` by the
            // `maxBodyBytes` check in `parseFraming`, `headEnd` by the
            // `maxBufferedBytes` guard above), so this never actually
            // overflows -- checked anyway because a declared length near
            // `Int.max` is exactly what used to reach Swift's trapping
            // `+` here and kill the daemon (round 2 finding).
            let (needed, overflowed) = headEnd.addingReportingOverflow(bodyLength)
            guard !overflowed else {
                return .malformed(detail: "the response's declared length overflowed while reassembling it")
            }
            guard buffer.count >= needed else { return .needsMoreBytes }
            return .complete(responseBytes: Array(buffer[0..<needed]))
        case .chunked(let headEnd):
            switch Self.chunkedBodyEnd(buffer, from: headEnd) {
            case .needsMoreBytes:
                return .needsMoreBytes
            case .malformed(let detail):
                return .malformed(detail: detail)
            case .found(let end):
                return .complete(responseBytes: Array(buffer[0..<end]))
            }
        case .untilClose:
            return .needsMoreBytes
        }
    }

    // MARK: - head

    private static func headEndIndex(_ bytes: [UInt8]) -> Int? {
        guard bytes.count >= 4 else { return nil }
        var i = 0
        while i + 3 < bytes.count {
            if bytes[i] == 0x0D, bytes[i + 1] == 0x0A, bytes[i + 2] == 0x0D, bytes[i + 3] == 0x0A {
                return i + 4
            }
            i += 1
        }
        return nil
    }

    private enum FramingParseResult {
        case success(Framing)
        case failure(String)
    }

    private static func parseFraming(_ bytes: [UInt8], headEnd: Int) -> FramingParseResult {
        let lines = splitLines(Array(bytes[0..<headEnd]))
        var contentLengthValues: [String] = []
        var transferEncodingValues: [String] = []
        for line in lines.dropFirst() {
            guard let colonIndex = line.firstIndex(of: 0x3A) else {
                return .failure("a response header line could not be parsed")
            }
            let name = String(decoding: line[..<colonIndex], as: UTF8.self)
            let value = trimOWS(String(decoding: line[(colonIndex + 1)...], as: UTF8.self))
            if name.caseInsensitiveCompare("Content-Length") == .orderedSame {
                contentLengthValues.append(value)
            } else if name.caseInsensitiveCompare("Transfer-Encoding") == .orderedSame {
                transferEncodingValues.append(value)
            }
        }

        if !contentLengthValues.isEmpty && !transferEncodingValues.isEmpty {
            return .failure("the response carries both Transfer-Encoding and Content-Length")
        }
        if contentLengthValues.count > 1 {
            return .failure("the response carries more than one Content-Length header")
        }
        if let value = contentLengthValues.first {
            guard let length = exactNonNegativeByteCount(value) else {
                return .failure("the response's Content-Length is not a single exact non-negative byte count")
            }
            // Refused here, before `length` ever reaches the `headEnd +
            // bodyLength` sum in `evaluate`, rather than left to overflow
            // that sum's trapping `+` -- a Content-Length near `Int.max`
            // fits in `Int` and parses clean, so only a cap check here
            // stops it (round 2 finding). The cap is the one
            // `ForwardingHTTPRequestReader` already applies to a request
            // body, reused rather than a second number invented for the
            // response side.
            guard length <= ForwardingSizeLimits.maxBodyBytes else {
                return .failure(
                    "the response's Content-Length of \(length) bytes exceeds the maximum body size of \(ForwardingSizeLimits.maxBodyBytes) bytes")
            }
            return .success(.contentLength(headEnd: headEnd, bodyLength: length))
        }
        if !transferEncodingValues.isEmpty {
            guard transferEncodingValues.count == 1, transferEncodingValues[0].lowercased() == "chunked" else {
                return .failure("the response's Transfer-Encoding is not a single 'chunked'")
            }
            return .success(.chunked(headEnd: headEnd))
        }
        return .success(.untilClose)
    }

    /// `headBytes` always ends with the `\r\n\r\n` `headEndIndex` matched;
    /// drops only that final blank line's own terminator, never the `\r\n`
    /// ending the line immediately before it.
    private static func splitLines(_ headBytes: [UInt8]) -> [[UInt8]] {
        let withoutFinalBlankLine = Array(headBytes.dropLast(2))
        var lines: [[UInt8]] = []
        var current: [UInt8] = []
        var i = 0
        while i < withoutFinalBlankLine.count {
            if withoutFinalBlankLine[i] == 0x0D, i + 1 < withoutFinalBlankLine.count, withoutFinalBlankLine[i + 1] == 0x0A {
                lines.append(current)
                current = []
                i += 2
            } else {
                current.append(withoutFinalBlankLine[i])
                i += 1
            }
        }
        if !current.isEmpty { lines.append(current) }
        return lines
    }

    private static func trimOWS(_ value: String) -> String {
        var bytes = Array(value.utf8)
        while let first = bytes.first, first == 0x20 || first == 0x09 { bytes.removeFirst() }
        while let last = bytes.last, last == 0x20 || last == 0x09 { bytes.removeLast() }
        return String(decoding: bytes, as: UTF8.self)
    }

    private static func exactNonNegativeByteCount(_ value: String) -> Int? {
        guard !value.isEmpty else { return nil }
        guard value.utf8.allSatisfy({ $0 >= UInt8(ascii: "0") && $0 <= UInt8(ascii: "9") }) else { return nil }
        return Int(value)
    }

    // MARK: - chunked body

    private enum ChunkedScanResult {
        case needsMoreBytes
        case malformed(String)
        case found(Int)
    }

    /// Scans forward from `start` (the index right after the response
    /// head) over however many chunk-size/data/CRLF groups have arrived so
    /// far, stopping as soon as the terminating zero-size chunk's own final
    /// blank line has too -- never decoding or re-encoding the body, since
    /// the bytes this returns carry it to the local caller exactly as the
    /// Keel destination framed it.
    private static func chunkedBodyEnd(_ bytes: [UInt8], from start: Int) -> ChunkedScanResult {
        var i = start
        // The running total of every chunk's `size` seen so far, checked
        // against the cap on each chunk (round 2 finding): a sequence of
        // chunks each under the cap individually can still sum past it,
        // which a per-chunk check alone would miss.
        var totalBodyBytes = 0
        while true {
            guard let sizeLineEnd = crlfIndex(bytes, from: i) else { return .needsMoreBytes }
            // `ForwardingChunkSizeParser` already refuses anything over
            // `ForwardingSizeLimits.maxBodyBytes` (round 3 finding: a bare
            // `Int(_:radix: 16)` accepted a leading `-`, and a negative
            // size passed a bare `size <= maxBodyBytes` check straight
            // through to a trapping negative array index), so `size` here
            // is always in `0...maxBodyBytes`.
            let size: Int
            switch ForwardingChunkSizeParser.parse(Array(bytes[i..<sizeLineEnd])) {
            case .malformed(let detail):
                return .malformed(detail)
            case .size(let parsedSize):
                size = parsedSize
            }
            let dataStart = sizeLineEnd + 2
            if size == 0 {
                var trailerStart = dataStart
                while true {
                    guard let trailerLineEnd = crlfIndex(bytes, from: trailerStart) else { return .needsMoreBytes }
                    if trailerLineEnd == trailerStart { return .found(trailerLineEnd + 2) }
                    trailerStart = trailerLineEnd + 2
                }
            }
            let (newTotal, totalOverflowed) = totalBodyBytes.addingReportingOverflow(size)
            guard !totalOverflowed, newTotal <= ForwardingSizeLimits.maxBodyBytes else {
                return .malformed(
                    "a chunked response's body exceeds the maximum body size of \(ForwardingSizeLimits.maxBodyBytes) bytes")
            }
            totalBodyBytes = newTotal
            let (chunkEnd, chunkEndOverflowed) = dataStart.addingReportingOverflow(size)
            guard !chunkEndOverflowed else {
                return .malformed("a chunked response's chunk-size line overflowed while locating its data")
            }
            guard bytes.count >= chunkEnd + 2 else { return .needsMoreBytes }
            guard bytes[chunkEnd] == 0x0D, bytes[chunkEnd + 1] == 0x0A else {
                return .malformed("a chunked response's chunk data was not followed by CRLF")
            }
            i = chunkEnd + 2
        }
    }

    /// The index of the next CRLF at or after `from`, or `nil` if none has
    /// arrived yet -- `nil` here always means "need more bytes", never
    /// "this framing is malformed": a buffer that does not yet carry a CRLF
    /// looks identical to one that never will, and the latter is exactly
    /// what `connectionClosed()` catches on its own once the connection
    /// actually closes.
    private static func crlfIndex(_ bytes: [UInt8], from: Int) -> Int? {
        guard from <= bytes.count else { return nil }
        var i = from
        while i + 1 < bytes.count {
            if bytes[i] == 0x0D, bytes[i + 1] == 0x0A { return i }
            i += 1
        }
        return nil
    }
}
