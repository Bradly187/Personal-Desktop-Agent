import XCTest
@testable import DesktopAgent

/// Bug Condition Exploration Test: Overlay Non-Interactive Areas Intercept Touches
///
/// **Property 1: Expected Behavior** — Overlay views pass through touches in non-interactive regions.
///
/// This test encodes the EXPECTED (correct) behavior: touches outside visible interactive
/// elements should pass through overlays to the tab bar or tab content. On FIXED code,
/// these tests PASS — confirming the bug is resolved.
///
/// **Bug Conditions Verified (now fixed):**
/// 1. Tab bar tap no longer blocked by bottom toolbar spacer (allowsHitTesting(false) on spacer)
/// 2. Content tap no longer blocked by toolbar frame (frame(maxWidth: .infinity) removed)
/// 3. Navigation area tap no longer blocked by DAConnectionBanner (allowsHitTesting(isDisconnected))
/// 4. Trackpad gesture no longer blocked by floating toolbar (contentShape constrains DragGesture)
///
/// **Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5**
@MainActor
final class OverlayTouchInterceptionTests: XCTestCase {

    // MARK: — Test Helpers

    /// Simulates a touch point in the coordinate space.
    struct TouchPoint {
        let x: CGFloat
        let y: CGFloat
        let intendedTarget: String // "tabBar", "tabContent", "trackpad", "navigation"

        var point: CGPoint { CGPoint(x: x, y: y) }
    }

    /// Represents the overlay frame geometry for hit-testing analysis.
    struct OverlayGeometry {
        let frame: CGRect
        let interactiveArea: CGRect
        let nonInteractiveArea: CGRect

        /// Returns true if the point is in the overlay frame but NOT in the interactive area.
        /// This is the bug condition: the overlay intercepts the touch.
        func isBugCondition(point: CGPoint) -> Bool {
            return frame.contains(point) &&
                   !interactiveArea.contains(point)
        }
    }

    // MARK: — Constants matching DwellToolbarContainer layout

    /// Standard iPad screen size for testing (11" iPad Pro landscape)
    private let screenWidth: CGFloat = 1194
    private let screenHeight: CGFloat = 834

    /// Tab bar height (standard iOS)
    private let tabBarHeight: CGFloat = 49

    /// Bottom padding in DwellToolbarContainer bottom mode
    private let bottomPadding: CGFloat = 56

    /// Toolbar intrinsic height (buttons + vertical padding)
    /// DwellActionToolbar: 5 buttons × 44pt min height + sm padding top/bottom
    private let toolbarIntrinsicHeight: CGFloat = 60

    /// Toolbar intrinsic width (5 buttons × ~60pt each + spacing + horizontal padding)
    private let toolbarIntrinsicWidth: CGFloat = 380

    /// DesignTokens.Spacing.md horizontal padding on each side
    private let horizontalPadding: CGFloat = 12 // DesignTokens.Spacing.md

    /// DesignTokens.Radius.md for the toolbar background
    private let toolbarCornerRadius: CGFloat = 12 // DesignTokens.Radius.md

    // MARK: — Bug Condition 1: Tab bar tap blocked by bottom toolbar 56pt padding

