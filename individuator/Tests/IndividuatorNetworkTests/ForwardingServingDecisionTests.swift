import Testing
@testable import IndividuatorCore
@testable import IndividuatorDaemonCore

/// `ForwardingServingDecision` (build plan slice S5b: "An install without
/// them serves no forwarding socket.").
@Suite struct ForwardingServingDecisionTests {
    private static func config(libraryAddress: String?) -> IndividuatorRootConfig {
        IndividuatorRootConfig(
            bosunHost: "bosun-test", bosunURIName: "convivy://machine/bosun-test",
            bosunAddress: "127.0.0.1:1", rootCAPath: "/dev/null",
            admittedAccounts: [AdmittedAccount(uid: 0, account: "operator")], libraryAddress: libraryAddress)
    }

    @Test func servesWhenALibraryAddressIsConfigured() {
        #expect(ForwardingServingDecision.shouldServeForwarding(config: Self.config(libraryAddress: "127.0.0.1:2")))
    }

    @Test func doesNotServeWhenNoLibraryAddressIsConfigured() {
        #expect(!ForwardingServingDecision.shouldServeForwarding(config: Self.config(libraryAddress: nil)))
    }
}
