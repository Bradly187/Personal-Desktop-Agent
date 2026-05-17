import CoreMotion
import Foundation
import UIKit

/// Streams iPad device tilt at 60 Hz to the PC bridge.
/// Dead-zone filtering prevents jitter while the device is resting.
/// Accelerometer impulse detection fires tilt_tap on a sharp table-tap.
@MainActor
final class TiltSensor {

    private let motion = CMMotionManager()
    private weak var ws: WebSocketManager?
    private var settings: SettingsStore?

    // MARK: — Neutral gravity (position-mapping calibration)

    /// The calibrated neutral gravity vector. When the device is held at this orientation,
    /// `computePosition` outputs (0.5, 0.5) — screen center.
    /// Default: iPad at ~45° from horizontal → (-0.707, 0, -0.707).
    var neutralGravity: SIMD3<Double> = SIMD3(-0.707, 0.0, -0.707)

    /// Timestamp of the last successful calibration, used for 500ms debounce.
    private var lastCalibrationTime: Date?

    // MARK: — Position-mode message suppression and stationary lock

    /// Last sent coordinates for suppression (skip if delta < 0.001 on both axes)
    /// Internal access for @testable import in property tests.
    var lastSentX: Double = 0.5
    var lastSentY: Double = 0.5

    /// Timestamp when the device first became stationary (angular velocity ≤ 0.01 rad/s)
    /// Internal access for testability via @testable import.
    var stationaryStartTime: Date?

    /// Frozen output coordinates during stationary lock
    /// Internal access for testability via @testable import.
    var lockedCoords: (x: Double, y: Double)?

    // Impulse detection state
    private var prevAccelMag: Double = 0
    private let tapThreshold: Double = 2.5   // g-force delta that counts as a tap
    private var tapCooldown: Bool = false
    private let tapCooldownDuration: Double = 0.25

    init(ws: WebSocketManager, settings: SettingsStore) {
        self.ws = ws
        self.settings = settings
        // Initialize neutral gravity from persisted calibration if available
        if settings.hasPersistedNeutral {
            neutralGravity = SIMD3(settings.neutralGravityX, settings.neutralGravityY, settings.neutralGravityZ)
        }
    }

    // MARK: — Lifecycle

    func start() {
        guard motion.isDeviceMotionAvailable else {
            print("TiltSensor: DeviceMotion unavailable")
            return
        }

        // Load persisted neutral gravity on start (may have been saved in a previous session)
        if let s = settings, s.hasPersistedNeutral {
            neutralGravity = SIMD3(s.neutralGravityX, s.neutralGravityY, s.neutralGravityZ)
        }

        motion.deviceMotionUpdateInterval = 1.0 / 60.0
        // Fix #8: Use a dedicated background queue for motion callbacks.
        // Trig computations and WebSocket sends no longer block the main thread at 60Hz.
        // WebSocketManager.send() already dispatches to its own serial queue.
        // Safety: maxConcurrentOperationCount = 1 ensures serial access to mutable state
        // (lastSentX, lastSentY, stationaryStartTime, lockedCoords, prevAccelMag).
        let motionQueue = OperationQueue()
        motionQueue.name = "tilt.sensor.motion"
        motionQueue.maxConcurrentOperationCount = 1
        motionQueue.qualityOfService = .userInteractive
        motion.startDeviceMotionUpdates(to: motionQueue) { [weak self] data, _ in
            guard let self, let data else { return }
            self.handle(data)
        }
    }

    func stop() {
        motion.stopDeviceMotionUpdates()
    }

    // MARK: — Calibration

    /// Captures the current gravity vector as the new neutral position.
    /// Debounced: ignores calls within 500ms of the last successful calibration.
    /// Provides light haptic feedback on success (SVT-safe — no startling).
    func calibrate() {
        // Debounce: ignore if called within 500ms of last calibration
        if let last = lastCalibrationTime, Date().timeIntervalSince(last) < 0.5 {
            return
        }

        // Capture current gravity from the motion manager
        guard let data = motion.deviceMotion else { return }
        let g = data.gravity
        neutralGravity = SIMD3(g.x, g.y, g.z)

        // Persist to SettingsStore
        if let s = settings {
            s.neutralGravityX = g.x
            s.neutralGravityY = g.y
            s.neutralGravityZ = g.z
        }

        // Record calibration time for debounce
        lastCalibrationTime = Date()

        // Light haptic feedback — subtle confirmation without startling (SVT-aware)
        let generator = UIImpactFeedbackGenerator(style: .light)
        generator.impactOccurred()
    }

    // MARK: — Position Computation

