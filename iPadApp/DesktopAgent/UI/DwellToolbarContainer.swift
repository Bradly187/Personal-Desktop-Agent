import SwiftUI

/// Container view that positions the `DwellActionToolbar` based on the user's selected
/// `ToolbarPosition` (top-anchored, bottom-anchored, or floating).
///
/// Floating position uses a drag gesture constrained to the safe area bounds.
/// The selected position is persisted to UserDefaults via `SettingsStore.toolbarPosition`.
///
/// **Validates: Requirements 6.5, 6.8**
struct DwellToolbarContainer: View {
    @ObservedObject var settings: SettingsStore
    @ObservedObject var ws: WebSocketManager

    @Environment(\.appTheme) private var theme

    /// Tracks the in-progress drag translation (floating mode only).
    @GestureState private var dragTranslation: CGSize = .zero

    var body: some View {
        GeometryReader { geometry in
            let safeArea = geometry.safeAreaInsets
            let toolbarContent = toolbarView

            switch settings.toolbarPosition {
            case .top:
                VStack {
                    toolbarContent
                        .frame(maxWidth: .infinity)
                        .background(
                            RoundedRectangle(cornerRadius: DesignTokens.Radius.md)
                                .fill(theme.surfacePrimary.opacity(0.95))
                        )
                        .padding(.top, safeArea.top > 0 ? 0 : DesignTokens.Spacing.sm)
                        .padding(.horizontal, DesignTokens.Spacing.md)
                    Spacer()
                }

            case .bottom:
                VStack {
                    Spacer()
                    toolbarContent
                        .frame(maxWidth: .infinity)
                        .background(
                            RoundedRectangle(cornerRadius: DesignTokens.Radius.md)
                                .fill(theme.surfacePrimary.opacity(0.95))
                        )
                        .padding(.bottom, safeArea.bottom > 0 ? 0 : DesignTokens.Spacing.sm)
                        .padding(.horizontal, DesignTokens.Spacing.md)
                }

            case .floating:
                floatingToolbar(in: geometry)
            }
        }
    }

    // MARK: — Toolbar Content

    private var toolbarView: some View {
        DwellActionToolbar(settings: settings, ws: ws)
    }

    // MARK: — Floating Position

    @ViewBuilder
    private func floatingToolbar(in geometry: GeometryProxy) -> some View {
        let containerSize = geometry.size
        let safeArea = geometry.safeAreaInsets

        // Approximate toolbar dimensions for constraint calculations
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
            safeArea: safeArea,
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
                            safeArea: safeArea,
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
        // The toolbar is centered by default; offset is relative to center.
        let centerX = containerSize.width / 2
        let centerY = containerSize.height / 2

        // Compute the allowed range for the toolbar center point
        let minX = safeArea.leading + toolbarSize.width / 2
        let maxX = containerSize.width - safeArea.trailing - toolbarSize.width / 2
        let minY = safeArea.top + toolbarSize.height / 2
        let maxY = containerSize.height - safeArea.bottom - toolbarSize.height / 2

        // The toolbar center after applying offset
        let proposedCenterX = centerX + offset.width
        let proposedCenterY = centerY + offset.height

        // Clamp to safe bounds
        let clampedX = max(minX, min(maxX, proposedCenterX))
        let clampedY = max(minY, min(maxY, proposedCenterY))

        return CGSize(
            width: clampedX - centerX,
            height: clampedY - centerY
        )
    }
}
