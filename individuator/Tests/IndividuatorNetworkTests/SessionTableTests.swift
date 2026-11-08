import Foundation
import Testing
@testable import IndividuatorDaemonCore
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif

/// `SessionTable` on its own (E12·2 slice S2) — no socket, no
/// `RegisterRequestHandler`; that type's own tests cover the reply shape
/// this table makes possible, not this table's internals.
@Suite struct SessionTableTests {
    @Test func twoRegistrationsReturnDistinctHandles() {
        let table = SessionTable()
        let first = table.register(
            bosunSessionToken: "tok-a", callerKind: "operator", registeringUID: 9001, account: "alice")
        let second = table.register(
            bosunSessionToken: "tok-b", callerKind: "operator", registeringUID: 9001, account: "alice")
        #expect(first != second)
    }

    @Test func lookupWithTheRegisteringUIDReturnsTheEntry() {
        let table = SessionTable()
        let handle = table.register(
            bosunSessionToken: "tok-a", callerKind: "agent", registeringUID: 9001, account: "alice")

        let result = table.lookup(handle: handle, peerUID: 9001)
        switch result {
        case .success(let entry):
            #expect(entry.bosunSessionToken == "tok-a")
            #expect(entry.callerKind == "agent")
            #expect(entry.registeringUID == 9001)
            #expect(entry.account == "alice")
        case .failure(let failure):
            Issue.record("expected a successful lookup, got \(failure)")
        }
    }

    @Test func lookupFromADifferentUIDIsRefused() {
        let table = SessionTable()
        let handle = table.register(
            bosunSessionToken: "tok-a", callerKind: "agent", registeringUID: 9001, account: "alice")

        let result = table.lookup(handle: handle, peerUID: 9002)
        #expect(result == .failure(.uidMismatch))
    }

    @Test func lookupOfAnUnknownHandleIsRefused() {
        let table = SessionTable()
        table.register(
            bosunSessionToken: "tok-a", callerKind: "agent", registeringUID: 9001, account: "alice")

        let result = table.lookup(handle: "not-a-handle-anyone-registered", peerUID: 9001)
        #expect(result == .failure(.unknownHandle))
    }

    /// A fresh `SessionTable` instance stands in for a daemon restart: the
    /// table lives in memory alone, so a new process (a new instance here)
    /// knows no handle a prior one minted.
    @Test func aFreshTableKnowsNoHandleFromAPriorOne() {
        let before = SessionTable()
        let handle = before.register(
            bosunSessionToken: "tok-a", callerKind: "agent", registeringUID: 9001, account: "alice")

        let afterRestart = SessionTable()
        let result = afterRestart.lookup(handle: handle, peerUID: 9001)
        #expect(result == .failure(.unknownHandle))
    }

    /// Many concurrent registers and lookups against one shared table,
    /// from many threads at once — `DispatchQueue.concurrentPerform` gives
    /// no guarantee about interleaving, so this exercises the table's own
    /// locking rather than any particular race window. A data race here
    /// would corrupt `entries` or crash under Swift's runtime exclusivity
    /// checks; every registered handle resolving back to its own entry
    /// afterward is this test's correctness check, not just "it didn't crash".
    @Test func isSafeUnderConcurrentRegistersAndLookups() {
        let table = SessionTable()
        let count = 200
        let handles = ThreadSafeBox<[Int: String]>([:])

        DispatchQueue.concurrentPerform(iterations: count) { i in
            let uid = uid_t(10_000 + i)
            let handle = table.register(
                bosunSessionToken: "tok-\(i)", callerKind: "agent", registeringUID: uid,
                account: "account-\(i)")
            handles.mutate { $0[i] = handle }
            // Immediately look up a handle registered by a (possibly
            // different) concurrently-running iteration too, so lookups and
            // registers race against each other, not just registers.
            _ = table.lookup(handle: handle, peerUID: uid)
        }

        let finalHandles = handles.value
        #expect(finalHandles.count == count)
        for i in 0..<count {
            let uid = uid_t(10_000 + i)
            let result = table.lookup(handle: finalHandles[i]!, peerUID: uid)
            switch result {
            case .success(let entry):
                #expect(entry.bosunSessionToken == "tok-\(i)")
            case .failure(let failure):
                Issue.record("expected iteration \(i)'s own handle to resolve, got \(failure)")
            }
        }
    }
}

/// A plain `NSLock`-guarded box, for this file's one concurrency test to
/// collect results from `DispatchQueue.concurrentPerform`'s closures
/// without racing on a bare `[Int: String]` itself.
private final class ThreadSafeBox<T> {
    private var storage: T
    private let lock = NSLock()

    init(_ initial: T) { self.storage = initial }

    var value: T {
        lock.lock()
        defer { lock.unlock() }
        return storage
    }

    func mutate(_ body: (inout T) -> Void) {
        lock.lock()
        defer { lock.unlock() }
        body(&storage)
    }
}
