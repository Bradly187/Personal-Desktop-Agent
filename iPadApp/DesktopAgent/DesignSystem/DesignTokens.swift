import SwiftUI

/// Single source of truth for all visual constants — sizing, spacing, radii, typography.
/// Enforces 80pt minimum touch targets for primary actions.
enum DesignTokens {

    // MARK: - Sizing

    enum Size {
        /// Minimum touch target for primary action buttons (80×80pt)
        static let touchTargetMin: CGFloat = 80
        /// Minimum touch target for secondary/compact buttons (64×64pt)
        static let touchTargetCompact: CGFloat = 64
        /// Standard icon size
        static let iconSize: CGFloat = 24
        /// Large icon size
        static let iconSizeLarge: CGFloat = 32
    }

    // MARK: - Spacing

    enum Spacing {
        static let xs: CGFloat = 4
        static let sm: CGFloat = 8
        static let md: CGFloat = 12
        static let lg: CGFloat = 16
        static let xl: CGFloat = 24
        static let xxl: CGFloat = 32
    }

    // MARK: - Corner Radii

    enum Radius {
        static let sm: CGFloat = 8
        static let md: CGFloat = 12
        static let lg: CGFloat = 16
        static let xl: CGFloat = 20
    }

    // MARK: - Typography

    enum Typography {
        static let headline: Font = .system(.title2, design: .rounded).weight(.semibold)
        static let body: Font = .system(.body, design: .default)
        static let caption: Font = .system(.caption, design: .default)
        static let mono: Font = .system(.body, design: .monospaced)
    }
}
