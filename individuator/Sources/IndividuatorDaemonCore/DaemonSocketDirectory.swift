import Foundation
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif

/// A named reason the root daemon refused to prepare the directory that
/// holds its Unix socket, refused one of that directory's ancestors, or
/// refused to clear a stale socket file left by an unclean prior exit.
public enum DaemonSocketDirectoryError: Error, CustomStringConvertible, Equatable {
    case prepareFailed(path: String, errno: Int32)
    case statFailed(path: String, errno: Int32)
    case notAPlainDirectory(path: String)
    case directoryModeWrong(path: String, expected: UInt16, found: UInt16)
    case directoryOwnerWrong(path: String, expected: uid_t, found: uid_t)
    case ancestorWritableByOthers(path: String, mode: UInt16)
    case staleSocketNotASocket(path: String)
    case staleSocketOwnerWrong(path: String, expected: uid_t, found: uid_t)
    case staleSocketRemoveFailed(path: String, errno: Int32)

    public var description: String {
        switch self {
        case .prepareFailed(let p, let err):
            return "could not prepare socket directory \(p) (errno \(err))"
        case .statFailed(let p, let err):
            return "could not lstat \(p) (errno \(err))"
        case .notAPlainDirectory(let p):
            return "\(p) exists but is not a plain directory (a symlink or other non-directory entry)"
        case .directoryModeWrong(let p, let expected, let found):
            return "socket directory \(p) has mode \(String(found, radix: 8)), expected \(String(expected, radix: 8))"
        case .directoryOwnerWrong(let p, let expected, let found):
            return "directory \(p) is owned by uid \(found), expected \(expected)"
        case .ancestorWritableByOthers(let p, let mode):
            return "directory \(p) has mode \(String(mode, radix: 8)), which is writable by group or other; " +
                "the socket directory's whole ancestor chain must be writable only by its owner"
        case .staleSocketNotASocket(let p):
            return "\(p) exists but is not a socket; refusing to remove it"
        case .staleSocketOwnerWrong(let p, let expected, let found):
            return "stale socket \(p) is owned by uid \(found), expected \(expected); refusing to remove it"
        case .staleSocketRemoveFailed(let p, let err):
            return "could not remove stale socket \(p) (errno \(err))"
        }
    }
}

/// Prepares the directory that holds the root daemon's Unix socket
/// (`/var/db/individuator-daemon`), and clears a stale socket file left by
/// an unclean prior exit: launchd's `KeepAlive` restarts this daemon after
/// a crash, and the old socket inode is still there when it does, which
/// would otherwise fail `bind()` with `EADDRINUSE`.
///
/// This is the same custody style `KeyFileStore` applies to the
/// machine-key directory — create at an explicit mode, then refuse
/// anything that is not exactly that — but does not reuse `KeyFileStore`
/// itself: that type's checks call `stat`, which follows a symlink, and
/// this package once placed the socket under `/var/run`, which is
/// group-writable on macOS with no sticky bit, so another process could
/// plant a symlink there, or — the gap review round 2 found — `rmdir`/
/// recreate the directory itself between this prepare step and `bind()`,
/// since nothing held it open across the two. Every check here calls
/// `lstat`, so a symlink is refused regardless of what it points to, and
/// `prepare` also walks and re-verifies every ancestor of the socket
/// directory on every call, immediately before `bind()` resolves the same
/// path: under `/var/db`, every ancestor (`/`, `/private`, `/private/var`,
/// `/var/db`) is root-owned and writable by nobody else, so there is no
/// directory entry anywhere on the path a non-root process can rename,
/// remove or replace — the race the single-check version of this code had
/// under `/var/run` does not exist here, and the ancestor walk exists to
/// confirm that invariant actually holds on the machine the daemon is
/// running on rather than assume it silently.
public enum DaemonSocketDirectory {
    /// `0755`: root owns the directory, and nothing sensitive is readable
    /// from inside it (listing it reveals only the socket's name). It must
    /// let ANY local uid traverse it to reach the socket file, because the
    /// operator account is not known until the root config is read, long
    /// after this directory is prepared — the real access control is the
    /// peer-uid check at `accept()` time (`PeerUIDGate`), not the
    /// filesystem mode of either this directory or the socket file it
    /// holds.
    public static let mode: UInt16 = 0o755

    private struct LStatResult {
        let mode: UInt16
        let owner: uid_t
        let isDirectory: Bool
        let isSocket: Bool
    }

    private static func lstatResult(_ path: String) -> LStatResult? {
        var st = stat()
        guard lstat(path, &st) == 0 else { return nil }
        return LStatResult(
            mode: UInt16(st.st_mode & 0o777),
            owner: st.st_uid,
            isDirectory: (st.st_mode & S_IFMT) == S_IFDIR,
            isSocket: (st.st_mode & S_IFMT) == S_IFSOCK)
    }