    /// Computes normalized screen coordinates from the given gravity vector.
    ///
    /// Pure function: reads `neutralGravity` and `settings` but does NOT modify any state.
    /// Returns (x, y) in [0.0, 1.0] where (0, 0) is top-left and (1, 1) is bottom-right.
    ///
    /// Algorithm:
    /// 1. Convert gravity to SIMD3
    /// 2. Compute pitch/roll angles for neutral and current via atan2
    /// 3. Delta = current - neutral, convert to degrees
    /// 4. Apply dead zone (zero out small deltas)
    /// 5. Linear map to [0.0, 1.0]
    /// 6. Apply inversion if enabled
    /// 7. Clamp to [0.0, 1.0]
    func computePosition(gravity: CMAcceleration) -> (x: Double, y: Double) {
        guard let settings else { return (0.5, 0.5) }

        // 1. Convert gravity to SIMD3
        let g = SIMD3<Double>(gravity.x, gravity.y, gravity.z)

        // 2. Compute angular displacement from neutral using atan2
        // Pitch: rotation around device X-axis (forward/back tilt)
        let neutralPitch = atan2(-neutralGravity.x, -neutralGravity.z)
        let currentPitch = atan2(-g.x, -g.z)

        // Roll: rotation around device Y-axis (left/right tilt)
        let neutralRoll = atan2(-neutralGravity.y, -neutralGravity.z)
        let currentRoll = atan2(-g.y, -g.z)

        // 3. Delta in radians, convert to degrees
        let deltaPitch = currentPitch - neutralPitch
        let deltaRoll = currentRoll - neutralRoll

        var deltaPitchDeg = deltaPitch * (180.0 / .pi)
        var deltaRollDeg = deltaRoll * (180.0 / .pi)

        // 4. Apply dead zone: zero out deltas smaller than threshold
        let deadZone = settings.tiltDeadZone
        if abs(deltaPitchDeg) < deadZone { deltaPitchDeg = 0 }
        if abs(deltaRollDeg) < deadZone { deltaRollDeg = 0 }

        // 5. Linear map to [0.0, 1.0]
        let tiltRange = settings.tiltRange
        var x = 0.5 + (deltaRollDeg / tiltRange) * 0.5   // roll right → x increases
        var y = 0.5 - (deltaPitchDeg / tiltRange) * 0.5  // pitch forward → y decreases (toward top)

        // 6. Apply inversion if enabled
        if settings.tiltInverted {
            x = 1.0 - x
            y = 1.0 - y
        }

        // 7. Clamp to [0.0, 1.0]
        x = max(0.0, min(1.0, x))
        y = max(0.0, min(1.0, y))

        return (x, y)
    }

    // MARK: — Processing

    private func handle(_ data: CMDeviceMotion) {
        guard let settings else { return }
        guard settings.tiltEnabled else { return }

        if settings.tiltPositionMode {
            // --- Position-mapped mode ---
            // Compute absolute screen coordinates from tilt angle relative to neutral.
            var (x, y) = computePosition(gravity: data.gravity)

            // --- Stationary lock ---
            // When angular velocity is ≤ 0.01 rad/s on both axes for ≥ 200ms,
            // lock output to the last stable coordinates to prevent micro-drift.
            let rot = data.rotationRate
            let angularVelX = abs(rot.x)
            let angularVelY = abs(rot.y)
            let isStationary = angularVelX <= 0.01 && angularVelY <= 0.01

            if isStationary {
                if stationaryStartTime == nil {
                    // Just became stationary — start timing
                    stationaryStartTime = Date()
                    lockedCoords = (x: x, y: y)
                } else if let startTime = stationaryStartTime,
                          Date().timeIntervalSince(startTime) >= 0.2 {
                    // Stationary for ≥ 200ms — use locked coordinates
                    if let locked = lockedCoords {
                        x = locked.x
                        y = locked.y
                    }
                }
            } else {
                // Motion resumed — clear stationary lock
                stationaryStartTime = nil
                lockedCoords = nil
            }

            // --- Message suppression ---
            // Skip sending if position hasn't changed meaningfully (delta < 0.001 on both axes)
            if abs(x - lastSentX) < 0.001 && abs(y - lastSentY) < 0.001 {
                // Suppress — no meaningful change
            } else {
                ws?.sendTiltPosition(x: x, y: y)
                lastSentX = x
                lastSentY = y
            }
        } else {
            // --- Legacy velocity-based mode ---
            // Gravity-compensated tilt projection: project rotationRate into a
            // ground-aligned frame so "tilt right" maps cleanly to horizontal
            // regardless of holding angle.

            let dz = settings.tiltDeadZone
            let sensitivity = settings.tiltSensitivity

            let g = data.gravity
            let rot = data.rotationRate

            let pitch = atan2(-g.x, -g.z)
            let cosPitch = cos(pitch)
            let sinPitch = sin(pitch)

            let worldYaw = rot.y * cosPitch + rot.z * sinPitch
            let worldPitch = rot.x

            var rx = worldPitch * sensitivity
            var ry = worldYaw * sensitivity

            if settings.tiltInverted {
                rx = -rx
                ry = -ry
            }

            if abs(rx) > dz || abs(ry) > dz {
                ws?.sendTilt(rx: rx, ry: ry)
            }
        }

        // --- Impulse tap detection (runs regardless of mode) ---
        let a = data.userAcceleration
        let mag = sqrt(a.x * a.x + a.y * a.y + a.z * a.z)
        if mag - prevAccelMag > tapThreshold && !tapCooldown {
            ws?.sendTiltTap()
            tapCooldown = true
            DispatchQueue.main.asyncAfter(deadline: .now() + tapCooldownDuration) { [weak self] in
                self?.tapCooldown = false
            }
        }
        prevAccelMag = mag
    }
}
