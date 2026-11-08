import Foundation
import IndividuatorCore
import NIOSSL

/// The TLS 1.3 client setup shared by every Bosun mTLS call this package
/// makes — registration's `POST /sessions` (`BosunRegistrationClient`) and
/// authorization's `POST /authorize` (`BosunAuthorizationClient`) alike
/// (E12·2 slice S3: "reusing the registration client's TLS setup"). A
/// second call site builds its own certificate chain, trust roots and
/// server-certificate verification from scratch only by copying this one,
/// so the two can never drift apart on what 0092 Handoff's "Individuator
/// server checks" actually require.
enum BosunMutualTLS {
    /// The pieces a caller's own `channelInitializer` still builds the
    /// `NIOSSLClientHandler` from — the handler itself is NOT built here,
    /// because NIO needs a fresh handler instance per connection, and every
    /// caller already has its own per-connection `channelInitializer`.
    struct Setup {
        let sslContext: NIOSSLContext
        let verify: NIOSSLCustomVerificationCallback
    }

    /// Builds `sslContext` (TLS 1.3 only, this machine's client certificate
    /// and `machineKey` as the signing identity, the Library's root as the
    /// one trusted anchor) and `verify` (the callback that runs in place of
    /// ordinary hostname verification).
    ///
    /// Hostname verification is replaced, never turned off outright (0092
    /// Handoff): `.noHostnameVerification` on `sslContext`'s own
    /// configuration disables only the SNI/CN-matching step BoringSSL would
    /// otherwise do; chain validation to the Library's root still runs, and
    /// `verify` runs IN ADDITION, doing the serverAuth/URI checks
    /// `BosunServerVerification` implements.
    ///
    /// `onServerCertificateRefusal` is called, on the connecting event
    /// loop, the moment `verify` refuses the Bosun's server certificate.
    /// Every caller stores it into a local `var` and later prefers it (via
    /// `BosunRegistrationClient.preferRealCause`) over whatever NIO raises
    /// next tearing down the still-failing handshake.
    static func makeSetup(
        config: IndividuatorRootConfig, machineKey: any MachineKey, clientCertificatePEM: String,
        onServerCertificateRefusal: @escaping (BosunServerRefusal) -> Void
    ) throws -> Setup {
        let rootPEM = try String(contentsOfFile: config.rootCAPath, encoding: .utf8)
        let rootCert = try NIOSSLCertificate(bytes: Array(try derFromPEM(rootPEM)), format: .der)

        let clientCertDER = try derFromPEM(clientCertificatePEM)
        let clientCert = try NIOSSLCertificate(bytes: Array(clientCertDER), format: .der)
        let privateKey = NIOSSLPrivateKey(customPrivateKey: MachineKeyNIOPrivateKey(key: machineKey))

        var tlsConfig = TLSConfiguration.makeClientConfiguration()
        tlsConfig.minimumTLSVersion = .tlsv13
        tlsConfig.maximumTLSVersion = .tlsv13
        tlsConfig.certificateChain = [.certificate(clientCert)]
        tlsConfig.privateKey = .privateKey(privateKey)
        tlsConfig.certificateVerification = .noHostnameVerification
        tlsConfig.trustRoots = .certificates([rootCert])

        let sslContext = try NIOSSLContext(configuration: tlsConfig)
        let bosunURIName = config.bosunURIName

        let verify: NIOSSLCustomVerificationCallback = { certs, promise in
            guard let leaf = certs.first else {
                promise.succeed(.failed)
                return
            }
            do {
                try BosunServerVerification.verify(
                    leafCertificateDER: Data(leaf.toDERBytes()), rootPEM: rootPEM,
                    expectedBosunURIName: bosunURIName)
                promise.succeed(.certificateVerified)
            } catch let refusal as BosunServerRefusal {
                onServerCertificateRefusal(refusal)
                promise.succeed(.failed)
            } catch {
                promise.succeed(.failed)
            }
        }
        return Setup(sslContext: sslContext, verify: verify)
    }
}
