import Testing
@testable import IndividuatorNetwork

/// `response`'s index right after `landmark`'s last occurrence — used
/// below to name a split point by what it falls inside ("mid status line",
/// "mid header") rather than by a hand-counted byte offset.
private func indexAfter(_ landmark: String, in response: [UInt8]) -> Int {
    let needle = Array(landmark.utf8)
    precondition(!needle.isEmpty)
    var i = 0
    while i + needle.count <= response.count {
        if Array(response[i..<(i + needle.count)]) == needle {
            return i + needle.count
        }
        i += 1
    }
    preconditionFailure("'\(landmark)' does not occur in the response fixture")
}

/// Feeds `response`'s bytes to a fresh reassembler in two pieces, split
/// right after `splitAfter` bytes, asserting `.needsMoreBytes` on the
/// first piece and `.complete(responseBytes: response)` on the second.
private func assertReassemblesWhenSplit(_ response: [UInt8], splitAfter: Int) {
    precondition(splitAfter > 0 && splitAfter < response.count)
    var reassembler = ForwardingUpstreamResponseReassembler()
    #expect(reassembler.feed(Array(response[0..<splitAfter])) == .needsMoreBytes)
    #expect(reassembler.feed(Array(response[splitAfter...])) == .complete(responseBytes: response))
}

/// Feeds `response`'s bytes to a fresh reassembler one byte at a time — the
/// most awkward split there is, since every single byte boundary lands mid
/// status-line, mid header name, mid header value, mid CRLF, mid chunk-size
/// line, or mid body at some point in the sequence.
private func assertReassemblesOneByteAtATime(_ response: [UInt8]) {
    var reassembler = ForwardingUpstreamResponseReassembler()
    for (index, byte) in response.enumerated() {
        let outcome = reassembler.feed([byte])
        if index == response.count - 1 {
            #expect(outcome == .complete(responseBytes: response))
        } else {
            #expect(outcome == .needsMoreBytes)
        }
    }
}

@Suite struct ForwardingUpstreamResponseReassemblerTests {

    @Test func reassemblesAContentLengthResponseSplitInsideTheStatusLine() {
        let response = Array("HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok".utf8)
        assertReassemblesWhenSplit(response, splitAfter: indexAfter("HTTP/1.1 2", in: response))
    }

    @Test func reassemblesAContentLengthResponseSplitInsideAHeader() {
        let response = Array("HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok".utf8)
        assertReassemblesWhenSplit(response, splitAfter: indexAfter("Content-Len", in: response))
    }

    @Test func reassemblesAContentLengthResponseSplitBetweenHeadAndBody() {
        let response = Array("HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok".utf8)
        assertReassemblesWhenSplit(response, splitAfter: indexAfter("Content-Length: 2\r\n\r\n", in: response))
    }

    @Test func reassemblesAContentLengthResponseSplitAcrossManyAwkwardByteBoundaries() {
        let response = Array(
            "HTTP/1.1 200 OK\r\nContent-Length: 11\r\nX-Trace: a\r\n\r\nhello world".utf8)
        assertReassemblesOneByteAtATime(response)
    }

    @Test func reassemblesAChunkedResponseSplitInsideTheStatusLine() {
        let response = Array(
            "HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n4\r\nWiki\r\n5\r\npedia\r\n0\r\n\r\n".utf8)
        assertReassemblesWhenSplit(response, splitAfter: indexAfter("HTTP/1.1 2", in: response))
    }

    @Test func reassemblesAChunkedResponseSplitInsideAHeader() {
        let response = Array(
            "HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n4\r\nWiki\r\n5\r\npedia\r\n0\r\n\r\n".utf8)
        assertReassemblesWhenSplit(response, splitAfter: indexAfter("Transfer-Enc", in: response))
    }

    @Test func reassemblesAChunkedResponseSplitInsideAChunkSizeLine() {
        let response = Array(
            "HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n4\r\nWiki\r\n5\r\npedia\r\n0\r\n\r\n".utf8)
        // Splits right inside the second chunk's own size line ("5\r\n").
        assertReassemblesWhenSplit(response, splitAfter: indexAfter("Wiki\r\n5", in: response))
    }

    @Test func reassemblesAChunkedResponseSplitInsideChunkData() {
        let response = Array(
            "HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n4\r\nWiki\r\n5\r\npedia\r\n0\r\n\r\n".utf8)
        // Splits inside "pedia"'s own chunk data.
        assertReassemblesWhenSplit(response, splitAfter: indexAfter("5\r\nped", in: response))
    }

