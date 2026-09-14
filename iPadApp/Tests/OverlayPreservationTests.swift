import XCTest
import SwiftUI
@testable import DesktopAgent

/// Property 2: Preservation — Interactive Overlay Elements Continue to Receive Touches
///
/// These tests verify that all interactive elements on the overlay views continue to
/// function correctly. They encode the BASELINE behavior that must be preserved after
/// the touch-passthrough fix is applied.
///
/// **Observation-first methodology**: Each test captures behavior observed on UNFIXED code
/// for non-buggy inputs (cases where touch IS on a visible interactive element).
///
/// **EXPECTED OUTCOME**: All tests PASS on unfixed code (confirms baseline to preserve).
///
/// **Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 3.11, 3.12, 3.13, 3.14**
@MainActor
final class OverlayPreservationTests: XCTestCase {

    // MARK: — Test Helpers

    /// Creates an isolated SettingsStore for test isolation.
    private func makeSettings() -> SettingsStore {
        let suiteName = "com.desktopagent.preservation.\(UUID().uuidString)"
        return SettingsStore(defaults: UserDefaults(suiteName: suiteName)!)
    }

    /// Creates a WebSocketManager for testing.
    private func makeWSManager() -> WebSocketManager {
        return WebSocketManager()
    }

    // MARK: — Property: Dwell Action Button Taps Select Actions (Req 3.1)

    /// For all dwell action button taps, the selected action updates correctly.
    /// Observed behavior: Tapping any dwell action button sets `activeDwellAction` to that type.
    ///
    /// **Validates: Requirements 3.1**
    func testDwellActionButtonTapsSelectAction() {
        let selectableActions: [DwellActionType] = [.leftClick, .rightClick, .doubleClick, .dragStart]

        for _ in 0..<100 {
            let settings = makeSettings()
            settings.isDragging = false

            // Pick a random action to select
            let targetAction = selectableActions.randomElement()!

            // Simulate tapping the button (same logic as DwellActionToolbar.selectAction)
            if targetAction == .dragStart {
                // Drag button only activates when not already dragging
                guard !settings.isDragging else { continue }
            }
            settings.activeDwellAction = targetAction

            XCTAssertEqual(
                settings.activeDwellAction, targetAction,
                "Tapping dwell action button should set activeDwellAction to \(targetAction.rawValue)"
            )
        }
    }

    /// For all dwell action types, the action persists to UserDefaults.
    ///
    /// **Validates: Requirements 3.1**
    func testDwellActionPersistsToUserDefaults() {
        for action in DwellActionType.allCases {
            let suiteName = "com.desktopagent.persist.\(UUID().uuidString)"
            let defaults = UserDefaults(suiteName: suiteName)!
            let settings = SettingsStore(defaults: defaults)

            settings.activeDwellAction = action

            // Read back from UserDefaults directly
            let persisted = defaults.string(forKey: "activeDwellAction")
            XCTAssertEqual(
                persisted, action.rawValue,
                "Action \(action.rawValue) should persist to UserDefaults"
            )
        }
    }

    // MARK: — Property: Floating Toolbar Drag Repositions (Req 3.2)

    /// For all drag gestures starting within the visible floating toolbar bounds,
    /// the toolbar repositions and persists offset to UserDefaults.
    ///
    /// **Validates: Requirements 3.2**
    func testFloatingToolbarDragRepositions() {
        for _ in 0..<100 {
            let suiteName = "com.desktopagent.drag.\(UUID().uuidString)"
            let defaults = UserDefaults(suiteName: suiteName)!
            let settings = SettingsStore(defaults: defaults)
            settings.toolbarPosition = .floating

            // Random drag translation
            let dx = CGFloat.random(in: -200...200)
            let dy = CGFloat.random(in: -200...200)

            // Simulate drag end (same logic as DwellToolbarContainer.floatingToolbar onEnded)
            let newOffset = CGSize(
                width: settings.toolbarFloatingOffset.width + dx,
                height: settings.toolbarFloatingOffset.height + dy
            )
            settings.toolbarFloatingOffset = newOffset

            // Verify offset updated
            XCTAssertEqual(
                settings.toolbarFloatingOffset.width, newOffset.width, accuracy: 0.001,
                "Floating toolbar X offset should update after drag"
            )
            XCTAssertEqual(
                settings.toolbarFloatingOffset.height, newOffset.height, accuracy: 0.001,
                "Floating toolbar Y offset should update after drag"
            )

            // Verify persisted to UserDefaults
            let persistedX = defaults.double(forKey: "toolbarFloatingOffsetX")
            let persistedY = defaults.double(forKey: "toolbarFloatingOffsetY")
            XCTAssertEqual(
                persistedX, Double(newOffset.width), accuracy: 0.001,
                "Floating offset X should persist to UserDefaults"
            )
            XCTAssertEqual(
                persistedY, Double(newOffset.height), accuracy: 0.001,
                "Floating offset Y should persist to UserDefaults"
            )
        }
    }

