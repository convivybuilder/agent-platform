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

let keyStore = KeyFileStore(directory: keyDir, fileName: IndividuatorPaths.keyFileName)
let certStore = KeyFileStore(directory: keyDir, fileName: IndividuatorPaths.certificateFileName)

let machineKey: SecureEnclaveMachineKey
do {
    machineKey = try SecureEnclaveMachineKey.load(store: keyStore)
} catch {
    fail("individuator-daemon: could not load the machine key: \(error)")
}

let certificatePEM: String
do {
    try certStore.validateFile()
    certificatePEM = try String(contentsOf: certStore.fileURL, encoding: .utf8)
} catch {
    fail("individuator-daemon: could not load the adopted certificate: \(error)")
}

let transport = BosunRegistrationClient(
    config: config, machineKey: machineKey, clientCertificatePEM: certificatePEM)
let account = accountName(for: config.operatorUID)
let handler = RegisterRequestHandler(requiredUID: config.operatorUID, transport: transport, account: account)
let server = RegisterSocketServer(socketPath: socketPath, handler: handler)

do {
    try server.run()
} catch {
    fail("individuator-daemon: \(error)")
}
