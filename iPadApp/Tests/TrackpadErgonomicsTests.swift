import XCTest
@testable import DesktopAgent

/// Pointer acceleration and momentum ("glide") scrolling.
///
/// Spec: `specs/ipad-trackpad-ergonomics/requirements.md` R1–R7.
///
/// The two Phase 1 guards — `testOffStateIsIdenticalToLinear` (R6.1) and
/// `testTremorIsNeverAmplified` (R2.4) — were written before the feature code
/// and gate it. R2.4 in particular is the whole reason this feature is safe:
/// acceleration rewards high instantaneous speed, and tremor *is* high speed
/// over a tiny distance.
@MainActor
final class TrackpadErgonomicsTests: XCTestCase {

    private final class EventLog {
        var events: [TrackpadEvent] = []

        var moves: [(dx: Int, dy: Int)] {
            events.compactMap { event -> (dx: Int, dy: Int)? in
                guard case .move(let dx, let dy) = event else { return nil }
                return (dx, dy)
            }
        }

        var scrolls: [(direction: String, clicks: Int)] {
            events.compactMap { event -> (direction: String, clicks: Int)? in
                guard case .scroll(let direction, let clicks) = event else { return nil }
                return (direction, clicks)
            }
        }

        var taps: [Int] {
            events.compactMap { event -> Int? in
                guard case .tap(let fingers) = event else { return nil }
                return fingers
            }
        }

        var totalDX: Int { moves.reduce(0) { $0 + $1.dx } }
        var totalClicks: Int { scrolls.reduce(0) { $0 + $1.clicks } }
    }

    private func makeCoordinator(
        speed: CGFloat = 1.0,
        accelExponent: Double = 1.0,
        momentumEnabled: Bool = true,
        painDay: Bool = false
    ) -> (TrackpadGestureView.Coordinator, EventLog) {
        let log = EventLog()
        let coordinator = TrackpadGestureView(
            palmRadius: 25,
            speed: speed,
            accelExponent: accelExponent,
            momentumEnabled: momentumEnabled,
            painDay: painDay
        ) { log.events.append($0) }.makeCoordinator()
        return (coordinator, log)
    }

    /// Drives a straight horizontal drag at a fixed speed and returns total
    /// emitted cursor travel.
    private func dragTravel(exponent: Double,
                            pointsPerFrame: CGFloat,
                            frames: Int = 30,
                            frameInterval: CFTimeInterval = 1.0 / 60.0) -> Int {
        let (c, log) = makeCoordinator(speed: 1.0, accelExponent: exponent)
        var t: CFTimeInterval = 1000
        for _ in 0..<frames {
            t += frameInterval
            c.accumulateMove(dx: pointsPerFrame, dy: 0, now: t)
        }
        return log.totalDX
    }

    // MARK: — Phase 1 guard: off state is exactly today's behaviour (R6.1, R1.4)

    func testOffStateIsIdenticalToLinear() {
        // Exponent 1.0 must reproduce plain `dx * speed` accumulation exactly,
        // including the fractional-carry behaviour from B0.
        let (c, log) = makeCoordinator(speed: 2.0, accelExponent: 1.0)
        var t: CFTimeInterval = 1000
        for _ in 0..<50 {
            t += 1.0 / 60.0
            c.accumulateMove(dx: 3.0, dy: 0, now: t)
        }
        // 50 frames x 3pt x speed 2.0 = 300px, no acceleration anywhere.
        XCTAssertEqual(log.totalDX, 300,
                       "Exponent 1.0 must be exactly linear — it is the off switch")
    }

    func testOffStateHoldsAtEveryDragSpeed() {
        for pointsPerFrame in [CGFloat(0.5), 2, 8, 40, 120] {
            let travel = dragTravel(exponent: 1.0, pointsPerFrame: pointsPerFrame)
            let expected = Int(pointsPerFrame * 30)
            XCTAssertLessThanOrEqual(
                abs(travel - expected), 1,
                "Linear mode must not vary with speed (\(pointsPerFrame) pt/frame): got \(travel), expected \(expected)")
        }
    }

    // MARK: — Phase 1 guard: tremor must never be amplified (R2.4)

