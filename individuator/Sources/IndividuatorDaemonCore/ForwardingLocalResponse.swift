import Foundation
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif

/// Writes a plain, locally-generated HTTP/1.1 response on the forwarding
/// socket — used for every refusal (admission, framing, matching) and for
/// relaying a Keel destination's own response verbatim. Never anything
/// fancier than a status line, three headers and a body: this is a local
/// control socket talking to this machine's own clients, not a general
/// HTTP server.
enum ForwardingLocalResponse {
    static func writeRefusal(fileDescriptor: Int32, statusCode: Int, reasonPhrase: String, detail: String) {
        write(fileDescriptor: fileDescriptor, statusCode: statusCode, reasonPhrase: reasonPhrase, bodyText: detail)
    }

    /// Writes `rawResponseBytes` — a Keel destination's own response, head
    /// and body, exactly as `ForwardingUpstreamClient` received it — back
    /// on the local socket unchanged. Nothing here re-parses or rebuilds
    /// it: the local caller sees exactly what the Keel sent.
    static func writeUpstreamResponse(fileDescriptor: Int32, rawResponseBytes: [UInt8]) {
        SocketIO.writeAll(fileDescriptor: fileDescriptor, data: Data(rawResponseBytes))
    }

    private static func write(fileDescriptor: Int32, statusCode: Int, reasonPhrase: String, bodyText: String) {
        let bodyBytes = Array(bodyText.utf8)
        var head = "HTTP/1.1 \(statusCode) \(reasonPhrase)\r\n"
        head += "Content-Type: text/plain; charset=utf-8\r\n"
        head += "Content-Length: \(bodyBytes.count)\r\n"
        head += "Connection: close\r\n\r\n"
        var data = Data(head.utf8)
        data.append(contentsOf: bodyBytes)
        SocketIO.writeAll(fileDescriptor: fileDescriptor, data: data)
    }
}
