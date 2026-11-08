/// The two byte bounds shared by both directions of the forwarding
/// socket's HTTP/1 framing: `ForwardingHTTPRequestReader`
/// (`IndividuatorDaemonCore`) enforces them against what the admitted
/// local process sends; `ForwardingUpstreamResponseReassembler` (this
/// target) enforces the same numbers against what the Keel destination
/// sends back. Defined once, here, because `IndividuatorDaemonCore`
/// already depends on `IndividuatorNetwork` and not the other way
/// around — a response-side cap could not reuse a request-side constant
/// defined on the daemon target without a dependency cycle.
public enum ForwardingSizeLimits {
    public static let maxHeadBytes = 64 * 1024
    public static let maxBodyBytes = 16 * 1024 * 1024
}
