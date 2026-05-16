import SwiftUI

/// Container view that positions the `DwellActionToolbar` based on the user's selected
/// `ToolbarPosition` (top-anchored, bottom-anchored, or floating).
///
/// Floating position uses a drag gesture constrained to the safe area bounds.
/// The selected position is persisted to UserDefaults via `SettingsStore.toolbarPosition`.
///
/// Touch passthrough architecture:
/// - A non-interactive GeometryReader captures container size (hit testing disabled).
/// - The toolbar is placed as an `.overlay` on the GeometryReader, making it a
///   sibling in the rendering tree — NOT a child of the disabled view.
/// - This ensures the toolbar receives touches while empty space passes through
///   to views underneath (TabView content, tab bar).
///
/// **Validates: Requirements 6.5, 6.8**
struct DwellToolbarContainer: View {
    @ObservedObject var settings: SettingsStore
    @ObservedObject var ws: WebSocketManager

    @Environment(\.appTheme) private var theme

    /// Tracks the in-progress drag translation (floating mode only).
    @GestureState private var dragTranslation: CGSize = .zero

    /// Captured geometry size for floating constraint calculations.
    @State private var containerSize: CGSize = .zero
    @State private var safeAreaInsets: EdgeInsets = EdgeInsets()

    var body: some View {
        // Measurement layer — captures size, never intercepts touches.
        // The toolbar is an overlay (not a child), so it is NOT affected
        // by the parent's .allowsHitTesting(false).
        GeometryReader { geometry in
            Color.clear
                .onAppear {
                    containerSize = geometry.size
                    safeAreaInsets = geometry.safeAreaInsets
                }
                .onChange(of: geometry.size) { _, newSize in
                    containerSize = newSize
                    safeAreaInsets = geometry.safeAreaInsets
                }
        }
        .allowsHitTesting(false)
        .overlay(alignment: toolbarAlignment) {
            // Interactive toolbar — only its visible frame receives touches.
            // Because this is an overlay (not a child of the disabled GeometryReader),
            // hit testing works correctly on the toolbar content.
            toolbarPositioned
        }
    }

    // MARK: — Alignment

    private var toolbarAlignment: Alignment {
        switch settings.toolbarPosition {
        case .top: return .top
        case .bottom: return .bottom
        case .floating: return .center
        }
    }

    // MARK: — Positioned Toolbar

    @ViewBuilder
    private var toolbarPositioned: some View {
        switch settings.toolbarPosition {
        case .top:
            toolbarView
                .background(
                    RoundedRectangle(cornerRadius: DesignTokens.Radius.md)
                        .fill(theme.surfacePrimary.opacity(0.95))
                )
                .contentShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
                .padding(.top, safeAreaInsets.top > 0 ? 0 : DesignTokens.Spacing.sm)
                .padding(.horizontal, DesignTokens.Spacing.md)

        case .bottom:
            VStack(spacing: 0) {
                toolbarView
                    .background(
                        RoundedRectangle(cornerRadius: DesignTokens.Radius.md)
                            .fill(theme.surfacePrimary.opacity(0.95))
                    )
                    .contentShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
                    .padding(.horizontal, DesignTokens.Spacing.md)

                // Spacer pushes toolbar above the custom tab bar (80pt).
                Spacer()
                    .frame(height: 88)
                    .allowsHitTesting(false)
            }
            // The VStack's frame is only as tall as toolbar + spacer, not full-screen.
            // Empty space around it passes through because the overlay alignment
            // positions this at .bottom without filling the container.
            .fixedSize(horizontal: false, vertical: true)

        case .floating:
            floatingToolbar
        }
    }

    // MARK: — Toolbar Content

    private var toolbarView: some View {
        DwellActionToolbar(settings: settings, ws: ws)
    }

    // MARK: — Floating Position

    @ViewBuilder
    private var floatingToolbar: some View {
        let toolbarWidth: CGFloat = min(containerSize.width - DesignTokens.Spacing.lg * 2, 400)
        let toolbarHeight: CGFloat = 80

        // Compute the combined offset (persisted + in-progress drag)
        let combinedOffset = CGSize(
            width: settings.toolbarFloatingOffset.width + dragTranslation.width,
            height: settings.toolbarFloatingOffset.height + dragTranslation.height
        )

        // Constrain to safe area bounds
        let constrainedOffset = constrainToSafeArea(
            offset: combinedOffset,
            containerSize: containerSize,
            safeArea: safeAreaInsets,
            toolbarSize: CGSize(width: toolbarWidth, height: toolbarHeight)
        )

        toolbarView
            .frame(maxWidth: toolbarWidth)
            .background(
                RoundedRectangle(cornerRadius: DesignTokens.Radius.md)
                    .fill(theme.surfacePrimary.opacity(0.95))
                    .shadow(color: .black.opacity(0.2), radius: 8, x: 0, y: 4)
            )
            .offset(constrainedOffset)
            .contentShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
            .gesture(
                DragGesture()
                    .updating($dragTranslation) { value, state, _ in
                        state = value.translation
                    }
                    .onEnded { value in
                        let newOffset = CGSize(
                            width: settings.toolbarFloatingOffset.width + value.translation.width,
                            height: settings.toolbarFloatingOffset.height + value.translation.height
                        )
                        // Constrain final position to safe area
                        settings.toolbarFloatingOffset = constrainToSafeArea(
                            offset: newOffset,
                            containerSize: containerSize,
                            safeArea: safeAreaInsets,
                            toolbarSize: CGSize(width: toolbarWidth, height: toolbarHeight)
                        )
                    }
            )
            .accessibilityLabel("Floating dwell action toolbar")
            .accessibilityHint("Drag to reposition")
    }

    // MARK: — Safe Area Constraint

    /// Constrains the toolbar offset so the toolbar remains fully within the safe area.
    private func constrainToSafeArea(
        offset: CGSize,
        containerSize: CGSize,
        safeArea: EdgeInsets,
        toolbarSize: CGSize
    ) -> CGSize {
        let centerX = containerSize.width / 2
        let centerY = containerSize.height / 2

        let minX = safeArea.leading + toolbarSize.width / 2
        let maxX = containerSize.width - safeArea.trailing - toolbarSize.width / 2
        let minY = safeArea.top + toolbarSize.height / 2
        let maxY = containerSize.height - safeArea.bottom - toolbarSize.height / 2

        let proposedCenterX = centerX + offset.width
        let proposedCenterY = centerY + offset.height

        let clampedX = max(minX, min(maxX, proposedCenterX))
        let clampedY = max(minY, min(maxY, proposedCenterY))

        return CGSize(
            width: clampedX - centerX,
            height: clampedY - centerY
        )
    }
}
