/// 0092 Handoff, "Individuator re-registration": on `session_machine_mismatch`
/// after a local certificate renewal, register again with the same ceiling
/// and caller kind. A `session_machine_mismatch` refusal to a *registration*
/// attempt (as opposed to the `/authorize` endpoint's own use of the same
/// reason) means this machine's certificate changed since the Bosun last
/// saw it bind a session — exactly the renewal case the Handoff names —
/// so the right response is simply to try again once with the identical
/// request, now riding the connection's current (renewed) certificate.
///
/// A pure function over `RegistrationTransport` rather than a method on
/// `BosunRegistrationClient` itself, so the retry policy is tested against a
/// scripted fake transport (`RegistrationOutcomeScript` in this target's
/// tests) without opening a real socket.
public enum RegistrationReattempt {
    /// Retries at most once: a second `session_machine_mismatch` is
    /// returned as-is rather than retried again, so a Bosun that keeps
    /// refusing this reason can never make this loop indefinitely.
    public static func register(
        transport: RegistrationTransport, account: String, callerKind: String, ceiling: String,
        protocolVersion: String
    ) -> RegistrationOutcome {
        let first = transport.register(
            account: account, callerKind: callerKind, ceiling: ceiling, protocolVersion: protocolVersion)
        guard case .refused(let reason, _) = first, reason == "session_machine_mismatch" else {
            return first
        }
        return transport.register(
            account: account, callerKind: callerKind, ceiling: ceiling, protocolVersion: protocolVersion)
    }
}