    /// A 4 Hz oscillation of 8 pt amplitude — representative tremor — must not
    /// produce more cursor travel with acceleration on than with it off.
    ///
    /// This is the test that makes acceleration safe to ship. A naive
    /// implementation multiplies gain by instantaneous speed, and a tremor spike
    /// is a high instantaneous speed, so the cursor jumps.
    func testTremorIsNeverAmplified() {
        func oscillationTravel(exponent: Double) -> Int {
            let (c, log) = makeCoordinator(speed: 2.0, accelExponent: exponent)
            let frequency = 4.0            // Hz
            let amplitude: CGFloat = 8.0   // points
            let frameInterval = 1.0 / 60.0
            var t: CFTimeInterval = 1000
            var previous: CGFloat = 0
            for frame in 0..<120 {
                t += frameInterval
                let phase = 2 * Double.pi * frequency * (Double(frame) * frameInterval)
                let position = amplitude * CGFloat(sin(phase))
                c.accumulateMove(dx: position - previous, dy: 0, now: t)
                previous = position
            }
            // Total absolute travel is what a user perceives as jitter.
            return log.moves.reduce(0) { $0 + abs($1.dx) }
        }

        let linear = oscillationTravel(exponent: 1.0)
        for exponent in [1.2, 1.6, 2.0, 3.0] {
            let accelerated = oscillationTravel(exponent: exponent)
            XCTAssertLessThanOrEqual(
                accelerated, linear,
                "Exponent \(exponent) amplified a 4Hz/8pt tremor: \(accelerated) > \(linear)")
        }
    }

    // MARK: — R1: acceleration helps fast drags

    func testFastDragsCoverMoreScreenThanLinear() {
        // ~120 pt/frame at 60fps = 7200 pt/s — a deliberate fast traversal.
        let linear = dragTravel(exponent: 1.0, pointsPerFrame: 120)
        let accelerated = dragTravel(exponent: 1.6, pointsPerFrame: 120)
        XCTAssertGreaterThan(accelerated, linear,
                             "A fast drag should cover more screen with acceleration on")
    }

    func testSlowPrecisionDragsAreUnaffected() {
        // 1 pt/frame at 60fps = 60 pt/s, below accelLowSpeedThreshold (120).
        let linear = dragTravel(exponent: 1.0, pointsPerFrame: 1)
        let accelerated = dragTravel(exponent: 1.6, pointsPerFrame: 1)
        XCTAssertEqual(accelerated, linear,
                       "Slow, deliberate positioning must keep today's precision")
    }

    /// R1.3 — acceleration may only ever add gain.
    func testGainIsNeverReducedBelowLinear() {
        for pointsPerFrame in [CGFloat(0.5), 1, 4, 15, 60, 200] {
            let linear = dragTravel(exponent: 1.0, pointsPerFrame: pointsPerFrame)
            let accelerated = dragTravel(exponent: 2.0, pointsPerFrame: pointsPerFrame)
            XCTAssertGreaterThanOrEqual(
                accelerated, linear,
                "Acceleration reduced gain at \(pointsPerFrame) pt/frame")
        }
    }

    /// R1.7 — a stalled callback must not be read as a fast drag.
    func testStalledCallbackDoesNotInferSpeed() {
        let (c, log) = makeCoordinator(speed: 1.0, accelExponent: 2.5)
        // A 500 ms gap is a stall, not motion. 10pt over that is ~20 pt/s.
        c.accumulateMove(dx: 10, dy: 0, now: 1000)
        c.accumulateMove(dx: 10, dy: 0, now: 1000.5)
        XCTAssertEqual(log.totalDX, 20,
                       "An implausible frame interval must fall back to linear gain")
    }

    /// R1.6 — a diagonal drag must stay straight.
    func testDiagonalDragDoesNotCurve() {
        let (c, log) = makeCoordinator(speed: 1.0, accelExponent: 2.0)
        var t: CFTimeInterval = 1000
        for _ in 0..<40 {
            t += 1.0 / 60.0
            c.accumulateMove(dx: 30, dy: 30, now: t)
        }
        let totalX = log.moves.reduce(0) { $0 + $1.dx }
        let totalY = log.moves.reduce(0) { $0 + $1.dy }
        XCTAssertLessThanOrEqual(
            abs(totalX - totalY), 1,
            "Equal x and y input must produce equal output — no curving (\(totalX) vs \(totalY))")
    }

    // MARK: — R3/R4: momentum scrolling

    /// R3.5 — a slow, deliberate scroll must come to rest on release.
    func testSlowScrollDoesNotCoast() {
        let (c, _) = makeCoordinator()
        var t: CFTimeInterval = 1000
        for _ in 0..<10 {
            t += 1.0 / 60.0
            c.accumulateScroll(dx: 0, dy: 2, now: t)   // ~120 pt/s
        }
        c.endGesture(now: t)
        XCTAssertFalse(c.isCoasting, "A slow scroll must not drift after release")
    }

