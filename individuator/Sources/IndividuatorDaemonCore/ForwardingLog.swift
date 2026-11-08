import Foundation
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif

/// What one forwarding-socket request's log line records, and nothing
/// more (0092: "For each request the log holds the time, the peer uid,
/// the method, the allowlist entry it matched or the reason it was
/// refused, and the Keel's response status. It never writes a header
/// value, a query string or a body..."). There is deliberately no field
/// here for a header, a target or a body: a type that never carries one
/// cannot leak one into the line `render` builds, the same discipline
/// `SessionEntry`/`SessionTable` apply to the Bosun's own session token.
public struct ForwardingLogEntry: Equatable {
    public enum Result: Equatable {
        case forwarded(destination: ForwardingDestination, upstreamStatus: Int)
        case upstreamUnreachable(destination: ForwardingDestination)
        case refused(reason: String)
    }

    public let time: Date
    public let peerUID: uid_t
    public let method: String
    public let result: Result

    public init(time: Date, peerUID: uid_t, method: String, result: Result) {
        self.time = time
        self.peerUID = peerUID
        self.method = method
        self.result = result
    }
}

/// Renders one `ForwardingLogEntry` as a single text line — pure, so a
/// test can check the exact text never carries a header value, a query
/// string or a body without ever starting the daemon's own accept loop.
public enum ForwardingLog {
    private static let isoFormatter: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()

    public static func render(_ entry: ForwardingLogEntry) -> String {
        let time = isoFormatter.string(from: entry.time)
        var fields = ["time=\(time)", "peer_uid=\(entry.peerUID)", "method=\(entry.method)"]
        switch entry.result {
        case .forwarded(let destination, let status):
            fields.append("result=forwarded")
            fields.append("destination=\(name(of: destination))")
            fields.append("upstream_status=\(status)")
        case .upstreamUnreachable(let destination):
            fields.append("result=refused")
            fields.append("destination=\(name(of: destination))")
            fields.append("reason=upstream_unreachable")
        case .refused(let reason):
            fields.append("result=refused")
            fields.append("reason=\(reason)")
        }
        return fields.joined(separator: " ")
    }

    private static func name(of destination: ForwardingDestination) -> String {
        switch destination {
        case .library: return "library"
        case .bosun: return "bosun"
        }
    }
}
