import Testing
@testable import IndividuatorNetwork

/// A scripted `RegistrationTransport`: returns its outcomes in order,
/// recording how many times it was called, so a test asserts both the
/// returned outcome AND the number of attempts — the point of
/// `RegistrationReattempt` is retrying exactly once, not merely returning
/// a plausible-looking result.
private final class ScriptedTransport: RegistrationTransport {
    private var outcomes: [RegistrationOutcome]
    private(set) var callCount = 0

    init(outcomes: [RegistrationOutcome]) {
        self.outcomes = outcomes
    }

    func register(account: String, callerKind: String, ceiling: String, protocolVersion: String) -> RegistrationOutcome {
        callCount += 1
        guard !outcomes.isEmpty else {
            return .transportError(detail: "ScriptedTransport ran out of scripted outcomes")
        }
        return outcomes.removeFirst()
    }
}

@Suite struct RegistrationReattemptTests {
    @Test func returnsSuccessOnTheFirstTryWithoutRetrying() {
        let transport = ScriptedTransport(outcomes: [.registered(sessionID: "s1", sessionToken: "t1")])
        let outcome = RegistrationReattempt.register(
            transport: transport, account: "operator", callerKind: "operator", ceiling: "ceil", protocolVersion: "1")

        #expect(outcome == .registered(sessionID: "s1", sessionToken: "t1"))
        #expect(transport.callCount == 1)
    }

    @Test func retriesExactlyOnceOnSessionMachineMismatch() {
        let transport = ScriptedTransport(outcomes: [
            .refused(reason: "session_machine_mismatch", detail: [:]),
            .registered(sessionID: "s2", sessionToken: "t2"),
        ])
        let outcome = RegistrationReattempt.register(
            transport: transport, account: "operator", callerKind: "agent", ceiling: "ceil", protocolVersion: "1")

        #expect(outcome == .registered(sessionID: "s2", sessionToken: "t2"))
        #expect(transport.callCount == 2)
    }

    @Test func doesNotRetryATwiceRepeatedSessionMachineMismatch() {
        let transport = ScriptedTransport(outcomes: [
            .refused(reason: "session_machine_mismatch", detail: [:]),
            .refused(reason: "session_machine_mismatch", detail: [:]),
        ])
        let outcome = RegistrationReattempt.register(
            transport: transport, account: "operator", callerKind: "agent", ceiling: "ceil", protocolVersion: "1")

        #expect(outcome == .refused(reason: "session_machine_mismatch", detail: [:]))
        #expect(transport.callCount == 2)
    }

    @Test func doesNotRetryAnyOtherRefusalReason() {
        let transport = ScriptedTransport(outcomes: [.refused(reason: "machine_custody_not_hardware", detail: [:])])
        let outcome = RegistrationReattempt.register(
            transport: transport, account: "operator", callerKind: "agent", ceiling: "ceil", protocolVersion: "1")

        #expect(outcome == .refused(reason: "machine_custody_not_hardware", detail: [:]))
        #expect(transport.callCount == 1)
    }
}
