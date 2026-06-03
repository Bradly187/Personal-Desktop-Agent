import Combine
import CoreMotion
import Foundation

/// Centralized lifecycle controller for all 4 sensors.
///
/// Observes SettingsStore `@Published` toggles via Combine and reactively
/// starts/stops sensors when their toggle changes. Owns the SharedAudioSession
/// used by the three audio sensors.
///
/// Hardware availability is checked before starting each sensor — unavailable
/// sensors log a warning and remain stopped (no crash).
///
/// Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6
@MainActor
final class SensorManager: ObservableObject {

    // MARK: - Sensors

    let tiltSensor: TiltSensor
    let keywordListener: KeywordListener
    let soundDetector: SoundDetector
    let audioStreamer: AudioStreamer

    // MARK: - Shared Dependencies

    private let sharedAudioSession: SharedAudioSession
    private let settings: SettingsStore
    private weak var ws: WebSocketManager?
    private var cancellables = Set<AnyCancellable>()

    // MARK: - Published Sensor States (3.6)

    @Published var sensorStates: [SensorState] = []

    /// Timestamp of last activity per sensor — used by SensorActivityBar for pulse animations.
    @Published var lastActivity: [String: Date] = [:]

    // MARK: - Hardware Availability (3.5)

    /// Whether device motion (accelerometer/gyroscope) is available for TiltSensor.
    private let isTiltAvailable: Bool

    // MARK: - Initialization (3.1)

    init(ws: WebSocketManager, settings: SettingsStore) {
        self.settings = settings
        self.ws = ws

        // Create shared audio infrastructure
        let audioSession = SharedAudioSession()
        self.sharedAudioSession = audioSession

        // Check hardware availability before instantiating sensors
        let motionManager = CMMotionManager()
        self.isTiltAvailable = motionManager.isDeviceMotionAvailable

        // Instantiate all sensors with shared dependencies
        self.tiltSensor = TiltSensor(ws: ws, settings: settings)
        self.keywordListener = KeywordListener(ws: ws, settings: settings, sharedAudioSession: audioSession)
        self.soundDetector = SoundDetector(ws: ws, settings: settings, sharedAudioSession: audioSession)
        self.audioStreamer = AudioStreamer(ws: ws, settings: settings, sharedAudioSession: audioSession)

        // Initialize sensor states
        self.sensorStates = _buildInitialStates()

        // Subscribe to settings toggles (3.4)
        _subscribeToSettings()
    }

    // MARK: - Lifecycle (3.2, 3.3)

    /// Starts sensors whose toggles are enabled AND whose hardware is available.
    /// Sensors with unavailable hardware log a warning and remain stopped.
    func startAll() {
        if settings.tiltEnabled {
            _startTilt()
        }
        if !settings.keywordList.isEmpty {
            _startKeyword()
        }
        if !settings.soundMappings.isEmpty {
            _startSound()
        }
        if settings.audioStreamEnabled {
            _startAudioStream()
        }
    }

    /// G4: Stop only non-audio sensors for background mode.
    /// Core Motion (tilt) requires foreground.
    /// Audio sensors (keyword, sound, audioStream) can continue in background
    /// when the app has UIBackgroundModes: ["audio"] entitlement.
    func stopNonAudioSensors() {
        tiltSensor.stop()
        _updateState(id: "tilt", isRunning: false)
        // keyword, sound, audio remain running
    }

    /// Stops all sensors and releases all hardware resources.
    func stopAll() {
        tiltSensor.stop()
        keywordListener.stop()
        soundDetector.stop()
        audioStreamer.stop()

        _updateState(id: "tilt", isRunning: false)
        _updateState(id: "keyword", isRunning: false)
        _updateState(id: "sound", isRunning: false)
        _updateState(id: "audio", isRunning: false)
    }

    // MARK: - Individual Sensor Start/Stop

    private func _startTilt() {
        guard isTiltAvailable else {
            AppLogger.shared.warning("SensorManager", "TiltSensor unavailable — CMMotionManager.isDeviceMotionAvailable == false")
            _updateState(id: "tilt", isAvailable: false, lastError: "Device motion hardware unavailable")
            return
        }
        tiltSensor.start()
        _updateState(id: "tilt", isRunning: true)
    }

    private func _stopTilt() {
        tiltSensor.stop()
        _updateState(id: "tilt", isRunning: false)
    }

    private func _startKeyword() {
        keywordListener.start()
        _updateState(id: "keyword", isRunning: true)
    }

    private func _stopKeyword() {
        keywordListener.stop()
        _updateState(id: "keyword", isRunning: false)
    }

    private func _startSound() {
        soundDetector.start()
        _updateState(id: "sound", isRunning: true)
    }

    private func _stopSound() {
        soundDetector.stop()
        _updateState(id: "sound", isRunning: false)
    }

    private func _startAudioStream() {
        audioStreamer.start()
        _updateState(id: "audio", isRunning: true)
    }

    private func _stopAudioStream() {
        audioStreamer.stop()
        _updateState(id: "audio", isRunning: false)
    }

    // MARK: - Combine Subscriptions (3.4)

