import Foundation
import Testing
@testable import IndividuatorCore

/// `IndividuatorRootConfig` holding a list of admitted accounts rather
/// than one fixed uid (E12·2; platform/decisions/0099 item 8: a root-owned
/// list of admitted accounts).
@Suite struct IndividuatorRootConfigTests {
    private func makeTempDirectory() -> URL {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try! FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    private func writeConfig(_ json: String, in dir: URL) -> URL {
        let path = dir.appendingPathComponent("config.json")
        try! json.write(to: path, atomically: true, encoding: .utf8)
        try! FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path.path)
        return path
    }

    @Test func loadsMultipleAdmittedAccounts() throws {
        let dir = makeTempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let json = """
        {
          "bosun_host": "bosun-test",
          "bosun_address": "127.0.0.1:1",
          "root_ca_path": "/dev/null",
          "admitted_accounts": [
            {"uid": 9001, "account": "alice"},
            {"uid": 9002, "account": "bob"}
          ]
        }
        """
        let path = writeConfig(json, in: dir)

        let config = try IndividuatorRootConfig.load(from: path, requiredOwnerUID: getuid())

        #expect(
            config.admittedAccounts == [
                AdmittedAccount(uid: 9001, account: "alice"),
                AdmittedAccount(uid: 9002, account: "bob"),
            ])
    }

    /// The pre-E12·2 shape (one `operator_uid`, no `admitted_accounts`)
    /// still loads, as a one-entry list, so the config already installed
    /// keeps working unchanged.
    @Test func loadsALegacyOperatorUIDAsAOneEntryList() throws {
        let dir = makeTempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let json = """
        {
          "bosun_host": "bosun-test",
          "bosun_address": "127.0.0.1:1",
          "root_ca_path": "/dev/null",
          "operator_uid": \(getuid())
        }
        """
        let path = writeConfig(json, in: dir)

        let config = try IndividuatorRootConfig.load(from: path, requiredOwnerUID: getuid())

        let expectedAccount = String(cString: getpwuid(getuid())!.pointee.pw_name)
        #expect(config.admittedAccounts == [AdmittedAccount(uid: getuid(), account: expectedAccount)])
    }

    /// A repeated uid in `admitted_accounts` must be refused by `load`
    /// itself, with a catchable error — not left for
    /// `Dictionary(uniqueKeysWithValues:)` in the daemon's entry point to
    /// trap on (reviewer finding on PR 1110, round 1).
    @Test func refusesADuplicateUIDInAdmittedAccounts() throws {
        let dir = makeTempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let json = """
        {
          "bosun_host": "bosun-test",
          "bosun_address": "127.0.0.1:1",
          "root_ca_path": "/dev/null",
          "admitted_accounts": [
            {"uid": 9001, "account": "probe-alpha"},
            {"uid": 9001, "account": "probe-beta"}
          ]
        }
        """
        let path = writeConfig(json, in: dir)

        #expect(throws: IndividuatorRootConfigError.self) {
            try IndividuatorRootConfig.load(from: path, requiredOwnerUID: getuid())
        }
    }

    /// A repeated account name, even with distinct uids, is refused the
    /// same way — the daemon must be able to tell the two entries apart
    /// by name as well as by uid.
    @Test func refusesADuplicateAccountNameInAdmittedAccounts() throws {
        let dir = makeTempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let json = """
        {
          "bosun_host": "bosun-test",
          "bosun_address": "127.0.0.1:1",
          "root_ca_path": "/dev/null",
          "admitted_accounts": [
            {"uid": 9001, "account": "probe-alpha"},
            {"uid": 9002, "account": "probe-alpha"}
          ]
        }
        """
        let path = writeConfig(json, in: dir)

        #expect(throws: IndividuatorRootConfigError.self) {
            try IndividuatorRootConfig.load(from: path, requiredOwnerUID: getuid())
        }
    }

    /// E12·2 slice S5b: the forwarding endpoint's Library destination is
    /// an optional field, read when `bin/install-individuator` was given
    /// `--library-address`.
    @Test func loadsAnOptionalLibraryAddress() throws {
        let dir = makeTempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let json = """
        {
          "bosun_host": "bosun-test",
          "bosun_address": "127.0.0.1:1",
          "root_ca_path": "/dev/null",
          "operator_uid": \(getuid()),
          "library_address": "127.0.0.1:2"
        }
        """
        let path = writeConfig(json, in: dir)

        let config = try IndividuatorRootConfig.load(from: path, requiredOwnerUID: getuid())

        #expect(config.libraryAddress == "127.0.0.1:2")
    }

    /// An install that never names `--library-address` leaves the field
    /// absent, and `libraryAddress` reads `nil` rather than refusing to
    /// load — `ForwardingServingDecision` is what turns that absence into
    /// "serve no forwarding socket", not a load-time failure.
    @Test func defaultsLibraryAddressToNilWhenAbsent() throws {
        let dir = makeTempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let json = """
        {
          "bosun_host": "bosun-test",
          "bosun_address": "127.0.0.1:1",
          "root_ca_path": "/dev/null",
          "operator_uid": \(getuid())
        }
        """
        let path = writeConfig(json, in: dir)

        let config = try IndividuatorRootConfig.load(from: path, requiredOwnerUID: getuid())

        #expect(config.libraryAddress == nil)
    }

    @Test func refusesAConfigNamingNeitherShape() throws {
        let dir = makeTempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let json = """
        {
          "bosun_host": "bosun-test",
          "bosun_address": "127.0.0.1:1",
          "root_ca_path": "/dev/null"
        }
        """
        let path = writeConfig(json, in: dir)

        #expect(throws: IndividuatorRootConfigError.self) {
            try IndividuatorRootConfig.load(from: path, requiredOwnerUID: getuid())
        }
    }
}
