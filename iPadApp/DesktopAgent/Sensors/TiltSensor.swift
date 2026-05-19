import CoreMotion
import Foundation
import UIKit

/// Streams iPad device tilt at 60 Hz to the PC bridge.
/// Dead-zone filtering prevents jitter while the device is resting.
/// Accelerometer impulse detection fires tilt_tap on a sharp table-tap.
///
/// Fixes applied:
/// - tapCooldown uses timestamp comparison instead of boolean flag (eliminates cross-thread race)
/// - Date() replaced with CACurrentMediaTime() in hot path (cheaper monotonic clock)
/// - Settings values snapshotted at start() to avoid cross-isolation reads at 60Hz
@MainActor
final class TiltSensor: ObservableObject {

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
    var lastSentX: Double = 0.5
    var lastSentY: Double = 0.5

    /// Monotonic timestamp when the device first became stationary (angular velocity ≤ 0.01 rad/s)
    var stationaryStartTime: CFTimeInterval?

    /// Frozen output coordinates during stationary lock
    var lockedCoords: (x: Double, y: Double)?

    // Impulse detection state
    private var prevAccelMag: Double = 0
    private let tapThreshold: Double = 2.5   // g-force delta that counts as a tap
    /// Monotonic timestamp of last tap fire — replaces boolean tapCooldown to eliminate race condition.
    private var lastTapFireTime: CFTimeInterval = 0
    private let tapCooldownDuration: Double = 0.25

    // MARK: — Snapshotted settings (avoids cross-isolation reads at 60Hz)
    private var snapshotTiltEnabled: Bool = true
    private var snapshotTiltPositionMode: Bool = true
    private var snapshotTiltDeadZone: Double = 1.5
    private var snapshotTiltSensitivity: Double = 1.0
    private var snapshotTiltRange: Double = 25.0
    private var snapshotTiltInverted: Bool = false

    init(ws: WebSocketManager, settings: SettingsStore) {
        self.ws = ws
        self.settings = settings
        // Initialize neutral gravity from persisted calibration if available
        if settings.hasPersistedNeutral {
            neutralGravity = SIMD3(settings.neutralGravityX, settings.neutralGravityY, settings.neutralGravityZ)
        }
    }

    // MARK: — Lifecycle

    /// Whether the sensor is currently running (prevents double-start).
    private var isRunning = false

    func start() {
        guard !isRunning else { return }
        guard motion.isDeviceMotionAvailable else {
            print("TiltSensor: DeviceMotion unavailable")
            return
        }

        // Load persisted neutral gravity on start
        if let s = settings, s.hasPersistedNeutral {
            neutralGravity = SIMD3(s.neutralGravityX, s.neutralGravityY, s.neutralGravityZ)
        }

        // Snapshot settings for use on the motion OperationQueue (avoids cross-isolation reads)
        if let s = settings {
            snapshotTiltEnabled = s.tiltEnabled
            snapshotTiltPositionMode = s.tiltPositionMode
            snapshotTiltDeadZone = s.tiltDeadZone
            snapshotTiltSensitivity = s.tiltSensitivity
            snapshotTiltRange = s.tiltRange
            snapshotTiltInverted = s.tiltInverted
        }

        motion.deviceMotionUpdateInterval = 1.0 / 60.0
        let motionQueue = OperationQueue()
        motionQueue.name = "tilt.sensor.motion"
        motionQueue.maxConcurrentOperationCount = 1
        motionQueue.qualityOfService = .userInteractive
        motion.startDeviceMotionUpdates(to: motionQueue) { [weak self] data, _ in
            guard let self, let data else { return }
            self.handle(data)
        }
        isRunning = true
    }

    func stop() {
        guard isRunning else { return }
        motion.stopDeviceMotionUpdates()
        isRunning = false
        lastSentX = 0.5
        lastSentY = 0.5
        stationaryStartTime = nil
        lockedCoords = nil
        prevAccelMag = 0
        lastTapFireTime = 0
    }

    /// Update snapshotted settings when user changes them. Call from MainActor.
    func updateSettings() {
        guard let s = settings else { return }
        snapshotTiltEnabled = s.tiltEnabled
        snapshotTiltPositionMode = s.tiltPositionMode
        snapshotTiltDeadZone = s.tiltDeadZone
        snapshotTiltSensitivity = s.tiltSensitivity
        snapshotTiltRange = s.tiltRange
        snapshotTiltInverted = s.tiltInverted
    }

    // MARK: — Calibration

    /// Captures the current gravity vector as the new neutral position.
    /// Debounced: ignores calls within 500ms of the last successful calibration.
    func calibrate() {
        if let last = lastCalibrationTime, Date().timeIntervalSince(last) < 0.5 {
            return
        }
        guard let data = motion.deviceMotion else { return }
        let g = data.gravity
        neutralGravity = SIMD3(g.x, g.y, g.z)

        if let s = settings {
            s.neutralGravityX = g.x
            s.neutralGravityY = g.y
            s.neutralGravityZ = g.z
        }

        lastCalibrationTime = Date()
        let generator = UIImpactFeedbackGenerator(style: .light)
        generator.impactOccurred()
    }

    // MARK: — Ratcheting (re-center without cursor jump)

    private var lastRatchetTime: Date?
    private var delayedRatchetTask: Task<Void, Never>?

