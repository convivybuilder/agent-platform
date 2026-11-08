import Foundation

/// A named reason the forwarding endpoint refuses a request, one case per
/// clause of 0092's matching rule: "Refuse, never normalise: a target that
/// is not origin form; a path with an empty segment, a `.` or `..`
/// segment, a backslash, or `%2f`, `%2e` or `%5c` in either case; a
/// fragment; a method other than the entry's; `CONNECT`; any path or host
/// outside the allowlist." `parameterCharacterClassViolation` is the one
/// addition beyond that sentence, for the character-class rule the next
/// sentence states.
public enum ForwardingRefusalReason: Error, Equatable, CustomStringConvertible {
    /// The request target is not in origin form (does not begin with
    /// `/`). Covers absolute-form (`http://host/path`), authority-form
    /// (`host:port`, what `CONNECT` ordinarily carries), and asterisk-form
    /// (`*`) alike — 0092 states one rule for all of them.
    case targetNotOriginForm
    case emptyPathSegment
    case dotSegment
    case dotDotSegment
    case backslashInPath
    case percentEncodedSlashInPath
    case percentEncodedDotInPath
    case percentEncodedBackslashInPath
    case fragmentPresent
    /// Checked on the method alone, before the target is examined at all,
    /// so a `CONNECT` whose authority-form target would otherwise read as
    /// `targetNotOriginForm` is still named for what it is.
    case connectMethod
    /// The path matches no allowlist entry's template, for any method.
    case noMatchingRoute
    /// The path matches some entry's template with every parameter valid,
    /// but not with the request's method.
    case methodNotAllowedForPath
    /// The path matches some entry's template in shape, but a parameter
    /// segment holds a byte outside `ForwardingRoute.parameterCharacterClass`.
    case parameterCharacterClassViolation

    public var description: String {
        switch self {
        case .targetNotOriginForm:
            return "the request target is not in origin form"
        case .emptyPathSegment:
            return "the path holds an empty segment"
        case .dotSegment:
            return "the path holds a '.' segment"
        case .dotDotSegment:
            return "the path holds a '..' segment"
        case .backslashInPath:
            return "the path holds a backslash"
        case .percentEncodedSlashInPath:
            return "the path holds a percent-encoded '/'"
        case .percentEncodedDotInPath:
            return "the path holds a percent-encoded '.'"
        case .percentEncodedBackslashInPath:
            return "the path holds a percent-encoded backslash"
        case .fragmentPresent:
            return "the request target carries a fragment"
        case .connectMethod:
            return "CONNECT is never forwarded"
        case .noMatchingRoute:
            return "the path matches no allowlisted route"
        case .methodNotAllowedForPath:
            return "the path is allowlisted, but not for this method"
        case .parameterCharacterClassViolation:
            return "a parameter segment holds a character outside its allowed class"
        }
    }
}

public struct ForwardingMatch: Equatable {
    public let route: ForwardingRoute
    /// The request target forwarded byte for byte exactly as it arrived —
    /// never rebuilt from the parsed segments, so what the Keel's router
    /// sees is provably the same bytes this matcher checked.
    public let target: [UInt8]
}

/// The forwarding endpoint's pure routing decision (0092 Handoff,
/// "Forwarding endpoint"; build plan slice S5a). Opens no socket, makes no
/// network call: given a method and a request target's raw bytes, it
/// either returns the one allowlist entry that target matches, or a named
/// reason to refuse. Nothing here decodes, case-folds, collapses or trims
/// the target before deciding — the whole point of refusing rather than
/// normalising is that a path this matcher rewrote before matching could
/// reach the Keel's router as a different path from the one it checked.
public enum ForwardingRequestMatcher {
    public static func match(
        method: String, target: [UInt8], allowlist: [ForwardingRoute]
    ) -> Result<ForwardingMatch, ForwardingRefusalReason> {
        if method == "CONNECT" {
            return .failure(.connectMethod)
        }
        guard target.first == UInt8(ascii: "/") else {
            return .failure(.targetNotOriginForm)
        }
        if target.contains(UInt8(ascii: "#")) {
            return .failure(.fragmentPresent)
        }

        let path: [UInt8]
        if let queryStart = target.firstIndex(of: UInt8(ascii: "?")) {
            path = Array(target[..<queryStart])
        } else {
            path = target
        }

        let segments = pathSegments(ofPath: path)
        for segment in segments {
            if let reason = structuralRefusal(ofSegment: segment) {
                return .failure(reason)
            }
        }

        var matchedRoutes: [ForwardingRoute] = []
        var sawWrongMethod = false
        var sawInvalidParameter = false
        for route in allowlist {
            switch shapeMatch(path: segments, route: route) {
            case .matched:
                if route.method == method {
                    matchedRoutes.append(route)
                } else {
                    sawWrongMethod = true
                }
            case .shapeOnlySomeParameterInvalid:
                sawInvalidParameter = true
            case .noShape:
                continue
            }
        }
        if let route = mostSpecific(ofMatches: matchedRoutes) {
            return .success(ForwardingMatch(route: route, target: target))
        }
        if sawWrongMethod {
            return .failure(.methodNotAllowedForPath)
        }
        if sawInvalidParameter {
            return .failure(.parameterCharacterClassViolation)
        }
        return .failure(.noMatchingRoute)
    }

