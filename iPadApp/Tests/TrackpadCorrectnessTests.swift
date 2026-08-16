import XCTest
@testable import DesktopAgent

/// Phase B0 trackpad correctness — tap disambiguation, scroll magnitude, and
/// coordinator-owned interaction state.
///
/// Spec: `specs/ipad-assistive-tech-compat/requirements.md` R8, R9, R10.
@MainActor
final class TrackpadCorrectnessTests: XCTestCase {

    /// Builds a coordinator via the view's own factory, capturing every emitted
    /// event into the returned box.
    private func makeCoordinator(
        speed: CGFloat = 1.0
    ) -> (TrackpadGestureView.Coordinator, EventLog) {
        let log = EventLog()
        let coordinator = TrackpadGestureView(
            palmRadius: 25,
            speed: speed
        ) { log.events.append($0) }.makeCoordinator()
        // makeCoordinator captures the closure passed above; nothing else needed.
        return (coordinator, log)
    }

    private final class EventLog {
        var events: [TrackpadEvent] = []

        var taps: [Int] {
            events.compactMap { event -> Int? in
                guard case .tap(let fingers) = event else { return nil }
                return fingers
            }
        }

        var scrolls: [(direction: String, clicks: Int)] {
            events.compactMap { event -> (direction: String, clicks: Int)? in
                guard case .scroll(let direction, let clicks) = event else { return nil }
                return (direction, clicks)
            }
        }

        var moves: [(dx: Int, dy: Int)] {
            events.compactMap { event -> (dx: Int, dy: Int)? in
                guard case .move(let dx, let dy) = event else { return nil }
                return (dx, dy)
            }
        }
    }

    // MARK: — R8: tap disambiguation is order-independent

    /// R8.1, ordering A — tap2 reports first, then tap1 straggles in.
    /// The straggler must be dropped.
    func testTwoFingerTap_whenDoubleReportsFirst_emitsOnlyRightClick() {
        let (c, log) = makeCoordinator()
        let t: CFTimeInterval = 1000

        c.onDoubleTapRecognized(now: t)
        c.onSingleTapRecognized(now: t + 0.01)   // straggler, inside the window

        XCTAssertEqual(log.taps, [2],
                       "A 2-finger tap must emit exactly one right-click, never a left-click")
    }

    /// R8.1, ordering B — tap1 reports first, then tap2 arrives.
    /// This is the ordering the old `lastTap2Time`-only guard could not handle:
    /// it emitted a spurious left click before the right click.
    func testTwoFingerTap_whenSingleReportsFirst_emitsOnlyRightClick() {
        let (c, log) = makeCoordinator()
        let t: CFTimeInterval = 1000

        c.onSingleTapRecognized(now: t)          // deferred, not yet emitted
        XCTAssertEqual(log.taps, [], "1-finger tap must not fire immediately")

        c.onDoubleTapRecognized(now: t + 0.01)   // must cancel the deferred tap1

        XCTAssertEqual(log.taps, [2],
                       "REGRESSION: tap1-then-tap2 ordering emitted a spurious left click")
    }

    /// R8.1 — the deferred tap must stay cancelled, not fire after the window.
    func testDeferredSingleTapStaysCancelledAfterDoubleTap() async {
        let (c, log) = makeCoordinator()
        c.onSingleTapRecognized(now: 1000)
        c.onDoubleTapRecognized(now: 1000.01)

        let settled = expectation(description: "disambiguation window elapses")
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { settled.fulfill() }
        await fulfillment(of: [settled], timeout: 1.0)

        XCTAssertEqual(log.taps, [2],
                       "Cancelled tap1 must never fire, even after the window elapses")
    }

    /// R8.3 — a genuine lone 1-finger tap still produces a left click.
    func testLoneSingleTapEventuallyEmitsLeftClick() async {
        let (c, log) = makeCoordinator()
        c.onSingleTapRecognized(now: 1000)

        let fired = expectation(description: "deferred single tap fires")
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { fired.fulfill() }
        await fulfillment(of: [fired], timeout: 1.0)

        XCTAssertEqual(log.taps, [1], "A lone 1-finger tap must still left-click")
    }

    /// R8.2 — the deferral must stay well under `require(toFail:)`'s ~300 ms.
    func testDisambiguationWindowIsWellUnderRequireToFailCost() {
        let (c, _) = makeCoordinator()
        XCTAssertLessThanOrEqual(c.tapDisambiguationWindow, 0.12,
                                 "Deferral must not reintroduce the latency require(toFail:) cost")
    }

    // MARK: — R9: scroll carries magnitude and cannot flood

    /// R9.1, R9.2 — magnitude must track travel, not be a constant.
    func testScrollMagnitudeScalesWithTravel() {
        let (c, log) = makeCoordinator()
        c.scrollPointsPerClick = 20

        // 100pt of downward travel = 5 clicks.
        c.accumulateScroll(dx: 0, dy: 100, now: 1000)

        XCTAssertEqual(log.scrolls.count, 1)
        XCTAssertEqual(log.scrolls.first?.direction, "down")
        XCTAssertEqual(log.scrolls.first?.clicks, 5,
                       "100pt at 20pt/click must emit 5 clicks, not a hardcoded 3")
    }

