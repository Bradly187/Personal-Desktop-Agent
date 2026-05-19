import XCTest
@testable import DesktopAgent

/// Property-based tests for the drag state machine transitions in SettingsStore.
///
/// **Property 4: Drag state machine transitions**
/// *For any* dwell action selector state, when a drag-start action fires the selector
/// SHALL transition to drag-end mode, and when a drag-end action subsequently fires
/// the selector SHALL reset to left-click mode — regardless of what action type was
/// active before the drag sequence began.
///
/// **Validates: Requirements 1.3, 1.4**
@MainActor
final class DragStateMachineTests: XCTestCase {

    // MARK: — Helpers

    /// All possible initial states the selector can be in before a drag sequence.
    private let allActionTypes: [DwellActionType] = DwellActionType.allCases

    /// Creates a fresh SettingsStore with a given initial action type.
    private func makeStore(initialAction: DwellActionType) -> SettingsStore {
        let store = SettingsStore()
        store.activeDwellAction = initialAction
        store.isDragging = false
        return store
    }

    // MARK: — Property 4: Drag state machine transitions (deterministic exhaustive)

    /// Exhaustively verifies that for EVERY possible initial state, firing dragStart
    /// transitions to dragEnd and sets isDragging = true.
    func testDragStartTransitionFromAllInitialStates() {
        for initialAction in allActionTypes {
            let store = makeStore(initialAction: initialAction)

            store.applyDwellTransition(firedAction: .dragStart)

            XCTAssertEqual(
                store.activeDwellAction, .dragEnd,
                "After dragStart from \(initialAction), activeDwellAction should be .dragEnd"
            )
            XCTAssertTrue(
                store.isDragging,
                "After dragStart from \(initialAction), isDragging should be true"
            )
        }
    }

    /// Exhaustively verifies that for EVERY possible initial state, firing dragEnd
    /// resets to leftClick and sets isDragging = false.
    func testDragEndTransitionFromAllInitialStates() {
        for initialAction in allActionTypes {
            let store = makeStore(initialAction: initialAction)
            store.isDragging = true  // Simulate mid-drag state

            store.applyDwellTransition(firedAction: .dragEnd)

            XCTAssertEqual(
                store.activeDwellAction, .leftClick,
                "After dragEnd from \(initialAction), activeDwellAction should be .leftClick"
            )
            XCTAssertFalse(
                store.isDragging,
                "After dragEnd from \(initialAction), isDragging should be false"
            )
        }
    }

    /// Verifies the full drag sequence: dragStart → dragEnd resets to leftClick,
    /// regardless of what action was active before the sequence began.
    func testFullDragSequenceFromAllInitialStates() {
        for initialAction in allActionTypes {
            let store = makeStore(initialAction: initialAction)

            // Phase 1: dragStart fires
            store.applyDwellTransition(firedAction: .dragStart)
            XCTAssertEqual(store.activeDwellAction, .dragEnd)
            XCTAssertTrue(store.isDragging)

            // Phase 2: dragEnd fires
            store.applyDwellTransition(firedAction: .dragEnd)
            XCTAssertEqual(
                store.activeDwellAction, .leftClick,
                "Full drag sequence from \(initialAction) should end at .leftClick"
            )
            XCTAssertFalse(store.isDragging)
        }
    }

    // MARK: — Property 4: Randomized sequences

    /// Generates random sequences of dwell actions and verifies that the drag state
    /// machine invariants hold after every transition:
    /// - After dragStart: activeDwellAction == .dragEnd, isDragging == true
    /// - After dragEnd: activeDwellAction == .leftClick, isDragging == false
    /// - Non-drag actions do not alter activeDwellAction or isDragging via applyDwellTransition
    func testRandomActionSequencesMaintainDragInvariants() {
        let iterations = 200
        let maxSequenceLength = 20

        for _ in 0..<iterations {
            // Random initial state
            let initialAction = allActionTypes.randomElement()!
            let store = makeStore(initialAction: initialAction)

            // Generate a random sequence of actions
            let sequenceLength = Int.random(in: 1...maxSequenceLength)
            var expectedAction = initialAction
            var expectedDragging = false

            for _ in 0..<sequenceLength {
                let firedAction = allActionTypes.randomElement()!
                store.applyDwellTransition(firedAction: firedAction)

                // Compute expected state after transition
                switch firedAction {
                case .dragStart:
                    expectedAction = .dragEnd
                    expectedDragging = true
                case .dragEnd:
                    expectedAction = .leftClick
                    expectedDragging = false
                default:
                    // Non-drag actions don't change state via applyDwellTransition
                    break
                }

                XCTAssertEqual(
                    store.activeDwellAction, expectedAction,
                    "Invariant violated: after firing \(firedAction), expected \(expectedAction) but got \(store.activeDwellAction)"
                )
                XCTAssertEqual(
                    store.isDragging, expectedDragging,
                    "Invariant violated: after firing \(firedAction), expected isDragging=\(expectedDragging) but got \(store.isDragging)"
                )
            }
        }
    }

