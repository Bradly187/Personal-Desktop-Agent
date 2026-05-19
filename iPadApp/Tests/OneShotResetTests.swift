import XCTest
@testable import DesktopAgent

/// Property-based test for one-shot reset behavior.
///
/// **Property 5: One-shot reset behavior**
/// *For any* non-default action type (right-click, double-click) with one-shot mode enabled,
/// after the action fires exactly once, the dwell action selector SHALL reset to left-click mode.
///
/// **Validates: Requirements 1.5**
final class OneShotResetTests: XCTestCase {

    // MARK: — Property 5: One-shot reset behavior (randomized)

    /// Generates random non-default action types with one-shot enabled and verifies
    /// that calling applyOneShotResetIfNeeded resets to .leftClick every time.
    func testProperty5_oneShotResetsNonDefaultActions() {
        let nonDefaultActions: [DwellActionType] = [.rightClick, .doubleClick]

        // Run 100 iterations with random selection to approximate property-based testing
        for _ in 0..<100 {
            let store = SettingsStore()
            store.oneShotEnabled = true

            // Pick a random non-default action
            let action = nonDefaultActions.randomElement()!
            store.activeDwellAction = action

            // Fire one-shot reset
            let didReset = store.applyOneShotResetIfNeeded()

            XCTAssertTrue(didReset,
                "One-shot should reset for action: \(action.rawValue)")
            XCTAssertEqual(store.activeDwellAction, .leftClick,
                "After one-shot reset, action should be .leftClick but was \(store.activeDwellAction.rawValue) for initial action: \(action.rawValue)")
        }
    }

    /// Verifies that one-shot does NOT reset when oneShotEnabled is false,
    /// regardless of the active action type.
    func testProperty5_oneShotDisabledDoesNotReset() {
        let allActions = DwellActionType.allCases

        for _ in 0..<100 {
            let store = SettingsStore()
            store.oneShotEnabled = false

            let action = allActions.randomElement()!
            store.activeDwellAction = action

            let didReset = store.applyOneShotResetIfNeeded()

            XCTAssertFalse(didReset,
                "One-shot disabled should never reset, but did for action: \(action.rawValue)")
            XCTAssertEqual(store.activeDwellAction, action,
                "Action should remain \(action.rawValue) when one-shot is disabled")
        }
    }

    /// Verifies that one-shot does NOT reset for default/drag actions even when enabled.
    /// leftClick is already the default; dragStart/dragEnd have their own state machine.
    func testProperty5_oneShotEnabledDoesNotResetDefaultOrDragActions() {
        let exemptActions: [DwellActionType] = [.leftClick, .dragStart, .dragEnd]

        for _ in 0..<100 {
            let store = SettingsStore()
            store.oneShotEnabled = true

            let action = exemptActions.randomElement()!
            store.activeDwellAction = action

            let didReset = store.applyOneShotResetIfNeeded()

            XCTAssertFalse(didReset,
                "One-shot should NOT reset for action: \(action.rawValue)")
            XCTAssertEqual(store.activeDwellAction, action,
                "Action should remain \(action.rawValue) — exempt from one-shot")
        }
    }

    /// Verifies the one-shot fires exactly once: after reset, a second call is a no-op
    /// because the action is already .leftClick.
    func testProperty5_oneShotResetsExactlyOnce() {
        let nonDefaultActions: [DwellActionType] = [.rightClick, .doubleClick]

        for _ in 0..<100 {
            let store = SettingsStore()
            store.oneShotEnabled = true

            let action = nonDefaultActions.randomElement()!
            store.activeDwellAction = action

            // First fire — should reset
            let firstReset = store.applyOneShotResetIfNeeded()
            XCTAssertTrue(firstReset)
            XCTAssertEqual(store.activeDwellAction, .leftClick)

            // Second fire — already leftClick, should NOT reset
            let secondReset = store.applyOneShotResetIfNeeded()
            XCTAssertFalse(secondReset,
                "Second one-shot call should be no-op since action is already .leftClick")
            XCTAssertEqual(store.activeDwellAction, .leftClick)
        }
    }

    // MARK: — Exhaustive coverage (all combinations)

    /// Exhaustively tests all (action × oneShotEnabled) combinations to verify
    /// the property holds universally.
    func testProperty5_exhaustiveAllCombinations() {
        for action in DwellActionType.allCases {
            for oneShotEnabled in [true, false] {
                let store = SettingsStore()
                store.oneShotEnabled = oneShotEnabled
                store.activeDwellAction = action

                let didReset = store.applyOneShotResetIfNeeded()

                let shouldReset = oneShotEnabled && (action == .rightClick || action == .doubleClick)

                XCTAssertEqual(didReset, shouldReset,
                    "action=\(action.rawValue), oneShot=\(oneShotEnabled): expected reset=\(shouldReset), got=\(didReset)")

                if shouldReset {
                    XCTAssertEqual(store.activeDwellAction, .leftClick,
                        "After reset, action should be .leftClick")
                } else {
                    XCTAssertEqual(store.activeDwellAction, action,
                        "Without reset, action should remain \(action.rawValue)")
                }
            }
        }
    }
}
