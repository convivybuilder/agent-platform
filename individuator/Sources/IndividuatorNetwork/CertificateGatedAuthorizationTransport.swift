import Foundation
import IndividuatorCore

/// Wraps `BosunAuthorizationClient` so an `authorize` call refuses, without
/// ever touching the network, when no certificate has been adopted yet —
/// the same reason `CertificateGatedRegistrationTransport` exists for
/// `register` (2026-10-15: the daemon serves its socket before a
/// certificate exists). Reads `certificateStore`'s file fresh on every
/// `authorize` call, so adopting a certificate takes effect on the very
/// next request with no daemon restart.
///
/// A missing or custody-failing certificate here is still a DENY, never a
/// distinct "refused" shape the caller could mistake for something softer:
/// `RegisterRequestHandler.authorizeResponseBody` maps every non-decided
/// outcome, including this one, the same way `.bosunUnreachable` is
/// mapped — see that function's own doc comment.
public struct CertificateGatedAuthorizationTransport: AuthorizationTransport {
    public let config: IndividuatorRootConfig
    public let machineKey: any MachineKey
    public let certificateStore: KeyFileStore
    public let connectTimeoutSeconds: Int64
    public let responseTimeoutSeconds: Int64

    /// Builds the inner transport `authorize` relays to, given the
    /// certificate PEM just read from `certificateStore` — the real
    /// `BosunAuthorizationClient` by default, a scripted fake in this
    /// type's own tests. The seam exists because, unlike
    /// `CertificateGatedRegistrationTransport`'s `.transportError` vs.
    /// `.refused(reason: certificateMissingReason)`, a missing certificate
    /// and a real network failure both answer `.bosunUnreachable` here, so
    /// a test cannot tell "never touched the network" apart from "touched
    /// it and failed" by the returned decision alone.
    let makeClient: (String) -> AuthorizationTransport

    public init(
        config: IndividuatorRootConfig, machineKey: any MachineKey, certificateStore: KeyFileStore,
        connectTimeoutSeconds: Int64 = 10, responseTimeoutSeconds: Int64 = 30
    ) {
        self.config = config
        self.machineKey = machineKey
        self.certificateStore = certificateStore
        self.connectTimeoutSeconds = connectTimeoutSeconds
        self.responseTimeoutSeconds = responseTimeoutSeconds
        self.makeClient = { certificatePEM in
            BosunAuthorizationClient(
                config: config, machineKey: machineKey, clientCertificatePEM: certificatePEM,
                connectTimeoutSeconds: connectTimeoutSeconds, responseTimeoutSeconds: responseTimeoutSeconds)
        }
    }

    init(
        config: IndividuatorRootConfig, machineKey: any MachineKey, certificateStore: KeyFileStore,
        connectTimeoutSeconds: Int64 = 10, responseTimeoutSeconds: Int64 = 30,
        makeClient: @escaping (String) -> AuthorizationTransport
    ) {
        self.config = config
        self.machineKey = machineKey
        self.certificateStore = certificateStore
        self.connectTimeoutSeconds = connectTimeoutSeconds
        self.responseTimeoutSeconds = responseTimeoutSeconds
        self.makeClient = makeClient
    }

    public func authorize(sessionToken: String, toolName: String, input: [String: Any]) -> AuthorizationDecision {
        guard let certificatePEM = try? loadCertificatePEM() else {
            return .bosunUnreachable
        }
        return makeClient(certificatePEM).authorize(sessionToken: sessionToken, toolName: toolName, input: input)
    }

    private func loadCertificatePEM() throws -> String {
        try certificateStore.validateFile()
        return try String(contentsOf: certificateStore.fileURL, encoding: .utf8)
    }
}
