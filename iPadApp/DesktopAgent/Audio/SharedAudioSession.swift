import AVFoundation
import Foundation

/// Callback type for audio buffer distribution.
/// Called on the audio render thread — keep processing minimal or dispatch.
typealias AudioTapHandler = (AVAudioPCMBuffer, AVAudioTime) -> Void

/// Manages a single shared AVAudioEngine instance for all audio sensors.
///
/// KeywordListener, SoundDetector, and AudioStreamer all install taps on the
/// same engine's inputNode, eliminating the bug where three separate engines
/// compete for the microphone.
///
/// Uses a fan-out pattern: a single tap on inputNode bus 0 distributes buffers
/// to all registered handlers. This avoids the AVAudioEngine limitation of
/// one tap per bus per node.
///
/// Reference-counted: the engine runs while at least one consumer is registered,
/// and stops + deactivates the session when the last consumer is removed.
///
/// Handles AVAudioSession interruptions (e.g., incoming FaceTime call) by pausing
/// on interruption began and re-activating on interruption ended.
@MainActor
final class SharedAudioSession {

    // MARK: - Public Properties

    /// The shared AVAudioEngine. Sensors can access `engine.inputNode` for format info.
    let engine: AVAudioEngine

    /// Whether the audio session is currently active (engine running, consumers > 0).
    var isActive: Bool {
        !activeConsumers.isEmpty && engine.isRunning
    }

    /// The input node's native format (available after engine is prepared/started).
    var inputFormat: AVAudioFormat {
        engine.inputNode.outputFormat(forBus: 0)
    }

    // MARK: - Private State

    /// Set of consumer identifiers currently using the shared engine.
    private var activeConsumers: Set<String> = []

    /// Registered tap handlers keyed by consumer ID. Protected by a lock for audio thread safety.
    private var tapHandlers: [String: AudioTapHandler] = [:]
    private let handlersLock = NSLock()

    /// Whether the shared tap is currently installed on inputNode.
    private var tapInstalled = false

    /// Observation token for interruption notifications.
    private var interruptionObserver: NSObjectProtocol?

    // MARK: - Initialization

    init() {
        engine = AVAudioEngine()
        _subscribeToInterruptions()
    }

    deinit {
        if let observer = interruptionObserver {
            NotificationCenter.default.removeObserver(observer)
        }
    }

    // MARK: - Public API

    /// Configures AVAudioSession for `.playAndRecord` with `.mixWithOthers` and starts the engine.
    ///
    /// Call this before any sensor installs a tap. Safe to call multiple times —
    /// if the engine is already running, this is a no-op.
    func activate() throws {
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(
            .playAndRecord,
            mode: .default,
            options: [.mixWithOthers, .defaultToSpeaker]
        )
        try session.setActive(true, options: [])

        if !engine.isRunning {
            _installSharedTapIfNeeded()
            try engine.start()
        }
    }

    /// Registers a consumer by identifier and installs a buffer handler.
    /// Activates the engine if not already running.
    ///
    /// - Parameters:
    ///   - id: A unique string identifying the consumer (e.g., "KeywordListener").
    ///   - handler: Closure called on the audio render thread with each buffer.
    func addConsumer(_ id: String, handler: @escaping AudioTapHandler) {
        activeConsumers.insert(id)
        handlersLock.lock()
        tapHandlers[id] = handler
        handlersLock.unlock()

        if !engine.isRunning {
            do {
                try activate()
            } catch {
                print("SharedAudioSession: failed to activate for consumer '\(id)': \(error)")
            }
        }
    }

    /// Registers a consumer without a tap handler (legacy compatibility).
    /// Activates the engine if not already running.
    ///
    /// - Parameter id: A unique string identifying the consumer (e.g., "KeywordListener").
    func addConsumer(_ id: String) {
        addConsumer(id) { _, _ in }
    }

    /// Removes a consumer's tap handler and unregisters it.
    /// Deactivates the engine when the last consumer is removed.
    ///
    /// - Parameter id: The identifier previously passed to `addConsumer(_:handler:)`.
    func removeConsumer(_ id: String) {
        activeConsumers.remove(id)
        handlersLock.lock()
        tapHandlers.removeValue(forKey: id)
        handlersLock.unlock()

        if activeConsumers.isEmpty {
            deactivate()
        }
    }

    /// Stops the engine and deactivates the audio session, but only when no consumers remain.
    ///
    /// If consumers are still registered, this is a no-op to prevent stopping the engine
    /// while other sensors still need it.
    func deactivate() {
        guard activeConsumers.isEmpty else { return }

        _removeSharedTap()
        engine.stop()

        do {
            try AVAudioSession.sharedInstance().setActive(false, options: [.notifyOthersOnDeactivation])
        } catch {
            print("SharedAudioSession: failed to deactivate session: \(error)")
        }
    }

    // MARK: - Shared Tap Management

    /// Installs a single tap on inputNode bus 0 that fans out buffers to all registered handlers.
    private func _installSharedTapIfNeeded() {
        guard !tapInstalled else { return }

        let inputNode = engine.inputNode
        let format = inputNode.outputFormat(forBus: 0)

        inputNode.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buffer, time in
            guard let self else { return }
            self.handlersLock.lock()
            let handlers = self.tapHandlers.values
            self.handlersLock.unlock()

            for handler in handlers {
                handler(buffer, time)
            }
        }
        tapInstalled = true
    }

    /// Removes the shared tap from inputNode.
    private func _removeSharedTap() {
        guard tapInstalled else { return }
        engine.inputNode.removeTap(onBus: 0)
        tapInstalled = false
    }

    // MARK: - Interruption Handling

    /// Subscribes to AVAudioSession.interruptionNotification to handle system interruptions
    /// (e.g., incoming phone call, FaceTime, Siri).
    private func _subscribeToInterruptions() {
        interruptionObserver = NotificationCenter.default.addObserver(
            forName: AVAudioSession.interruptionNotification,
            object: AVAudioSession.sharedInstance(),
            queue: nil
        ) { [weak self] notification in
            Task { @MainActor [weak self] in
                self?._handleInterruption(notification)
            }
        }
    }

    /// Handles interruption began (pause engine) and interruption ended (re-activate if consumers remain).
    private func _handleInterruption(_ notification: Notification) {
        guard let userInfo = notification.userInfo,
              let typeValue = userInfo[AVAudioSessionInterruptionTypeKey] as? UInt,
              let type = AVAudioSession.InterruptionType(rawValue: typeValue) else {
            return
        }

        switch type {
        case .began:
            // System interrupted audio — engine is automatically paused by the system.
            // We don't need to explicitly stop, but we note the state.
            print("SharedAudioSession: interruption began")

        case .ended:
            // Interruption ended — re-activate if we still have consumers.
            guard !activeConsumers.isEmpty else { return }

            let options = userInfo[AVAudioSessionInterruptionOptionKey] as? UInt ?? 0
            let shouldResume = AVAudioSession.InterruptionOptions(rawValue: options)
                .contains(.shouldResume)

            if shouldResume {
                do {
                    try activate()
                    print("SharedAudioSession: re-activated after interruption")
                } catch {
                    print("SharedAudioSession: failed to re-activate after interruption: \(error)")
                }
            }

        @unknown default:
            break
        }
    }
}
