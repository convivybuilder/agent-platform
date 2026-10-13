import CryptoKit
import Foundation

/// A P-256 signing key able to produce the machine certificate's CSR
/// signature and, later, the mTLS client handshake signature (slice 3).
/// `SecureEnclaveMachineKey` is the only implementation this target ships;
/// a software implementation exists only under `Tests/` (see that target's
/// `SoftwareMachineKey`), never in a product target, per the CSR and
/// adoption builders in this module needing no awareness of which concrete
/// key they were handed.
public protocol MachineKey {
    var publicKey: P256.Signing.PublicKey { get }
    func signature<D: DataProtocol>(for data: D) throws -> P256.Signing.ECDSASignature
}
