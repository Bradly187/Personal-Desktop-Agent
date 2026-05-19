import SwiftUI

// MARK: - AppTheme (10.1)

/// Semantic color theme that adapts to dark mode and high contrast automatically
/// by using system UIColor semantic colors.
struct AppTheme {
    let surfacePrimary: Color
    let surfaceSecondary: Color
    let surfaceTertiary: Color
    let textPrimary: Color
    let textSecondary: Color
    let accent: Color
    let destructive: Color
    let success: Color
    let warning: Color
    let connected: Color
    let connecting: Color
    let disconnected: Color
}

// MARK: - Default Theme

extension AppTheme {
    /// Default theme using system semantic colors that adapt to dark mode
    /// and increased contrast accessibility settings automatically.
    static let `default` = AppTheme(
        surfacePrimary: Color(.systemBackground),
        surfaceSecondary: Color(.secondarySystemGroupedBackground),
        surfaceTertiary: Color(.tertiarySystemGroupedBackground),
        textPrimary: Color(.label),
        textSecondary: Color(.secondaryLabel),
        accent: Color.accentColor,
        destructive: Color.red,
        success: Color.green,
        warning: Color.orange,
        connected: Color.green,
        connecting: Color.yellow,
        disconnected: Color.red
    )
}

// MARK: - Environment Key (10.2)

struct AppThemeKey: EnvironmentKey {
    static let defaultValue = AppTheme.default
}

// MARK: - EnvironmentValues Extension (10.3)

extension EnvironmentValues {
    var appTheme: AppTheme {
        get { self[AppThemeKey.self] }
        set { self[AppThemeKey.self] = newValue }
    }
}

// MARK: - Adaptive Glass Modifier (10.4)

extension View {
    /// Applies `.glassEffect()` on iPadOS 26+ or `.regularMaterial` background on earlier versions.
    @ViewBuilder
    func adaptiveGlass() -> some View {
        if #available(iOS 26, *) {
            // TODO: Replace with self.glassEffect() when building with Xcode 26 SDK
            // .glassEffect() is only available in iPadOS 26+ (Xcode 26 beta)
            self.background(.regularMaterial)
        } else {
            self.background(.regularMaterial)
        }
    }
}