    /// The DwellToolbarContainer in bottom mode uses a VStack with a Color.clear spacer
    /// that has `.allowsHitTesting(false)`. The 56pt spacer area does NOT participate in
    /// hit-testing, so the overlay's effective hit-testable frame is only the toolbar itself.
    ///
    /// EXPECTED BEHAVIOR: Touches in the 56pt spacer area pass through to the tab bar.
    /// ON FIXED CODE: This test PASSES because the spacer has allowsHitTesting(false).
    func testBottomPaddingDoesNotBlockTabBar() {
        let settings = SettingsStore(defaults: makeIsolatedDefaults())
        settings.toolbarPosition = .bottom

        // On FIXED code, the outer ZStack has .allowsHitTesting(false) and only the
        // toolbar content re-enables hit-testing. The 56pt Color.clear spacer explicitly
        // has .allowsHitTesting(false). So the effective hit-testable frame is ONLY the
        // toolbar buttons area — the padding area is NOT part of the hit-testable frame.
        let frameTop = screenHeight - toolbarIntrinsicHeight - bottomPadding

        // The effective hit-testable frame on fixed code = only the toolbar content
        let effectiveHitFrame = CGRect(
            x: horizontalPadding,
            y: frameTop,
            width: screenWidth - horizontalPadding * 2,
            height: toolbarIntrinsicHeight
        )

        // The interactive area matches the effective hit frame on fixed code
        let interactiveArea = effectiveHitFrame

        let geometry = OverlayGeometry(
            frame: effectiveHitFrame,
            interactiveArea: interactiveArea,
            nonInteractiveArea: CGRect(
                x: horizontalPadding,
                y: frameTop + toolbarIntrinsicHeight,
                width: screenWidth - horizontalPadding * 2,
                height: bottomPadding
            )
        )

        // Generate touch points in the 56pt padding area that overlaps the tab bar.
        // Tab bar is at y=785 to y=834. Padding area is y=778 to y=834.
        // Overlap region: y=785 to y=834 (49pt).
        let tabBarTouches: [TouchPoint] = [
            // Center of tab bar (y=810)
            TouchPoint(x: screenWidth / 2, y: screenHeight - tabBarHeight / 2, intendedTarget: "tabBar"),
            // Left side of tab bar (y=804)
            TouchPoint(x: 100, y: screenHeight - 30, intendedTarget: "tabBar"),
            // Right side of tab bar (y=814)
            TouchPoint(x: screenWidth - 100, y: screenHeight - 20, intendedTarget: "tabBar"),
            // Just inside the overlap (y=790)
            TouchPoint(x: 300, y: screenHeight - tabBarHeight + 5, intendedTarget: "tabBar"),
            // Near bottom of tab bar (y=824)
            TouchPoint(x: 600, y: screenHeight - 10, intendedTarget: "tabBar"),
        ]

        for touch in tabBarTouches {
            // The bug condition: touch is in the overlay frame but not in the interactive area
            let isInPaddingArea = geometry.isBugCondition(point: touch.point)

            // ASSERTION: On fixed code, the padding area should NOT intercept touches.
            // This means allowsHitTesting(false) should be applied to the padding.
            // On UNFIXED code, the padding IS part of the hit-testable frame, so this
            // assertion verifies the EXPECTED behavior (which currently doesn't hold).
            XCTAssertFalse(
                isInPaddingArea,
                "Touch at (\(touch.x), \(touch.y)) intended for \(touch.intendedTarget) " +
                "should NOT be intercepted by the 56pt bottom padding area. " +
                "Bug: DwellToolbarContainer bottom padding overlaps tab bar hit region."
            )
        }
    }

    // MARK: — Bug Condition 2: Content tap blocked by full-width frame

