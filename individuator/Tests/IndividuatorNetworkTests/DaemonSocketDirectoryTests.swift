import Foundation
import Testing
@testable import IndividuatorDaemonCore
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif

/// Binds a real `AF_UNIX` socket at `path` and leaves the resulting
/// special file behind — the same shape a stale socket from an unclean
/// prior daemon exit leaves, without needing a second process to create
/// it. Returns whether `bind()` succeeded, rather than asserting it did:
/// some sandboxes (this one, sometimes — see `canBindAFUnixSocket` below)
/// refuse every `bind()`, local TCP ports included, with `EPERM`.
private func bindRealSocket(at path: String) -> Bool {
    let fd = socket(AF_UNIX, SOCK_STREAM, 0)
    precondition(fd >= 0, "socket() failed")
    defer { close(fd) }
    var addr = sockaddr_un()
    addr.sun_family = sa_family_t(AF_UNIX)
    let pathBytes = Array(path.utf8)
    withUnsafeMutableBytes(of: &addr.sun_path) { raw in
        let buffer = raw.bindMemory(to: UInt8.self)
        for (i, byte) in pathBytes.enumerated() { buffer[i] = byte }
        buffer[pathBytes.count] = 0
    }
    let size = MemoryLayout<sockaddr_un>.size
    let result = withUnsafePointer(to: &addr) { ptr -> Int32 in
        ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, socklen_t(size)) }
    }
    return result == 0
}

/// The positive control for the three tests below that need a REAL
/// `AF_UNIX` socket special file: binds one at a throwaway path and
/// removes it again. If this itself fails, the sandbox refuses `bind()`
/// outright (same idiom as `BosunRegistrationClientIntegrationTests.
/// pythonCanBindLoopback`), and those tests return early rather than
/// failing on a limitation that is the sandbox's, not
/// `DaemonSocketDirectory`'s.
private let canBindAFUnixSocket: Bool = {
    let path = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString).path
    let ok = bindRealSocket(at: path)
    if ok { unlink(path) }
    return ok
}()

/// Set by CI's `swift` job on its test step, the same variable
/// `BosunRegistrationClientIntegrationTests` already turns its own
/// bind-refusal skip into a failure with: turns the two tests below's
/// `canBindAFUnixSocket` skip into a recorded failure too, so a future CI
/// environment that refuses `AF_UNIX` bind cannot silently lose their
/// coverage of `removeStaleSocketIfSafe` while the job stays green.
/// Unset locally, where the skip is correct.
private let requireBoundSocketTests: Bool =
    ProcessInfo.processInfo.environment["INDIVIDUATOR_REQUIRE_MTLS_INTEGRATION_TEST"] != nil

/// `true` when a real bound `AF_UNIX` socket is available to test
/// against; when it isn't AND `INDIVIDUATOR_REQUIRE_MTLS_INTEGRATION_TEST`
/// is set, records a failure instead of letting the caller return quietly.
private func canRunBoundSocketTest() -> Bool {
    if canBindAFUnixSocket { return true }
    if requireBoundSocketTests {
        let message = "DaemonSocketDirectory's bound-socket tests cannot run (the sandbox refuses "
            + "AF_UNIX bind) and INDIVIDUATOR_REQUIRE_MTLS_INTEGRATION_TEST is set, so CI requires "
            + "them to run rather than skip"
        Issue.record("\(message)")
    }
    return false
}

