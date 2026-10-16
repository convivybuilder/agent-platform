import Foundation
import IndividuatorCore
import IndividuatorDaemonCore
import IndividuatorNetwork
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif

// The Individuator's root LaunchDaemon (platform/decisions/0092; CQ·447
// slice 3). Thin entry point only — every decision this process makes
// (the peer check, the Bosun's certificate checks, the one `register`
// operation) lives in IndividuatorCore/IndividuatorNetwork/
// IndividuatorDaemonCore, tested there without ever running this main as
// root or touching a real Secure Enclave key.

// launchd's StandardOutPath sends stdout to a plain file, where C stdio is
// fully buffered rather than line-buffered, so `print(keyStartup.logLine)`
// below would sit in libc's buffer for as long as this daemon keeps serving
// — which is forever — and never reach the file install-individuator polls
// for it in. Line-buffer stdout before any other write reaches it, so every
// line this process prints lands in the log file at once. (Nothing here
// writes to stderr through stdio — `fail` below calls `FileHandle.write`
// directly, which is an unbuffered `write(2)` — so stderr needs no such
// call.)
setvbuf(stdout, nil, _IOLBF, 0)

func fail(_ message: String) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(1)
}

func accountName(for uid: uid_t) -> String {
    guard let pw = getpwuid(uid) else {
        fail("individuator-daemon: no password-database entry for operator uid \(uid)")
    }
    return String(cString: pw.pointee.pw_name)
}

func option(_ args: [String], _ name: String) -> String? {
    guard let idx = args.firstIndex(of: name), idx + 1 < args.count else { return nil }
    return args[idx + 1]
}

let args = Array(CommandLine.arguments.dropFirst())
let configPath = option(args, "--config").map { URL(fileURLWithPath: $0) } ?? IndividuatorPaths.defaultRootConfigPath
let keyDir = option(args, "--key-dir").map { URL(fileURLWithPath: $0) } ?? IndividuatorPaths.defaultKeyDirectory
let socketPath = option(args, "--socket") ?? IndividuatorPaths.defaultDaemonSocketPath.path

let config: IndividuatorRootConfig
do {
    config = try IndividuatorRootConfig.load(from: configPath)
} catch {
    fail("individuator-daemon: \(error)")
}

let keyStore = KeyFileStore(directory: keyDir, fileName: IndividuatorPaths.softwareKeyFileName)
let certStore = KeyFileStore(directory: keyDir, fileName: IndividuatorPaths.certificateFileName)

// Creates the software machine key on first start (never regenerating one
// already there) and self-tests it, IN THIS PROCESS, in whatever context
// launchd actually runs this daemon — 2026-10-15: the installer's own
// `sudo individuator self-test` used to pass inside the operator's login
// session, a context this daemon never runs in, and told nobody whether
// the same signing operation would succeed here. A failed self-test logs
// why and exits non-zero rather than ever serving the socket with a key
// nobody has proven can sign.
let keyStartup: DaemonKeyStartupResult
do {
    keyStartup = try DaemonKeyStartup.prepareKey(store: keyStore, effectiveUID: geteuid())
} catch {
    fail("individuator-daemon: \(error)")
}
print(keyStartup.logLine)

// No certificate check here: this daemon must be able to start and serve
// its socket before one exists, because the certificate request (CLI
// `csr`, run by the operator under sudo) needs the key this process just
// created. `CertificateGatedRegistrationTransport` reads the certificate
// fresh on every `register` call and refuses that one request, under its
// own named reason, until a certificate has been adopted.
let transport = CertificateGatedRegistrationTransport(
    config: config, machineKey: keyStartup.key, certificateStore: certStore)
let account = accountName(for: config.operatorUID)
let handler = RegisterRequestHandler(requiredUID: config.operatorUID, transport: transport, account: account)
let server = RegisterSocketServer(socketPath: socketPath, handler: handler)

do {
    try server.run()
} catch {
    fail("individuator-daemon: \(error)")
}
