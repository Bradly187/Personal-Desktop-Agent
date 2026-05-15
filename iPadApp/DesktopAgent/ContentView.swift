import SwiftUI

struct ContentView: View {
    @EnvironmentObject var wsManager: WebSocketManager
    @EnvironmentObject var settings: SettingsStore
    @EnvironmentObject var screenshotStore: ScreenshotStore

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
            // Task 13.6: Replaced private ConnectionBanner with shared DAConnectionBanner component
            DAConnectionBanner()
                .padding(.horizontal, DesignTokens.Spacing.lg)
                .padding(.top, DesignTokens.Spacing.sm)
        }
        .overlay {
            // Task 11.2: Dwell action toolbar overlaid on main content
            DwellToolbarContainer(settings: settings, ws: wsManager)
                .allowsHitTesting(true)
        }
        .overlay {
            ScreenshotOverlayView()
        }
        .onReceive(wsManager.$lastMessage) { message in
            guard let message else { return }
            if case .screenshot(_, let imageBase64, let mime) = message {
                screenshotStore.handleScreenshot(base64: imageBase64, mime: mime)
            }
        }
    }
}