    @Test func reassemblesAChunkedResponseSplitRightAtTheFinalChunkBoundary() {
        let response = Array(
            "HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n4\r\nWiki\r\n5\r\npedia\r\n0\r\n\r\n".utf8)
        assertReassemblesWhenSplit(response, splitAfter: indexAfter("pedia\r\n", in: response))
    }

    @Test func reassemblesAChunkedResponseSplitAcrossManyAwkwardByteBoundaries() {
        let response = Array(
            "HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n4\r\nWiki\r\n5\r\npedia\r\n0\r\n\r\n".utf8)
        assertReassemblesOneByteAtATime(response)
    }

    @Test func connectionCloseFramedResponseCompletesOnlyWhenTheConnectionCloses() {
        let response = Array("HTTP/1.1 200 OK\r\nX-Trace: a\r\n\r\nhello, no content-length here".utf8)
        var reassembler = ForwardingUpstreamResponseReassembler()
        var offset = 0
        while offset < response.count {
            let end = min(offset + 7, response.count)
            #expect(reassembler.feed(Array(response[offset..<end])) == .needsMoreBytes)
            offset = end
        }
        #expect(reassembler.connectionClosed() == .complete(responseBytes: response))
    }

    @Test func closingBeforeADeclaredContentLengthBodyArrivesIsMalformed() {
        var reassembler = ForwardingUpstreamResponseReassembler()
        #expect(
            reassembler.feed(Array("HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\nabc".utf8))
                == .needsMoreBytes)
        guard case .malformed = reassembler.connectionClosed() else {
            Issue.record("expected a malformed close outcome")
            return
        }
    }

    @Test func closingBeforeTheHeadCompletesIsMalformed() {
        var reassembler = ForwardingUpstreamResponseReassembler()
        #expect(reassembler.feed(Array("HTTP/1.1 200 OK\r\nContent-Len".utf8)) == .needsMoreBytes)
        guard case .malformed(let detail) = reassembler.connectionClosed() else {
            Issue.record("expected a malformed close outcome")
            return
        }
        #expect(detail == "upstream closed the connection before sending a response (28 bytes received)")
    }

    @Test func closingWithNoBytesAtAllIsReportedAsClosedBeforeAnyResponse() {
        let reassembler = ForwardingUpstreamResponseReassembler()
        guard case .malformed(let detail) = reassembler.connectionClosed() else {
            Issue.record("expected a malformed close outcome")
            return
        }
        #expect(detail == "upstream closed the connection before sending a response (0 bytes received)")
    }

    @Test func bothTransferEncodingAndContentLengthIsMalformed() {
        var reassembler = ForwardingUpstreamResponseReassembler()
        let outcome = reassembler.feed(
            Array("HTTP/1.1 200 OK\r\nContent-Length: 2\r\nTransfer-Encoding: chunked\r\n\r\n".utf8))
        guard case .malformed = outcome else {
            Issue.record("expected a malformed outcome, got \(outcome)")
            return
        }
    }

    // MARK: - size caps and overflow (round 2 finding)
    //
    // Each of these declares a length Swift's trapping `+` would have
    // turned into a crashed daemon process before this round's fix: a
    // Content-Length or chunk size near `Int.max` reaching `headEnd +
    // bodyLength` or `dataStart + size` directly. Every case here must
    // come back `.malformed` rather than trap.

    @Test func aContentLengthOfIntMaxIsMalformedRatherThanTrapping() {
        var reassembler = ForwardingUpstreamResponseReassembler()
        let outcome = reassembler.feed(
            Array("HTTP/1.1 200 OK\r\nContent-Length: \(Int.max)\r\n\r\n".utf8))
        guard case .malformed = outcome else {
            Issue.record("expected a malformed outcome, got \(outcome)")
            return
        }
    }

    @Test func aContentLengthOneByteOverTheCapIsMalformed() {
        var reassembler = ForwardingUpstreamResponseReassembler()
        let overCap = ForwardingSizeLimits.maxBodyBytes + 1
        let outcome = reassembler.feed(
            Array("HTTP/1.1 200 OK\r\nContent-Length: \(overCap)\r\n\r\n".utf8))
        guard case .malformed = outcome else {
            Issue.record("expected a malformed outcome, got \(outcome)")
            return
        }
    }

    @Test func aContentLengthAtExactlyTheCapStillReassembles() {
        var reassembler = ForwardingUpstreamResponseReassembler()
        let atCap = ForwardingSizeLimits.maxBodyBytes
        let head = Array("HTTP/1.1 200 OK\r\nContent-Length: \(atCap)\r\n\r\n".utf8)
        let body = [UInt8](repeating: UInt8(ascii: "a"), count: atCap)
        #expect(reassembler.feed(head) == .needsMoreBytes)
        #expect(reassembler.feed(body) == .complete(responseBytes: head + body))
    }

    @Test func aHexChunkSizeThatOverflowsIntIsMalformedRatherThanTrapping() {
        var reassembler = ForwardingUpstreamResponseReassembler()
        let outcome = reassembler.feed(
            Array(
                "HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nffffffffffffffff\r\n".utf8))
        guard case .malformed = outcome else {
            Issue.record("expected a malformed outcome, got \(outcome)")
            return
        }
    }

    @Test func aSingleChunkSizeOverTheCapIsMalformed() {
        var reassembler = ForwardingUpstreamResponseReassembler()
        let overCapHex = String(ForwardingSizeLimits.maxBodyBytes + 1, radix: 16)
        let outcome = reassembler.feed(
            Array("HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n\(overCapHex)\r\n".utf8))
        guard case .malformed = outcome else {
            Issue.record("expected a malformed outcome, got \(outcome)")
            return
        }
    }

    // MARK: - hostile chunk-size lines (round 3 review finding)
    //
    // `Int(_:radix: 16)` alone accepts a leading `-` or `+`, part of
    // Swift's own numeric grammar rather than the chunked-encoding one. A
    // chunk size of `-100000` passed the `size <= maxBodyBytes` cap (a
    // large negative number is still `<=` the cap) and the
    // overflow-checked sums below it (a negative sum does not overflow),
    // reaching `bytes[chunkEnd]` with a negative index and trapping the
    // daemon. Every case here must come back `.malformed` rather than
    // trap.

    @Test(arguments: [
        "-100000",
        "-1",
        "+10",
        " 10",
        "0x10",
        "",
        "fffffffffffffffff",  // 17 hex digits
    ])
    func aHostileChunkSizeLineIsMalformedRatherThanTrapping(chunkSizeLine: String) {
        var reassembler = ForwardingUpstreamResponseReassembler()
        let outcome = reassembler.feed(
            Array("HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n\(chunkSizeLine)\r\n".utf8))
        guard case .malformed = outcome else {
            Issue.record("expected a malformed outcome for chunk-size line '\(chunkSizeLine)', got \(outcome)")
            return
        }
    }

    @Test func chunkSizesEachUnderTheCapButSummingOverItAreMalformed() {
        var reassembler = ForwardingUpstreamResponseReassembler()
        let head = Array("HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n".utf8)
        #expect(reassembler.feed(head) == .needsMoreBytes)
        // Two chunks, each under the cap on its own, whose sum exceeds it.
        let perChunk = (ForwardingSizeLimits.maxBodyBytes / 2) + 100
        let chunkHex = String(perChunk, radix: 16)
        let chunkData = [UInt8](repeating: UInt8(ascii: "x"), count: perChunk)
        let firstChunk = Array("\(chunkHex)\r\n".utf8) + chunkData + Array("\r\n".utf8)
        #expect(reassembler.feed(firstChunk) == .needsMoreBytes)
        let secondChunk = Array("\(chunkHex)\r\n".utf8) + chunkData + Array("\r\n".utf8)
        let outcome = reassembler.feed(secondChunk)
        guard case .malformed = outcome else {
            Issue.record("expected a malformed outcome, got \(outcome)")
            return
        }
    }

    @Test func aCloseFramedBodyThatGrowsPastTheCapIsMalformed() {
        var reassembler = ForwardingUpstreamResponseReassembler()
        let head = Array("HTTP/1.1 200 OK\r\nX-Trace: a\r\n\r\n".utf8)
        #expect(reassembler.feed(head) == .needsMoreBytes)
        let overTotalCap = ForwardingSizeLimits.maxHeadBytes + ForwardingSizeLimits.maxBodyBytes + 1
        let body = [UInt8](repeating: UInt8(ascii: "z"), count: overTotalCap)
        let outcome = reassembler.feed(body)
        guard case .malformed = outcome else {
            Issue.record("expected a malformed outcome, got \(outcome)")
            return
        }
    }
}