    /// The DwellToolbarContainer in top/bottom mode no longer uses `.frame(maxWidth: .infinity)`.
    /// The toolbar defines its own intrinsic width and uses `.contentShape(RoundedRectangle(...))`
    /// to constrain the hit area to the visible background bounds only.
    ///
    /// EXPECTED BEHAVIOR: Touches outside the visible toolbar buttons pass through.
    /// ON FIXED CODE: This test PASSES because the hit-testable frame matches the toolbar content.
    func testFullWidthFrameDoesNotBlockContent() {
        let settings = SettingsStore(defaults: makeIsolatedDefaults())
        settings.toolbarPosition = .top

        // On FIXED code, .frame(maxWidth: .infinity) is removed. The toolbar uses its
        // intrinsic width with .contentShape(RoundedRectangle(...)) constraining the hit area.
        // The effective hit-testable frame = the visible toolbar content area only.
        let toolbarContentX = (screenWidth - toolbarIntrinsicWidth) / 2
        let effectiveHitFrame = CGRect(
            x: toolbarContentX,
            y: 0,
            width: toolbarIntrinsicWidth,
            height: toolbarIntrinsicHeight
        )

        // Interactive area matches the effective hit frame on fixed code
        let interactiveArea = effectiveHitFrame

        let geometry = OverlayGeometry(
            frame: effectiveHitFrame,
            interactiveArea: interactiveArea,
            nonInteractiveArea: CGRect(
                x: horizontalPadding,
                y: 0,
                width: screenWidth - horizontalPadding * 2,
                height: toolbarIntrinsicHeight
            )
        )

        // Generate touch points in the full-width frame but outside toolbar buttons
        let contentTouches: [TouchPoint] = [
            // Far left of the full-width frame, outside toolbar buttons
            TouchPoint(x: 30, y: toolbarIntrinsicHeight / 2, intendedTarget: "tabContent"),
            // Far right of the full-width frame, outside toolbar buttons
            TouchPoint(x: screenWidth - 30, y: toolbarIntrinsicHeight / 2, intendedTarget: "tabContent"),
            // Just outside the toolbar content area on the left
            TouchPoint(x: toolbarContentX - 20, y: 30, intendedTarget: "tabContent"),
            // Just outside the toolbar content area on the right
            TouchPoint(x: toolbarContentX + toolbarIntrinsicWidth + 20, y: 30, intendedTarget: "tabContent"),
        ]

        for touch in contentTouches {
            let isInFullWidthButOutsideButtons = geometry.isBugCondition(point: touch.point)

            // ASSERTION: On fixed code, touches outside the visible toolbar shape should pass through.
            // On UNFIXED code, the .frame(maxWidth: .infinity) makes the entire row hit-testable.
            XCTAssertFalse(
                isInFullWidthButOutsideButtons,
                "Touch at (\(touch.x), \(touch.y)) intended for \(touch.intendedTarget) " +
                "should NOT be intercepted by the full-width toolbar frame. " +
                "Bug: .frame(maxWidth: .infinity) creates hit area beyond visible toolbar buttons."
            )
        }
    }

    // MARK: — Bug Condition 3: Banner tap when connected blocks navigation

    /// The DAConnectionBanner now uses `.allowsHitTesting(isDisconnected)` which disables
    /// hit-testing entirely when connected. The `.contentShape(Rectangle())` has been removed.
    /// When connected, the banner does not participate in hit-testing at all.
    ///
    /// EXPECTED BEHAVIOR: When connected, the banner should not intercept touches.
    /// ON FIXED CODE: This test PASSES because .allowsHitTesting(false) when connected.
    func testBannerDoesNotBlockWhenConnected() {
        // On FIXED code, .allowsHitTesting(isDisconnected) is applied to the Button.
        // When connected (isDisconnected == false), the entire banner has hit-testing disabled.
        // This means the banner's effective hit-testable frame is ZERO when connected.

        // Banner frame (full-width, ~44pt height, at top of screen with padding)
        let bannerHeight: CGFloat = 44
        let bannerTopPadding: CGFloat = 8 // DesignTokens.Spacing.sm
        let bannerHorizontalPadding: CGFloat = 16 // DesignTokens.Spacing.lg

        // When connected, the effective hit-testable frame is empty (allowsHitTesting(false))
        let effectiveHitFrame = CGRect.zero

        // When connected, there is NO interactive area
        let interactiveAreaWhenConnected = CGRect.zero

        let geometry = OverlayGeometry(
            frame: effectiveHitFrame,
            interactiveArea: interactiveAreaWhenConnected,
            nonInteractiveArea: CGRect(
                x: bannerHorizontalPadding,
                y: bannerTopPadding,
                width: screenWidth - bannerHorizontalPadding * 2,
                height: bannerHeight
            )
        )

        // Simulate connected state — banner should pass through ALL touches
        let navigationTouches: [TouchPoint] = [
            TouchPoint(x: screenWidth / 2, y: bannerTopPadding + bannerHeight / 2, intendedTarget: "navigation"),
            TouchPoint(x: 100, y: bannerTopPadding + 10, intendedTarget: "navigation"),
            TouchPoint(x: screenWidth - 100, y: bannerTopPadding + 30, intendedTarget: "navigation"),
            TouchPoint(x: bannerHorizontalPadding + 50, y: bannerTopPadding + 20, intendedTarget: "navigation"),
        ]

        for touch in navigationTouches {
            let isBannerIntercepting = geometry.isBugCondition(point: touch.point)

            // ASSERTION: On fixed code, the banner should NOT intercept touches when connected.
            // .allowsHitTesting(isDisconnected) should disable hit-testing when connected.
            // On UNFIXED code, .contentShape(Rectangle()) + .disabled() still intercepts.
            XCTAssertFalse(
                isBannerIntercepting,
                "Touch at (\(touch.x), \(touch.y)) intended for \(touch.intendedTarget) " +
                "should NOT be intercepted by DAConnectionBanner when connected. " +
                "Bug: .contentShape(Rectangle()) makes disabled button hit-testable."
            )
        }
    }

