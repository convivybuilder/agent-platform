import CryptoKit
import Foundation
import Security

public enum AdoptionRefusalReason: Equatable, CustomStringConvertible {
    case keyMismatch
    case chainInvalid(detail: String)
    case missingClientAuth
    case sanNotExactlyOneURI(count: Int)
    case malformedCertificate(detail: String)

    public var description: String {
        switch self {
        case .keyMismatch:
            return "certificate's public key does not match the machine key"
        case .chainInvalid(let detail):
            return "certificate does not chain to the Library's root: \(detail)"
        case .missingClientAuth:
            return "certificate does not carry the clientAuth extended key usage"
        case .sanNotExactlyOneURI(let count):
            return "certificate carries \(count) URI subjectAltName entries, not exactly one"
        case .malformedCertificate(let detail):
            return "could not parse certificate: \(detail)"
        }
    }
}

public struct AdoptionRefusal: Error, CustomStringConvertible, Equatable {
    public let reason: AdoptionRefusalReason
    public var description: String { reason.description }
}

public struct AdoptedCertificate {
    public let certificateDER: Data
    public let certificatePEM: String
    public let uriName: String
}

/// Adopts a certificate the Library issued for this machine's key, per the
/// work item's four checks, in this order, each with its own named refusal:
///
/// 1. the certificate's public key equals the machine key's — Security's
///    own key-comparison path (`SecCertificateCopyKey` +
///    `SecKeyCopyExternalRepresentation`), not a hand-rolled one;
/// 2. the certificate chains to the Library's root — `SecTrust`, with the
///    root as the *only* anchor (`SecTrustSetAnchorCertificatesOnly`), so a
///    certificate from any other root is refused regardless of its own
///    internal validity;
/// 3. it carries the clientAuth extended key usage;
/// 4. it carries exactly one URI subjectAltName.
///
/// Checks 3 and 4 read the certificate's own DER directly
/// (`CertificateExtensions`, `ASN1Reader.swift`) rather than through
/// `SecCertificateCopyValues`, whose dictionary shape is not something this
/// package wants to pin a security check's correctness to. Checks 1 and 2
/// are exactly the two a hand-rolled reader should not attempt: key
/// comparison and chain validation are what `Security` exists to get right.
public enum CertificateAdoption {
    public static func adopt(
        certificatePEM: String, rootPEM: String, machineKey: MachineKey
    ) throws -> AdoptedCertificate {
        let certDER: Data
        let rootDER: Data
        do {
            certDER = try derFromPEM(certificatePEM)
            rootDER = try derFromPEM(rootPEM)
        } catch {
            throw AdoptionRefusal(reason: .malformedCertificate(detail: "\(error)"))
        }

        guard let cert = SecCertificateCreateWithData(nil, certDER as CFData) else {
            throw AdoptionRefusal(reason: .malformedCertificate(detail: "not a valid X.509 certificate"))
        }
        guard let root = SecCertificateCreateWithData(nil, rootDER as CFData) else {
            throw AdoptionRefusal(reason: .malformedCertificate(detail: "root is not a valid X.509 certificate"))
        }

        try checkKeyMatches(cert: cert, machineKey: machineKey)
        try checkChainsToRoot(cert: cert, root: root)

        let extensions: [String: [UInt8]]
        do {
            extensions = try CertificateExtensions.extensions(ofCertificateDER: Array(certDER))
        } catch {
            throw AdoptionRefusal(reason: .malformedCertificate(detail: "\(error)"))
        }

        try checkClientAuth(extensions: extensions)
        let uriName = try checkExactlyOneURISAN(extensions: extensions)

        return AdoptedCertificate(certificateDER: certDER, certificatePEM: certificatePEM, uriName: uriName)
    }

    private static func checkKeyMatches(cert: SecCertificate, machineKey: MachineKey) throws {
        guard let certKey = SecCertificateCopyKey(cert) else {
            throw AdoptionRefusal(reason: .malformedCertificate(detail: "certificate has no public key"))
        }
        var exportError: Unmanaged<CFError>?
        guard let certKeyData = SecKeyCopyExternalRepresentation(certKey, &exportError) as Data? else {
            throw AdoptionRefusal(reason: .malformedCertificate(
                detail: "could not read certificate's public key: \(String(describing: exportError))"))
        }
        guard certKeyData == machineKey.publicKey.x963Representation else {
            throw AdoptionRefusal(reason: .keyMismatch)
        }
    }

    private static func checkChainsToRoot(cert: SecCertificate, root: SecCertificate) throws {
        let policy = SecPolicyCreateBasicX509()
        var trust: SecTrust?
        guard SecTrustCreateWithCertificates(cert, policy, &trust) == errSecSuccess, let trust else {
            throw AdoptionRefusal(reason: .chainInvalid(detail: "could not build a trust object"))
        }
        guard SecTrustSetAnchorCertificates(trust, [root] as CFArray) == errSecSuccess else {
            throw AdoptionRefusal(reason: .chainInvalid(detail: "could not set the Library's root as the anchor"))
        }
        // Only the passed-in root is trusted — not the system roots, and not
        // any other root this machine happens to trust. A certificate from a
        // foreign root is refused here regardless of whether that root is
        // otherwise valid.
        guard SecTrustSetAnchorCertificatesOnly(trust, true) == errSecSuccess else {
            throw AdoptionRefusal(reason: .chainInvalid(detail: "could not restrict trust to the Library's root"))
        }
        var trustError: CFError?
        guard SecTrustEvaluateWithError(trust, &trustError) else {
            throw AdoptionRefusal(reason: .chainInvalid(
                detail: trustError.map { String(describing: $0) } ?? "chain did not validate"))
        }
    }

    private static func checkClientAuth(extensions: [String: [UInt8]]) throws {
        guard let ekuValue = extensions[OID.extendedKeyUsage] else {
            throw AdoptionRefusal(reason: .missingClientAuth)
        }
        let keyPurposes: [String]
        do {
            keyPurposes = try CertificateExtensions.keyPurposeOIDs(ekuExtnValue: ekuValue)
        } catch {
            throw AdoptionRefusal(reason: .malformedCertificate(detail: "\(error)"))
        }
        guard keyPurposes.contains(OID.clientAuth) else {
            throw AdoptionRefusal(reason: .missingClientAuth)
        }
    }

    private static func checkExactlyOneURISAN(extensions: [String: [UInt8]]) throws -> String {
        guard let sanValue = extensions[OID.subjectAltName] else {
            throw AdoptionRefusal(reason: .sanNotExactlyOneURI(count: 0))
        }
        let uriNames: [String]
        do {
            uriNames = try CertificateExtensions.uriNames(sanExtnValue: sanValue)
        } catch {
            throw AdoptionRefusal(reason: .malformedCertificate(detail: "\(error)"))
        }
        guard uriNames.count == 1 else {
            throw AdoptionRefusal(reason: .sanNotExactlyOneURI(count: uriNames.count))
        }
        return uriNames[0]
    }
}