@Suite struct DaemonSocketDirectoryTests {
    private func makeTempPath() -> String {
        FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString).path
    }

    // --- prepareDirectory ---------------------------------------------

    @Test func createsAMissingDirectoryAtTheExpectedMode() throws {
        let dir = makeTempPath()
        defer { try? FileManager.default.removeItem(atPath: dir) }

        try DaemonSocketDirectory.prepareDirectory(path: dir, requiredOwnerUID: getuid())

        var st = stat()
        #expect(lstat(dir, &st) == 0)
        #expect(st.st_mode & 0o777 == mode_t(DaemonSocketDirectory.mode))
        #expect((st.st_mode & S_IFMT) == S_IFDIR)
    }

    @Test func acceptsAnExistingDirectoryAlreadyAtTheExpectedModeAndOwner() throws {
        let dir = makeTempPath()
        defer { try? FileManager.default.removeItem(atPath: dir) }
        try FileManager.default.createDirectory(
            atPath: dir, withIntermediateDirectories: true,
            attributes: [.posixPermissions: Int(DaemonSocketDirectory.mode)])

        try DaemonSocketDirectory.prepareDirectory(path: dir, requiredOwnerUID: getuid())
    }

    @Test func refusesASymlinkEvenWhenItsTargetMatchesModeAndOwner() throws {
        let realDir = makeTempPath()
        let linkPath = makeTempPath()
        defer {
            try? FileManager.default.removeItem(atPath: linkPath)
            try? FileManager.default.removeItem(atPath: realDir)
        }
        try FileManager.default.createDirectory(
            atPath: realDir, withIntermediateDirectories: true,
            attributes: [.posixPermissions: Int(DaemonSocketDirectory.mode)])
        try FileManager.default.createSymbolicLink(atPath: linkPath, withDestinationPath: realDir)

        #expect(throws: DaemonSocketDirectoryError.notAPlainDirectory(path: linkPath)) {
            try DaemonSocketDirectory.prepareDirectory(path: linkPath, requiredOwnerUID: getuid())
        }
    }

    @Test func refusesAnExistingDirectoryWithTheWrongMode() throws {
        let dir = makeTempPath()
        defer { try? FileManager.default.removeItem(atPath: dir) }
        try FileManager.default.createDirectory(
            atPath: dir, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])

        do {
            try DaemonSocketDirectory.prepareDirectory(path: dir, requiredOwnerUID: getuid())
            Issue.record("expected a refusal")
        } catch DaemonSocketDirectoryError.directoryModeWrong(_, let expected, let found) {
            #expect(expected == DaemonSocketDirectory.mode)
            #expect(found == 0o700)
        } catch {
            Issue.record("wrong error: \(error)")
        }
    }

    @Test func refusesAnExistingDirectoryWithTheWrongOwner() throws {
        let dir = makeTempPath()
        defer { try? FileManager.default.removeItem(atPath: dir) }
        try FileManager.default.createDirectory(
            atPath: dir, withIntermediateDirectories: true,
            attributes: [.posixPermissions: Int(DaemonSocketDirectory.mode)])
        // As in `KeyFileStoreTests.validateFileRefusesAWrongOwner`: a test
        // never runs as root, so an owner mismatch is exercised by
        // requiring a uid other than this process's own, rather than by a
        // real privilege change.
        let wrongUID = getuid() + 1

        do {
            try DaemonSocketDirectory.prepareDirectory(path: dir, requiredOwnerUID: wrongUID)
            Issue.record("expected a refusal")
        } catch DaemonSocketDirectoryError.directoryOwnerWrong(_, let expected, let found) {
            #expect(expected == wrongUID)
            #expect(found == getuid())
        } catch {
            Issue.record("wrong error: \(error)")
        }
    }

    // --- removeStaleSocketIfSafe ----------------------------------------

    @Test func removesAStaleSocketOwnedByTheRequiredUID() throws {
        guard canRunBoundSocketTest() else { return }
        let path = makeTempPath()
        defer { try? FileManager.default.removeItem(atPath: path) }
        #expect(bindRealSocket(at: path))

        try DaemonSocketDirectory.removeStaleSocketIfSafe(socketPath: path, requiredOwnerUID: getuid())

        var st = stat()
        #expect(lstat(path, &st) != 0)   // gone
    }

    @Test func doesNothingWhenNoStaleSocketIsPresent() throws {
        let path = makeTempPath()
        try DaemonSocketDirectory.removeStaleSocketIfSafe(socketPath: path, requiredOwnerUID: getuid())
    }

    @Test func refusesToRemoveAStalePathThatIsNotASocket() throws {
        let path = makeTempPath()
        defer { try? FileManager.default.removeItem(atPath: path) }
        try Data("not a socket".utf8).write(to: URL(fileURLWithPath: path))

        #expect(throws: DaemonSocketDirectoryError.staleSocketNotASocket(path: path)) {
            try DaemonSocketDirectory.removeStaleSocketIfSafe(socketPath: path, requiredOwnerUID: getuid())
        }
        #expect(FileManager.default.fileExists(atPath: path))
    }

    @Test func refusesToRemoveAStaleSocketOwnedByTheWrongUID() throws {
        guard canRunBoundSocketTest() else { return }
        let path = makeTempPath()
        defer { try? FileManager.default.removeItem(atPath: path) }
        #expect(bindRealSocket(at: path))
        let wrongUID = getuid() + 1

        do {
            try DaemonSocketDirectory.removeStaleSocketIfSafe(socketPath: path, requiredOwnerUID: wrongUID)
            Issue.record("expected a refusal")
        } catch DaemonSocketDirectoryError.staleSocketOwnerWrong(_, let expected, let found) {
            #expect(expected == wrongUID)
            #expect(found == getuid())
        } catch {
            Issue.record("wrong error: \(error)")
        }
        #expect(FileManager.default.fileExists(atPath: path))
    }

    // --- verifyRootOwnedChain (review round 2: the directory-rename race) --

    @Test func acceptsACorrectChainBuiltUnderATemporaryDirectory() throws {
        let base = makeTempPath()
        let level1 = base + "/level1"
        let level2 = level1 + "/level2"
        defer { try? FileManager.default.removeItem(atPath: base) }
        try FileManager.default.createDirectory(
            atPath: level2, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o755])
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: level1)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: base)

        try DaemonSocketDirectory.verifyRootOwnedChain([base, level1, level2], requiredOwnerUID: getuid())
    }

    @Test func refusesAChainComponentThatIsGroupWritable() throws {
        let base = makeTempPath()
        defer { try? FileManager.default.removeItem(atPath: base) }
        try FileManager.default.createDirectory(
            atPath: base, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o775])

        do {
            try DaemonSocketDirectory.verifyRootOwnedChain([base], requiredOwnerUID: getuid())
            Issue.record("expected a refusal")
        } catch DaemonSocketDirectoryError.ancestorWritableByOthers(_, let mode) {
            #expect(mode == 0o775)
        } catch {
            Issue.record("wrong error: \(error)")
        }
    }

    @Test func refusesAChainComponentThatIsOtherWritable() throws {
        let base = makeTempPath()
        defer { try? FileManager.default.removeItem(atPath: base) }
        try FileManager.default.createDirectory(
            atPath: base, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o757])

        do {
            try DaemonSocketDirectory.verifyRootOwnedChain([base], requiredOwnerUID: getuid())
            Issue.record("expected a refusal")
        } catch DaemonSocketDirectoryError.ancestorWritableByOthers(_, let mode) {
            #expect(mode == 0o757)
        } catch {
            Issue.record("wrong error: \(error)")
        }
    }

    @Test func refusesAChainComponentThatIsASymlink() throws {
        let realDir = makeTempPath()
        let linkPath = makeTempPath()
        defer {
            try? FileManager.default.removeItem(atPath: linkPath)
            try? FileManager.default.removeItem(atPath: realDir)
        }
        try FileManager.default.createDirectory(
            atPath: realDir, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o755])
        try FileManager.default.createSymbolicLink(atPath: linkPath, withDestinationPath: realDir)

        #expect(throws: DaemonSocketDirectoryError.notAPlainDirectory(path: linkPath)) {
            try DaemonSocketDirectory.verifyRootOwnedChain([linkPath], requiredOwnerUID: getuid())
        }
    }

    @Test func refusesAChainComponentOwnedByADifferentUID() throws {
        let base = makeTempPath()
        defer { try? FileManager.default.removeItem(atPath: base) }
        try FileManager.default.createDirectory(
            atPath: base, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o755])
        let wrongUID = getuid() + 1

        do {
            try DaemonSocketDirectory.verifyRootOwnedChain([base], requiredOwnerUID: wrongUID)
            Issue.record("expected a refusal")
        } catch DaemonSocketDirectoryError.directoryOwnerWrong(_, let expected, let found) {
            #expect(expected == wrongUID)
            #expect(found == getuid())
        } catch {
            Issue.record("wrong error: \(error)")
        }
    }
}
