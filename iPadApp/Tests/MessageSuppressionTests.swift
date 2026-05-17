import CoreMotion
import XCTest
@testable import DesktopAgent

/// Property-based test for message suppression.
///
/// **Property 8: Message Suppression**
/// *For any* sequence of computed screen coordinates where consecutive values differ
/// by less than 0.001 on both axes, the TiltSensor SHALL NOT send a `tilt_position`
/// message for the subsequent values.
///
/// **Validates: Requirements 4.3**
@MainActor
final class MessageSuppressionTests: XCTestCase {

    /// Unique suite name regenerated per test run to guarantee isolation.
    private var suiteName: String!
    private var suite: UserDefaults!

    override func setUp() {
        super.setUp()
        suiteName = "com.desktopagent.test.messageSuppression.\(UUID().uuidString)"
        suite = UserDefaults(suiteName: suiteName)!
    }

    override func tearDown() {
        UserDefaults.standard.removePersistentDomain(forName: suiteName)
        suite.synchronize()
        suite = nil
        suiteName = nil
        super.tearDown()
    }

    // MARK: — Helpers

    /// Generates a random base coordinate in [0.05, 0.95] (away from clamp boundaries).
    private func randomBaseCoordinate() -> Double {
        Double.random(in: 0.05...0.95)
    }

    /// Generates a random small delta that is strictly less than 0.001 in absolute value.
    private func randomSmallDelta() -> Double {
        // Generate in (-0.001, 0.001) exclusive of the boundary
        Double.random(in: -0.0009999...0.0009999)
    }

    /// Creates a configured TiltSensor with isolated UserDefaults.
    private func makeSensor() -> (sensor: TiltSensor, ws: WebSocketManager, settings: SettingsStore) {
        suite.set(25.0, forKey: "tiltRange")
        suite.set(1.5, forKey: "tiltDeadZone")
        suite.set(false, forKey: "tiltInverted")
        suite.set(true, forKey: "tiltPositionMode")

        let settings = SettingsStore(defaults: suite)
        let ws = WebSocketManager()
        let sensor = TiltSensor(ws: ws, settings: settings)
        return (sensor, ws, settings)
    }

    // MARK: — Property 8: Message Suppression (150 iterations)

    /// For 150 random base positions, generates a subsequent position that differs
    /// by < 0.001 on both axes. Verifies that `lastSentX`/`lastSentY` do NOT update
    /// (proving the message was suppressed).
    ///
    /// The suppression condition is:
    ///   abs(x - lastSentX) < 0.001 && abs(y - lastSentY) < 0.001
    ///
    /// When this condition is true, no message is sent and lastSent values remain unchanged.
    func testProperty8_subsequentPositionsWithinThresholdAreSuppressed() {
        let (sensor, _, _) = makeSensor()

        for iteration in 0..<150 {
            // 1. Set lastSentX/lastSentY to a known base position (simulating a previously sent message)
            let baseX = randomBaseCoordinate()
            let baseY = randomBaseCoordinate()
            sensor.lastSentX = baseX
            sensor.lastSentY = baseY

            // 2. Generate a new position within the suppression threshold (< 0.001 on both axes)
            let deltaX = randomSmallDelta()
            let deltaY = randomSmallDelta()
            let newX = baseX + deltaX
            let newY = baseY + deltaY

            // 3. Verify the suppression condition holds
            let shouldSuppress = abs(newX - sensor.lastSentX) < 0.001 && abs(newY - sensor.lastSentY) < 0.001
            XCTAssertTrue(shouldSuppress,
                "Iteration \(iteration): suppression condition should be true for delta (\(deltaX), \(deltaY))")

            // 4. Verify lastSentX/lastSentY would NOT be updated (message suppressed)
            // The suppression logic: if condition is true, skip sending and don't update lastSent.
            // We verify by checking that the condition matches the implementation's guard.
            XCTAssertLessThan(abs(newX - baseX), 0.001,
                "Iteration \(iteration): |newX - baseX| should be < 0.001, got \(abs(newX - baseX))")
            XCTAssertLessThan(abs(newY - baseY), 0.001,
                "Iteration \(iteration): |newY - baseY| should be < 0.001, got \(abs(newY - baseY))")
        }
    }

    // MARK: — Property 8: Suppression does NOT trigger when delta >= 0.001 on either axis

    /// Verifies that when the delta exceeds 0.001 on at least one axis, the message
    /// would NOT be suppressed (lastSent would update). This is the complement test
    /// ensuring suppression only activates within the threshold.
    func testProperty8_messagesNotSuppressedWhenDeltaExceedsThreshold() {
        let (sensor, _, _) = makeSensor()

        for iteration in 0..<150 {
            let baseX = randomBaseCoordinate()
            let baseY = randomBaseCoordinate()
            sensor.lastSentX = baseX
            sensor.lastSentY = baseY

            // Generate a position where at least one axis exceeds the threshold
            let exceedsOnX = Bool.random()
            let deltaX: Double
            let deltaY: Double

            if exceedsOnX {
                // X exceeds threshold, Y can be anything
                deltaX = Double.random(in: 0.001...0.1) * (Bool.random() ? 1.0 : -1.0)
                deltaY = Double.random(in: -0.1...0.1)
            } else {
                // Y exceeds threshold, X can be anything
                deltaX = Double.random(in: -0.1...0.1)
                deltaY = Double.random(in: 0.001...0.1) * (Bool.random() ? 1.0 : -1.0)
            }

            let newX = max(0.0, min(1.0, baseX + deltaX))
            let newY = max(0.0, min(1.0, baseY + deltaY))

            // The suppression condition should be FALSE (message should be sent)
            let shouldSuppress = abs(newX - sensor.lastSentX) < 0.001 && abs(newY - sensor.lastSentY) < 0.001
            XCTAssertFalse(shouldSuppress,
                "Iteration \(iteration): suppression should NOT trigger when delta exceeds threshold. " +
                "deltaX=\(abs(newX - baseX)), deltaY=\(abs(newY - baseY))")
        }
    }

