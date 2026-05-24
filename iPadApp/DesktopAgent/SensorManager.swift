import ARKit
import Combine
import CoreMotion
import Foundation

/// Centralized lifecycle controller for all 7 sensors.
///
/// Observes SettingsStore `@Published` toggles via Combine and reactively
/// starts/stops sensors when their toggle changes. Owns the SharedAudioSession
/// used by the three audio sensors and the SharedFaceSession used by
/// GazeTracker and HeadTracker.
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
    let lidarStreamer: LiDARStreamer

    // MARK: - Shared Dependencies

    private let sharedAudioSession: SharedAudioSession
    private let sharedFaceSession: SharedFaceSession
    private let settings: SettingsStore
    private weak var ws: WebSocketManager?
    private var cancellables = Set<AnyCancellable>()

    // MARK: - Published Sensor States (3.6)

    @Published var sensorStates: [SensorState] = []

    /// Timestamp of last activity per sensor — used by SensorActivityBar for pulse animations.
    @Published var lastActivity: [String: Date] = [:]

    /// E3: Mirrors SharedFaceSession.permissionFailureMessage so ContentView
    /// (an ObservableObject consumer of SensorManager) can render a banner.
    @Published var cameraPermissionMessage: String? = nil

    // MARK: - Hardware Availability (3.5)

    /// Whether device motion (accelerometer/gyroscope) is available for TiltSensor.
    private let isTiltAvailable: Bool

    /// Whether ARKit face tracking is supported (TrueDepth camera) for GazeTracker/HeadTracker.
    private let isFaceTrackingAvailable: Bool

    /// Whether this device has a LiDAR scanner (iPad Pro 2020+, iPhone 12 Pro+).
    private let isLiDARAvailable: Bool

    // MARK: - Initialization (3.1)

    init(ws: WebSocketManager, settings: SettingsStore) {
        self.settings = settings
        self.ws = ws

        // Create shared audio infrastructure
        let audioSession = SharedAudioSession()
        self.sharedAudioSession = audioSession

        // Create shared face-tracking session (GazeTracker + HeadTracker share one ARSession)
        let faceSession = SharedFaceSession()
        self.sharedFaceSession = faceSession

        // Check hardware availability before instantiating sensors
        let motionManager = CMMotionManager()
        self.isTiltAvailable = motionManager.isDeviceMotionAvailable
        self.isFaceTrackingAvailable = ARFaceTrackingConfiguration.isSupported
        self.isLiDARAvailable = LiDARStreamer.isSupported

        // Instantiate all 7 sensors with shared dependencies
        self.tiltSensor = TiltSensor(ws: ws, settings: settings)
        self.gazeTracker = GazeTracker(ws: ws, settings: settings, sharedFaceSession: faceSession)
        self.headTracker = HeadTracker(ws: ws, settings: settings, sharedFaceSession: faceSession)
        self.keywordListener = KeywordListener(ws: ws, settings: settings, sharedAudioSession: audioSession)
        self.soundDetector = SoundDetector(ws: ws, settings: settings, sharedAudioSession: audioSession)
        self.audioStreamer = AudioStreamer(ws: ws, settings: settings, sharedAudioSession: audioSession)
        self.lidarStreamer = LiDARStreamer(ws: ws)

        // Initialize sensor states
        self.sensorStates = _buildInitialStates()

        // Subscribe to settings toggles (3.4)
        _subscribeToSettings()

        // E3: Mirror SharedFaceSession permission failure into a published
        // property so ContentView re-renders when ARKit gives up on camera.
        faceSession.$permissionFailureMessage
            .receive(on: DispatchQueue.main)
            .assign(to: &$cameraPermissionMessage)
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
        if settings.lidarEnabled {
            _startLiDAR()
        }
    }

    /// G4: Stop only non-audio sensors for background mode.
    /// ARKit (gaze/head), Core Motion (tilt), LiDAR all require foreground.
    /// Audio sensors (keyword, sound, audioStream) can continue in background
    /// when the app has UIBackgroundModes: ["audio"] entitlement.
    func stopNonAudioSensors() {
        tiltSensor.stop()
        gazeTracker.stop()
        headTracker.stop()
        lidarStreamer.stop()
        _updateState(id: "tilt", isRunning: false)
        _updateState(id: "gaze", isRunning: false)
        _updateState(id: "head", isRunning: false)
        _updateState(id: "lidar", isRunning: false)
        // keyword, sound, audio remain running
    }

    /// Stops all sensors and releases all hardware resources.
    func stopAll() {
        tiltSensor.stop()
        gazeTracker.stop()
        headTracker.stop()
        keywordListener.stop()
        soundDetector.stop()
        audioStreamer.stop()
        lidarStreamer.stop()

        _updateState(id: "tilt", isRunning: false)
        _updateState(id: "gaze", isRunning: false)
        _updateState(id: "head", isRunning: false)
        _updateState(id: "keyword", isRunning: false)
        _updateState(id: "sound", isRunning: false)
        _updateState(id: "audio", isRunning: false)
        _updateState(id: "lidar", isRunning: false)
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

    private func _startGaze() {
        guard isFaceTrackingAvailable else {
            AppLogger.shared.warning("SensorManager", "GazeTracker unavailable — ARFaceTrackingConfiguration.isSupported == false")
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
            AppLogger.shared.warning("SensorManager", "HeadTracker unavailable — ARFaceTrackingConfiguration.isSupported == false")
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

    private func _startLiDAR() {
        guard isLiDARAvailable else {
            AppLogger.shared.warning("SensorManager", "LiDARStreamer unavailable — device has no LiDAR scanner")
            _updateState(id: "lidar", isAvailable: false, lastError: "LiDAR scanner not available on this device")
            return
        }
        lidarStreamer.start()
        _updateState(id: "lidar", isRunning: true)
    }

    private func _stopLiDAR() {
        lidarStreamer.stop()
        _updateState(id: "lidar", isRunning: false)
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

        // Gaze settings changes — debounced to avoid thrash during slider drag
        Publishers.CombineLatest3(
            settings.$gazeSensitivity.removeDuplicates(),
            settings.$gazeSaccadeEnterThreshold.removeDuplicates(),
            settings.$gazeSaccadeExitThreshold.removeDuplicates()
        )
        .dropFirst()
        .debounce(for: .milliseconds(100), scheduler: RunLoop.main)
        .sink { [weak self] _ in
            self?.gazeTracker.updateSettings()
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
                // Also update snapshot so processQueue sees the new enabled state
                self.headTracker.updateSettings()
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

        // LiDAR toggle
        settings.$lidarEnabled
            .removeDuplicates()
            .dropFirst()
            .sink { [weak self] enabled in
                guard let self else { return }
                self._updateState(id: "lidar", isEnabled: enabled)
                if enabled {
                    self._startLiDAR()
                } else {
                    self._stopLiDAR()
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
            SensorState(
                id: "lidar",
                isEnabled: settings.lidarEnabled,
                isRunning: false,
                isAvailable: isLiDARAvailable,
                lastError: isLiDARAvailable ? nil : "LiDAR scanner not available on this device"
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
    let id: String          // "tilt", "gaze", "head", "keyword", "sound", "audio", "lidar"
    var isEnabled: Bool     // from SettingsStore toggle
    var isRunning: Bool     // actual runtime state
    var isAvailable: Bool   // hardware capability check
    var lastError: String?  // most recent failure reason
}
