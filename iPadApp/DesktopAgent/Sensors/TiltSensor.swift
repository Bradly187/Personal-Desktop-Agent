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
            Task { @MainActor in self.handle(data) }
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

        // Rotation rate in rad/s → scaled relative motion
        let rx = data.rotationRate.x * sensitivity
        let ry = data.rotationRate.y * sensitivity

        if abs(rx) > dz || abs(ry) > dz {
            ws?.sendTilt(rx: rx, ry: ry)
        }

        // Impulse tap detection from accelerometer
        let a = data.userAcceleration
        let mag = sqrt(a.x * a.x + a.y * a.y + a.z * a.z)
        if mag - prevAccelMag > tapThreshold && !tapCooldown {
            ws?.sendTiltTap()
            tapCooldown = true
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
                self?.tapCooldown = false
            }
        }
        prevAccelMag = mag
    }
}
