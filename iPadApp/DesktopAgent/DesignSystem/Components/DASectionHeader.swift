import SwiftUI

// MARK: - DASectionHeader (11.3)

/// Section header using DesignTokens.Typography.headline font
/// and DesignTokens.Spacing.lg padding for consistent section separation.
struct DASectionHeader: View {
    let title: String

    @Environment(\.appTheme) private var theme

    var body: some View {
        Text(title)
            .font(DesignTokens.Typography.headline)
            .foregroundStyle(theme.textPrimary)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.vertical, DesignTokens.Spacing.lg)
            .accessibilityAddTraits(.isHeader)
    }
}

// MARK: - Preview

#if DEBUG
#Preview {
    VStack(alignment: .leading) {
        DASectionHeader(title: "Connection")
        DASectionHeader(title: "Sensors")
        DASectionHeader(title: "Appearance")
    }
    .padding()
}
#endif
