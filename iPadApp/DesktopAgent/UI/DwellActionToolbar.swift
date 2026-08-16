import SwiftUI

/// Toolbar displaying the 5 dwell action buttons: left-click, right-click, double-click, drag, scroll-toggle.
/// Highlights the active action with a filled background and ≥2pt border; inactive buttons use muted background.
/// All buttons meet the 44×44pt minimum touch target required by iOS accessibility guidelines.
///
/// **Validates: Requirements 6.1, 6.2, 6.4**
struct DwellActionToolbar: View {
    @ObservedObject var settings: SettingsStore
    @ObservedObject var ws: WebSocketManager

    @Environment(\.appTheme) private var theme

    /// Minimum touch target per iOS accessibility guidelines (44×44pt).
    private let minTouchTarget: CGFloat = 44

    var body: some View {
        HStack(spacing: DesignTokens.Spacing.md) {
            actionButton(
                icon: "cursorarrow.click",
                label: "Left Click",
                isActive: settings.activeDwellAction == .leftClick && !settings.isDragging,
                accessibilityLabel: "Left click dwell action"
            ) {
                selectAction(.leftClick)
            }

            actionButton(
                icon: "contextualmenu.and.cursorarrow",
                label: "Right Click",
                isActive: settings.activeDwellAction == .rightClick,
                accessibilityLabel: "Right click dwell action"
            ) {
                selectAction(.rightClick)
            }

            actionButton(
                icon: "cursorarrow.click.2",
                label: "Double Click",
                isActive: settings.activeDwellAction == .doubleClick,
                accessibilityLabel: "Double click dwell action"
            ) {
                selectAction(.doubleClick)
            }

            dragButton()

            scrollToggleButton()
        }
        .padding(.horizontal, DesignTokens.Spacing.lg)
        .padding(.vertical, DesignTokens.Spacing.sm)
    }

    // MARK: — Action Button Builder

    @ViewBuilder
    private func actionButton(
        icon: String,
        label: String,
        isActive: Bool,
        accessibilityLabel: String,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            VStack(spacing: DesignTokens.Spacing.xs) {
                Image(systemName: icon)
                    .font(.system(size: DesignTokens.Size.iconSize))
                    .foregroundStyle(isActive ? theme.accent : theme.textSecondary)
                Text(label)
                    .font(DesignTokens.Typography.caption)
                    .foregroundStyle(isActive ? theme.textPrimary : theme.textSecondary)
                    .lineLimit(1)
                    .minimumScaleFactor(0.8)
            }
            .frame(minWidth: minTouchTarget, minHeight: minTouchTarget)
            .padding(DesignTokens.Spacing.xs)
            .background(
                RoundedRectangle(cornerRadius: DesignTokens.Radius.sm)
                    .fill(isActive ? theme.accent.opacity(0.2) : theme.surfaceSecondary)
            )
            .overlay(
                RoundedRectangle(cornerRadius: DesignTokens.Radius.sm)
                    .stroke(isActive ? theme.accent : Color.clear, lineWidth: isActive ? 2.5 : 0)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(accessibilityLabel)
        .accessibilityAddTraits(isActive ? .isSelected : [])
    }

    // MARK: — Drag Button (with pulsing animation when dragging)

    @ViewBuilder
    private func dragButton() -> some View {
        let isActive = settings.activeDwellAction == .dragStart ||
                       settings.activeDwellAction == .dragEnd ||
                       settings.isDragging

        Button {
            selectDragAction()
        } label: {
            VStack(spacing: DesignTokens.Spacing.xs) {
                Image(systemName: "arrow.up.and.down.and.arrow.left.and.right")
                    .font(.system(size: DesignTokens.Size.iconSize))
                    .foregroundStyle(isActive ? theme.accent : theme.textSecondary)
                Text("Drag")
                    .font(DesignTokens.Typography.caption)
                    .foregroundStyle(isActive ? theme.textPrimary : theme.textSecondary)
                    .lineLimit(1)
            }
            .frame(minWidth: minTouchTarget, minHeight: minTouchTarget)
            .padding(DesignTokens.Spacing.xs)
            .background(
                RoundedRectangle(cornerRadius: DesignTokens.Radius.sm)
                    .fill(isActive ? theme.accent.opacity(0.2) : theme.surfaceSecondary)
            )
            .overlay(
                RoundedRectangle(cornerRadius: DesignTokens.Radius.sm)
                    .stroke(isActive ? theme.accent : Color.clear, lineWidth: isActive ? 2.5 : 0)
            )
            .overlay(
                // Pulsing animation when actively dragging
                RoundedRectangle(cornerRadius: DesignTokens.Radius.sm)
                    .stroke(theme.accent, lineWidth: 2)
                    .opacity(settings.isDragging ? 1 : 0)
                    .animation(
                        settings.isDragging
                            ? .easeInOut(duration: 0.8).repeatForever(autoreverses: true)
                            : .default,
                        value: settings.isDragging
                    )
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(settings.isDragging ? "Dragging in progress" : "Drag dwell action")
        .accessibilityAddTraits(isActive ? .isSelected : [])
    }

    // MARK: — Scroll Toggle Button

    @ViewBuilder
    private func scrollToggleButton() -> some View {
        let isActive = settings.momentumScrollEnabled

        Button {
            toggleMomentumScroll()
        } label: {
            VStack(spacing: DesignTokens.Spacing.xs) {
                Image(systemName: "scroll")
                    .font(.system(size: DesignTokens.Size.iconSize))
                    .foregroundStyle(isActive ? theme.accent : theme.textSecondary)
                Text("Glide")
                    .font(DesignTokens.Typography.caption)
                    .foregroundStyle(isActive ? theme.textPrimary : theme.textSecondary)
                    .lineLimit(1)
            }
            .frame(minWidth: minTouchTarget, minHeight: minTouchTarget)
            .padding(DesignTokens.Spacing.xs)
            .background(
                RoundedRectangle(cornerRadius: DesignTokens.Radius.sm)
                    .fill(isActive ? theme.accent.opacity(0.2) : theme.surfaceSecondary)
            )
            .overlay(
                RoundedRectangle(cornerRadius: DesignTokens.Radius.sm)
                    .stroke(isActive ? theme.accent : Color.clear, lineWidth: isActive ? 2.5 : 0)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(isActive ? "Glide scrolling enabled" : "Glide scrolling disabled")
        .accessibilityHint("Double-tap to toggle whether a two-finger flick keeps scrolling after you lift")
        .accessibilityAddTraits(isActive ? .isSelected : [])
    }

    // MARK: — Actions

    private func selectAction(_ action: DwellActionType) {
        // Setting the property triggers DwellActionSyncer, which handles
        // send-or-queue logic based on WebSocket connection state (Req 6.3, 6.7).
        settings.activeDwellAction = action
    }

    private func selectDragAction() {
        // If currently dragging (in drag-end mode), tapping drag button is a no-op.
        // User must complete the drag via dwell. Otherwise, enter drag-start mode.
        guard !settings.isDragging else { return }
        selectAction(.dragStart)
    }

    /// Toggles momentum ("glide") scrolling. Purely local — unlike the dwell
    /// action and the old edge-scroll toggle this replaced, nothing is sent to
    /// the PC: momentum is computed entirely on the iPad (R7.3).
    private func toggleMomentumScroll() {
        settings.momentumScrollEnabled.toggle()
    }
}
