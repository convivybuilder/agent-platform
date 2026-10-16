import CryptoKit
import Foundation

/// A P-256 signing key able to produce the machine certificate's CSR
/// signature and the mTLS client handshake signature. `SoftwareMachineKey`
/// is the only implementation this target ships (2026-10-15, retiring
/// `SecureEnclaveMachineKey`: a launchd daemon runs outside any user
/// context, and the Secure Enclave is unusable there — unsupported, not a
/// configuration mistake a differently-built daemon could work around).
/// The CSR and adoption builders in this module need no awareness of
/// which concrete key they were handed.
public protocol MachineKey {
    var publicKey: P256.Signing.PublicKey { get }
    func signature<D: DataProtocol>(for data: D) throws -> P256.Signing.ECDSASignature
}
