import Foundation

/// A small public, read-only window onto the internal `CertificateExtensions`
/// reader, for callers outside this module — the `individuator status`
/// subcommand in particular — that want to report what a certificate says
/// without redoing adoption's checks.
public enum CertificateInspection {
    public static func uriNames(ofCertificatePEM pem: String) throws -> [String] {
        let der = try derFromPEM(pem)
        let extensions = try CertificateExtensions.extensions(ofCertificateDER: Array(der))
        guard let sanValue = extensions[OID.subjectAltName] else { return [] }
        return try CertificateExtensions.uriNames(sanExtnValue: sanValue)
    }
}
