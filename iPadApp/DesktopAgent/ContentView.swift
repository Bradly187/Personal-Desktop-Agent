import SwiftUI

struct ContentView: View {
    @EnvironmentObject var wsManager: WebSocketManager
    @EnvironmentObject var settings: SettingsStore

    var body: some View {
        TabView {
            CommandPadView()
                .tabItem {
                    Label("Commands", systemImage: "square.grid.3x3.fill")
                }

            TrackpadView()
                .tabItem {
                    Label("Trackpad", systemImage: "hand.point.up")
                }

            ScientificKeypadView()
                .tabItem {
                    Label("Keypad", systemImage: "function")
                }

            HandwritingCanvasView()
                .tabItem {
                    Label("Write", systemImage: "pencil.and.scribble")
                }

            SettingsView()
                .tabItem {
                    Label("Settings", systemImage: "gearshape.fill")
                }
        }
        .overlay(alignment: .top) {
            ConnectionBanner()
        }
    }
}

// Small top banner showing WebSocket connection state
private struct ConnectionBanner: View {
    @EnvironmentObject var wsManager: WebSocketManager

    private var color: Color {
        switch wsManager.state {
        case .connected: return .green
        case .connecting, .reconnecting: return .yellow
        case .disconnected: return .red
        }
    }

    private var label: String {
        switch wsManager.state {
        case .connected: return "Connected"
        case .connecting: return "Connecting…"
        case .reconnecting(let attempt): return "Reconnecting (\(attempt))…"
        case .disconnected: return "Disconnected"
        }
    }

    var body: some View {
        HStack(spacing: 6) {
            Circle()
                .fill(color)
                .frame(width: 8, height: 8)
            Text(label)
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 4)
        .background(.regularMaterial, in: Capsule())
        .padding(.top, 4)
    }
}