    func ratchet() {
        if let last = lastRatchetTime, Date().timeIntervalSince(last) < 0.5 {
            return
        }
        guard let data = motion.deviceMotion else { return }
        let rot = data.rotationRate
        let angVel = max(abs(rot.x), abs(rot.y), abs(rot.z))

        if angVel > 0.5 {
            delayedRatchetTask?.cancel()
            delayedRatchetTask = Task { [weak self] in
                let deadline = Date().addingTimeInterval(0.3)
                while Date() < deadline {
                    try? await Task.sleep(nanoseconds: 30_000_000)
                    guard let self, let data = self.motion.deviceMotion else { return }
                    let rot = data.rotationRate
                    let vel = max(abs(rot.x), abs(rot.y), abs(rot.z))
                    if vel <= 0.5 {
                        await self.executeRatchet(gravity: data.gravity)
                        return
                    }
                }
                guard let self, let data = self.motion.deviceMotion else { return }
                await self.executeRatchet(gravity: data.gravity)
            }
        } else {
            executeRatchet(gravity: data.gravity)
        }
    }

    private func executeRatchet(gravity g: CMAcceleration) {
        neutralGravity = SIMD3(g.x, g.y, g.z)
        if let s = settings {
            s.neutralGravityX = g.x
            s.neutralGravityY = g.y
            s.neutralGravityZ = g.z
        }
        lastRatchetTime = Date()
        ws?.sendRatchet()
        let generator = UIImpactFeedbackGenerator(style: .light)
        generator.impactOccurred()
    }

    // MARK: — Position Computation

    /// Computes normalized screen coordinates from the given gravity vector.
    /// Pure function: reads `neutralGravity` and settings.
    /// Returns (x, y) in [0.0, 1.0] where (0, 0) is top-left and (1, 1) is bottom-right.
    func computePosition(gravity: CMAcceleration) -> (x: Double, y: Double) {
        guard let settings else { return (0.5, 0.5) }

        let g = SIMD3<Double>(gravity.x, gravity.y, gravity.z)

        let neutralPitch = atan2(-neutralGravity.x, -neutralGravity.z)
        let currentPitch = atan2(-g.x, -g.z)
        let neutralRoll = atan2(-neutralGravity.y, -neutralGravity.z)
        let currentRoll = atan2(-g.y, -g.z)

        let deltaPitch = currentPitch - neutralPitch
        let deltaRoll = currentRoll - neutralRoll

        var deltaPitchDeg = deltaPitch * (180.0 / .pi)
        var deltaRollDeg = deltaRoll * (180.0 / .pi)

        let deadZone = settings.tiltDeadZone
        if abs(deltaPitchDeg) < deadZone { deltaPitchDeg = 0 }
        if abs(deltaRollDeg) < deadZone { deltaRollDeg = 0 }

        let tiltRange = settings.tiltRange
        var x = 0.5 + (deltaRollDeg / tiltRange) * 0.5
        var y = 0.5 - (deltaPitchDeg / tiltRange) * 0.5

        if settings.tiltInverted {
            x = 1.0 - x
            y = 1.0 - y
        }

        x = max(0.0, min(1.0, x))
        y = max(0.0, min(1.0, y))

        return (x, y)
    }

    // MARK: — Processing (runs on serial OperationQueue, not MainActor)

    private func handle(_ data: CMDeviceMotion) {
        guard snapshotTiltEnabled else { return }

        if snapshotTiltPositionMode {
            // --- Position-mapped mode ---
            var (x, y) = computePosition(gravity: data.gravity)

            // --- Stationary lock ---
            // Uses CACurrentMediaTime() instead of Date() for cheaper monotonic timing.
            let rot = data.rotationRate
            let angularVelX = abs(rot.x)
            let angularVelY = abs(rot.y)
            let isStationary = angularVelX <= 0.01 && angularVelY <= 0.01
            let now = CACurrentMediaTime()

            if isStationary {
                if stationaryStartTime == nil {
                    stationaryStartTime = now
                    lockedCoords = (x: x, y: y)
                } else if let startTime = stationaryStartTime,
                          now - startTime >= 0.2 {
                    // Stationary for ≥ 200ms — use locked coordinates
                    if let locked = lockedCoords {
                        x = locked.x
                        y = locked.y
                    }
                }
            } else {
                stationaryStartTime = nil
                lockedCoords = nil
            }

            // --- Message suppression ---
            if abs(x - lastSentX) < 0.001 && abs(y - lastSentY) < 0.001 {
                // Suppress — no meaningful change
            } else {
                ws?.sendTiltPosition(x: x, y: y)
                lastSentX = x
                lastSentY = y
            }
        } else {
            // --- Legacy velocity-based mode ---
            let dz = snapshotTiltDeadZone
            let sensitivity = snapshotTiltSensitivity

            let g = data.gravity
            let rot = data.rotationRate

            let pitch = atan2(-g.x, -g.z)
            let cosPitch = cos(pitch)
            let sinPitch = sin(pitch)

            let worldYaw = rot.y * cosPitch + rot.z * sinPitch
            let worldPitch = rot.x

            var rx = worldPitch * sensitivity
            var ry = worldYaw * sensitivity

            if snapshotTiltInverted {
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
        let now = CACurrentMediaTime()

        // Fix: Use timestamp comparison instead of boolean flag.
        // This eliminates the race condition where a Task on the cooperative pool
        // would reset a boolean that the OperationQueue reads.
        if mag - prevAccelMag > tapThreshold && (now - lastTapFireTime) > tapCooldownDuration {
            ws?.sendTiltTap()
            lastTapFireTime = now
        }
        prevAccelMag = mag
    }
}