    /// R3.1 — a fast flick starts a coast.
    func testFastFlickStartsCoast() {
        let (c, _) = makeCoordinator()
        var t: CFTimeInterval = 1000
        for _ in 0..<10 {
            t += 1.0 / 60.0
            c.accumulateScroll(dx: 0, dy: 20, now: t)  // ~1200 pt/s
        }
        c.endGesture(now: t)
        XCTAssertTrue(c.isCoasting, "A fast two-finger flick should coast")
        c.stopCoast()
    }

    /// R3.6 — decay 0 disables coasting entirely.
    func testZeroDecayDisablesCoast() {
        let (c, _) = makeCoordinator()
        c.momentumDecayPerTick = 0
        var t: CFTimeInterval = 1000
        for _ in 0..<10 {
            t += 1.0 / 60.0
            c.accumulateScroll(dx: 0, dy: 20, now: t)
        }
        c.endGesture(now: t)
        XCTAssertFalse(c.isCoasting, "Decay 0 is the off switch")
    }

    /// R7.6 — the toggle actually gates momentum.
    func testMomentumDisabledByToggle() {
        let (c, _) = makeCoordinator(momentumEnabled: false)
        var t: CFTimeInterval = 1000
        for _ in 0..<10 {
            t += 1.0 / 60.0
            c.accumulateScroll(dx: 0, dy: 20, now: t)
        }
        c.endGesture(now: t)
        XCTAssertFalse(c.isCoasting, "Glide toggle off must prevent coasting")
    }

    /// R4.1 — the touch that stops a coast must NOT also click.
    func testTouchStoppingCoastDoesNotClick() {
        let (c, log) = makeCoordinator()
        var t: CFTimeInterval = 1000
        for _ in 0..<10 {
            t += 1.0 / 60.0
            c.accumulateScroll(dx: 0, dy: 20, now: t)
        }
        c.endGesture(now: t)
        XCTAssertTrue(c.isCoasting)

        // The user reaches out to halt the coast.
        XCTAssertTrue(c.interruptCoastIfRunning(), "Touch should interrupt the coast")
        XCTAssertFalse(c.isCoasting, "Coast must stop on touch")

        c.onSingleTapRecognized(now: t + 0.05)
        XCTAssertTrue(log.taps.isEmpty,
                      "Stopping a coast must not click whatever it landed on")
    }

    /// A tap with no coast running is unaffected by the suppression flag.
    func testTapStillWorksWhenNotCoasting() async {
        let (c, log) = makeCoordinator()
        XCTAssertFalse(c.interruptCoastIfRunning())
        c.onSingleTapRecognized(now: 1000)

        let fired = expectation(description: "tap fires")
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { fired.fulfill() }
        await fulfillment(of: [fired], timeout: 1.0)

        XCTAssertEqual(log.taps, [1], "A normal tap must be unaffected")
    }

    // MARK: — R5: flare adaptation

    /// R5.1 — a flare day pulls the exponent toward linear.
    func testFlareDayDampsAcceleration() {
        let (normal, _) = makeCoordinator(accelExponent: 2.0, painDay: false)
        let (flare, _) = makeCoordinator(accelExponent: 2.0, painDay: true)
        XCTAssertLessThan(flare.accelExponent, normal.accelExponent,
                          "A flare day should make the pointer more predictable")
        XCTAssertGreaterThanOrEqual(flare.accelExponent, 1.0,
                                    "Damping must never go below linear")
    }

    /// R5.2 — a flare day demands a firmer flick before coasting.
    func testFlareDayRaisesFlingThreshold() {
        let (normal, _) = makeCoordinator(painDay: false)
        let (flare, _) = makeCoordinator(painDay: true)
        XCTAssertGreaterThan(flare.momentumMinVelocity, normal.momentumMinVelocity,
                             "An unsteady release should be less likely to fling")
    }

    // MARK: — R6.2: B0 guarantees survive

    func testB0FractionalAccumulationStillHolds() {
        let (c, log) = makeCoordinator(speed: 1.0, accelExponent: 1.0)
        var t: CFTimeInterval = 1000
        for _ in 0..<4 {
            t += 1.0 / 60.0
            c.accumulateMove(dx: 0.3, dy: 0, now: t)
        }
        XCTAssertEqual(log.totalDX, 1, "Sub-pixel carry from B0 must survive")
    }
}
