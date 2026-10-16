import Foundation
import IndividuatorCore
import NIOCore
import NIOSSL

enum MachineKeyNIOPrivateKeyError: Error, CustomStringConvertible {
    case decryptionNotSupported
    case signingFailed(underlying: Error)

    var description: String {
        switch self {
        case .decryptionNotSupported:
            return "this is a P-256 signing key for TLS 1.3; RSA decryption is not supported"
        case .signingFailed(let underlying):
            return "machine key signing failed: \(underlying)"
        }
    }
}

/// Wraps a `MachineKey` (the Secure Enclave key in production; a software
/// key under `Tests/`) so swift-nio-ssl can use it to sign the TLS
/// handshake without the key's raw bytes ever leaving `MachineKey`'s own
/// `signature(for:)` — the private key material is never copied into this
/// type or into BoringSSL; only a signature over already-public handshake
/// transcript bytes crosses the boundary.
struct MachineKeyNIOPrivateKey: NIOSSLCustomPrivateKey, Hashable {
    let key: any MachineKey

    // TLS 1.3's one ECDSA signature algorithm for a P-256 key.
    var signatureAlgorithms: [SignatureAlgorithm] { [.ecdsaSecp256R1Sha256] }

    func sign(channel: Channel, algorithm: SignatureAlgorithm, data: ByteBuffer) -> EventLoopFuture<ByteBuffer> {
        let promise = channel.eventLoop.makePromise(of: ByteBuffer.self)
        do {
            // NIOSSL hands this callback the UNHASHED handshake transcript —
            // "the data... has not been hashed, and it is the
            // responsibility of the implementer to ensure it is hashed
            // before use" (NIOSSLCustomPrivateKey's own doc). `MachineKey
            // .signature(for:)` (CryptoKit's `P256.Signing.PrivateKey
            // .signature(for: some DataProtocol)`) hashes with SHA-256
            // itself before signing, which is exactly what
            // `ecdsaSecp256R1Sha256` requires.
            let signature = try key.signature(for: Data(data.readableBytesView))
            var out = channel.allocator.buffer(capacity: signature.derRepresentation.count)
            out.writeBytes(signature.derRepresentation)
            promise.succeed(out)
        } catch {
            promise.fail(MachineKeyNIOPrivateKeyError.signingFailed(underlying: error))
        }
        return promise.futureResult
    }

    func decrypt(channel: Channel, data: ByteBuffer) -> EventLoopFuture<ByteBuffer> {
        // RSA-only, and only for RSA key exchange before TLS 1.3 — never
        // called for a P-256 key restricted to TLS 1.3 (BosunRegistrationClient
        // pins both minimumTLSVersion and maximumTLSVersion to .tlsv13).
        channel.eventLoop.makeFailedFuture(MachineKeyNIOPrivateKeyError.decryptionNotSupported)
    }

    static func == (lhs: MachineKeyNIOPrivateKey, rhs: MachineKeyNIOPrivateKey) -> Bool {
        lhs.key.publicKey.x963Representation == rhs.key.publicKey.x963Representation
    }

    func hash(into hasher: inout Hasher) {
        hasher.combine(key.publicKey.x963Representation)
    }
}
