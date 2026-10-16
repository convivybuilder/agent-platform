import Foundation
import Testing
@testable import IndividuatorCore

/// `SoftwareMachineKey.create`/`load` are the daemon's own machine-key
/// path (2026-10-15, replacing `SecureEnclaveMachineKey`) — these exercise
/// them directly, something the retired Enclave type's own `requireRoot()`
/// never let a test do.
@Suite struct SoftwareMachineKeyTests {
    private func makeTempDirectory() -> URL {
        FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    }

    @Test func createPersistsAKeyFileAt0600OwnedByTheRequestedUID() throws {
        let dir = makeTempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = KeyFileStore(directory: dir, fileName: "k", requiredOwnerUID: getuid())

        _ = try SoftwareMachineKey.create(store: store)

        var fileStat = stat()
        #expect(stat(store.fileURL.path, &fileStat) == 0)
        #expect(fileStat.st_mode & 0o777 == 0o600)
        #expect(fileStat.st_uid == getuid())
    }

    @Test func createRefusesAnExistingKeyAndLeavesItUnchanged() throws {
        let dir = makeTempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = KeyFileStore(directory: dir, fileName: "k", requiredOwnerUID: getuid())

        let first = try SoftwareMachineKey.create(store: store)
        let firstBlob = try Data(contentsOf: store.fileURL)

        #expect(throws: (any Error).self) {
            _ = try SoftwareMachineKey.create(store: store)
        }

        let blobAfterSecondAttempt = try Data(contentsOf: store.fileURL)
        #expect(blobAfterSecondAttempt == firstBlob)

        // The first key is still the one on disk: reloading signs the same
        // way `first` does.
        let reloaded = try SoftwareMachineKey.load(store: store)
        let message = Data("unchanged".utf8)
        let signature = try first.signature(for: message)
        #expect(reloaded.publicKey.isValidSignature(signature, for: message))
    }

    @Test func reloadingAfterCreateVerifiesASignatureMadeByTheOriginal() throws {
        let dir = makeTempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = KeyFileStore(directory: dir, fileName: "k", requiredOwnerUID: getuid())

        let original = try SoftwareMachineKey.create(store: store)
        let message = Data("round-trip".utf8)
        let signature = try original.signature(for: message)

        let reloaded = try SoftwareMachineKey.load(store: store)
        #expect(reloaded.publicKey.isValidSignature(signature, for: message))
        #expect(reloaded.publicKey.x963Representation == original.publicKey.x963Representation)
    }

    @Test func loadRefusesAKeyFileWithTheWrongMode() throws {
        let dir = makeTempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = KeyFileStore(directory: dir, fileName: "k", requiredOwnerUID: getuid())
        _ = try SoftwareMachineKey.create(store: store)
        try FileManager.default.setAttributes([.posixPermissions: 0o644], ofItemAtPath: store.fileURL.path)

        do {
            _ = try SoftwareMachineKey.load(store: store)
            Issue.record("expected a refusal")
        } catch SoftwareMachineKeyError.store(.fileModeWrong(_, let expected, let found)) {
            #expect(expected == 0o600)
            #expect(found == 0o644)
        } catch {
            Issue.record("wrong error: \(error)")
        }
    }
}
