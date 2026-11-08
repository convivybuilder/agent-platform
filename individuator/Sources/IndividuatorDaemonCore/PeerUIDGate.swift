import Foundation
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif

public enum PeerUIDGateError: Error, CustomStringConvertible, Equatable {
    case getpeereidFailed(errno: Int32)
    case uidNotAdmitted(found: uid_t)

    public var description: String {
        switch self {
        case .getpeereidFailed(let err):
            return "getpeereid failed (errno \(err))"
        case .uidNotAdmitted(let found):
            return "connecting uid \(found) is not an admitted account"
        }
    }
}

/// The root daemon's first access-control check (CQ·447 slice 3; extended
/// by E12·2 from one admitted uid to a configured list). A connection to the
/// Unix socket is accepted only when its peer's REAL uid — read from the
/// kernel via `getpeereid`, never anything the peer can claim in its request
/// body — is one of the admitted uids. Checked before a single byte of the
/// request is read, and returns that uid so the caller can resolve which
/// admitted account it belongs to without a second trip through
/// `getpeereid`.
///
/// Shaped so a second check can be added after this one succeeds and
/// before `check` returns — the code-signature check of E12·6 goes there.
/// No such check exists yet.
public enum PeerUIDGate {
    public static func check(fileDescriptor: Int32, admittedUIDs: Set<uid_t>) throws -> uid_t {
        var uid: uid_t = 0
        var gid: gid_t = 0
        guard getpeereid(fileDescriptor, &uid, &gid) == 0 else {
            throw PeerUIDGateError.getpeereidFailed(errno: errno)
        }
        guard admittedUIDs.contains(uid) else {
            throw PeerUIDGateError.uidNotAdmitted(found: uid)
        }
        return uid
    }
}
