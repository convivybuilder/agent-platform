import Foundation
import IndividuatorNetwork
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif

/// Handles exactly one already-accepted connection to the root daemon's
/// Unix socket (CQ·447 slice 3). The socket offers exactly two operations —
/// `register` and `authorize` (E12·2 slice S3) — and this type's `switch`
/// on the request's `op` field has exactly those two cases; there is no
/// third operation here, and in particular none that signs caller-supplied
/// bytes.
///
/// `account` is resolved per connection from the peer's uid, as reported by
/// `PeerUIDGate` (never read from the request body, and never fixed once at
/// daemon startup — E12·2: a second account registering is a second
/// connection with a different peer uid, not a different daemon), per 0092
/// decision #2: "the operator account the Individuator observed as its
/// local peer" is the Individuator's own assertion, not something a caller
/// supplies.
///
/// `authorize` RELAYS THE BOSUN'S DECISION; IT DOES NOT ENFORCE ANYTHING OF
/// ITS OWN. Per platform/decisions/0057, stage one is a policy and audit
/// layer, not containment or isolation — this handler adds no boundary
/// beyond the handle lookup below. The Bosun's own tool surface for agents
/// is E12·7's concern, not this one's (CQ·365).
public struct RegisterRequestHandler {
    public let admittedAccounts: [uid_t: String]
    public let transport: RegistrationTransport
    /// Where `authorize` relays a looked-up handle's call (E12·2 slice S3).
    public let authorizationTransport: AuthorizationTransport
    /// Where a successful registration's Bosun session token is parked
    /// (E12·2 slice S2): `handle` stores it here and returns only the
    /// handle this generates, never the token itself. Shared across every
    /// connection this daemon serves — `RegisterSocketServer` hands every
    /// accepted connection to the same `RegisterRequestHandler`, and
    /// `SessionTable` is its own thread-safe type for exactly that reason.
    public let sessions: SessionTable
    /// Resolves the connecting peer's admitted uid for `fileDescriptor`,
    /// throwing when it is not one of `admittedAccounts`'s keys. Defaults
    /// to the real `PeerUIDGate.check`; a test overrides this with a
    /// synthetic uid it could not otherwise produce without a real
    /// privilege change — the same idiom `KeyFileStoreTests` uses for an
    /// owner mismatch — so two admitted accounts can be exercised over one
    /// real (same-uid) test connection.
    private let peerUIDCheck: (Int32, Set<uid_t>) throws -> uid_t

    public init(
        admittedAccounts: [uid_t: String], transport: RegistrationTransport,
        authorizationTransport: AuthorizationTransport, sessions: SessionTable,
        peerUIDCheck: @escaping (Int32, Set<uid_t>) throws -> uid_t = PeerUIDGate.check
    ) {
        self.admittedAccounts = admittedAccounts
        self.transport = transport
        self.authorizationTransport = authorizationTransport
        self.sessions = sessions
        self.peerUIDCheck = peerUIDCheck
    }