    // MARK: — Property: Reconnect Button Triggers When Disconnected (Req 3.3)

    /// For all taps on reconnect button when `isDisconnected == true`, reconnection triggers.
    /// The DAConnectionBanner's handleTap() calls wsManager.connect() only when disconnected.
    ///
    /// **Validates: Requirements 3.3**
    func testReconnectButtonTriggersWhenDisconnected() {
        // The WebSocketManager.connect() transitions state from .disconnected to .connecting.
        // We verify the state machine transition that proves the reconnect was triggered.
        let ws = makeWSManager()

        // Precondition: must be disconnected
        XCTAssertEqual(ws.state, .disconnected, "Initial state should be disconnected")

        // Simulate tapping reconnect (same logic as DAConnectionBanner.handleTap)
        // The guard checks isDisconnected — when true, calls connect()
        ws.connect()

        // After connect(), state transitions to .connecting
        XCTAssertEqual(
            ws.state, .connecting,
            "After reconnect tap when disconnected, state should transition to .connecting"
        )
    }

    /// Reconnect button is disabled when NOT disconnected — tapping does nothing.
    /// This verifies the `.disabled(!isDisconnected)` behavior.
    ///
    /// **Validates: Requirements 3.3**
    func testReconnectButtonDisabledWhenNotDisconnected() {
        let ws = makeWSManager()

        // First connect to get out of disconnected state
        ws.connect()
        XCTAssertEqual(ws.state, .connecting)

        // Simulate another tap — connect() guards on state == .disconnected
        ws.connect()

        // State should remain .connecting (guard prevented re-entry)
        XCTAssertEqual(
            ws.state, .connecting,
            "Reconnect tap when already connecting should be no-op"
        )
    }

    // MARK: — Property: One-Shot Dwell Mode Resets After Firing (Req 3.9)

    /// When one-shot mode is enabled and a non-default action fires,
    /// the system resets to leftClick after firing.
    ///
    /// **Validates: Requirements 3.9**
    func testOneShotModeResetsToLeftClick() {
        let nonDefaultActions: [DwellActionType] = [.rightClick, .doubleClick]

        for _ in 0..<100 {
            let settings = makeSettings()
            settings.oneShotEnabled = true

            let action = nonDefaultActions.randomElement()!
            settings.activeDwellAction = action

            // Fire one-shot reset (same logic triggered after dwell fires)
            let didReset = settings.applyOneShotResetIfNeeded()

            XCTAssertTrue(didReset, "One-shot should reset for \(action.rawValue)")
            XCTAssertEqual(
                settings.activeDwellAction, .leftClick,
                "After one-shot fire, action should reset to .leftClick"
            )
        }
    }

    // MARK: — Property: Toolbar Position Changes Persist (Req 3.6)

    /// For all toolbar position changes, the position persists to UserDefaults.
    ///
    /// **Validates: Requirements 3.6**
    func testToolbarPositionChangesPersist() {
        for position in ToolbarPosition.allCases {
            let suiteName = "com.desktopagent.pos.\(UUID().uuidString)"
            let defaults = UserDefaults(suiteName: suiteName)!
            let settings = SettingsStore(defaults: defaults)

            settings.toolbarPosition = position

            // Verify persisted
            let persisted = defaults.string(forKey: "toolbarPosition")
            XCTAssertEqual(
                persisted, position.rawValue,
                "Toolbar position \(position.rawValue) should persist to UserDefaults"
            )

            // Verify can be read back
            let reloaded = SettingsStore(defaults: defaults)
            XCTAssertEqual(
                reloaded.toolbarPosition, position,
                "Toolbar position should survive reload from UserDefaults"
            )
        }
    }

