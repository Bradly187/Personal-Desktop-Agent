import ARKit
import QuartzCore
import SwiftUI

/// Streams gaze direction deltas from ARKit TrueDepth camera.
///
/// Instead of projecting gaze onto a virtual screen plane (which breaks at steep
/// angles like iPad-on-desk + elevated monitors), this sends relative gaze movement
/// deltas. The PC side integrates these into cursor movement.
///
/// No dwell detection — dwell-click is removed in favor of other input modalities.
///
/// Uses SharedFaceSession — does NOT own its own ARSession.
@MainActor
final class GazeTracker: NSObject, ObservableObject {

    private let sharedFaceSession: SharedFaceSession
    private weak var ws: WebSocketManager?
    private var settings: SettingsStore?

    private static let consumerID = "GazeTracker"

    // Previous gaze direction for delta computation
    private var prevGazeDir: simd_float3?

    // Smoothing: exponential moving average on deltas
    private var smoothDx: Float = 0
    private var smoothDy: Float = 0

    /// Calibration mode: when set, raw deltas are sent to this handler instead of WebSocket.
    /// Used by GazeCalibrationSheet to measure eye movement range.
    var calibrationHandler: ((Float, Float) -> Void)?

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

        prevGazeDir = nil
        smoothDx = 0
        smoothDy = 0

        sharedFaceSession.addConsumer(Self.consumerID) { [weak self] anchor in
            Task { @MainActor [weak self] in
                self?.handleAnchorUpdate(anchor)
            }
        }
    }

    func stop() {
        sharedFaceSession.removeConsumer(Self.consumerID)
        prevGazeDir = nil
        smoothDx = 0
        smoothDy = 0
    }

    // MARK: — Gaze delta extraction

    private func handleAnchorUpdate(_ anchor: ARFaceAnchor) {
        guard let settings, settings.gazeEnabled else { return }

        let gazeDir = extractGazeDirection(from: anchor)
        guard let gazeDir else { return }

        guard let prev = prevGazeDir else {
            // First frame — just store, no delta to send
            prevGazeDir = gazeDir
            return
        }

        // Compute angular delta in the gaze direction
        let rawDx = gazeDir.x - prev.x
        let rawDy = gazeDir.y - prev.y

        prevGazeDir = gazeDir

        // If calibration is active, send raw deltas there instead of normal processing
        if let handler = calibrationHandler {
            handler(rawDx, rawDy)
            return
        }

        // Apply smoothing (EMA) to reduce jitter
        let alpha: Float = Float(settings.gazeStabilityThreshold)
        smoothDx = alpha * smoothDx + (1 - alpha) * rawDx
        smoothDy = alpha * smoothDy + (1 - alpha) * rawDy

        // Dead zone: suppress tiny movements (noise)
        let deadZone: Float = 0.002
        guard abs(smoothDx) > deadZone || abs(smoothDy) > deadZone else { return }

        // Scale to useful cursor movement range
        let sensitivity: Float = Float(settings.gazeSensitivity)
        let dx = Double(smoothDx * sensitivity)
        let dy = Double(smoothDy * sensitivity)

        ws?.sendGazeDelta(dx: dx, dy: dy)
    }

    /// Extract the average gaze direction vector from both eyes.
    /// Returns nil if eyes are closed or data is unreliable.
    private func extractGazeDirection(from anchor: ARFaceAnchor) -> simd_float3? {
        // Check eye openness — skip if blinking
        let blinkLeft = Float(truncating: anchor.blendShapes[.eyeBlinkLeft] ?? 0)
        let blinkRight = Float(truncating: anchor.blendShapes[.eyeBlinkRight] ?? 0)
        guard blinkLeft < 0.7 && blinkRight < 0.7 else { return nil }

        let leftEye = anchor.leftEyeTransform
        let rightEye = anchor.rightEyeTransform

        // Look vector: -Z column of each eye transform (forward direction)
        let leftFwd = simd_float3(-leftEye.columns.2.x, -leftEye.columns.2.y, -leftEye.columns.2.z)
        let rightFwd = simd_float3(-rightEye.columns.2.x, -rightEye.columns.2.y, -rightEye.columns.2.z)

        return normalize((leftFwd + rightFwd) / 2)
    }
}
