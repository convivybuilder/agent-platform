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
}
