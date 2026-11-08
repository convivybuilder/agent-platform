import Testing

/// Groups every suite in this target whose tests make a real, synchronous
/// blocking call — a subprocess wait, an NIO `.wait()`, a raw `connect()`/
/// `read()` — directly on a test body, under one `.serialized` parent.
///
/// Round 3's own occurrence (PR 1121, job 125672806699): each suite below
/// already carried its own `.serialized`, which bounds how many of ITS OWN
/// tests can block a cooperative-pool thread at once (one), but does
/// nothing to bound how many SUCH SUITES run at the same time — Swift
/// Testing schedules different suites in parallel by default, so round 2's
/// fix (adding `.serialized` to this PR's own two new suites) left six
/// suites that could still each hold a pool thread blocked at once:
/// `BosunRegistrationClientIntegrationTests` and
/// `BosunAuthorizationIntegrationTests` (pre-existing), plus this PR's
/// `ForwardingUpstreamClientIntegrationTests`, `ForwardingSocketServerTests`,
/// `ForwardingConnectionHandlerTests`, and
/// `CertificateGatedRegistrationTransportTests`'
/// `readsAnAdoptedCertificateOnTheNextRequestWithNoRestart` (which reaches
/// a real, unlisted address through `CertificateGatedRegistrationTransport`).
/// On the hosted runner's few cores, six could exceed the pool's own thread
/// count, and once every pool thread is blocked, nothing scheduled on it
/// can run — including the `.timeLimit` cancellation that needs a free pool
/// thread to fire, which is why the job hung past every suite's own minutes-
/// scale limit rather than failing under one.
///
/// Nesting each of those six suites here, rather than leaving them as
/// top-level suites, is what serializes them AGAINST EACH OTHER: Swift
/// Testing runs a `.serialized` suite's children — nested suites included —
/// one at a time, so at most ONE of this parent's six children is ever
/// running, and so at most ONE pool thread is ever blocked by any of them,
/// regardless of how many more of this shape the target grows to hold. Each
/// child keeps its own `.serialized` and `.timeLimit` traits too, so both
/// read correctly on their own and stay correct if a child is ever split
/// back out to run alone.
@Suite(.serialized)
struct PoolBlockingIntegrationSuites {}
