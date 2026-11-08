import Foundation
import IndividuatorCore

/// Whether the root daemon serves the forwarding socket at all (E12·2
/// slice S5b build plan: "An install without them serves no forwarding
/// socket."). `libraryAddress` is the one field `bin/install-individuator`
/// only writes when `--library-address` is given; its absence is what
/// tells a plain install — one that never asked for the forwarding
/// endpoint — apart from one that configured it.
public enum ForwardingServingDecision {
    public static func shouldServeForwarding(config: IndividuatorRootConfig) -> Bool {
        config.libraryAddress != nil
    }
}