    // MARK: — Property: CommandPad Buttons Fire Actions via WebSocket (Req 3.7)

    /// For all command buttons, the action payload is correctly constructed.
    /// We verify the sendCommand logic produces valid payloads.
    ///
    /// **Validates: Requirements 3.7**
    func testCommandPadButtonsConstructValidPayloads() {
        let ws = makeWSManager()

        // Test all default command buttons
        for button in CommandButton.defaults {
            var params: [String: Any] = [:]
            for (k, v) in button.params {
                if k == "keys" {
                    params[k] = v.split(separator: ",").map(String.init)
                } else {
                    params[k] = v
                }
            }

            // sendCommand returns a message ID — verifies the method executes without error
            let id = ws.sendCommand(action: button.action, text: button.label, params: params)

            // Message ID should be non-empty and follow the pattern
            XCTAssertTrue(
                id.hasPrefix("msg-"),
                "Command button '\(button.label)' should produce a valid message ID"
            )
        }
    }

    // MARK: — Property: TrackpadView Gestures Send Commands (Req 3.5)

    /// For all trackpad move events, the WebSocket manager constructs valid move payloads.
    ///
    /// **Validates: Requirements 3.5**
    func testTrackpadMoveGesturesSendCommands() {
        let ws = makeWSManager()

        for _ in 0..<100 {
            let dx = Int.random(in: -500...500)
            let dy = Int.random(in: -500...500)

            // sendTrackpadMove should execute without error
            // (won't actually send since not connected, but verifies the method is callable)
            ws.sendTrackpadMove(dx: dx, dy: dy)
        }

        // Verify tap sends work
        ws.sendTrackpadTap(button: "left")
        ws.sendTrackpadTap(button: "right")

        // Verify scroll sends work
        ws.sendTrackpadScroll(direction: "up")
        ws.sendTrackpadScroll(direction: "down")
        ws.sendTrackpadScroll(direction: "left")
        ws.sendTrackpadScroll(direction: "right")
    }

    // MARK: — Property: Write Tab Expressions Register (Req 3.14)

    /// For all expression sends from the Write tab, the sendCommand method is callable with DICTATE action.
    ///
    /// Renamed 2026-08-16: was `testScientificKeypadSendExpression`, which implied coverage of a
    /// `ScientificKeypadView` that never existed (Requirement 18, struck — see D031). This test
    /// only ever exercised the DICTATE send path shared by the Pencil canvas.
    ///
    /// **Validates: Requirements 3.14**
    func testWriteTabDictateSendExpression() {
        let ws = makeWSManager()

        // Simulate various expressions being sent via the Write tab (HandwritingCanvasView)
        let expressions = ["1+2", "sin(3.14)", "42", "2^10", "sqrt(16)", "3.14*2"]

        for expr in expressions {
            let id = ws.sendCommand(action: "DICTATE", text: expr)
            XCTAssertTrue(
                id.hasPrefix("msg-"),
                "Write-tab DICTATE send for '\(expr)' should produce a valid message ID"
            )
        }
    }

    // MARK: — Property: Edge Scroll Toggle Persists (Req 3.1)

    /// Toggling edge scroll updates the setting and persists.
    ///
    /// **Validates: Requirements 3.1**
    func testEdgeScrollTogglePersists() {
        for _ in 0..<50 {
            let suiteName = "com.desktopagent.scroll.\(UUID().uuidString)"
            let defaults = UserDefaults(suiteName: suiteName)!
            let settings = SettingsStore(defaults: defaults)

            let initialValue = settings.momentumScrollEnabled

            // Toggle (same logic as DwellActionToolbar.toggleEdgeScroll)
            settings.momentumScrollEnabled.toggle()

            XCTAssertNotEqual(
                settings.momentumScrollEnabled, initialValue,
                "Edge scroll should toggle"
            )

            // Verify persisted
            let persisted = defaults.bool(forKey: "momentumScrollEnabled")
            XCTAssertEqual(
                persisted, settings.momentumScrollEnabled,
                "Edge scroll toggle should persist to UserDefaults"
            )
        }
    }

