import Foundation

/// A named reason a key blob's filesystem custody is refused — never a raw
/// `errno` or a generic "could not read the file", because the operator
/// diagnosing a refusal needs to know which of mode, owner or existence
/// failed.
public enum KeyFileStoreError: Error, CustomStringConvertible, Equatable {
    case directoryMissing(path: String)
    case directoryModeWrong(path: String, expected: UInt16, found: UInt16)
    case directoryOwnerWrong(path: String, expected: uid_t, found: uid_t)
    case fileMissing(path: String)
    case fileModeWrong(path: String, expected: UInt16, found: UInt16)
    case fileOwnerWrong(path: String, expected: uid_t, found: uid_t)
    case keyAlreadyExists(path: String)

    public var description: String {
        switch self {
        case .directoryMissing(let p):
            return "key directory \(p) does not exist"
        case .directoryModeWrong(let p, let expected, let found):
            return "key directory \(p) has mode \(String(found, radix: 8)), expected \(String(expected, radix: 8))"
        case .directoryOwnerWrong(let p, let expected, let found):
            return "key directory \(p) is owned by uid \(found), expected \(expected)"
        case .fileMissing(let p):
            return "key file \(p) does not exist"
        case .fileModeWrong(let p, let expected, let found):
            return "key file \(p) has mode \(String(found, radix: 8)), expected \(String(expected, radix: 8))"
        case .fileOwnerWrong(let p, let expected, let found):
            return "key file \(p) is owned by uid \(found), expected \(expected)"
        case .keyAlreadyExists(let p):
            return "a key already exists at \(p); refusing to overwrite it"
        }
    }
}

/// Filesystem custody for a machine key blob: a 0700 directory holding a
/// 0600 file, both owned by `requiredOwnerUID` — the account the
/// Individuator's root daemon runs as (0 in production; a test passes its
/// own uid, since nothing under `Tests/` may run as root or touch a real
/// key, per this repo's operating rules).
///
/// This type knows nothing about Secure Enclave or CryptoKit; it only
/// stat()s and writes a directory and a file. `SecureEnclaveMachineKey`
/// calls it before (`prepareForCreate`/`validateForLoad`) and after
/// (`write`) touching the Enclave, so every custody check in the spec is
/// exercised here, in isolation, by a test that never creates a real
/// hardware key.
public struct KeyFileStore {
    public let directory: URL
    public let fileName: String
    public let requiredOwnerUID: uid_t

    public init(directory: URL, fileName: String, requiredOwnerUID: uid_t = 0) {
        self.directory = directory
        self.fileName = fileName
        self.requiredOwnerUID = requiredOwnerUID
    }

    public var fileURL: URL { directory.appendingPathComponent(fileName) }

    private struct StatResult {
        let mode: UInt16
        let owner: uid_t
    }

    private static func statOrNil(_ path: String) -> StatResult? {
        var st = stat()
        guard stat(path, &st) == 0 else { return nil }
        return StatResult(mode: UInt16(st.st_mode & 0o777), owner: st.st_uid)
    }

    /// Checks the directory's mode and owner. Does not create it — callers
    /// that may need to create it on first use call `prepareForCreate`
    /// instead, which calls this only for an already-existing directory.
    public func validateDirectory() throws {
        guard let st = Self.statOrNil(directory.path) else {
            throw KeyFileStoreError.directoryMissing(path: directory.path)
        }
        guard st.mode == 0o700 else {
            throw KeyFileStoreError.directoryModeWrong(path: directory.path, expected: 0o700, found: st.mode)
        }
        guard st.owner == requiredOwnerUID else {
            throw KeyFileStoreError.directoryOwnerWrong(path: directory.path, expected: requiredOwnerUID, found: st.owner)
        }
    }

    /// Checks the key file's mode and owner, after checking the directory
    /// that holds it. Used both before loading an existing key and, inside
    /// `prepareForCreate`, to refuse overwriting one.
    public func validateFile() throws {
        try validateDirectory()
        guard let st = Self.statOrNil(fileURL.path) else {
            throw KeyFileStoreError.fileMissing(path: fileURL.path)
        }
        guard st.mode == 0o600 else {
            throw KeyFileStoreError.fileModeWrong(path: fileURL.path, expected: 0o600, found: st.mode)
        }
        guard st.owner == requiredOwnerUID else {
            throw KeyFileStoreError.fileOwnerWrong(path: fileURL.path, expected: requiredOwnerUID, found: st.owner)
        }
    }

    /// Prepares the directory for a first-time key write. Creates it at
    /// 0700 when it does not yet exist; when it does, validates its mode
    /// and owner rather than relaxing them. Refuses outright, before
    /// touching anything, when a key file is already present — a key is
    /// never regenerated by writing over one.
    public func prepareForCreate() throws {
        if FileManager.default.fileExists(atPath: fileURL.path) {
            throw KeyFileStoreError.keyAlreadyExists(path: fileURL.path)
        }
        if FileManager.default.fileExists(atPath: directory.path) {
            try validateDirectory()
        } else {
            try FileManager.default.createDirectory(
                at: directory, withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700])
        }
    }

    /// Validates custody and loads the blob.
    public func read() throws -> Data {
        try validateFile()
        return try Data(contentsOf: fileURL)
    }

    /// Writes the blob at 0600. Callers are expected to have called
    /// `prepareForCreate` first; this does not re-check for an existing
    /// file, since the Enclave key has, by the time this runs, already
    /// been generated and must be persisted.
    public func write(_ data: Data) throws {
        try data.write(to: fileURL, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: fileURL.path)
    }
}