    /// Verifies every ancestor of `path`, from `/` down to (but not
    /// including) `path` itself. Resolves `path`'s parent with `realpath`
    /// first, purely to follow macOS's own base-system symlinks (`/var` ->
    /// `/private/var`) to the real directories they point at — not to
    /// trust an arbitrary symlink, but because every component `realpath`
    /// walks to resolve those is itself one of the root-owned,
    /// owner-only-writable directories `verifyRootOwnedChain` goes on to
    /// verify, so nothing attacker-controlled can be substituted along the
    /// way — then hands the resulting list of absolute paths, from `/`
    /// down to that resolved parent, to `verifyRootOwnedChain`.
    static func verifyAncestorsAreRootOwned(ofDirectory path: String, requiredOwnerUID: uid_t) throws {
        let parent = (path as NSString).deletingLastPathComponent
        guard let resolvedRaw = realpath(parent, nil) else {
            throw DaemonSocketDirectoryError.statFailed(path: parent, errno: errno)
        }
        defer { free(resolvedRaw) }
        let resolved = String(cString: resolvedRaw)

        var chain = ["/"]
        var current = "/"
        for component in resolved.split(separator: "/") {
            current = current == "/" ? "/\(component)" : "\(current)/\(component)"
            chain.append(current)
        }
        try verifyRootOwnedChain(chain, requiredOwnerUID: requiredOwnerUID)
    }

    /// Verifies that each of `chain` is a real directory (`lstat`, so a
    /// symlink is refused regardless of its target), owned by
    /// `requiredOwnerUID`, and writable by nobody but its owner — the
    /// per-component check that makes the directory-rename race review
    /// round 2 found impossible rather than merely narrower: an attacker
    /// who cannot write to any directory in `chain` has no way to detach
    /// the verified socket directory from the path and put another one in
    /// its place, at any point before or after this check runs. Separated
    /// from `verifyAncestorsAreRootOwned` so a test can exercise it
    /// against a chain built entirely under a temporary directory, rather
    /// than against the real filesystem root this process does not own.
    static func verifyRootOwnedChain(_ chain: [String], requiredOwnerUID: uid_t) throws {
        for path in chain {
            guard let st = lstatResult(path) else {
                throw DaemonSocketDirectoryError.statFailed(path: path, errno: errno)
            }
            guard st.isDirectory else {
                throw DaemonSocketDirectoryError.notAPlainDirectory(path: path)
            }
            guard st.owner == requiredOwnerUID else {
                throw DaemonSocketDirectoryError.directoryOwnerWrong(path: path, expected: requiredOwnerUID, found: st.owner)
            }
            guard st.mode & 0o022 == 0 else {
                throw DaemonSocketDirectoryError.ancestorWritableByOthers(path: path, mode: st.mode)
            }
        }
    }

    /// Verifies every ancestor of `directoryPath` is root-owned and
    /// writable by nobody else, creates `directoryPath` itself at `mode`
    /// when nothing is there yet, refuses to start unless what is there
    /// (new or pre-existing) is a real directory at exactly `mode` owned
    /// by `requiredOwnerUID`, then removes a stale socket file at
    /// `socketPath` when — and only when — it is safe to: a socket,
    /// inside the now-verified directory, owned by `requiredOwnerUID`.
    public static func prepare(directoryPath: String, socketPath: String, requiredOwnerUID: uid_t) throws {
        try verifyAncestorsAreRootOwned(ofDirectory: directoryPath, requiredOwnerUID: requiredOwnerUID)
        try prepareDirectory(path: directoryPath, requiredOwnerUID: requiredOwnerUID)
        try removeStaleSocketIfSafe(socketPath: socketPath, requiredOwnerUID: requiredOwnerUID)
    }

    static func prepareDirectory(path: String, requiredOwnerUID: uid_t) throws {
        if lstatResult(path) == nil {
            guard mkdir(path, mode_t(mode)) == 0 else {
                throw DaemonSocketDirectoryError.prepareFailed(path: path, errno: errno)
            }
            // `mkdir` applies the process umask to the mode it is given;
            // force the exact bits rather than trusting whatever umask
            // launchd happens to start this daemon under.
            guard chmod(path, mode_t(mode)) == 0 else {
                throw DaemonSocketDirectoryError.prepareFailed(path: path, errno: errno)
            }
        }

        guard let st = lstatResult(path) else {
            throw DaemonSocketDirectoryError.statFailed(path: path, errno: errno)
        }
        guard st.isDirectory else {
            throw DaemonSocketDirectoryError.notAPlainDirectory(path: path)
        }
        guard st.mode == mode else {
            throw DaemonSocketDirectoryError.directoryModeWrong(path: path, expected: mode, found: st.mode)
        }
        guard st.owner == requiredOwnerUID else {
            throw DaemonSocketDirectoryError.directoryOwnerWrong(path: path, expected: requiredOwnerUID, found: st.owner)
        }
    }

    static func removeStaleSocketIfSafe(socketPath: String, requiredOwnerUID: uid_t) throws {
        guard let st = lstatResult(socketPath) else { return }   // nothing there: nothing stale to clear
        guard st.isSocket else {
            throw DaemonSocketDirectoryError.staleSocketNotASocket(path: socketPath)
        }
        guard st.owner == requiredOwnerUID else {
            throw DaemonSocketDirectoryError.staleSocketOwnerWrong(path: socketPath, expected: requiredOwnerUID, found: st.owner)
        }
        guard unlink(socketPath) == 0 else {
            throw DaemonSocketDirectoryError.staleSocketRemoveFailed(path: socketPath, errno: errno)
        }
    }
}
