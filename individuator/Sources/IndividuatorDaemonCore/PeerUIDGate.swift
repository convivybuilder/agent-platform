import Foundation
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif

public enum PeerUIDGateError: Error, CustomStringConvertible, Equatable {
    case getpeereidFailed(errno: Int32)
    case uidMismatch(expected: uid_t, found: uid_t)

    public var description: String {
        switch self {
        case .getpeereidFailed(let err):
            return "getpeereid failed (errno \(err))"
        case .uidMismatch(let expected, let found):
            return "connecting uid \(found) is not the configured operator uid \(expected)"
        }
    }
}

/// The root daemon's one access-control check (CQ·447 slice 3). A
/// connection to the Unix socket is accepted only when its peer's REAL uid —
/// read from the kernel via `getpeereid`, never anything the peer can claim
/// in its request body — equals the configured operator uid. Checked before
/// a single byte of the request is read.
public enum PeerUIDGate {
    public static func check(fileDescriptor: Int32, requiredUID: uid_t) throws {
        var uid: uid_t = 0
        var gid: gid_t = 0
        guard getpeereid(fileDescriptor, &uid, &gid) == 0 else {
            throw PeerUIDGateError.getpeereidFailed(errno: errno)
        }
        guard uid == requiredUID else {
            throw PeerUIDGateError.uidMismatch(expected: requiredUID, found: uid)
        }
    }
}
