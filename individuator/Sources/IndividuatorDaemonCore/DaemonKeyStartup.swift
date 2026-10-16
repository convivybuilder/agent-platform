import CryptoKit
import Foundation
import IndividuatorCore
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif

/// A named reason the daemon could not prepare its own machine key at
/// startup.
public enum DaemonKeyStartupError: Error, CustomStringConvertible {
    case create(SoftwareMachineKeyError)
    case load(SoftwareMachineKeyError)
    case selfTestSigningFailed(underlying: Error)
    case selfTestVerifyFailed(step: String)
    case selfTestReloadFailed(SoftwareMachineKeyError)

    public var description: String {
        switch self {
        case .create(let e):
            return "could not create the machine key: \(e)"
        case .load(let e):
            return "could not load the machine key: \(e)"
        case .selfTestSigningFailed(let underlying):
            return "machine key self-test refused: could not sign: \(underlying)"
        case .selfTestVerifyFailed(let step):
            return "machine key self-test refused: the \(step) did not verify"
        case .selfTestReloadFailed(let e):
            return "machine key self-test refused: could not reload the key from its blob: \(e)"
        }
    }
}

public struct DaemonKeyStartupResult {
    public let key: SoftwareMachineKey
    /// One line naming the outcome, the effective uid and the public key's
    /// SHA-256 fingerprint — never any private key bytes. This is what the
    /// daemon's own entry point prints, and the only thing
    /// `bin/install-individuator` reads afterward to get the fingerprint
    /// for the Library's signing ceremony: the whole point of running the
    /// self-test inside the daemon's own process, rather than trusting a
    /// separate CLI run to have proven the same thing, is that this line
    /// can only exist once that self-test already passed in the daemon's
    /// own launchd context.
    public let logLine: String
}

/// Prepares the daemon's own software machine key on every start, factored
/// out of `individuator-daemon`'s entry point so it is testable without
/// launchd, root, or a real key file anywhere outside a test's own temp
/// directory (2026-10-15 Handoff "Individuator server checks" update: the
/// Secure Enclave is unusable from a launchd daemon at all, which is
/// exactly where this failed in production on 2026-10-15 — see the work
/// item's own account of the incident).
public enum DaemonKeyStartup {
    private static let selfTestMessage = Data("individuator-daemon self-test".utf8)

    /// Loads the key if `store`'s file already exists, or creates it if it
    /// does not — on EVERY start, not only the first, since a reused key
    /// must not be regenerated — then runs the same sign/verify/reload/
    /// sign/verify self-test `individuator self-test` has always run, now
    /// inside this process. Throws before ever returning a key whose
    /// self-test failed.
    public static func prepareKey(store: KeyFileStore, effectiveUID: uid_t) throws -> DaemonKeyStartupResult {
        let key: SoftwareMachineKey
        let created: Bool
        if FileManager.default.fileExists(atPath: store.fileURL.path) {
            created = false
            do {
                key = try SoftwareMachineKey.load(store: store)
            } catch let e as SoftwareMachineKeyError {
                throw DaemonKeyStartupError.load(e)
            }
        } else {
            created = true
            do {
                key = try SoftwareMachineKey.create(store: store)
            } catch let e as SoftwareMachineKeyError {
                throw DaemonKeyStartupError.create(e)
            }
        }

        try selfTest(key: key, store: store)

        let fingerprint = CertificateSigningRequest.publicKeyFingerprintHex(key.publicKey)
        let logLine = "individuator-daemon: machine key self-test passed " +
            "(\(created ? "created" : "loaded")), effective uid \(effectiveUID), " +
            "public key fingerprint \(fingerprint)"
        return DaemonKeyStartupResult(key: key, logLine: logLine)
    }

    /// Sign, verify, reload from the file, sign, verify again — the same
    /// self-test the CLI's own `self-test` subcommand runs, closing over
    /// whatever bug class a signature that only ever used the in-memory
    /// key from `create` could hide (a blob that failed to persist
    /// correctly, or one `P256.Signing.PrivateKey(rawRepresentation:)`
    /// cannot reconstitute).
    static func selfTest(key: SoftwareMachineKey, store: KeyFileStore) throws {
        try signAndVerify(key, step: "first signature")

        let reloaded: SoftwareMachineKey
        do {
            reloaded = try SoftwareMachineKey.load(store: store)
        } catch let e as SoftwareMachineKeyError {
            throw DaemonKeyStartupError.selfTestReloadFailed(e)
        }
        try signAndVerify(reloaded, step: "reloaded signature")
    }

    private static func signAndVerify(_ key: SoftwareMachineKey, step: String) throws {
        let signature: P256.Signing.ECDSASignature
        do {
            signature = try key.signature(for: selfTestMessage)
        } catch {
            throw DaemonKeyStartupError.selfTestSigningFailed(underlying: error)
        }
        guard key.publicKey.isValidSignature(signature, for: selfTestMessage) else {
            throw DaemonKeyStartupError.selfTestVerifyFailed(step: step)
        }
    }
}
