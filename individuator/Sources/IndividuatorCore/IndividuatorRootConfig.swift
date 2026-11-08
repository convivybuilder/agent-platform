import Foundation
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif

/// A named reason the root-owned configuration file is refused — the same
/// discipline `KeyFileStoreError` applies to the key blob's custody, for the
/// same reason: a config file a dispatched run could overwrite would let it
/// redirect the Individuator's registration to an attacker-controlled host,
/// or widen which local uid the root daemon accepts a `register` request
/// from.
public enum IndividuatorRootConfigError: Error, CustomStringConvertible, Equatable {
    case fileMissing(path: String)
    case fileModeWrong(path: String, expected: UInt16, found: UInt16)
    case fileOwnerWrong(path: String, expected: uid_t, found: uid_t)
    case malformed(detail: String)
    case invalidBosunHost(String)

    public var description: String {
        switch self {
        case .fileMissing(let p):
            return "individuator config \(p) does not exist"
        case .fileModeWrong(let p, let expected, let found):
            return "individuator config \(p) has mode \(String(found, radix: 8)), expected \(String(expected, radix: 8))"
        case .fileOwnerWrong(let p, let expected, let found):
            return "individuator config \(p) is owned by uid \(found), expected \(expected)"
        case .malformed(let detail):
            return "individuator config is malformed: \(detail)"
        case .invalidBosunHost(let name):
            return "individuator config names an invalid bosun_host: \(name)"
        }
    }
}

/// One entry of `admitted_accounts` in the config file.
private struct AdmittedAccountFile: Decodable {
    let uid: uid_t
    let account: String
}

private struct ConfigFile: Decodable {
    let bosun_host: String
    let bosun_address: String
    let root_ca_path: String
    /// The pre-E12·2 shape: exactly one admitted uid, with no account name
    /// of its own — the daemon resolved its name via `getpwuid` every start.
    /// Still read, as a one-entry `admittedAccounts`, so the config already
    /// installed keeps working unchanged.
    let operator_uid: uid_t?
    /// The E12·2 shape: every admitted uid, each with the account name the
    /// daemon reports for it, set once by `bin/install-individuator` rather
    /// than re-resolved via `getpwuid` on every connection.
    let admitted_accounts: [AdmittedAccountFile]?
    /// The forwarding endpoint's Library destination (E12·2 slice S5b; 0092
    /// "The Individuator carries a sessions-only host's clients to the
    /// Keel"): where a `.library`-destined allowlisted route is dialled.
    /// Optional, and absent by default — `bin/install-individuator` only
    /// writes it when `--library-address` is given, and the daemon serves
    /// no forwarding socket at all until it is present
    /// (`ForwardingServingDecision`). The Bosun destination reuses
    /// `bosun_address`/`bosun_host` above, never a second field: both
    /// services present the same Keel host's server certificate (0092
    /// section 6), so one expected URI name (`bosunURIName`, below) serves
    /// both destinations.
    let library_address: String?
}

/// One local account this machine's root daemon accepts a `register` request
/// from, paired with the uid `getpeereid` must report for it.
public struct AdmittedAccount: Equatable {
    public let uid: uid_t
    public let account: String

    public init(uid: uid_t, account: String) {
        self.uid = uid
        self.account = account
    }
}

/// The root-owned configuration naming the Bosun this machine's Individuator
/// registers against (0092 Handoff "Individuator server checks"), and the
/// accounts the root daemon's Unix socket accepts a `register` request from
/// (0092 item 8: a root-owned list of admitted accounts; CQ·447 slice 3's
/// root-daemon peer check, extended by E12·2 from one admitted uid to a
/// list). Read by both the mTLS client (the Bosun host name and address, and
/// the Library root CA path it pins trust to) and the root daemon (the
/// admitted accounts).
///
/// Custody mirrors `KeyFileStore`: a root-owned (by default) file at 0600,
/// checked before it is ever parsed.
public struct IndividuatorRootConfig: Equatable {
    public let bosunHost: String
    /// `convivy://machine/<bosunHost>`, computed once here via
    /// `MachineName.uri` so every caller of `BosunServerVerification.verify`
    /// passes an already-validated URI rather than re-validating the host
    /// name on every handshake.
    public let bosunURIName: String
    public let bosunAddress: String
    public let rootCAPath: String
    public let admittedAccounts: [AdmittedAccount]
    /// The forwarding endpoint's Library destination (E12·2 slice S5b), or
    /// `nil` when none is configured. See `ConfigFile.library_address`'s
    /// own doc comment for why there is no separate `libraryURIName`.
    public let libraryAddress: String?

