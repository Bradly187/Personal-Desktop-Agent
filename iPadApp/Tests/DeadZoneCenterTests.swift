import XCTest
import CoreMotion
@testable import DesktopAgent

/// Property-based test for dead zone behavior.
///
/// **Property 7: Dead Zone Maps to Center**
/// *For any* angular displacement from neutral that is less than the configured
/// `tiltDeadZone` (in degrees) on both axes, `computePosition` SHALL produce
/// screen coordinates of exactly (0.5, 0.5).
///
/// **Validates: Requirements 6.1**
@MainActor
final class DeadZoneCenterTests: XCTestCase {

    /// Unique suite name regenerated per test run to guarantee isolation.
    private var suiteName: String!
    private var suite: UserDefaults!

    override func setUp() {
        super.setUp()
        suiteName = "com.desktopagent.test.deadZoneCenter.\(UUID().uuidString)"
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

    /// Default neutral gravity: iPad at ~45° from horizontal.
    private let defaultNeutral = SIMD3<Double>(-0.707, 0.0, -0.707)

    /// Constructs a gravity vector that produces the desired delta pitch and delta roll
    /// angles (in degrees) relative to the default neutral gravity vector.
    ///
    /// Reverses the atan2 decomposition used in `computePosition`:
    /// - neutralPitch = atan2(-neutral.x, -neutral.z)
    /// - neutralRoll  = atan2(-neutral.y, -neutral.z)
    /// - currentPitch = neutralPitch + deltaPitchRad
    /// - currentRoll  = neutralRoll + deltaRollRad
    ///
    /// Reconstructs gravity so that both pitch and roll share a consistent g.z:
    ///   g.z = -cos(targetPitch)
    ///   g.x = -sin(targetPitch)
    ///   g.y = -cos(targetPitch) * tan(targetRoll)
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

    // MARK: — Property 7: Dead Zone Maps to Center (150 randomized iterations)

    /// Generates random angular displacements where both |deltaPitch| < deadZone AND
    /// |deltaRoll| < deadZone, constructs the corresponding gravity vector, and verifies
    /// that computePosition returns exactly (0.5, 0.5).
    func testProperty7_deadZoneMapsToCenter() {
        suite.set(1.5, forKey: "tiltDeadZone")
        suite.set(false, forKey: "tiltInverted")
        suite.set(25.0, forKey: "tiltRange")
        suite.set(true, forKey: "tiltPositionMode")

        let settings = SettingsStore(defaults: suite)
        let ws = WebSocketManager()
        let sensor = TiltSensor(ws: ws, settings: settings)
        sensor.neutralGravity = defaultNeutral

        let deadZone = settings.tiltDeadZone  // 1.5 degrees
        XCTAssertEqual(deadZone, 1.5, "Default dead zone should be 1.5 degrees")

        for iteration in 0..<150 {
            // Generate random displacements strictly within the dead zone on both axes
            let deltaPitch = Double.random(in: -(deadZone - 0.001)...(deadZone - 0.001))
            let deltaRoll = Double.random(in: -(deadZone - 0.001)...(deadZone - 0.001))

            // Sanity check: both within dead zone
            precondition(abs(deltaPitch) < deadZone)
            precondition(abs(deltaRoll) < deadZone)

            let gravity = gravityVector(deltaPitchDeg: deltaPitch, deltaRollDeg: deltaRoll)
            let result = sensor.computePosition(gravity: gravity)

            XCTAssertEqual(result.x, 0.5,
                "Iteration \(iteration): x should be 0.5 in dead zone (deltaPitch=\(deltaPitch)°, deltaRoll=\(deltaRoll)°), got \(result.x)")
            XCTAssertEqual(result.y, 0.5,
                "Iteration \(iteration): y should be 0.5 in dead zone (deltaPitch=\(deltaPitch)°, deltaRoll=\(deltaRoll)°), got \(result.y)")
        }
    }

    // MARK: — Property 7: Dead zone with various dead zone sizes

    /// Tests that the dead zone property holds for different configured dead zone values.
    /// Generates random dead zone sizes and random displacements within them.
    func testProperty7_deadZoneWithVariousSizes() {
        let ws = WebSocketManager()

        for iteration in 0..<100 {
            // Clean suite between iterations
            for key in suite.dictionaryRepresentation().keys {
                suite.removeObject(forKey: key)
            }

            // Set a random dead zone between 0.5 and 5.0 degrees
            let randomDeadZone = Double.random(in: 0.5...5.0)
            suite.set(randomDeadZone, forKey: "tiltDeadZone")
            suite.set(false, forKey: "tiltInverted")
            suite.set(25.0, forKey: "tiltRange")
            suite.set(true, forKey: "tiltPositionMode")

            let settings = SettingsStore(defaults: suite)
            let sensor = TiltSensor(ws: ws, settings: settings)
            sensor.neutralGravity = defaultNeutral

            // Generate displacement strictly within the dead zone
            let maxDelta = randomDeadZone - 0.001
            let deltaPitch = Double.random(in: -maxDelta...maxDelta)
            let deltaRoll = Double.random(in: -maxDelta...maxDelta)

            let gravity = gravityVector(deltaPitchDeg: deltaPitch, deltaRollDeg: deltaRoll)
            let result = sensor.computePosition(gravity: gravity)

            XCTAssertEqual(result.x, 0.5,
                "Iteration \(iteration): x should be 0.5 (deadZone=\(randomDeadZone)°, deltaRoll=\(deltaRoll)°), got \(result.x)")
            XCTAssertEqual(result.y, 0.5,
                "Iteration \(iteration): y should be 0.5 (deadZone=\(randomDeadZone)°, deltaPitch=\(deltaPitch)°), got \(result.y)")
        }
    }

    // MARK: — Property 7: Zero displacement always maps to center

    /// The trivial case: zero displacement on both axes should always produce (0.5, 0.5).
    func testProperty7_zeroDisplacementMapsToCenter() {
        suite.set(1.5, forKey: "tiltDeadZone")
        suite.set(false, forKey: "tiltInverted")
        suite.set(25.0, forKey: "tiltRange")
        suite.set(true, forKey: "tiltPositionMode")

        let settings = SettingsStore(defaults: suite)
        let ws = WebSocketManager()
        let sensor = TiltSensor(ws: ws, settings: settings)
        sensor.neutralGravity = defaultNeutral

        // Gravity exactly at neutral position
        let gravity = CMAcceleration(x: -0.707, y: 0.0, z: -0.707)
        let result = sensor.computePosition(gravity: gravity)

        XCTAssertEqual(result.x, 0.5, "Zero displacement x should be 0.5, got \(result.x)")
        XCTAssertEqual(result.y, 0.5, "Zero displacement y should be 0.5, got \(result.y)")
    }

    // MARK: — Property 7: Displacement at dead zone boundary (just inside)

    /// Displacements just barely inside the dead zone boundary should still map to center.
    func testProperty7_justInsideDeadZoneBoundary() {
        suite.set(1.5, forKey: "tiltDeadZone")
        suite.set(false, forKey: "tiltInverted")
        suite.set(25.0, forKey: "tiltRange")
        suite.set(true, forKey: "tiltPositionMode")

        let settings = SettingsStore(defaults: suite)
        let ws = WebSocketManager()
        let sensor = TiltSensor(ws: ws, settings: settings)
        sensor.neutralGravity = defaultNeutral

        let deadZone = settings.tiltDeadZone  // 1.5 degrees
        let justInside = deadZone - 0.01  // 1.49 degrees — inside dead zone

        // Test all four quadrants just inside the boundary
        let testCases: [(pitch: Double, roll: Double)] = [
            (justInside, justInside),
            (-justInside, justInside),
            (justInside, -justInside),
            (-justInside, -justInside),
            (justInside, 0.0),
            (0.0, justInside),
            (-justInside, 0.0),
            (0.0, -justInside),
        ]

        for (index, testCase) in testCases.enumerated() {
            let gravity = gravityVector(deltaPitchDeg: testCase.pitch, deltaRollDeg: testCase.roll)
            let result = sensor.computePosition(gravity: gravity)

            XCTAssertEqual(result.x, 0.5,
                "Case \(index): x should be 0.5 at boundary (pitch=\(testCase.pitch)°, roll=\(testCase.roll)°), got \(result.x)")
            XCTAssertEqual(result.y, 0.5,
                "Case \(index): y should be 0.5 at boundary (pitch=\(testCase.pitch)°, roll=\(testCase.roll)°), got \(result.y)")
        }
    }

    // MARK: — Property 7: One axis in dead zone, one outside

    /// When only one axis is within the dead zone, that axis should be 0.5 but the other
    /// axis should NOT be 0.5 (it's outside the dead zone). This verifies axes are independent.
    func testProperty7_singleAxisInDeadZone() {
        suite.set(1.5, forKey: "tiltDeadZone")
        suite.set(false, forKey: "tiltInverted")
        suite.set(25.0, forKey: "tiltRange")
        suite.set(true, forKey: "tiltPositionMode")

        let settings = SettingsStore(defaults: suite)
        let ws = WebSocketManager()
        let sensor = TiltSensor(ws: ws, settings: settings)
        sensor.neutralGravity = defaultNeutral

        let deadZone = settings.tiltDeadZone  // 1.5 degrees

        for iteration in 0..<50 {
            // Roll inside dead zone, pitch outside
            let smallRoll = Double.random(in: -(deadZone - 0.01)...(deadZone - 0.01))
            let largePitch = Double.random(in: 3.0...20.0) * (Bool.random() ? 1.0 : -1.0)

            let gravity = gravityVector(deltaPitchDeg: largePitch, deltaRollDeg: smallRoll)
            let result = sensor.computePosition(gravity: gravity)

            // Roll is in dead zone → x should be 0.5
            XCTAssertEqual(result.x, 0.5,
                "Iteration \(iteration): x should be 0.5 when roll (\(smallRoll)°) is in dead zone, got \(result.x)")
            // Pitch is outside dead zone → y should NOT be 0.5
            XCTAssertNotEqual(result.y, 0.5,
                "Iteration \(iteration): y should NOT be 0.5 when pitch (\(largePitch)°) is outside dead zone, got \(result.y)")
        }
    }

    // MARK: — Property 7: Dead zone with inversion still maps to center

    /// When tiltInverted is enabled, displacements within the dead zone should still
    /// produce (0.5, 0.5) because inverting center (0.5, 0.5) yields (0.5, 0.5).
    func testProperty7_deadZoneWithInversionStillCenter() {
        suite.set(1.5, forKey: "tiltDeadZone")
        suite.set(true, forKey: "tiltInverted")
        suite.set(25.0, forKey: "tiltRange")
        suite.set(true, forKey: "tiltPositionMode")

        let settings = SettingsStore(defaults: suite)
        let ws = WebSocketManager()
        let sensor = TiltSensor(ws: ws, settings: settings)
        sensor.neutralGravity = defaultNeutral

        let deadZone = settings.tiltDeadZone

        for iteration in 0..<50 {
            let deltaPitch = Double.random(in: -(deadZone - 0.001)...(deadZone - 0.001))
            let deltaRoll = Double.random(in: -(deadZone - 0.001)...(deadZone - 0.001))

            let gravity = gravityVector(deltaPitchDeg: deltaPitch, deltaRollDeg: deltaRoll)
            let result = sensor.computePosition(gravity: gravity)

            XCTAssertEqual(result.x, 0.5,
                "Iteration \(iteration): inverted x should be 0.5 in dead zone, got \(result.x)")
            XCTAssertEqual(result.y, 0.5,
                "Iteration \(iteration): inverted y should be 0.5 in dead zone, got \(result.y)")
        }
    }
}
