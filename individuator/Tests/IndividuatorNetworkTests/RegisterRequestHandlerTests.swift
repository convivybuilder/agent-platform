import Foundation
import Testing
@testable import IndividuatorDaemonCore
@testable import IndividuatorNetwork
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif

private func makeSocketPair() -> (Int32, Int32) {
    var fds: [Int32] = [0, 0]
    let result = fds.withUnsafeMutableBufferPointer { socketpair(AF_UNIX, SOCK_STREAM, 0, $0.baseAddress) }
    precondition(result == 0, "socketpair() failed")
    // Both ends get SO_NOSIGPIPE: a write from either side (the handler's,
    // or this file's own `sendAndReceive`) can land after the other end has
    // already closed, and should return EPIPE rather than raise SIGPIPE and
    // kill the whole test process (individuatorPackageTests on CI).
    SocketIO.setNoSIGPIPE(fileDescriptor: fds[0])
    SocketIO.setNoSIGPIPE(fileDescriptor: fds[1])
    return (fds[0], fds[1])
}

private final class RecordingTransport: RegistrationTransport {
    private(set) var callCount = 0
    let outcome: RegistrationOutcome
    init(outcome: RegistrationOutcome) { self.outcome = outcome }
    func register(account: String, callerKind: String, ceiling: String, protocolVersion: String) -> RegistrationOutcome {
        callCount += 1
        return outcome
    }
}

private func sendAndReceive(_ fd: Int32, peer: Int32, request: [String: Any]) -> [String: Any]? {
    var line = try! JSONSerialization.data(withJSONObject: request)
    line.append(UInt8(ascii: "\n"))
    line.withUnsafeBytes { ptr in _ = write(fd, ptr.baseAddress, ptr.count) }

    var responseBytes: [UInt8] = []
    var byte: UInt8 = 0
    while true {
        let n = read(fd, &byte, 1)
        if n <= 0 || byte == UInt8(ascii: "\n") { break }
        responseBytes.append(byte)
    }
    return try? JSONSerialization.jsonObject(with: Data(responseBytes)) as? [String: Any]
}

/// The root daemon's Unix socket offers exactly one operation. These tests
/// drive `RegisterRequestHandler` directly over a real connected socket
/// pair (never the full accept loop, which is thin, untested-by-design IO
/// glue — see `RegisterSocketServer`'s own doc comment).
@Suite struct RegisterRequestHandlerTests {
    @Test func dispatchesRegisterAndReturnsItsOutcome() {
        let (serverFD, clientFD) = makeSocketPair()
        let transport = RecordingTransport(outcome: .registered(sessionID: "sid", sessionToken: "tok"))
        let handler = RegisterRequestHandler(requiredUID: getuid(), transport: transport, account: "operator")

        let thread = Thread { handler.handle(fileDescriptor: serverFD) }
        thread.start()
        let response = sendAndReceive(
            clientFD, peer: serverFD,
            request: ["op": "register", "caller_kind": "operator", "ceiling": "ceil", "protocol_version": "1"])
        close(clientFD)

        #expect(transport.callCount == 1)
        #expect(response?["ok"] as? Bool == true)
        #expect(response?["session_id"] as? String == "sid")
        #expect(response?["session_token"] as? String == "tok")
    }

    @Test func refusesAnyOperationOtherThanRegister() {
        let (serverFD, clientFD) = makeSocketPair()
        let transport = RecordingTransport(outcome: .registered(sessionID: "sid", sessionToken: "tok"))
        let handler = RegisterRequestHandler(requiredUID: getuid(), transport: transport, account: "operator")

        let thread = Thread { handler.handle(fileDescriptor: serverFD) }
        thread.start()
        let response = sendAndReceive(clientFD, peer: serverFD, request: ["op": "sign", "data": "YQ=="])
        close(clientFD)

        #expect(transport.callCount == 0)
        #expect(response?["ok"] as? Bool == false)
        #expect(response?["refused"] as? String == "unknown_operation")
    }

    @Test func refusesAConnectionFromAnyOtherConfiguredUID() {
        let (serverFD, clientFD) = makeSocketPair()
        let transport = RecordingTransport(outcome: .registered(sessionID: "sid", sessionToken: "tok"))
        let handler = RegisterRequestHandler(requiredUID: getuid() + 1, transport: transport, account: "operator")

        let thread = Thread { handler.handle(fileDescriptor: serverFD) }
        thread.start()
        let response = sendAndReceive(clientFD, peer: serverFD, request: ["op": "register"])
        close(clientFD)

        #expect(transport.callCount == 0)
        #expect(response?["ok"] as? Bool == false)
        #expect(response?["refused"] as? String == "peer_uid_not_operator")
    }

    /// Reproduces the swift CI crash (signal 13, SIGPIPE) in
    /// `individuatorPackageTests` on GitHub-hosted macos-latest runners
    /// (run 38704394655, run 38727421254): the peer end is closed before
    /// `handle` writes, deterministically rather than by winning a thread
    /// race, so the handler's own refusal write is what hits the closed
    /// peer.
    @Test func writesARefusalAfterThePeerHasAlreadyClosed() {
        let (serverFD, clientFD) = makeSocketPair()
        close(clientFD)
        let transport = RecordingTransport(outcome: .registered(sessionID: "sid", sessionToken: "tok"))
        let handler = RegisterRequestHandler(requiredUID: getuid() + 1, transport: transport, account: "operator")

        handler.handle(fileDescriptor: serverFD)

        #expect(transport.callCount == 0)
    }

    /// Same closed-peer write, on the path where the request was valid and
    /// already fully buffered (so the handler reads it, dispatches, and
    /// only its *response* write hits the closed peer).
    @Test func writesARegistrationResponseAfterThePeerHasAlreadyClosed() {
        let (serverFD, clientFD) = makeSocketPair()
        var line = try! JSONSerialization.data(
            withJSONObject: ["op": "register", "caller_kind": "operator", "ceiling": "ceil", "protocol_version": "1"])
        line.append(UInt8(ascii: "\n"))
        line.withUnsafeBytes { ptr in _ = write(clientFD, ptr.baseAddress, ptr.count) }
        close(clientFD)

        let transport = RecordingTransport(outcome: .registered(sessionID: "sid", sessionToken: "tok"))
        let handler = RegisterRequestHandler(requiredUID: getuid(), transport: transport, account: "operator")

        handler.handle(fileDescriptor: serverFD)

        #expect(transport.callCount == 1)
    }

    /// The other half of the same CI crash: a *test client's* write after
    /// the handler has already closed its end — this is what
    /// `sendAndReceive` hits when the handler thread wins the race in
    /// `refusesAConnectionFromAnyOtherConfiguredUID`, made deterministic
    /// here by running the (synchronous, immediate) refusal to completion
    /// before the client ever writes.
    @Test func clientWriteAfterTheHandlerHasAlreadyClosedReturnsEPIPERatherThanRaisingSIGPIPE() {
        let (serverFD, clientFD) = makeSocketPair()
        let transport = RecordingTransport(outcome: .registered(sessionID: "sid", sessionToken: "tok"))
        let handler = RegisterRequestHandler(requiredUID: getuid() + 1, transport: transport, account: "operator")

        handler.handle(fileDescriptor: serverFD)

        var line = try! JSONSerialization.data(withJSONObject: ["op": "register"])
        line.append(UInt8(ascii: "\n"))
        let written: Int = line.withUnsafeBytes { ptr in write(clientFD, ptr.baseAddress, ptr.count) }
        let writeErrno = errno
        close(clientFD)

        #expect(written == -1)
        #expect(writeErrno == EPIPE)
    }
}
