import Foundation
import Testing
@testable import IndividuatorCore
@testable import IndividuatorDaemonCore

/// `DaemonKeyStartup.prepareKey` is what `individuator-daemon`'s entry
/// point calls on every start — factored out here so it is testable
/// without launchd, root or a real `/var/db/individuator` (2026-10-15).
@Suite(.timeLimit(.minutes(1)))
struct DaemonKeyStartupTests {
    private func makeTempDirectory() -> URL {
        FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    }

    @Test func absentKeyIsCreatedAndSelfTestedAndTheLogLineNamesTheFingerprintWithNoPrivateBytes() throws {
        let dir = makeTempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = KeyFileStore(directory: dir, fileName: "k", requiredOwnerUID: getuid())

        let result = try DaemonKeyStartup.prepareKey(store: store, effectiveUID: getuid())

        #expect(FileManager.default.fileExists(atPath: store.fileURL.path))
        let fingerprint = CertificateSigningRequest.publicKeyFingerprintHex(result.key.publicKey)
        #expect(result.logLine.contains(fingerprint))
        #expect(result.logLine.contains("created"))
        #expect(result.logLine.contains("\(getuid())"))

        // Never the private key's own bytes, in any encoding this package
        // produces for it.
        let rawHex = (try Data(contentsOf: store.fileURL)).map { String(format: "%02x", $0) }.joined()
        #expect(!result.logLine.lowercased().contains(rawHex))
    }

    @Test func aPresentKeyIsLoadedAndReusedUnchanged() throws {
        let dir = makeTempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = KeyFileStore(directory: dir, fileName: "k", requiredOwnerUID: getuid())
        _ = try SoftwareMachineKey.create(store: store)
        let blobBefore = try Data(contentsOf: store.fileURL)

        let result = try DaemonKeyStartup.prepareKey(store: store, effectiveUID: getuid())

        let blobAfter = try Data(contentsOf: store.fileURL)
        #expect(blobBefore == blobAfter)
        #expect(result.logLine.contains("loaded"))
    }

    @Test func aKeyFileWithTheWrongModeRefusesStartupWithTheKeyFileStoreReason() throws {
        let dir = makeTempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = KeyFileStore(directory: dir, fileName: "k", requiredOwnerUID: getuid())
        _ = try SoftwareMachineKey.create(store: store)
        try FileManager.default.setAttributes([.posixPermissions: 0o644], ofItemAtPath: store.fileURL.path)

        do {
            _ = try DaemonKeyStartup.prepareKey(store: store, effectiveUID: getuid())
            Issue.record("expected a refusal")
        } catch DaemonKeyStartupError.load(.store(.fileModeWrong(_, let expected, let found))) {
            #expect(expected == 0o600)
            #expect(found == 0o644)
        } catch {
            Issue.record("wrong error: \(error)")
        }
    }

    @Test func aKeyFileWithTheWrongOwnerRefusesStartupWithTheKeyFileStoreReason() throws {
        let dir = makeTempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let creatingStore = KeyFileStore(directory: dir, fileName: "k", requiredOwnerUID: getuid())
        _ = try SoftwareMachineKey.create(store: creatingStore)

        // A different required owner than the uid that actually created
        // the file — the only way to exercise the owner-mismatch branch
        // without a real privilege change (same idiom KeyFileStoreTests
        // already uses).
        let wrongOwnerStore = KeyFileStore(directory: dir, fileName: "k", requiredOwnerUID: getuid() + 1)

        do {
            _ = try DaemonKeyStartup.prepareKey(store: wrongOwnerStore, effectiveUID: getuid())
            Issue.record("expected a refusal")
        } catch DaemonKeyStartupError.load(.store(.directoryOwnerWrong(_, let expected, let found))) {
            #expect(expected == getuid() + 1)
            #expect(found == getuid())
        } catch {
            Issue.record("wrong error: \(error)")
        }
    }
}
