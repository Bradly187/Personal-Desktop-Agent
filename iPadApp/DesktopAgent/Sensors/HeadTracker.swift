import ARKit
import Foundation

/// Streams head pitch/yaw deltas from ARKit at the ARKit frame rate.
/// Sends delta angles (not absolute) so the PC can integrate them into cursor movement.
///
/// Uses 1-Euro adaptive filter (replaces fixed EMA smoothing factor) for
/// jitter reduction at rest and minimal latency during fast head movements.
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

    // 1-Euro adaptive filters (per-axis) — replace fixed EMA smoothing
    private var filterPitch: OneEuroFilter
    private var filterYaw: OneEuroFilter

    init(ws: WebSocketManager, settings: SettingsStore, sharedFaceSession: SharedFaceSession) {
        self.ws = ws
        self.settings = settings
        self.sharedFaceSession = sharedFaceSession

        // Initialize 1-Euro filters with head-appropriate parameters
        filterPitch = OneEuroFilter(
            minCutoff: settings.headFilterMinCutoff,
            beta: settings.headFilterBeta,
            dCutoff: settings.headFilterDCutoff
        )
        filterYaw = OneEuroFilter(
            minCutoff: settings.headFilterMinCutoff,
            beta: settings.headFilterBeta,
            dCutoff: settings.headFilterDCutoff
        )

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
        // Reset filters on start for fresh state (no stale values from previous session)
        filterPitch.reset()
        filterYaw.reset()

        sharedFaceSession.addConsumer(Self.consumerID) { [weak self] anchor in
            Task { @MainActor [weak self] in
                self?.handleAnchor(anchor)
            }
        }
    }

    func stop() {
        sharedFaceSession.removeConsumer(Self.consumerID)
        isFirstFrame = true
        filterPitch.reset()
        filterYaw.reset()
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

        // Compute raw deltas (radians)
        let rawDPitch = pitch - prevPitch
        let rawDYaw = yaw - prevYaw

        prevPitch = pitch
        prevYaw = yaw

        // Apply 1-Euro adaptive filter (per-axis)
        // The filter adapts: strong smoothing when head is still (jitter reduction),
        // minimal lag when head moves fast (responsive tracking).
        let now = CACurrentMediaTime()
        let filteredDPitch = filterPitch.filter(Double(rawDPitch), timestamp: now)
        let filteredDYaw = filterYaw.filter(Double(rawDYaw), timestamp: now)

        // Convert to degrees for the PC side
        let toDeg = 180.0 / Double.pi
        let pitchDeg = filteredDPitch * toDeg
        let yawDeg = filteredDYaw * toDeg

        // Axis convention: positive pitch (chin to chest) → positive dy → cursor down
        // This is sent as-is; the PC-side FusionEngine applies: dy = pitch * sensitivity
        ws?.sendHeadPose(pitch: pitchDeg, yaw: yawDeg)
    }
}
