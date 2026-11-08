import Foundation
import IndividuatorNetwork
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif

/// Qualified POSIX `read(2)`, never the bare name: this type's own static
/// `read(fileDescriptor:)` would otherwise shadow the global function at
/// every call site inside this file, the same clash `KeyFileStore.swift`
/// avoids by qualifying `write` as `Darwin.write`/`Glibc.write`.
private func posixRead(_ fileDescriptor: Int32, _ buffer: UnsafeMutableRawPointer?, _ count: Int) -> Int {
    #if canImport(Darwin)
    return Darwin.read(fileDescriptor, buffer, count)
    #else
    return Glibc.read(fileDescriptor, buffer, count)
    #endif
}

/// What `ForwardingHTTPRequestReader.read` returns: either a well-formed
/// request (method, raw target bytes, parsed header fields, and body), a
/// named `ForwardingFramingCheck` refusal, or a local parse failure this
/// reader found on its own (a malformed request line, an unreadable
/// header, or a bound this reader enforces that `ForwardingFramingCheck`
/// has no case for).
public enum ForwardingHTTPReadOutcome {
    case success(method: String, target: [UInt8], head: ForwardingRequestHead, body: [UInt8])
    /// `method` is always known here: the framing check only ever runs
    /// once the request line has already been parsed.
    case framingRefused(method: String, reason: ForwardingFramingRefusalReason)
    /// `method` is `nil` only when the failure happened before (or while)
    /// parsing the request line itself; once a method is known, every
    /// later failure on the same request carries it.
    case malformed(method: String?, detail: String)
    /// The request did not complete within `read`'s own `timeoutSeconds`,
    /// counted from the moment this call started rather than reset by
    /// every byte that arrives (round 1 finding): one admitted account
    /// that connects and sends nothing, or sends slowly, must not be able
    /// to hold this connection — and, before this fix, the whole serial
    /// accept loop behind it — open indefinitely by trickling bytes in
    /// just fast enough to dodge a per-read idle timer. `detail` names the
    /// deadline for the local 408 response and the log line alike.
    case timedOut(detail: String)
}

/// Reads one HTTP/1 request off an already-accepted connection to the
/// forwarding socket (0092: "A client sends an ordinary HTTP request to
/// that socket."). Bounded throughout — a request head over
/// `maxHeadBytes`, or a body over `maxBodyBytes`, is refused rather than
/// read without limit — because this reads from whatever admitted local
/// process connects, not from a fixed, trusted peer.
///
/// Runs `ForwardingFramingCheck` TWICE, by design: once right after the
/// head is parsed (before any body is read at all, so a malformed
/// Content-Length or a Transfer-Encoding/Content-Length conflict is
/// refused before this reader ever tries to read a body those headers
/// cannot be trusted to size), and again after the body is fully read and
/// whatever immediately followed it on the connection is drained — ONLY
/// that second call can see a non-empty `trailingBytes`, which is what
/// lets it catch a pipelined second request (0092 Handoff, "framing").
public enum ForwardingHTTPRequestReader {
    /// Reused by `ForwardingUpstreamResponseReassembler` for the response
    /// side of the same socket (E12·2 slice S5b round 2 finding): one pair
    /// of numbers, defined on `ForwardingSizeLimits` where both the
    /// request and response readers can reach it, never a second number
    /// invented on either side.
    public static let maxHeadBytes = ForwardingSizeLimits.maxHeadBytes
    public static let maxBodyBytes = ForwardingSizeLimits.maxBodyBytes
    /// The whole request — head and body together — must complete within
    /// this many seconds of `read` being called, or the connection is
    /// refused (round 1 finding: without this, one admitted account that
    /// connects and sends nothing, or sends slowly, stalls the daemon's
    /// forwarding of every other account's requests). 30s matches the
    /// `responseTimeoutSeconds` default `BosunRegistrationClient` and its
    /// siblings already use for a comparable end-to-end bound.
    public static let defaultTimeoutSeconds: TimeInterval = 30

    enum ReadError: Error, CustomStringConvertible {
        case headTooLarge
        case connectionClosedBeforeHead
        case malformedRequestLine
        case malformedHeaderLine
        case malformedChunkSize
        case bodyTooLarge
        case connectionClosedBeforeBody
        case requestReadTimedOut

