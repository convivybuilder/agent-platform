import Foundation
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif

/// Tiny, bounded read/write helpers over a raw POSIX socket file
/// descriptor — no `FileHandle`, so a test can drive these against one end
/// of a real `socketpair(AF_UNIX, SOCK_STREAM, ...)` without Foundation
/// ever taking ownership of (and auto-closing) the descriptor.
enum SocketIO {
    /// Reads up to `maxBytes` one byte at a time, stopping at the first
    /// `\n` (not included in the result) or at EOF. Bounded so a peer that
    /// never sends a newline cannot hold this read open indefinitely —
    /// this is a local control socket restricted to one uid
    /// (`PeerUIDGate`), not a listener exposed to any machine, so a size
    /// bound rather than a wall-clock deadline is the gate that matters
    /// here.
    static func readLineBounded(fileDescriptor: Int32, maxBytes: Int = 64 * 1024) -> String? {
        var bytes: [UInt8] = []
        var byte: UInt8 = 0
        while bytes.count < maxBytes {
            let n = read(fileDescriptor, &byte, 1)
            if n <= 0 { break }
            if byte == UInt8(ascii: "\n") { break }
            bytes.append(byte)
        }
        guard !bytes.isEmpty else { return nil }
        return String(decoding: bytes, as: UTF8.self)
    }

    static func writeAll(fileDescriptor: Int32, data: Data) {
        var remaining = data
        while !remaining.isEmpty {
            let written = remaining.withUnsafeBytes { ptr -> Int in
                write(fileDescriptor, ptr.baseAddress, ptr.count)
            }
            if written <= 0 { return }
            remaining = remaining.dropFirst(written)
        }
    }

    static func writeJSONLine(fileDescriptor: Int32, object: [String: Any]) {
        guard var data = try? JSONSerialization.data(withJSONObject: object) else { return }
        data.append(UInt8(ascii: "\n"))
        writeAll(fileDescriptor: fileDescriptor, data: data)
    }
}
