import XCTest
@testable import DesktopAgent

/// Property-based test for calibration persistence round-trip.
///
/// **Property 3: Calibration Persistence Round Trip**
/// *For any* valid gravity vector used as a neutral position, persisting it to
/// SettingsStore and then loading it back SHALL produce a neutral position that
/// is equal to the original (within floating-point epsilon).
///
/// **Validates: Requirements 2.1, 2.2**
@MainActor
final class CalibrationPersistenceTests: XCTestCase {

    /// Unique suite name regenerated per test run to guarantee isolation.
    private var suiteName: String!
    private var suite: UserDefaults!

    override func setUp() {
        super.setUp()
        suiteName = "com.desktopagent.test.calibrationPersistence.\(UUID().uuidString)"
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

    // MARK: — Property 3: Calibration persistence round-trip (100 iterations)

    /// Generates random unit vectors representing gravity, persists them via
    /// SettingsStore's neutralGravityX/Y/Z properties, then creates a fresh
    /// SettingsStore reading from the same UserDefaults suite and verifies
    /// all three components match within Float64 epsilon.
    func testProperty3_calibrationPersistenceRoundTrip() {
        for iteration in 0..<100 {
            let vector = randomUnitVector()

            // --- Write phase: persist neutral gravity vector ---
            let writer = SettingsStore(defaults: suite)
            writer.neutralGravityX = vector.x
            writer.neutralGravityY = vector.y
            writer.neutralGravityZ = vector.z

            // --- Read phase: create a new SettingsStore from the same suite ---
            let reader = SettingsStore(defaults: suite)

            // --- Verify equality within Float64 epsilon ---
            XCTAssertEqual(reader.neutralGravityX, vector.x, accuracy: Double.ulpOfOne,
                "Iteration \(iteration): neutralGravityX mismatch — wrote \(vector.x), read \(reader.neutralGravityX)")
            XCTAssertEqual(reader.neutralGravityY, vector.y, accuracy: Double.ulpOfOne,
                "Iteration \(iteration): neutralGravityY mismatch — wrote \(vector.y), read \(reader.neutralGravityY)")
            XCTAssertEqual(reader.neutralGravityZ, vector.z, accuracy: Double.ulpOfOne,
                "Iteration \(iteration): neutralGravityZ mismatch — wrote \(vector.z), read \(reader.neutralGravityZ)")

            // Clean suite between iterations to avoid cross-contamination
            for key in suite.dictionaryRepresentation().keys {
                suite.removeObject(forKey: key)
            }
        }
    }

    // MARK: — Property 3: hasPersistedNeutral reflects persistence state

    /// Verifies that `hasPersistedNeutral` is true after writing and false on a clean suite.
    func testProperty3_hasPersistedNeutralReflectsState() {
        // Fresh suite — no persisted neutral
        let fresh = SettingsStore(defaults: suite)
        XCTAssertFalse(fresh.hasPersistedNeutral,
            "hasPersistedNeutral should be false on a clean UserDefaults suite")

        // After writing neutral gravity
        fresh.neutralGravityX = -0.707
        fresh.neutralGravityY = 0.0
        fresh.neutralGravityZ = -0.707

        let reloaded = SettingsStore(defaults: suite)
        XCTAssertTrue(reloaded.hasPersistedNeutral,
            "hasPersistedNeutral should be true after persisting neutral gravity")
    }

    // MARK: — Edge case: extreme unit vectors

    /// Tests persistence of axis-aligned unit vectors (edge cases for atan2 computations).
    func testProperty3_axisAlignedVectors() {
        let axisVectors: [(x: Double, y: Double, z: Double)] = [
            (1.0, 0.0, 0.0),   // pure X
            (-1.0, 0.0, 0.0),  // pure -X
            (0.0, 1.0, 0.0),   // pure Y
            (0.0, -1.0, 0.0),  // pure -Y
            (0.0, 0.0, 1.0),   // pure Z
            (0.0, 0.0, -1.0),  // pure -Z
        ]

        for (index, vector) in axisVectors.enumerated() {
            let writer = SettingsStore(defaults: suite)
            writer.neutralGravityX = vector.x
            writer.neutralGravityY = vector.y
            writer.neutralGravityZ = vector.z

            let reader = SettingsStore(defaults: suite)

            XCTAssertEqual(reader.neutralGravityX, vector.x, accuracy: Double.ulpOfOne,
                "Axis vector \(index): X mismatch")
            XCTAssertEqual(reader.neutralGravityY, vector.y, accuracy: Double.ulpOfOne,
                "Axis vector \(index): Y mismatch")
            XCTAssertEqual(reader.neutralGravityZ, vector.z, accuracy: Double.ulpOfOne,
                "Axis vector \(index): Z mismatch")

            for key in suite.dictionaryRepresentation().keys {
                suite.removeObject(forKey: key)
            }
        }
    }

    // MARK: — Edge case: default 45° neutral vector

    /// Verifies the default neutral position (~45° from horizontal) persists correctly.
    func testProperty3_default45DegreeNeutralPersists() {
        let defaultX = -0.7071067811865476  // -1/√2
        let defaultY = 0.0
        let defaultZ = -0.7071067811865476  // -1/√2

        let writer = SettingsStore(defaults: suite)
        writer.neutralGravityX = defaultX
        writer.neutralGravityY = defaultY
        writer.neutralGravityZ = defaultZ

        let reader = SettingsStore(defaults: suite)

        XCTAssertEqual(reader.neutralGravityX, defaultX, accuracy: Double.ulpOfOne)
        XCTAssertEqual(reader.neutralGravityY, defaultY, accuracy: Double.ulpOfOne)
        XCTAssertEqual(reader.neutralGravityZ, defaultZ, accuracy: Double.ulpOfOne)
    }

    // MARK: — Edge case: very small components near zero

    /// Verifies that near-zero components (common in nearly-axis-aligned orientations)
    /// persist without being lost to UserDefaults' double(forKey:) returning 0.0 for missing keys.
    func testProperty3_nearZeroComponentsPersist() {
        let tinyValue = 1e-15  // Very small but non-zero

        let writer = SettingsStore(defaults: suite)
        writer.neutralGravityX = tinyValue
        writer.neutralGravityY = -tinyValue
        writer.neutralGravityZ = 1.0  // Approximately unit vector

        let reader = SettingsStore(defaults: suite)

        XCTAssertEqual(reader.neutralGravityX, tinyValue, accuracy: Double.ulpOfOne,
            "Near-zero X component should persist exactly")
        XCTAssertEqual(reader.neutralGravityY, -tinyValue, accuracy: Double.ulpOfOne,
            "Near-zero negative Y component should persist exactly")
        XCTAssertEqual(reader.neutralGravityZ, 1.0, accuracy: Double.ulpOfOne)
    }
}
