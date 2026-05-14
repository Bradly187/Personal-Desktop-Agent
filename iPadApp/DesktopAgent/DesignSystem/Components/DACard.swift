import SwiftUI

// MARK: - DACard (11.2)

/// Container card with AppTheme.surfaceSecondary background,
/// DesignTokens.Radius.md corner radius, and consistent padding.
/// Wraps arbitrary content in a visually distinct surface.
struct DACard<Content: View>: View {
    let content: Content

    @Environment(\.appTheme) private var theme

    init(@ViewBuilder content: () -> Content) {
        self.content = content()
    }

    var body: some View {
        content
            .padding(DesignTokens.Spacing.lg)
            .background(theme.surfaceSecondary)
            .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
    }
}

// MARK: - Preview

#if DEBUG
#Preview {
    DACard {
        VStack(alignment: .leading, spacing: DesignTokens.Spacing.sm) {
            Text("Card Title")
                .font(DesignTokens.Typography.headline)
            Text("Some content inside the card.")
                .font(DesignTokens.Typography.body)
        }
    }
    .padding()
}
#endif
