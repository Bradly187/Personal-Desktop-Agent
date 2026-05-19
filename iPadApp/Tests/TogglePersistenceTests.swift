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

    /// Generates random combinations of 6 boolean toggle states, persists them via
    /// one SettingsStore instance, then creates a fresh SettingsStore reading from the
    /// same UserDefaults suite and verifies all 6 toggles match.
    func testProperty12_togglePersistenceRoundTrip() {
        for iteration in 0..<100 {
            // Generate 6 random booleans
            let gazeDwellClick = Bool.random()
            let gazeDwellRightClick = Bool.random()
            let gazeDwellDoubleClick = Bool.random()
            let gazeDwellDrag = Bool.random()
            let edgeScroll = Bool.random()
            let gazeCursorMode = Bool.random()

            // --- Write phase: set toggles on a SettingsStore instance ---
            let writer = SettingsStore(defaults: suite)
            writer.gazeDwellClickEnabled = gazeDwellClick
            writer.gazeDwellRightClickEnabled = gazeDwellRightClick
            writer.gazeDwellDoubleClickEnabled = gazeDwellDoubleClick
            writer.gazeDwellDragEnabled = gazeDwellDrag
            writer.edgeScrollEnabled = edgeScroll
            writer.gazeCursorModeEnabled = gazeCursorMode

            // --- Read phase: create a new SettingsStore from the same suite ---
            let reader = SettingsStore(defaults: suite)

            // --- Verify all 6 toggles match ---
            XCTAssertEqual(reader.gazeDwellClickEnabled, gazeDwellClick,
                "Iteration \(iteration): gazeDwellClickEnabled mismatch — wrote \(gazeDwellClick), read \(reader.gazeDwellClickEnabled)")
            XCTAssertEqual(reader.gazeDwellRightClickEnabled, gazeDwellRightClick,
                "Iteration \(iteration): gazeDwellRightClickEnabled mismatch — wrote \(gazeDwellRightClick), read \(reader.gazeDwellRightClickEnabled)")
            XCTAssertEqual(reader.gazeDwellDoubleClickEnabled, gazeDwellDoubleClick,
                "Iteration \(iteration): gazeDwellDoubleClickEnabled mismatch — wrote \(gazeDwellDoubleClick), read \(reader.gazeDwellDoubleClickEnabled)")
            XCTAssertEqual(reader.gazeDwellDragEnabled, gazeDwellDrag,
                "Iteration \(iteration): gazeDwellDragEnabled mismatch — wrote \(gazeDwellDrag), read \(reader.gazeDwellDragEnabled)")
            XCTAssertEqual(reader.edgeScrollEnabled, edgeScroll,
                "Iteration \(iteration): edgeScrollEnabled mismatch — wrote \(edgeScroll), read \(reader.edgeScrollEnabled)")
            XCTAssertEqual(reader.gazeCursorModeEnabled, gazeCursorMode,
                "Iteration \(iteration): gazeCursorModeEnabled mismatch — wrote \(gazeCursorMode), read \(reader.gazeCursorModeEnabled)")

            // Clean suite between iterations to avoid cross-contamination
            for key in suite.dictionaryRepresentation().keys {
                suite.removeObject(forKey: key)
            }
        }
    }

    // MARK: — Edge case: all-true persists correctly

    /// Verifies that the all-true state (defaults) round-trips correctly.
    func testProperty12_allTogglesTrue() {
        let writer = SettingsStore(defaults: suite)
        writer.gazeDwellClickEnabled = true
        writer.gazeDwellRightClickEnabled = true
        writer.gazeDwellDoubleClickEnabled = true
        writer.gazeDwellDragEnabled = true
        writer.edgeScrollEnabled = true
        writer.gazeCursorModeEnabled = true

        let reader = SettingsStore(defaults: suite)

        XCTAssertTrue(reader.gazeDwellClickEnabled)
        XCTAssertTrue(reader.gazeDwellRightClickEnabled)
        XCTAssertTrue(reader.gazeDwellDoubleClickEnabled)
        XCTAssertTrue(reader.gazeDwellDragEnabled)
        XCTAssertTrue(reader.edgeScrollEnabled)
        XCTAssertTrue(reader.gazeCursorModeEnabled)
    }

    // MARK: — Edge case: all-false persists correctly

    /// Verifies that the all-false state round-trips correctly.
    func testProperty12_allTogglesFalse() {
        let writer = SettingsStore(defaults: suite)
        writer.gazeDwellClickEnabled = false
        writer.gazeDwellRightClickEnabled = false
        writer.gazeDwellDoubleClickEnabled = false
        writer.gazeDwellDragEnabled = false
        writer.edgeScrollEnabled = false
        writer.gazeCursorModeEnabled = false

        let reader = SettingsStore(defaults: suite)

        XCTAssertFalse(reader.gazeDwellClickEnabled)
        XCTAssertFalse(reader.gazeDwellRightClickEnabled)
        XCTAssertFalse(reader.gazeDwellDoubleClickEnabled)
        XCTAssertFalse(reader.gazeDwellDragEnabled)
        XCTAssertFalse(reader.edgeScrollEnabled)
        XCTAssertFalse(reader.gazeCursorModeEnabled)
    }

    // MARK: — allDwellActionsDisabled computed property

    /// Verifies that `allDwellActionsDisabled` correctly reflects persisted toggle state.
    func testProperty12_allDwellActionsDisabledReflectsPersistedState() {
        for _ in 0..<100 {
            let click = Bool.random()
            let rightClick = Bool.random()
            let doubleClick = Bool.random()
            let drag = Bool.random()

            let writer = SettingsStore(defaults: suite)
            writer.gazeDwellClickEnabled = click
            writer.gazeDwellRightClickEnabled = rightClick
            writer.gazeDwellDoubleClickEnabled = doubleClick
            writer.gazeDwellDragEnabled = drag

            let reader = SettingsStore(defaults: suite)
            let expectedDisabled = !click && !rightClick && !doubleClick && !drag

            XCTAssertEqual(reader.allDwellActionsDisabled, expectedDisabled,
                "allDwellActionsDisabled should be \(expectedDisabled) for toggles: click=\(click), right=\(rightClick), double=\(doubleClick), drag=\(drag)")

            for key in suite.dictionaryRepresentation().keys {
                suite.removeObject(forKey: key)
            }
        }
    }
}
