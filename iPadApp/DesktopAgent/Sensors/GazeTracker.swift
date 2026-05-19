import ARKit
import QuartzCore
import SwiftUI

/// Streams gaze direction deltas from ARKit TrueDepth camera.
///
/// Signal processing chain:
/// 1. Extract average eye forward vector from ARKit face anchor
/// 2. Compute angular delta from previous frame
/// 3. Axis correction (flip Y for natural mapping: look down → cursor down)
/// 4. 1-Euro adaptive filter (replaces EMA — jitter reduction at rest, minimal lag during saccades)
/// 5. Saccade detection (suppress output during rapid eye transitions)
/// 6. Confidence weighting (scale output by tracking confidence)
/// 7. Send over WebSocket with confidence and saccade metadata
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

    // 1-Euro adaptive filters (per-axis) — replace EMA smoothing
    private var filterDx: OneEuroFilter
    private var filterDy: OneEuroFilter

    // Saccade detection state machine
    private enum SaccadeState {
        case tracking       // Normal output
        case saccade        // Output suppressed (velocity > enter threshold)
        case rampIn(start: CFTimeInterval)  // Ramping from 0 to full over 50ms
    }
    private var saccadeState: SaccadeState = .tracking
    private var saccadeExitStart: CFTimeInterval?  // When velocity first dropped below exit threshold
    private static let saccadeRampDuration: Double = 0.05  // 50ms ramp-in after saccade

    // Blink/tracking-loss detection: count consecutive nil frames
    private var consecutiveNilFrames: Int = 0
    private static let maxNilBeforeReset: Int = 3  // Reset filter after 3 missed frames

    /// Calibration mode: when set, raw deltas are sent to this handler instead of WebSocket.
    /// Used by GazeCalibrationSheet to measure eye movement range.
    var calibrationHandler: ((Float, Float) -> Void)?

    init(ws: WebSocketManager, settings: SettingsStore, sharedFaceSession: SharedFaceSession) {
        self.ws = ws
        self.settings = settings
        self.sharedFaceSession = sharedFaceSession

        // Initialize 1-Euro filters with gaze-appropriate parameters
        filterDx = OneEuroFilter(
            minCutoff: settings.gazeFilterMinCutoff,
            beta: settings.gazeFilterBeta,
            dCutoff: settings.gazeFilterDCutoff
        )
        filterDy = OneEuroFilter(
            minCutoff: settings.gazeFilterMinCutoff,
            beta: settings.gazeFilterBeta,
            dCutoff: settings.gazeFilterDCutoff
        )

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
        filterDx.reset()
        filterDy.reset()
        saccadeState = .tracking
        saccadeExitStart = nil
        consecutiveNilFrames = 0

        sharedFaceSession.addConsumer(Self.consumerID) { [weak self] anchor in
            Task { @MainActor [weak self] in
                self?.handleAnchorUpdate(anchor)
            }
        }
    }

    func stop() {
        sharedFaceSession.removeConsumer(Self.consumerID)
        prevGazeDir = nil
        filterDx.reset()
        filterDy.reset()
        saccadeState = .tracking
        saccadeExitStart = nil
        consecutiveNilFrames = 0
    }

    // MARK: — Gaze delta extraction

    private func handleAnchorUpdate(_ anchor: ARFaceAnchor) {
        guard let settings, settings.gazeEnabled else { return }

        let gazeDir = extractGazeDirection(from: anchor)

        // Blink/tracking-loss detection
        guard let gazeDir else {
            consecutiveNilFrames += 1
            if consecutiveNilFrames >= Self.maxNilBeforeReset {
                // Reset filters to prevent spurious delta from stale history on return
                filterDx.reset()
                filterDy.reset()
                prevGazeDir = nil
            }
            return
        }
        consecutiveNilFrames = 0

        guard let prev = prevGazeDir else {
            // First frame — just store, no delta to send
            prevGazeDir = gazeDir
            return
        }

        // Compute angular delta in the gaze direction
        let rawDx = gazeDir.x - prev.x
        let rawDy = gazeDir.y - prev.y

        prevGazeDir = gazeDir

        // Axis correction: ARKit eye forward vector Y decreases when looking down,
        // but we need positive dy for downward gaze (cursor moves down).
        // Horizontal axis is correct as-is (look right → positive dx).
        let correctedDx = rawDx
        let correctedDy = -rawDy

        // If calibration is active, send raw deltas there instead of normal processing
        if let handler = calibrationHandler {
            handler(rawDx, rawDy)
            return
        }

        let now = CACurrentMediaTime()

        // 1-Euro adaptive filter (per-axis)
        // Replaces fixed EMA — adapts cutoff based on movement speed:
        // strong smoothing during fixation, minimal lag during saccades.
        let filteredDx = filterDx.filter(Double(correctedDx), timestamp: now)
        let filteredDy = filterDy.filter(Double(correctedDy), timestamp: now)

        // Compute gaze velocity (degrees/second) for saccade detection
        let frameRate: Double = 60.0  // ARKit nominal rate
        let velocityDegPerSec = sqrt(filteredDx * filteredDx + filteredDy * filteredDy)
            * (180.0 / .pi) * frameRate

        // Saccade detection state machine
        let saccadeEnter = settings.gazeSaccadeEnterThreshold  // default 100°/s
        let saccadeExit = settings.gazeSaccadeExitThreshold    // default 50°/s
        var outputScale: Double = 1.0
        var isSaccade = false

        switch saccadeState {
        case .tracking:
            if velocityDegPerSec > saccadeEnter {
                saccadeState = .saccade
                saccadeExitStart = nil
                outputScale = 0.0
                isSaccade = true
            }

        case .saccade:
            isSaccade = true
            outputScale = 0.0
            if velocityDegPerSec < saccadeExit {
                if saccadeExitStart == nil {
                    saccadeExitStart = now
                } else if now - saccadeExitStart! >= 0.03 {
                    // Below exit threshold for 30ms — transition to ramp-in
                    saccadeState = .rampIn(start: now)
                    saccadeExitStart = nil
                }
            } else {
                saccadeExitStart = nil
            }

        case .rampIn(let rampStart):
            let elapsed = now - rampStart
            if elapsed >= Self.saccadeRampDuration {
                // Ramp complete — back to normal tracking
                saccadeState = .tracking
                outputScale = 1.0
            } else {
                // Linear ramp from 0 to 1 over 50ms
                outputScale = elapsed / Self.saccadeRampDuration
            }
            // If velocity spikes again during ramp, go back to saccade
            if velocityDegPerSec > saccadeEnter {
                saccadeState = .saccade
                saccadeExitStart = nil
                outputScale = 0.0
                isSaccade = true
            }
        }

        // Confidence weighting: scale output by tracking confidence
        // ARKit doesn't provide per-frame confidence for eye tracking directly,
        // but we can use eye openness as a proxy (partially closed = lower confidence)
        let blinkLeft = Float(truncating: anchor.blendShapes[.eyeBlinkLeft] ?? 0)
        let blinkRight = Float(truncating: anchor.blendShapes[.eyeBlinkRight] ?? 0)
        let avgBlink = Double((blinkLeft + blinkRight) / 2.0)
        // Confidence: 1.0 when eyes fully open, decreasing as eyes close
        let confidence = max(0.0, min(1.0, 1.0 - avgBlink * 1.5))

        // Apply output scale (saccade suppression) and confidence weighting
        let scaledDx = filteredDx * outputScale * confidence
        let scaledDy = filteredDy * outputScale * confidence

        // Scale to useful cursor movement range
        let sensitivity = Double(settings.gazeSensitivity)
        let dx = scaledDx * sensitivity
        let dy = scaledDy * sensitivity

        // No hard dead zone — the 1-Euro filter's adaptive cutoff handles fixation noise.
        // Only suppress truly zero output to avoid unnecessary WebSocket traffic.
        guard abs(dx) > 0.01 || abs(dy) > 0.01 else { return }

        ws?.sendGazeDelta(dx: dx, dy: dy, confidence: confidence, saccade: isSaccade)
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