    /// Verifies that multiple consecutive dragStart calls are idempotent —
    /// the state remains .dragEnd / isDragging=true.
    func testConsecutiveDragStartsAreIdempotent() {
        let store = makeStore(initialAction: .leftClick)

        for _ in 0..<10 {
            store.applyDwellTransition(firedAction: .dragStart)
            XCTAssertEqual(store.activeDwellAction, .dragEnd)
            XCTAssertTrue(store.isDragging)
        }
    }

    /// Verifies that multiple consecutive dragEnd calls are idempotent —
    /// the state remains .leftClick / isDragging=false.
    func testConsecutiveDragEndsAreIdempotent() {
        let store = makeStore(initialAction: .dragEnd)
        store.isDragging = true

        for _ in 0..<10 {
            store.applyDwellTransition(firedAction: .dragEnd)
            XCTAssertEqual(store.activeDwellAction, .leftClick)
            XCTAssertFalse(store.isDragging)
        }
    }

    /// Verifies that non-drag actions (leftClick, rightClick, doubleClick) do NOT
    /// alter the activeDwellAction or isDragging state when passed to applyDwellTransition.
    func testNonDragActionsDoNotAlterState() {
        let nonDragActions: [DwellActionType] = [.leftClick, .rightClick, .doubleClick]

        for initialAction in allActionTypes {
            for nonDrag in nonDragActions {
                let store = makeStore(initialAction: initialAction)
                let initialDragging = Bool.random()
                store.isDragging = initialDragging

                store.applyDwellTransition(firedAction: nonDrag)

                XCTAssertEqual(
                    store.activeDwellAction, initialAction,
                    "Non-drag action \(nonDrag) should not change activeDwellAction from \(initialAction)"
                )
                XCTAssertEqual(
                    store.isDragging, initialDragging,
                    "Non-drag action \(nonDrag) should not change isDragging"
                )
            }
        }
    }

    // MARK: — Property 4: Stress test with random interleaving

    /// Generates long random sequences mixing drag and non-drag actions,
    /// verifying the state machine never enters an invalid state.
    /// Key invariant: when isDragging == true, activeDwellAction MUST be .dragEnd.
    func testLongRandomSequenceNeverProducesInvalidState() {
        let store = makeStore(initialAction: allActionTypes.randomElement()!)

        for _ in 0..<1000 {
            let action = allActionTypes.randomElement()!
            store.applyDwellTransition(firedAction: action)

            // Core invariant: if isDragging is true, we must be in dragEnd state
            if store.isDragging {
                XCTAssertEqual(
                    store.activeDwellAction, .dragEnd,
                    "When isDragging is true, activeDwellAction must be .dragEnd"
                )
            }

            // The activeDwellAction must always be a valid DwellActionType
            // (guaranteed by Swift's type system, but verify the value is sensible)
            XCTAssertTrue(
                allActionTypes.contains(store.activeDwellAction),
                "activeDwellAction must always be a valid DwellActionType"
            )
        }
    }

    /// Verifies that after any dragStart, the ONLY way to reach leftClick is through dragEnd.
    /// Random non-drag actions between dragStart and dragEnd should not reset the state.
    func testDragStateOnlyResetsViaDragEnd() {
        let store = makeStore(initialAction: .leftClick)
        let nonDragActions: [DwellActionType] = [.leftClick, .rightClick, .doubleClick]

        // Start a drag
        store.applyDwellTransition(firedAction: .dragStart)
        XCTAssertTrue(store.isDragging)
        XCTAssertEqual(store.activeDwellAction, .dragEnd)

        // Fire random non-drag actions — state should NOT change
        for _ in 0..<50 {
            let nonDrag = nonDragActions.randomElement()!
            store.applyDwellTransition(firedAction: nonDrag)
            XCTAssertTrue(
                store.isDragging,
                "Non-drag action \(nonDrag) must not reset isDragging during active drag"
            )
            XCTAssertEqual(
                store.activeDwellAction, .dragEnd,
                "Non-drag action \(nonDrag) must not change activeDwellAction during active drag"
            )
        }

        // Only dragEnd resets
        store.applyDwellTransition(firedAction: .dragEnd)
        XCTAssertFalse(store.isDragging)
        XCTAssertEqual(store.activeDwellAction, .leftClick)
    }
}
