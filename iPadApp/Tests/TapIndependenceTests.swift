import CoreMotion
import XCTest
@testable import DesktopAgent

/// Property-based test for tap independence.
///
/// **Property 12: Tap Independence**
/// *For any* CMDeviceMotion frame, the screen coordinates produced by `computePosition`
/// SHALL be identical regardless of whether `userAcceleration` contains an impulse spike
/// (tap) or not — the position computation uses only the gravity vector.
///
/// **Validates: Requirements 8.3**
final class TapIndependenceTests: XCTestCase {

    /// Unique suite name regenerated per test run to guarantee isolation.
    private var suiteName: String!
    private var suite: UserDefaults!

    override func setUp() {
        super.setUp()
        suiteName = "com.desktopagent.test.tapIndependence.\(UUID().uuidString)"
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

    /// Generates a random gravity-like unit vector.
    private func randomGravityVector() -> (x: Double, y: Double, z: Double) {
        var x: Double
        var y: Double
        var z: Double
        var magnitude: Double

        repeat {
            x = Double.random(in: -1.0...1.0)
            y = Double.random(in: -1.0...1.0)
            z = Double.random(in: -1.0...1.0)
            magnitude = sqrt(x * x + y * y + z * z)
        } while magnitude < 1e-10

        return (x / magnitude, y / magnitude, z / magnitude)
    }

    // MARK: — Property 12: Tap Independence (150 iterations)

    /// For 150 random gravity vectors, calls `computePosition` multiple times with the
    /// same gravity input (simulating different frames where userAcceleration varies).
    /// Verifies the output is always identical — proving the function depends ONLY on
    /// the gravity parameter and is deterministic for the same input.
    ///
    /// Since `computePosition(gravity:)` accepts only a CMAcceleration (the gravity
    /// component), it structurally cannot read userAcceleration. This test confirms
    /// that guarantee holds: same gravity in → same position out, every time.
    func testProperty12_computePositionDeterministicForSameGravity() {
        let settings = SettingsStore(defaults: suite)
        settings.tiltPositionMode = true
        settings.tiltRange = 25.0
        settings.tiltDeadZone = 1.5
        settings.tiltInverted = false
        settings.neutralGravityX = -0.707
        settings.neutralGravityY = 0.0
        settings.neutralGravityZ = -0.707

        let ws = WebSocketManager()
        let sensor = TiltSensor(ws: ws, settings: settings)

        for iteration in 0..<150 {
            let g = randomGravityVector()
            let gravity = CMAcceleration(x: g.x, y: g.y, z: g.z)

            // Call computePosition multiple times with the SAME gravity vector.
            // Each call simulates a different "frame" where userAcceleration would
            // be different (quiet vs tap impulse), but since computePosition only
            // takes gravity, the output must be identical every time.
            let result1 = sensor.computePosition(gravity: gravity)
            let result2 = sensor.computePosition(gravity: gravity)
            let result3 = sensor.computePosition(gravity: gravity)

            XCTAssertEqual(result1.x, result2.x, accuracy: 0.0,
                "Iteration \(iteration): x differs between call 1 (\(result1.x)) and call 2 (\(result2.x)) for same gravity")
            XCTAssertEqual(result1.y, result2.y, accuracy: 0.0,
                "Iteration \(iteration): y differs between call 1 (\(result1.y)) and call 2 (\(result2.y)) for same gravity")
            XCTAssertEqual(result1.x, result3.x, accuracy: 0.0,
                "Iteration \(iteration): x differs between call 1 (\(result1.x)) and call 3 (\(result3.x)) for same gravity")
            XCTAssertEqual(result1.y, result3.y, accuracy: 0.0,
                "Iteration \(iteration): y differs between call 1 (\(result1.y)) and call 3 (\(result3.y)) for same gravity")
        }
    }

    // MARK: — Property 12: Different gravity vectors produce different outputs

    /// Verifies that `computePosition` actually depends on the gravity parameter —
    /// different gravity vectors (sufficiently far apart) produce different outputs.
    /// This confirms the function isn't trivially constant (which would vacuously
    /// satisfy tap independence but be useless).
    func testProperty12_differentGravityProducesDifferentOutput() {
        let settings = SettingsStore(defaults: suite)
        settings.tiltPositionMode = true
        settings.tiltRange = 25.0
        settings.tiltDeadZone = 1.5
        settings.tiltInverted = false
        settings.neutralGravityX = -0.707
        settings.neutralGravityY = 0.0
        settings.neutralGravityZ = -0.707

        let ws = WebSocketManager()
        let sensor = TiltSensor(ws: ws, settings: settings)

        var distinctOutputCount = 0

        for _ in 0..<100 {
            let g1 = randomGravityVector()
            let g2 = randomGravityVector()
            let gravity1 = CMAcceleration(x: g1.x, y: g1.y, z: g1.z)
            let gravity2 = CMAcceleration(x: g2.x, y: g2.y, z: g2.z)

            let result1 = sensor.computePosition(gravity: gravity1)
            let result2 = sensor.computePosition(gravity: gravity2)

            if result1.x != result2.x || result1.y != result2.y {
                distinctOutputCount += 1
            }
        }

        // With random gravity vectors, the vast majority should produce different outputs.
        // Allow some collisions due to clamping/dead zone, but at least 80% should differ.
        XCTAssertGreaterThan(distinctOutputCount, 80,
            "computePosition should produce distinct outputs for distinct gravity vectors (got \(distinctOutputCount)/100 distinct)")
    }

    // MARK: — Property 12: No hidden state mutation between calls

    /// Verifies that calling computePosition with various gravity vectors does NOT
    /// mutate any hidden state that would affect subsequent calls. This proves the
    /// function is pure with respect to its gravity input — no side effects from
    /// "tap-like" frames can corrupt future position computations.
    func testProperty12_noHiddenStateMutationBetweenCalls() {
        let settings = SettingsStore(defaults: suite)
        settings.tiltPositionMode = true
        settings.tiltRange = 25.0
        settings.tiltDeadZone = 1.5
        settings.tiltInverted = false
        settings.neutralGravityX = -0.707
        settings.neutralGravityY = 0.0
        settings.neutralGravityZ = -0.707

        let ws = WebSocketManager()
        let sensor = TiltSensor(ws: ws, settings: settings)

        for iteration in 0..<100 {
            let target = randomGravityVector()
            let targetGravity = CMAcceleration(x: target.x, y: target.y, z: target.z)

            // First: compute position for our target gravity
            let baseline = sensor.computePosition(gravity: targetGravity)

            // Simulate "intervening frames" with different gravity vectors
            // (as if the device was moving and experiencing taps between frames)
            for _ in 0..<5 {
                let other = randomGravityVector()
                _ = sensor.computePosition(gravity: CMAcceleration(x: other.x, y: other.y, z: other.z))
            }

            // Re-compute position for the SAME target gravity — must be identical
            let afterOtherCalls = sensor.computePosition(gravity: targetGravity)

            XCTAssertEqual(baseline.x, afterOtherCalls.x, accuracy: 0.0,
                "Iteration \(iteration): x changed after intervening calls — hidden state mutation detected")
            XCTAssertEqual(baseline.y, afterOtherCalls.y, accuracy: 0.0,
                "Iteration \(iteration): y changed after intervening calls — hidden state mutation detected")
        }
    }

    // MARK: — Edge case: gravity vectors during simulated tap impulses

    /// Tests with a stable gravity vector across 20 consecutive "frames".
    /// During a real tap, Core Motion separates gravity from userAcceleration —
    /// the gravity vector remains stable while userAcceleration spikes.
    /// computePosition must return identical values for all frames.
    func testProperty12_gravityStableDuringSimulatedTapSequence() {
        let settings = SettingsStore(defaults: suite)
        settings.tiltPositionMode = true
        settings.tiltRange = 25.0
        settings.tiltDeadZone = 1.5
        settings.tiltInverted = false
        settings.neutralGravityX = -0.707
        settings.neutralGravityY = 0.0
        settings.neutralGravityZ = -0.707

        let ws = WebSocketManager()
        let sensor = TiltSensor(ws: ws, settings: settings)

        // Simulate a sequence: same gravity vector across multiple "frames"
        // In reality, during a tap, gravity stays constant while userAcceleration spikes.
        let stable = randomGravityVector()
        let stableGravity = CMAcceleration(x: stable.x, y: stable.y, z: stable.z)

        var results: [(x: Double, y: Double)] = []
        for _ in 0..<20 {
            let result = sensor.computePosition(gravity: stableGravity)
            results.append(result)
        }

        // All 20 results must be identical
        let firstResult = results[0]
        for (index, result) in results.enumerated() {
            XCTAssertEqual(result.x, firstResult.x, accuracy: 0.0,
                "Frame \(index): x differs from frame 0 for stable gravity during simulated tap sequence")
            XCTAssertEqual(result.y, firstResult.y, accuracy: 0.0,
                "Frame \(index): y differs from frame 0 for stable gravity during simulated tap sequence")
        }
    }
}
