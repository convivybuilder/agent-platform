import Foundation

/// Strips the request headers that name a client's identity or certificate
/// before a forwarded request leaves the Individuator (0092: "The
/// Individuator strips every request header that names a client identity
/// or a certificate, so the Keel identifies the host from the handshake
/// alone."; Handoff: "Headers naming a client identity or certificate
/// (`X-Client-Cert` and any `X-Forwarded-*`) are stripped."). Every other
/// header, `Authorization` included, passes through unchanged and in its
/// original order.
public enum ForwardingHeaderStripping {
    public static func strip(_ headerFields: [ForwardingHeaderField]) -> [ForwardingHeaderField] {
        headerFields.filter { !isStripped($0.name) }
    }

    private static func isStripped(_ name: String) -> Bool {
        let lowered = name.lowercased()
        return lowered == "x-client-cert" || lowered.hasPrefix("x-forwarded-")
    }
}