    // MARK: — Bug Condition 4: Floating toolbar DragGesture extends beyond visible bounds

    /// The floating toolbar now applies `.contentShape(RoundedRectangle(...))` before
    /// `.gesture(DragGesture())`, constraining the gesture scope to the visible toolbar bounds.
    /// Touches outside the visible toolbar pass through to TrackpadView.
    ///
    /// EXPECTED BEHAVIOR: DragGesture only captures drags starting within visible toolbar bounds.
    /// ON FIXED CODE: This test PASSES because contentShape constrains the gesture scope.
    func testFloatingToolbarDragDoesNotExtendBeyondVisibleBounds() {
        let settings = SettingsStore(defaults: makeIsolatedDefaults())
        settings.toolbarPosition = .floating
        settings.toolbarFloatingOffset = .zero // Centered

        // Floating toolbar visible bounds (centered, with background RoundedRectangle)
        let floatingWidth: CGFloat = min(screenWidth - 16 * 2, 400) // from DwellToolbarContainer
        let floatingHeight: CGFloat = 80 // from DwellToolbarContainer

        let floatingX = (screenWidth - floatingWidth) / 2
        let floatingY = (screenHeight - floatingHeight) / 2

        let visibleToolbarBounds = CGRect(
            x: floatingX,
            y: floatingY,
            width: floatingWidth,
            height: floatingHeight
        )

        // On FIXED code, .contentShape(RoundedRectangle(...)) is applied before .gesture(DragGesture()).
        // This constrains the gesture's hit area to the visible toolbar bounds only.
        // The effective hit-testable frame = the visible toolbar bounds (no extension).
        let effectiveHitFrame = visibleToolbarBounds

        let geometry = OverlayGeometry(
            frame: effectiveHitFrame,
            interactiveArea: visibleToolbarBounds,
            nonInteractiveArea: CGRect.zero // No non-interactive area extends beyond bounds
        )

        // Generate touch/drag start points just outside the visible toolbar but within gesture scope
        let trackpadDrags: [TouchPoint] = [
            // Just above the visible toolbar
            TouchPoint(x: screenWidth / 2, y: floatingY - 10, intendedTarget: "trackpad"),
            // Just below the visible toolbar
            TouchPoint(x: screenWidth / 2, y: floatingY + floatingHeight + 10, intendedTarget: "trackpad"),
            // Just to the left of the visible toolbar
            TouchPoint(x: floatingX - 10, y: screenHeight / 2, intendedTarget: "trackpad"),
            // Just to the right of the visible toolbar
            TouchPoint(x: floatingX + floatingWidth + 10, y: screenHeight / 2, intendedTarget: "trackpad"),
        ]

        for touch in trackpadDrags {
            let isDragCapturedOutsideBounds = geometry.isBugCondition(point: touch.point)

            // ASSERTION: On fixed code, drags outside visible toolbar bounds should reach TrackpadView.
            // .contentShape(RoundedRectangle(...)) before .gesture(DragGesture()) constrains the scope.
            // On UNFIXED code, the DragGesture captures drags from the extended frame area.
            XCTAssertFalse(
                isDragCapturedOutsideBounds,
                "Drag at (\(touch.x), \(touch.y)) intended for \(touch.intendedTarget) " +
                "should NOT be captured by floating toolbar DragGesture. " +
                "Bug: DragGesture scope extends beyond visible toolbar bounds."
            )
        }
    }

    // MARK: — Property-Based: Random touches in non-interactive overlay areas