        var description: String {
            switch self {
            case .headTooLarge: return "the request head exceeded \(maxHeadBytes) bytes"
            case .connectionClosedBeforeHead: return "the connection closed before a complete request head arrived"
            case .malformedRequestLine: return "the request line could not be parsed"
            case .malformedHeaderLine: return "a header line could not be parsed"
            case .malformedChunkSize: return "a chunked body's chunk-size line could not be parsed"
            case .bodyTooLarge: return "the request body exceeded \(maxBodyBytes) bytes"
            case .connectionClosedBeforeBody: return "the connection closed before the declared body arrived"
            case .requestReadTimedOut: return "the request did not complete within the read deadline"
            }
        }
    }

    /// One absolute point in time this read must finish by, recomputed
    /// against `now()` before every single syscall rather than reset by
    /// each one that returns — the only shape that bounds the WHOLE
    /// request rather than just the idle gap between bytes, so a trickle
    /// of single bytes can never extend it past `deadline`.
    private struct ReadDeadline {
        let deadline: Date
        let now: () -> Date

        var remainingSeconds: Double { deadline.timeIntervalSince(now()) }
    }

    public static func read(
        fileDescriptor: Int32, timeoutSeconds: TimeInterval = defaultTimeoutSeconds, now: @escaping () -> Date = Date.init
    ) -> ForwardingHTTPReadOutcome {
        let deadline = ReadDeadline(deadline: now().addingTimeInterval(timeoutSeconds), now: now)

        let headBytes: [UInt8]
        do {
            headBytes = try readHeadBytes(fileDescriptor: fileDescriptor, deadline: deadline)
        } catch ReadError.requestReadTimedOut {
            return .timedOut(detail: ReadError.requestReadTimedOut.description)
        } catch {
            return .malformed(method: nil, detail: describe(error))
        }

        let lines = splitLines(headBytes)
        guard let requestLine = lines.first else {
            return .malformed(method: nil, detail: ReadError.malformedRequestLine.description)
        }
        let method: String
        let target: [UInt8]
        do {
            (method, target) = try parseRequestLine(requestLine)
        } catch {
            return .malformed(method: nil, detail: describe(error))
        }

        var headerFields: [ForwardingHeaderField] = []
        for line in lines.dropFirst() {
            do {
                headerFields.append(try parseHeaderLine(line))
            } catch {
                return .malformed(method: method, detail: describe(error))
            }
        }
        let head = ForwardingRequestHead(headerFields: headerFields)

        if let reason = ForwardingFramingCheck.check(head: head, trailingBytes: [], target: target) {
            return .framingRefused(method: method, reason: reason)
        }

        let body: [UInt8]
        do {
            if let contentLength = contentLengthValue(head) {
                guard contentLength <= maxBodyBytes else { throw ReadError.bodyTooLarge }
                body = try readExactly(fileDescriptor: fileDescriptor, count: contentLength, deadline: deadline)
            } else if isChunked(head) {
                body = try readChunkedBody(fileDescriptor: fileDescriptor, deadline: deadline)
            } else {
                body = []
            }
        } catch ReadError.requestReadTimedOut {
            return .timedOut(detail: ReadError.requestReadTimedOut.description)
        } catch {
            return .malformed(method: method, detail: describe(error))
        }

        let trailingBytes = peekTrailingBytes(fileDescriptor: fileDescriptor)
        if let reason = ForwardingFramingCheck.check(head: head, trailingBytes: trailingBytes, target: target) {
            return .framingRefused(method: method, reason: reason)
        }

        return .success(method: method, target: target, head: head, body: body)
    }

    private static func describe(_ error: Error) -> String {
        (error as? ReadError)?.description ?? "\(error)"
    }

    /// Arms `fileDescriptor`'s `SO_RCVTIMEO` to `deadline`'s remaining time
    /// before every read, then issues exactly one `read(2)` — never a
    /// timeout computed once outside the loop, which would bound only the
    /// gap since the LAST byte rather than the time since this read began.
    /// `SO_RCVTIMEO` of exactly zero means "no timeout" (block forever) on
    /// Darwin and Linux alike, so a remaining time under one microsecond is
    /// rounded UP to one rather than down to zero — the deadline has
    /// already passed in that case regardless, and the guard above this
    /// call's own call sites catches it before the next read is even
    /// attempted.
    private static func boundedRead(
        fileDescriptor: Int32, buffer: UnsafeMutableRawPointer?, count: Int, deadline: ReadDeadline
    ) throws -> Int {
        guard deadline.remainingSeconds > 0 else { throw ReadError.requestReadTimedOut }
        setReadTimeout(fileDescriptor: fileDescriptor, seconds: deadline.remainingSeconds)
        let n = posixRead(fileDescriptor, buffer, count)
        if n < 0, errno == EAGAIN || errno == EWOULDBLOCK {
            throw ReadError.requestReadTimedOut
        }
        return n
    }