    /// More than one entry can shape-match the same path: a bare
    /// `{doc_id...}` read and a more specific `{doc_id...}/checkout` entry
    /// both match `/reference/documents/a/b/checkout`, for instance,
    /// because the bare entry's multi-segment parameter is happy to
    /// swallow the trailing literal too (security review, E12-2 slice S5c
    /// round 3). The byte-for-byte target forwarded is identical either
    /// way and the destination the allowlist resolves is the same for both
    /// entries today, but the daemon's audit log attributes the call to
    /// whichever entry this returns, so the MOST SPECIFIC one — the one
    /// with the most literal segments — has to win regardless of the
    /// allowlist's own order. A tie keeps the first match in allowlist
    /// order, which is deterministic because the allowlist is fixed,
    /// in-code configuration.
    private static func mostSpecific(ofMatches matches: [ForwardingRoute]) -> ForwardingRoute? {
        matches.max(by: { literalSegmentCount(of: $0) < literalSegmentCount(of: $1) })
    }

    private static func literalSegmentCount(of route: ForwardingRoute) -> Int {
        route.segments.reduce(0) { count, segment in
            if case .literal = segment { return count + 1 }
            return count
        }
    }

    /// Splits a path that is known to start with `/` into its segments,
    /// preserving every empty segment a leading, trailing or doubled `/`
    /// produces — `Swift`'s own `split` drops those unless told not to,
    /// and an empty segment is exactly one of the things this matcher must
    /// catch rather than silently collapse.
    private static func pathSegments(ofPath path: [UInt8]) -> [[UInt8]] {
        var segments: [[UInt8]] = []
        var current: [UInt8] = []
        for byte in path.dropFirst() {
            if byte == UInt8(ascii: "/") {
                segments.append(current)
                current = []
            } else {
                current.append(byte)
            }
        }
        segments.append(current)
        return segments
    }

    private static func structuralRefusal(ofSegment segment: [UInt8]) -> ForwardingRefusalReason? {
        if segment.isEmpty { return .emptyPathSegment }
        if segment == Array(".".utf8) { return .dotSegment }
        if segment == Array("..".utf8) { return .dotDotSegment }
        if segment.contains(UInt8(ascii: "\\")) { return .backslashInPath }
        let lowered = asciiLowercased(segment)
        if containsSubsequence(lowered, Array("%2f".utf8)) { return .percentEncodedSlashInPath }
        if containsSubsequence(lowered, Array("%2e".utf8)) { return .percentEncodedDotInPath }
        if containsSubsequence(lowered, Array("%5c".utf8)) { return .percentEncodedBackslashInPath }
        return nil
    }

    private static func asciiLowercased(_ bytes: [UInt8]) -> [UInt8] {
        bytes.map { ($0 >= 0x41 && $0 <= 0x5A) ? $0 + 0x20 : $0 }
    }

    private static func containsSubsequence(_ haystack: [UInt8], _ needle: [UInt8]) -> Bool {
        guard !needle.isEmpty, haystack.count >= needle.count else { return false }
        for start in 0...(haystack.count - needle.count) {
            if Array(haystack[start..<(start + needle.count)]) == needle { return true }
        }
        return false
    }

    private enum ShapeMatch {
        case matched
        case shapeOnlySomeParameterInvalid
        case noShape
    }

    /// Whether `path`'s segments have the same shape as `route`'s
    /// template — same literal segments in the same places, and the right
    /// segment count — independently of whether every parameter's bytes
    /// pass the character class. Keeping those two questions separate is
    /// what lets the caller tell "no route has this shape" apart from
    /// "this route's shape fits, but a parameter doesn't".
    ///
    /// A multi-segment parameter that is followed by literal segments
    /// consumes every path segment up to the count those literal segments
    /// need (`suffixLiteralCount` below), so the literal segments still
    /// match the path's LAST segments exactly: a parameter segment that
    /// happens to repeat the literal suffix's own text (a `doc_id` segment
    /// literally named `checkout`, followed by the real `/checkout`) still
    /// matches, and a path missing the suffix, or carrying one extra
    /// trailing segment, still fails the final literal comparison. The
    /// parameter must consume at least one segment — an empty
    /// multi-segment parameter is refused the same as an empty ordinary
    /// segment would be.
    private static func shapeMatch(path: [[UInt8]], route: ForwardingRoute) -> ShapeMatch {
        var pathIndex = 0
        var allParametersValid = true
        for (templateIndex, templateSegment) in route.segments.enumerated() {
            switch templateSegment {
            case .literal(let bytes):
                guard pathIndex < path.count, path[pathIndex] == bytes else { return .noShape }
                pathIndex += 1
            case .parameter:
                guard pathIndex < path.count else { return .noShape }
                if !ForwardingRoute.segmentMatchesParameterClass(path[pathIndex]) {
                    allParametersValid = false
                }
                pathIndex += 1
            case .trailingMultiSegmentParameter:
                let suffixLiteralCount = route.segments.count - templateIndex - 1
                let consumeCount = path.count - pathIndex - suffixLiteralCount
                guard consumeCount >= 1 else { return .noShape }
                for _ in 0..<consumeCount {
                    if !ForwardingRoute.segmentMatchesParameterClass(path[pathIndex]) {
                        allParametersValid = false
                    }
                    pathIndex += 1
                }
            }
        }
        guard pathIndex == path.count else { return .noShape }
        return allParametersValid ? .matched : .shapeOnlySomeParameterInvalid
    }
}
