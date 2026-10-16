import CryptoKit
import Foundation
import IndividuatorCore

// The Individuator's identity-core CLI (CQ·447 slices 2 and 3). `csr`,
// `adopt-cert` and `status` are slice 2; `self-test` and `fingerprint` are
// slice 3. This executable depends on IndividuatorCore alone, and there is
// still no path from it to the test-only fixtures under `Tests/`.
//
// There is no `keygen` subcommand (2026-10-15, retiring
// `SecureEnclaveMachineKey`): the root daemon creates and self-tests its
// own software machine key on first start, in its own launchd context,
// rather than this CLI generating one in whatever context an operator
// happens to run it from — see `individuator-daemon`'s own entry point and
// `IndividuatorDaemonCore.DaemonKeyStartup`. Every subcommand below loads
// that same key through `IndividuatorPaths.softwareKeyFileName`.

func printErr(_ s: String) {
    FileHandle.standardError.write((s + "\n").data(using: .utf8)!)
}

func usage() -> String {
    """
    usage: individuator <subcommand> [options]

    subcommands:
      csr --machine-name NAME                 build this machine's certificate signing request
        [--key-dir PATH] [--out PATH]
      adopt-cert --cert PATH --root PATH      adopt a certificate the Library issued for this key
        [--key-dir PATH]
      status                                  report the local key and certificate's state
        [--key-dir PATH]
      self-test                                sign, verify, reload the key from its blob, sign again
        [--key-dir PATH]
      fingerprint                              print the SHA-256 fingerprint of the stored public key
        [--key-dir PATH]
    """
}

func option(_ args: [String], _ name: String) -> String? {
    guard let idx = args.firstIndex(of: name), idx + 1 < args.count else { return nil }
    return args[idx + 1]
}

func keyDirectory(from args: [String]) -> URL {
    if let path = option(args, "--key-dir") {
        return URL(fileURLWithPath: path)
    }
    return IndividuatorPaths.defaultKeyDirectory
}

func runCSR(_ args: [String]) -> Int32 {
    guard let machineName = option(args, "--machine-name") else {
        printErr("csr requires --machine-name NAME")
        return 2
    }
    let store = KeyFileStore(directory: keyDirectory(from: args), fileName: IndividuatorPaths.softwareKeyFileName)
    do {
        let key = try SoftwareMachineKey.load(store: store)
        let (_, pem) = try CertificateSigningRequest.build(machineName: machineName, key: key)
        if let outPath = option(args, "--out") {
            try pem.write(toFile: outPath, atomically: true, encoding: .utf8)
            print("wrote CSR for \(machineName) to \(outPath)")
        } else {
            print(pem, terminator: "")
        }
        return 0
    } catch {
        printErr("csr refused: \(error)")
        return 1
    }
}

func runAdoptCert(_ args: [String]) -> Int32 {
    guard let certPath = option(args, "--cert"), let rootPath = option(args, "--root") else {
        printErr("adopt-cert requires --cert PATH --root PATH")
        return 2
    }
    let dir = keyDirectory(from: args)
    let keyStore = KeyFileStore(directory: dir, fileName: IndividuatorPaths.softwareKeyFileName)
    do {
        let key = try SoftwareMachineKey.load(store: keyStore)
        let certPEM = try String(contentsOfFile: certPath, encoding: .utf8)
        let rootPEM = try String(contentsOfFile: rootPath, encoding: .utf8)
        let adopted = try CertificateAdoption.adopt(certificatePEM: certPEM, rootPEM: rootPEM, machineKey: key)
        let certStore = KeyFileStore(directory: dir, fileName: IndividuatorPaths.certificateFileName)
        try certStore.validateDirectory()
        try certStore.write(Data(adopted.certificatePEM.utf8))
        print("adopted certificate for \(adopted.uriName) at \(certStore.fileURL.path)")
        return 0
    } catch {
        printErr("adopt-cert refused: \(error)")
        return 1
    }
}

