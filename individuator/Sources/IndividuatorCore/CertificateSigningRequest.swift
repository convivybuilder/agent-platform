import CryptoKit
import Foundation

/// OIDs this package writes into a CSR or reads out of a certificate.
/// Named here once rather than as string literals scattered through the
/// builder, since several of them (clientAuth, ecdsa-with-SHA256) also
/// appear in `CertificateAdoption.swift`.
enum OID {
    static let ecPublicKey = "1.2.840.10045.2.1"
    static let prime256v1 = "1.2.840.10045.3.1.7"
    static let ecdsaWithSHA256 = "1.2.840.10045.4.3.2"
    static let commonName = "2.5.4.3"
    static let subjectAltName = "2.5.29.17"
    static let extendedKeyUsage = "2.5.29.37"
    static let extensionRequest = "1.2.840.113549.1.9.14"
    static let clientAuth = "1.3.6.1.5.5.7.3.2"
    // Read by `BosunServerVerification` (slice 3) off the Bosun's own server
    // certificate — never written by this package's own CSR builder, which
    // only ever requests clientAuth for the Individuator's own machine key.
    static let serverAuth = "1.3.6.1.5.5.7.3.1"
}

public enum MachineNameError: Error, CustomStringConvertible {
    case invalid(String)

    public var description: String {
        switch self {
        case .invalid(let name):
            return "'\(name)' is not a valid machine name (must match the Bosun's own " +
                "^[a-z0-9][a-z0-9-]{0,63}$, lib/agent-platform/bosun_registration.py's _SAN_URI_RE)"
        }
    }
}

/// The same shape the Bosun's `check_client_certificate` requires of the
/// SAN it reads (`_SAN_URI_RE` in `lib/agent-platform/bosun_registration.py`)
/// — kept in sync by hand since the two live in different languages and
/// repos' worth of distance, not by a shared constant.
public enum MachineName {
    private static let pattern = try! NSRegularExpression(pattern: "^[a-z0-9][a-z0-9-]{0,63}$")

    public static func validate(_ name: String) throws {
        let range = NSRange(name.startIndex..<name.endIndex, in: name)
        guard pattern.firstMatch(in: name, range: range) != nil else {
            throw MachineNameError.invalid(name)
        }
    }

    public static func uri(_ name: String) throws -> String {
        try validate(name)
        return "convivy://machine/\(name)"
    }
}

/// Builds a PKCS#10 certification request for a machine key: a CSR with
/// one commonName in its subject, exactly one `convivy://machine/<name>`
/// URI subjectAltName, the clientAuth extended key usage, and its own
/// signature over `CertificationRequestInfo` made with the same key —
/// a CSR's self-signature is what proves possession of the private key,
/// which is the whole of what "self-signed with the machine key" in the
/// work item means here. Built entirely with this package's own `DER`
/// encoder: neither Security nor CryptoKit exposes a public PKCS#10 API.
public enum CertificateSigningRequest {
    /// The DER `SubjectPublicKeyInfo` this CSR's own `subjectPublicKeyInfo`
    /// field carries.
    public static func subjectPublicKeyInfo(_ publicKey: P256.Signing.PublicKey) -> [UInt8] {
        let algorithm = DER.sequence([DER.oid(OID.ecPublicKey), DER.oid(OID.prime256v1)])
        let subjectPublicKey = DER.bitString(Array(publicKey.x963Representation))
        return DER.sequence([algorithm, subjectPublicKey])
    }

    /// SHA-256 over `subjectPublicKeyInfo`'s DER, hex-encoded — what
    /// `individuator fingerprint` (CQ·447 slice 3, `Sources/individuator/main.swift`)
    /// prints, the input it hashes now the same SPKI DER the Library's
    /// `root-ca sign` hashes (`root-ca`'s own `Fingerprint.publicKey`), fixing
    /// the defect where `fingerprint` hashed the raw 65-byte public point
    /// instead and so could never match `root-ca sign`'s output for the same
    /// key. This is the exact comparison the signing ceremony's operator
    /// makes before the Touch ID gesture (platform/decisions/0066, issuance
    /// step 3).
    public static func publicKeyFingerprintHex(_ publicKey: P256.Signing.PublicKey) -> String {
        let der = Data(subjectPublicKeyInfo(publicKey))
        return SHA256.hash(data: der).map { String(format: "%02x", $0) }.joined()
    }

    private static func subjectName(commonName: String) -> [UInt8] {
        let atv = DER.sequence([DER.oid(OID.commonName), DER.utf8String(commonName)])
        let rdn = DER.set([atv])
        return DER.sequence([rdn])
    }

    private static func sanExtensionValue(uri: String) -> [UInt8] {
        let generalName = DER.contextPrimitive(6, Array(uri.utf8))
        return DER.sequence([generalName])
    }

    private static func ekuExtensionValue() -> [UInt8] {
        DER.sequence([DER.oid(OID.clientAuth)])
    }

    private static func extensionRequestAttribute(machineURI: String) -> [UInt8] {
        let sanExtension = DER.sequence([
            DER.oid(OID.subjectAltName),
            DER.octetString(sanExtensionValue(uri: machineURI)),
        ])
        let ekuExtension = DER.sequence([
            DER.oid(OID.extendedKeyUsage),
            DER.octetString(ekuExtensionValue()),
        ])
        let extensions = DER.sequence([sanExtension, ekuExtension])
        let attribute = DER.sequence([
            DER.oid(OID.extensionRequest),
            DER.set([extensions]),
        ])
        return DER.contextConstructed(0, [attribute])
    }

    /// Builds the signed CSR for `machineName` as DER bytes, and separately
    /// as a PEM `CERTIFICATE REQUEST`.
    public static func build(machineName: String, key: MachineKey) throws -> (der: Data, pem: String) {
        let uri = try MachineName.uri(machineName)

        let certificationRequestInfo = DER.sequence([
            DER.integer(0),
            subjectName(commonName: machineName),
            subjectPublicKeyInfo(key.publicKey),
            extensionRequestAttribute(machineURI: uri),
        ])

        let signature = try key.signature(for: certificationRequestInfo)
        let signatureAlgorithm = DER.sequence([DER.oid(OID.ecdsaWithSHA256)])
        let signatureValue = DER.bitString(Array(signature.derRepresentation))

        let der = Data(DER.sequence([certificationRequestInfo, signatureAlgorithm, signatureValue]))
        return (der: der, pem: pem(der: der, label: "CERTIFICATE REQUEST"))
    }
}

/// Wraps DER bytes as PEM: base64, wrapped at 64 columns, between the
/// standard `BEGIN`/`END` lines — shared by the CSR builder above and by
/// `Tests/IndividuatorCoreTests`' throwaway test certificate authority.
func pem(der: Data, label: String) -> String {
    let b64 = der.base64EncodedString()
    var lines: [String] = []
    var i = b64.startIndex
    while i < b64.endIndex {
        let end = b64.index(i, offsetBy: 64, limitedBy: b64.endIndex) ?? b64.endIndex
        lines.append(String(b64[i..<end]))
        i = end
    }
    return "-----BEGIN \(label)-----\n" + lines.joined(separator: "\n") + "\n-----END \(label)-----\n"
}
