import CryptoKit
import Foundation
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif

/// Why a `lookup` is refused. Deliberately two cases rather than one, so a
/// caller (and `RegisterRequestHandler`'s own response) can tell "this
/// handle was never registered, or has expired since a daemon restart"
/// apart from "this handle exists, but not for you" — without the second
/// case ever naming the uid or account that actually holds it; see
/// `SessionTable.lookup`.
public enum SessionLookupFailure: Error, Equatable {
    case unknownHandle
    case uidMismatch
}

/// What a handle resolves to: the Bosun's own session token plus the facts
/// `RegisterRequestHandler` recorded about the connection that registered
/// it. Never `Codable`, `CustomStringConvertible`, or reflectable in any way
/// that would let `String(describing:)`, string interpolation, or a
/// `print`/log call reach `bosunSessionToken` by accident — there is no
/// `description` to fall back to a default (Swift's mirror-based) rendering
/// of this struct's stored properties, which WOULD include the token.
public struct SessionEntry: Equatable {
    public let bosunSessionToken: String
    public let callerKind: String
    public let registeringUID: uid_t
    public let account: String

    public init(bosunSessionToken: String, callerKind: String, registeringUID: uid_t, account: String) {
        self.bosunSessionToken = bosunSessionToken
        self.callerKind = callerKind
        self.registeringUID = registeringUID
        self.account = account
    }
}

/// The root daemon's in-memory map from an opaque handle to the Bosun
/// session a `register` call won (E12·2 slice S2). `register` on
/// `RegisterRequestHandler` stores an entry here and returns only the
/// handle; the Bosun's own `session_token` never leaves this table.
///
/// The table lives in memory alone — never written to disk, never logged.
/// `entries` is `private`, `SessionEntry` carries no `CustomStringConvertible`
/// / `Codable` conformance that could render `bosunSessionToken`, and this
/// file contains no `print`, no `description`, and no interpolation of an
/// entry or its token; `lookup`'s failure path returns only
/// `SessionLookupFailure`, never the entry it almost matched. A daemon
/// restart starts a fresh, empty table, so every registered caller must
/// register again — this matches the strict ruling that a credential this
/// process cannot name a holder for must not survive the process holding it.
///
/// What the handle does not protect in stage one. Every dispatched run and
/// the operator's own session run as the same uid, so binding a handle to
/// the registering uid cannot tell one run's holder from another's. A
/// process under an admitted uid can also open the register socket itself.
/// Until E12·6 identifies a caller by its code signature, the handle's
/// confidentiality is the only thing keeping one run from acting under
/// another session's ceiling, so the handle must never be written anywhere
/// a run can read (see S4a). State this limitation in the code's comments
/// and the runbook in these words, not as isolation.
///
/// There is no runbook text in this repository to carry the limitation
/// above into — checked: this repository's own `docs/runbooks/` holds one
/// file (`platform-push-setup.md`, unrelated), and no other runbook
/// directory exists in this repository naming the Individuator or its
/// sessions. A runbook for this would live in the central knowledge base,
/// outside this repository, and is this slice's build plan's concern, not
/// this file's.
public final class SessionTable {
    private var entries: [String: SessionEntry] = [:]
    private let lock = NSLock()

    public init() {}

    /// Mints a fresh handle, stores `entry` under it, and returns the
    /// handle. Each call's handle comes from a fresh 32-byte (256-bit) draw
    /// of `CryptoKit`'s own CSPRNG-backed `SymmetricKey` — the same
    /// randomness source this package already trusts for key material
    /// elsewhere — so two calls collide in practice only as often as two
    /// independent 256-bit draws do.
    @discardableResult
    public func register(
        bosunSessionToken: String, callerKind: String, registeringUID: uid_t, account: String
    ) -> String {
        let handleBytes = Self.generateHandleBytes()
        let handle = Data(handleBytes).base64EncodedString()
        let entry = SessionEntry(
            bosunSessionToken: bosunSessionToken, callerKind: callerKind,
            registeringUID: registeringUID, account: account)
        lock.lock()
        entries[handle] = entry
        lock.unlock()
        return handle
    }

    /// Resolves `handle` for the connection whose real (kernel-reported) uid
    /// is `peerUID`. Succeeds only when `handle` was registered AND its
    /// registering uid equals `peerUID`; a handle registered under a
    /// different uid refuses with `.uidMismatch`, never `.unknownHandle` —
    /// but the failure value names only that mismatch, never the uid or
    /// account that actually holds the handle, so a caller this refuses
    /// learns nothing about who does.
    ///
    /// The byte-for-byte comparison against each stored handle runs in
    /// constant time (`constantTimeEqual`, below) over every entry in the
    /// table rather than stopping at the first match or relying on
    /// `Dictionary`'s own hash-bucket equality check, so how long this call
    /// takes depends on how many sessions are registered, never on how many
    /// leading bytes of a guessed handle happen to match a real one.
    public func lookup(handle: String, peerUID: uid_t) -> Result<SessionEntry, SessionLookupFailure> {
        guard let presentedData = Data(base64Encoded: handle) else {
            return .failure(.unknownHandle)
        }
        let presented = [UInt8](presentedData)

        lock.lock()
        let snapshot = entries
        lock.unlock()

        var matched: SessionEntry?
        for (storedHandle, storedEntry) in snapshot {
            guard let storedData = Data(base64Encoded: storedHandle) else { continue }
            if Self.constantTimeEqual(presented, [UInt8](storedData)) {
                matched = storedEntry
            }
        }

        guard let entry = matched else {
            return .failure(.unknownHandle)
        }
        guard entry.registeringUID == peerUID else {
            return .failure(.uidMismatch)
        }
        return .success(entry)
    }

    /// `true` only when `a` and `b` are the same length and every byte
    /// matches; always walks every byte of the longer comparison it makes
    /// (it never returns as soon as a mismatching byte is found), so its
    /// running time for two handles of the same length depends only on that
    /// length, never on where the first differing byte falls.
    private static func constantTimeEqual(_ a: [UInt8], _ b: [UInt8]) -> Bool {
        guard a.count == b.count else { return false }
        var difference: UInt8 = 0
        for i in 0..<a.count {
            difference |= a[i] ^ b[i]
        }
        return difference == 0
    }

    private static func generateHandleBytes() -> [UInt8] {
        let key = SymmetricKey(size: .bits256)
        return key.withUnsafeBytes { Array($0) }
    }
}
