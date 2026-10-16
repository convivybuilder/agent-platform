import Foundation
import IndividuatorCore

/// Wraps `BosunRegistrationClient` so the daemon can start and serve its
/// socket before an adopted certificate exists (2026-10-15: the
/// certificate request itself needs the key the daemon now creates on
/// first start, so the daemon can no longer refuse to start without a
/// certificate already in place). Reads `certificateStore`'s file fresh on
/// every `register` call rather than once at daemon startup, so adopting a
/// certificate takes effect on the very next request with no daemon
/// restart; refuses with `certificateMissingReason` before ever touching
/// the network when there is no certificate yet, or when its own custody
/// checks fail.
public struct CertificateGatedRegistrationTransport: RegistrationTransport {
    /// The refusal reason a `register` request gets when no certificate has
    /// been adopted yet (or its custody checks fail) — the same response
    /// shape `RegisterRequestHandler.responseBody` already gives every
    /// other named refusal.
    public static let certificateMissingReason = "certificate_missing"

    public let config: IndividuatorRootConfig
    public let machineKey: any MachineKey
    public let certificateStore: KeyFileStore
    public let connectTimeoutSeconds: Int64
    public let responseTimeoutSeconds: Int64

    public init(
        config: IndividuatorRootConfig, machineKey: any MachineKey, certificateStore: KeyFileStore,
        connectTimeoutSeconds: Int64 = 10, responseTimeoutSeconds: Int64 = 30
    ) {
        self.config = config
        self.machineKey = machineKey
        self.certificateStore = certificateStore
        self.connectTimeoutSeconds = connectTimeoutSeconds
        self.responseTimeoutSeconds = responseTimeoutSeconds
    }

    public func register(
        account: String, callerKind: String, ceiling: String, protocolVersion: String
    ) -> RegistrationOutcome {
        guard let certificatePEM = try? loadCertificatePEM() else {
            return .refused(reason: Self.certificateMissingReason, detail: [:])
        }
        let client = BosunRegistrationClient(
            config: config, machineKey: machineKey, clientCertificatePEM: certificatePEM,
            connectTimeoutSeconds: connectTimeoutSeconds, responseTimeoutSeconds: responseTimeoutSeconds)
        return client.register(
            account: account, callerKind: callerKind, ceiling: ceiling, protocolVersion: protocolVersion)
    }

    private func loadCertificatePEM() throws -> String {
        try certificateStore.validateFile()
        return try String(contentsOf: certificateStore.fileURL, encoding: .utf8)
    }
}