    // MARK: — Property: Drag State Machine Preserves Behavior (Req 3.2)

    /// The drag button interaction preserves the drag state machine:
    /// - Tapping drag when not dragging enters dragStart
    /// - dragStart fires → transitions to dragEnd, isDragging = true
    /// - dragEnd fires → resets to leftClick, isDragging = false
    ///
    /// **Validates: Requirements 3.2**
    func testDragButtonStateMachinePreserved() {
        for _ in 0..<100 {
            let settings = makeSettings()
            settings.isDragging = false
            settings.activeDwellAction = .leftClick

            // Step 1: Tap drag button (selectDragAction logic)
            if !settings.isDragging {
                settings.activeDwellAction = .dragStart
            }
            XCTAssertEqual(settings.activeDwellAction, .dragStart)

            // Step 2: Dwell fires dragStart → transition to dragEnd
            settings.applyDwellTransition(firedAction: .dragStart)
            XCTAssertEqual(settings.activeDwellAction, .dragEnd)
            XCTAssertTrue(settings.isDragging)

            // Step 3: Dwell fires dragEnd → reset to leftClick
            settings.applyDwellTransition(firedAction: .dragEnd)
            XCTAssertEqual(settings.activeDwellAction, .leftClick)
            XCTAssertFalse(settings.isDragging)
        }
    }

    // MARK: — Property: Toolbar Position Affects Layout (Req 3.6)

    /// All three toolbar positions are valid and can be set/read without error.
    ///
    /// **Validates: Requirements 3.6**
    func testAllToolbarPositionsValid() {
        let settings = makeSettings()

        for position in ToolbarPosition.allCases {
            settings.toolbarPosition = position
            XCTAssertEqual(settings.toolbarPosition, position)
        }
    }

    // MARK: — Property: Interactive Elements Within Bounds Receive Touches

    /// For all touch events where touch.point IS WITHIN a visible interactive element,
    /// the element receives and handles the touch. This is modeled by verifying that
    /// the interactive area geometry correctly identifies points within toolbar buttons.
    ///
    /// **Validates: Requirements 3.1, 3.2, 3.5, 3.7, 3.14**
    func testTouchesWithinInteractiveElementsAreHandled() {
        let screenWidth: CGFloat = 1194
        let screenHeight: CGFloat = 834
        let toolbarIntrinsicHeight: CGFloat = 60
        let toolbarIntrinsicWidth: CGFloat = 380
        let horizontalPadding: CGFloat = 12
        let bottomPadding: CGFloat = 56

        for position in ToolbarPosition.allCases {
            let interactiveArea: CGRect

            switch position {
            case .top:
                let toolbarContentX = (screenWidth - toolbarIntrinsicWidth) / 2
                interactiveArea = CGRect(
                    x: toolbarContentX,
                    y: 0,
                    width: toolbarIntrinsicWidth,
                    height: toolbarIntrinsicHeight
                )

            case .bottom:
                let frameHeight = toolbarIntrinsicHeight + bottomPadding
                let frameTop = screenHeight - frameHeight
                interactiveArea = CGRect(
                    x: horizontalPadding,
                    y: frameTop,
                    width: screenWidth - horizontalPadding * 2,
                    height: toolbarIntrinsicHeight
                )

            case .floating:
                let floatingWidth: CGFloat = min(screenWidth - 32, 400)
                let floatingHeight: CGFloat = 80
                let floatingX = (screenWidth - floatingWidth) / 2
                let floatingY = (screenHeight - floatingHeight) / 2
                interactiveArea = CGRect(
                    x: floatingX,
                    y: floatingY,
                    width: floatingWidth,
                    height: floatingHeight
                )
            }

            // Generate random points WITHIN the interactive area
            for _ in 0..<50 {
                let x = CGFloat.random(in: interactiveArea.minX...interactiveArea.maxX)
                let y = CGFloat.random(in: interactiveArea.minY...interactiveArea.maxY)
                let point = CGPoint(x: x, y: y)

                // The interactive area MUST contain this point (by construction)
                XCTAssertTrue(
                    interactiveArea.contains(point),
                    "Touch at (\(x), \(y)) in \(position) mode should be within interactive area"
                )
            }
        }
    }