    private static func setReadTimeout(fileDescriptor: Int32, seconds: Double) {
        let microseconds = max(1, Int64((seconds * 1_000_000).rounded(.up)))
        var tv = timeval()
        tv.tv_sec = Int(microseconds / 1_000_000)
        tv.tv_usec = Int32(microseconds % 1_000_000)
        setsockopt(fileDescriptor, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
    }

    // MARK: - head

    private static func readHeadBytes(fileDescriptor: Int32, deadline: ReadDeadline) throws -> [UInt8] {
        var bytes: [UInt8] = []
        var byte: UInt8 = 0
        while true {
            if bytes.count >= maxHeadBytes { throw ReadError.headTooLarge }
            let n = try boundedRead(fileDescriptor: fileDescriptor, buffer: &byte, count: 1, deadline: deadline)
            guard n > 0 else { throw ReadError.connectionClosedBeforeHead }
            bytes.append(byte)
            let c = bytes.count
            if c >= 4, bytes[c - 4] == 0x0D, bytes[c - 3] == 0x0A, bytes[c - 2] == 0x0D, bytes[c - 1] == 0x0A {
                return bytes
            }
        }
    }

    /// Splits `headBytes` (known to end with the `\r\n\r\n` that terminated
    /// `readHeadBytes`) into its request-line and header lines, dropping
    /// only the final blank line's own terminator — never the `\r\n` that
    /// ends the line immediately before it.
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

    private static func parseRequestLine(_ bytes: [UInt8]) throws -> (method: String, target: [UInt8]) {
        let parts = splitOnByte(bytes, 0x20)
        guard parts.count == 3, !parts[0].isEmpty, !parts[1].isEmpty, !parts[2].isEmpty,
              let method = String(bytes: parts[0], encoding: .utf8)
        else {
            throw ReadError.malformedRequestLine
        }
        return (method, parts[1])
    }

    private static func parseHeaderLine(_ bytes: [UInt8]) throws -> ForwardingHeaderField {
        guard let colonIndex = bytes.firstIndex(of: 0x3A) else { throw ReadError.malformedHeaderLine }
        let nameBytes = Array(bytes[..<colonIndex])
        let valueBytes = Array(bytes[(colonIndex + 1)...])
        guard let name = String(bytes: nameBytes, encoding: .utf8),
              let rawValue = String(bytes: valueBytes, encoding: .utf8)
        else {
            throw ReadError.malformedHeaderLine
        }
        return ForwardingHeaderField(name: name, value: trimOWS(rawValue))
    }

    private static func splitOnByte(_ bytes: [UInt8], _ delimiter: UInt8) -> [[UInt8]] {
        var result: [[UInt8]] = []
        var current: [UInt8] = []
        for byte in bytes {
            if byte == delimiter {
                result.append(current)
                current = []
            } else {
                current.append(byte)
            }
        }
        result.append(current)
        return result
    }

    private static func trimOWS(_ value: String) -> String {
        var bytes = Array(value.utf8)
        while let first = bytes.first, first == 0x20 || first == 0x09 { bytes.removeFirst() }
        while let last = bytes.last, last == 0x20 || last == 0x09 { bytes.removeLast() }
        return String(decoding: bytes, as: UTF8.self)
    }

    /// `field.value` reaching here has already passed `ForwardingFramingCheck
    /// .isExactNonNegativeByteCount` at this `read`'s earlier `check` call
    /// (before any body is read) -- digits only, no sign, non-empty, at
    /// most `Int64.max` -- so a bare `Int(_:)` here never sees a value
    /// that check would have refused (round 3 review: checked while
    /// looking for this same gap in the chunk-size parser below, found
    /// already closed here).
    private static func contentLengthValue(_ head: ForwardingRequestHead) -> Int? {
        guard let field = head.headerFields.first(where: { $0.name.caseInsensitiveCompare("Content-Length") == .orderedSame })
        else { return nil }
        return Int(field.value)
    }

    private static func isChunked(_ head: ForwardingRequestHead) -> Bool {
        head.headerFields.contains { $0.name.caseInsensitiveCompare("Transfer-Encoding") == .orderedSame }
    }

    // MARK: - body

    private static func readExactly(fileDescriptor: Int32, count: Int, deadline: ReadDeadline) throws -> [UInt8] {
        guard count > 0 else { return [] }
        var bytes: [UInt8] = []
        bytes.reserveCapacity(count)
        var chunk = [UInt8](repeating: 0, count: min(count, 64 * 1024))
        while bytes.count < count {
            let toRead = min(chunk.count, count - bytes.count)
            let n = try chunk.withUnsafeMutableBytes { ptr in
                try boundedRead(fileDescriptor: fileDescriptor, buffer: ptr.baseAddress, count: toRead, deadline: deadline)
            }
            guard n > 0 else { throw ReadError.connectionClosedBeforeBody }
            bytes.append(contentsOf: chunk[0..<n])
        }
        return bytes
    }

    /// Reads one line (not including its own `\r\n`) off the connection,
    /// bounded by `maxBytes` — used for a chunked body's chunk-size lines
    /// and trailer header lines, both of which are ordinarily tiny.
    private static func readLineCRLF(fileDescriptor: Int32, maxBytes: Int, deadline: ReadDeadline) throws -> [UInt8] {
        var bytes: [UInt8] = []
        var byte: UInt8 = 0
        while true {
            if bytes.count >= maxBytes { throw ReadError.headTooLarge }
            let n = try boundedRead(fileDescriptor: fileDescriptor, buffer: &byte, count: 1, deadline: deadline)
            guard n > 0 else { throw ReadError.connectionClosedBeforeBody }
            if byte == 0x0A, bytes.last == 0x0D {
                bytes.removeLast()
                return bytes
            }
            bytes.append(byte)
        }
    }

    private static func readChunkedBody(fileDescriptor: Int32, deadline: ReadDeadline) throws -> [UInt8] {
        var body: [UInt8] = []
        while true {
            let sizeLine = try readLineCRLF(fileDescriptor: fileDescriptor, maxBytes: 256, deadline: deadline)
            // `ForwardingChunkSizeParser` is shared with
            // `ForwardingUpstreamResponseReassembler` (round 3 finding: a
            // bare `Int(_:radix: 16)` accepted a leading `-`, which an
            // unrelated count check happened to keep safe on this side
            // today, but was never the thing enforcing that).
            let size: Int
            switch ForwardingChunkSizeParser.parse(sizeLine) {
            case .malformed:
                throw ReadError.malformedChunkSize
            case .size(let parsedSize):
                size = parsedSize
            }
            if size == 0 {
                // Trailer headers, if any, end with a blank line — the
                // same terminator the request head itself ends with.
                while true {
                    let trailerLine = try readLineCRLF(fileDescriptor: fileDescriptor, maxBytes: maxHeadBytes, deadline: deadline)
                    if trailerLine.isEmpty { break }
                }
                break
            }
            guard body.count + size <= maxBodyBytes else { throw ReadError.bodyTooLarge }
            body.append(contentsOf: try readExactly(fileDescriptor: fileDescriptor, count: size, deadline: deadline))
            // Each chunk's data is followed by its own trailing CRLF.
            _ = try readLineCRLF(fileDescriptor: fileDescriptor, maxBytes: 2, deadline: deadline)
        }
        return body
    }

    /// Grabs whatever bytes are already sitting in the connection's receive
    /// buffer, without blocking — a non-empty result means the peer wrote
    /// more than the request this reader just finished parsing, which can
    /// only be an attempted pipelined second request (0092 Handoff,
    /// "framing"): each local connection is forwarded at most once, never
    /// reused. `MSG_DONTWAIT` returns immediately with nothing when the
    /// peer has sent no more, rather than waiting to see if it ever does.
    private static func peekTrailingBytes(fileDescriptor: Int32, maxBytes: Int = 4096) -> [UInt8] {
        var buffer = [UInt8](repeating: 0, count: maxBytes)
        let n = buffer.withUnsafeMutableBytes { ptr -> Int in
            recv(fileDescriptor, ptr.baseAddress, maxBytes, MSG_DONTWAIT)
        }
        guard n > 0 else { return [] }
        return Array(buffer[0..<n])
    }
}
