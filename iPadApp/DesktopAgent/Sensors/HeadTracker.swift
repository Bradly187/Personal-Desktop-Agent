import ARKit
import Foundation

/// Streams head pitch/yaw deltas from ARKit at the ARKit frame rate.
/// Sends delta angles (not absolute) so the PC can integrate them into cursor movement.
/// Smoothing factor configurable via SettingsStore.
@MainActor
final class HeadTracker: NSObject {

    private let session = ARSession()
    private weak var ws: WebSocketManager?
    private var settings: SettingsStore?

    private var prevPitch: Float = 0
    private var prevYaw: Float = 0
    private var isFirstFrame = true

    init(ws: WebSocketManager, settings: SettingsStore) {
        self.ws = ws
        self.settings = settings
        super.init()
    }

    // MARK: — Lifecycle

    func start() {
        guard ARFaceTrackingConfiguration.isSupported else {
            print("HeadTracker: ARFaceTracking not supported")
            return
        }
        guard let settings, settings.headEnabled else { return }

        let config = ARFaceTrackingConfiguration()
        config.isLightEstimationEnabled = false
        session.delegate = self
        session.run(config)
    }

    func stop() {
        session.pause()
        isFirstFrame = true
    }

    // MARK: — Euler angle extraction

    private func handleAnchor(_ anchor: ARFaceAnchor) {
        guard let settings, settings.headEnabled else { return }

        // Extract Euler angles from the face anchor's transform
        let m = anchor.transform
        let pitch = asin(-m.columns.2.y)           // rotation around X
        let yaw   = atan2(m.columns.2.x, m.columns.2.z)  // rotation around Y

        if isFirstFrame {
            prevPitch = pitch
            prevYaw = yaw
            isFirstFrame = false
            return
        }

        let α = Float(settings.headSmoothingFactor)
        let dPitch = α * (pitch - prevPitch)
        let dYaw   = α * (yaw   - prevYaw)

        prevPitch = pitch
        prevYaw   = yaw

        let toDeg: Float = 180 / .pi
        ws?.sendHeadPose(pitch: Double(dPitch * toDeg), yaw: Double(dYaw * toDeg))
    }
}

// MARK: — ARSessionDelegate

extension HeadTracker: ARSessionDelegate {
    nonisolated func session(_ session: ARSession, didUpdate anchors: [ARAnchor]) {
        guard let face = anchors.first(where: { $0 is ARFaceAnchor }) as? ARFaceAnchor else {
            return
        }
        DispatchQueue.main.async { [weak self] in
            self?.handleAnchor(face)
        }
    }
}
