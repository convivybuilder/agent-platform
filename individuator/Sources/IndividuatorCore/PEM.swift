import Foundation

public enum PEMError: Error, CustomStringConvertible {
    case noBeginLine
    case noEndLine
    case notBase64

    public var description: String {
        switch self {
        case .noBeginLine: return "no PEM '-----BEGIN ...-----' line found"
        case .noEndLine: return "no PEM '-----END ...-----' line found"
        case .notBase64: return "PEM body did not decode as base64"
        }
    }
}

/// Strips a PEM envelope (any label) down to its DER bytes. Used for both
/// certificates and CSRs; the label itself is not checked, since the two
/// callers in this package already know which they asked for.
public func derFromPEM(_ pem: String) throws -> Data {
    let lines = pem.split(separator: "\n", omittingEmptySubsequences: false)
    guard let beginIdx = lines.firstIndex(where: { $0.hasPrefix("-----BEGIN ") }) else {
        throw PEMError.noBeginLine
    }
    guard let endIdx = lines[beginIdx...].firstIndex(where: { $0.hasPrefix("-----END ") }) else {
        throw PEMError.noEndLine
    }
    let body = lines[(beginIdx + 1)..<endIdx].joined()
    guard let data = Data(base64Encoded: body) else {
        throw PEMError.notBase64
    }
    return data
}
