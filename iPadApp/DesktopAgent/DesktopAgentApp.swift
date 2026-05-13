import SwiftUI
import Combine

@main
struct DesktopAgentApp: App {
    @StateObject private var wsManager = WebSocketManager()
    @StateObject private var settings = SettingsStore()
    @StateObject private var audioStreamer = AudioStreamerController()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(wsManager)
                .environmentObject(settings)
                .environmentObject(audioStreamer)
                .onAppear {
                    wsManager.settings = settings
                    audioStreamer.wire(ws: wsManager, settings: settings)
                    wsManager.connect()
                }
                .onDisappear {
                    audioStreamer.stop()
                    wsManager.disconnect()
                }
                .onReceive(settings.$audioStreamEnabled) { enabled in
                    if enabled {
                        audioStreamer.start()
                    } else {
                        audioStreamer.stop()
                    }
                }
        }
    }
}

/// Thin controller that owns the AudioStreamer and exposes observable state.
/// Separated from AudioStreamer to allow lazy initialization after wsManager is ready.
@MainActor
final class AudioStreamerController: ObservableObject {
    @Published var isStreaming = false

    private var streamer: AudioStreamer?
    private var cancellable: AnyCancellable?

    func wire(ws: WebSocketManager, settings: SettingsStore) {
        streamer = AudioStreamer(ws: ws, settings: settings)
        // Observe streamer's isStreaming state
        cancellable = streamer?.$isStreaming
            .receive(on: RunLoop.main)
            .assign(to: &$isStreaming)
    }

    func start() {
        streamer?.start()
    }

    func stop() {
        streamer?.stop()
    }
}
