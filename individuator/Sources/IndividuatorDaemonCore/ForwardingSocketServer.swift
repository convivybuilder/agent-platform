import Foundation
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif

public enum ForwardingSocketServerError: Error, CustomStringConvertible {
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

/// The root daemon's forwarding-socket accept loop — the same thin IO
/// glue `RegisterSocketServer` is for `register`/`authorize`, duplicated
/// rather than shared because the two sockets serve different protocols
/// (line-delimited JSON versus raw HTTP/1) over different handler types.
/// Every actual decision is `ForwardingConnectionHandler`'s, tested there.
///
/// Threaded, one `Thread` per accepted connection — NOT serial the way
/// `RegisterSocketServer` is (round 1 finding). `RegisterSocketServer`'s own
/// doc comment gives its reason: a single admitted account, so handling one
/// connection at a time cannot starve any OTHER account's request, only that
/// one account's own second concurrent call. The forwarding socket has no
/// such guarantee — decision 0099 lets more than one account share the same
/// Individuator — so serial handling here would let one admitted account
/// that connects and sends nothing, or sends slowly, stall every other
/// account's forwarding for as long as that connection stays open.
/// `ForwardingConnectionHandler.requestReadTimeoutSeconds` bounds how long
/// any one connection can take; this loop's job is only to make sure a
/// connection still waiting on that bound never keeps `accept()` from
/// handing the next one to its own thread.
public final class ForwardingSocketServer {
    private let socketPath: String
    private let handler: ForwardingConnectionHandler
    private let requiredDirectoryOwnerUID: uid_t
    private let prepareDirectory: (String, String, uid_t) throws -> Void
    private var listenFD: Int32 = -1

    public convenience init(socketPath: String, handler: ForwardingConnectionHandler, requiredDirectoryOwnerUID: uid_t = 0) {
        self.init(
            socketPath: socketPath, handler: handler, requiredDirectoryOwnerUID: requiredDirectoryOwnerUID,
            prepareDirectory: DaemonSocketDirectory.prepare)
    }

    /// Test-only seam (E12·2 slice S5b round 3): `DaemonSocketDirectory
    /// .prepare`'s own ancestor walk always starts from the real `/`,
    /// root-owned on every real system, so no non-root test process can
    /// ever satisfy it for any directory it builds — this is the actual
    /// cause `ForwardingSocketServerTests`' two tests hit ("could not
    /// connect to the forwarding socket"): `run()` threw
    /// `DaemonSocketDirectoryError.directoryOwnerWrong` for `/` on every
    /// attempt, silently, because the error was discarded rather than a
    /// path length ever being the problem. This lets that file substitute
    /// a check against its own synthetic directory instead — the same
    /// substitution `DaemonSocketDirectoryTests` already makes for
    /// `verifyRootOwnedChain` directly — while production keeps calling
    /// the real, unconditional `prepare` above.
    init(
        socketPath: String, handler: ForwardingConnectionHandler, requiredDirectoryOwnerUID: uid_t = 0,
        prepareDirectory: @escaping (String, String, uid_t) throws -> Void
    ) {
        self.socketPath = socketPath
        self.handler = handler
        self.requiredDirectoryOwnerUID = requiredDirectoryOwnerUID
        self.prepareDirectory = prepareDirectory
    }

    /// Binds and listens, then accepts connections forever, handing each
    /// one to `handler.handle` on its own thread. Never returns under
    /// normal operation. `accept()` itself is still called serially, one
    /// at a time — only `handler.handle`, which is where a slow or stuck
    /// peer's blocking reads live, runs off this loop, so accepting
    /// connection N+1 never waits on connection N finishing.
    public func run() throws {
        try bindAndListen()
        while true {
            let clientFD = accept(listenFD, nil, nil)
            guard clientFD >= 0 else { continue }
            SocketIO.setNoSIGPIPE(fileDescriptor: clientFD)
            let handler = self.handler
            Thread.detachNewThread { handler.handle(fileDescriptor: clientFD) }
        }
    }

    private func bindAndListen() throws {
        // Re-verified on every start, exactly as `RegisterSocketServer`
        // does for its own socket's directory — see that type's own doc
        // comment for why (launchd's `KeepAlive`, the TOCTOU this closes).
        try prepareDirectory(
            (socketPath as NSString).deletingLastPathComponent, socketPath, requiredDirectoryOwnerUID)

        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { throw ForwardingSocketServerError.socketCreateFailed(errno: errno) }

        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let pathBytes = Array(socketPath.utf8)
        let capacity = MemoryLayout.size(ofValue: addr.sun_path)
        guard pathBytes.count < capacity else {
            close(fd)
            throw ForwardingSocketServerError.pathTooLong(socketPath)
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
            throw ForwardingSocketServerError.bindFailed(errno: err)
        }

        // Mode 0666, the same reasoning as `RegisterSocketServer`'s own
        // socket file: this is owned by root, and the real access control
        // is `PeerUIDGate` against the admitted-accounts set at
        // `accept()` time, not this file's filesystem mode.
        chmod(socketPath, 0o666)

        guard listen(fd, 16) == 0 else {
            let err = errno
            close(fd)
            throw ForwardingSocketServerError.listenFailed(errno: err)
        }
        listenFD = fd
    }
}
