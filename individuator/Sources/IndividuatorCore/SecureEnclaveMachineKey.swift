import CryptoKit
import Foundation
import Security

/// The Secure Enclave access-control policy this package applies to every
/// machine key, as one named pair rather than a literal buried inside
/// `create` — 0092's Handoff flags the accessibility flag as an operator
/// guess that may change, and a later change should be a one-line edit
/// here.
///
/// `kSecAttrAccessibleWhenUnlockedThisDeviceOnly`: the key is usable only
/// while the machine is unlocked, and the enclave-wrapped blob is
/// meaningless on any other machine even if copied — it is bound to this
/// device's own Secure Enclave.
///
/// `.privateKeyUsage` alone, with no biometry/passcode flag: the key is
/// used unattended by a root daemon with nobody at the keyboard to approve
/// a Touch ID or passcode prompt, so requiring user presence would hang
/// every registration and every signed call.
public let machineKeyAccessControlProtection: CFString = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
public let machineKeyAccessControlFlags: SecAccessControlCreateFlags = [.privateKeyUsage]

public enum SecureEnclaveMachineKeyError: Error, CustomStringConvertible {
    case secureEnclaveUnavailable
    case notRoot(effectiveUID: uid_t)
    case accessControlCreationFailed(underlying: Error?)
    case enclaveOperationFailed(underlying: Error)
    case store(KeyFileStoreError)

    public var description: String {
        switch self {
        case .secureEnclaveUnavailable:
            return "this machine has no Secure Enclave"
        case .notRoot(let euid):
            return "refusing to create or load a machine key as effective uid \(euid); this must run as root"
        case .accessControlCreationFailed(let underlying):
            return "could not build the Secure Enclave access control policy: \(String(describing: underlying))"
        case .enclaveOperationFailed(let underlying):
            return "Secure Enclave operation failed: \(underlying)"
        case .store(let e):
            return e.description
        }
    }
}

/// A `MachineKey` backed by a non-exportable `SecureEnclave.P256.Signing`
/// key, stored on disk only as its enclave-wrapped `dataRepresentation`
/// blob — the bytes are useless off this device's Secure Enclave, which is
/// why the file-mode custody in `KeyFileStore` is still the only thing
/// standing between a readable file and a usable key, rather than the
/// bytes being a key in their own right.
///
/// `create` and `load` each refuse before ever calling into
/// `SecureEnclave`/`Security`: root-only and existing-key checks for
/// `create`, custody checks for `load`. That ordering is what makes the
/// filesystem-refusal tests in `KeyFileStoreTests` exercise the same checks
/// this type relies on without this type, or a real Secure Enclave key,
/// ever being invoked by a test.
public struct SecureEnclaveMachineKey: MachineKey {
    private let key: SecureEnclave.P256.Signing.PrivateKey

    public var publicKey: P256.Signing.PublicKey { key.publicKey }

    public func signature<D: DataProtocol>(for data: D) throws -> P256.Signing.ECDSASignature {
        try key.signature(for: data)
    }

    private init(key: SecureEnclave.P256.Signing.PrivateKey) {
        self.key = key
    }

    private static func requireRoot() throws {
        let euid = geteuid()
        guard euid == 0 else {
            throw SecureEnclaveMachineKeyError.notRoot(effectiveUID: euid)
        }
    }

    private static func buildAccessControl() throws -> SecAccessControl {
        var error: Unmanaged<CFError>?
        guard let ac = SecAccessControlCreateWithFlags(
            nil, machineKeyAccessControlProtection, machineKeyAccessControlFlags, &error
        ) else {
            throw SecureEnclaveMachineKeyError.accessControlCreationFailed(
                underlying: error?.takeRetainedValue())
        }
        return ac
    }

    /// Generates a new key in the Secure Enclave and persists its blob.
    /// Refuses, in order and before any Enclave or Security-framework call:
    /// not running as root; a key already present at `store`'s path; the
    /// directory existing with the wrong mode or owner. Only after all of
    /// that does it ask the Enclave for a key, and only then does it write
    /// the blob at 0600 in a 0700 directory.
    public static func create(store: KeyFileStore) throws -> SecureEnclaveMachineKey {
        try requireRoot()
        guard SecureEnclave.isAvailable else {
            throw SecureEnclaveMachineKeyError.secureEnclaveUnavailable
        }
        do {
            try store.prepareForCreate()
        } catch let e as KeyFileStoreError {
            throw SecureEnclaveMachineKeyError.store(e)
        }

        let accessControl = try buildAccessControl()
        let enclaveKey: SecureEnclave.P256.Signing.PrivateKey
        do {
            enclaveKey = try SecureEnclave.P256.Signing.PrivateKey(accessControl: accessControl)
        } catch {
            throw SecureEnclaveMachineKeyError.enclaveOperationFailed(underlying: error)
        }

        do {
            try store.write(enclaveKey.dataRepresentation)
        } catch let e as KeyFileStoreError {
            throw SecureEnclaveMachineKeyError.store(e)
        }
        return SecureEnclaveMachineKey(key: enclaveKey)
    }

    /// Loads an existing key's blob. Refuses, before ever asking the
    /// Enclave to reconstitute it, whenever the directory or file's mode or
    /// owner is wrong.
    public static func load(store: KeyFileStore) throws -> SecureEnclaveMachineKey {
        try requireRoot()
        let blob: Data
        do {
            blob = try store.read()
        } catch let e as KeyFileStoreError {
            throw SecureEnclaveMachineKeyError.store(e)
        }
        do {
            let enclaveKey = try SecureEnclave.P256.Signing.PrivateKey(dataRepresentation: blob)
            return SecureEnclaveMachineKey(key: enclaveKey)
        } catch {
            throw SecureEnclaveMachineKeyError.enclaveOperationFailed(underlying: error)
        }
    }
}