    /// R9.5 — sub-click travel accumulates instead of emitting a zero.
    func testSubClickTravelEmitsNothingAndAccumulates() {
        let (c, log) = makeCoordinator()
        c.scrollPointsPerClick = 20

        c.accumulateScroll(dx: 0, dy: 5, now: 1000)
        XCTAssertTrue(log.scrolls.isEmpty, "5pt is under one click — emit nothing")

        // Four more 5pt increments cross the threshold.
        c.accumulateScroll(dx: 0, dy: 5, now: 1001)
        c.accumulateScroll(dx: 0, dy: 5, now: 1002)
        c.accumulateScroll(dx: 0, dy: 5, now: 1003)

        XCTAssertEqual(log.scrolls.first?.clicks, 1,
                       "Accumulated 20pt must eventually emit exactly one click")
    }

    /// R9.3 — bursts inside one rate-limit window are coalesced, not multiplied.
    func testRapidScrollIsRateLimited() {
        let (c, log) = makeCoordinator()
        c.scrollPointsPerClick = 20
        c.maxScrollMessagesPerSecond = 30      // ~33ms minimum interval

        // 20 callbacks in 2ms — the ProMotion flood case.
        for i in 0..<20 {
            c.accumulateScroll(dx: 0, dy: 20, now: 1000 + Double(i) * 0.0001)
        }

        XCTAssertLessThanOrEqual(log.scrolls.count, 2,
                                 "A burst inside one window must coalesce, not emit per callback")
    }

    /// R9.3, R9.4 — coalesced travel is carried forward, never dropped.
    func testCoalescedTravelIsNotLost() {
        let (c, log) = makeCoordinator()
        c.scrollPointsPerClick = 20
        c.maxScrollMessagesPerSecond = 30

        for i in 0..<20 {
            c.accumulateScroll(dx: 0, dy: 20, now: 1000 + Double(i) * 0.0001)
        }
        c.endGesture(now: 1000.5)              // flush

        let totalClicks = log.scrolls.reduce(0) { $0 + $1.clicks }
        XCTAssertEqual(totalClicks, 20,
                       "400pt of travel at 20pt/click must total 20 clicks regardless of rate limiting")
    }

    /// R9.4 — total distance must not depend on callback frequency.
    func testTotalScrollIsIndependentOfCallbackFrequency() {
        func totalClicks(callbacks: Int) -> Int {
            let (c, log) = makeCoordinator()
            c.scrollPointsPerClick = 20
            c.maxScrollMessagesPerSecond = 30
            let perCallback = 400.0 / Double(callbacks)
            for i in 0..<callbacks {
                c.accumulateScroll(dx: 0, dy: CGFloat(perCallback),
                                   now: 1000 + Double(i) * 0.05)
            }
            c.endGesture(now: 2000)
            return log.scrolls.reduce(0) { $0 + $1.clicks }
        }

        XCTAssertEqual(totalClicks(callbacks: 8), totalClicks(callbacks: 40),
                       "60Hz and 120Hz callback rates must scroll the same distance")
    }

    /// R9 — direction follows the dominant axis, preserving prior sign convention.
    func testScrollDirectionFollowsDominantAxis() {
        for (dx, dy, expected) in [(0.0, -100.0, "up"), (0.0, 100.0, "down"),
                                   (-100.0, 0.0, "left"), (100.0, 0.0, "right")] {
            let (c, log) = makeCoordinator()
            c.scrollPointsPerClick = 20
            c.accumulateScroll(dx: CGFloat(dx), dy: CGFloat(dy), now: 1000)
            XCTAssertEqual(log.scrolls.first?.direction, expected,
                           "dx=\(dx) dy=\(dy) should scroll \(expected)")
        }
    }

    // MARK: — R10: coordinator owns interaction state

    /// R10.1 — the fractional accumulator must survive across callbacks, so slow
    /// movement is not truncated away.
    func testSubPixelMovesAccumulateRatherThanTruncate() {
        let (c, log) = makeCoordinator(speed: 1.0)

        // Four 0.3px moves — each truncates to 0, but they sum past 1.
        for _ in 0..<4 { c.accumulateMove(dx: 0.3, dy: 0) }

        let totalX = log.moves.reduce(0) { $0 + $1.dx }
        XCTAssertEqual(totalX, 1, "1.2px of travel must emit 1px, not 0")
    }

    /// R10.1 — speed is applied inside the coordinator.
    func testSpeedMultiplierIsAppliedToMoves() {
        let (c, log) = makeCoordinator(speed: 3.0)
        c.accumulateMove(dx: 10, dy: 0)
        XCTAssertEqual(log.moves.first?.dx, 30, "speed 3.0 must triple a 10px move")
    }

    /// R10.3 — a refreshed event sink must be the one that receives events.
    /// This is the class of bug that made the palm-radius slider inert.
    func testRefreshedOnEventSinkReceivesEvents() {
        let (c, original) = makeCoordinator()
        let replacement = EventLog()

        TrackpadGestureView.applyLiveSettings(to: c, palmRadius: 25, speed: 1.0) {
            replacement.events.append($0)
        }
        c.accumulateMove(dx: 10, dy: 0)

        XCTAssertTrue(original.events.isEmpty, "Stale closure must not receive events")
        XCTAssertEqual(replacement.moves.first?.dx, 10, "Refreshed closure must receive events")
    }

    /// R10.4 — ending a gesture clears accumulators so the next drag starts clean.
    func testEndGestureResetsAccumulators() {
        let (c, log) = makeCoordinator()
        c.accumulateMove(dx: 0.6, dy: 0)        // 0.6 banked, nothing emitted
        c.endGesture(now: 1000)
        c.accumulateMove(dx: 0.6, dy: 0)        // must not combine with the stale 0.6

        XCTAssertTrue(log.moves.isEmpty,
                      "Banked sub-pixel travel must not carry across gestures")
    }
}
