import Foundation

/// One HTTP header field, name and value kept apart exactly as the request
/// head carried them (no case-folding at this layer — the comparisons
/// below fold case themselves, deliberately, rather than relying on a
/// caller having already done it).
public struct ForwardingHeaderField: Equatable {
    public let name: String
    public let value: String

    public init(name: String, value: String) {
        self.name = name
        self.value = value
    }
}

/// The parsed head of one HTTP/1 request: its header fields, in the order
/// they arrived. The request line (method and target) is `ForwardingRequestMatcher`'s
/// concern, not this type's.
public struct ForwardingRequestHead: Equatable {
    public let headerFields: [ForwardingHeaderField]

    public init(headerFields: [ForwardingHeaderField]) {
        self.headerFields = headerFields
    }
}

/// A named reason the forwarding endpoint refuses a request for ambiguous
/// HTTP/1 framing (build plan slice S5a, "framing"): the request matcher
/// bounds only the first request line and target on a connection, so
/// anything that could make a later reader of the same byte stream parse a
/// different request boundary than this check did must be refused here
/// instead.
public enum ForwardingFramingRefusalReason: Equatable, CustomStringConvertible {
    case malformedHeaderFieldName
    /// The request target carries a bare CR, a bare LF, a NUL, or any
    /// other control byte besides HTAB — never a legitimate request
    /// target, and never forwarded: a bare LF in particular, surviving
    /// into the bytes sent upstream, is exactly what would let a line a
    /// tolerant parser on the Keel's end treats as its own reintroduce a
    /// header `ForwardingHeaderStripping` already removed.
    case controlByteInRequestTarget
    /// The same refusal as `controlByteInRequestTarget`, for a header
    /// field's value rather than the request target. `ForwardingFramingCheck`
    /// already refuses a header field NAME that is not a valid token
    /// (`malformedHeaderFieldName`); this is the matching check for the
    /// value, which that token check does not cover.
    case controlByteInHeaderValue
    case transferEncodingAndContentLength
    case multipleContentLengthHeaders
    case contentLengthNotAnExactByteCount
    case transferEncodingNotSingleChunked
    case pipelinedSecondRequest

    public var description: String {
        switch self {
        case .malformedHeaderFieldName:
            return "a header field's name is not a valid HTTP field-name token"
        case .controlByteInRequestTarget:
            return "the request target carries a disallowed control byte"
        case .controlByteInHeaderValue:
            return "a header value carries a disallowed control byte"
        case .transferEncodingAndContentLength:
            return "the request carries both Transfer-Encoding and Content-Length"
        case .multipleContentLengthHeaders:
            return "the request carries more than one Content-Length header"
        case .contentLengthNotAnExactByteCount:
            return "the request's Content-Length is not a single exact non-negative byte count"
        case .transferEncodingNotSingleChunked:
            return "the request's Transfer-Encoding is not a single 'chunked'"
        case .pipelinedSecondRequest:
            return "bytes after the first complete request begin a second request"
        }
    }
}

/// Checks a parsed request head, and whatever bytes followed the first
/// complete request on the same connection, for the framing ambiguities an
/// exact-match path/method allowlist cannot see on its own. Pure: it reads
/// what it is handed and decides, opening no socket and reading no further
/// bytes itself.
public enum ForwardingFramingCheck {
    /// `target` is the request line's raw target bytes, forwarded byte for
    /// byte by `ForwardingConnectionHandler` exactly as `ForwardingRequestMatcher`
    /// matched it; defaults to empty for a caller (existing tests) that has
    /// no target to check. `trailingBytes` is whatever the connection
    /// carried after the first request's own framing (its headers, plus
    /// whatever body its own `Content-Length` or chunked encoding declared)
    /// ended. Each local request gets its own upstream connection, never a
    /// reused one, so any non-empty `trailingBytes` can only be a second
    /// request attempting to pipeline onto the first, and is refused
    /// regardless of what it contains.
    public static func check(
        head: ForwardingRequestHead, trailingBytes: [UInt8], target: [UInt8] = []
    ) -> ForwardingFramingRefusalReason? {
        if containsDisallowedControlByte(target) {
            return .controlByteInRequestTarget
        }

        // A header field name is refused outright, rather than normalised
        // and matched anyway, when it is not a valid RFC 7230 `token` —
        // this is what catches a would-be Content-Length or
        // Transfer-Encoding whose name carries whitespace before where its
        // colon was, or an obs-fold continuation line's own empty name,
        // either of which would otherwise just drop silently out of the
        // count below instead of being seen as the ambiguity it is.
        for field in head.headerFields {
            if !isValidHeaderFieldNameToken(field.name) {
                return .malformedHeaderFieldName
            }
            // The reader splits header lines on an exact CRLF, so a bare CR
            // or bare LF surviving inside a parsed value is never part of a
            // real line terminator — it is bytes a well-formed line could
            // never carry, and (round 1 finding) exactly what would let a
            // stripped `X-Client-Cert` or `X-Forwarded-*` header reappear on
            // the Keel's side of this socket if its own parser reads a bare
            // LF as a line end.
            if containsDisallowedControlByte(Array(field.value.utf8)) {
                return .controlByteInHeaderValue
            }
        }

        let transferEncodingValues = headerValues(head, named: "Transfer-Encoding")
        let contentLengthValues = headerValues(head, named: "Content-Length")

        if !transferEncodingValues.isEmpty && !contentLengthValues.isEmpty {
            return .transferEncodingAndContentLength
        }
        if contentLengthValues.count > 1 {
            return .multipleContentLengthHeaders
        }
        if let contentLengthValue = contentLengthValues.first,
            !isExactNonNegativeByteCount(contentLengthValue)
        {
            return .contentLengthNotAnExactByteCount
        }
        if !transferEncodingValues.isEmpty {
            guard
                transferEncodingValues.count == 1,
                trimmedOptionalWhitespace(transferEncodingValues[0]).lowercased() == "chunked"
            else {
                return .transferEncodingNotSingleChunked
            }
        }
        if !trailingBytes.isEmpty {
            return .pipelinedSecondRequest
        }
        return nil
    }

