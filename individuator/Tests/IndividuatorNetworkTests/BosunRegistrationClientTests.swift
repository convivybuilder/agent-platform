import Testing
@testable import IndividuatorCore
@testable import IndividuatorNetwork

/// Unit coverage for `BosunRegistrationClient.preferCapturedRefusal` — no
/// socket, no TLS, no `RegistrationTransport` — the round-4 fix for a
/// defect the integration test (which needs a real socket, and so only
/// runs in CI) found: once the TLS verification callback has refused the
/// Bosun's certificate, the generic error NIO raises afterward tearing
/// down the still-failing handshake must never read as some OTHER,
/// uninformative transport failure.
private struct _DummyTransportError: Error, CustomStringConvertible {
    var description: String { "dummy transport failure" }
}

@Suite struct BosunRegistrationClientTests {
    @Test func prefersTheCapturedRefusalOverWhateverNIORaisesAfterward() {
        let refusal = BosunServerRefusal(reason: .missingServerAuth)
        let result = BosunRegistrationClient.preferCapturedRefusal(refusal, over: _DummyTransportError())

        let serverCertificateRefused: BosunServerRefusal? = result as? BosunServerRefusal
        #expect(serverCertificateRefused == refusal)
    }

    @Test func fallsBackToTheOriginalErrorWhenNoRefusalWasCaptured() {
        let original = _DummyTransportError()
        let result = BosunRegistrationClient.preferCapturedRefusal(nil, over: original)

        #expect(result is _DummyTransportError)
    }

    /// The outer mapping `BosunRegistrationClient.register` itself applies
    /// (`catch let refusal as BosunServerRefusal`) turns the preferred
    /// refusal into `.serverCertificateRefused` with its reason, and never
    /// `.transportError` — the shape the integration test's three refusal
    /// cases assert on.
    @Test func theRefusalRegisterCatchesBecomesAServerCertificateRefusalOutcomeNeverATransportError() {
        let refusal = BosunServerRefusal(reason: .uriNameMismatch(expected: "convivy://machine/bosun-test", found: "convivy://machine/some-other-host"))
        let error = BosunRegistrationClient.preferCapturedRefusal(refusal, over: _DummyTransportError())

        let outcome: RegistrationOutcome
        do {
            throw error
        } catch let caught as BosunServerRefusal {
            outcome = .serverCertificateRefused(caught.reason)
        } catch {
            outcome = .transportError(detail: "\(type(of: error)): \(error)")
        }

        #expect(outcome == .serverCertificateRefused(refusal.reason))
    }
}
