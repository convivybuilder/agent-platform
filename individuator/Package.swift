// swift-tools-version:5.9
import PackageDescription

// The Individuator's identity core and mutual-TLS client (platform/decisions/0092;
// CQ·447 slices 2 and 3).
//
// Targets are laid out so the shipped daemon and CLI have no possible import
// path to a test-only key, and so the one external dependency (swift-nio-ssl,
// D1 below) reaches only the networking targets that need it:
//
//   IndividuatorCore     — the identity core: MachineKey, SecureEnclaveMachineKey,
//                           the CSR builder, certificate adoption, the Bosun
//                           server-certificate check, and the root-owned config
//                           reader. No test key type lives here, and no external
//                           dependency: every certificate/CSR primitive is built
//                           from Apple's own Security/CryptoKit plus this
//                           package's own DER encoder.
//   IndividuatorNetwork  — the mutual-TLS registration client. Depends on
//                           swift-nio-ssl (D1) to present the Secure Enclave key
//                           at the TLS handshake through `NIOSSLCustomPrivateKey`
//                           without ever exporting it from the enclave or the
//                           Keychain.
//
//   D1 (operator, 2026-10-12): before adding swift-nio-ssl, this slice checked
//   whether `Network.framework`'s public API can present a Secure Enclave key
//   that was never stored in the Keychain. It cannot: every public route to
//   hand Network.framework a client identity (`sec_identity_t`) goes through
//   `SecIdentityCreateWithCertificate`, which resolves the private key by
//   searching the Keychain for a `SecKey` paired with the certificate — there
//   is no public API that accepts a bare `SecureEnclave.P256.Signing.PrivateKey`
//   (a CryptoKit value, never a Keychain item here — see
//   `SecureEnclaveMachineKey.swift`) as the handshake signer. So this slice
//   takes D1's fallback and pins swift-nio-ssl, exactly one version, below.
//
//   IndividuatorDaemonCore — the root daemon's logic: the Unix-socket peer
//                           check (`getpeereid`) and the single `register`
//                           operation it dispatches to. No product target
//                           exposes a second operation that signs
//                           caller-supplied bytes.
//   individuator         — the identity-core CLI (`keygen`/`csr`/`adopt-cert`/
//                           `status`). Depends on IndividuatorCore alone.
//   individuator-daemon  — the root LaunchDaemon executable. Depends on
//                           IndividuatorDaemonCore (and, transitively,
//                           IndividuatorNetwork).
//   IndividuatorCoreTests, IndividuatorNetworkTests — test targets. The
//                           latter depends on IndividuatorCoreTests (itself a
//                           target, not a product) purely to reuse its
//                           SoftwareMachineKey/TestCertificateAuthority test
//                           fixtures — a test-target-to-test-target
//                           dependency SwiftPM allows precisely for this,
//                           and still no product target can ever reach them.
let package = Package(
    name: "individuator",
    platforms: [.macOS(.v13)],
    products: [
        .executable(name: "individuator", targets: ["individuator"]),
        .executable(name: "individuator-daemon", targets: ["individuator-daemon"]),
        .library(name: "IndividuatorCore", targets: ["IndividuatorCore"]),
        .library(name: "IndividuatorNetwork", targets: ["IndividuatorNetwork"]),
        .library(name: "IndividuatorDaemonCore", targets: ["IndividuatorDaemonCore"]),
    ],
    dependencies: [
        .package(url: "https://github.com/apple/swift-nio-ssl.git", exact: "2.37.5"),
        // swift-nio-ssl 2.37.5 depends on swift-nio >= 2.98.0; pinned exactly
        // here too (D1), since IndividuatorNetwork imports NIOCore/NIOPosix
        // directly rather than only transitively through NIOSSL.
        .package(url: "https://github.com/apple/swift-nio.git", exact: "2.104.0"),
    ],
    targets: [
        .target(name: "IndividuatorCore"),
        .target(
            name: "IndividuatorNetwork",
            dependencies: [
                "IndividuatorCore",
                .product(name: "NIOSSL", package: "swift-nio-ssl"),
                .product(name: "NIOPosix", package: "swift-nio"),
                .product(name: "NIOCore", package: "swift-nio"),
            ]
        ),
        .target(name: "IndividuatorDaemonCore", dependencies: ["IndividuatorCore", "IndividuatorNetwork"]),
        .executableTarget(name: "individuator", dependencies: ["IndividuatorCore"]),
        .executableTarget(name: "individuator-daemon", dependencies: ["IndividuatorDaemonCore"]),
        .testTarget(name: "IndividuatorCoreTests", dependencies: ["IndividuatorCore"]),
        .testTarget(
            name: "IndividuatorNetworkTests",
            dependencies: ["IndividuatorCore", "IndividuatorNetwork", "IndividuatorDaemonCore", "IndividuatorCoreTests"]
        ),
    ]
)
