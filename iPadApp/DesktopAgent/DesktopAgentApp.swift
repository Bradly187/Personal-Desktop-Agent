import SwiftUI

@main
struct DesktopAgentApp: App {
    @StateObject private var wsManager: WebSocketManager
    @StateObject private var settings: SettingsStore
    @StateObject private var sensorManager: SensorManager
    @StateObject private var screenshotStore = ScreenshotStore()
    @StateObject private var serviceDiscovery = ServiceDiscovery()

    // Fix #5: Track scene phase for proper background/foreground lifecycle
    @Environment(\.scenePhase) private var scenePhase

    /// Syncs feature toggle changes to PC via WebSocket. Retained for app lifetime.
    private let featureToggleSyncer: FeatureToggleSyncer

    /// Syncs dwell action type changes to PC via WebSocket. Retained for app lifetime.
    private let dwellActionSyncer: DwellActionSyncer

    init() {
        let ws = WebSocketManager()
        let s = SettingsStore()
        _wsManager = StateObject(wrappedValue: ws)
        _settings = StateObject(wrappedValue: s)
        _sensorManager = StateObject(wrappedValue: SensorManager(ws: ws, settings: s))
        featureToggleSyncer = FeatureToggleSyncer(settings: s, ws: ws)
        dwellActionSyncer = DwellActionSyncer(settings: s, ws: ws)
    }

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environment(\.appTheme, .default)
                .environmentObject(wsManager)
                .environmentObject(settings)
                .environmentObject(sensorManager)
                .environmentObject(screenshotStore)
                .environmentObject(serviceDiscovery)
                .onAppear {
                    wsManager.settings = settings
                    wsManager.serviceDiscovery = serviceDiscovery

                    // Determine if user has manually changed host from default
                    let defaultHost = "192.168.18.2"
                    serviceDiscovery.hasManualOverride = (settings.serverHost != defaultHost)

                    // Start mDNS discovery
                    serviceDiscovery.startBrowsing()

                    sensorManager.startAll()
                    wsManager.connect()
                }
                .onDisappear {
                    serviceDiscovery.stopBrowsing()
                    sensorManager.stopAll()
                    wsManager.disconnect()
                }
        }
        // Fix #5: Reliable lifecycle handling via scenePhase.
        // .onDisappear is unreliable on iPad — scenePhase catches background/termination.
        .onChange(of: scenePhase) { newPhase in
            switch newPhase {
            case .background:
                // Stop all sensors and disconnect to save battery and prevent
                // ARSession/CMMotionManager/AVAudioEngine running in background.
                sensorManager.stopAll()
                wsManager.disconnect()
            case .active:
                // Resume sensors and reconnect when returning to foreground.
                sensorManager.startAll()
                if wsManager.state == .disconnected {
                    wsManager.connect()
                }
            case .inactive:
                // Transitional state (e.g., app switcher) — do nothing.
                break
            @unknown default:
                break
            }
        }
    }
}
