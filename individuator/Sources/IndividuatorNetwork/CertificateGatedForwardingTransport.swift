import Foundation
import IndividuatorCore

/// Wraps `ForwardingUpstreamClient` the same way
/// `CertificateGatedRegistrationTransport` wraps `BosunRegistrationClient`
/// — reading `certificateStore`'s file fresh on every `forward` call
/// rather than once at daemon startup, so adopting a certificate takes
/// effect on the very next request with no daemon restart, and answering
/// a transport error (never crashing, never forwarding) when there is no
/// certificate yet.
public struct CertificateGatedForwardingTransport: ForwardingUpstreamTransport {
    public static let certificateMissingDetail = "certificate_missing"

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

    public func forward(address: String, requestBytes: [UInt8]) -> ForwardingUpstreamOutcome {
        guard let certificatePEM = try? loadCertificatePEM() else {
            return .transportError(detail: Self.certificateMissingDetail)
        }
        let client = ForwardingUpstreamClient(
            config: config, machineKey: machineKey, clientCertificatePEM: certificatePEM,
            connectTimeoutSeconds: connectTimeoutSeconds, responseTimeoutSeconds: responseTimeoutSeconds)
        return client.forward(address: address, requestBytes: requestBytes)
    }

    private func loadCertificatePEM() throws -> String {
        try certificateStore.validateFile()
        return try String(contentsOf: certificateStore.fileURL, encoding: .utf8)
    }
}