    private func _subscribeToSettings() {
        // Tilt toggle
        settings.$tiltEnabled
            .removeDuplicates()
            .dropFirst() // Skip initial value (handled by startAll)
            .sink { [weak self] enabled in
                guard let self else { return }
                self._updateState(id: "tilt", isEnabled: enabled)
                if enabled {
                    self._startTilt()
                } else {
                    self._stopTilt()
                }
            }
            .store(in: &cancellables)

        // Tilt settings changes — debounced to avoid thrash during slider drag
        Publishers.CombineLatest4(
            settings.$tiltSensitivity.removeDuplicates(),
            settings.$tiltDeadZone.removeDuplicates(),
            settings.$tiltRange.removeDuplicates(),
            settings.$tiltInverted.removeDuplicates()
        )
        .dropFirst()
        .debounce(for: .milliseconds(100), scheduler: RunLoop.main)
        .sink { [weak self] _ in
            self?.tiltSensor.updateSettings()
        }
        .store(in: &cancellables)

        settings.$tiltPositionMode
            .removeDuplicates()
            .dropFirst()
            .sink { [weak self] _ in
                self?.tiltSensor.updateSettings()
            }
            .store(in: &cancellables)

        // Audio stream toggle
        settings.$audioStreamEnabled
            .removeDuplicates()
            .dropFirst()
            .sink { [weak self] enabled in
                guard let self else { return }
                self._updateState(id: "audio", isEnabled: enabled)
                if enabled {
                    self._startAudioStream()
                } else {
                    self._stopAudioStream()
                }
            }
            .store(in: &cancellables)

        // Keyword list — non-empty means enabled; debounce to avoid restart thrash during editing
        settings.$keywordList
            .removeDuplicates()
            .dropFirst()
            .debounce(for: .milliseconds(500), scheduler: RunLoop.main)
            .sink { [weak self] keywords in
                guard let self else { return }
                let nonEmpty = keywords.filter { !$0.trimmingCharacters(in: .whitespaces).isEmpty }
                let enabled = !nonEmpty.isEmpty
                self._updateState(id: "keyword", isEnabled: enabled)
                if enabled {
                    // Restart to pick up new keyword list
                    self._stopKeyword()
                    self._startKeyword()
                } else {
                    self._stopKeyword()
                }
            }
            .store(in: &cancellables)

        // Manual pain-day override — sync to PC immediately on toggle
        settings.$manualPainDay
            .removeDuplicates()
            .dropFirst()
            .sink { [weak self] active in
                self?.ws?.sendPainDayOverride(active: active)
            }
            .store(in: &cancellables)

        // Flare degrade profile — sync all flags to the PC whenever any change.
        // Mixed types, so map each to Void and merge; debounce collapses the
        // initial @Published replays into one emission, which dropFirst skips.
        Publishers.MergeMany([
            settings.$flareVoiceDegrades.map { _ in () }.eraseToAnyPublisher(),
            settings.$flareGestureDegrades.map { _ in () }.eraseToAnyPublisher(),
            settings.$flareTiltDegrades.map { _ in () }.eraseToAnyPublisher(),
            settings.$flareSoundDegrades.map { _ in () }.eraseToAnyPublisher(),
            settings.$flareVadScale.map { _ in () }.eraseToAnyPublisher(),
        ])
        .debounce(for: .milliseconds(300), scheduler: RunLoop.main)
        .dropFirst()
        .sink { [weak self] in
            guard let self else { return }
            self.ws?.sendFlareProfile(
                voiceDegrades: self.settings.flareVoiceDegrades,
                gestureDegrades: self.settings.flareGestureDegrades,
                tiltDegrades: self.settings.flareTiltDegrades,
                soundDegrades: self.settings.flareSoundDegrades,
                vadScale: self.settings.flareVadScale
            )
        }
        .store(in: &cancellables)

        // Disabled gestures — sync when assessment changes and on first connect
        settings.$disabledGestures
            .removeDuplicates()
            .sink { [weak self] disabled in
                self?.ws?.sendGestureAssessment(disabled: Array(disabled))
            }
            .store(in: &cancellables)

        // Sound mappings — non-empty means enabled
        settings.$soundMappings
            .removeDuplicates()
            .dropFirst()
            .sink { [weak self] mappings in
                guard let self else { return }
                let enabled = !mappings.isEmpty
                self._updateState(id: "sound", isEnabled: enabled)
                if enabled {
                    self._startSound()
                } else {
                    self._stopSound()
                }
            }
            .store(in: &cancellables)

    }

    // MARK: - Sensor State Management (3.6)

    private func _buildInitialStates() -> [SensorState] {
        [
            SensorState(
                id: "tilt",
                isEnabled: settings.tiltEnabled,
                isRunning: false,
                isAvailable: isTiltAvailable,
                lastError: isTiltAvailable ? nil : "Device motion hardware unavailable"
            ),
            SensorState(
                id: "keyword",
                isEnabled: !settings.keywordList.isEmpty,
                isRunning: false,
                isAvailable: true, // Speech recognition available on all iPads
                lastError: nil
            ),
            SensorState(
                id: "sound",
                isEnabled: !settings.soundMappings.isEmpty,
                isRunning: false,
                isAvailable: true, // Microphone available on all iPads
                lastError: nil
            ),
            SensorState(
                id: "audio",
                isEnabled: settings.audioStreamEnabled,
                isRunning: false,
                isAvailable: true, // Microphone available on all iPads
                lastError: nil
            ),
        ]
    }

    private func _updateState(id: String, isEnabled: Bool? = nil, isRunning: Bool? = nil, isAvailable: Bool? = nil, lastError: String? = nil) {
        guard let index = sensorStates.firstIndex(where: { $0.id == id }) else { return }
        if let isEnabled { sensorStates[index].isEnabled = isEnabled }
        if let isRunning { sensorStates[index].isRunning = isRunning }
        if let isAvailable { sensorStates[index].isAvailable = isAvailable }
        if let lastError { sensorStates[index].lastError = lastError }
    }
}

// MARK: - SensorState Model

/// Represents the observable state of a single sensor for UI consumption.
struct SensorState: Identifiable {
    let id: String          // "tilt", "keyword", "sound", "audio"
    var isEnabled: Bool     // from SettingsStore toggle
    var isRunning: Bool     // actual runtime state
    var isAvailable: Bool   // hardware capability check
    var lastError: String?  // most recent failure reason
}
