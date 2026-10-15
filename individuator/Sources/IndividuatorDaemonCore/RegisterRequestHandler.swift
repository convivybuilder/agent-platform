import Foundation
import IndividuatorNetwork
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif

/// Handles exactly one already-accepted connection to the root daemon's
/// Unix socket (CQ·447 slice 3). The socket offers exactly one operation —
/// `register` — and this type's `switch` on the request's `op` field has
/// exactly one case; there is no second operation here, and in particular
/// none that signs caller-supplied bytes.
///
/// `account` is fixed at daemon startup from the configured operator uid
/// (looked up once via `getpwuid`, by the daemon's own entry point — never
/// read from the request), per 0092 decision #2: "the operator account the
/// Individuator observed as its local peer" is the Individuator's own
/// assertion, not something a caller supplies.
public struct RegisterRequestHandler {
    public let requiredUID: uid_t
    public let transport: RegistrationTransport
    public let account: String

    public init(requiredUID: uid_t, transport: RegistrationTransport, account: String) {
        self.requiredUID = requiredUID
        self.transport = transport
        self.account = account
    }

    /// Checks the peer's uid first (`PeerUIDGate`) — before a single byte
    /// of the request is read — then parses and dispatches the one
    /// request this socket accepts, retrying once on
    /// `session_machine_mismatch` (`RegistrationReattempt`), and writes one
    /// JSON response line back. Always closes `fileDescriptor` on return.
    public func handle(fileDescriptor: Int32) {
        defer { close(fileDescriptor) }
        SocketIO.setNoSIGPIPE(fileDescriptor: fileDescriptor)

        do {
            try PeerUIDGate.check(fileDescriptor: fileDescriptor, requiredUID: requiredUID)
        } catch {
            SocketIO.writeJSONLine(fileDescriptor: fileDescriptor, object: ["ok": false, "refused": "peer_uid_not_operator"])
            return
        }

        guard let line = SocketIO.readLineBounded(fileDescriptor: fileDescriptor),
              let requestData = line.data(using: .utf8),
              let request = try? JSONSerialization.jsonObject(with: requestData) as? [String: Any],
              let op = request["op"] as? String
        else {
            SocketIO.writeJSONLine(fileDescriptor: fileDescriptor, object: ["ok": false, "refused": "malformed_request"])
            return
        }

        guard op == "register" else {
            SocketIO.writeJSONLine(fileDescriptor: fileDescriptor, object: ["ok": false, "refused": "unknown_operation"])
            return
        }

        guard let callerKind = request["caller_kind"] as? String,
              let ceiling = request["ceiling"] as? String,
              let protocolVersion = request["protocol_version"] as? String
        else {
            SocketIO.writeJSONLine(fileDescriptor: fileDescriptor, object: ["ok": false, "refused": "malformed_request"])
            return
        }

        let outcome = RegistrationReattempt.register(
            transport: transport, account: account, callerKind: callerKind, ceiling: ceiling,
            protocolVersion: protocolVersion)
        SocketIO.writeJSONLine(fileDescriptor: fileDescriptor, object: Self.responseBody(for: outcome))
    }

    static func responseBody(for outcome: RegistrationOutcome) -> [String: Any] {
        switch outcome {
        case .registered(let sessionID, let sessionToken):
            return ["ok": true, "session_id": sessionID, "session_token": sessionToken]
        case .refused(let reason, let detail):
            return ["ok": false, "refused": reason, "detail": detail]
        case .serverCertificateRefused(let reason):
            return ["ok": false, "refused": "bosun_server_certificate_refused", "detail": ["reason": reason.description]]
        case .transportError(let detail):
            return ["ok": false, "refused": "transport_error", "detail": ["detail": detail]]
        }
    }
}