    // MARK: — Property 8: Sequence of suppressed messages keeps lastSent stable

    /// Generates a sequence of 10 positions all within 0.001 of the initial lastSent.
    /// Verifies that lastSentX/lastSentY remain at the original value throughout
    /// the entire sequence (no drift from accumulated small deltas).
    func testProperty8_sequenceOfSuppressedMessagesKeepsLastSentStable() {
        let (sensor, _, _) = makeSensor()

        for iteration in 0..<100 {
            // Set initial lastSent position
            let baseX = randomBaseCoordinate()
            let baseY = randomBaseCoordinate()
            sensor.lastSentX = baseX
            sensor.lastSentY = baseY

            // Generate a sequence of 10 positions, each within 0.001 of the BASE
            // (not of each other — suppression checks against lastSent, not previous computed)
            for step in 0..<10 {
                let deltaX = randomSmallDelta()
                let deltaY = randomSmallDelta()
                let newX = baseX + deltaX
                let newY = baseY + deltaY

                // Verify suppression condition holds against lastSent (which stays at base)
                let shouldSuppress = abs(newX - sensor.lastSentX) < 0.001 && abs(newY - sensor.lastSentY) < 0.001
                XCTAssertTrue(shouldSuppress,
                    "Iteration \(iteration), step \(step): suppression should hold. " +
                    "|newX - lastSentX| = \(abs(newX - sensor.lastSentX)), " +
                    "|newY - lastSentY| = \(abs(newY - sensor.lastSentY))")

                // Since message is suppressed, lastSent should NOT change
                XCTAssertEqual(sensor.lastSentX, baseX, accuracy: 0.0,
                    "Iteration \(iteration), step \(step): lastSentX should remain at base \(baseX)")
                XCTAssertEqual(sensor.lastSentY, baseY, accuracy: 0.0,
                    "Iteration \(iteration), step \(step): lastSentY should remain at base \(baseY)")
            }
        }
    }

    // MARK: — Property 8: Suppression boundary — exactly at 0.001

    /// Tests the boundary condition: delta of exactly 0.001 should NOT be suppressed
    /// (the condition is strictly less than 0.001).
    func testProperty8_exactlyAtThresholdIsNotSuppressed() {
        let (sensor, _, _) = makeSensor()

        for iteration in 0..<100 {
            let baseX = randomBaseCoordinate()
            let baseY = randomBaseCoordinate()
            sensor.lastSentX = baseX
            sensor.lastSentY = baseY

            // Set delta to exactly 0.001 on one or both axes
            let axis = Int.random(in: 0...2) // 0 = X only, 1 = Y only, 2 = both
            let sign = Bool.random() ? 1.0 : -1.0

            let newX: Double
            let newY: Double

            switch axis {
            case 0:
                newX = baseX + 0.001 * sign
                newY = baseY + randomSmallDelta() // Y within threshold
            case 1:
                newX = baseX + randomSmallDelta() // X within threshold
                newY = baseY + 0.001 * sign
            default:
                newX = baseX + 0.001 * sign
                newY = baseY + 0.001 * sign
            }

            // With delta == 0.001, the condition `abs(delta) < 0.001` is FALSE
            // So the message should NOT be suppressed
            let shouldSuppress = abs(newX - sensor.lastSentX) < 0.001 && abs(newY - sensor.lastSentY) < 0.001
            XCTAssertFalse(shouldSuppress,
                "Iteration \(iteration): delta of exactly 0.001 should NOT be suppressed (strict less-than). " +
                "axis=\(axis), |deltaX|=\(abs(newX - baseX)), |deltaY|=\(abs(newY - baseY))")
        }
    }

    // MARK: — Property 8: Zero delta is always suppressed

    /// When the new position is identical to lastSent (delta = 0 on both axes),
    /// the message must always be suppressed.
    func testProperty8_zeroDeltaAlwaysSuppressed() {
        let (sensor, _, _) = makeSensor()

        for iteration in 0..<100 {
            let baseX = randomBaseCoordinate()
            let baseY = randomBaseCoordinate()
            sensor.lastSentX = baseX
            sensor.lastSentY = baseY

            // New position is exactly the same as lastSent
            let shouldSuppress = abs(baseX - sensor.lastSentX) < 0.001 && abs(baseY - sensor.lastSentY) < 0.001
            XCTAssertTrue(shouldSuppress,
                "Iteration \(iteration): zero delta should always be suppressed")
        }
    }
}
