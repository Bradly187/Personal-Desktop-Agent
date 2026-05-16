import CoreMotion
import Foundation

/// Streams iPad device tilt at 60 Hz to the PC bridge.
/// Dead-zone filtering prevents jitter while the device is resting.
/// Accelerometer impulse detection fires tilt_tap on a sharp table-tap.
@MainActor
final class TiltSensor {

    private let motion = CMMotionManager()
    private weak var ws: WebSocketManager?
    private var settings: SettingsStore?

    // Impulse detection state
    private var prevAccelMag: Double = 0
    private let tapThreshold: Double = 2.5   // g-force delta that counts as a tap
    private var tapCooldown: Bool = false
    private let tapCooldownDuration: Double = 0.25

    init(ws: WebSocketManager, settings: SettingsStore) {
        self.ws = ws
        self.settings = settings
    }

    // MARK: — Lifecycle

    func start() {
        guard motion.isDeviceMotionAvailable else {
            print("TiltSensor: DeviceMotion unavailable")
            return
        }
        motion.deviceMotionUpdateInterval = 1.0 / 60.0
        motion.startDeviceMotionUpdates(to: .main) { [weak self] data, _ in
            guard let self, let data else { return }
            self.handle(data)
        }
    }

    func stop() {
        motion.stopDeviceMotionUpdates()
    }

    // MARK: — Processing

    private func handle(_ data: CMDeviceMotion) {
        guard let settings else { return }
        guard settings.tiltEnabled else { return }

        let dz = settings.tiltDeadZone
        let sensitivity = settings.tiltSensitivity

        // --- Gravity-compensated tilt projection ---
        // The user holds the iPad at an angle (e.g. 30° off horizontal).
        // Raw rotationRate is in the device's body frame, so "tilt right"
        // bleeds into multiple axes depending on holding angle.
        //
        // Solution: use the gravity vector to find the device's pitch angle,
        // then project rotationRate into a ground-aligned frame where:
        //   horizontal (cursor left/right) = rotation around the gravity axis
        //   vertical (cursor up/down) = rotation perpendicular to gravity in the sagittal plane

        let g = data.gravity  // unit vector pointing "down" in device frame
        let rot = data.rotationRate

        // Device pitch angle: angle between device Z-axis and gravity
        // When flat: g.z ≈ -1, pitch ≈ 0
        // When held at 30°: g.x ≈ -0.5, g.z ≈ -0.87
        let pitch = atan2(-g.x, -g.z)  // radians the device is tilted back
        let cosPitch = cos(pitch)
        let sinPitch = sin(pitch)

        // Project rotation rate into ground-aligned frame:
        // Horizontal cursor (yaw in world frame) = rot.y * cos(pitch) + rot.z * sin(pitch)
        // Vertical cursor (pitch in world frame) = rot.x
        // This ensures "tilt right" maps cleanly to horizontal regardless of holding angle.
        let worldYaw = rot.y * cosPitch + rot.z * sinPitch
        let worldPitch = rot.x

        var rx = worldPitch * sensitivity
        var ry = worldYaw * sensitivity

        // Optional inversion for users who prefer opposite mapping
        if settings.tiltInverted {
            rx = -rx
            ry = -ry
        }

        if abs(rx) > dz || abs(ry) > dz {
            ws?.sendTilt(rx: rx, ry: ry)
        }

        // Impulse tap detection from accelerometer
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
