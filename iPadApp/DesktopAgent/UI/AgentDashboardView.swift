import SwiftUI

// MARK: - AgentDashboardView (Agent tab)
//
// The persistent agent dashboard — the whitepaper's "Canvas" as a tab. Hybrid:
//   • Native status section, rendered from local WebSocketManager state, so the
//     tab always shows something even before the PC pushes anything.
//   • An agent-authored A2UI canvas region (A2UICanvasStore), pushed by the PC
//     and cached on-device so it survives tab switches and app restarts.
//
// Canvas control taps reply over the same channel as the overlay
// (sendA2UIEvent), tagged event="canvas"; the PC decides what each means.

struct AgentDashboardView: View {
    @EnvironmentObject var wsManager: WebSocketManager
    @EnvironmentObject var canvasStore: A2UICanvasStore
    @Environment(\.appTheme) private var theme

    @State private var lastCommand: String = ""
    @State private var lastNotification: String = ""

    // A fresh A2UIState per appearance; routes canvas taps back to the PC.
    @State private var canvasState: A2UIState?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: DesignTokens.Spacing.lg) {
                DASectionHeader(title: "Agent")

                nativeStatusCard

                // ── Agent-authored canvas region ──────────────────────────
                if let surface = canvasStore.canvas, let state = canvasState {
                    A2UIRenderer(surface: surface, state: state)
                } else {
                    emptyCanvasCard
                }
            }
            .padding(DesignTokens.Spacing.lg)
        }
        .onAppear { canvasState = makeCanvasState() }
        .onReceive(wsManager.commandFeed) { lastCommand = $0 }
        .onReceive(wsManager.proactiveFeed) { lastNotification = $0.title }
    }

    // MARK: - Native status (always available)

    private var nativeStatusCard: some View {
        DACard {
            VStack(alignment: .leading, spacing: DesignTokens.Spacing.md) {
                statusRow("Connection", connectionText, connectionColor)
                statusRow("Microphone", wsManager.micMuted ? "Muted" : "Live",
                          wsManager.micMuted ? theme.warning : theme.accent)
                if !lastCommand.isEmpty {
                    statusRow("Last command", lastCommand, theme.textSecondary)
                }
                if !lastNotification.isEmpty {
                    statusRow("Last alert", lastNotification, theme.textSecondary)
                }
            }
        }
    }

    private func statusRow(_ label: String, _ value: String, _ valueColor: Color) -> some View {
        HStack {
            Text(label)
                .font(DesignTokens.Typography.body)
                .foregroundStyle(theme.textSecondary)
            Spacer()
            Text(value)
                .font(DesignTokens.Typography.body)
                .foregroundStyle(valueColor)
                .lineLimit(1)
                .truncationMode(.middle)
        }
    }

    private var emptyCanvasCard: some View {
        DACard {
            VStack(alignment: .leading, spacing: DesignTokens.Spacing.sm) {
                Text("No agent dashboard yet")
                    .font(DesignTokens.Typography.headline)
                    .foregroundStyle(theme.textPrimary)
                Text("The agent will populate this space when it has something to show.")
                    .font(DesignTokens.Typography.caption)
                    .foregroundStyle(theme.textSecondary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    // MARK: - Helpers

    private func makeCanvasState() -> A2UIState {
        A2UIState { event, value, values in
            let sid = canvasStore.canvas?.surfaceId ?? "dashboard"
            wsManager.sendA2UIEvent(surfaceId: sid, event: event, value: value, values: values)
        }
    }

    private var connectionText: String {
        switch wsManager.state {
        case .connected:            return "Connected"
        case .connecting:           return "Connecting…"
        case .reconnecting(let n):  return "Reconnecting (\(n))"
        case .disconnected:         return "Disconnected"
        }
    }

    private var connectionColor: Color {
        switch wsManager.state {
        case .connected:    return theme.accent
        case .disconnected: return theme.warning
        default:            return theme.textSecondary
        }
    }
}