    /// Checks the peer's uid first (`PeerUIDGate`) — before a single byte
    /// of the request is read — then parses and dispatches the one of the
    /// two requests this socket accepts, retrying `register` once on
    /// `session_machine_mismatch` (`RegistrationReattempt`), and writes one
    /// JSON response line back. Always closes `fileDescriptor` on return.
    public func handle(fileDescriptor: Int32) {
        defer { close(fileDescriptor) }
        SocketIO.setNoSIGPIPE(fileDescriptor: fileDescriptor)

        let account: String
        let peerUID: uid_t
        do {
            peerUID = try peerUIDCheck(fileDescriptor, Set(admittedAccounts.keys))
            account = admittedAccounts[peerUID]!
        } catch {
            SocketIO.writeJSONLine(fileDescriptor: fileDescriptor, object: ["ok": false, "refused": "peer_uid_not_admitted"])
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

        switch op {
        case "register":
            handleRegister(fileDescriptor: fileDescriptor, request: request, peerUID: peerUID, account: account)
        case "authorize":
            handleAuthorize(fileDescriptor: fileDescriptor, request: request, peerUID: peerUID)
        default:
            SocketIO.writeJSONLine(fileDescriptor: fileDescriptor, object: ["ok": false, "refused": "unknown_operation"])
        }
    }

    private func handleRegister(fileDescriptor: Int32, request: [String: Any], peerUID: uid_t, account: String) {
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
        let response = registerResponseBody(for: outcome, callerKind: callerKind, peerUID: peerUID, account: account)
        SocketIO.writeJSONLine(fileDescriptor: fileDescriptor, object: response)
    }

    /// Looks `handle` up in `sessions` for `peerUID` BEFORE any network call
    /// — an unknown handle or one registered under a different uid is
    /// refused right here, the same refusal `SessionTable.lookup` already
    /// names, and `authorizationTransport.authorize` is never called for
    /// either case (E12·2 slice S3). Only once the lookup succeeds does the
    /// looked-up `bosunSessionToken` go out to the Bosun — never into this
    /// function's own response, an error message, or anywhere else.
    private func handleAuthorize(fileDescriptor: Int32, request: [String: Any], peerUID: uid_t) {
        guard let handle = request["handle"] as? String,
              let toolName = request["tool_name"] as? String
        else {
            SocketIO.writeJSONLine(fileDescriptor: fileDescriptor, object: ["ok": false, "refused": "malformed_request"])
            return
        }
        let input = (request["input"] as? [String: Any]) ?? [:]

        switch sessions.lookup(handle: handle, peerUID: peerUID) {
        case .failure(.unknownHandle):
            SocketIO.writeJSONLine(fileDescriptor: fileDescriptor, object: ["ok": false, "refused": "unknown_handle"])
        case .failure(.uidMismatch):
            SocketIO.writeJSONLine(fileDescriptor: fileDescriptor, object: ["ok": false, "refused": "uid_mismatch"])
        case .success(let entry):
            let decision = authorizationTransport.authorize(
                sessionToken: entry.bosunSessionToken, toolName: toolName, input: input)
            SocketIO.writeJSONLine(fileDescriptor: fileDescriptor, object: authorizeResponseBody(for: decision))
        }
    }

    /// Builds the one JSON line `handleRegister` writes back. On
    /// `.registered`, stores the Bosun session token in `sessions` and
    /// replies with the handle `SessionTable.register` mints — never
    /// `sessionToken` itself, and nowhere in any other case does a value
    /// derived from it appear: every other outcome case already carried no
    /// token before this change and still does not.
    private func registerResponseBody(
        for outcome: RegistrationOutcome, callerKind: String, peerUID: uid_t, account: String
    ) -> [String: Any] {
        switch outcome {
        case .registered(let sessionID, let sessionToken):
            let handle = sessions.register(
                bosunSessionToken: sessionToken, callerKind: callerKind, registeringUID: peerUID,
                account: account)
            return ["ok": true, "session_id": sessionID, "handle": handle]
        case .refused(let reason, let detail):
            return ["ok": false, "refused": reason, "detail": detail]
        case .serverCertificateRefused(let reason):
            return ["ok": false, "refused": "bosun_server_certificate_refused", "detail": ["reason": reason.description]]
        case .transportError(let detail):
            return ["ok": false, "refused": "transport_error", "detail": ["detail": detail]]
        }
    }

    /// Builds the one JSON line `handleAuthorize` writes back for a decision
    /// that reached `authorizationTransport` (a lookup refusal never calls
    /// this). Carries `allow` and `reason` straight through from the Bosun
    /// — or from `AuthorizationDecision.bosunUnreachable` when the relay
    /// itself could not reach it — without ever losing the reason, plus the
    /// scoped authorization, when there is one, for the caller to inspect.
    private func authorizeResponseBody(for decision: AuthorizationDecision) -> [String: Any] {
        var body: [String: Any] = ["ok": true, "allow": decision.allow, "reason": decision.reason]
        switch decision.authorization {
        case .object(let objectRef):
            body["authorization"] = ["kind": "object", "object_ref": objectRef]
        case .boundary(let toolName):
            body["authorization"] = ["kind": "boundary", "tool_name": toolName]
        case nil:
            break
        }
        return body
    }
}
