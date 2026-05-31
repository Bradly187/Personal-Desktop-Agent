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
        // Wire AppLogger transport so sensor logs are forwarded to the PC bridge.
        AppLogger.shared.setTransport(ws)
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(\.appTheme, .default)
                .environmentObject(wsManager)
                .environmentObject(settings)
                .environmentObject(sensorManager)
                .environmentObject(screenshotStore)
                .environmentObject(serviceDiscovery)
        }
        // Fix #5: Reliable lifecycle handling via scenePhase.
        // .onDisappear is unreliable on iPad — scenePhase catches background/termination.
        .onChange(of: scenePhase) { _, newPhase in
            switch newPhase {
            case .background:
                // G4: Keep audio sensors running in background so voice commands
                // still reach the PC when the screen locks. Tilt stops
                // (Core Motion requires foreground). WebSocket stays up.
                // NOTE: requires UIBackgroundModes: ["audio"] in Info.plist and
                // AVAudioSession category .playAndRecord with .mixWithOthers option.
                sensorManager.stopNonAudioSensors()
            case .active:
                // Only auto-start if onboarding is complete
                if UserDefaults.standard.bool(forKey: "onboardingComplete") {
                    sensorManager.startAll()
                    if wsManager.state == .disconnected {
                        wsManager.connect()
                    }
                }
            case .inactive:
                break
            @unknown default:
                break
            }
        }
    }
}

// MARK: - Root View (gates onboarding vs main app)

struct RootView: View {
    @EnvironmentObject var wsManager: WebSocketManager
    @EnvironmentObject var settings: SettingsStore
    @EnvironmentObject var sensorManager: SensorManager
    @EnvironmentObject var serviceDiscovery: ServiceDiscovery

    // Use @AppStorage so the "Re-run Onboarding" button in SettingsView can
    // flip this flag and have RootView immediately re-render to OnboardingView.
    @AppStorage("onboardingComplete") private var onboardingComplete = false

    var body: some View {
        if onboardingComplete {
            ContentView()
                .onAppear {
                    wsManager.settings = settings
                    wsManager.serviceDiscovery = serviceDiscovery

                    let defaultHost = "192.168.18.2"
                    serviceDiscovery.hasManualOverride = (settings.serverHost != defaultHost)
                    serviceDiscovery.startBrowsing()
                    sensorManager.startAll()
                    wsManager.connect()
                }
                .onDisappear {
                    serviceDiscovery.stopBrowsing()
                    sensorManager.stopAll()
                    wsManager.disconnect()
                }
        } else {
            OnboardingView(isComplete: $onboardingComplete)
        }
    }
}