    /// Exposed so a test can build a config directly — bypassing `load`'s
    /// file-custody checks, which exist to protect a real root-owned file,
    /// not a value a test already built in memory — while every real caller
    /// goes through `load`.
    public init(
        bosunHost: String, bosunURIName: String, bosunAddress: String, rootCAPath: String,
        admittedAccounts: [AdmittedAccount], libraryAddress: String? = nil
    ) {
        self.bosunHost = bosunHost
        self.bosunURIName = bosunURIName
        self.bosunAddress = bosunAddress
        self.rootCAPath = rootCAPath
        self.admittedAccounts = admittedAccounts
        self.libraryAddress = libraryAddress
    }

    public static func load(from path: URL, requiredOwnerUID: uid_t = 0) throws -> IndividuatorRootConfig {
        var st = stat()
        guard stat(path.path, &st) == 0 else {
            throw IndividuatorRootConfigError.fileMissing(path: path.path)
        }
        let mode = UInt16(st.st_mode & 0o777)
        guard mode == 0o600 else {
            throw IndividuatorRootConfigError.fileModeWrong(path: path.path, expected: 0o600, found: mode)
        }
        guard st.st_uid == requiredOwnerUID else {
            throw IndividuatorRootConfigError.fileOwnerWrong(
                path: path.path, expected: requiredOwnerUID, found: st.st_uid)
        }

        let data: Data
        do {
            data = try Data(contentsOf: path)
        } catch {
            throw IndividuatorRootConfigError.malformed(detail: "\(error)")
        }
        let decoded: ConfigFile
        do {
            decoded = try JSONDecoder().decode(ConfigFile.self, from: data)
        } catch {
            throw IndividuatorRootConfigError.malformed(detail: "\(error)")
        }

        let admittedAccounts: [AdmittedAccount]
        if let listed = decoded.admitted_accounts, !listed.isEmpty {
            var seenUIDs = Set<uid_t>()
            var seenAccounts = Set<String>()
            for entry in listed {
                guard seenUIDs.insert(entry.uid).inserted else {
                    throw IndividuatorRootConfigError.malformed(
                        detail: "admitted_accounts repeats uid \(entry.uid)")
                }
                guard seenAccounts.insert(entry.account).inserted else {
                    throw IndividuatorRootConfigError.malformed(
                        detail: "admitted_accounts repeats account \(entry.account)")
                }
            }
            admittedAccounts = listed.map { AdmittedAccount(uid: $0.uid, account: $0.account) }
        } else if let legacyUID = decoded.operator_uid {
            guard let pw = getpwuid(legacyUID) else {
                throw IndividuatorRootConfigError.malformed(
                    detail: "no password-database entry for operator_uid \(legacyUID)")
            }
            admittedAccounts = [AdmittedAccount(uid: legacyUID, account: String(cString: pw.pointee.pw_name))]
        } else {
            throw IndividuatorRootConfigError.malformed(
                detail: "config names neither admitted_accounts nor operator_uid")
        }

        do {
            let uri = try MachineName.uri(decoded.bosun_host)
            return IndividuatorRootConfig(
                bosunHost: decoded.bosun_host, bosunURIName: uri, bosunAddress: decoded.bosun_address,
                rootCAPath: decoded.root_ca_path, admittedAccounts: admittedAccounts,
                libraryAddress: decoded.library_address)
        } catch {
            throw IndividuatorRootConfigError.invalidBosunHost(decoded.bosun_host)
        }
    }
}
