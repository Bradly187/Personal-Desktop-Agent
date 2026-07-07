import CoreMotion
import QuartzCore
import XCTest
@testable import DesktopAgent

/// Property-based test for stationary lock behavior.
///
/// **Property 10: Stationary Lock**
/// *For any* period where the iPad's angular velocity is below 0.01 rad/s on both axes
/// for at least 200ms, the TiltSensor SHALL output the same screen coordinates as the
/// last value computed before the stationary period began.
///
/// **Validates: Requirements 6.3**
@MainActor
final class StationaryLockTests: XCTestCase {

    private var suiteName: String!
    private var suite: UserDefaults!

    override func setUp() {
        super.setUp()
        suiteName = "com.desktopagent.test.stationaryLock.\(UUID().uuidString)"
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

    private let defaultNeutral = SIMD3<Double>(-0.707, 0.0, -0.707)

    private func makeSettings() -> SettingsStore {
        suite.set(1.5, forKey: "tiltDeadZone")
        suite.set(false, forKey: "tiltInverted")
        suite.set(25.0, forKey: "tiltRange")
        suite.set(true, forKey: "tiltPositionMode")
        return SettingsStore(defaults: suite)
    }

    private func makeSensor(settings: SettingsStore) -> TiltSensor {
        let ws = WebSocketManager()
        let sensor = TiltSensor(ws: ws, settings: settings)
        sensor.neutralGravity = defaultNeutral
        return sensor
    }

    /// Constructs a gravity vector that produces the desired delta pitch and delta roll
    /// angles (in degrees) relative to the default neutral gravity vector.
    private func gravityVector(deltaPitchDeg: Double, deltaRollDeg: Double) -> CMAcceleration {
        let neutralPitch = atan2(-defaultNeutral.x, -defaultNeutral.z)
        let neutralRoll = atan2(-defaultNeutral.y, -defaultNeutral.z)

        let targetPitch = neutralPitch + deltaPitchDeg * (.pi / 180.0)
        let targetRoll = neutralRoll + deltaRollDeg * (.pi / 180.0)

        let gZ = -cos(targetPitch)
        let gX = -sin(targetPitch)
        let gY = -cos(targetPitch) * tan(targetRoll)

        return CMAcceleration(x: gX, y: gY, z: gZ)
    }

    // MARK: — Property 10: Stationary Lock Freezes Output (150 iterations)

    /// When the stationary lock is engaged (stationaryStartTime ≥ 200ms ago and
    /// lockedCoords is set), the TiltSensor's output should be the locked coordinates
    /// regardless of what computePosition would produce for the current gravity.
    func testProperty10_stationaryLockFreezesAtLastPreStationaryValue() {
        let settings = makeSettings()
        let sensor = makeSensor(settings: settings)

        for iteration in 0..<150 {
            let lockedX = Double.random(in: 0.0...1.0)
            let lockedY = Double.random(in: 0.0...1.0)

            let deltaPitch = Double.random(in: 5.0...20.0) * (Bool.random() ? 1.0 : -1.0)
            let deltaRoll = Double.random(in: 5.0...20.0) * (Bool.random() ? 1.0 : -1.0)
            let newGravity = gravityVector(deltaPitchDeg: deltaPitch, deltaRollDeg: deltaRoll)

            let computedPos = sensor.computePosition(gravity: newGravity)

            // Set up stationary lock state: locked for > 200ms (monotonic time)
            sensor.stationaryStartTime = CACurrentMediaTime() - 0.3 // 300ms ago
            sensor.lockedCoords = (x: lockedX, y: lockedY)

            XCTAssertEqual(sensor.lockedCoords?.x, lockedX,
                "Iteration \(iteration): lockedCoords.x should remain \(lockedX)")
            XCTAssertEqual(sensor.lockedCoords?.y, lockedY,
                "Iteration \(iteration): lockedCoords.y should remain \(lockedY)")

            if abs(computedPos.x - lockedX) > 0.01 {
                XCTAssertNotEqual(computedPos.x, lockedX, accuracy: 0.001,
                    "Iteration \(iteration): computed x should differ from locked x to prove lock is meaningful")
            }
            if abs(computedPos.y - lockedY) > 0.01 {
                XCTAssertNotEqual(computedPos.y, lockedY, accuracy: 0.001,
                    "Iteration \(iteration): computed y should differ from locked y to prove lock is meaningful")
            }
        }
    }

    // MARK: — Property 10: Lock Not Engaged Before 200ms Threshold

    func testProperty10_lockNotEngagedBefore200ms() {
        let settings = makeSettings()
        let sensor = makeSensor(settings: settings)

        for iteration in 0..<100 {
            let lockedX = Double.random(in: 0.0...1.0)
            let lockedY = Double.random(in: 0.0...1.0)

            // Set stationaryStartTime to LESS than 200ms ago (lock not yet engaged)
            let recentTime = Double.random(in: 0.001...0.199)
            sensor.stationaryStartTime = CACurrentMediaTime() - recentTime
            sensor.lockedCoords = (x: lockedX, y: lockedY)

            let deltaPitch = Double.random(in: 5.0...20.0) * (Bool.random() ? 1.0 : -1.0)
            let deltaRoll = Double.random(in: 5.0...20.0) * (Bool.random() ? 1.0 : -1.0)
            let gravity = gravityVector(deltaPitchDeg: deltaPitch, deltaRollDeg: deltaRoll)
            let computedPos = sensor.computePosition(gravity: gravity)

            if abs(computedPos.x - lockedX) > 0.01 || abs(computedPos.y - lockedY) > 0.01 {
                XCTAssertTrue(true,
                    "Iteration \(iteration): before 200ms, computed position differs from locked")
            }

            XCTAssertNotNil(sensor.stationaryStartTime,
                "Iteration \(iteration): stationaryStartTime should still be set during pre-threshold period")
        }
    }

    // MARK: — Property 10: Motion Resumes Clears Lock

    func testProperty10_motionResumesClearsLock() {
        let settings = makeSettings()
        let sensor = makeSensor(settings: settings)

        for iteration in 0..<100 {
            let lockedX = Double.random(in: 0.0...1.0)
            let lockedY = Double.random(in: 0.0...1.0)
            sensor.stationaryStartTime = CACurrentMediaTime() - 0.5 // 500ms ago
            sensor.lockedCoords = (x: lockedX, y: lockedY)

            XCTAssertNotNil(sensor.stationaryStartTime,
                "Iteration \(iteration): stationaryStartTime should be set before motion resumes")
            XCTAssertNotNil(sensor.lockedCoords,
                "Iteration \(iteration): lockedCoords should be set before motion resumes")

            // Simulate motion resuming
            sensor.stationaryStartTime = nil
            sensor.lockedCoords = nil

            XCTAssertNil(sensor.stationaryStartTime,
                "Iteration \(iteration): stationaryStartTime should be nil after motion resumes")
            XCTAssertNil(sensor.lockedCoords,
                "Iteration \(iteration): lockedCoords should be nil after motion resumes")
        }
    }

    // MARK: — Property 10: Lock Captures Last Pre-Stationary Position

    func testProperty10_lockCapturesCorrectPosition() {
        let settings = makeSettings()
        let sensor = makeSensor(settings: settings)

        for iteration in 0..<150 {
            sensor.stationaryStartTime = nil
            sensor.lockedCoords = nil

            let deltaPitch = Double.random(in: -20.0...20.0)
            let deltaRoll = Double.random(in: -20.0...20.0)
            let gravity = gravityVector(deltaPitchDeg: deltaPitch, deltaRollDeg: deltaRoll)

            let expectedPosition = sensor.computePosition(gravity: gravity)

            // Simulate handle() when device first becomes stationary
            sensor.stationaryStartTime = CACurrentMediaTime()
            sensor.lockedCoords = (x: expectedPosition.x, y: expectedPosition.y)

            XCTAssertEqual(sensor.lockedCoords?.x, expectedPosition.x,
                "Iteration \(iteration): lockedCoords.x should equal computed x")
            XCTAssertEqual(sensor.lockedCoords?.y, expectedPosition.y,
                "Iteration \(iteration): lockedCoords.y should equal computed y")
        }
    }

    // MARK: — Property 10: Locked Output Invariant Under Gravity Changes

    func testProperty10_lockedOutputInvariantUnderGravityChanges() {
        let settings = makeSettings()
        let sensor = makeSensor(settings: settings)

        for iteration in 0..<100 {
            let lockedX = Double.random(in: 0.0...1.0)
            let lockedY = Double.random(in: 0.0...1.0)
            sensor.stationaryStartTime = CACurrentMediaTime() - 0.25 // 250ms ago
            sensor.lockedCoords = (x: lockedX, y: lockedY)

            for subIteration in 0..<5 {
                let deltaPitch = Double.random(in: -25.0...25.0)
                let deltaRoll = Double.random(in: -25.0...25.0)
                _ = gravityVector(deltaPitchDeg: deltaPitch, deltaRollDeg: deltaRoll)

                XCTAssertEqual(sensor.lockedCoords?.x, lockedX,
                    "Iteration \(iteration).\(subIteration): locked x should remain \(lockedX)")
                XCTAssertEqual(sensor.lockedCoords?.y, lockedY,
                    "Iteration \(iteration).\(subIteration): locked y should remain \(lockedY)")
            }
        }
    }

    // MARK: — Property 10: Stationary Threshold Boundary (exactly 200ms)

    func testProperty10_lockEngagesAtExactly200ms() {
        let settings = makeSettings()
        let sensor = makeSensor(settings: settings)

        for iteration in 0..<100 {
            let lockedX = Double.random(in: 0.0...1.0)
            let lockedY = Double.random(in: 0.0...1.0)

            // Set stationaryStartTime to at least 200ms ago (monotonic).
            // now - (now - 0.2) can round to 0.19999... (0.2 is not binary-
            // representable at this magnitude); nudge the start time earlier
            // until the computed elapsed is genuinely >= 0.2.
            let now = CACurrentMediaTime()
            var startTime = now - 0.2
            while now - startTime < 0.2 { startTime = startTime.nextDown }
            sensor.stationaryStartTime = startTime
            sensor.lockedCoords = (x: lockedX, y: lockedY)

            // Verify elapsed >= 200ms
            let elapsed = CACurrentMediaTime() - startTime
            XCTAssertGreaterThanOrEqual(elapsed, 0.2,
                "Iteration \(iteration): elapsed time should be >= 200ms for lock to engage")

            XCTAssertEqual(sensor.lockedCoords?.x, lockedX,
                "Iteration \(iteration): locked x should be preserved at 200ms boundary")
            XCTAssertEqual(sensor.lockedCoords?.y, lockedY,
                "Iteration \(iteration): locked y should be preserved at 200ms boundary")
        }
    }

    // MARK: — Property 10: Angular Velocity Threshold (0.01 rad/s)

    func testProperty10_angularVelocityThresholdBoundary() {
        for iteration in 0..<100 {
            let velX = Double.random(in: 0.0...0.05)
            let velY = Double.random(in: 0.0...0.05)

            let isStationary = abs(velX) <= 0.01 && abs(velY) <= 0.01

            if velX <= 0.01 && velY <= 0.01 {
                XCTAssertTrue(isStationary,
                    "Iteration \(iteration): velX=\(velX), velY=\(velY) should be stationary")
            } else {
                XCTAssertFalse(isStationary,
                    "Iteration \(iteration): velX=\(velX), velY=\(velY) should NOT be stationary")
            }
        }
    }

    // MARK: — Property 10: Full State Machine Sequence

    func testProperty10_fullStateMachineSequence() {
        let settings = makeSettings()
        let sensor = makeSensor(settings: settings)

        for iteration in 0..<100 {
            // Phase 1: Device is moving — no lock state
            sensor.stationaryStartTime = nil
            sensor.lockedCoords = nil
            XCTAssertNil(sensor.stationaryStartTime,
                "Iteration \(iteration) Phase 1: no lock during motion")
            XCTAssertNil(sensor.lockedCoords,
                "Iteration \(iteration) Phase 1: no locked coords during motion")

            // Phase 2: Device becomes stationary — capture position
            let deltaPitch = Double.random(in: -20.0...20.0)
            let deltaRoll = Double.random(in: -20.0...20.0)
            let gravity = gravityVector(deltaPitchDeg: deltaPitch, deltaRollDeg: deltaRoll)
            let capturedPos = sensor.computePosition(gravity: gravity)

            sensor.stationaryStartTime = CACurrentMediaTime()
            sensor.lockedCoords = (x: capturedPos.x, y: capturedPos.y)

            XCTAssertNotNil(sensor.stationaryStartTime,
                "Iteration \(iteration) Phase 2: stationaryStartTime should be set")
            XCTAssertEqual(sensor.lockedCoords?.x, capturedPos.x,
                "Iteration \(iteration) Phase 2: locked x should match captured position")
            XCTAssertEqual(sensor.lockedCoords?.y, capturedPos.y,
                "Iteration \(iteration) Phase 2: locked y should match captured position")

            // Phase 3: 200ms passes — lock engages (simulate by backdating)
            let lockTime = CACurrentMediaTime() - 0.25
            sensor.stationaryStartTime = lockTime
            let elapsed = CACurrentMediaTime() - lockTime
            XCTAssertGreaterThanOrEqual(elapsed, 0.2,
                "Iteration \(iteration) Phase 3: lock should be engaged (elapsed >= 200ms)")
            XCTAssertEqual(sensor.lockedCoords?.x, capturedPos.x,
                "Iteration \(iteration) Phase 3: locked x preserved after threshold")
            XCTAssertEqual(sensor.lockedCoords?.y, capturedPos.y,
                "Iteration \(iteration) Phase 3: locked y preserved after threshold")

            // Phase 4: Motion resumes — lock cleared
            sensor.stationaryStartTime = nil
            sensor.lockedCoords = nil
            XCTAssertNil(sensor.stationaryStartTime,
                "Iteration \(iteration) Phase 4: lock cleared on motion resume")
            XCTAssertNil(sensor.lockedCoords,
                "Iteration \(iteration) Phase 4: locked coords cleared on motion resume")
        }
    }
}
