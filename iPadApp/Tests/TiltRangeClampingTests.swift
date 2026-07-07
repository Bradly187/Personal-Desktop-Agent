import XCTest
@testable import DesktopAgent

/// Property-based test for tiltRange clamping.
///
/// **Property 5: Tilt Range Constraint**
/// *For any* value assigned to `tiltRange` in SettingsStore (including negative numbers,
/// zero, and values > 60), the stored value SHALL always be clamped to the interval
/// [5, 60] degrees.
///
/// **Validates: Requirements 3.3**
@MainActor
final class TiltRangeClampingTests: XCTestCase {

    /// Unique suite name regenerated per test run to guarantee isolation.
    private var suiteName: String!
    private var suite: UserDefaults!

    override func setUp() {
        super.setUp()
        suiteName = "com.desktopagent.test.tiltRangeClamping.\(UUID().uuidString)"
        suite = UserDefaults(suiteName: suiteName)!
    }

    override func tearDown() {
        UserDefaults.standard.removePersistentDomain(forName: suiteName)
        suite.synchronize()
        suite = nil
        suiteName = nil
        super.tearDown()
    }

    // MARK: — Property 5: Tilt Range Constraint (randomized, 150 iterations)

    /// Generates random Double values across the full range (including negatives, zero,
    /// very large values, and values within [5, 60]), assigns to `tiltRange`, and verifies
    /// the stored value is always clamped to [5, 60].
    func testProperty5_tiltRangeAlwaysClampedToValidInterval() {
        let store = SettingsStore(defaults: suite)

        for iteration in 0..<150 {
            // Generate a random value from a wide range including edge cases
            let randomValue = generateRandomTiltInput()

            store.tiltRange = randomValue

            XCTAssertGreaterThanOrEqual(store.tiltRange, 5.0,
                "Iteration \(iteration): tiltRange \(store.tiltRange) is below minimum 5.0 (input was \(randomValue))")
            XCTAssertLessThanOrEqual(store.tiltRange, 60.0,
                "Iteration \(iteration): tiltRange \(store.tiltRange) exceeds maximum 60.0 (input was \(randomValue))")

            // Also verify the value persisted to UserDefaults is clamped
            let persisted = suite.double(forKey: "tiltRange")
            XCTAssertGreaterThanOrEqual(persisted, 5.0,
                "Iteration \(iteration): persisted tiltRange \(persisted) is below minimum 5.0 (input was \(randomValue))")
            XCTAssertLessThanOrEqual(persisted, 60.0,
                "Iteration \(iteration): persisted tiltRange \(persisted) exceeds maximum 60.0 (input was \(randomValue))")
        }
    }

    // MARK: — Property 5: Values within valid range are preserved exactly

    /// For values within [5, 60], the stored value should equal the input exactly.
    func testProperty5_validRangeValuesPreservedExactly() {
        let store = SettingsStore(defaults: suite)

        for iteration in 0..<100 {
            // Generate a random value strictly within [5, 60]
            let validValue = Double.random(in: 5.0...60.0)

            store.tiltRange = validValue

            XCTAssertEqual(store.tiltRange, validValue, accuracy: 1e-10,
                "Iteration \(iteration): valid tiltRange \(validValue) should be preserved exactly, got \(store.tiltRange)")
        }
    }

    // MARK: — Edge cases: boundary values

    func testProperty5_exactMinimumBoundary() {
        let store = SettingsStore(defaults: suite)
        store.tiltRange = 5.0
        XCTAssertEqual(store.tiltRange, 5.0, "Exact minimum (5.0) should be preserved")
    }

    func testProperty5_exactMaximumBoundary() {
        let store = SettingsStore(defaults: suite)
        store.tiltRange = 60.0
        XCTAssertEqual(store.tiltRange, 60.0, "Exact maximum (60.0) should be preserved")
    }