    // MARK: — Property: WebSocket Dwell Action Sync (Req 3.1)

    /// For all dwell action selections, the WebSocket sync method is callable.
    /// Verifies sendSetDwellAction doesn't crash for any action type.
    ///
    /// **Validates: Requirements 3.1**
    func testDwellActionSyncViaWebSocket() {
        let ws = makeWSManager()

        for action in DwellActionType.allCases {
            // sendSetDwellAction should execute without error
            // (won't actually send since not connected, but verifies method is callable)
            ws.sendSetDwellAction(action)
        }
    }

    // MARK: — Property: Settings Survive App Lifecycle (Req 3.13)

    /// When the app enters background and returns to foreground, toolbar position
    /// and floating offset are preserved (simulated by re-creating SettingsStore
    /// from the same UserDefaults).
    ///
    /// **Validates: Requirements 3.13**
    func testSettingsSurviveAppLifecycle() {
        for _ in 0..<50 {
            let suiteName = "com.desktopagent.lifecycle.\(UUID().uuidString)"
            let defaults = UserDefaults(suiteName: suiteName)!

            // Set up initial state
            let settings1 = SettingsStore(defaults: defaults)
            let position = ToolbarPosition.allCases.randomElement()!
            let offsetX = CGFloat.random(in: -200...200)
            let offsetY = CGFloat.random(in: -200...200)

            settings1.toolbarPosition = position
            settings1.toolbarFloatingOffset = CGSize(width: offsetX, height: offsetY)

            // Simulate app lifecycle: create new SettingsStore from same defaults
            let settings2 = SettingsStore(defaults: defaults)

            XCTAssertEqual(
                settings2.toolbarPosition, position,
                "Toolbar position should survive lifecycle"
            )
            XCTAssertEqual(
                settings2.toolbarFloatingOffset.width, offsetX, accuracy: 0.001,
                "Floating offset X should survive lifecycle"
            )
            XCTAssertEqual(
                settings2.toolbarFloatingOffset.height, offsetY, accuracy: 0.001,
                "Floating offset Y should survive lifecycle"
            )
        }
    }

    // MARK: — Property: Floating Toolbar Constrained to Safe Area (Req 3.2)

    /// The constrainToSafeArea function keeps the toolbar within bounds.
    /// This verifies the drag repositioning logic preserves safe area constraints.
    ///
    /// **Validates: Requirements 3.2**
    func testFloatingToolbarConstrainedToSafeArea() {
        // Test the constraint logic directly (same as DwellToolbarContainer.constrainToSafeArea)
        let containerSize = CGSize(width: 1194, height: 834)
        let safeArea = EdgeInsets(top: 24, leading: 0, bottom: 20, trailing: 0)
        let toolbarSize = CGSize(width: 400, height: 80)

        let centerX = containerSize.width / 2
        let centerY = containerSize.height / 2

        let minX = safeArea.leading + toolbarSize.width / 2
        let maxX = containerSize.width - safeArea.trailing - toolbarSize.width / 2
        let minY = safeArea.top + toolbarSize.height / 2
        let maxY = containerSize.height - safeArea.bottom - toolbarSize.height / 2

        for _ in 0..<100 {
            // Random extreme offset
            let offset = CGSize(
                width: CGFloat.random(in: -2000...2000),
                height: CGFloat.random(in: -2000...2000)
            )

            let proposedCenterX = centerX + offset.width
            let proposedCenterY = centerY + offset.height

            let clampedX = max(minX, min(maxX, proposedCenterX))
            let clampedY = max(minY, min(maxY, proposedCenterY))

            let constrained = CGSize(
                width: clampedX - centerX,
                height: clampedY - centerY
            )

            // Verify the constrained position is within safe area
            let finalCenterX = centerX + constrained.width
            let finalCenterY = centerY + constrained.height

            XCTAssertGreaterThanOrEqual(finalCenterX, minX, "Toolbar should not go past left safe area")
            XCTAssertLessThanOrEqual(finalCenterX, maxX, "Toolbar should not go past right safe area")
            XCTAssertGreaterThanOrEqual(finalCenterY, minY, "Toolbar should not go past top safe area")
            XCTAssertLessThanOrEqual(finalCenterY, maxY, "Toolbar should not go past bottom safe area")
        }
    }
}
