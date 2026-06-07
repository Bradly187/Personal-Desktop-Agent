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

    /// Tilt-joystick integrator (rate-control mode): the accumulated virtual
    /// cursor position and the last frame time used to integrate velocity →
    /// position. Seeded from lastSentX/Y on the first frame so switching modes
    /// mid-session doesn't jump the cursor.
    private var joyPosX: Double = 0.5
    private var joyPosY: Double = 0.5
    private var lastJoyFrameTime: CFTimeInterval = 0

    /// Monotonic timestamp when the device first became stationary (angular velocity ≤ 0.01 rad/s)
    var stationaryStartTime: CFTimeInterval?

    /// Frozen output coordinates during stationary lock
    var lockedCoords: (x: Double, y: Double)?

    // MARK: — Dwell-to-click state

    /// True while a dwell countdown is running (drives the progress ring).
    /// Published so the HUD can animate; toggled only on start/cancel/fire,
    /// not per-frame, to avoid 60 Hz UI churn.
    @Published var dwellInProgress: Bool = false

    /// Whether a dwell click has already fired for the current stationary
    /// period (fire-once until the device moves again).
    private var dwellFired: Bool = false
    /// Background-side mirror of dwellInProgress so we only hop to the main
    /// actor when the state actually changes.
    private var dwellActiveLocal: Bool = false
    private let dwellHaptic = UIImpactFeedbackGenerator(style: .medium)

    // Impulse detection state
    private var prevAccelMag: Double = 0
    // g-force delta that counts as a tap. User-tunable via the Settings "Tap
    // Sensitivity" slider (SettingsStore.tapThreshold, default 0.4). Lower = a
    // lighter tap registers. Snapshotted from MainActor so the motion queue
    // reads it without a cross-isolation access. Each accepted tap sends one
    // tilt_tap → one left click; two quick taps become an OS double-click.
    private var snapshotTapThreshold: Double = 1.2
    /// Monotonic timestamp of last tap fire — replaces boolean tapCooldown to eliminate race condition.
    private var lastTapFireTime: CFTimeInterval = 0
    // Lowered 0.25 → 0.18 so a deliberate double-tap (two taps ~0.18–0.5 s apart)
    // both register and land inside the OS double-click window, while still
    // suppressing a single tap's acceleration ring from double-firing.
    private let tapCooldownDuration: Double = 0.18
    /// Cursor-motion freeze window (motion stabilization): while now < this, the
    /// position paths hold so a tap's follow-through wobble can't drag the cursor
    /// off the target. Opened by the tap-detection block; length = tapStabilizeMs.
    private var tapFreezeUntil: CFTimeInterval = 0

    // Diagnostic counters — logged once per second by `handle()` so we can verify
    // from the PC's ipad_logs table whether the motion handler is alive, what
    // its current snapshot says, and how many sends it actually performed.
    private var diagLastReportTime: CFTimeInterval = 0
    private var diagHandlerCalls: Int = 0
    private var diagSendsAttempted: Int = 0
    private var diagSendsSuppressed: Int = 0
    private var diagWsNilCount: Int = 0
    /// Max instantaneous angular velocity seen in the diag window (rad/s).
    /// Tells us if the user is actually moving the iPad or if it's stationary.
    private var diagMaxAngVel: Double = 0
    /// Tap impulses that crossed the threshold and fired a tilt_tap this window.
    private var diagTapsFired: Int = 0
    /// Largest accel-magnitude delta seen this window (g). Compared against
    /// snapshotTapThreshold, it tells us whether real taps are landing close to
    /// the bar — if peakAccelDelta hovers just under the threshold with
    /// tapsFired=0, the tap is being detected but rejected as too light.
    private var diagPeakAccelDelta: Double = 0

    // MARK: — Snapshotted settings (avoids cross-isolation reads at 60Hz)
    private var snapshotTiltEnabled: Bool = true
    private var snapshotTiltPositionMode: Bool = true
    private var snapshotTiltDeadZone: Double = 1.5
    private var snapshotTiltSensitivity: Double = 1.0
    private var snapshotTiltRange: Double = 25.0
    private var snapshotTiltInverted: Bool = false
    private var snapshotTiltDwellEnabled: Bool = false
    private var snapshotTiltDwellDuration: Double = 1.0
    private var snapshotTiltJoystickMode: Bool = true
    private var snapshotTiltDriftMaxSpeed: Double = 1.2
    private var snapshotTiltDriftSaturationDeg: Double = 30.0
    private var snapshotTapStabilizeMs: Double = 180.0

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
            AppLogger.shared.warning("TiltSensor", "DeviceMotion unavailable — CMMotionManager.isDeviceMotionAvailable == false")
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
            snapshotTiltDwellEnabled = s.tiltDwellClickEnabled
            snapshotTiltDwellDuration = s.tiltDwellDuration
            snapshotTapThreshold = s.tapThreshold
            snapshotTiltJoystickMode = s.tiltJoystickMode
            snapshotTiltDriftMaxSpeed = s.tiltDriftMaxSpeed
            snapshotTiltDriftSaturationDeg = s.tiltDriftSaturationDeg
            snapshotTapStabilizeMs = s.tapStabilizeMs
        }
        lastJoyFrameTime = 0

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
        AppLogger.shared.info("TiltSensor",
            "start: snapshotTiltEnabled=\(snapshotTiltEnabled) positionMode=\(snapshotTiltPositionMode) range=\(snapshotTiltRange) deadZone=\(snapshotTiltDeadZone) inverted=\(snapshotTiltInverted) wsAttached=\(ws != nil)")
    }

    func stop() {
        guard isRunning else { return }
        motion.stopDeviceMotionUpdates()
        isRunning = false
        lastSentX = 0.5
        lastSentY = 0.5
        joyPosX = 0.5
        joyPosY = 0.5
        lastJoyFrameTime = 0
        tapFreezeUntil = 0
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
        snapshotTiltDwellEnabled = s.tiltDwellClickEnabled
        snapshotTiltDwellDuration = s.tiltDwellDuration
        snapshotTapThreshold = s.tapThreshold
        snapshotTiltJoystickMode = s.tiltJoystickMode
        snapshotTiltDriftMaxSpeed = s.tiltDriftMaxSpeed
        snapshotTiltDriftSaturationDeg = s.tiltDriftSaturationDeg
        snapshotTapStabilizeMs = s.tapStabilizeMs
    }

    // MARK: — Dwell-to-click

    /// Advance the dwell countdown while the device is held still (position
    /// mode). Fires the active dwell action once per stationary period when the
    /// hold exceeds the configured duration. Called from the motion queue;
    /// published/UI/haptic side effects hop to the main actor.
    private func updateDwell(now: CFTimeInterval) {
        guard snapshotTiltDwellEnabled, !dwellFired,
              let startTime = stationaryStartTime else { return }

        // First still frame → start the ring countdown.
        if !dwellActiveLocal {
            dwellActiveLocal = true
            DispatchQueue.main.async { [weak self] in self?.dwellInProgress = true }
        }

        if now - startTime >= snapshotTiltDwellDuration {
            dwellFired = true
            dwellActiveLocal = false
            ws?.sendDwellClick()
            DispatchQueue.main.async { [weak self] in
                self?.dwellInProgress = false
                self?.dwellHaptic.impactOccurred()
            }
        }
    }

    /// Reset dwell state when the device moves again (or dwell is disabled).
    private func cancelDwell() {
        dwellFired = false
        if dwellActiveLocal {
            dwellActiveLocal = false
            DispatchQueue.main.async { [weak self] in self?.dwellInProgress = false }
        }
    }

    // MARK: — Tilt-joystick (rate-control)

    /// Rate-control tilt: the tilt *angle* from the calibrated neutral sets the
    /// cursor *velocity* (neutral = hold, more tilt = faster, saturating at
    /// `snapshotTiltDriftSaturationDeg`). The velocity is integrated into a
    /// virtual position each frame and sent over the existing tilt_position
    /// path, so the cursor keeps gliding while the iPad is held tilted and stops
    /// when returned to neutral — and the PC side (gravity, multi-monitor,
    /// edge-clamp) is unchanged. Runs on the motion queue.
    private func processJoystick(data: CMDeviceMotion, frozen: Bool) {
        let now = CACurrentMediaTime()

        // Tap freeze: hold position — don't integrate or send. Keep the frame
        // clock current so dt doesn't spike when the freeze ends, and reset the
        // dwell timer (a tap isn't a dwell).
        if frozen {
            lastJoyFrameTime = now
            stationaryStartTime = nil
            cancelDwell()
            diagSendsSuppressed += 1
            return
        }

        // First frame after (re)start / mode switch: seed from the last sent
        // position so the cursor doesn't jump, and use a nominal dt.
        if lastJoyFrameTime == 0 {
            joyPosX = lastSentX
            joyPosY = lastSentY
        }
        var dt = now - lastJoyFrameTime
        if lastJoyFrameTime == 0 || dt <= 0 || dt > 0.1 { dt = 1.0 / 60.0 }
        lastJoyFrameTime = now

        // Signed tilt offsets from neutral (degrees) — same basis computePosition
        // uses, so the dead zone and "neutral = center" calibration carry over.
        let g = SIMD3<Double>(data.gravity.x, data.gravity.y, data.gravity.z)
        let neutralPitch = atan2(-neutralGravity.x, -neutralGravity.z)
        let currentPitch = atan2(-g.x, -g.z)
        let neutralRoll = atan2(-neutralGravity.y, -neutralGravity.z)
        let currentRoll = atan2(-g.y, -g.z)
        let deltaRollDeg = (currentRoll - neutralRoll) * (180.0 / .pi)
        let deltaPitchDeg = (currentPitch - neutralPitch) * (180.0 / .pi)

        // Per-axis velocity (screen-fraction/sec). Tilt up (pitch+) → move up (y-).
        var vx = joystickAxisVelocity(deltaRollDeg)
        var vy = -joystickAxisVelocity(deltaPitchDeg)
        if snapshotTiltInverted { vx = -vx; vy = -vy }

        // Integrate → virtual position, clamped to the screen.
        joyPosX = max(0.0, min(1.0, joyPosX + vx * dt))
        joyPosY = max(0.0, min(1.0, joyPosY + vy * dt))

        let isMoving = (vx != 0.0 || vy != 0.0)

        // Dwell-to-click: in joystick mode the cursor is "still" when the tilt is
        // inside the dead zone (zero velocity), not when the device stops
        // rotating. Drive the dwell timer off that.
        if isMoving {
            stationaryStartTime = nil
            cancelDwell()
        } else {
            if stationaryStartTime == nil { stationaryStartTime = now }
            updateDwell(now: now)
        }

        // Send only on an actual position change (suppress when held still / at
        // an edge), matching the absolute path's suppression contract.
        if abs(joyPosX - lastSentX) < 0.0005 && abs(joyPosY - lastSentY) < 0.0005 {
            diagSendsSuppressed += 1
        } else if let wsRef = ws {
            wsRef.sendTiltPosition(x: joyPosX, y: joyPosY)
            diagSendsAttempted += 1
            lastSentX = joyPosX
            lastSentY = joyPosY
        } else {
            diagWsNilCount += 1
        }
    }

    /// Maps a signed tilt offset (deg from neutral) to a signed cursor velocity
    /// (screen-fraction/sec). Zero inside the dead zone (no neutral drift), then
    /// linear up to `snapshotTiltDriftMaxSpeed` at the saturation angle.
    private func joystickAxisVelocity(_ deg: Double) -> Double {
        let dead = snapshotTiltDeadZone
        let mag = abs(deg)
        if mag <= dead { return 0.0 }
        let sat = max(dead + 1.0, snapshotTiltDriftSaturationDeg)
        let frac = min(1.0, (mag - dead) / (sat - dead))
        let speed = frac * snapshotTiltDriftMaxSpeed
        return deg > 0 ? speed : -speed
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
        diagHandlerCalls += 1
        _emitDiagIfDue()

        guard snapshotTiltEnabled else { return }

        // Cursor-motion freeze: while a recent tap's stabilization window is open,
        // the position paths hold (neither integrate nor send) so the tap's
        // follow-through wobble can't drag the cursor off-target. The window is
        // opened by the tap-detection block below.
        let frozen = CACurrentMediaTime() < tapFreezeUntil

        if snapshotTiltPositionMode && snapshotTiltJoystickMode {
            // --- Tilt-joystick (rate-control) mode: tilt angle → cursor velocity ---
            processJoystick(data: data, frozen: frozen)
        } else if snapshotTiltPositionMode && frozen {
            // Tap freeze: hold the cursor (no send → the PC keeps the last position).
            diagSendsSuppressed += 1
        } else if snapshotTiltPositionMode {
            // --- Position-mapped (absolute) mode ---
            var (x, y) = computePosition(gravity: data.gravity)

            // --- Stationary lock ---
            // Uses CACurrentMediaTime() instead of Date() for cheaper monotonic timing.
            //
            // Threshold was 0.01 rad/s (~0.57°/s) which is below the noise floor
            // of normal hand-tilt motion — the lock would engage during active
            // tilting and freeze the cursor at the initial neutral position
            // forever (suppression filter then ate every send). Raised to
            // 0.05 rad/s (~2.9°/s) so the lock only fires when the iPad is
            // genuinely resting still on a surface, while still letting normal
            // tilt motion register. Use OR not AND so that motion on either
            // axis alone defeats the lock.
            let rot = data.rotationRate
            let angularVelX = abs(rot.x)
            let angularVelY = abs(rot.y)
            let stationaryThreshold = 0.05  // rad/s, ~2.9°/s
            let isStationary = angularVelX <= stationaryThreshold
                            && angularVelY <= stationaryThreshold
            let now = CACurrentMediaTime()

            // Track peak rotation for the diagnostic line so we can see whether
            // the user is genuinely tilting or the iPad is sitting still.
            let peakAngVel = max(angularVelX, angularVelY)
            if peakAngVel > diagMaxAngVel { diagMaxAngVel = peakAngVel }

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
                updateDwell(now: now)
            } else {
                stationaryStartTime = nil
                lockedCoords = nil
                cancelDwell()
            }

            // --- Message suppression ---
            if abs(x - lastSentX) < 0.001 && abs(y - lastSentY) < 0.001 {
                // Suppress — no meaningful change
                diagSendsSuppressed += 1
            } else {
                if let wsRef = ws {
                    wsRef.sendTiltPosition(x: x, y: y)
                    diagSendsAttempted += 1
                } else {
                    diagWsNilCount += 1
                }
                lastSentX = x
                lastSentY = y
            }
        } else {
            // --- Legacy velocity-based mode ---
            // No on-device dead zone. The PC owns gating (FusionConfig
            // dead_zone_ramp) AND the GyroBiasCalibrator needs the low-velocity
            // "stationary" samples (< 0.02 rad/s) to estimate drift — an
            // on-device dead zone filters out exactly those samples, so the
            // calibrator could never leave UNCALIBRATED.
            //
            // The previous gate `abs(rx) > tiltDeadZone` reused tiltDeadZone
            // (1.5, a *degree* value used correctly in position mode) as a
            // rad/s threshold ≈ 86°/s: it made the cursor nearly immovable and
            // starved the calibrator. Send every frame and let the PC gate.
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

            ws?.sendTilt(rx: rx, ry: ry)
        }

        // --- Impulse tap detection (runs regardless of mode) ---
        let a = data.userAcceleration
        let mag = sqrt(a.x * a.x + a.y * a.y + a.z * a.z)
        let now = CACurrentMediaTime()

        // Fix: Use timestamp comparison instead of boolean flag.
        // This eliminates the race condition where a Task on the cooperative pool
        // would reset a boolean that the OperationQueue reads.
        let accelDelta = mag - prevAccelMag
        if accelDelta > diagPeakAccelDelta { diagPeakAccelDelta = accelDelta }
        if accelDelta > snapshotTapThreshold && (now - lastTapFireTime) > tapCooldownDuration {
            ws?.sendTiltTap()
            lastTapFireTime = now
            diagTapsFired += 1
            // Open the cursor-freeze window so the tap's follow-through wobble
            // can't drag the cursor off the target before the click resolves.
            if snapshotTapStabilizeMs > 0 {
                tapFreezeUntil = now + snapshotTapStabilizeMs / 1000.0
            }
        }
        prevAccelMag = mag
    }

    /// Emits a structured log line once per second so the PC's ipad_logs table
    /// reflects whether the handler is alive, what its snapshot says, and how
    /// many sends actually completed. Cheap when called at 60 Hz: a single
    /// CACurrentMediaTime() read plus a comparison.
    private func _emitDiagIfDue() {
        let now = CACurrentMediaTime()
        if diagLastReportTime == 0 {
            diagLastReportTime = now
            return
        }
        guard now - diagLastReportTime >= 1.0 else { return }
        let summary = String(
            format: "handler=%d/s sends=%d suppressed=%d wsNil=%d enabled=%@ posMode=%@ locked=%@ peakAngVel=%.3f rad/s tapsFired=%d peakAccelDelta=%.2fg tapThresh=%.2fg",
            diagHandlerCalls, diagSendsAttempted, diagSendsSuppressed, diagWsNilCount,
            snapshotTiltEnabled.description, snapshotTiltPositionMode.description,
            (lockedCoords != nil).description, diagMaxAngVel,
            diagTapsFired, diagPeakAccelDelta, snapshotTapThreshold
        )
        AppLogger.shared.info("TiltSensor", summary)
        diagLastReportTime = now
        diagHandlerCalls = 0
        diagSendsAttempted = 0
        diagSendsSuppressed = 0
        diagWsNilCount = 0
        diagMaxAngVel = 0
        diagTapsFired = 0
        diagPeakAccelDelta = 0
    }
}
