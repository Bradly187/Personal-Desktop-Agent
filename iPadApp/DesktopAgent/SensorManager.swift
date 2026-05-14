import ARKit
import Combine
import CoreMotion
import Foundation

/// Centralized lifecycle controller for all 6 sensors.
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
    let gazeTracker: GazeTracker
    let headTracker: HeadTracker
    let keywordListener: KeywordListener
    let soundDetector: SoundDetector
    let audioStreamer: AudioStreamer

    // MARK: - Shared Dependencies

    private let sharedAudioSession: SharedAudioSession
    private let settings: SettingsStore
    private var cancellables = Set<AnyCancellable>()

    // MARK: - Published Sensor States (3.6)

    @Published var sensorStates: [SensorState] = []

    // MARK: - Hardware Availability (3.5)

    /// Whether device motion (accelerometer/gyroscope) is available for TiltSensor.
    private let isTiltAvailable: Bool

    /// Whether ARKit face tracking is supported (TrueDepth camera) for GazeTracker/HeadTracker.
    private let isFaceTrackingAvailable: Bool

    // MARK: - Initialization (3.1)

    init(ws: WebSocketManager, settings: SettingsStore) {
        self.settings = settings

        // Create shared audio infrastructure
        let audioSession = SharedAudioSession()
        self.sharedAudioSession = audioSession

        // Check hardware availability before instantiating sensors
        let motionManager = CMMotionManager()
        self.isTiltAvailable = motionManager.isDeviceMotionAvailable
        self.isFaceTrackingAvailable = ARFaceTrackingConfiguration.isSupported

        // Instantiate all 6 sensors with shared dependencies
        self.tiltSensor = TiltSensor(ws: ws, settings: settings)
        self.gazeTracker = GazeTracker(ws: ws, settings: settings)
        self.headTracker = HeadTracker(ws: ws, settings: settings)
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
        if settings.gazeEnabled {
            _startGaze()
        }
        if settings.headEnabled {
            _startHead()
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

    /// Stops all sensors and releases all hardware resources.
    func stopAll() {
        tiltSensor.stop()
        gazeTracker.stop()
        headTracker.stop()
        keywordListener.stop()
        soundDetector.stop()
        audioStreamer.stop()

        _updateState(id: "tilt", isRunning: false)
        _updateState(id: "gaze", isRunning: false)
        _updateState(id: "head", isRunning: false)
        _updateState(id: "keyword", isRunning: false)
        _updateState(id: "sound", isRunning: false)
        _updateState(id: "audio", isRunning: false)
    }

    // MARK: - Individual Sensor Start/Stop

    private func _startTilt() {
        guard isTiltAvailable else {
            print("SensorManager: TiltSensor unavailable — CMMotionManager.isDeviceMotionAvailable == false")
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

    private func _startGaze() {
        guard isFaceTrackingAvailable else {
            print("SensorManager: GazeTracker unavailable — ARFaceTrackingConfiguration.isSupported == false")
            _updateState(id: "gaze", isAvailable: false, lastError: "TrueDepth camera not available")
            return
        }
        gazeTracker.start()
        _updateState(id: "gaze", isRunning: true)
    }

    private func _stopGaze() {
        gazeTracker.stop()
        _updateState(id: "gaze", isRunning: false)
    }

    private func _startHead() {
        guard isFaceTrackingAvailable else {
            print("SensorManager: HeadTracker unavailable — ARFaceTrackingConfiguration.isSupported == false")
            _updateState(id: "head", isAvailable: false, lastError: "TrueDepth camera not available")
            return
        }
        headTracker.start()
        _updateState(id: "head", isRunning: true)
    }

    private func _stopHead() {
        headTracker.stop()
        _updateState(id: "head", isRunning: false)
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

        // Gaze toggle
        settings.$gazeEnabled
            .removeDuplicates()
            .dropFirst()
            .sink { [weak self] enabled in
                guard let self else { return }
                self._updateState(id: "gaze", isEnabled: enabled)
                if enabled {
                    self._startGaze()
                } else {
                    self._stopGaze()
                }
            }
            .store(in: &cancellables)

        // Head toggle
        settings.$headEnabled
            .removeDuplicates()
            .dropFirst()
            .sink { [weak self] enabled in
                guard let self else { return }
                self._updateState(id: "head", isEnabled: enabled)
                if enabled {
                    self._startHead()
                } else {
                    self._stopHead()
                }
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

        // Keyword list — non-empty means enabled
        settings.$keywordList
            .removeDuplicates()
            .dropFirst()
            .sink { [weak self] keywords in
                guard let self else { return }
                let enabled = !keywords.isEmpty
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
                id: "gaze",
                isEnabled: settings.gazeEnabled,
                isRunning: false,
                isAvailable: isFaceTrackingAvailable,
                lastError: isFaceTrackingAvailable ? nil : "TrueDepth camera not available"
            ),
            SensorState(
                id: "head",
                isEnabled: settings.headEnabled,
                isRunning: false,
                isAvailable: isFaceTrackingAvailable,
                lastError: isFaceTrackingAvailable ? nil : "TrueDepth camera not available"
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
    let id: String          // "tilt", "gaze", "head", "keyword", "sound", "audio"
    var isEnabled: Bool     // from SettingsStore toggle
    var isRunning: Bool     // actual runtime state
    var isAvailable: Bool   // hardware capability check
    var lastError: String?  // most recent failure reason
}
