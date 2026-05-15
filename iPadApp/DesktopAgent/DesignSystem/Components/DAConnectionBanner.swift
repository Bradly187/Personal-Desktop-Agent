import SwiftUI

// MARK: - DAConnectionBanner (11.4)

/// Full-width connection status banner with prominent state indication
/// and a reconnect action when disconnected.
/// Minimum 44pt tap target, shows connection state with color,
/// attempt count when reconnecting, and discovered mDNS host.
struct DAConnectionBanner: View {
    @EnvironmentObject var wsManager: WebSocketManager
    @EnvironmentObject var serviceDiscovery: ServiceDiscovery

    @Environment(\.appTheme) private var theme

    var body: some View {
        Button(action: handleTap) {
            HStack(spacing: DesignTokens.Spacing.md) {
                // Status indicator circle
                Circle()
                    .fill(statusColor)
                    .frame(width: 12, height: 12)

                // Status text
                VStack(alignment: .leading, spacing: DesignTokens.Spacing.xs) {
                    Text(statusTitle)
                        .font(DesignTokens.Typography.body)
                        .foregroundStyle(theme.textPrimary)

                    if let subtitle = statusSubtitle {
                        Text(subtitle)
                            .font(DesignTokens.Typography.caption)
                            .foregroundStyle(theme.textSecondary)
                    }
                }

                Spacer()

                // Reconnect button when disconnected
                if isDisconnected {
                    Text("Reconnect")
                        .font(DesignTokens.Typography.caption)
                        .foregroundStyle(theme.accent)
                        .padding(.horizontal, DesignTokens.Spacing.md)
                        .padding(.vertical, DesignTokens.Spacing.sm)
                        .background(theme.accent.opacity(0.15))
                        .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.sm))
                }
            }
            .padding(.horizontal, DesignTokens.Spacing.lg)
            .frame(maxWidth: .infinity, minHeight: 44)
            .background(statusColor.opacity(0.1))
            .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.sm))
        }
        .buttonStyle(.plain)
        .disabled(!isDisconnected)
        .allowsHitTesting(isDisconnected)
        .accessibilityLabel(accessibilityLabelText)
        .accessibilityHint(isDisconnected ? "Double-tap to reconnect" : "")
    }

    // MARK: - Computed Properties

    private var statusColor: Color {
        switch wsManager.state {
        case .connected:
            return theme.connected
        case .connecting:
            return theme.connecting
        case .reconnecting:
            return theme.connecting
        case .disconnected:
            return theme.disconnected
        }
    }

    private var statusTitle: String {
        switch wsManager.state {
        case .connected:
            return "Connected"
        case .connecting:
            return "Connecting…"
        case .reconnecting(let attempt):
            return "Reconnecting (attempt \(attempt))…"
        case .disconnected:
            return "Disconnected"
        }
    }

    private var statusSubtitle: String? {
        if let host = serviceDiscovery.discoveredHost {
            return "Discovered: \(host)"
        }
        return nil
    }

    private var isDisconnected: Bool {
        if case .disconnected = wsManager.state { return true }
        return false
    }

    private var accessibilityLabelText: String {
        switch wsManager.state {
        case .connected:
            return "Connection status: Connected"
        case .connecting:
            return "Connection status: Connecting"
        case .reconnecting(let attempt):
            return "Connection status: Reconnecting, attempt \(attempt)"
        case .disconnected:
            return "Connection status: Disconnected"
        }
    }

    // MARK: - Actions

    private func handleTap() {
        guard isDisconnected else { return }
        wsManager.connect()
    }
}

// MARK: - Preview

#if DEBUG
#Preview {
    VStack(spacing: DesignTokens.Spacing.md) {
        DAConnectionBanner()
            .environmentObject(WebSocketManager())
            .environmentObject(ServiceDiscovery())
    }
    .padding()
}
#endif
