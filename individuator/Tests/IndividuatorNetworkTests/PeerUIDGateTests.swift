import Foundation
import Testing
@testable import IndividuatorDaemonCore
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif

/// A connected `AF_UNIX` socket pair, both ends owned by this same test
/// process — so the peer's real uid, as `getpeereid` reports it, is always
/// this process's own `getuid()`. A test that wants "refused" therefore
/// configures a DIFFERENT required uid (`getuid() + 1`, the same idiom
/// `KeyFileStoreTests.validateFileRefusesAWrongOwner` uses for an owner
/// mismatch it cannot otherwise simulate without a real privilege change),
/// rather than trying to connect from a second uid.
private func makeSocketPair() -> (Int32, Int32) {
    var fds: [Int32] = [0, 0]
    let result = fds.withUnsafeMutableBufferPointer { socketpair(AF_UNIX, SOCK_STREAM, 0, $0.baseAddress) }
    precondition(result == 0, "socketpair() failed")
    return (fds[0], fds[1])
}

@Suite struct PeerUIDGateTests {
    @Test func acceptsTheActualPeerUID() throws {
        let (a, b) = makeSocketPair()
        defer { close(a); close(b) }
        let found = try PeerUIDGate.check(fileDescriptor: a, admittedUIDs: [getuid()])
        #expect(found == getuid())
    }

    /// Two admitted uids: the connecting peer is one of them, and the gate
    /// returns that one rather than refusing just because the set has more
    /// than one member (E12·2 — a second admitted account must not narrow
    /// what the first account could already do).
    @Test func acceptsThePeerUIDWhenItIsOneOfSeveralAdmittedUIDs() throws {
        let (a, b) = makeSocketPair()
        defer { close(a); close(b) }
        let found = try PeerUIDGate.check(fileDescriptor: a, admittedUIDs: [getuid(), getuid() + 1, getuid() + 2])
        #expect(found == getuid())
    }

    @Test func refusesAnUnlistedUID() {
        let (a, b) = makeSocketPair()
        defer { close(a); close(b) }
        let wrongUID = getuid() + 1

        #expect(throws: PeerUIDGateError.uidNotAdmitted(found: getuid())) {
            try PeerUIDGate.check(fileDescriptor: a, admittedUIDs: [wrongUID])
        }
    }
}
