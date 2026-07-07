import XCTest
@testable import DesktopAgent

/// Clamping + persistence tests for the user-tunable tap-to-click threshold
/// (Settings "Tap Sensitivity" slider → SettingsStore.tapThreshold).
///
/// Property: for ANY value assigned to `tapThreshold` (negative, zero, huge),
/// the stored value SHALL be clamped to [0.4, 3.0] g and survive a reload.
@MainActor
final class TapSensitivityClampingTests: XCTestCase {

    private var suiteName: String!
    private var suite: UserDefaults!

    override func setUp() {
        super.setUp()
        suiteName = "com.desktopagent.test.tapSensitivity.\(UUID().uuidString)"
        suite = UserDefaults(suiteName: suiteName)!
    }

    override func tearDown() {
        UserDefaults.standard.removePersistentDomain(forName: suiteName)
        suite.synchronize()
        suite = nil
        suiteName = nil
        super.tearDown()
    }

    func testDefaultIsPointFourG() {
        let store = SettingsStore(defaults: suite)
        XCTAssertEqual(store.tapThreshold, 0.4, accuracy: 1e-10,
                       "Fresh install should default tapThreshold to 0.4 g")
    }

    func testAlwaysClampedToValidInterval() {
        let store = SettingsStore(defaults: suite)
        let inputs: [Double] = [
            -Double.greatestFiniteMagnitude, -10.0, -0.001, 0.0, 0.39, 0.4,
            1.2, 2.0, 3.0, 3.01, 100.0, Double.greatestFiniteMagnitude,
        ]
        for v in inputs {
            store.tapThreshold = v
            XCTAssertGreaterThanOrEqual(store.tapThreshold, 0.4, "input \(v)")
            XCTAssertLessThanOrEqual(store.tapThreshold, 3.0, "input \(v)")
        }
    }

    func testBoundariesPreservedExactly() {
        let store = SettingsStore(defaults: suite)
        store.tapThreshold = 0.4
        XCTAssertEqual(store.tapThreshold, 0.4)
        store.tapThreshold = 3.0
        XCTAssertEqual(store.tapThreshold, 3.0)
    }

    func testValidValuesPreservedExactly() {
        let store = SettingsStore(defaults: suite)
        for _ in 0..<100 {
            let v = Double.random(in: 0.4...3.0)
            store.tapThreshold = v
            XCTAssertEqual(store.tapThreshold, v, accuracy: 1e-10)
        }
    }

    func testClampedValuePersistsAcrossInstances() {
        let writer = SettingsStore(defaults: suite)
        writer.tapThreshold = 99.0           // clamps to 3.0
        let reader = SettingsStore(defaults: suite)
        XCTAssertEqual(reader.tapThreshold, 3.0, accuracy: 1e-10,
                       "Reloaded tapThreshold should read back the clamped value")
    }
}