    /// `true` for any byte that is CR, LF, NUL, or any other C0 control
    /// byte or DEL — every one of those except HTAB (0x09), which RFC 7230
    /// itself permits inside a header value as `OWS`/`BWS`.
    private static func containsDisallowedControlByte(_ bytes: [UInt8]) -> Bool {
        bytes.contains { byte in
            byte != 0x09 && (byte < 0x20 || byte == 0x7F)
        }
    }

    private static func headerValues(_ head: ForwardingRequestHead, named name: String) -> [String] {
        head.headerFields
            .filter { $0.name.caseInsensitiveCompare(name) == .orderedSame }
            .map(\.value)
    }

    /// RFC 7230's `OWS` is exactly `*( SP / HTAB )` — not `Foundation`'s
    /// broader Unicode whitespace category, which would trim bytes HTTP
    /// never permits as padding around a field value.
    private static func trimmedOptionalWhitespace(_ value: String) -> String {
        var bytes = Array(value.utf8)
        while let first = bytes.first, first == 0x20 || first == 0x09 {
            bytes.removeFirst()
        }
        while let last = bytes.last, last == 0x20 || last == 0x09 {
            bytes.removeLast()
        }
        return String(decoding: bytes, as: UTF8.self)
    }

    /// A Content-Length value is accepted only when, once its surrounding
    /// OWS is removed, it is one or more ASCII digits (RFC 7230
    /// `1*DIGIT`) that fit a non-negative 64-bit integer — never a
    /// comma-joined list, a sign, a non-decimal form, an empty value, or a
    /// count too large to state exactly.
    private static func isExactNonNegativeByteCount(_ value: String) -> Bool {
        let trimmed = trimmedOptionalWhitespace(value)
        guard !trimmed.isEmpty else { return false }
        guard trimmed.utf8.allSatisfy({ $0 >= UInt8(ascii: "0") && $0 <= UInt8(ascii: "9") }) else {
            return false
        }
        guard let count = UInt64(trimmed) else { return false }
        return count <= UInt64(Int64.max)
    }

    /// RFC 7230's header field-name grammar is `token`: one or more
    /// `tchar`, never whitespace, never empty.
    private static func isValidHeaderFieldNameToken(_ name: String) -> Bool {
        guard !name.isEmpty else { return false }
        return name.utf8.allSatisfy(isHeaderFieldNameTokenCharacter)
    }

    private static func isHeaderFieldNameTokenCharacter(_ byte: UInt8) -> Bool {
        switch byte {
        case UInt8(ascii: "!"), UInt8(ascii: "#"), UInt8(ascii: "$"), UInt8(ascii: "%"),
            UInt8(ascii: "&"), UInt8(ascii: "'"), UInt8(ascii: "*"), UInt8(ascii: "+"),
            UInt8(ascii: "-"), UInt8(ascii: "."), UInt8(ascii: "^"), UInt8(ascii: "_"),
            UInt8(ascii: "`"), UInt8(ascii: "|"), UInt8(ascii: "~"):
            return true
        case UInt8(ascii: "0")...UInt8(ascii: "9"),
            UInt8(ascii: "A")...UInt8(ascii: "Z"),
            UInt8(ascii: "a")...UInt8(ascii: "z"):
            return true
        default:
            return false
        }
    }
}
