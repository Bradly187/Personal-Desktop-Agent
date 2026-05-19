import SwiftUI

// MARK: - DAButton (11.1)

/// Reusable button with 80pt minimum touch target, icon + label layout,
/// and built-in accessibility annotations.
/// Uses AppTheme colors and DesignTokens sizing for consistent visual language.
struct DAButton: View {
    let label: String
    let icon: String
    let action: () -> Void

    @Environment(\.appTheme) private var theme

    var body: some View {
        Button(action: action) {
            VStack(spacing: DesignTokens.Spacing.sm) {
                Image(systemName: icon)
                    .font(.system(size: DesignTokens.Size.iconSize))
                    .foregroundStyle(theme.accent)
                Text(label)
                    .font(DesignTokens.Typography.caption)
                    .foregroundStyle(theme.textPrimary)
            }
            .frame(minWidth: DesignTokens.Size.touchTargetMin,
                   minHeight: DesignTokens.Size.touchTargetMin)
            .background(theme.surfaceSecondary)
            .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
        .accessibilityHint("Double-tap to activate")
    }
}

// MARK: - Preview

#if DEBUG
#Preview {
    HStack(spacing: DesignTokens.Spacing.md) {
        DAButton(label: "Mouse", icon: "cursorarrow") {}
        DAButton(label: "Keyboard", icon: "keyboard") {}
        DAButton(label: "Settings", icon: "gearshape") {}
    }
    .padding()
}
#endif