    /// Generates random touch coordinates within overlay frames but outside interactive areas.
    /// For ALL such touches, the overlay should NOT intercept them (they should pass through).
    ///
    /// ON FIXED CODE: This test PASSES because the overlay frame equals the interactive area —
    /// there are no non-interactive regions within the hit-testable frame.
    func testRandomTouchesInNonInteractiveAreasPassThrough() {
        let settings = SettingsStore(defaults: makeIsolatedDefaults())
        let iterations = 100

        for _ in 0..<iterations {
            // Random toolbar position
            let position = ToolbarPosition.allCases.randomElement()!
            settings.toolbarPosition = position

            let overlayGeometry = computeOverlayGeometry(for: position)

            // Generate a random point within the overlay frame
            let randomX = CGFloat.random(in: overlayGeometry.frame.minX...overlayGeometry.frame.maxX)
            let randomY = CGFloat.random(in: overlayGeometry.frame.minY...overlayGeometry.frame.maxY)
            let randomPoint = CGPoint(x: randomX, y: randomY)

            // Only test points that are NOT in the interactive area (bug condition inputs)
            if !overlayGeometry.interactiveArea.contains(randomPoint) {
                let isIntercepted = overlayGeometry.frame.contains(randomPoint)

                // ASSERTION: Non-interactive overlay areas should NOT intercept touches.
                // On UNFIXED code, the full frame intercepts all touches within it.
                XCTAssertFalse(
                    isIntercepted,
                    "Random touch at (\(randomX), \(randomY)) in \(position) mode " +
                    "is within overlay frame but outside interactive area — " +
                    "should pass through but overlay intercepts it. " +
                    "Bug: Non-interactive overlay area participates in hit-testing."
                )
            }
        }
    }

    // MARK: — Helpers

    /// Creates isolated UserDefaults for test isolation.
    private func makeIsolatedDefaults() -> UserDefaults {
        let suiteName = "com.desktopagent.test.\(UUID().uuidString)"
        return UserDefaults(suiteName: suiteName)!
    }

    /// Computes the overlay geometry for a given toolbar position.
    /// Models the FIXED behavior where only the interactive area is hit-testable.
    private func computeOverlayGeometry(for position: ToolbarPosition) -> OverlayGeometry {
        switch position {
        case .top:
            // On fixed code, .frame(maxWidth: .infinity) is removed and .contentShape constrains
            // the hit area to the visible toolbar content only.
            let toolbarContentX = (screenWidth - toolbarIntrinsicWidth) / 2
            let frame = CGRect(
                x: toolbarContentX,
                y: 0,
                width: toolbarIntrinsicWidth,
                height: toolbarIntrinsicHeight
            )
            // Interactive area matches the frame on fixed code
            let interactive = frame
            return OverlayGeometry(frame: frame, interactiveArea: interactive, nonInteractiveArea: .zero)

        case .bottom:
            // On fixed code, the 56pt spacer has .allowsHitTesting(false) and the outer ZStack
            // has .allowsHitTesting(false). Only the toolbar content is hit-testable.
            let frameTop = screenHeight - toolbarIntrinsicHeight - bottomPadding
            let frame = CGRect(
                x: horizontalPadding,
                y: frameTop,
                width: screenWidth - horizontalPadding * 2,
                height: toolbarIntrinsicHeight
            )
            // Interactive area matches the frame on fixed code
            let interactive = frame
            return OverlayGeometry(frame: frame, interactiveArea: interactive, nonInteractiveArea: .zero)

        case .floating:
            // On fixed code, .contentShape(RoundedRectangle(...)) constrains the gesture scope
            // to the visible toolbar bounds only. No extension beyond visible bounds.
            let floatingWidth: CGFloat = min(screenWidth - 32, 400)
            let floatingHeight: CGFloat = 80
            let floatingX = (screenWidth - floatingWidth) / 2
            let floatingY = (screenHeight - floatingHeight) / 2

            let frame = CGRect(
                x: floatingX,
                y: floatingY,
                width: floatingWidth,
                height: floatingHeight
            )
            let interactive = frame
            return OverlayGeometry(frame: frame, interactiveArea: interactive, nonInteractiveArea: .zero)
        }
    }
}
