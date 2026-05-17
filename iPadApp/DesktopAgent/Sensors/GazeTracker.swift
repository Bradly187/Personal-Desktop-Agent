import ARKit
import QuartzCore
import SwiftUI

/// Streams gaze position from ARKit TrueDepth camera at the ARKit frame rate.
/// On-device dwell timer fires gaze_dwell when gaze is stable for `dwellTimeout` seconds.
/// Requires: front-facing TrueDepth camera (iPad Pro 2020+, iPad mini 6+, iPad Air 5+).
///
/// Uses SharedFaceSession — does NOT own its own ARSession. GazeTracker and HeadTracker
/// share one face-tracking session to avoid the ARKit one-session-per-camera limitation.
@MainActor
final class GazeTracker: NSObject, ObservableObject {

    private let sharedFaceSession: SharedFaceSession
    private weak var ws: WebSocketManager?
    private var settings: SettingsStore?

    private static let consumerID = "GazeTracker"

    // Dwell state
    private var dwellStart: CFTimeInterval?
    private var lastStablePoint: CGPoint?
    private let stabilityThreshold: CGFloat = 0.04   // fraction of screen diagonal

    // Ring animation progress (0…1)
    @Published var dwellProgress: Double = 0

    init(ws: WebSocketManager, settings: SettingsStore, sharedFaceSession: SharedFaceSession) {
        self.ws = ws
        self.settings = settings
        self.sharedFaceSession = sharedFaceSession
        super.init()
    }

    // MARK: — Lifecycle

    func start() {
        guard SharedFaceSession.isSupported else {
            print("GazeTracker: ARFaceTracking not supported on this device")
            return
        }
        guard let settings, settings.gazeEnabled else { return }

        sharedFaceSession.addConsumer(Self.consumerID) { [weak self] anchor in
            self?.handleAnchorUpdate(anchor)
        }
    }

    func stop() {
        sharedFaceSession.removeConsumer(Self.consumerID)
        dwellProgress = 0
    }

    // MARK: — Gaze extraction

    private func handleAnchorUpdate(_ anchor: ARFaceAnchor) {
        if let (point, conf) = extractGaze(from: anchor) {
            handleGaze(point, conf: conf)
        }
    }

    private func extractGaze(from anchor: ARFaceAnchor) -> (CGPoint, Float)? {
        // ARKit provides leftEyeTransform and rightEyeTransform relative to face.
        // We project the gaze direction onto the camera plane to get screen-space coords.
        let leftEye = anchor.leftEyeTransform
        let rightEye = anchor.rightEyeTransform

        // Average eye position in world space
        let eyePos = simd_float3(
            (leftEye.columns.3.x + rightEye.columns.3.x) / 2,
            (leftEye.columns.3.y + rightEye.columns.3.y) / 2,
            (leftEye.columns.3.z + rightEye.columns.3.z) / 2
        )

        // Look vector: -Z column of the average eye transform
        let leftFwd = simd_float3(-leftEye.columns.2.x, -leftEye.columns.2.y, -leftEye.columns.2.z)
        let rightFwd = simd_float3(-rightEye.columns.2.x, -rightEye.columns.2.y, -rightEye.columns.2.z)
        let gazeDir = normalize((leftFwd + rightFwd) / 2)

        // Project to a virtual screen plane 0.5 m in front of the face
        guard gazeDir.z < -0.01 else { return nil }
        let t: Float = 0.5 / (-gazeDir.z)
        let hitX = eyePos.x + gazeDir.x * t
        let hitY = eyePos.y + gazeDir.y * t

        // Normalize to [0,1]; clamp
        let nx = CGFloat(min(max((hitX + 0.3) / 0.6, 0), 1))
        let ny = CGFloat(min(max(1 - (hitY + 0.3) / 0.6, 0), 1))

        let conf = Float(truncating: anchor.blendShapes[.eyeBlinkLeft] ?? 0)
        let openness = 1 - conf   // higher when eye is open

        return (CGPoint(x: nx, y: ny), openness)
    }

    private func handleGaze(_ point: CGPoint, conf: Float) {
        guard let settings else { return }
        ws?.sendGaze(x: point.x, y: point.y, confidence: Double(conf))

        // Suppress dwell timer when all dwell actions are disabled (gaze stream continues)
        guard !settings.allDwellActionsDisabled else {
            dwellStart = nil
            dwellProgress = 0
            return
        }

        // Dwell detection
        let threshold = CGFloat(stabilityThreshold)
        if let last = lastStablePoint,
           abs(point.x - last.x) < threshold && abs(point.y - last.y) < threshold {
            // Gaze is stable
            let now = CACurrentMediaTime()
            if dwellStart == nil { dwellStart = now }
            let elapsed = now - dwellStart!
            let progress = min(elapsed / settings.dwellTimeout, 1.0)
            dwellProgress = progress
            if elapsed >= settings.dwellTimeout {
                let firedAction = settings.activeDwellAction
                ws?.sendGazeDwell(x: point.x, y: point.y, actionType: firedAction)

                // Apply state transitions: drag auto-advance (dragStart→dragEnd, dragEnd→leftClick)
                settings.applyDwellTransition(firedAction: firedAction)
                // Apply one-shot reset: if enabled and action was rightClick/doubleClick, revert to leftClick
                settings.applyOneShotResetIfNeeded()

                dwellStart = nil
                dwellProgress = 0
            }
        } else {
            lastStablePoint = point
            dwellStart = nil
            dwellProgress = 0
        }
    }
}

// MARK: — Dwell ring overlay

struct DwellRingView: View {
    @ObservedObject var tracker: GazeTracker

    var body: some View {
        if tracker.dwellProgress > 0 {
            Circle()
                .trim(from: 0, to: tracker.dwellProgress)
                .stroke(Color.accentColor, style: StrokeStyle(lineWidth: 4, lineCap: .round))
                .frame(width: 44, height: 44)
                .rotationEffect(.degrees(-90))
                .animation(.linear(duration: 0.1), value: tracker.dwellProgress)
        }
    }
}
