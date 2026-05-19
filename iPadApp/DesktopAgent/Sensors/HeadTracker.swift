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
///
/// Fix: Signal processing runs on a dedicated serial queue (not MainActor) to eliminate
/// the ~8-16ms latency from Task { @MainActor } hops.
@MainActor
final class HeadTracker: NSObject {

    private let sharedFaceSession: SharedFaceSession
    private weak var ws: WebSocketManager?
    private var settings: SettingsStore?

    private static let consumerID = "HeadTracker"

    /// Serial processing queue — all head math runs here, off MainActor.
    private let processQueue = DispatchQueue(label: "head.tracker.process", qos: .userInteractive)

    // All mutable processing state accessed ONLY from processQueue.
    nonisolated(unsafe) private var prevPitch: Float = 0
    nonisolated(unsafe) private var prevYaw: Float = 0
    nonisolated(unsafe) private var isFirstFrame = true
    nonisolated(unsafe) private var filterPitch: OneEuroFilter
    nonisolated(unsafe) private var filterYaw: OneEuroFilter

    /// Snapshotted setting — captured at start() to avoid cross-isolation reads.
    nonisolated(unsafe) private var headEnabled: Bool = false

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

        // Snapshot settings for processQueue
        headEnabled = settings.headEnabled

        isFirstFrame = true
        filterPitch.reset()
        filterYaw.reset()

        sharedFaceSession.addConsumer(Self.consumerID) { [weak self] anchor in
            guard let self else { return }
            // Dispatch to dedicated serial queue — no MainActor hop
            self.processQueue.async { [weak self] in
                self?.handleAnchor(anchor)
            }
        }
    }

    func stop() {
        sharedFaceSession.removeConsumer(Self.consumerID)
        processQueue.async { [weak self] in
            self?.isFirstFrame = true
            self?.filterPitch.reset()
            self?.filterYaw.reset()
        }
    }

    /// Update snapshotted settings when user changes them. Call from MainActor.
    func updateSettings() {
        guard let settings else { return }
        let enabled = settings.headEnabled
        processQueue.async { [weak self] in
            self?.headEnabled = enabled
        }
    }

    // MARK: — Euler angle extraction (runs on processQueue)

    private func handleAnchor(_ anchor: ARFaceAnchor) {
        guard headEnabled else { return }

        let m = anchor.transform
        let pitch = asin(-m.columns.2.y)
        let yaw   = atan2(m.columns.2.x, m.columns.2.z)

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
        let now = CACurrentMediaTime()
        let filteredDPitch = filterPitch.filter(Double(rawDPitch), timestamp: now)
        let filteredDYaw = filterYaw.filter(Double(rawDYaw), timestamp: now)

        // Convert to degrees for the PC side
        let toDeg = 180.0 / Double.pi
        let pitchDeg = filteredDPitch * toDeg
        let yawDeg = filteredDYaw * toDeg

        // WebSocketManager.send() dispatches to its own serial sendQueue — safe from here
        ws?.sendHeadPose(pitch: pitchDeg, yaw: yawDeg)
    }
}
