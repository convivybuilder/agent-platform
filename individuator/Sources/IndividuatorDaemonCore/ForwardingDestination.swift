import Foundation

/// Which Keel service an allowlisted forwarding route is dialled against
/// (0092 Handoff "Forwarding endpoint": "Each route's destination (Library
/// or Bosun address) comes from the configuration, never from the
/// request."). Resolved to an address by `ForwardingConnectionHandler`
/// against `IndividuatorRootConfig`: `.library` to `libraryAddress`,
/// `.bosun` to `bosunAddress`. Both destinations are checked against the
/// same `bosunURIName` at the TLS handshake — the Bosun presents the
/// Library host's own server certificate (0092 section 6), so one Keel
/// name covers both.
public enum ForwardingDestination: Equatable {
    case library
    case bosun
}

/// One entry of the forwarding endpoint's fixed allowlist: a route
/// (`ForwardingRequestMatcher`'s own concern — method and path template)
/// paired with where a request matching it is dialled. `ForwardingRoute`
/// itself (slice S5a) carries no destination of its own; this is the one
/// place the two are joined.
public struct ForwardingRouteEntry: Equatable {
    public let route: ForwardingRoute
    public let destination: ForwardingDestination

    public init(route: ForwardingRoute, destination: ForwardingDestination) {
        self.route = route
        self.destination = destination
    }
}
