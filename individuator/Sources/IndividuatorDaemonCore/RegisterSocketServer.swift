import Foundation
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif

public enum RegisterSocketServerError: Error, CustomStringConvertible {
    case socketCreateFailed(errno: Int32)
    case pathTooLong(String)
    case bindFailed(errno: Int32)
    case listenFailed(errno: Int32)

    public var description: String {
        switch self {
        case .socketCreateFailed(let err): return "socket() failed (errno \(err))"
        case .pathTooLong(let path): return "socket path too long for sockaddr_un: \(path)"
        case .bindFailed(let err): return "bind() failed (errno \(err))"
        case .listenFailed(let err): return "listen() failed (errno \(err))"
        }
    }
}

/// The root daemon's Unix-socket accept loop — thin IO glue around
/// `RegisterRequestHandler`, which is where every actual decision (the peer
/// check, the one `register` operation) is made and tested. Serial, not
/// threaded: this is a low-volume local control socket, and handling one
/// connection at a time means a slow or stuck registration attempt cannot
/// starve a concurrent one into a race over the Bosun connection.
public final class RegisterSocketServer {
    private let socketPath: String
    private let handler: RegisterRequestHandler
    private let requiredDirectoryOwnerUID: uid_t
    private var listenFD: Int32 = -1

    /// `requiredDirectoryOwnerUID` defaults to root (0), matching the only
    /// account this daemon ever actually runs as; a test overrides it to
    /// its own uid, since nothing under `Tests/` may run as root.
    public init(socketPath: String, handler: RegisterRequestHandler, requiredDirectoryOwnerUID: uid_t = 0) {
        self.socketPath = socketPath
        self.handler = handler
        self.requiredDirectoryOwnerUID = requiredDirectoryOwnerUID
    }

    /// Binds and listens, then accepts connections forever, handing each
    /// one to `handler.handle` in turn. Never returns under normal
    /// operation.
    public func run() throws {
        try bindAndListen()
        while true {
            let clientFD = accept(listenFD, nil, nil)
            guard clientFD >= 0 else { continue }
            handler.handle(fileDescriptor: clientFD)
        }
    }

    private func bindAndListen() throws {
        // Prepared here, every time this daemon starts, rather than once
        // by the installer, since launchd's `KeepAlive` restarts this
        // process after a crash and the directory must be re-verified on
        // every one of those starts too. `DaemonSocketDirectory.prepare`
        // refuses to proceed unless every ancestor of the socket's
        // directory, and the directory itself, is a real directory (never
        // a symlink), owned by `requiredDirectoryOwnerUID`, and writable
        // by nobody but its owner — `defaultDaemonSocketPath` places that
        // directory under `/var/db` precisely so this is always true in
        // production, closing the TOCTOU a prior version of this check had
        // under the group-writable `/var/run` (review round 2: nothing
        // held the verified directory open between the check and `bind()`
        // re-resolving the same path by name, so a `gid=daemon` process
        // could `rmdir` and replace it in between). Only after that does
        // this clear a stale socket left by an unclean prior exit — never
        // blindly, since a stale path is exactly where a planted symlink
        // would try to redirect the removal.
        try DaemonSocketDirectory.prepare(
            directoryPath: (socketPath as NSString).deletingLastPathComponent,
            socketPath: socketPath,
            requiredOwnerUID: requiredDirectoryOwnerUID)

        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { throw RegisterSocketServerError.socketCreateFailed(errno: errno) }

        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let pathBytes = Array(socketPath.utf8)
        let capacity = MemoryLayout.size(ofValue: addr.sun_path)
        guard pathBytes.count < capacity else {
            close(fd)
            throw RegisterSocketServerError.pathTooLong(socketPath)
        }
        withUnsafeMutableBytes(of: &addr.sun_path) { raw in
            let buffer = raw.bindMemory(to: UInt8.self)
            for (i, byte) in pathBytes.enumerated() { buffer[i] = byte }
            buffer[pathBytes.count] = 0
        }

        let size = MemoryLayout<sockaddr_un>.size
        let bindResult = withUnsafePointer(to: &addr) { ptr -> Int32 in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, socklen_t(size)) }
        }
        guard bindResult == 0 else {
            let err = errno
            close(fd)
            throw RegisterSocketServerError.bindFailed(errno: err)
        }

        // Mode 0666, deliberately wide open. This file is owned by root
        // (whoever calls `bind()`), and the operator account that must be
        // able to `connect()` to it is never root — a tighter mode here
        // (0600 was tried and is wrong: it would let only root connect,
        // which means the operator's own `register` call would be
        // refused by the filesystem before it ever reached `accept()`).
        // The real access control is `PeerUIDGate`, applied after
        // `accept()` to the peer's REAL uid as the kernel reports it via
        // `getpeereid` — a value this file's mode cannot narrow any
        // further without also narrowing out the one uid that is
        // supposed to connect.
        chmod(socketPath, 0o666)

        guard listen(fd, 16) == 0 else {
            let err = errno
            close(fd)
            throw RegisterSocketServerError.listenFailed(errno: err)
        }
        listenFD = fd
    }
}
