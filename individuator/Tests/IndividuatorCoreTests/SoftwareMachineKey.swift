import CryptoKit
import Foundation
@testable import IndividuatorCore

/// An in-memory P-256 signing key standing in for `SecureEnclaveMachineKey`
/// in every test in this target. It lives under `Tests/` only: SwiftPM
/// never links a test target's sources into a product, so this type has
/// no path into the shipped `individuator` executable or the
/// `IndividuatorCore` library — see `Package.swift`'s header comment for
/// how the package's three targets are laid out to guarantee that.
struct SoftwareMachineKey: MachineKey {
    let key: P256.Signing.PrivateKey

    init(key: P256.Signing.PrivateKey = P256.Signing.PrivateKey()) {
        self.key = key
    }

    var publicKey: P256.Signing.PublicKey { key.publicKey }

    func signature<D: DataProtocol>(for data: D) throws -> P256.Signing.ECDSASignature {
        try key.signature(for: data)
    }
}
