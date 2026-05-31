import XCTest
@testable import DesktopAgent

/// Property-based test for toggle persistence round-trip.
///
/// **Property 12: Toggle persistence round-trip**
/// *For any* combination of feature toggle states (each independently true or false),
/// after persisting to storage and reloading, all toggle states SHALL match their
/// pre-persistence values exactly.
///
/// **Validates: Requirements 4.1, 4.6**
final class TogglePersistenceTests: XCTestCase {

    /// Unique suite name regenerated per test run to guarantee isolation.
    private var suiteName: String!
    private var suite: UserDefaults!

    override func setUp() {
        super.setUp()
        suiteName = "com.desktopagent.test.togglePersistence.\(UUID().uuidString)"
        suite = UserDefaults(suiteName: suiteName)!
    }

    override func tearDown() {
        // Remove the volatile suite so it doesn't leak between tests.
        UserDefaults.standard.removePersistentDomain(forName: suiteName)
        suite.synchronize()
        suite = nil
        suiteName = nil
        super.tearDown()
    }

    // MARK: — Property 12: Toggle persistence round-trip (randomized, 100 iterations)

    /// Generates random feature toggle states, persists them via one SettingsStore
    /// instance, then creates a fresh SettingsStore reading from the same
    /// UserDefaults suite and verifies each toggle matches.
    func testProperty12_togglePersistenceRoundTrip() {
        for iteration in 0..<100 {
            let edgeScroll = Bool.random()

            // --- Write phase: set toggles on a SettingsStore instance ---
            let writer = SettingsStore(defaults: suite)
            writer.edgeScrollEnabled = edgeScroll

            // --- Read phase: create a new SettingsStore from the same suite ---
            let reader = SettingsStore(defaults: suite)

            // --- Verify toggle round-trips ---
            XCTAssertEqual(reader.edgeScrollEnabled, edgeScroll,
                "Iteration \(iteration): edgeScrollEnabled mismatch — wrote \(edgeScroll), read \(reader.edgeScrollEnabled)")

            // Clean suite between iterations to avoid cross-contamination
            for key in suite.dictionaryRepresentation().keys {
                suite.removeObject(forKey: key)
            }
        }
    }

    // MARK: — Edge case: true persists correctly

    func testProperty12_toggleTrue() {
        let writer = SettingsStore(defaults: suite)
        writer.edgeScrollEnabled = true

        let reader = SettingsStore(defaults: suite)

        XCTAssertTrue(reader.edgeScrollEnabled)
    }

    // MARK: — Edge case: false persists correctly

    func testProperty12_toggleFalse() {
        let writer = SettingsStore(defaults: suite)
        writer.edgeScrollEnabled = false

        let reader = SettingsStore(defaults: suite)

        XCTAssertFalse(reader.edgeScrollEnabled)
    }
}
