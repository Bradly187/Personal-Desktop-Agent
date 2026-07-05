import XCTest
import CoreMotion
@testable import DesktopAgent

/// Property-based test for linear position mapping.
///
/// **Property 1: Linear Mapping Produces Correct Normalized Output**
/// *For any* tilt angle within [-tiltRange, +tiltRange] on either axis, the
/// `computePosition` function SHALL produce a normalized coordinate that is
/// linearly proportional to the angle, with angle == 0 mapping to 0.5,
/// angle == +tiltRange mapping to 1.0 (or 0.0 for pitch), and
/// angle == -tiltRange mapping to 0.0 (or 1.0 for pitch).
///
/// **Validates: Requirements 1.3, 1.4, 3.5**
@MainActor
final class LinearMappingPropertyTests: XCTestCase {

    private var suiteName: String!
    private var suite: UserDefaults!

    override func setUp() {
        super.setUp()
        suiteName = "com.desktopagent.test.linearMapping.\(UUID().uuidString)"
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

    /// The default neutral gravity vector (iPad at ~45° from horizontal).
    private let defaultNeutral = SIMD3<Double>(-0.707, 0.0, -0.707)

    /// Floating-point tolerance for position comparisons.
    /// atan2 and trigonometric round-trips introduce small errors due to
    /// intermediate sin/cos/tan operations. 1e-12 is tight enough to catch
    /// real bugs while accommodating IEEE 754 double precision arithmetic.
    private let tolerance: Double = 1e-12

    /// Constructs a gravity vector that produces the desired delta pitch and delta roll
    /// angles (in degrees) relative to the given neutral gravity vector.
    ///
    /// The algorithm reverses the atan2 decomposition used in `computePosition`:
    /// - neutralPitch = atan2(-neutral.x, -neutral.z)
    /// - neutralRoll  = atan2(-neutral.y, -neutral.z)
    /// - currentPitch = neutralPitch + deltaPitchRad
    /// - currentRoll  = neutralRoll + deltaRollRad
    ///
    /// We reconstruct a gravity vector from the target pitch and roll angles.
    /// Since pitch uses (-g.x, -g.z) and roll uses (-g.y, -g.z), we need a
    /// consistent g.z that satisfies both. We use g.z = -1 / sqrt(1 + tan²(pitch))
    /// for the pitch-derived component, then compute g.x and g.y from their respective angles.
    ///
    /// For simplicity and to avoid singularities, we construct the gravity vector
    /// by independently setting the pitch and roll components using a shared g.z.
    private func gravityVector(
        deltaPitchDeg: Double,
        deltaRollDeg: Double,
        neutral: SIMD3<Double>
    ) -> CMAcceleration {
        let neutralPitch = atan2(-neutral.x, -neutral.z)
        let neutralRoll = atan2(-neutral.y, -neutral.z)

        let targetPitch = neutralPitch + deltaPitchDeg * (.pi / 180.0)
        let targetRoll = neutralRoll + deltaRollDeg * (.pi / 180.0)

        // Reconstruct gravity components from target angles.
        // atan2(-g.x, -g.z) = targetPitch  →  -g.x / -g.z = tan(targetPitch)
        // atan2(-g.y, -g.z) = targetRoll   →  -g.y / -g.z = tan(targetRoll)
        //
        // Choose g.z = -cos(targetPitch) (unit-ish magnitude for pitch plane)
        // Then g.x = -sin(targetPitch) * sign... but we need consistency.
        //
        // Actually, atan2(a, b) = angle means a = r*sin(angle), b = r*cos(angle) for some r > 0.
        // So: -g.x = r_p * sin(targetPitch), -g.z = r_p * cos(targetPitch)
        // And: -g.y = r_r * sin(targetRoll),  -g.z = r_r * cos(targetRoll)
        //
        // For both to share the same g.z: r_p * cos(targetPitch) = r_r * cos(targetRoll)
        // Let r_p = 1, then g.z = -cos(targetPitch)
        // Then r_r = cos(targetPitch) / cos(targetRoll)
        //
        // g.x = -sin(targetPitch)  (from r_p = 1)
        // g.y = -r_r * sin(targetRoll) = -(cos(targetPitch) / cos(targetRoll)) * sin(targetRoll)
        //     = -cos(targetPitch) * tan(targetRoll)

        let gZ = -cos(targetPitch)
        let gX = -sin(targetPitch)
        let gY = -cos(targetPitch) * tan(targetRoll)

        return CMAcceleration(x: gX, y: gY, z: gZ)
    }

    // MARK: — Property 1: Linear Mapping (150 randomized iterations)

    /// Generates random angles in [-tiltRange, +tiltRange] for both pitch and roll,
    /// constructs the corresponding gravity vector, and verifies the output matches
    /// the expected linear formula:
    ///   x = 0.5 + (deltaRollDeg / tiltRange) * 0.5
    ///   y = 0.5 - (deltaPitchDeg / tiltRange) * 0.5
    ///
    /// Dead zone is set to 0 to avoid interference.
    /// Inversion is disabled.
    @MainActor
    func testProperty1_linearMappingProducesCorrectNormalizedOutput() {
        // Configure settings: dead zone = 0, inversion off, tiltRange = 25
        suite.set(0.0, forKey: "tiltDeadZone")
        suite.set(false, forKey: "tiltInverted")
        suite.set(25.0, forKey: "tiltRange")
        suite.set(true, forKey: "tiltPositionMode")

        let settings = SettingsStore(defaults: suite)
        let ws = WebSocketManager()
        let sensor = TiltSensor(ws: ws, settings: settings)
        sensor.neutralGravity = defaultNeutral

        let tiltRange = 25.0

        for iteration in 0..<150 {
            // Generate random delta angles within [-tiltRange, +tiltRange]
            let deltaPitchDeg = Double.random(in: -tiltRange...tiltRange)
            let deltaRollDeg = Double.random(in: -tiltRange...tiltRange)

            // Construct gravity vector that produces these deltas
            let gravity = gravityVector(
                deltaPitchDeg: deltaPitchDeg,
                deltaRollDeg: deltaRollDeg,
                neutral: defaultNeutral
            )

            // Compute position
            let result = sensor.computePosition(gravity: gravity)

            // Expected linear mapping
            let expectedX = 0.5 + (deltaRollDeg / tiltRange) * 0.5
            let expectedY = 0.5 - (deltaPitchDeg / tiltRange) * 0.5

            XCTAssertEqual(result.x, expectedX, accuracy: tolerance,
                "Iteration \(iteration): x mismatch — deltaPitch=\(deltaPitchDeg), deltaRoll=\(deltaRollDeg), expected x=\(expectedX), got x=\(result.x)")
            XCTAssertEqual(result.y, expectedY, accuracy: tolerance,
                "Iteration \(iteration): y mismatch — deltaPitch=\(deltaPitchDeg), deltaRoll=\(deltaRollDeg), expected y=\(expectedY), got y=\(result.y)")
        }
    }

    // MARK: — Property 1: Zero angle maps to center (0.5, 0.5)

    /// When the gravity vector equals the neutral position, output should be exactly (0.5, 0.5).
    @MainActor
    func testProperty1_zeroAngleMapsToCenter() {
        suite.set(0.0, forKey: "tiltDeadZone")
        suite.set(false, forKey: "tiltInverted")
        suite.set(25.0, forKey: "tiltRange")

        let settings = SettingsStore(defaults: suite)
        let ws = WebSocketManager()
        let sensor = TiltSensor(ws: ws, settings: settings)
        sensor.neutralGravity = defaultNeutral

        // Gravity == neutral → zero delta → center
        let gravity = CMAcceleration(x: defaultNeutral.x, y: defaultNeutral.y, z: defaultNeutral.z)
        let result = sensor.computePosition(gravity: gravity)

        XCTAssertEqual(result.x, 0.5, accuracy: tolerance, "Zero roll should map to x=0.5")
        XCTAssertEqual(result.y, 0.5, accuracy: tolerance, "Zero pitch should map to y=0.5")
    }

    // MARK: — Property 1: +tiltRange roll maps to x=1.0

    /// When roll equals +tiltRange, x should be 1.0.
    @MainActor
    func testProperty1_maxPositiveRollMapsToXOne() {
        suite.set(0.0, forKey: "tiltDeadZone")
        suite.set(false, forKey: "tiltInverted")
        suite.set(25.0, forKey: "tiltRange")

        let settings = SettingsStore(defaults: suite)
        let ws = WebSocketManager()
        let sensor = TiltSensor(ws: ws, settings: settings)
        sensor.neutralGravity = defaultNeutral

        let gravity = gravityVector(deltaPitchDeg: 0, deltaRollDeg: 25.0, neutral: defaultNeutral)
        let result = sensor.computePosition(gravity: gravity)

        XCTAssertEqual(result.x, 1.0, accuracy: tolerance, "+tiltRange roll should map to x=1.0")
        XCTAssertEqual(result.y, 0.5, accuracy: tolerance, "Zero pitch should map to y=0.5")
    }

    // MARK: — Property 1: -tiltRange roll maps to x=0.0

    /// When roll equals -tiltRange, x should be 0.0.
    @MainActor
    func testProperty1_maxNegativeRollMapsToXZero() {
        suite.set(0.0, forKey: "tiltDeadZone")
        suite.set(false, forKey: "tiltInverted")
        suite.set(25.0, forKey: "tiltRange")

        let settings = SettingsStore(defaults: suite)
        let ws = WebSocketManager()
        let sensor = TiltSensor(ws: ws, settings: settings)
        sensor.neutralGravity = defaultNeutral

        let gravity = gravityVector(deltaPitchDeg: 0, deltaRollDeg: -25.0, neutral: defaultNeutral)
        let result = sensor.computePosition(gravity: gravity)

        XCTAssertEqual(result.x, 0.0, accuracy: tolerance, "-tiltRange roll should map to x=0.0")
        XCTAssertEqual(result.y, 0.5, accuracy: tolerance, "Zero pitch should map to y=0.5")
    }

    // MARK: — Property 1: +tiltRange pitch maps to y=0.0 (forward → top)

    /// When pitch equals +tiltRange (forward tilt), y should be 0.0 (top of screen).
    @MainActor
    func testProperty1_maxPositivePitchMapsToYZero() {
        suite.set(0.0, forKey: "tiltDeadZone")
        suite.set(false, forKey: "tiltInverted")
        suite.set(25.0, forKey: "tiltRange")

        let settings = SettingsStore(defaults: suite)
        let ws = WebSocketManager()
        let sensor = TiltSensor(ws: ws, settings: settings)
        sensor.neutralGravity = defaultNeutral

        let gravity = gravityVector(deltaPitchDeg: 25.0, deltaRollDeg: 0, neutral: defaultNeutral)
        let result = sensor.computePosition(gravity: gravity)

        XCTAssertEqual(result.x, 0.5, accuracy: tolerance, "Zero roll should map to x=0.5")
        XCTAssertEqual(result.y, 0.0, accuracy: tolerance, "+tiltRange pitch should map to y=0.0 (top)")
    }

    // MARK: — Property 1: -tiltRange pitch maps to y=1.0 (backward → bottom)

    /// When pitch equals -tiltRange (backward tilt), y should be 1.0 (bottom of screen).
    @MainActor
    func testProperty1_maxNegativePitchMapsToYOne() {
        suite.set(0.0, forKey: "tiltDeadZone")
        suite.set(false, forKey: "tiltInverted")
        suite.set(25.0, forKey: "tiltRange")

        let settings = SettingsStore(defaults: suite)
        let ws = WebSocketManager()
        let sensor = TiltSensor(ws: ws, settings: settings)
        sensor.neutralGravity = defaultNeutral

        let gravity = gravityVector(deltaPitchDeg: -25.0, deltaRollDeg: 0, neutral: defaultNeutral)
        let result = sensor.computePosition(gravity: gravity)

        XCTAssertEqual(result.x, 0.5, accuracy: tolerance, "Zero roll should map to x=0.5")
        XCTAssertEqual(result.y, 1.0, accuracy: tolerance, "-tiltRange pitch should map to y=1.0 (bottom)")
    }

    // MARK: — Property 1: Linearity with varying tiltRange (100 iterations)

    /// Tests that the linear mapping holds for different tiltRange values.
    /// For each iteration, picks a random tiltRange in [5, 60] and random angles
    /// within that range, verifying the formula still holds.
    @MainActor
    func testProperty1_linearMappingHoldsAcrossDifferentTiltRanges() {
        suite.set(0.0, forKey: "tiltDeadZone")
        suite.set(false, forKey: "tiltInverted")
        suite.set(true, forKey: "tiltPositionMode")

        let ws = WebSocketManager()

        for iteration in 0..<100 {
            let tiltRange = Double.random(in: 5.0...60.0)
            suite.set(tiltRange, forKey: "tiltRange")

            let settings = SettingsStore(defaults: suite)
            // defaults treats 0.0 as "unset" (.nonZero ?? 1.5), so disabling the
            // dead zone must happen on the live instance, not via the suite.
            settings.tiltDeadZone = 0.0
            let sensor = TiltSensor(ws: ws, settings: settings)
            sensor.neutralGravity = defaultNeutral

            // Neutral pitch is 45 deg; keep total pitch/roll clear of the +/-90 deg
            // atan2 singularity where this test's gravityVector construction wraps.
            let deltaCap = min(tiltRange, 40.0)
            let deltaPitchDeg = Double.random(in: -deltaCap...deltaCap)
            let deltaRollDeg = Double.random(in: -deltaCap...deltaCap)

            let gravity = gravityVector(
                deltaPitchDeg: deltaPitchDeg,
                deltaRollDeg: deltaRollDeg,
                neutral: defaultNeutral
            )

            let result = sensor.computePosition(gravity: gravity)

            let expectedX = 0.5 + (deltaRollDeg / tiltRange) * 0.5
            let expectedY = 0.5 - (deltaPitchDeg / tiltRange) * 0.5

            XCTAssertEqual(result.x, expectedX, accuracy: tolerance,
                "Iteration \(iteration): x mismatch with tiltRange=\(tiltRange), deltaRoll=\(deltaRollDeg)")
            XCTAssertEqual(result.y, expectedY, accuracy: tolerance,
                "Iteration \(iteration): y mismatch with tiltRange=\(tiltRange), deltaPitch=\(deltaPitchDeg)")
        }
    }

    // MARK: — Property 1: Proportionality (half angle → halfway between center and edge)

    /// Verifies that half the tiltRange produces coordinates halfway between center and edge.
    @MainActor
    func testProperty1_halfAngleProducesHalfwayPosition() {
        suite.set(0.0, forKey: "tiltDeadZone")
        suite.set(false, forKey: "tiltInverted")
        suite.set(25.0, forKey: "tiltRange")

        let settings = SettingsStore(defaults: suite)
        let ws = WebSocketManager()
        let sensor = TiltSensor(ws: ws, settings: settings)
        sensor.neutralGravity = defaultNeutral

        // Half tiltRange on roll → x should be 0.75
        let gravity = gravityVector(deltaPitchDeg: 0, deltaRollDeg: 12.5, neutral: defaultNeutral)
        let result = sensor.computePosition(gravity: gravity)

        XCTAssertEqual(result.x, 0.75, accuracy: tolerance, "Half tiltRange roll should map to x=0.75")
        XCTAssertEqual(result.y, 0.5, accuracy: tolerance, "Zero pitch should map to y=0.5")
    }
}
