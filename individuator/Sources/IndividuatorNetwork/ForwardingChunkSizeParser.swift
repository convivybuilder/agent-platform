/// Parses one chunked-transfer chunk-size line's size, the one strict way
/// both `ForwardingHTTPRequestReader` (`IndividuatorDaemonCore`, the
/// request side) and `ForwardingUpstreamResponseReassembler` (this target,
/// the response side) read it. Defined here, alongside
/// `ForwardingSizeLimits`, for the same reason that type is: the request
/// side already depends on this target and not the other way around.
///
/// Round 3 review finding: `Int(_:radix: 16)` accepts a leading `-` (and
/// `+`) as part of Swift's own numeric grammar. A chunk size of
/// `-100000` passed the `size <= maxBodyBytes` cap (a large negative
/// number is still `<=` the cap) and every overflow-checked sum downstream
/// (a negative sum does not overflow), reaching `bytes[chunkEnd]` with a
/// negative index and trapping the root daemon. Any Keel response, or any
/// admitted local client, could carry one.
///
/// `parse` accepts only 1 to 16 plain hex digits (`0`-`9`, `a`-`f`, `A`-`F`)
/// before an optional chunk extension starting with `;`, and refuses a
/// sign, whitespace anywhere before or inside the digits, a `0x` prefix, an
/// empty size, more than 16 digits, and any value over
/// `ForwardingSizeLimits.maxBodyBytes`. It never trims or otherwise
/// tolerates a byte the digits grammar does not call out.
public enum ForwardingChunkSizeParser {
    public enum Result: Equatable {
        case size(Int)
        case malformed(detail: String)
    }

    /// `sizeLine` is one chunk-size line's bytes, already split off its own
    /// terminating CRLF by the caller, exactly as both
    /// `ForwardingHTTPRequestReader.readChunkedBody` and
    /// `ForwardingUpstreamResponseReassembler.chunkedBodyEnd` already split
    /// it before this line ever meets a parser.
    public static func parse(_ sizeLine: [UInt8]) -> Result {
        let digits: ArraySlice<UInt8>
        if let semicolonIndex = sizeLine.firstIndex(of: UInt8(ascii: ";")) {
            digits = sizeLine[sizeLine.startIndex..<semicolonIndex]
        } else {
            digits = sizeLine[...]
        }
        guard !digits.isEmpty else {
            return .malformed(detail: "a chunk-size line's size was empty")
        }
        guard digits.count <= 16 else {
            return .malformed(detail: "a chunk-size line's size carried more than 16 hex digits")
        }

        var value = 0
        for byte in digits {
            guard let digit = hexDigitValue(byte) else {
                return .malformed(detail: "a chunk-size line's size was not a plain hex number")
            }
            let (multiplied, multiplyOverflowed) = value.multipliedReportingOverflow(by: 16)
            let (added, addOverflowed) = multiplied.addingReportingOverflow(digit)
            guard !multiplyOverflowed, !addOverflowed else {
                return .malformed(detail: "a chunk-size line's size overflowed")
            }
            value = added
        }

        guard value <= ForwardingSizeLimits.maxBodyBytes else {
            return .malformed(
                detail: "a chunk size of \(value) bytes exceeds the maximum body size of \(ForwardingSizeLimits.maxBodyBytes) bytes")
        }
        return .size(value)
    }

    private static func hexDigitValue(_ byte: UInt8) -> Int? {
        switch byte {
        case UInt8(ascii: "0")...UInt8(ascii: "9"): return Int(byte - UInt8(ascii: "0"))
        case UInt8(ascii: "a")...UInt8(ascii: "f"): return Int(byte - UInt8(ascii: "a")) + 10
        case UInt8(ascii: "A")...UInt8(ascii: "F"): return Int(byte - UInt8(ascii: "A")) + 10
        default: return nil
        }
    }
}
