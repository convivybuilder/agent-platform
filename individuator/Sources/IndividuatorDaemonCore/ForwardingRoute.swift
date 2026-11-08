import Foundation

/// One segment of a route's path template (0092 Handoff, "Forwarding
/// endpoint"): a literal segment, a single-segment parameter, or a
/// multi-segment parameter that swallows one or more path segments. A
/// multi-segment parameter may be the template's last segment, or may be
/// followed by one or more literal segments (never by another parameter);
/// at most one multi-segment parameter is allowed per template
/// (`ForwardingRoute.init`'s preconditions).
public enum ForwardingPathSegment: Equatable {
    case literal([UInt8])
    case parameter(name: String)
    case trailingMultiSegmentParameter(name: String)
}

/// One entry in the Individuator's fixed forwarding allowlist (0092): one
/// HTTP method paired with one path template. This slice's allowlist holds
/// only synthetic test entries; the real route list — the Library protocol
/// routes and the Bosun's run-register and session-view paths — is slice
/// S5c.
public struct ForwardingRoute: Equatable {
    public let method: String
    public let segments: [ForwardingPathSegment]

    /// `path` is a template such as `/v1/widgets/{id}`,
    /// `/v1/widgets/{id}/parts/{rest...}` or
    /// `/v1/widgets/{rest...}/archive`: literal segments, `{name}`
    /// single-segment parameters, and at most one `{name...}` multi-segment
    /// parameter, which may be the template's last segment or may be
    /// followed by one or more literal segments. A malformed template is a
    /// programmer error in this fixed, in-code allowlist, never request
    /// data, so this traps rather than throwing.
    public init(method: String, path: String) {
        precondition(path.hasPrefix("/"), "a route's path template must be in origin form: \(path)")
        let parts = path.dropFirst().split(separator: "/", omittingEmptySubsequences: false)
        precondition(!parts.contains(""), "a route's path template may not hold an empty segment: \(path)")

        var segments: [ForwardingPathSegment] = []
        var sawMultiSegmentParameter = false
        for part in parts {
            if part.hasPrefix("{") && part.hasSuffix("}") {
                let name = String(part.dropFirst().dropLast())
                if name.hasSuffix("...") {
                    precondition(
                        !sawMultiSegmentParameter,
                        "a route template may hold at most one multi-segment parameter: \(path)")
                    segments.append(.trailingMultiSegmentParameter(name: String(name.dropLast(3))))
                    sawMultiSegmentParameter = true
                } else {
                    precondition(
                        !sawMultiSegmentParameter,
                        "only literal segments may follow a multi-segment parameter: \(path)")
                    segments.append(.parameter(name: name))
                }
            } else {
                segments.append(.literal(Array(part.utf8)))
            }
        }
        self.method = method
        self.segments = segments
    }

    /// The fixed character class one parameter segment's bytes must all
    /// belong to: RFC 3986's `pchar` (`unreserved / sub-delims / ":" /
    /// "@"`) with `%` removed. `%` is excluded even though RFC 3986 admits
    /// it as `pct-encoded`, because a parameter is matched byte for byte
    /// with no percent-decoding (0092: "Matching reads the path alone ...
    /// refuse, never normalise"); admitting `%` here would let a
    /// percent-encoded `/` or `.` cross a parameter boundary as something
    /// other than the literal three bytes it is. `/` and `\` are outside
    /// `pchar` already — they are named below only because 0092's own
    /// wording names them.
    static let parameterCharacterClass: Set<UInt8> = {
        let allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._~:@!$&'()*+,;=-"
        return Set(allowed.utf8)
    }()

    static func segmentMatchesParameterClass(_ segment: [UInt8]) -> Bool {
        !segment.isEmpty && segment.allSatisfy { parameterCharacterClass.contains($0) }
    }
}
