import Combine
import SwiftUI

/// Floating toast that briefly shows the last command sent to the PC.
/// Auto-dismisses after 2 seconds. Shows only the most recent command.
struct CommandToast: View {
    @EnvironmentObject var wsManager: WebSocketManager

    @Environment(\.appTheme) private var theme

    @State private var currentMessage: String?
    @State private var dismissTask: Task<Void, Never>?

    var body: some View {
        if let message = currentMessage {
            HStack(spacing: DesignTokens.Spacing.sm) {
                Image(systemName: "arrow.up.circle.fill")
                    .foregroundStyle(theme.accent)
                    .font(.system(size: 16))
                Text(message)
                    .font(DesignTokens.Typography.caption)
                    .foregroundStyle(theme.textPrimary)
                    .lineLimit(1)
            }
            .padding(.horizontal, DesignTokens.Spacing.lg)
            .padding(.vertical, DesignTokens.Spacing.sm)
            .background(.regularMaterial, in: Capsule())
            .shadow(color: .black.opacity(0.1), radius: 4, y: 2)
            .transition(.move(edge: .top).combined(with: .opacity))
            .accessibilityLabel("Command sent: \(message)")
        }
    }

    /// Call this from .onReceive to handle new commands.
    func show(_ message: String) {
        withAnimation(.easeInOut(duration: 0.2)) {
            currentMessage = message
        }
        dismissTask?.cancel()
        dismissTask = Task { @MainActor in
            try? await Task.sleep(nanoseconds: 2_000_000_000)
            guard !Task.isCancelled else { return }
            withAnimation(.easeInOut(duration: 0.3)) {
                currentMessage = nil
            }
        }
    }
}

/// Standalone toast view model that can be used as a modifier.
/// Attach to any view and feed it the wsManager.commandFeed subject.
struct CommandToastOverlay: ViewModifier {
    @EnvironmentObject var wsManager: WebSocketManager
    @Environment(\.appTheme) private var theme

    @State private var currentMessage: String?
    @State private var isError: Bool = false
    @State private var dismissTask: Task<Void, Never>?

    func body(content: Content) -> some View {
        content
            .overlay(alignment: .top) {
                if let message = currentMessage {
                    HStack(spacing: DesignTokens.Spacing.sm) {
                        Image(systemName: isError ? "exclamationmark.triangle.fill" : "arrow.up.circle.fill")
                            .foregroundStyle(isError ? .orange : theme.accent)
                            .font(.system(size: 16))
                        Text(message)
                            .font(DesignTokens.Typography.caption)
                            .foregroundStyle(theme.textPrimary)
                            .lineLimit(2)
                    }
                    .padding(.horizontal, DesignTokens.Spacing.lg)
                    .padding(.vertical, DesignTokens.Spacing.sm)
                    .background(.regularMaterial, in: Capsule())
                    .shadow(color: .black.opacity(0.1), radius: 4, y: 2)
                    .padding(.top, 60)
                    .transition(.move(edge: .top).combined(with: .opacity))
                    .accessibilityLabel(isError ? "Error: \(message)" : "Command sent: \(message)")
                    .allowsHitTesting(false)
                }
            }
            .onReceive(wsManager.commandFeed) { message in
                show(message, error: false)
            }
            .onReceive(wsManager.errorFeed) { message in
                show(message, error: true)
            }
    }

    private func show(_ message: String, error: Bool) {
        withAnimation(.easeInOut(duration: 0.2)) {
            currentMessage = message
            isError = error
        }
        dismissTask?.cancel()
        dismissTask = Task { @MainActor in
            // Errors stay visible longer (4s vs 2s)
            let ns: UInt64 = error ? 4_000_000_000 : 2_000_000_000
            try? await Task.sleep(nanoseconds: ns)
            guard !Task.isCancelled else { return }
            withAnimation(.easeInOut(duration: 0.3)) {
                currentMessage = nil
            }
        }
    }
}

extension View {
    /// Adds a floating command toast that shows the last action sent to the PC.
    func commandToast() -> some View {
        modifier(CommandToastOverlay())
    }
}
