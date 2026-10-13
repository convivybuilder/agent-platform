import Foundation

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

private struct ConfigFile: Decodable {
    let bosun_host: String
    let bosun_address: String
    let root_ca_path: String
    let operator_uid: uid_t
}

/// The root-owned configuration naming the Bosun this machine's Individuator
/// registers against (0092 Handoff "Individuator server checks"), and the
/// operator uid the root daemon's Unix socket accepts a `register` request
/// from (0092 Handoff "Individuator key accessibility" context; CQ·447
/// slice 3's root-daemon peer check). Read by both the mTLS client (the
/// Bosun host name and address, and the Library root CA path it pins trust
/// to) and the root daemon (the operator uid).
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
    public let operatorUID: uid_t

    /// Exposed so a test can build a config directly — bypassing `load`'s
    /// file-custody checks, which exist to protect a real root-owned file,
    /// not a value a test already built in memory — while every real caller
    /// goes through `load`.
    public init(bosunHost: String, bosunURIName: String, bosunAddress: String, rootCAPath: String, operatorUID: uid_t) {
        self.bosunHost = bosunHost
        self.bosunURIName = bosunURIName
        self.bosunAddress = bosunAddress
        self.rootCAPath = rootCAPath
        self.operatorUID = operatorUID
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

        do {
            let uri = try MachineName.uri(decoded.bosun_host)
            return IndividuatorRootConfig(
                bosunHost: decoded.bosun_host, bosunURIName: uri, bosunAddress: decoded.bosun_address,
                rootCAPath: decoded.root_ca_path, operatorUID: decoded.operator_uid)
        } catch {
            throw IndividuatorRootConfigError.invalidBosunHost(decoded.bosun_host)
        }
    }
}
