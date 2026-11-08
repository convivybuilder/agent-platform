import Testing
@testable import IndividuatorDaemonCore

/// `ForwardingHeaderStripping` (0092: "Headers naming a client identity or
/// certificate (`X-Client-Cert` and any `X-Forwarded-*`) are stripped.").
@Suite struct ForwardingHeaderStrippingTests {
    private static func field(_ name: String, _ value: String) -> ForwardingHeaderField {
        ForwardingHeaderField(name: name, value: value)
    }

    @Test func stripsXClientCertCaseInsensitively() {
        let stripped = ForwardingHeaderStripping.strip([
            Self.field("x-Client-CERT", "deadbeef"), Self.field("Authorization", "Bearer tok"),
        ])
        #expect(stripped == [Self.field("Authorization", "Bearer tok")])
    }

    @Test func stripsEveryXForwardedHeaderCaseInsensitively() {
        let stripped = ForwardingHeaderStripping.strip([
            Self.field("X-Forwarded-For", "10.0.0.1"), Self.field("x-forwarded-proto", "https"),
            Self.field("X-FORWARDED-HOST", "keel.example"), Self.field("Authorization", "Bearer tok"),
        ])
        #expect(stripped == [Self.field("Authorization", "Bearer tok")])
    }

    @Test func keepsAuthorizationAndEveryOtherHeaderInOrder() {
        let headers = [
            Self.field("Authorization", "Bearer tok"), Self.field("Content-Type", "application/json"),
            Self.field("X-Session-Token", "sess-123"),
        ]
        #expect(ForwardingHeaderStripping.strip(headers) == headers)
    }

    /// Only a header whose name STARTS WITH `x-forwarded-` is stripped — a
    /// name that merely contains "forwarded" elsewhere is kept.
    @Test func keepsAHeaderThatOnlyContainsForwardedAsASubstring() {
        let headers = [Self.field("X-Was-Forwarded-By-Someone-Else", "value")]
        #expect(ForwardingHeaderStripping.strip(headers) == headers)
    }
}
