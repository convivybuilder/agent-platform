import Foundation
import Testing
@testable import IndividuatorCore

@Suite struct KeyFileStoreTests {
    private func makeTempDirectory() -> URL {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        return dir
    }

    @Test func prepareForCreateMakesA0700DirectoryAndWriteSucceedsAt0600() throws {
        let dir = makeTempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = KeyFileStore(directory: dir, fileName: "k", requiredOwnerUID: getuid())

        try store.prepareForCreate()
        try store.write(Data("blob".utf8))

        var dirStat = stat()
        #expect(stat(dir.path, &dirStat) == 0)
        #expect(dirStat.st_mode & 0o777 == 0o700)

        var fileStat = stat()
        #expect(stat(store.fileURL.path, &fileStat) == 0)
        #expect(fileStat.st_mode & 0o777 == 0o600)

        // And it now loads clean.
        #expect(try store.read() == Data("blob".utf8))
    }

    @Test func prepareForCreateRefusesAnExistingKey() throws {
        let dir = makeTempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = KeyFileStore(directory: dir, fileName: "k", requiredOwnerUID: getuid())
        try store.prepareForCreate()
        try store.write(Data("first".utf8))

        #expect(throws: KeyFileStoreError.keyAlreadyExists(path: store.fileURL.path)) {
            try store.prepareForCreate()
        }
    }

    @Test func validateFileRefusesAWrongDirectoryMode() throws {
        let dir = makeTempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = KeyFileStore(directory: dir, fileName: "k", requiredOwnerUID: getuid())
        try store.prepareForCreate()
        try store.write(Data("blob".utf8))

        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: dir.path)

        #expect(throws: KeyFileStoreError.self) {
            try store.read()
        }
        do {
            _ = try store.read()
            Issue.record("expected a refusal")
        } catch KeyFileStoreError.directoryModeWrong(_, let expected, let found) {
            #expect(expected == 0o700)
            #expect(found == 0o755)
        } catch {
            Issue.record("wrong error: \(error)")
        }
    }

    @Test func validateFileRefusesAWrongFileMode() throws {
        let dir = makeTempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = KeyFileStore(directory: dir, fileName: "k", requiredOwnerUID: getuid())
        try store.prepareForCreate()
        try store.write(Data("blob".utf8))

        try FileManager.default.setAttributes([.posixPermissions: 0o644], ofItemAtPath: store.fileURL.path)

        do {
            _ = try store.read()
            Issue.record("expected a refusal")
        } catch KeyFileStoreError.fileModeWrong(_, let expected, let found) {
            #expect(expected == 0o600)
            #expect(found == 0o644)
        } catch {
            Issue.record("wrong error: \(error)")
        }
    }

    @Test func validateFileRefusesAWrongOwner() throws {
        let dir = makeTempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        // requiredOwnerUID deliberately does not match the uid that
        // actually created the file (this process's own uid) — the test
        // never runs as root, so this is the only way to exercise the
        // owner mismatch branch without a real privilege change.
        let wrongUID = getuid() + 1
        let store = KeyFileStore(directory: dir, fileName: "k", requiredOwnerUID: wrongUID)
        try FileManager.default.createDirectory(
            at: dir, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        try Data("blob".utf8).write(to: dir.appendingPathComponent("k"))
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: dir.appendingPathComponent("k").path)

        do {
            _ = try store.read()
            Issue.record("expected a refusal")
        } catch KeyFileStoreError.directoryOwnerWrong(_, let expected, let found) {
            #expect(expected == wrongUID)
            #expect(found == getuid())
        } catch {
            Issue.record("wrong error: \(error)")
        }
    }

    @Test func validateFileRefusesAMissingFile() throws {
        let dir = makeTempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        try FileManager.default.createDirectory(
            at: dir, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let store = KeyFileStore(directory: dir, fileName: "k", requiredOwnerUID: getuid())

        #expect(throws: KeyFileStoreError.fileMissing(path: store.fileURL.path)) {
            try store.read()
        }
    }
}
