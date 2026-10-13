import Foundation
import Security

/// A named reason the Individuator refuses the Bosun's presented server
/// certificate — the checks 0092's Handoff lists under "Individuator server
/// checks": chain valid to the Library's root; the serverAuth extended key
/// usage; exactly one URI subjectAltName, equal to the Bosun host name read
/// from this machine's root-owned configuration.
public enum BosunServerRefusalReason: Equatable, CustomStringConvertible {
    case chainInvalid(detail: String)
    case missingServerAuth
    case sanNotExactlyOneURI(count: Int)
    case uriNameMismatch(expected: String, found: String)
    case malformedCertificate(detail: String)

    public var description: String {
        switch self {
        case .chainInvalid(let detail):
            return "the Bosun's server certificate does not chain to the Library's root: \(detail)"
        case .missingServerAuth:
            return "the Bosun's server certificate does not carry the serverAuth extended key usage"
        case .sanNotExactlyOneURI(let count):
            return "the Bosun's server certificate carries \(count) URI subjectAltName entries, not exactly one"
        case .uriNameMismatch(let expected, let found):
            return "the Bosun's server certificate names '\(found)', not the configured Bosun host '\(expected)'"
        case .malformedCertificate(let detail):
            return "could not parse the Bosun's server certificate: \(detail)"
        }
    }
}

public struct BosunServerRefusal: Error, CustomStringConvertible, Equatable {
    public let reason: BosunServerRefusalReason
    public var description: String { reason.description }
}

/// The Individuator's own verification of the certificate the Bosun presents
/// at the TLS handshake (0092 Handoff "Individuator server checks"). This
/// REPLACES ordinary hostname verification — it is never simply turned off —
/// so every check below runs regardless of what the TLS stack's own
/// certificate-chain validation already confirmed.
///
/// Deliberately independent of `CertificateAdoption` (which checks the
/// Individuator's OWN certificate, for clientAuth and against the machine
/// key): this checks the BOSUN's certificate, for serverAuth, and is never
/// handed a `MachineKey` because there is no key-match check to make here.
public enum BosunServerVerification {
    /// `expectedBosunURIName` is the full `convivy://machine/<name>` URI,
    /// not a bare host name — callers build it once, at configuration load
    /// time (`IndividuatorRootConfig`), via `MachineName.uri`, so a
    /// malformed host name in the root-owned configuration is refused
    /// there rather than inside every verification call.
    public static func verify(leafCertificateDER: Data, rootPEM: String, expectedBosunURIName: String) throws {
        let rootDER: Data
        do {
            rootDER = try derFromPEM(rootPEM)
        } catch {
            throw BosunServerRefusal(reason: .malformedCertificate(detail: "\(error)"))
        }
        guard let leaf = SecCertificateCreateWithData(nil, leafCertificateDER as CFData) else {
            throw BosunServerRefusal(reason: .malformedCertificate(detail: "not a valid X.509 certificate"))
        }
        guard let root = SecCertificateCreateWithData(nil, rootDER as CFData) else {
            throw BosunServerRefusal(reason: .malformedCertificate(detail: "root is not a valid X.509 certificate"))
        }

        try checkChainsToRoot(leaf: leaf, root: root)

        let extensions: [String: [UInt8]]
        do {
            extensions = try CertificateExtensions.extensions(ofCertificateDER: Array(leafCertificateDER))
        } catch {
            throw BosunServerRefusal(reason: .malformedCertificate(detail: "\(error)"))
        }

        try checkServerAuth(extensions: extensions)
        try checkURIName(extensions: extensions, expectedURI: expectedBosunURIName)
    }

    /// Same shape as `CertificateAdoption`'s own chain check: the Library's
    /// root is the ONLY trusted anchor, so a certificate from any other root
    /// is refused regardless of its own internal validity — never the
    /// system's trust store, and never any other root this machine happens
    /// to trust.
    private static func checkChainsToRoot(leaf: SecCertificate, root: SecCertificate) throws {
        let policy = SecPolicyCreateBasicX509()
        var trust: SecTrust?
        guard SecTrustCreateWithCertificates(leaf, policy, &trust) == errSecSuccess, let trust else {
            throw BosunServerRefusal(reason: .chainInvalid(detail: "could not build a trust object"))
        }
        guard SecTrustSetAnchorCertificates(trust, [root] as CFArray) == errSecSuccess else {
            throw BosunServerRefusal(reason: .chainInvalid(detail: "could not set the Library's root as the anchor"))
        }
        guard SecTrustSetAnchorCertificatesOnly(trust, true) == errSecSuccess else {
            throw BosunServerRefusal(reason: .chainInvalid(detail: "could not restrict trust to the Library's root"))
        }
        var trustError: CFError?
        guard SecTrustEvaluateWithError(trust, &trustError) else {
            throw BosunServerRefusal(reason: .chainInvalid(
                detail: trustError.map { String(describing: $0) } ?? "chain did not validate"))
        }
    }

    private static func checkServerAuth(extensions: [String: [UInt8]]) throws {
        guard let ekuValue = extensions[OID.extendedKeyUsage] else {
            throw BosunServerRefusal(reason: .missingServerAuth)
        }
        let keyPurposes: [String]
        do {
            keyPurposes = try CertificateExtensions.keyPurposeOIDs(ekuExtnValue: ekuValue)
        } catch {
            throw BosunServerRefusal(reason: .malformedCertificate(detail: "\(error)"))
        }
        guard keyPurposes.contains(OID.serverAuth) else {
            throw BosunServerRefusal(reason: .missingServerAuth)
        }
    }

    private static func checkURIName(extensions: [String: [UInt8]], expectedURI: String) throws {
        guard let sanValue = extensions[OID.subjectAltName] else {
            throw BosunServerRefusal(reason: .sanNotExactlyOneURI(count: 0))
        }
        let uriNames: [String]
        do {
            uriNames = try CertificateExtensions.uriNames(sanExtnValue: sanValue)
        } catch {
            throw BosunServerRefusal(reason: .malformedCertificate(detail: "\(error)"))
        }
        guard uriNames.count == 1 else {
            throw BosunServerRefusal(reason: .sanNotExactlyOneURI(count: uriNames.count))
        }
        guard uriNames[0] == expectedURI else {
            throw BosunServerRefusal(reason: .uriNameMismatch(expected: expectedURI, found: uriNames[0]))
        }
    }
}
