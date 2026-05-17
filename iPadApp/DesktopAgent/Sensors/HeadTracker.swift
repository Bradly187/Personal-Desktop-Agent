import ARKit
import Foundation

/// Streams head pitch/yaw deltas from ARKit at the ARKit frame rate.
/// Sends delta angles (not absolute) so the PC can integrate them into cursor movement.
/// Smoothing factor configurable via SettingsStore.
///
/// Uses SharedFaceSession — does NOT own its own ARSession. GazeTracker and HeadTracker
/// share one face-tracking session to avoid the ARKit one-session-per-camera limitation.
@MainActor
final class HeadTracker: NSObject {

    private let sharedFaceSession: SharedFaceSession
    private weak var ws: WebSocketManager?
    private var settings: SettingsStore?

    private static let consumerID = "HeadTracker"

    private var prevPitch: Float = 0
    private var prevYaw: Float = 0
    private var isFirstFrame = true

    init(ws: WebSocketManager, settings: SettingsStore, sharedFaceSession: SharedFaceSession) {
        self.ws = ws
        self.settings = settings
        self.sharedFaceSession = sharedFaceSession
        super.init()
    }

    // MARK: — Lifecycle

    func start() {
        guard SharedFaceSession.isSupported else {
            print("HeadTracker: ARFaceTracking not supported")
            return
        }
        guard let settings, settings.headEnabled else { return }

        isFirstFrame = true
        sharedFaceSession.addConsumer(Self.consumerID) { [weak self] anchor in
            self?.handleAnchor(anchor)
        }
    }

    func stop() {
        sharedFaceSession.removeConsumer(Self.consumerID)
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
