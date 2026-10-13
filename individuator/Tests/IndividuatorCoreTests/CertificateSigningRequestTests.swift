import CryptoKit
import Foundation
import Testing
@testable import IndividuatorCore

/// Parses just enough of a PKCS#10 CSR, independently of
/// `CertificateSigningRequest`'s own builder, to check what it produced:
/// the self-signature, and the extensionRequest attribute's SAN/EKU.
private enum CSRReader {
    struct Parsed {
        let certificationRequestInfoRaw: [UInt8]
        let signature: P256.Signing.ECDSASignature
        let uriNames: [String]
        let keyPurposeOIDs: [String]
    }

    static func parse(_ der: Data) throws -> Parsed {
        let bytes = Array(der)
        let csr = try ASN1.parseOne(bytes, at: 0).node
        let top = try ASN1.children(of: csr)
        let certReqInfoNode = top[0]
        let sigValueNode = top[2]

        let raw = DER.tlv(certReqInfoNode.tag, certReqInfoNode.content)
        let sigBytes = Array(sigValueNode.content.dropFirst()) // drop the unused-bits byte
        let signature = try P256.Signing.ECDSASignature(derRepresentation: Data(sigBytes))

        let infoFields = try ASN1.children(of: certReqInfoNode)
        let attributesField = infoFields[3] // [0] attributes
        let attributeNodes = try ASN1.children(of: attributesField)
        var uriNames: [String] = []
        var keyPurposeOIDs: [String] = []
        for attribute in attributeNodes {
            let attrFields = try ASN1.children(of: attribute)
            guard ASN1.oidString(attrFields[0].content) == "1.2.840.113549.1.9.14" else { continue }
            let valuesSet = try ASN1.children(of: attrFields[1])
            let extensionsSeq = try ASN1.children(of: valuesSet[0])
            for ext in extensionsSeq {
                let extFields = try ASN1.children(of: ext)
                let oid = ASN1.oidString(extFields[0].content)
                let valueNode = extFields.dropFirst().first(where: { $0.tag == 0x04 })!
                if oid == "2.5.29.17" {
                    let generalNames = try ASN1.children(of: ASN1.parseOne(valueNode.content, at: 0).node)
                    uriNames = generalNames.filter { $0.tag == 0x86 }.map { String(decoding: $0.content, as: UTF8.self) }
                } else if oid == "2.5.29.37" {
                    let purposes = try ASN1.children(of: ASN1.parseOne(valueNode.content, at: 0).node)
                    keyPurposeOIDs = purposes.map { ASN1.oidString($0.content) }
                }
            }
        }
        return Parsed(certificationRequestInfoRaw: raw, signature: signature,
                       uriNames: uriNames, keyPurposeOIDs: keyPurposeOIDs)
    }
}

@Suite struct CertificateSigningRequestTests {
    @Test func csrSelfSignatureVerifiesAndCarriesExactlyOneClientAuthURISAN() throws {
        let key = SoftwareMachineKey()
        let (der, pemText) = try CertificateSigningRequest.build(machineName: "synthetic-mac-1", key: key)

        #expect(pemText.hasPrefix("-----BEGIN CERTIFICATE REQUEST-----"))

        let parsed = try CSRReader.parse(der)
        #expect(key.publicKey.isValidSignature(parsed.signature, for: Data(parsed.certificationRequestInfoRaw)))
        #expect(parsed.uriNames == ["convivy://machine/synthetic-mac-1"])
        #expect(parsed.keyPurposeOIDs == [OID.clientAuth])
    }

    @Test func buildRefusesAnInvalidMachineName() throws {
        let key = SoftwareMachineKey()
        #expect(throws: MachineNameError.self) {
            _ = try CertificateSigningRequest.build(machineName: "Not Valid!", key: key)
        }
    }

    // Manually verified (not automated, since it would require shipping a
    // second, intentionally-broken builder): commenting out the
    // `try MachineName.validate` call in `CertificateSigningRequest.build`
    // and rerunning `buildRefusesAnInvalidMachineName` turns it red, and
    // flipping the signature check in `csrSelfSignatureVerifies...` to sign
    // over the wrong bytes (the raw DER rather than `certReqInfoNode`'s
    // re-encoding) turns that assertion red too.
}
