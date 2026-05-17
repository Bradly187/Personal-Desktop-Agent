import XCTest
import CoreMotion
@testable import DesktopAgent

/// Property-based test for recalibration recentering.
///
/// **Property 4: Recalibration Recenters Output**
/// *For any* gravity vector that equals the newly calibrated neutral position,
/// `computePosition` SHALL produce screen coordinates of exactly (0.5, 0.5) —
/// the screen center.
///
/// **Validates: Requirements 2.5**
@MainActor
final class RecalibrationRecentersTests: XCTestCase {

    /// Unique suite name regenerated per test run to guarantee isolation.
    private var suiteName: String!
    private var suite: UserDefaults!

    override func setUp() {
        super.setUp()
        suiteName = "com.desktopagent.test.recalibrationRecenters.\(UUID().uuidString)"
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

    /// Generates a random unit vector by normalizing a random (x, y, z) triple.
    /// Avoids zero-magnitude vectors by regenerating if all components are near zero.
    private func randomUnitVector() -> (x: Double, y: Double, z: Double) {
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

    // MARK: — Property 4: Recalibration Recenters Output (150 iterations)

    /// For 150 random gravity vectors:
    /// 1. Set the gravity vector as the neutral position on the TiltSensor directly
    /// 2. Call computePosition with the same gravity as CMAcceleration
    /// 3. Verify output is exactly (0.5, 0.5)
    ///
    /// When gravity == neutral, delta angles are 0 on both axes, which falls within
    /// the dead zone, so the output must always be screen center.
    func testProperty4_recalibrationRecentersOutputToCenter() {
        suite.set(25.0, forKey: "tiltRange")
        suite.set(1.5, forKey: "tiltDeadZone")
        suite.set(false, forKey: "tiltInverted")
        suite.set(true, forKey: "tiltPositionMode")

        let settings = SettingsStore(defaults: suite)
        let ws = WebSocketManager()
        let sensor = TiltSensor(ws: ws, settings: settings)

        for iteration in 0..<150 {
            let vector = randomUnitVector()

            // 1. Set the gravity vector as the neutral position directly
            sensor.neutralGravity = SIMD3(vector.x, vector.y, vector.z)

            // 2. Compute position with the same gravity as CMAcceleration
            let gravity = CMAcceleration(x: vector.x, y: vector.y, z: vector.z)
            let result = sensor.computePosition(gravity: gravity)

            // 3. Output must be exactly (0.5, 0.5) — screen center
            XCTAssertEqual(result.x, 0.5, accuracy: 1e-10,
                "Iteration \(iteration): x should be 0.5 when gravity == neutral, got \(result.x) for vector (\(vector.x), \(vector.y), \(vector.z))")
            XCTAssertEqual(result.y, 0.5, accuracy: 1e-10,
                "Iteration \(iteration): y should be 0.5 when gravity == neutral, got \(result.y) for vector (\(vector.x), \(vector.y), \(vector.z))")
        }
    }

    // MARK: — Property 4: Varying tiltRange still recenters

    /// Verifies that recalibration recenters regardless of the configured tiltRange.
    /// The dead zone and linear mapping should both produce center when delta is 0.
    func testProperty4_recentersRegardlessOfTiltRange() {
        let ws = WebSocketManager()

        for iteration in 0..<100 {
            for key in suite.dictionaryRepresentation().keys {
                suite.removeObject(forKey: key)
            }

            let vector = randomUnitVector()
            let tiltRange = Double.random(in: 5.0...60.0)

            suite.set(tiltRange, forKey: "tiltRange")
            suite.set(1.5, forKey: "tiltDeadZone")
            suite.set(false, forKey: "tiltInverted")
            suite.set(true, forKey: "tiltPositionMode")

            let settings = SettingsStore(defaults: suite)
            let sensor = TiltSensor(ws: ws, settings: settings)

            // Set neutral directly on the sensor
            sensor.neutralGravity = SIMD3(vector.x, vector.y, vector.z)

            let gravity = CMAcceleration(x: vector.x, y: vector.y, z: vector.z)
            let result = sensor.computePosition(gravity: gravity)

            XCTAssertEqual(result.x, 0.5, accuracy: 1e-10,
                "Iteration \(iteration): x should be 0.5 with tiltRange=\(tiltRange), got \(result.x)")
            XCTAssertEqual(result.y, 0.5, accuracy: 1e-10,
                "Iteration \(iteration): y should be 0.5 with tiltRange=\(tiltRange), got \(result.y)")
        }
    }

    // MARK: — Property 4: Recenters with inversion enabled

    /// When tiltInverted is true and gravity == neutral, output should still be (0.5, 0.5).
    /// Inversion of center is still center: 1.0 - 0.5 = 0.5.
    func testProperty4_recentersWithInversionEnabled() {
        let ws = WebSocketManager()

        for iteration in 0..<100 {
            for key in suite.dictionaryRepresentation().keys {
                suite.removeObject(forKey: key)
            }

            let vector = randomUnitVector()

            suite.set(25.0, forKey: "tiltRange")
            suite.set(1.5, forKey: "tiltDeadZone")
            suite.set(true, forKey: "tiltInverted")
            suite.set(true, forKey: "tiltPositionMode")

            let settings = SettingsStore(defaults: suite)
            let sensor = TiltSensor(ws: ws, settings: settings)

            // Set neutral directly on the sensor
            sensor.neutralGravity = SIMD3(vector.x, vector.y, vector.z)

            let gravity = CMAcceleration(x: vector.x, y: vector.y, z: vector.z)
            let result = sensor.computePosition(gravity: gravity)

            XCTAssertEqual(result.x, 0.5, accuracy: 1e-10,
                "Iteration \(iteration): x should be 0.5 with inversion, got \(result.x)")
            XCTAssertEqual(result.y, 0.5, accuracy: 1e-10,
                "Iteration \(iteration): y should be 0.5 with inversion, got \(result.y)")
        }
    }

    // MARK: — Edge cases: axis-aligned gravity vectors

    /// Tests recalibration recentering with axis-aligned unit vectors,
    /// which are edge cases for the atan2 computation.
    func testProperty4_axisAlignedVectorsRecenter() {
        suite.set(25.0, forKey: "tiltRange")
        suite.set(1.5, forKey: "tiltDeadZone")
        suite.set(false, forKey: "tiltInverted")
        suite.set(true, forKey: "tiltPositionMode")

        let settings = SettingsStore(defaults: suite)
        let ws = WebSocketManager()
        let sensor = TiltSensor(ws: ws, settings: settings)

        let axisVectors: [(x: Double, y: Double, z: Double)] = [
            (1.0, 0.0, 0.0),    // pure X (device on its side)
            (-1.0, 0.0, 0.0),   // pure -X
            (0.0, 1.0, 0.0),    // pure Y (device upside down landscape)
            (0.0, -1.0, 0.0),   // pure -Y
            (0.0, 0.0, 1.0),    // pure Z (device face down)
            (0.0, 0.0, -1.0),   // pure -Z (device face up, flat)
        ]

        for (index, vector) in axisVectors.enumerated() {
            // Set neutral directly on the sensor
            sensor.neutralGravity = SIMD3(vector.x, vector.y, vector.z)

            let gravity = CMAcceleration(x: vector.x, y: vector.y, z: vector.z)
            let result = sensor.computePosition(gravity: gravity)

            XCTAssertEqual(result.x, 0.5, accuracy: 1e-10,
                "Axis vector \(index) (\(vector.x), \(vector.y), \(vector.z)): x should be 0.5, got \(result.x)")
            XCTAssertEqual(result.y, 0.5, accuracy: 1e-10,
                "Axis vector \(index) (\(vector.x), \(vector.y), \(vector.z)): y should be 0.5, got \(result.y)")
        }
    }

    // MARK: — Edge case: default 45° neutral

    /// Verifies the default neutral position (~45° from horizontal) recenters correctly.
    func testProperty4_default45DegreeNeutralRecenters() {
        suite.set(25.0, forKey: "tiltRange")
        suite.set(1.5, forKey: "tiltDeadZone")
        suite.set(false, forKey: "tiltInverted")
        suite.set(true, forKey: "tiltPositionMode")

        let settings = SettingsStore(defaults: suite)
        let ws = WebSocketManager()
        let sensor = TiltSensor(ws: ws, settings: settings)

        // Set neutral directly
        sensor.neutralGravity = SIMD3(-0.7071067811865476, 0.0, -0.7071067811865476)

        let gravity = CMAcceleration(x: -0.7071067811865476, y: 0.0, z: -0.7071067811865476)
        let result = sensor.computePosition(gravity: gravity)

        XCTAssertEqual(result.x, 0.5, accuracy: 1e-10,
            "Default 45° neutral: x should be 0.5, got \(result.x)")
        XCTAssertEqual(result.y, 0.5, accuracy: 1e-10,
            "Default 45° neutral: y should be 0.5, got \(result.y)")
    }
}