    func testProperty5_justBelowMinimum() {
        let store = SettingsStore(defaults: suite)
        store.tiltRange = 4.999
        XCTAssertEqual(store.tiltRange, 5.0, "Value just below minimum should clamp to 5.0")
    }

    func testProperty5_justAboveMaximum() {
        let store = SettingsStore(defaults: suite)
        store.tiltRange = 60.001
        XCTAssertEqual(store.tiltRange, 60.0, "Value just above maximum should clamp to 60.0")
    }

    func testProperty5_zero() {
        let store = SettingsStore(defaults: suite)
        store.tiltRange = 0.0
        XCTAssertEqual(store.tiltRange, 5.0, "Zero should clamp to minimum 5.0")
    }

    func testProperty5_negativeValue() {
        let store = SettingsStore(defaults: suite)
        store.tiltRange = -100.0
        XCTAssertEqual(store.tiltRange, 5.0, "Negative value should clamp to minimum 5.0")
    }

    func testProperty5_veryLargeValue() {
        let store = SettingsStore(defaults: suite)
        store.tiltRange = 10000.0
        XCTAssertEqual(store.tiltRange, 60.0, "Very large value should clamp to maximum 60.0")
    }

    func testProperty5_doubleMaxValue() {
        let store = SettingsStore(defaults: suite)
        store.tiltRange = Double.greatestFiniteMagnitude
        XCTAssertEqual(store.tiltRange, 60.0, "Double.greatestFiniteMagnitude should clamp to 60.0")
    }

    func testProperty5_negativeDoubleMaxValue() {
        let store = SettingsStore(defaults: suite)
        store.tiltRange = -Double.greatestFiniteMagnitude
        XCTAssertEqual(store.tiltRange, 5.0, "-Double.greatestFiniteMagnitude should clamp to 5.0")
    }

    // MARK: — Persistence round-trip with clamping

    /// Verifies that after assigning an out-of-range value, creating a new SettingsStore
    /// from the same defaults reads back the clamped value.
    func testProperty5_clampedValuePersistsAcrossInstances() {
        for _ in 0..<50 {
            let randomValue = generateRandomTiltInput()

            // Clean suite between iterations
            for key in suite.dictionaryRepresentation().keys {
                suite.removeObject(forKey: key)
            }

            let writer = SettingsStore(defaults: suite)
            writer.tiltRange = randomValue

            let reader = SettingsStore(defaults: suite)

            XCTAssertGreaterThanOrEqual(reader.tiltRange, 5.0,
                "Reloaded tiltRange should be >= 5.0 (original input: \(randomValue))")
            XCTAssertLessThanOrEqual(reader.tiltRange, 60.0,
                "Reloaded tiltRange should be <= 60.0 (original input: \(randomValue))")
        }
    }

    // MARK: — Helpers

    /// Generates random Double values biased toward interesting edge cases:
    /// negatives, zero, values near boundaries, very large values, and valid range values.
    private func generateRandomTiltInput() -> Double {
        let category = Int.random(in: 0..<6)
        switch category {
        case 0:
            // Negative values
            return Double.random(in: -1000.0 ..< 0.0)
        case 1:
            // Zero and near-zero
            return Double.random(in: -0.1...0.1)
        case 2:
            // Below minimum boundary (0 to 5)
            return Double.random(in: 0.0..<5.0)
        case 3:
            // Valid range [5, 60]
            return Double.random(in: 5.0...60.0)
        case 4:
            // Above maximum (60 to 1000)
            return Double.random(in: 60.0...1000.0)
        case 5:
            // Extreme values
            let extremes: [Double] = [
                -Double.greatestFiniteMagnitude,
                -10000.0,
                -1.0,
                0.0,
                4.99,
                5.0,
                5.01,
                25.0,
                59.99,
                60.0,
                60.01,
                10000.0,
                Double.greatestFiniteMagnitude
            ]
            return extremes.randomElement()!
        default:
            return Double.random(in: -500.0...500.0)
        }
    }
}