func runStatus(_ args: [String]) -> Int32 {
    let dir = keyDirectory(from: args)
    let keyStore = KeyFileStore(directory: dir, fileName: IndividuatorPaths.softwareKeyFileName)
    let certStore = KeyFileStore(directory: dir, fileName: IndividuatorPaths.certificateFileName)
    var ok = true

    do {
        try keyStore.validateFile()
        print("key: present, custody checks pass, at \(keyStore.fileURL.path)")
    } catch {
        print("key: \(error)")
        ok = false
    }

    do {
        try certStore.validateFile()
        let certPEM = try String(contentsOf: certStore.fileURL, encoding: .utf8)
        let uris = try CertificateInspection.uriNames(ofCertificatePEM: certPEM)
        print("certificate: present, custody checks pass, at \(certStore.fileURL.path), names \(uris)")
    } catch {
        print("certificate: \(error)")
    }

    return ok ? 0 : 1
}

/// A manual diagnostic, run by hand against the key the daemon already
/// created: signs a fixed message, verifies the signature against the
/// key's own public key, RELOADS the key fresh from its on-disk blob
/// (closing over whatever bug class a signature that only ever used the
/// in-memory key could hide — a blob that failed to persist correctly, or
/// one `P256.Signing.PrivateKey(rawRepresentation:)` cannot reconstitute),
/// and signs once more. The daemon itself runs this same self-test, in its
/// own launchd context, on every start — see
/// `IndividuatorDaemonCore.DaemonKeyStartup`; this subcommand exists only
/// for an operator to re-run it by hand. Exits non-zero at the first step
/// that fails, naming which one.
func runSelfTest(_ args: [String]) -> Int32 {
    let store = KeyFileStore(directory: keyDirectory(from: args), fileName: IndividuatorPaths.softwareKeyFileName)
    let message = Data("individuator self-test".utf8)

    let key: SoftwareMachineKey
    do {
        key = try SoftwareMachineKey.load(store: store)
    } catch {
        printErr("self-test refused: could not load the key: \(error)")
        return 1
    }

    do {
        let signature = try key.signature(for: message)
        guard key.publicKey.isValidSignature(signature, for: message) else {
            printErr("self-test refused: the first signature did not verify")
            return 1
        }
    } catch {
        printErr("self-test refused: could not sign with the loaded key: \(error)")
        return 1
    }

    let reloaded: SoftwareMachineKey
    do {
        reloaded = try SoftwareMachineKey.load(store: store)
    } catch {
        printErr("self-test refused: could not reload the key from its blob: \(error)")
        return 1
    }

    do {
        let signature = try reloaded.signature(for: message)
        guard reloaded.publicKey.isValidSignature(signature, for: message) else {
            printErr("self-test refused: the reloaded key's signature did not verify")
            return 1
        }
    } catch {
        printErr("self-test refused: could not sign with the reloaded key: \(error)")
        return 1
    }

    print("self-test passed: signed, verified, reloaded the key from its blob, signed and verified again")
    return 0
}

func runFingerprint(_ args: [String]) -> Int32 {
    let store = KeyFileStore(directory: keyDirectory(from: args), fileName: IndividuatorPaths.softwareKeyFileName)
    do {
        let key = try SoftwareMachineKey.load(store: store)
        print(CertificateSigningRequest.publicKeyFingerprintHex(key.publicKey))
        return 0
    } catch {
        printErr("fingerprint refused: \(error)")
        return 1
    }
}

let allArgs = Array(CommandLine.arguments.dropFirst())
guard let subcommand = allArgs.first else {
    printErr(usage())
    exit(2)
}
let rest = Array(allArgs.dropFirst())

let exitCode: Int32
switch subcommand {
case "csr": exitCode = runCSR(rest)
case "adopt-cert": exitCode = runAdoptCert(rest)
case "status": exitCode = runStatus(rest)
case "self-test": exitCode = runSelfTest(rest)
case "fingerprint": exitCode = runFingerprint(rest)
case "-h", "--help", "help":
    print(usage())
    exit(0)
default:
    printErr("unknown subcommand: \(subcommand)\n")
    printErr(usage())
    exit(2)
}
exit(exitCode)
