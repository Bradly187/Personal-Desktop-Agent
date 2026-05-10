import SwiftUI

@main
struct DesktopAgentApp: App {
    @StateObject private var wsManager = WebSocketManager()
    @StateObject private var settings = SettingsStore()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(wsManager)
                .environmentObject(settings)
                .onAppear { wsManager.connect() }
                .onDisappear { wsManager.disconnect() }
        }
    }
}
