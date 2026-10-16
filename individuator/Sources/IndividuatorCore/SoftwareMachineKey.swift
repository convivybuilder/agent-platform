import CryptoKit
import Foundation

/// A named reason a software machine key could not be created or loaded —
/// either `KeyFileStore`'s own custody refusal, or a stored blob that is
/// not a valid P-256 raw private key representation.
public enum SoftwareMachineKeyError: Error, CustomStringConvertible {
    case store(KeyFileStoreError)
    case malformedKeyBlob(path: String)

    public var description: String {
        switch self {
        case .store(let e):
            return e.description
        case .malformedKeyBlob(let path):
            return "key blob at \(path) is not a valid P-256 private key"
        }
    }
}

/// A `MachineKey` backed by an in-memory CryptoKit P-256 signing key,
/// persisted as its raw 32-byte scalar through `KeyFileStore`'s own
/// custody (a 0700 directory holding a 0600 file, both root-owned in
/// production) — the shipped replacement for the retired
/// `SecureEnclaveMachineKey` (2026-10-15): a launchd daemon runs outside
/// any user context and cannot use the Secure Enclave at all, which is
/// exactly where the Individuator's first registration attempt failed in
/// production. Root's own custody of this file is what now stands between
/// the key and an operator-level process (a dispatched run included),
/// same as the Enclave-wrapped blob's custody did before.
public struct SoftwareMachineKey: MachineKey {
    private let key: P256.Signing.PrivateKey

    public var publicKey: P256.Signing.PublicKey { key.publicKey }

    public func signature<D: DataProtocol>(for data: D) throws -> P256.Signing.ECDSASignature {
        try key.signature(for: data)
    }

    /// An in-memory key with no file-backed persistence — for a CSR, a
    /// certificate adoption's key-match check, or a TLS peer in a test
    /// that needs *a* valid P-256 signing key and does not care where its
    /// bytes live. The daemon's own key never comes from here: it always
    /// goes through `create`/`load`, which route every byte through
    /// `KeyFileStore`'s custody.
    public init(key: P256.Signing.PrivateKey = P256.Signing.PrivateKey()) {
        self.key = key
    }

    /// Generates a new key and persists it through `store.createExclusive`
    /// — `O_CREAT|O_EXCL` at 0600 with no window where the file is briefly
    /// readable at a wider mode before narrowing, unlike `KeyFileStore
    /// .write`. Refuses (`keyAlreadyExists`, surfaced through either
    /// `prepareForCreate` or `createExclusive` itself) rather than ever
    /// overwriting an existing key file.
    public static func create(store: KeyFileStore) throws -> SoftwareMachineKey {
        do {
            try store.prepareForCreate()
        } catch let e as KeyFileStoreError {
            throw SoftwareMachineKeyError.store(e)
        }
        let key = P256.Signing.PrivateKey()
        do {
            try store.createExclusive(key.rawRepresentation)
        } catch let e as KeyFileStoreError {
            throw SoftwareMachineKeyError.store(e)
        }
        return SoftwareMachineKey(key: key)
    }

    /// Loads an existing key's raw representation. Refuses, before ever
    /// parsing it, whenever the directory or file's mode or owner is
    /// wrong.
    public static func load(store: KeyFileStore) throws -> SoftwareMachineKey {
        let raw: Data
        do {
            raw = try store.read()
        } catch let e as KeyFileStoreError {
            throw SoftwareMachineKeyError.store(e)
        }
        do {
            let key = try P256.Signing.PrivateKey(rawRepresentation: raw)
            return SoftwareMachineKey(key: key)
        } catch {
            throw SoftwareMachineKeyError.malformedKeyBlob(path: store.fileURL.path)
        }
    }
}
