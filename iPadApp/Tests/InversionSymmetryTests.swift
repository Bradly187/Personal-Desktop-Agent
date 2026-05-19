import XCTest
import CoreMotion
@testable import DesktopAgent

/// Property-based test for inversion symmetry.
///
/// **Property 11: Inversion Symmetry**
/// *For any* tilt angle, computing the position with `tiltInverted = false` yielding (x, y)
/// and then with `tiltInverted = true` SHALL yield (1.0 - x, 1.0 - y).
///
/// **Validates: Requirements 7.1**
@MainActor
final class InversionSymmetryTests: XCTestCase {

    /// Unique suite name regenerated per test run to guarantee isolation.
    private var suiteName: String!
    private var suite: UserDefaults!

    override func setUp() {
        super.setUp()
        suiteName = "com.desktopagent.test.inversionSymmetry.\(UUID().uuidString)"
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

    /// Generates a random gravity vector that produces angular displacements within tiltRange
    /// from the given neutral gravity. This avoids clamping interference which would break
    /// the inversion symmetry at extremes.
    ///
    /// Strategy: start from neutral, apply random pitch/roll deltas within tiltRange,
    /// then reconstruct the gravity vector.
    private func randomGravityWithinRange(
        neutral: SIMD3<Double>,
        tiltRange: Double
    ) -> CMAcceleration {
        // Compute neutral pitch and roll
        let neutralPitch = atan2(-neutral.x, -neutral.z)
        let neutralRoll = atan2(-neutral.y, -neutral.z)

        // Generate random angular deltas within tiltRange (in radians)
        // Use 95% of tiltRange to stay safely within bounds and avoid floating-point edge clamping
        let maxDeltaRad = (tiltRange * 0.95) * (.pi / 180.0)
        let deltaPitch = Double.random(in: -maxDeltaRad...maxDeltaRad)
        let deltaRoll = Double.random(in: -maxDeltaRad...maxDeltaRad)

        // Target pitch and roll
        let targetPitch = neutralPitch + deltaPitch
        let targetRoll = neutralRoll + deltaRoll

        // Reconstruct gravity vector from pitch and roll angles.
        // Given: pitch = atan2(-g.x, -g.z) and roll = atan2(-g.y, -g.z)
        // We need g.x, g.y, g.z such that these atan2 relations hold.
        // Choose g.z = -cos(targetPitch) (from pitch definition with unit projection)
        // Then g.x = -sin(targetPitch) * abs(g.z) / cos(targetPitch) ... 
        // Simpler: use the atan2 inverse directly.
        // Let g.z = -1 (pointing down), then:
        //   pitch = atan2(-g.x, 1) → g.x = -tan(targetPitch)
        //   roll = atan2(-g.y, 1) → g.y = -tan(targetRoll)
        // Then normalize.
        let gx = -tan(targetPitch)
        let gy = -tan(targetRoll)
        let gz = -1.0

        // Normalize to unit vector (gravity vectors are approximately unit length)
        let mag = sqrt(gx * gx + gy * gy + gz * gz)
        return CMAcceleration(x: gx / mag, y: gy / mag, z: gz / mag)
    }

    // MARK: — Property 11: Inversion Symmetry (150 iterations)

    /// For 150 random gravity vectors within tiltRange:
    /// 1. Compute position with tiltInverted = false → (x, y)
    /// 2. Compute position with tiltInverted = true → (x2, y2)
    /// 3. Verify x2 ≈ 1.0 - x and y2 ≈ 1.0 - y (within floating-point epsilon)
    func testProperty11_inversionSymmetry() {
        let ws = WebSocketManager()
        let settings = SettingsStore(defaults: suite)

        // Set dead zone to 0 to avoid dead zone interference with the symmetry property
        settings.tiltDeadZone = 0.0
        settings.tiltRange = 25.0
        settings.tiltPositionMode = true

        let sensor = TiltSensor(ws: ws, settings: settings)

        let epsilon = 1e-10

        for iteration in 0..<150 {
            let gravity = randomGravityWithinRange(
                neutral: sensor.neutralGravity,
                tiltRange: settings.tiltRange
            )

            // Compute with inversion OFF
            settings.tiltInverted = false
            let normal = sensor.computePosition(gravity: gravity)

            // Compute with inversion ON
            settings.tiltInverted = true
            let inverted = sensor.computePosition(gravity: gravity)

            // Verify inversion symmetry: inverted == (1.0 - normal)
            XCTAssertEqual(inverted.x, 1.0 - normal.x, accuracy: epsilon,
                "Iteration \(iteration): inverted.x (\(inverted.x)) should equal 1.0 - normal.x (\(1.0 - normal.x))")
            XCTAssertEqual(inverted.y, 1.0 - normal.y, accuracy: epsilon,
                "Iteration \(iteration): inverted.y (\(inverted.y)) should equal 1.0 - normal.y (\(1.0 - normal.y))")
        }
    }

    // MARK: — Property 11: Inversion symmetry with varying tiltRange

    /// Verifies inversion symmetry holds across different tiltRange values.
    func testProperty11_inversionSymmetryAcrossTiltRanges() {
        let ws = WebSocketManager()
        let settings = SettingsStore(defaults: suite)
        settings.tiltDeadZone = 0.0
        settings.tiltPositionMode = true

        let sensor = TiltSensor(ws: ws, settings: settings)

        let epsilon = 1e-10
        let tiltRanges: [Double] = [5.0, 10.0, 15.0, 25.0, 40.0, 60.0]

        for tiltRange in tiltRanges {
            settings.tiltRange = tiltRange

            for _ in 0..<20 {
                let gravity = randomGravityWithinRange(
                    neutral: sensor.neutralGravity,
                    tiltRange: tiltRange
                )

                settings.tiltInverted = false
                let normal = sensor.computePosition(gravity: gravity)

                settings.tiltInverted = true
                let inverted = sensor.computePosition(gravity: gravity)

                XCTAssertEqual(inverted.x, 1.0 - normal.x, accuracy: epsilon,
                    "tiltRange=\(tiltRange): inverted.x (\(inverted.x)) != 1.0 - normal.x (\(1.0 - normal.x))")
                XCTAssertEqual(inverted.y, 1.0 - normal.y, accuracy: epsilon,
                    "tiltRange=\(tiltRange): inverted.y (\(inverted.y)) != 1.0 - normal.y (\(1.0 - normal.y))")
            }
        }
    }

    // MARK: — Property 11: Inversion symmetry with custom neutral positions

    /// Verifies inversion symmetry holds regardless of the calibrated neutral position.
    func testProperty11_inversionSymmetryWithCustomNeutral() {
        let ws = WebSocketManager()
        let settings = SettingsStore(defaults: suite)
        settings.tiltDeadZone = 0.0
        settings.tiltRange = 25.0
        settings.tiltPositionMode = true

        let sensor = TiltSensor(ws: ws, settings: settings)

        let epsilon = 1e-10

        // Test with various neutral positions (different holding angles)
        let neutralPositions: [SIMD3<Double>] = [
            SIMD3(-0.707, 0.0, -0.707),   // Default 45°
            SIMD3(0.0, 0.0, -1.0),         // Flat on table
            SIMD3(-0.5, 0.0, -0.866),      // 30° from horizontal
            SIMD3(-0.866, 0.0, -0.5),      // 60° from horizontal
            SIMD3(-0.3, -0.2, -0.93),      // Slightly rolled
        ]

        for neutral in neutralPositions {
            sensor.neutralGravity = neutral

            for _ in 0..<25 {
                let gravity = randomGravityWithinRange(
                    neutral: neutral,
                    tiltRange: settings.tiltRange
                )

                settings.tiltInverted = false
                let normal = sensor.computePosition(gravity: gravity)

                settings.tiltInverted = true
                let inverted = sensor.computePosition(gravity: gravity)

                XCTAssertEqual(inverted.x, 1.0 - normal.x, accuracy: epsilon,
                    "Custom neutral \(neutral): inverted.x (\(inverted.x)) != 1.0 - normal.x (\(1.0 - normal.x))")
                XCTAssertEqual(inverted.y, 1.0 - normal.y, accuracy: epsilon,
                    "Custom neutral \(neutral): inverted.y (\(inverted.y)) != 1.0 - normal.y (\(1.0 - normal.y))")
            }
        }
    }

    // MARK: — Edge case: neutral position itself

    /// When gravity equals the neutral position, output should be (0.5, 0.5) regardless of inversion.
    /// Inversion of center is still center: 1.0 - 0.5 = 0.5.
    func testProperty11_neutralPositionInversionIsCenter() {
        let ws = WebSocketManager()
        let settings = SettingsStore(defaults: suite)
        settings.tiltDeadZone = 0.0
        settings.tiltRange = 25.0
        settings.tiltPositionMode = true

        let sensor = TiltSensor(ws: ws, settings: settings)

        let neutral = sensor.neutralGravity
        let gravity = CMAcceleration(x: Double(neutral.x), y: Double(neutral.y), z: Double(neutral.z))

        settings.tiltInverted = false
        let normal = sensor.computePosition(gravity: gravity)
        XCTAssertEqual(normal.x, 0.5, accuracy: 1e-10, "Neutral with inversion off should be 0.5 on X")
        XCTAssertEqual(normal.y, 0.5, accuracy: 1e-10, "Neutral with inversion off should be 0.5 on Y")

        settings.tiltInverted = true
        let inverted = sensor.computePosition(gravity: gravity)
        XCTAssertEqual(inverted.x, 0.5, accuracy: 1e-10, "Neutral with inversion on should still be 0.5 on X")
        XCTAssertEqual(inverted.y, 0.5, accuracy: 1e-10, "Neutral with inversion on should still be 0.5 on Y")
    }
}
